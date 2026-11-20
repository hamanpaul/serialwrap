---
type: fix
scope: rust-s2-rh-k6
---
PR #278 第六輪 hosted CI：systemd 黑箱等待 READY 的觀察上限 20 s → 60 s；bridge writer 停滯測試依比例放大時間（50 ms／500 ms）避開 Windows 計時粒度；p7 golden 新鮮度測試在安裝 google-re2 的測試輪略過（擷取以無 re2 為前置，由無 re2 輪覆蓋）。
