---
type: fix
scope: rust-refactor
issue: null
---

收緊 Stage 1 驗收與 0.4.0 升級驗證：SC-18 以 bridge barrier／checkpoint 核對完整 TX wire 前綴；出口 driver 只豁免 gate 已判定的 OD-30～OD-32 Windows `BLOCKED-OWNER` 列；正式候選 bundle 隔離 Python runtime、自帶 PyYAML 且不編入測試故障注入 feature。升級容器不再安裝系統 PyYAML 或共享 site-packages，避免掩蓋一般 pipx 使用者缺少執行依賴。
