//! 後端選擇（`platform_backends._select`）。

use crate::BackendKind;

/// `raw` 為 `SERIALWRAP_RPC_BACKEND`（Q-17）；錯誤字串為 `"unsupported backend: 'x'"`。
pub fn select_backend(raw: Option<&str>, native: BackendKind) -> Result<BackendKind, String> {
    let mode = raw
        .filter(|value| !value.is_empty())
        .unwrap_or("auto")
        .to_ascii_lowercase();
    match mode.as_str() {
        "auto" => Ok(native),
        "posix" | "unix" | "termios" => Ok(BackendKind::Posix),
        "win" | "windows" | "win32" => Ok(BackendKind::Win),
        _ => Err(format!("unsupported backend: {}", python_repr(&mode))),
    }
}

fn python_repr(value: &str) -> String {
    let quote = if !value.contains('\'') {
        '\''
    } else if !value.contains('"') {
        '"'
    } else {
        '\''
    };
    let mut result = String::with_capacity(value.len() + 2);
    result.push(quote);
    for character in value.chars() {
        match character {
            '\\' => result.push_str("\\\\"),
            '\n' => result.push_str("\\n"),
            '\r' => result.push_str("\\r"),
            '\t' => result.push_str("\\t"),
            value if value == quote => {
                result.push('\\');
                result.push(value);
            }
            value => result.push(value),
        }
    }
    result.push(quote);
    result
}
