# 10 — 完整性批判：缺口補遺與盤點間矛盾

- 基準：`~/prj_pri/serialwrap-worktrees/rust-refactor`，HEAD `93553e6`，唯讀。
- 對照：`docs/rust-refactor/serialwrap-rust-spec-final.md`（全文）與 `01`～`09` 盤點。
- 方法：
  1. 模組層：列出 `sw_core/**/*.py` 全部 45 個模組，逐一比對 01～09 是否描述其行為。
  2. 函式層：以 AST 掃描每個 `def`／`class` 名稱是否出現在任一盤點（結果見 §1.2）。
  3. 字串層：AST 掃描 `sw_core` 全部「大寫碼樣式」字串常值，以及全部 `SERIALWRAP_*` 環境變數，比對是否出現在任一盤點。
  4. 契約層：逐項核對 spec §9.4、§15.2、§9.2、§5.4、§6.1、§9.5、§12.1 需要的事實，確認盤點是否已提供；缺的自行讀碼補上（本檔 §2）。
  5. 矛盾：比對盤點之間的陳述，有衝突的回原始碼裁定（本檔 §3）。
- 標記：〔讀碼〕＝本檔重新讀碼確認；〔推論〕＝由程式碼推得、未實跑。

---

## 0. 結論摘要

- **模組覆蓋**：45 個 Python 模組（含 `event_engine/` 9 個）全部有盤點描述行為；`sw_mcp/` 不存在（09 §7 已確認，commit `135b5fe` 已刪）。字串碼掃描只剩 `ANY_ALL` 一個未出現，它是 `sre_constants` opcode 名，不是錯誤碼。`SERIALWRAP_*` 環境變數全部有盤點覆蓋。
- **未盤點的產品相關檔**：`scripts/build_windows.ps1`、`.github/workflows/release.yml`、`.github/workflows/package.yml`。它們決定現行 Windows 發佈物與 artifact 身分（spec §9.5、§12.1）。補在 §2.4。
- **契約缺口**：13 項，見 §2。最重要的 5 項：
  1. daemon 沒有任何 logging 設定（spec「一次性 log」的去向未定義）。
  2. human console 的 line-mode 行被 arbiter 拒絕時會被靜默丟棄。
  3. Windows 上 `multi_open`／holder 偵測恆失效。
  4. 現行沒有任何 artifact 身分欄位。
  5. spec §4.1／§4.2 沒有指定 `serialwrap-minicom` wrapper 由哪個階段承接。
- **盤點間矛盾**：5 組，見 §3。都已回原始碼裁定：
  - X1：`SERIALWRAP_TCP_PORT` 解析，01／07 錯，02／04／09 對。
  - X2：POSIX daemon spawn argv，02 錯，01／07 對。
  - X3：`USER_ENV_MISSING` 是否可達，02／05 錯，03 對。
  - X4：`session list` 欄位歸屬，08 錯，02／05 對。
  - X5：行號漂移若干。

---

## 1. 模組覆蓋核對

### 1.1 模組 → 主要盤點

| 模組 | 主要盤點 | 備註 |
|---|---|---|
| `cli.py`、`client.py`、`cli_trace.py`、`command_completion.py`、`version.py` | 01 | |
| `service.py`、`daemon.py`、`rpc.py`／`rpc_posix.py`／`rpc_win.py`、`platform_backends.py` | 02 | |
| （錯誤碼，跨模組） | 03 | |
| `wal.py`、`alias_registry.py`、`state_migrate.py`、`runtime_config.py`、`bench_registry.py`、`util.py` 的持久化相關部分 | 04 | |
| `session_manager.py`、`arbiter.py`、`login_fsm.py`、`transport_stall.py`、`auth.py` | 05 | |
| `uart_io.py`、`serial_port.py`、`telnet_console.py`、`flash_endpoint.py`、`mcu_patterns.py` | 06 | |
| `device_source.py`、`device_watcher.py`、`lock_posix.py`／`lock_win.py`／`daemon_lock.py`、`multi_open.py`、`sysenv.py`、`service_ctl.py`、`systemd_units.py`、`setup_cmd.py`、`doctor_cmd.py`、`remote_tunnel.py`、`assets/__init__.py`、`assets/tools/*.sh` | 07 | |
| `constants.py`、`config.py`、`event_engine/*`、`file_transfer.py`、`assets/skill/*` | 09 | |
| repo 根 shim：`serialwrap`、`serialwrap.py`、`serialwrapd.py` | 01 §0、07 §10.4 | |
| `sw_core/__init__.py` | — | 只有 1 行，無行為 |

### 1.2 名稱未出現在任何盤點的函式／類別（皆為私有 helper，行為已由呼叫端描述涵蓋）

