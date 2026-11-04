---
type: fix
scope: sw-cli
---
Windows 黑箱 B 輪改以 `test-fakes` 建置的 `serialwrap.exe` 執行（比照 Linux dev bundle 才讀得到 `SERIALWRAP_TEST_DELEGATE_GROUP`）。標準 release 路徑在其後以不含該 feature 的建置覆寫，且不得含 `SERIALWRAP_TEST_` 或 `SW_TEST_UPDATE_`。Windows `help_root_col80` golden 依 CPython 3.12.3 實測輸出對齊 Python 基線的 `--version` 說明。
