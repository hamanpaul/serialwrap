//! P8 Task 6：`file.push`／`file.pull` 的 daemon 級端到端（`Service::rpc` → `SessionManager::file_transfer` →
//! session actor → OpRunner → `OpTransferPort` → TxWriter → FakeDevice → RX → 引擎 → 回應）。
//!
//! 行為基準＝origin/main `sw_core/service.py` 的 `file.push`／`file.pull` 分支（轉型、`check_source`、
//! `_annotate_source`）與 `session_manager.py::file_push`／`file_pull`。DUT 以 FakeDevice 的逐行回應扮演 busybox
//! shell（decoder／encoder probe、chunk 解碼、md5sum、mv、pull 編碼）；外部證據取自裝置實際寫出與其虛擬檔案系統。
#![cfg(feature = "test-fakes")]

mod support;

use std::collections::BTreeMap;
use std::sync::{Arc, Mutex};

use support::*;
use sw_core::file_transfer::templates as t;
use sw_pal::fakes::serial::FakeDevice;
use sw_proto::pycompat::a2b_base64_lenient;
use sw_proto::pyjson::{self, PyDict, PyInt, PyValue};

type Fs = Arc<Mutex<BTreeMap<String, Vec<u8>>>>;

fn keys(d: &PyDict) -> Vec<&str> {
    d.0.keys().map(String::as_str).collect()
}

fn wire(d: &PyDict) -> String {
    pyjson::dumps(&PyValue::Dict(d.clone()), &pyjson::RPC_WIRE)
}

/// 去掉 shell 單引號的詞（測試路徑與 base64 皆不含空白）。
fn words(cmd: &str) -> Vec<String> {
    cmd.split(' ').map(|w| w.replace('\'', "")).collect()
}

/// busybox shell 的最小解譯（只認 T3 樣板）；回傳 prompt 之前的輸出。
fn mini_shell(fs: &Fs, cmd: &str) -> String {
    if cmd == t::decoder_probe_command() {
        return format!("{}\r\n", t::DECODER_B64_SENTINEL);
    }
    if cmd == t::encoder_probe_command() {
        return format!("{}\r\n", t::ENCODER_B64_SENTINEL);
    }
    let w = words(cmd);
    let mut fs = fs.lock().unwrap();
    match w.first().map(String::as_str) {
        Some("printf") if w.get(1).map(String::as_str) == Some("%s") => {
            // printf '%s' <b64> | base64 -d <op> <path> && ...
            let data = a2b_base64_lenient(w[2].as_bytes()).expect("chunk base64");
            let (op, path) = (w[6].as_str(), w[7].clone());
            if op == ">" {
                fs.insert(path, data);
            } else {
                fs.entry(path).or_default().extend_from_slice(&data);
            }
            format!("{}\r\n", t::CHUNK_SENTINEL)
        }
        Some("printf") => {
            // pull：printf '%s\n' <BEGIN> && base64 < <path> && printf '%s\n' <END> || ...
            let at = w.iter().position(|x| x == "<").expect("pull 命令含 <");
            let data = fs.get(&w[at + 1]).cloned().unwrap_or_default();
            let b64 = t::b64encode(&data);
            let body: String = b64
                .as_bytes()
                .chunks(76)
                .map(|c| format!("{}\r\n", String::from_utf8_lossy(c)))
                .collect();
            format!("{}\r\n{body}{}\r\n", t::SENTINEL_BEGIN, t::SENTINEL_END)
        }
        Some("md5sum") => {
            let data = fs.get(&w[1]).cloned().unwrap_or_default();
            format!("{}  {}\r\n{}\r\n", t::md5_hex(&data), w[1], t::MD5_SENTINEL)
        }
        Some("mv") => {
            let data = fs.remove(&w[1]).unwrap_or_default();
            fs.insert(w[2].clone(), data);
            format!("{}\r\n", t::MV_SENTINEL)
        }
        Some("rm") => {
            fs.remove(&w[2]);
            format!("{}\r\n", t::CLEANUP_SENTINEL)
        }
        _ => format!("sh: {cmd}: not found\r\n"),
    }
}

/// 對含 `SW_XFER_` 的每個已確認 TX 行回應輸出與 prompt。
fn install_xfer_shell(dev: &FakeDevice) -> Fs {
    let fs: Fs = Arc::new(Mutex::new(BTreeMap::new()));
    let f = Arc::clone(&fs);
    dev.on_tx_match(
        "SW_XFER_",
        Box::new(move |line: &[u8]| {
            let cmd = String::from_utf8_lossy(line).trim_end_matches(['\r', '\n']).to_string();
            format!("{}root@t:/# ", mini_shell(&f, &cmd)).into_bytes()
        }),
    );
    fs
}

/// 裝置實際收到的完整 TX 行。
fn dut_lines(dev: &FakeDevice) -> Vec<String> {
    let tx = dev.tx_since(0);
    String::from_utf8_lossy(&tx).split('\n').map(str::to_string).collect()
}

