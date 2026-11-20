---
type: fix
scope: rust-s2-gfx
---

Slice 2.4 覆蓋 plugin 將 pytest `unittest.subTest` 對同一 nodeid／phase 發出的多份報告彙整為單列，並保留其中最差結果；判定端對結果檔真實重複列的 R5-F3 不完整規則不變。
