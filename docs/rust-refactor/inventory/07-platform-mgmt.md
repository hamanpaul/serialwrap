# 07 — 平台與管理面盤點（platform & management）

> 盤點基準：`~/prj_pri/serialwrap-worktrees/rust-refactor` @ `93553e6`（VERSION=`0.3.2`），唯讀。
> 範圍：`sw_core/device_source.py`、`device_watcher.py`、`lock_posix.py`、`lock_win.py`、`daemon_lock.py`、`multi_open.py`、`sysenv.py`、`service_ctl.py`、`systemd_units.py`、`setup_cmd.py`、`doctor_cmd.py`、`remote_tunnel.py`、`bench_registry.py`（connect 依賴）、`runtime_config.py`、`state_migrate.py`、`platform_backends.py`、`version.py`、`assets/`、`install.sh`、`pyproject.toml`、`serialwrap.spec`，以及 `cli.py`／`daemon.py`／`service.py`／`constants.py` 與本區相關的部分。
> 行號格式 `file:line`。「推論」標記代表由程式碼閱讀推得、未實跑驗證。

---

## 0. 相關常數與路徑解析（`sw_core/constants.py`）

所有 `_env_path(name, default)`（constants.py:7）：env 未設或 `strip()` 為空 → default；結果一律 `os.path.expanduser`。**import 時凍結**（多數 consumer 讀模組常數；setup/remote 例外，見下）。

| 常數 | 定義 | 值 |
|---|---|---|
| `_CONFIG_HOME` | constants.py:21 | `$XDG_CONFIG_HOME` 或 `~/.config`（注意 `_xdg` 用 `or`，空字串視為未設） |
| `_STATE_HOME` | :22 | `$XDG_STATE_HOME` 或 `~/.local/state` |
| `_DATA_HOME` | :23 | `$XDG_DATA_HOME` 或 `~/.local/share` |
| `_RUNTIME_HOME` | :24 | `$XDG_RUNTIME_DIR`（可能 None） |
| `CONFIG_DIR` | :26 | `SERIALWRAP_CONFIG_DIR` > `_CONFIG_HOME/serialwrap` |
| `STATE_DIR` | :27 | `SERIALWRAP_STATE_DIR` > `_STATE_HOME/serialwrap` |
| `RUN_DIR` | :28-38 | `SERIALWRAP_RUN_DIR` > （若 `SERIALWRAP_STATE_DIR` 非空 → `STATE_DIR`）> （`XDG_RUNTIME_DIR` 存在 → `$XDG_RUNTIME_DIR/serialwrap`）> `STATE_DIR/run` |
| `DATA_DIR` | :39 | `SERIALWRAP_DATA_DIR` > `_DATA_HOME/serialwrap` |
| `LOCK_PATH` | :41 | `RUN_DIR/serialwrapd.lock` |
| `SOCKET_PATH` | :42 | `RUN_DIR/serialwrapd.sock` |
| `DEFAULT_TCP_PORT` | :47 | `int($SERIALWRAP_TCP_PORT or "48700")` |
| `LOOPBACK_TCP_HOSTS` | :49 | `frozenset({"127.0.0.1","localhost","::1"})` |
| `DEFAULT_ENDPOINT` | :50-56 | Windows（`os.name=="nt"` 或 `sys.platform.startswith("win")`）：`$SERIALWRAP_ENDPOINT or f"tcp://127.0.0.1:{DEFAULT_TCP_PORT}"`；其餘：`$SERIALWRAP_ENDPOINT or SOCKET_PATH` |
| `TTYMCU_PATH` | :58 | `SERIALWRAP_TTYMCU_PATH` > `RUN_DIR/dev/ttyMCU` |
| `STATE_PATH` | :59 | `STATE_DIR/state.json` |
| `WAL_DIR` | :60 | `SERIALWRAP_WAL_DIR` > `STATE_DIR/wal` |
| `PROFILE_DIR` | :61 | `SERIALWRAP_PROFILE_DIR` > `CONFIG_DIR/profiles` |
| `DEVICE_BY_ID_DIR` | :62 | `SERIALWRAP_BY_ID_DIR` > `/dev/serial/by-id` |
| `DEVICE_BY_PATH_DIR` | :63 | `SERIALWRAP_BY_PATH_DIR` > `/dev/serial/by-path` |
| `LOG_DIR` | :64 | `SERIALWRAP_LOG_DIR` > `~/b-log` |
| `ensure_runtime_dirs()` | :206-212 | `makedirs(exist_ok)`：CONFIG_DIR、STATE_DIR、RUN_DIR、WAL_DIR、EVENTS_DIR、EVENTS_RUNTIME_DIR |

`config.yaml` 一律位於 `os.path.join(CONFIG_DIR, "config.yaml")`（cli.py:311 `_default_runtime_config`、daemon.py:84、device_source.py:29）。**沒有** `SERIALWRAP_CONFIG` 環境變數、也沒有系統設定目錄 fallback（見 §13 差異 D-01）。

---

## 1. 裝置列舉

### 1.1 `DeviceInfo`（device_watcher.py:12-15）

```python
@dataclasses.dataclass(frozen=True)
class DeviceInfo:
    by_id: str       # POSIX：symlink 完整路徑（by-id 或 by-path 目錄下）；Windows：COM 名，如 "COM3"
    real_path: str   # POSIX：os.path.realpath(symlink)；Windows：r"\\.\COM3"
```
**沒有 `by_path` 欄位**（session_manager.py:880-887 `_by_path_for` 以 `getattr(dev,"by_path",None)` 取，永遠 None；docstring 標 TODO #100）。

### 1.2 `DeviceSource` Protocol（device_source.py:42-47）

`scan(self) -> dict[str, DeviceInfo]`；key = 裝置路徑（POSIX symlink 路徑或 Windows COM 名）。

### 1.3 `PosixDeviceSource(scan_dirs: list[str])`（device_source.py:50-77）

`scan()` 演算法（逐字）：
1. `out = {}`、`seen_real = set()`。
2. 依 `scan_dirs` **給定順序**逐一處理；`os.path.isdir(dir)` 為假則略過。
3. 對 `sorted(os.listdir(dir))`（字典序，Python str 排序 = code point 序）中每個 `name`：
   - `path = os.path.join(dir, name)`；`os.path.exists(path)` 為假（含 dangling symlink）→ 略過。
   - `real_path = os.path.realpath(path)`；若已在 `seen_real` → 略過（**先掃到者勝**，即 by-id 優先於 by-path）。
   - `out[path] = DeviceInfo(by_id=path, real_path=real_path)`。
4. 回傳 `out`（dict 插入序 = 目錄順序 × 檔名排序）。

不跟隨子目錄、不過濾檔案型別（一般檔也收，只要存在）。

### 1.4 `DeviceWatcher`（device_watcher.py:18-90）

建構子：
```python
DeviceWatcher(by_id_dir: str,
              on_change: Callable[[list[DeviceInfo], list[DeviceInfo]], None],
              poll_interval_s: float = 1.0,
              extra_scan_dirs: list[str] | None = None,
              on_tick: Callable[[], None] | None = None,
              source: DeviceSource | None = None)
```
- `self._scan_dirs = [by_id_dir] + (extra_scan_dirs or [])`；`source` 為 None 時建 `PosixDeviceSource(self._scan_dirs)`（延遲 import 避免循環）。
- `devices` property → `dict(self._devices)` 副本。
- `poll_once()`（:52-68）：`current = source.scan()`；`added_keys = sorted(cur - prev)`、`removed_keys = sorted(prev - cur)`、`changed_keys = sorted(k ∈ cur∩prev if cur[k].real_path != prev[k].real_path)`；`added=[cur[k]...]`、`removed=[prev[k]...]`；changed 的 key 依序 **append prev 至 removed、append cur 至 added**（排在 added/removed 之後）；先 `self._devices = current` 再 `if added or removed: on_change(added, removed)`。
- `_loop()`（:70-79）：`while not stop_event.is_set(): try: poll_once(); on_tick() if set; except Exception: logger("serialwrap").warning("device watcher tick 失敗，略過本輪", exc_info=True)`；之後 `stop_event.wait(poll_interval_s)`。單輪例外不殺 thread（#79 STA-2）。
- `start()`：thread 已活則 no-op；`threading.Thread(target=_loop, name="serialwrap-device-watcher", daemon=True)`。
- `stop()`：set event；`join(timeout=2.0)`。
- 無任何鎖：`poll_once` 若由多 thread 同時呼叫無保護（見 §13 O-03）。

daemon 組裝（service.py:259-272）：`DeviceWatcher(by_id_dir=DEVICE_BY_ID_DIR, self._on_device_change, extra_scan_dirs=[DEVICE_BY_PATH_DIR], on_tick=self._sessions.reconcile_readiness, source=_source)`；`_source = WindowsDeviceSource(exclude_coms=_load_exclude_coms())` 當 `select_device_backend()=="win"`，否則 None（→ PosixDeviceSource([by_id, by_path])）。poll 間隔預設 1.0s。`_on_device_change` 忽略參數，呼叫 `self._sessions.update_devices(self._watcher.devices)`（service.py:358-359）。

`SerialwrapService.start()`（service.py:~505-518）順序：`_engine.start()` → add rx observer → `_watcher.start()` → `_watcher.poll_once()` → `_sessions.prepare_dynamic_rank(list(_watcher.devices.keys()))` → `_sessions.update_devices(_watcher.devices)` → `_sessions.bootstrap_attach()` → `_flash_endpoint.start()`。

### 1.5 COM 排序鍵（#100）

- `device_sort_key(by_id: str, by_path: str | None) -> tuple[str, str]`（session_manager.py:102-112）：`return (by_id or "", by_path or "")`。tuple 字典序比較。
- `prepare_dynamic_rank(by_ids)`（session_manager.py:901-935）：持 `self._lock`；`used = {既有 session 的 profile.com} ∪ set(_pending_com.values())`；候選 = `by_ids` 中非空、不在 `_released_by_ids`、`not _is_explicit_or_bound(b)`（binding override 值或 `profile_source=="yaml-target"`）、且無既有 session 的 `device_by_id==b`；依 `device_sort_key(b, _by_path_for(b))` 排序；`idx=0`，對每個 b（已在 `_pending_com` 者跳過）：`while f"COM{idx}" in used: idx+=1`，指派 `COM{idx}`、加入 used、`idx+=1`。
- `_session_from_template` 消費：`com = _pending_com.pop(by_id, None) or _next_dynamic_com()`。
- 注意 `by_ids` 為 watcher key（**完整 symlink 路徑**），排序以完整路徑字串為準，故 `/dev/serial/by-id/...` 全部排在 `/dev/serial/by-path/...` 之前（'i' < 'p'）。

### 1.6 Windows：`WindowsDeviceSource(exclude_coms: set[str] | None = None)`（device_source.py:153-185）

- `_BT_TTL: float = 5.0`（class 屬性）；`_exclude_coms = exclude_coms or set()`；`_bt_cache: tuple[float, set[str]] | None = None`。
- `scan()`：`serialcomm = _read_serialcomm()`；`now = time.monotonic()`；若 cache None 或 `now - cache[0] >= 5.0` → `cache = (now, _read_bt_ports())`；`kept = exclude_bluetooth(serialcomm, bt_ports, _exclude_coms)`；回 `{com: DeviceInfo(by_id=com, real_path=rf"\\.\{com}")}`。無鎖（單一 poll thread）。
- **注意**：回傳 dict 順序 = SERIALCOMM 列舉順序，不排序（排序由 `prepare_dynamic_rank` 以 COM 名字串字典序做，`"COM10" < "COM3"`）。

`_read_serialcomm() -> dict[str,str]`（:80-104）：`winreg.OpenKey(HKEY_LOCAL_MACHINE, r"HARDWARE\DEVICEMAP\SERIALCOMM")`；`FileNotFoundError` → `{}`；`EnumValue(key, i)` 迭代至 `OSError`；`out[name] = value`（**name = device path value-name，如 `\Device\BthModem0`；value = COM 名**）；finally `CloseKey`。

`_read_bt_ports() -> set[str]`（:107-150）：遞迴 `walk(HKLM, r"SYSTEM\CurrentControlSet\Enum\BTHENUM")`：`OpenKey` 失敗 → return；嘗試 `OpenKey(path + r"\Device Parameters")` → `QueryValueEx(pp, "PortName")`，值為 `str` 才加入；所有 OSError 吞掉；`EnumKey(key, i)` 遞迴所有子鍵（深度不限）。

`exclude_bluetooth(serialcomm, bt_ports, exclude) -> dict[str,str]`（:188-216，純函式）：對每個 `(device_path, com)`：`com in bt_ports or com in exclude` → 剔除；`"bthmodem" in device_path.lower()` → 剔除；其餘 `kept[com] = com`。

`_load_exclude_coms() -> set[str]`（:20-39）：延遲 import yaml；`CONFIG_DIR/config.yaml` 不存在 → `set()`；`data = yaml.safe_load(...) or {}`；`coms = data.get("windows", {}).get("exclude_coms", [])`；list → `{str(c) for c in coms}`；任何例外（含 `data` 非 dict、`windows` 非 dict）→ `set()`。**只在 daemon 啟動時讀一次**（service.py:264），不熱載。