- `bench_registry`：`_require_*`、`_validate_*`（驗證訊息已在 07 §9.1 逐字列出）。
- `doctor_cmd._check_*`：15 個。07 §7.3 以 check 名稱逐項描述。
- `file_transfer`：`_*_command` 板端命令樣板 helper。09 §4.3 已逐字列出樣板。
- `uart_io`：`_accept_console_conn`、`_client_for_sock_locked`、`_close_console_client*`、`_scan_held_slave_paths`、`_prune_rx_window_locked`、`_set_nonblock`。06 §7、§8 已描述行為。
- `session_manager`：`list_aliases`、`_template_by_name`、`_last_prompt_start`、`_bridge_rx_bytes_last_10s`、`_bridge_rx_total`、`_session_for_bridge_locked`、`_reprobe_probe_lock_locked`、`join_reprobe_workers`（測試／優雅關閉用，daemon `stop()` 不呼叫）。
- `serial_port`：`_bytesize`／`_parity`／`_stopbits`。06 §10.3 已描述 `_apply` 的映射。
- `flash_endpoint.FlashSink`：Protocol，只要求 `flash_tx(bytes)`。
- `event_engine`：`flush_for_test`（測試鉤子）、`_matcher_ctx`、`_walk_ast`／`_child_subpatterns`（ReDoS AST 走訪；09 §3.1 已描述演算法）。
- `cli`：`_decode_env_text`、`_configured_daemon_env_file`、`_daemon_start_probe_trace`、`_NoOverrideArgs`、`_trace_verbose`、`_remembered_bench_alive`／`_port`、`_tunnel_listen_port`、`_profile_test_last_nonblank_line`／`_bootloader_check`。行為已在 01 對應段落描述。

結論：函式層沒有需要補的未覆蓋行為。

---

## 2. Spec 契約面缺口（盤點未提供或分散不足的事實；以下皆已讀碼）

### G-01 Daemon logging 完全未設定（spec §5.3、§9.3「一次性 log＋WAL 警告」、§10.2）〔讀碼〕

- `sw_core/daemon.py`、`service.py`、`__init__.py`、`serialwrapd.py` 都**沒有** `logging.basicConfig`、`addHandler`、`setLevel` 或任何 `logging.config`（全 `sw_core` grep；只有 `cli_trace.py` 為 CLI 自建 StreamHandler）。
- 因此 daemon 端 `logging.getLogger("serialwrap"|__name__)` 的輸出完全走 Python `logging.lastResort`：
  - 等級 **WARNING 以上**才輸出；
  - 格式只有 `%(message)s`（無時間、無等級、無 logger 名）；
  - `exc_info=True` 時附 traceback；
  - 目的地為 **daemon 的 stderr**。〔推論，CPython 預設行為〕
- **INFO 等級全部被丟棄**。唯一的 INFO 是 `login_fsm.py:316` 的 `console unlock attempt com=%s rx_idle_s=%.3f required_idle_s=%.3f`。
- stderr 實際去向依啟動方式而定：
  - CLI `daemon start` 背景 spawn：`stdout`／`stderr=DEVNULL`（01 §7 step 11），**全部 log 遺失**；
  - `--foreground`：輸出到終端；
  - systemd：journald；
  - regression ThrowawayDaemon：`workdir/daemon.log`；
  - Windows detached（`DETACHED_PROCESS`）：同樣 DEVNULL。
- 結論：spec 的「一次性 log＋WAL 警告」在 on-demand 部署下只有 WAL META 那一半能留存。log 的目的地、格式、等級在 Stage 0 契約中需要明定。
- daemon 端 log 訊息全集（`file:line`、等級、逐字模板）：

