//! RPC transport 實作放在 `posix`／`windows` 子模組，依目標平台擇一編譯。
//! P5-T17：server 端 listener 放在 `listener_posix`（AF_UNIX＋TCP loopback）／`listener_windows`（TCP loopback）。
#[cfg(unix)]
mod listener_posix;
#[cfg(windows)]
mod listener_windows;
#[cfg(unix)]
pub mod posix;
#[cfg(windows)]
pub mod windows;

use crate::traits::{PalError, PalErrorKind, PalResult};
use std::time::Duration;

/// RPC server 僅允許的 bind host（`sw_core/constants.py::LOOPBACK_TCP_HOSTS`；sw-pal 不依賴 sw-proto，故於此重述）。
pub const LOOPBACK_TCP_HOSTS: [&str; 3] = ["127.0.0.1", "localhost", "::1"];

/// 伺服端寫回應的單次 send 逾時（計畫 Task 17 Produces ③：寫逾時 2 s 後放棄，不阻塞 stop）。
pub(crate) const SERVER_WRITE_TIMEOUT: Duration = Duration::from_secs(2);

/// 原生 RPC listener 工廠（arch §4.6；P5-T17 交付 POSIX AF_UNIX／Windows loopback TCP）。
pub struct NativeRpcListenerFactory;

impl crate::traits::RpcListenerFactory for NativeRpcListenerFactory {
    fn bind(
        &self,
        ep: &crate::traits::EndpointSpec,
        opts: &crate::traits::ListenOptions,
    ) -> crate::traits::PalResult<Box<dyn crate::traits::RpcListener>> {
        #[cfg(unix)]
        {
            listener_posix::bind(ep, opts)
        }
        #[cfg(windows)]
        {
            listener_windows::bind(ep, opts)
        }
    }
}

/// 檢查 TCP bind host 是否為 loopback；訊息逐字照 `sw_core/rpc_win.py::_parse_tcp`（Python `ValueError`）。
pub(crate) fn check_loopback_host(host: &str) -> PalResult<()> {
    if LOOPBACK_TCP_HOSTS.contains(&host) {
        return Ok(());
    }
    Err(PalError::plain(
        PalErrorKind::InvalidInput,
        format!(
            "RPC server 僅允許 loopback 位址（127.0.0.1/localhost/::1），拒絕 {} 避免遠端暴露（RPC 無認證，非 loopback 會讓遠端呼叫所有命令）",
            py_repr_str(host)
        ),
    ))
}

/// Python `repr(str)`：預設單引號；含 `'` 且不含 `"` 時改用雙引號；跳脫反斜線與不可列印 ASCII 控制字元。
fn py_repr_str(s: &str) -> String {
    let quote = if s.contains('\'') && !s.contains('"') { '"' } else { '\'' };
    let mut out = String::with_capacity(s.len() + 2);
    out.push(quote);
    for ch in s.chars() {
        match ch {
            '\\' => out.push_str("\\\\"),
            '\n' => out.push_str("\\n"),
            '\r' => out.push_str("\\r"),
            '\t' => out.push_str("\\t"),
            c if c == quote => {
                out.push('\\');
                out.push(c);
            }
            c if (c as u32) < 0x20 || c as u32 == 0x7f => {
                out.push_str(&format!("\\x{:02x}", c as u32));
            }
            c => out.push(c),
        }
    }
    out.push(quote);
    out
}

/// 有界等待的剩餘毫秒數（poll 用；上限 i32::MAX）。
pub(crate) fn timeout_ms(d: Duration) -> i32 {
    d.as_millis().min(i32::MAX as u128) as i32
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn py_repr_matches_python() {
        assert_eq!(py_repr_str("0.0.0.0"), "'0.0.0.0'");
        assert_eq!(py_repr_str("a'b"), "\"a'b\"");
        assert_eq!(py_repr_str("a'\"b"), "'a\\'\"b'");
        assert_eq!(py_repr_str("a\\b"), "'a\\\\b'");
    }
}
