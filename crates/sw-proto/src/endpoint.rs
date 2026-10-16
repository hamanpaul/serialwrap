//! RPC endpoint 型別與解析（P3-T7）。
//!
//! 逐條承接 `sw_core/client.py::_parse_endpoint`，其中 `urlsplit` 以手寫子集移植自 CPython 3.12.3
//! `Lib/urllib/parse.py`（`urlsplit`、`_splitnetloc`、`_check_bracketed_netloc`、`_check_bracketed_host`、
//! `_checknetloc`、`_hostinfo`、`hostname`、`port`），並以 `contracts/golden/endpoint.tsv` 逐列比對。
//!
//! 錯誤訊息為 Python `str(ValueError)`：包含 endpoint 的部分以 `repr` 格式輸出（見 [`crate::pyfmt::py_repr_str`]）。

use crate::constants::LOOPBACK_TCP_HOSTS;
use crate::pycompat::INT_MAX_STR_DIGITS;
use crate::pyfmt::py_repr_str;

/// 解析後的 RPC endpoint。
#[derive(Clone, Debug, PartialEq, Eq)]
pub enum Endpoint {
    /// AF_UNIX socket 路徑。
    Unix(String),
    /// TCP 位址；`host` 已轉小寫（與 Python `hostname` 相同）。
    Tcp { host: String, port: u16 },
}

/// endpoint 解析錯誤，內容為 Python `str(ValueError)`。
#[derive(Clone, Debug, PartialEq, Eq)]
pub struct EndpointError(pub String);

/// `_WHATWG_C0_CONTROL_OR_SPACE` 的成員：U+0000..=U+0020。
fn is_c0_or_space(c: char) -> bool {
    (c as u32) <= 0x20
}

/// `scheme_chars`：ASCII 字母、數字與 `+-.`。
fn is_scheme_char(c: char) -> bool {
    c.is_ascii_alphanumeric() || matches!(c, '+' | '-' | '.')
}

/// NFKC 正規化後會含 `/ ? # @ :` 的非 ASCII 碼位（CPython 3.12.3 `unicodedata` / Unicode 15.0.0 全表掃描得出；
/// 命中即 `_checknetloc` 拒絕）。ASCII 的 `@ : # ?` 在 `_checknetloc` 中先被移除，故只需檢查這份非 ASCII 表。
const NFKC_PUNCT_CODEPOINTS: [char; 19] = [
    '\u{2047}', '\u{2048}', '\u{2049}', '\u{2100}', '\u{2101}', '\u{2105}', '\u{2106}', '\u{2a74}',
    '\u{fe13}', '\u{fe16}', '\u{fe55}', '\u{fe56}', '\u{fe5f}', '\u{fe6b}', '\u{ff03}', '\u{ff0f}',
    '\u{ff1a}', '\u{ff1f}', '\u{ff20}',
];

/// `_checknetloc`（非 ASCII netloc 的 NFKC 字元拒絕）。
fn checknetloc(netloc: &str) -> Result<(), String> {
    if netloc.is_empty() || netloc.is_ascii() {
        return Ok(());
    }
    if netloc.chars().any(|c| NFKC_PUNCT_CODEPOINTS.contains(&c)) {
        return Err(format!(
            "netloc '{netloc}' contains invalid characters under NFKC normalization"
        ));
    }
    Ok(())
}

/// `ipaddress.IPv4Address` 的嚴格解析（4 段、ASCII 十進位、不得有前導零、每段 ≤ 255）。
fn py_ipv4_valid(s: &str) -> bool {
    if s.is_empty() {
        return false;
    }
    let octets: Vec<&str> = s.split('.').collect();
    if octets.len() != 4 {
        return false;
    }
    octets.iter().all(|o| {
        !o.is_empty()
            && o.len() <= 3
            && o.bytes().all(|b| b.is_ascii_digit())
            && (*o == "0" || !o.starts_with('0'))
            && o.parse::<u32>().is_ok_and(|v| v <= 255)
    })
}

