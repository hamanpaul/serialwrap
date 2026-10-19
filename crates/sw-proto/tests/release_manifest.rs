mod common;

use sw_proto::release_manifest::{
    parse_current_release, parse_manifest, CurrentRelease, ManifestError, ReleaseManifest,
};
use sw_proto::version::{version, version_line};

#[test]
fn test_version_line_and_package_sync() {
    let ver = version();
    let expected_ver = env!("CARGO_PKG_VERSION");
    assert_eq!(ver, expected_ver);
    assert_eq!(version_line(), format!("serialwrap {expected_ver}"));
}

#[test]
fn test_parse_manifest_fixture_normal() {
    let text = r#"{
      "schema_version": 1,
      "bundle_version": "0.3.2",
      "stage": "1A",
      "target": "x86_64-unknown-linux-gnu",
      "git_revision": "abcdef0123456789",
      "components": {
        "python_cli_delegate": {
          "entry": ["py/python", "-I", "-m", "sw_core.cli"]
        }
      }
    }"#;
    let manifest = parse_manifest(text).expect("valid manifest");
    assert_eq!(
        manifest,
        ReleaseManifest {
            schema_version: 1,
            bundle_version: "0.3.2".into(),
            stage: "1A".into(),
            target: "x86_64-unknown-linux-gnu".into(),
            git_revision: "abcdef0123456789".into(),
            python_cli_delegate_entry: vec![
                "py/python".into(),
                "-I".into(),
                "-m".into(),
                "sw_core.cli".into(),
            ],
            state_schema: None,
            task_xml_template_sha256: None,
            raw: manifest.raw.clone(),
        }
    );
}

#[test]
fn test_parse_manifest_missing_entry() {
    let text = r#"{
      "schema_version": 1,
      "bundle_version": "0.3.2",
      "stage": "1A",
      "target": "x86_64-unknown-linux-gnu",
      "git_revision": "abcdef0123456789",
      "components": {
        "python_cli_delegate": {}
      }
    }"#;
    assert!(matches!(parse_manifest(text), Err(ManifestError::Schema(_))));
}

#[test]
fn test_parse_manifest_entry_empty_list() {
    let text = r#"{
      "schema_version": 1,
      "bundle_version": "0.3.2",
      "stage": "1A",
      "target": "x86_64-unknown-linux-gnu",
      "git_revision": "abcdef0123456789",
      "components": {
        "python_cli_delegate": {
          "entry": []
        }
      }
    }"#;
    assert!(matches!(parse_manifest(text), Err(ManifestError::Schema(_))));
}

#[test]
fn test_parse_manifest_entry_contains_number() {
    let text = r#"{
      "schema_version": 1,
      "bundle_version": "0.3.2",
      "stage": "1A",
      "target": "x86_64-unknown-linux-gnu",
      "git_revision": "abcdef0123456789",
      "components": {
        "python_cli_delegate": {
          "entry": ["py/python", 123]
        }
      }
    }"#;
    assert!(matches!(parse_manifest(text), Err(ManifestError::Schema(_))));
}

#[test]
fn test_parse_manifest_schema_version_2() {
    let text = r#"{
      "schema_version": 2,
      "bundle_version": "0.3.2",
      "stage": "1A",
      "target": "x86_64-unknown-linux-gnu",
      "git_revision": "abcdef0123456789",
      "components": {
        "python_cli_delegate": {
          "entry": ["py/python"]
        }
      }
    }"#;
    assert!(matches!(parse_manifest(text), Err(ManifestError::Schema(_))));
}

#[test]
fn test_parse_manifest_invalid_json() {
    let text = r#"not valid json {"#;
    assert!(matches!(parse_manifest(text), Err(ManifestError::InvalidJson(_))));
}

#[test]
fn test_parse_manifest_rejects_missing_or_non_exact_pairing_rule() {
    let base = r#"{
      "schema_version": 1,
      "bundle_version": "0.3.2",
      "stage": "1A",
      "target": "x86_64-unknown-linux-gnu",
      "git_revision": "abcdef0123456789",
      "components": {
        "python_cli_delegate": {
          "entry": ["py/python"]
        }
      },
      "pairing": {"cli_version": "0.3.2", "daemon_version": "0.3.2"}
    }"#;
    assert!(
        matches!(parse_manifest(base), Err(ManifestError::Schema(_))),
        "缺 pairing.rule 時必須拒絕"
    );
    let invalid = base.replace(
        r#""cli_version": "0.3.2""#,
        r#""rule": "loose", "cli_version": "0.3.2""#,
    );
    assert!(
        matches!(parse_manifest(&invalid), Err(ManifestError::Schema(_))),
        "pairing.rule 不是 exact 時必須拒絕"
    );
}

#[test]
fn test_parse_current_release() {
    let text = r#"{"bundle_version":"0.3.2","dir":"/opt/serialwrap/versions/0.3.2"}"#;
    let cur = parse_current_release(text).expect("valid current release");
    assert_eq!(
        cur,
        CurrentRelease {
            bundle_version: "0.3.2".into(),
            dir: "/opt/serialwrap/versions/0.3.2".into(),
        }
    );
}

#[test]
fn test_schema_contract_present() {
    let p = common::data_root().join("contracts/release_manifest.schema.json");
    if !p.exists() {
        panic!("P1 contracts 缺少: {}", p.display());
    }
}
