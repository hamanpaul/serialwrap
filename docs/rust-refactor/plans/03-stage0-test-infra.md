# P2：Stage 0 測試基礎實作計畫（外部 bridge、DUT 模擬器、比較器、案例承接清單、長跑 gate）

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 建立 Stage 0 的 L1.5／L2／L3 測試基礎，使 Python 基線與後續 Rust artifact 都能用同一批外部證據驗收：`tools/uart_stdio_bridge/`（獨立 Python bridge、POSIX PTY／Windows pySerial 端點、`bridge.v1` byte 證據、故障控制通道）、stdin/stdout DUT 模擬器（行為模型自 `FakeTarget` 抽離，Docker `-i` 與 `local-process` 兩種模式）、WAL↔wire 比較器與竄改拒絕、bridge 自測（Spike 4）、socat 接線 spike、`func-test` 外部 bridge 模式（保留 13 個 case）、§15.7.1 情境腳本、`tests/acceptance/cases.yaml` 承接清單（F1～F12 展開成 case ID 與切片歸屬）、pytest 資源報告與平台 SKIP 閘門（marker 標註屬 P0 T14）、L2／L3 Windows adapter（Telnet human frontend、版本 gate、bench lock、能力式 SKIP），以及 `realhw/acceptance/longrun_acceptance.yaml` 與外層 `release_gate_result` 判定器（保留 `engine_result`）。OD-15（2026-10-16）另加：docker 優先驗證環境與兩容器拓樸（Task 17：假 COM PTY pair、systemd container、48 h docker 長跑與 >72 h Linux 監管 runner、`L1.5-docker` 證據標記），以及擁有者 Windows host 的 `windows-task` >72 h throwaway 排程與 Spike 1 剩餘項收尾（Task 18）；原生 Windows COM 證據改在擁有者 host 以 com0com pair `COM20`／`COM21` 實跑。

**Architecture:** bridge、DUT 模擬器、比較器同屬 `tools/uart_stdio_bridge/` 的一個獨立 Python 發行單元（自有 `pyproject.toml`、自測、CI job），只用 stdlib＋Windows 端 `pyserial`，禁止 import `sw_core`、禁止共用產品判定邏輯（OD-07、§15.4.2）。bridge 只持有虛擬埠對端 B（POSIX：PTY master；Windows：COM-B 或實體 USB-UART 對端），以四個觀測位置（`serial_read`／`dut_stdin_write`／`dut_stdout_read`／`serial_write`）寫 `wire.ndjson`，故障控制走獨立 TCP loopback 控制通道與 DUT 控制檔目錄，絕不混入 UART byte stream（§15.3）。測試協調器仍是既有 `func-test/runner.py`（新增 `--mode bridge`），L2／L3 仍是既有 `regression/`、`realhw/` 引擎；`tests/acceptance/` 只放清單與 schema，不建新框架（§3.2）。長跑發版判定為引擎之外的獨立判定器，原樣保留引擎結果為 `engine_result`（OD-09、§15.11.4）。

**Tech Stack:** Python 3.10+（bridge／DUT／比較器 runtime 只用 stdlib；開發測試需 3.11+ 以使用 `tomllib`，CI 用 3.12）、`pyserial==3.5`（僅 `sys_platform == "win32"`）、`pytest==9.0.3`、`PyYAML==6.0.3`、`jsonschema==4.10.3`（測試側）、socat 1.8（spike）、Docker 28（DUT image 以 `python:3.12-slim@sha256:7a8b4750…7a17` 固定，見 Task 6）、tmux＋minicom（Linux human frontend）、Windows Python 3.12（Windows adapter 的 L1，經 WSL interop 執行）。

**Spec:** `docs/rust-refactor/serialwrap-rust-spec-final.md` §3.2、§8.3、§9.5、§11.1～§11.6、§13（Spike 4、7、8）、§14.2、§15.1～§15.12；`docs/rust-refactor/owner-decisions-2026-10-15.md` OD-01、OD-07、OD-08、OD-09、OD-10、OD-11、OD-13、OD-15；`docs/rust-refactor/plans/00-master-plan.md`；`docs/rust-refactor/plans/rust-architecture.md` §9、§10；`docs/rust-refactor/plans/spec-reconciliation.md` §2.8（`TST-D-*`）、§2.10（`GAP-G-12`）；盤點 `docs/rust-refactor/inventory/08-tests-assets.md`（主要）、`04-wal-state.md` §1.3～§1.12、`06-uartio-console.md` §5、§8、`10-gaps.md` G-12。

---

## 0. 前置條件、分支與範圍界線

**前置條件**

| 依賴 | 來源 Phase | 本計畫如何使用 | 未滿足時 |
|---|---|---|---|
| `contracts/wal_record.schema.json`、`contracts/error_codes.yaml`、`contracts/behavior_changes.yaml` 已合併 | P0（`01-stage0-contracts.md`） | 比較器測試以 WAL schema 交叉驗證；`cases.yaml` 的 `behavior_changes` 欄位以 `behavior_changes.yaml` 的 ID 驗證；L1.5 `manifest.json` 記錄 `contract_revision`（40 hex，`git rev-parse HEAD:contracts`，P0「revision 命名」） | Task 7、Task 12 的對應測試保持紅燈，不得改成 SKIP 或內建副本 |
| P0 T14 的 10 個 pytest marker（`tests/conftest.py` 唯一註冊處）、檔內 `pytestmark` 規則與 `tests/test_resource_markers.py` 稽核 | P0 T14 | 本計畫在 `tests/` 新增的每個測試檔建立時即依 T14 規則帶 `pytestmark`；Task 13 只做 `SW_RESOURCE_REPORT` 報告、`core_portable` 平台 SKIP 閘門、Windows collection 前排除與 hosted Windows L1 job | 新測試檔的稽核 FAIL 視為本 Phase 新增失敗 |
| `contracts/release_manifest.schema.json`、`contracts/golden/tree_sha256.ndjson`、`sw_core/release_manifest.package_tree_sha256()` 語意 | P1 T18 | `identity.tree_sha256` 以 golden 對拍；manifest `artifacts.daemon.package_tree_sha256` 與 P1 同範圍 | Task 2、Task 10 的對應測試保持紅燈 |
| BC-01～BC-05、SC-04、SC-07 的 Python 實作已合併 | P1（`02-stage0-python-bc.md`） | §15.7.1 情境的「預期 PASS」以修正後 Python 為準 | Task 11 的情境照常交付；對 Python 執行時若因 BC 未合併而失敗，報告標 `dependency_blocked:<BC-ID>`，不得標 PASS 或 SKIP |
| F12／`p0-wal-live` oracle 修訂（`TST-D-7`） | P1 | 本計畫**不**修改 `f12_diagnostics.py`、`realhw/cases/p0.py` 的 WAL oracle；只在 `cases.yaml` 登記其新 oracle 與證據檔 | — |
| P1 全部＋P1-OC（`02b-stage0-owner-confirmations.md`）已合併（修正後 Python 基線） | P1、P1-OC | Task 17 的 48 h docker 長跑與 >72 h Linux 監管 runner 只對修正後 Python 基線啟動 | `run-longrun.sh`／`run-supervision.sh` 拒絕啟動（Task 17 測試），其餘 Task 照常 |
| OD-15 Spike 1 基本資格（擁有者 host：com0com 3.0.0.0 簽章 zip md5 `a07aa80024cca837dacc75d3573285ff` 與 SourceForge 一致、Authenticode Valid（CyberCircuits，COMODO timestamp）、Windows 10.0.26200、HVCI 執行中、Secure Boot 關閉；pair `CNCA0=COM20`（serialwrap／受測端）／`CNCB0=COM21`（bridge 端）；256-byte 全值域 A→B 相等、B→A 正常、COM20 第二次開啟被拒（`ERROR_ACCESS_DENIED`）、關閉後可重開；證據在擁有者 host 的 `b-log` 下 `rust-refactor/spike1/`） | OD-15 | 原生 Windows COM 測試與 gate（Task 3、5、6、8、10、11、13、18）在擁有者 host 以 `SW_TEST_COM_PAIR=COM20:COM21` 實跑 | 擁有者 host 不可用時記「證據不足」；Spike 1 剩餘三項（控制線隔離、Console-side pair、peer-close）未實測前不得預標 PASS |

**分支**：`wt/rust-refactor/p2-test-infra`，PR base 為 `feature/rust-refactor`（repo CLAUDE.md：base 為 `feature/*` 時來源須為同 slug 的 `wt/<slug>/<subtask>`）。不得 commit 到 `main`。

**範圍界線（明列不做的事）**

- Sequential A/B 執行器（§15.9.1 五步驟）屬 P9（`10-stage3-canary.md`）；本計畫只提供它所需的證據、比較器與 manifest 欄位。
- `release-manifest.json` 的建置（`scripts/rust/mkbundle.sh`）屬 P3；本計畫只在 L1.5 `manifest.json` 收集受測 artifact 身分，並對 Python 基線以 arch §10.1 的 `tree_sha256` 演算法計算部署程式碼摘要。
- Windows WAL／端點防毒（Spike 3）、`windows-task`（Spike 2）的可執行驗證屬 P1／P4；本計畫只在 `cases.yaml` 登記其 case ID 與證據需求。例外（OD-15）：`windows-task` >72 h 監管的 throwaway 排程程序與腳本由本計畫 Task 18 交付（P4 Task 18 消費），因 Linux docker 無法承載 Task Scheduler。
- 不修改產品程式碼（`sw_core/**`、`serialwrapd.py`、`serialwrap`）。若測試揭露產品缺陷，依 §15.10 先判定最低足夠層級，另開 issue，不在本 Phase 修。

---

## Global Constraints

（自 `00-master-plan.md` 摘錄與本 Phase 相關者；違反任一條即為審查 FAIL）

- bridge 位置 `tools/uart_stdio_bridge/`，不得 import `sw_core`／連結 `sw-pal`（OD-07、§15.4.2）；bridge、DUT 模擬器、比較器不得 import／連結產品的 Serial、WAL、仲裁、登入 FSM 或 prompt parser 實作。允許共用的只有宣告式 schema、情境資料、標準 JSON／Base64／CRC 函式庫與測試專用 helper。
- 長跑門檻唯一來源 `realhw/acceptance/longrun_acceptance.yaml`；OD-09 三項已凍結，其餘延後凍結（§14.2、§15.11.4）；不得看完候選版本結果才修改門檻。
- WAL rotate 門檻 `R` 以 Stage 0 擷取的 Python 常數為準（`DEFAULT_WAL_ROTATE_BYTES = 67108864`，盤點 04 §1.8），不自行換算 64 MB。
- RPC／CLI `error_code` 與 session `last_error` 為兩個獨立命名空間（ADR-07、§9.3）；`CREDENTIALS_UNRESOLVED` 只屬後者。測試工具新增的 `HARNESS_*` 原因碼屬測試工具契約，不得混入產品錯誤碼目錄（§11.4）。
- CLI JSON 以 `json.dumps(..., ensure_ascii=False, separators=(",", ":"))` 等價輸出；`state.json`／WAL 以 `sort_keys=True`。本計畫的證據檔（`wire.ndjson`、`control.ndjson`、`correlation.ndjson`）比照 WAL：`sort_keys=True`、緊湊分隔、行尾 `\n`。
- Windows RPC 預設 `tcp://127.0.0.1:48700`；信任範圍＝整機本機使用者（OD-12）。bridge 控制通道同樣綁 `127.0.0.1`，並以隨機 token 檔（權限 0600）限制誤用，文件不得宣稱「同使用者隔離」。
- **OD-15 docker 優先**：新版 serialwrap（修正後 Python 基線與每個 Rust 候選）一律先在 docker 驗證（Task 17）。docker 拓樸證據屬 L1.5（§11.1／§15.3），報告必帶 `evidence_layer: "L1.5-docker"`，**不得**計為真板 L2、實機 L3 或 OD-10 bench 長跑；Stage 0 證據索引對掛在 L2／L3／bench 項目上的 docker 報告判 FAIL（Task 17）。`physical_uart` 類測試在 docker 內以假 COM（socat PTY pair）執行時，pytest marker 不改，資源報告另記 `uart_kind: "pty-fake"`，不滿足真實 `physical_uart` 證據。
- **OD-15 原生 Windows COM**：擁有者 host 已通過 Spike 1 基本資格（§0 前置表）。原生 COM 測試與 gate 一律在該 host 以 `SW_TEST_COM_PAIR=COM20:COM21`（COM20＝serialwrap／受測端、COM21＝bridge／測試端）執行；受測 daemon 一律為 OD-11 隔離實例（`SERIALWRAP_STATE_DIR`／`_RUN_DIR`／`_CONFIG_DIR` 三者明確設定且非預設）＋`SERIALWRAP_TEST_DEVICE_ALLOWLIST=COM20`。由 orchestrator 經 WSL interop 代跑（Codex 沙箱可能擋 `/mnt/c`），報告附 Windows build、HVCI 狀態與 pair 對應，並寫 `evidence_layer: "native-windows"`；只有擁有者 host 不可用時才記「證據不足」。Spike 1 剩餘三項（控制線隔離、Console-side pair、peer-close 語意）由 Task 18 實測，結果未知前不得預標 PASS。
- **OD-15 唯讀 RPC**：需比對 §9.4 欄位時，可對擁有者 host 的正式 daemon 只送唯讀方法（白名單同 `--retries` 的 11 個：`health.ping`、`health.status`、`session.list`、`session.get_state`、`session.activity`、`session.console_list`、`session.log_status`、`device.list`、`alias.list`、`mcu.patterns`、`mcu.status`）；任何有副作用方法一律只對隔離實例或 docker。
- repo 政策：不得 commit 到 `main`；每個 code PR 附 `changelog.d/<slug>.md` fragment（無對應 issue 時檔名不帶 issue 號，比照 `changelog.d/setup-preserve-remote-socket-path.md`）；commit 用 Conventional Commits 繁中 subject，含 `Co-authored-by: Copilot <223556219+Copilot@users.noreply.github.com>` trailer；完成前跑 `python3 -m pytest -q tests/` 與 `python3 -m policy_check --repo .`。

## Review Focus

1. **證據不足不得轉 PASS**：`loss_flag`、CRC／seq／offset 斷裂、digest 不符、不確定寫入、未凍結或事後凍結的門檻，一律得 `INSUFFICIENT_EVIDENCE` 或 `BLOCKED`，程式中沒有任何路徑把它們算成 PASS（§11.3、§11.4、§15.12）。
2. **獨立性**：`tools/uart_stdio_bridge/` 與比較器不 import 產品、不重寫產品 prompt／login／仲裁演算法；共用的 `DutModel` 只承載測試 DUT 行為，`FakeTarget` 改用它後 13 個 case 的位元組行為不變（OD-07、§15.4.2、§15.5）。
3. **Binary 保真與背壓**：不 decode、不換行轉換、不 echo；短讀與部分寫入記錄實際接受長度；有界佇列只背壓、不靜默丟資料；結果不明的寫入記不確定區間並讓本次驗證失效（§15.4.3、§15.6.2）。
4. **分母保護**：`tests/acceptance/cases.lock.json` 凍結切片必要集合、13 個 func-test case 的 ID／目的／oracle 保留、未結案命令留在分母、門檻 `frozen_at` 必須早於 run 起點（§11.3、§14.2、§15.11.4）。
5. **平台誠實**：Windows 能力不足者以登記的 capability SKIP（`reason_code=cap_missing:<cap>`），不得以 Linux 結果、記憶體 fake 或 socket 冒充 `windows_com`；com0com peer close 不得記為 `transport_disconnect`（§11.2、§15.8.3）。
6. **證據層級不混用（OD-15）**：`L1.5-docker` 與 `host-throwaway` 報告不得滿足 L2／L3／bench 項目；throwaway 排程工作不得觸碰正式 `serialwrap` 工作與預設目錄（Task 17、18）。

---

## 1. 執行協定（逐 Task red→green）

比照 P0（`01-stage0-contracts.md`「執行順序」）與 P3（`04-stage1a-rust-cli.md` §1），本 Phase **逐 Task** 執行 red→green，不採「先批次寫完全部紅燈」：批次紅燈會讓後段 Task 的失敗原因變成前段尚未實作的 `ModuleNotFoundError`，紅燈沒有鑑別力，且 `tests/` 下頂層 import 不存在模組會使整個 `pytest -q tests/` collection error、遮蔽既有測試。

1. **執行順序**：Task 1 → **Task 9** → Task 2 → 3 → 4 → 5 → 6 → 7 → 8 → 10 → 11 → 12 → 13 → 14 → 15 → 16 → 17 → 18。Task 17（docker 驗證環境，OD-15）依賴 Task 4、6、10、12、15；Task 18（Windows host throwaway 監管與 Spike 1 收尾，OD-15）依賴 Task 3、8、12、15。Task 16（OD-09 凍結工具）只依賴 Task 12、15，屬 Stage 0 交付，不得延到後續 Phase（R4-F2）。Task 9（socat 接線 spike）只依賴 Task 1 的套件骨架，依 §15.4.1「先做 socat 接線 spike，再建 Python 正式 bridge」排在 bridge 實作（Task 2～5）之前；spike 結果（`raw_roundtrip`、`no_echo`、`eof_on_child_exit`）任一為 False 時停止並回報，不得開始 Task 3／5。編號保留以利引用。
2. **每個 Task**：測試作者完成 T 步驟（撰寫失敗測試、執行並確認 FAIL、commit 測試）→ 實作者完成 I 步驟（實作、確認 PASS、commit 實作）→ 才開始下一個 Task。T 步驟的紅燈必須是本 Task 契約的語意失敗（前段 Task 已實作），不是任意的 `ModuleNotFoundError`。每個 Task 的 Interfaces 段落是測試與實作之間的固定契約，測試作者只依 Interfaces 撰寫，不得窺探或預寫實作。
3. **`tests/` 下的新測試檔**：建立時即依 P0 T14 規則帶 `pytestmark`；對本 Task 尚未實作的模組一律在**函式內** import（P0 慣例），不得在模組頂層 import，確保 T 步驟期間 `python3 -m pytest -q tests/` 仍可完整收集。T 步驟 commit 只在 phase 分支上，不開 PR。
4. **I 步驟的綠燈範圍**：只要求「本 Task 測試＋已完成 Task 的測試＋既有測試」全綠；`tools/uart_stdio_bridge/tests` 與 `tests/` 的整套全綠在 §3 收尾驗收。I 步驟**不得新增測試**；驗收所需測試一律屬於該 Task 的 T 步驟。
5. 實作者不得修改該 Task 的測試檔來遷就實作；發現測試本身錯誤時，停下並交回測試作者修正（§10.4「測試工具變更需獨立審閱」），修正以獨立 commit `test(...)` 記錄。func-test case YAML 的 oracle 修復（Task 10）屬測試資產變更：修復前先保存 pre-repair 基線輸出，修復以獨立 commit 提交並由非實作者審閱。
6. **雙審**：高風險 Task（3、5、7、8、10、14、15、16、17、18）在該 Task I 步驟完成後立即雙審（sonnet max＋codex gpt-6.1-sol medium；審查者不得為該 Task 實作者），修正後才進下一個 Task；其餘 Task 於 Phase 收尾一併雙審。全部 Task 綠燈後跑整體驗收（§3）、修正、開 PR。

**模型分工**（`00-master-plan.md` §2；測試作者 ≠ 實作者；並行／平台／I/O 狀態機與 Telnet parser 由 `codex-gpt-6-luna-max` 實作；DUT 模擬器與 bridge 周邊工具依 master §2 由 agy 實作）：

| Task | 標題 | Test author model | Implementer model |
|---|---|---|---|
| 1 | 套件骨架、獨立依賴、產品邊界守衛與 CI | haiku-xhigh | agy-gemini-3.8-flash-high |
| 2 | 證據格式、CRC golden、EvidenceWriter | agy-gemini-3.8-flash-high | haiku-xhigh（`records.py`、`clock.py`、`manifest.py`、schema）；codex-gpt-6-luna-max（`writer.py`：鎖內取 seq、有界佇列、writer thread、fail-stop） |
| 3 | 端點 adapter（POSIX PTY／Windows pySerial） | haiku-xhigh | codex-gpt-6-luna-max |
| 4 | DUT 行為模型抽離與 stdin/stdout DUT 模擬器 | haiku-xhigh | agy-gemini-3.8-flash-high |
| 5 | bridge 核心（方向 worker、背壓、stderr、控制通道、故障動作、quiesce、checkpoint） | agy-gemini-3.8-flash-high | codex-gpt-6-luna-max |
| 6 | DUT 執行模式：Docker image（`-i` 無 `-t`）與 `local-process` | haiku-xhigh | agy-gemini-3.8-flash-high |
| 7 | WAL↔wire 比較器、correlation、checkpoint、竄改拒絕 | codex-gpt-6-luna-max | agy-gemini-3.8-flash-high |
| 8 | bridge 自測（§15.4.3／Spike 4）、核准 revision 與資格報告 | codex-gpt-6-luna-max | agy-gemini-3.8-flash-high |
| 9 | socat 接線 spike 腳本（在 Task 2 之前執行） | haiku-xhigh | agy-gemini-3.8-flash-high |
| 10 | func-test 外部 bridge 模式、DaemonHarness 隔離、oracle 修復、Windows Telnet console client | agy-gemini-3.8-flash-high | 10A haiku-xhigh（case YAML／oracle 修復、`HarnessConfig`／env 隔離、`artifact_identity`、`target_config`、README、`tools/docker/*`）；10B codex-gpt-6-luna-max（`bridge_session`、`correlation`、settle／quiesce／checkpoint 迴圈、`TelnetClientCodec`／`TelnetConsole`、runner bridge 模式與 Windows endpoint 接線） |
| 11 | §15.7.1 情境腳本與協調器新動作 | haiku-xhigh | agy-gemini-3.8-flash-high |
| 12 | `tests/acceptance/cases.yaml`、schema、凍結鎖、Stage 0 證據索引、MCU 手動程序登記 | agy-gemini-3.8-flash-high | haiku-xhigh |
| 13 | pytest 資源報告、平台 SKIP 閘門、Windows collection 與 hosted Windows L1 | agy-gemini-3.8-flash-high | haiku-xhigh |
| 14 | L2／L3 平台 adapter（pin＋版本 gate、PID／RSS、bench lock、能力式 SKIP、Telnet frontend、worker 存活事件） | haiku-xhigh | codex-gpt-6-luna-max |
| 15 | `longrun_acceptance.yaml`、外層 `release_gate_result` 判定器、觀測器 | codex-gpt-6-luna-max | haiku-xhigh |
| 16 | OD-09 延後項凍結工具與候選長跑開跑登記（延後項唯一寫入者） | codex-gpt-6-luna-max | haiku-xhigh |
| 17 | docker 驗證環境與兩容器拓樸（OD-15；測試審核 agy-gemini-3.8-flash-high） | haiku-xhigh | codex-gpt-6-luna-max |
| 18 | Windows host 監管 >72 h throwaway 排程與 Spike 1 收尾（OD-15；測試審核 haiku-xhigh） | agy-gemini-3.8-flash-high | codex-gpt-6-luna-max |

**測試指令（全 Phase 共用）**

```bash
# bridge 工具自測（獨立套件）
python3 -m pytest -q tools/uart_stdio_bridge/tests
# repo 既有＋新增 pytest
python3 -m pytest -q tests/
# func-test（不在 CI；手動與 Stage 0 證據）
python3 func-test/runner.py --mode both
# 政策
python3 -m policy_check --repo .
```

---

## 2. 檔案總覽

```text
tools/uart_stdio_bridge/
├── pyproject.toml                         # name="serialwrap-uart-stdio-bridge"，獨立依賴（Task 1）
├── README.md                              # 用法、拓樸、證據格式、限制（Task 1、更新於 5/7/8）
├── src/uart_stdio_bridge/
│   ├── __init__.py  __main__.py  cli.py  harness_codes.py  identity.py   # Task 1、2
│   ├── evidence/{__init__,records,writer,manifest,clock}.py            # Task 2
│   ├── endpoints/{__init__,base,posix_pty,win_serial}.py               # Task 3
│   ├── dut/{__init__,__main__,model,stdio_adapter,control_file,plan}.py # Task 4
│   ├── bridge/{__init__,bytequeue,dut_process,pumps,control,core}.py    # Task 5、6
│   ├── compare/{__init__,wal_reader,wire_reader,integrity,streams,rules,comparator}.py
│   │          mapping_rules.json                                        # Task 7
│   └── selftest/{__init__,checks,runner}.py                             # Task 8
├── APPROVED_REVISION.json                 # Task 8（核准的工具 revision；人工審閱後更新）
├── docker/Dockerfile.dut                  # Task 6
├── scripts/build_dut_image.sh             # Task 6
├── spikes/{socat_wiring.sh,echo_child.py} # Task 9
└── tests/                                 # 各 Task 的工具自測
tests/acceptance/
├── cases.yaml  cases.schema.json  cases.lock.json                       # Task 12
├── stage0_evidence_index.yaml  evidence_index.schema.json  check_evidence_index.py  # Task 12
├── bridge_record.schema.json  bridge_control.schema.json                # Task 2
├── correlation.schema.json  comparison.schema.json  l15_manifest.schema.json  # Task 2、7
├── l1_required.lock.json  l1_required.schema.json  l1_results.schema.json  l1_judge.py  l1_judge_vectors.json  # Task 13（L1 必要 nodeid 凍結與完整性判定，R5-F3）
tests/test_acceptance_cases.py  tests/test_acceptance_schemas.py         # Task 12、2
tests/test_resource_report.py  tests/test_l1_judge.py                    # Task 13
tests/conftest.py                                                        # Task 13（只加報告、平台 SKIP 閘門與 Windows collection 排除；marker 註冊屬 P0 T14）
func-test/runner.py  func-test/lib/{daemon_harness,fake_target,target_config,console_client,correlation,bridge_session,artifact_identity}.py
func-test/cases/*.yaml（13 個，加 bridge 區塊與 oracle 修復）  func-test/scenarios/l15-*.yaml（15 個）、win-com-*.yaml（3 個）  # Task 10、11
tools/docker/{uart_harness,remote_lab}.py                                # Task 10（artifact env 與 target_config import）
realhw/{drivers,preflight,harness,__main__}.py  realhw/frontends.py  realhw/procobs.py  realhw/cases/{longrun,p1_cmd,p0,p1_console,p1_wal,p1_restart,p1_handoff,p1_hotplug}.py  # Task 14
regression/serialwrap_regression/{plugin,preflight,core,guards}.py  regression/serialwrap_regression/cases/{f05_console_coexist,f09_boot_uboot}.py  # Task 14
reliability/serialwrap_reliability/{core,testbed_loader}.py                # Task 14
realhw/acceptance/{__init__,longrun_acceptance.yaml,longrun_acceptance.schema.json,gate.py,observer.py,walscan.py,winexcl.py}  # Task 15
realhw/acceptance/{od09_freeze.py,candidate_longrun_starts.ndjson}  tests/{test_od09_freeze,test_longrun_gate_frozen,fakes_od09}.py  # Task 16
docs/rust-refactor/owner-decisions-addenda.md  docs/rust-refactor/procedures/od09-deferred-freeze.md  # Task 16
tools/docker/{serialwrap,dut-sim,systemd}.Dockerfile  tools/docker/compose.l15.yaml  tools/docker/fake_uart.sh  # Task 17（OD-15）
scripts/docker/{run-l15,run-systemd,run-longrun,run-supervision}.sh  tests/acceptance/evidence_labels.py  tests/test_docker_l15.py  tests/test_evidence_labels.py  # Task 17
scripts/stage0/win-task-throwaway.ps1  realhw/acceptance/spike1_remaining.py  realhw/acceptance/spike1_report.schema.json  tests/test_win_task_throwaway.py  tests/test_spike1_remaining.py  # Task 18（OD-15）
realhw/procedures/p2-1-mcu-flash.md                                      # Task 12
.github/workflows/tests.yml                                              # Task 1（bridge job）、Task 13（`pytest-windows` job、自架 Windows COM job）
docs/regression-plugin.md  func-test/README.md                           # Task 10、12、14（R-18 文件同步）
changelog.d/rust-p2-*.md                                                 # 每個 Task 一個 fragment（frontmatter 見各 Task）
```

---
## Task 1：套件骨架、獨立依賴、產品邊界守衛與 CI

**Test author model:** haiku-xhigh　**Implementer model:** agy-gemini-3.8-flash-high
**Changelog fragment:** `changelog.d/rust-p2-bridge-skeleton.md`（`type: feat`、`scope: test-infra`）
**BLOCKED-OWNER evidence:** 無（全部可在本環境與 hosted CI 產出）。

**Files:**
- Create: `tools/uart_stdio_bridge/pyproject.toml`、`tools/uart_stdio_bridge/README.md`、`tools/uart_stdio_bridge/src/uart_stdio_bridge/__init__.py`、`.../__main__.py`、`.../cli.py`、`.../harness_codes.py`、`.../identity.py`
- Create: `tools/uart_stdio_bridge/tests/conftest.py`、`tools/uart_stdio_bridge/tests/test_boundary.py`、`tools/uart_stdio_bridge/tests/test_cli_skeleton.py`
- Modify: `.github/workflows/tests.yml`（新增 `bridge-tools` 與 `bridge-tools-windows` 兩個 job）

**Interfaces:**

Produces（後續所有 Task 依賴）：

```python
# tools/uart_stdio_bridge/src/uart_stdio_bridge/__init__.py
__version__: str = "0.1.0"
TOOL_NAME: str = "uart-stdio-bridge"
WIRE_SCHEMA: str = "bridge.v1"
CONTROL_SCHEMA: str = "control.v1"
CORRELATION_SCHEMA: str = "correlation.v1"
COMPARISON_SCHEMA: str = "comparison.v1"
MANIFEST_SCHEMA: str = "l15-manifest.v1"
BRIDGE_MANIFEST_SCHEMA: str = "bridge-manifest.v1"

# .../identity.py
def file_sha256(path: pathlib.Path) -> str                      # 小寫 64 hex
def tree_sha256(root: pathlib.Path, *, include: Callable[[pathlib.Path], bool]) -> str
    # 演算法照 rust-architecture.md §10.1：sha256( 依 relpath（POSIX 分隔、UTF-8）排序後，逐檔串接 f"{relpath}\0{file_sha256}\n" )
def tool_tree_sha256() -> str
    # root=tools/uart_stdio_bridge；只涵蓋影響工具行為的檔：src/**（排除 __pycache__/、*.pyc）、pyproject.toml、docker/Dockerfile.dut。
    # README、scripts/、spikes/、tests/、APPROVED_REVISION.json 不計入（文件或 spike 變更不得讓既有資格報告失效，也不得改變行為摘要）
def package_tree_sha256(repo_root: pathlib.Path) -> str          # 與 P1 T18 `sw_core.release_manifest.package_tree_sha256()` 同範圍：sw_core/ 子樹、relpath 以 "sw_core/" 開頭（獨立實作，不 import 產品）
def repo_tree_sha256(repo_root: pathlib.Path) -> str             # 部署程式碼較大範圍：sw_core/**/*.py、sw_core/assets/**、serialwrapd.py、serialwrap、serialwrap.py、VERSION（鍵名與 package_tree_sha256 分開，不共用 tree_sha256）
def contract_revision(repo_root: pathlib.Path) -> tuple[str | None, bool]
    # (`git rev-parse HEAD:contracts` 的 40 hex 或 None, contracts/ 是否有未 commit 變更)；git 不可用回 (None, True)

# .../harness_codes.py（測試工具契約；不得進產品 error_codes.yaml）
HARNESS_CODES: tuple[str, ...] = (
    "HARNESS_EVIDENCE_WRITE_FAILED", "HARNESS_DISK_FULL", "HARNESS_EVIDENCE_OVERFLOW",
    "HARNESS_ENDPOINT_OPEN_FAILED", "HARNESS_ENDPOINT_IO_ERROR", "HARNESS_WRITE_UNCERTAIN",
    "HARNESS_DUT_SPAWN_FAILED", "HARNESS_DUT_EXITED", "HARNESS_CONTROL_PROTOCOL_ERROR",
    "HARNESS_CAPABILITY_UNAVAILABLE", "HARNESS_SCHEMA_INVALID", "HARNESS_CRC_MISMATCH",
    "HARNESS_SEQ_GAP", "HARNESS_OFFSET_GAP", "HARNESS_TAMPER_DETECTED", "HARNESS_LOSS_FLAG",
    "HARNESS_WAL_UNREADABLE", "HARNESS_WINDOW_UNSETTLED", "HARNESS_INSUFFICIENT_EVIDENCE",
    "HARNESS_DEPENDENCY_BLOCKED",
)

# .../cli.py
def main(argv: list[str] | None = None) -> int
    # 子命令：run（Task 5）、compare／redact（Task 7）、selftest（Task 8）、dut（Task 4，轉呼叫 uart_stdio_bridge.dut）、version
    # 未知子命令或參數錯誤：argparse 標準行為，exit 2
```

`version` 子命令 stdout 一行緊湊 JSON：`{"name":"uart-stdio-bridge","tree_sha256":"<64hex>","version":"0.1.0","wire_schema":"bridge.v1"}`（`sort_keys=True`），exit 0。

`pyproject.toml` 固定內容要點：`[project] name="serialwrap-uart-stdio-bridge"`、`version="0.1.0"`、`requires-python=">=3.10"`、`dependencies=['pyserial==3.5; sys_platform == "win32"']`、`[project.optional-dependencies] dev=["pytest==9.0.3","PyYAML==6.0.3","jsonschema==4.10.3"]`、`[project.scripts] uart-stdio-bridge="uart_stdio_bridge.cli:main"`、build backend `setuptools>=69`、`package-dir={"" = "src"}`。

### T 步驟

- [ ] **T1-1：撰寫失敗測試** `tools/uart_stdio_bridge/tests/conftest.py`：

```python
from __future__ import annotations
import pathlib, sys
TOOL_ROOT = pathlib.Path(__file__).resolve().parents[1]
REPO_ROOT = TOOL_ROOT.parents[1]
SRC = TOOL_ROOT / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))

def pytest_sessionfinish(session, exitstatus):
    # 產品邊界：整個工具自測 session 不得載入產品模組
    leaked = sorted(m for m in sys.modules if m == "sw_core" or m.startswith("sw_core."))
    if leaked:
        print(f"[bridge-boundary] FAIL: 載入了產品模組 {leaked}")
        if exitstatus == 0:
            session.exitstatus = 1
```

`tools/uart_stdio_bridge/tests/test_boundary.py`：

```python
from __future__ import annotations
import ast, os, pathlib, subprocess, sys, tomllib
TOOL_ROOT = pathlib.Path(__file__).resolve().parents[1]
FORBIDDEN_TOP = {"sw_core", "serialwrapd", "serialwrap", "realhw", "serialwrap_regression"}

def _src_files() -> list[pathlib.Path]:
    return sorted((TOOL_ROOT / "src").rglob("*.py"))

def test_src_exists():
    assert (TOOL_ROOT / "src" / "uart_stdio_bridge" / "__init__.py").is_file()

def test_no_product_import_ast():
    offenders: list[str] = []
    for path in _src_files():
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        for node in ast.walk(tree):
            names: list[str] = []
            if isinstance(node, ast.Import):
                names = [a.name for a in node.names]
            elif isinstance(node, ast.ImportFrom) and node.level == 0 and node.module:
                names = [node.module]
            elif isinstance(node, ast.Call):
                fn = node.func
                fname = getattr(fn, "attr", None) or getattr(fn, "id", None)
                if fname in {"import_module", "__import__"}:
                    names = [a.value for a in node.args if isinstance(a, ast.Constant) and isinstance(a.value, str)]
            for name in names:
                if name.split(".")[0] in FORBIDDEN_TOP:
                    offenders.append(f"{path.relative_to(TOOL_ROOT)}:{node.lineno}:{name}")
    assert offenders == []

def test_import_all_modules_without_product():
    code = (
        "import sys; sys.path.insert(0, sys.argv[1]);"
        "import pkgutil, importlib, uart_stdio_bridge as p;"
        "[importlib.import_module(m.name) for m in pkgutil.walk_packages(p.__path__, 'uart_stdio_bridge.')"
        " if not m.name.endswith('.__main__') and (not m.name.endswith('.win_serial') or sys.platform == 'win32')];"
        "bad=[m for m in sys.modules if m=='sw_core' or m.startswith('sw_core.')];"
        "print(bad); raise SystemExit(1 if bad else 0)"
    )
    env = {k: v for k, v in os.environ.items() if k != "PYTHONPATH"}
    r = subprocess.run([sys.executable, "-I", "-c", code, str(TOOL_ROOT / "src")],
                       capture_output=True, text=True, env=env, timeout=60)
    assert r.returncode == 0, r.stdout + r.stderr

def test_runtime_deps_pinned_and_minimal():
    data = tomllib.loads((TOOL_ROOT / "pyproject.toml").read_text(encoding="utf-8"))
    assert data["project"]["name"] == "serialwrap-uart-stdio-bridge"
    assert data["project"]["requires-python"] == ">=3.10"
    assert data["project"]["dependencies"] == ['pyserial==3.5; sys_platform == "win32"']
    assert sorted(data["project"]["optional-dependencies"]["dev"]) == sorted(
        ["pytest==9.0.3", "PyYAML==6.0.3", "jsonschema==4.10.3"])

def test_harness_codes_disjoint_from_product_codes():
    from uart_stdio_bridge.harness_codes import HARNESS_CODES
    assert len(HARNESS_CODES) == len(set(HARNESS_CODES))
    assert all(c.startswith("HARNESS_") for c in HARNESS_CODES)
    contract = TOOL_ROOT.parents[1] / "contracts" / "error_codes.yaml"
    text = contract.read_text(encoding="utf-8")  # P0 產出；不存在即紅燈（不得 SKIP）
    assert "HARNESS_" not in text
```

`tools/uart_stdio_bridge/tests/test_cli_skeleton.py`：

| 測試名 | 斷言 |
|---|---|
| `test_version_json` | `python -m uart_stdio_bridge version` exit 0；stdout 單行；`json.loads` 後 keys＝`{"name","tree_sha256","version","wire_schema"}`；`version=="0.1.0"`；`re.fullmatch(r"[0-9a-f]{64}", tree_sha256)`；stdout 等於 `json.dumps(obj, sort_keys=True, separators=(",",":"))+"\n"` |
| `test_unknown_subcommand_exit2` | `python -m uart_stdio_bridge nosuch` exit 2，stderr 含 `usage:` |
| `test_tree_sha256_stable_and_excludes_tests` | 連呼兩次 `tool_tree_sha256()` 相等；在 `tests/`、`spikes/`、`scripts/` 各新增暫存檔、改 `README.md` 後值不變（暫存檔與 README 於 finally 還原） |
| `test_tree_sha256_algorithm` | 對 `tmp_path` 建 `a.txt`(`b"x"`)、`d/b.txt`(`b"y"`)，`tree_sha256(tmp_path, include=lambda p: True)` 等於 `sha256(("a.txt\0"+sha256(b"x")+"\n"+"d/b.txt\0"+sha256(b"y")+"\n").encode()).hexdigest()` |
| `test_tree_sha256_matches_p1_golden` | 讀 `contracts/golden/tree_sha256.ndjson`（P1 T18；含非 ASCII 檔名如 `中.py` 的向量），每筆以 `tree_sha256` 在 `tmp_path` 重建檔案後結果等於 golden（不存在即紅燈，不得 SKIP） |
| `test_package_tree_scope` | `package_tree_sha256(REPO_ROOT)` 只納入 relpath 以 `sw_core/` 開頭者；`repo_tree_sha256` 另含 `serialwrapd.py`、`VERSION`；兩值不同 |
| `test_contract_revision_shape` | `contract_revision(REPO_ROOT)[0]` 為 `None` 或 `re.fullmatch(r"[0-9a-f]{40}")` |
| `test_main_module_guarded` | `src/uart_stdio_bridge/__main__.py` 與 `dut/__main__.py` 的 AST 頂層呼叫只出現在 `if __name__ == "__main__":` 區塊內 |

- [ ] **T1-2：執行並確認 FAIL**：`python3 -m pytest -q tools/uart_stdio_bridge/tests` → 預期 `test_src_exists` 等以 `AssertionError`／`ModuleNotFoundError` 失敗。
- [ ] **T1-3：commit 測試**：`git add tools/uart_stdio_bridge/tests && git commit -m "test(bridge): 新增工具骨架與產品邊界守衛的失敗測試"`（含 Co-authored-by trailer）。

### I 步驟

- [ ] **I1-1：實作**：
  1. 依 Interfaces 建 `pyproject.toml`、`__init__.py`、`harness_codes.py`、`identity.py`、`cli.py`（argparse，`prog="uart-stdio-bridge"`；`run`／`compare`／`selftest`／`dut` 子命令先註冊並在對應模組 import 失敗時印 `{"error":"not_implemented","subcommand":...}` 至 stderr、exit 2，由後續 Task 取代）、`__main__.py`（`if __name__ == "__main__": raise SystemExit(main())`）。
  2. `README.md`：拓樸圖（照 §15.3）、方向定義（TX＝serialwrap→bridge→DUT stdin）、「bridge 只持有端 B」、證據檔列表（§15.6.1）、本工具不得 import 產品、`HARNESS_*` 非產品碼。
  3. `.github/workflows/tests.yml` 新增：
     - job `bridge-tools`（`ubuntu-latest`，Python 3.12）：`python3 -m pip install --disable-pip-version-check pytest==9.0.3 PyYAML==6.0.3 jsonschema==4.10.3`，`sudo apt-get install -y socat`，執行 `python3 -m pytest -q tools/uart_stdio_bridge/tests`。
     - job `bridge-tools-windows`（`windows-latest`，Python 3.12）：同依賴＋`pyserial==3.5`，執行 `python -m pytest -q tools/uart_stdio_bridge/tests -m "not posix_pty and not docker and not windows_com"`（marker 由 Task 3 起註冊於工具 conftest）。此 job 只證明免驅動 Windows L1，不得計為 `windows_com`。
- [ ] **I1-2：執行並確認 PASS**：`python3 -m pytest -q tools/uart_stdio_bridge/tests/test_boundary.py tools/uart_stdio_bridge/tests/test_cli_skeleton.py` 全綠（`test_harness_codes_disjoint_from_product_codes` 依賴 P0 的 `contracts/error_codes.yaml`、`test_tree_sha256_matches_p1_golden` 依賴 P1 T18 golden；未合併時本 Task 不得宣告完成）。另跑 `python3 -m pytest -q tests/` 確認無新失敗。
- [ ] **I1-3：commit**：`git commit -m "feat(bridge): 建立 uart_stdio_bridge 獨立套件骨架與 CI 自測 job"`，同時加入 `changelog.d/rust-p2-bridge-skeleton.md`。

---

## Task 2：證據格式、CRC golden vectors、EvidenceWriter

**Test author model:** agy-gemini-3.8-flash-high　**Implementer model:** haiku-xhigh（records／clock／manifest／schema）＋codex-gpt-6-luna-max（`evidence/writer.py`）
**Changelog fragment:** `changelog.d/rust-p2-bridge-evidence-schema.md`（`type: feat`、`scope: test-infra`）
**BLOCKED-OWNER evidence:** 無。

**Files:**
- Create: `tools/uart_stdio_bridge/src/uart_stdio_bridge/evidence/{__init__.py,records.py,writer.py,manifest.py,clock.py}`
- Create: `tests/acceptance/bridge_record.schema.json`、`tests/acceptance/bridge_control.schema.json`、`tests/acceptance/correlation.schema.json`、`tests/acceptance/l15_manifest.schema.json`（`comparison.schema.json` 於 Task 7）
- Create: `tools/uart_stdio_bridge/tests/test_records.py`、`tools/uart_stdio_bridge/tests/test_writer.py`、`tools/uart_stdio_bridge/tests/fixtures/crc_golden.json`
- Create: `tests/test_acceptance_schemas.py`（repo pytest；驗 schema 本身合法、與工具產出相容）

**Interfaces:**

```python
# evidence/records.py
DIRS = ("TX", "RX", "META")
OBSERVATIONS = {"serial_read": "TX", "dut_stdin_write": "TX", "dut_stdout_read": "RX", "serial_write": "RX"}
META_OBSERVATION = "bridge_meta"
BRIDGE_SOURCE = "bridge:external"
def crc32_hex(payload: bytes) -> str          # f"{zlib.crc32(payload) & 0xFFFFFFFF:08x}"，與 WAL 相同變體（盤點 04 §0-2）
def b64(payload: bytes) -> str                 # base64.b64encode(...).decode("ascii")，標準 alphabet、含 padding
def wall_ts_now() -> str                       # datetime.now(timezone.utc).isoformat()（微秒為 0 時省略小數，與 WAL 相同）
def dumps_record(rec: dict[str, Any]) -> bytes # json.dumps(rec, ensure_ascii=False, sort_keys=True, separators=(",",":")).encode("utf-8") + b"\n"
def make_data_record(*, run_id: str, case_id: str, seq: int, mono_ts_ns: int, wall_ts: str, clock_id: str,
                     port_id: str, endpoint_generation: int, dut_generation: int, observation: str,
                     stream_offset: int, payload: bytes) -> dict[str, Any]
def make_meta_record(*, run_id: str, case_id: str, seq: int, mono_ts_ns: int, wall_ts: str, clock_id: str,
                     port_id: str, endpoint_generation: int, dut_generation: int,
                     meta: dict[str, Any], loss_flag: bool = False) -> dict[str, Any]

# evidence/clock.py
def clock_id() -> str
    # Linux：f"linux-monotonic:{/proc/sys/kernel/random/boot_id 去空白}"
    # Windows：f"windows-qpc:{socket.gethostname()}:{int(time.time() - time.monotonic())}"
    # 其他：f"{sys.platform}-monotonic:{socket.gethostname()}:{os.getpid()}"

# evidence/writer.py
class EvidenceFailure(RuntimeError):
    code: str                                  # HARNESS_* 之一
class EvidenceWriter:
    def __init__(self, path: pathlib.Path, *, run_id: str, case_id: str, clock_id: str,
                 max_pending_records: int = 65_536, block_timeout_s: float = 5.0,
                 os_write: Callable[[int, bytes], int] = os.write,
                 os_fsync: Callable[[int], None] = os.fsync) -> None
    def start(self) -> None                    # 開檔 O_WRONLY|O_CREAT|O_EXCL|O_APPEND（Windows 加 O_BINARY；POSIX mode 0o600），啟動單一 writer thread
                                               # 證據可能含登入資料（§15.6.4）：父目錄由 ensure_private_dir() 建立
    def emit_data(self, *, port_id: str, endpoint_generation: int, dut_generation: int,
                  observation: str, payload: bytes) -> int      # 回傳 seq；len(payload)==0 時 raise ValueError
    def emit_meta(self, *, port_id: str, endpoint_generation: int, dut_generation: int,
                  meta: dict[str, Any], loss_flag: bool = False) -> int
    def offsets(self, port_id: str, endpoint_generation: int) -> dict[str, int]   # 四個 observation 的目前累計 offset
    def flush(self, timeout_s: float = 5.0) -> None            # 等 queue 清空＋fsync；失敗 raise EvidenceFailure
    def close(self) -> dict[str, Any]                          # {"records": int, "bytes": int, "sha256": str}；冪等
    @property
    def failure(self) -> str | None            # 第一個 HARNESS_* 失敗碼；一旦非 None，後續 emit_* 一律 raise EvidenceFailure

# evidence/manifest.py
def ensure_private_dir(path: pathlib.Path) -> None   # mkdir(parents) 後 POSIX chmod 0o700；Windows 不改 ACL（README 註明需放在使用者私有目錄）
def write_json_atomic(path: pathlib.Path, obj: dict[str, Any]) -> None   # tmp＋os.replace，indent=2、sort_keys、尾端換行；POSIX 檔案 0o600
def bridge_manifest(*, run_id: str, case_id: str, tool: dict, transport: dict, dut: dict,
                    evidence: dict, platform_info: dict, started_at: str, closed_at: str | None,
                    closed_cleanly: bool, failure: str | None) -> dict[str, Any]
def platform_info() -> dict[str, Any]
    # 共同：{"os": platform.system(), "release", "version", "machine", "python": platform.python_version(),
    #        "evidence_insufficient": [欄位名...]}
    # Windows 另加（§1.1.1、§1.2、§15.8.3，唯讀查詢；任何一項查詢失敗或權限不足記字串 "unknown" 並把欄位名加入
    #   evidence_insufficient，不得填 0／False 冒充）：
    #   "windows_build"（`ver` 與 registry CurrentBuild.UBR）、"secure_boot"（Confirm-SecureBootUEFI → bool|"unknown"）、
    #   "hvci"（Win32_DeviceGuard SecurityServicesRunning 含 2 → bool|"unknown"）、
    #   "com_driver":{"name","version","sha256","install_method"}（只在 win-serial endpoint 時填；install_method 由
    #   `--com-driver-install-method` 參數提供，未提供記 "unknown"）、
    #   "endpoint_av":{"product","state"}（root\SecurityCenter2 AntiVirusProduct；查無或拒絕 → "unknown"）
```

**證據遮罩（§15.6.4）**：原始證據（`wire.ndjson`、`dut.stderr.log`、`cli.ndjson`、`wal/`）只存放於 `ensure_private_dir` 建立的目錄。對外報告用 `uart-stdio-bridge redact --evidence-dir D --out D.redacted --secret-env NAME ...`（Task 7 實作）產生遮罩衍生檔：payload 中出現的 secret bytes 以等長 `*` 取代並重算 `crc32`／`payload_b64`，寫 `redaction-manifest.json`（`{"schema_version":"redaction.v1","rules":[{"kind":"secret_env","name","len"}],"files":[{"path","raw_sha256","redacted_sha256"}],"byte_equivalent":false}`）；遮罩後不得宣稱保有 byte 等價證據（`byte_equivalent:false`）。`func-test-report.json` 與 `comparison.json` 只記 offset、長度與 hex 上下文（上下文與 secret 重疊處以 `*` 表示），不得內嵌完整 payload。

**`bridge.v1` 紀錄欄位（§15.6.2，全部必填；`sort_keys` 序列化）**

| 欄位 | 型別 | 規則 |
|---|---|---|
| `schema_version` | str | 固定 `"bridge.v1"` |
| `run_id`、`case_id` | str | 非空 |
| `seq` | int | 從 1 起每筆 +1，跨 observation 共用；與 WAL `seq` 無關 |
| `mono_ts_ns` | int | `time.monotonic_ns()` |
| `wall_ts` | str | `wall_ts_now()` |
| `clock_id` | str | `clock_id()` |
| `port_id` | str | 邏輯受測埠（如 `COM0`） |
| `endpoint_generation` | int | ≥1；`transport_reconnect` 後 +1 |
| `dut_generation` | int | ≥1；DUT 程序重啟或邏輯 reboot 後 +1 |
| `dir` | str | `TX`／`RX`／`META`；data 紀錄由 `OBSERVATIONS[observation]` 決定 |
| `observation` | str | data：四值之一；META：`"bridge_meta"` |
| `stream_offset` | int \| null | data：該 `(port_id, endpoint_generation, observation)` 寫入此筆前的累計 bytes；META：`null` |
| `len` | int | `len(payload)`；META 為 0 |
| `payload_b64` | str | META 為 `""` |
| `crc32` | str | 8 位小寫 hex；META 為 `"00000000"` |
| `source` | str | 固定 `"bridge:external"` |
| `cmd_id` | null | 原始 bridge 紀錄一律 `null`（§15.6.2「不猜測」） |
| `loss_flag` | bool | data 恆 `false`；META 表示證據缺口時為 `true` |
| `meta` | object | data 為 `{}`；META 至少含 `"event"` |

META `event` 值域（固定，新增需改 schema）：`bridge_start`、`bridge_stop`、`peer_state`（`{"open": bool}`）、`endpoint_generation`（`{"from":g,"to":g+1,"reason"}`）、`dut_generation`（`{"from","to","reason":"restart"|"logical_reboot"}`）、`dut_stdout_eof`、`dut_exited`（`{"returncode"}`）、`write_uncertain`（`{"observation","offset_start","requested_len","accepted_len":null,"error"}`，`offset_start`＝該 observation 不確定區間起點的 `stream_offset`，區間為 `[offset_start, offset_start+requested_len)`，`loss_flag:true`）、`io_error`（`{"observation","error"}`）、`mark`（`{"label","op_id"}`）、`fault`（`{"action_id","action","discarded_len"}`）、`evidence_failure`（`{"code"}`，`loss_flag:true`）、`quiesce`（`{"action_id","phase":"begin"|"end","settled"}`，Task 5 `barrier mode:"quiesce"`）、`checkpoint`（`{"checkpoint_id","wire_len"}`，Task 5）。

`bridge_control.schema.json`（`control.v1`，`control.ndjson`）：`schema_version`、`run_id`、`case_id`、`seq`、`mono_ts_ns`、`wall_ts`、`clock_id`、`action_id`（str）、`action`（str）、`phase`（`requested`／`completed`／`failed`）、`params`（object）、`result`（object \| null）、`error`（str \| null，`HARNESS_*`）、`endpoint_generation`、`dut_generation`、`wire_seq`（該 phase 發生時 `wire.ndjson` 最後 seq）。

`correlation.schema.json`（`correlation.v1`，由協調器寫，Task 10 實作）：`schema_version`、`run_id`、`case_id`、`seq`、`mono_ts_ns`、`wall_ts`、`clock_id`、`op_id`、`step_index`、`kind`（`op_start`／`op_end`／`marker`）、`action`、`argv`（list \| null）、`rc`（int \| null）、`response_sha256`（str \| null）、`cmd_id`（str \| null，CLI 回應中的值，僅關聯用）、`wal_seq`（int \| null）、`settled`（bool）、`wire`（`{"endpoint_generation","dut_generation","wire_seq","offsets":{serial_read,dut_stdin_write,dut_stdout_read,serial_write}}` \| null）、`marker`（str \| null）、`marker_dir`（`TX`／`RX` \| null）。

`l15_manifest.schema.json`（`l15-manifest.v1`，`manifest.json`，協調器寫）：`schema_version`、`run_id`、`case_id`、`created_at`、`mode`（`pty`／`bridge`）、`topology`（`dut_in_container_daemon_native`／`dut_local_process_daemon_native`／`faketarget_pty`）、`transport`（`{"mode":"virtual_pty"|"virtual_com0com"|"physical_usb_uart"|"faketarget_pty","port_id","daemon_side","bridge_side"}`）、`artifacts`（`{"cli":{"path","sha256","version_stdout"},"daemon":{"argv","sha256"|null,"implementation":"python"|"rust","package_tree_sha256"|null,"repo_tree_sha256"|null},"release_manifest_sha256":str|null,"deployed_content":{"start":DeployedContent,"end":DeployedContent}}`；`DeployedContent` 為 Task 10 `realhw.deployed_content.measure` 的 `deployed-content.v1` 物件；此鍵由 Task 10 加入 schema 並自 Task 10 起必填，Task 2 的 schema fixture 於 Task 10 一併補上，R5-F4）、`tool`（`{"name","version","tree_sha256","approved":bool}`）、`dut`（`{"mode":"docker"|"local-process"|"faketarget","image","image_id"|null,"target_sha256","scenario_sha256"|null,"seed"}`）、`repo_git_revision`（str \| null，`git rev-parse HEAD`）、`repo_dirty`（bool）、`contract_revision`（str \| null：`git rev-parse HEAD:contracts` 40 hex）、`contract_dirty`（bool：`contracts/` 有未 commit 變更）、`cases_yaml_sha256`（str \| null；null 只允許在 Task 12 合併前的開發執行，此時 `gate_eligible` 必為 false）、`case`（`{"source_path","sha256","revision"}`）、`platform`（Task 2 `platform_info()`）、`bridge_manifest_sha256`（str \| null）、`evidence`（`{"wire":{"path","sha256"},"control":{...},"correlation":{...},"dut_stderr":{...},"wal_dir":str,"cli_log":{...}}`）。

### T 步驟

- [ ] **T2-1：撰寫失敗測試**。`tools/uart_stdio_bridge/tests/fixtures/crc_golden.json`（已以 `zlib.crc32` 實測）：

```json
{"crc32_variant": "zlib-ieee-802.3 (poly 0xEDB88320 reflected, init 0xFFFFFFFF, xorout 0xFFFFFFFF), 8 lowercase hex",
 "vectors": [
  {"payload_hex": "", "crc32": "00000000"},
  {"payload_hex": "313233343536373839", "crc32": "cbf43926"},
  {"payload_hex": "00", "crc32": "d202ef8d"},
  {"payload_hex": "ffffffff", "crc32": "ffffffff"},
  {"payload_hex": "68656c6c6f0d0a1b5b306d0b7fff", "crc32": "a81bc28c"},
  {"payload_hex": "RANGE_0_255", "crc32": "29058c73"}
 ]}
```

（`RANGE_0_255` 代表 `bytes(range(256))`，測試內展開。）

`tools/uart_stdio_bridge/tests/test_records.py` 關鍵測試：

```python
def test_crc_golden_vectors():
    data = json.loads((FIX / "crc_golden.json").read_text(encoding="utf-8"))
    for v in data["vectors"]:
        payload = bytes(range(256)) if v["payload_hex"] == "RANGE_0_255" else bytes.fromhex(v["payload_hex"])
        assert records.crc32_hex(payload) == v["crc32"]

def test_data_record_exact_bytes():
    rec = records.make_data_record(run_id="r1", case_id="c1", seq=1, mono_ts_ns=5, wall_ts="2026-10-15T03:06:17.663778+00:00",
                                   clock_id="linux-monotonic:x", port_id="COM0", endpoint_generation=1,
                                   dut_generation=1, observation="serial_read", stream_offset=0, payload=b"\x00\xff\n")
    assert records.dumps_record(rec) == (
        b'{"case_id":"c1","clock_id":"linux-monotonic:x","cmd_id":null,"crc32":"' + records.crc32_hex(b"\x00\xff\n").encode()
        + b'","dir":"TX","dut_generation":1,"endpoint_generation":1,"len":3,"loss_flag":false,"meta":{},'
        b'"mono_ts_ns":5,"observation":"serial_read","payload_b64":"AP8K","port_id":"COM0","run_id":"r1",'
        b'"schema_version":"bridge.v1","seq":1,"source":"bridge:external","stream_offset":0,'
        b'"wall_ts":"2026-10-15T03:06:17.663778+00:00"}\n')
```

其餘以表列：

| 測試名（`test_records.py`／`test_writer.py`） | 斷言 |
|---|---|
| `test_observation_dir_mapping` | 四個 observation 產出的 `dir` 分別為 TX、TX、RX、RX |
| `test_meta_record_shape` | `make_meta_record(meta={"event":"mark","label":"x","op_id":"o"})` 的 `observation=="bridge_meta"`、`stream_offset is None`、`len==0`、`payload_b64==""`、`crc32=="00000000"` |
| `test_meta_requires_event` | `meta={}` → `ValueError` |
| `test_unknown_observation_rejected` | `observation="dut_read"` → `ValueError` |
| `test_wall_ts_format` | `re.fullmatch(r"\d{4}-\d\d-\d\dT\d\d:\d\d:\d\d(\.\d{6})?\+00:00", wall_ts_now())` |
| `test_records_validate_against_schema` | 以 `jsonschema.Draft202012Validator(tests/acceptance/bridge_record.schema.json)` 驗 data 與 META 紀錄皆無錯 |
| `test_writer_seq_and_offsets` | `emit_data` 三次（serial_read 3B、serial_read 5B、serial_write 2B）後 `close()`；讀檔三行的 `seq==[1,2,3]`、`stream_offset==[0,3,0]`；`offsets("COM0",1)=={"serial_read":8,"dut_stdin_write":0,"dut_stdout_read":0,"serial_write":2}` |
| `test_writer_offsets_reset_per_endpoint_generation` | gen=1 寫 4B 後 gen=2 寫 1B，gen=2 該筆 `stream_offset==0` |
| `test_writer_close_digest` | `close()["sha256"] == sha256(檔案全部 bytes)`、`records==N`、`bytes==檔案大小`；第二次 `close()` 回相同 dict |
| `test_writer_refuses_existing_file` | 目標檔已存在 → `start()` raise `EvidenceFailure`，`code=="HARNESS_EVIDENCE_WRITE_FAILED"` |
| `test_writer_disk_full_fail_stop` | 注入 `os_write` 於第 2 次呼叫 raise `OSError(errno.ENOSPC,"x")`；之後 `failure=="HARNESS_DISK_FULL"`，再 `emit_data` raise `EvidenceFailure`；`close()` 不 raise 且回傳 `records==1` |
| `test_writer_generic_write_error` | 注入 `OSError(errno.EIO)` → `failure=="HARNESS_EVIDENCE_WRITE_FAILED"` |
| `test_writer_short_write_retried` | 注入 `os_write` 每次只寫 3 bytes → 檔案內容等於完整序列化（writer 迴圈補寫，不丟、不重複） |
| `test_writer_overflow_fail_stop` | `max_pending_records=2, block_timeout_s=0.2`，注入 `os_write` 阻塞（`threading.Event().wait()`）；第 4 次 `emit_data` 於約 0.2 s 後 raise `EvidenceFailure` 且 `code=="HARNESS_EVIDENCE_OVERFLOW"` |
| `test_writer_fsync_failure` | `os_fsync` raise `OSError` → `flush()` raise `EvidenceFailure`，`failure=="HARNESS_EVIDENCE_WRITE_FAILED"` |
| `test_writer_concurrent_emit_unique_seq` | 4 threads 各 emit 1000 筆，檔案 seq 恰為 1..4000 無重複、每個 `(observation)` 的 offset 連續 |
| `test_writer_empty_payload_rejected` | `emit_data(payload=b"")` → `ValueError` |
| `test_writer_private_permissions`（POSIX） | `start()` 後證據檔 mode 為 0o600；`ensure_private_dir` 建立的目錄 mode 為 0o700；`write_json_atomic` 產出檔 0o600 |
| `test_platform_info_windows_unknown_not_zero` | monkeypatch `platform.system()=="Windows"` 並令所有查詢 raise：`secure_boot`、`hvci`、`endpoint_av.product` 皆為 `"unknown"`，且三者都在 `evidence_insufficient`；不得出現 `False`／`0` |
| `test_platform_info_linux_shape` | Linux 上 keys ⊇ `{"os","release","version","machine","python","evidence_insufficient"}`，不含 Windows 專屬鍵 |

`tests/test_acceptance_schemas.py`（repo pytest；依 P0 T14 規則模組層 `pytestmark = [pytest.mark.core_portable, pytest.mark.memory_only]`；需要 `jsonschema==4.10.3`，P0 T8 已在 `tests.yml` 安裝；未安裝時測試以 ImportError 失敗，不得 importorskip）：

| 測試名 | 斷言 |
|---|---|
| `test_all_acceptance_schemas_are_valid_draft2020` | `tests/acceptance/*.schema.json` 每一個 `Draft202012Validator.check_schema` 通過，且含 `"$schema": "https://json-schema.org/draft/2020-12/schema"` 與 `$id` |
| `test_bridge_record_schema_required_fields` | `required` 恰為上表 20 個欄位；`additionalProperties is False` |
| `test_bridge_record_schema_rejects_tamper` | 以合法 data 紀錄為基準，分別刪 `crc32`、把 `crc32` 改成大寫、`len` 設 -1、`dir` 設 `"tx"`、`cmd_id` 設 `"c1"`、`observation` 設 `"x"` → 每一個都驗證失敗 |

- [ ] **T2-2：執行並確認 FAIL**：`python3 -m pytest -q tools/uart_stdio_bridge/tests/test_records.py tools/uart_stdio_bridge/tests/test_writer.py tests/test_acceptance_schemas.py` → `ModuleNotFoundError: uart_stdio_bridge.evidence`、schema 檔不存在。
- [ ] **T2-3：commit 測試**：`git commit -m "test(bridge): 新增 bridge.v1 紀錄、CRC golden 與證據 writer 的失敗測試"`。

### I 步驟

- [ ] **I2-1：實作**：
  1. `records.py` 照 Interfaces；`make_*` 驗證 `observation`、`meta["event"]`、generation ≥1、`seq ≥1`。
  2. `writer.py`：`threading.Lock` 內分配 `seq` 與 `stream_offset`（同一鎖段取 `mono_ts_ns`／`wall_ts`，保證 seq 與觀測順序一致），把序列化後 bytes 放入 `queue.Queue(maxsize=max_pending_records)`；`put(timeout=block_timeout_s)` 逾時即設 `failure="HARNESS_EVIDENCE_OVERFLOW"`、寫一筆 `evidence_failure` META（若仍可寫）並 raise。writer thread 以 `os_write` 迴圈寫完整 bytes（處理短寫），`ENOSPC`／`EDQUOT`／`EFBIG` → `HARNESS_DISK_FULL`，其他 `OSError` → `HARNESS_EVIDENCE_WRITE_FAILED`。同時以 `hashlib.sha256` 累計已寫 bytes 作 `close()` digest。
  3. `clock.py`、`manifest.py` 照 Interfaces（`writer.py` 由 codex-gpt-6-luna-max 實作，其餘由 haiku-xhigh 實作）。
  4. 四個 schema 檔：JSON Schema draft 2020-12，`$id` 為 `https://serialwrap.local/schemas/<name>`，`additionalProperties:false`；`bridge_record.schema.json` 以 `if/then` 區分 data 與 META（data：`observation` ∈ 四值、`len ≥ 1`、`stream_offset ≥ 0`、`loss_flag == false`、`meta == {}`；META：`observation == "bridge_meta"`、`stream_offset == null`、`len == 0`、`meta.event` enum 照上列）；`crc32` pattern `^[0-9a-f]{8}$`；`payload_b64` pattern `^[A-Za-z0-9+/]*={0,2}$`。
- [ ] **I2-2：執行並確認 PASS**：同 T2-2 指令全綠；`python3 -m pytest -q tests/` 無新失敗。確認 `.github/workflows/tests.yml` 的 `pytest` job 已由 P0 T8 安裝 `jsonschema==4.10.3`（只確認、不重複修改該行；若缺少即停下回報 P0，不在本 Task 另加）。
- [ ] **I2-3：commit**：`git commit -m "feat(bridge): 實作 bridge.v1 證據紀錄、CRC golden 與 fail-stop 證據 writer"`＋fragment。

---

## Task 3：端點 adapter（POSIX PTY／Windows pySerial）

**Test author model:** haiku-xhigh　**Implementer model:** codex-gpt-6-luna-max
**Changelog fragment:** `changelog.d/rust-p2-bridge-endpoints.md`（`type: feat`、`scope: test-infra`）
**BLOCKED-OWNER evidence:** 無（OD-15 起改為可執行 gate）。
**擁有者 host 可執行 gate（OD-15，G1）：**
- `WinSerialEndpoint` 對真實 COM 的 `test_cap_windows_com__*` 測試：在擁有者 host 以 `SW_TEST_COM_PAIR=COM20:COM21` 經 WSL interop 原生執行（`scripts/stage0/win-pytest.sh -m windows_com tools/uart_stdio_bridge/tests`）；Linux 與 hosted CI 只以注入的 fake `serial` 模組驗 adapter 邏輯，**不得**計為 `windows_com`。擁有者 host 不可用時記「證據不足」。
- com0com 關閉 COM-B 時 COM-A 的實際行為（決定 `transport_disconnect` 能力是否成立，§15.8.3）：屬 Spike 1 剩餘項「peer-close 語意」，由 Task 18 `spike1_remaining.py peer-close` 實測；結果寫回本 Task 的能力表前，`transport_disconnect` 在 Windows 維持未宣告。

**Files:**
- Create: `tools/uart_stdio_bridge/src/uart_stdio_bridge/endpoints/{__init__.py,base.py,posix_pty.py,win_serial.py}`
- Create: `tools/uart_stdio_bridge/tests/test_posix_pty_endpoint.py`、`tools/uart_stdio_bridge/tests/test_win_serial_endpoint.py`、`tools/uart_stdio_bridge/tests/test_pty_kernel_semantics.py`
- Modify: `tools/uart_stdio_bridge/tests/conftest.py`（註冊 markers `posix_pty`、`windows_com`、`physical_uart`、`docker`；`windows_com` 測試在 `SW_TEST_COM_PAIR` 未設時以 `pytest.skip("cap:windows_com 未提供（證據不足，非 PASS）")` 顯示）

**Interfaces:**

```python
# endpoints/base.py
TRANSPORT_MODES = ("virtual_pty", "virtual_com0com", "physical_usb_uart")
class EndpointError(OSError): ...
class EndpointWriteUncertain(EndpointError):
    def __init__(self, requested_len: int, detail: str) -> None
    requested_len: int
class Endpoint(Protocol):
    kind: str                     # "posix-pty" | "win-serial"
    transport_mode: str           # TRANSPORT_MODES 之一
    generation: int               # endpoint_generation，open() 後為 1
    def open(self) -> None
    def read(self, max_bytes: int, timeout_s: float) -> bytes
        # 無資料、peer 未開啟或 disconnect 後尚未 reconnect：等至 timeout 回 b""（不 raise）
        # 從未 open 或已 close：raise EndpointError；I/O 錯誤亦 raise EndpointError
    def write_some(self, data: bytes, timeout_s: float) -> int
        # 回傳實際被 OS 接受的 bytes（0 < n ≤ len(data)）；timeout 內 0 bytes 被接受：回 0
        # disconnect 後尚未 reconnect：等至 timeout 回 0；從未 open 或已 close：raise EndpointError
        # 接受長度不明：raise EndpointWriteUncertain（呼叫端記 write_uncertain META、loss_flag）
    def peer_open(self) -> bool | None
        # POSIX：slave 端目前是否有 opener（poll 無 POLLHUP）；Windows：None（不可得知）
    def capabilities(self) -> dict[str, bool]
        # {"transport_disconnect": bool, "by_id_link": bool, "peer_state": bool}
    def disconnect(self, *, remove_link: bool) -> None
    def reconnect(self) -> int                       # 回傳新 generation（舊 +1）
    def link_remove(self) -> None
    def link_add(self) -> None
    def close(self) -> None                          # 冪等
    def describe(self) -> dict[str, Any]             # 進 bridge-manifest.transport

# endpoints/__init__.py
def open_endpoint(spec: dict[str, Any]) -> Endpoint
    # 建立並 open adapter 後回傳；spec={"kind":"posix-pty","by_id_link":str,"peer_gate":bool}
    #    | {"kind":"win-serial","com":str,"baud":int,"transport_mode":"virtual_com0com"|"physical_usb_uart","allow_peer_close":bool}

# endpoints/posix_pty.py
class PosixPtyEndpoint:
    def __init__(self, by_id_link: pathlib.Path, *, peer_gate: bool = True) -> None
    slave_path: str | None          # 目前世代的 /dev/pts/N
# endpoints/win_serial.py
class WinSerialEndpoint:
    def __init__(self, com: str, *, baud: int = 115200, transport_mode: str = "virtual_com0com",
                 allow_peer_close: bool = False, serial_factory: Callable[..., Any] | None = None) -> None
```

**POSIX 行為（已在 WSL2 Linux 6.6 實測，`test_pty_kernel_semantics.py` 把它釘成事實測試）**：
- slave 無任何 opener 時，master 的 `poll` 回 `POLLHUP`、`read` 得 `EIO`；但 `write` 到 master **成功**，bytes 會交給下一個 opener。
- `tty.setraw(slave)` 後再關閉 slave，raw termios 狀態在 pty 存續期間保留給下一個 opener。

因此 `PosixPtyEndpoint.open()`：`pty.openpty()` → `tty.setraw(slave_fd)` → 記 `slave_path=os.ttyname(slave_fd)` → **關閉 slave_fd**（bridge 不持有端 A，§15.3）→ master 設 `O_NONBLOCK` → 建 `by_id_link` symlink（先移除舊 link）。`read`：以不超過 10 ms 的 poll 切片等待；若 event 同時帶 `POLLIN|POLLHUP`，先 `os.read` 排空已到資料，只有 `POLLHUP` 且無 `POLLIN` 才視為 peer 關閉並節流等待至 timeout；`EIO` 視同 peer 關閉。`write_some`：每次 `os.write` 前都重新檢查 peer gate；`peer_gate=True` 且 `peer_open()` 為 False 時**不寫**、等待至 timeout 回 0，`EAGAIN`／`EIO` 重試亦同。資料 I/O 與端點生命週期以鎖同步，跨入新 generation 的讀取回 `b""`、尚未接受資料的寫入回 0。`disconnect(remove_link=True)`：移除 link、關 master（之後 slave opener 讀到 EIO／hangup）；斷線到重連期間，讀取等滿 timeout 回 `b""`、寫入等滿 timeout 回 0。`reconnect()`：重新 openpty（新 slave 路徑）、重建 link、`generation += 1`。PTY 核心可能保留已接受但未讀的 bytes 給下一個 opener；情境若要求上下文隔離，必須先 `reconnect()`。`disconnect(remove_link=False)` 只可用於隔離測試目錄，因留下的 pts symlink 可能指向後續被重新指派的路徑。`peer_open()` 是即時取樣；週期性 watcher 可能漏掉兩次取樣間完成的關閉與重開。`capabilities()` 全 True。

**Windows 行為**：`serial_factory` 預設為 `serial.Serial`（lazy import，非 Windows 不 import）。`open()`：`Serial(port=com, baudrate=baud, bytesize=8, parity="N", stopbits=1, timeout=0.05, write_timeout=None, exclusive=None)`；失敗 raise `EndpointError`。`read`：timeout 未變時不重設 pySerial 屬性；需要時設 `timeout=timeout_s` 後 `read(max_bytes)`（pySerial 回傳已到 bytes，可短讀）。`write_some`：設 `write_timeout=max(timeout_s, 0.001)` 後 `n = ser.write(data)`；逾時 → raise `EndpointWriteUncertain(len(data), "write timeout")`（pySerial 3.5 Windows 在逾時時不回報已寫長度）；必須驗證 `0 <= n <= len(data)`，超界視為長度不確定。`peer_open()` 回 None。`capabilities()`：`transport_disconnect=False`、`by_id_link=False`、`peer_state=False`。`disconnect()`：`allow_peer_close=False` 時 raise `EndpointError("HARNESS_CAPABILITY_UNAVAILABLE")`；True 時關閉 COM-B 並在 `describe()` 標 `"last_fault":"peer_close"`（不是 transport_disconnect）。斷線後至重連前，`read`／`write_some` 分別等滿 timeout 回 `b""`／0。`reconnect()`：若仍開啟 COM handle，必須 `allow_peer_close=True` 才可關閉後重開，並標 `last_fault="peer_close"`；若已 disconnect，直接重開；成功後 `generation += 1`。I/O 與生命週期操作以鎖同步，設定值相同時不重設 pySerial timeout。`link_remove/link_add` raise `EndpointError("HARNESS_CAPABILITY_UNAVAILABLE")`。

### T 步驟

- [ ] **T3-1：撰寫失敗測試**。`test_pty_kernel_semantics.py`（`@pytest.mark.posix_pty`，非 Linux 時 `pytest.skip("cap:posix_pty")`）：

```python
def test_master_pollhup_and_write_when_no_slave_opener():
    m, s = pty.openpty(); tty.setraw(s); path = os.ttyname(s); os.close(s)
    p = select.poll(); p.register(m, select.POLLIN | select.POLLHUP)
    assert any(ev & select.POLLHUP for _, ev in p.poll(100))
    assert os.write(m, b"abc") == 3                     # 無 opener 仍接受寫入
    s2 = os.open(path, os.O_RDWR | os.O_NOCTTY)
    assert os.read(s2, 10) == b"abc"                    # 交給下一個 opener
    attrs = termios.tcgetattr(s2)
    assert not (attrs[3] & termios.ECHO) and not (attrs[3] & termios.ICANON)   # raw 保留
    os.close(s2); os.close(m)
```

`test_posix_pty_endpoint.py`（全部 `@pytest.mark.posix_pty`；「daemon 側」以測試自行 `os.open(link)` 模擬受測端 A）：

| 測試名 | 斷言 |
|---|---|
| `test_open_creates_link_and_releases_slave` | `open()` 後 `by_id_link` 為 symlink 指向 `slave_path`；`/proc/self/fd` 中沒有任何 fd 指向 `slave_path`（bridge 不持有端 A） |
| `test_peer_open_tracks_opener` | 開啟前 `peer_open() is False`；測試 `os.open(link)` 後 ≤0.5 s 內 `peer_open() is True`；關閉後回 False |
| `test_full_byte_range_both_directions` | peer 寫 `bytes(range(256))*16` → `read` 累積後完全相等；`write_some` 寫同資料 → peer 讀到完全相等（無換行轉換、無 echo） |
| `test_peer_gate_blocks_write_without_opener` | 無 opener 時 `write_some(b"x", 0.2)==0` 且耗時 ≥0.2 s；之後 peer 開啟讀取 0.2 s 內讀不到任何 byte |
| `test_peer_gate_disabled_writes_queue` | `peer_gate=False` 時無 opener 的 `write_some(b"x",0.2)==1` |
| `test_write_without_peer_at_backpressure_does_not_spin` | 無 peer 且 PTY buffer 滿時，`peer_gate=False` 的寫入等滿 timeout 回 0，CPU 使用接近 0 |
| `test_partial_write_reports_accepted_len` | peer 開啟但不讀；反覆 `write_some(b"\x55"*65536, 0.05)` 直到回傳 < 要求長度；累計接受量等於 peer 之後讀出的總量 |
| `test_read_timeout_returns_empty` | peer 開啟無資料時 `read(4096, 0.1)==b""` 且耗時 ≥0.1 s |
| `test_read_drains_pending_after_peer_close` | peer 寫 `b"LAST"` 後立即關閉；`read` 仍回傳尾端 bytes、`peer_open() is False`，下一個 opener 讀不到舊資料 |
| `test_idle_read_without_peer_waits_without_spinning` | 無 opener 時 `read(4096,0.1)` 等滿 timeout，CPU 使用接近 0 |
| `test_peer_gate_rechecks_after_backpressure` | 注入第一次 `os.write` 回 EAGAIN 並關閉 peer；重試不得寫入，下一個 opener 讀不到該資料 |
| `test_disconnected_endpoint_waits_then_reconnects` | disconnect 後 read 等滿 timeout 回 `b""`、write 等滿 timeout 回 0；reconnect 世代遞增且可雙向傳輸 |
| `test_reconnect_during_pumps_does_not_raise` | pump thread 持續呼叫 read／write 時反覆 reconnect，不得因 fd 被關閉而 raise `EndpointError` |
| `test_open_endpoint_returns_open_adapter_and_validates_spec` | factory 回傳 generation 1 的已開啟 adapter，並拒絕無效 spec |
| `test_disconnect_removes_link_and_peer_sees_hangup` | `disconnect(remove_link=True)` 後 link 不存在；peer 的 `os.read` raise `OSError`（EIO）或回 `b""` |
| `test_reconnect_increments_generation_and_new_slave` | `reconnect()==2`、`generation==2`、新 `slave_path` 與舊不同、link 指向新路徑 |
| `test_link_remove_add_keeps_pty` | `link_remove()` 後 link 不存在但已開啟的 peer 仍可雙向傳輸；`link_add()` 後 link 恢復且指向同一 `slave_path` |
| `test_close_idempotent` | `close()` 兩次不 raise |
| `test_describe_fields` | `describe()` 含 `kind=="posix-pty"`、`transport_mode=="virtual_pty"`、`by_id_link`、`slave_path`、`generation`、`peer_gate` |

`test_win_serial_endpoint.py`（不需 Windows：注入 `FakeSerial`，在 Linux 與 Windows hosted CI 都跑；另有 `test_cap_windows_com__*` 測試需真實 COM pair；`test_` 前綴使 pytest 收集，無 `SW_TEST_COM_PAIR` 時顯示為 skipped「證據不足，非 PASS」）：

| 測試名 | 斷言 |
|---|---|
| `test_open_uses_exact_params` | `FakeSerial` 收到 `port="COMb", baudrate=115200, bytesize=8, parity="N", stopbits=1`，`timeout` 為數值 |
| `test_write_some_returns_accepted` | FakeSerial `write` 回 3 → `write_some(b"abcde",1.0)==3` |
| `test_write_some_rejects_impossible_accepted_lengths` | write 回傳負數或大於要求長度時 raise `EndpointWriteUncertain` |
| `test_io_does_not_reconfigure_unchanged_timeouts` | 連續相同 timeout 的 read／write 不重設 pySerial timeout；零秒 write timeout 設為至少 1 ms |
| `test_write_timeout_is_uncertain` | FakeSerial `write` raise `SerialTimeoutException` → raise `EndpointWriteUncertain`，`requested_len==5` |
| `test_capabilities_no_transport_disconnect` | `capabilities()=={"transport_disconnect":False,"by_id_link":False,"peer_state":False}`；`peer_open() is None` |
| `test_disconnect_refused_without_peer_close` | `allow_peer_close=False` 時 `disconnect(remove_link=False)` raise `EndpointError`，訊息含 `HARNESS_CAPABILITY_UNAVAILABLE` |
| `test_peer_close_labelled_not_transport_disconnect` | `allow_peer_close=True` 時 `disconnect` 後 `describe()["last_fault"]=="peer_close"` |
| `test_reconnect_requires_peer_close_permission_for_open_handle` | 已開啟 handle 且 `allow_peer_close=False` 時，reconnect 以 `HARNESS_CAPABILITY_UNAVAILABLE` 拒絕關閉 COM |
| `test_disconnected_endpoint_waits_then_reconnects` | disconnect 後 read／write 等滿 timeout 回空結果；reconnect 後 generation 遞增且可寫入 |
| `test_io_and_reconnect_are_serialized` | read 未完成時 reconnect 不可先關閉正在使用的 handle；兩個操作完成且不 raise `EndpointError` |
| `test_open_endpoint_dispatches_and_opens_win_serial` | factory 依 spec 建立並開啟 Windows adapter，參數正確透傳；無效 spec raise `ValueError` |
| `test_link_ops_unavailable` | `link_remove()`、`link_add()` 皆 raise `EndpointError`（含 `HARNESS_CAPABILITY_UNAVAILABLE`） |
| `test_transport_mode_validated` | `transport_mode="virtual_pty"` → `ValueError` |
| `test_cap_windows_com__roundtrip_full_byte_range` | （`@pytest.mark.windows_com`；讀 `SW_TEST_COM_PAIR`）測試以 pySerial 開 COMa 為受測端，endpoint 開 COMb；雙向 `bytes(range(256))*16` 完全相等 |
| `test_cap_windows_com__exclusive_second_open_fails` | endpoint 開 COMb 後再以 pySerial 開 COMb 必定失敗（Windows 通訊裝置獨占） |

- [ ] **T3-2：執行並確認 FAIL**：僅執行 Task 3 三個指定測試檔：`python3 -m pytest -q tools/uart_stdio_bridge/tests/test_posix_pty_endpoint.py tools/uart_stdio_bridge/tests/test_win_serial_endpoint.py tools/uart_stdio_bridge/tests/test_pty_kernel_semantics.py`；`test_pty_kernel_semantics` 應 PASS（事實測試，不依賴實作；若 FAIL 代表核心語意與本計畫假設不同，停止並回報），其餘端點測試為契約語意紅燈。不可用 `-k` 從整個工具測試目錄選取，以免誤收後續 Task 的測試。
- [ ] **T3-3：commit 測試**：`git commit -m "test(bridge): 新增 PTY 核心語意事實測試與端點 adapter 失敗測試"`。

### I 步驟

- [ ] **I3-1：實作** `base.py`、`posix_pty.py`、`win_serial.py`、`__init__.py`，照 Interfaces 與上方行為。`posix_pty.py` 不得以 `#ifdef` 式平台判斷散布在其他模組；`open_endpoint` 是唯一選擇點。peer 狀態以 `poll` 判定，不輪詢 `/proc`。
- [ ] **I3-2：執行並確認 PASS**：僅執行 Task 3 三個指定測試檔：`python3 -m pytest -q tools/uart_stdio_bridge/tests/test_posix_pty_endpoint.py tools/uart_stdio_bridge/tests/test_win_serial_endpoint.py tools/uart_stdio_bridge/tests/test_pty_kernel_semantics.py` 全綠；`windows_com` 測試顯示為 skipped 且訊息含「證據不足，非 PASS」。在 Windows host 經 interop 跑一次 `py -3.12 -m pytest -q tools\uart_stdio_bridge\tests\test_win_serial_endpoint.py`（fake 部分）並保存輸出於 `~/b-log/rust-refactor/p2/win-endpoint-l1.txt`。`windows_com` 原生 gate 未執行或 skip 時不得宣稱為 PASS。
- [ ] **I3-3：commit**：`git commit -m "feat(bridge): 實作 POSIX PTY 與 Windows pySerial 端點 adapter（只持有對端 B）"`＋fragment。

---
## Task 4：DUT 行為模型抽離與 stdin/stdout DUT 模擬器

**Test author model:** haiku-xhigh　**Implementer model:** agy-gemini-3.8-flash-high
**Changelog fragment:** `changelog.d/rust-p2-dut-model.md`（`type: feat`、`scope: test-infra`）
**BLOCKED-OWNER evidence:** 無。

**Files:**
- Create: `tools/uart_stdio_bridge/src/uart_stdio_bridge/dut/{__init__.py,__main__.py,model.py,plan.py,stdio_adapter.py,control_file.py}`
- Modify: `func-test/lib/fake_target.py`（改為 `DutModel`＋POSIX PTY adapter；公開 API 不變）
- Create: `func-test/lib/target_config.py`（import-safe re-export，Windows runner 可用）
- Create: `tools/uart_stdio_bridge/tests/fixtures/faketarget_golden/{default.json,login_brcm.json,overrides.json,pause.json,noise.json}`、`tools/uart_stdio_bridge/tests/fixtures/capture_faketarget_golden.py`
- Create: `tools/uart_stdio_bridge/tests/test_dut_model.py`、`tools/uart_stdio_bridge/tests/test_dut_stdio.py`、`tools/uart_stdio_bridge/tests/test_dut_control_file.py`、`tools/uart_stdio_bridge/tests/test_faketarget_equivalence.py`

**Interfaces:**

```python
# dut/model.py（純邏輯、sans-IO、時間由呼叫端注入；不得 import os/pty/termios/select/socket）
@dataclass(frozen=True)
class CommandOverride:
    response: str = ""
    delay_ms: int = 0
    interactive: bool = False
@dataclass(frozen=True)
class LoginStep:
    expect: str
    respond: str
@dataclass(frozen=True)
class TargetConfig:
    platform: str = "prpl"
    boot_banner: str = "boot done\r\nroot@prplOS:/# "
    noise_enabled: bool = True
    noise_interval_ms: int = 50
    noise_pattern: str = "KDBG:tick:{tick}\r\n"
    default_response: str = "EXEC:{cmd}\r\nRESULT:{cmd}:OK\r\nroot@prplOS:/# "
    overrides: Mapping[str, CommandOverride] = field(default_factory=dict)
    login_steps: tuple[LoginStep, ...] = ()
    @classmethod
    def from_yaml(cls, data: dict[str, Any] | None) -> "TargetConfig"   # 與現行 func-test/lib/fake_target.py:52-77 逐 key 同語意
    def to_json(self) -> dict[str, Any]
    @classmethod
    def from_json(cls, data: dict[str, Any]) -> "TargetConfig"
@dataclass(frozen=True)
class Emit:
    at_ms: int
    data: bytes
    kind: str          # "banner" | "response" | "prompt" | "login" | "noise" | "emit" | "reboot" | "flood"
class DutModel:
    def __init__(self, config: TargetConfig, plan: "ScenarioPlan | None" = None) -> None
    generation: int                                   # 從 1 起；logical_reboot 後 +1
    stdin_paused: bool
    def boot(self, now_ms: int) -> list[Emit]
    def feed(self, data: bytes, now_ms: int) -> list[Emit]       # 回傳「此刻即可輸出」者；延後者由 poll 取
    def poll(self, now_ms: int) -> list[Emit]                     # 到期的延遲輸出、noise、flood 分段
    def next_deadline_ms(self) -> int | None
    def pause_responding(self, now_ms: int, duration_ms: int) -> None
    def logical_reboot(self, now_ms: int, *, banner_delay_ms: int, pre_text: str = "") -> list[Emit]
    def emit_raw(self, data: bytes, now_ms: int) -> list[Emit]
    def apply_control(self, msg: dict[str, Any], now_ms: int) -> tuple[list[Emit], dict[str, Any]]   # 回 (emits, ack)

# dut/plan.py
@dataclass(frozen=True)
class Trigger:
    id: str
    line_contains: str
    action: dict[str, Any]     # kind ∈ {"delay_reply","half_prompt","silent","stop_stdin","logical_reboot","flood"}
    once: bool = True
@dataclass(frozen=True)
class ScenarioPlan:
    seed: int
    triggers: tuple[Trigger, ...]
    @classmethod
    def from_json(cls, data: dict[str, Any]) -> "ScenarioPlan"    # schema_version 必須為 "dut-plan.v1"

# dut/control_file.py（DUT 控制通道：檔案目錄，Docker 以 bind mount 共用，不需網路）
class ControlFileChannel:                     # DUT 端
    def __init__(self, control_dir: pathlib.Path) -> None
    def poll(self) -> list[dict[str, Any]]   # 讀 control.ndjson 自上次 offset 起的完整新行
    def ack(self, ack: dict[str, Any]) -> None   # append 到 ack.ndjson，flush＋fsync
class DutControlClient:                       # bridge 端
    def __init__(self, control_dir: pathlib.Path) -> None
    def send(self, msg: dict[str, Any], *, timeout_s: float = 5.0) -> dict[str, Any]
        # 自動補 "id"（遞增 "dc-<n>"）；輪詢 ack.ndjson 直到同 id；逾時 raise TimeoutError

# dut/stdio_adapter.py
def run_stdio(model: DutModel, *, stdin_fd: int = 0, stdout_fd: int = 1, stderr_fd: int = 2,
              control: ControlFileChannel | None = None,
              clock_ms: Callable[[], int] = lambda: time.monotonic_ns() // 1_000_000,
              read_chunk: int = 4096) -> int
def run_echo(*, stdin_fd: int = 0, stdout_fd: int = 1, stderr_fd: int = 2, stderr_flood_bytes: int = 0,
             exit_after_bytes: int | None = None, exit_code: int = 0, close_stdout_after_bytes: int | None = None,
             read_delay_ms: int = 0, write_chunk: int = 4096) -> int        # 自測用純 binary echo

# dut/__main__.py：CLI
#   python -m uart_stdio_bridge.dut --target-json P [--plan-json P] [--control-dir D] [--seed N]
#   python -m uart_stdio_bridge.dut --mode echo [--stderr-flood-bytes N] [--exit-after-bytes N --exit-code C]
#                                     [--close-stdout-after-bytes N] [--read-delay-ms N]
#   stdout 只輸出 DUT bytes；診斷一律 JSON lines 到 stderr；stdin EOF → exit 0；設定錯誤 → exit 2
#   Windows：啟動即 msvcrt.setmode(0/1/2, os.O_BINARY)
```

**`DutModel` 必須逐點重現的 `FakeTarget` 行為**（來源：`func-test/lib/fake_target.py:156-247`，盤點 08 §2.6）：

1. 輸入累積到 buffer，以 `b"\n"` 切行；每行 `line.replace(b"\r", b"").decode("utf-8", errors="replace").strip()`。
2. 有 `login_steps` 時先走登入：`step.expect in cmd` → 輸出 `step.respond`、前進一步、全部完成後離開登入階段；不符時：第 0 步重送 `"\r\n" + boot_banner.split("\r\n")[-1]`（banner 為空時 `"\r\n" + "Login: "`），其餘步重送 `"\r\n" + 前一步 respond 的最後一行`（最後一行為空則不送）。
3. 登入後空行 → 輸出 prompt：`_current_prompt` → 最後一個 login respond 的最後一行 → boot_banner 最後一行。
4. 非空命令：override 存在時先等 `delay_ms`；`interactive=True` 不輸出；否則輸出 override `response`；無 override 時輸出 `default_response.replace("{cmd}", cmd)`。輸出後若回應最後一行 `strip()` 非空，更新 `_current_prompt` 為該行。
5. 串行語意：`FakeTarget` 的命令執行緒以 `sleep` 實作延遲，因此**後續所有輸出（含空行 prompt、登入回覆）都排在前一個延遲之後**。模型以 `busy_until_ms` 表示：每個輸出的時間＝`max(now, busy_until)`（命令再加 `delay_ms`），並更新 `busy_until`。
6. `pause_responding`：只影響「命令」（第 4 點），起始時間取 `max(now, busy_until, min(pause_until, now + 30000))`（原實作 `self._pause.wait(timeout=30.0)`）；noise 在 pause 期間不輸出且 tick 不前進。
7. noise（`fake_target.py:151-159` 實際語意）：`noise_enabled` 時 tick 1 於 `boot(now)` 當下（緊接 banner 之後）輸出，之後每 `noise_interval_ms` 輸出一筆 `noise_pattern.replace("{tick}", str(tick))`，tick 每次輸出 +1；pause 期間不輸出、tick 不前進；pause 結束當下**立即**輸出下一筆（不等下一個 interval 邊界），並以該時點重新起算 interval。

情境觸發（`ScenarioPlan`，供 §15.7.1）：某行 `cmd` 包含 `line_contains` 時套用 `action`：

| `kind` | 參數 | 行為 |
|---|---|---|
| `delay_reply` | `delay_ms` | 該命令的回應額外延後 `delay_ms`（疊加 override delay；照第 5 點串行） |
| `half_prompt` | `split_at`、`gap_ms`、`inject_b64` | 依序輸出：回應主體（去掉最後一行 prompt）、`inject` bytes、`prompt[:split_at]`；`gap_ms` 後輸出 `prompt[split_at:]` |
| `silent` | — | 讀入命令但不輸出任何 bytes |
| `stop_stdin` | `resume_after_ms`（可為 null） | `stdin_paused=True`；adapter 停止讀 stdin（pipe 自然背壓）；到期或收到 `stdin_resume` 控制才恢復 |
| `logical_reboot` | `banner_delay_ms`、`pre_text` | 立即輸出 `pre_text`，`generation += 1`，清除登入進度與 `_current_prompt`，`banner_delay_ms` 後輸出 `boot_banner` |
| `flood` | `bytes`、`line` | 以 `line.replace("{n}", str(n))` 重複產生，總長恰為 `bytes`（最後一行截斷），由 `poll` 每次最多 65536 bytes 分段交出 |

控制訊息（`apply_control`）：`{"action":"pause_responding","duration_ms"}`、`{"action":"emit","payload_b64"}`、`{"action":"stdin_pause"}`、`{"action":"stdin_resume"}`、`{"action":"logical_reboot","banner_delay_ms","pre_text"}`、`{"action":"set_trigger","trigger_id","enabled"}`。ack：`{"id","ok":true,"generation":int,"t_ms":int}`；未知 action → `{"id","ok":false,"error":"unknown_action"}`。

### T 步驟

- [ ] **T4-1：擷取 golden 並撰寫失敗測試**。
  1. 先寫 `capture_faketarget_golden.py`：以 commit `93553e6` 的 `func-test/lib/fake_target.py`（`git show 93553e6:func-test/lib/fake_target.py` 寫成暫存模組載入，**不得**用 `HEAD`，因 I4-3 之後 HEAD 已是新版）建 `FakeTarget`。FakeTarget 自身在 **master** 端讀命令、寫回應（`fake_target.py:150`、`:178`），所以擷取程式扮演 daemon：開 `slave_path`（`O_RDWR|O_NOCTTY|O_NONBLOCK`）並只從 slave 寫入命令、讀取回應；`start()` 後先讀出 boot banner（不存入 steps），再依序寫入腳本輸入，每步以 `select` 有界讀取 `wait_ms`（預設 500 ms；delay 案例為 delay+500 ms；`pause.json` 的每步為「pause 剩餘時間＋500 ms」，使回應遠離窗口邊界），整體 timeout 30 s 後中止並報錯；收集該步 slave 讀到的 bytes，輸出 `{"source_blob":"93553e6:func-test/lib/fake_target.py","config": TargetConfig 的 YAML 形式, "steps":[{"input_b64","output_b64","wait_ms"}]}`。擷取後斷言每個非空命令步驟的 `output_b64` 非空（確認 golden 真的含命令回應）。五個 golden：`default.json`（noise 關；`ls\n`、`\n`、`echo a b\r\n`、`\xff\xfe\n`（非 UTF-8）、`pwd\n`）、`login_brcm.json`（`brcm-01-login-flow.yaml` 的 `target`；輸入 `bogus\n`、`admin\n`、`wrong\n`、`admin\n`、`sh\n`、`\n`）、`overrides.json`（override `slow:{response:"S\r\n# ",delay_ms:700}`、`hang:{interactive:true}`；輸入 `slow\n`、`ls\n`、`hang\n`、`\n`）、`pause.json`（`pause_responding(1.0)` 後輸入 `ls\n`、`\n`）、`noise.json`（noise 開、`interval_ms:100`；`pause_responding(0.35)` 後等 1.0 s 再輸入 `ls\n`；另記錄每筆 noise 的相對到達時間供時間容差比對）。執行一次並 commit 產出檔（golden 屬測試資料，之後不得重產以遷就實作）。
  2. `test_dut_model.py` 關鍵測試：

```python
def _replay(model: DutModel, golden: dict) -> bytes:
    now = 0
    out = b"".join(e.data for e in model.boot(now))
    for step in golden["steps"]:
        out += b"".join(e.data for e in model.feed(base64.b64decode(step["input_b64"]), now))
        end = now + step["wait_ms"]
        while (d := model.next_deadline_ms()) is not None and d <= end:
            out += b"".join(e.data for e in model.poll(d))
        now = end
    return out

@pytest.mark.parametrize("name", ["default", "login_brcm", "overrides"])
def test_model_matches_faketarget_golden(name):
    g = json.loads((GOLDEN / f"{name}.json").read_text(encoding="utf-8"))
    cfg = TargetConfig.from_yaml(g["config"])
    expected = b"".join(base64.b64decode(s["output_b64"]) for s in g["steps"])
    banner = cfg.boot_banner.encode("utf-8")
    assert _replay(DutModel(cfg), g) == banner + expected
```

其餘：

| 測試名 | 斷言 |
|---|---|
| `test_from_yaml_same_semantics_as_legacy` | 對 13 個 `func-test/cases/*.yaml` 的 `target` 區塊，新版 `TargetConfig.from_yaml(...)` 與舊版的結果經正規化後相等：兩者皆轉成 `{"platform",...,"overrides":{k: dataclasses.asdict(v)},"login_steps":[dataclasses.asdict(s)...]}`（`tuple`／`Mapping` 與舊版 `list`／`dict` 不直接 `==`）。舊版固定以 `importlib` 從 `git show 93553e6:func-test/lib/fake_target.py` 寫出的暫存副本載入（不得用 `HEAD`，否則 I4-3 之後成同義反覆） |
| `test_json_roundtrip` | `TargetConfig.from_json(cfg.to_json()) == cfg` |
| `test_delay_serializes_following_outputs` | override `slow` delay 700 ms；t=0 feed `slow\n\n` → feed 回傳空；`poll(699)` 空；`poll(700)` 依序得 slow 回應再得 prompt |
| `test_pause_delays_commands_not_login` | 有 login_steps 時 pause 期間 login 回覆照常；登入完成後 pause 中的命令回應時間＝pause 結束時間 |
| `test_pause_capped_at_30s` | `pause_responding(0, 60000)` 後 t=0 送命令，回應 `at_ms == 30000` |
| `test_noise_ticks_and_pause` | interval 50：`boot(0)` 輸出 banner 後緊接 `KDBG:tick:1\r\n`（`at_ms==0`）；`poll(50)` 得 `tick:2`；t=60 `pause_responding(60, 100)`，`poll(100)`～`poll(159)` 無 noise 且 tick 不前進；`poll(160)` 立即得 `tick:3`（pause 結束當下，不等 interval 邊界）；下一筆於 210 |
| `test_model_matches_noise_golden` | `noise.json` 重放：noise 行序列（tick 編號）完全相等；每筆 noise 時間與 golden 相對時間差 ≤ 60 ms；命令回應 bytes 完全相等 |
| `test_half_prompt_trigger` | trigger `L15_HALF` split_at=5 gap=800 inject=`\x1b[32m`：feed 後立即輸出不含 `prompt[5:]`；`poll(800)` 才輸出 `prompt[5:]`；合併輸出等於 `body + inject + prompt` |
| `test_silent_trigger` | `L15_SILENT` 命令 feed 與之後 10 s 內 poll 皆無輸出；下一個命令正常回應 |
| `test_logical_reboot_generation` | trigger `reboot` → 立即輸出 `pre_text`、`generation==2`；`banner_delay_ms` 後輸出 banner；之後空行回 banner 最後一行 prompt（`_current_prompt` 已清除） |
| `test_flood_exact_length_bounded_chunks` | `flood bytes=200000` → 所有 poll 合計恰 200000 bytes，單次 ≤65536 |
| `test_stop_stdin_and_resume` | `stop_stdin resume_after_ms=1000` 後 `stdin_paused is True`；`poll(1000)` 後 False |
| `test_control_ack_shapes` | `apply_control({"id":"a","action":"emit","payload_b64":"AP8="})` 回 emits `[b"\x00\xff"]` 與 ack `{"id":"a","ok":True,"generation":1,...}`；未知 action ack `ok False, error "unknown_action"` |
| `test_model_has_no_io_imports` | AST 掃 `dut/model.py`、`dut/plan.py`：無 `os`、`pty`、`termios`、`select`、`socket`、`subprocess` import |
| `test_dut_package_self_contained` | AST 掃 `dut/*.py`：只 import stdlib 與 `uart_stdio_bridge.dut.*`（Docker image 只複製 `dut/` 與套件 `__init__.py`，Task 6） |

`test_dut_stdio.py`（以 subprocess 執行 DUT CLI）：

| 測試名 | 斷言 |
|---|---|
| `test_stdout_only_dut_bytes` | default config：stdout 前綴恰為 banner bytes；stderr 每行可 `json.loads` |
| `test_binary_stdin_not_translated` | `--mode echo`：寫 `bytes(range(256))*64` 關 stdin，stdout 完全相等、exit 0 |
| `test_echo_stderr_flood_does_not_block_stdout` | `--mode echo --stderr-flood-bytes 20000000`，父程序以獨立 thread 排空 stderr；1 MiB 資料 echo 完整，總時間 < 30 s |
| `test_echo_exit_after_bytes` | `--exit-after-bytes 1000 --exit-code 7`：寫 4096 bytes，stdout 恰 1000 bytes、returncode 7 |
| `test_echo_close_stdout_after_bytes` | `--close-stdout-after-bytes 10`：stdout 讀到 10 bytes 後 EOF，程序仍存活直到 stdin 關閉 |
| `test_bad_config_exit2` | `--target-json` 指向非 JSON → exit 2，stdout 為空 |
| `test_stdin_eof_exit0` | default config 關 stdin → exit 0 |

`test_dut_control_file.py`：`DutControlClient.send` 與 DUT 程序（`--control-dir`）往返 `emit`、`pause_responding`、`logical_reboot`，ack `generation` 正確；DUT 未啟動時 `send` 在 timeout 後 raise `TimeoutError`；`ControlFileChannel.poll` 對尚未寫完的半行不回傳。

`test_faketarget_equivalence.py`（`posix_pty`）含三個測試：`test_faketarget_uses_model`（`FakeTarget(cfg).model` 為 `DutModel` 實例）、`test_faketarget_replays_golden`：以**改寫後**的 `func-test/lib/fake_target.FakeTarget` 對同四個無 noise golden 經真實 PTY 重放（測試持有 slave、自 slave 寫命令與讀回應，與擷取程式同一側），slave 讀到的 bytes 等於 golden；`FakeTarget` 仍有 `master_fd`、`slave_fd`、`slave_path`、`start()`、`stop()`、`pause_responding(duration_s)`、`config` 屬性；以及 `test_target_config_import_safe`：子程序 `python -c "import sys; sys.modules['pty']=None; sys.modules['termios']=None; import target_config"`（`sys.path` 指向 `func-test/lib`）exit 0（`target_config` 不得拉入 `pty`／`termios`，供 Windows runner 使用）。

- [ ] **T4-2：執行並確認 FAIL**：`python3 -m pytest -q tools/uart_stdio_bridge/tests -k "dut or faketarget"` → `ModuleNotFoundError: uart_stdio_bridge.dut`；`test_faketarget_replays_golden` 對舊 FakeTarget 應 PASS（證明 golden 正確），`test_faketarget_uses_model` 應 FAIL。
- [ ] **T4-3：commit 測試與 golden**：`git commit -m "test(dut): 擷取 FakeTarget golden 並新增 DUT 行為模型與 stdio 模擬器失敗測試"`。

### I 步驟

- [ ] **I4-1：實作**：
  1. `model.py`／`plan.py` 照 Interfaces 與上列七點；內部以 `(at_ms, seq, Emit)` 的 heap 管理延後輸出，`next_deadline_ms` 取 heap 頂與下一個 noise／flood 時點的最小值。
  2. `stdio_adapter.py`：一條 stdin reader thread（`stdin_paused` 時不讀）把 bytes 送進 `queue.Queue(maxsize=64)`；主迴圈以 `next_deadline_ms` 計算等待時間，`os.write` 迴圈寫完整 emit；`control` 每 50 ms `poll` 一次並 `ack`。stdin EOF 後輸出已到期的 emit 即 exit 0。
  3. 新增 `func-test/lib/target_config.py`（import-safe：只 import stdlib 與 `uart_stdio_bridge.dut.model`，`sys.path` 以 `ROOT_DIR / "tools" / "uart_stdio_bridge" / "src"` 插入一次），re-export `TargetConfig`／`CommandOverride`／`LoginStep`。`func-test/lib/fake_target.py`：保留類別名稱、建構子、公開屬性與方法；內部改為「PTY I/O thread＋`DutModel`」，`pause_responding(duration_s)` 轉呼叫 `model.pause_responding(now_ms, int(duration_s*1000))`，新增唯讀屬性 `model`；`TargetConfig` 等名稱自 `target_config` re-export（既有 `from lib.fake_target import TargetConfig` 寫法仍可用，`tools/docker/*` 不需同步修改即可運作；Task 10 再改為 import `target_config`）。
- [ ] **I4-2：執行並確認 PASS**：`python3 -m pytest -q tools/uart_stdio_bridge/tests -k "dut or faketarget"` 全綠；`python3 func-test/runner.py` 的結果與實作前相同：T4-1 時先以**現行** runner（尚無 `--mode`、無 JSON 報告）執行一次，把純文字輸出存為 `~/b-log/rust-refactor/p2/functest-pty-before.txt`；此處再跑一次存 `functest-pty-after.txt`，逐 case 比對「✅ PASS／❌ FAIL＋失敗步驟」不變（預期基線為 17 次執行（13 個 case，`rc-01` repeat 5）中 13 pass／4 fail：`cowork-01`、`hk-01`、`hk-02`、`hk-04`；`hk-03` 因 `input_raw` 空轉而假性 PASS；oracle 修復在 Task 10）。
- [ ] **I4-3：commit**：`git commit -m "feat(dut): 自 FakeTarget 抽離 DUT 行為模型並新增 stdin/stdout DUT 模擬器"`＋fragment。

---

## Task 5：bridge 核心（方向 worker、背壓、stderr 排空、控制通道、故障動作）

**Test author model:** agy-gemini-3.8-flash-high　**Implementer model:** codex-gpt-6-luna-max
**Changelog fragment:** `changelog.d/rust-p2-bridge-core.md`（`type: feat`、`scope: test-infra`）
**BLOCKED-OWNER evidence:** 無（OD-15 起改為可執行 gate）。
**擁有者 host 可執行 gate（OD-15，G2）：** Windows 上 `run --endpoint win-serial --com COM21` 對 com0com pair 的端到端證據（OD-08）：bridge 開 COM21、受測端（pySerial 測試程式或 OD-11 隔離實例 daemon＋allowlist `COM20`）開 COM20，經 interop 原生執行，報告附 Windows build／HVCI／pair 對應。`local-process` DUT＋fake endpoint 的單元測試仍不得計為原生 serial gate。

**Files:**
- Create: `tools/uart_stdio_bridge/src/uart_stdio_bridge/bridge/{__init__.py,bytequeue.py,dut_process.py,pumps.py,control.py,core.py}`
- Modify: `tools/uart_stdio_bridge/src/uart_stdio_bridge/cli.py`（`run` 子命令）
- Create: `tools/uart_stdio_bridge/tests/helpers.py`（`bridge_proc` fixture、`FakeEndpoint`、`StubDutProcess`（`stdin_write_some` 依腳本回傳接受長度）、`read_exact`、`write_all`）、`tools/uart_stdio_bridge/tests/test_bytequeue.py`、`test_dut_process.py`、`test_control_channel.py`、`test_bridge_core.py`、`test_bridge_faults.py`

**Interfaces:**

Consumes：Task 2 `EvidenceWriter`、`make_*`；Task 3 `Endpoint`、`open_endpoint`、`EndpointWriteUncertain`；Task 4 `DutControlClient`、DUT CLI。

```python
# bridge/bytequeue.py
class ByteQueue:
    def __init__(self, max_bytes: int) -> None
    def put(self, data: bytes, *, stop: threading.Event, timeout_s: float | None = None) -> bool
        # 空間不足時阻塞（背壓，不丟資料）；佇列為空時允許單一 chunk 超過 max_bytes；stop 設定或 close 後回 False
    def get(self, timeout_s: float) -> bytes | None
    def pending_bytes(self) -> int
    def discard_all(self) -> int          # 只供 dut_restart(pending="discard")；回傳丟棄 bytes
    def close(self) -> None

# bridge/dut_process.py
class DutStdinClosed(OSError): ...
class DutProcess:
    def __init__(self, argv_factory: Callable[[int], list[str]], *, mode: str,
                 env: dict[str, str] | None = None, cwd: str | None = None) -> None
        # argv_factory(generation) → argv；mode ∈ {"local-process","docker"}
    generation: int                       # spawn 成功後為 1，每次 respawn +1
    def spawn(self) -> None               # Popen(stdin/stdout/stderr=PIPE, bufsize=0, close_fds=True)；Windows 加 CREATE_NEW_PROCESS_GROUP
    def stdin_write_some(self, data: bytes) -> int      # os.write；EPIPE/BrokenPipe → raise DutStdinClosed
    def stdout_read(self, max_bytes: int) -> bytes      # 阻塞 os.read；b"" = EOF
    def stderr_read(self, max_bytes: int) -> bytes
    def close_stdin(self) -> None
    def terminate(self, grace_s: float = 2.0) -> int | None    # SIGTERM/CTRL_BREAK → 等 grace → kill；回 returncode
    def kill(self) -> int | None
    def poll(self) -> int | None
    def respawn(self, grace_s: float = 2.0) -> int             # terminate 後以 generation+1 spawn；回新 generation
    def describe(self) -> dict[str, Any]                       # {"mode","argv","pid","generation"}

# bridge/control.py
class ControlError(RuntimeError):
    code: str
class ControlServer:
    def __init__(self, handler: Callable[[str, dict[str, Any], str], dict[str, Any]], *, token: str) -> None
    def start(self) -> tuple[str, int]       # 綁 127.0.0.1:0；回 (host, port)
    def stop(self) -> None
def write_control_info(path: pathlib.Path, *, host: str, port: int, token: str) -> None   # {"endpoint":"tcp://H:P","token":T}；POSIX chmod 0600
class ControlClient:
    def __init__(self, info_path: pathlib.Path, *, timeout_s: float = 30.0) -> None
    def call(self, action: str, params: dict[str, Any] | None = None) -> dict[str, Any]   # ok:false → raise ControlError(error)
    def close(self) -> None
# 協定：每行一個 JSON。請求 {"action","action_id","params","token"}；回應 {"action_id","ok":true,"result":{...}}
#        或 {"action_id","ok":false,"error":"HARNESS_..."}；token 錯誤 → error HARNESS_CONTROL_PROTOCOL_ERROR 並關閉連線

# bridge/core.py
@dataclass(frozen=True)
class BridgeConfig:
    run_id: str
    case_id: str
    port_id: str
    evidence_dir: pathlib.Path
    read_chunk: int = 4096
    max_inflight_bytes: int = 1_048_576       # 每方向
    write_timeout_s: float = 5.0
    barrier_quiet_ms: int = 500
    stderr_cap_bytes: int = 268_435_456       # dut.stderr.log 上限；超過後繼續排空但不寫檔，manifest 標 dut_stderr_truncated
    allow_dut_exit: bool = False
class Bridge:
    def __init__(self, cfg: BridgeConfig, endpoint: Endpoint, dut: DutProcess, writer: EvidenceWriter,
                 dut_control: DutControlClient | None) -> None
    def start(self) -> None
    def handle_action(self, action: str, params: dict[str, Any], action_id: str) -> dict[str, Any]
    def wait(self) -> int                     # 阻塞到 shutdown 或 fail-stop；回 exit code（0 正常、3 HARNESS 失敗）
    @property
    def failure(self) -> str | None
```

**執行緒與資料流（每條都是獨立 thread；任何一條不得被另一條阻塞）**

| thread | 動作 | 證據 |
|---|---|---|
| `tx-reader` | `endpoint.read(read_chunk, 0.05)` → `emit_data(observation="serial_read")` → `tx_q.put()`（滿則阻塞＝停止讀 serial，背壓到 daemon） | 先記證據再轉送 |
| `tx-writer` | `tx_q.get()` → 迴圈 `dut.stdin_write_some(rest)`，每次接受 `n` bytes 立即 `emit_data(observation="dut_stdin_write", payload=chunk[:n])` | 只記實際接受長度 |
| `rx-reader` | `dut.stdout_read(read_chunk)` → `emit_data(observation="dut_stdout_read")` → `rx_q.put()`；`b""` → META `dut_stdout_eof`，結束 | |
| `rx-writer` | `rx_q.get()` → 迴圈 `endpoint.write_some(rest, write_timeout_s)`；回 0 時重試（peer gate 或背壓）；每次 `n>0` 記 `serial_write`；`EndpointWriteUncertain` → META `write_uncertain`（`loss_flag:true`）並 fail-stop `HARNESS_WRITE_UNCERTAIN` | 不得把要求長度記成成功長度 |
| `stderr-drain` | `dut.stderr_read(65536)` → 寫 `dut.stderr.log`（binary，超過 cap 只計數不寫） | 不進 UART、不進 wire |
| `dut-watch` | 每 100 ms `dut.poll()`；非預期結束 → META `dut_exited{returncode}`；`allow_dut_exit=False` 時 fail-stop `HARNESS_DUT_EXITED` | |
| `peer-watch` | 每 50 ms 比較 `endpoint.peer_open()`，變化時 META `peer_state{open}` | POSIX 才有 |
| `control` | `ControlServer` 收請求 → `handle_action` | 每個 action 在 `control.ndjson` 記 `requested` 與 `completed`／`failed` 兩筆 |

fail-stop：`writer.failure` 非 None、`HARNESS_WRITE_UNCERTAIN`、`HARNESS_ENDPOINT_IO_ERROR`、非預期 DUT 結束 → 停止所有 pump、關閉 endpoint 與 DUT、`bridge-manifest.json` 記 `closed_cleanly:false`、`failure:<code>`，stderr 最後一行 `{"code":"...","event":"BRIDGE_FAILED"}`，exit 3。

**控制動作（`handle_action`）**

| action | params | result | 證據與世代 |
|---|---|---|---|
| `status` | — | `{"endpoint_generation","dut_generation","failure","peer_open","pending":{"rx","tx"},"pumps":{"rx","tx"},"wire_seq"}` | `peer_open` 於呼叫當下以 `endpoint.peer_open()` 即時計算（POSIX `poll` POLLHUP），不讀 peer-watch 快取；Windows 為 null |
| `barrier` | `mode`：`passive`（預設）／`quiesce`；`quiet_ms`(預設 cfg)、`timeout_ms`=5000 | `{"settled":bool,"mode","offsets":{四個},"endpoint_generation","dut_generation","wire_seq"}` | `passive`：兩方向佇列為空且 `quiet_ms` 內無任何 pump 活動才 `settled:true`。`quiesce`（供 DUT 持續輸出 noise 的案例）：(1) 暫停 `rx-reader`（只停讀 DUT stdout；DUT 寫入因 pipe 滿而背壓阻塞，bytes 留在 pipe、不丟、不進 wire），META `quiesce{phase:"begin"}`；(2) 等 `rx_q`／`tx_q` 排空、`rx-writer` 寫完；(3) 等 `quiet_ms` 內 `tx-reader`／`tx-writer`／`rx-writer` 皆無活動；(4) 取 offsets 後**維持 RX 暫停**回傳，由呼叫端在取得 WAL seq 後呼叫 `resume_pump{dir:"RX"}`（META `quiesce{phase:"end"}`）；逾時回 `settled:false` 並自動恢復 RX。兩種模式回傳前都 `writer.flush()` |
| `checkpoint` | — | `{"checkpoint_id","path","wire_len","wire_sha256","wire_seq","control_len","control_sha256"}` | 先 `writer.flush()`，再對 `wire.ndjson`／`control.ndjson` 目前位元組前綴計算長度與 sha256，原子寫 `checkpoints/<checkpoint_id>.json`（`checkpoint.v1`）並記 META `checkpoint`；供 bridge 存活期間的比較（Task 7）驗證「前綴未被竄改」，取代只在 shutdown 才有的 `bridge-manifest.closed_cleanly` |
| `mark` | `label`、`op_id` | `{"wire_seq"}` | META `mark` |
| `pause_pump`／`resume_pump` | `dir`: `TX`／`RX` | `{"dir","state"}` | `control.ndjson` |
| `by_id_link_remove`／`by_id_link_add` | — | `{}` | 端點不支援 → `HARNESS_CAPABILITY_UNAVAILABLE` |
| `transport_disconnect` | `remove_link`=true | `{"endpoint_generation"}` | 只在 `endpoint.capabilities()["transport_disconnect"]` 為 True 時可用 |
| `transport_reconnect` | — | `{"endpoint_generation"}` | META `endpoint_generation{from,to,reason:"reconnect"}` |
| `dut_restart` | `pending`: `discard`／`deliver`（預設 discard）、`grace_s`=2.0 | `{"dut_generation","discarded_len"}` | META `dut_generation{reason:"restart"}`；`discard` 時 META `fault{discarded_len}`（刻意丟棄，`loss_flag:false`） |
| `dut_kill` | — | `{"returncode"}` | 模擬「容器／Docker 連線故障」；META `fault{action:"dut_kill"}`，與 `dut_restart` 分開報告（§15.7.2） |
| `dut_spawn` | — | `{"dut_generation"}` | 只在 DUT 已結束時可用 |
| `dut_control` | `msg` | DUT ack | ack 的 `generation` 大於目前 `dut_generation` 時 META `dut_generation{reason:"logical_reboot"}` |
| `shutdown` | — | `{"evidence":{"records","bytes","sha256"}}` | META `bridge_stop`；寫最終 `bridge-manifest.json`；`wait()` 回 0 |

**`run` 子命令參數**

```text
uart-stdio-bridge run --run-id R --case-id C --port-id COM0 --evidence-dir DIR
  ( --endpoint posix-pty --by-id-link PATH [--no-peer-gate]
  | --endpoint win-serial --com COMb [--baud 115200] [--transport-mode virtual_com0com|physical_usb_uart] [--allow-peer-close] )
  --dut-mode local-process|docker ( --dut-argv-json JSON | --dut-image IMAGE )
  [--dut-target-json PATH] [--dut-plan-json PATH] [--dut-seed N]
  [--max-inflight-bytes 1048576] [--read-chunk 4096] [--write-timeout-s 5.0] [--allow-dut-exit]
```

**測試專用覆寫（只供工具自測；任何一個生效時 `bridge-manifest.test_overrides` 非空，`is_qualified` 與比較器一律不得給 PASS 資格）**：env `UART_STDIO_BRIDGE_TEST_STDERR_CAP=<bytes>`（覆寫 `stderr_cap_bytes`）、`UART_STDIO_BRIDGE_TEST_FAULT=drop_every_1000th|enospc_after_1mib`（Task 8 自測鉤子）。

證據目錄內容：`wire.ndjson`、`control.ndjson`、`dut.stderr.log`、`bridge-manifest.json`、`control.json`（控制端點與 token）、`dut-control/{control.ndjson,ack.ndjson}`、`dut-input/{target.json,plan.json}`。就緒後 bridge 自身 stdout 印一行 `{"control_info":"<abs path>","event":"BRIDGE_READY","port_id":...,"slave_path"|"com":...}`（bridge 自身 stdout 不是 DUT bytes）。

### T 步驟

- [ ] **T5-1：撰寫失敗測試**。`tests/helpers.py` 提供 `bridge_proc(tmp_path)` fixture：以 `[sys.executable, "-m", "uart_stdio_bridge", "run", ..., "--endpoint","posix-pty","--by-id-link", tmp/"by-id"/"fake-uart0", "--dut-mode","local-process","--dut-argv-json", json.dumps([sys.executable,"-m","uart_stdio_bridge.dut","--mode","echo", *extra])]` 啟動（`cwd` 與 `PYTHONPATH=src`），讀到 `BRIDGE_READY` 後回傳物件 `.link`、`.evidence`、`.control`（`ControlClient`）、`.wait(timeout)`；teardown 一律 `shutdown` 或 kill。關鍵測試：

```python
@pytest.mark.posix_pty
def test_roundtrip_full_byte_range_all_four_observations(bridge_proc):
    b = bridge_proc()
    fd = os.open(b.link, os.O_RDWR | os.O_NOCTTY | os.O_NONBLOCK)   # 模擬 daemon 開端 A
    try:
        data = bytes(range(256)) * 64
        write_all(fd, data)
        assert read_exact(fd, len(data), timeout_s=10) == data
        res = b.control.call("barrier", {"quiet_ms": 300})
        assert res["settled"] is True
        assert res["offsets"] == {"serial_read": len(data), "dut_stdin_write": len(data),
                                  "dut_stdout_read": len(data), "serial_write": len(data)}
    finally:
        os.close(fd)
    b.control.call("shutdown")
    assert b.wait(10) == 0
    recs = [json.loads(x) for x in (b.evidence / "wire.ndjson").read_text(encoding="utf-8").splitlines()]
    assert [r["seq"] for r in recs] == list(range(1, len(recs) + 1))
    for obs in ("serial_read", "dut_stdin_write", "dut_stdout_read", "serial_write"):
        assert b"".join(base64.b64decode(r["payload_b64"]) for r in recs if r["observation"] == obs) == data, obs
    man = json.loads((b.evidence / "bridge-manifest.json").read_text(encoding="utf-8"))
    assert man["closed_cleanly"] is True and man["failure"] is None
    assert man["evidence"]["wire"]["sha256"] == hashlib.sha256((b.evidence / "wire.ndjson").read_bytes()).hexdigest()
```

其餘：

| 測試名（檔案） | 斷言 |
|---|---|
| `test_bytequeue_backpressure_blocks_not_drops`（bytequeue） | `max_bytes=10`：put 8 B 後第二個 put 8 B 在另一 thread 阻塞；get 後才完成；`pending_bytes` 從未 >18；總取出等於總放入 |
| `test_bytequeue_oversize_chunk_when_empty` | 空佇列 put 20 B 成功 |
| `test_bytequeue_stop_unblocks` | 阻塞中的 put 在 `stop.set()` 後 ≤0.2 s 回 False |
| `test_dut_process_binary_pipes`（dut_process） | echo DUT 寫入 `b"\x00\r\n\xff"`，`stdout_read` 得完全相同 bytes |
| `test_dut_process_respawn_generation` | `spawn()` 後 generation 1；`respawn()` 回 2，新 pid ≠ 舊 pid |
| `test_dut_stdin_closed_raises` | DUT 結束後 `stdin_write_some` raise `DutStdinClosed` |
| `test_control_bad_token`（control） | 錯 token 的請求得 `{"ok":false,"error":"HARNESS_CONTROL_PROTOCOL_ERROR"}` 且連線被關 |
| `test_control_info_file_mode` | POSIX 上 `control.json` 權限為 0o600；內容 endpoint 以 `tcp://127.0.0.1:` 開頭 |
| `test_control_log_requested_and_completed` | 呼叫 `mark` 後 `control.ndjson` 有同 `action_id` 的 `requested` 與 `completed` 兩筆，`wire_seq` 單調 |
| `test_partial_write_to_dut_records_accepted_only`（core，in-process `Bridge`＋可腳本化的 `StubDutProcess`） | 阻塞 pipe 上 `os.write` 不會短寫、非阻塞 pipe 上 ≤`PIPE_BUF` 的寫入是原子的（EAGAIN 而非短寫），真 pipe 無法穩定製造部分寫入（§15.7.1：無法穩定製造者交給 fault test）。因此以 `StubDutProcess.stdin_write_some` 依腳本回傳 `[3, 4096, 1, 0→重試, 7, ...]` 的接受長度：wire 每筆 `dut_stdin_write.len` 等於腳本回傳值、串接後等於 `serial_read` 串流、無遺失無重複 |
| `test_dut_pipe_accepted_total_matches_received` | 真 echo DUT `--read-delay-ms 50`；peer 一次寫 2 MiB：`dut_stdin_write` 各筆 `len` 總和＝DUT 實收（echo 回來的總長）＝`serial_read` 總長；不要求出現短寫 |
| `test_barrier_settles_under_continuous_noise` | model DUT 開 noise（`interval_ms:100`，同 `sm-01`）：`barrier{mode:"passive",quiet_ms:500,timeout_ms:2000}` 回 `settled:false`；`barrier{mode:"quiesce",quiet_ms:300}` 回 `settled:true`，回傳後 1 s 內 `serial_write` offset 不前進；`resume_pump{dir:"RX"}` 後 noise 恢復且 tick 連續（未遺失 bytes：DUT 端 tick 序列在 wire 連續） |
| `test_checkpoint_prefix_digest` | 往返 4 KiB 後 `checkpoint`：`wire_sha256 == sha256(wire.ndjson[:wire_len])`；之後再往返資料，舊 checkpoint 的前綴 digest 仍成立；`checkpoints/<id>.json` 存在 |
| `test_status_peer_open_live` | peer 關閉 fd 後立即呼叫 `status`（不等 peer-watch 50 ms 週期）得 `peer_open:false` |
| `test_backpressure_bounded_memory` | echo DUT `--read-delay-ms 200`、`--max-inflight-bytes 65536`；peer 持續寫 8 MiB 期間每 0.2 s 取 `status`，`pending.tx` 從未 >65536+4096；peer 的 `os.write` 出現 EAGAIN（背壓抵達受測端） |
| `test_stderr_flood_does_not_stall_data` | echo DUT `--stderr-flood-bytes 50000000`：1 MiB 往返完成 <30 s；`dut.stderr.log` 大小等於 50000000（未超 cap） |
| `test_stderr_cap_truncates_file_keeps_draining` | `stderr_cap_bytes` 由 env `UART_STDIO_BRIDGE_TEST_STDERR_CAP=1000` 縮小：檔案 1000 B、manifest `dut_stderr_truncated:true`、資料往返不受影響 |
| `test_dut_stdout_eof_meta` | echo DUT `--close-stdout-after-bytes 10`：wire 有 META `dut_stdout_eof`；TX 方向仍可轉送 |
| `test_unexpected_dut_exit_fail_stop` | echo DUT `--exit-after-bytes 5 --exit-code 7`：bridge exit 3；wire 有 META `dut_exited{returncode:7}`；manifest `failure=="HARNESS_DUT_EXITED"` |
| `test_allowed_dut_exit_no_fail` | 同上加 `--allow-dut-exit`：`dut_spawn` 後 `dut_generation==2`，往返恢復，最後 exit 0 |
| `test_peer_gate_no_write_before_open` | DUT 啟動即輸出 banner；peer 尚未開啟時 `serial_write` 串流長度為 0；peer 開啟後收到完整 banner；wire 有 META `peer_state{open:true}` |
| `test_write_uncertain_fail_stop`（faults，以 `FakeEndpoint` in-process 建 `Bridge`） | `FakeEndpoint.write_some` raise `EndpointWriteUncertain(10,"x")` → META `write_uncertain`（`loss_flag:true`、`accepted_len:null`、`requested_len:10`）、`failure=="HARNESS_WRITE_UNCERTAIN"` |
| `test_evidence_disk_full_fail_stop` | 注入 writer `os_write` ENOSPC → `Bridge.wait()==3`，`failure=="HARNESS_DISK_FULL"`，pump thread 全部結束 |
| `test_transport_disconnect_reconnect_generation` | `transport_disconnect` 後 peer 讀到 EIO；`transport_reconnect` 回 `endpoint_generation:2`；新世代 `serial_read` 的 `stream_offset` 從 0 起；wire 有 META `endpoint_generation{from:1,to:2}` |
| `test_dut_restart_discard_records_fault` | `pause_pump TX` 後 peer 寫 100 B，`dut_restart{pending:"discard"}` → `discarded_len==100`；META `fault` 的 `loss_flag is False`；`dut_generation==2` |
| `test_dut_kill_distinct_from_restart` | `dut_kill` 後 META `fault.action=="dut_kill"`，`dut_generation` 未變；`dut_spawn` 後才 +1 |
| `test_barrier_unsettled_while_traffic` | 持續雙向流量時 `barrier{quiet_ms:300,timeout_ms:1000}` 回 `settled:false` |
| `test_dut_control_logical_reboot_generation` | 以 model DUT（default config）＋`dut_control{msg:{action:"logical_reboot",banner_delay_ms:0}}` → META `dut_generation{reason:"logical_reboot"}`；之後 data 紀錄 `dut_generation==2` |
| `test_capability_unavailable_on_win_endpoint` | `FakeEndpoint(capabilities 全 False)`：`transport_disconnect` 回 `HARNESS_CAPABILITY_UNAVAILABLE` |

- [ ] **T5-2：執行並確認 FAIL**：`python3 -m pytest -q tools/uart_stdio_bridge/tests -k "bytequeue or dut_process or control or bridge"` → `ModuleNotFoundError: uart_stdio_bridge.bridge`。
- [ ] **T5-3：commit 測試**：`git commit -m "test(bridge): 新增 bridge 核心方向 worker、背壓與故障控制的失敗測試"`。

### I 步驟

- [ ] **I5-1：實作** `bytequeue.py`、`dut_process.py`、`control.py`、`pumps.py`（四個 pump 與 drain／watch 的 thread 函式，各自只透過 `ByteQueue`、`EvidenceWriter`、`threading.Event` 溝通）、`core.py`（`Bridge`：狀態以單一 `threading.Lock` 保護 generation 與 pump 狀態；`barrier` 以每個 pump 更新的 `last_activity_ns` 判定 quiet；`mode:"quiesce"` 依控制動作表四步實作，RX 暫停期間 `dut-watch` 與 `stderr-drain` 照常運作）、`cli.py run`（建立證據目錄、`EvidenceWriter.start()`、`open_endpoint`、`DutProcess.spawn()`、`ControlServer.start()`、`write_control_info`、印 `BRIDGE_READY`、`Bridge.wait()`）。`pause_pump` 只停「讀」那一側（TX：停 `tx-reader`；RX：停 `rx-reader`），寫入側照常清空佇列。
- [ ] **I5-2：執行並確認 PASS**：`python3 -m pytest -q tools/uart_stdio_bridge/tests -k "bytequeue or dut_process or control or bridge"` 與 Task 1～4、9 的工具測試全綠（連跑 3 次無 flaky；有不穩定即視為缺陷修正，不得加 retry）。
- [ ] **I5-3：commit**：`git commit -m "feat(bridge): 實作方向獨立 worker、有界背壓、stderr 排空與獨立故障控制通道"`＋fragment。

---

## Task 6：DUT 執行模式：Docker image（`-i` 無 `-t`）與 `local-process`

**Test author model:** haiku-xhigh　**Implementer model:** agy-gemini-3.8-flash-high
**Changelog fragment:** `changelog.d/rust-p2-dut-docker.md`（`type: feat`、`scope: test-infra`）
**BLOCKED-OWNER evidence:** 無（OD-15 起改為可執行 gate）。
**擁有者 host 可執行 gate（OD-15，G3）：** Windows 上 `local-process` 模式的端到端執行，以 `SW_TEST_COM_PAIR=COM20:COM21` 在擁有者 host 原生執行。`local-process` 只證明功能案例共用，不得宣告 Docker 拓樸通過（§15.7.2）；docker 兩容器拓樸另由 Task 17 交付。

**Files:**
- Create: `tools/uart_stdio_bridge/docker/Dockerfile.dut`、`tools/uart_stdio_bridge/scripts/build_dut_image.sh`、`tools/uart_stdio_bridge/src/uart_stdio_bridge/bridge/dut_modes.py`
- Create: `tools/uart_stdio_bridge/tests/test_dut_modes.py`、`tools/uart_stdio_bridge/tests/test_docker_dut.py`
- Modify: `tools/uart_stdio_bridge/src/uart_stdio_bridge/cli.py`（`--dut-mode docker` 走 `docker_argv`）

**Interfaces:**

```python
# bridge/dut_modes.py
DUT_IMAGE_REPO: str = "serialwrap-dut-sim"
def image_tag() -> str                                   # f"{DUT_IMAGE_REPO}:0.1.0-{tool_tree_sha256()[:12]}"
def docker_argv(image: str, *, run_id: str, generation: int, scenario_dir: pathlib.Path,
                control_dir: pathlib.Path, seed: int, has_plan: bool,
                user: str | None = None) -> list[str]
    # user 預設：POSIX f"{os.getuid()}:{os.getgid()}"（使容器可寫 host 建立的 bind-mount 控制目錄；image 的 USER 65534 只作非 POSIX host 的退路）
def local_process_argv(src_root: pathlib.Path, *, target_json: pathlib.Path, plan_json: pathlib.Path | None,
                       control_dir: pathlib.Path, seed: int) -> list[str]
def docker_image_id(image: str) -> str | None             # `docker image inspect --format {{.Id}}`；失敗回 None
```

`docker_argv` 固定輸出（順序即契約）：

```text
docker run --rm -i --network none --read-only --cap-drop ALL --security-opt no-new-privileges
  --pids-limit 64 --memory 256m --name sw-dut-<run_id>-g<generation> --user <uid>:<gid>
  --mount type=bind,src=<scenario_dir>,dst=/scenario,readonly
  --mount type=bind,src=<control_dir>,dst=/control
  <image> --target-json /scenario/target.json [--plan-json /scenario/plan.json] --control-dir /control --seed <seed>
```

`local_process_argv`：`[sys.executable, "-E", "-s", "-c", "import sys; sys.path.insert(0, sys.argv[1]); from uart_stdio_bridge.dut.__main__ import main; raise SystemExit(main(sys.argv[2:]))", str(src_root), "--target-json", str(target_json), *(["--plan-json", str(plan_json)] if plan_json else []), "--control-dir", str(control_dir), "--seed", str(seed)]`。

`Dockerfile.dut`：

```dockerfile
FROM python:3.12-slim@sha256:7a8b475003c4fe15a2cd4e55e5cfc2f3560bdc9333d624f24cdd6d4340fd7a17
ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1
COPY src/uart_stdio_bridge/__init__.py /usr/local/lib/python3.12/site-packages/uart_stdio_bridge/__init__.py
COPY src/uart_stdio_bridge/dut/ /usr/local/lib/python3.12/site-packages/uart_stdio_bridge/dut/
USER 65534:65534
ENTRYPOINT ["python3", "-I", "-m", "uart_stdio_bridge.dut"]
```

（只複製 DUT 所需模組；image 內沒有 bridge、比較器、產品程式碼，也沒有 pip 安裝步驟，測試途中不會升級相依。）

`build_dut_image.sh`：`set -euo pipefail`；cwd 切到 `tools/uart_stdio_bridge`；`docker build -f docker/Dockerfile.dut -t "$(python3 -c 'from uart_stdio_bridge.bridge.dut_modes import image_tag; print(image_tag())')" .`（`PYTHONPATH=src`）；印出 `{"image":...,"image_id":...}`。

### T 步驟

- [ ] **T6-1：撰寫失敗測試**：

| 測試名 | 斷言 |
|---|---|
| `test_docker_argv_exact` | `docker_argv("img:1", run_id="r", generation=1, scenario_dir=P("/s"), control_dir=P("/c"), seed=7, has_plan=True, user="1000:1000")` 等於上方固定序列；省略 `user` 時（POSIX）為 `f"{os.getuid()}:{os.getgid()}"` |
| `test_docker_argv_no_tty_no_privilege` | argv 不含 `-t`、`--tty`、`-it`、`-ti`、`--privileged`、`--device`、`--net=host`、`--network host`；任何元素不含 `docker.sock`、`/dev/tty` |
| `test_docker_argv_name_per_generation` | generation=2 時 `--name` 為 `sw-dut-r-g2` |
| `test_local_process_argv_isolated` | 第 1 個元素為 `sys.executable`，第 2、3 為 `-E`、`-s`；不含 `-i` 互動旗標；以此 argv 實際啟動後 stdout 前綴為 banner |
| `test_dockerfile_pinned_digest` | `Dockerfile.dut` 第一行 `re.fullmatch(r"FROM python:3\.12-slim@sha256:[0-9a-f]{64}", line)`；不含 `pip install`、`apt-get` |
| `test_dockerfile_copies_only_dut` | 所有 `COPY` 來源只在 `src/uart_stdio_bridge/__init__.py` 與 `src/uart_stdio_bridge/dut/` |
| `test_image_tag_tracks_tool_tree` | `image_tag()` 以 `serialwrap-dut-sim:0.1.0-` 開頭，後綴等於 `tool_tree_sha256()[:12]` |
| `test_docker_roundtrip`（`@pytest.mark.docker`；`shutil.which("docker")` 不存在或 `docker info` 失敗時 `pytest.skip("cap:docker 不可用（非 PASS）")`） | 先執行 `build_dut_image.sh`；以 bridge `--dut-mode docker` 跑 default config：peer 收到 banner；送 `ls\n` 得 `EXEC:ls\r\nRESULT:ls:OK\r\nroot@prplOS:/# `；`bridge-manifest.dut.image_id` 等於 `docker_image_id(tag)` |
| `test_docker_dut_control_roundtrip`（`docker`） | 以 bridge `--dut-mode docker` 跑 default config，`dut_control` 依序送 `emit`、`pause_responding`、`logical_reboot`：每個都得到 ack（容器可寫 `/control/ack.ndjson`，無 `PermissionError`），`logical_reboot` 後 META `dut_generation{reason:"logical_reboot"}` |
| `test_docker_restart_new_container` | 同上，`dut_restart` 後 `docker ps -a --filter name=sw-dut-<run>-g1` 為空（`--rm`），`-g2` 在執行中 |
| `test_docker_kill_reported_as_transport_fault` | `dut_kill` 後 META `fault.action=="dut_kill"`，manifest `dut.faults` 含 `{"kind":"docker_transport_fault"}`，與 `dut_restart` 的 `{"kind":"dut_reboot"}` 分開 |

- [ ] **T6-2：執行並確認 FAIL**：`python3 -m pytest -q tools/uart_stdio_bridge/tests/test_dut_modes.py tools/uart_stdio_bridge/tests/test_docker_dut.py` → `ModuleNotFoundError` 與 Dockerfile 不存在。
- [ ] **T6-3：commit 測試**：`git commit -m "test(dut): 新增 Docker（-i 無 -t）與 local-process 執行模式失敗測試"`。

### I 步驟

- [ ] **I6-1：實作**：`Dockerfile.dut` 第一行使用上方固定 digest（2026-10-15 自本機 `docker image inspect python:3.12-slim --format '{{index .RepoDigests 0}}'` 取得；該 image 為 Python 3.12.14、建立於 2026-08-25）；若 registry 已無此 digest，停止並回報，不得自行換 tag；實作 `dut_modes.py`、`build_dut_image.sh`、`cli.py` 的 docker 分支（`DutProcess(argv_factory=lambda g: docker_argv(..., generation=g, ...), mode="docker")`），`bridge-manifest.dut` 寫 `image`、`image_id`、`faults`。
- [ ] **I6-2：執行並確認 PASS**：`bash tools/uart_stdio_bridge/scripts/build_dut_image.sh && python3 -m pytest -q tools/uart_stdio_bridge/tests/test_dut_modes.py tools/uart_stdio_bridge/tests/test_docker_dut.py`（本機 Docker 28 可用，docker 測試必須實際執行，不得 skip）。
- [ ] **I6-3：commit**：`git commit -m "feat(dut): 新增固定 digest 的 DUT Docker image 與 local-process 模式"`＋fragment。

---
## Task 7：WAL↔wire 比較器、correlation 與竄改拒絕

**Test author model:** codex-gpt-6-luna-max　**Implementer model:** agy-gemini-3.8-flash-high
**Changelog fragment:** `changelog.d/rust-p2-evidence-comparator.md`（`type: feat`、`scope: test-infra`）
**BLOCKED-OWNER evidence:** 無（比較器為純離線工具；Windows 上的實際 WAL 樣本由 Task 10 的 Windows runner 執行時產生，屬 Task 10 的 BLOCKED 項）。

**Files:**
- Create: `tools/uart_stdio_bridge/src/uart_stdio_bridge/compare/{__init__.py,wal_reader.py,wire_reader.py,integrity.py,streams.py,rules.py,comparator.py,redact.py,mapping_rules.json}`
- Create: `tests/acceptance/comparison.schema.json`
- Modify: `tools/uart_stdio_bridge/src/uart_stdio_bridge/cli.py`（`compare`、`redact` 子命令）、`tests/test_acceptance_schemas.py`（T7-1 新增 `test_builder_wal_matches_contract`）
- Create: `tools/uart_stdio_bridge/tests/fixtures/compare/`（由測試以 builder 產生，不 check-in 大檔）、`tools/uart_stdio_bridge/tests/evidence_builder.py`、`test_compare_integrity.py`、`test_compare_streams.py`、`test_compare_tamper.py`、`test_compare_invariants.py`、`test_compare_cli.py`

**Interfaces:**

```python
# compare/wal_reader.py（獨立實作；只依 contracts/wal_record.schema.json 的欄位語意與盤點 04 §1.3，不 import 產品）
WAL_REQUIRED = ("cmd_id","com","crc32","dir","len","loss_flag","meta","mono_ts_ns","payload_b64","seq","source","wall_ts")
@dataclass(frozen=True)
class WalRecord:
    file: str; line_no: int; seq: int; com: str; dir: str; source: str; cmd_id: str | None
    payload: bytes; loss_flag: bool; meta: dict[str, Any]
def list_wal_files(wal_dir: pathlib.Path) -> list[pathlib.Path]
    # 歸檔 raw.wal.ndjson.<YYYYmmdd-HHMMSS>[.<N>]（DF-03 後綴）依（時間戳, N）排序，現行 raw.wal.ndjson 最後
def load_wal(wal_dir: pathlib.Path) -> tuple[list[WalRecord], list[dict[str, Any]]]
    # 回 (依檔案順序＋行序的紀錄, 完整性問題)；問題 dict：{"code","file","line_no","detail"}
    # 檢查：JSON 可解析、WAL_REQUIRED 全在、crc32 == crc32_hex(payload)、len == len(payload)、base64 strict 解碼

# compare/wire_reader.py
def load_wire(evidence_dir: pathlib.Path) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]
    # 檢查：每行 JSON、欄位與型別（同 bridge_record.schema.json，自行實作，不依賴 jsonschema）、
    #       seq 自 1 起嚴格 +1、crc32/len 與 payload 相符、每個 (port_id, endpoint_generation, observation) 的 stream_offset 連續、
    #       最後一行以 "\n" 結尾、loss_flag、bridge-manifest.json 存在且 evidence.wire.sha256 等於實際檔案 sha256
def load_correlation(evidence_dir: pathlib.Path) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]

# compare/rules.py（只允許事先核准的映射；未知規則名 raise ValueError，不提供 regex 正規化）
APPROVED_RULES: frozenset[str] = frozenset({"secret_tx_unlogged", "wal_com_alias"})
def load_rules(names: Sequence[str]) -> tuple[str, ...]

# compare/comparator.py
@dataclass(frozen=True)
class CompareInputs:
    evidence_dir: pathlib.Path
    wal_dir: pathlib.Path
    port_map: Mapping[str, tuple[str, ...]]     # {"COM0": ("COM0",)}；wal_com_alias 啟用時可 ("COM0","PROBE")
    rules: tuple[str, ...] = ()
    secrets: tuple[bytes, ...] = ()              # secret_tx_unlogged 用；合成帳密，來自 env，不寫入報告
    until_op: str | None = None                  # 以此 op 的 op_start 已 settle 快照為窗口終點；None＝最後一個 settle 快照
    checkpoint: pathlib.Path | None = None       # bridge 存活期間比較：以 checkpoints/<id>.json 驗證 wire／control 前綴（長度＋sha256）取代 closed manifest
def compare(inputs: CompareInputs) -> dict[str, Any]                       # comparison.v1
def check_zero_tx(inputs: CompareInputs, op_id: str) -> dict[str, Any]    # {"id":"zero_tx_on_reject","op_id","verdict","detail"}
def check_contiguous(inputs: CompareInputs, payloads: Sequence[bytes]) -> dict[str, Any]   # {"id":"no_forbidden_interleave",...}
VERDICTS = ("PASS", "FAIL", "INSUFFICIENT_EVIDENCE")
EXIT_CODES = {"PASS": 0, "FAIL": 1, "INSUFFICIENT_EVIDENCE": 4}

# compare/redact.py（§15.6.4）
def redact(evidence_dir: pathlib.Path, out_dir: pathlib.Path, *, secrets: Sequence[tuple[str, bytes]]) -> dict[str, Any]
    # 產生遮罩衍生目錄與 redaction-manifest.json（格式見 Task 2「證據遮罩」）；secrets 為 (env 名, 值)，值不寫入任何輸出
```

**比較程序（§15.6.3；順序即規則）**

1. **完整性先行**：`load_wire`、`load_wal`、`load_correlation` 任一有問題 → `verdict=INSUFFICIENT_EVIDENCE`，`reasons` 列出對應 `HARNESS_*`（`HARNESS_SCHEMA_INVALID`、`HARNESS_CRC_MISMATCH`、`HARNESS_SEQ_GAP`、`HARNESS_OFFSET_GAP`、`HARNESS_TAMPER_DETECTED`、`HARNESS_LOSS_FLAG`、`HARNESS_WAL_UNREADABLE`）。bridge-manifest 的 `closed_cleanly` 為 false 或 `failure` 非 null → `INSUFFICIENT_EVIDENCE`（`HARNESS_INSUFFICIENT_EVIDENCE`）。bridge 仍存活時（`checkpoint` 非 None）改驗 checkpoint：`wire.ndjson` 前 `wire_len` bytes 的 sha256 與 checkpoint 相符、`control.ndjson` 同理，且窗口終點的 `wire_seq` ≤ checkpoint `wire_seq`；不符 → `HARNESS_TAMPER_DETECTED`；checkpoint 缺失 → `HARNESS_INSUFFICIENT_EVIDENCE`。期中結果只作 step assertion，case 最終判定一律以 shutdown 後的 manifest 重跑比較。WAL 紀錄 `loss_flag:true` 或含 `rotation_failed` → 該埠 `INSUFFICIENT_EVIDENCE`（產品自述缺口，不得當作相等）。
2. **窗口**：取 correlation 中 `settled:true` 的快照為終點（`until_op` 指定時取該 op 的 `op_start`）；終點包含 `wal_seq` 與 `wire.offsets`、`wire.endpoint_generation`。沒有任何 settled 快照 → `INSUFFICIENT_EVIDENCE`（`HARNESS_WINDOW_UNSETTLED`）。窗口內若有多個 endpoint generation，以每次 `transport_reconnect` 的 `op_end` settled 快照切段；缺少切段快照 → 該埠 `INSUFFICIENT_EVIDENCE`。窗口內有 `wal reset`（correlation `action=="cli"` 且 argv 為 `["wal","reset"]`）→ `INSUFFICIENT_EVIDENCE`（seq 不唯一，盤點 04 §1.7）。
3. **串流**：WAL TX＝`dir=="TX"` 且 `com ∈ port_map[port]`、`seq ≤ wal_seq` 的 payload 依檔案順序串接；wire TX＝該埠、該 generation、`observation=="serial_read"` 且 `stream_offset < offsets.serial_read` 的 payload 依 offset 串接。RX 同理：WAL `dir=="RX"` ↔ wire `serial_write`。**不比 chunk 邊界、筆數、seq 或時間戳**。
4. **核准映射**：只在 `rules` 列出時套用。`secret_tx_unlogged`：比對到第一個差異位置 k 時，若 k 落在 WAL TX 紀錄邊界、且 wire TX 自 k 起等於某 `secret + b"\n"`，就略過 wire 這段並記 `rules_applied:[{"rule","dir":"TX","wire_offset":k,"len"}]`（產品 `send_secret` 刻意不記 WAL，盤點 06 §5.3）；`wal_com_alias`：允許 `port_map` 一埠對多個 WAL `com`。任何其他差異不得被規則吸收。
5. **判定**：所有串流相等 → `PASS`；任一不等 → `FAIL`，記 `first_diff_offset`、兩側各 32 bytes 的 hex 上下文（與任何 `secrets` 重疊的 bytes 以 `**` 取代）、兩側長度。

`comparison.v1`（`tests/acceptance/comparison.schema.json`）：`{"schema_version","run_id","case_id","verdict","reasons":[...],"tool":{"name","version","tree_sha256"},"integrity":{"wire_records","wire_sha256","wire_sha256_expected","wal_files":[...],"wal_records","problems":[...]},"window":{"until_op","wal_seq","wire_offsets","endpoint_generation"},"streams":[{"port_id","com":[...],"endpoint_generation","dir","wal_bytes","wire_bytes","equal","first_diff_offset","wal_context_hex","wire_context_hex","rules_applied":[...]}],"invariants":[...]}`。`secrets` 本身不得出現在輸出（只記長度）。

**`redact` 子命令**：`uart-stdio-bridge redact --evidence-dir D --out D.redacted --secret-env NAME ...`；exit 0，失敗（secret env 未設、輸出目錄已存在）exit 2。

**`compare` 子命令**：`[--checkpoint PATH]` 用於 bridge 存活期間的比較；`uart-stdio-bridge compare --evidence-dir D --wal-dir W --port-map COM0=COM0[,PROBE] [--rule secret_tx_unlogged] [--secret-env NAME ...] [--rule wal_com_alias] [--until-op OP] [--zero-tx OP ...] [--contiguous-b64 B64 ...] --out comparison.json`；exit code 照 `EXIT_CODES`（任一 invariant FAIL → 1；任一 INSUFFICIENT 且無 FAIL → 4）。

### T 步驟

- [ ] **T7-1：撰寫失敗測試**。`evidence_builder.py`：以 Task 2 的 `EvidenceWriter` 與 `make_*` 產生合法 `wire.ndjson`＋`bridge-manifest.json`＋`correlation.ndjson`，並以產品無關的方式（依盤點 04 §1.3 格式手寫 JSON 行）產生 `raw.wal.ndjson`；提供 `EvidenceSet.build(tx_chunks_wire, tx_chunks_wal, rx_chunks_wire, rx_chunks_wal, settle_points)`。關鍵竄改測試：

```python
TAMPERS = {
    "flip_payload_keep_crc":    ("HARNESS_CRC_MISMATCH",),
    "flip_payload_fix_crc":     ("HARNESS_TAMPER_DETECTED",),      # 單筆一致，但整檔 digest 不符
    "delete_middle_record":     ("HARNESS_SEQ_GAP", "HARNESS_OFFSET_GAP"),
    "duplicate_record":         ("HARNESS_SEQ_GAP",),
    "swap_two_records":         ("HARNESS_SEQ_GAP",),
    "change_len":               ("HARNESS_CRC_MISMATCH",),
    "set_meta_loss_flag":       ("HARNESS_LOSS_FLAG",),
    "truncate_last_line":       ("HARNESS_SCHEMA_INVALID",),
    "remove_bridge_manifest":   ("HARNESS_TAMPER_DETECTED",),
    "manifest_closed_uncleanly":("HARNESS_INSUFFICIENT_EVIDENCE",),
    "wal_flip_payload":         ("HARNESS_CRC_MISMATCH",),
    "wal_drop_required_key":    ("HARNESS_WAL_UNREADABLE",),
    "correlation_unsettled":    ("HARNESS_WINDOW_UNSETTLED",),
}

@pytest.mark.parametrize("tamper,codes", sorted(TAMPERS.items()))
def test_tampered_evidence_never_passes(tmp_path, tamper, codes):
    ev = EvidenceSet.clean(tmp_path)              # 未竄改時 compare(...)["verdict"] == "PASS"
    assert compare(ev.inputs())["verdict"] == "PASS"
    ev.apply_tamper(tamper)
    out = compare(ev.inputs())
    assert out["verdict"] == "INSUFFICIENT_EVIDENCE"
    assert set(codes) <= set(out["reasons"]), out["reasons"]

def test_content_difference_is_fail_with_first_diff(tmp_path):
    ev = EvidenceSet.build(tmp_path, tx_wire=[b"echo a\n", b"echo b\n"], tx_wal=[b"echo a\n", b"echo c\n"])
    out = compare(ev.inputs())
    assert out["verdict"] == "FAIL"
    s = next(x for x in out["streams"] if x["dir"] == "TX")
    assert s["equal"] is False and s["first_diff_offset"] == len(b"echo a\necho ")
```

其餘：

| 測試名 | 斷言 |
|---|---|
| `test_chunk_boundaries_ignored` | wire TX 以 1 byte 一筆、WAL TX 以單筆 4 KiB 表示相同 bytes → `PASS` |
| `test_rx_direction_compared_against_serial_write` | wire `dut_stdout_read` 與 `serial_write` 不同（模擬 peer gate 前的保留），只要 `serial_write`＝WAL RX 即 PASS |
| `test_window_respects_settle_point` | settle 點後 wire 多 100 B、WAL 多 100 B（不同內容）→ 仍 PASS（窗口外不比） |
| `test_secret_rule_only_when_enabled` | wire TX 多出 `b"s3cr3t\n"` 於 WAL 紀錄邊界：未啟用規則 → FAIL；`rules=("secret_tx_unlogged",), secrets=(b"s3cr3t",)` → PASS 且 `rules_applied` 長度 1；輸出 JSON 字串不含 `s3cr3t` |
| `test_secret_rule_not_mid_record` | 多出的 secret 位於 WAL 紀錄中間 → FAIL |
| `test_unknown_rule_rejected` | `load_rules(["strip_ansi"])` raise `ValueError` |
| `test_wal_com_alias_requires_rule` | WAL 部分 TX 的 `com=="PROBE"`：`port_map={"COM0":("COM0","PROBE")}` 但未啟用 `wal_com_alias` → `ValueError`；啟用後 PASS |
| `test_wal_loss_flag_insufficient` | 窗口內 WAL 有 `loss_flag:true` → `INSUFFICIENT_EVIDENCE` |
| `test_wal_reset_in_window_insufficient` | correlation 含 `wal reset` → `INSUFFICIENT_EVIDENCE` |
| `test_multi_generation_requires_split_snapshot` | wire 有 gen 1、2 而 correlation 無 reconnect settle → `INSUFFICIENT_EVIDENCE`；補上後分段比對 PASS |
| `test_wal_archive_order` | `raw.wal.ndjson.20261015-030617`、`.20261015-030617.1`、現行檔依序串接（DF-03 後綴） |
| `test_zero_tx_pass`（invariants） | op 前後 settled、wire `serial_read` offset 相同、WAL TX 無新紀錄 → `verdict=="PASS"` |
| `test_zero_tx_fail_on_wire_bytes` | op 期間 wire 多 1 byte → FAIL |
| `test_zero_tx_fail_on_wal_record` | op 期間 WAL 多一筆 TX（wire 無）→ FAIL |
| `test_zero_tx_insufficient_unsettled` | op_end `settled:false` → `INSUFFICIENT_EVIDENCE` |
| `test_zero_tx_insufficient_concurrent_ops` | 另一個 op 的區間與其重疊（多來源）→ `INSUFFICIENT_EVIDENCE`（§15.6.4） |
| `test_contiguous_pass_and_fail` | wire TX `b"echo A1\necho B1\n"`：`payloads=[b"echo A1\n", b"echo B1\n"]` PASS；`b"echo Aecho B1\n1\n"` → FAIL |
| `test_comparison_schema_valid` | 每個輸出以 `tests/acceptance/comparison.schema.json` 驗證通過 |
| `test_cli_exit_codes`（cli） | PASS→0、FAIL→1、INSUFFICIENT→4；`--secret-env` 讀 env；`--out` 檔案為 `indent=2, sort_keys=True` |
| `test_checkpoint_mid_run_pass_and_tamper` | 以 builder 產生「未 shutdown」證據＋checkpoint：`compare(checkpoint=...)` PASS；改動前綴中任一 byte → `HARNESS_TAMPER_DETECTED`；不給 checkpoint 也無 closed manifest → `INSUFFICIENT_EVIDENCE` |
| `test_redact_masks_secret_and_records_hashes` | wire TX 含 `b"s3cr3t\n"`：`redact` 後衍生 `wire.ndjson` 解碼不含 `s3cr3t`、CRC 與遮罩後 payload 相符；`redaction-manifest.json` 的 `raw_sha256` 等於原檔、`redacted_sha256` 等於衍生檔、`byte_equivalent is False`；輸出中不含 secret 字串 |
| `test_failure_context_masks_secret` | 首個差異鄰近 secret 時 `wal_context_hex`／`wire_context_hex` 不含 secret 的 hex |
| `test_builder_wal_matches_contract`（位於 `tests/test_acceptance_schemas.py`） | 以 P0 的 `contracts/wal_record.schema.json` 驗 `evidence_builder` 產生的每一行 WAL |
| `test_comparator_has_no_product_logic` | AST 掃 `compare/*.py`：不 import `re`（比較器不做任何 regex 正規化）、不出現 `prompt_regex`、`login_regex` 字串常數 |

- [ ] **T7-2：執行並確認 FAIL**：`python3 -m pytest -q tools/uart_stdio_bridge/tests -k compare` → `ModuleNotFoundError: uart_stdio_bridge.compare`。
- [ ] **T7-3：commit 測試**：`git commit -m "test(compare): 新增 WAL↔wire 比較、竄改拒絕與零 TX 不變量失敗測試"`。

### I 步驟

- [ ] **I7-1：實作** 各模組照 Interfaces 與「比較程序」五步；`mapping_rules.json` 記錄兩條規則的說明、依據（盤點 06 §5.3、盤點 04 §1.6）與核准 revision `2026-10-15.1`；`comparison.schema.json` 照上列欄位，`additionalProperties:false`。
- [ ] **I7-2：執行並確認 PASS**：`python3 -m pytest -q tools/uart_stdio_bridge/tests -k "compare or redact" tests/test_acceptance_schemas.py` 全綠（`test_builder_wal_matches_contract` 已於 T7-1 撰寫）。
- [ ] **I7-3：commit**：`git commit -m "feat(compare): 實作 WAL↔wire byte stream 比較器與竄改拒絕"`＋fragment。

---

## Task 8：bridge 自測（§15.4.3／Spike 4）與資格報告

**Test author model:** codex-gpt-6-luna-max　**Implementer model:** agy-gemini-3.8-flash-high
**Changelog fragment:** `changelog.d/rust-p2-bridge-selftest.md`（`type: feat`、`scope: test-infra`）
**BLOCKED-OWNER evidence:** 無（OD-15 起改為可執行 gate）。
**擁有者 host 可執行 gate（OD-15，G4）：** `selftest --endpoint win-serial --com-pair COM20:COM21` 在擁有者 host（Spike 1 基本資格已通過的 com0com DUT-side pair）經 interop 原生執行，`verdict=="PASS"` 的報告登錄於 `stage0_evidence_index.yaml`（`S0-BRIDGE-SELFTEST-WIN`）。沒有此報告前，Windows bridge 只能當接線工具，不得作 gate 證據（§13 Spike 4）；報告若含 FAIL 項，依 §15.4.3 停止，不得以 Linux 自測替代。

**Files:**
- Create: `tools/uart_stdio_bridge/src/uart_stdio_bridge/selftest/{__init__.py,checks.py,runner.py}`、`tools/uart_stdio_bridge/APPROVED_REVISION.json`
- Modify: `cli.py`（`selftest` 子命令）
- Create: `tools/uart_stdio_bridge/tests/test_selftest.py`

**Interfaces:**

```python
# selftest/checks.py
@dataclass(frozen=True)
class CheckResult:
    id: str; verdict: str; reason: str; evidence_dir: str     # verdict ∈ {"PASS","FAIL","NOT_RUN"}
CHECK_IDS: tuple[str, ...] = ("ST-01","ST-02","ST-03","ST-04","ST-05","ST-06","ST-07",
                              "ST-08","ST-09","ST-10","ST-11","ST-12","ST-13","ST-14")
def run_check(check_id: str, ctx: "SelftestContext") -> CheckResult
@dataclass(frozen=True)
class SelftestContext:
    endpoint: str                     # "posix-pty" | "win-serial"
    com_pair: tuple[str, str] | None  # win-serial：(受測端 COMa, bridge 端 COMb)
    transport_mode: str
    work_dir: pathlib.Path
    seed: int = 20261015
# selftest/runner.py
def run_selftest(ctx: SelftestContext, *, only: Sequence[str] | None = None) -> dict[str, Any]   # bridge-selftest.v1
def load_approved_revision(path: pathlib.Path | None = None) -> dict[str, Any]
    # 預設 tools/uart_stdio_bridge/APPROVED_REVISION.json：{"schema_version":"bridge-approved.v1","tree_sha256","reviewer","review_ref","date"}
    # reviewer 不得為該 revision 的實作者；欄位缺少或空字串 raise ValueError
def is_qualified(report: dict[str, Any], *, endpoint: str, tool_tree_sha256: str, platform_os: str,
                 approved: dict[str, Any]) -> bool
    # 全部 14 項 PASS（ST-12 在 win-serial 且 capability 不成立時允許 NOT_RUN 但報告須標 transport_disconnect:false）、
    # report.tool.tree_sha256 == approved.tree_sha256 == 目前工具（三者相等；只改工具並重跑自測不能自我資格化，§10.4／ADR-08）、
    # report.platform.os 相同、report.endpoint 相同、report 的 test_overrides 為空
```

`bridge-selftest.v1`：`{"schema_version":"bridge-selftest.v1","created_at","tool":{"name","version","tree_sha256"},"platform":{...},"endpoint","transport_mode","capabilities":{...},"checks":[CheckResult...],"verdict":"PASS"|"FAIL"}`；`verdict` 只有在所有必要項 PASS 時為 PASS。CLI：`uart-stdio-bridge selftest --endpoint posix-pty|win-serial [--com-pair COMa:COMb] [--transport-mode ...] --out DIR [--only ST-01,...]`，報告寫 `DIR/selftest-report.json`，exit 0（PASS）／1（FAIL）。

**自測項目（自測中的「受測端 A」由自測程式自己開啟，角色為測試替身）**

| ID | §15.4.3／Spike 4 項 | 做法 | PASS 條件 |
|---|---|---|---|
| ST-01 | 完整 byte 值域 | A 端雙向送 `bytes(range(256))*16`，DUT=echo | 四個 observation 串流與 A 端收到的資料全等；比較器（自建 WAL 替身＝A 端實際收送 bytes）PASS |
| ST-02 | 多區塊資料 | 以 seed 產生 4 MiB 亂數、A 端以隨機 1～8192 B 分塊寫 | 同上；`seq` 連續、offset 連續 |
| ST-03 | 短讀／部分寫入 | (a) 真 pipe：DUT `--read-delay-ms 20`；A 端一次寫 2 MiB 且不讀，之後再讀；(b) in-process：`StubDutProcess` 依腳本回傳短寫長度、`FakeEndpoint.write_some` 回傳短寫長度 | (a) 全部 bytes 無遺失無重複，`dut_stdin_write` 各筆 `len` 總和＝DUT 實收；serial 端短讀（`serial_read` 筆數 >1）；(b) wire 記錄的每筆接受長度等於腳本值、串接無遺失無重複（真 pipe 無法穩定製造短寫，§15.7.1） |
| ST-04 | stderr 大量輸出 | DUT `--stderr-flood-bytes 50000000` 同時往返 1 MiB | 往返 < 30 s；`dut.stderr.log` 大小正確；wire 中無 stderr bytes |
| ST-05 | EOF | DUT `--close-stdout-after-bytes 1024` | META `dut_stdout_eof`；之前 1024 B 完整；TX 仍轉送 |
| ST-06 | 程序退出 | DUT `--exit-after-bytes 512 --exit-code 7`，未加 `--allow-dut-exit` | bridge exit 3、`HARNESS_DUT_EXITED`、比較器對此證據回 `INSUFFICIENT_EVIDENCE` |
| ST-07 | 故障控制 | 依序呼叫 Task 5 表中每個 action | 每個 action 在 `control.ndjson` 有 requested＋completed（或能力不足的 failed＝`HARNESS_CAPABILITY_UNAVAILABLE`）；generation 變化與 META 一致 |
| ST-08 | CRC／offset 驗證 | 正常結束的證據跑 `load_wire` | 無問題；`bridge-manifest` digest 相符 |
| ST-09 | 破壞證據後拒絕 | 對 ST-08 證據套 Task 7 的 13 種竄改 | 13 種皆非 PASS，且原因碼符合 `TAMPERS` |
| ST-10 | 空間不足 | POSIX：以 `resource.setrlimit(RLIMIT_FSIZE, 1 MiB)`＋忽略 `SIGXFSZ` 啟動 bridge 子程序，使證據寫入得到 `EFBIG`；Windows：以 bridge 測試鉤子 `UART_STDIO_BRIDGE_TEST_FAULT=enospc_after_1mib` 注入 `os_write` ENOSPC，報告註明 `injection:"os_write"` | bridge exit 3、`HARNESS_DISK_FULL`；比較器 `INSUFFICIENT_EVIDENCE` |
| ST-11 | DUT restart | `dut_restart` 兩次 | `dut_generation` 1→2→3、offset 不重設、META 正確 |
| ST-12 | transport fault | POSIX：`transport_disconnect`＋`transport_reconnect`；win-serial：capability 為 False 時 `NOT_RUN`＋報告 `transport_disconnect:false` | A 端看到 hangup；新世代 offset 自 0 起 |
| ST-13 | 不確定寫入 | in-process `FakeEndpoint` raise `EndpointWriteUncertain` | META `write_uncertain` `loss_flag:true`、bridge `HARNESS_WRITE_UNCERTAIN` |
| ST-14 | 非預期 overflow | in-process：writer `os_write` 阻塞、`max_pending_records=8` | `HARNESS_EVIDENCE_OVERFLOW`，pump 停止，比較器 `INSUFFICIENT_EVIDENCE` |

### T 步驟

- [ ] **T8-1：撰寫失敗測試** `test_selftest.py`：

| 測試名 | 斷言 |
|---|---|
| `test_check_ids_cover_spec_list` | `CHECK_IDS` 恰 14 項；`checks.py` docstring 對照表含 §15.4.3 九項關鍵詞（完整 byte 值域、多區塊、短讀、stderr、EOF、程序退出、故障控制、CRC、破壞證據）與 Spike 4 的 DUT restart、transport fault、空間不足 |
| `test_posix_selftest_all_pass`（`posix_pty`） | `run_selftest(SelftestContext(endpoint="posix-pty",...))["verdict"]=="PASS"`，14 項皆 PASS |
| `test_report_schema_and_identity` | 報告 `tool.tree_sha256 == tool_tree_sha256()`；`platform.os == platform.system()` |
| `test_is_qualified_rejects_other_revision` | 改動報告 `tool.tree_sha256` 一個字元 → `is_qualified` False；報告與目前工具一致但 `approved.tree_sha256` 不同 → False；三者一致 → True |
| `test_approved_revision_matches_tool_tree` | `load_approved_revision()['tree_sha256'] == tool_tree_sha256()`（工具行為檔變動而 `APPROVED_REVISION.json` 未經審閱更新即 FAIL；CI `bridge-tools` job 執行） |
| `test_approved_revision_reviewer_present` | `reviewer`、`review_ref`、`date`（ISO 日期）皆非空 |
| `test_is_qualified_rejects_any_fail_or_notrun` | 任一項改 FAIL → False；posix 報告任一項 NOT_RUN → False |
| `test_win_serial_transport_not_run_allowed_only_with_flag` | win-serial 報告 ST-12 為 NOT_RUN 且 `capabilities.transport_disconnect` 為 False → True；為 True 卻 NOT_RUN → False |
| `test_selftest_detects_broken_bridge` | 以 env `UART_STDIO_BRIDGE_TEST_FAULT=drop_every_1000th`（bridge 測試鉤子：每 1000 bytes 丟 1 byte 且照常記錄）跑 ST-02 → FAIL（證明自測能抓到 bridge 自身漏資料） |
| `test_cli_selftest_exit_code` | `--only ST-01` 成功 exit 0；`--only ST-06 --endpoint posix-pty` 仍 exit 0（該項 PASS 條件是 bridge 正確失敗） |

- [ ] **T8-2：執行並確認 FAIL**：`python3 -m pytest -q tools/uart_stdio_bridge/tests/test_selftest.py` → `ModuleNotFoundError`。
- [ ] **T8-3：commit 測試**：`git commit -m "test(bridge): 新增 bridge 自測與資格判定失敗測試"`。

### I 步驟

- [ ] **I8-1：實作** `checks.py`（每項一函式，各自建 `work_dir/<ID>/` 證據目錄）、`runner.py`、`cli.py selftest`；`UART_STDIO_BRIDGE_TEST_FAULT` 只供自測使用：`drop_every_1000th` 作用於 `tx-writer`（轉送給 DUT 時每 1000 bytes 丟 1 byte 但照常記錄），`enospc_after_1mib` 作用於 `EvidenceWriter` 注入的 `os_write`（累計 1 MiB 後 raise ENOSPC）；`bridge-manifest` 的 `test_overrides` 記錄生效值，`is_qualified` 與比較器遇到非空 `test_overrides` 一律不給資格。
- [ ] **I8-2：執行並確認 PASS**：先跑 `python3 -m pytest -q tools/uart_stdio_bridge/tests/test_selftest.py`，此時除 `test_approved_revision_*` 外全綠（兩者因尚未核准而紅，屬預期）；Task 8 雙審（§1 第 6 點）通過後，由審查者（非實作者）以審查紀錄連結填寫 `APPROVED_REVISION.json`（獨立 commit `chore(bridge): 核准工具 revision <12hex>`），再跑一次須全綠。之後任何改動工具行為檔（`src/**`、`pyproject.toml`、`docker/Dockerfile.dut`）的 commit 都必須伴隨新的審閱與 `APPROVED_REVISION.json` 更新，否則 CI 紅燈（README 載明）。之後產出正式資格報告 `python3 -m uart_stdio_bridge selftest --endpoint posix-pty --out ~/b-log/rust-refactor/p2/selftest-linux`（`PYTHONPATH=tools/uart_stdio_bridge/src`），保存 `selftest-report.json` 作 Stage 0 證據。
- [ ] **I8-3：commit**：`git commit -m "feat(bridge): 實作 §15.4.3 bridge 自測與 gate 資格判定"`＋fragment。

---

## Task 9：socat 接線 spike 腳本（§15.4.1 第一步；執行順序在 Task 1 之後、Task 2 之前）

**Test author model:** haiku-xhigh　**Implementer model:** agy-gemini-3.8-flash-high
**Changelog fragment:** `changelog.d/rust-p2-socat-spike.md`（`type: feat`、`scope: test-infra`）
**BLOCKED-OWNER evidence:** 無。

**Files:**
- Create: `tools/uart_stdio_bridge/spikes/socat_wiring.sh`、`tools/uart_stdio_bridge/spikes/echo_child.py`、`tools/uart_stdio_bridge/spikes/README.md`
- Create: `tools/uart_stdio_bridge/tests/test_socat_spike.py`

**Interfaces:**

`socat_wiring.sh [--out DIR]`：`set -euo pipefail`；需要 `socat`（缺少時印 `{"result":"NOT_RUN","reason":"socat_missing"}` 並 exit 0 —— spike 不是 gate）；步驟：

1. `T=$(mktemp -d)`；啟動 `socat -d -d PTY,link=$T/pty,raw,echo=0,wait-slave EXEC:"python3 $HERE/echo_child.py",pipes`（背景，stderr 存 `$OUT/socat.stderr.log`）。
2. 以 inline Python 開 `$T/pty`（`O_RDWR|O_NOCTTY`，`tty.setraw`）寫 `bytes(range(256))*4`，讀回比對（raw 設定與 binary 保真）。
3. 寫 `b"__EXIT__\n"` 令 `echo_child.py` 結束，量測 PTY 端讀到 EOF／EIO 的時間（EOF 與子程序連接語意）。
4. 輸出 `$OUT/spike-report.json`：`{"schema_version":"socat-spike.v1","socat_version","checks":{"raw_roundtrip":bool,"no_echo":bool,"eof_on_child_exit":bool,"eof_latency_ms":int},"note":"拓樸可行性檢查，非差分證據格式（§15.4.1）"}`。

`echo_child.py`：stdlib；以 `sys.stdin.buffer`／`sys.stdout.buffer` 原樣回寫（`flush` 每次）；讀到行 `__EXIT__` 時 exit 0。

### T 步驟

- [ ] **T9-1：撰寫失敗測試** `test_socat_spike.py`：

| 測試名 | 斷言 |
|---|---|
| `test_spike_report_shape` | 執行腳本（`shutil.which("socat")` 不存在時斷言輸出為 `NOT_RUN`）；報告 `schema_version=="socat-spike.v1"`、`note` 含「非差分證據」 |
| `test_spike_checks_pass_with_socat` | socat 存在時 `raw_roundtrip`、`no_echo`、`eof_on_child_exit` 皆 True |
| `test_spike_report_has_no_verdict` | 報告沒有 `verdict` 欄位（「不得作 gate 證據」的 `cases.yaml` 檢查移到 Task 12 `test_spike_is_not_gate_evidence`，避免前向依賴） |
| `test_echo_child_binary` | 直接執行 `echo_child.py`：輸入 `bytes(range(256))+b"\n__EXIT__\n"`，輸出等於 `bytes(range(256))+b"\n"`、exit 0 |

- [ ] **T9-2：執行並確認 FAIL**：`python3 -m pytest -q tools/uart_stdio_bridge/tests/test_socat_spike.py` → 腳本不存在。
- [ ] **T9-3：commit 測試**：`git commit -m "test(bridge): 新增 socat 接線 spike 的失敗測試"`。

### I 步驟

- [ ] **I9-1：實作** 兩個腳本與 `spikes/README.md`（說明用途、限制、輸出欄位）。
- [ ] **I9-2：執行並確認 PASS**：`python3 -m pytest -q tools/uart_stdio_bridge/tests/test_socat_spike.py`；執行 `bash tools/uart_stdio_bridge/spikes/socat_wiring.sh --out ~/b-log/rust-refactor/p2/socat-spike` 保存報告（本機 socat 1.8.0.0）。這是 Task 2～5 的前置 gate：`raw_roundtrip`、`no_echo`、`eof_on_child_exit` 任一為 False（或 NOT_RUN）時停止並回報，不得開始 bridge 實作；報告的 `eof_latency_ms` 記入 Task 5 README 作為 `dut-watch`／`rx-reader` EOF 處理的設計依據。
- [ ] **I9-3：commit**：`git commit -m "feat(bridge): 新增 socat PTY＋EXEC 接線可行性 spike"`＋fragment。

---
## Task 10：func-test 外部 bridge 模式、DaemonHarness 隔離與 artifact 選定、oracle 修復、Windows Telnet console client

**Test author model:** agy-gemini-3.8-flash-high　**Implementer model:** 10A haiku-xhigh（case YAML／oracle 修復、`HarnessConfig`／env 隔離、`artifact_identity`、README、`tools/docker/*`）；10B codex-gpt-6-luna-max（`bridge_session`、`correlation`、settle／quiesce／checkpoint 迴圈、`TelnetClientCodec`／`TelnetConsole`、runner bridge 模式與 Windows endpoint 接線）。10A 先做，10B 依賴 10A 的 `HarnessConfig`。
**Changelog fragment:** `changelog.d/rust-p2-functest-bridge-mode.md`（`type: feat`、`scope: func-test`）
**BLOCKED-OWNER evidence:**
- 實體傳輸替代模式（OD-08 C）的 manifest 與逐案例承接紀錄（只在 com0com 路徑失效時才需要；OD-15 Spike 1 基本資格已通過，預設不執行）。

**擁有者 host 可執行 gate（OD-15，G5）：** Windows 原生：Python daemon（Windows，OD-11 隔離實例＋`SERIALWRAP_TEST_DEVICE_ALLOWLIST=COM20`）＋`WinSerialEndpoint`（bridge 開 COM21）依下方「Windows 接線契約」執行 13 個 case 的 bridge 模式與 `win-com-*` 三個情境（Task 11、Task 12 登記），經 interop 在擁有者 host 原生執行。Linux 上只以 fake-os 單元測試驗接線邏輯、以 fake Telnet server 驗 client codec，不得計為 Windows 原生結果。

**Files:**
- Modify: `func-test/runner.py`、`func-test/lib/daemon_harness.py`、`func-test/lib/cli_client.py`、`func-test/lib/console_client.py`、`func-test/lib/yaml_loader.py`、`func-test/README.md`
- Create: `func-test/lib/bridge_session.py`、`func-test/lib/correlation.py`、`func-test/lib/artifact_identity.py`、`realhw/telnet_client.py`（stdlib-only；Task 14 的 frontend 共用）
- Create: `realhw/deployed_content.py`（stdlib-only；**實際部署內容量測的唯一產生端實作**，R5-F4：本 Task 的 func-test、Task 13 的 pytest 資源報告、Task 14 的 realhw／regression 報告、Task 15 的 `release-gate.json` 與擁有者手動程序都以它量測並寫入報告；P9 Task 14B ingest 只採信報告內的量測值，不以 target 回填）、`tests/test_deployed_content.py`（`core_portable`＋`memory_only`）
- Modify: `func-test/cases/*.yaml`（13 個：新增 `bridge:` 區塊；`hk-01`、`hk-02`、`hk-03`、`hk-04`、`cowork-01`、`sm-04`、`ev-01` 依 `TST-D-9`／`TST-D-8` 修 oracle 與路徑，ID 與 `meta.description` 不變）
- Modify: `tools/docker/uart_harness.py`、`tools/docker/remote_lab.py`（arch §9.3：`SERIALWRAP` 改為 `os.environ.get("SERIALWRAP_CLI_EXE") or ROOT_DIR/"serialwrap"`、daemon argv 改用 `resolve_daemon_argv()`；`TargetConfig` 改自 `lib.target_config` import）
- Create: `tests/test_functest_harness.py`、`tests/test_functest_oracles.py`、`tests/test_telnet_client.py`、`tests/test_functest_bridge_smoke.py`、`tests/test_functest_windows_wiring.py`（全部建立時即依 P0 T14 帶 `pytestmark`：前四者模組層 `core_portable`＋`memory_only`，但 `test_telnet_console_against_fake_server` 開 socket → 該函式改標 `process_rpc`（依 T14 第 2 條，模組層只放平台 marker）；`test_functest_bridge_smoke.py` 為 `posix_only`＋`posix_pty`＋`process_rpc`；`test_functest_windows_wiring.py` 為 `core_portable`＋`memory_only`）

**Interfaces:**

Consumes：Task 4 `TargetConfig.to_json`；Task 5 `run` CLI、`ControlClient`；Task 7 `compare`、`check_zero_tx`、`check_contiguous`；Task 8 `is_qualified`；`uart_stdio_bridge.identity`。

```python
# func-test/lib/daemon_harness.py
ISOLATED_DIR_ENV: dict[str, str] = {           # 9 個，對齊 tests/conftest.py（盤點 08 §1.2）
    "SERIALWRAP_STATE_DIR": "state", "SERIALWRAP_RUN_DIR": "run", "SERIALWRAP_WAL_DIR": "wal",
    "SERIALWRAP_CONFIG_DIR": "config", "SERIALWRAP_LOG_DIR": "blog", "SERIALWRAP_EVENTS_DIR": "events.d",
    "SERIALWRAP_EVENTS_RUNTIME_DIR": "events-rt", "SERIALWRAP_BY_ID_DIR": "by-id", "SERIALWRAP_BY_PATH_DIR": "by-path"}
# 外層環境隔離比照 P3 `isolated_env`（04 §「isolated_env 內容」）：先刪除外層 env 中**所有** `SERIALWRAP_` 開頭的變數
# （涵蓋 #120 的 5 個高優先變數、P1 新增的 SERIALWRAP_CONFIG／_TEST_DEVICE_ALLOWLIST／_RELEASE_MANIFEST、P3 的
# _TCP_PORT／_RPC_BACKEND／_BENCHES_FILE／_LOG_LEVEL 與日後新增者），再設 ISOLATED_DIR_ENV 與下列 harness 顯式值；
# 另設 XDG_CONFIG_HOME=<root>/xdg-config、XDG_STATE_HOME=<root>/xdg-state、刪除 XDG_RUNTIME_DIR。
# 只有 ARTIFACT_ENV 兩個值在刪除前讀出、供 resolve_* 使用，不傳給 daemon。
ARTIFACT_ENV: tuple[str, ...] = ("SERIALWRAP_CLI_EXE", "SERIALWRAP_DAEMON_EXE")   # rust-architecture.md §9.2
def build_isolated_env(base: Mapping[str, str], root: pathlib.Path, *, extra: Mapping[str, str]) -> dict[str, str]
@dataclass
class HarnessConfig:
    target_config: TargetConfig = field(default_factory=TargetConfig)
    profile_overrides: dict[str, Any] = field(default_factory=dict)
    com: str = "COM0"
    alias: str = "func-test"
    env_files: dict[str, str] = field(default_factory=dict)
    mode: str = "pty"                         # "pty" | "bridge"
    dut_mode: str = "local-process"           # bridge 模式："local-process" | "docker"
    dut_plan: dict[str, Any] | None = None    # dut-plan.v1
    evidence_dir: pathlib.Path | None = None  # None → <root>/evidence
    run_id: str = ""
    case_id: str = ""
    # —— Windows 接線（bridge 模式；POSIX 忽略）——
    transport_mode: str = "virtual_pty"       # "virtual_pty" | "virtual_com0com" | "physical_usb_uart"
    daemon_com: str | None = None             # 受測端 COM-A（daemon 開啟；profile targets 綁定此 COM）
    bridge_com: str | None = None             # bridge 端 COM-B（WinSerialEndpoint 開啟）
    rpc_endpoint: str | None = None           # None → POSIX：<root>/run/serialwrapd.sock；Windows：f"tcp://127.0.0.1:{free_port()}"
def resolve_cli_exe() -> list[str]            # env SERIALWRAP_CLI_EXE → [exe]；未設 → [ROOT_DIR/"serialwrap"]
def resolve_daemon_argv() -> list[str]        # env SERIALWRAP_DAEMON_EXE → [exe]；未設 → [$PYTHON or sys.executable, ROOT_DIR/"serialwrapd.py"]
def free_port() -> int                        # bind 127.0.0.1:0 取得後關閉
class DaemonHarness:
    def __init__(self, config: HarnessConfig | None = None) -> None
    endpoint: str; env: dict[str, str]; root: pathlib.Path; evidence_dir: pathlib.Path
    socket_path: str                          # 相容既有呼叫端：POSIX 為 socket 路徑；Windows 等於 endpoint 字串
    fake_target: "FakeTarget | None"          # pty 模式；`lib.fake_target` 只在 mode=="pty" 時於 start() 內 lazy import
    bridge: "BridgeSession | None"            # bridge 模式
    def start(self) -> None
    def wait_endpoint(self, timeout_s: float = 5.0) -> None
        # POSIX：os.path.exists(socket_path)；Windows（tcp://）：connect probe 成功；逾時 raise TimeoutError
        # （取代 runner 內的 `_wait_for_socket`，runner 改呼叫本方法）
    def stop(self) -> None
    def target_emit(self, payload: bytes) -> None          # pty：os.write(master)；bridge：dut_control emit
    def pause_target(self, duration_s: float) -> None      # pty：FakeTarget.pause_responding；bridge：dut_control pause_responding
    def device_event(self, event: str) -> None             # "remove"/"add"：pty 刪建 symlink；bridge：by_id_link_remove/add（Windows 無此能力 → StepError cap_missing:by_id_link）
    def wal_dir(self) -> pathlib.Path

# func-test/lib/bridge_session.py
class BridgeSession:
    def __init__(self, *, run_id: str, case_id: str, port_id: str, endpoint_spec: dict[str, Any],
                 evidence_dir: pathlib.Path, target_json: dict[str, Any], plan_json: dict[str, Any] | None,
                 dut_mode: str, env: dict[str, str], quiesce: bool = False,
                 extra_args: Sequence[str] = ()) -> None
        # extra_args：case／情境 `bridge.run_args`（只允許 `--max-inflight-bytes`、`--read-chunk`、`--write-timeout-s`、`--allow-dut-exit`；其他 → StepError）
        # endpoint_spec 即 Task 3 `open_endpoint` 的 spec：POSIX {"kind":"posix-pty","by_id_link":...}；
        # Windows {"kind":"win-serial","com":bridge_com,"transport_mode":...}；轉成 `run` 子命令參數
        # quiesce：case 的 DUT 開 noise（target.noise.enabled）時為 True，settle 改用 barrier mode:"quiesce"
    def start(self, timeout_s: float = 15.0) -> None      # 啟動 `python3 -m uart_stdio_bridge run`，等 BRIDGE_READY
    def call(self, action: str, params: dict[str, Any] | None = None) -> dict[str, Any]
    def snapshot(self, *, settle: bool) -> dict[str, Any]
        # settle=False：barrier{mode:"passive",quiet_ms:0,timeout_ms:500} 結果（只取 offsets）
        # settle=True、quiesce=False：最多 10 輪 {b1=barrier(); seq=wal_current_seq(); b2=barrier()}，b1.offsets==b2.offsets 且兩者 settled 才回
        #              {"settled":True,"wal_seq":seq,"wire":b2}；否則 {"settled":False,...}
        # settle=True、quiesce=True：b1=barrier{mode:"quiesce"}（RX 暫停中）；最多 10 輪等 `wal current-seq` 連續兩次相同且間隔 ≥quiet_ms；
        #              取 seq 後 b2=barrier{mode:"passive"}，b1.offsets==b2.offsets 才 settled；最後一定 resume_pump{dir:"RX"}
    def checkpoint(self) -> pathlib.Path                    # bridge `checkpoint` 動作；runner 同時 flush correlation.ndjson
    def wire_tail(self) -> "WireTail"
    def stop(self) -> dict[str, Any]                       # shutdown；回 bridge exit code 與 evidence digest
class WireTail:
    def find(self, marker: bytes, *, observation: str, timeout_s: float) -> int | None   # 回 marker 起始 stream_offset

# func-test/lib/correlation.py
class CorrelationLog:
    def __init__(self, path: pathlib.Path, *, run_id: str, case_id: str, clock_id: str) -> None
    def op_start(self, op_id: str, *, step_index: int, action: str, argv: list[str] | None, snapshot: dict | None) -> None
    def op_end(self, op_id: str, *, step_index: int, action: str, rc: int | None, response: dict | None,
               snapshot: dict | None) -> None
    def marker(self, op_id: str, *, marker: str, marker_dir: str) -> None
    def close(self) -> None

# func-test/lib/artifact_identity.py
def collect_artifacts(cli_argv: list[str], daemon_argv: list[str], repo_root: pathlib.Path) -> dict[str, Any]
    # cli：path（realpath）、sha256、version_stdout（`<cli> --version` 原樣）；
    # daemon：argv、sha256（argv[0] 為非 python 執行檔時）、implementation（argv 含 serialwrapd.py 或 `-m sw_core.daemon` → "python"，否則 "rust"）、
    #         package_tree_sha256／repo_tree_sha256（python 時以 identity 同名函式計算；rust 時為 null，身分由 sha256＋release manifest 承擔）；
    # release_manifest_sha256：CLI 同目錄上一層有 release-manifest.json 時的 sha256，否則 null
    # deployed_content：本函式不填；runner 於每個 case run 啟動 daemon 前與 harness stop 後各呼叫一次
    #   realhw.deployed_content.measure(cli_argv[0], daemon_argv=daemon_argv)，daemon ready 後以 health.status.pid 呼叫一次 live_daemon(pid)，
    #   寫入 manifest.json 的 artifacts.deployed_content = bracket(start, end, live=...)（R5-F4；daemon 綁定為 R5 審查後續）

# realhw/deployed_content.py（stdlib-only；R5-F4）
SCHEMA: str = "deployed-content.v1"
class DeployedContentError(RuntimeError): ...
def install_dir(cli_exe: pathlib.Path) -> pathlib.Path | None
    # 與 P9 Task 9 resolve_artifact 同一定位規則：Linux realpath(cli_exe).parent.parent／"release-manifest.json" 存在 → 該目錄；
    # Windows（os.name=="nt"）cli_exe.parent／"current-release.json" 的 "dir"／"release-manifest.json" 存在 → 該目錄；否則 None（例：repo 原始碼執行的 Python 基線）
WINDOWS_ENTRY_FILES: frozenset[str] = frozenset({"serialwrap.exe", "serialwrapd.exe", "current-release.json"})   # arch §10.2 Windows 穩定 bin 的全部合法內容
def entry_dir_check(cli_exe: pathlib.Path, bundle_dir: pathlib.Path) -> tuple[str | None, str | None]
    # (entry_dir_sha256, error)（B5，R5 審查後續）：Windows 實際執行的是穩定 bin 內的 exe **複本**，DLL 搜尋順序先找 exe 所在目錄，
    # 只量 versions/<v> 會漏掉穩定 bin 旁的旁載檔。Windows：cli_exe.parent 只能含 WINDOWS_ENTRY_FILES 的一般檔（其他任何項目，含子目錄、symlink → error
    # "entry_dir_extra: <name>"）；serialwrap.exe／serialwrapd.exe 的 sha256 必須等於 bundle_dir/"bin"/<同名> → 否則 "entry_copy_mismatch: <name>"；
    # entry_dir_sha256 以 tree_digests 同演算法對 cli_exe.parent 計算。POSIX（穩定入口為指向 versions/<v>/bin 的 symlink，實際載入目錄即 bundle 內）→ (None, None)
def tree_digests(bundle_dir: pathlib.Path) -> tuple[str, str]
    # (bundle_tree_sha256, runtime_tree_sha256)：與 P9 Task 3 bundle_tree_sha256／runtime_tree_sha256 **同演算法**（sorted "<posix relpath>\0<sha256>\n" 的 sha256；
    # 前者涵蓋全部一般檔、後者只排除根目錄 release-manifest.json 與 SHA256SUMS），單次走訪同時算兩值；每次呼叫都重新雜湊全部檔案（不以 size／mtime 快取）；
    # symlink 或其他非一般檔 → DeployedContentError("non-regular file: <relpath>")
def measure(cli_exe: pathlib.Path, *, daemon_argv: Sequence[str] | None = None, now: Callable[[], str] | None = None) -> dict[str, Any]
    # 回 deployed-content.v1：{"schema_version","measured_at","cli_configured","cli_resolved","cli_sha256","install_dir","release_manifest_sha256",
    #   "bundle_tree_sha256","runtime_tree_sha256","entry_dir_sha256","daemon_configured","daemon_resolved","daemon_sha256","error"}；
    #   量測的是**這次執行實際使用的那份 CLI 所在的安裝與要啟動的 daemon 映像**，不是任何宣告值。
    # daemon_argv（R5 審查後續）：harness／runner 實際用來 spawn daemon 的 argv（黑箱為 SERIALWRAP_DAEMON_EXE，func-test 為 resolve_daemon_argv()）；
    #   realhw／TestPilot 不 spawn daemon 時傳 None，daemon 身分改由 live_daemon 承擔（"daemon_configured" 等三欄為 null、不算 error）。
    # error（依序取第一個）：install_dir 為 None → "unresolved"；sha256(realpath(cli_exe)) != manifest components.cli.sha256 → "cli_mismatch"；
    #   tree_digests 拋 DeployedContentError → "tree_invalid: <relpath>"；entry_dir_check 的 error；
    #   daemon_argv 給定但 realpath(daemon_argv[0]) 不在 install_dir 之下 → "daemon_outside_install"（例：release CLI 配 repo 的 py-serialwrapd）；
    #   在 install_dir 內但 sha256 既不等於 components.daemon.launcher.sha256 也不等於 components.daemon.runtime.sha256 → "daemon_mismatch"；
    #   error 非 None 時 install_dir 以外的 digest 欄位一律 null（不得部分填值）
def live_daemon(pid: int, install: pathlib.Path | None) -> dict[str, Any]
    # 量測**實際在服務**的 daemon 行程映像（R5 審查後續）：{"pid","image_path","image_sha256","in_install","error"}；
    #   POSIX realpath("/proc/<pid>/exe")；Windows ctypes OpenProcess(PROCESS_QUERY_LIMITED_INFORMATION)＋QueryFullProcessImageNameW；
    #   取不到 → error "image_unavailable"；in_install＝image_path 位於 install 之下；pid 取自 health.status.pid（Python bundle 為 py/python3 或 py\python.exe，
    #   Rust 為 bin/serialwrapd；消費端以 manifest 的 launcher／runtime sha256 核對）
def bracket(start: Mapping[str, Any], end: Mapping[str, Any], *, live: Sequence[Mapping[str, Any]] = ()) -> dict[str, Any]
    # {"start": start, "end": end, "daemon_live": list(live)}；產生端一律在受測工作開始前量 start、結束後量 end，兩次都寫進報告，
    #   daemon 在跑期間至少量一次 live（不在產生端判定相等，由消費端判定）
# CLI：python3 -m realhw.deployed_content --cli <exe> [--daemon-pid <pid>] --out <path>（--daemon-pid 時另寫 live_daemon 結果；寫單一 measure 結果，sort_keys=True；供擁有者手動程序在執行前後各跑一次，P9 Task 14B manual-procedure）

# func-test/lib/console_client.py（新增）
class TelnetConsole:
    def __init__(self, endpoint: str, *, timeout_s: float = 5.0) -> None   # "127.0.0.1:PORT"
    def write(self, data: bytes) -> None                                    # IAC escape
    def read(self, timeout_s: float, *, max_bytes: int = 65536) -> bytes    # 回已去除 Telnet 指令的資料
    def close(self) -> None

# realhw/telnet_client.py（stdlib-only）
IAC, DONT, DO, WONT, WILL, SB, SE = 255, 254, 253, 252, 251, 250, 240
class TelnetClientCodec:
    def feed(self, data: bytes) -> tuple[bytes, bytes]       # (應用資料, 應回覆的協商 bytes)
    @staticmethod
    def encode(data: bytes) -> bytes                          # 0xFF → 0xFF 0xFF
    # 協商：WILL {ECHO,SGA,BINARY} → 首次回 DO；其他 WILL → DONT；DO SGA → WILL SGA；其他 DO → WONT；
    #       DONT/WONT 不回覆；SB…IAC SE 整段丟棄；IAC IAC → 資料 0xFF；其他 IAC 指令丟棄；同一 option 同一回覆只送一次
```

**runner 變更**

- 新參數：`--mode pty|bridge|both`（預設 `pty`，維持既有行為）、`--dut-mode local-process|docker`（預設 `local-process`）、`--report-dir DIR`（預設 `~/b-log/func-test-reports/<%y%m%d-%H%M%S>`）、`--selftest-report PATH`（bridge 模式必填；`is_qualified(...)` 為 False 時所有 bridge 結果的 `gate_eligible=false`，最終 verdict 不得為 PASS，改記 `INSUFFICIENT_EVIDENCE`）、`--scenarios`（同時載入 `func-test/scenarios/`，Task 11）。
- 每個 case×mode×repeat 一個證據目錄 `<report-dir>/<case_id>/<mode>/<run_index>/`：`manifest.json`（`l15-manifest.v1`）、`cli.ndjson`（每次 CLI 呼叫的 argv、rc、stdout、stderr 原樣）、`daemon.log`（daemon stdout/stderr，不再丟 DEVNULL）、`wal/`（隔離 WAL 目錄複本：harness stop 後複製）、bridge 模式另有 Task 5 的證據檔、`correlation.ndjson`、`comparison.json`。
- `func-test-report.json`（`func-test-report.v1`）：`{"schema_version","started_at","modes":{"pty":{"runs","pass","fail","insufficient"},"bridge":{...}},"independent_cases":<case 數>,"results":[{"case_id","mode","run_index","verdict","message","evidence_dir","comparison_verdict","gate_eligible"}]}`。**不同模式的執行次數與獨立 case 數分開統計**（§15.5）；exit 1 若任一 verdict ≠ PASS。
- bridge 模式下動作映射：`target_emit` → `harness.target_emit`（`dut_control emit`）；`target_stop_responding` → `harness.pause_target`；`inject_device_event` → `harness.device_event`（`by_id_link_remove`／`by_id_link_add`，與 pty 模式同為「只移除 symlink，不關 PTY」）；`attach_console` 在 Windows（`attach` 回應 `transport=="tcp"`）改用 `TelnetConsole(endpoint)`，`client_id` 以連線前後 `session console-list` 差集取得。其餘動作不變。
- 每個 step 前後寫 correlation：`op_id=f"s{index:03d}-{action}"`；snapshot 一律 `settle=False`；`wait_ready` 完成後與最後一個 step 之後各做一次 `settle=True`（op `__settle_start__`、`__settle_end__`）。case 結束時依 `bridge.compare` 執行比較器：`full`（窗口＝`__settle_end__`）、`until_op:<op_id>`、`none`。`bridge.secrets_from_env` 列出的 env 值傳入 `secrets` 並啟用 `secret_tx_unlogged`。
- correlation 的 settle 規則補充：`bridge.compare` 為 `until_op:<op_id>` 時，runner 在該 op 的 `op_start` 一律以 `settle=True` 取快照（其餘 step 仍 `settle=False`），否則窗口必為 `HARNESS_WINDOW_UNSETTLED`；`op_id` 的自動命名與 Task 11 顯式 `op_begin` 共用同一命名空間（重複即 `StepError`）。DUT 開 noise 的 case（`sm-01`、`ha-05`）以 `quiesce=True` 建 `BridgeSession`。bridge 存活期間的 step 斷言（Task 11 `assert_compare`／`assert_zero_tx`）先呼叫 `BridgeSession.checkpoint()`，以 checkpoint 驗證；case 結束時的最終比較在 `stop()`（shutdown manifest）之後重跑。
- 新增內建變數 `{run.root}`（harness root）供 case 使用；`_expand` 也套用在 `shell.cmd`、`wait_for_file.path` 與新動作 `write_file{path,content}`（跨平台寫檔，取代 `cat <<'JSON'`／`touch`）；`wait_state{selector,state,timeout_s=15,accept=[state]}` 新動作（每 0.3 s 查 `session list`，狀態屬於 `accept` 即通過）。`yaml_loader.TestCase` 新增 `bridge: dict` 與 `scenario: dict | None`（`meta.scenario`）。

**Windows 接線契約（bridge 模式，`os.name=="nt"`；OD-08、SC-19）**

| 項目 | 規則 |
|---|---|
| 端點 | `--daemon-com COMa --bridge-com COMb --transport-mode virtual_com0com\|physical_usb_uart`（runner 參數；缺任一 → exit 2）。com0com：COMa/COMb 為 DUT-side pair 兩端；實體：COMa 為 Windows 端 USB-UART、COMb 為另一條 USB-UART，兩者 TX/RX 交叉直連（不經 usbipd） |
| profile 綁定 | harness 寫的 profile `targets` 以 `device_by_id: COMa` 綁定（Windows by-id＝COM 名，P1 T13 `filter_allowlist` 比對對象）；不建 symlink |
| 隔離實例 | `SERIALWRAP_TEST_DEVICE_ALLOWLIST=COMa`，且 `SERIALWRAP_STATE_DIR`／`_RUN_DIR`／`_CONFIG_DIR` 皆顯式指向 harness root（OD-11 隔離實例條件）；daemon 啟動後 `daemon status` 須回 `test_device_allowlist.enabled:true` 且 `coms==["COMa"]`，否則 harness 停止並 `StepError` |
| RPC | `rpc_endpoint=tcp://127.0.0.1:<free_port()>`：daemon 以 `--socket <rpc_endpoint>` 啟動（`sw_core/daemon.py:42` 預設值即 endpoint 字串）；CLI 以 `--endpoint <rpc_endpoint>`（`sw_core/cli.py:1923`，優先於 `--socket`）呼叫；`SERIALWRAP_ENDPOINT` 已在隔離時刪除；`wait_endpoint` 以 TCP connect probe 判定 |
| bridge | `BridgeSession(endpoint_spec={"kind":"win-serial","com":"COMb","transport_mode":...})`；`device_event`／`transport_disconnect` 回 `cap_missing:by_id_link`／`cap_missing:transport_disconnect`（case 結果 SKIP，`reason_code` 照登記） |
| 逐案例能力 | 13 case 的 `bridge.windows_caps`：`sm-04` 需 `by_id_link` → Windows SKIP（`cases.yaml` 標 gap）；console 類 case 走 `TelnetConsole`；其餘只需 `windows_com` |

**oracle 修復（`TST-D-9`；ID、目的、預期結果不變）**

| case | 修復 |
|---|---|
| `hk-01-special-keys-raw` | `assert_state` 的 `has_interactive`：runner 以 `session console-list --selector S` 計算 `any(c["interactive_owner"] for c in consoles)` 後注入比對；`console_write.input_raw` 實作為逐字元 `ord(c) ≤ 0xFF` 轉 bytes（超過即 `StepError`）；`assert_wal.any_tx_contains_raw` 改為 list：`["\x1b[A","\x1b[B","\x09"]`，搭配 `tx_source_prefix: "human:"`，每項須為 TX（來源前綴相符）payload 串接後的子字串 |
| `hk-02-agent-preempt-human` | YAML L58 的 `cmd get`（不存在的子命令；有效子命令只有 `submit`／`status`／`result-tail`／`cancel`）改為 `cmd status --cmd-id {submit1.cmd_id}`，`expect` 改為 `command.status: done` |
| `hk-03-deferred-input-flush` | 現行因 `input_raw` 空轉而**假性 PASS**；`input_raw: "echo deferred\r"` 真正送出；新增 `assert_wal any_tx_contains_raw: ["echo deferred\r"]`（`tx_source_prefix: "human:"`），維持原描述「deferred input 於 agent 命令後 flush」 |
| `hk-04-minicom-exit-cleanup` | `has_interactive` 同 hk-01 的計算方式；YAML L62 的 `cmd get` 同 hk-02 改為 `cmd status --cmd-id`、`expect command.status: done` |
| `cowork-01-agent-bad-cmd` | (1) `boot_banner: "login: "` 永遠不符 `prompt_regex '^\$ '`，session 停在 `ATTACHED`／`PROMPT_UNAVAILABLE`：改為 `boot_banner: "boot done\r\n$ "`；(2) `commands` 下的命令移入 `commands.overrides`（`{"bad-hang":{response:"partial output..."}, "echo ok":..., "ls":..., "echo after-recover":{response:"after-recover\r\n$ "}}`）；`submit_command`／`wait_command` 改為既有 `cli`（`cmd submit --selector COM0 --source agent:stress-test --cmd bad-hang --cmd-timeout 5`，`save_as`）＋`wait_command_done`＋`cli cmd status` 的 `expect`（`command.status: error`、`command.error_code: PROMPT_TIMEOUT`）；(3) PROMPT_TIMEOUT 後 session 落 `ATTACHED`，下一個 `cmd submit` 會得 `SESSION_NOT_READY`：壞命令與好命令之間插 `wait_ready timeout_s: 40`；刪除 runner 不讀的 `profile.login`。此變體已由審查者在 Python 基線實跑轉綠 |
| `sm-04-device-unplug` | `inject_device_event remove` 之後新增 `wait_state{state: DETACHED, timeout_s: 15}`；原 `daemon status ok` 斷言保留 |
| `ev-01-spawn-handler` | `/tmp/sw-ev-test-rule.json`、`/tmp/sw-ev-test-fired` 改為 `{run.root}/ev-rule.json`、`{run.root}/ev-fired`（EVENTS_DIR 已由 harness 隔離，不再寫 live `~/.serialwrap/events.d`）；規則檔以 `write_file` 寫出（取代 `shell: cat <<'JSON'`），handler 由 `touch` 改為跨平台 `{python} -c "import pathlib,sys; pathlib.Path(sys.argv[1]).touch()" {run.root}/ev-fired`（`{python}` 為 runner 內建變數＝`sys.executable`），使 Windows 可執行 |

13 個 case 的 `bridge:` 區塊：`sm-04-device-unplug` 為 `{applicable: true, compare: "until_op:s002-inject_device_event", windows_caps: [by_id_link]}`（拔除後 daemon 關埠，之後的 bytes 不再可比）；`brcm-01-login-flow` 為 `{applicable: true, compare: full, secrets_from_env_file: {file: brcm.env, keys: [BRCM_PASS]}}`（`BRCM_PASS` 只在 case 的 `env_files.brcm.env` 內，不在 runner 環境變數；runner 解析該 env_file 內容取值）；其餘 11 個為 `{applicable: true, compare: full}`。

**oracle 修復程序（§10.4 測試資產變更）**：修 YAML 前先以未修改的 case 跑 `python3 func-test/runner.py`（Task 4 已保存的 `functest-pty-before.txt`：17 次執行 13 pass／4 fail）作 pre-repair 對照證據；7 個 case 的 oracle 修復單獨一個 commit（`test(func-test): 修復 7 個失效或空轉的 func-test oracle`），由非實作者審閱、在 PR 描述逐 case 列「修前結果／修後結果／修復理由」。

### T 步驟

- [ ] **T10-1：撰寫失敗測試**。`tests/test_functest_harness.py`（不啟 daemon；以 monkeypatch 攔截 `subprocess.Popen` 取得 argv／env）：

```python
def test_harness_isolates_nine_dirs_and_strips_all_serialwrap(monkeypatch, tmp_path):
    from lib import daemon_harness                       # 函式內 import（§1 第 3 點）
    captured = {}
    monkeypatch.setenv("SERIALWRAP_ENDPOINT", "unix:///live.sock")
    monkeypatch.setenv("SERIALWRAP_PROFILE_DIR", "/live/profiles")
    monkeypatch.setenv("SERIALWRAP_TEST_DEVICE_ALLOWLIST", "COM9")
    monkeypatch.setenv("SERIALWRAP_TCP_PORT", "1")
    h = daemon_harness.DaemonHarness(daemon_harness.HarnessConfig(mode="pty"))
    monkeypatch.setattr(daemon_harness.subprocess, "Popen", _capture_popen(captured))
    monkeypatch.setattr(h, "wait_endpoint", lambda *_a, **_k: None)
    h.start()
    try:
        env = captured["env"]
        for key, sub in daemon_harness.ISOLATED_DIR_ENV.items():
            assert env[key] == str(h.root / sub), key
            assert pathlib.Path(env[key]).is_dir()
        leaked = {k for k in env if k.startswith("SERIALWRAP_")} - set(daemon_harness.ISOLATED_DIR_ENV)
        assert leaked == set(), leaked
        assert env["XDG_CONFIG_HOME"].startswith(str(h.root)) and "XDG_RUNTIME_DIR" not in env
    finally:
        h.stop()
```

其餘：

| 測試名（檔案） | 斷言 |
|---|---|
| `test_cli_exe_env_override`（harness） | `SERIALWRAP_CLI_EXE=/x/serialwrap` → `resolve_cli_exe()==["/x/serialwrap"]`；未設 → `[str(ROOT_DIR/"serialwrap")]` |
| `test_daemon_exe_env_override` | `SERIALWRAP_DAEMON_EXE=/x/serialwrapd` → daemon Popen argv 為 `["/x/serialwrapd","--profile-dir",...,"--socket",...,"--lock",...]` |
| `test_daemon_log_not_devnull` | Popen 的 stdout／stderr 指向 `evidence_dir/"daemon.log"` 的檔案物件 |
| `test_artifact_identity_python_baseline` | 預設 argv 時 `collect_artifacts(...)["daemon"]["implementation"]=="python"`，`package_tree_sha256`、`repo_tree_sha256` 皆為 64 hex 且不同；CLI `version_stdout` 以 `serialwrap ` 開頭 |
| `test_deployed_content_sideload_changes_tree`（deployed_content；R5-F4，failing-first） | `tmp/"v1"` 建最小安裝：`bin/serialwrap`（`b"cli"`）、`release-manifest.json`（`components.cli.sha256 = sha256(b"cli")`）；`a = measure(tmp/"v1"/"bin"/"serialwrap")` → `error is None`、`install_dir == str(tmp/"v1")`、`release_manifest_sha256 == sha256_file(manifest)`、兩個 tree 皆 64 hex 且不同。`copytree` 成 `tmp/"v2"` 後寫入 `bin/version.dll`（manifest 與 `bin/serialwrap` 位元組不變）→ `b = measure(...v2...)`：`b["release_manifest_sha256"] == a["release_manifest_sha256"]`、`b["cli_sha256"] == a["cli_sha256"]`，但 `b["bundle_tree_sha256"] != a["bundle_tree_sha256"]` 且 `b["runtime_tree_sha256"] != a["runtime_tree_sha256"]` |
| `test_deployed_content_algorithm_vector`（deployed_content） | `tmp` 下 `release-manifest.json`(`b"m"`)、`SHA256SUMS`(`b"s"`)、`bin/x`(`b"x"`)：`tree_digests(tmp)[0] == sha256(("SHA256SUMS\0"+h(b"s")+"\n"+"bin/x\0"+h(b"x")+"\n"+"release-manifest.json\0"+h(b"m")+"\n").encode())`、`tree_digests(tmp)[1] == sha256(("bin/x\0"+h(b"x")+"\n").encode())`（P9 Task 14B `test_producer_tree_algorithm_matches_task3` 另以 P9 Task 3 實作交叉對拍） |
| `test_deployed_content_rehashes_preserved_mtime`（deployed_content） | 兩次 `measure` 之間以 `os.utime` 保留原 mtime 改寫 `bin/version.dll` 內容 → 兩次 `bundle_tree_sha256` 不同 |
| `test_deployed_content_errors_null_digests`（deployed_content） | CLI 不在含 `release-manifest.json` 的安裝內 → `error == "unresolved"`、三個 digest 皆 None；改寫 `bin/serialwrap` 使 sha 不符 manifest → `"cli_mismatch"`、digest 皆 None；安裝內放 symlink（`posix` 時；Windows 以 monkeypatch `tree_digests` 拋錯）→ `error` 以 `"tree_invalid: "` 開頭、digest 皆 None |
| `test_deployed_content_daemon_outside_install_flagged`（deployed_content；R5 審查後續，failing-first） | 最小安裝 `tmp/"v1"`（manifest `components.daemon.launcher={path:"bin/serialwrapd",sha256}`）；`measure(cli, daemon_argv=[str(tmp/"repo"/"py-serialwrapd")])` → `error == "daemon_outside_install"`、digest 皆 None；`daemon_argv=[str(v1/"bin"/"serialwrapd")]` 但內容改寫 → `"daemon_mismatch"`；內容相符 → `error is None`、`daemon_sha256 == launcher.sha256`；`daemon_argv=None` → `error is None` 且 `daemon_resolved is None` |
| `test_deployed_content_live_daemon_image`（deployed_content；R5 審查後續） | 以 `subprocess.Popen([sys.executable, "-c", "import time; time.sleep(30)"])` 的 pid 呼叫 `live_daemon(pid, install=tmp)` → `image_path == realpath(sys.executable)`、`in_install is False`；不存在的 pid → `error == "image_unavailable"`；Windows 分支以注入假 `ctypes` kernel32 回 `C:\\i\\py\\python.exe` 驗證 |
| `test_deployed_content_windows_stable_bin_sidecar`（deployed_content；B5，R5 審查後續，failing-first） | monkeypatch `os.name="nt"`，建 Windows 佈局：`root/bin/{serialwrap.exe,serialwrapd.exe,current-release.json}`（複本與 `versions/0.4.0/bin/` 同內容）＋`versions/0.4.0/release-manifest.json` → `error is None`、`entry_dir_sha256` 為 64 hex；在 `root/bin/` 加 `version.dll`（`versions/0.4.0` 不變、manifest 不變）→ `error == "entry_dir_extra: version.dll"`；改寫 `root/bin/serialwrapd.exe` 一個 byte → `"entry_copy_mismatch: serialwrapd.exe"` |
| `test_functest_manifest_records_deployed_bracket`（harness；R5-F4） | monkeypatch `realhw.deployed_content.measure` 依呼叫序回 `{"bundle_tree_sha256":"a"*64,…}`、`{"bundle_tree_sha256":"b"*64,…}`：runner 跑一個 case 後 `manifest.json["artifacts"]["deployed_content"] == {"start": <第 1 次>, "end": <第 2 次>}`；`measure` 第 1 次呼叫早於 daemon `Popen`、第 2 次晚於 `harness.stop()`（以呼叫序記錄斷言）；兩次的引數皆為 `resolve_cli_exe()[0]` |
| `test_artifact_env_names_popped_by_repo_conftest` | `tests/conftest.py` 的 pop 清單包含 `SERIALWRAP_CLI_EXE`、`SERIALWRAP_DAEMON_EXE`（arch §9.2：產品不讀、測試隔離時清除；若 P3 尚未加入，本 Task 一併加入） |
| `test_bridge_mode_maps_device_event_to_link_ops` | bridge 模式以 fake `BridgeSession` 記錄呼叫：`device_event("remove")` → `call("by_id_link_remove")`；`target_emit(b"x")` → `call("dut_control",{"msg":{"action":"emit","payload_b64":"eA=="}})`；`pause_target(5)` → `pause_responding` `duration_ms=5000` |
| `test_settle_snapshot_retries_until_stable` | fake bridge 前兩輪 b1≠b2、第三輪相等 → `settled True`、`wal_seq` 取第三輪；十輪皆不等 → `settled False` |
| `test_mode_stats_separate`（oracles） | runner 對 2 case × 2 mode × repeat 2 產生報告：`independent_cases==2`、`modes.pty.runs==4`、`modes.bridge.runs==4` |
| `test_unqualified_bridge_never_pass` | `--selftest-report` 指向 `tool.tree_sha256` 不符的報告 → bridge 結果 `gate_eligible False` 且 verdict `INSUFFICIENT_EVIDENCE` |
| `test_has_interactive_computed_from_console_list` | 以 stub CLI 回 `consoles:[{"interactive_owner":false},{"interactive_owner":true}]` → `has_interactive` 為 True；空 list → False |
| `test_input_raw_latin1_bytes` | `input_raw: "\x1b[A\xff"` 寫出 `b"\x1b[A\xff"`；含 `"中"` → `StepError` |
| `test_any_tx_contains_raw_list_and_prefix` | WAL records：human TX `G1tB`（`\x1b[A`）、agent TX `CQ==`（`\t`）；`["\x1b[A"]`＋prefix `human:` PASS；`["\t"]`＋prefix `human:` FAIL |
| `test_case_yaml_ids_and_descriptions_unchanged` | 13 個 case 的 `meta.name`、`meta.description`、`meta.category`、`meta.severity` 與 `git show 93553e6:func-test/cases/<f>` 相同；`hk-01` 不再有重複 key（以自訂 loader 偵測重複 key） |
| `test_all_cases_declare_bridge_block` | 13 個 case 皆有 `bridge.applicable`；`compare` ∈ {`full`,`none`} 或 `until_op:` 前綴 |
| `test_runner_knows_all_case_actions` | 13 個 case 所用 action 全在 runner 動作表內（`cowork-01` 不再有 `submit_command`）；所有 `cli argv` 的 `cmd` 子命令 ∈ {`submit`,`status`,`result-tail`,`cancel`}（`hk-02`／`hk-04` 不再有 `cmd get`） |
| `test_cowork01_ready_reachable` | `cowork-01` 的 `boot_banner` 最後一行符合其 `profile.prompt_regex`；壞命令（`bad-hang`）之後、下一個 `cmd submit` 之前有 `wait_ready` |
| `test_codec_negotiation`（telnet_client） | 餵入 server greeting `FF FB 01 FF FB 03 FF FD 03 FF FB 00`（盤點 06 §8.4）→ 應用資料空、回覆為 `FF FD 01 FF FD 03 FF FB 03 FF FD 00`；再餵同 greeting → 回覆空（不重複） |
| `test_codec_strips_sb_and_unescapes` | `b"a\xff\xfa\x18\x01\xff\xf0b\xff\xffc"` → 資料 `b"ab\xffc"` |
| `test_codec_split_across_feeds` | 同一序列逐 byte 餵入 → 結果相同 |
| `test_encode_escapes_iac` | `encode(b"\xff\x00")==b"\xff\xff\x00"` |
| `test_telnet_console_against_fake_server` | 本機 fake server（送 greeting、記錄收到 bytes、回送固定資料）：`TelnetConsole.write(b"ls\r\n")` 後 server 收到協商回覆＋`b"ls\r\n"`；`read` 得去除指令後的資料 |
| `test_bridge_smoke_sm01`（smoke；`posix_only`、`posix_pty`、`process_rpc`） | `python3 func-test/runner.py --mode bridge --case sm-01-attach-to-ready --selftest-report <Task 8 產出的暫存報告> --report-dir tmp` exit 0；`sm-01` 的 DUT 持續 noise（100 ms），correlation 的 `__settle_start__`／`__settle_end__` 皆 `settled:true`（quiesce）；`comparison.json` verdict PASS；`manifest.json` 以 `l15_manifest.schema.json` 驗證通過 |
| `test_bridge_smoke_sm04_until_op`（smoke；同上 marker） | `--case sm-04-device-unplug`：correlation 中 `s002-inject_device_event` 的 `op_start` 為 `settled:true`；`comparison.json` 的 `window.until_op` 等於 `s002-inject_device_event` 且 verdict PASS |
| `test_lazy_fake_target_import` | 子程序在 `sys.modules['pty']=None; sys.modules['termios']=None` 下 `import runner`（`func-test/`）與 `from lib import daemon_harness` 皆成功（Windows 可載入；`fake_target` 只在 pty 模式 lazy import） |
| `test_windows_wiring_env_and_argv`（windows_wiring；monkeypatch `os.name="nt"`、攔截 Popen） | `HarnessConfig(mode="bridge", daemon_com="COM7", bridge_com="COM8", transport_mode="virtual_com0com")`：daemon env 含 `SERIALWRAP_TEST_DEVICE_ALLOWLIST=="COM7"`，`SERIALWRAP_STATE_DIR`／`_RUN_DIR`／`_CONFIG_DIR` 皆在 harness root 下；daemon argv 的 `--socket` 值以 `tcp://127.0.0.1:` 開頭、CLI argv 含 `--endpoint` 同值；profile YAML `targets` 綁 `COM7`；bridge argv 含 `--endpoint win-serial --com COM8 --transport-mode virtual_com0com`；不建立任何 symlink |
| `test_windows_wait_endpoint_tcp_probe` | `wait_endpoint` 對 tcp endpoint 以 connect probe 判定：本機 listen 中 → 回傳；無 listener → 逾時 raise `TimeoutError`（本函式標 `process_rpc`） |
| `test_windows_allowlist_not_enabled_stops` | stub `daemon status` 未回 `test_device_allowlist.enabled:true` → `start()` raise，訊息含 `OD-11` |
| `test_windows_device_event_cap_missing` | Windows bridge 模式 `device_event("remove")` → `StepError`，訊息含 `cap_missing:by_id_link` |
| `test_brcm_secret_from_env_file` | `brcm-01` 的 `secrets_from_env_file` 解析出 `b"admin"` 傳入比較器 `secrets`；runner 環境變數中無 `BRCM_PASS` 亦可 |
| `test_expand_in_shell_and_wait_for_file` | `shell.cmd: "echo {run.root}"`、`wait_for_file.path: "{run.root}/x"`、`write_file.path` 皆展開為 harness root |
| `test_tools_docker_reads_artifact_env` | `SERIALWRAP_CLI_EXE=/x/sw` 時 `tools/docker/uart_harness.py` 與 `remote_lab.py` 的 CLI 解析回 `/x/sw`；兩檔 AST 不再 `from lib.fake_target import`（改 `lib.target_config`） |
| `test_report_never_embeds_payload` | `func-test-report.json`、`comparison.json` 不含任何 `payload_b64` 鍵，且不含 case 的 secret 字串 |

- [ ] **T10-2：執行並確認 FAIL**：`python3 -m pytest -q tests/test_functest_harness.py tests/test_functest_oracles.py tests/test_telnet_client.py tests/test_functest_bridge_smoke.py tests/test_functest_windows_wiring.py tests/test_deployed_content.py` →（`test_deployed_content_*` 以 `ModuleNotFoundError: realhw.deployed_content` 失敗） 函式內 import 的屬性／模組不存在、case YAML 斷言失敗（`pytest -q tests/` 仍可完整收集）。
- [ ] **T10-3：commit 測試**：`git commit -m "test(func-test): 新增外部 bridge 模式、隔離、oracle 修復與 Telnet client 失敗測試"`。

### I 步驟

- [ ] **I10-1：實作**（10A → 10B）：上列檔案；`cli_client.cli_run` 改用 `resolve_cli_exe()`；`runner.py` 與 `daemon_harness.py` 改 import `lib.target_config`，`fake_target` 僅 pty 模式 lazy import；`runner._wait_for_socket` 刪除改呼叫 `harness.wait_endpoint()`；`yaml_loader.TestCase` 新增 `bridge: dict`（預設 `{"applicable": False, "compare": "none"}`）與 `scenario`；runner 新參數 `--daemon-com`、`--bridge-com`、`--transport-mode`（Windows 接線契約）；`README.md` 補 `--mode`、`--selftest-report`、Windows 接線、證據目錄、`shell`／`target_emit`／`wait_for_file`／`write_file`／`wait_state` 動作說明（R-18）。修 13 個 case YAML 照上表（oracle 修復依「oracle 修復程序」獨立 commit）。
- [ ] **I10-2：執行並確認 PASS**：上列 pytest 全綠；先確認 `is_qualified(<Task 8 報告>, approved=load_approved_revision(), ...)` 為 True（工具行為檔自 Task 8 後若有變動，先重新審閱並重跑 `selftest`）；`python3 func-test/runner.py --mode both --selftest-report ~/b-log/rust-refactor/p2/selftest-linux/selftest-report.json` 對修正後 Python 基線執行，保存 `func-test-report.json`（此時 `cases.yaml` 尚未建立，`cases_yaml_sha256:null`、`gate_eligible:false`，屬開發執行；正式證據為 §3 的執行）；任何非 PASS 依 §15.10 分診：先排除測試資產缺陷（與 pre-repair 基線對照），確認屬產品缺陷才另開 issue，報告保留原結果。
- [ ] **I10-3：commit**：`git commit -m "feat(func-test): 新增外部 bridge 模式、artifact 選定、Windows 接線與證據目錄"`＋fragment（oracle 修復已於獨立 `test(func-test)` commit）。

---

## Task 11：§15.7.1 情境腳本與協調器新動作

**Test author model:** haiku-xhigh　**Implementer model:** agy-gemini-3.8-flash-high
**Changelog fragment:** `changelog.d/rust-p2-l15-scenarios.md`（`type: feat`、`scope: func-test`）
**BLOCKED-OWNER evidence:** 無（OD-15 起改為可執行 gate）。
**擁有者 host 可執行 gate（OD-15，G6）：**
- 同一組情境在 Windows（`SW_TEST_COM_PAIR=COM20:COM21`，Task 10「Windows 接線契約」）上的執行結果；`l15-transport-disconnect` 在 com0com 上只有 peer close 能力時須登記 SKIP（`cap_missing:transport_disconnect`），不得 PASS；`assert_peer_closed` 在 Windows 為 `cap_missing:peer_state`，直到 Task 18 peer-close 實測結果寫回能力表。
- 三個 `win-com-*` 情境在擁有者 host 的實跑結果（orchestrator 經 interop 代跑；自架 GitHub runner 註冊與否不影響本 gate）。

**Files:**
- Create（15 個 L1.5 情境）：`func-test/scenarios/l15-delayed-reply.yaml`、`l15-half-prompt.yaml`、`l15-dut-silent.yaml`、`l15-slow-reader-backpressure.yaml`、`l15-dut-reboot-port-alive.yaml`、`l15-transport-disconnect.yaml`、`l15-multi-source-contention-cli.yaml`、`l15-multi-source-contention-console.yaml`、`l15-stale-event-delay.yaml`、`l15-wal-interference.yaml`、`l15-reject-zero-tx.yaml`、`l15-release-inflight.yaml`、`l15-exclusive-open.yaml`、`l15-reboot-min-guard.yaml`、`l15-context-switch-deferred-input.yaml`
- Create（3 個 Windows-native 黑箱情境，`platforms:[windows]`）：`func-test/scenarios/win-com-open-exclusive.yaml`、`win-com-txrx-bridge.yaml`、`win-com-cancel-close.yaml`
- Modify: `func-test/runner.py`（新動作）、`func-test/lib/yaml_loader.py`（`meta.scenario` 欄位驗證）
- Create: `tests/test_l15_scenarios.py`（模組層 `pytestmark=[pytest.mark.core_portable]`；結構檢查函式 `memory_only`，`test_reject_zero_tx_runs_on_python` 改標 `posix_only` 不可行（T14 第 2 條：模組層有平台 marker 時子層不得再加）→ 該 smoke 移到 `tests/test_l15_smoke.py`，模組層 `posix_only`＋`posix_pty`＋`process_rpc`）

**Interfaces:**

情境 YAML 在既有 case 格式上增加 `meta.scenario`（§15.7.1：固定 ID、revision、seed、前置能力、觸發條件、timeout、故障動作、預期不變量）：

```yaml
meta:
  name: l15-reject-zero-tx              # == 檔名 stem == cases.yaml case_id
  category: l15-scenario
  severity: critical
  description: "含換行的命令被拒絕時，外部 serial_read 與 WAL TX 皆為零"
  scenario:
    revision: "2026-10-15.1"
    seed: 1015
    capabilities: [posix_pty, process_rpc]     # windows：[windows_com, process_rpc]
    timeout_s: 60
    invariants: [zero_tx_on_reject, insufficient_evidence_not_pass]
    behavior_changes: []
    expected: PASS
bridge: {applicable: true, only: true, compare: full}
```

新動作（只在 bridge 模式可用；pty 模式遇到 → `StepError("bridge-only action")`）：

| action | 參數 | 行為 |
|---|---|---|
| `op_begin` / `op_end` | `op_id`、`settle`（預設 true） | 寫 correlation `op_start`／`op_end`，附 snapshot |
| `bridge_control` | `action`、`params` | `BridgeSession.call`；`expect_error` 可指定預期 `HARNESS_*`（如能力不足） |
| `dut_control` | `msg` | `bridge_control dut_control` 的簡寫 |
| `wait_wire_marker` | `marker`、`observation`（預設 `serial_read`）、`timeout_s` | `WireTail.find`；逾時 `StepError` |
| `assert_zero_tx` | `op_id` | `check_zero_tx`；PASS 才通過，INSUFFICIENT → case 結果 `INSUFFICIENT_EVIDENCE` |
| `assert_wire_contiguous` | `payloads`（list[str]，latin-1） | `check_contiguous` |
| `assert_compare` | `until_op`（可省） | 立即執行 `compare`，非 PASS 即失敗 |
| `wal_control` | `op`: `unlink_current` \| `hold_reader` \| `reset`；`seconds` | `unlink_current`：`os.unlink(<wal_dir>/raw.wal.ndjson)`；`hold_reader`：以預設 share mode 開檔持有 `seconds` 秒（背景 thread）；`reset`：CLI `wal reset` |
| `assert_wal_meta` | `contains_value`、`loss_flag`、`has_keys` | 讀隔離 WAL 目錄所有檔；存在一筆 `dir=="META"` 且 `meta` 任一值等於 `contains_value`、`loss_flag` 相符、`has_keys` 全在 `meta` |
| `assert_wal_marker` | `marker`、`dir`（`TX`/`RX`） | 讀 WAL 檔（含歸檔）找 payload 含 marker 的紀錄，且 `crc32`、`len` 驗證通過（§8.3 精神） |
| `assert_command_resolved` | `from`（某個 `save_as` 名）、`timeout_s`、`payload` | 見 `l15-transport-disconnect` 範例下方說明：被接受者須到終態且 wire 出現 ≤1 次；被拒者須有 `error_code` 且 wire 出現 0 次 |
| `assert_wire_absent` | `marker`（latin-1）、`after_op`、`observation`（預設 `serial_read`） | `after_op` 的 `op_end` 快照之後（至 case 結束或下一個 `checkpoint`）的 wire 該 observation 串流不得含 `marker`；先 `checkpoint`，快照未 settled → `INSUFFICIENT_EVIDENCE`。供 `no_stale_context_replay`：舊上下文的輸入／排隊命令在上下文切換後不得送到 UART |
| `assert_peer_closed` | `at_op`、`until_op` | 只在 POSIX（`endpoint.capabilities().peer_state`）可用：`at_op` 的 `op_end` 快照所附 bridge `status.peer_open`（即時計算）必須為 false（證明 release 回覆時 handle 已關閉）；且在 `at_op.op_end` 與 `until_op.op_start` 之間 wire 不得有 META `peer_state{open:true}`（證明交接完成前未重開端點）。Windows → case 結果 SKIP `cap_missing:peer_state`（不得 PASS） |
| `hold_endpoint` | `mode`：`open`／`flock`／`excl`；`seconds`；`save_as` | 以測試替身身分開受測端 A（POSIX：`slave_path`；Windows：`daemon_com` 以 pySerial `exclusive=True`）。`flock`：取 `LOCK_EX\|LOCK_NB`；`excl`：另設 `TIOCEXCL`。回 `{"opened":bool,"error":str\|null}`；背景持有 `seconds` 秒後釋放，或以 `release_endpoint{from}` 提前釋放 |
| `release_endpoint` | `from` | 釋放 `hold_endpoint` 取得的 handle；POSIX `mode:excl` 時先 `ioctl(TIOCNXCL)` 再 `LOCK_UN`、`close`（TIOCEXCL 是 tty 物件旗標，bridge 持有 PTY master 時不隨 close 清除；漏清會讓之後的 `device attach` 永遠 EBUSY，R0-F3） |
| `write_file` | `path`、`content` | Task 10 新增；跨平台寫檔（`{run.root}` 展開） |

**不變量執行者（`INVARIANT_ENFORCERS`，runner 常數；`tests/test_l15_scenarios.py` 檢查）**：每個情境 `meta.scenario.invariants` 宣告的每一項，必須在該情境 steps 中至少出現一個對應的執行者動作，否則「只貼標籤」：

| invariant | 執行者動作 |
|---|---|
| `zero_tx_on_reject` | `assert_zero_tx` |
| `no_forbidden_interleave` | `assert_wire_contiguous` |
| `no_stale_context_replay` | `assert_wire_absent` |
| `no_reopen_before_handoff` | `assert_peer_closed`（POSIX）；Windows 情境改以 `hold_endpoint` 在 release 回覆後立即成功取得獨占作為「handle 已關閉」證據 |
| `no_auto_resend` | `assert_command_resolved` 或 `assert_wire_contiguous`（payload 恰一次） |
| `insufficient_evidence_not_pass` | `assert_compare`、`assert_wal_meta{loss_flag:true}` 或 `assert_zero_tx`（三者的 INSUFFICIENT 不得轉 PASS） |

無法由外部決定性製造的部分（delayed-close、drain 3,000 ms 逾時下 release 回 `DRAIN_STALLED`＋`drain_stalled`（OD-14 #4 新碼，取代原 `SESSION_RECOVERING`；drain 中的 attach／clear／bind／recover 仍回 `SESSION_RECOVERING`）、舊 worker 晚到 completion 不改新 session 狀態）不在 L1.5 假造，明確連到 P1 的 L1 測試入口：`02-stage0-python-bc.md` T07（`DrainReceipt`）、T08（release／attach 交接回應、drain 診斷）、T20（OS write 前授權 permit），以及 `02b-stage0-owner-confirmations.md`（P1-OC，`DRAIN_STALLED` 回應形狀）；`no_reopen_before_handoff` 的 oracle 對 `DRAIN_STALLED` 同樣以「回覆後不得重開、回條前外部無法取得獨占」判定；P2 的 zero-TX oracle 不變（`DRAIN_STALLED` 為 `uart_tx: possible`，不列入 `zero_tx_on_reject`）；`cases.yaml` 的對應 case 以 `implemented_by: 02-stage0-python-bc.md` 登記。

**情境清單（15 個 L1.5＋3 個 Windows-native；`daemon_slice` 與 `cases.yaml` 一致）**

| case_id | 觸發與故障動作（DUT plan／bridge／協調器） | 主要 assertion | 不變量 | 切片 |
|---|---|---|---|---|
| `l15-delayed-reply` | plan `delay_reply` on `L15_DELAY_1`（delay 8000 ms）；`cmd submit --cmd "echo L15_DELAY_1" --cmd-timeout 3` | 命令以 timeout 終態結束（`cmd status` 之 `command.status == "error"` 且 `command.error_code == "PROMPT_TIMEOUT"`；Python 沒有 `timeout` 狀態值，`arbiter.py` 狀態集合為 `accepted`／`running`／`done`／`error`／`canceled`／`interactive`）；`cmd cancel` 不讓 session 卡住；之後 `echo L15_AFTER` 在 15 s 內 done；比較器 full PASS | `no_auto_resend`（`assert_wire_contiguous` 確認 `echo L15_DELAY_1\n` 只出現一次） | 2.1 |
| `l15-half-prompt` | plan `half_prompt` on `L15_HALF`（split_at 7、gap 1500 ms、inject `\x1b[0m KDBG:x\r\n`） | `wait_wire_marker`（RX `serial_write` 出現 prompt 前半）時 `cmd status` 尚未 done；後半出現後 done，`stdout` 含 `RESULT:echo L15_HALF:OK` | — | 2.1 |
| `l15-dut-silent` | plan `silent` on `L15_SILENT` | 命令依 profile timeout 結束，`error_code` 依凍結錯誤碼表（`contracts/error_codes.yaml`）屬 timeout 類；`session activity` 回報分類；下一命令可恢復或依 Stall 契約進 RECOVERING；末尾 `assert_compare`（DUT 靜默期間 bytes 仍須一致，INSUFFICIENT 不得轉 PASS） | `insufficient_evidence_not_pass` | 2.3 |
| `l15-slow-reader-backpressure` | bridge 以 `--max-inflight-bytes 16384` 啟動（`bridge.run_args`）；plan `stop_stdin` on `L15_SLOW`（resume 20000 ms）；先 `wait_wire_marker` 確認 `L15_SLOW` 已到 `dut_stdin_write`（DUT 停讀的可觀測 barrier），再提交 ≥12 個各約 16000 B（< `CMD_REJECT_BYTES`=16384）的 `echo L15_BP_<n>_<pad>` 命令，使 TX 量（≈192 KB）遠超 DUT pipe 64 KiB＋bridge 16 KiB 緩衝 | bridge `status.pending.tx` 有界（≤ 16384+4096）；受測端 PTY 寫入出現背壓（`serial_read` offset 在停讀期間停滯）；daemon 不死（每 2 s `daemon status ok`）；resume 後已接受命令全部終態、每個 payload 在 wire 恰一次且連續。`SESSION_QUEUE_FULL`（`CMD_PENDING_MAX=256`）不在本情境斷言，由 L2 `f2-queue-full-backpressure` 承接 | `no_forbidden_interleave`、`no_auto_resend` | 2.1 |
| `l15-dut-reboot-port-alive` | console attach 後輸入未完成行；`dut_control logical_reboot`（banner_delay 3000）；reboot 期間 agent `cmd submit` | wire 有 `dut_generation` 2 且 `endpoint_generation` 仍 1；reboot 前的未完成 human 輸入（marker `L15_STALE_HUMAN`）以 `assert_wire_absent{marker:"L15_STALE_HUMAN", after_op:"reboot"}` 證明在 reboot 後**未**出現在 wire TX（BC-02）；agent 命令依 Quiet Gate 回 `AUTOBOOT_QUIET`／`READY_UNCONFIRMED` 或等待後執行；session 重新 READY | `no_stale_context_replay` | 2.3 |
| `l15-transport-disconnect` | READY 後 `bridge_control transport_disconnect`；5 s 後 `transport_reconnect`（新 slave、同 link） | 期間 session 進 `RECOVERING` 或 `DETACHED`（SC-04 drain 可觀測）；重連後回 READY；新世代比較 PASS（比較器分段）；斷線期間提交的命令終態明確、未自動重送（`no_reopen_before_handoff` 由 `l15-release-inflight`、`l15-exclusive-open` 執行，本情境不宣告） | `no_auto_resend`、`insufficient_evidence_not_pass` | 2.1 |
| `l15-multi-source-contention-cli` | `parallel` 8 個 CLI 同時 `cmd submit`（不同 source，無測試端 lock），每個 2 次 | 每個命令 `echo M_<src>_<n>\n` 在 wire TX 連續出現恰一次（`assert_wire_contiguous`）；另有一個來源刻意送含換行命令，以 `op_begin`／`op_end` 包住並 `assert_zero_tx`（被拒者零 TX）；比較器 PASS | `no_forbidden_interleave`、`zero_tx_on_reject` | 2.1 |
| `l15-multi-source-contention-console` | 同上＋一個 console 同時以 `input_raw` 逐字元輸入 `echo HUMAN_X\r` | human 行與 agent 命令在 wire 不互相切斷（`assert_wire_contiguous` 對 agent payload）；suspend／resume 後 human bytes 才送出 | `no_forbidden_interleave` | 2.2 |
| `l15-stale-event-delay` | plan `delay_reply` on `L15_STALE`（6000 ms）；命令送出後立刻 `session recover`；延遲回覆在 recover 後才到 | 舊命令的晚到輸出不讓舊上下文取回寫入權：recover 之前另提交 `echo L15_STALE_Q`（仍為 `accepted` 的排隊命令；#128 recovery 會以 error 終結未啟動命令），`assert_wire_absent{marker:"L15_STALE_Q", after_op:"recover"}`；recover 後新命令正常；WAL 保留晚到 RX（`assert_wal_marker RX RESULT:echo L15_STALE`）；`assert_compare` | `no_stale_context_replay`、`insufficient_evidence_not_pass` | 2.1 |
| `l15-wal-interference` | 三段：(a) plan `flood` 70254592 bytes（> `DEFAULT_WAL_ROTATE_BYTES`=67108864）後 `assert_wal_marker TX`；(b) `wal_control unlink_current` 後送 marker；(c) `wal_control hold_reader 10` 期間送 marker；(d) `wal_control reset` | (a) 出現 `raw.wal.ndjson.<ts>` 歸檔且 seq 不重設；(b) `assert_wal_meta contains_value WAL_RECREATED_AFTER_EXTERNAL_UNLINK, loss_flag true, has_keys [potentially_lost_seq_range]`（META 細部欄位依 P1 凍結的 BC-01 契約）；(c) 只在 Windows 有意義（POSIX 的 reader handle 不阻擋 unlink／rename）：`platform==windows` 時執行並驗 marker 可讀回、CRC 正確，POSIX 記 `segment_c: not_applicable_posix`（不計 PASS）；(d) marker 可讀回且 CRC 正確；本情境 `compare: none`（含 reset 與刻意缺口） | `insufficient_evidence_not_pass` | 2.1 |
| `l15-reject-zero-tx` | 見下方完整 YAML | 見下方 | `zero_tx_on_reject` | 2.1 |
| `l15-release-inflight` | plan `stop_stdin` on `L15_INFLIGHT`（resume null）；`cmd submit --cmd "echo L15_INFLIGHT" --cmd-timeout 30`（`save_as: inf`）；`wait_wire_marker` 確認已到 `serial_read`；`op_begin release` → `device release --selector COM0` → `op_end release`；`op_begin reattach` → `device attach` → `wait_ready` | release 回覆時 `assert_peer_closed{at_op:release, until_op:reattach}`；在途命令終態明確（`assert_command_resolved`，payload 在 wire 出現 ≤1 次，release 後零自動重送）；release 回應形狀依 P1 T08 凍結契約（drain 收據欄位）；重新 attach 後 READY 且 `assert_compare` PASS | `no_reopen_before_handoff`、`no_auto_resend`、`insufficient_evidence_not_pass` | 2.1 |
| `l15-exclusive-open` | READY 後 `device release`；`hold_endpoint{mode:excl}`（外部持有者先開受測端 A 並設 `flock`＋`TIOCEXCL`）；`op_begin attach-held` → `device attach` → `op_end attach-held`；`release_endpoint`；`device attach` → `wait_ready` | release 回覆後的 `hold_endpoint{mode:excl}` 必須回 `opened:true`（POSIX：daemon 關閉 fd 前已 `TIOCNXCL`；`opened:false` 且 `error` 含 `EBUSY` 判 FAIL，不得當成外部持有，R0-F3）；被持有時 `device attach` 回 `DEVICE_STILL_HELD`（`pids` 非空或 `holder_unknown`，BC-04／SC-05），session 維持 `RELEASED`；`assert_zero_tx{op_id:attach-held}`；`assert_peer_closed{at_op:attach-held, until_op:attach-held}`（daemon 未取得端點）；外部釋放後 attach 成功 | `no_reopen_before_handoff`、`zero_tx_on_reject` | 2.1 |
| `l15-reboot-min-guard` | READY 後 `dut_control logical_reboot{pre_text:"\r\nU-Boot 2024.01\r\nHit any key to stop autoboot:  3 \r\n", banner_delay_ms:4000}`；`wait_wire_marker`（RX `serial_write` 見 `Hit any key`）後 `op_begin quiet-window`；Quiet 窗內 agent `cmd submit --cmd "echo L15_QUIET"` 兩次；`wait_state` 至 `boot_quiet_remaining_s` 為 null 後 `op_end quiet-window` | §11.6 Slice 2.1「最小 reboot 防護」：`assert_zero_tx{op_id:quiet-window}`（Quiet 窗內不論命令被拒或排隊，wire `serial_read` 與 WAL TX 皆為零）；被拒者 `error_code` 依 P1 凍結的 Quiet Gate 契約（`AUTOBOOT_QUIET`／`READY_UNCONFIRMED`）；窗後 session 回 READY | `zero_tx_on_reject`、`no_auto_resend` | 2.1 |
| `l15-context-switch-deferred-input` | console attach（raw owner）後三段，每段 human 以 `input_raw` 輸入未完成行 `L15_CTX_<seg>`（不按 Enter），同時 agent 長命令使 human 輸入進 deferred buffer；段 R（Recovery）：`session recover`；段 L（Release）：`device release`＋`device attach`；段 S（Lease 切換）：agent `session interactive-open`＋`interactive-close` | 每段以 `assert_wire_absent{marker:"L15_CTX_<seg>", after_op:<該段切換 op>}` 證明 deferred bytes 在上下文切換後未補回 UART（BC-02）；切換後新的 human 輸入照常送出（`assert_wal_marker TX`） | `no_stale_context_replay` | 2.2 |
| `win-com-open-exclusive`（Windows-native） | `platforms:[windows]`、`capabilities:[windows_com, process_rpc]`；READY 後 `hold_endpoint{mode:open}` 開 COM-A；`device release`；`hold_endpoint{mode:open}`；`op_begin attach-held` → `device attach` → `op_end`；`release_endpoint`；`device attach` → `wait_ready` | daemon 持有時外部開 COM-A 必失敗（`opened:false`，`dwShareMode=0`）；release 回覆後外部開啟立即成功（handle 已關閉）；外部持有時 `device attach` 回 `DEVICE_STILL_HELD{holder_unknown:true}`（P1 T10 映射）且 `assert_zero_tx{attach-held}`；釋放後重新取得 READY | `no_reopen_before_handoff`、`zero_tx_on_reject` | 2.1 |
| `win-com-txrx-bridge`（Windows-native） | 同上 caps；`cmd submit` 送 `echo W_<n>` 50 次；console（`TelnetConsole`）以 `input_raw` 送 `bytes(range(0x20,0x7f))` 後 Enter | 每個命令 done 且 payload 在 wire 恰一次；console bytes 在 wire TX 原樣出現；`assert_compare` full PASS（雙向 bytes 經原生 COM 與 bridge 一致） | `no_forbidden_interleave`、`insufficient_evidence_not_pass` | 2.1 |
| `win-com-cancel-close`（Windows-native） | 同上 caps；plan `stop_stdin` on `W_SLOW`；`cmd submit --cmd "echo W_SLOW" --cmd-timeout 30`（`save_as: s`）；`cmd cancel --cmd-id {s.cmd_id}`；`op_begin release` → `device release` → `op_end release`；`hold_endpoint{mode:open}`；`release_endpoint`；`device attach` → `wait_ready` | cancel 後命令終態 `canceled` 或 BC-03 中止碼（`assert_command_resolved`，payload ≤1 次、無自動重送）；release 回覆後外部可立即獨占開啟（handle 已關閉的 Windows 證據，取代不可得的 `peer_state`）；重新 attach 後 READY | `no_reopen_before_handoff`、`no_auto_resend` | 2.1 |

`func-test/scenarios/l15-reject-zero-tx.yaml` 完整內容：

```yaml
---
meta:
  name: l15-reject-zero-tx
  category: l15-scenario
  severity: critical
  description: "含換行或超長命令被拒時，外部 serial_read 與 WAL TX 皆為零（§11.3 不變量 1）"
  scenario:
    revision: "2026-10-15.1"
    seed: 1015
    capabilities: [posix_pty, process_rpc]
    timeout_s: 60
    invariants: [zero_tx_on_reject, insufficient_evidence_not_pass]
    behavior_changes: []
    expected: PASS
bridge: {applicable: true, only: true, compare: full}
target:
  platform: prpl
  boot_banner: "boot done\r\nroot@prplOS:/# "
  noise: {enabled: false}
  commands:
    default: "EXEC:{cmd}\r\nRESULT:{cmd}:OK\r\nroot@prplOS:/# "
profile:
  prompt_regex: '(?m)^root@prplOS:.*# '
  ready_probe: 'echo __READY__${nonce}'
  timeout_s: 15
steps:
  - action: wait_ready
    timeout_s: 20
  - action: op_begin
    op_id: reject-newline
  - action: cli
    argv: ["cmd", "submit", "--selector", "COM0", "--source", "agent:l15", "--cmd", "echo a\necho b"]
    expect: {ok: false, error_code: CMD_CONTAINS_NEWLINE}
  - action: op_end
    op_id: reject-newline
  - action: assert_zero_tx
    op_id: reject-newline
  - action: cli
    argv: ["daemon", "status"]
    save_as: st
  - action: op_begin
    op_id: reject-too-long
  - action: cli
    argv: ["cmd", "submit", "--selector", "COM0", "--source", "agent:l15", "--cmd", "{st.limits.max_submit_cmd_bytes_plus_one_x}"]
    expect: {ok: false, error_code: CMD_TOO_LONG}
  - action: op_end
    op_id: reject-too-long
  - action: assert_zero_tx
    op_id: reject-too-long
  - action: cli
    argv: ["cmd", "submit", "--selector", "COM0", "--source", "agent:l15", "--cmd", "echo L15_ACCEPTED"]
    save_as: ok1
  - action: wait_command_done
    cmd_id: "{ok1.cmd_id}"
  - action: assert_wire_contiguous
    payloads: ["echo L15_ACCEPTED\n"]
```

（`{st.limits.max_submit_cmd_bytes_plus_one_x}` 由 runner 的衍生變數產生：對任何 `save_as` 的整數欄位 `F`，`F_plus_one_x` 為長度 `F+1` 的 `"x"` 字串；runner 實作此規則並列入 README。）

`func-test/scenarios/l15-transport-disconnect.yaml` 的 steps（其餘欄位格式同上，`capabilities: [posix_pty, process_rpc, transport_disconnect]`）：

```yaml
steps:
  - {action: wait_ready, timeout_s: 20}
  - {action: op_begin, op_id: gen1-end}
  - {action: op_end, op_id: gen1-end}
  - {action: bridge_control, action_name: transport_disconnect, params: {remove_link: false}}
  - {action: cli, argv: ["cmd", "submit", "--selector", "COM0", "--source", "agent:l15", "--cmd", "echo L15_DURING_DOWN", "--cmd-timeout", "5"], save_as: down}
  - {action: wait_state, selector: COM0, state: RECOVERING, timeout_s: 10, accept: [RECOVERING, DETACHED]}
  - {action: sleep, seconds: 5}
  - {action: op_begin, op_id: reconnect, settle: false}
  - {action: bridge_control, action_name: transport_reconnect}
  - {action: op_end, op_id: reconnect}
  - {action: wait_ready, timeout_s: 40}
  - {action: assert_command_resolved, from: down, timeout_s: 30, payload: "echo L15_DURING_DOWN\n"}
  - {action: cli, argv: ["cmd", "submit", "--selector", "COM0", "--source", "agent:l15", "--cmd", "echo L15_AFTER_UP"], save_as: up}
  - {action: wait_command_done, cmd_id: "{up.cmd_id}"}
  - {action: assert_wire_contiguous, payloads: ["echo L15_AFTER_UP\n"]}
```

（`bridge_control` 的子動作鍵名為 `action_name`，避免與 step 的 `action` 鍵衝突；`wait_state.accept` 為可接受狀態集合。斷線期間的 `cmd submit` 可能被接受也可能被拒，兩者都合法，由 `assert_command_resolved` 判定：有 `cmd_id` 時輪詢 `cmd status` 直到 `command.status` 屬終態白名單 {`done`,`error`,`canceled`,`interactive`}（`sw_core/arbiter.py:179`、`259-324`；`accepted`、`running` 持續等待；缺欄位或未知狀態值一律失敗，不得視為已結案；逾時即失敗），並確認 `payload` 在 wire TX 出現次數 ≤1（不得自動重送）；無 `cmd_id` 時要求 `error_code` 非空，且 `payload` 在 wire TX 出現 0 次。本情境的 YAML `invariants` 為 `[no_auto_resend, insufficient_evidence_not_pass]`，steps 末尾另加 `{action: assert_compare}`。）

### T 步驟

- [ ] **T11-1：撰寫失敗測試** `tests/test_l15_scenarios.py`：

| 測試名 | 斷言 |
|---|---|
| `test_scenarios_present` | `func-test/scenarios/l15-*.yaml` 恰為上表 15 個 stem；`func-test/scenarios/win-com-*.yaml` 恰為 3 個 stem |
| `test_scenario_meta_complete` | 每個 `meta.scenario` 含 `revision`（`^\d{4}-\d{2}-\d{2}\.\d+$`）、`seed`（int）、`capabilities`（非空 list）、`timeout_s`（>0）、`invariants`（⊆ §11.3 六個 ID）、`expected`（`PASS`） |
| `test_declared_invariants_have_enforcers` | 每個情境宣告的每個 invariant，在 steps 中（含 `parallel`／`repeat` 巢狀）至少有一個 `INVARIANT_ENFORCERS[inv]` 所列動作；`win-com-*` 的 `no_reopen_before_handoff` 以 `hold_endpoint` 滿足 |
| `test_slice21_handoff_classes_covered` | 至少一個 `daemon_slice=="2.1"` 情境分別涵蓋 Release（`device release`）、Recovery（`session recover`）、Reboot（`logical_reboot`）、獨占（`hold_endpoint`）；至少一個 2.2 情境涵蓋 Lease 切換（`interactive-open`） |
| `test_assert_wire_absent` | 手寫 wire：marker 只出現在 `after_op` 之前 → 通過；之後出現 1 次 → 失敗；快照未 settled → `INSUFFICIENT_EVIDENCE` |
| `test_assert_peer_closed` | 手寫 correlation＋wire：`op_end` 快照 `peer_open:false` 且區間內無 `peer_state{open:true}` → 通過；`peer_open:true` → 失敗；區間內有 `open:true` → 失敗；Windows（`peer_open:null`）→ SKIP `cap_missing:peer_state` |
| `test_command_resolved_terminal_set` | 終態白名單＝{`done`,`error`,`canceled`,`interactive`}：stub `cmd status` 依序回 `accepted`→`running`→`done` → 通過；永久 `accepted` → 逾時失敗；缺 `command.status` 或未知值（如 `queued`）→ 失敗（不得視為已結案） |
| `test_scenarios_bridge_only` | 皆 `bridge.only: true` |
| `test_fault_actions_not_in_uart_stream` | 所有 step 的 `cli argv`、`console_write input`、`input_raw` 都不含 `transport_disconnect`、`dut_restart`、`pause_pump` 等控制字樣（控制只走 `bridge_control`／`dut_control`，§15.3） |
| `test_no_fixed_sleep_as_sync_point` | 每個情境中 `sleep` step 合計 ≤10 s，且每個故障動作之後的同步點是 `wait_wire_marker`／`wait_state`／`wait_command_done`／`wait_ready` 之一（§15.7.1「不依賴固定 sleep」） |
| `test_runner_actions_exist` | runner 動作表含上表 16 個新動作（`op_begin`、`op_end`、`bridge_control`、`dut_control`、`wait_wire_marker`、`assert_zero_tx`、`assert_wire_contiguous`、`assert_compare`、`wal_control`、`assert_wal_meta`、`assert_wal_marker`、`assert_command_resolved`、`assert_wire_absent`、`assert_peer_closed`、`hold_endpoint`、`release_endpoint`） |
| `test_release_endpoint_clears_tiocexcl`（`posix_pty`；非 root，root 下 skip 記「證據不足」） | 測試持有 PTY master；`hold_endpoint{mode:excl}` 回 `opened:true` → 一般 `os.open(slave)` 擲 `EBUSY` → `release_endpoint` → 一般 `os.open(slave)` 成功（R0-F3：替身自己的 TIOCEXCL 不得殘留） |
| `test_mid_run_asserts_use_checkpoint` | stub `BridgeSession`：`assert_compare`／`assert_zero_tx`／`assert_wire_absent` 執行前各呼叫一次 `checkpoint()`，並把回傳路徑傳給比較器 `checkpoint` 參數 |
| `test_bridge_only_action_rejected_in_pty` | 在 pty 模式執行 `op_begin` → `StepError("bridge-only action")` |
| `test_plus_one_x_derived_var` | `save_as` 存 `{"limits":{"max_submit_cmd_bytes":5}}` 後 `{st.limits.max_submit_cmd_bytes_plus_one_x}` 展開為 `"xxxxxx"` |
| `test_assert_command_resolved_both_branches` | 以 stub CLI 與手寫 wire：被接受（有 `cmd_id`、`cmd status` 回 `error`）且 payload 出現 1 次 → 通過；出現 2 次 → 失敗；被拒（無 `cmd_id`、`error_code` 非空）且 payload 出現 0 次 → 通過；出現 1 次 → 失敗 |
| `test_wal_meta_assert` | 對手寫 WAL 檔，`assert_wal_meta` 在 `meta={"event":"WAL_RECREATED_AFTER_EXTERNAL_UNLINK","potentially_lost_seq_range":"unknown"}`、`loss_flag:true` 時通過；`loss_flag:false` 時失敗 |
| `test_reject_zero_tx_runs_on_python`（`tests/test_l15_smoke.py`） | `runner.py --mode bridge --scenarios --case l15-reject-zero-tx` exit 0 |
| `test_release_inflight_runs_on_python`（`tests/test_l15_smoke.py`） | `--case l15-release-inflight` 對修正後 Python 結果為 PASS，或在 P1 T08 未合併時為 `dependency_blocked:SC-04`（不得 PASS 或 SKIP） |

- [ ] **T11-2：執行並確認 FAIL**：`python3 -m pytest -q tests/test_l15_scenarios.py tests/test_l15_smoke.py` → 情境檔不存在、新動作不存在。
- [ ] **T11-3：commit 測試**：`git commit -m "test(func-test): 新增 §15.7.1 情境腳本結構與新動作的失敗測試"`。

### I 步驟

- [ ] **I11-1：實作** 15＋3 個情境檔（每檔照上表＋上方兩個完整範例的格式）與 runner 新動作、`INVARIANT_ENFORCERS`、`_plus_one_x` 衍生變數。
- [ ] **I11-2：執行並確認 PASS**：`python3 -m pytest -q tests/test_l15_scenarios.py tests/test_l15_smoke.py`；確認 `is_qualified` 對 Task 8 報告仍為 True 後，`python3 func-test/runner.py --mode bridge --scenarios --selftest-report ~/b-log/rust-refactor/p2/selftest-linux/selftest-report.json` 對 Python 基線執行全部 15 個 L1.5 情境並保存報告（`win-com-*` 在 Linux 以 `cap_missing:windows_com` SKIP，屬 B3）。依前置條件表：P1 BC 尚未合併時對應情境記 `dependency_blocked:<BC-ID>`；P1 已合併仍失敗者分診為產品缺陷，報告原樣保留。
- [ ] **I11-3：commit**：`git commit -m "feat(func-test): 新增 15 個 L1.5 外部情境、3 個 Windows COM 情境與 bridge 專用協調動作"`＋fragment。

---
## Task 12：`tests/acceptance/cases.yaml` 承接清單、schema、凍結鎖、Stage 0 證據索引、MCU 手動程序登記

**Test author model:** agy-gemini-3.8-flash-high　**Implementer model:** haiku-xhigh
**Changelog fragment:** `changelog.d/rust-p2-acceptance-cases.md`（`type: feat`、`scope: test-infra`）
**BLOCKED-OWNER evidence:**
- `f4-background-result-tail-consistent` 在 Stage 0 bench 的實跑結果（`TST-D-6`：預期 PASS；若重現紅燈，擁有者核准後才登記 XFAIL 哨兵，含 issue #159、失敗特徵、期限）。
- `p2-1-mcu-flash-manual` 程序的實機執行與證據（DUT console、FTDI MCU 線、`ocp-mcu-upgrade`）。

**Files:**
- Create: `tests/acceptance/cases.yaml`、`tests/acceptance/cases.schema.json`、`tests/acceptance/cases.lock.json`、`tests/acceptance/regen_lock.py`、`realhw/procedures/p2-1-mcu-flash.md`
- Create: `tests/acceptance/stage0_evidence_index.yaml`、`tests/acceptance/evidence_index.schema.json`、`tests/acceptance/check_evidence_index.py`（§15.12「Stage 0～3 出口證據索引」）
- Create: `tests/test_acceptance_cases.py`、`tests/test_evidence_index.py`（皆模組層 `core_portable`＋`memory_only`；呼叫 `regen_lock.py`／`check_evidence_index.py` 子程序的函式改標 `process_rpc`，依 T14 第 2 條模組層只放平台 marker）
- Modify: `docs/regression-plugin.md`（第 91 行附近：f4 由「FAIL（刻意保留的紅燈哨兵）」改為「預期 PASS；#159 已修，常駐回歸防線；若重現紅燈依 §11.4 登記 XFAIL」，解決 `TST-D-6` 三方矛盾）

**Interfaces:**

`cases.yaml` 頂層：

```yaml
schema_version: 1
revision: "2026-10-15.1"
spec: "docs/rust-refactor/serialwrap-rust-spec-final.md"
invariant_ids: [zero_tx_on_reject, no_forbidden_interleave, no_stale_context_replay,
                no_reopen_before_handoff, no_auto_resend, insufficient_evidence_not_pass]   # §11.3 六項
features:            # 前置功能 → 最早可用切片（用於檢查跨切片依賴）
  wal_basic: "2.1"          arbiter: "2.1"          shell_login_min: "2.1"   quiet_gate_min: "2.1"
  drain_release: "2.1"      exclusive_open: "2.1"   background_capture: "2.1" self_test_basic: "2.1"
  activity_observe: "2.1"   multi_open_detect: "2.1" recovery_min: "2.1"     state_persistence: "2.1"
  console_pty: "2.2"        console_telnet: "2.2"   raw_line_mode: "2.2"     lease: "2.2"
  suspend_resume: "2.2"     deferred_buffer: "2.2"
  login_full: "2.3"         quiet_reconfirm: "2.3"  event_engine: "2.3"      stall_recovery: "2.3"
  rx_flood_classification: "2.3"
  file_transfer: "2.4"      device_handoff_full: "2.4" mcu_flash: "2.4"     supervision: "2.4"
  remote_cli: "1B"
cases:
  - case_id: f1-newline-rejected
    suite: regression                     # regression | realhw | func-test | l15-scenario | windows-native | manual-procedure
    source_ref: "regression/serialwrap_regression/cases/f01_cmd_contract.py:74"
    family: F1                            # F1..F12 | P0 | P1 | remote | longrun | L15 | WIN | MANUAL
    title: "含換行命令被拒"
    contract: "cmd submit 含換行 → error_code=CMD_CONTAINS_NEWLINE，零副作用"
    invariants: [zero_tx_on_reject]
    layer: L2                             # L1 | L1.5 | L2 | L3 | manual
    platforms: [linux, windows]
    windows_status: native                # native | ported | gap
    windows_gap_reason: null              # windows_status == gap 時必填
    capabilities: [physical_uart]
    cli_stage: "1A"                       # "1A" | "1B" | null
    daemon_slice: "2.1"                   # "2.1" | "2.2" | "2.3" | "2.4" | null
    stage0_required: true
    prerequisites: [arbiter]
    necessity: required                   # required | conditional（依板能力，SKIP 須事先登記）
    issues: ["#27"]
    behavior_changes: []
    destructive: false
    expected: PASS                        # PASS | XFAIL
    sentinel: null                        # XFAIL 時 {issue, signature, expires}
    evidence: [report.json, report.md]
    implemented_by: existing              # existing | <plan 檔名>（尚待其他 Phase 建立可執行驗證者）
    cross_slice_reason: null              # daemon_slice 晚於 family 預設切片時必填（例："需 Lease（Slice 2.2）"）
```

`cases.lock.json`：`{"schema_version":1,"cases_revision":"2026-10-15.1","sets":{"stage0":[...],"cli_1A":[...],"cli_1B":[...],"slice_2.1":[...],"slice_2.2":[...],"slice_2.3":[...],"slice_2.4":[...]}}`，每個集合為依字典序排序的 `case_id`（`stage0`＝`stage0_required:true`；`cli_1A`＝`cli_stage=="1A"`；`cli_1B`＝`cli_stage ∈ {1A,1B}`；`slice_X`＝`necessity=="required"` 且 `daemon_slice ≤ X`）。

`regen_lock.py`：`python3 tests/acceptance/regen_lock.py --check | --add-only`；`--add-only` 重算集合，若任何既有 ID 會從任何集合消失或改到較晚切片，exit 1 並列出（不寫檔）；否則寫回。這是唯一允許更新鎖檔的方式；移除或延後案例須另有擁有者裁決並在 PR 說明（§11.3「不得在測試失敗後刪除案例、縮小分母」）。

`cases.schema.json` 是承接清單欄位的**單一權威**：下游切片（P5～P8）新增案例一律以本 schema 欄位登記進 `cases.yaml`（P8 依 09 Task 1 以「只增不改」擴充 `suite`／`family` 列舉）；切片自有的清單檔（如 P6 `slice22.yaml`）不得另定欄位名，需改用本 schema 欄位或只作為 `cases.yaml` 的引用索引（下游同步項，見交付回報）。

**Stage 0 證據索引（§15.12）**：`stage0_evidence_index.yaml`（`schema_version: 1`、`stage: "0"`、`items:[{id, requirement, spec_ref, case_ids, artifact_path, sha256|null, status: present|missing|blocked_owner, owner}]`）逐項列出 Stage 0 出口所需證據（每個 `stage0_required` case 的 evidence、bridge 自測報告與 `APPROVED_REVISION.json`、兩平台 48 h 基線 `release-gate.json`、OD-09 凍結 revision、§4 BLOCKED-OWNER 各項）。`check_evidence_index.py --stage 0 [--root DIR]`：驗 schema、每個 `present` 項的檔案存在且 sha256 相符、每個 `stage0_required` case 至少被一項引用；任何 `missing`／`blocked_owner` → exit 5（BLOCKED），不得 exit 0。Stage 1～3 的索引由 P3／P4／P9 以同一 schema 新增 `stage1a_evidence_index.yaml` 等檔，checker 以 `--stage` 選擇。OD-15 證據層級：Task 17 為每個 item 加 `required_layer`（`L1` 或 `evidence_layer` 封閉集合 `L1.5-local`／`L1.5-docker`／`host-throwaway`／`native-windows`／`L2-bench`／`L3-bench`／`hw-bench` 之一）並讓 checker 讀被引用報告的 `evidence_layer`：`L1.5-docker` 或 `host-throwaway` 報告掛在 `required_layer ∈ {L2-bench, L3-bench, hw-bench}` 的項目 → 該項違規（exit 5，訊息 `evidence_layer_mismatch`）；docker／throwaway runner 產出的報告缺 `evidence_layer` → `evidence_layer_missing`（fail-closed）；docker 長跑與監管結果以獨立 item（`S0-L15-DOCKER-48H-LINUX`、`S0-L15-DOCKER-SUPERVISION-72H`）登錄，不併入 `S0-L3-BASELINE-*`。

**承接清單內容（全部 117 筆；`daemon_slice` 依 §11.6，跨切片者事先明列）**

L2：`regression/` 38 筆（`TST-D-1`：實際 38，不是 37）。共同欄位：`suite=regression`、`layer=L2`、`platforms=[linux, windows]`、`capabilities` 至少含 `physical_uart`、`evidence=[report.json, report.md]`、`implemented_by=existing`；非破壞者 `stage0_required:true`（§11.6 Stage 0 L2「全部適用非破壞案例」），破壞者 `false`。

| case_id | fam | slice | CLI | D | 追加 caps | prerequisites | issues | invariants／BC | windows |
|---|---|---|---|---|---|---|---|---|---|
| f3-cmd-fail-observable | F3 | 2.1 | 1A | | | arbiter | #94 | zero_tx_on_reject | native |
| f3-device-error-names-selector | F3 | 2.1 | 1A | | | arbiter | #16 | — | native |
| f3-fail-error-code | F3 | 2.1 | 1A | | | arbiter | #94 | — | native |
| f3-log-tail-latest | F3 | 2.1 | 1A | | | wal_basic | #124 | — | native |
| f1-cmdid-survives-timeout | F1 | 2.1 | 1A | | | arbiter | #15 | no_auto_resend | native |
| f1-limits-queryable | F1 | 2.1 | 1B | | | arbiter | #129, #27 | — | native |
| f1-near-limit-no-logout | F1 | 2.1 | 1A | | | arbiter, shell_login_min | #19 | — | native |
| f1-newline-rejected | F1 | 2.1 | 1A | | | arbiter | #27 | zero_tx_on_reject | native |
| f1-too-long-rejected | F1 | 2.1 | 1A | | | arbiter | #23, #27 | zero_tx_on_reject | native |
| f5-console-peer-gone-recycled | F5 | 2.2 | 1B | | human_frontend | console_pty, console_telnet, raw_line_mode | #53, #11 | — | gap：以 SIGKILL minicom 製造 PTY 對端異常消失；Windows Telnet 只有 socket EOF／RST 語意，無孤兒 PTY，`HumanFrontend.crash()` 的 RST 不等價 |
| f5-deferred-bytes-flushed | F5 | 2.2 | 1B | | human_frontend | suspend_resume, deferred_buffer | #78 | no_stale_context_replay；BC-02 | ported |
| f5-raw-ownership-survives-agent-rounds | F5 | 2.2 | 1B | | human_frontend | raw_line_mode, suspend_resume | #78 | no_forbidden_interleave | ported |
| f5-second-console-linebuffer | F5 | 2.2 | 1B | | human_frontend | raw_line_mode | #7, #8 | no_forbidden_interleave | ported |
| f6-ping-during-long-op | F6 | 2.2 | 1B | | | arbiter | #80 | — | native |
| f6-two-boards-no-starvation | F6 | 2.2 | 1A | | two_boards | arbiter | #80, #52 | no_forbidden_interleave | native |
| f2-history-bounded-rss | F2 | 2.1 | 1B | | | arbiter | #81 | — | ported（`procobs`） |
| f2-queue-full-backpressure | F2 | 2.1 | 1A | | | arbiter | #81 | zero_tx_on_reject | native |
| f2-recovery-flushes-queue | F2 | 2.1 | 1A | | | arbiter, recovery_min | #128, #156 | no_auto_resend；SC-04 | native |
| f2-rx-window-crossing-prompt | F2 | 2.1 | 1A | | | arbiter | #158 | — | native |
| f4-activity-classification | F4 | 2.1 | 1A | | | activity_observe | #34 | — | native |
| f4-background-result-tail-consistent | F4 | 2.1 | 1A | | | background_capture | #28 | expected PASS（`TST-D-6`）；`sentinel: null` | native |
| f4-interactive-line-cmd-defined | F4 | **2.2**（跨切片：需 Lease） | 1A | | | lease | #26 | — | native |
| f7-binary-roundtrip-md5 | F7 | 2.4 | 1A | | target_b64_tool | file_transfer | #32, #21, #161, #166 | — | native |
| f7-larger-file-not-truncated | F7 | 2.4 | 1A | | target_b64_tool | file_transfer | #21, #161, #166 | — | native |
| f8-second-daemon-detected | F8 | 2.2 | 1B | | posix_throwaway_daemon | multi_open_detect | #101 | — | gap：ThrowawayDaemon 為 POSIX-only（nohup、`/tmp` RUN_DIR、unix socket） |
| f8-tty-holders-reported | F8 | 2.2 | 1B | | | multi_open_detect, exclusive_open | #101, #53 | no_reopen_before_handoff；BC-04 | gap：Python Windows holder 偵測恆失效（`GAP-G-03`），Rust 由 SC-17 回 `holder_unknown` |
| f12-version-reported-consistent | F12 | 2.1 | 1B | | | — | #154 | — | native |
| f12-wal-path-live | F12 | 2.1 | 1B | | | wal_basic | #148 | insufficient_evidence_not_pass；BC-01（oracle 依 §8.3／OD-13 由 P1 修訂） | native |
| f9-attach-during-boot-reprobes | F9 | 2.3 | 1A | D | uboot_board | quiet_reconfirm | #69, #14, #20 | — | native |
| f9-brcm-spaced-prompt-reboot-login | F9 | 2.3 | 1A | D | brcm_board | login_full, quiet_reconfirm | #224 | — | native |
| f9-quiet-window-agent-passthrough | F9 | 2.3 | 1A | D | uboot_board | quiet_gate_min, quiet_reconfirm | #130 | zero_tx_on_reject | native |
| f9-reboot-autoboot-unmolested | F9 | 2.3 | 1A | D | uboot_board | quiet_gate_min | #130 | zero_tx_on_reject | native |
| f9-spontaneous-reboot-agent-gated | F9 | 2.3 | 1B | D | uboot_board, human_frontend | quiet_reconfirm, console_pty | #139, #130 | zero_tx_on_reject, no_stale_context_replay | ported |
| f9-uboot-readonly-and-console-kept | F9 | 2.3 | 1B | D | uboot_board, human_frontend | quiet_reconfirm, console_pty | #44, #130 | — | ported |
| f10-creds-fixed-then-ready | F10 | 2.3 | 1A | D | posix_throwaway_daemon | login_full, drain_release | #140 | no_reopen_before_handoff | gap：ThrowawayDaemon 為 POSIX-only |
| f10-unresolved-creds-terminal | F10 | 2.3 | 1A | D | posix_throwaway_daemon | login_full | #140 | zero_tx_on_reject | gap：ThrowawayDaemon 為 POSIX-only |
| f11-flood-probe-classified | F11 | 2.3 | 1A | D | | rx_flood_classification, self_test_basic | #153 | — | native |
| f11-last-rx-age-sane | F11 | 2.3 | 1A | | | activity_observe | #150 | — | native |

`f9-*` 依 §15.10 U-Boot 唯讀護欄：`necessity: conditional`（`uboot_board`／`brcm_board` 為事先登記能力；缺少時 SKIP 但阻擋相應平台出口）。`f7-*` 同為 `necessity: conditional`：能力 `target_b64_tool`（板端有 `base64` 或 `openssl enc -base64`，`f07_file_transfer.py` 的 probe；缺少時 SKIP `target_tool_missing`，事先登記、阻擋 Slice 2.4 出口直到擁有者提供具備工具的板或裁決）。

L3：`realhw/` 37 筆（`TST-D-2`：remote 實際 8）。共同欄位：`suite=realhw`、`layer=L3`、`capabilities` 含 `physical_uart`、`evidence=[report.json, report.md]`、`issues: []`（realhw registry 無 issue 欄位）。`stage0_required`：P0、P1 為 true（§11.6 Stage 0 L3「P0＋P1 適用集合」），remote 為 false，`lr-mixed` 為 true（修正後 Python 48 h 基線，兩平台各一次，OD-10）。

| case_id | tier | slice | CLI | D | 追加 caps | prerequisites | invariants／BC | windows |
|---|---|---|---|---|---|---|---|---|
| p0-doctor | P0 | 2.1 | 1B | | two_boards | — | — | native |
| p0-cmd-async | P0 | 2.1 | 1A | | two_boards | arbiter, background_capture | — | native |
| p0-console-raw | P0 | 2.2 | 1B | | human_frontend, two_boards | raw_line_mode | — | ported |
| p0-clear-reattach | P0 | 2.1 | 1A | | two_boards | drain_release | — | native |
| p0-selftest | P0 | 2.1 | 1A | | | self_test_basic | — | native |
| p0-blog-clean | P0 | 2.2 | 1B | | tmux | console_pty | — | gap：驗的是 minicom capture 檔（`~/b-log/mini_COM0_*.log`），Windows Telnet frontend 無對應產物 |
| p0-wal-live | P0 | 2.1 | 1A | | | wal_basic | insufficient_evidence_not_pass；BC-01（oracle 由 P1 依 `TST-D-7` 修訂） | native |
| p0-multiopen | P0 | 2.1 | 1B | | | multi_open_detect, exclusive_open | BC-04 | gap：Python Windows multi_open 恆失效（`GAP-G-03`） |
| p1-con-fanout | P1 | 2.2 | 1B | | human_frontend, two_boards | console_pty, console_telnet | — | ported |
| p1-con-defer | P1 | 2.2 | 1B | | human_frontend, two_boards | suspend_resume, deferred_buffer | no_stale_context_replay；BC-02 | ported |
| p1-con-busy | P1 | 2.2 | 1B | | human_frontend, two_boards | lease | — | ported |
| p1-con-softpreempt | P1 | 2.2 | 1B | | human_frontend, two_boards | lease | — | ported |
| p1-con-liveness | P1 | 2.2 | 1B | | human_frontend, two_boards | console_pty, console_telnet | — | gap：以 `pgrep minicom`＋`kill -9` 製造對端異常消失（`p1_console.py:20-29`）；Windows socket EOF 偵測無孤兒語意 |
| p1-con-orphan | P1 | 2.2 | 1B | | human_frontend, two_boards | console_pty, console_telnet | — | gap：同上（孤兒只來自 SIGKILL／crash） |
| p1-con-second | P1 | 2.2 | 1B | | human_frontend, two_boards | raw_line_mode | no_forbidden_interleave | ported |
| p1-cmd-modes | P1 | 2.2 | 1A | | two_boards | lease, background_capture | — | native |
| p1-cmd-serial | P1 | 2.1 | 1A | | two_boards | arbiter | no_forbidden_interleave | native |
| p1-cmd-file | P1 | 2.4 | 1A | | two_boards | file_transfer | — | native |
| p1-wal-reset | P1 | 2.2（跨切片：讀 console） | 1B | | human_frontend, two_boards | wal_basic, console_pty | — | ported |
| p1-wal-fullrun | P1 | 2.2（跨切片：讀 console） | 1B | | human_frontend, two_boards | wal_basic, console_pty | — | ported |
| p1-rst-daemon | P1 | 2.1 | 1B | D | two_boards, supervisor_runtime | state_persistence | — | gap：`sudo systemctl restart`；Windows `windows-task` 版本由 P4 建立（`requires` 由 Task 14 補 `supervisor_runtime`，使 Windows 能力式 SKIP 生效） |
| p1-rst-reboot | P1 | 2.3 | 1B | D | two_boards, deployed_recent, human_frontend | quiet_reconfirm, console_pty | — | ported |
| p1-rst-bootwindow | P1 | 2.3 | 1A | D | two_boards, deployed_recent | quiet_reconfirm | — | native |
| p1-rst-recover | P1 | 2.1 | 1A | D | two_boards | recovery_min | no_auto_resend | native |
| p1-ho-cycle | P1 | 2.4 | 1A | D | two_boards, tmux | device_handoff_full | no_reopen_before_handoff；BC-04 | gap：外部持有者為 `minicom -D`、holder 以 `/proc` 判定 |
| p1-ho-persist | P1 | 2.4 | 1B | D | two_boards, supervisor_runtime | device_handoff_full, state_persistence | — | gap：systemd restart |
| p1-hp-cycle | P1 | 2.1 | 1A | D | two_boards, usbipd | drain_release, exclusive_open | no_reopen_before_handoff；SC-04 | gap：usbipd 由 WSL 端操作 Linux daemon |
| p1-hp-reorder | P1 | 2.1 | 1B | D | two_boards, usbipd, supervisor_runtime | drain_release, state_persistence | — | gap：同上 |
| lr-mixed | longrun | 2.4 | 1B | | two_boards, human_frontend, longrun_bench | console_pty, console_telnet, lease, background_capture | 全部六項（由 Task 15 外層 gate 判定） | ported（Telnet frontend、`procobs`、`daemon status.pid`） |
| rm-topo-direct | remote | 2.3 | 1B | | docker | remote_cli | BC-05 | gap：容器拓樸只在 Linux |
| rm-topo-nat-host | remote | 2.3 | 1B | | docker | remote_cli | BC-05 | gap：同上 |
| rm-topo-dual-nat | remote | 2.3 | 1B | | docker | remote_cli | BC-05 | gap：同上 |
| rm-topo-agent-pull | remote | 2.3 | 1B | | docker | remote_cli | BC-05 | gap：同上 |
| rm-topo-gwports | remote | 2.3 | 1B | | docker | remote_cli | BC-05 | gap：同上 |
| rm-live-e2e | remote | 2.3 | 1B | | docker, two_boards, remote_capability | remote_cli | BC-05 | gap：Windows remote 承接於 P4（SC-27） |
| rm-live-orphan | remote | 2.3 | 1B | | docker, two_boards, remote_capability | remote_cli | BC-05 | gap：同上 |
| rm-live-cycle | remote | 2.3 | 1B | | docker, two_boards, remote_capability | remote_cli | BC-05 | gap：同上 |

`lr-mixed` 的 `evidence`：`[events.ndjson, snapshots.ndjson, longrun-analysis.md, observer.ndjson, wal/, release-gate.json]`。

L1.5：`func-test/` 13 筆（`suite=func-test`、`layer=L1.5`、`platforms=[linux, windows]`、`capabilities=[posix_pty, process_rpc]`（Windows 列 `windows_com`）、`stage0_required:true`、`issues: []`、`evidence=[manifest.json, wire.ndjson, control.ndjson, correlation.ndjson, comparison.json, dut.stderr.log, cli.ndjson, daemon.log, wal/]`、`implemented_by=existing`）：

| case_id | slice | CLI | prerequisites | invariants | windows |
|---|---|---|---|---|---|
| brcm-01-login-flow | 2.3 | 1A | login_full | — | ported |
| co-01-line-buffering | 2.2 | 1B | console_pty, raw_line_mode | — | ported（`TelnetConsole`） |
| cowork-01-agent-bad-cmd | 2.2 | 1B | arbiter, recovery_min, console_pty | no_auto_resend | ported |
| ev-01-spawn-handler | 2.3 | 1A | event_engine | — | ported（`write_file`＋跨平台 handler，Task 10） |
| ha-01-human-then-agent | 2.2 | 1B | console_pty | — | ported |
| ha-05-concurrent-submit | 2.1 | 1A | arbiter, wal_basic | no_forbidden_interleave；SC-07 | native |
| hk-01-special-keys-raw | 2.2 | 1B | raw_line_mode | — | ported |
| hk-02-agent-preempt-human | 2.2 | 1B | suspend_resume | no_forbidden_interleave | ported |
| hk-03-deferred-input-flush | 2.2 | 1B | deferred_buffer | no_stale_context_replay；BC-02 | ported |
| hk-04-minicom-exit-cleanup | 2.2 | 1B | raw_line_mode | — | ported |
| rc-01-interactive-mode-switch | 2.2 | 1B | raw_line_mode, suspend_resume | — | ported |
| sm-01-attach-to-ready | 2.1 | 1A | shell_login_min | — | native |
| sm-04-device-unplug | 2.1 | 1A | drain_release | no_reopen_before_handoff | gap：Windows 端點無 by-id link（`by_id_link:false`），拔除需實體或 PnP（L2） |

L1.5：§15.7.1 情境 15 筆（`suite=l15-scenario`、`layer=L1.5`、`platforms=[linux, windows]`、`stage0_required:true`、`implemented_by="03-stage0-test-infra.md"`，`slice`／`invariants` 照 Task 11 表；`behavior_changes`：`l15-delayed-reply`→[BC-03]、`l15-dut-reboot-port-alive`→[BC-02]、`l15-transport-disconnect`→[BC-03, SC-04]、`l15-wal-interference`→[BC-01]、`l15-multi-source-contention-cli`→[SC-07]、`l15-release-inflight`→[BC-03, SC-04]、`l15-exclusive-open`→[BC-04, SC-05]、`l15-context-switch-deferred-input`→[BC-02]；windows 為 gap 者：`l15-transport-disconnect`（com0com 只有 peer close，未經實測不得計 `transport_disconnect`，§15.8.3）、`l15-exclusive-open`（POSIX flock／TIOCEXCL 語意；Windows 由 `win-com-open-exclusive` 承接）、`l15-release-inflight`（`assert_peer_closed` 需 `peer_state`；Windows 由 `win-com-cancel-close` 承接））。

Windows-native 13 筆（`suite=windows-native`、`platforms=[windows]`、`windows_status=native`）。hk-01／co-01／hk-02／hk-03 在 Windows 的執行**不另列** case：它們是同一 case 的不同平台執行，已由該 case 的 `platforms:[linux, windows]` 承接（§15.5「不同模式的獨立 case 數分開統計」，不重複計入分母）：

| case_id | layer | caps | slice | CLI | stage0 | prerequisites | BC | implemented_by |
|---|---|---|---|---|---|---|---|---|
| win-com-open-exclusive | L1.5 | windows_com, process_rpc | 2.1 | 1A | true | exclusive_open | BC-04, SC-05, SC-19 | 03-stage0-test-infra.md（`func-test/scenarios/win-com-open-exclusive.yaml`，Task 11） |
| win-com-txrx-bridge | L1.5 | windows_com, process_rpc | 2.1 | 1A | true | arbiter | SC-19 | 03-stage0-test-infra.md（`func-test/scenarios/win-com-txrx-bridge.yaml`） |
| win-com-cancel-close | L1.5 | windows_com, process_rpc | 2.1 | 1A | true | drain_release | BC-03, SC-04 | 03-stage0-test-infra.md（`func-test/scenarios/win-com-cancel-close.yaml`） |
| win-wal-av-rotate | L1.5 | windows_com, endpoint_av | 2.1 | 1A | true | wal_basic | BC-01 | 02-stage0-python-bc.md（Spike 3） |
| win-wal-av-unlink | L1.5 | windows_com, endpoint_av | 2.1 | 1A | true | wal_basic | BC-01 | 02-stage0-python-bc.md |
| win-wal-av-reader-handle | L1.5 | windows_com, endpoint_av | 2.1 | 1A | true | wal_basic | BC-01 | 02-stage0-python-bc.md |
| win-wal-f12-progress | L2 | physical_uart, endpoint_av | 2.1 | 1B | true | wal_basic | BC-01 | 02-stage0-python-bc.md |
| win-task-logon-trigger | L3 | supervisor_runtime, interactive_desktop | 2.4 | 1B | false | supervision | SC-24 | 05-stage1b-rust-cli-mgmt.md（Spike 2） |
| win-task-single-instance | L3 | supervisor_runtime | 2.4 | 1B | false | supervision | SC-24 | 05-stage1b-rust-cli-mgmt.md |
| win-task-restart-on-failure | L3 | supervisor_runtime | 2.4 | 1B | false | supervision | SC-24 | 05-stage1b-rust-cli-mgmt.md |
| win-task-72h | L3 | supervisor_runtime, longrun_bench | 2.4 | 1B | false | supervision | SC-24 | 05-stage1b-rust-cli-mgmt.md |
| win-remote-native-l1 | L1 | windows_native, process_rpc | null（`remote_cli`） | 1B | true | remote_cli | BC-05, SC-21, SC-27 | 02-stage0-python-bc.md（T14 `tests/test_stage0_remote_windows.py` 經 `scripts/stage0/win-pytest.sh` 原生執行） |
| win-remote-openssh-3topo | L3 | windows_native, remote_capability | null（`remote_cli`） | 1B | false | remote_cli | BC-05 | 02-stage0-python-bc.md（T14 BLOCKED-OWNER：Win32-OpenSSH 對真實 relay 的既有三拓樸） |

手動程序 1 筆：`p2-1-mcu-flash-manual`（`suite=manual-procedure`、`layer=manual`、`platforms=[linux]`、`windows_status=gap`、`windows_gap_reason`：「`/dev/ttyMCU` PTY-bridge 為 POSIX-only；Windows 燒錄走 #54 device release」、`capabilities=[physical_uart, mcu_line]`、`daemon_slice: "2.4"`、`cli_stage: "1B"`、`stage0_required:false`、`destructive:true`、`prerequisites=[mcu_flash]`、`issues=["#55"]`、`evidence=[flash.log, procedure-checklist.md, daemon-status-before.json, daemon-status-after.json]`、另有欄位 `procedure: {path: "realhw/procedures/p2-1-mcu-flash.md", revision: <該檔 git blob sha1>, entry: "手動，依程序逐步執行", owner: "擁有者（實機）", counted_in: none}`）。此筆**不計入** L2 38／L3 37 計數（§11.6 Slice 2.4）。

合計：L2 38＋L3 37＋func-test 13＋L1.5 情境 15＋Windows-native 13＋手動 1＝117 筆。

`realhw/procedures/p2-1-mcu-flash.md`：把 repo `CLAUDE.md`「MCU flash 真機驗證手法」節的隔離跑法、進 BSL（逐行短指令）、燒錄指令與期望 `Return error code : 0x0`、燒後 `ATTACHED` 恢復、三個實機陷阱改寫為編號步驟＋每步證據檔名，結尾列 PASS 條件（燒錄 0x0、session 自動恢復 ATTACHED、daemon PID 不變、其他 COM 狀態不變）。

### T 步驟

- [ ] **T12-1：撰寫失敗測試** `tests/test_acceptance_cases.py`：

```python
REPO = pathlib.Path(__file__).resolve().parents[1]
CASES = yaml.safe_load((REPO / "tests/acceptance/cases.yaml").read_text(encoding="utf-8"))

def _ids(suite): return {c["case_id"] for c in CASES["cases"] if c["suite"] == suite}

def test_regression_cases_match_registry():
    sys.path.insert(0, str(REPO / "regression"))
    from serialwrap_regression import harness as rh
    reg = rh.load_registry()
    assert _ids("regression") == {c.id for c in reg}          # 以 registry 為準（實際 38），不寫死數字

def test_realhw_cases_match_registry():
    sys.path.insert(0, str(REPO))
    import realhw.cases  # noqa: F401  註冊所有 case
    from realhw import harness as hh
    assert _ids("realhw") == {c.id for c in hh.REGISTRY}

def test_functest_cases_match_yaml():
    assert _ids("func-test") == {p.stem for p in (REPO / "func-test/cases").glob("*.yaml")}

def test_scenarios_match_yaml():
    assert _ids("l15-scenario") == {p.stem for p in (REPO / "func-test/scenarios").glob("l15-*.yaml")}

def test_windows_native_p2_cases_have_scenarios():
    own = {c["case_id"] for c in CASES["cases"]
           if c["suite"] == "windows-native" and str(c["implemented_by"]).startswith("03-stage0-test-infra.md")}
    assert own == {p.stem for p in (REPO / "func-test/scenarios").glob("win-com-*.yaml")}

def test_lock_never_shrinks():
    lock = json.loads((REPO / "tests/acceptance/cases.lock.json").read_text(encoding="utf-8"))
    current = compute_sets(CASES)                              # 由 tests/acceptance/regen_lock.py 匯入
    for name, ids in lock["sets"].items():
        missing = sorted(set(ids) - set(current[name]))
        assert missing == [], f"{name} 少了 {missing}（不得刪除案例或延後切片）"
```

其餘：

| 測試名 | 斷言 |
|---|---|
| `test_schema_valid` | `cases.yaml` 以 `cases.schema.json`（draft 2020-12）驗證無錯；schema 中 `required` 含 §11.3 全部欄位：`case_id`、`contract`、`invariants`、`layer`、`platforms`、`capabilities`、`daemon_slice`、`prerequisites`、`necessity`、`issues`、`behavior_changes`、`expected`、`evidence` |
| `test_case_ids_unique` | 全部 `case_id` 不重複 |
| `test_regression_cases_have_issues` | `suite==regression` 者 `issues` 非空 |
| `test_prerequisite_slice_order` | 每筆 `daemon_slice`（非 null）≥ 其所有 `prerequisites` 在 `features` 表中的切片；`remote_cli` 只要求 `cli_stage=="1B"` |
| `test_cli_stage_rule` | `prerequisites` 含 `console_pty`、`console_telnet`、`remote_cli`、`supervision`，或 `capabilities` 含 `human_frontend`、`supervisor_runtime` 者，`cli_stage=="1B"`（§4.2：console-attach／daemon／service／remote 屬 1B） |
| `test_spec_slice_families` | F1、F2、F3、F12 全部 2.1；F4 除 `f4-interactive-line-cmd-defined`（2.2）外為 2.1；F5、F6、F8 為 2.2；F9、F10、F11 為 2.3；F7 為 2.4（§11.6） |
| `test_cross_slice_cases_declared` | `daemon_slice` 晚於其 family 預設切片者（`f4-interactive-line-cmd-defined`、`p1-wal-reset`、`p1-wal-fullrun`）在 YAML 中有 `cross_slice_reason` 欄位 |
| `test_windows_gap_has_reason` | `windows_status=="gap"` 者 `windows_gap_reason` 為非空字串；`native`／`ported` 者為 null |
| `test_windows_com_not_satisfied_by_memory` | `capabilities` 含 `windows_com` 者 `layer` ∈ {L1.5, L2}（不得登記為 L1）；`platforms` 含 `windows` |
| `test_behavior_changes_exist` | 每個 `behavior_changes` ID 在 `contracts/behavior_changes.yaml`（P0）中存在 |
| `test_each_bc_has_stage0_case` | BC-01～BC-05 每一個至少被一筆 `stage0_required:true` 的 case 引用（§11.6 Stage 0「BC-02／03 有對應案例」；BC-05 由 `win-remote-native-l1` 承接） |
| `test_no_duplicate_platform_rows` | 沒有 case 的 `contract`／`source_ref` 只是另一筆 case 換平台的重複（`windows-native` suite 中不得出現 `title` 含「於 Windows」且指向既有 func-test case 者） |
| `test_spike_is_not_gate_evidence` | 沒有任何 case 的 `evidence` 引用 `spike-report.json`（自 Task 9 移入，避免前向依賴） |
| `test_conditional_cases_declare_capability` | `necessity=="conditional"` 者 `capabilities` 至少含一個事先登記能力（`uboot_board`、`brcm_board`、`target_b64_tool` 等）；`f7-*` 為 conditional 且含 `target_b64_tool` |
| `test_evidence_index_schema_and_check`（evidence_index） | `stage0_evidence_index.yaml` 以 schema 驗證通過；每個 `stage0_required` case 至少被一項引用；`check_evidence_index.py --stage 0` 在目前（含 `blocked_owner` 項）exit 5，暫存副本全部改為 `present` 且檔案／sha256 正確時 exit 0；sha256 不符 exit 1 |
| `test_invariants_from_spec_only` | 每個 `invariants` 值屬 `invariant_ids`；六個 ID 每一個至少被一筆 `stage0_required` 的 case 引用 |
| `test_f4_sentinel_state_consistent` | `f4-background-result-tail-consistent` 為 `expected: PASS`、`sentinel: null`；`docs/regression-plugin.md` 不再含「刻意保留的紅燈哨兵」字樣 |
| `test_xfail_requires_sentinel` | `expected=="XFAIL"` 者 `sentinel` 含 `issue`、`signature`、`expires`（ISO 日期） |
| `test_manual_procedure_not_counted` | `p2-1-mcu-flash-manual` 的 `procedure.counted_in=="none"`；`procedure.revision` 等於 `realhw/procedures/p2-1-mcu-flash.md` 的 git blob sha1（`sha1(b"blob %d\0" % len + content)`） |
| `test_lr_mixed_stage0_and_evidence` | `lr-mixed` 的 `stage0_required` 為 true，`evidence` 含 `release-gate.json` 與 `observer.ndjson` |
| `test_regen_lock_check_clean` | `python3 tests/acceptance/regen_lock.py --check` exit 0 |
| `test_regen_lock_refuses_removal` | 在暫存副本刪除一筆 required case 後執行 `--add-only` → exit 1 且鎖檔未變 |
| `test_counts_report` | 產生的統計（`by suite`、`by layer`、`by slice`）寫入 `tests/acceptance/cases.counts.json` 並與 YAML 一致（僅資訊，§11.3「承接率只作進度資訊」） |

- [ ] **T12-2：執行並確認 FAIL**：`python3 -m pytest -q tests/test_acceptance_cases.py` → `FileNotFoundError: tests/acceptance/cases.yaml`。
- [ ] **T12-3：commit 測試**：`git commit -m "test(acceptance): 新增案例承接清單一致性、切片凍結鎖與哨兵狀態失敗測試"`。

### I 步驟

- [ ] **I12-1：實作**：依上列五張表與共同欄位寫 `cases.yaml`（117 筆；`contract`、`title` 取自各 case 的 docstring／`meta.description`，不得留空）；寫 `cases.schema.json`、`regen_lock.py`（含 `compute_sets`）、以 `--add-only` 產生初版 `cases.lock.json` 與 `cases.counts.json`；寫 `stage0_evidence_index.yaml`、`evidence_index.schema.json`、`check_evidence_index.py`；寫 `realhw/procedures/p2-1-mcu-flash.md`；修 `docs/regression-plugin.md`。
- [ ] **I12-2：執行並確認 PASS**：`python3 -m pytest -q tests/test_acceptance_cases.py tests/test_evidence_index.py`；`python3 -m pytest -q tests/` 無新失敗。
- [ ] **I12-3：commit**：`git commit -m "feat(acceptance): 建立 117 筆案例承接清單、切片凍結鎖、Stage 0 證據索引與 MCU 手動程序登記"`＋fragment。

---

## Task 13：pytest 資源報告、平台 SKIP 閘門、Windows collection 與 hosted Windows L1（`TST-D-4` 報告部分）

**Test author model:** agy-gemini-3.8-flash-high　**Implementer model:** haiku-xhigh
**Changelog fragment:** `changelog.d/rust-p2-pytest-resource-report.md`（`type: feat`、`scope: tests`）
**BLOCKED-OWNER evidence:**
- 自架 Windows COM runner（OD-08）註冊與 `windows-com-selfhosted` job 的 CI 實跑結果（需擁有者在 runner 上安裝 GitHub Actions runner 並加 label `serialwrap-com`）。hosted `windows-latest` 的 `pytest-windows` job 在 PR CI 可取得，不屬 BLOCKED。
- OD-15：同一組 `windows_com` 步驟（`selftest --endpoint win-serial`、`-m windows_com`、`win-com-*`）不必等 runner 註冊，可由 orchestrator 在擁有者 host 以 `SW_TEST_COM_PAIR=COM20:COM21` 經 interop 直接執行（gate G4～G6）；runner 只影響 CI 自動化，不影響證據可得性。

**範圍（依 P0 T14「本 Task 是 marker 註冊、檔內標註與稽核的唯一 owner；P2 只保留報告」）**：本 Task **不**建立 `tests/platform_resources.yaml`、**不**在 `pytest_collection_modifyitems` 依檔名套 marker、**不**註冊 marker、**不**另訂分類規則；marker 一律讀檔內 `pytestmark`（P0 T14 規則），P1 T01 的平台自動 skip hook 保持原樣。本 Task 與 P0／P1 的 hook 合併在 `tests/conftest.py` 的**同一個** `pytest_collection_modifyitems`／`pytest_sessionfinish` 定義內（不得出現第二個同名 hook 定義）。

**Files:**
- Modify: `tests/conftest.py`（`pytest_ignore_collect`（Windows collection 前排除）；`pytest_configure` 註冊 `tests/resource_report_plugin.py`（見下））
- Create: `tests/resource_report_plugin.py`（R5 審查後續：報告 hook 的**唯一**宿主——`pytest_runtest_logreport` 累計、`pytest_collection_finish`／`pytest_deselected`、`pytest_sessionstart`／`pytest_sessionfinish` 量測 `deployed_content`（R5-F4）、`pytest_sessionfinish`（`trylast=True`，P0 live-guard 之後）寫 `SW_RESOURCE_REPORT` 與 `core_portable` 平台 SKIP 閘門；只 import stdlib、`tests/resource_report.py` 與 `realhw/deployed_content.py`（皆以 `importlib.util.spec_from_file_location` 依相對本檔的路徑載入），**不得** import `sw_core` 或 root conftest。理由：黑箱以 `--confcutdir=tests/contract_blackbox` 獨立執行（P3 Task 2，Windows staging 甚至不含 root conftest），root `tests/conftest.py` 不會被載入，報告 hook 若只在 root conftest，黑箱 L1 出口所需的 `l1-results.v1` 永遠不會產生。root conftest 與 P3 `tests/contract_blackbox/conftest.py` 都在 `pytest_configure` 以 `if not config.pluginmanager.has_plugin("sw_resource_report"): config.pluginmanager.register(<模組>, "sw_resource_report")` 註冊，完整 `pytest tests/` 只註冊一次）
- Create: `tests/resource_report.py`（純函式：AST 讀模組層 `pytestmark`、平台條件判定、報告彙總；conftest 只呼叫它）、`tests/test_resource_report.py`（模組層 `core_portable`；AST／彙總函式 `memory_only`，子程序測試 `process_rpc`）
- Modify: `.github/workflows/tests.yml`（新增 `pytest-windows` 與 `windows-com-selfhosted` 兩個 job）
- Create（L1 完整性，R5-F3）：`tests/acceptance/l1_required.lock.json`、`tests/acceptance/l1_required.schema.json`、`tests/acceptance/l1_results.schema.json`、`tests/acceptance/l1_judge.py`（stdlib 純函式；全計畫**唯一**的 L1 完整性判定實作，P3～P9 以 `importlib.util.spec_from_file_location` 依路徑載入，不得另寫）、`tests/acceptance/l1_judge_vectors.json`（golden 向量，P9 Task 14B 以同一份向量回歸）、`tests/test_l1_judge.py`（模組層 `core_portable`；判定函式 `memory_only`，子程序測試 `process_rpc`）
- Modify：`tests/acceptance/regen_lock.py`（新增 `--l1-add-only --set <id> --platform <p> --collect <檔>`，見下）、`tests/acceptance/check_evidence_index.py`＋`evidence_index.schema.json`（項目新增選填 `l1_set`，見下）、`tests/acceptance/stage0_evidence_index.yaml`（`S0-L1-BC`、`S0-L1-WIN-PYTEST` 兩項填 `l1_set`）

**Interfaces:**

```python
# tests/resource_report.py
PLATFORM_MARKERS = ("core_portable", "posix_only", "windows_native")
PLATFORM_SKIP_RE = r"sys\.platform|os\.name|platform\.system|AF_UNIX|openpty|geteuid|fcntl|msvcrt|win32|_IS_WIN|IS_WINDOWS|posix"
    # 與 P0 T14 規則第 3 條「平台條件 SKIP」同一 regex（逐字複製並以測試比對 P0 稽核檔中的定義，避免漂移）
def module_platform_marks(path: pathlib.Path) -> set[str]
    # 不 import 模組：AST 讀模組層 `pytestmark` 中的平台 marker（只認 P0 T14 允許的寫法）
def should_ignore_on_windows(path: pathlib.Path) -> bool
    # 模組層平台 marker 為 {"posix_only"} 時 True（Windows 上 import 該模組可能因頂層 `import pty`／`termios` 失敗，
    #  例：tests/test_multiagent_e2e.py:5,12；`-m` 在 import 之後才篩選，擋不住 collection error）
def is_platform_skip(item_markers: Iterable[Any]) -> bool
    # item 的 skip／skipif marker 是否為平台條件：skipif 的 condition 原始碼（字串或 AST 片段）符合 PLATFORM_SKIP_RE；
    # 以 marker 結構判定，不看 skip reason 文字（P1 T01 的 windows_native／posix_pty 自動 skip 只落在非 core_portable item）
def summarize(records: list[dict[str, Any]], *, ignored: list[str], platform: str) -> dict[str, Any]
```

`SW_RESOURCE_REPORT=<path>` 時於 `pytest_sessionfinish`（P0 live-guard 之後）寫：

```json
{"schema_version":2,"platform":"linux","counts":{"core_portable":{"passed":0,"failed":0,"skipped":0},"posix_only":{...},"windows_native":{...}},
 "resources":{"memory_only":{...},"process_rpc":{...},"posix_pty":{...},"windows_com":{...},"physical_uart":{...},"supervisor_runtime":{...},"interactive_desktop":{...}},
 "not_collected_platform":["tests/test_multiagent_e2e.py", "..."],
 "core_portable_platform_skips":["<nodeid>", "..."]}
```

- `not_collected_platform`：Windows 上被 `pytest_ignore_collect` 排除的檔案逐一列出（報告為「未執行」，不計 skipped 也不計 passed）。
- 閘門：`core_portable` 的 item 若帶平台條件 SKIP（`is_platform_skip`），列入 `core_portable_platform_skips`，session exit status 升為 1（§11.2「此集合禁止以平台原因 SKIP」）。判定以 marker 結構為準，不以 skip reason 子字串判定。
- `deployed_content`（R5-F4，報告必帶此鍵）：環境變數 `SERIALWRAP_CLI_EXE`（arch §9.2 黑箱 artifact 選定；conftest pop 前先讀出）有值時，`pytest_sessionstart`（collection 前）以 Task 10 `realhw.deployed_content.measure(Path(值), daemon_argv=[SERIALWRAP_DAEMON_EXE 的值] 或 None)` 量 start、`pytest_sessionfinish` 寫報告前再量 end，寫 `bracket(start, end)`；**`SERIALWRAP_CLI_EXE` 有值而 `SERIALWRAP_DAEMON_EXE` 未設**（R5 審查後續：黑箱 harness 會退回 repo 的 `py-serialwrapd`，形成「release CLI＋repo Python daemon」）→ start／end 的 `error` 皆為 `"daemon_env_missing"`、digest 為 null（ingest 視為未量測，tree target 一律拒收）；未設時寫 `null`（repo 原始碼執行的 Python 基線沒有安裝可量）。只量測、不改 exit status——是否足以證明受測內容由 P9 Task 14B ingest 對照 target 判定（S3-L1-RELEASE-CONTRACT-* 等 target 記有 bundle tree 的項目，`null` 或指紋不符一律拒收）。此鍵同時列入下方 `l1-results.v1` 的 `tests/acceptance/l1_results.schema.json`（required；值為 `null` 或 `{"start","end"}` 兩個 `deployed-content.v1` 物件），不得因 schema 收緊而被省略。
- **L1 逐案結果（R5-F3，報告 `schema_version` 升為 2；以下鍵必帶）**：報告同時是 `l1-results.v1`（`tests/acceptance/l1_results.schema.json`）文件——`"format":"l1-results.v1"`、`"runner":"pytest"`、`"argv"`（`sys.argv` 原樣）、`"markexpr"`（`config.option.markexpr`，無則 `""`）、`"collected"`（`pytest_collection_finish` 時 `session.items` 的 nodeid，**依收集順序、不去重**）、`"deselected"`（`pytest_deselected` 收到的 nodeid）、`"results"`（每個執行過的 nodeid 一列 `{"nodeid","outcome","skip_kind","phases":{"setup","call","teardown"}}`；`outcome` 由三個 phase 彙總：任一 phase `failed` 且屬 setup／teardown → `error`、call `failed` → `failed`、`wasxfail` 且 call 失敗 → `xfailed`、`wasxfail` 且通過 → `xpassed`、skip → `skipped`、其餘 `passed`；`skip_kind` ∈ `platform`（`is_platform_skip`）、`capability`（P1 T01 自動 skip 的資源 marker）、`other`、`null`）、`"exit_code"`（`pytest_sessionfinish` 的 `exitstatus`，**含**上列平台 SKIP 閘門升級後的值）、`"finished": true`（只在 `pytest_sessionfinish` 走完才寫；KeyboardInterrupt、`-x` 中止、collection error 仍寫報告但 `exit_code` 為 pytest 原值，被 kill 則根本無報告）。同一 nodeid 的多列（如 `--count`／重跑外掛造成）**原樣保留**，不得合併——是否重複由判定端認定。這些鍵只供判定，不改變既有 exit status 規則。

**L1 必要案例集合與完整性判定（R5-F3，§11.3「必要案例集合事先凍結」、§11.4「證據不足不得 PASS」）**：L1 出口不得以「測試數 >0、無 failure」或「passed ≥ 某數」判定——只跑一個成功案例、或全部 skip 的報告都會滿足那種條件。

- `tests/acceptance/l1_required.lock.json`：`{"schema_version":1,"sets":{"<set_id>":{"<platform>":{"selector":{"argv":[str],"paths":[str],"markexpr":str},"nodeids":[str]}}}}`；`nodeids` 依字典序、不重複、非空。`selector.argv`（R5 審查後續）是**產生該平台報告的完整命令**（pytest：`python3 -m pytest` 之後的參數；cargo：`cargo_l1_results.py -- ` 之後的 cargo test 參數，含 `--workspace`／`--package`／`--all-features`），以 `normalize_argv` 去除報告與 junit 輸出路徑值（`--junitxml=…`、`--out …`）；`paths`＋`markexpr` 則從該次收集結果篩出本 set 的 nodeid（多個 set 可共用同一個產生命令，例：`s1a_delegation` 取 `s1a_blackbox` 那次執行中 `tests/contract_blackbox/test_bb_delegation.py` 的 nodeid）。`set_id` 對應 P9 `stage_exits.json` 的 `required_l1_set`（`s0_bc`、`s0_win_pytest`、`s1a_blackbox`、`s1a_delegation`（R5 審查後續：只含 pytest 黑箱 `test_bb_delegation.py`，原定義另混 cargo 測試卻只能收一份 `l1-results.v1`；`crates/sw-cli` 的 `delegate_route`／`exec_delegate` 測試本就在 `s1a_cargo` 的 workspace selector 內，由該 set 承擔）、`s1b_20groups`、`s21_core`、`s3_release_contract`），另含各 Phase gate 自用的集合（P3 `s1a_cargo`、P4 `s1b_cargo`、P5 `s21_gate:<section>`、P6 `s22_gate:<section>`、P7 `p7_gate:<row>`）。本 Task 建立檔案與 `s0_bc`（linux、windows）、`s0_win_pytest`（windows）三組；其餘由各 owner Phase 以下列工具**只增**登記（P3 Task 18：`s1a_blackbox`、`s1a_delegation`、`s1a_cargo`；P4 Task 16：`s1b_20groups`、`s1b_cargo`；P5 Task 22：`s21_core`、`s21_gate:*`；P6 Task 13：`s22_gate:*`；P7 Task 19：`p7_gate:*`；P9 Task 1：`s3_release_contract`）。P8 Task 15 的契約覆蓋以 `coverage_map.yaml` 逐 ID 對 nodeid，並同樣拒收未完成／重複／exit 非 0 的結果檔。cargo 測試的 nodeid 為 `cargo:<crate>::<test binary>::<test name>`（P3 Task 17 `cargo_l1_results.py`）。
- 凍結方式：`python3 tests/acceptance/regen_lock.py --l1-add-only --set <id> --platform <p> --collect <檔>`，`<檔>` 為以該 set 的 `selector` 執行 `pytest --collect-only -q`（或 `cargo_l1_results.py --list-only`）的輸出；**凍結時點**（R5 審查後續：原「紅燈 commit 之後、第一次執行出口 gate 之前」晚於 §11.3「開始實作前凍結」——Phase 內任一 Task 的測試若在 gate 前被刪除、改名或弱化，凍結時已不在收集結果中，分母縮小偵測不到）＝**逐 Task**：owner Phase 的每個 Task 在其紅燈測試 commit（failing-first、早於該 Task 的實作 commit）**同一個 commit** 內執行 `--l1-add-only`，把該 Task 新增、屬於某 set selector 的 nodeid 登記進鎖檔，lock diff 隨紅燈 commit 提交；gate Task 只登記 gate 自身的新測試並確認無缺。測試先行，集合由測試定義、不由執行結果反推。既有 nodeid 不得消失：`--l1-add-only` 發現任何既有 nodeid 不在新收集結果中 → exit 1 並列出、不寫檔（改名／刪除須擁有者裁決並於 PR 說明，§11.3「不得在測試失敗後刪除案例、縮小分母」）；新增 nodeid 才寫回。`--check` 同時驗 `l1_required.lock.json`：schema、排序、無重複、每個 set 至少一個平台、每個平台 `nodeids` 非空。`--check --base <rev>`（R5 審查後續）另以 `git show <rev>:tests/acceptance/l1_required.lock.json` 讀基準：基準已有的任一 `(set, platform, nodeid)` 在 HEAD 鎖檔消失、或其 `selector` 改變 → exit 1 並列出（擁有者裁決的改名／刪除須在 PR 說明並以 `--allow-removal <nodeid>…` 逐一列名，輸出照樣列出供審閱）；`.github/workflows/tests.yml` 以 `--base origin/${{ github.base_ref }}` 在每個 PR 執行，故鎖檔只能隨紅燈 commit 只增、不能在後續 commit 被手改縮小。
- `tests/acceptance/l1_judge.py`：

```python
L1_VERDICTS = ("PASS", "FAIL", "INSUFFICIENT", "INVALID")
def set_sha256(nodeids: Sequence[str]) -> str            # sha256(json.dumps(sorted(nodeids), separators=(",", ":")).encode())
def required_for(lock: Mapping, set_id: str, platform: str) -> list[str]   # set／平台不存在 → KeyError（呼叫端轉為 INSUFFICIENT "no required set"）
def normalize_argv(argv: Sequence[str]) -> list[str]   # selector 比對用：pytest 報告去掉 argv[0]（sys.argv 的 pytest 入口）；只移除 `--junitxml=<p>`、`--junitxml <p>`、`--out <p>` 三種路徑參數，其餘逐字保留、不重排
def judge(report: Mapping, required: Sequence[str], *, platform: str, junit: Mapping[str, int] | None = None,
          selector: Mapping[str, Any] | None = None) -> dict   # selector：lock 該 set／平台的 selector（R5 審查後續）
    # 回 {"verdict", "reasons":[str], "missing":[…], "deselected_required":[…], "not_run":[…], "skipped_required":[{"nodeid","outcome","skip_kind"}],
    #     "duplicates":[…], "failed":[…], "extra":[…], "required_sha256": set_sha256(required), "counts":{"collected","executed","passed","skipped","failed"}}
```

  判定（依序；`reasons` 列出全部命中項，verdict 取最嚴重者 INVALID > FAIL > INSUFFICIENT > PASS）：
  1. `format != "l1-results.v1"`、schema 不符、`platform` 不等於參數 → `INVALID`；給了 `junit`（`{"tests","failures","errors","skipped"}`）而與報告 `results` 的列數、`failed`、`error`、`skipped＋xfailed` 計數任一不等 → `INVALID`（`"junit/l1-results mismatch"`：兩份檔不是同一次執行）；給了 `selector` 而 `normalize_argv(report.argv)`（cargo 報告為其記錄的 cargo test 參數）≠ `normalize_argv(selector.argv)` → `INVALID "selector mismatch"`（例：凍結的是 `--workspace --all-features`，報告卻只跑 `--package sw-pal`）。呼叫端（`check_evidence_index.py`、P3 `stage1a_gate.py`、P4 `check_p4_cases.py`、P5～P7 gate、P9 Task 14B）一律傳 `selector`。
  2. 任何列（必要與否）`outcome ∈ {failed, error, xpassed}` → `FAIL`（`failed` 列出）。
  3. `finished is not True` → `INSUFFICIENT "runner not finished"`；`exit_code != 0` 且未落入第 2 條 → `INSUFFICIENT "runner exit <n>"`（例：平台 SKIP 閘門升 1、collection error 2、interrupted 2、usage error 4、no tests 5）。
  4. `collected` 內同一 nodeid 出現 >1 次，或 `results` 內同一 nodeid >1 列 → `INSUFFICIENT "duplicate nodeid"`（`duplicates` 列出；不得取最好的一列）。
  5. `required` 為空 → `INSUFFICIENT "no required set"`。
  6. 每個必要 nodeid：不在 `collected` → `missing`；在 `deselected` → `deselected_required`；收集了但無 `results` 列 → `not_run`；`outcome ∈ {skipped, xfailed}`（**不論 `skip_kind`**：必要案例的任何 SKIP 都不是 PASS；平台／能力不適用的案例本來就不得列入該平台的集合）→ `skipped_required`；任一非空 → `INSUFFICIENT`。
  7. `core_portable_platform_skips` 非空 → `INSUFFICIENT "core_portable platform skip"`。
  8. `set(results 的 nodeid) != set(collected) - set(deselected)` → `INSUFFICIENT "results do not match collection"`（中途停止或報告被裁剪）。
  9. 其餘 → `PASS`。非必要的額外 nodeid（`extra`）只要沒有第 2 條的結果即不影響判定。
- `tests/acceptance/l1_judge_vectors.json`：`[{"name","report","required","platform","junit","verdict","reasons_contains":[…]}]`，至少涵蓋下表 T13 測試的每一種情境；P9 Task 14B `test_junit_judge_matches_p2_vectors` 以同一檔回歸。
- `check_evidence_index.py`：項目帶 `l1_set`（`{"set_id": str, "platforms": [str]}`）時，`present` 還不夠——對每個平台讀 `artifact_path` 下的 `<platform>/resource-report.json`（`l1-results.v1`）與同目錄 `junit.xml`，以 `l1_judge.judge(report, required_for(lock, set_id, platform), platform=…, junit=<junit 計數>)` 判定：`PASS` 才算該項通過；`FAIL` → exit 1；`INSUFFICIENT`／`INVALID` → exit 5（BLOCKED，列出 `reasons`）。`stage0_evidence_index.yaml` 的 `S0-L1-BC`（`l1_set: {set_id: s0_bc, platforms: [linux, windows]}`）與 `S0-L1-WIN-PYTEST`（`{set_id: s0_win_pytest, platforms: [windows]}`）兩項以此判定，不再只核對檔案存在與 sha256。

`.github/workflows/tests.yml` 新增：

- job `pytest-windows`（`windows-latest`，Python 3.12，每個 PR 觸發；§11.5「每個 PR 雙平台 L1」、§12.2）：`python -m pip install --disable-pip-version-check pytest==9.0.3 PyYAML==6.0.3 jsonschema==4.10.3 pyserial==3.5`；`python -m pytest -q tests -m "core_portable or windows_native"`，env `SW_RESOURCE_REPORT=resource-report-windows.json`；以 `actions/upload-artifact` 上傳報告。此 job 只證明免驅動 Windows L1，不得計為 `windows_com`。
- job `windows-com-selfhosted`（`runs-on: [self-hosted, windows, serialwrap-com]`）：觸發條件為 `workflow_dispatch` 或 PR 帶 label `run-windows-com`（`if: contains(github.event.pull_request.labels.*.name, 'run-windows-com') || github.event_name == 'workflow_dispatch'`）；步驟：`uart-stdio-bridge selftest --endpoint win-serial --com-pair $env:SW_TEST_COM_PAIR`、`python -m pytest -q tools/uart_stdio_bridge/tests -m windows_com`、`python func-test/runner.py --mode bridge --scenarios --case win-com-*`；上傳報告。runner 未註冊時 job 排隊不執行，切片／Stage 出口由 Task 12 `check_evidence_index.py` 以缺證據判 BLOCKED（不得以 job 未執行視為通過）。

### T 步驟

- [ ] **T13-1：撰寫失敗測試** `tests/test_resource_report.py`：

| 測試名 | 斷言 |
|---|---|
| `test_platform_skip_regex_matches_p0` | `resource_report.PLATFORM_SKIP_RE` 與 P0 `tests/test_resource_markers.py` 中的平台條件 regex 字串相等 |
| `test_module_platform_marks_ast` | 對暫存檔 `pytestmark = [pytest.mark.posix_only, pytest.mark.posix_pty]` 回 `{"posix_only"}`；只有 class 層 marker 的檔回空集合；不 import 該檔（檔內含 `raise SystemExit` 也不觸發） |
| `test_should_ignore_on_windows` | 模組層 `posix_only` → True；`core_portable`／混合平台（模組層無平台 marker）→ False |
| `test_windows_collection_excludes_posix_modules`（`process_rpc`） | 子程序以 monkeypatch `os.name="nt"` 的 conftest 外掛（`-p` 載入測試用 plugin 設定 `resource_report._FORCE_OS="nt"`）跑 `pytest --collect-only -q tests/test_multiagent_e2e.py tests/test_wal.py`：無 collection error；報告 `not_collected_platform` 含 `tests/test_multiagent_e2e.py` |
| `test_core_portable_platform_skip_fails_session`（`process_rpc`） | 在 `tmp_path` 建含 conftest 副本與一個模組層 `core_portable`＋`memory_only`、函式帶 `@pytest.mark.skipif(sys.platform=="win32", reason="x")` 的檔：session exit 1、報告 `core_portable_platform_skips` 含該 nodeid |
| `test_reason_text_not_used` | `core_portable` 函式以非平台條件 skip（`skipif(True, reason="windows docs pending")`）→ 不列入 `core_portable_platform_skips`（以結構而非 reason 文字判定） |
| `test_resource_report_written`（`process_rpc`） | 子程序以 `SW_RESOURCE_REPORT=<tmp>/r.json` 跑 `tests/test_wal.py`，報告 `counts.core_portable.passed > 0` |
| `test_resource_report_deployed_content`（`process_rpc`；R5-F4，failing-first） | 以 Task 10 測試的最小安裝 `tmp/"inst"`（含 `bin/serialwrap`、`release-manifest.json`、旁載 `bin/version.dll`）：子程序帶 `SERIALWRAP_CLI_EXE=<inst>/bin/serialwrap`、`SERIALWRAP_DAEMON_EXE=<inst>/bin/serialwrapd`（manifest launcher 相符；R5 審查後續）、`SW_RESOURCE_REPORT=<tmp>/r.json` 跑 `tests/test_wal.py` → 報告 `deployed_content.start` 與 `.end` 的 `bundle_tree_sha256` 皆等於 `realhw.deployed_content.tree_digests(inst)[0]`（含旁載檔的值）、`release_manifest_sha256 == sha256_file(inst/"release-manifest.json")`；不帶 `SERIALWRAP_CLI_EXE` 再跑一次 → `deployed_content is None`（鍵存在） |
| `test_resource_report_cli_without_daemon_env_unmeasured`（`process_rpc`；R5 審查後續） | 同上但不設 `SERIALWRAP_DAEMON_EXE` → `deployed_content.start.error == .end.error == "daemon_env_missing"`、digest 皆 None；設成 repo 的 `tests/contract_blackbox/bin/py-serialwrapd` → `error == "daemon_outside_install"` |
| `test_windows_com_never_counted_without_device` | 報告中 `resources.windows_com.passed` 為 0（P0 T14 第 6 條：本 Stage 無 `windows_com` 的 tests/ item） |
| `test_single_hook_definitions` | AST 掃 `tests/conftest.py`：`pytest_collection_modifyitems`、`pytest_sessionfinish`、`pytest_configure` 各至多一個頂層定義；檔中不出現 `platform_resources.yaml` |
| `test_workflow_has_windows_jobs` | 解析 `.github/workflows/tests.yml`：有 `pytest-windows`（`runs-on: windows-latest`、指令含 `-m "core_portable or windows_native"` 與 `SW_RESOURCE_REPORT`）與 `windows-com-selfhosted`（`runs-on` 含 `self-hosted` 與 `serialwrap-com`、有 label 條件） |
| `test_resource_report_l1_results_fields`（`process_rpc`；R5-F3，failing-first） | 在 `tmp_path` 建 conftest 副本＋一檔三函式 `test_a`（pass）、`test_b`（`skipif(True, reason="x")`）、`test_c`（pass），以 `-k "not test_c"` 與 `SW_RESOURCE_REPORT=<tmp>/r.json` 執行：報告 `schema_version == 2`、`format == "l1-results.v1"`、`collected == [<a>, <b>]`（`-k` 篩掉者不在 collected）、`deselected == [<c>]`、`results` 恰兩列且 `<b>` 的 `outcome == "skipped"`、`skip_kind == "other"`、`exit_code == 0`、`finished is True`、`argv` 含 `"-k"` |
| `test_resource_report_phase_outcomes`（`process_rpc`） | fixture setup 拋例外的函式 → 該列 `outcome == "error"`、`phases.setup == "failed"`；teardown 拋例外而 call 通過 → `error`；`xfail(strict=False)` 而通過 → `xpassed`；報告 `exit_code == 1` |
| `test_resource_report_interrupted_not_finished`（`process_rpc`） | 子程序第二個測試內 `raise KeyboardInterrupt`：報告存在、`finished is True` 但 `exit_code == 2`，且 `results` 只有已執行列；以 `l1_judge.judge(report, [<a>, <b>, <c>], platform=…)` → `INSUFFICIENT`，`reasons` 含 `"runner exit 2"` 與 `not_run` 非空 |
| `test_l1_judge_single_pass_not_enough`（`memory_only`；R5-F3 核心，failing-first） | 報告 `results=[{<a>, passed}]`、`collected=[<a>]`、`exit_code=0`、`finished=True`；必要集合 `[<a>, <b>, <c>]` → `verdict == "INSUFFICIENT"`、`missing == [<b>, <c>]`；同報告搭 `junit={"tests":1,"failures":0,"errors":0,"skipped":0}` 仍 `INSUFFICIENT`（舊判準 `tests>0` 會給 PASS） |
| `test_l1_judge_required_skip_any_reason_insufficient`（`memory_only`；failing-first） | 必要 `[<a>]`，`<a>` 為 `skipped` 且 `skip_kind` 分別為 `"other"`、`"capability"`、`"platform"`、`null` → 四次皆 `INSUFFICIENT`、`skipped_required[0].nodeid == <a>`；`tests=1, skipped=1` 的 junit 也不得 PASS；`<a>` 為 `xfailed` → `INSUFFICIENT` |
| `test_l1_judge_duplicates_insufficient`（`memory_only`） | `collected=[<a>, <a>]` 或 `results` 中 `<a>` 兩列（一 `passed` 一 `passed`）→ `INSUFFICIENT`、`duplicates == [<a>]`；`<a>` 兩列一 `failed` 一 `passed` → `FAIL`（不得取最好一列） |
| `test_l1_judge_deselected_and_not_run`（`memory_only`） | 必要 `<b>` 在 `deselected` → `deselected_required == [<b>]`、`INSUFFICIENT`；`<c>` 在 `collected` 但無 `results` 列 → `not_run == [<c>]`、`INSUFFICIENT`，`reasons` 另含 `"results do not match collection"` |
| `test_l1_judge_exit_and_finish`（`memory_only`） | 全部必要案例 `passed` 但 `exit_code == 5` → `INSUFFICIENT "runner exit 5"`；`exit_code == 1` 且無失敗列（平台 SKIP 閘門）→ `INSUFFICIENT`；`finished` 缺鍵或 `False` → `INSUFFICIENT "runner not finished"`；`core_portable_platform_skips == [<x>]` → `INSUFFICIENT` |
| `test_l1_judge_failure_anywhere_fails`（`memory_only`） | 必要 `[<a>]` 皆 `passed`，額外非必要 `<z>` 為 `failed` → `FAIL`、`failed == [<z>]`；額外 `<z>` 為 `skipped` → `PASS`、`extra == [<z>]` |
| `test_l1_judge_junit_mismatch_invalid`（`memory_only`） | 報告 `results` 兩列皆 `passed`，`junit={"tests":3,"failures":0,"errors":0,"skipped":0}` → `INVALID`、`reasons` 含 `"junit/l1-results mismatch"`；`format == "l1-results.v0"` → `INVALID`；報告 `platform == "windows"` 而參數 `linux` → `INVALID` |
| `test_l1_judge_empty_required_insufficient`（`memory_only`） | `required == []` → `INSUFFICIENT "no required set"`；`required_for(lock, "nope", "linux")` → `KeyError` |
| `test_l1_judge_selector_argv_mismatch_invalid`（`memory_only`；R5 審查後續） | 報告 `argv` 為 `["--package","sw-pal"]`、`selector.argv` 為 `["--workspace","--all-features"]` → `INVALID`、`reasons` 含 `"selector mismatch"`；兩者只差 `--junitxml=<p>` → 不影響判定 |
| `test_regen_lock_base_check_detects_removal`（`process_rpc`；R5 審查後續，failing-first） | 暫存 git repo：commit 1 鎖檔含 `s0_bc.linux=[a,b]`；commit 2 改為 `[a]` → `regen_lock.py --check --base HEAD~1` exit 1、stdout 含 `b`；`--allow-removal b` → exit 0 但仍列出；只增為 `[a,b,c]` → exit 0；改 `selector.argv` → exit 1 |
| `test_resource_report_plugin_works_under_confcutdir`（`process_rpc`；R5 審查後續，failing-first：報告 hook 只在 root conftest 時，`--confcutdir` 獨立執行不產生報告） | 在 `tmp/"sub"` 建 `conftest.py`（只以 `spec_from_file_location` 載入 `<repo>/tests/resource_report_plugin.py` 並 register，寫法同 P3 黑箱 conftest）與一個模組層 `core_portable` 的 passing 測試；子程序 `python3 -m pytest -q <tmp>/sub --confcutdir=<tmp>/sub -p no:cacheprovider` 帶 `SW_RESOURCE_REPORT=<tmp>/r.json` → 檔存在、`format == "l1-results.v1"`、`finished is True`、`collected` 為該 1 個 nodeid；子程序 `sys.modules` 不含 `sw_core`；完整 `pytest tests/test_wal.py` 時 `sw_resource_report` 只註冊一次（`results` 無重複列）。P3 Task 2 以真黑箱目錄另驗 `test_bb_standalone_writes_l1_report` |
| `test_l1_judge_vectors_roundtrip`（`memory_only`） | 對 `l1_judge_vectors.json` 每筆：`judge(...)["verdict"] == vec["verdict"]` 且每個 `reasons_contains` 子字串出現在 `reasons`；向量至少含 `single-pass`、`required-skip`、`duplicate`、`deselected`、`not-run`、`exit-nonzero`、`not-finished`、`junit-mismatch`、`failure-extra`、`all-pass` 十個 `name` |
| `test_l1_lock_shape_and_add_only`（`process_rpc`） | `l1_required.lock.json` 以 `l1_required.schema.json` 驗證；`s0_bc` 有 `linux`、`windows`，`s0_win_pytest` 有 `windows`，各 `nodeids` 非空、已排序、無重複，且全部存在於 `pytest --collect-only -q <selector>` 輸出；暫存副本中從收集輸出移除一個既有 nodeid 後執行 `regen_lock.py --l1-add-only --set s0_bc --platform linux --collect <檔>` → exit 1、stderr 列出該 nodeid、鎖檔位元組不變；新增一個 nodeid → exit 0 且只多該筆 |
| `test_evidence_index_l1_item_judged`（`process_rpc`；failing-first） | 暫存 Stage 0 索引把 `S0-L1-BC` 設為 `present`、sha256 正確，`artifact_path/linux/resource-report.json` 只含一個 `passed` 的必要 nodeid（其餘未執行）→ `check_evidence_index.py --stage 0` exit 5、stdout 含 `S0-L1-BC` 與 `missing`（舊規則只核 sha256 會通過此項）；改為全部必要 nodeid `passed`、`exit_code 0`、兩平台皆具 → 該項通過；其中一列改 `failed` → exit 1 |

- [ ] **T13-2：執行並確認 FAIL**：`python3 -m pytest -q tests/test_resource_report.py tests/test_l1_judge.py` → `tests/resource_report.py`／`tests/acceptance/l1_judge.py` 不存在（函式內 import）、workflow 缺 job。
- [ ] **T13-3：commit 測試**：`git commit -m "test(tests): 新增資源報告、平台 SKIP 閘門與 Windows collection 的失敗測試"`。

### I 步驟

- [ ] **I13-1：實作**：`tests/resource_report.py`、`tests/conftest.py` hook（併入既有 hook 定義；`pytest_collection_finish`／`pytest_deselected` 記錄 L1 逐案欄位）、`.github/workflows/tests.yml` 兩個 job；`tests/acceptance/l1_judge.py`、兩個 schema、`l1_judge_vectors.json`、`regen_lock.py --l1-add-only`、`check_evidence_index.py` 的 `l1_set` 判定；以 `--l1-add-only` 產生初版 `l1_required.lock.json`（`s0_bc` 的 selector＝P1 BC-01～05 契約與安全測試檔、`markexpr` 依平台為 `"core_portable or posix_only"`／`"core_portable or windows_native"`；`s0_win_pytest`＝`tests`＋`"core_portable or windows_native"`；Windows 集合以 P1 `win-pytest.sh --collect-only` 的輸出凍結）。`test_l1_judge_*`、`test_l1_lock_shape_and_add_only`、`test_evidence_index_l1_item_judged` 放在 `tests/test_l1_judge.py`，其餘在 `tests/test_resource_report.py`。
- [ ] **I13-2：執行並確認 PASS**：`python3 -m pytest -q tests/`（無新失敗，且 `core_portable_platform_skips` 為空）；在 Windows host 經 interop 跑 `py -3.12 -m pytest -q tests -m "core_portable or windows_native"` 並以 `SW_RESOURCE_REPORT` 保存報告於 `~/b-log/rust-refactor/p2/win-core-portable.json`（Windows Python 無依賴時先 `py -3.12 -m pip install --user PyYAML==6.0.3 pytest==9.0.3 jsonschema==4.10.3`，需擁有者同意；同意前記「證據不足」）。若有 `core_portable` item 在 Windows 失敗，依 P0 T14「重新分類的限制」處理（只有測試本身直接依賴 POSIX 設施且能指出具體行時才可改標，需獨立審閱；失敗指向 `sw_core/**` 時保留 FAIL 並開修復項），不得為了綠燈改成 `posix_only`。Windows 報告另以 `l1_judge.judge(report, required_for(lock, "s0_win_pytest", "windows"), platform="windows")` 判定，結果與 `reasons` 寫入同目錄 `l1-verdict.json`；非 `PASS` 不得記為 S0-L1-WIN-PYTEST 證據（R5-F3）。
- [ ] **I13-3：commit**：`git commit -m "feat(tests): 新增 pytest 資源報告、平台 SKIP 閘門與 Windows L1 CI job"`＋fragment。

---
## Task 14：L2／L3 平台 adapter（pin＋版本 gate、PID／RSS、bench lock、能力式 SKIP、Telnet human frontend、worker 存活事件）

**Test author model:** haiku-xhigh　**Implementer model:** codex-gpt-6-luna-max
**Changelog fragment:** `changelog.d/rust-p2-l2-l3-platform-adapters.md`（`type: feat`、`scope: regression`）
**BLOCKED-OWNER evidence:**
- Linux live bench：以 pinned `serialwrap_exe` 執行 `testpilot run serialwrap_regression`（非破壞集合）、`python3 -m realhw --tier p0,p1`、`testpilot run serialwrap_reliability` 的報告（bench 互斥、真板）。
- Windows bench（Spike 7）：Telnet frontend 下的 F5、`p0-console-raw`、`p1-con-*`、`lr-mixed` 實跑；Windows 共用 bench lock 與其他 TestPilot 套件實際共鎖的證據；Windows RSS 觀測值。
- 破壞性案例（F9、F10、`p1-rst-*`、`p1-ho-*`、`p1-hp-*`）的授權執行。
- OD-15：上列 bench 執行前，同一組 regression／realhw 非破壞集合先以 Task 17 `scripts/docker/run-l15.sh realhw-p0p1`、`run-l15.sh regression-nondestructive` 在 docker 兩容器拓樸跑過（報告 `evidence_layer: "L1.5-docker"`）；docker 結果只作前置篩檢，不計為真板 L2／實機 L3。

**Files:**
- Create: `realhw/frontends.py`、`realhw/procobs.py`
- Modify: `realhw/harness.py`（`Ctx.human`）、`realhw/preflight.py`（`version_gate`、`resolve_serialwrap_exe`、`bench_lock_path(cfg)`、跨平台 `acquire_benchlock`、Windows 的 `_tools_missing`；`_leaked_daemons`／`_other_pytest`／`_external_testpilot` 改用 `procobs.list_processes()`（不再直接 `pgrep`，Windows 上不得 `FileNotFoundError`）；`_state_polluted` 改用 `resolve_state_dir(cfg)`（Windows：`%LOCALAPPDATA%\serialwrap` 或 cfg 覆寫，不寫死 `~/.local/state`）；跨 OS 守衛 `cross_os_holder_check`；`Checks.version_problem`／`exe_identity`／`cross_os_guard`）、`realhw/__main__.py`（`main(argv: list[str] | None = None)`；pin、版本 gate、`meta.started_at_utc`、`meta.cli_identity`、`meta.deployed_content`（R5-F4：`resolve_serialwrap_exe` 之後、第一個 case 之前量 start，`write_reports` 之前量 end，皆以 Task 10 `realhw.deployed_content.measure(resolved)`；R5 審查後續：`meta.daemon_identity` 取得 `health.status.pid` 後立即 `live_daemon(pid, install_dir)`，寫入 `meta.deployed_content.daemon_live`（daemon 不是本工具 spawn，受測 daemon 映像只能由行程本身量測））、`meta.contract_revision`、`meta.cases_yaml_sha256`、`meta.daemon_identity`、frontend factory、能力計算）、`realhw/config.json`（新增 `serialwrap_exe: ""`、`bench_lock_path: ""`、`human_frontend: "auto"`、`cross_os_probe_argv: []`）
- Modify: `realhw/cases/longrun.py`（PID 取 `daemon status.pid` 且以 `procobs.pid_alive` 判死亡、RSS 用 `procobs.rss_kb`、human worker 用 `ctx.human`、每個 agent worker 以 `--source agent:rhw{i}` 提交、worker 存活事件、事件加 `wall_ts`、background 結果於執行期以有界 pending 集合收斂並即時寫入 events 帳本（`BgResultTracker`；收尾只處理仍未結案者）、刪 `FOREGROUND_BUSY` 說明）、`realhw/drivers.py`（`SwCli.submit_and_wait(..., source: str | None = None)` 帶 `--source`）、`realhw/cases/p1_cmd.py:35`（容許碼移除 `FOREGROUND_BUSY`，`GAP-G-12`）、`realhw/cases/p0.py`（`p0-console-raw`）、`realhw/cases/p1_console.py`（7 個；`p1-con-liveness`／`p1-con-orphan` 以 `h.crash()` 取代 `pgrep minicom`＋`kill -9`）、`realhw/cases/p1_wal.py`（2 個）、`realhw/cases/p1_restart.py`（`p1-rst-reboot`）：`requires` 由 `tmux` 改為 `human_frontend`，操作改用 `ctx.human`
- Modify（補齊實際依賴，使 Windows 能力式 SKIP 生效；現行 `requires` 只有 `("two_boards",)`）：`realhw/cases/p1_restart.py`（`p1-rst-daemon` 加 `supervisor_runtime`）、`realhw/cases/p1_handoff.py`（`p1-ho-cycle` 加 `tmux`（以 `tmux` 起 `minicom -D` 外部持有者，`p1_handoff.py:48-51`）；`p1-ho-persist` 加 `supervisor_runtime`）、`realhw/cases/p1_hotplug.py`（`p1-hp-cycle` 加 `usbipd`；`p1-hp-reorder` 加 `usbipd`、`supervisor_runtime`）
- Modify: `regression/serialwrap_regression/plugin.py`（移除 `os.name=="nt"` 硬性 SKIP，改能力式；prepare 時以 pinned `serialwrap_exe` 量 `run_meta["deployed_content_start"]`，R5-F4）、`regression/serialwrap_regression/reporter.py`（`build_reports` 在 `write_reports` 前量 end，寫 `meta.deployed_content = bracket(start, end, live=[prepare 時以 `daemon status` 的 pid 量的 live_daemon])`；start 缺漏時 `meta.deployed_content` 仍寫出且 `start` 為 `{"error":"not_measured", …digest 皆 null}`，R5-F4）、`regression/serialwrap_regression/preflight.py`（`version_gate` 改由 realhw 匯入、訊息不變；Windows lock 用 `bench_lock_path(cfg)`；回傳平台 `missing_caps`）、`regression/serialwrap_regression/core.py`（`build_ctx` 加 human factory；`platform_missing_caps`）、`regression/serialwrap_regression/guards.py`（`UBootConsole` 改吃 `HumanFrontend`；`_TA_ENV_DIRS` 補 `SERIALWRAP_LOG_DIR`、`SERIALWRAP_EVENTS_DIR`、`SERIALWRAP_EVENTS_RUNTIME_DIR`，與 `tests/conftest.py` 的 9 目錄一致（另保留 `SERIALWRAP_PROFILE_DIR`），避免 throwaway daemon 把 log／events 寫到 live 目錄）、`regression/serialwrap_regression/cases/f05_console_coexist.py`、`f09_boot_uboot.py`（改用 `ctx.human`，`requires=("human_frontend",)`）、`f08_daemon_singleton.py`（second-daemon）與 `f10_login_creds.py`（兩個）`requires` 加 `posix_throwaway_daemon`、`f02_backpressure.py`（RSS 用 `procobs`）、`testbed.yaml.example`（`bench_lock_path`、`human_frontend`）
- Modify: `reliability/serialwrap_reliability/core.py`（`SwCli(exe=...)`、版本 gate）、`reliability/serialwrap_reliability/testbed_loader.py`（`_SECTION_KEYS` 加 `serialwrap_exe`、`bench_lock_path`、`human_frontend`）、`reliability/testbed.yaml.example`
- Modify: `docs/regression-plugin.md`（Windows 能力式 SKIP、frontend、bench_lock_path）
- Modify（介面變更須同步、預告斷言更新，審查確認語意不變）：`tests/test_realhw_main.py`（stub 的 `bench_lock_path` 改為接受 `cfg`／`os_name`；`daemon status` stub 補 `version`；改以 `main([...])` 傳 argv）、`tests/test_realhw_longrun.py`（snapshot pid 來源、`--source` 參數）、`tests/test_realhw_drivers.py`（`submit_and_wait` 的 argv 多出 `--source`）
- Create: `tests/test_realhw_frontends.py`、`tests/test_realhw_procobs.py`、`tests/test_realhw_pin_gate.py`、`tests/test_regression_windows_caps.py`、`tests/test_reliability_pin.py`、`tests/test_no_foreground_busy.py`、`tests/test_realhw_longrun_sources.py`（全部建立時即依 P0 T14 帶 `pytestmark`；含 `"tmux"` 字串但不啟動 tmux 者加 `RESOURCE_AUDIT_WAIVERS = {"interactive_desktop": "只檢查命令字串組裝，不啟動 tmux"}`；開 fake Telnet server 的函式標 `process_rpc`）

**Interfaces:**

Consumes：Task 10 `realhw/telnet_client.py`。

```python
# realhw/frontends.py（stdlib-only）
class FrontendError(RuntimeError): ...
KEYMAP_TELNET: dict[str, bytes] = {"Enter": b"\r\n", "Tab": b"\t", "Up": b"\x1b[A", "Down": b"\x1b[B",
                                   "C-c": b"\x03", "C-u": b"\x15"}
class HumanFrontend(Protocol):
    kind: str                                # "tmux-minicom" | "tmux-reader" | "telnet"
    def open(self, com: str) -> None
    def alive(self) -> bool
    def send_line(self, text: str) -> None   # 文字＋Enter
    def send_text(self, text: str) -> None   # 不加 Enter
    def send_key(self, key: str) -> None     # KEYMAP_TELNET 的 key 名
    def capture(self) -> str                 # 去 ANSI 後的近期輸出
    def client_id(self) -> str | None
    def close(self) -> None                  # 正常關閉（tmux kill-session／Telnet FIN）
    def crash(self) -> None
        # 對端異常消失：tmux 模式 SIGKILL minicom 程序（取代 case 內 `pgrep minicom`＋`kill -9`）；
        # telnet 模式設 SO_LINGER(1,0) 後 close 送 RST。兩者語意不同：Windows 無孤兒 PTY，
        # 依賴孤兒語意的 case（p1-con-liveness、p1-con-orphan、f5-console-peer-gone-recycled）在 Windows 登記 gap
class TmuxMinicomFrontend:                   # 既有路徑：tmux new-session "serialwrap-minicom <com>"
    def __init__(self, tmux: Any, tag: str) -> None
class TmuxReaderFrontend:                    # 既有路徑：console-attach 取 vtty 後 tmux "cat <vtty>"
    def __init__(self, tmux: Any, sw: Any, tag: str, *, label: str = "realhw") -> None
class TelnetFrontend:                        # Windows：console-attach 取 TCP endpoint，以 TelnetClientCodec 連線
    def __init__(self, sw: Any, tag: str, *, label: str = "realhw", connect_timeout_s: float = 5.0,
                 buffer_limit_bytes: int = 1_048_576) -> None
def select_frontend_kind(cfg: dict, *, os_name: str | None = None) -> str
    # cfg["human_frontend"] ∈ {"auto","tmux","telnet"}；auto：os.name=="nt" → "telnet"，否則 "tmux"
def make_frontend_factory(cfg: dict, *, tmux: Any, sw: Any, os_name: str | None = None) -> Callable[[str, str], HumanFrontend]
    # factory(tag, role)；role ∈ {"minicom","reader"}；tmux 模式依 role 選 Minicom／Reader；telnet 模式兩者皆 TelnetFrontend
def frontend_capability(cfg: dict, *, os_name: str | None = None, which: Callable[[str], str | None] = shutil.which) -> tuple[bool, str]
    # tmux：需 tmux 與 minicom（role=minicom）；telnet：恆 (True, "telnet")

# realhw/procobs.py（stdlib-only；Windows 以 ctypes psapi）
def rss_kb(pid: int) -> int                  # Linux：/proc/<pid>/status VmRSS；Windows：K32GetProcessMemoryInfo.WorkingSetSize // 1024；失敗回 0
def pid_alive(pid: int) -> bool
def list_processes() -> list[dict[str, Any]] | None
    # [{"pid","name","cmdline"}]；Linux 讀 /proc/*/cmdline；Windows 以 ctypes ToolHelp32（CreateToolhelp32Snapshot）取 pid／exe 名，
    # cmdline 以 `wmic`／CIM 取不到時為 null；完全無法列舉回 None（呼叫端記「證據不足」，不得當作「沒有其他程序」）

# realhw/preflight.py（新增／變更）
class BenchLockConfigError(RuntimeError): ...
def version_gate(cli: str, daemon: str) -> str | None        # 自 regression/preflight.py 原樣移入；兩邊訊息逐字不變
def resolve_serialwrap_exe(cfg: dict) -> tuple[str, dict[str, Any]]
    # cfg["serialwrap_exe"] 非空 → 展開 ~、realpath，{"configured", "resolved", "sha256", "pinned": True}
    # 空 → shutil.which("serialwrap")，{"configured": None, "resolved", "sha256", "pinned": False}（相容既有 PATH 行為，但報告標示未 pin）
def bench_lock_path(cfg: dict | None = None, *, os_name: str | None = None) -> Path
    # cfg["bench_lock_path"] 非空 → 展開；POSIX 未設 → ~/.local/state/serialwrap/bench.lock；Windows 未設 → raise BenchLockConfigError
def acquire_benchlock(path: Path) -> int | None              # POSIX flock LOCK_EX|LOCK_NB；Windows os.open(O_RDWR|O_CREAT|O_BINARY)+msvcrt.locking(LK_NBLCK,1)；被占用回 None
def resolve_state_dir(cfg: dict, *, os_name: str | None = None) -> Path      # cfg["state_dir"] → 展開；POSIX ~/.local/state/serialwrap；Windows %LOCALAPPDATA%\serialwrap
def cross_os_holder_check(cfg: dict, boards: list[str], *, run: Callable = subprocess.run) -> tuple[str, str | None]
    # §15.2「跨主機／跨 OS 單機檔案鎖不足」：WSL 的 bench.lock 與 Windows bench_lock_path 互不相干。
    # cfg["cross_os_probe_argv"] 非空（例：WSL 端設 ["cmd.exe","/c","serialwrap","daemon","status"]、Windows 端設 ["wsl.exe","-e","serialwrap","daemon","status"]）時執行；
    # 另一側 daemon 回應中任一 session 的 device 與本 bench 板相同且 state ∉ {DETACHED, RELEASED} → ("held", 說明)（整場拒跑）；
    # 探測失敗 → ("unknown", 錯誤)（記入 meta、報告標示，不拒跑也不宣稱安全）；未設定 → ("unconfigured", None)（README 載明此時互斥完全依擁有者排程，OD-10）
def platform_missing_caps(*, os_name: str | None = None, frontend_ok: bool, which: Callable = shutil.which) -> dict[str, str]
    # Windows：{"tmux":"cap_missing:tmux","posix_throwaway_daemon":"cap_missing:posix_throwaway_daemon",
    #           "usbipd":"cap_missing:usbipd","supervisor_runtime":"cap_missing:supervisor_runtime"}；
    # frontend_ok 為 False 時加 {"human_frontend":"cap_missing:human_frontend"}；POSIX：依 which("tmux") 等實際偵測
# Checks 新增欄位：version_problem: str | None = None、exe_identity: dict = field(default_factory=dict)
# evaluate()：version_problem 非 None → problems 加入該訊息（整場拒跑，#154）

# realhw/harness.py
@dataclasses.dataclass
class Ctx:                                   # 新增最後一個欄位，預設 None，既有建構呼叫不受影響
    ...
    human: Callable[[str, str], Any] | None = None
```

`lr-mixed` 變更（計數口徑不變，`TST-D-12`；以下屬工作負載 revision 變更，`longrun_acceptance.yaml` 的 `workload.engine_files` sha256 會隨之改變，兩平台基線與候選必須使用同一 engine revision，Task 15 `baseline_compatible` 檢查）：

1. **TX 來源**：每個 agent worker 以 `source=f"agent:rhw{i}"` 提交——`_do_line` 經 `ctx.sw.submit_and_wait(board, cmd, cmd_timeout=10.0, source=source)`，`_do_background` 的 `cmd submit` argv 加 `--source source`，`_do_interactive` 已以 `--owner source`。現行未帶 `--source` 時 CLI 預設 `agent`（`sw_core/cli.py:2129`），WAL TX source 全為 `agent`，四個 worker 混成同一來源，`allowed_tx_source_regex` 與 walscan 逐來源判定都無法成立。
2. **PID 與死亡判定**：snapshot 的 `pid` 取 `ctx.sw.run("daemon","status")` 的 `pid`（取代 system-scope `systemctl show`）；RPC 失敗（`_error`／`TIMEOUT`／缺 `pid`）時記 `pid: null` 與 `pid_probe_error`，**不得**記 0。只有「連續 3 次 probe 失敗且 `procobs.pid_alive(last_pid) is False`」或「probe 成功但 pid 與上次不同」才分別判 `daemon_death`／重啟；暫時性 RPC 失敗不停整個長跑。`analyze` 的 `pid_changes`／`daemon_death_at` 把 `null` 視為「無觀測」（跳過，不算變更、不算死亡）。
3. **RSS**：`rss_kb` 改 `procobs.rss_kb(pid)`（pid 為 null 時記 null）。
4. **human**：`h = ctx.human("lrhuman", "minicom")`，`alive()` 為 False 時記 `minicom_dead`（事件名不變，維持 `analyze` 相容）並 `close()`＋`open("COM0")`（記 `minicom_open`），每 `uniform(120,300)` 秒 `h.send_line(f"echo HUMAN_TICK_{n}")` 並記 `tick{n}`。
5. **worker 存活（§15.11.3「測試 worker 存活情況」）**：每個 worker（`agent:rhw0..N-1`、`human`、`orchestrator` snapshot thread、第 7 點的 `bg_resolver` thread）啟動時記 `worker_start`、結束時（含例外）記 `worker_stop{reason}`，並每 300 s 記 `worker_alive`；`analyze` 不讀這些 kind（輸出鍵不變）。
6. **事件時間**：每筆 events／snapshots 另加 `wall_ts`（UTC ISO，`datetime.now(timezone.utc).isoformat()`），供 Task 15 把 WAL `wall_ts` 與事件對齊；既有 `t` 欄位不變。
7. **background 終態（執行期收斂，不得延至長跑結束才查）**：產品只保留 `CMD_HISTORY_MAX=512` 筆命令紀錄，`_evict_commands_locked` 依 `done_at` 淘汰最舊的已完成紀錄，之後 `cmd status` 回 `{"ok":false,"error_code":"CMD_NOT_FOUND"}`（`sw_core/arbiter.py:233-251`；Rust 依 P6 Task 8 `history_evicts_oldest_done_by_done_at_string` 照搬）。`lr-mixed` 4 個 worker 每輪間隔 `uniform(0.5,2.0)` s，最壞約 8 cmd/s，512 筆約 64 s 就輪替完畢；48 h 有數十萬筆，若收尾才查，絕大多數 background 紀錄早已淘汰。因此：
   - `_do_background` 拿到 `cmd_id` 時記 `submit_accepted{cmd_id}`（不再直接記 `done`），並呼叫 `bg.add(cmd_id, source=, com=, round_=n, stop=stop)` 放入有界 pending 集合。
   - 獨立 `bg_resolver` thread（第 5 點的 worker 之一）每 `BG_POLL_INTERVAL_S=1.0` s 對 pending 由舊到新各呼叫一次唯讀 `cmd status --cmd-id`（不觸碰 Lease）。`command.status ∈ {done, error, canceled}` → **立即**寫 `bg_result{cmd_id, status, error_code, latency_s}` 到 `events.ndjson`，並移出 pending。`events.ndjson` 是 append＋flush 的測試端持久帳本。`bg_result` 的 `source`／`com`／`round` 沿用原 submit 的值，讓 gate 的 `pair_key` 對得上。`accepted`／`running` 留在 pending。若 age 超過 `BG_RESOLVE_WARN_S=30` 仍未終態，記一次 `bg_slow{cmd_id, age_s}`；此時命令仍在途，產品不會淘汰它。缺 `status` 或出現未知值時，記一次 `bg_poll_error{cmd_id, detail}` 並繼續留在 pending。RPC 失敗（`_error`／`TIMEOUT`）同樣處理，下一輪重試。
   - 回 `CMD_NOT_FOUND` → 記 `bg_lost{cmd_id, reason:"CMD_NOT_FOUND", age_s}`，並移出 pending；**不得**記 `bg_result`／`done`。該 pair 在 gate 計為 unresolved，並觸發 Task 15 的 `background_result_coverage`，標示為測試端收斂落後。
   - pending 上限 `BG_PENDING_MAX=64`（不到 512 的 1/8）。集合滿時 `add` 阻塞 worker，直到有空位或 `stop` 被設定，並記 `bg_backpressure{source, wait_s}`。這是測試端背壓，不丟棄已受理的 cmd_id。`stop` 已設定時照樣加入；可能超過上限，但超出量不多於 agent worker 數，交給收尾處理。
   - 收尾：stop 且所有 worker join 之後，`bg.final_drain(BG_FINAL_DRAIN_S=30)` **只**查詢仍在 pending 的 cmd_id，方式與上面相同。逾時仍未終態者不補寫任何紀錄，留為 unresolved，並記 `bg_drain{remaining}`。
   - **不得**為了長跑調大產品 history，例如改 `CMD_HISTORY_MAX`、加環境變數或 CLI 旗標；也不得把 `CMD_NOT_FOUND` 或非終態當成功。

```python
# realhw/cases/longrun.py（stdlib-only；新增）
BG_PENDING_MAX = 64
BG_POLL_INTERVAL_S = 1.0
BG_RESOLVE_WARN_S = 30.0
BG_FINAL_DRAIN_S = 30.0
BG_TERMINAL = frozenset({"done", "error", "canceled"})
class BgResultTracker:
    def __init__(self, sw: Any, elog: "_EventLog", *, pending_max: int = BG_PENDING_MAX,
                 clock: Callable[[], float] = time.monotonic) -> None
    def add(self, cmd_id: str, *, source: str, com: str, round_: int, stop: threading.Event) -> None
        # 滿則阻塞（可被 stop 中斷；中斷後仍加入）；阻塞過即記 bg_backpressure
    def poll_once(self) -> int          # pending 由舊到新各查一次；回本輪移出數（bg_result＋bg_lost）
    def run(self, stop: threading.Event) -> None   # worker_start／worker_alive／worker_stop（source="bg_resolver"）；每 BG_POLL_INTERVAL_S poll_once
    def final_drain(self, timeout_s: float = BG_FINAL_DRAIN_S) -> int   # 只查剩餘 pending；回逾時後剩餘數並記 bg_drain
    def pending_count(self) -> int
    max_pending_seen: int               # 測試用：觀測到的 pending 峰值
```

regression Windows：`plugin.execute_step` 不再判斷 `os.name`：刪除 `plugin.py:125-126` 的 `os.name=="nt"` 覆寫，保留既有 `core.runtime_skip(meta, self._missing_caps, self._broken_by, allow_destructive=...)` 呼叫（`core.py:86` 的實際簽章，不改），只在 `setup` 時把 `preflight` 依 `platform_missing_caps` 算出的平台缺項併入 `self._missing_caps`；SKIP 的 `reason_code` 為 `cap_missing:<cap>`、`category="environment"`。`f9-brcm-spaced-prompt-reboot-login` 以外原本在 Windows 一律 SKIP 的案例，凡 `requires` 不含缺少能力者改為實際執行。

### T 步驟

- [ ] **T14-1：撰寫失敗測試**。關鍵：

```python
def test_telnet_frontend_send_line_and_capture(fake_telnet_server, stub_sw):
    stub_sw.responses[("session", "console-attach")] = {"ok": True, "client_id": None, "label": "realhw",
        "vtty": f"127.0.0.1:{fake_telnet_server.port}", "endpoint": f"127.0.0.1:{fake_telnet_server.port}",
        "transport": "tcp", "protocol": "telnet"}
    h = frontends.TelnetFrontend(stub_sw, "t1")
    h.open("COM0")
    try:
        h.send_line("echo HUMAN_TICK_1")
        h.send_key("Tab")
        fake_telnet_server.send(b"\x1b[32mroot@prplOS:/# \x1b[0mecho HUMAN_TICK_1\r\n")
        assert fake_telnet_server.wait_received(b"echo HUMAN_TICK_1\r\n\t", timeout_s=2)
        assert wait_until(lambda: "root@prplOS:/# echo HUMAN_TICK_1" in h.capture(), 2)
        assert h.alive() is True
        fake_telnet_server.close_client()
        assert wait_until(lambda: h.alive() is False, 2)
    finally:
        h.close()
```

其餘：

| 測試名（檔案） | 斷言 |
|---|---|
| `test_select_frontend_kind`（frontends） | `auto`＋`os_name="nt"` → `telnet`；`auto`＋`posix` → `tmux`；`"telnet"` 強制 → `telnet` |
| `test_factory_roles_tmux` | tmux 模式 `factory("x","minicom")` 為 `TmuxMinicomFrontend`、`factory("x","reader")` 為 `TmuxReaderFrontend` |
| `test_telnet_requires_tcp_endpoint` | console-attach 回 POSIX `vtty="/dev/pts/3"`（無 `transport`）→ `open` raise `FrontendError` |
| `test_telnet_iac_escaped` | `send_text("a\u00ff")` 在 server 收到 `b"a\xc3\xbf"`（UTF-8 後無 0xFF）；`send_text` 直接送 `b"\xff"` 的內部 API → server 收到 `b"\xff\xff"` |
| `test_capture_bounded` | server 送 3 MiB，`capture()` 長度 ≤ `buffer_limit_bytes` 解碼後字元數 |
| `test_rss_kb_self`（procobs） | `rss_kb(os.getpid()) > 0`；`rss_kb(0)==0`；`rss_kb(2**22+12345)==0` |
| `test_rss_kb_windows_branch` | monkeypatch `os.name="nt"` 並注入假 `ctypes` psapi 回 `WorkingSetSize=10485760` → 10240 |
| `test_version_gate_moved_message_identical`（pin_gate） | `realhw.preflight.version_gate("serialwrap 0.3.2","0.3.3")` 等於 `"client↔daemon 版本不齊：cli=0.3.2 daemon=0.3.3（#154 gate，suite-refuse）"`；`regression` 的 `version_gate is realhw.preflight.version_gate` |
| `test_resolve_exe_pinned_and_unpinned` | 設 `serialwrap_exe` 指向 tmp 可執行檔 → `pinned True`、`sha256` 正確；未設 → `pinned False` |
| `test_realhw_main_refuses_version_mismatch` | stub `SwCli`：`--version` 回 `serialwrap 0.3.2`、`daemon status.version` 回 `0.3.1` → `realhw.__main__.main(["--tier","p0","--report-dir",str(tmp)])`（新簽章 `main(argv=None)`）回 2，報告 problems 含 `#154 gate` |
| `test_meta_started_at_utc` | `main` 產生的 `report.json` `meta.started_at_utc` 符合 `^\d{4}-\d\d-\d\dT\d\d:\d\d:\d\d(\.\d+)?\+00:00$`，且 `meta.cli_identity.pinned`、`meta.contract_revision`（40 hex 或 null）、`meta.cases_yaml_sha256`、`meta.daemon_identity`（`pid`、`version`、`image_path`、`image_sha256`，後兩者取自 `live_daemon`，R5 審查後續）存在 |
| `test_realhw_report_deployed_content`（pin_gate；R5-F4，failing-first） | `serialwrap_exe` 指向 Task 10 測試的最小安裝（含旁載 `bin/version.dll`）的 `bin/serialwrap`，stub 其餘 RPC：`main([...])` 產生的 `report.json` `meta.deployed_content.start`／`.end` 皆存在，`bundle_tree_sha256 == realhw.deployed_content.tree_digests(inst)[0]`、`cli_resolved == meta.cli_identity.resolved`、`cli_sha256 == meta.cli_identity.sha256`；monkeypatch `measure` 記錄呼叫序 → start 早於第一個 case 執行、end 晚於最後一個 case 且早於 `write_reports`；stub `daemon status.pid` 指向一個 `sys.executable` 子行程 → `meta.deployed_content.daemon_live[0].in_install is False`（R5 審查後續：ingest 據此拒收） |
| `test_regression_report_deployed_content`（windows_caps；R5-F4） | 以 stub run_result 呼叫 `RegressionReporter.build_reports`：輸出 `report.json` 的 `meta.deployed_content` 有 `start`（取自 `plugin.run_meta["deployed_content_start"]`）與 `end`（`build_reports` 內量測）；`run_meta` 無 start 時 `meta.deployed_content.start.error == "not_measured"` 且三個 digest 為 None（鍵仍存在，ingest 據此拒收而非回填） |
| `test_main_argv_default_uses_sys_argv` | `main()` 不帶參數時解析 `sys.argv[1:]`（既有以 monkeypatch `sys.argv` 的測試仍可用） |
| `test_bench_lock_path_windows_requires_key` | `bench_lock_path({}, os_name="nt")` raise `BenchLockConfigError`；給 `C:/bench/bench.lock` 回該路徑 |
| `test_acquire_benchlock_exclusive_posix` | 同一路徑第二次 `acquire_benchlock` 回 None |
| `test_platform_missing_caps_windows` | `os_name="nt", frontend_ok=True` → 恰含 `tmux`、`posix_throwaway_daemon`、`usbipd`、`supervisor_runtime` 四鍵，值為 `cap_missing:<鍵>` |
| `test_lr_mixed_pid_from_daemon_status` | 以 stub ctx 跑 `lr-mixed` 2 秒（duration 2s、snapshot 1s）：`snapshots.ndjson` 的 `pid` 等於 stub `daemon status.pid`；未呼叫 `ctx.systemd.main_pid` |
| `test_lr_mixed_rpc_timeout_not_death` | stub `daemon status` 第 2 次回 `{"ok":false,"error_code":"TIMEOUT"}`、`procobs.pid_alive` 回 True：該筆 snapshot `pid is None` 且有 `pid_probe_error`；長跑未停、無 `major daemon_death`；`analyze` 的 `pid_changes==0`、`daemon_death_at is None` |
| `test_lr_mixed_death_requires_consecutive_and_dead_pid` | 連續 3 次 probe 失敗且 `pid_alive(last)` 為 False → `major daemon_death`；連續 3 次失敗但 `pid_alive` True → 不判死亡 |
| `test_lr_mixed_agents_use_distinct_sources`（longrun_sources） | stub `SwCli` 記錄 argv：line／background 的 `cmd submit` 皆含 `--source agent:rhw<i>`，四個 worker 的 source 兩兩不同；interactive-open 的 `--owner` 與該 worker source 相同 |
| `test_lr_mixed_worker_liveness_events` | 2 秒短跑後 events 中每個 worker 各有一筆 `worker_start` 與 `worker_stop`；每筆事件與 snapshot 含 `wall_ts` |
| `test_background_results_survive_history_eviction`（longrun_sources；先寫、先紅） | 測試端 fake `EvictingCmdStore(history_max=512)` 照 `sw_core/arbiter.py:233-251` 實作：每次 submit 建一筆紀錄，完成時寫遞增 `done_at`；總數 >512 時依 `done_at` 淘汰最舊已完成者；被淘汰的 id 由 `cmd status` 回 `{"ok":false,"error_code":"CMD_NOT_FOUND","cmd_id":id}`。這個 fake 包成 stub `sw.run`。負載共 1,600 筆：1,100 筆 line 填充命令（submit 即 `done`）與 500 筆 background（submit 後第 1 次 `cmd status` 回 `running`、第 2 次起回 `done`）交錯送出；每送 8 筆呼叫一次 `tracker.poll_once()`，最後 `final_drain()`。斷言：(a) `store.evicted_count >= 1600 - 512`，證明確實發生淘汰；(b) 500 個 background `cmd_id` 各恰有 1 筆 `bg_result` 且 `status=="done"`；(c) `bg_lost` 為 0 筆；(d) `tracker.max_pending_seen <= BG_PENDING_MAX`，且 `<= 16`；(e) `final_drain` 期間呼叫 `cmd status` 的次數等於呼叫前的 `pending_count()`（≤ 8），收尾只處理未完成項；(f) `store.history_max == 512`，stub 收到的 argv 不含 `history` 字樣，`longrun.py` AST 中沒有 `CMD_HISTORY_MAX` 的賦值或 env 覆寫；(g) 對照組：用同一 store 與負載、全程不 poll、只在最後 `final_drain()`，得到 `bg_lost` ≥ 400 筆，且這些 `cmd_id` **沒有**任何 `bg_result`／`done`，證明查無不會被當成功 |
| `test_bg_pending_bounded_backpressure` | `pending_max=4`，store 一律回 `running`：前 4 次 `add` 立即返回；第 5 次在 thread 中阻塞。另一 thread 0.2 s 後讓 store 完成第 1 筆並 `poll_once()`，第 5 次 `add` 才返回。events 有 1 筆 `bg_backpressure` 且 `wait_s >= 0.2`；任一時刻 `pending_count() <= 4`；已受理的 cmd_id 全部保留，沒有任何一筆被丟棄 |
| `test_bg_result_carries_submit_pair_key` | `add("c1", source="agent:rhw2", com="COM1", round_=9)` 後 store 回 `done`：`bg_result` 的 `source=="agent:rhw2"`、`com=="COM1"`、`round==9`、`cmd_id=="c1"`、`latency_s >= 0`；回 `error` 與 `canceled` 時 `status` 原樣記錄、`error_code` 照抄 |
| `test_bg_lost_never_counted_done` | store 對 `c1` 回 `CMD_NOT_FOUND`：記 1 筆 `bg_lost{cmd_id:"c1", reason:"CMD_NOT_FOUND"}`，沒有 `bg_result`／`done`；`pending_count()==0`；再 `poll_once()` 不再查 `c1` |
| `test_bg_poll_error_keeps_pending` | 第 1 次 `cmd status` 回 `{"_error":"TIMEOUT"}`，第 2 次回 `done`：第 1 輪後 `c1` 仍在 pending，`bg_poll_error` 恰 1 筆；第 2 輪記 `bg_result status=done`。未知 `status:"weird"` 持續出現 → `bg_poll_error` 只記 1 筆，`c1` 一直留在 pending 直到收尾 |
| `test_final_drain_only_pending` | 23 筆 add：20 筆已在執行期以 `bg_result` 結案，3 筆一直 `running`。`final_drain(timeout_s=0.5)` 只對這 3 個 id 呼叫 `cmd status`，已結案的 20 筆零查詢；回傳 3；記 `bg_drain{remaining:3}`；這 3 筆沒有 `bg_result` |
| `test_lr_mixed_background_resolved_during_run` | `lr-mixed` 以 stub ctx 跑 2 s：background 送出得 `cmd_id`，stub `cmd status` 第 1 次就回 `done`。每個 `bg_result` 的 `t` 都小於 `stop` 事件的 `t`，代表在執行期寫入，不是收尾才查；events 有 `bg_resolver` 的 `worker_start` 與 `worker_stop`；不存在只有 `submit_accepted`、卻已在 store 標為 `done` 而未寫 `bg_result` 的 cmd_id |
| `test_lr_mixed_human_uses_frontend` | 同上，stub human frontend 收到 `send_line("echo HUMAN_TICK_<n>")`；`analyze` 輸出鍵不變（`per_source`、`stuck_attached`、`pid_changes`、`daemon_death_at`、`rss_trend`） |
| `test_ported_cases_require_human_frontend` | realhw `p0-console-raw`、7 個 `p1-con-*`、`p1-wal-reset`、`p1-wal-fullrun`、`p1-rst-reboot`、`lr-mixed` 的 `requires` 含 `human_frontend` 且不含 `tmux`；`p0-blog-clean` 仍含 `tmux`；`p1-ho-cycle` 的 `requires` 由本 Task **新增** `tmux` |
| `test_realhw_requires_reflect_dependencies` | `p1-rst-daemon`、`p1-ho-persist` 含 `supervisor_runtime`；`p1-hp-cycle`、`p1-hp-reorder` 含 `usbipd`；`p1-hp-reorder` 另含 `supervisor_runtime`；在 `platform_missing_caps(os_name="nt")` 下這五案的 runtime skip 皆為 `cap_missing:*`（不會在 Windows 實際執行並因 `sudo`／`systemctl` 失敗） |
| `test_frontend_crash_semantics` | tmux：`crash()` 對 minicom pid 送 SIGKILL（stub `os.kill` 記錄）；telnet：`crash()` 前設 `SO_LINGER` 為 `(1,0)`，fake server 收到 RST（`ConnectionResetError`）而非 FIN |
| `test_preflight_process_scans_portable` | monkeypatch `procobs.list_processes` 回固定清單：`_leaked_daemons`／`_other_pytest`／`_external_testpilot` 依清單判定且不呼叫 `pgrep`；`list_processes` 回 None → 三者回報「證據不足」（problems 含 `process_scan_unavailable`），不視為乾淨 |
| `test_list_processes_windows_branch` | monkeypatch `os.name="nt"` 並注入假 ToolHelp32 → 回 `[{"pid":4,"name":"System","cmdline":None}, ...]` |
| `test_state_dir_windows` | `resolve_state_dir({}, os_name="nt")` 以 `%LOCALAPPDATA%` 為基底；`_state_polluted` 讀該路徑 |
| `test_cross_os_holder_check` | stub 另一側回應一個 `COM0` session 為 `READY` 且 device 與本 bench 板相同 → `("held", ...)`、preflight 拒跑；探測失敗 → `("unknown", ...)` 且 `meta.cross_os_guard=="unknown"`；未設定 → `"unconfigured"` |
| `test_regression_no_os_name_hard_skip`（windows_caps） | AST 掃 `regression/serialwrap_regression/plugin.py`：不再出現 `os.name` 與 `windows_case_unsupported` |
| `test_regression_windows_skip_is_capability_based` | `core.runtime_skip(case_meta, platform_missing_caps(os_name="nt", frontend_ok=True), None, allow_destructive=False)`（`core.py:86` 實際簽章）：F5 case → None（執行）；`f8-second-daemon-detected` → `("cap_missing:posix_throwaway_daemon", ...)`；`f3-fail-error-code` → None |
| `test_regression_case_requires_updated` | F5 四個與 `f9-uboot-readonly-and-console-kept`、`f9-spontaneous-reboot-agent-gated` 的 `requires==("human_frontend",)`；F10 兩個、`f8-second-daemon-detected` 含 `posix_throwaway_daemon` |
| `test_throwaway_env_isolates_all_dirs` | `guards.throwaway_env(work, by_id, run)` 回傳的 env 含 9 個隔離目錄鍵（同 `tests/conftest.py`）＋`SERIALWRAP_PROFILE_DIR`，且值皆在 `work`／`run` 之下 |
| `test_uboot_console_uses_frontend` | `UBootConsole(ctx, "COM0", frontend)` 以 stub frontend：`readonly_cmd("printenv")` 呼叫 `send_line("printenv")`；`validate_uboot_cmd` 規則不變（`saveenv` 仍 raise） |
| `test_reliability_pin_and_gate`（reliability_pin） | testbed 含 `serialwrap_exe` → `build_ctx` 的 `sw.exe` 為該路徑；版本不齊時 `run_preflight` 的 `ok` False 且 problems 含 `#154 gate` |
| `test_reliability_testbed_keys` | `testbed_to_cfg` 保留 `serialwrap_exe`、`bench_lock_path`、`human_frontend` |
| `test_no_foreground_busy`（no_foreground_busy） | `realhw/`、`regression/` 全部 `.py` 不含字串 `FOREGROUND_BUSY`（`GAP-G-12`） |
| 既有回歸 | `tests/test_regression_preflight.py`、`test_regression_guards.py`、`test_regression_harness.py`、`test_regression_pluginfiles.py`、`test_regression_brcm_reboot.py`、`test_realhw_preflight.py`、`test_realhw_longrun.py`、`test_realhw_main.py`、`test_realhw_cases.py`、`test_realhw_harness.py`、`test_realhw_drivers.py`、`test_reliability_core.py`、`test_reliability_testbed.py`、`test_reliability_pluginfiles.py`、`test_reliability_reporter.py` 全部維持綠燈。不得修改斷言來遷就；本 Task 預告必須更新的斷言只有：`test_realhw_main.py` 的 `bench_lock_path` stub（零參數 → 接受 `cfg`／`os_name`）、`daemon status` stub 補 `version`、改以 `main(argv)` 呼叫；`test_realhw_longrun.py`／`test_realhw_drivers.py` 中 `cmd submit` argv 多出 `--source`、snapshot pid 來源。每處於 PR 說明並由審查確認語意不變（§10.4） |

- [ ] **T14-2：執行並確認 FAIL**：`python3 -m pytest -q tests/test_realhw_frontends.py tests/test_realhw_procobs.py tests/test_realhw_pin_gate.py tests/test_regression_windows_caps.py tests/test_reliability_pin.py tests/test_no_foreground_busy.py tests/test_realhw_longrun_sources.py` → 函式內 import 的模組不存在、`FOREGROUND_BUSY` 仍存在、硬性 SKIP 仍存在、`--source` 未帶、`BgResultTracker` 不存在（`test_background_results_survive_history_eviction` 以 ImportError／AttributeError 失敗）。
- [ ] **T14-3：commit 測試**：`git commit -m "test(regression): 新增 L2/L3 pin 版本 gate、能力式 SKIP 與 Telnet frontend 失敗測試"`。

### I 步驟

- [ ] **I14-1：實作** 上列檔案；所有 realhw 模組維持 stdlib-only（不得 import yaml／sw_core）。移植 case 時保留原 marker、斷言、`reason_code` 與 evidence 檔名；只替換「人類輸入／讀取」的手段（`p1-con-liveness`／`p1-con-orphan` 的 SIGKILL 改經 `h.crash()`）。`docs/regression-plugin.md` 與 `realhw/README`（若有）載明跨 OS bench 互斥規則（`cross_os_probe_argv`；未設定時依 OD-10 擁有者排程）。
- [ ] **I14-2：執行並確認 PASS**：上列新測試與既有 realhw／regression／reliability 測試全綠；`python3 -m pytest -q tests/` 無新失敗。另在 Windows host 經 interop 以 `py -3.12 -m pytest -q tests/test_realhw_frontends.py tests/test_realhw_procobs.py tests/test_realhw_pin_gate.py` 執行一次並保存輸出（Windows L1）。
- [ ] **I14-3：commit**：`git commit -m "feat(regression): L2/L3 改為 pinned CLI 版本 gate、能力式 SKIP 並新增 Windows Telnet frontend"`＋fragment。

---

## Task 15：`longrun_acceptance.yaml`、外層 `release_gate_result` 判定器、長跑觀測器

**Test author model:** codex-gpt-6-luna-max　**Implementer model:** haiku-xhigh
**Changelog fragment:** `changelog.d/rust-p2-longrun-release-gate.md`（`type: feat`、`scope: realhw`）
**BLOCKED-OWNER evidence:**
- Linux 與 Windows 各一次修正後 Python 48 h `lr-mixed` 基線（OD-10：擁有者 bench、依序、同機同板經 usbipd、不可並行），含 `observer.ndjson`、WAL 目錄複本、`release-gate.json`（`mode=baseline`）；Windows 另含 `excl/probes.ndjson`＋`excl/handoff.ndjson`（winexcl 原生 COM 獨占證據，R1-F7；真 `CreateFileW` 行為只能在擁有者 Windows bench 取得）。
- OD-09 延後凍結項（`resources`、`min_effective_load`、`comparison_uncertainty`）的數值：須在上述基線完成後、任何 Rust 候選長跑前由擁有者裁決，並以本 Phase Task 16 `python3 -m realhw.acceptance.od09_freeze freeze` 寫入 `longrun_acceptance.yaml`（新 revision，`frozen_at` 早於候選 run；不得手改）——屬正式 Stage 0 出口條件（§12.2 Stage 0 列），不依賴後續 Phase。在此之前所有 candidate 判定必為 `BLOCKED`。
- `lr-mixed` 故障保留：長跑實例的外部監管自動重啟須關閉或以等價措施稽核（§15.11.2，Spike 8）——由擁有者在 bench 上設定並記錄。
- OD-15：P1（含 P1-OC）合併、修正後 Python 基線就緒後，另以 Task 17 `scripts/docker/run-longrun.sh --hours 48` 在 docker 兩容器拓樸執行 48 h `lr-mixed`（假 COM），本 Task 的 `gate.py` 照常判定，但報告為 `evidence_layer: "L1.5-docker"`：可提早揭露缺陷，**不得**取代上列 bench 基線（B8）或作為 OD-09 凍結依據（B9 只吃 bench 基線）。

**Files:**
- Create: `realhw/acceptance/__init__.py`、`realhw/acceptance/longrun_acceptance.yaml`、`realhw/acceptance/longrun_acceptance.schema.json`、`realhw/acceptance/gate.py`、`realhw/acceptance/observer.py`、`realhw/acceptance/walscan.py`、`realhw/acceptance/winexcl.py`（Windows 原生 COM 獨占開啟證據，R1-F7）、`realhw/acceptance/README.md`（含 48 h 基線執行程序：啟 observer →（Windows）`winexcl handoff --phase pre` → 啟 realhw longrun →（Windows）`winexcl handoff --phase post` → 結束後複製 WAL → 執行 gate）
- Create: `tests/test_longrun_acceptance_file.py`、`tests/test_longrun_gate.py`、`tests/test_longrun_observer.py`、`tests/test_longrun_walscan.py`、`tests/test_longrun_winexcl.py`（`core_portable`、`memory_only`：以注入的假 opener／假 `SwCli` 驗判定，不開真 COM）

**Interfaces:**

`realhw/acceptance/longrun_acceptance.yaml`（完整內容；數值與狀態即契約）：

```yaml
schema_version: 1
revision: "2026-10-15.1"
authority: "發版長跑門檻唯一來源（spec §15.11.4、§14.2、OD-09）；其他前端不得維護另一份數值"
owner_decisions: [OD-09, OD-10, OD-13]
workload:
  engine_case: lr-mixed
  engine_files: [realhw/cases/longrun.py]
  min_continuous_duration_s: 172800
  allow_segment_stitching: false
  snapshot_interval_s: 300
  observer_interval_s: 10
  max_observation_gap_factor: 2.0
  windows_excl_probe_interval_s: 300   # Windows 長跑期間第二開啟探測週期（winexcl）；屬 workload 契約，A/B 兩側須相同（計入 workload_contract_sha256）
counting:
  status: frozen
  frozen_at: "2026-10-15T00:00:00+00:00"
  pair_key: [source, round]
  expected_reject_kinds: [busy]
  expected_reject_error_codes: [SESSION_INTERACTIVE_BUSY]
  unresolved_policy: count_as_unexpected_error
  worker_error_policy: count_as_unexpected_error
  definitions:
    submitted: "kind == submit"
    completed: "kind == done，或 kind == bg_result 且 status == done（background 以執行期收斂即時寫入的 bg_result 為準；submit_accepted 不算完成；bg_lost 不算完成）"
    bg_lost: "kind == bg_lost（產品 history 淘汰前測試端未取得結果；不是終態，該 pair 計入 unresolved，並使 background_result_coverage 判 INSUFFICIENT_EVIDENCE）"
    expected_rejects: "kind in expected_reject_kinds，或 kind == error 且 detail 屬 expected_reject_error_codes"
    unexpected_errors: "kind == error 且非 expected reject；加上 kind == bg_result 且 status ∈ {error, canceled}；加上 worker_error（每個 (source, round) 至多計 1 次）"
    unresolved: "submit 之後同 (source, round) 沒有 done／error／busy／worker_error／bg_result 終態（bg_lost、bg_slow、bg_poll_error、bg_drain 都不是終態；已計入 worker_error 的 pair 不再計 unresolved，避免分子重複）"
    accepted: "submitted - expected_rejects"
    error_rate: "(unexpected_errors + unresolved) / accepted"
    effective_duration_s: "events stop.t - start.t，扣除 observer 判定該板非 READY 的確定區間（兩端樣本皆非 READY）；逐板計算"
    effective_hour: "effective_duration_s / 3600（逐板）；throughput = completed / effective_hour"
thresholds:
  unexpected_cmd_error_rate:
    status: frozen
    frozen_at: "2026-10-15T00:00:00+00:00"
    decision: OD-09
    max_ratio: 0.01
    relative: not_higher_than_baseline
    granularity: [platform, board, source]
  effective_throughput:
    status: frozen
    frozen_at: "2026-10-15T00:00:00+00:00"
    decision: OD-09
    metric: "completed per effective hour"
    min_ratio_of_baseline: 0.95
    granularity: [platform, board, source]
  stuck_attached:
    status: frozen
    frozen_at: "2026-10-15T00:00:00+00:00"
    decision: OD-09
    total_s: not_higher_than_baseline
    longest_s: not_higher_than_baseline
    granularity: [platform, board]
    bounds_rule: "候選上界 ≤ 基線下界 → PASS；候選下界 > 基線上界 → FAIL；其餘 INSUFFICIENT_EVIDENCE"
  resources:                 # §15.11.5：Linux／Windows 分開凍結、逐平台比較，不合併平均
    status: deferred
    decision: OD-09
    freeze_deadline: "Stage 0 修正後 Python 48h 基線（Linux、Windows 各一）完成後、任何 Rust 候選長跑開始前"
    per_platform:
      linux:   {rss_peak_kb: null, rss_steady_kb: null, rss_slope_kb_per_h: null}
      windows: {rss_peak_kb: null, rss_steady_kb: null, rss_slope_kb_per_h: null}
  min_effective_load:
    status: deferred
    decision: OD-09
    freeze_deadline: "同 resources"
    per_platform:
      linux:   {min_completed_per_board_source: null, max_idle_gap_s: null}
      windows: {min_completed_per_board_source: null, max_idle_gap_s: null}
  comparison_uncertainty:
    status: deferred
    decision: OD-09
    freeze_deadline: "同 resources"
    per_platform:
      linux:   {error_rate_abs_tolerance: null, throughput_ratio_tolerance: null, stuck_s_tolerance: null, max_reruns: null}
      windows: {error_rate_abs_tolerance: null, throughput_ratio_tolerance: null, stuck_s_tolerance: null, max_reruns: null}
safety:
  status: frozen
  frozen_at: "2026-10-15T00:00:00+00:00"
  waivable: false
  unauthorized_tx: 0
  forbidden_double_holder: 0
  cross_context_replay: 0
  dangerous_auto_resend: 0
  allowed_tx_source_regex: "^(agent:rhw[0-9]+|human:[A-Za-z0-9._:-]+|system|system:[a-z_]+)$"
liveness:
  status: frozen
  frozen_at: "2026-10-15T00:00:00+00:00"
  unexpected_daemon_exit: 0
  unexpected_restart: 0
  pid_generation_change: 0
  worker_heartbeat_max_gap_s: 600       # 2 × worker_alive 週期（300 s）
evidence:
  require_pinned_cli: true
  require_engine_report: true
  require_observer: true
  require_wal_dir: true
  require_fault_retention_attestation: true   # §15.11.2／Spike 8：observer 首筆記錄監管自動重啟已關閉或等價措施
  require_windows_exclusive_evidence: true    # §6.2 Windows：雙重持有判定以 dwShareMode=0 獨占開啟證據為準（winexcl），holder_unknown 只作診斷（R1-F7）
  context_time_tolerance_s: 2.0               # WAL wall_ts 與 events wall_ts 對齊容差；落在容差內的判定記為不確定
```

延後項的凍結：OD-09 凍結時寫入 `thresholds.<key>.per_platform.linux` 與 `per_platform.windows` **兩組**數值、改該 key 的 `status: frozen`、加 `frozen_at`，並遞增檔案 `revision`。`status` 只有 key 層級一個，涵蓋兩平台；**不支援單一平台先凍結**（凍結需兩平台基線皆完成，由本 Phase Task 16 `realhw.acceptance.od09_freeze.freeze` 一次寫入；這是延後項唯一的寫入者，且只依賴 Task 12／15，於正式 Stage 0 出口即可執行；P4／P8／P9 只消費其 `check_frozen_before`、`register_candidate_start` 與 `freeze --out` 報告，R4-F2）。`frozen_at` 必須晚於兩平台基線 run 的結束時間、早於候選 run 的 `started_at_utc`（§14.2「Python 基線完成後、Rust 候選前」）。

**延後項讀寫契約（gate 讀取與 P9 寫入共用；欄位名與型別即介面，任一側不得改名或另設子樹）**：路徑一律 `thresholds.<key>.per_platform.<os>.<field>`，`<os> ∈ {linux, windows}`；gate 只讀 `per_platform[GateInputs.platform]`。

| key | field | 凍結後型別 | gate 用途 |
|---|---|---|---|
| `resources` | `rss_peak_kb` | int ≥ 1 | 全程 `rss_kb` 最大值上限 |
| `resources` | `rss_steady_kb` | int ≥ 1 | 最後 24 h 樣本中位數上限 |
| `resources` | `rss_slope_kb_per_h` | number ≥ 0 | 最後 24 h 最小平方法斜率上限 |
| `min_effective_load` | `min_completed_per_board_source` | object：`{"<com>\|<source>": int ≥ 1}`（全程 completed 下限，completed 依 `counting.definitions.completed`） | 每個列出的鍵；候選缺該鍵視為 0 |
| `min_effective_load` | `max_idle_gap_s` | int ≥ 1 | 每個列出的 `<com>\|<source>` 相鄰 completed 事件最大間隔上限 |
| `comparison_uncertainty` | `error_rate_abs_tolerance` | number ≥ 0 | `error_rate_relative` 容許帶 |
| `comparison_uncertainty` | `throughput_ratio_tolerance` | number ≥ 0 | `throughput_relative` 容許帶 |
| `comparison_uncertainty` | `stuck_s_tolerance` | number ≥ 0 | `stuck_relative` 上下界放寬量 |
| `comparison_uncertainty` | `max_reruns` | int ≥ 0 | relative check 重跑上限 |

schema 以 `if status == "frozen" then` 要求兩平台上表全部欄位非 null 且符合型別；`status == "deferred"` 時全部為 null。`per_platform.<os>` 不得含上表以外的欄位（`additionalProperties: false`），`thresholds.<key>` 下不得出現 `per_platform` 以外的數值欄位（例如直接在 `thresholds.resources` 下放 `rss_peak_kb`、或 `tolerance`／`rerun_rule` 子樹）。凍結紀錄欄位 `decision_id`、`approved_by`、`overrides`、`proposal_sha256`、`baseline_evidence` 由本 Phase Task 16 加入 schema。

```python
# realhw/acceptance/gate.py（可 import yaml；引擎模組不得 import 本檔，也不得 import yaml）
GATE_SCHEMA = "release-gate.v1"
RESULTS = ("PASS", "FAIL", "INSUFFICIENT_EVIDENCE", "BLOCKED", "NOT_APPLICABLE_BASELINE")
EXIT_CODES = {"PASS": 0, "NOT_APPLICABLE_BASELINE": 0, "FAIL": 1, "INSUFFICIENT_EVIDENCE": 4, "BLOCKED": 5}
@dataclass(frozen=True)
class GateInputs:
    report_dir: Path
    case_dir: Path
    acceptance_path: Path
    platform: str                 # "linux" | "windows"
    mode: str                     # "baseline" | "candidate"
    observer_path: Path | None
    wal_dir: Path | None
    baseline_gate_path: Path | None
    excl_evidence_path: Path | None = None   # winexcl 證據目錄 `excl/`（probes.ndjson＋handoff.ndjson）；platform=="windows" 時必要（缺 → safety_double_holder INSUFFICIENT_EVIDENCE），linux 忽略
def load_acceptance(path: Path) -> tuple[dict[str, Any], dict[str, Any]]
    # (內容, {"path","revision","sha256","git_blob_sha1"})；schema 不符 raise ValueError
def count_commands(events: list[dict[str, Any]], counting: dict[str, Any]) -> dict[str, dict[str, int]]
    # key = f"{com}|{source}"；值含 submitted, accepted, completed, failed, unresolved, expected_rejects, unexpected_errors
def stuck_bounds(samples: list[dict[str, Any]], *, interval_s: float) -> dict[str, dict[str, Any]]
    # samples：[{"t": 相對秒, "sessions": {com: state}}]；每板回
    # {"segments":[{"lower_s","upper_s"}], "total_lower_s","total_upper_s","longest_lower_s","longest_upper_s"}
    # 區段在樣本 i..j-1 非 READY、樣本 j READY：lower = t[j-1]-t[i]；upper = t[j]-t[i-1]（i==0 時 upper = t[j]-t[i]）
    # 開放區段（到最後一個樣本 k 仍非 READY，run 結束時未恢復）：lower = t[k]-t[i]；upper = run_end_t - t[i-1]（i==0 時 run_end_t - t[i]），
    #   並標 "open": true；開放區段計入 total／longest 的上下界
def evaluate(inputs: GateInputs) -> dict[str, Any]
def main(argv: list[str] | None = None) -> int
    # python3 -m realhw.acceptance.gate --report-dir D --case-dir C --acceptance A --platform linux|windows
    #   --mode baseline|candidate [--observer O] [--wal-dir W] [--baseline-gate B] [--excl-evidence E] --out release-gate.json

# realhw/acceptance/observer.py（stdlib-only；只呼叫唯讀 `daemon status` 與 `session list`，絕不呼叫 interactive-status）
def sample(sw: Any, boards: list[str]) -> dict[str, Any]
    # {"wall_ts","t_mono","daemon":{"ok","pid","version","multi_open","foreign_holders","holder_unknown","wal_healthy"},"sessions":{com: state},
    #  "procscan":{"other_serialwrapd":[pid...]|null}}（procobs.list_processes；無法列舉為 null）
def header_record(*, supervision: dict[str, Any], platform: str,
                  device_binding: dict[str, str | None] | None = None) -> dict[str, Any]
    # device_binding：observer 啟動時由 `session list` 取每個受測 COM 的 `attached_real_path`（Windows 為 `COMn`），
    #   寫入 header `"device_binding": {com: path}`；winexcl 探測的裝置必須等於此值，否則該筆證據不可歸屬（R1-F7）
    # observer.ndjson 第一行：{"kind":"header","platform","supervision":{"mode","auto_restart_disabled":bool|null,"evidence":str}}
    # supervision 由 `--supervision-attest FILE`（擁有者填寫的 JSON：mode、auto_restart_disabled、證據說明，如 `systemctl show -p Restart` 輸出）提供；
    # 未提供 → auto_restart_disabled:null
def run_observer(sw: Any, boards: list[str], *, interval_s: float, out: Path, stop: threading.Event,
                 duration_s: float | None) -> int
def main(argv: list[str] | None = None) -> int
    # python3 -m realhw.acceptance.observer --serialwrap-exe X --boards COM0,COM1 --interval-s 10 --out observer.ndjson [--duration 48h] [--supervision-attest F]

# realhw/acceptance/walscan.py（stdlib-only；獨立 WAL 讀取，不 import 產品與 uart_stdio_bridge）
MARKER_PATTERNS = {"agent_line": rb"LR_rhw\d+_\d+_\d{4}", "agent_bg": rb"BG_rhw\d+_\d+", "human": rb"HUMAN_TICK_\d+"}
def scan_wal(wal_dir: Path, *, since_wall_ts: str, allowed_tx_source_regex: str,
             context: "SafetyContext | None" = None, tolerance_s: float = 2.0) -> dict[str, Any]
    # 讀 raw.wal.ndjson 與全部歸檔（依時間排序），只看 wall_ts ≥ since_wall_ts 的紀錄；驗 crc32、len
    # 回 {"files","records","problems","unauthorized_tx":[{"seq","source","reason"}],"loss_flags":[seq],"rotation_failed":[seq],
    #     "dangerous_auto_resend":[{"marker","count"}],"cross_context_replay":[{"marker","count","reason"}],
    #     "uncertain":[{"seq","marker"|null,"reason"}],"context_missing":bool}
    # 計數方式：agent 以每個 source 的 TX payload 串流、human 以全部 human:* 的 TX payload 串流（raw mode 逐鍵分筆）
    # 計算各 marker 出現次數與首次出現的 wall_ts；agent marker 次數 >1 → dangerous_auto_resend；HUMAN_TICK 次數 >1 → cross_context_replay
    # 有 context 時另做上下文判定（只靠重複次數無法證明「失效輸入只送一次」或「合法來源在禁止期間送出」）：
    #   - unauthorized_tx 另含：source 合法但 TX wall_ts 落在該板「確定禁止窗口」（observer 兩端樣本皆為 RELEASED／FLASHING／DETACHED）內的 agent TX（reason="forbidden_window"）
    #   - cross_context_replay 另含：HUMAN_TICK_n 的 TX 首次出現時間晚於「該 tick 發出後的下一個 minicom_dead／minicom_open」事件（輸入屬於已失效的 frontend 世代，即使只出現一次，reason="stale_generation"）
    #   - 與窗口邊界或事件時間差 ≤ tolerance_s 者不判 FAIL，列入 uncertain
@dataclass(frozen=True)
class SafetyContext:
    forbidden_windows: dict[str, list[tuple[str, str]]]     # com → [(start_wall_ts, end_wall_ts)]（由 observer 計算）
    human_ticks: list[dict[str, Any]]                     # [{"n","wall_ts","generation"}]（events tick／minicom_open 計算）
    generation_ends: list[str]                            # 每個 frontend 世代結束的 wall_ts（minicom_dead）
def build_safety_context(events: list[dict[str, Any]], observer: list[dict[str, Any]]) -> SafetyContext | None
    # events 或 observer 缺 wall_ts → None（呼叫端判 INSUFFICIENT_EVIDENCE）

# realhw/acceptance/winexcl.py（stdlib-only；Windows 以 ctypes kernel32；不 import 產品碼與 uart_stdio_bridge）（R1-F7）
# 目的：Windows 無法反查 holder（SC-17／GAP-G-03：Python 與 Rust 的 holders 一律 unknown），holder_unknown 恆為 true，
#   不能作雙重持有的證據來源。§6.2 Windows 的保證是 CreateFileW(OPEN_EXISTING, dwShareMode=0) 的原生獨占開啟本身，
#   因此改以「獨占開啟結果」取證：daemon 持有期間第二次開啟必被拒；release 後外部可取得；外部持有時 daemon attach 被拒；
#   外部關閉後 daemon 重新取得。每筆證據綁定受測裝置（`attached_real_path`、`bridge_generation`）與 daemon 程序身分（pid＋建立時間）。
#   holder_unknown／pids 照記於紀錄供診斷，不作判定依據。
ERROR_ACCESS_DENIED = 5
PROBE_SHARE_MODES = (0, FILE_SHARE_READ | FILE_SHARE_WRITE)   # 兩種 share mode 都須被拒：既有 handle 不允許共用（dwShareMode=0 的外部可觀測結果）
@dataclass(frozen=True)
class OpenAttempt:
    share_mode: int
    opened: bool
    winerror: int | None              # opened=False → GetLastError()；opened=True → None
@dataclass(frozen=True)
class Binding:
    com: str
    state: str | None                 # session list 的 state
    device: str | None                # session list 的 attached_real_path（Windows 為 COMn）
    bridge_generation: int | None
    daemon_pid: int | None            # daemon status 的 pid
    daemon_created_at: int | None     # GetProcessTimes CreationTime（FILETIME 100 ns）；取不到 → None
Opener = Callable[[str, int], OpenAttempt]
def win_open(device: str, share_mode: int) -> OpenAttempt
    # CreateFileW("\\\\.\\" + device, GENERIC_READ|GENERIC_WRITE, share_mode, None, OPEN_EXISTING, 0, None)；
    # 成功 → 立即 CloseHandle。不呼叫 ReadFile／WriteFile／SetCommState／SetCommMask／PurgeComm／EscapeCommFunction／TransmitCommChar（零 I/O、不動 DTR／RTS）
def win_hold(device: str) -> ContextManager[OpenAttempt]
    # 同 win_open(share_mode=0) 但持有 handle 至 context 結束才 CloseHandle（handoff 的外部持有者）；同樣零 I/O
def process_created_at(pid: int) -> int | None
def read_binding(sw: Any, com: str) -> Binding          # 只呼叫唯讀 `daemon status` 與 `session list`
def second_open_probe(sw: Any, com: str, *, expected_device: str, opener: Opener = win_open,
                      identity: Callable[[int], int | None] = process_created_at,
                      lock: "ProbeLock") -> dict[str, Any]
    # 非阻塞取 lock（`<excl_dir>/.probe.lock`，LockFileEx）；取不到 → 不開檔，verdict="skipped"、reason="handoff_in_progress"
    # before = read_binding；before.state != "READY"、device != expected_device、或 daemon_pid／daemon_created_at 為 None → 不開檔，
    #   verdict="inconclusive"、reason ∈ {"not_ready","device_unbound","process_identity_unknown"}
    # 依 PROBE_SHARE_MODES 順序各呼叫 opener 一次 → after = read_binding
    # verdict：before == after（state READY、device、bridge_generation、daemon_pid、daemon_created_at 全同）時——
    #   任一 attempt.opened → "violated"（daemon 同一 handle 世代持有期間外部仍取得 handle）；
    #   全部 opened==False 且 winerror==ERROR_ACCESS_DENIED → "exclusive"；其他 winerror → "inconclusive"、reason="unexpected_winerror"
    #   before != after → "inconclusive"、reason="binding_changed"（daemon 自行換 handle 世代，無法歸屬；opened 也不判 violated）
    # 回 {"kind":"excl_probe","wall_ts","com","expected_device","before":Binding,"after":Binding,"attempts":[OpenAttempt],"verdict","reason"}
def handoff_probe(sw: Any, com: str, *, phase: str, expected_device: str, opener: Opener = win_open,
                  holder: Callable[[str], ContextManager[OpenAttempt]] = win_hold,
                  identity: Callable[[int], int | None] = process_created_at,
                  lock: "ProbeLock", ready_timeout_s: float = 120.0) -> dict[str, Any]
    # 全程持有 lock（阻塞取得）；steps 依序且名稱固定：
    #   1 "probe_before"：second_open_probe 必須 "exclusive"
    #   2 "release"：`device release --selector com` → ok:true，session list 該 COM 為 RELEASED
    #   3 "hold"：holder(device) 必須 opened（§6.2 交接：release 成功即 daemon handle 已關閉）
    #   4 "attach_held"：持有期間 `device attach --selector com` 必須 ok:false、error_code=="DEVICE_STILL_HELD"，session 仍 RELEASED；
    #      回應的 pids／holder_unknown 原樣記入 step（診斷，Windows 預期 pids:[]、holder_unknown:true；不參與 verdict）
    #   5 "unhold"：關閉外部 handle
    #   6 "attach"：`device attach --selector com` → ok:true，ready_timeout_s 內 session list 回 READY
    #   7 "probe_after"：second_open_probe 必須 "exclusive"，且 after.bridge_generation > 步驟 1 的值（重新取得新 handle）、
    #      daemon_pid／daemon_created_at 與步驟 1 相同（同一程序，非重啟後取得）
    # verdict：步驟 1／7 任一 "violated"、步驟 3 opened==False 且 winerror==ERROR_ACCESS_DENIED（release 回成功但 handle 仍被持有）、
    #   或步驟 4 回 ok:true（外部持有時 daemon 仍宣稱取得）→ "violated"；全部符合 → "exclusive"；
    #   其餘（RPC 失敗、逾時未 READY、程序身分改變、generation 未前進、probe inconclusive）→ "inconclusive"（reason 為首個不符的 step）
    # 回 {"kind":"excl_handoff","phase":"pre"|"post","wall_ts_start","wall_ts_end","com","expected_device",
    #     "daemon":{"pid","created_at"},"steps":[{"name","ok","detail"}],"verdict","reason"}
def main(argv: list[str] | None = None) -> int
    # python3 -m realhw.acceptance.winexcl handoff --phase pre|post --serialwrap-exe X --boards COM0,COM1
    #   --observer-header observer.ndjson --excl-dir D
    #   expected_device 取 observer header 的 device_binding[com]；結果附加到 D/handoff.ndjson；非 Windows → exit 2（"windows only"）
    #   exit：全部 board verdict=="exclusive" → 0；任一 "violated" → 1；其餘 → 4
# observer.py（Windows 增補）：run_observer(..., excl_dir: Path | None = None, excl_probe_interval_s: float | None = None)；
#   platform=="windows" 且兩者皆給 → 每 excl_probe_interval_s 對每個 board 呼叫 second_open_probe（expected_device 取 header device_binding），
#   附加到 excl_dir/probes.ndjson；CLI `--excl-dir D --excl-probe-interval-s 300`。探測仍只呼叫唯讀 `daemon status`／`session list`，
#   不得呼叫 `device release`／`attach`（只有 handoff CLI 可呼叫）。linux 不探測、不寫 probes.ndjson。
```

**Windows 獨占證據契約（`safety_double_holder` 的 Windows 分支讀取；R1-F7）**：`GateInputs.excl_evidence_path` 指向 `excl/` 目錄，內含 `probes.ndjson`（observer 週期探測）與 `handoff.ndjson`（winexcl CLI）。gate 對 `observer` header `device_binding` 的每個 board 要求：(1) `phase=="pre"` 與 `phase=="post"` 的 `excl_handoff` 各至少一筆 `verdict=="exclusive"`，pre 的 `wall_ts_end` ≤ events `start` 的 `wall_ts`、post 的 `wall_ts_start` ≥ events `stop` 的 `wall_ts`；(2) pre、post 與所有計入的 probe 的 `daemon.pid`／`daemon_created_at` 相同，且等於 observer 全部非 null `daemon.pid`；(3) 每筆紀錄的 `expected_device` 與 `before.device` 等於 header `device_binding[com]`；(4) events `start`～`stop` 期間 `verdict=="exclusive"` 的 probe 時間序列（含 start→首筆、末筆→stop）相鄰間隔 ≤ `max_observation_gap_factor × windows_excl_probe_interval_s`，但 observer 確定非 READY 的區間（兩端樣本皆非 READY）不計入間隔。任一紀錄 `verdict=="violated"` 計入雙重持有事件數（>0 → FAIL，優先於證據不足）；(1)～(4) 任一不滿足或目錄／檔案缺 → INSUFFICIENT_EVIDENCE（`detail.missing`／`detail.gaps`／`detail.reason ∈ {"excl_evidence_missing","handoff_missing","device_unbound","process_identity_mismatch","probe_gap"}`）。`inconclusive`／`skipped` 不計 PASS 也不計 FAIL，只影響間隔。

**判定規則（`evaluate`）**

| check id | group | 依據 | 結果規則 |
|---|---|---|---|
| `engine_report_present` | evidence | `report.json` 有 `id=="lr-mixed"` 的結果 | 缺 → INSUFFICIENT_EVIDENCE |
| `pinned_cli` | evidence | `report.json.meta.cli_identity.pinned`（Task 14） | false 或缺 → INSUFFICIENT_EVIDENCE |
| `continuous_duration` | integrity | events `start`／`stop` 各恰一筆、無 `sigint`、`stop.t - start.t ≥ min_continuous_duration_s` | 不足 → FAIL；`start` 多於一筆（拼接）→ FAIL |
| `observation_gaps` | evidence | snapshots 與 observer 相鄰間隔 > `factor × interval` 的區段 | 有 → INSUFFICIENT_EVIDENCE（列出區段） |
| `pre_frozen` | evidence | 每個 `status: frozen` 項目的 `frozen_at` < `meta.started_at_utc` | 違反者其相依 check 改 BLOCKED（`detail.reason="NOT_PRE_FROZEN"`） |
| `frozen_after_baseline` | evidence | candidate：每個原為 deferred、現為 frozen 的項目（該平台）`frozen_at` > baseline gate 的 `run.ended_at_utc` | 違反（基線前或與基線同時凍結）→ 相依 check BLOCKED（`detail.reason="FROZEN_BEFORE_BASELINE"`）；baseline 模式 → NOT_APPLICABLE_BASELINE |
| `fault_retention_attested` | evidence | observer header `supervision.auto_restart_disabled is True`（§15.11.2、Spike 8） | null 或缺 header → INSUFFICIENT_EVIDENCE；false → INSUFFICIENT_EVIDENCE（故障可能被自動重啟掩蓋） |
| `worker_liveness` | evidence | events 中每個預期 worker（`agent_workers` 個 agent、`human`、`orchestrator`、`bg_resolver`）各有 `worker_start`，相鄰 `worker_alive` 間隔 ≤ `worker_heartbeat_max_gap_s`，且 `worker_stop` 只在 run 結束時出現 | 任一 worker 缺心跳或提前停止 → INSUFFICIENT_EVIDENCE（`detail` 列 worker 與缺口區段） |
| `background_result_coverage` | evidence | events 的 `bg_lost` 筆數：測試端在產品 history（512 筆）淘汰前沒有取得結果（Task 14 第 7 點）；`detail` 另記 `bg_backpressure` 總等待秒數、`bg_slow` 筆數、`bg_drain.remaining` | `bg_lost > 0` → INSUFFICIENT_EVIDENCE（屬工具故障，§15.11.4「工具故障不得判產品 PASS」）。這些 pair 仍以 unresolved 留在 `error_rate` 分子與 `accepted` 分母；本 check 不得把它們移出分母，也不得改判為完成。`bg_drain.remaining`（收尾時產品仍未終態）只計 unresolved，不影響本 check |
| `safety_unauthorized_tx` | safety | `walscan.unauthorized_tx`（含合法 source 但落在確定禁止窗口者） | >0 → FAIL；缺 WAL、`problems`／`loss_flags` 非空、`context_missing`、或 `uncertain` 非空 → INSUFFICIENT_EVIDENCE |
| `safety_double_holder` | safety | observer `multi_open==true`、`foreign_holders` 含自身 pid 以外的持有者、或 `procscan.other_serialwrapd` 非空 | >0 → FAIL；缺 observer → INSUFFICIENT_EVIDENCE。`platform=="windows"`（R1-F7）：Windows holders 一律 unknown（SC-17／`GAP-G-03`，Python 與 Rust 皆然），`holder_unknown`／`pids` **只作診斷**（`detail.holder_unknown_samples` 記筆數），不作判定依據，也不得因其為 true 判證據不足；改依上方「Windows 獨占證據契約」：任一 `excl_probe`／`excl_handoff` 紀錄 `verdict=="violated"` 計入事件數（>0 → FAIL）；否則 `excl_evidence_path` 缺、契約 (1)～(4) 任一不滿足、或任一 observer 樣本 `procscan` 為 null（多 daemon 偵測靠測試端 ToolHelp32，無法列舉即無證據）→ INSUFFICIENT_EVIDENCE（§6.2「查不到不代表沒有」：缺獨占證據仍不得空洞 PASS）；全部滿足 → PASS |
| `safety_cross_context_replay` | safety | `walscan.cross_context_replay`（含 `stale_generation`：失效世代的輸入即使只出現一次） | >0 → FAIL；`context_missing` 或相關 `uncertain` 非空 → INSUFFICIENT_EVIDENCE |
| `safety_dangerous_auto_resend` | safety | `walscan.dangerous_auto_resend` | >0 → FAIL |
| `liveness_daemon_exit` | liveness | snapshots 的 `daemon_death` major 事件（Task 14：連續 3 次 probe 失敗且 pid 已不存在），或 observer `daemon.ok==false` 連續 ≥3 筆且同期 `procobs.pid_alive` 為 False；`pid: null` 視為無觀測 | 有 → FAIL；`pid: null` 連續超過 `max_observation_gap_factor × snapshot_interval_s` → `observation_gaps` 判 INSUFFICIENT |
| `liveness_restart` | liveness | snapshots＋observer 中非 null pid 的變更次數 | >0 → FAIL |
| `error_rate_absolute` | load | `count_commands` 依 granularity（平台整體與每板每來源）計算 `error_rate` | 任一 > 0.01 → FAIL |
| `error_rate_relative` | relative | candidate：同 granularity 的 rate ≤ baseline rate（`comparison_uncertainty` 凍結後：candidate ≤ baseline → PASS；baseline < candidate ≤ baseline + `error_rate_abs_tolerance` → INSUFFICIENT_EVIDENCE（可依 `max_reruns` 重跑）；> baseline + tolerance → FAIL） | baseline 模式 → NOT_APPLICABLE_BASELINE |
| `throughput_relative` | relative | candidate：每板每來源 `completed / effective_hour` ≥ 0.95 × baseline（凍結後：落在 `[0.95 − throughput_ratio_tolerance, 0.95)` → INSUFFICIENT_EVIDENCE） | baseline 模式 → NOT_APPLICABLE_BASELINE |
| `stuck_relative` | relative | `stuck_bounds`（observer 有則用 observer 10 s 樣本，否則 snapshots）照 `bounds_rule`（凍結後上下界各放寬 `stuck_s_tolerance`） | baseline 模式 → NOT_APPLICABLE_BASELINE |
| `baseline_compatible` | evidence | candidate：baseline gate 的 `platform` 相同、`run.engine_files_sha256` 相同、`acceptance.workload_contract_sha256`（`workload`＋`counting` 兩段正規化 JSON 的 sha256）相同、baseline 的 `baseline_result=="PASS"`。**不要求** `acceptance.revision` 相同（OD-09 在基線後以新 revision 凍結延後項是正常流程） | 不同、缺、或 baseline 自身非 PASS → INSUFFICIENT_EVIDENCE |
| `resources` | resources | `status: deferred` → candidate BLOCKED（`detail` 記 RSS 首尾、峰值、穩態、斜率）。`status: frozen`（key 層級，兩平台一次凍結；值讀 `per_platform[platform]`）→ 由 snapshots `rss_kb`（null 略過）計算：`peak`＝最大值；`steady`＝最後 24 h 樣本中位數；`slope`＝最後 24 h 最小平方法斜率（kB/h）；三者皆 ≤ 凍結值 → PASS，任一超標 → FAIL；有效樣本 < 最後 24 h 預期樣本數的 90% → INSUFFICIENT_EVIDENCE | baseline 模式 → NOT_APPLICABLE_BASELINE（`detail` 照記校準資料，供 OD-09 凍結） |
| `min_effective_load` | load | `status: deferred` → candidate BLOCKED（`detail` 記每板每來源每小時 completed 分布與最大閒置間隔）。`status: frozen` → 讀 `per_platform[platform]`：對 `min_completed_per_board_source` 的每個鍵 `"<com>|<source>"`，候選該板該來源全程 completed ≥ 值（候選無此鍵 → 視為 0），且該鍵相鄰 completed 事件最大間隔 ≤ `max_idle_gap_s` → PASS；否則 INSUFFICIENT_EVIDENCE（負載不足即證據不足，不得判 PASS；`detail` 列出不足的鍵） | baseline 模式 → NOT_APPLICABLE_BASELINE（`detail` 照記） |
| `comparison_uncertainty` | relative | `status: deferred` → candidate BLOCKED；`status: frozen` → PASS（其數值由上方三個 relative check 套用）；重跑次數超過 `max_reruns` 仍 INSUFFICIENT → 該 relative check 維持 INSUFFICIENT_EVIDENCE，不得取最佳一次 | baseline 模式 → NOT_APPLICABLE_BASELINE |

彙總：任一 FAIL → `release_gate_result="FAIL"`；否則任一 INSUFFICIENT → `INSUFFICIENT_EVIDENCE`；否則任一 BLOCKED → `BLOCKED`；否則 baseline 模式 → `NOT_APPLICABLE_BASELINE`、candidate 模式 → `PASS`。BLOCKED 只會出現在 candidate 模式（baseline 模式下延後項與 relative 群組一律 NOT_APPLICABLE_BASELINE）。`baseline_result`（只在 baseline 模式）只彙總 `evidence`、`integrity`、`safety`、`liveness`、`error_rate_absolute`：FAIL > INSUFFICIENT_EVIDENCE > PASS（延後凍結項不參與，§11.6「基線依執行前已凍結的絕對條件驗收」）。`engine_result` 為 `report.json` 中 `lr-mixed` 結果 dict 的**原樣複本**（`verdict`、`reason`、`category`、`reason_code`、`duration_s`、`evidence`），判定器不得改寫；`engine_result.verdict=="FAIL"` 不會被任何規則轉成 PASS，且 `release_gate_result` 必為 FAIL（引擎 FAIL 判準保留，§15.11.4）。

`release-gate.v1` 輸出：`{"schema_version","created_at","platform","mode","engine_result":{...},"release_gate_result","baseline_result","acceptance":{"path","revision","sha256","git_blob_sha1","workload_contract_sha256","effective_content"},"run":{"started_at_utc","ended_at_utc","duration_s","cli_identity","deployed_content","daemon_version","engine_files_sha256":{"realhw/cases/longrun.py":"<sha256>",...},"contract_revision","cases_yaml_sha256"},"checks":[{"id","group","status","result","detail"}],"counts":{...},"stuck":{...},"walscan":{...摘要},"evidence_hashes":{"report.json","events.ndjson","snapshots.ndjson","observer.ndjson","wal":{檔名: sha256}}}`；`indent=2`、`sort_keys`、`ensure_ascii=False`。`run.deployed_content` 原樣複製 `report.json.meta.deployed_content`（Task 14 產生端量測的 start／end；報告缺此鍵時寫 `null`），gate 不重算也不據以改變 `release_gate_result`——是否足以證明受測內容由 P9 Task 14B ingest 對照 target 判定（R5-F4，長跑 entry 的 `runtime_tree_sha256` 只能來自這裡，供 Stage 3 `reuse_of` 比對）。

### T 步驟

- [ ] **T15-1：撰寫失敗測試**。關鍵：

```python
def test_engine_result_preserved_verbatim_and_fail_dominates(tmp_path):
    run = LongrunFixture(tmp_path).healthy(hours=48, platform="linux")
    run.engine_result = {"verdict": "FAIL", "reason": "命令錯誤率過高：6/10", "category": "test",
                         "reason_code": "cmd_error_rate_high", "duration_s": 172800.0, "evidence": {"analysis": "x"}}
    out = gate.evaluate(run.inputs(mode="candidate", baseline=run.baseline_gate()))
    assert out["engine_result"] == run.engine_result
    assert out["release_gate_result"] == "FAIL"

def test_deferred_thresholds_block_candidate_pass(tmp_path):
    run = LongrunFixture(tmp_path).healthy(hours=48, platform="linux")
    out = gate.evaluate(run.inputs(mode="candidate", baseline=run.baseline_gate()))
    assert out["release_gate_result"] == "BLOCKED"
    blocked = {c["id"] for c in out["checks"] if c["result"] == "BLOCKED"}
    assert {"resources", "min_effective_load", "comparison_uncertainty"} <= blocked

def test_baseline_mode_never_claims_non_regression(tmp_path):
    run = LongrunFixture(tmp_path).healthy(hours=48, platform="windows")
    out = gate.evaluate(run.inputs(mode="baseline"))
    assert out["release_gate_result"] == "NOT_APPLICABLE_BASELINE"
    assert out["baseline_result"] == "PASS"
    rel = {c["id"]: c["result"] for c in out["checks"] if c["group"] == "relative"}
    assert set(rel.values()) == {"NOT_APPLICABLE_BASELINE"}
    deferred = {c["id"]: c["result"] for c in out["checks"]
                if c["id"] in {"resources", "min_effective_load", "comparison_uncertainty"}}
    assert set(deferred.values()) == {"NOT_APPLICABLE_BASELINE"}       # baseline 模式不得出現 BLOCKED
    assert all(c["result"] != "BLOCKED" for c in out["checks"])
```

（`LongrunFixture` 為測試內建 builder：產生 `report.json`（含 `meta.started_at_utc`、`meta.cli_identity.pinned:true`）、`events.ndjson`、`snapshots.ndjson`（300 s）、`observer.ndjson`（10 s；48 h 以壓縮表示：builder 以參數產生、檔案實際寫出）、WAL 目錄（每個 marker 一筆 TX，crc 正確），以及以全部延後項填入假數值的 acceptance 副本供 PASS 路徑測試。`platform="windows"` 的 `healthy()` 另產生：observer 每筆 `holder_unknown:true`、`procscan.other_serialwrapd:[]`，header `device_binding={"COM0":"COM5","COM1":"COM6"}`；`excl/handoff.ndjson` 含每板 pre（`wall_ts_end` 早於 `start` 60 s）與 post（`wall_ts_start` 晚於 `stop` 60 s）各一筆 `verdict:"exclusive"`；`excl/probes.ndjson` 每 300 s 每板一筆 `verdict:"exclusive"`；全部紀錄 `daemon.pid==4242`、`daemon_created_at==133000000000000000`，與 observer pid 相同。builder 提供 `drop_excl_evidence()`、`drop_handoff(phase, com)`、`probe_gap(com, start_t, end_t)`、`set_probe(i, **fields)`、`set_handoff(phase, com, **fields)` 供變體測試。）

其餘：

| 測試名（檔案） | 斷言 |
|---|---|
| `test_acceptance_file_schema`（acceptance_file） | 正式檔以 `longrun_acceptance.schema.json` 驗證通過；`thresholds` 三個 OD-09 立即凍結項 `status=="frozen"`，`resources`／`min_effective_load`／`comparison_uncertainty` 為 `deferred`，且 `per_platform.linux`、`per_platform.windows` 兩組的數值欄位皆為 null；兩平台欄位集合恰為上方「延後項讀寫契約」表（`resources`：`{rss_peak_kb, rss_steady_kb, rss_slope_kb_per_h}`；`min_effective_load`：`{min_completed_per_board_source, max_idle_gap_s}`；`comparison_uncertainty`：`{error_rate_abs_tolerance, throughput_ratio_tolerance, stuck_s_tolerance, max_reruns}`）；schema 拒絕：缺少任一平台、改回單一純量、`per_platform.<os>` 多出欄位、`thresholds.resources.rss_peak_kb` 這類直接掛在 key 下的數值欄位、`comparison_uncertainty.tolerance` 子樹、`status: frozen` 但任一平台任一欄位為 null、`min_completed_per_board_source` 為純量 |
| `test_allowed_source_regex_matches_engine` | `safety.allowed_tx_source_regex` 匹配 `agent:rhw0`～`agent:rhw7`、`human:<client_id>`、`system`、`system:recover`、`system:self_test`；不匹配 `agent`（未帶 `--source` 的預設值）與 `agent:intruder` |
| `test_frozen_values_exact` | `max_ratio==0.01`、`min_ratio_of_baseline==0.95`、`min_continuous_duration_s==172800`、`allow_segment_stitching is False`、safety 四項皆 0 且 `waivable is False`、`workload.windows_excl_probe_interval_s==300`、`evidence.require_windows_exclusive_evidence is True` |
| `test_single_source_of_thresholds` | repo 中除本檔與其 schema、測試外，沒有其他檔案出現 `0.95` 搭配 `throughput` 或 `172800` 字樣（`realhw/`、`reliability/`、`regression/` 掃描） |
| `test_engine_does_not_import_yaml_or_gate` | `realhw/*.py`、`realhw/cases/*.py`（不含 `realhw/acceptance/gate.py`）AST 無 `import yaml`、無 `realhw.acceptance.gate` |
| `test_all_frozen_filled_candidate_pass`（gate） | 以填好延後項（`frozen_at` 早於 run）的 acceptance 副本、健康候選與健康基線 → `PASS` |
| `test_post_hoc_freeze_is_blocked` | 同上但延後項 `frozen_at` 晚於 `started_at_utc` → 相依 check `BLOCKED`，`detail.reason=="NOT_PRE_FROZEN"`，整體 `BLOCKED` |
| `test_stitched_segments_fail` | events 有兩筆 `start` → `continuous_duration` FAIL |
| `test_short_run_fail` | 47 h → FAIL |
| `test_sigint_fail` | 有 `sigint` 事件 → FAIL |
| `test_unpinned_cli_insufficient` | `cli_identity.pinned:false` → INSUFFICIENT_EVIDENCE |
| `test_gate_copies_deployed_content`（R5-F4） | `LongrunFixture` 的 `report.json` 帶 `meta.deployed_content = {"start": X, "end": Y}` → `release-gate.json` `run.deployed_content == {"start": X, "end": Y}`（逐鍵相等）；刪除該鍵 → `run.deployed_content is None`，且兩情形 `release_gate_result` 相同 |
| `test_observation_gap_insufficient` | observer 中間缺 60 s（>2×10 s）→ INSUFFICIENT_EVIDENCE，`detail.gaps` 列出區段 |
| `test_unresolved_stays_in_denominator` | 100 submit、95 done、5 無後續 → `unresolved==5`、`error_rate==0.05` → `error_rate_absolute` FAIL |
| `test_busy_is_expected_reject` | 100 submit、10 busy、90 done → `accepted==90`、`error_rate==0` |
| `test_worker_error_counts` | 1 筆 `worker_error`（其 `submit` 已發出）→ 計入 `unexpected_errors` 1 次、`unresolved` 0 次（不重複計分子） |
| `test_background_counted_by_runtime_result` | background `submit`＋`submit_accepted` 無 `bg_result` → unresolved；`bg_result status=error` → unexpected_errors；`bg_result status=done` → completed；`bg_result` 以原 submit 的 `(source, round)` 配對 |
| `test_bg_lost_unresolved_and_insufficient` | 100 筆 background submit：97 筆 `bg_result status=done`、3 筆 `submit_accepted`＋`bg_lost` → `completed==97`、`unresolved==3`、`accepted==100`（分母不縮）、`error_rate==0.03`；`background_result_coverage` 為 INSUFFICIENT_EVIDENCE，`detail.bg_lost==3`；`bg_lost` 不讓任何 pair 計為 completed。另一組：0 筆 `bg_lost`、2 筆收尾 `bg_drain{remaining:2}` → `background_result_coverage` PASS，該 2 筆計 unresolved |
| `test_effective_hour_excludes_non_ready` | 48 h run 中某板確定非 READY 2 h：該板 `effective_hour==46`，throughput 以 46 h 為分母 |
| `test_stuck_bounds_open_segment` | 樣本 t=[0,300,600] 狀態 [READY,ATTACHED,ATTACHED]、run_end_t=700 → 一段 `open:true`、`lower=300`、`upper=700` |
| `test_error_rate_per_board_source` | 整體 0.5% 但板 `COM1`、來源 `agent:rhw2` 為 2% → FAIL |
| `test_throughput_relative` | 候選某板來源 completed/h 為基線 94% → FAIL；96% → 該 check PASS |
| `test_stuck_bounds_math` | 樣本 t=[0,300,600,900] 狀態 [READY,ATTACHED,ATTACHED,READY] → `lower=300`、`upper=900` |
| `test_stuck_relative_insufficient_when_overlapping` | 候選 [lower 300, upper 900] vs 基線 [600, 1200] → INSUFFICIENT_EVIDENCE；候選 [10,20] vs 基線 [300,900] → PASS；候選 [1500,1800] vs 基線 [300,900] → FAIL |
| `test_safety_unauthorized_tx_fail` | WAL 有一筆 `source=="agent:intruder"` 的 TX → FAIL |
| `test_safety_missing_wal_insufficient` | `wal_dir=None` → 四個 safety check 皆 INSUFFICIENT_EVIDENCE |
| `test_safety_double_holder_fail` | observer 一筆 `multi_open:true` → FAIL |
| `test_safety_double_holder_windows_exclusive_evidence_pass`（取代舊 `test_safety_double_holder_windows_unknown_insufficient`；R1-F7 配對測試之一，先寫、須先 FAIL：舊規則見 `holder_unknown:true` 即判 INSUFFICIENT） | `platform="windows"` 的 `healthy()`（所有樣本 `multi_open:false`、`holder_unknown:true`、`procscan.other_serialwrapd:[]`，完整 `excl/`）→ `safety_double_holder` 的 `result=="PASS"`、`detail.holder_unknown_samples == len(observer 樣本)`（診斷保留）；同 fixture `mode="baseline"` → `baseline_result=="PASS"`；`mode="candidate"`＋填好延後項的 acceptance → `release_gate_result=="PASS"` |
| `test_safety_double_holder_windows_missing_evidence_insufficient`（R1-F7 配對測試之二） | 同上 fixture，各變體 `safety_double_holder` 皆 `INSUFFICIENT_EVIDENCE`、不得 PASS：`excl_evidence_path=None` → `detail.reason=="excl_evidence_missing"`；`drop_handoff("post","COM1")` → `reason=="handoff_missing"`、`detail.missing==["handoff:post:COM1"]`；pre 的 `wall_ts_end` 晚於 events `start` → `handoff_missing`；`probe_gap("COM0", 3600, 4500)`（900 s > 2×300，期間 observer COM0 皆 READY）→ `reason=="probe_gap"`、`detail.gaps==[{"com":"COM0","from_t":3600,"to_t":4500}]`；同區間 observer COM0 兩端樣本皆 `RELEASED` → 該缺口不計、PASS；`set_probe(5, expected_device="COM9")` → `device_unbound`；`set_handoff("post","COM0", daemon={"pid":4242,"created_at":133000000000000001})` → `process_identity_mismatch`；全部 probe 改 `verdict:"inconclusive"` → `probe_gap`；任一樣本 `procscan:null` → INSUFFICIENT_EVIDENCE |
| `test_safety_double_holder_windows_violation_fail` | 完整 `excl/` 但 `set_probe(7, verdict="violated")` → `safety_double_holder` FAIL、`detail.events==1`；`set_handoff("pre","COM1", verdict="violated")` 且同時 `excl_evidence_path` 其餘缺項 → 仍 FAIL（FAIL 優先於證據不足）；`release_gate_result=="FAIL"` |
| `test_safety_double_holder_linux_ignores_excl_evidence` | `platform="linux"` 健康 fixture、`excl_evidence_path=None`、樣本無 `holder_unknown` 欄 → `safety_double_holder` PASS（Linux 規則不變；`excl/` 只在 Windows 讀） |
| `test_winexcl_second_open_exclusive`（winexcl；`tests/test_longrun_winexcl.py`） | 假 `SwCli`：before／after 皆 `{state:"READY", attached_real_path:"COM5", bridge_generation:3}`、`daemon status.pid==4242`；`identity` 恆回 `133000000000000000`；假 opener 兩次皆回 `OpenAttempt(opened=False, winerror=5)` → `verdict=="exclusive"`、`[a.share_mode for a in attempts]==[0, 3]`（`FILE_SHARE_READ\|FILE_SHARE_WRITE==3`）、`before==after` |
| `test_winexcl_second_open_opened_is_violated` | 同上但 share_mode 0 的 attempt 回 `opened=True` → `verdict=="violated"`；`len(attempts)==2`（仍做完兩種 share mode） |
| `test_winexcl_probe_not_ready_does_not_open` | before `state=="RELEASED"`（或 `ATTACHED`）→ 假 opener 呼叫次數 0、`verdict=="inconclusive"`、`reason=="not_ready"`；`attached_real_path=="COM9"`（≠ expected `COM5`）→ opener 0 次、`reason=="device_unbound"`；`identity` 回 None → opener 0 次、`reason=="process_identity_unknown"` |
| `test_winexcl_binding_changed_inconclusive` | after `bridge_generation==4`（before 3）：opener 回 `opened=True` → `verdict=="inconclusive"`、`reason=="binding_changed"`（不得判 violated）；opener 回拒絕 → 同樣 `inconclusive` |
| `test_winexcl_unexpected_winerror_inconclusive` | opener 回 `winerror=2`（FILE_NOT_FOUND）→ `inconclusive`、`reason=="unexpected_winerror"`（不得判 exclusive） |
| `test_winexcl_probe_skips_when_lock_busy` | 假 lock `try_acquire()` 回 False → opener 0 次、`verdict=="skipped"`、`reason=="handoff_in_progress"` |
| `test_winexcl_handoff_full_cycle_exclusive` | 假 `SwCli` 腳本：release ok→RELEASED；holder 回 opened；持有期間 attach 回 `{"ok":false,"error_code":"DEVICE_STILL_HELD","pids":[],"holder_unknown":true,"selector":"COM0"}`；unhold 後 attach ok、第 2 次 `session list` 為 READY、`bridge_generation` 3→4；pid／created_at 不變 → `verdict=="exclusive"`；`[s["name"] for s in steps]==["probe_before","release","hold","attach_held","unhold","attach","probe_after"]`；`steps[3]["detail"]["holder_unknown"] is True` 且 `pids==[]`（診斷照記）；假 `SwCli` 呼叫序列中 `device attach` 恰 2 次、`device release` 恰 1 次 |
| `test_winexcl_handoff_attach_ok_while_held_violated` | 同上但持有期間 attach 回 `ok:true` → `verdict=="violated"`、`reason=="attach_held"` |
| `test_winexcl_handoff_hold_denied_after_release_violated` | release 回 ok 但 holder 回 `opened=False, winerror=5` → `verdict=="violated"`、`reason=="hold"`；holder 回 `winerror=2` → `inconclusive`、`reason=="hold"` |
| `test_winexcl_handoff_identity_or_generation_inconclusive` | probe_after 的 `daemon_created_at` 與 probe_before 不同 → `inconclusive`、`reason=="probe_after"`；`bridge_generation` 未前進（仍 3）→ `inconclusive`；attach 後 `ready_timeout_s=0.2` 內未見 READY → `inconclusive`、`reason=="attach"` |
| `test_winexcl_no_io_calls` | `realhw/acceptance/winexcl.py` 原始碼（AST Name／Attribute／字串常值）不含 `ReadFile`、`WriteFile`、`SetCommState`、`SetCommMask`、`PurgeComm`、`EscapeCommFunction`、`TransmitCommChar`；`win_open`／`win_hold` 的 `CreateFileW` 引數以 monkeypatch 的假 kernel32 記錄：`dwCreationDisposition==OPEN_EXISTING`、`dwDesiredAccess==GENERIC_READ\|GENERIC_WRITE`、`win_hold` 的 `dwShareMode==0`；成功後 `CloseHandle` 恰 1 次 |
| `test_winexcl_cli_non_windows_exit2` | `os.name != "nt"` 下 `main(["handoff","--phase","pre",...])` → exit 2、stderr 含 `windows only`、不呼叫任何 `SwCli` |
| `test_observer_windows_excl_probes`（observer） | `platform="windows"`、`excl_dir=tmp`、`excl_probe_interval_s=0.05`、duration 0.3 s、假 opener 回拒絕 → `probes.ndjson` 每板 ≥4 筆 `kind=="excl_probe"`；header 含 `device_binding`；stub `SwCli` 呼叫仍只有 `("daemon","status")` 與 `("session","list")`（`test_observer_readonly_calls` 不變） |
| `test_observer_linux_no_excl_probes` | `platform="linux"` 即使給 `excl_dir` → 不建立 `probes.ndjson`、不呼叫 opener |
| `test_cross_context_replay_single_occurrence_fail` | `HUMAN_TICK_5` 於 `tick` 事件後、下一個 `minicom_dead` 之後才首次出現在 WAL TX（只出現一次）→ `cross_context_replay` 1 筆（`stale_generation`）→ FAIL |
| `test_unauthorized_tx_legit_source_in_forbidden_window` | `agent:rhw1` 的 TX 落在 observer 兩端樣本皆 `RELEASED` 的窗口內 → FAIL；落在窗口邊界 ±1 s → `uncertain` → INSUFFICIENT_EVIDENCE |
| `test_safety_missing_context_insufficient` | events 缺 `wall_ts` → `build_safety_context` 回 None → `safety_unauthorized_tx`、`safety_cross_context_replay` 皆 INSUFFICIENT_EVIDENCE |
| `test_fault_retention_required` | observer header `auto_restart_disabled:null` → `fault_retention_attested` INSUFFICIENT_EVIDENCE；`true` → PASS |
| `test_worker_liveness_gap_insufficient` | `agent:rhw2` 的 `worker_alive` 中斷 1200 s → `worker_liveness` INSUFFICIENT_EVIDENCE |
| `test_resources_frozen_rules` | 填好兩平台 `resources.per_platform`（linux `rss_peak_kb=60000`、windows `rss_peak_kb=40000`）後：linux candidate peak 61000 → FAIL；全部未超標 → PASS；最後 24 h 有效樣本 80% → INSUFFICIENT_EVIDENCE；windows candidate peak 50000 → FAIL（證明只讀 `per_platform[platform]`，不混用 linux 值）；`status: frozen` 但 `per_platform.windows.rss_peak_kb` 為 null → `load_acceptance` 以 schema 錯誤拒絕（不得把 null 當成無上限） |
| `test_min_load_map_keys` | `min_completed_per_board_source={"COM0\|agent:rhw0": 100, "COM1\|human:h1": 10}`：候選 `COM0\|agent:rhw0` 完成 99 → INSUFFICIENT_EVIDENCE、`detail` 含該鍵；候選完全沒有 `COM1\|human:h1` 事件 → INSUFFICIENT_EVIDENCE；兩鍵皆達標且最大間隔 ≤ `max_idle_gap_s` → PASS |
| `test_min_load_too_low_insufficient` | 填好 `min_effective_load` 後某板某來源 completed 低於門檻 → INSUFFICIENT_EVIDENCE（不得 PASS） |
| `test_uncertainty_band_insufficient` | `throughput_ratio_tolerance=0.02`：候選 94%（落在 [0.93,0.95)）→ INSUFFICIENT_EVIDENCE；92% → FAIL |
| `test_frozen_before_baseline_blocked` | 延後項 `frozen_at` 早於 baseline `run.ended_at_utc` → `frozen_after_baseline` 違反、相依 check BLOCKED |
| `test_baseline_must_pass_for_candidate` | baseline gate `baseline_result=="INSUFFICIENT_EVIDENCE"` → candidate `baseline_compatible` INSUFFICIENT_EVIDENCE |
| `test_liveness_restart_fail` | observer pid 由 100 變 200 → FAIL |
| `test_baseline_incompatible_insufficient` | baseline gate 的 `run.engine_files_sha256` 或 `acceptance.workload_contract_sha256` 不同 → INSUFFICIENT_EVIDENCE；只有 `acceptance.revision` 不同（OD-09 基線後凍結）而 workload／counting／engine 相同 → 該 check PASS |
| `test_output_schema_and_hashes` | 輸出含 `acceptance.sha256` 等於檔案 sha256、`git_blob_sha1` 正確、`effective_content` 等於解析內容；`evidence_hashes` 每檔 sha256 正確 |
| `test_cli_exit_codes` | PASS→0、NOT_APPLICABLE_BASELINE→0、FAIL→1、INSUFFICIENT→4、BLOCKED→5 |
| `test_observer_readonly_calls`（observer） | stub `SwCli` 記錄呼叫：只出現 `("daemon","status")` 與 `("session","list")`；`observer.py` 原始碼不含 `interactive-status` |
| `test_observer_writes_samples` | interval 0.05 s、duration 0.3 s → ≥4 行，每行含 `wall_ts`、`daemon.pid`、`sessions` |
| `test_observer_survives_rpc_error` | stub 第 2 次 raise → 該筆 `daemon.ok:false` 並續跑 |
| `test_observer_header_supervision` | `--supervision-attest` 指向 `{"mode":"systemd-user","auto_restart_disabled":true,"evidence":"Restart=no"}` → 第一行 `kind=="header"` 且內容相同；未提供 → `auto_restart_disabled is None` |
| `test_walscan_counts_human_across_records`（walscan） | `HUMAN_TICK_7` 被拆成 12 筆單字元 human TX → 計數 1；同 marker 再出現一次 → `cross_context_replay` 1 筆 |
| `test_walscan_agent_resend` | `echo LR_rhw0_3_1234\n` 兩筆 TX（同 source）→ `dangerous_auto_resend` 1 筆 |
| `test_walscan_since_filter` | `wall_ts` 早於 `since_wall_ts` 的紀錄不計 |
| `test_walscan_crc_problem` | 一筆 crc 錯 → `problems` 非空 |
| `test_walscan_archives_included` | 歸檔 `raw.wal.ndjson.20261015-030617` 的紀錄被讀入 |

- [ ] **T15-2：執行並確認 FAIL**：`python3 -m pytest -q tests/test_longrun_acceptance_file.py tests/test_longrun_gate.py tests/test_longrun_observer.py tests/test_longrun_walscan.py tests/test_longrun_winexcl.py` → 模組與檔案不存在。
- [ ] **T15-3：commit 測試**：`git commit -m "test(realhw): 新增長跑門檻檔、外層發版判定器與觀測器的失敗測試"`。

### I 步驟

- [ ] **I15-1：實作** 上列檔案；`README.md` 寫 48 h 基線程序：(1) 確認 bench lock 與無其他 pytest／testpilot；(2) 關閉該實例的監管自動重啟並把設定證據寫成 `supervision-attest.json`（§15.11.2）；(3) 啟 `observer --supervision-attest supervision-attest.json` 背景程序（Windows 另加 `--excl-dir <report>/excl --excl-probe-interval-s <workload.windows_excl_probe_interval_s>`）；(3a) Windows：`py -3.12 -m realhw.acceptance.winexcl handoff --phase pre --boards … --observer-header observer.ndjson --excl-dir <report>/excl`，exit 0 才繼續（exit 1 → 停止並保留現場；exit 4 → 排除後重做 pre，不得跳過）；(4) `python3 -m realhw --tier longrun --duration 48h`（pinned exe）；(4a) Windows：longrun 結束後、停止 observer 前執行 `winexcl handoff --phase post`（同參數），結果照實保存（非 0 由 gate 判定）；(5) 結束後複製 `daemon status.wal_path` 所在目錄到 report 目錄 `wal/`；(6) `python3 -m realhw.acceptance.gate --mode baseline ...`（Windows 加 `--excl-dir` 對應的 `--excl-evidence <report>/excl`）；(7) 保存所有 hash（含 `excl/*.ndjson`）。Windows 版步驟改用 `py -3.12` 與 `bench_lock_path`。
- [ ] **I15-2：執行並確認 PASS**：上列測試全綠；`python3 -m pytest -q tests/` 無新失敗。
- [ ] **I15-3：commit**：`git commit -m "feat(realhw): 新增 longrun_acceptance.yaml 唯一門檻檔與保留 engine_result 的外層發版判定器"`＋fragment。

---

## Task 16：OD-09 延後項凍結工具與候選長跑開跑登記（`realhw/acceptance/od09_freeze.py`；延後項唯一寫入者，R4-F2）

**Test author model:** codex-gpt-6-luna-max　**Implementer model:** haiku-xhigh
**Changelog fragment:** `changelog.d/rust-p2-od09-freeze.md`（`type: feat`、`scope: realhw`）
**BLOCKED-OWNER evidence:** B9（實際 `stats → propose → freeze → 重跑基線 gate → check` 由擁有者在兩平台基線（B8）完成後、於**正式 Stage 0 出口**執行；必須早於任何 Rust 候選長跑，最早者為 Stage 1B 的 `S1B-L3-MIXED-48H-WINDOWS`）。

**為什麼放在 P2（R4-F2）**：規格 §12.2 Stage 0 列要求「正式 Stage 0 出口須完成延後項目的凍結與證據充分性核對」，§14.2／OD-09 要求凍結早於任何 Rust 候選長跑。凍結工具若放在最後建置的 P9（前置 P0～P8），Stage 1B 48 h 長跑就只能延到 Stage 2 結束後，或靠手改 acceptance（違反單一寫入者）。因此凍結工具與它需要的最小證據核對子集屬於 Stage 0：本 Task **只依賴 P2 Task 12**（`stage0_evidence_index.yaml`＋`check_evidence_index.py`）與 **P2 Task 15**（`longrun_acceptance.yaml`、schema、`gate.py`、`LongrunFixture`），不得 import `tools.stage3`、`sw_core`，不得讀 P9 的 `tests/acceptance/stage_exits.json`、`docs/rust-refactor/evidence/index.json`，也不依賴 P3～P9 任何交付物。P4（Stage 1B 長跑）、P8（Slice 2.4 長跑）與 P9（Stage 3 A/B、證據 ingest）只**消費**本 Task 的 `check_frozen_before`／`register_candidate_start` 與 `freeze --out` 報告，不另寫延後項。

讀寫契約（路徑、欄位名、型別）以 Task 15「延後項讀寫契約」表為唯一來源。本 Task 只補**寫入端**與時序守門：凍結程序（stats → propose → freeze）、`check_frozen_before`、候選長跑開跑登記 `register_candidate_start`，以及「凍結輸出能被正式 gate 原樣讀取」的整合測試；**不改 gate 判定、不改名、不另設 `deferred`／`tolerance`／`rerun_rule` 等子樹**。

**Files:**
- Create: `realhw/acceptance/od09_freeze.py`（含 `build_parser()`／`main(argv)`；`python3 -m realhw.acceptance.od09_freeze <stats|propose|freeze|check|register-candidate>`）
- Create: `realhw/acceptance/candidate_longrun_starts.ndjson`（空檔；append-only 的候選長跑開跑登記，只由 `register_candidate_start` 寫入）
- Create: `docs/rust-refactor/owner-decisions-addenda.md`（附加式紀錄檔，初始只有表頭 `| decision_id | 主題 | 日期 | 內容 |`；本 Task 的 OD-09 凍結與 P9 Task 12 的 Canary policy 凍結都附加到這裡，P9 不重建）
- Create: `docs/rust-refactor/procedures/od09-deferred-freeze.md`（Stage 0 出口程序；P9 Task 17 只引用，不重建）
- Create: `tests/fakes_od09.py`（`od09_env`、`longrun_case`、`rerun_baseline_gate`、`ts`）
- Modify: `realhw/acceptance/longrun_acceptance.schema.json`（只新增凍結紀錄欄位：`status: frozen` 時 `decision_id`、`approved_by`、`overrides`、`proposal_sha256`、`baseline_evidence` 為必填；`per_platform` 形狀與欄位型別沿用 Task 15，不放寬；同一 commit 讓 `tests/test_longrun_gate.py` 的 `LongrunFixture`「填好延後項的 acceptance 副本」補上這五欄，Task 15 既有測試維持綠燈）
- Modify: `tests/acceptance/stage0_evidence_index.yaml`（兩平台基線項 `id` 固定為 `S0-L3-BASELINE-LINUX-48H`／`S0-L3-BASELINE-WINDOWS-48H`、`artifact_path` 指向該平台 baseline `release-gate.json`；OD-09 項 `id` 固定為 `S0-OD09-DEFERRED-FROZEN`、`artifact_path` 指向 `od09-freeze.json`；三項初始 `status: blocked_owner`。id 與 P9 `stage_exits.json` 同名，供 P9 之後沿用同一證據）
- Modify: `tests/acceptance/check_evidence_index.py`（新增純函式 `verify_item(index_path: Path, item_id: str, *, root: Path) -> tuple[dict | None, list[str]]`：回傳該項與違規清單（不存在、非 `present`、檔案缺、sha256 不符）；CLI 改為呼叫它，exit code 行為不變）
- 不修改 `realhw/acceptance/gate.py`：若整合測試因 gate 未依 `per_platform[platform]` 讀值而紅，屬 Task 15 缺陷，回 Task 15 修正並補 Task 15 測試；不得在 `od09_freeze` 內加相容層或寫第二份值
- Create: `tests/test_od09_freeze.py`、`tests/test_longrun_gate_frozen.py`（皆 `core_portable`＋`memory_only`；呼叫子程序的 `test_stage0_chain_without_later_phases` 改標 `process_rpc`）

**Interfaces — Produces：**

```python
# realhw/acceptance/od09_freeze.py（可 import yaml 與 realhw.acceptance.gate；不得 import tools.stage3／sw_core）
DEFERRED_KEYS: tuple[str, ...] = ("resources", "min_effective_load", "comparison_uncertainty")
PLATFORMS: tuple[str, ...] = ("linux", "windows")
PER_PLATFORM_FIELDS: Mapping[str, tuple[str, ...]] = {   # 必須等於 Task 15「延後項讀寫契約」表（test_per_platform_fields_match_acceptance）
    "resources": ("rss_peak_kb", "rss_steady_kb", "rss_slope_kb_per_h"),
    "min_effective_load": ("min_completed_per_board_source", "max_idle_gap_s"),
    "comparison_uncertainty": ("error_rate_abs_tolerance", "throughput_ratio_tolerance", "stuck_s_tolerance", "max_reruns"),
}
BASELINE_ITEMS: Mapping[str, str] = {"linux": "S0-L3-BASELINE-LINUX-48H", "windows": "S0-L3-BASELINE-WINDOWS-48H"}
FREEZE_ITEM: str = "S0-OD09-DEFERRED-FROZEN"
CANDIDATE_LONGRUN_ITEMS: Mapping[str, str] = {   # item_id -> platform；P9 stage_exits.json 的 candidate_longrun 集合必須與此相等
    "S1B-L3-MIXED-48H-WINDOWS": "windows",
    "S24-L3-LRMIXED-48H-LINUX": "linux", "S24-L3-LRMIXED-48H-WINDOWS": "windows",
    "S3-L3-AB-48H-LINUX": "linux", "S3-L3-AB-48H-WINDOWS": "windows",
}
LEDGER_PATH: Path      # <repo>/realhw/acceptance/candidate_longrun_starts.ndjson
OD09_CODES: tuple[str, ...] = ("HARNESS_OD09_BASELINE_INVALID", "HARNESS_OD09_TOO_LATE", "HARNESS_OD09_NOT_FROZEN")
class Od09Error(Exception):            # .code ∈ OD09_CODES（其他碼 → ValueError）、.detail: list[str]
    def __init__(self, code: str, detail: list[str] | None = None) -> None
PROPOSAL_RULES: Mapping[str, str]      # 公式的人類可讀描述，原樣寫入 proposal
@dataclass(frozen=True)
class BaselineStats:
    platform: str
    artifact_id: str
    daemon_implementation: str
    duration_s: int
    snapshot_interval_s: int
    max_snapshot_gap_s: int
    rss_peak_kb: int
    rss_steady_median_kb: int
    rss_steady_slope_kb_per_h: float
    hourly_done: Mapping[str, Mapping[str, int]]    # "<com>|<source>" -> {"min": n, "p10": n, "hours": h}
    completed_total: Mapping[str, int]              # "<com>|<source>" -> 全程 completed（counting.definitions.completed）
def workload(acceptance_path: Path) -> Mapping[str, Any]          # 回 acceptance 的 workload 段（時長、間隔、缺口係數唯一來源）
def baseline_stats(case_dir: Path, *, platform: str, release_manifest: Path) -> BaselineStats
def validate_baseline(stats: BaselineStats, *, acceptance_path: Path) -> list[str]
def propose(linux: BaselineStats, windows: BaselineStats, *, baseline_gates: Mapping[str, Path]) -> dict
def freeze(acceptance_path: Path, proposal: dict, *, decision_id: str, approved_by: str, overrides: Mapping[str, Any],
           stage0_index: Path, evidence_root: Path, ledger_path: Path = LEDGER_PATH, addenda_path: Path, now: str) -> dict
def check_frozen_before(acceptance_path: Path, run_started_at: str) -> list[str]
def register_candidate_start(acceptance_path: Path, *, item_id: str, platform: str, run_id: str, started_at: str,
                             ledger_path: Path = LEDGER_PATH) -> dict
def build_parser() -> argparse.ArgumentParser
def main(argv: list[str] | None = None) -> int   # exit：0 成功／check 無違規；1 check 有違規；2 參數錯或 ValueError；5 Od09Error（stdout {"ok":false,"reason_code","detail"}）
```

CLI 子命令（`build_parser()`；JSON 一律 compact、`ensure_ascii=False`）：

| 子命令 | 參數 | 呼叫 |
|---|---|---|
| `stats` | `--case-dir DIR --platform P --release-manifest PATH --out PATH` | `baseline_stats`＋`validate_baseline`（違規 → exit 1） |
| `propose` | `--linux-stats JSON --windows-stats JSON --baseline-gate linux=PATH --baseline-gate windows=PATH --out PATH` | `propose` |
| `freeze` | `--acceptance PATH --proposal PATH --decision-id ID --approved-by TEXT [--set <key>.per_platform.<os>.<field>=VALUE ...] --stage0-index PATH --evidence-root DIR [--ledger PATH] --addenda PATH [--now ISO] --out PATH` | `freeze`（`--now` 預設現在 UTC） |
| `check` | `--acceptance PATH --run-started-at ISO` | `check_frozen_before` |
| `register-candidate` | `--acceptance PATH --item ID --platform P --run-id ID --started-at ISO [--ledger PATH]` | `register_candidate_start` |

`baseline_stats`：`duration_s = last_t - first_t`；`max_snapshot_gap_s` 為相鄰快照最大間隔；`snapshot_interval_s` 為間隔中位數；RSS：`peak = max(rss_kb)`，穩態窗＝最後 24 h 快照（與 Task 15 gate `resources` 的 steady／slope 窗相同），`median`、`slope`（最小平方法，kb/h）；`hourly_done`：依 `t // 3600` 分桶，每 (`com`, `source`) 只計 `kind=="done"`，只統計完整小時，取 `min` 與第 10 百分位（nearest-rank）；`completed_total`：每 (`com`, `source`) 全程 completed 數，定義與 `counting.definitions.completed` 相同；`daemon_implementation`、`artifact_id` 取自 `release_manifest`（`components.daemon.implementation`、manifest sha256）。

`validate_baseline` 違規（時長與係數一律讀 `workload(acceptance_path)`，§15.11.4 唯一來源）：`daemon_implementation != "python"`；`duration_s < workload.min_continuous_duration_s`；`max_snapshot_gap_s > workload.max_observation_gap_factor × snapshot_interval_s`（不得拼接 segments）；任一 (`com`,`source`) 的 `min == 0`。

`propose` 固定公式（本計畫先於基線存在而定，**不得在看到基線後修改公式；擁有者只能在 freeze 時以 `overrides` 明示覆寫，覆寫原樣記錄**）。`I = max(linux.snapshot_interval_s, windows.snapshot_interval_s)`；路徑逐字等於 `thresholds.<key>.per_platform.<os>.<field>`：

| 路徑（`thresholds.` 之下） | 值 |
|---|---|
| `resources.per_platform.<os>.rss_peak_kb` | `ceil(1.25 × s.rss_peak_kb)` |
| `resources.per_platform.<os>.rss_steady_kb` | `ceil(1.25 × s.rss_steady_median_kb)` |
| `resources.per_platform.<os>.rss_slope_kb_per_h` | `max(ceil(2 × s.rss_steady_slope_kb_per_h), 256)` |
| `min_effective_load.per_platform.<os>.min_completed_per_board_source` | `{"<com>\|<source>": max(1, floor(0.5 × s.completed_total[key]))}`，鍵＝該平台基線出現的全部 (`com`,`source`) |
| `min_effective_load.per_platform.<os>.max_idle_gap_s` | `3600` |
| `comparison_uncertainty.per_platform.<os>.error_rate_abs_tolerance` | `0.002` |
| `comparison_uncertainty.per_platform.<os>.throughput_ratio_tolerance` | `0.02` |
| `comparison_uncertainty.per_platform.<os>.stuck_s_tolerance` | `I` |
| `comparison_uncertainty.per_platform.<os>.max_reruns` | `1` |

proposal 形狀與 acceptance 子樹同構：`{"thresholds": {<key>: {"per_platform": {"linux": {...}, "windows": {...}}}}, ...}`，產出後以 `PER_PLATFORM_FIELDS` 自我檢查。另含 `baseline_artifacts`（每平台 `{artifact_id, release_gate_sha256}`，`release_gate_sha256` 取自 `baseline_gates[platform]` 檔案 sha256）、`proposal_rules`、`generated_at`、`acceptance_revision`、`proposal_sha256`（不含自身欄位：`sha256(json.dumps(..., sort_keys=True, separators=(",",":"), ensure_ascii=False))`）。

`freeze` 前置（任一不符 → `Od09Error`，acceptance 檔與 addenda bytes 不變）：
1. **基線證據（只讀 Stage 0 索引，不依賴 P9）**：對每個平台以 `check_evidence_index.verify_item(stage0_index, BASELINE_ITEMS[p], root=evidence_root)`（依路徑載入 `tests/acceptance/check_evidence_index.py`）取得項目，違規非空 → `HARNESS_OD09_BASELINE_INVALID`；其 `artifact_path` 的 `release-gate.json` 必須 `mode=="baseline"`、`platform==p`、`baseline_result=="PASS"`、`acceptance.sha256` 等於現行 acceptance 檔的 `gate.load_acceptance(...)[1]["sha256"]`（基線以凍結前的同一 revision 判定）、`run.ended_at_utc < now`，且其 sha256 等於 `proposal.baseline_artifacts[p].release_gate_sha256`——任一不符 → `HARNESS_OD09_BASELINE_INVALID`。
2. **時序**：`ledger_path` 有任何一行（可解析或不可解析皆算）→ `HARNESS_OD09_TOO_LATE`（已有 Rust 候選長跑登記開跑；防止把 acceptance 回退成 deferred 後事後重凍）；acceptance 任一 `thresholds.<DEFERRED_KEYS>.status` 已為 `frozen` → `HARNESS_OD09_TOO_LATE`。
3. **proposal 完整性**：`proposal_sha256` 重算不符或 `proposal.acceptance_revision` 不等於現行 `revision` → `HARNESS_OD09_BASELINE_INVALID`。
4. **介面一致性**：acceptance 每個 `DEFERRED_KEYS` 必須有 `per_platform`，平台集合恰為 `PLATFORMS`、每平台欄位集合恰為 `PER_PLATFORM_FIELDS[key]`，proposal 亦同；`overrides` 每個鍵必須是 `"<key>.per_platform.<os>.<field>"` 且指向既有欄位——任一不符 → `HARNESS_OD09_BASELINE_INVALID`（`detail` 列出多出／缺少的路徑）。

通過後對每個 `DEFERRED_KEYS` 改寫 `thresholds.<key>`：`status: frozen`、`frozen_at: now`、保留 `decision: OD-09` 與 `freeze_deadline`、新增 `decision_id`、`approved_by`、`overrides`、`proposal_sha256`、`baseline_evidence`（兩平台各 `{"item_id","artifact_path","sha256"}`），並把 proposal 值（套上 overrides）逐欄寫進**既有的** `thresholds.<key>.per_platform.<os>.<field>`（兩平台一次寫入）；頂層 `revision` 改為 `f"{now 日期}.{n}"`（同日第 n 次）；其他鍵逐值保留；以 PyYAML `safe_dump(sort_keys=False, allow_unicode=True)` 寫回並以 `gate.load_acceptance` 驗證。在 `addenda_path` 附加一列 `| <decision_id> | OD-09 延後項凍結 | <now 日期> | approved_by=<…>；revision=<新 revision>；proposal_sha256=<…>；overrides=<鍵清單或 無> |`。回傳 `{"thresholds":{三鍵寫入後內容},"revision","acceptance_sha256","frozen_at","baseline_evidence","requires_baseline_gate_rerun":true}`；CLI `freeze --out od09-freeze.json` 寫成報告，作為 Stage 0 索引 `S0-OD09-DEFERRED-FROZEN` 的 artifact（之後 P9 Task 14B 以 `od09-freeze` kind ingest 同一檔）。凍結後以新 revision 對兩平台已保存的基線證據離線重跑 `python3 -m realhw.acceptance.gate --mode baseline`，供候選引用（Task 15 `baseline_compatible` 不要求 revision 相同）。

`check_frozen_before(path, ts)`：讀與正式 gate 相同的路徑，回傳違規清單——任一 `DEFERRED_KEYS` 的 `status != "frozen"` → `"<key> not frozen"`；`frozen_at >= ts` → `"<key> frozen after run start"`；`status == "frozen"` 但任一 `per_platform.<os>.<field>` 缺少或為 null → `"<key>.per_platform.<os>.<field> missing"`；`gate.load_acceptance` 拋 schema 錯誤 → `"acceptance schema invalid: <訊息>"`。

`register_candidate_start`：`item_id ∉ CANDIDATE_LONGRUN_ITEMS` 或 `platform != CANDIDATE_LONGRUN_ITEMS[item_id]` 或 `run_id` 已在 ledger → `ValueError`（ledger 不變）；`check_frozen_before(acceptance_path, started_at)` 非空 → `Od09Error("HARNESS_OD09_NOT_FROZEN", detail=violations)`（ledger 不變）；否則以 `"a"` 附加一行 `{"item_id","platform","run_id","started_at","acceptance_revision","acceptance_sha256"}`（`sort_keys`、compact、`flush`＋`fsync`）並回傳該紀錄。**每個 Rust 候選長跑在啟動 `python3 -m realhw --tier longrun` 前必須先登記**（P4 Task 16 B-12、P8 Task 15 第 8 項、P9 Task 6 L3 A/B 的 B 側）；未登記的長跑仍會被 Task 15 gate 的 `pre_frozen` 判 BLOCKED（凍結晚於 `meta.started_at_utc`），登記則讓「未凍結就開跑」在開跑前即被拒。

**凍結後 gate 判定**：沿用 Task 15 gate 既有規則，本 Task 不另定義。

### T 步驟

- [ ] **T16-1：撰寫失敗測試**。`tests/test_od09_freeze.py` 關鍵測試：

```python
import ast, json, subprocess, sys, textwrap
from pathlib import Path

import pytest

import fakes_od09
from realhw.acceptance import od09_freeze   # T 步驟期間改為各測試函式內 import（§1 第 3 條），此處為閱讀方便

ROOT = Path(__file__).resolve().parents[1]
pytestmark = [pytest.mark.core_portable, pytest.mark.memory_only]


@pytest.mark.process_rpc
def test_stage0_chain_without_later_phases(tmp_path):
    # R4-F2：基線 → 凍結 → 登記 → candidate gate 全鏈只用 P2 交付物；任何 P3～P9 模組被 import 即失敗
    script = textwrap.dedent(f"""
        import importlib.abc, json, sys
        BLOCKED = ("tools.stage3", "sw_core")
        class _Block(importlib.abc.MetaPathFinder):
            def find_spec(self, name, path=None, target=None):
                if name.startswith(BLOCKED):
                    raise ImportError("later-phase module imported: " + name)
                return None
        sys.meta_path.insert(0, _Block())
        sys.path.insert(0, {str(ROOT)!r}); sys.path.insert(0, {str(ROOT / "tests")!r})
        from pathlib import Path
        import fakes_od09
        from realhw.acceptance import gate, od09_freeze
        env = fakes_od09.od09_env(Path({str(tmp_path)!r}))
        assert not (env.repo / "tests" / "acceptance" / "stage_exits.json").exists()
        assert not (env.repo / "docs" / "rust-refactor" / "evidence" / "index.json").exists()
        prop = od09_freeze.propose(env.linux_stats, env.windows_stats, baseline_gates=env.baseline_gates)
        od09_freeze.freeze(env.acceptance, prop, decision_id="OD-09-F1", approved_by="owner", overrides={{}},
                           stage0_index=env.stage0_index, evidence_root=env.evidence_root, ledger_path=env.ledger,
                           addenda_path=env.addenda, now=fakes_od09.ts(20, 9))
        od09_freeze.register_candidate_start(env.acceptance, item_id="S1B-L3-MIXED-48H-WINDOWS", platform="windows",
                                             run_id="s1b-1", started_at=fakes_od09.ts(21, 0), ledger_path=env.ledger)
        run = fakes_od09.longrun_case(env.repo / "cand", platform="windows", started_at=fakes_od09.ts(21, 0))
        out = gate.evaluate(run.inputs(mode="candidate", acceptance_path=env.acceptance,
                                       baseline=fakes_od09.rerun_baseline_gate(env, platform="windows")))
        res = {{c["id"]: c["result"] for c in out["checks"]}}
        print(json.dumps({{"res": res, "release": out["release_gate_result"],
                          "later": sorted(m for m in sys.modules if m.startswith(BLOCKED))}}))
    """)
    p = subprocess.run([sys.executable, "-c", script], capture_output=True, text=True, timeout=300)
    assert p.returncode == 0, p.stderr
    got = json.loads(p.stdout.strip().splitlines()[-1])
    assert got["later"] == []
    assert got["res"]["resources"] == got["res"]["min_effective_load"] == got["res"]["comparison_uncertainty"] == "PASS"
    assert got["release"] == "PASS"


def test_od09_freeze_imports_only_stage0():
    src = (ROOT / "realhw" / "acceptance" / "od09_freeze.py").read_text(encoding="utf-8")
    mods = set()
    for node in ast.walk(ast.parse(src)):
        if isinstance(node, ast.Import):
            mods |= {a.name for a in node.names}
        elif isinstance(node, ast.ImportFrom) and node.level == 0:
            mods.add(node.module)
    allowed_extra = {"yaml", "realhw", "realhw.acceptance", "realhw.acceptance.gate"}
    assert {m for m in mods if m.split(".")[0] not in sys.stdlib_module_names} <= allowed_extra, mods
    for banned in ("tools.stage3", "sw_core", "stage_exits.json", "evidence/index.json"):
        assert banned not in src


def test_freeze_refused_after_candidate_registered(tmp_path):
    env = fakes_od09.od09_env(tmp_path)
    env.ledger.write_text('{"item_id":"S1B-L3-MIXED-48H-WINDOWS","platform":"windows","run_id":"x",'
                          '"started_at":"2026-11-19T00:00:00+00:00"}\n', encoding="utf-8")   # 模擬 acceptance 被回退後的重凍
    before = env.acceptance.read_bytes()
    prop = od09_freeze.propose(env.linux_stats, env.windows_stats, baseline_gates=env.baseline_gates)
    with pytest.raises(od09_freeze.Od09Error) as ei:
        od09_freeze.freeze(env.acceptance, prop, decision_id="OD-09-F1", approved_by="owner", overrides={},
                           stage0_index=env.stage0_index, evidence_root=env.evidence_root, ledger_path=env.ledger,
                           addenda_path=env.addenda, now=fakes_od09.ts(20, 9))
    assert ei.value.code == "HARNESS_OD09_TOO_LATE"
    assert env.acceptance.read_bytes() == before


def test_register_candidate_refused_before_freeze(tmp_path):
    env = fakes_od09.od09_env(tmp_path)
    with pytest.raises(od09_freeze.Od09Error) as ei:
        od09_freeze.register_candidate_start(env.acceptance, item_id="S1B-L3-MIXED-48H-WINDOWS", platform="windows",
                                             run_id="s1b-1", started_at=fakes_od09.ts(21, 0), ledger_path=env.ledger)
    assert ei.value.code == "HARNESS_OD09_NOT_FROZEN"
    assert sorted(ei.value.detail) == sorted(f"{k} not frozen" for k in od09_freeze.DEFERRED_KEYS)
    assert env.ledger.read_bytes() == b""
```

（`fakes_od09.od09_env(root, *, linux_rss_peak=40000, linux_slope=10.0, interval=300, duration_s=None, rust_baseline=False)`：在 `root/repo` 建只含 P2 檔的最小 repo 樹——acceptance 與 schema 自 repo 實際 `realhw/acceptance/longrun_acceptance.yaml`、`longrun_acceptance.schema.json` **原樣複製**（變形只能在複本上以 dotted-path 修改單一鍵）、`tests/acceptance/stage0_evidence_index.yaml` 與 `check_evidence_index.py`、`evidence_index.schema.json` 原樣複製；以 `LongrunFixture(...).healthy(hours=48, platform=p, started_at=ts(10,0) 起依序兩平台)`（`started_at` 若 Task 15 builder 尚無，於本 T 步驟以 keyword-only 擴充、預設不變）建兩平台 Python 基線 case_dir，以複本 acceptance 執行 `gate.evaluate(mode="baseline")` 寫出 `evidence_root/<item_id>/release-gate.json`，再把索引複本的兩基線項改為 `present`＋相對路徑＋sha256；`duration_s=None` 時取 `workload.min_continuous_duration_s`；回傳 `SimpleNamespace(repo, acceptance, stage0_index, evidence_root, ledger, addenda, linux_stats, windows_stats, linux_case_dir, windows_case_dir, baseline_gates)`。`longrun_case(dir, *, platform, started_at, rss_peak_kb=None, completed_scale=1.0)` 以 `LongrunFixture` 建健康候選，`inputs(*, mode, acceptance_path, baseline)` 產 `gate.GateInputs`；`rerun_baseline_gate(env, *, platform)` 以凍結後的 acceptance 對 `env.<platform>_case_dir` 執行 `gate.evaluate(mode="baseline")` 並寫出 `release-gate.json`，回傳路徑；`ts(day, hour)` → `f"2026-11-{day:02d}T{hour:02d}:00:00+00:00"`。）

| 測試名 | 斷言 |
|---|---|
| `test_od09_env_baselines_present` | 替身自檢：`check_evidence_index.verify_item` 對兩基線項回 `(item, [])` 且 `item["status"] == "present"` |
| `test_stage0_index_od09_item_ids` | repo 實際 `stage0_evidence_index.yaml` 含 `od09_freeze.BASELINE_ITEMS` 兩值與 `FREEZE_ITEM`，三項 `status == "blocked_owner"` |
| `test_baseline_stats_from_lr_mixed` | 合成 48h、間隔 300 s → `duration_s == workload.min_continuous_duration_s`、`hourly_done["COM0|agent:rhw0"]["hours"] == 48` |
| `test_spliced_baseline_rejected` | 中間缺 1 小時快照 → `validate_baseline` 含 `"snapshot gap"` |
| `test_short_baseline_rejected` | 少 1 小時 → 違規 `"duration"` |
| `test_thresholds_read_from_workload` | 複本 `min_continuous_duration_s` 改 3600 → 1.5h 基線不因時長違規 |
| `test_rust_baseline_rejected` | `daemon_implementation == "rust"` → 違規 |
| `test_load_stop_rejected` | 某來源某完整小時 done=0 → 違規 |
| `test_proposal_formula_fixed` | `linux_rss_peak=40000, linux_slope=10.0, interval=300` → linux `rss_peak_kb == 50000`、`rss_slope_kb_per_h == 256`；`comparison_uncertainty.per_platform.linux == {"error_rate_abs_tolerance": 0.002, "throughput_ratio_tolerance": 0.02, "stuck_s_tolerance": 300, "max_reruns": 1}`；windows `max_idle_gap_s == 3600`、`min_completed_per_board_source["COM0|agent:rhw0"] == max(1, completed_total//2)`；無 `tolerance`／`rerun_rule` 鍵；`baseline_artifacts.<p>.release_gate_sha256` == 對應檔 sha256 |
| `test_per_platform_fields_match_acceptance` | `PER_PLATFORM_FIELDS` 每個 key 的欄位集合 == repo 實際 acceptance `thresholds.<key>.per_platform.linux`／`.windows` 鍵集合 |
| `test_real_acceptance_roundtrip_propose_freeze_check_gate` | 以 repo 實際門檻檔複本走 propose → freeze → `check_frozen_before(ts(21,0)) == []` → `gate.evaluate` candidate：峰值 = 凍結值 − 1 → 三延後項 PASS、整體 PASS；+1 → `resources` FAIL；凍結後檔案無 `deferred` 鍵、欄位集合與凍結前相同、無 null、`thresholds.<key>` 只多出凍結紀錄欄位 |
| `test_freeze_writes_only_deferred_and_bumps_revision` | 三個已凍結項與其他頂層鍵逐值不變；三延後項 `status == "frozen"`、`decision_id == "OD-09-F1"`、兩平台值 == proposal；`baseline_evidence` 兩筆 `item_id` == `BASELINE_ITEMS` 值且 sha256 == 索引記錄；`revision` 改變；`gate.load_acceptance` 通過 |
| `test_freeze_records_overrides` | `overrides={"resources.per_platform.linux.rss_peak_kb": 60000}` → linux 值 60000、windows 不變、`overrides` 原樣記錄、addenda 該列含該鍵 |
| `test_freeze_rejects_unknown_override_path` | `{"resources.rss_budget.linux": 1}` 或 `{"comparison_uncertainty.per_platform.linux.tolerance": 1}` → `HARNESS_OD09_BASELINE_INVALID`、acceptance bytes 不變、addenda 無新列 |
| `test_freeze_rejects_field_set_mismatch` | 複本刪 `thresholds.resources.per_platform.windows.rss_slope_kb_per_h` → `HARNESS_OD09_BASELINE_INVALID`、`detail` 含該路徑；proposal 刪一欄並重算 sha → 同上；acceptance bytes 不變 |
| `test_freeze_requires_present_baselines_in_stage0_index` | 各變體皆 `HARNESS_OD09_BASELINE_INVALID` 且 acceptance bytes 不變：windows 項 `status: blocked_owner`；索引 sha256 與檔案不符；baseline gate `baseline_result == "FAIL"`；`mode == "candidate"`；`acceptance.sha256` 與現行檔不同；`run.ended_at_utc >= now`；`proposal.baseline_artifacts.windows.release_gate_sha256` 與索引不符 |
| `test_refreeze_refused` | 已 frozen 再 freeze → `HARNESS_OD09_TOO_LATE` |
| `test_freeze_refused_on_malformed_ledger_line` | ledger 只有一行 `{bad` → `HARNESS_OD09_TOO_LATE` |
| `test_tampered_proposal_rejected` | 改值不更新 sha → `HARNESS_OD09_BASELINE_INVALID`；`acceptance_revision` 不同 → 同上 |
| `test_check_frozen_before` | 未凍結 → 三條 `not frozen`；凍結於 t，run 於 t+1s → `[]`；t-1s → 三條 `frozen after run start` |
| `test_check_frozen_before_rejects_null_values` | 正常凍結後把三項 `per_platform.windows` 各欄改回 null → 含 `"comparison_uncertainty.per_platform.windows.max_reruns missing"` 等每個 null 欄位與 `"acceptance schema invalid: …"`，不得回 `[]` |
| `test_register_candidate_after_freeze_appends` | freeze 於 `ts(20,9)` 後登記 `started_at=ts(21,0)` → ledger 一行，`acceptance_revision` == 新 revision、`acceptance_sha256` == 新檔 sha256；`started_at=ts(20,8)`（早於 `frozen_at`）→ `HARNESS_OD09_NOT_FROZEN`、ledger 仍一行 |
| `test_register_candidate_rejects_unknown_item_or_platform` | `item_id="S0-L3-P0P1"` → `ValueError`；`S1B-L3-MIXED-48H-WINDOWS` 配 `platform="linux"` → `ValueError`；重複 `run_id` → `ValueError`；三者 ledger bytes 不變 |
| `test_addenda_append_only` | 兩次（不同 acceptance 檔）freeze → addenda 前段原文不變、新增兩列 |
| `test_od09_cli_exit_codes` | `main(["check","--acceptance",A,"--run-started-at",T])`：未凍結 → 1、凍結後 → 0；`register-candidate` 未凍結 → 5 且 stdout `reason_code == "HARNESS_OD09_NOT_FROZEN"`；未知 item → 2 |
| `test_od09_procedure_doc` | `docs/rust-refactor/procedures/od09-deferred-freeze.md` 存在；含 `S1B-L3-MIXED-48H-WINDOWS`、`Stage 0 出口`、`register-candidate`；每個 ```` ```bash ```` 區塊中以 `python3 -m realhw.acceptance.od09_freeze ` 開頭的行皆可被 `od09_freeze.build_parser().parse_args` 解析，且 `stats`、`propose`、`freeze`、`check`、`register-candidate` 五個子命令各至少一行；文件不含 `python3 -m tools.stage3 od09`（寫入端只有 P2） |

`tests/test_longrun_gate_frozen.py`（acceptance 一律為 repo 實際檔複本經本 Task `freeze` 寫出的結果，不得手寫凍結後 acceptance）：

| 測試名 | 斷言 |
|---|---|
| `test_deferred_still_blocked` | 未凍結 → 三項 check `BLOCKED`、`release_gate_result == "BLOCKED"` |
| `test_frozen_resources_fail_over_budget` | freeze 後 linux 峰值 = 凍結值 + 1 → `resources` FAIL、整體 FAIL；以 override 讓兩平台值不同，同一峰值 linux PASS、windows FAIL |
| `test_frozen_min_load_insufficient` | `COM0\|agent:rhw0` 全程 completed = 下限 − 1 → `min_effective_load` `INSUFFICIENT_EVIDENCE`、`detail` 含該鍵；相鄰 completed 間隔 3601 s → 同上 |
| `test_frozen_tolerance_band_insufficient` | 錯誤率 = baseline + 0.001 → `error_rate_relative` INSUFFICIENT；+0.003 → FAIL；throughput 為基線 94% → `throughput_relative` INSUFFICIENT |
| `test_baseline_mode_not_applicable` | baseline 模式、三項已凍結 → 三項 `NOT_APPLICABLE_BASELINE` |
| `test_all_frozen_candidate_pass` | 候選 `started_at` 晚於 `frozen_at`、基線 gate 以新 revision 重跑 → 三延後項 PASS、整體 PASS |

- [ ] **T16-2：執行並確認 FAIL**：`python3 -m pytest -q tests/test_od09_freeze.py tests/test_longrun_gate_frozen.py` → `ModuleNotFoundError: realhw.acceptance.od09_freeze`（函式內 import），`test_stage0_index_od09_item_ids` 因索引 id 未固定而紅。
- [ ] **T16-3：commit 測試**：`git commit -m "test(realhw): 新增 OD-09 延後項凍結與候選長跑登記的失敗測試"`（含 `tests/fakes_od09.py`）。

### I 步驟

- [ ] **I16-1：實作** 依規則與公式表；百分位與分桶只用 stdlib；YAML 以 PyYAML；**不修改 `gate.py`**；寫入路徑只用 `thresholds.<key>.per_platform.<os>.<field>`；`od09-deferred-freeze.md` 寫 Stage 0 出口程序：(1) 確認 B8 兩平台基線已登錄於 `stage0_evidence_index.yaml`（`present`＋sha256）；(2) `python3 -m realhw.acceptance.od09_freeze stats …` 兩平台；(3) `propose`；(4) 擁有者核准後 `freeze --acceptance … --proposal … --decision-id … --approved-by … [--set <key>.per_platform.<os>.<field>=VALUE …] --stage0-index tests/acceptance/stage0_evidence_index.yaml --evidence-root … --addenda docs/rust-refactor/owner-decisions-addenda.md --now <ISO> --out od09-freeze.json`；(5) 以新 revision 離線重跑兩平台基線 gate；(6) 把 `S0-OD09-DEFERRED-FROZEN` 改 `present`＋sha256；(7) 此後每個 Rust 候選長跑（最早 `S1B-L3-MIXED-48H-WINDOWS`）開跑前 `register-candidate`；時序：必須在 Stage 0 出口、任何候選長跑前完成，ledger 有任何紀錄後 `freeze` 一律拒絕。
- [ ] **I16-2：執行並確認 PASS**：上列兩檔＋Task 12、15 測試全綠；`python3 -m pytest -q tests/` 無新失敗。
- [ ] **I16-3：commit**：`git commit -m "feat(realhw): 新增 OD-09 延後項凍結程序、候選長跑開跑登記與凍結輸出對正式 gate 的整合驗證"`＋fragment（不含 `gate.py`）。

---

## Task 17：docker 驗證環境與兩容器拓樸（OD-15）

**Test author model:** haiku-xhigh　**Test reviewer:** agy-gemini-3.8-flash-high　**Implementer model:** codex-gpt-6-luna-max（容器內 PTY pair、systemd PID 1、長跑 runner 的行程與資源隔離）
**Changelog fragment:** `changelog.d/rust-p2-docker-verification.md`（`type: feat`、`scope: test-infra`）：新增 docker 優先驗證環境：假 COM（PTY pair）兩容器拓樸、systemd container、48 h 長跑與 >72 h 監管 runner，報告一律標示 L1.5-docker。
**BLOCKED-OWNER evidence:** 無（docker 在本環境可用）。真板 L2／實機 L3／OD-10 bench 長跑（B4、B5、B8）**不因本 Task 解除**。

**範圍（OD-15 (3)(4)(5)(6)(7)）：**
1. 兩容器拓樸：`serialwrap` 容器（Python 基線或掛入的 Rust bundle）⇄ `bridge`（`tools/uart_stdio_bridge` 於 `serialwrap` 容器內作為獨立行程，持 PTY 對端）⇄ `dut-sim` 容器（Task 4／6 的 DUT 模擬器 image，以 `-i` 無 `-t` stdin/stdout 接 bridge）。假 COM 以 `socat -d -d pty,raw,echo=0,link=/run/sw-fake/by-id/usb-fake-<n> pty,raw,echo=0,link=/run/sw-fake/peer-<n>` 建 PTY pair，`SERIALWRAP_BY_ID_DIR=/run/sw-fake/by-id`（只含假 by-id）。
2. `physical_uart` 類測試在容器內以假 COM 執行：pytest marker 不改（P0 T14 擁有），Task 13 資源報告另記 `uart_kind: "pty-fake"`；`l1_judge`／證據索引不得把 `pty-fake` 視為真實 `physical_uart` 證據。
3. systemd container：systemd 為 PID 1，啟用 user lingering 跑 `systemd-user` 模式（另一個 compose profile 跑 `systemd-system`）；驗 journald 收到 daemon stderr、SC-23 autostart hold drop-in（`Restart=no`、`RefuseManualStart=yes`）以 `systemctl show` 驗證、SC-12 reconcile 先停舊再起新。新版 serialwrap 一律先在此驗證（OD-15 (4)）。
4. 長跑：`run-longrun.sh --hours 48`（P1＋P1-OC 合併後才可啟動）；`run-supervision.sh --hours 72`（Linux 監管 >72 h，與 48 h 長跑並行，不同 compose project、volume、網路；監管模式 `systemd-user` 與 `systemd-system` 各一）。
5. 證據標記：`evidence_layer` 取自封閉集合 `{"L1.5-local", "L1.5-docker", "host-throwaway", "native-windows", "L2-bench", "L3-bench", "hw-bench"}`（與 P9 共用，只增不改）；docker／throwaway runner 產出的報告**必須**寫 `evidence_layer` 與 `producer`（fail-closed：缺鍵即拒收）。每份 docker 報告寫 `evidence_layer: "L1.5-docker"`、`producer`、`topology: "docker-2c"`、`uart_kind`、`image_digests`（三個 image 的 `sha256:` digest）、`bundle_tree_sha256`（Rust 候選；Python 基線為 `package_tree_sha256`）；`check_evidence_index.py` 依 `required_layer` 拒收（Task 12 段落）。

**Files:**
- Create: `tools/docker/serialwrap.Dockerfile`（`python:3.12-slim@sha256:<與 Task 6 同 digest>`＋socat、`pip install` repo；Rust bundle 以唯讀 bind mount `/opt/sw-bundle`）、`tools/docker/dut-sim.Dockerfile`（若 Task 6 `tools/uart_stdio_bridge/docker/Dockerfile.dut` 已滿足則為指向它的薄包裝，不重複定義）、`tools/docker/systemd.Dockerfile`（`debian:12-slim@sha256:<固定>`＋`systemd`、`dbus-user-session`，`STOPSIGNAL SIGRTMIN+3`）、`tools/docker/compose.l15.yaml`（services：`serialwrap`、`dut-sim`；profiles：`systemd-user`、`systemd-system`）、`tools/docker/fake_uart.sh`（建立／清除 PTY pair 與 sandbox by-id）
- Create: `scripts/docker/run-l15.sh <suite> [--dry-run] [--bundle DIR]`（suite：`pytest-physical-uart`、`func-test-bridge`、`realhw-p0p1`、`regression-nondestructive`）、`scripts/docker/run-systemd.sh [--mode systemd-user|systemd-system] [--dry-run]`、`scripts/docker/run-longrun.sh --hours N [--dry-run]`、`scripts/docker/run-supervision.sh --hours N --mode … [--dry-run]`
- Create: `tests/acceptance/evidence_labels.py`（`LAYERS`、`label_report(report: dict, **fields) -> dict`、`assert_not_real_board(report: dict) -> None`、`layer_satisfies(report_layer: str, required_layer: str) -> bool`）
- Modify: `tests/acceptance/evidence_index.schema.json`（item 新增必填 `required_layer`，列舉＝`LAYERS ∪ {"L1"}`；既有 item 依性質補值）、`tests/acceptance/check_evidence_index.py`（讀 `present` 項報告 JSON：先 `validate_report_layer`（缺鍵 → `evidence_layer_missing`、未知值 → `evidence_layer_unknown`），再 `layer_satisfies` 與 `assert_not_real_board`，不符 → 違規 `evidence_layer_mismatch`；`required_layer: L1` 的項目改由 Task 13 `l1_judge` 判定）、`tests/acceptance/stage0_evidence_index.yaml`（新增 `S0-L15-DOCKER-48H-LINUX`（`required_layer: L1.5-docker`）、`S0-L15-DOCKER-SUPERVISION-72H`、`S0-L15-DOCKER-SYSTEMD`，初始 `missing`）、`tests/resource_report_plugin.py`（Task 13；報告加 `uart_kind`，未設 `SW_UART_KIND` 時為 `"native"`）
- Create: `tests/test_docker_l15.py`（`core_portable`＋`memory_only`；呼叫 `--dry-run` 子程序者改標 `process_rpc`；真跑 docker 者另標 `docker` 資源 marker 並以 `cap:docker` 能力式 SKIP）、`tests/test_evidence_labels.py`（`core_portable`＋`memory_only`）

**Interfaces — Produces：**

```python
# tests/acceptance/evidence_labels.py（只 import stdlib；不得 import sw_core）
LAYERS: frozenset[str] = frozenset({"L1.5-local", "L1.5-docker", "host-throwaway", "native-windows",
                                    "L2-bench", "L3-bench", "hw-bench"})   # evidence_layer 封閉集合（與 P9 共用，只增不改）
REAL_LAYERS: frozenset[str] = frozenset({"L2-bench", "L3-bench", "hw-bench"})
FAIL_CLOSED_PRODUCERS: frozenset[str] = frozenset({"run-l15.sh", "run-systemd.sh", "run-longrun.sh",
                                                   "run-supervision.sh", "win-task-throwaway.ps1"})
def label_report(report: dict, *, evidence_layer: str, producer: str, topology: str, uart_kind: str,
                 image_digests: dict[str, str] | None = None, tree_sha256: str | None = None) -> dict
    # 回傳新 dict（含 evidence_layer、producer）；evidence_layer 不在 LAYERS → ValueError；不覆寫既有 evidence_layer（不同值 → ValueError）
def validate_report_layer(report: dict) -> str
    # fail-closed：report["producer"] ∈ FAIL_CLOSED_PRODUCERS 而缺 evidence_layer → ValueError("evidence_layer_missing")；
    # evidence_layer 存在但不在 LAYERS → ValueError("evidence_layer_unknown")；回傳 evidence_layer
def layer_satisfies(report_layer: str, required_layer: str) -> bool
    # 相等 → True；required ∈ REAL_LAYERS 時只有相等才 True；required == "L1.5-local" 時 "L1.5-docker" 亦 True；其餘 False
def assert_not_real_board(report: dict, required_layer: str) -> None
    # report["evidence_layer"] ∈ {"L1.5-docker","host-throwaway"} 且 required_layer ∈ REAL_LAYERS → AssertionError；
    # 另 report 含 counts_as 且與 REAL_LAYERS 相交時同樣 AssertionError
```

`evidence_index.schema.json` 的 `required_layer` 列舉＝`LAYERS ∪ {"L1"}`（`L1` 項目由 Task 13 `l1_judge` 判定，不讀 `evidence_layer`）。

`scripts/docker/*.sh --dry-run`：只印出將執行的 `docker compose …`／`docker run …`／容器內 pytest 指令（一行一個，`+ ` 前綴），exit 0，不呼叫 docker。

### T 步驟

- [ ] **T17-1：撰寫失敗測試**。`tests/test_evidence_labels.py`：

```python
import pytest

pytestmark = [pytest.mark.core_portable, pytest.mark.memory_only]


def _el():
    from tests.acceptance import evidence_labels   # 函式內 import（§1 第 3 條）
    return evidence_labels


def test_layers_exact_closed_set():
    el = _el()
    assert el.LAYERS == frozenset({"L1.5-local", "L1.5-docker", "host-throwaway", "native-windows",
                                   "L2-bench", "L3-bench", "hw-bench"})
    assert el.REAL_LAYERS == frozenset({"L2-bench", "L3-bench", "hw-bench"})


def test_docker_layer_never_satisfies_real_board():
    el = _el()
    for real in ("L2-bench", "L3-bench", "hw-bench"):
        assert el.layer_satisfies("L1.5-docker", real) is False
        assert el.layer_satisfies("host-throwaway", real) is False
    assert el.layer_satisfies("L1.5-docker", "L1.5-local") is True
    assert el.layer_satisfies("L2-bench", "L2-bench") is True


def test_label_report_rejects_unknown_and_conflicting_layer():
    el = _el()
    with pytest.raises(ValueError):
        el.label_report({}, evidence_layer="L1.5", producer="run-l15.sh", topology="docker-2c", uart_kind="pty-fake")
    with pytest.raises(ValueError):
        el.label_report({"evidence_layer": "L2-bench"}, evidence_layer="L1.5-docker", producer="run-l15.sh",
                        topology="docker-2c", uart_kind="pty-fake")
    r = el.label_report({"x": 1}, evidence_layer="L1.5-docker", producer="run-l15.sh", topology="docker-2c",
                        uart_kind="pty-fake", image_digests={"serialwrap": "sha256:" + "a" * 64})
    assert r["evidence_layer"] == "L1.5-docker" and r["producer"] == "run-l15.sh"
    assert r["topology"] == "docker-2c" and r["uart_kind"] == "pty-fake"


def test_fail_closed_missing_layer_for_docker_and_throwaway_producers():
    el = _el()
    for producer in ("run-l15.sh", "run-systemd.sh", "run-longrun.sh", "run-supervision.sh", "win-task-throwaway.ps1"):
        with pytest.raises(ValueError, match="evidence_layer_missing"):
            el.validate_report_layer({"producer": producer})
    with pytest.raises(ValueError, match="evidence_layer_unknown"):
        el.validate_report_layer({"producer": "run-l15.sh", "evidence_layer": "L2"})
    assert el.validate_report_layer({"producer": "run-l15.sh", "evidence_layer": "L1.5-docker"}) == "L1.5-docker"


def test_assert_not_real_board():
    el = _el()
    for real in ("L2-bench", "L3-bench", "hw-bench"):
        for layer in ("L1.5-docker", "host-throwaway"):
            with pytest.raises(AssertionError):
                el.assert_not_real_board({"evidence_layer": layer}, real)
    with pytest.raises(AssertionError):
        el.assert_not_real_board({"evidence_layer": "L1.5-docker", "counts_as": ["L2-bench"]}, "L1.5-docker")
    el.assert_not_real_board({"evidence_layer": "L1.5-docker", "counts_as": ["L1.5-local"]}, "L1.5-docker")
```

`tests/test_docker_l15.py` 關鍵案例（表列者皆須實作，斷言逐字）：

| 測試 | 斷言 |
|---|---|
| `test_compose_two_app_containers` | `yaml.safe_load(compose.l15.yaml)["services"]` 的預設（無 profile）服務集合恰為 `{"serialwrap", "dut-sim"}`；`dut-sim` 的 `stdin_open is True` 且 `tty` 不存在或為 `False`（§15.7.2 `-i` 無 `-t`）；`dut-sim.network_mode == "none"` |
| `test_compose_no_real_serial_devices` | 所有服務的 `devices`、`volumes` 字串都不含 `/dev/ttyUSB`、`/dev/ttyACM`、`/dev/serial`；`privileged` 不存在或為 `False`（systemd profile 例外只允許 `cgroup: host` 與 `/sys/fs/cgroup` 掛載） |
| `test_run_l15_dry_run_uses_sandbox_by_id` | `bash scripts/docker/run-l15.sh pytest-physical-uart --dry-run` 的 stdout 含 `SERIALWRAP_BY_ID_DIR=/run/sw-fake/by-id` 與 `SW_UART_KIND=pty-fake`，且不含 `/dev/serial/by-id` |
| `test_run_l15_dry_run_labels_report` | 同上 stdout 含 `--evidence-layer L1.5-docker`（runner 收尾以 `label_report` 寫報告） |
| `test_run_systemd_dry_run_asserts_dropin` | `run-systemd.sh --mode systemd-user --dry-run` stdout 含 `systemctl --user show serialwrap -p Restart -p RefuseManualStart` 與期望字串 `Restart=no`、`RefuseManualStart=yes`，以及 `journalctl --user -u serialwrap -o cat` |
| `test_longrun_and_supervision_isolated` | `run-longrun.sh --hours 48 --dry-run` 與 `run-supervision.sh --hours 72 --mode systemd-user --dry-run` 的 `-p <project>` 不同、所有 `-v <volume>:` 名稱集合交集為空 |
| `test_longrun_refuses_before_p1` | 以 `SW_BC_PATH` 指向 tmp 的 `behavior_changes.yaml` 副本（BC-01 `status: APPROVED`）執行 `run-longrun.sh --hours 48 --dry-run` → exit 3，stderr 含 `P1 未合併：BC-01 非 IMPLEMENTED` |
| `test_evidence_index_rejects_docker_for_l2` | 合成 index：item `required_layer: L2-bench`、`status: present`、引用報告 `{"producer": "run-l15.sh", "evidence_layer": "L1.5-docker"}`（sha256 相符）→ `check_evidence_index.py --stage 0 --root <tmp>` exit 5，stdout 含 `evidence_layer_mismatch`；同法對 `L3-bench`、`hw-bench` 與 `host-throwaway` 報告逐一 exit 5 |
| `test_evidence_index_fail_closed_missing_layer` | 合成 index：item `required_layer: L1.5-docker`、引用報告 `{"producer": "run-longrun.sh"}`（無 `evidence_layer`）→ exit 5，stdout 含 `evidence_layer_missing` |
| `test_evidence_index_requires_required_layer` | 合成 index 缺 `required_layer` → schema 驗證失敗（exit 2） |
| `test_resource_report_uart_kind` | `SW_UART_KIND=pty-fake` 下 `resource_report_plugin` 產出的報告 `uart_kind == "pty-fake"`；未設時 `"native"` |
| `test_docker_smoke_two_container`（`docker` marker；`cap:docker` 缺時能力式 SKIP，`reason_code=cap_missing:docker`） | `run-l15.sh func-test-bridge --case sm-01-attach-to-ready` 真跑：報告 `evidence_layer == "L1.5-docker"`、`topology == "docker-2c"`、`len(image_digests) == 3`、案例 PASS |

- [ ] **T17-2：執行並確認 FAIL**：`python3 -m pytest -q tests/test_evidence_labels.py tests/test_docker_l15.py` → `ModuleNotFoundError: tests.acceptance.evidence_labels`（函式內）、compose／runner 檔不存在。
- [ ] **T17-3：commit 測試**：`git commit -m "test(test-infra): 新增 OD-15 docker 驗證環境與證據層級標記的失敗測試"`。

### I 步驟

- [ ] **I17-1：實作** 依 Files／Interfaces；`fake_uart.sh` 在容器 entrypoint 建立 PTY pair 並等待 `link` 出現（上限 5 s）；`run-l15.sh` 收尾以 `python3 -c 'from tests.acceptance.evidence_labels import label_report …'` 寫 `l15-docker-report.json`；`run-longrun.sh` 啟動前讀 `behavior_changes.yaml`（或 `SW_BC_PATH`）確認 BC-01～BC-05 皆 `IMPLEMENTED`；長跑與監管 runner 只輸出到 `--out` 指定目錄（預設 `b-log/rust-refactor/p2/docker-<run_id>`，相對 HOME）。
- [ ] **I17-2：執行並確認 PASS**：上列兩檔＋Task 12、13 測試全綠；本環境有 docker 時 `test_docker_smoke_two_container` 必須真跑 PASS（不得 SKIP 交差）；`python3 -m pytest -q tests/` 無新失敗。
- [ ] **I17-3：commit**：`git commit -m "feat(test-infra): 新增 docker 兩容器 L1.5 拓樸、systemd container 與長跑／監管 runner（OD-15）"`＋fragment。

---

## Task 18：Windows host 監管 >72h throwaway 排程與 Spike 1 收尾（OD-15）

**Test author model:** agy-gemini-3.8-flash-high　**Test reviewer:** haiku-xhigh　**Implementer model:** codex-gpt-6-luna-max（Windows 排程工作、行程與 COM 控制線）
**Changelog fragment:** `changelog.d/rust-p2-win-throwaway-spike1.md`（`type: feat`、`scope: test-infra`）：新增 Windows host 的隔離 throwaway 排程工作程序（windows-task >72 h 監管驗證）與 Spike 1 剩餘項（控制線隔離、Console-side pair、peer-close）實測工具。
**BLOCKED-OWNER evidence:** Console-side pair 需擁有者在 host 另建第二組 com0com pair（OD-01 選配）；未建立時該項報告 `NOT_RUN`（不得 PASS）。其餘項目可在擁有者 host 由 orchestrator 經 interop 執行（G7、G8）。

**範圍（OD-15 (1)(7)）：**
1. `windows-task` >72 h 監管驗證不能在 Linux docker 執行，改在擁有者 Windows host 以**隔離名稱與目錄的 throwaway 排程工作**執行：工作名 `serialwrap-throwaway-<run_id>`（`run_id` 符合 `^[0-9]{8}T[0-9]{6}Z-[0-9a-f]{6}$`）；`SERIALWRAP_STATE_DIR`／`_RUN_DIR`／`_CONFIG_DIR`／`_WAL_DIR`／`_LOG_DIR` 全指向 `%LOCALAPPDATA%\sw-throwaway\<run_id>\…`；daemon 為 OD-11 隔離實例＋`SERIALWRAP_TEST_DEVICE_ALLOWLIST=COM20`；RPC endpoint 用該 run 專屬 TCP port（不得為 48700）；不得建立、修改或停用正式 `serialwrap` 工作；`ExecutionTimeLimit=PT0S`、登入自啟、失敗自動重啟同 OD-03。報告 `evidence_layer: "host-throwaway"`（不計為正式工作站 `windows-task` 證據；P4 Task 18 Spike 2 消費本程序）。
2. Spike 1 剩餘三項（P1 T21 `realhw/procedures/stage0-spike1-win-vcom.md` 的未完成段落）：(a) 控制線隔離：COM20 設 RTS／DTR 時 COM21 依 com0com 預設接線只看到對應 CTS／DSR／DCD 變化，不外洩到其他 pair；(b) Console-side pair（OD-01 選配）：第二組 pair 開啟／獨占／關閉語意與 DUT-side 相同；(c) peer-close 語意：COM21 關閉時 COM20 的讀（回 0 byte／逾時）與寫（成功或錯誤碼）實際行為，寫回 Task 3 能力表（`transport_disconnect` 是否成立）。結果只記實測值，三項各 `PASS|FAIL|NOT_RUN`。

**Files:**
- Create: `scripts/stage0/win-task-throwaway.ps1`（`-RunId <id> -Hours <n> -Bundle <win path> [-Cleanup] [-WhatIf]`；`-WhatIf` 只輸出將註冊的 Task XML 與 env 到 stdout，不呼叫 `schtasks`）、`realhw/acceptance/spike1_remaining.py`（`python -m realhw.acceptance.spike1_remaining <control-lines|console-pair|peer-close> --pair COM20:COM21 [--console-pair COMx:COMy] --out DIR`；只在 Windows 原生 Python 執行，Linux 只跑純函式）、`realhw/acceptance/spike1_report.schema.json`、`realhw/procedures/stage0-win-task-throwaway.md`（執行與清除程序）
- Modify: `realhw/procedures/stage0-spike1-win-vcom.md`（P1 T21 產出；只追加「剩餘項執行」段落與 OD-15 基本資格結果引用，不改既有步驟）、`tests/acceptance/stage0_evidence_index.yaml`（新增 `S0-SPIKE1-REMAINING`、`S0-WIN-TASK-THROWAWAY-72H`（`required_layer: host-throwaway`），初始 `missing`）
- Create: `tests/test_win_task_throwaway.py`、`tests/test_spike1_remaining.py`（模組層 `core_portable`＋`memory_only`；原生實測函式另標 `windows_native`＋`windows_com`，`SW_TEST_COM_PAIR` 未設時能力式 SKIP）

**Interfaces — Produces：**

```python
# realhw/acceptance/spike1_remaining.py（只 import stdlib；Windows 原生路徑才 import serial）
ITEMS: tuple[str, ...] = ("control_lines", "console_pair", "peer_close")
VERDICTS: tuple[str, ...] = ("PASS", "FAIL", "NOT_RUN")
def build_report(results: dict[str, dict], *, host: dict, pair: str, console_pair: str | None) -> dict
    # host 必含 windows_build、hvci_running、secure_boot、com0com_version；ITEMS 缺項自動補 {"verdict":"NOT_RUN"}
def throwaway_task_name(run_id: str) -> str        # "serialwrap-throwaway-" + run_id；run_id 格式不符 → ValueError
def throwaway_env(run_id: str, localappdata: str) -> dict[str, str]
    # 五個 SERIALWRAP_*_DIR 皆在 <localappdata>\sw-throwaway\<run_id>\ 之下、SERIALWRAP_TEST_DEVICE_ALLOWLIST="COM20"、SERIALWRAP_SOCKET 為 tcp://127.0.0.1:<port≠48700>
```

### T 步驟

- [ ] **T18-1：撰寫失敗測試**。`tests/test_win_task_throwaway.py`、`tests/test_spike1_remaining.py`：

| 測試 | 斷言 |
|---|---|
| `test_throwaway_name_prefix_and_not_production` | `throwaway_task_name("20261016T120000Z-a1b2c3") == "serialwrap-throwaway-20261016T120000Z-a1b2c3"`；結果 `!= "serialwrap"`；`run_id="x"` → `ValueError` |
| `test_throwaway_env_all_under_isolated_root` | `throwaway_env(rid, r"C:\Users\u\AppData\Local")` 的五個 `SERIALWRAP_*_DIR` 值皆以 `C:\Users\u\AppData\Local\sw-throwaway\<rid>\` 開頭；`SERIALWRAP_TEST_DEVICE_ALLOWLIST == "COM20"`；`SERIALWRAP_SOCKET` 以 `tcp://127.0.0.1:` 開頭且 port ≠ 48700 |
| `test_task_xml_whatif`（`windows_native`；或在 Linux 以 `pwsh` 存在時執行，否則能力式 SKIP） | `win-task-throwaway.ps1 -RunId <rid> -Hours 72 -Bundle C:\b -WhatIf` 輸出的 Task XML：`<ExecutionTimeLimit>PT0S</ExecutionTimeLimit>`、`<URI>\serialwrap-throwaway-<rid></URI>`、所有 env 路徑在隔離根下、不含字串 `\serialwrap</URI>` |
| `test_cleanup_removes_only_throwaway`（`windows_native`） | 註冊後 `-Cleanup`：`schtasks /Query /TN serialwrap-throwaway-<rid>` 失敗（不存在）；`schtasks /Query /TN serialwrap` 的結果（存在與否）與執行前相同 |
| `test_spike1_report_schema_and_not_run_default` | `build_report({"control_lines": {"verdict": "PASS", …}}, host=<完整>, pair="COM20:COM21", console_pair=None)` 通過 `spike1_report.schema.json`；`console_pair.verdict == "NOT_RUN"`、`peer_close.verdict == "NOT_RUN"` |
| `test_spike1_report_requires_host_fields` | `host` 缺 `hvci_running` → `ValueError` |
| `test_spike1_control_lines_native`（`windows_native`＋`windows_com`） | 在擁有者 host 以 `SW_TEST_COM_PAIR=COM20:COM21` 實測：COM20 `rts=True` 後 COM21 `cts` 讀值與 com0com 預設接線一致，結果寫入報告（verdict 依實測，不預設 PASS） |
| `test_spike1_peer_close_native`（`windows_native`＋`windows_com`） | COM21 關閉後 COM20 `read(1)` 於 timeout 內回 `b""` 或擲例外（記錄實際型別與 Windows error code）；結果寫入報告與 Task 3 能力表候選值 |

- [ ] **T18-2：執行並確認 FAIL**：`python3 -m pytest -q tests/test_win_task_throwaway.py tests/test_spike1_remaining.py` → `ModuleNotFoundError: realhw.acceptance.spike1_remaining`（函式內）。
- [ ] **T18-3：commit 測試**：`git commit -m "test(test-infra): 新增 Windows throwaway 排程與 Spike 1 剩餘項的失敗測試"`。

### I 步驟

- [ ] **I18-1：實作** 依 Files／Interfaces；`stage0-win-task-throwaway.md` 寫：(1) `mkbundle.sh` 產生 Windows bundle（P3 交付前用 Python 基線入口）；(2) `-WhatIf` 審閱 XML；(3) 註冊並開跑 72 h（與 Task 17 Linux 監管 runner 並行）；(4) 期間以唯讀 RPC 每 5 分鐘輪詢 `health.status`；(5) 結束 `-Cleanup`；(6) 報告登錄 `S0-WIN-TASK-THROWAWAY-72H`。
- [ ] **I18-2：執行並確認 PASS**：Linux 上純函式測試全綠；orchestrator 在擁有者 host 經 `scripts/stage0/win-pytest.sh -m "windows_native" tests/test_win_task_throwaway.py tests/test_spike1_remaining.py` 原生執行（G7），報告附 Windows build／HVCI／pair；`python3 -m pytest -q tests/` 無新失敗。
- [ ] **I18-3：commit**：`git commit -m "feat(test-infra): 新增 Windows host throwaway 排程監管程序與 Spike 1 剩餘項實測工具（OD-15）"`＋fragment。

---

## 3. Phase 整體驗收（全部 Task 綠燈後）

- [ ] `python3 -m pytest -q tools/uart_stdio_bridge/tests` 全綠，連跑 3 次結果一致。
- [ ] `python3 -m pytest -q tests/` 全綠（無新失敗；`core_portable_platform_skips` 為空；P0 `tests/test_resource_markers.py` 對本 Phase 新增測試檔全數通過）。
- [ ] L1 出口證據（`S0-L1-BC`、`S0-L1-WIN-PYTEST`）只以 Task 13 `l1_judge` 對 `l1_required.lock.json` 凍結集合判定為 `PASS` 者計入；`check_evidence_index.py --stage 0` 對這兩項不得只因檔案存在與 sha256 相符而通過（R5-F3）。
- [ ] PR CI 的 `pytest-windows`（hosted `windows-latest`）綠燈，`resource-report-windows.json` 已上傳且 `not_collected_platform` 逐檔列出。
- [ ] `APPROVED_REVISION.json` 的 `tree_sha256` 等於目前 `tool_tree_sha256()`（`test_approved_revision_matches_tool_tree` 綠燈）。
- [ ] `python3 -m uart_stdio_bridge selftest --endpoint posix-pty --out ~/b-log/rust-refactor/p2/selftest-linux`：`verdict=="PASS"`（`PYTHONPATH=tools/uart_stdio_bridge/src`）。
- [ ] `python3 func-test/runner.py --mode both --selftest-report <上行報告>`：13 個 case 的 pty 與 bridge 兩種模式結果、`func-test-report.json` 的 `independent_cases==13`；非 PASS 者已分診並記錄（不得改 oracle）。
- [ ] `python3 func-test/runner.py --mode bridge --dut-mode docker --case sm-01-attach-to-ready --selftest-report <報告>` 一次，確認「DUT 在容器內、daemon 在主機原生 OS」拓樸在 `manifest.json.topology` 正確標示（§15.5 與既有 remote 拓樸分開）。
- [ ] `python3 func-test/runner.py --mode bridge --scenarios ...`：15 個 L1.5 情境結果已保存（`win-com-*` 3 個在 Linux 以 `cap_missing:windows_com` SKIP，Windows 結果屬 §4.1 G6）；`dependency_blocked` 項列出對應 BC。
- [ ] 對外分享的證據以 `uart-stdio-bridge redact` 產生遮罩衍生檔，`redaction-manifest.json` 同時記原始與遮罩 sha256（§15.6.4）。
- [ ] `python3 tests/acceptance/regen_lock.py --check` exit 0；`python3 tests/acceptance/check_evidence_index.py --stage 0` 的結果（預期 exit 5：B 項未取得）與缺項清單附於 PR 描述，不得宣稱 Stage 0 出口已滿足。
- [ ] `python3 -m policy_check --repo .` 通過。
- [ ] `tests/test_od09_freeze.py::test_stage0_chain_without_later_phases` 綠燈：基線 gate → `od09_freeze.freeze` → `register_candidate_start` → candidate gate 三延後項 PASS，過程未 import 任何 `tools.stage3`／`sw_core` 模組（證明正式 Stage 0 出口的 OD-09 凍結不依賴 P3～P9，R4-F2）。
- [ ] OD-15 docker 優先（Task 17）：`run-l15.sh func-test-bridge`、`run-l15.sh pytest-physical-uart` 與 `run-systemd.sh --mode systemd-user`／`systemd-system` 結果已保存，報告 `evidence_layer == "L1.5-docker"`；P1（含 P1-OC）合併後 `run-longrun.sh --hours 48` 與 `run-supervision.sh --hours 72` 已並行啟動並登錄 `S0-L15-DOCKER-48H-LINUX`、`S0-L15-DOCKER-SUPERVISION-72H`（docker 結果不得標為 B4／B5／B8 已取得）。
- [ ] OD-15 擁有者 host 可執行 gate（§4.1 G1～G8）：已執行者附 `evidence_layer: "native-windows"`／`"host-throwaway"` 報告；Spike 1 剩餘三項以實測 verdict 登錄 `S0-SPIKE1-REMAINING`（`NOT_RUN` 照實列出）；未執行者在 PR 描述列為「待執行」，不得標 PASS。
- [ ] 每個 Task 的 `changelog.d/rust-p2-*.md` fragment 已加入（18 個，frontmatter 的 `type`／`scope` 照各 Task 標頭）；PR 描述依 repo 回歸 case 政策（#155）記錄：本 Phase 為測試基礎，不修 bug，故無新增 `regression/` case；L2 案例調整為平台 adapter 與 `requires` 變更，oracle 未改。
- [ ] PR 描述列出本文件 §4 的 BLOCKED-OWNER 清單，狀態一律「未取得」，不得標 PASS。

## 4. BLOCKED-OWNER 證據總表（本環境無法產出；交付為程序、腳本、判定器與 schema）

OD-15（2026-10-16）後，原 B1、B2、B3（com0com 路徑部分）、B12 與 B13（`platform_info()` 實測部分）改為 §4.1 的擁有者 host 可執行 gate；本表保留其列以供追溯，狀態欄以 §4.1 為準。

| # | 項目 | 依據 | 相關 Task | 需要的資源／動作 |
|---|---|---|---|---|
| B1 | （OD-15 → G1、G4）Windows 原生 COM 端點：`test_cap_windows_com__*` 測試、`selftest --endpoint win-serial` PASS 報告 | OD-08、§13 Spike 1／4、§11.2 | 3、5、8 | 擁有者 host com0com pair `COM20:COM21`（Spike 1 基本資格已通過）；orchestrator 經 interop 執行 |
| B2 | （OD-15 → G8）com0com 關閉 COM-B 對 COM-A 的實際行為（`transport_disconnect` 能力是否成立） | §15.8.3 | 3、11、18 | Task 18 `spike1_remaining.py peer-close`；未實測前 `l15-transport-disconnect` 在 Windows 登記 SKIP |
| B3 | （OD-15 → G5、G6；實體傳輸替代模式仍 BLOCKED）Windows 上 13 個 func-test case（bridge 模式，依 Task 10「Windows 接線契約」）、3 個 `win-com-*` 情境、L1.5 情境的 Windows 執行結果；實體傳輸替代模式的逐案例承接紀錄 | §11.6 Stage 0 L1.5、§15.8.4、OD-08 C | 10、11、12 | `COM20:COM21`＋Windows Python daemon（OD-11 allowlist `COM20` 隔離實例）；實體傳輸替代模式只在 com0com 路徑失效時需要 |
| B13 | 自架 Windows COM runner 註冊（label `serialwrap-com`）與 `windows-com-selfhosted` job 的 CI 實跑（仍 BLOCKED）；Windows `platform_info()` 的 Secure Boot／HVCI／COM driver／端點防毒實測值（OD-15 → G7 一併記錄） | §11.5、§1.1.1、§15.8.3、OD-05、OD-08 | 2、13 | runner 註冊需擁有者；`platform_info()` 在擁有者 host 以 interop 取得，查詢權限不足時記「證據不足」 |
| B14 | 跨 OS bench 互斥：`cross_os_probe_argv` 在 WSL 與 Windows 兩側的實際設定與探測結果 | §15.2、OD-10 | 14 | 擁有者 bench 設定 |
| B4 | Linux live bench：pinned `serialwrap_exe` 下 regression 非破壞集合、realhw P0＋P1、reliability 報告 | §11.6 Stage 0 L2／L3 | 14 | 擁有者 bench（共用 bench lock、真板、無並行 pytest） |
| B5 | Windows bench（Spike 7）：Telnet frontend 下 F5／`p0-console-raw`／`p1-con-*`／`lr-mixed` 實跑、共用 bench lock 實際共鎖、Windows RSS | §13 Spike 7、§15.8.4 | 14 | 擁有者 Windows bench |
| B6 | 破壞性案例授權執行（F9、F10、`f11-flood-probe-classified`、`p1-rst-*`、`p1-ho-*`、`p1-hp-*`） | §11.5「破壞性測試必須顯式授權」 | 12、14 | 擁有者授權與目標板 |
| B7 | `f4-background-result-tail-consistent` Stage 0 bench 實跑結果；若紅燈，XFAIL 哨兵登記（issue、特徵、期限） | `TST-D-6`、§11.4 | 12 | 擁有者 bench 與核准 |
| B8 | Linux、Windows 各一次修正後 Python 48 h `lr-mixed` 基線（含 observer、WAL 複本、`release-gate.json` baseline 模式；Windows 另含 `excl/probes.ndjson`＋`excl/handoff.ndjson` 原生 COM 獨占證據，R1-F7） | OD-10、§11.6、§15.11.5 | 15 | 擁有者 bench，兩平台依序、不可並行，約 5 天 bench 佔用 |
| B9 | OD-09 延後凍結項數值（RSS／資源、最低有效負載與樣本、比較不確定性）於基線後、正式 Stage 0 出口、任何 Rust 候選長跑（最早 `S1B-L3-MIXED-48H-WINDOWS`）前以 Task 16 `od09_freeze freeze` 寫入新 revision | OD-09、§12.2 Stage 0 列、§14.2 | 15、16 | 擁有者裁決（執行 Task 16 程序 `od09-deferred-freeze.md`） |
| B10 | 長跑實例關閉監管自動重啟或等價故障保留措施的設定紀錄 | §15.11.2、Spike 8 | 15 | 擁有者 bench 設定 |
| B11 | `p2-1-mcu-flash-manual` 程序的實機執行與證據 | §11.6 Slice 2.4 | 12 | 擁有者實機（DUT console＋FTDI MCU 線） |
| B12 | （OD-15 → G7）>72 h `windows-task` 監管驗證（Spike 2）：本計畫 Task 18 交付 throwaway 排程程序（`evidence_layer: "host-throwaway"`）；測試團隊工作站上的正式工作驗證仍由 P4 Task 18 承接 | §13 Spike 2、OD-03、OD-15 | 12、18 | 擁有者 Windows host（隔離名稱與目錄的 throwaway 工作）；測試團隊工作站部分仍 BLOCKED |
| B15 | OD-15：docker 結果不得取代的真板 L2／實機 L3／OD-10 bench 長跑（B4、B5、B8 照舊）；docker 48 h 與 >72 h 監管只作 L1.5 前置 | §11.1、§15.3、OD-10、OD-15 | 14、15、17 | 擁有者 bench |

### 4.1 OD-15 擁有者 host 可執行 gate（不再列 BLOCKED-OWNER）

前提：擁有者 host（Windows 10.0.26200、HVCI 執行中、Secure Boot 關閉）已安裝 com0com 3.0.0.0（簽章 zip md5 `a07aa80024cca837dacc75d3573285ff`）並建立 pair `CNCA0=COM20`／`CNCB0=COM21`；受測 daemon 一律 OD-11 隔離實例＋`SERIALWRAP_TEST_DEVICE_ALLOWLIST=COM20`；orchestrator 經 WSL interop 執行；報告 `evidence_layer: "native-windows"`（G7 throwaway 為 `"host-throwaway"`）。擁有者 host 不可用時該 gate 記「證據不足」，不得標 PASS。

| Gate | 內容 | Task | 指令 |
|---|---|---|---|
| G1 | `test_cap_windows_com__*`（`WinSerialEndpoint` 真實 COM） | 3 | `SW_TEST_COM_PAIR=COM20:COM21 scripts/stage0/win-pytest.sh -m windows_com tools/uart_stdio_bridge/tests` |
| G2 | `run --endpoint win-serial` 端到端 | 5 | bridge 開 COM21、受測端開 COM20 |
| G3 | `local-process` 模式端到端 | 6 | 同 G2，`--dut-mode local-process` |
| G4 | `selftest --endpoint win-serial --com-pair COM20:COM21` `verdict=="PASS"` | 8 | 登錄 `S0-BRIDGE-SELFTEST-WIN` |
| G5 | 13 個 func-test case bridge 模式（Windows Python daemon） | 10 | `python func-test/runner.py --mode bridge --endpoint win-serial --com COM21` |
| G6 | L1.5 情境與 3 個 `win-com-*` 情境 | 11 | `--scenarios`；`l15-transport-disconnect` 依 G8 結果決定 SKIP 與否 |
| G7 | `windows-task` >72 h throwaway 監管；`platform_info()` 實測值 | 13、18 | `scripts/stage0/win-task-throwaway.ps1 -RunId <id> -Hours 72 -Bundle <dir>` |
| G8 | Spike 1 剩餘三項：控制線隔離、Console-side pair（需第二組 pair，否則 `NOT_RUN`）、peer-close 語意 | 18 | `python -m realhw.acceptance.spike1_remaining <item> --pair COM20:COM21` |

## 5. 規格覆蓋自檢（§15.12 最小交付物 → Task）

| §15.12 交付物／規格要求 | Task |
|---|---|
| 1. 可獨立執行並通過自測的 bridge、stdin/stdout DUT adapter、固定版本 image 與控制介面 | 1、3、4、5、6、8 |
| 2. `func-test` 既有案例的模式對應清單，及必要安全／故障情境 | 10、11、12 |
| 3. 外部證據 schema、WAL 關聯規則、比較器與完整性檢查 | 2、7 |
| 4. L2／L3 逐 case 契約與能力清單、Windows adapter、核准的 F12 oracle 修訂 | 12、14（F12 oracle 本身由 P1 交付，本計畫登記於 `cases.yaml`） |
| 5. 各平台基線、A/B 報告、長跑 gate 與 Stage 0～3 出口證據索引 | 15（gate 與程序）；12（Stage 0 證據索引 schema、checker；Stage 1～3 索引由 P3／P4／P9 以同 schema 新增）；基線證據為 B8；A/B 執行器屬 P9 |
| §15.4.1 socat spike → Python 正式 bridge | 9（執行順序在 2～5 之前，結果為 bridge 實作前置 gate） |
| §15.6.4 原始證據存取限制、遮罩衍生檔與雙 hash | 2（0600／0700、遮罩格式）、7（`redact`） |
| §11.3 六項不變量的可執行斷言與交接安全情境 | 11（`INVARIANT_ENFORCERS`、`assert_wire_absent`、`assert_peer_closed`、4 個新情境）；不可外部製造部分連到 P1 T07／T08／T20 |
| §11.5 每 PR 雙平台 L1、排程整合與 Windows 原生 serial gate | 1（bridge Windows job）、13（`pytest-windows`、`windows-com-selfhosted`）、12（證據索引缺項即 BLOCKED） |
| §15.11.3 worker 存活、有效運行時長、觀測缺口 | 14（worker 事件、`wall_ts`）、15（`worker_liveness`、`effective_hour`、`observation_gaps`） |
| §15.4.3 binary、短讀／部分寫入、方向 worker、stderr 排空、有界緩衝、fail-stop、自測清單 | 2、5、8 |
| §15.6.2 `bridge.v1` 欄位、四個 observation、`stream_offset`、`endpoint_generation`、`dut_generation`、CRC golden | 2 |
| §15.6.3 只比 byte stream、核准映射、correlation | 7、10 |
| §15.7.2 Docker `-i` 無 `-t`、無網路、固定 image、`local-process` | 6 |
| §11.2 平台分類與資源標記 | P0 T14（標註與稽核）；13（資源報告、`core_portable` 平台 SKIP 閘門、Windows collection） |
| §11.3 承接清單欄位、凍結、不以 SKIP 取得全綠 | 12、13 |
| §14.2／§15.11.4 唯一門檻檔、`engine_result` 保留、`release_gate_result` | 15 |
| §12.2 Stage 0 列「正式 Stage 0 出口須完成延後項目的凍結與證據充分性核對」、§14.2／OD-09「任何 Rust 候選長跑前凍結」 | 16（凍結工具、候選開跑登記；只依賴 12、15） |
| OD-15 (3)(4)(5)(6)(7)：docker 假 COM、systemd container、兩容器 L1.5、48 h docker 長跑、>72 h Linux 監管、證據層級標記 | 17（標記規則亦由 12 的證據索引執行） |
| OD-15 (1)(7)：Spike 1 剩餘項、`windows-task` >72 h throwaway 排程（Windows host） | 18 |
| OD-15 原生 Windows COM 證據改為擁有者 host 可執行 gate | §4.1 G1～G8（Task 3、5、6、8、10、11、13、18） |
| 調和表 `TST-D-4`、`TST-D-6`、`TST-D-8`、`TST-D-9`、`TST-D-10`、`TST-D-12`、`TST-D-13`、`TST-D-14`、`GAP-G-12` | 13、12、10、10、14、14、14、14、14 |
