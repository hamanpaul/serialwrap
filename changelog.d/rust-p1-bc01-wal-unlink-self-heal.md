---
type: feat
scope: wal
---

偵測 WAL 被外部刪除或替換，以有界重試重建並先寫入 `WAL_RECREATED_AFTER_EXTERNAL_UNLINK` 缺口 META。重建失敗時標示 WAL 不健康並停止新增的系統自動 TX。
