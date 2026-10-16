//! 執行期路徑與環境變數快照（P3-T4 / P3-T7 stub）。
#![allow(unused_variables, dead_code)]

use std::collections::BTreeMap;
use crate::pyjson::PyInt;

/// 凍結的環境變數快照。
#[derive(Clone, Debug, Default, PartialEq, Eq)]
pub struct EnvSnapshot {
    pub vars: BTreeMap<String, String>,
}

impl EnvSnapshot {
    pub fn from_pairs<I: IntoIterator<Item = (String, String)>>(it: I) -> Self {
        todo!("P3-T4: EnvSnapshot::from_pairs")
    }

    pub fn get(&self, k: &str) -> Option<&str> {
        todo!("P3-T4: EnvSnapshot::get")
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
