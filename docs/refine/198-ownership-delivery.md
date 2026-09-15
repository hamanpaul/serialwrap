# #198 ownership 收斂交付

## 狀態

2026-09-15 最新：使用者指定由 root 接替未完成審查；已完成最終讀碼、反例核對與
實跑，裁決 PASS，詳見 [root 最終審查](198-root-final-review.md)。
下方 Sol 中止的文字為先前紀錄，不再是等待中的交付門檻，也不改稱 Sol PASS。

已完成本 worktree 的 ownership admission 與 operation/epoch cleanup 實作及
pytest 驗證；尚未包含真機 UART、live daemon、merge 或部署驗證。root 整合時需
保留本文件與 `changelog.d/198-ownership.md`，並將共享
`sw_core/session_manager.py` 與其他 task 逐 hunk 合併。

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

`tests/test_refine_ownership.py` 使用 Event/barrier、SessionManager 及可記錄 TX 的
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

## Root review 後續修正（2026-09-13）

初始候選 `6d946ec` 仍有兩項 root 獨立重現的缺口，已於 Luna max 候選
`9ac9d12` 修正，整合為 `aa278db`／`cda607f`：

1. 傳輸期間新 console 仍可被授予 human raw owner：root 隔離 RED 1 failed
   （0.13s）。現在 manager 發佈 operation token 前先關 bridge admission，
   POSIX／TCP 新 console 沿 line broker，human raw grant 留待 gate 重開。
2. raw RX 通過檢查後、真正寫入前才關 gate，舊 bytes 仍會插入：root 以真
   `send_bytes` 與 write lock、mock 最底層 `_write_all` 重現 RED 1 failed
   （0.06s）。現在用 thread-local 內部身分標示 console raw 路徑，在 write→state
   鎖序內重驗 gate，不以呼叫者可指定的 `source` 字串作權限判斷。

被擋住的已接收 raw bytes 進 bounded deferred buffer，不靜默丟失；收尾在
manager lock 內核對 operation／session／bridge／generation 並擷取回放資料，
在 lock 外做 UART I/O，最後才依身分清 busy。已有 TCP human owner 但 manager
尚無 lease 的情境亦有回放 assertion。原有 agent interactive 接管不排入 human
pending；FLASHING 仍先丟棄輸入，不可改成延後回放。

關鍵新 assertions（均在 `tests/test_refine_ownership.py`）：

- `test_new_console_during_transfer_does_not_send_raw`、pull／command 對應案例及
  `test_new_console_gate_reopens_after_operation_exception`。
- `test_raw_write_rechecks_admission_at_actual_write`：關 gate 時零實際 write，
  開 gate 後只回放一次。
- `test_operation_finish_replays_raw_bytes_without_manager_lease`：真 write 邊界
  回放一次，另一 thread 可取得 manager lock。
- `test_interactive_command_replaces_existing_human_bridge_owner`：未啟動的真
  UARTBridge 驗 agent owner replacement，不以缺 gate 的 fake bridge 自證。
- Root 另增 `tests/test_refine_flash_precedence.py`：raw RX 快照後同時關 admission
  並進入 FLASHING，驗證不寫入、不排 deferred、不得延後回放。

worker 最終全套：1705 passed、16 skipped、44 subtests，93.42s；policy
24 pass、0 fail、2 warn。Root 候選定向兩組：63 passed／6 subtests（2.66s）及
65 passed／9 skipped／9 subtests（8.77s）；合併 ownership、CLI、holder 與 root
FLASHING assertion 後定向 45 passed／2 subtests（0.42s）。完整整合結果另見
[merge-summary](merge-summary.md)。Sol 的 ownership 審查由平台中止、Opus 受額度
限制，這些測試與 root review 不代表已完成指定雙審。

回歸歸屬：本輪已重現的排程／gate／回放缺陷可由 pytest、PTY／socketpair 與
最底層 write 替身覆蓋，因此不新增實機 `regression/` case。真板、native Windows、
長時 UART 背壓及外部 process 持有者仍需另外驗證，不以本輪離線綠燈代替。