### 1.7 `SERIALWRAP_BY_ID_DIR` sandbox

- 只影響 `DEVICE_BY_ID_DIR`。`DeviceWatcher` 仍掃 `DEVICE_BY_PATH_DIR`（預設 `/dev/serial/by-path`）。**只設 BY_ID_DIR 不足以隔離**：主機其他裝置仍會經 by-path 被動態偵測（regression/serialwrap_regression/guards.py:105-131 註解「round 4 實測」已確認，故該 guard 一併設 `SERIALWRAP_BY_PATH_DIR`=空目錄；tests/conftest.py 亦同時覆寫兩者）。見 §13 D-02。
- Windows 無對應機制（spec OD-11 待建）。

### 1.8 平台後端選擇器（platform_backends.py）

`_select(env_name, backend)`：`mode = (backend or os.environ.get(env_name) or "auto").lower()`；`posix|unix|termios` → `"posix"`；`win|windows|win32` → `"win"`；`auto` → Windows 判準（`os.name=="nt"` 或 `sys.platform.startswith("win")`）→ `"win"` 否則 `"posix"`；其他 → `ValueError(f"unsupported backend: {mode!r}")`。
- `select_rpc_backend()` ← `SERIALWRAP_RPC_BACKEND`；`select_lock_backend()` ← `SERIALWRAP_LOCK_BACKEND`；`select_device_backend()` ← `SERIALWRAP_DEVICE_BACKEND`。
- CLI 包裝 `_rpc_backend_is_win()`（cli.py:660-670）：ValueError 時退回平台判斷（不讓例外穿出）。daemon 端嚴格（ValueError 會穿出 → traceback）。

---

## 2. Singleton lock

### 2.1 POSIX `SingletonLock(lock_path, socket_path)`（lock_posix.py:9-56）

`acquire()`：
1. `os.makedirs(dirname(lock_path), exist_ok=True)`（lock_path 無目錄部分時 `dirname==""` → makedirs("") 會 FileNotFoundError；推論）。
2. `fd = os.open(lock_path, O_RDWR|O_CREAT, 0o600)`；`fcntl.flock(fd, LOCK_EX|LOCK_NB)`；`BlockingIOError` → close fd，`raise RuntimeError("DAEMON_ALREADY_RUNNING")`。
3. 若 `os.path.exists(socket_path)`（tcp:// 字串在 POSIX 恆不存在 → 略過探測）：AF_UNIX `settimeout(0.2)` `connect`：
   - 連上 → close fd，`RuntimeError("DAEMON_ALREADY_RUNNING")`（**持有 flock 仍拒絕**：同 socket、不同 lock 路徑的第二 daemon）。
   - `OSError.errno ∈ {ENOENT, ECONNREFUSED}` → stale，`os.unlink(socket_path)`（失敗吞掉）。
   - 其他 OSError（含 timeout，errno=None；EACCES）→ close fd，`RuntimeError("SOCKET_UNAVAILABLE")`。
   - finally `sock.close()`。
4. `self._fd = fd`；`ftruncate(fd,0)`；`write(f"{os.getpid()}\n")`（ASCII）；`fsync`。

`release()`：`_fd` None → no-op；`flock(LOCK_UN)`，finally `close`、`_fd=None`。lock 檔不刪除。

### 2.2 Windows `WindowsSingletonLock(lock_path, endpoint)`（lock_win.py:20-68）

- `_endpoint_alive(endpoint)`（:8-17）：`urlsplit`；`scheme!="tcp"` 或無 hostname 或無 port → False；`socket.create_connection((host,port), timeout=0.2)` 成功 → True；OSError → False。
- `acquire()`：**先**探 endpoint，活 → `RuntimeError("DAEMON_ALREADY_RUNNING")`；延遲 `import msvcrt`；`parent=dirname(lock_path)` 非空才 makedirs；`os.open(O_RDWR|O_CREAT, 0o600)`；`msvcrt.locking(fd, LK_NBLCK, 1)`（從目前檔位置 0 鎖 1 byte）；OSError → close、`DAEMON_ALREADY_RUNNING`；之後 ftruncate/寫 PID/fsync（同 POSIX）。
- `release()`：`lseek(fd,0,SEEK_SET)` 後 `msvcrt.locking(fd, LK_UNLCK, 1)`，finally close。
- 無 `SOCKET_UNAVAILABLE` 分支。

### 2.3 `daemon_lock.py`
僅 `from sw_core.lock_posix import SingletonLock`；`__all__ = ["SingletonLock"]`（相容 re-export）。

### 2.4 daemon 使用（daemon.py）
- `build_parser()`（:38-44）：`prog="serialwrapd"`；`--profile-dir` 預設 `PROFILE_DIR`；`--socket` 預設 `DEFAULT_ENDPOINT`；`--lock` 預設 `LOCK_PATH`。
- `_build_lock(args)`（:47-57）：`select_lock_backend()=="win"` → `WindowsSingletonLock(args.lock, args.socket)`，否則 `SingletonLock(args.lock, args.socket)`。
- `_run_async`（:128-198）：`ensure_runtime_dirs()` → `load_profiles(args.profile_dir)`（空 → stderr `serialwrapd: no profiles loaded\n`，繼續）→ Windows 裝置後端注入 `windows-default-passthrough` template（:103-125；`ProfileTemplate(profile_name="windows-default-passthrough", platform="passthrough", ready_probe="")`，已有 passthrough template 則不注入）→ `lock.acquire()`，`RuntimeError` → stderr `serialwrapd: {exc}\n`，**exit 2** → `SerialwrapService(...)`，`StateLoadError` → stderr、`lock.release()`、**exit 3** → `server.start()` 成功後 `_write_effective_endpoint(args)`（全平台寫 `config.yaml` 的 `socket_path = args.socket`，失敗僅 stderr `serialwrapd: 寫入 config.yaml endpoint 失敗（非致命）: {exc}\n`）→ 等 stop_event（SIGINT/SIGTERM 或 RPC `daemon.stop` 回 `{"ok":True,"stopping":True}`）→ finally `server.stop()`、`service.stop()`、`lock.release()` → exit 0。
- `main()` 先 `force_utf8_stdio()`。

---

## 3. 同機多開偵測（`multi_open.py`，#101/#173）

### 3.1 函式
- `_iter_pids(proc_root)`：`os.listdir(proc_root)` 中 `name.isdigit()` 者 yield `int`。順序 = listdir 順序。
- `_is_serialwrapd(proc_root, pid) -> bool`（:27-62）：讀 `{proc_root}/{pid}/cmdline`（OSError → False）；`parts = [p.decode("utf-8","surrogateescape") for p in raw.split(b"\0") if p]`；空 → False。判準（任一成立）：
  1. `os.path.basename(parts[0]) == "serialwrapd"`；
  2. `any(p.endswith("serialwrapd.py") for p in parts)`（含 argv0，不要求路徑）；
  3. `parts[1:]` 中某 p 同時 `"/" in p` 且 `basename(p) == "serialwrapd"`。
  不做 TGID 去重（/proc 頂層只列 TGID）。
- `_extract_socket_arg(proc_root, pid) -> str | None`（:65-83）：同法讀 cmdline；依序找 `part == "--socket" and i+1 < len` → `parts[i+1]`；或 `part.startswith("--socket=")` → 後綴；皆無 → None。
- `detect_multi_open(proc_root="/proc", tty_paths=None) -> dict`（:86-144）。

### 3.2 `detect_multi_open` 輸出契約
```json
{"multi_open": bool, "daemons": [{"pid": int, "socket": str|null}], "holders": {"<tty_path>": int}, "holders_status": "ok"|"permission"|"unknown"}
```
- `_iter_pids` 拋 OSError → 立即回 `{"multi_open": False, "daemons": [], "holders": {}, "holders_status": "unknown"}`。
- `multi_open = len(daemons) > 1`。
- `daemons` 依 `pid` 升冪排序輸出（掃描時為 listdir 順序）。
- `holders`：只掃 **serialwrapd 行程**的 `/proc/<pid>/fd/*`；`os.readlink` 結果（字串完全相等）在 `set(tty_paths)` 內 → `holders[target] = pid`（多 daemon 同持時後掃者覆寫）。**不排除自身 pid**（推論：`daemon status` 的 `foreign_holders` 會含 daemon 自己持有的 tty，見 §13 O-04）。
- `holders_status`：初值 `"ok"`；listdir fd 目錄 `PermissionError` → `"permission"`（覆寫任何值）；其他 OSError 且目前為 `"ok"` → `"unknown"`。單一 fd readlink 失敗靜默略過。

### 3.3 Consumers
- `service.health()`（service.py:604-615）：`tty_paths = [s["attached_real_path"] for s in sessions if s.get("attached_real_path")]`；輸出 `result["multi_open"] = mo["multi_open"]`、`result["foreign_holders"] = mo["holders"]`、`result["multi_open_detail"] = {"daemons": mo["daemons"], "holders_status": mo["holders_status"]}`。`health.status` 在 daemon 的 `BLOCKING_RPC_METHODS`（daemon.py:34）。
- `cli._find_conflicting_daemon(sock, proc_root="/proc")`（cli.py:403-422）：回第一個 `d.get("socket") != sock` 的 daemon（`socket=None` 也視為衝突）；無 → None。**比對為字串相等，不正規化路徑**。
- `doctor._check_single_daemon`、`doctor._check_endpoint_reachable`（§7）。

---

## 4. Effects 邊界（`sysenv.py`）

### 4.1 Protocol `Effects`
`run(cmd: List[str], *, timeout_s: Optional[float]=None) -> Tuple[int,str,str]`、`has_systemd() -> bool`、`user_in_group(group) -> bool`、`is_wsl() -> bool`、`which(name) -> Optional[str]`、`which_all(name) -> List[str]`。

### 4.2 `SystemEffects`
- `run`：`subprocess.run(cmd, capture_output=True, text=True, check=False, timeout=timeout_s)`；`TimeoutExpired` → `(-1, "", "TIMEOUT")`；其餘例外（如 FileNotFoundError 找不到執行檔）**不攔截，會拋出**（推論）。回 `(returncode, stdout, stderr)`。
- `has_systemd`：`os.path.isdir("/run/systemd/system")`；例外 → False。
- `user_in_group(group)`：`sys.platform=="win32"` → False；`grp.getgrnam(group).gr_gid`（KeyError → False）∈ `set(os.getgroups()) | {os.getegid()}`；例外 → False。
- `is_wsl`：`"microsoft" in platform.uname().release.lower()`；例外 → False。
- `which(name)`：`shutil.which(name)`；例外 → None。
- `which_all(name)`：`PATH.split(os.pathsep)`（空 PATH → []）；Windows：`PATHEXT`（預設 `".COM;.EXE;.BAT;.CMD"`）以 `os.pathsep` 切，若 `name` 已有副檔名則 suffix=[""]；POSIX suffix=[""]；跳過空目錄字串；`candidate = join(d, name+suf)`，需 `isfile` 且（非 win）`os.access(X_OK)`；依序去重；任何例外 → `[]`。
- **`SystemEffects` 沒有 `calls` 屬性**（影響 setup 的 `ran` 欄位，§6.6）。

### 4.3 `FakeEffects`（測試替身）
建構參數 keyword-only：`systemd=False, in_groups=frozenset(), wsl=False, which=None, which_all=None, commands=None`；`self.calls: List[List[str]]`、`self.timeouts: List[Optional[float]]`；`run` 記錄後回 `commands.get(tuple(cmd), (0,"",""))`。

### 4.4 `force_utf8_stdio()`
非 win32 → no-op；對 `sys.stdout`、`sys.stderr`：有 callable `reconfigure` → `reconfigure(encoding="utf-8")`，任何例外吞掉（#118）。CLI `main()` 與 daemon `main()` 在 parse_args 前呼叫。

---

## 5. 監管模式、service 子命令、systemd unit

### 5.1 `RuntimeConfig(path)`（runtime_config.py）
- 建構：檔案存在 → `yaml.safe_load(read_text(utf-8))`，非 dict → `{}`；**YAML 解析錯誤會拋出**（呼叫端用 `_safe_runtime_config` 才吞）。
- `mode() -> str|None` = `_data.get("supervision_mode")`；`socket_path() -> str|None` = `_data.get("socket_path")`（**值未驗型別**）。
- `set_mode(mode, *, socket_path=None)`：設 `supervision_mode`；`socket_path is not None` 才覆寫；`mkdir(parents)`；`write_text(yaml.safe_dump(_data, allow_unicode=True))`（非原子寫入、保留其他鍵如 `windows:`）。
- `set_socket(endpoint)`：只設 `socket_path`，同法寫回。
- config.yaml 鍵：`supervision_mode`（`on-demand`|`systemd-user`|`systemd-system`）、`socket_path`、`windows.exclude_coms`（list）。`safe_dump` 預設 `sort_keys=True`。

CLI 讀取：`_default_runtime_config()`（cli.py:311，**不吞例外**）、`_safe_runtime_config()`（:321，例外 → None）、`should_auto_spawn(rc)`（:315：`(rc.mode() or "on-demand") == "on-demand"`）。