/// `ipaddress.IPv6Address._ip_int_from_string` 的有效性判定（含 `%` scope 與內嵌 IPv4 尾段）。
fn py_ipv6_valid(s: &str) -> bool {
    // `_split_scope_id`：`%` 之後必須非空且不含第二個 `%`。
    let addr = match s.split_once('%') {
        None => s,
        Some((a, scope)) => {
            if scope.is_empty() || scope.contains('%') {
                return false;
            }
            a
        }
    };
    if addr.is_empty() || addr.contains('/') {
        return false;
    }
    let mut parts: Vec<&str> = addr.split(':').collect();
    if parts.len() < 3 {
        return false;
    }
    if parts.last().is_some_and(|p| p.contains('.')) {
        let v4 = parts.pop().unwrap_or_default();
        if !py_ipv4_valid(v4) {
            return false;
        }
        // 內嵌 IPv4 轉為兩個十六進位段；此處只需有效性，故以合法十六進位佔位。
        parts.push("0");
        parts.push("0");
    }
    if parts.len() > 9 {
        return false;
    }
    let len = parts.len();
    let mut skip: Option<usize> = None;
    for (i, p) in parts.iter().enumerate().take(len - 1).skip(1) {
        if p.is_empty() {
            if skip.is_some() {
                return false;
            }
            skip = Some(i);
        }
    }
    let (hi, lo) = match skip {
        Some(sk) => {
            let mut hi = sk;
            let mut lo = len - sk - 1;
            if parts[0].is_empty() {
                if sk != 1 {
                    return false;
                }
                hi = 0;
            }
            if parts[len - 1].is_empty() {
                if lo != 1 {
                    return false;
                }
                lo = 0;
            }
            if hi + lo > 7 {
                return false;
            }
            (hi, lo)
        }
        None => {
            if len != 8 || parts[0].is_empty() || parts[len - 1].is_empty() {
                return false;
            }
            (len, 0)
        }
    };
    let hextet_ok =
        |p: &str| !p.is_empty() && p.len() <= 4 && p.bytes().all(|b| b.is_ascii_hexdigit());
    parts[..hi].iter().all(|p| hextet_ok(p)) && parts[len - lo..].iter().all(|p| hextet_ok(p))
}

/// `_check_bracketed_host`。
fn check_bracketed_host(hostname: &str) -> Result<(), String> {
    if let Some(rest) = hostname.strip_prefix('v') {
        // `\Av[a-fA-F0-9]+\..+\Z`
        let hex_len = rest.bytes().take_while(|b| b.is_ascii_hexdigit()).count();
        let after = &rest[hex_len..];
        let ok =
            hex_len > 0 && after.starts_with('.') && after.len() > 1 && !after[1..].contains('\n');
        if !ok {
            return Err("IPvFuture address is invalid".to_string());
        }
        return Ok(());
    }
    // `ipaddress.ip_address`：先 IPv4（成功即拋 IPv4 錯誤），再 IPv6，皆失敗則通用訊息。
    if py_ipv4_valid(hostname) {
        return Err("An IPv4 address cannot be in brackets".to_string());
    }
    if py_ipv6_valid(hostname) {
        return Ok(());
    }
    Err(format!(
        "{} does not appear to be an IPv4 or IPv6 address",
        py_repr_str(hostname)
    ))
}

/// `_check_bracketed_netloc`（netloc 同時含 `[` 與 `]` 時）。
fn check_bracketed_netloc(netloc: &str) -> Result<(), String> {
    let hostname_and_port = netloc.rsplit_once('@').map_or(netloc, |(_, h)| h);
    if let Some((before, bracketed)) = hostname_and_port.split_once('[') {
        if !before.is_empty() {
            return Err("Invalid IPv6 URL".to_string());
        }
        let (hostname, port) = bracketed.split_once(']').unwrap_or((bracketed, ""));
        if !port.is_empty() && !port.starts_with(':') {
            return Err("Invalid IPv6 URL".to_string());
        }
        check_bracketed_host(hostname)
    } else {
        let hostname = hostname_and_port
            .split_once(':')
            .map_or(hostname_and_port, |(h, _)| h);
        check_bracketed_host(hostname)
    }
}

