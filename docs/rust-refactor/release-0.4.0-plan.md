# serialwrap 0.4.0 release 計畫（Stage 1 出口：Rust CLI＋Python daemon）

> 依據：`owner-decisions-2026-10-15.md` OD-33（2026-10-28）。本文件只做準備：**實作輪不得 merge 到 `main`、不得建 tag、不得發 GitHub release**；這些由主 session 在 Stage 1 出口 gate 通過後依政策開 PR 執行。
> 版號：Stage 1 出口＝`0.4.0`；Stage 2 出口（全 Rust daemon）＝`0.5.0`（Canary 前可用 `0.5.0-rc.N`）；Stage 3 Python daemon 退役＝`1.0.0`。

## 1. 前置條件（全部滿足才開 release PR）

| # | 條件 | 證據 |
|---|---|---|
| 1 | Stage 1 出口 gate：Stage 1A `stage1a_gate.py` overall 為 `VERIFIED`，或為 `IMPLEMENTED` 且只剩 OD-27～OD-32 明確允許的 `BLOCKED-OWNER`；Stage 1B 只剩同範圍擁有者項，沒有 FAIL／INSUFFICIENT | 出口 driver `scripts/rust/stage1_exit_driver.sh` 於最終 HEAD 的報告 |
| 2 | 全套 `python3 -m pytest -q tests/`、`cargo test --workspace`、`python3 -m tools.contracts check`、`python3 -m policy_check --repo .` 在 release HEAD 通過 | 同上 |
| 3 | Linux 升級驗證（§4）在 release 候選 bundle 上兩種監管模式皆 PASS | `scripts/docker/run-upgrade.sh` 的 `<out>/<mode>/upgrade-report.json` |
| 4 | §6 列的待擁有者裁決項已裁決（或擁有者明示接受以已知限制發佈） | `owner-decisions-2026-10-15.md` |
| 5 | release workflow 已能產出 §2 的 bundle（RELEASE-TODO-01） | release PR 的 CI |

## 2. 發佈內容

| 產物 | 內容 | 產生方式 |
|---|---|---|
| Linux x86_64 bundle（`serialwrap-0.4.0-linux-x86_64.tar.gz`） | `bin/serialwrap`（Rust CLI，20 個群組原生）、`bin/serialwrapd`（launcher）、`versions/0.4.0/`：`release-manifest.json`、`SHA256SUMS`、`py/`（自帶 PyYAML 的 Python daemon runtime＋`sw_core`，亦為 Python 委派元件 `python_cli_delegate`）；不含 `sw-cli/test-fakes` | `scripts/rust/mkbundle.sh --release-bundle --profile release --out <dir>`（`VERSION` 已改為 0.4.0，**不得**用 `--version-override`） |
| Windows x86_64 bundle（`serialwrap-0.4.0-windows-x86_64.zip`） | 同上，入口為 `.exe`；可選 com0com 3.0.0.0 元件（`--with-com0com`，含 GPLv2 授權與原始碼連結） | 擁有者 Windows runner 原生建置（P9 BO-P9-17 的同一條路徑）；或 `--target x86_64-pc-windows-gnu` 交叉編譯後於 Windows 原生驗證。**release workflow 不建 Windows bundle**：待 `publish-linux-bundle` job 成功後，由擁有者以 `gh release upload` 附加 zip 並重傳含 zip 雜湊的 `SHA256SUMS`（步驟見下方 RELEASE-TODO-01） |
| Python wheel（`serialwrap-0.4.0-py3-none-any.whl`） | Python CLI＋daemon（相容路徑：只裝 Python 的使用者、`install.sh`） | 既有 `release.yml` 的 `python3 -m build --wheel` |
| `install.sh` | 目前仍為 pipx 安裝 Python 套件＋`serialwrap setup`（與 0.3.2 相同）。**0.4.0 Rust CLI 的安裝入口是 bundle 的 `bin/serialwrap setup`** | 見 RELEASE-TODO-02 |

**RELEASE-TODO-01（release workflow）**：原 `.github/workflows/release.yml` 只發佈 wheel 與 PyInstaller exe，沒有 Rust bundle job。release PR 必須新增 Linux bundle job（cargo build＋`mkbundle.sh --release-bundle --profile release`＋打包＋上傳＋`SHA256SUMS`）並登記 Windows bundle 的上傳方式；PyInstaller exe 是否續發由擁有者決定（建議 0.4.0 仍附上，供尚未改用 bundle 的 Windows 使用者）。

