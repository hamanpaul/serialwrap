# 04 — WAL 與持久化盤點（Python 基線 → Rust 1:1）

- 盤點對象 repo：`~/prj_pri/serialwrap-worktrees/rust-refactor`，HEAD `93553e6`（唯讀，未修改）。
- 對照規格：`docs/rust-refactor/serialwrap-rust-spec-final.md`（§1.1.3、§3.2、§4.1、§6.1、§6.2、§8、§9.4、§12.1）。
- 驗證方式：逐行讀碼＋在 scratchpad 以隔離 env（`SERIALWRAP_STATE_DIR/CONFIG_DIR/RUN_DIR/WAL_DIR` 指到 scratchpad）實跑 `WalWriter`、`SessionManager`、`cli._remember_bench_endpoint`、`RuntimeConfig`，取得實際序列化位元組。實跑結果標 **[實跑]**；只讀碼得出者標 **[讀碼]**。
- 行號皆為該 HEAD 的 1-based 行號。

---

## 0. 摘要：Rust 實作者必須知道的 12 件事

1. WAL 一筆＝一行 `json.dumps(record, ensure_ascii=False, sort_keys=True, separators=(",", ":")) + "\n"`；**key 依字母序**排列（不是 schema 宣告順序）。
2. `crc32` 是 **8 位小寫十六進位字串**（`f"{zlib.crc32(payload) & 0xFFFFFFFF:08x}"`），**不是** JSON 數字。演算法＝zlib／IEEE 802.3 CRC-32（poly 0xEDB88320 reflected、init 0xFFFFFFFF、xorout 0xFFFFFFFF；Rust 可用 `crc32fast`）。規格 §8.1 寫型別 `u32`，與現況不符。
3. rotate 門檻 `R = DEFAULT_WAL_ROTATE_BYTES = 64 * 1024 * 1024 = 67108864` bytes（`sw_core/constants.py:65`）。判斷是 `getsize(path) >= R` **在寫入前**檢查，因此檔案最終大小可超過 R 一筆。
4. WAL 與 mirror **各自獨立判斷 rotate**（mirror 比 WAL 小很多，輪替時間點不同、後綴時間戳不同）。
5. 輪替檔名＝`<path>.<YYYYmmdd-HHMMSS>`，**UTC**（`time.gmtime()`），秒級精度；同一秒內兩次輪替會 `os.replace` 覆蓋前一個歸檔（資料遺失）。
6. seq：行程內單調 +1；rotate **不重設**；`wal.reset` 重設為 0（下一筆＝1）；daemon 重啟時＝**現行檔**中最大有效 `seq`，現行檔不存在則 0。因此 seq **跨歸檔檔不唯一**（reset 後重複）。
7. 目前（BC-01 前）**每筆 append 開檔→寫→關檔**，WAL 先、mirror 後，沒有 flush/fsync（只有 Python 檔案物件關閉時的 flush）；rotate/reset 有目錄 fsync（POSIX）。
8. mirror（`raw.mirror.log`）＝所有 session、所有方向（RX/TX/META）payload 的 `to_printable()` 直接串接，**沒有任何分隔、時間戳、COM 標示**。
9. `wal.range` 回應**沒有** `truncated` 欄位（規格 §8.3 宣稱「WAL range 的 `truncated=true` 表示仍有下一頁」不成立）；`truncated` 只存在於 `log.tail_raw`／`log.tail_text`。
10. `state.json` 只有 5 個頂層 key：`aliases`、`bindings`、`released`、`profile_pins`、`profile_detected`；**沒有版本欄位**、沒有 migration 機制；未知 key 在下一次存檔時被丟棄。
11. **已確認缺陷 [實跑]**：動態偵測（非 YAML target）session 的 RELEASED 只撐過**一次**重啟——第二次重啟後 `released` 為 `{}`、`_released_by_ids` 為空，裝置會被自動 attach（違反規格 §6.2 交接持久化）。
12. **已確認缺陷 [實跑]**：`alias.set` 寫進 `state.json` 但重啟後**不會套回** session（`get_session("<自訂 alias>")` 為 None，profile.alias 回到 YAML 值）；alias registry 只增不減殘留舊列。

---

## 1. WAL（`sw_core/wal.py`，355 行）

### 1.1 模組層

| 項目 | 值 | 位置 |
|---|---|---|
| import 常數 | `DEFAULT_WAL_ROTATE_BYTES`, `WAL_DIR` from `.constants` | wal.py:14 |
| helper | `dumps_stable`, `monotonic_ns`, `now_iso`, `to_printable` from `.util` | wal.py:15 |
| logger | `_LOG = logging.getLogger("serialwrap")`（模組層，避免熱路徑取全域鎖，#189） | wal.py:19 |
| `_fsync_dir(path)` | `os.name == "nt"` 直接 return；否則 `os.open(path, os.O_RDONLY)` → `os.fsync` → `os.close` | wal.py:22-34 |

### 1.2 `class WalWriter`

建構子 `__init__(self, wal_dir: str | None = None, rotate_bytes: int = DEFAULT_WAL_ROTATE_BYTES) -> None`（wal.py:38-52）：

| 欄位 | 初值 | 說明 |
|---|---|---|
| `_wal_dir` | `WAL_DIR if wal_dir is None else wal_dir` | None-sentinel 於**建構時**讀模組層 `WAL_DIR`（#120；測試以 setattr 隔離） |
| `_rotate_bytes` | `rotate_bytes`（預設 67108864） | |
| `_wal_path` | `os.path.join(_wal_dir, "raw.wal.ndjson")` | |
| `_mirror_path` | `os.path.join(_wal_dir, "raw.mirror.log")` | |
| `_lock` | `threading.Lock()` | append/reset/current_seq 共用 |
| `_seq` | `0` → `_load_last_seq()` | |
| `_write_failures` | `0` | #189 |
| `_last_write_error` | `None`（`str \| None`） | 格式 `f"{type(exc).__name__}: {exc}"` |
| `_recreated_count` | `0` | |
| 副作用 | `os.makedirs(_wal_dir, exist_ok=True)` | wal.py:51 |

daemon 中唯一實例：`SerialwrapService.__init__` 內 `self._wal = WalWriter()`（service.py:231，無參數 → 用 `WAL_DIR`、R=64MiB）。

Properties：`wal_path`、`mirror_path`（無鎖）、`current_seq`（**取 `_lock`**，wal.py:62-65）。

### 1.3 WAL record schema（`append`，wal.py:115-173）

簽章：
```python
def append(self, *, com: str, direction: str, source: str, payload: bytes,
           cmd_id: str | None = None, loss_flag: bool = False,
           meta: dict[str, Any] | None = None) -> dict[str, Any]
```

鎖外先算：`payload_b64 = base64.b64encode(payload).decode("ascii")`（標準 alphabet、含 `=` padding、不換行）；`crc32 = zlib.crc32(payload) & 0xFFFFFFFF`。

鎖內：`rotation_failed = self._rotate_if_needed()` → `self._seq += 1` → 建 record：

| key（建構順序） | 型別 | 值 |
|---|---|---|
| `seq` | int | `self._seq`（遞增後） |
| `mono_ts_ns` | int | `time.monotonic_ns()`（Linux CLOCK_MONOTONIC，開機相對；跨重啟/跨主機不可比） |
| `wall_ts` | str | `datetime.now(tz=timezone.utc).isoformat()` |
| `com` | str | 呼叫端給的 COM（如 `"COM0"`；偵測用 probe bridge 為 `"PROBE"`） |
| `dir` | str | `"RX"` / `"TX"` / `"META"`（無驗證，任意字串都收） |
| `source` | str | 見 §1.6 |
| `cmd_id` | str \| null | |
| `len` | int | `len(payload)`（原始 bytes 長度） |
| `crc32` | **str** | `f"{crc32:08x}"`，8 位小寫 hex，空 payload＝`"00000000"` |
| `payload_b64` | str | 空 payload＝`""` |
| `loss_flag` | bool | `bool(loss_flag)`；寫入失敗時回傳物件被改成 `True`（但那筆根本沒寫進檔） |
| `meta` | object | `meta or {}`（None 或空 dict 都寫 `{}`） |
| `rotation_failed` | bool（**條件式**） | 只在 rotate 發生 OSError 時加 `true`；正常 record 無此 key |

`wall_ts` 格式細節（Rust 必須逐字對齊）：`2026-10-15T03:06:17.663778+00:00`——微秒 6 位、offset 寫 `+00:00`（不是 `Z`）。**當 microsecond == 0 時 Python `isoformat()` 省略小數部分**，輸出 `2026-10-15T03:06:17+00:00`。

序列化：`util.dumps_stable(obj) = json.dumps(obj, ensure_ascii=False, sort_keys=True, separators=(",", ":"))`（util.py:20-21），行尾 `"\n"`。因 `sort_keys=True`，實際 key 順序為：
`cmd_id, com, crc32, dir, len, loss_flag, meta, mono_ts_ns, payload_b64, [rotation_failed,] seq, source, wall_ts`；`meta` 內部 key 也排序。

