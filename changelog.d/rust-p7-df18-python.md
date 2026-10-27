### Fixed

- 事件引擎：Windows 上第二次寫入同一事件規則或 counter 不再失敗（tmp→正式檔改名由 `os.rename` 改為 `os.replace`，DF-18）。
