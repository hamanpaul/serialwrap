---
type: fix
scope: rust-s21
---
Slice 2.1 審查修正（P5 雲端雙審 p5-cloud／p6-cloud／p7-cloud 指向 P5 的發現）：holder 測試不再依賴主機 `/dev/pts/9` 且缺 proc 根目錄對齊 Python 回 `permission`；multi_open 的 serialwrapd tty 持有者改以 inode 身分（`same_tty`）判定並移植 `TestDetectMultiOpenNamespace` 3 案；其餘修正見 `docs/rust-refactor/reviews/p5-fix-responses.md`。
