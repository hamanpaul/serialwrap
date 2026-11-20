---
type: fix
scope: rust-supervision
---
P8 T13 實跑以正式 `sw-cli` graceful stop 驗證停止流程，並要求 staging daemon WAL 尾端包含可核對的 test-fakes 記錄。
