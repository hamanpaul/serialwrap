# serialwrap 安裝流程設計：pipx 打包 + systemd 服務（on-demand 降級備援）

> 日期：2026-06-22 ｜ 狀態：設計定稿（待轉 OpenSpec proposal / 實作計畫）
> 範圍：把 serialwrap 從「`install.sh` copy 到 `~/.paul_tools` + 手動 PATH + 裸借系統 PyYAML + 按需自啟 daemon」轉為**正規可發佈安裝**：pipx + `git+SHA` 取得、systemd 服務常駐為主、無 systemd 平台退回現有按需自啟。

## 1. 背景與問題

serialwrap 是「常駐 daemon（獨佔 `/dev/ttyUSB*`、維持狀態、唯一 reader）＋輕量 CLI client（走 Unix socket）」這一類工具，需要 OS 層裝置權限（`dialout`）。現況安裝有以下缺口：

- ❌ 無打包（無 `pyproject.toml`）→ 不能 `pip/pipx install`、無 entry point、無版本化產物。
- ❌ 依賴未宣告、**裸借系統 PyYAML**（換機/換 python 即可能失效）。
- ⚠️ 無服務管理（不開機自啟、crash 不自動拉回，靠 `minicom_router.sh` 的 `AUTO_START_DAEMON` 順手帶起）。
- ⚠️ 狀態放 `/tmp/serialwrap`（非持久、會被 `/tmp` reaper 掃——本專案已實際遇過）。
- ⚠️ 裝置權限（dialout）、PATH 都靠人工/文件；`minicom` wrapper 靠 PATH 搶先 shadow 真 minicom（脆弱）。

## 2. 目標決策（已與使用者確認）

| 維度 | 決策 |
|---|---|
| 部署情境 | **對外發佈、要能在不受控環境裝起來** |
| 分發管道 | **pipx + `git+SHA`**：`pipx install "git+https://github.com/hamanpaul/serialwrap@<tag或SHA>"`（沿用 org 既有 pin-SHA 慣例，免 PyPI 帳號） |
| 平台 | **Linux only（含 WSL2）**；不含原生 macOS/Windows（serial 路徑/權限模型不同） |
| 套件範圍 | **全集**：core CLI/daemon + 預設 profiles + minicom wrappers + agent skill |
| daemon 生命週期 | **方案 C（hybrid）**：systemd 服務為主；無 systemd 平台**退回現有按需自啟（on-demand spawn）作為降級備援** |
| 建置 backend | **setuptools**；資產 **relocate 進套件** 後以 `importlib.resources` 取用 |
| minicom | wrapper 以 `command -v minicom` 解析真本體，**不再 PATH-shadow `minicom`** |

非目標（YAGNI）：原生 macOS/Windows 支援；PyPI 公開發佈；systemd socket-activation（`.socket` 單元）。

## 3. 架構：監管模型（supervision model）

三種模式，且全系統以**單一事實來源**決定誰負責 daemon：

| 模式 | 何時 | 怎麼跑 | 開機自啟 |
|---|---|---|---|
| `systemd-user`（預設） | 偵測到 systemd（含已啟用的 WSL2） | `systemctl --user` unit，`ExecStart=%h/.local/bin/serialwrapd`，跑安裝者本人身分 | `loginctl enable-linger $USER` |
| `systemd-system`（`--system` opt-in） | lab 機全機共用一份 | `/etc/systemd/system/serialwrap.service`，專屬 `serialwrap` 服務帳號（在 `dialout`） | systemd 原生 |
| `on-demand`（降級備援） | 無 systemd 或無法啟用 | 現有機制：連不到就 spawn + `SingletonLock` | 無（跟著 session） |

**消除 systemd 與 on-demand 互搶的競態**（本專案已親見：殺 daemon 後 `minicom_router` 立即重拉）：

- `serialwrap setup` 決定模式後記錄為單一事實來源：`~/.config/serialwrap/config.yaml` 的 `supervision_mode`。
- **所有自動 spawn 路徑（CLI lazy-start、`minicom_router` 的 `AUTO_START_DAEMON`）先讀此模式**：systemd 模式 → 一律不自 spawn，連不到就回明確錯誤（提示 `systemctl --user start serialwrap`）；on-demand 模式 → 維持現狀。
- `SingletonLock` 仍為最後一道防線（防任何情況雙開）。

**WSL 銜接**：`setup` 偵測 systemd；未啟用則引導寫 `/etc/wsl.conf` 的 `[boot] systemd=true` + 提示 `wsl --shutdown`；使用者重啟 WSL 前先以 `on-demand` 運作，重啟後 `setup` 再切 `systemd-user`。

