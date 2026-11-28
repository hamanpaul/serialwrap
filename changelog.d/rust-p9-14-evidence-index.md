---
type: feat
scope: stage3
---
依 OD-113 實作 Stage 0～3 出口證據索引與分平台判定：逐項保留目錄中的 required/reference 真實狀態，required 項全滿足才可通過；同 artifact 的正式 FAIL 優先保留，依 A5 換新 RC 與共同輪次、重設兩平台 target 並完整重跑。R1-02 驗證凍結的 S3-E／B5 義務清冊、來源快照、審閱引用與 resolutions，尚未裁決的 BO-P9-14／BO-P9-19 維持阻擋；R3-01 綁定共同候選輪次、archive、target、entry 與歷史 OD-09／合法歷史例外；R4-01 重驗逐出口案例鎖、指定 revision 的 seed 與附件；R7-01 不把私有 hash 或遮罩摘要當成 BO-P9-19 的公開替代驗證；R8-01 以最小 restricted FAIL 列保全已核實失敗；R9-01／R10-01 提供 entries 完整內容只增純函式，並在 PR workflow 逐提交、逐父節點檢查，歷史缺失時 fail closed；R11-01 只接受指定 revision 的靜態 seed 來源與原位元組 SHA。

`.github/workflows/tests.yml` 使用既有 `fetch-depth: 0`，在 PR pytest job 內不加路徑過濾地呼叫跨提交檢查；索引曾於任一 commit 改動即逐父節點檢查，並另比對 merge-base 與 PR head。T14 不修改正式 evidence index、T1 schema 或既有 BO 裁決。

SPEC-GAPS：`legacy_obligations_scope.schema.json` 的 `item_id` 不接受 plan 所允許的 null，測試 fixture 使用合成 ID，本步不改 T1 schema；review `record` 的 schema 目前只固定 FrozenFile，T14 讀取附件中的 `source_hashes` 與 `scope_sha256`，其細部格式仍待規格裁決。T1 `candidate_context` 固定七個身分欄位，未定義 tree 身分與原始報告引用；T14 另比對 entry tree 與 target，T1 驗證器拒絕未定義欄位並使 entry STALE，待 T1 契約提供可驗證的引用格式。若來源盤點未提供機器可讀義務鍵、平台與範圍，T14 無法逐筆證明 scope 覆蓋，會回 BLOCKED 並列 SPEC-GAP，不接受僅有一列的清冊。BO-P9-14 與 BO-P9-19 均維持阻擋，未假設擁有者已裁決。

第 1 輪審查修正：所有索引讀取後修改入口共用跨平台鎖；Stage 3 判定重驗 T1 執行 context、seed 與分母治理，拒絕跨輪 reuse，archive 逐檔對照已驗證 bundle；reuse 比對平台、證據 kind、凍結案例與報告範圍。BO-P9-19 影響的必要來源在核定公開替代驗證前一律回 MISSING；legacy gate 先保留正式 FAIL，再驗完整義務清冊與逐平台 resolutions。分母鎖快照改為依內容 hash 不可變保存；Git 歷史測試改走 production guard、正式 CLI 與 workflow 形狀；memory-only fixture 注入固定 revision 來源 bytes。新增紅綠紀錄與回歸案例，未修改正式 evidence index 或 T1／契約檔。