**[實跑] 實際位元組範例**：
```
{"cmd_id":null,"com":"COM0","crc32":"a81bc28c","dir":"RX","len":14,"loss_flag":false,"meta":{},"mono_ts_ns":5798622898976,"payload_b64":"aGVsbG8NChtbMG0Lf/8=","seq":1,"source":"uart","wall_ts":"2026-10-15T03:06:17.663778+00:00"}
{"cmd_id":"c1","com":"COM0","crc32":"0e048d3e","dir":"TX","len":2,"loss_flag":false,"meta":{"a":"中","z":1},"mono_ts_ns":5798623076941,"payload_b64":"w6k=","seq":2,"source":"agent:x","wall_ts":"2026-10-15T03:06:17.663955+00:00"}
{"cmd_id":null,"com":"COM0","crc32":"00000000","dir":"META","len":0,"loss_flag":false,"meta":{"event":"x"},"mono_ts_ns":5798623144560,"payload_b64":"","seq":3,"source":"system","wall_ts":"2026-10-15T03:06:17.664022+00:00"}
```
（範例中 `source:"uart"` 是測試值；生產 RX 為 `"device"`。）

JSON 編碼注意（Rust serde_json 對齊點）：
- `ensure_ascii=False` → 非 ASCII 直接輸出 UTF-8（如 `"中"`）；控制字元 `\u00XX`，`\b \f \n \r \t` 用短跳脫；`/` 不跳脫；DEL(0x7f)、U+2028/2029 不跳脫。serde_json 預設行為一致。
- `meta` 內 float：Python `repr`（如 `rx_age_s: round(x,1)` → `31.5`；大數 `1e+16`），serde_json 會寫 `1e16`——若需逐位相容須自訂 formatter。
- Python `allow_nan=True` 預設，NaN/Infinity 會寫出非法 JSON token；目前 meta 來源不會產生，但無防護。

### 1.4 寫入路徑（BC-01 前現況）

`_write_record_locked(record, payload)`（wal.py:175-181），須持 `_lock`：
```python
with open(self._wal_path, "a", encoding="utf-8") as wal_fp:
    wal_fp.write(dumps_stable(record)); wal_fp.write("\n")
with open(self._mirror_path, "a", encoding="utf-8") as mirror_fp:
    mirror_fp.write(to_printable(payload))
```
- **每筆開關檔兩次**；無 `flush()` 明呼叫、無 `fsync`（關檔即 flush 到 OS）。
- Windows 下 Python `open()` 不帶 `FILE_SHARE_DELETE`。
- 檔案權限：依 umask（一般 0644；systemd system unit `UMask=0117` → 0660）。
- 順序：WAL 行先、mirror 後。

錯誤處理（wal.py:148-172）：
1. 第一次 `_write_record_locked` 拋 `OSError` → `os.makedirs(_wal_dir, exist_ok=True)` → 再寫一次。
2. 重試成功：`_recreated_count += 1`、`_last_write_error = "<ExcType>: <msg>"`（記的是**第一次**的例外）、`_LOG.warning("WAL 目錄 %s 消失，已重建並續寫（原因：%s）；消失期間的紀錄無法復原", ...)`。**不寫任何 META 紀錄**、不設 loss_flag。
3. 重試也失敗：`record["loss_flag"] = True`、`_write_failures += 1`、`_last_write_error` 記重試例外、`_LOG.warning("WAL append 寫入失敗（best-effort 略過）：%s", ...)`；仍回傳 record（seq 已消耗，形成 seq 缺口）。
4. 非 OSError 例外會逃出 append。

**邊角缺陷 [讀碼]**：若第一次寫入時 WAL 行已成功、但 mirror 開檔失敗（非 ENOENT 的 OSError，如 EACCES/ENOSPC），重試會把**同一 seq 的 WAL 行再寫一次**（重複 seq 行）。

**外部刪檔偵測現況 [讀碼]**：只刪 `raw.wal.ndjson`（目錄仍在）時，下一次 `open("a")` 直接建新檔，**完全沒有偵測**——seq 照舊累加、`_recreated_count` 不變、`health().healthy` 為 True。只有整個目錄被刪（ENOENT）才走上述自癒。規格 §8.2 的 `WAL_RECREATED_AFTER_EXTERNAL_UNLINK`／`potentially_lost_seq_range` META **尚未存在於程式碼**（grep 無結果；BC-01 待實作）。

### 1.5 mirror 格式（`util.to_printable`，util.py:24-33）

```python
printable = set(string.printable)  # digits+ascii_letters+punctuation+' \t\n\r\x0b\x0c'
for b in data:
    ch = chr(b)
    if ch in printable and ch not in {"\x0b", "\x0c"}: out.append(ch)
    else: out.append(f"\\x{b:02x}")
```
- 保留原樣：0x20-0x7E、`\t`(0x09)、`\n`(0x0a)、`\r`(0x0d)。
- 其餘（含 0x00-0x08、0x0b、0x0c、0x0e-0x1f、0x1b ESC、0x7f、0x80-0xff）→ 4 字元 `\xNN`（小寫 hex）。UTF-8 多位元組逐 byte 跳脫（`é` → `\xc3\xa9`）。
- **[實跑]** payload `b"hello\r\n\x1b[0m\x0b\x7f\xff"` + `"é"` → mirror 位元組 `hello\r\n\\x1b[0m\\x0b\\x7f\\xff\\xc3\\xa9`。
- 無分隔、無時間、無 COM、無方向；META（空 payload）不寫任何東西；TX（含 `source="flash"` 的燒錄位元組）也寫入。多 session 交錯混在同一檔。mirror 無法重建 WAL。

### 1.6 呼叫端與 `dir`/`source`/`meta` 值域（全 repo grep）

| 位置 | dir | source | cmd_id | meta |
|---|---|---|---|---|
| uart_io.py:682 `_handle_serial_rx` | `RX` | `"device"` | None | `{}` |
| uart_io.py:986 `send_bytes(..., log=True)` | `TX` | 呼叫端 `source`（agent 提交的任意字串、`human:<client_id>`、`system`、`system:recover`、`system:self_test`、`flash` 等） | 呼叫端 | `{}` |
| uart_io.py:1095 echo-ACK 停滯 | `TX` | 同上 | 同上 | `{}`；payload＝已送出 slices 合併（單筆稽核） |
| uart_io.py:1101 echo-ACK 完成 | `TX` | 同上 | 同上 | `{}`；payload＝全部 slices + `b"\n"` 合併成一筆 |
| session_manager.py:1217 | `META` | `"system"` | None | `{"event":"credentials_unresolved","reason":<auth_res.reason>,"env_file":<path or "">}` |
| session_manager.py:1316 | `META` | `"system"` | None | `{"event":"transport_stall_suspected","real_path":<str or "">,"rx_age_s":round(x,1)}` |
| session_manager.py:3755 | `META` | `"system"` | `cmd_id` | `{"event":"command_completion","completion_regex":<str>}` |

- META 的 payload 一律 `b""`（`len:0`、`crc32:"00000000"`、`payload_b64:""`）。
- 不寫 WAL 的 TX：`send_bytes(log=False)`——echo-ACK 分片（之後合併記一筆）、`source="system:secret"`（密碼，uart_io.py:1149）**刻意不記**；FLASHING 期間被 gate 丟棄的 bytes；被 deferred buffer 暫存的 human bytes（flush 時才經 send_bytes 記錄）。
- TX 紀錄在 `_write_all`/`port.write` **成功返回後**才 append，記整段 payload；寫入中途例外 → 不記錄（無部分長度紀錄）。
- RX 紀錄在 console fan-out、EventEngine、Login FSM 之前（uart_io.py:682-684），與規格 §8.1 RX 順序一致。
- RX observer 拿到的 `wal_seq` 是 `self._wal.current_seq`（session_manager.py:2393，另取一次鎖，可能已被其他 session 的 append 推進，非該 chunk 的精確 seq）。
- `BackgroundCapture.from_seq = self._wal.current_seq + 1`（session_manager.py:~3770）。
- EventEngine 透過 `SERIALWRAP_EVENT_WAL_SEQ` 把 `wal_seq` 傳給 handler（event_engine/dispatcher.py:66）。

### 1.7 seq 語意（Stage 0 凍結值）

| 情境 | 行為 | 依據 |
|---|---|---|
| 正常 append | `_seq += 1`，鎖內，與 `mono_ts_ns`/`wall_ts` 取值同一鎖段 → seq 與 mono_ts 同序 | wal.py:128-134 |
| 寫入失敗 | seq 仍消耗（形成缺口），record.loss_flag=True | wal.py:162 |
| 自動 rotate | **不重設**；新現行檔第一筆 seq＝舊檔最後 seq+1 | wal.py:87-113；tests/test_wal_integrity.py:49 |
| `wal.reset` | `_seq = 0`，下一筆 `seq=1` | wal.py:252；**[實跑]** |
| daemon 重啟 | `_load_last_seq()`：逐行掃**現行檔**，取 `isinstance(seq,int)` 的**最大值**（非最後一行）；無檔→0；壞行/非 dict/JSON 錯誤略過 | wal.py:67-85 |
| 重啟時現行檔不存在（剛 reset 未寫、或被外部刪除） | seq 從 0 重新開始 → 與歸檔檔 seq 重複 | 同上 |
| 讀取 `errors="replace"` | 非 UTF-8 位元組不會中止掃描 | wal.py:72 |

注意：`isinstance(True, int)` 為 True，`"seq": true` 會被當 1（無實務影響）。

