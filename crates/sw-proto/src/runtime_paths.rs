//! 執行期路徑與環境變數快照（P3-T4／P3-T7）。
//!
//! 路徑計算逐條承接 `sw_core/constants.py` 模組層邏輯（07 §0、01 §4.1），以 `contracts/golden/runtime_paths.*.tsv` 比對。
use crate::pycompat::{py_int_parse, py_strip};
use crate::pyjson::PyInt;
use crate::pypath::{self, HomeLookup, PathFlavor};
use std::collections::BTreeMap;

/// 凍結的環境變數快照。
///
/// 鍵大小寫由建構方式決定（sw-proto 不依平台分支，平台語意由呼叫端選擇）：
/// - [`EnvSnapshot::from_pairs`]：精確比對（POSIX `os.environ`）。
/// - [`EnvSnapshot::from_pairs_fold_upper`]：鍵轉大寫存放、查詢亦轉大寫（Windows `os.environ`，大小寫不敏感）。
#[derive(Clone, Debug, Default, PartialEq, Eq)]
pub struct EnvSnapshot {
    vars: BTreeMap<String, String>,
    fold_upper: bool,
}

impl EnvSnapshot {
    /// 由鍵值對建立快照（鍵大小寫敏感）；重複鍵以後者為準（與 `dict(pairs)` 相同）。
    pub fn from_pairs<I: IntoIterator<Item = (String, String)>>(it: I) -> Self {
        EnvSnapshot {
            vars: it.into_iter().collect(),
            fold_upper: false,
        }
    }

    /// 由鍵值對建立快照（Windows `os.environ` 語意）：鍵轉為大寫存放，[`EnvSnapshot::get`] 查詢時亦轉為大寫。
    /// Windows 平台的 `SysEnv` 應以此建構，否則 `serialwrap_*` 小寫設定會查不到。
    pub fn from_pairs_fold_upper<I: IntoIterator<Item = (String, String)>>(it: I) -> Self {
        EnvSnapshot {
            vars: it.into_iter().map(|(k, v)| (k.to_uppercase(), v)).collect(),
            fold_upper: true,
        }
    }

