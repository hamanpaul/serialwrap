mod common;
use sw_proto::config_types::{load_profiles, ProfileFs, RuntimeConfig};
use sw_proto::{pycompat::PyOsError, pypath::PathFlavor};

struct DirFs;
impl ProfileFs for DirFs {
    fn flavor(&self) -> PathFlavor { PathFlavor::Posix }
    fn is_dir(&self, p: &str) -> bool { std::path::Path::new(p).is_dir() }
    fn list_dir(&self, p: &str) -> Result<Vec<String>, PyOsError> {
        Ok(std::fs::read_dir(p).unwrap().map(|e| e.unwrap().file_name().into_string().unwrap()).collect())
    }
    fn read_bytes(&self, p: &str) -> Result<Vec<u8>, PyOsError> { Ok(std::fs::read(p).unwrap()) }
    fn expanduser(&self, p: &str) -> String { p.replacen('~', "/golden/home", 1) }
}

#[test]
fn load_profiles_golden() {
    let base = common::data_root().join("contracts/golden/profiles_fixtures");
    let base = std::fs::canonicalize(base).unwrap().to_string_lossy().into_owned();
    let mut fails = Vec::new();
    for r in common::golden("load_profiles.tsv") {
        let dir = format!("{base}/{}", r.input);
        let got = match load_profiles(&dir, &DirFs) {
            Ok(res) => common::load_result_json(&res).replace(&dir, "${FIXTURE}"),
            Err(e) => common::config_error_kind(&e),
        };
        if got != r.expected { fails.push(format!("{}: got {got}\nwant {}", r.input, r.expected)); }
    }
    assert!(fails.is_empty(), "{}", fails.join("\n\n"));
}

#[test]
fn runtime_config_semantics() {
    assert!(RuntimeConfig::from_text(None).unwrap().mode().is_none());
    assert!(RuntimeConfig::from_text(Some("- a\n")).unwrap().socket_path().is_none());       // 非 dict → {}
    assert!(RuntimeConfig::from_text(Some("a: [\n")).is_err());                               // YAML 錯 → Err（_safe_runtime_config 會吞）
    let rc = RuntimeConfig::from_text(Some("socket_path: 5\nsupervision_mode: on-demand\n")).unwrap();
    assert!(matches!(rc.socket_path(), Some(sw_proto::yaml11::YValue::Int(_))));              // Q-18：不驗型別
}
