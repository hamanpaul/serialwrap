//! P8-T3：檔案傳輸板端命令樣板與 RX 判定 helper 對 Python golden 的逐列比對。
//!
//! golden 由 `scripts/rust/capture_transfer_golden.py` 以 Python 基線（sw_core.file_transfer）產生。
use std::collections::BTreeMap;

use sw_core::file_transfer::{rx_match as m, templates as t};
use sw_proto::pyjson::{self, PyInt, PyValue};

fn st(v: &PyValue) -> &str {
    v.as_str().expect("str")
}
fn opt(v: &PyValue) -> Option<&str> {
    match v {
        PyValue::Null => None,
        x => Some(st(x)),
    }
}
fn list(v: &PyValue) -> &[PyValue] {
    match v {
        PyValue::List(l) => l,
        other => panic!("expected list: {other:?}"),
    }
}
fn int(v: &PyValue) -> i128 {
    match v {
        PyValue::Int(PyInt::Small(n)) => *n,
        other => panic!("expected int: {other:?}"),
    }
}
fn hex_decode(s: &str) -> Vec<u8> {
    assert!(s.len() % 2 == 0, "odd hex");
    (0..s.len()).step_by(2).map(|i| u8::from_str_radix(&s[i..i + 2], 16).unwrap()).collect()
}
fn hex_encode(b: &[u8]) -> String {
    b.iter().map(|x| format!("{x:02x}")).collect()
}
fn verr<T>(r: Result<String, T>) -> PyValue {
    PyValue::Str(r.unwrap_or_else(|_| "<ValueError>".into()))
}

