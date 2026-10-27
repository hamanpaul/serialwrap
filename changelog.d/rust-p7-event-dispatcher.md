### Added

- Rust 重構 P7 Task 15：事件 dispatcher 子程序池（per-rule 併發 1、per-daemon 上限、`event_dropped`／`fire_completed`／`fire_timeout`／`fire_failed` 事件與 counter 更新，payload／env 對齊 Python），以及關機時有期限的 handler 終止（SC-11 細化、R1-F2 停止受理）。
