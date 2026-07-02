# 設計：測試污染 live state.json 的雙向量根修（#120）

- 日期：2026-07-02
- Issue：#120
- 分支：`feature/120-test-state-isolation`
- 狀態：使用者已核可方案 A（2026-07-02）

## 問題

在有 production daemon 的機器上跑 `python3 -m pytest -q tests/`，live `~/.local/state/serialwrap/state.json` 會被寫入測試殘留（fake binding、測試 alias），且 `released`（裝置交接）會被整段清空。fake binding 持久化 reserve COM0，daemon restart 後真板無法接上。2026-07-01 至 07-02 間三度復發（tempdir suffix `hq32qs6t`／`m5lwjxzx`／`3j2ml4p2`）。

探索階段（四路平行稽核＋sandbox 破壞性實證＋唯讀路由實驗）確認污染有**兩個獨立向量**，僅修 issue 原載的向量 1 無法阻止 fake binding 復發：

### 向量 1：in-process 測試未隔離 import-time 凍結的 STATE_PATH

- `sw_core/constants.py:27` `STATE_DIR`（讀 `SERIALWRAP_STATE_DIR`）與 `:57` `STATE_PATH` 於 **import time 凍結**；`sw_core/session_manager.py:32` 以 `from .constants import STATE_PATH` 綁定副本。測試內 `monkeypatch.setenv` 一律太遲，唯一有效的是 `monkeypatch.setattr(session_manager, "STATE_PATH", ...)`。
- `SessionManager.__init__` 尾段**無條件 `_save_state()`**（session_manager.py:407）——光是建構就寫一次 state.json。
- 完全未隔離的測試共 **8 檔、33 個建構點**（`test_session_capture.py` 12 點、`test_issue24_heartbeat.py` 3 點、`test_session_activity.py`、`test_command_guard.py`、`test_service_human_console.py` 6 點、`test_mcu_cli_rpc.py` 2 點、`test_flash_service_wiring.py` 6 點、`test_daemon_service_selector.py` 2 點）。此向量產出 `a0/a1/mybox/t0/test` 等測試 alias。
- **破壞性實證**：`_save_state` 只從 in-memory sessions 重建 `released`，未隔離測試建構會把 live 的 RELEASED 整段清空——摧毀裝置交接持久化，屬 two-reader 等級破壞。
- 凍結共三層：constants import-time → 消費模組 from-import 綁定副本 → 函式預設參數 def-time（`wal.py:16` `WalWriter(wal_dir=WAL_DIR)`、`service.py:222-223`）。
- 結構性原因：`tests/` 無 `conftest.py`，隔離全靠各檔自律（19 檔做對了、8 檔漏網）。

### 向量 2：CLI `--socket` 等值誤判 → 測試 RPC 誤路由到 live daemon

- `cli.py:289` 以 `args.socket != SOCKET_PATH` 判斷「使用者是否明確指定」，而 `SOCKET_PATH` 是 `SERIALWRAP_RUN_DIR` 推導的 import-time 值。coexist／e2e 測試傳的 `--socket` **恰等於其自身 env 推導出的預設值** → 被誤判為未指定 → fallback 讀**未隔離的 live `~/.config/serialwrap/config.yaml`** → 連上 `/run/serialwrap/serialwrapd.sock`。
- 後果（已唯讀實驗復現路由錯向）：測試的 `session bind COM0 → /tmp/sw-coexist-*/by-id/fake-uart0` 由 **live daemon** 執行並持久化（即擋住真板的那筆 fake binding）；`wal reset` 重置 live WAL；`cmd submit` 打真板；tearDown 的 `daemon stop` route 到 live systemd `service stop`（僅因 NEEDS_SUDO 未生效）。
- `test_multiagent_e2e.py` 同構同風險，可能是 pre-existing「agent TX count mismatch」flaky 的成因之一。
- 時序：等值判斷由 install 重構（a64c465，Codex #1a）引入，晚於測試寫成——測試當時是安全的。
- 附帶：coexist 的 `_wait_ready` 在 `setUp` 內失敗 → unittest 跳過 `tearDown` → daemon／FakeTarget／tempdir 全洩漏（2026-07-02 實測系統上 8 個殭屍 serialwrapd，已手動清除）。

## 目標

