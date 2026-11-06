# serialwrap 0.4.0 release 計畫（Stage 1 出口：Rust CLI＋Python daemon）

> 依據：`owner-decisions-2026-10-15.md` OD-33（2026-10-28）。本文件只做準備：**實作輪不得 merge 到 `main`、不得建 tag、不得發 GitHub release**；這些由主 session 在 Stage 1 出口 gate 通過後依政策開 PR 執行。
> 版號：Stage 1 出口＝`0.4.0`；Stage 2 出口（全 Rust daemon）＝`0.5.0`（Canary 前可用 `0.5.0-rc.N`）；Stage 3 Python daemon 退役＝`1.0.0`。

## 1. 前置條件（全部滿足才開 release PR）

| # | 條件 | 證據 |
|---|---|---|
| 1 | Stage 1 出口 gate：Stage 1A `stage1a_gate.py` overall 為 `VERIFIED`，或為 `IMPLEMENTED` 且只剩 OD-27～OD-32 明確允許的 `BLOCKED-OWNER`；Stage 1B 只剩同範圍擁有者項，沒有未登記的 FAIL／INSUFFICIENT。OD-75 已登記且平台、狀態、訊息特徵都符合的 Python daemon 基線缺陷不擋出口，證據維持 FAIL（不改判 BLOCKED-OWNER、PASS 或 SKIP），gate 另列「基線已知缺陷（不擋）」 | 出口 driver `scripts/rust/stage1_exit_driver.sh` 於最終 HEAD 的報告 |
| 2 | 全套 `python3 -m pytest -q tests/`、`cargo test --workspace`、`python3 -m tools.contracts check`、`python3 -m policy_check --repo .` 在 release HEAD 通過 | 同上 |
| 3 | Linux 升級驗證（§4）在 release 候選 bundle 上兩種監管模式皆 PASS | `scripts/docker/run-upgrade.sh` 的 `<out>/<mode>/upgrade-report.json` |
| 4 | §6 列的待擁有者裁決項已裁決（或擁有者明示接受以已知限制發佈） | `owner-decisions-2026-10-15.md` |
| 5 | release workflow 已能產出 §2 的 bundle（RELEASE-TODO-01） | release PR 的 CI |

已登記新的基線缺陷後，不必重跑出口測試，用該次證據目錄重判 gate：

```bash
python3 scripts/rust/l15_exit_driver.py --rejudge <evidence_dir>
```

命令讀取 `<evidence_dir>/driver.log` 的 `GATE_ARGS` 與 `<evidence_dir>/HEAD`，以該次受測 revision 重跑 `scripts/rust/stage1a_gate.py`，並把新報告寫到暫存檔，不改證據目錄。另讀取 `rc.txt`，只在記憶體中將唯一的 `stage1a_gate` 步驟退出碼替換為重判退出碼，再執行第二層 producer 核對（含 Windows L1.5 報告與 metadata）。尾端 `DONE` 為 driver 結束標記，不視為測試步驟。

stdout JSON 包含 `baseline_known_defects`、`baseline_pending_classification`、`rejudge_overall`、`rejudge_rc`、`failed_steps`、`unresolved`、`suppressed` 與 `exit_passed`。預設行程碼為 0 的條件是新 gate 通過且沒有未解釋的失敗步驟；新 gate 仍為 FAIL 時，命令回傳非 0。`same_judgment` 僅供比對存檔 gate（略過新增的兩個基線分類欄位），不決定預設退出碼。若只是確認空登記時與既有證據的判定相同，可用：

```bash
python3 scripts/rust/l15_exit_driver.py --rejudge <evidence_dir> --assert-same-judgment
```

此旗標以 `same_judgment` 決定退出碼；必須另外查看 `exit_passed`，不能把「相同的 FAIL」當成出口通過。缺少重判所需報告或登記損毀時仍回傳非 0。

每個 rc=1 的 producer 都須有自己的完整原始報告，且全部失敗均符合登記與 gate 的不擋列，才列入 `suppressed`。Linux L1.5 依該步驟的 cases／scenarios 宣告逐案核對 repeat；pytest 的 resource report 仍由既有 `l1_judge.py` 判定，並與 JUnit 逐列核對；A/B 必須兩輪同一個精確 nodeid 都失敗且符合相同登記。參數、類別及路徑皆保留，不能以同函式其他參數的登記代替。證據內的 FAIL 不改寫。

出口 driver 會保存 BB-proc 的 resource report 與 JUnit（`bbproc-multiagent-coexist/`、`bbproc-1b/`），並將本次 Stage 1B 的 `target/p4-evidence` 複製到 `<evidence_dir>/p4/`；目的地已存在（含符號連結）時拒絕保存並擋出口，避免混入舊報告。重判只讀該次快照，不借用後續 run 的 Stage 1B 報告。舊證據若沒有這些逐步報告，對應失敗維持擋住；只有一份 gate 核准列不足以解釋 producer 退出碼。

## 2. 發佈內容

