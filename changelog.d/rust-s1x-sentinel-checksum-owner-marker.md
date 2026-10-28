---
type: fix
scope: rust-cli
---

修正 Stage 1B sentinel 扁平 bundle（`scripts/rust/mkbundle.sh --dev --delegate-sentinel`）的 `SHA256SUMS` 缺列問題。先前組裝腳本刻意排除根目錄的組裝期所有權標記 `.mkbundle-owner`，但 Rust 驗證器（`verify_bundle`）要求 bundle 內除 `release-manifest.json` 與 `SHA256SUMS` 外每個檔案都有 checksum，因此 `serialwrap setup --on-demand` 回 `CHECKSUM_VERIFY_FAILED`（`sha256:.mkbundle-owner missing`）。現改為將 `.mkbundle-owner` 納入 `SHA256SUMS`，僅 `release-manifest.json` 與 `SHA256SUMS` 本身豁免；驗證器未放寬。所有權拒絕覆寫（`refuse_foreign_out`）保留不變。本條取代 `rust-s1x-sentinel-flat-bundle.md` 中「`SHA256SUMS` 排除 `.mkbundle-owner`」的敘述。

測試端：`tests/test_rust_mkbundle.py` 新增 `test_sha256sums_lists_every_file_dev`、`test_sha256sums_lists_every_file_sentinel`（除 `release-manifest.json` 與 `SHA256SUMS` 外每個檔案都列於 `SHA256SUMS` 且數值相符）與 `test_setup_on_demand_from_sentinel_bundle_isolated_home`（隔離 HOME 執行 `setup --on-demand` 須 rc=0）。最後一項於非特權 pid namespace 內執行，與 `pidns_rig` 同一隔離手段，避免宿主上無 `--socket` 的生產 daemon 進入 setup 的 safety check。
