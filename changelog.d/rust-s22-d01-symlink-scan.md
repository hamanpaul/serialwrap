---
type: fix
scope: rust-pal
---
P6 console conformance 的 D-01（console PTY 不建 symlink）斷言改為掃描 PAL 實際可能落 symlink 的目錄（`SERIALWRAP_RUN_DIR`、`XDG_RUNTIME_DIR`、temp、cwd、`/dev`）並比對 `create_pty` 前後快照；舊斷言只掃測試自建的空目錄而恆真。新增 `testutil::{d01_scan_dirs, symlink_snapshot, new_symlinks_to}` 與植入違規的自我檢查案。
