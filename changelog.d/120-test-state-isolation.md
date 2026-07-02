---
type: fix
issue: 120
scope: tests
---
修復測試污染 live state.json 的兩個向量：`SessionManager`/`SerialwrapService` 注入 `state_path`（in-process 測試不再寫 live）；CLI `--socket` 改 None sentinel（有傳即明確，杜絕等值誤判把測試 RPC 路由到 live daemon）。新增 `tests/conftest.py` 三層防線（強制 env 隔離／autouse STATE_PATH patch／live guard gate：state/WAL/config/daemon 四維快照，`SERIALWRAP_LIVE_GATE=warn` 逃生閥）、8 檔 per-file 隔離（unittest runner 防線）、coexist/e2e 隔離 config/WAL/events 維度＋`addCleanup` 根絕 daemon 洩漏。
