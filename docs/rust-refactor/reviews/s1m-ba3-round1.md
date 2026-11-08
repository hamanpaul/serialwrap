# S1M-ba3 第 1 輪回報

日期：2026-11-05。分支：`feature/s1m-ba3`；起點：`f59ce8050111550607f59aa8593e0a6393af6df6`。

## Commit 與逐項處理

- `eede1d45` — `fix(release): 固定升級 wheel 目標並綁定驗證來源`
- 本回報與四份原始 Docker 報告另以文件 commit 保存；其提交識別以 Git 歷史為準。四份報告的 bundle 與 driver revision 均為完整 `eede1d4515c0fac018a2dd4bb54f7d4e9039d64f`。

| 項目 | 處理 | 內容 |
|---|---|---|
| A3 | fixed `eede1d45` | PyYAML 下載固定 `--python-version 3.12 --implementation cp --abi cp312 --platform manylinux_2_17_x86_64`，映像建置與容器執行固定 `linux/amd64`；dry-run 與實跑共用同一組 wheel 目標參數。 |
| A4 | fixed `eede1d45` | 升級報告新增容器內實際載入的 `run_upgrade_container.py`／`upgrade_checks.py` 雜湊及獨立的 `driver_git_revision`。升級入口（含 `--bundle`）與正式 bundle 入口在副作用前拒絕未暫存、已暫存、未追蹤的來源變更，過程及結束時再次核對來源與 HEAD。 |
| D10 | 依本輪 Low 一律不修的規則保留 | 七項明細見下方「既有碼待辦」，交編排者 low-backlog。 |

`mkbundle.sh --dev` 供開發回歸使用，允許未提交來源；`run-upgrade.sh --dry-run` 不產證據，也不要求乾淨工作樹。正式 bundle 與實跑升級證據使用乾淨提交。

未修改 `sw_core/`、`serialwrapd.py`、`contracts/rpc_schema.json`、Rust crate 或任何凍結鎖；Stage 2 Python daemon 對齊基線無變更。回歸由 pytest／替身與拋棄式 Docker 覆蓋，無需新增實體 UART regression case。

## High／Medium 核對與不成立主張

指定來源審查的 A 段未修項與 B 段新增項中，沒有本範圍尚未處理的 High／Medium。A3、A4 為 brief 明定承接的漏追項，均如上修正。審查中其他分支已修項不重做。

不成立主張：無。

## 先紅後綠

只新增測試時，對未修的 `f59ce805` 程式執行新回歸：**8 failed、95 deselected**。修後同一選取為 **8 passed、95 deselected**。修前失敗 ID：

- `tests/test_docker_upgrade.py::test_wheel_download_targets_container_python_even_on_host313`
- `tests/test_docker_upgrade.py::test_evidence_entry_refuses_dirty_source_before_side_effects[tracked-upgrade]`
- `tests/test_docker_upgrade.py::test_evidence_entry_refuses_dirty_source_before_side_effects[tracked-release_bundle]`
- `tests/test_docker_upgrade.py::test_evidence_entry_refuses_dirty_source_before_side_effects[staged-upgrade]`
- `tests/test_docker_upgrade.py::test_evidence_entry_refuses_dirty_source_before_side_effects[staged-release_bundle]`
- `tests/test_docker_upgrade.py::test_evidence_entry_refuses_dirty_source_before_side_effects[untracked-upgrade]`
- `tests/test_docker_upgrade.py::test_evidence_entry_refuses_dirty_source_before_side_effects[untracked-release_bundle]`
- `tests/test_docker_upgrade.py::test_upgrade_report_hashes_actual_container_driver`

wheel 回歸使用不需要 Docker 的 pip 呼叫替身，驗證主機版本不會決定下載目標；本輪實跑主機為 Python 3.12.3，未宣稱實際以 3.13 主機跑 Docker。

## 驗證結果

所有測試均以前景程序執行並等結束。bwrap 將真實根檔案系統唯讀掛載，只開放本 worktree 寫入，另隔離 HOME、XDG、`/run`、`/dev` 與 PID 空間；保留根 conftest。Docker pytest 的暫存輸入位於 worktree 的 `target/`，確保宿主 Docker 可讀到唯讀掛載來源；只有該次執行的 Docker socket 被提供給沙箱。

