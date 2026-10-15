# 05 — Session Runtime 盤點（session_manager / arbiter / login_fsm / transport_stall / auth）

- 基準 repo：`~/prj_pri/serialwrap-worktrees/rust-refactor`，HEAD `93553e6`（含 #230 producer completion）。
- 盤點檔案（行數）：`sw_core/session_manager.py`（5010，全讀）、`sw_core/arbiter.py`（330）、`sw_core/login_fsm.py`（367）、`sw_core/transport_stall.py`（112）、`sw_core/auth.py`（144）。為完整性另讀相依片段：`sw_core/constants.py`、`sw_core/command_completion.py`、`sw_core/config.py:20-85`、`sw_core/service.py`（接線、`_resolve_session_id`、RPC handler）、`sw_core/uart_io.py`（deferred buffer / write path / wait 原語）、`sw_core/assets/profiles/default.yaml`。
- 行號格式 `file:line`；未註檔名者為 `session_manager.py`。
- 規格對照：`docs/rust-refactor/serialwrap-rust-spec-final.md` §4.1、§5、§9、ADR-06。
- 慣例：Python 沒有 `SessionState` enum；state 是**字串**，由 `SessionRuntime.state` property 存取（`_state` 預設 `"DETACHED"`）。

---

## 0. 速覽：常數（`sw_core/constants.py`）

| 常數 | 值 | 行 | 用途 |
|---|---|---|---|
| `ERROR_CREDENTIALS_UNRESOLVED` | `"CREDENTIALS_UNRESOLVED"` | 85 | last_error 終態（#140） |
| `ERROR_RX_FLOOD` | `"RX_FLOOD"` | 90 | probe 失敗反分類（#153） |
| `RX_RATE_WINDOW_S` | `10.0` | 91 | `UARTBridge.rx_stats()` 視窗 |
| `RX_FLOOD_BYTES_PER_10S` | `20_000` | 93 | 洪水門檻（≥） |
| `ERROR_TRANSPORT_STALL` | `"TRANSPORT_STALL"` | 101 | #150 |
| `TRANSPORT_STALL_MIN_RX_AGE_S` | `30.0` | 102 | last_rx 凍結門檻（≥） |
| `MAX_RECOVERY_LEASE_S` | `120.0` | 107 | recovery lease timeout clamp |
| `BOOTLOADER_RX_TAIL_BYTES` | `512` | 109 | bootloader/self-test rx_tail 長度（實際是 chars） |
| `HUMAN_ACTIVE_WINDOW_S` | `60.0` | 111 | human_active 判定窗 |
| `_HUMAN_PEER_GRACE_S` | `3.0` | 113 | 孤兒 console 回收節流 + human lease peer-loss grace |
| `REPROBE_RX_IDLE_S` | `3.0` | 115 | 自動重探前 RX 需閒置秒數 |
| `REPROBE_BACKOFF_S` | `2.0` | 117 | 重探初始 backoff |
| `REPROBE_MAX_INTERVAL_S` | `15.0` | 119 | backoff 上限 |
| `REPROBE_MAX_ATTEMPTS` | `10` | 121 | 重探上限 |
| `BOOT_BANNER_PATTERNS` | `("U-Boot", "Hit any key to stop autoboot", "CFE version ")` | 125 | **大小寫敏感 substring**（非 regex） |
| `BOOT_QUIET_WINDOW_S` | `180.0` | 134 | quiet window 長度 |
| `ERROR_AUTOBOOT_QUIET` | `"AUTOBOOT_QUIET"` | 142 | 可重試 gate 碼；同時是 self-test classification 字串 |
| `BOOT_BANNER_TAIL_CHARS` | `256` | 149 | banner rolling tail 保存長度 |
| `READY_RECONFIRM_RETRY_S` | `5.0` | 151 | pending-only 拒絕的 `retry_after_s` 固定值（service 層） |
| `READY_RECONFIRM_MAX_S` | `BOOT_QUIET_WINDOW_S + 120.0` = `300.0` | 156 | pending 存活上限 |
| `READY_RECONFIRM_MAX_ATTEMPTS` | `5` | 163 | pending 確認 probe 失敗上限 |
| `ERROR_READY_UNCONFIRMED` | `"READY_UNCONFIRMED"` | 169 | 不可重試 gate 碼 |
| `ERROR_BOOTLOADER_STUCK` | `"BOOTLOADER_STUCK"` | 176 | last_error（#162 C 組） |
| `UBOOT_FALLBACK_PROMPTS` | `("^=> $", "^U-Boot> $", "^CFE> $")` | 182 | profile 無 bootloader_prompts 時 fallback |
| `BG_CAPTURE_MAX_BYTES` | `4 * 1024 * 1024` | 191 | 單一 background capture 記憶體上限（以 `len(str)` 計，非 bytes） |
| `BG_CAPTURE_MAX_COUNT` | `64` | 193 | capture 數上限 |
| `CMD_HISTORY_MAX` | `512` | 195 | arbiter 命令記錄上限 |
| `CMD_PENDING_MAX` | `256` | 197 | per-session 未完成命令硬上限 |
| `DEFERRED_INPUT_MAX_BYTES` | `256 * 1024` = `262144` | 202 | per-console deferred buffer 上限（**超過丟最舊**） |
| `LOG_DIR` | `$SERIALWRAP_LOG_DIR` 或 `~/b-log` | 64 | agent log 目錄 fallback |
| `STATE_PATH` | `STATE_DIR/state.json` | 59 | 持久化狀態 |

`arbiter.py` 模組常數：`CMD_WARN_BYTES = 4096`（:14）、`CMD_REJECT_BYTES = 16384`（:15）、`ERROR_CMD_TOO_LONG = "CMD_TOO_LONG"`（:20）、`ERROR_CMD_CONTAINS_NEWLINE = "CMD_CONTAINS_NEWLINE"`（:21）、`WARNING_CMD_LENGTH = "CMD_LENGTH_WARNING"`（:22）。

`command_completion.py`：`COMPLETION_REGEX_MAX_BYTES = 1024`（:7）、`ERROR_INVALID_COMPLETION_REGEX = "INVALID_COMPLETION_REGEX"`（:8）、`valid_completion_regex(value: object) -> bool`（:11）：`None`→True；非 str 或空字串→False；UTF-8 長度 >1024→False；`re.compile` 失敗（`re.error`/`UnicodeEncodeError`/`RecursionError`）→False；`compiled.search("") is not None`（會匹配空串）→False。

`session_manager.py` 模組常數：`_ATTACHED_CONSOLE_LEASE_TIMEOUT_S = 86400.0`（:65）、`_MIN_FILE_CHUNK_TIMEOUT_S = 5.0`（:69）、`_MIN_FILE_PULL_TIMEOUT_S = 30.0`（:70）、`_MIN_FILE_ECHO_TIMEOUT_S = DEFAULT_ECHO_TIMEOUT_S`（=5.0，:74）。

`DeviceWatcher` tick：`poll_interval_s=1.0`（`device_watcher.py:29`）；每 tick 呼叫 `on_tick=SessionManager.reconcile_readiness`（`service.py:268-272`）。

---

## 1. 資料結構

### 1.1 `BackgroundCapture`（:195-231，mutable dataclass）

| 欄位 | 型別 | 預設 |
|---|---|---|
| `cmd_id` | `str` | 必填 |
| `session_id` | `str` | 必填 |
| `from_seq` | `int` | 必填（建立時 `wal.current_seq + 1`） |
| `quiet_window_s` | `float` | 必填（= `profile.quiet_window_s`，預設 2.0） |
| `created_at` | `str` | 必填（`now_iso()`，UTC ISO8601） |
| `chunks` | `deque[str]` | `deque()` |
| `last_seq` | `int` | `0` |
| `status` | `str` | `"active"`；其他值 `"done"`、`"error"` |
| `error_code` | `str \| None` | `None` |
| `last_activity_mono` | `float` | `time.monotonic()` |
| `total_bytes` | `int` | `0`（以 `len(chunk)` 字元數累計） |
| `dropped_chunks` | `int` | `0` |

- `add_chunk(chunk, max_bytes)`（:210）：空 chunk 忽略；append 後 `while total_bytes > max_bytes and len(chunks) > 1: popleft, dropped_chunks += 1`（至少保留 1 段）。
- `maybe_finalize()`（:228）：`status=="active"` 且 `monotonic - last_activity_mono >= quiet_window_s` → `"done"`。僅在 `get_background_result` 讀時被動觸發（無 timer）。

### 1.2 `InteractiveLease`（:233-252）

| 欄位 | 型別 | 預設 |
|---|---|---|
| `interactive_id` | `str` | 必填（`uuid4().hex`，32 hex） |
| `session_id` | `str` | 必填 |
| `owner` | `str` | 必填（`"agent"`/任意字串，或 `"human:<client_id>"`） |
| `created_at` | `str` | 必填 |
| `timeout_s` | `float` | 必填 |
| `last_activity_at` | `float` | `time.monotonic()` |
| `status` | `str` | `"active"`；其他 `"closed"`、`"expired"` |
| `recovery_mode` | `bool` | `False` |
| `suspended_human` | `bool` | `False`（內部：開啟時已 suspend human lease；不透出 RPC） |
| `peer_lost_at` | `float \| None` | `None`（human lease peer-loss grace 起點） |

- `touch()`：`last_activity_at = monotonic()`。
- `expired()`：`monotonic() - last_activity_at > timeout_s`（嚴格大於）。

### 1.3 `SessionCapture`（:255-263）

`capture_id: str`（`str(uuid.uuid4())`，含連字號）、`session_id: str`、`log_path: str`、`started_at: str`、`line_count: int = 0`、`byte_count: int = 0`、`status: str = "active"`（停止為 `"stopped"`）。

### 1.4 `_ForegroundOperation`（:266-277，frozen）

`session_id: str`、`bridge: UARTBridge`、`bridge_generation: int`。內部 operation token，比對 `session is current`、`_foreground_operation is operation`、`session.bridge is operation.bridge`、`bridge_generation ==`，舊 epoch callback 晚到即 no-op。**這是現有程式裡唯一接近 spec `conn_epoch`/`owner_gen` 的身分機制**。

### 1.5 `_PostCloseAction`（:280-295）

`bridge: UARTBridge | None = None`、`needs_resume: bool = False`、`clear_owner_after_resume: bool = False`；`execute()`：bridge 非 None 且 needs_resume → `bridge.resume_interactive()`；若 `clear_owner_after_resume` → `bridge.set_interactive_owner(None)`。必須在 manager lock 外執行。

### 1.6 `SessionRuntime`（:298-638，mutable）

| 欄位 | 型別 | 預設 | 持久化 | 說明 |
|---|---|---|---|---|
| `session_id` | `str` | 必填 | key | `f"{profile_name}:{com}"` |
| `profile` | `SessionProfile` | 必填 | 部分 | frozen，用 `dataclasses.replace` 更新 |
| `detached_at` | `str\|None` | None | 否 | |
| `last_ready_at` | `str\|None` | None | 否 | |
| `vtty_path` | `str\|None` | None | 否 | |
| `bridge` | `UARTBridge\|None` | None | 否 | |
| `attached_real_path` | `str\|None` | None | 否 | |
| `bridge_generation` | `int` | 0 | 否 | 每次 attach 成功 +1（:2708、:3132） |
| `recovering` | `bool` | False | 否 | |
| `recovery_started_at` | `str\|None` | None | 否 | |
| `pending_auto_login` | `bool` | False | 否 | reboot 命令後 True |
| `interactive_session_id` | `str\|None` | None | 否 | 目前 lease id |
| `foreground_busy` | `bool` | False | 否 | |
| `_foreground_operation` | `_ForegroundOperation\|None` | None | 否 | repr=False |
| `background_cmd_ids` | `list[str]` | [] | 否 | |
| `active_capture` | `SessionCapture\|None` | None | 否 | |
| `retained_consoles` | `PreservedConsoles\|None` | None | 否 | detach 時保留的 PTY |
| `retained_human_owner` | `str\|None` | None | 否 | |
| `retained_human_timeout_s` | `float\|None` | None | 否 | |
| `fg_cmd_started_mono` | `float\|None` | None | 否 | |
| `fg_cmd_expected_duration_s` | `float\|None` | None | 否 | |
| `last_state_change_at` | `str\|None` | None | 否 | state setter 值改變時自動 `now_iso()` |
| `last_rx_at` / `last_tx_at` / `last_probe_at` | `str\|None` | None | 否 | |
| `last_rx_mono` / `last_tx_mono` | `float` | 0.0 | 否 | 0.0＝從未 |
| `reprobe_attempts` | `int` | 0 | 否 | |
| `next_reprobe_at` | `float\|None` | None | 否 | monotonic |
| `reprobe_exhausted` | `bool` | False | 否 | latch |
| `credentials_warned` | `bool` | False | 否 | #140 去重 |
| `last_error_detail` | `str\|None` | None | 否 | `last_error` 值變更時自動清為 None |
| `transport_stall_warned` | `bool` | False | 否 | #150 去重；RX 到即重臂 |
| `released_by` / `released_at` / `released_reason` | `str\|None` | None | **是**（`released` map） | #54 |
| `profile_source` | `str` | `"unknown"` | 間接 | `unknown`/`pin`/`sticky`/`detected`/`fallback`/`yaml-target` |
| `boot_quiet_until` | `float` | 0.0 | 否 | monotonic；0.0=未啟用 |
| `boot_banner_tail` | `str` | `""` | 否 | |
| `ready_reconfirm_pending` | `bool` | False | 否 | #162 |
| `ready_reconfirm_deadline` | `float` | 0.0 | 否 | |
| `ready_reconfirm_attempts` | `int` | 0 | 否 | |
| `ready_reconfirm_failed` | `bool` | False | 否 | |
| `flash_prev_state` | `str\|None` | None | 否 | #55 |
| `_stashed_human_lease` | `InteractiveLease\|None` | None | 否 | soft preempt / recovery stash |
| `_state` | `str` | `"DETACHED"` | 部分（只存 RELEASED） | init=False |
| `_last_error` | `str\|None` | None | 否 | init=False |

