//! Slice 2.1 T0c 上游介面補齊：以 Python 產生的 golden 鎖定相容行為。

#![allow(unused_imports)]

use std::collections::BTreeMap;

use sw_proto::config_types::RuntimeConfig;
use sw_proto::pycompat::{
    a2b_base64_lenient, b2a_base64, clean_text, coerce_rpc_bool, iso_utc_now, py_round,
    shlex_quote, shlex_split, strftime_local, strftime_utc, strip_ansi, to_printable, uuid4_dashed,
    uuid4_hex, PyErr, ShlexError,
};
use sw_proto::pyjson::{loads, PyDict, PyInt, PyValue};
use sw_proto::rpc_types::Params;
use sw_proto::runtime_paths::{resolve_daemon_dirs, DaemonDirs, EnvSnapshot};
use sw_proto::wal_types::crc32_hex;

const GOLDEN: &str = include_str!("../../../contracts/golden/pycompat_s21.tsv");
const SEP: char = '\u{1f}';

fn decode64(input: &str) -> Vec<u8> {
    fn val(byte: u8) -> Option<u8> {
        match byte {
            b'A'..=b'Z' => Some(byte - b'A'),
            b'a'..=b'z' => Some(byte - b'a' + 26),
            b'0'..=b'9' => Some(byte - b'0' + 52),
            b'+' => Some(62),
            b'/' => Some(63),
            _ => None,
        }
    }
    let mut out = Vec::new();
    let mut acc = 0u32;
    let mut bits = 0u8;
    for byte in input.bytes() {
        if byte == b'=' {
            break;
        }
        if let Some(digit) = val(byte) {
            acc = (acc << 6) | u32::from(digit);
            bits += 6;
            if bits >= 8 {
                bits -= 8;
                out.push((acc >> bits) as u8);
                acc &= (1 << bits) - 1;
            }
        }
    }
    out
}

fn decoded(s: &str) -> String {
    String::from_utf8(decode64(s)).expect("golden 欄位必須是 UTF-8")
}

fn from_hex(s: &str) -> Vec<u8> {
    assert_eq!(s.len() % 2, 0, "hex 長度必須為偶數");
    s.as_bytes()
        .chunks_exact(2)
        .map(|pair| {
            let hi = (pair[0] as char).to_digit(16).expect("hex digit");
            let lo = (pair[1] as char).to_digit(16).expect("hex digit");
            ((hi << 4) | lo) as u8
        })
        .collect()
}

fn hex_bytes(bytes: &[u8]) -> String {
    bytes.iter().map(|byte| format!("{byte:02x}")).collect()
}

