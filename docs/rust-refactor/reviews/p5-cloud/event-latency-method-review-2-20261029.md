> 審閱者：雲端 Claude Opus xhigh（≠ owner；擁有者 2026-10-29 指定雲端 Opus 代理審查）；審閱對象 feature/rust-refactor-p5 @ 86b54d8

結論：approved-with-conditions

外部量測方法（method_revision `2026-10-29.2`）可以作為 stop_request／command_cancel／release_request 上限值的凍結依據：

- 前次審閱的 3 High、3 Medium 都已在 `239e025` 修正，修正本身沒有引入新的假綠路徑。
- r6 證據 18 輪 × 500 筆全部 PASS，數字支持 OD-58／OD-65 的上限。

L1 結構 gate 還守不住兩項 l1_only 界限，列為凍結前必須完成的條件：

- drain_receipt：OD-64 的新界限尚未落實，而且 gate 測試從未走到依賴等待路徑。
- lease_expiry：gate 的偵測力偏弱。

- 審閱依據：
  - 擁有者裁決 OD-45、OD-58、OD-64、OD-65（`rulings/p5.md`）。
  - 前次審閱 `reviews/p5-cloud/event-latency-method-review-20261029.md`（Codex gpt-6.1-sol，rejected）。
  - P5 第 6 輪回報 `reviews/p5-cloud/round6-report-20261029.md`，以及 `p5-fix-responses.md` 的 F171～F176（`239e025`）。
- 範圍：
  - `contracts/resource_limits.yaml` 的 `event_latency`。
  - `scripts/rust/event_latency_measure.py`。
  - `slice21_gate.sh` 與 `slice21_gate_judge.py` 的 event_latency 段。
  - `crates/sw-core/src/{mailbox,actor}.rs` 的見證數探針。
  - 相關測試與 r6 證據。
- 唯讀審閱：審閱 worktree 未修改，除本檔外未新增或變更任何 repo 檔案。

## 逐項判讀

### 1. 前次 3 High、3 Medium

| 前次發現 | 判定 | 依據 |
|---|---|---|
| [High] flood 失效未判 | 已修 `239e025` | `_Flood` 計數送出失敗，並記錄起訖時刻與存活狀態（`event_latency_measure.py:287-343`）。`flood_problems()` 驗證送出失敗、中途停止、速率 ≥ 90% 與持續時間（`:346-361`）。release／cancel 每輪補足 `seconds`（`:541-542`）；stop 每樣本暖機 `stop_warmup_s` 並逐段驗證（`:500`、`:504`）。負載無效的輪次判 INSUFFICIENT（`:189`）。殘留問題見 L-4。 |
| [High] drain_receipt 探針漏算 | 「漏算」已修 `239e025`；OD-64 新界限未落實（條件 C1–C3） | mailbox 以影子佇列與 `stopped` 平行佇列逐事件計數（`mailbox.rs:172-195`、`:280-307`、`:380-385`）。原本會被歸零的共用計數已移除，actor 改取 `take_control_witness()`（`actor.rs:289-297`）。timer／tick 改計「到期後才開始處理的 data 數」（`actor.rs:95-99`、`:233`、`:258`）。紅燈測試 `tests/mailbox.rs:603-637` 驗證 D,D,D,L,W 情境的見證數為 3。lane 見證數經變異 M1 驗證有效。 |
| [High] 建置失敗後舊 binary 被綁到 HEAD；judge 不讀證據 | 已修 `239e025` | gate 建置前先 `rm -f` 舊 artifact（`slice21_gate.sh:233`），Linux／Windows 結束碼分開保存（`:235`、`:241`）。只有建置成功才記錄 `BUILT_D_SHA`（`:236-239`）並明示來源（`:311-313`）。工具要求 artifact sha256 等於受測 binary，否則不綁來源（`event_latency_measure.py:147-151`），結果為證據不足（`:235-237`）。judge 讀取證據的 verdict，並核對 daemon_sha256、artifact_source_commit、run_id、工具與契約 sha256、method_revision（`slice21_gate_judge.py:228-268`）；Linux 建置失敗即 FAIL（`:294-301`）。殘留問題見 L-2、L-3。 |
| [Medium] 已確認超限被錯誤樣本降級；release 重新 attach 失敗遺失耗時 | 已修 `239e025` | `judge_run` 先判作廢；未作廢且有效樣本超限即 FAIL（`:185-188`）。`_sample_release` 回傳（耗時, 準備錯誤），耗時不再遺失（`:384-397`）。殘留問題見 L-5。 |
| [Medium] 未套 OD-58 | 已修 `239e025` | `resource_limits.yaml:75-81`：release_request 為 idle 250／flood 300 ms，並帶 `retest_before: "0.5.0"`、`owner_decision_ref: OD-58`。stop／cancel 維持 50／100（`:63`、`:66`）。對應測試 `test_event_latency_measure.py:285-290`。OD-65 要求「凍結紀錄註明 Python 基線 255.4 ms」，尚未寫入（條件 C5）。 |
| [Medium] 作廢只看輪次兩端；缺監測資料仍放行 | 已修 `239e025` | `_HostMonitor` 每秒取樣（`:77-98`），以峰值判作廢；取不到 cpu 或 loadavg 即證據不足（`:177-189`）。r6 每輪的 host 取樣數與時長相符，例如 stop flood 約 564 s 有 564 筆取樣。殘留問題見 L-7。 |

