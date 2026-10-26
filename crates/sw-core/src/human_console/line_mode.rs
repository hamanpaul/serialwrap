//! human console line 命令的模式判定（P6 Task 3；`service.py` `_human_console_mode`）。

use sw_pal::PalPathFlavor;
use sw_proto::constants::{HUMAN_INTERACTIVE_COMMANDS, SUDO_VALUE_OPTS};
use sw_proto::pycompat::shlex_split;
use sw_proto::pypath::{self, PathFlavor};

/// `line`：一般命令；`interactive`：全螢幕互動程式（開 human lease）。
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum HumanLineMode {
    Line,
    Interactive,
}

impl HumanLineMode {
    /// `"line"` ｜ `"interactive"`。
    pub fn as_str(self) -> &'static str {
        match self {
            HumanLineMode::Line => "line",
            HumanLineMode::Interactive => "interactive",
        }
    }
}

/// Python `str.isspace()` 的字元集合（`str.split()`／`str.strip()` 使用）：Rust `char::is_whitespace` 另缺 U+001C～U+001F。
fn py_space(c: char) -> bool {
    c.is_whitespace() || ('\u{1c}'..='\u{1f}').contains(&c)
}

/// `re.match(r"^[A-Za-z_][A-Za-z0-9_]*=.*$", s)`：`.` 不配 `\n`，`$` 可配在結尾單一 `\n` 之前。
fn is_env_assignment(s: &str) -> bool {
    let b = s.as_bytes();
    if b.is_empty() || !(b[0].is_ascii_alphabetic() || b[0] == b'_') {
        return false;
    }
    let mut i = 1;
    while i < b.len() && (b[i].is_ascii_alphanumeric() || b[i] == b'_') {
        i += 1;
    }
    if i >= b.len() || b[i] != b'=' {
        return false;
    }
    let rest = &s[i + 1..];
    match rest.find('\n') {
        None => true,
        Some(pos) => pos == rest.len() - 1,
    }
}

/// `_human_console_mode`：shlex 分詞（失敗退回 `strip().split()`）後略過 env 賦值、`--`、`sudo`／`env`／`command`／`builtin`／`exec`
/// 前綴，第一個實際命令的 basename 在 `HUMAN_INTERACTIVE_COMMANDS` 內即 `Interactive`。
///
/// basename 為 Python 的 `os.path.basename`，依 PAL 路徑風格 `flavor`（`Pal.caps.path_flavor`）取 `posixpath`／`ntpath`
/// 語意：Windows 另以 `\` 與 drive prefix 分隔（`'C:\bin\vim'`、`'C:vim'` → `vim`；sol 補審 P6 #2）。
pub fn human_console_mode(command: &str, flavor: PalPathFlavor) -> HumanLineMode {
    let flavor = match flavor {
        PalPathFlavor::Posix => PathFlavor::Posix,
        PalPathFlavor::Windows => PathFlavor::Windows,
    };
    let tokens: Vec<String> = match shlex_split(command) {
        Ok(t) => t,
        Err(_) => command.trim_matches(py_space).split(py_space).filter(|t| !t.is_empty()).map(str::to_string).collect(),
    };
    let mut idx = 0;
    while idx < tokens.len() {
        let token = tokens[idx].trim_matches(py_space);
        if token.is_empty() || token == "--" || is_env_assignment(token) {
            idx += 1;
            continue;
        }
        let base = pypath::basename(flavor, token);
        let base = base.as_str();
        if base == "sudo" {
            idx += 1;
            while idx < tokens.len() {
                let opt = tokens[idx].as_str();
                if opt == "--" {
                    idx += 1;
                    break;
                }
                if SUDO_VALUE_OPTS.contains(&opt) {
                    idx += 2;
                    continue;
                }
                if opt.starts_with('-') {
                    idx += 1;
                    continue;
                }
                break;
            }
            continue;
        }
        if base == "env" {
            idx += 1;
            while idx < tokens.len() {
                let opt = tokens[idx].as_str();
                if opt == "--" {
                    idx += 1;
                    break;
                }
                if opt.starts_with('-') || is_env_assignment(opt) {
                    idx += 1;
                    continue;
                }
                break;
            }
            continue;
        }
        if base == "command" || base == "builtin" || base == "exec" {
            idx += 1;
            continue;
        }
        return if HUMAN_INTERACTIVE_COMMANDS.contains(&base) { HumanLineMode::Interactive } else { HumanLineMode::Line };
    }
    HumanLineMode::Line
}
