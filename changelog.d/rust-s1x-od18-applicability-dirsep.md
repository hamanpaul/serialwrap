---
type: change
scope: rust-refactor
---

依 OD-18 收斂 Stage 1 P4 證據閘門：平台不適用的（案例, 平台, 節點）組合不再要求該平台 rust PASS，但僅限 `check_p4_cases.py` 內 15 列核准清單（`APPROVED_NOT_APPLICABLE`），實算不適用列與清單不一致即 FAIL（待擁有者核准）；每個必要案例仍須至少一個平台有 rust PASS，不適用節點出現紀錄仍計為 FAIL。Windows dev bundle 的 `current-release.json` 指標 `dir` 一律以正斜線 `versions/<V>` 表示（launcher 拒收含反斜線之 dir），計畫 04 Task 17 與 `win-blackbox.ps1` 同步修訂；讀取端仍相容舊反斜線指標。
