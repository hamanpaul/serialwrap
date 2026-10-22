# G 待擁有者裁決清單（S1X 組）

由 `owner-decisions-addenda.md` 分出（該檔僅保留表格列，`od09_freeze._append_addenda` 於 EOF 追加）。以下皆為待裁決項目，未裁決前不得視為已核准。

- OD-S1X-01（Linux）：是否同意自 `s1a_cargo`／`s1b_cargo` 移除 ignored 案例（移除者須留在 BLOCKED-OWNER 總表）。
- OD-S1X-01（Windows）：Windows 集合的 add-only 登記須以真機收集輸出完成（WSL 無法產生）。
- OD-S1X-02：Windows golden 採 (i) 依 plan 04 Task 10 的 12 情境並縮減分母（實作目前擷取 14 情境，含 2 boundary），或 (ii) 改 plan 擷取鎖定的全部 test_bb_cli_help 節點（432 筆）。
- OD-S1X-03：docker Rust-CLI L1.5 是否屬 Stage 1 範圍；目前一律 BLOCKED-PREREQ，非 PASS。
- EDR 資料（spike3）：win-wal-av-* 與 win-wal-f12-progress 的 `--edr-product`／`--edr-status`，由擁有者 EDR 主控台查得；未知即 BLOCKED-OWNER，不得 PASS。
- BLOCKED-PREREQ-S1X-04：systemd 實跑的基底 glibc（debian:12-slim／2.36 vs 二進位 GLIBC_2.39）；三個候選見 addenda 列，須擇一裁決後才能 pin 新映像。
- Windows cli_help oracle（`tests/contract_blackbox/test_bb_cli_help.py::test_platform_oracle_exists_and_boundary_values_are_native` 與 windows golden）：屬新 oracle，須非實作者單審通過後才可凍結。