**修正是否引入新的假綠或降級路徑：沒有發現新的假綠路徑。**

- 新判定順序為「作廢 → 有效超限 FAIL → 其餘證據不足」。作廢優先於 FAIL 是契約明文（`resource_limits.yaml:59-60`）。
- judge 只在證據為 PASS 且身分欄位全部相符時才沿用 PASS；任何缺漏一律 EVIDENCE_MISSING，證據 FAIL 直接判 FAIL。
- 殘留的降級路徑只有 L-5（逾時與非 ok 樣本），其結果是證據不足，不會變成 PASS。

### 2. 外部量測方法是否足以作為凍結依據

- **起訖點**：工具在寫出請求前取 `monotonic_ns`，讀到完整回應行後再取一次（`event_latency_measure.py:260-275`），不含連線建立與 JSON 解碼，符合 OD-45。三個事件的回應時點不同：
  - release：回應在排空完成後才寫出（`drain_release_e2e.rs:17-43` 顯示 release 會等回條），所以量值包含排空。這是 OD-58 放寬上限的原因。
  - cancel：`registry.cancel` 後同步 `route(CancelQueued)`，等 actor 回覆才回應（`crates/sw-daemon/src/service.rs:570-584`），所以量值涵蓋 complete_point。
  - stop：daemon 先寫出 `stopping:true` 回應，再呼叫 `request_stop()`（`crates/sw-daemon/src/framing.rs:229-239`；Python 的 `_rpc_after_write` 同序）。外部值因此止於 accept_point，不含 Shutdown→BeginDrain；actor 端只受 L1 資料筆數界限約束，沒有時間上限（L-1）。
- **樣本數與 gate_rule**：idle／flood 各 3 輪 × 500 筆，規則為 `every_run_max_le_max_ms`。`overall_verdict`（`:212-243`）要求：
  - 涵蓋 repeats × loads × 三個外部事件；
  - 非診斷模式；
  - L1 gate 全部 passed；
  - 上限已凍結；
  - 身分欄位齊全。
  這是經驗性 gate，足以作為凍結依據。
