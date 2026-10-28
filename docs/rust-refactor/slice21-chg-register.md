# Slice 2.1（P5）測試／oracle 變更登記（CHG）

本檔登記 P5 分支依擁有者裁決（OD-34～OD-44、OD-21）所做的測試、案例鎖、凍結分母與 gate 判定變更。
每列皆須：引用裁決、說明變更前後的判準、證明變更前／錯誤實作會紅（或說明為何不適用）、列出驗證命令。
與主線共用的 `owner-decisions-addenda.md` 分開登記，避免平行 append 衝突；彙整時由主 session 併入。

| CHG | 裁決 | 變更 | 判準是否放寬 | 先紅證據 | 狀態 |
|---|---|---|---|---|---|
| CHG-S21-OD34-DEFER-01 | OD-34 | `tests/acceptance/regen_lock.py` 新增 `--add-only --allow-defer case:from:to:OD-NN`：只允許逐名核准的案例把 `daemon_slice` 改晚，核准須與實際延後完全相符；`cases.yaml` `l15-delayed-reply` 2.1→2.3、revision `2026-10-28.1` | 否：其餘移除／屬性改動仍拒絕；核准多列、少列、OD 不合法皆拒絕 | `tests/test_acceptance_cases.py::test_regen_lock_defer_requires_named_owner_decision`（實作前 argparse 不認得 `--allow-defer`，FAIL） | APPLIED（Review-pending） |
| CHG-S21-OD34-FREEZE-01 | OD-34 | `scripts/rust/freeze_slice_cases.py` 新增 `--amend`／`load_effective_freeze()`：凍結檔本體不改寫，追加 `breaker/slice_2.1.amendments.json` 修訂 #1（扣除 `l15-delayed-reply`、denominator 49→48、記錄當下 lock／cases.yaml provenance）；judge、`slice_breaker.py`、`test_freeze_matches_lock` 改讀有效凍結 | 否：觀測窗口、frozen_at、Stage 2 起算日沿用；有效分母須恰等於鎖的 slice_2.1；修訂鏈遭改動即 ValueError | `tests/test_slice21_case_freeze.py::test_freeze_amend_records_owner_deferral_and_keeps_base`（實作前 `--amend` 不存在，FAIL）；`test_freeze_matches_lock` 於 merge `1393573` 起因主線改 cases.yaml 未升 revision 而 FAIL（sol P5-1393573-2），修訂後轉綠 | APPLIED（Review-pending） |
| CHG-S21-OD34-L15-01 | OD-34 | `tests/acceptance/l15/test_slice21_l15.py`：`l15-delayed-reply`、`s21-reboot-minimal-guard` 自 l15_ab 參數移出（`DEFERRED_TO_S23`），情境檔保留；`tests/test_slice21_scope.py` 列舉清單同步；`slice21_gate_expect.yaml` 新增 `deferred_to_s23` | 否：兩案改列 Slice 2.3 出口必須 PASS；`test_case_list_matches_freeze` 另斷言情境檔仍在、鎖中已在 slice_2.3 | 不適用（範圍變更；裁決前兩案 Rust 恆 FAIL，見 slice21-l15-gaps.md） | APPLIED（Review-pending） |
| CHG-S21-OD40-WAL-01 | OD-40 | `l15-wal-interference` 自 l15_ab 判定移出（`L15_AB_EXCLUDED`），新增 `slice21_gate_expect.yaml` `l15_ab_excluded`：逐名列出承擔的 pytest／cargo nodeid 與所在 section；judge 要求每筆恰為 passed 才可令 l15_ab PASS；補黑箱 A/B `test_wal_reset_archives_and_next_command_recorded`、`test_wal_reader_hold_does_not_block_records`（Python 與 Rust release 皆 PASS） | 否：原 compare:none 恆 INSUFFICIENT 改為具體 pytest 判準；Windows 讀取鎖定列 LOCAL-WINDOWS-TODO | `tests/test_slice21_gate_judge.py::test_l15_ab_excluded_case_requires_covering_pytest_passed`（實作前 l15_ab 維持 PASS，FAIL） | APPLIED（Review-pending） |
| CHG-S21-OD42-PARITY32-01 | OD-42 | parity #32 以 `session_core::boot_quiet_survives_clear_drain_and_blocks_reattach_probe` 替代 `no_redetect_during_quiet`；plan 06 #32 列改名附不可達理由；狀態檔 #32 改 PASS（4 支 nodeid 皆在 s21_core） | 否 | 不適用（替代案例沿用既有 F63 回歸） | APPLIED |
| CHG-S21-PAOD1-SC37-01 | OD-21 (1) | `contracts/behavior_changes.yaml` 新增 SC-37（rust_only、absolute_only）；`tests/test_contracts_catalog.py` 範圍斷言加 SC-37；parity #6／#71 改 PASS | 否 | `test_bc_real_sc_df_ranges` 先加 SC-37 斷言（FAIL）再登記 | APPLIED |
| CHG-S21-OD44-BREAKER-01 | OD-44 | 斷路器維持固定窗口（實作不改）；plan 09 斷路器段合併主線 OD-44 寫法；spec-reconciliation NC-P5-02 改 RESOLVED | 否 | 不適用（文件） | APPLIED |
