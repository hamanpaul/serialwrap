> 審查者：codex gpt-6.1-terra xhigh（擁有者 2026-11-23 指定）

## A 前輪 finding 核對

| 編號 | 原嚴重度 | 狀態 | 累計未修輪數 | 說明 |
|---|---|---|---:|---|
| V051-af95cf2-1 | High | 已修 `aea228ee` | 0 | 本輪未發現 schema 修正被破壞。 |
| V051-af95cf2-2 | High | 已修 `aea228ee` | 0 | 本輪未發現離線 claim 修正被破壞。 |
| V051-af95cf2-3 | High | 未修 | 2 | 一般 dynamic owner 的交錯已修，但 YAML 來源仍繞過「另一 owner 持有時一律拒絕」；見 B-1。 |
| V051-af95cf2-4 | High | 已修 `282d713e` | 0 | 依裁決拒絕保留名稱，並停用非 YAML 舊列；測試涵蓋熱插及重啟。 |
| V051-af95cf2-5 | High | 已修 `282d713e` | 0 | 同名 fallback 改配其他自動名稱，測試涵蓋解除及重啟。 |
| V051-aea228e-1 | High | 未修 | 1 | 一般 dynamic owner 已修；YAML 來源的例外仍不符合本次並行裁決，見 B-1。 |
| V051-aea228e-2 | High | 已修 `282d713e` | 0 | `alias.set`／`alias.assign` 拒絕保留名稱，定向測試涵蓋兩個入口。 |
| V051-aea228e-3 | High | 已修 `282d713e` | 0 | 排除剛解除的名稱，並更新 actor、profile 與 registry。 |
| V051-aea228e-4 | High | 已修 `282d713e` | 0 | RPC schema、錯誤碼 scope、BC-18 及受控交錯測試已補齊；修前證據限制另見 B-2。 |

## C 完整性核對

| 項目 | 狀態 | 證據 |
|---|---|---|
| 逐項處理 finding，先提供修前紅燈再修正 | 部分完成 | 四組回歸均有修正與測試，但 YAML owner 的並行裁決未完整落實；紅燈執行時產品碼未綁定可核實的 SHA，見 B-1、B-2。 |
| 不放寬既有斷言、不刪除或改名既有測試 | 完成 `282d713e` | 既有 eviction 測試改用 test-fakes 建立現在被公開 API 拒絕的 collision；原淘汰行為斷言及 nodeid 保留。L1 鎖只新增節點。 |
| finding 對照表、驗收結果與 commit SHA | 完成 `282d713e` | 實作者回報有逐項對照與 SHA；綠燈產品檔雜湊與提交內容一致。 |
| 先檢查半成品再決定處置 | 完成 `282d713e` | 實作者工作紀錄顯示先讀取並比對 `partial.diff`，再調整測試與實作。 |
| 保留 selector、舊列警示、熱插及重啟 | 完成 `282d713e` | 實作、BC-18、README 與定向測試均存在。YAML target 維持既有行為。 |
| 並行 owner 判定 | 部分完成 | dynamic 對 dynamic 的受控交錯通過；YAML owner 例外見 B-1，未列入 SPEC-GAPS。 |
| 同名自動 fallback | 完成 `282d713e` | 測試確認解除後與重啟後原 selector 不復活。 |
| unassign 錯誤契約與受控測試 | 完成 `282d713e` | schema、scope、BC-18 及退休／衝突測試均存在；修前證據可追溯性見 B-2。 |
| 不執行 `cargo fmt --all`、大型產物位置 | 完成 `282d713e` | 提交 diff 無全域格式化；實作者回報未執行，並將大型產物放在指定 Cargo 目錄。 |

本次在 `282d713e` 以指定 `CARGO_TARGET_DIR` 執行 `cargo test -p sw-daemon --features test-fakes --test dispatch issue281_ -- --test-threads=1`：**19 passed**。未跑完整 pytest、workspace gate、Docker、bundle 或 Windows 原生測試。

## B 新發現

1. **V051-282d713-1 [High]** [session_manager.rs](crates/sw-core/src/session_manager.rs:7960) — 並行裁決未涵蓋 YAML owner。假設 YAML target A 已持有一般名稱 `bench`，dynamic session B 呼叫 `alias.set("bench")`：鎖內因 `is_yaml_alias` 略過 owner 衝突，最終 B 回 `ok:true`，但 `bench` 仍屬 A，B 實際使用自動名稱。反向由 YAML session 呼叫時，`profile_source != "yaml-target"` 也使衝突檢查整段略過。裁決要求名稱已由另一 owner 持有時**一律**回 `INVALID_ARGS`；此缺口未列 SPEC-GAPS。**修法：**保留 YAML 設定 alias 的既有行為，但對公開 `alias.set` 在操作鎖內及提交前按實際 owner 檢查，無論來源為何都拒絕另一 owner；補兩個來源順序的受控測試。

2. **V051-282d713-2 [High]** [修前紅燈紀錄](/srv/thala-e/serialwrap-cargo/v051-i281/i281-f2-red-tests.log) — 修前反例未能綁定實際執行的產品碼版本。log 的 `HEAD` 是 `aea228ee`，但當時工作樹仍含半成品產品碼與測試鉤子；log 只記測試檔雜湊，未記產品碼雜湊或完整 diff。另一次輸出確實顯示兩個契約碼未登錄，但後來保存的 log 在前置衝突斷言失敗，未到達契約斷言。因此無法確認所報四項紅燈實際對應哪個產品碼版本；此項未列 SPEC-GAPS。**修法：**在可識別的修前產品碼上補跑各反例，記錄 commit、必要的測試鉤子差異及產品檔雜湊，並保存確實抵達退休與衝突契約斷言的輸出；若無法重建，列 SPEC-GAPS 並附理由。

## 既有碼待辦（不計入結論）

無。

## 對裁決的異議（不計入結論）

無。

結論：需修正