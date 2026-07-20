# Proposal: serialwrap-reliability-plugin

## Why

serialwrap 是 wifi_llapi 等 testpilot 測試的信任基礎——broker 壞了其上所有結果都不可信，但 #122 的實機穩定性套件（`python3 -m realhw`）目前只有獨立入口，無法納入 lab 既有的 testpilot 框架（統一 case 選擇、diagnostic_status 四類分診、agent_trace、報表）；同時 PR #143 新增的 `serialwrap remote` 只有全容器 harness 覆蓋（假 UART），缺「部署後 CLI＋live daemon＋真板」的實機驗證；2026-07-19 hp-cycle 事故的 root cause（Windows 原生 serialwrapd 與 WSL 受測 daemon 共享 USB 樹、Windows 端抓走裝置）已查明且可腳本化救援，應內建進套件。

## What Changes

- **Phase 1（realhw 擴充，standalone 可交付）**：
  - 新增 tier `remote` 7 case：`rm-topo-*` ×4（包裝 `tools/docker/remote_tunnel_test.sh` 逐拓樸；容器封閉世界驗工具鏈）＋`rm-live-*` ×3（部署 daemon＋真板穿隧道 e2e／orphan 自癒／open-close 循環）。
  - `CaseResult` 增 `category`／`reason_code` 欄（向後相容），既有 29 case 逐案標註分類；report 增分類欄。
  - `p1-hp-cycle` 內建 Windows 端自動救援鏈（新 driver `WinSwCli` 經 `/mnt/c/...` 呼叫 Windows 端 serialwrap.exe `device release`）；救援失敗才 FAIL＝`FailEnv/windows_daemon_holds_device`。
  - preflight 引入**兩級判決**：suite-refuse（既有六項＋新 `benchlock` flock/pgrep 互斥）與 family-gate（`capabilities`：remote 子命令存在性、部署版本 ≥0.2.3、docker 可達→對應 case 執行期 SKIP 而非整場拒跑）；新 `windows_daemon` 診斷增強。
  - `tools/docker/remote_tunnel_test.sh` 加逐拓樸分派參數（微改）。
- **Phase 2（testpilot plugin 殼，dev-only）**：
  - 新 `reliability/` 發行單位（pyproject＋entry point `testpilot.plugins`；editable-only、永不 release；release wheel 零改動）。
  - `serialwrap_reliability` package：`plugin.py`（PluginBase 薄轉接）＋`core.py`（不 import testpilot 的核心邏輯，CI 可測）＋testbed loader（testbed.yaml 與 config.json 雙來源等價）＋自訂 reporter（md/json 重用 realhw 報告）＋`agent-config.yaml`（sequential、max_attempts=1、remediation=snapshot-only：enabled true 但動作三重鎖死——core 於 disabled 時不寫 failure_snapshot 會使分類全滅）。
  - longrun 以 checkpoint-case 模型走 default run_loop（steps 於 discover 合成、always-pass criteria、判決集中收尾）。

## Capabilities

### New Capabilities
- `reliability-testpilot-plugin`: testpilot-core plugin 轉接層——entry-point 註冊、PluginBase 生命週期映射（prepare_run=preflight gate、execute_step=black-box case.run、evaluate=分類抄寫）、testbed.yaml 組態來源、diagnostic_status 分類契約（受測物反轉原則）、自訂報表。

### Modified Capabilities
- `realhw-stability-suite`: 新增 tier `remote`（7 case）與其 requires 語意；`CaseResult` 分類欄位；preflight 兩級判決（suite-refuse／family-gate）＋benchlock＋windows_daemon 探測；hp-cycle 自動救援行為。

## Impact

- **程式碼**：`realhw/`（harness/drivers/preflight/cases）、`tools/docker/remote_tunnel_test.sh`（微改）、新 `reliability/`；`sw_core/` 與 release wheel **零改動**。
- **測試**：`tests/test_realhw_*.py` 擴充（純邏輯）；plugin core.py 單測；bench 五步真機驗收（standalone 先綠→plugin 冒煙→雙前端一致性→分類落桶→longrun 短跑）。
- **部署**：首次 plugin run 前需 redeploy 0.2.3+remote（營運前置，非本 change 的程式碼範圍）。
- **文件**：README／docs/func-test/realhw-stability-checklist.md 同步 remote 族與 plugin 用法（R-18）。
