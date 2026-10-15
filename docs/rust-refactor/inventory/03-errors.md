# 03 — 錯誤碼（error_code）與 Session `last_error` 盤點

- 盤點對象：`~/prj_pri/serialwrap-worktrees/rust-refactor`（HEAD `93553e6`），唯讀。
- 範圍：`sw_core/**/*.py`（repo 無 `sw_mcp/`）、`serialwrapd.py`／`serialwrap.py`（僅 shim，無錯誤碼）。
- 方法：
  1. AST 掃描 `sw_core` 全部大寫字串常值（`[A-Z][A-Z0-9_]+`），逐一判定是否進入回應的 `error_code`。
  2. `grep error_code`（252 處）＋追蹤所有**間接傳遞路徑**：`ERROR_*` 常數、`TunnelError(code)`／`_EndpointResolutionError(code)` 例外、`probe_error` 回傳值、arbiter 把 `result["error_code"]` 寫入命令紀錄、`last_error` 被提升為 `error_code`（`session.attach`／`session.recover`）。
  3. 對照 spec §9.2（71 字面碼＋6 常數碼＋BC-03）與 §9.3／§9.4／§5.3。
- 行號皆為該 HEAD 的實際行號（`file:line`），函式名為最內層 enclosing function。

---

## 1. 共通回應契約（Rust 必須逐位元對齊的行為）

### 1.1 Daemon RPC 回應形狀
- 每個回應為 JSON object，必有 `ok: bool`；失敗附 `error_code: str`，另常附 `selector` / `session_id` / `cmd_id` / `interactive_id` / `session`（`SessionRuntime.to_public_dict()`）/ `hint` / `message` 等**依 code 而異**的欄位（見 §3 表格「附帶欄位」）。
- `SerialwrapService.rpc()`（`sw_core/service.py:688-704`）對 `_dispatch()` 回傳的 **dict** 一律 `setdefault("version", self._version)`——成功、失敗回應都有 `version`（`health.status` 本身已帶）。
- RPC framing（`sw_core/rpc_posix.py:11-70` `serve_connection`，POSIX 與 Windows `rpc_win.TcpRpcServer` 共用，`rpc_win.py:9,35`）：
  - 每行一個 request；回應以 `json.dumps(resp, ensure_ascii=False, separators=(",", ":")) + "\n"`。
  - **JSON decode 失敗** → `{"ok": false, "error_code": "INVALID_JSON"}`（`rpc_posix.py:31`）：**無 `id`、無 `version`**，且以 `json.dumps(..., ensure_ascii=False)` 預設分隔符（`", "`／`": "`，非緊湊）輸出。
  - **非 object** → `{"ok": false, "error_code": "INVALID_REQUEST"}`（`rpc_posix.py:38`）：同上，無 `id`、無 `version`、非緊湊。
  - **`method` 非 str** → `{"id": <req_id>, "ok": false, "error_code": "INVALID_METHOD"}`（`rpc_posix.py:47`），無 `version`。
  - `params` 非 dict → 靜默改為 `{}`。
  - handler 拋任何 `Exception` → `{"id":…, "ok": false, "error_code": "EXCEPTION", "message": str(exc)}`（`rpc_posix.py:58-59`），**無 `version`**（例外發生在 `service.rpc` 之前就中斷）。
  - handler 回非 dict → `{"id":…, "ok": true, "data": result}`。
  - 回應都先放 `id` 再 `update(result)`（`rpc_posix.py:60-64`），所以 result 若含 `id` 會覆寫。
- `daemon.stop` 由 `sw_core/daemon.py:165-169` 的 `_handle` 攔截（回 `{"ok": true, "stopping": true}`，不經 `service.rpc`，無 `version`）。
- 未知 method → `METHOD_NOT_FOUND`（`service.py:1174`，附 `method`）。

### 1.2 CLI 輸出與 exit code
- `_print()`（`sw_core/cli.py:124-126`）：`json.dumps(obj, ensure_ascii=False, sort_keys=True, separators=(",", ":"))` + `\n` 到 stdout（**sort_keys=True**）。
- stderr 鏡射 `_format_err_line()`（`cli.py:144-168`）：`serialwrap: {context} failed: {code_str}[: {message}][（hint: {hint}）]`；`code_str` = `error_code` → 否則 `message`（且不再重複 message）→ 否則字面 `UNKNOWN_ERROR`（`cli.py:161`，**只出現在 stderr，從不出現在 JSON**）。下游以 substring `failed: <CODE>` 比對，形狀需凍結。
- `_run_rpc`（`cli.py:932-956`）：RPC 失敗 exit **2**；成功 0。endpoint 解析失敗走 `_print_endpoint_resolution_error`（`cli.py:130-134`）exit **1**。
- 其他 exit code（逐一見 §3）：`daemon start` 失敗 2、`connect`/`benches`/`remote` 失敗 1、argparse 失敗於 `connect`/`benches` 邊界 1、`main` fallthrough `INVALID_ARGS` 2、`cmd submit` 本地 `INVALID_COMPLETION_REGEX` 1、`profile test` 錯誤 0、`setup` FLASHING_BUSY 2、`service`/`daemon stop` 路由 0/2、`_print_internal_error` 1。
- `_run_rpc` 不捕捉 client 端例外：`client._rpc_call_once` 若收到非 JSON 回應，`json.loads` 拋 `json.JSONDecodeError`（`ValueError`，**不是** `OSError`，`client.py:268` 未被 `except OSError` 捕捉）→ 例外穿越 CLI（Python traceback）。Rust 若改成回碼屬行為變更，需登記。

### 1.3 Client 端重試（`sw_core/client.py`）
- `_RETRYABLE_TRANSIENT_ERROR_CODES = frozenset({"TIMEOUT", "SOCKET_ERROR", "EMPTY_RESPONSE"})`（`client.py:48`）。
- 只對 `RETRYABLE_READONLY_METHODS`（`client.py:22-34`，11 個：`health.ping`、`health.status`、`session.list`、`session.get_state`、`session.activity`、`session.console_list`、`session.log_status`、`device.list`、`alias.list`、`mcu.patterns`、`mcu.status`）在 `retries>0` 時重試；指數退避 `_RETRY_BACKOFF_BASE_S=0.5`、`×2`、單次上限 `_RETRY_BACKOFF_MAX_S=5.0`。
- 最終仍 `TIMEOUT` 且 method ∉ `_PROBE_METHODS=("health.ping","health.status")` → 追加 `daemon_reachable: bool`，可達時再追加 `daemon_busy: {"commands": int, "sessions": int}`（任一缺則省略該鍵）；探測單次 `_PROBE_TIMEOUT_S=1.0`、整段 `_PROBE_DEADLINE_S=2.5`（`client.py:173-174, 190-221`）。
- `rpc_call` 初始 `resp = {"ok": False, "error_code": "TIMEOUT"}`（`client.py:160`，若 attempts=0 不會發生，實際總會被覆寫）。

---

## 2. 錯誤碼常數定義（defining constant）

| 常數 | 值 | 定義位置 | 用途 |
|---|---|---|---|
| `ERROR_CMD_TOO_LONG` | `"CMD_TOO_LONG"` | `sw_core/arbiter.py:20` | submit 拒絕；也出現在 `health.status.limits.reject_error_code`（`service.py:596`） |
| `ERROR_CMD_CONTAINS_NEWLINE` | `"CMD_CONTAINS_NEWLINE"` | `sw_core/arbiter.py:21` | submit 拒絕；`limits.newline_error_code`（`service.py:597`） |
| `WARNING_CMD_LENGTH` | `"CMD_LENGTH_WARNING"` | `sw_core/arbiter.py:22` | **警告碼**（`warning.code`，非 error_code）；`limits.warning_code`（`service.py:598`） |
| `CMD_WARN_BYTES` | `4096` | `arbiter.py:14` | soft limit（`limits.warn_submit_cmd_bytes`） |
| `CMD_REJECT_BYTES` | `16384` | `arbiter.py:15` | hard limit（`limits.max_submit_cmd_bytes`） |
| `ERROR_INVALID_COMPLETION_REGEX` | `"INVALID_COMPLETION_REGEX"` | `sw_core/command_completion.py:8` | completion_regex 驗證失敗（**spec §9.2 漏列**） |
| `COMPLETION_REGEX_MAX_BYTES` | `1024` | `command_completion.py:7` | regex UTF-8 長度上限；另要求非空、可 compile、`search("") is None`（`:11-23`） |
| `ERROR_AUTOBOOT_QUIET` | `"AUTOBOOT_QUIET"` | `sw_core/constants.py:142` | 可重試 gate 拒絕；亦為 self-test `classification` |
| `ERROR_READY_UNCONFIRMED` | `"READY_UNCONFIRMED"` | `constants.py:169` | 不可重試 gate 拒絕 |
| `ERROR_CREDENTIALS_UNRESOLVED` | `"CREDENTIALS_UNRESOLVED"` | `constants.py:85` | `last_error`（亦被提升為 error_code，見 §5） |
| `ERROR_RX_FLOOD` | `"RX_FLOOD"` | `constants.py:90` | `last_error`／classification |
| `ERROR_TRANSPORT_STALL` | `"TRANSPORT_STALL"` | `constants.py:101` | `last_error` |
| `ERROR_BOOTLOADER_STUCK` | `"BOOTLOADER_STUCK"` | `constants.py:176` | `last_error` |
| `_RETRYABLE_TRANSIENT_ERROR_CODES` | `{"TIMEOUT","SOCKET_ERROR","EMPTY_RESPONSE"}` | `client.py:48` | client retry 判定 |
| `LOGIN_FSM_DETAIL_ERRORS` | 12 碼 frozenset（§5.2） | `sw_core/login_fsm.py:31-44` | 決定 `last_error_detail` 是否帶 rx tail |
| `_FLOOD_MASKABLE_ERRORS` | `{"PROMPT_UNAVAILABLE","READY_NONCE_TIMEOUT","READY_PROMPT_TIMEOUT","POST_LOGIN_CMD_TIMEOUT","LOGIN_PROMPT_TIMEOUT"}` | `login_fsm.py:19-25` | 可被反分類為 `RX_FLOOD`（另含所有 `*_PROMPT_TIMEOUT`） |

