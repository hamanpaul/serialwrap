---
type: change
scope: rust-refactor
---

- contracts：依擁有者裁決 OD-80 登記 DF-26（檔案拉取不依賴 131072 字元 RX 視窗上限，Python 受限、Rust 不受限），`applies_to.rust.stage` 為 `slice-2.4`，案例 `f7-larger-file-not-truncated` 改列 DF-26（不採 XFAIL）。
- tests/contracts：`tests/test_contracts_catalog.py` 更新 DF 範圍斷言至 1～27 並補齊 DF-26 屬性檢查；`tests/test_acceptance_cases.py` 新增 `f7-larger-file-not-truncated` 改列 DF-26 驗證。
- tests/sw-daemon：新增 ServiceRig 端到端回歸測試 `file_pull_over_rx_window_succeeds_od80_df26`，驗證 `file.pull` 於 150 KiB 檔案（base64 超過 131072 字元視窗）時能以分段串流完成拉取與 MD5 驗證。
- acceptance：以 add-only 登記 `file_pull_over_rx_window_succeeds_od80_df26` 至 `l1_required.lock.json`。
