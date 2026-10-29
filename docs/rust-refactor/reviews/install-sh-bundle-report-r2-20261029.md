# install.sh bundle 化第 2 輪雲端回報（合入 OD-48＋release 前置、解 Docker 腳本衝突，2026-10-29）

- 依據：`rulings/s1.md` 的 OD-50 列、「release 前置第 2 輪 OWNER-BATCH #2」原列與「承接改派：改由 install.sh 第 2 輪處理」列；`review-convergence-rules.md`；`s1-cloud/62ffec7_b84ee5b-sol.md`（起點時最新）與本輪期間新出的 `s1-cloud/b84ee5b_407ed7e-sol.md`。
- 分支：`feature/install-sh-bundle`，`b84ee5b..ed46551` 以 fast-forward 推送（未被拒，未用 `claude/install-sh-bundle-r2`）。未 merge 到 main、未開 PR、未建 tag、未改 `VERSION`；未接觸 production（本 VM 無）。
- 環境：雲端 VM（Ubuntu 24.04、4 cores、15 GB），Rust 1.96.1（`rust-toolchain.toml` 釘定，含 rustfmt、`x86_64-pc-windows-gnu` 與 mingw-w64）；`/usr/bin/python3.12`（3.12.3）venv＋PyYAML 6.0.3＋pytest 9.2.0＋jsonschema 4.26.0＋setuptools／wheel＋policy engine `2dcf2f8`；clone 已 unshallow。所有測試與 Docker 實跑皆以一般使用者 `dev`（uid 30033，docker 群組）前景等待結束；dockerd 29.9.2 於 VM 內啟動。

## Commit 清單（first-parent）

| commit | 內容 |
|---|---|
| `a9b8a38` merge: 併入 origin/feature/od48-legacy-handoff（2179a83）並解 Docker 升級腳本衝突 | 三檔衝突解法見下；新增 `test_install_sh_scenario_rejects_systemd_system_mode`；`REQUIRED_CHECKS` 斷言由比長度改為逐項比 tuple |
| `e1c5365` merge: 併入 origin/feature/release-0.4.0-prep（2ddf90e） | 唯一衝突：release 計畫 §2 表格 |
| `407ed7e` fix(docker): 升級驗證以 /usr/bin/python3.12 build venv 建 bundle 並檢查 pyvenv.cfg | OWNER-BATCH #2；7 個先紅回歸；§4.1 文件；`changelog.d/od-50-upgrade-docker-system-python.md` |
| `ed46551` test(docker): 保存 install.sh 第 2 輪三模式與 install-sh 升級 Docker 證據（407ed7e） | `evidence/stage1/upgrade-docker/install-sh-407ed7e/`；§4.1 補記 |

每個 commit 皆帶三行 trailer（Copilot、`Review-pending: 非實作者增量單審`、Claude）。

## 衝突解法

**`a9b8a38`（OD-48）**：兩邊的檢查全部保留，沒有刪除任何檢查 id，也沒有放寬判定。
- `scripts/docker/upgrade_checks.py`：同時保留 OD-48 的 `REQUIRED_CHECKS`（19 項，含 `legacy_handoff_direct_setup`）與 `REQUIRED_CHECKS_SYSTEMD_SYSTEM`（13 項），以及 OD-50 的 `REQUIRED_INSTALL_SH_CHECKS`（12 項）、`PIPX_UNINSTALL_HINT`、`judge_install_sh`。兩邊 `overall_verdict` 的簽章不同，改成 `required: Iterable[str] = REQUIRED_CHECKS`，位置或關鍵字傳入都可以，兩邊的呼叫端都相容。
- `scripts/docker/run_upgrade_container.py`：`Driver` 同時帶 install-sh 的 `scenario`／`installer`／`bundle_tarball`，以及 OD-48 的模式旗標對照（含 `--system`）與 `setup_args`。報告的 `required` 與 `scope` 依情境選定：install-sh 用 `REQUIRED_INSTALL_SH_CHECKS`（scope `install_sh`），systemd-system 用 `REQUIRED_CHECKS_SYSTEMD_SYSTEM`，其餘用 `REQUIRED_CHECKS`（scope `full`）。
- `scripts/docker/run-upgrade.sh`：用法字串同時列出 `systemd-system` 與 `--scenario`。`--mode all` 仍是 systemd-user＋on-demand，這是 OD-48 既有測試 `test_run_upgrade_dry_run_systemd_system_mode_is_explicit_only` 的約束。
- 合併後出現一個新組合 `--scenario install-sh --mode systemd-system`。install-sh 的 driver 沒有 system 模式的 phase（setup 需要 `--with-sudo`，監管檢查走 system unit），所以 runner 與 driver 兩端都以 rc=2 拒絕。新測試在任一端拿掉守門時都會紅。
- 語意衝突：install-sh 側的 `test_install_sh_scenario_required_checks_and_verdict` 原本以 `len(REQUIRED_CHECKS) == 18` 斷言，合入 OD-48 後變成 19。改為逐項釘住 19 個 id 的 tuple 相等，比只比長度更嚴；另加驗 install-sh 的集合不足以讓 systemd-system 的必要集合 PASS。
- 其餘檔案（Rust、README、契約、release 計畫）皆自動合併。

