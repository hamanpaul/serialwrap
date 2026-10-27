### Added

- Rust 重構 P7 Task 14：跨平台事件 handler 子程序執行器（POSIX setsid＋fd CLOEXEC＋killpg SIGTERM→SIGKILL；Windows Job Object、`cmd.exe /d /s /c` shell、只繼承 stdio pipe），對齊 SC-26。
