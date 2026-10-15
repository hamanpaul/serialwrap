# 06 — UART I/O 與 Console 盤點（uart_io / serial_port / telnet_console / flash_endpoint / mcu_patterns）

> 來源 repo：`~/prj_pri/serialwrap-worktrees/rust-refactor`（HEAD `93553e6`，唯讀盤點）。
> 行號以該 HEAD 為準。對照規格：`docs/rust-refactor/serialwrap-rust-spec-final.md`（ADR-02/05/06、§4.1、§5.1–§5.4、§6.2、§7、§8.1、§9.1 BC-01～BC-05）。
> 本檔所稱「現況」= Python 實作實際行為，**不是**規格要求；差異集中於 §12。

---

## 0. 檔案與 Rust 承接位置

| Python 檔 | 行數 | 主要內容 | 規格 §4.1 承接 |
|---|---|---|---|
| `sw_core/uart_io.py` | 1481 | `ConsoleClient`、`PreservedConsoles`、`UARTBridge`（RX/TX/console/WAL/flash gate/suspend） | `sw-core::uart_bridge`＋PAL Console/Serial（2.1 RX/TX；2.2 完整 Human Console） |
| `sw_core/serial_port.py` | 394 | `SerialPort` ABC、`_PosixSerialPort`、`_PySerialPort`、`open_serial_port` | `sw-pal::serial`（2.1 必備：獨占、部分完成、取消、關閉） |
| `sw_core/telnet_console.py` | 139 | Telnet 常數、`TELNET_GREETING`、`escape_iac`、`TelnetFilter` | `sw-core::telnet_console`（2.2） |
| `sw_core/flash_endpoint.py` | 295 | `ProbeTransport`、`DetectResult`、`detect_mcu_line`、`FlashEndpoint`、`pump_endpoint_to_sink`、`make_rx_to_endpoint_writer`、`resolve_flash_target` | `sw-core::flash_endpoint`＋PAL（2.4） |
| `sw_core/mcu_patterns.py` | 117 | `McuPattern`、`_DEFAULTS`、`McuPatternRegistry` | 同上（2.4） |

flash 的實際接線（sync-probe transport、holder-probe、baud 鏡射呼叫、RX→endpoint）不在上述檔案，而在 `sw_core/service.py:146-232`（`_BridgeProbe`）、`service.py:273-284`（構造）、`service.py:363-499`（`_flash_rx_observer` / `_on_flash_open` / `_watch_flasher_disconnect` / `_flash_candidates`）、`service.py:711-723`（`mcu.patterns` / `mcu.status` RPC）與 `sw_core/session_manager.py:1892-1952`（`collect_flash_candidates_and_mark` / `enter_flashing` / `exit_flashing`）、`session_manager.py:1954-2001`（`_probe_external_holder`）。本檔一併盤點。

---

## 1. 相關常數（精確值）

| 名稱 | 值 | 定義位置 | 用途 |
|---|---|---|---|
| `_STALE_CONSOLE_GRACE_S` | `2.0`（秒，`time.time()` 基準） | `uart_io.py:26` | console 建立後多久才可被 prune/reap |
| `_RX_WINDOW_MAX_ENTRIES` | `4096` | `uart_io.py:30` | RX 速率視窗 deque `maxlen`（兜底） |
| `_rx_max_chars`（實例屬性） | `131072`（字元，非 bytes） | `uart_io.py:142` | RX 文字視窗上限 |
| `DEFERRED_INPUT_MAX_BYTES` | `256 * 1024` = `262144` | `constants.py:202` | 每 client deferred buffer 上限（超過丟最舊） |
| `RX_RATE_WINDOW_S` | `10.0` | `constants.py:91` | `rx_stats()` 視窗 |
| `RX_FLOOD_BYTES_PER_10S` | `20_000` | `constants.py:93` | 洪水判定（使用端在 session_manager） |
| `HUMAN_ACTIVE_WINDOW_S` | `60.0` | `constants.py:111` | `last_human_input_at` 的 human-active 判定窗（session_manager 用） |
| `_HUMAN_PEER_GRACE_S` | `3.0` | `constants.py:113` | reconcile tick 中 console reap / 自癒節流週期 |
| `_ATTACHED_CONSOLE_LEASE_TIMEOUT_S` | `86400.0` | `session_manager.py:65` | console-attach 自動開的 human lease timeout 下限 |
| `TTYMCU_PATH` | `$SERIALWRAP_TTYMCU_PATH` 或 `os.path.join(RUN_DIR, "dev", "ttyMCU")` | `constants.py:58` | flash 端點 symlink |
| `_PySerialPort._READ_TIMEOUT_S` | `0.2` | `serial_port.py:227` | pyserial read timeout（亦為 Windows reader 輪詢節奏） |
| `_PySerialPort._WRITE_TIMEOUT_S` | `5.0` | `serial_port.py:228` | pyserial write_timeout |
| `_BAUD_MAP` | `{9600,19200,38400,57600,115200,230400,460800,921600}` → `termios.B*` | `serial_port.py:36-45` | POSIX 支援 baud；未列者 configure 退回 `B115200`、set_baud 直接 no-op |
| select timeout（serial reader） | `0.2` | `uart_io.py:893` | POSIX `_loop` |
| select timeout（console loop） | `0.2` | `uart_io.py:419` | Windows `_console_loop` |
| read chunk | `8192` | `uart_io.py:874, 900`, `uart_io.py:439` | serial/PTY/socket 每次讀上限 |
| EAGAIN 退避 | `time.sleep(0.01)` | `uart_io.py:995,999`；`serial_port.py:173,177` | TX 部分寫入重試，**無總 timeout** |
| TCP listener backlog | `8` | `uart_io.py:347` | Windows console |
| reader/console join timeout | `2.0` | `uart_io.py:291, 295` | `stop()` |
| `FlashEndpoint.grace` | 預設 `0.3` | `flash_endpoint.py:107` | 端點 select timeout |
| 端點首讀 | `4096` | `flash_endpoint.py:182` | flasher 首段（作為 sync probe） |
| `pump_endpoint_to_sink` chunk / select | `4096` / `0.2` | `flash_endpoint.py:211, 232` | |
| `_flash_rx_buffers` 上限 | `4096`（`del buf[:-4096]`） | `service.py:373-374` | probe 用 RX 緩衝 |
| `_BridgeProbe` poll | `0.02` | `service.py:200` | 等 ACK |
| `_watch_flasher_disconnect` | `poll_s=0.5`、`max_s=1800.0` | `service.py:469-470` | holder-probe |
| McuPattern 預設 | `baud=115200`、`timeout_ms=500` | `mcu_patterns.py:49-50` | `from_dict` 預設 |

---

## 2. 資料型別

### 2.1 `ConsoleClient`（`uart_io.py:41-64`，`@dataclasses.dataclass`，可變）

| 欄位 | 型別 | 預設 | 說明 |
|---|---|---|---|
| `client_id` | `str` | — | `uuid.uuid4().hex[:12]`（12 hex） |
| `label` | `str` | — | PTY：`(label or client_id).strip() or client_id`；TCP：`"<peer_ip>:<peer_port>"` |
| `master_fd` | `int` | — | PTY master（nonblock）；TCP 為 `-1` |
| `slave_fd` | `int` | — | PTY slave（**daemon 持有**）；TCP 為 `-1` |
| `slave_path` | `str` | — | PTY：`os.ttyname(slave_fd)`（`/dev/pts/N`）；TCP：`self._console_endpoint or peer`（`"127.0.0.1:<port>"`） |
| `attached_at` | `float` | — | `time.time()`（wall clock，非 monotonic） |
| `tx_buffer` | `bytearray` | `bytearray()` | line-mode 行緩衝 |
| `sock` | `Any` | `None` | TCP console socket；PTY 為 None |
| `internal` | `bool` | `False` | 哨兵 primary（start() 建立）為 True，永不被 reaper 回收 |
| `telnet` | `TelnetFilter \| None` | `None` | TCP accept 時掛 `TelnetFilter()`；PTY 恆 None |
| `sock_send_lock` | `threading.Lock` | 新 Lock | 每 client socket 送出序列化 |

### 2.2 `PreservedConsoles`（`uart_io.py:66-78`）
- 欄位：`clients: dict[str, ConsoleClient]`、`primary_client_id: str | None`。
- `primary_vtty() -> str | None`：primary 的 `slave_path`。
- `has_client(client_id) -> bool`。
- 用途：bridge 重建（recovery/re-attach）時把 console（PTY fd 或 socket）原封交接給新 bridge，使 `/dev/pts/N` 路徑在重連後不變（session_manager `retained_consoles`，`session_manager.py:2612`；`bridge.stop(preserve_consoles=True)` 於 `2653, 2692, 2738`）。

