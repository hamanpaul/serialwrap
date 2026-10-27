# serialwrap Rust 重構總體實作計畫（Master Plan）

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 依 `docs/rust-refactor/serialwrap-rust-spec-final.md`（定稿）與 `docs/rust-refactor/owner-decisions-2026-10-15.md`（OD-01～OD-15）1:1 完成 serialwrap 的 Stage 0～3 重構交付：Python 基線修正（BC-01～05）、契約凍結、外部 bridge 證據工具、Rust CLI（Stage 1A／1B）、Rust daemon 四切片（Slice 2.1～2.4）與 Stage 3 退役前置工具。

**Architecture:** Cargo workspace（`sw-proto`／`sw-pal`／`sw-core`／`sw-daemon`／`sw-cli`），平台相依只在 `sw-pal`；契約先行（`contracts/*.yaml|json` 為權威，Python 與 Rust 都對契約測）；黑箱測試只經部署 artifact（CLI subprocess＋JSON），同一批黑箱案例可指向 Python 或 Rust artifact 做 Sequential A/B。

**Tech Stack:** Rust 1.96（edition 2021，targets `x86_64-unknown-linux-gnu`、`x86_64-pc-windows-gnu`）、Python 3.10+（既有 daemon／測試工具）、pytest、PyYAML、jsonschema（測試工具側）、socat（Spike 4 接線檢查）、Docker（DUT 模擬器）。

**Spec:** `docs/rust-refactor/serialwrap-rust-spec-final.md`、`docs/rust-refactor/owner-decisions-2026-10-15.md`。各分計畫（`docs/rust-refactor/plans/NN-*.md`）引用章節號。

## Global Constraints

- 平台相依 production code 只在 `sw-pal`；其他 crate 禁止 `#[cfg(unix)]`／`#[cfg(windows)]`／`target_os`／`cfg!`／`cfg_attr` 平台分支與 build script 間接分支（§3.1）；允許 `#[cfg(test)]`。
- RPC／CLI `error_code` 與 session `last_error` 為兩個獨立命名空間（ADR-07、§9.3）；`CREDENTIALS_UNRESOLVED` 只屬後者。
- 錯誤碼計數由 `contracts/error_codes.yaml` 資料產生，不得硬編碼 78（§9.2、OD-06）。
- `--retries` 只對 11 個唯讀方法重試：`health.ping`、`health.status`、`session.list`、`session.get_state`、`session.activity`、`session.console_list`、`session.log_status`、`device.list`、`alias.list`、`mcu.patterns`、`mcu.status`（§4.2）；`session.interactive_status` 絕不重試。
- CLI JSON：`json.dumps(..., ensure_ascii=False, separators=(",", ":"))` 等價輸出；`state.json`／WAL 以 `sort_keys=True`。
- 背壓上限：`CMD_PENDING_MAX=256`、`CMD_HISTORY_MAX=512`、`BG_CAPTURE_MAX_COUNT=64`、`DEFERRED_INPUT_MAX_BYTES=262144`、EventEngine LineBuffer `16384`、每 Console 出向 Ring `1 MiB`（§5.4）；Actor mailbox／RX／TX／WAL 待處理一律有界。
- Drain timeout 3,000 ms → `RECOVERING`＋`drain_stalled:true`＋`drain_timeout_epoch`（§5.2.5）；`device.release` 逾時回 `DRAIN_STALLED`（OD-14 #4），drain 中的 attach／clear／bind／recover 維持 `SESSION_RECOVERING`。
- OD-14：`session.attach`／`session.recover` 的 `error_code` 不放 `last_error` 值，改回 `SESSION_NOT_READY`＋`last_error`／`last_error_detail` 欄位（SC-34）；`platform=passthrough` 連續 30 s 無 RX 自動 `READY` 並可送命令（SC-35）；UDS 維持 `0660`（SC-08 撤回）。外部指令格式（CLI 子命令、參數、RPC method）只能新增不能修改。
- Ready Reconfirm 耗盡：300 秒或 5 次失敗（§5.3）。
- WAL rotate 門檻 `R` 以 Stage 0 擷取的 Python 常數為準，不自行換算 64 MB（§1.1.3）。
- Windows 序列埠：`CreateFileW(OPEN_EXISTING, dwShareMode=0)`；WAL handle：`FILE_SHARE_READ|FILE_SHARE_WRITE|FILE_SHARE_DELETE`（§6.2、§8.2）。
- Windows RPC 預設 `tcp://127.0.0.1:48700`；信任範圍＝整機本機使用者（OD-12），規格與文件不得宣稱「同使用者隔離」。
- Windows 監管預設 `windows-task`，`ExecutionTimeLimit=PT0S`（OD-03、§6.1）。
- bridge 位置 `tools/uart_stdio_bridge/`，不得 import `sw_core`／連結 `sw-pal`（OD-07、§15.4.2）。
- 長跑門檻唯一來源 `realhw/acceptance/longrun_acceptance.yaml`；OD-09 三項已凍結，其餘延後凍結（§14.2、§15.11.4）。
- repo 政策：不得 commit 到 `main`；每個 code PR 附 `changelog.d/<issue>-<slug>.md`；commit 用 Conventional Commits 繁中 subject，含 `Co-authored-by: Copilot <223556219+Copilot@users.noreply.github.com>` trailer；完成前跑 `python3 -m pytest -q tests/` 與 `python3 -m policy_check --repo .`。
- 雙軌同步上限（§12.2「契約先行的雙軌開發」）：Stage 2 期間 Python／Rust 雙軌同步（含測試工具同步、實機重跑、缺陷處理）超過當雙週重構投入的 30% 時，暫停開立下一切片並重新排程；各切片校準報告（06 Task 23 起，07／08／09 出口報告沿用同格式）須列出當期雙軌投入比例。
- 斷路器（§12.2、OD-02／OD-13）每個切片都適用：2.1～2.4 各自在切片開始時以 `scripts/rust/slice_breaker.py`（06 Task 0b 唯一建立者）凍結必要案例分母與觀測窗口（`tests/acceptance/breaker/slice_2.{1,2,3,4}.json`），出口 gate 判定；不得拆案例或改分母。
- Spike 9（多使用者 IPC 信任邊界，§13）不在本計畫交付範圍：OD-12 限定單人工作站部署，多使用者部署（含 `windows-service` bench）前須另案交付 RPC token＋ACL 與 Spike 9 證據；P9 退役裁決不得宣稱多使用者隔離。

## Review Focus

1. **舊 epoch／過期事件與晚到 completion**：Release／Recovery 後舊 worker 的 completion 仍到達 → 不得改新 session 狀態，但其 I/O 證據須落 WAL（§5.2.4）。由 Slice 2.1 Task「drain barrier」測試覆蓋。
2. **部分 TX／結果不明**：OS 只寫出部分 bytes 或結果不明時被中止 → `COMMAND_ABORTED_IN_FLIGHT`（≥1 byte）或凍結映射，且絕不自動重送（BC-03、§5.1.4）。由 Stage 0 BC-03 與 Slice 2.1 測試覆蓋。
3. **WAL 外部 unlink／外部 reader 持有 handle**：重建 META 帶 `loss_flag:true` 與 `potentially_lost_seq_range`（未知時明示 unknown），重建失敗不得宣稱 WAL 健康，並停止自動 TX（§8.2）。由 Stage 0 BC-01 與 Slice 2.1 WAL 測試覆蓋。
4. **DeferredBuffer 跨上下文重灌**：清空後 mailbox 中尚未處理的舊按鍵不得再補回 UART（BC-02）。由 Stage 0 BC-02 與 Slice 2.2 測試覆蓋。
5. **CLI 委派遞迴與參數保真**：Stage 1A Rust CLI 委派到 Python 入口時，不得經 `PATH` 找到自己造成 Rust→Python→Rust 遞迴；stdin／exit code／signal 須原樣保留（§4.3）。由 Stage 1A delegation 測試覆蓋。

---

## 1. 工作包（Phase）與分計畫

