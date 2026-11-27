//! p279：原生 loopback TCP → ConsoleHub → actor／RPC → FakeSerial 與 WAL。
#![cfg(feature = "test-fakes")]
mod support;

use std::collections::BTreeMap;
use std::io::{Read, Write};
use std::net::TcpStream;
use std::sync::{Arc, Mutex};
use std::time::{Duration, Instant};

use support::*;
use sw_core::file_transfer::templates as xfer;
use sw_pal::fakes::serial::{FakeDevice, FakeWriteStep};
use sw_proto::pycompat::a2b_base64_lenient;
use sw_proto::pyjson::PyValue;

type RemoteFs = Arc<Mutex<BTreeMap<String, Vec<u8>>>>;

const TELNET_GREETING: &[u8] = &[
    0xff, 0xfb, 0x01, 0xff, 0xfb, 0x03, 0xff, 0xfd, 0x03, 0xff, 0xfb, 0x00,
];

fn attach(r: &ServiceRig) -> String {
    let response = r.call("session.console_attach", r#"{"selector":"COM0"}"#);
    assert_eq!(response.get("ok"), Some(&PyValue::Bool(true)), "{response:?}");
    response.get("endpoint").and_then(PyValue::as_str).unwrap().to_string()
}

fn connect(endpoint: &str) -> TcpStream {
    let (host, port) = endpoint.rsplit_once(':').expect("host:port");
    assert_eq!(host, "127.0.0.1");
    TcpStream::connect((host, port.parse::<u16>().unwrap())).expect("native TCP console")
}

fn greet(stream: &mut TcpStream) {
    stream.set_read_timeout(Some(Duration::from_secs(5))).unwrap();
    let mut greeting = [0u8; 12];
    stream.read_exact(&mut greeting).expect("Telnet greeting");
    assert_eq!(&greeting, TELNET_GREETING);
}

fn wait_for(what: &str, mut condition: impl FnMut() -> bool) {
    let deadline = Instant::now() + Duration::from_secs(10);
    while !condition() {
        assert!(Instant::now() < deadline, "等待逾時：{what}");
        std::thread::sleep(Duration::from_millis(5));
    }
}

fn human_wal_rows(r: &ServiceRig, payload_b64: &str) -> usize {
    r.wal
        .tail_raw_with_meta(None, Some("COM0"), 10_000)
        .expect("讀 WAL")
        .0
        .iter()
        .filter(|row| {
            row.get("dir").and_then(PyValue::as_str) == Some("TX")
                && row.get("source").and_then(PyValue::as_str).is_some_and(|s| s.starts_with("human:"))
                && row.get("payload_b64").and_then(PyValue::as_str) == Some(payload_b64)
        })
        .count()
}

fn words(command: &str) -> Vec<String> {
    command.split(' ').map(|word| word.replace('\'', "")).collect()
}

fn run_remote_shell(fs: &RemoteFs, command: &str) -> String {
    if command.contains("base64 -d 2>/dev/null") {
        return format!("{}\r\n", xfer::DECODER_B64_SENTINEL);
    }
    if command.contains("printf '%s' 'sw-probe' | base64 2>/dev/null") {
        return format!("{}\r\n", xfer::ENCODER_B64_SENTINEL);
    }
    let words = words(command);
    let mut fs = fs.lock().unwrap_or_else(|e| e.into_inner());
    match words.first().map(String::as_str) {
        Some("printf") if words.get(1).map(String::as_str) == Some("%s") => {
            let data = a2b_base64_lenient(words[2].as_bytes()).expect("chunk base64");
            let (op, path) = (words[6].as_str(), words[7].clone());
            if op == ">" {
                fs.insert(path, data);
            } else {
                fs.entry(path).or_default().extend_from_slice(&data);
            }
            format!("{}\r\n", xfer::CHUNK_SENTINEL)
        }
        Some("printf") => {
            let at = words.iter().position(|word| word == "<").expect("pull command");
            let data = fs.get(&words[at + 1]).cloned().unwrap_or_default();
            let body: String = xfer::b64encode(&data)
                .as_bytes()
                .chunks(76)
                .map(|chunk| format!("{}\r\n", String::from_utf8_lossy(chunk)))
                .collect();
            format!("{}\r\n{body}{}\r\n", xfer::SENTINEL_BEGIN, xfer::SENTINEL_END)
        }
        Some("md5sum") => {
            let data = fs.get(&words[1]).cloned().unwrap_or_default();
            format!("{}  {}\r\n{}\r\n", xfer::md5_hex(&data), words[1], xfer::MD5_SENTINEL)
        }
        Some("mv") => {
            let data = fs.remove(&words[1]).unwrap_or_default();
            fs.insert(words[2].clone(), data);
            format!("{}\r\n", xfer::MV_SENTINEL)
        }
        Some("rm") => {
            fs.remove(&words[2]);
            format!("{}\r\n", xfer::CLEANUP_SENTINEL)
        }
        _ => format!("sh: {command}: not found\r\n"),
    }
}

fn install_xfer_shell(device: &FakeDevice) -> RemoteFs {
    let fs: RemoteFs = Arc::new(Mutex::new(BTreeMap::new()));
    let shared = Arc::clone(&fs);
    device.on_tx_match(
        "SW_XFER_",
        Box::new(move |line| {
            let command = String::from_utf8_lossy(line).trim_end_matches(['\r', '\n']).to_string();
            format!("{}root@t:/# ", run_remote_shell(&shared, &command)).into_bytes()
        }),
    );
    fs
}

struct TempDir(std::path::PathBuf);
impl TempDir {
    fn new() -> Self {
        let path = std::env::temp_dir().join(format!("sw-p279-{}-{:?}", std::process::id(), std::thread::current().id()));
        let _ = std::fs::remove_dir_all(&path);
        std::fs::create_dir_all(&path).unwrap();
        Self(path)
    }
    fn path(&self, name: &str) -> String {
        self.0.join(name).to_string_lossy().into_owned()
    }
}
impl Drop for TempDir {
    fn drop(&mut self) {
        let _ = std::fs::remove_dir_all(&self.0);
    }
}

#[test]
fn p279_tcp_socket_selftest_and_arbitration() {
    let r = ServiceRig::with_native_tcp_console();
    let endpoint = attach(&r);
    let mut client = connect(&endpoint);
    greet(&mut client);
    wait_for("TCP client 登記", || {
        r.call("session.console_list", r#"{"selector":"COM0"}"#)
            .get("consoles")
            .and_then(PyValue::as_array)
            .is_some_and(|rows| rows.len() == 1)
    });
    client.write_all(b"k").expect("raw human key");
    wait_for("TCP key 到 FakeSerial", || r.device(REAL_A).tx_since(0).contains(&b'k'));

    let tested = r.call("session.self_test", r#"{"selector":"COM0"}"#);
    assert_eq!(tested.get("human_attached"), Some(&PyValue::Bool(true)), "{tested:?}");
    assert_eq!(tested.get("human_active"), Some(&PyValue::Bool(true)), "{tested:?}");

    let busy = r.call("session.interactive_open", r#"{"selector":"COM0","owner":"agent:p279"}"#);
    assert_eq!(busy.get("error_code"), Some(&PyValue::from("SESSION_INTERACTIVE_BUSY")), "{busy:?}");

    r.clock.advance(Duration::from_secs(61));
    let opened = r.call("session.interactive_open", r#"{"selector":"COM0","owner":"agent:p279-idle","timeout_s":30}"#);
    assert_eq!(opened.get("ok"), Some(&PyValue::Bool(true)), "idle human should soft-preempt: {opened:?}");
    assert_eq!(opened.get("soft_preempted"), Some(&PyValue::Bool(true)), "idle human uses existing soft preempt");
    let interactive_id = opened.get("interactive_id").and_then(PyValue::as_str).unwrap();
    let closed = r.call("session.interactive_close", &format!(r#"{{"interactive_id":"{interactive_id}"}}"#));
    assert_eq!(closed.get("ok"), Some(&PyValue::Bool(true)), "agent close restores TCP mirror: {closed:?}");
    let restored = r.call("session.self_test", r#"{"selector":"COM0"}"#);
    assert_eq!(restored.get("human_attached"), Some(&PyValue::Bool(true)), "close restores human lease: {restored:?}");
}

#[test]
fn p279_tcp_socket_eof_reconnect() {
    let r = ServiceRig::with_native_tcp_console();
    let endpoint = attach(&r);
    let mut a = connect(&endpoint);
    greet(&mut a);
    a.write_all(b"A").expect("A raw key");
    wait_for("A 到 UART/WAL", || human_wal_rows(&r, "QQ==") == 1);
    let _ = a.shutdown(std::net::Shutdown::Both);
    drop(a);
    wait_for("A EOF 後 console 移除", || {
        r.call("session.console_list", r#"{"selector":"COM0"}"#)
            .get("consoles")
            .and_then(PyValue::as_array)
            .is_some_and(|rows| rows.is_empty())
    });

    let mut b = connect(&endpoint);
    greet(&mut b);
    b.write_all(b"B").expect("B raw key");
    wait_for("B 重連後 raw bytes 到 UART/WAL", || human_wal_rows(&r, "Qg==") == 1);
    assert!(r.device(REAL_A).tx_since(0).contains(&b'B'));
}

#[test]
fn p279_tcp_socket_foreground_refresh_and_replay() {
    let r = ServiceRig::with_native_tcp_console();
    let endpoint = attach(&r);
    let mut client = connect(&endpoint);
    greet(&mut client);
    client.write_all(b"k").expect("initial raw key");
    wait_for("initial key 到 UART/WAL", || human_wal_rows(&r, "aw==") == 1);

    let device = r.device(REAL_A);
    let before = device.tx_since(0).len();
    let accepted = r.call(
        "command.submit",
        r#"{"selector":"COM0","cmd":"sleep p279","completion_regex":"P279_DONE","timeout_s":30}"#,
    );
    assert_eq!(accepted.get("ok"), Some(&PyValue::Bool(true)), "{accepted:?}");
    let cmd_id = accepted.get("cmd_id").and_then(PyValue::as_str).unwrap().to_string();
    wait_for("agent command running", || {
        r.call("command.get", &format!(r#"{{"cmd_id":"{cmd_id}"}}"#))
            .get("command")
            .and_then(|value| match value { PyValue::Dict(row) => Some(row), _ => None })
            .and_then(|row| row.get("status").and_then(PyValue::as_str).map(str::to_string))
            .as_deref() == Some("running")
    });

    client.write_all(b"x").expect("command 期間延後 human key");
    std::thread::sleep(Duration::from_millis(50));
    assert!(!device.tx_since(before).contains(&b'x'), "command 期間不得插入 human TX");
    let tested = r.call("session.self_test", r#"{"selector":"COM0"}"#);
    assert_eq!(tested.get("human_attached"), Some(&PyValue::Bool(true)), "{tested:?}");

    device.push_rx(b"P279_DONE\r\nroot@t:/# ");
    wait_for("agent command 完成且 deferred 回放", || {
        r.call("command.get", &format!(r#"{{"cmd_id":"{cmd_id}"}}"#))
            .get("command")
            .and_then(|value| match value { PyValue::Dict(row) => Some(row), _ => None })
            .and_then(|row| row.get("status").and_then(PyValue::as_str).map(str::to_string))
            .as_deref() == Some("done")
            && human_wal_rows(&r, "eA==") == 1
    });
}

#[test]
fn p279_tcp_rpc_source_policy() {
    let r = Arc::new(ServiceRig::with_native_tcp_console());
    let endpoint = attach(&r);
    let mut client = connect(&endpoint);
    greet(&mut client);
    client.write_all(b"k").expect("raw human key");
    wait_for("human key 到 UART/WAL", || human_wal_rows(&r, "aw==") == 1);

    let device = r.device(REAL_A);
    let writes_before = device.write_calls();
    let sequence_before = r.wal.current_seq();
    let accepted = r.call(
        "command.submit",
        r#"{"selector":"COM0","cmd":"echo must-not-send","source":"human"}"#,
    );
    assert_eq!(accepted.get("ok"), Some(&PyValue::Bool(true)), "{accepted:?}");
    let cmd_id = accepted.get("cmd_id").and_then(PyValue::as_str).unwrap().to_string();
    wait_for("bare human command 在執行 gate 被拒", || {
        r.call("command.get", &format!(r#"{{"cmd_id":"{cmd_id}"}}"#))
            .get("command")
            .and_then(|value| match value { PyValue::Dict(row) => Some(row), _ => None })
            .and_then(|row| row.get("status").and_then(PyValue::as_str).map(str::to_string))
            .as_deref() == Some("error")
    });
    let rejected = r.call("command.get", &format!(r#"{{"cmd_id":"{cmd_id}"}}"#));
    let row = rejected
        .get("command")
        .and_then(|value| match value { PyValue::Dict(row) => Some(row), _ => None })
        .expect("command row");
    assert_eq!(row.get("error_code"), Some(&PyValue::from("SESSION_INTERACTIVE_BUSY")), "{row:?}");
    assert!(row.get("interactive_session_id").and_then(PyValue::as_str).is_some());
    assert_eq!(device.write_calls(), writes_before, "bare human BUSY 零 UART TX");
    assert_eq!(r.wal.current_seq(), sequence_before, "bare human BUSY 不新增 WAL TX");

    let fs = install_xfer_shell(device);
    let temp = TempDir::new();
    let data: Vec<u8> = (0..97u32).map(|n| (n % 251) as u8).collect();
    let local = temp.path("upload.bin");
    std::fs::write(&local, &data).unwrap();
    let device = r.device(REAL_A);
    let before_push = device.tx_since(0).len();
    device.script_write(vec![FakeWriteStep::PauseBeforeSubmit]);
    let push_rig = Arc::clone(&r);
    let push_local = local.clone();
    let push = std::thread::spawn(move || {
        push_rig.call(
            "file.push",
            &format!(r#"{{"selector":"COM0","local_path":{},"remote_path":"/tmp/p279.bin","ack_mode":"none","source":"human"}}"#, json_string(&push_local)),
        )
    });
    assert!(device.wait_paused(Duration::from_secs(5)), "file.push 到達假串列提交點");
    client.write_all(b"x").expect("file.push 期間 human key");
    std::thread::sleep(Duration::from_millis(50));
    assert!(!device.tx_since(before_push).contains(&b'x'), "file.push 期間 suspend 不得把 human key 送入 UART");
    device.release_pause();
    let pushed = push.join().expect("file.push thread");
    assert_eq!(pushed.get("ok"), Some(&PyValue::Bool(true)), "file.push 仍沿 agent 路徑：{pushed:?}");
    assert_eq!(fs.lock().unwrap_or_else(|e| e.into_inner()).get("/tmp/p279.bin"), Some(&data));
    wait_for("file.push 結束後 human replay 到 UART/WAL", || human_wal_rows(&r, "eA==") == 1);
    assert_eq!(human_wal_rows(&r, "eA=="), 1, "file.push resume 恰回放一次");

    let downloaded = temp.path("download.bin");
    let pulled = r.call(
        "file.pull",
        &format!(r#"{{"selector":"COM0","remote_path":"/tmp/p279.bin","local_path":{},"source":"human"}}"#, json_string(&downloaded)),
    );
    assert_eq!(pulled.get("ok"), Some(&PyValue::Bool(true)), "file.pull 仍沿 agent 路徑：{pulled:?}");
    assert_eq!(std::fs::read(downloaded).unwrap(), data);

    let sessions = r.call("session.console_list", r#"{"selector":"COM0"}"#);
    assert_eq!(sessions.get("ok"), Some(&PyValue::Bool(true)));
    let final_self_test = r.call("session.self_test", r#"{"selector":"COM0"}"#);
    assert_eq!(final_self_test.get("human_attached"), Some(&PyValue::Bool(true)), "file.* 後恢復 human lease：{final_self_test:?}");
}
