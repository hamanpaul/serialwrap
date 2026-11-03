> 審查者：pi＋gemini-4-argon（11 月 argon 已花 $25.98）（代理 sol 單審位，擁有者 2026-11-02 指定）

## A 前輪 finding 核對

| 編號 | 原嚴重度 | 狀態 | 累計未修輪數 | 說明 |
|---|---|---|---|---|
| 1 | Medium | 已修 978aa1e1（及 897a3cdc） | 0 | `tests/contract_blackbox/test_cli_mgmt_daemon.py` 改由 `_checkpointed_dut_tx` 經 bridge `barrier` 與 `checkpoint`（核對 `wire_len` 與 `wire_sha256`）讀取封閉的 TX 區間，並於 daemon stop 後再做最終比對；已補 `test_checkpointed_dut_tx_includes_frames_written_before_barrier_settles` 回歸。 |
| 2 | Medium | 已修 51bd8fa5 | 0 | `crates/sw-pal/tests/exec_delegate.rs` 改由 `exec_or_wait` 實際啟動的委派孫行程寫出含 PID 的 `DELEGATE_READY_ENV` 標記，父行程以 `process_is_running(delegate_pid)` 確認孫行程存活後才送 Ctrl 事件，並於事件後確認孫行程已終止；另有 `tests/test_rust_exec_delegate.py` 防線。 |
| 3 | Medium | 已修 ddcbdde0、4e3ba13f | 0 | 新增 `scripts/rust/l15_exit_driver.py` 並接通 `scripts/rust/stage1_exit_driver.sh`，同時核對 `rc.txt`、`stage1a-gate.json`、Windows `scenarios`／`basic` 原始報告與 `win-func-test-meta.json`，僅當所有非 PASS 列皆精確符合核准 `BLOCKED-OWNER` 延後規則時才豁免 `win_l15_func_test` exit 1；含 `tests/test_rust_stage1_exit_driver.py` 回歸。 |
| 4 | High | 已修 12b3a929 | 0 | `tools/docker/upgrade.Dockerfile` 移除 `python3-yaml`，`scripts/docker/run_upgrade_container.py` 移除 `--system-site-packages` 並改用隔離 wheelhouse（`--find-links=/opt/sw-old`），`scripts/rust/mkbundle.sh` 移除 `--system-site-packages` 並將 PyYAML 封裝進 bundle runtime（以 `-I -B` 驗證載入路徑）；含 `tests/test_docker_upgrade.py` 與 `tests/test_rust_mkbundle.py` 回歸。 |
| 5 | Medium | 已修 12b3a929 | 0 | `scripts/rust/mkbundle.sh` 新增 `--release-bundle` 模式（不啟用 `sw-cli/test-fakes`），`docs/rust-refactor/release-0.4.0-plan.md` 與 `scripts/docker/run-upgrade.sh` 改用 `--release-bundle --profile release`；`tests/test_rust_mkbundle.py::test_release_bundle_build_does_not_enable_test_fakes` 驗證不帶測試 feature。 |
| 6 | Low | 已修 12b3a929 | 0 | `docs/rust-refactor/release-0.4.0-plan.md` 前置條件改為要求 `VERIFIED`，或為 `IMPLEMENTED` 且只剩 OD-27～OD-32 明確允許的 `BLOCKED-OWNER`、無 `FAIL`／`INSUFFICIENT`；含 `tests/test_docker_upgrade.py::test_release_plan_accepts_only_real_stage1_gate_outcomes` 回歸。 |

## B 新發現