### 1.8 Rotate（`_rotate_if_needed`，wal.py:87-113）

```python
for path in (self._wal_path, self._mirror_path):
    try:
        if not os.path.exists(path): continue
        if os.path.getsize(path) < self._rotate_bytes: continue
        ts = time.strftime("%Y%m%d-%H%M%S", time.gmtime())
        dst = f"{path}.{ts}"
        os.replace(path, dst)
        _fsync_dir(os.path.dirname(path))
    except OSError as exc:
        rotation_failed = True
        logging.getLogger("serialwrap").warning("WAL 輪替失敗（best-effort 續用既有檔案，不殺 reader）：%s：%s", path, exc)
return rotation_failed
```
- 每次 append 前呼叫（在 `_lock` 內）；每筆 2×`exists`＋2×`getsize`。
- 條件 `size >= R`（R＝67108864）。WAL/mirror 各自判斷、各自取 ts。
- 歸檔名：`raw.wal.ndjson.20261015-030617`、`raw.mirror.log.20261015-030617`（UTC）。不保留數量上限、不壓縮、不清理。
- 同秒碰撞：`os.replace` 覆蓋同名舊歸檔（POSIX 靜默、Windows 也會覆蓋）。
- 失敗只標記，繼續寫入既有（超大）檔；該筆 record 帶 `"rotation_failed": true`。
- 讀取 API（tail/range/available_from_seq/_load_last_seq）**只掃現行檔**，歸檔不在範圍。

### 1.9 Reset（`reset`，wal.py:242-253）

```python
with self._lock:
    prev_seq = self._seq
    ts = time.strftime("%Y%m%d-%H%M%S", time.gmtime())
    for path in (wal, mirror):
        if os.path.exists(path) and os.path.getsize(path) > 0:
            os.replace(path, f"{path}.{ts}")
    _fsync_dir(self._wal_dir)
    self._seq = 0
    return {"ok": True, "previous_seq": prev_seq, "rotated_suffix": ts}
```
- 與 rotate 差異：條件 `> 0`（非 `>= R`）；兩檔共用同一 ts；**不捕捉 OSError**（例外穿出 → RPC 層回 `{"ok":false,"error_code":"EXCEPTION","message":...}`，且 `_seq` 未重設）；即使沒檔可輪替也回 `ok:true` 且帶 `rotated_suffix`；`_fsync_dir` 無論是否輪替都執行（目錄不存在會拋）。
- RPC：`wal.reset` → `self._wal.reset()`（service.py:1045-1046），不在 `BLOCKING_RPC_METHODS`。CLI：`serialwrap wal reset`（cli.py:2222, 2669-2670）。

### 1.10 Health（`health`，wal.py:183-225）

**刻意不取 `_lock`**（避免 RPC loop 被資料平面拖住）。回傳：

| key | 型別 | 計算 |
|---|---|---|
| `wal_dir` | str | `_wal_dir` |
| `wal_path` | str | |
| `wal_dir_exists` | bool | `os.path.isdir` |
| `wal_file_exists` | bool | `os.path.exists(wal_path)` |
| `wal_dir_writable` | bool | `wal_dir_exists and os.access(dir, W_OK \| X_OK)` |
| `current_seq` | int | `self._seq`（無鎖快照） |
| `write_failures` | int | |
| `last_write_error` | str \| null | |
| `recreated_count` | int | |
| `healthy` | bool | `wal_dir_exists and wal_dir_writable and not (seq > 0 and not wal_file_exists)` |

`available_from_seq()`（wal.py:227-240）：現行檔第一筆有效 seq（`_iter_matching(None)` 第一個）；無檔/無有效紀錄/OSError → `None`。

### 1.11 查詢 API（WalWriter 層）

`_iter_matching(com)`（wal.py:255-275）：逐行讀現行檔（`errors="replace"`）、strip、空行略過、JSON 錯誤略過、非 dict 略過、`seq` 非 int 略過、`com` 為 truthy 且 `obj.get("com") != com` 略過。檔案不存在會拋 `FileNotFoundError`（呼叫端先檢查 exists）。

`tail_raw_with_meta(*, from_seq: int | None = None, com: str | None = None, limit: int = 200) -> tuple[list[dict], bool]`（wal.py:292-328）：
- 檔不存在 → `([], False)`。
- **latest 模式**（`from_seq is None`）：`deque(maxlen=max(limit, 0))` 收最後 limit 筆（seq 升冪，依檔內順序）；`truncated = total > len(rows)`（視窗**之前**還有更舊）。limit ≤ 0 → 空、truncated=total>0。
- **range 模式**（`from_seq=N`，含 0）：略過 `seq <= N`；收到 limit 筆後再遇到一筆符合者 → `truncated=True` 並 break（視窗**之後**還有）。limit ≤ 0 → 第一筆符合即 truncated=True、回空。
- 依「檔內出現順序」而非 seq 排序；reset 後重啟等情況下現行檔內 seq 永遠遞增，故等價。
- 全檔掃描 O(n)，64 MiB 檔每次查詢都整檔讀。

`tail_raw(...)` → 只回 rows；`tail_text(...)` → `rows_to_text_lines(rows)`（limit 計紀錄筆數非行數）。

`rows_to_text_lines(rows)`（wal.py:338-355）：每筆 `base64.b64decode(payload_b64, validate=False)` → `to_printable` → 串接 → `text.splitlines()`；若 text 以 `\n` 結尾直接回 lines；否則以 `consumed = sum(len(line)) + (len(lines)-1)` 補「partial 尾段」。
- **已確認缺陷 [實跑]**：`consumed` 假設每個行分隔為 1 字元；遇到 `\r\n`（2 字元）或其他 splitlines 認得的分隔時少算，導致最後一行尾端字元被**重複附加**成額外一行。例：`["hello\r\n…\\xc3\\xa9"]` 輸出 `['hello', '\\x1b[0m\\x0b\\x7f\\xff\\xc3\\xa9', '9']`（多出 `'9'`）。注意 `splitlines()` 也會在 `\x1c \x1d \x1e \x85  ` 等切行，但 to_printable 已把這些轉成 `\xNN`，實務上只剩 `\r`、`\r\n`、`\n`。Rust 1:1 需決定是否複製此缺陷（建議登記為契約例外並修正）。

### 1.12 WAL 相關 RPC／CLI（`sw_core/service.py`、`sw_core/cli.py`）

所有 RPC 回應另由 `SerialwrapService.rpc()` `setdefault("version", self._version)`（service.py:690-703）與 transport 補 `"id"`；handler 例外 → `{"ok":false,"error_code":"EXCEPTION","message":str(exc)}`（rpc_posix.py:58-59，Windows 共用 `serve_connection`）。

`_wal_missing_response(health)`（service.py:538-560）：`wal_file_exists` 為真或 `current_seq <= 0` → None；否則回
```json
{"ok":false,"error_code":"WAL_MISSING","wal_path":...,"wal_dir":...,"wal_dir_exists":bool,"wal_file_exists":false,"current_seq":int,"hint":"WAL 已寫過 N 筆紀錄，但現行 WAL 檔不存在——…"}
```

#### `log.tail_raw` / `log.tail_text`（service.py:964-1019）
- params：`com` 或 `selector`（`str(com or selector or "")`）；`from_seq`：key 缺或 JSON `null` → latest 模式；否則 `int()`，失敗 → `{"ok":false,"error_code":"INVALID_ARGS"}`；`limit = int(params.get("limit") or 200)`（0 → 200）。
- selector 非空 → `get_session_state(selector)`，失敗原樣回傳；取 `session.com` 當 `com` 過濾（`PROBE` 紀錄不會出現）。
- 先 WAL_MISSING 檢查。
- 回應：`tail_raw` → `{"ok":true,"records":[...],"returned":len(rows),"from_seq":N|null,"last_seq":rows[-1].seq|null,"current_seq":<health 快照>,"truncated":bool,"wal_path":str,"wal_file_exists":bool}`；`tail_text` → 同上但 `"lines":[...]`、`returned=len(lines)`。
- 兩者在 `BLOCKING_RPC_METHODS`（daemon.py:31，跑 executor）。
- CLI：`serialwrap log tail-raw|tail-text [--selector S] [--com C] [--from-seq N] [--limit 200]`；`--from-seq` 預設 None → 不送 key（cli.py:2162-2181, 2626-2637）。

#### `wal.range`（service.py:1021-1043）
- params：`from_seq = int(params.get("from_seq") or 0)`、`to_seq = int(params.get("to_seq") or 0)`、`limit = int(params.get("limit") or 1000)`；非整數 → `int()` ValueError → `EXCEPTION`（不是 INVALID_ARGS）。
- WAL_MISSING 檢查 → `tail_raw(from_seq=from_seq, com=None, limit=limit)`（**永遠 range 模式**，含 PROBE）→ `to_seq > 0` 時**在 limit 之後**再過濾 `seq <= to_seq`。
- 回應：`{"ok":true,"records":[...],"wal_path":str,"wal_file_exists":bool,"available_from_seq":int|null,"rotated_out":bool}`，`rotated_out = available is not None and from_seq + 1 < available`。
- **沒有** `truncated`、`returned`、`last_seq`、`next_from_seq`。分頁唯一方法：以上頁最後 `seq` 當下一頁 `from_seq`，直到回傳筆數 < limit（或 0）。
- 在 `BLOCKING_RPC_METHODS`。CLI：`serialwrap wal export [--from-seq 0] [--to-seq 0] [--limit 1000]`（cli.py:2218-2221, 2666-2667）。

