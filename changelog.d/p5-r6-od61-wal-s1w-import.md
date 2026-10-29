---
type: change
scope: rust-s21
---
OD-61：Slice 2.1 gate 的 `wal_s1w_perf` 可匯入外部量測證據（`SW_GATE_WAL_S1W_IMPORT`＋主機類型標註 `SW_GATE_WAL_S1W_HOST`），由新子命令 `slice21_gate_judge.py wal-s1w-verdict` 核對 schema、Linux 門檻、正式量測參數、主機類型（wsl2／native）與 `env.git_revision` 可追溯性（本分支歷史且其後 WAL 寫入相關路徑未變更）；雲端 vm 本機量測改只作診斷（EVIDENCE_MISSING），不再判 PASS／FAIL。
