# wp510r 第 23 項：退休驗證失敗的關閉終態補收

起點：`8b327e23`，分支 `feature/rust-refactor-p8`。範圍限整包第 5 次審查的單一 Medium，依 2026-11-12 擁有者補充裁決處理。

## 修正與 Python 對照

`SessionCore::retire_result` 處理 `RetireOutcome::Invalid { closing: true }` 時，已將 token 登記為 retired drain 並保留 `pending_attach`，但原本沒有排補收計時器。關閉者以 `try_send` 投遞終態遇 Full 後，帳本雖已結算，actor 仍可能永久等待。

本次在登記等待後呼叫既有 `arm_custody_poll(now)`。補收只查詢帳本終態，再經原有 `custody_terminal` 清理等待並回覆；不更動 actor／manager 的所有權分工、D1／T-1 或凍結分類。

Python 基準為 `origin/main:sw_core/session_manager.py`：3614–3622 行在驗證失敗後先 `_finish_attach_op()`，再回四鍵 `DEVICE_NOT_PRESENT`；4320–4365 行同步關閉、清理作業表並設定 `done`。Rust RPC 同樣保留原 selector，另由 service 附加 `version`。

## 等待登記分支盤點

目前 `awaits` 的實作由 `retired_drains` 共表承擔；`session_core.rs` 共四處加入項目，沒有其他 token／epoch 登記入口：

| 函式 | 等待對象 | 補收／回條路徑 | 結果 |
|---|---|---|---|
| `release_claimed` | `ReleaseClaim.token` | 回傳前 `arm_custody_poll(now)` | 原已涵蓋 |
| `custody_claimed` | `ClaimForStop` 的 token | 登記後 `arm_custody_poll(now)`，失敗與收斂分支均保留 effects | 原已涵蓋 |
| `retire_result` 的 `Invalid { closing: true }` | 退休驗證失敗的 token | 登記後 `arm_custody_poll(now)` | 本次補齊 |
| `retire_opened_bridge` | `(epoch, stop_id)`，`custody_token: None` | `BeginDrain` → `IoWorker::finish_async` → 阻塞 `send(WorkerStopped)`，成功與 panic 回條皆不使用 `try_send` | 使用 worker 回條；無帳本 token 可輪詢 |

另核對 `begin_external_attach`：等待 W6 offer 時直接排 `custody_poll_schedule`；既有 `custody_poll` 會在等待未清空時續排，空集合即停止。沒有新增事件、請求或效果：既有 Timer 在 Open 處理、Pending 延後至撤銷重播、Permanent 仍處理收尾。

## 正式 RPC 接線與回歸

新增 `dispatch::wp510r_i23_rpc_offline_invalid_retire_terminal_full_poll_completes_attach`，使用 ServiceRig 與記憶體 FakeSerial，不開啟實體序列埠。

接線為 `Service::rpc(device.attach)` → `SessionManager::route_with_selector` → `DeviceAttachRouted` → actor 準備交接 worker → `OpenPlaceholderRaw` → `PlaceholderOpened` → `RetirePlaceholder` → `ManagerExec::retire_placeholder` → `RetireResult::Invalid { device: true, closing: true }` → `CustodyPoll` → `PollCustody` → `CustodyTerminal` → RPC 回覆。

測試在退休提交前暫停 actor 並填滿 control lane。watcher 經正式 `update_devices` 更新離線快照後，被 `DeviceMissing` 投遞擋住，故 handle 仍由退休驗證失敗分支取得並交 closer。接著讓 actor 停在下一筆查詢，確認 `Closing { job: "retire-invalid" }`，關閉成功時維持 lane 滿載，並核對真實 `try_send(CustodyTerminal)` 遇 Full 的觀測值。

恢復容量、推進 FakeClock 600 ms 後，逐位元組比對 `DEVICE_NOT_PRESENT`、原 alias selector 與 `version`，並確認佔位仍為 RELEASED、drain 欄位清空、帳本／等待／prepared／inflight 清空、沒有退休落盤、只開關一次且零 TX。裝置重新上線後再次呼叫 `device.attach`，確認正式 `t:COM3` 到 READY、佔位退休，總共只另開一次。

產品原始碼保持起點內容時，新回歸失敗於「補收後 attach 應完成 DEVICE_NOT_PRESENT: Timeout」（0 passed、1 failed）；修正後同一案例通過（1 passed、0 failed）。輸出位於 `target/s2-par/wp510r/i23-red.log` 與 `i23-green.log`。這是 ServiceRig 端到端證據，不是 Windows 原生或硬體證據。

## SPEC-GAPS 與既有碼待辦

