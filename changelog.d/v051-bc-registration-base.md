---
type: change
scope: contracts
---
- 契約目錄驗證器的 BC 核准來源改為逐項對照：BC-01～BC-05 仍必須是 OD-04，BC-06 起必須是 OD-111（Python 凍結後的 Rust-only 行為差異）；不在對照表內的來源一律拒絕。
- 釘定測試改為 add-only 擴充：五項 BC 的斷言不變，0.5.1 新增的 BC 只能是 `_V051_BC_IDS` 明列者；OD-88 靜態 universe 維持 269，新增 ID 以 `_V051_ADDED_CONTRACT_IDS` 逐項計入。
