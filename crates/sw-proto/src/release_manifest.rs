//! Release manifest 與 current-release 解析（P3-T7 stub）。
#![allow(unused_variables, dead_code)]

use crate::pyjson::PyDict;

#[derive(Clone, Debug, PartialEq)]
pub struct ReleaseManifest {
    pub schema_version: i64,
    pub bundle_version: String,
    pub stage: String,
    pub target: String,
    pub git_revision: String,
    pub python_cli_delegate_entry: Vec<String>,
    pub raw: PyDict,
}

#[derive(Clone, Debug, PartialEq)]
pub struct CurrentRelease {
    pub bundle_version: String,
    pub dir: String,
}

#[derive(Clone, Debug, PartialEq)]
pub enum ManifestError {
    InvalidJson(String),
    Schema(String),
}

pub fn parse_manifest(text: &str) -> Result<ReleaseManifest, ManifestError> {
    todo!("P3-T7: parse_manifest")
}

pub fn parse_current_release(text: &str) -> Result<CurrentRelease, ManifestError> {
    todo!("P3-T7: parse_current_release")
}