- **Linux（已備，`feature/release-0.4.0-prep`）**：`release.yml` 新增 `publish-linux-bundle` job（tag push 觸發、`needs: publish`、`runs-on: ubuntu-24.04`；既有 `publish`／`publish-windows-exe` 不動）。步驟依序：tag 與 `VERSION`、`Cargo.toml` `[workspace.package] version` 三者一致檢查（不一致即 `::error::` 失敗）→ 以系統 `/usr/bin/python3.12` 建 build venv（apt 安裝 `python3.12-venv`；版本須等於 `contracts/cli_help/_meta.json` 的 `python_version`；venv 內裝 `pyyaml==6.0.3`、`jsonschema==4.10.3` 並置於 PATH 首位），使 bundle runtime 的 `pyvenv.cfg` 為 `home = /usr/bin`（OD-50；**不得**用 `actions/setup-python`：hosted toolcache Python 建的 runtime 依賴 `/opt/hostedtoolcache` 的 libpython 與標準函式庫，搬到使用者主機即無法啟動，S1-3185c6d-1）→ `cargo build --release --workspace --locked` → `scripts/rust/mkbundle.sh --release-bundle --profile release --out "$RUNNER_TEMP/bundle/serialwrap-${VERSION}-linux-x86_64"` → `tar` 打包為 `dist/serialwrap-${VERSION}-linux-x86_64.tar.gz`（保留 `bin/` 相對 symlink、排除 `.mkbundle-owner`）並產生 `dist/SHA256SUMS` → 解開後 smoke（外層 `sha256sum -c`、`bin/serialwrap --version` 等於 `serialwrap ${VERSION}`、`bin/serialwrap --help`、`versions/${VERSION}/release-manifest.json` 存在且 `bundle_version`／cli／daemon／pairing 版本一致且非 sentinel、內層 `SHA256SUMS` 核對、`versions/${VERSION}/bin` 不含 `SW_TEST_UPDATE_PAUSE_AFTER`／`SW_TEST_UPDATE_FORCE_VERIFY_FAIL` 故障注入鉤子字串、不含 delegate sentinel、`versions/${VERSION}/py/pyvenv.cfg` 含 `home = /usr/bin` 與 `include-system-site-packages = false`）→ 容器 smoke（以 OD-16 (5) 同一 digest 的 `ubuntu:24.04` 只加裝 `python3.12`——無 PyYAML、無 libpython 共享庫、無 toolcache——`docker run --network none`，只唯讀掛載上一步解開的實際 tarball 內容；容器內以 bundle runtime `-I -B` 匯入 `sw_core.daemon` 與 `yaml`，再以 bundle 的 `bin/serialwrap daemon start`／`daemon status`／`daemon stop` 啟停 on-demand daemon（獨立暫存 config／state／run／WAL 目錄、空 by-id 目錄、不碰裝置），status 須 `ok`、`running` 且版本等於 `VERSION`，stop 後行程須結束；任一失敗即阻擋上傳）→ `gh release upload "$TAG_NAME" dist/serialwrap-${VERSION}-linux-x86_64.tar.gz dist/SHA256SUMS --clobber`。結構與步驟行為由 `tests/test_release_workflow.py` 守護。
- **Windows（擁有者 runner，workflow 不建）**：目前 `mkbundle.sh` 拒絕 Windows target，Windows 正式 bundle 的建置腳本屬 BO-P9-17 同一條路徑、由擁有者 runner 承接（現有 `win-blackbox.ps1` 的 `New-WindowsDevBundle` 只產生測試用 dev bundle，不得作為 release 產物）。建出並在 Windows 原生驗證後（`bin\serialwrap.exe --version` 等於 `serialwrap <V>`、`--help`、`versions\<V>\release-manifest.json` 版本一致、內層 `SHA256SUMS` 核對、不含 test-fakes 鉤子），**等 release workflow 的 `publish-linux-bundle` job 成功之後**於 runner 的 WSL 或 Git Bash 執行：

  ```bash
  V="$(tr -d ' \n' < VERSION)"                       # 必須等於 tag 去掉 v
  cd <Windows bundle 根目錄的上一層>                   # 其下為 serialwrap-${V}-windows-x86_64/（bin\、versions\<V>\）
  zip -r -X "serialwrap-${V}-windows-x86_64.zip" "serialwrap-${V}-windows-x86_64"
  #（無 zip 時可用 PowerShell：Compress-Archive -Path serialwrap-<V>-windows-x86_64 -DestinationPath serialwrap-<V>-windows-x86_64.zip）
  gh release download "v${V}" --pattern SHA256SUMS --clobber   # 取回 Linux job 上傳的 SHA256SUMS
  grep -q "  serialwrap-${V}-linux-x86_64.tar.gz$" SHA256SUMS  # 必須已含 Linux tarball 列
  sed -i "/  serialwrap-${V}-windows-x86_64.zip$/d" SHA256SUMS
  sha256sum "serialwrap-${V}-windows-x86_64.zip" >> SHA256SUMS # LF 行尾、與 Linux 列同格式
  gh release upload "v${V}" "serialwrap-${V}-windows-x86_64.zip" SHA256SUMS --clobber
  gh release download "v${V}" --pattern 'serialwrap-*' --pattern SHA256SUMS --dir verify-dl && (cd verify-dl && sha256sum -c SHA256SUMS)
  ```

  若之後重跑 release workflow，`publish-linux-bundle` 會以 `--clobber` 覆寫 `SHA256SUMS`（只剩 Linux 列），須重做上面最後四行把 Windows 列補回。

