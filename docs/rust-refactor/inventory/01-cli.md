# 01 — CLI 盤點（sw_core/cli.py、sw_core/client.py、sw_core/cli_trace.py、入口）

> 基準：worktree `~/prj_pri/serialwrap-worktrees/rust-refactor`，HEAD `93553e6`，`VERSION` = `0.3.2`。
> 只讀盤點，沒有修改 repo。行號格式為 `file:line`。
> 實測（離線、不連 daemon）：`--version`、argparse 錯誤、`connect` 缺參數、連不存在的 socket 的錯誤輸出，結果見各節的「實測」標記。

---

## 0. 入口點

| 入口 | 檔案 | 內容 |
|---|---|---|
| `serialwrap`（repo 根，無副檔名） | `serialwrap:1-6` | `from sw_core.cli import main`; `raise SystemExit(main())` |
| `serialwrap.py`（薄 shim，PyInstaller entry） | `serialwrap.py:1-8` | 同上，`from sw_core.cli import main` 重新導出 |
| console_script `serialwrap` | `pyproject.toml:24-26` | `serialwrap = "sw_core.cli:main"`、`serialwrapd = "sw_core.daemon:main"` |
| `sw_core/__main__.py` | **不存在** | `python -m sw_core` 不可用；只有 `python -m sw_core.daemon`（daemon spawn fallback 用） |
| `sw_core/cli.py` 本身 | `cli.py:2723-2724` | `if __name__ == "__main__": sys.exit(main())`（package-relative import，不能當 `__main__` 直接跑） |

打包：`[tool.setuptools] packages = ["sw_core", "sw_core.event_engine", "sw_core.assets"]`（`pyproject.toml:28-29`）。**repo 根的 `serialwrapd.py` 不在套件裡**。本機實查 pipx venv 的 `site-packages/` 只有 `sw_core`、`yaml`、`_yaml`，沒有 `serialwrapd.py`（影響 §7.4）。

`main(argv: list[str] | None = None) -> int`（`cli.py:2476`）的流程：
1. `force_utf8_stdio()`（`sysenv.py:245`）：只在 `sys.platform == "win32"` 時對 stdout/stderr 呼叫 `reconfigure(encoding="utf-8")`，任何例外都吞掉；其他平台 no-op。
2. `effective_argv = list(sys.argv[1:] if argv is None else argv)`。
3. `build_parser().parse_args(effective_argv)`。遇到 `SystemExit` 時：
   - `exc.code == 2` 且 `_json_boundary_command_from_argv(argv)` 回傳 `"connect"` 或 `"benches"`：stdout 印 `{"error_code":"INVALID_ARGS","ok":false}`，stderr 印 `serialwrap: connect failed: INVALID_ARGS`，**return 1**。argparse 原本的 usage 與 error 仍會先寫到 stderr。
   - 其他情況直接 re-raise（`--help`/`--version` 為 exit 0；一般 argparse 錯誤為 exit 2，stderr 印 usage）。
4. 依 `args.cmd` 的平面 if 鏈分派（`cli.py:2491-2720`）；都沒命中時印 `{"ok":false,"error_code":"INVALID_ARGS"}` 並 return 2（`_mirror_err` 不帶 context，stderr 為 `serialwrap: failed: INVALID_ARGS`）。

實測：`serialwrap connect` 的 stdout 為 `{"error_code":"INVALID_ARGS","ok":false}`，rc=1；stderr 依序為 usage、`serialwrap connect: error: the following arguments are required: code`、`serialwrap: connect failed: INVALID_ARGS`。`serialwrap session` 的 stderr 為 usage 加上 `...required: <command>`，rc=2，stdout 為空。

### 0.1 `_json_boundary_command_from_argv(argv)`（`cli.py:2457-2473`）
- `_JSON_BOUNDARY_GLOBAL_OPTIONS = {"--socket","--endpoint","--bench","--timeout","--retries"}`（`cli.py:83-85`）
- `_JSON_BOUNDARY_COMMANDS = {"connect","benches"}`（`cli.py:86`）
- 演算法：逐 token 掃描。
  - token 的 `--opt` 或 `--opt=value` 屬於上述全域選項時：若是 `=value` 形式且 value 屬於 boundary commands，回傳 value；若下一個 token 屬於 boundary commands，**回傳下一個 token**（因此 `--socket connect` 這種寫法會被誤判，是 quirk）；否則跳過 1（`=` 形式）或 2 個 token。
  - 其他以 `-` 開頭的 token 跳過 1 個（`-v`、`-vv`、`--verbose`）。
  - 第一個非選項 token：屬於 boundary commands 就回傳，否則回傳 None。

---

## 1. 全域選項（`build_parser`，`cli.py:1908-1950`）

`ArgumentParser(prog="serialwrap", description="serialwrap client（支援本機 Unix socket 與遠端 endpoint）", epilog=examples, formatter_class=RawTextHelpFormatter)`。

| 旗標 | dest | type | default | 說明／行為 | 行 |
|---|---|---|---|---|---|
| `--version` | — | action=`version` | — | stdout 印 `serialwrap {resolve_version()}\n`，exit 0。實測輸出為 `serialwrap 0.3.2` | 1920 |
| `-v`, `--verbose` | `verbose` | action=`count` | `0` | CLI trace 層級：`-v` 為 INFO，`-vv` 為 DEBUG，優先於 `SERIALWRAP_LOG_LEVEL` | 1921 |
| `--socket` | `socket` | str | `None` | 本機 daemon socket；**`None` 是 sentinel**，表示未指定（#120），不得與預設值比對 | 1922 |
| `--endpoint` | `endpoint` | str | `None` | metavar `ENDPOINT`，優先於 `--socket` | 1923 |
| `--bench` | `bench` | str | `None` | metavar `CODE`，用 connect 記住的 endpoint；優先序低於 `--endpoint`/`--socket`，高於 config | 1924-1929 |
| `--timeout` | `timeout_s` | float | `None` | RPC socket timeout；None 時一般方法用 5.0，長操作用 45.0 | 1930-1936 |
| `--retries` | `retries` | int | `0` | 只對唯讀白名單生效 | 1937-1943 |

Subparsers：`dest="cmd", required=True, title="command groups", metavar="<group>"`（`cli.py:1945-1950`）。

**argparse 特性（Rust 需要決定是否複製）：**
- `allow_abbrev=True`（預設），長選項接受唯一前綴縮寫，例如 `--sock`、`--end`、`--time`、`--retr`、`--verb`。clap 預設不支援，需要 `infer_long_args(true)`。
- 全域選項**只能放在 group 之前**（定義在根 parser）。`session recover --timeout` 與 `session interactive-open --timeout` 是子命令自己的選項（dest 分別為 `recover_timeout_s`、`interactive_timeout_s`），與全域 `--timeout`（dest `timeout_s`）是不同的東西。例如 `serialwrap --timeout 60 session recover --selector COM0 --timeout 3`：socket timeout 60，daemon 參數 3。
- 子命令旗標的連字號會轉成底線作 dest，例如 `--device-by-id` → `device_by_id`。
- 未知參數：`error: unrecognized arguments: ...`，exit 2。

### 1.1 版本解析 `resolve_version()`（`sw_core/version.py:17-41`，cli 以 `_resolve_version` 名稱匯入，`cli.py:48`）
順序如下，第一個成功即回傳：
1. `open(<sw_core>/../VERSION).read().strip()`（`_repo_version_path()`，`version.py:13-14`）
2. `importlib.metadata.version("serialwrap")`
3. `sw_core.assets.read_text("VERSION").strip()`（PyInstaller 內嵌）
4. `"unknown"`

版本字串在 **`build_parser()` 建構時**就計算（`--version` 的 version 字串），所以每次執行 CLI 都會讀一次。

### 1.2 Client/daemon 版本不一致警告 `_warn_version_mismatch(resp)`（`cli.py:913-929`）
- 只在 `_run_rpc` 與 `_dispatch_event` 裡呼叫（daemon start/stop、setup、doctor 不呼叫）。
- `resp["version"]` 不是非空 str 時略過（舊 daemon 不帶這個欄位）。
- `cli_version in ("unknown", daemon_version)` 時略過。
- 否則 stderr 寫入：`serialwrap: client 版本 {cli_version} 與 daemon 版本 {daemon_version} 不一致（建議統一安裝來源或重啟 daemon 對齊版本；serialwrap doctor 可查其他安裝）\n`
- **不會改變 exit code**，順序在 stdout JSON 與錯誤 stderr 行之後。
- daemon 端：`SerialwrapService.rpc()` 對 dict 結果做 `setdefault("version", self._version)`（`service.py:690-703`）。`daemon.stop` 由 `daemon.py:166-169` 在進入 service.rpc 之前攔截處理，回傳 `{"ok":true,"stopping":true}`，**不帶 `version`**。

---

## 2. 輸出慣例

### 2.1 stdout JSON：`_print(obj)`（`cli.py:125-127`）
```python
sys.stdout.write(json.dumps(obj, ensure_ascii=False, sort_keys=True, separators=(",", ":")))
sys.stdout.write("\n")
```
- **sort_keys=True**（遞迴對所有巢狀 dict 排序）、緊湊分隔符、非 ASCII 原樣輸出、單行加 `\n`。
- RPC 指令會把 daemon 回應 dict **原封不動**印出，包含 daemon 補上的 `version`，以及 client 端 enrich 的 `daemon_reachable`/`daemon_busy`。
- 例外：`supervision-mode` 印**純文字**（`print(mode)`）；`skill` 印 Markdown 原文；`daemon start --foreground` 不印 JSON；`--version`/`--help` 為 argparse 文字。

### 2.2 stderr 錯誤行：`_format_err_line(resp, *, context=None)`（`cli.py:144-169`）
```
serialwrap: {context + " " if context else ""}failed: {code_str}[: {message}][（hint: {hint}）]
```
- `code_str` 取 `error_code`；沒有時用 `message` 頂替（此時不再重複附加 message）；兩者都沒有時為 `"UNKNOWN_ERROR"`。
- `message`、`hint` 有值（truthy）才附加。hint 用**全形括號**：`（hint: ...）`。
- 相容性要求：`serialwrap: {context} failed: {code}` 這段字首不可變動，下游用 substring 比對，例如 `failed: SOCKET_ERROR`。
- `_mirror_err(resp, *, context=None)`（`cli.py:172-182`）：`resp.get("ok")` 為 truthy 時 no-op，否則寫一行。
- `_run_rpc` 自己寫錯誤行，不經 `_mirror_err`，避免重複輸出（`cli.py:954`）。

實測：`serialwrap --socket /nonexistent/x.sock session list` 的 stderr 為 `serialwrap: session.list failed: SOCKET_ERROR: [Errno 2] No such file or directory`；stdout 為 `{"error_code":"SOCKET_ERROR","message":"[Errno 2] No such file or directory","ok":false}`；rc=2。（AF_UNIX connect 的 `str(OSError)` **不含 socket 路徑**；`cli.py:950` 註解說 message 帶 socket 路徑，與實際不符。）

各出口的 context 字串：

| 出口 | context |
|---|---|
| `_run_rpc` | RPC method 名，例如 `command.submit` |
| `_dispatch_event` | 真實 method 名（見 `_EVENT_CMD_METHOD`）；UNKNOWN_EVENT_CMD 時為 `event` |
| daemon start | `daemon start` |
| daemon stop | `daemon.stop`（含 systemd 重導路徑） |
| connect / benches / remote / setup | `connect` / `benches` / `remote` / `setup` |
| service | `service {action}` |
| argparse JSON 邊界 | `connect` / `benches` |
| fallthrough | 無 context：`serialwrap: failed: INVALID_ARGS` |