跑 `python3 -m pytest -q tests/` 在有 production daemon 的機器上，live 的 state.json／WAL／config／daemon **完全不被讀寫**；一旦復發，gate 立刻紅（本機與 CI 皆真陽性）。

## 非目標（YAGNI）

- 不做 constants 全面惰性化（三層凍結使其單獨無效，改動面全 repo 最大、reload 型測試全要改寫）。
- 不做跨行程 spy／strace 級的「live 資源零接觸」證明——以 outcome guard（state／WAL／config／daemon 前後快照）取代 mechanism spy。
- `python3 -m unittest discover` 的**完整**隔離（WAL 目錄建立、events 目錄等雜項維度）仍非目標——conftest 防線是 pytest-only；但 **state.json 維度經 P5 per-file 隔離後，兩種 runner 皆安全**（回應 adversarial review F3），並於 CLAUDE.md 測試政策註記 pytest 為唯一具完整防線的跑法。
- 不在本 change 處理 `setup_cmd._state_path_for` 對 systemd-system 回 `/var/lib/serialwrap` 與 daemon 實際行為不一致的既有 mismatch（另案）。

## 設計

### P1. `SessionManager` 建構注入 `state_path`（向量 1 正解）

- `SessionManager.__init__` 新增 keyword `state_path: str | None = None`；建構時 `self._state_path = state_path or STATE_PATH`。
  - fallback 讀**模組層全域**（非 def-time default），既有 19 檔 `setattr(session_manager, "STATE_PATH", ...)` 隔離測試零破壞（皆於 setUp patch 後才建構）。
- `_load_state`／`_save_state`／`.corrupt` 備份路徑／mkstemp dir／`os.replace` 目標，全部改用 `self._state_path`。
- `SerialwrapService.__init__` 新增同名 keyword 透傳給 `SessionManager`；default `None` → 行為與現行完全一致，`daemon.py` 不需改。
- 併修 `WalWriter.__init__` 的 def-time 凍結：`wal_dir: str = WAL_DIR` → `wal_dir: str | None = None`，於建構時 `wal_dir or WAL_DIR`（讀模組層全域）。行為相容，但 `setattr(wal, "WAL_DIR", ...)` 與 conftest env 隔離對 default 路徑真正生效，P5 的 per-file 隔離才有著力點。

### P2. `cli.py --socket` 改 sentinel（向量 2 根修）

- `--socket` 的 `default=SOCKET_PATH` 改 `default=None`；help 文字保留「預設依 XDG 執行期目錄解析」說明。
- `_resolve_endpoint`（cli.py:274-312）：
  - `if args.socket and args.socket != SOCKET_PATH:` → `if args.socket:`（有傳即明確，不再與 import-time 預設值比對）。
  - `chosen = cfg_sock or args.socket` → `chosen = cfg_sock or SOCKET_PATH`（args.socket 至此必為 None）。
  - dangling fallback（#108 #2）邏輯不變。
- `_run_daemon_start` on-demand 路徑（cli.py:181/211/213/216 消費 `args.socket`）：路徑開頭 `sock = args.socket or SOCKET_PATH` 等價替換，spawn 與 health ping 都用 `sock`。
- 語意變化僅一處：明確傳入「恰等於預設值」的 `--socket` 現在被尊重、不再 fallback config——這正是 bug 本身。a64c465 原意（未指定 → 讀 config 連 systemd-system socket）完整保留。

### P3. 新增 `tests/conftest.py` 三層防線

**第 1 層——module top-level 強制 env 隔離**（pytest 載入 conftest 早於任何測試模組 import，是 in-process 隔離唯一有效的時序；fixture 階段已太遲）：

1. 先計算 live 快照（**在改任何 env 之前**）：
   - live path 公式：`(XDG_STATE_HOME 或 ~/.local/state)/serialwrap/state.json`，**刻意忽略 `SERIALWRAP_STATE_DIR`**（它是隔離用變數，daemon env 沒有它；不沿用 `setup_cmd._state_path_for`——它對 systemd-system 回 `/var/lib/serialwrap` 與 daemon 實際行為不符）。
   - 記錄「存在與否＋內容 bytes」。
