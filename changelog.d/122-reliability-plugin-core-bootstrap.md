---
type: feat
issue: 122
scope: reliability
---
新增 `serialwrap_reliability.core` 最小 bootstrap：以 `REPO_ROOT` 定位 repo root、冪等注入 repo root 使 `realhw` 可 import，並提供 `load_registry()` 載入 realhw registry；同步加入 `tests/test_reliability_core.py` 驗證 repo-root、bootstrap 冪等、registry 唯一且含 `p0-doctor`，以及 `__init__.py` / `core.py` 不得 import `testpilot`。
