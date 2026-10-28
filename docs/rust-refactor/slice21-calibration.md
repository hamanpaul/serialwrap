# Slice 2.1 完成校準報告（P5-T23；§12.2、OD-02、OD-13）

> 狀態：**初版（本環境可得資料）**。Slice 2.1 出口尚未通過：本環境 gate 尚未產出 `READY_FOR_OWNER_EVIDENCE`
> （T21 L1.5 A/B 需 P2 bridge 資格報告與 release artifact，未在本環境執行；T22 gate 僅 `--dry-run`，verdict
> `INCOMPLETE`），BO-S21-01～16 證據未回填。擁有者欄位一律標「待擁有者補值」，**不得據以重算後續切片工期**。
>
> 資料來源：`git log bba1b21^..4920d90`（分支 `feature/rust-refactor-p5`）、`tests/acceptance/breaker/slice_2.1.json`
> （Task 0b 凍結）、`scripts/rust/slice21_gate.sh --dry-run` 報告、`docs/rust-refactor/reviews/p5-fix-responses.md`。

## 實測工作量

紅燈 commit 統一為 `bba1b21`（2026-10-21 15:26 UTC，「依計畫先寫 P5 失敗測試」）。綠燈 commit 取各 Task 主實作 commit；
修正輪數為該 Task 範圍內之後 `fix(...)` commit 數（依處理紀錄 F 編號歸屬，跨 Task 的修正各計一次）。時距為牆鐘，
含排隊與審查等待，非純實作工時。

| Task | 綠燈 commit | 綠燈時間（UTC） | 紅燈→綠燈 | 修正輪數 |
|---|---|---|---|---|
| T0a／T0b 範圍清單、資源上限、分母凍結 | `498c304`、`d757a4a` | 10-23 03:18～03:47 | ≈1.5 日 | 0 |
| T1 sw-proto helper | `dfd5f58` | 10-23 05:09 | ≈1.6 日 | 0 |
| T2～T7 sw-pal（fake、POSIX／Windows serial、lock、device、WalFs） | `6ccb118`～`df66409` | 10-23 06:47～18:40 | ≈1.6～2.1 日 | 3（`fb76460`、`18358a5`、`e671620`） |
| T7 WalWriter | `7c084ef` | 10-26 10:22 | ≈4.8 日 | 2（`2d93181`、`5f625a2`） |
| T8 命令 admission | `0721a4c` | 10-26 10:59 | ≈4.8 日 | 0 |
| T9 TxWriter／fence | `50345ea` | 10-26 12:45 | ≈4.9 日 | 1（`f346f6b`） |
| T10 IoWorker reader | `335665e` | 10-26 19:30 | ≈5.2 日 | 1（`10e4ea6`） |
| T11 SessionCore／Mailbox／Actor | `f20bdd0` | 10-26 22:38 | ≈5.3 日 | 0 |
| T12／T12b Drain／Release | `afe4853`、`b73da42` | 10-27 08:46／03:04 | ≈5.5～5.7 日 | 2（`0e47d0c`、`7536870`） |
| T13 登入／boot quiet | `0a187c1` | 10-27 01:52 | ≈5.4 日 | 5（`18abb03`、`177dfd9`、`620ca8e`、`46419db`、`f346f6b`） |
| T14 命令執行／lease／self-test／capture | `8b8a40f` | 10-27 02:43 | ≈5.5 日 | 3（`348eb2f`、`10e4ea6`、`3b5927a`） |
| T15a 黑箱 FakeTarget | `b779527` | 10-27 01:38 | ≈5.4 日 | 0 |
| T15 StateStore | `c96a91c` | 10-27 01:57 | ≈5.4 日 | 1（`47c7b25`） |
| T16 SessionManager | `73634db` | 10-27 02:45 | ≈5.5 日 | 8（`7a84168`、`9363d9b`、`5270c9d`、`21de0db`、`6aafe8f`、`314eedd`、`e95c5e9`、`b069633`） |
| T17 RpcListener／framing | `2fcdbbf` | 10-27 01:38 | ≈5.4 日 | 2（`c43bb3e`、`e671620`） |
| T18 分派器 | `8a7986e` | 10-27 03:31 | ≈5.5 日 | 1（`9fdd89d`） |
| T19 daemon_main | `3938032` | 10-27 04:20 | ≈5.5 日 | 3（`edfc741`、`d46c323`、`3afdc4f`） |
| T20 黑箱契約 | `1f5aafa` | 10-27 05:20 | ≈5.6 日（**未完成**，見 F103） | — |
| T21a P2 runner 動作 | `2af2554` | 10-27 02:57 | ≈5.5 日 | 0 |
| T21 L1.5 A/B | — | — | 未完成（BLOCKED：bridge 資格報告、release artifact） | — |
| T22 gate 包 | `b7cd852`、`4920d90` | 10-27 04:00／15:45 | ≈5.5 日（僅 dry-run） | — |

