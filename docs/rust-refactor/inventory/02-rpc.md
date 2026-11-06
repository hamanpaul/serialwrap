# 02 — RPC 表面盤點（serialwrap Python → Rust 1:1）

- 來源樹：`~/prj_pri/serialwrap-worktrees/rust-refactor`，HEAD `93553e6`，`VERSION` = `0.3.2`
- 盤點檔案：`sw_core/service.py`（分派器）、`sw_core/rpc_posix.py`、`sw_core/rpc_win.py`、`sw_core/rpc.py`（shim）、`sw_core/daemon.py`、`sw_core/platform_backends.py`；回應形狀必須追進 `sw_core/session_manager.py`、`sw_core/arbiter.py`、`sw_core/wal.py`、`sw_core/file_transfer.py`、`sw_core/event_engine/*`、`sw_core/uart_io.py`、`sw_core/lock_posix.py`、`sw_core/lock_win.py`、`sw_core/multi_open.py`、`sw_core/command_completion.py`、`sw_core/version.py`、`sw_core/constants.py`、`sw_core/client.py`（client 端 framing 對照）。
- 行為驗證：§1 的 framing 行為已用 throwaway `JsonRpcUnixServer`（相對路徑 socket、stub handler，未碰 repo／prod daemon）在 Python 3.12.3 實跑確認，標「〔實測〕」。
- 本檔只讀不改 repo。行號均為上述 HEAD。

---

## 0. 摘要數字

| 項目 | 值 |
|---|---|
| `SerialwrapService._dispatch()` 處理的 method 數 | **53**（`service.py:705-1174`） |
| daemon 層額外 method | **1**：`daemon.stop`（`daemon.py:167`，不經 service） |
| 合計 RPC method | **54** |
| 未知 method | `METHOD_NOT_FOUND`（`service.py:1174`） |
| executor offload（阻塞）method | 12 個（`daemon.py:20-35` `BLOCKING_RPC_METHODS`） |
| client 自動重試白名單 | 11 個（`client.py:23-35` `RETRYABLE_READONLY_METHODS`） |
| 真正的 streaming RPC | **無**（見 §10 D-03） |

---

## 1. Wire protocol（`rpc_posix.py:11-70` `serve_connection`，POSIX/TCP 共用）

### 1.1 Framing

- **NDJSON，一行一請求／一行一回應**，分隔字元 `\n`（0x0A）。
- 讀取：`await reader.readline()`（asyncio `StreamReader`，預設 `limit = 2**16 = 65536` bytes；`asyncio.start_unix_server`/`start_server` 未傳 `limit`）。
- 每行先 `line.strip()`（去頭尾空白，含 `\r`、`\t`、空格）；**空行忽略、不回應**（`rpc_posix.py:23-25`）。〔實測：`\n\n{...}\r\n` → 只回一行〕
- 解碼：`line.decode("utf-8", errors="replace")` 後 `json.loads`。非法 UTF-8 bytes 變 U+FFFD，通常導致 `INVALID_JSON`。〔實測：`\xff\xfe{"id":1}` → `INVALID_JSON`〕
- Python `json.loads` 接受 `NaN`、`Infinity`、`-Infinity` 字面；重複 key 取最後一個。〔實測：params `{"x":NaN}` 被接受並原樣回送 `NaN`（非法 JSON）〕
- **單連線可多請求**：迴圈讀取直到 EOF（`readline()` 回空 bytes → break）。同一連線上的請求**序列處理、回應依序寫出**；pipelining 允許。〔實測：一次送兩行 → 兩行回應依序〕
- 連線結束：`finally: writer.close(); await writer.wait_closed()`。

### 1.2 單行長度上限（隱性）

- 請求行內容（`\n` 之前）超過 **65536 bytes** → `readline()` 拋 `ValueError`（源自 `LimitOverrunError`："Separator is found, but chunk is longer than limit" 或 "...not found, and chunk exceed the limit"）。`serve_connection` **不攔截**此例外（只有 try/finally），→ 連線直接被關閉、**不回任何 bytes**，asyncio 記 "Unhandled exception in client_connected_cb"。client 端看到 EOF → `EMPTY_RESPONSE`。〔實測：70000 字元 params → `<EOF>`〕
- 回應行**無上限**（無截斷）。

### 1.3 請求物件

```json
{"id": <any JSON>, "method": "<str>", "params": {<object>}}
```

| 欄位 | 處理 |
|---|---|
| 整行非 JSON | 回 `{"ok": false, "error_code": "INVALID_JSON"}` 並**繼續**讀下一行 |
| JSON 但非 object（如 array、數字） | 回 `{"ok": false, "error_code": "INVALID_REQUEST"}`，繼續 |
| `id` | `obj.get("id")`，任意 JSON 型別原樣回送；缺省 → `null` |
| `method` 非 `str`（含缺省） | 回 `{"id": <id>, "ok": false, "error_code": "INVALID_METHOD"}` |
| `params` 非 object（缺省、array、null、字串…） | **靜默改為 `{}`**（`rpc_posix.py:49-50`），不報錯〔實測：`"params":[1]` → handler 收到 `{}`〕 |
| 其他頂層 key（如 `jsonrpc`） | 忽略 |

### 1.4 回應物件與序列化

- 正常路徑：`resp = {"id": req_id}`；`result` 為 dict → `resp.update(result)`；非 dict → `resp.update({"ok": True, "data": result})`（實務上 54 個 method 全回 dict，`data` 分支不會觸發）。
- **key 順序**：`id` 第一、接著 handler dict 的插入順序，最後（若經 `service.rpc`）`version`（`setdefault`，插在末尾）。
- 正常路徑序列化：`json.dumps(resp, ensure_ascii=False, separators=(",", ":")) + "\n"`，UTF-8 編碼；**不 sort_keys**；`allow_nan` 預設 True（float NaN/Inf 會輸出 `NaN`/`Infinity` 非法 JSON 字面）。
- **`INVALID_JSON` / `INVALID_REQUEST` 兩個早退回應用不同序列化**：`json.dumps({...}, ensure_ascii=False)`（**預設分隔字元 `", "` 與 `": "`，含空格**）、**無 `id` 欄位**、無 `version`。〔實測位元組：`{"ok": false, "error_code": "INVALID_JSON"}\n`、`{"ok": false, "error_code": "INVALID_REQUEST"}\n`〕
- `INVALID_METHOD`：緊湊序列化 `{"id":7,"ok":false,"error_code":"INVALID_METHOD"}`，**無 `version`**（未經 `service.rpc`）。
- 例外：handler（含 executor 內）拋任何 `Exception` → `{"ok": false, "error_code": "EXCEPTION", "message": str(exc)}`（`rpc_posix.py:58-59`）。因例外在 `service.rpc()` 補 version 之前就穿出，**`EXCEPTION` 回應沒有 `version`**。〔實測：`{"id":1,"ok":false,"error_code":"EXCEPTION","message":"kaboom"}`〕
- 每次回應後 `await writer.drain()`。

### 1.5 分派執行模型

- 單執行緒 asyncio event loop。method ∈ `blocking_methods` → `loop.run_in_executor(None, handler, method, params)`（loop 的預設 `ThreadPoolExecutor`，Python 3.12 預設 `max_workers = min(32, os.cpu_count() + 4)`）；其餘 method **直接在 event loop 執行緒同步呼叫**（會阻塞全 daemon 所有連線）。
- 連線之間並行（每連線一個 task），同連線內序列。
- 無請求逾時、無取消（client 斷線不會中止 handler）、無認證、無大小以外的速率限制。

### 1.6 Client 端對照（`client.py:226-278` `_rpc_call_once`，供 Rust CLI 對齊）

- 請求：`{"id": req_id, "method": method, "params": params}`，`json.dumps(..., ensure_ascii=False, separators=(",", ":")) + "\n"`；`req_id` 預設 `1`（健康探測用 `0`）。
- 讀：`recv(65536)` 累積直到出現 `\n` 或 EOF；只取**第一行**、`strip()`；空 → `EMPTY_RESPONSE`；非 dict → `INVALID_RESPONSE`；`socket.timeout` → `TIMEOUT`；`OSError` → `SOCKET_ERROR` + `message`；endpoint 解析失敗 → `INVALID_ENDPOINT` + `message`；無 AF_UNIX 平台連 unix endpoint → `SOCKET_ERROR`（特定訊息）。每次呼叫一條新連線。
- endpoint 格式：`tcp://host:port`（不得有 path/query/fragment）、`unix:///abs/path`（不得有 netloc）、或裸路徑（視為 unix）。
- 重試：只對 §0 的 11 個 method，且僅在 `TIMEOUT`/`SOCKET_ERROR`/`EMPTY_RESPONSE`；退避 0.5s 起 ×2、單次上限 5.0s。TIMEOUT（非 `health.ping`/`health.status`）後 enrich `daemon_reachable`（bool）與可選 `daemon_busy: {"commands": int, "sessions": int}`；探測 timeout 1.0s、總 deadline 2.5s。

---

## 2. RPC server transports

### 2.1 後端選擇（`platform_backends.py`）

`_select(env_name, backend)`（`platform_backends.py:7-34`）：
- `mode = (backend or os.environ.get(env_name) or "auto").lower()`
- `"posix" | "unix" | "termios"` → `"posix"`；`"win" | "windows" | "win32"` → `"win"`；`"auto"` → `os.name == "nt" or sys.platform.startswith("win")` 時 `"win"`，否則 `"posix"`；其他值 → `raise ValueError(f"unsupported backend: {mode!r}")`。
- 注意：env 值為空字串 → `or "auto"` → auto；env 值前後空白**不 strip**（`" posix"` → ValueError）。
- 三個入口：`select_rpc_backend(backend=None)` 讀 `SERIALWRAP_RPC_BACKEND`；`select_lock_backend` 讀 `SERIALWRAP_LOCK_BACKEND`；`select_device_backend` 讀 `SERIALWRAP_DEVICE_BACKEND`。
- daemon 內 `select_*` 拋 ValueError 不被攔截 → daemon traceback 退出（exit 1）；CLI 端 `_rpc_backend_is_win()` 會吞 ValueError 退回平台判斷（`cli.py` 約 660 行）。

### 2.2 POSIX：`JsonRpcUnixServer`（`rpc_posix.py:73-129`）

- 建構：`(socket_path: str, handler: Callable[[str, dict], dict], *, blocking_methods: set[str] | None = None)`。
- `start()`：若 `os.path.exists(socket_path)` → `os.unlink`（無條件刪除既有檔；前置 lock 已做存活探測，見 §4）；`asyncio.start_unix_server(self._handle_client, path=socket_path)`；然後 `_secure_socket()`。
- `_secure_socket()`：`os.chmod(socket_path, 0o660)`（失敗吞掉）；`SERIALWRAP_SOCKET_GROUP` 非空（strip 後）→ `shutil.chown(socket_path, group=<strip 值>)`（`OSError`/`LookupError` 吞掉）。bind 與 chmod 之間存在以 umask 權限存在的短暫窗口。
- `serve_forever()`：daemon **未使用**（`asyncio.start_unix_server` 預設 `start_serving=True`，`start()` 後即接受連線）。
- `stop()`：`server.close(); await server.wait_closed()`；`_server = None`；若 socket 檔存在 → unlink。
- `sw_core/rpc.py` 是 re-export shim（`from sw_core.rpc_posix import JsonRpcUnixServer`）。

### 2.3 Windows：`TcpRpcServer`（`rpc_win.py:11-80`）

- 建構：`(endpoint: str, handler, *, blocking_methods=None)`；建構時 `_parse_tcp(endpoint)`。
- `_parse_tcp`：`urlsplit`；`scheme != "tcp"` 或無 hostname 或 `port is None` → `ValueError(f"invalid tcp endpoint: {endpoint!r}")`；`parsed.port` 非法時 urllib 自拋 ValueError；hostname（urlsplit 會小寫化）不在 `LOOPBACK_TCP_HOSTS = frozenset({"127.0.0.1", "localhost", "::1"})`（`constants.py:49`）→ `ValueError("RPC server 僅允許 loopback 位址（127.0.0.1/localhost/::1），拒絕 {host!r} 避免遠端暴露（RPC 無認證，非 loopback 會讓遠端呼叫所有命令）")`。IPv6 需寫成 `tcp://[::1]:port`。
- `start()`：`asyncio.start_server(handler, host=host, port=port)`（`localhost` 可能同時綁 v4+v6）。無 socket 選項設定、無 ACL、無認證。
- `serve_forever()` 後 `_server=None`；`stop()` close + wait_closed。
- 預設 endpoint：`DEFAULT_TCP_PORT = int(os.environ.get("SERIALWRAP_TCP_PORT", "48700"))`；Windows `DEFAULT_ENDPOINT = SERIALWRAP_ENDPOINT or f"tcp://127.0.0.1:{DEFAULT_TCP_PORT}"`；POSIX `DEFAULT_ENDPOINT = SERIALWRAP_ENDPOINT or SOCKET_PATH`（`constants.py:47-56`）。

