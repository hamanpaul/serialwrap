---
type: fix
scope: rust-s2
---

補齊 Slice 2.1 parity 回歸節點：Rust 序列埠 backend 環境變數診斷、系統設定檔路徑覆寫、Unknown TX WAL 確認前綴、watcher 未確認 Delta 重送，以及跨 devpts holder 判定；修正相關執行路徑，並以 ServiceRig 驗證 watcher 熱插更新可由 RPC 觀察。