    /// 取得環境變數；未設定回傳 `None`（空字串視為已設定）。
    pub fn get(&self, k: &str) -> Option<&str> {
        if self.fold_upper {
            self.vars.get(&k.to_uppercase()).map(String::as_str)
        } else {
            self.vars.get(k).map(String::as_str)
        }
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn exact_snapshot_is_case_sensitive_and_last_duplicate_wins() {
        let env = EnvSnapshot::from_pairs([
            ("a".to_string(), "1".to_string()),
            ("A".to_string(), "x".to_string()),
            ("a".to_string(), "2".to_string()),
            ("EMPTY".to_string(), String::new()),
        ]);
        assert_eq!(env.get("a"), Some("2"));
        assert_eq!(env.get("A"), Some("x"));
        assert_eq!(env.get("a2"), None);
        // 空字串視為已設定（Python `'K' in os.environ` 為真）。
        assert_eq!(env.get("EMPTY"), Some(""));
    }

    /// Windows `os.environ`：鍵大小寫不敏感（`py.exe` 實測：設 `mixedCase_x`，以 `MIXEDCASE_X` 與 `mixedcase_x` 皆可取得）。
    #[test]
    fn fold_upper_snapshot_is_case_insensitive() {
        let env =
            EnvSnapshot::from_pairs_fold_upper([("mixedCase_x".to_string(), "1".to_string())]);
        assert_eq!(env.get("MIXEDCASE_X"), Some("1"));
        assert_eq!(env.get("mixedcase_x"), Some("1"));
        assert_eq!(env.get("other"), None);
    }

    /// 旗標相依：Windows 無 `USERPROFILE`／`HOMEPATH` 時，`~` 必須原樣保留（`ntpath.expanduser` 行為）。
    /// 路徑解析不得以 `HomeLookup` 補值（P3-T7 審查 B2）；期望值為 Python 3.12.3 以 `ntpath` 模擬實測（`gen_runtime_paths.py` 的 Windows probe，env 不含家目錄鍵）。
    #[test]
    fn windows_without_home_env_keeps_tilde_like_python() {
        struct AnyHome;
        impl HomeLookup for AnyHome {
            fn current_user_home(&self) -> Option<String> {
                Some(r"C:\Users\golden".to_string())
            }
            fn user_home(&self, _: &str) -> Option<String> {
                None
            }
        }
        let native = NativeTraits {
            default_endpoint_is_tcp: true,
            path_flavor: PathFlavor::Windows,
        };
        let env = EnvSnapshot::from_pairs_fold_upper([("PATH".to_string(), "x".to_string())]);
        let p = RuntimePaths::resolve(&env, native, &AnyHome).expect("有效環境");
        assert_eq!(p.config_dir, r"~\.config\serialwrap");
        assert_eq!(p.config_yaml, r"~\.config\serialwrap\config.yaml");
        assert_eq!(p.log_dir, r"~\b-log");
        assert_eq!(p.default_endpoint, "tcp://127.0.0.1:48700");

        // 明確設定的 `~` 路徑同樣原樣保留（不展開）。
        let env = EnvSnapshot::from_pairs_fold_upper([(
            "SERIALWRAP_CONFIG_DIR".to_string(),
            "~/custom".to_string(),
        )]);
        let p = RuntimePaths::resolve(&env, native, &AnyHome).expect("有效環境");
        assert_eq!(p.config_dir, "~/custom");
        assert_eq!(p.config_yaml, r"~/custom\config.yaml");
    }
}

/// 平台靜態特性（由呼叫端注入，`sw-proto` 不依平台分支）。
#[derive(Clone, Copy, Debug)]
pub struct NativeTraits {
    /// 預設 endpoint 是否為 TCP loopback（Windows 為真，POSIX 為假）。
    pub default_endpoint_is_tcp: bool,
    /// 路徑風格（POSIX 或 Windows）。
    pub path_flavor: crate::pypath::PathFlavor,
}

/// 執行期路徑集合（對應 `sw_core/constants.py` 的模組層常數，逐欄以 golden 比對）。
#[derive(Clone, Debug, PartialEq)]
pub struct RuntimePaths {
    pub config_dir: String,
    pub state_dir: String,
    pub run_dir: String,
    pub data_dir: String,
    pub lock_path: String,
    pub socket_path: String,
    /// `int(os.environ.get("SERIALWRAP_TCP_PORT", "48700"))`。
    pub default_tcp_port: PyInt,
    pub default_endpoint: String,
    pub profile_dir: String,
    pub wal_dir: String,
    pub log_dir: String,
    /// `os.path.join(CONFIG_DIR, "config.yaml")`。
    pub config_yaml: String,
}

/// 路徑解析錯誤。
#[derive(Clone, Debug, PartialEq)]
pub enum PathsError {
    /// `SERIALWRAP_TCP_PORT` 無法以 Python `int()` 解析（Python 於 import 時拋 `ValueError`；Q-15）。
    InvalidTcpPort { raw: String },
}

/// `PyInt` 的十進位字串（對應 Python `str(int)` 與 f-string 插值）。
fn py_int_to_string(v: &PyInt) -> String {
    match v {
        PyInt::Small(n) => n.to_string(),
        PyInt::Big(s) => s.clone(),
    }
}

/// `_env_path(name, default)`：未設或 strip 後為空即採預設值；採用值不 strip，但一律 `expanduser`。
fn env_path(
    env: &EnvSnapshot,
    f: PathFlavor,
    home: &dyn HomeLookup,
    name: &str,
    default: &str,
) -> String {
    let raw = match env.get(name) {
        Some(v) if !py_strip(v).is_empty() => v.to_string(),
        _ => default.to_string(),
    };
    pypath::expanduser(f, &raw, env, home)
}

/// `_xdg(name, fallback_rel)`：`os.environ.get(name) or os.path.join(expanduser("~"), fallback_rel)`；空字串視為未設且不 expanduser。
fn xdg(
    env: &EnvSnapshot,
    f: PathFlavor,
    home: &dyn HomeLookup,
    name: &str,
    fallback_rel: &str,
) -> String {
    match env.get(name) {
        Some(v) if !v.is_empty() => v.to_string(),
        _ => {
            let user_home = pypath::expanduser(f, "~", env, home);
            pypath::join(f, &user_home, &[fallback_rel])
        }
    }
}

impl RuntimePaths {
    /// 逐條承接 `sw_core/constants.py` 模組層路徑計算（07 §0、01 §4.1）。
    ///
    /// `RUN_DIR` 三段 fallback：`SERIALWRAP_STATE_DIR` strip 後非空 → `STATE_DIR`；否則 `XDG_RUNTIME_DIR`（非空）→ `<它>/serialwrap`；否則 `STATE_DIR/run`。
    /// `DEFAULT_ENDPOINT`：`SERIALWRAP_ENDPOINT` 非空即採用；否則 TCP 平台為 `tcp://127.0.0.1:<port>`、POSIX 為 `SOCKET_PATH`。
    pub fn resolve(
        env: &EnvSnapshot,
        native: NativeTraits,
        home: &dyn HomeLookup,
    ) -> Result<RuntimePaths, PathsError> {
        let f = native.path_flavor;

        let config_home = xdg(env, f, home, "XDG_CONFIG_HOME", ".config");
        let state_home = xdg(env, f, home, "XDG_STATE_HOME", ".local/state");
        let data_home = xdg(env, f, home, "XDG_DATA_HOME", ".local/share");
        let runtime_home = env.get("XDG_RUNTIME_DIR").filter(|v| !v.is_empty());

        let config_dir = env_path(
            env,
            f,
            home,
            "SERIALWRAP_CONFIG_DIR",
            &pypath::join(f, &config_home, &["serialwrap"]),
        );
        let state_dir = env_path(
            env,
            f,
            home,
            "SERIALWRAP_STATE_DIR",
            &pypath::join(f, &state_home, &["serialwrap"]),
        );

        // constants.py `_run_dir_default()`：注意判定的是原始 env（strip 後非空），而非展開後的 STATE_DIR。
        let state_dir_set = env
            .get("SERIALWRAP_STATE_DIR")
            .is_some_and(|v| !py_strip(v).is_empty());
        let run_default = if state_dir_set {
            state_dir.clone()
        } else if let Some(rh) = runtime_home {
            pypath::join(f, rh, &["serialwrap"])
        } else {
            pypath::join(f, &state_dir, &["run"])
        };
        let run_dir = env_path(env, f, home, "SERIALWRAP_RUN_DIR", &run_default);

        let data_dir = env_path(
            env,
            f,
            home,
            "SERIALWRAP_DATA_DIR",
            &pypath::join(f, &data_home, &["serialwrap"]),
        );
        let lock_path = pypath::join(f, &run_dir, &["serialwrapd.lock"]);
        let socket_path = pypath::join(f, &run_dir, &["serialwrapd.sock"]);

        // `int(os.environ.get(...))`：鍵存在即採值（含空字串，空字串會 ValueError）。
        let raw_port = env
            .get("SERIALWRAP_TCP_PORT")
            .unwrap_or(crate::constants::DEFAULT_TCP_PORT_RAW_DEFAULT);
        let default_tcp_port =
            py_int_parse(raw_port).ok_or_else(|| PathsError::InvalidTcpPort {
                raw: raw_port.to_string(),
            })?;
        let port_str = py_int_to_string(&default_tcp_port);

        let default_endpoint = match env.get("SERIALWRAP_ENDPOINT").filter(|v| !v.is_empty()) {
            Some(v) => v.to_string(),
            None if native.default_endpoint_is_tcp => format!("tcp://127.0.0.1:{port_str}"),
            None => socket_path.clone(),
        };

        let profile_dir = env_path(
            env,
            f,
            home,
            "SERIALWRAP_PROFILE_DIR",
            &pypath::join(f, &config_dir, &["profiles"]),
        );
        let wal_dir = env_path(
            env,
            f,
            home,
            "SERIALWRAP_WAL_DIR",
            &pypath::join(f, &state_dir, &["wal"]),
        );
        let user_home = pypath::expanduser(f, "~", env, home);
        let log_dir = env_path(
            env,
            f,
            home,
            "SERIALWRAP_LOG_DIR",
            &pypath::join(f, &user_home, &["b-log"]),
        );
        let config_yaml = pypath::join(f, &config_dir, &["config.yaml"]);

        Ok(RuntimePaths {
            config_dir,
            state_dir,
            run_dir,
            data_dir,
            lock_path,
            socket_path,
            default_tcp_port,
            default_endpoint,
            profile_dir,
            wal_dir,
            log_dir,
            config_yaml,
        })
    }
}

/// `sw_core/cli.py::_bench_state_path()`（cli.py:1115-1122）：呼叫時讀 env。
///
/// `SERIALWRAP_STATE_DIR` strip 後非空才採用（原值再 `expanduser`）；否則 `XDG_STATE_HOME`（falsy 時 `~/.local/state`）＋`serialwrap`。
pub fn bench_state_path(env: &EnvSnapshot, f: PathFlavor, home: &dyn HomeLookup) -> String {
    let state_dir = match env.get("SERIALWRAP_STATE_DIR") {
        Some(v) if !py_strip(v).is_empty() => v.to_string(),
        _ => {
            let state_home = match env.get("XDG_STATE_HOME").filter(|v| !v.is_empty()) {
                Some(v) => v.to_string(),
                None => {
                    let user_home = pypath::expanduser(f, "~", env, home);
                    pypath::join(f, &user_home, &[".local", "state"])
                }
            };
            pypath::join(f, &state_home, &["serialwrap"])
        }
    };
    let expanded = pypath::expanduser(f, &state_dir, env, home);
    pypath::join(f, &expanded, &["benches.state.json"])
}

/// `bench_registry._default_benches_path()`：`SERIALWRAP_BENCHES_FILE`（strip 後非空才採用，原值再 `expanduser`）；
/// 否則 `<config_dir>/benches.yaml`。`config_dir` 傳入 `RuntimePaths::config_dir`（與 `_default_config_dir()` 同源）。
pub fn benches_yaml_path(
    env: &EnvSnapshot,
    f: PathFlavor,
    home: &dyn HomeLookup,
    config_dir: &str,
) -> String {
    let raw = match env.get("SERIALWRAP_BENCHES_FILE") {
        Some(v) if !py_strip(v).is_empty() => v.to_string(),
        _ => pypath::join(f, config_dir, &["benches.yaml"]),
    };
    pypath::expanduser(f, &raw, env, home)
}

/// `resolve_config_path()`（SC-09，逐字對齊 P1 `constants.resolve_config_path(for_write=False)`，02 T15）。
///
/// 1. `$SERIALWRAP_CONFIG` strip 後非空 → `expanduser` 後原樣回傳（即使檔案不存在）；
/// 2. `CONFIG_DIR/config.yaml` 存在 → 它；
/// 3. `<system_config_dir>/config.yaml` 存在 → 它；
/// 4. 否則 `CONFIG_DIR/config.yaml`。
pub fn resolve_config_path(
    env: &EnvSnapshot,
    paths: &RuntimePaths,
    system_config_dir: &str,
    f: PathFlavor,
    home: &dyn HomeLookup,
    exists: &dyn Fn(&str) -> bool,
) -> String {
    if let Some(v) = env.get("SERIALWRAP_CONFIG") {
        if !py_strip(v).is_empty() {
            return pypath::expanduser(f, v, env, home);
        }
    }
    if exists(&paths.config_yaml) {
        return paths.config_yaml.clone();
    }
    let system_yaml = pypath::join(f, system_config_dir, &["config.yaml"]);
    if exists(&system_yaml) {
        return system_yaml;
    }
    paths.config_yaml.clone()
}

/// daemon 目錄集合（Task 0c；依 `EnvSnapshot` 解析，arch §3.x／規格 §3.2）。
#[derive(Clone, Debug, PartialEq, Eq)]
pub struct DaemonDirs {
    pub by_id_dir: String,
    pub by_path_dir: String,
    pub events_dir: String,
    pub events_runtime_dir: String,
}

/// 解析 daemon 目錄（Task 0c）。
pub fn resolve_daemon_dirs(env: &EnvSnapshot) -> DaemonDirs {
    let flavor = if std::path::MAIN_SEPARATOR == '\\' {
        PathFlavor::Windows
    } else {
        PathFlavor::Posix
    };
    resolve_daemon_dirs_for_flavor(env, flavor)
}

fn resolve_daemon_dirs_for_flavor(env: &EnvSnapshot, flavor: PathFlavor) -> DaemonDirs {
    let home = daemon_home(env, flavor);
    let home_root = home.as_deref().unwrap_or("~");
    let state_home_default = pypath::join(flavor, home_root, &[".local", "state"]);
    let state_home = env
        .get("XDG_STATE_HOME")
        .filter(|path| !py_strip(path).is_empty())
        .unwrap_or(&state_home_default);
    let state_default = pypath::join(flavor, state_home, &["serialwrap"]);
    let state_dir = daemon_env_path(env, "SERIALWRAP_STATE_DIR", &state_default, flavor);
    let events_default = pypath::join(flavor, home_root, &[".serialwrap", "events.d"]);
    let events_dir = daemon_env_path(env, "SERIALWRAP_EVENTS_DIR", &events_default, flavor);
    let events_runtime_default = pypath::join(flavor, &state_dir, &["events"]);
    let events_runtime_dir = daemon_env_path(
        env,
        "SERIALWRAP_EVENTS_RUNTIME_DIR",
        &events_runtime_default,
        flavor,
    );
    DaemonDirs {
        by_id_dir: daemon_env_path(env, "SERIALWRAP_BY_ID_DIR", "/dev/serial/by-id", flavor),
        by_path_dir: daemon_env_path(env, "SERIALWRAP_BY_PATH_DIR", "/dev/serial/by-path", flavor),
        events_dir,
        events_runtime_dir,
    }
}

struct NoDaemonHome;

impl HomeLookup for NoDaemonHome {
    fn current_user_home(&self) -> Option<String> {
        None
    }

