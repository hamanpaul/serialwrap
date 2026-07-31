---
type: fixed
scope: session_manager
---
修復 background 模式快速完成命令整段輸出遺失且回 `lost: False` 假保證（#159）：複合根因——`BackgroundCapture` 在 prompt 比對成功「之後」才回溯建立（快速完成命令的全部輸出都落在建立之前，`chunks` 恆空）、`_on_bridge_rx` 用 `foreground_busy` gate 連坐擋掉 background capture 的 RX 累積（bg 命令自己的等待視窗內 `foreground_busy=True`）、`maybe_finalize()` 的 quiet-window 活動時鐘建立在從未被真實餵過資料的空殼物件上（首次輪詢即誤判 `done`）。修法三段：capture 於命令送出「之前」掛好（`from_seq` 語意由「TX 寫入 WAL 後」略前移為「TX WAL 記錄本身」，metadata 精度差異）；`_on_bridge_rx` 的 background 累積迴圈移到 `foreground_busy` gate 之前（agent-log capture 維持原 gate 行為）；`_set_terminal_capture_locked` 僅「先前未掛載」（line 模式逾時）分支回填 chunks，避免 CTRL_C 復原路徑對已即時累積的 capture 全量重讀重複疊加。修後 `last_activity_mono` 被真實 RX 正確推進、quiet-window 判定誠實，`dropped_chunks`／`lost` 如實反映唯一剩餘遺失來源（環形上限主動丟棄）。回歸 plugin：`f4-background-result-tail-consistent` oracle 不變，hints 改註「#159 已修，本 case 為其常駐回歸防線」。
