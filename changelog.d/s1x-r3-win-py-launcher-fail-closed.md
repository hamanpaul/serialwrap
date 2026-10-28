---
type: fix
scope: rust-refactor
---

Windows Rust CLI 在 manifest 宣告 `.py` daemon launcher 但缺 manifest runtime 時改為 `DAEMON_BINARY_NOT_FOUND` fail-closed，不再落到 CLI 同層 `serialwrapd.exe` 而繞過版本配對。
