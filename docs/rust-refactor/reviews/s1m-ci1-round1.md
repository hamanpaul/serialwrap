# S1M-ci1 第 1 輪

起點：`5cac8744bb28b780b18ffa952cd1d9b199a42f6f`；分支：`feature/s1m-ci1`。
範圍：Rust Linux 黑箱步驟的三項 CI 失敗。未提供本子項上一輪 A／B 審查；本輪逐項處理 brief 中的失敗。

## 逐項處理

1. `test_bb_delegation.py::test_windows_exec_or_wait_exit_code_and_terminate_propagation`：fixed `9a61ad82`。
   - 根因：CI 使用 `--confcutdir=tests/contract_blackbox`，沒有載入根 conftest 的 Linux → `windows_native` skip。黑箱 conftest 原本只處理反方向的 `posix_only`，因而在 Linux 呼叫 `ctypes.WinDLL`。
   - 修法：於黑箱 conftest 的 setup hook 補齊 Windows 原生平台判斷，在 fixture 啟動前處理。既有 Windows 原生測試與所有斷言保留，Linux skip 不是 Windows PASS。
   - 該節點仍保留於 `s1a_blackbox.windows` 與 `s1a_delegation.windows`；凍結鎖沒有變更。
   - 未把任何既有測試改標 POSIX；這支依賴 Windows kernel32 process handle／Job Object，只適用原生 Windows。
2. `test_cli_mgmt_ab_suite.py::test_cli_mgmt_ab_suite_execution`：namespace 問題 fixed `9a61ad82`；完整 suite 待 Docker。
   - 已證實的直接原因：CI 直接跑 pytest，與本機 `ab_blackbox.sh` 先進入 PID namespace 不同；巢狀 A／B 的 `pidns_rig` 無法以非特權 unshare 建立 namespace，必要節點被 skip，judge 正確拒絕。
   - 修法：新增 `scripts/rust/blackbox_sandbox.sh`，以實際 namespace inode 設定 `SW_PIDNS_OUTER`；根目錄唯讀、僅本 worktree 可寫，獨立 `/dev`、`/proc`、`/tmp`、`/run`、PID。既有巢狀 A／B 繼承 namespace，照常執行必要節點。
   - CI 安裝 bubblewrap，僅在一次性 hosted VM 開放 AppArmor 的非特權 user namespace；本機未改 sysctl。預設不暴露 Docker socket；CI 明確使用 `--with-docker` 保留真正 Docker 測試。
   - 實跑 A／B 的 `test_daemon_restart_ab` 皆 PASS；原先的 namespace 未核准 skip 已消失。完整巢狀 suite 仍因另一個凍結 Docker 節點 `test_remote_topologies_docker` 在兩輪皆缺能力而 FAIL，不能表述為 suite 全過。
   - 先前未完整辨識巢狀 Docker 依賴：該節點執行能力探測後因沙箱無 Docker socket 而 SKIP，沒有連到宿主 Docker daemon，也沒有啟動容器；未以 mock 替代這項驗收。
   - 既有 judge、skip 核准表與必要節點完全保留。