| 位置 | 等級 | 訊息 |
|---|---|---|
| `auth.py:109` | WARNING | `無法解析 env_file: %s（session %s）` |
| `auth.py:112` | WARNING | `env_file 不存在: %s（session %s）` |
| `device_watcher.py:78` | WARNING＋exc | `device watcher tick 失敗，略過本輪` |
| `login_fsm.py:316` | INFO（被丟棄） | `console unlock attempt com=%s rx_idle_s=%.3f required_idle_s=%.3f` |
| `event_engine/matcher.py:154` | WARNING（logger=`sw_core.event_engine.matcher`） | `event-matcher thread did not exit within 2s; context may receive late callbacks` |
| `event_engine/matcher.py:226` | WARNING＋exc | `event matcher 求值失敗，丟棄此 item（selector=%s）` |
| `wal.py:110` | WARNING | `WAL 輪替失敗（best-effort 續用既有檔案，不殺 reader）：%s：%s` |
| `wal.py:165` | WARNING | `WAL append 寫入失敗（best-effort 略過）：%s` |
| `wal.py:169` | WARNING | `WAL 目錄 %s 消失，已重建並續寫（原因：%s）；消失期間的紀錄無法復原` |
| `uart_io.py:453` | WARNING＋exc | `console RX handler 例外（com=%s），略過此塊、console 續行` |
| `uart_io.py:885`、`:928` | WARNING＋exc | `RX handler 例外（com=%s），略過此塊、reader 續行` |
| `session_manager.py:763` | WARNING | `state.json 解析失敗（JSON 損毀），略過載入並備份至 %s：%s` |
| `session_manager.py:1211` | WARNING | `session %s 帳密解析未完成（reason=%s, env_file=%s）：不對 login prompt 送空帳密，標記 CREDENTIALS_UNRESOLVED 終態，請補齊帳密後手動 attach/recover` |
| `session_manager.py:1311` | WARNING | `session %s 疑似 transport stall（零 RX %.1fs、probe 全程無 echo，real_path=%s）：%s` |
| `session_manager.py:2568` | WARNING | `已達 max_sessions=%d，忽略裝置 %s` |
| `session_manager.py:3438` | WARNING＋exc | `reboot-recovery ensure_ready 例外（session=%s），續迴圈待逾時收尾` |

- daemon 另有直接 `sys.stderr.write` 的行（已見 02 §3.2）：
  - `serialwrapd: no profiles loaded`
  - `serialwrapd: DAEMON_ALREADY_RUNNING`、`serialwrapd: SOCKET_UNAVAILABLE`
  - `serialwrapd: <StateLoadError>`
  - `serialwrapd: 寫入 config.yaml endpoint 失敗（非致命）: …`

### G-02 Human console line-mode 行被拒時靜默丟棄（spec §5.2.1、§5.3「Console 輸入…失效則丟棄並可診斷」）〔讀碼〕

- `service._on_console_line`（`service.py:313-322`）呼叫 `arbiter.submit(...)` 後**丟棄回傳值**。`SessionManager._on_bridge_console_line`（`session_manager.py:2381-2383`）只是透傳。
- 以下情況下，該行對使用者**無任何回饋**（console 不顯示錯誤），WAL 不記錄，也沒有 log：
  - `CMD_TOO_LONG`；
  - `CMD_CONTAINS_NEWLINE`（實務上不會出現，因為行已切開）；
  - `SESSION_QUEUE_FULL`；
  - `SESSION_NOT_READY`：該 session 的 arbiter queue 尚未註冊。queue 只在曾進 READY 時建立，所以 **ATTACHED 狀態下第二個（line-buffer）console 輸入的每一行都會被丟**。例外：session 經 T11（READY→ATTACHED，不呼叫 `on_detached`）時 queue 仍在，submit 會成功，之後 `execute_command` 以 state≠READY 在命令紀錄中記 `SESSION_NOT_READY`。
- line-mode 的 `ConsoleClient.tx_buffer`（`uart_io.py:721-750`）**沒有上限**：沒有換行的長輸入會無限累積，送出時再因 >16384 bytes 被 `CMD_TOO_LONG` 靜默丟棄。
- human 行的命令結果（stdout）不回送給該 console；使用者只看得到裝置 echo 經 fan-out 回來的內容。

### G-03 Windows 上 `multi_open`／holder 偵測恆失效（spec §6.2 Windows、§9.4「占用」、BC-04）〔讀碼〕

- `detect_multi_open(proc_root="/proc")`（`multi_open.py:86`）在 Windows 上 `os.listdir("/proc")` 會失敗，因此 `health.status` 恆回：
  - `multi_open: false`
  - `foreign_holders: {}`
  - `multi_open_detail: {"daemons": [], "holders_status": "unknown"}`
- `_probe_external_holder`（`session_manager.py:1954-1975`）在 `listdir` 失敗時回 `{"pids": [], "holder": None}`。因此在 Windows 上：
  - `device.attach` **永不回 `DEVICE_STILL_HELD`**；
  - self-test RELEASED 分支恆為 `external_holder:"none"`、`reclaimable:true`、`recommended_action:"device_attach"`；
  - flash 的 `_watch_flasher_disconnect` 永不 armed（POSIX-only，Windows 上 flash endpoint 本來就 no-op）。
- CLI `daemon start` 的跨 socket 預檢在 Windows 上本來就跳過（01 §7 step 7）。所以 Windows 現況**沒有任何多開或占用偵測**，只靠 Tier 1 lock（`msvcrt.locking`）以及 pyserial 的 `dwShareMode=0` 開檔失敗。開檔失敗會變成 `bridge.start()` 例外 → `ATTACH_FAILED:SerialException|OSError` 之類的 `last_error`（06 §10.3 已記錄 `OSError(errno or EIO)` 的轉換）。

