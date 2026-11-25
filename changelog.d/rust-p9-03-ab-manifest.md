---
type: feat
scope: stage3
---

新增 Sequential A/B manifest 載入與凍結檢查：依 OD-113 錨定出口 ID、候選 context、B 退役候選與同 CLI SHA 配對；依 R4-01 重驗 T1 逐出口分母、固定 seed、family 不可重分類與只增契約。`tool_revision` 是 repo 凍結輸入的唯一版本錨點；檔案比對原始 Git blob，工具與 contracts 樹比較完整成員集合並逐檔核對。`base_revision` 是選用的額外只增檢查，Task 6 可照原介面呼叫。依 R12-01 加入僅適用 Windows L1.5 出口的逐案傳輸映射格式與驗證，以注入的 `trusted_pair_map` 綁定核准 COM pair、兩個不同的 COM 埠、不同的去識別 port ID 與精確 allowlist；未注入時回報 BLOCKED-OWNER（BO-P9-01），映射必須位於 repo 內並納入 tool_revision；權威映射與實際對照內容由 T5B 建立。執行產物以 SHA 與交叉綁定凍結；只有 Git index 明確未追蹤且 tool_revision 無該路徑時才豁免版本比對。缺少 release component `path` 會回報明確違規，具名 digest-only assets 保持合法。1A／1B 的 Python 基線元件與 Docker image 亦須通過最小身分驗證。依 R2-F7 驗證 bundle 元件、完整樹與路徑邊界；依 R3-F2 提供 runtime manifest 摘要及排除根層 metadata 的受測樹摘要；依 R4-03 拒絕 bundle 樹中的 symlink 與非一般檔案。
