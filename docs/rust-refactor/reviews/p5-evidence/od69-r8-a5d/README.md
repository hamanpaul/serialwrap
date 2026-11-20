# OD-69 接線整體驗收（P5-r8-a5d）

第 2 輪於 `3bf93828` 的當前重跑、前審八項處置及嚴格邊界阻塞，另見 [第 2 輪驗收](round2/README.md)；本頁保留第 1 輪證據與結果，不用舊結果替代當前驗收。

起點 HEAD：`9d8e31bc604e5d1593a60aa0ee012a933a232125`，分支 `feature/rust-refactor-p5`。受測為此 HEAD 加本項測試差異，測試檔 SHA-256 見 `commands.json`；這是局部驗收，沒有執行出口證據 driver，不代表 T22／Slice 2.1 出口、部署、push 或 PR 完成。

裁決依據為本機已存在的 `origin/docs/parity-amendments`，固定於 `3854d4534c62d6d7cc85add2adadcf1bf3e3f3ef`：`reviews/rulings/p5.md:32`（OD-64）、`:38`（OD-69）、`:45`（本輪無新 High／Medium）、`:46`（receipt 送出前排空，不改契約）。本輪未 fetch／移動其他分支。S1 裁決已唯讀核對，未承接其他分支工作。

## 實作核對

既有 `200c10fd` 已完成產品 barrier。本輪沒有重寫實作，也沒有新增另一套 RxShared 完成介面。原 brief 的 `session_core.rs` 丟棄 `rx_seq` 片段，不能單獨作為缺接線的證據：實際完成 watermark 位於 actor，且在同步處理之後更新。

| 驗收要求 | 產品／測試位置與核對結果 |
|---|---|
| 完成點必須晚於 RX 與同步 capture，不能用 reader append 或 mailbox 出列代替 | `crates/sw-core/src/actor.rs:357` 保留事件 `(epoch, rx_seq)`，`:363` 呼叫 dispatch，`:376` 在 dispatch 返回後才更新 processed；dispatch `:451` 呼叫 `core.on_event` 並執行效果。`session_core.rs:690` → `:2714` → `:3819` 先同步 background add_chunk，再執行 agent log append_flush。 |
| receipt 送出前先完成成功入列 RX | `uart_bridge.rs:440` 建立帶序號 data event，`:463` 只在成功入列後更新 queued watermark；`:647` join writer／reader，`:687` 等 DrainDataBarrier 完成才於 `:716` 送 receipt。`:723` 使用原 drain 預算，有界失敗保留 EvidenceGap，未改成成功。 |
| 對應 bridge 的 RX 視窗與 epoch | `session_manager.rs:1834` 每次開埠建立新 RxShared，`:1981` 以 epoch 存入 opened_rx，`:2009` AdoptBridge 取出同 epoch 視窗；`session_core.rs:2355` adopt 同一 OpenedBridge.rx。`:2015` BeginDrain 保持 safety sender 與既有 worker 移除方式。`session_core::rx_window_resets_per_bridge`（`tests/session_core.rs:1662`）在 crate 全套實跑通過。 |
| capture 關閉位置 | `session_core.rs:645` 只有完成 barrier 後才 detach_captures 並 ack；`:2715` 排除舊 epoch RX；未將 reader `RxShared::append` 視為完成。 |
| OD-64 排空順序與 N_pre 定義 | `mailbox.rs:447` 起依賴排空程式完全未改；兩支依賴 gate 仍要求 N_pre＝backlog、見證數＝backlog＋1、OpFinished 先於 receipt。 |
| Python 基線（靜態核對，沒有啟動 Python daemon） | `sw_core/uart_io.py:1663` 先寫 WAL，再同步呼叫 RX callback；`sw_core/session_manager.py:4455` 同步處理 background capture 與 agent log flush；reader 收尾才產生 drain receipt。 |
| 凍結契約 | `contracts/resource_limits.yaml` 與產品五檔的位元組、SHA-256 均與起點相同，見 `unchanged-product.json`。method／threshold／freeze_status 全未改。 |

## 回歸與先紅證據

起點原有的 `release_receipt_follows_queued_rx_capture_and_wal` 實跑為 1 passed；兩支 OD-64 成功對照逐支執行，各含 20 輪、均 passed。起點已含前置修正，沒有宣稱起點仍紅。起點此檔只有一支具 capture／WAL 排序斷言的 OD-69 成功案例；本輪新增 shutdown 與舊 epoch 隔離，保留 release 原 nodeid，三支皆各 20 輪。

