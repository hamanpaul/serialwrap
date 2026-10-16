//! 12 個合成 parser 案例比對測試（P3-T10）。
mod common;

use std::collections::BTreeMap;
use sw_cli::argparse::{
    parse_args, ActionKind, ActionSpec, ArgValue, Conv, MutexSpec, Nargs, ParseResult, ParserSpec,
};
use sw_proto::pyjson::{self, PyInt, PyValue};

fn unhex(s: &str) -> Vec<u8> {
    (0..s.len())
        .step_by(2)
        .map(|i| u8::from_str_radix(&s[i..i + 2], 16).unwrap())
        .collect()
}

struct Row {
    id: String,
    op: String,
    input: String,
    expected: String,
}

fn read_synth_tsv() -> Vec<Row> {
    let p = common::data_root().join("contracts/golden/argparse_synth.tsv");
    let text = std::fs::read_to_string(&p).unwrap_or_else(|e| panic!("{}: {e}", p.display()));
    text.lines()
        .filter(|l| !l.is_empty())
        .map(|l| {
            let parts: Vec<&str> = l.split('\t').collect();
            assert_eq!(parts.len(), 4, "TSV 列必須為 4 欄");
            Row {
                id: parts[0].to_string(),
                op: parts[1].to_string(),
                input: String::from_utf8(unhex(parts[2])).expect("input 必須為 UTF-8"),
                expected: String::from_utf8(unhex(parts[3])).expect("expected 必須為 UTF-8"),
            }
        })
        .collect()
}

