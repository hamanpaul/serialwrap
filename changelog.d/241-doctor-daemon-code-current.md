---
type: feat
scope: rust-refactor
issue: 241
---

Rust CLI `doctor` 會以 bundle manifest 的 Python daemon 指紋比對 `health.ping` 回報；指紋不同或舊 daemon 未回報時列為 advisory，不改變既有 `version_pairing` 判定。
