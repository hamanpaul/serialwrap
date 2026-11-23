> 本檔是編排者本機 Stage 3 計畫草稿 r2 的副本（2026-11-23），經 codex astra 對抗審查兩輪收斂。
> 擁有者已裁決 §7 A 組與 Canary，記為 OD-113（`docs/rust-refactor/reviews/rulings/s3.md`）。OD-113 推翻了本檔的部分建議，例如長跑與 Canary 的地位；兩者衝突時，以 OD-113 為準。

# Stage 3 計畫（1.0.0：A/B、Canary、回滾演練、Python daemon 退役）

- 日期：2026-11-23；版本：**r2（依 astra 第 1 輪審查修訂）**；狀態：**修訂版草稿，待擁有者裁決 §7**。基準：origin/main e50d4b3b；裁決分支 origin/docs/parity-amendments（下稱 PA）。
  - reader 盤點時 PA 的 tip 是 710140dd。之後 RUNNING-STATUS 又記有 62a522bf、a63907a8 等 rulings commit〔機〕。
- 產出方式：
  - 草稿：Stage 3 規劃 workflow 彙整五份唯讀盤點（p9＝P9 計畫、spec＝規格與總計畫、impl＝實作現況、od＝擁有者裁決、bl＝待辦與資源）。
  - 定稿：經三位審查者（完整性、可行性、證據）審過，定稿者逐項判斷並修訂，處理結果見文末「審查處理紀錄」。
  - 本檔只做規劃，沒有改 repo。
- r2 核對基準：`M=e50d4b3b03b2dd7f7328e05e7e70f88f4b5e0281`、`P=2f19aba79d98afb7b9fd0e74046952f215a88c74`、`PA=71ed5b44557ff3415c57bedd58ab7ee5f0fd0107`。以下 `M:路徑:行號`／`P:路徑:行號` 都指固定物件，`P10`、`spec` 固定於 M。裁決另以使用者指定的 `origin/docs/parity-amendments` 讀回；本輪 ref 為 `b45a4ee5eb463a7e23b9f2b66ef3293b16a4f99f`（下稱 PA2），s2 全文與 PA 相同，v051 的 OD-111／112 段相同，後段有新增裁決。r1 的本機盤點不當作本輪已重驗的現況。
- 來源標記：
  - 〔本〕：草稿彙整時親自查證。
  - 〔定〕：定稿時親自重新查證（origin/main 或 PA 的實際內容）。
  - 〔審:完〕〔審:可〕〔審:證〕：分別由完整性、可行性、證據審查者查證，定稿者沒有逐行重驗。
  - 〔讀:鍵〕：由該 reader 查證，所引檔案行號在同一處。
  - 〔推〕：推論。
  - 〔機〕：本機非 repo 來源，例如 ledger-inbox、RUNNING-STATUS、v051/PLAN.md、記憶。只能當線索。
  - 〔未〕：未查證。
  - 〔修2〕：本輪以固定 Git 物件或指定裁決 ref 親自核對；只做靜態閱讀與集合重算，沒有實跑產品或測試。
- 行號簡寫：
  - `P10:n`＝origin/main `docs/rust-refactor/plans/10-stage3-canary.md` 第 n 行（共 3890 行〔本〕）。
  - `spec:n`＝`serialwrap-rust-spec-final.md`。
  - `s2:n`／`v051:n`＝PA 的 `reviews/rulings/s2.md`／`v051.md`。
  - `RS:n`＝本機 `exec/RUNNING-STATUS.md`〔機〕。
- 審查編號：C1～C15（完整性）、F1～F15（可行性）、E1～E14（證據），內文以「（C1）」標示修訂來源。

---

## 1. 結論摘要

1. **Stage 3 只有「工具與計畫」這一半可以開工。**
   - **現在能動的**：S3-M0（裁決與 plan 10 改版）。M1～M3 是工具移植及 runner 整合；M0 收斂後可開始，原生驗收仍需另外排程。
   - **現在不能開始的**：所有產生最後 RC 正式 Stage 3 證據的活動，包括 A/B、Canary、回滾演練、L2／L3 RC 集合。
   - **證據線的開始條件**：0.5.1 發布、工具與環境缺口完成、兩組實機彩排（S3-M4R）依 owner 選定深度完成（縮短方案仍欠完整正向 gate）、最後退役形狀 RC 建好且 target 凍結，全部達成之後才行。彩排使用隔離索引，不得當作正式證據。
2. **最緊的約束是證據身分，而且比草稿寫的更嚴（C1、F4）。**
   - **target 凍結的欄位**：`artifact_id`、`runtime_digest`、`contract_revision`、`tool_revision`（＝`git rev-parse HEAD`）、`case_list_sha256`、bundle／runtime tree（P10:40、1467、2817〔定〕）。
   - **L1 項目另一條件**：L1 項目還要求 `l1_set_sha256` 等於以**目前**鎖檔重算的值（P10:2847〔定〕）。
   - **任一欄位不符的後果**：ingest 拒收（P10:3100〔定〕），已登錄的證據判 STALE。
   - **同一 artifact 的 FAIL**：一旦登錄就無法抵銷（P10:2848〔定〕）。
   - **結論**：
     - 所有證據都必須從固定在 target `tool_revision` 的專用 worktree 產生。
     - 凍結後只要修一次工具、案例清單或 L1 集合，就要重設 target 並全部重跑。
     - 0.5.0 的長跑與 0.5.x 上累計的使用量，都不能直接算進 1.0.0〔推〕。
3. **最大缺口一：在現有設備與分母規則下，部分 S3 項目不可能判為 SATISFIED（F5、F6、C8、C10）。**
   - **分母規則**：Stage 3 的分母是 `slice_2.4 ∩ layer ∩ platform`，沒有依 `windows_status` 排除 gap（P10:259、882〔定〕）。
   - **Windows L3 realhw 的分母**：37 案，其中 gap 19 案、需要 `two_boards` 28 案；Windows 只有單板 OPI〔定〕。
   - **remote 集合**：5 支需要 docker，3 支需要 docker、兩板與 remote 能力〔定〕。
   - **Windows 沒有 TestPilot**：無法執行 L2 regression 34 案，另有 1 案 windows-native 必須獨立承接〔修2：M:cases.yaml＋cases.lock.json 重算〕（od27 windows/report.md:116〔定〕）。
   - **windows-native 案例沒有承接證據**：L1.5 有 14 案、L2 有 1 案、L3 有 4 案，P9 全文沒有提到（P10 grep 為 0〔定〕）。
   - **Linux 也有 runner 缺口**：L1.5 共 85 案＝13 func-test＋15 l15-scenario＋57 contract-blackbox；Windows L1.5 共 50 案＝13＋15＋14 windows-native＋7 contract-blackbox＋1 rust-l1。現有 func-test adapter 無法承接後三類（S3A-5）。
   - **處理期限**：M0 逐案盤點、裁決拓樸與凍結負載（A4／B16）；RC 前完成 runner 與設備驗收（A7）。保留完整分母；排除只能作為待核准的新 OD，未授權或缺能力就是阻擋。
4. **最大缺口二：P9 工具不在 main。**
   - main 沒有 `tools/stage3/`、`stage_exits.json`、`packaging/python_daemon_retirement.json`〔讀:impl〕，也沒有 `s3_release_contract` L1 集合〔定〕。
   - 實作都在未合併的 `origin/feature/rust-refactor-p9a`，tip 為 2f19aba7〔定〕，落後 main 約 1514 個 commit〔讀:impl〕。
   - Task 1 的基礎模組與契約檔只是 worktree 的未追蹤檔（§2 T1）。
5. **最大缺口三：plan 10 的前提已過期（§3），且計畫本身有幾處和現行發布機制互斥。**
   - **retirement verify 的時機**：verify 與 verify-archive 在 `retired:false` 時拒絕（P10:3323、3336、3744〔定〕），所以不能接在 RC 建置。
   - **pack_bundle 與現行打包互斥**：pack_bundle 不容許 symlink 和外層目錄（P10:3310-3311〔定〕），但現行 release 的 tar 兩者都有（release.yml:414-416〔定〕）。
   - **promote guard 的限制**：RC 之後只放行 `docs/`、`changelog.d/`、`CHANGELOG.md`、breaker 快照與 behavior_changes 的 status 欄位（release_promote_guard.py:52-58〔定〕）。因此 install.sh、README.md、tools/、repo 根目錄 `evidence/` 的變更都必須排在 RC 之前。
6. **時程比草稿估得長（F1～F3）。**
   - 單一保底工具線 M1～M4 改估 **9～14 週**；另加 S3-E 必要遺留清冊 **1～2 週**、新增環境／拓樸負載準備 M0c **2～4 週**，OD-83 B 補做另列 **1～2 週**，不得因放進 0.5.1 就扣掉工期。
   - M4R 改為兩組；若完整演完退休發布正向 gate，合計 **5.5～10.5 週**；M5 的 4～8 週僅為配額量測可行、A4(c) 與兩板負載方案核准後的暫估。
   - **從 0.5.1 發布算起**：尚餘前置工作完成後，再需 **11～21 週**；完整計算及不確定項見 §5。
7. **已盤點實機據點只有兩處，尚未滿足完整分母。**
   - **Windows OPI COM6**：只有單板，而且和 thala 的 USB SSD 共用 root hub。
   - **兩板環境**：只有遠端 a250113（BRCM COM0／COM1），它的正式 service 還沒還原〔讀:bl〕。
8. **建議第一步：**
   1. **立刻保全 p9a worktree（今天）**，範圍見 §9（E10）：
      - 20 筆未追蹤項（展開為 23 個檔）〔定〕。
      - 16 個已修改的已追蹤檔，含 plan 10 與 `l1_required.lock.json`〔本〕。
      - 被 gitignore 的 `.superpowers/sdd/10-stage3-canary/progress.md`〔定〕。
      - 由編排者 commit 到 p9a 分支，或封存到 b-log。本檔依限制不執行。
   2. **在 2026-11-23～11-29 之間補一筆 Slice 2.4 斷路器快照**（§4.3）：
      - 要實跑 2.4 凍結集合，至少 56／187 案通過，才不會觸發斷路器。
      - 要排 heavy-slot，並事先通知 thala。
   3. **擁有者裁決 §7 A 組 8 項，並提前裁決 B16、B4、B5 與 B3 的版本歸屬**。先定 Stage 3 時長／拓樸／逐板負載與 OD-102／109 關係，再產出完整逐案映射；不得等 M2 的 SKIP／FAIL 代替裁決。
   4. **開 S3-M0a／S3-E／M0b**：plan 10 改版，用三方比對做基準（E8），交獨立審查；納入 §4.4 六項整合契約、兩組彩排及工期。裁決後開 M0c 準備第二板／remote／負載，A6 保底產能核准後才採用 §5 週數。
   5. 同時由編排者唯讀統計本機正式 daemon 的使用量（B2），並指定 Windows Canary 工作站（B2、F8）。
9. **有時限的 Stage 2 收尾（不由 S3 16 項直接檢查，必要義務仍交 B5 核定）**：
   - Slice 2.4 斷路器的窗口在 **2026-11-29** 結束〔定〕。
   - main 上只有 11-14 一筆快照，rate 0.198〔定〕。
   - OD-87 要求「之後每週一筆」，已漏一週（s2:393〔定〕）。

---

## 2. 現況對照（P9 共 20 個 Task）

main 上這 20 個 Task 都是「未開始」：沒有 `tools/stage3/`、`tests/test_stage3_*`、`stage3_fakes.py`，逐檔以 git cat-file 查過〔讀:impl〕。下表的「p9a」欄是未合併分支的狀態。

| Task | 計畫要求（P10） | main | p9a（commit；審查） | 依賴 | 阻擋／需修訂 |
|---|---|---|---|---|---|
| T1 契約與基礎模組 | 28 個原因碼、4 個 schema、stage_exits 67 項（其中 S3 16 項）、draft canary policy、空索引、retired:false 開關、`s3_release_contract` L1 集合（:103-648、:121） | 未開始；lock 沒有 `s3_release_contract`〔定〕 | **部分**。已提交的只有 canary_policy.py（d072ca40）與 stage3_reason_codes.json（19341ae8）。jsonio／proc／reason_codes、stage_exits.json、schema、evidence/、retirement json、test_stage3_{base,contracts,consumes}.py 都是**未追蹤檔**〔本〕 | C01～C11 | BO-P9-06；manifest 形狀與 stage 字串要依現況改（§3）；`s3_release_contract` 兩平台都不得為空（P10:476-484〔定〕），Windows 那一半要原生收集（C9） |
| T2 共用 fakes | stage3_fakes 12 項（:652-770） | 未開始 | 149e420d、19341ae8；p9a 紀錄為 sol 已補審，main 紀錄為「待補審」〔定〕 | T1 | T1 未提交 |
| T3 A/B manifest | 載入、凍結、案例涵蓋、tree 指紋（:774-1012） | 未開始 | 60756486＋20915da4；sol 已補審（p9a 紀錄） | T1、T2、C06 | 無實機 |
| T4 釋放驗證 | Posix／Windows probe，unknown 不算已釋放（:1016-1223） | 未開始 | abe7fb32；無審查紀錄（計畫要求單審） | T3 | BO-P9-15；Windows 原生 pytest 要經 interop |
| T5 DUT 狀態隔離 | fresh instance、verify_clean（:1227-1308） | 未開始 | b7b4fdd5；無審查紀錄 | T3 | BO-P9-02／03 的 reset 與 fingerprint 指令 |
| T5B Suite adapter | L1.5／L2／L3 adapter、身分核對（:1312-1391） | 未開始 | 19341ae8；無審查紀錄（單審）。「需重做」註記已過期〔讀:impl〕 | T3、T4、T5、C08～C10 | L3 程序要改成實際拓樸（A4）；**缺 Linux 57 案 contract-blackbox 及 Windows 非 func-test runner 整合**（S3A-5；逐案表見 §4.1-5）；BO-P9-01～05、18 |
| T6 A/B 執行器 | 九步執行序，加一次 Linux L1.5 實跑（:1395-1568） | 未開始 | 0b0645e0；無審查紀錄（單審） | T3～T5B、T7、T7B、C05 | `diff_l15` 的參數和 T7B 不一致（:1464 對 :1594）；BO-P9-16；報告的 `tool_revision` 取 HEAD（:1467〔定〕） |
| T7B L1.5 差分 | 正規化與差異歸屬，ab_normalization 只能追加（:1572-1625） | 未開始 | 24f42d0a＋4de1c364：sol 已補審。**2f19aba7**（10-23 17:42，foreign_holders 依情境區分嚴格與易變）**已實作易變規則，但沒有審查紀錄**〔定〕（E4） | C01、C08 | 追加鍵會改 contract_revision；B11 |
| T7 A/B 判定 | combine 表、L2／L3 比較（:1629-1735） | 未開始 | d6c08e06＋99f46066；sol 已補審（p9a 紀錄） | C01、T7B、C04、C06、C09 | 無 |
| T8 Canary 收割 | 32 hex cmd_id 計數，處理 rotate、截斷（:1739-1930） | 未開始 | 5e44e425；無審查紀錄（單審） | T1 | 計數規則引用的 Python 行號要改對 Rust 核對〔讀:p9〕 |
| T9 collector zipapp | 唯讀命令、pyz（:1934-2104） | 未開始 | e246556e；無審查紀錄 | T1、T8、T10、T11 | BO-P9-07／08；Windows 端要 Python 3.10+ |
| T10 分類與活躍日 | 循環分類、steady 判定（:2108-2276） | 未開始 | f10db782；無審查紀錄（單審） | T1 | BO-P9-06；session list 欄位要對 Rust 核對 |
| T11 事故 | 12 種事故、結案規則（:2280-2406） | 未開始 | 345450b8，worktree 另有**未提交修改**〔本〕 | T1、T10 | BO-P9-09 |
| T12 帳本與 gate | freeze_policy，五值結果（:2410-2604） | 未開始 | d072ca40；無審查紀錄 | T1、T8、T10、T11 | BO-P9-06～09；carryover 只帶 submits／cycles（:374〔定〕） |
| T13 回滾演練 | D0～D8，D7 與 D8 都跑才算 PASS（:2608-2744） | 未開始 | 511687d6；無審查紀錄（單審） | C12、T2、T3、T4 | BO-P9-10／11；`--expect-previous` 與 D8 的 `--start-fail-from` bundle（:3734〔定〕；B14） |
| T14 證據索引 | item_status 與 stage_status（:2748-3002） | 未開始 | 5a038a14＋1a50be1f、db8eb369、c64e23e1；sol 已補審（p9a 紀錄） | T1、T3、C06、C07 | BO-P9-14 範圍待裁（§4.2）；預設 A5(b)；若另核准 A5(a)，須以新 OD 及 FAIL 優先反例驗收後才可新增 VOID |
| T14B 證據 ingest | 11 種 kind，唯一的 PASS 入口（:3006-3153） | 未開始 | 95ea1341；無審查紀錄（單審） | T14、T15、T6、T12、T13 | 測試直接用 T12／T16，與 §0.1 順序（:73）衝突 |
| T15 OD-09 消費 | 唯讀 check（:3157-3266） | 未開始 | cbf40e9d；sol 已補審（只在 p9a 紀錄，main 紀錄沒有 T15〔審:證〕） | C05、T1、T14 | 既有 Stage 2 凍結已完成，Stage 3 拓樸／逐板負載仍待 B16；`longrun_acceptance.yaml` 與 S3-L3-AB-48H-* 名稱不得改（A4） |
| T16 退役切換 | retirement.py、Rust 守門、mkbundle、release.yml（:3270-3703） | 未開始 | 24d415ff、681bb688；無審查紀錄（單審）。**24d415ff 已自建 `tools/stage3/__main__.py` 骨架（46 行）**，T17 的 eecd4882 再擴充〔定〕。release_manifest.rs、launcher.rs、daemon_cmd.rs、delegate.rs、mkbundle.sh、release.yml 在 main 之後都改過 | T3、T14、C13、C14 | BO-P9-13；BO-P9-17 擬撤（A3）；要改接 RC／promote、hosted Windows、mkbundle-windows.ps1；pack_bundle 與現行佈局互斥（A3） |
| T17 入口與程序文件 | CLI 加 6 份 procedures（:3707-3853） | 未開始 | eecd4882 | 全部 | runbook 要依新裁決重寫；撤 BO-P9-17 時 `test_runbook_marks_every_blocked_owner_item` 與 `test_runbook_order` 要一起修（P10:3812-3822〔定〕） |