相關門檻常數（影響錯誤分類）：`RX_FLOOD_BYTES_PER_10S=20_000`、`RX_RATE_WINDOW_S=10.0`、`TRANSPORT_STALL_MIN_RX_AGE_S=30.0`、`READY_RECONFIRM_RETRY_S=5.0`、`READY_RECONFIRM_MAX_S=BOOT_QUIET_WINDOW_S+120.0=300.0`、`READY_RECONFIRM_MAX_ATTEMPTS=5`、`BOOT_QUIET_WINDOW_S=180.0`、`CMD_PENDING_MAX=256`、`CMD_HISTORY_MAX=512`、`BG_CAPTURE_MAX_COUNT=64`、`MAX_RECOVERY_LEASE_S=120.0`、`REPROBE_MAX_ATTEMPTS=10`（`sw_core/constants.py`）。

注意：spec 所稱「常數碼」中 `DAEMON_ALREADY_RUNNING`、`FLUSHED_BY_RECOVERY` **在程式碼中不是具名常數**：前者是 `RuntimeError("DAEMON_ALREADY_RUNNING")` 字面（`lock_posix.py:22,40`、`lock_win.py:37,50`），後者是函式預設參數字面（`arbiter.py:60,86`）與 `session_manager.py:3896` 字面。

---

## 3. 主表：每個 error_code

介面標記：
- **RPC** = daemon `service.rpc` 分派產生（top-level `error_code`）。
- **RPC-frame** = `serve_connection` framing 層。
- **CLI-client** = `sw_core/client.py`（CLI 本地，連線層，daemon 未參與）。
- **CLI-local** = `sw_core/cli.py`／`service_ctl.py`／`remote_tunnel.py`（CLI 本地邏輯）。
- **REC** = 存在 `command.get` 的 `command.error_code`（arbiter 命令紀錄）或 `command.result_tail`／`result.tail` 的 `error_code`（BackgroundCapture 或 arbiter fallback）。
- **PROC** = daemon 行程 stderr＋exit code（非 JSON）。

可重試欄：`是`＝程式碼/註解明示可重試或語意即「未送出」；`否`＝明示不可；`條件`＝視情況；`—`＝參數/設定錯誤（修正後再送）。

### 3.1 Daemon RPC 產生的錯誤碼

