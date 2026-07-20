---
type: feat
issue: 122
scope: reliability
---
新增 `serialwrap_reliability.testbed_loader`，讓 `testbed.yaml` 與 `realhw/config.json` 都透過 `realhw.load_cfg` 單一正規化路徑產生等價 cfg；同步加入 `tests/test_reliability_testbed.py` 驗證雙來源等價、selector 排序、`longrun.duration` → `duration_s`、`win_serialwrap_exe` 直帶，以及 config.json 會剝除 `_` 開頭註解鍵。