2. `tempfile.mkdtemp(prefix="sw-pytest-iso-")` 建 per-run 隔離根，**硬覆寫**（不是 `setdefault`；開發 shell 可能 export 指向 live 的值，如 `SERIALWRAP_WAL_DIR=~/b-log`，必須蓋掉）：
   - `SERIALWRAP_STATE_DIR`、`SERIALWRAP_RUN_DIR`、`SERIALWRAP_WAL_DIR`、`SERIALWRAP_CONFIG_DIR`、`SERIALWRAP_LOG_DIR`、`SERIALWRAP_EVENTS_DIR`、`SERIALWRAP_EVENTS_RUNTIME_DIR`
   - `SERIALWRAP_BY_ID_DIR`／`SERIALWRAP_BY_PATH_DIR` 指向空目錄（防 in-process 動態偵測抓到真板 → two-reader）。
   - 附帶效果：`RUN_DIR` 跟隨 `STATE_DIR`（constants.py:31-32）→ `SOCKET_PATH`／`DEFAULT_ENDPOINT` 也離開 live，in-process 誤連 production socket 之路一併封死。
3. 隔離根於 sessionfinish 清除。

相容性（探索已逐檔驗證＋baseline 實測）：subprocess 型測試自帶 `os.environ.copy()` 後覆寫，不受影響；reload 型測試（`test_runtime_paths*`、`test_constants_endpoint`、`test_autospawn_gate` 等）先清光 `SERIALWRAP_*`/`XDG_*` 再斷言，安全；其 teardown reload 會以「含 conftest env」環境重凍 constants，方向一致（回到隔離值）。

**已知需併修的 2 個測試**（baseline 以外層 env 隔離實測發現）：`test_setup_materialize.py` 的前 2 個測試只 monkeypatch `XDG_CONFIG_HOME`，而 `setup_cmd._user_dirs` 為 runtime 讀 env 且 `SERIALWRAP_CONFIG_DIR` 優先——conftest 第 1 層設定後會被 shadow 而失敗。修法：該 2 測試補 `monkeypatch.delenv("SERIALWRAP_CONFIG_DIR", raising=False)`（同檔 :60-63 已有兩維度都管理的先例）。實作時以完整 suite 驗證是否還有同型「runtime-lazy／reload 讀 XDG 但未管理 SERIALWRAP_*」的測試需同樣處理。

**第 2 層——function-scoped autouse fixture**：per-test `monkeypatch.setattr(sw_core.session_manager, "STATE_PATH", str(tmp_path/"state.json"))`，消除未隔離測試共寫同一 session 級 state 的順序耦合。autouse 對 unittest.TestCase 風格測試同樣生效；既有自帶 setUp patch 的測試，其 setUp 後蓋、tearDown 還原到 fixture 值、fixture teardown 還原原值，語意不變。

**第 3 層——session-finish live guard**（#120 的驗證要求，`pytest_sessionstart`/`pytest_sessionfinish` 快照比對，FAIL 時設 `session.exitstatus = 1`）。判定邏輯抽成純函式模組 `tests/liveguard.py`（conftest import），使每種失敗模式可被 RED unit test 證明（adversarial review F1/F2 要求）：

**Guard 1——live state.json**（strict by default，回應 F1「乾淨覆寫／released 清空不能只 WARN」）：

| 情形 | 判定 |
|---|---|
| 從不存在 → 存在 | **FAIL**（CI fresh runner 上即此型態，真陽性） |
| 任何 byte-level 內容變更（含「乾淨空 state 覆寫」「`released`/`bindings`/`aliases` 被刪」） | **FAIL**＋印結構化 diff |
| 未變（byte-identical） | 通過 |

- 逃生閥：`SERIALWRAP_LIVE_GATE=warn` 供「測試期間 live daemon 確有合法活動（hotplug／人為 alias 操作）」的機器降級；**warn 模式下結構性破壞仍一律 FAIL**——`released`/`bindings`/`aliases` 任一 key 消失、或內容含污染特徵（`/tmp/sw-`、`test-tpl`、`"test:`、`fake-uart`）。
- live path 公式同前（XDG，刻意忽略 `SERIALWRAP_STATE_DIR`）。

**Guard 2——live WAL**（回應 F2；live daemon 的 RX append 是常態，byte 比對必誤報，改守「破壞」）：

