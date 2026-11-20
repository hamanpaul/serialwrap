---
type: fix
scope: rust-s21
---
Rust POSIX 序列埠對齊 Python：holder 同一 tty 改以 (st_dev, st_ino) 判定（僅非 devpts 字元裝置允許 rdev 別名），目標 tty 無法 stat 時回 holder_unknown；open 只有 ENOENT／EBUSY 分類，EACCES／EPERM／ENODEV／ENXIO 原樣為 OSError；flock EACCES 視為被占用（parity-amendments-2026-10-26 #62／#63／#64）。
