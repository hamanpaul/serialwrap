# 09 — Event Engine／File Transfer／Config／Constants／Stream‧Log／Skill／sw_mcp 盤點

- 盤點對象：`~/prj_pri/serialwrap-worktrees/rust-refactor`，HEAD `93553e6`（VERSION `0.3.2`），唯讀。
- 對照規格：`docs/rust-refactor/serialwrap-rust-spec-final.md`（§3.2、§4.1、§4.2、§5.4、§5.5、§9.2）。
- 行號格式 `file:line` 皆對 HEAD `93553e6`。
- 本機環境：**未安裝 `google-re2`**（`import re2` 失敗），故下文「Python 現況行為」皆為「無 re2」路徑，除非另註。

---

## 1. `sw_core/constants.py` — 全部常數（依出現順序）

> 全部在 **import 時**一次計算（讀環境變數），之後不再變動。Rust 對應 `sw-proto::constants`。

### 1.1 輔助函式

| 名稱 | 行 | 語意 |
|---|---|---|
| `_env_path(name, default)` | 7-11 | `raw = os.environ.get(name)`；`None` 或 `raw.strip()==""` → 用 `default`；回傳 `os.path.expanduser(raw)`。**注意**：非空值不 strip（前後空白保留）；`~` 只在最終值展開。 |
| `_xdg(name, fallback_rel)` | 17-18 | `os.environ.get(name) or join(expanduser("~"), fallback_rel)`；空字串視同未設；**不**對 XDG 值做 expanduser。 |
| `_run_dir_default()` | 28-35 | 若 `SERIALWRAP_STATE_DIR` strip 後非空 → 回 `STATE_DIR`；否則若 `XDG_RUNTIME_DIR` 為真值 → `$XDG_RUNTIME_DIR/serialwrap`；否則 `STATE_DIR/run`。 |
| `ensure_runtime_dirs()` | 206-212 | `makedirs(exist_ok=True)`：`CONFIG_DIR`、`STATE_DIR`、`RUN_DIR`、`WAL_DIR`、`EVENTS_DIR`、`EVENTS_RUNTIME_DIR`（**不含** `DATA_DIR`、`LOG_DIR`、`PROFILE_DIR`）。 |

### 1.2 路徑／端點常數

| 常數 | 行 | 值（預設） | 覆寫 env |
|---|---|---|---|
| `BASE_DIR` | 14 | `dirname(dirname(abspath(constants.py)))`（repo/安裝根） | — |
| `_CONFIG_HOME` | 21 | `$XDG_CONFIG_HOME` 或 `~/.config` | `XDG_CONFIG_HOME` |
| `_STATE_HOME` | 22 | `$XDG_STATE_HOME` 或 `~/.local/state` | `XDG_STATE_HOME` |
| `_DATA_HOME` | 23 | `$XDG_DATA_HOME` 或 `~/.local/share` | `XDG_DATA_HOME` |
| `_RUNTIME_HOME` | 24 | `os.environ.get("XDG_RUNTIME_DIR")`（可為 None） | `XDG_RUNTIME_DIR` |
| `CONFIG_DIR` | 26 | `_CONFIG_HOME/serialwrap` | `SERIALWRAP_CONFIG_DIR` |
| `STATE_DIR` | 27 | `_STATE_HOME/serialwrap` | `SERIALWRAP_STATE_DIR` |
| `RUN_DIR` | 38 | `_run_dir_default()` | `SERIALWRAP_RUN_DIR` |
| `DATA_DIR` | 39 | `_DATA_HOME/serialwrap` | `SERIALWRAP_DATA_DIR` |
| `LOCK_PATH` | 41 | `RUN_DIR/serialwrapd.lock` | （隨 RUN_DIR） |
| `SOCKET_PATH` | 42 | `RUN_DIR/serialwrapd.sock` | （隨 RUN_DIR） |
| `DEFAULT_TCP_PORT: int` | 47 | `int(os.environ.get("SERIALWRAP_TCP_PORT", "48700"))` → `48700`；**非整數字串於 import 時 ValueError** | `SERIALWRAP_TCP_PORT` |
| `LOOPBACK_TCP_HOSTS: frozenset[str]` | 49 | `{"127.0.0.1", "localhost", "::1"}` | — |
| `DEFAULT_ENDPOINT: str` | 50-56 | Windows（`os.name=="nt"` 或 `sys.platform.startswith("win")`）：`$SERIALWRAP_ENDPOINT` 或 `tcp://127.0.0.1:{DEFAULT_TCP_PORT}`；POSIX：`$SERIALWRAP_ENDPOINT` 或 `SOCKET_PATH`（不經 expanduser） | `SERIALWRAP_ENDPOINT` |
| `TTYMCU_PATH` | 58 | `RUN_DIR/dev/ttyMCU` | `SERIALWRAP_TTYMCU_PATH` |
| `STATE_PATH` | 59 | `STATE_DIR/state.json` | （隨 STATE_DIR） |
| `WAL_DIR` | 60 | `STATE_DIR/wal` | `SERIALWRAP_WAL_DIR` |
| `PROFILE_DIR` | 61 | `CONFIG_DIR/profiles` | `SERIALWRAP_PROFILE_DIR` |
| `DEVICE_BY_ID_DIR` | 62 | `/dev/serial/by-id` | `SERIALWRAP_BY_ID_DIR` |
| `DEVICE_BY_PATH_DIR` | 63 | `/dev/serial/by-path` | `SERIALWRAP_BY_PATH_DIR` |
| `LOG_DIR` | 64 | `~/b-log` | `SERIALWRAP_LOG_DIR` |
| `DEFAULT_WAL_ROTATE_BYTES` | 65 | `64 * 1024 * 1024` = 67108864 | — |
| `EVENTS_DIR` | 66-69 | `~/.serialwrap/events.d`（**非 XDG**） | `SERIALWRAP_EVENTS_DIR` |
| `EVENTS_RUNTIME_DIR` | 70-73 | `STATE_DIR/events` | `SERIALWRAP_EVENTS_RUNTIME_DIR` |
| `EVENTS_LOG_PATH` | 74-77 | `EVENTS_RUNTIME_DIR/events.ndjson` | `SERIALWRAP_EVENTS_LOG_PATH` |
| `EVENTS_LOG_ROTATE_BYTES` | 78 | `10 * 1024 * 1024` = 10485760（**未被使用**：`EventLogger` 用自己的預設值，見 §3.8） | — |
| `EVENTS_LOG_BACKUP_COUNT` | 79 | `3`（**未被使用**，同上） | — |

### 1.3 錯誤碼／分類常數與門檻

| 常數 | 行 | 值 | 說明 |
|---|---|---|---|
| `ERROR_CREDENTIALS_UNRESOLVED: str` | 85 | `"CREDENTIALS_UNRESOLVED"` | session `last_error`（§9.3） |
| `ERROR_RX_FLOOD: str` | 90 | `"RX_FLOOD"` | probe 失敗反分類 |
| `RX_RATE_WINDOW_S: float` | 91 | `10.0` | UARTBridge RX 速率視窗 |
| `RX_FLOOD_BYTES_PER_10S: int` | 93 | `20_000` | 視窗內 raw bytes 洪水門檻 |
| `ERROR_TRANSPORT_STALL: str` | 101 | `"TRANSPORT_STALL"` | |
| `TRANSPORT_STALL_MIN_RX_AGE_S: float` | 102 | `30.0` | |
| `MAX_RECOVERY_LEASE_S: float` | 107 | `120.0` | |
| `BOOTLOADER_RX_TAIL_BYTES: int` | 109 | `512` | |
| `HUMAN_ACTIVE_WINDOW_S: float` | 111 | `60.0` | |
| `_HUMAN_PEER_GRACE_S: float` | 113 | `3.0` | |
| `REPROBE_RX_IDLE_S: float` | 115 | `3.0` | |
| `REPROBE_BACKOFF_S: float` | 117 | `2.0` | |
| `REPROBE_MAX_INTERVAL_S: float` | 119 | `15.0` | |
| `REPROBE_MAX_ATTEMPTS: int` | 121 | `10` | |
| `BOOT_BANNER_PATTERNS: tuple[str, ...]` | 125-129 | `("U-Boot", "Hit any key to stop autoboot", "CFE version ")`（大小寫敏感 substring，非 regex；第 3 項尾端有空白） | |
| `BOOT_QUIET_WINDOW_S: float` | 134 | `180.0` | |
| `ERROR_AUTOBOOT_QUIET: str` | 142 | `"AUTOBOOT_QUIET"` | 可重試 |
| `BOOT_BANNER_TAIL_CHARS: int` | 149 | `256` | |
| `READY_RECONFIRM_RETRY_S: float` | 151 | `5.0` | `retry_after_s` 固定值 |
| `READY_RECONFIRM_MAX_S: float` | 156 | `BOOT_QUIET_WINDOW_S + 120.0` = `300.0` | |
| `READY_RECONFIRM_MAX_ATTEMPTS: int` | 163 | `5` | |
| `ERROR_READY_UNCONFIRMED: str` | 169 | `"READY_UNCONFIRMED"` | 不可重試 |
| `ERROR_BOOTLOADER_STUCK: str` | 176 | `"BOOTLOADER_STUCK"` | `last_error` 值 |
| `UBOOT_FALLBACK_PROMPTS: tuple[str, ...]` | 182 | `("^=> $", "^U-Boot> $", "^CFE> $")` | |
| `BG_CAPTURE_MAX_BYTES: int` | 191 | `4 * 1024 * 1024` = 4194304 | |
| `BG_CAPTURE_MAX_COUNT: int` | 193 | `64` | |
| `CMD_HISTORY_MAX: int` | 195 | `512` | |
| `CMD_PENDING_MAX: int` | 197 | `256` | |
| `DEFERRED_INPUT_MAX_BYTES: int` | 202 | `256 * 1024` = 262144；docstring 寫「超過則**丟最舊位元組**」（BC-02 核准為整段清空，現況 docstring 為舊語意） | |

> 本檔**沒有**定義 `DAEMON_ALREADY_RUNNING`、`FLUSHED_BY_RECOVERY`、`CMD_TOO_LONG`、`CMD_CONTAINS_NEWLINE`（spec §9.2 稱「6 個既有常數碼」，其中只有 `AUTOBOOT_QUIET`、`READY_UNCONFIRMED` 在 constants.py；另 4 個在其他模組）。`CREDENTIALS_UNRESOLVED`、`RX_FLOOD`、`TRANSPORT_STALL`、`BOOTLOADER_STUCK` 屬 session `last_error`／分類值命名空間。

### 1.4 本區塊相關但不在 constants.py 的模組常數

| 常數 | 位置 | 值 |
|---|---|---|
| `DEFAULT_RPC_TIMEOUT_S` | `sw_core/cli.py:81` | `5.0` |
| `LONG_RPC_TIMEOUT_FLOOR_S` | `sw_core/cli.py:79` | `45.0` |
| `_LONG_RPC_METHODS` | `sw_core/cli.py:91-96` | `{"session.recover","session.attach","session.self_test","session.console_attach"}`（**不含** `file.push`／`file.pull`，#123 defer） |
| `BLOCKING_RPC_METHODS` | `sw_core/daemon.py:20-36` | 含 `"file.push","file.pull"`、`"command.result_tail","result.tail","log.tail_raw","log.tail_text","wal.range"`、`"health.status"`、`session.recover/attach/self_test/console_attach`；**event.\* 全不在內**（同步跑在 asyncio loop 上，且含 fsync I/O） |
| `RETRYABLE_READONLY_METHODS` | `sw_core/client.py:23-35` | 11 個（與 spec §4.2 一致），含 `session.log_status`；event.\*／file.\*／log.\*／result.tail 不在內 |
| `_RX_WINDOW_MAX_ENTRIES` | `sw_core/uart_io.py:30` | `4096` |
| `UARTBridge._rx_max_chars` | `sw_core/uart_io.py:152` | `131072`（RX 文字視窗，file transfer 依賴之） |
| `_MIN_FILE_CHUNK_TIMEOUT_S` | `sw_core/session_manager.py:68` | `5.0` |
| `_MIN_FILE_PULL_TIMEOUT_S` | `sw_core/session_manager.py:69` | `30.0` |
| `_MIN_FILE_ECHO_TIMEOUT_S` | `sw_core/session_manager.py:73` | `= DEFAULT_ECHO_TIMEOUT_S` = `5.0` |
| file_transfer / event_engine 常數 | 見 §3、§4 | |

---

## 2. `sw_core/config.py` — Profile YAML 解析

### 2.1 Dataclasses（全部 `@dataclass(frozen=True)`）

**`UartProfile`**（11-18）

| 欄位 | 型別 | 預設 |
|---|---|---|
| `baud` | int | `115200` |
| `data_bits` | int | `8` |
| `parity` | str | `"N"` |
| `stop_bits` | int | `1` |
| `flow_control` | str | `"none"` |
| `xonxoff` | bool | `False` |

**`ProfileTemplate`**（21-47）