| Phase | 對應規格階段 | 分計畫 | 交付 |
|---|---|---|---|
| P0 | Stage 0 契約治理 | `01-stage0-contracts.md` | `contracts/*`、契約驗證器、SKILL 受管區段、OD-06 通用規則設定 |
| P1 | Stage 0 Python BC | `02-stage0-python-bc.md` | BC-01～BC-05、OD-11 allowlist、F12 oracle 修訂、Stage 0 擷取凍結值 |
| P1-OC | Stage 0 擁有者裁決套用（OD-14） | `02b-stage0-owner-confirmations.md` | SC-34、SC-35、`DRAIN_STALLED`、SC-08 撤回（0660）、擁有者確認簿記；P1 完成後執行 |
| P2 | Stage 0 測試基礎 | `03-stage0-test-infra.md` | `tools/uart_stdio_bridge/`、DUT 模擬器、比較器、func-test 外部模式、`tests/acceptance/`、資源標記、`longrun_acceptance.yaml`＋gate 判定器、OD-09 延後項凍結工具與候選長跑開跑登記（T16，R4-F2）、release manifest |
| P3 | Stage 1A | `04-stage1a-rust-cli.md` | Cargo workspace、`sw-proto`、`sw-pal`（client 側）、`sw-cli` 資料面＋固定路徑委派 |
| P4 | Stage 1B | `05-stage1b-rust-cli-mgmt.md` | daemon／service／supervision-mode／remote／connect／setup／doctor／console-attach、`windows-task`、版本配對、更新／回滾 |
| P5 | Slice 2.1 | `06-slice21-daemon-core.md` | PAL serial／lock／device／wal_fs／rpc server、WAL、Arbiter、SessionActor、最小 Shell 登入、Quiet Gate、Drain／Release、獨占、有界資源 |
| P6 | Slice 2.2 | `07-slice22-console.md` | PTY／Telnet Console、Raw／Line、Lease、Soft Preempt、Suspend／Resume、DeferredBuffer |
| P7 | Slice 2.3 | `08-slice23-login-events.md` | 完整 Login profile、Quiet／Reconfirm、Event Engine、Stall／Recovery |
| P8 | Slice 2.4 | `09-slice24-transfer-flash.md` | FileTransfer、完整裝置交接、MCU flash／holder-probe、監管整合 |
| P9 | Stage 3 前置 | `10-stage3-canary.md` | A/B 執行器、Canary 帳本工具、退役切換與回滾演練程序 |

每個 Phase 依序：**計畫（本目錄）→ 測試先行（TDD，紅燈確認）→ 實作 → 雙審（sonnet max＋codex gpt-6.1-sol medium）→ 修正 → 全套測試＋policy_check → commit**。

## 2. 模型分工（使用者指定）

| 角色 | 模型 | 呼叫方式 | 用途 |
|---|---|---|---|
| 實作 A | Claude Haiku（effort xhigh） | Workflow `agent(..., {model:'haiku', effort:'xhigh'})` | 範圍明確、介面已定的模組（契約檔、CLI 子命令、schema、測試案例展開） |
| 實作 B | Codex `gpt-6-luna`（reasoning max） | `codex exec -m gpt-6-luna -c model_reasoning_effort="max" -s workspace-write -C <dir>` | 高風險並行／平台／狀態機（PAL、WAL、Arbiter、SessionActor、Drain barrier、Telnet parser） |
| 實作 C | agy `gemini-3.8-flash-high` | `agy --model gemini-3.8-flash-high --dangerously-skip-permissions -p '<prompt>'`（cwd＝工作目錄） | 可平行獨立模組（Event Engine、FileTransfer、bridge 工具、DUT 模擬器） |
| 審查 A | Claude Sonnet（effort max） | Workflow `agent(..., {model:'sonnet', effort:'max'})` | 規格符合度、正確性、安全不變量 |
| 審查 B | Codex `gpt-6.1-sol`（reasoning medium） | `codex exec -m gpt-6.1-sol -c model_reasoning_effort="medium" -s read-only` | 獨立第二意見 |

原則：同一 Task 的「測試作者」與「實作者」盡量不同模型（降低共同盲點）；審查者永不為該 Task 的實作者。daemon 生命週期（啟動序、RAII／退出清理、監管停止、更新／回滾與跨 Stage 停止交接）與 Windows 行程管理（行程樹終止、WSLENV、staging、exit code）一律由實作 B（Codex `gpt-6-luna`）實作，不派實作 A／C（R0-F6）；這類 Task 的 Windows 執行步驟（寫 `/mnt/c`、呼叫 `cmd.exe`／`powershell.exe`）可能被 Codex `-s workspace-write` 沙箱擋下，一律由 orchestrator 代跑並把報告路徑交給實作者。

## 3. 本環境可產出與不可產出的證據

可在本環境（WSL2＋同機 Windows 11 host interop）取得：
- Linux L1（pytest、cargo test）、Linux L1.5（PTY pair＋bridge＋DUT 模擬器，含 Docker 模式）。
- Windows 原生 L1：交叉編譯 `x86_64-pc-windows-gnu`，經 WSL interop 在 Windows host 原生執行 Rust 測試（檔案分享旗標、`LockFileEx`、TCP RPC、Task Scheduler XML 產生）；Windows Python（3.10～3.12）可跑 Python 側 Windows 測試。

OD-15（2026-10-16）後新增可取得的證據：
- **原生 Windows COM handle 證據**：Spike 1 基本資格已在擁有者 host 通過（證據在擁有者 host 的 `b-log` 下 `rust-refactor/spike1/`：com0com 3.0.0.0 signed zip md5 `a07aa80024cca837dacc75d3573285ff` 與 SourceForge 一致、Authenticode Valid、Windows 10.0.26200、HVCI 執行中、Secure Boot 關閉；pair `CNCA0=COM20`（serialwrap 端）／`CNCB0=COM21`（bridge 端）；256-byte 全值域雙向正常、COM20 第二次開啟 `ERROR_ACCESS_DENIED`、關閉後可重開）。各 Phase 原列 BLOCKED-OWNER 的 OD-08 原生 COM gate 改為可執行步驟：`SW_TEST_COM_PAIR=COM20:COM21`、daemon 以 OD-11 隔離實例＋`SERIALWRAP_TEST_DEVICE_ALLOWLIST=COM20` 啟動，由 orchestrator 經 WSL interop 代跑。Spike 1 剩餘項（控制線隔離、Console-side pair、peer-close 語意）仍為待測，不得預標 PASS。
- **docker 優先驗證**：新版 serialwrap 一律先在 docker 驗證（P2-T17）：`physical_uart` 類測試以假 COM（socat PTY pair）在容器內跑、systemd 行為（journald stderr、autostart hold drop-in）在 systemd container 驗、L2／L3 情境先以兩容器拓樸（serialwrap ⇄ bridge ⇄ DUT 模擬器）跑；48h 長跑於 P1 與修正後 Python 基線就緒後在 docker 執行，Linux 監管 >72h 在 docker 並行。docker 證據一律標 `evidence_layer: "L1.5-docker"`，**不計為**真板 L2、實機 L3 或 OD-10 bench 長跑。
- **`windows-task` >72h**：Linux docker 無法承載 Task Scheduler，改在擁有者 Windows host 以隔離名稱／目錄的 throwaway 排程工作執行（P2-T18，`evidence_layer: "host-throwaway"`）。
- **production daemon 唯讀比對**：授權對正式 daemon 執行唯讀 RPC（`--retries` 白名單的 11 個唯讀方法，如 `daemon status`、`session list`）比對 §9.4 欄位；不得送有副作用方法。

