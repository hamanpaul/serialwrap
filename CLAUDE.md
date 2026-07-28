<!-- managed-by: hamanpaul/paulsha-conventions@v1.0.15 -->
<!-- CLAUDE.md 為單一事實來源；AGENTS.md / GEMINI.md / .github/copilot-instructions.md 為指向本檔的 symlink，只需維護本檔 -->
policy_version: 1.0.15
<!-- policy_version 為 policy_check R-14 machine-readable marker；需保持裸行格式，請勿移入 frontmatter 或 code block。 -->

# serialwrap — AI Agent Policy Checklist

本文件為所有 AI agent（Claude、GitHub Copilot、Gemini 等）在本 repo 工作時必須遵守的政策清單。

## 分支政策

- **禁止直接 commit 到 `main` 分支**，所有變更必須透過 PR。
- 跨多個子項目或長期功能開發，建議使用 `git worktree` 避免分支污染。
- 分支命名慣例：`feature/<issue-id>-<short-desc>`、`fix/<issue-id>-<short-desc>`。

## 變更紀錄政策

- 自 policy v1.0.9/v1.0.10 的 **fragment 硬切換**起改採 **per-PR changelog fragment** 模型（消除並行 agent 對 `[Unreleased]` 的衝突）：所有 production code 變更，**必須新增 `changelog.d/<issue>-<slug>.md` fragment**（`changelog.d/` 的 direct child、`.md` 結尾，非巢狀路徑）。R-09 驗本 PR 有無 direct fragment——`code_paths` 有變動而缺 fragment → **FAIL**（純文件／release PR 因不動 `code_paths` 不觸發）；例外走 `skip-changelog` label 並附理由。
- **release 收斂**：以 `python3 -m policy_check.changelog collate --version X.Y.Z --date YYYY-MM-DD` 把 `changelog.d/*.md` 依 type 收斂成 Keep a Changelog dated 段並清空目錄；R-04 於 v1.0.9+ 僅要求 `# Changelog` 標題、不再強制 `[Unreleased]`（本檔仍保留空 `[Unreleased]` 供人閱讀無妨）。
- 版本號更動時，同步更新 `VERSION` 檔案（`pyproject.toml` dynamic version 從 `VERSION` 讀）。

## 測試政策

- **完成任何 code change 前，必須執行**：
  ```bash
  python3 -m pytest -q tests/
  ```
- 亦可執行：
  ```bash
  python3 -m unittest discover -s tests -v
  ```
  （注意：unittest 不載入 `tests/conftest.py` 的強制 env 隔離與 live guard 防線，僅 state/WAL/events 維度有 per-file 隔離（`tests/state_iso.py`）；**有 production daemon 的機器一律以 pytest 為準**，#120。）
- pytest 下的隔離行為（#120）：`tests/conftest.py` 會硬覆寫 9 個 `SERIALWRAP_*` 目錄變數並 pop 5 個高優先變數（外層 shell export 無效），並於 suite 結束執行 live guard（state/WAL/config/daemon 四維快照比對；逃生閥 `SERIALWRAP_LIVE_GATE=warn`，warn 下結構性破壞仍 FAIL——warn 為明知風險的 opt-in：daemon TX/state 變更僅示警，開發者需自行確認；daemon 維度多層探測 system→user systemd→唯讀 RPC on-demand，全不可達才 SKIP，#121）。
- 既有失敗：`tests/test_multiagent_e2e.py::TestMultiAgentE2E::test_five_agents_three_rounds_no_conflict`（agent TX count mismatch，pre-existing）。
- 不得引入**新的**測試失敗。

## Policy Check 政策

- 完成任何 phase 前，必須執行：
  ```bash
  python3 -m policy_check --repo .
  ```
- policy engine pinned SHA：`a764806046c410eb4f254ac0b6a8aec8b7559dab`（v1.0.15）。
- 安裝命令：
  ```bash
  python3 -m pip install --user --disable-pip-version-check \
    "git+https://github.com/hamanpaul/paulsha-conventions.git@a764806046c410eb4f254ac0b6a8aec8b7559dab"
  ```

