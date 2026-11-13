---
type: change
scope: rust-s2
---
Stage 2 gate 的 pytest selector 改為與 CI 一致，並以裁決 OD-85 登記 Windows TCP console 尚未提供 human lease 的兩個案例差異。補上 P7 登入交錯 WAL 判定、T6 SPEC-GAP 4 報告依據、Windows daemon 測試工具的 `SW_TEST_SERIALWRAPD` 選擇，以及 P8 已知差異紀錄。