| 產物 | 內容 | 產生方式 |
|---|---|---|
| Linux x86_64 bundle（`serialwrap-0.4.0-linux-x86_64.tar.gz`） | `bin/serialwrap`（Rust CLI，20 個群組原生）、`bin/serialwrapd`（launcher）、`versions/0.4.0/`：`release-manifest.json`、`SHA256SUMS`、`py/`（自帶 PyYAML 的 Python daemon runtime＋`sw_core`，亦為 Python 委派元件 `python_cli_delegate`）；不含 `sw-cli/test-fakes` | `scripts/rust/mkbundle.sh --release-bundle --profile release --out <dir>`（`VERSION` 已改為 0.4.0，**不得**用 `--version-override`） |
| Windows x86_64 bundle（`serialwrap-<版本>-windows-x86_64.zip`） | `bin\serialwrap.exe`、`bin\serialwrapd.exe`（launcher）、`bin\current-release.json`、`versions\<V>\`：`bin\*.exe`、`release-manifest.json`、`SHA256SUMS`、`py\`（CPython 3.12.3 embeddable＋PyYAML 6.0.3＋pyserial 3.5＋`py\sw_core`＋`py\serialwrapd.py`）；不含 `sw-cli/test-fakes`、**不含 com0com**、**不簽署**（OD-78） | OD-78：GitHub hosted `windows-latest` 由 `release.yml` 的 `publish-windows-bundle` job 建置（`needs: publish-linux-bundle`；`scripts/rust/mkbundle-windows.ps1` 組裝、`scripts/rust/smoke-windows-bundle.ps1` 解開後 smoke、合併 `SHA256SUMS` 後上傳）；不綁 0.4.1，可用 `workflow_dispatch`（輸入 tag）對既有 tag 補傳。PyInstaller exe 續發 |
| Python wheel（`serialwrap-0.4.0-py3-none-any.whl`） | Python CLI＋daemon（照發；OD-50 後標準安裝流程不使用，`install.sh` 不再走 pipx） | 既有 `release.yml` 的 `python3 -m build --wheel` |
| `install.sh` | OD-50：Linux 安裝 Rust bundle。預設從 GitHub release 下載 `serialwrap-<版本>-linux-x86_64.tar.gz` 與 `SHA256SUMS` 並驗證（未給 `--version` 取 `releases/latest`），或以 `--bundle <目錄或 tar.gz>` 離線安裝；解到暫存目錄後執行 `<bundle>/bin/serialwrap setup` 並回傳其 rc。pipx 路徑退役 | repo 根目錄的 `install.sh`（`tests/test_install_sh.py`）；release 須附上 Linux bundle tarball 與含該列的 `SHA256SUMS`（RELEASE-TODO-01） |

**RELEASE-TODO-01（release workflow）**：原 `.github/workflows/release.yml` 只發佈 wheel 與 PyInstaller exe，沒有 Rust bundle job。release PR 必須新增 Linux bundle job（cargo build＋`mkbundle.sh --release-bundle --profile release`＋打包＋上傳＋`SHA256SUMS`）並登記 Windows bundle 的上傳方式；PyInstaller exe 是否續發由擁有者決定（建議 0.4.0 仍附上，供尚未改用 bundle 的 Windows 使用者）。

- **Linux（已備，`feature/release-0.4.0-prep`）**：`release.yml` 新增 `publish-linux-bundle` job（tag push 觸發、`needs: publish`、`runs-on: ubuntu-24.04`；既有 `publish`／`publish-windows-exe` 不動）。步驟依序：tag 與 `VERSION`、`Cargo.toml` `[workspace.package] version` 三者一致檢查（不一致即 `::error::` 失敗）→ 以系統 `/usr/bin/python3.12` 建 build venv（apt 安裝 `python3.12-venv`；版本須等於 `contracts/cli_help/_meta.json` 的 `python_version`；venv 內裝 `pyyaml==6.0.3`、`jsonschema==4.10.3` 並置於 PATH 首位），使 bundle runtime 的 `pyvenv.cfg` 為 `home = /usr/bin`（OD-50；**不得**用 `actions/setup-python`：hosted toolcache Python 建的 runtime 依賴 `/opt/hostedtoolcache` 的 libpython 與標準函式庫，搬到使用者主機即無法啟動，S1-3185c6d-1）→ `cargo build --release --workspace --locked` → `scripts/rust/mkbundle.sh --release-bundle --profile release --out "$RUNNER_TEMP/bundle/serialwrap-${VERSION}-linux-x86_64"` → `tar` 打包為 `dist/serialwrap-${VERSION}-linux-x86_64.tar.gz`（保留 `bin/` 相對 symlink、排除 `.mkbundle-owner`）並產生 `dist/SHA256SUMS` → 解開後 smoke（外層 `sha256sum -c`、`bin/serialwrap --version` 等於 `serialwrap ${VERSION}`、`bin/serialwrap --help`、`versions/${VERSION}/release-manifest.json` 存在且 `bundle_version`／cli／daemon／pairing 版本一致且非 sentinel、內層 `SHA256SUMS` 核對、`versions/${VERSION}/bin` 不含 `SERIALWRAP_TEST_`、`SW_TEST_UPDATE_PAUSE_AFTER`、`SW_TEST_UPDATE_FORCE_VERIFY_FAIL` 測試鉤子字串、不含 delegate sentinel、`versions/${VERSION}/py/pyvenv.cfg` 含 `home = /usr/bin` 與 `include-system-site-packages = false`）→ 容器 smoke（以 OD-16 (5) 同一 digest 的 `ubuntu:24.04` 只加裝 `python3.12`——無 PyYAML、無 libpython 共享庫、無 toolcache——`docker run --network none`，只唯讀掛載上一步解開的實際 tarball 內容；容器內以 bundle runtime `-I -B` 匯入 `sw_core.daemon` 與 `yaml`，再以 bundle 的 `bin/serialwrap daemon start`／`daemon status`／`daemon stop` 啟停 on-demand daemon（獨立暫存 config／state／run／WAL 目錄、空 by-id 目錄、不碰裝置），status 須 `ok`、`running` 且版本等於 `VERSION`，stop 後行程須結束；任一失敗即阻擋上傳）→ `gh release upload "$TAG_NAME" dist/serialwrap-${VERSION}-linux-x86_64.tar.gz dist/SHA256SUMS --clobber`。結構與步驟行為由 `tests/test_release_workflow.py` 守護。
- **Windows（OD-78，hosted `windows-latest`，已備於 `feature/windows-release-bundle`）**：`release.yml` 新增 `publish-windows-bundle` job（`needs: publish-linux-bundle`，`runs-on: windows-latest`，釘 `1.96.1-x86_64-pc-windows-gnu` host toolchain，與 `rust.yml` 的 MinGW 探測／安裝步驟相同）。取代原「擁有者 runner 原生建置」的做法；擁有者仍需做一次 Windows 原生驗收（見 §4.2）。步驟依序：關閉 `core.autocrlf` → checkout（`workflow_dispatch` 補傳時另以 `refs/tags/<tag>` checkout 原始碼到 `release-src/`，腳本取自觸發 ref）→ tag／`VERSION`／`Cargo.toml` 一致檢查 → `setup-python` 3.12.3（建置機，裝 `jsonschema==4.10.3`）→ Rust 與 MinGW → `scripts/rust/mkbundle-windows.ps1`（下載 python.org CPython 3.12.3 embeddable 並核對 `scripts/rust/win-python-3.12.3.sha256`；`python312._pth` 只寫相對路徑 `python312.zip`、`.`、`Lib\site-packages`，bundle 可搬移；以 `scripts/rust/win-bundle-requirements.txt`（`--require-hashes`）安裝 cp312 win_amd64 的 PyYAML／pyserial wheel；`sw_core` 放 `py\sw_core`——Rust `install_layout` 固定讀此處；比照 `mkbundle.sh` 在 cargo 期間蓋章 `embedded_code_fingerprint.bin` 並還原；`cargo build --release --locked -p sw-cli -p sw-daemon` 不含 test-fakes；產生 `release-manifest.json`（`target = x86_64-pc-windows-gnu`，runtime `py/python.exe`）與內層 `SHA256SUMS`，以 `contracts/release_manifest.schema.json` 驗證）→ `check_win_imports.py`（兩個 exe）→ `smoke-windows-bundle.ps1`（解開實際要發佈的 zip：`--version` 為 `serialwrap <V> (code <12 hex>)`、`--help`、內層 `SHA256SUMS` 逐項、manifest 版本、`._pth` 無絕對路徑、無測試鉤子字串、yaml／serial／sw_core 皆由 bundle 載入；throwaway 根下 `versions\<V>\bin\serialwrap.exe setup --on-demand`、`doctor`、`daemon start`／`status`／`stop`，`SERIALWRAP_TEST_DEVICE_ALLOWLIST=COM999` 不開任何 COM 埠）→ 取回 Linux job 的 `SHA256SUMS`（缺 Linux 列即失敗）→ 刪舊 Windows 列、附新列（LF、兩空白）→ `gh release upload ... --clobber` → 重新下載核對（`sha256sum -c --ignore-missing`）。其餘 job（`project-policy`、`publish`、`publish-windows-exe`、`publish-linux-bundle`）加 `if: github.event_name == 'push'`，`workflow_dispatch` 只會跑 Windows bundle job。兩個 job 都改寫同一個 `SHA256SUMS`，由 `needs` 序列化，重跑 Linux job 後須重跑 Windows job 補回 Windows 列。結構與上傳步驟行為由 `tests/test_release_workflow.py` 守護。
  - 使用者安裝：解 zip 後須由 `versions\<V>\bin\serialwrap.exe setup`（與 `release-manifest.json` 同層的那份）執行；zip 頂層 `bin\` 的副本只是穩定入口，`setup` 不會從它安裝。安裝根預設 `%LOCALAPPDATA%\Programs\serialwrap`。
  - 已知限制：`doctor` 的 `health.status` 探測逾時只有 0.5 秒，Windows 上 daemon 的行程掃描較慢時（實測 `daemon status` 約 3 秒）`version_pairing` 會只回報 `daemon_unreachable`；其他配對欄位仍會檢查。屬既有行為（Stage 1 Python daemon 凍結），不在本項修改。

**RELEASE-TODO-02（install.sh）**：已依擁有者裁決 OD-50（2026-10-29，(b')）決定並實作（分支 `feature/install-sh-bundle`）。新流程：`install.sh [--version X.Y.Z] [--bundle <目錄或 .tar.gz>] [--] [setup 參數...]`——安裝前檢查系統需求（x86_64、glibc ≥ 2.39、`/usr/bin/python3.12`；Linux 使用者皆為 Ubuntu 24.04 以上），任一不符 rc=2 且不改動任何東西；未給 `--bundle` 時以 curl（無 curl 用 wget）從 GitHub release 下載 bundle tarball 與 `SHA256SUMS`，以該 tarball 的列驗證，雜湊不符或缺列立即失敗、不執行 setup；解到 mktemp 暫存目錄（結束時清除）後執行 `<bundle>/bin/serialwrap setup "$@"` 並回傳其 rc；setup 成功後偵測到舊 pipx 安裝（`pipx list --short` 含 serialwrap 或 pipx venv 目錄存在）時提示可 `pipx uninstall serialwrap`，不自動移除。pipx 安裝路徑退役（README 主要安裝方式一併改寫）；wheel 仍照發但標準流程不使用。下載基底 URL 可由 `SERIALWRAP_RELEASE_BASE_URL` 覆寫（測試用）。

## 3. 版號與 changelog 步驟（release PR 內執行）

1. `VERSION`：`0.3.2` → `0.4.0`；`Cargo.toml` 的 `[workspace.package] version` 同步改為 `0.4.0` 並更新 `Cargo.lock`（`cargo update -w`）。`pyproject.toml` 的 dynamic version 由 `VERSION` 讀取。
2. 收斂 changelog：`python3 -m policy_check.changelog collate --version 0.4.0 --date <發佈日 YYYY-MM-DD>`（把 `changelog.d/*.md` 依 type 收斂成 `CHANGELOG.md` 的 dated 段並清空目錄）。collate 前先修正缺 frontmatter 的既存 fragment（第 3 輪報告列為範圍外的 5 個壞檔），否則分類會錯。
   - 2026-10-29 試跑（暫存 clone、`--date 2026-11-01`，結果未提交）：原始 217 個 fragment 中 5 個缺 frontmatter（`229-late-unlock-prompt.md`、`rust-p4-win-test-l1-set.md`、`st06-command-completion.md`、`st06-smart4-prompt-window.md`、`st06-smart4r-console-unlock.md`），collate 於第一個壞檔即 `FragmentError` 中止且不改任何檔。`feature/release-0.4.0-prep` 已只補 frontmatter／格式（`st06-smart4r-console-unlock.md` 同時含 Added 與 Fixed，依原條目拆為 `-a-profile`／`-b-recovery`／`-c-sysrq-reboot`（feat）與 `-d-prompt-boundary`（fix）四檔，文字不變）；重跑 collate 成功收斂 220 個 fragment（Added 73、Changed 39、Fixed 106、Security 2）。release PR 執行前若再有新 fragment，仍須重跑本步驟確認。
   - 2026-11-05 於 `feature/s1m-rel040`（起點 `038d8eba`）以 `--version 0.4.0 --date 2026-11-06` 正式收斂：另有 2 個 fragment 缺 frontmatter（`s1m-x6w-windows-passwd-fixtures.md`、`s1mx6b2-user-hold-root.md`），只補 `type: fix` 與原文句子。收斂 289 個 fragment（Added 85、Changed 46、Fixed 156、Security 2），目錄清空，空的 `[Unreleased]` 保留。
3. 重新產生 bundle（§2）並重跑 §4 升級驗證，證據綁 release HEAD。
4. `python3 -m policy_check --repo .`、全套測試通過。

## 4. 升級驗證（OD-33 R2(b)）

### 4.1 Linux（實作完成，可重複執行）

```bash
scripts/docker/run-upgrade.sh                    # 兩種模式；預設由 git tag v0.3.2 建 wheel、mkbundle 建不含測試鉤子的 0.4.0 release 候選
scripts/docker/run-upgrade.sh --mode systemd-user --bundle <release bundle 目錄> --wheel <0.3.2 發佈 wheel>
```

- 容器：`tools/docker/upgrade.Dockerfile`（OD-16 (5) 的 ubuntu:24.04 digest），`--network none`、不帶任何裝置，wheel／bundle／driver 唯讀 bind mount；每種監管模式一個全新容器。不碰本機正式安裝。
- 0.3.2 來源：容器不連網，預設以 `git archive v0.3.2` 建 wheel；正式驗證時改用 `--wheel` 指向 GitHub release 上的 0.3.2 wheel。
- wheelhouse 固定下載 CPython 3.12、`cp312`、`manylinux_2_17_x86_64` 的 PyYAML wheel；映像建置與容器執行固定 `linux/amd64`，主機 `python3` 為其他版本時仍使用相同目標。
- 來源完整性：`run-upgrade.sh`（含重用 `--bundle`）與 `mkbundle.sh --release-bundle` 要求乾淨 Git 工作樹（含未追蹤來源），不符即 rc=3；建置／驗證後再次核對乾淨狀態與 HEAD。`--dry-run` 不產證據、`mkbundle.sh --dev` 供開發回歸使用，不受此限制。報告分別記錄 bundle 的版本與 manifest 雜湊，以及 `driver_git_revision`、容器實際載入的 `run_upgrade_container.py`／`upgrade_checks.py` 各自的 `driver_sha256`；雜湊鍵只含檔名，不記宿主路徑。
- 0.4.0 bundle（release 前置第 2 輪 OWNER-BATCH #2）：未給 `--bundle` 時比照 release workflow 的 `publish-linux-bundle`，先確認主機 `/usr/bin/python3.12` 可執行、實體位於 `/usr/bin` 且版本等於 `contracts/cli_help/_meta.json` 的 `python_version`，以它建 build venv（`pyyaml==6.0.3`、`jsonschema==4.10.3`）並置於 PATH 首位執行 `mkbundle.sh --release-bundle --profile release`；任一不符即 rc=3 fail-closed，在任何建置、下載或 docker 動作之前結束，不回退 PATH 上的其他 python。自建或 `--bundle` 提供的 bundle 都必須 `versions/0.4.0/py/pyvenv.cfg` 含 `home = /usr/bin` 與 `include-system-site-packages = false`，否則 rc=3、不建映像。`--scenario install-sh` 只支援 systemd-user／on-demand，與 `--mode systemd-system` 併用時 rc=2。
- 停止後等待（Docker on-demand 預熱競態裁決，2026-10-29）：0.3.2 的 `daemon.stop` 先回應、先刪 socket，行程稍後才結束；在這段期間 `daemon start` 會回 `DAEMON_ALREADY_RUNNING_ELSEWHERE`，之後沒有 daemon。driver 在每個 `daemon stop` 之後、下一個 start／setup／`install.sh` 之前（基線預熱、停舊後 setup、install.sh 前、手動降級、重新升級），先輪詢停止前快照的 daemon 行程樹（含子孫，以啟動時間辨識 PID 重用）已結束、沒有任何 serialwrapd 實例、且其 socket（argv `--socket` 與 config.yaml `socket_path`）已移除，上限 60 秒。逾時略過後續的 start／setup，並讓該步驟依原判定判 FAIL。等待結果寫進該步驟判定列的 `detail.stop_wait`。
- 判定：`scripts/docker/upgrade_checks.py`（`tests/test_docker_upgrade.py`）。19 項必要檢查，任一 FAIL 即 FAIL、缺項即 INSUFFICIENT：
  0.3.2 安裝與啟動、建立 profile／alias／binding 基線、0.3.2 doctor、舊 daemon 執行中直接 setup（`upgrade_with_old_daemon_running`：成功切換；拒絕只對「非 legacy 的不支援交接 daemon」以 `METHOD_NOT_FOUND`＋現場未改動判定，0.3.x daemon 被拒絕即 FAIL）、`legacy_handoff_direct_setup`（OD-48：0.3.2 daemon 執行中直接 setup 必須成功切換、報告含 `legacy_stop`、舊 daemon 行程樹結束、新 daemon 由 setup 自己啟動——在 driver 任何啟動動作之前取樣，setup 沒啟動即 FAIL、config／profiles 逐位元組保留、setup 期間無兩個 serialwrapd 同時存在）、停舊後 setup（直接 setup 成功時記為不需要；「先停再升級」路徑與 OBS-UPGRADE-NOT-STARTED 保留於重新升級階段）、`~/.local/bin` 入口取代且備份記下 pipx 入口、config／profiles 逐位元組保留、0.4.0 daemon 由 bundle 啟動、監管模式依 config 收斂（systemd-user：unit active＋enabled、MainPID 為新 daemon、無殘留 hold drop-in；on-demand：unit 未啟用）、alias／binding 語意相容、0.4.0 doctor（非 advisory 全綠＋`version_pairing`；advisory 只容許容器內必然為 0 的 `devices`）、`setup --rollback`（拒絕時現場不得改動）、手動降級回 0.3.2（版本、config、狀態、doctor）、降級後重新升級、全程 /proc 20 ms 取樣不得同時出現兩個 serialwrapd 實例。
- 首次實跑（2026-10-28，bundle git revision `2a947f6`）：systemd-user 與 on-demand 皆 **PASS**（各約 2,800 筆取樣、0 次重疊）；報告未保存，不作為證據。
- 本輪驗證（起點 `9908f93`，工作樹含 BB-1 修正）：`python3 -m pytest -q tests/test_docker_upgrade.py` 29 passed，含 systemd-user 與 on-demand 兩模式的完整 0.3.2→0.4.0→降級→再升級實跑並各判 PASS；Docker 報告留在 pytest 暫存目錄，未保存進 repo，不能替代最終 HEAD 的出口證據。
- 判準收緊後實跑（2026-10-28，run `s1v14-c8df563`，bundle git revision `c8df563`）：兩模式皆 **PASS**（取樣 2641／2705、0 次重疊）；報告原文與 `SHA256SUMS` 保存於 `evidence/stage1/upgrade-docker/s1v14-c8df563/`。此為開發中 bundle，不取代前置條件 #3 的 release bundle 重跑。觀察項見 §6。
- OD-48 實作後實跑（2026-10-29，run `od48run2`，bundle git revision `014eb2b`，分支 `feature/od48-legacy-handoff`；雲端環境以 HTTPS apt 來源建映像、其餘 driver／判定／`--network none` 不變）：兩模式皆 **PASS**，19 項含 `legacy_handoff_direct_setup`（systemd-user：`systemctl_stop`；on-demand：`rpc_daemon_stop`；兩者 `waited_ms` 約 5.2 s、新 daemon 由 setup 啟動、取樣 1576／1531、0 次重疊）；報告與 `SHA256SUMS` 保存於 `evidence/stage1/upgrade-docker/od48-014eb2b/`。同為開發中 bundle，不取代前置條件 #3。
- OD-48 第 2 輪實跑（2026-10-29，bundle git revision `2f8f7ed`，`--release-bundle --profile release --version-override 0.4.0`；建映像方式同上）：systemd-user、on-demand 完整流程各 19 項 **PASS**，另以 `--mode systemd-system` 驗「0.3.2 system daemon 執行中直接 `setup --system --with-sudo`」成功路徑 13 項 **PASS**（`systemctl_stop`、`waited_ms` 約 5.2 s、新 daemon 由 setup 啟動、MainPID 為新 daemon、無殘留 drop-in、取樣 1602、0 次重疊）。systemd-system 觀察項：`OBS-0.3.2-SYSTEM-UMASK`（0.3.2 system unit `UMask=0117` 使 daemon 自建目錄缺 x 位元，driver 先以一般 umask 暖機一次 0.3.2 daemon）、`OBS-SYSTEM-PROFILES-MANAGED`（system setup 以套件 profiles 覆寫 `/etc/serialwrap/profiles` 同名檔，0.3.2 同語意）。證據於 `evidence/stage1/upgrade-docker/od48-2f8f7ed/`；同為開發中 bundle，不取代前置條件 #3。
- OD-48 第 2 輪 sol 修正後重跑（bundle git revision `55cfa03`）：三模式皆 **PASS**（19／19／13 項；`waited_ms` 5351／5214／5311、取樣 1476／1562／1623、0 次重疊）。systemd-system 的套件管理 profile 改為全數保留於快照、只允許覆寫成新 bundle 權威內容（S1-f3c66b4-1）：`/etc/serialwrap/profiles/default.yaml` 記為 `package_updated`。證據於 `evidence/stage1/upgrade-docker/od48-55cfa03/`（`od48-2f8f7ed` 的 systemd-system 判定已被此輪取代）。
- install.sh 第 2 輪（合入 OD-48 與 release 前置後，2026-10-29，bundle git revision `407ed7e`，bundle 以 `/usr/bin/python3.12` build venv 自建、`py/pyvenv.cfg` 為 `home = /usr/bin`；雲端容器的 apt 以原 HTTP 來源即可建映像，未改來源）：`--mode all` 的 systemd-user、on-demand 完整流程各 19 項 **PASS**（`legacy_stop` 為 `systemctl_stop`／`rpc_daemon_stop`，`waited_ms` 5265／5416，取樣 1522／1590、0 次重疊）；`--mode systemd-system`（`--bundle` 重用同一 bundle，走 pyvenv.cfg 檢查）13 項 **PASS**（`systemctl_stop`、5173 ms、取樣 1573、0 次重疊，`default.yaml` 列 `package_updated`）；`--scenario install-sh` 兩模式各 12 項 **PASS**（取樣 895／951、0 次重疊，`OBS-UPGRADE-NOT-STARTED` 同 §5.1）。五份報告的 `bundle_manifest_sha256`、wheel 雜湊相同；證據與 `SHA256SUMS`、過濾後的 `bundle-pyvenv.txt` 於 `evidence/stage1/upgrade-docker/install-sh-407ed7e/`。同為開發中 bundle（`--version-override 0.4.0`），不取代前置條件 #3。
- 預熱競態修正後重跑（bundle git revision `a20de1d`，driver 在 stop 後等舊行程樹與 socket 消失）：
  - `--mode all`：systemd-user、on-demand 各 19 項 **PASS**。
  - `--mode on-demand` 另以同一 bundle 再跑 2 次，合計連跑 3 次皆 19 項 **PASS**：
    - `seed_user_data` 的 `stop_wait.waited_ms` 三次皆為 100。0.3.2 `daemon stop` 回應後，舊行程約 0.1 秒才結束，正是原先競態的窗口。
    - 降級等待約 300 ms，重新升級等待約 200～300 ms。
    - `legacy_stop` 為 `rpc_daemon_stop`，取樣約 1440、0 次重疊。
  - `--mode systemd-system` 13 項 **PASS**。
  - `--scenario install-sh` 兩模式各 12 項 **PASS**。
  - 7 份報告的 `bundle_manifest_sha256` 與 wheel 雜湊相同。證據於 `evidence/stage1/upgrade-docker/install-sh-a20de1d/`，含 `on-demand-run2`／`on-demand-run3`；driver 修正前的判定由 `install-sh-407ed7e` 取代。
  - 同為開發中 bundle，不取代前置條件 #3。
- REL-040-r2 release 候選重跑（2026-11-05，分支 `feature/s1m-rel040`，來源與 bundle revision `29772118e099a972b38169c816f57e57e27ff9d9`，run `rel040-29772118`）：由 `run-upgrade.sh` 以 `/usr/bin/python3.12` 3.12.3 建立同一份 `--release-bundle --profile release --version-override 0.4.0` bundle，三模式依序以前景執行；沒有測試失敗，故未重跑。三份報告的 `bundle_manifest_sha256` 均為 `07c073a0805a1c83554a7d7d7b2b4c18eefbcead9fd46fb4bbf531f2ce122332`，0.3.2 wheel 雜湊均為 `ba7eb2a93afea17305a3a3f974548da7e824ce3de53a8432c43d88a2367208ef`。runtime `pyvenv.cfg` 為 `home = /usr/bin`、`include-system-site-packages = false`；內層 `SHA256SUMS` 1,196 項全數通過，release `bin/serialwrap`／`bin/serialwrapd` 未找到 `SERIALWRAP_TEST_`、`SW_TEST_UPDATE_PAUSE_AFTER`、`SW_TEST_UPDATE_FORCE_VERIFY_FAIL` 標記。
  - systemd-user：19/19 **PASS**；legacy handoff 使用 `systemctl_stop`、等待 5,178 ms；1,158 次 `no_two_reader` 取樣、0 次重疊。
  - on-demand：19/19 **PASS**；legacy handoff 使用 `rpc_daemon_stop`、等待 5,446 ms；1,174 次取樣、0 次重疊。
  - systemd-system：計畫限定的 direct-setup 範圍 13/13 **PASS**；legacy handoff 使用 `systemctl_stop`、等待 5,073 ms；1,344 次取樣、0 次重疊。`legacy_handoff_direct_setup` 三模式均確認舊行程樹結束且新 0.4.0 daemon 由 setup 啟動。
  - 證據（3 份原始 `upgrade-report.json`、過濾後的 `bundle-pyvenv.txt`、`SHA256SUMS`）位於 `evidence/stage1/upgrade-docker/rel040-29772118/`；`test_upgrade_evidence_only_container_home_paths` 1 passed。§6 補列本輪觀察到的既有 systemd-system 行為。

### 4.2 Windows（LOCAL-WINDOWS-TODO）

0.3.2 在 Windows 的正式安裝為 PyInstaller one-file exe（或 pip 安裝）；0.4.0 為 bundle 安裝到 `%LOCALAPPDATA%\Programs\serialwrap`。升級驗證需要：throwaway 隔離根（`SERIALWRAP_INSTALL_ROOT`、隔離的 config／state／run 目錄、device allowlist 只有 COM20）、windows-task 監管的 0.3.2→0.4.0 交接。OD-28 的排程工作授權時機在 P8 Rust daemon bundle 完成後（plan 09 Task 13），因此本項列 **LOCAL-WINDOWS-TODO**（OD-78 之後仍由擁有者以 hosted job 產出的 zip 做一次原生驗收；hosted 與 WSL 交叉編譯的結果不能代替）：on-demand 模式的 Windows 升級可在 OD-28 範圍內先以 throwaway 隔離根實跑；windows-task 模式待 OD-28 時機。不得以 Linux 結果代替。

### 4.3 既有正式安裝機（OD-27）

release 後於已安裝 0.3.2 的其他電腦（含接有真板者）以 release 產物實際升級，並在該機發起 OD-27 的真板全套。屬擁有者項目，維持 BLOCKED-OWNER。

## 5. 使用者升級與降級程序（release notes 用）

### 5.1 0.3.2（pipx）→ 0.4.0（bundle），Linux

1. 不需先停 0.3.2 daemon（OD-48）：0.4.0 `setup` 偵測到 0.3.x daemon（`daemon.handoff_stop` 回 `METHOD_NOT_FOUND`、版本低於 0.4.0、行程正向辨識為 pipx 的 Python daemon）時，會先確認沒有 flash／前景命令／interactive／背景命令等忙碌狀態、沒有其他同使用者的 serialwrapd、沒有外部監管者，再以原監管手段停止它（systemd unit 的 MainPID → `systemctl stop`，否則 RPC `daemon.stop`；不硬殺），等舊行程樹完全結束、UART 不再被持有並穩定 5 秒後才切換，切換後由 setup 啟動 0.4.0 daemon。忙碌時拒絕且不送任何 stop，請待其完成後重試。
   - 舊 daemon 已停止後若後續步驟失敗，setup 會把安裝狀態（入口、config、unit 註冊與 enabled 狀態、current）還原到升級前，但**不會自動重啟舊 daemon**；回應附 `legacy_daemon_stopped`、`restored` 與啟動指令（OD-48 修訂）。舊 daemon 在 330 秒內未結束時回 `legacy_stop_pending`、保留 journal，待其結束後再執行一次 `setup`。還原本身失敗時回 `revert_pending`、保留 journal，並以 `unrestored` 列出未還原的項目（`entries`／`config`／`unit`／`enabled`／`current`）。
   - 舊 daemon 由 serialwrap 以外的監管者管理（Linux：daemon 位於別的 systemd service unit，依 `/proc/<pid>/cgroup` 判定；`*.scope` 結尾一律視為一般 session，不因位於 `system.slice` 或 `user.slice` 而判為外部監管者；或父行程為 supervisord、NSSM、Task Scheduler 等）時，setup 在送出任何 stop 之前以 `DEVICE_STILL_HELD`（`external_supervisor`）拒絕，請先停用該監管者再重試。
   - 舊 daemon 實際由 systemd-user／systemd-system 其中一種 unit 監管，但 `config.yaml` 的 `supervision_mode` 記的是另一種時，setup 停止前以 `DEVICE_STILL_HELD`（`supervisor_mode_mismatch`）拒絕；請以 `setup --user` 或 `setup --system --with-sudo` 指定實際模式後重試（OD-48 第 2 輪編排者裁決：維持 fail-closed，不另取第二個模式的 hold）。
   - 也可以照舊先 `serialwrap daemon stop`（systemd 模式會自動轉為 `service stop`）再升級；此時 setup 不會代為啟動新 daemon（見 OBS-UPGRADE-NOT-STARTED），請依步驟 3 啟動。
2. 解開 bundle，執行 `<bundle>/bin/serialwrap setup` 並**明確指定目前的監管模式**（`--user`／`--system --with-sudo`／`--on-demand`）。不加旗標時與 0.3.2 相同，會自動選擇（有 systemd → systemd-user，否則 on-demand），可能改變現有模式與 `config.yaml`（S1X-c-2）。`~/.local/bin/serialwrap`、`serialwrapd` 改指 `~/.local/share/serialwrap/versions/0.4.0/bin/`，原 pipx 入口記在 `~/.local/share/serialwrap/backups/none-to-0.4.0-*/entries.json`。以明確旗標沿用原模式時 config、profiles、state.json 不變。
3. 只在步驟 1 先手動停止 daemon 時才需要：systemd 模式 `serialwrap service start`；on-demand `serialwrap daemon start`。
4. `serialwrap doctor`：`version_pairing` 必須通過。
5. pipx 的 0.3.2 venv 仍保留（不再被入口使用），確認無誤後可 `pipx uninstall serialwrap`（注意：uninstall 不會移除指向 bundle 的入口）。

### 5.2 降級回 0.3.2（Linux）

`serialwrap setup --rollback` 目前**不能**回到 pipx 的 0.3.2（首次 bundle 安裝沒有 Previous，回 `INVALID_ARGS`／`rollback_unavailable` 且不改動現場，見 OBS-UPGRADE-ROLLBACK-PIPX）。手動程序（已由 §4.1 實跑驗證）：

1. `serialwrap daemon stop`
2. 移除指向 bundle 版本目錄的入口：`~/.local/bin/serialwrap`、`~/.local/bin/serialwrapd`（只在它們是指向 `~/.local/share/serialwrap/versions/` 的 symlink 時刪除）。
3. `pipx uninstall serialwrap && pipx install serialwrap==0.3.2`（或 0.3.2 wheel）。只做 `pipx install --force` **不會**重建入口，`serialwrap` 仍是 0.4.0（實測）。
4. `serialwrap setup`，再 `serialwrap service start`（或 on-demand `serialwrap daemon start`；0.3.2 在 pipx 安裝下 on-demand `daemon start` 有既有缺陷，見 OBS-0.3.2-ONDEMAND-START，可改直接背景執行 `serialwrapd`）。

## 6. 升級驗證發現（照實回報）

| ID | 現象 | 影響 | 處置 |
|---|---|---|---|
| OBS-UPGRADE-LEGACY-HANDOFF | 0.3.2 daemon 執行中時 0.4.0 `setup` 回 `METHOD_NOT_FOUND`（`daemon.handoff_stop` 不存在）拒絕 | 使用者必須先手動停 daemon | **已依 OD-48（含修訂）解決**（SC-39，分支 `feature/od48-legacy-handoff`）：判定為 0.3.x Python daemon 時改走舊版停止路徑，確認舊行程樹結束、UART 釋放並穩定 5 秒後才切換，成功後由 setup 啟動新 daemon；失敗還原安裝狀態但不自動重啟舊 daemon。Docker 檢查新增 `legacy_handoff_direct_setup`；`REFUSAL_LEGACY_HANDOFF` 只用於非 legacy 的不支援交接 daemon |
| OBS-UPGRADE-ROLLBACK-PIPX | `setup --rollback` 無法回到 pip／pipx 安裝的 0.3.2 | 降級需 §5.2 手動程序 | **已知限制（OD-49）**：不實作 pre-bundle 還原；release notes 寫明「不支援降級回 0.3.2」，§5.2 手動程序保留為參考、不另投入驗證 |
| OBS-UPGRADE-NOT-STARTED | 使用者先停 daemon 再 setup 時，setup 的 `start_and_verify` 為 `daemon_was_not_running`、不啟動新 daemon | 需手動 `service start`／`daemon start` | 規格行為（維持原執行狀態），release notes 明寫 |
| OBS-UPGRADE-STALE-CURRENT | 手動降級後 `current-release.json` 仍記 0.4.0；重新升級被記為 `0.4.0→0.4.0` update、Previous 指向 0.4.0 本身 | 重新升級本身成功；但之後的 `setup --rollback` 不會回到 0.3.2 | **已知限制（OD-49）**，與 OBS-UPGRADE-ROLLBACK-PIPX 一併列入 release notes；殘留 current 的 0.3.2 daemon 由 OD-48 的 `op=update` 路徑同樣處理 |
| OBS-0.3.2-ONDEMAND-START | 0.3.2 在 pipx 安裝下 on-demand `daemon start` 必然 `DAEMON_EXITED`（以 site-packages 上一層的 `serialwrapd.py` 啟動，wheel 不含該檔） | 0.3.2 既有缺陷，0.4.0 以 DF-05 定位順序（manifest launcher）修正 | release notes 記為「0.4.0 修正」；升級測試以 pipx 的 `serialwrapd` 入口背景啟動 0.3.2 並記為觀察項 |
| OBS-0.3.2-SYSTEM-UMASK | 0.3.2 system unit 的 `UMask=0117` 使 daemon 自建目錄缺少搜尋權限，可能觸發 `ensure_runtime_dirs`／`ttyMCU` `PermissionError`；本輪 warm-up 的 `warmup_rc=124` 照報告保留 | 舊版 systemd-system 安裝需先建立 runtime 目錄 | 0.3.2 既有行為；driver 以 swtest 一般 umask 暖機。本輪 systemd-system 的 13 項必要檢查仍逐項記為 PASS，未把 warm-up 退出碼改判為檢查結果 |
| OBS-SYSTEM-PROFILES-MANAGED | system setup 會以套件 profiles 覆寫 `/etc/serialwrap/profiles` 同名檔，與 0.3.2 語意相同；本輪 `default.yaml` 記為 `package_updated` | system 管理的 profile 以新 bundle 內容為準 | 依 OD-48 後續判定保留完整快照，只允許套件權威內容更新；本輪記為既有行為，未改產品碼 |

2026-11-05 REL-040-r2 三模式 release 候選重跑沒有意外的 `FAIL`／`INSUFFICIENT`，也沒有觸發同條件重跑；本表保留的觀察與 OD-49 已知限制照原狀記錄，未更改判定。

## 7. release notes 草稿

```markdown
## serialwrap 0.4.0