**`e1c5365`（release 前置）**：§2 表格的 Windows bundle 列採用 release 前置版（補上「workflow 不建 Windows bundle，由擁有者以 `gh release upload` 附加」）；wheel 與 install.sh 兩列保留 OD-50 版（release 前置分支仍寫「目前仍為 pipx」，已被 OD-50 取代）。合併後 `changelog.d` 共 225 個 fragment，在暫存複本試跑 `policy_check.changelog collate --version 0.4.0` 可以成功收斂（未提交）。

## 各裁決處理

- **release 前置第 2 輪 OWNER-BATCH #2（承接改派到本輪）**：照修（`407ed7e`），裁決內容沒有變。
  - 未給 `--bundle` 時，會先確認 `/usr/bin/python3.12` 可執行、realpath 位於 `/usr/bin`、版本等於 `contracts/cli_help/_meta.json` 的 3.12.3。任一不符就以 rc=3 結束，而且是在 git archive／pip／mkbundle／docker 之前結束，不回退其他 python。
  - 通過後以它建 build venv（`pyyaml==6.0.3`、`jsonschema==4.10.3`，與 workflow 同釘版），確認 `sys._base_executable` 位於 `/usr/bin`，再把 build venv 放在 PATH 首位執行 `mkbundle.sh --release-bundle --profile release --version-override 0.4.0`。
  - 自建或 `--bundle` 提供的 bundle 都必須在 `py/pyvenv.cfg` 含 `home = /usr/bin` 與 `include-system-site-packages = false`，否則 rc=3、不建映像。
  - `SW_UPGRADE_SYSTEM_PYTHON` 只供測試模擬缺件；指向 `/usr/bin` 以外的直譯器或版本不符一律拒絕，所以不構成回退。
  - 7 個回歸（dry-run 流程、缺件與實體不在 `/usr/bin` 2 例、pyvenv.cfg 4 例）在實作前 7 項全紅、實作後全綠。fail-closed 測試都以 PATH 首位的假 docker 確認沒有呼叫 build／run／exec。
- **OD-48、OD-48 修訂、OWNER-BATCH #1～#3（含 cgroup 裁決修正）、SPEC-GAP #5／#10、S1X-c-1／c-2、OD-51**：皆由 OD-48 分支實作並已通過非實作者審查，本輪只負責合入，未改動其程式。
- **OD-50**：install.sh 本身本輪未改。
- **S1-3185c6d-1**：Docker 驗證的 bundle 建法已與 workflow 對齊（見上）。
- **審查報告**：
  - 起點時最新的 `62ffec7_b84ee5b-sol.md`：NO FINDINGS，可放行。
  - 本輪期間新出的 `b84ee5b_407ed7e-sol.md`：可放行，沒有 High／Medium。
    - S1-407ed7e-1 [Low]：README 兩段殘留「升級前請先 `serialwrap daemon stop`（TODO(OD-48)）」，與同檔的 OD-48 段矛盾。依收斂規則 Low 不修，請編排者記入 `low-backlog.md`。
    - 既有碼待辦：wheelhouse 的 PyYAML 以主機 PATH 上的 python3 下載，主機為 3.13 時會抓到 cp313 wheel。不計入結論，本輪也未修。本輪 Docker 實跑時 PATH 上的 python3 為 3.12 venv，不受影響。