補充：
- **S3 出口的 16 項（P10:332-347〔定〕）都還沒有證據。**
  - OD-109 已於 2026-11-21 凍結且早於 S24 候選〔讀:spec、impl〕，但這只證明既有 Stage 2 凍結時序；Stage 3 的 two_boards／第二板負載尚未合法承接前，不能稱 S3-OD09-ALL-FROZEN 的前置全部就緒（S3A-8）。
- **P9 Phase 完成不等於 Stage 3 出口。**
  - Phase 完成條件是 20 個 Task 合併、全套測試通過、Linux L1.5 A/B 實跑一次，且 `evidence status --stage 3` 預期為 BLOCKED（P10:3857-3861〔讀:p9〕）。
- **審查覆蓋不足（C11、E5）。**
  - 計畫要求單審的高風險 Task 有 9 項：4、5B、6、7B、8、10、13、14B、16（P10:74 一帶的收尾列〔定〕）。其中只有 7B 有 sol 補審，而且不含 2f19aba7；其餘 8 項都沒有紀錄〔定〕。
  - p9a 與 main 的 `pending-sol-review.md` 有 5 項不一致：T2、T3、T7B、T7、T14 在 main 寫「待補審」〔定〕。main 也沒有 T15 那一列〔審:證〕。
  - Phase 層級雙審還沒做。

---

## 3. P9 寫作後改變的前提，以及 P9 要修訂的地方

| # | 前提 | P9 寫法 | 現況 | P9 修訂建議 |
|---|---|---|---|---|
| 1 | 長跑時長 | 48h（P10:26、85-86；spec:1064、1187） | OD-94 改為各 24h。yaml 為 `min_continuous_duration_s: 86400`（**下限**）、`allow_segment_stitching: false`（longrun_acceptance.yaml:15-16〔定〕）。該檔 sha256 e647d908… 等於 od09-freeze.json 的 `acceptance_sha256`〔定〕 | **不得改 yaml，也不得改 S3-L3-AB-48H-* 的 item_id**，因為 od09_freeze.py:53-59 寫死這些名稱，:778-779 遇到未知項會 raise〔定〕（C5）。Stage 3 時長尚須 A4 新 OD；不得直接套用 Stage 2 的 OD-94／102。維持凍結檔與 item_id，title／rulings／spec 只在裁決後同步；拓樸負載依 B16 收斂 |
| 2 | 拓樸與排程 | 同機同板經 usbipd，依序執行（P10:26） | **OD-94**：兩平台**同時**跑（s2:455〔定〕）。**OD-102**：在 OPI 上錯開四輪；修訂後 Linux 經 localhost TCP 橋接，期間停 WSL 正式 service（s2:540-554〔定〕）。**S24 候選的實際做法**：兩平台在不同硬體上**並行**，兩筆候選開跑登記都是 2026-11-21T10:46Z（candidate_longrun_starts.ndjson〔定〕）；Windows 在 OPI，Linux 在 a250113 的兩塊 BRCM 板（evidence/stage2/longrun/README.md:7〔定〕）。換板與並行都**沒有書面裁決**（E3） | 改寫 Task 5B 與 runbook；M0 取得 Stage 3 專屬裁決，寫明 OD-102 的單板／錯開適用範圍，以及 B16 的兩板負載如何在不改原凍結檔與歷史下承接 OD-109；僅追認並行或用板不足 |
| 3 | 發布流程 | Task 16 自建 self-hosted 發布 job：verify→pack→verify-archive→上傳（P10:3371-3375） | OD-104／106 改為 RC→promote 同一份資產。release_promote_guard 規定 RC 之後只放行 docs/、changelog.d/、CHANGELOG.md、breaker 快照與 behavior_changes 的 status 變更（release_promote_guard.py:52-58〔定〕）。另有三個限制：verify 與 verify-archive 在 `retired:false` 時拒絕（P10:3323、3336、3744〔定〕）；release 模式要 `evidence_root` 與兩平台 bundle 目錄（:3321、:3375〔定〕）；pack_bundle 不容許 symlink 和外層目錄，現行 tar 兩者都有（release.yml:414-416〔定〕；C4） | 拆成兩段（§7 A3）：**RC 建置**只做 Stage 3 形狀的 bundle 加 `retirement pack`（pack 與佈局要先調和）；**promote** 時才把 retired 設為 true，並對 RC 已上傳的 archive 跑 `verify --mode release` 與 `verify-archive`。guard 的必要變更依 A3 逐路徑收斂，不預稱只需一檔；候選帳本先按 §4.4-2 分離，不把 realhw 執行增量提交進發布分支。另須改造 release_assets.py 的 stage／佈局／資產白名單、RC 建置上傳入口與安裝指南（S3A-6）；A8 須讓 hosted job 取得完整必要證據 |
| 4 | Windows 正式 bundle | 自架 runner 原生建置（BO-P9-17，P10:98） | OD-78 改由 hosted windows-latest 建置（s2:15-17〔定〕）；Windows 用 mkbundle-windows.ps1 | 撤掉 BO-P9-17，並同步修 T17 的兩支 runbook 測試（P10:3812-3822〔定〕），runbook 改標為「OD-78 的 Windows 原生驗收」（C12）；Task 16 的 Files 加入 mkbundle-windows.ps1 |
| 5 | mkbundle 與 manifest | 「目前沒有 --stage」（P10:61）；Stage 2 manifest 只有 launcher（P10:50） | 已有 `--stage 1\|2`（mkbundle.sh:8、14、121），manifest 的 stage 值為 "rust-2" 等。Stage 2 manifest 仍保留 py runtime、daemon.entry、package、python_cli_delegate（mkbundle.sh:594-656）〔讀:p9、impl〕 | 擴充為 `--stage 3` 並新增 `--plan-only`，或另取旗標名稱以免語意衝突；重寫 C02／C14 的 Consumes 說明 |
| 6 | OD-09 延後項 | 可能仍為 deferred（P10:21、93） | OD-109 已於 2026-11-21 凍結（revision 2026-11-21.1）；OD-103、OD-107 修訂了計數與缺口容差〔讀:spec、od〕 | BO-P9-12 的 S0 部分改為已完成；Stage 3 新拓樸仍等 B16，L3 A/B 必須透過同一可稽核帳本做 check_frozen_before 與 register_candidate_start |
| 7 | 人工接受 | PASS 只能由 ingest 從工具報告推導（P10:40） | OD-110：Windows 候選的工具判定是 FAIL，擁有者排除擾動區間後人工接受（addenda:127〔定〕）。同一 artifact 有 FAIL 就固定為 FAIL（P10:2848〔定〕） | 第一筆 Stage 3 證據之前，要先定好擾動的程序性處理（§7 A5） |
| 8 | Python 基線 | 未指定版本 | OD-111 凍結 sw_core，只改 Rust，差異都要登錄 BC（v051:3-10）。OD-102 的長跑基線用 0.4.1〔讀:od〕 | B4 只選 daemon／runtime 基線來源；另建「共用候選 CLI＋凍結 Python daemon」A 包並驗版本配對，禁止直接用舊整包配新 CLI（S3A-3） |
| 9 | 0.5.x 仍在變動 | Canary 從 0.5.x 起算（P10:28） | 0.5.1 有三條線在跑（線 A、B，以及 10:53 起的線 C ci-thin，之後接 i304）。OD-112 把必修 9 項加候選 3 項全部納入（v051:36-54〔定〕）〔機〕 | Stage 3 target 必須在 0.5.1 之後凍結；Canary 從 Stage 3 候選起算 |
| 10 | Windows 崩潰重啟 | windows-task 會在失敗後自動重啟（OD-03） | OD-83 A：0.5.0 不提供崩潰自動重啟；長期方案 B 沒有排進 0.5.1（s2:149-155）。slice_2.4 的 `win-task-restart-on-failure`（L3、windows native）在 A 之下不成立〔定〕（F6） | Windows Canary 與 win-task 案例開跑前要先裁決（§7 B3） |
| 11 | 真板 | OD-27 在 release 後由其他電腦執行 | 執行地點改過三次。目前只有 OPI 能跑 reboot 與 U-Boot（OD-92）；MTK 板只能讀（OD-94）；實體拔插仍為 BLOCKED-OWNER〔讀:od、bl〕 | 重新指定兩平台 L2／L3／演練的用板（§7 B16、B7） |
| 12 | 產品事實的出處 | 計數、分類、回滾規則引用的都是 sw_core 的 Python 行號 | 退役後受測的是 Rust daemon。ft- 前綴與 CMD_HISTORY_MAX 已在 Rust 找到對應，其餘還沒核對〔讀:p9〕 | 移植時逐條對 Rust 核對：cmd_id 格式、boot_quiet／reconfirm 欄位、bridge_generation |
| 13 | 模型分工 | agy、haiku、Sonnet＋gpt-6.1-sol（P10:74、3863-3890）。P10 §2 已把 T4、5B、6、8、9、10、13、14B、16 的實作者指定為 codex-gpt-6-luna-max〔審:證〕 | 現行為 luna max 實作；審查自 2026-11-23 11:08 起改為 codex gpt-6.1-terra xhigh（REVIEWER_OVERRIDE=terra；RS:667）〔機〕（F13、E9） | 依現行路由重新指派（§7 B13） |
| 14 | 裁決文件位置 | 都在 main | 分歧是雙向的。OD-77～108、111、112 與編排者裁決只在 PA；OD-109、OD-110 只在 main 的 addenda:126-127，PA 沒有〔定〕。PA 落後 main 1424 個 commit〔定〕（E11） | brief 同時列出 PA rulings、main 的 `owner-decisions-addenda.md` 與 `evidence/stage2/longrun/`；或在 M0 先做雙向合流，再改成單一路徑 |
| 15 | 磁碟、CPU、額度 | 沒有考慮 | E 槽上限 50G、D 槽要 ≥120G、heavy-slot 每個 session 2 格、codex 週額度（§6）〔讀:bl〕〔機〕 | 工具線以純 Python 為主；Rust（T16）另排時段 |
| 16 | 計畫內部 | — | 依賴順序衝突：T14B 用到 T12／T16；T16 要 T17 的 `__main__`。介面也不一致：T6 與 T7B〔讀:p9〕。p9a 的解法是由 T16 先建 `__main__` 骨架，T17 再擴充〔定〕（E14） | 照 p9a 先例寫成「T16 建骨架、T17 擴充」，T17 的順序不必調整；T14B 拆成三段 |
| 17 | **證據身分** | target 凍結 tool_revision＝HEAD、case_list、L1 集合（P10:40、1467、2817、2847、3100〔定〕） | 草稿沒寫；0.5.0 長跑期間工具就改過兩次（ac34ba82→ef484d37、OD-107；RS:566、585）〔機〕（C1、F4） | 證據從凍結 worktree 產生；工具、案例與鎖要在 target 之前凍結（§4.2）；加實機彩排（S3-M4R） |
| 18 | **Stage 3 分母** | `required_case_set` 必須是 slice_2.4，分母為 set ∩ layer ∩ platform（P10:259、882〔定〕）；任一必要案例 SKIP → INCOMPLETE／INSUFFICIENT（P10:1682、3090〔定〕） | 不依 windows_status 排除 gap。Stage 0 的 check_evidence_index.py:398-407 只收 native／ported〔定〕。windows-native suite 共 19 案，沒有 adapter 承接〔定〕（F5、F6） | §7 A7 裁決；分母一改，case_list_sha256 跟著變，所以必須早於 target |
| 19 | **docker 前置** | 每個 RC 在 L2／L3 A/B 之前，要先以 docker 兩容器拓樸跑 L1.5，標為 `L1.5-docker`，不計實機（P10:25；00-master-plan:93〔定〕） | 草稿漏列；main 已有 `scripts/docker/run-l15.sh`〔定〕（C6） | 列入 M5 順序與驗收 |
| 20 | **TDD 兩輪與版號** | 先寫測試、確認紅燈，PR 附「全部紅燈清單」（P10:72-76〔定〕） | 移植 p9a 時，程式碼已經存在；1.0.0 的 VERSION、Cargo.toml、golden 還沒有負責的里程碑（C15） | A1(b) 照樣先提交紅燈；版號升級併入 target 前的修正批次（OD-100 前例，s2:518〔審:完〕） |

---

## 4. 開工前置條件

### 4.1 工具線（S3-M1～M4）開工前
1. **p9a worktree 已保全**：20 筆未追蹤項（23 個檔）、16 個已修改檔，以及被 gitignore 的 `.superpowers/sdd/10-stage3-canary/progress.md`〔定〕（E10）。
2. **§7 A 組已裁決**，含 A4 與提前到 M0 的 B16。A3、A7 決定 T16／T5B；B4 決定基線來源，B5 決定必要證據範圍，B3 決定前置修補在哪一版完成。沒有裁決的分母／負載衝突維持阻擋。
3. **plan 10 改版**：
   - 依 §3 修正，並以**三方比對**為基準（E8）：
     - main（f431f1c0，2026-10-28）。
     - p9a 已提交的修改：abe7fb32 改 T4、f10db782 改 T10，合計 22+／15-〔定〕。
     - worktree 未提交的修改。
   - 經 terra 審查收斂，並合入 PA 或 main。
4. **p9a 與 main 的差異盤點完成**：逐 Task 列出要改的消費端介面（P10:64 規定改消費端）。
5. **S3 必要案例可執行性盤點完成**（F5）：
   - 逐案產出「出口項目／平台 → 凍結 case_id／suite／source_ref → runner 與參數 → A／B 受測 artifact／component → report 與身分欄位 → ingest kind → 出口」映射；required 以出口契約為來源，報告不能自行縮小。
   - M 固定集合重算：Linux L1.5 85 案（57 contract-blackbox）；Windows L1.5 50 案（7 contract-blackbox、1 rust-l1、14 windows-native）；Windows L2 34 regression＋1 native、L3 37 realhw＋4 native。逐案核實對外執行指定 release artifact，不能把 repo 內編譯的程式或 Python 基線誤當 B。
   - 無 runner、無可接受 report、缺平台／破壞授權、缺設備各自列為阻擋，交 A7／B7／B16；不得刪分母配合 adapter。映射在 M2 驗完整性、M4R 驗串接，RC 前完成。

### 4.2 證據線（S3-M5）開工前，也就是凍結 Stage 3 target 之前
1. **0.5.1 發布**（OD-112：全部納入後才發 0.5.1，v051:38-43〔定〕）。和 Stage 3 直接相關的包：
   - **i300**（ATTACH_FAILED 有上限自動重試）：降低 USB 重新列舉造成整輪 FAIL 的機率。OD-110 的觸發原因就是 USB 斷線後不會自動接回（evidence/stage2/longrun/README.md〔定〕）。
   - **i297、i227、i267、i294**：daemon 核心的 runtime 行為。若在 target 凍結後才合入，A/B 與 Canary 都要重來〔推〕。
   - **p279**（Windows TCP console lease 移植）：會改變 Windows L2 的 windows_gap 登記（OD-85）〔讀:od〕。
   - **c0c**：Windows zip 夾帶 com0com，影響 bundle tree 與 Canary 工作站的安裝內容規則（P10:3756）〔讀:p9〕。
   - **p277、inst4**：更新、回滾、hold 與安裝路徑，是回滾演練與 Canary 部署的基礎。
   - **chk3**：WAL 不健康時命令仍會 TX，可能牽涉安全不變量；WindowsService 沒有安裝定義〔讀:bl〕。
   - **ci-thin、i304**：在線 C 上，i304 排在 ci-thin 之後（RS:668；PLAN.md「其他」表）〔機〕。
2. **工具與案例凍結**（C1）：
   - 以下路徑的最後修改都要排在 target 之前：`tools/stage3`、`realhw`、`regression`、`func-test`、`scripts/docker`、`tests/acceptance/cases.yaml`、`cases.lock.json`、`l1_required.lock.json`。
   - target 的 `tool_revision` 建議直接取 1.0.0-rc.N 的 commit〔推〕。理由：promote guard 本來就禁止 RC 之後修改這些路徑，兩者會自然一致。
   - 所有證據都從固定在該 commit 的專用 worktree 執行，包括 Canary collector 的 pyz 與回滾演練。
   - 候選開跑 ledger／lock、報告、collector 增量置於凍結工具樹之外；不是讓 realhw 免驗。移出前須完成 §4.4-2 的歷史鏈與單一帳本驗收，否則 B 側登記即造成 tree mismatch（S3A-2）。