Stage 1 of the Rust rewrite: the `serialwrap` CLI is now a native Rust binary (all 20 command groups),
while the daemon remains the Python implementation shipped inside the bundle.

### 繁體中文
- `serialwrap` CLI 改為 Rust 原生實作（20 個命令群組），daemon 仍為 bundle 內附的 Python 實作（Stage 1）。
- CLI 反應變快：`daemon status` 往返 p50，Rust CLI 約 16 ms，同版本原始碼的 Python CLI 約 155 ms，快約 10 倍（p95 約 20 對 220 ms；最快一次約 8 對 86 ms）。兩邊連同一個 Python daemon，Rust 用 release build，各 600 個樣本，在 WSL2 開發機（24 執行緒）高背景負載下量測，絕對值只供參考，請看倍數；另一輪同樣條件的 p50 約 9 倍。這不是標準量測環境，Windows 尚未量測。這些數字只涵蓋 CLI，WAL 與 console 延遲要到 Rust daemon（Stage 2）才能比較。量測腳本見 skill 的「CLI 效能比較」一節。
- `install.sh` 改為安裝 Rust bundle（OD-50）：下載 GitHub release 的 `serialwrap-0.4.0-linux-x86_64.tar.gz` 並以 `SHA256SUMS` 驗證後執行 `bin/serialwrap setup`，或以 `--bundle` 離線安裝；系統需求為 x86_64、glibc ≥ 2.39（Ubuntu 24.04 以上）與 `/usr/bin/python3.12`。pipx 安裝路徑退役（wheel 仍照發）。
- 新增發佈組合（bundle）安裝：`bin/serialwrap setup` 安裝到 `~/.local/share/serialwrap/versions/<版本>`（Windows：`%LOCALAPPDATA%\Programs\serialwrap`），支援原子更新、`setup --rollback` 與 `doctor` 的 `version_pairing` 檢查。
- Windows：`windows-task` 監管（免管理員）、可選 com0com 虛擬序列埠（`setup --with-com0com`）。
- 修正：pipx 安裝下 on-demand `daemon start` 找不到 `serialwrapd.py`。
- 升級注意：從 0.3.2 升級時 0.3.2 daemon 可以維持執行；`setup` 會在確認閒置後停止舊 daemon，等它完全結束才切換，再啟動 0.4.0 daemon。舊 daemon 停止後若升級失敗，安裝會還原但舊 daemon 不會自動重啟，請依回應中的啟動指令手動啟動。
- 升級時請**明確指定目前的監管模式**（`--user`／`--system --with-sudo`／`--on-demand`）；不加旗標時與 0.3.2 相同會自動選擇，可能改變模式與 `config.yaml`。
- 不支援以降級功能回到 0.3.2：`setup --rollback` 不能回到 pipx 的 0.3.2（沒有上一版 bundle 時回 `INVALID_ARGS`／`rollback_unavailable`，且不改動現場）。手動步驟見本文件 §5.2，不是受支援的降級路徑。
- 行為變更清單見 `contracts/behavior_changes.yaml`（BC／SC／DF）與 CHANGELOG。

