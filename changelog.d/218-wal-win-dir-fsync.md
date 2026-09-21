---
type: fix
issue: 218
scope: wal
---
Windows（nt）上 `wal reset` 與 WAL 大小輪替不再對目錄做 POSIX fsync：`os.open(<dir>)` 在 Windows
會拋 Permission denied，導致 `wal.reset` RPC 回 EXCEPTION（檔案已輪替但 seq 未歸零）、
每次輪替誤記「WAL 輪替失敗」警告。兩處抽成 `_fsync_dir()`，nt 跳過（比照 session_manager
#84 PORT-4 守衛），POSIX 行為不變。