### 5.2 `service_ctl.service_action(action, *, mode, fx=None, with_sudo=False) -> dict`（service_ctl.py:21-61）
常數 `_UNIT = "serialwrap"`、`_PRIVILEGED = {"start","stop","restart"}`。
- `mode == "systemd-user"`：`fx.run(["systemctl","--user",action,"serialwrap"])` → `{"ok": rc==0, "mode", "action", "rc", "stdout", "stderr"}`。
- `mode == "systemd-system"`：`base = ["systemctl", action, "serialwrap"]`；特權 action 且 `not with_sudo` → `{"ok":False,"mode","action","error_code":"NEEDS_SUDO","hint":"需 root：請執行 `sudo systemctl <action> serialwrap`（或加 --with-sudo 讓本指令代跑）"}`（不執行）；否則 `cmd = ["sudo"]+base`（特權+with_sudo）或 `base`（status），執行同上格式。
- 其他（含 `on-demand`、未知）：`{"ok":False,"mode","action","error_code":"NO_SYSTEMD","hint":"目前為 on-demand 監管模式（無 systemd 服務）。使用 `serialwrap daemon start/stop`。"}`。
- `action` 不驗證（parser 限 `start|stop|restart|status`）。

CLI `service`（cli.py:2348-2369 parser；2688-2693 dispatch）：`mode = _default_runtime_config().mode() or "on-demand"`；`_print(result)`；`_mirror_err(result, context=f"service {action}")`；exit `0 if ok else 2`。旗標 `--with-sudo`（dest `with_sudo`）。

CLI `supervision-mode`（2342-2346；2684-2686）：stdout **純文字**一行 `mode or "on-demand"`（非 JSON），exit 0；config 壞 YAML → traceback。

### 5.3 `daemon start`／`daemon stop` 的監管轉送（cli.py）
`daemon start`（:425-621）流程：
1. `is_win = _rpc_backend_is_win()`。`--endpoint` 有值且非（win 且 loopback tcp）→ `{"ok":False,"error_code":"REMOTE_NOT_SUPPORTED","message":...}`（win：`"--endpoint 於 daemon start 僅接受 loopback tcp://（127.0.0.1/localhost/::1）作為本機 bind 位址"`；POSIX：`"--endpoint 不支援 daemon start（daemon 只能在本機啟動）"`），exit 2。
2. `--endpoint` 與 `--socket` 同給且不等 → `INVALID_ARGS`，message `"--endpoint 與 --socket 不一致；daemon start 請擇一指定 bind 位址"`，exit 2。
3. `rc=_safe_runtime_config()`；`rc` 非 None 且 `not should_auto_spawn(rc)` → `resp = service_action("start", mode, with_sudo=args.with_sudo)`；`resp["_routed_to"]="service start"`；print、mirror_err；exit 0/2。
4. on-demand：`_resolve_local_endpoint_info(args)`（忽略 `--bench`）→ `health.ping` 探測（`_probe_healthy_daemon`）健康 → `{"ok":True,"already_running":True,"socket":endpoint}` exit 0。
5. spawn 目標 `sock = args.socket` > `ep_arg` > `_local_default_endpoint()`。
6. POSIX 且無 `--force-spawn`：`_find_conflicting_daemon(sock)` 命中 → stdout `{"ok":False,"error_code":"DAEMON_ALREADY_RUNNING_ELSEWHERE","message":"偵測到另一個 serialwrapd（pid=…）綁在 …，與本次 daemon start 目標 … 不同；為避免同一批裝置被兩個 daemon 同時開啟（two-reader），預設拒絕再 spawn 一個。","hint":…,"existing_socket":<str|null>,"existing_pid":int,"target_socket":sock}`，exit 2（**此出口未呼叫 `_mirror_err`**）。hint：socket 已知 → `"改用 --socket {known} 指向現有 daemon；確認後要強制另起才加 --force-spawn"`；未知 → `"先用 `serialwrap daemon status`（看 multi_open_detail）確認現有 daemon 的 socket；確認後要強制另起才加 --force-spawn"`；existing_socket 顯示用字串 `"未知（無法從 /proc 讀出 --socket，可能是舊版 daemon）"`。
7. argv：win → `_daemon_spawn_argv()`（frozen：`sys.executable` 同層 `serialwrapd.exe` > `which serialwrapd` > None；非 frozen：repo `serialwrapd.py` 存在 → `[sys.executable, script]`，否則 `[sys.executable, "-m", "sw_core.daemon"]`），None → `DAEMON_BINARY_NOT_FOUND`（message `"找不到 serialwrapd（serialwrap.exe 同層或 PATH 上皆無）；請確認發行包完整"`）exit 2；**POSIX 固定 `[sys.executable, _daemon_script_path()]`**，`_daemon_script_path() = normpath(dirname(cli.py)/../serialwrapd.py)`（見 §13 O-02）。
8. `cmd = [*argv, "--profile-dir", args.profile_dir, "--socket", sock, "--lock", args.lock]`；env 檔載入失敗 → `ENV_FILE_SOURCE_FAILED`（含 `env_file`、`env_files`）exit 2。
9. `--foreground` → `subprocess.call(cmd, env)` 回其 rc。
10. 背景：stdout/stderr DEVNULL；Windows（`os.name=="nt"`）`creationflags = DETACHED_PROCESS|CREATE_NEW_PROCESS_GROUP`；POSIX `start_new_session=True`。
11. 輪詢 `attempts = 50 if is_win else 15`，每次 `sleep(0.2)`：子行程已退出 → `{"ok":False,"error_code":"DAEMON_EXITED","pid","returncode"}` exit 2；`health.ping`(0.5s) ok → `health.status`(1.0s)，輸出 `{"ok":True,"pid","socket":sock[, "env_files"][, "warnings"]}` exit 0。逾時 → `{"ok":False,"error_code":"DAEMON_NOT_READY","pid"}` exit 2。

parser（:1959-1989）：`start`：`--profile-dir`（預設 PROFILE_DIR）、`--lock`（預設 LOCK_PATH）、`--foreground`、`--with-sudo`、`--force-spawn`；`stop`：`--with-sudo`；`status`（→ RPC `health.status`）。**無 `restart` 子命令**。

`daemon stop`（:624-657）：`mode = (rc.mode() if rc else None) or "on-demand"`；`explicit_target = bool(--endpoint) or --socket is not None or bool(--bench)`；`mode.startswith("systemd") and not explicit_target` → `service_action("stop", ...)` + `_routed_to:"service stop"`，exit 0/2；否則 RPC `daemon.stop`（timeout 2.0）失敗 exit 2、成功 exit 0；`_EndpointResolutionError` → JSON exit 1；其他例外 → `INTERNAL_ERROR` exit 1。

### 5.4 systemd unit 文字（systemd_units.py）
兩函式皆**無尾端換行**（setup 以 `write_text` 原樣寫入 → 檔案無結尾 `\n`）。

`render_user_unit(exec_start)`（逐字）：
```ini
[Unit]
Description=serialwrap UART broker daemon (user)
After=default.target

[Service]
Type=simple
ExecStart={exec_start}
Restart=on-failure
RestartSec=2

[Install]
WantedBy=default.target
```

`render_system_unit(exec_start, run_user="serialwrap")`（逐字）：
```ini
[Unit]
Description=serialwrap UART broker daemon (system)
After=multi-user.target

[Service]
Type=simple
User={run_user}
SupplementaryGroups=dialout
RuntimeDirectory=serialwrap
StateDirectory=serialwrap
ConfigurationDirectory=serialwrap
UMask=0117
Environment=SERIALWRAP_SOCKET_GROUP=dialout
ExecStart={exec_start}
Restart=on-failure
RestartSec=2

[Install]
WantedBy=multi-user.target
```
刻意不含 `PrivateDevices`／`DeviceAllow`。`SERIALWRAP_SOCKET_GROUP` 被 rpc_posix.py:98-113 用於 `chmod 0o660` 後 `shutil.chown(group=...)`（失敗吞掉）。

setup_cmd 常數（:257-264）：`_USER_UNIT_REL = Path(".config")/"systemd"/"user"/"serialwrap.service"`（相對 `home`，**不看 XDG_CONFIG_HOME**）；`_USER_EXEC_START = "%h/.local/bin/serialwrapd"`（無參數）；`_SYSTEM_UNIT_PATH = "/etc/systemd/system/serialwrap.service"`；`SYSTEM_SOCKET = "/run/serialwrap/serialwrapd.sock"`；`SYSTEM_PROFILE_DIR = "/etc/serialwrap/profiles"`。
- `_resolve_run_user()`：`$SUDO_USER or getpass.getuser()`。
- `_resolve_serialwrapd_path(home)`：`shutil.which("serialwrapd") or str(home/".local/bin/serialwrapd")`。
- `_system_exec_start(home)` = `f"{path} --socket /run/serialwrap/serialwrapd.sock --profile-dir /etc/serialwrap/profiles"`（**不帶 `--lock`**）。

---

## 6. setup（`setup_cmd.py` + `cli._run_setup`）

### 6.1 `_user_dirs(home)`（setup_cmd.py:31-62，**呼叫時**讀 env）
`home_path = Path(home) if home else expanduser("~")`；
- `config` = `$SERIALWRAP_CONFIG_DIR`（truthy）或 `($XDG_CONFIG_HOME or home/.config)/serialwrap`
- `data` = `$SERIALWRAP_DATA_DIR` 或 `($XDG_DATA_HOME or home/.local/share)/serialwrap`
- `agents_skill_link` = `home/.agents/skills/serialwrap`
- `legacy_agents_skill_link` = `home/.agents/skills/serialwrap-mcp`
- `bin` = `home/.local/bin`
（注意：這裡 env 值**不 expanduser、不 strip**，與 constants 的 `_env_path` 略有差異。）

### 6.2 `materialize_assets(home=None, *, force=False) -> dict[str,str]`（:174-235）
1. Profiles：`profiles_dest = config/"profiles"`（mkdir parents）；對 `importlib.resources.files("sw_core.assets")/"profiles"` 的每個**檔案**：`_copy_profile_file`（dest 存在且 not force → 跳過；否則 `shutil.copy2`）。目前資產：`default.yaml`、`brcm.env.example`。
2. Skill（永遠刷新）：`skill_dest = data/"skill"`；`assets.copy_tree("skill", skill_dest)`（遞迴 copy2 / copytree dirs_exist_ok，不刪多餘檔）；`_force_symlink(agents_skill_link, skill_dest)`（link 為 symlink → unlink；真目錄 → rmtree；其他存在 → unlink；再 `symlink_to`）；`_remove_legacy_serialwrap_mcp_link(legacy)`（僅當是 symlink 且 `target.name=="serialwrap-mcp"` 或 `"custom-skills/serialwrap-mcp" in str(target)` 才 unlink）。
3. Wrappers → `bin`（mkdir）：`_wrapper_map = {"minicom_router.sh":"serialwrap-minicom", "minicom-broker.sh":"serialwrap-minicom-broker", "minicom-raw.sh":"serialwrap-minicom-raw"}`；copy2 後 `chmod 0o755`（永遠覆寫）。
4. 回 `{"profiles": str(profiles_dest), "skill_link": str(agents_skill_link), "bin": str(bin)}`（CLI **未輸出**此回傳值）。

### 6.3 `detect_legacy_install(home=None) -> dict|None`（:115-171）
- 判準：`home/.paul_tools/serialwrap` 存在；否則 None。
- `minicom_symlink = any(p.is_symlink() for p in [home/.paul_tools/minicom, home/.local/bin/minicom])`。
- `state_json = "/tmp/serialwrap/state.json"` 若存在，否則 None。
- 回 `{"path": str(~/.paul_tools), "serialwrap": True, "minicom_symlink": bool, "state_json": str|None, "hint": "偵測到舊版 ~/.paul_tools 安裝。建議退役步驟：1) 停掉舊 daemon（若仍在跑）；2) 移除 shadow 二進位 ~/.paul_tools/serialwrap 與 minicom 符號連結；3) 從 shell rc 移除 ~/.paul_tools 的 PATH export。新版改以 pipx 安裝（~/.local/bin）並以 XDG 路徑運作，不需 ~/.paul_tools。本指令不會自動刪除任何檔案。"}`。絕不刪除。

### 6.4 `ensure_wsl_systemd(fx, home) -> dict`（:313-348）
- `not fx.is_wsl()` → `{"wsl":False,"already":False,"enabled_now":False,"needs_restart":False}`。
- `fx.has_systemd()` → `{"wsl":True,"already":True,"enabled_now":False,"needs_restart":False}`。
- 否則讀 `/etc/wsl.conf`（OSError → ""）→ `_merge_wsl_conf_systemd`（configparser、`optionxform=str`；解析失敗改用空 parser；確保 `[boot]` 並 `systemd = true`；`cp.write` 輸出格式為 `key = value`）→ 寫 staging `home/.local/share/serialwrap/wsl.conf` → `fx.run(["sudo","install","-m","0644",staging,"/etc/wsl.conf"])` → `{"wsl":True,"already":False,"enabled_now":rc==0,"needs_restart":rc==0,"rc":rc,"hint":"已寫入 /etc/wsl.conf [boot] systemd=true；請在 Windows 端執行 `wsl --shutdown`，重新進入 WSL 後再跑一次 `serialwrap setup --system --with-sudo`。"}`。
- **不檢查 `with_sudo`、不看目標模式**（見 §13 O-06）。