#### 已知問題

下列是 Python daemon 的既有行為，0.4.0 維持現況，這一版不修改。

- 互動租約還開著時，若取消剛好落在該次輸入已經送出、命令尚未返回的瞬間，同一個租約之後每次互動傳送都回 `SESSION_BUSY`，直到租約逾時。這段時間無法再送互動輸入。
- `file push` 與 `file pull` 會為每個步驟分開累計已寫出的位元組，傳輸結束後這些步驟帳沒有結清。若要依「這次傳輸總共寫出多少」判斷中斷是否安全，這個總帳可能是空的。請改看目標上的檔案，以及回應裡的 `chunks_sent`／`chunks_total`。
- 動態偵測到的序列裝置若開埠失敗，而且沒有 passthrough 後備 profile，daemon 不會留下這次失敗。session 清單看不到該裝置，也沒有失敗原因。
- 裝置釋放後，若原來的 profile template 已不存在，daemon 會留下不能下命令的佔位 session，並繼續占用原來的 COM 編號。佔位只在該裝置再接上並執行 `device attach` 時移除。裝置不再回來時，沒有命令可以刪掉這個佔位。
- 對這種佔位 session 執行 `session bind`，會直接回覆仍是釋放狀態並建議改做 `device attach`。這個回覆早於「正在燒錄」與「裝置已綁到其他 session」的檢查，所以燒錄中或裝置已被占用時，看到的不是 `FLASHING_BUSY` 或 `DEVICE_ALREADY_BOUND`。
- `setup --rollback` 不能回到 pipx 安裝的 0.3.2。第一次換成 bundle 時沒有上一個 bundle 版本，命令回 `INVALID_ARGS`（`rollback_unavailable`），而且不改動目前的安裝。手動回到 0.3.2 的步驟與 §5.2 相同：先 `serialwrap daemon stop`，只在 `~/.local/bin/serialwrap` 與 `serialwrapd` 是指向 `~/.local/share/serialwrap/versions/` 的 symlink 時才刪除，再 `pipx uninstall serialwrap && pipx install serialwrap==0.3.2`（或 0.3.2 wheel；只做 `pipx install --force` 不會重建入口），然後 `serialwrap setup`。systemd 用 `serialwrap service start` 啟動；on-demand 的 0.3.2 `serialwrap daemon start` 有既有缺陷，可改直接背景執行 `serialwrapd`。這不是受支援的降級路徑。bundle 與 bundle 之間的 `setup --rollback` 不受此限。

