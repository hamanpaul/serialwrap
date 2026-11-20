//! 檔案傳輸板端命令樣板（逐字承接 `sw_core/file_transfer.py`）。

pub const SENTINEL_BEGIN: &str = "===SW_XFER_BEGIN===";
pub const SENTINEL_END: &str = "===SW_XFER_END===";
pub const TOOL_PROBE_PAYLOAD: &str = "c3ctcHJvYmU=";
pub const DECODER_B64_SENTINEL: &str = "SW_XFER_DECODER_B64_42";
pub const DECODER_OPENSSL_SENTINEL: &str = "SW_XFER_DECODER_OPENSSL_42";
pub const ENCODER_B64_SENTINEL: &str = "SW_XFER_ENCODER_B64_42";
pub const ENCODER_OPENSSL_SENTINEL: &str = "SW_XFER_ENCODER_OPENSSL_42";
pub const CHUNK_SENTINEL: &str = "SW_XFER_CHUNK_42";
pub const MD5_SENTINEL: &str = "SW_XFER_MD5_42";
pub const MV_SENTINEL: &str = "SW_XFER_MV_42";
pub const CLEANUP_SENTINEL: &str = "SW_XFER_CLEANUP_42";
pub const ENCODER_FAILURE_SENTINEL: &str = "SW_XFER_ENCODER_FAILED_42";
pub const DECODERS: [&str; 2] = ["base64", "openssl"];
pub const ENCODERS: [&str; 2] = ["base64", "openssl"];
pub const ACK_MODES: [&str; 3] = ["auto", "echo", "none"];
pub const DEFAULT_CHUNK_SIZE: i128 = 512;
pub const DEFAULT_ECHO_SLICE_SIZE: usize = 64;
pub const DEFAULT_ECHO_TIMEOUT_S: f64 = 5.0;

#[derive(Debug, PartialEq, Eq)]
pub enum TemplateError {
    UnsupportedMarker(String),
    UnknownTool(String),
}

use md5::{Digest, Md5};
use sw_proto::pycompat::{b2a_base64, shlex_quote};

/// `printf '%s\n' '<prefix>'$((6*7))`：只在 target 執行後才出現完整值的 sentinel。
pub fn runtime_sentinel(prefix: &str) -> String {
    format!("printf '%s\\n' '{prefix}'$((6*7))")
}

/// 以 quote-split 生成 marker，避免完整 marker 出現在 command echo。
pub fn marker_expression(marker: &str) -> Result<String, TemplateError> {
    const HEAD: &str = "===SW_XFER_";
    const TAIL: &str = "===";
    if !(marker.starts_with(HEAD) && marker.ends_with(TAIL)) {
        return Err(TemplateError::UnsupportedMarker(marker.to_string()));
    }
    // Python 以 `marker[len(HEAD):-3]` 切片；兩端重疊（如 "===SW_XFER_==="之外的短字串）時為空字串。
    let start = HEAD.len();
    let end = marker.len().saturating_sub(TAIL.len());
    let middle = if start <= end { &marker[start..end] } else { "" };
    Ok(format!("'===SW_XFER_'{middle}'==='"))
}

pub fn decoder_probe_command() -> String {
    format!(
        "if test \"$(printf '%s' '{TOOL_PROBE_PAYLOAD}' | base64 -d 2>/dev/null)\" = 'sw-probe'; \
         then {}; \
         elif test \"$(printf '%s' '{TOOL_PROBE_PAYLOAD}' | openssl enc -base64 -d -A 2>/dev/null)\" = 'sw-probe'; \
         then {}; fi",
        runtime_sentinel("SW_XFER_DECODER_B64_"),
        runtime_sentinel("SW_XFER_DECODER_OPENSSL_"),
    )
}

pub fn encoder_probe_command() -> String {
    format!(
        "if test \"$(printf '%s' 'sw-probe' | base64 2>/dev/null)\" = 'c3ctcHJvYmU='; \
         then {}; \
         elif test \"$(printf '%s' 'sw-probe' | openssl enc -base64 2>/dev/null)\" = 'c3ctcHJvYmU='; \
         then {}; fi",
        runtime_sentinel("SW_XFER_ENCODER_B64_"),
        runtime_sentinel("SW_XFER_ENCODER_OPENSSL_"),
    )
}

pub fn decoder_command_name(decoder: &str) -> Result<&'static str, TemplateError> {
    match decoder {
        "base64" => Ok("base64 -d"),
        "openssl" => Ok("openssl enc -base64 -d -A"),
        other => Err(TemplateError::UnknownTool(other.to_string())),
    }
}

pub fn encoder_command_name(encoder: &str) -> Result<&'static str, TemplateError> {
    match encoder {
        "base64" => Ok("base64"),
        // 保留 OpenSSL 的換行，避免大型 pull 輸出變成單一超長 console line。
        "openssl" => Ok("openssl enc -base64"),
        other => Err(TemplateError::UnknownTool(other.to_string())),
    }
}

pub fn chunk_command(encoded: &str, decoder: &str, op: &str, path: &str) -> Result<String, TemplateError> {
    Ok(format!(
        "printf '%s' {} | {} {op} {} && {}",
        shlex_quote(encoded),
        decoder_command_name(decoder)?,
        shlex_quote(path),
        runtime_sentinel("SW_XFER_CHUNK_"),
    ))
}

pub fn md5_command(path: &str) -> String {
    format!("md5sum {} && {}", shlex_quote(path), runtime_sentinel("SW_XFER_MD5_"))
}

pub fn mv_command(source_path: &str, remote_path: &str) -> String {
    format!(
        "mv {} {} && {}",
        shlex_quote(source_path),
        shlex_quote(remote_path),
        runtime_sentinel("SW_XFER_MV_")
    )
}

pub fn cleanup_command(path: &str) -> String {
    format!("rm -f {} && {}", shlex_quote(path), runtime_sentinel("SW_XFER_CLEANUP_"))
}

pub fn pull_command(remote_path: &str, encoder: &str) -> Result<String, TemplateError> {
    // Python 先求值 begin marker（恆成立），再求值 encoder 名稱（未知 → ValueError）。
    let begin = marker_expression(SENTINEL_BEGIN)?;
    let end = marker_expression(SENTINEL_END)?;
    Ok(format!(
        "printf '%s\\n' {begin} && {} < {} && printf '%s\\n' {end} || {}",
        encoder_command_name(encoder)?,
        shlex_quote(remote_path),
        runtime_sentinel("SW_XFER_ENCODER_FAILED_"),
    ))
}

/// 切成 `chunk_size` 大小的分段；空資料回傳一段空 bytes。`chunk_size` 必須 > 0（呼叫端先驗證）。
pub fn split_chunks(data: &[u8], chunk_size: usize) -> Vec<&[u8]> {
    if data.is_empty() {
        return vec![data];
    }
    data.chunks(chunk_size).collect()
}

/// 任一命令的 UTF-8 bytes 長度超過 budget 即 true；`None` 表示不限。
pub fn line_budget_exceeded(commands: &[&str], budget: Option<i128>) -> bool {
    match budget {
        None => false,
        Some(b) => commands.iter().any(|c| (c.len() as i128) > b),
    }
}

/// `base64.b64encode(data).decode()`（無換行）。
pub fn b64encode(data: &[u8]) -> String {
    let mut s = b2a_base64(data);
    if s.ends_with('\n') {
        s.pop();
    }
    s
}

/// `hashlib.md5(data).hexdigest()`。
pub fn md5_hex(data: &[u8]) -> String {
    let digest = Md5::digest(data);
    digest.iter().map(|b| format!("{b:02x}")).collect()
}