3. **L1 鎖**（C9、E1）：
   - **S3-L1 的直接前置**是 `s3_release_contract` 兩平台集合（P10:121〔定〕）。
     - 在 0.5.1 的 contract_blackbox 測試定案之後、target 之前，最後一次收集並以 add-only 凍結。
     - 之後到 ingest 之前不得再加節點，否則已登錄的 L1 證據會 STALE（P10:2847〔定〕）。
     - Windows 那一半只能在 Windows 原生 session 收集。
   - **Stage 2 遺留的 Windows 鎖缺口**（不由 S3-L1 直接檢查，但 B5／B12 的必要義務仍須處理）：
     - PR 291 新增的節點只登記了 Linux 集合：cargo 各 +27、pytest 各 +16〔讀:impl〕。
     - 0.5.1 各包依共同規則都不登記 Windows 集合〔讀:bl〕。
     - s2.md:268 那 7 列能力測試**已經登記**，不必再處理（`p7_gate:win-test-sh-caps-windows-com` 3 列、`s22_gate:windows_cap_windows_com` 3 列、`s21_gate:cap_supervisor_runtime` 1 列〔定〕）。
4. **Stage 3 形狀的 bundle 已建好**（T16 完成）：
   - 內容：無 py runtime、無 python_cli_delegate、manifest 帶 retirement 標記。
   - VERSION、Cargo.toml、golden 與測試中的版號已升到 1.0.0（C15）。
   - 已用 `evidence target --stage 3 --bundle-dir` 設定兩平台 target。
5. **Windows 受測安裝的指紋可解析**（C3）：
   - **前例**：main 上唯一的 Windows 實機 gate 報告，`deployed_content` 的 start 與 end 都是 `error: "unresolved"`，tree 為 null〔定〕。原因是 CLI 直接指向 `...\versions\0.5.0\bin\serialwrap.exe`；而 `realhw/deployed_content.py:71-79` 在 Windows 只能從 `bin\current-release.json` sidecar 找到安裝目錄〔定〕。
   - **規則**：target 記有 tree 時，這種報告會被拒收（P10:3100 (a′)〔定〕）。
   - **修法**：不能只改 testbed 指向穩定入口；loader 要求的卻是版本內 component realpath（P:ab_manifest.py:313-318），adapter 又會覆寫設定。M2 先依 §4.4-4 統一執行入口、版本根及 component 的身分契約；建議讓量測器安全支援直接版本路徑，保留 A/B component 精確路徑規則。驗收須同時通過 loader、兩種 adapter、前後 deployed tree 與 ingest，並拒絕錯 sidecar／入口複本／版本根／實際 daemon；tree 非 null 本身不足。
6. **實機彩排（S3-M4R）通過**（F3、F4）：
   - 第一組：0.5.1↔0.5.0 跨版本 shakedown，Current 與 Previous 必須不同，覆蓋 D0～D8。
   - 第二組：可丟棄的 Stage 3 形狀候選↔0.5.1；其 A 包使用同一候選 CLI＋凍結 Python daemon，走 Stage 3 loader、所有 runner／ingest、退役打包與 RC→promote 完整流程。若要正向演完 retirement gate，必須實際滿足全部必要證據、完整長跑與 Canary 配額；縮短診斷不能當作這一項通過，彩排深度待 A3 決定。
   - collector 在兩台部署機各跑 2～3 天，0.5.1 日常 shakedown 1～2 週。縮短長跑仍按凍結門檻預期 INSUFFICIENT，不能以成功解析宣稱 PASS；兩組只寫各自隔離彩排索引。
7. **Canary policy 已凍結（BO-P9-06）**：
   - 凍結時點要早於第一筆計數。
   - 兩平台的部署機與證據責任人已指定（§7 B1、B2）。
   - runbook 事先寫明：哪些事故不必換 artifact 就能結案，哪些一定要開新 rc（F3）。
8. **OD-83 B 已裁決**（§7 B3）。建議補做，屬 runtime 變更；不論歸入 0.5.1 或前置版，都另估 1～2 週並排原生驗收，與其他工作共用資源不得重複計算產能。
9. **可執行性缺口已依 A7 解決，任何變更原判準另有明確新 OD 才能採用**：
   - remote tier 環境（B15）。
   - B16 的 Stage 3 專屬板數與逐板負載裁決、相容原凍結集合的契約及先行基線證據；不能只排除 two_boards。
   - Windows 的 TestPilot。
   - a250113 的 sudo 與 usbipd 授權。
   - Linux／Windows 所有非 func-test 案例（含 windows-native）都有對應 runner、受測 artifact 與完整 report 承接（§4.1-5）。
10. **破壞性授權與演練組合已備妥**（B7、B14）：
    - 各項的 decision_ref 與限定板子。
    - Previous 版本已選定。
    - D8 的 start-fail 來源 bundle 已建。
    - 1.0.0 ⇄ Previous 的 state.json、config、監管定義相容檢查已完成（spec:1110、1112〔定〕）。
11. **真板（OD-27）**：
    - OD-27 對 Stage 1／2 的延後不自動豁免 Stage 3；S3-L2-FULL-AB-LINUX 要求「發版前全部適用案例通過，含破壞集」（spec:1064），其餘遺留義務依 B5 逐項核定。
    - 建議把 OD-27 全套併進 M4R 彩排，當作 BO-P9-02 的預演〔推〕。
12. **證據索引缺口（BO-P9-14）**：
    - `retirement verify --mode release` 只呼叫 `stage_status(index,"3")`（P10:3318-3321〔本〕）。所以 Stage 0～2.4 的 owner 項**不是**退役的程式前置。
    - spec:1193「必要證據缺漏阻擋退役」的範圍，要由擁有者逐項核定（§7 B5）；程式只查 Stage 3 不是豁免理由。所有既有必要項須補驗、提出符合身分／範圍的沿用證據，或取得逐項新 OD；未完成仍阻擋 1.0.0。
    - stage0 索引共 747 項，其中 725 項 missing〔讀:impl〕。
13. **環境還原與同意**：
    - a250113 的 serialwrap.service 與五個 cortex manager 要由對方使用者以 sudo 還原（ledger-inbox.md:1085）〔機〕。
    - 遠端使用者要以書面同意 M4R 與 M5 的停機時段、sudo 操作與安裝改動（F11）。
14. **安裝與文件變更排在 RC 之前**（C12）：
    - promote guard 不放行 install.sh 與 README.md（release_promote_guard.py:52-58〔定〕）。
    - 所以 B6 的 install.sh 修改（取消 python3.12）與 README 中英段，都必須在建 rc.N 之前合入。

### 4.3 Stage 2 收尾（有獨立時限，必要遺留義務仍依 B5 逐項核定）
- **Slice 2.4 斷路器**（E2、F9）：
  - **凍結參數**：`slice_2.4.json` 的窗口從 2026-11-08 起算 3 週，到 11-29 結束；分母 187〔定〕。
  - **判定規則**（slice_breaker.py:39-41、103-112〔定〕）：
    - 首筆快照不晚於 start＋7 天：11-14 這筆已符合。
    - 末筆快照**不早於 end−7 天，也就是 2026-11-22**，否則判 insufficient_evidence。
    - 從首筆到末筆，承接率要增加 **≥10 pp**，否則判 **tripped**，比 insufficient 更嚴重。
  - **目前狀況**：唯一一筆是 11-14，rate 0.198（37／187）〔定〕。
  - **結論**：要在 **11-23～11-29** 之間實跑 2.4 凍結集合，產生新快照，rate 須 **≥0.298（≥56／187）**〔定，算術〕。
  - **計算方式**：由 `slice_breaker.acceptance_rate` 依實際逐案結果計算，不得手填（s2:391-395〔定〕）。
  - **排程**：要在 §6 排 heavy-slot、指定負責人並事先通知 thala，避開 thala T1。0.5.0 跑 2.4 gate 時，曾需要 thala 讓出機器（RS:561-562）〔機〕。
  - **若決定不跑**：要事先請擁有者裁決是否接受 insufficient_evidence。
- **Slice 2.1～2.4 的出口報告**（E12）：
  - **2.2**：main 有 `slice22-exit-report.md`（b5bfb6b8，2026-10-29），但內容只有 #185 流程證據與第 5～7 輪 gate，沒有 OD-87 要求的缺漏說明〔審:證〕。
  - **2.1、2.3、2.4**：沒有出口報告。
  - **快照現況**：2.2 與 2.3 的快照檔是空的；2.1 只有兩筆，都是 10-29〔定〕。
  - **OD-87 的要求**：2.1～2.3 豁免斷路器，但出口報告要寫明快照缺漏與實際進度（s2:392〔定〕）。

---

### 4.4 RC 前必須完成的整合契約（S3A-1～6）

1. **出口決定必要分母（M1 設計、M2 實作）**：`realhw-report` 的必要 family／case 集合從凍結出口契約與 slice 鎖推導；`meta.tiers` 只是報告宣稱的覆蓋，不能用來減少 required。驗收含 P0-only 不得滿足 P0P1-REMOTE、缺 remote／單案／重複列均不能取得 PASS；報告外的 FAIL 仍保留。出處：P10:338-339；P:evidence_ingest.py:529-534、854-864、944-986。
2. **不可變工具與附加執行帳本分離（M1、M2）**：保留 M 的 `realhw/acceptance/candidate_longrun_starts.ndjson` 原始歷史及 SHA；以其完整內容為受核對前綴，在凍結 checkout 外建立受控的追加帳本。manifest／run context 固定帳本位置、歷史錨點及 run_id；registration、OD-09 時序檢查、T15、T14B ingest 必須消費同一份帳本，追加以鎖及 fsync 保護，缺漏、截短、改寫前綴、重複 run_id、指錯帳本均拒收。每輪留存開跑記錄與帳本快照／hash，依 A8 歸檔到 docs 證據；不得刪歷史、重設 earliest start、重新 freeze 或將整棵 realhw 排除。驗收先 A 完成→B 必要登記→重新驗證 frozen tree→B 可開始，並測併發與拒絕分支。RC 前改好讀寫介面；promote 不修改原 realhw ledger／lock，guard 不放行執行工具。出處：M:od09_freeze.py:40、589-605、772-814；P:ab_runner.py:137-153、288-301、358-364，P:evidence_ingest.py:55-56、1395-1399，P:ab_manifest.py:441-464、705-715。
3. **A 側為相容配對包（M2 製作、M4R 驗證）**：B4 只核定 Python daemon／runtime 來源。每平台用同一份候選 CLI 位元組組成 A 與 B；A 保留凍結 Python daemon／runtime，B 是退役形狀的 Rust daemon，分別記 manifest、daemon/runtime SHA 與來源。檢查 CLI implementation／SHA 完全相同、CLI↔daemon 的版號／協定配對及啟動方式，並以 BC 處理已核准行為差異；0.4.1 產品碼保持凍結。若不相容，M2 修配對／打包介面或回報新裁決，不偷偷換 CLI／改基線。正式 A 包待最後候選 CLI 確定後重建、凍結，僅 B 包對外發布。出處：P10:906；P:ab_manifest.py:392-402；M:crates/sw-cli/src/lib.rs:54-57。
4. **Windows 執行與量測同一 component（M2）**：建議讓 `deployed_content` 明確接受並安全驗證版本根／manifest，直接版本入口仍須等於該版本宣告的 CLI component realpath；不向版本目錄偽造 sidecar。穩定安裝入口另驗 sidecar 安全相對路徑、入口複本 SHA 與所指版本的 bundle/runtime tree。實際啟動 daemon 的路徑／SHA、A/B 受測版本根及前後 tree 均須對應當側 manifest，adapter 不得把穩定入口或版本根悄悄改成另一份安裝。若選用穩定入口執行，必須先設計等強度的入口→component 驗證並修 loader 契約，不能僅取消 realpath 檢查。兩條路徑的偽造 sidecar、錯複本、旁載檔、換版本、未宣告 daemon 均須拒絕。出處：M:realhw/deployed_content.py:71-120；P:ab_manifest.py:313-320；P:suites/l2_regression.py:43、suites/l3_realhw.py:380。
5. **完整 runner 映射（M0 盤點、M2 交付）**：不能只加 windows-native adapter。Linux 57 案 contract-blackbox、Windows 7 案 contract-blackbox／1 案 rust-l1 及各層 native 均須有可執行 runner 與統一報告承接，證明跑的是當側 release artifact；rust_only／BC 使用既有核准規則，不把 pytest／cargo 自測視為 A/B 產品證據。完整性驗收將 §4.1-5 逐案表與分母作集合等值比較，unsupported／missing report／錯 artifact 必須阻擋，包含 `bc02-deferred-context-switch-clear` 反例。出處：M:cases.yaml:3653-3675；P:suites/l15_functest.py:61-74、170-174、207-212。
6. **端到端 Stage 3 發布路徑（M4）**：新增明確的 hosted RC 建置／上傳入口，兩平台打包、`retirement pack`、SHA256SUMS、RC-INSTALL.md、prerelease 資產驗證及下載重驗；現行 release.yml:110-140 只有驗資產能力。統一 Stage 3 manifest stage（P9 要求字串 `"3"`，現驗證器只接受 `"rust-2"`）、schema、archive 佈局及兩平台 manifest 位置，讓 loader／retirement／install／release_assets.py 同意同一契約；維持 Stage 2 路徑相容。精確資產白名單及 SHA256SUMS 逐檔驗證不得改成任意接受；promote 以 Stage 3 安裝指南產生 RELEASE-INSTALL.md，下載原 RC archive，驗退休證據與資產後上傳並再下載重驗，兩平台 archive 位元組不變。retired:true 僅在 promote 的授權 metadata，不重打受測包；M4R 的 retirement 裁決／標記只在隔離彩排資產與索引演練，不能寫正式退役決定或提前發正式 1.0.0。新建置入口、guard 允許範圍、證據取得方式全在正式 RC 前演練。出處：M:scripts/rust/release_assets.py:111-114、142-166、220-239；M:.github/workflows/release.yml:110-140、197-218。

---

## 5. 里程碑與順序

> 工期均為規劃估算〔推〕，以一條持續有保底產能的線及每週原生驗收為基礎，尚非日曆承諾。等待 owner、配額不足、採購／遠端授權沒有上限，不能混入固定週數。以下列的是未來執行工作，本輪沒有執行測試、部署或發布。

