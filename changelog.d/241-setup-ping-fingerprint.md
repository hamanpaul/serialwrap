---
type: feat
scope: rust-refactor
issue: 241
---

Rust CLI `setup` 使用既有 `health.ping` 回應與安裝 manifest 比對 daemon 程式碼指紋；無法比較時省略 `daemon_code`。
