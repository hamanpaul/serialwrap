---
type: fix
issue: 128
---

**recovery/re-attach 時 flush session 命令佇列，修掉 stale accepted 永久佔 pending 額度的洩漏（#128）**：session 因 PROMPT_TIMEOUT 等 recovery 路徑離開 `READY` 時（`_on_detached` → `arbiter.unregister_session`），原本只丟棄 PriorityQueue，佇列中尚未啟動（`status=accepted`、`done_at=None`）的命令記錄永無終結——stale accepted 永久計入 `_count_pending_locked`、佔用 `CMD_PENDING_MAX` 額度，數次 recovery episode 後該 session 直到 daemon 重啟前一律 `SESSION_QUEUE_FULL`，且 client 對被丟棄命令 `command.get` 永遠看到 `accepted`、無從得知已丟失。新增 `CommandArbiter.flush_session()`：unregister 時把尚未啟動的佇列命令以 `status=error`、`error_code=FLUSHED_BY_RECOVERY`、`done_at` 終結（**行為變更**：client 收到此終端態即代表命令未執行、應於 session 回 `READY` 後重送），記錄轉為可淘汰並即刻釋放 pending 額度；in-flight（running/interactive）命令不重複標記，由 worker 以真實結果終結；worker 取件處補「已終結（`done_at` 非 None）即跳過」防 flush/consume race。flush 於 `unregister_session` 首段鎖內與 pop queue 原子完成（防舊 worker 卡 send_cb 超過 join timeout 時，遲到 flush 誤殺 re-register 後新 submit 的 epoch race）；所有 detach 類路徑（clear/release/rebind/熱拔/re-attach）皆用 `FLUSHED_BY_RECOVERY`，daemon shutdown 用 `FLUSHED_BY_SHUTDOWN`（語意相同＝未執行、可重送）；flush 不主動 evict，避免超量時剛 flush 的記錄被當場淘汰成 `CMD_NOT_FOUND`。