### English
- The `serialwrap` CLI is now native Rust (20 command groups); the daemon is still the bundled Python implementation (Stage 1).
- Faster CLI: a `daemon status` round trip takes about 16 ms at p50 with the Rust CLI, versus about 155 ms with the Python CLI from the same source tree, roughly 10x faster (p95 about 20 ms vs 220 ms; fastest run about 8 ms vs 86 ms). Both CLIs talked to the same Python daemon, the Rust CLI was a release build, and each side had 600 samples. This was measured on a WSL2 development machine (24 threads) under heavy background load, so read the ratio rather than the absolute values; a separate run under the same conditions showed about 9x at p50. This is not a standard measurement environment, and Windows has not been measured. These numbers cover the CLI only; WAL and console latency can only be compared once the Rust daemon ships in Stage 2. The measurement script is in the skill's CLI performance comparison section.
- `install.sh` now installs the Rust bundle (OD-50): it downloads `serialwrap-0.4.0-linux-x86_64.tar.gz` from the GitHub release, verifies it against `SHA256SUMS`, and runs `bin/serialwrap setup`; `--bundle` installs offline. Requirements: x86_64, glibc >= 2.39 (Ubuntu 24.04 or later), and `/usr/bin/python3.12`. The pipx install path is retired (the wheel is still published).
- New bundle install: `bin/serialwrap setup` installs into `~/.local/share/serialwrap/versions/<version>` (Windows: `%LOCALAPPDATA%\Programs\serialwrap`) with atomic update, `setup --rollback`, and a `version_pairing` doctor check.
- Windows: `windows-task` supervision (no admin), optional com0com virtual serial pair (`setup --with-com0com`).
- Fixed: on-demand `daemon start` could not find `serialwrapd.py` under pipx installs.
- Upgrade note: the 0.3.2 daemon may keep running while you upgrade; `setup` stops it once it is idle, waits until the old process tree has fully exited, switches, and then starts the 0.4.0 daemon. If the upgrade fails after the old daemon was stopped, the install is restored but the old daemon is not restarted automatically; use the start command printed in the response.
- Pass your current supervision mode explicitly (`--user`, `--system --with-sudo`, or `--on-demand`). Without a flag, `setup` picks a mode automatically exactly like 0.3.2, which may change your mode and `config.yaml`.
- Downgrading to 0.3.2 is not a supported operation: `setup --rollback` cannot return to a pipx 0.3.2 install (with no previous bundle it returns `INVALID_ARGS` / `rollback_unavailable` and leaves the install unchanged). The manual steps are in §5.2 of this document; they are not a supported downgrade.
- Behavior changes are listed in `contracts/behavior_changes.yaml` (BC / SC / DF) and the changelog.

