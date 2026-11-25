use sw_proto::config_types::{load_profiles, ProfileFs, TimeoutRecoveryKey};
use sw_proto::pycompat::PyOsError;
use sw_proto::pypath::PathFlavor;

struct DirFs;

impl ProfileFs for DirFs {
    fn flavor(&self) -> PathFlavor {
        PathFlavor::Posix
    }

    fn is_dir(&self, path: &str) -> bool {
        std::path::Path::new(path).is_dir()
    }

    fn list_dir(&self, path: &str) -> Result<Vec<String>, PyOsError> {
        Ok(std::fs::read_dir(path)
            .unwrap()
            .map(|entry| entry.unwrap().file_name().into_string().unwrap())
            .collect())
    }

    fn read_bytes(&self, path: &str) -> Result<Vec<u8>, PyOsError> {
        Ok(std::fs::read(path).unwrap())
    }

    fn expanduser(&self, path: &str) -> String {
        path.replacen('~', "/home/test", 1)
    }
}

fn load(text: &str) -> sw_proto::config_types::LoadResult {
    let dir = tempfile::tempdir().unwrap();
    std::fs::write(dir.path().join("default.yaml"), text).unwrap();
    let path = dir.path().to_string_lossy().into_owned();
    load_profiles(&path, &DirFs).unwrap()
}

fn rejects(text: &str) -> bool {
    let dir = tempfile::tempdir().unwrap();
    std::fs::write(dir.path().join("default.yaml"), text).unwrap();
    let path = dir.path().to_string_lossy().into_owned();
    load_profiles(&path, &DirFs).is_err()
}

#[test]
fn language_default_and_null_use_ctrl_c_only() {
    let result = load(
        "profiles:\n  omitted:\n    platform: shell\n  explicit-null:\n    platform: shell\n    timeout_recovery_keys: null\ntargets: []\n",
    );
    assert_eq!(
        result
            .templates
            .iter()
            .map(|template| template.timeout_recovery_keys.clone())
            .collect::<Vec<_>>(),
        vec![
            vec![TimeoutRecoveryKey::CtrlC],
            vec![TimeoutRecoveryKey::CtrlC]
        ]
    );
}

#[test]
fn target_null_inherits_and_empty_list_overrides() {
    let result = load(
        "profiles:\n  shell:\n    platform: shell\n    timeout_recovery_keys: [CTRL_D, CTRL_C]\ntargets:\n  - {com: COM0, profile: shell, act_no: 1, alias: a, device_by_id: /dev/a}\n  - {com: COM1, profile: shell, act_no: 2, alias: b, device_by_id: /dev/b, timeout_recovery_keys: null}\n  - {com: COM2, profile: shell, act_no: 3, alias: c, device_by_id: /dev/c, timeout_recovery_keys: []}\n",
    );
    assert_eq!(
        result.profiles[0].template.timeout_recovery_keys,
        vec![TimeoutRecoveryKey::CtrlD, TimeoutRecoveryKey::CtrlC]
    );
    assert_eq!(
        result.profiles[1].template.timeout_recovery_keys,
        vec![TimeoutRecoveryKey::CtrlD, TimeoutRecoveryKey::CtrlC]
    );
    assert!(result.profiles[2].template.timeout_recovery_keys.is_empty());
}

#[test]
fn invalid_keys_are_rejected_without_fallback() {
    for value in [
        "CTRL_C",
        "[ctrl_c]",
        "[CTRL_X]",
        "[CTRL_C, CTRL_C]",
        "[CTRL_C, 4]",
    ] {
        let yaml = format!(
            "profiles:\n  shell:\n    platform: shell\n    timeout_recovery_keys: {value}\ntargets: []\n"
        );
        assert!(rejects(&yaml), "非法 timeout_recovery_keys 被接受：{value}");
    }
}

#[test]
fn template_and_target_key_matrix() {
    let result = load(
        "profiles:\n  omitted:\n    platform: shell\n  explicit-null:\n    platform: shell\n    timeout_recovery_keys: null\n  one:\n    platform: shell\n    timeout_recovery_keys: [CTRL_C]\n  reverse:\n    platform: shell\n    timeout_recovery_keys: [CTRL_D, CTRL_C]\n  empty:\n    platform: shell\n    timeout_recovery_keys: []\ntargets: []\n",
    );
    let by_name = |name: &str| {
        result
            .templates
            .iter()
            .find(|template| template.profile_name == name)
            .unwrap()
            .timeout_recovery_keys
            .clone()
    };
    assert_eq!(by_name("omitted"), vec![TimeoutRecoveryKey::CtrlC]);
    assert_eq!(by_name("explicit-null"), vec![TimeoutRecoveryKey::CtrlC]);
    assert_eq!(by_name("one"), vec![TimeoutRecoveryKey::CtrlC]);
    assert_eq!(
        by_name("reverse"),
        vec![TimeoutRecoveryKey::CtrlD, TimeoutRecoveryKey::CtrlC]
    );
    assert!(by_name("empty").is_empty());
}

#[test]
fn rust_only_contract_matrix_matches_generated_golden() {
    const GOLDEN: &str =
        include_str!("../../../contracts/golden/rust_only/timeout_recovery_keys.ndjson");
    assert_eq!(
        GOLDEN,
        concat!(
            "{\"case\":\"language_default_omitted\",\"effective\":[\"CTRL_C\"],\"target\":\"OMITTED\",\"template\":\"OMITTED\",\"tx_hex\":\"03\"}\n",
            "{\"case\":\"template_null_uses_default\",\"effective\":[\"CTRL_C\"],\"target\":\"OMITTED\",\"template\":null,\"tx_hex\":\"03\"}\n",
            "{\"case\":\"target_omitted_inherits\",\"effective\":[\"CTRL_D\",\"CTRL_C\"],\"target\":\"OMITTED\",\"template\":[\"CTRL_D\",\"CTRL_C\"],\"tx_hex\":\"0403\"}\n",
            "{\"case\":\"target_null_inherits\",\"effective\":[\"CTRL_D\",\"CTRL_C\"],\"target\":null,\"template\":[\"CTRL_D\",\"CTRL_C\"],\"tx_hex\":\"0403\"}\n",
            "{\"case\":\"target_list_replaces\",\"effective\":[\"CTRL_C\"],\"target\":[\"CTRL_C\"],\"template\":[\"CTRL_D\"],\"tx_hex\":\"03\"}\n",
            "{\"case\":\"empty_list_sends_nothing\",\"effective\":[],\"target\":[],\"template\":[\"CTRL_C\",\"CTRL_D\"],\"tx_hex\":\"\"}\n",
            "{\"case\":\"explicit_two_key_order\",\"effective\":[\"CTRL_C\",\"CTRL_D\"],\"target\":\"OMITTED\",\"template\":[\"CTRL_C\",\"CTRL_D\"],\"tx_hex\":\"0304\"}\n",
        )
    );
}