| 欄位 | 型別 | 預設（**Python 字面值**） |
|---|---|---|
| `profile_name` | str | （必填） |
| `platform` | str | `"prpl"` |
| `prompt_regex` | str | `r"(?m)^root@prplOS:.*# "` |
| `login_regex` | str | `r"(?mi)^login:\\s*$"` → 實際字串 `(?mi)^login:\\s*$`（**雙反斜線，見 §8 D-C1**） |
| `password_regex` | str | `r"(?mi)^password:\\s*$"`（同上問題） |
| `post_login_cmd` | str | `""` |
| `ready_probe` | str | `"echo __READY__${nonce}"` |
| `username` | str\|None | `None` |
| `user_env` | str\|None | `None` |
| `pass_env` | str\|None | `None` |
| `env_file` | str\|None | `None` |
| `console_unlock_env` | str\|None | `None` |
| `console_unlock_idle_s` | float | `5.0` |
| `timeout_s` | float | `10.0` |
| `quiet_window_s` | float | `2.0` |
| `hard_timeout_s` | float | `60.0` |
| `log_dir` | str\|None | `None` |
| `uart` | UartProfile | `UartProfile()` |
| `bootloader_prompts` | tuple[str,...] | `()` |
| `max_console_line_chars` | int\|None | `None` |
| property `command_capable` | bool | `bool((ready_probe or "").strip())` |

**`SessionProfile`**（50-85）：欄位同 ProfileTemplate，另於前段加 `com: str`、`act_no: int`、`alias: str`、`device_by_id: str`、`platform: str`（**無預設**，必填）；其餘預設值與 ProfileTemplate 相同；同名 property `command_capable`。

**`LoadResult`**（306-311）：`profiles: list[SessionProfile]`、`templates: list[ProfileTemplate]`、`max_sessions: int`。

### 2.2 轉型 helper（Python 語意需逐一複製）

| 函式 | 行 | 行為 |
|---|---|---|
| `_as_int(v, default)` | 88-92 | `int(v)`；`TypeError/ValueError` → default。注意 `int(True)=1`、`int(3.9)=3`、`int("12")=12`、`int("1.5")`→default。 |
| `_as_float(v, default)` | 95-99 | `float(v)`；失敗 → default（`float("inf")` 成功；bool 轉 1.0/0.0）。 |
| `_as_opt_str(v)` | 102-106 | None→None；`str(v).strip()`，空→None。 |
| `_as_max_console_line_chars(v, default=None)` | 109-119 | None→default；`bool` 或非 int 或 `<=0` → **raise ValueError("max_console_line_chars 必須是正整數或 null")**（不吞，會穿出 `load_profiles`）。 |
| `_as_unlock_idle(v, default=5.0)` | 122-134 | None→default；bool → ValueError；`float(v)` 失敗 → ValueError；非有限或 `<=0` → ValueError（訊息 `"console_unlock_idle_s 必須是有限正數"`）。 |
| `_as_str_keep_empty(v, default)` | 137-140 | None→default；否則 `str(v).strip()`（空字串保留為空，用於 `ready_probe:""`）。 |
| `_resolve_opt_path(v, *, base_dir)` | 143-150 | `_as_opt_str`→None 則 None；`expanduser`；絕對 → `normpath`；相對 → `normpath(join(base_dir, ...))`（base_dir＝該 YAML 所在目錄）。 |
| `_load_uart(raw, default=None)` | 153-163 | 非 dict 視同 `{}`；`baud/data_bits/stop_bits`→`_as_int`；`parity=str(obj.get("parity") or base.parity).upper()`；`flow_control=str(... or base).lower()`；`xonxoff=bool(obj.get("xonxoff", base.xonxoff))`（**Python truthiness**：字串 `"false"` → True）。 |

### 2.3 `_template_from_dict(name, raw, *, base_dir)`（166-190）

| 欄位 | 來源鍵 | 規則 |
|---|---|---|
| `platform` | `platform` | `str(raw.get("platform") or "prpl").strip().lower()` |
| `prompt_regex` | `prompt_regex` | `str(... or 預設)`（**不 strip**） |
| `login_regex` | `login_regex` | `str(... or 預設).strip()` |
| `password_regex` | `password_regex` | `str(... or 預設).strip()` |
| `post_login_cmd` | `post_login_cmd` | `str(... or "").strip()` |
| `ready_probe` | `ready_probe` | `_as_str_keep_empty(..., "echo __READY__${nonce}")` |
| `username` | `username` | `_as_opt_str` |
| `user_env` | `user_env` → `username_env` → `login_env`（`or` 鏈） | `_as_opt_str` |
| `pass_env` | `pass_env` → `password_env` → `pw_env` | `_as_opt_str` |
| `env_file` | `env_file` | `_resolve_opt_path` |
| `console_unlock_env` | 同名 | `_as_opt_str` |
| `console_unlock_idle_s` | 同名 | `_as_unlock_idle`（可 raise） |
| `timeout_s`／`quiet_window_s`／`hard_timeout_s` | 同名 | `_as_float`，預設 10.0／2.0／60.0 |
| `log_dir` | `log_dir` | `_resolve_opt_path` |
| `max_console_line_chars` | 同名 | `_as_max_console_line_chars`（可 raise） |
| `uart` | `uart` | `_load_uart(raw.get("uart"))`（預設 UartProfile()） |
| `bootloader_prompts` | 同名 | list 時取其中 `isinstance(str)` 元素成 tuple；非 list → `()` |

### 2.4 `_load_templates(file_name, obj, *, base_dir)`（193-240）

- `default_name = str(obj.get("profile_name") or splitext(file_name)[0]).strip()`。
- `profiles` 為 dict：逐 `k,v`，v 非 dict 跳過，`name=str(k).strip()` 空跳過。
- `profiles` 為 list：每列 dict，`name = row.name or row.profile_name`（strip），空跳過。
- 無任何 template → 以**整個頂層 obj** 當 template（名稱 default_name）。
- `default_name` 不在 templates → `sorted(keys)[0]`。
- `ordered.sort(key=passthrough→1 else 0)`（穩定排序：passthrough 永遠最後，其餘維持 YAML 順序）。
- 回傳 `(templates_map, ordered, default_name)`。

### 2.5 `_merge_session(template, target, *, act_no, com, alias, device_by_id, base_dir, defaults_log_dir=None)`（243-303）

- `log_dir = (_resolve_opt_path(target["log_dir"]) if "log_dir" in target else template.log_dir) or defaults_log_dir`（target 明設空字串/null → 跳過 template，落到 defaults）。
- `platform`／`prompt_regex`／`login_regex`／`password_regex`／`post_login_cmd`：`target.get(x) or template.x`（login/password/post_login/platform 會 strip；platform 會 lower；prompt_regex 不 strip）。
- `ready_probe`：`_as_str_keep_empty(target.get, template.ready_probe)`。
- `username`：`target.get("username") is not None` 時 `_as_opt_str`，否則 template。
- `user_env`／`pass_env`：只要三個別名鍵**任一存在於 target** 就用 target（可能解析為 None），否則 template。
- `env_file`：`"env_file" in target` → `_resolve_opt_path`，否則 template。
- `console_unlock_env`：鍵存在 → `_as_opt_str`，否則 template。
- `console_unlock_idle_s`：`_as_unlock_idle(target.get(...), template.value)`。
- `timeout_s`／`quiet_window_s`／`hard_timeout_s`：`_as_float(target.get, template)`。
- `max_console_line_chars`：鍵存在 → `_as_max_console_line_chars`（null→None 即清除），否則 template。
- `uart`：`_load_uart(target.get("uart"), default=template.uart)`（逐欄繼承）。
- `bootloader_prompts`：**永遠 template 的**（target 不能覆寫）。

### 2.6 `load_profiles(profile_dir) -> LoadResult`（314-373）

1. `max_sessions = 16`；目錄不存在 → 空結果。
2. `sorted(os.listdir(profile_dir))`，只處理 `.yaml`／`.yml`。
3. `yaml.safe_load(fp) or {}`；非 dict 跳過。**YAML 語法錯／open 失敗不吞**（例外穿出）。
4. `defaults_obj = obj["defaults"]`（dict 才算，否則 `{}`）；`defaults_log_dir = _resolve_opt_path(defaults.log_dir)`（defaults 為空 dict 時 None）；defaults 非空時 `max_sessions = _as_int(defaults.max_sessions, max_sessions)`（**後讀的檔覆蓋前者**）。
5. `all_templates = ordered_templates`（**每個檔覆蓋前一個**：最後回傳只剩最後一個 YAML 檔的 templates，見 §8 D-C2）。
6. `targets` 為 None → 全部動態偵測（`continue`）；非 list → 跳過。
7. 每個 target（dict），`idx` 從 1 起：
   - `profile_ref = str(t.profile or t.profile_name or default_profile_name).strip()`；在**同檔** templates_map 找不到 → 跳過（無錯誤）。
   - `act_no = _as_int(t.get("act_no", idx), idx)`。
   - `com = str(t.com or f"COM{max(act_no-1,0)}").strip()`。
   - `device_by_id = str(t.device_by_id or "").strip()`；**空 → 整筆跳過**。
   - `alias = str(t.alias or f"{tpl.profile_name}+{act_no}").strip()`。
   - `_merge_session(...)` 加入 out。
8. 動態偵測 session（不在本檔）由 `session_manager.py:960-990` 由 template 直接組 `SessionProfile`：`log_dir=tpl.log_dir`（**不套 `defaults.log_dir`**），`sid=f"{profile_name}:{com}"`。

### 2.7 出貨 profile（`sw_core/assets/profiles/default.yaml`）用到的鍵

`defaults: {log_dir: "~/b-log", max_sessions: 16}`；templates：`prpl-template`(prpl)、`op3-template`(shell, env_file OPI.env, user_env SW_OPI_U/pass_env SW_OPI_P)、`brcm-template`(bcm, post_login_cmd "sh", timeout_s 15, env_file brcm.env)、`smart4R-profile`(shell, login/password `'$^'`, console_unlock_env SMART4R_CONSOLE_UNLOCK, timeout_s 120, hard_timeout_s 240)、`uboot-template`(passthrough)、`others-template`(passthrough, `ready_probe: ""`)。YAML 雙引號 `"\\s"` 解析後是正確的 `\s`。

### 2.8 Rust 承接注意

- YAML 1.1（PyYAML `safe_load`）vs YAML 1.2（serde_yaml）：PyYAML 把 `yes/no/on/off` 解成 bool；`bool(...)` 對任意非空字串為 True。`parity: N` 在兩者皆為字串。
- regex 方言：`prompt_regex`／`login_regex` 等為 **Python `re` 語法**（含 inline flags `(?m)`、`(?mi)`）。

---

## 3. Event Engine（`sw_core/event_engine/*`）

`__init__.py`：匯出 `EventEngine`、`EngineDeps`、`Rule`、`validate_rule_dict`。

### 3.1 規則 Schema（`schema.py`）

#### 合法值集合

| 名稱 | 行 | 值 |
|---|---|---|
| `_VALID_KIND` | 15 | `{"tool","agent"}` |
| `_VALID_SCOPE` | 16 | `{"spontaneous","command_output","any"}` |
| `_VALID_LEVEL` | 17 | `{"INFO","NOTYS","WARN","ERR","ENMR","CRITL"}` |
| `_VALID_PATTERN_KIND` | 18 | `{"contains","regex"}` |
| `_NAME_RE` / `_OWNER_RE` | 19-20 | `^[a-z0-9][a-z0-9-]{0,63}$`（Python `re.match`，`$` 允許結尾 `\n`） |
| `_REGEX_MAX_LEN` | 25 | `512`（字元） |
| `REGEX_MATCH_INPUT_MAX` | 26 | `4096`（字元，matcher 標準 re 路徑輸入封頂） |

#### Dataclasses（frozen）

- `Pattern(kind: str, value: str, flags: str = "")`（191-195）
- `Handler(exec: list[str] | None = None, shell: str | None = None)`（198-201）
- `Rule`（204-222）：`schema_version:int, owner:str, name:str, rule_id:str, kind:str, selectors:tuple[str,...], profile:str, level:str, pattern:Pattern, scope:str, max_fires:int|None, cooldown_ms:int, timeout_ms:int, handler:Handler, auto_enable_com_on_load:bool, debug:bool, raw:MappingProxyType`（`raw` = 原始輸入 dict 的唯讀副本，含未知鍵、**原樣**保留）。
- `RuleSchemaError(ValueError)`（225-226）。

#### `validate_rule_dict(obj) -> Rule`（234-342）— 檢查順序與錯誤訊息（verbatim）

