---
type: fix
scope: wal
---
Windows WAL 與 mirror 改用含 share-delete 權限的常駐檔案 handle，讓外部工具能在寫入期間改名或刪除檔案；新增 DeletePending、FileId 與 LockFileEx 包裝。
