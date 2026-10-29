# 0.4.0 release 前置準備：第 2 輪雲端回報（2026-10-29）

- 依據：`reviews/s1-cloud/62ffec7_3185c6d-sol.md`（Codex sol 對 `feature/release-0.4.0-prep` 的增量審查，B 段 1 項 High：S1-3185c6d-1）、上一輪回報 `reviews/release-0.4.0-prep-report-20261029.md`、`reviews/rulings/s1.md` OD-50 列（Linux 使用者皆 Ubuntu 24.04 以上；bundle 內附 Python runtime 綁定 `/usr/bin/python3.12`）。
- 分支：`feature/release-0.4.0-prep`，起點 `3185c6d`，push 後 HEAD `2ddf90e`（fast-forward）。未開 PR、未建 tag、未改 `VERSION`（仍為 `0.3.2`）、未提交 collate 結果；未使用 codex／agy／pi。
- 環境：雲端容器 Ubuntu 24.04.5（`/usr/bin/python3.12` = 3.12.3）、Rust 1.96.1、Docker 29.9.2。測試用 venv 由 `/usr/bin/python3.12` 建立（PyYAML 6.0.3、pytest 9.0.3、jsonschema 4.10.3、pyserial 3.5、policy engine `2dcf2f8`）。pytest、policy_check、contracts check、cargo／mkbundle 開發建置都以一般使用者（`ubuntu`，uid 1000）執行。workflow 等效實跑需要 `sudo apt-get` 與 docker，所以以 root 執行。

## 範圍判定

- sol A 段：前輪 6 項全標「已修」，沒有待處理項。
- sol B 段：只有 S1-3185c6d-1（High），本輪處理。沒有 Medium。Low 依指示不處理（本來也沒有）。

## Commit 清單

| commit | 內容 |
|---|---|
| `2ddf90e` | `fix(release)`：`publish-linux-bundle` 改以系統 `/usr/bin/python3.12` 建 runtime，並新增 `pyvenv.cfg` 可攜性檢查與乾淨 ubuntu:24.04 容器 smoke；同步更新 `tests/test_release_workflow.py`、release 計畫 RELEASE-TODO-01 段，並新增 changelog fragment `release-0.4.0-linux-bundle-portable-python.md` |

## (1) S1-3185c6d-1：不再使用 hosted toolcache Python

- `runs-on: ubuntu-latest` 改為 `ubuntu-24.04`（OD-50），並移除 `actions/setup-python@v5`。
- 新增步驟 `system-python`（排在 `mkbundle` 之前）：
  1. `sudo apt-get install -y --no-install-recommends python3.12 python3.12-venv`；
  2. 確認 `/usr/bin/python3.12` 的 `platform.python_version()` 等於 `contracts/cli_help/_meta.json` 的 `python_version`（3.12.3），不符即 `::error::`；
  3. 以 `/usr/bin/python3.12 -m venv "$RUNNER_TEMP/build-venv"` 建立 build venv，在其中安裝 `pyyaml==6.0.3 jsonschema==4.10.3`（與前輪及 `rust.yml` 同版，不動系統套件，不受 PEP 668 限制）；
  4. 確認 build venv 的 `sys._base_executable` 位於 `/usr/bin`，再把 `build-venv/bin` 寫入 `$GITHUB_PATH`。
- 原理：mkbundle 會用 PATH 上的 `python3` 執行 `python3 -m venv --copies`。venv 的 `pyvenv.cfg` `home` 取自基底直譯器（`sys._base_executable`）的目錄，所以從 build venv 建出的 bundle runtime 是 `home = /usr/bin`。PyYAML 6.0.3 由 build venv 複製進 bundle。**`mkbundle.sh` 未修改。**
- 已驗證的陷阱：用 PATH shim（例如 `shim/python3 -> /usr/bin/python3.12`）會得到 `home = <shim 目錄>`，不可行；只有直接呼叫 `/usr/bin/python3.12`，或經由它建立的 venv，才會得到 `home = /usr/bin`。
- EXPECTED_PYTHON_VERSION：Ubuntu 24.04（noble）的 `python3.12` 套件為 `3.12.3-1ubuntu0.17`，`platform.python_version()` = `3.12.3`，與契約一致，**不需要放寬或改契約**。若日後 runner 映像的 patch 版變動，`system-python` 步驟會直接 `::error::` 失敗。

