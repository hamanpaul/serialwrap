//! 管理面子命令解析器擴充測試（P4-T1）。
mod common;

use sw_cli::argparse::{parse_args, ArgValue, ParseResult};
use sw_cli::cli_spec::{root_spec, SpecDefaults};

#[test]
fn supervision_mode_help_matches_rust_only_golden() {
    let root = common::data_root();
    let spec = root_spec(&SpecDefaults {
        version_line: sw_proto::version::version_line(),
        profile_dir: "/golden/config/profiles".into(),
        lock_path: "/golden/run/serialwrapd.lock".into(),
    });
    for cols in [80, 120] {
        let golden_path =
            root.join(format!("contracts/cli_help/rust_only/supervision-mode.{cols}.txt"));
        let want = std::fs::read_to_string(&golden_path).expect("golden 檔案必須存在");
        let result = parse_args(&spec, &["supervision-mode".into(), "--help".into()], cols - 2);
        match result {
            ParseResult::Exit(x) => {
                assert_eq!(x.code, 0);
                assert_eq!(x.stdout, want);
            }
            _ => panic!("預期 ParseResult::Exit(0)"),
        }
    }
}

#[test]
fn setup_help_matches_rust_only_golden() {
    let root = common::data_root();
    let spec = root_spec(&SpecDefaults {
        version_line: sw_proto::version::version_line(),
        profile_dir: "/golden/config/profiles".into(),
        lock_path: "/golden/run/serialwrapd.lock".into(),
    });
    for cols in [80, 120] {
        let golden_path = root.join(format!("contracts/cli_help/rust_only/setup.{cols}.txt"));
        let want = std::fs::read_to_string(&golden_path).expect("golden 檔案必須存在");
        let result = parse_args(&spec, &["setup".into(), "--help".into()], cols - 2);
        match result {
            ParseResult::Exit(x) => {
                assert_eq!(x.code, 0);
                assert_eq!(x.stdout, want);
            }
            _ => panic!("預期 ParseResult::Exit(0)"),
        }
    }
}

#[test]
fn setup_rollback_and_user_mutually_exclusive() {
    let spec = root_spec(&SpecDefaults {
        version_line: sw_proto::version::version_line(),
        profile_dir: "/golden/config/profiles".into(),
        lock_path: "/golden/run/serialwrapd.lock".into(),
    });
    let result = parse_args(
        &spec,
        &["setup".into(), "--user".into(), "--rollback".into()],
        78,
    );
    match result {
        ParseResult::Exit(x) => {
            assert_eq!(x.code, 2, "互斥參數應 exit 2");
            // 逐字比對（計畫 Task 1 步驟 3 第 10 點）：與 Python argparse＋擴充實測一致。
            assert_eq!(
                x.stderr,
                "usage: serialwrap setup [-h]\n                        [--user | --system | --on-demand | --windows-task | --windows-service | --rollback]\n                        [--force] [--with-sudo]\nserialwrap setup: error: argument --rollback: not allowed with argument --user\n"
            );
        }
        _ => panic!("預期 ParseResult::Exit(2)"),
    }
}

#[test]
fn supervision_mode_words_nargs_star() {
    let spec = root_spec(&SpecDefaults {
        version_line: sw_proto::version::version_line(),
        profile_dir: "/golden/config/profiles".into(),
        lock_path: "/golden/run/serialwrapd.lock".into(),
    });
    let result = parse_args(
        &spec,
        &[
            "supervision-mode".into(),
            "set".into(),
            "systemd-user".into(),
            "--force".into(),
        ],
        78,
    );
    match result {
        ParseResult::Ok(ns) => {
            let words = ns.get("words");
            assert_eq!(
                words,
                &ArgValue::List(vec!["set".into(), "systemd-user".into()])
            );
            let force = ns.get("force");
            assert_eq!(force, &ArgValue::Bool(true));
        }
        ParseResult::Exit(x) => {
            panic!("解析失敗，exit code {}: {}", x.code, x.stderr);
        }
        ParseResult::Delegated => panic!("不應委派"),
    }
}
