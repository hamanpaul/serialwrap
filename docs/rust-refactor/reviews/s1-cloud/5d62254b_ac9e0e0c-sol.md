> 審查者：agy gemini-3.8-flash-high（代理 sol 單審位，擁有者 2026-11-02 指定）

## A 前輪 finding 核對

| 編號 | 原嚴重度 | 狀態 | 累計未修輪數 | 說明 |
|---|---|---|---|---|
| 1 | Medium | 已修 `978aa1e1` | 0 | `test_cli_mgmt_daemon.py` 改用 `_checkpointed_dut_tx` 經 bridge barrier 與 checkpoint 驗證完整 TX wire 視窗，並新增延遲落盤回歸測試 `test_checkpointed_dut_tx_includes_frames_written_before_barrier_settles`。 |
| 2 | Medium | 已修 `51bd8fa5` | 0 | `crates/sw-pal/tests/exec_delegate.rs` 改由受委派孫行程輸出 `SW_PAL_CTRLC_DELEGATE_READY` 標記與 PID，父行程確認 PID 存活後才發送控制事件，並新增 `tests/test_rust_exec_delegate.py` 回歸防衛。 |
| 3 | Medium | 已修 `ddcbdde0` | 0 | `scripts/rust/stage1_exit_driver.sh` 引入 `l15_exit_driver.py` 依 gate 逐列核對核准的擁有者延後項（OD-30～OD-32），並於 `4e3ba13f` 補齊 `test_rust_stage1_exit_driver.py` 整合回歸。 |
| 4 | High | 已修 `12b3a929` | 0 | `upgrade.Dockerfile` 移除系統 `python3-yaml`，使用隔離 wheelhouse；bundle runtime 自帶 PyYAML 且強制 `include-system-site-packages = false`（後續由 `407ed7e3` 擴充 build venv 檢查）。 |
| 5 | Medium | 已修 `12b3a929` | 0 | `mkbundle.sh` 新增 `--release-bundle` 正式模式排除 `sw-cli/test-fakes`，更新 `release-0.4.0-plan.md` 配方，並由 `test_release_bundle_build_does_not_enable_test_fakes` 守護。 |
| 6 | Low | 已修 `12b3a929` | 0 | `release-0.4.0-plan.md` 條件 1 改寫為接受 `VERIFIED` 或僅含 OD-27～OD-32 `BLOCKED-OWNER` 的 `IMPLEMENTED`。 |

---

## B 新發現

### S1-ac9e0e0-1
- **[Medium] [contracts/cli_help/rust_only/setup.90.txt:1](file://contracts/cli_help/rust_only/setup.90.txt#L1)** — setup 的 Rust-only help golden 遺漏 7 個欄寬，導致黑箱測試 7 案紅燈失敗且與 Python 基線文字不一致。
  - **問題與具體失敗情境：**
    `ac9e0e0c` 在 [`crates/sw-cli/src/cli_spec.rs`](file://crates/sw-cli/src/cli_spec.rs#L1825-L1845) 為 Rust CLI 的 `setup` 補齊了 `--no-restart` 旗標與 description 說明，但更新 golden 檔案時僅修改了 `contracts/cli_help/rust_only/setup.80.txt` 與 `setup.120.txt`，遺漏了其餘 7 個欄寬（20, 21, 25, 30, 40, 60, 90 欄寬）的 golden。
    此外，[`crates/sw-cli/src/cli_spec.rs`](file://crates/sw-cli/src/cli_spec.rs#L1844) 與 80/120 golden 所帶文字（例如 `--no-restart` 說明為 `daemon 載入的程式碼與剛安裝的不同時，不自動 restart 服務（預設：systemd 模式且服務 active 時自動 restart，讓 daemon 換新碼）`、`--with-sudo` 為 `systemd-system 模式下允許執行特權 sudo 指令（安裝 unit／enable／start／restart）`）與當前分支的 Python 基線 [`sw_core/cli.py:2660`](file://sw_core/cli.py#L2660)（`daemon 指紋過期時不自動 restart systemd service` 與 `安裝 unit／enable／start`）字串不對齊，違反外部可觀察行為與 Python 1:1 的要求。
    **具體重現情境：**
    輸入：
    ```bash
    SERIALWRAP_AB_LABEL=rust SERIALWRAP_CLI_EXE=/tmp/solrev-s1-target/debug/serialwrap python3 -m pytest -q tests/contract_blackbox/test_bb_cli_help.py -k "help_grp_setup_col"
    ```
    預期輸出：9 passed（20～120 共 9 種欄寬的 setup help 比對全數通過）。
    實際輸出：7 failed, 2 passed。其中 20, 21, 25, 30, 40, 60, 90 欄寬因讀取缺少 `--no-restart` 與新版說明的舊版 golden，全部拋出 `AssertionError` 失敗：
    ```
    FAILED tests/contract_blackbox/test_bb_cli_help.py::test_help_usage_error_bytes[help_grp_setup_col20]
    FAILED tests/contract_blackbox/test_bb_cli_help.py::test_help_usage_error_bytes[help_grp_setup_col21]
    FAILED tests/contract_blackbox/test_bb_cli_help.py::test_help_usage_error_bytes[help_grp_setup_col25]
    FAILED tests/contract_blackbox/test_bb_cli_help.py::test_help_usage_error_bytes[help_grp_setup_col30]
    FAILED tests/contract_blackbox/test_bb_cli_help.py::test_help_usage_error_bytes[help_grp_setup_col40]
    FAILED tests/contract_blackbox/test_bb_cli_help.py::test_help_usage_error_bytes[help_grp_setup_col60]
    FAILED tests/contract_blackbox/test_bb_cli_help.py::test_help_usage_error_bytes[help_grp_setup_col90]
    ```
  - **確切修法：**
    1. 同步 [`sw_core/cli.py`](file://sw_core/cli.py) 的 setup description、`--with-sudo` 與 `--no-restart` 說明文字，與 `origin/main`（以及 `cli_spec.rs`）保持一致。
    2. 執行 `python3 scripts/rust/capture_cli_help.py --rust-only`，將 `contracts/cli_help/rust_only/setup.<COLUMNS>.txt` 全套 9 種寬度（20, 21, 25, 30, 40, 60, 80, 90, 120）全數重新擷取更新。
    3. 實跑上述黑箱測試確認 9 案全綠。

---

## 既有碼待辦（不計入結論）

- [crates/sw-cli/tests/cli_spec_schema.rs:40](file://crates/sw-cli/tests/cli_spec_schema.rs#L40)：`action_kind()` 不認得 `_VersionAction`。這是 `4f27ea12` 合入 main 時將 `_VersionAction` 帶入 `contracts/cli_schema.json` 所致，執行 `CARGO_TARGET_DIR=/tmp/solrev-s1-target cargo test -p sw-cli --test cli_spec_schema` 會 panic。待後續 S1M 分支對齊版本動作時收斂。

---

## 對裁決的異議（不計入結論）

無

---

結論：需修正
