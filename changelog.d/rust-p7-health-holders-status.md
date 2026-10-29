### Fixed

- Rust daemon `health.status` 的 `multi_open_detail.holders_status` 對已 attach 但已消失的 tty 回 `ok`（與 Python `detect_multi_open` 略過不存在 tty 一致），不再沿用更新／回滾確認釋放的 fail-closed 掃描而回 `unknown`；更新／回滾路徑維持 fail-closed（目標 tty 身分不明即 `DEVICE_STILL_HELD`＋`holder_unknown`）。