### 2.4 路徑常數（`constants.py`）

| 常數 | 解析 |
|---|---|
| `CONFIG_DIR` | `SERIALWRAP_CONFIG_DIR` 或 `$XDG_CONFIG_HOME/serialwrap`（預設 `~/.config/serialwrap`） |
| `STATE_DIR` | `SERIALWRAP_STATE_DIR` 或 `$XDG_STATE_HOME/serialwrap`（預設 `~/.local/state/serialwrap`） |
| `RUN_DIR` | `SERIALWRAP_RUN_DIR`；否則：若 `SERIALWRAP_STATE_DIR` 非空 → `STATE_DIR`；否則 `$XDG_RUNTIME_DIR/serialwrap`；否則 `STATE_DIR/run` |
| `LOCK_PATH` | `RUN_DIR/serialwrapd.lock` |
| `SOCKET_PATH` | `RUN_DIR/serialwrapd.sock` |
| `TTYMCU_PATH` | `SERIALWRAP_TTYMCU_PATH` 或 `RUN_DIR/dev/ttyMCU` |
| `STATE_PATH` | `STATE_DIR/state.json` |
| `WAL_DIR` | `SERIALWRAP_WAL_DIR` 或 `STATE_DIR/wal` |
| `PROFILE_DIR` | `SERIALWRAP_PROFILE_DIR` 或 `CONFIG_DIR/profiles` |
| `DEVICE_BY_ID_DIR` / `DEVICE_BY_PATH_DIR` | `SERIALWRAP_BY_ID_DIR`（`/dev/serial/by-id`）/ `SERIALWRAP_BY_PATH_DIR`（`/dev/serial/by-path`） |
| `LOG_DIR` | `SERIALWRAP_LOG_DIR` 或 `~/b-log` |
| `EVENTS_DIR` | `SERIALWRAP_EVENTS_DIR` 或 `~/.serialwrap/events.d` |
| `EVENTS_RUNTIME_DIR` | `SERIALWRAP_EVENTS_RUNTIME_DIR` 或 `STATE_DIR/events` |
| `EVENTS_LOG_PATH` | `SERIALWRAP_EVENTS_LOG_PATH` 或 `EVENTS_RUNTIME_DIR/events.ndjson` |

`_env_path(name, default)`：env 未設或 strip 後空 → default；結果 `os.path.expanduser`。注意 `_xdg()` 用 `os.environ.get(name) or ...`（空字串視為未設、不 strip）。所有常數在 **import 時**計算一次。

---

## 3. Daemon 程序（`daemon.py`）

### 3.1 命令列（`build_parser`，`daemon.py:38-44`）

`prog="serialwrapd"`，`description="serialwrap daemon"`：

| 參數 | 預設 |
|---|---|
| `--profile-dir` | `PROFILE_DIR` |
| `--socket` | `DEFAULT_ENDPOINT`（POSIX = socket 路徑；Windows = `tcp://127.0.0.1:48700`） |
| `--lock` | `LOCK_PATH` |

入口：`main(argv=None) -> int`：`force_utf8_stdio()` → parse → `int(asyncio.run(_run_async(args)))`；`__main__` 時 `sys.exit(main())`。console_script `serialwrapd`；`serialwrapd.py` 薄 shim；CLI 也以 `[sys.executable, "-m", "sw_core.daemon"]` 或同目錄 `serialwrapd.exe` 啟動（`cli.py:207-215`）。

### 3.2 啟動序列（`_run_async`，`daemon.py:128-198`）

1. `ensure_runtime_dirs()`：`makedirs(exist_ok)` `CONFIG_DIR`、`STATE_DIR`、`RUN_DIR`、`WAL_DIR`、`EVENTS_DIR`、`EVENTS_RUNTIME_DIR`（`constants.py:206-212`）。失敗 → traceback exit 1。
2. `result = load_profiles(args.profile_dir)`（回 `LoadResult(profiles, templates, max_sessions)`）；`profiles` 與 `templates` 皆空 → stderr `"serialwrapd: no profiles loaded\n"`（續跑）。YAML 例外不攔 → exit 1。
3. `select_device_backend() == "win"` → `_make_windows_passthrough_templates(templates)`：若無 `platform == "passthrough"` 的 template，追加 `ProfileTemplate(profile_name="windows-default-passthrough", platform="passthrough", ready_probe="")`。POSIX 不注入。
4. `lock = _build_lock(args)`：`select_lock_backend() == "win"` → `WindowsSingletonLock(args.lock, args.socket)`，否則 `SingletonLock(args.lock, args.socket)`。`lock.acquire()`；`RuntimeError` → stderr `f"serialwrapd: {exc}\n"`，**return 2**（訊息為 `DAEMON_ALREADY_RUNNING` 或 `SOCKET_UNAVAILABLE`）。其他例外（如 makedirs/open 失敗的 OSError）不攔 → exit 1。
5. `service = SerialwrapService(result.profiles, templates=templates, max_sessions=result.max_sessions)`；`StateLoadError` → stderr `f"serialwrapd: {exc}\n"`、`lock.release()`、**return 3**。（建構期間：`WalWriter()` makedirs WAL_DIR 並**全檔掃描** `raw.wal.ndjson` 取最大 seq；`resolve_version()` 一次；SessionManager 載入 state.json；EventEngine/DeviceWatcher/FlashEndpoint 建構但未啟動。）
6. `stop_event = asyncio.Event()`；`_handle(method, params)`：`method == "daemon.stop"` → `stop_event.set()` 並回 `{"ok": True, "stopping": True}`；否則 `service.rpc(method, params)`。
7. `server = _build_server(args, _handle)`：`select_rpc_backend() == "win"` → `TcpRpcServer(args.socket, handler, blocking_methods=BLOCKING_RPC_METHODS)`，否則 `JsonRpcUnixServer(...)`。**此步在 try/finally 之外**：Windows endpoint 解析 ValueError → 例外穿出（lock 未顯式 release，靠程序結束釋放）→ exit 1。
8. 訊號：`for sig in (SIGINT, SIGTERM): loop.add_signal_handler(sig, _stop)`（`_stop` = `stop_event.set()`）；`NotImplementedError`（Windows）吞掉 → Windows 無 handler（Ctrl+C 走 `KeyboardInterrupt`／asyncio.run 取消主 task 路徑，未另行處理）。SIGHUP 等其他訊號未處理（預設行為）；重複 SIGTERM 無額外效果。
9. `try:` `service.start()` → `await server.start()` → `_write_effective_endpoint(args)` → `await stop_event.wait()`；`finally:` `await server.stop()` → `service.stop()` → `lock.release()`。
10. 正常結束 **return 0**。

`service.start()`（`service.py:502-518`，冪等，以 `_running` 旗標防重入）：設 `_running=True`、`_started_at=now_iso()` → `engine.start()` → `add_rx_observer(_engine_rx_observer)` → `watcher.start()` → `watcher.poll_once()` → `prepare_dynamic_rank(list(watcher.devices.keys()))` → `update_devices(watcher.devices)` → `bootstrap_attach()` → `flash_endpoint.start()`。（`_flash_rx_observer` 在 `__init__` 時已加入。）**注意：server 在 service.start() 之後才綁定**——attach 執行緒已在跑，但 RPC 尚不可連。

`service.stop()`（`service.py:520-536`）：`_running=False` → `engine.stop()` → `watcher.stop()` → 對每個 session `arbiter.unregister_session(sid, error_code="FLUSHED_BY_SHUTDOWN")` → `flash_endpoint.stop()`（OSError 吞）。**不關閉 UARTBridge**（UART fd 隨程序結束關閉）、不寫 state.json。

`_write_effective_endpoint(args)` → `_write_config_endpoint(args.socket)`（`daemon.py:76-100`，#173 起全平台）：`RuntimeConfig(os.path.join(CONFIG_DIR, "config.yaml")).set_socket(endpoint)`：讀既有 YAML（非 dict 視為 `{}`）、設 `socket_path = endpoint`（**原字串，未正規化／未絕對化**）、`mkdir(parents)`、`write_text(yaml.safe_dump(data, allow_unicode=True))`（**非原子**、重排 key、丟註解）。任何例外 → stderr `f"serialwrapd: 寫入 config.yaml endpoint 失敗（非致命）: {exc}\n"`，續跑。

`daemon.stop` 時序：handler 回應在同一 task 內同步 `writer.write`，主 task 被喚醒前回應已進 transport buffer，故 client 會收到 `{"id":..,"ok":true,"stopping":true}`（無 `version`）。

### 3.3 Exit code 一覽

| code | 條件 |
|---|---|
| 0 | 經 `daemon.stop` / SIGINT / SIGTERM 正常關閉 |
| 1 | 未攔截例外（profile YAML、makedirs、backend env 非法、Windows endpoint 非法、lock 的 OSError…） |
| 2 | `lock.acquire()` 拋 `RuntimeError`（`DAEMON_ALREADY_RUNNING` / `SOCKET_UNAVAILABLE`） |
| 3 | `StateLoadError`（state.json 讀取失敗、非 JSON 損毀；fail-closed） |

### 3.4 `BLOCKING_RPC_METHODS`（`daemon.py:20-35`，12 個）

`file.push`、`file.pull`、`session.recover`、`session.attach`、`session.self_test`、`session.console_attach`、`command.result_tail`、`result.tail`、`log.tail_raw`、`log.tail_text`、`wal.range`、`health.status`。

**不在清單但會阻塞 event loop 的 method**（Rust 版須決定是否修正）：`session.clear`（同步 `_save_state` fsync）、`device.release`／`device.attach`（`attach_device` 非 force 時同步掃整個 `/proc`）、`session.bind`、`session.pin/unpin`、`alias.*`（fsync）、`wal.reset`（rename+fsync dir）、`event.rule_set/delete/reload/tail`（檔案 I/O、`tail` 讀整個 events.ndjson）、`session.interactive_open`（ATTACHED 分支讀 rx tail；含 `command` 時送 UART）、`session.log_start`（open 檔）、`mcu.status`、`session.recover` 的 `_force_recover`（在 executor，OK）。

---

## 4. Singleton lock（Tier 1 實例鎖）

### 4.1 POSIX `SingletonLock(lock_path, socket_path)`（`lock_posix.py`；`daemon_lock.py` 為 re-export shim）

`acquire()`：
1. `os.makedirs(dirname(lock_path), exist_ok=True)`
2. `fd = os.open(lock_path, O_RDWR | O_CREAT, 0o600)`（Python 預設 non-inheritable = CLOEXEC）
3. `fcntl.flock(fd, LOCK_EX | LOCK_NB)`；`BlockingIOError` → close fd → `RuntimeError("DAEMON_ALREADY_RUNNING")`
4. 若 `os.path.exists(socket_path)`：建 `AF_UNIX` socket、`settimeout(0.2)`、`connect(socket_path)`：
   - 連上 → close fd → `RuntimeError("DAEMON_ALREADY_RUNNING")`
   - `OSError` 且 `errno in (ENOENT, ECONNREFUSED)` → `os.unlink(socket_path)`（失敗吞）→ 繼續
   - 其他 OSError（含 timeout） → close fd → `RuntimeError("SOCKET_UNAVAILABLE")`
5. `ftruncate(fd, 0)`；寫 `f"{os.getpid()}\n"`（ASCII）；`fsync`。
`release()`：`flock(LOCK_UN)` 後 `close`；未取鎖 no-op。**lock 檔永不刪除**。路徑**未正規化**（spec §6.2 要求「路徑須正規化」，見 D-07）。

### 4.2 Windows `WindowsSingletonLock(lock_path, endpoint)`（`lock_win.py`）

`acquire()`（**順序與 POSIX 相反：先探 endpoint 再取檔鎖**）：
1. `_endpoint_alive(endpoint)`：`urlsplit`；非 `tcp` 或缺 host/port → False；`socket.create_connection((host, port), timeout=0.2)` 成功 → True。True → `RuntimeError("DAEMON_ALREADY_RUNNING")`。
2. `makedirs(parent)`（parent 非空時）；`os.open(lock_path, O_RDWR|O_CREAT, 0o600)`。
3. `msvcrt.locking(fd, LK_NBLCK, 1)`（鎖 offset 0 的 1 byte）；`OSError` → close → `RuntimeError("DAEMON_ALREADY_RUNNING")`。
4. `ftruncate(0)`、寫 pid+`\n`、`fsync`。
`release()`：`lseek(0)` → `msvcrt.locking(fd, LK_UNLCK, 1)` → close。