#### `wal.current_seq`（service.py:1048-1049）
`{"ok":true,"seq":int}`；經 `current_seq` property **取寫鎖**。CLI：`serialwrap wal current-seq`。

#### `result.tail` legacy（無 `cmd_id`，service.py:939-962）
`legacy_from_seq = int(params.get("from_seq") or 0)`、`limit = int(... or 200)`；selector → com；`tail_raw(from_seq=..., com=..., limit=...)`；回 `{"ok":true,"records":rows}`；**不做 WAL_MISSING 檢查、無 metadata**。CLI `serialwrap stream tail [--selector] [--com] [--from-seq 0] [--limit 200]`（cli.py:2150-2154, 2619-2624）。

#### `health.status` 中的 WAL 欄位（service.py:562-590）
`"wal_path"`、`"mirror_path"`、`"wal": <WalWriter.health()>`。在 `BLOCKING_RPC_METHODS`。doctor 的 `wal_writable`（非 advisory）與 `wal_dir`（advisory，shell `SERIALWRAP_WAL_DIR` 與 daemon 回報不一致 WARN）消費此欄位（doctor_cmd.py:340-410）。

---

## 2. `state.json`（`sw_core/session_manager.py`）

### 2.1 路徑

- `STATE_PATH = os.path.join(STATE_DIR, "state.json")`（constants.py:59）。
- `SessionManager.__init__(..., state_path: str | None = None)`：`self._state_path = STATE_PATH if state_path is None else state_path`（session_manager.py:658，建構時讀模組層）。daemon 路徑 `SerialwrapService(...)` 未傳 state_path（daemon.py `_run_async`）→ 用 `STATE_PATH`。
- 舊版路徑 `/tmp/serialwrap/state.json`（setup_cmd.py:153，只偵測、提示，不搬）。

### 2.2 完整 schema（寫出端 `_save_state`，session_manager.py:804-863）

```json
{
  "aliases":          { "<alias>": AliasRow, ... },
  "bindings":         { "<session_id>": "<device_by_id>", ... },
  "profile_detected": { "<by_id>": "<profile_name>", ... },
  "profile_pins":     { "<device_key>": "<profile_name>", ... },
  "released":         { "<session_id>": ReleasedRow, ... }
}
```
- 序列化：`json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":")) + "\n"`（單行、key 排序、尾端換行）。
- **無 `version`／`schema_version` 欄位**；5 個 key 一律輸出（空也寫 `{}`）。

| 結構 | 欄位 | 型別 | 來源 |
|---|---|---|---|
| `AliasRow`（session 型） | `session_id` | str | `AliasRegistry.set_for_session` |
| | `updated_at` | str（`now_iso()`） | |
| `AliasRow`（by-id 型） | `device_by_id` | str | `AliasRegistry.assign_by_id` |
| | `updated_at` | str | |
| | `profile` | str（**條件式**：`profile` truthy 才有） | |
| `ReleasedRow` | `by_id` | str（`s.profile.device_by_id`，可為 `""`） | |
| | `released_by` | str \| null（`source`，預設 `"cli"`；如 `"agent:flash"`） | |
| | `released_at` | str \| null（`now_iso()`） | |
| | `reason` | str \| null | |

- `session_id` 格式：`f"{profile_name}:{com}"`（如 `prpl-template:COM0`）。
- `device_key`（pins）：`_resolve_device_key()` 結果——session 的 `device_by_id`，或直接給的 `/dev/serial/by-id/...`／`/dev/serial/by-path/...` 字串（session_manager.py:2088-2104）。
- `profile_detected` key 為 by_id（`_maybe_persist_sticky`，session_manager.py:2761-2773）。

**[實跑] 範例**：
```
{"aliases":{"custom":{"session_id":"p:COM5","updated_at":"2026-10-15T03:07:39.596535+00:00"},"lab+1":{"session_id":"p:COM5","updated_at":"..."},"prpl-template+2":{"session_id":"prpl-template:COM0","updated_at":"..."},"zz":{"device_by_id":"/dev/serial/by-id/Z","profile":"prpl-template","updated_at":"..."}},"bindings":{},"profile_detected":{},"profile_pins":{"/dev/serial/by-id/X":"prpl-template"},"released":{"prpl-template:COM0":{"by_id":"/dev/serial/by-id/X","reason":"r","released_at":"t","released_by":"cli"}}}
```

### 2.3 寫入程序（原子寫）

1. `os.makedirs(state_dir, exist_ok=True)`。
2. 持 `self._lock`（RLock）建 snapshot：`released` 由 `self._sessions` 中 `state == "RELEASED"` 者即時組出（**不是**從 `_loaded_released` 或 `_released_by_ids`）。
3. 持 `self._state_io_lock`（#133，獨立 Lock，鎖序 `_lock → _state_io_lock`）：
   - `tempfile.mkstemp(dir=state_dir, prefix="state.json.tmp.")`（**權限 0600**，最終 state.json 也是 0600）
   - write → `fp.flush()` → `os.fsync(fp.fileno())`
   - `_replace_state_file(tmp, dst)`（session_manager.py:77-98）：非 Windows 直接 `os.replace`；Windows 對 `PermissionError` 重試 6 次，延遲 0.01→0.02→0.04→0.08→0.16 秒（共約 0.31 s），第 6 次失敗上拋。
   - `sys.platform != "win32"` 時目錄 fsync。
   - finally：tmp 若仍存在則刪除。
4. **失敗不捕捉**：例外穿出至呼叫端（多數為 RPC handler → `EXCEPTION`）。注意守衛不一致：wal.py 用 `os.name == "nt"`，這裡用 `sys.platform != "win32"`。

`_save_state()` 呼叫點：`__init__` 尾（732）、`set_alias_for_session`（1723，**持 _lock 內**）、`assign_alias`（1728）、`unassign_alias`（1733，**先存檔再判斷 not found**）、`clear_session`（1847）、`release_device`（1875）、`attach_device`（2030）、`bind_session`（2080，持 _lock 內）、`pin_session`（2121）、`unpin_session`（2132）、`_attach_by_id` DETACHED-rebind（2574）、`_maybe_persist_sticky`（2773）、re-resolve pin/detected（2935, 2981）。

### 2.4 讀取程序（`_load_state`，session_manager.py:734-802）

1. 檔不存在 → return（空狀態）。
2. `open(..., "rb").read()` 拋 `OSError` → **`raise StateLoadError(...)`**（fail closed；daemon.py 攔截後釋放 lock、非零退出，拒絕啟動，#82）。
3. `json.loads(raw)` 拋 `JSONDecodeError`/`UnicodeDecodeError`/`ValueError` → `os.replace(state, state + ".corrupt")`（覆蓋舊 `.corrupt`；失敗則 backup=None）→ warning → return（之後 `__init__` 以空狀態寫新檔）。`json.loads(bytes)` 會自動偵測 UTF-8/16/32。
4. 合法 JSON 但非 dict（如 list）→ 各 key 皆 None → 視為空，**不備份**，隨即被覆寫。
5. 逐 key 解析（容錯，型別不符即略過）：
   - `aliases`：dict → `AliasRegistry.load(rows)`（`{k: dict(v)}`；若 v 非 dict 會在 `dict(v)` 拋例外 → 穿出 `__init__`）。
   - `bindings`：只收 `str→str` 且 strip 後非空，存 strip 後值。
   - `released`：dict；每筆 sid 為 str 且 meta 為 dict 才收；取 `by_id/released_by/released_at/reason`（不驗型別）；`by_id` 為非空 str 才加入 `_released_by_ids`；存入 `_loaded_released`。
   - `profile_pins`、`profile_detected`：只收 `str→str` 且 strip 非空。
6. 未知 key 忽略並在下次存檔時消失。

### 2.5 `__init__` 對持久化狀態的套用（session_manager.py:701-732）

1. `_load_state()`。
2. 對每個 YAML target profile：`sid = f"{p.profile_name}:{p.com}"`；若 `bindings[sid]` 存在、≠ 自己宣告的 `device_by_id`、且屬於**其他 target 宣告的 by_id** → 刪除該 override（#186）；`device_by_id = override or p.device_by_id`；空則略過；建 `SessionRuntime`、`profile_source="yaml-target"`；`self._aliases.set_for_session(sid, profile.alias)`（每次啟動都以 YAML alias 重寫一列、更新 `updated_at`）。
3. `_loaded_released` 中 sid 在 `self._sessions` 者 → `state="RELEASED"`、`released_by/at/reason` 套回（**`by_id` 不套回 profile**）。
4. `_save_state()`（此時 `released` 只含第 3 步套上的 YAML session）。

### 2.6 Released 持久化語意與已知缺陷

