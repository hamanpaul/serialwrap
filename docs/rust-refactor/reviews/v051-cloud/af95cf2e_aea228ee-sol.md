> 審查者：codex gpt-6.1-terra xhigh（擁有者 2026-11-23 指定）

## A 前輪 finding 核對

| 編號 | 原嚴重度 | 狀態 | 累計未修輪數 | 說明 |
|---|---|---|---:|---|
| V051-af95cf2-1 | High | 已修 `aea228ee` | 0 | schema 接受雙 owner 欄位及 Python 舊輸出；兩端實際回應的 schema 測試均存在。 |
| V051-af95cf2-2 | High | 已修 `aea228ee` | 0 | dynamic 建立與 RELEASED 還原均檢查離線 claim；定向測試包含重啟與衝突標示。 |
| V051-af95cf2-3 | High | 未修 | 1 | 操作鎖修正了測試指定的交錯，但另一個合法交錯仍會讓兩個同名請求都回成功；見 B-1。 |
| V051-af95cf2-4 | High | 未修 | 1 | 已存在的 COM／session ID 指派有防護；後插入 session 的 COM 仍可能被較早的 alias 遮蔽，見 B-2。 |
| V051-af95cf2-5 | High | 未修 | 1 | `bench` 的一般情境已修；自訂 alias 恰好等於自動名稱時，解除後會復活，見 B-3。 |

## C 完整性核對

| 項目 | 狀態 | 證據 |
|---|---|---|
| 五項各有修前反例與修正 | 部分完成 | 修正均在 `aea228ee`；可讀到綁定 `af95cf2e` 的六項 Rust 紅燈及 Python schema 紅燈。並行、selector 和 unassign 尚有 B-1～B-3 的未覆蓋情境，未列入 SPEC-GAPS。 |
| 不放寬既有斷言、不刪除或改名既有測試 | 完成 `aea228ee` | 本次 diff 的測試節點為新增；L1 鎖變更為新增節點。 |
| finding 對照表及綁定 SHA 的驗收回報 | 完成 `aea228ee` | 實作者回報包含五項對照及驗收 SHA；本次另行核對 commit 並執行定向測試。 |
| schema、離線 claim、並行、selector、unassign 與 BC-18 | 部分完成 | schema、離線 claim 與 BC-18 已提交；其餘缺口見 B-1～B-3。 |
| 不執行全 crate／workspace 格式化 | 完成 `aea228ee` | 本輪提交 diff 無全域格式化變更；實作者亦回報未執行 `cargo fmt`。 |

定向驗證於 `aea228eebde669aa9c724190ecfa22ec35532256` 執行：Rust `issue281_` **15 passed**；Python schema 與兩組凍結分母 **119 passed**；`tools.contracts check` 通過。以下新增情境為程式路徑分析，尚未以受控交錯實跑。完整 workspace gate 與 Windows 原生收集仍依實作者回報待完成。

## B 新發現

1. **V051-aea228e-1 [High]** [session_manager.rs](crates/sw-core/src/session_manager.rs:7926) — 同名並行設定仍可能都回成功。A、B 同時呼叫 `alias.set("shared")`，若 B 的 preflight 恰在 A 已提交 claim、但 A 尚未返回時執行，B 的 `preflight_conflict=true`；之後兩處重驗都因 `!preflight_conflict` 為 false 而跳過拒絕。B 最後回 `ok:true`，即使 `shared` 仍屬 A。現有 barrier 測試只固定兩者都在 claim 建立前完成 preflight。**修法：**在操作鎖內依目前 owner 決定衝突結果，對另一 owner 已持有的名稱回 `INVALID_ARGS`；補上 A 提交後、返回前才讓 B 執行 preflight 的受控測試。

2. **V051-aea228e-2 [High]** [session_manager.rs](crates/sw-core/src/session_manager.rs:2515) — selector 唯一性在熱插入後仍未成立。先讓 A 使用 `COM0`，在尚無 `COM1` 時對 A 執行 `alias.assign`，名稱設為 `COM1`；再插入 B，B 可獲配 COM1。建立 B 時只檢查 **B 選出的 alias** 是否撞到既有名稱，沒有檢查 **B 的 COM** 是否撞到 A 的 alias。`resolve` 依插入順序同時比對 COM 與 alias，因此 `COM1` 可先命中 A，命令送錯裝置。**修法：**session 建立或還原時，也檢查新 COM／session ID 是否被其他生效 alias 使用，依既定衝突規則重新解析該 alias；補先指派、後熱插及重啟後的精確 selector 測試。

3. **V051-aea228e-3 [High]** [session_manager.rs](crates/sw-core/src/session_manager.rs:8160) — 解除與自動名稱相同的自訂 alias 會立即復活。例如 dynamic session 的自動名稱為 `t+1`，先對它執行 `alias.set("t+1")`，再執行 `alias.unassign("t+1")`：移除自訂列後，fallback 又選到同名 `t+1`，程式將其重新寫成 automatic 列並回成功；該 selector 仍有效。**修法：**解除生效名稱時，若 fallback 與被解除名稱相同，配發另一個未占用的自動名稱，並同步 actor、profile、registry；補解除前後及重啟後測試。

4. **V051-aea228e-4 [High]** [rpc_schema.json](contracts/rpc_schema.json:1306) — 新增的 `alias.unassign` 生命週期分支可在等待 actor 後發現 session 已退休，回傳 `SESSION_NOT_FOUND`；但 RPC 契約仍只列 `ALIAS_NOT_FOUND`。例如解除 RELEASED placeholder 的 alias 與 placeholder 退休交錯，即可能產生契約未列出的回應。`INVALID_ARGS` 分支也未登錄於該方法的錯誤清單。**修法：**讓實作與契約一致：明確決定這些交錯的回應，更新 RPC schema、錯誤碼 scope 與 BC-18，並以受控退休／衝突測試驗證實際回應。

## 既有碼待辦（不計入結論）

無。

## 對裁決的異議（不計入結論）

無。

結論：需修正