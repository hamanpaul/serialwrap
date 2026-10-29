# event_latency method_revision 2026-10-29.1 獨立審閱（Codex gpt-6.1-sol）

- 審閱對象：`feature/rust-refactor-p5` @ `110bd58`（contracts/resource_limits.yaml 的 event_latency、scripts/rust/event_latency_measure.py、slice21_gate.sh／slice21_gate_judge.py 的 event_latency 段、相關測試與 p5-evidence 證據）
- 審閱者：codex-gpt-6.1-sol（≠ owner codex-gpt-6-luna-max）；唯讀；2026-10-29
- 依據裁決：OD-45、OD-58（rulings/p5.md）
- 處置（編排者）：rejected → 維持 `freeze_status: pending_review_record`；P5 第 6 輪把下列 High／Medium 當本輪 finding 處理；drain_receipt 界限與既有 mailbox 排空順序測試（D,D,D,L,W）若確屬契約衝突，列 OWNER-BATCH 並附 Python 基線實際排空順序；修正後由編排者重新提交獨立審閱。

---

結論：rejected

目前版本不能作為凍結依據。RPC 計時口徑符合 OD-45，但 flood、L1 探針與證據身分接線仍有假綠或降級判定的路徑；契約也尚未套用 OD-58。

## 逐項判讀

1. **外部起訖點合理，但量到的是 RPC 往返延遲。**  
   `scripts/rust/event_latency_measure.py:208–223` 在 JSON 編碼後、`sendall()` 前開始計時，收到完整回應行後停止，排除連線建立、JSON 解碼及 Python 啟動成本；包含 client／daemon 排程、socket 傳輸與 client Python 執行成本。這符合 OD-45，不能解讀成原本 actor 內部 accept／complete 的耗時。stop 每樣本重新啟動 daemon 的準備與清理都在計時之外，因此沒有直接混入啟動成本，但只代表 READY 後的新實例情境。

2. **500 筆 × 三輪、每輪最大值判定可作經驗性 gate，不能保證一般最壞延遲。**  
   拒絕取消與最終非 canceled 已被排除，不會冒充成功取消；少量 stop 準備失敗會另記並補樣本，超額則證據不足。不過，已觀測超限可被其他錯誤降級，flood 與主機作廢規則也未完整落實，見下列發現。

3. **身分欄位已有改善，仍不足以防止舊 artifact 被標成新來源。**  
   現存 JSON 的工具與契約 SHA256 均符合目前檔案；但原始建置報告及 binary 不在此工作樹，無法獨立核對 artifact 來源。gate 的建置失敗處理與判定器也缺少必要核對。

4. **現存三輪數字通過 OD-58 的上限，但不能驗證題述全部數字。**  
   唯一符合指定 glob 的 JSON 含 18 組、每組 500 筆，共 9,000 筆；無錯誤、無作廢。三輪各取最大值如下：

   | 事件 | idle 最大 ms | flood 最大 ms |
   |---|---:|---:|
   | stop_request | 3.735 | 7.801 |
   | command_cancel | 3.592 | 15.622 |
   | release_request | 22.338 | 19.289 |

   Rust 約 38 ms、Python 153～155 ms 僅見於 `docs/rust-refactor/reviews/p5-t22-gate-run.md:103` 的摘要；157／212 ms 見於所指定遠端 ref 的 OD-58 裁決。現存 JSON 無法重算這些數字。它們在數值上落於 250／300 ms 之內，但不能據此宣稱已完整驗證放寬值。

5. **L1 有實際觸發事件，但尚不能證明兩項 l1_only 界限都成立。**  
   lease 測試確實開啟 lease、推進時鐘、等待 LeaseExpiry 並確認收回；receipt 也要求探針非空。問題是探針會漏算暫存 receipt 的等待，且既存測試已有三筆 data 先於 receipt 的明確反例。

## 發現