- 本項未變更一般 session 的 `attach_open_failed` 回應建構；該函式的 Held 分支仍以 `error_session("DEVICE_STILL_HELD", now)` 產生額外 `session` 鍵，對應 brief 所載的 mergep8x 既有黑箱差異。此路徑不在本項退休驗證失敗的補收範圍內，保留待別包處理；本輪未重跑 `test_s24_device_handoff.py`，不宣稱先前的五項失敗現況已重新驗證。
- workspace 初跑在既有 `cargo:sw-core::custody::test_dr1_d_release_not_enqueued_keeps_ledger_then_next_release_completes` 失敗：`custody.rs:2901` 預期 `ok:true`，實際為 `DRAIN_STALLED`。在同一工作樹移除本次產品修改後，`git diff --exit-code 8b327e23 -- crates/sw-core` 為空；起點的 custody 全檔為 69 passed，但同一節點聚焦重跑即在同一行重現（0 passed、1 failed、68 filtered out）。證據為 `i23-baseline-source.diff`、`i23-baseline-custody.log`、`i23-baseline-dr1d-1.log`；不修改此既有測試或產品路徑，不將失敗歸因於環境。
- 完整 clippy 的既有錯誤為 `sw-proto/src/pyre/parse.rs:587` 的 `clippy::never_loop`。`sw-proto` 不依賴其他 workspace crate，與 `8b327e23` 的 source diff 為空；`cargo clippy -p sw-proto --lib` 單獨重現相同錯誤。保留完整 gate 的 exit 101，不加 lint 豁免、不修改範圍外程式。證據為 `i23-clippy.log`、`i23-clippy-baseline-source.diff`、`i23-clippy-baseline-proto.log`。
- 本輪沒有不成立主張。

## 驗證結果

以下輸出全部位於 `target/s2-par/wp510r/`。所有測試皆為前景執行並等待結束；沒有執行 slice gate、Docker 或出口 driver。

| 命令／檢查 | 結果 | 證據 |
|---|---|---|
| 新回歸（`cargo test -p sw-daemon --features test-fakes --test dispatch wp510r_i23_ -- --nocapture`） | 1 passed、0 failed | `i23-green.log` |
| `cargo test -p sw-core -p sw-daemon --features test-fakes` | 2119 passed、0 failed、7 ignored | `i23-affected-tests.log` |
| `cargo test --workspace --features test-fakes --no-fail-fast` | 3482 passed、0 failed、51 ignored；原本兩次 workspace 執行的單一既有失敗保留，見上節 | `i23-workspace-complete.log`、`i23-workspace-tests.log`、`i23-workspace-tests-rerun.log` |
| `cargo build --workspace --all-targets` | exit 0；警告位於未修改的 PAL／proto／CLI 測試等既有位置 | `i23-build.log` |
| `cargo clippy --workspace --all-targets --features test-fakes` | exit 101；起點既有 `never_loop`，未改判 PASS | `i23-clippy.log` |
| `cargo clippy -p sw-core -p sw-daemon --all-targets --features test-fakes --no-deps` | exit 0；既有警告保留，本次新增測試的 unit variant 語法警告已修正，斷言語意不變並重跑新回歸 | `i23-clippy-affected.log`、`i23-green.log` |
| Windows GNU：workspace、all-targets、test-fakes | exit 0；交叉編譯，非 Windows 原生證據；既有 unused／dead-code 警告保留 | `i23-windows-check.log` |
| `python3 scripts/rust/check_cfg_boundary.py` | exit 0 | `i23-cfg-boundary.log` |
| `python3 -m tools.contracts check` | `ok:true`、findings 0 | `i23-contracts.log` |
| L1：repo 收集器 list-only → `regen_lock.py --l1-add-only --set s21_core --platform linux` | 新增本項 1 個節點、移除 0；selector 與既有節點不變 | `i23-l1-collected.json`、`i23-l1-add.log` |
| `python3 tests/acceptance/regen_lock.py --check --base 8b327e23` | 案例／統計／L1 結構與起點比對皆 OK | `i23-lock-check.log` |
| changelog 單測 | 1 passed、2 個 unknown-marker 警告 | `i23-changelog-test.log` |

changelog 單測直接使用 `python3 -m pytest -q --noconftest --basetemp=target/s2-par/wp510r/i23-pytest-tmp tests/test_changelog_fragments.py`。該測試只驗 fragment 解析／收斂；不載入會讀取正式設定、狀態與探測 daemon 的全域 conftest，因此其註冊的兩個 marker 未載入而示警。未修改測試斷言或全域 conftest，未執行全套 pytest。
