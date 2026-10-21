---
type: feat
scope: rust-cli
---
新增 Rust CLI 的 stdout JSON（sort_keys、緊湊分隔符）、stderr 錯誤行與版本不一致警告的輸出函式，以及 `-v`／`SERIALWRAP_LOG_LEVEL` 的 RPC trace 層級解析、endpoint 摘要與 trace 行序列化，行為與 Python 基線位元組一致；並新增 `scripts/rust/goldens/gen_cli_trace.py` 與 `contracts/golden/cli_trace.tsv` 作為 trace 解析的 golden 基準。
