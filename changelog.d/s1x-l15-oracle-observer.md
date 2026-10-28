---
type: change
scope: test-infra
---

Stage 1 出口 G5 黑箱 L1.5 oracle 修正（test oracle 變更；審查狀態：待兩位非實作者審查，未放寬任何斷言）：

- `tests/contract_blackbox/test_cli_mgmt_l15.py`：`test_restart_no_reopen_before_handoff`、`test_restart_no_stale_console_replay` 的 holder 交接判準改為 restart 期間背景取樣（`bb_l15.HolderObserver`），斷言取樣中新舊 holder 不得同時存在，且舊 holder 最後一次釋放不晚於新 holder 首次開啟（`bb_l15.assert_holder_handoff`）。Before：操作完成後才以 `wait_holder` 探測並依測試自身的呼叫順序寫入 `closed`／`opened` 標記再比較，因此即使新舊 holder 重疊，`old_close < new_open` 仍必然成立（假綠）。After：取樣自操作開始前即進行，取樣起點須已見舊 holder，重疊即 FAIL；`control.ndjson` 的 `opened`／`closed` 標記改為依取樣邊緣回放，僅供留存、不作判準。
- `tests/contract_blackbox/bb_l15.py`：`run_nodeid_in_pidns` 的子 pytest exit status 非零一律回 `failed`，診斷保留 exit code 與 stderr／stdout 尾段。Before：`del returncode` 丟棄 exit status，僅憑單筆 JUnit 判定，子行程以 exit 1／3 結束而 JUnit 為 passed 時會被判為 PASS（例如 conftest live guard 以 `session.exitstatus=1` 回報的失敗）。
- 新增 `tests/test_bb_l15_holder_observer.py`（含刻意重疊的負向測試，oracle 必須拒絕）與 `tests/test_bb_l15_pidns_exit.py`（stub 子行程回歸）。
- 限制：取樣為 polling（單次掃描約 15–30 ms，觀測期間約佔一個 CPU 核心的大部分），短於一次掃描週期的瞬時重疊無法被觀測；判準為「已取樣到的重疊不得存在」，不宣稱完備。
- 案例名稱與數量不變；`s1b_20groups` 之 nodeid 未改動。新增 nodeid 之 add-only 登記另由 G8 處理。
