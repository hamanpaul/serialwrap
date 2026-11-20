---
type: fix
scope: contracts
---

- Slice 2.4 覆蓋判定：長跑證據的 pinned bundle、CLI 與 daemon 映像須等於本輪候選 bundle（`slice24_exit.sh --candidate-bundle`／Windows runner `bundle/`，經 `contract_coverage.py --record-candidate-bundle` 記錄），且候選 bundle 的 CLI／daemon 等於同輪實際受測 artifact，舊候選的長跑結果不得替新候選解除 `lr-mixed` 阻擋；長跑 candidate／baseline 的檢查列須恰為正式 P2 gate 推導的完整集合（ID 唯一、group 與 `NOT_APPLICABLE_BASELINE` 適用性一致），彙總以正式規則重算；雙邊參數聯集中兩側皆未通過的節點不再漏判。