### 2.3 `UARTBridge.__init__`（`uart_io.py:116-190`）
簽章：
```python
UARTBridge(com: str, device_path: str, profile: UartProfile, wal: WalWriter, *,
           on_console_line: Callable[[str, str], None] | None = None,   # (client_id, line)
           on_rx_data: Callable[[bytes], None] | None = None,          # (data)
           on_bridge_down: Callable[[str], None] | None = None,        # (reason)
           preserved_consoles: PreservedConsoles | None = None)
```
`UartProfile`（`config.py:11-18`，frozen）：`baud=115200, data_bits=8, parity="N", stop_bits=1, flow_control="none", xonxoff=False`。

實例狀態（全部）：

| 屬性 | 初值 | 保護鎖 | 語意 |
|---|---|---|---|
| `_serial: SerialPort \| None` | None | `_state_lock` | 開啟中的 port |
| `_serial_fd: int \| None` | None | `_state_lock` | POSIX = `port.fileno()`；Windows 恆 None；**必須與 `_serial` 同鎖成對設定/清除**（`_loop` 以此分 POSIX/Windows 路徑） |
| `_primary_client_id: str \| None` | None | `_state_lock` | vtty 錨點 |
| `_stop_event` | `threading.Event()` | — | |
| `_thread` | None | — | `serialwrap-uart-{com}` reader |
| `_write_lock` | `threading.Lock()` | — | 單寫入者鎖 |
| `_state_lock` | `threading.RLock()` | — | 狀態鎖（可重入） |
| `_console_raw_token` | `object()` | — | raw console 送出的身分 token（不可偽造） |
| `_console_raw_context` | `threading.local()` | — | 攜帶 `token`、`client_id` 給 `send_bytes` |
| `_rx_lock` | `threading.Lock()` | — | RX 文字/統計鎖 |
| `_rx_text: str` | `""` | `_rx_lock` | RX 文字視窗（utf-8 replace 解碼） |
| `_rx_max_chars` | `131072` | — | |
| `_rx_trimmed: int` | 0 | `_rx_lock` | 已修剪/清除的累計字元數（絕對偏移基準，#158） |
| `_rx_window: deque[(mono, len)]` | `maxlen=4096` | `_rx_lock` | 速率視窗（raw bytes） |
| `_rx_total_bytes: int` | 0 | `_rx_lock` | raw RX 累計（單調，clear 不歸零，#150） |
| `_last_rx_mono` | `time.monotonic()`（建構時） | `_rx_lock` | |
| `_preserved_consoles` | 參數 | `_state_lock` | start() 消費後置 None |
| `_clients: dict[str, ConsoleClient]` | `{}` | `_state_lock` | |
| `_interactive_owner: str \| None` | None | `_state_lock` | `"human:<cid>"` 或 agent owner 字串 |
| `_interactive_admission_blocked: bool` | False | `_state_lock` | foreground operation admission gate |
| `_pending_interactive_owner: str \| None` | None | `_state_lock` | gate 關閉期間待提升的 owner |
| `_agent_active: bool` | False | `_state_lock` | suspend 期間 True |
| `_suspended_owner: str \| None` | None | `_state_lock` | 最外層 suspend 保存的 owner |
| `_suspend_depth: int` | 0 | `_state_lock` | 可重入計數（#78） |
| `_deferred_buffers: dict[str, bytearray]` | `{}` | `_state_lock` | key = client_id |
| `_last_human_input_at: float \| None` | None | `_state_lock` | monotonic；**僅** human owner raw 直送分支更新（#53） |
| `_flash_mode: bool` | False | `_write_lock`+`_state_lock` | FLASHING gate |
| `_console_listener` / `_console_thread` / `_console_endpoint` | None | `_state_lock` | Windows TCP console |

### 2.4 鎖序不變式（`uart_io.py:96-114` docstring）
- 固定 `_write_lock ⊃ _state_lock`：永遠先取 `_write_lock` 再取 `_state_lock`；全檔僅 `send_bytes`（`946-947`）與 `set_flash_mode`（`202-203`）巢狀。
- 持 `_state_lock` 時不得呼叫 `send_bytes`（resume flush、admission flush 都先釋放 state lock）。
- `_state_lock → sock_send_lock` 單向（RX fan-out 持 state lock 呼叫 `_console_sock_send`）。
- `_rx_lock` 獨立，不與其他鎖巢狀（`snapshot()` 先取 `_rx_lock` 再分開取 `_state_lock`，非巢狀）。

---

## 3. 執行緒模型

| 平台 | 執行緒 | 名稱 | 工作 |
|---|---|---|---|
| POSIX | 單一 reader | `serialwrap-uart-{com}`（`uart_io.py:285`） | `select([serial_fd, *master_fds], [], [], 0.2)` 多工 serial fd 與所有 console PTY master |
| Windows | reader | 同上 | `port.read(8192)` 阻塞（pyserial timeout 0.2s） |
| Windows | console | `serialwrap-console-{com}`（`uart_io.py:353`） | `select([listener, *socks], [], [], 0.2)`；accept + recv(8192) |
| POSIX | flash 端點 | `serialwrap-ttyMCU`（`flash_endpoint.py:145`） | 端點 select；**flash pump 也在這條執行緒內同步執行**（`_on_flash_open` 為同步 callback） |
| POSIX | holder 監看 | 無名 daemon thread（`service.py:445`） | `_watch_flasher_disconnect` |
| 任一 | bridge-down 通知 | 無名 daemon thread（`uart_io.py:933`） | `on_bridge_down(failure_reason)` |

### 3.1 `_loop()`（`uart_io.py:859-933`）
每輪：
1. 持 `_state_lock` 快照 `port`、`serial_fd`、`clients_by_fd = {client.master_fd: client}`（注意：TCP client 的 master_fd=-1 也會進 dict，但 Windows 不走 select 分支）。
2. `port is None` → break（stop 已清）。
3. Windows 分支（`serial_fd is None`）：`port.read(8192)`；例外 → `failure_reason = f"SERIAL_READ:{type(exc).__name__}"`、`_stop_event.set()`、break；`b""` → continue；否則 `_handle_serial_rx`（例外只 log warning，reader 續行，#79）。
4. POSIX 分支：`select(read_fds, [], [], 0.2)`；`OSError` → `failure_reason = f"SELECT:{exc.errno or type(exc).__name__}"`、break（**未** set stop_event）。
5. 對每個可讀 fd：`os.read(fd, 8192)`：
   - `BlockingIOError` → 略過。
   - 其他 `OSError`：serial fd → `failure_reason = f"SERIAL_READ:{errno or type}"`、set stop、**break 出 for**（之後 while 檢查 stop 結束）；console fd → `_drop_console_client`。
   - `b""`（EOF）：serial fd → **只 continue，不視為斷線**；console fd → drop。
   - 資料：serial → `_handle_serial_rx`；console → `_handle_console_rx`。例外記 warning、續行。
6. 結束時若 `failure_reason` 且有 `on_bridge_down` → 開新 thread 呼叫（非同步）。

failure_reason 字串集合（對外經 session_manager `_handle_bridge_down`）：`SERIAL_READ:<errno int>`、`SERIAL_READ:<ExcClassName>`、`SELECT:<errno int>`、`SELECT:<ExcClassName>`。

---

## 4. RX 路徑與 fan-out 順序

### 4.1 `_handle_serial_rx(data)`（`uart_io.py:681-696`）— 嚴格順序
1. **`self.wal.append(com=self.com, direction="RX", source="device", payload=data)`**（回傳 record，含 seq，但**被丟棄**）。
2. `_append_rx_text(data)`（`659-673`）：持 `_rx_lock`：`_rx_total_bytes += len`、`_last_rx_mono = now`、`_rx_window.append((now, len))`、剪除 > `RX_RATE_WINDOW_S` 的舊項、`_rx_text += data.decode("utf-8", errors="replace")`；超過 `_rx_max_chars` → 修頭並 `_rx_trimmed += overflow`。
   - 注意：每個 chunk 獨立 decode，跨 chunk 的 UTF-8 多位元組字元會變成 U+FFFD（現況）。
3. `self._on_rx_data(data)`（若有）→ `SessionManager._on_bridge_rx(sid, data)`（`session_manager.py:2385-2440`）：
   - 先在 manager `_lock` 內讀 `wal_seq = self._wal.current_seq`，鎖外依序呼叫所有 RX observers `obs(com, data, wal_seq)`（例外吞掉）。註冊順序：`_flash_rx_observer`（`service.py:280`，建構時）→ `_engine_rx_observer`（`service.py:513`，start 時）。
   - 再做 `clean_text` 後的 boot quiet、background capture、agent log capture（foreground_busy 時跳過 agent log）。
4. **console fan-out**：持 `_state_lock`，對 `list(self._clients.values())` 每個 client `_console_send(client, data)`，`OSError` 忽略（#83 RACE-1：持鎖迭代避免寫到已關閉/被重用的 fd）。原始 bytes 不做任何轉換（PTY）；telnet client 經 `escape_iac`。

