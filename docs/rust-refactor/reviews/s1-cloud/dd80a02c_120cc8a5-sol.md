> 審查者：claude-fable-5-1（重型任務，擁有者 2026-11-07 指定）

## A 前輪 finding 核對

| 編號 | 原嚴重度 | 狀態 | 累計未修輪數 | 說明 |
|---|---|---|---|---|
| 1（SC-18 `_wait_dut_tx` 提早結束） | Medium | 已修 978aa1e1 | 0 | `_wait_dut_tx` 已移除，改為 `_checkpointed_dut_tx`：先呼叫 bridge `barrier`（passive、quiet 500 ms、上限 10 s，未 settled 即 FAIL），再以 `checkpoint` 的 `wire_len`／`wire_sha256` 封住完整 wire 前綴後才比對；最終比對移到 `daemon stop` 之後的封閉區間。新增 `test_checkpointed_dut_tx_includes_frames_written_before_barrier_settles`，重現「第二筆延遲落盤」並要求納入視窗（`tests/contract_blackbox/test_cli_mgmt_daemon.py:146`、`:178`、`:504-519`）。 |
| 2（Ctrl 事件測試 ready 標記不證明委派） | Medium | 已修 51bd8fa5＋b89e90f7 | 0 | ready 標記改由 `exec_or_wait` 實際啟動的孫行程寫出並附 `pid=`；父測試取 pid 後以 `process_is_running` 確認存活才送事件，事件後再斷言孫行程已結束（`crates/sw-pal/tests/exec_delegate.rs:311-352`）。新增可攜負向突變 `delegate_ready_rejects_child_stopped_before_exec`（`SW_PAL_EXEC_PARK_BEFORE_DELEGATE` 停在委派前），要求以同一判準逾時拒絕且不得出現 ready 標記（`:40-74`）。 |
| 3（BLOCKED-OWNER 未接通 driver 退出碼） | Medium | 已修 ddcbdde0＋4e3ba13f＋2ae832c8 | 0 | `win-func-test.sh` 保留原始退出碼；`stage1_exit_driver.sh` 結尾改由 `scripts/rust/l15_exit_driver.py` 做第二層判定，逐列核對 Windows L1.5 報告中所有非 PASS 列是否精確符合核准延後規則（`_gate_deferred_case_ids`／`_windows_l15_evidence_allows_defer`），摘要無效或產生器非 0 一律判失敗（2ae832c8）。`tests/test_rust_stage1_exit_driver.py` 有 driver 層回歸。 |
| 4（升級環境額外提供系統 PyYAML） | High | 已修 12b3a929 | 0 | `upgrade.Dockerfile` 已不裝 `python3-yaml`；`run_upgrade_container.py` 與 `run-upgrade.sh` 無 `--system-site-packages`；舊版 pipx 夾具改由 wheelhouse（`pip download --only-binary=:all: PyYAML>=6`）離線供應；新版 bundle 以 `--release-bundle` 將 PyYAML 納入隔離 runtime，並強制 `pyvenv.cfg` 為 `home = /usr/bin`＋`include-system-site-packages = false`（`run-upgrade.sh:162-168`），不符 rc=3。 |
| 5（正式發佈配方編入 test-fakes） | Medium | 已修 12b3a929 | 0 | `mkbundle.sh` 新增 `--release-bundle`，`--features test-fakes` 僅在 `DEV=1` 加入（`scripts/rust/mkbundle.sh:178-180`）；release plan、`run-upgrade.sh`、`release.yml` 均改用 `--release-bundle --profile release`，smoke 另掃 `versions/<V>/bin` 不含 `SW_TEST_UPDATE_*` 字串。 |
| 6（前置條件要求不存在的 `overall: PASS`） | Low | 已修 12b3a929 | 0 | `release-0.4.0-plan.md:10` 已改為「`VERIFIED`，或 `IMPLEMENTED` 且只剩 OD-27～OD-32 允許的 `BLOCKED-OWNER`」並併入 OD-75 基線缺陷條件。 |

## B 新發現

本輪四個 commit（d99b6ad2、eca428c7、1334c1f6、120cc8a5）皆為版號升至 0.4.1 的連帶變更，無 merge commit。核對結果：