本環境**無法**產出、須由擁有者排程或授權的項目（各 Phase 交付時以「BLOCKED-OWNER」明列，不得標 PASS）：
- 擁有者 host 以外的原生 Windows COM 證據（例如測試團隊工作站、開啟 Secure Boot 的機台）；需管理員的 com0com 選用元件實機安裝／移除（P4-T19，擁有者 host 可執行）。
- L2 實機回歸（TestPilot `serialwrap_regression` on live bench）、L3 `realhw` P0／P1／remote、OD-10 bench 依序 48 小時長跑（docker 48h 不取代）。
  - **OD-27（2026-10-27）**：本 host 沒有可供測試的真板。真板 L2 回歸 f1～f11 與 pt-quiet-ready（41 案）、realhw p0／p1（28 案，含 p1-rst-daemon）與 rm-live-e2e，以及 OD-30 (2) 的 Windows transport disconnect（bridge selftest ST-12）與 Serial Console 實體控制線隔離，移到「可 release、可在其他電腦安裝」之後執行。**步驟**：(1) 以 release 產物在接有真板與真實 USB 序列硬體的其他電腦完成安裝（`install.sh`／Windows installer，非開發 worktree）；(2) 由該安裝機發起真板全套（TestPilot `serialwrap_regression` 的 f1～f11、pt-quiet-ready，`realhw` p0／p1、rm-live-e2e，bridge selftest 實際拔線的 ST-12）；(3) 證據以該安裝機的 release 版本與 tree 標示，FAIL 照實回報。Stage 1 與 Stage 2 出口不以上述案例為條件，維持 BLOCKED-OWNER 並引用 OD-27，不得記為 PASS、不得刪除案例。
- Canary（每平台 14 活躍日、≥5,000 次 `cmd submit`、≥50 次干擾／交接；OD-15 (8) 由執行者決定延到 Stage 3（P9）執行，P9 帳本工具保留，早期 Phase 不以 canary 為出口條件）、實機回滾演練、Stage 3 退役判定。
- OD-09 延後凍結項（RSS 預算、最低負載、容許誤差）——需 Python 48h 基線後、於正式 Stage 0 出口以 P2-T16 凍結工具凍結（早於 Stage 1B 混合 48 h 長跑；工具不依賴後續 Phase，R4-F2）。

以上項目本計畫交付「可執行的程序、腳本、gate 判定器與證據 schema」，證據本身待擁有者執行。

---

## Cross-plan index

> 跨計畫一致性審查（2026-10-15）產生。ID 規則：`P<phase>-T<分計畫 Task 編號>`（P1 的 `T01`～`T09` 記為 `P1-T1`～`P1-T9`；`0b`、`12b`、`15a`、`21a`、`5B`、`7B`、`14B`、`16-wire`、`18A` 照分計畫原名）。模型代號：**H**＝`haiku-xhigh`（實作 A）、**L**＝`codex-gpt-6-luna-max`（實作 B）、**G**＝`agy-gemini-3.8-flash-high`（實作 C）；審查一律 Sonnet max＋Codex `gpt-6.1-sol` medium，且審查者不得為該 Task 實作者。「依賴」欄只列同 Phase 的執行序前置（取自各分計畫的執行順序／相依表）；跨 Phase 前置列在各 Phase 標題，具體符號以各分計畫 Consumes 段為準。分計畫與本索引不一致時以分計畫為準，並回頭修正本索引。

### P0（`01-stage0-contracts.md`；Phase 前置：—）

| ID | Task | 測試作者 | 實作者 | 依賴（同 Phase Task） |
|---|---|---|---|---|
| P0-T1 | 契約目錄 loader 與結構規則 | G | H | — |
| P0-T2 | AST 碼抽取器（常數傳遞、參數／回傳傳遞、動態前綴） | H | L | P0-T1 |
| P0-T3 | `contracts/error_codes.yaml`（RPC／CLI error_code 權威目錄） | G | H | P0-T2 |
| P0-T4 | `contracts/session_last_errors.yaml` 與 `contracts/diagnostic_aux.yaml` | G | H | P0-T3 |
| P0-T5 | `contracts/behavior_changes.yaml`（BC／SC／DF 登記） | G | H | P0-T4 |
| P0-T6 | 契約驗證器 CLI（目錄↔抽取↔契約測試↔交叉參照↔文件） | H | G | P0-T5 |
| P0-T7 | 補齊缺少契約測試的錯誤碼（Python 基線特徵化測試） | H | G | P0-T6 |
| P0-T8 | `contracts/rpc_schema.json` | G | H | P0-T7 |
| P0-T9 | `contracts/cli_schema.json` | G | H | P0-T8 |
| P0-T10 | `contracts/wal_record.schema.json`、`contracts/wal_meta_events.schema.json` 與 `contracts/state_file.schema.json` | H | G | P0-T9 |
| P0-T11 | SKILL.md 受管區段生成 | G | H | P0-T16 |
| P0-T12 | `.project-policy.yml` 宣告 OD-06 規則、R-26 同步與 preflight | H | G | P0-T11 |
| P0-T13 | paulsha-conventions 通用規則 R-27（本地分支，不 push） | H | G | P0-T12 |
| P0-T14 | pytest 平台／資源 marker 註冊與逐檔標註 | L | H | P0-T1、P0-T2、P0-T3、P0-T4、P0-T5、P0-T6、P0-T7、P0-T8、P0-T9、P0-T10、P0-T11、P0-T12、P0-T13、P0-T16 |
| P0-T15 | P0 文件勘誤（調和表指派 P0 的 DOC 列）與 P0 列覆蓋閘門 | G | H | P0-T14 |
| P0-T16 | P0 KEEP 列基線凍結（daemon logging、診斷文字 golden、config quirks） | H | G | P0-T10 |

### P1（`02-stage0-python-bc.md`；Phase 前置：P0）

| ID | Task | 測試作者 | 實作者 | 依賴（同 Phase Task） |
|---|---|---|---|---|
| P1-T1 | Stage 0 測試設施：替身、平台 skip、TX 結果純型別、Windows 原生 pytest runner | H | L | — |
| P1-T2 | BC-01a WAL 常駐 handle、每筆 flush、唯一 close、DF-17（部分寫入依 `sent` 只補寫剩餘、殘行以 `\n` 終結，R2-F3） | H | L | P1-T1 |
| P1-T3 | BC-01b Windows `CreateFileW` share-delete handle 與 `win32_files` | G | L | P1-T2 |
| P1-T4 | BC-01c rotate／reset 於常駐 handle 下同步與 DF-03 歸檔後綴 | G | L | P1-T3 |
| P1-T5 | BC-01d 外部 unlink／替換／DeletePending 偵測、有界自癒、缺口 META、停止自動 TX | H | L | P1-T4 |
| P1-T6 | BC-03／SC-07 TX 三態、TX WAL 只記確認 bytes、可取消寫入、DF-15 精確 wal_seq | G | L | P1-T5 |
| P1-T7 | SC-04a Bridge 兩段式 Drain 與 `DrainReceipt`（drain 期限自 `begin_drain` 起算，R3-F3） | H | L | P1-T6 |
| P1-T8 | SC-04b Session Drain、release／attach 交接回應、drain 診斷欄位、BC-03 中止映射、SC-11 有序關機與 `daemon.handoff_stop` 原子交接（R1-F1） | G | L | P1-T7 |
| P1-T9 | BC-02 DeferredBuffer 整段清空與上下文標記 | H | L | P1-T8 |
| P1-T10 | BC-04 Per-UART 獨占、holder 診斷、Windows ACCESS_DENIED 映射、SC-05 同步收回、DF-04 | G | L | P1-T9 |
| P1-T11 | Gate 錯誤碼：SC-01、SC-02、SC-03、SC-06、DF-02 | G | L | P1-T10 |
| P1-T12 | SC-13 lease tick＋tombstone、SC-14 flash probe 資格、SC-16 console 行上限 | H | L | P1-T11 |
| P1-T20 | OS write 前授權 permit（quiet／release／cancel／lease／owner_gen 零 TX；自動登入／密碼／ready probe 帶 `system_probe` permit＋`login_gen`（R2-F1）；Quiet 不擋有效 interactive lease 救援（R2-F2）；Windows 持鎖寫入下 drain 取 fence 鎖有上限、3,000 ms 期限自請求起算（R3-F3）；self-test nonce probe 帶 `self_test_probe` permit、提交點重驗 boot quiet 與 bridge 世代，被擋回 `AUTOBOOT_QUIET`（R5-F2，DF-19）） | H | L | P1-T12 |
| P1-T13 | OD-11／SC-19 隔離實例測試裝置 allowlist | G | H | P1-T20 |
| P1-T14 | BC-05 Windows `remote -R/-L`、SC-27 `connect`／`benches`、SC-21 `remote stop`、跨行程檔案鎖 | H | L | P1-T13 |
| P1-T15 | 平台管理：SC-08、SC-09、SC-10、SC-12、SC-20、DF-14（SC-08 已由 OD-14 撤回，P1-OC-T3 還原為 0660） | G | L | P1-T18 |
| P1-T16 | 缺陷修正群組：DF-01、DF-06、DF-07、DF-08、DF-09、DF-10、DF-11、DF-12、DF-13、DF-16 | H | G（DF-01／06／07／08／09）＋L（DF-10～13／16） | P1-T15 |
| P1-T17 | F12 `f12-wal-path-live` 與 L3 `p0-wal-live` 改為 seq／marker／CRC oracle | H | G | P1-T16 |
| P1-T18 | Python 側 release manifest、`tree_sha256`、`version_pairing`、DF-05 daemon 定位 | G | H | P1-T14 |
| P1-T19 | Stage 0 凍結值擷取與 S1-W WAL 微基準 | H | G | P1-T17 |
| P1-T21 | Stage 0 實機回歸 case、程序 runbook 與 Spike 3 腳本（撰寫；實跑 BLOCKED-OWNER） | G | H | P1-T19 |

