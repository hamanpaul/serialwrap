---
type: fix
scope: acceptance
---
凍結修訂鏈新增「指紋更新」修訂（OD-52）：`scripts/rust/freeze_slice_cases.py --amend --fingerprint-update <case>:<OD-NN> --source-commit <sha> --field <欄位>` 只追加修訂紀錄、不改凍結本體與分母，逐案記錄舊／新指紋、欄位、來源 commit 與 OD，舊指紋須等於前一版有效指紋；`load_effective_freeze()` 回傳套用後的 `case_fingerprints`。`slice_2.1`、`slice_2.2` 修訂鏈各追加一筆，涵蓋主線 `dbbc1f7` 對 `l15-release-inflight.windows_gap_reason` 的變更。