## 4. 打包（`pyproject.toml`）

- **建置系統**：PEP 621 `pyproject.toml` + setuptools backend。
- **套件內容與 entry points**：
  - package = 現有 `sw_core/`（含 `sw_core/event_engine/`）。
  - console_scripts：`serialwrap = sw_core.cli:main`；`serialwrapd = sw_core.daemon:main`——把 root `serialwrapd.py` 的 `main()`／`BLOCKING_RPC_METHODS` 搬進 `sw_core/daemon.py`；root `serialwrapd.py` 保留為薄 shim（`from sw_core.daemon import main`）以相容既有 `--socket …` 呼叫與 systemd `ExecStart`。
- **依賴與門檻**：`dependencies = ["PyYAML>=6"]`（唯一第三方 runtime dep；pyserial 不需要，從 Dockerfile 移除）；`requires-python = ">=3.10"`（程式用 `X | None`、`set[str]`）。
- **資產隨輪子打包**：預設 profiles、minicom wrappers、agent skill、關鍵 docs **relocate 到套件內**（如 `sw_core/assets/{profiles,tools,skill}`），runtime 以 `importlib.resources` 讀出 materialize。連帶調整見 §8（`install.sh`/README 路徑/`constants.py` 預設/skill symlink 目標/func-test）。
- **版本**：`pyproject` 版本與既有 `VERSION`（現 0.1.0）同源；對外切 git **tag**（`v0.1.0`…）供 `@vX.Y.Z`/`@<sha>` pin。

## 5. 後安裝流程：`setup` / `doctor`

pipx 裝完只放好 binary；其餘靠兩個冪等子命令（pip 無 post-install hook，故顯式化）。

### `serialwrap setup`（裝完跑一次，可重跑；為 reconciler）

1. **Materialize 資產**（`importlib.resources` → 使用者可寫位置）：
   - profiles → `~/.config/serialwrap/profiles/`（已存在不覆蓋；`--force` 才覆寫）。
   - agent skill → 物化到 `~/.local/share/serialwrap/skill`，再 symlink `~/.agents/skills/serialwrap` 指過去（不直接指 pipx venv 內部，升級不斷鏈；重跑刷新）。
   - minicom wrappers → `~/.local/bin/serialwrap-minicom`（等），內以 `command -v minicom` 找真本體，不 shadow `minicom`。
2. **決定並落地監管模式**（見 §3）：偵測 systemd → 預設 `systemd-user` unit + `loginctl enable-linger`；`--system` 改系統級；WSL 無 systemd → 引導開機旗標後先 `on-demand`。最後寫 `supervision_mode` 進 `config.yaml`。
3. **dialout 檢查**：不在群組 → 印 `sudo usermod -aG dialout $USER` + 重登提示。
4. **旗標**：`--user`/`--system`/`--on-demand` 強制模式；`--force` 覆寫 profiles；`--yes` 非互動（CI）；`--uninstall` 反向移除 unit/linger/symlink。

**sudo 邊界**：setup 全程 user-level；唯三需要 root 的動作——加 dialout、寫 `/etc/wsl.conf`、`--system` unit——**預設只印確切指令**，`--with-sudo` 或互動 y/N 明確同意才代跑，**絕不靜默 sudo**。

### `serialwrap doctor`（唯讀診斷，印可貼修復指令）

逐項檢查：python 版本／PyYAML／`serialwrap`+`serialwrapd` 在 PATH／dialout 成員／systemd 可用性與 unit 狀態／`supervision_mode`／socket 可連／`/dev/serial/by-id` 裝置可見／（WSL）`/etc/wsl.conf` systemd 旗標。

### README

新增/改寫「安裝」段為標準流程：

```bash
pipx install "git+https://github.com/hamanpaul/serialwrap@v0.1.0"
serialwrap setup     # 物化 profiles/skill/minicom、設定服務
serialwrap doctor    # 驗證環境
```

附 dialout / WSL-systemd 的 sudo 一行指令、on-demand 降級說明；並修掉現有 README「預設 /usr/local/bin」與實際 `~/.paul_tools` 不符的漂移。

## 6. 路徑/XDG 與跨重裝的模式轉換

### 6.1 路徑模型（脫離 `/tmp`，env 覆寫全保留）

