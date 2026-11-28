---
type: feat
scope: stage3
---
依 OD-113 實作 Stage 0～3 出口證據索引與分平台判定：逐項保留目錄中的 required/reference 真實狀態，required 項全滿足才可通過；同 artifact 的正式 FAIL 優先保留，依 A5 換新 RC 與共同輪次、重設兩平台 target 並完整重跑。R1-02 驗證凍結的 S3-E／B5 義務清冊、來源快照、審閱引用與 resolutions，尚未裁決的 BO-P9-14／BO-P9-19 維持阻擋；R3-01 綁定共同候選輪次、archive、target、entry 與歷史 OD-09／合法歷史例外；R4-01 重驗逐出口案例鎖、指定 revision 的 seed 與附件；R7-01 不把私有 hash 或遮罩摘要當成 BO-P9-19 的公開替代驗證；R8-01 以最小 restricted FAIL 列保全已核實失敗；R9-01／R10-01 提供 entries 完整內容只增純函式，並在 PR workflow 逐提交、逐父節點檢查，歷史缺失時 fail closed；R11-01 只接受指定 revision 的靜態 seed 來源與原位元組 SHA。

`.github/workflows/tests.yml` 使用既有 `fetch-depth: 0`，在 PR pytest job 內不加路徑過濾地呼叫跨提交檢查；索引曾於任一 commit 改動即逐父節點檢查，並另比對 merge-base 與 PR head。T14 不修改正式 evidence index、T1 schema 或既有 BO 裁決。

SPEC-GAPS：`legacy_obligations_scope.schema.json` 的 `item_id` 不接受 plan 所允許的 null，測試 fixture 使用合成 ID，本步不改 T1 schema；review `record` 的 schema 目前只固定 FrozenFile，T14 讀取附件中的 `source_hashes` 與 `scope_sha256`，其細部格式仍待規格裁決。BO-P9-14 與 BO-P9-19 均維持阻擋，未假設 owner 已裁決。
