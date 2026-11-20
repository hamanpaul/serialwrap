---
type: fix
scope: rust-daemon
---
Rust daemon 的 `session.console_attach` 在內部請求逾時（actor 未及回覆）後，core 才處理到的遲到 attach 改以 detach 語意撤回：除移除沒有 fd 的 console 外，一併關閉它開出的 human lease（ERR-7.5-b），避免留下幽靈 `human:<client_id>` lease 使之後 attach 的 console 取不到 `interactive_session_id` 與 raw ownership；預建 PTY 照舊隨 RPC handler 丟棄、不洩漏 fd。Python daemon 的 attach 為同步呼叫、無此逾時出口，行為不受影響。
