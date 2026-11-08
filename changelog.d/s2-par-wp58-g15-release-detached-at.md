---
type: fix
scope: rust-refactor
---
Rust daemon 的 `device.release` 收尾（drain、無 bridge、開埠在途三條路徑）比照 Python `_complete_drain`／`_detach_session_locked` 更新 `detached_at`，並把 `reprobe_attempts`、`next_reprobe_at`、`reprobe_exhausted` 歸零；`session.clear` 收尾（drain 與無 bridge，裝置在場或不在場）同樣歸零 reprobe 進度，無 bridge 的 clear 即使原本已是 `DETACHED` 也更新 `detached_at`。已 `RELEASED` 的冪等 release 不刷新 `detached_at` 與 `released_*`（對齊 Python `sw_core/session_manager.py` S1X-a-7）。