- `release_device(selector, *, source="cli", reason=None)`（session_manager.py:1852-1876）：持鎖內 detach、`state="RELEASED"`、`released_by=source`、`released_at=now_iso()`、`released_reason=reason`、加入 `_released_by_ids` → 釋鎖 → `_save_state()` → `{"ok":true,"session":...,"closed_consoles":int,"aborted_cmd":bool}`。已 RELEASED → `{"ok":true,"already_released":true,...}`；FLASHING/flash-critical → `FLASHING_BUSY`。
- 存檔失敗：例外穿出 → RPC 回 `EXCEPTION`（未回 ok），但記憶體已是 RELEASED（自動 attach 仍被 `_released_by_ids` 擋）——行為大致符合規格 §6.2「提交失敗不得回成功」，但沒有專用 error_code／診斷欄位。
- `attach_device`：清 `_released_by_ids`、released_* 歸 None、`state="ATTACHING"` → 存檔 → spawn attach（session_manager.py:2020-2032）。
- `bind_session` 對 RELEASED session 視為明確離開 RELEASED（I1，2063-2075）。
- `_spawn_attach` 對 `_released_by_ids` 中的 by_id 直接 return（2290-2293）；`prepare_dynamic_rank` 也排除。
- 動態 session 改名（`_rematerialize_profile_locked`）會搬移 `_binding_overrides` 與 `_loaded_released` 的 key（2855-2860）。

**已確認缺陷 [實跑]：動態 session 的 RELEASED 只撐一次重啟。** 動態 session 不在 YAML，`__init__` 時不存在 → 第 3 步套不上 → 第 4 步 `_save_state` 寫出 `"released":{}`。第一次重啟後的那個 daemon 仍因 `_released_by_ids`（記憶體）擋住自動 attach，但**第二次重啟**時 `released` 已空，`_released_by_ids` 為空 → 裝置會被自動 attach（two-reader 風險）。現有測試（tests/test_device_handoff.py:255 `TestReleasedPersistence`）只覆蓋 YAML target。規格 §6.2／§9.4 要求 released 「能在 daemon 重啟後維持禁止自動 attach」，Rust 版須決定是否修正（建議登記 BC）。

### 2.7 Alias 持久化（`sw_core/alias_registry.py`，50 行）

```python
class AliasRegistry:
    _lock: threading.Lock; _rows: dict[str, dict[str, Any]]   # key = alias
    load(rows)            # 深一層複製
    dump() -> dict        # 深一層複製
    list_alias() -> list  # sorted(_rows.items()) 依 alias 字典序，每列複製並加 "alias": alias
    set_for_session(session_id, alias)        # _rows[alias] = {"session_id", "updated_at": now_iso()}
    assign_by_id(by_id, alias, profile=None)  # _rows[alias] = {"device_by_id", "updated_at"[, "profile"]}
    unassign(alias) -> bool                   # pop
```
RPC（service.py:871-889）：
- `alias.list` → `{"ok":true,"aliases": list_alias()}`。
- `alias.set {session_id, alias}` → `set_alias_for_session`：session 不存在 → `SESSION_NOT_FOUND`；否則 `session.profile = replace(alias=alias)`、registry 寫列、存檔 → `{"ok":true,"session_id","alias"}`。**不驗證 alias 非空、不檢查重複、不移除舊 alias 列**。
- `alias.assign {by_id, alias, profile?}` → 缺 by_id/alias → `INVALID_ARGS`；`{"ok":true,"alias","device_by_id"}`。
- `alias.unassign {alias}` → 先存檔；不存在 → `ALIAS_NOT_FOUND`；`{"ok":true,"alias"}`。

**已確認缺陷 [實跑]**：registry 只被寫入與列出，**沒有任何程式碼讀 registry 來決定 session alias 或解析 selector**。`get_session()` 只比對 `session_id`、`profile.com`、`profile.alias`。因此：
- `alias.set` 的結果重啟後失效（session alias 回到 YAML 的 `alias` 或 `f"{profile_name}+{act_no}"`），但 `state.json` 仍留著 `custom → session_id` 列；`alias.list` 會列出已失效 alias。
- `alias.assign`（by-id 型）完全不影響動態 session 命名。
- 列只增不減（除 `alias.unassign` 與 rematerialize 時 `unassign(old_alias)`）。
CLAUDE.md「執行期 `alias.*` 的結果會覆寫到持久化狀態」只對「寫入」成立，不對「生效」成立。

### 2.8 Binding／Profile pin／sticky

- `bindings`：`bind_session`（2079）、`_attach_by_id` DETACHED-rebind fallback（2562）、rematerialize 改名（2855）寫入；`__init__` #186 清除。
- `profile_pins`：`pin_session(selector, profile_name)`（template 不存在→`UNKNOWN_PROFILE`（session_manager.py:2112-2113）；device_key 無→`DEVICE_NOT_FOUND`；yaml-target→`PROFILE_IS_EXPLICIT`）；`unpin_session`。
- `profile_detected`：只在 `profile_source == "detected"` 且達 READY、`real_path` 與 attach 時一致時寫（#95 TOCTOU）。
- 動態 profile 解析優先序（session_manager.py:2997-3033）：`pin > sticky(profile_detected) > detect（PROBE bridge，WAL com="PROBE"）> fallback(passthrough)`；`profile_source` 值域：`unknown`、`yaml-target`、`pin`、`sticky`、`detected`、`fallback`。

### 2.9 版本化／遷移

- 無 schema 版本、無升降版 migration、無備份還原（除 `.corrupt`）。
- `sw_core/state_migrate.py`（13 行）：
  ```python
  def migrate_legacy_state(legacy, dest) -> bool:
      legacy, dest = Path(legacy), Path(dest)
      if not legacy.exists() or dest.exists(): return False
      dest.parent.mkdir(parents=True, exist_ok=True)
      shutil.copy2(legacy, dest)   # 複製（非搬移），保留 mtime/權限
      return True
  ```
  唯一呼叫：`setup_cmd.reconcile()` 跨 scope 模式轉換（setup_cmd.py:617-624），`try/except Exception: pass`。
- `_state_path_for(mode, home)`（setup_cmd.py:394-411）：`systemd-system` → `/var/lib/serialwrap/state.json`；其餘 → `${XDG_STATE_HOME:-home/.local/state}/serialwrap/state.json`。**忽略 `SERIALWRAP_STATE_DIR`**。
- **不一致 [讀碼]**：system unit（systemd_units.py:42-80）ExecStart＝`<serialwrapd> --socket /run/serialwrap/serialwrapd.sock --profile-dir /etc/serialwrap/profiles`，`User=<安裝者>`，有 `StateDirectory=serialwrap` 但**未設 `SERIALWRAP_STATE_DIR`**，而程式只讀 `SERIALWRAP_STATE_DIR`/`XDG_STATE_HOME`（不讀 systemd 的 `$STATE_DIRECTORY`）→ system daemon 實際 state.json 在 `~<user>/.local/state/serialwrap/state.json`，setup 遷移到 `/var/lib/serialwrap/state.json` 的檔永遠不會被讀。lock 預設 `RUN_DIR`（system service 無 `XDG_RUNTIME_DIR` → `~/.local/state/serialwrap/run/serialwrapd.lock`）。未在實機驗證。

---

## 3. Bench 狀態

### 3.1 `benches.yaml`（`sw_core/bench_registry.py`，174 行；唯讀設定）

- 路徑：`_default_benches_path()` = `_env_path("SERIALWRAP_BENCHES_FILE", <CONFIG_DIR>/benches.yaml)`；`_default_config_dir()` = `_env_path("SERIALWRAP_CONFIG_DIR", ${XDG_CONFIG_HOME:-~/.config}/serialwrap)`。**呼叫時**解析（非 import 時）。
- `@dataclass(frozen=True) class BenchEntry: target: str; remote_socket: str; local_port: int; ssh_opts: tuple[str, ...] = (); autossh: bool = False`。
- 根 key 白名單 `_ALLOWED_ROOT_KEYS = {"benches"}`；entry key 白名單且**全部必填** `_ALLOWED_ENTRY_KEYS = {"target","remote_socket","local_port","ssh_opts","autossh"}`（dataclass 預設值在 YAML 層不適用）。
- 驗證：`target` 非空字串、strip 後、恰一個 `@`、user/host 非空且前後無空白；`remote_socket` 非空字串（strip 後值）；`local_port` 為 int 非 bool、1..65535；`ssh_opts` 為 str list；`autossh` 為 bool；bench code 為非空字串（strip 後）。錯誤皆 `ValueError`（繁中訊息）。
- 檔不存在 → `{}`；YAML 為 null → `{}`；`benches` 缺 → `{}`。
- `to_remote_argv(entry)` → `["-L","--remote-socket",<remote_socket>,["--autossh"],*[f"--ssh-opt={o}"],f"{target}:{local_port}"]`。
- `resolve(code)` → 不存在拋 `KeyError(f"unknown bench code: {code}")`。

### 3.2 `benches.state.json`（`sw_core/cli.py`，CLI 端記憶 endpoint）