- **作廢規則**：量測期間每秒取樣 loadavg，峰值 > CPU 數即作廢重跑（預設最多 2 次），重跑仍作廢即證據不足；監測資料缺失也判證據不足。作廢只看 loadavg、與延遲值無關，因此不會挑出低延遲樣本。限制：1 分鐘 loadavg 反應遲鈍，短暫尖峰不會觸發作廢；這只會造成假紅，不會造成假綠。
- **flood 實際負載**：送出失敗、中途停止、速率與持續時間都已驗證。r6 實際速率 19943.6～20003.7 B/s，送出失敗 0，無效期間 0。FakeTarget 自己持有 slave fd 但不讀取（`tests/contract_blackbox/bb_faketarget.py:88-110`、`:261-309`），所以持續 30 s 以上、≥ 90% 的平均速率只可能由 daemon 讀取達成，平均意義上可信。不足之處是沒有分段速率，也沒有接收端核對（L-4）。
- **證據身分**：建置成功核對、artifact sha256、run_id、工具與契約 sha256、method_revision 都已綁定，judge 也讀取證據內容。殘留問題：
  - judge 不由證據內容重算 verdict（L-2）。
  - 來源 commit 取量測當下的 HEAD，不是建置當下；dirty 檢查不含未追蹤檔，git 執行失敗時判為乾淨（L-3）。
- **結論**：除條件 C5（OD-65 註記）外，外部量測方法本身足以凍結三個外部事件的上限。

### 3. r6 證據是否支持 OD-58／OD-65

重算 `event-latency-od45-rust-r6-261029.json` 的結果：

- 量：18 輪、每輪 500 筆，共 9,000 筆。
- 狀態：errors 0、load_problems 0、void 0、voided_runs 0；L1 gate 兩支 passed。
- identity：artifact 建置自 `c2f6882`、run `r6-261029`、建置當下 sha256 相符。
- 工具與契約 sha256 與 `86b54d8` 的檔案相同。

| 事件 | 上限 idle／flood (ms) | idle 三輪 max (ms) | flood 三輪 max (ms) | 各輪 p99 最大 (ms) |
|---|---|---|---|---|
| stop_request | 50／100 | 1.00／2.69／3.37 | 2.27／4.16／2.44 | 2.80 |
| command_cancel | 50／100 | 4.19／2.81／3.01 | 8.47／5.88／50.58 | 2.04 |
| release_request | 250／300 | 9.72／42.70／36.77 | 28.34／13.52／12.77 | 8.48 |

- **全部 PASS，數字支持 OD-58**（release 250／300；stop／cancel 50／100）**與 OD-65**（維持 250／300）。證據的 verdict 為 INSUFFICIENT_EVIDENCE，唯一原因是尚未凍結。
- **Python 診斷**（每輪 50 樣本）：idle release_request 三輪 max 為 154.40／255.37／157.65 ms，第 2 輪超過 250 ms。這是診斷資料、不計入判定，與 OD-65 所載一致。
  - Python 的 release 在 flood 下反而較快（18.10／51.95／25.37 ms）。可能原因是 reader 的 `select(…, 0.2)` 逾時決定了 idle 時的排空等待（`sw_core/uart_io.py:1946`）。這不是 Rust 的問題。
- **注意事項**：
  - command_cancel flood 第 3 輪有 50.58 ms 離群值（p99 只有 1.31 ms），release idle 也有 42.70／36.77 ms 離群值，表示同一主機的排程停頓已接近 stop／cancel 的 idle 上限 50 ms。這是假紅風險，不是假綠（L-6）。
  - OD-58 理由所引的 Rust 157／212 ms 離群值，無法從已提交的證據重算；r6 的 Rust 最大值為 42.70 ms。
  - r6 證據的 `contract_sha256` 綁定目前的契約。凍結時改寫 `freeze_status` 與審閱欄位，契約 sha256 必然改變，judge 會拒收 r6（`slice21_gate_judge.py:254-265`）。因此 r6 只能作為上限值的依據；Slice 出口的 gate 證據必須在凍結後的 commit 重跑產生。

### 4. l1_only 界限是否由 L1 gate 真正守住

**lease_expiry：結構上成立，但 gate 的偵測力偏弱。**