Property／方法：
- `last_error` setter（:401）：`prev != value` 時 `last_error_detail = None`；**呼叫端須先設 last_error 再設 detail**。
- `state` setter（:414）：`prev != value` 時更新 `last_state_change_at`。
- `compute_idle_ms()`（:421）：`max(last_rx_mono, last_tx_mono)`；0.0→None；否則 `int((now-last)*1000)`。
- `classify_activity()`（:427）：state ∉ {READY, ATTACHED} → `"offline"`；idle None → `"newly-attached"`；`<5_000` → `"active"`；`<60_000` → `"idle-healthy"`；否則 `"quiet-suspicious"`。
- `reset_reprobe_progress()`（:439）：attempts=0、next=None、exhausted=False。
- `boot_quiet_active(now=None)`（:444）：`until > 0.0 and now < until`。
- `arm_boot_quiet(now=None)`（:450）：`boot_quiet_until = base + 180.0`；`boot_banner_tail = ""`；若 `ready_reconfirm_pending` 為 False → 設 True、`deadline = base + 300.0`、`attempts = 0`、`failed = False`（**重臂不延長 deadline**）。
- `clear_boot_quiet()`（:472）：until=0.0、tail=""；**不**清 pending。
- `confirm_ready()`（:481）：clear_boot_quiet + pending=False、deadline=0.0、attempts=0、failed=False。唯一成功出口。
- `expire_ready_reconfirm(now=None, *, force=False) -> bool`（:497）：非 pending→False；非 force 時需 `deadline>0 and now>=deadline` 或 `attempts >= 5`；成立 → pending=False、failed=True、回 True。
- `ready_reconfirm_remaining_s(now=None)`（:519）：非 pending 或 deadline≤0 → None；否則 `round(max(deadline-now,0),1)`。
- `agent_gate_active(now=None)`（:526）：先 `expire_ready_reconfirm(now)`，回 `boot_quiet_active(now) or ready_reconfirm_pending`。

### 1.7 `to_public_dict()` 欄位（:538-638；`session list`/`get_state`/`activity` 輸出）

依序 key：`session_id`、`profile`（profile_name）、`com`、`alias`、`act_no`、`device_by_id`、`platform`、`profile_source`、`command_capable`、`state`、`last_error`、`last_error_detail`、`detached_at`、`last_ready_at`、`vtty`（bridge None 時用 `retained_consoles.primary_vtty()`）、`attached_real_path`、`bridge_generation`、`recovering`、`interactive_session_id`、`foreground_busy`、`fg_cmd_expected_duration_s`、`console_count`（bridge `list_consoles()` 長度或 retained clients 數）、`last_state_change_at`、`last_rx_at`、`last_tx_at`、`last_probe_at`、`reprobe_attempts`、`next_reprobe_at`（**monotonic 原值**，跨程序不可比）、`reprobe_exhausted`、`boot_quiet_remaining_s`（until>0 且 >now 時 `round(until-now,1)`，否則 None）、`ready_reconfirm_pending`、`ready_reconfirm_failed`、`ready_reconfirm_remaining_s`、`idle_for_ms`、`last_rx_age_s`（`last_rx_mono>0` 時 `round(now-last_rx_mono,1)` 否則 None）、`last_tx_age_s`（同理）、`rx_bytes_last_10s`、`rx_rate_bps`（bridge `rx_stats()` 回 dict 才有值，否則 None）、`outstanding_commands`（`len(background_cmd_ids) + (1 if foreground_busy else 0)`）、`activity_classification`、`released_by`、`released_at`、`released_reason`、`capture`（`{capture_id, log_path, status, line_count, byte_count}` 或 None）；條件 key `console_endpoint`：`console_endpoint is not None or not _pty_available()` 時才出現（POSIX 無此 key）。

注意：`to_public_dict` **不會**呼叫 `expire_ready_reconfirm`；`get_session_state`（:3934）會先呼叫，`list_sessions`（:991）不會 → `session list` 可能顯示尚未翻轉的 pending。

---

## 2. SessionManager 全域狀態（`__init__` :642-732）

建構參數：`profiles: list[SessionProfile]`、`wal: WalWriter`、`templates=None`、`max_sessions=16`、`on_ready: Callable[[str],None]`、`on_detached: Callable[[str],None]`、`on_console_line=None`、`on_command_flush: Callable[[str,str],None] | None = None`、`state_path=None`（fallback 模組層 `STATE_PATH`，建構時讀取）。

成員：`_lock: RLock`、`_state_io_lock: Lock`（state.json I/O 序列化；鎖序 `_lock → _state_io_lock`）、`_rx_observers: list[Callable[[str, bytes, int], None]]`、`_sessions: dict[str, SessionRuntime]`（**插入順序有語意**）、`_aliases: AliasRegistry`、`_devices: dict[str, DeviceInfo]`（`DeviceInfo(by_id, real_path)`）、`_binding_overrides: dict[str,str]`（sid→by_id）、`_pending_com: dict[str,str]`（by_id→COM，#100）、`_attach_inflight: set[str]`（by_id）、`_reprobe_inflight: set[str]`（sid）、`_reprobe_probe_locks: dict[str, Lock]`、`_reprobe_workers: list[Thread]`、`_released_by_ids: set[str]`、`_flash_critical_coms: set[str]`、`_loaded_released: dict[str, dict]`、`_background: dict[str, BackgroundCapture]`、`_interactive: dict[str, InteractiveLease]`、`_capture_fps: dict[str, file]`、`_templates: list[ProfileTemplate]`、`_max_sessions`、`_profile_pins: dict[str,str]`、`_profile_detected: dict[str,str]`、`_last_console_reap_at: float = 0.0`。

Service 接線（`service.py:239-249, 286-296`）：
- `on_ready(sid)` → `arbiter.register_session(sid)`（建立 queue + worker）。
- `on_detached(sid)` → `arbiter.unregister_session(sid)`（預設 `FLUSHED_BY_RECOVERY`）。
- `on_command_flush(sid, code)` → `arbiter.flush_session(sid, code)`（#156）。
- daemon `stop()` → 每 session `unregister_session(sid, error_code="FLUSHED_BY_SHUTDOWN")`（`service.py:532`）。

初始化流程：`_load_state()` → 對每個 YAML target profile：`sid=f"{profile_name}:{com}"`；#186：若 binding override 等於「另一 target 在 YAML 宣告的 device_by_id」→ 刪除該 override；`device_by_id = override or p.device_by_id`，空則略過（未綁定的 target 不建 session）；建 `SessionRuntime`、`profile_source="yaml-target"`、alias 註冊 → 對 `_loaded_released` 中存在的 sid 設 `state="RELEASED"`＋provenance → `_save_state()`。

### 2.1 state.json（`_load_state` :734、`_save_state` :804）

Schema（`json.dumps(sort_keys=True, ensure_ascii=False, separators=(",",":")) + "\n"`）：
```
{"aliases": <AliasRegistry.dump()>,
 "bindings": {sid: by_id},
 "profile_detected": {by_id: profile_name},
 "profile_pins": {device_key: profile_name},
 "released": {sid: {"by_id":..., "released_by":..., "released_at":..., "reason":...}}}
```
- 讀取 `OSError` → `StateLoadError`（fail closed，daemon 拒絕啟動；`daemon.py` 攔截）。
- JSON/Unicode/Value error → `os.replace(path, path+".corrupt")`、warning、略過。
- 寫入：snapshot 在 `_lock` 內；I/O 在 `_state_io_lock` 內：`mkstemp(dir, prefix="state.json.tmp.")` → write → flush → fsync → `_replace_state_file`（nt：PermissionError 退避 0.01×2^n，6 次）→ 非 win32 目錄 fsync；finally 移除殘留 tmp。
- `released` 只序列化 `state=="RELEASED"` 的 session；`_released_by_ids` 由載入時 `by_id` 填入。

---

## 3. 狀態轉移總表

狀態字串：`DETACHED`、`ATTACHING`、`ATTACHED`、`READY`、`RECOVERING`、`RELEASED`、`FLASHING`。下表「副作用」中 `detach()` 指 `_detach_session_locked`（§3.1）。

| # | 起始 | 觸發（函式:行） | Guard | 結果 | 副作用 |
|---|---|---|---|---|---|
| T1 | (無) | `_session_from_template`（:946）經 `_attach_by_id_dynamic` | 有 templates、`len(_sessions) < max_sessions`、裝置在線 | 新 session `DETACHED`→立即 `ATTACHING` | COM：`_pending_com.pop(by_id)` 或 `_next_dynamic_com()`；`act_no=len(_sessions)+1`；alias `f"{tpl}+{act_no}"` |
| T2 | DETACHED/ATTACHED… | `_attach_by_id`（:2549）開 bridge 前 | `dev` 存在、`bridge is None`、`profile.device_by_id==by_id`、非 RELEASED | `ATTACHING`，`last_error=None` | 記 `gen_before` |
| T3 | ATTACHING | `_attach_by_id` probe 成功 | 裝置 real_path 未變、state≠DETACHED、`bridge_generation==gen_before`、非 RELEASED | `READY` | `bridge_generation+=1`、`confirm_ready()`、`last_ready_at`、清 recovering/pending_auto_login、reset reprobe、`_restore_retained_human_console_locked`、lock 外 `on_ready` |
| T4 | ATTACHING | probe 失敗或非 command_capable | 同上 | `ATTACHED`，`last_error=err`（非 capable 時 `None`），`last_error_detail` | 同上（不 notify） |
| T5 | ATTACHING | require_login + `ensure_ready` 失敗（:2650-2668） | — | `DETACHED`，`last_error=err` | `bridge.stop(preserve_consoles=True)`、保留 consoles、`on_detached` |
| T6 | ATTACHING | attach 中裝置換 path/消失/gen 變 | — | `DETACHED`，`DEVICE_REMOVED_DURING_ATTACH` | stop bridge（保留 consoles） |
| T7 | ATTACHING | attach 中被 release | `state==RELEASED or by_id in _released_by_ids` | 保持 `RELEASED` | `bridge.stop(preserve_consoles=False)` |
| T8 | ATTACHING | `bridge.start()` 等例外 | — | `DETACHED`，`ATTACH_FAILED:<ExcType>` | stop、`on_detached` |
| T9 | (attach 前) | `_attach_by_id` 時 `dev is None` | — | `DETACHED`，`DEVICE_NOT_FOUND` | `detached_at` |
| T10 | ATTACHED | `_probe_existing_bridge` 成功（:2460，attach_session/recover_session/reprobe） | bridge 同一、state∉{FLASHING,RELEASED} | `READY` | `confirm_ready()`、清 recovering 等、reset reprobe、`on_ready` |
| T11 | ATTACHED/READY | `_probe_existing_bridge` 失敗 | 同上 | `ATTACHED`，`last_error=err` | **注意：READY 再確認失敗也會把 READY 打回 ATTACHED**（:2541）但**不**呼叫 `on_detached`（arbiter queue 保留） |
| T12 | ATTACHED | `_probe_existing_bridge` quiet blocks | `_boot_quiet_blocks_probe` | 不變；若非 READY 設 `last_error="PROMPT_UNAVAILABLE"` | 不送 TX |
| T13 | ATTACHED/READY | reprobe worker 例外（:1571-1586） | state∉{FLASHING,RELEASED} | `ATTACHED`，`REPROBE_FAILED:<Exc>` | record attempt |
| T14 | DETACHED | `_prepare_reprobe_locked`（:1461） | 可重探條件（§6） | `ATTACHING` | record attempt、`_spawn_attach` |
| T15 | READY | `_execute_command_inner` reboot 命令（`_handle_reboot_command` :3530）agent 來源且 2s 內無 prompt | — | `RECOVERING` | `pending_auto_login=True`、`recovering=True`、`recovery_started_at`、`last_error=None`、`on_detached`、`_spawn_reboot_recovery(hard_timeout_s)` |
| T16 | READY | reboot 命令 human 來源且 2s 內無 prompt | — | `ATTACHED`，`REBOOTING` | 開/沿用 human lease、`on_detached` |
| T17 | RECOVERING | reboot recovery `ensure_ready` 成功 | bridge 同一、state∉{FLASHING,RELEASED} | `READY` | `confirm_ready`、`on_ready` |
| T18 | RECOVERING | reboot recovery 逾時 | state∉{FLASHING,RELEASED} | bridge None→`DETACHED`/`RECOVERY_TIMEOUT`；否則 `ATTACHED`/`last_error or "RECOVERY_TIMEOUT"` | 清 recovering 旗標 |
| T19 | READY | 命令逾時 `_recover_after_failure`→`_transition_to_attached(reason="PROMPT_TIMEOUT")`（:3345） | state∉{FLASHING,RELEASED} | `ATTACHED`，`PROMPT_TIMEOUT` | 原為 READY 時 `on_detached`（flush queue） |
| T20 | 任一（有 bridge） | `_handle_bridge_down`（:2441） | 非 flash busy、bridge 同一 | detach→`DETACHED`；若裝置仍在 → `RECOVERING`（`pending_auto_login`）或 `ATTACHING` | `last_error=None`（覆寫 `BRIDGE_DOWN:<reason>`）、`_spawn_attach` |
| T21 | 非 RELEASED、非 flash busy | `update_devices` 移除/改 path → `_detach_by_id`（:2307） | — | `DETACHED`，`DEVICE_REMOVED` / `DEVICE_REBOUND_REQUIRED` | 改 path 者再 `_spawn_attach` |
| T22 | 無 bridge | `_mark_missing_devices_locked`（:2240） | by_id 不在 `_devices` | `DETACHED`，`last_error` 若 None 設 `DEVICE_NOT_FOUND` | **包含 RELEASED 也會被改成 DETACHED？**——是：不檢查 RELEASED，見 §12 D-15 |
| T23 | 非 RELEASED、非 flash busy | `clear_session`（:1829） | — | detach(`CLEARED`)；裝置在 → `ATTACHING` | `_save_state`、`_spawn_attach` |
| T24 | 非 RELEASED、非 flash busy | `release_device`（:1852） | — | `RELEASED` | detach(`RELEASED`, drop_consoles=True)、provenance、`_released_by_ids.add`、`_save_state`；回 `closed_consoles`、`aborted_cmd=foreground_busy` |
| T25 | RELEASED（或無 bridge） | `attach_device`（:2004） | 裝置在線；非 force 時 `/proc` 無外部 holder | `ATTACHING` | 清 provenance、`_released_by_ids.discard`、reset reprobe、`_save_state`、`_spawn_attach` |
| T26 | 非 flash busy | `bind_session`（:2034） | by_id 未被其他 session 綁 | 有 bridge→detach(`REBOUND`)；RELEASED→`DETACHED`；裝置在→`ATTACHING`；否則 `DETACHED`/`DEVICE_NOT_FOUND` | 寫 binding override、`_save_state` |
| T27 | ATTACHED（有 bridge、無 human lease） | `attach_session`（:2150） | — | 經 T10/T11 | `credentials_warned=False` |
| T28 | 無 bridge | `attach_session` | 非 RELEASED、裝置在 | `ATTACHING` | `credentials_warned=False`、`_spawn_attach`；裝置不在→`DETACHED`/`DEVICE_NOT_FOUND` |
| T29 | 無 bridge | `recover_session`（:4582） | 裝置在 | `ATTACHING`（action `REATTACH`） | `_spawn_attach` |
| T30 | 任一 | `enter_flashing`（:1920） | — | `FLASHING` | `flash_prev_state=state`、`bridge.set_flash_mode(True)`；**無 guard**（可從 DETACHED/RELEASED 進入） |
| T31 | FLASHING | `exit_flashing`（:1935） | — | `flash_prev_state or (READY if bridge else DETACHED)` | 先無條件 `set_flash_mode(False)` |
| T32 | — | `__init__` released 還原 | sid 在 `_loaded_released` | `RELEASED` | |

