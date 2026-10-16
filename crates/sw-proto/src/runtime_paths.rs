//! 執行期路徑與環境變數快照（P3-T4 / P3-T7 stub）。
#![allow(unused_variables, dead_code)]

use crate::pyjson::PyInt;
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
}

#[derive(Clone, Copy, Debug)]
pub struct NativeTraits {
    pub default_endpoint_is_tcp: bool,
    pub path_flavor: crate::pypath::PathFlavor,
}

#[derive(Clone, Debug, PartialEq)]
pub struct RuntimePaths {
    pub config_dir: String,
    pub state_dir: String,
    pub run_dir: String,
    pub data_dir: String,
    pub lock_path: String,
    pub socket_path: String,
    pub default_tcp_port: PyInt,
    pub default_endpoint: String,
    pub profile_dir: String,
    pub wal_dir: String,
    pub log_dir: String,
    pub config_yaml: String,
}

#[derive(Clone, Debug, PartialEq)]
pub enum PathsError {
    InvalidTcpPort { raw: String },
}

impl RuntimePaths {
    pub fn resolve(
        env: &EnvSnapshot,
        native: NativeTraits,
        home: &dyn crate::pypath::HomeLookup,
    ) -> Result<RuntimePaths, PathsError> {
        todo!("P3-T7: RuntimePaths::resolve")
    }
}

pub fn bench_state_path(
    env: &EnvSnapshot,
    f: crate::pypath::PathFlavor,
    home: &dyn crate::pypath::HomeLookup,
) -> String {
    todo!("P3-T7: bench_state_path")
}

pub fn resolve_config_path(
    env: &EnvSnapshot,
    paths: &RuntimePaths,
    system_config_dir: &str,
    f: crate::pypath::PathFlavor,
    home: &dyn crate::pypath::HomeLookup,
    exists: &dyn Fn(&str) -> bool,
) -> String {
    todo!("P3-T7: resolve_config_path")
}
