> 審查者：pi＋gemini-4-argon（11 月 argon 已花 $36.35）（代理 sol 單審位，擁有者 2026-11-02 指定）

## A 前輪 finding 核對

| 編號 | 原嚴重度 | 狀態 | 累計未修輪數 | 說明 |
|---|---|---|---|---|
| 1 | Medium | 已修 `978aa1e1` | 0 | `tests/contract_blackbox/test_cli_mgmt_daemon.py` 改以 bridge checkpoint／drain barrier 封閉 TX 區間後比對 prefix 長度與 sha256，並補延遲第二筆 TX 負向回歸。 |
| 2 | Medium | 已修 `51bd8fa5` | 0 | `crates/sw-pal/tests/exec_delegate.rs` 改由受委派孫行程寫入含 PID 的 delegate-ready 標記，父行程確認該 PID 存活後才送出 Ctrl 事件，並補未進入孫行程時的逾時負向案例。 |
| 3 | Medium | 已修 `ddcbdde0`、`4e3ba13f` | 0 | `scripts/rust/l15_exit_driver.py` 與 `scripts/rust/stage1_exit_driver.sh` 已接通第二層逐列證據核對，僅在所有非 PASS 列均符合核准 `BLOCKED-OWNER` 規則且 Linux 同案 PASS 時放行，並有 `tests/test_rust_stage1_exit_driver.py` 回歸。 |
| 4 | High | 已修 `12b3a929`、`2ddf90e1` | 0 | `tools/docker/upgrade.Dockerfile` 與 `scripts/docker/run_upgrade_container.py` 已移除系統 `python3-yaml` 與 `--system-site-packages`，`scripts/rust/mkbundle.sh --release-bundle` 將 PyYAML 封入隔離 runtime 並以 `-I` 驗證。 |
| 5 | Medium | 已修 `12b3a929`、`ac6c4e4e` | 0 | `mkbundle.sh --release-bundle` 不啟用 `sw-cli/test-fakes`，`crates/sw-cli/src/lib.rs` 與 `rollback.rs` 的測試鉤子（`SERIALWRAP_TEST_DELEGATE_GROUP`、`SW_TEST_UPDATE_PAUSE_AFTER`、`SW_TEST_UPDATE_FORCE_VERIFY_FAIL`）均受 `test-fakes` 門禁，`.github/workflows/release.yml` 與 `docs/rust-refactor/release-0.4.0-plan.md` 已同步檢查。 |
| 6 | Low | 已修 `12b3a929` | 0 | `docs/rust-refactor/release-0.4.0-plan.md:10` 已改寫為接受 `VERIFIED`，或 `IMPLEMENTED` 且僅剩 OD-27～OD-32 允許的 `BLOCKED-OWNER`。 |

## B 新發現

