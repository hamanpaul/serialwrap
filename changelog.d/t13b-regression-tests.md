---
type: change
scope: rust-supervision
---
P8 Task 13 第 2 輪先新增正式 `service stop` 隔離 harness 與 Windows staging WAL 證據的回歸測試；以 memory-only 假環境核對正式 P4 停止路徑、測試工作／RPC 埠限縮，以及缺少 WAL 時回報證據不足。
