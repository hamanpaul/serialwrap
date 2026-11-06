---
type: fix
scope: acceptance
---
Slice 2.2 出口判定 `scripts/rust/s22_gate_check.py`：對照表測試的「恰一列 passed」改逐份報告（集合×平台）計算。`s22_gate:console_conformance_pty` 的 selector 會執行整個 `console_conformance` 測試執行檔，其非 cap 測試同時出現在 `s22_gate:cargo_workspace` 報告，先前以平台合計兩列而誤判證據不足；單一報告內重複仍判證據不足，任一列 failed 仍判 FAIL。