- 結束前再 fetch `docs/parity-amendments`（`9e86f99`）：只有上述新報告與其他分支的報告，沒有新的 s1 裁決。

## 測試數字

| 項目 | 起點 `b84ee5b`（乾淨 worktree） | 最終 `ed46551`（同一 worktree） |
|---|---|---|
| `python3 -m pytest -q tests/` | 20 failed, 5322 passed, 164 skipped, 1 xfailed（34:48） | 12 failed, 5382 passed, 164 skipped, 1 xfailed（33:08） |

**起點 20 failed 的組成**：

- 10 項為 `test_rust_dep_direction.py`。原因是 `dev` 的 rustup 沒有預設工具鏈，暫存 workspace 不在 `rust-toolchain.toml` 範圍內，屬環境因素。補上 `rustup default stable`（1.99.1）後，在起點 worktree 重跑為 11 passed。
- 其餘 10 項與上輪回報的起點集合相同，皆為環境因素：
  - 缺 pyserial：5 項（`test_doctor.py` 1 項、`test_stage0_tx_outcome.py` 1 項、`test_stage0_tx_permit.py` 2 項、`test_stage0_uart_exclusive.py` 1 項）
  - docker 網路拓撲：2 項（`test_cli_mgmt_remote.py::test_remote_topologies_docker`、`test_docker_l15.py::test_docker_smoke_two_container`）
  - Windows 原生 cargo：1 項（`test_rust_bench_tools.py::test_cargo_workspace_tests_windows_native`）
  - P4 證據：1 項（`test_rust_stage1b_registration.py::test_p4_required_cases_have_passing_evidence`）
  - L1 lock：1 項（`test_l1_judge.py::test_l1_lock_nodeids_subset_of_collect_only`）

**最終 12 failed**：起點那 10 項全部仍在，另多出 2 項：

- `test_rpc_blocking_offload.py::test_release_rpc_offloaded_from_event_loop`（health.ping 被阻塞 0.62 s，門檻 0.5 s）
- `test_stage0_uart_exclusive.py::TestAttachProbeDrain::test_force_spawn_does_not_bypass_uart_lock`（session 仍為 DETACHED）

起點到最終之間，`sw_core/` 與這兩個測試檔都沒有任何變動。重跑前確認沒有洩漏的 serialwrapd，然後以同範圍（兩檔）序列重跑兩次：兩次皆 52 passed、1 skipped、1 failed，那一項 failed 只是起點既有的 pyserial 缺件。失敗集合與全套時不同，判定為時序因素，不屬本輪引入。

其他驗證：

- 全套中的兩個 Docker 實跑測試（`test_docker_upgrade_0_3_2_to_0_4_0`、`test_docker_install_sh_upgrade_from_0_3_2`）另在 HEAD 單獨以 `-rs` 重跑：2 passed、非 skip（2:14），走的是新的 build venv 流程。
- Cargo（`a9b8a38`，唯一含 Rust 變動的 commit）：`cargo test -p sw-cli -p sw-pal --features test-fakes` 共 59 個測試 binary，814 passed、0 failed、5 ignored；`cargo check --workspace --target x86_64-pc-windows-gnu` 成功。`e1c5365`、`407ed7e`、`ed46551` 沒有動 Rust。
- 每組 commit 都跑過以下檢查，結果皆 rc=0：
  - `check_cfg_boundary.py`
  - `tools.contracts check`（ok）
  - `regen_lock.py --check`
  - `policy_check --repo .`（R-19、R-22 為 warn，與起點相同）
  - `git diff --check`
- 觸及的 Python 測試：`test_docker_upgrade.py`、`test_install_sh.py`、`test_install_docs.py`、`test_release_workflow.py`，不含兩個 Docker 實跑，共 123 passed。證據守門 `test_policy_r21_allow_guard.py`、`test_evidence_index.py`、`test_evidence_labels.py` 共 31 passed。
- 凍結鎖：本輪沒有改動。新增的都是 pytest 節點，不屬任何 L1 set。

## Docker 實跑

