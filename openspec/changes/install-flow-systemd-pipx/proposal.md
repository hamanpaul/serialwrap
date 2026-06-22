## Why

serialwrap 目前只能靠 `install.sh` 把檔案 copy 到 `~/.paul_tools` + 手動 PATH + 裸借系統 PyYAML + 按需自啟 daemon，無法對外發佈、依賴脆弱、daemon 不常駐、狀態放 `/tmp` 會被 reaper 掃。本變更把安裝流程轉為正規可發佈型態，讓不受控的外部環境也能可靠裝起並常駐。

完整設計見 `docs/superpowers/specs/2026-06-22-install-flow-systemd-pipx-design.md`。

## What Changes

- 新增 `pyproject.toml`（setuptools），提供 `serialwrap` / `serialwrapd` 兩個 console_scripts、宣告 `PyYAML>=6` 依賴與 `requires-python>=3.10`；資產（profiles、minicom wrappers、agent skill、關鍵 docs）relocate 進套件，runtime 以 `importlib.resources` 取用。
- 對外安裝改為 `pipx install "git+https://github.com/hamanpaul/serialwrap@<tag或SHA>"`（沿用 org pin-SHA 慣例）；切 git tag 供 pin。
- daemon 生命週期改以 **systemd 服務為主**（`systemd-user` 預設、`--system` 選項），**無 systemd 平台退回現有 on-demand spawn 作降級備援**；以 `config.yaml` 的 `supervision_mode` 為單一事實來源，**gate 掉所有 auto-spawn 路徑**杜絕雙 daemon 競態。
- 新增 `serialwrap setup`（冪等 reconciler：物化資產、決定/落地監管模式、dialout/WSL 引導、跨重裝模式轉換「先停舊再起新」、不靜默 sudo、flash/傳輸進行中護欄）與 `serialwrap doctor`（唯讀診斷）。
- 路徑改走 XDG（脫離 `/tmp`），保留所有 `SERIALWRAP_*` env 覆寫；新增 `serialwrap service {start|stop|restart|status}` 包裝 systemctl。
- minicom wrapper 改以 `command -v minicom` 解析真本體，**不再 PATH-shadow `minicom`**。
- 向後相容：偵測並引導退役既有 `~/.paul_tools` 安裝、遷移 `state.json`；`install.sh` 轉型為 `pipx install + serialwrap setup`；Dockerfile 對齊（移除未用 pyserial、容器退 on-demand）。
- **BREAKING**（行為變更）：systemd 模式下 CLI/`minicom_router` 不再自動 spawn daemon，連不到時改回明確錯誤並提示啟動服務。

## Capabilities

### New Capabilities
- `packaging-distribution`: pyproject 打包、console_scripts entry points、依賴與 Python 門檻、資產隨輪子打包、版本/tag 與 pipx+git 安裝指令。
- `runtime-paths`: XDG 路徑模型（依監管範圍切 user/system）、`SERIALWRAP_*` env 覆寫優先序、`config.yaml` 記錄有效 socket/模式、`/tmp`→XDG 狀態遷移。
- `daemon-supervision`: 三種監管模式與單一事實來源、auto-spawn gate、systemd user/system unit（Restart/不可 /dev 沙箱）、on-demand 降級備援、`service` 子命令。
- `install-setup`: `serialwrap setup` reconciler（資產物化、模式決策與轉換、dialout/WSL 引導、sudo 邊界、flash 護欄）、`serialwrap doctor` 診斷、`~/.paul_tools` legacy 遷移。

### Modified Capabilities
<!-- 既有 capability（device-handoff / mcu-flash-broker / session-* ）的 REQUIREMENTS 不變，故無 -->

## Impact

- 新增：`pyproject.toml`、`sw_core/daemon.py`、`serialwrap setup`/`doctor`/`service` 子命令、systemd unit 範本、`sw_core/assets/`（relocated）。
- 修改：`sw_core/constants.py`（XDG 路徑預設）、`sw_core/cli.py`（新子命令、auto-spawn gate）、root `serialwrapd.py`（轉薄 shim）、`tools/minicom_router.sh`（讀 supervision_mode、`command -v minicom`）、`install.sh`、`Dockerfile`、`README.md`（安裝段）、CI workflow（打包 smoke）。
- 依賴：明確宣告 `PyYAML`（移除 Dockerfile 多餘 pyserial）。
- 相容：保留所有 `SERIALWRAP_*` env 覆寫（既有 throwaway-daemon / CI 隔離跑法不受影響）；偵測並遷移既有 `~/.paul_tools` 安裝。