### 6.5 system scope staging
- `_stage_system_unit(home)`：寫 `home/.local/share/serialwrap/serialwrap.service` = `render_system_unit(_system_exec_start(home), run_user=_resolve_run_user())`。
- `_stage_system_profiles(home)`：`assets.copy_tree("profiles", home/.local/share/serialwrap/system-profiles)`。
- `_system_install_cmds(home, *, include_start)`（會先執行上述兩個 staging 寫檔）回：
  1. `["sudo","install","-m","0644",<unit_staging>,"/etc/systemd/system/serialwrap.service"]`
  2. `["sudo","mkdir","-p","/etc/serialwrap/profiles"]`
  3. `["sudo","cp","-r",f"{prof_staging}/.","/etc/serialwrap/profiles"]`
  4. `["sudo","systemctl","daemon-reload"]`
  5. `["sudo","systemctl","enable","serialwrap"]`
  6. （`include_start`）`["sudo","systemctl","start","serialwrap"]`
- staging 路徑固定 `home/.local/share/serialwrap`，不看 `XDG_DATA_HOME`/`SERIALWRAP_DATA_DIR`。

### 6.6 `reconcile(*, old_mode, target_mode, fx, home, daemon_running=False, any_flashing=False, with_sudo=False, force=False, socket_path=None, config_path=None) -> dict`（:500-644）
- 護欄 1：`any_flashing and not force` → `raise FlashingBusy("flash 進行中，拒絕切換監管模式（可用 force 覆寫）")`。
- `cfg = RuntimeConfig(config_path or home/.config/serialwrap/config.yaml)`（CLI 傳 `CONFIG_DIR/config.yaml`）。
- 護欄 2：`_system_target = target=="systemd-system"`；`_system_old_transition = old=="systemd-system" and old!=target`；任一且 `not with_sudo` → pending = （old transition：`["sudo","systemctl","stop","serialwrap"]`、`["sudo","systemctl","disable","serialwrap"]`）+（system target：`_system_install_cmds(home, include_start=(old!=target))`）；回 `{"mode": old_mode, "requested_mode": target_mode, "transitioned": False, "applied": False, "ran": [], "pending_sudo": pending}`；**不停舊、不起新、不寫 config**（但已 stage 檔案）。
- 情境 A 同模式：`systemd-user` → 重寫 user unit、`systemctl --user daemon-reload`、`enable serialwrap`（不 start）；`systemd-system`（必有 with_sudo）→ 執行 `_system_install_cmds(include_start=False)`；`on-demand` → 無；`cfg.set_mode(target, socket_path=socket_path)`；回 `{"mode":target,"transitioned":False,"applied":True,"ran":[list(c) for c in getattr(fx,"calls",[])],"pending_sudo":pending_sudo}`。
- 情境 B 模式改變（嚴格順序）：
  a. `_stop_old`：`on-demand` 且 `daemon_running` → `fx.run(["serialwrap","daemon","stop"])`；`systemd-user` → `systemctl --user stop serialwrap`、`systemctl --user disable serialwrap`；`systemd-system` → with_sudo 執行 `sudo systemctl stop/disable serialwrap`，否則加入 pending（實務上已被護欄 2 早退）。
  b. state 遷移：`_state_path_for(mode, home)`：`systemd-system` → `/var/lib/serialwrap/state.json`；其他 → `($XDG_STATE_HOME or home/.local/state)/serialwrap/state.json`；兩者不同才 `migrate_legacy_state(legacy, new)`（legacy 存在且 dest 不存在才 `copy2`，**複製非搬移**；例外吞掉）。
  c. `_start_new`：`systemd-user` → 寫 `home/.config/systemd/user/serialwrap.service`（`render_user_unit("%h/.local/bin/serialwrapd")`）→ `systemctl --user daemon-reload`/`enable serialwrap`/`start serialwrap` → `linger_user = $USER or $LOGNAME`，有才 `loginctl enable-linger <user>`；`systemd-system` → 執行 `_system_install_cmds(include_start=True)`（無 sudo 時 extend pending，防禦）；`on-demand` → 無。
  d. `cfg.set_mode(target, socket_path=socket_path)`。
  回 `{"mode":target,"transitioned":True,"applied":True,"ran":[...],"pending_sudo":[...]}`。
- 所有 `fx.run` 回傳碼**皆被忽略**（不因 systemctl 失敗而中止或回報）。
- 生產環境 `SystemEffects` 無 `calls` → `ran` 恆為 `[]`（§13 O-07）。
- 不刪除舊 scope 的 unit 檔（system→user 時 `/etc/systemd/system/serialwrap.service` 保留，只 disable）。

### 6.7 CLI `setup`（parser cli.py:2371-2398；`_run_setup` :1673-1801）
旗標：互斥群組 `--user` | `--system` | `--on-demand`（dest `on_demand`）；`--force`；`--with-sudo`（dest `with_sudo`）。**無 `--rollback`**。
流程：
1. `legacy = detect_legacy_install()`。
2. `target = _resolve_target_mode(args, fx)`：`--user`→`systemd-user`、`--system`→`systemd-system`、`--on-demand`→`on-demand`、否則 `fx.has_systemd()` ? `systemd-user` : `on-demand`。
3. `old = _default_runtime_config().mode() or "on-demand"`（壞 YAML → traceback）。
4. `daemon_running = health.ping(0.5s).ok`、`any_flashing = mcu.status(0.5s).flashing`，皆經 `_resolve_local_endpoint_info(args)`（忽略 --bench），任何例外 → False。
5. `any_flashing and not args.force` → `{"ok":False,"error_code":"FLASHING_BUSY","message":"flash 進行中，拒絕 setup（可用 --force 覆寫）"}`，mirror_err(context="setup")，**exit 2**（物化前）。
6. `materialize_assets(force=args.force)`。
7. `wsl_info = ensure_wsl_systemd(fx, ~)`；`needs_restart` → 印 `{"ok":True,"legacy":legacy,"wsl_systemd_enabled":True,"needs_restart":True,"requested_mode":target,"hint":...}` exit 0。
8. `effective_socket`：若 `old == target == "on-demand"` 且非 win backend 且 `args.socket is None` 且無 `--endpoint`，且既有 config `socket_path` 為可解析的 `tcp` endpoint → 保留之（`preserved_remote_socket`）；否則 `SYSTEM_SOCKET`（target=systemd-system）或 `SOCKET_PATH`。
9. `reconcile(..., home=~, socket_path=effective_socket, config_path=CONFIG_DIR/config.yaml)`；`FlashingBusy` → `{"ok":False,"error_code":"FLASHING_BUSY","message":str(exc),"legacy":legacy}` exit 2。
10. 成功輸出 `{"ok":True,"legacy":legacy,"setup":result,"doctor_hint":"serialwrap doctor 可驗證環境","console_hint":"human console 請用 serialwrap-minicom COMx（勿直接 minicom -D /dev/ttyUSBx，避免與 daemon two-reader 衝突）"[,"pending_sudo":[...]]}`，**exit 0**（含 `applied:false` 未授權 system 情境）。

---

## 7. doctor（`doctor_cmd.py` + `cli._run_doctor`）

### 7.1 項目結構
每項 `{"check": str, "ok": bool, "detail": str, "fix": str, "advisory": bool}`；`fix` 在 ok 時為 `""`；`advisory` 由 `run_doctor` 的 `_stamp` 依平台集合蓋章。

`DOCTOR_ADVISORY_CHECKS`（Linux，:566-570）= `{"systemd","wsl_systemd","devices","other_serialwrap_installs","wal_dir","serialwrap_minicom_on_path","jq_on_path","minicom_on_path","profile_bootloader_prompts"}`。
`DOCTOR_ADVISORY_CHECKS_WIN`（:571-575）= `{"serialwrap_on_path","serialwrapd_on_path","daemon_endpoint","devices","other_serialwrap_installs","wal_dir","profile_bootloader_prompts"}`。

### 7.2 `run_doctor(fx=None, home=None, *, platform=None) -> list[dict]`（:578-652）
`plat = platform or sys.platform`；`plat.startswith("win")` → Windows 清單，否則 Linux 清單。順序：

**Linux**（18 項，列序即輸出序）：`python`、`pyyaml`、`serialwrap_on_path`、`serialwrapd_on_path`、`other_serialwrap_installs`、`dialout`、`serialwrap_minicom_on_path`、`jq_on_path`、`minicom_on_path`、`systemd`、`supervision_mode`、`single_daemon`、`endpoint_reachable`、`wal_dir`、`wal_writable`、`profile_bootloader_prompts`、`devices`、`wsl_systemd`。非 advisory（會拉低整體 ok）：`python`、`pyyaml`、`serialwrap_on_path`、`serialwrapd_on_path`、`dialout`、`supervision_mode`（恆 ok）、`single_daemon`、`endpoint_reachable`、`wal_writable`。

**Windows**（12 項）：`python`、`pyyaml`、`pyserial`、`serialwrap_on_path`、`serialwrapd_on_path`、`other_serialwrap_installs`、`supervision_mode`、`daemon_endpoint`、`wal_dir`、`wal_writable`、`profile_bootloader_prompts`、`devices`（Windows 版）。非 advisory：`python`、`pyyaml`、`pyserial`、`supervision_mode`、`wal_writable`。Windows 不含 `single_daemon`／`endpoint_reachable`／`dialout`／`systemd`／`wsl_systemd`。

### 7.3 各檢查明細
| check | ok 判準 | detail | fix（not ok） |
|---|---|---|---|
| `python` | `sys.version_info >= (3,10)` | `"{major}.{minor}.{micro}"` | `"需 Python ≥ 3.10"` |
| `pyyaml` | `import yaml` 成功 | `yaml.__version__` 或 `"已安裝"`；失敗 `"未安裝"` | `"安裝 PyYAML（pipx 安裝會自動帶入）"` |
| `pyserial`（win） | `import serial` | `serial.__version__`/`"已安裝"`/`"未安裝"` | `"安裝 pyserial（pipx 於 Windows 自動帶入；release exe 已內嵌）"` |
| `<name>_on_path` | `fx.which(name) is not None` | 路徑或 `"找不到"` | Linux 預設 `"確認 ~/.local/bin 在 PATH（pipx ensurepath）"`；Windows `"將 serialwrap.exe / serialwrapd.exe 所在目錄加入 PATH"` |
| `serialwrap_minicom_on_path` | `which("serialwrap-minicom")` | 同上 | `"執行 `serialwrap setup` 物化 minicom wrapper 到 ~/.local/bin（並確認該目錄在 PATH，可用 pipx ensurepath）；human console 一律經 serialwrap-minicom COMx，勿直接對 tty 開 minicom（避免與 daemon two-reader 衝突）"` |
| `jq_on_path` | `which("jq")` | 同上 | `"安裝 jq（Debian/Ubuntu/Mint: sudo apt install jq）——serialwrap-minicom wrapper 解析 session 狀態需要"` |
| `minicom_on_path` | `which("minicom")` | 同上 | `"安裝 minicom（Debian/Ubuntu/Mint: sudo apt install minicom）"` |
| `other_serialwrap_installs` | `paths = fx.which_all("serialwrap")`；≤1 筆 → ok（不跑 subprocess）；多筆 → 各跑 `[p,"--version"]`（`timeout_s=2.0`），`rc==0 且 out.strip()` 取 `out.strip()` 否則 `"無法取得"`；`ok = len(set(versions))==1` | ≤1：有 → `"僅偵測到目前這份"`，無 → `"PATH 上找不到 serialwrap（可能以絕對路徑呼叫）"`；多：`"；".join(f"{p}={v}")` | `"確認各安裝版本一致，或統一改呼叫同一份（建議 pipx 系統安裝／以絕對路徑或 SERIALWRAP_ENDPOINT 釘住）"` |
| `dialout` | `fx.user_in_group("dialout")` | `"已在 dialout 群組"`/`"不在 dialout 群組"` | `"sudo usermod -aG dialout $USER（之後重新登入）"` |
| `systemd` | `fx.has_systemd()` | `"systemd 為 init"`/`"未偵測到 systemd"` | `"WSL 可於 /etc/wsl.conf 設 [boot] systemd=true 後 wsl --shutdown（on-demand 模式可忽略）"` |
| `supervision_mode` | 恆 True | `(rc.mode() if rc else None) or "on-demand"`（`cli._safe_runtime_config`；`home` 參數未使用） | `""` |
| `single_daemon` | `not detect_multi_open()["multi_open"]` | `f"{n} 個 serialwrapd 在跑"` + 多開時 `"（偵測到多開）"` | `"停掉多餘 daemon（serialwrap service stop；並檢查 systemd-user 與 system 是否同時在跑）"` |
| `endpoint_reachable` | 見下 | 見下 | 見下 |
| `daemon_endpoint`（win） | `lock_win._endpoint_alive(endpoint)`；`endpoint = cfg_sock（僅 tcp:// 開頭才採用）or cli._local_default_endpoint()` | `f"{endpoint}（{'可連' if ok else '未在跑'}）"` | `"serialwrap daemon start（或 serialwrapd.exe --socket tcp://127.0.0.1:48700）"` |
| `wal_dir` | 見下 | | |
| `wal_writable` | 見下 | | |
| `profile_bootloader_prompts` | 見下 | | |
| `devices`（Linux） | `os.path.isdir(DEVICE_BY_ID_DIR)` 且 `len(os.listdir) > 0`（**只看 by-id、不看 by-path、含 dangling 項**） | 條目數字串（目錄不存在 `"0"`；例外 `"無法讀取"`） | `"確認 USB-serial 已接上且有 /dev/serial/by-id/* （或設 SERIALWRAP_BY_ID_DIR）"` |
| `devices`（win） | `kept = exclude_bluetooth(_read_serialcomm(), _read_bt_ports(), _load_exclude_coms())`；`len(kept) > 0` | 有 → `f"{len}（{', '.join(sorted(kept))}）"`，無 → `"0"`；`excluded = len(serialcomm)-len(kept)` > 0 時追加 `f"；排除 {excluded}（藍牙/exclude_coms）"`；例外 → `"無法讀取 SERIALCOMM"` | `"確認 USB-serial 已接上並出現在裝置管理員（藍牙 COM 會被自動排除）"` |
| `wsl_systemd` | `(not is_wsl) or has_systemd` | `"非 WSL 環境"`/`"WSL 已啟用 systemd"`/`"WSL 未啟用 systemd"` | `"於 /etc/wsl.conf 設 [boot] systemd=true 後 wsl --shutdown（on-demand 模式可忽略）"` |

