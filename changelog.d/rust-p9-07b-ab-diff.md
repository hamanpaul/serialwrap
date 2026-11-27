---
type: feat
scope: stage3
---
新增 L1.5 A/B 差分工具 `tools/stage3/ab_diff.py`，依報告資料契約讀取 bridge、contract-blackbox、windows-native 與 rust-l1 四種來源；保留原始來源路徑與 SHA，並比較逐案 verdict、CLI rc 與正規化後的 stdout JSON。產品失敗、安全違規與完整性問題採 FAIL 優先，缺案或 runner 未完成時仍保留可解析的失敗來源及完整性診斷。`rust_only`／`absolute_only` 差異照實輸出並標記 `registered_change`，交由 T7 combine 過濾及判定。`contracts/ab_normalization.yaml` 僅追加 G1、RPC-H-13 的 G2 路徑、volatile 欄位與 `<ROOT>` 佔位規則，既有 mappings 不變。B11 `foreign_holders` 自身 PID 快照為候選忽略規則，待擁有者追認。