1. **S1-e8aa5aa-1 [Medium] [crates/sw-cli/tests/expect_reboot_parity.rs:23](crates/sw-cli/tests/expect_reboot_parity.rs:23) — 測試在 Cargo test 內直接執行 `env!("CARGO_BIN_EXE_serialwrap")` 與 `python3 -m sw_core.cli`，在 Windows 原生 Cargo runner 下必敗**
   - **問題（含具體失敗情境）**：`899a60bf` 新增的 `interactive_rejection_matches_python_cli_byte_for_byte` 以 `Command::new(env!("CARGO_BIN_EXE_serialwrap"))`（第 23 行）與 `Command::new("python3").args(["-m", "sw_core.cli"])`（第 29 行）啟動外部行程。在 WSL 交叉編譯 `--target x86_64-pc-windows-gnu` 並經 `scripts/rust/win-runner.sh`（即 `scripts/rust/win-test.sh` 與 Stage 1 出口 `win_cargo_1a`／`win_cargo_1b`）於 Windows `%LOCALAPPDATA%\Temp\sw-rust-test\<run_id>` 執行時：
     1. `env!("CARGO_BIN_EXE_serialwrap")` 在編譯期固定為 WSL Linux 宿主路徑（如 `/.../x86_64-pc-windows-gnu/debug/serialwrap.exe`），且 `win-runner.sh` 僅將受測 test binary 複製至 staging、未複製 `serialwrap.exe`，Windows `CreateProcessW` 無法解析該 Linux 路徑；
     2. `win-runner.sh` 的 `$staging/data`（`common::data_root()`）僅複製 `sw_core/constants.py` 與 `sw_core/assets/`，不含 `sw_core/cli.py`，且 Windows 下無 `python3` 可執行檔。
     實跑重現：執行 `CARGO_TARGET_DIR=/tmp/solrev-s1-target cargo test --no-run -p sw-cli --test expect_reboot_parity --target x86_64-pc-windows-gnu` 後，執行 `scripts/rust/win-runner.sh /tmp/solrev-s1-target/x86_64-pc-windows-gnu/debug/deps/expect_reboot_parity-*.exe`，預期 `1 passed`（exit 0），實際於 `crates/sw-cli/tests/expect_reboot_parity.rs:28:10` panic：`Rust CLI 執行失敗: Os { code: 3, kind: NotFound, message: "系統找不到指定的路徑。" }`（exit 101）。
   - **確切修法**：比照 `crates/sw-cli/tests/profile_checks.rs` 等 `sw-cli` 整合測試，在 `crates/sw-cli/tests/expect_reboot_parity.rs` 改用行程內入口（如 `sw_cli::run_with` 搭配緩衝 `Io`）執行 Rust CLI，斷言回傳碼 `1`、stderr 空、stdout 逐位元組等於 Python 基線輸出的 `{"error_code":"EXPECT_REBOOT_MODE_UNSUPPORTED","hint":"--expect-reboot 只適用 line／background 模式；interactive 是 lease 按鍵通道，不走 reboot 路徑。","ok":false}\n`，移除對 `env!("CARGO_BIN_EXE_serialwrap")` 與 `Command::new("python3")` 的呼叫（跨行程 Python／Rust CLI 對照如需保留，應置於 `tests/contract_blackbox/test_bb_exit_codes.py`）；並以 `scripts/rust/win-runner.sh` 驗證 Windows target 測試通過。

2. **S1-e8aa5aa-2 [Medium] [changelog.d/242-rust-cli-expect-reboot.md:1](changelog.d/242-rust-cli-expect-reboot.md:1) — changelog fragment 缺少開頭 `---` 分隔線，導致 `policy_check.changelog collate` 拋出 `FragmentError` 中止**
   - **問題（含具體失敗情境）**：`899a60bf` 新增的 `changelog.d/242-rust-cli-expect-reboot.md` 首行直接為 `type: change`，缺少 YAML frontmatter 起始行 `---`。依 `CLAUDE.md` 與 `docs/rust-refactor/release-0.4.0-plan.md` §3 第 2 步，在暫存副本執行 `python3 -m policy_check.changelog collate --version 0.4.0 --date 2026-11-03`（或呼叫 `policy_check.changelog.parse_fragment(Path("changelog.d/242-rust-cli-expect-reboot.md").read_text(encoding="utf-8"))`），預期成功解析為 `Fragment(type='change', scope='cli', ...)` 並完成收斂，實際拋出 `policy_check.changelog.FragmentError: fragment must start with a YAML frontmatter block` 中止。
   - **確切修法**：在 `changelog.d/242-rust-cli-expect-reboot.md` 第 1 行補上 `---`，並以 `policy_check.changelog.parse_fragment` 確認 `changelog.d/*.md` 全數可解析。

3. **S1-e8aa5aa-3 [Low] [contracts/cli_schema.json:5](contracts/cli_schema.json:5) — 修改 `contracts/cli_schema.json` 未同步遞增 `x-catalog-revision`**
   - **問題**：依編排者裁決（S1X-a 第 4 輪 SPEC-GAP 3，plan 02 P0 規則），schema 檔任何修改皆遞增 `x-catalog-revision`。`e8aa5aa6` 更新了 `contracts/cli_schema.json:1193` 的 `x-option-help["--expect-reboot"]`，但 `x-catalog-revision` 仍維持 `4f27ea12` 時的 `7`。無程式消費此欄位，無執行期失敗情境。
   - **確切修法**：將 `contracts/cli_schema.json` 的 `"x-catalog-revision"` 遞增為 `8`。

## 既有碼待辦（不計入結論）

無

## 對裁決的異議（不計入結論）

無

結論：需修正