- `drain_release_e2e.rs:90` release、`:98` shutdown：actor 暫停於 BeginDrain，讓真 FakeSerial reader 將八個標記送入 data lane；確認 WAL 已寫，之後加入 128 筆無 reader watermark 的測試 backlog，於 WorkerStopped dispatch 前直接讀 agent log／WAL，要求全部標記已在證據中。原 backlog 界限、capture／WAL 斷言保留並加強。release 另建立 active background capture，收尾後要求 done 且八個標記全保留。
- `:106` 重掛後隔離：先完成 release／attach／READY，再於新 bridge 停止時排入舊 epoch 的 `rx_seq=u64::MAX`，確認它不能代替新 epoch 進度，也不進入新 capture。ActorRig 共用 RX Arc 是既有替身限制；本案例驗證 epoch 與 barrier 隔離，產品每 bridge 的新 RxShared 則由上述接線及既有 `rx_window_resets_per_bridge` 實跑交叉核對。
- 三支每輪均斷言 N_pre＝0、見證數 ≤1、成功回應、無 stderr 證據缺口。見 `rounds.txt`（完整逐輪輸出）；60 輪、480 個標記，全無逾時缺口。背景 capture 另於 release 與重掛 release 共 40 輪斷言。
- `:726`／`:740` OD-64 依賴 gate 各 20 輪，backlog 3～7，原斷言完全保留，僅輸出觀測值。`:539`／`:623` flood gate 的 release／cancel／lease／shutdown 各 20 輪，仍要求 200 筆流動、lane 確實滿載與原界限，未改前提或見證數。

新增回歸的辨識力以本 worktree `target/` 內的 archive 副本驗證：只在副本令 finisher 略過 `wait_for_rx_dispatch`（差異見 `mutation.diff`），模擬 OD-69 前直接送 receipt 的缺陷。`mutation-isolated` exit 101：三條 OD-69 路徑均在 round 0 因 receipt 時尚有 129／130 筆 backlog 失敗；兩條 OD-64 對照仍綠。這是明示的變異試驗，不是偽稱未修 HEAD 的歷史紅燈；主工作樹產品從未改動。修正前／後的成功條件相同，沒有放寬斷言。

初次變異試驗誤共用 Cargo target，使 `drain-release`／`mailbox` 後續取到變異建置；這兩次結果作廢，沒有拿來歸因產品或算通過。清除本 worktree 相關快取、分開 target 後，`drain-release-clean` 15 passed、`mailbox-clean` 22 passed；隔離的 `mutation-isolated` 仍 3 failed。另兩次測試撰寫中錯誤（錯誤函式名稱、假定不存在的探針標籤）均記錄為測試撰寫失敗，不作產品紅燈證據；探針實際把 barrier 標為 Control，正式斷言核對精確增量。

## 前輪 High／Medium 處置

依固定裁決與 `p5-cloud/cc25b01_8c0c942-sol.md` 的 A 段逐列核對以下 28 條；B 段僅 Low，不修。此表記錄既有修正狀態，未聲稱本輪另做全套獨立複審。未提供／未發現本項新的 High／Medium。

| ID（前審原編號） | 嚴重度 | 處置 |
|---|---|---|
| 1 | Medium | already fixed `25ad390` |
| 2 | High | already fixed `25ad390` |
| 3 | Medium | already fixed `25ad390` |
| 4 | Medium | already fixed `25ad390` |
| 5 | Medium | already fixed `25ad390` |
| 6 | High | already fixed `25ad390` |
| 7 | Medium | already fixed `25ad390` |
| 8 | Medium | already fixed `25ad390` |
| P5-1393573-1 | High | already fixed `8134806` |
| P5-1393573-2 | Medium | already fixed `d8ac988` |
| P5-5cc4623-1 | High | already fixed `0a7d0929` |
| P5-5cc4623-2 | Medium | 依裁決不處理 |
| P5-70f6ce7-1 | High | already fixed `8134806` |
| P5-3c4e051-1 | Medium | already fixed `cca5ba8` |
| P5-3c4e051-2 | Medium | already fixed `cca5ba8` |
| P5-3084388-1 | High | already fixed `2472f78` |
| P5-3084388-2 | Medium | already fixed `289f7573` |
| P5-3084388-3 | Medium | already fixed `464adec` |
| P5-2472f78-1 | Medium | already fixed `c4cfb96` |
| P5-4f8acdf-1 | High | already fixed `86b54d8` |
| P5-4f8acdf-2 | Medium | already fixed `86b54d8` |
| P5-4f8acdf-3 | Medium | already fixed `86b54d8` |
| P5-86b54d8-1 | Medium | already fixed `b9ae1d2` |
| P5-86b54d8-2 | Medium | already fixed `b9ae1d2` |
| P5-b9ae1d2-1 | Medium | already fixed `1b83da9d` |
| P5-5d5314e-1 | High | already fixed `1b83da9d` |
| P5-54dd588-1 | Medium | already fixed `8d824b3a` |
| P5-54dd588-2 | Medium | already fixed `cc25b01d` |

## 不成立主張

無。已裁決延後的項目維持延後，未自行略過未決 High／Medium。

## 測試、失敗與可比較基準

每個命令的 HEAD、argv、exit code、時間、摘要與原始 log SHA-256 記於 `commands.json`；`*.txt` 是原輸出的路徑去識別化副本（`$WORKTREE`／`$HOME`；僅移除行尾空白及 EOF 多餘空行），另保留副本 SHA-256。沒有把 FAIL／INSUFFICIENT 變成 PASS。所有測試皆在前景依序執行並等結束；靜態檢查的實際起訖照錄。