`endpoint_reachable`（:417-483）：
- `resolved, source = cli._resolve_default_endpoint_with_source()`；例外 → ok True，detail `f"無法解析本 client 的 endpoint（{exc}），略過"`。
- `daemons = detect_multi_open()["daemons"]` 空 → ok True，detail `f"未偵測到執行中的 serialwrapd（on-demand 模式正常）；本 client 解析到 {resolved}（來源：{source}）"`。
- `cli._endpoint_alive(resolved)`（unix：0.2s connect；**POSIX 上 tcp 恆 True 不探測**）→ ok True，detail `f"本 client 解析到 {resolved}（來源：{source}），可連上執行中 daemon"`。
- 否則 ok False，detail `f"本 client 解析到 {resolved}（來源：{source}）連不上；目前執行中 daemon 綁在 {'、'.join(sorted({d.socket or '未知（無法從 /proc 讀出 --socket）'}))}"`，fix `"確認本 client 的 SERIALWRAP_STATE_DIR/config.yaml 是否與該 daemon 一致（可用 --socket/--endpoint 明確指定該路徑繞過解析；daemon 若為 #173 修正後版本，重啟一次即會把實際 socket 同步寫回 config.yaml）"`。
- source 標籤（cli.py:810-835）：config 有 socket 且 resolved 相等 → `"config.yaml"`；不等 → `f"config.yaml（記錄 {cfg_sock} 不可連，#108 dangling fallback 改用 canonical）"`；無 → `"預設（SOCKET_PATH，受 SERIALWRAP_STATE_DIR/XDG 環境變數影響）"`。

`wal_dir`（:358-414）：`endpoint = cfg_sock or _local_default_endpoint()`（**不做 dangling fallback**）；`rpc_call(endpoint,"health.status",{},timeout_s=0.5)`；`not ok or not wal_path` → ok True，detail `f"daemon 未在跑或無法連線，僅顯示本地端解析值：WAL_DIR={constants.WAL_DIR}"`；否則 `daemon_wal_dir = dirname(resp["wal_path"])`；`shell_override = $SERIALWRAP_WAL_DIR.strip()`；`mismatch = bool(shell_override) and normpath(expanduser(shell_override)) != normpath(daemon_wal_dir)` → ok False，detail `f"daemon 實際生效 WAL_DIR={d}，與 shell SERIALWRAP_WAL_DIR={s} 不一致"`，fix 長字串（systemd unit 加 `Environment=SERIALWRAP_WAL_DIR=<path>`…以 `daemon status` 的 `wal_path` 為準）；一致 → ok True detail `f"daemon 實際生效 WAL_DIR={d}"`。

`wal_writable`（:292-355，**非 advisory**）：同 endpoint；`health.status` 0.5s；`wal = resp.get("wal")`；`not resp.ok or wal 非 dict` → ok True，detail `"daemon 未在跑、無法連線，或為不回報 wal 健康欄位的舊版 daemon；略過"`。`wal.healthy` → ok True，detail `f"WAL 目錄可寫：{wal_dir}（current_seq={current_seq}）"` + `recreated_count` 真時 `f"；曾自癒重建 {n} 次（消失期間的紀錄無法復原）"`。否則 reasons：`not wal_dir_exists` → `"目錄不存在"`、elif `not wal_dir_writable` → `"目錄不可寫"`；`int(current_seq or 0) > 0 and not wal_file_exists` → `f"已寫過 {seq} 筆紀錄但現行 WAL 檔不存在"`；`write_failures` → `f"append 失敗 {n} 次（最後一次：{last_write_error}）"`；ok False，detail `f"WAL 目錄 {wal_dir} 異常：{'；'.join(reasons) or '未知'}"`，fix 長字串（testpilot `clean_wal()`、hamanpaul/testpilot-core#36…）。依賴 health.status `wal` 欄位：`healthy, wal_dir, current_seq, recreated_count, wal_dir_exists, wal_dir_writable, wal_file_exists, write_failures, last_write_error`。

`profile_bootloader_prompts`（:486-559）：資產 = `dirname(doctor_cmd.py)/assets/profiles/default.yaml`（直接路徑，非 importlib.resources）；線上 = `PROFILE_DIR/default.yaml`。資產讀失敗 → ok True `"資產不可讀，略過"`；線上讀失敗 → ok True `f"線上 profile 未物化（{live_path}），略過"`。`_profile_templates_missing_bootloader_prompts(asset_text, live_text)`：yaml 失敗或 profiles 非 dict → []；對資產中 `bootloader_prompts` 真值的 template，若線上同名 template 為 dict 且其 `bootloader_prompts` 為假值 → 收集；回 `sorted`。空 → ok True `"與出貨資產一致"`；否則 ok False，detail `f"線上 {live_path} 的 {', '.join(drifted)} 缺 bootloader_prompts（出貨資產有）"`，fix `"重新物化資產或手動補上：備份現有檔後執行 `serialwrap setup`，或把 sw_core/assets/profiles/default.yaml 對應 template 的 bootloader_prompts 段落補進線上檔"`（註：`setup` 不加 `--force` 不會覆寫既有 profile）。

### 7.4 CLI `doctor`（cli.py:1649-1659）
`report = run_doctor()`；`overall_ok = all(item["ok"] or item["check"] in advisory)`（advisory 依 `sys.platform`）；stdout `{"ok": overall_ok, "checks": report}`（`_print`：`sort_keys=True`、`ensure_ascii=False`、`separators=(",",":")`）；**exit 恆為 0**。

---

## 8. remote 隧道（`remote_tunnel.py` + `cli._run_remote`）

### 8.1 型別與純函式
- `_TARGET_RE = r"^(?:(?P<user>[^@:]+)@)?(?P<host>[^@:]+):(?P<port>\d+)$"`。
- `class TunnelError(Exception)`：`__init__(code, message=None)`；`self.code`、`self.message`（可 None）。
- `@dataclass(frozen=True) TunnelSpec`：`role: str`（`"expose"`|`"connect"`）、`ssh_target: str`、`port: int`、`local: int|None=None`、`forward_src: str|None=None`、`remote_socket: str|None=None`、`via: str="ssh"`（`"ssh"`|`"autossh"`）、`ssh_opts: tuple[str,...]=()`、`ready_timeout: float=10.0`。
- `parse_target(target) -> (ssh_target, port)`：不符 → `TunnelError("INVALID_TARGET", f"target 需為 [user@]host:port，取得 {target!r}")`；`not 1<=port<=65535` → `TunnelError("INVALID_TARGET", f"port 超出範圍：{port}")`；`ssh_target = f"{user}@{host}" if user else host`。
- `compute_identity(spec)` = `sha256("|".join(str(x) for x in (role, ssh_target, port, local, forward_src, remote_socket, via, ";".join(ssh_opts)))).hexdigest()`（None 變字串 `"None"`；`ready_timeout` 不計）。
- `default_ssh_opts(control_path)` = `["-o","BatchMode=yes","-o","ExitOnForwardFailure=yes","-o","ControlMaster=auto","-o",f"ControlPath={cp}","-o","ControlPersist=no","-o","ServerAliveInterval=30","-o","ServerAliveCountMax=3"]`（置於使用者 `--ssh-opt` 之前，OpenSSH 取第一個同鍵值 → 預設為權威）。
- `_direction_args(spec)`：expose → `["-R", f"{bind}:{forward_src}"]`，`bind = remote_socket or f"127.0.0.1:{port}"`；connect → `local = spec.local if not None else port`；`dest = remote_socket or f"localhost:{port}"`；`["-L", f"127.0.0.1:{local}:{dest}"]`。
- `build_argv(spec, cp)` = `(["autossh","-M","0"] if via=="autossh" else ["ssh"]) + ["-N","-T"] + default_ssh_opts(cp) + list(ssh_opts) + _direction_args(spec) + [ssh_target]`（argv[0] 為裸名，Popen 依 PATH 解析）。
- `read_pid_start_ticks(pid)`：讀 `/proc/{pid}/stat`；取最後一個 `)` 後 `data[rparen+2:].split()`；`fields[19]`（第 22 欄 starttime）轉 int；任何失敗 → None。
- `pid_alive(pid, start_ticks)`：`os.kill(pid, 0)` OSError → False；`start_ticks is None` → True；目前 ticks 為 None → True；否則 `current == start_ticks`。
- `endpoint_for(spec)` = `f"tcp://127.0.0.1:{spec.local if not None else spec.port}"`（expose 時亦然，即遠端 loopback port；`--remote-socket` 時此值與實際 unix socket 不符）。
- `_bind_is_loopback_only(ss_out, port)`：逐行，行含 `f":{port}"` 者，對空白切出的 token 以 `f":{port}"` 結尾者：`addr = tok.rsplit(":",1)[0].strip("[]")`，`found=True`；`addr` 非 `"127.0.0.1"`、非 `startswith("127.")`、非 `"::1"` → 立即 False；最終回 `found`。
- `_POLL_INTERVAL_S = 0.2`。

### 8.2 Readiness
- `make_ssh_check(spec, cp, *, runner)` → `lambda: runner(["ssh","-O","check","-o",f"ControlPath={cp}", ssh_target])[0] == 0`（即使 via=autossh 也用 `ssh`）。
- `make_role_probe(spec, cp, endpoint, *, runner, ping)`：connect → `bool(ping(endpoint))`；expose+remote_socket → True；expose+tcp → `runner(["ssh","-o",f"ControlPath={cp}", ssh_target, "ss","-ltnH", f"sport = :{port}"])`；`rc!=0 or not out.strip()` → `TunnelError("REMOTE_BIND_UNVERIFIED","無法在 relay 驗證遠端 bind")`；`not _bind_is_loopback_only` → `TunnelError("REMOTE_BIND_UNVERIFIED", out.strip()[:200])`；否則 True。**第一次 probe 失敗即 raise，不重試**。
- `wait_ready(spec, pid, start_ticks, *, ssh_check, role_probe, sleep, monotonic, alive, stderr_tail) -> str`：`deadline = monotonic()+ready_timeout`；loop：`not alive()` → `tail=(stderr_tail() or "").strip()[-500:]` → `TunnelError("TUNNEL_SPAWN_FAILED", tail)`；`ssh_check() and role_probe()`（短路）→ `"active"`；`monotonic() >= deadline` → `"starting"`；`sleep(0.2)`。
- `make_runner()`：`subprocess.run(argv, stdout=PIPE, stderr=STDOUT, text=True, timeout=10.0, check=False)` → `(rc, stdout or "")`；`TimeoutExpired` → `(124, f"probe timeout: {partial}"[:500])`。
- `real_ping(endpoint)` = `bool(client.rpc_call(endpoint,"health.ping",{},timeout_s=2.0).get("ok"))`，例外 → False。

### 8.3 Registry（`<run_dir>/remote/`）
- `Registry(run_dir)`：`remote_dir = join(run_dir,"remote")`。
- `lock()`：contextmanager；`makedirs(remote_dir)`；`fd=os.open(remote_dir/".registry.lock", O_RDWR|O_CREAT, 0o600)`；`fcntl.flock(fd, LOCK_EX)`（**阻塞**）；finally `LOCK_UN` + close。
- `control_path(p)` = `remote_dir/cm-{p}`；`state_path(p)` = `remote_dir/{p}.json`；`log_path(p)` = `remote_dir/{p}.log`。
- `write(state)`：`tmp = f"{path}.tmp.{os.getpid()}"`；`json.dump(state, ensure_ascii=False, sort_keys=True, separators=(",",":"))`；`os.replace`（無 fsync）。
- `read(p)`：OSError/JSONDecodeError → None。`read_all()`：`sorted(listdir)` 中 `.json` 結尾者（`.json.tmp.<pid>` 不符）；壞檔略過。
- `remove(p)`：unlink state、control、log（OSError 吞掉）。

