---
type: fix
scope: uart
---

UART bridge 停止改為兩段式 drain：先凍結 TX 與取消等待，確認在途寫入收斂並關閉 handle 後才產生回條。
