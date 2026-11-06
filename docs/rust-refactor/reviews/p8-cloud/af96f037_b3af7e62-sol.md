> 審查者：pi＋gemini-4-argon（11 月 argon 已花 $68.09）（代理 sol 單審位，擁有者 2026-11-02 指定）

## A 前輪 finding 核對

無

## B 新發現

- P8-b3af7e6-1 [High] `tests/test_mcu_golden_fresh.py:13` — 問題：commit `74d25537` 新增的 `tests/test_mcu_golden_fresh.py` 未標註平台與資源 marker，且在 `test_mcu_golden_is_fresh` 內呼叫 `subprocess.run(...)` 觸發 `process_rpc` 資源偵測，違反 `CLAUDE.md`「pytest 平台／資源 marker（Rust 重構 P0 T14）」與「不得引入新的測試失敗」規定。具體失敗情境：執行 `python3 -m pytest -q tests/test_resource_markers.py` 時，`test_every_item_has_exactly_one_platform_marker`、`test_every_item_has_resource_marker_and_memory_only_is_exclusive`、`test_detected_resources_are_marked` 三項測試皆因 `('test_mcu_golden_fresh.py', 'test_mcu_golden_is_fresh', ...)` 斷言失敗（exit code 1） — 確切修法：對齊 `tests/test_rust_goldens_fresh.py` 慣例，於 `tests/test_mcu_golden_fresh.py` 匯入 `import pytest` 並在模組層宣告 `pytestmark = [pytest.mark.core_portable, pytest.mark.process_rpc]`，再執行 `python3 -m pytest -q tests/test_resource_markers.py tests/test_mcu_golden_fresh.py` 確認全綠。

## 既有碼待辦（不計入結論）

無

## 對裁決的異議（不計入結論）

無

結論：需修正