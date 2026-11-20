---
type: fix
scope: acceptance
---
P8（Slice 2.4）Task 1 第 2 輪：`tests/test_slice24_cases.py` 新增 Slice 2.4 凍結後逐案指紋漂移檢查（審查 P8X-0aa7457-1）。`cases.lock.json` 的 `case_attrs` 只含分母屬性，2.4 必要案例的 `evidence`／`contract`／`source_ref` 在凍結後被改動且 revision 依 OD-76 前進時，原有守門全數維持綠燈；現以有效凍結指紋（套用修訂鏈後）逐案重算比對，凍結後要改必要案例須先取得擁有者逐名 OD，再以 `freeze_slice_cases.py --amend --fingerprint-update` 追加修訂。