### P2（`03-stage0-test-infra.md`；Phase 前置：P0、P1（T17 F12 oracle））

| ID | Task | 測試作者 | 實作者 | 依賴（同 Phase Task） |
|---|---|---|---|---|
| P2-T1 | 套件骨架、獨立依賴、產品邊界守衛與 CI | H | G | — |
| P2-T2 | 證據格式、CRC golden vectors、EvidenceWriter | G | H（records／clock／manifest／schema）＋L（writer.py） | P2-T9 |
| P2-T3 | 端點 adapter（POSIX PTY／Windows pySerial） | H | L | P2-T2 |
| P2-T4 | DUT 行為模型抽離與 stdin/stdout DUT 模擬器 | H | G | P2-T3 |
| P2-T5 | bridge 核心（方向 worker、背壓、stderr 排空、控制通道、故障動作） | G | L | P2-T4 |
| P2-T6 | DUT 執行模式：Docker image（`-i` 無 `-t`）與 `local-process` | H | G | P2-T5 |
| P2-T7 | WAL↔wire 比較器、correlation 與竄改拒絕 | L | G | P2-T6 |
| P2-T8 | bridge 自測（§15.4.3／Spike 4）與資格報告 | L | G | P2-T7 |
| P2-T9 | socat 接線 spike 腳本（§15.4.1 第一步；執行順序在 Task 1 之後、Task 2 之前） | H | G | P2-T1 |
| P2-T10 | func-test 外部 bridge 模式、DaemonHarness 隔離與 artifact 選定、oracle 修復、Windows Telnet console client；實際部署內容量測 `realhw/deployed_content.py`（各報告產生端共用，R5-F4） | G | H（10A）＋L（10B，依賴 10A） | P2-T8 |
| P2-T11 | §15.7.1 情境腳本與協調器新動作 | H | G | P2-T10 |
| P2-T12 | `tests/acceptance/cases.yaml` 承接清單、schema、凍結鎖、Stage 0 證據索引、MCU 手動程序登記 | G | H | P2-T11 |
| P2-T13 | pytest 資源報告、平台 SKIP 閘門、Windows collection 與 hosted Windows L1（`TST-D-4` 報告部分；報告帶 `deployed_content`，R5-F4） | G | H | P2-T12 |
| P2-T14 | L2／L3 平台 adapter（pin＋版本 gate、PID／RSS、bench lock、能力式 SKIP、Telnet human frontend、worker 存活事件、background 結果執行期收斂 `BgResultTracker`（R1-F6）；realhw／TestPilot `report.json` 帶 `meta.deployed_content`（R5-F4）） | H | L | P2-T13 |
| P2-T15 | `longrun_acceptance.yaml`、外層 `release_gate_result` 判定器、長跑觀測器、Windows 原生 COM 獨占證據 `winexcl`（R1-F7）；`release-gate.json` 帶 `run.deployed_content`（R5-F4） | L | H | P2-T14 |
| P2-T16 | OD-09 延後項凍結工具與候選長跑開跑登記（`realhw/acceptance/od09_freeze.py`；延後項唯一寫入者；只讀 Stage 0 索引，不依賴 P3～P9，R4-F2） | L | H | P2-T12、P2-T15 |
| P2-T17 | docker 驗證環境與兩容器拓樸（OD-15：假 COM PTY pair、systemd container、`run-l15.sh`／`run-systemd.sh`／`run-longrun.sh`／`run-supervision.sh`、`evidence_layer` 標記，docker 證據不計 L2／L3／bench） | H | L | P2-T4、P2-T6、P2-T10、P2-T12、P2-T15 |
| P2-T18 | Windows host 監管 >72h throwaway 排程工作與 Spike 1 剩餘項收尾（OD-15；COM20／COM21） | G | L | P2-T3、P2-T8、P2-T12、P2-T15 |

### P3（`04-stage1a-rust-cli.md`；Phase 前置：P0、P1（T15 SC-09、T16 DF-06、T18 manifest）、P1-OC（Task 8 `gen_error_codes` 前）、P2（T10、T12））

| ID | Task | 測試作者 | 實作者 | 依賴（同 Phase Task） |
|---|---|---|---|---|
| P3-T1 | Cargo workspace 骨架、stub 與 cfg 邊界檢查 | G | H | — |
| P3-T2 | 黑箱契約 harness（`tests/contract_blackbox/`） | H | G | P3-T1 |
| P3-T3 | `sw-proto::pyjson` | H | G | P3-T2 |
| P3-T4 | `sw-proto::pycompat`／`pyfmt`／`pypath` | G | H | P3-T3 |
| P3-T5 | `sw-proto::pyre`（Python `re` 相容） | H | L | P3-T4 |
| P3-T6 | `sw-proto::yaml11` 與 `config_types` | H | G | P3-T5 |
| P3-T7 | `sw-proto` 資料型別（constants、runtime_paths、endpoint、version、rpc_types、wal_types、release_manifest） | G | H | P3-T6 |
| P3-T8 | 錯誤碼生成與命名空間 | G | H | P3-T7 |
| P3-T9 | `sw-pal` client 側 | H | L | P3-T8 |
| P3-T10 | argparse 相容層、`cli_spec` 與委派路由 | G | L | P3-T9 |
| P3-T11 | `output` 與 `cli_trace` | G | H | P3-T10 |
| P3-T12 | endpoint 解析（#108）與 bench state | H | G | P3-T11 |
| P3-T13 | RPC client（白名單重試、退避、TIMEOUT 探測） | G | L | P3-T12 |
| P3-T14 | 資料面 RPC 指令與 `main` 分派 | G | H | P3-T13 |
| P3-T15 | `event`／`profile test`／`skill` | G | H | P3-T14 |
| P3-T16 | Stage 1A 透明委派與 `mkbundle.sh --dev` | H | L | P3-T15 |
| P3-T17 | Windows 原生 interop 執行（win-runner／win-test／win-blackbox、匯入 DLL 與 100 次啟動檢查） | H | L | P3-T16 |
| P3-T18 | A/B 比較器、Rust CLI→Python daemon 黑箱與 Stage 1A gate 判定器 | H | G | P3-T17 |
| P3-T19 | Rust CI workflow 與 Stage 1A 出口 gate | G | H | P3-T18 |

### P4（`05-stage1b-rust-cli-mgmt.md`；Phase 前置：P3（全部）、P2（T16：B-12 混合 48 h 候選長跑開跑前 `register-candidate`；OD-09 於正式 Stage 0 出口已凍結））