- 結構保證：actor loop 每一輪開頭先檢查 timer（`actor.rs:229-250`）；deadline 已過時，`recv_next` 在 select 之前就回 None（`mailbox.rs:389-391`）。因此到期後至多再處理 1 筆 data。
- 探針：以 FakeClock 時戳計「到期後才開始處理的 data 數」（`actor.rs:95-99`、`:233`、`:292`、`:322-325`）。計法正確，且偏保守。
- 偵測力：gate 每次只取樣 1 個 LeaseExpiry 事件（`drain_release_e2e.rs:347-368`）。
  - 變異 M5（timer 每 4 輪才檢查，且 deadline 過後仍回最多 3 筆 data）跑 6 次只紅 1 次。
  - lane 事件同樣只取樣一次。變異 M1（data 可越過 control 至多 3 筆）下，單看 Release 標籤 12 次只抓到 4 次；整支測試 12/12 全紅，是靠同測試裡其他標籤補足的。
- 處置：條件 C4。

**drain_receipt（依 OD-64 改判）：目前守不住新界限。**

- 契約與測試仍用舊界限：契約仍寫 `at_most_one_data_item_before_control`（`resource_limits.yaml:72-74`），`tests/test_slice21_scope.py:112` 也要求五個事件都是這個值。
- 探針缺少新界限需要的量：探針只記總見證數，沒有記錄 receipt 進入依賴等待時已讀入的同 epoch data 筆數。`ProbeEntry` 只有 `data_while_waiting` 與 `data_backlog`（`actor.rs:56-65`），`note_stopped_push` 只搬移見證數（`mailbox.rs:303-307`），因此無法判定「≤ 已讀入筆數＋1」。
- gate 沒有涵蓋依賴等待路徑：drain_receipt 的兩支 l1_gate 從未走到 `stopped`。變異 M3（`stopped` 非空即 panic）下兩支照樣 passed，只有不在 gate 內的 `tests/mailbox.rs` 3 支轉紅。也就是說，OD-64 針對的情境目前完全不在 gate 內。
- 新測試的設計要點：`stopped` 路徑對 data 一律 `try_recv`、不分 epoch（`mailbox.rs:349-351`）。
  - 正式環境中 reader 停止後不會再有新的 data，所以界限在結構上成立。
  - 但若用現有的 Flood 注入器（持續注入現行 epoch 的 data）測依賴情境，W 會被無限延後。
  - 新測試須使用有限的 backlog，並把跨 epoch 的 data 計入「＋1」。
- 處置：條件 C1–C3。

**P5 OWNER-BATCH #1 的主張屬實（現已由 OD-64 裁決）。** 依據：

- Rust：`stopped` 路徑先排空 data，再釋放同 epoch 的依賴事件，最後才回 receipt（`mailbox.rs:349-386`）。`tests/mailbox.rs:182-216` 斷言順序為 D,D,D,L,W，`:603-637` 得到見證數 3 > 1。原本的「至多一筆」界限與這個有測試保護的既有排空順序確實互斥。
- Python 基線同序：reader `_loop` 讀到資料即同步呼叫 `_handle_serial_rx`（`sw_core/uart_io.py:1935`、`:1975`）；`_complete_drain` 先 join reader（`:1095`）才產生回條。因此 stop 前已讀入的 RX 一定在 receipt 之前處理完。
- 結論：這是契約問題，不是實作能修的問題，主張成立。
- 附註：無依賴情境下，safety lane 優先，Rust 的 W 會越過同 epoch 的 backlog 先被處理，與 Python「RX 全部處理完才有回條」的順序不同。這不影響 OD-64 的界限，但從 parity 角度可另行確認。

## 發現

- **[Medium] OD-64 的 drain_receipt 新界限尚未落實，L1 gate 未涵蓋依賴等待路徑**（凍結前必修，條件 C1–C3）
  - 位置：
    - `contracts/resource_limits.yaml:72-74`
    - `tests/test_slice21_scope.py:112`
    - `crates/sw-core/src/mailbox.rs:303-307`、`:349-386`
    - `crates/sw-core/src/actor.rs:56-65`
    - `crates/sw-core/tests/drain_release_e2e.rs:312-389`
  - 失敗情境：假設 `stopped` 路徑回歸成「receipt 等待期間多處理 N 筆不屬於已讀入 backlog 的 data」，例如跨 epoch 的 data，或 receipt 進入等待後才到的 data。兩支 l1_gate 仍會 passed，因為變異 M3 證實它們根本沒走到 `stopped`。探針也沒有「已讀入筆數」，無法依 OD-64 判定。