## Agent 檔案同步政策

- **`CLAUDE.md` 為唯一事實來源（single source of truth）**；只需維護本檔。
- `AGENTS.md`、`GEMINI.md`、`.github/copilot-instructions.md` 一律為指向 `CLAUDE.md` 的 **symlink**（相對路徑），不再各自維護內容；改 `CLAUDE.md` 即同步生效。
  - 合規性：policy_check R-13（`is_file()`）與 R-14（`read_text()` 找 `policy_version:`）皆會跟隨 symlink 解析到 `CLAUDE.md`，故四檔仍視為存在且版本對齊。
  - 若 symlink 遺失或被取代為一般檔，重建：
    ```bash
    ln -sf CLAUDE.md AGENTS.md
    ln -sf CLAUDE.md GEMINI.md
    ln -sf ../CLAUDE.md .github/copilot-instructions.md
    ```
- 本檔首行保留 `<!-- managed-by: hamanpaul/paulsha-conventions@v1.0.15 -->`，第 3 行保留裸行 `policy_version: 1.0.15`（R-14 machine-readable marker，勿移入 frontmatter 或 code block）。

## PR 政策

- 所有 PR 必須填寫 `.github/pull_request_template.md` 的 Policy Checklist（R-11）。
- PR checklist 項目：
  - [ ] 分支不是 `main`
  - [ ] 變更已記錄：新增 `changelog.d/<issue>-<slug>.md` fragment（code 變更必備；純文件／release PR 可改動 `CHANGELOG.md` 或免記）
  - [ ] `VERSION` 已更新（若有版本號變動）
  - [ ] `python3 -m pytest -q tests/` 通過（無新失敗）
  - [ ] `python3 -m policy_check --repo .` 通過
  - [ ] `CLAUDE.md` 已更新（`AGENTS.md` / `GEMINI.md` / `.github/copilot-instructions.md` 為 symlink，自動同步）
  - [ ] 已標記 exemption label（若適用）

## Exemption Label 白名單

以下 label 可豁免特定 policy 規則（需在 PR 標記）：

> 以下為 policy engine 實際認得的豁免 label（對齊 v1.0.10 引擎；名稱需精確，多為 `policy-exempt:*` 冒號式，R-09 為歷史命名 `skip-changelog`）。

| Label | 豁免項目 |
|-------|---------|
| `skip-changelog` | 免記 changelog fragment（R-09；純文件/CI 變更，附理由）|
| `policy-exempt:ci-tests` | 免 CI 執行測試（R-19）|
| `policy-exempt:issue-link` | 免 PR↔issue closing-keyword（R-17）|
| `policy-exempt:docs-sync` | 免 docs/README 對齊（R-18，WARN）|
| `policy-exempt:doc-reference` | 免 doc 懸空引用（R-22）|
| `release:<version>` | 免 VERSION↔最新 tag 一致（R-07；release PR 於 tag 建立前）|

## 語言政策

- 本 repo 文件、註解、docstring、README、規格、commit message 與 AI 回覆**一律使用繁體中文**。
- **例外（雙語／對外發布素材）**：
  - `README.md` 採**中英雙語並存**（`English` 章節 + `繁體中文` 章節；英文段為繁中段的對照翻譯，供非中文操作者閱讀）——兩段內容須保持一致，非以英文取代繁中。
  - `brag-output/**` 為**對外發布素材**（launch video composition brief 等，目標受眾與影片字幕皆為英文），以**英文**撰寫，不受「一律繁中」限制。此為刻意例外，範圍僅限該目錄。

## Commit 政策

- Commit message 使用 Conventional Commits 格式（繁中 subject）。
- 所有 AI-assisted commit 必須包含 trailer：
  ```
  Co-authored-by: Copilot <223556219+Copilot@users.noreply.github.com>
  ```

## 實際命令

> 開發/操作速查（由 `.github/copilot-instructions.md` 併入，並對齊現行 pipx + systemd + XDG 流程）。