| 類別 | user 範圍（`systemd-user`/`on-demand`） | 系統範圍（`systemd-system`） | env 覆寫 |
|---|---|---|---|
| 設定（profiles、`config.yaml`） | `$XDG_CONFIG_HOME/serialwrap`（`~/.config/serialwrap`） | `/etc/serialwrap` | `SERIALWRAP_PROFILE_DIR` |
| 狀態（`state.json`、`wal/`、log） | `$XDG_STATE_HOME/serialwrap`（`~/.local/state/serialwrap`） | `/var/lib/serialwrap` | `SERIALWRAP_STATE_DIR`/`_WAL_DIR`/`_LOG_DIR` |
| Runtime（socket、lock、ttyMCU） | `$XDG_RUNTIME_DIR/serialwrap`（缺則退 `state/run`，不再 `/tmp`） | `/run/serialwrap`（unit `RuntimeDirectory=`） | `SERIALWRAP_RUN_DIR` |
| 資料（物化 skill/minicom） | `$XDG_DATA_HOME/serialwrap`（`~/.local/share/serialwrap`） | `/usr/local/share/serialwrap` | — |

- CLI 與 daemon 用同一份 `constants.py` 解析，必然一致。系統模式下 root 無 `$XDG_RUNTIME_DIR`，故 socket 走固定 `/run/serialwrap/serialwrapd.sock`、mode 660 +（`dialout`/`serialwrap`）群組；**`config.yaml` 記錄「有效 socket 路徑與模式」讓任何使用者跑的 CLI 找得到、連得上**。
- env 覆寫優先級最高（既有自動化/throwaway daemon 沿用無痛）。

### 6.2 跨重裝模式轉換（`setup` 作 reconciler）

1. 讀 `config.yaml` 舊 `supervision_mode`（首次為空）。
2. 解析目標模式（auto-detect 或旗標）。
3. 目標 == 舊 → 冪等刷新（重物化資產、確保 unit enabled），不拆。
4. 目標 ≠ 舊 → 受控轉換，順序嚴格：
   1. **先在舊機制下停掉現役 daemon**（on-demand→`daemon stop`；systemd→`systemctl [--user] stop/disable`，視情況撤 linger）——**務必先關舊、釋放 tty FD，再起新**，否則交接瞬間兩 process 同開 `/dev/ttyUSB*` 變 two-reader（`SingletonLock` 只擋同 socket、擋不了同實體裝置）。
   2. 必要時遷移 state（user XDG ↔ `/var/lib` 之間 copy `state.json`，保住 sessions/alias/RELEASED map）。
   3. 起新機制（裝/enable unit 或設 on-demand）→ 更新 `config.yaml` 的 mode。
   4. **驗證恰好一個 daemon**：`SingletonLock` + 收尾 health/doctor。
5. 轉換後新 mode 立即生效於 §3 的 auto-spawn gate（舊 `minicom_router` AUTO_START 不會復活第二個 daemon）。

**安全護欄**：轉換前若偵測任一 session 在 `FLASHING` 或有進行中傳輸/前景命令，中止並明確報錯（沿用 `FLASHING_BUSY` 哲學），`--force` 才強制——避免重裝/切模式切斷燒錄或傳輸。

**WSL 典型**：首裝無 systemd→`on-demand`；開 `[boot] systemd=true` + `wsl --shutdown`（舊 daemon 隨 WSL 重啟消失）→ 重跑 `setup` 偵測 systemd→乾淨 stand-up `systemd-user`（無舊 daemon 要拆，轉換退化成單純安裝）。

## 7. systemd unit 設計

**User unit**（`~/.config/systemd/user/serialwrap.service`）

```ini
[Unit]
Description=serialwrap UART broker daemon (user)
After=default.target

[Service]
Type=simple
ExecStart=%h/.local/bin/serialwrapd
Restart=on-failure
RestartSec=2
# 不可設 PrivateDevices=/DeviceAllow 等沙箱——會把 /dev/ttyUSB* 藏起來

[Install]
WantedBy=default.target
```

+ `loginctl enable-linger $USER`。

**System unit**（`/etc/systemd/system/serialwrap.service`）

```ini
[Service]
Type=simple
User=serialwrap
SupplementaryGroups=dialout
RuntimeDirectory=serialwrap        # → /run/serialwrap
StateDirectory=serialwrap          # → /var/lib/serialwrap
ConfigurationDirectory=serialwrap  # → /etc/serialwrap
ExecStart=/usr/local/bin/serialwrapd --socket /run/serialwrap/serialwrapd.sock
Restart=on-failure
RestartSec=2

[Install]
WantedBy=multi-user.target
```

