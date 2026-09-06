---
type: change
issue: 195
scope: remote
---
README（中英雙語）與 `SKILL.md` 新增「以 Cloudflare 當 reachability provider」的可操作實例。純文件變更，`sw_core/` 零改動。

- **情境**：agent 跑在本機、bench 在遠端，兩端皆在 NAT 後；本機不裝 overlay client、不對 bench 建立系統級 ssh 關係——只有 `serialwrap remote` 派生的那條 ssh 過去；不自養 relay VPS。這是 #193 拓樸二（agent-pull）的實例化：Cloudflare Tunnel 讓 bench 的 sshd 以 hostname 可達，bench 只跑 `cloudflared`、**不跑 `serialwrap remote`**；本機的 `cloudflared` 只是 serialwrap 那條 ssh 的 per-connection `ProxyCommand` helper，透過 `--ssh-opt` 傳入而非寫進 `~/.ssh/config`。
- **兩條路線**：Quick Tunnel（無帳號無網域，hostname 每次重啟換，`cloudflared tunnel --url ssh://localhost:22` 一行）與 Named Tunnel 不開 Access（帳號裡有網域，hostname 固定，`tunnel login` → `create` → `route dns` → `config.yml`）。本機指令兩條路線相同。附對照表、bench 一次性前提（sshd key-only／pubkey／`dialout`）、以及「先用純 ssh 驗 ProxyCommand」的里程碑。
- **刻意不寫 Access**：它是把服務發佈給組織的企業形狀工具，對「我連我自己的 bench」是錯的量級；其瀏覽器登入 token 會過期、撞上 serialwrap 強制的 `BatchMode=yes` 會靜默失敗。只註明日後要加需用 service token。
- 附自養 relay（拓樸三）與 tmate 的一句話對照，避免讀者再繞一次：前者功能等價、差在誰養會合點；後者同為「別人養 relay＋token 會合」但只橋 terminal 不傳 socket。
- 寫進文件的 `cloudflared` 旗標（`tunnel --url`／`access ssh --hostname`／`--service-token-id`／`--service-token-secret`／`tunnel route dns [TUNNEL] [HOSTNAME]`／`service install`）已對 cloudflared 2026.8.3 的 `--help` 逐一核對；`ssh://` ingress scheme 以離線 `tunnel ingress validate` 驗證為 OK。未實際開 tunnel（會把本機 sshd 曝到公網），Cloudflare 端到端待帳號加上網域後由使用者實跑。