- **[Medium] l1_only 事件的 L1 gate 每個事件只取樣一次，對間歇性違規的偵測力弱**（條件 C4）
  - 位置：`drain_release_e2e.rs:300-307`、`:312-369`、`:374-389`。
  - 失敗情境：
    - 變異 M5 讓 lease 到期後最多再處理 2～3 筆 data，`release_handled_within_one_data_item_under_flood` 6 次有 5 次 passed。
    - 變異 M1 讓 data 可越過 control 至多 3 筆，單看 Release 標籤 12 次只抓到 4 次。
    - lease_expiry 與 drain_receipt 為 l1_only，L1 gate 是唯一防線，間歇性回歸可能在 gate 綠燈下被凍結。

- **[Low] L-1：stop_request 的外部值止於 accept_point**
  - 位置：`crates/sw-daemon/src/framing.rs:229-239`、`resource_limits.yaml:63-65`。
  - 情境：daemon.stop 先寫回應、再 `request_stop()`，所以 50／100 ms 只約束 RPC 寫回路徑。Shutdown→BeginDrain 只受 L1 資料筆數界限約束，沒有時間上限。凍結紀錄若不註明，可能被誤引為「停止延遲 ≤ 50 ms」的保證。
  - 建議：在 stop_request 列註明。

- **[Low] L-2：judge 只信任證據的 `verdict` 欄位，不由內容重算**
  - 位置：`slice21_gate_judge.py:249-268`。
  - 情境：證據 JSON 若被截斷或人工拼接（例如 runs 少一輪、l1_gate 缺項），只要寫著 `verdict: PASS` 且身分欄位相符，judge 就沿用 PASS。
  - 建議：judge 載入工具的 `overall_verdict()` 重算，並要求結果一致。這是縱深防禦，不是現行流程中已知會發生的路徑。

- **[Low] L-3：來源 commit 與 dirty 判定有漏洞**
  - 位置：`slice21_gate.sh:312`、`event_latency_measure.py:127-133`、`:160`。
  - 情境：
    - `--artifact-source-commit` 取的是量測當下的 HEAD，不是建置當下；gate 執行期間（約 2 小時）若 HEAD 變動，binary 會被標成後來的 commit。
    - dirty 檢查用 `--untracked-files=no`，未追蹤且未被 ignore 的檔案不算 dirty。例如 `.cargo/config.toml` 可以改變 release 建置結果（已確認該路徑不在 `.gitignore`）。
    - `_git` 不檢查 returncode。以壞掉的 index 讓 `git status` 回 rc=128 時，`report_identity` 仍回傳 `source_tree_dirty: False`，source_commit 則照常取得（fail-open）。
  - 建議：建置當下記錄 HEAD 與 `git status --porcelain`（含未追蹤檔）並傳給工具；git 回非 0 時判證據不足。

- **[Low] L-4：flood 只驗證送出端的平均速率**
  - 位置：`event_latency_measure.py:304-320`、`:346-361`。
  - 情境：`_run` 以時間表補償速率。送出端被擋住後（例如 release／re-attach 期間 PTY 緩衝區滿）會集中補送，平均速率仍可 ≥ 90%，但量測窗口內的瞬時負載可能為 0。
  - 建議：加上分段（例如每秒）最低速率；或以 daemon 端的 RX 計數（WAL 或 session 統計）核對實際接收量。

- **[Low] L-5：逾時與非 ok 的長耗時樣本不列為超限**
  - 位置：`event_latency_measure.py:509-515`、`:536-540`；`_Rpc` 逾時 10 s（`:249-256`）。
  - 情境：daemon 處理 release 卡住超過 10 s 時，socket 逾時只記為錯誤，累積 6 筆即中止該輪。結果是證據不足而非 FAIL，即使已確知延遲 > 10 s、遠超上限。這不會造成假綠，但會把已確認的超限降級為證據不足。
  - 建議：逾時（已耗時 > 上限）記為超限樣本。

