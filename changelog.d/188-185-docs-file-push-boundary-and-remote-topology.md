---
type: docs
issue: 188
scope: docs
---
釐清兩條對外契約的邊界，皆為純文件變更、不動實作。

- **`file push` 的檔案大小與通道界線（#188）**：`SKILL.md` 的「避免 base64 inline」原本只寫
  「改用 `serialwrap file push`」，照這條規則走 agent 會把整包 firmware image 也交給
  `file push`。實測一顆 81 MB image 走 SCP 經 LAN 數秒完成；同一顆走 UART 115200
  （原始上限約 11 KB/s，echo-ACK 節流後更低）即使通道穩定也需數小時。現在明訂
  `file push` 適用**數十 KB 內的小檔**（設定檔／腳本／探針），DUT 有 SSH／TFTP／HTTP
  通道時大檔一律走 SCP／TFTP、serialwrap 只負責控制面，`file push` 用於大檔僅能是
  「確認無網路通道後」的明確 fallback。`README.md`（中英雙語）與 `SKILL.md` 的範例
  一併從 `./firmware.bin` 改為 `./probe.sh`——原範例本身正是要避免的反面示範。
- **`serialwrap remote` 的責任邊界與拓樸模型（#185）**：明訂 `serialwrap remote`
  **不實作 NAT traversal**，只在一個本來就可達的 SSH target 上管理 forward lifecycle；
  可達性由外部 reachability provider 提供（LAN／Cloudflare Zero Trust／Tailscale／
  WireGuard／ZeroTier／VPN／jump host），新增責任分層圖。拓樸重新分級：overlay 已提供
  互相可達性時，`UART host -R→ agent` 為 **preferred**（即使兩端皆在 NAT 後也不需要
  relay 或成對的 `-L`）；public relay 的 `-R/-L` 鏈降為兩端完全互不可達時的
  **fallback**，不再是雙 NAT 的唯一描述。明文記錄刻意**不提供** `--cloudflare`／
  `--tailscale` 之類 provider-specific 旗標、不引入 provider SDK 或執行期依賴——
  provider 細節屬於 `ssh_config` alias／`ProxyJump`／`--ssh-opt`。既有 loopback bind
  不變量、`--remote-socket` 硬化與單租戶 relay 要求在所有拓樸下維持不變。

同時涵蓋 #185。`code_paths`（`**/*.py`／`**/*.sh`／`scripts/**`）未變動，本 fragment
為自願記錄以利 release 收斂。