執行期依賴：`PyYAML`；`pyserial` 為 **Windows 序列埠後端**（`sw_core/serial_port.py` 的 `_PySerialPort`，#84 PORT-1）依賴，`pyproject` 以 `sys_platform=='win32'` 條件安裝（Linux/WSL 預設走 termios 後端不需要；亦可手動裝以 `SERIALWRAP_SERIAL_BACKEND=pyserial` 覆寫）。human console 路徑另需 `jq`、`minicom`。套件以 `pyproject.toml`（setuptools）打包，console_scripts `serialwrap` / `serialwrapd`，內嵌資產在 `sw_core/assets/`。

```bash
# 安裝（pipx 隔離 venv + serialwrap setup；有 systemd → systemd-user，無 → on-demand 降級）
./install.sh
serialwrap doctor                 # 驗證環境（python/pyyaml/PATH/dialout/systemd/監管模式/裝置）

# 測試（CI 與政策以 pytest 為準）
python3 -m pytest -q tests/
python3 -m unittest discover -s tests -v   # 亦可

# policy check
python3 -m policy_check --repo .

# 常用 daemon / session smoke
serialwrap service status|start|stop|restart   # systemd 模式的生命週期管理
serialwrap daemon status
serialwrap session list
serialwrap session attach --selector COM0
serialwrap session self-test --selector COM0
```

- 監管模式（`supervision_mode`）為單一事實來源（`~/.config/serialwrap/config.yaml`）。**systemd 模式下建議用 `serialwrap service ...` 管理生命週期**；`serialwrap daemon start` / `daemon stop` 在 systemd 模式皆會自動 route 到 `service start` / `service stop`（#108 #1，對稱；故 `minicom_router` 的 `AUTO_START_DAEMON=1` 在 systemd 模式不再另起非託管 daemon 造成 two-reader）。on-demand 模式 `daemon start` 為冪等（已有健康 daemon 時回 `already_running` no-op）。CLI 解析 endpoint 時若 `config.yaml` 的 `socket_path` 失聯，會依 `supervision_mode` fallback 到 canonical socket（#108 #2，唯讀、不改寫 config）。
- daemon 多開（two-reader）可被動偵測（#101）：`serialwrap doctor` 的 `single_daemon` 檢查與 `serialwrap daemon status` 的 `multi_open`/`foreign_holders` 欄位會掃 `/proc` 報出同機其他 `serialwrapd` 與 tty 持有者（純偵測、不自動 refuse/kill）。`SingletonLock` 僅 per-`(lock_path, socket_path)` flock，擋不到不同 socket／監管模式的第二個 daemon，故需此偵測。
- 既有測試框架同時涵蓋 `pytest` 與 `unittest`；CI（`.github/workflows/tests.yml`）與政策以 `pytest` 為準。

## 高層架構

serialwrap 是讓多個 agent 與多個 human console 共用**同一條 UART** 的 broker 架構，核心不是單一 CLI，而是 daemon + RPC + broker pipeline。

- `serialwrapd`（`sw_core/daemon.py`，`serialwrapd.py` 為薄 shim）：singleton daemon。啟動時載入 profiles、建立 `SerialwrapService`，再以 `sw_core/rpc.py` 提供 JSON-RPC Unix socket server。只有這個 daemon 會直接碰 UART。
- `serialwrap`（`sw_core/cli.py`）：子命令式 CLI。每個子命令都只是 RPC client；帳密為 per-session 在 attach 時解析。

### 主要資料流

`command.submit` 的實際路徑是：

`CLI` → `SerialwrapService.rpc()` → `_resolve_session_id()`（僅 `READY` 可送 agent 命令）→ `CommandArbiter.submit()` → 該 session 的 worker thread → `SessionManager.execute_command()` → `UARTBridge` → `WalWriter`

要理解前景命令、背景命令、interactive lease、human console 為什麼互不打架，至少要一起看這幾個檔案：