- 路徑 `_bench_state_path()`（cli.py:1115-1122）：`SERIALWRAP_STATE_DIR`（None 或空白 → `${XDG_STATE_HOME:-~/.local/state}/serialwrap`）→ `expanduser` → `/benches.state.json`。**呼叫時**解析。
- 鎖 `_lock_bench_state_doc`（cli.py:1125-1145）：POSIX＝`<path>.lock`（`O_RDWR|O_CREAT`, 0600）`flock(LOCK_EX)` ＋ 行程內 `_BENCH_STATE_IO_LOCK`；Windows 只用行程內鎖（無跨行程互斥）。
- 讀 `_load_bench_state_doc`（1148-1161）：檔不存在 → `({}, 同一個 {})`；`json.load`；非 dict → `ValueError("benches.state.json 必須是 object")`；**`benches` key 缺 → 回 `(payload, payload)`，即頂層本身當 benches map**；`benches` 非 dict → ValueError。
- 寫 `_write_bench_state_doc`（1213-1232）：`mkstemp(prefix="benches.state.json.tmp.")`（0600）→ `json.dump(sort_keys=True, ensure_ascii=False, separators=(",",":"))`（**無尾端換行、無 fsync、無目錄 fsync**）→ `os.replace`。
- **實際 schema [實跑]**：全新檔寫出為**扁平** `{"B1":"tcp://127.0.0.1:49001"}`（code → endpoint）；只有既有檔本來就含 `benches` 物件時才寫成巢狀 `{"benches":{...}}`。兩種形態讀取皆相容。
- endpoint 驗證 `_remembered_bench_endpoint`：必須非空字串、`_parse_endpoint` 成功、transport `tcp`、host ∈ `LOOPBACK_TCP_HOSTS = {"127.0.0.1","localhost","::1"}`，否則 ValueError「endpoint 記憶損壞」。
- 錯誤碼映射：ValueError → `INVALID_BENCH_STATE`、OSError → `BENCH_STATE_IO_ERROR`；`--bench` 未記住 → `BENCH_ENDPOINT_NOT_REMEMBERED`（cli.py:757-761）。

---

## 4. `config.yaml`（`sw_core/runtime_config.py`，38 行）

- 路徑：`os.path.join(CONFIG_DIR, "config.yaml")`（cli.py:312、daemon.py:84、device_source.py:29、cli.py:1775）。setup `reconcile()` 自身 fallback 為 `home/.config/serialwrap/config.yaml`，但 CLI 一律傳 CONFIG_DIR 版本。
- `RuntimeConfig(path)`：檔存在 → `yaml.safe_load`；非 dict → `{}`；**YAML 語法錯誤不捕捉**（CLI 用 `_safe_runtime_config()` 包成 None；daemon 寫入端整段 try/except）。
- 方法：`mode() -> str | None`（`supervision_mode`）、`socket_path() -> str | None`（`socket_path`）、`set_mode(mode, *, socket_path=None)`、`set_socket(endpoint)`。
- 寫：保留讀到的其他 key，`yaml.safe_dump(self._data, allow_unicode=True)`（**預設 sort_keys=True**、block style、無註解保留）、`write_text` 直接覆寫（**非原子**、無 fsync）。
- **[實跑]** 範例：`'socket_path: tcp://127.0.0.1:48700\nsupervision_mode: on-demand\n'`。

已知 key：

| key | 型別 | 寫入者 | 讀取者 / 語意 |
|---|---|---|---|
| `supervision_mode` | str：`"on-demand"`／`"systemd-user"`／`"systemd-system"`（無驗證） | `setup`／`supervision-mode` 經 `reconcile()` | 缺省視為 `"on-demand"`（cli.py:318, 455, 629, 773, 1684, 2685）；`should_auto_spawn` 只在 on-demand 為真 |
| `socket_path` | str（unix 路徑或 `tcp://host:port`） | `reconcile()`（`SOCKET_PATH`／`SYSTEM_SOCKET`／保留既有 tcp），以及 **daemon 每次啟動成功都 `set_socket(args.socket)`**（daemon.py:76-98，#173，全平台） | CLI `_resolve_endpoint`；不可連時依 mode fallback canonical（`systemd-system`→`/run/serialwrap/serialwrapd.sock`，其餘 `_local_default_endpoint()`），CLI 不改寫 |
| `windows.exclude_coms` | list（元素 `str()`） | 手動 | `device_source._load_exclude_coms()`；任何錯誤 → 空集合 |

- 程式中**不存在** `windows-task`／`windows-service` 模式值（規格 §6.1 為目標設計）。
- endpoint 解析順序（cli.py 約 727-784）：`--endpoint` > `--socket` > `--bench`（benches.state.json）> config.yaml `socket_path` > `_local_default_endpoint()`。

---

## 5. Profiles YAML（`sw_core/config.py`，373 行）

### 5.1 Dataclass（皆 `frozen=True`）

`UartProfile`（config.py:11-18）：`baud: int = 115200`、`data_bits: int = 8`、`parity: str = "N"`、`stop_bits: int = 1`、`flow_control: str = "none"`、`xonxoff: bool = False`。

`ProfileTemplate`（21-47）與 `SessionProfile`（50-85）共同欄位與預設：

| 欄位 | 型別 | 預設 |
|---|---|---|
| `platform` | str | Template `"prpl"`；SessionProfile 必填 |
| `prompt_regex` | str | `r"(?m)^root@prplOS:.*# "` |
| `login_regex` | str | `r"(?mi)^login:\\s*$"` ⚠ |
| `password_regex` | str | `r"(?mi)^password:\\s*$"` ⚠ |
| `post_login_cmd` | str | `""` |
| `ready_probe` | str | `"echo __READY__${nonce}"` |
| `username` | str \| None | None |
| `user_env` | str \| None | None |
| `pass_env` | str \| None | None |
| `env_file` | str \| None | None |
| `console_unlock_env` | str \| None | None |
| `console_unlock_idle_s` | float | 5.0 |
| `timeout_s` | float | 10.0 |
| `quiet_window_s` | float | 2.0 |
| `hard_timeout_s` | float | 60.0 |
| `log_dir` | str \| None | None |
| `uart` | UartProfile | `UartProfile()` |
| `bootloader_prompts` | tuple[str, ...] | `()` |
| `max_console_line_chars` | int \| None | None |

`ProfileTemplate` 另有 `profile_name: str`（必填）；`SessionProfile` 另有必填 `profile_name, com, act_no: int, alias, device_by_id, platform`。兩者 `command_capable` property ＝ `bool((ready_probe or "").strip())`。

⚠ **程式碼預設 regex 缺陷 [實跑]**：Python raw string `r"(?mi)^login:\\s*$"` 的實際字元是 `\\s`，regex 語意為「字面反斜線＋任意個 s」，`re.search(p, "login: ")` → False、`"login:\\"` → True。出貨資產 YAML 寫 `"(?mi)^login:\\s*$"`（YAML 雙引號 → `\s`）才是正確的。只有 YAML 省略 `login_regex`/`password_regex`（或給空字串）時才會落到這個壞預設。Rust 移植須決定逐字保留或修正（建議登記）。

### 5.2 檔案載入 `load_profiles(profile_dir) -> LoadResult`（config.py:314-373）

`@dataclass(frozen=True) class LoadResult: profiles: list[SessionProfile]; templates: list[ProfileTemplate]; max_sessions: int`。

- `max_sessions` 初值 16；目錄不存在 → 空結果。
- `sorted(os.listdir(profile_dir))` 中 `.yaml`/`.yml` 檔逐一 `yaml.safe_load(...) or {}`；非 dict 略過。
- `base_dir = dirname(path)`（相對路徑解析基準）。
- `defaults`（dict 才用）：`log_dir`（`_resolve_opt_path`）、`max_sessions`（`_as_int`，**後讀檔覆蓋前者**）。
- `_load_templates`：見 §5.3。
- **`all_templates = ordered_templates` 每檔覆蓋**：多個 profile 檔時，只有**字典序最後一個檔**的 templates 進入 `LoadResult.templates`（動態偵測用）；`profiles`（explicit targets）則跨檔累加。[讀碼]
- `targets`：None → 該檔不產生 explicit session；非 list → 略過；每個 dict 元素：
  - `profile_ref = str(t.get("profile") or t.get("profile_name") or default_profile_name).strip()`；找不到 template → 略過。
  - `act_no = _as_int(t.get("act_no", idx), idx)`（idx 從 1）。
  - `com = str(t.get("com") or f"COM{max(act_no - 1, 0)}").strip()`。
  - `device_by_id = str(t.get("device_by_id") or "").strip()`；空 → **略過**（explicit target 必須有 device_by_id）。
  - `alias = str(t.get("alias") or f"{tpl.profile_name}+{act_no}").strip()`。
  - `_merge_session(...)`。

### 5.3 Template 解析（`_load_templates`，193-240；`_template_from_dict`，166-190）

- `default_name = str(obj.get("profile_name") or splitext(file_name)[0]).strip()`。
- `profiles` 為 dict → key＝名稱（strip、空略過、值非 dict 略過）；為 list → 每列 `name` 或 `profile_name`。
- 若無任何 template → 以**整份檔頂層**當單一 template（名稱 default_name）。
- default_name 不在 templates → 取 `sorted(keys)[0]`。
- 排序：`ordered.sort(key=lambda t: 1 if t.platform == "passthrough" else 0)`（stable，passthrough 置後）。
- 各欄解析：