3. `test_cli_mgmt_update.py::test_update_systemd_exit1_not_relaunched_l15`：runtime 可攜性缺口與診斷 fixed `9a61ad82`；**Docker 實跑仍未驗收**。
   - 實際 CI 斷言是 `INSUFFICIENT_EVIDENCE != PASS`；`evidence_layer=L1.5-docker` 已存在。不能以 brief 的概述誤判為缺欄位。
   - CI log 的 pytest dict repr 已省略 `runner_error`／`reason`；原 workflow 沒有上傳 artifact。唯讀查詢 run `42074645309` 的 artifact 清單為空，無法由既有資料確認是哪個 runner 例外。
   - 已證實的建置差異：`bb_l15.update_bundles()` 以 PATH 上的 `python3` 建 venv；CI log 顯示 setup-python 位於 `/opt/hostedtoolcache/Python/3.12.3/x64`，而 `systemd.Dockerfile` 只安裝 Ubuntu 系統 Python。`mkbundle.sh` 的 `venv --copies` 仍保留 base home，與既有 release／run-upgrade 要求 `home = /usr/bin` 不符。
   - 修法：CI 先驗證系統 Python 與 argparse golden 的精確版本相同，再建立專用 build venv；開發 bundle、黑箱與 G5 子 bundle 全部繼承此 PATH。Docker 測試啟動前新增與 release／run-upgrade 相同的 `pyvenv.cfg` 防線，不接受 toolcache home 或 system-site-packages。
   - 原 run 未保留例外細節，因此這個確定的可攜性缺口是否就是原 INSUFFICIENT 的全部原因仍不確定；不能宣稱已修完原失敗。
   - 修正診斷：斷言附完整 JSON、runner exit／stdout／stderr，CI `always()` 上傳 A／B、JUnit、resource report 與已保留的 systemd 證據。所有原斷言與 INSUFFICIENT 判定保留。
   - 依禁令未跑 Docker；不得宣稱這支已通過。需下一輪 CI 或另派 Docker 執行者提供完整報告。

## 回歸證據

- `target/s1m-ci1/regression-red.log`：平台防線與沙箱入口修前 `2 failed, 1 passed`；反方向 POSIX 防線原本即通過。
- `target/s1m-ci1/diagnostic-red.log`：修前無法從斷言取得完整 runner 原因及輸出，`1 failed`。
- `target/s1m-ci1/regression-green.log`：上述項目加上 runtime 防線共五項全部通過；合成 INSUFFICIENT 報告依然拋出 AssertionError，保存內容與原報告逐值相同。這是診斷工具的單元回歸，不能替代 Docker 實跑。
- `target/s1m-ci1/runtime-red.log`：修前錯誤地進入 Docker runner；修後拒絕 toolcache home，先於 Docker 呼叫失敗。
- 新增沙箱自測標為 `posix_only`：依賴 Linux bubblewrap、`/proc`、devpts 與唯讀 mount，原生 Windows 不適用。其他新增回歸維持 `core_portable`，沒有把既有可攜案例改標 POSIX。
- 沙箱探針驗證 namespace、空 `/run`、不暴露實體 ttyUSB、worktree 外寫入回 EROFS、worktree 內可寫。

## 測試與 gate