| 里程碑 | 內容 | 驗收 | 資源／slot | 推估工期 |
|---|---|---|---|---|
| **S3-M0a 裁決與逐案盤點** | 保全 p9a；A1～A8 及提前的 B16／B4／B5／B3；§4.1-5 完整映射；Canary 真實使用量測、Windows 工作站與責任人；A6 產能方案 | 逐案表可重算；長跑時長、板數、逐板負載、既有 OD 適用範圍與待決項逐條記錄，不以 gap 或 SKIP 代裁決 | 擁有者、唯讀盤點；不需 heavy-slot | **3～5 工作天（0.6～1 週）**；較 r1 多平台映射及裁決債務盤點 |
| **S3-M0b plan 10 改版收斂** | §3、§4.4、§7 全部連動；三方比對與 PA 合流；寫入新 OD／spec；A5 預設 (b) | 獨立審查無 High／Medium；每個放寬提案有逐名受影響條款及明確 owner 決定，未核准仍阻擋 | 文件與審查配額 | **1～2 週**（2～3 輪）；不預支 owner 同意 |
| **S3-E 必要遺留證據清冊**（新增） | 按 B5 逐 item／平台核對必要義務、現有證據身分、補驗／沿用／待 OD 及逐項工期；不以 stage_status(3) 取代 spec:1193 | 全清冊有處理路徑，owner 核定前保留義務；尚欠補驗列為阻擋並加入資源及關鍵路徑 | 唯讀盤點、owner／獨立核對；在 M0b 前完成範圍收斂 | **1～2 週**清冊與裁決材料；未落在既列集合內的補驗另逐項估，不以此週數包辦所有 missing |
| **S3-M0c 環境、拓樸及凍結負載承接**（新增） | 按 A4／B16 新 OD 準備 Windows 第二板、TestPilot、原生 OpenSSH／remote、Linux 對端；處理 OD-109 只有 Windows COM0 的凍結負載與新增板的基線／附加契約；取得 B7／B8 授權及還原遠端環境 | 舊 YAML、歷史與時序完整；逐板負載／比較方式可稽核；所需基線與其凍結均早於對應 Stage 3 候選；完整分母每案有設備及授權，否則停 | Windows 原生、遠端使用者、安靜主機／bench | **2～4 週**：環境／授權落地 1～2 週＋基線／負載核對與凍結 1～2 週；依建議兩板方案暫估，等待裁決／採購另計 |
| **S3-M1 基礎與判定器** | T1／T2／T3／T7B／T7／T14／T15；出口驅動分母規則；外部帳本及歷史鏈介面；`s3_release_contract` 初登；B11 先獨立審再追認 | 逐 Task TDD 紅燈清單、全套 pytest／policy；原生 Windows add-only collection；不縮分母、不掩蓋 FAIL 的反例 | Python 工具線；每週 Windows 原生時段 | **2～3 週**（原 1～2 週，新增帳本及判定契約約 1 週） |
| **S3-M2 A/B 與全部 runner** | T4／T5／T5B／T6；T14B 第一段；Linux 57 案 blackbox 與 Windows blackbox／rust-l1／native runner；共用候選 CLI 的 A 配對包；Windows 入口／版本根／component 量測；帳本接線及 ingest 分母修正 | §4.4-1～5 驗收；完整逐案映射無 unsupported；Windows 原生釋放測試；同 CLI SHA 的 Python↔Rust 預演；A→登記→凍結重驗→B；P0-only／缺 remote 不能取得 PASS；錯入口／artifact 拒收。單板短跑僅診斷，不能收斂 B16 | 安靜主機、Windows COM20／21、已核准兩板及 remote；原生 runner 所需 cargo／heavy-slot 另排，不能再稱只跑 pytest | **3～5 週**（原 1～2 週，加 runner 約 1～2 週、基線包及 Windows 身分／串接約 1 週）；M0c 未完成則驗收不能結束 |
| **S3-M3 Canary 工具** | T8／T10／T11／T9（pyz）／T12；T14B 第二段；事故與帳本歸檔 | 兩平台原生測試；synthetic ledger 驗五值只驗工具；真實配額仍待 M5 | 工具線、Windows 原生 | **1～2 週** |
| **S3-M4 回滾與完整發布改造** | T13／T16／T14B 第三段／T17；Rust 守門、mkbundle 兩平台、pack、release_assets.py、RC 建置上傳、stage／schema／佈局／白名單、Stage 3 安裝指南、guard、hosted 證據取得與 promote 重驗（§4.4-6） | P9 Phase gates：全套 pytest、resource markers、cargo workspace、check_cfg_boundary、win-test.sh、policy_check；Stage 3 status 預期 BLOCKED；Phase 雙審；離線完整資產驗證及缺檔／錯 stage／錯 SHA 拒絕；install.sh／README 已更新 | cargo／heavy-slot；原生 Windows；T16 在 inst4、c0c 合入後 | **3～4 週**（原 1～2 週，新增資產驗證器／建置上傳／promote 整合約 2 週） |
| **S3-R 監管前置修補**（獨立列帳） | B3 選定版本實作 OD-83 B、契約／BC、Windows 原生 restart 與 Defender 需求驗證；若另發前置版則含發布 | 凍結分母中的 restart 案例成立；實跑身分與版本記錄；未完成不能以已知限制直接豁免 | 產品實作線、cargo／原生 slot；與 M1～M4 共用時須扣產能 | **1～2 週**；進 0.5.1 仍耗同樣資源，前置版發布額外等待另計 |
| **S3-M4R 兩組彩排** | 第一組 0.5.1↔0.5.0 shakedown（1～2 週）；第二組建可丟棄 Stage 3 候選／同 CLI A 包（0.5 週），對 0.5.1 演 D0～D8、全 kind／全部必要集合及拒絕路徑，先用 1～2h 長跑／2～3 天 collector 診斷；按 A3 建議再完成不縮門檻的長跑與真實 Canary（4～8 週條件式估算），以隔離彩排 release 演完整 RC／promote 正向及負向路徑 | 各組獨立彩排 target／index／artifact；Current≠Previous，D6／D8 回到原 Current 內容指紋；Stage 3 loader／ingest／退役包與 hosted 全證據取得可用。短長跑預期不足、缺 tiers／FAIL／缺檔須被擋；負向／縮短診斷不記 PASS，完整正向必要集合仍全數 SATISFIED。缺陷修完先驗相關工具測試，再依凍結規則重設彩排 target／重跑必要集合，之後才建正式 RC | OPI＋已授權第二板、a250113、COM20／21、兩台 Canary 機；遠端停機、hosted 配額 | **5.5～10.5 週**（1～2＋0.5＋4～8），依 A3 完整正向方案；工具／artifact 修正後按凍結規則重設彩排 target／全套重跑，額外重置另計；正式 RC 仍另跑 |
| **S3-M5 最後候選與正式證據** | 版號批次及最後修正→hosted 建 1.0.0-rc.N（退役形狀）→重建同 CLI A 包→兩平台 target；docker L1.5 前置（不登錄，失敗即停）→兩平台 L1、完整 L1.5 A/B、L2 A/B 含破壞集、L3 P0＋P1＋remote、長跑、native 72h／登出、D0～D8；Canary 在 target／policy 均凍結後並行；B5 必要遺留證據逐項收齊 | 16 個 S3-* 逐項 ingest SATISFIED，stage_status(3)=PASS；分母／歷史／FAIL 不可抵銷規則保持；B5 無未裁決必要缺口。彩排全部不得沿用，正式 RC 重跑全部必要集合 | 已裁決拓樸、安靜主機、remote、真實使用機；擁有者到場 | **建置／配對／target 0.5 週＋證據 4～8 週暫估**；下表限定假設 |
| **S3-M6 退役與 1.0.0** | BO-P9-13 裁決；promote metadata retired:true；對原 RC archive 及完整 evidence_root 驗 verify／verify-archive／release_assets；同位元組 promote、上傳後下載重驗；changelog／release notes；依新 OD 停發 wheel／exe | workflow 綠、正式 archive SHA 等於受測 RC；必要證據檔案全部可取且 hash 正確；只宣稱 OD-12 單人工作站信任模型 | owner、hosted promote；不需本機 cargo | **1～2 週** |

**M5 暫估的資源計算**（保留 r1 的 0.5.0 重跑觀測作風險線索〔機〕，不是本輪實測）：

| 資源 | 名目項目 | 名目合計 | 含重跑暫估 |
|---|---|---|---|
| Windows bench（OPI＋核准第二板，依序獨占） | L2 A/B 1～2 天；完整 L3 1 天；A4(c) 各側 24h 加前後置 2.5～3 天；native 72h 3～3.5 天；D0～D8 0.5～1 天；Windows 登出 0.5 天 | 約 9～12 天 | 約 **14～24 天（3～5 週）**，倍率 1.5～2；補板不代表已證明此產能 |
| a250113 兩板 | L2 A/B 1～2 天；完整 L3＋remote 1～2 天；24h A/B 2.5～3 天；D0～D8 0.5～1 天 | 約 6～8 天 | 約 9～12 天，倍率 1.5 |
| 安靜主機／COM20：COM21 | docker L1.5、完整 Linux 85 案與 Windows 50 案 A/B（含新增 runner） | **2～4 天**（原 1～2 天上修） | **3～6 天**；M2／M4R 必須量測校準 |
| Canary（兩平台） | 14 活躍日、5000 submits、50 cycles、至少三類、比例限制均滿足 | 若 B2 量測可行約 3 週 | 預留一次重置約 +3 週；**3～6 週僅條件式估算** |

- M5 證據線暫估為 `max(Windows 3～5 週, Linux 約 2～3 週, 安靜主機約 1～2 週, 兩平台 Canary 達標期)`＋分診／緩衝約 1～2 週；B2 若支持 Canary 3～6 週，取 **4～8 週**。
- Canary 各平台達標期須由 M0a／M4R 的實測提交量、各類循環速率與活躍日估算，再取兩平台較長者；不能只用「每天 357 submits」的算術替代真實量測。未量測前，4～8 週不能作完成期限。
- A4 改選 48h、單板串行或新負載基線若使 bench 增加，須按該 OD 重算名目時長及重跑，不保留 24h 並行估算。
- 正式 FAIL 後按 A5(b) 開新 RC、重設 target／全套重跑；上述只編一次約 3 週 Canary 重置，多次故障及修復時間另加，不用同 artifact 的新 PASS 抵銷。

**產能與依賴**：

- M0a→S3-E→M0b→M0c 依序，合計 **4.6～9 週**；M1→M2→M3→M4 的保守單線排程合計 **9～14 週**。M0c 可與工具設計交錯，但只有取得獨立人力、原生／bench 時段及 quota 後才能扣重疊；預設不扣。
- 從現在到 RC 前置完成，基準是 M0＋S3-E＋M1～M4＋S3-R＝**14.6～25 週有效工作／排程時間**，再加 owner／採購／額度等待；這不是全部在 0.5.1 後才開始。B3 若由另一條有獨立份額的線做，僅能依實際重疊扣除 1～2 週，不能稱免費。
- A6 建議保底工具線每週 5 個有效工作天（1 週估工以 5 天產出校準）、Windows 原生至少每週半天、安靜主機至少每週一個 4h 窗口，M0c／M4R／M5 bench 另外整段預約；M4R 的完整 Canary 使用量不可轉到最後 RC 的 M5。review 額度與 token quota 須以首週實測列成數字並由 owner 核准；只承諾 slot 名稱不足以支持上述工期。
- T16 要等 inst4／c0c 合入；M4R 等 0.5.1、S3-E／M0c／工具／S3-R 全部完成。任何在彩排後改 runtime／工具／集合的缺陷修正都先完成，再建最後 RC，不能把第一次 Stage 3 整合留到正式證據後。

**修訂後關鍵路徑**（條件式估算）：

```text
T前置 = M0a → S3-E 必要證據清冊 → M0b 裁決改版 → M0c 環境／負載
        → M1→M2→M3→M4
        ＋ S3-R（無獨立資源時串行；另須等 inst4／c0c）
T起跑 = max(0.5.1 發布完成日, T前置完成日)
1.0.0 = T起跑 ＋ M4R 兩組彩排 5.5～10.5 週
              ＋ 最後 RC／同 CLI A 包／target 0.5 週
              ＋ M5 證據 4～8 週（待真實配額量測確認）
              ＋ M6 1～2 週
      = 0.5.1 發布日 ＋ max(0, 尚餘全部前置的排程週數)
                      ＋ 11～21 週
```

A6 影響「尚餘全部前置」；缺 owner 裁決、第二板負載無合法承接、原生能力未補齊或 Canary 真實使用不可達時，發布仍阻擋，沒有可承諾的總週數。

---

## 6. 資源與排程

| 資源 | 限制 | Stage 3 用途 | 排程注意 |
|---|---|---|---|
| Windows OPI（COM6，CH340，OD-92） | 單板；可登入、reboot、進 U-Boot、傳檔；寫 flash 要先問；實體拔插仍為 BLOCKED-OWNER。和 thala 的 USB SSD 共用 AMD xHCI root hub，CH340 不能換埠（OD-107）〔讀:bl、od〕 | Windows L2 A/B、L3 P0P1、L3 A/B、win-task-72h、回滾演練、M2 完整映射驗收、M4R 兩組 | **M5 的瓶頸**：含重跑約 14～24 天（§5）。長跑期間請 thala 不要動磁碟或 USB，或把 USB SSD 改接其他 root hub；擁有者不要插拔周邊（OD-110 前例；F2）；Windows 上不得有其他 serialwrapd |
| Windows 第二板（待授權） | 非現有資源；型號／selector／用途／逐板負載與 OD-109 承接待 B16，未具備不得開始完整證據 | Windows two_boards、lr-mixed、remote 及彩排 | M0c 準備；不能用 OPI 的 COM0 門檻充作新板門檻 |
| 擁有者 Windows host COM20：COM21（com0com） | 只有編排者能經 interop 執行；interop 以系統管理員權限執行，改系統設定要先取得同意；Defender 排除要擁有者處理〔讀:bl〕〔機〕 | BO-P9-01、15（Windows L1.5 A/B、WindowsProbe）、Windows 原生 pytest、L1 鎖收集 | 實作 agent 的 PATH 沒有 Windows interop，不能代跑 |
| **編排者的 Windows 原生時段**（新增，F12） | 只有編排者能執行；需要它的有 M1 的 s3_release_contract.windows、M2 的 T4、M3 原生測試、M4 的 win-test.sh、0.5.1 各包的「待 Windows 原生收集」（如 p277 REMAINING，RS:664）〔機〕 | 上列 | 固定節奏：每週集中一次，M1～M4 的原生驗收併進同一批；target 凍結前再收最後一次（B12） |
| 遠端 a250113（BRCM，COM0 FTDI、COM1 CH340） | 唯一的兩板環境；正式 service 與五個 cortex manager 待以 sudo 還原；單次 push 最多 5 條分支；COM0 有 Python 基線既有的 FLUSHED_BY_RECOVERY 雜訊〔讀:bl〕〔機〕 | Linux L2 全套 A/B（含破壞集）、L3 P0P1＋remote、L3 A/B、回滾演練（`setup --rollback` 與 roll-forward 會改動對方的正式安裝，P10:2675、2678〔審:可〕）、M4R | **停機天數**：M0c 基線／負載另排 1～2 週窗口；M4R 第一組預演 1～2 天、第二組完整集合含重跑 9～12 天；M5 另含重跑 9～12 天〔推〕。事先取得遠端使用者的書面同意；每段結束都要「還原 service」（F11） |
| **remote tier 環境**（新增，C8） | rm-topo 需要 docker；rm-live 需要 docker、兩板、remote_capability（realhw-stability-checklist.md:181；preflight.py:98-108〔審:完〕）。OD-27 時 Windows 沒有 sshd，裝 OpenSSH server 要提權，rm-live-e2e 記為 BLOCKED-OWNER（od27 windows/report.md:117〔定〕）；Linux 完全沒跑 remote | S3-L3-P0P1-REMOTE-LINUX／WINDOWS | 依 B15 分別指定 Linux 與 Windows 原生 OpenSSH／remote 負責人；Linux docker／SSH 不代 Windows 證據；不提供即阻擋 |
| **docker 主機**（新增，C6） | `scripts/docker/run-l15.sh` 等已在 main〔定〕；本機是否可跑 docker〔未〕；0.5.1 共同規則禁止實作 agent 跑容器（brief-common.md:12），只適用 0.5.1 | 每個 RC 的 docker L1.5 前置、remote rm-topo、B14 的升級相容檢查 | 由編排者在 M4R、M5 執行 |
| 本機 WSL（MTK 正式網路板） | 只能下讀取指令，不得重開機（OD-94）〔讀:od〕 | 可能作為 Linux Canary 的日常使用機 | 配額可行性未驗證（B2，F7）；Linux 長跑經 OPI 橋接時要停正式 service，會和 Canary 衝突（s2:553〔定〕） |
| gb10 | 可用記憶體約 5GB（vLLM 佔用），一次只能跑一個重工作〔機〕 | 只做文件，例如程序文件草稿 | 暫停 vLLM 要擁有者決定 |
| CPU heavy-slot 與**安靜主機**（F10） | 全機 3 格、每個 session 最多 2 格；cargo 要包進 slot；thala T1 期間減為 2 條線；L1.5 時序案例與延遲量測要整機安靜，並提前 30 分鐘通知 thala〔機〕 | M1／M3 工具測試、M2 多 runner（必要時 cargo）、M4 Rust／發布改造；需要安靜主機的有：M2 的 Linux L1.5 預演（A、B 各一次）、M4R 與 M5 的 Linux 和 Windows L1.5 A/B（COM20／21 與 WSL 在同一台實體主機〔推〕）、M5 的 docker L1.5、11-23～29 的 2.4 斷路器實跑 | B8 列出每次的項目、次數、時長（暫以每週 4 小時窗口排程、實際每次時長待 M2 量測），並寫明要暫停哪些線（A、B、C、S3）；M2 的預演排在 0.5.1 各線的空檔 |
| 磁碟（F14） | E 槽 `/srv/thala-e/serialwrap-cargo` 上限 50G，超過 45G 先清；D 槽要 ≥120G；每條線的 target 約 14～19G（RS:658-660）；曾因清理誤刪證據〔讀:bl〕〔機〕 | M4 新增一個 cargo target | 三條 0.5.1 線都在 E 槽時已約 42～57G〔推〕。所以 M4 的 target 放 D 槽，或等某條 0.5.1 線完成並刪掉 target 後再開；清理前先看內容，證據目錄保留 |
| codex 額度 | 每週重置（11-11、11-18 的 13:47；下次推測 11-25〔未〕）；11-16 已用到 90%；11-17 三條線並行撞上 TPM 上限（2M/min）〔讀:bl〕〔機〕。審查改為 terra xhigh 後的用量**未重估**〔未〕（F13） | P9 移植（luna max）、terra 審查（M0b 多輪＋高風險 Task 9 項＋Phase 雙審） | 依 A6 的保底份額規劃；額度用到 80% 時，先停 0.5.1 中非關鍵的包，或依 A6 裁決 |
| Canary 真實使用 | 每平台 14 活躍日、≥5,000 次 submit、≥50 次循環（OD-10(3)）。draft 活躍日門檻為每日 ≥20 次 submit、快照覆蓋 ≥3600 s；至少 3 種類型有配額、單一類型占比 ≤0.5（draft 0.4）、coexist 每 com 每天最多計 5 次（P10:173、181、219、227〔審:可〕）；Windows 不計 flash〔讀:spec〕 | S3-CANARY-LINUX／WINDOWS | 5,000 次分攤到 14 天約為每天 357 次，這只是算術，沒有量測過現行使用量〔推〕（F7）；不能用日曆期限取代配額（spec §12.2）；Windows 工作站在 M0a 就要指定（F8） |
| bench 時段 | M0c 基線、M4R 兩組及 M5 各自預約，見 §5 細算；A→B 之間要獨占（BO-P9-16） | M4R、M5 | 和 thala、遠端使用者協調；OPI 長跑期間不能並行其他實機案例；win-task-logon-trigger 需要一次 Windows 登出，會中斷整個 WSL（所有線、thala、本機 Linux Canary），時間由擁有者挑（記憶 windows-logout-coordination〔機〕；F6） |

---

## 7. 需要擁有者決定的事

### A 組：工具線開工前（plan 10 改版要用）

**A1. p9a 分支怎麼處理**
- 選項：
  - (a) 整支 rebase 到 main。
  - (b) 以 p9a 為參考，依 Task 移植到現行 main，逐 Task 對齊介面。
  - (c) 依修訂後的 plan 10 重寫。
- **建議 (b)。**
  - 理由：(a) 要面對約 1514 個 commit 的落差，而且有 15 個檔案兩邊都改過〔讀:impl〕。
  - 移植前先以 commit 保全未追蹤檔與 ignored 的 progress.md。
  - 移植驗收加上 §4.4 的分母反例、外部帳本歷史鏈、共用 CLI A 包、Windows 入口身分、全部 runner 及完整發布路徑；不能只對齊函式介面（S3A-A1）。
  - 移植時仍照 P9 的 TDD 兩輪：每個 Task 先提交測試、確認紅燈，並在 PR 附全部紅燈清單（P10:72-76〔定〕）。若要以 p9a 既有程式碼豁免這一步，需另行裁決（C15）。