| 命令／檢查 | 結果 |
|---|---|
| `python3 -m pytest -q tests/test_docker_upgrade.py`（另指定 worktree 內 basetemp 與 JUnit 路徑） | **103 passed，0 failed，0 skipped**；215.16 秒，含兩個真 Docker 測試。 |
| `python3 -m pytest -q tests/test_rust_mkbundle.py tests/test_resource_markers.py` | **38 passed，0 failed**；112.92 秒；marker 稽核掃描時另有 11 則既有 `invalid escape sequence` SyntaxWarning。 |
| `cargo test --workspace --features test-fakes --locked` | **1026 passed，0 failed，7 ignored**；85 個結果群組（含空的 doc-test 群組）。 |
| `cargo test --workspace --locked` | **1026 passed，0 failed，7 ignored**；85 個結果群組。 |
| `cargo check --workspace --target x86_64-pc-windows-gnu --locked` | **PASS**；6 個 workspace crate，6.93 秒；只屬交叉編譯證據。 |
| `python3 scripts/rust/check_cfg_boundary.py` | **PASS**。 |
| `python3 -m tools.contracts check` | **PASS**；`ok=true`、`findings=[]`。 |
| `python3 tests/acceptance/regen_lock.py --check` | **PASS**；案例鎖、屬性、統計與 L1 必要集合結構通過；未修改鎖。 |
| `python3 -m policy_check --repo .` | **25 pass、0 fail、2 warn**（R-19 CI 解析提示、R-22 既有文件懸空引用）。 |

兩次 cargo 的 7 ignored 為既有項目：`mkbundle_respects_cargo_target_dir_override`、`real_mkbundle_dev_output_verifies`、`windows_switch_running_exe_rename`、`cap_windows_native__launcher_gui_subsystem`、`cap_windows_native__launcher_parent_kill_kills_child`、`cap_interactive_desktop__exec_or_wait_ctrl_c`、`cap_supervisor_runtime__systemd_hold_blocks_restart_on_failure`。第一項已由本輪 mkbundle pytest 驅動；其餘不冒稱本輪原生／擁有者證據。硬體與擁有者項維持 **BLOCKED-OWNER**。

最後驗證失敗 ID：**無**。live guard 的 daemon 維度因刻意隔離而顯示不可達 SKIP，這是 guard 診斷，沒有將任何測試項目轉為 PASS。依 brief 明確限制，未跑全套 pytest、slice gate 或出口證據 driver。

## Docker 原始證據

原文與 [SHA256SUMS](../../../evidence/stage1/upgrade-docker/s1m-ba3-round1/SHA256SUMS) 位於 `evidence/stage1/upgrade-docker/s1m-ba3-round1/`。四份報告的 driver 雜湊與來源檔一致，且不含宿主家目錄路徑。

| 情境 | 模式 | 檢查 | two-reader 取樣／重疊 |
|---|---|---|---|
| upgrade | systemd-user | 19 PASS | 1274／0 |
| upgrade | on-demand | 19 PASS | 1453／0 |
| install-sh | systemd-user | 12 PASS | 970／0 |
| install-sh | on-demand | 12 PASS | 797／0 |

兩個情境實際下載的依賴均為 `pyyaml-6.0.3-cp312-cp312-manylinux2014_x86_64.manylinux_2_17_x86_64.manylinux_2_28_x86_64.whl`。本輪兩組 run 對應的拋棄式容器與映像已逐名查核，殘留均為 **0**；容器內測試 daemon 隨容器移除，host 測試則在各自 PID 沙箱結束後清除。Docker 證據屬 **L1.5-docker**，不替代真機驗收。

## SPEC-GAPS

無本範圍的新契約缺口或需擴權項目。

## 既有碼待辦

D10 依本輪規則不修，由編排者收入 low-backlog；未更新 worktree 外的追蹤清單：

- `S1-607ea76-1`：手動降級 stop 等待逾時後仍可能執行 pipx／setup。
- `S1-607ea76-2`：release 計畫舊證據取代方向的文字顛倒。
- `S1-607ea76-3`：部分 driver 讀取仍混用固定 `/proc` 與 `proc_root`。
- `S1-607ea76-4`：Windows 測試替身以 Timer 刪目錄，可能遇檔案分享競態；來源審查標示為推論，未在本輪冒稱重現。
- `S1-ed46551-1`：dry-run 在重用 bundle 時仍列出自建步驟。
- `S1-ed46551-2`：系統 Python 版本／build venv 基底的 fail-closed 路徑缺指定回歸。
- `S1-ed46551-3`：`require_system_python` 未先檢查 `venv`／`ensurepip` 能力。

## REMAINING:

D10 的七項 Low：依本輪明確規則延後，由編排者收進 low-backlog；A3、A4 與本輪必要驗證均已完成。