狀態寫入保護：`_probe_existing_bridge`、`_transition_to_attached`、`_spawn_reboot_recovery` 遇 state ∈ {FLASHING, RELEASED} 不覆寫（#69）。`_flash_busy_locked(s) = s.state=="FLASHING" or s.profile.com in _flash_critical_coms`（:1888）擋：`clear_session`、`release_device`、`bind_session`、`recover_session`、`_detach_by_id`、`_handle_bridge_down`。

### 3.1 `_detach_session_locked(session, *, reason, drop_consoles=False)`（:1781）
1. 若現 lease 是 human → 記 `retained_human_owner/timeout_s`。
2. `bridge.stop(preserve_consoles=not drop_consoles)`（reader/console thread join 各 2.0s；清 `_deferred_buffers`、owner、suspend depth，`uart_io.py:288-316`）；`session.bridge=None`。
3. drop_consoles → 清 retained。`_store_retained_consoles_locked`。
4. `attached_real_path=None`、`state="DETACHED"`、`detached_at`、`last_error=reason`、reset reprobe。
5. pop lease（status `"closed"`）、`interactive_session_id=None`、`_stashed_human_lease=None`。
6. 撤銷 foreground token：`_foreground_operation=None`、`foreground_busy=False`、`fg_cmd_*=None`（**不等待在途命令結束**）。
7. `_stop_capture_locked`；所有 background capture `status="done"`、清 `background_cmd_ids`。
8. `on_detached(sid)`（**在 manager lock 內呼叫**；arbiter 內 `join(timeout=1.0)` 也在此 lock 下執行）。

---

## 4. CommandArbiter（`arbiter.py`）

### 4.1 結構
- `_QueuedCommand`（:25, `order=True`）：`sort_key: tuple[int,int]=(priority, counter)`；其餘 compare=False：`cmd_id`、`session_id`、`command`、`source`、`mode`、`timeout_s`、`expected_duration_s=None`、`completion_regex=None`。
- `CommandArbiter.__init__(send_cb)`：`_lock: Lock`、`_queues: dict[sid, PriorityQueue]`、`_threads`、`_stops: dict[sid, Event]`、`_commands: dict[cmd_id, dict]`、`_counter=0`。
- `send_cb` = `SerialwrapService._send_cb(session_id, command, source, cmd_id, timeout_s, mode, expected_duration_s=None, completion_regex=None)` → `SessionManager.execute_command(...)`（`service.py:307`）。

### 4.2 Priority
- 小值先出；同 priority 依 `counter`（FIFO）。
- Agent `command.submit`：`priority = int(params.get("priority") or 10)`（`service.py:900`；**0 會被當成 10**）。
- Human console 行（`_on_console_line`，`service.py:313`）：`priority=100`、`source=f"human:{client_id}"`、`timeout_s=30.0`、`mode=_human_console_mode(line)`：shlex 解析後略過 env 賦值、`sudo`（含帶參數 `-u -g -h -p -C -T -r -t`）、`env`、`command/builtin/exec`，base 命令 ∈ `{alsamixer, btop, htop, less, menuconfig, more, most, nano, nmtui, screen, tig, tmux, top, vi, view, vim, vimdiff, watch}` → `"interactive"`，否則 `"line"`。
- 喚醒 sentinel：`_QueuedCommand(sort_key=(0,0), cmd_id="")`。

### 4.3 `submit(*, session_id, command, source, mode, timeout_s, priority=10, expected_duration_s=None, completion_regex=None)`（:127）
檢查順序：
1. `valid_completion_regex` 失敗 → `{"ok":False,"error_code":"INVALID_COMPLETION_REGEX"}`。
2. `cmd_len = len(command.encode("utf-8","replace"))`；`> 16384` → `CMD_TOO_LONG`（含 `cmd_length`、`limit`、`hint`）。
3. `> 4096` → 附 `warning={"code":"CMD_LENGTH_WARNING","cmd_length","soft_limit":4096,"hint"}`。
4. `"\n" in command` → `CMD_CONTAINS_NEWLINE`（`\r` 不擋；空命令允許）。
5. lock 內：queue 不存在 → `SESSION_NOT_READY`（含 `session_id`）；`_count_pending_locked >= 256` → `SESSION_QUEUE_FULL`（`pending`、`limit`、`hint`）。
6. `counter += 1`、寫 record、`_evict_commands_locked()`；lock 外 `pq.put`。
7. 回 `{"ok":True,"cmd_id","status":"accepted","session_id"[, "warning"]}`。

Record 欄位（`command.get` 回 `{"ok":True,"command":dict(rec)}`）：`cmd_id`（`uuid4().hex`）、`session_id`、`command`、`source`、`mode`（原字串）、`execution_mode`（`fg→line`、`bg→background`、其餘原樣）、`timeout_s`、`expected_duration_s`、`completion_regex`、`priority`、`status`、`created_at`、`accepted_at`、`started_at`、`done_at`、`error_code`、`stdout`（""）、`partial`（False）、`background_capture_id`、`interactive_session_id`、`recovery_action`。

Status 值：`accepted` → `running` → `done` | `error` | `interactive` | `canceled`。

### 4.4 Pending／History
- `_count_pending_locked(sid)`：該 sid 且 `not rec.get("done_at")` 的筆數（含 accepted/running；**`interactive` 有 done_at，不計**）。
- `_evict_commands_locked()`：`len > 512` 時依 `done_at` 字串排序淘汰最舊已完成者；在途者不淘汰。呼叫點：submit、cancel、worker 終結；`_flush_session_locked` **刻意不呼叫**。

### 4.5 Worker（`_worker` :266）
- 每 session 一 daemon thread；`pq.get(timeout=0.2)`。
- 取件後：stop 已設→break；空 cmd_id→continue；lock 內：record 不存在、`status=="canceled"` 或已有 `done_at` → skip；否則 `status="running"`、`started_at`。
- 呼叫 `send_cb(sid, command, source, cmd_id, timeout_s, mode, expected_duration_s[, completion_regex=])`（completion_regex 為 None 時用 7 參數舊契約）。
- 例外 → `status="error"`、`error_code="SEND_FAILED"`、`done_at`。
- 結果：若 record 已 `canceled` → **丟棄結果**（不覆寫）；否則複製 `stdout`、`partial`、`background_capture_id`、`interactive_session_id`、`recovery_action`、`execution_mode`（存在才拷貝）；`ok` False → `status="error"`、`error_code = result.error_code or "COMMAND_FAILED"`；`status=="interactive"` → `"interactive"`；否則 `"done"`。非 dict 結果 → `"done"`。設 `done_at`、evict。
- **`ok:True` 但帶 `error_code` 的結果（`PROMPT_TIMEOUT_RECOVERED`、`PROMPT_TIMEOUT_FORCE_RECOVERED`）→ status `done`，`error_code` 不會被拷貝進 record（保持 None）**；僅 `recovery_action` 可辨識。

### 4.6 Register / Unregister / Flush
- `register_session(sid)`（:48）：已存在 no-op；建 PriorityQueue + Event + thread。只在 `on_ready` 呼叫 → **queue 只存在於 READY 過的 session**。
- `unregister_session(sid, error_code="FLUSHED_BY_RECOVERY")`（:60）：lock 內 pop stop/queue/thread 並 `_flush_session_locked`（與 pop 原子，#128）；lock 外 set stop、塞 sentinel、`join(timeout=1.0)`（非自身 thread）。
- `flush_session(sid, code)`（:86）／`_flush_session_locked`（:100）：所有 `session_id==sid`、`done_at is None` 且 `status=="accepted"` → `status="error"`、`error_code=code`、`done_at=now`。running/interactive 不動。

### 4.7 Cancel（`cancel(cmd_id)` :254）
- 不存在 → `CMD_NOT_FOUND`；`status ∈ {done, error}` → `CMD_NOT_CANCELABLE`。
- 其餘（`accepted`、`running`、`interactive`、**`canceled` 再取消也回 ok**）→ `status="canceled"`、`done_at=now`、evict、回 `{"ok":True,"cmd_id","status":"canceled"}`。
- **語意：純記錄層取消**。running 命令的 UART TX／等待**不中止**，worker 結束後丟棄結果；interactive 命令的 lease 不關閉；無「已送出 bytes」判定。

### 4.8 `snapshot()`：所有 record 依 `created_at` 排序的複本。

---

## 5. 命令執行（`execute_command` :3622 → `_execute_command_inner` :3705）

### 5.1 兩層 Admission
**Submit-time（`service._resolve_session_id` :618）**，順序：
1. `get_session_state(selector)`（含 `expire_ready_reconfirm`）；查無 → 原樣錯誤（`SESSION_NOT_FOUND`）。
2. state≠READY：`ATTACHED` 且 `command_capable=False` → `PROFILE_NOT_COMMAND_CAPABLE`（含 hint、session）；其餘 → `SESSION_NOT_READY`（含 session）。**RECOVERING/FLASHING/RELEASED 一律 `SESSION_NOT_READY`**。
3. `source` 不以 `human:` 開頭時：`ready_reconfirm_failed` → `READY_UNCONFIRMED`（`hint`、`recommended_action:"self_test"`、session，**無** retry_after_s）；`boot_quiet_remaining_s is not None` → `AUTOBOOT_QUIET`＋`retry_after_s=<remaining>`；`ready_reconfirm_pending` → `AUTOBOOT_QUIET`＋`retry_after_s=5.0`。
4. 再進 `arbiter.submit`（§4.3）。
（`file.push`/`file.pull` 亦走同一 `_resolve_session_id`。`source` 由 RPC 參數決定，client 傳 `source="human:x"` 可繞過 gate。）

