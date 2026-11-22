---
type: fix
scope: rust-s2
---
dev profile 將 `sha2` 以 `opt-level = 3` 編譯（`[profile.dev.package.sha2]`），消除黑箱更新測試（`test_cli_mgmt_update.py`、`test_cli_mgmt_l15.py` 共 6 支）在 hosted CI 成批逾時。根因是 `mkbundle.sh --dev` 的 debug bundle 約 230 MiB，`setup` 在預檢（`safety_check`）前要對來源與 staging 各驗一次、共約 850 MiB 的 SHA-256，而未最佳化的 sha2 只有約 60 MiB/s（軟體後端約 30 MiB/s）：本機到 `handoff_stop:replied` 掛鉤即需 12.7～15.9 s，2 核加負載時 20.2～22.9 s，超過測試的 20 s 等待上限。最佳化後同一段為 1.1～1.2 s（2 核加負載 1.7～2.1 s）。只影響 dev／test profile；release profile 設定與正式候選執行檔位元組不變，測試判準與等待上限未改。
