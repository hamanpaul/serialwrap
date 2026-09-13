---
type: change
issue: 171
scope: cli
---
CLI 新增最小控制平面 RPC trace：`-v`/`-vv` 與 `SERIALWRAP_LOG_LEVEL` 可在 stderr 輸出單行白名單 JSON，記錄實際 RPC method、endpoint 來源、遮罩後 endpoint 識別、elapsed、error_code、retry 次數與最後一次主請求 attempt 的 errno。

- 新增專用 `serialwrap.cli_trace` logger，`propagate=False` 且 handler 冪等，不污染 root / `serialwrap` logger；預設 `WARNING`，因此既有 stdout/stderr 契約維持不變。
- `_run_rpc()`、event 路徑、`daemon stop` 與 `daemon start` 的**前置 health probe／就緒等待** CLI RPC 呼叫統一走同一個 trace wrapper；`event add` 會正確記錄真正送出的 `event.rule_set` method。
- `-v` / `--verbose` 為公開 help 旗標，README `serialwrap-help` marker 同步更新，不再以隱藏旗標繞過 R-16。
- trace wrapper 不再因 `trace_sink` 相關 `TypeError` 退回第二次 `rpc_call()`；mutating RPC 維持無額外重送契約。
- Unix socket path 一律 SHA-256；TCP 僅對 loopback literal 保留 `host:port`。trace 不輸出 params、command、secret、raw endpoint path 或完整 response。
- `sw_core.client.rpc_call()` 新增內部 metadata sink：`elapsed_ms` 含既有 retry/backoff/TIMEOUT enrich，`errno` 僅取最後一次**主請求** attempt 的 `OSError.errno`，因此 retry 後 success 與 TIMEOUT enrich 失敗都不會留下錯誤 errno。

**regression-case 評估**：新增 `tests/test_cli_diagnostics.py`，搭配既有 CLI / endpoint / timeout / event / Windows seam 測試即可完整覆蓋；此變更只碰 client/CLI transport seam，不依賴真板、真 daemon 或外部工具搶 tty，故不需新增 `regression/` 真機 case。