**A2. Stage 3 target 的版號，以及 Canary 在哪個 bundle 上累計**
- 選項：
  - (a) 建 `1.0.0-rc.N` 退役形狀 bundle，A/B 與 Canary 都在它上面跑，正式 1.0.0 promote 同一份位元組。
  - (b) Canary 先在 0.5.x 上累計，再核准 carryover。但 carryover 只帶 submits／cycles，不帶活躍日（P10:374〔定〕）。
  - (c) 修訂 P9 的身分規則。
- **建議 (a)。**
  - 理由：符合「受測物即發佈物」，也延續 OD-104／106 已實證的做法。0.5.0 promote 後，資產的 sha256 和 rc.1 完全相同（RS 11-22 19:45〔機〕）。
- A 側另按 B4／§4.4-3 製作同 CLI SHA 的配對包；A2(a) 不授權拿 0.4.1 整包與不同 CLI 的 B 比較（S3A-A2）。
- 注意版號：
  - P10:3321 要求 `bundle_version == effective_bundle_version`。所以 bundle 內嵌的版號要直接寫 `1.0.0`，只有 tag 用 `v1.0.0-rc.N`。
  - VERSION、Cargo.toml、golden 與測試中的版號，要在建 rc.1 之前一次升到 1.0.0，併入 target 前的修正批次（OD-100 前例；C15）。
  - changelog collate 可以放在 promote commit，因為 guard 放行 CHANGELOG.md 與 changelog.d/〔定〕。

**A3. 1.0.0 的發布機制（S3A-2、6）**
- 選項：
  - (a) 沿用 RC→promote，同步完成 §4.4-6 的資產驗證器、RC hosted 建置／上傳入口、manifest stage、archive layout、精確白名單、Stage 3 指南與上傳後重驗。RC 以 retired:false 建退役形狀包並 pack；promote metadata 才置 true，驗完整證據及原 RC archive，正式 archive 不重建、不改位元組。
  - (b) 自建 self-hosted 發布入口，仍必須滿足相同 stage／資產驗證、Windows 原生與完整證據條件；會另增維運及 runner 驗證工期，不能略過 validator。
- **建議 (a)，條件全寫入 M4 及 M4R；目前未獲 owner 裁決。** Windows 沿用 hosted windows-latest（OD-78），撤 BO-P9-17 並同步修 T17 兩支 runbook 測試。
- pack 與現行佈局二擇一須在 RC 前定案：安全支援既有受控相對 symlink／外層目錄，或 Stage 3 改為無 symlink／無外層目錄，後者同步驗 install、setup、tree 與 validator；任一路徑不得放鬆安全路徑檢查。
- guard 變更逐路徑列明及驗證：預期新增 retirement JSON 的受控 false→true 變更，docs 證據及帳本快照使用既有 docs 規則；原 realhw ledger／lock 不列白名單。若尚需其他路徑，先在 M0b 列 owner 決定及範圍，不預稱「只需一檔」。工具／workflow／install.sh／README 修改全在 RC 前完成。
- 採 (a) 的後果：M4 增加約 2 週發布整合，M4R 必須驗 Stage 3 建置→上傳→下載→驗證→promote→再下載，現行 release event 的 validate-rc-assets 不能冒充建置能力。
- **另待 owner 決定彩排深度**：建議完整正向彩排，第二組實際完成所有必要證據（含長跑下限、Canary 14／5000／50），才能通過 retirement gate，兩組共估 **5.5～10.5 週**；對外正式 1.0.0 仍只在 M6 發布。可選只做縮短通路診斷（兩組約 3～4 週），則 retirement verify 必須拒絕、以整合 fixture 驗正向分支僅算工具測試，不能宣稱完整發布實跑成功；完整正向 gate 到 M6 才首次以正式實證完成，須記此未驗風險。這不是降低門檻的選項，不能把缺配額改 PASS；§5 依建議完整方案計算。

**A4. Stage 3 L3 A/B 的時長、拓樸、排程與負載（S3A-8）**
- **目前仍待 Stage 3 專屬新 OD**：OD-94、OD-102 明載 0.5.0 四輪，各 24h；OD-102 又限定只放 OPI 一板。不能由 Stage 2 的實跑，推得 Stage 3 已獲 24h／換板／並行授權。
- 硬限制：不修改已凍結 `longrun_acceptance.yaml`、不重設 OD-09 歷史／earliest start、不改 S3-L3-AB-48H-* item_id；86400 是下限，不是自動取代 Stage 3 spec 的 48h。產品／安全 FAIL 不得抵銷。
- 選項（皆須連同 B16 的逐板負載裁決才完整）：
  - (a) 每側 24h，同一 bench 錯開四輪；名目約 4 天加前後置。若仍單板，必須由新 OD 明列修改 `lr-mixed` two_boards 與 Stage 3 覆蓋要求，否則不是可發布方案。
  - (b) 每側 48h，保留 spec 時長；bench 約增一倍。須核定與 OD-109 的 24h 資源／逐板吞吐基線如何可比，不能直接調低既有凍結值。
  - (c) 每側 24h，Windows 與 Linux 各有已核准且滿足 two_boards 的硬體；Windows OPI＋第二板、Linux a250113 BRCM 兩板，每平台 A→B 依序獨占、兩平台並行，名目各 2.5～3 天。M0c 先補 Windows 第二板負載與基線承接。
- **有條件建議 (c)**：降低 bench 與 Linux Canary 搶 service 的衝突；條件不成立就阻擋，不能退回未授權單板。新 OD 必須逐條寫明：Stage 3 的 24h 取代 spec／P10 的 48h 要求及 OD-10(1)；本輪硬體／排程取代 OD-102 的單板／錯開限制；OD-09／109 的凍結值與時序如何維持。時長變更本身是 owner 裁決提案，本稿沒有授權。
- 逐板最低負載若不能在保留原凍結檔下合法補足，由 B16 提出新增適用契約的明確 OD-09／109 裁決；不得直接編輯 YAML、將 COM0 門檻充作第二板門檻，或憑經驗認定等價。裁決後同步 spec、stage_exits title、程序及 §5 時程。

**A5. 外部擾動、VOID 與 FAIL 優先（S3A-7）**
- 現行規則：P10:1462-1466 產品／安全 FAIL 優先於 abort／INFRA；:2848 同 artifact FAIL 不被後續 PASS 抵銷；OD-09、spec:1702 保留 engine_result／release_gate_result 的 FAIL，安全不變量不得豁免。OS 重新列舉只證明事件，不能證明產品失敗無效。
- 選項：
  - (a) **另立新 OD 後才可能新增 VOID**：只允許開跑前定義、可證明因果的純基礎設施失效窗口；規定時間窗、OS 與產品雙側證據、提出者與 owner 判定權限、所有原始資料留存及 ingest 稽核。必須先排除已成立的產品 FAIL／安全 FAIL；任一成立、先後不明或因果不足，仍照現有 FAIL／INFRA 規則，不得 VOID。若改變任何非安全失敗的採計，逐條列出修改 OD-09 與 P10:2848 的部分並於新一輪前凍結；既有 FAIL 不回溯作廢，安全 FAIL 永不排除。獨立審查不能代替 owner OD。
  - (b) **維持現行結果模型**：擾動輪照實留存／登錄（已成立 FAIL 仍為 FAIL，純 INFRA 依現行規則），artifact 已有 FAIL 則重建 rc.N+1、重設 target，重跑全部必要證據與 Canary；不挑最好一輪。
  - (c) 個案人工接受：OD-110 不是一般豁免。無新 OD 不能改結果或放行；安全違規一律不可接受，因此不作合法發布捷徑。
- **改建議 (b)**。M0b／M1／M2 以既有結果模型排程；(a) 僅列待決提案，若 owner 要做，另加設計／實作／反例／獨立驗證 **1～2 週**到 RC 前置，核准且驗收前禁止啟用。
- VOID 的必要反例：先產品 FAIL 再 USB、先 USB 再證實安全 FAIL、無法確定先後，均不得作廢；後續 PASS 不能解除該 FAIL。不得將排除規則寫回凍結 YAML。
- M5 前降擾：i300、USB SSD 換 root hub 或停用約定、不插拔周邊；這些降低風險，並不改變判準。

**A6. 保底資源、配額與每週產能（S3A-A6）**
- 工具線 M1～M4 現估 9～14 週，另有 M0／環境及 S3-R；線 C 仍先 ci-thin→i304（本機 PLAN 線索），不能假設 M0b 1～2 週內它必然做完。
- 選項及後果：
  - (a) 0.5.1 先用全部資源，S3 後開工：其後承擔全部尚未完成的前置；至少 M1～M4 9～14 週，不能再引用 r1 的 4～8 週。
  - (b) ci-thin／i304 後固定給 S3：可提前工具線，但延後原 0.5.1 待辦；實際可扣重疊量以週產出核對。
  - (c) 只用空檔：無保底、無可靠總週數，等待及暫停另列，不能使用 §5 的持續產能假設。
  - (d) 保底：ci-thin→i304 後線 C 固定給 S3，每週 **5 個有效工作天、Windows 原生半天、安靜主機 4h 一窗**；M0c／M4R／M5 bench 另整段預約。建議至少保留每週可用 agent 額度 **25%**給此線（含獨立 review），另兩線不得借走；額度與 thala 減線衝突時由 owner 選延後哪一線並更新日期。
- **有條件建議 (d)**：上述份額是待 owner 核准的資源提案，不是目前已取得的 slot。首週量測工具／review token 消耗，填實際每週 token 上限、正常 Task 每週完成數；暫定正常 Task 每週 1～2 項、高風險 Task 每 1～2 週 1 項（含修正與審查）。若 25% 不足達 5 天產能，增加核准份額或上修週數，不能只改模型名稱。
- M4 的 cargo 仍等 inst4／c0c；B3 即使排進 0.5.1，仍另耗 1～2 週及原生 slot，延長 0.5.1 或排擠工具產能都要進 §5，不能當作「不增加總工期」。

**A7. 完整分母與逐案可執行性（S3A-1、5、10）**
- 基準不變：Stage 3＝slice_2.4∩layer∩platform，`gap` 不是豁免。Linux L1.5 85 案有 57 contract-blackbox；Windows L1.5 50 案含 14 native／7 blackbox／1 rust-l1；Windows L2＝34 regression＋1 native，L3＝37 realhw＋4 native。L3 Windows two_boards 28 案、L2 另有 f6-two-boards-no-starvation；remote 必須有各平台實際證據。
- 選項：
  - (a) **整批排 gap 的新 OD 提案**：會改變 P10:259／882、既有 Stage 3 分母及 cases 鎖；其中原生 COM 涉 OD-08、sm-04-device-unplug 涉 OD-32「不得縮減分母」、Windows remote connect／benches 涉 OD-14 #7（BC-05）。每案還須列其真正授權條款，不能以 Stage 0 規則或 gap 標籤概括取代。後果是降低已核准覆蓋；本稿不建議，未核准不得採用。
  - (b) **保留全部分母、補齊能力**：Windows TestPilot、B16 兩板與逐板負載、B15 原生 Windows OpenSSH／remote、Linux 對端、逐案破壞授權；M2 完成 §4.4-5 全部 runner／report，而非只加 native adapter。後果是增加 M0c、M2 及原生時段，但維持既有判準。
  - (c) **逐案等強度替代或例外裁決**：逐列給 case_id、原義務、替代 artifact／平台／runner／report／出口、尚欠證據及 decision_ref。真正改分母／平台範圍／判準者須由新 OD 明寫取代哪條 OD（含上述 OD-08／32／14 #7 與其他個案條款），走 cases revision／slice 鎖修訂鏈；只提前凍結不等於已獲准放寬。
  - (d) 不補也無新 OD：維持 BLOCKED，stage_status(3) 不能 PASS，1.0.0 不能發布；B5 不能自動解除此阻擋。
- **改建議 (b)**；若不可行，交 owner 逐案決定 (c)，不得預設 (a)＋最小 (b)。無授權破壞的案例仍在分母且阻擋，不能先排除再說適用集合已通過。
- M0 先凍結映射與範圍決定，M2 驗完整可執行性，正式 RC 前解完；報告 meta.tiers 不得反向決定 required。

**A8. evidence_root、必要檔案與 hosted 取得方式（S3A-A8）**
- 選項：
  - (a) 索引及完整必要檔案集合放 `docs/rust-refactor/evidence/`，hosted checkout 後可獨立驗證；帳本快照與歷史鏈亦在其中。
  - (b) 作為 release evidence 資產：M4 必須將確切檔名納入精確資產白名單、SHA256SUMS 及下載驗證，不能假設 validator 接受額外檔案。
  - (c) 外部儲存：hosted 必須按固定清單下載、驗 hash 並重建相對路徑；缺檔、過期、hash 不符即阻擋。增加下載／保存政策維護成本。
- **有條件建議 (a)**。必要集合是 index／Derived.files 及其遞迴引用的全部檔案，含被引用原始 log／附件；不是只放報告和 hash。大型檔只有未被必要集合引用才可純外存；若必要且過大，改採 (c) 並在 M4 交付下載／驗證，不得靜默省略。
- RC 後 docs 可追加證據，不將 repo 根 evidence/ 或 realhw/ 增量加進 guard。敏感內容逐案檢查；不因既有 secret_scan.allow 個案就自動放行新內容。
- M4R 要以乾淨 hosted checkout、沒有測試機本地路徑可用的情況，驗證全檔案可取與 hash，再以移除必要附件的反例確認 promote 阻擋。

### B 組：證據線開工前（B3 版本歸屬、B4 基線、B5 必要範圍、B16 拓樸須提前至 M0）

**B1. 凍結 Canary policy（BO-P9-06）**
- 要定的內容：活躍日門檻、計數與去重規則、循環類型與最低配額、單一類型上限（draft 為 0.4）、utc_offset_changes、兩平台證據責任人。
- 選項：
  - (a) 採用 draft 值。
  - (b) 依部署機的實際能力調整配額結構。
- **建議 (b)**，以 B2 的唯讀量測結果為依據。
- 必須早於第一筆計數。配額結構可調，**OD-10 的每平台 14 活躍日／5000 submits／50 cycles 不可下修**，至少三種類型有配額、單類型上限 ≤0.5、coexist 每 COM 每日上限及去重規則均保留；Windows 不計 flash。B2 量測不足時延長累計，不降門檻（S3A-B1）。

**B2. Canary 部署機**
- **Windows**：
  - 選項：
    - (a) 測試團隊的單人工作站（OD-12、P10:23）。
    - (b) 擁有者的 Windows host。
  - **建議 (a)。**
    - M0a 就指定工作站與責任人（BO-P9-08 要求能跑 Python 3.10+ 的 pyz，P10:89〔定〕）。
    - M4R 第一組以 0.5.1 彩排、第二組以 Stage 3 形狀候選彩排，各自核對 ledger／ingest／tree；collector 每機至少 2～3 天。M0a 先量測 Windows 端真實 submit 與各類循環，和 Linux 一樣決定可達標期，未量測不能承諾 Canary 3～6 週（S3A-B2）。
- **Linux**：
  - 選項：
    - (a) 本機 WSL 的日常使用，符合 OD-10「Linux＝擁有者日常使用」。
    - (b) a250113。這偏離 OD-10 的寫法，要修 OD-10。
    - (c) OPI 經橋接。期間要停本機正式 service，有衝突。
  - **建議：先量測再裁（F7）。**
    - M0a 期間由編排者以唯讀方式統計本機正式 daemon 近 1～2 週每天的 cmd submit 數，以及可分類的循環數，算出預期達標天數。
    - MTK 板只能讀、不能重開，所以 dut_reboot 為 0；hotplug 要經 usbipd 或實體拔插（BLOCKED-OWNER）。50 次循環要靠 handoff、coexist、recovery 湊，而健康的板子上 recovery 與 handoff 很少自然發生〔推〕。
    - 量測顯示可行就選 (a)，並把 Linux 長跑放在 a250113（A4(c)），兩者才不衝突。
    - 不可行時，請擁有者另訂「真實使用」協定，或改選 (b) 並修 OD-10。

**B3. windows-task 崩潰自動重啟（OD-83 B）**
- 選項：
  - (a) owner 擴充 0.5.1 範圍納入，另編 S3-R **1～2 週**：設計／實作約 3～5 天、Windows 原生及 Defender／契約／發布整合約 2～5 天；排擠其他包或延後 0.5.1 都要記帳。
  - (b) 0.5.2 或 Stage 3 前置包補做，同樣 1～2 週，另加新發版等待；M4R 等其完成，不帶未修候選開始 Canary。
  - (c) 不補：win-task-restart-on-failure 仍在凍結分母中而不成立，屬發布阻擋；若要放寬須新 OD 明列修改 OD-83 的長期 B、對應凍結案例與 Stage 3 覆蓋，不能只接受事故作為通過。
- **有條件保留建議 (a)**，由 owner 決定版本及資源。現有 OD-112／0.5.1 PLAN 未排此實作，不將建議當已納入；移階段不消除工期（S3A-B3）。

**B4. A 側 Python daemon／runtime 來源與共用 CLI 配對包（S3A-3）**
- 選項：
  - (a) 取已發布 0.4.1 的 Python daemon／runtime 為凍結來源，兩平台另組候選 CLI＋Python daemon 的 A 包。
  - (b) 取 M 的凍結 sw_core／runtime 來源，先列相對 0.4.1 的差異與 BC；同樣另組 A 包。
  - (c) 取 v0.3.2 的 daemon／runtime，需額外處理更舊配對及基線差異，工期風險更大。
