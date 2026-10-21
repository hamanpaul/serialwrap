//! Python daemon 退役切換（P9-T16）：launcher 對退役 manifest 的守門測試（紅燈）。
//!
//! 夾具以結構體直接建構，**繞過** `validate_retirement`，模擬已被竄改的 manifest。
use sw_daemon::launcher::{resolve_launch, LaunchError, LaunchPlan};
use sw_proto::pyjson::{self, PyValue};
use sw_proto::release_manifest::ReleaseManifest;

mod fixtures {
    use super::{pyjson, PyValue, ReleaseManifest};

    /// 直接建構（不經 `parse`，故不做 validate_retirement）。
    /// P9 原夾具以 serde_json 讀取；本 crate 不引入第二套 JSON 解析器，改用 P3 的 `pyjson`（僅換解析器，斷言不變）。
    fn build(name: &str) -> ReleaseManifest {
        let text = match name {
            "stage3_retired_but_python_impl.json" => {
                include_str!("fixtures/stage3_retired_but_python_impl.json")
            }
            "stage3_retired_rust.json" => include_str!("fixtures/stage3_retired_rust.json"),
            _ => panic!("未知 manifest fixture: {name}"),
        };
        let PyValue::Dict(raw) = pyjson::loads(text).expect("夾具 JSON 必須合法") else {
            panic!("夾具頂層必須為物件");
        };
        let field = |key: &str| match raw.get(key) {
            Some(PyValue::Str(value)) => value.clone(),
            _ => panic!("夾具缺少字串欄位 {key}"),
        };
        ReleaseManifest {
            schema_version: 1,
            bundle_version: field("bundle_version"),
            stage: field("stage"),
            target: field("target"),
            git_revision: field("git_revision"),
            python_cli_delegate_entry: None,
            state_schema: None,
            task_xml_template_sha256: None,
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