第一次 workspace 測試因 TMPDIR 位於 `target/t`，daemon_process 的 socket 路徑達 115 bytes，超過 SUN_LEN，33 支中 23 支失敗。以 9d8e31bc archive（866 個 Cargo／crates／contracts Git blob 逐檔相等，見 `baseline-source.json`），同環境同範圍重跑，失敗集合完全相同，逐支 id 見 `path-failure-ids.txt`。改用 worktree 根目錄作 TMPDIR（socket 106 bytes），同組 33 passed。這是同程式不同暫存路徑的對照；原失敗保留，沒有將它歸因為 flaky 或加豁免。workspace 因此重跑。

正式驗收摘要如下；完整 argv（含 TMPDIR／CARGO_TARGET_DIR）見 commands.json。測試程式在最後一次 Rust 實跑後未再變動。

| 命令／紀錄名稱 | exit | 實際摘要 |
|---|---:|---|
| 原 release（baseline-release） | 0 | 1 passed；起點已綠 |
| 原 OD-64 release／shutdown（baseline-od64-*） | 0／0 | 各 1 passed，每支內含 20 輪 |
| `cargo test -p sw-core --features test-fakes --test drain_release_e2e -- --nocapture --test-threads=1`（drain-release-clean） | 0 | 15 passed；OD-69 60 輪、OD-64 40 輪、四段 flood 共 80 輪 |
| `cargo test -p sw-core --features test-fakes --test mailbox -- --test-threads=1`（mailbox-clean） | 0 | 22 passed |
| `cargo test -p sw-core --features test-fakes -- --skip cap_`（sw-core） | 0 | 560 passed，0 failed／ignored／filtered |
| `cargo test --workspace --features test-fakes -- --skip cap_`（workspace-short） | 0 | 1790 passed，0 failed，4 ignored，26 filtered；未跑能力列 |
| `cargo check --workspace --target x86_64-pc-windows-gnu` | 0 | 完成；sw-pal 3 個既有 unused／dead_code 警告，沒有原生 Windows 實跑 |
| `python3 scripts/rust/check_cfg_boundary.py` | 0 | 無輸出、無違規 |
| `python3 -m tools.contracts check` | 0 | ok=true，findings=[] |
| `python3 -m policy_check --repo .` | 0 | 25 pass、0 fail、2 warn（R-19／R-22）；本地非 PR context |
| `python3 tests/acceptance/regen_lock.py --check` | 0 | cases／L1 鎖結構通過 |
| 同上加 `--base 9d8e31bc604e5d1593a60aa0ee012a933a232125` | 0 | 相對本輪起點僅新增三列，無刪列／改 selector |
| 同上加 `--base 8c0c942a5a3a0a3771cb1f9ec53159acc76bd4fd`（lock-check-8c） | 1 | **保留 FAIL**：起點已依 OD-68 移除的三列未傳核准參數 |
| 同上加 `--base 8c0c942…` 與三個 OD-68 `--allow-removal`（lock-check-8c-od68） | 0 | 僅套裁決逐名核准三列，沒有寫鎖或新增移除 |

凍結鎖由 regen_lock 唯一寫入，`s21_core.linux` 1739 → 1742：新增本項三列、移除 0，其他集合與所有 selector 相同。實際收集 1820（ignored 29）；依本項範圍匯出既有必要列加三列的實際交集供 regen_lock，不登記其他子項新列。詳見 registration.json。舊 OD-68 三列的核准參數全部列在 commands.json，對照基準的原命令失敗仍保留。

原始 UTC 時鐘在 lock-check 附近曾回撥，時間戳保留原值；本項沒有以這些時間戳量測 event_latency 或更動任何凍結方法。

## SPEC-GAPS／缺測

1. brief 同時包含一般收尾「全套 pytest 一次」與專項編排者註「不要跑全套 pytest、slice gate、Docker（另派）」；本輪依專項限制不執行這三類及出口 driver。且 `tests/conftest.py:31`～`:41` 會讀取 production state／WAL／config 並探測 production daemon，與本輪嚴格邊界衝突；未修改／停用 guard 來製造通過。本輪沒有改 Python 測試或產品 Python。
2. 未執行 Windows 原生／com0com 或能力測試；cross cargo check 只代表可編譯，不能替代原生證據。Windows 原生缺測維持 LOCAL-WINDOWS-TODO；擁有者／硬體工作維持 BLOCKED-OWNER。
3. 起點已修正，無法交付「當前起點原 a1 兩支皆紅」的實際結果；已交付起點實跑與明示變異紅燈，未虛構先紅歷史。

## 既有碼待辦

長絕對 TMPDIR 會使既有 daemon_process 隔離 socket 超過 SUN_LEN；本輪以工作目錄內短入口完成測試，不修改範圍外 harness。`crates/sw-pal/tests/proc_scan.rs:528` 的 chmod 000 fd 夾具會留下無法由 TempDir 移除的空目錄；本輪只還原該次自建目錄權限後清除，未修改範圍外測試。未修 Low 或其他 r8／S1 分支工作。

REMAINING: 全套 pytest、Windows 原生／能力與出口證據由另派或擁有者執行；不宣稱本項局部驗收已替代這些門檻。
