---
type: fix
scope: contracts
---
`python3 -m tools.contracts` 入口固定 stdout／stderr 為 UTF-8＋LF，原生 Windows（locale 編碼如 cp950、CRLF 換行）下讀取端不再解碼失敗；C13／C14 子行程的 `PYTHONPATH` 改以 `os.pathsep` 串接（Rust 重構 P0 gate）。
