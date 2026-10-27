# Slice 2.1 L1.5 情境與現行 P2 runner 的表達缺口（P5-T21）

本檔記錄 `func-test/scenarios/s21-*.yaml` 以現行 runner（未修改 `func-test/runner.py`）可表達與不可表達之處。
T21 A/B 實跑尚未執行（缺 P2 bridge 資格報告 `selftest-report.json` 與 release artifact），以下缺口須在實跑前
由 runner 擁有者（§10.4 測試資產）裁決是否補動作；不得以放寬斷言取得 PASS。

1. `s21-exclusive-conflict`／`-plain-open`：steps 在 `harness.start()` 之後才執行，無法在 daemon 啟動前 `external_open`；
   現以「`device release` → `external_open` → `device attach`」近似，測不到初次自動 attach 路徑，
   `last_error == ATTACH_FAILED:DeviceHeldError` 與「全段 serial_read 0 byte」無法斷言（只有 op 窗口 `assert_zero_tx`）。
2. `DEVICE_STILL_HELD.pids` 含外部行程 pid：`expect` 不支援 list contains（`{ext.pid}` 展開為字串）；現只斷言
   `error_code`、`holder_unknown:false`、`selector`。
3. `s21-credentials-unresolved`：daemon 啟動到第一個 step 之間的 TX 不在零 TX 窗口內，僅由 `assert_compare`（wire＝WAL）涵蓋。
4. `s21-partial-tx-abort`：BC-03 條件式映射（bytes>0 → `COMMAND_ABORTED_IN_FLIGHT`、=0 → `FLUSHED_BY_RECOVERY`）與
   「該 cmd_id TX len 總和 < 16001」無法表達；release 若需等 writer 解除阻塞才回，依序步驟會卡到 CLI 逾時。
5. `s21-reboot-minimal-guard`：`recovery_action == EXPECT_REBOOT` 或 `done` 的 OR、`dut_generation == 2` 無對應 assertion；
   為觸發 Quiet，`logical_reboot` 加了與 `l15-reboot-min-guard` 相同的 U-Boot `pre_text`。
6. `s21-event-latency`：缺 `stop_request`（每筆樣本會停 daemon，runner 無重啟動作；現量 release_request、command_cancel、
   lease_expiry、drain_receipt）；DUT `flood` 無速率控制；loadavg 超標不會自動重跑；樣本數 500×3 輪預估數千秒，超過
   測試 subprocess 900 s 逾時；計時欄位依賴 test-fakes，Python 基線可能回 INSUFFICIENT_EVIDENCE。
7. 既有 P2 情境 `l15-wal-interference` 為 `compare: none`，不產 `comparison.json`，有證據時兩個 label 必失敗（現為明確的
   INSUFFICIENT_EVIDENCE 訊息）；原檔依規定未改。
8. `external_open` 只能排在 release 之後（daemon 持 TIOCEXCL 時 EBUSY 依 R0-F3 判 FAIL）。

另：`tests/acceptance/l15/test_slice21_l15.py` 的 P2 凍結清單依 `cases.lock.json` `slice_2.1` 為 11 個（計畫程式草稿列 8 個，
與凍結 lock 不符；repo 內 06 計畫第 4827、4842 行亦為 11 個），總項數 43。
