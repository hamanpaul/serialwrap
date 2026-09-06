---
type: change
issue: 193
scope: remote
---
補上 `serialwrap remote` 的 **agent-pull** 形狀：測試覆蓋、文件範例，以及兩處會誤導的敘述。
不改 `sw_core/` 任何實作——現行程式碼本來就支援，缺的是驗證與文件。

- **agent-pull 形狀**（`serialwrap remote -L --remote-socket <UART host 的 socket> user@dut:7777`）：
  agent 主動 ssh 進 UART host、把對方的 `serialwrapd.sock` 拉回自己的 loopback，
  **UART host 端不需跑 `-R`、不需任何 relay**。這是「開發機要連一台在 NAT 後的 bench」
  最自然的形狀，搭配把 SSH 架在 overlay 上的 provider 即得到兩端皆只有出站連線的路徑
  （遠端桌面工具用的模型），且不必自己營運 relay。
  `sw_core/remote_tunnel.py` 早已支援——`_direction_args()` 的 connect 分支對「對端是誰」
  無任何假設，全檔亦無強制 `-L` 必須配 `-R` 的檢核——但三個既有 docker 拓樸
  （`direct`／`nat_host`／`dual_nat`）**全是 UART host 用 `-R` 推出去**，方向相反的
  agent-pull 從未被測過，README／SKILL 也沒有任何範例。
- **新增第 4 個 docker 拓樸 `agent_pull`**（`tools/docker/remote_tunnel_test.sh`）：
  方向反轉，sshd 改跑在 uart 容器、agent 容器主動連入。斷言粒度比照 `topology_direct()`
  ——①預設不啟用 ②session list＋cmd submit/status ③daemon pid 全程不變
  ④close all 後乾淨復歸 ⑤loopback 不變量＋獨立 attacker 容器連不到。
  `realhw/cases/remote.py` 註冊 `rm-topo-agent-pull`（`remote` tier ×7 → ×8）。
  - 兩個新 helper：`uart_sshd_up()` 強制以 `-u root` 在 uart 容器起 sshd
    （`start_uart` 用 `docker run -u tester`，`docker exec` 不帶 `-u` 會繼承該非 root
    使用者、讀不到 0600 的 host key 而 `no hostkeys available -- exiting`；既有
    `sshd_up()` 只用在 `start_plain`/`start_role` 建的 root 容器上，未踩過此坑）；
    `uart_socket_path()` 讀 uart 容器的 `sw-uart.env` 推算 `RUN_DIR/serialwrapd.sock`
    （`DaemonHarness` 用 tempdir，路徑每次啟動都不同，不能寫死）。
- **`BatchMode=yes` 為強制且不可覆寫**（README 中英雙語＋SKILL.md）：readiness／安全預設
  `-o` 置於使用者 `--ssh-opt` 之前，OpenSSH 取同鍵第一個值，故蓋不掉（刻意如此）。
  後果是不接受任何互動式認證；provider 憑證會過期時（如 Cloudflare Access token），
  過期會讓 ssh 直接失敗且無有用診斷，無人值守需用 service token 或事先 refresh。
  此限制先前完全沒有記載。
- **修正 `--remote-socket` 的配對敘述**：原文「`-R`／`-L` 兩端須成對指定同一路徑」的脈絡
  是共享 relay 的硬化情境，但照字面讀會讓人以為 agent-pull 不合法。改為明確界定該配對
  要求只適用 relay 類形狀。
- 順帶修掉 `role_exec` 一段「connect 端（-L，僅拓樸 3 用）」的過期註解（拓樸 4 也用它）。

**regression-case 評估**：本變更的驗證面**就是** `regression/`／realhw 這一側——新增的
`rm-topo-agent-pull` 即為回歸 case，涵蓋 pytest 無法覆蓋的部分（真 sshd、真 ssh forward、
真 unix socket 轉發、跨容器隔離）。不需另加 pytest：`sw_core/` 零改動，無新的 in-process
行為可測；既有 `tests/test_remote_tunnel.py`／`test_remote_cli.py` 已覆蓋 argv 組裝與 CLI 解析。