**沒有 stderr 鏡像的非 ok 出口**（不符合 #172 慣例，屬既有行為）：
- `daemon start` 的 `DAEMON_ALREADY_RUNNING_ELSEWHERE`（`cli.py:511-524`，只有 `_print`）
- `cmd submit` 的 `INVALID_COMPLETION_REGEX`（`cli.py:2592-2594`）
- `profile test` 的 `UNKNOWN_PROFILE`、`SAMPLE_READ_FAILED`（exit 0）
- `doctor` 的整體 `ok:false`（exit 0）

### 2.3 其他 stderr 輸出
- dangling fallback 通知（`cli.py:778-782`）：`serialwrap: config.yaml socket_path '{cfg_sock}' 不可連，依 supervision_mode={mode} 改用 '{canonical}'；如需修正請更新 config.yaml 或重跑 serialwrap setup。\n`
- RPC trace（§5）：INFO 層級時每次 RPC 寫一行 JSON。

### 2.4 Exit code 總表

| 指令／情境 | 成功 | 失敗 |
|---|---|---|
| 一般 RPC 指令（`_run_rpc`） | 0 | 2（`ok` 為 false）；endpoint 解析錯誤為 **1** |
| `event *` | 0 | 2；endpoint 解析錯誤為 1；任何其他例外（例如 `--file` 不存在或 JSON 解析失敗）為 1 + `INTERNAL_ERROR` |
| `daemon start` | 0（含 `already_running`） | 2（所有錯誤）；`--foreground` 回傳 **daemon 子程序的 exit code** |
| `daemon stop` | 0 | 2；endpoint 解析錯誤為 1；其他例外為 1 + `INTERNAL_ERROR` |
| `service <action>` | 0 | 2 |
| `setup` | 0（含 `needs_restart`） | 2（`FLASHING_BUSY`）；其他例外**不攔截**，會印 Python traceback（exit 1） |
| `doctor` | **一律 0**（`ok:false` 也是 0） | — |
| `profile test` | **一律 0**（含 `UNKNOWN_PROFILE`、`SAMPLE_READ_FAILED`） | config 解析例外不攔截 |
| `skill` | 0 | — |
| `supervision-mode` | 0 | config.yaml 壞掉時印 traceback（用的是不安全的 `_default_runtime_config()`） |
| `connect` / `benches` / `remote` | 0 | **1** |
| `cmd submit` 的 `INVALID_COMPLETION_REGEX` | — | **1**（不連 daemon） |
| argparse 錯誤 | — | 2；`connect`/`benches` 為 1 + JSON |
| `--help` / `--version` | 0 | — |
| fallthrough | — | 2 |

`service`、`supervision-mode`、`setup` 讀 mode 時用 `_default_runtime_config()`（`cli.py:311-312`），不是 `_safe_runtime_config()`，所以 config.yaml 有壞 YAML 時會 traceback。

---

## 3. RPC client（`sw_core/client.py`）

### 3.1 常數

| 名稱 | 值 | 行 |
|---|---|---|
| `RETRYABLE_READONLY_METHODS` | frozenset 11 個：`health.ping`、`health.status`、`session.list`、`session.get_state`、`session.activity`、`session.console_list`、`session.log_status`、`device.list`、`alias.list`、`mcu.patterns`、`mcu.status` | 23-35 |
| `_RETRY_BACKOFF_BASE_S` | `0.5` | 38 |
| `_RETRY_BACKOFF_MAX_S` | `5.0` | 42 |
| `_RETRYABLE_TRANSIENT_ERROR_CODES` | `{"TIMEOUT","SOCKET_ERROR","EMPTY_RESPONSE"}` | 48 |
| `_PROBE_TIMEOUT_S` | `1.0` | 52 |
| `_PROBE_DEADLINE_S` | `2.5` | 58 |
| `_PROBE_METHODS` | `("health.ping","health.status")` | 61 |

`session.console_list` 有冪等的 prune 副作用（`client.py:18-22` 註解）。`session.get_state` 在白名單內，但**沒有任何 CLI 子命令呼叫它**。

### 3.2 `_parse_endpoint(endpoint) -> tuple[str, tuple[str,int] | str]`（`client.py:76-114`）
- 字串含 `://` 時用 `urllib.parse.urlsplit` 解析：
  - `tcp`：`parsed.port`（ValueError → `invalid tcp endpoint: {endpoint!r}`）；`host = parsed.hostname`（會**轉小寫**，IPv6 去掉方括號）。有 path/query/fragment、無 host 或無 port 時 raise `ValueError("invalid tcp endpoint: ...")`，否則回傳 `("tcp", (host, port))`。注意 `tcp://h:1/` 的 path 是 `/`，會被拒絕。
  - `unix`：有 netloc 時拒絕（`unix endpoint must use an absolute path such as 'unix:///path/to/sock'`）；有 query/fragment、無 path 或 path 不以 `/` 開頭時拒絕（`unix endpoint path must be absolute`）；否則回傳 `("unix", path)`。
  - 其他 scheme：`ValueError("unsupported endpoint scheme: 'x'")`。
- 不含 `://`：視為裸路徑，回傳 `("unix", endpoint)`（不驗證是否為絕對路徑，空字串也接受）。

`_af_unix_available()`（`client.py:117-119`）：`hasattr(socket, "AF_UNIX")`。

### 3.3 `rpc_call(socket_path, method, params, *, req_id=1, timeout_s=5.0, retries=0, trace_sink=None) -> dict`（`client.py:122-191`）
- `attempts = 1 + max(0, int(retries))`，只在 `method in RETRYABLE_READONLY_METHODS` 時套用，其他方法一律 1 次。
- 迴圈：呼叫 `_rpc_call_once`。`resp.ok` 為 true 或 `error_code ∉ transient` 時，`retry_count = attempt` 並 break；否則若還有下一次，`retry_count = attempt+1`，`sleep(min(delay, 5.0))`，`delay *= 2`。退避序列為 0.5, 1, 2, 4, 5, 5, …
- 最終 `ok` 為 false、`error_code == "TIMEOUT"` 且 method 不在 `_PROBE_METHODS` 時，`resp.update(_probe_daemon_after_timeout(socket_path))`。
- 有 `trace_sink` 時呼叫 `trace_sink({"elapsed_ms": int>=0, "retry_count": int, "errno": int|None, "errno_name": errno.errorcode.get(errno)|None})`，例外吞掉。`errno` 取**最後一次主嘗試**的 errno（探測不算）。
- 最壞耗時 ≈ `(N+1)×timeout_s + Σ min(0.5×2^i, 5)`。

### 3.4 `_probe_daemon_after_timeout(endpoint)`（`client.py:194-223`）
- `deadline = monotonic() + 2.5`。
- `ping = _rpc_call_once(endpoint, "health.ping", {}, req_id=0, timeout_s=1.0)`，結果寫入 `info["daemon_reachable"] = bool(ping.ok)`；false 時直接回傳。
- 已超過 deadline 時直接回傳。
- `status = _rpc_call_once(endpoint, "health.status", {}, req_id=0, timeout_s=1.0)`；ok 時，`commands`、`sessions` 中**型別為 int** 的值放進 `info["daemon_busy"] = {...}`（非空才加）。
- 附加欄位：`daemon_reachable: bool`（一定有），`daemon_busy: {commands?: int, sessions?: int}`（可選）。

### 3.5 `_rpc_call_once(socket_path, method, params, *, req_id=1, timeout_s=5.0)`（`client.py:226-278`）
- 每次呼叫先重設 thread-local `last_attempt_errno`。
- 解析 endpoint 失敗時回傳 `{"ok":False,"error_code":"INVALID_ENDPOINT","message":str(exc)}`。
- tcp：`socket.create_connection(address, timeout=timeout_s)` 後 `settimeout(timeout_s)`。
- unix：沒有 AF_UNIX 時回傳 `{"ok":False,"error_code":"SOCKET_ERROR","message":"本平台無 AF_UNIX，無法連接 unix endpoint {path!r}（請改用 tcp://127.0.0.1:<port>，#131）"}`；否則 `socket(AF_UNIX, SOCK_STREAM)`、`settimeout`、`connect`。
- connect 階段：`socket.timeout` 回傳 `{"ok":False,"error_code":"TIMEOUT"}`（**沒有 message**）；`OSError` 時記下 errno，回傳 `{"ok":False,"error_code":"SOCKET_ERROR","message":str(exc)}`。
- 請求：`json.dumps({"id":req_id,"method":method,"params":params}, ensure_ascii=False, separators=(",",":")).encode("utf-8") + b"\n"`（**沒有 sort_keys**，key 順序固定為 id、method、params），用 `sendall` 送出。
- 回應：`recv(65536)` 直到收到 `\n` 或 EOF；取第一個 `\n` 之前的內容並 `.strip()`；空的時候回傳 `{"ok":False,"error_code":"EMPTY_RESPONSE"}`；`json.loads(line.decode("utf-8", errors="replace"))`；不是 dict 時回傳 `{"ok":False,"error_code":"INVALID_RESPONSE"}`；是 dict 則**原樣回傳**（不檢查 `id`）。
- 收發階段：`socket.timeout` 回傳 TIMEOUT；`OSError` 回傳 SOCKET_ERROR 並帶 message。
- **`json.JSONDecodeError`（ValueError）沒有被攔截**，會穿出 `rpc_call` 與 `_run_rpc` 變成 traceback（exit 1）。這是 Python 既有缺陷，Rust 應改回傳 `INVALID_RESPONSE` 或類似錯誤，需要定義。
- 每次呼叫都開新連線，`finally: sock.close()`。

---

## 4. Endpoint 解析（#108 / #120 / #131 / #173）

### 4.1 常數（`sw_core/constants.py`，**import 時計算**）

| 名稱 | 計算方式 |
|---|---|
| `_CONFIG_HOME` | `$XDG_CONFIG_HOME` 或 `~/.config` |
| `_STATE_HOME` | `$XDG_STATE_HOME` 或 `~/.local/state` |
| `_RUNTIME_HOME` | `$XDG_RUNTIME_DIR`（可為 None） |
| `CONFIG_DIR` | `_env_path("SERIALWRAP_CONFIG_DIR", _CONFIG_HOME/serialwrap)`（空白值視同未設，並做 expanduser） |
| `STATE_DIR` | `_env_path("SERIALWRAP_STATE_DIR", _STATE_HOME/serialwrap)` |
| `RUN_DIR` | `_env_path("SERIALWRAP_RUN_DIR", _run_dir_default())`；default：`SERIALWRAP_STATE_DIR` 非空時為 STATE_DIR，否則有 XDG_RUNTIME_DIR 時為 `$XDG_RUNTIME_DIR/serialwrap`，否則為 `STATE_DIR/run` |
| `LOCK_PATH` | `RUN_DIR/serialwrapd.lock` |
| `SOCKET_PATH` | `RUN_DIR/serialwrapd.sock` |
| `DEFAULT_TCP_PORT` | `int($SERIALWRAP_TCP_PORT or "48700")` |
| `LOOPBACK_TCP_HOSTS` | `frozenset({"127.0.0.1","localhost","::1"})` |
| `DEFAULT_ENDPOINT` | Windows（`os.name=="nt"` 或 `sys.platform` 以 win 開頭）：`$SERIALWRAP_ENDPOINT` 或 `tcp://127.0.0.1:{DEFAULT_TCP_PORT}`；其他平台：`$SERIALWRAP_ENDPOINT` 或 `SOCKET_PATH` |
| `PROFILE_DIR` | `_env_path("SERIALWRAP_PROFILE_DIR", CONFIG_DIR/profiles)` |
| `SYSTEM_SOCKET`（`setup_cmd.py:263`） | `"/run/serialwrap/serialwrapd.sock"` |
| `SYSTEM_PROFILE_DIR`（`setup_cmd.py:264`） | `"/etc/serialwrap/profiles"` |

