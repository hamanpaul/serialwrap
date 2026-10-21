---
type: feat
scope: rust-cli
issue: null
---
Rust CLI 原生承接 `connect` 與 `benches`，Windows 亦可使用（移除 `REMOTE_NOT_SUPPORTED`）；bench endpoint 記憶改以 `benches.state.json.lock` 跨行程鎖與原子寫入維護，並沿用 remote 隧道核心。