=> 現況滿足規格 §8.1 RX「先 WAL append，再提供給 Console/EventEngine/Login FSM」。但：
- `WalWriter.append` 每筆 `open(..., "a")` 寫入後關檔（`wal.py:179-185`），無 fsync；失敗 → 目錄自癒重試一次，再失敗 → `loss_flag=True`、不拋例外（`wal.py:160-174`）。
- 若 `wal.append` 拋出未預期例外（非 OSError），整個 chunk 的 rx_text/observer/fan-out 被跳過（被 `_loop` 吞掉），資料不會到 console（現況缺口）。
- observer 收到的 `wal_seq` 是事後讀的 `current_seq`，在多 COM／TX 併發時**不保證**等於該 chunk 的 seq（`_handle_serial_rx` 未傳 record.seq）。

### 4.2 RX 查詢 API（全部持 `_rx_lock`）
| 方法 | 行 | 行為 |
|---|---|---|
| `clear_rx_buffer()` | 1151 | `_rx_trimmed += len(_rx_text)`；`_rx_text = ""`（不歸零 total/window/last_rx） |
| `wait_for_regex(pattern, timeout_s) -> bool` | 1158 | 每 0.05s 對全窗 `re.search` |
| `rx_snapshot_len() -> int` | 1169 | `_rx_trimmed + len(_rx_text)`（絕對偏移，字元） |
| `rx_stats() -> dict` | 1177 | `{"rx_bytes_last_10s": total, "rx_rate_bps": int(total / 10.0)}` |
| `rx_total_bytes() -> int` | 1194 | |
| `rx_idle_s() -> float` | 1203 | `max(0, monotonic - _last_rx_mono)` |
| `rx_text_from(from_offset) -> str` | 1208 | `rel = from_offset - _rx_trimmed`；`rel<=0` 回全窗（降級） |
| `rx_tail(max_chars=4096) -> str` | 1214 | |
| `wait_for_regex_from(pattern, from_offset, timeout_s) -> bool` | 1218 | 每輪重算 rel，poll 0.05s |

---

## 5. TX 路徑

### 5.1 `send_bytes(payload, *, source, cmd_id=None, log=True, _allow_during_flash=False)`（`uart_io.py:935-986`）
全程持 `_write_lock`：
1. 持 `_state_lock`：
   - 讀 thread-local `raw_context.token`。
   - **flash gate**：`_flash_mode and not _allow_during_flash` → **靜默 return**（不寫、不記 WAL、不拋錯）。授權綁內部參數，不綁 `source` 字串（#69 round3）。
   - **raw-console admission 重檢**：`raw_token is self._console_raw_token and _interactive_admission_blocked` → 把 payload append 到 `_deferred_buffers[raw_context.client_id]`（超過上限截頭），return（不寫、不記 WAL）。
   - 快照 `port`、`serial_fd`。
2. 鎖外（仍持 `_write_lock`）：POSIX `self._write_all(serial_fd, payload)`；Windows `port.write(payload)`；兩者皆 None → `RuntimeError("serial not ready")`。
3. **釋放 `_write_lock` 之後**：`if log: self.wal.append(com, direction="TX", source=source, payload=payload, cmd_id=cmd_id)`。

### 5.2 `_write_all(fd, payload)`（`uart_io.py:988-1004`）
- memoryview 迴圈 `os.write(fd, view[sent:])`。
- `BlockingIOError` / `EAGAIN` / `EWOULDBLOCK` → `sleep(0.01)` 後重試，**無上限、無 timeout、不檢查 stop/epoch**。
- 其他 `OSError` → 上拋（呼叫端拿到例外，**WAL 不記錄任何 TX，即使已寫出部分 bytes**）。
- `n <= 0` → break（**部分寫入被當作完成，WAL 仍記整段 payload**）。
- `_PosixSerialPort.write`（`serial_port.py:163-182`）為完全相同邏輯的重複實作；POSIX 熱路徑實際用 `UARTBridge._write_all`，不經 `port.write`。
- 不做 `tcdrain`（write 回傳僅代表進入 kernel tty buffer）。

### 5.3 TX 包裝方法
| 方法 | 行 | payload | source | WAL |
|---|---|---|---|---|
| `send_command(cmd, *, source, cmd_id=None)` | 1049 | `cmd.encode("utf-8","replace")`，不以 `\n` 結尾則補 `b"\n"`（**只補 LF**） | 呼叫端 | 一筆 TX |
| `send_secret(secret)` | 1145 | 同上補 `\n` | `"system:secret"` | **不記（log=False）** |
| `cancel_input_line(*, source)` | 1136 | `b"\x15\n"`（Ctrl-U + LF） | 呼叫端 | 一筆 TX |
| `flash_tx(payload)` | 1006 | 原樣 | `"flash"` | 一筆 TX/每次呼叫；`_allow_during_flash=True` |
| `send_command_echo_paced(cmd, *, source, cmd_id=None, slice_size=64, echo_timeout_s=2.0) -> {"ok","acked_chars","sent_chars"}` | 1055-1105 | `body = cmd.rstrip("\r\n")`；`slice_size=max(1,…)`；逐 slice（以 **str 字元**切）`send_bytes(..., log=False)`，每段後 `_await_echo_progress`；全確認後送 `b"\n"`（log=False） | 呼叫端 | 成功：一筆 `sent_payload + b"\n"`；停滯：一筆實際已送 slices（無 `\n`），回 `ok=False` |
| `_await_echo_progress(expected_cumulative, from_offset, timeout_s) -> int` | 1107-1134 | `strip_ansi(rx_text_from(off))` 去 `\r`、`\n` 後以移動起點 `find` 依序比對；先比後 sleep 0.01；逾時回已確認累計字元數 | | |

### 5.4 TX 來源字串（現況使用者）
`"system"`（login_fsm）、`"system:secret"`、`"flash"`、`"human:<client_id>"`（raw 直送、deferred flush）、agent 提交的任意 `source`（arbiter 經 session_manager）、interactive lease owner 字串、`"system..."`（`session_manager.py:3879, 4533` probe）。

### 5.5 TX WAL 排序現況
- WAL TX 記錄在 `_write_lock` **釋放後**才 append → 兩個並發寫入者的 WAL TX 順序可能與實際 UART 寫入順序相反；device echo 的 RX 記錄可能早於其 TX 記錄。
- 寫入例外不記任何 TX；部分寫入 (`n<=0` break) 記全長。
- 秘密不記 TX；echo-paced 的多段只記一筆聚合。
- flash gate / admission defer 的 drop/defer 無 WAL 痕跡。

---

## 6. Human console：ownership、raw/line、suspend/resume、deferred

### 6.1 Owner 字串
- human：`f"human:{client_id}"`；agent interactive lease：任意非 `human:` 字串（`_promote_pending_interactive_owner_locked` 對非 human pending 直接提升，`uart_io.py:1323-1326`）。

### 6.2 授予 raw ownership 的入口
| 入口 | 行 | 條件 |
|---|---|---|
| `set_interactive_owner(owner)` | 1345-1358 | human owner 且 `_interactive_admission_blocked` → 只記 `_pending_interactive_owner` 並 return；否則直接設；`owner is None` 時同時清 pending |
| `try_grant_interactive_if_idle(owner) -> bool` | 1464-1481 | 原子：owner/pending/suspended 皆 None、`not admission_blocked`、`not agent_active`、`not flash_mode` 才設 |
| TCP accept（Windows） | 393-410 | `owner is None and suspend_depth == 0`：admission blocked → 設 pending（若尚無）；否則設 owner。`suspend_depth > 0 and suspended_owner is None` → 設 `_suspended_owner`（#134） |
| `_promote_pending_interactive_owner_locked()` | 1310-1329 | admission 開、無 owner、depth 0、無 suspended 才提升；human pending 須 client 仍存在；之後清 pending |
| POSIX console-attach | session_manager `attach_console`（`session_manager.py:3947-3976`） | bridge.attach_console 後由 session layer `_open_interactive_locked(owner="human:<cid>", timeout_s=max(hard_timeout_s, 86400))` → 內部呼 `bridge.set_interactive_owner`；回應附 `interactive_session_id`、`interactive_owner`（bool，= 非 foreground_busy 且無 foreground op） |

結果：第一個 console 取得 raw；第二個以後走 line-buffer（因 owner 已被佔）。

### 6.3 `_handle_console_rx(client, data)`（`uart_io.py:752-857`）決策順序
1. **Telnet 入向過濾**（僅 `client.telnet` 非 None）：`data, reply = telnet.feed(data)`；reply 以 `_console_sock_send`（不逸出）回送、OSError 忽略；`data` 空 → return。**先於 flash gate**（協商回覆 flash 期間照送）。
2. 持 `_state_lock`：`_flash_mode` → **丟棄**（return，無 echo、無記錄）。快照 `owner`、`admission_blocked`、`agent_active`、`suspended`。
3. **owner == `human:<this cid>`**：再持 `_state_lock` 重檢：
   - 仍是 owner 且 gate 開 → `_last_human_input_at = monotonic()`、`send_raw=True`。
   - 仍是 owner 但 gate 關 → append 到 `_deferred_buffers[cid]`（截頭至上限）並 return。
   - owner 已變 → 用新快照往下走。
   - `send_raw`：設定 thread-local `raw_context.token = _console_raw_token`、`client_id`，呼 `send_bytes(data, source=owner, cmd_id=None)`，finally 還原/刪除 thread-local；return。
