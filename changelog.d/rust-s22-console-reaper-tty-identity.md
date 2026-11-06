---
type: fix
scope: rust-pal
---
console reaper 的 `held_slave_paths()` 改以 `(st_dev, st_ino)` 身分判定同一 tty（新增可共用的 `sw_pal::tty_identity::same_tty`，與 Python #R2 `serial_port.same_tty` 同一判準；devpts 不以 rdev 判定、非 devpts 字元裝置允許 rdev 相等別名），他 mount namespace 同名 `/dev/pts/N` 不再被誤計為外部 peer（parity-amendments #40／#64）。
