---
type: fix
scope: rust-cli
---
負路徑測試的 passwd 家目錄改放測試暫存目錄，並拒絕指向行程真實家目錄，避免回歸時寫入真實 systemd user unit。