**RELEASE-TODO-02（install.sh）**：決定 0.4.0 的 `install.sh` 是維持 pipx（Python CLI）還是改為「下載／解開 bundle 後執行 `bin/serialwrap setup`」。在決定前，release notes 必須明寫兩條安裝路徑的差異。

## 3. 版號與 changelog 步驟（release PR 內執行）

1. `VERSION`：`0.3.2` → `0.4.0`；`Cargo.toml` 的 `[workspace.package] version` 同步改為 `0.4.0` 並更新 `Cargo.lock`（`cargo update -w`）。`pyproject.toml` 的 dynamic version 由 `VERSION` 讀取。
2. 收斂 changelog：`python3 -m policy_check.changelog collate --version 0.4.0 --date <發佈日 YYYY-MM-DD>`（把 `changelog.d/*.md` 依 type 收斂成 `CHANGELOG.md` 的 dated 段並清空目錄）。collate 前先修正缺 frontmatter 的既存 fragment（第 3 輪報告列為範圍外的 5 個壞檔），否則分類會錯。
   - 2026-10-29 試跑（暫存 clone、`--date 2026-11-01`，結果未提交）：原始 217 個 fragment 中 5 個缺 frontmatter（`229-late-unlock-prompt.md`、`rust-p4-win-test-l1-set.md`、`st06-command-completion.md`、`st06-smart4-prompt-window.md`、`st06-smart4r-console-unlock.md`），collate 於第一個壞檔即 `FragmentError` 中止且不改任何檔。`feature/release-0.4.0-prep` 已只補 frontmatter／格式（`st06-smart4r-console-unlock.md` 同時含 Added 與 Fixed，依原條目拆為 `-a-profile`／`-b-recovery`／`-c-sysrq-reboot`（feat）與 `-d-prompt-boundary`（fix）四檔，文字不變）；重跑 collate 成功收斂 220 個 fragment（Added 73、Changed 39、Fixed 106、Security 2）。release PR 執行前若再有新 fragment，仍須重跑本步驟確認。
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
- 判定：`scripts/docker/upgrade_checks.py`（`tests/test_docker_upgrade.py`）。19 項必要檢查，任一 FAIL 即 FAIL、缺項即 INSUFFICIENT：
  0.3.2 安裝與啟動、建立 profile／alias／binding 基線、0.3.2 doctor、舊 daemon 執行中直接 setup（`upgrade_with_old_daemon_running`：成功切換；拒絕只對「非 legacy 的不支援交接 daemon」以 `METHOD_NOT_FOUND`＋現場未改動判定，0.3.x daemon 被拒絕即 FAIL）、`legacy_handoff_direct_setup`（OD-48：0.3.2 daemon 執行中直接 setup 必須成功切換、報告含 `legacy_stop`、舊 daemon 行程樹結束、新 daemon 由 setup 自己啟動——在 driver 任何啟動動作之前取樣，setup 沒啟動即 FAIL、config／profiles 逐位元組保留、setup 期間無兩個 serialwrapd 同時存在）、停舊後 setup（直接 setup 成功時記為不需要；「先停再升級」路徑與 OBS-UPGRADE-NOT-STARTED 保留於重新升級階段）、`~/.local/bin` 入口取代且備份記下 pipx 入口、config／profiles 逐位元組保留、0.4.0 daemon 由 bundle 啟動、監管模式依 config 收斂（systemd-user：unit active＋enabled、MainPID 為新 daemon、無殘留 hold drop-in；on-demand：unit 未啟用）、alias／binding 語意相容、0.4.0 doctor（非 advisory 全綠＋`version_pairing`；advisory 只容許容器內必然為 0 的 `devices`）、`setup --rollback`（拒絕時現場不得改動）、手動降級回 0.3.2（版本、config、狀態、doctor）、降級後重新升級、全程 /proc 20 ms 取樣不得同時出現兩個 serialwrapd 實例。