| ID | Task | 測試作者 | 實作者 | 依賴（同 Phase Task） |
|---|---|---|---|---|
| P4-T1 | 介面骨架、P3→P4 遷移、測試替身、黑箱 stub、案例綁定與 golden 擷取工具 | G | H | — |
| P4-T2 | PAL `SysEnv` 管理面方法與 Windows 登錄讀取（`RegistryView`） | H | L | P4-T1 |
| P4-T3 | PAL `ServiceCtl`：systemd、`windows-task`（Task XML／schtasks）、`windows-service`（sc.exe）；交接前監管拉起抑制 `hold_autostart`／`release_autostart`（systemd 持久 drop-in `Restart=no`＋`RefuseManualStart=yes`＋`disable`，R5-F1） | H | L | P4-T1 |
| P4-T4 | PAL `ProcessCtl`（spawn／poll／foreground）與 `ProcScan`（POSIX `/proc`、Windows Toolhelp32） | G | L | P4-T1 |
| P4-T5 | PAL `ProcessTunnel`（BC-05）與 `FsOps` 跨行程鎖／連結 | H | L | P4-T1 |
| P4-T6 | `config.yaml` 寫入、`service`、`supervision-mode` 查詢（DF-07） | G | H | P4-T2、P4-T3、P4-T5、P4-T17 |
| P4-T7 | `daemon start/stop/status/restart`、`graceful_stop`、多開預檢與 Windows 首次需求啟動（SC-10、SC-17、SC-18、SC-20、DF-05）；`graceful_stop_with(Handoff)` 先 hold 所有 mode、被拉起實例重新交接、`hard_stop` 只對 grant identity，SC-18 於 `install-journal.json` 存在時暫停（R5-F1） | H | L | P4-T3、P4-T4、P4-T6、P4-T11、P4-T17 |
| P4-T8 | `remote`（`-R`／`-L`／`status`／`close`／`stop` 別名）原生與 BC-05 Windows 路徑（SC-21） | H | L | P4-T2、P4-T5 |
| P4-T9 | `connect`／`benches` 原生（SC-27） | G | H | P4-T8、P4-T17 |
| P4-T10 | `setup`（資產物化、WSL systemd、reconcile）與 `supervision-mode set` 參數驗證、reconcile `OldStop`（SC-12、SC-22 部分、SC-24、DF-10～DF-13、DF-16；切換執行與交接護欄在 P4-T12，R1-F3） | G | L | P4-T3、P4-T4、P4-T5、P4-T6、P4-T7、P4-T11 |
| P4-T11 | 安裝佈局、release manifest 驗證與版本配對 gate（§9.5、§12.1、SC-25） | H | L | P4-T2、P4-T3、P4-T5、P4-T17 |
| P4-T12 | §12.1 首次安裝、更新流程與 `setup --rollback`（SC-23；步驟 3、步驟 7 回退與 journal 復原停止 daemon 皆經 `daemon.handoff_stop` 交接（R1-F1、R2-F6）、journal 原始快照與分段備份中斷復原（R1-F5）；交接前抑制監管拉起並持久化於 journal、切換前／啟動前重驗 endpoint 並對新實例重新交接、hold 期間重新註冊以停用狀態送出（R5-F1））；`supervision-mode set` 執行入口 `run_supervision_switch`：同一 `safety_check`＋`daemon.handoff_stop{reason:"supervision_set"}`，不受 `--force` 影響（SC-22，R1-F3） | G | L | P4-T4、P4-T7、P4-T10、P4-T11、P1-T8 |
| P4-T13 | `doctor` 全部檢查原生與 `version_pairing`（SC-25） | G | H | P4-T2、P4-T4、P4-T10、P4-T11 |
| P4-T14 | 終端類承接（console-attach／detach、`serialwrap-minicom` 端到端、Windows Telnet 提示）與委派集合清空 | H | G | P4-T7、P4-T10 |
| P4-T15 | Agent 權限模型文件與驗證工具（§10.3）、對外文件同步 | H | G | P4-T6、P4-T7、P4-T8、P4-T9、P4-T10、P4-T11、P4-T12、P4-T13、P4-T14 |
| P4-T16 | Stage 1B 整合：全群組黑箱 A/B、L1.5、效能 gate、Windows 原生 L1、契約與案例完成度核對 | G | H | 全部 |
| P4-T17 | `sw-daemon` launcher、`yaml11::emit`、`bench_registry` 與自含式 dev bundle（P3 明列為 P4 範圍者） | H | L | P4-T1 |
| P4-T18 | `windows-task` Spike 2 程序、腳本與證據 schema（P2 指派 `win-task-*` 案例；OD-15：擁有者 host 以 P2-T18 throwaway 排程工作實跑 >72h） | H | G | P4-T3、P4-T10、P4-T12 |
| P4-T19 | Windows 安裝的 com0com 選用元件（SC-36，OD-15：預設不勾選、只有該元件提權、附 GPLv2 與原始碼取得方式、Spike 1 剩餘項通過前標實驗性；`setup --with-com0com`／`--remove-com0com` 只新增） | H | L | P4-T4、P4-T11、P4-T12、P4-T17 |

### P5（`06-slice21-daemon-core.md`；Phase 前置：P0～P4、P1-OC）

| ID | Task | 測試作者 | 實作者 | 依賴（同 Phase Task） |
|---|---|---|---|---|
| P5-T0 | 範圍凍結、資源上限與事件延遲契約、介面骨架 | G | H | — |
| P5-T0b | Slice 2.1 必要案例集合凍結與承接率斷路器（§11.3、§12.2、OD-02、OD-13） | G | H | P5-T0 |
| P5-T0c | 上游缺漏介面補齊（sw-proto pycompat／Params／RuntimeConfig accessor、sw-pal Clock wall、RpcServerStream） | G | L | P5-T0b |
| P5-T1 | PAL test-fakes（FakeClock／FakeSerial／FakeWalFs／FakeDeviceSource／MemStream） | H | G | P5-T0c |
| P5-T2 | PAL POSIX serial＋UartExclusive＋哨兵 PTY | H | L | P5-T1 |
| P5-T3 | PAL Windows serial（overlapped、CancelIoEx） | G | L | P5-T2 |
| P5-T4 | PAL InstanceLock（Tier 1）＋SignalWait | G | L | P5-T3 |
| P5-T5 | PAL DeviceSource／DeviceWatcher（#100 前提、SERIALCOMM、藍牙排除、OD-11） | H | L | P5-T4 |
| P5-T6 | PAL WalFs（常駐 handle、identity、share-delete、不覆蓋 rename；`write_all_flush` 失敗回 `WalWriteError.sent`，R2-F3） | G | L | P5-T5 |
| P5-T7 | sw-core WalWriter（BC-01、DF-03、DF-17；`sent`／`dirty_tail` 重試協定，R2-F3） | H | L | P5-T6 |
| P5-T8 | sw-core CommandRegistry／SessionQueue（admission、出列、history、SC-06） | H | L | P5-T7 |
| P5-T9 | sw-core TxWriter＋Fence（送出前檢查、BC-03 三態、SC-07、TX 帳本結算／退休與上限（R1-F8）） | H | L | P5-T8 |
| P5-T10 | sw-core IoWorker reader＋RxWindow＋哨兵 fan-out | G | L | P5-T9 |
| P5-T11 | sw-core SessionCore／Mailbox／Generations／Actor（§5.3 的 2.1 列） | H | L | P5-T10 |
| P5-T12 | sw-core Two-Phase Drain 核心＋IoWorker 收尾＋Release／Attach（sans-IO 與 IoWorker 層） | G | L | P5-T11 |
| P5-T13 | sw-core 最小 Login＋帳密＋Quiet Gate＋Profile 能力閘 | H | L | P5-T12 |
| P5-T14 | sw-core 命令執行、agent Interactive Lease、self-test（`SelfTestProbe` 被 `Quiet` 擋時回 `AUTOBOOT_QUIET`，R5-F2／DF-19）、capture | G | L | P5-T13 |
| P5-T12b | Drain／Release 端到端（真 actor＋login＋命令執行） | G | L | P5-T14 |
| P5-T15a | 黑箱 FakeTarget 擴充（測試資產變更，§10.4） | H | G | P5-T12b |
| P5-T15 | sw-core StateStore＋AliasRegistry＋state.json 雙向相容 | G | L | P5-T15a |
| P5-T16 | sw-core SessionManager（裝置、rank #100、動態 session、selector、管理操作、SC-11 stopping gate `begin_shutdown`（R1-F2）） | H | L | P5-T15 |
| P5-T17 | PAL RpcListener＋sw-daemon framing | G | L | P5-T16 |
| P5-T18 | sw-daemon 分派器（service.rs／health.rs） | G | H | P5-T17 |
| P5-T19 | sw-daemon daemon_main（啟動順序、有序關機（stopping gate 先於 watcher.stop，R1-F2）、exit code） | H | L | P5-T18 |
| P5-T20 | 黑箱契約與既有套件改指 Rust（F1／F2／F3／F4／F12＋安全骨架） | H | L | P5-T19 |
| P5-T21a | P2 runner 補動作（測試資產變更，§10.4） | H | G | P5-T20 |
| P5-T21 | L1.5 bridge 回歸（P2 凍結 2.1 情境＋s21 情境）與事件延遲量測 | G | L | P5-T21a |
| P5-T22 | Windows 原生 L1 證據與 Slice 2.1 出口 gate 包 | H | G | P5-T21 |
| P5-T23 | Slice 2.1 完成校準報告（§12.2、OD-02、OD-13） | H | G | P5-T22 |

