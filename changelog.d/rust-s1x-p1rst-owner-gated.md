---
type: fix
scope: rust-refactor
---

依計畫 05 L3733 與附錄 B-17 修正 Stage 1 P4 完成度檢查對 `p1-rst-daemon` 的擁有者分類。該案例的 windows-task 面證據須以擁有者 throwaway Windows 工作實跑（`spike2 -Phase restart`）產生，該實跑不得於 73 h 長跑進行中執行，且 stage1 driver 不執行 judge_spike2；先前 `check_p4_cases.py --check` 僅對 L2／L3／manual 且標 `evidence_status: BLOCKED-OWNER` 的案例歸類擁有者缺口，`p1-rst-daemon`（L1.5、無該標記）因而只能判為 FAIL，exit 2 不可達。現改為：`JUDGE_SPIKE2_CASES` 之案例證據檔缺席時判為 BLOCKED-OWNER（exit 2，不得 PASS）；證據檔存在時照一般規則判定，FAIL 紀錄與無效紀錄（含 INSUFFICIENT_EVIDENCE）仍為 FAIL。`JUDGE_SPIKE2_CASES` 改為 `check_p4_cases.py` 的單一共用常數，`register_p4_evidence.py` 直接引用，兩支腳本的 owner 判定不再分歧。本次未修改 `p4_case_map.yaml`、`p4_required.lock.json`、案例 layer 或 evidence_status，亦不屬 lock amendment。
