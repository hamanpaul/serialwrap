---
type: fix
scope: rust-s2-gfx
---
Stage 2 bundle 驗證器依 Python parity 規則放行 CLI 與 daemon 共用的 `SERIALWRAP_TEST_SYSTEM_CONFIG_PATH`，其餘三個 daemon 專用鉤子仍只允許出現在 daemon。CLI 與 daemon 共用的系統設定路徑讀取處都改用 NUL 結尾 C 字串保存，讓二進位掃描仍以完整 token 精確比對；未知鉤子與 `SW_TEST_UPDATE_*` 繼續拒絕。
