---
type: fix
scope: rust-p7
---
`sw_proto::pyre::redos`：歧義分數改在 `sre_parse` 等價的 SubPattern 項目序列上計算——無 flags 的非捕獲群組先攤平進外層序列，alternation 在攤平後的分支上做共同前綴抽出與 IN 化（字元類別依 `_uniq` 去重）；修正 `(?:(?:a)|b)+`、`(?:(?:ab)|(?:ac))+` 等 Python 接受（score 1）的規則被 Rust 誤拒（sol 補審 #1）。