struct Tmp(std::path::PathBuf);
impl Tmp {
    fn new() -> Tmp {
        let p = std::env::temp_dir().join(format!("sw-p8t6-rpc-{}-{:?}", std::process::id(), std::thread::current().id()));
        let _ = std::fs::remove_dir_all(&p);
        std::fs::create_dir_all(&p).unwrap();
        Tmp(p)
    }
    fn path(&self, name: &str) -> String {
        self.0.join(name).to_string_lossy().into_owned()
    }
}
impl Drop for Tmp {
    fn drop(&mut self) {
        let _ = std::fs::remove_dir_all(&self.0);
    }
}

#[test]
fn file_push_and_pull_rpc_end_to_end() {
    let rig = ServiceRig::ready_shell();
    let dev = rig.device(REAL_A);
    let fs = install_xfer_shell(dev);
    let tmp = Tmp::new();
    let data: Vec<u8> = (0..700u32).map(|i| (i % 253) as u8).collect();
    let local = tmp.path("src.bin");
    std::fs::write(&local, &data).unwrap();

    // bare human 依 OD-74 正規化為 human:cli；成功回應附 source／source_normalized_from（`_annotate_source`）。
    let r = rig.call(
        "file.push",
        &format!(r#"{{"selector":"COM0","local_path":"{local}","remote_path":"/tmp/r.bin","ack_mode":"none","source":"human"}}"#),
    );
    assert_eq!(
        keys(&r),
        ["ok", "bytes", "chunks", "md5", "remote_path", "source", "source_normalized_from", "version"],
        "{}",
        wire(&r)
    );
    assert_eq!(r.get("ok"), Some(&PyValue::Bool(true)));
    assert_eq!(r.get("bytes"), Some(&PyValue::Int(PyInt::Small(700))));
    assert_eq!(r.get("chunks"), Some(&PyValue::Int(PyInt::Small(2))));
    assert_eq!(r.get("md5"), Some(&PyValue::from(t::md5_hex(&data))));
    assert_eq!(r.get("source"), Some(&PyValue::from("human:cli")));
    assert_eq!(r.get("source_normalized_from"), Some(&PyValue::from("human")));
    assert_eq!(fs.lock().unwrap().get("/tmp/r.bin"), Some(&data), "外部 oracle：DUT 端檔案內容");
    let chunk_lines = dut_lines(dev).into_iter().filter(|l| l.contains("SW_XFER_CHUNK_")).count();
    assert_eq!(chunk_lines, 2, "DUT 收到的 chunk 命令恰 2 行（不重送）");
    let wal_bytes = rig.wfs.contents(&rig.wal.wal_path()).unwrap_or_default();
    let tx_ids: Vec<String> = String::from_utf8_lossy(&wal_bytes)
        .lines()
        .filter_map(|l| match pyjson::loads(l) {
            Ok(PyValue::Dict(row)) => Some(row),
            _ => None,
        })
        .filter(|row| row.get("dir").and_then(PyValue::as_str) == Some("TX"))
        .filter_map(|row| row.get("cmd_id").and_then(PyValue::as_str).map(str::to_string))
        .collect();
    assert!(tx_ids.iter().any(|id| id.starts_with("ft-") && id.ends_with("-1")), "{tx_ids:?}");

    // pull：未帶 source 時不附來源標註。
    let out = tmp.path("back.bin");
    let r = rig.call("file.pull", &format!(r#"{{"selector":"COM0","remote_path":"/tmp/r.bin","local_path":"{out}"}}"#));
    assert_eq!(keys(&r), ["ok", "bytes", "md5", "local_path", "version"], "{}", wire(&r));
    assert_eq!(r.get("local_path"), Some(&PyValue::from(out.clone())));
    assert_eq!(std::fs::read(&out).unwrap(), data);
    let s = rig.session("COM0").expect("COM0");
    assert_eq!(s.get("foreground_busy"), Some(&PyValue::Bool(false)), "傳輸結束後釋放前景槽");
}

#[test]
fn file_rpc_rejections_have_no_side_effects() {
    let rig = ServiceRig::ready_shell();
    let before = rig.side_effect_fingerprint();
    let v = &rig.version;
    for m in ["file.push", "file.pull"] {
        assert_eq!(
            wire(&rig.call(m, "{}")),
            format!(r#"{{"ok":false,"error_code":"INVALID_ARGS","version":"{v}"}}"#),
            "{m}"
        );
        let r = rig.call(m, r#"{"selector":"COM0","local_path":"/l","remote_path":"/r","source":"system:recover"}"#);
        assert_eq!(keys(&r), ["ok", "error_code", "field", "value", "reserved_prefix", "hint", "version"], "{m}");
        assert_eq!(r.get("error_code"), Some(&PyValue::from("SOURCE_RESERVED")));
        assert_eq!(
            wire(&rig.call(m, r#"{"selector":"COM9","local_path":"/l","remote_path":"/r"}"#)),
            format!(r#"{{"ok":false,"error_code":"SESSION_NOT_FOUND","selector":"COM9","version":"{v}"}}"#),
            "{m}"
        );
        assert_eq!(
            wire(&rig.call(m, r#"{"selector":"COM0","local_path":"/l","remote_path":"/r","ack_mode":"ehco"}"#)),
            format!(r#"{{"ok":false,"error_code":"INVALID_ARGS","version":"{v}"}}"#),
            "{m}"
        );
    }
    assert_eq!(rig.side_effect_fingerprint(), before, "拒絕路徑不得碰 WAL／state／UART");
}
