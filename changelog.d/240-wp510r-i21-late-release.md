---
type: fix
scope: rust-core
---

保留 Bridged 改綁與 release 交錯時的在途停止語意；即使兩個請求都已逾時，配對退休排空回條仍會恢復 RELEASED、清除 drain 欄位與退休等待鏡像，並完成落盤。