雲端雙審（grok＋Claude Opus）共開出 F01～F111，處置見 `docs/rust-refactor/reviews/p5-fix-responses.md`。

## Bench 等待

BLOCKED-OWNER 各項自 2026-10-27 起計；等待天數由擁有者回填時計算。

| ID | 項目摘要 | 開始等待 | 等待天數 |
|---|---|---|---|
| BO-S21-01 | 原生 Windows COM handle gate（OD-08；OD-15 起擁有者 host 可執行） | 2026-10-27 | 待擁有者補值 |
| BO-S21-02 | Windows 外部 TeraTerm／PuTTY 先開 COM | 2026-10-27 | 待擁有者補值 |
| BO-S21-03 | Windows USB 拔除時 overlapped 完成碼 | 2026-10-27 | 待擁有者補值 |
| BO-S21-04 | 實體 USB-UART TIOCEXCL＋flock、RTS/CTS 可取消寫入 | 2026-10-27 | 待擁有者補值 |
| BO-S21-05 | 外部 minicom 先開埠得 `Held`（Linux 實機） | 2026-10-27 | 待擁有者補值 |
| BO-S21-06 | Windows CTRL_BREAK／CTRL_CLOSE 與 windows-task 啟停 | 2026-10-27 | 待擁有者補值 |
| BO-S21-07 | L2 `serialwrap_regression` F1／F2／F3／F4／F12 | 2026-10-27 | 待擁有者補值 |
| BO-S21-08 | L3 `realhw` P0 核心集合＋P1 | 2026-10-27 | 待擁有者補值 |
| BO-S21-09 | 外部燒錄工具接手→`device attach` 收回 | 2026-10-27 | 待擁有者補值 |
| BO-S21-10 | 實機登入→READY 與 reboot quiet | 2026-10-27 | 待擁有者補值 |
| BO-S21-11 | 同款 CH340 by-path COM 指派 | 2026-10-27 | 待擁有者補值 |
| BO-S21-12 | systemd 監管下 Rust serialwrapd 啟停 | 2026-10-27 | 待擁有者補值 |
| BO-S21-13 | Windows L1.5 與實機 A/B | 2026-10-27 | 待擁有者補值 |
| BO-S21-14 | Spike 3：EDR 下 Rust WalFs | 2026-10-27 | 待擁有者補值 |
| BO-S21-15 | 藍牙 SPP 虛擬 COM 排除 | 2026-10-27 | 待擁有者補值 |
| BO-S21-16 | Windows L2 EDR 下 F12 WAL 進展 | 2026-10-27 | 待擁有者補值 |

## 安全里程碑

§11.6 Slice 2.1 安全出口各項於本環境的證據狀態（「Linux L1」指 `cargo test --workspace --features test-fakes`
與 L1 鎖 `s21_core.linux`；root 下 3 支以 FAIL 報證據不足、非 root 以 `setpriv --reuid=65534` 補跑）。

| 項目 | Linux L1 | Windows 原生 | L1.5／實機 |
|---|---|---|---|
| 單一寫入者、fence 線性化、零 TX 拒絕 | 有（tx_writer、command_exec） | LOCAL-WINDOWS-TODO | 未執行（T21／BO-S21-07） |
| BC-03 部分寫出中止不重送（含在途 Unknown） | 有（drain_release、drain_release_e2e） | LOCAL-WINDOWS-TODO | 未執行 |
| Two-Phase Drain／release／attach 交接（DRAIN_STALLED、Phase 1 快照） | 有 | LOCAL-WINDOWS-TODO | BO-S21-09 |
| 獨占開啟與 `DEVICE_STILL_HELD` | 有（非 root） | BO-S21-01／02 | BO-S21-04／05 |
| boot quiet 零 TX（含 drain 保留 quiet） | 有（login_quiet、session_core） | LOCAL-WINDOWS-TODO | BO-S21-10 |
| 帳密未解析終態 | 有 | LOCAL-WINDOWS-TODO | 未執行 |
| WAL 常駐 handle、外部刪檔自癒、輪替不覆蓋 | 有 | LOCAL-WINDOWS-TODO（share-delete） | BO-S21-14／16 |
| 有序關機與 exit code | 有（daemon_process；F91／F111 偶發待追查） | BO-S21-06 | BO-S21-12 |