#[test]
fn every_golden_row_matches_python() {
    let mut n = 0usize;
    let mut per_fn: BTreeMap<String, usize> = BTreeMap::new();
    for line in include_str!("../../../contracts/golden/file_transfer.ndjson").lines().filter(|l| !l.is_empty()) {
        let PyValue::Dict(rec) = pyjson::loads(line).unwrap() else { panic!("not a dict") };
        let f = st(rec.get("fn").unwrap()).to_string();
        let a = list(rec.get("args").unwrap());
        let want = rec.get("out").unwrap();
        let got = match f.as_str() {
            "constants" => {
                // 以 want 的欄位逐一核對；組成同形 PyValue 比較。
                let PyValue::Dict(w) = want else { panic!() };
                let s = |k: &str, v: &str| assert_eq!(st(w.get(k).unwrap()), v, "const {k}");
                s("SENTINEL_BEGIN", t::SENTINEL_BEGIN);
                s("SENTINEL_END", t::SENTINEL_END);
                s("TOOL_PROBE_PAYLOAD", t::TOOL_PROBE_PAYLOAD);
                s("DECODER_B64", t::DECODER_B64_SENTINEL);
                s("DECODER_OPENSSL", t::DECODER_OPENSSL_SENTINEL);
                s("ENCODER_B64", t::ENCODER_B64_SENTINEL);
                s("ENCODER_OPENSSL", t::ENCODER_OPENSSL_SENTINEL);
                s("CHUNK", t::CHUNK_SENTINEL);
                s("MD5", t::MD5_SENTINEL);
                s("MV", t::MV_SENTINEL);
                s("CLEANUP", t::CLEANUP_SENTINEL);
                s("ENCODER_FAILURE", t::ENCODER_FAILURE_SENTINEL);
                let strs = |k: &str| list(w.get(k).unwrap()).iter().map(|x| st(x).to_string()).collect::<Vec<_>>();
                assert_eq!(strs("DECODERS"), t::DECODERS);
                assert_eq!(strs("ENCODERS"), t::ENCODERS);
                assert_eq!(strs("ACK_MODES"), t::ACK_MODES);
                assert_eq!(int(w.get("DEFAULT_CHUNK_SIZE").unwrap()), t::DEFAULT_CHUNK_SIZE);
                assert_eq!(int(w.get("DEFAULT_ECHO_SLICE_SIZE").unwrap()), t::DEFAULT_ECHO_SLICE_SIZE as i128);
                assert_eq!(w.get("DEFAULT_ECHO_TIMEOUT_S").unwrap(), &PyValue::Float(t::DEFAULT_ECHO_TIMEOUT_S));
                want.clone()
            }
            "runtime_sentinel" => PyValue::Str(t::runtime_sentinel(st(&a[0]))),
            "marker_expression" => verr(t::marker_expression(st(&a[0]))),
            "decoder_probe_command" => PyValue::Str(t::decoder_probe_command()),
            "encoder_probe_command" => PyValue::Str(t::encoder_probe_command()),
            "decoder_command_name" => verr(t::decoder_command_name(st(&a[0])).map(String::from)),
            "encoder_command_name" => verr(t::encoder_command_name(st(&a[0])).map(String::from)),
            "chunk_command" => verr(t::chunk_command(st(&a[0]), st(&a[1]), st(&a[2]), st(&a[3]))),
            "md5_command" => PyValue::Str(t::md5_command(st(&a[0]))),
            "mv_command" => PyValue::Str(t::mv_command(st(&a[0]), st(&a[1]))),
            "cleanup_command" => PyValue::Str(t::cleanup_command(st(&a[0]))),
            "pull_command" => verr(t::pull_command(st(&a[0]), st(&a[1]))),
            "b64encode" => PyValue::Str(t::b64encode(&hex_decode(st(&a[0])))),
            "md5_hex" => PyValue::Str(t::md5_hex(&hex_decode(st(&a[0])))),
            "split_chunks" => PyValue::List(
                t::split_chunks(&hex_decode(st(&a[0])), int(&a[1]) as usize)
                    .iter()
                    .map(|c| PyValue::Str(hex_encode(c)))
                    .collect(),
            ),
            "line_budget_exceeded" => {
                let cmds: Vec<&str> = list(&a[0]).iter().map(st).collect();
                let budget = match &a[1] {
                    PyValue::Null => None,
                    v => Some(int(v)),
                };
                PyValue::Bool(t::line_budget_exceeded(&cmds, budget))
            }
            "strip_command_echo" => PyValue::Str(m::strip_command_echo(st(&a[0]), opt(&a[1])).to_string()),
            "runtime_sentinel_seen" => PyValue::Bool(m::runtime_sentinel_seen(st(&a[0]), st(&a[1]), opt(&a[2]))),
            "failure_sentinel_seen" => PyValue::Bool(m::failure_sentinel_seen(st(&a[0]), st(&a[1]), opt(&a[2]))),
            "probe_shell_error" => PyValue::Bool(m::probe_shell_error(st(&a[0]), st(&a[1]))),
            "extract_between_sentinels" => {
                m::extract_between_sentinels(st(&a[0]), opt(&a[1])).map(PyValue::Str).unwrap_or(PyValue::Null)
            }
            "remote_md5_from_output" => {
                m::remote_md5_from_output(st(&a[0]), st(&a[1])).map(PyValue::Str).unwrap_or(PyValue::Null)
            }
            other => panic!("golden 出現未處理的函式 {other}"),
        };
        assert_eq!(&got, want, "fn={f} line={line}");
        *per_fn.entry(f).or_default() += 1;
        n += 1;
    }
    assert!(n >= 600, "golden 只有 {n} 筆");
    for (f, c) in &per_fn {
        assert!(*c >= 3 || f == "constants" || f.ends_with("_probe_command"), "{f} 只有 {c} 列");
    }
}

#[test]
fn chunk_template_lengths_match_inventory() {
    let tmp = "/tmp/.sw_upload_0123456789ab"; // 28 字元
    assert_eq!(t::chunk_command("", "base64", ">", tmp).unwrap().len(), 99);
    assert_eq!(t::chunk_command("", "openssl", ">>", tmp).unwrap().len(), 116);
    assert_eq!(t::chunk_command("", "bogus", ">", tmp), Err(t::TemplateError::UnknownTool("bogus".into())));
    assert_eq!(t::decoder_probe_command().len(), 282);
    assert_eq!(t::encoder_probe_command().len(), 273);
}

#[test]
fn split_chunks_empty_file_yields_one_empty_chunk() {
    assert_eq!(t::split_chunks(b"", 512), vec![&b""[..]]);
    assert_eq!(t::split_chunks(&[0u8; 1025], 512).iter().map(|c| c.len()).collect::<Vec<_>>(), vec![512, 512, 1]);
}

#[test]
fn b64encode_has_no_trailing_newline() {
    assert_eq!(t::b64encode(b"sw-probe"), t::TOOL_PROBE_PAYLOAD);
    assert_eq!(t::b64encode(b""), "");
}