1. **[High] [crates/sw-cli/src/lib.rs:134](crates/sw-cli/src/lib.rs#L134) — `SERIALWRAP_TEST_DELEGATE_GROUP` 加上 `#[cfg(feature = "test-fakes")]` 後，未在 `sw-cli` 的 `[dev-dependencies]` 啟用 `test-fakes`，導致不帶 `--features test-fakes` 的標準 `cargo test --workspace` 與 L1 鎖定集合中兩支既有整合測試失敗。**

   具體失敗情境：`crates/sw-cli/Cargo.toml` 的 `[dev-dependencies]` 僅啟用 `sw-pal` 的 `test-fakes`，未啟用 `sw-cli` 自身的 `test-fakes`。當執行標準驗證命令 `cargo test --workspace`（對應 `scripts/rust/stage1_exit_driver.sh:411` 的 `cargo_test_workspace`、`.github/workflows/rust.yml:88` 的 `rust-windows-hosted`、`docs/rust-refactor/release-0.4.0-plan.md:11`，以及 `tests/acceptance/l1_required.lock.json` 中 `s1b_cargo` linux/windows 與 `s1a_cargo` windows 的無 `--all-features` selector）時，`sw_cli` library 以未啟用 `test-fakes` 編譯，忽略 `FakeEnv` 中的 `SERIALWRAP_TEST_DELEGATE_GROUP="doctor"`，改走原生 `doctor` 分支：
   - 執行 `CARGO_TARGET_DIR=/tmp/solrev-s1-target cargo test -p sw-cli --test delegate_resolve`：鎖定案例 `cargo:sw-cli::delegate_resolve::run_delegate_increments_depth_and_executes_once_with_three_environment_values` 於 `crates/sw-pal/src/fakes/platform.rs:158` panic `fake component not configured: procs`（預期透過委派執行 1 次並回 `rc = 1`，實際 panic FAIL）。
   - 執行 `CARGO_TARGET_DIR=/tmp/solrev-s1-target cargo test -p sw-cli --test mgmt_terminal`：鎖定案例 `cargo:sw-cli::mgmt_terminal::delegate_mechanism_still_guards_recursion` 走原生 `doctor` 回 `rc = 0`，於 `crates/sw-cli/tests/mgmt_terminal.rs:119` 斷言 `assert_eq!(rc, 1)` 失敗（預期攔截遞迴回 `rc = 1` 且 stderr 含 `delegation recursion detected`，實際 `left: 0, right: 1` FAIL）。

   **確切修法：**在 `crates/sw-cli/Cargo.toml` 的 `[dev-dependencies]` 加入 `sw-cli = { path = ".", features = ["test-fakes"] }`（使 `cargo test` 編譯整合測試時為 `sw_cli` library 啟用 `test-fakes`，而 `cargo build --release -p sw-cli` 與 `mkbundle.sh --release-bundle` 因不編譯 dev-dependencies 仍不含測試鉤子；同時確認不改變 `regen_lock.py --check` 的鎖定 nodeid 集合）。驗證不帶 `--features test-fakes` 的 `cargo test -p sw-cli --test delegate_resolve --test mgmt_terminal` 與 `test_release_profile_binary_excludes_test_hooks` 同時通過。

2. **[Medium] [tests/test_release_workflow.py:236](tests/test_release_workflow.py#L236) — `test_release_profile_binary_excludes_test_hooks` 硬編碼讀取 `REPO_ROOT / "target"`，忽略 `CARGO_TARGET_DIR`，導致外部指定 target 目錄時測試失敗或誤檢過期 binary。**

   具體失敗情境：`test_release_profile_binary_excludes_test_hooks` 執行 `cargo build --release --target x86_64-unknown-linux-gnu -p sw-cli` 時繼承外層 `os.environ`（若設有 `CARGO_TARGET_DIR`，產物輸出至 `$CARGO_TARGET_DIR/x86_64-unknown-linux-gnu/release/serialwrap`），但第 236 行卻固定檢查 `REPO_ROOT / "target" / "x86_64-unknown-linux-gnu" / "release" / "serialwrap"`：
   - 在無 `REPO_ROOT/target` 的 worktree 以 `CARGO_TARGET_DIR=/tmp/solrev-s1-target python3 -m pytest -q tests/test_release_workflow.py -k test_release_profile_binary_excludes_test_hooks` 實測，`cargo build` 成功（rc=0），但第 237 行立即因 `AssertionError: release binary 不存在：.../target/x86_64-unknown-linux-gnu/release/serialwrap` 失敗。
   - 若 `REPO_ROOT/target` 殘留舊版 binary 且外層設定 `CARGO_TARGET_DIR` 指向他處，`cargo build` 更新的是 `$CARGO_TARGET_DIR` 下的執行檔，而 `strings` 檢查的卻是 `REPO_ROOT/target` 下未更新的舊檔，使回歸檢查對新編入的測試鉤子漏判 PASS。

   **確切修法：**比照 `scripts/rust/mkbundle.sh` 以 `Path(os.environ.get("CARGO_TARGET_DIR") or (REPO_ROOT / "target"))`（相對路徑相對於 `REPO_ROOT` 解析）解析 target 根目錄，或在 `cargo build` 明確傳入同一 `CARGO_TARGET_DIR`／`--target-dir`，確保建置輸出路徑與 `strings` 讀取的 `binary` 路徑一致；並補一個設定 `CARGO_TARGET_DIR` 指向暫存目錄或驗證路徑解析一致性的測試。

## 既有碼待辦（不計入結論）

- 與 `210c1af1..ac6c4e4e` 無關的分支既有待辦（如 `cli_spec_schema::cli_spec_matches_schema` 對 `_VersionAction` 的既有紅燈、`doctor_cmd.rs` manifest `code_fingerprint` 欄位辨識、`test_doctor_ab_shared_checks` 檢查數與 `test_rust_mkbundle.py` 指紋版本字串對齊）已列於 S1M 後續子項（`S1M-fin`），不在本輪 `S1M-hook` 範圍內。

## 對裁決的異議（不計入結論）

無

結論：需修正