- **建議修訂後的 (a)**，沿用 OD-102 的基線來源，但不沿用其整包。M2 交付製包及相容性驗收（已含約 1 週整合估工）；M4R 用彩排候選 CLI，M5 用最後 RC 的同一 CLI 位元組重建 A 包、凍結其身分。A／B CLI SHA 必須相同，任何不同 CLI 的比較都需要 owner 新裁決明改 P10:906 的比較契約，本文不建議。
- 需核對 `doctor` version_pairing、CLI／RPC／啟動相容、manifest 與 runtime tree；Python 產品碼不改，已核准差異依 BC。配對不成立就回報阻擋，不以名稱 0.4.1 推定可用。

**B5. 必要遺留證據的逐項裁決（BO-P9-14；S3A-B5）**
- `retirement verify` 目前只查 Stage 3 是實作範圍，不能取代 spec:1193 的必要證據義務。M0 建立完整遺留清冊（包含 stage0 的 747 項／725 missing 這份 r1 線索、Stage 2 Windows 鎖、OD-27／32、實體拔插／MCU／ST-12），先核對當前證據，不用三類摘要豁免剩餘項。
- 每列須有：item_id／平台／原義務及 OD／缺口／證據 artifact 和有效範圍／owner、補驗 runner／時程；處理只能是補驗、符合身分契約的有效沿用、或新 OD 逐項修改義務（明列被取代 OD 與後果）。沒有裁決／證據不能自動標不適用。
- 選項：
  - (a) 只保留 S3 16 項：是實質修改必要證據範圍，須列全體被解除義務及各自 OD，並明改 spec:1193；降低覆蓋，不建議。
  - (b) 保留全部既有必要義務：完整清冊逐項補驗／有效沿用，未解項阻擋 1.0.0；工期由逐案估工及設備可用性決定。
  - (c) 逐項 owner 決定：每列選補驗、有效沿用或新 OD，例外不可涵蓋安全不變量；若改 OD-27／32 的保留義務，必須逐名寫明，不能只在發布說明標 BLOCKED-OWNER 就當可發。
- **改建議 (c) 的逐項方案，核准前按 (b) 阻擋**。S3-E **1～2 週**做完整清冊、身分核對與裁決收斂；OD-27 彩排只能當預演，正式必要集合仍須最後 RC 驗證。Stage 2 Windows 鎖依 B12 原生 add-only 補齊。
- 已含於 M2／M4R／M5 的補驗不重複計工；其他尚未可估的補驗由清冊逐項加進前置或 M5 關鍵路徑，未完成清冊前不得把 §5 區間稱為全量總工期。

**B6. Python 產物的退役範圍**
- 要定的項目：
  - 停發 wheel 與 PyInstaller exe。
  - 移除 bundle 內的 py runtime 與 python_cli_delegate。
  - 取消 install.sh 對 python3.12 的需求（install.sh:4、9、112〔定〕）。
  - repo 是否保留 sw_core/。
- 選項：
  - (a) 1.0.0 前前三項全部移除，sw_core/ 保留。
  - (b) 只移除 bundle 內的 runtime，wheel 與 exe 再保留一版作為 legacy fallback。
  - (c) 連同 sw_core/ 一起刪除。
- **建議 (a)。** sw_core/ 是 A/B 基線重現所需，刪除要另行裁決（P10:3277）。
- 附帶事項（C12、C14）：
  - 停發 wheel 與 exe 會改變 OD-50「wheel 仍照發」與 OD-78「PyInstaller exe 續發」的核准範圍（s1.md:15、s2.md:15-17〔定〕）。依 spec:1274，要新增一筆 OD 取代這些條款。
  - install.sh 的修改與 README 中英段同步（R-18）要在**建 rc.N 之前**合入（§4.2-14）。
  - 1.0.0 發布說明、python-daemon-retirement.md 與 README，只能宣稱單人工作站信任模型（OD-12），不得宣稱多使用者或同使用者隔離（00-master-plan:33〔定〕）。windows-service 等多使用者部署要等 Spike 9，另行裁決。

**B7. Stage 3 全部破壞性集合的授權（C7）**
- 依 slice_2.4 統計，L2 每平台 7 案、L3 每平台 8 案是破壞性案例〔定〕。L3 的 8 案為 p1-ho-cycle、p1-ho-persist、p1-hp-cycle、p1-hp-reorder、p1-rst-bootwindow、p1-rst-daemon、p1-rst-reboot、p1-rst-recover。
- manifest 只要含破壞性案例，就必須附 authorization，而且 boards 必須是 dut.boards 的子集（P10:268〔審:完〕）。
- 要定：下列每一項的 decision_ref 與限定板子：
  - S3-L2-FULL-AB-LINUX、S3-L2-AB-WINDOWS。
  - S3-L3-P0P1-REMOTE-LINUX／WINDOWS。
  - S3-ROLLBACK-LINUX／WINDOWS（BO-P9-10／11 明定為破壞性，P10:90-91〔定〕）。
- **建議：**
  - Linux 用 a250113 的 BRCM 兩板，並附遠端使用者的同意。
  - Windows 用 OPI＋B16 核准第二板；未獲准實體拔插仍照 OD-92 維持 BLOCKED-OWNER，**案例保留在分母、阻擋出口**；A7／B5 只有逐名新 OD 才能改義務（S3A-B7）。

**B8. bench 排程、A→B 獨占（BO-P9-16）、安靜主機、Windows 登出**
- 要定的內容：
  - M4R 與 M5 的時段。
  - 和 thala 的不碰 USB 與磁碟約定。
  - 擁有者到場的日子：破壞集、D8、Windows 登出。
  - a250113 的停機時段。
  - 所有需要安靜主機的執行（§6），以及期間要暫停哪些線。
- 選項：
  - (a) M4R 開始前一次排好整段時程，並預留重跑窗口。
  - (b) 每輪結束後才排下一輪。
- **建議 (a)。** A→B 之間必須獨占，逐輪排程容易出現空檔或被其他工作插隊。

**B9. 延後 issue 的版本歸屬（#182、#171、#280、#269、#233、#228）**
- 選項：
  - (a) 全部放在 1.0.0 之後。
  - (b) 挑幾項進 1.0.0。
- **改為有條件建議 (a)**：M0 逐 issue 與凍結 case／能力映射核對，只有不影響任何 Stage 3 必要案例的一般功能才可延後；必要能力先納前置修復並逐項估工，或交 owner 新 OD 明列修改原覆蓋條款。OD-112 的「不在 0.5.1」不授權晚於 1.0.0（S3A-B9）。
- #270 在既有延後清單；本輪未核對其 issue 原文，不推定已獲「1.0.0 之後」授權，同樣先核對必要案例。target 後不得為延後功能改凍結測試。
- #264 不在本清單：OD-112 已把它的文件修正納入 0.5.1（v051:42〔定〕；C13）。

**B10. low-backlog 與 ledger-inbox 的 Low 收尾包**
- 選項：
  - (a) Python 項目改列已知限制後關閉，Rust 項目在 target 凍結前修完。
  - (b) 全部修。
- **有條件建議 (a)**：OD-111 凍結的是 Python 產品程式碼；依 PA／PA2 v051:45，**sw_core/assets/ 仍出貨資產明准修改**，文件、tools shell 等要依出貨用途及必要案例逐項處理，不能整個 sw_core 目錄關閉待辦。凍結程式碼的已知限制仍須核對是否影響 Stage 3 gate；涉及必要案例不能逕行關閉（S3A-B10）。
- 先把 ledger-inbox 的 76 行 Low 與 149 行既有碼待辦去重分類〔讀:bl〕。

**B11. foreign_holders 審查殘留（2f19aba7）**
- 背景（E4）：
  - sol 補審時留下殘留：foreign_holders 相鄰配對約 45% 非乾淨（p9a pending-sol-review.md:15〔定〕）。
  - 之後的 2f19aba7 已實作易變規則，等於採了殘留紀錄中「另定容忍規則（須獨立審閱）」那一項。這條規則只忽略成功的 daemon status／health.status 回應最上層的 foreign_holders。
  - 2f19aba7 沒有審查紀錄〔定〕。
- 選項：
  - (a) **先獨立審查 2f19aba7，通過後再請 owner 追認**；證明危險 holder／unknown-holder 情境仍有嚴格且決定性的測試。
  - (b) 撤回 2f19aba7，改用確定性案例覆蓋。
- **有條件建議修訂後的 (a)**。M1 審查須驗成功 daemon status 的頂層快照以外仍嚴格；增加危險 holder、holder_unknown、錯誤回應及巢狀同名欄位不得被正規化掩蓋的決定性反例，安全 invariant 必須獨立成立。未證明就採 (b) 或保持阻擋。ab_normalization 只能追加且改 contract_revision，故在 target 前定案（P:ab_diff.py:53-54、669-681；S3A-B11）。

**B12. Windows L1 鎖的負責人與收集節奏（F12）**
- 選項：
  - (a) 編排者每週固定集中收集一次（併 M1～M4 的原生驗收），target 凍結前再收最後一次。
  - (b) 0.5.1 發布前集中收集一次。
  - (c) Stage 3 target 凍結前才收集。
- **建議 (a)。** 收集範圍：
  - `s3_release_contract.windows`。
  - PR 291 新增的節點（Stage 2 遺留）。
  - 0.5.1 各包的節點。
  - 7 列能力測試已登記，不在範圍內（E1）。
- ci-thin 的 brief 要把 #291 補進去。

**B13. P9 的實作與審查路由**
- 選項：
  - (a) 沿用現行路由：luna max 實作，加 terra xhigh 審（11-23 11:08 起，RS:667〔機〕）。
  - (b) 依 plan 10 §0.1／§2 原訂路由。
  - (c) 重型 Task（T6、T14B、T16）改由 Opus 實作。
- **建議 (a)。**
  - 高風險 Task 9 項各做一次單審。
  - Phase 層級雙審改為 terra 加另一位非實作者（C11）。
  - 放行依可執行證據，模型名稱／輪次不當作驗收：必含 P0-only／缺案反例、候選登記不改 frozen tree、錯 Windows 入口／artifact、全部 runner 分母覆蓋、Stage 3 資產／hosted 全證據及跨版本 D0～D8。僅修改、具體失敗或尚未滿足條件才補對應複審（S3A-B13）。

**B14. 正式及彩排的回滾組合（S3A-9）**
- 選項：
  - (a) 正式 **Current＝最後 1.0.0 RC、Previous＝0.5.1**；M4R 第一組 **Current＝0.5.1、Previous＝0.5.0**，第二組 **Current＝可丟棄 Stage 3 候選、Previous＝0.5.1**。三組 Current／Previous 皆為不同版本／內容並記 manifest／tree。
  - (b) 另選有支援義務的 Previous：需 owner 核准、補相容性與兩組彩排驗證；若無法測出真實版本切換，不能用於 D0～D8。
- **建議 (a)**；正式 Previous 的 0.5.1 建議保留，撤回 r1 全用 0.5.1 彩排的做法。
- M4 建版本相容檢查與隔離 start-fail 來源製包程序；M4R 分別建與彩排 Current 配對的 D7 corrupt／D8 start-fail 來源，M5 再以正式 Current 重建，不重用錯版本來源。兩平台都記 `--expect-previous`、D1 指紋，D6／D8 須回到 D1 同一 Current 的完整內容。
- state.json／config／監管定義相容、舊版可讀、Current／Previous 保留照 spec:1110、1112。`run-upgrade.sh` 的 Linux docker 方法只補相容訊號，不能代 Windows 原生 D0～D8。M4R 第二組建置 0.5 週＋完整證據 4～8 週條件式估算已計入 §5，正式最後 RC 仍全部重跑。

**B15. remote tier 環境（新增，C8）**
- 要定：
  - 由誰準備 sshd 與 docker 主機，在哪台機器準備：WSL、Windows，或 a250113。
  - 在 Windows 提權安裝 OpenSSH server，要擁有者同意，因為 interop 以系統管理員權限執行〔機〕。
- 選項：
  - (a) 提供 remote 拓樸，M4R 先彩排。
  - (b) 不提供，依 A7(d) 處理，但這樣 S3-L3-P0P1-REMOTE-* 無法滿足。
- **有條件建議 (a)**。Linux 端可用 a250113 或本機 docker 加 SSH 對端；**Windows 必須另有 Windows-native OpenSSH／remote connect／benches 及實際受測 artifact 的證據**，Linux 對端只提供拓樸，不替代 Windows 平台證據。負責人、權限、每案 runner／report 於 M0c 落地，M4R 實驗（OD-14 #7、P10:338-339；S3A-B15）。

**B16. Stage 3 專屬拓樸與 OD-109 凍結負載（提前至 M0；S3A-8）**
- 已核對：PA:s2:542-546 的 OD-102 明定當輪 OPI 單板；M:realhw/acceptance/README.md:53 直接指出與 `lr-mixed` two_boards 不一致；M:longrun_acceptance.yaml:12、130-138 只有 lr-mixed engine、Windows COM0 的最低有效負載。補第二板不會自動補出凍結門檻，排除 two_boards 又會排掉 required lr-mixed，兩者都不是現成完整方案。
- 選項及後果：
  - (a) **兩平台完整兩板，建議方案**：Linux a250113 BRCM 兩板、Windows OPI＋核准第二板。owner 新 OD 明列 Stage 3 不沿用 OD-102 單板範圍；保留原 YAML／OD-109 數值／歷史，先核定逐板／來源對應及新增第二板的基線、最低負載、資源及比較方式。若需 Stage 3 附加契約，必須由新 OD 明確修訂 OD-09／109 的適用範圍與消費規則，凍結早於對應 Stage 3 候選；原門檻不得降低，原證據及 earliest start 不重設。尚未確定這種承接能合法完成前，維持 BLOCKED，不能實作者自行開新檔繞過凍結。
  - (b) **保留單板的放寬提案**：須逐名列出被修改的 Stage 3 two_boards 案例（包含 lr-mixed）、長跑固定出口與 engine 的替代覆蓋，明改 OD-102 的 Stage 3 適用性、OD-09／109 負載契約、P10:259／338-341 及相關 spec；排除案例不是完整替代。後果是降低兩板覆蓋，本文不建議；無新 OD 不得實施。
  - (c) 無法合法承接凍結負載／無設備或授權：保持阻擋，1.0.0 延後；不交給 B5 自動豁免。
- **有條件建議 (a)，在 M0a／M0b 裁決、M0c 落地，禁止等 M2 單板結果再二選一**。M0c 2～4 週已含環境及必要先行基線／負載收斂；若 owner 選擇的合法方案需要更多基線或無法保留既有凍結判準，先回 owner 新裁決並重算工期，不能將本稿視為已授權變更凍結集合。

---

## 8. 風險與對策