`config.yaml` 路徑：`os.path.join(CONFIG_DIR, "config.yaml")`（`cli.py:312`）。`RuntimeConfig`（`runtime_config.py`）用 `yaml.safe_load` 讀；內容不是 dict 時視為 `{}`；`mode()` 回傳 `data.get("supervision_mode")`；`socket_path()` 回傳 `data.get("socket_path")`。不存在的檔案視為 `{}`。YAML 語法錯誤時 **raise**，`_safe_runtime_config` 會攔住並回傳 None。

daemon 啟動成功後會**無條件**把 `--socket` 值寫回 config.yaml 的 `socket_path`（`daemon.py:90-100`，`_write_effective_endpoint`，#173）。CLI 讀取的 `socket_path` 因此來自 daemon。

### 4.2 `_rpc_backend_is_win()`（`cli.py:660-670`）
`select_rpc_backend() == "win"`（`platform_backends.py`：`SERIALWRAP_RPC_BACKEND` 為 `posix|unix|termios` 時為 posix；為 `win|windows|win32` 時為 win；`auto` 或未設時依 `os.name=="nt" or sys.platform.startswith("win")` 判定；其他值 raise ValueError）。ValueError 時 CLI 退回只看實際平台。

### 4.3 `_local_default_endpoint()`（`cli.py:673-687`）
- win backend：`DEFAULT_ENDPOINT` 以 `tcp://` 開頭時用它，否則用 `f"tcp://127.0.0.1:{DEFAULT_TCP_PORT}"`（在 POSIX 上以 env 模擬 win 的情況）。
- 其他：`SOCKET_PATH`（**不是** `DEFAULT_ENDPOINT`，因此 POSIX CLI 不讀 `SERIALWRAP_ENDPOINT`）。

### 4.4 `_endpoint_alive(ep)`（`cli.py:690-724`）
- 空字串時回傳 True；解析失敗時回傳 True。
- transport 不是 unix 的情況：tcp 且 win backend 時用 `lock_win._endpoint_alive(ep)`（`create_connection(timeout=0.2)`，`lock_win.py:8-17`）；其他一律 True（POSIX 的 tcp 不探測，可能是 ssh tunnel）。
- unix：沒有 AF_UNIX 時回傳 False；否則 `connect` 超時 0.2s，成功為 True，失敗為 False。

### 4.5 `_resolve_endpoint_info(args) -> _ResolvedEndpoint(endpoint, source)`（`cli.py:727-784`）
`_ResolvedEndpoint` 是 frozen dataclass `(endpoint: str, source: str)`（`cli.py:112-115`）。優先序：
1. `args.endpoint` 為 truthy 時用 `(ep, "--endpoint")`。
2. `args.socket is not None` 時用 `(args.socket, "--socket")`。空字串也算明確指定。
3. `args.bench` 為 truthy 時：讀 `_bench_state_path()`，經 `_load_bench_state_doc` 與 `_remembered_bench_endpoint` 處理。
   - ValueError 時拋 `_EndpointResolutionError("INVALID_BENCH_STATE", str)`
   - OSError（含 JSON 以外的 IO 錯誤）時拋 `"BENCH_STATE_IO_ERROR"`
   - json 解析錯誤屬 ValueError，歸入 INVALID_BENCH_STATE
   - 沒有記錄時拋 `"BENCH_ENDPOINT_NOT_REMEMBERED"`，message 為 `bench "{code}" 尚未記住 endpoint；請先執行 serialwrap connect {code}`
   - 成功時用 `(endpoint, "--bench")`
4. `rc = _safe_runtime_config()`；`cfg_sock = rc.socket_path()`，例外時為 None。
5. `chosen = cfg_sock or _local_default_endpoint()`。
6. **dangling fallback**：`cfg_sock` 為 truthy 且 `not _endpoint_alive(cfg_sock)` 時，取 `mode = rc.mode() or "on-demand"`（例外時用 on-demand）；`canonical = SYSTEM_SOCKET if mode=="systemd-system" else _local_default_endpoint()`。若 `canonical != cfg_sock` 且 `_endpoint_alive(canonical)`，stderr 印通知並回傳 `(canonical, "config.yaml -> canonical fallback")`。
7. 回傳 `(chosen, "config.yaml" if cfg_sock else "default")`。
- CLI **不會改寫** config.yaml。

`_EndpointResolutionError(code, message)`（`cli.py:118-122`）；`_print_endpoint_resolution_error(exc, *, context)`（`cli.py:130-134`）：stdout 印 `{"error_code":code,"message":msg,"ok":false}`，stderr 鏡像，**return 1**。

`_resolve_local_endpoint_info(args)`（`cli.py:787-795`）：去掉 `bench` 後走同一套解析，供 daemon start 與 setup 這類本機操作使用。
`_resolve_endpoint(args) -> str`（`cli.py:798-799`）。
`_resolve_default_endpoint_with_source() -> (endpoint, label)`（`cli.py:810-835`，doctor 用）。label 為 `"config.yaml"`、`"config.yaml（記錄 {cfg} 不可連，#108 dangling fallback 改用 canonical）"` 或 `"預設（SOCKET_PATH，受 SERIALWRAP_STATE_DIR/XDG 環境變數影響）"`。

### 4.6 Timeout 解析 `_effective_timeout_s(args, method)`（`cli.py:892-910`）
- `args.timeout_s` 不為 None 時用 `float(args.timeout_s)`。
- method 在 `_LONG_RPC_METHODS = {"session.recover","session.attach","session.self_test","session.console_attach"}`（`cli.py:91-96`）時用 `LONG_RPC_TIMEOUT_FLOOR_S = 45.0`（`cli.py:79`）。
- 其他用 `DEFAULT_RPC_TIMEOUT_S = 5.0`（`cli.py:81`）。
- `file.push`/`file.pull` 被明確列為已知缺口：仍是 5s，大檔幾乎必定假性逾時（`cli.py:98-103`）。

**固定 timeout、不讀 `--timeout`/`--retries` 的呼叫**：daemon start 的探測與輪詢（ping 0.5、status 1.0）、daemon stop（`daemon.stop` 2.0）、setup 的探測（ping 0.5、mcu.status 0.5）、doctor 內部（health.status 0.5）、remote 的 `real_ping`（2.0）。

---

## 5. CLI trace（`sw_core/cli_trace.py`）

- `TRACE_LOGGER_NAME = "serialwrap.cli_trace"`、`_TRACE_HANDLER_ATTR = "_serialwrap_cli_trace_handler"`、`_MAX_NUMERIC_LOG_LEVEL_DIGITS = 4300`（`cli_trace.py:14-16`）。
- `resolve_cli_trace_level(verbose, env=None)`（`48-56`）：`verbose>=2` 為 DEBUG(10)；`>=1` 為 INFO(20)；否則依 `SERIALWRAP_LOG_LEVEL` 解析，無效或未設時為 WARNING(30)。
- `_parse_log_level(raw)`（`19-45`）：None 或空白回傳 None；`raw.lstrip("-")` 全為數字且長度 ≤4300 時用 `int(value)`（ValueError 時為 None）；否則名稱不分大小寫，接受 `CRITICAL/FATAL(50)/ERROR(40)/WARNING/WARN(30)/INFO(20)/DEBUG(10)/NOTSET(0)`；其他回傳 None。
- `configure_cli_trace_logger(verbose, *, stream=None, env=None)`（`59-91`）：`propagate=False`、設定 level、維持單一自有 StreamHandler（formatter `%(message)s`），目標為 stderr。stream 更換失敗時重建 handler。
- 只在 `logger.isEnabledFor(INFO)` 時發出（`emit_rpc_trace`，`117-157`），stderr 每次 RPC 一行 JSON，**沒有 sort_keys**，key 依插入順序：
  ```
  {"endpoint_transport","endpoint_id","endpoint_source","method","elapsed_ms","error_code","errno","errno_name","retry_count","timeout_s"}
  ```
  - `endpoint_transport`：`unix|tcp|unknown`
  - `endpoint_id`（`describe_trace_endpoint`，`104-114`）：unix 為路徑的 `sha256(path).hexdigest()`；tcp 且 host 屬 loopback 時為 `host:port`（IPv6 寫成 `[::1]:port`）；非 loopback tcp 為 `sha256("tcp://{host}:{port}")`；解析失敗為 `sha256(raw)`。編碼為 utf-8 + surrogateescape。
  - `endpoint_source`：`--endpoint|--socket|--bench|config.yaml|default|config.yaml -> canonical fallback`；daemon start spawn 輪詢時為 `--socket|--endpoint|default`。
  - `error_code`：ok 時為 null，否則 `str(error_code)`；`errno`/`errno_name` 為 int/str 或 null（errno 為 None 時 errno_name 一律 null）；`retry_count` 為 int，預設 0；`elapsed_ms` 為 int，預設 0；`timeout_s` 為 float。
- 實測：`-v --socket /nonexistent/x.sock --retries 1 session list` 會輸出 `{"endpoint_transport":"unix","endpoint_id":"534a41…","endpoint_source":"--socket","method":"session.list","elapsed_ms":500,"error_code":"SOCKET_ERROR","errno":2,"errno_name":"ENOENT","retry_count":1,"timeout_s":5.0}`。
- 會產生 trace 的呼叫：`_run_rpc`、event、daemon stop、daemon start（冪等探測透過 thread-local `_DAEMON_START_PROBE_TRACE` context，以及 spawn 輪詢）、setup 探測。**不產生 trace**：doctor 內部 rpc_call、remote_tunnel 的 `real_ping`。
- `_call_rpc_endpoint(endpoint, method, params, *, timeout_s, retries=None, trace_sink=None)`（`cli.py:842-856`）、`_rpc_call_traced(args, resolved, method, params, *, timeout_s, retries=None)`（`cli.py:859-889`）。

---

## 6. 子命令完整清單（20 個頂層 group，依 parser 註冊順序）

註冊順序：daemon、device、session、alias、cmd、stream、log、file、wal、mcu、remote、connect、benches、event、supervision-mode、service、setup、doctor、profile、skill（`cli.py:1952-2452`）。

表中「RPC」欄為實際送出的 method 與 params；「重試」欄表示是否在 retry 白名單內；「長」欄表示是否套用 45s floor。所有 `_run_rpc` 指令的 exit code 為 0/2，endpoint 解析錯誤為 1。

### 6.1 `daemon`（`cli.py:1952-1989`；本機）
子 parser：`dest="daemon_cmd", required=True, metavar="<command>"`。

| 子命令 | 參數 | 行為 |
|---|---|---|
| `start` | `--profile-dir`（default `PROFILE_DIR`）、`--lock`（default `LOCK_PATH`）、`--foreground`（store_true）、`--with-sudo`（dest `with_sudo`，store_true，False）、`--force-spawn`（dest `force_spawn`，store_true，False） | `_run_daemon_start`，見 §7 |
| `stop` | `--with-sudo`（dest `with_sudo`，False） | `_run_daemon_stop`，見 §7.6 |
| `status` | 無 | `_run_rpc(args,"health.status",{})`；重試：是；timeout 預設 5 |

**沒有 `restart` 子命令。**

### 6.2 `device`（`cli.py:1991-2010`）

