//! P5-T13：per-session 帳密（`LoginPlan::EnsureReady` 使用；05 §9，對照 `sw_core/auth.py`）。
//!
//! 帳密值只存在於 `SessionAuth`，不得輸出到日誌或 RPC 回應；`AuthResolution` 只描述解析原因。

use indexmap::IndexMap;
use sw_pal::traits::SysEnv;
use sw_proto::config_types::SessionProfile;

/// 已解析的帳密（05 §9 逐字；不得輸出到日誌或 RPC 回應）。
#[derive(Clone, Default)]
pub struct SessionAuth {
    pub username: Option<String>,
    pub password: Option<String>,
    pub console_unlock: Option<String>,
}

/// 帳密解析結果的分類（`last_error` 與 detail 的來源）。
#[derive(Clone, Debug, PartialEq)]
pub struct AuthResolution {
    pub reason: &'static str,
    pub env_file_path: Option<String>,
}

/// 解析 env 檔（`export A='x'`、`B = "y"`；略過註解與無 `=` 的行；`auth.py::parse_env_file` 逐規則）。
pub fn parse_env_file(text: &str) -> IndexMap<String, String> {
    let mut env = IndexMap::new();
    for raw_line in text.lines() {
        let mut line = raw_line.trim();
        if line.is_empty() || line.starts_with('#') {
            continue;
        }
        if let Some(rest) = line.strip_prefix("export ") {
            line = rest.trim();
        }
        let Some((raw_key, raw_value)) = line.split_once('=') else {
            continue;
        };
        let key = raw_key.trim();
        if key.is_empty() {
            continue;
        }
        let value = raw_value.trim();
        let chars: Vec<char> = value.chars().collect();
        let value = if chars.len() >= 2
            && chars[0] == chars[chars.len() - 1]
            && (chars[0] == '\'' || chars[0] == '"')
        {
            chars[1..chars.len() - 1].iter().collect::<String>()
        } else {
            value.to_string()
        };
        env.insert(key.to_string(), value);
    }
    env
}

/// Python `local_env.get(key) or os.environ.get(key)`：空字串視為缺值。
fn lookup(key: &str, local: &IndexMap<String, String>, env: &dyn SysEnv) -> Option<String> {
    local
        .get(key)
        .filter(|value| !value.is_empty())
        .cloned()
        .or_else(|| env.var(key).filter(|value| !value.is_empty()))
}

fn declared(key: &Option<String>) -> Option<&str> {
    key.as_deref().filter(|value| !value.is_empty())
}

/// 由 profile 的 `env_file`／`user_env`／`pass_env` 與 env 解析帳密（`auth.py::resolve_session_auth`）。
/// `read` 回 `None` 表示檔案不存在，`Some(Err)` 表示不可讀。
pub fn resolve_session_auth(
    p: &SessionProfile,
    env: &dyn SysEnv,
    read: &dyn Fn(&str) -> Option<std::io::Result<String>>,
) -> (SessionAuth, AuthResolution) {
    let t = &p.template;
    let mut env_file_path = None;
    let mut env_file_state: Option<&'static str> = None;
    let mut local = IndexMap::new();
    if let Some(path) = declared(&t.env_file) {
        env_file_path = Some(path.to_string());
        match read(path) {
            Some(Ok(text)) => {
                local = parse_env_file(&text);
                env_file_state = Some("read");
            }
            Some(Err(_)) => env_file_state = Some("unreadable"),
            None => env_file_state = Some("missing"),
        }
    }

    let mut username = declared(&t.user_env).and_then(|key| lookup(key, &local, env));
    if username.is_none() {
        username = t.username.clone().filter(|value| !value.is_empty());
    }
    let password = declared(&t.pass_env).and_then(|key| lookup(key, &local, env));
    let console_unlock = declared(&t.console_unlock_env).and_then(|key| lookup(key, &local, env));

    let is_declared = declared(&t.user_env).is_some()
        || declared(&t.pass_env).is_some()
        || declared(&t.env_file).is_some()
        || declared(&t.console_unlock_env).is_some();
    let resolved = if declared(&t.console_unlock_env).is_some() {
        console_unlock.is_some()
    } else {
        username.is_some() && password.is_some()
    };
    let reason = if !is_declared {
        "not_configured"
    } else if resolved {
        "ok"
    } else {
        match env_file_state {
            Some("missing") => "env_file_missing",
            Some("unreadable") => "env_file_unreadable",
            _ => "key_absent",
        }
    };
    (
        SessionAuth {
            username,
            password,
            console_unlock,
        },
        AuthResolution {
            reason,
            env_file_path,
        },
    )
}

/// profile 宣告了需要帳密、但解析未完成（→ `CREDENTIALS_UNRESOLVED` 終態；`_credentials_declared_but_unresolved`）。
pub fn credentials_declared_but_unresolved(p: &SessionProfile, r: &AuthResolution) -> bool {
    let t = &p.template;
    let is_declared = declared(&t.user_env).is_some()
        || declared(&t.pass_env).is_some()
        || declared(&t.env_file).is_some()
        || declared(&t.console_unlock_env).is_some();
    is_declared && r.reason != "ok"
}
