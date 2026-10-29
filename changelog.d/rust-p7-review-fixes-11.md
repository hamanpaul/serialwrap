---
type: change
scope: rust-p7
---
Slice 2.3 gate／A/B 報告器依審查收緊（sol 7bb9cfd_cd7acf0）：A/B、先前切片回歸各階段與 `slice_2.3` 涵蓋的必要節點一律取自 L1 鎖預先凍結的 `p7_gate:*` 集合（缺漏、重複、替代節點皆不得 PASS）；`verify` 要求 Rust CLI 與 daemon artifact；repo 外證據路徑保留可定位的完整相對路徑；Windows COM gate 列改用 win-test.sh 可解析的參數；`cap_windows_com_p7` 改逐位元組比對 wire／WAL、quiet 零 TX 視窗自送出 banner 前起算、三案以鎖序列化共用的 COM pair。
