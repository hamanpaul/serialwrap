---
type: feat
scope: stage3
---
新增 Stage 0～3 出口證據索引與逐平台判定：Stage 3 target 記錄 `bundle_tree_sha256`，entry 保存受測內容指紋並納入有效性判定，同 manifest 換 tree 時拒絕重設 target；canary-report 項目只以帳本版本最新報告判定，新事故撤銷舊 PASS；缺凍結 L1 set 時該項判為 MISSING。
