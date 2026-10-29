---
type: fix
scope: acceptance
---
L1 鎖 `#[ignore]` 列稽核依 OD-53 只豁免 selector 以 `--ignored cap_<cap>__` 明確執行 ignored 能力測試的集合（如 `s22_gate:console_conformance_pty`，重現命令為 `cargo_l1_results.py --caps posix_pty`），且只豁免符合該 cap 前綴的列；其他集合仍禁止。Slice 2.2 出口判定依 OD-54：跨集合皆 passed、無 failed 判 PASS，但任一份報告內部重複仍判證據不足，不再被其他報告的單列 passed 遮蔽（sol P6-c6ddda1-1）。
