# #198 diagnostics refine：#171 最小可交付方案

日期：2026-09-12；盤點基準：`cf2aaba9004c18a82a6a5e4e0dcc66ea7211aeb1`。

## 結論先行

#171 的核心缺口仍存在：沒有一致的 runtime logging 設定、CLI verbosity、控制平面
RPC trace 或 daemon log。可是原票部分盤點已過期，不能原樣當作驗收基線：

- 現行 package 已有分散的 `logging` 呼叫（WAL、device watcher、session、UART、event
  matcher），例如 `sw_core/wal.py:5,17-19,96-101`、`sw_core/device_watcher.py:76-78`；
  因此「只有 auth.py import logging」不再成立。這些呼叫沒有共同設定、格式、level
  或 daemon sink，仍不足以提供 #171 要求的控制平面證據。
- #171 的 WAL 靜默回空已由原票 comment 明確拆到 #189；現行 `service.py:979-1011`
  與 `:1013-1035` 已回報 `WAL_MISSING`／`rotated_out`。本方案不重做 WAL。
- #172 已 CLOSED 且在基準內：`cli.py:106-144,734-753` 以 `_format_err_line`
  同時保留 `error_code`、追加 `message`／`hint`；`tests/test_cli_stderr_full_error.py:23-126`
  驗證不存在 socket 的 errno 與 `NEEDS_SUDO`。診斷 trace 不應把 #172 當未修 bug。

因此本文件是可執行的 bounded plan，不是 implemented／tested／deployed 宣稱。

## 現行路徑與證據

1. CLI endpoint 的唯一實際選擇在 `sw_core/cli.py:612-652`：優先
   `--endpoint`、明確 `--socket`、config `socket_path`、平台預設，另有 dangling
   fallback；`_resolve_default_endpoint_with_source()`（`:662-691`）是 doctor 的
   來源文字包裝。#173 已使 daemon 成功 bind 後以 `daemon.py:76-100,185-192` 寫回
   config，並由 doctor `doctor_cmd.py:417-483` 對照 client 與執行中 daemon。
2. 一般 CLI RPC 在 `cli.py:734-753` 呼叫 `_resolve_endpoint()` 一次後送
   `client.rpc_call()`；event 分支在 `cli.py:776-836` 直接各自送實際 method。全域
   目前只有 `--socket/--endpoint/--timeout/--retries`（`:1226-1242`），沒有 `-v`。
3. `client.rpc_call()`（`client.py:107-153`）可能依使用者明確給的 `--retries` 對
   唯讀白名單重送，TIMEOUT 後已有 `health.ping`／`health.status` enrich；單發的
   `client.py:188-237` 在 `OSError` 回 `SOCKET_ERROR` + `str(exc)`，但 errno 目前
   沒有獨立欄位。這是 trace 應從 `OSError.errno` 取值的理由，不應再解析 message 猜。
4. daemon 目前只有 ad-hoc stderr（`daemon.py:76-88,128-170`），RPC 共用
   `rpc_posix.py:11-67`，POSIX AF_UNIX 與 Windows TCP 都走同一個
   `serve_connection`；`daemon.py:20-35` 另列 blocking methods。尚無 inbound/outbound
   method、duration、result error 的統一記錄。
5. RPC 分派是 `service.py:686-740` 起的平面 method map；`session.list` 是現有
   `list_sessions()`（`session_manager.py:969-971`），而不是新增診斷 API。

## Phase 1：CLI 單次操作 trace（本輪唯一首批）

目標是一次既有 CLI 操作留下最小控制平面事件；開關 trace 前後的 RPC 序列相同（既有
TIMEOUT enrich 所需的 `health.ping`／`health.status` 仍照原路徑），不碰 UART、不改
既有 stdout/stderr 機器契約：

```text
rpc_trace { endpoint_transport, endpoint_id, endpoint_source, method, elapsed_ms,
            error_code, errno, errno_name, retry_count, timeout_s }
```

- `endpoint_source` 必須描述**同一次實際決策**：`--endpoint`、`--socket`、
  `config.yaml`、平台預設，或 `config.yaml -> canonical fallback`。把現有解析邏輯
  收斂為一次回傳 resolution metadata；`_run_rpc`、event、daemon start 的既有呼叫
  使用該結果，不可先 probe 一次再重新 resolve，避免 #108 fallback TOCTOU/行為漂移。
- `method` 用真正送出的 RPC method（例如 `event.rule_set`，不可用 `event.add`）；
  `elapsed_ms` 從進入 `rpc_call` 至最終回應計算，包含既有明確 retry/backoff 與
  TIMEOUT enrich。`error_code` 取最終 response；成功為 null。
- `errno`／`errno_name` 只取**最後一次主請求 attempt** 捕捉到的 `OSError`；不能取
  先前 retry 殘留，也不能取 TIMEOUT enrich 的 `health.ping`／`health.status` 探測。
  最終主請求成功時必為 null；RPC 回傳的 domain error 沒有 errno 就是 null。可記錄
  `daemon_reachable`／`daemon_busy` 作為既有 TIMEOUT context，但**TIMEOUT 只表示
  client 停止等待，不表示 daemon 沒有執行或操作未成功**。
- 首批 trace 不自動 retry、不自動 `session.self_test`、不 acquire lease、不 reset/
  recover/UART；只有使用者明確給 `--retries` 時沿現行 `client.py:20-32,141-153`
  行為，trace 記錄實際 retry 次數。

### 輸出、開關與 privacy

