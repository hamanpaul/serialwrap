//! argparse 與 Python 3.12.3 golden 輸出對齊測試（P3-T10）。
mod common;

use sw_cli::argparse::{parse_args, ParseResult};
use sw_cli::cli_spec::{root_spec, SpecDefaults};

#[derive(Debug)]
struct Case {
    argv: Vec<String>,
    columns: usize,
    rc: i32,
    stdout: String,
    stderr: String,
    native: bool,
    post_parse_effects: bool,
    parse_expected: Option<(i32, String, String)>,
}

impl From<&sw_proto::pyjson::PyValue> for Case {
    fn from(val: &sw_proto::pyjson::PyValue) -> Self {
        let d = match val {
            sw_proto::pyjson::PyValue::Dict(d) => d,
            _ => panic!("Case 必須為 PyValue::Dict"),
        };
        let argv = match d.get("argv").expect("argv 欄位缺失") {
            sw_proto::pyjson::PyValue::List(l) => l
                .iter()
                .map(|item| match item {
                    sw_proto::pyjson::PyValue::Str(s) => s.clone(),
                    _ => panic!("argv 項目必須為 Str"),
                })
                .collect(),
            _ => panic!("argv 必須為 List"),
        };
        let columns = match d.get("columns").expect("columns 欄位缺失") {
            sw_proto::pyjson::PyValue::Int(sw_proto::pyjson::PyInt::Small(n)) => *n as usize,
            _ => panic!("columns 必須為 Small Int"),
        };
        let rc = match d.get("rc").expect("rc 欄位缺失") {
            sw_proto::pyjson::PyValue::Int(sw_proto::pyjson::PyInt::Small(n)) => *n as i32,
            _ => panic!("rc 必須為 Small Int"),
        };
        let stdout = match d.get("stdout").expect("stdout 欄位缺失") {
            sw_proto::pyjson::PyValue::Str(s) => s.clone(),
            _ => panic!("stdout 必須為 Str"),
        };
        let stderr = match d.get("stderr").expect("stderr 欄位缺失") {
            sw_proto::pyjson::PyValue::Str(s) => s.clone(),
            _ => panic!("stderr 必須為 Str"),
        };
        let native = match d.get("native").expect("native 欄位缺失") {
            sw_proto::pyjson::PyValue::Bool(b) => *b,
            _ => panic!("native 必須為 Bool"),
        };
        let post_parse_effects = match d.get("post_parse_effects") {
            Some(sw_proto::pyjson::PyValue::Bool(b)) => *b,
            _ => false,
        };
        let parse_expected = match (
            d.get("parse_rc"),
            d.get("parse_stdout"),
            d.get("parse_stderr"),
        ) {
            (
                Some(sw_proto::pyjson::PyValue::Int(sw_proto::pyjson::PyInt::Small(rc))),
                Some(sw_proto::pyjson::PyValue::Str(stdout)),
                Some(sw_proto::pyjson::PyValue::Str(stderr)),
            ) => Some((*rc as i32, stdout.clone(), stderr.clone())),
            (None, None, None) => None,
            _ => panic!("parse_rc、parse_stdout、parse_stderr 必須同時存在且型別正確"),
        };
        Case {
            argv,
            columns,
            rc,
            stdout,
            stderr,
            native,
            post_parse_effects,
            parse_expected,
        }
    }
}

impl Case {
    fn parse_only_expected(&self) -> (i32, &str, &str) {
        if let Some((rc, stdout, stderr)) = &self.parse_expected {
            (*rc, stdout, stderr)
        } else if self.post_parse_effects {
            (0, "", "")
        } else {
            (self.rc, self.stdout.as_str(), self.stderr.as_str())
        }
    }
}