fn build_specs() -> BTreeMap<&'static str, ParserSpec> {
    let mut m = BTreeMap::new();

    // P01: nargs='*'
    m.insert(
        "P01",
        ParserSpec {
            prog: "p01".into(),
            description: None,
            epilog: None,
            raw_text: false,
            add_help: true,
            allow_abbrev: true,
            actions: vec![ActionSpec {
                option_strings: vec!["--items"],
                dest: "items",
                kind: ActionKind::Store,
                nargs: Nargs::ZeroOrMore,
                conv: Conv::Str,
                default: ArgValue::List(vec![]),
                choices: None,
                required: false,
                help: None,
                metavar: None,
                version: None,
            }],
            mutex: vec![],
            sub: None,
        },
    );

    // P02: action='append'
    m.insert(
        "P02",
        ParserSpec {
            prog: "p02".into(),
            description: None,
            epilog: None,
            raw_text: false,
            add_help: true,
            allow_abbrev: true,
            actions: vec![ActionSpec {
                option_strings: vec!["-i", "--item"],
                dest: "item",
                kind: ActionKind::Append,
                nargs: Nargs::One,
                conv: Conv::Str,
                default: ArgValue::None,
                choices: None,
                required: false,
                help: None,
                metavar: None,
                version: None,
            }],
            mutex: vec![],
            sub: None,
        },
    );

    // P03: action='count'
    m.insert(
        "P03",
        ParserSpec {
            prog: "p03".into(),
            description: None,
            epilog: None,
            raw_text: false,
            add_help: true,
            allow_abbrev: true,
            actions: vec![ActionSpec {
                option_strings: vec!["-v", "--verbose"],
                dest: "verbose",
                kind: ActionKind::Count,
                nargs: Nargs::One,
                conv: Conv::Str,
                default: ArgValue::Count(0),
                choices: None,
                required: false,
                help: None,
                metavar: None,
                version: None,
            }],
            mutex: vec![],
            sub: None,
        },
    );

    // P04: 必填 mutex
    m.insert(
        "P04",
        ParserSpec {
            prog: "p04".into(),
            description: None,
            epilog: None,
            raw_text: false,
            add_help: true,
            allow_abbrev: true,
            actions: vec![
                ActionSpec {
                    option_strings: vec!["--foo"],
                    dest: "foo",
                    kind: ActionKind::Store,
                    nargs: Nargs::One,
                    conv: Conv::Str,
                    default: ArgValue::None,
                    choices: None,
                    required: false,
                    help: None,
                    metavar: None,
                    version: None,
                },
                ActionSpec {
                    option_strings: vec!["--bar"],
                    dest: "bar",
                    kind: ActionKind::Store,
                    nargs: Nargs::One,
                    conv: Conv::Str,
                    default: ArgValue::None,
                    choices: None,
                    required: false,
                    help: None,
                    metavar: None,
                    version: None,
                },
            ],
            mutex: vec![MutexSpec {
                required: true,
                dests: vec!["foo", "bar"],
            }],
            sub: None,
        },
    );

    // P05: choices
    m.insert(
        "P05",
        ParserSpec {
            prog: "p05".into(),
            description: None,
            epilog: None,
            raw_text: false,
            add_help: true,
            allow_abbrev: true,
            actions: vec![ActionSpec {
                option_strings: vec!["--mode"],
                dest: "mode",
                kind: ActionKind::Store,
                nargs: Nargs::One,
                conv: Conv::Str,
                default: ArgValue::Str("auto".into()),
                choices: Some(vec!["auto", "manual", "off"]),
                required: false,
                help: None,
                metavar: None,
                version: None,
            }],
            mutex: vec![],
            sub: None,
        },
    );

    // P06: type=int 負數
    m.insert(
        "P06",
        ParserSpec {
            prog: "p06".into(),
            description: None,
            epilog: None,
            raw_text: false,
            add_help: true,
            allow_abbrev: true,
            actions: vec![ActionSpec {
                option_strings: vec!["-n", "--num"],
                dest: "num",
                kind: ActionKind::Store,
                nargs: Nargs::One,
                conv: Conv::Int,
                default: ArgValue::Int(PyInt::Small(0)),
                choices: None,
                required: false,
                help: None,
                metavar: None,
                version: None,
            }],
            mutex: vec![],
            sub: None,
        },
    );

    // P07: allow_abbrev=False
    m.insert(
        "P07",
        ParserSpec {
            prog: "p07".into(),
            description: None,
            epilog: None,
            raw_text: false,
            add_help: true,
            allow_abbrev: false,
            actions: vec![
                ActionSpec {
                    option_strings: vec!["--foobar"],
                    dest: "foobar",
                    kind: ActionKind::Store,
                    nargs: Nargs::One,
                    conv: Conv::Str,
                    default: ArgValue::None,
                    choices: None,
                    required: false,
                    help: None,
                    metavar: None,
                    version: None,
                },
                ActionSpec {
                    option_strings: vec!["--foobaz"],
                    dest: "foobaz",
                    kind: ActionKind::Store,
                    nargs: Nargs::One,
                    conv: Conv::Str,
                    default: ArgValue::None,
                    choices: None,
                    required: false,
                    help: None,
                    metavar: None,
                    version: None,
                },
            ],
            mutex: vec![],
            sub: None,
        },
    );

    // P08: 長選項前綴衝突
    m.insert(
        "P08",
        ParserSpec {
            prog: "p08".into(),
            description: None,
            epilog: None,
            raw_text: false,
            add_help: true,
            allow_abbrev: true,
            actions: vec![
                ActionSpec {
                    option_strings: vec!["--version"],
                    dest: "version",
                    kind: ActionKind::StoreTrue,
                    nargs: Nargs::One,
                    conv: Conv::Str,
                    default: ArgValue::Bool(false),
                    choices: None,
                    required: false,
                    help: None,
                    metavar: None,
                    version: None,
                },
                ActionSpec {
                    option_strings: vec!["--verbose"],
                    dest: "verbose",
                    kind: ActionKind::StoreTrue,
                    nargs: Nargs::One,
                    conv: Conv::Str,
                    default: ArgValue::Bool(false),
                    choices: None,
                    required: false,
                    help: None,
                    metavar: None,
                    version: None,
                },
            ],
            mutex: vec![],
            sub: None,
        },
    );

    // P09: -xVALUE（短選項直接相連值）
    m.insert(
        "P09",
        ParserSpec {
            prog: "p09".into(),
            description: None,
            epilog: None,
            raw_text: false,
            add_help: true,
            allow_abbrev: true,
            actions: vec![
                ActionSpec {
                    option_strings: vec!["-f"],
                    dest: "f",
                    kind: ActionKind::Store,
                    nargs: Nargs::One,
                    conv: Conv::Str,
                    default: ArgValue::None,
                    choices: None,
                    required: false,
                    help: None,
                    metavar: None,
                    version: None,
                },
                ActionSpec {
                    option_strings: vec!["-b"],
                    dest: "b",
                    kind: ActionKind::StoreTrue,
                    nargs: Nargs::One,
                    conv: Conv::Str,
                    default: ArgValue::Bool(false),
                    choices: None,
                    required: false,
                    help: None,
                    metavar: None,
                    version: None,
                },
            ],
            mutex: vec![],
            sub: None,
        },
    );

    // P10: -x=VALUE（短選項以等號連接值）
    m.insert(
        "P10",
        ParserSpec {
            prog: "p10".into(),
            description: None,
            epilog: None,
            raw_text: false,
            add_help: true,
            allow_abbrev: true,
            actions: vec![ActionSpec {
                option_strings: vec!["-s"],
                dest: "s",
                kind: ActionKind::Store,
                nargs: Nargs::One,
                conv: Conv::Str,
                default: ArgValue::None,
                choices: None,
                required: false,
                help: None,
                metavar: None,
                version: None,
            }],
            mutex: vec![],
            sub: None,
        },
    );

    // P11: 合併旗標 -vv 與 boolean flags
    m.insert(
        "P11",
        ParserSpec {
            prog: "p11".into(),
            description: None,
            epilog: None,
            raw_text: false,
            add_help: true,
            allow_abbrev: true,
            actions: vec![
                ActionSpec {
                    option_strings: vec!["-a"],
                    dest: "a",
                    kind: ActionKind::StoreTrue,
                    nargs: Nargs::One,
                    conv: Conv::Str,
                    default: ArgValue::Bool(false),
                    choices: None,
                    required: false,
                    help: None,
                    metavar: None,
                    version: None,
                },
                ActionSpec {
                    option_strings: vec!["-b"],
                    dest: "b",
                    kind: ActionKind::StoreTrue,
                    nargs: Nargs::One,
                    conv: Conv::Str,
                    default: ArgValue::Bool(false),
                    choices: None,
                    required: false,
                    help: None,
                    metavar: None,
                    version: None,
                },
                ActionSpec {
                    option_strings: vec!["-c"],
                    dest: "c",
                    kind: ActionKind::StoreTrue,
                    nargs: Nargs::One,
                    conv: Conv::Str,
                    default: ArgValue::Bool(false),
                    choices: None,
                    required: false,
                    help: None,
                    metavar: None,
                    version: None,
                },
                ActionSpec {
                    option_strings: vec!["-v"],
                    dest: "v",
                    kind: ActionKind::Count,
                    nargs: Nargs::One,
                    conv: Conv::Str,
                    default: ArgValue::Count(0),
                    choices: None,
                    required: false,
                    help: None,
                    metavar: None,
                    version: None,
                },
            ],
            mutex: vec![],
            sub: None,
        },
    );

    // P12: 綜合混合型
    m.insert(
        "P12",
        ParserSpec {
            prog: "p12".into(),
            description: None,
            epilog: None,
            raw_text: false,
            add_help: true,
            allow_abbrev: true,
            actions: vec![
                ActionSpec {
                    option_strings: vec!["--name"],
                    dest: "name",
                    kind: ActionKind::Store,
                    nargs: Nargs::One,
                    conv: Conv::Str,
                    default: ArgValue::None,
                    choices: None,
                    required: true,
                    help: None,
                    metavar: None,
                    version: None,
                },
                ActionSpec {
                    option_strings: vec!["-v"],
                    dest: "v",
                    kind: ActionKind::Count,
                    nargs: Nargs::One,
                    conv: Conv::Str,
                    default: ArgValue::Count(0),
                    choices: None,
                    required: false,
                    help: None,
                    metavar: None,
                    version: None,
                },
                ActionSpec {
                    option_strings: vec!["--count"],
                    dest: "count",
                    kind: ActionKind::Store,
                    nargs: Nargs::One,
                    conv: Conv::Int,
                    default: ArgValue::Int(PyInt::Small(1)),
                    choices: None,
                    required: false,
                    help: None,
                    metavar: None,
                    version: None,
                },
                ActionSpec {
                    option_strings: vec!["-f"],
                    dest: "f",
                    kind: ActionKind::StoreTrue,
                    nargs: Nargs::One,
                    conv: Conv::Str,
                    default: ArgValue::Bool(false),
                    choices: None,
                    required: false,
                    help: None,
                    metavar: None,
                    version: None,
                },
            ],
            mutex: vec![],
            sub: None,
        },
    );

    m
}

