> 審查者：claude-fable-5-1（重型任務，擁有者 2026-11-07 指定）

## A 前輪 finding 核對

無（本包無前輪報告）。

## B 新發現

審查範圍 `b3a9d37f..3fdbce63` 共 3 個線性 commit（無 merge commit）：`b70d7a2b`（核心修正＋3 筆 sw-core 測試）、`cd039f02`（3 筆 sw-daemon RPC 端到端測試）、`3fdbce63`（L1 鎖 add-only 登記）。

**核對結果（High／Medium）：NO FINDINGS。**

核對依據摘要：

- **接線完整**：`OpRunner::run_command`／`run_reboot` 皆改走 `command_tx_class(q)`，只看 `SubmitReq::is_trusted_human()`（`trusted_human` OR `HumanConsole` origin），不看 source 字首。對應 Python `session_manager.py:6549`／`:6399` 的 `human = trusted_human`。Python `service._send_cb` 的 `trusted_human: bool = False` 預設保證 RPC 路徑永遠傳明確布林，故明寫 `human:cli` 在 Python 亦走 `agent` permit，Rust 的 `human:cli` queued → `AUTOBOOT_QUIET` 與基線一致，且符合 OD-74／DF-02。
- **Fence 規則與 `uart_io.py:621-670` 逐項對照**：`HumanLine` 仍受 draining（Frozen）、flashing、stale_generation（epoch）、cancelled 擋；quiet 只擋 `agent`（`:640`）；不比對 owner_gen（Python 只對 `interactive`／`human_raw`）；WAL unhealthy 只擋 system probe。Rust 的 `reconfirm_pending` fence 是 Rust 自有設計，Python 的 reconfirm 僅存在於 submit／execute gate，可信 human 在 Python 本就不受其擋，故 `HumanLine` 豁免 reconfirm 無偏差。
- **reboot arm**：`session_manager.py:6430-6446` 的 `arm_boot_quiet` 不分來源，`tx_writer.rs:644` 改成 `Command | HumanLine` 正確；`reboot_arm_op` 清除順序不變。
- **其他 `TxClass` 使用點**（`login_fsm.rs`、`console.rs`、`human_tx.rs`、`session_manager.rs`）皆為具名類別或透傳，無 exhaustive match 被新變體誤入；`reject_reason` 與 `FenceReject` → error_code 映射與類別無關。
- **FileTransfer 不受影響**：Python `file_push`／`file_pull` 明傳 `trusted_human=False`，Rust `FileTransfer` 仍受 quiet 擋，一致。
- **回歸測試在無修正時會紅**：sw-core 三筆引用 `TxClass::HumanLine` 無法編譯；sw-daemon 的 `wp55_rpc_bare_human_bypasses_quiet` 在舊碼會因 TxWriter 回 `Quiet` → `AUTOBOOT_QUIET` 而非 `done` 失敗。測試同時以 UART bytes、WAL `cmd_id` TX 紀錄、registry、queue 長度四維核對，且 `trusted_human:true` RPC 參數不授權有覆蓋。
- **凍結鎖**：`3fdbce63` 僅於 `s2_par_wp55:cargo/linux` 新增 6 筆 nodeid，無移除，符合 add-only。
- **定向實測**（`CARGO_TARGET_DIR=/tmp/solrev-p8-rust-refactor-p8-target`）：

| 目標 | 結果 |
|---|---|
| `cargo test -p sw-core --features test-fakes --test tx_writer wp55_` | 3 passed |
| `cargo test -p sw-daemon --features test-fakes --test dispatch wp55_` | 3 passed |

  clippy（`--cap-lints warn`）在本輪 diff 行無新警告（命中的 `op_runner.rs:79/100`、`tx_writer.rs:901` 皆為既有行）。工作樹保持 clean。

Low（不擋放行）：

- **P8-3fdbce6-1** [Low] `changelog.d/s2-par-wp55-gates.md:3` — fragment 的 `scope: sw-daemon` 與同包兩筆 `240-wp55-*.md` 的 `scope: rust-refactor` 不一致，實際改動在 `sw-core`；檔名也未沿用同包的 `240-` issue 前綴。無功能影響，R-09 僅驗 direct child `.md`。修法：改 `scope: rust-refactor` 並更名為 `240-wp55-human-line-tx-class.md`（collate 前處理即可）。

## 既有碼待辦（不計入結論）

- 可信 human 命令逾時的 interactive lease 升格（Python `_recover_after_failure` 的 human 路徑、`source_policy.py` 模組說明第 2 點）：Rust `op_runner.rs` 目前只有 `command_tx_class` 讀取 `is_trusted_human()`，逾時處理尚未分流。歸屬 **wp61（WP6-1：bare human 逾時升格）**。

## 對裁決的異議（不計入結論）

無。

結論：可放行
