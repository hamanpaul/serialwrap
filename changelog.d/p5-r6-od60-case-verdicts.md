---
type: change
scope: rust-s21
---
OD-60：Slice 2.1 gate 斷路器輸入 `stage2_estimate_weeks` 填 6 週；新增 `slice21_gate_judge.py case-verdicts`，依 l15_ab（Python／Rust 兩邊逐案 outcome）、移出 l15_ab 者的承擔測試與 func-test runner 結果彙整 `case_verdicts.json`（每案取最差判定 FAIL＞EVIDENCE_MISSING＞PASS，缺證據即 EVIDENCE_MISSING，另附明細檔）；gate 每次正式實跑於判定前附加一筆斷路器快照（含 passed／denominator／results_sha256／run_id）。
