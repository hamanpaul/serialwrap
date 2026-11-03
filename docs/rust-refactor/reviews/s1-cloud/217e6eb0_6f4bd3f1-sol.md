> 審查者：pi＋gemini-4-argon（11 月 argon 已花 $35.26）（代理 sol 單審位，擁有者 2026-11-02 指定）

## A 前輪 finding 核對

| 編號 | 原嚴重度 | 狀態 | 累計未修輪數 | 說明 |
|---|---|---|---|---|
| 1 | Medium | 已修 978aa1e1 | 0 | `tests/contract_blackbox/test_cli_mgmt_daemon.py` 已改用 `_checkpointed_dut_tx` 經 bridge `barrier` + `checkpoint` 封住完整 TX wire 前綴，並於 `daemon stop` 後比對封閉視窗；新增 `test_checkpointed_dut_tx_includes_frames_written_before_barrier_settles` 驗證延遲落盤的第二筆重複 TX 會被納入。 |
| 2 | Medium | 已修 51bd8fa5 | 0 | `crates/sw-pal/tests/exec_delegate.rs` 改由 `exec_or_wait` 實際啟動的孫行程寫出 `SW_PAL_CTRLC_DELEGATE_READY` 與孫行程 PID，父行程以 `process_is_running(delegate_pid)` 確認孫行程存活後才送 Ctrl 事件，並於事件後確認孫行程已終止。 |
| 3 | Medium | 已修 ddcbdde0、4e3ba13f | 0 | 新增 `scripts/rust/l15_exit_driver.py` 並接入 `scripts/rust/stage1_exit_driver.sh`，對完整 Windows L1.5 原始報告、metadata 與 `stage1a-gate.json` 逐筆對帳，僅在所有非 PASS 列皆精確符合核准 `BLOCKED-OWNER` 延後規則時才豁免 `win_l15_func_test` 的 exit 1。 |
| 4 | High | 已修 12b3a929 | 0 | `tools/docker/upgrade.Dockerfile` 移除系統 `python3-yaml`，`scripts/docker/run_upgrade_container.py` 移除 `--system-site-packages` 改走離線 wheelhouse，`scripts/rust/mkbundle.sh` 建立隔離 venv 並封裝 `PyYAML` 入 bundle runtime（後續 `b84ee5bc` 亦依 OD-50 將 `install.sh` 改裝 Rust bundle）。 |
| 5 | Medium | 已修 12b3a929 | 0 | `scripts/rust/mkbundle.sh` 新增 `--release-bundle` 模式，不啟用 `sw-cli/test-fakes`；`docs/rust-refactor/release-0.4.0-plan.md` 與 `scripts/docker/run-upgrade.sh` 已改用 `--release-bundle --profile release`，並由 `test_release_bundle_build_does_not_enable_test_fakes` 鎖定。 |
| 6 | Low | 已修 12b3a929 | 0 | `docs/rust-refactor/release-0.4.0-plan.md` 前置條件 #1 已改為接受 `VERIFIED`，或 `IMPLEMENTED` 且只剩 OD-27～OD-32 明確允許的 `BLOCKED-OWNER`，並由 `test_release_plan_accepts_only_real_stage1_gate_outcomes` 鎖定。 |

## B 新發現

NO FINDINGS

## 既有碼待辦（不計入結論）

- `crates/sw-cli/src/doctor_cmd.rs:441`：前序 commit `6656d951` 在 `sw_proto::release_manifest::parse_manifest` 新增選用欄位 `components.daemon.code_fingerprint` 的 12 位小寫十六進位驗證，但 `doctor_cmd.rs::parse_only_rejection_key` 尚未同步加入 `components.daemon.code_fingerprint` 分支；若 manifest 的 `code_fingerprint` 格式不合法，`version_pairing` 會落入後備回報 `invalid release manifest: unreadable` 而非 `invalid release manifest: components.daemon.code_fingerprint`。
- `tests/contract_blackbox/test_cli_mgmt_doctor.py:184`：`test_doctor_ab_shared_checks` 仍寫死 18 項檢查（S1M 分支中間狀態），待 `S1M-fin` 收尾子項同步更新為 19 項並覆蓋 bundle `daemon_code_current`。

## 對裁決的異議（不計入結論）

無

結論：可放行