> 審查者：agy gemini-3.8-flash-high（代理 sol 單審位，擁有者 2026-11-02 指定）

## A 前輪 finding 核對

| 編號 | 原嚴重度 | 狀態 | 累計未修輪數 | 說明 |
|---|---|---|---|---|
| 1 | Medium | 已修 978aa1e1 | 0 | [test_cli_mgmt_daemon.py:143](file://tests/contract_blackbox/test_cli_mgmt_daemon.py#L143) 已移除 `_wait_dut_tx`，改以 bridge barrier 與 checkpoint 封閉 TX 擷取窗口，並新增 `test_checkpointed_dut_tx_includes_frames_written_before_barrier_settles` 覆蓋延遲落盤重複 TX。 |
| 2 | Medium | 已修 51bd8fa5 | 0 | [exec_delegate.rs:168](file://crates/sw-pal/tests/exec_delegate.rs#L168) 改由實際受委派的孫行程 helper 寫出 `DELEGATE_READY_ENV` 與 PID，父測試以 Win32 `OpenProcess` + `WaitForSingleObject` 確認孫行程活著後才發送事件，並在 `tests/test_rust_exec_delegate.py` 補齊突變案例。 |
| 3 | Medium | 已修 ddcbdde0 | 0 | [stage1a_gate.py:163](file://scripts/rust/stage1a_gate.py#L163) 相關的 driver exit 問題已由 `ddcbdde0` 新增 `scripts/rust/l15_exit_driver.py` 接通判定，並由 `4e3ba13f` 補齊 Windows L1.5 raw reports 與 metadata 的逐列核對，只在全數非 PASS 列皆為核准延後且無其他失敗時豁免 exit 1。 |
| 4 | High | 已修 12b3a929 | 0 | [run_upgrade_container.py:190](file://scripts/docker/run_upgrade_container.py#L190) 相關環境已自 `tools/docker/upgrade.Dockerfile` 移除 `python3-yaml`，pipx 舊版改由 wheelhouse 離線提供 PyYAML，`mkbundle.sh` 將 PyYAML 納入 runtime `py/` 隔離目錄，並經 `2ddf90e1` 與 `407ed7e3` 強化系統 Python 3.12 與 pyvenv.cfg 邊界檢查。 |
| 5 | Medium | 已修 12b3a929 | 0 | [release-0.4.0-plan.md:20](file://docs/rust-refactor/release-0.4.0-plan.md#L20) 所述正式發佈配方已在 `12b3a929` 改為 `mkbundle.sh --release-bundle --profile release`（不含 `sw-cli/test-fakes`）；`ac6c4e4e` 進一步將委派覆寫測試變數收納於 `cfg(feature = "test-fakes")` 守衛，`tests/test_release_workflow.py` 亦已補齊 release binary 排除測試鉤子之字串斷言。 |
| 6 | Low | 已修 12b3a929 | 0 | [release-0.4.0-plan.md:10](file://docs/rust-refactor/release-0.4.0-plan.md#L10) 已將發佈前置條件 1 改寫為實際 gate 狀態：接受 `VERIFIED`，或 `IMPLEMENTED` 且僅剩 OD-27～OD-32 明確允許之 `BLOCKED-OWNER`。 |

---

## B 新發現

NO FINDINGS

本輪審查範圍 `ac6c4e4e..5d62254b` 包含 2 個提交（無 merge commit）：
- [`d4f18756`](file://crates/sw-cli/Cargo.toml) `fix(sw-cli): 修正測試建置的 feature 啟用`：在 `crates/sw-cli/Cargo.toml` 的 dev-dependencies 引入自身 `test-fakes` 特性，使整合測試在 `cargo test` 時可透過 dev-dependencies 啟用測試注入鉤子，而 `cargo build --release` 正式建置產物不含任何 `test-fakes` 標記（已實測二進位檔 `strings` 確認乾淨）；`scripts/rust/mkbundle.sh` 亦對齊以 `--features test-fakes` 在 `--dev` 建置時啟用。
- [`5d62254b`](file://tests/test_release_workflow.py) `test(release): 對齊 Cargo target 與 binary 檢查路徑`：`_cargo_target_dir` 正確支援絕對與相對 `CARGO_TARGET_DIR` 解析；`test_release_profile_binary_excludes_test_hooks` 以暫存目錄隔離 release 建置輸出，實測通過且標有合規之平台與資源 marker。
- 契約檢查 `python3 -m tools.contracts check`（`ok: true, findings: []`）與政策檢查 `python3 -m policy_check --repo .`（25 pass, 0 fail, 2 warn）均全數通過，未引入新缺陷。

---

## 既有碼待辦（不計入結論）

1. [crates/sw-cli/tests/cli_spec_schema.rs:40](file://crates/sw-cli/tests/cli_spec_schema.rs#L40) — 在合入 main 的 #241 提交 `0757f1da`（merge `4f27ea12`）中，`contracts/cli_schema.json` 的 `--version` 全域選項宣告為 `"action": "_VersionAction"`，但 `cli_spec_schema.rs` 的 `action_kind` 僅匹配 `"store"`、`"store_true"`、`"count"`、`"append"`、`"version"`，未處理 `_VersionAction`，導致 `cargo test -p sw-cli --test cli_spec_schema` 出現 panic（可將 `_VersionAction` 對應至 `ActionKind::Version`）。
2. [tests/test_rust_mkbundle.py:636](file://tests/test_rust_mkbundle.py#L636), [tests/test_rust_mkbundle.py:880](file://tests/test_rust_mkbundle.py#L880) — 在 PR #241 對齊版本與程式碼指紋（`a494670c`、`210c1af1`）後，Rust CLI 的 `--version` 對外輸出為 `serialwrap <VERSION> (code <FINGERPRINT>)`，但上述兩項舊測試的斷言仍要求純版本字串 `serialwrap {version}` 以及在 setup verify 階段直接比對未帶指紋的版本，待後續 S1M-241 任務更新測試斷言。

---

## 對裁決的異議（不計入結論）

無

---

結論：可放行
