---
type: feat
scope: realhw
---
新增長跑發版判定套件 `realhw/acceptance/`：`longrun_acceptance.yaml` 為發版長跑門檻唯一來源（延後項待 OD-09 凍結），外層 `release_gate_result` 判定器原樣保留引擎 `engine_result` 且不把引擎 FAIL 轉為 PASS；新增唯讀長跑觀測器（只呼叫 `daemon status`、`session list`）、獨立 WAL 掃描器（驗 crc32、重送與禁止窗口），以及 Windows 原生 COM 獨占開啟證據（`winexcl`，零 I/O 的 `CreateFileW` 探測與 handoff 循環）。
