---
type: feat
scope: rust-cli
---
實作 Python 相容語意層：`int`／`float` 字串解析、`repr(str)`、`str.splitlines`／`strip`、UTF-8 解碼錯誤字串、`posixpath`／`ntpath` 路徑操作與 `OSError` 字串格式，並以 CPython 3.12.3 產生的 golden 驗證。
