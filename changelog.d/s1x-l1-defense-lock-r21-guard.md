---
type: fix
scope: rust-refactor
---

新增 R-21 整檔豁免防線 `tests/test_policy_r21_allow_guard.py`（逐字複製的雲端審查原文只容許第 3 行一處 structural 命中、不得有 credential／marker）；OD-23／OD-24／能力閘防線與此防線共 12 筆以 add-only 登記進 `s1_pytest_full.linux` 凍結集合。
