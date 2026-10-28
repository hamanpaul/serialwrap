# serialwrap 0.4.0 release 計畫（Stage 1 出口：Rust CLI＋Python daemon）

> 依據：`owner-decisions-2026-10-15.md` OD-33（2026-10-28）。本文件只做準備：**實作輪不得 merge 到 `main`、不得建 tag、不得發 GitHub release**；這些由主 session 在 Stage 1 出口 gate 通過後依政策開 PR 執行。
> 版號：Stage 1 出口＝`0.4.0`；Stage 2 出口（全 Rust daemon）＝`0.5.0`（Canary 前可用 `0.5.0-rc.N`）；Stage 3 Python daemon 退役＝`1.0.0`。

## 1. 前置條件（全部滿足才開 release PR）

| # | 條件 | 證據 |
|---|---|---|
| 1 | Stage 1 出口 gate：Stage 1A `stage1a_gate.py` overall PASS；Stage 1B 只剩擁有者裁決允許的 BLOCKED-OWNER（OD-27～OD-32），無 FAIL／INSUFFICIENT | 出口 driver `scripts/rust/stage1_exit_driver.sh` 於最終 HEAD 的報告 |
| 2 | 全套 `python3 -m pytest -q tests/`、`cargo test --workspace`、`python3 -m tools.contracts check`、`python3 -m policy_check --repo .` 在 release HEAD 通過 | 同上 |
| 3 | Linux 升級驗證（§4）在 release 候選 bundle 上兩種監管模式皆 PASS | `scripts/docker/run-upgrade.sh` 的 `<out>/<mode>/upgrade-report.json` |
| 4 | §6 列的待擁有者裁決項已裁決（或擁有者明示接受以已知限制發佈） | `owner-decisions-2026-10-15.md` |
| 5 | release workflow 已能產出 §2 的 bundle（RELEASE-TODO-01） | release PR 的 CI |

## 2. 發佈內容

| 產物 | 內容 | 產生方式 |
|---|---|---|
| Linux x86_64 bundle（`serialwrap-0.4.0-linux-x86_64.tar.gz`） | `bin/serialwrap`（Rust CLI，20 個群組原生）、`bin/serialwrapd`（launcher）、`versions/0.4.0/`：`release-manifest.json`、`SHA256SUMS`、`py/`（Python daemon runtime＋`sw_core`，亦為 Python 委派元件 `python_cli_delegate`） | `scripts/rust/mkbundle.sh --dev --profile release --out <dir>`（`VERSION` 已改為 0.4.0，**不得**用 `--version-override`） |
| Windows x86_64 bundle（`serialwrap-0.4.0-windows-x86_64.zip`） | 同上，入口為 `.exe`；可選 com0com 3.0.0.0 元件（`--with-com0com`，含 GPLv2 授權與原始碼連結） | 擁有者 Windows runner 原生建置（P9 BO-P9-17 的同一條路徑）；或 `--target x86_64-pc-windows-gnu` 交叉編譯後於 Windows 原生驗證 |
| Python wheel（`serialwrap-0.4.0-py3-none-any.whl`） | Python CLI＋daemon（相容路徑：只裝 Python 的使用者、`install.sh`） | 既有 `release.yml` 的 `python3 -m build --wheel` |
| `install.sh` | 目前仍為 pipx 安裝 Python 套件＋`serialwrap setup`（與 0.3.2 相同）。**0.4.0 Rust CLI 的安裝入口是 bundle 的 `bin/serialwrap setup`** | 見 RELEASE-TODO-02 |

**RELEASE-TODO-01（release workflow）**：現行 `.github/workflows/release.yml` 只發佈 wheel 與 PyInstaller exe，沒有 Rust bundle job。release PR 必須新增 Linux bundle job（cargo build＋`mkbundle.sh --profile release`＋打包＋上傳＋`SHA256SUMS`）並登記 Windows bundle 的上傳方式；PyInstaller exe 是否續發由擁有者決定（建議 0.4.0 仍附上，供尚未改用 bundle 的 Windows 使用者）。

**RELEASE-TODO-02（install.sh）**：決定 0.4.0 的 `install.sh` 是維持 pipx（Python CLI）還是改為「下載／解開 bundle 後執行 `bin/serialwrap setup`」。在決定前，release notes 必須明寫兩條安裝路徑的差異。

## 3. 版號與 changelog 步驟（release PR 內執行）

