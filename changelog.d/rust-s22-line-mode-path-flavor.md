---
type: fix
scope: rust-console
---
human console line mode 判定（`human_console_mode`）的 basename 改依 PAL 路徑風格（`PalPathFlavor`）取 `posixpath`／`ntpath` 語意，Windows 上 `'C:\bin\vim' file`、`'C:vim' file` 與 Python 一致判為 `interactive`；新增 `contracts/golden/human_console_mode.windows.tsv`（Python oracle 以 `ntpath` 取代 `os.path`，sol 補審 P6 #2）。
