---
type: docs
scope: rust-s2-gfx-d1
---
逐名稽核 OD-98 的 Linux 全套 pytest 必要集合：僅將 46 個在替代必要集合實際 passed 的 skipped nodeid 從兩個集合移出，其餘 139 個 skipped 與 1 個 xfailed 保留；另只從 P7 A 輪移除 SC-30，P7 B 輪不變。以目前 HEAD 更新 Windows Cargo add-only 集合並登記 F-S19 的 Windows parity selector；只放行其精確的凍結 parity node/selector 組合，該列僅有 collect-only 證據、狀態仍為 INCOMPLETE。完整逐名證據見 `docs/rust-refactor/reviews/od98-linux-pytest-skipped-audit.md`。