## (2) 可攜性驗證

- `smoke` 新增檢查：`versions/${VERSION}/py/pyvenv.cfg` 必須存在，且含整行 `home = /usr/bin` 與 `include-system-site-packages = false`。錯誤訊息會點名 `pyvenv.cfg` 與實際的 `home` 值。
- 新增步驟 `container-smoke`（排在 `smoke` 之後、`upload` 之前）：
  - 映像以 OD-16 (5) 同一 digest 的 `ubuntu:24.04@sha256:68ee2200…` 為基底，只加裝 `python3.12`，由 stdin Dockerfile 建置（無 build context）。建置時斷言 `/usr/bin/python3.12` 存在、`libpython3.12.so.1.0` **不存在**；映像內沒有 PyYAML、toolcache 或 venv 套件。
  - `docker run --rm -i --network none --user 1000:1000 -v "$root:/opt/serialwrap:ro"`：唯一的掛載是上一步從實際 tarball 解開的目錄（唯讀）。沒有掛載 repo、workspace 或 toolcache，也沒有 `--device`／`--privileged`。
  - 容器內腳本經 stdin 傳入，步驟如下：
    1. 在 `mktemp` 目錄下設好獨立的 HOME／XDG／`SERIALWRAP_CONFIG_DIR`／`RUN_DIR`／`STATE_DIR`／`WAL_DIR`／`LOG_DIR`，`SERIALWRAP_BY_ID_DIR` 為空目錄，`config.yaml` 為 `supervision_mode: on-demand`；
    2. 以 bundle runtime 執行 `-I -B -c 'import sw_core.daemon, yaml'`；
    3. 執行 `bin/serialwrap daemon start`；
    4. 執行 `bin/serialwrap daemon status`，並以 bundle Python 核對 `ok`、`running` 為真且 `version == VERSION`；
    5. 執行 `bin/serialwrap daemon stop`，確認 daemon pid 已結束，且之後的 `daemon status` 會失敗；
    6. 輸出通過標記。
  - 外層判定：docker 非零結束，或輸出缺少通過標記，都會 `::error::` 並阻擋上傳。
- `tests/test_release_workflow.py`（先紅後綠）：
  - 修改既有斷言：`runs-on` 由 `ubuntu-latest` 改為 `ubuntu-24.04`，原本「setup-python 3.12.3」的斷言改為**禁止** setup-python（此為收緊，不是削弱）。
  - 新增結構測試：
    - `system-python` 的位置與關鍵命令，以及全 job 不得引用 `hostedtoolcache`；
    - `container-smoke` 的隔離條件：`--network none`、只有一個 `-v` 且為 `:ro`、不得帶入 workspace／toolcache／裝置、映像不得預裝 `python3-yaml` 等套件、需有 daemon start／status／stop 與 on-demand 設定；
    - smoke 必須含 `py/pyvenv.cfg` 與 `home = /usr/bin`。
  - 新增行為測試（以 bash 實跑步驟）：
    - 5 種不可攜 runtime 讓 smoke 失敗，且失敗訊息須含 `pyvenv.cfg`：缺 `pyvenv.cfg`、home 為 toolcache、home 為 `/usr/local/bin`、home 為 `/usr/bin/extra`、`include-system-site-packages = true`；
    - 以假 docker 驗證 `container-smoke`：容器回報通過時成功，且 run 參數含 `--network none` 與唯讀掛載；容器內載入失敗（rc 127）或沒有通過標記時都會失敗。
  - 紅燈：workflow 修改前 `12 failed, 21 passed`；修改後 `33 passed`（前輪為 23，本輪新增 10 個測試）。

