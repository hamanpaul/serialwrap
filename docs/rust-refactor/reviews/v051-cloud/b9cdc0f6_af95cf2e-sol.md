> 審查者：codex gpt-6.1-terra xhigh（擁有者 2026-11-23 指定）

## A 前輪 finding 核對

無。

## B 新發現

定向驗證：在 `af95cf2e` 執行 8 個 `issue281` Rust 測試，**8 passed**。另以 JSON Schema validator 重現下列契約問題；其餘情境依程式路徑分析，尚未另作受控交錯實跑。

1. **V051-af95cf2-1 [High]** [rpc_schema.json](contracts/rpc_schema.json:882) — `AliasRow` 同時排除了新的 Rust 輸出與既有 Python 輸出。Dynamic `alias.set` 產生的列同時有 `session_id`、`device_by_id`，因此同時符合 `oneOf` 的兩個分支而驗證失敗；Python 列則沒有新列為必填的 `effective`、`conflict`，也驗證失敗。已用 validator 對兩種列各重現一次。**修法：**讓 schema 接受 session 列附帶 device 關聯，並保留 Python 凍結輸出的合法性；以實際 Rust 與 Python `alias.list` 回應各補一個 schema 驗證。

2. **V051-af95cf2-2 [High]** [session_manager.rs](crates/sw-core/src/session_manager.rs:2417) — 建立 dynamic session 時，只檢查所選名稱是否被*線上* session 使用，沒有檢查自動名稱是否已由離線裝置登錄。例如 registry 的 `t+1` 已指派給離線裝置 B，裝置 A 啟動且預設名稱也是 `t+1`：A 仍取得該名稱，`alias.list` 還會把 B 的列標成 effective；以 `t+1` 下命令會指向 A。**修法：**建立與 RELEASED 還原時都檢查 registry claim；自動名稱被其他 owner 占用時，選擇未占用的自動名稱並正確標示衝突。補離線 owner 的重啟測試。

3. **V051-af95cf2-3 [High]** [session_manager.rs](crates/sw-core/src/session_manager.rs:7776) — alias 衝突檢查和持久化之間會釋放 manager lock，提交時只重驗 actor 身分，沒有重新驗證名稱。兩個 session 同時 `alias.set` 同一個尚未登錄的名稱時，可以都通過檢查、各自更新 actor，最後一筆 registry 寫入覆蓋另一筆；兩個 session 都使用同一 selector，命令可能送往非預期 session。**修法：**以可跨 actor 等待的 claim 保留或序列化機制，讓衝突判定、actor／manager 更新與 registry 提交形成一致的操作；補兩個 session 同名設定，以及 `alias.set` 與 `alias.assign` 的受控交錯測試。

4. **V051-af95cf2-4 [High]** [session_manager.rs](crates/sw-core/src/session_manager.rs:7922) — 新的即時 `alias.assign` 只檢查名稱是否與其他 alias 重複，沒有檢查是否與其他 session 的 COM 或 session ID 重複。例如 A 指派 alias `COM1`，而 B 的 COM 是 `COM1`：兩者都符合 selector `COM1`，解析依 session 迭代順序可能選到 A，原本送給 B 的命令會送錯裝置。**修法：**建立或重新解析 alias 時，同時檢查其他 session 的 COM、session ID 與 alias；衝突依既定規則退回安全的自動名稱，並補精確驗證 selector 指向的測試。

5. **V051-af95cf2-5 [High]** [session_manager.rs](crates/sw-core/src/session_manager.rs:7946) — 本次讓 `alias.assign` 立即更新線上 session，但既有 `alias.unassign` 只刪 registry，沒有重新解析線上 actor／profile。對 A 指派 `bench` 後再解除 `bench`，解除指派回成功，`bench` 仍可作 selector；重啟後才消失，違反「只有目前生效 alias 可作 selector」的新增契約。**修法：**解除生效指派時，依 by-id／session／自動名稱優先序重新解析並同步 actor 與 manager，再回覆成功；補解除指派前後及重啟後的 selector 測試。

## 既有碼待辦（不計入結論）

無。

## 對裁決的異議（不計入結論）

無。

結論：需修正