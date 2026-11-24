---
type: fix
scope: rust
---
issue 306／307（BC-13）：Rust self-test 在前景操作、有效 queue、login／Recover、agent interactive lease、另一個 probe 或進行中 background capture 時被動回 busy／wait，不送 probe、不 suspend human、不更新 `last_probe_at`、不 confirm ready。閒置 probe 取得排他的前景槽，並以完整 TX、TX 外執行標記、零 RC、標記後 prompt 與有效世代確認 READY；標記行終止後仍保留自訂 prompt regex 所需的 CRLF／LF 行界，略過結尾未越過 marker 的早期命中，並支援同 chunk 與分 chunk 回應。Shutdown、Recover 介入及晚到結果依核准生命週期收尾。Python golden 維持不變；自訂 `ready_probe` 須符合 POSIX shell 框架，不相容時失敗關閉。