### G-04 現行發佈物與 artifact 身分（spec §9.5、§12.1、§1.2「不採 one-file」）〔讀碼〕

- **Windows 發佈**（`.github/workflows/release.yml` 的 `publish-windows-exe` job，`runs-on: windows-latest`，在 Linux 的 `publish` job 之後執行）：
  1. 檢查 tag `v<VERSION>`；
  2. `pip install . 'pyinstaller>=6'`；
  3. `python -m PyInstaller --noconfirm serialwrap.spec`；
  4. smoke test：`serialwrap.exe --help`、`serialwrapd.exe --help`（只檢查 exit 0）；
  5. 以 `gh release upload` 上傳 `dist/serialwrap.exe` 與 `dist/serialwrapd.exe`（`--clobber`）。
  - 產物是 **one-file PyInstaller exe**，執行時會解壓到 `%TEMP%\_MEIxxxx`（spec §1.2 要消除的正是這個）。
  - 沒有 installer、沒有版本目錄、沒有 manifest、沒有 hash 檔、沒有程式碼簽章。
- **Linux 發佈**（同 workflow 的 `publish` job）：
  1. 先跑 policy check；
  2. 檢查 tag 與 `VERSION` 一致；
  3. 跑兩輪 `pytest`（無 re2、有 re2）；
  4. `python -m build --wheel`；
  5. `gh release create --generate-notes`；
  6. 上傳 `dist/*.whl`。
  - 沒有 sdist，沒有 hash 檔。
- **本機 Windows 建置**：`scripts/build_windows.ps1`（`-Clean` 選項）。流程為 `pip install pyinstaller>=6` → `PyInstaller serialwrap.spec` → 列出 exe 大小 → 兩支 exe 跑 `--help` smoke test。
- **PR 打包 CI**：`.github/workflows/package.yml`（ubuntu）。在乾淨 venv 中 `pip install .`；確認 console_scripts `--help` 可執行；在 `cd /` 下確認 `assets.list_profile_files()` 含 `.yaml`、`list_tool_files()` 含 `minicom_router.sh`；再跑 `serialwrap doctor`（容器內無 systemd，走 on-demand）。PR 上沒有 Windows build job。
- **身分欄位現況**：
  - 全 `sw_core` 沒有 `binary_sha256`、artifact digest、git revision、implementation 名稱或契約 revision 的輸出（grep 只命中 `remote_tunnel` identity 與 `cli_trace` endpoint hash）。
  - `health.status` 只有 `version`（`resolve_version()` 字串）與 `pid`。
- **`serialwrapd` 沒有 `--version`**：`daemon.py:36-42` 的 parser 只有 `--profile-dir`、`--socket`、`--lock`，所以 `serialwrapd --version` 是 argparse 錯誤（exit 2）。daemon 版本只能透過 RPC 回應的 `version` 取得。
- **frozen exe 的版本解析**〔推論〕：PyInstaller datas 把 `VERSION` 放在 `sw_core/assets`（07 §10.4）。`version.py` 的第 1 步 `<sw_core>/../VERSION` 在 `_MEIPASS` 下不存在；第 2 步 `importlib.metadata`，在 spec 未收集 dist-info 時失敗；第 3 步 `assets.read_text("VERSION")` 命中。

### G-05 現行 Windows 執行期路徑（spec §12.1 目標為 `%LOCALAPPDATA%\Programs\serialwrap\…`）〔讀碼＋推論〕

- `constants.py` 在 Windows 上同樣走 XDG fallback，`~` 展開為 `%USERPROFILE%`：
  - `CONFIG_DIR = %USERPROFILE%\.config\serialwrap`
  - `STATE_DIR = %USERPROFILE%\.local\state\serialwrap`
  - `RUN_DIR = STATE_DIR\run`（Windows 通常沒有 `XDG_RUNTIME_DIR`）
  - `LOCK_PATH = RUN_DIR\serialwrapd.lock`
  - `WAL_DIR = STATE_DIR\wal`
  - `EVENTS_DIR = %USERPROFILE%\.serialwrap\events.d`
  - `LOG_DIR = %USERPROFILE%\b-log`
  - `SKILL_WINDOWS.md:70` 記載 `%USERPROFILE%\.config\serialwrap\config.yaml` 與 `profiles\*.yaml`。
- 以 release exe 安裝時（`SKILL_WINDOWS.md` §1 方式 A：下載兩支 exe 放同一目錄、自行加 PATH），沒有任何步驟物化 `profiles/default.yaml`。SKILL_WINDOWS 也沒有要求跑 `setup`。
  - 結果：daemon `load_profiles` 為空 → stderr `no profiles loaded` → 注入 `windows-default-passthrough` template。
  - 影響：所有 Windows session 都是 passthrough、非 command_capable，停在 ATTACHED，除非使用者手寫 profile。
