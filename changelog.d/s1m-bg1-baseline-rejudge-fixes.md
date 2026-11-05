---
type: fix
scope: rust-stage1
---

修正 OD-75 出口判定：保留 nodeid 的參數、類別與路徑，依 L1 案例引用的全部 A/B 證據套用登記；已登記失敗不再污染其他通過的 JUnit 列，仍對帳其餘必要節點與 suite 失敗數。producer 退出碼只在自己的完整原始報告全部失敗均已獲准時不擋，並保存 BB-proc 與 Stage 1B 原始報告。`--rejudge` 同時重判 gate 與 producer，輸出分類明細，預設以新的出口結果決定退出碼；另提供 `--assert-same-judgment` 比對舊判定。原始 FAIL 與 OD-30～OD-32 行為維持不變。