| 子命令 | 參數 | RPC 與 params | 重試 | 長 |
|---|---|---|---|---|
| `list` | — | `device.list` `{}` | ✔ | — |
| `release` | `--selector`（必填）、`--source`（default `"cli"`）、`--reason`（default None） | `device.release` `{"selector","source","reason"}`；reason 為 None 時送 JSON null | — | — |
| `attach` | `--selector`（必填）、`--force`（store_true） | `device.attach` `{"selector","force":bool}` | — | — |

### 6.3 `session`（`cli.py:2012-2080`）
`--selector` 的 help 多為 `session_id | COMx | alias`；pin/unpin 為 `session_id | COMx | alias | by-id | by-path`。

| 子命令 | 參數（名稱／type／default／必填） | RPC 與 params | 重試 | 長 |
|---|---|---|---|---|
| `list` | — | `session.list` `{}` | ✔ | — |
| `clear` | `--selector` 必填 | `session.clear` `{"selector"}` | — | — |
| `bind` | `--selector` 必填；`--device-by-id` 必填 | `session.bind` `{"selector","device_by_id"}` | — | — |
| `pin` | `--selector` 必填；`--profile` 必填 | `session.pin` `{"selector","profile"}` | — | — |
| `unpin` | `--selector` 必填 | `session.unpin` `{"selector"}` | — | — |
| `attach` | `--selector` 必填 | `session.attach` `{"selector"}` | — | ✔ 45s |
| `self-test` | `--selector` 必填；`--probe-timeout`（dest `probe_timeout_s`，float，2.0）；`--strict-human-lock`（store_true，False） | `session.self_test` `{"selector","timeout_s":probe_timeout_s,"strict_human_lock":bool}` | — | ✔ |
| `activity` | `--selector` 必填 | `session.activity` `{"selector"}` | ✔ | — |
| `recover` | `--selector` 必填；`--timeout`（dest `recover_timeout_s`，float，2.0）；`--force`（store_true） | `session.recover` `{"selector","timeout_s":recover_timeout_s,"force":bool}` | — | ✔ |
| `console-attach` | `--selector` 必填；`--label`（default None） | `session.console_attach` `{"selector"}`，`label` 為 truthy 時才帶 | — | ✔ |
| `console-detach` | `--selector` 必填；`--client-id` 必填 | `session.console_detach` `{"selector","client_id"}` | — | — |
| `console-list` | `--selector` 必填 | `session.console_list` `{"selector"}` | ✔ | — |
| `interactive-open` | `--selector` 必填；`--owner`（default `"agent"`）；`--timeout`（dest `interactive_timeout_s`，float，60.0）；`--command`（default `""`）；`--allow-attached`（store_true，False） | `session.interactive_open` `{"selector","owner","timeout_s":interactive_timeout_s,"command","allow_attached":bool}` | — | — |
| `interactive-send` | `--interactive-id` 必填；`--data` 必填；`--encoding`（default `"plain"`） | `session.interactive_send` `{"interactive_id","data","encoding"}` | — | — |
| `interactive-status` | `--interactive-id` 必填；`--screen-chars`（int，2048） | `session.interactive_status` `{"interactive_id","screen_chars"}`（會 touch lease，`session_manager.py:4253-4259`） | — | — |
| `interactive-close` | `--interactive-id` 必填 | `session.interactive_close` `{"interactive_id"}` | — | — |
| `log-start` | `--selector` 必填 | `session.log_start` `{"selector"}` | — | — |
| `log-stop` | `--selector` 必填 | `session.log_stop` `{"selector"}` | — | — |
| `log-status` | `--selector` 必填 | `session.log_status` `{"selector"}` | ✔ | — |

`console-attach` 與 `console-detach` 在 Python CLI 中**只是 RPC 呼叫**，沒有本地終端橋接。終端橋接由 `sw_core/assets/tools/minicom_router.sh` 完成：wrapper 呼叫 `SW session console-attach --selector X --label "minicom:$$"`，再用 jq 取 `.ok`、`.client_id`、`.vtty`（`minicom_router.sh:332-351`），接著讓 minicom 開 `.vtty`。結束時呼叫 `session console-detach`（`:357`）。wrapper 還用到 `supervision-mode`（`:252`）、`daemon start`（`:258`）、`session list`（讀 `.sessions[].{alias,com,session_id,state,last_error,reprobe_attempts,reprobe_exhausted}`）與 `session attach`。`SERIALWRAP_SOCKET` env 設定時 wrapper 會加 `--socket`（`:17-22`）；`SERIALWRAP_BIN` 預設為 `${BASE_DIR}/serialwrap`。

### 6.4 `alias`（`cli.py:2082-2097`）

| 子命令 | 參數 | RPC | 重試 |
|---|---|---|---|
| `list` | — | `alias.list` `{}` | ✔ |
| `set` | `--session-id` 必填；`--alias` 必填 | `alias.set` `{"session_id","alias"}` | — |
| `assign` | `--by-id` 必填；`--alias` 必填；`--profile`（None） | `alias.assign` `{"by_id","alias"}`，`profile` 為 truthy 時才帶 | — |
| `unassign` | `--alias` 必填 | `alias.unassign` `{"alias"}` | — |

### 6.5 `cmd`（`cli.py:2099-2142`）

| 子命令 | 參數 | RPC 與 params |
|---|---|---|
| `submit` | `--selector` 必填；`--cmd`（dest `command_text`，default `""`，**非必填**）；`--source`（`"agent"`）；`--mode`（`"line"`，**沒有 choices，任意字串都轉送**）；`--priority`（int，10）；`--cmd-timeout`（dest `cmd_timeout_s`，float，10.0）；`--expected-duration`（dest `expected_duration_s`，float，None）；`--completion-regex`（None） | 先在本地做 `valid_completion_regex(args.completion_regex)`，失敗時 stdout 印 `{"error_code":"INVALID_COMPLETION_REGEX","ok":false}`，**exit 1**，不連 daemon、不寫 stderr。通過後送 `command.submit` `{"selector","cmd":command_text,"source","mode","priority","timeout_s":cmd_timeout_s}`，`expected_duration_s`、`completion_regex` 不為 None 時才帶 |
| `status` | `--cmd-id` 必填 | `command.get` `{"cmd_id"}` |
| `result-tail` | `--cmd-id` 必填；`--from-chunk`（int，0）；`--limit`（int，200） | `command.result_tail` `{"cmd_id","from_chunk","limit"}` |
| `cancel` | `--cmd-id` 必填 | `command.cancel` `{"cmd_id"}` |

`submit` 的 description 引用常數：`CMD_WARN_BYTES = 4096`、`CMD_REJECT_BYTES = 16384`（`arbiter.py:14-15`）。超過 4096 時 daemon 附 `CMD_LENGTH_WARNING`，超過 16384 時回 `CMD_TOO_LONG`，含 `\n` 時回 `CMD_CONTAINS_NEWLINE`。這些檢查**都在 daemon 端**，CLI 不檢查長度。

`valid_completion_regex`（`command_completion.py:7-23`）：`COMPLETION_REGEX_MAX_BYTES = 1024`。None 為 True；非 str 或空字串為 False；UTF-8 長度 >1024 為 False；`re.compile` 失敗為 False；`compiled.search("")` 能匹配（可匹配空字串）為 False；`re.error`、`UnicodeEncodeError`、`RecursionError` 為 False。**語意綁定 Python `re`**，Rust 移植需要 fancy-regex/pcre2 等相容引擎，否則會改變可接受集合。

### 6.6 `stream`（`cli.py:2144-2154`）
`tail`：`--selector`（None）、`--com`（None）、`--from-seq`（int，**0**）、`--limit`（int，200）。
RPC：`result.tail` `{"from_seq","limit"}`；`selector = args.selector or args.com`，為 truthy 時才帶 `"selector"`。

### 6.7 `log`（`cli.py:2156-2181`）
`tail-raw` 與 `tail-text`：`--selector`、`--com`、`--from-seq`（int，**None**）、`--limit`（int，200）。
params：`{"limit"}`；`from_seq` 不為 None 時才帶（含 0），省略時 daemon 走 latest 模式（#124）；`selector`（`selector or com`）為 truthy 時才帶。
RPC：`log.tail_raw` / `log.tail_text`。

### 6.8 `file`（`cli.py:2183-2210`）

| 子命令 | 參數 | RPC 與 params |
|---|---|---|
| `push` | `--selector` 必填；`--local` 必填；`--remote` 必填；`--chunk-size`（dest `chunk_size`，int，`DEFAULT_CHUNK_SIZE`=512，`file_transfer.py:43`）；`--chunk-timeout`（dest `chunk_timeout_s`，float，None）；`--ack-mode`（choices `auto|echo|none`，default `auto`）；`--source`（`"agent"`） | `file.push` `{"selector","local_path","remote_path","chunk_size","ack_mode","source"}`，`chunk_timeout_s` 不為 None 時才帶。**本地檔案由 daemon 讀**（`local_path` 原樣傳給 daemon，CLI 不讀檔、不轉絕對路徑） |
| `pull` | `--selector` 必填；`--remote` 必填；`--local`（None）；`--chunk-timeout`（None）；`--ack-mode`（同上）；`--source`（`"agent"`） | `file.pull` `{"selector","remote_path","ack_mode","source"}`，`local_path`、`chunk_timeout_s` 不為 None 時才帶 |

file.* 的 timeout 預設 5s（已知缺口），且不在重試白名單。

### 6.9 `wal`（`cli.py:2212-2223`）

| 子命令 | 參數 | RPC |
|---|---|---|
| `export` | `--from-seq`（int，0）、`--to-seq`（int，0）、`--limit`（int，1000） | `wal.range` `{"from_seq","to_seq","limit"}` |
| `reset` | — | `wal.reset` `{}` |
| `current-seq` | — | `wal.current_seq` `{}` |

### 6.10 `mcu`（`cli.py:2225-2232`）
`patterns` 送 `mcu.patterns` `{}`（✔ 重試）；`status` 送 `mcu.status` `{}`（✔ 重試）。daemon 回應欄位：`candidates`、`flashing`（bool）、`flashing_com`、`last_detect`（`service.py:716-724`）。

### 6.11 `remote`（`cli.py:2234-2279`；本機；`_run_remote` 在 `cli.py:1532-1618`）
參數：
- `words`：nargs=`*`，形式為 `[user@]host:port`、`status` 或 `close <port|all>`
- `-R`：dest `reverse`，store_true
- `-L`：dest `forward`，store_true。**刻意不用** mutually_exclusive_group，以維持 JSON 邊界
- `--autossh`：store_true
- `--local`：int，None
- `--remote-socket`：dest `remote_socket`，None
- `--ready-timeout`：dest `ready_timeout`，float，10.0
- `--ssh-opt`：dest `ssh_opt`，action=append，default `[]`