### P6（`07-slice22-console.md`；Phase 前置：P5）

| ID | Task | 測試作者 | 實作者 | 依賴（同 Phase Task） |
|---|---|---|---|---|
| P6-T0 | 契約登記、案例凍結與架構細化 | G | H | — |
| P6-T1 | 常數、模組骨架與 harness 簽章 | G | H | P6-T0 |
| P6-T2 | Telnet 協定層 | H | L | P6-T1 |
| P6-T3 | Line editor、shlex 與 human line mode | H | G | P6-T1 |
| P6-T4 | DeferredBuffer（BC-02） | G | L | P6-T1 |
| P6-T5 | 每 console 出向 ring（SC-15） | H | G | P6-T1 |
| P6-T6 | Ownership 狀態機、human TX lane 與 Fence 整合（回放保留 `ReplayHold`／`replay_auth`，D-22，R2-F4） | G | L | P6-T2、P6-T3、P6-T4、P6-T5 |
| P6-T7 | Interactive lease 差量（tombstone、soft preempt、ATTACHED 救援、stash） | H | L | P6-T2、P6-T3、P6-T4、P6-T5、P6-T6 |
| P6-T8 | PAL console backends（PTY／TCP／ConsoleMux／com0com） | G | L | P6-T1 |
| P6-T9 | ConsoleHub runtime 與 RX fan-out | H | L | P6-T8 |
| P6-T10 | RPC 表面、LeaseIndex 路由與 SessionPublic 欄位 | G | L | P6-T6、P6-T7、P6-T9 |
| P6-T11 | 黑箱契約與既有測試改指 Rust（Linux L1.5） | G | L | P6-T10 |
| P6-T12 | Windows 原生 console 與選配 com0com | H | L | P6-T11 |
| P6-T13 | 案例證據對照、文件、全套 gate | G | H | P6-T12 |

### P7（`08-slice23-login-events.md`；Phase 前置：P5、P6）

| ID | Task | 測試作者 | 實作者 | 依賴（同 Phase Task） |
|---|---|---|---|---|
| P7-T0 | 介面凍結、模組骨架、登記與案例凍結 | G | H | — |
| P7-T1 | Python 基線 golden 擷取 | G | H | P7-T0 |
| P7-T2 | `session_auth.rs`：golden 回歸與 Debug 遮蔽 | G | H | P7-T1 |
| P7-T3 | `login_fsm.rs`：全平台 Login FSM | H | L | P7-T2 |
| P7-T4 | `quiet_gate.rs`：Autoboot Quiet＋Ready Reconfirm | H | L | P7-T3 |
| P7-T5 | `reprobe.rs`：readiness reprobe 與 bootloader 證據 | H | L | P7-T4 |
| P7-T6 | `transport_stall.rs`＋PAL `UsbTopology`＋`FsOps` 寫入側 | G | L | P7-T5 |
| P7-T7 | Login run 協調、fence `fsm_gen`、帳密終態與收斂 | H | L | P7-T6 |
| P7-T8 | `self_test.rs`：self-test 分類與 nonce probe 整合（`SelfTestProbe` 看 `quiet_active`、`fsm_gen: None`，console unlock 亦擋，R5-F2） | G | L | P7-T7 |
| P7-T9 | `recover.rs`：session recover／force recover | H | L | P7-T8 |
| P7-T10 | `capture.rs`：agent log capture（reader 端 observer） | G | L | P7-T9 |
| P7-T11 | Event schema＋`pyre/redos.rs` | H | G | P7-T1 |
| P7-T12 | `line_buffer.rs`＋`matcher.rs` | H | G | P7-T11 |
| P7-T13 | `counter.rs`＋`registry.rs`＋`event_log.rs`（含 Python DF-18） | H | G（Rust）；13-py：測試 G／實作 H | P7-T12、P7-T6 |
| P7-T14 | PAL `HandlerSpawner`（POSIX／Windows，SC-26） | G | L | P7-T13 |
| P7-T15 | `dispatcher.rs`（含 `close_intake`、`DispatcherStopReport`，R1-F2） | H | G | P7-T14 |
| P7-T16 | EventEngine 組裝與 10 個 `event.*` RPC | H | G | P7-T15 |
| P7-T16-wire | reader `RxObserver` 接線（DF-15）、`BridgeQueries` 與 daemon 啟停順序 | H | L | P7-T10、P7-T16 |
| P7-T17 | session RPC 接線（self_test／recover／attach／log_*） | G | H | P7-T7、P7-T8、P7-T9、P7-T10、P7-T16-wire |
| P7-T18A | 黑箱測試工具擴充（`bb_faketarget`、P7 helper）——Stage T0，獨立 PR | H | G | — |
| P7-T18 | L1.5 黑箱契約、§11.3 不變量、延遲與背景雜訊、func-test、A/B | G | H | P7-T18A、P7-T17 |
| P7-T19 | Windows 原生 L1／COM 證據與 Phase gate | G | H | P7-T18 |

### P8（`09-slice24-transfer-flash.md`；Phase 前置：P2～P7）

| ID | Task | 測試作者 | 實作者 | 依賴（同 Phase Task） |
|---|---|---|---|---|
| P8-T0 | 上游介面核對與 P8 黑箱共用 fixture | H | G | P2～P7 |
| P8-T1 | Slice 2.4 案例登記、契約增補登記、斷路器凍結 | H | G | P8-T0 |
| P8-T2 | `mcu_patterns` registry 與 hex 語意 | G | H | P8-T0 |
| P8-T3 | File transfer 板端命令樣板與 RX 判定 helper（Python golden） | H | G | P8-T0 |
| P8-T4 | File transfer 引擎 `push_file`／`pull_file` | H | G | P8-T3 |
| P8-T5 | `OpTransferPort`：echo-ACK 節流、TX 證據聚合與中止 | G | L | P8-T4 |
| P8-T6 | `file.push`／`file.pull` 的 session 整合與 RPC 分派 | H | L | P8-T0、P8-T5 |
| P8-T7 | PAL flash 端點能力 | G | L | P8-T0 |
| P8-T8 | SessionCore FLASHING 仲裁 | H | L | P8-T0 |
| P8-T9 | FlashEndpoint 執行緒與認線協調（`EndpointShutdown` 與每次 flash 的 `FlashRunCancel` 分離，R2-F5） | G | L | P8-T0、P8-T2、P8-T7、P8-T8 |
| P8-T10 | `mcu.patterns`／`mcu.status` RPC 與 daemon 組裝 | G | L | P8-T9 |
| P8-T11 | 外部工具裝置交接的黑箱與整合驗證 | H | L | P8-T0、P8-T6、P8-T8 |
| P8-T12 | systemd 監管整合與有序停止（完整停止序含 stopping gate／watcher／event engine 收尾（R1-F2）；未收斂 exit 1（R1-F4）） | G | L | P8-T6、P8-T9、P8-T11 |
| P8-T13 | `windows-task` 監管對 Rust daemon 的驗證 | H | L | P8-T12 |
| P8-T14 | 更新／回滾守衛對 Rust daemon 的驗證與補強（SC-23）；Rust `AdmissionGate`＋`daemon.handoff_stop`（SC-11，R1-F1）；Rust daemon 交接後 exit 1 不被 systemd 拉起、切換期間被拉起實例的 flash 不被截斷（R5-F1 黑箱） | G | L | P8-T6、P8-T9 |
| P8-T15 | 全部凍結契約的最終覆蓋判定與切片出口腳本 | H | G | P8-T0～14 |