**Execute-time（`execute_command`，lock 內）**，順序：
1. `valid_completion_regex` → `INVALID_COMPLETION_REGEX`。
2. `mode` 正規化：`fg→line`、`bg→background`。
3. state==FLASHING → `FLASHING_BUSY`（含 selector）。
4. 無 session／無 bridge／state≠READY → `SESSION_NOT_READY`。
5. 非 human source → `_agent_gate_reject_locked`（:1125）：先 expire；`failed` → `READY_UNCONFIRMED`（`selector`、`hint`、`recommended_action:"self_test"`）；`agent_gate_active()` → `AUTOBOOT_QUIET`（**僅 `selector`，無 `retry_after_s`**）。
6. `session.recovering` → `SESSION_RECOVERING`。
7. `mode=="interactive"` 且 foreground busy → `SESSION_BUSY`。
8. `_refresh_interactive_locked`；有 lease 且 mode≠interactive：非 human source 且 lease 是 human → 標記 `suspend_human_interactive`；否則 `SESSION_INTERACTIVE_BUSY`（含 `interactive_session_id`）。
9. `_begin_foreground_operation_locked`（:1048）：bridge None 或已 busy → `SESSION_BUSY`；否則先 `bridge._set_interactive_admission(False)` 再發 token，`foreground_busy=True`、`fg_cmd_started_mono`、`fg_cmd_expected_duration_s`。
- lock 外：`post.execute()`；需要時 `bridge.suspend_interactive()`；執行 inner；finally：resume（若 suspended）→ `_finish_foreground_operation`（開 admission、lock 外回放 `_set_interactive_admission(True)` 回傳的 deferred payload，再清 token）。

### 5.2 模式
**interactive**（:3719）：將該 session 所有 background capture 設 `done`；開 lease（`owner=source`、`timeout_s=max(timeout_s, hard_timeout_s)`）；有 command 才 `send_command`；回 `{"ok":True,"execution_mode":"interactive","interactive_session_id","stdout":"","status":"interactive"}`。

**line / background**：
1. 非 background → 終結既有 active background capture（設 `done`）。
2. `_is_reboot_command(command)`（:174）→ `_handle_reboot_command`（§5.4）。判定：`\s*echo\s+(?:b|'b'|"b")\s*>\s*/proc/sysrq-trigger\s*` fullmatch；或 shlex 首 token basename 為 `reboot`；`systemctl reboot`；`shutdown` 且任何 token 為 `-r`、`--reboot` 或以 `-r` 開頭。
3. `pre_offset = bridge.rx_snapshot_len()`（絕對偏移）。
4. 有 `completion_regex` → WAL `META`，`source="system"`，`cmd_id`，`payload=b""`，`meta={"event":"command_completion","completion_regex":...}`。
5. background → 送出**前**建立 `BackgroundCapture`（`from_seq=current_seq+1`、`last_seq=current_seq`）、evict、加入 `background_cmd_ids`（#159）。
6. `_mark_session_tx`；`bridge.send_command(command, source, cmd_id)`（補 `\n`）。
7. 完成偵測迴圈：`effective_timeout = max(timeout_s, expected_duration_s)`（有給時）；`silence_limit = min(timeout_s, 30.0)`；`deadline = now + effective_timeout`；每輪 `wait_chunk=min(silence_limit, remaining)`、記 `pre_rx`；`wait_for_regex_from(completion_regex or prompt_regex, pre_offset, wait_chunk)` 命中 → matched；否則若 `rx_snapshot_len()==pre_rx`（該輪完全無 RX）→ 視為靜默直接 break；有 RX 則繼續。
8. 未 matched → `_recover_after_failure`（§5.3）。
9. matched → `stdout=_extract_command_stdout(raw, command, prompt_regex, completion_regex=...)`；回 `{"ok":True,"execution_mode","stdout","partial":False[, "background_capture_id":cmd_id]}`。

`_extract_command_stdout`（:3180）：`clean_text` 後找**最後一個** prompt_regex 匹配起點；`completion_regex` 為 None → 直接裁掉 prompt 之後；否則僅當 prompt 起點 ≥ 完成標記結尾（`len(clean_text(text[:m.end()]))`）才裁；`lstrip("\n").splitlines()`；有 command 時移除開頭 `line.strip().endswith(command.strip())` 的回顯行（可重複）；`"\n".join().strip("\n")`。（完成標記本身會留在 stdout 中。）

**#230 producer completion**：`completion_regex` 取代 prompt 作完成條件；驗證在 RPC（`service.py:893`）、arbiter submit、execute_command 三處；記 WAL META；stdout 擷取依上述規則。

### 5.3 `_recover_after_failure`（:3826）
- human source → 沿用或開 human lease（`timeout=max(timeout_s, hard_timeout_s)`）；回 `{"ok":True,"execution_mode":"interactive","interactive_session_id","status":"interactive","stdout":"","partial":True,"recovery_action":"PROMOTE_HUMAN_INTERACTIVE"}`。
- 非 quiet：依序 `CTRL_C=b"\x03"`、`CTRL_D=b"\x04"`；**每 byte 送出前於 lock 內重驗 `boot_quiet_active()`**（#162），active 即 break；`send_bytes(payload, source="system:recover")`；`wait_for_regex_from(prompt_regex, offset, min(timeout_s,2.0))` 命中 → terminal capture（`error_code="PROMPT_TIMEOUT_RECOVERED"`）、reset reprobe、lock 外 `on_command_flush(sid,"FLUSHED_BY_RECOVERY")`、回 `{"ok":True,"error_code":"PROMPT_TIMEOUT_RECOVERED","stdout","partial":True,"recovery_action":"CTRL_C"|"CTRL_D"}`（session 維持 READY）。
- 皆失敗 → terminal capture（`PROMPT_TIMEOUT`，partial stdout）→ `_transition_to_attached("PROMPT_TIMEOUT")`。
- `source == "system:recover"`（僅 `recover_session` 的 READY 路徑）→ `_force_recover`；`recovered` → `{"ok":True,"error_code":"PROMPT_TIMEOUT_FORCE_RECOVERED","partial":True,"recovery_action":"FORCE_CLEAR_REATTACH","session":...}`。
- 否則 `{"ok":False,"error_code":"PROMPT_TIMEOUT","partial":True,"recovery_action":"NONE"}`。

`_set_terminal_capture_locked(session, *, cmd_id, chunks=None, error_code=None)`（:3497）：cmd_id 空→no-op；無 capture 才新建並回填 chunks（避免與即時累積重複）；設 `last_seq`、`last_activity_mono`、`status = "error" if error_code else "done"`、`error_code`。

### 5.4 `_handle_reboot_command`（:3530）
1. `pre_offset`；lock 內 `arm_boot_quiet()`（**收到命令當下即 arm**）。
2. `console_unlock_env` → `bridge.clear_rx_buffer()`。
3. 送命令；`wait_for_regex_from(prompt_regex, pre_offset, min(timeout_s, 2.0))` 命中 → 只 `clear_boot_quiet()`（**保留 `ready_reconfirm_pending`**，#162）；回 `{"ok":True,"execution_mode","stdout","partial":False}`。
4. human source → T16；回 `{"ok":True,"execution_mode":"interactive","interactive_session_id","status":"interactive","stdout":"","partial":True,"recovery_action":"PROMOTE_HUMAN_INTERACTIVE"}`。
5. 其餘 → T15；回 `{"ok":True,"execution_mode","stdout":"","partial":True,"status":"recovering","recovery_action":"EXPECT_REBOOT"}`。

### 5.5 `_spawn_reboot_recovery(session_id, timeout_s)`（:3392）
執行緒 `serialwrap-reboot-<sid>`：`deadline=start+timeout_s`、`deadline_cap=start+180+timeout_s`。迴圈：quiet blocks → `deadline=min(max(deadline, quiet_until+timeout_s), deadline_cap)`、sleep `min(1.0, quiet_until-now)`、continue；有 bridge → 解析帳密，宣告但未解析 → 告警、`CREDENTIALS_UNRESOLVED`、break；否則 `ensure_ready(bridge, profile, auth)`（例外 → `RECOVERY_ERROR`）；成功 → T17；失敗 → `last_error=err`；bridge None 且裝置在 → `_spawn_attach`；sleep 1.0。結束 → T18。

### 5.6 FLUSHED_BY_RECOVERY 來源彙整
- `unregister_session` 預設碼：任何 `on_detached` 呼叫（detach 全部原因：CLEARED/RELEASED/REBOUND/DEVICE_REMOVED/BRIDGE_DOWN、READY→ATTACHED、reboot 進 RECOVERING、attach 失敗）。
- `on_command_flush`：CTRL_C/CTRL_D 攔截成功（session 仍 READY）。
- 只影響 `status=="accepted"`；在途（running）命令不帶此碼。
- `FLUSHED_BY_SHUTDOWN`：daemon stop。

---

## 6. 自動重探（readiness reprobe，#69/#162）

`reconcile_readiness()`（:1609，每 1s tick）：lock 內對每 session `_prepare_reprobe_locked`；lock 外 `attach`→`_spawn_attach(by_id)`、`probe`→`_spawn_reprobe_probe`。另每 `_HUMAN_PEER_GRACE_S`（3s）：以第一個 bridge `_enumerate_all_held_paths()` 共用 `/proc` 掃描，對每 bridge `reap_stale_consoles(held_slave_paths=held)`；再做 lease-backed 自癒：snapshot 無 owner/無 suspended owner/非 `agent_active`/非 `flash_mode`、有 `primary_client_id`、primary 的 vtty 在 held（或 fallback `console_has_external_peer`）、session READY/ATTACHED 且無 lease → `bridge.try_grant_interactive_if_idle(f"human:{cid}")` 成功 → `_record_self_heal_lease_locked`（timeout `max(hard_timeout_s, 86400)`，不呼叫 set_interactive_owner）。

`_prepare_reprobe_locked(session, now)`（:1390）回 `None` 或 `(action, session, bridge, by_id)`，順序：
1. by_id 空、在 `_released_by_ids`、不在 `_devices` → None。
2. state ∈ {RELEASED, FLASHING, ATTACHING, RECOVERING} → None。
3. READY → `expire_ready_reconfirm(now)`。
4. `ready_reconfirm = state==READY and pending and not _boot_quiet_blocks_probe`；READY 且非 ready_reconfirm → None。
5. `last_error == CREDENTIALS_UNRESOLVED` → None。
6. `reprobe_exhausted or attempts >= 10` → 設 exhausted、next=None；若 ready_reconfirm → `expire_ready_reconfirm(force=True)`；None。
7. `next_reprobe_at` 未到 → None。
8. quiet blocks → None（不計 attempt）。
9. 非 ready_reconfirm 且 `not _is_reprobe_prompt_error(state, last_error)` → None。
10. `not _rx_idle_enough`（`last_rx_mono==0 or now-last_rx_mono >= 3.0`）→ None。
11. `_human_active_locked`（human lease、external peer、bridge owner==lease.owner、`now - last_human_input_at <= 60`）→ None。
12. ATTACHED/READY：bridge None 或已 inflight → None；`next_reprobe_at = now + min(2.0*2**attempts, 15.0)`、加入 inflight、回 `("probe", ...)`。
13. DETACHED：`state="ATTACHING"`、`last_error=None`、`_record_reprobe_attempt_locked`、回 `("attach", ...)`。

`_is_reprobe_prompt_error(state, last_error)`（:1332）：ATTACHED → `PROMPT_UNAVAILABLE`、`PROMPT_TIMEOUT`、`*_PROMPT_TIMEOUT`、`RX_FLOOD`、`TRANSPORT_STALL`；DETACHED → 同上但**不含 `PROMPT_UNAVAILABLE`**；其他 state → False。（LOGIN_REQUIRED、READY_NONCE_TIMEOUT、BOOTLOADER_STUCK、DEVICE_NOT_FOUND 等不重探；`*_PROMPT_TIMEOUT` 包含 `READY_PROMPT_TIMEOUT`、`LOGIN_PROMPT_TIMEOUT`、`BCM/SHELL/PRPL_PROMPT_TIMEOUT`。）

`_record_reprobe_attempt_locked`（:1378）：attempts+=1；≥10 → exhausted、next=None；否則 `next = now + min(2.0 * 2**(attempts-1), 15.0)`。

`_spawn_reprobe_probe(sid, bridge)`（:1551）：thread `serialwrap-reprobe-<sid>`；`probe_lock.acquire(blocking=False)`（被 manual attach/recover 持有則跳過）；`_reprobe_target_still_valid_locked`（:1507：bridge 同、裝置在線非 released、state∈{ATTACHED,READY}；READY 須 expire 後仍 pending 且 RX tail 非 bootloader（是則 force expire 回 False）；非 exhausted；ATTACHED 須 prompt error；非 quiet；非 human active）→ `_probe_existing_bridge` → `_finish_probe_reprobe`；finally 釋放 probe lock、移出 inflight。

`_finish_probe_reprobe`（:1469）：bridge 不同 → return；READY → reset reprobe；否則 record attempt；pending → `ready_reconfirm_attempts += 1`、expire；`_bootloader_evidence` 命中 → `last_error=BOOTLOADER_STUCK`、`last_error_detail=f"matched_prompt={pattern}"`、exhausted、next=None、force expire。（READY 確認 probe 失敗會因 `_probe_existing_bridge` 把 state 改成 ATTACHED，見 T11。）