4. **`agent_active and suspended == human:<cid>`** → 持 `_state_lock` append 到 deferred（截頭至 `DEFERRED_INPUT_MAX_BYTES`），return。
5. **line-buffer**：`lines, echo = _consume_console_input(client, data)`；echo 非空 → `_console_send(client, echo)`（OSError 忽略）；每行 `on_console_line(client.client_id, line)` → `SessionManager._on_bridge_console_line` → `service._on_console_line`（`service.py:313-322`）→ `arbiter.submit(session_id, command=line, source=f"human:{client_id}", mode=_human_console_mode(line), timeout_s=30.0, priority=100)`。
   - `_human_console_mode`（`service.py:89-142`）：shlex 拆字，跳過 `--`、`VAR=val`、`sudo`（含 `-u -g -h -p -C -T -r -t` 帶值選項）、`env`、`command/builtin/exec`，basename 屬 `_HUMAN_INTERACTIVE_COMMANDS`（`alsamixer btop htop less menuconfig more most nano nmtui screen tig tmux top vi view vim vimdiff watch`）→ `"interactive"`，否則 `"line"`。
   - 注意：非 owner 的第二 console 在 agent 執行中仍走 line-buffer（line 送進 arbiter 排隊），不 defer。

### 6.4 Line 編輯 `_consume_console_input`（`uart_io.py:721-750`）
逐 byte：
- `0x08`/`0x7F`：`last_terminator=None`；`_pop_last_console_char(tx_buffer)`（`712-719`：從尾往前跳過 `0b10xxxxxx` continuation bytes，刪一個 UTF-8 字元）成功才 echo `b"\b \b"`。
- `0x0A`/`0x0D`：若 `last_terminator` 非 None、與本 byte 不同、且 buffer 空 → 吞（CRLF / LFCR 合併）並重設 terminator；否則 emit `tx_buffer.decode("utf-8","replace")`、清 buffer、echo `b"\r"`（不送 LF，避免多空行）、記 terminator。
  - `last_terminator` 是**區域變數**，每次呼叫重設 → CR 與 LF 分在兩個 read chunk 時會多 emit 一個空行（現況行為）。
  - `CR CR` / `LF LF` → 第二個產生空行（空字串 line 也會 submit 給 arbiter）。
- 其他 byte：append 到 buffer；只有 `0x09`、`0x20-0x7E`、`>=0x80` 會 echo（控制字元如 0x03/ESC 進 buffer 但不回顯）。
- 本地回顯只送給輸入的那個 client。
- `_drain_line_buffer`（`698-710`）為**無呼叫者的死碼**。

### 6.5 suspend/resume（`uart_io.py:1360-1400`）
- `suspend_interactive()`：持 state lock；`depth == 0` 時 `_suspended_owner = _interactive_owner`、`_interactive_owner = None`、`_agent_active = True`；`depth += 1`。
- `resume_interactive()`：depth 0 → no-op；`depth -= 1`；>0 → return；歸 0 時 `_interactive_owner = _suspended_owner`、`_agent_active=False`、`_suspended_owner=None`、`_promote_pending…()`、收集全部非空 deferred 為 `[(f"human:{cid}", bytes(buf))]` 並 `clear()`；**鎖外**逐一 `send_bytes(data, source=..., cmd_id=None)`（WAL 一筆/每 client；無 raw token，故不受 admission gate 再攔）。
- 呼叫者（session_manager）：`execute_command`（`3689/3701`，`suspend_human_interactive` 時）、interactive lease soft-preempt（`4051`, `4147`）、self-test（`4529`）、`_PostCloseAction.execute`（`293-295`，resume 後可 `set_interactive_owner(None)`）。

### 6.6 Foreground admission gate（`_set_interactive_admission`，`uart_io.py:1331-1343`）
- `allowed=False` → `_interactive_admission_blocked=True`。
- `allowed=True` → 清旗標、promote pending；若 `depth==0 and suspended is None` → 回傳並清空全部 deferred `[(human:cid, bytes)]`（由呼叫端鎖外 replay）。
- 呼叫端：`SessionManager._set_bridge_interactive_admission`（`session_manager.py:1036-1045`，getattr seam）；`_finish_foreground_operation`（`1084-1120`）在 manager lock 內取 flush_data，鎖外 `bridge.send_bytes(payload, source=source, cmd_id=None)` replay，最後清 `_foreground_operation`/`foreground_busy`。
- 時序：`execute_command` finally 先 `resume_interactive()`（此時 gate 仍關，但 resume flush 不帶 raw token → 直接寫出 suspend 期間的 deferred），再 `_finish_foreground_operation` 開 gate 並 replay「gate 關閉期間」累積者。

### 6.7 Deferred buffer 溢位（現況）
三處 append（`uart_io.py:791-797`、`842-850`、`964-972`）皆：`if len(buf) > DEFERRED_INPUT_MAX_BYTES: del buf[:len(buf)-MAX]` → **丟最舊、保留最新 262144 bytes**；無 WAL/診斷計數。`tests/test_bounded_memory.py:226-240`（`test_deferred_buffer_capped`）斷言此行為（「保留最近輸入」）。
清除時機：`stop()` 全清；`detach_console`/`_drop_console_client`/`reap_stale_consoles` 刪該 client 的 buffer；`_prune_stale_consoles_locked` **不刪**（殘留 key，下次 flush 送已不存在 client 的輸入）；resume / admission-open flush 後清。flash 開啟時**不清** deferred（flash 期間新輸入被丟棄，但既有 deferred 在下一次 resume/gate-open 會被回放）。

### 6.8 `_last_human_input_at`
只在 §6.3 step 3 raw 直送前更新；snapshot 輸出；session_manager `_human_active_locked`（`1361-1376`）以 `HUMAN_ACTIVE_WINDOW_S=60.0` 判斷 soft-preempt。

---

## 7. Console 管理

### 7.1 PTY 建立（POSIX）`_create_console_client(label=None)`（`uart_io.py:232-248`）
- `_pty_available()`（`33-38`）= `hasattr(os, "openpty")`；否則 `RuntimeError("human console（PTY）在此平台不支援；序列埠 RX/TX 仍可用")`。
- `os.openpty()` → master 設 `O_NONBLOCK`（`fcntl F_SETFL`）；slave 套 `_configure_pty_slave`（`220-230`）：`iflag=0, oflag=0, cflag=CREAD|CLOCAL|CS8, lflag=0, VMIN=1, VTIME=0`，`TCSANOW`；不設 speed。
- **daemon 同時保留 master 與 slave fd**（keepalive：外部 minicom 關閉時 master 不會 EOF/EIO，console 存活靠 /proc 掃描判斷）。
- **無 symlink**：console vtty 直接是 `/dev/pts/N`（`os.ttyname`）。穩定性來源是 `PreservedConsoles` 跨 bridge 重建保留同一 PTY；只有 `/dev/ttyMCU` 有 symlink（§9）。
- `start()` 時若無 preserved → 建立 `label="primary"`、`internal=True` 的哨兵 primary console（POSIX）；PROBE bridge（`session_manager.py:2953, 3013`，com=`"PROBE"`）也會建一個。

### 7.2 `start()`（`uart_io.py:250-286`）
已有活 thread → return。`open_serial_port(device_path, profile)` → `port.open()` → `port.configure(profile)`（失敗例外直接上拋）。持 state lock：消費 preserved（primary 不在 clients 內時取第一個）；否則 POSIX 建哨兵 primary、Windows 空。設 `_serial/_serial_fd/_clients/_primary_client_id`。`_stop_event.clear()`；Windows `_start_console_listener()`；起 reader thread。

### 7.3 `stop(*, preserve_consoles=False) -> PreservedConsoles | None`（`uart_io.py:288-332`）
1. `_stop_event.set()`；join reader（2.0s，非本執行緒）與 console thread（2.0s）。**join 逾時仍繼續**。
2. 持 state lock：preserve 且有 clients → 回傳 `PreservedConsoles(dict(clients), primary)`、不關；否則全部待關。清 `_serial/_serial_fd/_clients/_primary_client_id/_interactive_owner/_interactive_admission_blocked/_pending_interactive_owner/_suspended_owner/_agent_active/_suspend_depth`、`_deferred_buffers.clear()`、listener/thread/endpoint。**不清 `_flash_mode`**、不取 `_write_lock`。
3. 鎖外 `port.close()`、`listener.close()`、關閉 client fds。
- 無 drain、無 in-flight 追蹤、不等待持 `_write_lock` 中的寫入者 → 寫入者可能對已 close（或被重用）的 fd `os.write`。