有實跑：VM 內 dockerd、ubuntu:24.04 digest 映像、`--network none`。容器內的 apt 以原本的 HTTP 來源即可建映像，未改來源，也沒有 scratch 複本。bundle git revision `407ed7e`，以 build venv 自建，`py/pyvenv.cfg` 為 `home = /usr/bin`。

| 執行 | 結果 |
|---|---|
| `run-upgrade.sh --mode all` | systemd-user **19/19 PASS**（`systemctl_stop`、`waited_ms` 5265、取樣 1522、0 次重疊）；on-demand **19/19 PASS**（`rpc_daemon_stop`、5416 ms、取樣 1590、0 次重疊） |
| `--mode systemd-system --bundle <同一 bundle>` | **13/13 PASS**（`systemctl_stop`、5173 ms、取樣 1573、0 次重疊，`default.yaml` 列 `package_updated`）；走 `--bundle` 的 pyvenv.cfg 檢查 |
| `--scenario install-sh --bundle <同一 bundle>` | systemd-user、on-demand 各 **12/12 PASS**（取樣 895／951、0 次重疊） |

- 五份報告的 `bundle_manifest_sha256`（`92baa19…`）與 wheel 雜湊相同。
- 證據：`evidence/stage1/upgrade-docker/install-sh-407ed7e/`，內含五份報告、`SHA256SUMS`，以及過濾後的 `bundle-pyvenv.txt`（只留四列，不含 host 路徑）。
- 觀察項：
  - 與前輪相同：OBS-0.3.2-ONDEMAND-START、OBS-UPGRADE-ROLLBACK-PIPX、OBS-UPGRADE-STALE-CURRENT（OD-49 已知限制）、OBS-0.3.2-SYSTEM-UMASK、OBS-SYSTEM-PROFILES-MANAGED。
  - install-sh 兩模式出現 OBS-UPGRADE-NOT-STARTED，與 §5.1 一致。
- 這是開發中 bundle（`--version-override 0.4.0`），不取代 release 前置條件 #3。

## 不成立主張

無。兩份審查報告沒有 High／Medium，本輪也沒有不採納的 finding。

## SPEC-GAPS

1. `rulings/s1.md`「OD-48 OWNER-BATCH #3 裁決修正」列寫「以本機 Docker 實跑 `run-upgrade.sh --mode all` 三模式 PASS 為準」，本輪任務指示也寫 `--mode all` 三模式。但 OD-48 的設計（含既有測試）是 `all` 只涵蓋 systemd-user＋on-demand，systemd-system 必須明確指定。本輪沒有改 `all` 的語意，以 `--mode all`＋`--mode systemd-system` 兩次執行取得三模式 PASS。若要讓 `all` 涵蓋三模式，屬 OD-48 runner 語意變更，需要編排者裁決並同步修改該測試。

## OWNER-BATCH

無。本輪沒有需要擁有者簽核的事項。

## LOCAL 驗證項

- Windows 原生：本輪只做 `cargo check --target x86_64-pc-windows-gnu` 交叉檢查。Windows 原生的 cargo test、`win-pytest.sh -m core_portable／windows_native`，以及 OD-48 在 Windows 的父行程判定（LOCAL-WINDOWS-TODO）都沒有實跑。
- install.sh 的預設下載路徑（GitHub release 的 tarball＋`SHA256SUMS`）要等 0.4.0 release 資產上架後才能對真實 GitHub 驗證。
- release 前置條件 #3：以 `VERSION=0.4.0` 的正式 release bundle（不用 `--version-override`）重跑 §4 升級驗證。
- 實體 UART／DUT：本環境沒有，不宣稱任何 `physical_uart`／`windows_com` 證據。

## 未完成項

- S1-407ed7e-1（Low，不修）：README 中英兩段的 install.sh 升級說明仍寫「先 `daemon stop`」並標 TODO(OD-48)，`tests/test_install_docs.py` 釘住這個標記。改寫時要一併調整該斷言，並建議同時新增「0.3.2 daemon 執行中直接 `install.sh --bundle`」的 Docker 檢查（用新的檢查 id）。這兩項請編排者排入後續工作包或 low-backlog。
- 既有碼待辦：wheelhouse 的 PyYAML 下載跟隨主機 python3 的標籤（見審查報告），本輪未修。
