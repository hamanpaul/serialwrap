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
    // Windows 的 canonicalize 回傳 verbatim 路徑（`\\?\C:\…`），其中 `/` 不是分隔符，
    // 後續以 `/` 串接的 fixture 子目錄會整個查無；去掉 verbatim 前綴改用一般絕對路徑。
    let base = base.strip_prefix(r"\\?\").map(str::to_owned).unwrap_or(base);
    let mut fails = Vec::new();
    for r in common::golden("load_profiles.tsv") {
        let dir = format!("{base}/{}", r.input);
        let got = match load_profiles(&dir, &DirFs) {
            // JSON 輸出中的 `\` 會跳脫成 `\\`（Windows 路徑），先換跳脫形，再換原形。
            Ok(res) => common::load_result_json(&res)
                .replace(&dir.replace('\\', "\\\\"), "${FIXTURE}")
                .replace(&dir, "${FIXTURE}"),
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