### 7.4 attach/detach/list
- `attach_console(*, label=None) -> dict`（`1231-1257`）：
  - Windows：`{"client_id": None, "label": label, "vtty": endpoint, "endpoint": endpoint, "transport": "tcp", "protocol": "telnet"}`（不建 client）。
  - POSIX：建 client；持鎖 `_prune_stale_consoles_locked(now=client.attached_at)`、註冊、若無 primary 設為 primary；鎖外關 stale；回 `{"client_id", "label", "vtty"}`。
- `detach_console(client_id) -> bool`（`1259-1279`）：pop；primary 轉移給第一個剩餘；清 owner/pending；若為 suspended owner → `_suspended_owner=None`、**`_agent_active=False`、`_suspend_depth=0`**（放棄整個 suspend 簿記，#78）；刪 deferred；關 fd。
- `_drop_console_client(client_id)`（`503-524`，I/O 錯誤/EOF 路徑）：同上但 suspended owner 只清 `_suspended_owner`，**不**歸零 `_agent_active/_suspend_depth`（#136）。
- `list_consoles() -> list[dict]`（`1281-1297`）：先 prune stale（副作用），回傳列依 `(label, client_id)` 排序：

  | 欄位 | 型別 | 值 |
  |---|---|---|
  | `client_id` | str | |
  | `label` | str | 哨兵為 `"primary"` |
  | `vtty` | str | `slave_path` |
  | `interactive_owner` | bool | `owner == f"human:{client_id}"` |

  哨兵 primary 也會列出。`SessionRuntime.to_public_dict()`（`session_manager.py:539-544`）以 `len(bridge.list_consoles())` 產生 console 數 → 讀取 session 也會觸發 prune 與 /proc 掃描。
- `console_has_external_peer(cid) -> bool`、`has_console(cid) -> bool`、`console_endpoint() -> str|None`、`vtty_path` property（primary 的 slave_path）。

### 7.5 Stale console 偵測
- `_enumerate_all_held_paths() -> set[str] | None`（`463-490`）：掃 `/proc/<pid>/fd/*` readlink（排除自身 pid），`/proc` 不可讀回 None。
- `_client_has_external_peer_locked(client)`（`492-501`）：socket → True；`held is None` → True；否則 `slave_path in held`。**每呼叫一次完整掃 /proc，且在 `_state_lock` 內**。
- `_prune_stale_consoles_locked(now=None)`（`589-605`，attach/list 時）：跳過 primary、未滿 2.0s 者、有 peer 者；pop，若為 owner 清 owner。**不**檢查 `internal`、不清 pending/suspended/deferred、不轉移 primary。
- `reap_stale_consoles(*, held_slave_paths=None)`（`526-575`，週期）：鎖內選候選（非 internal、PTY、非 owner/suspended、滿 2.0s），鎖外掃 /proc（或用傳入共享 held），回鎖重檢保護集、刪 deferred、轉移 primary；鎖外關 fd。session_manager reconcile tick 每 `_HUMAN_PEER_GRACE_S=3.0`s 呼叫並做 lease-backed 自癒 `try_grant_interactive_if_idle`（`session_manager.py:1638-1690`）。
- TCP client 斷線只靠 `recv` 回 `b""`/OSError drop，不掃 /proc。

### 7.6 Console 出向送出（fan-out / echo）
- `_console_send(client, payload)`（`617-631`）：socket → telnet 時 `escape_iac`，再 `_console_sock_send`；PTY → `_write_console_best_effort(master_fd, payload)`。
- `_write_console_best_effort`（`607-615`）：單次 `os.write`；EAGAIN 直接 return；**忽略回傳值 → 部分寫入時尾端靜默丟失**；其他 OSError 上拋（呼叫端吞）。
- `_console_sock_send`（`633-657`）：持 `sock_send_lock`；迴圈 `sock.send(view[sent:])`，`BlockingIOError` 或 `n<=0` → 停止並**丟棄剩餘**（可能切開 IAC IAC）；其他 OSError 上拋。
- **沒有 per-console ring queue**：慢 console 的輸出是「kernel buffer 滿就丟（丟最新/尾端）」；fan-out 在 reader thread 中持 `_state_lock` 同步執行。

### 7.7 snapshot()（`uart_io.py:1402-1462`）欄位
`com`、`device_path`、`vtty`（primary slave_path 或 None）、`serial_alive`（POSIX `os.fstat(fd)` 成功；Windows `port.is_alive()`）、`vtty_alive`（Windows：listener 非 None；POSIX：`os.path.exists(primary)`）、`interactive_owner`、`last_human_input_at`、`consoles`（= `list_consoles()`）、`running`（reader thread alive）、`console_endpoint`、`agent_active`、`suspended_owner`、`flash_mode`、`primary_client_id`、`rx_dropped_chars`（=`_rx_trimmed`）、`rx_total_bytes`。

---

## 8. Windows TCP + Telnet console

### 8.1 Listener（`uart_io.py:338-355`）
`socket(AF_INET, SOCK_STREAM)`、`SO_REUSEADDR=1`、`bind(("127.0.0.1", 0))`（**ephemeral port，每次 bridge start 都換**）、`listen(8)`、nonblocking；`_console_endpoint = f"{host}:{port}"`。只在 `_pty_available()` 為 False 時啟用（Linux 恆不啟用）。

### 8.2 Accept（`uart_io.py:363-410`）
`setblocking(False)`、`TCP_NODELAY=1`（失敗忽略）；建 `ConsoleClient(master_fd=-1, slave_fd=-1, slave_path=endpoint or peer, sock=conn, telnet=TelnetFilter())`；**先送 `TELNET_GREETING`（未註冊前，避免與 RX fan-out 交錯）**，再註冊；primary 為空則設；ownership 規則見 §6.2。

### 8.3 `_console_loop`（`uart_io.py:412-456`）
select 例外 → stop 則 break、否則 sleep 0.02 續行；listener 可讀 → accept；socket `recv(8192)`：`BlockingIOError` 略過；OSError / `b""` → drop；資料 → `_handle_console_rx`（例外 log、續行）。

### 8.4 Telnet 協定（`telnet_console.py`）
常數：`IAC=0xFF, SE=0xF0, SB=0xFA, WILL=0xFB, WONT=0xFC, DO=0xFD, DONT=0xFE, OPT_BINARY=0x00, OPT_ECHO=0x01, OPT_SGA=0x03`。
`TELNET_GREETING`（12 bytes）= `FF FB 01  FF FB 03  FF FD 03  FF FB 00`（WILL ECHO、WILL SGA、DO SGA、WILL BINARY）。
`escape_iac(payload)`：無 0xFF 原物件回傳；否則 `replace(b"\xff", b"\xff\xff")`。出向無 LF→CRLF 轉換（宣稱 WILL BINARY）。

`TelnetFilter`：`_state` 初值 `_DATA`，`_local_on = {OPT_ECHO, OPT_SGA, OPT_BINARY}`。狀態：`_DATA=0, _DATA_CR=1, _IAC=2, _OPT_WILL=3, _OPT_WONT=4, _OPT_DO=5, _OPT_DONT=6, _SB=7, _SB_IAC=8`（跨 feed 存活）。`feed(data) -> (uart_bytes, reply_bytes)`：

| 狀態 | 輸入 | 動作 |
|---|---|---|
| `_DATA_CR` | 任一 | 先轉 `_DATA`；若 byte ∈ {0x00, 0x0A} → 吞（一次）；否則依 `_DATA` 處理 |
| `_DATA` | `0xFF` | → `_IAC` |
| `_DATA` | `0x0D` | **立即** emit `0x0D`，→ `_DATA_CR` |
| `_DATA` | 其他 | emit（含單獨 LF、NUL） |
| `_IAC` | `0xFF` | emit `0xFF`，→ `_DATA` |
| `_IAC` | WILL/WONT/DO/DONT | → 對應 `_OPT_*` |
| `_IAC` | SB | → `_SB` |
| `_IAC` | 其他（SE/NOP/DM/BRK/IP/AO/AYT/EC/EL/GA…） | 吞，→ `_DATA`（IP 不轉 Ctrl-C、AYT 不回應） |
| `_OPT_WILL` | opt | `opt != SGA` → reply `IAC DONT opt`（含 WILL BINARY → **入向拒絕 binary**，WILL ECHO → DONT ECHO）；WILL SGA 靜默；→ `_DATA` |
| `_OPT_DO` | opt | `opt not in _local_on` → reply `IAC WONT opt`；在集合內靜默（視為 ack）；→ `_DATA` |
| `_OPT_DONT` | opt | `opt in _local_on` → 移出、reply `IAC WONT opt`（只在狀態變更時回）；否則靜默 |
| `_OPT_WONT` | 任一 | 靜默，→ `_DATA` |
| `_SB` | `0xFF` → `_SB_IAC`；其他吞 | |
| `_SB_IAC` | `SE` → `_DATA`；其他 → `_SB`（續吞） | |

