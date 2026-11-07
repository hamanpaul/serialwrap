> 審查者：claude-fable-5-1（重型任務，擁有者 2026-11-07 指定）

## A 前輪 finding 核對

無（本輪無前輪報告）。

## B 新發現

審查範圍 `32f4bb9b..2e5f1d9f` 共 2 個 commit：`4d4f9264`（量測工具修正＋5 個回歸＋鎖檔 add-only 登記＋changelog fragment）與 `2e5f1d9f`（純文件補記）。無 merge commit。

已實測核對：
- `tests/test_event_latency_measure.py` 於本分支 24 passed；把 `32f4bb9b` 版本的 `event_latency_measure.py` 放到暫存目錄對新測試重跑，4 個新 drain 測試確實紅燈，第 5 個（refused／not canceled）在新舊皆綠，與文件宣稱一致。
- 鎖檔 diff 移除行數為 0，`regen_lock.py --check` 兩項 OK；`tests/test_resource_markers.py` 9 passed；`policy_check` 25 pass、0 fail。
- `foreground_busy` 欄位 Python 由 `to_public_dict()`（`sw_core/session_manager.py:873`）提供，Rust 由 `crates/sw-core/src/session_core.rs:982` 提供，兩端 `session.get_state` 都帶此欄，drain 判準對雙方對稱。Rust 的 `foreground_busy()` 另含 `replay_hold_active()`（`console.rs:124`），但本量測不涉 interactive／replay，不影響 drain。
- 計時區間（`rpc.timed("command.cancel")`）、accepted 10 秒等待、`judge_run()`、100 ms 上限、`contracts/resource_limits.yaml` 皆未改動；錯誤仍逐筆計入 `errors` 並擋 PASS。

**P8-2e5f1d9-1** [Low] `scripts/rust/event_latency_measure.py:488-499` — `deadline = time.monotonic() + 15` 是在 `_wait_state(rpc, "READY")`（自帶 30 s 逾時）返回之後才起算，而新加的錯誤訊息 `elapsed` 卻從 `cancel_returned_at` 計算。情境：取消後 session 進 RECOVERING 20 s 才回 READY、worker 再忙 16 s，訊息會印 `elapsed=36.x s` 搭配「15 秒期限」的註解，讀者無法從訊息判斷期限實際是 45 s 上限。修法：把 `deadline` 改為 `cancel_returned_at + 15`（讓 `_wait_state`、done_at 與 drain 三段真正共用同一期限），或在訊息中另附 `deadline_elapsed`。不影響判準正確性，只影響診斷可讀性。

**P8-2e5f1d9-2** [Low] `scripts/rust/event_latency_measure.py:499` — drain 期限沿用 15 s，而修改前 drain 若落在 (10, 15] s 會以下一筆 accepted timeout 的形式浮現（雖然歸屬錯誤），修改後這個區間變成兩筆皆有效。文件（`s2-par-fixlat-python.md`「為何不是放寬判準」）已如實揭露並論證契約口徑只含 RPC 送出→回應，且本輪 400 筆實測 drain 均 < 0.36 s，故不構成 gate 放寬；但若擁有者希望保守維持「修改前可觀察到的失敗集合不縮小」，可把 drain 期限設為與 accepted 等待相同的 10 s。屬設計取捨，列 Low 供編排者裁決。

其餘無新缺陷。

## 既有碼待辦（不計入結論）

- accepted timeout 發生後，工具不會取消留在佇列中的那筆命令，後續樣本會繼續受其影響（文件已自述、本項無實測證據故未處理）。屬後續子項「僅在 Rust 歸因成立時修正取消後的命令推進」與「接線＋整體驗收」範圍，非本項缺陷。
- `_FakeRpc.session_replies` 以總呼叫次數索引（含 cancel 前的呼叫）；目前正常路徑 cancel 前不會呼叫 `session.get_state`，若未來在 accepted 等待路徑加入狀態輪詢，現有測試的 replies 序列需同步調整。

## 對裁決的異議（不計入結論）

無。

結論：可放行