- `setup` 在 Windows 上**沒有平台守衛**〔推論，未實跑〕：
  - `materialize_assets` 會把 `.sh` wrapper 複製到 `%USERPROFILE%\.local\bin` 並 chmod；
  - `_force_symlink(~/.agents/skills/serialwrap → DATA_DIR/skill)` 在 Windows 上需要開發者模式或管理員權限，否則 `OSError`；`_run_setup` 不攔這個例外，會 traceback、exit 1；
  - `ensure_wsl_systemd` 以 `platform.uname().release` 判定非 WSL，所以不動作；
  - target 解析為 `on-demand`。

### G-06 on-demand 並非「首次需求自動啟動」（spec §6.1 Windows `on-demand`：「首次需求啟動」）〔讀碼〕

- CLI 的一般 RPC 路徑（`_run_rpc`）**不會自動 spawn daemon**。`should_auto_spawn` 只在 `_run_daemon_start` 使用（`cli.py:315, 454`）；daemon 不在時直接回 `SOCKET_ERROR`（POSIX）或 `SOCKET_ERROR`／`TIMEOUT`（TCP），exit 2。
- 唯一的「需求觸發」啟動在 bash wrapper `minicom_router.sh` 的 `_ensure_daemon`：條件為 `SERIALWRAP_AUTO_START_DAEMON=1` 且 `supervision-mode`==`on-demand`，動作是 `serialwrap daemon start`（07 §11）。Windows 沒有這個 wrapper。
- 結論：spec 的「首次需求啟動」對現行 CLI 是**新行為**，需要另立契約，不能視為「保留既有」。

### G-07 `health.status` 完整鍵集合與 OD-11 顯示欄位（spec §9.4「測試裝置選取」、OD-11「啟用時 `daemon status` 必須顯示」）〔讀碼 `service.py:562-616`〕

- 頂層鍵（插入序）：
  1. `ok`、`pid`、`version`、`running`、`started_at`、`sessions`（int）、`devices`（int）、`commands`（`len(arbiter.snapshot())`）、`wal_path`、`mirror_path`、`wal`（物件）、`limits`（物件）
  2. （非空時）`warnings`
  3. `multi_open`、`foreign_holders`、`multi_open_detail`
  4. （`rpc()` 補上的）`version` 已存在，`setdefault` 為 no-op
- **不存在**以下欄位：
  - 裝置來源或 sandbox 目錄（`DEVICE_BY_ID_DIR`／`BY_PATH_DIR`）、device backend（`posix`／`win`）、`windows.exclude_coms`；
  - 實際 bind 的 endpoint 或 socket；
  - `supervision_mode`、`profile_dir`、`STATE_DIR`／`CONFIG_DIR`；
  - 平台或 serial backend。
- 現行隔離實例的唯一機制是 env：`SERIALWRAP_BY_ID_DIR`＋`SERIALWRAP_BY_PATH_DIR`（07 §1.7）。Windows 只有 `windows.exclude_coms` **排除**清單（daemon 啟動時讀一次），**沒有 include／allowlist**，也沒有「正式設定遇到即拒絕」或「隔離實例判定」邏輯。OD-11 的設定名稱、位置、解析優先序與 status 欄位都屬於新增契約，現況是零。

### G-08 CLI 程序層行為：訊號、SIGPIPE、stdin、help（spec §4.3「委派必須保留 stdin／stdout／stderr、exit code、timeout 與必要的訊號／取消行為」）〔讀碼＋推論〕

- `cli.py`、`client.py` **沒有** `signal` handler，沒有 `KeyboardInterrupt` 或 `BrokenPipeError` 攔截（grep 無結果）。依 CPython 預設〔推論〕：
  - Ctrl-C：印 `KeyboardInterrupt` traceback，程序以 SIGINT 結束（shell 看到 130）。若當下正在等 RPC 回應，daemon 端 handler **不會被取消**（02 §1.5「client 斷線不會中止 handler」）。
  - stdout 管線被關閉（例如 `| head`）：Python 預設忽略 SIGPIPE，`_print` 拋 `BrokenPipeError`，印 traceback，exit 1。
  - SIGTERM：預設行為，直接終止。
