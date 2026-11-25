---
type: feat
scope: stage3
---

新增 Sequential A/B manifest 載入與凍結檢查：依 OD-113 錨定出口 ID、候選 context、B 退役候選與同 CLI SHA 配對；依 R4-01 重驗 T1 逐出口分母、固定 seed、family 不可重分類與只增契約。`tool_revision` 是 repo 凍結輸入的唯一版本錨點；檔案比對原始 Git blob，工具與 contracts 樹比較完整成員集合並逐檔核對。`base_revision` 是選用的額外只增檢查，Task 6 可照原介面呼叫。loader 從明確的 `repo_root` 讀取 A/B schema 與 Windows L1.5 的可信 pair schema，保存實際載入 schema 位元組摘要，凍結檢查再比對目前檔案及 tool_revision；loader 獨立要求 `tools/stage3` 樹，並要求 import 快照、目前 `tools/stage3` 樹與該 FrozenTree 三者一致，執行根目錄固定為本模組 checkout。manifest source、release sidecar、候選 context、Stage 3 source-manifest 與 Windows L1.5 transport map 都以單次讀取的位元組解析並計算 SHA，保存載入摘要；凍結檢查核對載入摘要、目前檔案與宣告值。依 R12-01 加入僅適用 Windows L1.5 出口的逐案傳輸映射格式與驗證，以注入的 `trusted_pair_map` 綁定核准 COM pair、兩個不同的 COM 埠、不同的去識別 port ID 與精確 allowlist；未注入時回報 BLOCKED-OWNER（BO-P9-01），映射必須位於 repo 內並納入 tool_revision；權威映射與實際對照內容由 T5B 建立。執行產物以 SHA 與交叉綁定凍結；repo 外 profile 僅接受絕對路徑，載入時雜湊並於執行前重驗；repo 內 profile 維持 tool_revision 控制。repo 內的 FrozenTree、release component、package 與 delegate 樹成員統一經同一路徑分類；repo 內執行產物只對 ASCII 路徑使用 Git `:(icase,literal)` 查詢；含非 ASCII 字元的路徑保守拒絕，拼字不符或有歧義亦拒絕；只有 index 與 tool_revision 均確認未追蹤時才豁免版本比對。缺少 release component `path` 會回報明確違規，具名 digest-only assets 保持合法。1A／1B 的 Python 基線元件與 Docker image 亦須通過最小身分驗證。依 R2-F7 驗證 bundle 元件、完整樹與路徑邊界；依 R3-F2 提供 runtime manifest 摘要及排除根層 metadata 的受測樹摘要；依 R4-03 拒絕 bundle 樹中的 symlink 與非一般檔案。

第 7 輪將 `load_manifest`、`verify_frozen` 與其案例／seed 驗證呼叫鏈改為每次呼叫共用 resolved-path bytes 快照，解析、SHA、coverage、授權與工具版本比對均使用同一份輸入。Git NUL 路徑清單改以 bytes 擷取並 fail closed；repo 路徑含 ASCII 控制字元（0x00–0x1F、0x7F）時拒絕，以保留路徑身分。