---

## 5. 分派器慣例（`service.py:690-705`）

### 5.1 `rpc(method, params)` 包裝（#154）

```python
result = self._dispatch(method, params)
version = getattr(self, "_version", None)
if isinstance(result, dict) and version is not None:
    result.setdefault("version", version)
return result
```

- **所有經 service 的回應（含 `ok:false`、`METHOD_NOT_FOUND`）都帶 `version: str`**，位置在 dict 末尾；`health.status` 已在 `health()` 內顯式放 `version`（位於 `pid` 之後），setdefault 為 no-op。
- 不帶 `version` 的回應：`INVALID_JSON`、`INVALID_REQUEST`、`INVALID_METHOD`、`EXCEPTION`、`daemon.stop`。
- `self._version = resolve_version()`（建構時算一次，`version.py`）：依序 repo `../VERSION`（相對 `sw_core/`，strip）→ `importlib.metadata.version("serialwrap")` → 內嵌 `assets/VERSION`（strip）→ `"unknown"`。

### 5.2 參數強制轉型模式（跨 method 共通，**Rust 必須逐一複製**）

| 寫法 | 語意 |
|---|---|
| `str(params.get(k) or "")` | 缺省、`null`、`""`、`0`、`false`、`[]`、`{}` → `""`；其他型別 → `str()`（如數字 `5` → `"5"`、`true` → `"True"`、dict → Python repr） |
| `float(params.get(k) or D)` | **falsy（含 `0`、`0.0`、`""`、`false`）→ 預設 D**；`"abc"` → `ValueError` → `EXCEPTION`；`"nan"`/`"inf"` 接受；`true` → 1.0 |
| `int(params.get(k) or D)` | 同上；`0` → D（例如 `limit: 0` → 200、`priority: 0` → 10）；float `3.9` → 3（截斷）；`"3.5"` → ValueError → `EXCEPTION`；`true` → 1 |
| `bool(params.get(k))` | Python truthiness：**字串 `"false"`/`"0"` 為 True**；用於 `device.attach.force`、`session.recover.force`、`session.interactive_open.allow_attached` |
| `_coerce_rpc_bool(v)`（`service.py:72-86`） | `None`→False；bool 原值；int→`!= 0`；str strip+lower ∈ `{"1","true","yes","on"}`→True、其餘字串一律 False；其他型別→`bool(v)`。**只用於 `session.self_test.strict_human_lock`** |
| 原樣 `params.get(k)` | 不轉型直接下傳（`device.release.reason`、`event.*` 的 selector/owner/rule_id/since_ts/n、`command.submit.completion_regex`、`file.pull.local_path` 判斷） |

轉型發生在 selector 驗證**之前**的 method（例如 `command.submit`、`session.self_test`、`session.recover`、`session.interactive_open`、`session.interactive_status`、`command.result_tail`）：非法數字字串即使 selector 為空也回 `EXCEPTION` 而非 `INVALID_ARGS`。

### 5.3 selector 參數別名

| 別名順序 | method |
|---|---|
| `selector` 僅此 | `device.release`、`device.attach`、`session.get_state`、`file.push`、`file.pull` |
| `selector → session_id → com → alias` | `session.activity`、`session.self_test`、`session.recover`、`session.clear`、`session.bind`、`session.pin`、`session.unpin`、`session.attach`、`session.console_attach`、`session.console_detach`、`session.console_list`、`session.interactive_open`、`session.log_start`、`session.log_stop`、`session.log_status` |
| `selector → com → alias`（**無 session_id**） | `command.submit` |
| `com → selector`（com 優先） | `result.tail`（legacy 分支）、`log.tail_raw`、`log.tail_text` |
| 原樣 `selector` | `event.com_enable/disable/status`、`event.rule_list`、`event.reset`、`event.tail` |

`SessionManager.get_session(selector)`（`session_manager.py:995-1002`）：先精確比對 session_id（dict key），再依 `_sessions` 插入順序找 `profile.com == selector or profile.alias == selector` 的第一個。session_id 格式 `f"{profile_name}:{com}"`（`session_manager.py:711,985`）。

### 5.4 `_resolve_session_id(selector, *, source="agent")`（`service.py:618-688`；`command.submit`、`file.push`、`file.pull` 使用）

依序：
1. `get_session_state(selector)` 非 ok → 原樣回（`{"ok":false,"error_code":"SESSION_NOT_FOUND","selector":...}`）。
2. `session.state != "READY"`：
   - `state == "ATTACHED"` 且 `command_capable` 為 False → `{"ok":false,"error_code":"PROFILE_NOT_COMMAND_CAPABLE","hint":<固定中文>,"session":<SessionPublic>}`
   - 否則 → `{"ok":false,"error_code":"SESSION_NOT_READY","session":<SessionPublic>}`
3. `source` **不以 `"human:"` 開頭**時（human 來源整段略過，任何 client 可自稱 `human:*` 繞過，見 H-05）：
   - `session.ready_reconfirm_failed` → `{"ok":false,"error_code":"READY_UNCONFIRMED","hint":f"READY 已逾 {300:.0f}s／5 次未能經 nonce probe 再確認（可能卡在 bootloader）；請 session self-test 取分類後 interactive-open 處理","recommended_action":"self_test","session":...}`（**不帶 `retry_after_s`**）
   - `session.boot_quiet_remaining_s is not None` → `{"ok":false,"error_code":"AUTOBOOT_QUIET","retry_after_s":<float, 1 位小數>,"hint":"boot quiet window 進行中（疑似板卡自發重開機）；等 session 重新確認 READY 後重送","session":...}`
   - `session.ready_reconfirm_pending` → `{"ok":false,"error_code":"AUTOBOOT_QUIET","retry_after_s":5.0,"hint":"boot quiet window 已結束但 READY 尚未重新確認；等 daemon 重新確認 READY 後重送","session":...}`
4. 通過 → 回 `session_id`。

---

## 6. 共用回應子結構

### 6.1 `SessionPublic`（`SessionRuntime.to_public_dict()`，`session_manager.py:538-638`）

key 依下列插入順序：

| key | 型別 | 說明 |
|---|---|---|
| `session_id` | str | `"<profile_name>:<COM>"` |
| `profile` | str | `profile.profile_name` |
| `com` | str | 如 `"COM0"` |
| `alias` | str \| null | |
| `act_no` | int | |
| `device_by_id` | str \| null | |
| `platform` | str | `shell`/`bcm`/`prpl`/`passthrough`… |
| `profile_source` | str | `unknown`/`pin`/`sticky`/`detected`/`fallback`/`yaml-target` |
| `command_capable` | bool | 由 profile `ready_probe` 決定 |
| `state` | str | `DETACHED`/`ATTACHING`/`ATTACHED`/`READY`/`RECOVERING`/`RELEASED`/`FLASHING` |
| `last_error` | str \| null | session 命名空間（見 §8.3） |
| `last_error_detail` | str \| null | |
| `detached_at` | str \| null | ISO8601 |
| `last_ready_at` | str \| null | |
| `vtty` | str \| null | `vtty_path`，無則取 `retained_consoles.primary_vtty()` |
| `attached_real_path` | str \| null | |
| `bridge_generation` | int | |
| `recovering` | bool | |
| `interactive_session_id` | str \| null | 32-hex |
| `foreground_busy` | bool | |
| `fg_cmd_expected_duration_s` | float \| null | |
| `console_count` | int | bridge 存在：`len(bridge.list_consoles())`（**副作用：prune stale consoles**）；否則 retained clients 數；否則 0 |
| `last_state_change_at` / `last_rx_at` / `last_tx_at` / `last_probe_at` | str \| null | ISO8601 |
| `reprobe_attempts` | int | |
| `next_reprobe_at` | float \| null | **monotonic 秒**（跨程序無意義） |
| `reprobe_exhausted` | bool | |
| `boot_quiet_remaining_s` | float \| null | `round(until - monotonic, 1)`；未啟用或已過 → null |
| `ready_reconfirm_pending` | bool | |
| `ready_reconfirm_failed` | bool | |
| `ready_reconfirm_remaining_s` | float \| null | `round(max(deadline-now,0),1)`；非 pending → null |
| `idle_for_ms` | int \| null | `max(last_rx_mono,last_tx_mono)` 為 0 → null |
| `last_rx_age_s` / `last_tx_age_s` | float \| null | 1 位小數 |
| `rx_bytes_last_10s` | int \| null | bridge `rx_stats()`；不支援 → null |
| `rx_rate_bps` | int \| null | `int(total / 10.0)` |
| `outstanding_commands` | int | `len(background_cmd_ids) + (1 if foreground_busy else 0)` |
| `activity_classification` | str | 非 READY/ATTACHED → `offline`；idle null → `newly-attached`；<5000ms `active`；<60000ms `idle-healthy`；否則 `quiet-suspicious` |
| `released_by` / `released_at` / `released_reason` | str \| null（`released_reason` 可為任意 JSON，見 `device.release`） | |
| `capture` | object \| null | `{"capture_id": str(uuid4 含 -), "log_path": str, "status": str, "line_count": int, "byte_count": int}` |
| `console_endpoint` | str \| null | **僅無 PTY 平台（Windows）或 bridge 有 endpoint 時才出現此 key**；POSIX 正常不出現 |

ISO8601 格式：`datetime.now(tz=utc).isoformat()` → `2026-10-15T01:02:03.456789+00:00`；**微秒為 0 時省略小數部分**（`2026-10-15T01:02:03+00:00`）。

### 6.2 `LeaseContext`（`_lease_context`，`session_manager.py:3322-3343`）

`{"interactive_owner": str|null, "human_attached": bool, "human_active": bool, "recovery_mode": bool}`；`human_active` = human lease 且 bridge `last_human_input_at` 在 `HUMAN_ACTIVE_WINDOW_S = 60.0` 秒內。以 `**` 展開進 self_test 回應頂層。

### 6.3 `CommandRecord`（arbiter，`arbiter.py:168-190`）

| key | 型別 | 初值 |
|---|---|---|
| `cmd_id` | str | `uuid4().hex`（32 hex） |
| `session_id` | str | |
| `command` | str | |
| `source` | str | |
| `mode` | str | 原樣（未驗證） |
| `execution_mode` | str | `{"fg":"line","bg":"background"}.get(mode, mode)`；worker 完成後可被結果覆寫 |
| `timeout_s` | float | |
| `expected_duration_s` | float \| null | |
| `completion_regex` | str \| null | |
| `priority` | int | |
| `status` | str | `accepted` → `running` → `done`/`error`/`interactive`/`canceled` |
| `created_at`, `accepted_at` | str | 同一個 now_iso |
| `started_at`, `done_at` | str \| null | |
| `error_code` | str \| null | |
| `stdout` | str | `""` |
| `partial` | bool | False |
| `background_capture_id` | str \| null | |
| `interactive_session_id` | str \| null | |
| `recovery_action` | str \| null | |

worker 終結規則（`arbiter.py:266-326`）：
- 取出時若 `status == "canceled"` 或已有 `done_at` → 跳過。否則 `status="running"`、`started_at=now`。
- `send_cb` 拋例外 → `status="error"`、`error_code="SEND_FAILED"`、`done_at`。
- 回 dict：複製 `stdout`、`partial`、`background_capture_id`、`interactive_session_id`、`recovery_action`、`execution_mode`（存在才複製）；`not result.get("ok", True)` → `status="error"`、`error_code = result.get("error_code") or "COMMAND_FAILED"`；`result.status == "interactive"` → `status="interactive"`；否則 `"done"`。**ok:true 回應裡的 `error_code`（如 `PROMPT_TIMEOUT_RECOVERED`）不會寫入記錄**（記錄 `error_code` 保持 null、`status=done`）；`status:"recovering"` 也落為 `done`。
- 若期間被 cancel（status canceled）→ 結果不寫回（但 `done_at` 已由 cancel 設定）。
- flush（detach/shutdown/CTRL_C 成功 recover）：只動 `status=="accepted"` 且無 `done_at` 者 → `status="error"`、`error_code` = `FLUSHED_BY_RECOVERY`（預設）/`FLUSHED_BY_SHUTDOWN`、`done_at`。
- 淘汰：`len > CMD_HISTORY_MAX (512)` 時依 `done_at` 字串排序淘汰最舊已完成者。

### 6.4 `WalRecord`（`wal.py:131-147`；`log.tail_raw`、`wal.range`、`result.tail` legacy 的 `records[]` 元素，以磁碟 JSON 原樣回）

