### Fixed

- Rust 重構 P7 審查第 8 輪：同步 P5 審查修正後，登入 TX 例外以 Python 類別名與 `str(exc)` 回報（結果不明為 `TxUnknownError`）；停機「drain 前關閉事件受理」與「handler 逾時 WARNING」補行為回歸。