- **CLI 從不讀 stdin**（`cli.py` 無 `sys.stdin`）。唯一相關的是 `daemon start` 背景 spawn 時 daemon **繼承父程序 stdin**（01 §7 step 11）；`--foreground` 走 `subprocess.call`，前景 daemon 與 CLI 在同一 process group，終端 Ctrl-C 會同時送達兩者。
- `--help`：`RawTextHelpFormatter`，`description="serialwrap client（支援本機 Unix socket 與遠端 endpoint）"`，epilog 原文：
  ```
  examples:
    serialwrap session list
    serialwrap --endpoint tcp://127.0.0.1:7777 session list
    serialwrap --endpoint tcp://127.0.0.1:7777 cmd submit --selector COM0 --cmd 'uname -a'
  ```
  （`cli.py:1908-1918`）。各子命令的 help 字串分散在 `cli.py:1952-2452`。
  - 已知依賴：`tests/test_bootloader_recovery.py` 要求 `session interactive-open --help` 含 `--allow-attached`；release／package CI 以 `--help` exit 0 作為 smoke test（兩支 exe 與兩個 console_scripts）。
  - help 文字為繁中，Windows cp1252 console 下靠 `force_utf8_stdio` 避免崩潰（#118）。

### G-09 `TRANSPORT_STALL` 的 `last_error_detail` 逐字格式（spec §9.4「活動：`last_error_detail`」）〔讀碼 `transport_stall.py:94-112`〕

- 共同前綴 `base`：
  ```
  零 RX {round(rx_age_s, 1)} 秒且 probe 期間連 echo 都無：疑似 USB/usbip read-endpoint stall（dmesg 常見 `urb stopped: -32`）；serialwrap 的 recover/release+attach 無法自復，需 host 層 USB re-enumeration。亦可能為 DUT 斷電/當機，請先以 dmesg 佐證再動作。
  ```
- 取得 busid 時（`resolve_usb_busid`，`_BUSID_RE = \d+-\d+(\.\d+)*`，來源 `/sys/class/tty/<name>/device` 的 realpath）：
  ```
  {base} 復原指令：sudo sh -c 'echo 0 > /sys/bus/usb/devices/{busid}/authorized; echo 1 > /sys/bus/usb/devices/{busid}/authorized'（usbip 環境亦可 usbipd detach/attach 後重新 attach）。
  ```
- 取不到 busid 時：
  ```
  {base} 復原方式：對該 USB 裝置做 authorized 0→1 toggle（/sys/bus/usb/devices/<busid>/authorized）或 usbipd detach/attach 後重新 attach。
  ```
- `round(x, 1)` 依 Python `repr` 輸出：`31.0` 印成 `31.0`，不是 `31`。
- 其他 detail 格式：`BOOTLOADER_STUCK` 為 `matched_prompt=<pattern>`；login 類為 `clean_text(rx_tail())[-300:]`（03 §5.5）。

### G-10 現行有界／無界資源總表（spec §5.4 要求 Slice 2.1 前凍結；盤點分散在 02／05／06／09）〔讀碼〕

| 資源 | 現況上限 | 滿載行為 | 位置 |
|---|---|---|---|
| arbiter per-session `queue.PriorityQueue()` | **無 maxsize** | 只靠 admission 端 `_count_pending_locked >= 256` 擋（human 行亦經此，被拒即靜默丟，G-02） | `arbiter.py:52` |
| arbiter `_commands` 歷史 | 512＋在途 | 淘汰最舊已完成 | `arbiter.py` |
| BackgroundCapture | 每個 4 MiB（以 `len(str)` 計），共 64 個 | 截頭（至少留 1 段）；淘汰已結束者 | 05 §1.1 |
| RX 文字視窗 `_rx_text` | 131072 字元 | 截頭，`_rx_trimmed` 累計 | `uart_io.py:152` |
| RX 速率視窗 deque | maxlen 4096 | 丟最舊 | `uart_io.py:30` |
| Deferred buffer | 每 client 262144 bytes；client 數不限 | 截頭留尾 | 06 §6.7 |
| console line `tx_buffer` | **無上限** | — | `uart_io.py:721-750` |
| console 數 | **無上限**（PTY 每次 attach 新開一對；TCP backlog 8，連線數不限） | — | 06 §7、§8 |
| console 出向 | 無 queue | 同步非阻塞寫，滿即丟尾 | 06 §7.6 |
| WAL 寫入 | 無 queue（呼叫端同步 append） | 失敗 `loss_flag` | 04 §1.4 |
| WAL／mirror 歸檔 | 數量**不限**、不清理 | — | 04 §1.8 |
| flash probe RX buffer | 4096 | 保留尾端 | `service.py:373` |
| Event matcher queue | 1024（全 daemon 一條） | drop-oldest＋`event_dropped` | 09 §3.2 |
| Event dispatcher | 8 並行＋per-rule 1 | 丟新＋`event_dropped` | 09 §3.6 |
| events.ndjson | 10 MiB × 3 備份 | rotate | 09 §3.8 |
| RPC 請求行 | 65536 bytes（asyncio 預設） | 超過即斷線不回應 | 02 §1.2 |
| RPC 連線數／executor | 連線數不限；executor `min(32, cpu+4)` | — | 02 §1.5 |
| reprobe／attach／reboot-recovery 執行緒 | 每 session 或 by_id 以 inflight 集合去重；總數不限 | — | 05 §6 |
| `_interactive` lease dict | 不限；過期 lease 只在存取時被動移除 | — | 05 §10.5 |
| remote tunnel log `{port}.log` | append、**不限** | — | 07 §8.4 |
| agent capture log 檔 | append、不限 | — | 05 §13 |
| client recv | 每次 65536，累積到 `\n`，**總長不限** | — | 01 §3.5 |