/// `urlsplit` 的結果（只保留 `_parse_endpoint` 會用到的欄位）。
struct SplitResult {
    scheme: String,
    netloc: String,
    path: String,
    query: String,
    fragment: String,
}

/// `urlsplit(url)`（allow_fragments=True）。
fn urlsplit(input: &str) -> Result<SplitResult, String> {
    let lstripped = input.trim_start_matches(is_c0_or_space);
    let mut url: String = lstripped
        .chars()
        .filter(|c| !matches!(c, '\t' | '\r' | '\n'))
        .collect();
    let mut scheme = String::new();
    if let Some(i) = url.find(':') {
        let first_ok = url.chars().next().is_some_and(|c| c.is_ascii_alphabetic());
        if i > 0 && first_ok && url[..i].chars().all(is_scheme_char) {
            scheme = url[..i].to_ascii_lowercase();
            url = url[i + 1..].to_string();
        }
    }

    let mut netloc = String::new();
    if url.starts_with("//") {
        // `_splitnetloc(url, 2)`
        let delim = url[2..].find(['/', '?', '#']).map_or(url.len(), |k| k + 2);
        netloc = url[2..delim].to_string();
        url = url[delim..].to_string();
        let has_open = netloc.contains('[');
        let has_close = netloc.contains(']');
        if has_open != has_close {
            return Err("Invalid IPv6 URL".to_string());
        }
        if has_open && has_close {
            check_bracketed_netloc(&netloc)?;
        }
    }

    let mut fragment = String::new();
    if let Some((u, f)) = url.split_once('#') {
        fragment = f.to_string();
        url = u.to_string();
    }
    let mut query = String::new();
    if let Some((u, q)) = url.split_once('?') {
        query = q.to_string();
        url = u.to_string();
    }
    checknetloc(&netloc)?;
    Ok(SplitResult {
        scheme,
        netloc,
        path: url,
        query,
        fragment,
    })
}

/// `_hostinfo` 的 port 欄（空字串視為 None）。
fn raw_port_of(netloc: &str) -> Option<&str> {
    let hostinfo = netloc.rsplit_once('@').map_or(netloc, |(_, h)| h);
    let port = if let Some((_, bracketed)) = hostinfo.split_once('[') {
        let (_, after) = bracketed.split_once(']').unwrap_or((bracketed, ""));
        after.split_once(':').map_or("", |(_, p)| p)
    } else {
        hostinfo.split_once(':').map_or("", |(_, p)| p)
    };
    if port.is_empty() {
        None
    } else {
        Some(port)
    }
}

/// `SplitResult.port`：None 表示未給；非十進位 ASCII、超出 0..=65535，或位數超過 CPython `int()` 上限回傳 `Err(())`。
///
/// 位數上限在去除前導零之前檢查：Python `int(port)` 計入前導零（`"0"*4300 + "1"` 為 4301 位，拋 `ValueError`；
/// CPython 3.12.3 實測），與 [`INT_MAX_STR_DIGITS`] 一致。
fn parse_port(netloc: &str) -> Result<Option<u16>, ()> {
    let Some(port) = raw_port_of(netloc) else {
        return Ok(None);
    };
    if port.is_empty() || !port.bytes().all(|b| b.is_ascii_digit()) {
        return Err(());
    }
    if port.len() > INT_MAX_STR_DIGITS {
        return Err(());
    }
    let trimmed = port.trim_start_matches('0');
    if trimmed.len() > 5 {
        return Err(());
    }
    let value: u32 = if trimmed.is_empty() {
        0
    } else {
        trimmed.parse().map_err(|_| ())?
    };
    if value > 65535 {
        return Err(());
    }
    Ok(Some(value as u16))
}

