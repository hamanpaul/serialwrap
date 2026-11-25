//! issue 312 回到 TX 寫出時的 session 密碼窗口。
#![cfg(feature = "test-fakes")]

mod support;

use std::fs::{self, OpenOptions};
use std::io::{Read, Write};
use std::path::{Path, PathBuf};
use std::process::{Command, Stdio};
use std::sync::Arc;
use std::time::{Duration, Instant};

use support::{RigSpec, ServiceRig, BY_ID_A, REAL_A};
use sw_core::event_engine::{BridgeQueries, EngineDeps, EventEngine};
use sw_pal::fakes::serial::FakeWriteStep;
use sw_pal::fakes::{FakeClock, FakeEnv, FakeHandlerSpawner, FakeSerialBackend};
use sw_pal::traits::{Clock, HandlerSpawner, SerialBackend, SysEnv};
use sw_proto::pycompat::a2b_base64_lenient;
use sw_proto::pyjson::PyValue;

const PREFIX: &str = "abc";
const SECRET: &str = "def";
const RAW_SECRET: &str = "raw-secret-i312";

#[derive(Default)]
struct StubBridge;

impl BridgeQueries for StubBridge {
    fn active_cmd_id_for(&self, _com: &str) -> Option<String> {
        None
    }

    fn profile_for(&self, _com: &str) -> Option<String> {
        Some("t".into())
    }

    fn known_coms(&self) -> Vec<String> {
        vec!["COM0".into()]
    }
}

struct EventRig {
    engine: Arc<EventEngine>,
    _dir: tempfile::TempDir,
}

fn event_rig() -> EventRig {
    let dir = tempfile::tempdir().expect("事件目錄位於 TMPDIR");
    let clock: Arc<dyn Clock> = FakeClock::new();
    let env: Arc<dyn SysEnv> = Arc::new(FakeEnv::default());
    let handlers: Arc<dyn HandlerSpawner> = FakeHandlerSpawner::scripted(vec![]);
    let platform = sw_pal::fakes::platform()
        .fs(sw_pal::native().fs)
        .clock(clock)
        .env(env)
        .handlers(handlers)
        .build();
    let engine = Arc::new(
        EventEngine::new(EngineDeps {
            events_dir: dir.path().join("events.d"),
            runtime_dir: dir.path().join("runtime"),
            log_path: dir.path().join("events.ndjson"),
            bridge: Arc::new(StubBridge),
            queue_max: 1024,
            per_daemon_max: 8,
            platform: Arc::new(platform),
            warn: Arc::new(|_message| {}),
        })
        .expect("建立 EventEngine"),
    );
    engine.start().expect("啟動 EventEngine");
    EventRig { engine, _dir: dir }
}

struct WindowRig {
    service: ServiceRig,
    device: sw_pal::fakes::serial::FakeDevice,
    _event: EventRig,
    _log_dir: tempfile::TempDir,
}

fn build_window_rig() -> WindowRig {
    build_window_rig_with_readiness(false)
}

fn build_window_rig_ready() -> WindowRig {
    build_window_rig_with_readiness(true)
}

fn build_window_rig_with_readiness(ready: bool) -> WindowRig {
    let event = event_rig();
    let log_dir = tempfile::tempdir().expect("agent log 目錄位於 TMPDIR");
    let profile = format!(
        "profiles:\n  t:\n    platform: {}\n    prompt_regex: '(?m)^root@t:/# '\n    password_regex: 'Password:'\n    timeout_s: 2.0\ndefaults:\n  log_dir: '{}'\ntargets:\n  - {{com: COM0, profile: t, device_by_id: {BY_ID_A}}}\n",
        if ready { "shell" } else { "passthrough" },
        log_dir.path().display()
    );
    let serial = FakeSerialBackend::new();
    let device = serial.add_device(REAL_A);
    device.set_loopback(true);
    if ready {
        support::install_shell_replies(&device);
    }
    let observers: Vec<Arc<dyn sw_core::uart_bridge::RxObserver>> = vec![event.engine.clone()];
    let service = ServiceRig::build_with_env_serial_console_observers(
        RigSpec {
            yaml: profile,
            devices: vec![(BY_ID_A.into(), REAL_A.into())],
            explicit_target_count: 1,
            allowlist: None,
            wait_ready: if ready { vec!["COM0".into()] } else { Vec::new() },
        },
        Arc::new(FakeEnv::default()),
        serial as Arc<dyn SerialBackend>,
        observers,
    );
    assert!(service.svc.attach_engine(event.engine.clone()));
    service.wait_state("COM0", if ready { "READY" } else { "ATTACHED" });
    WindowRig {
        service,
        device,
        _event: event,
        _log_dir: log_dir,
    }
}

