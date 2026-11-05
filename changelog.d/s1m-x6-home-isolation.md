---
type: fix
scope: rust-cli
---

Rust CLI 解析 systemd user unit 時，環境變數 `HOME` 優先於 passwd 家目錄，路徑固定為 `HOME/.config/systemd/user/serialwrap.service`，不改走 `XDG_CONFIG_HOME`。一般 config 與 state 仍依既有 XDG 與 `SERIALWRAP_*` 覆寫。Rust 黑箱 session 結束時比對真實 user unit 的存在性、mtime 與內容雜湊，與 session 開始不同即失敗，且不改寫該檔。