#[test]
fn pycompat_golden() {
    let mut count = 0usize;
    for (line_no, line) in GOLDEN.lines().enumerate() {
        let fields: Vec<_> = line.split('\t').collect();
        assert_eq!(fields.len(), 3, "golden 第 {} 行欄位數", line_no + 1);
        let input = decoded(fields[1]);
        let expected = decoded(fields[2]);
        match fields[0] {
            "iso_utc_now" => assert_eq!(iso_utc_now(input.parse().unwrap()), expected),
            "strftime_utc" => {
                let (us, fmt) = input.split_once(SEP).unwrap();
                assert_eq!(strftime_utc(us.parse().unwrap(), fmt), expected);
            }
            "strftime_local" => {
                let mut parts = input.split(SEP);
                let us: i64 = parts.next().unwrap().parse().unwrap();
                let offset: i32 = parts.next().unwrap().parse().unwrap();
                let fmt = parts.next().unwrap();
                assert_eq!(strftime_local(us, offset, fmt), expected);
            }
            "to_printable" => assert_eq!(to_printable(&from_hex(&input)), expected),
            "clean_text" => assert_eq!(clean_text(&input), expected),
            "strip_ansi" => assert_eq!(strip_ansi(&input), expected),
            "a2b_base64_lenient" => {
                let actual = match a2b_base64_lenient(input.as_bytes()) {
                    Ok(bytes) => hex_bytes(&bytes),
                    Err(error) => format!("ERR:{error}"),
                };
                assert_eq!(actual, expected);
            }
            "b2a_base64" => assert_eq!(b2a_base64(&from_hex(&input)), expected),
            "shlex_quote" => assert_eq!(shlex_quote(&input), expected),
            "shlex_split" => match shlex_split(&input) {
                Ok(tokens) => {
                    let expected = match loads(&expected).unwrap() {
                        PyValue::List(items) => items
                            .into_iter()
                            .map(|v| match v {
                                PyValue::Str(s) => s,
                                other => panic!("非字串 token: {other:?}"),
                            })
                            .collect::<Vec<_>>(),
                        other => panic!("shlex golden 不是 list: {other:?}"),
                    };
                    assert_eq!(tokens, expected)
                }
                Err(_e) => assert_eq!(expected, "ERR"),
            },
            "coerce_rpc_bool" => {
                let normalized = input.to_ascii_lowercase();
                let value = if !matches!(normalized.as_str(), "true" | "false" | "null" | "0") {
                    PyValue::Str(input)
                } else {
                    loads(&normalized).unwrap()
                };
                assert_eq!(coerce_rpc_bool(&value), expected == "true");
            }
            "py_round" => {
                let (number, digits) = input.split_once(SEP).unwrap();
                assert_eq!(
                    py_round(number.parse().unwrap(), digits.parse().unwrap()),
                    expected.parse::<f64>().unwrap()
                );
            }
            "uuid4_hex" | "uuid4_dashed" => {
                let seed = from_hex(&input);
                let mut rng = |out: &mut [u8]| out.copy_from_slice(&seed[..out.len()]);
                let actual = if fields[0] == "uuid4_hex" {
                    uuid4_hex(&mut rng)
                } else {
                    uuid4_dashed(&mut rng)
                };
                assert_eq!(actual, expected);
            }
            unknown => panic!("unknown golden op on line {}: {unknown}", line_no + 1),
        }
        count += 1;
    }
    assert!(count >= 300, "golden 至少 300 列，實際 {count}");
}

#[test]
fn params_coercion_golden() {
    let params = PyDict::from_pairs(vec![
        ("selector".into(), PyValue::Str("COM0".into())),
        ("session_id".into(), PyValue::Str("profile:COM0".into())),
        ("com".into(), PyValue::Str("COM1".into())),
        ("alias".into(), PyValue::Str("board".into())),
        ("zero".into(), PyValue::Int(PyInt::Small(0))),
        ("truthy_false_text".into(), PyValue::Str("false".into())),
        ("float".into(), PyValue::Float(3.9)),
        (
            "above_i64".into(),
            PyValue::Int(PyInt::Small(i64::MAX as i128 + 1)),
        ),
        (
            "below_i64".into(),
            PyValue::Int(PyInt::Small(i64::MIN as i128 - 1)),
        ),
        (
            "above_i64_str".into(),
            PyValue::Str("9223372036854775808".into()),
        ),
        (
            "below_i64_str".into(),
            PyValue::Str("-9223372036854775809".into()),
        ),
        (
            "above_i64_float".into(),
            PyValue::Float(9_223_372_036_854_775_808.0),
        ),
        (
            "below_i64_float".into(),
            PyValue::Float(-9_223_372_036_854_777_856.0),
        ),
        ("string".into(), PyValue::Str("value".into())),
    ]);
    let p = Params::new(&params);
    assert_eq!(p.str_or("string", "default").unwrap(), "value");
    assert_eq!(p.str_or("zero", "default").unwrap(), "default");
    assert_eq!(p.str_or("missing", "default").unwrap(), "default");
    assert_eq!(p.int_or("float", 9).unwrap(), 3);
    assert_eq!(p.int_or("zero", 9).unwrap(), 9);
    assert_eq!(
        p.int_or("above_i64", 0).unwrap().to_string(),
        "9223372036854775808"
    );
    assert_eq!(
        p.int_or("below_i64", 0).unwrap().to_string(),
        "-9223372036854775809"
    );
    assert_eq!(
        p.int_or("above_i64_str", 0).unwrap().to_string(),
        "9223372036854775808"
    );
    assert_eq!(
        p.int_or("below_i64_str", 0).unwrap().to_string(),
        "-9223372036854775809"
    );
    assert_eq!(
        p.int_or("above_i64_float", 0).unwrap().to_string(),
        "9223372036854775808"
    );
    assert_eq!(
        p.int_or("below_i64_float", 0).unwrap().to_string(),
        "-9223372036854777856"
    );
    assert_eq!(p.float_or("zero", 2.5).unwrap(), 2.5);
    assert_eq!(p.float_or("float", 0.0).unwrap(), 3.9);
    assert!(p.truthy("truthy_false_text"));
    assert!(matches!(p.raw("string"), Some(PyValue::Str(s)) if s == "value"));
    assert_eq!(
        p.selector_chain(),
        vec!["COM0", "profile:COM0", "COM1", "board"]
    );
}