行為：
1. `os.name=="nt"` 時回傳 `{"ok":false,"error_code":"REMOTE_NOT_SUPPORTED","message":"native Windows 本期不支援 serialwrap remote；請手動 ssh -R（見 SKILL_WINDOWS.md）"}`，exit 1。這一步在 import remote_tunnel（硬依賴 fcntl）**之前**。
2. `run_dir = _remote_run_dir()`：每次呼叫時讀 `$SERIALWRAP_RUN_DIR`（strip 非空時 expanduser），否則用 `constants.RUN_DIR`。
3. `words` 為空或 `words[0]=="status"` 時印 `rt.status(run_dir)`，**exit 0**。
4. `words[0]=="close"` 時 selector 取 `words[1]`，否則為 `"all"`，印 `rt.close(run_dir, selector)`，exit 0。
5. 其他情況（open）依序處理：
   - `rt.guard_platform()`
   - `-R` 與 `-L` 同時給：`TunnelError("INVALID_ARGS","-R 與 -L 不可同時指定")`
   - `role = "connect" if forward else "expose"`
   - expose 帶 `--local`：`INVALID_ARGS "--local 僅用於 -L（connect）"`
   - `--local` 超出 1..65535：`INVALID_ARGS "--local 超出範圍：{n}"`
   - `rt.parse_target(words[0])`（`_TARGET_RE = ^(?:(?P<user>[^@:]+)@)?(?P<host>[^@:]+):(?P<port>\d+)$`，不符或 port 超出範圍時為 `INVALID_TARGET`）
   - `rt.resolve_ssh_bin("autossh"|"ssh")`（`shutil.which`，找不到時為 `SSH_NOT_FOUND`）
   - expose 時 `forward_src = _forward_src_from_endpoint(_resolve_endpoint(args))`（unix 為原路徑；tcp 為 `127.0.0.1:{port}`；ValueError 時為 `INVALID_ENDPOINT`；`--bench` 解析錯誤走 `_EndpointResolutionError`，exit 1）
   - 組 `TunnelSpec(role, ssh_target, port, local=args.local, forward_src, remote_socket, via, ssh_opts=tuple(ssh_opt), ready_timeout)`
   - `rt.open_tunnel(spec, run_dir, spawner=rt.real_spawner, runner=rt.make_runner(), ping=rt.real_ping)`，印結果，exit 0
6. `TunnelError` 時回傳 `{"ok":false,"error_code":exc.code,"message":exc.message or exc.code}`，exit 1；`_EndpointResolutionError` 時 exit 1；其他 `Exception` 為 `INTERNAL_ERROR`，exit 1。

remote_tunnel 對外輸出形狀（`remote_tunnel.py`）：
- `status`（`500-530`）：`{"ok":true,"tunnels":[{status,role,pid,listen_port,target,remote_bind,forward_target,via,endpoint,identity,remote_hint?,"alive":true} | {"status":"orphan","control_path":...,"alive":true}]}`。**會就地 prune 死掉的 state**，屬寫入副作用。
- `close`（`541-568`）：`{"ok":true,"closed":[port,...]}`；找不到時為冪等的空 list；拆除失敗時把 state 標成 `status="error"` 並保留。
- `open_tunnel`（`406-497`）：`{"ok":true, ..._public(state)}`，status 為 `active` 或 `starting`；冪等時多帶 `"already_running":true`。不同 identity 回 `TUNNEL_CONFLICT`；行程死亡回 `TUNNEL_SPAWN_FAILED`（message 為 stderr 尾端 ≤500 字元）；遠端 bind 驗證失敗回 `REMOTE_BIND_UNVERIFIED`。
- `_public`（`571-581`）的 keys：`status, role, pid, listen_port, target, remote_bind, forward_target, via, endpoint, identity`；expose 另加 `remote_hint = "agent 端用 serialwrap --endpoint {endpoint}"`。
- ssh argv：`[ssh | autossh -M 0] -N -T -o BatchMode=yes -o ExitOnForwardFailure=yes -o ControlMaster=auto -o ControlPath={run_dir}/remote/cm-{port} -o ControlPersist=no -o ServerAliveInterval=30 -o ServerAliveCountMax=3 {ssh_opts...} (-R {127.0.0.1:{port}|remote_socket}:{forward_src} | -L 127.0.0.1:{local}:{remote_socket|localhost:{port}}) {ssh_target}`（`82-115`）。
- Registry 存放在 `<run_dir>/remote/`：`{port}.json`（sort_keys 緊湊格式，原子寫入 `.tmp.{pid}`）、`cm-{port}`、`{port}.log`、`.registry.lock`（0600 flock）。
- identity 為 `sha256("|".join(role,ssh_target,port,local,forward_src,remote_socket,via,";".join(ssh_opts)))`，None 寫成 `"None"`。
- readiness：每 0.2s 輪詢；`ssh -O check` 加 role probe（connect 用 `health.ping` 2.0s；expose 帶 unix remote_socket 時直接為 True；expose tcp 時透過 `ssh ... ss -ltnH "sport = :{port}"` 驗證 bind 只在 loopback）。`make_runner` 的 subprocess timeout 為 10.0s，逾時回 rc 124。

### 6.12 `connect`（`cli.py:2282-2292`；本機；`_run_connect` 在 `cli.py:1333-1459`）
參數：`code`（positional，必填）、`--close`（store_true）。argparse 錯誤時輸出 JSON 邊界格式（INVALID_ARGS，exit 1）。
流程：
1. `os.name=="nt"` 時回 `REMOTE_NOT_SUPPORTED`，message 為 `"native Windows 本期不支援 serialwrap connect；請手動 ssh -L（見 SKILL_WINDOWS.md）"`，exit 1。
2. `rt.guard_platform()`；`bench_registry.resolve(code)`：KeyError 為 `UNKNOWN_BENCH_CODE`（message `unknown bench code: {code}`）；ValueError 為 `INVALID_BENCH_CONFIG`。YAML 錯誤與 OSError **不轉換**，落入 catch-all 成為 `INTERNAL_ERROR`。
3. `via = "autossh" if entry.autossh else "ssh"`；`spec = TunnelSpec(role="connect", ssh_target=entry.target, port=entry.local_port, remote_socket=entry.remote_socket, via, ssh_opts=entry.ssh_opts)`（local=None，ready_timeout=10.0）。
4. `--close`：
   - 讀 bench state，取記住的 port，否則用 `entry.local_port`。錯誤為 `INVALID_BENCH_STATE` 或 `BENCH_STATE_IO_ERROR`。
   - `_ensure_connect_close_target_matches`：同 port 上非 orphan 的 tunnel，其 role 與 identity 不符時回 `TUNNEL_CONFLICT`，message `local_port={p} 已有不相符的 stateful tunnel；僅可關閉相符的 connect tunnel`。
   - 執行 `rt.close(run_dir, str(port))`。
   - close 成功但 tunnel 仍存在時回 `{"ok":false,"error_code":"TUNNEL_STILL_ACTIVE","message":"local_port={p} 的 tunnel 仍存在，未清除 endpoint 記憶"}`，exit 1。
   - 否則呼叫 `_remember_bench_endpoint(code, None)` 刪除記憶，印 close 結果，exit 0。
   - `_status_tunnels` 發生錯誤時回 `TUNNEL_STATUS_ERROR`（`"remote status 回應格式異常"`、`"無法確認 tunnel 狀態"`、`"remote status 回應缺少 tunnels 清單"`）。
5. open：`rt.resolve_ssh_bin(via)` 後執行 `rt.open_tunnel(...)`。ok 時寫入記憶 `tcp://127.0.0.1:{entry.local_port}`。寫入失敗時，若**這次**真的開了 tunnel（不是 `already_running`）就 `_rollback_connect_open`（close 後再驗證），回 `INVALID_BENCH_STATE` 或 `BENCH_STATE_IO_ERROR`，並可附 `rollback_warning`（字串），exit 1。最後印 res，exit 0（ok）或 1。
6. 錯誤 JSON 為 `{"ok":false,"error_code","message"}`，exit 1，stderr context 為 `connect`。

`benches.yaml`（`bench_registry.py`）：路徑為 `$SERIALWRAP_BENCHES_FILE`，否則 `{$SERIALWRAP_CONFIG_DIR or ($XDG_CONFIG_HOME or ~/.config)/serialwrap}/benches.yaml`，在**呼叫時**讀取。根只允許 `benches` 一個 key。每個 entry **必須同時具備且只能有** `target`（str、strip 後非空、恰好一個 `@`、形式為 `user@host`、兩段前後無空白）、`remote_socket`（非空 str）、`local_port`（int，非 bool，1..65535）、`ssh_opts`（list[str]）、`autossh`（bool）。檔案不存在時視為 `{}`；內容 None 時為 `{}`。`BenchEntry` 是 frozen dataclass。另有 `to_remote_argv(entry)`（未被 CLI 使用）。

### 6.13 `benches`（`cli.py:2294-2298`；本機；`_run_benches` 在 `cli.py:1462-1529`）
沒有參數，argparse 錯誤時為 JSON 邊界格式。
- `nt` 時回 `REMOTE_NOT_SUPPORTED`，message `"native Windows 本期不支援 serialwrap benches；請直接檢視 benches.yaml 與 endpoint 記憶"`，exit 1。
- `bench_registry.load_configured_benches()` 的 ValueError 為 `INVALID_BENCH_CONFIG`。
- 讀 bench state 的錯誤為 `INVALID_BENCH_STATE` 或 `BENCH_STATE_IO_ERROR`。
- `alive_ports` 取自 `_status_tunnels(rt, run_dir)` 中 `role=="connect"` 的 listen_port（會觸發 rt.status 的 prune 副作用）。
- 輸出 `{"ok":true,"benches":[{"alive":bool,"code":str,"endpoint":str|null}...]}`，依 code 排序（`sorted(configured_benches)`），exit 0。
- `alive` 的條件：記住的 endpoint 為 tcp、host 屬 loopback，且 port 在 alive_ports 中。
- **不連 daemon，也不送 RPC。**

### 6.14 bench state 檔（`benches.state.json`）
- 路徑 `_bench_state_path()`（`cli.py:1115-1122`）：`$SERIALWRAP_STATE_DIR`（strip 後非空）否則 `($XDG_STATE_HOME or ~/.local/state)/serialwrap`，經 expanduser 後加 `/benches.state.json`。**呼叫時**讀 env，不用 constants。
- 讀取 `_load_bench_state_doc(path)`（`1148-1161`）：檔案不存在時 payload 與 benches 都是**同一個** `{}`；根不是 object 時 ValueError（`benches.state.json 必須是 object`）；`benches` 欄位為 None/缺席時把**根本身**當 bench map（扁平格式）；`benches` 不是 dict 時 ValueError（`...的 benches 欄位必須是 object`）。
- **因此新檔是扁平格式** `{"<code>":"tcp://127.0.0.1:<port>"}`；同時相容巢狀格式 `{"benches":{...}}`。
- 值的驗證 `_remembered_bench_endpoint`（`1164-1188`）：None 視為沒有記錄；非 str 或空白、解析失敗、不是 tcp、host 不屬 loopback，都回 ValueError `bench "{code}" 的 endpoint 記憶損壞；請重新執行 serialwrap connect {code}`；合法時回傳 strip 後的值。
- 寫入 `_remember_bench_endpoint`（`1235-1243`）：在 `_lock_bench_state_doc` 內（POSIX 用 `{path}.lock`，0600 + `fcntl.flock LOCK_EX`，外層再加 thread lock；nt 只用 thread lock）重新讀取並修改，再 `_write_bench_state_doc`（`tempfile.mkstemp(dir, prefix="benches.state.json.tmp.")`，`json.dump(sort_keys, 緊湊, ensure_ascii=False)`，**沒有尾端換行**，`os.replace`）。
- `--bench` 讀取時**不加鎖**。

### 6.15 `event`（`cli.py:2300-2340`；`_dispatch_event` 在 `cli.py:979-1078`）
`_EVENT_CMD_METHOD`（`cli.py:965-976`）：add→`event.rule_set`、rm→`event.rule_delete`、list→`event.rule_list`、show→`event.rule_get`、enable→`event.com_enable`、disable→`event.com_disable`、status→`event.com_status`、reset→`event.reset`、reload→`event.reload`、tail→`event.tail`。