| code | 常數 | 發出位置（file:line, function） | 介面 | 意義 | 可重試 | 副作用／附帶欄位 |
|---|---|---|---|---|---|---|
| `ALIAS_NOT_FOUND` | — | `session_manager.py:1735` `unassign_alias` | RPC `alias.unassign` | alias 不存在 | — | **失敗前仍執行 `_save_state()`**（`:1733`）；附 `alias` |
| `AUTOBOOT_QUIET` | `ERROR_AUTOBOOT_QUIET` | `service.py:671`（quiet 進行中，`retry_after_s=boot_quiet_remaining_s`）、`service.py:683`（pending-only，`retry_after_s=READY_RECONFIRM_RETRY_S`=5.0）`_resolve_session_id`；`session_manager.py:1155` `_agent_gate_reject_locked`（無 retry_after_s，附 `selector`） | RPC `command.submit`/`file.push`/`file.pull`（source 非 `human:`）；REC（execute 時 gate） | session 名義 READY 但 boot quiet armed 或 READY 再確認 pending | **是**（等 READY 再送） | 零 UART 副作用；service 版附 `retry_after_s`、`hint`、`session`；manager 版只附 `selector`（形狀不同） |
| `BASE64_DECODE_FAILED` | — | `file_transfer.py:324` `pull_file` | RPC `file.pull` | 擷取的 base64 無法解碼 | 條件 | 不寫本地檔 |
| `CHECKSUM_MISMATCH` | — | `file_transfer.py:237` `push_file`；`:334` `pull_file` | RPC `file.push`/`file.pull` | md5 不符 | 條件 | push：先 `_cleanup_remote` 刪 tmp；附 `expected`/`actual`（push: expected=本地 md5；pull: expected=remote md5, actual=local）；pull 不寫本地檔 |
| `CHECKSUM_VERIFY_FAILED` | — | `file_transfer.py:232` push；`:330` pull | RPC | target `md5sum` 無回應／擷取不到 | 條件 | push 先 cleanup tmp |
| `CMD_CONTAINS_NEWLINE` | `ERROR_CMD_CONTAINS_NEWLINE` | `arbiter.py:159-165` `submit` | RPC `command.submit`（亦 human console line） | 命令含 `\n` | — | 附 `hint`；不入列 |
| `CMD_NOT_CANCELABLE` | — | `arbiter.py:260` `cancel` | RPC `command.cancel` | 命令 status 已是 `done`/`error` | 否 | 附 `cmd_id`；注意 `canceled`/`interactive`/`running`/`accepted` 都可再 cancel（`running` 被標 canceled 但 worker 仍在跑；`canceled` 可重複 cancel） |
| `CMD_NOT_FOUND` | — | `arbiter.py:251` `get`；`arbiter.py:258` `cancel`；`session_manager.py:4721` `get_background_result`；`service.py:335,338,342,345` `_bg_fallback_from_arbiter` | RPC `command.get`/`command.cancel`/`command.result_tail`/`result.tail` | cmd_id 不存在（或已被 evict；或 result_tail fallback：arbiter 不認得、非 background、`done` 卻無 capture、`canceled` 且已 started） | 否 | 附 `cmd_id`；`command.get` 空 cmd_id 也回此碼 |
| `CMD_TOO_LONG` | `ERROR_CMD_TOO_LONG` | `arbiter.py:143-151` `submit` | RPC `command.submit` | UTF-8（errors=replace）長度 > 16384 | — | 附 `cmd_length`、`limit`、`hint`；檢查順序：completion_regex → 長度 → 換行 |
| `CONSOLE_LINE_LIMIT_TOO_SMALL` | — | `file_transfer.py:376-381` `_line_limit_error`（呼叫點 push `:120,124,141,143`… pull 多處） | RPC `file.push`/`file.pull` | profile `max_console_line_chars` 放不下必要命令 | 否 | 第一個 TX 前拒絕；附 `max_console_line_chars`；chunk 迴圈內的 fail-closed 版本會先 `_cleanup_remote` |
| `CONSOLE_NOT_FOUND` | — | `session_manager.py:3996` `detach_console` | RPC `session.console_detach` | client_id 不存在 | 否 | 仍會關閉對應 human lease（若 owner 相符）並更新 vtty；附 `client_id` |
| `DEVICE_ALREADY_BOUND` | — | `session_manager.py:2049-2054` `bind_session` | RPC `session.bind` | 該 by_id 已綁其他 session | — | 附 `device_by_id`、`session_id`（他方） |
| `DEVICE_NOT_BOUND` | — | `session_manager.py:2161` `attach_session` | RPC `session.attach` | session 無 device_by_id | — | 附 `session` |
| `DEVICE_NOT_FOUND` | — | `session_manager.py:2116` `pin_session`、`:2127` `unpin_session`（selector 解不出 device key）；`:2190` `attach_session`（by_id 不在線） | RPC `session.pin`/`session.unpin`/`session.attach` | 裝置不存在 | 條件 | attach 版：**設 state=DETACHED、last_error=DEVICE_NOT_FOUND、detached_at**，附 `session`；pin/unpin 版無附帶欄位 |
| `DEVICE_NOT_PRESENT` | — | `session_manager.py:2015` `attach_device` | RPC `device.attach` | by_id 不在 `_devices` | 條件 | 附 `selector`、`device_by_id` |
| `DEVICE_STILL_HELD` | — | `session_manager.py:2020` `attach_device` | RPC `device.attach`（非 force） | `/proc` 掃到外部 holder | 條件（等外部釋放或 `force`） | 附 `pids`（排序後 int list）、`selector`；**無 `holder_unknown` 欄位**（spec BC-04 Windows 欄位尚未實作） |
| `ECHO_ACK_UNSUPPORTED` | — | `file_transfer.py:109` `push_file` | RPC `file.push`（`ack_mode=echo`） | bridge 無 `send_command_echo_paced` | — | 無 TX |
| `EXCEPTION` | — | `rpc_posix.py:59` | RPC-frame | handler 拋例外（例如 `timeout_s="abc"` 的 `float()`、`int(params["limit"])`、`INVALID_INTERACTIVE_ENCODING` ValueError、base64 解碼錯、`RuleLoadError`） | 否 | 附 `message`；無 `version` |
| `FLASHING_BUSY` | — | `session_manager.py:1838` `clear_session`、`:1860` `release_device`、`:2046` `bind_session`、`:3644` `execute_command`、`:4231` `interactive_send`、`:4600` `recover_session`、`:2144` `_attach_state_result`（state FLASHING） | RPC `session.clear`/`device.release`/`session.bind`/`session.interactive_send`/`session.recover`/`session.attach`；REC（execute） | FLASHING 或 flash-critical（probe 臨界區，`_flash_busy_locked` `:1888-1890`） | 是（等 flash 結束） | 多數附 `selector`（=COM）、`session`；`interactive_send` 附 `interactive_id`；execute 只附 `selector` |
| `FORCE_RECOVER_TIMEOUT` | — | `session_manager.py:4686-4692` `_force_recover` | RPC `session.recover`（`force=true` 路徑） | clear+reattach 後 10×1s 內未 READY | 條件 | 已執行 `clear_session`（detach、spawn attach）；附 `action:"FORCE_CLEAR_REATTACH"`、`recovered:false`、`session` |
| `INTERACTIVE_EXPIRED` | — | `session_manager.py:4224` `interactive_send`、`:4257` `interactive_status` | RPC | lease 逾時 | 否（重開 lease） | 將 lease 標 expired 並 `_close_interactive_locked`（可能還原 human lease、resume） |
| `INTERACTIVE_NOT_FOUND` | — | `session_manager.py:4218` send（不存在或 status≠active）、`:4249` status、`:4277` close | RPC | interactive_id 不存在 | 否 | 附 `interactive_id` |
| `INVALID_ARGS` | — | service 分派：`service.py:732` device.release、`:740` device.attach、`:759` session.activity、`:767` session.self_test、`:779` session.recover、`:791` session.clear、`:798` session.bind、`:805` session.pin、`:811` session.unpin、`:817` session.attach、`:824` session.console_attach、`:831` session.console_detach、`:837` session.console_list、`:847` session.interactive_open、`:855` interactive_send、`:862` interactive_status、`:868` interactive_close、`:884` alias.assign、`:904` command.submit（selector 空）、`:929` command.result_tail（cmd_id 空）、`:979` log.tail_*（from_seq 非 int）、`:1054/1060/1066` session.log_start/stop/status、`:1083`（file.push chunk_size/chunk_timeout_s 轉型失敗）`:1087`（ack_mode 非 `_ACK_MODES`）`:1090`（缺 selector/local/remote）、`:1114/1118/1121` file.pull 同理；manager：`session_manager.py:2037` bind（by_id strip 後空）；file_transfer：`:96`（附 `ack_mode`）、`:101`（附 `chunk_size`）、`:370`（附 `max_console_line_chars`） | RPC | 參數缺漏／非法 | — | service 版**無附帶欄位**（只 `ok`、`error_code`＋`version`） |
| `INVALID_COMPLETION_REGEX` | `ERROR_INVALID_COMPLETION_REGEX` | `service.py:894` command.submit（最先檢查）；`arbiter.py:140` submit；`session_manager.py:3635` execute_command | RPC；REC | completion_regex 非法 | — | 無附帶欄位；**spec 未列** |
| `INVALID_RULE_SCHEMA` | — | `service.py:1138` | RPC `event.rule_set` | `ValueError`（含 `RuleSchemaError`，`event_engine/schema.py:218`） | — | 附 **`error`**（非 `message`）=str(e) |
| `LOCAL_FILE_NOT_FOUND` | — | `file_transfer.py:103` | RPC `file.push` | daemon 端看不到 local_path（`os.path.isfile` 以 daemon 身分） | — | 附 `local_path` |
| `LOG_OPEN_FAILED` | — | `session_manager.py:4787` `log_start` | RPC `session.log_start` | 開檔 OSError | 條件 | 附 **`detail`**；`os.makedirs(log_dir)` 若失敗則拋例外 → `EXCEPTION`（`:4775` 在 try 外） |
| `METHOD_NOT_FOUND` | — | `service.py:1174` | RPC | 未知 method | 否 | 附 `method` |
| `MOVE_FAILED` | — | `file_transfer.py:252` | RPC `file.push` | mv sentinel 未見 | 條件 | **不 cleanup**，tmp 可能殘留 |
| `MOVE_TIMEOUT` | — | `file_transfer.py:250` | RPC `file.push` | mv 後 prompt 逾時 | 條件 | 同上 |
| `NO_ACTIVE_CAPTURE` | — | `session_manager.py:4810` `log_stop` | RPC | 無 capture | 否 | 附 `session` |
| `NOT_READY` | — | `session_manager.py:2146` `_attach_state_result`（last_error 為 None 時的 fallback） | RPC `session.attach` | command-capable 且 state∉{READY, ATTACHING}、非 FLASHING/RECOVERING、無 last_error | 條件 | 附 `session`；**spec 未列** |
| `PROFILE_IS_EXPLICIT` | — | `session_manager.py:2118` pin、`:2129` unpin | RPC | session 來源是 `yaml-target` | — | 無附帶 |
| `PROFILE_NOT_COMMAND_CAPABLE` | — | `service.py:628-640` `_resolve_session_id` | RPC `command.submit`/`file.push`/`file.pull` | ATTACHED 且 `command_capable=false` | 否 | 附長 `hint`、`session` |
| `PROMPT_TIMEOUT` | — | `session_manager.py:3927-3932` `_recover_after_failure`（ok:false）；capture `error_code` `:3911` | RPC `session.recover`（READY 分支）；REC（命令紀錄與 capture） | 命令逾時，CTRL_C/CTRL_D 皆未救回 | 否（session 已轉 ATTACHED） | **副作用大**：可能送出 `\x03`、`\x04`（quiet 未 armed 時，逐 byte 重驗）；建立/終結 BackgroundCapture（status=error）；`_transition_to_attached(reason="PROMPT_TIMEOUT")` → state ATTACHED、last_error=PROMPT_TIMEOUT、`_on_detached`（arbiter unregister → 佇列 FLUSHED_BY_RECOVERY）；附 `partial:true`、`recovery_action:"NONE"` |
| `PROMPT_TIMEOUT_FORCE_RECOVERED` | — | `session_manager.py:3919-3925` | RPC `session.recover`（READY 分支 source=`system:recover` 才走 force fallback） | 逾時後 force clear+reattach 成功 | n/a | **`ok: true`**；附 `partial:true`、`recovery_action:"FORCE_CLEAR_REATTACH"`、`session` |
| `PROMPT_TIMEOUT_RECOVERED` | — | `session_manager.py:3897-3903`（回應）；capture `error_code` `:3887` | RPC `session.recover`；REC（**僅 capture**） | CTRL_C 或 CTRL_D 後 prompt 回來 | n/a | **`ok: true`**；附 `stdout`、`partial:true`、`recovery_action:"CTRL_C"|"CTRL_D"`；呼叫 `_on_command_flush(sid,"FLUSHED_BY_RECOVERY")` 終結排隊命令；arbiter 對 ok:true 結果**不寫 error_code**（命令紀錄 status=done、error_code=null），但 capture status=`error`、error_code=`PROMPT_TIMEOUT_RECOVERED` |
| `PULL_PARSE_FAILED` | — | `file_transfer.py:319` | RPC `file.pull` | 找不到 sentinel 區段且無 encoder failure sentinel | 條件 | — |
| `READY_UNCONFIRMED` | `ERROR_READY_UNCONFIRMED` | `service.py:655-666` `_resolve_session_id`；`session_manager.py:1141-1151` `_agent_gate_reject_locked` | RPC `command.submit`/`file.*`；REC | READY 再確認逾 300s 或 5 次 | **否** | 附 `hint`、`recommended_action:"self_test"`；service 版另附 `session`、manager 版附 `selector`；刻意**不帶** `retry_after_s` |
| `RULE_NOT_FOUND` | — | `service.py:1150` | RPC `event.rule_get` | rule 不存在 | 否 | 無附帶 |
| `SESSION_BUSY` | — | `session_manager.py:1062-1066` `_begin_foreground_operation_locked`（bridge None／foreground_busy／operation 存在）；`:3664` execute（interactive 模式且前景忙）；`:4028` interactive_open（READY 且前景忙）；`:4233` interactive_send（前景忙） | RPC `file.*`/`session.interactive_open`/`session.interactive_send`；REC | 同 session 前景操作進行中 | 是 | 附 `selector` 或 `interactive_id` |
| `SESSION_INTERACTIVE_BUSY` | — | `session_manager.py:3670-3674` execute（非 human 被非 human lease 擋，或 human 被任何 lease 擋）；`:4065-4069`、`:4162-4166` interactive_open；`:4895` file_push；`:4976` file_pull | RPC；REC | 已有不可搶占的 interactive lease | 是 | execute/open 附 `interactive_session_id`；file 版無附帶 |
| `SESSION_NOT_FOUND` | — | `session_manager.py:1720` set_alias（附 `session_id`）、`:1833` clear、`:1856` release、`:1925` enter_flashing、`:1940` exit_flashing、`:2008` attach_device、`:2042` bind、`:2158` attach、`:3937` get_session_state、`:4314-4319` self_test（附 lease context 欄位）、`:4587` recover、`:4768/4807/4826` log_*；`flash_endpoint.py:290`（dead，見 §7.3） | RPC（幾乎所有 selector 型 method；`command.submit`/`file.*`/`log.tail_*`/`result.tail` 經 `get_session_state` 間接） | selector 不對應任何 session | — | 多附 `selector` |
| `SESSION_NOT_READY` | — | `service.py:642` `_resolve_session_id`（附 `session`）；`arbiter.py:196`（session 未註冊 queue，附 `session_id`）；`session_manager.py:2465,2477,2515` `_probe_existing_bridge`；`:3646` execute；`:3950` attach_console；`:3983` detach_console；`:4006` list_consoles；`:4024,4082,4088,4111-4116（附 error_detail:"NOT_BOOTLOADER"）,4179` interactive_open；`:4228` send；`:4252` status；`:4614,4623` recover；`:4645` recover REPROBE fallback；`:4879` file_push；`:4962` file_pull | RPC；REC | state 不符（非 READY；或 bridge None） | 條件（等 READY） | `interactive_open` 有 `error_detail:"NOT_BOOTLOADER"` 子碼 |
| `SESSION_QUEUE_FULL` | — | `arbiter.py:202-211` | RPC `command.submit` | 該 session pending（無 done_at）≥ 256 | 是（等排空） | 附 `session_id`、`pending`、`limit`、`hint` |
| `SESSION_RECOVERING` | — | `session_manager.py:2145` `_attach_state_result`（state RECOVERING 且有 bridge）；`:3660` execute、`:4889` file_push、`:4970` file_pull（`session.recovering` 旗標，見 §7.4 實務不可達） | RPC `session.attach`；REC | session 恢復中 | 是 | attach 版附 `session` |
| `STATE_CHANGED` | — | `session_manager.py:2521` `_probe_existing_bridge` | RPC `session.attach`/`session.recover` | probe 期間被搶進 FLASHING/RELEASED | 條件 | 附 `session`；attach 在此情況若已 RELEASED 會改回 `released:true` ok 回應（`:2207-2219`） |
| `TARGET_DECODER_FAILED` | — | `file_transfer.py:219-226` | RPC `file.push` | chunk sentinel 未見 | 條件 | cleanup tmp；附 `chunks_sent`、`chunks_total` |
| `TARGET_DECODER_MISSING` | — | `file_transfer.py:522` `_probe_decoder` → `:157` | RPC `file.push` | target 無 base64/openssl decoder | 否 | probe 命令已 TX；**spec 未列** |
| `TARGET_ENCODER_FAILED` | — | `file_transfer.py:318` | RPC `file.pull` | encoder failure sentinel 出現 | 條件 | — |
| `TARGET_ENCODER_MISSING` | — | `file_transfer.py:544` `_probe_encoder` → `:300` | RPC `file.pull` | target 無 encoder | 否 | **spec 未列** |
| `TRANSFER_ECHO_STALL` | — | `file_transfer.py:198-206` | RPC `file.push`（echo-ACK 路徑） | echo 停滯 | **是**（換行未送出＝命令未執行，註解 `:91-93`） | `bridge.cancel_input_line()` + cleanup；附 `chunks_sent`、`chunks_total`、`acked_chars`、`sent_chars` |
| `TRANSFER_PROBE_FAILED` | — | `file_transfer.py:521`／`:543`（probe 回 shell error） | RPC `file.push`/`file.pull` | 工具探測命令 shell 錯誤 | 條件 | **spec 未列** |
| `TRANSFER_TIMEOUT` | — | `file_transfer.py:210-216`（chunk，附 `chunks_sent/total`）、`:310`（pull）、`:514`／`:536`（probe） | RPC `file.*` | prompt 逾時 | 條件 | chunk 版 cleanup tmp |
| `WAL_MISSING` | — | `service.py:545-563` `_wal_missing_response` | RPC `log.tail_raw`/`log.tail_text`/`wal.range` | WAL 曾寫過（`current_seq>0`）但現行檔不存在 | 條件 | 附 `wal_path`、`wal_dir`、`wal_dir_exists`、`wal_file_exists:false`、`current_seq`、`hint`（文字含 `serialwrap doctor`） |

