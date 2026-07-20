---
type: fix
scope: remote
---
docker 三拓樸驗收 harness（`tools/docker/remote_tunnel_test.sh`）補 review 發現的攻擊者隔離覆蓋缺口：`topology_nat_host`（net_a）與 `topology_dual_nat`（net_a／net_b 雙 NAT）先前僅以 `ss -ltn` 檢查 relay 綁 loopback，未實際驗證「同網段的獨立攻擊者容器」真的連不到——現在兩個拓樸都在 tcp 模式 `-R` 隧道存活期間，另起一個獨立 `attacker` 容器（與 relay 同網段），以 relay 的**非 loopback 容器位址**（`tcp://<relay-container>:7777`，而非合法路徑用的 relay 自身 `127.0.0.1`）嘗試 `daemon status`，斷言真連線失敗（`ok:false` + `SOCKET_ERROR`）；`topology_dual_nat` 額外驗證 net_b 上的 agent 若繞過自身 `-L` 轉發直連 relay 容器位址同樣失敗，證明合法路徑必須經過 relay 自身 loopback。另外 `topology_gatewayports_failclosed` 的 `--remote-socket` 成功案例先前只有 tcp fail-closed 分支有 teardown 收尾複查，現補上同樣的 `assert_default_off`／pid 不變複查，避免該路徑的 teardown 迴歸漏測。攻擊者容器已納入既有 `teardown_now` 回收流程，不留殘留資源。