#[test]
fn native_paths_match_python_3_12_golden() {
    let dir = common::data_root().join("contracts/cli_help/linux");
    let spec = root_spec(&SpecDefaults {
        version_line: sw_proto::version::version_line(),
        profile_dir: "/golden/config/profiles".into(),
        lock_path: "/golden/run/serialwrapd.lock".into(),
    });
    let mut fails = Vec::new();
    for ent in std::fs::read_dir(&dir).unwrap() {
        let path = ent.unwrap().path();
        if path.file_name().unwrap() == "_meta.json" {
            continue;
        }
        let case = sw_proto::pyjson::loads(&std::fs::read_to_string(&path).unwrap()).unwrap();
        let c = Case::from(&case);
        if !c.native {
            continue;
        }
        let (rc, out, err) = match parse_args(&spec, &c.argv, c.columns - 2) {
            ParseResult::Exit(x) => (x.code, x.stdout, x.stderr),
            ParseResult::Ok(_) => (0, String::new(), String::new()),
            ParseResult::Delegated => (-1, String::new(), String::new()),
        };
        if c.parse_only_expected() != (rc, out.as_str(), err.as_str()) {
            fails.push(format!(
                "{}:\n--- got rc={rc}\n{out}{err}\n--- want rc={}\n{}{}",
                path.display(),
                c.rc,
                c.stdout,
                c.stderr
            ));
        }
    }
    assert!(
        fails.is_empty(),
        "{} 筆不符\n{}",
        fails.len(),
        fails.join("\n")
    );
}

#[test]
fn help_width_rules() {
    use sw_cli::argparse::help_width;
    assert_eq!(help_width(Some("120"), Some(50)), 118);
    assert_eq!(help_width(Some("0"), Some(50)), 48);
    assert_eq!(help_width(Some("x"), None), 78);
    assert_eq!(help_width(None, None), 78);
}

/// P4-T1：Stage 1B 原生承接群組的 help golden（`native:false` 原本因委派而略過；Task 7–9 原生後必須逐寬度比對）。
///
/// `setup` 與 `supervision-mode` 不在此列：其 Rust-only 擴充使輸出與 Python 必然不同，改比對 `contracts/cli_help/rust_only`（Task 10／12）。
#[test]
fn stage1b_group_help_goldens_match_python_at_every_width() {
    const STAGE_1B_GROUPS: [&str; 6] = ["daemon", "remote", "connect", "benches", "service", "doctor"];
    let dir = common::data_root().join("contracts/cli_help/linux");
    let spec = root_spec(&SpecDefaults {
        version_line: sw_proto::version::version_line(),
        profile_dir: "/golden/config/profiles".into(),
        lock_path: "/golden/run/serialwrapd.lock".into(),
    });
    let mut fails = Vec::new();
    let mut checked = 0usize;
    for ent in std::fs::read_dir(&dir).unwrap() {
        let path = ent.unwrap().path();
        let name = path.file_name().unwrap().to_string_lossy().into_owned();
        let Some(rest) = name.strip_prefix("help_grp_") else { continue };
        let Some((group, _)) = rest.rsplit_once("_col") else { continue };
        if !STAGE_1B_GROUPS.contains(&group) {
            continue;
        }
        let case = sw_proto::pyjson::loads(&std::fs::read_to_string(&path).unwrap()).unwrap();
        let c = Case::from(&case);
        checked += 1;
        let (rc, out, err) = match parse_args(&spec, &c.argv, c.columns - 2) {
            ParseResult::Exit(x) => (x.code, x.stdout, x.stderr),
            ParseResult::Ok(_) => (0, String::new(), String::new()),
            ParseResult::Delegated => (-1, String::new(), String::new()),
        };
        if c.parse_only_expected() != (rc, out.as_str(), err.as_str()) {
            fails.push(format!("{name}: got rc={rc}\n{out}{err}"));
        }
    }
    assert!(checked >= 6 * 9, "Stage 1B 群組 help golden 數量不足：{checked}");
    assert!(fails.is_empty(), "{} 筆 Stage 1B help 與 Python golden 不符\n{}", fails.len(), fails.join("\n"));
}