- 監看 live XDG WAL（`(XDG_STATE_HOME|~/.local/state)/serialwrap/wal/raw.wal.ndjson`）：檔案**消失或 size 縮小**（`wal reset`／truncate 的特徵）→ **FAIL**；append（size 增加）視為 live daemon 合法活動。已知極罕見誤報：測試期間恰逢 64MB rotation（載明、可用逃生閥）。
- 若外層 shell 在 conftest 覆寫前就有 `SERIALWRAP_WAL_DIR`（如 `~/b-log`），該路徑的 `raw.wal.ndjson` **任何變更 → FAIL**（live daemon 不寫那裡；會寫的只有 env 繼承類回歸，即 P4 修的 e2e 型態）。

**Guard 3——live config.yaml**：`(XDG_CONFIG_HOME|~/.config)/serialwrap/config.yaml` byte 快照，任何變更 → **FAIL**（CLI 對 config 依設計唯讀；合法寫入者只有 `serialwrap setup`，測試不得對 live 跑 setup）。

**Guard 4——live daemon 未被觸碰**（回應 F2「misroute 不改 state 也要被抓」）：

- sessionstart：若 systemd unit `serialwrap` active → 記錄 MainPID；並對 live daemon 做**唯讀** RPC 快照（`session.list` 的每 session `last_tx_at`／`bridge_generation`／`state`）。daemon 不存在／不可達（CI）→ 此 guard 靜默 skip。
- sessionfinish：unit 不再 active 或 MainPID 變更 → **FAIL**（測試 stop/restart 了 live daemon）；任一 session 的 `last_tx_at` 前進或 `bridge_generation` 變更 → **FAIL**（有東西對真板 TX／rebind——vector 2 的 `cmd submit`／`session bind` 型態）。同受 `SERIALWRAP_LIVE_GATE=warn` 逃生閥管轄（並列印完整 diff 供人工判讀）。

### P4. coexist／e2e 併修（縱深防禦＋洩漏根絕）

- `tests/test_human_agent_coexist.py`：
  - subprocess env 補 `SERIALWRAP_CONFIG_DIR`（斷掉「讀 live config → 連 live daemon」之路；P2 修好後屬縱深防禦）。
  - setUp 改為每項資源（tempdir／FakeTarget／daemon／tmux session）建立後立刻 `addCleanup` 註冊清理，`_wait_ready` 失敗不再洩漏 daemon（今日 8 個殭屍 daemon 的成因）。
- `tests/test_multiagent_e2e.py`：subprocess env 補 `SERIALWRAP_CONFIG_DIR`，並明確覆寫 `SERIALWRAP_WAL_DIR` 到自身 tempdir（現行繼承外層 shell env，若 shell export `SERIALWRAP_WAL_DIR=~/b-log` 則子 daemon 真寫 live WAL）。

### P5. 8 檔未隔離測試補 per-file 隔離＋測試政策文件註記（回應 adversarial review F3）

conftest 防線是 pytest-only；`python3 -m unittest discover -s tests` 是 CLAUDE.md 載明的替代跑法，不載入 conftest → 8 檔未隔離建構點在該跑法下仍直寫 live STATE_PATH。雙防線收斂：

- **8 檔全部補 per-file 隔離**（issue 修法 3，比照既有 19 檔正確範例）：setUp `setattr(session_manager, "STATE_PATH", tmp)`＋`setattr(wal, "WAL_DIR", tmp)`（P1 的 WalWriter None-sentinel 使後者生效）、tearDown 還原。對象：`test_session_capture.py`、`test_issue24_heartbeat.py`、`test_session_activity.py`、`test_command_guard.py`、`test_service_human_console.py`、`test_mcu_cli_rpc.py`、`test_flash_service_wiring.py`、`test_daemon_service_selector.py`。此層與 conftest 第 2 層冗餘——刻意如此：conftest 防 pytest 下的未來漏網，per-file 防 unittest runner 與單檔直跑。
- **CLAUDE.md 測試政策註記**：unittest discover 不載入 `tests/conftest.py` 的隔離與 live guard 防線，在有 production daemon 的機器上以 pytest 為準（政策與 CI 本即如此）。README 若提及測試跑法則同步。

## 測試策略（TDD，RED 先行）

