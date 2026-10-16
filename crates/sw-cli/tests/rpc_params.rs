//! P3-T14 RPC 參數、wire 格式與有效 timeout 測試。
mod common;

use sw_cli::{
    argparse::{parse_args, Namespace, ParseResult},
    cli_spec::{root_spec, SpecDefaults},
    commands::rpc_cmds::{effective_timeout_s, request_for},
};

/// 表格的唯一來源：`tests/contract_blackbox/data/rpc_params.json`（55 列），
/// Rust 單元測試與黑箱測試共用同一檔，避免兩份表漂移。
fn table() -> Vec<(Vec<String>, String, String)> {
    let p = common::data_root().join("tests/contract_blackbox/data/rpc_params.json");
    let content = std::fs::read_to_string(&p)
        .unwrap_or_else(|e| panic!("無法讀取 {}: {e}", p.display()));
    let v = sw_proto::pyjson::loads(&content)
        .unwrap_or_else(|e| panic!("解析 {} 失敗: {e:?}", p.display()));
    rows_from(&v)
}

fn rows_from(v: &sw_proto::pyjson::PyValue) -> Vec<(Vec<String>, String, String)> {
    let list = match v {
        sw_proto::pyjson::PyValue::List(l) => l,
        other => panic!("預期頂層為 list，實際為: {other:?}"),
    };
    list.iter()
        .map(|item| {
            let dict = match item {
                sw_proto::pyjson::PyValue::Dict(d) => d,
                other => panic!("預期清單項為 dict，實際為: {other:?}"),
            };
            let argv = match dict.get("argv") {
                Some(sw_proto::pyjson::PyValue::List(l)) => l
                    .iter()
                    .map(|s| match s {
                        sw_proto::pyjson::PyValue::Str(st) => st.clone(),
                        other => panic!("預期 argv 元素為字串，實際為: {other:?}"),
                    })
                    .collect(),
                other => panic!("缺少或無效的 argv: {other:?}"),
            };
            let method = match dict.get("method") {
                Some(sw_proto::pyjson::PyValue::Str(s)) => s.clone(),
                other => panic!("缺少或無效的 method: {other:?}"),
            };
            let params = match dict.get("params") {
                Some(sw_proto::pyjson::PyValue::Str(s)) => s.clone(),
                other => panic!("缺少或無效的 params: {other:?}"),
            };
            (argv, method, params)
        })
        .collect()
}

fn shell_words(s: &str) -> Vec<String> {
    s.split_whitespace().map(|x| x.to_string()).collect()
}

fn ns(argv: &str) -> Namespace {
    let spec = root_spec(&SpecDefaults {
        version_line: "serialwrap t".into(),
        profile_dir: "/p".into(),
    });
    match parse_args(&spec, &shell_words(argv), 78) {
        ParseResult::Ok(n) => n,
        _ => panic!("{argv}"),
    }
}

fn ns_argv(argv: &[String]) -> Namespace {
    let spec = root_spec(&SpecDefaults {
        version_line: "serialwrap t".into(),
        profile_dir: "/p".into(),
    });
    match parse_args(&spec, argv, 78) {
        ParseResult::Ok(n) => n,
        _ => panic!("解析 argv 失敗 {argv:?}"),
    }
}

#[test]
fn params_wire_exact() {
    let rows = table();
    assert_eq!(rows.len(), 55);
    for (argv, method, params) in rows {
        let (m, p) = request_for(&ns_argv(&argv)).unwrap_or_else(|| panic!("{argv:?}"));
        assert_eq!(
            (m, sw_proto::pyjson::dumps(&p, &sw_proto::pyjson::RPC_WIRE)),
            (method.as_str(), params),
            "{argv:?}"
        );
    }
}

#[test]
fn long_timeout_floor() {
    for (argv, want) in [
        ("session attach --selector A", 45.0),
        ("session recover --selector A", 45.0),
        ("session self-test --selector A", 45.0),
        ("session console-attach --selector A", 45.0),
        ("file push --selector A --local l --remote r", 5.0),
        ("--timeout 2 session attach --selector A", 2.0),
    ] {
        let n = ns(argv);
        let (m, _) = request_for(&n).unwrap();
        assert_eq!(effective_timeout_s(&n, m), want, "{argv}");
    }
}

#[test]
fn json_boundary_quirks() {
    use sw_cli::json_boundary_command as jb;
    let v = |s: &str| shell_words(s);
    assert_eq!(jb(&v("--socket connect")), Some("connect"));
    assert_eq!(jb(&v("--endpoint=connect")), Some("connect"));
    assert_eq!(jb(&v("-vv benches")), Some("benches"));
    assert_eq!(jb(&v("--timeout 3 session list")), None);
    assert_eq!(jb(&v("--bench=x connect")), Some("connect"));
}
