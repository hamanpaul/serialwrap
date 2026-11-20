---
type: feat
scope: rust-p7
---
上游補件（P3 缺口，UP-P7-01）：新增 `sw_proto::pyre::escape`，語意與 CPython 3.12 `re.escape(str)` 逐位元組相同；附 `contracts/golden/pyre_escape.tsv` golden、產生器 `gen_pyre_escape.py` 與新鮮度測試（P7 Task 3 前置）。