`{"seq": int, "mono_ts_ns": int, "wall_ts": str, "com": str, "dir": "RX"|"TX"|"META"…, "source": str, "cmd_id": str|null, "len": int, "crc32": "<8 hex 小寫>", "payload_b64": str, "loss_flag": bool, "meta": object, ["rotation_failed": true]}`（磁碟以 `sort_keys=True` 緊湊寫入，回傳時保持解析後的 key 順序＝字母序）。

### 6.5 其他

- `DeviceRow`：`{"by_id": str, "real_path": str}`，依 by_id 排序（`session_manager.py:2236-2238`）。
- `ConsoleRow`（`uart_io.py:1281-1297`）：`{"client_id": str(12 hex), "label": str, "vtty": str, "interactive_owner": bool}`，依 `(label, client_id)` 排序；呼叫時 prune stale consoles（副作用）。
- `AliasRow`（`alias_registry.py:22-29`）：依 alias 排序；兩種形狀 `{"session_id": str, "updated_at": str, "alias": str}` 或 `{"device_by_id": str, "updated_at": str, ["profile": str], "alias": str}`。

---

## 7. Method 目錄（54 個）

格式：**params**（名稱：轉型 → 預設）／**驗證**／**回應**／**error_code**／**副作用**／**屬性**（B=blocking offload、R=client 可重試）。未特別說明者，所有 service 回應末尾都加 `version`。

### 7.1 health / daemon

#### `health.ping`（`service.py:706`）R
- 回應：`{"ok": true, "pong": true, "version": str}`。無副作用。

#### `health.status`（`service.py:708` → `health()` `562-616`）B R
- params：無。
- 回應（key 順序）：
  - `ok: true`、`pid: int`、`version: str`、`running: bool`、`started_at: str|null`、`sessions: int`（session 數）、`devices: int`、`commands: int`（arbiter `_commands` 總數，含已完成歷史，≤ 512+pending）、`wal_path: str`、`mirror_path: str`
  - `wal: {"wal_dir": str, "wal_path": str, "wal_dir_exists": bool, "wal_file_exists": bool, "wal_dir_writable": bool (W_OK|X_OK), "current_seq": int, "write_failures": int, "last_write_error": str|null, "recreated_count": int, "healthy": bool}`（`wal.py:183-225`；`healthy = dir_exists and dir_writable and not (seq>0 and not file_exists)`；不取 WAL 鎖）
  - `limits: {"max_submit_cmd_bytes": 16384, "warn_submit_cmd_bytes": 4096, "reject_error_code": "CMD_TOO_LONG", "newline_error_code": "CMD_CONTAINS_NEWLINE", "warning_code": "CMD_LENGTH_WARNING", "newline_forbidden": true}`
  - `warnings: [str]`（**僅非空時出現**）：`"no_profiles_loaded"`（`_profile_count == 0`；**只計 explicit targets，不計 templates**——純動態設定恆出此警告）、`"no_devices_found"`
  - `multi_open: bool`、`foreign_holders: {tty_real_path: pid}`、`multi_open_detail: {"daemons": [{"pid": int, "socket": str|null}], "holders_status": "ok"|"permission"|"unknown"}`
- multi_open 計算（`multi_open.py`）：`tty_paths` = 各 session 非空 `attached_real_path`；掃 `/proc/*/cmdline` 辨識 serialwrapd（argv0 basename == `serialwrapd`；或任一參數以 `serialwrapd.py` 結尾；或 argv[1:] 中含 `/` 且 basename == `serialwrapd`）；`multi_open = len(daemons) > 1`（**含自身**）；`daemons` 依 pid 排序、`socket` 取 `--socket X`/`--socket=X`；holders 只掃 **serialwrapd 程序**的 `/proc/<pid>/fd` readlink 精確比對 tty 路徑——**自身 daemon 也算，健康單一 daemon 的 `foreign_holders` 含自身 pid**；外部 minicom 不會列入。`listdir(/proc)` 失敗 → `{"multi_open":false,"daemons":[],"holders":{},"holders_status":"unknown"}`；任一 fd 目錄 PermissionError → `permission`；其他 OSError 且當前仍 ok → `unknown`。
- 副作用：`list_sessions()` 觸發 console prune（見 6.1）。

#### `daemon.stop`（`daemon.py:167-169`）
- 回應：`{"id":..,"ok": true, "stopping": true}`（**無 version**）。副作用：觸發 §3.2 關閉序列。

### 7.2 mcu

#### `mcu.patterns`（`service.py:711-715`）R
- 回應：`{"ok": true, "patterns": [{"family": str, "probe": str, "expect": str, "baud": int}]}`；`probe`/`expect` = `bytes.hex(" ")`（小寫、空白分隔，如 `"55 55"`、`"00 cc"`）。預設唯一 pattern：`ti-cc26xx`／`5555`／`00cc`／115200（`mcu_patterns.py:56-63`；`timeout_ms` 不外露）。

#### `mcu.status`（`service.py:716-724`）R
- 回應：`{"ok": true, "candidates": [{"com": str, "by_id": str|null, "real_path": str|null, "command_capable": false}], "flashing": bool, "flashing_com": str|null, "last_detect": null | {"status": "matched"|"ambiguous"|"none", "com": str|null, "family": str|null, "hits": [{"by_id": str, "com": str|null}]}}`
- candidates = 非 command_capable 且 state ∈ {READY, ATTACHED} 的 session（`_flash_candidates`，`service.py:485-500`）。

### 7.3 device

#### `device.list`（`service.py:726`）R
- 回應：`{"ok": true, "devices": [DeviceRow]}`。

#### `device.release`（`service.py:729-735` → `release_device` `session_manager.py:1852-1876`）
- params：`selector`（str，必填）、`source`（`str(... or "cli")`）、`reason`（**原樣**，任意 JSON 型別）。
- 驗證：selector 空 → `{"ok": false, "error_code": "INVALID_ARGS"}`（無其他欄位）。
- 回應：
  - 找不到 → `{"ok":false,"error_code":"SESSION_NOT_FOUND","selector":<原 selector>}`
  - FLASHING 或 flash-critical → `{"ok":false,"error_code":"FLASHING_BUSY","selector":<com>,"session":SessionPublic}`
  - 已 RELEASED → `{"ok":true,"already_released":true,"session":...}`
  - 成功 → `{"ok":true,"session":SessionPublic,"closed_consoles":int,"aborted_cmd":bool}`
- 副作用：detach bridge（`drop_consoles=True`；lease 關閉、capture 停止、background capture 標 done、arbiter unregister → 排隊命令 `FLUSHED_BY_RECOVERY`）、state=RELEASED、`released_by/at/reason`、by_id 加入 `_released_by_ids`、**`_save_state()`**（失敗例外 → `EXCEPTION`，但記憶體狀態已改）。

#### `device.attach`（`service.py:737-741` → `attach_device` `2004-2032`）
- params：`selector`（必填）、`force`（`bool()` truthiness）。
- 回應：
  - `INVALID_ARGS`；`SESSION_NOT_FOUND` + `selector`
  - 已有 bridge 且非 RELEASED 且 by_id 不在 released 集合 → `{"ok":true,"already_attached":true,"session":...}`
  - by_id 空或裝置不在 → `{"ok":false,"error_code":"DEVICE_NOT_PRESENT","selector":...,"device_by_id":str|null}`
  - 非 force 且 `/proc` 掃到外部持有者（排除自身 pid；readlink 比對 realpath/原路徑，或 `st_rdev` 相同）→ `{"ok":false,"error_code":"DEVICE_STILL_HELD","pids":[int],"selector":...}`（**無 `holder_unknown` 欄位**；`/proc` 不可列時回 `pids:[]` 視為可收回，見 D-08）
  - 成功 → `{"ok":true,"session":SessionPublic}`（state 已設 ATTACHING）
- 副作用：清 released 標記與 provenance、state=ATTACHING、last_error=None、重置 reprobe、`_save_state()`、`_spawn_attach(by_id)`（背景執行緒）。**/proc 掃描在 event loop 上同步執行**（非 BLOCKING）。

### 7.4 session（查詢）

#### `session.list`（`743`）R
- `{"ok": true, "sessions": [SessionPublic]}`（依 session_id 排序）。

#### `session.get_state`（`746-748` → `get_session_state` `3934-3943`）R
- params：`selector`（`str(... or "")`），**不檢查空值**。
- 回應：`{"ok":true,"session":SessionPublic}` 或 `{"ok":false,"error_code":"SESSION_NOT_FOUND","selector":...}`。
- 副作用：`session.expire_ready_reconfirm()`（pending 逾時／逾次 → 翻 `ready_reconfirm_failed`）。

#### `session.activity`（`750-760`）R
- 同 `session.get_state`，差別：selector 別名鏈 + 空 → `INVALID_ARGS`。

#### `session.console_list`（`834-838` → `list_consoles` `4002-4007`）R
- 回應：`{"ok":true,"consoles":[ConsoleRow],"session":...}`；session 不在或無 bridge → `{"ok":false,"error_code":"SESSION_NOT_READY","selector":...}`。副作用：prune stale console（冪等）。

#### `session.log_status`（`1063-1067` → `log_status` `4822-4841`）R
- 無 capture：`{"ok":true,"active":false,"session":...}`；有：`{"ok":true,"active":bool(status=="active"),"capture_id","log_path","line_count":int,"byte_count":int,"started_at":str,"session":...}`；`SESSION_NOT_FOUND`+`selector`；`INVALID_ARGS`。

### 7.5 session（生命週期）

#### `session.self_test`（`762-772` → `self_test` `4286-4580`）B
- params：selector 別名鏈、`timeout_s`（float → 2.0）、`strict_human_lock`（`_coerce_rpc_bool`）。
- 頂層一律補 `command_capable: bool`（取巢狀 session 的值；無 session → false）。
- `INVALID_ARGS`（無 command_capable——此分支在 service 層早退，**不經** self_test 包裝，故**沒有** `command_capable`）。
- 分支（皆 `ok:true`，除 SESSION_NOT_FOUND）：

| classification | 額外欄位 | recommended_action | 條件 |
|---|---|---|---|
| —（`ok:false`,`error_code:"SESSION_NOT_FOUND"`）| `selector` + LeaseContext(None) | — | 查無 |
| `RELEASED` | `released_by`,`released_at`,`reason`,`external_holder`（`[int]` 或字串 `"none"`）,`reclaimable: bool` | `device_attach`／`wait_external_flash` | state RELEASED（/proc 掃描在鎖外） |
| `SESSION_RECOVERING` | — | `wait` | `session.recovering` |
| `HUMAN_INTERACTIVE_ACTIVE` | `interactive_id` | `wait_or_detach_console` | strict 且 human lease |
| `DEVICE_MISSING` | — | `check_cable_or_bind` | 裝置不在 |
| `DEVICE_REBOUND_REQUIRED` | `attached_real_path`,`current_real_path` | `reattach` | real path 變了 |
| `BRIDGE_DOWN` | `current_real_path` | `attach`（無 bridge）／`recover`（bridge 未 running 或 serial 死） | |
| `VTTY_STALE` | `attached_vtty` | `console_attach` | `vtty_alive` false |
| ATTACHED 族：`PASSTHROUGH`／`LOGIN_REQUIRED`／`REBOOTING`／`BOOTLOADER`／`RX_FLOOD`／`ATTACHED_NOT_READY` | 共同：`attached_real_path`,`current_real_path`,`attached_vtty`,`bridge_generation`；PASSTHROUGH 且 fallback 命中：`suggested_profile`,`hint`；BOOTLOADER：`matched_prompt`,`rx_tail`；RX_FLOOD：`rx_bytes_last_10s` | PASSTHROUGH→`console_attach` 或 `pin_profile`；LOGIN_REQUIRED→`console_attach`；REBOOTING→`wait_or_console_attach`；BOOTLOADER→`recover_interactive`；RX_FLOOD→`wait`；ATTACHED_NOT_READY→`console_attach` | state ATTACHED |
| `AUTOBOOT_QUIET` | — | `wait` | 非 ATTACHED 且 boot quiet 進行中 |
| `RX_FLOOD`（probe 後） | `attached_real_path`,`current_real_path`,`probe_ok:false`,`rx_bytes_last_10s` | `wait` | nonce 未回且 RX ≥ 20000 B/10s |
| `TARGET_UNRESPONSIVE` | `attached_real_path`,`current_real_path`,`probe_ok:false` | `recover` | nonce 未回 |
| `OK` | `attached_real_path`,`current_real_path`,`attached_vtty`,`bridge_generation`,`probe_ok:true` | `none` | nonce 回且等 prompt |