| # | 風險 | 對策 |
|---|---|---|
| 1 | p9a 的未提交檔在清理 worktree 或磁碟時遺失（含 ignored 的 progress.md）；已提交的 tip 本身無法 import〔本〕〔讀:impl〕 | 第一步就保全（§1）；保全前不得清理該 worktree |
| 2 | **凍結後修工具、案例或 L1 集合，就等於全部重跑**（P10:2847、3100〔定〕）；0.5.0 長跑期間工具改過兩次〔機〕（C1、F4） | main 可以前進，但產生證據的 worktree 固定在 target 的 tool_revision；M4R 彩排先把工具缺陷洗掉；工具缺陷一律開新 rc 並重設 target。若要放寬成「工具樹指紋」，必須先在 M0b 修訂 P9 並經審查 |
| 3 | Stage 3 target 凍結後才出現 runtime 修補（0.5.2、延後 issue、事故修正），A/B 與 Canary 全部過期 | target 以 0.5.1 發布、chk3 定案、S3-R／S3-E 完成、M4R 依 owner 選定深度完成且限制已記錄為前提；事故修正一律開新 rc 並重新累計；M5 編列一次 Canary 重置（約 +3 週，F3） |
| 4 | 移植衝突與語意漂移：p9a 落後約 1514 個 commit，Task 16 碰到的 Rust 與 release 檔案大幅改過 | 採 §7 A1(b) 逐 Task 移植；T16 排在 inst4、c0c 之後；開工前先做差異盤點 |
| 5 | OPI USB 擾動（共用 hub、插拔周邊）讓長跑或 A/B 判 FAIL，而 FAIL 不能抵銷（P10:2848） | 預設 §7 A5(b)，產品／安全 FAIL 優先，VOID 僅待決；i300 在 0.5.1 合入；長跑期間 thala 的 USB SSD 改接其他 root hub 或暫停使用，並約定不插拔周邊；重跑預算見 §5 |
| 6 | **Windows 部署內容指紋解不出**，Windows 證據全部被拒收（0.5.0 前例〔定〕；C3） | M2 統一入口／版本根／component 身分（§4.4-4）；保留精確 component 路徑，驗 sidecar／複本 SHA／實際 daemon／前後 tree／ingest；非只改 config |
| 7 | **分母不可達**：Linux／Windows 非 func-test runner、Windows gap／native、two_boards 逐板負載、remote、TestPilot | M0a／S3-E 逐案盤點；B16 在 M0 裁決，M0c 落地；M2 全部 runner 承接、缺案反例驗收，任何無授權案例仍阻擋 |
| 8 | Windows Canary 沒有工作站；windows-task 不會在崩潰後自動重啟，事故一直累積 | §7 B2 在 M0a 指定工作站；B3 建議在 0.5.1 補上；collector_gap 與 unexpected_restart 的結案流程寫進 runbook |
| 9 | 兩板只有 a250113，得依賴遠端使用者的 sudo、push 與停機同意（M5 含重跑約 9～12 天） | M4R 前先還原 service、取得書面同意與破壞性授權；遠端執行步驟寫成可交付的 brief |
| 10 | Windows L1 鎖缺口持續擴大，原生執行只有編排者能做（單點瓶頸） | §7 B12 每週固定收集；§6 編排者原生時段 |
| 11 | codex 額度與 TPM 被 0.5.1 與 S3 工具線同時消耗；terra xhigh 的用量未重估 | 依 §7 A6(d) 的保底份額；用到 80% 時先停 0.5.1 中非關鍵的包 |
| 12 | 規格與實際做法多處偏離（48h／24h、錯開／並行、人工接受、RC、分母），1.0.0 gate 被質疑事後改門檻 | M0b 把 owner 決定寫進 rulings 並同步 spec／plan 10；預先宣告不等於授權放寬，沒有新 OD 仍依原分母／門檻／FAIL／凍結規則阻擋 |
| 13 | Canary 判定保守：新事故會撤銷已登錄的 PASS（P10:2849）；活躍日只能從頭算 | M4R 先做 shakedown；M5 編列一次重置；事故分診每天做 |
| 14 | 部署內容指紋不符（旁載檔、漏設 SERIALWRAP_CLI_EXE 或 SERIALWRAP_DAEMON_EXE），ingest 拒收 | runbook、M2 預演與 M4R 彩排都要實際走過 ingest；Canary 機以 archive 原樣安裝 |
| 15 | Linux Canary 和 Linux 長跑搶本機正式 service | 依 A4(c)，Linux 長跑放在 a250113；Linux Canary 依 B2 量測結果決定 |
| 16 | 裁決分散在 main 與 PA 兩條分支（雙向）：只讀 main 會漏 OD-77～112，只讀 PA 會漏 OD-109、110 | M0b 處理雙向合流；合流前 brief 同時列兩處路徑 |
| 17 | Python 基線本身的雜訊（a250113 BRCM COM0 的 FLUSHED_BY_RECOVERY、#294 B 型窗口）讓 A 側變成 BASELINE_INVALID，或讓相對門檻判 INSUFFICIENT。0.5.0 的 Windows 候選就有 error_rate_relative 判 INSUFFICIENT 的前例〔讀:bl〕 | 選板前先檢查該板的基線雜訊；A/B manifest 事先宣告是否做 B/A 複驗；基線無效時重跑 A 側，不改門檻 |
| 18 | windows-native 的 Windows 登出會中斷整個 WSL（所有線、thala、本機 Canary） | 由擁有者挑時間，事先通知 thala，先讓所有線停在段落 |
| 19 | E 槽容量（50G）在三條 0.5.1 線加 M4 時超限 | M4 的 target 放 D 槽，或等某條 0.5.1 線刪掉 target 後再開 |
| 20 | RC 之後才改 install.sh、README.md 或 tools/，promote guard 會擋下（release_promote_guard.py:52-58〔定〕） | 這些變更全部排在 rc.N 之前（§4.2-14） |

---

## 9. 附錄：來源清單

**r2 親自核對〔修2〕**（SHA 定義見開頭；以下表內簡稱對應完整路徑）
- `owner-decisions`＝M:`docs/rust-refactor/owner-decisions-2026-10-15.md`；`acceptance/README.md`、`yaml` 分別簡寫 M:`realhw/acceptance/README.md`、`realhw/acceptance/longrun_acceptance.yaml`；`guard`＝M:`scripts/rust/release_promote_guard.py`。
- `cases.yaml`／`cases.lock.json`＝M:`tests/acceptance/cases.yaml`／`tests/acceptance/cases.lock.json`。以 slice_2.4 ID 集合與 layer／platform 交集重算 suite 分布，沒有改鎖或執行案例。
- `ab_manifest.py`、`ab_runner.py`、`evidence_ingest.py`、`od09_consume.py`、`ab_diff.py`＝P:`tools/stage3/` 下同名檔；`suites/*` 亦在該目錄。分別核對 :313-320、392-402、441-464、705-715；:137-153、288-301、358-364；:529-534、854-864、944-986、1175-1180、1395-1399；:1-11、51-85；:53-54、669-681。
- M:`realhw/acceptance/od09_freeze.py`:40、589-605、772-814；`longrun_acceptance.yaml`:12-16、118-150；`realhw/acceptance/README.md`:53；`realhw/deployed_content.py`:71-120。
- M:`scripts/rust/release_assets.py`:100-166、220-245；`release_promote_guard.py`:52-77；`.github/workflows/release.yml`:110-140、197-218；`crates/sw-cli/src/lib.rs`:54-57。
- M:P10:171-181、219、259-268、332-347、476-484、906、1462-1466、2673-2684、2847-2849、3100；spec:1185-1195、1698-1702；`docs/rust-refactor/owner-decisions-2026-10-15.md`:12-19、30、35（OD-08／09／10／12／14 #7／27／32）。
- PA／指定裁決 ref 的 `docs/rust-refactor/reviews/rulings/s2.md`:149-155、455、542-553、621-638；v051:7、21-22、40-45；PA2 v051 後段另讀到 :158-167，不把 ci-thin 的個別驗收命令豁免延伸成 Stage 3 減分母授權。
- 本機唯讀 `（編排者本機的 0.5.1 計畫檔）` 全文：未排 OD-83 B；延後清單只界定不在 0.5.1。未開網頁、未核對即時 issue 狀態。
- 本輪僅寫本草稿；未執行 cargo／pytest／docker、interop、正式 daemon／設定／狀態／UART 操作。以下 r1 來源及未查證項保留歷史性質。


**定稿時親自查證〔定〕**（origin/main e50d4b3b，除非另註）
- plan 10：
  - :25（docker L1.5 前置）、:40（target 凍結 tool_revision 等）、:72-76（TDD 兩輪、紅燈清單）、:89-91（BO-P9-08／10／11）。
  - :121（s3_release_contract 登記）、:259、:332-347、:374（carryover counters）、:476-484、:882、:1467、:1682。
  - :2817、:2847-2849、:3090、:3100、:3305-3323、3336、3744（pack_bundle、verify、verify_archive 與 CLI exit 2）、:3370-3376、:3734、:3812-3822。
  - 全文沒有 `windows-native`、`win-task`。
- `tests/acceptance/l1_required.lock.json`：
  - 沒有 `s3_release_contract`。
  - 7 列能力測試已登記在 `p7_gate:win-test-sh-caps-windows-com`、`s22_gate:windows_cap_windows_com`、`s21_gate:cap_supervisor_runtime` 的 windows 集合。
- `tests/acceptance/cases.lock.json` 的 slice_2.4（187 案）對照 cases.yaml：
  - 各 layer、平台與 windows_status 的分布。
  - 破壞性案例 L2 每平台 7 案、L3 每平台 8 案。
  - two_boards：Windows L3 28 案、L2 1 案。
  - remote 與 windows-native 案例清單。
- 長跑證據：
  - `evidence/stage2/longrun/windows/release-gate.json`：deployed_content 的 start 與 end 都是 unresolved、tree 為 null。
  - `linux/release-gate.json`：有完整的 tree。
  - `evidence/stage2/longrun/README.md`。
  - `realhw/acceptance/candidate_longrun_starts.ndjson`：兩筆，都是 10:46Z。
- OD-09 凍結：
  - `realhw/acceptance/od09_freeze.py`:50-59、770-779。
  - `longrun_acceptance.yaml`：sha256 等於 od09-freeze.json 的 acceptance_sha256；:15 為 86400。
- 其他 main 檔案：
  - `realhw/deployed_content.py`:16、71-79。
  - `realhw/acceptance/README.md`:53。
  - `scripts/rust/release_promote_guard.py`:52-58（放行路徑）、61-80（RC 須為正式 tag 祖先，差異只能觸及放行路徑）。
  - `.github/workflows/release.yml`:414-416。
  - `scripts/rust/slice_breaker.py`:39-41、95-112；`tests/acceptance/breaker/slice_2.{1,2,3,4}.snapshots.ndjson`；`slice_2.4.json`。
  - `tests/acceptance/check_evidence_index.py`:398-407。
  - `install.sh`:4、9、112。
  - `docs/rust-refactor/plans/00-master-plan.md`:33、93。
  - `serialwrap-rust-spec-final.md`:1110、1112、1125-1128、1698-1700。
  - `owner-decisions-addenda.md`:126-127，PA 同檔沒有這兩列；`git rev-list --count PA..main` 為 1424。
  - `docs/rust-refactor/slice22-exit-report.md` 存在。
- PA rulings：
  - s2.md:15-17（OD-78）、:264-268、:305-310、:389-395（OD-87）、:455（OD-94）、:538-556（OD-102 與其修訂）。
  - v051.md:36-48（OD-112）。
  - s1.md:15（OD-50）。
- p9a：
  - p9a 的 tip 即 2f19aba7（worktree HEAD 亦同）。
  - 24d415ff 建立 `tools/stage3/__main__.py`（46 行）；eecd4882 擴充（669 行）。
  - `027b296e..p9a` 只有 abe7fb32、f10db782 改過 plan 10；main 在 merge-base 之後只有 f431f1c0 改過。
  - p9a 與 main 的 `pending-sol-review.md` 審查紀錄不一致。
  - worktree：`git ls-files --others --exclude-standard` 為 23 檔；`status --short` 為 36 行；ignored 的 `.superpowers/sdd/10-stage3-canary/progress.md`。
- p277（ddd642aa）：對 main 的 diff 共 12 檔，沒有 T16 的檔案。
- 本機：
  - `b-log/rust-refactor/exec/s1/od27/windows/report.md`:110-118。
  - RS:564-598、626-635、655-668。
  - `exec/REVIEWER_OVERRIDE`＝terra。
  - `v051/PLAN.md`「其他」表（c0c、chk3、ci-thin、i304）。

**草稿彙整時查證〔本〕**
- `git -C …/rust-refactor-p9a status --short`：16 個已追蹤檔有修改、20 筆未追蹤項（共 36 行）。
- plan 10 的 :26、:28、:95、:2854、:2987、:3318-3321。
- `v051/ci-thin/launch.log`：10:56 NEEDS_ORCHESTRATOR，原因是 tests/contract_blackbox/test_cli_mgmt_update.py:473。

**審查者查證，定稿者未逐行重驗〔審:完〕〔審:可〕〔審:證〕**
- P10:268（破壞性 authorization）；P10:2675、2678（D2／D5）。
- realhw-stability-checklist.md:181；realhw/preflight.py:98-108。
- od27-local-run-20261109.md:58。
- docs/rust-refactor/deploy/windows-remote-live-e2e.md。
- s2.md:518（OD-100）。
- P10 §2 模型分工表的 luna 指派。
- main 的 pending-sol-review 沒有 T15。
- slice22-exit-report 的章節內容。
- RS:71、499、518、561-563、592；ledger-inbox.md:1085。

