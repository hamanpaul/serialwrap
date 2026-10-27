//! P5-T13：per-session 帳密（`LoginPlan::EnsureReady` 使用；05 §9，對照 `sw_core/auth.py`）。
//!
//! 帳密值只存在於 `SessionAuth`，不得輸出到日誌或 RPC 回應；`AuthResolution` 只描述解析原因。

use indexmap::IndexMap;
use sw_pal::traits::SysEnv;
use sw_pal::PalPathFlavor;
use sw_proto::config_types::SessionProfile;
use sw_proto::pycompat::{py_strip, universal_newlines};
use sw_proto::pypath::{expanduser, HomeLookup, PathFlavor};
use sw_proto::runtime_paths::EnvSnapshot;

/// 已解析的帳密（05 §9 逐字；不得輸出到日誌或 RPC 回應）。
#[derive(Clone, Default)]
pub struct SessionAuth {
    pub username: Option<String>,
    pub password: Option<String>,
    pub console_unlock: Option<String>,
}

/// P7 Task 2：`username` 照印；`password`／`console_unlock` 有值時一律輸出 `Some(<redacted>)`。
impl std::fmt::Debug for SessionAuth {
    fn fmt(&self, f: &mut std::fmt::Formatter<'_>) -> std::fmt::Result {
        struct Redacted<'a>(&'a Option<String>);
        impl std::fmt::Debug for Redacted<'_> {
            fn fmt(&self, f: &mut std::fmt::Formatter<'_>) -> std::fmt::Result {
                match self.0 {
                    Some(_) => f.write_str("Some(<redacted>)"),
                    None => f.write_str("None"),
                }
            }
        }
        f.debug_struct("SessionAuth")
            .field("username", &self.username)
            .field("password", &Redacted(&self.password))
            .field("console_unlock", &Redacted(&self.console_unlock))
            .finish()
    }
}

/// 帳密解析結果的分類（`last_error` 與 detail 的來源）。
#[derive(Clone, Debug, PartialEq)]
pub struct AuthResolution {
    pub reason: &'static str,
    pub env_file_path: Option<String>,
}

/// 解析 env 檔（`export A='x'`、`B = "y"`；略過註解與無 `=` 的行；`auth.py::parse_env_file` 逐規則）。
///
/// 行切分等同 Python text-mode 迭代：先 `universal_newlines` 再依 `\n` 切（不可用 `splitlines`，
/// 後者會在 `\x1c` 等字元斷行）；空白去除一律用 Python `str.strip` 語意（`py_strip`）。
pub fn parse_env_file(text: &str) -> IndexMap<String, String> {
    let mut env = IndexMap::new();
    for raw_line in universal_newlines(text).split('\n') {
        let mut line = py_strip(raw_line);
        if line.is_empty() || line.starts_with('#') {
            continue;
        }
        if let Some(rest) = line.strip_prefix("export ") {
            line = py_strip(rest);
        }
        let Some((raw_key, raw_value)) = line.split_once('=') else {
            continue;
        };
        let key = py_strip(raw_key);
        if key.is_empty() {
            continue;
        }
        let value = py_strip(raw_value);
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

/// Python `local_env.get(key) or os.environ.get(key)`：本地值為空字串時落到 `os.environ.get`，
/// 後者照原值回傳（可為空字串；`or` 只對左值取真值）。
fn lookup(key: &str, local: &IndexMap<String, String>, env: &dyn SysEnv) -> Option<String> {
    local
        .get(key)
        .filter(|value| !value.is_empty())
        .cloned()
        .or_else(|| env.var(key))
}

fn non_empty(value: &Option<String>) -> bool {
    value.as_deref().is_some_and(|v| !v.is_empty())
}

struct SysEnvHome<'a>(&'a dyn SysEnv);

impl HomeLookup for SysEnvHome<'_> {
    fn current_user_home(&self) -> Option<String> {
        self.0.current_user_home()
    }
    fn user_home(&self, user: &str) -> Option<String> {
        self.0.user_home(user)
    }
}

/// daemon 端 `os.path.expanduser`（`SysEnv::path_flavor` 決定 posixpath／ntpath；Windows 環境變數大小寫不敏感）。
pub fn expand_user(env: &dyn SysEnv, path: &str) -> String {
    let (flavor, snapshot) = match env.path_flavor() {
        PalPathFlavor::Posix => (PathFlavor::Posix, EnvSnapshot::from_pairs(env.vars())),
        PalPathFlavor::Windows => (
            PathFlavor::Windows,
            EnvSnapshot::from_pairs_fold_upper(env.vars()),
        ),
    };
    expanduser(flavor, path, &snapshot, &SysEnvHome(env))
}

fn declared(key: &Option<String>) -> Option<&str> {
    key.as_deref().filter(|value| !value.is_empty())
}

/// 由 profile 的 `env_file`／`user_env`／`pass_env` 與 env 解析帳密（`auth.py::resolve_session_auth`）。
/// `read` 回 `None` 表示檔案不存在，`Some(Err)` 表示不可讀；WARNING 寫往 daemon stderr。
pub fn resolve_session_auth(
    p: &SessionProfile,
    env: &dyn SysEnv,
    read: &dyn Fn(&str) -> Option<std::io::Result<String>>,
) -> (SessionAuth, AuthResolution) {
    resolve_session_auth_with_warnings(p, env, read, &mut |message| {
        eprintln!("WARNING {message}");
    })
}

/// 同 [`resolve_session_auth`]，WARNING（`log.warning` 的訊息本文）交給 `warn`。
///
/// `env_file` 以 `os.path.expanduser` 展開（相對路徑以 daemon cwd 解析，不做 abspath）；`read` 收到展開後路徑。
pub fn resolve_session_auth_with_warnings(
    p: &SessionProfile,
    env: &dyn SysEnv,
    read: &dyn Fn(&str) -> Option<std::io::Result<String>>,
    warn: &mut dyn FnMut(String),
) -> (SessionAuth, AuthResolution) {
    let t = &p.template;
    let mut env_file_path = None;
    let mut env_file_state: Option<&'static str> = None;
    let mut local = IndexMap::new();
    if let Some(path) = declared(&t.env_file) {
        let expanded = expand_user(env, path);
        match read(&expanded) {
            Some(Ok(text)) => {
                local = parse_env_file(&text);
                env_file_state = Some("read");
            }
            Some(Err(_)) => {
                env_file_state = Some("unreadable");
                warn(format!("無法解析 env_file: {expanded}（session {}）", p.com));
            }
            None => {
                env_file_state = Some("missing");
                warn(format!("env_file 不存在: {expanded}（session {}）", p.com));
            }
        }
        env_file_path = Some(expanded);
    }

    let mut username = declared(&t.user_env).and_then(|key| lookup(key, &local, env));
    if !non_empty(&username) && non_empty(&t.username) {
        username = t.username.clone();
    }
    let password = declared(&t.pass_env).and_then(|key| lookup(key, &local, env));
    let console_unlock = declared(&t.console_unlock_env).and_then(|key| lookup(key, &local, env));

    let is_declared = declared(&t.user_env).is_some()
        || declared(&t.pass_env).is_some()
        || declared(&t.env_file).is_some()
        || declared(&t.console_unlock_env).is_some();
    let resolved = if declared(&t.console_unlock_env).is_some() {
        non_empty(&console_unlock)
    } else {
        non_empty(&username) && non_empty(&password)
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
