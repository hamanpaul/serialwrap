> 審查者：claude-fable-5-1（重型任務，擁有者 2026-11-07 指定）

無殘留 throwaway daemon（只有驅動本次審查的 driver 與我自己的行程）。審查完成，以下為報告。

## A 前輪 finding 核對

無（本輪為 P8X-7af8adc-1 的首次增量審查，無前輪報告表）。

## B 新發現

審查範圍：36d78dfb（測試）、d39e2018（修正），無 merge commit。

核對結論：

- **Python 判定順序對照**：`sw_core/session_manager.py:6542-6599` 的 `execute_command` 順序為 completion regex → FLASHING → 非 READY → agent gate（human 免）→ recovering → interactive 模式遇前景占用回 `SESSION_BUSY`＋selector（在 lease refresh 之前）→ `_refresh_interactive_locked` → 非 interactive 遇 lease（agent 遇 human lease 放行、否則 `SESSION_INTERACTIVE_BUSY`）→ `_begin_foreground_operation_locked` 回 `SESSION_BUSY`＋selector（`session_manager.py:1712-1717`）。Rust `reject_queued_during_file_transfer`（`crates/sw-core/src/session_core/file_xfer.rs:72-97`）以 `execute_time_gate_reject` → interactive 跳過 lease 段 → `execute_time_lease_reject` → `selector_error("SESSION_BUSY")` 完整對應，回應鍵集一致。
- **一般出列路徑未變**：`execute_time_reject` 拆成兩段後以 `or_else` 串接（`session_core.rs:4266-4269`），順序與拆分前相同；`mode` 變數移入 lease 段，無行為差。
- **觸發點**：`submit`（`session_core.rs:2120-2123`）與 console line 出列（`console.rs:693`）皆在 push 後立即呼叫 `start_next`，命令在傳輸進行中即結算，不會等傷輸結束。
- **Human lease 邊界**：傳輸期間 human lease 被 suspend，Rust `ownership.suspend()` 把 `interactive_owner` 取為 None（`human_console/ownership.rs:66`），`refresh_inner(rpc=true)` 因 owner 不符關閉 lease；Python `suspend_interactive()` 同樣設 `_interactive_owner = None`（`uart_io.py:2797`），`_refresh_interactive_locked` 以 snapshot owner 不符關閉（`session_manager.py:6133-6135`）。兩邊一致，無差異。
- **replay_parked 交互**：`replay_parked` 只在 replay hold 有效時存在，而 `file_transfer_admit` 以同一個 `fg_slot_busy()`（含 hold）拒絕，兩者不可能並存；新分支忽略 `replay_parked` 不構成失敗情境。
- **鎖檔**：`python3 tests/acceptance/regen_lock.py --check --base 7af8adc4` 通過（只增、結構一致）；三集合各登記 1 筆，符合慣例。
- **實測**：`cargo test -p sw-core --features test-fakes --test file_transfer_session` 21 passed（含新測試）；黑箱 `test_gate_resolve_quiet[command_during_transfer]` Python 基線 1 passed（Rust artifact 需 bundle，未在本次重跑，沿用 commit 訊息所述 14 passed 作為實作者證據）。

- **P8X-d39e201-1** [Low] `crates/sw-core/tests/file_transfer_session.rs:407` — 新測試只覆蓋 RPC 來源三種模式，未覆蓋「傳輸期間存在 human lease（已 suspend）時送入 agent 命令」的分支：此時 Python 與 Rust 都會先因 owner 不符關閉 human lease 再回 `SESSION_BUSY`，是本次修正新接通的唯一帶副作用路徑，目前無測試釘住其一致性。無具體失敗情境（兩邊實作已核對一致），屬覆蓋建議。修法：在 `file_transfer_session.rs` 加一案例，先 console-attach 取得 human lease、啟動 push、送 agent line 命令，斷言命令 `SESSION_BUSY`、lease 已關閉、傳輸結束後 DUT 未收到命令。

## 既有碼待辦（不計入結論）

- `crates/sw-core/src/session_core.rs:4356-4363`：一般出列路徑中 interactive 命令被 `execute_time_reject` 拒絕時，不清除 `tick_close_interactive` 標記（新路徑有清），該 cmd_id 永不再出列，標記殘留直到 drain Phase 1 的 `clear()`；只有記憶體殘留，無外部可觀察差異。

## 對裁決的異議（不計入結論）

無。

結論：可放行
