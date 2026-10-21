mod common;
use sw_proto::pyjson::{self, CLI_STDOUT, DISK_STABLE, RPC_EARLY, RPC_WIRE};

#[test]
fn golden_cases() {
    let mut fails = Vec::new();
    for r in common::golden("pyjson.tsv") {
        let got = match r.op.as_str() {
            "loads_error" => match pyjson::loads(&r.input) {
                Err(pyjson::JsonError::Decode(e)) => e.to_string(),
                Err(pyjson::JsonError::Recursion) => "RECURSION".into(),
                Ok(_) => "OK".into(),
            },
            "float_repr" => pyjson::repr_float(f64::from_bits(u64::from_str_radix(&r.input, 16).unwrap())),
            op => {
                let o = match op {
                    "dumps:cli_stdout" => &CLI_STDOUT,
                    "dumps:rpc_wire" => &RPC_WIRE,
                    "dumps:rpc_early" => &RPC_EARLY,
                    "dumps:disk_stable" => &DISK_STABLE,
                    _ => panic!("{op}"),
                };
                pyjson::dumps(&pyjson::loads(&r.input).expect(&r.id), o)
            }
        };
        if got != r.expected {
            fails.push(format!("{} {}: got {:?} want {:?}", r.id, r.op, got, r.expected));
        }
    }
    assert!(fails.is_empty(), "{} 筆不符，前 20 筆：\n{}", fails.len(), fails[..fails.len().min(20)].join("\n"));
}

#[test]
fn lone_surrogate_becomes_replacement() {
    let v = pyjson::loads(r#""\ud800x""#).unwrap();
    assert_eq!(pyjson::dumps(&v, &RPC_WIRE), "\"\u{FFFD}x\"");
}

#[test]
fn duplicate_key_keeps_first_position_last_value() {
    let v = pyjson::loads(r#"{"a":1,"b":2,"a":3}"#).unwrap();
    assert_eq!(pyjson::dumps(&v, &RPC_WIRE), r#"{"a":3,"b":2}"#);
}

#[test]
fn rpc_early_exact_bytes() {
    let d = sw_proto::pydict! { "ok" => false, "error_code" => "INVALID_JSON" };
    assert_eq!(pyjson::dumps(&pyjson::PyValue::Dict(d), &RPC_EARLY), r#"{"ok": false, "error_code": "INVALID_JSON"}"#);
}

#[test]
fn non_finite_repr_vs_dumps() {
    assert_eq!(pyjson::repr_float(f64::NAN), "nan");
    assert_eq!(pyjson::repr_float(f64::NEG_INFINITY), "-inf");
    assert_eq!(pyjson::dumps(&pyjson::PyValue::Float(f64::INFINITY), &RPC_WIRE), "Infinity");
}

#[test]
fn depth_limit_is_iterative_and_bounded() {
    let deep = "[".repeat(100_000) + &"]".repeat(100_000);
    assert_eq!(pyjson::loads(&deep), Err(pyjson::JsonError::Recursion)); // 不得 stack overflow（Windows 主執行緒 1 MiB 堆疊）
}
