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

/// S1X-b-2：請求端檔案內容保留孤立 surrogate（`pyfmt` 跳脫）並回報旗標；`loads` 的 RPC-H-03 行為不變。
#[test]
fn keep_lone_surrogates_escapes_and_flags() {
    let (v, lone) =
        pyjson::loads_keep_lone_surrogates(r#"{"k\udcff":["\ud800\u0041","\udc80"]}"#).unwrap();
    assert!(lone);
    let mut k = String::from("k");
    sw_proto::pyfmt::escape_surrogate(0xDCFF, &mut k);
    let mut a = String::new();
    sw_proto::pyfmt::escape_surrogate(0xD800, &mut a);
    a.push('A');
    let mut b = String::new();
    sw_proto::pyfmt::escape_surrogate(0xDC80, &mut b);
    let mut want = pyjson::PyDict::new();
    want.insert(&k, pyjson::PyValue::List(vec![a.into(), b.into()]));
    assert_eq!(v, pyjson::PyValue::Dict(want));

    let (v, lone) =
        pyjson::loads_keep_lone_surrogates(r#"{"e":"\ud83d\ude00","n":"\u0000"}"#).unwrap();
    assert!(!lone);
    assert_eq!(
        v,
        pyjson::loads(r#"{"e":"\ud83d\ude00","n":"\u0000"}"#).unwrap()
    );

    let v = pyjson::loads(r#""\udc80""#).unwrap();
    assert_eq!(pyjson::dumps(&v, &RPC_WIRE), "\"\u{FFFD}\"");
}

/// S1-2a65438-1：旗標只反映最終值樹；被重複鍵覆寫掉的孤立 surrogate 不計。
#[test]
fn keep_lone_surrogates_flag_follows_duplicate_key_overwrite() {
    for doc in [
        r#"{"lone":"\ud800","lone":"ok"}"#,
        r#"{"a":{"x":"\udc80"},"a":{"x":"ok"}}"#,
        r#"{"a":["\udc80"],"b":1,"a":[1]}"#,
        r#"{"o":{"a":"\udfff","a":2},"o":{"a":"\ud801","a":3}}"#,
        r#"{"a":"\udc80","a":"\u0000\udbb6\udc00"}"#,
    ] {
        let (v, lone) = pyjson::loads_keep_lone_surrogates(doc).unwrap();
        assert!(!lone, "doc={doc}");
        assert_eq!(v, pyjson::loads(doc).unwrap(), "doc={doc}");
    }
    for doc in [
        r#"{"a":"ok","a":"\udc80"}"#,
        r#"{"k\udcff":1,"k\udcff":2}"#,
        r#"{"a":"\ud800","b":"x","a":"y","c":"\udc81"}"#,
        r#"[{"a":"\udc80","a":1},"\ud800"]"#,
    ] {
        let (_, lone) = pyjson::loads_keep_lone_surrogates(doc).unwrap();
        assert!(lone, "doc={doc}");
    }
}

/// S1-f2a23b1-1：合法鍵「NUL＋U+FD800」與孤立 surrogate 鍵 `\ud800` 是不同的鍵（Python 3.12.3 `json.loads`
/// 保留兩者），去重不得合併；後者留在最終值樹，旗標為 `true`。只有合法鍵時旗標為 `false`、值同 `loads`。
#[test]
fn keep_lone_surrogates_keys_distinguish_legal_content_from_surrogates() {
    for (doc, keys) in [
        (r#"{"\u0000\udbb6\udc00":1,"\ud800":2}"#, 2),
        (r#"{"\ud800":2,"\u0000\udbb6\udc00":1}"#, 2),
        (r#"{"\u0000\udbb6\udc00":"\ud800","\ud800":"x"}"#, 2),
        (
            r#"{"\u0000\udbb6\udc00":1,"\ud800":2,"\u0000\udbb6\udc00":3,"\ud800":4}"#,
            2,
        ),
    ] {
        let (v, lone) = pyjson::loads_keep_lone_surrogates(doc).unwrap();
        assert!(lone, "doc={doc}");
        let pyjson::PyValue::Dict(dict) = v else {
            panic!("doc={doc} 應解析為 dict");
        };
        assert_eq!(dict.0.len(), keys, "doc={doc}");
    }
    let (v, lone) =
        pyjson::loads_keep_lone_surrogates(r#"{"o":{"\u0000\udbb6\udc00":1,"\ud800":2}}"#).unwrap();
    assert!(lone);
    let pyjson::PyValue::Dict(inner) = &v["o"] else {
        panic!("o 應解析為 dict");
    };
    assert_eq!(inner.0.len(), 2);

    for doc in [
        r#"{"\u0000\udbb6\udc00":1}"#,
        r#"{"\u0000\udbb6\udc00":1,"\u0000\udbb6\udc00":2}"#,
        r#"{"\u0000\udbb6\udc00":"\ud800","\u0000\udbb6\udc00":"ok"}"#,
        r#"{"\u0000":"\u0000\u0000","a":"\udc80","a":"\u0000"}"#,
        r#"["\u0000","\u0000\u0000",{"\u0000\u0000":"\\u0000"}]"#,
    ] {
        let (v, lone) = pyjson::loads_keep_lone_surrogates(doc).unwrap();
        assert!(!lone, "doc={doc}");
        assert_eq!(v, pyjson::loads(doc).unwrap(), "doc={doc}");
    }
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