- 首次實跑（2026-10-28，bundle git revision `2a947f6`）：systemd-user 與 on-demand 皆 **PASS**（各約 2,800 筆取樣、0 次重疊）；報告未保存，不作為證據。
- 本輪驗證（起點 `9908f93`，工作樹含 BB-1 修正）：`python3 -m pytest -q tests/test_docker_upgrade.py` 29 passed，含 systemd-user 與 on-demand 兩模式的完整 0.3.2→0.4.0→降級→再升級實跑並各判 PASS；Docker 報告留在 pytest 暫存目錄，未保存進 repo，不能替代最終 HEAD 的出口證據。
- 判準收緊後實跑（2026-10-28，run `s1v14-c8df563`，bundle git revision `c8df563`）：兩模式皆 **PASS**（取樣 2641／2705、0 次重疊）；報告原文與 `SHA256SUMS` 保存於 `evidence/stage1/upgrade-docker/s1v14-c8df563/`。此為開發中 bundle，不取代前置條件 #3 的 release bundle 重跑。觀察項見 §6。
- OD-48 實作後實跑（2026-10-29，run `od48run2`，bundle git revision `014eb2b`，分支 `feature/od48-legacy-handoff`；雲端環境以 HTTPS apt 來源建映像、其餘 driver／判定／`--network none` 不變）：兩模式皆 **PASS**，19 項含 `legacy_handoff_direct_setup`（systemd-user：`systemctl_stop`；on-demand：`rpc_daemon_stop`；兩者 `waited_ms` 約 5.2 s、新 daemon 由 setup 啟動、取樣 1576／1531、0 次重疊）；報告與 `SHA256SUMS` 保存於 `evidence/stage1/upgrade-docker/od48-014eb2b/`。同為開發中 bundle，不取代前置條件 #3。
- OD-48 第 2 輪實跑（2026-10-29，bundle git revision `2f8f7ed`，`--release-bundle --profile release --version-override 0.4.0`；建映像方式同上）：systemd-user、on-demand 完整流程各 19 項 **PASS**，另以 `--mode systemd-system` 驗「0.3.2 system daemon 執行中直接 `setup --system --with-sudo`」成功路徑 13 項 **PASS**（`systemctl_stop`、`waited_ms` 約 5.2 s、新 daemon 由 setup 啟動、MainPID 為新 daemon、無殘留 drop-in、取樣 1602、0 次重疊）。systemd-system 觀察項：`OBS-0.3.2-SYSTEM-UMASK`（0.3.2 system unit `UMask=0117` 使 daemon 自建目錄缺 x 位元，driver 先以一般 umask 暖機一次 0.3.2 daemon）、`OBS-SYSTEM-PROFILES-MANAGED`（system setup 以套件 profiles 覆寫 `/etc/serialwrap/profiles` 同名檔，0.3.2 同語意）。證據於 `evidence/stage1/upgrade-docker/od48-2f8f7ed/`；同為開發中 bundle，不取代前置條件 #3。
- OD-48 第 2 輪 sol 修正後重跑（bundle git revision `55cfa03`）：三模式皆 **PASS**（19／19／13 項；`waited_ms` 5351／5214／5311、取樣 1476／1562／1623、0 次重疊）。systemd-system 的套件管理 profile 改為全數保留於快照、只允許覆寫成新 bundle 權威內容（S1-f3c66b4-1）：`/etc/serialwrap/profiles/default.yaml` 記為 `package_updated`。證據於 `evidence/stage1/upgrade-docker/od48-55cfa03/`（`od48-2f8f7ed` 的 systemd-system 判定已被此輪取代）。

### 4.2 Windows（LOCAL-WINDOWS-TODO）