注意：
- CR 摺疊：`CR NUL`、`CR LF` → 單一 `CR`；`CR CR` → 兩個 CR；`CR IAC IAC` → `CR 0xFF`。
- 選項被 DONT 關閉後，之後的 `DO opt` 會回 WONT（無法重新開啟）。
- 處理順序：telnet 過濾在 flash gate 之前；deferred buffer 存的是過濾後 bytes。
- 過濾後的資料再進 §6.3 raw/line 判斷 → line mode 的 echo 經 `_console_send` 做 IAC 逸出。

---

## 9. Flash 端點 `/dev/ttyMCU`（POSIX-only）

### 9.1 `FlashEndpoint`（`flash_endpoint.py:98-195`）
- `__init__(*, link_path, on_flash_open=None, grace=0.3)`；`on_flash_open(master_fd, slave_fd, first_bytes) -> None`。
- `start()`：import `pty`/`tty`；ImportError 且 `sys.platform == "win32"` → return（no-op）；否則上拋。`pty.openpty()`；**`tty.setraw(slave_fd)`**（byte-transparent）；`os.set_blocking(master_fd, False)`；`os.makedirs(dirname(link_path), exist_ok=True)`；移除既有 link（FileNotFoundError 忽略）；`os.symlink(os.ttyname(slave_fd), link_path)`；起 `serialwrap-ttyMCU` thread。daemon 持 master+slave（避免閒置 master EOF 空轉）。
- `stop()`：set stop、join 2.0s、關 master/slave、`os.remove(link_path)`（OSError 忽略）。
- `is_flashing() -> bool`：`_flash_active` Event。
- `_loop()`：`select([master], [], [], grace)`；OSError → return（執行緒結束）；無資料 → continue（**端點永不主動寫**）；有資料 → `first = os.read(master, 4096)`（錯誤 → `b""`）；若未 active：set active → 同步呼叫 `on_flash_open(master, slave, first)`（任何例外吞掉）→ finally clear active。pump 在這個 callback 內跑完才回來。
  - 未匹配時 `first` 已被讀掉丟棄（不回放），flasher 下一次 sync 重觸發整套偵測。
  - 只用第一次 read 的 ≤4096 bytes 當 probe（sync 分多次寫入時只取第一段）。

### 9.2 Sync-probe 認線（`service.py:146-232, 381-462`；`flash_endpoint.py:57-95`）
1. `_on_flash_open`：`candidates = sessions.collect_flash_candidates_and_mark()`（`session_manager.py:1892-1918`，manager `_lock` 內原子收集：`not profile.command_capable` 且 state ∈ {`READY`,`ATTACHED`}，項目 `{"com","by_id","real_path","command_capable": False}`，同時把 com 加入 `_flash_critical_coms` → destructive op 回 `FLASHING_BUSY`）。
2. `transport = _BridgeProbe(svc, by_id_to_com, sync_bytes=first_bytes)`。
3. `detect_mcu_line(candidates, registry, transport)`：過濾 `command_capable`；對每候選依 `registry.all()` 順序 probe，首中即 break；0 → `DetectResult("none")`；>1 → `"ambiguous"`（`hits=[by_id...]`）；1 → `"matched"`（`by_id`, `family`）。`DetectResult` 欄位：`status: str`、`by_id: str|None=None`、`family: str|None=None`、`hits: list[str]=[]`。
4. `_BridgeProbe.probe(by_id, probe_bytes, expect, timeout_ms)`：com 不在 map / 無 bridge → False；重置 `_flash_rx_buffers[com] = bytearray()`；**`bridge.set_flash_mode(True)`**（probe 期間擋 human/其他寫入）；`bridge.flash_tx(self._sync_bytes or probe_bytes)`（**送 flasher 自己的 sync，不另注入**；例外 → False）；在 `timeout_ms` 內每 0.02s 檢查 `expect in buf`（**子字串包含**，非前綴）→ 命中：`acks[by_id] = buf`、`held[com] = bridge`（**gate 不解**）、True；未中 finally `set_flash_mode(False)`。
5. 記錄 `_flash_last_detect = {"status", "com", "family", "hits": [{"by_id","com"}]}`。
6. 非 matched → return（端點保持沉默）。matched：`fallback_baud = registry.get(family).baud`；`ack = transport.acks[by_id]`；`sessions.enter_flashing(com)`（state→`FLASHING`、保存 `flash_prev_state`、`bridge.set_flash_mode(True)`；**不 detach bridge**）；`release_held(adopt=com)`（命中線 gate 交接，其他 held 解除）；全部候選 `unmark_flash_critical`。
7. `_flash_active_com = com`、`_flash_master_fd = master_fd`；`slave_path = os.ttyname(slave_fd)` → 起 `_watch_flasher_disconnect(slave_path, stop)`。
8. `bridge.mirror_termios_from(slave_fd, fallback_baud=fallback_baud)`；若有 ack → `os.write(master_fd, ack)`（**回放 MCU 的 ACK 給 flasher**，回放的是整段 probe 期間 RX buffer，可能含 ACK 前後的其他 bytes）；`pump_endpoint_to_sink(master_fd, bridge, stop, first_bytes=b"")`（**不重送 first_bytes**）。
9. finally：清 `_flash_active_com/_flash_master_fd`、`_flash_rx_buffers.pop(com)`、`sessions.exit_flashing(com)`（無條件 `set_flash_mode(False)`；state 還原為 `flash_prev_state or ("READY" if bridge else "DETACHED")`）。外層 finally：`release_held()` + 全部 unmark。

### 9.3 RX → endpoint（`service.py:363-379`）
`_flash_rx_observer(com, data, wal_seq)`：持 `_flash_lock`：若 `_flash_rx_buffers` 有該 com → extend 並保留最後 4096；`active = _flash_active_com == com`；鎖外 `os.write(master_fd, data)`（OSError 忽略，部分寫入忽略）。在 bridge reader thread 中、WAL append 之後、console fan-out 之前執行。`make_rx_to_endpoint_writer(master_fd)`（`flash_endpoint.py:247-263`）同語意但**未被使用**。

### 9.4 Endpoint → device pump（`flash_endpoint.py:206-244`）
`pump_endpoint_to_sink(master_fd, sink, stop_event, first_bytes=b"", chunk=4096)`：先送 first_bytes（非空）；`select([master], [], [], 0.2)`；可讀 → `os.read(master, chunk)`；錯誤 → return；`b""` → return（EOF）；`sink.flash_tx(data)`。`OSError`/`RuntimeError`（bridge 掉線 "serial not ready"）→ return（C1）。每次 `flash_tx` 都產生一筆 WAL TX（source `"flash"`）。

### 9.5 Holder-probe（`service.py:467-483`，`session_manager.py:1954-2001`）
- `_watch_flasher_disconnect(slave_path, stop, poll_s=0.5, max_s=1800.0)`：每 0.5s `sessions._probe_external_holder(slave_path)["pids"]`；有 pids → `armed=True`；armed 後 pids 空 → break；結束一律 `stop.set()`。
  - 若 flasher 在第一次 poll 前就關閉（從未 armed），pump 只能等 EOF（不會來，因 daemon 持 slave）或 1800s 上限。
- `_probe_external_holder(real_path, *, _proc_root="/proc") -> {"pids": sorted list[int], "holder": pids[0] | None}`：排除自身 pid；fd readlink 等於 `realpath(real_path)` 或 `real_path` → 命中；否則 `os.stat(fd_path).st_rdev == stat(real_path).st_rdev`（非 0）→ 命中。`/proc` 不可讀 → `{"pids": [], "holder": None}`（**與「無 holder」不可區分**）。

### 9.6 Baud 鏡射 `mirror_termios_from(slave_fd, *, fallback_baud=None)`（`uart_io.py:1014-1047`）
- `port is None` → return；`port.fileno() is None`（Windows）→ 只 `port.set_baud(fallback_baud)`。
- POSIX：`termios.tcgetattr(slave_fd)` 取 `[4]ispeed/[5]ospeed`，寫入 serial fd 的 attrs `[4]/[5]`，`tcsetattr(TCSANOW)`；OSError → `port.set_baud(fallback_baud)`（再 OSError 忽略）。
- **一次性**：只在 flash 開始時鏡射一次，flasher 中途改 baud 不會跟隨；只鏡射 speed（不含 parity/data bits/stop bits）；`tty.setraw` 不改 speed，故 flasher 未設 baud 時 slave 的初始 speed（kernel 預設，常為 B38400）會被鏡射到實體 UART（現況風險，需驗證）。
- 不在 `_write_lock` 下執行（與並發 TX 無序）。

### 9.7 Flash gate 互動
- `set_flash_mode(enabled)`（`uart_io.py:192-203`）：持 `_write_lock`→`_state_lock` 設旗標 → 進行中寫入完成後才生效、之後非 flash 寫入一律丟棄（#69）。
- flash 期間：console 輸入丟棄、`send_bytes` 非 flash 來源靜默 return（命令/probe/self-test 等都會「成功但沒寫」）；RX fan-out 照常（console 唯讀觀察）。
- flash 不清 deferred buffer、不改 owner/suspend 簿記。

