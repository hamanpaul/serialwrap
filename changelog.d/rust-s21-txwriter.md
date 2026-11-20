---
type: feat
scope: rust-s21
---
Rust 單一 TX 寫入者：每次 OS 提交都在 fence 鎖內重驗 epoch／凍結／Quiet／Reconfirm／WAL 健康／世代／取消後才交 OS（授權與提交同一臨界區，鎖不跨完成等待），TX WAL 只記 OS 確認的 bytes 且順序等於實際寫入順序；TX 帳本只保留未結算 op，終態 op 經 actor 結算後退休，證據缺口另有上限與合併計數。