#[test]
fn runtime_config_accessors() {
    let bad_excludes = RuntimeConfig::from_text(Some("windows:\n  exclude_coms: nope\n")).unwrap();
    assert!(bad_excludes.windows_exclude_coms().is_empty());
    let bad_allowlist =
        RuntimeConfig::from_text(Some("windows:\n  test_device_allowlist: nope\n")).unwrap();
    assert_eq!(bad_allowlist.windows_test_device_allowlist(), Err(()));
    let mixed_allowlist =
        RuntimeConfig::from_text(Some("windows:\n  test_device_allowlist: [COM20, 5]\n")).unwrap();
    assert_eq!(mixed_allowlist.windows_test_device_allowlist(), Err(()));

    let valid = RuntimeConfig::from_text(Some(
        "windows:\n  exclude_coms: [COM1, 5, COM1]\n  test_device_allowlist: [COM20, ' COM21 ', COM20, ' ']\n",
    ))
    .unwrap();
    assert_eq!(valid.windows_exclude_coms(), vec!["COM1", "5"]);
    assert_eq!(
        valid.windows_test_device_allowlist(),
        Ok(Some(vec!["COM20".to_string(), "COM21".to_string()]))
    );
    let missing = RuntimeConfig::from_text(None).unwrap();
    assert_eq!(missing.windows_test_device_allowlist(), Ok(None));
}

#[test]
fn daemon_dirs_defaults() {
    let env = EnvSnapshot::from_pairs(BTreeMap::<String, String>::new());
    let dirs: DaemonDirs = resolve_daemon_dirs(&env);
    assert_eq!(dirs.by_id_dir, "/dev/serial/by-id");
    assert_eq!(dirs.by_path_dir, "/dev/serial/by-path");
    assert_eq!(dirs.events_dir, "~/.serialwrap/events.d");
    assert_eq!(dirs.events_runtime_dir, "~/.local/state/serialwrap/events");

    let custom = EnvSnapshot::from_pairs(BTreeMap::from([
        ("HOME".to_string(), "/home/tester".to_string()),
        ("XDG_STATE_HOME".to_string(), "/state".to_string()),
        ("SERIALWRAP_BY_ID_DIR".to_string(), "~/by-id".to_string()),
        ("SERIALWRAP_EVENTS_RUNTIME_DIR".to_string(), "".to_string()),
    ]));
    let dirs = resolve_daemon_dirs(&custom);
    assert_eq!(dirs.by_id_dir, "/home/tester/by-id");
    assert_eq!(dirs.events_dir, "/home/tester/.serialwrap/events.d");
    assert_eq!(dirs.events_runtime_dir, "/state/serialwrap/events");
}

#[test]
fn crc32_hex_matches_zlib() {
    assert_eq!(crc32_hex(b"hello\r\n\x1b[0m\x0b\x7f\xff"), "a81bc28c");
}

#[test]
fn pyerr_value_error_has_python_message() {
    let error = PyErr::value_error("bad value");
    assert_eq!(error.message, "bad value");
    assert_eq!(error.to_string(), "bad value");
    let _: Option<ShlexError> = None;
}
