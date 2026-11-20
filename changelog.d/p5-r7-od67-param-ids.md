---
type: fix
scope: tests
---

依擁有者裁決 OD-67，`test_invalid_regex_rpc_and_arbiter_reject_without_enqueue` 與 `test_classify_pyserial_open_error` 改用明確參數 id（沿用 pytest 9.0.3 的自動 id 形式）：已凍結的 nodeid 全數不變，pytest 9.2 起被改名的過長參數改為兩版本相同的穩定 id，並以 add-only 登記於 Slice 2.1 全套集合。