| 子命令 | 參數 | params |
|---|---|---|
| `add` | `--file` 必填 | 讀檔後 `json.load`，結果**整個**當 params（不檢查型別；不是 dict 時照樣送出） |
| `rm` | `rule_id`（positional） | `{"rule_id"}` |
| `list` | `--selector`、`--owner` | `{"selector":v|null,"owner":v|null}`（None 也送 null） |
| `show` | `rule_id`（positional） | `{"rule_id"}` |
| `enable` | `--selector` 必填 | `{"selector"}` |
| `disable` | `--selector` 必填 | `{"selector"}` |
| `status` | `--selector` | `{"selector":v|null}` |
| `reset` | mutually exclusive group（required=True）：`--rule-id` 或 `--selector` | `{"rule_id":v|null,"selector":v|null}` |
| `reload` | — | `{}` |
| `tail` | `--rule-id`、`--selector`、`-n`（int，50）、`--since`（int，None） | `{"rule_id","selector","n","since_ts":since}`，None 送 null |

- timeout 取 `_effective_timeout_s(args, method_name)`，全部為 5s。**`--retries` 不轉送**，event.* 也都不在白名單。
- 輸出：`_print(result)`、`_warn_version_mismatch`、`_mirror_err(result, context=method_name)`，exit 0/2。
- 錯誤：`_EndpointResolutionError` 時 exit 1；任何其他 Exception（含 `--file` 開檔失敗、JSON 解析錯誤）印 `{"error_code":"INTERNAL_ERROR","message":...,"ok":false}` 加 stderr，exit 1。
- 未知 event_cmd（argparse 已擋，理論上到不了）：`{"ok":false,"error_code":"UNKNOWN_EVENT_CMD","cmd":...}`，exit 2。
- 寫入順序：stdout JSON 先輸出，接著 version mismatch 警告，再來錯誤行（與 `_run_rpc` 的順序相反：`_run_rpc` 先寫錯誤行，再寫 version 警告）。

### 6.16 `supervision-mode`（`cli.py:2342-2346`、`2684-2686`；本機）
沒有參數。stdout 純文字為 `{mode}\n`（`print(_default_runtime_config().mode() or "on-demand")`），exit 0。**唯讀，不能切換模式**；切換只能透過 `setup`。config.yaml 有壞 YAML 時會 traceback。mode 值不驗證，config 裡寫什麼就印什麼。

### 6.17 `service`（`cli.py:2348-2369`、`2688-2693`；本機）
參數：`action`（positional，choices `start|stop|restart|status`）、`--with-sudo`（False）。
`mode = _default_runtime_config().mode() or "on-demand"`；`result = service_action(action, mode=mode, with_sudo=...)`；`_print`；`_mirror_err(context=f"service {action}")`；exit 0/2。

`service_action`（`service_ctl.py:21-62`），`_UNIT = "serialwrap"`、`_PRIVILEGED = {"start","stop","restart"}`：
- `systemd-user`：執行 `systemctl --user {action} serialwrap`，回傳 `{"ok":rc==0,"mode","action","rc","stdout","stderr"}`。
- `systemd-system`：特權 action 且未帶 with_sudo 時回 `{"ok":false,"mode","action","error_code":"NEEDS_SUDO","hint":"需 root：請執行 `sudo systemctl {action} serialwrap`（或加 --with-sudo 讓本指令代跑）"}`。否則執行 `[sudo] systemctl {action} serialwrap`（status 不加 sudo），回傳同上。
- 其他模式：`{"ok":false,"mode","action","error_code":"NO_SYSTEMD","hint":"目前為 on-demand 監管模式（無 systemd 服務）。使用 `serialwrap daemon start/stop`。"}`。
- `SystemEffects.run`：`subprocess.run(capture_output=True, text=True, check=False)`，沒有 timeout；stdout/stderr 為 str。

### 6.18 `setup`（`cli.py:2371-2398`、`_run_setup` 在 `1673-1801`；本機）
參數：mutually exclusive group 中的 `--user`、`--system`、`--on-demand`（dest `on_demand`），另有 `--force`、`--with-sudo`。**沒有 `--rollback`。**
流程：
1. `fx = SystemEffects()`；`legacy = detect_legacy_install()`（偵測 `~/.paul_tools`，回傳 dict 或 None）。
2. `target = _resolve_target_mode(args, fx)`（`1662-1670`）：依 `--user`、`--system`、`--on-demand` 順序對應 `systemd-user`、`systemd-system`、`on-demand`；都沒給時，`fx.has_systemd()`（`os.path.isdir("/run/systemd/system")`）為真就用 systemd-user，否則 on-demand。
3. `old = _default_runtime_config().mode() or "on-demand"`（不安全讀取）。
4. 探測（忽略 `--bench`）：`health.ping` 0.5s 取得 `daemon_running`；`mcu.status` 0.5s 取 `.flashing` 得到 `any_flashing`。例外時當作 False。
5. `any_flashing` 為真且未帶 `--force` 時回 `{"ok":false,"error_code":"FLASHING_BUSY","message":"flash 進行中，拒絕 setup（可用 --force 覆寫）"}`，exit 2。這一步在物化**之前**。
6. `materialize_assets(force=args.force)`。
7. `wsl_info = ensure_wsl_systemd(fx, ~)`；`needs_restart` 時回 `{"ok":true,"legacy","wsl_systemd_enabled":true,"needs_restart":true,"requested_mode":target,"hint"}`，exit 0。
8. effective socket：`old == target == "on-demand"`、非 win backend、未帶 `--socket` 與 `--endpoint`，且既有 config 的 `socket_path` 為 tcp 時保留該值；否則 systemd-system 用 `SYSTEM_SOCKET`，其他用 `SOCKET_PATH`。
9. `reconcile(old_mode, target_mode, fx, home, daemon_running, any_flashing, with_sudo, force, socket_path=effective, config_path=CONFIG_DIR/config.yaml)`；`FlashingBusy` 時回 `{"ok":false,"error_code":"FLASHING_BUSY","message":str,"legacy"}`，exit 2。
10. 成功時回 `{"ok":true,"legacy","setup":result,"doctor_hint":"serialwrap doctor 可驗證環境","console_hint":"human console 請用 serialwrap-minicom COMx（勿直接 minicom -D /dev/ttyUSBx，避免與 daemon two-reader 衝突）","pending_sudo"?}`（pending_sudo 非空才帶），exit 0。
- `reconcile` 的回傳形狀（`setup_cmd.py:500-644`）：`{"mode","transitioned","applied","ran":[[argv]...],"pending_sudo":[[argv]...]}`；system 模式未授權時另帶 `"requested_mode"`，且 `mode` 為舊模式、`applied:false`。
- 其他例外（materialize 失敗等）**不攔截**。

### 6.19 `doctor`（`cli.py:2400-2410`、`_run_doctor` 在 `1649-1659`；本機加 RPC 探測）
沒有參數。`report = run_doctor()`；advisory 集合依 `sys.platform.startswith("win")` 選 WIN 版或一般版；`overall_ok = all(item["ok"] or item["check"] in advisory)`；輸出 `{"ok":overall_ok,"checks":report}`，**exit 一律 0**。
- 每個 check 為 `{"check":str,"ok":bool,"detail":str,"fix":str,"advisory":bool}`（`advisory` 由 `run_doctor._stamp` 加上）。
- `DOCTOR_ADVISORY_CHECKS`（`doctor_cmd.py:566-570`）：`systemd, wsl_systemd, devices, other_serialwrap_installs, wal_dir, serialwrap_minicom_on_path, jq_on_path, minicom_on_path, profile_bootloader_prompts`。
- `DOCTOR_ADVISORY_CHECKS_WIN`（`571-575`）：`serialwrap_on_path, serialwrapd_on_path, daemon_endpoint, devices, other_serialwrap_installs, wal_dir, profile_bootloader_prompts`。
- Linux check 順序（`doctor_cmd.py:622-652`）：python、pyyaml、serialwrap_on_path、serialwrapd_on_path、other_serialwrap_installs、dialout、serialwrap_minicom_on_path、jq_on_path、minicom_on_path、systemd、supervision_mode、single_daemon、endpoint_reachable、wal_dir、wal_writable、profile_bootloader_prompts、devices、wsl_systemd。
- Windows 順序（`606-620`）：python、pyyaml、pyserial、serialwrap_on_path、serialwrapd_on_path、other_serialwrap_installs、supervision_mode、daemon_endpoint、wal_dir、wal_writable、profile_bootloader_prompts、devices。
- doctor 會反向 import cli 的 `_safe_runtime_config`、`_local_default_endpoint`、`_endpoint_alive`、`_resolve_default_endpoint_with_source`；內部 `rpc_call(endpoint,"health.status",{},timeout_s=0.5)`。各 check 細節屬 doctor 盤點範圍。

### 6.20 `profile`（`cli.py:2412-2436`、`_run_profile_test` 在 `1859-1905`；離線）
`test`：`--profile` 必填、`--sample` 必填、`--profile-dir`（dest `profile_dir`，default `PROFILE_DIR`）。
- `load_profiles(profile_dir)`（`config.py:314`）。**注意**：`all_templates = ordered_templates` 會被逐檔**覆寫**，只保留排序最後一個 yaml 檔的 templates（config 盤點範圍的 quirk，會影響 `available`）。
- 找不到時回 `{"ok":false,"error_code":"UNKNOWN_PROFILE","profile","profile_dir","available":sorted(set(names))}`，exit 0。
- sample 以 `open(..., encoding="utf-8", errors="replace")` 讀；OSError 時回 `{"ok":false,"error_code":"SAMPLE_READ_FAILED","message"}`，exit 0。
- 成功時回 `{"ok":true,"profile","platform","profile_dir","sample","checks":{"prompt_regex":C,"login_regex":C,"password_regex":C,"bootloader_prompts":[C...]}}`，其中 `C = {"pattern","matched":bool,"matched_line":str|null,"error":str|null}`。
  - `_profile_test_check`（`1804-1826`）：pattern 為空時 matched=false；`re.compile` 失敗時 error 為錯誤訊息；`re.search(sample_text)` 命中時 `matched_line` 為**命中位置所在的完整行**（取前一個 `\n` 之後到下一個 `\n` 之前）。
  - bootloader 檢查（`1841-1856`）：只對**最後一個非空白行**（`splitlines()` 由後往前找第一個 `strip()` 非空的行）做 `re.search`。
- 也依賴 Python `re` 的語意。

### 6.21 `skill`（`cli.py:2438-2452`、`_run_skill` 在 `1098-1112`；離線）
`--platform`：choices `auto|linux|windows`，default `auto`。auto 時 `sys.platform.startswith("win")` 為 windows，否則為 linux。windows 讀 `assets/skill/SKILL_WINDOWS.md`，否則讀 `assets/skill/SKILL.md`。`sys.stdout.write(read_text(...))` 原文輸出，不加換行，exit 0。

---

## 7. `daemon start`：CLI 如何 spawn daemon（`_run_daemon_start`，`cli.py:425-621`）

依序：
1. `is_win = _rpc_backend_is_win()`。
2. **`--endpoint` 檢查**：有 `--endpoint`，且不是「win 且為 loopback tcp」的情況時，回 `REMOTE_NOT_SUPPORTED`，exit 2，stderr context 為 `daemon start`。
   - POSIX message：`"--endpoint 不支援 daemon start（daemon 只能在本機啟動）"`
   - Windows message：`"--endpoint 於 daemon start 僅接受 loopback tcp://（127.0.0.1/localhost/::1）作為本機 bind 位址"`
   - `_is_loopback_tcp(ep)`（`190-196`）：tcp 且 host ∈ LOOPBACK_TCP_HOSTS。