`_bootloader_evidence(session, bridge)`（:1162）：`clean_text(bridge.rx_tail(512))`，patterns `profile.bootloader_prompts or UBOOT_FALLBACK_PROMPTS`，`_matches_any_bootloader_prompt`（:125：取最後一個非空白行，`re.search` 各 pattern，無效 regex 略過，回第一個命中的 pattern 字串）。

---

## 7. Autoboot Quiet Gate（#130/#139/#162）

- **Arm 點**：`_update_boot_quiet_locked`（:2327，`_on_bridge_rx` 每個 chunk、含 foreground 期間）：`window = boot_banner_tail[-256:] + chunk`（chunk 不截）；`detect_boot_banner(window)` → `arm_boot_quiet()`、return。另 `_handle_reboot_command` 收到 reboot 命令即 arm。
- **解除點**：保存 `tail = window[-256:]`；quiet 已 armed 時：tail 尾行命中 `profile.bootloader_prompts`（**不用 fallback**）→ 不解除；否則 `login_regex` 或 `prompt_regex` `re.search(tail)` 命中 → `clear_boot_quiet()`。逾 180s 自然失效。
- **只清 quiet、不清 pending**；pending 只由 `confirm_ready()`（attach probe 成功 T3/T10、reboot recovery 成功 T17、self-test nonce 成功）或 `expire_ready_reconfirm()` 清。
- `_boot_quiet_blocks_probe(session)`（:1193）=`boot_quiet_active() and not profile.console_unlock_env`（console_unlock profile 不受 quiet 擋自動 probe）。
- **被 gate 的自動 system TX**：reboot recovery 迴圈、reprobe（prepare + final validate）、`_attach_by_id` attach probe（合成 `PROMPT_UNAVAILABLE`，不精煉）、`_probe_existing_bridge`（attach/recover 共用）、`_recover_after_failure` CTRL_C/CTRL_D（逐 byte 重驗；此處用 `boot_quiet_active()` 而非 `_blocks_probe`）、`_self_test_impl` READY nonce probe（用 `boot_quiet_active()`）、`_reresolve_profile_on_reattach` 偵測。
- **不被 gate**：human console bytes、interactive lease TX（open command／send）、`_attach_by_id_dynamic` 新裝置偵測與 attach probe（無既有 session，無 quiet 狀態）。
- **Agent 顯式命令**：submit-time（service）與 execute-time／file_push／file_pull（`_agent_gate_reject_locked`）雙層；回應形狀見 §5.1。`READY_UNCONFIRMED` 帶 `recommended_action:"self_test"`。
- `AUTOBOOT_QUIET` 亦為 self-test classification（READY 且 `boot_quiet_active()`，`recommended_action:"wait"`）。
- READY 時 quiet 過期但 pending → 自動重探排確認 probe（§6 step 4、12）。

---

## 8. Login FSM（`login_fsm.py`）

### 8.1 平台差異
平台只影響 `_prompt_timeout_error(sp)`（:119）：`bcm` → `BCM_PROMPT_TIMEOUT`；`shell` → `SHELL_PROMPT_TIMEOUT`；其他（`prpl`、`passthrough`）→ `PRPL_PROMPT_TIMEOUT`。其餘差異完全來自 profile 欄位（regex、`post_login_cmd`、`timeout_s`、`console_unlock_env`、`ready_probe`）。

`command_capable`（`config.py:45, 78`）= `bool((ready_probe or "").strip())`。passthrough 的 `others-template`（`ready_probe:""`）不 probe、停 ATTACHED、`last_error=None`；`uboot-template`（passthrough 但有 ready_probe）**會** probe 並可升 READY。

出貨 profile（`sw_core/assets/profiles/default.yaml`）：
| template | platform | prompt_regex | login_regex | password_regex | 其他 |
|---|---|---|---|---|---|
| prpl-template | prpl | `(?m)^root@prplOS:.*# ` | `(?mi)^login:\s*$` | `(?mi)^password:\s*$` | bootloader `^=> $`、`^U-Boot> $` |
| op3-template | shell | `.*[$#] $` | `(?mi)^.*login:\s*$` | `(?mi)^password:\s*$` | user_env `SW_OPI_U`、pass_env `SW_OPI_P`、env_file `OPI.env` |
| brcm-template | bcm | `(?m)^(?:[ \t]*\|.*[^>#\s])[>#][ \t]*$` | `(?mi)login:\s*$` | `(?mi)password:\s*$` | post_login_cmd `sh`、`BRCM_USER/BRCM_PASS`、env_file `brcm.env`、timeout_s 15、bootloader `^CFE> $`、`^=> $`、`^BCM\d+>> $` |
| smart4R-profile | shell | `(?m)^root@Speedport Smart 4 Typ [AB]:[^\r\n#]*#[ \t]*` | `$^` | `$^` | console_unlock_env `SMART4R_CONSOLE_UNLOCK`、idle 5、timeout 120、hard 240、bootloader `(?m)^CFE>\s*$` |
| uboot-template | passthrough | `(?mi)^(=>\|u-boot>\|cfe>) $` | `$^` | `$^` | ready_probe 預設 |
| others-template | passthrough | `.*` | `$^` | `$^` | ready_probe `""` |

SessionProfile／ProfileTemplate 預設：`platform="prpl"`（template）、`prompt_regex=r"(?m)^root@prplOS:.*# "`、`login_regex=r"(?mi)^login:\\s*$"`（注意 Python raw string 內是兩個反斜線，實際 regex 為 `\\s`＝字面反斜線+s——**疑似預設值 bug**，YAML 載入值正確）、`password_regex` 同、`post_login_cmd=""`、`ready_probe="echo __READY__${nonce}"`、`console_unlock_idle_s=5.0`、`timeout_s=10.0`、`quiet_window_s=2.0`、`hard_timeout_s=60.0`、`bootloader_prompts=()`、`max_console_line_chars=None`。

### 8.2 公開入口
- `probe_ready(bridge, sp)`（:236）：`_probe_prompt` 失敗 → `_maybe_reclassify_flood(_classify_non_ready_state)`（`LOGIN_REQUIRED` 或 `PROMPT_UNAVAILABLE`）；成功 → `_finalize_ready`，失敗碼經 flood 反分類。
- `ensure_ready(bridge, sp, auth=None)`（:247）：`console_unlock_env` → `_ensure_console_unlocked`；`_probe_prompt` 失敗 → `_maybe_login`；有 err → flood 反分類回傳；`ok=False, err=None`（不需登入）→ `_prompt_timeout_error`；之後 `_finalize_ready`。
- session_manager 選擇：`login_regex or console_unlock_env` 為真（attach 路徑另含 `pending_auto_login`）時解析帳密：宣告但未解析 → `CREDENTIALS_UNRESOLVED`；`auth.console_unlock or (username and password)` → `ensure_ready`；否則 `probe_ready`（讓 human 手動登入）。無 login_regex → `probe_ready`。reboot recovery 一律 `ensure_ready`。

### 8.3 步驟與逾時（每步皆 `sp.timeout_s`，除註明）
- `_probe_prompt`（:127）：`clear_rx_buffer()` → `send_command("", source="system")`（送 `\n`）→ `wait_for_regex(prompt_regex, timeout_s)`。
- `_maybe_login`（:208）：`_resolve_login_user`（auth.username → `os.environ[user_env]`（缺→`USER_ENV_MISSING`）→ `sp.username`）；`needs_login = user or pass_env`，否則 `(False, None)`；wait login_regex → `LOGIN_PROMPT_TIMEOUT`；無 user → `LOGIN_USER_REQUIRED`；送 user；wait password_regex：命中 → `_resolve_login_password`（auth.password → 無 pass_env `PASS_ENV_REQUIRED` → env 缺 `PASS_ENV_MISSING`）→ `send_secret`（`source="system:secret"`、`log=False` 不進 WAL）→ wait prompt → `<PLATFORM>_PROMPT_TIMEOUT` → `clear_rx_buffer()`；未見 password prompt → wait prompt → `<PLATFORM>_PROMPT_TIMEOUT`。
- `_finalize_ready`（:165）：有 `post_login_cmd`：rx_tail 命中 login/password → `LOGIN_REQUIRED`；送 cmd；wait prompt → `POST_LOGIN_CMD_TIMEOUT`（失敗時再檢 login/password → `LOGIN_REQUIRED`）。Nonce：`uuid4().hex[:8]`；`probe = ready_probe.replace("${nonce}", nonce)`；console_unlock → 先 clear buffer；送 probe；pattern = console_unlock 且 ready_probe 為預設 → `rf"(?m)^__READY__{re.escape(nonce)}\r?$"`，否則**裸 nonce 字串**；wait → `READY_NONCE_TIMEOUT`（失敗再檢 login → `LOGIN_REQUIRED`）；wait prompt → `READY_PROMPT_TIMEOUT`。
- **重要行為**：`wait_for_regex` 掃描整個 RX buffer（非自送出偏移起），故 (a) 裸 nonce 會被 target 的命令回顯（`echo __READY__<nonce>`）滿足，(b) 其後的 prompt 等待會被 `_probe_prompt` 早已收到的 prompt 滿足；post_login_cmd 的 prompt 等待同理。只有 console_unlock 路徑（清 buffer＋行首錨定）真正驗證輸出。Rust 1:1 承接須決定是否保留此語意。
- `_ensure_console_unlocked`（:282）：無 secret → `CREDENTIALS_UNRESOLVED`；`deadline=now+timeout_s`；`_wait_console_idle`（:272，至少經過 `console_unlock_idle_s` 且 `bridge.rx_idle_s() >= idle_s`，每 50ms 輪詢、完全不 TX）失敗 → `CONSOLE_UNLOCK_IDLE_TIMEOUT`；尾行命中 bootloader_prompts → `CONSOLE_UNLOCK_BOOTLOADER`；尾行無 prompt → clear、送 `\n`、wait prompt `min(1.0, remaining)`；仍無 → 再 idle 等待（同錯誤碼）、bootloader 檢查、`rx_text_from(0)` 有 prompt → finalize；`rx_idle_s < idle_s` → `CONSOLE_UNLOCK_IDLE_TIMEOUT`；info log（不含 secret）→ clear → `send_secret(secret)` → wait prompt → `CONSOLE_UNLOCK_PROMPT_TIMEOUT`；最後 `_finalize_ready`。
- `matches_login_or_password(text, sp)`（:140）：login_regex、password_regex 對 `clean_text(text)` 或原文 `re.search`；無效 regex 略過。
- `_maybe_reclassify_flood(bridge, err)`（:47）：err ∈ `_FLOOD_MASKABLE_ERRORS`={`PROMPT_UNAVAILABLE`,`READY_NONCE_TIMEOUT`,`READY_PROMPT_TIMEOUT`,`POST_LOGIN_CMD_TIMEOUT`,`LOGIN_PROMPT_TIMEOUT`} 或以 `_PROMPT_TIMEOUT` 結尾，且 `rx_stats()["rx_bytes_last_10s"] >= 20000` → `RX_FLOOD`。
- `LOGIN_FSM_DETAIL_ERRORS`（:31）={`USER_ENV_MISSING`,`PASS_ENV_REQUIRED`,`PASS_ENV_MISSING`,`LOGIN_USER_REQUIRED`,`LOGIN_PROMPT_TIMEOUT`,`BCM_PROMPT_TIMEOUT`,`SHELL_PROMPT_TIMEOUT`,`PRPL_PROMPT_TIMEOUT`,`POST_LOGIN_CMD_TIMEOUT`,`READY_NONCE_TIMEOUT`,`READY_PROMPT_TIMEOUT`,`LOGIN_REQUIRED`}：未翻轉 stall 時 `last_error_detail = clean_text(rx_tail())[-300:]`（`_login_fsm_rx_tail_detail` :155）。`CONSOLE_UNLOCK_*` 不在集合內。
- `detect_boot_banner(text)`（:76）：任一 `BOOT_BANNER_PATTERNS` substring。
- `detect_template(bridge, templates, probe_timeout_s=3.0)`（:331）：clear → 送 `\n` → `sleep(3.0)` → `rx_tail()`（4096 chars）；略過 passthrough 與 console_unlock template；第一個 prompt_regex 命中者回傳；否則第一個 login_regex 命中者；否則 None。

### 8.4 Probe 失敗精煉（`_refine_probe_failure` :1267，#150/#174）
只在真的送過 probe 後呼叫（boot-quiet 合成 `PROMPT_UNAVAILABLE` 排除）。`rx_before`=probe 前 `bridge.rx_total_bytes()`；`classify_probe_failure(err, rx_delta, last_rx_mono, now)`（`transport_stall.py:41`）四條件皆成立才翻 `TRANSPORT_STALL`：`is_transport_refinable_error(err)`（:27，`PROMPT_UNAVAILABLE`/`LOGIN_PROMPT_TIMEOUT`/`*_PROMPT_TIMEOUT`）、`rx_delta==0`、`last_rx_mono>0`、`now-last_rx_mono >= 30.0`。翻轉 → detail=`transport_stall_hint(real_path, rx_age_s)`（:94；`resolve_usb_busid` :72 由 `/sys/class/tty/<name>/device` realpath 取 `\d+-\d+(\.\d+)*` 段，回 sudo authorized 0→1 指令）；`transport_stall_warned` 去重 → warning log + WAL `META`（`event:"transport_stall_suspected"`、`real_path`、`rx_age_s`）。未翻轉且 err ∈ `LOGIN_FSM_DETAIL_ERRORS` → rx tail detail。