State JSON 欄位（open_tunnel :459-466）：`identity`、`status`（`spawning`→`active`|`starting`；close 失敗 `error`）、`role`、`pid`、`pgid`、`pid_start_ticks`（int|null）、`target`（ssh_target）、`listen_port`、`remote_bind`（`remote_socket or f"127.0.0.1:{port}"`，connect 也填）、`forward_target`（forward_src，connect 為 null）、`via`、`control_path`、`endpoint`。

`_public(state)`：保留存在的 `status, role, pid, listen_port, target, remote_bind, forward_target, via, endpoint, identity`；`role=="expose"` 加 `remote_hint = f"agent 端用 serialwrap --endpoint {state.get('endpoint', 'tcp://127.0.0.1:%d' % listen_port)}"`。（去除 `pgid`、`pid_start_ticks`、`control_path`。）

### 8.4 生命週期
- `open_tunnel(spec, run_dir, *, spawner, runner, ping, ssh_check_factory=make_ssh_check, role_probe_factory=make_role_probe, clock=None) -> dict`：
  - `listen_port = spec.local if (role=="connect" and local is not None) else spec.port`。
  - 持鎖：`existing = reg.read(listen_port)`；`alive = pid>0 and pid_alive(pid, pid_start_ticks)`（`pid` 缺 → -1）；alive 且 identity 同 → 回 `{"ok":True,"already_running":True, **_public(existing)}`；alive 且 identity 異 → `TunnelError("TUNNEL_CONFLICT", f"port {listen_port} 已有不同 identity 的隧道，請先 remote close {listen_port}")`；死 → `reg.remove`。`spawner(build_argv(spec,cp), cp, log_path) -> (pid, pgid, start_ticks, stderr_tail_fn)`；寫 `status="spawning"`。
  - 鎖外 `wait_ready`；任何 `BaseException`（含 KeyboardInterrupt）→ `_terminate_pgid(pgid, sleep=clock.sleep)`、持鎖 `reg.remove`、re-raise。
  - 成功 → 持鎖寫回 `status`；回 `{"ok":True, **_public(state)}`。
- `real_spawner(argv, cp, log_path)`：makedirs 兩者父目錄；`open(log_path,"ab",buffering=0)`；`subprocess.Popen(argv, stdin=DEVNULL, stdout=log, stderr=log, start_new_session=True, close_fds=True)`；父端 close log；`pgid=os.getpgid(pid)`；`start_ticks=read_pid_start_ticks(pid)`；`_stderr_tail()` 每次重開 log 讀末 500 bytes `decode("utf-8","replace")`。
- `_reap_pgid(pgid)`：`os.waitpid(-pgid, WNOHANG)` 迴圈至回 0 或例外。
- `_terminate_pgid(pgid, *, timeout=5.0, sleep=time.sleep)`：`killpg(SIGTERM)`（吞 ProcessLookupError/PermissionError）；`waited<timeout` 迴圈：reap → `killpg(pgid,0)` ProcessLookupError → return；`sleep(0.1)`、`waited+=0.1`；逾時 `killpg(SIGKILL)` + reap。
- `status(run_dir)`：持鎖；每個 state：`pid>0 and pid_alive` → `_public` + `alive:True`；否則 `reg.remove(listen_port)`（prune，不殺）。孤兒掃描：`sorted(listdir)` 中 `cm-*` 且 `reg.read(_port_of_cm(name))` 為 None → `{"status":"orphan","control_path":<path>,"alive":True}`（alive 未驗證）。回 `{"ok":True,"tunnels":[...]}`。`_port_of_cm`：`int(name[3:])`，失敗 -1。
- `close(run_dir, selector, *, sleep=time.sleep)`：持鎖；`read_all()`，`str(selector)!="all"` 時篩 `str(listen_port)==str(selector)`；每筆：`pgid = int(st.get("pgid", pid))`；`pid>0 and pid_alive` → `_terminate_pgid`，例外 → 寫回 `status="error"`、`continue`（不 remove、不列入 closed）；然後 `reg.remove(port)`、`closed.append(port)`。回 `{"ok":True,"closed":[int...]}`（找不到 → `closed:[]`，冪等）。pid 已死時**不對 pgid 送訊號**。
- `guard_platform()`：`os.name=="nt"` → `TunnelError("REMOTE_NOT_SUPPORTED","native Windows 本期不支援 serialwrap remote；請手動 ssh -R（見 SKILL_WINDOWS.md）")`。
- `resolve_ssh_bin(via)`：`shutil.which("autossh" if via=="autossh" else "ssh")`，無 → `TunnelError("SSH_NOT_FOUND", f"PATH 找不到 {name}")`；回傳路徑**未被 build_argv 使用**。

### 8.5 CLI `remote`（parser cli.py:2234-2279；`_run_remote` :1532-1618）
參數：`words`（nargs `*`）、`-R`（dest `reverse`）、`-L`（dest `forward`）、`--autossh`、`--local INT`（預設 None）、`--remote-socket`（dest `remote_socket`）、`--ready-timeout FLOAT`（預設 10.0）、`--ssh-opt`（append，預設 []）；全域 `--socket/--endpoint/--bench/--timeout`。-R/-L 刻意不用 argparse 互斥群組。
流程：
1. `os.name=="nt"` → `{"ok":False,"error_code":"REMOTE_NOT_SUPPORTED","message":"native Windows 本期不支援 serialwrap remote；請手動 ssh -R（見 SKILL_WINDOWS.md）"}`，mirror_err，exit 1（在 import remote_tunnel 之前）。
2. `run_dir = _remote_run_dir()`（呼叫時讀 `$SERIALWRAP_RUN_DIR`（strip 非空 → expanduser）否則 `constants.RUN_DIR`）。
3. `words` 空或 `words[0]=="status"` → print `rt.status(run_dir)`，exit 0。
4. `words[0]=="close"` → `selector = words[1] if len>1 else "all"`；print `rt.close(...)`，exit 0。
5. 否則 open：`guard_platform()`；`-R` 且 `-L` → `INVALID_ARGS` `"-R 與 -L 不可同時指定"`；`role = "connect" if -L else "expose"`；`--local` 用於 expose → `INVALID_ARGS` `"--local 僅用於 -L（connect）"`；`--local` 越界 → `INVALID_ARGS` `f"--local 超出範圍：{n}"`；`parse_target(words[0])`；`resolve_ssh_bin`；expose 時 `forward_src = _forward_src_from_endpoint(_resolve_endpoint(args))`（unix → AF_UNIX 路徑原樣；tcp → `f"127.0.0.1:{port}"`；ValueError → `INVALID_ENDPOINT`）；建 `TunnelSpec(..., ssh_opts=tuple(args.ssh_opt), ready_timeout=args.ready_timeout)`；`open_tunnel(spec, run_dir, spawner=real_spawner, runner=make_runner(), ping=real_ping)`；print，**exit 0**（含 `status:"starting"`）。
6. 錯誤：`TunnelError` → `{"ok":False,"error_code":code,"message":message or code}` exit 1；`_EndpointResolutionError`（如 `--bench` 未記住）→ exit 1；其他 → `INTERNAL_ERROR` exit 1。
7. 多餘 words（例如 `remote host:1 extra`）被忽略。

### 8.6 BC-05 相關：POSIX-only 依賴清單（逐項）
| # | 依賴 | 位置 | Windows 狀況 |
|---|---|---|---|
| 1 | `import fcntl`（模組頂層）+ `fcntl.flock` registry 鎖 | remote_tunnel.py:10,206 | 無 fcntl → import 失敗（故 cli 延遲 import 並先擋 nt） |
| 2 | bench state 跨行程鎖 `fcntl.flock(<state>.lock)` | cli.py:1125-1145 | nt 只用 `threading.Lock`（無跨行程保護） |
| 3 | OpenSSH 連線多工 `ControlMaster=auto`／`ControlPath=<remote_dir>/cm-<port>`（Unix domain socket）／`ControlPersist=no` 與 `ssh -O check` readiness、`ssh -o ControlPath=... host ss ...` 借 master 連線 | :82-92,280-317 | Win32-OpenSSH 不支援 ControlMaster/ControlPath（推論，屬 BC-05 須替換的 readiness 機制）；另 Unix socket 路徑長度上限（~108 bytes）在 POSIX 也是限制 |
| 4 | 行程群組：`start_new_session=True`（setsid）、`os.getpgid`、`os.killpg(SIGTERM/SIGKILL/0)`、`os.waitpid(-pgid, WNOHANG)` | :356-403,602-627 | 無 killpg/getpgid；需 Job Object 等價 |
| 5 | `os.kill(pid, 0)` 存活探測 | :142 | **Windows 上 `os.kill(pid, 0)` 會呼叫 TerminateProcess 殺掉目標**（CPython 行為），Rust 必須改用 OpenProcess/GetExitCodeProcess |
| 6 | `/proc/<pid>/stat` starttime 防 PID 重用 | :118-136 | 不存在 → 降級 pid-only |
| 7 | relay 端 `ss -ltnH 'sport = :<port>'` | :308-311 | relay 為遠端 POSIX，與本機平台無關 |
| 8 | forward_src 為 AF_UNIX 路徑（本機 daemon unix socket） | cli.py:1621-1631 | Windows daemon 為 tcp → `127.0.0.1:<port>` |
| 9 | 程式碼中**沒有任何 `os.pipe`／pipe-based 非阻塞轉接**；ssh 子行程 stdout/stderr 直接導向 log 檔、stdin DEVNULL | | spec 用語「POSIX-only pipe 依賴」與現碼不符（§13 D-08） |

---

## 9. connect／benches（`bench_registry.py` + cli）

### 9.1 `bench_registry.py`
- `_ALLOWED_ENTRY_KEYS = frozenset({"target","remote_socket","local_port","ssh_opts","autossh"})`（**全部必填**）；`_ALLOWED_ROOT_KEYS = frozenset({"benches"})`。
- `@dataclass(frozen=True) BenchEntry(target: str, remote_socket: str, local_port: int, ssh_opts: tuple[str,...]=(), autossh: bool=False)`。
- 路徑：`_default_benches_path()` = `SERIALWRAP_BENCHES_FILE` > `(SERIALWRAP_CONFIG_DIR > ($XDG_CONFIG_HOME or ~/.config)/serialwrap)/benches.yaml`（呼叫時解析）。
- `load_benches(path)`：expanduser；不存在 → `{}`；`yaml.safe_load`（None → {}）；root 非 dict → `ValueError("benches.yaml 必須是 object")`；未知根鍵 → `ValueError(f"benches.yaml 包含不支援根欄位 {names}；只能使用 benches")`；`benches` 非 dict → `ValueError("benches.yaml 的 benches 必須是 object")`；code 經 `_require_string(field_name="code", code="<root>")`（strip、非空）。
- `_entry_from_raw(code, raw)` 驗證順序與訊息：非 dict → `f'bench "{code}" 必須是 object'`；多餘鍵 → `f'bench "{code}" 包含不支援欄位 {names}；benches.yaml 必須維持 provider-neutral，只能使用 autossh, local_port, remote_socket, ssh_opts, target'`；缺鍵 → `f'bench "{code}" 缺少必要欄位 {names}'`；`target` 字串 strip 非空，且 `count("@")==1`、user/host 非空且無前後空白 → 否則 `f'bench "{code}" 的 target 必須是 user@host 形式'`（**target 不含 port**）；`remote_socket` 字串非空；`local_port` 為非 bool 的 int、1..65535；`ssh_opts` 為 list[str]；`autossh` 為 bool。字串欄位存的是 `strip()` 後值。
- `load_configured_benches()`、`resolve(code)`（KeyError → `KeyError(f"unknown bench code: {code}")`）。
- `to_remote_argv(entry)` = `["-L","--remote-socket",remote_socket,(--autossh)?,*[f"--ssh-opt={o}"],f"{target}:{local_port}"]`（目前 CLI 未使用）。

### 9.2 bench endpoint 記憶（cli.py:1115-1243）
- 路徑 `_bench_state_path()` = `(SERIALWRAP_STATE_DIR（strip 非空）> ($XDG_STATE_HOME or ~/.local/state)/serialwrap)/benches.state.json`（expanduser）。
- 鎖：`<path>.lock`（POSIX flock + process 內 `_BENCH_STATE_IO_LOCK`；nt 僅 threading lock）。
- `_load_bench_state_doc(path) -> (payload, benches)`：不存在 → 同一個 `{}` 兩次；非 dict → `ValueError("benches.state.json 必須是 object")`；`payload["benches"]` 缺 → `(payload, payload)`（**扁平格式：code 直接在根**）；存在但非 dict → `ValueError("benches.state.json 的 benches 欄位必須是 object")`。
- 寫入格式：`{code: "tcp://127.0.0.1:<local_port>"}`（新檔即扁平根）；`mkstemp(dir, prefix=f"{basename}.tmp.")` + `json.dump(sort_keys=True, ensure_ascii=False, separators=(",",":"))` + `os.replace`。
- `_remembered_bench_endpoint(benches, code)`：缺 → None；非字串/空/無法解析/非 tcp/host 不在 `LOOPBACK_TCP_HOSTS` → `ValueError(f'bench "{code}" 的 endpoint 記憶損壞；請重新執行 serialwrap connect {code}')`；回 strip 後值。
- `--bench CODE` 解析（`_resolve_endpoint_info`，cli.py:747-762）：`INVALID_BENCH_STATE`／`BENCH_STATE_IO_ERROR`／未記住 → `BENCH_ENDPOINT_NOT_REMEMBERED`（message `f'bench "{code}" 尚未記住 endpoint；請先執行 serialwrap connect {code}'`）。優先序：`--endpoint` > `--socket` > `--bench` > config.yaml（含 dangling fallback）> 預設。