### P9（`10-stage3-canary.md`；Phase 前置：P0～P8（以前置 Phase 交付物為介面））

| ID | Task | 測試作者 | 實作者 | 依賴（同 Phase Task） |
|---|---|---|---|---|
| P9-T1 | 契約檔、schema 與基礎模組 | G | H | — |
| P9-T2 | 共用基礎測試替身（`tests/stage3_fakes.py`） | H | G | P9-T1 |
| P9-T3 | A/B manifest 載入、凍結與案例涵蓋檢查（`tools/stage3/ab_manifest.py`；`verify_bundle`／`bundle_tree_sha256`，R2-F7；`runtime_tree_sha256`，R3-F2） | H | G | P9-T2 |
| P9-T4 | A→B 資源釋放驗證（`tools/stage3/release_check.py`） | H | L | P9-T3 |
| P9-T5 | 乾淨 DUT 狀態與實例隔離（`tools/stage3/dut_state.py`） | G | H | P9-T4 |
| P9-T5B | Suite adapter（`tools/stage3/suites/`；`verify_run_identity` 核對 suite 量測的部署內容＝該側凍結 tree，R5-F4） | H | L | P9-T5 |
| P9-T6 | Sequential A/B 執行器（`tools/stage3/ab_runner.py`） | H | L | P9-T14B |
| P9-T7B | L1.5 A/B 差分（`tools/stage3/ab_diff.py`） | L | H | P9-T5B |
| P9-T7 | A/B 判定（`tools/stage3/ab_verdict.py`） | L | H | P9-T7B |
| P9-T8 | Canary WAL 收割與提交計數（`tools/stage3/canary_harvest.py`） | G | L | P9-T6 |
| P9-T9 | Canary collector 與 zipapp（`tools/stage3/canary_collect.py`；結果於 history 淘汰前持續收斂，R1-F6；每次快照對部署目錄重算 `bundle_tree_sha256`／`runtime_tree_sha256`，R4-F1） | H | L | P9-T11 |
| P9-T10 | 干擾／交接分類與活躍日（`tools/stage3/canary_classify.py`；歸屬鍵含內容指紋，R4-F1） | H | L | P9-T8 |
| P9-T11 | 事故追蹤（`tools/stage3/canary_incidents.py`） | G | H | P9-T10 |
| P9-T12 | Canary policy 凍結、帳本彙總與配額 gate（`tools/stage3/canary_ledger.py`；gate 輸出帳本版本 `ledger_through`／`ledger_records`，R3-F1；只計內容指紋等於 target 的紀錄並於報告輸出指紋，R4-F1） | L | G | P9-T9 |
| P9-T13 | 實機回滾演練（`tools/stage3/rollback_drill.py`；報告記錄 current 部署目錄內容指紋，R4-F1） | G | L | P9-T12 |
| P9-T14 | Stage 0～3 出口證據索引（`tools/stage3/evidence_index.py`；Stage 3 target 記錄 `bundle_tree_sha256`，R2-F7；entry 保存受測內容指紋並納入有效性、同 manifest 換 tree 拒絕重設 target，R3-F2；canary-report 項目只以帳本版本最新報告判定，新事故撤銷舊 PASS，R3-F1） | L | G | P9-T7 |
| P9-T14B | 證據 ingest（`tools/stage3/evidence_ingest.py`；報告受測內容指紋核對 target，R3-F2；canary 報告防回放，R3-F1；ab／canary／drill 報告缺指紋即拒收、不以 target 回填，R4-F1；junit／functest／regression／realhw／gate／manual 一併納入 TREE_REQUIRED_KINDS、移除時刻規則，R5-F4） | H | L | P9-T15 |
| P9-T15 | OD-09 凍結結果的 Stage 3 消費端（`tools/stage3/od09_consume.py`：`od09-freeze` 報告判定、`S0-OD09-DEFERRED-FROZEN`／`S3-OD09-ALL-FROZEN` 核對、`od09 check` 唯讀委派；凍結工具屬 P2-T16，P9 不寫延後項，R4-F2） | L | H | P9-T14 |
| P9-T16 | Python daemon 退役切換（packaging；發佈前逐檔重驗 bundle 內容與 tree、`retirement pack`＋`verify-archive` 後只上傳已驗 archive，R2-F7；旁載檔後重設 target 或新事故報告後拒絕發佈，R3-F1／R3-F2） | H | L | P9-T13 |
| P9-T17 | `python3 -m tools.stage3` 入口與擁有者程序文件 | G | H | P9-T16 |

### P1-OC（`02b-stage0-owner-confirmations.md`；Phase 前置：P1（全部 Task 合併後）；須在 P3 Task 8 與 P5 開始前合併）

OD-14（2026-10-16）裁決套用到 Python Stage 0 基線與 P0 契約。新 SC：SC-34（#1）、SC-35（#2）；SC-08 撤回（#3）；SC-04 改以 `DRAIN_STALLED` 回報 release drain 逾時（#4）；DF-02、SC-26、SC-27 確認原預設（#5～#7）。

| ID | Task | 測試作者 | 實作者 | 依賴（同 Phase Task） |
|---|---|---|---|---|
| P1-OC-T1 | 契約簿記：SC-34／SC-35 登記、SC-08 撤回、#5～#7 確認、pending 清空、`owner_item` 驗證放寬 | H | G | — |
| P1-OC-T2 | SC-34 attach／recover 回應分離 `last_error`，移除 `last_error_passthrough` meta 項 | G | L | P1-OC-T1 |
| P1-OC-T3 | SC-08 撤回：socket 權限還原 0660 基線 | H | L | P1-OC-T1 |
| P1-OC-T4 | `DRAIN_STALLED`：release drain 逾時新錯誤碼 | G | L | P1-OC-T1 |
| P1-OC-T5 | SC-35a passthrough 狀態機（不登入／不 probe、30 s 安靜升 READY、gate、self-test／recover／attach） | H | L | P1-OC-T2 |
| P1-OC-T6 | SC-35b passthrough 命令路徑（完成條件、無救援按鍵、file.* 拒絕；SC-35 → IMPLEMENTED） | G | L | P1-OC-T5 |
| P1-OC-T7 | 文件、SKILL 受管區段、#155 回歸 case、docker／Windows 原生驗證收尾 | G | H | P1-OC-T2～T6 |

### 跨 Phase 關鍵交接（本次審查定案的唯一 owner）

