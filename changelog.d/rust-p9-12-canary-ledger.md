---
type: feat
scope: stage3
---
新增 Canary policy 凍結、帳本彙總與分平台配額 gate（`tools/stage3/canary_ledger.py`、`tools/stage3/canary_policy.py`）；提供 `freeze_policy` 附加決策紀錄，`tally` 嚴格核實 policy 參考、凍結時序與實際部署內容指紋（R4-F1），`gate` 依活躍日、有效提交、循環配額與事故狀態判定並輸出單調不減的帳本版本（R3-F1）。
