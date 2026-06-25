<!-- managed-by: hamanpaul/paulsha-conventions@v1.0.5 -->
<!-- CLAUDE.md 為單一事實來源；AGENTS.md / GEMINI.md / .github/copilot-instructions.md 為指向本檔的 symlink，只需維護本檔 -->
policy_version: 1.0.5
<!-- policy_version 為 policy_check R-14 machine-readable marker；需保持裸行格式，請勿移入 frontmatter 或 code block。 -->

# serialwrap — AI Agent Policy Checklist

本文件為所有 AI agent（Claude、GitHub Copilot、Gemini 等）在本 repo 工作時必須遵守的政策清單。

## 分支政策

- **禁止直接 commit 到 `main` 分支**，所有變更必須透過 PR。
- 跨多個子項目或長期功能開發，建議使用 `git worktree` 避免分支污染。
- 分支命名慣例：`feature/<issue-id>-<short-desc>`、`fix/<issue-id>-<short-desc>`。

## 變更紀錄政策

- 所有 production code 與文件變更，**必須同步更新 `CHANGELOG.md`**（`[Unreleased]` 段落）。
- 版本號更動時，同步更新 `VERSION` 檔案。

## 測試政策

- **完成任何 code change 前，必須執行**：
  ```bash
  python3 -m pytest -q tests/
  ```
- 亦可執行：
  ```bash
  python3 -m unittest discover -s tests -v
  ```
- 既有失敗：`tests/test_multiagent_e2e.py::TestMultiAgentE2E::test_five_agents_three_rounds_no_conflict`（agent TX count mismatch，pre-existing）。
- 不得引入**新的**測試失敗。

## Policy Check 政策

- 完成任何 phase 前，必須執行：
  ```bash
  python3 -m policy_check --repo .
  ```
- policy engine pinned SHA：`484f963adddf384d30fa0dd85aef35dddf822ee7`。
- 安裝命令：
  ```bash
  python3 -m pip install --user --disable-pip-version-check \
    "git+https://github.com/hamanpaul/paulsha-conventions.git@484f963adddf384d30fa0dd85aef35dddf822ee7"
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
- 本檔首行保留 `<!-- managed-by: hamanpaul/paulsha-conventions@v1.0.5 -->`，第 3 行保留裸行 `policy_version: 1.0.5`（R-14 machine-readable marker，勿移入 frontmatter 或 code block）。

## PR 政策

- 所有 PR 必須填寫 `.github/pull_request_template.md` 的 Policy Checklist（R-11）。
- PR checklist 項目：
  - [ ] 分支不是 `main`
  - [ ] `CHANGELOG.md` 已更新
  - [ ] `VERSION` 已更新（若有版本號變動）
  - [ ] `python3 -m pytest -q tests/` 通過（無新失敗）
  - [ ] `python3 -m policy_check --repo .` 通過
  - [ ] `CLAUDE.md` 已更新（`AGENTS.md` / `GEMINI.md` / `.github/copilot-instructions.md` 為 symlink，自動同步）
  - [ ] 已標記 exemption label（若適用）

## Exemption Label 白名單

以下 label 可豁免特定 policy 規則（需在 PR 標記）：

| Label | 豁免項目 |
|-------|---------|
| `policy-exempt-changelog` | 免更新 CHANGELOG（如純文件拼字修正）|
| `policy-exempt-tests` | 免跑測試（如純 CI/文件變更）|
| `policy-exempt-version` | 免更新 VERSION（如非 release 的 chore）|

## 語言政策

- 本 repo 文件、註解、docstring、README、規格、commit message 與 AI 回覆**一律使用繁體中文**。

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

- 監管模式（`supervision_mode`）為單一事實來源（`~/.config/serialwrap/config.yaml`）。**systemd 模式下用 `serialwrap service ...` 管理生命週期；勿用 `serialwrap daemon start`**（它不會 route 到 systemd，會另起非託管 daemon 造成 two-reader）。`serialwrap daemon stop` 在 systemd 模式會自動 route 到 `service stop`。
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
- `sw_core/uart_io.py`：serial port 與 PTY bridge、RX fan-out、human line buffering、本地回顯與 backspace 編輯。
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

### Alias / binding 是持久化狀態

- `SessionManager` 把 alias 與 binding override 存到 `state.json`；`profiles/*.yaml` 是預設來源，但執行期 `session.bind` / `alias.*` 的結果會覆寫到持久化狀態。
- 裝置綁定用 `/dev/serial/by-id/` 或 `/dev/serial/by-path/`，不要用不穩定的 `/dev/ttyUSB*`。同款晶片（如 CH340）`by-id` 會相同，須改用 `by-path`。

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

- **隔離跑法（不動 prod / 人類 minicom）**：prod daemon 不停；用獨立 socket/state/run 的 **throwaway daemon** 跑待測程式碼（`SERIALWRAP_RUN_DIR` / `_STATE_DIR` / `_BY_ID_DIR` 等 env）。關鍵：`SERIALWRAP_BY_ID_DIR` 指向只放「MCU 線（FTDI）by-id symlink」的 sandbox 目錄，否則動態偵測會抓到被人類 minicom 佔住的 DUT console（ttyUSB0）造成 two-reader 衝突。
- **進 BSL**：DUT console（如 CH340/ttyUSB0）下 GPIO BSL-invoke（unbind `1fbf0300.serial`、GPIO13/14 設 in、GPIO31/54 reset）。**長指令會在 UART console 被截斷 → 必須逐行短指令送**（`tmux send-keys -l` 每行 +Enter +sleep，勿用 `;` 串長行）。
- **燒錄**：`ocp-mcu-upgrade -d <RUN_DIR>/dev/ttyMCU -b 115200 -t 8 -e -s -i <fw.bin>` → 期望 `Return error code : 0x0`；燒後 session 自動恢復 `ATTACHED`、daemon 不死、其他 COM 不受影響。
- **三個只有實機才現形的坑（皆已修，列為回歸重點）**：
  1. 端點未 bridge 時**一律沉默、不主動寫任何 bytes**（曾於 idle 寫支援清單 → 被 flasher 讀成假回應、汙染 SBL sync）。清單查詢只走 `mcu patterns` / `mcu status`，不經此 PTY。
  2. 認線 probe 必須用 **flasher 自身的 sync bytes** 並把 MCU 的 ACK **回放**給 flasher；若另注入獨立 sync 會吃掉 MCU 的 ACK（double-sync），flasher 隨後自己的 sync 永遠收不到回應。
  3. daemon 同持 PTY master+slave（避免閒置時 master 一直 EOF 空轉）→ flasher 關閉端點時 master 無 EOF；需以 holder-probe（`_probe_external_holder` 掃 pts）偵測 flasher 斷線才能結束 pump、離開 `FLASHING` 自動恢復。
