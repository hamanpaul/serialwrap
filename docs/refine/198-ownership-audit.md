# #198 ownership 證據盤點與收斂建議

## 判定

本次只把目前程式與既有測試能支持的契約寫成證據；行號是當前
worktree 的 trace 佐證，不以靜態掃描宣稱「不存在其他 writer／reader」。

結論：#198 的方向可收斂，但原 issue 把三種不同語意混成「兩個 writer
只有一個成功」，且引用了錯誤 issue。未見已由現有證據證實的 ownership defect；
仍有下列可界定、應補測的契約缺口。未實作的補強不可在結案時稱為已修復。

## Issue 來源校正

- [#198](https://github.com/hamanpaul/serialwrap/issues/198) 原文把 #32、#33
  當成 ownership／診斷背景，與實際內容不符。
- [#32](https://github.com/hamanpaul/serialwrap/issues/32) 已 CLOSED，實際是
  binary file pull 的 `BASE64_DECODE_FAILED`（終端 ANSI 汙染）問題。
- [#33](https://github.com/hamanpaul/serialwrap/issues/33) 已 CLOSED，實際是
  DUT+STA paired-session preflight／wait-ready，並非 generic ownership。
- 因此 #198 的 ownership 論證不可把 #32/#33 當成既有 ownership defect 或
  acceptance evidence；歷史 #78、#83、#134 只能作現行行為的背景，不能替代本文件的測試證據。

## 實際 API 與已存在保證

### Command arbiter：接受多 client，正常 epoch 內逐一執行

- `sw_core/arbiter.py:46-56` 以 `session_id` 建立 queue／worker；
  `:125-213` 在同一把 lock 下 admission、record、enqueue，並非 writer token。
- `sw_core/service.py:887-910` 的 `command.submit` 只把來源標在 `source`；
  多個 client 可同時得到 `ok=true,status=accepted`，之後由該 session queue
  序列化。故「兩個 command writer 競爭只有一個成功」不符合現有 API。
- `tests/test_stress_submit.py::TestStressSubmit::test_10_agents_10_commands_concurrent`
  （`:82-119`）、`tests/test_multiagent_stress.py` 的
  `test_agent_a_hang_does_not_block_agent_b_queue`（`:25-66`）及
  `test_interleaved_good_bad_from_multiple_agents`（`:68-110`）直接支持
  「多 client accepted、結果逐一收斂」；`tests/test_multiagent_e2e.py::TestMultiAgentE2E::test_five_agents_three_rounds_no_conflict`
  （`:131-`）提供 daemon／假 PTY 的整合證據。
- 這個「一 session 一 worker」只應限定在**正常已註冊 epoch**。`unregister`
  會在 `arbiter.py:58-82` 原子 flush 後 join；但舊 callback 可能仍在 join timeout
  之外執行。`tests/test_arbiter_flush_on_recovery.py::test_reregister_submit_during_unregister_join_window_not_flushed`
  （`:185-247`）明確允許此時 re-register 新 worker，證明的是 queue epoch flush
  正確，不是跨 recovery 的物理 single-writer。

### Interactive lease：才是「競爭只有一個 lease」的語意

- `sw_core/session_manager.py:230-248` 的 `InteractiveLease` 以
  `interactive_id` 作 capability；`:3067-3090` 在 lock 內登記唯一
  `session.interactive_session_id` 並設定 bridge raw owner。
- `interactive_open`（`:3862-3930`）對既有 agent lease 回
  `SESSION_INTERACTIVE_BUSY`；human lease 若近期有輸入同樣 busy，若 idle 則
  soft-preempt、暫存 human、agent close 後還原（`:3885-3929`）。
- 直接 assertion：
  `tests/test_cowork_session_usability.py` 的
  `test_agent_soft_preempts_idle_human`（`:471-481`）、
  `test_agent_cannot_preempt_active_human`（`:483-491`）、
  `test_soft_preempt_restores_human_on_close`（`:493-505`）、
  `test_agent_lease_not_preempted_stays_busy`（`:507-538`）。
- agent 命令遇 human lease 會在 `session_manager.py:3486-3549` suspend／resume
  raw interactive；`tests/test_interactive_raw.py::test_human_input_deferred_during_agent_then_flushed`
  （`:268-305`）及 `tests/test_suspend_resume_reentrant.py` 的四個案例（`:24-63`）
  覆蓋 deferred、巢狀 suspend 與 outermost resume。

### 失效、重連與 FLASHING

- `interactive_send`（`session_manager.py:4063-4090`）對 unknown／closed／expired
  id 不應送 UART；現有 `tests/test_bootloader_recovery.py::test_interactive_send_expired_recovery_restores_and_returns_expired`
  （`:775-791`）直接覆蓋 expired recovery，並驗證 human stash 還原。
- human lease 的 peer 消失不是立即失效：`_refresh_interactive_locked`
  （`:3148-3187`）保留 `_HUMAN_PEER_GRACE_S`，回來可延續同一 lease；逾 grace
  才 close。`test_peer_flap_within_grace_keeps_lease`（`:405-428`）與
  `test_peer_gone_past_grace_tears_down`（`:430-451`）已直接 assertion。
- `sw_core/uart_io.py:859-891` 的 `send_bytes` 只有 `_write_lock` 的物理寫入
  序列化；`source` 不是授權 token。FLASHING 另由 flash gate 擋非 flash 寫入，
  `tests/test_flashing_state.py::test_cmd_submit_rejected_while_flashing`
  （`:40-45`）及 `tests/test_uart_flash_bridge.py::test_flash_mode_blocks_console_injection`
  （`:33-57`）支持此界線。不能把它解讀成一般 writer lease。

## 不存在於現行 API 的語意

- 沒有 generic writer token、generic reader ACL，亦沒有 `interactive_send` 的
  client identity／owner 參數；public interactive API 實際只收 `interactive_id`
  （`sw_core/service.py:836-865`）。`interactive_status` 會 `lease.touch()`
  （`session_manager.py:4092-4118`），因此也不是完全無副作用的 read-only probe。
- 第二個 console 並非「read-only client」：`uart_io.py:730-781` 對非 raw owner
  的輸入採 line buffer，再由 `service.py:309-318` 以 `human:<client_id>` 送入
  arbiter；raw bytes 不直通，但換行命令仍是 broker work。既有
  `tests/test_interactive_raw.py::test_second_console_does_not_get_interactive`
  （`:117-137`）只支持「不能取得 raw interactive」，不能支持 ACL 全稱。
- `file.push`／`file.pull` 由 `service.py:1061-1123` 直達 SessionManager，
  不經 arbiter；`file_transfer.py:45-216` 直接送多段 bridge command。
  因此不能把 command queue 的 single-writer 證據外推到檔案傳輸。

## 最小補強（3–5 項，均為建議／尚未實作）

1. **stale interactive id matrix**：在 close、soft-preempt、expiry、unknown id
   各路徑呼叫 `interactive_send`，assert 明確錯誤碼且 fake bridge 的
   `send_bytes` 次數為零；保留現有 expired case，補 close／replacement／unknown。
2. **第二 console 語意固定**：owner A + secondary B 的整合測試須 assert B 的 raw
   bytes 不直通，而 newline 是 line-buffer broker command；若產品真正要
   read-only，先新增明確 capability／ACL API，再測拒絕，不得從 owner bool 推論。
3. **file-transfer contention**：用 blocking fake bridge 競爭兩個 `file.push`／
   `file.pull`（並至少一個 command path），明定是序列化或拒絕，assert 不發生
   logical transfer 交錯；若刻意不共用 gate，須把它列為 scope 外契約。
4. **reconnect end-to-end**：human peer 在 grace 內斷線／重連、逾 grace 後以舊
   id 發送，以及新 client 依公開流程取得 lease；確認舊 id 不可跨 close 寫入。
5. **recovery epoch integration**：阻塞舊 `send_cb` 後 re-register／換 bridge，
   assert 舊 epoch 不會寫新 bridge 或清掉新 epoch 的 `foreground_busy`；在未有此
   整合測試前，只列為 bounded residual，不判已知 defect。

## 結案契約

- 可宣稱的範圍：正常 registered epoch 內 command queue per-session serialization；
  interactive API 的單一 lease/raw owner、active BUSY、idle human soft-preempt／
  restore；FLASHING 對非 flash 寫入的 gate；secondary console 是 line-buffer
  broker client，不是 generic read-only client。
- Root 應對上列命名測試及新增補強逐項執行（再依 repo 政策統一跑完整 pytest）；
  文件／測試未落地前，不得寫成「已修」。
- 文件需列出尚未補強的 file-transfer、跨 recovery epoch、reconnect／reader ACL
  風險及 owner／下一個測試。若缺口未處置或未明確列管，review 判定 FAIL；有界且
  明文列管的 residual risk 不單獨構成 FAIL。