- 新 trace 使用專用 `serialwrap.cli_trace` logger，`propagate=False`，handler 建立
  必須冪等；不可配置、改 level 或重複掛 handler 到既有 `serialwrap`／root logger。
  後續 daemon phase 才加檔案 sink。預設 level `WARNING` 且 trace event 用 `INFO`，故
  既有預設 stdout/stderr 位元組不變；測試須連續呼叫 `main()` 驗證不重複輸出。
  verbosity 優先序固定為：顯式 `-v`/`-vv`（分別 INFO/DEBUG）> 有效的
  `SERIALWRAP_LOG_LEVEL` > WARNING；未帶 `-v` 時 env 才生效。既有 #172 error line
  不刪、不改前綴、不重複印。未知 env 值 fail-safe 回 WARNING，不把診斷錯誤變成 CLI 失敗。
- 不記錄 params、command、UART bytes/WAL payload、帳密、env 值、interactive owner
  或完整 response body。Unix endpoint 不輸出 raw path 或 basename，只輸出
  `endpoint_transport=unix` 與固定長度 SHA-256 `endpoint_id`；TCP 只在 host 為
  `127.0.0.1`／`localhost`／`::1` 時輸出 host+port，其他 host 一律同樣雜湊。不得
  以 DEBUG 或環境變數放寬成任意路徑；stderr 的既有 #172 message 維持現狀，scrub
  只作用於新 trace。
- POSIX 需涵蓋 ENOENT／ECONNREFUSED／EACCES 等 AF_UNIX errno；Windows 使用
  `tcp://` loopback 與 Winsock/OSError errno，不假設 `/proc`、systemd 或 AF_UNIX。

### Phase 1 source edit map（實作時）

- `sw_core/cli.py`：新增 verbosity parser、單一 endpoint-resolution metadata、中央
  CLI RPC trace wrapper；event/daemon start 改使用已解析結果，不另加 probe。
- `sw_core/client.py`：保留 `rpc_call()` 回應 dict 形狀；以 optional internal trace
  sink／metadata 傳遞 `OSError.errno`、實際 attempts、elapsed，避免把私有 errno 欄位
  泄漏到 stdout JSON。
- `tests/test_cli_diagnostics.py`（新增）：fake AF_UNIX/TCP server 與 patched OSError
  覆蓋 endpoint source、method、success/failure、errno、timeout、顯式 retries、
  no-param/no-secret；沿用 `tests/test_cli_stderr_full_error.py`，不改其 #172 相容斷言。
- 不在 Phase 1 修改 `daemon.py`、`rpc_posix.py`、`service.py`、`session_manager.py`
  或啟動真 daemon/UART。

## 後續分期（不納入首批完成條件）

Phase 2 才在 daemon 入口集中設定 logger，並在共用 `serve_connection` 記錄 inbound
method、實際 handler duration、error_code、bound endpoint；daemon log 才另設
`<STATE_DIR>/serialwrapd.log` rotation，systemd journal 僅作同一事件的收集出口。
Phase 2 不能與 Phase 1 混稱已完成 #171。

Phase 3 才評估 session state/gate trace（old/new state、reason、gate、單調時間、
session id 的最小化識別），並以現有 `last_state_change_at`／`last_error` 為資料來源；
不另造硬體狀態權威。`session_manager.py:4092-4117` 的 `interactive_status` 會在
讀取時 `lease.touch()`，過期時還會 close lease，**不是純讀診斷來源**；同理
`get_session_state()`（`:3789-3798`）會 `expire_ready_reconfirm()`。Phase 3 不得用
它們做額外 probe，亦不把 `session.self_test`（會做 UART probe）包成觀測命令。

## 可執行驗收情境（Phase 1）

1. 不存在 AF_UNIX socket：`SERIALWRAP_LOG_LEVEL=INFO serialwrap --socket <missing>
   session list`；exit=2，stdout 仍為既有 JSON，stderr 保留
   `failed: SOCKET_ERROR`／`[Errno 2]` 並另有 trace：source=`--socket`、method、
   `error_code=SOCKET_ERROR`、`errno=2`，`endpoint_id` 僅為固定長度雜湊，不含
   command/secret/raw basename。
2. fake server 成功回 `session.list`：`-v` trace 有同一 endpoint、method、有限
   `elapsed_ms`、null error；stdout 做 byte-for-byte baseline 比對。
3. fake server 收下 `session.recover` 後不回應：trace=`TIMEOUT`；測試不得斷言
   daemon 沒執行，且不得看到自動 resend。另測 `serialwrap --retries 2 session list`
   僅在既有唯讀白名單重送，trace attempts/elapsed 如實。
4. patched `OSError(errno.EACCES/ECONNREFUSED)` 與 Windows TCP seam：trace 取 errno
   及 symbolic name；無 `/proc` 或 systemd 仍能完成 CLI trace，不向 UART 寫入。
5. config stale/canonical fallback：assert source 與 endpoint 來自同一次 resolution，
   不因診斷再做第二次 `_endpoint_alive` 而改變選擇；`doctor` 既有 read-only path
   另測，不能把 `interactive_status`／`self_test` 當作 probe。

## Root review / 放行條件

- Root 須逐項核對 source 行號與 live issues：[#171](https://github.com/hamanpaul/serialwrap/issues/171)、[#172](https://github.com/hamanpaul/serialwrap/issues/172)、[#198](https://github.com/hamanpaul/serialwrap/issues/198)；不得以本文件取代實作證據。
- Root 合併後執行 `python3 -m pytest -q tests/` 與 `python3 -m policy_check --repo .`；至少核對上述新增/既有 targeted tests、stdout/stderr 相容、跨平台 seam。pytest 本輪由 root 統一執行。
- 尚未驗證項：daemon log sink/rotation 的成本與 systemd 行為、Windows 實際 errno 文字、
  長時間 logger overhead、真機 state trace。這些是明列 residual/defer，不可在本輪宣稱已修復。