3. `--endpoint` 與 `--socket` 都有給且值不同時回 `INVALID_ARGS`，message `"--endpoint 與 --socket 不一致；daemon start 請擇一指定 bind 位址"`，exit 2。
4. **監管 gate**：`rc = _safe_runtime_config()`；`rc` 不為 None 且 `should_auto_spawn(rc)` 為 false（mode 不是 on-demand）時，`resp = service_action("start", mode, with_sudo)`，加上 `resp["_routed_to"] = "service start"`，印出並鏡像到 stderr，exit 0/2。`should_auto_spawn(rc)`（`315-318`）：`(rc.mode() or "on-demand") == "on-demand"`。注意：未知 mode 字串（例如 `"foo"`）會被當成非 on-demand，於是送進 service_action，得到 `NO_SYSTEMD`。
5. **冪等探測**：`resolved = _resolve_local_endpoint_info(args)`（忽略 bench）；`_probe_healthy_daemon(endpoint)` 送 `health.ping` timeout 0.5，例外時為 False。ok 時印 `{"already_running":true,"ok":true,"socket":endpoint}`，exit 0。
6. **spawn 目標 sock**：依 `args.socket`（不為 None）、`--endpoint`、`_local_default_endpoint()` 的順序決定；`daemon_resolution.source` 依序為 `--socket`、`--endpoint`、`default`。
   - spawn 目標**不讀 config.yaml**；探測則會讀 config。
7. **POSIX 多開防線（#173）**：非 win 且未帶 `--force-spawn` 時，`_find_conflicting_daemon(sock)` 會 `detect_multi_open(proc_root="/proc")`，找第一個 `d["socket"] != sock` 的 daemon（socket 為 None 也算衝突）。找到時：
   ```
   {"ok":false,"error_code":"DAEMON_ALREADY_RUNNING_ELSEWHERE",
    "message":"偵測到另一個 serialwrapd（pid={pid}）綁在 {existing_socket}，與本次 daemon start 目標 {sock} 不同；為避免同一批裝置被兩個 daemon 同時開啟（two-reader），預設拒絕再 spawn 一個。",
    "hint": known? "改用 --socket {known} 指向現有 daemon；確認後要強制另起才加 --force-spawn"
                 : "先用 `serialwrap daemon status`（看 multi_open_detail）確認現有 daemon 的 socket；確認後要強制另起才加 --force-spawn",
    "existing_socket": str|null, "existing_pid": int, "target_socket": sock}
   ```
   exit 2，**沒有 stderr 鏡像**。`existing_socket` 為 None 時，message 中的佔位字串是 `未知（無法從 /proc 讀出 --socket，可能是舊版 daemon）`。
   - serialwrapd 判定（`multi_open.py:27-61`）：argv0 的 basename 為 `serialwrapd`；或任一參數以 `serialwrapd.py` 結尾；或 argv[1:] 中有含 `/` 的參數其 basename 為 `serialwrapd`。
   - `--socket` 值取自 cmdline，支援 `--socket X` 與 `--socket=X`。
   - `/proc` 列舉失敗時 `daemons=[]`，視為沒有衝突。
8. **daemon argv**：
   - win：`_daemon_spawn_argv()`（`199-215`）。frozen 時依序找 `dirname(sys.executable)/serialwrapd.exe`、PATH 上的 `serialwrapd`，都沒有時回傳 None，對應 `DAEMON_BINARY_NOT_FOUND`（message `"找不到 serialwrapd（serialwrap.exe 同層或 PATH 上皆無）；請確認發行包完整"`），exit 2。非 frozen 時，repo 的 `serialwrapd.py` 存在就用 `[sys.executable, script]`，否則用 `[sys.executable, "-m", "sw_core.daemon"]`。
   - **POSIX**：一律用 `[sys.executable, _daemon_script_path()]`，其中 `_daemon_script_path()` 為 `normpath(dirname(cli.py)/../serialwrapd.py)`（`185-187`）。**沒有 `-m` fallback**。
   - 完整 cmd：`[*argv, "--profile-dir", args.profile_dir, "--socket", sock, "--lock", args.lock]`。
   - daemon 端 parser（`daemon.py:38-44`）：`--profile-dir`（PROFILE_DIR）、`--socket`（DEFAULT_ENDPOINT）、`--lock`（LOCK_PATH）。
9. **env 檔**：`_resolve_daemon_start_env_files(profile_dir)`（`329-347`）。`SERIALWRAP_DAEMON_ENV_FILE` 有設時，strip 後非空就用 `[value]`，否則 `[]`；未設時為 `["~/OPI.env", "{profile_dir}/OPI.env"]`（`LEGACY_DAEMON_ENV_FILE="~/OPI.env"`、`PROFILE_DAEMON_ENV_FILE="OPI.env"`，`cli.py:51-52`）。
   - `_load_daemon_start_env_files(files)`（`257-299`）：起點為 `env = dict(os.environ)`。逐檔處理：expanduser 並 strip、去重、不存在就略過。
     - POSIX：`subprocess.run(["bash","-lc",'set -a && source "$1" >/dev/null && env -0',"serialwrap",path], env=env)`。rc≠0 時拋 `EnvFileSourceError(path, stderr or "failed to source {path}")`。成功時解析 `\0` 分隔的 `KEY=VALUE`，用 utf-8 + surrogateescape 解碼，**以結果整個取代 env**（bash -l 的 profile 會影響環境）。
     - win（`_rpc_backend_is_win()`）：`_parse_env_file_simple`（`230-254`）：支援 `KEY=VALUE`、`export KEY=VALUE`、`#` 註解、空行，去掉成對的單/雙引號，不展開變數；OSError 時為 `EnvFileSourceError("無法讀取 env 檔：{exc}")`；結果用 `env.update`。
   - 失敗時回 `{"ok":false,"error_code":"ENV_FILE_SOURCE_FAILED","env_file":path,"message":str,"env_files":[expanduser(...)]}`（env_files 非空才帶），exit 2。
10. **`--foreground`**：`return subprocess.call(cmd, env=daemon_env)`，沒有 JSON，exit code 為 daemon 的 exit code（daemon 可能回 3 = StateLoadError 等）。
11. **背景模式**：`Popen(cmd, stdout=DEVNULL, stderr=DEVNULL, env=daemon_env, start_new_session=True)`；`os.name=="nt"` 時改用 `creationflags=DETACHED_PROCESS|CREATE_NEW_PROCESS_GROUP`。**stdin 繼承**父程序（沒有設 DEVNULL）。
12. **就緒輪詢**：`attempts = 50 if is_win else 15`，每次先 `sleep(0.2)`，所以 POSIX 約 3s、win 約 10s。
    - `proc.poll()` 不為 None 時回 `{"ok":false,"error_code":"DAEMON_EXITED","pid","returncode"}`，exit 2。
    - `health.ping` 送往 **sock**，timeout 0.5，帶 trace。ok 時再送 `health.status`（timeout 1.0）；輸出 `{"ok":true,"pid":proc.pid,"socket":sock,"env_files"?:loaded_paths,"warnings"?:health.warnings}`（兩者都是 truthy 才帶），exit 0。
13. 輪詢逾時回 `{"ok":false,"error_code":"DAEMON_NOT_READY","pid"}`，exit 2。**不會 kill** 已 spawn 的 daemon。

### 7.4 已觀察到的 POSIX spawn 缺陷（推論，未實跑）
POSIX 路徑固定用 `[python, site-packages/serialwrapd.py]`，但套件沒有包含 `serialwrapd.py`，本機 pipx venv 實查也沒有這個檔。依此推論，pipx 安裝且 on-demand 模式下 `daemon start` 會 spawn 失敗，得到 `DAEMON_EXITED`。repo checkout 與 systemd 模式（走 service start）不受影響。Rust 版應明確規定 daemon binary 的定位方式（同目錄、PATH、組合固定路徑）。

### 7.5 本機與遠端的區分
- daemon start 與 setup 的探測用 `_resolve_local_endpoint_info`，**忽略 `--bench`**。
- daemon start 拒絕 `--endpoint`（Windows 的 loopback 除外）。
- daemon stop **會尊重** `--endpoint`、`--socket`、`--bench`，可以停遠端 daemon。

### 7.6 `daemon stop`（`_run_daemon_stop`，`cli.py:624-657`）
- `mode = (_safe_runtime_config().mode() or None) or "on-demand"`。
- `explicit_target = bool(endpoint) or socket is not None or bool(bench)`。
- `mode.startswith("systemd")` 且不是 explicit_target 時，呼叫 `service_action("stop", mode, with_sudo)` 並加上 `_routed_to: "service stop"`，印出、鏡像到 stderr（context `daemon stop`），exit 0/2。
- 否則 `resolved = _resolve_endpoint_info(args)`（含 bench 與 dangling fallback），送 `daemon.stop` `{}`，**timeout 固定 2.0，retries 0**。失敗時印出、stderr context 為 `daemon.stop`，exit 2；成功時印 `{"ok":true,"stopping":true}`，exit 0。
- `_EndpointResolutionError` 時 exit 1；其他例外為 INTERNAL_ERROR，exit 1。
- 不呼叫 `_warn_version_mismatch`。

---

## 8. 本機專屬與 RPC 指令分類（依實際程式碼）

| 類別 | 指令 |
|---|---|
| 純 RPC（`_run_rpc`） | `daemon status`；`device *`；`session *`（含 console-attach/console-detach）；`alias *`；`cmd status/result-tail/cancel`；`cmd submit`（另有本地 regex 預檢）；`stream tail`；`log *`；`file *`；`wal *`；`mcu *` |
| RPC（自有分派） | `event *`（`_dispatch_event`） |
| 本機加 RPC 混合 | `daemon start`（探測、/proc、spawn、輪詢）；`daemon stop`（systemd 時走 systemctl，否則 RPC `daemon.stop`）；`setup`（探測 ping 與 mcu.status）；`doctor`（health.status 探測） |
| 純本機，不連 daemon | `supervision-mode`、`service`、`remote`、`connect`、`benches`、`profile test`、`skill`、`--version` |

CLI 可能送出的 RPC method 全集（53 個）：
`health.ping, health.status, daemon.stop, device.list, device.release, device.attach, session.list, session.clear, session.bind, session.pin, session.unpin, session.attach, session.self_test, session.activity, session.recover, session.console_attach, session.console_detach, session.console_list, session.interactive_open, session.interactive_send, session.interactive_status, session.interactive_close, session.log_start, session.log_stop, session.log_status, alias.list, alias.set, alias.assign, alias.unassign, command.submit, command.get, command.result_tail, command.cancel, result.tail, log.tail_raw, log.tail_text, file.push, file.pull, wal.range, wal.reset, wal.current_seq, mcu.patterns, mcu.status, event.rule_set, event.rule_delete, event.rule_list, event.rule_get, event.com_enable, event.com_disable, event.com_status, event.reset, event.reload, event.tail`。

daemon 有、但 CLI 從不呼叫的：`session.get_state`（只出現在 retry 白名單）。

---

## 9. CLI 範圍可能產生的 error_code（依來源整理）

