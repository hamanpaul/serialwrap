---
type: fix
scope: sw-cli
---

移除 `sw-cli` 對自身的 dev-dependency。測試鉤子改由 `cargo test --features test-fakes` 啟用，不再以自我依賴打開 `test-fakes`，依賴方向回到 arch §1.3 允許表。正式 release binary 仍不包含 `SERIALWRAP_TEST_`／`SW_TEST_UPDATE_` 字串。
