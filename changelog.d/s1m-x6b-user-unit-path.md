---
type: fix
scope: rust-cli
---
Rust CLI 的 systemd user unit 寫入、更新快照、失敗還原與 hold 計畫共用 `HOME/.config/systemd/user` 路徑，避免誤用 XDG 或 passwd 家目錄；一般 config／state 維持既有覆寫優先序。