### 9.8 `resolve_flash_target(selector, sessions, *, force) -> dict`（`flash_endpoint.py:266-295`）
保留未接線：找不到 → `{"ok": False, "error_code": "SESSION_NOT_FOUND", "selector"}`；command_capable 且未 force → `{"ok": False, "error_code": "FLASH_TARGET_IS_CONSOLE", "selector", "hint"}`；否則 `{"ok": True, "by_id", "com"}`。

### 9.9 MCU pattern registry（`mcu_patterns.py`）
- `McuPattern`（frozen）：`family: str`、`probe: bytes`、`expect: bytes`、`baud: int`、`timeout_ms: int`、`non_destructive: bool = True`；`__post_init__` 若 `not non_destructive` → `ValueError(f"pattern {family!r} 未通過 non_destructive 審核，拒絕載入")`。
- `from_dict(d)`：`family=str(d["family"])`、`probe/expect=_hex_to_bytes(str(...))`（去空白、去所有 `"0x"` 子字串、`bytes.fromhex`）、`baud=int(d.get("baud",115200))`、`timeout_ms=int(d.get("timeout_ms",500))`、`non_destructive=bool(d.get("non_destructive", False))`（**缺省視為 False → 拒絕**）。
- `_DEFAULTS`：唯一一筆 `{"family": "ti-cc26xx", "probe": "5555", "expect": "00cc", "baud": 115200, "timeout_ms": 500, "non_destructive": True}`。
- `McuPatternRegistry(patterns)`：`_by_family = {p.family: p}`（後者覆蓋同名，dict 保持插入序）；`default()`；`load(rows)`（defaults + rows；service 目前只呼叫 `load(None)`，無自訂 pattern 設定來源）；`get(family)`（KeyError）；`all()`；`render_support_list(*, candidates)`（多行字串，service 目前未使用）。
- RPC：`mcu.patterns` → `{"ok": True, "patterns": [{"family", "probe": hex(" "), "expect": hex(" "), "baud"}]}`（**不含 timeout_ms**）；`mcu.status` → `{"ok": True, "candidates": _flash_candidates(), "flashing": endpoint.is_flashing(), "flashing_com", "last_detect"}`（`service.py:711-723`）。`_flash_candidates()`（`485-499`）以 `list_sessions()` + `list_devices()` 非原子組成，欄位 `{"com","by_id","real_path","command_capable": False}`。

---

## 10. SerialPort 抽象（`serial_port.py`）

### 10.1 介面 `SerialPort(ABC)`（`62-103`）
生命週期 `open()` → `configure(profile)` → `read()/write()` → `close()`。
| 方法 | 契約 |
|---|---|
| `open() -> None` | 失敗拋 `OSError` |
| `configure(profile: UartProfile) -> None` | baud/data_bits/parity/stop_bits/flow_control |
| `read(max_bytes=8192) -> bytes` | POSIX：select 後呼叫，等同 `os.read`（無資料 `BlockingIOError`，EOF `b""`）；pyserial：阻塞 ≤ timeout，idle 回 `b""` |
| `write(payload) -> None` | 寫完整段（無部分完成回報） |
| `close() -> None` | best-effort，吞例外 |
| `set_baud(baud) -> None` | |
| `is_alive() -> bool` | |
| `fileno() -> int \| None` | 預設 None |
**沒有** cancel / interrupt / drain / flush / 獨占 API。

### 10.2 `_PosixSerialPort`（`106-217`）
- `open()`：`os.open(path, O_RDWR | O_NOCTTY | O_NONBLOCK)`。**無 `O_EXCL`、無 `TIOCEXCL` ioctl、無 `flock`/`fcntl` lock、無 holder 預檢**。
- `configure(profile)`：`tcgetattr` → `iflag=0`、`oflag=0`、`lflag=0`；`cflag = CREAD | CLOCAL | (CS7 if data_bits==7 else CS8)`；parity `"E"` → `PARENB`，`"O"` → `PARENB|PARODD`（其他 = 無）；`stop_bits==2` → `CSTOPB`；`flow_control.lower()=="rtscts"` 且有 `CRTSCTS` → `CRTSCTS`；**`xonxoff` 被忽略（iflag=0 → 無 IXON/IXOFF）**；speed `_BAUD_MAP.get(baud, B115200)` 以 `cfsetispeed/cfsetospeed`（無則直寫 attrs[4]/[5]）；**未設 `VMIN/VTIME`（沿用原 cc 值）**；`tcsetattr(TCSANOW)`；再設 `O_NONBLOCK`。不 `tcflush`、不設 `HUPCL`、不碰 DTR/RTS。
- `read`：`os.read(fd, max_bytes)`；fd None → `OSError("serial not open")`。
- `write`：同 `_write_all`（EAGAIN 無限重試、`n<=0` break）。
- `close`：清 `_fd` 後 `os.close`（吞 OSError）。
- `set_baud(baud)`：fd None 或 baud 不在 `_BAUD_MAP` → **靜默 no-op**；否則改 speed `TCSANOW`。
- `is_alive`：`os.fstat(fd)`。`fileno`：`_fd`。