| YAML key（含別名） | 解析 |
|---|---|
| `platform` | `str(v or "prpl").strip().lower()` |
| `prompt_regex` | `str(v or 預設)`（**不 strip**） |
| `login_regex` / `password_regex` | `str(v or 預設).strip()` |
| `post_login_cmd` | `str(v or "").strip()` |
| `ready_probe` | `_as_str_keep_empty`：None → 預設；否則 `str(v).strip()`（空字串保留 → console-only） |
| `username` | `_as_opt_str`（None/空白 → None） |
| `user_env` ← `user_env` \| `username_env` \| `login_env` | 第一個 truthy，`_as_opt_str` |
| `pass_env` ← `pass_env` \| `password_env` \| `pw_env` | 同上 |
| `env_file` | `_resolve_opt_path`（expanduser；絕對→normpath；相對→相對 YAML 目錄） |
| `console_unlock_env` | `_as_opt_str` |
| `console_unlock_idle_s` | `_as_unlock_idle`：None→5.0；bool/非數/非有限/≤0 → **ValueError**（整個 daemon 載入失敗） |
| `timeout_s` / `quiet_window_s` / `hard_timeout_s` | `_as_float(v, 10.0/2.0/60.0)`（轉換失敗回預設） |
| `log_dir` | `_resolve_opt_path` |
| `max_console_line_chars` | `_as_max_console_line_chars`：None→None；非 int、bool、≤0 → **ValueError** |
| `uart` | `_load_uart`：非 dict→全預設；`baud/data_bits/stop_bits` `_as_int`；`parity` `str(v or base).upper()`；`flow_control` `str(v or base).lower()`；`xonxoff` `bool(obj.get("xonxoff", base))` |
| `bootloader_prompts` | list → 只保留 str 元素的 tuple；否則 `()` |

### 5.4 Target 合併（`_merge_session`，243-303）

target 層可覆寫：`platform, prompt_regex, login_regex, password_regex, post_login_cmd, ready_probe, username, user_env(+別名), pass_env(+別名), env_file, console_unlock_env, console_unlock_idle_s, timeout_s, quiet_window_s, hard_timeout_s, log_dir, max_console_line_chars, uart`（uart 以 template.uart 為底逐欄合併）。**不可覆寫 `bootloader_prompts`**（一律用 template）。
- 「key 存在」判斷（可用 null 清成 None）：`user_env`/`pass_env`（任一別名在）、`env_file`、`console_unlock_env`、`log_dir`、`max_console_line_chars`；`username` 用 `is not None`；其餘用 `or` 退回 template（空字串 → template 值）。
- `log_dir` 優先序：target（key 存在時）> template > `defaults.log_dir`，且三者皆 None/空時由 `_resolve_log_dir` 退到 `LOG_DIR`（session_manager.py:4748-4749）。
- **動態 session 不套用 `defaults.log_dir`** [讀碼]：`_session_from_template` 直接用 `tpl.log_dir`（session_manager.py:~980），與 CLAUDE.md 描述的優先序不符。
- capture 檔名 `f"{com}_{time.strftime('%y%m%d-%H%M%S')}.log"`（**本地時間**，與 WAL 輪替的 UTC 不同；session_manager.py:4780-4781）。

### 5.5 動態 session 命名
`com = _pending_com.pop(by_id) or _next_dynamic_com()`；`act_no = len(self._sessions) + 1`；`alias = f"{tpl.profile_name}+{act_no}"`；`sid = f"{profile_name}:{com}"`（session_manager.py:947-990）。

### 5.6 出貨預設 `sw_core/assets/profiles/default.yaml`
`defaults: {log_dir: "~/b-log", max_sessions: 16}`；templates：`prpl-template`(prpl)、`op3-template`(shell, `SW_OPI_U`/`SW_OPI_P`, `env_file: OPI.env`)、`brcm-template`(bcm, `post_login_cmd: sh`, `timeout_s: 15`, `BRCM_USER`/`BRCM_PASS`, `env_file: brcm.env`)、`smart4R-profile`(shell, `console_unlock_env: SMART4R_CONSOLE_UNLOCK`, `timeout_s: 120`, `hard_timeout_s: 240`)、`uboot-template`(passthrough)、`others-template`(passthrough, `ready_probe: ""`)；`targets` 註解掉。Windows daemon 若無 passthrough template 會注入 `ProfileTemplate(profile_name="windows-default-passthrough", platform="passthrough", ready_probe="")`（daemon.py:101-123）。

---

## 6. 路徑與環境變數解析（`sw_core/constants.py`，全部 **import 時**計算）

Helper：
- `_env_path(name, default)`：env 為 None 或 strip 後空 → default；結果 `os.path.expanduser`（constants.py:7-11）。
- `_xdg(name, fallback_rel)`：`os.environ.get(name) or ~/<fallback_rel>`（空字串視為未設；**不 expanduser env 值**）。

| 常數 | env 覆寫 | 預設 | 行 |
|---|---|---|---|
| `_CONFIG_HOME` | `XDG_CONFIG_HOME` | `~/.config` | 21 |
| `_STATE_HOME` | `XDG_STATE_HOME` | `~/.local/state` | 22 |
| `_DATA_HOME` | `XDG_DATA_HOME` | `~/.local/share` | 23 |
| `_RUNTIME_HOME` | `XDG_RUNTIME_DIR` | None | 24 |
| `CONFIG_DIR` | `SERIALWRAP_CONFIG_DIR` | `$_CONFIG_HOME/serialwrap` | 26 |
| `STATE_DIR` | `SERIALWRAP_STATE_DIR` | `$_STATE_HOME/serialwrap` | 27 |
| `RUN_DIR` | `SERIALWRAP_RUN_DIR` | 若 `SERIALWRAP_STATE_DIR` 非空白 → `STATE_DIR`；elif `XDG_RUNTIME_DIR` → `$XDG_RUNTIME_DIR/serialwrap`；else `STATE_DIR/run` | 28-38 |
| `DATA_DIR` | `SERIALWRAP_DATA_DIR` | `$_DATA_HOME/serialwrap` | 39 |
| `LOCK_PATH` | — | `RUN_DIR/serialwrapd.lock` | 41 |
| `SOCKET_PATH` | — | `RUN_DIR/serialwrapd.sock` | 42 |
| `DEFAULT_TCP_PORT` | `SERIALWRAP_TCP_PORT`（`int()`，非法值使 import 失敗） | 48700 | 47 |
| `LOOPBACK_TCP_HOSTS` | — | `{"127.0.0.1","localhost","::1"}` | 49 |
| `DEFAULT_ENDPOINT` | `SERIALWRAP_ENDPOINT`（`or`，不 strip、不 expand） | Windows：`tcp://127.0.0.1:{DEFAULT_TCP_PORT}`；POSIX：`SOCKET_PATH` | 50-56 |
| `TTYMCU_PATH` | `SERIALWRAP_TTYMCU_PATH` | `RUN_DIR/dev/ttyMCU` | 58 |
| `STATE_PATH` | — | `STATE_DIR/state.json` | 59 |
| `WAL_DIR` | `SERIALWRAP_WAL_DIR` | `STATE_DIR/wal` | 60 |
| `PROFILE_DIR` | `SERIALWRAP_PROFILE_DIR` | `CONFIG_DIR/profiles` | 61 |
| `DEVICE_BY_ID_DIR` | `SERIALWRAP_BY_ID_DIR` | `/dev/serial/by-id` | 62 |
| `DEVICE_BY_PATH_DIR` | `SERIALWRAP_BY_PATH_DIR` | `/dev/serial/by-path` | 63 |
| `LOG_DIR` | `SERIALWRAP_LOG_DIR` | `~/b-log` | 64 |
| `DEFAULT_WAL_ROTATE_BYTES` | — | `64*1024*1024 = 67108864` | 65 |
| `EVENTS_DIR` | `SERIALWRAP_EVENTS_DIR` | `~/.serialwrap/events.d`（**非 XDG**） | 66-69 |
| `EVENTS_RUNTIME_DIR` | `SERIALWRAP_EVENTS_RUNTIME_DIR` | `STATE_DIR/events` | 70-73 |
| `EVENTS_LOG_PATH` | `SERIALWRAP_EVENTS_LOG_PATH` | `EVENTS_RUNTIME_DIR/events.ndjson` | 74-77 |
| `EVENTS_LOG_ROTATE_BYTES` | — | `10*1024*1024` | 78 |
| `EVENTS_LOG_BACKUP_COUNT` | — | 3 | 79 |

`ensure_runtime_dirs()`（206-212）：makedirs `CONFIG_DIR, STATE_DIR, RUN_DIR, WAL_DIR, EVENTS_DIR, EVENTS_RUNTIME_DIR`（daemon `_run_async` 開頭呼叫）。

其他（非 constants、呼叫時讀）：

| env | 用途 | 位置 |
|---|---|---|
| `SERIALWRAP_BENCHES_FILE` | benches.yaml 路徑 | bench_registry.py:48-52 |
| `SERIALWRAP_DAEMON_ENV_FILE` | CLI spawn daemon 時載入的 env 檔；未設 → `~/OPI.env`（`LEGACY_DAEMON_ENV_FILE`）＋`PROFILE_DIR/OPI.env`；設空 → 不載入 | cli.py:51-52, 222-227, 330-346 |
| `SERIALWRAP_RPC_BACKEND` / `SERIALWRAP_LOCK_BACKEND` / `SERIALWRAP_DEVICE_BACKEND` | `auto`/`posix`/`win` | platform_backends.py |
| `SERIALWRAP_SERIAL_BACKEND` | `auto`/`posix`/`pyserial` | serial_port.py:363 |
| `SERIALWRAP_SOCKET_GROUP` | socket chgrp（system unit 設 `dialout`） | rpc_posix.py:108；systemd_units.py:74 |
| `SERIALWRAP_LOG_LEVEL` | CLI trace 等級 | cli_trace.py:53 |
| `SERIALWRAP_EVENT_*` | 傳給 event handler 子程序的輸出變數（非設定） | event_engine/dispatcher.py |
| `SERIALWRAP_LIVE_GATE` | 只用於 tests/conftest.py | |

