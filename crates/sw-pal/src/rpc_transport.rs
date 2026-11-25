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

/// 伺服端 send 的短輪詢逾時：只讓寫迴圈定期醒來檢查停止旗標，不是放棄條件（審查 F06）。平時寫入無期限
/// （Python `writer.drain()` 無逾時）；stop 後的總期限（停止時刻 + 2 s）由 sw-daemon framing 自行計算。
#[cfg(unix)]
pub(crate) const SERVER_WRITE_TIMEOUT: Duration = Duration::from_millis(100);
/// Windows：send 逾時後 socket 狀態未定義（Winsock `SO_SNDTIMEO` 文件），維持 2 s 單次逾時且不重試
/// （`SEND_TIMEOUT_RETRYABLE=false`）。LOCAL-WINDOWS-TODO：改 non-blocking＋`WSAPoll` 以對齊 Python 平時無逾時
/// （慢讀大回應不截斷），並以原生慢讀案例驗證。
#[cfg(windows)]
pub(crate) const SERVER_WRITE_TIMEOUT: Duration = Duration::from_secs(2);

/// send 逾時（`ErrorKind::TimedOut`）後可否在同一 socket 重試：POSIX `SO_SNDTIMEO` 逾時為 EAGAIN、不改 socket 狀態；
/// Winsock 逾時後 socket 狀態未定義、不得再用（審查 F102）。framing 只在本值為 true 時把 `TimedOut` 當短輪詢。
#[cfg(unix)]
pub const SEND_TIMEOUT_RETRYABLE: bool = true;
#[cfg(windows)]
pub const SEND_TIMEOUT_RETRYABLE: bool = false;

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

/// 以目前平台的 RPC listener 暫時探測 loopback TCP 位址與埠是否可 bind。
/// 成功時立即關閉 listener；此探測不啟動 RPC accept loop。
pub fn probe_loopback_tcp_bind(
    factory: &dyn crate::traits::RpcListenerFactory,
    host: &str,
    port: u16,
) -> bool {
    use crate::traits::{EndpointSpec, ListenOptions};

    if check_loopback_host(host).is_err() {
        return false;
    }
    let endpoint = EndpointSpec::Tcp {
        host: host.to_string(),
        port,
    };
    let options = ListenOptions {
        mode: 0,
        group: None,
        ensure_dir_mode: None,
    };
    match factory.bind(&endpoint, &options) {
        Ok(listener) => {
            listener.close();
            true
        }
        Err(_) => false,
    }
}

/// 以原生 RPC listener 探測 loopback TCP bind；非 Windows host 只提供模擬成功值。
pub fn native_loopback_tcp_bind_available(host: &str, port: u16) -> bool {
    #[cfg(windows)]
    {
        probe_loopback_tcp_bind(&NativeRpcListenerFactory, host, port)
    }
    #[cfg(not(windows))]
    {
        let _ = (host, port);
        true
    }
}

/// 判斷 TCP 主機是否為 loopback（支援 IPv4 127.0.0.0/8、IPv6 ::1 與 localhost）。
pub fn is_loopback_host(host: &str) -> bool {
    let clean = host
        .strip_prefix('[')
        .and_then(|h| h.strip_suffix(']'))
        .unwrap_or(host);
    clean.eq_ignore_ascii_case("localhost")
        || clean
            .parse::<std::net::Ipv4Addr>()
            .is_ok_and(|addr| addr.is_loopback())
        || clean
            .parse::<std::net::Ipv6Addr>()
            .is_ok_and(|addr| addr.is_loopback())
}

/// 檢查 TCP bind host 是否為 loopback；訊息逐字照 `sw_core/rpc_win.py::_parse_tcp`（Python `ValueError`）。
pub(crate) fn check_loopback_host(host: &str) -> PalResult<()> {
    if is_loopback_host(host) {
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
