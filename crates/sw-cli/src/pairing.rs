//! release manifest 版本配對判定（P4-T11 承接；doctor 的 `version_pairing` 與 update／rollback 共用）。
//!
//! 紅燈階段骨架：函式本體一律 `unimplemented!("P4-T11: <name>")`。
#![allow(unused_variables)]

use std::path::Path;

use sw_proto::pyjson::PyDict;
use sw_proto::release_manifest::ReleaseManifest;

/// 單一配對差異（`field` 名稱：`cli_version`、`daemon_version`、`bundle_version`、`sha256:<path>`、
/// `tree_sha256:<component>`、`stable_entry:<name>`）。
#[derive(Clone, Debug, PartialEq, Eq)]
pub struct PairingDiff {
    pub field: String,
    pub expected: String,
    pub actual: String,
}

/// update／rollback 內部用的結構化配對檢查（`pairing.rule == "exact"`）。
/// `digests` 為 `(component_path, actual_sha256)`，須與 manifest 宣告相同。
pub fn check_pairing(
    m: &ReleaseManifest,
    cli_version: &str,
    daemon_version: Option<&str>,
    digests: &[(String, String)],
) -> Vec<PairingDiff> {
    unimplemented!("P4-T11: check_pairing")
}

/// doctor 的 `version_pairing` 項目（逐字照 P1 `check_version_pairing` 的判定與 `mismatches` 順序）。
/// daemon 不可達時 `daemon_version` 為 `None`。
pub fn pairing_item(
    m: &ReleaseManifest,
    manifest_dir: &Path,
    cli_version: &str,
    daemon_version: Option<&str>,
) -> PyDict {
    unimplemented!("P4-T11: pairing_item")
}
