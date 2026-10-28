---
type: fix
scope: rust-refactor
---

systemd hold release 移除 drop-in 後，若 `<unit>.service.d` 已空則 best-effort 一併移除（systemd-user 經 `FsOps::remove_dir`；systemd-system 以 `sudo rmdir --ignore-fail-on-non-empty`），目錄內另有他人 drop-in 時保留；`FsOps` 新增 `remove_dir`（空目錄 `rmdir` 語意，預設回 `Unsupported`）。