### G-11 Spec §4.1／§4.2 沒有指定承接位置的現行產品表面

- spec §4.1 模組表**沒有列出**下列項目，crate 歸屬未定：
  - `util.py`：`now_iso`、`dumps_stable`、`to_printable`、`clean_text`、`strip_ansi`。它們是 WAL、RPC、mirror、capture 逐位元格式的單一來源。
  - `version.py`：版本解析順序是 §9.5 版本閘門的依據。
  - `command_completion.py`：`INVALID_COMPLETION_REGEX` 與 Python `re` 方言。
  - `assets/__init__.py` 與 `assets/` 內容：`SKILL.md`、`SKILL_WINDOWS.md`、`profiles/default.yaml`、`brcm.env.example`、三支 `.sh` wrapper。
  - `rpc.py` shim；repo 根 `serialwrap`、`serialwrap.py`、`serialwrapd.py`（PyInstaller entry、tests／func-test 寫死路徑）。
- **`serialwrap-minicom` wrapper（`assets/tools/minicom_router.sh`，456 行）是 L2／L3 human 路徑的實際前端**（08 §3.11、§7.4），有自己的 env 與 JSON 契約（07 §11、08 §1.5 B）。spec 只在 §15.8.4／§15.11.1 提到「tmux＋minicom」，沒有說明 Stage 1 Rust CLI 取代 `~/.local/bin/serialwrap` 後 wrapper 是否沿用、改寫或移植，也沒有指定 `setup` 物化 wrapper 由哪個階段承接。
- spec §4.2 第 13 項 `skill` 只寫輸出 `SKILL.md`，沒有提 `--platform` 與 `SKILL_WINDOWS.md`（09 D-S10 已點出）。

### G-12 測試 oracle 引用了產品不存在的錯誤碼〔讀碼〕

- `realhw/cases/p1_cmd.py:35` 容許 `error_code in ("SESSION_QUEUE_FULL", "FOREGROUND_BUSY")`；`realhw/cases/longrun.py:278` 的說明文字也提到 `FOREGROUND_BUSY`。
- `FOREGROUND_BUSY` 在 `sw_core` **沒有任何輸出路徑**（03 的 AST 盤點無此碼）。前景衝突的實際碼是 `SESSION_BUSY`，而 `cmd submit` 走 arbiter 排隊，submit 階段不會回這個碼。
- 這是 L3 oracle 的過期分支：判定時永不命中，但也不會造成誤判。Rust 契約不應因此新增 `FOREGROUND_BUSY`；建議由測試工具 PR 移除（spec §10.4 要求獨立審閱）。

### G-13 Session 狀態對外欄位補充：`session.list` 不評估 reconfirm 到期〔讀碼，補強 05 §1.7〕

- `list_sessions()` 不呼叫 `expire_ready_reconfirm`；`get_session_state`／`activity` 會呼叫（05 §1.7 已註明）。
- 對 spec §9.4「Boot／Ready：`ready_reconfirm_failed` 更新時機」的影響：L2／L3 讀 `session list`（08 §7.6），可能看到 `ready_reconfirm_pending:true` 但 `ready_reconfirm_remaining_s:0.0`，`failed` 尚未翻成 true。翻轉要等以下任一路徑：submit、`get_state`、`activity`、reprobe tick（每 1s 的 `_prepare_reprobe_locked` 會對 READY 呼叫 expire），因此最遲約 1s 後由 tick 翻轉。凍結「更新時機」時需寫入此延遲。

---

## 3. 盤點之間的矛盾（已回原始碼裁定）