fn attach_console(rig: &WindowRig) -> (String, sw_pal::fakes::console::FakeConsoleRef) {
    let before = rig
        .service
        .console
        .as_ref()
        .expect("fake PTY factory")
        .created()
        .len();
    let response = rig
        .service
        .call("session.console_attach", r#"{"selector":"COM0"}"#);
    assert_eq!(response.get("ok"), Some(&PyValue::Bool(true)), "{response:?}");
    let client_id = response
        .get("client_id")
        .and_then(PyValue::as_str)
        .expect("console client_id")
        .to_string();
    let factory = rig.service.console.as_ref().expect("fake PTY factory");
    support::wait_until("human console 建立", || factory.created().len() > before);
    let console = factory.created()[before].1.clone();
    support::wait_until("human console 登記", || {
        matches!(
            rig.service
                .call("session.console_list", r#"{"selector":"COM0"}"#)
                .get("consoles"),
            Some(PyValue::List(rows))
                if rows.iter().any(|row| row.get("client_id").and_then(PyValue::as_str) == Some(client_id.as_str()))
        )
    });
    (client_id, console)
}

fn install_event_probe(rig: &WindowRig, name: &str, value: &str) {
    let rule = format!(
        r#"{{"schema_version":1,"owner":"o","name":"{name}","kind":"tool","selectors":["COM0"],"pattern":{{"kind":"contains","value":"{value}"}},"handler":{{"exec":["true"]}}}}"#
    );
    let response = rig.service.call("event.rule_set", &rule);
    assert_eq!(response.get("ok"), Some(&PyValue::Bool(true)), "{response:?}");
}

fn wait_console_contains(
    console: &sw_pal::fakes::console::FakeConsoleRef,
    needle: &[u8],
    label: &str,
) {
    support::wait_until_within(label, Duration::from_secs(3), || {
        console
            .written()
            .windows(needle.len())
            .any(|window| window == needle)
    });
}

fn wait_tx(rig: &WindowRig, expected: usize, label: &str) {
    support::wait_until_within(label, Duration::from_secs(3), || rig.device.tx_len() >= expected);
    assert_eq!(rig.device.tx_len(), expected, "{label} 不得多送 UART bytes");
}

fn read_wal(rig: &WindowRig) -> String {
    String::from_utf8(
        rig.service
            .wfs
            .contents(&rig.service.wal.wal_path())
            .expect("raw.wal.ndjson"),
    )
    .expect("WAL UTF-8")
}

fn decode_wal(wal: &str) -> Vec<u8> {
    wal.lines()
        .filter_map(|line| sw_proto::pyjson::loads(line).ok())
        .filter_map(|row| row.get("payload_b64").and_then(PyValue::as_str).map(str::to_owned))
        .flat_map(|payload| a2b_base64_lenient(payload.as_bytes()).expect("WAL payload base64"))
        .collect()
}

fn prompted_secret_rig() -> (
    WindowRig,
    String,
    sw_pal::fakes::console::FakeConsoleRef,
    String,
) {
    let rig = build_window_rig();
    let (client_id, console) = attach_console(&rig);
    let log = rig.service.call("session.log_start", r#"{"selector":"COM0"}"#);
    assert_eq!(log.get("ok"), Some(&PyValue::Bool(true)), "{log:?}");
    let log_path = log
        .get("log_path")
        .and_then(PyValue::as_str)
        .expect("log_path")
        .to_string();
    install_event_probe(&rig, "racy-secret-plain", RAW_SECRET);
    install_event_probe(&rig, "racy-secret-masked", "***");
    let enabled = rig
        .service
        .call("event.com_enable", r#"{"selector":"COM0"}"#);
    assert_eq!(enabled.get("ok"), Some(&PyValue::Bool(true)), "{enabled:?}");
    let rx_start = rig.device.rx_bytes_read();
    rig.device.push_rx(b"Password: ");
    support::wait_until_within("reader 看到 Password prompt", Duration::from_secs(3), || {
        rig.device.rx_bytes_read() >= rx_start + b"Password: ".len()
    });
    wait_console_contains(&console, b"Password: ", "輸入前 console prompt barrier");
    (rig, client_id, console, log_path)
}

fn wait_secret_meta(rig: &WindowRig) -> Vec<PyValue> {
    support::wait_until_within("human secret META 寫入 WAL", Duration::from_secs(3), || {
        read_wal(rig)
            .lines()
            .filter_map(|line| sw_proto::pyjson::loads(line).ok())
            .any(|row| {
                row.get("dir").and_then(PyValue::as_str) == Some("META")
                    && row
                        .get("meta")
                        .and_then(|meta| meta.get("event"))
                        .and_then(PyValue::as_str)
                        == Some("secret_tx")
            })
    });
    read_wal(rig)
        .lines()
        .filter_map(|line| sw_proto::pyjson::loads(line).ok())
        .filter(|row| {
            row.get("dir").and_then(PyValue::as_str) == Some("META")
                && row
                    .get("meta")
                    .and_then(|meta| meta.get("event"))
                    .and_then(PyValue::as_str)
                    == Some("secret_tx")
        })
        .collect()
}

fn wait_secret_meta_count(rig: &WindowRig, client_id: &str, expected: usize) {
    let source = format!("human:{client_id}");
    support::wait_until_within(
        "已寫 UART 的 secret chunk 立即有對應 META",
        Duration::from_secs(3),
        || {
            read_wal(rig)
                .lines()
                .filter_map(|line| sw_proto::pyjson::loads(line).ok())
                .filter(|row| {
                    row.get("dir").and_then(PyValue::as_str) == Some("META")
                        && row.get("source").and_then(PyValue::as_str) == Some(source.as_str())
                        && row
                            .get("meta")
                            .and_then(|meta| meta.get("event"))
                            .and_then(PyValue::as_str)
                            == Some("secret_tx")
                })
                .count()
                >= expected
        },
    );
    let count = read_wal(rig)
        .lines()
        .filter_map(|line| sw_proto::pyjson::loads(line).ok())
        .filter(|row| {
            row.get("dir").and_then(PyValue::as_str) == Some("META")
                && row.get("source").and_then(PyValue::as_str) == Some(source.as_str())
                && row
                    .get("meta")
                    .and_then(|meta| meta.get("event"))
                    .and_then(PyValue::as_str)
                    == Some("secret_tx")
        })
        .count();
    assert_eq!(count, expected, "已寫 TX chunk 的 META 筆數必須精確：{source}");
}

fn assert_secret_sinks_clear(
    rig: &WindowRig,
    console: &sw_pal::fakes::console::FakeConsoleRef,
    log_path: &str,
    secret: &[u8],
) {
    let wal = read_wal(rig);
    let decoded = decode_wal(&wal);
    let mirror = String::from_utf8(
        rig.service
            .wfs
            .contents(&rig.service.wal.mirror_path())
            .expect("raw.mirror.log"),
    )
    .expect("mirror UTF-8");
    let event_text = format!("{:?}", rig.service.call("event.tail", r#"{"n":-1}"#));
    let console_text = String::from_utf8_lossy(&console.written()).into_owned();
    let agent_log = std::fs::read_to_string(Path::new(log_path)).unwrap_or_default();
    let secret = String::from_utf8_lossy(secret);
    assert!(
        !wal.contains(secret.as_ref())
            && !String::from_utf8_lossy(&decoded).contains(secret.as_ref())
            && !mirror.contains(secret.as_ref())
            && !event_text.contains(secret.as_ref())
            && !console_text.contains(secret.as_ref())
            && !agent_log.contains(secret.as_ref()),
        "TX/RX sink 洩漏明文密碼：WAL={wal:?}; mirror={mirror:?}; event={event_text:?}; console={console_text:?}; agent_log={agent_log:?}"
    );
}

fn wait_for_masked_echo(log_path: &str) {
    support::wait_until_within("agent log 收到 masked DUT echo", Duration::from_secs(3), || {
        std::fs::read_to_string(Path::new(log_path))
            .is_ok_and(|text| text.contains("***"))
    });
}

fn submit_blocked_secret(rig: &WindowRig, console: &sw_pal::fakes::console::FakeConsoleRef) {
    rig.device
        .script_write(vec![FakeWriteStep::BlockForever]);
    console.push_input(&[RAW_SECRET.as_bytes(), b"\r"].concat());
    rig.device.wait_blocked();
}

fn assert_one_secret_meta(rig: &WindowRig, client_id: &str) {
    let rows = wait_secret_meta(rig);
    assert_eq!(rows.len(), 1, "每個單一 secret chunk 恰有一筆 META：{rows:?}");
    let meta = rows[0].get("meta").expect("META 欄位");
    assert_eq!(
        meta.get("secret_len").and_then(PyValue::as_i64),
        Some(RAW_SECRET.len() as i64)
    );
    assert_eq!(
        rows[0].get("source").and_then(PyValue::as_str),
        Some(format!("human:{client_id}").as_str())
    );
}

#[test]
fn issue312_in_flight_secret_disconnect_keeps_wal_meta() {
    let (rig, client_id, console, log_path) = prompted_secret_rig();
    submit_blocked_secret(&rig, &console);
    console.hangup();
    support::wait_until_within("human console 自然斷線移除", Duration::from_secs(3), || {
        !matches!(
            rig.service
                .call("session.console_list", r#"{"selector":"COM0"}"#)
                .get("consoles"),
            Some(PyValue::List(rows)) if rows.iter().any(|row| row.get("client_id").and_then(PyValue::as_str) == Some(client_id.as_str()))
        )
    });
    rig.device.release_block();
    assert_one_secret_meta(&rig, &client_id);
    wait_for_masked_echo(&log_path);
    assert_secret_sinks_clear(&rig, &console, &log_path, RAW_SECRET.as_bytes());
}

#[test]
fn issue312_in_flight_secret_detach_keeps_wal_meta() {
    let (rig, client_id, console, log_path) = prompted_secret_rig();
    submit_blocked_secret(&rig, &console);
    let detached = rig.service.call(
        "session.console_detach",
        &format!(r#"{{"selector":"COM0","client_id":"{client_id}"}}"#),
    );
    assert_eq!(detached.get("ok"), Some(&PyValue::Bool(true)), "{detached:?}");
    rig.device.release_block();
    assert_one_secret_meta(&rig, &client_id);
    wait_for_masked_echo(&log_path);
    assert_secret_sinks_clear(&rig, &console, &log_path, RAW_SECRET.as_bytes());
}

#[test]
fn issue312_in_flight_secret_stop_keeps_wal_meta() {
    let (rig, client_id, console, log_path) = prompted_secret_rig();
    submit_blocked_secret(&rig, &console);
    rig.service.mgr.begin_shutdown();
    rig.device.release_block();
    assert_one_secret_meta(&rig, &client_id);
    wait_for_masked_echo(&log_path);
    assert_secret_sinks_clear(&rig, &console, &log_path, RAW_SECRET.as_bytes());
}

#[test]
fn issue312_line_buffer_loopback_masks_each_secret_echo_byte() {
    let rig = build_window_rig_ready();
    let (raw_id, raw_console) = attach_console(&rig);
    let (line_id, line_console) = attach_console(&rig);
    let detached = rig.service.call(
        "session.console_detach",
        &format!(r#"{{"selector":"COM0","client_id":"{raw_id}"}}"#),
    );
    assert_eq!(detached.get("ok"), Some(&PyValue::Bool(true)), "{detached:?}");
    rig.device.set_loopback(false);
    let log = rig.service.call("session.log_start", r#"{"selector":"COM0"}"#);
    assert_eq!(log.get("ok"), Some(&PyValue::Bool(true)), "{log:?}");
    let log_path = log
        .get("log_path")
        .and_then(PyValue::as_str)
        .expect("log_path")
        .to_string();
    install_event_probe(&rig, "line-secret-plain", RAW_SECRET);
    install_event_probe(&rig, "line-secret-masked", "***");
    let enabled = rig
        .service
        .call("event.com_enable", r#"{"selector":"COM0"}"#);
    assert_eq!(enabled.get("ok"), Some(&PyValue::Bool(true)), "{enabled:?}");

    let rx_start = rig.device.rx_bytes_read();
    rig.device.push_rx(b"Password: ");
    support::wait_until_within("reader 收到 line-buffer password prompt", Duration::from_secs(3), || {
        rig.device.rx_bytes_read() >= rx_start + b"Password: ".len()
    });
    wait_console_contains(&line_console, b"Password: ", "line console prompt barrier");

    let tx_start = rig.device.tx_len();
    line_console.push_input(&[RAW_SECRET.as_bytes(), b"\r"].concat());
    wait_tx(
        &rig,
        tx_start + RAW_SECRET.len() + 1,
        "line-buffer secret 寫出 UART",
    );
    assert_one_secret_meta(&rig, &line_id);
    let echo_start = rig.device.rx_bytes_read();
    for (index, byte) in RAW_SECRET.bytes().chain(b"\r\n".iter().copied()).enumerate() {
        rig.device.push_rx(&[byte]);
        support::wait_until_within("line-buffer DUT 逐 byte 回顯經 reader", Duration::from_secs(3), || {
            rig.device.rx_bytes_read() > echo_start + index
        });
    }
    let wal = read_wal(&rig);
    assert!(
        decode_wal(&wal).windows(3).any(|part| part == b"***"),
        "line-buffer DUT echo 應在 RX WAL 以遮罩內容記錄：{wal}"
    );
    let event_text = format!("{:?}", rig.service.call("event.tail", r#"{"n":-1}"#));
    assert!(event_text.contains("***"), "line-buffer event 應看到遮罩回顯：{event_text}");
    assert!(
        line_console.written().windows(3).any(|part| part == b"***"),
        "line-buffer console 應看到遮罩回顯：{:?}",
        String::from_utf8_lossy(&line_console.written())
    );
    let rows = read_wal(&rig)
        .lines()
        .filter_map(|line| sw_proto::pyjson::loads(line).ok())
        .collect::<Vec<_>>();
    let metas = rows
        .iter()
        .filter(|row| {
            row.get("dir").and_then(PyValue::as_str) == Some("META")
                && row.get("source").and_then(PyValue::as_str)
                    == Some(format!("human:{line_id}").as_str())
                && row
                    .get("meta")
                    .and_then(|meta| meta.get("event"))
                    .and_then(PyValue::as_str)
                    == Some("secret_tx")
        })
        .collect::<Vec<_>>();
    assert_eq!(metas.len(), 1, "line-buffer 的整行 TX 是一個 secret chunk");
    assert_eq!(
        metas[0]
            .get("meta")
            .and_then(|meta| meta.get("secret_len"))
            .and_then(PyValue::as_i64),
        Some(RAW_SECRET.len() as i64)
    );
    assert_secret_sinks_clear(&rig, &line_console, &log_path, RAW_SECRET.as_bytes());
    assert!(
        !String::from_utf8_lossy(&raw_console.written()).contains(RAW_SECRET),
        "其他 console 也不得收到 line-buffer 密碼明文"
    );
}

#[test]
fn issue312_echo_registration_over_1024_masks_the_entire_rx_line() {
    let rig = build_window_rig();
    let (client_id, console) = attach_console(&rig);
    let log = rig.service.call("session.log_start", r#"{"selector":"COM0"}"#);
    assert_eq!(log.get("ok"), Some(&PyValue::Bool(true)), "{log:?}");
    let log_path = log
        .get("log_path")
        .and_then(PyValue::as_str)
        .expect("log_path")
        .to_string();
    let secret = vec![b'Z'; 1025];
    let secret_text = String::from_utf8(secret.clone()).expect("ASCII secret");
    install_event_probe(&rig, "over-cap-secret-plain", &secret_text);
    install_event_probe(&rig, "over-cap-secret-masked", "***");
    let enabled = rig
        .service
        .call("event.com_enable", r#"{"selector":"COM0"}"#);
    assert_eq!(enabled.get("ok"), Some(&PyValue::Bool(true)), "{enabled:?}");
    let rx_start = rig.device.rx_bytes_read();
    rig.device.push_rx(b"Password: ");
    support::wait_until_within("reader 收到 over-cap password prompt", Duration::from_secs(3), || {
        rig.device.rx_bytes_read() >= rx_start + b"Password: ".len()
    });
    wait_console_contains(&console, b"Password: ", "over-cap 輸入 prompt barrier");

    let tx_start = rig.device.tx_len();
    console.push_input(&[secret.as_slice(), b"\r"].concat());
    wait_tx(&rig, tx_start + secret.len() + 1, "超過 1024 bytes 的 secret TX");
    support::wait_until_within("over-cap loopback echo 經 reader", Duration::from_secs(5), || {
        rig.device.rx_bytes_read() > rx_start + b"Password: ".len() + secret.len()
    });
    support::wait_until_within("over-cap echo 進 agent log", Duration::from_secs(3), || {
        std::fs::read_to_string(Path::new(&log_path))
            .is_ok_and(|text| text.contains("***"))
    });
    let rows = read_wal(&rig)
        .lines()
        .filter_map(|line| sw_proto::pyjson::loads(line).ok())
        .collect::<Vec<_>>();
    let metas = rows
        .iter()
        .filter(|row| {
            row.get("dir").and_then(PyValue::as_str) == Some("META")
                && row.get("source").and_then(PyValue::as_str)
                    == Some(format!("human:{client_id}").as_str())
                && row
                    .get("meta")
                    .and_then(|meta| meta.get("event"))
                    .and_then(PyValue::as_str)
                    == Some("secret_tx")
        })
        .collect::<Vec<_>>();
    assert_eq!(metas.len(), 1, "over-cap secret TX 只寫一筆 META");
    assert_eq!(
        metas[0]
            .get("meta")
            .and_then(|meta| meta.get("secret_len"))
            .and_then(PyValue::as_i64),
        Some(secret.len() as i64),
        "META 仍記錄實際寫出的完整 secret 長度"
    );
    assert_secret_sinks_clear(&rig, &console, &log_path, &secret);
}

#[test]
fn issue312_echo_registration_clears_after_newline_without_echo_prefix() {
    let rig = build_window_rig();
    let (client_id, console) = attach_console(&rig);
    let log = rig.service.call("session.log_start", r#"{"selector":"COM0"}"#);
    assert_eq!(log.get("ok"), Some(&PyValue::Bool(true)), "{log:?}");
    let log_path = log
        .get("log_path")
        .and_then(PyValue::as_str)
        .expect("log_path")
        .to_string();
    let secret = "ZebraEchoTail-i312";
    install_event_probe(&rig, "after-terminator-prefix", "Z");
    install_event_probe(&rig, "after-terminator-secret", secret);
    install_event_probe(&rig, "after-terminator-mask", "***");
    let enabled = rig
        .service
        .call("event.com_enable", r#"{"selector":"COM0"}"#);
    assert_eq!(enabled.get("ok"), Some(&PyValue::Bool(true)), "{enabled:?}");
    rig.device.set_loopback(false);

    let prompt_start = rig.device.rx_bytes_read();
    rig.device.push_rx(b"Password: ");
    support::wait_until_within("reader 收到 no-echo 測試提示", Duration::from_secs(3), || {
        rig.device.rx_bytes_read() >= prompt_start + b"Password: ".len()
    });
    wait_console_contains(&console, b"Password: ", "no-echo 測試輸入 prompt barrier");

    let tx_start = rig.device.tx_len();
    console.push_input(&[secret.as_bytes(), b"\r"].concat());
    wait_tx(
        &rig,
        tx_start + secret.len() + 1,
        "no-echo secret TX 與換行",
    );
    wait_secret_meta_count(&rig, &client_id, 1);

    let rx_end = rig.device.rx_bytes_read();
    rig.device.push_rx(b"\r\n");
    support::wait_until_within("reader 收到沒有 echo prefix 的行尾", Duration::from_secs(3), || {
        rig.device.rx_bytes_read() >= rx_end + 2
    });
    rig.device.push_rx(b"Z");
    support::wait_until_within("行尾後新 RX prefix 送達 console", Duration::from_secs(3), || {
        console.written().contains(&b'Z')
    });
    support::wait_until_within("行尾後新 RX prefix 送達 agent log", Duration::from_secs(3), || {
        std::fs::read_to_string(Path::new(&log_path))
            .is_ok_and(|text| text.contains('Z'))
    });
    support::wait_until_within("行尾後新 RX prefix 觸發 event", Duration::from_secs(3), || {
        format!("{:?}", rig.service.call("event.tail", r#"{"n":-1}"#))
            .contains("after-terminator-prefix")
    });

    let wal = read_wal(&rig);
    assert!(
        decode_wal(&wal).contains(&b'Z'),
        "已清除 echo matcher 後的 RX prefix 應進入 WAL：{wal}"
    );
    assert_secret_sinks_clear(&rig, &console, &log_path, secret.as_bytes());
}

#[test]
fn issue312_raw_loopback_records_each_secret_chunk_at_write_time() {
    let rig = build_window_rig();
    let (client_id, console) = attach_console(&rig);
    let log = rig.service.call("session.log_start", r#"{"selector":"COM0"}"#);
    assert_eq!(log.get("ok"), Some(&PyValue::Bool(true)), "{log:?}");
    let log_path = log
        .get("log_path")
        .and_then(PyValue::as_str)
        .expect("log_path")
        .to_string();
    install_event_probe(&rig, "raw-secret-plain", RAW_SECRET);
    install_event_probe(&rig, "raw-secret-masked", "***");
    let enabled = rig
        .service
        .call("event.com_enable", r#"{"selector":"COM0"}"#);
    assert_eq!(enabled.get("ok"), Some(&PyValue::Bool(true)), "{enabled:?}");

    let prompt_start = rig.device.rx_bytes_read();
    rig.device.push_rx(b"Password: ");
    support::wait_until_within("reader 收到 password prompt", Duration::from_secs(3), || {
        rig.device.rx_bytes_read() >= prompt_start + b"Password: ".len()
    });
    wait_console_contains(&console, b"Password: ", "human console 收到提示 barrier");

    let tx_start = rig.device.tx_len();
    for (index, byte) in RAW_SECRET.bytes().enumerate() {
        console.push_input(&[byte]);
        wait_tx(&rig, tx_start + index + 1, "raw password 逐鍵送出");
        wait_secret_meta_count(&rig, &client_id, index + 1);
    }
    console.push_input(b"\r");
    wait_tx(&rig, tx_start + RAW_SECRET.len() + 1, "raw password 換行送出");
    support::wait_until_within("raw loopback echo 經 reader", Duration::from_secs(3), || {
        rig.device.rx_bytes_read() > prompt_start + b"Password: ".len() + RAW_SECRET.len()
    });

    let wal = read_wal(&rig);
    let rows = wal
        .lines()
        .filter_map(|line| sw_proto::pyjson::loads(line).ok())
        .collect::<Vec<_>>();
    let secret_meta = rows
        .iter()
        .filter(|row| {
            row.get("dir").and_then(PyValue::as_str) == Some("META")
                && row.get("source").and_then(PyValue::as_str)
                    == Some(format!("human:{client_id}").as_str())
                && row
                    .get("meta")
                    .and_then(|meta| meta.get("event"))
                    .and_then(PyValue::as_str)
                    == Some("secret_tx")
        })
        .collect::<Vec<_>>();
    let decoded = decode_wal(&wal);
    let mirror = String::from_utf8(
        rig.service
            .wfs
            .contents(&rig.service.wal.mirror_path())
            .expect("raw.mirror.log"),
    )
    .expect("mirror UTF-8");
    let event_text = format!("{:?}", rig.service.call("event.tail", r#"{"n":-1}"#));
    support::wait_until_within("agent log 收到 masked RX", Duration::from_secs(3), || {
        std::fs::read_to_string(Path::new(&log_path)).is_ok_and(|text| text.contains("***"))
    });
    let agent_log = std::fs::read_to_string(Path::new(&log_path)).expect("agent log");
    let console_text = String::from_utf8_lossy(&console.written()).into_owned();
    let sink_leaks = [
        wal.contains(RAW_SECRET),
        String::from_utf8_lossy(&decoded).contains(RAW_SECRET),
        mirror.contains(RAW_SECRET),
        event_text.contains(RAW_SECRET),
        console_text.contains(RAW_SECRET),
        agent_log.contains(RAW_SECRET),
    ];
    assert!(
        !sink_leaks.into_iter().any(|leak| leak),
        "TX/RX sink 不得含明文密碼：WAL={wal:?}; mirror={mirror:?}; event={event_text:?}; console={console_text:?}; agent_log={agent_log:?}"
    );
    assert_eq!(
        secret_meta.len(),
        RAW_SECRET.len(),
        "逐鍵 raw TX 每個已寫 secret chunk 各有一筆 META: {secret_meta:?}; WAL={wal:?}"
    );
    assert!(secret_meta.iter().all(|row| {
        row.get("meta")
            .and_then(|meta| meta.get("secret_len"))
            .and_then(PyValue::as_i64)
            == Some(1)
    }));
}

#[test]
fn issue312_plain_raw_without_newline_has_immediate_tx_wal() {
    let rig = build_window_rig();
    let (_client_id, console) = attach_console(&rig);
    let tx_start = rig.device.tx_len();
    console.push_input(b"ordinary-open");
    wait_tx(&rig, tx_start + b"ordinary-open".len(), "普通未換行 raw TX 寫出");

    support::wait_until_within("普通未換行 raw TX 立即落 WAL", Duration::from_secs(1), || {
        read_wal(&rig)
            .lines()
            .filter_map(|line| sw_proto::pyjson::loads(line).ok())
            .any(|row| {
                row.get("dir").and_then(PyValue::as_str) == Some("TX")
                    && row
                        .get("payload_b64")
                        .and_then(PyValue::as_str)
                        .and_then(|payload| a2b_base64_lenient(payload.as_bytes()).ok())
                        .as_deref()
                        == Some(b"ordinary-open".as_slice())
            })
    });
    rig.service.mgr.begin_shutdown();
    let wal_after_stop = read_wal(&rig);
    assert!(
        decode_wal(&wal_after_stop)
            .windows(b"ordinary-open".len())
            .any(|part| part == b"ordinary-open"),
        "daemon stop 前未換行普通輸入的 WAL payload 應已落盤：{wal_after_stop}"
    );
}

#[test]
fn issue312_unfinished_wal_record_survives_abrupt_process_exit() {
    const CHILD_DIR: &str = "SERIALWRAP_I312_CRASH_CHILD_DIR";
    const CHILD_MARKER: &str = "SERIALWRAP_I312_CRASH_CHILD_MARKER";
    const CHILD_ROOT_MARKER: &str = "SERIALWRAP_I312_CRASH_CHILD_ROOT_MARKER";

    if let Some(dir) = std::env::var_os(CHILD_DIR) {
        let wal_dir = PathBuf::from(dir);
        let marker = PathBuf::from(
            std::env::var_os(CHILD_MARKER).expect("child marker path"),
        );
        let root_marker = PathBuf::from(
            std::env::var_os(CHILD_ROOT_MARKER).expect("child root marker path"),
        );
        let mut env = support::isolated_env();
        fs::write(&root_marker, env.root.display().to_string())
            .expect("提供 parent 清理 child 隔離目錄所需的路徑");
        env.vars.insert(
            "SERIALWRAP_WAL_DIR".into(),
            wal_dir.display().to_string(),
        );
        let device_info = sw_pal::traits::DeviceInfo {
            by_id: support::TARGET_BY_ID.into(),
            real_path: "/fake/serialwrap-i312-crash".into(),
        };
        let (platform, handles) = support::in_process_platform_with_handles(
            &env.vars,
            vec![(support::TARGET_BY_ID.into(), device_info)],
            support::Fixture::ReadyShell,
        );
        let device = handles.devices[0].clone();
        device.set_loopback(true);
        let profile_path = Path::new(&env.vars["SERIALWRAP_CONFIG_DIR"])
            .join("profiles/inproc.yaml");
        let profile = fs::read_to_string(&profile_path).expect("讀取隔離測試 profile");
        fs::write(
            &profile_path,
            profile.replace(
                "timeout_s: 2.0",
                "timeout_s: 2.0\n    password_regex: 'Password:'",
            ),
        )
        .expect("設定隔離 profile 的 password_regex");

        let socket = env.socket.clone();
        let _daemon = std::thread::spawn(move || {
            support::run_with_hooks(
                vec!["serialwrapd".into()],
                platform,
                support::DaemonHooks::default(),
            )
        });
        support::wait_socket(&socket, Duration::from_secs(30));
        support::wait_ready(&env, "COM0");

        let attached = support::rpc_value(
            &socket,
            "session.console_attach",
            r#"{"selector":"COM0"}"#,
        );
        assert_eq!(attached.get("ok"), Some(&PyValue::Bool(true)), "{attached:?}");
        let vtty = attached
            .get("vtty")
            .and_then(PyValue::as_str)
            .expect("隔離 PTY vtty");
        let mut console_reader = OpenOptions::new()
            .read(true)
            .open(vtty)
            .expect("讀取隔離 human console PTY");
        let (prompt_tx, prompt_rx) = crossbeam_channel::bounded(1);
        std::thread::spawn(move || {
            let mut received = Vec::new();
            let mut chunk = [0u8; 256];
            loop {
                match console_reader.read(&mut chunk) {
                    Ok(0) | Err(_) => return,
                    Ok(count) => {
                        received.extend_from_slice(&chunk[..count]);
                        if received
                            .windows(support::TARGET_PROMPT.len())
                            .any(|window| window == support::TARGET_PROMPT.as_bytes())
                        {
                            let _ = prompt_tx.send(());
                            return;
                        }
                    }
                }
            }
        });
        device.push_rx(support::TARGET_PROMPT.as_bytes());
        prompt_rx
            .recv_timeout(Duration::from_secs(5))
            .expect("human 等待自己的 console 收到 shell prompt");
        let mut console = OpenOptions::new()
            .write(true)
            .open(vtty)
            .expect("開啟隔離 human console PTY");
        console
            .write_all(b"ordinary-open")
            .expect("透過 human console 送出未換行輸入");
        console.flush().expect("flush 隔離 human console PTY");

        let wal_path = wal_dir.join("raw.wal.ndjson");
        let deadline = Instant::now() + Duration::from_secs(10);
        loop {
            assert!(
                Instant::now() < deadline,
                "TX WAL 未在異常終止前記錄普通未換行輸入；UART tx_len={}",
                device.tx_len()
            );
            if device.tx_len() >= b"ordinary-open".len()
                && fs::read_to_string(&wal_path)
                    .is_ok_and(|wal| wal_has_tx_payload(&wal, b"ordinary-open"))
            {
                break;
            }
            std::thread::sleep(Duration::from_millis(10));
        }
        fs::write(&marker, env.root.display().to_string())
            .expect("通知 parent WAL 已寫入且尚未送換行");
        loop {
            std::thread::sleep(Duration::from_secs(60));
        }
    }

    let dir = tempfile::tempdir().expect("abnormal-exit WAL directory");
    let wal_dir = dir.path().join("wal");
    let marker = dir.path().join("written.marker");
    let root_marker = dir.path().join("child-root.marker");
    let stdout_path = dir.path().join("child.stdout");
    let stderr_path = dir.path().join("child.stderr");
    let stdout = std::fs::File::create(&stdout_path).expect("child stdout log");
    let stderr = std::fs::File::create(&stderr_path).expect("child stderr log");
    let mut child = Command::new(std::env::current_exe().expect("test binary path"))
        .args([
            "--exact",
            "issue312_unfinished_wal_record_survives_abrupt_process_exit",
        ])
        .env(CHILD_DIR, &wal_dir)
        .env(CHILD_MARKER, &marker)
        .env(CHILD_ROOT_MARKER, &root_marker)
        .stdin(Stdio::null())
        .stdout(Stdio::from(stdout))
        .stderr(Stdio::from(stderr))
        .spawn()
        .expect("spawn isolated WAL writer process");
    let deadline = std::time::Instant::now() + Duration::from_secs(5);
    loop {
        if marker.exists() {
            break;
        }
        if let Some(status) = child.try_wait().expect("檢查 child 是否提早結束") {
            panic!(
                "child 未在普通未換行 TX 後建立 marker；status={status}; stdout={}; stderr={}",
                fs::read_to_string(&stdout_path).unwrap_or_default(),
                fs::read_to_string(&stderr_path).unwrap_or_default()
            );
        }
        if std::time::Instant::now() >= deadline {
            let _ = child.kill();
            let _ = child.wait();
            panic!("child 未在普通未換行 TX 後建立 marker");
        }
        std::thread::sleep(Duration::from_millis(10));
    }
    let child_root = PathBuf::from(
        fs::read_to_string(&root_marker).expect("讀取 child 隔離目錄路徑"),
    );
    assert!(
        child_root.starts_with(std::env::temp_dir())
            && child_root.file_name().is_some_and(|name| {
                name.to_string_lossy()
                    .starts_with(&format!("sw21-{}-", child.id()))
            }),
        "child 隔離目錄必須位於測試 TMPDIR 且屬於此 child：{}",
        child_root.display()
    );
    child.kill().expect("terminate WAL child before line terminator");
    let status = child.wait().expect("wait for abruptly terminated WAL child");
    assert!(!status.success(), "child termination must be abnormal");
    fs::remove_dir_all(&child_root).expect("移除被終止 child 留下的隔離 daemon 狀態");

    let wal = std::fs::read_to_string(wal_dir.join("raw.wal.ndjson"))
        .expect("原生 WAL survives abrupt daemon-process exit");
    assert!(
        wal_has_tx_payload(&wal, b"ordinary-open"),
        "human console 的普通未換行 TX WAL 必須在 child 異常終止後仍可還原：{wal}"
    );
}

fn wal_has_tx_payload(wal: &str, expected: &[u8]) -> bool {
    wal.lines()
        .filter_map(|line| sw_proto::pyjson::loads(line).ok())
        .filter(|row| row.get("dir").and_then(PyValue::as_str) == Some("TX"))
        .filter_map(|row| row.get("payload_b64").and_then(PyValue::as_str).map(str::to_owned))
        .filter_map(|payload| a2b_base64_lenient(payload.as_bytes()).ok())
        .any(|payload| payload == expected)
}

#[test]
fn issue312_prompt_after_raw_prefix_keeps_tx_and_rx_boundaries() {
    let rig = build_window_rig();
    let (client_id, console) = attach_console(&rig);
    let log = rig.service.call("session.log_start", r#"{"selector":"COM0"}"#);
    assert_eq!(log.get("ok"), Some(&PyValue::Bool(true)), "{log:?}");
    let log_path = log
        .get("log_path")
        .and_then(PyValue::as_str)
        .expect("log_path")
        .to_string();
    install_event_probe(&rig, "prefix-plain", PREFIX);
    install_event_probe(&rig, "secret-plain", SECRET);
    install_event_probe(&rig, "secret-masked", "***");
    let enabled = rig
        .service
        .call("event.com_enable", r#"{"selector":"COM0"}"#);
    assert_eq!(enabled.get("ok"), Some(&PyValue::Bool(true)), "{enabled:?}");

    let tx_start = rig.device.tx_len();
    let mut rx_cursor = rig.device.rx_bytes_read();
    for (index, byte) in PREFIX.bytes().enumerate() {
        console.push_input(&[byte]);
        wait_tx(&rig, tx_start + index + 1, "提示前 raw 前綴逐鍵送出");
        rx_cursor += 1;
        support::wait_until_within("DUT 提示前逐鍵回顯", Duration::from_secs(3), || {
            rig.device.rx_bytes_read() >= rx_cursor
        });
    }
    wait_console_contains(&console, PREFIX.as_bytes(), "console 收到提示前回顯");

    let rx_before_prompt = rig.device.rx_bytes_read();
    rig.device.push_rx(b"Password: ");
    support::wait_until_within("reader 讀到測試提示", Duration::from_secs(3), || {
        rig.device.rx_bytes_read() >= rx_before_prompt + b"Password: ".len()
    });
    wait_console_contains(&console, b"Password: ", "human console 收到提示 barrier");

    for byte in SECRET.bytes() {
        let expected = rig.device.tx_len() + 1;
        console.push_input(&[byte]);
        wait_tx(&rig, expected, "提示後 raw password 逐鍵送出");
        rx_cursor += 1;
        support::wait_until_within("DUT 提示後逐鍵即時回顯", Duration::from_secs(3), || {
            rig.device.rx_bytes_read() >= rx_cursor
        });
    }
    let expected = rig.device.tx_len() + 1;
    console.push_input(b"\r");
    wait_tx(&rig, expected, "raw password 換行送出");
    rx_cursor += 1;
    support::wait_until_within("DUT echo 結束 RX 行", Duration::from_secs(3), || {
        rig.device.rx_bytes_read() >= rx_cursor
    });
    support::wait_until_within("提示後 echo 到達 console", Duration::from_secs(3), || {
        let text = String::from_utf8_lossy(&console.written()).into_owned();
        text.contains(SECRET) || text.contains("***")
    });
    rig.service.settle();

    let wal = read_wal(&rig);
    let decoded = decode_wal(&wal);
    let mirror = String::from_utf8(
        rig.service
            .wfs
            .contents(&rig.service.wal.mirror_path())
            .expect("raw.mirror.log"),
    )
    .expect("mirror UTF-8");
    support::wait_until_within("agent log 收到提示後 echo", Duration::from_secs(3), || {
        std::fs::read_to_string(Path::new(&log_path))
            .is_ok_and(|text| text.contains(SECRET) || text.contains("***"))
    });
    let event_text = format!("{:?}", rig.service.call("event.tail", r#"{"n":-1}"#));
    let agent_log = std::fs::read_to_string(Path::new(&log_path)).expect("agent log");
    let console_text = String::from_utf8_lossy(&console.written()).into_owned();
    let rows = wal
        .lines()
        .filter_map(|line| sw_proto::pyjson::loads(line).ok())
        .collect::<Vec<_>>();
    let human_tx = rows
        .iter()
        .filter(|row| {
            row.get("dir").and_then(PyValue::as_str) == Some("TX")
                && row.get("source").and_then(PyValue::as_str)
                    == Some(format!("human:{client_id}").as_str())
        })
        .filter_map(|row| row.get("payload_b64").and_then(PyValue::as_str))
        .flat_map(|payload| a2b_base64_lenient(payload.as_bytes()).expect("human TX base64"))
        .collect::<Vec<_>>();
    let human_tx_text = String::from_utf8_lossy(&human_tx);
    let tx_leaks_secret = human_tx_text.contains(SECRET);
    let tx_lost_prompt_prefix = !human_tx_text.contains(PREFIX);
    let tx_wal_wrong = human_tx != [PREFIX.as_bytes(), b"\r"].concat();
    let rx_leaks_secret = [
        String::from_utf8_lossy(&decoded).contains(SECRET),
        mirror.contains(SECRET),
        event_text.contains(SECRET),
        console_text.contains(SECRET),
        agent_log.contains(SECRET),
    ]
    .into_iter()
    .any(|leak| leak);
    assert!(
        !tx_leaks_secret && !tx_lost_prompt_prefix && !tx_wal_wrong && !rx_leaks_secret,
        "TX/RX 方向必須分開保護：TX 明文密碼={tx_leaks_secret}、提示前綴遺失={tx_lost_prompt_prefix}、TX WAL 不等於 prefix+CR={tx_wal_wrong}、RX sink 明文密碼={rx_leaks_secret}; TX={:?}; WAL={wal:?}; mirror={mirror:?}; event={event_text:?}; console={console_text:?}; agent_log={agent_log:?}",
        String::from_utf8_lossy(&human_tx)
    );
}