### 3.2 RPC framing 層

| code | 位置 | 意義 | 附註 |
|---|---|---|---|
| `INVALID_JSON` | `rpc_posix.py:31` | 該行不是合法 JSON（`json.JSONDecodeError`） | 無 `id`／`version`；**非緊湊分隔符**；連線不斷，續讀下一行 |
| `INVALID_REQUEST` | `rpc_posix.py:38` | JSON 非 object | 同上 |
| `INVALID_METHOD` | `rpc_posix.py:47` | `method` 非字串 | 有 `id`，無 `version` |
| `EXCEPTION` | `rpc_posix.py:59` | handler 例外 | 有 `id`、`message` |

### 3.3 CLI-client（`sw_core/client.py`，CLI 本地）

| code | 位置 | 意義 | 可重試 | 附帶 |
|---|---|---|---|---|
| `INVALID_ENDPOINT` | `client.py:232` | `_parse_endpoint` ValueError（tcp 缺 host/port、unix 非絕對、未知 scheme） | — | `message` |
| `SOCKET_ERROR` | `client.py:239-244`（無 AF_UNIX 平台連 unix endpoint）、`:252`（connect OSError）、`:276`（send/recv OSError） | 連線失敗 | 是（僅白名單方法） | `message`；trace 另記 errno |
| `TIMEOUT` | `client.py:249`（connect `socket.timeout`）、`:273`（recv 逾時）、`:160`（初值） | 逾時 | 白名單方法是；其他**否**（daemon 可能仍在執行） | 非 probe 方法追加 `daemon_reachable`/`daemon_busy` |
| `EMPTY_RESPONSE` | `client.py:267` | 連線關閉但未收到任何一行 | 是（白名單） | — |
| `INVALID_RESPONSE` | `client.py:270` | 回應 JSON 非 object | 否 | — |

### 3.4 CLI-local（`cli.py`／`service_ctl.py`／`remote_tunnel.py`／`bench`）

| code | 位置 | 意義 | exit | 附帶／副作用 |
|---|---|---|---|---|
| `BENCH_ENDPOINT_NOT_REMEMBERED` | `cli.py:758-761` `_resolve_endpoint_info`（`_EndpointResolutionError`） | `--bench` 尚未 `connect` 記住 endpoint | 1 | `message`；**spec 未列** |
| `BENCH_STATE_IO_ERROR` | `cli.py:756`；`:1376,:1404,:1490`（TunnelError）；`:1440`（直接） | benches.state.json OSError | 1 | `message`；`:1440` 路徑先嘗試 rollback，附 `rollback_warning` |
| `DAEMON_ALREADY_RUNNING_ELSEWHERE` | `cli.py:511-522` `_run_daemon_start` | `/proc` 發現另一 serialwrapd 綁不同 socket（POSIX、非 `--force-spawn`） | 2 | `message`、`hint`、`existing_socket`（可 null）、`existing_pid`、`target_socket`；**未呼叫 `_mirror_err`**（stderr 無行） |
| `DAEMON_BINARY_NOT_FOUND` | `cli.py:527-535` | win backend 找不到 serialwrapd | 2 | `message` |
| `DAEMON_EXITED` | `cli.py:589-593` | spawn 後 daemon 退出 | 2 | `pid`、`returncode` |
| `DAEMON_NOT_READY` | `cli.py:618-621` | POSIX 15×0.2s／win 50×0.2s 內 `health.ping` 未成功 | 2 | `pid`；daemon 行程仍留著 |
| `ENV_FILE_SOURCE_FAILED` | `cli.py:551-562` | env 檔 `bash -lc source` 非 0，或 Windows 讀檔 OSError | 2 | `env_file`、`message`、（有時）`env_files` |
| `FLASHING_BUSY` | `cli.py:1707-1712`（mcu.status flashing 且非 force）；`:1778-1786`（`reconcile` 拋 `FlashingBusy`，`setup_cmd.py:252`） | setup 拒絕 | 2 | `message`、（後者）`legacy` |
| `INTERNAL_ERROR` | `cli.py:138` `_print_internal_error`（event dispatch、daemon stop）；`:1456` connect；`:1526` benches；`:1615` remote | CLI 內未預期例外 | 1 | `message`=str(exc) |
| `INVALID_ARGS` | `cli.py:442-449`（daemon start `--endpoint`≠`--socket`，exit 2）；`:2485`（argparse SystemExit(2) 且命令為 `connect`/`benches`，exit **1**）；`:2717`（main fallthrough，exit 2，實務不可達）；`:1570,1574,1576`（remote，TunnelError，exit 1） | 參數錯誤 | 見左 | `message`（部分） |
| `INVALID_BENCH_CONFIG` | `cli.py:1355`、`:1482` | benches.yaml 驗證錯（`bench_registry` ValueError） | 1 | **spec 未列** |
| `INVALID_BENCH_STATE` | `cli.py:754`；`:1374,1402,1488,1505`（TunnelError）；`:1428`（直接，附 rollback_warning） | benches.state.json 結構錯 | 1 | — |
| `INVALID_COMPLETION_REGEX` | `cli.py:2591-2594` | `cmd submit` 本地預檢 | **1** | 不連 daemon |
| `INVALID_ENDPOINT` | `cli.py:1585` remote（`_resolve_endpoint` ValueError） | — | 1 | — |
| `INVALID_TARGET` | `remote_tunnel.py:54,57` `parse_target` | `[user@]host:port` 格式錯或 port 越界 | 1 | **spec 未列** |
| `NEEDS_SUDO` | `service_ctl.py:42-49` | systemd-system 的 start/stop/restart 未帶 `--with-sudo` | 2 | `mode`、`action`、`hint`；CLI `service`、`daemon start/stop` 路由皆可能 |
| `NO_SYSTEMD` | `service_ctl.py:55-61` | on-demand／未知模式呼叫 service | 2 | `mode`、`action`、`hint` |
| `REMOTE_BIND_UNVERIFIED` | `remote_tunnel.py:313,315` | expose tcp 模式 relay 上 `ss` 驗證失敗或非 loopback | 1 | **spec 未列** |
| `REMOTE_NOT_SUPPORTED` | `cli.py:434-438`（daemon start `--endpoint`，exit **2**）；`:1334-1342` connect、`:1464-1472` benches、`:1545-1553` remote（native Windows，exit 1）；`remote_tunnel.py:586-590` `guard_platform` | 不支援 | 見左 | `message` |
| `SAMPLE_READ_FAILED` | `cli.py:1885` | profile test 讀樣本檔失敗 | **0** | `message` |
| `SSH_NOT_FOUND` | `remote_tunnel.py:598` `resolve_ssh_bin` | PATH 無 ssh/autossh | 1 | **spec 未列** |
| `TUNNEL_CONFLICT` | `remote_tunnel.py:451-454`；`cli.py:1307-1310` | port 已有不同 identity 的 tunnel | 1 | **spec 未列** |
| `TUNNEL_SPAWN_FAILED` | `remote_tunnel.py:180` | ssh 行程在 ready 前死亡 | 1 | `message`=stderr 尾 500 字；**spec 未列** |
| `TUNNEL_STATUS_ERROR` | `cli.py:1265,1270-1273（rt.status 失敗但無 code 時的 fallback），1275` | remote status 回應異常 | 1 | **spec 未列**；若 status 回應帶 code 則透傳該 code |
| `TUNNEL_STILL_ACTIVE` | `cli.py:1390-1398` | connect --close 後 tunnel 仍在 | 1 | 不清除 endpoint 記憶 |
| `UNKNOWN_BENCH_CODE` | `cli.py:1353` | `bench_registry.resolve` KeyError | 1 | message=`unknown bench code: <code>`；**spec 未列** |
| `UNKNOWN_EVENT_CMD` | `cli.py:1064-1067` | event 子命令未知 | 2 | `cmd`；argparse `required=True`（`cli.py:2305`）使其實務不可達 |
| `UNKNOWN_PROFILE` | `cli.py:1870-1878` `_run_profile_test`（exit **0**，附 `profile`、`profile_dir`、`available`）；RPC 版見 3.1 外：`session_manager.py:2113` `pin_session`（RPC `session.pin`） | profile 名不存在 | 見左 | — |

### 3.5 REC：只出現在命令紀錄／capture 的碼