---

## 9. 帳密（`auth.py`）與 CREDENTIALS_UNRESOLVED（#140）

- `SessionAuth`（`auth.py:18`，frozen）：`username: str|None=None`、`password: str|None=None`（repr=False）、`console_unlock: str|None=None`（repr=False）。
- `AuthResolution`（:27，frozen）：`reason: str="ok"`（`ok`/`env_file_missing`/`env_file_unreadable`/`key_absent`/`not_configured`）、`env_file_path: str|None=None`（`expanduser` 後）。
- `parse_env_file(path)`（:53）：逐行 strip；空行/`#` 略過；`export ` 前綴移除；無 `=` 略過；key strip 空略過；value strip；首尾相同引號（`'`/`"`）且長度≥2 去引號；無跳脫處理。
- `resolve_session_auth(sp) -> (SessionAuth, AuthResolution)`（:83）：env_file `expanduser`（**相對路徑以 daemon cwd 解析**）；`isfile` → parse（例外 → unreadable + warning）；否則 missing + warning。`username = local_env[user_env] or environ[user_env]`，再 fallback `sp.username`；`password = local_env[pass_env] or environ[pass_env]`；`console_unlock` 同法。`declared = user_env or pass_env or env_file or console_unlock_env`；未宣告 → `not_configured`；`(console_unlock if console_unlock_env else username and password)` 真 → `ok`；否則 missing → unreadable → `key_absent`。
- Gate：`_credentials_declared_but_unresolved(profile, auth_res)`（:1181）= `declared and reason != "ok"`。
- 進入終態的 4 個點：`_probe_existing_bridge`（:2490-2497）、`_attach_by_id`（:2641-2647）、`_attach_by_id_dynamic`（:3077-3083）、`_spawn_reboot_recovery`（:3426-3433）；另 `login_fsm._ensure_console_unlocked` 在 secret 為空時回同字串（理論上已被 gate 先擋）。結果 state：attach 路徑 → `ATTACHED`＋`last_error=CREDENTIALS_UNRESOLVED`（bridge 保持開啟）；reboot recovery → break 後逾時收尾為 `ATTACHED`（保留 last_error）。
- 告警 `_emit_credentials_unresolved_warning`（:1197）：`credentials_warned` 去重；warning log（含 reason、env_file，不含帳密）；WAL `META`、`source="system"`、`payload=b""`、`meta={"event":"credentials_unresolved","reason","env_file":path or ""}`（best-effort）。
- 不自動重探：`_prepare_reprobe_locked` step 5；`_is_reprobe_prompt_error` 本就不含此碼。
- 重新介入（`credentials_warned=False`）：`attach_session`（ATTACHED probe 分支與無 bridge 分支）、`recover_session`。`clear_session`/`attach_device`/`bind_session` **不**重置 `credentials_warned`（再進終態不會再告警），但會重新解析帳密。

---

## 10. Interactive Lease

### 10.1 `interactive_open(selector, *, owner="agent", timeout_s=60.0, command="", allow_attached=False)`（:4009）
RPC：`session.interactive_open`（`service.py:840`；`timeout_s = float(... or 60.0)`；`allow_attached=bool(...)`）。外層 `while True` 迴圈：若 `_refresh_interactive_locked` 的 post 需要 resume → lock 外執行後重試。
- 無 session／無 bridge → `SESSION_NOT_READY`。
- **READY**：foreground busy → `SESSION_BUSY`；refresh；無 lease → 開 lease（`recovery_mode=False`）回 `{"ok":True,"interactive_id","session","recovery_mode":False}`；有 lease：human lease 且 `human_active=False` 且 owner 非 human → **soft preempt**（#53）：pop human lease、`_stashed_human_lease=existing`、`bridge.suspend_interactive()`、開 lease（`suspended_human=True`）、回含 `"soft_preempted":True`；否則 `SESSION_INTERACTIVE_BUSY`（含 `interactive_session_id`）。
- 非 READY 且 `allow_attached=False` → `SESSION_NOT_READY`。
- **ATTACHED + allow_attached**：bridge snapshot `running`、`serial_alive`、`vtty_alive` 任一假 → `SESSION_NOT_READY`；`rx_tail(512)` clean 後：尾行命中 `profile.bootloader_prompts`（**不用 fallback**）→ 一般 recovery；否則 `detect_boot_banner` → `boot_interrupt=True`；否則 `matches_login_or_password` → `login_required=True`（#174）；否則 `SESSION_NOT_READY` + `"error_detail":"NOT_BOOTLOADER"`。`timeout = min(timeout_s, 120.0)`；無 lease → 開 recovery lease（`recovery_mode=True`）；human lease → stash＋suspend＋開（`suspended_human=True`）；agent lease → `SESSION_INTERACTIVE_BUSY`。回 `{"ok":True,"interactive_id","session","recovery_mode":True[, "boot_interrupt":True][, "login_required":True]}`。
- 其他 state（FLASHING、RECOVERING、ATTACHING…）→ `SESSION_NOT_READY`。
- 成功後 lock 外：有 command → `_mark_session_tx`、`send_command(command, source=owner, cmd_id=None)`（不受 quiet gate）。
- `_open_interactive_locked`（:3200）：uuid4 hex id、登記、`bridge.set_interactive_owner(owner)`（human owner 在 admission blocked 時進 pending owner）。

### 10.2 `interactive_send(interactive_id, *, data, encoding="plain")`（:4212）
不存在或 `status!="active"` → `INTERACTIVE_NOT_FOUND`；`expired()` → `status="expired"`、close、`INTERACTIVE_EXPIRED`；無 session/bridge → `SESSION_NOT_READY`；FLASHING → `FLASHING_BUSY`；foreground busy → `SESSION_BUSY`；否則編碼並 `send_bytes(payload, source=lease.owner)`、`touch()`、回 `{"ok":True,"interactive_id","bytes":len}`。
`_encode_interactive_payload`（:4191）：`plain` → UTF-8 replace；`base64` → `b64decode`（錯誤丟例外）；`key` → `enter=\n`、`tab=\t`、`escape=\x1b`、`ctrl-c=\x03`、`ctrl-d=\x04`、`up=\x1b[A`、`down=\x1b[B`、`right=\x1b[C`、`left=\x1b[D`（大小寫不敏感、strip）；其他 → `ValueError("INVALID_INTERACTIVE_ENCODING")`（穿越至 RPC 層，POSIX 被包成 `error_code:"EXCEPTION"`，`rpc_posix.py:59`）。**不檢查 session state**（ATTACHED／RECOVERING 皆可送，只擋 FLASHING）。

### 10.3 `interactive_status(interactive_id, *, screen_chars=2048)`（:4243）
不存在 → `INTERACTIVE_NOT_FOUND`（不看 status，已 closed 但仍在 dict 不可能——close 會 pop）；無 session/bridge → `SESSION_NOT_READY`；先判 expired → `INTERACTIVE_EXPIRED`；否則 **`touch()`（status 會延長 lease）** 並回 `{"ok":True,"interactive_id","owner","status","recovery_mode","screen":clean_text(rx_tail(screen_chars)),"session"}`。

### 10.4 `interactive_close(interactive_id)`（:4272）
不存在 → `INTERACTIVE_NOT_FOUND`；有 session → `_close_interactive_locked`；否則 pop；回 `{"ok":True,"interactive_id"}`。

`_close_interactive_locked(session, *, interactive_id=None, expected_owner=None)`（:3225）：owner 不符 → `(None, no-op)`；lease `status="closed"`、pop。若 `suspended_human`：取出 stash；`post.needs_resume=True`；stash 未過期且其 client 仍有 external peer → 還原 lease 並 `set_interactive_owner(stash.owner)`；否則清 `interactive_session_id`、owner None、`clear_owner_after_resume=True`。一般 close → `interactive_session_id=None`、owner None。resume（lock 外）會回放 deferred buffer。**還原／回放不檢查期間是否發生 banner、quiet、recovery 等上下文失效**（僅 detach 會因 `bridge.stop` 清掉）。

### 10.5 到期與 refresh（`_refresh_interactive_locked` :3281）
- **無背景到期 timer**；到期在 refresh/send/status 被動判定。
- lease 已不在 dict 或 bridge None → close。
- human lease：不檢查 `expired()`；peer 不在 → 首次記 `peer_lost_at`、grace 3.0s 內持有、逾時 `detach_console(client_id)` + close；peer 在 → 清 `peer_lost_at`；bridge `interactive_owner != lease.owner` → close。
- agent lease：`expired()` → close，回還原後的 lease（若有）。
- `_lease_context(lease, bridge=None)`（:3322）：`{"interactive_owner","human_attached","human_active"(最後鍵入 ≤60s),"recovery_mode"}`。
- human console lease：`attach_console` 在 READY/ATTACHED 且無 lease 時自動開（`owner=f"human:{client_id}"`、`timeout=max(hard_timeout_s, 86400)`），回應 `interactive_session_id`、`interactive_owner`（= 非 foreground busy）。Windows（`client_id is None`）不開 lease。
- `_record_self_heal_lease_locked`、`_restore_retained_human_console_locked`（attach 成功後，若 retained human owner 的 console 仍在且無 lease → 重開）。
- `_wait_for_human_interactive_release`（:3363）為**未使用的 dead code**。

---

## 11. Self-test（`self_test` :4286 / `_self_test_impl` :4305）

RPC `session.self_test`：`timeout_s = float(... or 2.0)`、`strict_human_lock` 經 `_coerce_rpc_bool`。外層一律補 `command_capable`（取巢狀 session，無則 False）。所有成功分支附 `**lease_context`（4 鍵）。

| 順序 | 條件 | classification | recommended_action | 額外欄位 |
|---|---|---|---|---|
| 0 | 查無 | (`ok:False`,`SESSION_NOT_FOUND`) | — | lease_context(None) |
| 1 | state==RELEASED | `RELEASED` | holder 空→`device_attach`，否則 `wait_external_flash` | `released_by`、`released_at`、`reason`、`external_holder`（pids 或 `"none"`）、`reclaimable`；`/proc` 掃描在 lock 外 |
| 2 | `session.recovering` | `SESSION_RECOVERING` | `wait` | （不 refresh lease） |
| 3 | `strict_human_lock` 且 human lease | `HUMAN_INTERACTIVE_ACTIVE` | `wait_or_detach_console` | `interactive_id` |
| 4 | 裝置不在 `_devices` | `DEVICE_MISSING` | `check_cable_or_bind` | |
| 5 | `attached_real_path` ≠ 目前 real_path | `DEVICE_REBOUND_REQUIRED` | `reattach` | `attached_real_path`、`current_real_path` |
| 6 | bridge None | `BRIDGE_DOWN` | `attach` | `current_real_path` |
| 7 | snapshot `!running or !serial_alive` | `BRIDGE_DOWN` | `recover` | `current_real_path` |
| 8 | `!vtty_alive` | `VTTY_STALE` | `console_attach` | `attached_vtty` |
| 9 | ATTACHED & platform passthrough | `PASSTHROUGH` | `console_attach`；`profile_source=="fallback"` 且 `_suggest_profile_from_rx` 命中 → `pin_profile` | `suggested_profile`、`hint` |
| 10 | ATTACHED & `last_error=="LOGIN_REQUIRED"` | `LOGIN_REQUIRED` | `console_attach` | |
| 11 | ATTACHED & `last_error=="REBOOTING"` | `REBOOTING` | `wait_or_console_attach` | |
| 12 | ATTACHED & bootloader（prompts or fallback；或 last_error BOOTLOADER_STUCK） | `BOOTLOADER` | `recover_interactive` | `matched_prompt`、`rx_tail` |
| 13 | ATTACHED & (`last_error==RX_FLOOD` 或 rx_bytes_last_10s ≥ 20000) | `RX_FLOOD` | `wait` | `rx_bytes_last_10s` |
| 14 | ATTACHED 其他 | `ATTACHED_NOT_READY` | `console_attach` | ATTACHED 分支共有：`attached_real_path`、`current_real_path`、`attached_vtty`、`bridge_generation` |
| 15 | 非 ATTACHED 且 `boot_quiet_active()` | `AUTOBOOT_QUIET` | `wait` | |
| 16 | 其他（READY，或 FLASHING/ATTACHING 等有 bridge 者）→ nonce probe：human lease 時 suspend；`last_probe_at`；`send_command(probe, source="system:self_test")`；`wait_for_regex_from(nonce, offset, timeout_s)` 失敗：flood → `RX_FLOOD`/`wait`（`probe_ok:False`、`rx_bytes_last_10s`）；否則 `TARGET_UNRESPONSIVE`/`recover`（`probe_ok:False`）。成功：再 wait prompt（結果忽略）、lock 內 `confirm_ready()` → `OK`/`none`（`probe_ok:True`、`attached_vtty`、`bridge_generation`） |

