# install.sh bundle 化雲端回報（OD-50，2026-10-29）

- 依據：`docs/rust-refactor/reviews/rulings/s1.md` 的 OD-50 列（擁有者裁決 (b')）；`release-0.4.0-plan.md` RELEASE-TODO-02、§2、§5、§7；`scripts/rust/mkbundle.sh`。
- 分支：`feature/install-sh-bundle`（起點 `origin/feature/rust-refactor` `62ffec7`），已 push；未開 PR、未建 tag、未改 `VERSION`。
- 環境：雲端容器，Rust 1.96.1（`rust-toolchain.toml` 釘定），`/usr/bin/python3.12`（3.12.3）venv＋PyYAML 6.0.3＋pytest 9.2.0＋jsonschema 4.26.0＋setuptools／wheel（`run-upgrade.sh` 建 0.3.2 wheel 需要）＋policy engine `2dcf2f8`；測試一律以一般使用者 `dev`（uid 30033，docker 群組）執行；docker 29.9.2 daemon 於容器內啟動。

## Commit 清單

| commit | 內容 |
|---|---|
| `b84ee5b` feat(install): install.sh 改為安裝 Rust bundle 並退役 pipx 路徑（OD-50） | install.sh 改寫、`tests/test_install_sh.py`／`tests/test_install_docs.py`、docker `--scenario install-sh`、README／CLAUDE.md／release 計畫／相關 docs、`changelog.d/od-50-install-sh-bundle.md` |

（單一 commit：每個 commit 須跑一次全套 pytest，本機全套約 34 分鐘，故將規格 (1)～(7) 收在同一個已驗證的 commit。）

## 各項做法

1. **用法**：`install.sh [--version X.Y.Z] [--bundle <目錄或 .tar.gz>] [--] [setup 參數...]`。
   - 未給 `--bundle`：以 curl（無 curl 用 wget；`SERIALWRAP_INSTALL_DOWNLOADER=auto|curl|wget` 可指定，供測試 wget 路徑）從 `${SERIALWRAP_RELEASE_BASE_URL:-https://github.com/hamanpaul/serialwrap/releases}/download/v<版本>/` 下載 `serialwrap-<版本>-linux-x86_64.tar.gz` 與 `SHA256SUMS`；未給 `--version` 時以 `<base>/latest` redirect 的最終 URL（`.../tag/vX.Y.Z`）解析版本。
   - 驗證：`SHA256SUMS` 中檔名欄位**完全等於** tarball 名稱的列（容許 `*` binary 標記），缺列或雜湊不符即 rc=1、不執行 setup。
   - 解到 `mktemp -d "${TMPDIR:-/tmp}/serialwrap-install.XXXXXX"`，`trap` 於 EXIT（INT／TERM 轉 exit）清除；bundle 根為解開目錄本身或其唯一含 `bin/serialwrap` 的子目錄（扁平或帶頂層目錄的 tarball 皆可）。
   - 執行 `<bundle>/bin/serialwrap setup "$@"` 並回傳其 rc。`--` 之後原樣轉交；未知參數（如 `--user`、`--on-demand`）也轉交，維持舊用法 `./install.sh --on-demand` 相容。
   - `--bundle` 目錄直接使用、tar.gz 先解開；`--version` 與 `--bundle` 互斥（rc=2）；`--version` 須為 `X.Y.Z`（rc=2）。
   - 解出的 `bin/serialwrap` 存在但不可執行（TMPDIR 為 noexec）時明確報錯並提示 `TMPDIR=<可執行目錄>`（docker 實跑發現，見下）。
2. **需求檢查**（任一不符 rc=2，訊息含繁中與 `requirement not met`，列出所有不符項；檢查早於任何下載或建立暫存目錄）：`uname -m` 為 x86_64；glibc ≥ 2.39（`getconf GNU_LIBC_VERSION`，失敗改 `ldd --version`；無法判定也算不符）；`/usr/bin/python3.12` 可執行。覆寫變數：`SERIALWRAP_INSTALL_ARCH`、`SERIALWRAP_INSTALL_GLIBC`、`SERIALWRAP_INSTALL_PYTHON312`（後者為要檢查的路徑）；未設時一律實際探測（docker 實跑即走實際探測）。
3. **舊 pipx 提示**：setup rc=0 後，若 `${PIPX_HOME:-~/.local/pipx}/venvs/serialwrap` 或 `~/.local/share/pipx/venvs/serialwrap` 存在，或 `pipx list --short` 首欄為 `serialwrap`，印出可執行 `pipx uninstall serialwrap` 的提示；不呼叫 uninstall（測試以假 pipx 記錄呼叫驗證只有 `list`）。setup 失敗時不提示。
4. **`-h/--help`**：用法、選項、下載來源與 `SERIALWRAP_RELEASE_BASE_URL`、系統需求、0.3.x 升級說明。
5. **pytest**（先紅後綠：install.sh 改寫前 `test_install_sh.py` 28 failed／2 passed、`test_install_docs.py` 4 failed；改寫後全綠）：
   - `tests/test_install_sh.py`（31 項，posix_only＋process_rpc）：假 bundle（`bin/serialwrap` 記錄參數、回傳 `FAKE_SETUP_RC`）；`--bundle` 目錄、tar.gz（扁平／帶頂層目錄）；本機 `ThreadingHTTPServer` 模擬 releases（`--version`、latest redirect，curl 與 wget 兩條）；SHA256 不符、缺列（含相近檔名列）、缺 SHA256SUMS 檔、缺 release 資產；三項需求各自不符（含 glibc 2.35／2.4／unknown）rc=2、未執行 setup、未發出任何 HTTP 請求、暫存目錄為空；較新 glibc（2.40／2.100／3.0）接受；setup rc 透傳（1／3／7）；`--` 之後原樣轉交（含 `--version`、`--bundle`、空白、第二個 `--`、`-h`）；pipx 提示三種情況；noexec 提示；`--help`。只連 127.0.0.1。
   - `tests/test_install_docs.py`（4 項）：README English 段與繁體中文段各自含 install.sh／bundle、系統需求、離線、0.3.x 升級（daemon stop＋TODO(OD-48)）、pipx uninstall 提示且不含 `pipx install "git+...@v0.3.2"`；CLAUDE.md 實際命令段；release 計畫 RELEASE-TODO-02 記 OD-50。
6. **文件**：README 頂部 Install、English Prerequisites／Quick Start（新增 Installer details、Upgrading from 0.3.x）、繁中 依賴／快速開始／尾段 安裝 同步改寫（兩段內容對照一致；0.3.x 升級寫「升級前請先 `serialwrap daemon stop`」並標 TODO(OD-48)，升級後需手動 `service start`／`daemon start`——依 §5.1 現況 OBS-UPGRADE-NOT-STARTED）。CLAUDE.md「實際命令」與「測試與除錯重點」的 install.sh 說明同步。release-0.4.0-plan.md：RELEASE-TODO-02 標為已依 OD-50 決定並描述新流程、§2 install.sh 與 wheel 列、§7 release notes 雙語各加一條；**未改 §5.1 與 §6**（OD-48 分支負責）。另同步 `docs/setup-tcp-endpoint.md`、`docs/func-test/realhw-stability-checklist.md` 的 install.sh 註解。fragment：`changelog.d/od-50-install-sh-bundle.md`（type: change）。
7. **Docker**：`scripts/docker/run-upgrade.sh --scenario install-sh`（預設 `upgrade` 情境與既有 18 項 `REQUIRED_CHECKS` 不變）。host 端把 release 候選 bundle 打包成 `serialwrap-0.4.0-linux-x86_64.tar.gz`（頂層 `serialwrap-0.4.0-linux-x86_64/`，`--transform ...,SH` 不改寫 symlink 目標），唯讀掛載 repo 的 `install.sh` 與 tarball；容器內 driver：pipx 安裝 0.3.2→建基線→`serialwrap daemon stop`→以 swtest 執行 `install.sh --bundle <tarball> -- <模式旗標>`（實際需求探測）→沿用既有判定的入口取代、設定保留、新 daemon、監管收斂、狀態相容、doctor（含 `version_pairing`）與 two-reader 取樣。必要檢查 `REQUIRED_INSTALL_SH_CHECKS`（12 項，新增 `old_daemon_stopped`、`install_sh_upgrade`）；`overall_verdict` 加 `required` 參數（預設仍為 18 項）。新增 pytest：判定純函式、dry-run、未知情境 rc=2、docker 實跑。

## 測試數字

| 項目 | 起點 `62ffec7`（乾淨 worktree） | `b84ee5b` |
|---|---|---|
| `python3 -m pytest -q tests/` | 10 failed, 5286 passed, 164 skipped, 1 xfailed（34:23） | 10 failed, 5332 passed, 164 skipped, 1 xfailed（34:18） |
| 失敗集合 | 見下 | **與起點完全相同**（新增 0、修好 0） |
| 受影響測試 `test_install_sh.py`＋`test_install_docs.py`＋`test_docker_upgrade.py`（不含兩個 docker 實跑） | — | 73 passed |
| `python3 -m policy_check --repo .` | rc=0（R-19、R-22 warn） | rc=0（R-19、R-22 warn；R-22 advisory 2881→2885，為 README 新增的 `state.json`／`config.yaml`／`SHA256SUMS` 等反引號檔名，與既有同類） |
| `python3 -m tools.contracts check` | — | ok |

起點既有失敗（皆與本變更無關，環境因素：容器缺 pyserial、docker 網路拓撲、Windows 原生 cargo、P4 證據與 L1 lock）：`contract_blackbox/test_cli_mgmt_remote.py::test_remote_topologies_docker`、`test_docker_l15.py::test_docker_smoke_two_container`、`test_doctor.py::TestWindowsDoctor::test_pyserial_ok_when_importable`、`test_l1_judge.py::test_l1_lock_nodeids_subset_of_collect_only`、`test_rust_bench_tools.py::test_cargo_workspace_tests_windows_native`、`test_rust_stage1b_registration.py::test_p4_required_cases_have_passing_evidence`、`test_stage0_tx_outcome.py::test_pyserial_port_write_maps_timeout_to_unknown`、`test_stage0_tx_permit.py` 2 項 pyserial、`test_stage0_uart_exclusive.py::...::test_pyserial_access_denied_raises_device_held_unknown`（2 筆）。

## Docker 是否實跑

**是**（容器內 dockerd，ubuntu:24.04 digest 映像，`--network none`）。
- 開發中實跑：第一次兩模式皆 FAIL——容器 `/tmp` 為 docker tmpfs（預設 noexec），解開的 `bin/serialwrap` 不可執行。修正：install.sh 對此明確報錯並提示 `TMPDIR`（新增 pytest），driver 把 install.sh 的 `TMPDIR` 指向 `~swtest/.cache/sw-install-tmp`，殘留檢查改看該目錄。修正後 run `dbg3`（bundle git revision `62ffec7`＋工作樹變更）：systemd-user **PASS**（取樣 946、0 次重疊）、on-demand **PASS**（取樣 986、0 次重疊）；報告 sha256 `335bc332…6177`（systemd-user）、`d0df4a6c…b79bf5`（on-demand），未提交進 repo（只留在雲端容器），不作為 release 證據。
- 觀察：on-demand 模式照舊出現 OBS-0.3.2-ONDEMAND-START；兩模式 install.sh 升級後 setup 皆為 `daemon_was_not_running`／`started:false`（OBS-UPGRADE-NOT-STARTED，與 §5.1 第 3 步一致，文件已寫明需手動啟動）。
- `b84ee5b` 的全套 pytest 亦包含 `test_docker_install_sh_upgrade_from_0_3_2` 與既有 `test_docker_upgrade_0_3_2_to_0_4_0` 實跑（skipped 數與起點同為 164，兩者皆通過）。

## 未完成／待後續

- 0.3.2 daemon **執行中**直接執行 install.sh 的情境未做（依指示留給 OD-48 分支 `feature/od48-legacy-handoff`）；README 兩段的升級說明以 `TODO(OD-48)` 標註，OD-48 合入後需更新，§5.1／§6 未動。
- RELEASE-TODO-01（release workflow 產出並上傳 Linux bundle tarball 與含該列的 `SHA256SUMS`）不在本分支；install.sh 的預設下載路徑在 0.4.0 release 資產上架前無法對真實 GitHub 驗證（目前 latest 為 0.3.2，無 bundle 資產，install.sh 會明確報下載失敗）。tarball 佈局由 install.sh 兩種皆接受（扁平或單一頂層目錄）。
- `--bundle` 離線安裝不檢查 `SHA256SUMS`（README 已寫明請自行 `sha256sum -c --ignore-missing SHA256SUMS`）。
- 本報告的 docker 證據與 pytest log 未保存進 repo。
