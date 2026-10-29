---
type: fix
scope: rust-s21
---
sol `c4cfb96..4f8acdf` 修正：(1) Slice 2.1 gate 判定器對鎖中未被任何 section 承擔的 `s21_gate:*` 集合（如 OD-55 的 linux 能力集合 sys_admin／windows_native／interactive_desktop／supervisor_runtime）判 INCOMPLETE 並列名，不得 READY；(2) `wal_s1w` 量測輸出記錄 `env.source_tree_dirty`，gate 匯入要求為 false，可追溯路徑加入 workspace 與 crate `Cargo.toml`；(3) Rust 鎖後 holder 掃描不完整的 WARNING 改印 Python `report.status` 字串（`status=permission`），與 `daemon_logging.yaml` `uart_holder_scan_unknown` 逐字一致。