### 9.3 CLI `connect CODE [--close]`（cli.py:2282-2292；`_run_connect` :1333-1459）
- nt → `REMOTE_NOT_SUPPORTED` `"native Windows 本期不支援 serialwrap connect；請手動 ssh -L（見 SKILL_WINDOWS.md）"` exit 1。
- `guard_platform()`；`bench_registry.resolve(code)`：KeyError → `UNKNOWN_BENCH_CODE`（message `"unknown bench code: <code>"`）；ValueError → `INVALID_BENCH_CONFIG`。
- `via = "autossh" if entry.autossh else "ssh"`；`spec = TunnelSpec(role="connect", ssh_target=entry.target, port=entry.local_port, remote_socket=entry.remote_socket, via=via, ssh_opts=entry.ssh_opts)`（`local=None`、`ready_timeout=10.0`）→ ssh `-L 127.0.0.1:<local_port>:<remote_socket>`。
- `--close`：讀記憶（ValueError → `INVALID_BENCH_STATE`；OSError → `BENCH_STATE_IO_ERROR`），有記憶 port 則 `close_port = remembered_port`；`close_spec` 同上但 `port=close_port`；`_ensure_connect_close_target_matches`：`_find_stateful_tunnel_for_port`（略過 orphan）存在且（role 不同或 identity ≠ `compute_identity(close_spec)`）→ `TUNNEL_CONFLICT` `f"local_port={p} 已有不相符的 stateful tunnel；僅可關閉相符的 connect tunnel"`；`rt.close(run_dir, str(close_port))`；ok 後 `_connect_tunnel_present`（任何 tunnel 含 orphan，listen_port 或 `cm-<n>` 推得 port 相同）仍在 → `{"ok":False,"error_code":"TUNNEL_STILL_ACTIVE","message":f"local_port={p} 的 tunnel 仍存在，未清除 endpoint 記憶"}` exit 1；否則清除記憶（`benches.pop(code)`）；print close 結果，exit 0。
- open：`resolve_ssh_bin(via)` → `open_tunnel(...)`；ok → `should_rollback = not already_running`；`_remember_bench_endpoint(code, f"tcp://127.0.0.1:{local_port}")`；寫失敗（ValueError → `INVALID_BENCH_STATE`；OSError → `BENCH_STATE_IO_ERROR`）時若 should_rollback 執行 `_rollback_connect_open`（`rt.close` + 確認不在；失敗字串放 `rollback_warning`）→ exit 1。成功 print open 結果 exit 0。
- `_status_tunnels` 防禦：非 dict / not ok / 無 list → `TUNNEL_STATUS_ERROR`。
- 例外 → TunnelError JSON exit 1；其他 → `INTERNAL_ERROR` exit 1。
- argparse 解析錯誤（exit code 2）且命令為 `connect`/`benches`（`_json_boundary_command_from_argv` 會跳過全域選項 `--socket/--endpoint/--bench/--timeout/--retries` 及其值）→ 改印 `{"ok":False,"error_code":"INVALID_ARGS"}` + mirror_err，exit 1（cli.py:2476-2489）。

### 9.4 CLI `benches`（`_run_benches` :1462-1529）
nt → `REMOTE_NOT_SUPPORTED`（message `"native Windows 本期不支援 serialwrap benches；請直接檢視 benches.yaml 與 endpoint 記憶"`）exit 1。`load_configured_benches()`（ValueError → `INVALID_BENCH_CONFIG`）；讀記憶；`alive_ports = {listen_port of status tunnels with role=="connect"}`（orphan 無 role 不計）；輸出 `{"ok":True,"benches":[{"alive":bool,"code":str,"endpoint":str|null}]}`，依 code 排序；`alive` = 記憶 endpoint 為 loopback tcp 且 port ∈ alive_ports。**非 RPC、不連 daemon**。

---

## 10. 版本與打包

### 10.1 `version.resolve_version()`（version.py）
順序：`normpath(dirname(version.py)/../VERSION)` 讀取 strip → `importlib.metadata.version("serialwrap")` → `assets.read_text("VERSION").strip()`（PyInstaller datas 帶入）→ `"unknown"`。
- CLI `--version`（cli.py:1920）：argparse `version` action，stdout `serialwrap <version>`，exit 0。
- daemon：`SerialwrapService._version = resolve_version()`（啟動算一次）；每個 RPC 回應 `setdefault("version", ...)`。
- `_warn_version_mismatch(resp)`（cli.py:913-929）：只在 `_run_rpc` 路徑；daemon version 非空字串且 ≠ cli version 且 cli ≠ `"unknown"` → stderr `serialwrap: client 版本 {c} 與 daemon 版本 {d} 不一致（建議統一安裝來源或重啟 daemon 對齊版本；serialwrap doctor 可查其他安裝）`。**僅警告不阻擋**。

### 10.2 `pyproject.toml`
- build：`setuptools>=61`、`setuptools.build_meta`。
- `name="serialwrap"`、`dynamic=["version"]`（`[tool.setuptools.dynamic] version = {file=["VERSION"]}`，現值 `0.3.2`）、`requires-python=">=3.10"`、description `"UART broker daemon + CLI（多 agent/human 共用序列埠）"`。
- deps：`PyYAML>=6`、`pyserial>=3; sys_platform == 'win32'`。extras：`redos=["google-re2"]`、`dev=["pyinstaller>=6"]`。
- scripts：`serialwrap = "sw_core.cli:main"`、`serialwrapd = "sw_core.daemon:main"`。
- packages：`["sw_core","sw_core.event_engine","sw_core.assets"]`（**不含** repo 根的 `serialwrapd.py`／`serialwrap.py`／`VERSION`）。
- package-data：`"sw_core.assets" = ["profiles/*","tools/*","skill/**/*"]`。（已核對 pipx 安裝結果含全部 7 個資產檔。）

### 10.3 `install.sh`
`set -euo pipefail`；`-h|--help` → usage 兩行 exit 0；有 `pipx` → `pipx install --force "${SCRIPT_DIR}"`，否則 stderr 警告後 `python3 -m pip install --user --force-reinstall "${SCRIPT_DIR}"`；`hash -r`；`serialwrap` 在 PATH → `serialwrap setup "$@"`（其 exit code 即腳本結果）；否則 stderr 提示（`pipx ensurepath`）exit 1。

### 10.4 `serialwrap.spec`（PyInstaller one-file）
`datas = [("sw_core/assets","sw_core/assets"), ("VERSION","sw_core/assets")]`；兩個 Analysis：`["serialwrapd.py"]` → EXE `name="serialwrapd"`，`["serialwrap.py"]` → EXE `name="serialwrap"`；`hiddenimports=["winreg","msvcrt","serial","yaml"]`；`console=True`；無 hooks/excludes；`noarchive=False`。repo 根 `serialwrap.py`／`serialwrapd.py` 為薄 shim（後者重新導出 `BLOCKING_RPC_METHODS`）。

### 10.5 `sw_core/assets/__init__.py`
`_names(subdir)` → 排序檔名（只含檔案）；`list_profile_files()`、`list_tool_files()`；`read_text(relpath)`（`/` 切段，utf-8）；`copy_tree(subdir, dest)`（mkdir；目錄 → `copytree(dirs_exist_ok=True)`；檔 → `copy2`）。

---

## 11. 內嵌資產（`sw_core/assets/`）

| 檔案 | 行數 | sha256 前 16 | 物化目的地 |
|---|---|---|---|
| `profiles/default.yaml` | 127 | `8af14d5745816195` | `CONFIG_DIR/profiles/default.yaml`（不覆寫，除非 --force）；system：`/etc/serialwrap/profiles/` |
| `profiles/brcm.env.example` | 5 | `efaea09764eb07eb` | 同上 |
| `tools/minicom_router.sh` | 456 | `0ba1d1d472a27764` | `~/.local/bin/serialwrap-minicom`（0755） |
| `tools/minicom-broker.sh` | 16 | `5be6ea08ea905c81` | `~/.local/bin/serialwrap-minicom-broker` |
| `tools/minicom-raw.sh` | 7 | `ccdc799eae3ffbec` | `~/.local/bin/serialwrap-minicom-raw` |
| `skill/SKILL.md` | 249 | `a34dad0cc745ca18` | `DATA_DIR/skill/`，`~/.agents/skills/serialwrap` → symlink；`serialwrap skill --platform linux` 輸出 |
| `skill/SKILL_WINDOWS.md` | 181 | `556f9f5bba82defd` | 同目錄；`serialwrap skill --platform windows`（無 frontmatter name） |

`default.yaml` 結構：`defaults: {log_dir: "~/b-log", max_sessions: 16}`；`profiles`：`prpl-template`（platform prpl，`bootloader_prompts: ["^=> $","^U-Boot> $"]`）、`op3-template`（shell，`user_env SW_OPI_U`/`pass_env SW_OPI_P`/`env_file OPI.env`）、`brcm-template`（bcm，`prompt_regex "(?m)^(?:[ \\t]*|.*[^>#\\s])[>#][ \\t]*$"`、`post_login_cmd sh`、`BRCM_USER/BRCM_PASS`、`env_file brcm.env`、`timeout_s 15`、`bootloader_prompts ["^CFE> $","^=> $","^BCM\\d+>> $"]`）、`smart4R-profile`（shell，`console_unlock_env SMART4R_CONSOLE_UNLOCK`、`console_unlock_idle_s 5`、`timeout_s 120`、`hard_timeout_s 240`、`bootloader_prompts ['(?m)^CFE>\s*$']`，uart 無 xonxoff 鍵）、`uboot-template`（passthrough）、`others-template`（passthrough，`prompt_regex ".*"`、`ready_probe ""`）；全部 uart 115200 8N1 none。`targets` 僅註解範例。（profile 語意由其他盤點負責。）

`minicom_router.sh`（`serialwrap-minicom`）重點：
- `SERIALWRAP_BIN` = `$SERIALWRAP_BIN` 或 `${BASE_DIR}/serialwrap`（BASE_DIR：腳本目錄若含可執行 `serialwrap`，否則上一層，否則腳本目錄）。`SW()`：`$SERIALWRAP_SOCKET` 非空才加 `--socket`。
- env 預設：`SERIALWRAP_AUTO_START_DAEMON=1`、`SERIALWRAP_ATTACH_WHEN_NOT_READY=1`、`SERIALWRAP_ATTACH_WAIT_TICKS=60`（×0.2s）、`MINICOM_WAIT_READY=0`、`MINICOM_WAIT_READY_TICKS=120`（×0.5s）、`SERIALWRAP_PREFERRED_COM=COM0`、`MINICOM_BIN=/usr/bin/minicom`、`MINICOM_DEFAULT_COLOR=on`、`MINICOM_AUTO_CAPTURE=1`、`BLOG_DIR=$HOME/b-log`、`MINICOM_CAPTURE_WRAPPER`、`MINICOM_CAPTURE_MODE`（`script|minicom|off`；未設時看 WRAPPER=1 → script，有設其他值 → minicom，預設 minicom；非法值 exit 2）。
- 參數：第一個非 `-` 開頭參數為 selector；`-D dev`／`-Ddev` 被抽出為 `user_device`，其餘透傳 minicom。
- `_ensure_daemon`：`session list` ok → 用之；`AUTO_START_DAEMON!=1` → 原樣；`serialwrap supervision-mode` ≠ `on-demand` → stderr 提示 systemctl、不啟動；否則 `serialwrap daemon start`、輪詢 30×0.1s。
- 選 session：無 selector → 第一個 READY，否則第一個 ATTACHED；都無且 ATTACH_WHEN_NOT_READY → `PREFERRED_COM` 或第一個 com，`session attach` 後等 READY/ATTACHED。有 selector（alias/com/session_id 比對）且非 READY/ATTACHED → attach 並等待。
- `_run_broker_minicom`：`session console-attach --selector <com> --label minicom:$$` → 取 `client_id`、`vtty`；trap EXIT/INT/TERM 呼叫 `session console-detach --client-id`；`_exec_minicom vtty com`：未帶 `-c` 時加 `--color=on`；auto capture 檔 `${BLOG_DIR}/mini_<safe_com>_<yymmdd-HHMMSS>.log`（safe_com：`tr -c 'A-Za-z0-9._+-' '_'`）；script 模式 `script -qef -c "<cmd>" <log>`，minicom 模式加 `-C <log>`。
- broker 不可用時 fallback：`user_device` → 直接 `minicom -D`（`DIRECT`）；`MINICOM_RAW_DEVICE` → 直接 minicom（`RAW`）；否則印 reprobe 診斷（`last_error ∈ {PROMPT_UNAVAILABLE, PROMPT_TIMEOUT, *_PROMPT_TIMEOUT, RX_FLOOD, TRANSPORT_STALL}` 或 `reprobe_attempts>0` 或 `reprobe_exhausted`）、`MINICOM_WAIT_READY=1` 時等 READY；最後印 `hint:` 兩行 exit 2。依賴 `jq`、`seq`、`date`、`tr`、`script`（可選）。`_wait_ready_row` 定義未使用。
- `minicom-broker.sh`：依序找 `${SCRIPT_DIR}/minicom_router.sh`、`../tools/minicom_router.sh`、`../minicom_router.sh` exec；都無 → stderr `minicom-broker: minicom_router.sh not found near …` exit 2（見 §13 O-08）。
- `minicom-raw.sh`：`MINICOM_RAW_DEVICE` 未設 → exit 2；否則 `exec /usr/bin/minicom -D "$MINICOM_RAW_DEVICE" "$@"`。