- 所有分支均另含 `session` 與 LeaseContext 四欄。
- 副作用（OK/probe 路徑）：送 `ready_probe`（`${nonce}` 換成 `uuid4().hex[:8]`）到 UART（source `system:self_test`）、`last_probe_at`、human lease 時 suspend/resume interactive、成功 → `confirm_ready()`（清 quiet 與 pending）。**注意：state 為 FLASHING／RECOVERING 以外的非 ATTACHED 狀態（含 FLASHING）也會走到 nonce probe 分支**（見 H-07）。

#### `session.recover`（`774-780` → `recover_session` `4582-4667`）B
- params：selector 別名鏈、`timeout_s`（float → 2.0）、`force`（`bool()`）。
- 回應：
  - `INVALID_ARGS`；`SESSION_NOT_FOUND`+`selector`
  - RELEASED → `{"ok":true,"released":true,"recommended_action":"device_attach","session":...}`
  - flash busy → `FLASHING_BUSY` + `selector`(com) + `session`
  - 無 bridge 且裝置在 → `{"ok":true,"recovering":false,"action":"REATTACH","session":...}`（spawn attach）；裝置不在 → `{"ok":false,"error_code":"SESSION_NOT_READY","session":...}`
  - ATTACHED（非 force）→ probe：probe 回 ok:false（`SESSION_NOT_READY`／`STATE_CHANGED`+`session`）→ 原樣（force 時改走 force）；否則 `{"ok":true,"recovering":false,"action":"REPROBE","recovered":bool,"session":...}`，未恢復時加 `error_code = session.last_error or "SESSION_NOT_READY"`（**ok:true 但帶 error_code**），bootloader 證據時加 `classification:"BOOTLOADER"`,`recommended_action:"recover_interactive"`,[`matched_prompt`]；force 且未恢復 → force 路徑
  - 其他非 READY 狀態：force → force 路徑；否則 `SESSION_NOT_READY`+`session`
  - READY → `_recover_after_failure(source="system:recover", cmd_id="")`：
    - CTRL_C 或 CTRL_D（`\x03`/`\x04`，每個等 `min(timeout_s, 2.0)`）後見 prompt → `{"ok":true,"error_code":"PROMPT_TIMEOUT_RECOVERED","stdout":str,"partial":true,"recovery_action":"CTRL_C"|"CTRL_D"}`，**並 flush 該 session 排隊命令（FLUSHED_BY_RECOVERY）**
    - 皆失敗（或 boot quiet 中略過按鍵）→ session 轉 ATTACHED（reason PROMPT_TIMEOUT）→ 自動 `_force_recover`：成功 → `{"ok":true,"error_code":"PROMPT_TIMEOUT_FORCE_RECOVERED","partial":true,"recovery_action":"FORCE_CLEAR_REATTACH","session":...}`；失敗 → `{"ok":false,"error_code":"PROMPT_TIMEOUT","partial":true,"recovery_action":"NONE"}`
  - force 路徑 `_force_recover`（`4669-4692`）：`clear_session` 後每 1s 輪詢最多 10 次：READY → `{"ok":true,"recovering":false,"action":"FORCE_CLEAR_REATTACH","recovered":true,"session":...}`；否則 `{"ok":false,"error_code":"FORCE_RECOVER_TIMEOUT","action":"FORCE_CLEAR_REATTACH","recovered":false,"session":<dict 或 {}>}`
- 副作用：重置 reprobe、`credentials_warned=False`、UART TX、state 轉移、`_save_state`（clear 路徑）。

#### `session.clear`（`782-792` → `clear_session` `1829-1850`）
- 回應：`INVALID_ARGS`；`SESSION_NOT_FOUND`+`selector`；flash busy → `FLASHING_BUSY`+`selector`+`session`；RELEASED（或 by_id 在 released 集合）→ `{"ok":true,"released":true,"session":...}`；成功 → `{"ok":true,"session":...}`。
- 副作用：detach（last_error=`CLEARED`）、裝置在 → state ATTACHING；`_save_state()`；`_spawn_attach`。

#### `session.bind`（`794-799` → `bind_session` `2034-2093`）
- params：selector 別名鏈、`device_by_id` → `by_id`（皆 str）。
- 回應：任一空 → `INVALID_ARGS`；strip 後空 → `INVALID_ARGS`；`SESSION_NOT_FOUND`+`selector`；`FLASHING_BUSY`+`selector`+`session`；他 session 已綁 → `{"ok":false,"error_code":"DEVICE_ALREADY_BOUND","device_by_id":str,"session_id":<他者>}`；冪等 → `{"ok":true,"already_bound":true,"session":...}`；成功 → `{"ok":true,"session":...}`（裝置不在時 session.last_error=`DEVICE_NOT_FOUND`、state DETACHED，仍 ok:true）。
- 副作用：detach（REBOUND）、離開 RELEASED、`_binding_overrides[sid]=by_id`、`_save_state()`、spawn attach。

#### `session.pin`（`801-806` → `pin_session` `2111-2122`）
- params：selector 別名鏈、`profile` → `profile_name`。
- 回應：空 → `INVALID_ARGS`；template 不存在 → `{"ok":false,"error_code":"UNKNOWN_PROFILE"}`；selector 解析不到 device key（可為 COM/alias/sid/by-id 或 `/dev/serial/by-id/`、`/dev/serial/by-path/` 開頭字串）→ `{"ok":false,"error_code":"DEVICE_NOT_FOUND"}`；`profile_source=="yaml-target"` → `{"ok":false,"error_code":"PROFILE_IS_EXPLICIT"}`；成功 → `{"ok":true,"device_key":str,"profile":str}`。副作用：`_profile_pins`、`_save_state()`（不 reattach；需 `session.clear` 套用）。

#### `session.unpin`（`808-812`）
- 同上去掉 profile；成功 `{"ok":true,"device_key":str}`。

#### `session.attach`（`814-818` → `attach_session` `2150-2234`）B
- 回應：
  - `INVALID_ARGS`；`SESSION_NOT_FOUND`+`selector`
  - 無 by_id → `{"ok":false,"error_code":"DEVICE_NOT_BOUND","session":...}`
  - RELEASED → `{"ok":true,"released":true,"recommended_action":"device_attach","session":...}`
  - 有 bridge：human lease 或非 ATTACHED → `_attach_state_result`：command_capable 且 state ∉ {READY, ATTACHING} → `{"ok":false,"error_code": FLASHING→"FLASHING_BUSY" / RECOVERING→"SESSION_RECOVERING" / 否則 `session.last_error` / 否則 `"NOT_READY"`, "session":...}`，否則 `{"ok":true,"session":...}`
  - ATTACHED 無 human lease → 同步 probe（`_probe_existing_bridge`）；期間被 RELEASED → released payload；probe ok:false（`SESSION_NOT_READY`（無 session）/`STATE_CHANGED`+session）→ 原樣；否則 `_attach_state_result(probe_session)`
  - 無 bridge 且裝置不在 → `{"ok":false,"error_code":"DEVICE_NOT_FOUND","session":...}`（並把 state 設 DETACHED、last_error DEVICE_NOT_FOUND）
  - 無 bridge 且裝置在 → `{"ok":true,"session":...}`（state ATTACHING，spawn attach）
- **error_code 可為任意 session `last_error` 值**（見 §8.3、D-05）。

### 7.6 session（console / interactive）

#### `session.console_attach`（`820-825` → `attach_console` `3945-3976`）B
- params：selector 別名鏈、`label`（`str(label) if label else None`）。
- 失敗：`INVALID_ARGS`；session 不在／無 bridge／state ∉ {READY, ATTACHED} → `{"ok":false,"error_code":"SESSION_NOT_READY","selector":...}`。
- POSIX 成功：`{"ok":true,"client_id":str(12 hex),"label":str,"vtty":"/dev/pts/N",["interactive_session_id":str,"interactive_owner":bool],"session":...}`（後兩欄僅在本次新開 human lease 時出現；lease owner `human:<client_id>`、timeout `max(profile.hard_timeout_s, 86400.0)`）。
- Windows（無 PTY）：`{"ok":true,"client_id":null,"label":str|null,"vtty":<endpoint>,"endpoint":"host:port","transport":"tcp","protocol":"telnet","session":...}`，不開 lease。

#### `session.console_detach`（`827-832` → `detach_console` `3978-4000`）
- params：selector 別名鏈、`client_id`（str）。皆必填。
- 回應：`INVALID_ARGS`；無 session／bridge → `SESSION_NOT_READY`+`selector`；client 不存在 → `{"ok":false,"error_code":"CONSOLE_NOT_FOUND","client_id":...}`；成功 `{"ok":true,"client_id":...,"session":...}`。副作用：關 human lease（若屬該 client）。

#### `session.interactive_open`（`840-848` → `interactive_open` `4009-4189`）
- params：selector 別名鏈、`owner`（str → `"agent"`）、`timeout_s`（float → 60.0）、`command`（str → `""`）、`allow_attached`（`bool(params.get("allow_attached", False))`）。
- 回應：
  - `INVALID_ARGS`；session 不在／無 bridge → `SESSION_NOT_READY`+`selector`
  - READY：前景忙 → `{"ok":false,"error_code":"SESSION_BUSY","selector":<com>}`；既有 human lease 閒置且 owner 非 human → soft preempt `{"ok":true,"interactive_id":str,"session":...,"recovery_mode":false,"soft_preempted":true}`；既有其他 lease → `{"ok":false,"error_code":"SESSION_INTERACTIVE_BUSY","interactive_session_id":str}`；無 → `{"ok":true,"interactive_id":str,"session":...,"recovery_mode":false}`
  - 非 READY 且 `allow_attached` false → `SESSION_NOT_READY`+`selector`
  - ATTACHED + allow_attached：bridge 不健康 → `SESSION_NOT_READY`+`selector`；rx tail（512 B）尾行不符 bootloader prompts 且無 boot banner 且無 login/password → `{"ok":false,"error_code":"SESSION_NOT_READY","selector":...,"error_detail":"NOT_BOOTLOADER"}`；否則 recovery lease（timeout `min(timeout_s, 120.0)`）→ `{"ok":true,"interactive_id","session","recovery_mode":true,["boot_interrupt":true],["login_required":true]}`；他 agent lease → `SESSION_INTERACTIVE_BUSY`
  - 其他狀態 → `SESSION_NOT_READY`+`selector`
- 副作用：開 lease、可能 suspend human；`command` 非空 → `bridge.send_command(command, source=owner)`（在 event loop 上）。**未檢查 FLASHING**（FLASHING 走「其他狀態」→ SESSION_NOT_READY）。

#### `session.interactive_send`（`850-856` → `interactive_send` `4212-4241`）
- params：`interactive_id`（必填）、`data`（str → `""`）、`encoding`（str → `"plain"`）。
- encoding：`plain` → UTF-8（errors=replace）；`base64` → `b64decode`（**非 validate 模式**，非法字元被丟棄；padding 錯誤拋 `binascii.Error`）；`key` + `enter|tab|escape|ctrl-c|ctrl-d|up|down|right|left`（strip+lower）→ `\n`、`\t`、`\x1b`、`\x03`、`\x04`、`\x1b[A`、`\x1b[B`、`\x1b[C`、`\x1b[D`；其他 → `raise ValueError("INVALID_INTERACTIVE_ENCODING")` → **`{"ok":false,"error_code":"EXCEPTION","message":"INVALID_INTERACTIVE_ENCODING"}`**（無 version）。
- 回應：lease 不在或非 active → `{"ok":false,"error_code":"INTERACTIVE_NOT_FOUND","interactive_id":...}`；過期 → `INTERACTIVE_EXPIRED`+`interactive_id`（並關閉）；session 不在/無 bridge → `SESSION_NOT_READY`+`interactive_id`；FLASHING → `FLASHING_BUSY`+`interactive_id`；前景忙 → `SESSION_BUSY`+`interactive_id`；成功 `{"ok":true,"interactive_id":...,"bytes":int}`。副作用：`send_bytes`（source = lease.owner）、`lease.touch()`。

#### `session.interactive_status`（`858-863` → `interactive_status` `4243-4270`）
- params：`interactive_id`（必填）、`screen_chars`（int → 2048）。
- 回應：`INTERACTIVE_NOT_FOUND`；`SESSION_NOT_READY`+`interactive_id`；`INTERACTIVE_EXPIRED`；成功 `{"ok":true,"interactive_id","owner":str,"status":str,"recovery_mode":bool,"screen":str(clean_text(rx_tail(screen_chars))),"session":...}`。
- **副作用：`lease.touch()` 延長租約**（spec §4.2 明列不可重試的理由）。`screen_chars` 負值 → `rx_text[-(-n):]` 語意怪異（H-08）。

