---
type: feat
scope: rust-s21
---
Rust WalWriter：以 Python 相容位元組格式寫入 WAL 與 mirror，使用常駐 handle，偵測並自癒外部刪檔或替換並寫入 `WAL_RECREATED_AFTER_EXTERNAL_UNLINK`；歸檔遇同名時加上數字後綴，mirror 寫入失敗不會重複 WAL 行，重建失敗時停止自動 TX。
