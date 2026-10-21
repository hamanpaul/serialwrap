---
type: feat
scope: stage3
---
新增 OD-09 凍結結果的 Stage 3 消費端 `tools/stage3/od09_consume.py`（唯讀）：把 P2 `freeze --out` 報告轉為 `od09-freeze` 證據判定、以 P2 `check_frozen_before` 核對候選長跑開跑時刻，並提供 `od09 check` 唯讀委派；延後項的凍結與候選開跑登記仍只由 P2 `realhw/acceptance/od09_freeze.py` 執行；`Derived` 型別移至 `tools/stage3/derived.py`（避免循環匯入），`evidence_ingest.derive()` 新增 `index_doc`（od09-freeze 必填）與 `now` 參數。
