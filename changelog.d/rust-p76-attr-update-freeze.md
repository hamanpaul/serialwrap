---
type: fix
scope: acceptance
---
依 SG-P76-S23-REVISION 裁決，凍結工具新增 `--amend --attr-update CASE:FIELD:OLD:NEW:OD-NN`。
修訂鏈記錄分母屬性快照，寫入與讀取時皆將具名欄位還原成舊值，驗證其指紋等於前一版；
未列出的欄位變動、案例增刪或順序變動皆拒絕。Slice 2.3 以 OD-34 補記兩筆 `daemon_slice` 延後，
維持原凍結本體、必要案例及觀測窗口。
