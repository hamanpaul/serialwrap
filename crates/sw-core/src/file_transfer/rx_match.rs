//! RX 判定 helper（逐字承接 `sw_core/file_transfer.py`）。
//!
//! 所有函式只看傳入的文字，不假設 RX 視窗大小（OD-80：pull 不依賴 131072 字元視窗）。

use super::templates::MD5_SENTINEL;
use sw_proto::pycompat::{py_is_space, strip_ansi};
use sw_proto::pyre::{compile, PyFlags};

/// 移除目前命令的本地 echo：`text` 以非空 `command` 開頭 → 去掉，再去一個 `\r\n` 或 `\n`。
pub fn strip_command_echo<'a>(text: &'a str, command: Option<&str>) -> &'a str {
    match command {
        Some(c) if !c.is_empty() && text.starts_with(c) => {
            let rest = &text[c.len()..];
            rest.strip_prefix("\r\n").or_else(|| rest.strip_prefix('\n')).unwrap_or(rest)
        }
        _ => text,
    }
}

/// sentinel 之後須為結尾、`\n` 或 `\r\n`。
fn suffix_ok(rest: &str) -> bool {
    rest.is_empty() || rest.starts_with('\n') || rest.starts_with("\r\n")
}

/// 去 echo 後，sentinel 須獨立成行：`(?:^|\r?\n)ESC(?:\r?\n|$)`。
pub fn runtime_sentinel_seen(text: &str, sentinel: &str, command: Option<&str>) -> bool {
    let output = strip_command_echo(text, command);
    output
        .match_indices(sentinel)
        .any(|(p, _)| (p == 0 || output.as_bytes()[p - 1] == b'\n') && suffix_ok(&output[p + sentinel.len()..]))
}

/// 失敗 sentinel：只要求後綴條件（可黏在 partial 輸出之後）。
pub fn failure_sentinel_seen(text: &str, sentinel: &str, command: Option<&str>) -> bool {
    let output = strip_command_echo(text, command);
    output.match_indices(sentinel).any(|(p, _)| suffix_ok(&output[p + sentinel.len()..]))
}

/// shell 語法／環境錯誤（保留 Python `(?i)` 的 Unicode 大小寫摺疊，故走 `pyre`）。
pub fn probe_shell_error(text: &str, command: &str) -> bool {
    let output = strip_command_echo(text, Some(command));
    let re = compile(r"(?i)(?:syntax error|bad substitution|unterminated|permission denied)", PyFlags(0))
        .expect("固定 pattern 必可編譯");
    re.search(output).is_some()
}

/// 取去 echo 後最後一組 BEGIN/END 之間的內容，去 ANSI 並刪除所有 Python `\s` 字元。
pub fn extract_between_sentinels(text: &str, command: Option<&str>) -> Option<String> {
    use super::templates::{SENTINEL_BEGIN, SENTINEL_END};
    let output = strip_command_echo(text, command);
    let begin = output.rfind(SENTINEL_BEGIN)?;
    let end = output.rfind(SENTINEL_END)?;
    if end <= begin {
        return None;
    }
    let from = begin + SENTINEL_BEGIN.len();
    // Python 切片在 from > end（END 與 BEGIN 尾端重疊）時得空字串。
    let content = if from <= end { &output[from..end] } else { "" };
    Some(strip_ansi(content).chars().filter(|c| !py_is_space(*c)).collect())
}

/// `\s*(?:\\)?([0-9a-fA-F]{32})(?:\s|$)`（`re.match`）；回傳小寫 digest。
fn md5_from_line(line: &str) -> Option<String> {
    let rest = line.trim_start_matches(py_is_space);
    let rest = rest.strip_prefix('\\').unwrap_or(rest);
    let digest = rest.get(..32)?;
    if !digest.bytes().all(|b| b.is_ascii_hexdigit()) {
        return None;
    }
    match rest[32..].chars().next() {
        None => {}
        Some(c) if py_is_space(c) => {}
        Some(_) => return None,
    }
    Some(digest.to_ascii_lowercase())
}

/// `_remote_md5` 的解析段：須見 `MD5_SENTINEL`，在其之前的文字逐行取第一個 md5sum 欄位。
pub fn remote_md5_from_output(raw: &str, command: &str) -> Option<String> {
    let output = strip_command_echo(raw, Some(command));
    if !runtime_sentinel_seen(raw, MD5_SENTINEL, Some(command)) {
        return None;
    }
    let before = &output[..output.find(MD5_SENTINEL)?];
    sw_proto::pycompat::splitlines(before).into_iter().find_map(md5_from_line)
}
