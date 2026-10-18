---
type: fix
scope: wal
---

WAL 與 mirror 輪替及重設前先同步並關閉常駐 handle；歸檔名稱碰撞時加上數字後綴，避免覆蓋既有資料。檔案系統不支援原子 no-replace 改名時，採相容降級方式維持輪替與重設。
