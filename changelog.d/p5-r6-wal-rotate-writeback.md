---
type: fix
scope: rust-s21
---
WAL rotate 停頓（OD-61 WSL2 實測約 855 ms，Python 基線 132 ms）：根因為 rotate 前 fsync 須寫出整個檔案的髒頁。`sw_pal::wal_fs::posix` 寫入路徑改為每 1 MiB 以 `sync_file_range(SYNC_FILE_RANGE_WRITE)` 啟動非同步回寫（Linux；不等待、不改持久性語意），cloud 同主機 rotate max 由 368～440 ms 降為 65～66 ms（Python 基線 221～360 ms）。
