---
type: changed
scope: rust-refactor
---

- `device.release` 驗證並正規化外部來源；`session.interactive_open` 拒絕 daemon 保留 owner，並保留 bare `human` 原值。