---

## 12. 本區 CLI 出口碼與錯誤碼彙整

| 命令 | 成功 | 失敗 |
|---|---|---|
| `doctor` | 0（恆 0，即使 ok:false） | — |
| `setup` | 0（含 needs_restart、applied:false） | 2（FLASHING_BUSY）；壞 config → traceback |
| `service <action>` | 0 | 2 |
| `supervision-mode` | 0（純文字） | traceback |
| `daemon start` | 0 | 2（所有 JSON 失敗）；foreground 回子行程 rc |
| `daemon stop` | 0 | 2（RPC/route 失敗）、1（endpoint 解析／INTERNAL_ERROR） |
| `remote …` | 0（status/close/open，含 starting） | 1 |
| `connect` / `benches` | 0 | 1（含 argparse 錯誤轉 INVALID_ARGS） |
| `skill` | 0 | — |
| `serialwrapd` | 0 | 2（lock）、3（StateLoadError） |

本區產生的 error_code／訊息碼：`DAEMON_ALREADY_RUNNING`（lock，stderr）、`SOCKET_UNAVAILABLE`（lock_posix，stderr `serialwrapd: SOCKET_UNAVAILABLE`）、`NEEDS_SUDO`、`NO_SYSTEMD`、`FLASHING_BUSY`、`REMOTE_NOT_SUPPORTED`、`INVALID_ARGS`、`INVALID_ENDPOINT`、`INVALID_TARGET`、`SSH_NOT_FOUND`、`TUNNEL_CONFLICT`、`TUNNEL_SPAWN_FAILED`、`REMOTE_BIND_UNVERIFIED`、`TUNNEL_STATUS_ERROR`、`TUNNEL_STILL_ACTIVE`、`UNKNOWN_BENCH_CODE`、`INVALID_BENCH_CONFIG`、`INVALID_BENCH_STATE`、`BENCH_STATE_IO_ERROR`、`BENCH_ENDPOINT_NOT_REMEMBERED`、`DAEMON_ALREADY_RUNNING_ELSEWHERE`、`DAEMON_BINARY_NOT_FOUND`、`DAEMON_EXITED`、`DAEMON_NOT_READY`、`ENV_FILE_SOURCE_FAILED`、`INTERNAL_ERROR`；stderr fallback 字串 `UNKNOWN_ERROR`（`_format_err_line`）。

stderr 一行格式（`_format_err_line`，cli.py:144-169）：`serialwrap: {context} failed: {error_code or message or "UNKNOWN_ERROR"}` + （有 code 且有 message）`: {message}` + （有 hint）`（hint: {hint}）`。stdout JSON 一律 `sort_keys=True, ensure_ascii=False, separators=(",",":")` + `\n`。

---

## 13. 規格與程式碼差異（Discrepancies）及觀察

### 規格 vs 實作差異（D）
- **D-01**（spec §6.1）：「設定發現順序為明確指定的 config／`SERIALWRAP_CONFIG`、使用者設定目錄、系統設定目錄」。實作無 `SERIALWRAP_CONFIG`，只有 `SERIALWRAP_CONFIG_DIR`／XDG；無系統設定目錄 fallback（system daemon 也讀「執行使用者」的 `~/.config/serialwrap/config.yaml`）。
- **D-02**（CLAUDE.md、README:2534、docs/func-test/realhw-stability-checklist.md:220、spec OD-11 引用）：宣稱設 `SERIALWRAP_BY_ID_DIR` sandbox 即可避免掃到其他裝置。實作同時掃 `DEVICE_BY_PATH_DIR`；須一併設 `SERIALWRAP_BY_PATH_DIR`（regression guards.py:105-131 已實證）。
- **D-03**（spec §4.2 #14）：`daemon` 子命令列 `restart`；實作只有 `start|stop|status`。
- **D-04**（spec §4.2 #16）：`supervision-mode`「查詢與切換、執行監管 reconcile」；實作唯讀、純文字輸出；切換只在 `setup`。
- **D-05**（spec §4.2 #17）：`remote` 子命令列 `stop`；實作為 `close <port|all>`（無參數預設 all），另有 `status`/無參數。
- **D-06**（spec §4.2 #19、§12.1）：`setup --rollback`；實作不存在，也無版本目錄／Current-Previous 機制。
- **D-07**（spec §4.2 #11/#12）：`benches`、`profile` 標為「純 RPC／設定查詢」。`benches` 為本機檔案＋remote registry，不連 daemon，Windows 回 `REMOTE_NOT_SUPPORTED`；`profile` 僅 `test` 離線子命令。
- **D-08**（spec §9.1 BC-05）：「移除 POSIX-only pipe 依賴」。現碼無 pipe；實際 POSIX-only 依賴為 §8.6 清單（fcntl、ControlMaster/ControlPath Unix socket、killpg/pgid、`/proc` stat、`os.kill(pid,0)` 在 Windows 為終止行程）。
- **D-09**（spec §6.1）：Windows 模式 `windows-task`／`windows-service` 尚未存在；`service_ctl` 對非 systemd 模式一律 `NO_SYSTEMD`；`setup` 只認三種模式；`RuntimeConfig` 不驗模式值。
- **D-10**（spec §6.2 Tier 1）：「Windows `LockFileEx`；路徑須正規化」。實作用 `msvcrt.locking(LK_NBLCK, 1)`、不正規化 lock 路徑（POSIX 亦然）；POSIX 另有 socket 探測及 `SOCKET_UNAVAILABLE`。
- **D-11**（spec §9.2 錯誤碼集合）：本區實際輸出但不在 71+6 清單的碼：`SOCKET_UNAVAILABLE`、`INVALID_TARGET`、`SSH_NOT_FOUND`、`TUNNEL_CONFLICT`、`TUNNEL_SPAWN_FAILED`、`REMOTE_BIND_UNVERIFIED`、`TUNNEL_STATUS_ERROR`、`UNKNOWN_BENCH_CODE`、`INVALID_BENCH_CONFIG`、`BENCH_ENDPOINT_NOT_REMEMBERED`（皆經 `TunnelError(code)`／`_EndpointResolutionError(code)`／`RuntimeError(code)` 傳遞，grep `"error_code": "…"` 抓不到；印證 spec「須包含常數傳遞路徑」的要求）。另 stderr 有 `UNKNOWN_ERROR` fallback 字串。
- **D-12**（spec §9.5 版本閘門）：「pinned CLI 與 daemon 版本不一致時整場拒跑」為測試 harness 層規則；CLI 本身只對 `_run_rpc` 路徑在 stderr 警告、不阻擋；`daemon start`/`setup`/doctor 等路徑不比對。
- **D-13**（spec §6.2 POSIX Tier 2 `flock`+`TIOCEXCL`）：sw_core 全碼無 `TIOCEXCL`／tty flock（BC-04 尚未實作，屬他區但影響 multi_open 定位）；目前多開防護只有 singleton lock + `/proc` 被動偵測 + CLI 預檢。

### 實作觀察／潛在缺陷（O，Rust 1:1 時須決定是否保留）
- **O-01**：system scope state 路徑不一致（推論）。`_state_path_for("systemd-system")` = `/var/lib/serialwrap/state.json`，但 system unit 以 `User=<安裝者>` 執行、未設 `SERIALWRAP_STATE_DIR`，systemd 會設 `HOME=~安裝者`，daemon 實際 `STATE_PATH` = `~/.local/state/serialwrap/state.json`；遷移把狀態複製到 daemon 從不讀的位置。`StateDirectory=serialwrap`/`ConfigurationDirectory=serialwrap` 只建目錄、未被引用。lock 亦落在 `~/.local/state/serialwrap/run/serialwrapd.lock`（system service 無 XDG_RUNTIME_DIR），與 user 模式（`$XDG_RUNTIME_DIR/serialwrap/`）不同 → 跨模式 singleton lock 不互斥（這正是需要 #101 偵測的原因）。
- **O-02**：POSIX `daemon start` 固定 spawn `[sys.executable, <site-packages>/../serialwrapd.py]`；pipx 安裝不含 `serialwrapd.py`（已核對 `~/.local/share/pipx/venvs/serialwrap/.../site-packages/` 無此檔）→ 推論 pipx 下 on-demand `daemon start` 會 `DAEMON_EXITED`。Windows 路徑有 `-m sw_core.daemon` fallback，POSIX 沒有。
- **O-03**：`SerialwrapService.start()` 先 `_watcher.start()`（thread 立刻 `poll_once` → `on_change` → `update_devices`）再於主執行緒 `poll_once()` + `prepare_dynamic_rank`；DeviceWatcher 無鎖 → 推論存在 thread 先觸發 `update_devices` 早於 rank 預配的競態（#100 想消除的 race 可能殘留）。
- **O-04**：`foreign_holders` 不排除 daemon 自身 pid → 推論正常單 daemon 時也會列出自己持有的 tty（名稱暗示「外部」）。holders 只掃 serialwrapd 行程，不會抓到 minicom 等外部持有者。
- **O-05**：`_find_conflicting_daemon` 與 `SingletonLock` 皆以原字串比對 socket；`--socket` 寫法不同（相對路徑、`unix://` 前綴、symlink）會誤判衝突／漏判。
- **O-06**：`ensure_wsl_systemd` 在 WSL 無 systemd 時，即使 `--on-demand`、未帶 `--with-sudo` 也會直接 `sudo install` 寫 `/etc/wsl.conf`（違反同檔「未帶 with_sudo 絕不靜默 sudo」註解），成功時 setup 早退不 reconcile。
- **O-07**：`reconcile` 的 `ran` 取 `getattr(fx,"calls",[])`，`SystemEffects` 無此屬性 → 生產輸出恆 `[]`；且所有 systemctl/loginctl 回傳碼被忽略，`applied:true` 不代表 unit 真的啟動。
- **O-08**：`serialwrap-minicom-broker`（物化後的 `minicom-broker.sh`）在 `~/.local/bin` 找 `minicom_router.sh`，但 router 被改名為 `serialwrap-minicom` → 推論物化版必定 exit 2。
- **O-09**：on-demand → systemd 轉換時 `_stop_old` 送 `serialwrap daemon stop`（RPC 立即回 `stopping:true`）後不等待 daemon 釋放 tty 就 `systemctl start` → 與「先停舊再起新」不變式之間存在短暫 two-reader 窗口（推論）。
- **O-10**：`remote` expose+tcp 的 `role_probe` 在第一次 `ss` 查不到時立即 `REMOTE_BIND_UNVERIFIED` 並拆除，不在 ready_timeout 內重試；ssh_check 雖先確認 master 可用，`-R` forward 是否已在 relay 端 listen 仍可能落後（推論，時序依 ExitOnForwardFailure 行為）。
- **O-11**：`remote close` 只在 `pid_alive` 時 killpg；pid 已死但同 pgid 仍有子行程（如 autossh 衍生的 ssh）時不會清掉。`status` 會 prune 死 state 但同樣不殺；orphan 的 `alive:true` 未經驗證。
- **O-12**：doctor `endpoint_reachable` 在 POSIX 對 tcp endpoint 一律判可連（`cli._endpoint_alive` 對 tcp 回 True）；`wal_dir`/`wal_writable` 不套用 #108 dangling fallback（與 `endpoint_reachable` 解析不同 seam）。
- **O-13**：`supervision-mode`、`service`、`setup` 以 `_default_runtime_config()`（不吞例外）讀 config，壞 YAML → Python traceback（與其他路徑的 `_safe_runtime_config` 不一致）。
- **O-14**：user unit 路徑固定 `home/.config/systemd/user/`（不看 `XDG_CONFIG_HOME`）；staging 固定 `home/.local/share/serialwrap/`（不看 `XDG_DATA_HOME`/`SERIALWRAP_DATA_DIR`）；`_user_dirs` 的 env 值不 expanduser。
- **O-15**：systemd unit 文字無尾端換行；`/etc/wsl.conf` 合併經 configparser 重寫（註解遺失、`key = value` 格式）。
- **O-16**：`WindowsDeviceSource` 的 `exclude_coms` 只在 daemon 啟動讀一次；doctor 每次重讀。