| # | 檢查 | 失敗訊息 |
|---|---|---|
| 1 | `isinstance(obj, dict)` | `rule must be a JSON object` |
| 2 | `obj.get("schema_version") == 1`（Python `==`：`True`、`1.0` 也通過） | `schema_version must be 1` |
| 3 | `owner=str(obj.get("owner") or "")` 符合 `_OWNER_RE` | `owner must match [a-z0-9-]{1,64}` |
| 4 | `name=str(obj.get("name") or "")` 符合 `_NAME_RE` | `name must match [a-z0-9-]{1,64}` |
| 5 | `rid = obj.get("rule_id", f"{owner}.{name}")` 必須 `== derived_id` | `rule_id must equal '{owner}.{name}'` |
| 6 | `kind in _VALID_KIND` | `kind must be one of ['agent', 'tool']` |
| 7 | `selectors` 為非空 list | `selectors must be a non-empty list` |
| 8 | 每個 selector 為非空 str | `each selector must be a non-empty string` |
| 9 | `profile = str(obj.get("profile","ALL"))`（無檢查）；`level = str(obj.get("level","INFO"))` ∈ `_VALID_LEVEL` | `level must be one of ['CRITL', 'ENMR', 'ERR', 'INFO', 'NOTYS', 'WARN']` |
| 10 | `pattern` 為 dict | `pattern must be an object` |
| 11 | `pattern.kind ∈ {contains, regex}` | `pattern.kind must be 'contains' or 'regex'` |
| 12 | `pattern.value` 為非空 str | `pattern.value must be non-empty string` |
| 13 | （regex）`len(value) <= 512` | `pattern.value regex 過長（上限 512 字元，避免 ReDoS）` |
| 14 | （regex）`_flags_from_string(flags)`（i→IGNORECASE、s→DOTALL、m→MULTILINE，其他字元） | `unsupported regex flag: {ch}` |
| 15 | （regex）`re.compile(value, flags)` | `pattern.value is not a valid regex: {re.error}` |
| 16 | （regex，且 `_re2_compile(value, flags) is None`＝re2 不可用或不支援該構造）`_sre_parse.parse` 後 `_maybe_redos_suspicious` 為 False | `pattern.value 結構上可能 catastrophic backtracking（ReDoS：≥2 個回溯量詞／backreference／量詞單元含 alternation 或巢狀量詞）；無 google-re2 時一律拒絕。請改寫為單一非歧義量詞形式，或安裝 google-re2（pip install 'serialwrap[redos]'）以線性引擎免疫此限制` |
| 17 | `scope = str(obj.get("scope","spontaneous"))` ∈ `_VALID_SCOPE` | `scope must be one of ['any', 'command_output', 'spontaneous']` |
| 18 | `max_fires`：`None` 或（`isinstance(int)` 且 `>=0`；**bool 通過**） | `max_fires must be null or non-negative int` |
| 19 | `cooldown_ms = int(obj.get("cooldown_ms", 0))`、`timeout_ms = int(obj.get("timeout_ms", 10000))`（Python `int()`：`"5000"`→5000、`1.5`→1、`True`→1；`"abc"`→**ValueError（非 RuleSchemaError）**、`null`→**TypeError**） | — |
| 20 | `cooldown_ms >= 0` | `cooldown_ms must be >= 0` |
| 21 | `timeout_ms > 0` | `timeout_ms must be > 0` |
| 22 | `handler` 為 dict | `handler must be an object` |
| 23 | `bool(exec) ^ bool(shell)`（空 list／空字串視為缺） | `handler must have exactly one of 'exec' (list[str]) or 'shell' (str)` |
| 24 | exec 非 None：list 且每元素為非空 str | `handler.exec must be a non-empty list of non-empty strings` |
| 25 | 否則 shell 為非空 str | `handler.shell must be non-empty string` |
| — | `auto_enable_com_on_load = bool(obj.get(..., True))`；`debug = bool(obj.get(..., False))`（Python truthiness） | — |

- **`pattern.flags` 只有 regex 會被驗證**；`contains` 的 flags 不驗證，但 matcher 建構時 `_flags_from_string` 仍會 raise（見 §8 D-E5）。`contains` + `flags:"i"` 在 matcher 為大小寫不敏感比對。
- 回傳 `Rule.rule_id = derived_id`、`schema_version=1`、`selectors=tuple(...)`、`max_fires=原值`（可能是 bool）。

#### ReDoS 判定（無 re2 時）（114-188）

- `_is_variable_repeat(mn, mx)`：`mn != mx`。
- `_repeats_at_least_twice(mn, mx)`：`mx == MAXREPEAT or (int 且 mx >= 2)`。
- `_has_backref(parsed)`：AST 中任一 `GROUPREF`。
- `_ambiguity_score(sp)`：逐節點累加：
  - MAX_REPEAT/MIN_REPEAT：`body_score = score(body)`；可變 → `+1+body_score`；固定且 ≥2 → `+2*body_score`；否則 `+body_score`。
  - POSSESSIVE_REPEAT：`+score(body)`。
  - BRANCH：`+1 + max(score(各分支))`。
  - SUBPATTERN／ATOMIC_GROUP／ASSERT／ASSERT_NOT：`+score(子)`。
  - 其他具子 SubPattern 的 op（如 GROUPREF_EXISTS）：`+max(score(children))`。
  - 純 atom：0。
- `_maybe_redos_suspicious = score >= 2 or has_backref`。
- 實測：`a+b+` 被拒（score 2）；`temp=(\d+)C`、`Kernel panic` 通過；`\d+\.\d+`、`(foo|bar)(baz|qux)` 會被拒（保守誤拒）。
- 有 re2 且 re2 能編譯（無 backref/lookaround）→ 跳過此檢查（零限制）。re2 以 inline `(?{flags})` 前綴帶 flags（`_re2_compile`，61-69）。

### 3.2 Matcher（`matcher.py`）

- `MatchResult(matched_text: str, groups: list[str])`。
- `PatternMatcher(pattern)`（26-58）：`contains` → `re.escape(value)`；regex → value；先 `_flags_from_string(flags)`（可 raise）；`_re2_compile(raw, flags)` 成功用 re2（`_engine="re2"`），否則 `re.compile(raw, flags)`（`_engine="re"`）。
  - `eval(line)`：`regex` 且 `_engine=="re"` → 只對 `line[:4096]` 求值；其餘全文。`search`（非 match），回 `MatchResult(m.group(0), list(m.groups()))`（未參與的 group 為 `None`）。
- Gate 函式：
  - `apply_cooldown(rule, counter, *, now_ms)`：`cooldown_ms <= 0 or last_fire_ts is None` → True；否則 `(now_ms - last_fire_ts) >= cooldown_ms`。
  - `apply_max_fires(rule, counter)`：`max_fires is None` → True；`counter.exhausted` → False；否則 `counter.fires < max_fires`（`max_fires=0` 永不觸發）。
  - `apply_scope(rule, *, active_cmd_id)`：`any`→True；`spontaneous`→`active is None`；`command_output`→`active is not None`。
  - `apply_profile(rule, *, com_profile)`：`profile=="ALL"`→True；否則 `com_profile == rule.profile`（比對 session 的 `profile_name`，即 template 名，如 `prpl-template`）。
- `MatcherContext` Protocol：`active_cmd_id(com)`、`com_profile(com)`、`now_ms()`、`emit_fire(fire)`、`emit_dropped(event)`、`emit_skipped(event)`、`counter_for(rule_id)`。
- `MatcherFire(rule, selector, match, wal_seq, matched_at, active_cmd_id)`；`_Item(selector, line, wal_seq)`。
- `MatcherWorker(rules, context, queue_max=1024)`（118-293）：
  - 單一 daemon thread `event-matcher`；`queue.Queue(maxsize=queue_max)`（**全域一條**，非 per-COM）。
  - `feed_line`：`put_nowait`；滿 → `get_nowait()` 丟最舊並 `emit_dropped({"type":"event_dropped","reason":"matcher_queue_overflow","selector":<被丟項>,"wal_seq":<被丟項>})`，再 put；仍滿 → 對新項 emit 同型事件。
  - `stop()`：設 stop、塞 `_Item("__stop__","",0)`、join 2.0s（逾時 log warning）。
  - `replace_rules(rules)`：鎖內換 `_rules` 並 `_rebuild_compiled()`（`{rule_id: PatternMatcher}`；任一 rule 的 PatternMatcher raise 會整個中斷，`_compiled` 維持舊值）。
  - `_run`：`get(timeout=0.1)`；`_evaluate` 例外吞掉並 log warning（`event matcher 求值失敗，丟棄此 item（selector=%s）`）。
  - `_evaluate(item)` 每條 rule 依序：
    1. selector 過濾：`"ALL" not in rule.selectors and item.selector not in rule.selectors` → skip（selector 比對的是 **COM 名**，如 `COM0`，不解析 alias）。
    2. `counter = ctx.counter_for(rule_id)`（**每行每 rule 讀一次 counter 檔**）、`now = ctx.now_ms()`、`active`、`profile`。
    3. `pm.eval(line)`；None → skip。
    4. scope → profile → max_fires → cooldown；任一不過且 `rule.debug` → `emit_skipped({"type":"match_skipped","rule_id","selector","reason": "scope_mismatch"|"profile_mismatch"|"exhausted"|"cooldown"})`。
    5. 全過 → `emit_fire(MatcherFire(..., matched_at=now, active_cmd_id=active))`。
  - 同一行可同時觸發多條 rule。

### 3.3 LineBuffer（`line_buffer.py`）與 ANSI 剝除（`sw_core/util.py`）

`LineBuffer(max_line_bytes=16*1024)`（16384 bytes）；`feed(data: bytes) -> list[str]`：

```
buf += data
loop:
  idx = buf.find(b"\n")
  if idx < 0:
      if len(buf) > max: out.append(finalize(buf[:max], truncated=True)); del buf[:max]
      break                       # 每次 feed 最多只切一段無換行的超長塊
  if idx > max:                   # 嚴格大於；恰 16384 bytes 的行不截斷
      out.append(finalize(buf[:max], truncated=True)); del buf[:idx+1]   # 丟棄其餘直到換行
  else:
      chunk = buf[:idx]; del buf[:idx+1]
      if chunk.endswith(b"\r"): chunk = chunk[:-1]
      out.append(finalize(chunk, truncated=False))
```

