---
type: feat
scope: rust-cli
---
實作 Rust CLI 資料面 RPC 子命令（device、session、alias、cmd、stream、log、file、wal、mcu）的參數組包、wire 格式與有效 timeout，並接上 `main` 分派；RPC 失敗、endpoint 解析失敗與 stdout 寫入失敗的 stdout／stderr 與 exit code 對齊 Python 基線（exit 0／1／2／120）。非 UTF-8 argv 依 Q-19 處理：路徑型值（`--socket`、`--endpoint`）以原始位元組連線，進入 RPC 請求者於送出前失敗（daemon 收 0 筆），`--bench` 回顯者 strict 失敗（Q-19(c)，登記為與 Python 的非契約差異）。