- socket mode 660 +（`dialout`/`serialwrap`）群組，讓其他使用者 CLI 連得上；CLI 由 `config.yaml` 得知有效 socket 路徑。需 root 安裝；daemon 須系統級可執行（`sudo pipx install --global` 或物化到 `/usr/local`）。
- `Restart=on-failure`：crash 自動拉回、clean stop 不亂重啟；重啟後 DeviceWatcher 重新認線、`state.json` 的 RELEASED 跨重啟仍被尊重。
- **明確禁用會擋 `/dev` 的沙箱指令**（此類工具最常見踩雷）。

**service 操作收斂**：`serialwrap service {start|stop|restart|status}` 依 `config.yaml` 模式轉成對應 `systemctl [--user]`；systemd 模式下 `serialwrap daemon stop` 重導到 `service stop`（避免被 systemd 重啟的混淆）；on-demand 維持原低階語意。

**刻意排除（YAGNI）**：socket-activation（需 daemon 接 `sd_listen_fds`、改 `JsonRpcUnixServer` 自建 socket 的碼）；`Restart=on-failure` + enable-linger 已滿足。

## 8. 向後相容 / 遷移既有 `~/.paul_tools` 安裝

1. **重複 binary 去歧義**：pipx 放 `~/.local/bin`、舊的在 `~/.paul_tools`，PATH 誰先誰贏會混淆。`setup` 偵測 legacy 安裝 → 警告 + 提議退役：停 legacy daemon、移除/備份 `~/.paul_tools` 的 `serialwrap`/`serialwrapd.py`/`minicom` 影子、提示拿掉 `export PATH=~/.paul_tools`。
2. **state 遷移**：首次 setup 把 legacy `/tmp/serialwrap/state.json` 搬到新 XDG state（接 §6.2「先停舊再起新」）。
3. **minicom 影子退役**：舊 `~/.paul_tools/minicom` symlink → 換 `serialwrap-minicom`（`command -v minicom`）；提示移除舊 symlink。
4. **`minicom_router` AUTO_START 收斂**：新版 router 讀 `supervision_mode`——systemd 模式不再 auto-spawn，改 `systemctl --user start` 或明確報錯。
5. **env 覆寫全保留**：所有 `SERIALWRAP_*` 照舊 → 既有 throwaway-daemon / CI 隔離跑法（真機驗證 playbook）原封不動可用。
6. **`install.sh` 轉型**：改成 `pipx install "<repo>" && serialwrap setup`（dev/本地用）；舊「copy 到 ~/.paul_tools + 裝 skill」由 pipx + setup 取代並標 legacy。
7. **Dockerfile 對齊**：改 pip/pipx 安裝、移除 pyserial、容器無 systemd → `setup` 自動退 on-demand；維持當 CI/remote-lab smoke 影像。

## 9. 測試策略

1. **單元測試（pytest）**——設計約束：**`setup`/`doctor` 的副作用走可注入的「effects 介面」**（執行指令、偵測 systemd、檔案操作經它），不散落 `subprocess`/`os` 直呼，方能無 root/無 systemd 單元測試：
   - mode 偵測、轉換決策（舊→新）、state 遷移、冪等、資產物化（到 temp HOME）、dialout 訊息、不靜默 sudo、auto-spawn gate（systemd 模式 CLI/router 該報錯不該 spawn）。
   - XDG 路徑解析 + env 覆寫優先；`doctor` 判定；pyproject metadata（entry points 可解析、`import sw_core`）。
2. **打包 smoke（CI）**：build wheel → venv/pipx 裝 → 斷言 entry point 在、`serialwrap doctor` 能跑。掛進現有 `tests.yml` 或新增 `package.yml`。
3. **容器 smoke（Dockerfile，無 systemd）**：`setup` 退 on-demand → daemon spawn → 用 func-test `fake_target` round-trip。
4. **真機驗證（手動，沿用既有方法論）**：WSL2 上 (a) 全新 `pipx install @<sha>` + `setup`（systemd 已啟用）→ `systemctl --user status serialwrap` active、認線、`cmd submit` 通；(b) 轉換情境：無 systemd 裝（on-demand）→ 開 WSL systemd → 重跑 setup → 驗證乾淨轉成 systemd-user、單一 daemon（無 two-reader）、state 保留。以 env 覆寫隔離不擾其他工作。

## 10. 政策對齊（落地時）

- 分支 `feature/<slug>`（R-12）；`CHANGELOG.md [Unreleased]`、`VERSION`（若改版）、README 安裝段同步；`pytest`／`policy_check` 綠；四份 agent 檔若有改則同步；PR 若對應 issue 用 `Closes #N`。