- **[Low] L-6：stop／cancel 的 idle 上限 50 ms 餘裕不足**
  - 位置：證據中 cancel flood 第 3 輪 max 50.58 ms、release idle 42.70 ms。
  - 情境：同一主機的排程停頓已接近 50 ms，日後只要 idle 輪出現一次這種停頓就會 FAIL（假紅）。
  - 建議：在凍結紀錄註明，0.5.0 重測時一併觀察。

- **[Low] L-7：作廢判定粒度粗**
  - 位置：`event_latency_measure.py:66-74`、`:177-183`。
  - 情境：只用 1 分鐘 loadavg 與 `os.cpu_count()`。在受 cgroup CPU 配額限制的容器中（loadavg 低、實際被節流）不會作廢。結果偏向假紅，記錄即可。

- **[Low] L-8：證據未保存原始樣本**
  - 位置：`event_latency_measure.py:59-63`、`:553-555`。
  - 情境：證據 JSON 只有 count／max／p99，獨立審閱者無法重算分布或確認離群值。
  - 建議：保存原始樣本（9,000 筆浮點數約 100 KB）。

- **[Low] L-9：preflight 與凍結測試接受任何非空的 `review_conclusion`**
  - 位置：`slice21_gate.sh:94-98`、`tests/test_slice21_scope.py:90-92`。
  - 情境：`review_conclusion: rejected` 加上 `freeze_status: frozen` 也能通過開跑檢查。
  - 建議：限定為 {approved, approved-with-conditions}；為 approved-with-conditions 時，須附條件完成紀錄。

- **[Low] L-10：lane 見證數沒有正向自測**
  - 位置：`mailbox.rs:280-301`。
  - 情境：變異 M2（拿掉 lane 見證數累加，同時讓 data 可越過 control 3 筆）下，兩支 e2e gate 全綠。只因 `tests/mailbox.rs` 的順序測試另外轉紅，linux_l1 整體才會失敗。也就是說，探針一旦退化，l1_gate 會靜默失效。
  - 建議：補一個 unit test——事件停在 control／safety lane 期間，經 pending／stopped 路徑回傳 N 筆 data，見證數須等於 N。可併入 C4。

## 條件

approved-with-conditions：以下條件全部完成、並由編排者核對後，才可改為 `freeze_status: frozen`。

- **C1. 契約（OD-64）**
  - `resource_limits.yaml` 的 drain_receipt 改用新的界限識別字（例如 `at_most_preread_same_epoch_data_plus_one_before_receipt`），並明確定義：
    - N_pre：receipt 進入依賴等待（移入 `stopped`）當下，data lane 中同 epoch 的 data 筆數。
    - 界限：receipt 被處理前，mailbox 回傳的 data ≤ N_pre＋1。
    - 跨 epoch 的 data，或 receipt 進入等待後才到的 data，一律計入「＋1」。
  - 同步更新 `tests/test_slice21_scope.py:112`；其餘四個事件維持原界限。
  - 是否換 method_revision 由 P5 依慣例決定；若換，審閱紀錄須註明本審閱涵蓋 `2026-10-29.2` 加上 C1–C5 的增量。
- **C2. 探針**
  - 在 `note_stopped_push`（或等價位置）記錄 N_pre，只計同 epoch。
  - `ProbeEntry`（或 mailbox 的見證介面）對 WorkerStopped 暴露 N_pre。
  - gate 斷言 `data_while_waiting ≤ N_pre + 1`；無依賴時 N_pre 視為 0，等同原本的 ≤ 1。
- **C3. L1 gate**
  - 新增 actor 層的依賴情境測試：在途命令或登入的 OpFinished／LoginFinished 帶 `after_rx_seq`，搭配有限的 RX backlog，release 與 shutdown 各一。
  - 須斷言確實走到 `stopped`（N_pre ≥ 1），並符合 C2 的界限。
  - 把這支測試加入 drain_receipt 的 `l1_gate`，並以 add-only 登記進 s21_core 鎖。
  - 附變異證明會轉紅，例如讓 `stopped` 路徑多取 1 筆不同 epoch 的 data。