#### `session.interactive_close`（`865-869` → `interactive_close` `4272-4284`）
- 回應：`INTERACTIVE_NOT_FOUND`+`interactive_id`；成功 `{"ok":true,"interactive_id":...}`（session 不在時直接移除）。副作用：關 lease、還原 stashed human lease、resume interactive。

### 7.7 session log capture

#### `session.log_start`（`1051-1055` → `log_start` `4764-4801`）
- 回應：`INVALID_ARGS`；`SESSION_NOT_FOUND`+`selector`；已 active → `{"ok":true,"already_active":true,"capture_id","log_path","session"}`；`open()` OSError → `{"ok":false,"error_code":"LOG_OPEN_FAILED","detail":str}`；成功 `{"ok":true,"capture_id":str(uuid4，含連字號),"log_path":str,"session":...}`。
- log_path = `{profile.log_dir or LOG_DIR}/{com}_{%y%m%d-%H%M%S 本地時間}.log`（append 模式）。`os.makedirs(log_dir)` 失敗**未被攔** → `EXCEPTION`。

#### `session.log_stop`（`1057-1061` → `log_stop` `4803-4820`）
- 回應：`INVALID_ARGS`；`SESSION_NOT_FOUND`；無 capture → `{"ok":false,"error_code":"NO_ACTIVE_CAPTURE","session":...}`；成功 `{"ok":true,"capture_id","log_path","line_count","byte_count","started_at"}`（**無 session**）。

### 7.8 alias

#### `alias.list`（`871`）R — `{"ok":true,"aliases":[AliasRow]}`。

#### `alias.set`（`874-877` → `set_alias_for_session` `1716-1724`）
- params：`session_id`、`alias`（皆 str），**無 INVALID_ARGS 檢查**（alias 可為 `""`）。
- 回應：`{"ok":false,"error_code":"SESSION_NOT_FOUND","session_id":...}` 或 `{"ok":true,"session_id","alias"}`。副作用：profile.alias 改寫、registry 列、`_save_state()`。

#### `alias.assign`（`879-885` → `assign_alias` `1726-1729`）
- params：`by_id`、`alias`（必填）、`profile`（`str(profile) if profile else None`）。
- 回應：`INVALID_ARGS`；`{"ok":true,"alias","device_by_id"}`。不驗證 by_id 是否存在。`_save_state()`。

#### `alias.unassign`（`887-889` → `unassign_alias` `1731-1736`）
- 回應：`{"ok":false,"error_code":"ALIAS_NOT_FOUND","alias":...}` 或 `{"ok":true,"alias":...}`；**兩者都先 `_save_state()`**。

### 7.9 command

#### `command.submit`（`891-918`）
- params 與轉型順序：
  1. `completion_regex`（原樣）→ `valid_completion_regex`：`None` OK；否則必須非空 str、UTF-8 ≤ `COMPLETION_REGEX_MAX_BYTES = 1024`、Python `re.compile` 成功、`compiled.search("") is None`（不可匹配空字串）；`re.error`/`UnicodeEncodeError`/`RecursionError` → 不合法 → `{"ok":false,"error_code":"INVALID_COMPLETION_REGEX"}`（**在 selector 檢查之前**）
  2. selector：`selector → com → alias`
  3. `cmd`：`cmd → command`（str，可為 `""`）
  4. `source`：str → `"agent"`
  5. `mode`：str → `"line"`（**不驗證**；`fg`→line、`bg`→background 別名；未知值當 line 執行）
  6. `timeout_s`：float → 10.0
  7. `priority`：int → 10（數值小者優先；`0` 會變 10；負數可用）
  8. `expected_duration_s`：`float(raw) if raw is not None else None`（`0` 合法）
- 驗證：selector 空 → `INVALID_ARGS`；`_resolve_session_id`（§5.4）；`arbiter.submit`：
  - 再驗 completion_regex（同上碼）
  - UTF-8 長度 > 16384 → `{"ok":false,"error_code":"CMD_TOO_LONG","cmd_length":int,"limit":16384,"hint":"Command exceeds 16 KB hard limit. Use file-based injection or split into smaller commands."}`
  - 含 `"\n"` → `{"ok":false,"error_code":"CMD_CONTAINS_NEWLINE","hint":"Command must not contain embedded newline characters. Split into multiple independent submissions."}`（**長度檢查先於換行檢查**；`\r` 允許）
  - session queue 未註冊（READY 但 arbiter 無 worker 的競態）→ `{"ok":false,"error_code":"SESSION_NOT_READY","session_id":...}`
  - pending（無 done_at）≥ `CMD_PENDING_MAX = 256` → `{"ok":false,"error_code":"SESSION_QUEUE_FULL","session_id","pending":int,"limit":256,"hint":"Per-session pending command queue is full (backpressure). Wait for in-flight commands to drain before submitting more."}`
- 成功：`{"ok":true,"cmd_id":str,"status":"accepted","session_id":str,["warning":{"code":"CMD_LENGTH_WARNING","cmd_length":int,"soft_limit":4096,"hint":"Command exceeds 4 KB soft limit. Long commands may cause UART buffer overflow or prompt timeout."}]}`（warning 在長度 > 4096 時）。
- 副作用：建 CommandRecord、入 PriorityQueue（sort key `(priority, counter)`）、淘汰歷史。
- 執行期（worker → `execute_command`，結果進記錄，見 §6.3）可能錯誤碼：`INVALID_COMPLETION_REGEX`、`FLASHING_BUSY`、`SESSION_NOT_READY`、`READY_UNCONFIRMED`、`AUTOBOOT_QUIET`、`SESSION_RECOVERING`、`SESSION_BUSY`、`SESSION_INTERACTIVE_BUSY`、`PROMPT_TIMEOUT`、`SEND_FAILED`、`COMMAND_FAILED`、`FLUSHED_BY_RECOVERY`、`FLUSHED_BY_SHUTDOWN`；成功路徑的 `recovery_action` ∈ {`PROMOTE_HUMAN_INTERACTIVE`,`CTRL_C`,`CTRL_D`,`FORCE_CLEAR_REATTACH`,`NONE`,`EXPECT_REBOOT`}。

#### `command.get`（`920-922`）
- params：`cmd_id`（str，不檢空）。回應：`{"ok":true,"command":CommandRecord}` 或 `{"ok":false,"error_code":"CMD_NOT_FOUND","cmd_id":...}`。

#### `command.result_tail`（`924-933`）B
- params：`cmd_id`（必填）、`from_chunk`（int → 0）、`limit`（int → 200）。轉型在 cmd_id 檢查**之前**。
- 回應（capture 存在，`get_background_result` `4717-4746`）：`{"ok":true,"cmd_id","status":"active"|"done"|"error","error_code":str|null,"from_seq":int,"last_seq":int,"from_chunk":int(原值),"next_chunk":int,"dropped_chunks":int,"lost":bool(from_chunk < dropped_chunks),"chunks":[str]}`。讀取時 `maybe_finalize()`（quiet_window 後 active→done，副作用）。
- capture 不存在 → `_bg_fallback_from_arbiter`（`service.py:324-356`）：記錄不存在／非 background／status done／canceled 且已開始 → `{"ok":false,"error_code":"CMD_NOT_FOUND","cmd_id":...}`；否則 `{"ok":true,"cmd_id","status":<記錄 status>,"error_code":<記錄>,"from_seq":0,"last_seq":0,"from_chunk":from_chunk,"next_chunk":from_chunk,"chunks":[]}`（**無 `dropped_chunks`、`lost`**）。
- `limit` 負值 → `islice` 拋 ValueError → `EXCEPTION`。

#### `command.cancel`（`935-937`）
- 回應：`CMD_NOT_FOUND`+`cmd_id`；status ∈ {done, error} → `{"ok":false,"error_code":"CMD_NOT_CANCELABLE","cmd_id"}`；否則 `{"ok":true,"cmd_id","status":"canceled"}`（`running`/`interactive`/`canceled` 也可再 cancel；**running 命令不會被中止**，只是記錄標 canceled）。

#### `result.tail`（`939-962`，legacy）B
- 有 `cmd_id` → 同 `command.result_tail`（含 fallback）。
- 無 `cmd_id`（CLI `stream tail` 使用）：selector `com → selector`；`from_seq` int → 0（range 模式，`seq > from_seq`）；`limit` int → 200；selector 非空時解析 session（失敗原樣回）；回 `{"ok":true,"records":[WalRecord]}`——**無 WAL_MISSING 檢查、無任何 metadata**。

### 7.10 log / wal

#### `log.tail_raw` / `log.tail_text`（`964-1019`）B
- params：selector `com → selector`；`from_seq`：key 缺省或 `null` → **latest 模式**（`None`）；否則 `int(raw)`，`TypeError/ValueError` → `INVALID_ARGS`（`""`、`"abc"`；注意 `true`→1、`3.7`→3 被接受）；`limit` int → 200。
- 順序：from_seq 解析 → limit → selector 解析（`SESSION_NOT_FOUND` 原樣）→ `wal.health()` → `WAL_MISSING` 檢查 → 掃描。
- `WAL_MISSING`（`_wal_missing_response` `538-560`）：`wal_file_exists` false 且 `current_seq > 0` → `{"ok":false,"error_code":"WAL_MISSING","wal_path","wal_dir","wal_dir_exists":bool,"wal_file_exists":false,"current_seq":int,"hint":<固定中文>}`。
- 掃描（`tail_raw_with_meta` `wal.py:292-328`）：只讀現行 `raw.wal.ndjson`（輪替檔不含）；略過空行／非 JSON／非 dict／`seq` 非 int；com 過濾（`com` 為 session 的 COM）。latest：最後 `max(limit,0)` 筆，`truncated = total > len(rows)`；range：`seq > from_seq` 起最多 limit 筆，再遇到符合者 → `truncated=true`。
- 回應：
  - raw：`{"ok":true,"records":[WalRecord],"returned":int,"from_seq":int|null,"last_seq":int|null,"current_seq":int,"truncated":bool,"wal_path":str,"wal_file_exists":bool}`
  - text：`{"ok":true,"lines":[str],"returned":len(lines),...同 meta}`；`lines` = `rows_to_text_lines`：串接各筆 `to_printable(b64decode(payload_b64))`（**不分方向，RX/TX/META 全串**）、`splitlines()`、未以 `\n` 結尾的尾段保留為最後一行。
- `to_printable`：`string.printable` 中且非 `\x0b`/`\x0c` 的 byte 原樣，其餘 `\\xHH`。

#### `wal.range`（`1021-1043`）B
- params：`from_seq`（int → 0）、`to_seq`（int → 0）、`limit`（int → 1000）。
- 順序：WAL_MISSING 檢查 → `tail_raw(from_seq, com=None, limit)`（range 模式）→ `to_seq > 0` 時過濾 `seq <= to_seq`（**過濾在 limit 之後**）→ `available_from_seq()`（現行檔第一筆 seq 或 null）。
- 回應：`{"ok":true,"records":[WalRecord],"wal_path","wal_file_exists":bool,"available_from_seq":int|null,"rotated_out":bool(available is not None and from_seq + 1 < available)}`。**無 `truncated`、`returned`、`current_seq`**。

#### `wal.reset`（`1045-1046` → `wal.py:242-253`）
- 回應：`{"ok":true,"previous_seq":int,"rotated_suffix":"YYYYmmdd-HHMMSS"(UTC)}`。副作用：持 WAL 鎖，非空的 `raw.wal.ndjson`／`raw.mirror.log` rename 成 `.<ts>`（同秒重複 reset 會覆蓋前一份歸檔）、fsync 目錄、seq 歸 0。OSError 不攔 → `EXCEPTION`。

#### `wal.current_seq`（`1048-1049`）
- `{"ok":true,"seq":int}`（取 WAL 鎖）。

### 7.11 file

