> 審查者：pi＋gemini-4-argon（11 月 argon 已花 $29.86）（代理 sol 單審位，擁有者 2026-11-02 指定）

## A 前輪 finding 核對

| 編號 | 原嚴重度 | 狀態 | 累計未修輪數 | 說明 |
|---|---|---|---|---|
| 1 | Medium | 已修 978aa1e1（及 897a3cdc） | 0 | `tests/contract_blackbox/test_cli_mgmt_daemon.py` 改由 `_checkpointed_dut_tx` 經 bridge `barrier` 與 `checkpoint`（核對 `wire_len` 與 `wire_sha256`）讀取封閉的 TX 區間，並於 daemon stop 後再做最終比對；含 `test_checkpointed_dut_tx_includes_frames_written_before_barrier_settles` 回歸。 |
| 2 | Medium | 已修 51bd8fa5 | 0 | `crates/sw-pal/tests/exec_delegate.rs` 改由 `exec_or_wait` 實際啟動的委派孫行程寫出含 PID 的 `DELEGATE_READY_ENV` 標記，父行程以 `process_is_running(delegate_pid)` 確認孫行程存活後才送 Ctrl 事件，並於事件後確認孫行程已終止；另有 `tests/test_rust_exec_delegate.py` 防線。 |
| 3 | Medium | 已修 ddcbdde0、4e3ba13f | 0 | 新增 `scripts/rust/l15_exit_driver.py` 並接通 `scripts/rust/stage1_exit_driver.sh`，同時核對 `rc.txt`、`stage1a-gate.json`、Windows `scenarios`／`basic` 原始報告與 `win-func-test-meta.json`，僅當所有非 PASS 列皆精確符合核准 `BLOCKED-OWNER` 延後規則時才豁免 `win_l15_func_test` exit 1；含 `tests/test_rust_stage1_exit_driver.py` 回歸。 |
| 4 | High | 已修 12b3a929 | 0 | `tools/docker/upgrade.Dockerfile` 移除 `python3-yaml`，`scripts/docker/run_upgrade_container.py` 移除 `--system-site-packages` 並改用隔離 wheelhouse（`--find-links=/opt/sw-old`），`scripts/rust/mkbundle.sh` 移除 `--system-site-packages` 並將 PyYAML 封裝進 bundle runtime（以 `-I -B` 驗證載入路徑）；含 `tests/test_docker_upgrade.py` 與 `tests/test_rust_mkbundle.py` 回歸。 |
| 5 | Medium | 已修 12b3a929 | 0 | `scripts/rust/mkbundle.sh` 新增 `--release-bundle` 模式（不啟用 `sw-cli/test-fakes`），`docs/rust-refactor/release-0.4.0-plan.md` 與 `scripts/docker/run-upgrade.sh` 改用 `--release-bundle --profile release`；`tests/test_rust_mkbundle.py::test_release_bundle_build_does_not_enable_test_fakes` 驗證不帶測試 feature。 |
| 6 | Low | 已修 12b3a929 | 0 | `docs/rust-refactor/release-0.4.0-plan.md` 前置條件改為要求 `VERIFIED`，或為 `IMPLEMENTED` 且只剩 OD-27～OD-32 明確允許的 `BLOCKED-OWNER`、無 `FAIL`／`INSUFFICIENT`；含 `tests/test_docker_upgrade.py::test_release_plan_accepts_only_real_stage1_gate_outcomes` 回歸。 |
| S1-e8aa5aa-1 | Medium | 已修 892af115 | 0 | `crates/sw-cli/tests/expect_reboot_parity.rs` 改以行程內 `sw_cli::run` 搭配 `ClientPlatform` fakes 驗證 `--mode interactive --expect-reboot` 的退出碼、stderr、stdout 位元組與零 RPC；實測 Linux 原生與 `--target x86_64-pc-windows-gnu` 經 `scripts/rust/win-runner.sh` 皆 `1 passed`。 |
| S1-e8aa5aa-2 | Medium | 已修 a433228f | 0 | `changelog.d/242-rust-cli-expect-reboot.md` 補齊首行 `---`，實測 `policy_check.changelog.load_fragments` 與 `render_section` 對全庫 255 個 fragment 全數通過。 |
| S1-e8aa5aa-3 | Low | 依裁決不處理 | 1 | `contracts/cli_schema.json` 的 `x-catalog-revision` 遞增屬 Low，依收斂規則不擋放行、交由編排者收進 low-backlog。 |

## B 新發現

1. **S1-a433228-1 [Low] [tests/test_changelog_fragments.py:13](tests/test_changelog_fragments.py:13) — 測試硬編碼單一 fragment 路徑且僅檢查 `---` 分隔線，未呼叫 `policy_check.changelog` 解析且於 release `collate` 清空 `changelog.d/` 後將找不到檔案** — **確切修法**：將 `tests/test_changelog_fragments.py` 改為呼叫 `policy_check.changelog.load_fragments(repo_root)`（並在回傳非空時呼叫 `render_section("0.0.0", "2026-01-01", [frag for _, frag in loaded])`），使目前所有 `changelog.d/*.md` 均受完整 frontmatter／`TYPE_TO_SECTION` 驗證，且未來 release PR 執行 `policy_check.changelog collate` 清空 `changelog.d/` 後仍能通過。

## 既有碼待辦（不計入結論）

無

## 對裁決的異議（不計入結論）

無

結論：可放行