- `_finalize`：`decode("utf-8", errors="replace")` → `strip_ansi` → truncated 時**追加字串 `" ...truncated"`**（regex 會看到此後綴）。截斷塊不剝 `\r`。
- 無換行超長塊截斷後，剩餘部分之後會成為獨立「行」（同一實體行可切成多段）。
- `strip_ansi(text) = _ESC_RE.sub("", text)`，`_ESC_RE`（`util.py:38-44`）：

  ```
  \x1b(?:\[[0-?]*[ -/]*[@-~]|\][^\x07]*\x07|[@-Z\\-_])
  ```
  - CSI：`ESC [` + 參數 0x30-0x3F* + 中間 0x20-0x2F* + 終字 0x40-0x7E。
  - OSC：`ESC ]` … **僅 BEL(0x07) 終止**；`ESC \`（ST）終止的 OSC 只會剝掉 `ESC ]` 與 `ESC \` 兩段，中間文字保留（實測 `'\x1b]0;t\x1b\\x'`→`'0;tx'`）。
  - 單字元：`ESC` + 0x40-0x5A 或 0x5C-0x5F（含 `]`、`^`、`_`、`\`；不含 `[`）。`ESC ( B` 不剝。
- `clean_text`（`util.py:48-62`，**不同函式**，用於 agent log capture／result）：`\r\n→\n`、刪 `\r`、`_ANSI_RE`（`\x1B\[[0-9;?]*[A-Za-z]`）、`_OSC_RE`（`\x1B\][^\x07]*(\x07|\x1B\\)`）、`\b`/`\x7f` 退格刪前一字、刪 `\a`。

### 3.4 Counter（`counter.py`）

- `Counter(fires: int = 0, last_fire_ts: int | None = None, exhausted: bool = False)`；`to_json()` → `{"fires","last_fire_ts","exhausted"}`；`from_json` 用 `int()`/`bool()` 轉型。
- `CounterStore(root)`：`makedirs(root)`；檔名 `"{root}/{rule_id}.counter.json"`。
  - `load`：不存在／JSON 錯／OSError → `Counter()`。
  - `save`：寫 `path + ".tmp"`（`json.dump` 預設分隔符 `", "`/`": "`）→ flush → fsync → `os.rename`。
  - `clear`：unlink（不存在忽略）。
  - `known_rule_ids()`：列 `*.counter.json`（未被引擎使用）。
- 預設 root = `EVENTS_RUNTIME_DIR` = `~/.local/state/serialwrap/events`（**非 tmpfs**，跨 host reboot 保留）。

### 3.5 Registry（`registry.py`）

- `RuleLoadError(Exception)`；`FailedLoad(path, reason)`；`LoadResult(rules, failed)`；`DiffResult(added, removed, changed)`。
- `RuleRegistry(root)`：`makedirs(root)`；`_cache: dict[str, Rule]`（upsert 寫入快取，只供 `get` 用）。
  - `path_for(rule_id)` = `{root}/{rule_id}.json`。
  - `load_all()`：`sorted(listdir)` 只取 `*.json`；`OSError/JSONDecodeError` → `FailedLoad(full, f"read/parse failed: {exc}")`；`RuleSchemaError` → `FailedLoad(full, str(exc))`；**其他例外（`ValueError`/`TypeError` from `int()`）不攔截**（見 §8 D-E6）。**只讀 JSON**（README 稱 JSON/YAML，錯）。
  - `get(rule_id)`：cache 優先；否則讀檔；不存在 → None；錯 → `RuleLoadError`（引擎未用）。
  - `upsert(obj)`：`validate_rule_dict` → 寫 `{rule_id}.json.tmp`（`json.dump(dict(rule.raw), ensure_ascii=False, indent=2, sort_keys=True)`，無結尾換行）→ fsync → rename → cache。
  - `delete(rule_id)`：unlink 成功 True、不存在 False（**rule_id 未驗證**，見 §8 D-E9）。
  - `diff_against(previous)`：以 `load_all()` 結果比較；`changed` 判準 `current.raw != prev.raw`（dict 相等）。

### 3.6 Dispatcher（`dispatcher.py`）— 子程序模型

常數：`_STDIO_TAIL_BYTES = 4096`；`_INHERITED_ENV_KEYS = ("PATH","HOME","USER","LANG","LC_ALL")`。

`build_payload(fire, *, fire_count, bridge_generation=0, matched_line="")`（stdin JSON，欄位順序）：

```json
{"schema_version":1,"rule_id":"","rule_name":"<rule.name>","owner":"","kind":"",
 "selector":"COM0","matched_at":<epoch ms int>,"matched_line":"<見下>","matched_text":"",
 "match_groups":[...],"scope":"","active_cmd_id":null|"foreground","wal_seq":<int>,
 "bridge_generation":0,"fire_count":<int>,"level":"","profile":""}
```

- 實際呼叫時 `matched_line = fire.match.matched_text`（**不是整行**）、`bridge_generation` 恆 `0`（見 §8 D-E2）。
- stdin bytes：`json.dumps(payload, ensure_ascii=False) + "\n"`（預設分隔符 `", "`、`": "`），UTF-8。

`build_env(fire, *, fire_count)`：只繼承 `_INHERITED_ENV_KEYS` 中存在者（其餘環境全清空），再加：

| env | 值 |
|---|---|
| `SERIALWRAP_EVENT_SCHEMA_VERSION` | `"1"` |
| `SERIALWRAP_EVENT_RULE_ID` | rule_id |
| `SERIALWRAP_EVENT_OWNER` | owner |
| `SERIALWRAP_EVENT_KIND` | kind |
| `SERIALWRAP_EVENT_SELECTOR` | selector（COM 名） |
| `SERIALWRAP_EVENT_MATCHED_AT` | `str(matched_at)` |
| `SERIALWRAP_EVENT_MATCHED_TEXT` | matched_text；`len > 4096`（**字元**）→ 前 4096 字元 + `" ...truncated"` |
| `SERIALWRAP_EVENT_FIRE_COUNT` | `str(fire_count)` |
| `SERIALWRAP_EVENT_WAL_SEQ` | `str(wal_seq)` |
| `SERIALWRAP_EVENT_LEVEL` | level |
| `SERIALWRAP_EVENT_SCOPE` | scope |

`Dispatcher(*, event_emit, counter_store, per_daemon_max=8)`：

- `start()`：`ThreadPoolExecutor(max_workers=per_daemon_max, thread_name_prefix="event-dispatch")`；`stop()`：`shutdown(wait=True)`（daemon 關閉會等所有 handler 結束，最長約 `timeout_ms+1s`）。
- `dispatch(fire, *, matched_line)`（鎖內）：
  1. `rule_id in _busy_rules`（**per-rule 並行度 1**）→ emit `{"type":"event_dropped","reason":"per_rule_busy","rule_id","selector"}`，return。
  2. `_inflight >= per_daemon_max` → emit `{"type":"event_dropped","reason":"per_daemon_saturated","rule_id","selector"}`，return（**丟新**，不是 drop_oldest）。
  3. 加入 busy、`_inflight += 1`、submit；submit 例外 → 回滾並 emit `{"type":"fire_failed","rule_id","selector","reason":str(exc)}`。
- `_run_handler(fire, matched_line)`：
  1. `counter = load(rule_id)`；`new_fires = counter.fires + 1`。
  2. argv：`exec` 原樣；或 `["/bin/sh", "-c", shell]`（**POSIX 專屬**）。
  3. `subprocess.Popen(argv, stdin/stdout/stderr=PIPE, env=env, close_fds=True, start_new_session=True)`（cwd 繼承 daemon）。
     - `OSError` → emit `{"type":"fire_failed","rule_id","selector","reason":str(exc)}`，`_post_fire_counter_update`，return。
  4. `proc.communicate(stdin_bytes, timeout=timeout_ms/1000)`：
     - 正常 → emit `{"type":"fire_completed","rule_id","selector","exit_code":returncode,"duration_ms":int,"stdout_tail":stdout[-4096:].decode("utf-8","replace"),"stderr_tail":同,"fire_count":new_fires,"wal_seq"}`（非 0 exit 也是 `fire_completed`）。
     - `TimeoutExpired` → `os.killpg(os.getpgid(pid), SIGTERM)`（`ProcessLookupError/PermissionError` 忽略）→ `wait(1.0)` → 仍逾時 `killpg SIGKILL` → `communicate()`（例外忽略）→ emit `{"type":"fire_timeout","rule_id","selector","duration_ms","fire_count","wal_seq"}`。
  5. `_post_fire_counter_update(rule_id, new_fires)`：重新 load → `fires=new_fires`、`last_fire_ts=int(time.time()*1000)`（**handler 結束時刻**）→ save。
  6. finally：移出 busy、`_inflight -= 1`。
- `DispatcherContext`：空類別（測試型別用）。
- 子程序無 UART fd（`close_fds=True`），若要下命令須自行呼叫 `serialwrap cmd submit`（與 spec §5.5 一致）。

### 3.7 EventEngine（`engine.py`）

- `BridgeQueries` Protocol：`active_cmd_id_for(com)`、`profile_for(com)`、`known_coms()`。實作者為 `SessionManager`（`session_manager.py:1007-1027`）：
  - `active_cmd_id_for`：找 `profile.com == com` 的 session；無 → None；`session.foreground_busy` → 字串 **`"foreground"`**（不是實際 cmd_id），否則 None。
  - `profile_for`：`session.profile.profile_name`。
  - `known_coms`：`sorted(s.profile.com for s in sessions)`。
- `EngineDeps(events_dir, runtime_dir, log_path, bridge, queue_max=1024, per_daemon_max=8)`。
- `EventEngine.__init__`：`RuleRegistry(events_dir)`、`CounterStore(runtime_dir)`、`EventLogger(log_path)`（rotate 10 MiB／3 份）、`_enabled_coms: set[str]`、`_rules`。
- `start()`：建 Dispatcher 並 start → `load_all()`；每個 failed 寫 `{"type":"rule_load_failed","path","reason"}`；每條 rule 寫 `{"type":"rule_loaded","rule_id"}`；`auto_enable_com_on_load` 時對每 selector：`"ALL"` → 對**當下** `known_coms()` 每個 `_mark_com_enabled(com,"auto")`，否則 `_mark_com_enabled(sel,"auto")` → 建 MatcherWorker 並 start。
- `stop()`：matcher.stop → dispatcher.stop。
- `feed_line(com, line, wal_seq)`：`com not in _enabled_coms` → 丟棄；否則 `matcher.feed_line`。
- `rule_set(obj) -> Rule`：`registry.upsert`（先落盤）→ 取代同 rule_id → `matcher.replace_rules` → auto_enable 處理（triggered_by `"rule_set"`）→ 寫 `rule_loaded`。
- `rule_delete(rule_id) -> bool`：delete 成功 → `counters.clear` → 移除 → replace_rules → 寫 `{"type":"rule_unloaded","rule_id"}`。不 disable 任何 COM。
- `rule_list(*, selector=None, owner=None) -> list[dict]`：來源為**記憶體中**規則；selector 過濾：`selector not in r.selectors and "ALL" not in r.selectors` → skip；每筆：`{"rule_id","owner","kind","selectors":[...],"fires","exhausted","last_fire_ts"}`。
- `rule_get(rule_id) -> dict|None`：記憶體中找；`{"rule": dict(rule.raw), "counter": counter.to_json()}`。
- `reload() -> dict`：`diff_against(previous)`（內部 load_all）＋再一次 `load_all()`（failed 只記第二次）→ 取代 `_rules` → removed 的 counter 清除 → added 的 auto_enable（triggered_by `"reload"`）→ log：added/changed 各寫 `rule_loaded`、removed 寫 `rule_unloaded` → 回 `{"added":[ids],"removed":[ids],"changed":[ids]}`（set 差集順序，非排序）。
- `com_enable(com)`：`_mark_com_enabled(com,"manual")` → 回 `com_status(com)`。
- `com_disable(com)`：移出 enabled；`affected = [rules where com in selectors or "ALL" in selectors]`；每個 load→clear→寫 `{"type":"counter_reset","rule_id","previous_fires","triggered_by":"com_disable"}`；寫 `{"type":"com_disabled","selector":com,"triggered_by":"manual"}`（即使原本未啟用也寫）→ 回 `com_status(com)`。
- `com_status(com=None)`：None → `{"coms": sorted(enabled)}`；否則 `{"selector":com,"enabled":bool,"active_rules":[rule_id where com in selectors or "ALL" in selectors]}`。
- `reset(*, rule_id=None, selector=None) -> int`：rule_id 優先（**不檢查是否存在**，直接 clear 並計 1）；否則 selector 對應規則；都 None → 0；每個寫 `counter_reset`（triggered_by `"reset"`）。
- `tail(**filters)`：`EventLogger.tail`。
- `_mark_com_enabled(com, triggered_by)`：已在集合 → no-op；否則加入並寫 `{"type":"com_enabled","selector":com,"triggered_by":<"auto"|"rule_set"|"reload"|"manual">}`。
- `MatcherContextImpl`：
  - `now_ms() = int(time.time()*1000)`（wall clock）。
  - `emit_fire(fire)`：寫 `{"type":"match_recorded","rule_id","selector","wal_seq","matched_text"}` → load counter → 若 `max_fires is not None and counter.fires + 1 >= max_fires` → `exhausted=True` 並 save → `dispatcher.dispatch(fire, matched_line=fire.match.matched_text)`。
  - `emit_dropped`／`emit_skipped`：直接寫 log。
  - `counter_for`：`counters.load`。
- `_enabled_coms` **僅存記憶體**，daemon 重啟只依 `auto_enable_com_on_load` 重建。

### 3.8 EventLogger（`event_log.py`）

- `EventLogger(path, rotate_bytes=10*1024*1024, backup_count=3)`；`makedirs(dirname)`。
- `write(event)`：無 `"ts"` 則複製並加 `ts = int(time.time()*1000)`；`json.dumps(event, ensure_ascii=False) + "\n"`（**預設分隔符帶空白、不 sort_keys**）；鎖內 `_rotate_if_needed(len)` → `open("ab")` 寫 → flush → **fsync（每筆）**。
- rotate：`size + incoming > rotate_bytes` → `path.{i}`→`path.{i+1}`（i=backup_count-1..1）→ `path`→`path.1`（最多保留 `.1`～`.3`）。
- `tail(*, rule_id=None, selector=None, since_ts=None, n=None)`：讀**現行檔**全部行（不含 `.1..3`）；JSON 錯行略過；過濾 `rule_id`、`selector` 相等、`obj.get("ts",0) >= since_ts`；`n is not None` → `out[-n:]`（**`n=0` 回全部**）。

#### 事件類型總表（events.ndjson，皆另含 `ts`）

| `type` | 欄位 | 來源 |
|---|---|---|
| `rule_load_failed` | `path`,`reason` | start/reload |
| `rule_loaded` | `rule_id` | start/rule_set/reload(added,changed) |
| `rule_unloaded` | `rule_id` | rule_delete/reload(removed) |
| `com_enabled` | `selector`,`triggered_by` | `_mark_com_enabled` |
| `com_disabled` | `selector`,`triggered_by:"manual"` | com_disable |
| `counter_reset` | `rule_id`,`previous_fires`,`triggered_by:"com_disable"|"reset"` | |
| `match_recorded` | `rule_id`,`selector`,`wal_seq`,`matched_text` | emit_fire |
| `match_skipped` | `rule_id`,`selector`,`reason:"scope_mismatch"|"profile_mismatch"|"exhausted"|"cooldown"` | 僅 `debug:true` |
| `event_dropped` | `reason:"matcher_queue_overflow"`+`selector`,`wal_seq`；或 `reason:"per_rule_busy"|"per_daemon_saturated"`+`rule_id`,`selector` | |
| `fire_failed` | `rule_id`,`selector`,`reason` | spawn OSError / submit 失敗 |
| `fire_completed` | `rule_id`,`selector`,`exit_code`,`duration_ms`,`stdout_tail`,`stderr_tail`,`fire_count`,`wal_seq` | |
| `fire_timeout` | `rule_id`,`selector`,`duration_ms`,`fire_count`,`wal_seq` | |

### 3.9 Service 接線（`sw_core/service.py`）

- 建構（251-258）：`EventEngine(EngineDeps(events_dir=EVENTS_DIR, runtime_dir=EVENTS_RUNTIME_DIR, log_path=EVENTS_LOG_PATH, bridge=self._sessions))`；`_engine_line_buffers: dict[str, LineBuffer]`（per COM，懶建立，**永不清除／重設**）。
- `start()`（508-509）：`self._engine.start()` 後 `self._sessions.add_rx_observer(self._engine_rx_observer)`，**然後**才 `watcher.start()`／`bootstrap_attach()` → 啟動時 `known_coms()` 只含 YAML explicit targets（動態偵測 session 尚未建立）。
- `stop()`（525）：`self._engine.stop()`。
- `_engine_rx_observer(com, data, wal_seq)`（298-305）：取/建 LineBuffer → `for line in buf.feed(data): engine.feed_line(com, line, wal_seq)`（LineBuffer 對**所有** COM 都在跑，enable 檢查在 feed_line）。
- RX observer 呼叫鏈：`UARTBridge._handle_serial_rx`（`uart_io.py:681-685`）先 `wal.append(dir="RX")`、`_append_rx_text`，再 `_on_rx_data(data)` → `SessionManager._on_bridge_rx`（`session_manager.py:2385-2399`）在 `foreground_busy` gate **之前**呼叫 observers：`com = session.profile.com`（session 不存在時用 session_id）、`wal_seq = self._wal.current_seq`（全域 WAL seq 快照，非該筆 RX 的精確 seq；同 chunk 切出的所有行共用同值）；observer 例外吞掉。
- 並無 `dir == "RX"` 判斷：observer 收到的就是 bridge 讀到的原始 RX bytes。

### 3.10 Event RPC（`service.py:1133-1172`）— 全部在 asyncio loop 上同步執行

| method | params | 成功回應 | 失敗 |
|---|---|---|---|
| `event.rule_set` | 整個 params 就是 rule 物件 | `{"ok":true, **rule.raw}`（raw 原樣展開；**raw 若含 `ok` 鍵會覆蓋**） | `ValueError`（含 RuleSchemaError）→ `{"ok":false,"error_code":"INVALID_RULE_SCHEMA","error":str(e)}`；TypeError/OSError → RPC 層 `{"ok":false,"error_code":"EXCEPTION","message":...}` |
| `event.rule_delete` | `rule_id` | `{"ok":true,"deleted":bool}` | — |
| `event.rule_list` | `selector?`,`owner?` | `{"ok":true,"rules":[...]}` | — |
| `event.rule_get` | `rule_id` | `{"ok":true,"rule":{...},"counter":{...}}` | `{"ok":false,"error_code":"RULE_NOT_FOUND"}` |
| `event.com_enable` | `selector`（`str(... or "")`，不驗證） | `{"ok":true,"selector","enabled","active_rules"}` | — |
| `event.com_disable` | `selector` | 同上 | — |
| `event.com_status` | `selector?`（None→全部） | `{"ok":true,"coms":[...]}` 或 `{"ok":true,"selector","enabled","active_rules"}` | — |
| `event.reset` | `rule_id?`,`selector?` | `{"ok":true,"cleared":int}` | — |
| `event.reload` | — | `{"ok":true,"added":[],"removed":[],"changed":[]}` | 非 RuleSchemaError 的 load 例外 → EXCEPTION |
| `event.tail` | `rule_id?`,`selector?`,`since_ts?`,`n?` | `{"ok":true,"entries":[...]}` | `since_ts` 非數字 → EXCEPTION |

所有回應另由 `SerialwrapService.rpc()` `setdefault("version", <daemon version>)`；RPC 框架另補 `id`。

### 3.11 Event CLI（`cli.py:2299-2338`、`_dispatch_event` 979-1078、`_EVENT_CMD_METHOD` 965-976）

| 子命令 | 參數 | RPC | params |
|---|---|---|---|
| `event add` | `--file`（必填） | `event.rule_set` | `json.load(open(file, encoding="utf-8"))`（CLI 端讀檔） |
| `event rm` | `rule_id`（位置參數） | `event.rule_delete` | `{"rule_id"}` |
| `event list` | `--selector`、`--owner` | `event.rule_list` | 兩者（未給為 null） |
| `event show` | `rule_id` | `event.rule_get` | `{"rule_id"}` |
| `event enable` | `--selector`（必填） | `event.com_enable` | `{"selector"}` |
| `event disable` | `--selector`（必填） | `event.com_disable` | `{"selector"}` |
| `event status` | `--selector` | `event.com_status` | `{"selector"}` |
| `event reset` | `--rule-id` \| `--selector`（互斥、必填其一，argparse 層） | `event.reset` | `{"rule_id","selector"}` |
| `event reload` | — | `event.reload` | `{}` |
| `event tail` | `--rule-id`、`--selector`、`-n`（int，預設 **50**）、`--since`（int） | `event.tail` | `{"rule_id","selector","n","since_ts"}` |

- timeout：`_effective_timeout_s`（未給 `--timeout` → 5.0s）；**不轉發 `--retries`**。
- 輸出：stdout 印回應 JSON（`_print`）；`_warn_version_mismatch`；`_mirror_err(result, context=<method>)` 於 ok=false 時寫 stderr 一行 `serialwrap: {method} failed: {error_code}[: {message}][（hint: {hint}）]`（`INVALID_RULE_SCHEMA` 的說明放在 `error` 欄位，**不會**出現在 stderr 行）。
- exit：ok → 0；ok=false → 2；未知子命令 → `{"ok":false,"error_code":"UNKNOWN_EVENT_CMD","cmd":...}` exit 2（argparse `required=True` 使其實際不可達）；`_EndpointResolutionError` → `_print_endpoint_resolution_error`；其他例外（如 `--file` 不存在／非 JSON）→ `{"ok":false,"error_code":"INTERNAL_ERROR","message":str(exc)}` exit **1**。

### 3.12 檔案佈局（Event）

| 用途 | 預設路徑 | 格式 |
|---|---|---|
| 規則 | `~/.serialwrap/events.d/<owner>.<name>.json` | JSON，indent=2，sort_keys，無結尾換行；暫存 `.json.tmp` |
| Counter | `~/.local/state/serialwrap/events/<rule_id>.counter.json` | `{"fires": n, "last_fire_ts": ms|null, "exhausted": bool}`；暫存 `.tmp` |
| 事件 log | `~/.local/state/serialwrap/events/events.ndjson`（+`.1..3`） | NDJSON，每筆 fsync |

---

## 4. File Transfer（`sw_core/file_transfer.py`）

### 4.1 常數

| 名稱 | 行 | 值 |
|---|---|---|
| `_SENTINEL_BEGIN` | 22 | `"===SW_XFER_BEGIN==="` |
| `_SENTINEL_END` | 23 | `"===SW_XFER_END==="` |
| `_TOOL_PROBE_PAYLOAD` | 25 | `base64("sw-probe")` = `"c3ctcHJvYmU="` |
| `_DECODER_B64_SENTINEL` | 26 | `"SW_XFER_DECODER_B64_42"` |
| `_DECODER_OPENSSL_SENTINEL` | 27 | `"SW_XFER_DECODER_OPENSSL_42"` |
| `_ENCODER_B64_SENTINEL` | 28 | `"SW_XFER_ENCODER_B64_42"` |
| `_ENCODER_OPENSSL_SENTINEL` | 29 | `"SW_XFER_ENCODER_OPENSSL_42"` |
| `_CHUNK_SENTINEL` | 30 | `"SW_XFER_CHUNK_42"` |
| `_MD5_SENTINEL` | 31 | `"SW_XFER_MD5_42"` |
| `_MV_SENTINEL` | 32 | `"SW_XFER_MV_42"` |
| `_CLEANUP_SENTINEL` | 33 | `"SW_XFER_CLEANUP_42"`（定義但成功與否不檢查） |
| `_ENCODER_FAILURE_SENTINEL` | 34 | `"SW_XFER_ENCODER_FAILED_42"` |
| `_DECODERS` / `_ENCODERS` | 35-36 | `("base64","openssl")`（探測優先序） |
| `DEFAULT_CHUNK_SIZE` | 43 | `512`（raw bytes） |
| `DEFAULT_ECHO_SLICE_SIZE` | 47 | `64`（字元） |
| `DEFAULT_ECHO_TIMEOUT_S` | 48 | `5.0` |
| `_ACK_MODES` | 60 | `("auto","echo","none")` |

### 4.2 Runtime sentinel 技巧

`_runtime_sentinel(prefix) = f"printf '%s\\n' '{prefix}'$((6*7))"` → 板端執行後印出 `{prefix}42` + LF；command echo 中只出現 `'…_'$((6*7))`，不含完整 sentinel。Pull 的 BEGIN/END marker 以 `_marker_expression` quote-split：`'===SW_XFER_'BEGIN'==='`（marker 必須以 `===SW_XFER_` 開頭、`===` 結尾，否則 ValueError）。

### 4.3 板端命令樣板（verbatim；以 `shlex.quote` 引用）

```
# decoder probe（282 bytes）
if test "$(printf '%s' 'c3ctcHJvYmU=' | base64 -d 2>/dev/null)" = 'sw-probe'; then printf '%s\n' 'SW_XFER_DECODER_B64_'$((6*7)); elif test "$(printf '%s' 'c3ctcHJvYmU=' | openssl enc -base64 -d -A 2>/dev/null)" = 'sw-probe'; then printf '%s\n' 'SW_XFER_DECODER_OPENSSL_'$((6*7)); fi

# encoder probe（273 bytes）
if test "$(printf '%s' 'sw-probe' | base64 2>/dev/null)" = 'c3ctcHJvYmU='; then printf '%s\n' 'SW_XFER_ENCODER_B64_'$((6*7)); elif test "$(printf '%s' 'sw-probe' | openssl enc -base64 2>/dev/null)" = 'c3ctcHJvYmU='; then printf '%s\n' 'SW_XFER_ENCODER_OPENSSL_'$((6*7)); fi

# chunk（op 第 0 段 ">"，其餘 ">>"）
printf '%s' <shlex(b64)> | <base64 -d | openssl enc -base64 -d -A> <op> <shlex(tmp)> && printf '%s\n' 'SW_XFER_CHUNK_'$((6*7))

# md5 / mv / cleanup
md5sum <shlex(path)> && printf '%s\n' 'SW_XFER_MD5_'$((6*7))
mv <shlex(tmp)> <shlex(remote)> && printf '%s\n' 'SW_XFER_MV_'$((6*7))
rm -f <shlex(tmp)> && printf '%s\n' 'SW_XFER_CLEANUP_'$((6*7))

# pull（encoder：base64 | openssl enc -base64，後者保留換行）
printf '%s\n' '===SW_XFER_'BEGIN'===' && <encoder> < <shlex(remote)> && printf '%s\n' '===SW_XFER_'END'===' || printf '%s\n' 'SW_XFER_ENCODER_FAILED_'$((6*7))
```

- chunk 空 payload 樣板長度：base64 `>` 99 bytes、openssl `>>` 116 bytes（tmp 名 28 字元時）。512B chunk → base64 684 字元 → 整行約 783～800 bytes（file 內註解「≈741」已過時）。
- 暫存檔名：`/tmp/.sw_upload_{uuid4().hex[:12]}`；`cmd_id_prefix = f"ft-{uuid4().hex[:8]}"`；各命令 cmd_id：`{prefix}-probe`、`{prefix}-{idx}`、`{prefix}-md5`、`{prefix}-mv`、`{prefix}-cleanup`、`{prefix}-b64`（pull）。
- `shlex.quote` 必須逐字複製：空字串 → `''`；不含 `[^\w@%+=:,./-]`（ASCII）→ 原樣；否則 `'` + `s.replace("'", "'\"'\"'")` + `'`。base64 字元集全為安全字元 → 不加引號。

### 4.4 RX 判定 helper

- `_strip_command_echo(text, command)`：`text.startswith(command)` → 去掉，再去一個 `\r\n` 或 `\n`；否則原文。
- `_runtime_sentinel_seen(text, sentinel, *, command)`：去 echo 後 `re.search(rf"(?:^|\r?\n){re.escape(sentinel)}(?:\r?\n|$)")`（須獨立成行）。
- `_failure_sentinel_seen`：`re.search(rf"{re.escape(sentinel)}(?:\r?\n|$)")`（可黏在 partial 輸出後）。
- `_probe_shell_error`：`(?i)(?:syntax error|bad substitution|unterminated|permission denied)`。
- `_extract_between_sentinels(text, *, command)`：去 echo；`rfind` BEGIN 與 END（取**最後**一組）；缺或 `end <= begin` → None；內容 `strip_ansi` 後 `re.sub(r"\s+","",…)`。
- `_remote_md5`：送 md5 命令、等 prompt（逾時 → None）、須見 `_MD5_SENTINEL`（否則 None）；在 sentinel 之前的文字逐行 `re.match(r"\s*(?:\\)?([0-9a-fA-F]{32})(?:\s|$)")` 取第一欄並 `.lower()`。
- `_cleanup_remote`：送 cleanup、等 prompt，結果忽略。

### 4.5 `push_file(bridge, local_path, remote_path, *, chunk_size=512, timeout_s=10.0, prompt_regex, source="file_transfer", ack_mode="auto", echo_slice_size=64, echo_timeout_s=5.0, max_console_line_chars=None) -> dict`（63-260）

依序（任何 TX 前的檢查都零副作用）：

1. `ack_mode ∉ _ACK_MODES` → `{"ok":false,"error_code":"INVALID_ARGS","ack_mode":...}`。
2. `_validate_line_budget(max_console_line_chars)`：bool/非 int → `{"ok":false,"error_code":"INVALID_ARGS","max_console_line_chars":v}`；`<=0` → `CONSOLE_LINE_LIMIT_TOO_SMALL`。
3. `chunk_size` 為 bool／非 int／`<=0` → `{"ok":false,"error_code":"INVALID_ARGS","chunk_size":...}`。
4. `not os.path.isfile(local_path)` → `{"ok":false,"error_code":"LOCAL_FILE_NOT_FOUND","local_path":...}`（**daemon 端路徑**）。
5. `paced_send = getattr(bridge,"send_command_echo_paced")`；`none` → None；`echo` 且不可呼叫 → `{"ok":false,"error_code":"ECHO_ACK_UNSUPPORTED"}`。
6. 讀整檔（記憶體）、`md5_expected = md5(data).hexdigest()`。
7. 預算檢查（`len(cmd.encode("utf-8")) > budget` 即超）：md5/mv/cleanup 固定命令 → decoder probe → 兩種 decoder×兩種 op 的空 chunk 樣板；`raw_capacity = max(0, (budget - max_overhead)//4*3)`；`data` 非空且 `raw_capacity<1` → `CONSOLE_LINE_LIMIT_TOO_SMALL`。無 budget 時 `raw_capacity = chunk_size`。錯誤形狀：`{"ok":false,"error_code":"CONSOLE_LINE_LIMIT_TOO_SMALL","max_console_line_chars":budget}`。
8. **第一個 TX**：`_probe_decoder` → 失敗 `{"ok":false,"error_code": "TRANSFER_TIMEOUT"|"TRANSFER_PROBE_FAILED"|"TARGET_DECODER_MISSING"}`。
9. `effective_chunk_size = min(chunk_size, raw_capacity)`；有 budget 時以實際 decoder、`b"\0"*min(eff, len(data))` 的 base64 再驗 `>`／`>>` 兩種。
10. `_split_chunks`（空檔 → `[b""]`，仍會送一個 chunk 建立空檔）。
11. 每個 chunk：
    - 預算超 → cleanup + `CONSOLE_LINE_LIMIT_TOO_SMALL`。
    - `pre = bridge.rx_snapshot_len()`。
    - paced：`paced_send(cmd, source, cmd_id, slice_size=echo_slice_size, echo_timeout_s)`；`ok=false` → `bridge.cancel_input_line(source)` → cleanup → `{"ok":false,"error_code":"TRANSFER_ECHO_STALL","chunks_sent":idx,"chunks_total":total,"acked_chars","sent_chars"}`。
    - 非 paced：`bridge.send_command(cmd, source, cmd_id)`。
    - `wait_for_regex_from(prompt_regex, pre, timeout_s)` 失敗 → cleanup → `{"ok":false,"error_code":"TRANSFER_TIMEOUT","chunks_sent":idx,"chunks_total":total}`。
    - 未見 `_CHUNK_SENTINEL` → cleanup → `{"ok":false,"error_code":"TARGET_DECODER_FAILED","chunks_sent":idx,"chunks_total":total}`。
12. `_remote_md5(tmp)`：None → cleanup → `{"ok":false,"error_code":"CHECKSUM_VERIFY_FAILED"}`；不符 → cleanup → `{"ok":false,"error_code":"CHECKSUM_MISMATCH","expected":md5_expected,"actual":md5_actual}`。
13. mv：預算超 → cleanup + LIMIT；等 prompt 失敗 → `{"ok":false,"error_code":"MOVE_TIMEOUT"}`；未見 MV sentinel → `{"ok":false,"error_code":"MOVE_FAILED"}`（**此兩者不 cleanup**）。
14. 成功：`{"ok":true,"bytes":len(data),"chunks":total,"md5":md5_expected,"remote_path":remote_path}`。

- **timeout 是 per-命令**（每個 chunk、probe、md5、mv 各自 `timeout_s`）；無整體上限。
- 無 resume、無進度回報、無中途 abort（傳輸中 banner 到達不中斷，自然 timeout）。

### 4.6 `pull_file(bridge, remote_path, local_path=None, *, timeout_s=30.0, prompt_regex, source="file_transfer", max_console_line_chars=None) -> dict`（263-347）

1. `local_path is None` → `os.path.basename(remote_path)`（相對 **daemon cwd**）。
2. budget 驗證；md5 命令＋encoder probe、兩種 pull 命令 預算檢查 → `CONSOLE_LINE_LIMIT_TOO_SMALL`。
3. `_probe_encoder` → `TRANSFER_TIMEOUT`／`TRANSFER_PROBE_FAILED`／`TARGET_ENCODER_MISSING`。
4. 送 pull 命令（cmd_id `-b64`）、等 prompt（**整段單一 timeout**）→ 逾時 `TRANSFER_TIMEOUT`。
5. `raw_text = rx_text_from(pre)`；擷取失敗 → 見 ENCODER_FAILED sentinel → `TARGET_ENCODER_FAILED`，否則 `PULL_PARSE_FAILED`。
6. `base64.b64decode(b64_content)`（**非 strict**：丟棄非字母表字元，僅 padding 錯才失敗）→ 失敗 `BASE64_DECODE_FAILED`。
7. `md5_local`；`_remote_md5(remote_path)` None → `CHECKSUM_VERIFY_FAILED`；不符 → `{"ok":false,"error_code":"CHECKSUM_MISMATCH","expected":md5_remote,"actual":md5_local}`。
8. `open(local_path,"wb").write(data)`（非原子、不 mkdir；OSError → RPC 層 `EXCEPTION`）。
9. 成功：`{"ok":true,"bytes":len(data),"md5":md5_local,"local_path":local_path}`（**不回傳內容**）。

- pull 大小實際上限：bridge RX 文字視窗 `_rx_max_chars=131072`；若 BEGIN 已被修剪，`rx_text_from` 降級回全窗→找不到 BEGIN→`PULL_PARSE_FAILED`（約 96 KiB raw 以上不可靠）。

### 4.7 UARTBridge 原語（`sw_core/uart_io.py`）

| 方法 | 行 | 行為 |
|---|---|---|
| `send_command(cmd, *, source, cmd_id=None)` | 1049-1053 | UTF-8（replace）編碼，不以 `\n` 結尾則補 `\n`；`send_bytes`（記 WAL TX） |
| `send_command_echo_paced(cmd, *, source, cmd_id=None, slice_size=64, echo_timeout_s=2.0)` | 1055-1105 | `body = cmd.rstrip("\r\n")`；切 `slice_size` 字元段；`pre=rx_snapshot_len()`；每段 `send_bytes(log=False)` 後 `_await_echo_progress(slices[:idx+1], pre, timeout)`；`acked < sent` → 寫**一筆** WAL TX（實際已送部分）並回 `{"ok":false,"acked_chars","sent_chars"}`；全部確認後送 `\n`（log=False）並寫一筆 WAL TX（`payload+"\n"`）→ `{"ok":true,...}`。（模組函式預設 echo_timeout 2.0，但 file_transfer 傳入 5.0 或推導值） |
| `_await_echo_progress(expected_cumulative, from_offset, timeout_s)` | 1107-1134 | 取 `rx_text_from(from_offset)` → `strip_ansi` → 去 `\r`、`\n` → 以移動起點 `find` 依序比對每段（段間允許雜訊）；完成即回累計字元數；逾時回部分；poll 0.01s（先檢查後 sleep） |
| `cancel_input_line(*, source)` | 1136-1143 | `send_bytes(b"\x15\n", source, cmd_id=None)`（Ctrl-U + LF，記 WAL） |
| `rx_snapshot_len()` | 1169-1175 | 絕對偏移 `_rx_trimmed + len(_rx_text)` |
| `rx_text_from(from_offset)` | 1208-1212 | `rel = from_offset - _rx_trimmed`；`rel <= 0` → 回全窗，否則 `_rx_text[rel:]` |
| `wait_for_regex_from(pattern, from_offset, timeout_s)` | 1218-1229 | `re.compile(pattern)`（Python re）；每 0.05s 持鎖重算 rel 並 `search`；逾時 False |
| `_append_rx_text(payload)` | 659-673 | `payload.decode("utf-8","replace")`（**未 clean**）累加；超過 131072 字元修剪頭並推進 `_rx_trimmed` |

### 4.8 SessionManager 包裝（`session_manager.py:4843-5011`）

`file_push(selector, *, local_path, remote_path, chunk_size=512, chunk_timeout_s=None, ack_mode="auto", echo_timeout_s=None, source="agent")`／`file_pull(selector, *, remote_path, local_path=None, chunk_timeout_s=None, source="agent")`：

1. 鎖內：`get_session(selector)`（session_id／com／alias）；None 或 bridge None 或 `state != "READY"` → `{"ok":false,"error_code":"SESSION_NOT_READY"}`。
2. 非 `human:` 來源 → `_agent_gate_reject_locked`（`READY_UNCONFIRMED`［帶 `hint`、`recommended_action:"self_test"`、`selector`］或 `AUTOBOOT_QUIET`［帶 `retry_after_s`］）。
3. `session.recovering` → `SESSION_RECOVERING`。
4. interactive lease：agent 對 human lease → 暫停 human raw（`suspend_interactive`／結束 `resume_interactive`）；其他 lease → `SESSION_INTERACTIVE_BUSY`。
5. `_begin_foreground_operation_locked` → 已有前景操作 → `{"ok":false,"error_code":"SESSION_BUSY","selector":com}`（file transfer **不經 arbiter**，與 `cmd submit` 競爭時回 SESSION_BUSY 而非排隊）。
6. timeout 推導：push `chunk_timeout_s` 或 `max(profile.timeout_s, 5.0)`；push echo `echo_timeout_s` 或 `max(profile.timeout_s, 5.0)`；pull `chunk_timeout_s` 或 `max(profile.timeout_s, 30.0)`。
7. 傳 `prompt_regex=profile.prompt_regex`、`max_console_line_chars=profile.max_console_line_chars`。
8. finally：resume human、`_finish_foreground_operation`（重放 deferred human bytes）。

### 4.9 File RPC（`service.py:1069-1131`，均在 `BLOCKING_RPC_METHODS`）

`file.push` params：`selector`、`local_path`、`remote_path`（皆 `str(... or "")`）、`chunk_size`（`max(1, int(params.get("chunk_size") or 512))`：0/缺 → 512、負數 → 1、`True` → 1、非數字 → INVALID_ARGS）、`chunk_timeout_s`（`float()`；`None` 表推導；`0` 會被接受）、`ack_mode`（`str(... or "auto")` ∉ `_ACK_MODES` → INVALID_ARGS）、`source`（`or "agent"`）。缺 selector/local/remote → `INVALID_ARGS`。之後 `_resolve_session_id(selector, source)`（可回 `SESSION_NOT_FOUND`、`PROFILE_NOT_COMMAND_CAPABLE`［帶 hint、session］、`SESSION_NOT_READY`［帶 session］、`READY_UNCONFIRMED`、`AUTOBOOT_QUIET`）→ `SessionManager.file_push`。RPC 不轉 `echo_timeout_s`。

`file.pull` params：`selector`、`remote_path`、`local_path`（falsy → None）、`chunk_timeout_s`、`ack_mode`（只驗白名單、不轉傳）、`source`。

### 4.10 File CLI（`cli.py:2182-2210`、2639-2664）

| 子命令 | 參數 | 預設 |
|---|---|---|
| `file push` | `--selector`(必)、`--local`(必)、`--remote`(必)、`--chunk-size`(int, `DEFAULT_CHUNK_SIZE`=512)、`--chunk-timeout`→`chunk_timeout_s`(float, None)、`--ack-mode`{auto,echo,none}(auto)、`--source`(`"agent"`) | params 一律帶 `selector,local_path,remote_path,chunk_size,ack_mode,source`；`chunk_timeout_s` 有給才帶 |
| `file pull` | `--selector`(必)、`--remote`(必)、`--local`(None)、`--chunk-timeout`、`--ack-mode`、`--source` | `local_path`/`chunk_timeout_s` 有給才帶 |

- 走 `_run_rpc`；CLI socket timeout 未給 `--timeout` 時為 **5.0s**（file.* 不在 `_LONG_RPC_METHODS`，#123 已知缺口：CLI 先 TIMEOUT、daemon 仍在傳）。
- **CLI 不讀寫本地檔**：`--local` 原樣送給 daemon，由 daemon 進程在其 host／cwd 解讀。

### 4.11 本區錯誤碼（file）

`INVALID_ARGS`、`CONSOLE_LINE_LIMIT_TOO_SMALL`、`LOCAL_FILE_NOT_FOUND`、`ECHO_ACK_UNSUPPORTED`、`TRANSFER_TIMEOUT`、`TRANSFER_PROBE_FAILED`、`TARGET_DECODER_MISSING`、`TARGET_ENCODER_MISSING`、`TRANSFER_ECHO_STALL`、`TARGET_DECODER_FAILED`、`TARGET_ENCODER_FAILED`、`PULL_PARSE_FAILED`、`BASE64_DECODE_FAILED`、`CHECKSUM_VERIFY_FAILED`、`CHECKSUM_MISMATCH`、`MOVE_TIMEOUT`、`MOVE_FAILED`；包裝層 `SESSION_NOT_READY`、`SESSION_NOT_FOUND`、`PROFILE_NOT_COMMAND_CAPABLE`、`SESSION_RECOVERING`、`SESSION_INTERACTIVE_BUSY`、`SESSION_BUSY`、`AUTOBOOT_QUIET`、`READY_UNCONFIRMED`；RPC 層 `EXCEPTION`。

---

## 5. stream／log／session log capture／wal CLI 與 RPC

### 5.1 `stream tail`（`cli.py:2144-2154`、2619-2624）

- 參數：`--selector`、`--com`（`selector = args.selector or args.com`）、`--from-seq`（int，預設 **0**）、`--limit`（int，預設 200）。
- RPC `result.tail`，params `{"from_seq","limit"}` + 有 selector 才帶 `selector`。
- **一次性請求／回應，非持續串流**。
- `result.tail`（`service.py:939-962`）：
  - 有 `cmd_id` → 等同 `command.result_tail`（`from_chunk`、`limit` 預設 200；CMD_NOT_FOUND 時 `_bg_fallback_from_arbiter`）。CLI `stream tail` 不帶 cmd_id。
  - 否則 legacy：`selector = str(params.com or params.selector or "")`；`from_seq = int(params.get("from_seq") or 0)`（**永遠 range 模式**）、`limit = int(... or 200)`（0 → 200）；selector 有值 → `get_session_state` 失敗直接回（`SESSION_NOT_FOUND`）→ `target_com = session.com`；`wal.tail_raw(from_seq, com, limit)` → `{"ok":true,"records":rows}`（**無 metadata、無 WAL_MISSING 檢查**）。

### 5.2 `log tail-raw` / `log tail-text`（`cli.py:2156-2180`、2626-2637）

- 參數：`--selector`、`--com`、`--from-seq`（預設 **None**＝latest 模式；顯式值含 0＝range）、`--limit`（200）。
- params：`{"limit"}` + `from_seq`（有給才帶）+ `selector`。
- RPC `log.tail_raw` / `log.tail_text`（`service.py:964-1021`）：
  1. selector：`com or selector`；`from_seq`：key 缺或 null → None（latest）；非整數 → `{"ok":false,"error_code":"INVALID_ARGS"}`。
  2. `limit = int(params.get("limit") or 200)`（0 → 200；非數字 → EXCEPTION）。
  3. selector → `get_session_state` → `target_com`。
  4. `wal.health()` → `_wal_missing_response`：`wal_file_exists==False and current_seq>0` → `{"ok":false,"error_code":"WAL_MISSING","wal_path","wal_dir","wal_dir_exists","wal_file_exists":false,"current_seq","hint":...}`。
  5. `wal.tail_raw_with_meta(from_seq, com, limit)`：只掃**現行** `raw.wal.ndjson`；latest＝最後 limit 筆（`truncated = total > len(rows)`）；range＝`seq > from_seq` 最舊 limit 筆（`truncated` = 之後還有）。
  6. meta：`from_seq`（latest 為 null）、`last_seq`（rows 最後 seq 或 null）、`current_seq`（health 快照）、`truncated`、`wal_path`、`wal_file_exists`。
  7. tail_raw → `{"ok":true,"records":rows,"returned":len(rows),**meta}`；tail_text → `lines = rows_to_text_lines(rows)`、`{"ok":true,"lines":lines,"returned":len(lines),**meta}`。
- WAL row 欄位（`wal.py:132-143`）：`seq, mono_ts_ns, wall_ts, com, dir, source, cmd_id, len, crc32("%08x"), payload_b64, loss_flag, meta`。
- `rows_to_text_lines(rows)`（`wal.py:338-355`）：各 row `b64decode(validate=False)` → `to_printable`（`string.printable` 中除 `\x0b`,`\x0c` 原樣，其他 `\xNN`）→ 串接 → `splitlines()`；文字以 `\n` 結尾直接回；否則補回 partial 尾段。**RX 與 TX、所有 com（未給 selector 時）混在一起**。

### 5.3 `wal export/reset/current-seq`（`cli.py:2212-2222`、2666-2673）

- `wal export --from-seq(0) --to-seq(0) --limit(1000)` → `wal.range`：`int(... or 0)`；health → WAL_MISSING 檢查；`tail_raw(from_seq, com=None, limit)`（range 模式）；`to_seq > 0` 再過濾 `seq <= to_seq`；回 `{"ok":true,"records","wal_path","wal_file_exists","available_from_seq","rotated_out": available is not None and from_seq+1 < available}`。
- `wal reset` → `wal.reset`：輪替 `raw.wal.ndjson`、`raw.mirror.log` 為 `.{%Y%m%d-%H%M%S UTC}`（非空才轉）、fsync dir、seq 歸 0 → `{"ok":true,"previous_seq","rotated_suffix"}`。
- `wal current-seq` → `{"ok":true,"seq"}`。

### 5.4 session log capture（`session log-start/log-stop/log-status`，`cli.py:2075-2080`、2569-2574）

- 參數皆 `--selector`（必填，`session_id | COMx | alias`）→ RPC `session.log_start`／`session.log_stop`／`session.log_status`（`service.py:1051-1067`）；selector 取 `selector or session_id or com or alias`，空 → `INVALID_ARGS`。
- `SessionCapture`（`session_manager.py:255-262`）：`capture_id, session_id, log_path, started_at, line_count=0, byte_count=0, status="active"`。
- `log_start`（4764-4801）：session 不存在 → `{"ok":false,"error_code":"SESSION_NOT_FOUND","selector"}`；已 active → `{"ok":true,"already_active":true,"capture_id","log_path","session":public}`；`log_dir = profile.log_dir or LOG_DIR`；`makedirs`；檔名 `f"{com}_{time.strftime('%y%m%d-%H%M%S')}.log"`（**本地時間**）；`open(path,"a",encoding="utf-8")` 失敗 → `{"ok":false,"error_code":"LOG_OPEN_FAILED","detail":str(exc)}`（`makedirs` 失敗則 → EXCEPTION）；`capture_id = uuid4()`；成功 `{"ok":true,"capture_id","log_path","session":public}`。
- `log_stop`（4803-4820）：無 capture → `{"ok":false,"error_code":"NO_ACTIVE_CAPTURE","session":public}`；成功 `{"ok":true,"capture_id","log_path","line_count","byte_count","started_at"}`（無 session 欄位）。
- `log_status`（4822-4840）：無 → `{"ok":true,"active":false,"session":public}`；有 → `{"ok":true,"active":status=="active","capture_id","log_path","line_count","byte_count","started_at","session":public}`。
- 寫入（`session_manager.py:2426-2438`）：在 `_on_bridge_rx` 中，`chunk = clean_text(data.decode("utf-8","replace"))`，**`foreground_busy` 期間不寫**（agent 前景命令與 file transfer 輸出不入 log）；`fp.write(chunk); fp.flush()`；`byte_count += len(chunk.encode())`、`line_count += chunk.count("\n")`；例外吞掉。
- 自動停止：bridge 交接／detach 路徑 `_stop_capture_locked`（`session_manager.py:1821`）。
- `session.to_public_dict()` 含 `"capture": {capture_id, log_path, status, line_count, byte_count} | null`（620-631）。

---

## 6. `serialwrap skill` 與 `sw_core/assets/skill/SKILL.md`

- CLI（`cli.py:2438-2452`、`_run_skill` 1098-1112）：`serialwrap skill [--platform {auto,linux,windows}]`（預設 auto：`sys.platform.startswith("win")` → windows）。windows → `skill/SKILL_WINDOWS.md`，其他 → `skill/SKILL.md`；`sys.stdout.write(assets.read_text(...))`（**原文 verbatim，不加換行**），exit 0；不連 daemon、不讀設定。stdout 由 `force_utf8_stdio` 保證 UTF-8。
- `assets.read_text(relpath)`（`sw_core/assets/__init__.py`）：`importlib.resources` 逐段 join 後 `read_text(encoding="utf-8")`。打包：`pyproject.toml` `package-data "sw_core.assets" = ["profiles/*","tools/*","skill/**/*"]`。
- 檔案指紋（Rust 以 `include_str!` 內嵌時的 golden）：
  - `SKILL.md`：249 行／33342 bytes，sha256 `a34dad0cc745ca18c3f4731698eec9a394c65f52397e38800826c046c869551b`。
  - `SKILL_WINDOWS.md`：181 行／7961 bytes，sha256 `556f9f5bba82defdf8b6fa6c5fc6e2d7f3491c9805c7f01aa1d9221fa7217fdc`。
- SKILL.md frontmatter：`name: serialwrap`、`description: 透過 serialwrap broker + CLI 進行多 agent UART 存取…`。章節：目的／何時該使用／何時不要使用／前置條件／標準執行順序／command_capable 與 READY 判定(#51)／裝置 handoff(#54)／MCU 韌體燒錄端點 `/dev/ttyMCU`(#55)／human_active 與 soft-preempt(#53)／FAQ(#69)／Timeout 語意(#123)／U-Boot autoboot 保護(#130)／Remote Support（含 `--remote-socket` 子節）／Event Trigger Engine／透過 UART 推送／拉取檔案／安全規則／短命令原則／最小可用範例／參考檔案。
- `setup` 另會物化 skill 到使用者位置（屬 setup 範圍，不在本盤點）。
- 依 OD-06，SKILL／help 須涵蓋所有會輸出的 error code；目前 SKILL.md 未提及本區的多數 file／event 錯誤碼（見 §8 D-S3）。

---

## 7. `sw_mcp/` — 已退役，不存在

- 本 worktree **沒有 `sw_mcp/` 目錄**，也沒有 `serialwrap-mcp` shim。已於 commit `135b5fe`「refactor(mcp): 退役 vestigial MCP adapter 並重整 MCP-coupled 測試（#59）」硬刪。
- 原貌（依 `docs/superpowers/specs/2026-06-18-serialwrap-retire-mcp-consolidate-skill-design.md`、`openspec/changes/archive/2026-06-22-retire-mcp-consolidate-skill/`）：`sw_mcp/server.py` 是**非合規的 MCP 空殼**，以 `_TOOL_DEFS`／`_TOOL_MAP` 把 `serialwrap_*` tool 1:1 對映到 CLI/RPC，三個 host 都沒註冊、零腳本呼叫。
- Rust 承接：**不需實作**；spec §3.2／§4.1 也未列 MCP 模組（一致）。SKILL.md 明言「不存在 MCP tool」。`docs/design-file-transfer.md`、`openspec/changes/archive/2026-04-27-event-trigger-37/design.md` 等歷史文件仍提 MCP，屬歷史快照。

---

## 8. 差異與疑點清單（spec／文件 vs 實際程式碼）

### 8.1 Spec 宣稱 vs 程式碼

| ID | Spec 位置 | Spec 宣稱 | 實際程式碼 |
|---|---|---|---|
| D-S1 | §9.2 錯誤碼 71 個既有字面碼 | 視為完整盤點 | **漏列 3 個 file transfer 碼**：`TRANSFER_PROBE_FAILED`、`TARGET_DECODER_MISSING`、`TARGET_ENCODER_MISSING`（`file_transfer.py:521-522、543-544`；僅此檔輸出，SKILL.md 亦未記載）。 |
| D-S2 | §9.2「6 個既有常數碼」 | 暗示皆為常數 | `constants.py` 只有 `ERROR_AUTOBOOT_QUIET`、`ERROR_READY_UNCONFIRMED` 屬此 6 個；另有 `ERROR_CREDENTIALS_UNRESOLVED`、`ERROR_RX_FLOOD`、`ERROR_TRANSPORT_STALL`、`ERROR_BOOTLOADER_STUCK` 屬 `last_error`／分類，§9.3 只列 `CREDENTIALS_UNRESOLVED`。 |
| D-S3 | OD-06 | 所有輸出 error code 都要有 help/SKILL 說明 | SKILL.md 未說明 `INVALID_RULE_SCHEMA`、`RULE_NOT_FOUND`、`UNKNOWN_EVENT_CMD`、`TRANSFER_*`、`TARGET_*`、`CHECKSUM_*`、`MOVE_*`、`PULL_PARSE_FAILED`、`BASE64_DECODE_FAILED`、`ECHO_ACK_UNSUPPORTED`、`CONSOLE_LINE_LIMIT_TOO_SMALL`、`LOCAL_FILE_NOT_FOUND`、`LOG_OPEN_FAILED`、`NO_ACTIVE_CAPTURE`、`SESSION_BUSY`、`EXCEPTION` 等（現況缺口）。 |
| D-S4 | §5.5 | 「將 `dir == "RX"` 切分為行」 | 沒有 dir 判斷；engine 收的是 `UARTBridge._handle_serial_rx` 的原始 RX bytes（經 SessionManager RX observer），`wal_seq` 是**全域 `current_seq` 快照**而非該筆 RX 的 seq。語意相當，但 wal_seq 精確度要在契約中講明。 |
| D-S5 | §5.5 | 「每個受管 COM 埠配備一個獨立 LineBuffer」 | 是 per-COM，但 matcher queue 是**全 daemon 單一** 1024 佇列（`design-event-trigger.md` 稱 per-COM，錯）；LineBuffer 對未 enable 的 COM 也在跑，且永不重設（detach/reattach、enable 時殘留半行會被比對）。 |
| D-S6 | §5.5 | 子程序以 `exec` 或 `shell` 字串執行 | shell 寫死 `["/bin/sh","-c",…]`，timeout 以 `os.killpg/os.getpgid/SIGTERM/SIGKILL`、`start_new_session=True`——**全為 POSIX 專屬**；Windows 上 shell handler 會 spawn 失敗、timeout 路徑會 `AttributeError`（被 executor 吞，busy/inflight 仍由 finally 回收，但不寫 `fire_timeout`）。Rust 跨平台須定義 Windows 語意（Job Object、`cmd /C` 或拒絕 shell）。 |
| D-S7 | §4.2 #5 `file` | 「本地讀寫檔案＋RPC 傳輸呼叫」（Stage 1A） | CLI **不讀寫本地檔**；`local_path` 原樣傳 daemon，由 daemon 進程讀（push）/寫（pull，未給 `--local` 時寫 `basename(remote)` 到 daemon cwd）。remote endpoint 時路徑在 daemon host。若 Rust CLI 改成本地讀寫，屬行為變更（需 BC）。 |
| D-S8 | §4.2 #8 `stream` | 「所有即時串流讀取子命令／RPC 串流讀取」 | 只有 `stream tail`，為**一次性**請求回應（RPC `result.tail` legacy 路徑，range 模式、無 metadata、無 `WAL_MISSING` 檢查）；無 follow/subscribe。 |
| D-S9 | §4.2 #7 `log` | 「所有日誌操作子命令」 | 只有 `log tail-raw`、`log tail-text`（讀 WAL）；agent log capture 在 `session log-start/stop/status`。 |
| D-S10 | §4.2 #13 `skill` | 輸出內建 `SKILL.md` | 另有 `--platform {auto,linux,windows}`，windows 輸出 `SKILL_WINDOWS.md`；Rust 須內嵌兩份。 |
| D-S11 | §4.2 #10 `event` | 「純 RPC 呼叫」 | `event add --file` 由 CLI 讀本地 JSON（本地 I/O）；讀檔/JSON 失敗回 `INTERNAL_ERROR` exit 1（其他 event 失敗 exit 2）。 |
| D-S12 | §5.4 | LineBuffer 16,384 bytes，「依既有強制切行規則」 | 一致；規則細節（每次 feed 只切一段無換行超長塊、`idx > max` 嚴格、追加 `" ...truncated"`、截斷塊不剝 `\r`）見 §3.3，須寫入契約。 |
| D-S13 | §5.4 | Actor mailbox／待處理資料必須有界 | Event matcher queue 1024（drop-oldest）、dispatcher 8（丟新）有界；但 `EventLogger.tail` 一次讀整檔、event.* 在 asyncio loop 上同步 fsync——可能拖住 RPC loop（Python 現況缺陷）。 |
| D-S14 | §4.1 | `config.py` → `sw-proto::config_types` | config 解析對錯誤值的處理不一：多數欄位靜默回預設，但 `max_console_line_chars`、`console_unlock_idle_s` 與 YAML 語法錯會**直接 raise 穿出 `load_profiles`**（daemon 啟動失敗）。契約需凍結此分流。 |
| D-S15 | spec 全文 | 未提及 regex 引擎 | 規則 regex、profile regex、`completion_regex` 都是 **Python `re` 方言**；規則 ReDoS 防護依「有無 google-re2」改變可接受集合（本機無 re2 → `a+b+`、`\d+\.\d+` 被拒）。Rust `regex` crate 天生線性但**不支援 backreference／lookaround**（Python 無 re2 時，單一 lookaround 例如 `(?=x)` 可被接受）。需要 owner 決定：完全複製 Python 無-re2 拒絕集、或改 re2 等價語意，並處理 lookaround 規則的相容性。 |

### 8.2 文件（README／SKILL.md／design docs）vs 程式碼

| ID | 文件 | 宣稱 | 實際 |
|---|---|---|---|
| D-D1 | SKILL.md「透過 UART 推送／拉取檔案」 | `--chunk-size` 預設 2048 | `DEFAULT_CHUNK_SIZE = 512`（README 已寫 512）。 |
| D-D2 | SKILL.md 同節 | 「pull 省略 `--local` 時回傳檔案內容」 | 不回內容；寫 `os.path.basename(remote_path)` 到 daemon cwd，回 `local_path`。 |
| D-D3 | README:2957 | 規則為 JSON/YAML 檔 | Registry 只讀 `*.json`。 |
| D-D4 | README:2998 | stdin payload 含 `com`、`trigger_ts` | 實際欄位為 `selector`、`matched_at`（無 `com`、`trigger_ts`）。 |
| D-D5 | design-event-trigger.md:33、159 | counter／event log 在 tmpfs `/tmp/serialwrap/events/` | 實際 `STATE_DIR/events`（`~/.local/state/serialwrap/events`），非 tmpfs，host reboot 保留。SKILL.md「daemon 重啟後 counter 保留（tmpfs 除外）」也基於舊前提。 |
| D-D6 | design-event-trigger.md:79 | per-daemon 飽和 → drop_oldest | 實作丟**新** fire（`per_daemon_saturated` 直接 return）。 |
| D-D7 | design-event-trigger.md:374 | `match_skipped.reason` 含 `per_rule_busy/per_daemon_saturated` | 這兩者只以 `event_dropped` 發出（不需 debug）；`match_skipped` 僅 4 種。`event_dropped` 無 `dropped_count` 欄位。 |
| D-D8 | design-event-trigger.md:249-255 | `matched_line` 為整行、`bridge_generation` 為實際世代 | `matched_line` = `matched_text`；`bridge_generation` 恆 0。 |
| D-D9 | CLAUDE.md「Agent 日誌 capture」 | `log_dir` 優先序 per-target > per-profile > `defaults.log_dir` > env > `~/b-log` | 僅對 YAML explicit target 成立；**動態偵測 session 只用 template `log_dir`，跳過 `defaults.log_dir`** 直接落 `LOG_DIR`。 |
| D-D10 | file_transfer.py:38-42 註解 | 512B chunk 整行 ≈741 字元 | 實際約 783～800 bytes（runtime sentinel 加長樣板）。 |
| D-D11 | SKILL.md「Event Trigger Engine」 | `auto_enable_com_on_load:true` 的規則在 daemon 重啟後自動啟用 | 對具體 COM selector 成立；**selector `ALL` 在啟動時只展開到當下 `known_coms()`**（engine.start 早於 device watcher／bootstrap_attach，只含 YAML explicit target），之後熱插或動態偵測的 COM 不會自動啟用。 |

### 8.3 Python 現況潛在缺陷（Rust 需決定「照抄」或「修正並開 BC」）

| ID | 位置 | 問題 |
|---|---|---|
| D-C1 | `config.py:26-27,59-60,170-172` | `login_regex`／`password_regex` 的 Python 預設是 raw string `r"(?mi)^login:\\s*$"` → regex 需要字面反斜線，**永遠匹配不到 `login:`**（實測 False）。只在 YAML 未定義 `login_regex` 時生效（出貨 YAML 皆有定義）。 |
| D-C2 | `config.py:336` | `all_templates = ordered_templates` 每個檔覆蓋前一個：多個 YAML 檔時只回傳**最後一個檔**的 templates（動態偵測只看得到它）；`max_sessions` 也是後者覆蓋。target 只能引用同檔 template。 |
| D-C3 | `config.py:162`、schema 339-340 | `bool()` truthiness：`xonxoff: "false"`、`auto_enable_com_on_load: "false"` 皆為 True。 |
| D-E1 | `engine.py:530-534` | `emit_fire` 在 dispatch **前**就設 `exhausted=True`；若該 fire 隨後因 `per_rule_busy`／`per_daemon_saturated` 被丟，counter 已 exhausted 但 `fires` 未增 → 實際觸發次數少於 `max_fires`。 |
| D-E2 | `engine.py:534`、`dispatcher.py:23` | payload `matched_line` 實為 `matched_text`；`bridge_generation` 恆 0。 |
| D-E3 | `session_manager.py:1013` | scope 判斷用的 `active_cmd_id` 是字串 `"foreground"`（非實際 cmd_id），且只看 `foreground_busy`（background 命令期間視為 spontaneous）。payload/env 的 `active_cmd_id` 因此是 `"foreground"` 或 null。 |
| D-E4 | `dispatcher.py:237-241` | `last_fire_ts` 記的是 handler **結束**時刻；cooldown 從結束起算；handler 執行期間同 rule 匹配一律 `per_rule_busy` 丟棄。 |
| D-E5 | `schema.py:259-291` vs `matcher.py:36` | `contains` 規則的 `flags` 不驗證，但 `PatternMatcher` 建構時 `_flags_from_string` 會 raise。`event.rule_set` 這種規則：檔案已落盤、`_rules` 已更新，`replace_rules` 才 raise → 回 `INVALID_RULE_SCHEMA`，但規則留在磁碟與記憶體（該 rule 無 compiled，靜默不匹配）；**daemon 下次啟動 `MatcherWorker.__init__` raise → `engine.start()` 失敗**。 |
| D-E6 | `registry.py:229-233` | `load_all` 只攔 `RuleSchemaError`；磁碟上 `cooldown_ms:"abc"`（ValueError）或 `cooldown_ms:null`（TypeError）會讓 `engine.start()`／`event.reload` 直接 raise。 |
| D-E7 | `service.py:1136` | `event.rule_set` 回 `{"ok": True, **rule.raw}`：raw 內若有 `ok`／`version` 等鍵會覆寫回應欄位。 |
| D-E8 | `event_log.py:157-158` | `event.tail` 的 `n=0` 回傳**全部**（`out[-0:]`）；`tail` 只讀現行檔，不含 `.1..3`。 |
| D-E9 | `registry.py:209`、`counter.py:37` | `event.rule_delete`／`event.reset` 的 `rule_id` 未驗證即 `os.path.join` → 可用 `../` 刪除 `events.d` 外的 `*.json`／`*.counter.json`（RPC 信任範圍為本機使用者，仍建議 Rust 以 `_NAME_RE`/`_OWNER_RE` 驗證）。`event.com_enable` 接受任意字串（含空字串）。 |
| D-E10 | `engine.py:403-435` | `reload` 不會 disable 已移除規則的 COM；`_enabled_coms` 只增不減（除非手動 disable）。 |
| D-E11 | `matcher.py:240` | 每一行、每條 selector 命中的 rule 都同步讀一次 counter 檔（tmpfs 假設已不成立）。 |
| D-F1 | `service.py:1077` | RPC `chunk_size` 0/缺 → 512、負數 → 1（`max(1,…)`），`push_file` 的 `chunk_size<=0 → INVALID_ARGS` 對 RPC 路徑不可達；`chunk_timeout_s=0` 被接受（每段立即逾時）。 |
| D-F2 | `cli.py:98-110` | file.push/pull CLI 預設 socket timeout 5s，大檔必然 CLI 端 `TIMEOUT`，daemon 仍繼續傳（#123 已知）。 |
| D-F3 | `file_transfer.py:249-252` | `MOVE_TIMEOUT`／`MOVE_FAILED` 不 cleanup 暫存檔（其他失敗路徑都會）。 |
| D-F4 | `uart_io.py:152`、`file_transfer.py:312` | pull 依賴 131072 字元 RX 視窗，超過約 96 KiB raw 的檔案會 `PULL_PARSE_FAILED`；SKILL 說「數十 KB 內」大致相符但未說是硬限制。 |
| D-F5 | `file_transfer.py:322` | `base64.b64decode` 非 strict（夾雜雜訊字元會被靜默丟棄後再以 md5 把關）；Rust 需同樣「先過濾非字母表字元」再解碼，否則行為不同。 |
| D-F6 | `session_manager.py:4880-4890` | file transfer 不經 CommandArbiter（直接取前景 operation），與排隊中的 agent 命令競爭時回 `SESSION_BUSY` 而非排隊；spec §5.2.1 表把 FileTransfer 列為 TX 來源之一，需確認 Rust 是否保持「不排隊、回 BUSY」。 |
| D-L1 | `session_manager.py:2426` | agent log capture 於 `foreground_busy` 期間完全不寫（含 file transfer、agent 命令輸出），且寫入的是 `clean_text` 後文字（與 WAL raw 不同）。 |
| D-L2 | `service.py:946-961` | `result.tail` legacy 的 `int(params.get("from_seq") or 0)` 非數字會 raise → `EXCEPTION`；`log.tail_*` 則回 `INVALID_ARGS`（兩者不一致）。各 tail 的 `limit=0` 均被 `or 200` 吃成 200。 |
| D-K1 | `constants.py:78-79` | `EVENTS_LOG_ROTATE_BYTES`／`EVENTS_LOG_BACKUP_COUNT` 定義但未被引用（EventLogger 用同值的硬編碼預設）。 |

---

## 9. Rust 實作時必須逐字複製的 Python 語意（checklist）

1. JSON 序列化三種風格：CLI／RPC 回應 `separators=(",",":")`、`ensure_ascii=False`；events.ndjson 與 handler stdin 為 **Python 預設分隔符**（`", "`、`": "`），不 sort；規則檔 `indent=2, sort_keys=True`、無結尾換行；counter 檔預設分隔符。
2. `int()`/`float()`/`bool()` 的 Python 轉型規則（schema 的 `cooldown_ms`、`timeout_ms`；config 的 `_as_int`/`_as_float`；RPC 的 `chunk_size`、`limit`、`from_seq`），以及 `x or default` 把 0／空字串視為缺值。
3. `shlex.quote` 逐字實作（§4.3）。
4. `strip_ansi`（§3.3）與 `clean_text` 是兩套不同 regex，用途不同（engine/transfer vs capture/result）。
5. UTF-8 `errors="replace"` 解碼點：LineBuffer finalize、RX 文字視窗、log capture；字元（code point）與 byte 計數差異：`REGEX_MATCH_INPUT_MAX`、env `MATCHED_TEXT` 4096 為**字元**，`stdout_tail`/`stderr_tail` 4096 為 **bytes**（先切 bytes 再 decode），LineBuffer 16384 為 **bytes**，`max_console_line_chars` 比較 UTF-8 **bytes**，echo slice 64 為**字元**。
6. `time.time()`（wall clock ms）用於 `matched_at`、`last_fire_ts`、`ts`；log capture 檔名用本地時間 `%y%m%d-%H%M%S`；`wal.reset` 後綴用 UTC `%Y%m%d-%H%M%S`。
7. Python `re` 方言（inline flags、`(?m)`、`$` 行尾語意）於 profile regex 與 `wait_for_regex_from`。