#### `file.push`（`1069-1102` → `file_push` `4843-4936` → `push_file` `file_transfer.py:63-261`）B
- params：`selector`（必填）、`local_path`（必填，**daemon 端路徑**）、`remote_path`（必填）、`chunk_size`（`max(1, int(... or 512))`）、`chunk_timeout_s`（`float(raw)` 或 None）、`ack_mode`（str → `"auto"`，∈ `("auto","echo","none")`）、`source`（str → `"agent"`）。轉型例外 → `INVALID_ARGS`；ack_mode 不合法 → `INVALID_ARGS`；三者任一空 → `INVALID_ARGS`；`_resolve_session_id`（§5.4）。
- manager 層：session 不在／無 bridge／非 READY → `{"ok":false,"error_code":"SESSION_NOT_READY"}`（無 selector）；gate → `READY_UNCONFIRMED`（`selector`,`hint`,`recommended_action`）／`AUTOBOOT_QUIET`（`selector`，**無 retry_after_s**）；recovering → `SESSION_RECOVERING`；他 lease → `SESSION_INTERACTIVE_BUSY`；前景忙 → `SESSION_BUSY`+`selector`。
- 預設 timeout：chunk `max(profile.timeout_s, 5.0)`；echo `max(profile.timeout_s, 5.0)`；echo slice 64。
- push_file 錯誤：`INVALID_ARGS`（+`ack_mode` 或 +`chunk_size` 或 +`max_console_line_chars`）、`CONSOLE_LINE_LIMIT_TOO_SMALL`+`max_console_line_chars`、`LOCAL_FILE_NOT_FOUND`+`local_path`、`ECHO_ACK_UNSUPPORTED`、probe：`TRANSFER_TIMEOUT`／`TRANSFER_PROBE_FAILED`／`TARGET_DECODER_MISSING`（僅 `error_code`）、`TRANSFER_ECHO_STALL`+`chunks_sent`,`chunks_total`,`acked_chars`,`sent_chars`、`TRANSFER_TIMEOUT`+`chunks_sent`,`chunks_total`、`TARGET_DECODER_FAILED`+同、`CHECKSUM_VERIFY_FAILED`、`CHECKSUM_MISMATCH`+`expected`,`actual`、`MOVE_TIMEOUT`、`MOVE_FAILED`。
- 成功：`{"ok":true,"bytes":int,"chunks":int,"md5":str,"remote_path":str}`。
- 副作用：UART 大量 TX、遠端暫存檔 `/tmp/.sw_upload_<12hex>`（失敗時 cleanup）、前景 busy、human suspend/resume。

#### `file.pull`（`1104-1131` → `file_pull` `4938-5010` → `pull_file` `file_transfer.py:263-350`）B
- params：`selector`、`remote_path`（必填）、`local_path`（原樣，truthy 才 `str()`，否則 None → `basename(remote_path)`，**相對於 daemon CWD**）、`chunk_timeout_s`、`ack_mode`（只驗白名單、不下傳）、`source`。
- 預設 timeout `max(profile.timeout_s, 30.0)`。
- 錯誤：同 push 的前置碼；`INVALID_ARGS`/`CONSOLE_LINE_LIMIT_TOO_SMALL`、probe `TRANSFER_TIMEOUT`/`TRANSFER_PROBE_FAILED`/`TARGET_ENCODER_MISSING`、`TRANSFER_TIMEOUT`、`TARGET_ENCODER_FAILED`、`PULL_PARSE_FAILED`、`BASE64_DECODE_FAILED`、`CHECKSUM_VERIFY_FAILED`、`CHECKSUM_MISMATCH`+`expected`(remote),`actual`(local)；寫檔 OSError 不攔 → `EXCEPTION`。
- 成功：`{"ok":true,"bytes":int,"md5":str,"local_path":str}`。

### 7.12 event（`service.py:1133-1172` → `event_engine/engine.py`）

| method | params | 回應 | 錯誤／備註 |
|---|---|---|---|
| `event.rule_set` | 整個 `params`（`params or {}`）即規則物件 | `{"ok":true, **rule.raw}`（raw = 原請求 dict，**raw 內的 `ok` key 會覆蓋**） | `RuleSchemaError`（ValueError 子類）或其他 ValueError → `{"ok":false,"error_code":"INVALID_RULE_SCHEMA","error":str}`；檔案 OSError → `EXCEPTION`。副作用：寫 `EVENTS_DIR/<owner>.<name>.json`（tmp+rename、`indent=2, sort_keys`）、替換 matcher 規則、`auto_enable_com_on_load` 時 enable COM、事件 log |
| `event.rule_delete` | `rule_id`（str → `""`） | `{"ok":true,"deleted":bool}` | **永遠 ok:true**；rule_id 未驗證格式直接 `os.path.join(root, f"{rule_id}.json")` unlink（路徑穿越，見 H-01）；成功時清 counter、事件 log |
| `event.rule_list` | `selector`、`owner`（原樣，None 不過濾） | `{"ok":true,"rules":[{"rule_id","owner","kind","selectors":[str],"fires":int,"exhausted":bool,"last_fire_ts":int\|null}]}` | selector 過濾：`selector in selectors or "ALL" in selectors` |
| `event.rule_get` | `rule_id`（str） | `{"ok":true,"rule":{...raw},"counter":{"fires":int,"last_fire_ts":int\|null,"exhausted":bool}}` | 不在記憶體規則 → `{"ok":false,"error_code":"RULE_NOT_FOUND"}` |
| `event.com_enable` | `selector`（str → `""`） | `{"ok":true,"selector":str,"enabled":bool,"active_rules":[rule_id]}` | 空字串也會被加入 enabled 集合 |
| `event.com_disable` | 同上 | 同上 | 對受影響規則清 counter、寫 `counter_reset`/`com_disabled` |
| `event.com_status` | `selector`（原樣） | selector 為 None → `{"ok":true,"coms":[sorted str]}`；否則同 enable 形狀 | |
| `event.reset` | `rule_id`、`selector`（原樣） | `{"ok":true,"cleared":int}` | rule_id 優先；兩者 None → 0 |
| `event.reload` | 無 | `{"ok":true,"added":[id],"removed":[id],"changed":[id]}` | 清單由 set 差集產生，**順序不穩定** |
| `event.tail` | `rule_id`、`selector`、`since_ts`、`n`（原樣） | `{"ok":true,"entries":[object]}` | 讀整個 `events.ndjson`；`n=0` → `out[-0:]` = 全部；`n` 非 int → TypeError → `EXCEPTION`；`since_ts` 與 `ts` 型別不可比 → `EXCEPTION` |

### 7.13 Fallback

- 未知 method → `{"ok":false,"error_code":"METHOD_NOT_FOUND","method":<method>,"version":...}`。

---

## 8. error_code 盤點

### 8.1 transport／daemon 層
`INVALID_JSON`、`INVALID_REQUEST`、`INVALID_METHOD`、`EXCEPTION`（server）；`DAEMON_ALREADY_RUNNING`、`SOCKET_UNAVAILABLE`（lock，僅 stderr 文字、非 JSON）；client 端：`TIMEOUT`、`SOCKET_ERROR`、`EMPTY_RESPONSE`、`INVALID_RESPONSE`、`INVALID_ENDPOINT`。

### 8.2 service 可回的 error_code（依來源）
- service.py：`INVALID_ARGS`、`METHOD_NOT_FOUND`、`CMD_NOT_FOUND`、`PROFILE_NOT_COMMAND_CAPABLE`、`SESSION_NOT_READY`、`READY_UNCONFIRMED`、`AUTOBOOT_QUIET`、`WAL_MISSING`、`INVALID_RULE_SCHEMA`、`RULE_NOT_FOUND`、`INVALID_COMPLETION_REGEX`
- arbiter：`INVALID_COMPLETION_REGEX`、`CMD_TOO_LONG`、`CMD_CONTAINS_NEWLINE`、`SESSION_NOT_READY`、`SESSION_QUEUE_FULL`、`CMD_NOT_FOUND`、`CMD_NOT_CANCELABLE`；記錄內：`SEND_FAILED`、`COMMAND_FAILED`、`FLUSHED_BY_RECOVERY`、`FLUSHED_BY_SHUTDOWN`；warning code `CMD_LENGTH_WARNING`
- session_manager：`SESSION_NOT_FOUND`、`SESSION_NOT_READY`、`FLASHING_BUSY`、`DEVICE_NOT_PRESENT`、`DEVICE_STILL_HELD`、`DEVICE_ALREADY_BOUND`、`DEVICE_NOT_BOUND`、`DEVICE_NOT_FOUND`、`UNKNOWN_PROFILE`、`PROFILE_IS_EXPLICIT`、`SESSION_RECOVERING`、`NOT_READY`、`STATE_CHANGED`、`SESSION_BUSY`、`SESSION_INTERACTIVE_BUSY`、`CONSOLE_NOT_FOUND`、`INTERACTIVE_NOT_FOUND`、`INTERACTIVE_EXPIRED`、`ALIAS_NOT_FOUND`、`LOG_OPEN_FAILED`、`NO_ACTIVE_CAPTURE`、`FORCE_RECOVER_TIMEOUT`、`PROMPT_TIMEOUT`、`PROMPT_TIMEOUT_RECOVERED`、`PROMPT_TIMEOUT_FORCE_RECOVERED`、`READY_UNCONFIRMED`、`AUTOBOOT_QUIET`、`CMD_NOT_FOUND`
- file_transfer：見 7.11（含 `TRANSFER_PROBE_FAILED`、`TARGET_DECODER_MISSING`、`TARGET_ENCODER_MISSING`）
- 非 error_code 的分類字串：`error_detail:"NOT_BOOTLOADER"`；self_test `classification` 集合（§7.5）；`recommended_action` 集合：`wait`、`wait_or_detach_console`、`check_cable_or_bind`、`reattach`、`attach`、`recover`、`console_attach`、`pin_profile`、`wait_or_console_attach`、`recover_interactive`、`none`、`device_attach`、`wait_external_flash`、`self_test`

### 8.3 會以 RPC `error_code` 外流的 session `last_error` 值
`session.attach`（`_attach_state_result`，`session_manager.py:2135-2148`）與 `session.recover` REPROBE 分支（`4645`）直接把 `session.last_error` 當 `error_code`。已知 `last_error` 值：`CLEARED`、`RELEASED`、`REBOUND`、`DEVICE_REBOUND_REQUIRED`、`PROMPT_TIMEOUT`、`DEVICE_NOT_FOUND`、`DEVICE_REMOVED_DURING_ATTACH`、`PROMPT_UNAVAILABLE`、`REBOOTING`、`RECOVERY_TIMEOUT`、`RX_FLOOD`、`TRANSPORT_STALL`、`BOOTLOADER_STUCK`、`CREDENTIALS_UNRESOLVED`，以及 login_fsm 回傳碼 `LOGIN_PROMPT_TIMEOUT`、`POST_LOGIN_CMD_TIMEOUT`、`READY_NONCE_TIMEOUT`、`READY_PROMPT_TIMEOUT`、`CONSOLE_UNLOCK_BOOTLOADER`、`CONSOLE_UNLOCK_IDLE_TIMEOUT`、`CONSOLE_UNLOCK_PROMPT_TIMEOUT`、`LOGIN_REQUIRED`、`LOGIN_USER_REQUIRED`、`USER_ENV_MISSING`、`PASS_ENV_REQUIRED`、`PASS_ENV_MISSING`（完整集合屬 session_manager／login_fsm 盤點範圍）。background capture 的 `error_code` 另有 `PROMPT_TIMEOUT`／`PROMPT_TIMEOUT_RECOVERED`。

---

## 9. 相關常數