| 介面／檔案 | 唯一建立者 | 後續 Phase 的處置 |
|---|---|---|
| 測試檔平台／資源分類 | P0-T14（檔內 `pytestmark`） | P1～P9 不建 `tests/platform_resources.yaml`；Windows collection 排除由 P2-T13 `pytest_ignore_collect`（P1-T1 `win-pytest.sh` 以同規則 AST 掃描） |
| `contracts/golden/pycompat.tsv` | P3-T4 | P5-T0c 另建 `pycompat_s21.tsv` |
| `sw_proto::pycompat::{shlex_split, shlex_quote, b2a_base64, strip_ansi, clean_text, uuid4_*, iso_utc_now, strftime_*, py_round, coerce_rpc_bool, a2b_base64_lenient, to_printable}` | P5-T0c | P6-T3 只加 shlex 回歸 golden；P7、P8 直接消費 |
| `HolderScanStatus` | P4-T1（arch §4.2 名稱） | P5 `HolderReport`／`UartExclusive` 沿用，不重定義 |
| `FakeClock` | P3-T9（auto-advance）、P4-T1 擴充 | P5-T1 擴充同一型別：`FakeClock::at(mono, wall)`＝threaded 模式 |
| `device_source/{mod.rs, windows_model.rs}`、`RegistryView`、`read_bt_ports`、`exclude_bluetooth` | P4-T1／T2 | P5-T5 只新增 posix.rs、windows.rs 與 `WindowsDeviceSource` |
| `FsOps`（`mkdirs`、`remove_file -> PalResult<()>` 等） | P4-T1／T5 | P7-T6 只新增 `open_append`、`create_truncate`、`is_file`、`file_size`、`remove_file_if_exists` |
| `resolve_config_path`（SC-09） | P3-T7 | P5-T19 委派（P3 沒有 `config_candidates`） |
| `crates/sw-core/src/mcu_patterns.rs` | P5-T0（骨架） | P8-T2 Modify |
| `scripts/rust/slice_breaker.py` | P5-T0b | P6-T0、P7-T0、P8-T1 只新增 `breaker/slice_2.x.json` |
| `contracts/ab_normalization.yaml` | P5-T20 | P9-T7B 只追加鍵 |
| `longrun_acceptance.yaml` 三個 OD-09 延後項的寫入（`realhw/acceptance/od09_freeze.py`：`freeze`／`check_frozen_before`／`register_candidate_start`、`candidate_longrun_starts.ndjson`）、`docs/rust-refactor/owner-decisions-addenda.md`、`procedures/od09-deferred-freeze.md` | P2-T16（Stage 0；只依賴 P2-T12、P2-T15，R4-F2） | P4 Task 16（B-12）、P8 Task 15（第 8 項）長跑開跑前 `register-candidate`；P9-T6 L3 A/B 呼叫 `check_frozen_before`＋`register_candidate_start`；P9-T15／T14B 只消費 `freeze --out` 報告；P9-T1 不重建 addenda、P9-T17 不重建該程序檔；任何 Phase 不得另寫延後項或手改 acceptance |
| `tests/acceptance/evidence_index.schema.json` | P2-T12 | P9-T1 另建 `stage3_evidence_index.schema.json` |
| L1 完整性：`tests/acceptance/l1_required.lock.json`（每平台必要 nodeid 凍結）、`l1_results.schema.json`（`l1-results.v1`＝`SW_RESOURCE_REPORT` v2）、`l1_judge.py`（唯一判定實作）、`regen_lock.py --l1-add-only`；cargo 端 `scripts/rust/cargo_l1_results.py` | P2-T13；cargo 轉換器 P3-T17（R5-F3） | P3-T18、P4-T16、P5-T22、P6-T13、P7-T19、P9-T1 只以 `--l1-add-only` 新增 set，set 內 nodeid 由各 Phase 每個 Task 的紅燈 commit 逐 Task 登記、PR 以 `regen_lock.py --check --base` 防縮減（R5 審查後續）；P3～P9 的 L1 出口與 gate 一律依路徑載入 `l1_judge.py` 判定，不得以 `tests>0`、`passed>0`、`passed≥N` 或函式存在判定；P8-T15 結果檔同帶 `collected`／`exit_code`／`finished` |
| `tests/contract_blackbox/bb_faketarget.py` | P3-T2 | P5-T15a、P6-T11、P7-T18A 以 keyword-only 擴充（不存在 `contract_blackbox/faketarget.py`） |
| `behavior_changes.yaml` 新條目欄位 | P0-T1／T5 schema | 一律 `approved_at_revision`＋`ab_participation`，`contracts/` 內不得出現 `contract_revision` 鍵 |
| SC 編號 | 調和表 SC-01～27（SC-08 已由 OD-14 撤回，編號不重用）；P1-OC：SC-34、35（OD-14） | P6：SC-28、29、31；P7：SC-30；P8：SC-32、33（原稿 SC-31／32 撞號，已改）；P4-T19：SC-36（OD-15 com0com 選用元件，Rust-only）；`test_bc_real_sc_df_ranges` 依序改為 P1-OC `range(1,28)−{8}∪{34,35}` → P4 `range(1,28)−{8}∪{34,35,36}` → P6 `(range(1,30)∪{31,34,35,36})−{8}` → P7 `(range(1,32)∪{34,35,36})−{8}` → P8 `range(1,37)−{8}`；P1-OC 清空 `pending_owner_confirmations` 並把 `owner_item` 驗證放寬為 ≥1，P8 的 SC-33 以 item 8 登記；DF-18 屬 P7；DF-19（self-test nonce probe 提交點 quiet 重驗，R5-F2）由 P0-T5 登記、P1-T20 實作，DF 期望 P0 為 `range(1,18)∪{19}`、P7 Task 0 起為 `range(1,20)` |

### 審查後仍未解決（需擁有者或後續 PR 處理）

1. （已解決，R0-F6）P8-T10（daemon 組裝、flash endpoint 啟動序）與 P8-T14（更新／回滾守衛）已依 §2 原則與 P5 §0.4「daemon 生命週期一律 L」改派 L（09 §0.2、Task 總覽與兩 Task 標頭同步）；測試作者維持 G（≠ 實作者），Sonnet 逐項核對不變量的審查要求保留。
2. P5-T1（PAL test-fakes，含可注入部分寫入／取消失敗的 `FakeSerial`）由 G 實作；依 P5 自述屬「獨立模組」，未視為違規，但 fakes 的 cancel 語意錯誤會讓 L1 誤綠，建議審查 A 專審。
3. （已解決，R0-F6）P1-T1 的 `win-pytest.sh`（Windows 行程樹終止、WSLENV）比照 P3-T17 改派 L；P1 §0.1 的 Test reviewer（A1.5）同步由 L 改為 G，使測試作者 H／審核者 G／實作者 L 三者不同；Windows 實跑由 orchestrator 代跑。另 01 計畫的本機絕對路徑已改為 `$SW_ROOT`／`$CONV_REPO`／`$CONV_WT` 變數。
4. P7-T18A 的 `bb_faketarget` 擴充鍵（`noise` 等）與 P5-T15a（`noise_interval_s`）可能重複，P7 執行時需合併為單一參數。
5. P8-T10 新建 `crates/sw-daemon/tests/support/service_rig.rs` 與 P5-T18 `support/mod.rs` 既有 `ServiceRig` 可能同名衝突，P8 執行時應改為擴充既有型別。
6. arch 文件（`rust-architecture.md`）尚未同步本次定案（`HolderScanStatus` 共用、`FakeClock::at`、`pycompat_s21.tsv`、P8 SC 改號）；由各 owner Task 依其「修訂 arch」步驟寫回。

### 執行期待追蹤項目（2026-10-20）

- `tests/test_stage0_uart_exclusive.py::TestManagerAttach::test_concurrent_attach_device_single_open`：合併 P1-OC＋P2 後主線全套 7 輪中 2 次非確定性失敗（失敗時未保存 traceback；單獨連跑 10 次、整檔、4800 程序 `/proc` 負載皆通過；依序 attach 已確認由 preopened 擋下，排除「負載下退化為依序雙 open」）。`e71dbe2` 已加 thread 完成斷言並於失敗訊息附兩次 attach 回應；**未判為 flaky、未放行**，下次重現時依訊息定位根因（候選：鎖競爭使 attach 超過期限、前序測試洩漏的 thread／狀態）。
