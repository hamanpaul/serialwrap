---
type: fix
scope: rust-cli
---

修正 Rust CLI `event add --file`：規則 JSON 同時含合法鍵「NUL＋U+FD800」（如 `"\u0000\udbb6\udc00"`）與孤立 surrogate 鍵（如 `"\ud800"`）時，兩者原本被當成同一鍵合併，本應拒送的請求被改寫後送出；現比照 Python `json.load` 保留兩個鍵，於送出前以 `INTERNAL_ERROR` 拒送（S1-f2a23b1-1）。拒送時的錯誤位置也不再把合法的 NUL＋U+FD800 或 `\\u0000` 文字誤認為 surrogate，與 Python 相同。只含合法鍵時照常送出。
