//! Python daemon 退役切換（P9-T16）：launcher 對退役 manifest 的守門測試（紅燈）。
//!
//! 夾具以結構體直接建構，**繞過** `validate_retirement`，模擬已被竄改的 manifest。
use sw_daemon::launcher::{resolve_launch, LaunchError, LaunchPlan};
use sw_proto::release_manifest::ReleaseManifest;

mod fixtures {
    use super::ReleaseManifest;

    /// 直接建構（不經 `parse`，故不做 validate_retirement）。
    fn build(name: &str) -> ReleaseManifest {
        let text = match name {
            "stage3_retired_but_python_impl.json" => {
                include_str!("fixtures/stage3_retired_but_python_impl.json")
            }
            "stage3_retired_rust.json" => include_str!("fixtures/stage3_retired_rust.json"),
            _ => panic!("未知 manifest fixture: {name}"),
        };
        let raw: serde_json::Value = serde_json::from_str(&text).unwrap();
        ReleaseManifest {
            schema_version: raw["schema_version"].as_u64().unwrap() as u32,
            bundle_version: raw["bundle_version"].as_str().unwrap().to_string(),
            stage: raw["stage"].as_str().unwrap().to_string(),
            target: raw["target"].as_str().unwrap().to_string(),
            git_revision: raw["git_revision"].as_str().unwrap().to_string(),
            python_cli_delegate_entry: None,
            raw,
        }
    }

    /// 已退役標記，但 daemon 的 implementation 被改成 python。
    pub fn stage3_retired_but_python_impl() -> ReleaseManifest {
        build("stage3_retired_but_python_impl.json")
    }

    /// 合法的退役 Rust manifest。
    pub fn stage3_retired_rust() -> ReleaseManifest {
        build("stage3_retired_rust.json")
    }
}

#[test]
fn tampered_retired_manifest_refuses_python_child() {
    let m = fixtures::stage3_retired_but_python_impl(); // 繞過 validate_retirement 直接建構
    assert!(matches!(
        resolve_launch(&m),
        Err(LaunchError::PythonDaemonRetired)
    ));
}

#[test]
fn retired_rust_manifest_runs_native() {
    assert!(matches!(
        resolve_launch(&fixtures::stage3_retired_rust()),
        Ok(LaunchPlan::RustNative)
    ));
}

#[test]
fn retired_python_error_matches_launcher_contract() {
    let error = resolve_launch(&fixtures::stage3_retired_but_python_impl()).unwrap_err();
    assert_eq!(
        error.to_string(),
        "serialwrapd: INTERNAL_ERROR: python daemon retired by release manifest"
    );
}
