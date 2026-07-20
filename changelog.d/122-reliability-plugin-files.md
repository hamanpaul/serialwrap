---
type: feat
issue: 122
scope: reliability
---
新增 `serialwrap_reliability` 的 testpilot plugin 薄殼與執行契約檔：補上 `plugin.py`、`agent-config.yaml`、`testbed.yaml.example`，並以 `tests/test_reliability_pluginfiles.py` 釘住 entry point、testpilot import 邊界、remediation/retry 鎖死組態，以及 testbed example 與 `realhw/config.json` 的 bench 事實等價性。
