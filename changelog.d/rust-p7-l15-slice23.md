---
type: change
scope: rust-p7
---
新增 Slice 2.3 bridge 模式 wire 證據情境六案（P7 Task 18）`func-test/scenarios/s23-*.yaml`：登入 wire＝WAL（secret 只在 wire）、登入中 U-Boot banner 之後零 TX、登入／逾時救援不重送、登入序列不被 agent／human 夾入、reboot recovery 不回放重開機前輸入、force recover 期間零 TX；`tests/acceptance/l15/test_slice23_l15.py` 以 Python 基線與 Rust daemon 各跑一次。新增 `scripts/rust/p7_chg_probe.py` 與 `docs/rust-refactor/slice23-chg-register.md`，逐列登記第 5 輪案例設計修正並附 Python 基線實測；`p7_gate:*` 測試列必要集合以 `regen_lock --l1-add-only` 登記。
