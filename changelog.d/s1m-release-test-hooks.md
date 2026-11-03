---
type: fix
scope: sw-cli
---
正式 Rust CLI 僅在 `test-fakes` 建置中讀取 `SERIALWRAP_TEST_DELEGATE_GROUP`。
release profile 不含 `SERIALWRAP_TEST_` 或 `SW_TEST_UPDATE_*` 測試鉤子；
dev bundle 與啟用 `test-fakes` 的 Cargo 測試仍可使用委派覆寫。