命令紀錄（`arbiter._commands[cmd_id]`，經 `command.get` → `command.error_code`，以及 `command.result_tail` fallback `service.py:347-357` 的頂層 `error_code`）：
- 初始 `error_code: None`（`arbiter.py:184`）。
- `FLUSHED_BY_RECOVERY`：`arbiter.unregister_session` 預設（`arbiter.py:60`，由 `service._on_detached` `service.py:289-290` 觸發，即任一 `_on_detached` 呼叫：detach、轉 ATTACHED、reboot recovery、`_transition_to_attached` 等）；`flush_session`（`arbiter.py:86`，`service._on_command_flush` `:292-296`，由 `session_manager.py:3895-3896` CTRL_C/CTRL_D 成功後呼叫）。只標記 `status=="accepted"` 且 `done_at is None` 者 → `status="error"`、`error_code`、`done_at`（`arbiter.py:116-124`）。語意：**未執行、可在 READY 後重送**。
- `FLUSHED_BY_SHUTDOWN`：`service.stop()` `service.py:532`（daemon 停止時對每個 session `unregister_session(sid, error_code="FLUSHED_BY_SHUTDOWN")`）。語意同上（可重送），但辨識為 daemon 停止。
- `SEND_FAILED`：`arbiter.py:297`，`send_cb`（`execute_command`）拋例外。**spec 未列**；是否已 TX 不明（例外可能發生在送出後）→ 不可假設安全重試。
- `COMMAND_FAILED`：`arbiter.py:318`，結果 `ok:false` 但沒有 `error_code` 時的 fallback。**spec 未列**（目前 execute_command 所有 ok:false 皆帶碼，實務不可達但為合約 fallback）。
- 透傳 execute_command 的 ok:false 碼：`FLASHING_BUSY`、`SESSION_NOT_READY`、`READY_UNCONFIRMED`、`AUTOBOOT_QUIET`、`SESSION_RECOVERING`、`SESSION_BUSY`、`SESSION_INTERACTIVE_BUSY`、`INVALID_COMPLETION_REGEX`、`PROMPT_TIMEOUT`。
- ok:true 結果（含 `PROMPT_TIMEOUT_RECOVERED`）**不寫** error_code；`status` 依 `result.status=="interactive"` → `interactive`，否則 `done`。被 cancel 的紀錄不被 worker 覆寫（`arbiter.py:304`）。
- 命令紀錄 `status` 列舉：`accepted`、`running`、`done`、`error`、`canceled`、`interactive`。

BackgroundCapture（`session_manager.py:195-229`，`get_background_result` `:4717-4744` 頂層 `error_code`）：
- `error_code` 只會是 `None`、`"PROMPT_TIMEOUT_RECOVERED"`（`:3887`）、`"PROMPT_TIMEOUT"`（`:3911`），由 `_set_terminal_capture_locked`（`:3496-3528`）設定；`status = "error" if error_code else "done"`；`cmd_id==""`（recover 路徑）時不建 capture。
- capture `status` 列舉：`active`、`done`、`error`；`maybe_finalize()` 在 quiet_window 後 active→done。

### 3.6 PROC：daemon 行程層（非 JSON）

| 字串 | 位置 | 行為 |
|---|---|---|
| `DAEMON_ALREADY_RUNNING` | `lock_posix.py:22`（flock 失敗）、`:40`（socket 可連）；`lock_win.py:37`（endpoint 可連）、`:50`（`msvcrt.locking` 失敗） | `daemon.py:146-150`：stderr `serialwrapd: DAEMON_ALREADY_RUNNING\n`，exit **2**；**不是任何 JSON 回應的 error_code** |
| `SOCKET_UNAVAILABLE` | `lock_posix.py:37`（socket 存在但 connect 錯誤非 ENOENT/ECONNREFUSED） | 同上 stderr＋exit 2；**spec 未列** |
| `StateLoadError` 訊息 | `daemon.py:158-163` | stderr、釋放 lock、exit **3** |

### 3.7 非錯誤但同名空間的碼（避免與 error_code 混淆）
- `warning.code = "CMD_LENGTH_WARNING"`（`arbiter.py:131-137`，cmd>4096 bytes 時 submit 成功回應附 `warning: {code, cmd_length, soft_limit, hint}`）。
- `recovery_action`：`"PROMOTE_HUMAN_INTERACTIVE"`、`"EXPECT_REBOOT"`、`"CTRL_C"`、`"CTRL_D"`、`"FORCE_CLEAR_REATTACH"`、`"NONE"`；`action`（recover）：`"REATTACH"`、`"REPROBE"`、`"FORCE_CLEAR_REATTACH"`。
- `error_detail: "NOT_BOOTLOADER"`（`session_manager.py:4116`）。
- `TIMEOUT` 也是 `SystemEffects.run` 逾時時的 stderr 字串（`sysenv.py:86`，回 `(-1,"","TIMEOUT")`），與 RPC TIMEOUT 無關。
- `util.shell_command_incomplete_reason`（`util.py:69-139`）的 `EMPTY_COMMAND`、`NUL_BYTE`、`UNBALANCED_SINGLE_QUOTE`、`UNBALANCED_DOUBLE_QUOTE`、`UNBALANCED_BACKTICK`、`TRAILING_OPERATOR`、`TRAILING_BACKSLASH`：**production 無呼叫端**（只有 `tests/test_command_guard.py`），不會出現在任何回應。
- `INVALID_INTERACTIVE_ENCODING`：`session_manager.py:4210` `raise ValueError(...)`，經 framing 成為 `{"error_code":"EXCEPTION","message":"INVALID_INTERACTIVE_ENCODING"}`（而非獨立 error_code）。
- Self-test `classification`（§6）。

---

## 4. RPC method → 可能 error_code 矩陣（含透傳）

| method | 可能 error_code |
|---|---|
| `health.ping`/`health.status`/`mcu.patterns`/`mcu.status`/`device.list`/`session.list`/`alias.list`/`wal.reset`/`wal.current_seq`/`event.rule_delete`/`event.rule_list`/`event.com_*`/`event.reset`/`event.reload`/`event.tail` | 僅 framing（`EXCEPTION` 等） |
| `device.release` | `INVALID_ARGS`、`SESSION_NOT_FOUND`、`FLASHING_BUSY` |
| `device.attach` | `INVALID_ARGS`、`SESSION_NOT_FOUND`、`DEVICE_NOT_PRESENT`、`DEVICE_STILL_HELD` |
| `session.get_state` | `SESSION_NOT_FOUND`（空 selector 亦同） |
| `session.activity` | `INVALID_ARGS`、`SESSION_NOT_FOUND` |
| `session.self_test` | `INVALID_ARGS`、`SESSION_NOT_FOUND`（其餘以 `ok:true`+classification 表達） |
| `session.recover` | `INVALID_ARGS`、`SESSION_NOT_FOUND`、`FLASHING_BUSY`、`SESSION_NOT_READY`、`STATE_CHANGED`、`FORCE_RECOVER_TIMEOUT`、`PROMPT_TIMEOUT`、(ok:true) `PROMPT_TIMEOUT_RECOVERED`/`PROMPT_TIMEOUT_FORCE_RECOVERED`、(ok:true REPROBE payload) **任一 `last_error` 值**或 `SESSION_NOT_READY`（`:4644-4645`，並可附 `classification:"BOOTLOADER"`、`recommended_action:"recover_interactive"`、`matched_prompt`） |
| `session.clear` | `INVALID_ARGS`、`SESSION_NOT_FOUND`、`FLASHING_BUSY` |
| `session.bind` | `INVALID_ARGS`、`SESSION_NOT_FOUND`、`FLASHING_BUSY`、`DEVICE_ALREADY_BOUND` |
| `session.pin` | `INVALID_ARGS`、`UNKNOWN_PROFILE`、`DEVICE_NOT_FOUND`、`PROFILE_IS_EXPLICIT` |
| `session.unpin` | `INVALID_ARGS`、`DEVICE_NOT_FOUND`、`PROFILE_IS_EXPLICIT` |
| `session.attach` | `INVALID_ARGS`、`SESSION_NOT_FOUND`、`DEVICE_NOT_BOUND`、`DEVICE_NOT_FOUND`、`FLASHING_BUSY`、`SESSION_RECOVERING`、`SESSION_NOT_READY`、`STATE_CHANGED`、`NOT_READY`、**任一 ATTACHED 態 `last_error` 值**（§5.4） |
| `session.console_attach` | `INVALID_ARGS`、`SESSION_NOT_READY` |
| `session.console_detach` | `INVALID_ARGS`、`SESSION_NOT_READY`、`CONSOLE_NOT_FOUND` |
| `session.console_list` | `INVALID_ARGS`、`SESSION_NOT_READY` |
| `session.interactive_open` | `INVALID_ARGS`、`SESSION_NOT_READY`（含 `error_detail:"NOT_BOOTLOADER"`）、`SESSION_BUSY`、`SESSION_INTERACTIVE_BUSY` |
| `session.interactive_send` | `INVALID_ARGS`、`INTERACTIVE_NOT_FOUND`、`INTERACTIVE_EXPIRED`、`SESSION_NOT_READY`、`FLASHING_BUSY`、`SESSION_BUSY`、`EXCEPTION`（encoding） |
| `session.interactive_status` | `INVALID_ARGS`、`INTERACTIVE_NOT_FOUND`、`SESSION_NOT_READY`、`INTERACTIVE_EXPIRED` |
| `session.interactive_close` | `INVALID_ARGS`、`INTERACTIVE_NOT_FOUND` |
| `alias.set` | `SESSION_NOT_FOUND` |
| `alias.assign` | `INVALID_ARGS` |
| `alias.unassign` | `ALIAS_NOT_FOUND` |
| `command.submit` | `INVALID_COMPLETION_REGEX`、`INVALID_ARGS`、`SESSION_NOT_FOUND`、`PROFILE_NOT_COMMAND_CAPABLE`、`SESSION_NOT_READY`、`READY_UNCONFIRMED`、`AUTOBOOT_QUIET`、`CMD_TOO_LONG`、`CMD_CONTAINS_NEWLINE`、`SESSION_QUEUE_FULL`（gate 順序：regex→selector→state→READY_UNCONFIRMED→quiet→pending→arbiter：regex→len→newline→queue→pending） |
| `command.get` | `CMD_NOT_FOUND`（成功時 `command.error_code` 見 §3.5） |
| `command.result_tail`/`result.tail`(cmd_id) | `INVALID_ARGS`（僅 command.result_tail）、`CMD_NOT_FOUND`；成功時頂層 `error_code` 可為 null/`PROMPT_TIMEOUT`/`PROMPT_TIMEOUT_RECOVERED`（capture）或 §3.5 命令紀錄碼（fallback） |
| `result.tail`(無 cmd_id) | `SESSION_NOT_FOUND` |
| `command.cancel` | `CMD_NOT_FOUND`、`CMD_NOT_CANCELABLE` |
| `log.tail_raw`/`log.tail_text` | `INVALID_ARGS`、`SESSION_NOT_FOUND`、`WAL_MISSING` |
| `wal.range` | `WAL_MISSING`（非 int 參數 → `EXCEPTION`） |
| `session.log_start` | `INVALID_ARGS`、`SESSION_NOT_FOUND`、`LOG_OPEN_FAILED` |
| `session.log_stop` | `INVALID_ARGS`、`SESSION_NOT_FOUND`、`NO_ACTIVE_CAPTURE` |
| `session.log_status` | `INVALID_ARGS`、`SESSION_NOT_FOUND` |
| `file.push` | `INVALID_ARGS`、`SESSION_NOT_FOUND`、`PROFILE_NOT_COMMAND_CAPABLE`、`SESSION_NOT_READY`、`READY_UNCONFIRMED`、`AUTOBOOT_QUIET`、`SESSION_RECOVERING`、`SESSION_INTERACTIVE_BUSY`、`SESSION_BUSY`、`CONSOLE_LINE_LIMIT_TOO_SMALL`、`LOCAL_FILE_NOT_FOUND`、`ECHO_ACK_UNSUPPORTED`、`TRANSFER_TIMEOUT`、`TRANSFER_PROBE_FAILED`、`TARGET_DECODER_MISSING`、`TRANSFER_ECHO_STALL`、`TARGET_DECODER_FAILED`、`CHECKSUM_VERIFY_FAILED`、`CHECKSUM_MISMATCH`、`MOVE_TIMEOUT`、`MOVE_FAILED` |
| `file.pull` | `INVALID_ARGS`、`SESSION_NOT_FOUND`、`PROFILE_NOT_COMMAND_CAPABLE`、`SESSION_NOT_READY`、`READY_UNCONFIRMED`、`AUTOBOOT_QUIET`、`SESSION_RECOVERING`、`SESSION_INTERACTIVE_BUSY`、`SESSION_BUSY`、`CONSOLE_LINE_LIMIT_TOO_SMALL`、`TRANSFER_TIMEOUT`、`TRANSFER_PROBE_FAILED`、`TARGET_ENCODER_MISSING`、`TARGET_ENCODER_FAILED`、`PULL_PARSE_FAILED`、`BASE64_DECODE_FAILED`、`CHECKSUM_VERIFY_FAILED`、`CHECKSUM_MISMATCH` |
| `event.rule_set` | `INVALID_RULE_SCHEMA` |
| `event.rule_get` | `RULE_NOT_FOUND` |
| 其他 | `METHOD_NOT_FOUND` |