| # | 主張 A | 主張 B | 原始碼事實 | 裁定 |
|---|---|---|---|---|
| X1 | 01 §4.1、07 §0：`DEFAULT_TCP_PORT = int($SERIALWRAP_TCP_PORT or "48700")`（空字串 → 48700） | 02 §2.3、04 §6、09 §1.2：`int(os.environ.get("SERIALWRAP_TCP_PORT", "48700"))`；非整數（含空字串）在 import 時 `ValueError` | `constants.py:47`：`DEFAULT_TCP_PORT: int = int(os.environ.get("SERIALWRAP_TCP_PORT", "48700"))` | **B 正確**。env 設為空字串時 CLI 與 daemon 都會在 import 時崩潰（traceback，exit 1），不會退回 48700。 |
| X2 | 02 §3.1：「CLI 也以 `[sys.executable, "-m", "sw_core.daemon"]` 或同目錄 `serialwrapd.exe` 啟動（`cli.py:207-215`）」 | 01 §7 step 8、07 §5.3 step 7：POSIX 固定 `[sys.executable, _daemon_script_path()]`，沒有 `-m` fallback；`-m` 只在 Windows 非 frozen、且 repo 無 `serialwrapd.py` 時使用 | `cli.py:526` win 分支 → `_daemon_spawn_argv()`（frozen：同層 exe → PATH → None；非 frozen：`serialwrapd.py` → `-m`）；`cli.py:538` POSIX → `[sys.executable, _daemon_script_path()]` | **01／07 正確**。02 的描述只適用於 Windows backend。pipx POSIX 安裝下 spawn 失敗的推論（01 §7.4、07 O-02）成立。 |
| X3 | 05 §16.5、02 §8.3：`USER_ENV_MISSING` 列為會產生的 `last_error` 值 | 03 §5.2、§7.5：`USER_ENV_MISSING` 目前無法到達 `last_error` | `login_fsm.py:208-216`：`_maybe_login` 用 `user, err = _resolve_login_user(...)` 取值，之後 `err` 被 `_wait_or_fail` 的回傳覆寫，從未回出；`needs_login=False` 時回 `(False, None)`；全 repo 只有 `_maybe_login` 呼叫 `_resolve_login_user`（`login_fsm.py:209`） | **03 正確**。`USER_ENV_MISSING` 只存在於 `LOGIN_FSM_DETAIL_ERRORS` 集合與 `_resolve_login_user` 回傳值，對外不可達。user 為 None 且有 `pass_env` 時實際回 `LOGIN_USER_REQUIRED`（等到 login prompt 後）或 `LOGIN_PROMPT_TIMEOUT`。契約的 last_error 列舉應標為「定義但不可達」。 |
| X4 | 08 §7.6：`session list` 的 `sessions[]` 被 L2／L3 依賴的欄位含 `recommended_action`、`human_attached` | 02 §6.1、05 §1.7：`SessionPublic`（`to_public_dict`）沒有這兩個鍵 | `human_attached` 由 `session self-test` 讀取（`realhw/cases/p1_console.py:194-195, 227-228`，LeaseContext 四欄展開在 self-test 頂層）；`recommended_action` 由 `cmd submit` 的 `READY_UNCONFIRMED` 回應讀取（`regression/.../f09_boot_uboot.py:144`），self-test／recover 回應也有 | **02／05 正確**；08 §7.6 誤植歸屬。凍結 L2／L3 依賴欄位時，`human_attached` 應掛在 `session self-test` 回應，`recommended_action` 掛在 `command.submit`（READY_UNCONFIRMED）、`session.self_test`、`session.recover`。 |
| X5 | 行號漂移 | — | `uart_io.py` `_rx_max_chars = 131072` 在 **:152**（06 §1 寫 :142；09 正確）；`SerialwrapService.rpc` 在 **`service.py:690`**（03 §1.1 寫 688-704；02 正確）；`_MIN_FILE_CHUNK/PULL/ECHO_TIMEOUT_S` 在 **`session_manager.py:69/70/74`**（09 §1.4 寫 68/69/73；05 正確）；`_ATTACHED_CONSOLE_LEASE_TIMEOUT_S` 在 **:65**（02 §9 寫 :66） | 數值都一致，只有行號不同。引用時以本列為準。 |

其他比對過、未發現實質矛盾的項目：
- RPC method 總數：01 的 CLI 53 個含 `daemon.stop`、不含 `session.get_state`；02 的 service 53 個＋`daemon.stop`＝54。兩者一致。
- DeferredBuffer 溢位行為：05、06、09 一致為截頭留尾。
- `wal.range` 沒有 `truncated`：02、04、09 一致。
- `foreign_holders` 含自身 pid：02、07 一致。
- 動態 session 的 RELEASED 遺失：04 §2.6（第二次重啟遺失）與 05 §16.6（裝置離線 tick 被改成 DETACHED）是兩條**不同且並存**的遺失路徑，不互相矛盾。兩者都應進 BC 評估。