- Rust 黑箱主集合：`776 passed, 6 skipped, 1 failed, 1 deselected`（1417.78 s）。唯一失敗為 `test_cli_mgmt_ab_suite_execution`，原因是凍結 remote Docker 節點在 A／B 皆缺能力；直接 systemd Docker 節點依禁令未跑。此主集合使用移除獨立網路限制前的沙箱，最終沙箱另補跑受影響的 RPC 與工具自測。
- A／B：A exit 0、B exit 0、compare exit 1；11 `ok`、1 `rust-only-ok`、1 `evidence-insufficient`。`test_daemon_restart_ab` 兩輪皆 pass，A 輪 release-check `ok:true`。完整證據：`target/s1m-ci1/ab-suite/summary.json`。
- 黑箱 6 個 skip：5 個 Windows 原生節點（委派、skill 平台、兩項 daemon stdio pipe、自動啟動）與 1 個 remote Docker 能力項。沒有把這些 skip 算作 PASS。
- 黑箱 namespace 結束後殘留行程為空，見 `target/s1m-ci1/blackbox-cleanup.txt`。
- Windows GNU：`cargo check --workspace --target x86_64-pc-windows-gnu` exit 0。
- Cargo 首次因本輪額外加的獨立網路限制，`resolve_tcp_gai_matches_python`／`tcp_refused_and_gai_and_timeout0_match_python` 回 -3 而非 oracle 的 -2；已移除該限制並沿用宿主解析器，未改斷言或 Rust。原紅燈保留於 `target/s1m-ci1/cargo-fakes-red-netns.log`。
- 其後一次 Cargo 的 `file_lock_nonblocking_contention_wouldblock` 失敗。Rust 原始碼與起點相同；起點封存副本 focused 20 輪均 `13 passed`，未能重現，不能據此宣稱已證實為 baseline 或 flaky。紅燈：`target/s1m-ci1/cargo-fakes-fs-lock-fail.log`；基準：`target/s1m-ci1/baseline-fs-lock.log`。
- 最終沙箱的 Python 工具自測／RPC／resource marker 稽核：`83 passed, 11 warnings`（69.19 s）；警告為 AST 稽核遇到的 `SyntaxWarning: invalid escape sequence`。Rust CLI RPC：`55 passed`（27.87 s）。證據：`target/s1m-ci1/focused.log`、`target/s1m-ci1/rust-rpc.log`。
- `cargo test --workspace --features test-fakes`：完整 `1041 passed, 0 failed, 7 ignored`；`cargo test --workspace`：完整 `1041 passed, 0 failed, 7 ignored`。兩次 exit 0；7 個 ignored 不計為通過。證據：`target/s1m-ci1/cargo-fakes.log`、`target/s1m-ci1/cargo-default.log`。先前檔案鎖紅燈仍保留為待分類項。
- `python3 scripts/rust/check_cfg_boundary.py`：exit 0。
- `python3 -m tools.contracts check`：exit 0、`ok:true`、findings 0；error code 100、session last error 34。
- `python3 tests/acceptance/regen_lock.py --check --base 5cac8744`：exit 0，結構、案例屬性與基準只增比對通過；沒有改鎖。
- `python3 -m policy_check --repo .`：exit 1；24 pass、1 fail（R-07）、2 warn（R-19、R-22）。起點封存副本同樣 R-07 失敗；沒有加 exemption 把 FAIL 轉為 PASS。
- 最終追蹤的黑箱／Cargo namespace 殘留行程皆為空：`target/s1m-ci1/cleanup-final.txt`。

## 不成立主張

無。

## SPEC-GAPS

- 第三項缺少 CI 完整 Docker 證據，不能確認根因；本輪禁跑 Docker。保留未完成，未修改 runner／產品行為以猜測修復。
- 完整「同命令全部通過」包含直接 systemd Docker 案例與巢狀 A/B 的 remote Docker 案例。本輪排除直接 systemd 節點後，巢狀 remote Docker 節點仍被 judge 判為能力不足；不擴張核准 skip、不刪節點、不把它表述為完整驗收。
- 範圍外 `sw-pal::fs_lock::file_lock_nonblocking_contention_wouldblock`：本輪一次失敗、起點 focused 20 輪未重現，根因與 baseline 歸屬尚不確定。不擴修 Rust 檔案鎖，也不以重跑綠燈消除此失敗記錄。

## 既有碼待辦

- policy R-07：起點封存副本同樣為 `VERSION (0.4.0) mismatches latest tag (v0.3.2)`。版本與 tag 未變；release／tag 對齊留給既有發布流程，不在本輪修改。證據：`target/s1m-ci1/policy-baseline.log`。
- 其他分支之 finding 未處理；範圍外檔案鎖失敗的未定歸屬見 SPEC-GAPS。

## 交付狀態

程式 commit：`9a61ad82`（`fix(ci): 修正 Rust 黑箱隔離與 runtime 建置`）。實作限於測試、CI 與測試工具，另附 changelog／本報告。未改 Rust 產品碼、Python daemon、RPC schema、bridge 核准來源或凍結鎖。未 push、未執行全套 pytest、Docker 容器情境、slice gate 或出口 driver，未部署。

## REMAINING:

- Docker `test_update_systemd_exit1_not_relaunched_l15` 的實際 runner 根因與實跑驗證；以及完整巢狀 A/B 的 `test_remote_topologies_docker` 兩輪實跑。完整相同 CI 命令全過仍未達成。
- 上述 `file_lock_nonblocking_contention_wouldblock` 偶發失敗的歸屬與根因尚未證實，列範圍外待分類。
- CI hosted VM 的 workflow 重跑，由 driver 後續交付驗證。
