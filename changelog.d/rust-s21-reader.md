---
type: feat
scope: rust-s21
---
Rust UART reader：RX 先落 WAL（精確 seq）再更新絕對偏移 RX 視窗、哨兵 console 與 actor；等待錯誤以 SELECT:<errno>、讀取錯誤以 SERIAL_READ:<errno> 回報，EOF 不視為斷線。regex 在 RX 鎖外搜尋；RX handler panic 會告警並續行，有界 data lane 滿載時 stop 仍可中斷等待。