**reader 查證（行號見各 reader 原始輸出）**
- 〔讀:p9〕plan 10 全部 Task、BO-P9-01～18、S3 出口 16 項；mkbundle.sh:8、14-15、122、594-656；release.yml:233-234、294、339-347、407-408、571-578；contracts/release_manifest.schema.json。
- 〔讀:spec〕spec-final §0.2、§11.5、§11.6（:1064、1070、1072）、§12.1、§12.2（:1185-1197）、§14.2、§15.9、§15.11.4；spec-errata TST-D-1、TST-D-2；00-master-plan:63-67、100、332-349；owner-decisions-2026-10-15.md:14、19、30、36。
- 〔讀:impl〕p9a 各 commit 與審查狀態；stage0_evidence_index.yaml 統計；l1_required.lock.json 在 12eb9a0e 前後的差異；evidence/stage2/longrun/*；本機 gate 輸出。
- 〔讀:od〕owner-decisions-2026-10-15.md（OD-01～51、74、75）；PA rulings s1.md:46、75-87；s2.md:149-155、257-275、414-460、492-500、518-627、629-638；v051.md:3-54；install.sh:4、9、38；longrun_acceptance.yaml:2-160。
- 〔讀:bl〕PA low-backlog.md；issue-triage-20261123.md；gh issue #182、171、228、233、264、269、270、280；brief-common.md 第 4、8、9、12 條；ledger-inbox.md（:374、376、870、1034、1040、1082-1085）；od27-local-run-20261109.md；rust.yml:238、tests.yml:721。

**本機非 repo 來源與記憶〔機〕**
- ledger-inbox.md、RUNNING-STATUS.md、v051/PLAN.md、brief-common.md。
- 記憶 heavy-slot-cpu-coordination、cargo-target-on-thala-e、opi3-test-board-com6、agent-path-lacks-windows-interop、wsl-interop-runs-elevated、windows-logout-coordination、review-convergence-rules、rust-refactor-review-routing。

**未查證〔未〕**
- self-hosted runner `serialwrap-com` 是否已註冊。
- codex 下次重置時間，以及 terra xhigh 的用量。
- S1B-L3-MIXED-48H-WINDOWS 是否執行過。
- p9a 的 T4、T5、T5B、T6、T8～T13、T14B、T16 是否有本表以外的審查紀錄。
- 本機是否可跑 docker。
- 單板時 two_boards 案例的實際判定。
- 每次 L1.5 A/B 的實際時長。
- inst4、c0c 是否與 T16 的檔案重疊（待它們設計定案）。

---

## 審查處理紀錄

判定：**採納**（照 finding 修改）、**部分採納**（採納主要內容，不採的部分附理由）、**不採納**。下列 C／F／E 為 r1 三位審查者的歷史處理紀錄，共 44 項（採納 41、部分 3、不採納 0）；保留原文以追溯。**r2 對 C2／C3／C10／E7／F5／F6 等處理的修正，以本文現行章節及末尾 S3A 表為準，歷史表不是仍有效的實作指示。**

### 完整性（C1～C15）

| ID | 嚴重度 | 判定 | 處理 |
|---|---|---|---|
| C1 | High | 採納 | 已重驗 P10:40、1467、2817、2847、3100。§1-2、§3 #17、§4.2-2、§8 #2；tool_revision 建議取 rc commit，與 promote guard 一致 |
| C2 | High | 採納 | 已重驗 P10:2848。§7 A5 改為兩層：VOID 機制在 M0b 實作，機制完成前預設登錄 FAIL 並重建 rc；排除規則不得寫進 yaml |
| C3 | High | 採納 | 已重驗 Windows release-gate.json 的 unresolved／null，以及 deployed_content.py:71-79。§4.2-5、M2 驗收、§8 #6 |
| C4 | Medium | 採納 | 已重驗 P10:3305-3323、3336、3744、release.yml:414-416。§3 #3、§7 A3 拆成兩段；新增 A8（evidence_root）；另補發現：RC 後 guard 不放行 install.sh、README，見 §4.2-14 |
| C5 | Medium | 部分採納 | 採納：yaml 與 item_id 不得修改的硬限制（已重驗 sha 與 od09_freeze.py）。**不採**「選 48h 等於放棄 OD-109 凍結、應刪除此選項」：`min_continuous_duration_s: 86400` 是下限，跑 48h 不必改 yaml，也不必重新 freeze。真正的問題是 48h 候選與 24h 基線凍結值的可比性，A4(b) 已改寫為「需另裁可比性」並保留選項 |
| C6 | Medium | 採納 | 已重驗 P10:25、00-master-plan:93、scripts/docker/run-l15.sh。M5 順序與驗收；§6 新增 docker 主機列 |
| C7 | Medium | 採納 | 已重驗破壞性統計、P10:90-91、3734、spec:1110、1112。B7 擴大為全部破壞性集合；新增 B14 |
| C8 | Medium | 採納 | 已重驗 rm-* 案例的能力與 od27 report:117。§6 新增 remote 列；新增 B15；併入 A7 |
| C9 | Medium | 部分採納 | 採納：M1 的實機欄、`s3_release_contract` 凍結時點、§4.2-3 改寫（已重驗 lock 沒有該集合、P10:121、476-484）。修正處：fix 寫「PR 291 與 7 列能力測試改列 B5」，但依 E1 查證，7 列已登記，不需處理；只有 PR 291 移入 B5／B12 |
| C10 | Medium | 採納 | 已重驗 README:53、two_boards 統計。新增 B16；M2 加單板預演；單板實際判定標為〔未〕 |
| C11 | Low | 採納 | §2 補充加 7B；M4 驗收加全套 pytest、test_resource_markers.py、Phase 雙審 |
| C12 | Low | 部分採納 | 採納：B6 以新 OD 取代 OD-50 與 OD-78 的相關條款、T17 兩支測試連動修訂、install.sh 與 README 工作項。**不採**「可以排在 target 之後」：release_promote_guard.py:52-58 規定 RC 之後只放行 docs/、changelog.d/、CHANGELOG.md 等，install.sh 與 README.md 在 RC 之後修改會擋住 promote，所以改為必須在建 rc.N 之前合入（§4.2-14） |
| C13 | Low | 採納 | 已重驗 v051:42、44。B9 刪除 #264，並註明 #264 的文件修正屬 0.5.1 |
| C14 | Low | 採納 | 已重驗 00-master-plan:33。B6 與 M6 驗收加入單人工作站信任模型的限制 |
| C15 | Low | 採納 | A1(b) 補紅燈清單要求；A2 與 §4.2-4 補版號升級時點；changelog collate 放在 promote commit（guard 放行） |

### 可行性（F1～F15）

| ID | 嚴重度 | 判定 | 處理 |
|---|---|---|---|
| F1 | High | 採納 | §5 關鍵路徑改寫為區間公式；A6 各選項標出週數影響；新增選項 (d) 保底份額並作為建議 |
| F2 | High | 採納 | 已重驗 spec:1127-1128 的前提假設。M5 改為依資源由下而上估算，以 0.5.0 重跑率取倍率；降擾措施列為前置 |
| F3 | High | 採納 | 已重驗 P10:374。M5 編列一次 Canary 重置；新增 M4R shakedown；runbook 事先寫明哪些事故要開新 rc |
| F4 | High | 採納 | 和 C1 合併處理；新增 S3-M4R 實機彩排；§8 #2 |
| F5 | High | 採納 | 已重驗分母規則（P10:259、882、1682、3090）、cases 統計與 od27 report:116-118。新增 A7、M0a 可執行性盤點、§4.2-9 |
| F6 | Medium | 採納 | 已重驗 slice_2.4 的 windows-native 19 案、P10 grep 為 0。A7、§3 #18、M5、§6 Windows 登出；B3 改建議 (a)，避免 0.5.2 讓 M5 延後 |
| F7 | Medium | 採納 | B2 改為先量測再裁；A4(c) 讓 Linux 長跑放 a250113。補充一點：Linux Canary 改用 a250113 會偏離 OD-10「擁有者日常使用」，要修 OD-10 |
| F8 | Medium | 採納 | M0a 指定 Windows 工作站；M4R 在該工作站彩排 2～3 天 |
| F9 | Medium | 採納 | 和 E2 合併；已重驗 slice_breaker.py。§4.3 改寫，並在 §1 列為第一步之一 |
| F10 | Medium | 採納 | §6 heavy-slot 列改為「heavy-slot 與安靜主機」並列出各次執行；B8 擴大 |
| F11 | Medium | 採納 | §6 a250113 列加停機天數與書面同意；§4.2-13 |
| F12 | Low | 採納 | §6 新增編排者 Windows 原生時段列；B12 改為每週固定節奏 |
| F13 | Low | 採納 | A 組項數已更正（現為 8 項）；§3 #13 與 B13 改為 terra；codex 列標注 terra 用量未重估 |
| F14 | Low | 採納 | §6 磁碟列：M4 的 target 放 D 槽，或等 0.5.1 線清出空間 |
| F15 | Low | 採納 | M0 拆成 M0a（裁決與盤點）和 M0b（改版收斂，以 2～3 輪估算） |

### 證據（E1～E14）

| ID | 嚴重度 | 判定 | 處理 |
|---|---|---|---|
| E1 | Medium | 採納 | 已重驗 7 列分別位於三個 windows 集合。§4.2-3 刪去這類缺口；B12 範圍縮小 |
| E2 | Medium | 採納 | 已重驗 :39、41、103-112 與快照。§4.3 寫明末筆日期、≥10 pp、tripped 與 insufficient 的差別、≥56／187、OD-87 漏一週 |
| E3 | Medium | 採納 | 已重驗兩筆 10:46Z 的開跑登記、s2:455 與 s2:540。§3 #2 寫明並行；A4 新增 (c) 並作為建議；B16 追認用板。基線兩輪是否並行仍未查證，所以沒有寫 |
| E4 | Medium | 採納 | 已重驗 2f19aba7 的內容、日期、祖先關係，以及審查表沒有這一筆。T7B 列與 B11 改寫 |
| E5 | Low | 採納 | §2 補充改寫為 9 項中只有 7B 有補審，並記下 main 與 p9a 的 5 項不一致 |
| E6 | Low | 採納 | 已重驗 p277 的 12 個檔。M4 不必等 p277；inst4、c0c 的重疊標為〔推〕 |
| E7 | Low | 採納 | 已重驗 guard:52-58。A3 只新增 retirement json；另補 repo 根目錄 `evidence/` 不在白名單（A8） |
| E8 | Low | 採納 | 已重驗 plan 10 的 log。§4.1-3 改為三方比對 |
| E9 | Low | 採納 | RS:667、668 與 REVIEWER_OVERRIDE 已讀到（本機來源）。B13 與 M0b 改為 terra；A6 補上線 C 之後接 i304；§3 #13 補 luna。檔內時間順序錯亂（11:35 那筆排在 11:08 之前），所以這兩筆仍標〔機〕，請編排者確認 |
| E10 | Low | 採納 | 已重驗 23 檔、36 行、ignored 的 progress.md。§1、§4.1、§8 #1 |
| E11 | Low | 採納 | 已重驗 addenda 的兩列與 1424。§3 #14、§8 #16 改為雙向 |
| E12 | Low | 採納 | 已重驗 slice22-exit-report.md 存在，以及快照行數。§4.3 改寫；報告章節內容標為〔審:證〕 |
| E13 | Low | 採納 | 已重驗 spec:1698-1700。A5 的引用改為 P10:40 與 spec:1700 |
| E14 | Low | 採納 | 已重驗 24d415ff 與 eecd4882 的 `__main__.py`。§3 #16 照先例寫；T16 的依賴欄移除「依賴 T17」 |


### 第 1 輪 astra 審查（S3A-*）

本輪先核對原始物件，再改本文；沒有實跑或改 repo。**10 項 finding：採納 10、部分採納 0、不採納 0；A1～A8／B1～B16 的 24 項意見亦逐項採納其限制或保留其同意方向。合計 34 項：採納 34、部分採納 0、不採納 0。**「採納審查意見」只表示修正規劃與建議，不代表 owner 已裁決或工程已通過。

| 編號 | 處置 | 改了哪裡（節名） | 理由與出處 |
|---|---|---|---|
| S3A-1 | 採納 | §4.1-5、§4.4-1、M1／M2、A7 | 核對 P:evidence_ingest.py:854-864 確實以 meta.tiers 縮 required，:944-986 可能在縮小集合 PASS。改由凍結出口決定必要 family／case，補 P0-only／缺 remote 反例，不改 P10:338-339 的完整集合。新增工期含 M1／M2。 |
| S3A-2 | 採納 | §4.2-2、§4.4-2、M1／M2／M4、A3／A8 | M:od09_freeze.py:40、772-814 預設 append realhw；P:ab_runner.py:288-301 未傳外部 ledger，:358-364 隨即驗樹；P:ab_manifest.py:441-464 不排 ledger／lock。分離不可變工具與帳本，保留完整歷史前綴，registration／T15／ingest 同源且拒缺漏；不刪登記、不免驗 realhw，promote 不放行其增量（M:guard:52-77）。 |
| S3A-3 | 採納 | §3 #8、§4.4-3、M2／M4R／M5、B4 | P10:906、P:ab_manifest.py:392-401 要求相同 CLI implementation／SHA；M:crates/sw-cli/src/lib.rs:54-57 含版號輸入。0.4.1 僅選 daemon／runtime 來源，另建共用候選 CLI 的 A 包及配對驗收；正式候選確定後重建 A，不更換比較契約。 |
| S3A-4 | 採納 | §4.2-5、§4.4-4、M2、§8 #6 | M:deployed_content.py:71-120 的穩定入口／sidecar 與 P:ab_manifest.py:313-320 的版本 component 精確路徑確實衝突，adapter 又覆寫入口（P:suites/l2_regression.py:43、l3_realhw.py:380）。改為完整入口／版本根／component 及前後量測契約，建議安全支援版本路徑，含偽造與錯配拒收；不以 tree 非 null 放行。 |
| S3A-5 | 採納 | §1-3、T5B、§4.1-5、§4.4-5、M0／M2／M5、A7 | 重算 M 的 slice_2.4：Linux L1.5＝85（57 blackbox），Windows L1.5＝50（含 7 blackbox、1 rust-l1）；P:l15_functest.py:61-74、170-174、207-212 只承接兩類 YAML。逐案列 runner→artifact→report→出口，完整集合等值驗收；M2 上修 3～5 週，正式執行也上修安靜主機時長。 |
| S3A-6 | 採納 | §4.4-6、M4／M4R／M6、A3 | M:release_assets.py:111-114 只收 rust-2，:142-166 固定 manifest 路徑，:220-239 精確白名單；M:release.yml:110-140 只驗 RC 資產，:197-218 仍接 Stage 2 指南／重驗。列入 stage／schema／佈局／資產／RC 建置上傳／promote 全路徑，RC 前交付，M4 增約 2 週。 |
| S3A-7 | 採納 | T14、A5、M0b／M1／M2、§8 #5 | OD-09（M:owner-decisions:13）、spec:1702、P10:1462-1466／2848 明確保留產品／安全 FAIL。A5 改建議 (b)，VOID 只留 owner 新 OD 提案，明定因果／窗口／權限／FAIL 優先及不回溯；可選實作另加 1～2 週，未採不偷算已完成。 |
| S3A-8 | 採納 | §1 首要行動、T15、§3 #1／2／6、M0c、A4／B16、§6 | PA:s2:542-546 確有單板限制；M:acceptance/README.md:53 記規範矛盾，yaml:12、130-138 的 engine／Windows COM0 負載不足以替兩板定規。B16 提前到 M0，列完整兩板／單板放寬／阻擋及 OD-09／102／109 後果；原 YAML／歷史不改，新增負載承接未經 owner 不實施。M0c 2～4 週含合法方案所需先行基線。 |
| S3A-9 | 採納 | §4.2-6、M4R／M5、A3 彩排深度、B14、§5 關鍵路徑 | P10:2673-2684 要驗不同 Current／Previous 及回原內容；P:ab_manifest.py:399-402、evidence_ingest.py:1175-1180 拒 Stage 2 包冒充 Stage 3。拆 0.5.1↔0.5.0 與可丟棄 Stage 3↔0.5.1，正式仍最後 RC 重跑。正向 retirement 發布不能靠縮短長跑／Canary 取得 PASS，增 owner 彩排深度決定；建議完整正向方案 M4R 5.5～10.5 週，縮短方案只算診斷且明記首次完整 gate 未驗。 |
| S3A-10 | 採納 | §1-3、§4.1-5、A7、§9 | 以 M:cases.yaml＋cases.lock.json 重算 Windows L2＝34 regression＋1 windows-native；native 是 win-wal-f12-progress（cases.yaml:3033-3048）。統一數字，安裝 TestPilot 不代表涵蓋 native。 |

**A／B 意見的逐項決定**（含同意項，避免未寫出的默認）：

| 編號 | 處置 | 改了哪裡（節名） | 理由與出處 |
|---|---|---|---|
| S3A-A1 | 採納 | A1、§4.4、M1～M4 | 保留逐 Task 移植 (b)，增加 S3A-1～6 的語意／反例驗收；P 的可重用程式確有缺口，不能把函式對齊視為完成。 |
| S3A-A2 | 採納 | A2、B4、M5 | 保留同位元組 RC→promote (a)，補同 CLI SHA 的 A 包；P10:906、2817-2827 兩個義務要同時成立。 |
| S3A-A3 | 採納 | A3、§4.4-2／6、M4／M4R | 保留 RC→promote (a) 但補資產驗證器、RC 建置、佈局與帳本；M:release_assets／workflow 及 P:ab_runner 的阻斷路徑均已核對。另列完整正向／縮短診斷彩排的成本及未驗範圍，不以短報告假綠。 |
| S3A-A4 | 採納 | A4、B16、M0c、§5 | 並行 (c) 改有條件建議，新 OD 必須同時處理 Stage 3 24h、板數、排程、逐板負載與 OD-109；PA:s2:455、542-553 是 Stage 2 的特定範圍，不能直接延伸。 |
| S3A-A5 | 採納 | A5、T14、§5／§8 | 撤回優先做 VOID (a)，改建議現行規則 (b)；P10:1462-1466／2848 與 spec:1702 禁止用外擾抹掉 FAIL，新 OD 不得豁免安全。 |
| S3A-A6 | 採納 | A6、§5 產能及公式、§6 | 保留 (d) 但量化建議保底每週 5 天、原生半天、安靜主機 4h、25% agent quota 與週產出；數值仍待 owner 核准及首週校準。0.5.1 PLAN 未列 OD-83 B，新增 S3-R 1～2 週，不能以移階段扣除總工期。 |
| S3A-A7 | 採納 | A7、§4.1-5、M0／M2 | 撤回整批排 gap 的建議，改 (b) 保完整分母。逐案替代／例外才可提新 OD；M:owner-decisions:12（OD-08）、18（OD-14 #7）、35（OD-32）及 P10:259／882 均不能由 gap 狀態覆寫。 |
| S3A-A8 | 採納 | A8、§4.4-2／6、M4R | docs 路徑 (a) 保留，必要檔案集合全取並驗 hash；P10:3100 的 Derived.files 複製及 P:evidence_ingest 的證據鏈要求不是只留 hash。外存必要附件須有下載／校驗，乾淨 hosted checkout 做缺附件反例。 |
| S3A-B1 | 採納 | B1、A6、M5 估期 | 配額結構 (b) 保留，明禁下修 OD-10 的 14／5000／50、至少三類及比例／去重；M:P10:171-181、219、227 支持。可用量不足只延長週期。 |
| S3A-B2 | 採納 | B2、M0a／M4R、M5 估期 | 保留 Windows 單人工作站／Linux 真實使用，兩平台均先量測各類速率；M:OD-10／12、P10:88-89。3～6 週只條件式估期，未量測不承諾。 |
| S3A-B3 | 採納 | B3、S3-R、§5 | 保留 (a) 為待 owner 擴範圍的建議，另列 1～2 週與原生／Defender／發版等待。PA:s2:149-155 只授權長期方向，PA:v051:40-44 及本機 PLAN 不代表已排進 0.5.1。 |
| S3A-B4 | 採納 | B4、§4.4-3、M2／M5 | 改 (a) 的整包表述為 daemon／runtime 來源＋共用候選 CLI A 配對包；P10:906、P:ab_manifest.py:392-402，舊包名稱不能證明 SHA／配對相容。 |
| S3A-B5 | 採納 | B5、S3-E、§4.2-12、§5 | 原 (c) 三類摘要改成全清冊逐項補驗／有效沿用／新 OD，未核准按保留義務阻擋。P10:95 BO-P9-14、spec:1193；程式只查 stage 3 不構成其餘項豁免，新增清冊 1～2 週及尚欠補驗增量。 |
| S3A-B6 | 採納 | B6（保留）、M4／M6 | 保留產品 Python 退役、基線／工具留存，及 RC 前文件／install 改造；spec:1195、PA:s1:15 OD-50、PA:s2:17 OD-78、spec:1274 支持需新 OD 取代 wheel／exe 續發條款。 |
| S3A-B7 | 採納 | B7、A7、M0c | 保留 decision_ref／限定板子，但明定無破壞授權就是阻擋、不得自行排除適用集合；P10:268、M:OD-27／32 的保留義務。 |
| S3A-B8 | 採納 | B8（保留）、§6、M0c／M4R／M5 | 保留整段預約／重跑窗口，連同 USB hub、安靜主機、Windows 登出與遠端使用者書面同意；P10:97 BO-P9-16、PA:s2:545、553 的停止 service／不得另起 daemon 限制支持。實際資源狀況仍列 r1 線索，不宣稱本輪已取得同意。 |
| S3A-B9 | 採納 | B9、S3-E、§5 | 由全部延到 1.0.0 後改為先核對凍結案例、只有非必要一般功能才延；PA／PA2:v051:44 與本機 PLAN 只排除 0.5.1，不給晚於 1.0.0 授權。未讀即時 issue，撤去 #270 原文已有授權的斷言。 |
| S3A-B10 | 採納 | B10、B6、S3-E | 保留凍結 Python 產品碼，但明列 sw_core/assets/ 出貨資產例外，按用途與 gate 處理；PA／PA2:v051:7、45 原文允許資產修改，不可整個目錄直接關單。 |
| S3A-B11 | 採納 | B11、M1 | 調整 (a) 為先獨立審再請 owner 追認；P:ab_diff.py:669-681 成功 status 頂層排除真實存在，危險 holder／unknown／錯誤／巢狀反例及獨立安全驗證須先證明，否則撤回或阻擋。 |
| S3A-B12 | 採納 | B12（保留）、M1／M4、§4.2-3 | 保留每週 Windows 原生、add-only 及 target 前最後凍結；PA:v051:22、P10:476-484／2847 要求非空完整集合及當前鎖值，WSL 不代原生。 |
| S3A-B13 | 採納 | B13、M1～M4／M4R | 保留路由 (a) 為規劃，但明列分母反例、凍結副作用及完整發布的驗收證據；P:evidence_ingest／ab_runner 與 M:release_assets 的實際缺口證明模型名稱／次數不足以放行。 |
| S3A-B14 | 採納 | B14、M4R／M5、A3 | 正式 Previous＝0.5.1 保留；撤回 Current＝Previous 的彩排建議，分兩組並重建各自 D7／D8 來源，含回原 Current tree；P10:2673-2684、P:ab_manifest.py:399-402、evidence_ingest.py:1175-1180。 |
| S3A-B15 | 採納 | B15、§6 remote、M0c／M4R | 保留提供拓樸 (a)，加獨立 Windows-native OpenSSH／remote／artifact 證據；M:OD-14 #7（connect／benches）、P10:338-339 的平台出口不能由 Linux docker／SSH 替代。 |
| S3A-B16 | 採納 | B16、A4、M0a／M0b／M0c、首要行動 | 撤回等 M2 單板結果再二選一；PA:s2:542-546、M:acceptance/README.md:53、yaml:12／130-138、cases.yaml:1906-1922 同時形成單板／兩板／凍結負載衝突，必須先裁決完整方案，原凍結檔與歷史不動。 |
