---
type: fix
scope: rust-refactor
issue: 241
---

升級驗證改依 Python 基線指認 `--version`：純版本或附 12 位程式碼指紋都算該版本，版本號仍須整段相等，入口切換與 pipx 備份條件不變。
