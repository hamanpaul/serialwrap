---
type: fix
scope: rust-p7
---
Rust daemon 的 event handler 子行程於 exec 前清空 signal mask 並將 SIGPIPE／SIGXFSZ 還原預設（與 Python `subprocess` 子行程一致）：daemon 以 sigwait 阻擋 SIGINT／SIGTERM，先前 handler 的背景 job 繼承被阻擋的 SIGTERM，逾時 `killpg(SIGTERM)` 殺不掉、`fire_timeout` 要等背景行程自然結束才產生。
