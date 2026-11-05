---
type: fix
scope: pyre
---

不完整的十六進位跳脫（`\x`、`\u`、`\U`）錯誤訊息改為帶上已讀到的跳脫原文與位置，與 CPython 3.12.3 `re.error` 逐字一致（例如 `\x4` 為 `incomplete escape \x4 at position 0`）。