---

## 5. Session `last_error` 盤點

### 5.1 儲存與序列化
- 欄位：`SessionRuntime._last_error: str | None`（`session_manager.py:395`，`init=False`，預設 `None`）；property getter/setter `:397-408`。**setter 在值改變時自動把 `last_error_detail` 清成 None**（`:405-407`）；需附 detail 時必須先設 last_error 再設 detail。
- 對外：`to_public_dict()` 的 `"last_error"`、`"last_error_detail"`（`:573-575`）——出現在所有含 `session` 的回應、`session.list`、`session.get_state` 等。
- **不持久化**：`_save_state`／`state.json` 不含 last_error（全 repo grep 無其他引用）；daemon 重啟後一律 None。
- 未有錯誤時為 JSON `null`。
- 名義 READY 時 last_error 恆為 None（不變量，註解 `:1534-1537`、`:2479-2484`）；但 reprobe 失敗中的 READY 再確認分支刻意不改 last_error。

### 5.2 值集合（含來源）
login FSM（`sw_core/login_fsm.py`，經 `probe_ready`/`ensure_ready` 回傳 `err` 後寫入）：
| 值 | 產生點 |
|---|---|
| `PROMPT_UNAVAILABLE` | `_classify_non_ready_state` `:137`；另 boot-quiet 合成：`session_manager.py:2484`（probe_existing_bridge，非 READY 時）、`:2639`（attach_by_id，`ok,err=False,"PROMPT_UNAVAILABLE"`） |
| `LOGIN_REQUIRED` | `:136`（非 READY 且 rx tail 命中 login/password regex）、`:172`、`:181`、`:200`（#174 guard/分流） |
| `USER_ENV_MISSING` | `:100`（**注意**：`_maybe_login` 取得 err 後未使用，`user` 為 None 時 `needs_login=bool(sp.pass_env)`；實際上此值僅在 `_resolve_login_user` 回傳，`_maybe_login` 會覆寫 `err`——見 §7.5） |
| `PASS_ENV_REQUIRED` | `:112` |
| `PASS_ENV_MISSING` | `:115` |
| `LOGIN_USER_REQUIRED` | `:219` |
| `LOGIN_PROMPT_TIMEOUT` | `:214` |
| `BCM_PROMPT_TIMEOUT`／`SHELL_PROMPT_TIMEOUT`／`PRPL_PROMPT_TIMEOUT` | `_prompt_timeout_error` `:119-125`（platform bcm／shell／其他一律 PRPL） |
| `POST_LOGIN_CMD_TIMEOUT` | `:174` |
| `READY_NONCE_TIMEOUT` | `:195` |
| `READY_PROMPT_TIMEOUT` | `:202` |
| `CREDENTIALS_UNRESOLVED` | `login_fsm.py:288`（console_unlock 無 secret）；`session_manager.py:2497,2647,3083,3432`（`_credentials_declared_but_unresolved` → 不 probe） |
| `CONSOLE_UNLOCK_IDLE_TIMEOUT` | `login_fsm.py:291,306,315` |
| `CONSOLE_UNLOCK_BOOTLOADER` | `login_fsm.py:293,308` |
| `CONSOLE_UNLOCK_PROMPT_TIMEOUT` | `login_fsm.py:323` |
| `RX_FLOOD` | `_maybe_reclassify_flood` `login_fsm.py:47-73`：err ∈ `_FLOOD_MASKABLE_ERRORS` 或以 `_PROMPT_TIMEOUT` 結尾，且 `bridge.rx_stats()["rx_bytes_last_10s"] >= 20000` |
| `TRANSPORT_STALL` | `transport_stall.classify_probe_failure`（`transport_stall.py:39-69`）經 `session_manager._refine_probe_failure`（`:1268-1330`）：err 為 `PROMPT_UNAVAILABLE`/`LOGIN_PROMPT_TIMEOUT`/`*_PROMPT_TIMEOUT`、probe 期間 rx_delta==0、`last_rx_mono>0`、RX 凍結 ≥30s |

session_manager 直接寫入：
| 值 | 位置（function） | 伴隨 state |
|---|---|---|
| `BOOTLOADER_STUCK` | `:1493` `_finish_probe_reprobe`（同時 `last_error_detail="matched_prompt=<pattern>"`、`reprobe_exhausted=True`、`next_reprobe_at=None`、`expire_ready_reconfirm(force=True)`） | ATTACHED（READY 時 `_finish_probe_reprobe` 於 `:1474-1476` 先 return，故不會寫在 READY 上） |
| `REPROBE_FAILED:<ExcClassName>` | `:1584` reprobe worker `_run`（probe 拋例外） | 設 ATTACHED |
| `CLEARED` | `:1841`→`_detach_session_locked` `:1807`（若裝置在線，`:1846` 立刻改回 None） | DETACHED→ATTACHING |
| `RELEASED` | `:1866` release_device | RELEASED |
| `REBOUND` | `:2063` bind_session（之後若裝置在線 `:2084` → None，否則 `:2091` → DEVICE_NOT_FOUND） | DETACHED |
| `DEVICE_REBOUND_REQUIRED`／`DEVICE_REMOVED` | `:2280` update_devices → `_detach_by_id` `:2318` | DETACHED |
| `BRIDGE_DOWN:<reason>` | `:2453` `_handle_bridge_down`；reason = `SERIAL_READ:<errno|ExcName>`／`SELECT:<errno|ExcName>`（`uart_io.py:876,895,905`）；若裝置在線 `:2456` 立刻改 None | DETACHED→RECOVERING/ATTACHING |
| `DEVICE_NOT_FOUND` | `:2091` bind、`:2188` attach_session、`:2250` `_mark_missing_devices_locked`（僅在 last_error 為 None 時）、`:2593` `_attach_by_id` | DETACHED |
| `DEVICE_REMOVED_DURING_ATTACH` | `:2700`、`:3123` | DETACHED |
| `ATTACH_FAILED:<ExcClassName>` | `:2749`、`:3166`（bridge.start/probe 例外） | DETACHED |
| probe `err`（上表 login FSM 值） | `:2542` probe_existing_bridge（ATTACHED）、`:2662`/`:3092`（ensure_ready 失敗 → **DETACHED**）、`:2724`/`:3149`（ATTACHED） | 見左 |
| `PROMPT_TIMEOUT` | `:3356` `_transition_to_attached(reason="PROMPT_TIMEOUT")`（`:3913`） | ATTACHED |
| `RECOVERY_ERROR` | `:3441`→`:3466` reboot recovery（ensure_ready 例外） | RECOVERING（迴圈中） |
| reboot recovery `err` | `:3466`（任一 login FSM 值或 CREDENTIALS_UNRESOLVED） | RECOVERING |
| `RECOVERY_TIMEOUT` | `:3490`（bridge None → DETACHED）、`:3493`（`last_error or "RECOVERY_TIMEOUT"` → ATTACHED，保留既有錯） | DETACHED／ATTACHED |
| `REBOOTING` | `:3589` `_handle_reboot_command`（human 來源） | ATTACHED |

