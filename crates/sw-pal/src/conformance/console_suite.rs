//! console backend conformance（P6 Task 1 骨架；斷言由 Task 8 交付）：同一套斷言跑在 fakes、POSIX PTY／TCP、
//! Windows TCP／com0com 上。在 fakes 上通過不得計為原生 gate（OD-08）。

use crate::console_pty::ConsoleTransportFactory;

/// 對某個 `ConsoleTransportFactory` 執行 console conformance（PTY raw termios／keepalive、TCP loopback／NODELAY、mux）。
pub fn console_suite(_factory: &dyn ConsoleTransportFactory) {
    todo!("P6 Task 8")
}
