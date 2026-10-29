---
type: change
scope: rust-p7
---
新增 Slice 2.3 黑箱契約案例（P7 Task 18）：`test_p7_login.py`（帳密終態零 TX、補帳密不自動重探、shell／prpl／bcm 平台矩陣、SC-35 passthrough 安靜 READY 與 banner 不 gate、console unlock）、`test_p7_quiet_reconfirm.py`（自發重開機 gate、quiet 期間 attach／recover 零 TX、READY bootloader 尾行 D1、ATTACHED bootloader STUCK）、`test_p7_selftest_recover.py`（self-test 六種分類、SC-34 last_error 欄位、force recover 逾時與按鍵、CTRL_C 救援）、`test_p7_stall.py`、`test_p7_events.py`、`test_p7_log_capture.py`、`test_p7_latency_noise.py`、`test_p7_invariants.py`（§11.3-1／-4／-5）；斷言依 Python 基線實測，Python 與 Rust daemon 皆通過。
