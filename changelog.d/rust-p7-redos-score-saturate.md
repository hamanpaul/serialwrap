---
type: fix
scope: rust-p7
---
`sw_proto::pyre::redos`：歧義分數改為 `u64` 並全面採飽和加乘；巢狀固定 `{2}` 每層倍增，原 `u32` 於 32 層（226 字元）溢位——debug 下 panic、release 下回繞為 0 而放行可疑規則。現與 Python 任意精度判定一致（超大分數封頂 `u64::MAX` 仍判可疑）（sol 補審 #2）。