- **C4. 偵測力**
  - l1_gate 測試對每個標籤（Release、CancelQueued、LeaseExpiry、Shutdown、WorkerStopped）至少重複 20 次，以迴圈或參數化實作。
  - 補上 L-10 的 lane 見證數正向自測。
  - 附上 M1／M5 類變異會轉紅的紀錄。
- **C5. 凍結紀錄（OD-65）**
  - event_latency 註解記載：r6 的 Python 基線 idle release_request 有一輪 255.4 ms；Rust r6 最大 42.7 ms；上限維持 250／300；0.5.0 前須重測（保留 `retest_before`）。
  - 依 L-1，註明 stop_request 的外部值止於 accept_point。

另提醒（非條件）：凍結 commit 之後須重跑 T22 gate（非 dry-run、非 root），產生新的 event_latency 證據。judge 會因 `contract_sha256` 不符而拒收 r6。

## 審閱紀錄欄位

```yaml
reviewed_by: cloud-claude-opus-xhigh
reviewed_at: "2026-10-29"
review_conclusion: approved-with-conditions
```

- 填入時機：C1–C5 完成並經編排者核對後，與 `freeze_status: frozen` 一起寫入。
- 契約註解須附本檔路徑（`reviews/p5-cloud/event-latency-method-review-2-20261029.md`）與條件完成的 commit。
- `reviewed_by` ≠ owner（`codex-gpt-6-luna-max`）。

## 驗證紀錄

- **環境**
  - 雲端 Linux，4 核；以一般使用者 ubuntu（uid 1000）執行。
  - Rust 1.97.0；Python 3.12＋PyYAML 6.0.1＋pytest 9.2.0＋jsonschema 4.26.0。
  - 審閱 worktree（detached `86b54d8`）未修改；變異測試在另一份 `git archive` 複本進行。
- **Rust 測試**：`cargo test -p sw-core --features test-fakes --test mailbox --test drain_release_e2e` 共 27 passed，含兩支 l1_gate 與 `witness_counts_data_overtaking_stopped_receipt`。
- **Python 測試**：`pytest tests/test_event_latency_measure.py tests/test_slice21_scope.py tests/test_slice21_gate_judge.py` 共 65 passed、1 skipped。skipped 的是凍結測試，預期結果為 INSUFFICIENT_EVIDENCE。
  - 以非擁有者 uid 執行時，`test_report_identity_binds_artifact_commit_tool_and_run` 因 git 的 dubious ownership 檢查而失敗（source_commit 為空）；加上 safe.directory 後通過。這屬環境因素，但順帶揭露了 L-3 的 fail-open。
- **變異測試**
  - 每個變異使用各自的 target 目錄。有一次共用 target 的結果因 mtime 判定而沿用舊產物，已作廢重跑。

  | 變異 | 內容 | 結果 |
  |---|---|---|
  | M1 | data 可越過 control 至多 3 筆 | 12/12 紅（單看 Release 標籤 4/12） |
  | M2 | M1＋拿掉 lane 見證數累加 | `drain_release_e2e` 10/10 綠（gate 靜默）；`tests/mailbox.rs` 4 支紅 |
  | M3 | `stopped` 非空即 panic | 兩支 l1_gate 綠；`tests/mailbox.rs` 3 支紅 |
  | M4 | timer 每 4 輪才檢查 | 6/6 綠（被「deadline 過後回 None」吸收，不構成有效回歸） |
  | M5 | M4＋deadline 過後仍回最多 3 筆 data | 1/6 紅 |

- **證據重算**
  - 直接讀取兩份 r6 JSON，逐輪取值（見第 3 節表格）。
  - 工具與契約的 sha256（`aa8e8bb1…`、`b23f9a1a…`）與 `c2f6882`、`86b54d8` 的檔案相同。
  - `c2f6882..86b54d8` 之間，審閱範圍內只有 `slice21_gate_judge.py` 在 `86b54d8` 有變更（未承擔集合判 INCOMPLETE），不影響 event_latency 段。