#### Known issues

The Python daemon already behaved as follows. 0.4.0 keeps that behavior and does not change it.

- If you cancel an interactive command in the moment after its input has been sent but before the command returns, later interactive sends on that same open lease return `SESSION_BUSY` until the lease times out. You cannot send more interactive input during that time.
- `file push` and `file pull` record transmitted bytes separately for each step, and those step totals are not closed out when the transfer finishes. A later check of "how many bytes this transfer wrote" can therefore come back empty. Look at the file on the target, and at `chunks_sent` / `chunks_total` in the response, before deciding whether a retry is safe.
- If a newly detected serial device fails to open and there is no passthrough fallback profile, the daemon keeps no record of that failure. The device does not appear in the session list, and there is no failure reason.
- After a device is released, if its profile template no longer exists, the daemon leaves a placeholder session that accepts no commands and keeps the original COM number. The placeholder is removed only when that device is connected again and you run `device attach`. If the device never comes back, no command deletes the placeholder.
- `session bind` on that placeholder returns immediately that the session is still released and that you should run `device attach`. This happens before the checks for an in-progress flash and for a device already bound to another session, so you do not see `FLASHING_BUSY` or `DEVICE_ALREADY_BOUND` in those cases.
- `setup --rollback` cannot return to a pipx 0.3.2 install. The first switch to a bundle has no previous bundle version, so the command returns `INVALID_ARGS` (`rollback_unavailable`) and does not change the installation. A manual return follows §5.2: `serialwrap daemon stop`, delete `~/.local/bin/serialwrap` and `serialwrapd` only when they are symlinks into `~/.local/share/serialwrap/versions/`, then `pipx uninstall serialwrap && pipx install serialwrap==0.3.2` (or the 0.3.2 wheel; `pipx install --force` alone does not recreate the entries), then `serialwrap setup`. Start systemd with `serialwrap service start`. On-demand `serialwrap daemon start` on that pipx 0.3.2 install has a pre-existing defect; start `serialwrapd` in the background instead. That path is not a supported downgrade. `setup --rollback` between bundle versions is unchanged.
```

## 8. PR 政策（release PR）

- 分支：`feature/rust-refactor` → `main`（不得直接 commit 到 `main`）。
- PR body 填 `.github/pull_request_template.md` 的 Policy Checklist（R-11）；引用 issue 一律 `Closes #N`（R-17）。
- label：`release:0.4.0`（R-07：tag 建立前 `VERSION` 與最新 tag 不一致的豁免）。
- 審查：修改 `contracts/`、比較器或哨兵的部分需獨立審閱（§10.4）；PR 描述分列產品與契約變更理由及基線對照。
- merge 後由主 session 建 tag `v0.4.0`（`release.yml` 以 tag 對 `VERSION` 驗證）並確認 GitHub release 的產物與 `SHA256SUMS`。
