---
type: fix
scope: tests
---
`tests/test_human_agent_coexist.py` 的 FakeTarget 對空行重印 prompt（比照真實 shell 與 `test_multiagent_e2e.py`），消除 prompt probe 依賴開機殘留 prompt、被 `clear_rx_buffer()` 清掉後 20s 內到不了 READY 的競態（Rust 重構 P0 gate）。