/// `SplitResult.hostname`：空則 None；否則 `%` 之前小寫，之後（scope）保留原樣。
fn hostname_of(netloc: &str) -> Option<String> {
    let hostinfo = netloc.rsplit_once('@').map_or(netloc, |(_, h)| h);
    let hostname = if let Some((_, bracketed)) = hostinfo.split_once('[') {
        bracketed.split_once(']').map_or(bracketed, |(h, _)| h)
    } else {
        hostinfo.split_once(':').map_or(hostinfo, |(h, _)| h)
    };
    if hostname.is_empty() {
        return None;
    }
    Some(match hostname.split_once('%') {
        Some((h, zone)) => format!("{}%{}", h.to_lowercase(), zone),
        None => hostname.to_lowercase(),
    })
}

fn invalid_tcp(endpoint: &str) -> EndpointError {
    EndpointError(format!("invalid tcp endpoint: {}", py_repr_str(endpoint)))
}

/// 解析 RPC endpoint（`client._parse_endpoint`）。
///
/// - 含 `://`：經 `urlsplit`；scheme 僅接受 `tcp`（需 host＋port，且無 path／query／fragment 內容）與 `unix`（需絕對路徑、無 netloc）。
/// - 不含 `://`：視為裸 AF_UNIX 路徑。
pub fn parse_endpoint(s: &str) -> Result<Endpoint, EndpointError> {
    if !s.contains("://") {
        return Ok(Endpoint::Unix(s.to_string()));
    }
    let parsed = urlsplit(s).map_err(EndpointError)?;
    match parsed.scheme.as_str() {
        "tcp" => {
            let port = parse_port(&parsed.netloc).map_err(|()| invalid_tcp(s))?;
            let host = hostname_of(&parsed.netloc);
            let (Some(host), Some(port)) = (host, port) else {
                return Err(invalid_tcp(s));
            };
            if !parsed.path.is_empty() || !parsed.query.is_empty() || !parsed.fragment.is_empty() {
                return Err(invalid_tcp(s));
            }
            Ok(Endpoint::Tcp { host, port })
        }
        "unix" => {
            if !parsed.netloc.is_empty() {
                return Err(EndpointError(format!(
                    "invalid unix endpoint: {} (unix endpoint must use an absolute path such as 'unix:///path/to/sock')",
                    py_repr_str(s)
                )));
            }
            if !parsed.query.is_empty()
                || !parsed.fragment.is_empty()
                || parsed.path.is_empty()
                || !parsed.path.starts_with('/')
            {
                return Err(EndpointError(format!(
                    "invalid unix endpoint: {} (unix endpoint path must be absolute)",
                    py_repr_str(s)
                )));
            }
            Ok(Endpoint::Unix(parsed.path))
        }
        other => Err(EndpointError(format!(
            "unsupported endpoint scheme: {}",
            py_repr_str(other)
        ))),
    }
}

/// 是否為 loopback TCP endpoint（`cli._is_loopback_tcp`：解析失敗或非 tcp 皆為 false）。
pub fn is_loopback_tcp(s: &str) -> bool {
    match parse_endpoint(s) {
        Ok(Endpoint::Tcp { host, .. }) => LOOPBACK_TCP_HOSTS.contains(&host.as_str()),
        _ => false,
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    /// 埠號位數上限（審查 B3）：Python 3.12.3 `urlsplit(u).port` 實測，4300 位（含前導零）解析為 1，4301 位拋 `ValueError`，
    /// `_parse_endpoint` 將其包成 `invalid tcp endpoint`。
    #[test]
    fn port_digit_limit_counts_leading_zeros_like_python() {
        let at_limit = format!("tcp://127.0.0.1:{}1", "0".repeat(INT_MAX_STR_DIGITS - 1));
        assert_eq!(
            parse_endpoint(&at_limit),
            Ok(Endpoint::Tcp {
                host: "127.0.0.1".to_string(),
                port: 1,
            })
        );
        let over_limit = format!("tcp://127.0.0.1:{}1", "0".repeat(INT_MAX_STR_DIGITS));
        let err = parse_endpoint(&over_limit).expect_err("4301 位必須拒絕");
        assert!(err.0.starts_with("invalid tcp endpoint: "), "{}", err.0);
    }
}
