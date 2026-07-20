---
type: feat
issue: 122
scope: realhw
---
新增實機穩定性測試套件 `python3 -m realhw`（#122）：P0 煙霧×8＋P1 核心穩定性×20（console 對抗／重啟恢復／裝置交接／usbipd 插拔／命令執行／WAL）全自動、長跑（`lr-mixed`，預設 32h）無人看護＋事後分析報告（`~/b-log/realhw-reports/`，含 `snapshots.ndjson`／`events.ndjson`／`longrun-analysis.md`）；preflight 守門（部署新鮮度／doctor／兩板 READY／工具可用／環境乾淨／破壞性預告，任一不過整場拒跑）；continue-on-failure 執行引擎＋逐 case evidence 與診斷提示。`docs/func-test/realhw-stability-checklist.md` 人可讀清單含 P0/P1 逐 case 手動等效命令與 P2 手動程序（MCU flash `/dev/ttyMCU`、U-Boot recovery lease、self-test 全譜、監管模式轉換、Windows loopback）。**測部署後系統**（用已安裝 `serialwrap` CLI 操作 live daemon 與真板，不 import `sw_core`、不進 CI、不入 wheel、pytest `tests/` 不收集）。