    fn user_home(&self, _user: &str) -> Option<String> {
        None
    }
}

fn daemon_home(env: &EnvSnapshot, flavor: PathFlavor) -> Option<String> {
    let home = pypath::expanduser(flavor, "~", env, &NoDaemonHome);
    (home != "~").then_some(home)
}

fn daemon_env_path(env: &EnvSnapshot, key: &str, default: &str, flavor: PathFlavor) -> String {
    let value = env
        .get(key)
        .filter(|value| !py_strip(value).is_empty())
        .unwrap_or(default);
    pypath::expanduser(flavor, value, env, &NoDaemonHome)
}

#[cfg(test)]
mod daemon_dirs_tests {
    use super::*;

    #[test]
    fn daemon_dirs_windows_home_matches_ntpath_precedence() {
        let env = EnvSnapshot::from_pairs_fold_upper([
            ("HOME".into(), r"C:\other".into()),
            ("USERPROFILE".into(), r"C:\Users\p".into()),
            ("HOMEDRIVE".into(), "D:".into()),
            ("HOMEPATH".into(), r"\u\me".into()),
            ("XDG_STATE_HOME".into(), "~/state-root".into()),
            ("SERIALWRAP_BY_ID_DIR".into(), "~/serial".into()),
        ]);
        let dirs = resolve_daemon_dirs_for_flavor(&env, PathFlavor::Windows);
        assert_eq!(dirs.by_id_dir, r"C:\Users\p/serial");
        assert_eq!(dirs.events_dir, r"C:\Users\p\.serialwrap\events.d");
        assert_eq!(
            dirs.events_runtime_dir,
            r"C:\Users\p/state-root\serialwrap\events"
        );

        let fallback = EnvSnapshot::from_pairs_fold_upper([
            ("HOME".into(), r"C:\ignored".into()),
            ("HOMEDRIVE".into(), "D:".into()),
            ("HOMEPATH".into(), r"\u\me".into()),
        ]);
        let dirs = resolve_daemon_dirs_for_flavor(&fallback, PathFlavor::Windows);
        assert_eq!(dirs.events_dir, r"D:\u\me\.serialwrap\events.d");

        let no_home = EnvSnapshot::from_pairs_fold_upper([]);
        let dirs = resolve_daemon_dirs_for_flavor(&no_home, PathFlavor::Windows);
        assert_eq!(dirs.events_dir, r"~\.serialwrap\events.d");
        assert_eq!(dirs.events_runtime_dir, r"~\.local\state\serialwrap\events");
    }
}