#[test]
fn test_argparse_synth_all_cases() {
    let rows = read_synth_tsv();
    assert_eq!(rows.len(), 120, "必須恰好有 120 筆合成測試案例");
    let specs = build_specs();

    let mut fails = Vec::new();

    for row in &rows {
        let spec = specs
            .get(row.id.as_str())
            .unwrap_or_else(|| panic!("找不到規格: {}", row.id));

        let argv_val = pyjson::loads(&row.input).expect("input 必須為合規 JSON");
        let argv: Vec<String> = match argv_val {
            PyValue::List(l) => l
                .into_iter()
                .map(|item| match item {
                    PyValue::Str(s) => s,
                    _ => panic!("argv 必須為字串"),
                })
                .collect(),
            _ => panic!("input 必須為 list"),
        };

        let exp_val = pyjson::loads(&row.expected).expect("expected 必須為合規 JSON");
        let exp_dict = match &exp_val {
            PyValue::Dict(d) => d,
            _ => panic!("expected 必須為 dict"),
        };

        let exp_ok = match exp_dict.get("ok").unwrap() {
            PyValue::Bool(b) => *b,
            _ => panic!("ok 必須為 bool"),
        };
        let exp_rc = match exp_dict.get("rc").unwrap() {
            PyValue::Int(PyInt::Small(n)) => *n as i32,
            _ => panic!("rc 必須為 Small Int"),
        };

        let result = parse_args(spec, &argv, 78);

        if exp_ok {
            match result {
                ParseResult::Ok(ns) => {
                    // 比對 namespace 值
                    let exp_vals = match exp_dict.get("vals").unwrap() {
                        PyValue::Dict(d) => d,
                        _ => panic!("vals 必須為 dict"),
                    };
                    for (k, expected_v) in &exp_vals.0 {
                        let actual_v = ns.get(k);
                        let matched = match (expected_v, actual_v) {
                            (PyValue::Null, ArgValue::None) => true,
                            (PyValue::Bool(b1), ArgValue::Bool(b2)) => b1 == b2,
                            (PyValue::Str(s1), ArgValue::Str(s2)) => s1 == s2,
                            (PyValue::Int(PyInt::Small(n1)), ArgValue::Int(PyInt::Small(n2))) => {
                                n1 == n2
                            }
                            (PyValue::Int(PyInt::Small(n1)), ArgValue::Count(n2)) => {
                                *n1 as i64 == *n2
                            }
                            (PyValue::List(l1), ArgValue::List(l2)) => {
                                let l1_strs: Vec<&str> = l1
                                    .iter()
                                    .map(|v| match v {
                                        PyValue::Str(s) => s.as_str(),
                                        _ => panic!("預期 list 項目必須為 Str"),
                                    })
                                    .collect();
                                let l2_strs: Vec<&str> = l2.iter().map(|s| s.as_str()).collect();
                                l1_strs == l2_strs
                            }
                            _ => false,
                        };
                        if !matched {
                            fails.push(format!(
                                "{}/{} argv={argv:?}: dest '{k}' 不符 (got {actual_v:?}, want {expected_v:?})",
                                row.id, row.op
                            ));
                        }
                    }
                }
                ParseResult::Exit(x) => {
                    fails.push(format!(
                        "{}/{} argv={argv:?}: 預期成功，卻收到 Exit(code={}, stderr={})",
                        row.id, row.op, x.code, x.stderr
                    ));
                }
                ParseResult::Delegated => {
                    fails.push(format!(
                        "{}/{} argv={argv:?}: 預期成功，卻收到 Delegated",
                        row.id, row.op
                    ));
                }
            }
        } else {
            let exp_stdout = match exp_dict.get("stdout").unwrap() {
                PyValue::Str(s) => s.as_str(),
                _ => panic!("stdout 必須為 Str"),
            };
            let exp_stderr = match exp_dict.get("stderr").unwrap() {
                PyValue::Str(s) => s.as_str(),
                _ => panic!("stderr 必須為 Str"),
            };

            match result {
                ParseResult::Exit(x) => {
                    if (x.code, x.stdout.as_str(), x.stderr.as_str())
                        != (exp_rc, exp_stdout, exp_stderr)
                    {
                        fails.push(format!(
                            "{}/{} argv={argv:?}:\n--- got rc={}\n{}{}\n--- want rc={exp_rc}\n{exp_stdout}{exp_stderr}",
                            row.id, row.op, x.code, x.stdout, x.stderr
                        ));
                    }
                }
                ParseResult::Ok(_) => {
                    fails.push(format!(
                        "{}/{} argv={argv:?}: 預期失敗 (rc={exp_rc})，卻成功解析",
                        row.id, row.op
                    ));
                }
                ParseResult::Delegated => {
                    fails.push(format!(
                        "{}/{} argv={argv:?}: 預期失敗 (rc={exp_rc})，卻收到 Delegated",
                        row.id, row.op
                    ));
                }
            }
        }
    }

    assert!(fails.is_empty(), "{} 筆合成案例不符:\n{}", fails.len(), fails.join("\n"));
}