## 平台缺陷

**POSIX（本 Phase 發現並修正）**

- holder 掃描以 readlink 字串比對，別名路徑誤判（F02 → inode 身分 `same_tty`）；測試依賴主機 `/dev/pts/9`（F01）。
- root 下 TIOCEXCL 不生效：相關案例改以 FAIL 報證據不足、非 root 補跑（F81、F108 待擁有者定 L1 收集規則）。
- RPC 回應寫入在 stop 前即套 2 s 逾時截斷大回應（F06 → 平時無期限、stop 後 2 s 總期限）。
- state.json 只接受 UTF-8，UTF-16／32 與雙 BOM 行為與 Python 不同（F54、F58）。

**Windows（本環境僅 `cargo check --target x86_64-pc-windows-gnu`，原生行為皆 LOCAL-WINDOWS-TODO）**

- Winsock send 逾時後 socket 狀態未定，framing 不得重試（F06 Windows 半段：PAL `SEND_TIMEOUT_RETRYABLE=false`）；慢讀大回應的 WSAPoll 路徑待原生驗證。
- state.json 文字模式 CRLF 換行（F55，`TEXT_NEWLINE`）待原生驗證。
- 預開 port `configure_port`（F59）Windows 實作待原生驗證。
- Linux 上 `SERIALWRAP_RPC_BACKEND=win` 的 Python 行為（Linux 建 TCP RPC）未移植，候選 SC-37 待擁有者裁決（F109）。

## 回歸重跑

| 層級 | 重跑次數 | 原因 |
|---|---|---|
| L1（Linux 全量 pytest，`drive.sh` 分段） | 6 | 每組審查修正推送前各一次；新增失敗只見既有 QUEUE_FULL 兩支 flake（F94 洩漏 daemon 劫持 `/dev/pts/0`） |
| L1（sw-daemon cargo 全量） | 本輪 2 | 首輪 `kill_before_commit_restores_previous_state` 偶發失敗，第二輪與單獨 2/2 通過（F111） |
| L1.5 | 0 | 未執行（T21 BLOCKED：P2 bridge 資格報告、release artifact） |
| L2 | 0（待擁有者補值） | BO-S21-07 |
| L3 | 0（待擁有者補值） | BO-S21-08 |

## 承接率

分母：49

`slice_2.1` 凍結於 2026-10-21（`denominator_sha256` 見 `tests/acceptance/breaker/slice_2.1.json`）。

| 週（起） | valid_pass | rate | 斷路器 |
|---|---|---|---|
| 2026-10-21 | 0 | 0.00 | 未觸發（尚無 gate 判定為 PASS 的案例；完整 gate 未執行） |

`case_uptake.valid_pass` 只計 gate 判定為 PASS 的 `slice_2.1` 案例；本環境 gate 僅 dry-run，故記 0，待完整 gate 後更新。

## OD-02 24 週上限輸入

以下為**規劃估計**，非承諾；擁有者 bench 等待與實機重跑次數未回填前不得據以重算工期。

- Slice 2.1 本環境實作（T0～T19、T21a、T22 骨架）自紅燈至最後一組修正約 6 個日曆日（2026-10-21～10-27），其中雲端雙審修正輪佔約 1 日。
- 未完成且計入 Slice 2.1 的本環境工作：T20 release artifact A/B 重跑與交付報告、T21 L1.5 A/B（10 個 s21 情境＋8 個 P2 凍結情境 × 2 label）、T22 完整 gate、Windows 原生 L1（LOCAL-WINDOWS-TODO）；粗估 1～2 週（規劃估計）。
- 擁有者項（BO-S21-01～16）：待擁有者補值。
- 外推 Slice 2.2～2.4：若各切片規模與 2.1 相當，本環境部分各約 1.5～3 週（規劃估計），加上擁有者 bench 等待（待擁有者補值）；以 OD-02 24 週上限計，尚餘緩衝需待 bench 等待實測後重算。