完整列舉（不含動態後綴）：`PROMPT_UNAVAILABLE`、`LOGIN_REQUIRED`、`USER_ENV_MISSING`、`PASS_ENV_REQUIRED`、`PASS_ENV_MISSING`、`LOGIN_USER_REQUIRED`、`LOGIN_PROMPT_TIMEOUT`、`BCM_PROMPT_TIMEOUT`、`SHELL_PROMPT_TIMEOUT`、`PRPL_PROMPT_TIMEOUT`、`POST_LOGIN_CMD_TIMEOUT`、`READY_NONCE_TIMEOUT`、`READY_PROMPT_TIMEOUT`、`CREDENTIALS_UNRESOLVED`、`CONSOLE_UNLOCK_IDLE_TIMEOUT`、`CONSOLE_UNLOCK_BOOTLOADER`、`CONSOLE_UNLOCK_PROMPT_TIMEOUT`、`RX_FLOOD`、`TRANSPORT_STALL`、`BOOTLOADER_STUCK`、`CLEARED`、`RELEASED`、`REBOUND`、`DEVICE_REBOUND_REQUIRED`、`DEVICE_REMOVED`、`DEVICE_NOT_FOUND`、`DEVICE_REMOVED_DURING_ATTACH`、`PROMPT_TIMEOUT`、`RECOVERY_ERROR`、`RECOVERY_TIMEOUT`、`REBOOTING`（31 個），加動態前綴 `REPROBE_FAILED:*`、`ATTACH_FAILED:*`、`BRIDGE_DOWN:SERIAL_READ:*`、`BRIDGE_DOWN:SELECT:*`。

### 5.3 清除（設為 None）時機
`:1463` `_prepare_reprobe_locked`（DETACHED→ATTACHING 自動重探）、`:1846` clear_session（裝置在線）、`:2027` attach_device、`:2084` bind_session（裝置在線）、`:2193` attach_session（非 probe 分支→ATTACHING）、`:2456` bridge_down（裝置在線）、`:2536` probe 成功→READY、`:2602` `_attach_by_id` 開始→ATTACHING、`:2715` attach 成功→READY、`:3053`／`:3138` dynamic 同上、`:3454` reboot recovery 成功→READY、`:3610` reboot（agent 來源）→RECOVERING、`:4610` recover_session（bridge None 且裝置在線→REATTACH）。
- `CREDENTIALS_UNRESOLVED` 的唯一解除方式：使用者明確 `session.attach`（`:2178-2181`、`:2195-2196` 並重置 `credentials_warned=False`）、`session.recover`（`:4604-4605`）、或 daemon 重啟（不持久化）。自動重探對它早退（`_prepare_reprobe_locked` `:1416-1417`；`_is_reprobe_prompt_error` 不含此值）。
- reprobe 資格（`_is_reprobe_prompt_error` `:1332-1356`）：ATTACHED 時 `PROMPT_UNAVAILABLE`、`PROMPT_TIMEOUT`、`*_PROMPT_TIMEOUT`、`RX_FLOOD`、`TRANSPORT_STALL`；DETACHED 時同上去掉 `PROMPT_UNAVAILABLE`。其餘值（`LOGIN_REQUIRED`、`REBOOTING`、`BOOTLOADER_STUCK`、`CREDENTIALS_UNRESOLVED`、`READY_NONCE_TIMEOUT` 等非 `*_PROMPT_TIMEOUT`）**不會自動重探**（注意：`READY_NONCE_TIMEOUT`、`POST_LOGIN_CMD_TIMEOUT`、`USER_ENV_MISSING`…不在資格內）。

### 5.4 last_error 提升為 RPC error_code 的路徑（Rust 必須保留）
1. `session.attach` → `_attach_state_result`（`session_manager.py:2133-2148`）：command-capable 且 state∉{READY,ATTACHING} 時 `error_code = {"FLASHING":"FLASHING_BUSY","RECOVERING":"SESSION_RECOVERING"}.get(state) or last_error or "NOT_READY"`。只在 bridge 存在時被呼叫，故實際可達 last_error 集合＝ATTACHED 態可能值：login FSM 值、`CREDENTIALS_UNRESOLVED`、`RX_FLOOD`、`TRANSPORT_STALL`、`BOOTLOADER_STUCK`、`PROMPT_UNAVAILABLE`、`PROMPT_TIMEOUT`、`REBOOTING`、`RECOVERY_TIMEOUT`、`RECOVERY_ERROR`（RECOVERY 收尾保留）、`REPROBE_FAILED:*`。測試已斷言 `PROMPT_UNAVAILABLE` 作為 error_code（`tests/` 中）。
2. `session.recover` REPROBE 分支（`:4636-4656`）：**`ok: true`** 且 `recovered:false` 時 `error_code = last_error or "SESSION_NOT_READY"`；若 rx tail 命中 bootloader 或 error_code==`BOOTLOADER_STUCK` 再附 `classification:"BOOTLOADER"`、`recommended_action:"recover_interactive"`、（命中時）`matched_prompt`。

### 5.5 `last_error_detail`
- `TRANSPORT_STALL`：`transport_stall_hint(real_path, rx_age_s)`（`transport_stall.py:94-112`，繁中文字，含 busid 時附 `sudo sh -c 'echo 0 > /sys/bus/usb/devices/<busid>/authorized; echo 1 > …'`）。
- `LOGIN_FSM_DETAIL_ERRORS`（`USER_ENV_MISSING`、`PASS_ENV_REQUIRED`、`PASS_ENV_MISSING`、`LOGIN_USER_REQUIRED`、`LOGIN_PROMPT_TIMEOUT`、`BCM_PROMPT_TIMEOUT`、`SHELL_PROMPT_TIMEOUT`、`PRPL_PROMPT_TIMEOUT`、`POST_LOGIN_CMD_TIMEOUT`、`READY_NONCE_TIMEOUT`、`READY_PROMPT_TIMEOUT`、`LOGIN_REQUIRED`）：`_login_fsm_rx_tail_detail`（`session_manager.py:155-171`）＝ `clean_text(rx_tail())[-300:]`，空則 None。RX_FLOOD 反分類後的值不在集合中 → detail None。
- `BOOTLOADER_STUCK`：`"matched_prompt=<pattern>"`（`:1494`）。
- 其餘：None。
- 只在 probe 真的送出後精煉（boot-quiet 合成 PROMPT_UNAVAILABLE 不精煉，`:2671-2674`）。

### 5.6 一次性警告（WAL META／log）
- `CREDENTIALS_UNRESOLVED`：`_emit_credentials_unresolved_warning`（`:1197-1229`），`credentials_warned` 去重；WAL `direction="META"`、`source="system"`、`payload=b""`、`meta={"event":"credentials_unresolved","reason":<auth reason>,"env_file":<path or "">}`；reason ∈ `not_configured`/`ok`/`env_file_missing`/`env_file_unreadable`/`key_absent`（`auth.py:129-144`，進終態時不會是 ok/not_configured）。
- `TRANSPORT_STALL`：`transport_stall_warned` 去重、RX 恢復重臂（`_mark_session_rx` `:2325`）；WAL meta `{"event":"transport_stall_suspected","real_path":…,"rx_age_s":round(x,1)}`。

---

## 6. 相鄰名稱空間：self-test `classification`（`session_manager._self_test_impl` `:4305-4581`）
皆 `ok: true`：`SESSION_RECOVERING`（`recommended_action:"wait"`）、`HUMAN_INTERACTIVE_ACTIVE`（strict_human_lock，`"wait_or_detach_console"`）、`DEVICE_MISSING`（`"check_cable_or_bind"`）、`DEVICE_REBOUND_REQUIRED`（`"reattach"`）、`BRIDGE_DOWN`（bridge None→`"attach"`；未 running/serial_alive→`"recover"`）、`VTTY_STALE`（`"console_attach"`）、`PASSTHROUGH`（`"console_attach"` 或 fallback 且 RX 命中 template→`"pin_profile"`＋`suggested_profile`、`hint`）、`LOGIN_REQUIRED`（`"console_attach"`）、`REBOOTING`（`"wait_or_console_attach"`）、`BOOTLOADER`（`"recover_interactive"`＋`matched_prompt`、`rx_tail`）、`RX_FLOOD`（`"wait"`＋`rx_bytes_last_10s`）、`ATTACHED_NOT_READY`（`"console_attach"`）、`AUTOBOOT_QUIET`（`"wait"`）、`RELEASED`（`"device_attach"`/`"wait_external_flash"`＋`external_holder`、`reclaimable`）、`TARGET_UNRESPONSIVE`（`"recover"`）、`OK`（`"none"`）。唯一失敗：`SESSION_NOT_FOUND`（`ok:false`＋lease context）。外層一律注入 `command_capable`（`self_test` `:4288-4303`）。

---

## 7. 與 spec 的差異

### 7.1 spec §9.2 有、但程式碼中**不是 JSON error_code** 或不可達
| code | 實際狀況 |
|---|---|
| `DAEMON_ALREADY_RUNNING` | 只是 `RuntimeError` 訊息，daemon 印 stderr＋exit 2（`daemon.py:146-150`），從不進 JSON；也**不是具名常數**。§6.2「回 `DAEMON_ALREADY_RUNNING`」需界定為 PROC 介面。 |
| `FLUSHED_BY_RECOVERY` | 不是具名常數（參數預設字面）；只出現在命令紀錄 `command.error_code`（REC），從不是 top-level 回應碼。 |
| `FLASH_TARGET_IS_CONSOLE` | 只在 `flash_endpoint.resolve_flash_target`（`:266-295`），production 無呼叫端（僅 `tests/test_flash_guard.py`）→ 目前不可達。 |
| `UNKNOWN_EVENT_CMD` | argparse `required=True`（`cli.py:2305`）→ 實務不可達。 |
| `PROMPT_TIMEOUT_RECOVERED`、`PROMPT_TIMEOUT_FORCE_RECOVERED` | 以 `ok:true` 回應攜帶，非失敗；前者在命令紀錄中被丟棄（僅 capture 保留）。 |
| `TIMEOUT`、`SOCKET_ERROR`、`EMPTY_RESPONSE`、`INVALID_RESPONSE`、`INVALID_ENDPOINT` | 純 CLI-client，daemon 從不發出（`INVALID_ENDPOINT` 另有 CLI remote 路徑）。 |
| `INVALID_JSON`/`INVALID_REQUEST`/`INVALID_METHOD`/`EXCEPTION` | framing 層，無 `version`；前兩者無 `id`、非緊湊序列化。 |
| `DEVICE_STILL_HELD` | 目前只有 `pids`；spec §6.2／BC-04 的 `holder_unknown` 欄位**尚未實作**。 |