1. **P1**：新 unit test——`SessionManager(state_path=tmp)` 寫到注入路徑、不碰模組層 `STATE_PATH`；未注入時 fallback 至模組層值（含 setattr patch 後建構的相容性）。`SerialwrapService(state_path=...)` 透傳生效。`WalWriter()` 於 `setattr(wal, "WAL_DIR", tmp)` 後建構落 tmp（RED：現行 def-time default 無視 patch）。
2. **P2**：`_resolve_endpoint` unit test——`--socket` 傳「恰等於 `SOCKET_PATH` 預設值」→ 直接回傳、不讀 config（RED：現行 fallback 到 config）；未傳 → config fallback 照舊；`--endpoint` 優先序不變；daemon start on-demand 等價替換。
3. **P3（liveguard 純函式，每一 F1/F2 失敗模式一個 RED case）**：
   - state：乾淨空 state 覆寫 → FAIL；`released` 整段消失 → FAIL（strict 與 warn 模式皆然）；byte-identical → PASS；warn 模式＋無結構破壞的變更 → WARN。
   - WAL：size 縮小／檔案消失 → FAIL；append → PASS；shell `SERIALWRAP_WAL_DIR` 維度任何變更 → FAIL。
   - config：任何 byte 變更 → FAIL。
   - daemon：MainPID 變更／unit 轉 inactive → FAIL；`last_tx_at` 前進／`bridge_generation` 變更 → FAIL；daemon 不可達 → SKIP。
4. **P5**：8 檔各自在無 conftest 環境下驗證不觸 live path（實作時抽測代表檔以 `python3 -m unittest tests.test_issue24_heartbeat` 型式驗證）。
5. **整體驗收**（= issue 驗證要求＋F2 補強）：完整 pytest suite 跑完，四個 guard 全綠——live state.json byte-identical、live WAL 未縮小、live config 未變、live daemon PID／sessions 未被觸碰。

## 風險與相容性

- **gate 與隔離必須同一 PR**：CI fresh runner 上未隔離測試同樣會建 runner 的 live path，先上 gate 會立即紅（真陽性、非 false-fail）。
- **Guard 誤報特性（strict 預設的代價，載明）**：測試期間若開發者同時對真板操作（attach／alias／下命令）或發生 hotplug，Guard 1／4 會 FAIL——這是刻意選擇（寧可誤報也不放過 released 清空型破壞，adversarial review F1）；此情境用 `SERIALWRAP_LIVE_GATE=warn` 降級，warn 下結構性破壞仍 FAIL。Guard 2 的 64MB rotation 誤報極罕見。
- Guard 4 依賴 systemd unit 與唯讀 RPC；CI／無 daemon 機器靜默 skip，不造成 false-fail。
- 既有 19 檔 setattr 隔離、5 檔 reload 隔離、2 檔 subprocess 隔離：全部不需改、行為不變。
- `python3 -m unittest discover` 不載入 conftest → 第 1／3 層防線不生效；state 維度由 P5 per-file 隔離補上，雜項維度（events 目錄等）載明為 pytest-only。
- pre-existing flaky `test_multiagent_e2e` TX count mismatch 可能因 P2 順帶改善（觀察、不承諾）。
- 本機驗證期間避免在 fix 合入前跑 coexist／e2e（會經向量 2 復發污染）；live state 已於 2026-07-02 手動清理並確認兩板 READY。

## Adversarial review 迭代紀錄

- 2026-07-02 Codex adversarial review（spec v1）三發現，全數採納：F1（high）gate WARNING 路徑放過無特徵破壞 → Guard 1 改 strict byte-FAIL＋warn 模式仍擋結構破壞；F2（high）驗收只守 state → 補 Guard 2/3/4（WAL／config／daemon）；F3（medium）unittest discover 破口 → P5 per-file 隔離＋CLAUDE.md 註記。

## 探索紀錄（供 review 參考）

- 四路平行稽核：constants 消費者面（三層凍結全清單）、tests 隔離分類（a 正確 19 檔／b setenv 無效 anti-pattern 現存於 WAL 維度／c 未隔離 8 檔／d subprocess 2 檔）、coexist 洩漏機制（唯讀實驗復現 `--socket` 等值誤路由）、gate 可行性（conftest 時序、CI 行為、live path 公式）。
- 現存 `setenv` anti-pattern 實例（WAL 維度，隨第 1 層防線一併失效無害化）：`test_com_rank.py:222-223`、`test_multi_open_detect.py:178-180`。
