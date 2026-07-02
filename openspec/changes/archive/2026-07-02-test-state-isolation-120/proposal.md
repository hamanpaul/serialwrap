# Proposal: test-state-isolation-120

## Why

在有 production daemon 的機器上跑 `python3 -m pytest -q tests/` 會污染 live `~/.local/state/serialwrap/state.json`（fake binding 持久化 reserve COM0、測試 alias 寫入、`released` 整段清空），daemon restart 後真板無法接上，已三度復發並實際阻斷開發測試（#120）。探索確認污染有兩個獨立向量：(1) in-process 測試未隔離 import-time 凍結的 `STATE_PATH`（8 檔 33 個建構點）；(2) CLI `--socket` 等值誤判使 coexist／e2e 測試的 RPC 誤路由到 live daemon（fake binding 真兇，並附帶 live WAL reset、真板 TX、洩漏 daemon）。

## What Changes

- `SessionManager` 新增 `state_path` 建構注入（`SerialwrapService` 透傳）；`WalWriter` default 改 None-sentinel 於建構時解析——消除「光是建構就寫 live state」的類別耦合。
- `serialwrap` CLI `--socket` 改 `default=None` sentinel：「明確指定」的判準從「值 ≠ import-time 預設」改為「有無傳入」，杜絕等值誤判 fallback 到 live config。**語意變更**：明確傳入恰等於預設值的 `--socket` 現在被尊重、不再讀 config。
- 新增 `tests/conftest.py` 三層防線：top-level 強制 env 隔離（per-run tmp）、function-scoped autouse `STATE_PATH` patch、session-finish live guard（state／WAL／config／daemon 四維快照比對，strict FAIL 預設＋`SERIALWRAP_LIVE_GATE=warn` 逃生閥）；判定邏輯抽 `tests/liveguard.py` 純函式。
- 8 檔未隔離測試補 per-file 隔離（unittest runner 防線）；coexist／e2e subprocess env 補 `SERIALWRAP_CONFIG_DIR`（e2e 另覆寫 `SERIALWRAP_WAL_DIR`）；coexist setUp 改 `addCleanup` 根絕 daemon 洩漏；`test_setup_materialize.py` 2 測試補 `delenv`。
- CLAUDE.md 測試政策註記：pytest 為唯一具完整隔離防線的跑法。

## Capabilities

### New Capabilities
- `test-live-isolation`: 測試套件對 live 資源（state.json／WAL／config.yaml／production daemon）的零接觸保證與其可執行防線（env 隔離、state_path 注入點、live guard gate、雙 runner 的 state 維度安全）。

### Modified Capabilities
- `daemon-supervision`: 「CLI endpoint 解析」requirement 中「明確指定的 `--endpoint` 或**非預設** `--socket` 維持最高優先序」的判準變更——`--socket` 以「有無傳入」判定明確性（sentinel），不再與 import-time 預設值比對；傳入值恰等於預設值時同樣尊重、不 fallback config。

## Impact

- 受影響程式碼：`sw_core/session_manager.py`（注入）、`sw_core/service.py`（透傳）、`sw_core/wal.py`（None-sentinel）、`sw_core/cli.py`（`--socket` sentinel＋`_resolve_endpoint`＋`_run_daemon_start`）。
- 測試面：新增 `tests/conftest.py`、`tests/liveguard.py`＋其 unit test；8 檔 per-file 隔離；coexist／e2e／setup_materialize 併修。
- 文件：CLAUDE.md 測試政策、README（若提及測試跑法）。
- 相容性：production daemon 行為不變（注入 default fallback 至 constants）；既有 19 檔 setattr／5 檔 reload／2 檔 subprocess 隔離測試不需改。gate 與隔離必須同一 PR（CI 上 gate 單獨先行會真陽性紅）。
- 風險：live guard strict 模式在「測試期間對真板操作」時誤報（刻意取捨，逃生閥載明）。
