---
type: feat
scope: rust-core
---
MCU pattern registry 承接 Python `mcu_patterns` 語意：hex 解析、`from_dict` 欄位預設與 non_destructive 守衛、同名覆蓋的 `load`，並以 `contracts/golden/mcu_patterns.ndjson` 對照 Python 基線；`mcu.patterns` 回應改由 `patterns_rpc` 產生。