- `sw_core/service.py`：整體組裝點，持有 `CommandArbiter`、`SessionManager`、`DeviceWatcher`、`WalWriter`，也是唯一的 RPC 路由層。
- `sw_core/arbiter.py`：每個 session 一條 daemon worker thread + priority queue，保證單 UART 單寫入者。
- `sw_core/session_manager.py`：session 狀態機、裝置 hotplug、binding/alias 持久化、console attach、interactive lease、recover、background capture 全都在這裡。
- `sw_core/uart_io.py`：UART bridge、RX fan-out、human line buffering、本地回顯與 backspace 編輯。序列埠 I/O 透過 `sw_core/serial_port.py` 的 `SerialPort` 抽象（非直接 termios）；human console 在 POSIX 走 PTY、在無 PTY 平台（Windows）改走 `127.0.0.1` TCP listener（#84 PORT-2，TeraTerm/PuTTY 連入即一個 socket-backed `ConsoleClient`）。
- `sw_core/serial_port.py`：`SerialPort` 抽象（#84 PORT-1）——`_PosixSerialPort`（termios，Linux/WSL 生產路徑）與 `_PySerialPort`（pyserial，Windows）雙後端，`open_serial_port()` 依平台或 `SERIALWRAP_SERIAL_BACKEND`（`auto`/`posix`/`pyserial`）選擇；termios import 收在 POSIX 守衛內，使 `import sw_core.uart_io` 在 Windows 不致硬失敗。
- `sw_core/platform_backends.py`：**三個平台 seam 選擇器**（#84 PORT-4）——`select_rpc_backend()` / `select_lock_backend()` / `select_device_backend()` 依 `os.name == "nt"` 或 `sys.platform` 以 `win` 開頭自動選 `"posix"` 或 `"win"` 後端，並接受環境變數 `SERIALWRAP_RPC_BACKEND` / `SERIALWRAP_LOCK_BACKEND` / `SERIALWRAP_DEVICE_BACKEND`（`auto`/`posix`/`win`）覆寫；純函式、無副作用，可在 POSIX 機器 import 後以 env 強制選 win 後端（用於測試）。
- `sw_core/rpc_posix.py` / `sw_core/rpc_win.py`：RPC 後端分檔（#84 PORT-4）——POSIX 走 `JsonRpcUnixServer`（AF_UNIX socket）、Windows 走 `TcpRpcServer`（asyncio TCP loopback，預設 `tcp://127.0.0.1:48700`）；`daemon.py` 的 `_build_server()` 依 `select_rpc_backend()` 延遲 import 選擇，呼叫介面（`start`/`serve_forever`/`stop`）對齊，不改動分派邏輯（`serve_connection` 共用）。
- `sw_core/lock_posix.py` / `sw_core/lock_win.py`：Singleton lock 後端分檔（#84 PORT-4）——POSIX 走 `SingletonLock`（flock + Unix socket probe）、Windows 走 `WindowsSingletonLock`（`msvcrt.locking` LK_NBLCK + TCP connect probe）；語意對齊：endpoint 可連 → `DAEMON_ALREADY_RUNNING`，stale → 取得檔鎖。
- `sw_core/device_source.py`：`DeviceSource` Protocol + 雙後端（#84 PORT-4）——`PosixDeviceSource`（掃 `/dev/serial/by-id`）與 `WindowsDeviceSource`（讀 HKLM `SERIALCOMM` registry、雙重排除藍牙：BTHENUM PortName 掃描 + `bthmodem` device path 啟發式 + `windows.exclude_coms` 手動排除清單）；`service.py` 的 `DeviceWatcher` 依 `select_device_backend()` 注入對應實作。
- `sw_core/multi_open.py`：同機多開（two-reader）被動偵測（#101）——on-demand 掃 `/proc` 找其他 `serialwrapd` 與 tty 持有者，暴露到 `doctor`／`daemon status`，純偵測+回報、不自動動手。
- `sw_core/auth.py`：per-session 帳密解析。`SessionAuth` frozen dataclass 持有已解析的帳密；`resolve_session_auth()` 從 `env_file` → `os.environ` 解析。
- `sw_core/login_fsm.py`：prompt probe、登入流程與 `ready_probe` nonce 驗證。接受 `SessionAuth` 參數，不直接碰 `os.environ`。
- `sw_core/wal.py`：`raw.wal.ndjson` 與 `raw.mirror.log` 的雙軌 append-only 記錄。