注意：self-test nonce probe **不取 foreground operation token、不檢查 `foreground_busy`**，可與執行中的 arbiter 命令交錯 TX（§13 Gap G-6）。FLASHING 時 probe bytes 被 `send_bytes` flash gate 靜默丟棄 → `TARGET_UNRESPONSIVE`。

---

## 12. Recover / Force recover / Release / Attach / FLASHING

### 12.1 `recover_session(selector, *, timeout_s=2.0, force=False)`（:4582）
RPC `session.recover`（`timeout_s=float(... or 2.0)`、`force=bool(...)`）。
1. 查無 → `SESSION_NOT_FOUND`。
2. RELEASED／by_id released → `{"ok":True,"released":True,"recommended_action":"device_attach","session"}`。
3. flash busy → `FLASHING_BUSY`。
4. reset reprobe、`credentials_warned=False`。
5. 無 bridge：裝置在 → `ATTACHING`、`_spawn_attach`、`{"ok":True,"recovering":False,"action":"REATTACH","session"}`；否則 `SESSION_NOT_READY`。
6. ATTACHED：force → `_force_recover`；否則 REPROBE（取 per-session probe lock blocking → `_probe_existing_bridge`）：helper ok False → force? force_recover : 原樣；回 `{"ok":True,"recovering":False,"action":"REPROBE","recovered":bool,"session"}`；未恢復加 `error_code = session.last_error or "SESSION_NOT_READY"`，bootloader 證據或 `BOOTLOADER_STUCK` → `classification:"BOOTLOADER"`、`recommended_action:"recover_interactive"`、`matched_prompt`；force → `_force_recover`。
7. 非 READY（ATTACHING/RECOVERING/FLASHING*）：force → `_force_recover`；否則 `SESSION_NOT_READY`。
8. READY → `_recover_after_failure(cmd_id="", source="system:recover", command="", pre_offset=now)`（送 CTRL_C/CTRL_D；**不取 foreground token**）。

### 12.2 `_force_recover(session)`（:4669）
`clear_session(session_id)`（detach `CLEARED`→`ATTACHING`→spawn attach）；`for _ in range(10): sleep(1.0); get_session_state → READY` → `{"ok":True,"recovering":False,"action":"FORCE_CLEAR_REATTACH","recovered":True,"session"}`；逾時 → `{"ok":False,"error_code":"FORCE_RECOVER_TIMEOUT","action":"FORCE_CLEAR_REATTACH","recovered":False,"session"}`。總上限約 10 秒，阻塞呼叫端 RPC。

### 12.3 `release_device(selector, *, source="cli", reason=None)`（:1852）
RPC `device.release`（selector 必填，`source` 預設 `"cli"`）。查無 → `SESSION_NOT_FOUND`；flash busy → `FLASHING_BUSY`（含 session）；已 RELEASED → `{"ok":True,"already_released":True,"session"}`；否則 T24；回 `{"ok":True,"session","closed_consoles":int,"aborted_cmd":bool}`。
- 實際關閉：`bridge.stop()` 在 manager lock 內執行（reader thread join 2.0s、console thread join 2.0s，逾時照樣繼續）；**沒有 drain receipt、沒有確認 OS handle 已關閉即回成功**。
- 在途命令：foreground token 被撤銷，但 arbiter worker 仍在 `execute_command` 內；等待結束後 `_recover_after_failure` 對已 stop 的 bridge `send_bytes` → `RuntimeError("serial not ready")` → worker 標 `SEND_FAILED`（或因 quiet 跳過按鍵後 `PROMPT_TIMEOUT`）。`aborted_cmd` 只是 release 當下的 `foreground_busy` 快照。
- 未執行命令：`on_detached` → `FLUSHED_BY_RECOVERY`。

### 12.4 `attach_device(selector, *, force=False)`（:2004）
RPC `device.attach`。查無 → `SESSION_NOT_FOUND`；已有 bridge 且非 RELEASED → `{"ok":True,"already_attached":True,"session"}`；裝置不在 → `DEVICE_NOT_PRESENT`（含 `device_by_id`）；非 force：`_probe_external_holder(real_path)`（:1954，掃 `/proc/*/fd`，比 readlink 字串或 `st_rdev`，排除自身 pid）有 pids → `DEVICE_STILL_HELD`（`pids`）；否則 T25，回 `{"ok":True,"session"}`（attach 非同步進行）。

### 12.5 其他
- `attach_session`（:2150）：無 by_id → `DEVICE_NOT_BOUND`；RELEASED → `{"ok":True,"released":True,"recommended_action":"device_attach"}`；有 bridge：human lease → `_attach_state_result`；ATTACHED → 同步 probe（probe 中被 release → released payload）；其他 → `_attach_state_result`。`_attach_state_result`（:2135）：command_capable 且 state ∉ {READY, ATTACHING} → `ok:False`，`error_code` = `FLASHING→FLASHING_BUSY`、`RECOVERING→SESSION_RECOVERING`、否則 `last_error or "NOT_READY"`。
- `clear_session`（:1829）：RELEASED → `{"ok":True,"released":True,"session"}`。
- `bind_session`（:2034）：空 by_id → `INVALID_ARGS`；已被他 session 綁 → `DEVICE_ALREADY_BOUND`（`device_by_id`、`session_id`）；冪等 → `already_bound`。
- `pin_session`／`unpin_session`（:2111/:2124）：`UNKNOWN_PROFILE`、`DEVICE_NOT_FOUND`、`PROFILE_IS_EXPLICIT`（yaml-target）。
- FLASHING：`mark_flash_critical(com)`／`unmark_flash_critical(com)`／`collect_flash_candidates_and_mark()`（非 command_capable 且 READY/ATTACHED，原子標記，回 `{com, by_id, real_path, command_capable:False}`）；`enter_flashing`（`already_flashing`）／`exit_flashing`（`not_flashing`）。bridge 全程不關。FLASHING 下 `execute_command`/`interactive_send` → `FLASHING_BUSY`；`interactive_open` → `SESSION_NOT_READY`；submit-time → `SESSION_NOT_READY`。

---

## 13. 活動欄位、Log capture、Background result

- RX 時間戳：`_mark_session_rx`（:2320，每個非空 cleaned chunk，同時重臂 `transport_stall_warned`）；TX：`_mark_session_tx`（:2376，在各送出點**送出前**呼叫：命令、reboot、CTRL_C/D、interactive open/send、self-test；login FSM／reprobe probe 內部 TX **不更新** `last_tx_*`）。human console raw bytes 也不經此（不更新 last_tx）。
- `rx_bytes_last_10s` / `rx_rate_bps`：bridge `rx_stats()`（`uart_io.py:1177`，raw bytes、10s 視窗、`int(total/10.0)`）。`last_rx_age_s` / `last_tx_age_s`：秒、1 位小數、從未 → null。`idle_for_ms`：毫秒 int 或 null。`last_error_detail`：見 §8.4、§6（BOOTLOADER_STUCK `matched_prompt=...`）。
- `_on_bridge_rx`（:2385）：先通知 `_rx_observers(com, data, wal.current_seq)`（lock 外，例外吞掉）；`clean_text(decode utf-8 replace)` 空則 return；lock 內 mark RX → quiet 維護 → 所有 active background capture `add_chunk`＋更新 `last_activity_mono`、`last_seq`（**不受 foreground_busy 影響**，#159）→ `foreground_busy` 時 return → agent log capture 寫入（**foreground 期間不寫 agent log**）。
- Log capture：`log_start(selector)`（:4764）：已 active → `already_active`；`log_dir = profile.log_dir or LOG_DIR`；`makedirs`；檔名 `f"{com}_{time.strftime('%y%m%d-%H%M%S')}.log"`（本地時間）；`open(..., "a", utf-8)` 失敗 → `LOG_OPEN_FAILED`（`detail`）；回 `{"ok":True,"capture_id","log_path","session"}`。寫入內容為 cleaned 文字、每 chunk flush；`byte_count += len(utf-8)`、`line_count += count("\n")`。`log_stop`（:4803）：無 → `NO_ACTIVE_CAPTURE`；回 `{capture_id, log_path, line_count, byte_count, started_at}`。`log_status`（:4822）：無 → `{"ok":True,"active":False,"session"}`。detach 時 `_stop_capture_locked` 自動停止。
- `get_background_result(cmd_id, *, from_chunk=0, limit=200)`（:4717）：無 → `CMD_NOT_FOUND`（service 轉 `_bg_fallback_from_arbiter`）；`maybe_finalize()`；`start=max(from_chunk - dropped_chunks, 0)`；回 `{"ok":True,"cmd_id","status","error_code","from_seq","last_seq","from_chunk","next_chunk","dropped_chunks","lost":from_chunk<dropped_chunks,"chunks"}`。
- `_evict_background_locked`（:4694）：數量 > 64 → 依 `created_at` 淘汰已終結者，並從擁有者 `background_cmd_ids` 移除。

---

## 14. 動態 COM 排序（#100）與 Hotplug

- `device_sort_key(by_id, by_path) -> (by_id or "", by_path or "")`（:102）；`_by_path_for` 目前恆 None（`DeviceInfo` 無 by_path 欄位，TODO #100）。
- `prepare_dynamic_rank(by_ids)`（:901，service start 時 `prepare_dynamic_rank(list(watcher.devices))` → `update_devices` → `bootstrap_attach`，`service.py:515-517`）：lock 內；`used` = 既有 session COM ∪ pending；pool 排除空字串、released、explicit/bound（`_is_explicit_or_bound`：在 binding override 值中，或某 `yaml-target` session 的 by_id）、已有 session 的 by_id；依 sort key 排序；已有 pending 者略過；從 `idx=0` 找下個未用 `COM{idx}` 指派並加入 used。
- `_next_dynamic_com()`（:864）：最小未被 session 或 pending 佔用的 `COM{i}`（runtime 熱插 fallback；不以 max_sessions 限制）。
- `update_devices(devices)`（:2254）：替換 `_devices`；prune 不在線的 `_pending_com`；`_mark_missing_devices_locked`；`changed`（real_path 改）/`removed`/`added`；這些 by_id 的 session reset reprobe；`removed+changed` → `_detach_by_id`（`DEVICE_REMOVED`/`DEVICE_REBOUND_REQUIRED`）；`added+changed` → `_spawn_attach`。
- `_spawn_attach(by_id)`（:2290）：released 或 inflight → no-op；thread `serialwrap-attach-<by_id>` → `_attach_by_id`；finally 移出 inflight。
- `_attach_by_id`（:2549）：找 by_id session；無 → DETACHED-rebind fallback：`state=="DETACHED"` 且其 by_id 不在線的 session，依 `act_no` 取最小，改綁並寫 binding override；仍無且有 templates：達 `max_sessions` → warning return；否則走 `_attach_by_id_dynamic`。既有 session → `_reresolve_profile_on_reattach`（#181：pin 最高優先；僅 `profile_source=="fallback"` 才重新偵測；quiet 中不偵測；`_rematerialize_profile_locked` 原地換 template、可能改 session_id 並搬移 binding/probe lock/loaded_released、auto alias 跟著改）→ 開 bridge（§3 T2-T8）。
- `_attach_by_id_dynamic`（:2986）：四層優先序 pin → sticky（`_profile_detected`）→ detect（PROBE bridge、`UartProfile()` 預設參數、`detect_template`）→ fallback（`_default_passthrough_template`：優先非 command_capable passthrough）；建 session（§T1）→ 開 bridge；**無 boot quiet gate**；`ensure_ready` 失敗 → `DETACHED`（不保留 consoles）；READY 且 `source=="detected"` → `_maybe_persist_sticky`（real_path 未變才寫）。
- `known_coms()`、`profile_for(com)`、`active_cmd_id_for(com)`（回 `"foreground"` 或 None）供 event engine 使用。

---

## 15. DeferredBuffer（`uart_io.py`）與 suspend/resume

- `_deferred_buffers: dict[client_id, bytearray]`（`uart_io.py:179`）。寫入點（皆**超過 262144 時 `del buf[:len-262144]`＝丟最舊、保留尾端**）：
  1. human owner raw 輸入但 admission blocked（`uart_io.py:789-796`）。
  2. `agent_active` 且 `suspended_owner == human:<cid>`（`uart_io.py:835-846`）。
  3. `send_bytes` raw console context 在 write lock 內發現 admission blocked（`uart_io.py:963-973`）。
- 回放點：`resume_interactive()`（最外層 depth→0，`uart_io.py:1378-1400`，lock 外 `send_bytes(source="human:<cid>")`）；`_set_interactive_admission(True)`（`suspend_depth==0 and suspended_owner is None` 才取出，由 `_finish_foreground_operation` lock 外回放）。
- 清除點：`bridge.stop()`（`uart_io.py:316`，所有 detach/release/rebind/bridge-down/clear）；`detach_console` / reap 該 client（`uart_io.py:523, 568, 1277`）。
- `suspend_interactive()`：可重入（`_suspend_depth`）；最外層保存 `_suspended_owner`、owner=None、`_agent_active=True`。
- 呼叫者：`execute_command`、`file_push`、`file_pull`（human lease 時）、`_self_test_impl`（human lease 時）、soft preempt / recovery lease open；resume：上述 finally、`_PostCloseAction.execute`。