setup 的路徑解析（setup_cmd.py:30-62）：`SERIALWRAP_CONFIG_DIR` > `XDG_CONFIG_HOME` > `~/.config`（注意：用 `if cfg_override:`，空白字串不會被視為未設，與 `_env_path` 的 strip 判斷不同）。

固定 system 路徑：`SYSTEM_SOCKET = "/run/serialwrap/serialwrapd.sock"`、`SYSTEM_PROFILE_DIR = "/etc/serialwrap/profiles"`、`_SYSTEM_UNIT_PATH = "/etc/systemd/system/serialwrap.service"`、user unit `~/.config/systemd/user/serialwrap.service`（setup_cmd.py:256-264）。

daemon CLI 參數（daemon.py:40-43）：`--profile-dir`（預設 `PROFILE_DIR`）、`--socket`（預設 `DEFAULT_ENDPOINT`）、`--lock`（預設 `LOCK_PATH`）。

規格 §6.1 提到的 `SERIALWRAP_CONFIG`（明確指定 config 檔）與「系統設定目錄」fallback **在程式碼中不存在**；config.yaml 只看 `CONFIG_DIR/config.yaml`。

---

## 7. `sw_core/util.py` 其他函式（被持久化層使用者）

- `now_iso()`：`datetime.now(tz=timezone.utc).isoformat()`（見 §1.3 格式注意）。用於 WAL `wall_ts`、alias `updated_at`、`released_at`、`last_ready_at`、capture `started_at`。
- `monotonic_ns()`：`time.monotonic_ns()`。
- `dumps_stable(obj)`：見 §1.3。
- `to_printable(data)`：見 §1.5。
- `clean_text`、`strip_ansi`、`shell_command_incomplete_reason`：非持久化（login/command 區域）；`shell_command_incomplete_reason` 回傳碼 `EMPTY_COMMAND`、`NUL_BYTE`、`UNBALANCED_SINGLE_QUOTE`、`UNBALANCED_DOUBLE_QUOTE`、`UNBALANCED_BACKTICK`、`TRAILING_OPERATOR`、`TRAILING_BACKSLASH` 或 None。

---

## 8. 其他持久化檔（指引，不在本區詳列）

- `RUN_DIR/remote/<listen_port>.json`、`cm-<port>`、`<port>.log`、`.registry.lock`：remote tunnel registry（remote_tunnel.py:193-260，dumps sort_keys 緊湊）。
- `EVENTS_DIR/<rule_id>.json`（規則）、`EVENTS_RUNTIME_DIR/<rule_id>.counter.json`、`events.ndjson`（event_engine/registry.py、counter.py）。
- agent capture 檔 `{log_dir}/{COM}_{yymmdd-HHMMSS}.log`（append、utf-8）。

---

## 9. 規格與程式碼的差異清單

| # | 規格敘述 | 實際程式碼 | 影響 |
|---|---|---|---|
| D1 | §8.1：`crc32` 型別 `u32` | 8 位小寫 hex **字串** | `wal_record.schema.json` 必須寫 string pattern `^[0-9a-f]{8}$`，Rust 不能用 u32 序列化 |
| D2 | §8.1：頂層欄位表共 12 個 | 另有條件式 `rotation_failed: true` | schema 需 optional 欄位 |
| D3 | §8.1：欄位以表格順序列出 | 實際 key 順序＝字母序（`sort_keys=True`），meta 內也排序 | 逐位元比對（A/B）需同樣排序 |
| D4 | §8.3：「WAL range 的 `truncated=true` 表示仍有下一頁」 | `wal.range` 無 `truncated`；只有 `log.tail_raw/text` 有；`wal.range` 只有 `available_from_seq`/`rotated_out` | 比較器分頁需以「回傳筆數 == limit」判斷；或登記新增欄位 |
| D5 | §8.2：外部 unlink 寫 `WAL_RECREATED_AFTER_EXTERNAL_UNLINK` META、`potentially_lost_seq_range` | 不存在；只有目錄消失時 makedirs＋計數，無 META；單檔被刪完全不偵測 | BC-01 尚待 Python 基線實作；Stage 0 須補 schema |
| D6 | §8.2：每筆 flush、正常 rotate／關機同步 | 每筆開關檔、無 fsync；rotate 只有目錄 fsync，無檔案 fsync；無關機同步（WalWriter 無 close/stop） | BC-01 |
| D7 | §1.1.3：R 待 Stage 0 擷取 | `R = 67108864`（64 MiB）；判斷 `size >= R`、寫前檢查、WAL/mirror 各自判斷 | 可直接凍結 |
| D8 | §8.2：「正常 rotate 保留可追蹤的檔案與 seq 關係」 | 歸檔名只有秒級 UTC 時間戳，同秒覆蓋；`wal.reset` 後 seq 重複；重啟時現行檔缺 → seq 歸 0；查詢 API 不讀歸檔 | 需凍結並可能需 BC |
| D9 | §6.2／§9.4：released 須在重啟後維持禁止自動 attach | 動態 session 的 released 第二次重啟即遺失 [實跑] | 安全缺陷，建議 BC |
| D10 | §6.2：release 提交失敗須提供失敗診斷 | 存檔例外 → 泛用 `EXCEPTION` + `message`，無專用碼 | 契約決策 |
| D11 | §6.1：設定發現順序含 `SERIALWRAP_CONFIG`、系統設定目錄 | 只有 `CONFIG_DIR/config.yaml`（`SERIALWRAP_CONFIG_DIR` > XDG > `~/.config`），無 `SERIALWRAP_CONFIG`、無系統目錄 fallback | 規格描述為目標，非現況 |
| D12 | §6.1：Windows `windows-task`/`windows-service` 模式 | 程式只認 `on-demand`/`systemd-user`/`systemd-system` | 新功能，非移植 |
| D13 | §9.2：71 個字面碼清單 | CLI 另產生 `BENCH_ENDPOINT_NOT_REMEMBERED`（cli.py:757-761）不在清單；`log.tail_*` 用 `INVALID_ARGS`、`wal.range` 非法參數走 `EXCEPTION` | error_codes.yaml 補登 |
| D14 | §9.4／§12.1：`state.json` 欄位、升降版相容與備份還原 | 無版本欄位、無 migration、未知 key 存檔即丟；唯一「備份」是 `.corrupt` | `state_file.schema.json` 需定義 additionalProperties 策略 |
| D15 | CLAUDE.md：alias/binding 執行期結果覆寫持久化 | alias registry 寫入但重啟不套用 [實跑] | 契約決策（1:1 或修正） |
| D16 | CLAUDE.md：log_dir 優先序含 YAML `defaults.log_dir` | 動態 session 不套用 `defaults.log_dir` | 1:1 或修正 |
| D17 | setup 假設 system scope state 在 `/var/lib/serialwrap/state.json` | system unit 未傳 state 目錄 env，daemon 實際用安裝者 `~/.local/state/serialwrap`；遷移目標不會被讀 [讀碼] | 安裝／遷移契約需修正 |
| D18 | §4.1：`state_migrate.py` 對應 `sw-core::state_migrate` | 實際只是 13 行 copy2 helper，由 setup（CLI 端）使用 | 放 sw-cli/setup 較貼切 |
| D19 | §3.2 `bench_registry.rs` 放 sw-core | bench_registry 只被 CLI（connect/benches/--bench）使用；`benches.state.json` 讀寫在 `cli.py` 內 | crate 歸屬可調整 |

## 10. 建議 Stage 0 凍結的程式行為（Rust 契約測試要點）

1. 序列化 byte-exact：key 排序、緊湊分隔、`ensure_ascii=False`、尾端 `\n`、`wall_ts` 微秒為 0 時省略小數。
2. CRC：IEEE CRC-32，輸出 `%08x`。
3. 空 payload META：`len 0 / crc "00000000" / payload_b64 ""`、mirror 不寫。
4. rotate：`>= 67108864`、寫前檢查、雙檔獨立、UTC 秒級後綴、失敗時 `rotation_failed:true`。
5. reset：`> 0` 才輪替、共用 ts、`_seq=0`、回 `{"ok","previous_seq","rotated_suffix"}`。
6. 重啟 seq＝現行檔最大合法 int seq。
7. `log.tail_*` latest/range 模式與 `truncated` 方向；`wal.range` 回應欄位（無 truncated）；`limit` 0 → 預設值。
8. `state.json` 5 key、0600 權限、原子寫、`StateLoadError` fail-closed、JSON 損毀 → `.corrupt`。
9. benches.state.json 扁平／巢狀雙形態。
10. config.yaml `safe_dump` 排序輸出、daemon 每次啟動寫 `socket_path`。