### Session 狀態機

基本流轉：`DETACHED -> ATTACHING -> ATTACHED -> READY`，另有 `RECOVERING`；裝置交接/燒錄另有 `RELEASED`(#54) 與 `FLASHING`(#55)（見下方 MCU 段與 `README.md` 狀態機）。

- `ATTACHED`：bridge 已掛上，但 target 還沒確認進入可執行 prompt；這時候 **human console 仍可 attach 進去做手動登入或觀察 boot/log**。
- `READY`：agent 命令可進入 arbiter。
- `platform=passthrough` 的 session 會停在 `ATTACHED`，因為它不做 prompt/login/ready gating。

### WAL 與結果擷取

- 權威記錄為 `raw.wal.ndjson`、人類可讀鏡像為 `raw.mirror.log`，預設落在 XDG state home（`~/.local/state/serialwrap/wal/`，可由 `SERIALWRAP_WAL_DIR` 覆寫；舊版為 `/tmp/serialwrap/wal/`）。
- 每筆 WAL 都有 `seq`、`mono_ts_ns`、`wall_ts`、`source`、`cmd_id`、`crc32`、`payload_b64`。
- `background` 命令不是直接把所有輸出塞回 `command.get`；需要透過 `command.result_tail` 逐段讀取 capture。

### Agent 日誌 capture

- Agent 可透過 `session.log_start` / `session.log_stop` 對特定 session 啟停純文字 RX capture。
- 日誌寫入 `{log_dir}/{COM}_{YYMMDD}-{HHMMSS}.log`，預設 `~/b-log`。
- `log_dir` 優先序：per-target > per-profile > YAML `defaults.log_dir` > `SERIALWRAP_LOG_DIR` env > `~/b-log`。
- session detach 時自動停止 capture。WAL 是 always-on 審計記錄，agent log 是 on-demand focused capture，兩者互補。

## 關鍵慣例

### 設定物件 immutable，執行期狀態 mutable

- `sw_core/config.py` 的 `UartProfile`、`ProfileTemplate`、`SessionProfile` 都是 `@dataclass(frozen=True)`。
- `sw_core/session_manager.py` 的 `SessionRuntime`、`BackgroundCapture`、`InteractiveLease`、`SessionCapture` 則是可變 dataclass。
- 需要更新 session profile（例如 alias、device_by_id）時，慣例是用 `dataclasses.replace(...)` 產生新物件，而不是原地改 frozen config。

### RPC 路由是平面 if/elif，不做動態註冊

- `SerialwrapService.rpc()` 是單一平面分派器；新增 RPC 方法時直接加分支，不要引入 decorator registry 或 metaprogramming。
- 所有 RPC 回應都維持 `dict[str, Any]` + `ok: bool`；失敗時附 `error_code`，例外不要穿越 RPC 邊界。

### JSON 輸出必須維持緊湊且穩定

- CLI 一律用 `json.dumps(..., ensure_ascii=False, separators=(",", ":"))`。
- `state.json` 與 WAL 相關輸出會加上 `sort_keys=True`，避免不必要的 diff 與測試波動。

### human console 預設走 raw interactive 模式

`console-attach` 在 `ATTACHED` 或 `READY` 狀態下，會自動授予第一個 human console **raw interactive ownership**：所有 console bytes 透過 `UARTBridge.send_bytes()` 即時透傳到 UART（方向鍵/Tab 等特殊按鍵可用）；第二個以後的 console 仍走 line-buffer 路徑。

當 agent 提交命令時，daemon 會暫時 **suspend** human raw mode：`bridge.suspend_interactive()`（切 deferred）→ 執行 agent 命令（human 按鍵累積在 deferred buffer）→ `bridge.resume_interactive()`（flush 回 UART）。Agent 不需等 human 關閉 minicom 才能執行命令。

POSIX 上 human console 走 PTY（minicom 開 `/dev/pts/N`）；無 PTY 的平台（Windows）改走 `127.0.0.1` TCP listener（#84 PORT-2），由 TeraTerm/PuTTY 連入，每條連線即一個 socket-backed console，對端斷線以 socket EOF 偵測（不掃 `/proc`，#84 PORT-5 半切）。

### Alias / binding 是持久化狀態

- `SessionManager` 把 alias 與 binding override 存到 `state.json`；`profiles/*.yaml` 是預設來源，但執行期 `session.bind` / `alias.*` 的結果會覆寫到持久化狀態。
- 裝置綁定用 `/dev/serial/by-id/` 或 `/dev/serial/by-path/`，不要用不穩定的 `/dev/ttyUSB*`。同款晶片（如 CH340）`by-id` 會相同，須改用 `by-path`。
- **COM 編號依 by-id 排序確定性指派（#100）**：dynamic 自動偵測 session 的 COM 號在 startup 由 `prepare_dynamic_rank()` 於 lock 內、spawn attach threads **之前**依 `device_sort_key`（by-id 為主、by-path tiebreak）排序一次配好（存 `_pending_com`，`_session_from_template` 優先消費），消除「並發 attach 完成順序決定 COM0」的 race（restart 後 COM↔板對調根因）。rank 只作用 dynamic session；explicit YAML `targets`/`session.bind`/RELEASED 的 COM 為權威、排除在 pool 外。runtime 熱插沿用既有 DETACHED-rebind（不同 by-id 板繼承空槽）。on-demand 強制重排（`session renumber`）defer 至 #103。

### Profile YAML 結構

- 三個頂層區段：`defaults`、`profiles`、`targets`。`defaults` 支援 `log_dir`；`profiles` 定義 template（`platform`、`prompt_regex`、`login_regex`、`password_regex`、`user_env`、`pass_env`、`env_file`、`post_login_cmd`、`ready_probe`、`timeout_s`、`uart.*` 等）；`targets` 綁定 COM → template → device_by_id（省略則全走動態偵測）。

### Platform 行為差異

- `platform=shell`：generic Linux login，走 prompt → login → ready_probe。
- `platform=bcm`：Broadcom 原生平台，登入後進入 BCM CLI（`>`），需 `post_login_cmd: "sh"` 切到 Linux shell（`#`），`timeout_s` 建議加大（15s+）。
- `platform=prpl`：prplOS，prompt_regex 匹配 prefix，不依賴行尾錨點。
- `platform=passthrough`：不做任何 login/ready gating，停在 `ATTACHED`，適合未知設備觀察。

### 新增能力通常要同步改多個面

新增命令/RPC/工具時，通常至少一起檢查：`sw_core/service.py`（RPC 分派）、`sw_core/cli.py`（subparser 與參數）、`README.md` / `docs/**`（對外契約，R-16/R-18）、`tests/`（代表性 unit 或 E2E）。本 repo 設計是**顯式同步多個表面**，而非自動產生。

### Python 風格慣例

- Python 3.10+；幾乎所有模組以 `from __future__ import annotations` 開頭；函式簽章普遍有完整型別標註。

## 測試與除錯重點

- `tests/test_multiagent_e2e.py` 會啟動真實 daemon，再用 PTY 假 target 驗證 `READY` 流程與多 agent 序列化，任何跨 `service / arbiter / session_manager / uart_io` 的改動都適合先看這個測試。
- `tests/test_wal.py`、`tests/test_login_fsm.py`、`tests/test_session_bind.py` 分別對應 WAL、登入狀態機與綁定/持久化行為。
- 安裝走 `install.sh`（pipx install + `serialwrap setup`），不是單純複製檔案；setup 會物化資產、reconcile 監管模式（先停舊再起新）、並以 `detect_legacy_install` 偵測舊版 `~/.paul_tools` 安裝給退役指引（只指引不刪除）。

## v1.0.1 新增規則（issue 連結 / docs 對齊 / 語言）
> 本段於 policy 1.0.1 隨 R-17 / R-18 與語言規範新增。

- **R-17（PR↔issue，FAIL gate）**：PR body 引用 issue（`#N`）時必須為 closing-keyword 形式（`Closes` / `Fixes` / `Resolves #N`），merge 由 GitHub 原生自動關閉 issue 並留下 cross-reference；只引用不關閉時上 `policy-exempt:issue-link`。
- **R-18（docs 對齊，WARN，不擋 merge）**：`code_paths` 有變動但 `README.md` / `docs/**` 未同步時提醒；純內部變動可上 `policy-exempt:docs-sync`。
- **語言規範（checklist）**：依 repo 來源決定語言——`github.com/hamanpaul/*`、`github.com/paulc-arc/*` → zh-tw；arcadyan GitLab → en_US。涵蓋 PR 標題／內文與所有 comment。本 repo 屬 `hamanpaul` → zh-tw。
- **動工前（軟性，不打斷流程）**：若任務對應某 issue，`gh issue view <N>` 核對相關性後分支可命名 `feature/<N>-<slug>`，開 PR 於 body 寫 `Closes #N`；查無對應 issue 照常進行，不另開、不停。
- **Exemption 白名單新增**：`policy-exempt:issue-link`（R-17）、`policy-exempt:docs-sync`（R-18）。

## MCU flash 真機驗證手法（#55 `/dev/ttyMCU`，PR #66 實證）

> serialwrap 原生 MCU 韌體升級端點 `/dev/ttyMCU`（daemon 維持 tty 唯一 reader、sync-probe 自動認線、FLASHING 仲裁）的真機驗證程序與已知陷阱。

> **平台範圍（#84）**：`/dev/ttyMCU` PTY-bridge（含 sync-probe、baud 鏡射）為 **POSIX-only**。**Windows 不走此模型也不需要**——Windows 韌體升級工具直接獨佔開啟 UART `COMx` 自行燒錄；serialwrap 在 Windows flash 只需 **detach（release）該 COM port**（關閉自身 handle、燒完 reclaim），對應 **#54 device release/handoff** 語意（非 #55）。底層 `stop()`→`close()` / `start()`→re-open primitive 已可用；完整 `device release`/`attach` 編排待 Windows daemon（#84 PORT-4）。

- **隔離跑法（不動 prod / 人類 minicom）**：prod daemon 不停；用獨立 socket/state/run 的 **throwaway daemon** 跑待測程式碼（`SERIALWRAP_RUN_DIR` / `_STATE_DIR` / `_BY_ID_DIR` 等 env）。關鍵：`SERIALWRAP_BY_ID_DIR` 指向只放「MCU 線（FTDI）by-id symlink」的 sandbox 目錄，否則動態偵測會抓到被人類 minicom 佔住的 DUT console（ttyUSB0）造成 two-reader 衝突。
- **進 BSL**：DUT console（如 CH340/ttyUSB0）下 GPIO BSL-invoke（unbind `1fbf0300.serial`、GPIO13/14 設 in、GPIO31/54 reset）。**長指令會在 UART console 被截斷 → 必須逐行短指令送**（`tmux send-keys -l` 每行 +Enter +sleep，勿用 `;` 串長行）。
- **燒錄**：`ocp-mcu-upgrade -d <RUN_DIR>/dev/ttyMCU -b 115200 -t 8 -e -s -i <fw.bin>` → 期望 `Return error code : 0x0`；燒後 session 自動恢復 `ATTACHED`、daemon 不死、其他 COM 不受影響。
- **三個只有實機才現形的坑（皆已修，列為回歸重點）**：
  1. 端點未 bridge 時**一律沉默、不主動寫任何 bytes**（曾於 idle 寫支援清單 → 被 flasher 讀成假回應、汙染 SBL sync）。清單查詢只走 `mcu patterns` / `mcu status`，不經此 PTY。
  2. 認線 probe 必須用 **flasher 自身的 sync bytes** 並把 MCU 的 ACK **回放**給 flasher；若另注入獨立 sync 會吃掉 MCU 的 ACK（double-sync），flasher 隨後自己的 sync 永遠收不到回應。
  3. daemon 同持 PTY master+slave（避免閒置時 master 一直 EOF 空轉）→ flasher 關閉端點時 master 無 EOF；需以 holder-probe（`_probe_external_holder` 掃 pts）偵測 flasher 斷線才能結束 pump、離開 `FLASHING` 自動恢復。
