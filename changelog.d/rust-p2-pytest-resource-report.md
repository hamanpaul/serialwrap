---
type: feat
scope: tests
---
新增 pytest 資源報告外掛 `tests/resource_report_plugin.py`（`SW_RESOURCE_REPORT=<path>` 寫出 `l1-results.v1` 報告，含每案 outcome、收集與取消收集清單、`deployed_content` 量測）與純函式 `tests/resource_report.py`；`core_portable` 項目若帶平台條件 SKIP 即 exit 1（以 marker 結構判定，不看 reason 文字）；Windows collection 前排除模組層 `posix_only` 測試檔；新增 L1 必要案例集合鎖 `l1_required.lock.json`（`regen_lock.py --l1-add-only` 只增、`--check --base` 禁止基準 nodeid 消失）與唯一判定實作 `tests/acceptance/l1_judge.py`（單一 pass、required skip、重複、deselected、未執行、exit 非零、未完成、junit 不符皆不得 PASS）；`check_evidence_index.py` 以 `l1_set` 判定 `S0-L1-BC`、`S0-L1-WIN-PYTEST`；`.github/workflows/tests.yml` 新增 hosted `pytest-windows`（免驅動 Windows L1）與自架 `windows-com-selfhosted`（label `run-windows-com` 或 workflow_dispatch，runner 未註冊時不視為通過）。