1. `VERSION`：`0.3.2` → `0.4.0`；`Cargo.toml` 的 `[workspace.package] version` 同步改為 `0.4.0` 並更新 `Cargo.lock`（`cargo update -w`）。`pyproject.toml` 的 dynamic version 由 `VERSION` 讀取。
2. 收斂 changelog：`python3 -m policy_check.changelog collate --version 0.4.0 --date <發佈日 YYYY-MM-DD>`（把 `changelog.d/*.md` 依 type 收斂成 `CHANGELOG.md` 的 dated 段並清空目錄）。collate 前先修正缺 frontmatter 的既存 fragment（第 3 輪報告列為範圍外的 5 個壞檔），否則分類會錯。
3. 重新產生 bundle（§2）並重跑 §4 升級驗證，證據綁 release HEAD。
4. `python3 -m policy_check --repo .`、全套測試通過。

## 4. 升級驗證（OD-33 R2(b)）

### 4.1 Linux（實作完成，可重複執行）

```bash
scripts/docker/run-upgrade.sh                    # 兩種模式；預設由 git tag v0.3.2 建 wheel、mkbundle 建 0.4.0 候選
scripts/docker/run-upgrade.sh --mode systemd-user --bundle <release bundle 目錄> --wheel <0.3.2 發佈 wheel>
```

- 容器：`tools/docker/upgrade.Dockerfile`（OD-16 (5) 的 ubuntu:24.04 digest），`--network none`、不帶任何裝置，wheel／bundle／driver 唯讀 bind mount；每種監管模式一個全新容器。不碰本機正式安裝。
- 0.3.2 來源：容器不連網，預設以 `git archive v0.3.2` 建 wheel；正式驗證時改用 `--wheel` 指向 GitHub release 上的 0.3.2 wheel。
- 判定：`scripts/docker/upgrade_checks.py`（`tests/test_docker_upgrade.py`）。18 項必要檢查，任一 FAIL 即 FAIL、缺項即 INSUFFICIENT：
  0.3.2 安裝與啟動、建立 profile／alias／binding 基線、0.3.2 doctor、舊 daemon 執行中直接 setup（拒絕時現場不得改動）、停舊後 setup、`~/.local/bin` 入口取代且備份記下 pipx 入口、config／profiles 逐位元組保留、0.4.0 daemon 由 bundle 啟動、監管模式依 config 收斂（systemd-user：unit active＋enabled、MainPID 為新 daemon、無殘留 hold drop-in；on-demand：unit 未啟用）、alias／binding 語意相容、0.4.0 doctor（非 advisory 全綠＋`version_pairing`；advisory 只容許容器內必然為 0 的 `devices`）、`setup --rollback`（拒絕時現場不得改動）、手動降級回 0.3.2（版本、config、狀態、doctor）、降級後重新升級、全程 /proc 20 ms 取樣不得同時出現兩個 serialwrapd 實例。
- 首次實跑（2026-10-28，bundle git revision `2a947f6`）：systemd-user 與 on-demand 皆 **PASS**（各約 2,800 筆取樣、0 次重疊）；報告未保存，不作為證據。
- 判準收緊後實跑（2026-10-28，run `s1v14-c8df563`，bundle git revision `c8df563`）：兩模式皆 **PASS**（取樣 2641／2705、0 次重疊）；報告原文與 `SHA256SUMS` 保存於 `evidence/stage1/upgrade-docker/s1v14-c8df563/`。此為開發中 bundle，不取代前置條件 #3 的 release bundle 重跑。觀察項見 §6。

### 4.2 Windows（LOCAL-WINDOWS-TODO）

0.3.2 在 Windows 的正式安裝為 PyInstaller one-file exe（或 pip 安裝）；0.4.0 為 bundle 安裝到 `%LOCALAPPDATA%\Programs\serialwrap`。升級驗證需要：throwaway 隔離根（`SERIALWRAP_INSTALL_ROOT`、隔離的 config／state／run 目錄、device allowlist 只有 COM20）、windows-task 監管的 0.3.2→0.4.0 交接。OD-28 的排程工作授權時機在 P8 Rust daemon bundle 完成後（plan 09 Task 13），因此本項列 **LOCAL-WINDOWS-TODO**：on-demand 模式的 Windows 升級可在 OD-28 範圍內先以 throwaway 隔離根實跑；windows-task 模式待 OD-28 時機。不得以 Linux 結果代替。

### 4.3 既有正式安裝機（OD-27）

release 後於已安裝 0.3.2 的其他電腦（含接有真板者）以 release 產物實際升級，並在該機發起 OD-27 的真板全套。屬擁有者項目，維持 BLOCKED-OWNER。

## 5. 使用者升級與降級程序（release notes 用）

### 5.1 0.3.2（pipx）→ 0.4.0（bundle），Linux