---

## 16. Spec 對照：差異與缺口

### 16.1 BC-02（DeferredBuffer 溢位／跨上下文整段清空）— **未實作**
- 溢位：三個寫入點皆「截頭留尾」（`uart_io.py:794-795, 844-845, 971-972`），`constants.py:203` docstring 明寫「超過則丟最舊位元組」。Spec §5.4 要求整段清空。
- 跨上下文：僅 `bridge.stop()` 會清（因而 Release／Recovery-by-detach／bridge down 間接符合）。以下**不清**：boot banner／quiet arm（`arm_boot_quiet` :450 不碰 bridge）、reboot 命令進 RECOVERING（`_handle_reboot_command` 不 stop bridge，若當時有 suspend，resume 會回放舊輸入）、`enter_flashing`（`set_flash_mode` 只翻旗標；flash 期間 resume 的回放被 flash gate 靜默丟棄、無診斷）、soft preempt close 時的 `_close_interactive_locked` 回放不檢查上下文。
- 無 owner/connection 上下文標記：deferred payload 只以 `client_id` 為 key，沒有 `owner_gen`／`conn_epoch`，無法拒絕「mailbox 中排隊的舊按鍵」。

### 16.2 BC-03（`COMMAND_ABORTED_IN_FLIGHT`）— **未實作**
- 全 repo 無此字串（grep 確認）。在途命令被 release/clear/bridge-down 中止時，結果為 `SEND_FAILED`（例外）、`PROMPT_TIMEOUT`、或 `PROMPT_TIMEOUT_RECOVERED` 等，與「已確認寫出 ≥1 byte」無關聯。
- TX 完成結果三分法（未提交／已確認 N bytes／不明）不存在：`_write_all`（`uart_io.py:988`）在 `n <= 0` 時 `break` 靜默截斷，之後 `send_bytes` 仍以**完整 payload** 寫 TX WAL（`uart_io.py:985-986`）＝可能虛構完成長度；寫入中例外則完全不記 WAL。
- Cancel 只改記錄（§4.7），running 命令的 bytes 照送；沒有 retryable 標記。

### 16.3 Drain Barrier（§5.2、ADR-06）— **未實作**
- 沒有 `conn_epoch`／`fsm_gen`／`owner_gen`；最接近者為 `bridge_generation`（attach 成功 +1）與 `_ForegroundOperation` token、`bridge is` 物件身分比對。
- 沒有 `WorkerStoppedReceipt`、`drain_stalled`、`drain_timeout_epoch`、3000ms timeout（grep 確認無）。`bridge.stop()` 以 thread join(2.0s)×2 取代，逾時直接繼續、同步在 manager lock 內執行（阻塞所有 RPC 最多 ~4s+1s arbiter join）；release 在未確認 handle 關閉前即回成功、寫入 RELEASED。
- 三個檢查位置：Admission（service + arbiter）與出列（execute_command 重驗 state/gate/recovering/lease）存在；「開始 OS 寫入前」只有 flash gate 與 raw-console admission gate（`send_bytes` 內），**沒有 epoch/state/quiet 重驗**——例如 `_execute_command_inner` 在 lock 外 `send_command`，期間 quiet arm 或 release 都不會阻止寫出（release 後 serial=None 會 raise）。
- 單寫入者缺口：`_self_test_impl` nonce probe、`recover_session` READY 路徑 CTRL_C/CTRL_D、READY 再確認 reprobe（`_reprobe_target_still_valid_locked` 不檢查 `foreground_busy`）、`interactive_open` 的 command 送出（ATTACHED 分支不檢查 foreground busy）都不取 foreground token，可與 arbiter 命令交錯 TX。
- 停止操作去重：release 有 `already_released`；recover／force recover 無去重（`_spawn_attach` 的 inflight 只防同 by_id 雙 attach thread）。

### 16.4 §5.3 狀態表逐項差異
| Spec 列 | 實際 Python |
|---|---|
| `RECOVERING`＋CmdSubmit／InteractiveOpen → `SESSION_RECOVERING` | submit-time 回 `SESSION_NOT_READY`（`service.py:624-640`）；execute-time state≠READY 先回 `SESSION_NOT_READY`；`SESSION_RECOVERING` 只在 READY 且 `recovering=True`（實務不會同時成立）或 `_attach_state_result` 出現。InteractiveOpen 回 `SESSION_NOT_READY`。 |
| `FLASHING`＋CmdSubmit／InteractiveOpen → `FLASHING_BUSY` | CmdSubmit submit-time 回 `SESSION_NOT_READY`，只有已排隊命令在 execute-time 得 `FLASHING_BUSY`；InteractiveOpen 回 `SESSION_NOT_READY`；InteractiveSend 回 `FLASHING_BUSY`；DeviceRelease 回 `FLASHING_BUSY`（符合）。 |
| passthrough `ATTACHED`＋CmdSubmit → 進 Arbiter | **不存在**：非 command_capable → `PROFILE_NOT_COMMAND_CAPABLE`；command_capable passthrough 在 ATTACHED → `SESSION_NOT_READY`；arbiter queue 只在 READY 時註冊。 |
| 活躍態＋斷線／I/O error／recover → `RECOVERING` | bridge down → DETACHED → `ATTACHING`（僅 `pending_auto_login` 時 `RECOVERING`）；裝置移除 → `DETACHED`；手動 recover 不進 RECOVERING（READY 留 READY、ATTACHED reprobe、force → clear → ATTACHING）。RECOVERING 實際只由 agent reboot 命令與 reboot 後 bridge down 產生。 |
| `FlashSyncMatched` → 清 DeferredBuffer | 不清（§16.1）。 |
| `FLASHING` 進入 guard | `enter_flashing` 無 state guard（可從 DETACHED/RELEASED 進入；`exit_flashing` 會還原為 flash_prev_state）。 |
| InteractiveExpired 事件核對 deadline | 無事件／timer，到期為存取時被動判定；`interactive_status` 會 touch 延長；human lease 不以 timeout 到期（只看 peer）。 |
| 救援 InteractiveOpen 條件 | 除 bootloader prompt／banner（`boot_interrupt`）外，另有 #174 `login_required` 分支（spec 未列）；bootloader 比對**不用** `UBOOT_FALLBACK_PROMPTS`（與 self-test／reprobe 不一致）。 |
| Quiet／Reconfirm＋一般命令 → `AUTOBOOT_QUIET` 與 `retry_after_s` | submit-time 有 `retry_after_s`（quiet：剩餘秒數；pending-only：5.0）；execute-time（已排隊命令）與 manager 直呼 file_push/pull 的 gate 回應**沒有** `retry_after_s`。 |
| Ready Reconfirm 耗盡（300s／5 次） | 符合（`READY_RECONFIRM_MAX_S=300.0`、`MAX_ATTEMPTS=5`）；另有第三條路徑：`reprobe_exhausted` 或 bootloader 證據 → `force=True` 直接落終態（spec 未列）。終態只在 gate／prepare 時被動評估，`session list` 不評估。 |
| READY 確認 probe 失敗 | spec 隱含維持 READY；Python `_probe_existing_bridge` 失敗會把 READY 改為 `ATTACHED`（T11）且不呼叫 `on_detached`（arbiter queue 仍在、submit-time 改回 SESSION_NOT_READY）。 |
| `READY`＋InteractiveOpen 衝突 | 另有 foreground busy → `SESSION_BUSY`（spec 未列）。 |
| RELEASED＋DeviceAttach | 另有 `DEVICE_STILL_HELD`（非 force）與 `DEVICE_NOT_PRESENT`；成功回應時 attach 尚未完成（非同步 spawn）。 |
| Drain timeout／`FORCE_RECOVER_TIMEOUT` | `FORCE_RECOVER_TIMEOUT` 在 `_force_recover` 10×1s 未 READY 時回（符合「既有時限」≈10s）；無 drain timeout。 |

### 16.5 §9 錯誤碼／last_error 命名空間差異
- Spec 78 碼清單**缺**以下實際會從本區 RPC 回出的 `error_code`：`INVALID_COMPLETION_REGEX`（#230，arbiter/service/session_manager）、`SEND_FAILED`、`COMMAND_FAILED`（arbiter 命令記錄的 `error_code`，可經 `command.get` 讀到）、`NOT_READY`（`_attach_state_result` fallback）、`PROMPT_TIMEOUT_RECOVERED`／`PROMPT_TIMEOUT_FORCE_RECOVERED`（在清單中）以外，警告碼 `CMD_LENGTH_WARNING`（`warning.code`）。
- `last_error` 值**洩漏為 RPC `error_code`**：`attach_session`（`_attach_state_result` 回 `snap.last_error`）與 `recover_session` REPROBE 未恢復時（`payload["error_code"] = last_error`）。因此 `CREDENTIALS_UNRESOLVED`、`PROMPT_UNAVAILABLE`、`LOGIN_REQUIRED`、`TRANSPORT_STALL`、`RX_FLOOD`、`BOOTLOADER_STUCK`、`ATTACH_FAILED:<Exc>`、`REPROBE_FAILED:<Exc>`、`*_PROMPT_TIMEOUT`、`CONSOLE_UNLOCK_*` 等都可能出現在 RPC `error_code`。這與 spec §9.3「`CREDENTIALS_UNRESOLVED` 不新增為 RPC error_code」衝突，需在契約凍結時決定（現況即「既有行為」）。
- Spec §9.3 稱 `CREDENTIALS_UNRESOLVED`「由 `login_fsm` 回傳」：實際主要由 `session_manager` 的 gate（`_credentials_declared_but_unresolved`）在呼叫 login FSM 前合成；`login_fsm` 只在 console_unlock secret 為空時回傳。基本 SessionState 實測為 `ATTACHED`（bridge 開著）。
- `last_error` 完整值集合（本區產生）：`None`、`CLEARED`、`RELEASED`、`REBOUND`、`DEVICE_NOT_FOUND`、`DEVICE_REMOVED`、`DEVICE_REBOUND_REQUIRED`、`DEVICE_REMOVED_DURING_ATTACH`、`BRIDGE_DOWN:<reason>`（`SERIAL_READ:<errno|Exc>`、`SELECT:<errno|Exc>`；隨即被覆寫為 None 若裝置在線）、`ATTACH_FAILED:<Exc>`、`REPROBE_FAILED:<Exc>`、`PROMPT_UNAVAILABLE`、`PROMPT_TIMEOUT`、`REBOOTING`、`RECOVERY_ERROR`、`RECOVERY_TIMEOUT`、`BOOTLOADER_STUCK`、`CREDENTIALS_UNRESOLVED`、`TRANSPORT_STALL`、`RX_FLOOD`、`LOGIN_REQUIRED`、`USER_ENV_MISSING`、`PASS_ENV_REQUIRED`、`PASS_ENV_MISSING`、`LOGIN_USER_REQUIRED`、`LOGIN_PROMPT_TIMEOUT`、`BCM_PROMPT_TIMEOUT`、`SHELL_PROMPT_TIMEOUT`、`PRPL_PROMPT_TIMEOUT`、`POST_LOGIN_CMD_TIMEOUT`、`READY_NONCE_TIMEOUT`、`READY_PROMPT_TIMEOUT`、`CONSOLE_UNLOCK_IDLE_TIMEOUT`、`CONSOLE_UNLOCK_BOOTLOADER`、`CONSOLE_UNLOCK_PROMPT_TIMEOUT`。名義 READY 時恆為 None（不變量）。

### 16.6 其他事實差異
- CLAUDE.md／spec 描述 passthrough「不做任何 login/ready gating、停在 ATTACHED」：實際由 `ready_probe` 決定；有 ready_probe 的 passthrough（`uboot-template`）會 probe 並可進 READY。
- `CMD_PENDING_MAX` 計數不含 `interactive` 狀態（有 done_at）；`health.limits` 不揭露 `CMD_PENDING_MAX`。
- `command.submit` 的 `priority=0` 被 `or 10` 吞掉；`source` 可由 client 偽造 `human:` 以繞過 quiet gate。
- `ProfileTemplate`/`SessionProfile` 預設 `login_regex`/`password_regex` 為 `r"(?mi)^login:\\s*$"`（raw string 內雙反斜線），與 YAML 版不同，可能是預設值 bug（盤點者觀察，未實測）。
- READY nonce 驗證可被命令回顯滿足（§8.3），self-test nonce（`wait_for_regex_from(nonce, offset)`）亦同。
- `_mark_missing_devices_locked`（T22）對無 bridge 且裝置離線的 session 無條件設 `DETACHED`，**未排除 RELEASED**；但 `_save_state` 只序列化 RELEASED，因此 RELEASED session 在其裝置離線的 tick 後會變 DETACHED，下次 `_save_state` 將從 `released` map 移除，`_released_by_ids` 仍保留 by_id（spawn_attach 仍被擋）。此為盤點觀察（`session_manager.py:2240-2252`），建議 Stage 0 以測試確認是否為既有 bug。
- `_wait_for_human_interactive_release`（:3363）、`com_for_by_id`（:938，sw_core 內無呼叫者）為未用程式碼。