### 容器內等效實跑

以腳本解析 `release.yml` 的 `publish-linux-bundle`，依序用 `bash -e -o pipefail` 執行每個 `run` 步驟。跳過 checkout、rust-toolchain 與 upload。`$GITHUB_ENV`／`$GITHUB_PATH` 在步驟間傳遞，`TAG_NAME=v0.3.2`（依指示不改 `VERSION`），git revision 為 `2ddf90e`。

**正向（新 workflow）：job 全綠**

| 步驟 | 結果 |
|---|---|
| verify-version | rc=0 |
| system-python | rc=0，6.6 s |
| cargo-build | rc=0，43.5 s |
| mkbundle | rc=0，35.8 s |
| package | rc=0 |
| smoke | rc=0（`tar.gz: OK`、`smoke passed`） |
| container-smoke | rc=0，1.8 s |

- tarball `serialwrap-0.3.2-linux-x86_64.tar.gz`：24,789,815 bytes，sha256 `4ef832eef084222731c4a8bee29d25381ae091c2596322a615ac83de8544295f`。
- manifest `git_revision` 為 `2ddf90e1…`。
- bundle 內含 `pyyaml-6.0.3.dist-info`。
- `py/pyvenv.cfg` 內容：`home = /usr/bin`、`include-system-site-packages = false`、`version = 3.12.3`、`executable = /usr/bin/python3.12`。
- 容器輸出：

  ```
  bundle python runtime ok: /opt/serialwrap/versions/0.3.2/py/lib/python3.12/site-packages/yaml/__init__.py
  {"ok":true,"pid":12,"socket":"/tmp/sw-smoke.IjZ4q6/sw-run/serialwrapd.sock","warnings":["no_profiles_loaded","no_devices_found"]}
  {"id":1,"ok":true,"stopping":true}
  container smoke passed for serialwrap-0.3.2
  ```

  其中 PyYAML 由 bundle 自身的 site-packages 載入。

**負向對照（模擬 hosted toolcache）**

取得 GitHub `actions/python-versions` 的正式 toolcache 建置 `python-3.12.3-linux-24.04-x64.tar.gz`（sha256 `07ef2452a390e76505b5a26b37bae7f88bc260926ca70ae88cb679003ab42279`），裝到 `/opt/hostedtoolcache/Python/3.12.3/x64`。`ldd` 顯示其 `libpython3.12.so.1.0` 透過 RPATH 指向 toolcache。以它建立含 PyYAML／jsonschema 的 venv 並放在 PATH 首位，等同舊 workflow 的 setup-python＋pip。接著跳過 `system-python`、其餘步驟照跑：

- `pyvenv.cfg` 為 `home = /opt/hostedtoolcache/Python/3.12.3/x64/bin`，mkbundle、package 仍為 rc=0。
- **新 smoke 失敗**：`::error::py/pyvenv.cfg must contain 'home = /usr/bin' (OD-50): home = /opt/hostedtoolcache/Python/3.12.3/x64/bin`，job 停止，不會走到上傳。
- **單獨對同一個解開的 toolcache bundle 執行 `container-smoke` 也失敗**：`/opt/serialwrap/versions/0.3.2/py/bin/python3: error while loading shared libraries: libpython3.12.so.1.0: cannot open shared object file`，並輸出 `::error::bundle daemon failed in clean ubuntu:24.04 container (exit 127)`。這與 sol 推導的失敗情境完全一致，而且兩道檢查各自都能擋下。
- **舊 workflow（`3185c6d`）的 smoke 對同一 toolcache bundle 為 rc=0（`smoke passed`）**，證實原本的缺口確實存在。

## 測試數字

