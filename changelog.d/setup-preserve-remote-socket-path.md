---
type: fix
scope: setup
---
POSIX backend 的 `serialwrap setup --on-demand` 在同模式刷新時保留
`config.yaml` 既有有效 TCP `socket_path`，包含 loopback SSH tunnel；
不因 endpoint 暫時無法連線而改寫為本機 Unix socket。

- 保存規則限定目前與目標都是 on-demand、未顯式指定 endpoint 的情境；
  systemd、模式轉換與 win backend 維持既有契約。
- `./install.sh --on-demand` 可轉交模式旗標；未帶旗標的 auto 安裝在有
  systemd 時會轉換模式，不能視為本次已修復的遠端客戶端流程。
- 顯式 `--socket`／`--endpoint` 仍供探測使用，setup 寫回 canonical
  socket，並非持久化旗標值。詳見 `docs/setup-tcp-endpoint.md`。
- 原 PR 的四個新增案例改為隔離測試矩陣，補重跑冪等、一般 RPC 路由、
  斷線保存及各排除邊界；不使用真實 TCP endpoint 或系統控制指令。
- 還原與產品修復無關的 inline policy workflow 與自動豁免說明；R-12
  的中央 conventions 問題另行處理，本變更不繞過規則。
