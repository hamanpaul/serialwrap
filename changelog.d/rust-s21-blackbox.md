---
type: feat
scope: rust-s21
---
新增 Slice 2.1 黑箱契約案例（F1／F2／F3／F4／F12 L1 承接、零 TX、獨占、release／attach、帳密未解析、WAL 自癒），同一批案例可指向 Python 或 Rust daemon；修正 Rust daemon 與基線不一致處：attach probe 進行中收到開機 banner（boot quiet arm）時，attach 結果不再因登入世代遞增而被丟棄、永久停在 ATTACHING，改與 Python 一致落定 ATTACHED＋`PROMPT_UNAVAILABLE`；`session clear`（含 recover 救援失敗後的 clear）drain 收尾重新開埠進 ATTACHING 時清除 `last_error`，不再帶著 `CLEARED`，與 Python `_attach_by_id` 一致。