- 起點基準（`3185c6d`，`ubuntu` 使用者，全套序列執行）：`5 failed, 5318 passed, 160 skipped, 1 xfailed`（31 分 18 秒）。其中：
  - `tests/test_rust_bench_tools.py::test_cargo_workspace_tests_windows_native` 與 `tests/test_rust_stage1b_registration.py::test_p4_required_cases_have_passing_evidence`：缺 Windows 原生／P4 證據檔，與前輪相同。
  - `tests/test_functest_bridge_smoke.py::test_bridge_smoke_sm01`、`tests/test_l15_smoke.py::test_reject_zero_tx_runs_on_python`、`tests/test_l15_smoke.py::test_release_inflight_runs_on_python`：`ATTACH_FAILED:DeviceHeldError`。查明原因是我前兩次中止的基準 pytest 留下了 2 個 throwaway `serialwrapd`（contract_blackbox）沒有退出。清掉這兩個行程後，這 3 個測試在同一 HEAD 單獨重跑 `4 passed`（含前輪失敗的 `test_l1_judge.py::test_l1_lock_nodeids_subset_of_collect_only`，本輪環境已通過）。因此這 3 項屬於本輪自身操作造成的汙染，不是 repo 基線問題。
- `2ddf90e` 受影響測試：`tests/test_release_workflow.py tests/test_resource_markers.py tests/test_rust_ci_workflow.py tests/test_docker_upgrade.py::test_release_plan_accepts_only_real_stage1_gate_outcomes` 共 `50 passed`。
- `2ddf90e` 全套（已清除洩漏 daemon，序列執行）：`2 failed, 5331 passed, 160 skipped, 1 xfailed`（30 分 43 秒）。失敗集合為上述 2 項證據缺口，是起點基準失敗集合的子集；passed 增加的 13 項 = 新增的 10 個測試＋3 項不再受汙染的測試。未引入新失敗。
- `python3 -m policy_check --repo .`：rc=0，只有既有的 R-19／R-22 warn（與前輪相同，非 PR 上下文）。
- `python3 -m tools.contracts check`：`ok: true`、0 findings。
- 限制：`ubuntu` 不在 docker 群組，所以 pytest 內以 docker 實跑的案例（如 `test_docker_upgrade.py` 的實跑兩模式）在基準與 HEAD 兩次都是 skip，條件一致。容器驗證改由上一節的 root 等效實跑提供。

## OWNER-BATCH

1. **EXPECTED_PYTHON_VERSION**：無需裁決。ubuntu-24.04 系統 python3.12 為 3.12.3，等於 `_meta.json`。若 GitHub runner 映像日後換成 noble 的新 patch 版，`system-python` 會失敗，屆時需由擁有者決定是否更新契約；本輪未改契約。
2. **既有 Docker 升級驗證的同類風險（觀察，未改）**：`scripts/docker/run-upgrade.sh` 以 host 的 `python3` 執行 `mkbundle.sh --release-bundle`，再把 bundle 掛進 ubuntu:24.04 容器。若 host 的 `python3` 不是 `/usr/bin/python3.12`（例如 pyenv、toolcache，或本容器 `python3` → 3.13 的情況），產出的 bundle 一樣不可搬移。這不在 sol 報告範圍，請擁有者決定是否讓它也走同樣的 `/usr/bin/python3.12` build venv 並加上 `pyvenv.cfg` 斷言。

## 未完成項／交給 release PR

1. `publish-linux-bundle`（含 `system-python`、`container-smoke`）尚未在真正的 GitHub Actions tag push 上跑過（不能建 tag），第一次實跑會在 v0.4.0 tag。本輪的等效實跑在 Ubuntu 24.04 雲端容器中進行，與 hosted runner 的差異是 runner uid 與 docker 權限（`--user 1000:1000` 只需要 bundle 檔案可讀；tar 解開後權限為 644／755）。
2. 前輪列出的項目不變：Windows 正式 bundle 建置腳本（BO-P9-17）；release PR 內改 `VERSION`／`Cargo.toml`、正式 collate、修正 `CHANGELOG.md` 的相對連結；RELEASE-TODO-02（install.sh，OD-50）。