### 10.3 `_PySerialPort`（`220-359`）
- `open()`：import `serial`（缺 → `OSError("pyserial 後端需要 pyserial（pip install pyserial）")`）；`serial.Serial()`；`port = _win_device_name(path)`（`COMx` → `\\.\COMX` 大寫；已有 `\\.\` 或非 COM 原樣，`_COM_RE = r"^COM\d+$"` IGNORECASE）；`timeout=0.2`、`write_timeout=5.0`；`_apply(profile)`；`ser.open()`；`SerialException` → `OSError(errno or EIO, str)`。未傳 `exclusive=`（Windows 上 pyserial 以 `CreateFile(..., dwShareMode=0)` 開，原生獨占；POSIX 上使用 pyserial 則無獨占）。
- `_apply`：`baudrate`、`bytesize`（7→SEVENBITS 否則 EIGHTBITS）、`parity`（E/O/N，未知→NONE）、`stopbits`（2→TWO 否則 ONE）、`rtscts = flow_control.lower()=="rtscts"`、`xonxoff = bool(profile.xonxoff)`、`dsrdtr=False`。
- `configure`：只重 `_apply`。
- `read`：未開 → `OSError(EBADF)`；`ser.read(1)`（≤0.2s），空 → `b""`；再讀 `min(in_waiting, max_bytes-1)`；`SerialException` → `OSError`。
- `write`：memoryview 迴圈 `ser.write`；`SerialTimeoutException` → `OSError(ETIMEDOUT)`（**已寫部分長度遺失**）；`SerialException` → `OSError`；`not n` → break。不 flush。
- `close`：吞所有例外。`set_baud`：設 `baudrate` 並重建 `_profile`（`type(self._profile)(baud=..., data_bits, parity, stop_bits, flow_control, xonxoff)`）。`is_alive`：`ser.is_open`。`fileno`：None。
- 沒有取消 pending I/O 的機制；reader 只能靠 0.2s timeout 自然返回。

### 10.4 後端選擇
- `_select_backend(backend)`（`362-371`）：`backend or $SERIALWRAP_SERIAL_BACKEND or "auto"`（lower）；`posix|termios` → posix；`pyserial|windows|win32` → pyserial；auto：`os.name=="nt"` 或 `sys.platform` 以 `win` 開頭 → pyserial，否則 posix。未知字串視同 auto。
- `open_serial_port(device_path, profile, *, backend=None) -> SerialPort`（`374-394`）：未開啟物件；非 POSIX 強選 posix → `OSError("POSIX/termios 序列埠後端在此平台不可用（缺 termios/fcntl）；請改用 pyserial 後端")`。
- 注意：`UARTBridge.start()` 呼叫 `open_serial_port(self.device_path, self.profile)` 不傳 backend，只受 env 控制。

---

## 11. 測試資產（承接契約時應對照）
`tests/test_uart_io.py`、`test_serial_port.py`（含 `_serial`/`_serial_fd` 配對不變式）、`test_telnet_filter.py`、`test_interactive_raw.py`、`test_human_agent_coexist.py`、`test_suspend_resume_reentrant.py`、`test_service_human_console.py`、`test_session_console_endpoint.py`、`test_bounded_memory.py`（deferred 截頭）、`test_rx_flood.py`、`test_refine_ownership.py`、`test_refine_flash_precedence.py`、`test_uart_flash_bridge.py`、`test_flash_endpoint.py`、`test_flash_probe.py`、`test_flash_pump.py`、`test_flash_service_wiring.py`、`test_flash_guard.py`、`test_flashing_state.py`、`test_mcu_patterns.py`、`test_mcu_cli_rpc.py`、`test_console_unlock.py`、`test_file_transfer.py`（echo-paced）、`test_transport_stall.py`、`test_boot_quiet.py`、`test_readiness_reprobe.py`。

---

## 12. 規格 vs 現況差異（Gap 清單）

### 12.1 BC-04 / ADR-02 / §6.2 Per-UART 獨占（現況：**不存在**）
- POSIX `_PosixSerialPort.open` 只有 `O_RDWR|O_NOCTTY|O_NONBLOCK`：**無 `flock(LOCK_EX|LOCK_NB)`、無 `TIOCEXCL`**；規格要求「對已開啟 tty fd 取非阻塞 flock 並設 TIOCEXCL，失敗時不啟動一般收送」。全 repo grep 無 `TIOCEXCL`；`flock` 僅用於 daemon singleton、remote_tunnel、cli。
- 現有替代只是**被動診斷**：`multi_open.py`（`doctor` / `daemon status` 的 `multi_open`/`foreign_holders`）、`_probe_external_holder`（僅 `attach_device`（非 force，RELEASED 收回）與 self-test RELEASED 分支使用；一般 hotplug attach 不預檢）。
- `/proc` 不可讀時 `_probe_external_holder` 回 `pids: []`，與「無 holder」不可區分；規格要求不得以空 PID 推導無 holder，Windows 要 `pids: []` + `holder_unknown: true`——現況無 `holder_unknown` 欄位。
- Windows：pyserial 在 Windows 實際以 share mode 0 開啟（原生獨占），但失敗只轉成一般 `OSError(errno or EIO)`；無 `DEVICE_STILL_HELD` 對映、無 `ERROR_ACCESS_DENIED` 分類。
- PROBE bridge（`session_manager.py:2953, 3013`）與正式 bridge 先後開同一裝置，無鎖交接。
- `xonxoff` 在 POSIX 被忽略（pyserial 有套用）——跨平台行為不一致。

### 12.2 §5.2 Two-Phase Drain Barrier（現況：**不存在**）
- 無 `conn_epoch`/`owner_gen`/`fsm_gen`；唯一身分是 session_manager 的 `bridge_generation` 與 `_ForegroundOperation` token。
- `stop()` 不取 `_write_lock`、不等待進行中的 `_write_all`；reader/console join 2.0s 逾時後仍關 handle → 可能對已關閉／被重用 fd 寫或讀；無 `WorkerStoppedReceipt`、無 in-flight I/O 結果、無 3,000 ms drain timeout / `drain_stalled` / `drain_timeout_epoch`。
- `_write_all` 在 EAGAIN 上無限 `sleep(0.01)` 重試且持 `_write_lock`：RTS/CTS 被拉住或 USB 卡死時會永久卡住所有寫入者，也無法被 stop 取消（POSIX 無可喚醒機制；Windows 只有 pyserial `write_timeout=5.0`）。
- 「送出前檢查」只有 flash gate 與 raw-console admission 兩項（於 `_write_lock` 內）；沒有 epoch/owner/Quiet/Release 在 write 前重檢（這些在 session_manager 層更早檢查，存在 TOCTOU）。
- §5.1.4 三態 TX 結果（未提交 / 確認 N bytes / 不明）：現況 write 只有「回傳 None」或「拋例外」，部分寫入（`n<=0` break、pyserial timeout）長度不回報。

### 12.3 §8.1 TX WAL 語意
- 規格：只為**確認完成**的 bytes 建 TX 紀錄、確認長度小於要求時只記確認部分。現況：成功路徑記**整段 payload**（含 `n<=0` 提早 break 的情況）；例外路徑**完全不記**（即使已寫出部分）；flash gate 靜默丟棄回傳成功（呼叫端以為已送）。
- TX WAL 在 `_write_lock` 外 append → WAL 順序不等於實際寫入順序。
- `send_secret` 不記 TX（規格未明列此例外，需在契約凍結時明載為既有行為或 META）。

### 12.4 §8.1 RX / ADR-05
- RX 順序符合（WAL 先於 console/engine/login）。但 WAL 為每筆 open/append/close、無常駐 handle、無每筆 flush 語意外的 durable（BC-01 尚未在 Python 實作）；observer 拿到的 `wal_seq` 非該 chunk 精確 seq；非 OSError 例外會讓 chunk 不送 console。

### 12.5 §5.2.6 / §5.4 / BC-02 DeferredBuffer
- 規格：超過 262,144 bytes **整段清空，不截頭留尾**；Reboot/Quiet/Recovery/Release/Flash 跨上下文時丟棄整段舊輸入；保留輸入擷取時的 owner/connection 上下文。
- 現況：**截頭留尾**（三處相同），`test_deferred_buffer_capped` 鎖定此舊行為（BC-02 實作時需改測試）；flash 開啟不清 deferred（flash 結束後下一次 resume/gate-open 仍會回放 flash 前累積的輸入）；`stop(preserve_consoles=True)`（recovery）會清 deferred（符合「Recovery 丟棄」）；`_prune_stale_consoles_locked` 不清該 client 的 deferred；buffer 無上下文標記（僅 client_id key）。
- 值相同：`DEFERRED_INPUT_MAX_BYTES = 262144`。

### 12.6 §5.4 每 Console 出向 Ring Queue 1 MiB Drop-Oldest
- 現況**無 ring queue**：fan-out 在 reader thread 持 `_state_lock` 同步非阻塞寫；PTY 單次 `os.write` 忽略部分寫入（尾端丟）、EAGAIN 整塊丟；socket 迴圈送到 `BlockingIOError` 為止、剩餘丟棄（可能切開 `IAC IAC`）。語意是「drop-newest/截尾」，不是 drop-oldest；但符合「不阻塞 UART/WAL/其他 console」。
- 無丟棄計數/診斷。

### 12.7 §7.1 POSIX PTY「穩定 symlink 與必要 keepalive」
- Console PTY **沒有 symlink**（vtty = `/dev/pts/N`）；穩定性來自 `PreservedConsoles` 跨 bridge 重建保留 fd。只有 `/dev/ttyMCU` 有 symlink。規格「穩定 symlink」若指 console，與現況不符（需釐清是否僅指 ttyMCU）。
- keepalive = daemon 同持 master+slave（console 與 ttyMCU 皆然）＋哨兵 internal primary console；外部斷線不產生 EOF，靠 `/proc` 掃描（2.0s grace、3.0s 週期）。

### 12.8 §7.2 Telnet
- 規格「既有 WILL ECHO、WILL SGA、WILL BINARY」：現況 greeting 另含 **`DO SGA`**（共 4 組 12 bytes）。
- 「拒絕入向 Binary」：現況 = 對 client `WILL BINARY` 回 `DONT BINARY`（且對所有非 SGA 的 WILL 都回 DONT）。
- CR LF / CR NUL 摺疊、`0xFF 0xFF` 還原、跨 chunk 狀態：符合。
- Listener port 為 ephemeral（每次 bridge start 改變；preserved socket client 的 `slave_path` 仍是舊 endpoint 字串）——規格未定，需凍結。
- 規格要求 parser 不得把 Telnet 控制 bytes 當 DUT TX：符合（IP/AYT 等被吞，不轉換為 Ctrl-C）。

### 12.9 §5.3 Flash 相關事件/授權
- 無 `probe_id`；probe 授權以 `set_flash_mode(True)` 共用 FLASHING 同一旗標表示（同 handle 模式切換，未換 handle——符合「不重開不必要連線」）。
- 候選資格：現況為「非 command_capable 且 READY/ATTACHED」；未檢查 RELEASED/RECOVERING（RECOVERING 不在集合內故自然排除）、未檢查是否有執行中命令（probe 會在 agent 命令執行期間把該 bridge 的寫入全部靜默丟棄）。
- FlashSyncMatched 時規格要求「清理 DeferredBuffer」：現況不清。
- FlashHolderGone → 規格「回 `ATTACHED`」：現況 `exit_flashing` 還原為 `flash_prev_state`（可能是 `READY`），bridge 不關不重開。
- holder-probe 從未 armed 時最長等 1800s；ACK 判定為「expect 子字串出現在 probe 期間 RX」，回放的是整段 RX buffer。
- baud 鏡射一次性、只鏡射 speed。

### 12.10 其他觀察（承接時需決定保留或修正）
- `_consume_console_input` 的 CR/LF 合併不跨 chunk（CR|LF 分屬兩次 read 會多一個空行，空行也會 submit 到 arbiter）。
- `_append_rx_text` 每 chunk 獨立 UTF-8 decode（跨 chunk 多位元組字元損毀）；`_rx_max_chars` 以字元計。
- `_prune_stale_consoles_locked` 在 `_state_lock` 內對每個 client 完整掃 `/proc`，且由 `to_public_dict()`/`list_consoles()` 等讀取路徑觸發。
- POSIX serial `read` 回 `b""`（EOF）不觸發 bridge-down；`select` 錯誤不 set stop_event（但 break 出迴圈）。
- `_drain_line_buffer`、`make_rx_to_endpoint_writer`、`resolve_flash_target`、`render_support_list` 為無 runtime 呼叫者的程式碼。
- `mcu.patterns` 不輸出 `timeout_ms`；registry 無使用者自訂 pattern 的設定入口（`load(None)`）。
- `configure()` 不設 VMIN/VTIME、不 `tcflush`，殘留的 kernel 輸入緩衝會在 attach 後立即當 RX 進 WAL。