- d99b6ad2：`VERSION`、`Cargo.toml` workspace 版號、`Cargo.lock` 六個 workspace crate 同步；`cargo metadata --locked` 通過。12 個 fragment 與 `CHANGELOG.md` 0.4.1 段逐條對照，內容對應無遺漏（`docs` type 落 Changed 為 `policy_check.changelog` 的固定對應）；所引用的 `f8-legacy-attached-upgrade` 回歸 case 確實存在於 `regression/serialwrap_regression/cases/f08_legacy_upgrade.py`。`changelog.d/` 已清空。
- eca428c7：只改 `action_version`／`action_version_session` 版號與 `_meta.json` revision；Rust 端 `sw_proto::version::version()` 以 `include_str!` 讀 `VERSION`，`cargo test -p sw-cli --test argparse_golden` 3 passed。`contracts/cli_help/` 其餘目錄（windows、rust_only）無殘留 `0.4.0`。
- 1334c1f6：README 英文／繁中兩段同步，0.3.x 判定門檻「低於 0.4.0」未被誤改。
- 120cc8a5：`NEW_VERSION` 改 0.4.1，標籤與失敗原因改由 `NEW_VERSION` 產生；測試改由 `VERSION` 推得候選版並新增 driver `NEW_VERSION == VERSION` 檢查；純函式測試的 0.4.0 固定值與假 `/proc` 夾具路徑未動（屬任意字串，不影響判定）。定向 `tests/test_docker_upgrade.py`＋`tests/test_release_workflow.py`（排除 `supervisor_runtime` 實跑）174 passed。

S1-120cc8a-1 [Low] contracts/cli_help/_meta.json:3 — `git_revision` 記為 dd80a02c，但該 commit 的 `VERSION` 仍為 0.4.0，Python CLI 於該 revision 會印 `serialwrap 0.4.0`，與 golden 內容 0.4.1 不一致；證據來源指向的是升版前的 tree（dirty 工作樹擷取）。0.4.0 升版時的 29772118 亦為相同做法，屬既有慣例，不擋放行。— 修法：golden 重產放在版號 commit 之後單獨執行，或在 `_meta.json` 另記擷取時的 `VERSION`。

S1-120cc8a-2 [Low] docs/rust-refactor/release-0.4.0-plan.md:1 — 本分支為 release 0.4.1，但前置條件、§4 Linux 升級驗證與 §5 使用者程序全以 0.4.0 撰寫，且 `evidence/stage1/upgrade-docker/` 尚無 `NEW_VERSION=0.4.1` 的三模式與 `attached-legacy` 實跑報告；120cc8a5 commit 訊息稱「作為 0.3.2 → 0.4.1 發布前證據」，但證據尚未產生。— 修法：補 0.4.1 release 段（或獨立 0.4.1 plan），列明本次須重跑的 Docker 情境與結果保存路徑，由編排者在 host 以 0.4.1 release 候選 bundle 實跑後登記。

## 既有碼待辦（不計入結論）

- `scripts/docker/run-upgrade.sh` 自建候選 bundle 仍帶 `--version-override "$NEW_VERSION"`，與 `release-0.4.0-plan.md:40`「正式 bundle 不得用 `--version-override`」的敘述不一致；`NEW_VERSION` 等於 `VERSION` 時為 no-op，但一旦兩者脫節（`test_upgrade_driver_new_version_matches_version_file` 只守 driver，不守 `run-upgrade.sh`），證據會以覆寫後版號產生。非本輪改動。

## 對裁決的異議（不計入結論）

無。

驗證摘要：`python3 -m policy_check --repo .` 24 pass、1 fail（R-07 VERSION↔最新 tag，release PR 於 tag 建立前屬預期，由 `release:0.4.1` label 豁免）、2 warn（R-19、R-22，非 PR 上下文）；`python3 -m tools.contracts check` ok；未碰 production daemon、`~/.config/serialwrap`、`~/.local/state/serialwrap` 與 `/dev/ttyUSB*`；工作樹維持乾淨，Cargo 產物在 `/tmp/solrev-s1-release-0.4.1-target`。未執行完整 pytest、Docker 實跑與 Windows 原生重跑。

結論：可放行