0.3.2 在 Windows 的正式安裝為 PyInstaller one-file exe（或 pip 安裝）；0.4.0 為 bundle 安裝到 `%LOCALAPPDATA%\Programs\serialwrap`。升級驗證需要：throwaway 隔離根（`SERIALWRAP_INSTALL_ROOT`、隔離的 config／state／run 目錄、device allowlist 只有 COM20）、windows-task 監管的 0.3.2→0.4.0 交接。OD-28 的排程工作授權時機在 P8 Rust daemon bundle 完成後（plan 09 Task 13），因此本項列 **LOCAL-WINDOWS-TODO**：on-demand 模式的 Windows 升級可在 OD-28 範圍內先以 throwaway 隔離根實跑；windows-task 模式待 OD-28 時機。不得以 Linux 結果代替。

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

## 7. release notes 草稿

```markdown
## serialwrap 0.4.0

Stage 1 of the Rust rewrite: the `serialwrap` CLI is now a native Rust binary (all 20 command groups),
while the daemon remains the Python implementation shipped inside the bundle.

### 繁體中文
- `serialwrap` CLI 改為 Rust 原生實作（20 個命令群組），daemon 仍為 bundle 內附的 Python 實作（Stage 1）。
- 新增發佈組合（bundle）安裝：`bin/serialwrap setup` 安裝到 `~/.local/share/serialwrap/versions/<版本>`（Windows：`%LOCALAPPDATA%\Programs\serialwrap`），支援原子更新、`setup --rollback` 與 `doctor` 的 `version_pairing` 檢查。
- Windows：`windows-task` 監管（免管理員）、可選 com0com 虛擬序列埠（`setup --with-com0com`）。
- 修正：pipx 安裝下 on-demand `daemon start` 找不到 `serialwrapd.py`。
- 升級注意：從 0.3.2 升級時 0.3.2 daemon 可以維持執行；`setup` 會在確認閒置後停止舊 daemon，等它完全結束才切換，再啟動 0.4.0 daemon。舊 daemon 停止後若升級失敗，安裝會還原但舊 daemon 不會自動重啟，請依回應中的啟動指令手動啟動。
- 升級時請**明確指定目前的監管模式**（`--user`／`--system --with-sudo`／`--on-demand`）；不加旗標時與 0.3.2 相同會自動選擇，可能改變模式與 `config.yaml`。
- 不支援降級回 0.3.2：`setup --rollback` 不能回到 pipx 的 0.3.2（OD-49 已知限制）；文件中的手動程序僅供參考。
- 行為變更清單見 `contracts/behavior_changes.yaml`（BC／SC／DF）與 CHANGELOG。

### English
- The `serialwrap` CLI is now native Rust (20 command groups); the daemon is still the bundled Python implementation (Stage 1).
- New bundle install: `bin/serialwrap setup` installs into `~/.local/share/serialwrap/versions/<version>` (Windows: `%LOCALAPPDATA%\Programs\serialwrap`) with atomic update, `setup --rollback`, and a `version_pairing` doctor check.
- Windows: `windows-task` supervision (no admin), optional com0com virtual serial pair (`setup --with-com0com`).
- Fixed: on-demand `daemon start` could not find `serialwrapd.py` under pipx installs.
- Upgrade note: the 0.3.2 daemon may keep running while you upgrade; `setup` stops it once it is idle, waits until the old process tree has fully exited, switches, and then starts the 0.4.0 daemon. If the upgrade fails after the old daemon was stopped, the install is restored but the old daemon is not restarted automatically; use the start command printed in the response.
- Pass your current supervision mode explicitly (`--user`, `--system --with-sudo`, or `--on-demand`). Without a flag, `setup` picks a mode automatically exactly like 0.3.2, which may change your mode and `config.yaml`.
- Downgrading to 0.3.2 is not supported: `setup --rollback` cannot return to a pipx 0.3.2 install (known limitation, OD-49); the documented manual procedure is for reference only.
```

## 8. PR 政策（release PR）

- 分支：`feature/rust-refactor` → `main`（不得直接 commit 到 `main`）。
- PR body 填 `.github/pull_request_template.md` 的 Policy Checklist（R-11）；引用 issue 一律 `Closes #N`（R-17）。
- label：`release:0.4.0`（R-07：tag 建立前 `VERSION` 與最新 tag 不一致的豁免）。
- 審查：修改 `contracts/`、比較器或哨兵的部分需獨立審閱（§10.4）；PR 描述分列產品與契約變更理由及基線對照。
- merge 後由主 session 建 tag `v0.4.0`（`release.yml` 以 tag 對 `VERSION` 驗證）並確認 GitHub release 的產物與 `SHA256SUMS`。
