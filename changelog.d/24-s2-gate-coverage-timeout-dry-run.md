---
type: fix
scope: rust-refactor
---

- 修正共用 L1 證據遭拒後仍回退採用舊報告、Slice 2.4 coverage 封裝層級與 Cargo／pytest 同標籤合併問題（重複封裝的 Python 結果不再重複計入）、Linux 檔案 I/O timeout 遺失 errno，以及 Windows 測試 dry-run 提前建置與 staging 的問題。
