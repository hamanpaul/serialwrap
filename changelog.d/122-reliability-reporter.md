---
type: feat
issue: 122
scope: reliability
---
新增 `serialwrap_reliability.reporter`，以不 import `testpilot` 的 duck-typed reporter 重用 `realhw.harness.write_reports()` 產生 `report.md`／`report.json`，將報告複製到 testpilot `artifact_dir`，並在 meta/payload 烙上 deployed 版本、`fw_ver`、`run_id` 與 retry `diagnostic_status` 統計；同步加入 `tests/test_reliability_reporter.py` 覆蓋一般路徑與 `run_meta`／`artifact_dir` fallback。
