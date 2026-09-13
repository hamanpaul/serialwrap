# #198 ownership 收斂交付

## 狀態

已完成本 worktree 的 ownership admission 與 operation/epoch cleanup 實作及
pytest 驗證；尚未包含真機 UART、live daemon、merge 或部署驗證。root 整合時需
保留本文件與 `changelog.d/198-ownership.md`，並將共享
`session_manager.py` 與其他 task 逐 hunk 合併。

## 修復內容

- 在 `SessionManager` lock 內以 `_ForegroundOperation` 保存 session 身分、
  bridge 身分與 `bridge_generation`，讓 foreground command、`file_push`、
  `file_pull` 的 admission 與釋放使用同一個內部 token；不新增 public API 或
  public session 欄位。
- 同一 registered session 已有 logical operation 時，後到的 direct manager
  writer 回 `SESSION_BUSY`，在取得 bridge 前即結束，因此不會產生 UART TX。
  正常 `CommandArbiter` queue 仍由既有 per-session worker 逐一執行，多 client
  submit 的 accepted 契約不變。
- `interactive_open`、`interactive_send` 與 interactive-mode command 也檢查
  foreground admission，避免 lease 或 interactive handle 成為傳輸／前景命令的
  writer 旁路。`FLASHING_BUSY`、`SESSION_INTERACTIVE_BUSY` 原有優先語意保留。
- detach/re-register 時撤銷舊 operation token；遲到 callback 的 finally 只在
  token 仍是同一 session 的目前 operation 時清 busy，不能清除新 bridge／新
  epoch 的操作狀態。human suspend/resume 仍在 lock 外執行，resume 或 post-close
  例外也會進入 token cleanup。
- `foreground_busy`、`fg_cmd_started_mono`、`fg_cmd_expected_duration_s` 的
  原有觀測欄位與 background capture 行為保留；背景命令仍可被既有 RX capture
  路徑收集。

## 測試證據

### RED（production 修改前）

在新增 behavioral tests 後執行：

```text
python3 -m pytest -q tests/test_refine_ownership.py
6 failed, 1 passed
```

失敗是已重現的缺口：push/pull/foreground admission、execute inner 前窗口、
interactive writer 旁路，以及舊 epoch finally 清掉新 busy；未知／closed／
expired／replaced interactive ID 的 characterization 已通過。未以靜態掃描代替
競態行為驗證。

### GREEN 與定向回歸

```text
python3 -m pytest -q tests/test_refine_ownership.py
11 passed in 0.10s

python3 -m pytest -q tests/test_refine_ownership.py tests/test_resilience_timeout.py::TestCommandCancel::test_canceled_command_skipped_by_worker
12 passed in 0.12s

python3 -m pytest -q tests/test_refine_ownership.py tests/test_issue159_bg_fast_capture.py tests/test_issue24_heartbeat.py tests/test_cowork_session_usability.py
39 passed in 1.17s

python3 -m pytest -q tests/test_bootloader_recovery.py tests/test_file_transfer_chunk_config.py tests/test_interactive_raw.py
112 passed, 3 subtests passed in 2.36s

python3 -m pytest -q tests/test_flashing_state.py tests/test_arbiter_flush_on_recovery.py tests/test_multiagent_stress.py tests/test_suspend_resume_reentrant.py
36 passed, 6 subtests passed in 10.75s
```

`test_refine_ownership.py` 使用 Event/barrier、SessionManager 及可記錄 TX 的
fake bridge；包含 push↔push、command↔pull、pull↔command、execute inner 前置
窗口、bridge detach/re-register epoch、interactive open/send/interactive command
防旁路，以及 post-close/resume 例外的 cleanup fault injection。沒有使用 sleep
猜測競態，也沒有讀寫真實 UART。

Step 4 的決定性 assertion 對應下列 exact test node：

- `tests/test_refine_ownership.py::TestRefineOwnership::test_secondary_console_raw_bytes_use_line_broker_without_uart_tx`：secondary 的 raw bytes
  不會呼叫 UART TX；只有 newline 後才收到 `(client_id, line)` broker callback，且
  TX 仍為空。
- `tests/test_refine_ownership.py::TestRefineOwnership::test_human_peer_grace_rejects_old_id_and_allows_new_client`：peer-loss grace
  第一次 refresh 保留 lease；逾 grace 後舊 ID 的 `interactive_send` 回
  `INTERACTIVE_NOT_FOUND` 且 TX 為空；新的 human client 取得不同 interactive ID。
- 原有 `tests/test_interactive_raw.py::TestHumanLeasePeerLossGrace::test_peer_flap_within_grace_keeps_lease`
  與 `test_peer_gone_past_grace_tears_down` 保留 grace 狀態／拆除 characterization；
  上述新 node 補上舊 ID 拒絕與新 client lease 的正反端到端 assertion。

### Arbiter cancel fixture

Root integration 曾記錄 `python3 -m pytest -q tests/` 的
`1 failed, 1691 passed`（94.68s），失敗為
`tests/test_resilience_timeout.py::TestCommandCancel::test_canceled_command_skipped_by_worker`：
tracking callback 太快完成，submit 後立即 cancel 並未保證 `cmd2` 仍在 queue，因而
可能得到 `CMD_NOT_CANCELABLE` 或把已完成的 `cmd2` 算入 call log。修復只調整測試
排程：Event 讓 `cmd1` 明確在途後才 submit `cmd2`／`cmd3`，確認 cancel 終態，並以
bounded poll 觀測 worker 實際 terminal status；沒有改 arbiter production 契約。
Barrier 以 `try/finally` 釋放，避免 assertion 失敗遺留卡住 worker。

## 保證界線與殘餘風險

- 可宣稱的範圍是 manager 所控制之同一 session 的 logical writer admission、
  operation token cleanup、interactive handle busy gate，以及正常 arbiter queue
  契約；`interactive_id` 仍是 handle，不是 client ACL。
- secondary console 的既有契約仍是 raw owner 以外走 line-buffer，再經 broker
  callback；本 task 未把它改成 generic read-only API。
- 未驗證真板 boot、外部 flasher／其他 process 持 tty、physical UART 電氣時序、
  installed runtime 或 live daemon；這些屬後續真機／整合驗證，不在本 worktree
  聲稱範圍。
- operation token 不可能取代外部 process 的不可控寫入，也不宣稱所有未盤點
  callback 都不存在；若後續新增繞過 SessionManager 的 writer，必須重新接入
  相同 admission/epoch 邊界並補 behavior test。