1. 停止 0.3.2 daemon：`serialwrap daemon stop`（systemd 模式會自動轉為 `service stop`）。
   - 若未先停止，0.4.0 `setup` 會以 `METHOD_NOT_FOUND`（`daemon.handoff_stop`）**拒絕且不改動任何東西**：0.3.2 daemon 沒有原子交接 RPC，0.4.0 依規格 fail-closed（見 §6 OBS-UPGRADE-LEGACY-HANDOFF）。
2. 解開 bundle，執行 `<bundle>/bin/serialwrap setup`（沿用 config 的監管模式；可加 `--user`／`--on-demand`）。`~/.local/bin/serialwrap`、`serialwrapd` 改指 `~/.local/share/serialwrap/versions/0.4.0/bin/`，原 pipx 入口記在 `~/.local/share/serialwrap/backups/none-to-0.4.0-*/entries.json`。config、profiles、state.json 不變。
3. 啟動 daemon：systemd 模式 `serialwrap service start`；on-demand `serialwrap daemon start`。（步驟 1 已停止 daemon 時 setup 不會代為啟動，見 OBS-UPGRADE-NOT-STARTED。）
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
| OBS-UPGRADE-LEGACY-HANDOFF | 0.3.2 daemon 執行中時 0.4.0 `setup` 回 `METHOD_NOT_FOUND`（`daemon.handoff_stop` 不存在）拒絕 | 使用者必須先手動停 daemon；安全（不 two-reader、不改動現場，已驗證） | 規格行為（plan 05 Task 12：不支援交接的 daemon fail-closed）。是否為舊版 daemon 提供「確認閒置後一般 stop」的升級特例 → **PENDING-OD-S1X-UPGRADE-LEGACY-HANDOFF** |
| OBS-UPGRADE-ROLLBACK-PIPX | `setup --rollback` 無法回到 pip／pipx 安裝的 0.3.2 | 降級需 §5.2 手動程序 | 是否讓 rollback 以首次安裝備份（entries.json、config、unit）還原 pre-bundle 安裝 → **PENDING-OD-S1X-UPGRADE-ROLLBACK-PIPX** |
| OBS-UPGRADE-NOT-STARTED | 使用者先停 daemon 再 setup 時，setup 的 `start_and_verify` 為 `daemon_was_not_running`、不啟動新 daemon | 需手動 `service start`／`daemon start` | 規格行為（維持原執行狀態），release notes 明寫 |
| OBS-UPGRADE-STALE-CURRENT | 手動降級後 `current-release.json` 仍記 0.4.0；重新升級被記為 `0.4.0→0.4.0` update、Previous 指向 0.4.0 本身 | 重新升級本身成功；但之後的 `setup --rollback` 不會回到 0.3.2 | 併入 PENDING-OD-S1X-UPGRADE-ROLLBACK-PIPX；§5.2 是否加「刪除 current-release.json」步驟一併裁決 |
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
- 升級注意：從 0.3.2 升級前請先 `serialwrap daemon stop`；升級後以 `serialwrap service start`（或 `daemon start`）啟動。`setup --rollback` 不能回到 pipx 的 0.3.2，降級請依文件手動程序。
- 行為變更清單見 `contracts/behavior_changes.yaml`（BC／SC／DF）與 CHANGELOG。

### English
- The `serialwrap` CLI is now native Rust (20 command groups); the daemon is still the bundled Python implementation (Stage 1).
- New bundle install: `bin/serialwrap setup` installs into `~/.local/share/serialwrap/versions/<version>` (Windows: `%LOCALAPPDATA%\Programs\serialwrap`) with atomic update, `setup --rollback`, and a `version_pairing` doctor check.
- Windows: `windows-task` supervision (no admin), optional com0com virtual serial pair (`setup --with-com0com`).
- Fixed: on-demand `daemon start` could not find `serialwrapd.py` under pipx installs.
- Upgrade note: run `serialwrap daemon stop` before upgrading from 0.3.2, then start with `serialwrap service start` (or `daemon start`). `setup --rollback` cannot return to a pipx 0.3.2 install; follow the documented manual downgrade.
```

## 8. PR 政策（release PR）

- 分支：`feature/rust-refactor` → `main`（不得直接 commit 到 `main`）。
- PR body 填 `.github/pull_request_template.md` 的 Policy Checklist（R-11）；引用 issue 一律 `Closes #N`（R-17）。
- label：`release:0.4.0`（R-07：tag 建立前 `VERSION` 與最新 tag 不一致的豁免）。
- 審查：修改 `contracts/`、比較器或哨兵的部分需獨立審閱（§10.4）；PR 描述分列產品與契約變更理由及基線對照。
- merge 後由主 session 建 tag `v0.4.0`（`release.yml` 以 tag 對 `VERSION` 驗證）並確認 GitHub release 的產物與 `SHA256SUMS`。
