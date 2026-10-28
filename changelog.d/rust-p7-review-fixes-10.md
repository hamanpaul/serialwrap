### Fixed

- Rust 重構 P7 審查第 9 輪（Opus a0cad06_4221ba2）：session 公開快照的 `capture.line_count／byte_count` 改讀 reader 端即時計數（修前恆為 0）；`session.recover force` 的 10×1 s 輪詢改以 clear 返回（drain 回條或 3 s 逾時）為起點，與 Python `_force_recover` 一致（drain 慢時 `recovered:true` 與 `FORCE_RECOVER_TIMEOUT` 不再互換）；`recover_request` 分支只依 `plan_recover`。