| 常數 | 值 | 位置 |
|---|---|---|
| `CMD_WARN_BYTES` | 4096 | `arbiter.py:14` |
| `CMD_REJECT_BYTES` | 16384 | `arbiter.py:15` |
| `ERROR_CMD_TOO_LONG` / `ERROR_CMD_CONTAINS_NEWLINE` / `WARNING_CMD_LENGTH` | `"CMD_TOO_LONG"` / `"CMD_CONTAINS_NEWLINE"` / `"CMD_LENGTH_WARNING"` | `arbiter.py:20-22` |
| `COMPLETION_REGEX_MAX_BYTES` / `ERROR_INVALID_COMPLETION_REGEX` | 1024 / `"INVALID_COMPLETION_REGEX"` | `command_completion.py:7-8` |
| `CMD_HISTORY_MAX` / `CMD_PENDING_MAX` | 512 / 256 | `constants.py:195,197` |
| `BG_CAPTURE_MAX_BYTES` / `BG_CAPTURE_MAX_COUNT` | 4 MiB / 64 | `constants.py:191,193` |
| `DEFERRED_INPUT_MAX_BYTES` | 262144 | `constants.py:202` |
| `DEFAULT_WAL_ROTATE_BYTES` | 64 MiB | `constants.py:65` |
| `READY_RECONFIRM_RETRY_S` / `_MAX_S` / `_MAX_ATTEMPTS` | 5.0 / 300.0 / 5 | `constants.py:151,156,163` |
| `BOOT_QUIET_WINDOW_S` | 180.0 | `constants.py:134` |
| `RX_FLOOD_BYTES_PER_10S` / `RX_RATE_WINDOW_S` | 20000 / 10.0 | `constants.py:91-93` |
| `MAX_RECOVERY_LEASE_S` / `BOOTLOADER_RX_TAIL_BYTES` / `HUMAN_ACTIVE_WINDOW_S` | 120.0 / 512 / 60.0 | `constants.py:107-111` |
| `_ATTACHED_CONSOLE_LEASE_TIMEOUT_S` | 86400.0 | `session_manager.py:66` |
| `_MIN_FILE_CHUNK_TIMEOUT_S` / `_MIN_FILE_PULL_TIMEOUT_S` / `_MIN_FILE_ECHO_TIMEOUT_S` | 5.0 / 30.0 / 5.0 | `session_manager.py:70-75` |
| `DEFAULT_CHUNK_SIZE` / `DEFAULT_ECHO_SLICE_SIZE` / `DEFAULT_ECHO_TIMEOUT_S` / `_ACK_MODES` | 512 / 64 / 5.0 / `("auto","echo","none")` | `file_transfer.py:43-60` |
| `DEFAULT_TCP_PORT` | 48700（env `SERIALWRAP_TCP_PORT`） | `constants.py:47` |
| `LOOPBACK_TCP_HOSTS` | `{"127.0.0.1","localhost","::1"}` | `constants.py:49` |
| socket 權限 | `0o660` + 可選 `SERIALWRAP_SOCKET_GROUP` chgrp | `rpc_posix.py:105-111` |
| lock 檔權限 | `0o600` | `lock_posix.py:17`、`lock_win.py:44` |
| lock 存活探測 timeout | 0.2 s | `lock_posix.py:27`、`lock_win.py:13` |
| asyncio 行長上限 | 65536 bytes（隱性預設） | `rpc_posix.py:20` |
| client 預設 RPC timeout | 5.0 s（`DEFAULT_RPC_TIMEOUT_S`） | `cli.py:81`、`client.py:128` |
| interactive lease 預設 timeout | 60.0（RPC 預設） | `service.py:843` |
| reprobe 失敗後 flasher 斷線輪詢 | `poll_s=0.5`、`max_s=1800.0` | `service.py:468-469` |
| flash probe RX buffer 上限 | 4096 bytes | `service.py:372-373` |
| human console 命令 | `timeout_s=30.0`、`priority=100`、`source=f"human:{client_id}"` | `service.py:313-322` |

`_HUMAN_INTERACTIVE_COMMANDS`（`service.py:49-68`，human console 該行以 interactive mode 送出）：`alsamixer, btop, htop, less, menuconfig, more, most, nano, nmtui, screen, tig, tmux, top, vi, view, vim, vimdiff, watch`；`_human_console_mode` 會跳過 env 賦值（`^[A-Za-z_][A-Za-z0-9_]*=.*$`）、`--`、`sudo`（帶值選項 `-u -g -h -p -C -T -r -t`）、`env`、`command`/`builtin`/`exec` 前綴，取 basename 判斷；`shlex.split` 失敗退回空白切分。

---

## 10. 規格書（`docs/rust-refactor/serialwrap-rust-spec-final.md`）與程式碼的出入

- **D-01（§9.2／ADR-08 錯誤碼集合不完整）**：spec「71 個既有字面碼」恰等於 `grep '"error_code": "X"'` 的結果（我以同法重算得 69 + `FLUSHED_BY_SHUTDOWN` 經 keyword 引數）。經非字面路徑輸出、但**不在 78 個清單**的碼：`INVALID_COMPLETION_REGEX`（常數，`command.submit` 直接回）、`TRANSFER_PROBE_FAILED`、`TARGET_DECODER_MISSING`、`TARGET_ENCODER_MISSING`（tuple 回傳後組 dict，`file.push/pull` 直接回）、`NOT_READY`（`session.attach` 的 `or "NOT_READY"`）、`SEND_FAILED`、`COMMAND_FAILED`（`command.get` 記錄內）、warning `CMD_LENGTH_WARNING`、lock 的 `SOCKET_UNAVAILABLE`。spec 自己也要求「掃描須包含常數傳遞與回應建構路徑」——現有盤點未達成。
- **D-02（§9.3 `CREDENTIALS_UNRESOLVED`「不新增為 RPC error_code」）**：實際上 `session.attach` 與 `session.recover`（REPROBE、`ok:true` 但帶 `error_code`）會把 `last_error` 原樣當 `error_code` 回，`CREDENTIALS_UNRESOLVED` 及 §8.3 全部 last_error 值都能出現在 RPC `error_code`。兩個命名空間在現行碼並未分離。
- **D-03（§4.2 `stream` 群組「RPC 串流讀取」）**：不存在 streaming RPC。`serialwrap stream tail` 只發一次 `result.tail`（無 cmd_id 的 legacy 分支，range 模式 `from_seq` 預設 0、無 metadata、無 WAL_MISSING 檢查），一請求一回應。
- **D-04（§4.2 方法名）**：spec 寫 `cmd.status`、`cmd.result_tail`；實際 RPC 名為 `command.get`、`command.result_tail`（`cmd status` 是 CLI 子命令名）。spec 未列 `result.tail`、`daemon.stop`、`session.get_state`（CLI 不呼叫，但在 retry 白名單內）、`wal.current_seq`、`event.*` 的實際 10 個方法名。
- **D-05（ADR-04「Linux UDS 使用 0600 socket 權限」）**：實作為 `0o660`，並可經 `SERIALWRAP_SOCKET_GROUP` chgrp（systemd-system 模式供 dialout 群組使用者連線）。Rust 若改 0600 會打斷 systemd-system 多使用者存取。
- **D-06（§6.2 Tier 1「Windows `LockFileEx`」）**：實作為 `msvcrt.locking(fd, LK_NBLCK, 1)`，且 Windows 是「先 TCP 探 endpoint、再取檔鎖」，POSIX 是「先 flock、再探 socket」。
- **D-07（§6.2「路徑須正規化」）**：`SingletonLock`/`WindowsSingletonLock` 皆不對 `lock_path` 做 realpath/正規化；config.yaml 寫入的 `socket_path` 也是 `--socket` 原字串。
- **D-08（§6.2／BC-04 `pids: []`、`holder_unknown: true`）**：現行 `DEVICE_STILL_HELD` 只帶 `pids`、`selector`；無 `holder_unknown`。`/proc` 不可列時 `_probe_external_holder` 回 `{"pids":[],"holder":null}` → `device.attach` 視為**可收回**（把「掃不到」當安全證明，正是 spec 禁止的推論）；self_test RELEASED 分支同樣據此給 `reclaimable:true`。此為 BC-04 待實作項，非既有契約。
- **D-09（§9.4「WAL 查詢 range、分頁、`truncated=true`」）**：`truncated` 只存在於 `log.tail_raw/tail_text`；`wal.range` 沒有 `truncated`/`returned`/`current_seq`，只有 `available_from_seq`/`rotated_out`；`result.tail` legacy 什麼 metadata 都沒有。
- **D-10（§9.4「占用」欄位 `foreign_holders`）**：名稱暗示「外部持有者」，實際只掃 serialwrapd 程序且**包含自身 pid**（健康單 daemon 恆非空；`docs/func-test/realhw-stability-checklist.md:106` 已註明）。外部工具（minicom）不會出現。
- **D-11（spec §5.4 `CMD_PENDING_MAX` 計數範圍「Accepted／Running」）**：實作以「無 `done_at`」計數，`interactive` 狀態記錄已有 `done_at`、不計；`canceled` 但仍在跑的命令（cancel 只標記不中止）也不計——pending 上限可被 cancel 繞過而讓實際在途 UART 工作超過 256。
- **D-12（§4.1「`client.py` 11 方法重試白名單」）**：一致（11 個，見 §0）。spec 稱 `session.console_list` 等為唯讀；實際 `session.list`、`health.status`、`session.console_list` 都會 prune stale console（`list_consoles` 副作用），`session.get_state`/`session.activity` 會推進 `expire_ready_reconfirm`——皆冪等，但並非零副作用。
- **D-13（ADR-04／OD-12「Windows TCP 無認證」）**：一致；另注意 POSIX 也無任何 peer-credential 檢查（純靠檔案權限）。
- **D-14（`docs/serialwrap-spec.md` §4.3「daemon 只 listen Unix socket」）**：舊規格已過時（Windows 走 TCP）；非 Rust 定稿 spec，但若作契約來源需更正。
- **D-15（§4.1「`platform_backends.py`／`rpc_posix.py`／`rpc_win.py` → 1A client；2.1 server」）**：spec 未提及 `BLOCKING_RPC_METHODS`／executor offload 模型與 64 KiB 行長隱性上限，Rust server 需明確凍結這兩項（目前只是 asyncio 預設值）。

---

## 11. 行為怪癖與風險（Rust 1:1 時需逐條裁決「複製」或「列 BC」）

- **H-01 路徑穿越**：`event.rule_delete` 的 `rule_id` 未驗證，`os.path.join(EVENTS_DIR, f"{rule_id}.json")` 可刪除任意 `*.json`（如 `../../x`），且 `event.rule_get`/`rule_delete` 以外，`CounterStore.clear(rule_id)` 同樣 join（`EVENTS_RUNTIME_DIR/<rule_id>.counter.json`）。`event.reset`、`com_disable` 也會以未驗證 rule_id 呼叫 `counters.clear`。
- **H-02 超長行斷線**：>65536 bytes 的請求行導致無回應斷線（§1.2）。
- **H-03 NaN 回送**：請求內 NaN/Infinity 被接受並可原樣出現在回應（非法 JSON）。
- **H-04 早退回應格式不一致**：`INVALID_JSON`/`INVALID_REQUEST` 用含空格的分隔字元且無 `id`。
- **H-05 human 前綴繞過 gate**：任何 client 傳 `source:"human:..."` 給 `command.submit`/`file.push`/`file.pull` 即跳過 AUTOBOOT_QUIET/READY_UNCONFIRMED gate；`command.submit` 的 human 來源在逾時時還會被升格為 interactive lease（`PROMOTE_HUMAN_INTERACTIVE`）。
- **H-06 falsy 預設**：`timeout_s:0`、`limit:0`、`priority:0`、`from_chunk:0`（無差）等全被換成預設值；`bool("false") == True` 用於 `force`／`allow_attached`。
- **H-07 self_test 對 FLASHING session 送 nonce probe**：FLASHING 不在任何早退分支，最後走 probe（bridge flash_mode 會擋非 flash 寫入，結果多半 TARGET_UNRESPONSIVE；仍是 FLASHING 期間的 TX 嘗試）。
- **H-08 負數參數**：`command.result_tail.limit<0` → EXCEPTION；`interactive_status.screen_chars<0` → 回傳截頭的 rx 文字；`log.tail_*` `limit<0`（非 0）latest 模式回空、range 模式立即 truncated=true。
- **H-09 manual recover 對 READY session**：一律送 CTRL_C（板子正常也會），成功回 `ok:true,error_code:"PROMPT_TIMEOUT_RECOVERED"` 並 **flush 該 session 所有排隊命令**；boot quiet 中則直接轉 ATTACHED 並 `_force_recover`（clear + 最多 10s 等待）。
- **H-10 `command.cancel` 不中止執行中命令**；`interactive` 狀態記錄可被 cancel 但 lease 仍在。
- **H-11 未攔截例外**：`log_start` 的 makedirs、`file.pull` 寫檔、`wal.reset`、`event.rule_set` 檔案錯誤、`_save_state` 失敗、各種 `int()/float()` 轉型 → `EXCEPTION`（無 version）；`_save_state` 失敗時記憶體狀態已變更但未持久化（release 的 spec §6.2「提交失敗不得回覆成功」在此成立——回 EXCEPTION 非 ok——但 session 已 RELEASED 於記憶體）。
- **H-12 event loop 阻塞**：§3.4 所列非 BLOCKING 卻有 I/O 的 method（特別是 `device.attach` 同步掃 `/proc`）。
- **H-13 reload 清單順序不穩定**（set 差集）。
- **H-14 `event.rule_set` 回應 `**raw` 可覆蓋 `ok`**。
- **H-15 server 綁定晚於 attach**：`service.start()` 先 spawn attach，RPC socket 之後才可連；config.yaml endpoint 再之後才寫。
- **H-16 shutdown 不關 bridge、不寫 state**；`daemon.stop` 與 SIGTERM 同路徑。
- **H-17 `health.status` `no_profiles_loaded`** 只看 explicit targets。
- **H-18 ISO 時間戳微秒為 0 時省略小數**（Python `isoformat`）；`next_reprobe_at` 是 monotonic 秒。
- **H-19 `wal.reset`／WAL 輪替同秒重名會覆蓋前一份歸檔**（`os.replace`）。
- **H-20 Windows `_build_server` 例外在 try/finally 外**：endpoint 非法時 service 已建構、lock 未顯式釋放。