### 7.2 程式碼有、spec §9.2 未列（可出現在 JSON `error_code`）
直接發出（16）：`INVALID_COMPLETION_REGEX`（具名常數，RPC+CLI-local+REC）、`SEND_FAILED`（REC）、`COMMAND_FAILED`（REC fallback）、`NOT_READY`（RPC session.attach）、`TRANSFER_PROBE_FAILED`、`TARGET_DECODER_MISSING`、`TARGET_ENCODER_MISSING`（RPC file.*）、`BENCH_ENDPOINT_NOT_REMEMBERED`、`UNKNOWN_BENCH_CODE`、`INVALID_BENCH_CONFIG`、`TUNNEL_STATUS_ERROR`、`TUNNEL_CONFLICT`、`INVALID_TARGET`、`TUNNEL_SPAWN_FAILED`、`REMOTE_BIND_UNVERIFIED`、`SSH_NOT_FOUND`（CLI-local）。
經 last_error 提升（§5.4，23 個具名＋動態）：`PROMPT_UNAVAILABLE`、`LOGIN_REQUIRED`、`USER_ENV_MISSING`、`PASS_ENV_REQUIRED`、`PASS_ENV_MISSING`、`LOGIN_USER_REQUIRED`、`LOGIN_PROMPT_TIMEOUT`、`BCM_PROMPT_TIMEOUT`、`SHELL_PROMPT_TIMEOUT`、`PRPL_PROMPT_TIMEOUT`、`POST_LOGIN_CMD_TIMEOUT`、`READY_NONCE_TIMEOUT`、`READY_PROMPT_TIMEOUT`、`CONSOLE_UNLOCK_IDLE_TIMEOUT`、`CONSOLE_UNLOCK_BOOTLOADER`、`CONSOLE_UNLOCK_PROMPT_TIMEOUT`、`CREDENTIALS_UNRESOLVED`、`RX_FLOOD`、`TRANSPORT_STALL`、`BOOTLOADER_STUCK`、`RECOVERY_TIMEOUT`、`RECOVERY_ERROR`、`REBOOTING`，以及 `REPROBE_FAILED:<Exc>`。
PROC（非 JSON）：`SOCKET_UNAVAILABLE`。
非 error_code 但 spec 應知：`CMD_LENGTH_WARNING`（warning.code）、`error_detail:"NOT_BOOTLOADER"`、`EXCEPTION` 包裝的 `INVALID_INTERACTIVE_ENCODING`。

### 7.3 語意差異（spec 敘述 vs 實作）
1. **`CREDENTIALS_UNRESOLVED` 命名空間**：spec §9.3／§15（行 1313）規定「不得新增為 RPC／CLI `error_code`」，但現行 Python 已經會把它當 `error_code` 回出：`session.attach`（ok:false，`_attach_state_result`）與 `session.recover` REPROBE（ok:true payload）。Rust 1:1 會繼承此行為；需裁決是否視為既有契約（凍結）或為違規（BC 變更）。同理 `RX_FLOOD` 也經此路徑成為 error_code，spec 只把它列為 classification。
2. **`RECOVERING`＋CmdSubmit**：spec §5.3（行 584）稱回 `SESSION_RECOVERING`，實作 `command.submit` 先在 `_resolve_session_id` 以 state≠READY 回 **`SESSION_NOT_READY`**（`service.py:642`）；`interactive_open` 同樣回 `SESSION_NOT_READY`。`SESSION_RECOVERING` 只在 `session.attach`（state RECOVERING 且有 bridge）出現；execute/file 內的 `session.recovering` 檢查（`:3659,4888,4969`）因 `recovering=True` 只與 state=RECOVERING 同時設定（`:3607-3609`）而實務不可達。
3. **`FLUSHED_BY_RECOVERY` 的觸發面**：spec 敘述為 Recovery 清除未執行命令；實作上**任何** `_on_detached`（含 `_transition_to_attached`、release、clear、bind、bridge-down、reboot）都會以此碼終結 accepted 命令；daemon 停止則用 `FLUSHED_BY_SHUTDOWN`。
4. **`FORCE_RECOVER_TIMEOUT` 適用面**：只在 `session.recover force=true` 路徑回出；READY 分支的自動 force fallback（`:3916-3925`）失敗時丟棄 FORCE_RECOVER_TIMEOUT，改回 `PROMPT_TIMEOUT`。
5. **retryable 標記**：spec 要求每 entry 有 `retryable`；程式中唯一機器可讀的重試依據是 client `_RETRYABLE_TRANSIENT_ERROR_CODES`（僅 3 碼且限 11 方法）與 `AUTOBOOT_QUIET.retry_after_s`；`READY_UNCONFIRMED` 以 `recommended_action` 表示不可重試。其餘均需在契約中人工標註（本表「可重試」欄為推論）。
6. **計數**：spec 宣稱「71 字面＋6 常數」，經 AST 驗證 77 個字串在 `sw_core` 中都存在；但若以「會出現在 JSON `error_code`」為準，有效集合應為 77 − {`DAEMON_ALREADY_RUNNING`} − {不可達 `FLASH_TARGET_IS_CONSOLE`、`UNKNOWN_EVENT_CMD`} ＋ §7.2 的 16 直接碼 ＋ 23 提升碼。spec §9.2 也要求「掃描須包含常數傳遞與回應建構路徑」——last_error 提升路徑即此類，目前未被盤點進表一。
7. **`INVALID_RULE_SCHEMA`** 以 `error` 鍵帶訊息（非 `message`）；`LOG_OPEN_FAILED` 以 `detail` 帶訊息——形狀不一致需凍結。
8. **`AUTOBOOT_QUIET`／`READY_UNCONFIRMED` 雙形狀**：service 層（submit 前，帶 `session`、`retry_after_s`/`recommended_action`、`hint`）與 manager 層（execute 時，`AUTOBOOT_QUIET` 只帶 `selector`；`READY_UNCONFIRMED` 帶 `selector`+`hint`+`recommended_action`）回應欄位不同。
9. **BC-03 `COMMAND_ABORTED_IN_FLIGHT`**：程式碼中不存在（符合 spec「實作前尚未生效」）。現行 `PROMPT_TIMEOUT`（已 TX 後逾時）與 `SEND_FAILED`（結果不明）承載了「已送出/不明」語意，Stage 0 映射需處理。

### 7.4 dead code／不可達
- `SessionManager._wait_for_human_interactive_release`（`:3363-3391`，回 `SESSION_NOT_READY`/`SESSION_RECOVERING`/`SESSION_INTERACTIVE_BUSY` tuple）：無呼叫端。
- `flash_endpoint.resolve_flash_target`：無 production 呼叫端。
- `util.shell_command_incomplete_reason`：無 production 呼叫端。
- `cli.py:2717` main fallthrough、`cli.py:1064` UNKNOWN_EVENT_CMD：argparse 已攔截。

### 7.5 其他觀察（非 spec 差異，但影響 1:1 移植）
- `login_fsm._maybe_login`（`:206-230`）：`_resolve_login_user` 回傳的 `USER_ENV_MISSING` 被同名變數 `err` 接住後在 `_wait_or_fail` 前未檢查；若 `user` 為 None 但 `pass_env` 有值，流程會等 login prompt，拿到後回 `LOGIN_USER_REQUIRED`，`USER_ENV_MISSING` 不會被回出。`USER_ENV_MISSING` 實務上只在 `needs_login` 為 False 時被丟棄——即**此值目前無法到達 last_error**（雖列在 `LOGIN_FSM_DETAIL_ERRORS`）。
- `client._rpc_call_once` 收到非 JSON 一行 → `JSONDecodeError` 未捕捉（§1.2）。
- `unassign_alias` 失敗仍寫 state；`detach_console` 失敗仍關 lease。
- `DAEMON_ALREADY_RUNNING_ELSEWHERE` 不寫 stderr 行（其他 daemon start 失敗皆 `_mirror_err`）。
- `daemon start` 的 `REMOTE_NOT_SUPPORTED` exit 2，`connect/benches/remote` 的同碼 exit 1。
- `profile test` 的 `UNKNOWN_PROFILE`／`SAMPLE_READ_FAILED` exit 0（純診斷，docstring 明示）。

### 7.6 文件覆蓋（OD-06 相關，`sw_core/assets/skill/SKILL.md`／`README.md`／`docs/`，排除 rust-refactor）
未在 SKILL/README/docs 任何處出現：`SEND_FAILED`、`COMMAND_FAILED`、`BENCH_ENDPOINT_NOT_REMEMBERED`、`UNKNOWN_BENCH_CODE`、`INVALID_BENCH_CONFIG`、`TUNNEL_STATUS_ERROR`、`RECOVERY_TIMEOUT`、`RECOVERY_ERROR`、`INVALID_INTERACTIVE_ENCODING`、`UNKNOWN_EVENT_CMD`。SKILL.md 未提但 README/docs 有：`INVALID_COMPLETION_REGEX`、`TARGET_DECODER_MISSING`、`TARGET_ENCODER_MISSING`、`TUNNEL_CONFLICT`、`LOGIN_REQUIRED`、`REBOOTING`、`INVALID_TARGET`、`TUNNEL_SPAWN_FAILED`、`SSH_NOT_FOUND`、`TRANSFER_PROBE_FAILED`（僅 docs）。