| code | 產生處 | exit |
|---|---|---|
| `INVALID_ENDPOINT` | client 解析；remote expose 的 forward_src | 2（RPC）／1（remote） |
| `SOCKET_ERROR`、`TIMEOUT`、`EMPTY_RESPONSE`、`INVALID_RESPONSE` | client | 2 |
| `INVALID_BENCH_STATE`、`BENCH_STATE_IO_ERROR`、`BENCH_ENDPOINT_NOT_REMEMBERED` | `--bench` 解析；connect；benches | 1 |
| `INTERNAL_ERROR` | daemon stop、event、connect、benches、remote 的 catch-all | 1 |
| `REMOTE_NOT_SUPPORTED` | daemon start 的 `--endpoint`（exit 2）；Windows 上的 remote/connect/benches（exit 1） | 2／1 |
| `INVALID_ARGS` | daemon start 的 socket/endpoint 不一致（2）；remote（1）；connect/benches 的 argparse 錯誤（1）；fallthrough（2） | |
| `DAEMON_ALREADY_RUNNING_ELSEWHERE`、`DAEMON_BINARY_NOT_FOUND`、`ENV_FILE_SOURCE_FAILED`、`DAEMON_EXITED`、`DAEMON_NOT_READY` | daemon start | 2 |
| `NEEDS_SUDO`、`NO_SYSTEMD` | service_action（service、daemon start/stop 重導） | 2 |
| `FLASHING_BUSY` | setup | 2 |
| `UNKNOWN_EVENT_CMD` | event（到不了） | 2 |
| `INVALID_COMPLETION_REGEX` | cmd submit 本地預檢 | 1 |
| `UNKNOWN_PROFILE`、`SAMPLE_READ_FAILED` | profile test | 0 |
| `UNKNOWN_BENCH_CODE`、`INVALID_BENCH_CONFIG`、`TUNNEL_STATUS_ERROR`、`TUNNEL_CONFLICT`、`TUNNEL_STILL_ACTIVE` | connect／benches | 1 |
| `INVALID_TARGET`、`SSH_NOT_FOUND`、`TUNNEL_SPAWN_FAILED`、`REMOTE_BIND_UNVERIFIED`、`TUNNEL_CONFLICT` | remote_tunnel（remote/connect） | 1 |
| `UNKNOWN_ERROR` | **只出現在 stderr 文字**（沒有 code 也沒有 message 時），不是 JSON error_code | — |

另有 client 端附加欄位：`daemon_reachable`、`daemon_busy`。daemon start 與 setup 的 JSON 另有 `_routed_to`、`already_running`、`existing_socket`、`existing_pid`、`target_socket`、`env_file`、`env_files`、`warnings`、`legacy`、`needs_restart`、`requested_mode`、`wsl_systemd_enabled`、`doctor_hint`、`console_hint`、`pending_sudo`、`setup`、`rollback_warning`。

---

## 10. 環境變數（CLI 程序內實際讀取）

| 變數 | 用途 | 讀取時機 |
|---|---|---|
| `SERIALWRAP_CONFIG_DIR`、`XDG_CONFIG_HOME` | CONFIG_DIR → config.yaml、PROFILE_DIR | import 時 |
| `SERIALWRAP_STATE_DIR`、`XDG_STATE_HOME`、`XDG_RUNTIME_DIR`、`SERIALWRAP_RUN_DIR` | RUN_DIR → SOCKET_PATH/LOCK_PATH | import 時；另外 `_bench_state_path`（STATE_DIR、XDG_STATE_HOME）與 `_remote_run_dir`（RUN_DIR）在**呼叫時**再讀一次 |
| `SERIALWRAP_PROFILE_DIR` | daemon start、profile test 的 `--profile-dir` 預設值 | import 時 |
| `SERIALWRAP_TCP_PORT` | DEFAULT_TCP_PORT（預設 48700） | import 時 |
| `SERIALWRAP_ENDPOINT` | 只影響 `DEFAULT_ENDPOINT`；POSIX CLI 的解析**不使用**；win 的 `_local_default_endpoint` 使用 | import 時 |
| `SERIALWRAP_RPC_BACKEND` | win/posix 判定 | 呼叫時 |
| `SERIALWRAP_DAEMON_ENV_FILE` | daemon start 的 env 檔 | 呼叫時 |
| `SERIALWRAP_LOG_LEVEL` | trace 層級 | 呼叫時 |
| `SERIALWRAP_BENCHES_FILE` | benches.yaml 路徑 | 呼叫時 |
| `HOME`（expanduser） | 多處 | — |
| wrapper：`SERIALWRAP_BIN`、`SERIALWRAP_SOCKET`、`SERIALWRAP_AUTO_START_DAEMON`（1）、`SERIALWRAP_ATTACH_WHEN_NOT_READY`（1） | minicom_router.sh | — |

---

## 11. 規格書與實際程式碼的差異（spec §4.1、§4.2、§9.2、§9.4）

1. **全域選項不完整**（§4.2-1）：規格只列 `--socket/--endpoint/--bench/--timeout/--retries`。實際還有 `--version` 與 `-v/--verbose`（count，對應 CLI trace，§5），以及 `SERIALWRAP_LOG_LEVEL`。
2. **`--endpoint` 格式**：規格寫 `tcp://<HOST>:<PORT>`，實際也接受 `unix:///abs/path` 與裸路徑（`client.py:76-114`）。
3. **重試白名單 11 個方法與程式完全一致** ✔。但規格的「不重試」舉例 `cmd.status`、`cmd.result_tail`、`wal`、`benches`、`profile` 不是 RPC method 名稱：實際是 `command.get` 與 `command.result_tail`，`benches` 與 `profile` 根本不送 RPC。重試另外只對 transient code `TIMEOUT/SOCKET_ERROR/EMPTY_RESPONSE` 生效，退避為 0.5×2ⁿ、上限 5s；TIMEOUT 會 enrich `daemon_reachable/daemon_busy`。這些規格都沒寫。`event` 的 dispatch 完全不轉送 `--retries`。
4. **`daemon` 群組**：規格寫 `start (--force-spawn), stop, restart, status`，**實際沒有 `restart`**。另有 `start --profile-dir/--lock/--foreground/--with-sudo` 與 `stop --with-sudo`，規格沒列。
5. **`remote` 群組**：規格寫 `-R, -L, status, stop`，實際是 `close <port|all>`，**沒有 `stop`**；`status` 是 positional word，不是子 parser。另有 `--autossh/--local/--remote-socket/--ready-timeout/--ssh-opt`。
6. **`setup` 群組**：規格寫 `--rollback`，**實際沒有這個旗標**。實際旗標為 `--user|--system|--on-demand`（互斥）、`--force`、`--with-sudo`。
7. **`supervision-mode`**：規格寫「查詢與切換、修改設定並執行監管 reconcile」，實際**只讀並印純文字**，不能切換；切換由 `setup` 負責。
8. **`session console-attach/console-detach` 被歸為「終端類，Stage 1B」**：Python CLI 中兩者**只是 RPC**（`session.console_attach` 回傳 `client_id/vtty`），沒有本地終端橋接；橋接在 `minicom_router.sh` 與 daemon PTY。技術上可以歸入 1A 的純 RPC。console_attach 套用 45s floor。
9. **`benches` 被歸為「純 RPC／設定查詢，1A」**：實際**不送任何 RPC**。它讀 benches.yaml、benches.state.json 與 remote tunnel registry，並透過 `rt.status` **就地 prune 死掉的 state（寫入副作用）**，依賴 `remote_tunnel`（規格 §4.1 把它列在 1B）。歸類需要調整，或者在 1A 階段同時承接 remote_tunnel 的 status。
10. **`profile` 被歸為「純 RPC／設定查詢」**：實際只有 `profile test` 一個子命令，**完全離線**（load_profiles 加 Python `re`），exit 一律 0。Rust 版需要與 Python `re` 相容的 regex 引擎，`cmd submit --completion-regex` 的本地預檢也有同樣問題，規格沒提。
11. **`connect`**：規格寫「遠端／測試台連線輔助子命令」，實際為 `connect <code> [--close]`，只有一個 positional 加一個旗標，沒有子命令。
12. **`cmd submit --mode line|background|interactive`**：CLI **沒有 choices 驗證**，任意字串都轉給 daemon。`--cmd` 不是必填（default `""`）。
13. **`daemon status` 寫成含「多開偵測」**：CLI 只送 `health.status`，多開偵測在 daemon 端的 health() 內完成。CLI 端的 /proc 多開檢查只在 `daemon start` 的 spawn 防線中做。
14. **錯誤碼集合（§9.2）不完整**：CLI 範圍實際會輸出、但不在規格 71+6+1 清單中的碼共 10 個：`BENCH_ENDPOINT_NOT_REMEMBERED`、`INVALID_BENCH_CONFIG`、`UNKNOWN_BENCH_CODE`、`TUNNEL_STATUS_ERROR`、`TUNNEL_CONFLICT`、`TUNNEL_SPAWN_FAILED`、`REMOTE_BIND_UNVERIFIED`、`INVALID_TARGET`、`SSH_NOT_FOUND`、`INVALID_COMPLETION_REGEX`（daemon 端亦用）。`UNKNOWN_ERROR` 只出現在 stderr 文字。
15. **Exit code 契約（§9.4 要求凍結）**：規格沒有列出 exit code。實際並不一致：RPC 失敗為 2；endpoint 解析失敗為 1；remote/connect/benches 為 1；doctor 與 profile test 恆為 0；`cmd submit` 的 regex 錯誤為 1；connect/benches 的 argparse 錯誤為 1，其他 argparse 錯誤為 2；daemon start 的 foreground 為子程序 rc。凍結時應以 §2.4 為基準。
16. **stdout JSON 格式**：CLAUDE.md 寫 CLI 用 `json.dumps(..., ensure_ascii=False, separators=(",",":"))`，實際 `_print` **另外有 `sort_keys=True`**。規格要凍結的 JSON 格式應包含排序規則。
17. **規格 §4.3 委派**：要求 Stage 1A 對第 14～20 項加 console-attach/console-detach 透明委派給 Python。依上列分類，console-attach/console-detach 可以不委派，benches 反而需要 remote_tunnel 能力。另外 `daemon start` 依賴 `sys.executable` 加 repo 的 `serialwrapd.py` 來 spawn（POSIX）。由 Rust CLI 委派到 Python 時，要確保 Python 入口能找到 daemon；POSIX pipx 安裝的 spawn 路徑本身已有疑慮，見 §7.4。
18. **CLI timeout 預設**：規格沒有寫 `DEFAULT_RPC_TIMEOUT_S=5.0`、`LONG_RPC_TIMEOUT_FLOOR_S=45.0`、長操作 4 個方法，也沒寫各硬編 timeout（daemon stop 2.0、探測 0.5/1.0、輪詢 15 或 50 次 ×0.2s）。

---

## 12. 移植時須注意的 Python 既有缺陷與 quirk（建議在契約中明確定義）

- 回應不是合法 JSON 時，`json.JSONDecodeError` 沒被攔截，CLI 印 traceback（`client.py:268`）。
- `supervision-mode`、`service`、`setup` 讀 config.yaml 遇到壞 YAML 時會 traceback（沒走 `_safe_runtime_config`）。
- `setup` 的 materialize 或 reconcile 拋出非 FlashingBusy 例外時會 traceback。
- `event add --file` 的 JSON 不是 dict 時仍照送。
- `_json_boundary_command_from_argv` 把 `--socket connect` 誤判成 connect。
- AF_UNIX 的 `SOCKET_ERROR` message 不含路徑（與 `cli.py:950` 註解不符）。
- argparse 前綴縮寫（allow_abbrev）。
- `stream tail --from-seq` 預設 0；`log tail-* --from-seq` 預設 None（省略 key）。兩者不同。
- `device release --reason` 為 None 時送 null；`session console-attach --label` 與 `alias assign --profile` 為 falsy 時**省略 key**；`event list/status/reset/tail` 為 None 時**送 null**。這三種處理方式不一致，需要逐一照抄。
- `DAEMON_NOT_READY` 時不 kill 子程序；背景 spawn 時 stdin 繼承父程序。
- bench state 新檔為扁平格式，同時相容 `{"benches":{}}`；`--bench` 讀取不加鎖。
- POSIX daemon spawn 沒有 `-m sw_core.daemon` fallback（§7.4）。
