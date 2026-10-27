### Fixed

- Rust 重構 P7 審查第 5 輪：事件規則 diff 與 `event.tail` 過濾對容器內 NaN 採 Python identity 語意；Windows 上規則檔以文字模式（CRLF）寫出，與 Python 位元組一致；`event.tail` 的 UTF-8 解碼錯誤位置依 8 KiB 分塊計算。