- **[High] flood 可以失效而未使量測失敗。**  
  `scripts/rust/event_latency_measure.py:244–266、386–423`：`emit()` 的 `OSError`／`RuntimeError` 被忽略，沒有最低送出量、實際速率或 thread 健康檢查；`flood_load.seconds` 完全未使用。stop 每個實例僅暖機 50 ms，沒有建立 30 秒持續負載。唯讀反例確認發送零 bytes 時 context 仍正常結束。此時正常 RPC 樣本仍可能標成 flood PASS。應記錄並驗證實際負載，發送失敗不得當作有效 flood。

- **[High] drain_receipt 的 L1 探針會漏算，已有具體契約反例。**  
  `crates/sw-core/src/mailbox.rs:213–216、271–303`：`WorkerStopped` 移入 `stopped` 後，`control_waiting()` 看不到它，mailbox 卻可繼續取 data。`crates/sw-core/src/actor.rs:83–99` 又會在其他 control 事件出現時清零共用計數。既存 `crates/sw-core/tests/mailbox.rs:183–216` 明確要求順序為 `D,D,D,L,W`，但 `crates/sw-core/tests/drain_release_e2e.rs:300–306` 只檢查這個可能漏算的探針值。因此不能證明 receipt 送出後至多處理一筆 data。須以事件自己的送入／處理界點計數，並由擁有者釐清依賴排空順序與此界限的衝突。

- **[High] release 建置失敗後，舊 binary 仍可被綁到目前 HEAD。**  
  `scripts/rust/slice21_gate.sh:67–76、210–212、276–278`：`run_logged` 永遠回傳成功，Linux 建置結果接著被 Windows 建置覆寫，量測仍使用既有路徑，並直接宣告來源為目前 HEAD。`scripts/rust/event_latency_measure.py:104–126` 對這條明示來源路徑沒有建置成功核對。具體情境是新版本 release 建置失敗、舊 binary 留存，報告卻取得新 commit／run ID。另 `scripts/rust/slice21_gate_judge.py:194–205` 不讀取 event_latency 證據內容；反例中 `evidence: missing.json` 仍可判 section PASS。應要求成功建置紀錄，並核對量測 JSON 與 gate 的 artifact、commit、run、工具及契約身分。

- **[Medium] 已確認超限會被錯誤樣本降成證據不足。**  
  `scripts/rust/event_latency_measure.py:130–144` 先判 errors／筆數，再判最大值。唯讀重現：有效樣本 80 ms、上限 50 ms 原為 FAIL；同輪加入一筆取消被拒後變成 INSUFFICIENT_EVIDENCE。`289–297` 也會在已取得 release 耗時後，因重新 attach 失敗而遺失該耗時。除明確作廢輪次外，應保留有效超限的 FAIL，另記缺樣或準備錯誤。

- **[Medium] 契約尚未套用 OD-58，現存證據不足以完整核對其數值依據。**  
  `contracts/resource_limits.yaml:69` 的 release 仍為 idle 50／flood 100 ms，應為 250／300 ms。現存 `docs/rust-refactor/reviews/p5-evidence/event-latency-od45-rust-evlat-r5-c4cfb96.json:99、170、466` 也使用舊門檻，整體結論仍為 INSUFFICIENT_EVIDENCE。若只補審閱欄位便凍結，會凍結錯誤門檻。應同步 OD-58、補齊其數值來源，並保留 **0.5.0 前同工具重測三輪、重新提交擁有者確認**的要求。

- **[Medium] 主機作廢規則只檢查輪次兩端，缺資料時還會放行。**  
  `scripts/rust/event_latency_measure.py:61–69、134–139、367、427` 只取 before／after，可能漏掉量測中途的過載；load 或 CPU 資料缺失時 `void` 為 false。唯讀反例確認兩者皆缺仍可 PASS。應在量測期間取樣並保留峰值，必要監測資料缺失時判證據不足。

驗證限於靜態追蹤、七個既有測試函式直接呼叫，以及記憶體內反例；未執行完整 pytest、cargo 或 daemon 量測。Git 工作樹無變更。

## 審閱紀錄欄位

```yaml
reviewed_by: codex-gpt-6.1-sol
reviewed_at: "2026-10-29"
review_conclusion: rejected
```

應維持 `freeze_status: pending_review_record`，修正後再提交獨立審閱。