---
type: fix
scope: sw-cli
---

Stage 1B 凍結 selector（`cargo test --workspace` 與 Windows `--package sw-pal --package sw-cli`，皆不含 `--features`）會再編譯兩支委派整合測試。行程內 override 不進入環境變數鉤子；`SERIALWRAP_TEST_DELEGATE_GROUP` 與 `SW_TEST_UPDATE_` 仍只在 `test-fakes` 建置讀取。不恢復 `sw-cli` 自我依賴，正式 release binary 不含那些字串。
