//! issue 311：自動登入／console unlock 的 secret 回顯在所有 reader 出口均已遮罩。
#![cfg(feature = "test-fakes")]
mod support;

use std::path::Path;
use std::sync::atomic::{AtomicBool, Ordering};
use std::sync::Arc;
use std::time::Duration;

use support::{RigSpec, ServiceRig, BY_ID_A, REAL_A};
use sw_core::event_engine::{BridgeQueries, EngineDeps, EventEngine};
use sw_pal::fakes::{FakeClock, FakeEnv, FakeHandlerSpawner, FakeSerialBackend, FakeWriteStep};
use sw_pal::traits::{Clock, HandlerSpawner, SerialBackend, SysEnv};
use sw_proto::pycompat::a2b_base64_lenient;
use sw_proto::pyjson::PyValue;

const SECRET: &str = "i311secret";
const PROMPT: &str = "root@t:/# ";

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

struct LoginRig {
    service: ServiceRig,
    device: sw_pal::fakes::serial::FakeDevice,
    echo_release: crossbeam_channel::Sender<Vec<u8>>,
    _event: EventRig,
    _log_dir: tempfile::TempDir,
}

fn build_login_rig(console_unlock: bool) -> LoginRig {
    let event = event_rig();
    let log_dir = tempfile::tempdir().expect("agent log 目錄位於 TMPDIR");
    let log_path = log_dir.path().join("agent-log");
    let profile = if console_unlock {
        format!(
            "profiles:\n  t:\n    platform: shell\n    prompt_regex: '(?m)^root@t:/# '\n    login_regex: 'login:'\n    post_login_cmd: sh\n    console_unlock_env: SW_I311_UNLOCK\n    console_unlock_idle_s: 0.01\n    timeout_s: 2.0\ndefaults:\n  log_dir: '{}'\ntargets:\n  - {{com: COM0, profile: t, device_by_id: {BY_ID_A}}}\n",
            log_path.display()
        )
    } else {
        format!(
            "profiles:\n  t:\n    platform: shell\n    prompt_regex: '(?m)^root@t:/# '\n    login_regex: 'login:'\n    password_regex: 'Password:'\n    user_env: SW_I311_USER\n    pass_env: SW_I311_PASS\n    timeout_s: 2.0\ndefaults:\n  log_dir: '{}'\ntargets:\n  - {{com: COM0, profile: t, device_by_id: {BY_ID_A}}}\n",
            log_path.display()
        )
    };

    let env = Arc::new(FakeEnv::default());
    {
        let mut vars = env.vars.lock().unwrap();
        vars.insert("SW_I311_USER".into(), "root".into());
        vars.insert("SW_I311_PASS".into(), SECRET.into());
        vars.insert("SW_I311_UNLOCK".into(), SECRET.into());
    }

    let serial = FakeSerialBackend::new();
    let device = serial.add_device(REAL_A);
    if console_unlock {
        device.push_rx(b"locked console\r\n");
    } else {
        // ensure_ready 會先清除 attach 前的 RX，再以空行 probe 取得 login prompt。
        device.on_tx_match("^$", Box::new(|_| b"\r\nlogin: ".to_vec()));
        device.on_tx_match("^root$", Box::new(|_| b"\r\nPassword: ".to_vec()));
    }
    let (secret_seen_tx, secret_seen) = crossbeam_channel::bounded(1);
    let (echo_release, echo_release_rx) = crossbeam_channel::bounded(1);
    device.on_tx_match(
        SECRET,
        Box::new(move |_line| {
            let _ = secret_seen_tx.send(());
            echo_release_rx
                .recv_timeout(Duration::from_secs(30))
                .expect("測試釋放 fake DUT 的 echo_secret")
        }),
    );
    if !console_unlock {
        device.on_tx_match(
            r"__READY__[0-9a-f]+",
            Box::new(|line: &[u8]| {
                let text = String::from_utf8_lossy(line);
                let token = text
                    .split("__READY__")
                    .nth(1)
                    .unwrap_or("")
                    .trim()
                    .to_string();
                format!("__READY__{token}\r\n{PROMPT}").into_bytes()
            }),
        );
    }

    let service = ServiceRig::build_with_env_serial_console_observers(
        RigSpec {
            yaml: profile,
            devices: vec![(BY_ID_A.into(), REAL_A.into())],
            explicit_target_count: 1,
            allowlist: None,
            wait_ready: Vec::new(),
        },
        env,
        serial as Arc<dyn SerialBackend>,
        vec![event.engine.clone()],
    );
    assert!(service.svc.attach_engine(event.engine.clone()));
    if let Err(error) = secret_seen.recv_timeout(Duration::from_secs(10)) {
        panic!(
            "未觀察到自動 secret TX：{error}; tx={:?}; session={:?}",
            String::from_utf8_lossy(&device.tx_since(0)),
            service.session("COM0")
        );
    }
    LoginRig {
        service,
        device,
        echo_release,
        _event: event,
        _log_dir: log_dir,
    }
}

fn build_prompt_prefix_overlap_rig(console_unlock: bool) -> LoginRig {
    const OVERLAP_SECRET: &str = "# secret";
    const OVERLAP_PROMPT: &str = "# ";

    let event = event_rig();
    let log_dir = tempfile::tempdir().expect("agent log 目錄位於 TMPDIR");
    let profile = if console_unlock {
        format!(
            "profiles:\n  t:\n    platform: shell\n    prompt_regex: '(?m)^# '\n    console_unlock_env: SW_I311_OVERLAP_UNLOCK\n    console_unlock_idle_s: 0.01\n    timeout_s: 2.0\ntargets:\n  - {{com: COM0, profile: t, device_by_id: {BY_ID_A}}}\n"
        )
    } else {
        format!(
            "profiles:\n  t:\n    platform: shell\n    prompt_regex: '(?m)^# '\n    login_regex: 'login:'\n    password_regex: 'Password:'\n    user_env: SW_I311_OVERLAP_USER\n    pass_env: SW_I311_OVERLAP_PASS\n    timeout_s: 2.0\ntargets:\n  - {{com: COM0, profile: t, device_by_id: {BY_ID_A}}}\n"
        )
    };
    let env = Arc::new(FakeEnv::default());
    {
        let mut vars = env.vars.lock().unwrap();
        vars.insert("SW_I311_OVERLAP_USER".into(), "root".into());
        vars.insert("SW_I311_OVERLAP_PASS".into(), OVERLAP_SECRET.into());
        vars.insert("SW_I311_OVERLAP_UNLOCK".into(), OVERLAP_SECRET.into());
    }

    let serial = FakeSerialBackend::new();
    let device = serial.add_device(REAL_A);
    if console_unlock {
        device.push_rx(b"locked console\r\n");
    } else {
        device.on_tx_match("^$", Box::new(|_| b"\r\nlogin: ".to_vec()));
        device.on_tx_match("^root$", Box::new(|_| b"\r\nPassword: ".to_vec()));
    }
    let (secret_seen_tx, secret_seen) = crossbeam_channel::bounded(1);
    let (echo_release, echo_release_rx) = crossbeam_channel::bounded(1);
    device.on_tx_match(
        OVERLAP_SECRET,
        Box::new(move |_line| {
            let _ = secret_seen_tx.send(());
            echo_release_rx
                .recv_timeout(Duration::from_secs(30))
                .expect("測試釋放 prompt 前綴回覆")
        }),
    );
    device.on_tx_match(
        r"__READY__[0-9a-f]+",
        Box::new(|line: &[u8]| {
            let text = String::from_utf8_lossy(line);
            let token = text
                .split("__READY__")
                .nth(1)
                .unwrap_or("")
                .trim()
                .to_string();
            format!("__READY__{token}\r\n{OVERLAP_PROMPT}").into_bytes()
        }),
    );

    let service = ServiceRig::build_with_env_serial_console_observers(
        RigSpec {
            yaml: profile,
            devices: vec![(BY_ID_A.into(), REAL_A.into())],
            explicit_target_count: 1,
            allowlist: None,
            wait_ready: Vec::new(),
        },
        env,
        serial as Arc<dyn SerialBackend>,
        vec![event.engine.clone()],
    );
    assert!(service.svc.attach_engine(event.engine.clone()));
    if let Err(error) = secret_seen.recv_timeout(Duration::from_secs(10)) {
        panic!(
            "未觀察到 prompt 前綴 secret TX：{error}; tx={:?}; session={:?}",
            String::from_utf8_lossy(&device.tx_since(0)),
            service.session("COM0")
        );
    }
    LoginRig {
        service,
        device,
        echo_release,
        _event: event,
        _log_dir: log_dir,
    }
}

struct ControlledLoginRig {
    service: ServiceRig,
    device: sw_pal::fakes::serial::FakeDevice,
    secret_seen: crossbeam_channel::Receiver<()>,
    echo_release: crossbeam_channel::Sender<Vec<u8>>,
    ready_probe_seen: crossbeam_channel::Receiver<()>,
    _event: EventRig,
    _log_dir: tempfile::TempDir,
}

fn build_controlled_login_rig(secret: &str, prompt: &str) -> ControlledLoginRig {
    build_controlled_login_rig_with_pause(secret, prompt, false)
}

fn build_controlled_login_rig_with_pause(
    secret: &str,
    prompt: &str,
    pause_secret_tx: bool,
) -> ControlledLoginRig {
    let event = event_rig();
    let log_dir = tempfile::tempdir().expect("agent log 目錄位於 TMPDIR");
    let log_path = log_dir.path().join("agent-log");
    let profile = format!(
        "profiles:\n  t:\n    platform: shell\n    prompt_regex: '(?m)^{}'\n    login_regex: 'login:'\n    password_regex: 'Password:'\n    user_env: SW_I311_USER\n    pass_env: SW_I311_PASS\n    timeout_s: 2.0\ndefaults:\n  log_dir: '{}'\ntargets:\n  - {{com: COM0, profile: t, device_by_id: {BY_ID_A}}}\n",
        prompt,
        log_path.display()
    );
    let env = Arc::new(FakeEnv::default());
    {
        let mut vars = env.vars.lock().unwrap();
        vars.insert("SW_I311_USER".into(), "root".into());
        vars.insert("SW_I311_PASS".into(), secret.into());
    }

    let serial = FakeSerialBackend::new();
    let device = serial.add_device(REAL_A);
    device.on_tx_match("^$", Box::new(|_| b"\r\nlogin: ".to_vec()));
    let password_prompt_device = device.clone();
    device.on_tx_match(
        "^root$",
        Box::new(move |_| {
            if pause_secret_tx {
                password_prompt_device.script_write(vec![FakeWriteStep::PauseBeforeSubmit]);
            }
            b"\r\nPassword: ".to_vec()
        }),
    );
    let (secret_seen_tx, secret_seen) = crossbeam_channel::bounded(1);
    let (echo_release, echo_release_rx) = crossbeam_channel::bounded(1);
    let secret_pattern = format!("^{secret}$");
    device.on_tx_match(
        &secret_pattern,
        Box::new(move |_| {
            let _ = secret_seen_tx.send(());
            echo_release_rx
                .recv_timeout(Duration::from_secs(30))
                .expect("測試釋放分段登入 secret 回顯")
        }),
    );
    let (ready_probe_seen_tx, ready_probe_seen) = crossbeam_channel::bounded(1);
    let prompt = prompt.to_string();
    device.on_tx_match(
        r"__READY__[0-9a-f]+",
        Box::new(move |line: &[u8]| {
            let text = String::from_utf8_lossy(line);
            let token = text
                .split("__READY__")
                .nth(1)
                .unwrap_or("")
                .trim()
                .to_string();
            let _ = ready_probe_seen_tx.send(());
            format!("__READY__{token}\r\n{prompt}").into_bytes()
        }),
    );

    let service = ServiceRig::build_with_env_serial_console_observers(
        RigSpec {
            yaml: profile,
            devices: vec![(BY_ID_A.into(), REAL_A.into())],
            explicit_target_count: 1,
            allowlist: None,
            wait_ready: Vec::new(),
        },
        env,
        serial as Arc<dyn SerialBackend>,
        vec![event.engine.clone()],
    );
    assert!(service.svc.attach_engine(event.engine.clone()));
    if pause_secret_tx {
        assert!(
            device.wait_paused(Duration::from_secs(10)),
            "受控登入 secret TX 未停在 OS 提交前；tx={:?}; session={:?}",
            String::from_utf8_lossy(&device.tx_since(0)),
            service.session("COM0")
        );
    } else if let Err(error) = secret_seen.recv_timeout(Duration::from_secs(10)) {
        panic!(
            "未觀察到受控登入 secret TX：{error}; tx={:?}; session={:?}",
            String::from_utf8_lossy(&device.tx_since(0)),
            service.session("COM0")
        );
    }
    ControlledLoginRig {
        service,
        device,
        secret_seen,
        echo_release,
        ready_probe_seen,
        _event: event,
        _log_dir: log_dir,
    }
}

struct ConsoleUnlockRig {
    service: ServiceRig,
    device: sw_pal::fakes::serial::FakeDevice,
    secret_seen: crossbeam_channel::Receiver<()>,
    echo_release: crossbeam_channel::Sender<Vec<u8>>,
    _event: EventRig,
    _log_dir: tempfile::TempDir,
}

struct ControlledConsoleUnlockRig {
    service: ServiceRig,
    device: sw_pal::fakes::serial::FakeDevice,
    secret_seen: crossbeam_channel::Receiver<()>,
    echo_release: crossbeam_channel::Sender<Vec<u8>>,
    ready_probe_seen: crossbeam_channel::Receiver<()>,
    _event: EventRig,
    _log_dir: tempfile::TempDir,
}

fn build_controlled_console_unlock_rig(secret: &str, prompt: &str) -> ControlledConsoleUnlockRig {
    build_controlled_console_unlock_rig_with_pause(secret, prompt, false)
}

fn build_controlled_console_unlock_rig_with_pause(
    secret: &str,
    prompt: &str,
    pause_secret_tx: bool,
) -> ControlledConsoleUnlockRig {
    let event = event_rig();
    let log_dir = tempfile::tempdir().expect("agent log 目錄位於 TMPDIR");
    let log_path = log_dir.path().join("agent-log");
    let profile = format!(
        "profiles:\n  t:\n    platform: shell\n    prompt_regex: '(?m)^{}'\n    console_unlock_env: SW_I311_UNLOCK\n    console_unlock_idle_s: 0.01\n    timeout_s: 2.0\ndefaults:\n  log_dir: '{}'\ntargets:\n  - {{com: COM0, profile: t, device_by_id: {BY_ID_A}}}\n",
        prompt,
        log_path.display()
    );
    let env = Arc::new(FakeEnv::default());
    env.vars
        .lock()
        .unwrap()
        .insert("SW_I311_UNLOCK".into(), secret.into());
    let serial = FakeSerialBackend::new();
    let device = serial.add_device(REAL_A);
    device.on_tx_reply(b"\n", prompt.as_bytes());
    let initial_prompt = prompt.to_string();
    device.on_tx_match(
        r"__READY__[0-9a-f]+",
        Box::new(move |line: &[u8]| {
            let text = String::from_utf8_lossy(line);
            let token = text
                .split("__READY__")
                .nth(1)
                .unwrap_or("")
                .trim()
                .to_string();
            format!("__READY__{token}\r\n{initial_prompt}").into_bytes()
        }),
    );
    let service = ServiceRig::build_with_env_serial_console_observers(
        RigSpec {
            yaml: profile,
            devices: vec![(BY_ID_A.into(), REAL_A.into())],
            explicit_target_count: 1,
            allowlist: None,
            wait_ready: vec!["COM0".into()],
        },
        env,
        serial as Arc<dyn SerialBackend>,
        vec![event.engine.clone()],
    );
    assert!(service.svc.attach_engine(event.engine.clone()));

    let (secret_seen_tx, secret_seen) = crossbeam_channel::bounded(1);
    let (echo_release, echo_release_rx) = crossbeam_channel::bounded(1);
    let (ready_probe_seen_tx, ready_probe_seen) = crossbeam_channel::bounded(1);
    device.clear_replies();
    device.on_tx_match("^reboot$", Box::new(|_| b"rebooting\r\n".to_vec()));
    let locked_prompt_device = device.clone();
    device.on_tx_match(
        "^$",
        Box::new(move |_| {
            if pause_secret_tx {
                locked_prompt_device.script_write(vec![FakeWriteStep::PauseBeforeSubmit]);
            }
            b"locked console\r\n".to_vec()
        }),
    );
    let secret_pattern = format!("^{secret}$");
    device.on_tx_match(
        &secret_pattern,
        Box::new(move |_| {
            let _ = secret_seen_tx.send(());
            echo_release_rx
                .recv_timeout(Duration::from_secs(30))
                .expect("測試釋放分段 console unlock 回顯")
        }),
    );
    let prompt = prompt.to_string();
    device.on_tx_match(
        r"__READY__[0-9a-f]+",
        Box::new(move |line: &[u8]| {
            let text = String::from_utf8_lossy(line);
            let token = text
                .split("__READY__")
                .nth(1)
                .unwrap_or("")
                .trim()
                .to_string();
            let _ = ready_probe_seen_tx.send(());
            format!("__READY__{token}\r\n{prompt}").into_bytes()
        }),
    );
    ControlledConsoleUnlockRig {
        service,
        device,
        secret_seen,
        echo_release,
        ready_probe_seen,
        _event: event,
        _log_dir: log_dir,
    }
}

fn build_console_unlock_recovery_rig() -> ConsoleUnlockRig {
    let event = event_rig();
    let log_dir = tempfile::tempdir().expect("agent log 目錄位於 TMPDIR");
    let log_path = log_dir.path().join("agent-log");
    let profile = format!(
        "profiles:\n  t:\n    platform: shell\n    prompt_regex: '(?m)^root@t:/# '\n    console_unlock_env: SW_I311_UNLOCK\n    console_unlock_idle_s: 0.01\n    timeout_s: 2.0\ndefaults:\n  log_dir: '{}'\ntargets:\n  - {{com: COM0, profile: t, device_by_id: {BY_ID_A}}}\n",
        log_path.display()
    );
    let env = Arc::new(FakeEnv::default());
    env.vars
        .lock()
        .unwrap()
        .insert("SW_I311_UNLOCK".into(), SECRET.into());
    let serial = FakeSerialBackend::new();
    let device = serial.add_device(REAL_A);
    device.on_tx_reply(b"\n", PROMPT.as_bytes());
    device.on_tx_match(
        r"__READY__[0-9a-f]+",
        Box::new(|line: &[u8]| {
            let text = String::from_utf8_lossy(line);
            let token = text
                .split("__READY__")
                .nth(1)
                .unwrap_or("")
                .trim()
                .to_string();
            format!("__READY__{token}\r\n{PROMPT}").into_bytes()
        }),
    );
    let service = ServiceRig::build_with_env_serial_console_observers(
        RigSpec {
            yaml: profile,
            devices: vec![(BY_ID_A.into(), REAL_A.into())],
            explicit_target_count: 1,
            allowlist: None,
            wait_ready: vec!["COM0".into()],
        },
        env,
        serial as Arc<dyn SerialBackend>,
        vec![event.engine.clone()],
    );
    assert!(service.svc.attach_engine(event.engine.clone()));

    let (secret_seen_tx, secret_seen) = crossbeam_channel::bounded(1);
    let (echo_release, echo_release_rx) = crossbeam_channel::bounded(1);
    device.clear_replies();
    device.on_tx_match("^reboot$", Box::new(|_| b"rebooting\r\n".to_vec()));
    device.on_tx_match("^$", Box::new(|_| b"locked console\r\n".to_vec()));
    device.on_tx_match(
        SECRET,
        Box::new(move |_line| {
            let _ = secret_seen_tx.send(());
            echo_release_rx
                .recv_timeout(Duration::from_secs(30))
                .expect("測試釋放 console unlock 的 fake DUT echo")
        }),
    );
    ConsoleUnlockRig {
        service,
        device,
        secret_seen,
        echo_release,
        _event: event,
        _log_dir: log_dir,
    }
}

fn set_event_rules(rig: &ServiceRig) {
    let masked = "*".repeat(SECRET.len());
    for (name, needle) in [("masked", masked), ("plaintext", SECRET.into())] {
        let rule = format!(
            r#"{{"schema_version":1,"owner":"o","name":"{name}","kind":"tool","selectors":["COM0"],"pattern":{{"kind":"contains","value":"{needle}"}},"handler":{{"exec":["true"]}}}}"#
        );
        assert_eq!(
            rig.call("event.rule_set", &rule).get("ok"),
            Some(&PyValue::Bool(true)),
            "安裝 {name} event rule"
        );
    }
}

fn start_log(rig: &ServiceRig) -> String {
    let log = rig.call("session.log_start", r#"{"selector":"COM0"}"#);
    assert_eq!(log.get("ok"), Some(&PyValue::Bool(true)), "{log:?}");
    log.get("log_path")
        .and_then(PyValue::as_str)
        .expect("log_path")
        .to_string()
}

fn register_secret_echo(rig: &ServiceRig, secret: &str) -> Vec<PyValue> {
    let mut rows = Vec::new();
    support::wait_until("event observer 記錄 secret 回顯", || {
        let tail = rig.call("event.tail", r#"{"n":-1}"#);
        rows = match tail.get("entries") {
            Some(PyValue::List(rows)) => rows.clone(),
            other => panic!("event.tail entries: {other:?}"),
        };
        rows.iter().any(|row| {
            row.get("type").and_then(PyValue::as_str) == Some("match_recorded")
                && row
                    .get("matched_text")
                    .and_then(PyValue::as_str)
                    .is_some_and(|text| {
                        text.contains(secret) || text.contains(&"*".repeat(secret.len()))
                    })
        })
    });
    rows
}

fn decode_wal_payloads(wal: &str) -> Vec<u8> {
    let mut decoded = Vec::new();
    for line in wal.lines() {
        let Some(start) = line.find("\"payload_b64\":\"") else {
            continue;
        };
        let encoded_start = start + "\"payload_b64\":\"".len();
        let Some(encoded_end) = line[encoded_start..].find('"') else {
            continue;
        };
        if let Ok(bytes) =
            a2b_base64_lenient(line[encoded_start..encoded_start + encoded_end].as_bytes())
        {
            decoded.extend_from_slice(&bytes);
        }
    }
    decoded
}

fn assert_secret_tx_meta(wal: &str) {
    let records = wal
        .lines()
        .filter_map(|line| sw_proto::pyjson::loads(line).ok())
        .filter(|row| {
            row.get("meta")
                .and_then(|meta| meta.get("event"))
                .and_then(PyValue::as_str)
                == Some("secret_tx")
        })
        .collect::<Vec<_>>();
    assert_eq!(
        records.len(),
        1,
        "每次登入 secret TX 應有一筆 META：{records:?}"
    );
    let row = &records[0];
    let meta = match row.get("meta") {
        Some(PyValue::Dict(meta)) => meta,
        other => panic!("secret_tx meta: {other:?}"),
    };
    assert_eq!(meta.0.len(), 2, "META 不得包含 secret 以外資訊：{meta:?}");
    assert_eq!(
        meta.get("secret_len").and_then(PyValue::as_i64),
        Some(SECRET.len() as i64)
    );
    assert_eq!(row.get("dir").and_then(PyValue::as_str), Some("META"));
    assert_eq!(
        row.get("source").and_then(PyValue::as_str),
        Some("system:secret")
    );
    assert_eq!(row.get("payload_b64").and_then(PyValue::as_str), Some(""));
}

fn assert_echo_outputs_masked(
    rig: &LoginRig,
    log_path: &str,
    console: Option<&sw_pal::fakes::console::FakeConsoleRef>,
) {
    let wal_text = String::from_utf8(
        rig.service
            .wfs
            .contents(&rig.service.wal.wal_path())
            .expect("raw.wal.ndjson"),
    )
    .expect("WAL UTF-8");
    assert!(
        !wal_text.contains(SECRET),
        "WAL JSON 外洩 secret：{wal_text:?}"
    );
    assert_secret_tx_meta(&wal_text);
    let wal = String::from_utf8(decode_wal_payloads(&wal_text)).expect("WAL payload UTF-8");
    let mirror = String::from_utf8(
        rig.service
            .wfs
            .contents(&rig.service.wal.mirror_path())
            .expect("raw.mirror.log"),
    )
    .expect("mirror UTF-8");
    let agent_log = std::fs::read_to_string(Path::new(log_path)).expect("agent log");
    let console_text = console.map(|io| String::from_utf8_lossy(&io.written()).into_owned());
    let events = register_secret_echo(&rig.service, SECRET);

    let mut sinks = vec![
        ("WAL", wal.as_str()),
        ("mirror", mirror.as_str()),
        ("agent log", agent_log.as_str()),
    ];
    if let Some(text) = console_text.as_deref() {
        sinks.push(("human console", text));
    }
    for (sink, text) in sinks {
        assert!(!text.contains(SECRET), "{sink} 外洩 secret：{text:?}");
    }
    let matched_events = events
        .iter()
        .filter_map(|event| event.get("matched_text").and_then(PyValue::as_str))
        .filter(|text| text.contains(SECRET) || text.contains(&"*".repeat(SECRET.len())))
        .collect::<Vec<_>>();
    assert!(
        !matched_events.is_empty(),
        "event 未記錄 secret echo：{events:?}"
    );
    for matched in matched_events {
        assert!(
            !matched.contains(SECRET),
            "event matched_text 外洩：{matched:?}"
        );
        assert!(
            matched.contains(&"*".repeat(SECRET.len())),
            "event 未保留等長遮罩：{matched:?}"
        );
    }
    assert!(
        wal.contains(&"*".repeat(SECRET.len())),
        "WAL 未保留等長遮罩"
    );
    assert!(
        mirror.contains(&"*".repeat(SECRET.len())),
        "mirror 未保留等長遮罩"
    );
    assert!(
        agent_log.contains(&"*".repeat(SECRET.len())),
        "agent log 未保留等長遮罩"
    );
    if let Some(text) = console_text.as_deref() {
        assert!(
            text.contains(&"*".repeat(SECRET.len())),
            "human console 未保留等長遮罩"
        );
    }
}

fn set_event_rules_for_secret(rig: &ServiceRig, secret: &str, include_plaintext: bool) {
    let mut rules = vec![("masked", "*".repeat(secret.len()))];
    if include_plaintext {
        rules.push(("plaintext", secret.to_string()));
    }
    for (name, needle) in rules {
        let rule = format!(
            r#"{{"schema_version":1,"owner":"o","name":"{name}","kind":"tool","selectors":["COM0"],"pattern":{{"kind":"contains","value":"{needle}"}},"handler":{{"exec":["true"]}}}}"#
        );
        assert_eq!(
            rig.call("event.rule_set", &rule).get("ok"),
            Some(&PyValue::Bool(true)),
            "安裝 {name} event rule"
        );
    }
}

fn login_sentinel_console(rig: &ServiceRig) -> sw_pal::fakes::console::FakeConsoleRef {
    support::wait_until("登入期間 sentinel console 建立", || {
        rig.console
            .as_ref()
            .is_some_and(|console| !console.created().is_empty())
    });
    rig.console
        .as_ref()
        .expect("fake PTY console factory")
        .created()
        .into_iter()
        .next()
        .expect("登入期間 sentinel console")
        .1
}

fn wait_controlled_rx(
    device: &sw_pal::fakes::serial::FakeDevice,
    expected_read: usize,
    label: &str,
) {
    support::wait_until_within(label, Duration::from_secs(2), || {
        device.rx_bytes_read() >= expected_read
    });
}

fn assert_no_followup_tx(
    device: &sw_pal::fakes::serial::FakeDevice,
    ready_probe_seen: &crossbeam_channel::Receiver<()>,
    tx_before: usize,
    phase: &str,
) {
    assert!(
        ready_probe_seen
            .recv_timeout(Duration::from_millis(100))
            .is_err(),
        "真正 prompt 前已送出 ready probe（phase={phase}）；tx={:?}",
        String::from_utf8_lossy(&device.tx_since(tx_before))
    );
    assert_eq!(
        device.tx_len(),
        tx_before,
        "真正 prompt 前不得有後續 TX（phase={phase}）；tx={:?}",
        String::from_utf8_lossy(&device.tx_since(tx_before))
    );
}

fn assert_ready_probe_seen(ready_probe_seen: &crossbeam_channel::Receiver<()>, phase: &str) {
    ready_probe_seen
        .recv_timeout(Duration::from_secs(3))
        .unwrap_or_else(|error| {
            panic!("真正 prompt 後未送出 ready probe（phase={phase}）：{error}")
        });
}

fn assert_secret_tx_meta_for(wal: &str, secret: &str) {
    let records = wal
        .lines()
        .filter_map(|line| sw_proto::pyjson::loads(line).ok())
        .filter(|row| {
            row.get("meta")
                .and_then(|meta| meta.get("event"))
                .and_then(PyValue::as_str)
                == Some("secret_tx")
        })
        .collect::<Vec<_>>();
    assert_eq!(records.len(), 1, "secret TX 應只有一筆 META：{records:?}");
    let meta = records[0]
        .get("meta")
        .and_then(|value| match value {
            PyValue::Dict(dict) => Some(dict),
            _ => None,
        })
        .expect("secret_tx meta");
    assert_eq!(meta.0.len(), 2, "META 不得包含 secret 以外資訊：{meta:?}");
    assert_eq!(
        meta.get("secret_len").and_then(PyValue::as_i64),
        Some(secret.len() as i64)
    );
    assert_eq!(
        records[0].get("dir").and_then(PyValue::as_str),
        Some("META")
    );
    assert_eq!(
        records[0].get("source").and_then(PyValue::as_str),
        Some("system:secret")
    );
    assert_eq!(
        records[0].get("payload_b64").and_then(PyValue::as_str),
        Some("")
    );
}

struct ControlledOutputBaseline {
    wal_json_len: usize,
    wal_payload_len: usize,
    mirror_len: usize,
    agent_log_len: usize,
    console_len: usize,
    event_count: usize,
}

fn controlled_output_baseline(
    service: &ServiceRig,
    log_path: &str,
    console: &sw_pal::fakes::console::FakeConsoleRef,
) -> ControlledOutputBaseline {
    let wal = String::from_utf8(
        service
            .wfs
            .contents(&service.wal.wal_path())
            .expect("raw.wal.ndjson"),
    )
    .expect("WAL UTF-8");
    let mirror = service
        .wfs
        .contents(&service.wal.mirror_path())
        .expect("raw.mirror.log");
    let agent_log = std::fs::read(Path::new(log_path)).expect("agent log");
    let event_tail = service.call("event.tail", r#"{"n":-1}"#);
    let event_count = match event_tail.get("entries") {
        Some(PyValue::List(rows)) => rows.len(),
        other => panic!("event.tail entries: {other:?}"),
    };
    ControlledOutputBaseline {
        wal_json_len: wal.len(),
        wal_payload_len: decode_wal_payloads(&wal).len(),
        mirror_len: mirror.len(),
        agent_log_len: agent_log.len(),
        console_len: console.written_len(),
        event_count,
    }
}

struct FakeClockDriver {
    stop: Arc<AtomicBool>,
    join: Option<std::thread::JoinHandle<()>>,
}

impl FakeClockDriver {
    fn start(service: &ServiceRig) -> Self {
        let stop = Arc::new(AtomicBool::new(false));
        let clock = service.clock.clone();
        let manager = service.mgr.clone();
        let thread_stop = stop.clone();
        let join = std::thread::spawn(move || {
            while !thread_stop.load(Ordering::SeqCst) {
                clock.advance(Duration::from_millis(10));
                manager.on_tick();
                std::thread::sleep(Duration::from_millis(2));
            }
        });
        Self {
            stop,
            join: Some(join),
        }
    }

    fn stop(&mut self) {
        self.stop.store(true, Ordering::SeqCst);
        if let Some(join) = self.join.take() {
            join.join().expect("停止測試時鐘推進器");
        }
    }
}

impl Drop for FakeClockDriver {
    fn drop(&mut self) {
        self.stop();
    }
}

fn wait_for_session_error(service: &ServiceRig, expected: &str, label: &str) {
    let deadline = std::time::Instant::now() + Duration::from_secs(5);
    loop {
        if service.session("COM0").is_some_and(|session| {
            session.get("last_error").and_then(PyValue::as_str) == Some(expected)
        }) {
            break;
        }
        if std::time::Instant::now() >= deadline {
            panic!(
                "{label}: 未取得預期錯誤碼 {expected}; session={:?}",
                service.session("COM0")
            );
        }
        std::thread::sleep(Duration::from_millis(2));
    }
}

fn assert_no_secret_leak_since(
    service: &ServiceRig,
    log_path: &str,
    console: &sw_pal::fakes::console::FakeConsoleRef,
    secret: &str,
    baseline: &ControlledOutputBaseline,
) {
    let wal = String::from_utf8(
        service
            .wfs
            .contents(&service.wal.wal_path())
            .expect("raw.wal.ndjson"),
    )
    .expect("WAL UTF-8");
    let payload = decode_wal_payloads(&wal);
    let mirror = service
        .wfs
        .contents(&service.wal.mirror_path())
        .expect("raw.mirror.log");
    let agent_log = std::fs::read(Path::new(log_path)).expect("agent log");
    let console_bytes = console.written();
    for (sink, bytes) in [
        ("WAL JSON", &wal.as_bytes()[baseline.wal_json_len..]),
        ("WAL payload", &payload[baseline.wal_payload_len..]),
        ("mirror", &mirror[baseline.mirror_len..]),
        ("agent log", &agent_log[baseline.agent_log_len..]),
        ("human console", &console_bytes[baseline.console_len..]),
    ] {
        assert!(
            !bytes.windows(secret.len()).any(|window| window == secret.as_bytes()),
            "{sink} 在 secret TX 後新增明文，bytes={}",
            bytes.len()
        );
    }
    let event_tail = service.call("event.tail", r#"{"n":-1}"#);
    let rows = match event_tail.get("entries") {
        Some(PyValue::List(rows)) => rows,
        other => panic!("event.tail entries: {other:?}"),
    };
    for row in rows.iter().skip(baseline.event_count) {
        assert!(
            !format!("{row:?}").contains(secret),
            "event 在 secret TX 後新增明文 matched_text"
        );
    }
}

fn assert_controlled_outputs(
    service: &ServiceRig,
    log_path: &str,
    console: &sw_pal::fakes::console::FakeConsoleRef,
    secret: &str,
    echo_expected: bool,
    prompt_equals_secret: bool,
    baseline: &ControlledOutputBaseline,
) {
    let wal_text = String::from_utf8(
        service
            .wfs
            .contents(&service.wal.wal_path())
            .expect("raw.wal.ndjson"),
    )
    .expect("WAL UTF-8");
    assert!(
        !wal_text.contains(secret),
        "WAL JSON 外洩 secret：{wal_text:?}"
    );
    assert_secret_tx_meta_for(&wal_text, secret);
    let wal_payload = String::from_utf8(decode_wal_payloads(&wal_text)).expect("WAL payload UTF-8");
    let mirror = String::from_utf8(
        service
            .wfs
            .contents(&service.wal.mirror_path())
            .expect("raw.mirror.log"),
    )
    .expect("mirror UTF-8");
    let agent_log = std::fs::read_to_string(Path::new(log_path)).expect("agent log");
    let console_text = String::from_utf8_lossy(&console.written()).into_owned();
    let tail = service.call("event.tail", r#"{"n":-1}"#);
    let rows = match tail.get("entries") {
        Some(PyValue::List(rows)) => rows,
        other => panic!("event.tail entries: {other:?}"),
    };
    let event_text = format!("{rows:?}");

    if prompt_equals_secret {
        // 相同 bytes 同時是 public prompt；比較 secret TX 後的新增輸出，要求兩個 prompt
        // 都位於 CRLF 後（fake DUT prompt 與 ready probe 回覆），排除換行前的 secret echo。
        let prompt_line = format!("\n{secret}");
        let outputs = [
            (
                "WAL payload",
                String::from_utf8_lossy(&wal_payload.as_bytes()[baseline.wal_payload_len..])
                    .into_owned(),
            ),
            (
                "mirror",
                String::from_utf8_lossy(&mirror.as_bytes()[baseline.mirror_len..]).into_owned(),
            ),
            (
                "agent log",
                String::from_utf8_lossy(&agent_log.as_bytes()[baseline.agent_log_len..])
                    .into_owned(),
            ),
            (
                "console",
                String::from_utf8_lossy(&console_text.as_bytes()[baseline.console_len..])
                    .into_owned(),
            ),
        ];
        for (sink, text) in outputs {
            let normalized = text.replace("\r\n", "\n").replace('\r', "\n");
            assert_eq!(
                text.matches(secret).count(),
                2,
                "{sink} 應只有行終止符後 fake prompt 與 probe prompt 的同值 bytes：{text:?}"
            );
            assert_eq!(
                normalized.matches(&prompt_line).count(),
                2,
                "{sink} 的同值 prompt 必須位於行終止符後：{text:?}"
            );
        }
        assert!(
            !event_text.contains(secret),
            "event 外洩 secret：{event_text:?}"
        );
    } else {
        for (sink, text) in [
            ("WAL payload", wal_payload.as_str()),
            ("mirror", mirror.as_str()),
            ("agent log", agent_log.as_str()),
            ("console", console_text.as_str()),
            ("event", event_text.as_str()),
        ] {
            assert!(!text.contains(secret), "{sink} 外洩 secret：{text:?}");
        }
    }

    if echo_expected {
        let masked = "*".repeat(secret.len());
        for (sink, text) in [
            ("WAL payload", wal_payload.as_str()),
            ("mirror", mirror.as_str()),
            ("agent log", agent_log.as_str()),
            ("console", console_text.as_str()),
            ("event", event_text.as_str()),
        ] {
            assert!(text.contains(&masked), "{sink} 未保留等長遮罩：{text:?}");
        }
    } else if !prompt_equals_secret {
        assert!(
            !wal_payload.contains(&"*".repeat(secret.len())),
            "DUT 不回顯時 WAL 不應製造遮罩：{wal_payload:?}"
        );
    }
}

fn controlled_output_leaks(
    service: &ServiceRig,
    log_path: &str,
    console: &sw_pal::fakes::console::FakeConsoleRef,
    secret: &str,
) -> Vec<&'static str> {
    let wal_text = String::from_utf8(
        service
            .wfs
            .contents(&service.wal.wal_path())
            .expect("raw.wal.ndjson"),
    )
    .expect("WAL UTF-8");
    let wal_payload = String::from_utf8(decode_wal_payloads(&wal_text)).expect("WAL payload UTF-8");
    let mirror = service
        .wfs
        .contents(&service.wal.mirror_path())
        .expect("raw.mirror.log");
    let mirror = String::from_utf8_lossy(&mirror);
    let agent_log = std::fs::read_to_string(Path::new(log_path)).expect("agent log");
    let console_bytes = console.written();
    let console_text = String::from_utf8_lossy(&console_bytes);
    let event_text = format!("{:?}", service.call("event.tail", r#"{"n":-1}"#));
    [
        ("WAL JSON", wal_text.contains(secret)),
        ("WAL payload", wal_payload.contains(secret)),
        ("mirror", mirror.contains(secret)),
        ("agent log", agent_log.contains(secret)),
        ("console", console_text.contains(secret)),
        ("event", event_text.contains(secret)),
    ]
    .into_iter()
    .filter_map(|(sink, leaked)| leaked.then_some(sink))
    .collect()
}

fn run_controlled_login_case(
    secret: &str,
    prompt: &str,
    first_rx: &[u8],
    second_rx: &[u8],
    echo_expected: bool,
) {
    let rig = build_controlled_login_rig(secret, prompt);
    let log_path = start_log(&rig.service);
    let console = login_sentinel_console(&rig.service);
    set_event_rules_for_secret(&rig.service, secret, secret != prompt);
    rig.echo_release
        .send(Vec::new())
        .expect("完成登入 secret TX 的 fake DUT barrier");
    let tx_before = rig.device.tx_len();
    let baseline = controlled_output_baseline(&rig.service, &log_path, &console);

    let read_before = rig.device.rx_bytes_read();
    rig.device.push_rx(first_rx);
    wait_controlled_rx(
        &rig.device,
        read_before + first_rx.len(),
        "reader 收到第一段登入 RX",
    );
    assert_no_followup_tx(
        &rig.device,
        &rig.ready_probe_seen,
        tx_before,
        "第一段登入 RX",
    );

    if !second_rx.is_empty() {
        let read_before = rig.device.rx_bytes_read();
        rig.device.push_rx(second_rx);
        wait_controlled_rx(
            &rig.device,
            read_before + second_rx.len(),
            "reader 收到第二段登入 RX",
        );
        assert_no_followup_tx(
            &rig.device,
            &rig.ready_probe_seen,
            tx_before,
            "第二段登入 RX",
        );
    }

    let read_before = rig.device.rx_bytes_read();
    rig.device.push_rx(prompt.as_bytes());
    wait_controlled_rx(
        &rig.device,
        read_before + prompt.len(),
        "reader 收到真正登入 prompt",
    );
    assert_ready_probe_seen(&rig.ready_probe_seen, "一般登入");
    rig.service.wait_state("COM0", "READY");
    assert_controlled_outputs(
        &rig.service,
        &log_path,
        &console,
        secret,
        echo_expected,
        secret == prompt,
        &baseline,
    );
}

fn run_controlled_console_unlock_case(
    secret: &str,
    prompt: &str,
    first_rx: &[u8],
    second_rx: &[u8],
    echo_expected: bool,
) {
    let rig = build_controlled_console_unlock_rig(secret, prompt);
    let log_path = start_log(&rig.service);
    let attach = rig
        .service
        .call("session.console_attach", r#"{"selector":"COM0"}"#);
    assert_eq!(attach.get("ok"), Some(&PyValue::Bool(true)), "{attach:?}");
    let console = rig
        .service
        .console
        .as_ref()
        .expect("fake PTY console")
        .created()
        .into_iter()
        .last()
        .expect("human console 已建立")
        .1;
    set_event_rules_for_secret(&rig.service, secret, secret != prompt);

    let stop_clock = Arc::new(AtomicBool::new(false));
    let driver_clock = rig.service.clock.clone();
    let driver_manager = rig.service.mgr.clone();
    let driver_stop = stop_clock.clone();
    let clock_driver = std::thread::spawn(move || {
        while !driver_stop.load(Ordering::SeqCst) {
            driver_clock.advance(Duration::from_millis(10));
            driver_manager.on_tick();
            std::thread::sleep(Duration::from_millis(2));
        }
    });
    let reboot = rig.service.call(
        "command.submit",
        r#"{"selector":"COM0","cmd":"reboot","timeout_s":1.0}"#,
    );
    let secret_result = rig.secret_seen.recv_timeout(Duration::from_secs(10));
    stop_clock.store(true, Ordering::SeqCst);
    clock_driver.join().expect("停止測試時鐘推進器");
    assert_eq!(reboot.get("ok"), Some(&PyValue::Bool(true)), "{reboot:?}");
    secret_result.unwrap_or_else(|error| {
        panic!(
            "reboot recovery 未送出受控 console unlock secret：{error}; tx={:?}; session={:?}",
            String::from_utf8_lossy(&rig.device.tx_since(0)),
            rig.service.session("COM0")
        )
    });
    rig.echo_release
        .send(Vec::new())
        .expect("完成 console unlock secret TX 的 fake DUT barrier");
    let tx_before = rig.device.tx_len();
    let baseline = controlled_output_baseline(&rig.service, &log_path, &console);

    let read_before = rig.device.rx_bytes_read();
    rig.device.push_rx(first_rx);
    wait_controlled_rx(
        &rig.device,
        read_before + first_rx.len(),
        "reader 收到第一段 console RX",
    );
    assert_no_followup_tx(
        &rig.device,
        &rig.ready_probe_seen,
        tx_before,
        "第一段 console unlock RX",
    );

    if !second_rx.is_empty() {
        let read_before = rig.device.rx_bytes_read();
        rig.device.push_rx(second_rx);
        wait_controlled_rx(
            &rig.device,
            read_before + second_rx.len(),
            "reader 收到第二段 console RX",
        );
        assert_no_followup_tx(
            &rig.device,
            &rig.ready_probe_seen,
            tx_before,
            "第二段 console unlock RX",
        );
    }

    let read_before = rig.device.rx_bytes_read();
    rig.device.push_rx(prompt.as_bytes());
    wait_controlled_rx(
        &rig.device,
        read_before + prompt.len(),
        "reader 收到真正 console prompt",
    );
    assert_ready_probe_seen(&rig.ready_probe_seen, "console unlock");
    support::wait_until("console unlock recovery 完成", || {
        rig.service
            .session("COM0")
            .and_then(|session| {
                session
                    .get("state")
                    .and_then(PyValue::as_str)
                    .map(str::to_string)
            })
            .as_deref()
            == Some("READY")
    });
    assert_controlled_outputs(
        &rig.service,
        &log_path,
        &console,
        secret,
        echo_expected,
        secret == prompt,
        &baseline,
    );
}

#[test]
fn issue311_login_secret_equals_prompt_without_echo_waits_for_line_boundary() {
    const SECRET: &str = "# ";
    let rig = build_controlled_login_rig(SECRET, SECRET);
    let log_path = start_log(&rig.service);
    let console = login_sentinel_console(&rig.service);
    set_event_rules_for_secret(&rig.service, SECRET, true);
    let baseline = controlled_output_baseline(&rig.service, &log_path, &console);

    rig.echo_release.send(Vec::new()).expect("DUT 不回顯 secret");
    let read_before = rig.device.rx_bytes_read();
    rig.device.push_rx(b"\r\n");
    wait_controlled_rx(&rig.device, read_before + 2, "等於 prompt 的 secret 前行終止符");
    let read_before = rig.device.rx_bytes_read();
    rig.device.push_rx(SECRET.as_bytes());
    wait_controlled_rx(
        &rig.device,
        read_before + SECRET.len(),
        "不回顯 secret 時的同值 prompt",
    );

    let mut clock_driver = FakeClockDriver::start(&rig.service);
    wait_for_session_error(
        &rig.service,
        "SHELL_PROMPT_TIMEOUT",
        "等於 prompt 的 secret 應 fail-safe 逾時",
    );
    clock_driver.stop();
    assert_no_secret_leak_since(&rig.service, &log_path, &console, SECRET, &baseline);
}

#[test]
fn issue311_login_prompt_prefix_secret_echo_does_not_advance_early() {
    run_controlled_login_case("# secret", "# ", b"# ", b"secret\r\n", true);
}

#[test]
fn issue311_login_split_secret_echo_is_masked_until_line_end() {
    run_controlled_login_case(SECRET, PROMPT, b"i311", b"secret\r\n", true);
}

#[test]
fn issue311_login_prompt_after_line_boundary_without_echo_is_visible() {
    run_controlled_login_case(SECRET, PROMPT, b"\r\n", b"", false);
}

#[test]
fn issue311_console_unlock_secret_equals_prompt_without_echo_waits_for_line_boundary() {
    const SECRET: &str = "# ";
    let rig = build_controlled_console_unlock_rig(SECRET, SECRET);
    let log_path = start_log(&rig.service);
    let attach = rig
        .service
        .call("session.console_attach", r#"{"selector":"COM0"}"#);
    assert_eq!(attach.get("ok"), Some(&PyValue::Bool(true)), "{attach:?}");
    let console = rig
        .service
        .console
        .as_ref()
        .expect("fake PTY console")
        .created()
        .into_iter()
        .last()
        .expect("human console 已建立")
        .1;
    set_event_rules_for_secret(&rig.service, SECRET, true);

    let mut clock_driver = FakeClockDriver::start(&rig.service);
    let reboot = rig.service.call(
        "command.submit",
        r#"{"selector":"COM0","cmd":"reboot","timeout_s":1.0}"#,
    );
    assert_eq!(reboot.get("ok"), Some(&PyValue::Bool(true)), "{reboot:?}");
    rig.secret_seen
        .recv_timeout(Duration::from_secs(10))
        .expect("console unlock secret 已送出");
    let baseline = controlled_output_baseline(&rig.service, &log_path, &console);
    rig.echo_release
        .send(b"\r\n# ".to_vec())
        .expect("DUT 不回顯 secret，只回傳同值 prompt");
    wait_for_session_error(
        &rig.service,
        "CONSOLE_UNLOCK_PROMPT_TIMEOUT",
        "等於 prompt 的 console unlock secret 應 fail-safe 逾時",
    );
    clock_driver.stop();
    assert_no_secret_leak_since(&rig.service, &log_path, &console, SECRET, &baseline);
}

#[test]
fn issue311_console_unlock_prompt_prefix_secret_echo_does_not_advance_early() {
    run_controlled_console_unlock_case("# secret", "# ", b"# ", b"secret\r\n", true);
}

#[test]
fn issue311_console_unlock_split_secret_echo_is_masked_until_line_end() {
    run_controlled_console_unlock_case(SECRET, PROMPT, b"i311", b"secret\r\n", true);
}

#[test]
fn issue311_console_unlock_prompt_after_line_boundary_without_echo_is_visible() {
    run_controlled_console_unlock_case(SECRET, PROMPT, b"\r\n", b"", false);
}

#[test]
fn issue311_login_newline_before_secret_submit_does_not_end_echo_guard() {
    const SECRET: &str = "# secret";
    const PROMPT: &str = "# ";
    let rig = build_controlled_login_rig_with_pause(SECRET, PROMPT, true);
    let log_path = start_log(&rig.service);
    let console = login_sentinel_console(&rig.service);
    set_event_rules_for_secret(&rig.service, SECRET, true);
    let baseline = controlled_output_baseline(&rig.service, &log_path, &console);

    let tx_before_secret = rig.device.tx_len();
    let read_before = rig.device.rx_bytes_read();
    rig.device.push_rx(b"\r\n");
    wait_controlled_rx(&rig.device, read_before + 2, "登入 secret 提交前的行終止符");
    assert_eq!(
        rig.device.tx_len(),
        tx_before_secret,
        "secret 尚未提交時不得有後續 TX"
    );

    rig.device.release_pause();
    rig.secret_seen
        .recv_timeout(Duration::from_secs(3))
        .expect("secret 已越過 OS 提交點");
    let tx_before_echo = tx_before_secret + SECRET.len() + 1;
    let echo = b"# secret\r\n";
    let read_before = rig.device.rx_bytes_read();
    rig.echo_release
        .send(echo.to_vec())
        .expect("送出登入 secret echo");
    wait_controlled_rx(
        &rig.device,
        read_before + echo.len(),
        "reader 收到登入 secret echo",
    );
    let _ = register_secret_echo(&rig.service, SECRET);
    let leaks = controlled_output_leaks(&rig.service, &log_path, &console, SECRET);
    let early_probe = rig
        .ready_probe_seen
        .recv_timeout(Duration::from_millis(100))
        .is_ok();
    let early_tx = rig.device.tx_len();

    let prompt = PROMPT.as_bytes();
    let read_before = rig.device.rx_bytes_read();
    rig.device.push_rx(prompt);
    wait_controlled_rx(&rig.device, read_before + prompt.len(), "真正登入 prompt");
    if !early_probe {
        assert_ready_probe_seen(&rig.ready_probe_seen, "一般登入真實 prompt");
    }
    rig.service.wait_state("COM0", "READY");
    assert_controlled_outputs(
        &rig.service,
        &log_path,
        &console,
        SECRET,
        true,
        false,
        &baseline,
    );
    assert!(
        !early_probe && early_tx == tx_before_echo && leaks.is_empty(),
        "提交前換行後的 secret echo 必須保持遮罩，且真 prompt 前零後續 TX；early_probe={early_probe}; tx={:?}; leaks={leaks:?}",
        String::from_utf8_lossy(&rig.device.tx_since(tx_before_echo))
    );
}

#[test]
fn issue311_login_echo_is_masked_when_crlf_is_buffered_at_secret_submit() {
    const SECRET: &str = "# secret";
    const PROMPT: &str = "# ";
    let rig = build_controlled_login_rig_with_pause(SECRET, PROMPT, true);
    let log_path = start_log(&rig.service);
    let console = login_sentinel_console(&rig.service);
    set_event_rules_for_secret(&rig.service, SECRET, true);
    let baseline = controlled_output_baseline(&rig.service, &log_path, &console);

    let device = rig.device.clone();
    rig.device
        .on_os_submit(Box::new(move || device.push_rx(b"\r\n")));
    let read_before = rig.device.rx_bytes_read();
    rig.device.release_pause();
    rig.secret_seen
        .recv_timeout(Duration::from_secs(3))
        .expect("secret TX 已抵達 fake OS submit，CRLF 先前已放入 RX buffer");
    let secret_echo = b"# secret\r\n";
    rig.echo_release
        .send(secret_echo.to_vec())
        .expect("釋放受控 secret echo");
    wait_controlled_rx(
        &rig.device,
        read_before + 2 + secret_echo.len(),
        "reader 消耗 submit 前已緩衝的 CRLF 與 secret echo",
    );
    let leaks = controlled_output_leaks(&rig.service, &log_path, &console, SECRET);
    assert!(
        leaks.is_empty(),
        "OS submit 前已緩衝的 CRLF 不得讓 secret echo 明文發布：{leaks:?}"
    );

    let prompt_before = rig.device.rx_bytes_read();
    rig.device.push_rx(PROMPT.as_bytes());
    wait_controlled_rx(
        &rig.device,
        prompt_before + PROMPT.len(),
        "reader 收到真正登入 prompt",
    );
    assert_ready_probe_seen(&rig.ready_probe_seen, "CRLF buffer 交錯登入");
    rig.service.wait_state("COM0", "READY");
    assert_no_secret_leak_since(&rig.service, &log_path, &console, SECRET, &baseline);
    assert_controlled_outputs(
        &rig.service,
        &log_path,
        &console,
        SECRET,
        true,
        false,
        &baseline,
    );
}

#[test]
fn issue311_console_unlock_newline_before_secret_submit_does_not_end_echo_guard() {
    const SECRET: &str = "# secret";
    const PROMPT: &str = "# ";
    let rig = build_controlled_console_unlock_rig_with_pause(SECRET, PROMPT, true);
    let log_path = start_log(&rig.service);
    let attach = rig
        .service
        .call("session.console_attach", r#"{"selector":"COM0"}"#);
    assert_eq!(attach.get("ok"), Some(&PyValue::Bool(true)), "{attach:?}");
    let console = rig
        .service
        .console
        .as_ref()
        .expect("fake PTY console")
        .created()
        .into_iter()
        .last()
        .expect("human console 已建立")
        .1;
    set_event_rules_for_secret(&rig.service, SECRET, true);

    let stop_clock = Arc::new(AtomicBool::new(false));
    let driver_clock = rig.service.clock.clone();
    let driver_manager = rig.service.mgr.clone();
    let driver_stop = stop_clock.clone();
    let clock_driver = std::thread::spawn(move || {
        while !driver_stop.load(Ordering::SeqCst) {
            driver_clock.advance(Duration::from_millis(10));
            driver_manager.on_tick();
            std::thread::sleep(Duration::from_millis(2));
        }
    });
    let rpc_service = rig.service.svc.clone();
    let (reboot_tx, reboot_rx) = crossbeam_channel::bounded(1);
    let reboot_thread = std::thread::spawn(move || {
        let params =
            match sw_proto::pyjson::loads(r#"{"selector":"COM0","cmd":"reboot","timeout_s":1.0}"#)
                .expect("reboot 參數")
            {
                PyValue::Dict(params) => params,
                other => panic!("reboot 參數必須是物件：{other:?}"),
            };
        let ok = rpc_service
            .rpc("command.submit", &params)
            .map(|reply| reply.get("ok") == Some(&PyValue::Bool(true)))
            .unwrap_or(false);
        let _ = reboot_tx.send(ok);
    });
    let paused = rig.device.wait_paused(Duration::from_secs(10));
    if !paused {
        stop_clock.store(true, Ordering::SeqCst);
        clock_driver.join().expect("停止測試時鐘推進器");
        panic!("console unlock secret TX 未停在 OS 提交前");
    }
    let baseline = controlled_output_baseline(&rig.service, &log_path, &console);
    let tx_before_secret = rig.device.tx_len();
    let read_before = rig.device.rx_bytes_read();
    rig.device.push_rx(b"\r\n");
    wait_controlled_rx(
        &rig.device,
        read_before + 2,
        "console unlock secret 提交前的行終止符",
    );
    assert_eq!(
        rig.device.tx_len(),
        tx_before_secret,
        "console unlock secret 尚未提交時不得有後續 TX"
    );

    rig.device.release_pause();
    rig.secret_seen
        .recv_timeout(Duration::from_secs(3))
        .expect("console unlock secret 已越過 OS 提交點");
    let tx_before_echo = tx_before_secret + SECRET.len() + 1;
    let echo = b"# secret\r\n";
    let read_before = rig.device.rx_bytes_read();
    rig.echo_release
        .send(echo.to_vec())
        .expect("送出 console unlock secret echo");
    wait_controlled_rx(
        &rig.device,
        read_before + echo.len(),
        "reader 收到 console unlock secret echo",
    );
    let _ = register_secret_echo(&rig.service, SECRET);
    let leaks = controlled_output_leaks(&rig.service, &log_path, &console, SECRET);
    let early_probe = rig
        .ready_probe_seen
        .recv_timeout(Duration::from_millis(100))
        .is_ok();
    let early_tx = rig.device.tx_len();

    let prompt = PROMPT.as_bytes();
    let read_before = rig.device.rx_bytes_read();
    rig.device.push_rx(prompt);
    wait_controlled_rx(
        &rig.device,
        read_before + prompt.len(),
        "真正 console unlock prompt",
    );
    if !early_probe {
        assert_ready_probe_seen(&rig.ready_probe_seen, "console unlock 真實 prompt");
    }
    support::wait_until("console unlock 回到 READY", || {
        rig.service
            .session("COM0")
            .and_then(|session| {
                session
                    .get("state")
                    .and_then(PyValue::as_str)
                    .map(str::to_string)
            })
            .as_deref()
            == Some("READY")
    });
    let reboot_ok = reboot_rx
        .recv_timeout(Duration::from_secs(10))
        .unwrap_or(false);
    reboot_thread.join().expect("完成 reboot RPC");
    stop_clock.store(true, Ordering::SeqCst);
    clock_driver.join().expect("停止測試時鐘推進器");
    assert!(reboot_ok, "reboot recovery command 應正常完成");
    assert_controlled_outputs(
        &rig.service,
        &log_path,
        &console,
        SECRET,
        true,
        false,
        &baseline,
    );
    assert!(
        !early_probe && early_tx == tx_before_echo && leaks.is_empty(),
        "提交前換行後的 console unlock secret echo 必須保持遮罩，且真 prompt 前零後續 TX；early_probe={early_probe}; tx={:?}; leaks={leaks:?}",
        String::from_utf8_lossy(&rig.device.tx_since(tx_before_echo))
    );
}

#[test]
fn issue311_login_echo_is_masked_at_rpc_and_every_rx_exit() {
    let rig = build_login_rig(false);
    let log_path = start_log(&rig.service);
    set_event_rules(&rig.service);

    rig.echo_release
        .send(format!("{SECRET}\r\n{PROMPT}").into_bytes())
        .expect("釋放一般密碼 echo");
    rig.service.wait_state("COM0", "READY");
    assert_echo_outputs_masked(&rig, &log_path, None);

    // 守衛解除後，相同字串屬普通 RX，必須原樣寫入。
    let before = rig.service.wal.current_seq();
    rig.device.push_rx(SECRET.as_bytes());
    support::wait_until("守衛外 RX 寫入 WAL", || {
        rig.service.wal.current_seq() > before
    });
    let wal = rig
        .service
        .wfs
        .contents(&rig.service.wal.wal_path())
        .unwrap();
    let wal_text = String::from_utf8(wal).unwrap();
    let decoded = decode_wal_payloads(&wal_text);
    assert!(decoded
        .windows(SECRET.len())
        .any(|window| window == SECRET.as_bytes()));
}

#[test]
fn issue311_console_unlock_echo_is_masked_in_last_error_detail() {
    let rig = build_login_rig(true);
    let log_path = start_log(&rig.service);
    set_event_rules(&rig.service);

    rig.echo_release
        .send(format!("{SECRET}\r\nlogin: \r\n{PROMPT}").into_bytes())
        .expect("釋放 console unlock echo");
    support::wait_until("console unlock attach 結算", || {
        rig.service
            .session("COM0")
            .and_then(|session| {
                session
                    .get("state")
                    .and_then(PyValue::as_str)
                    .map(str::to_string)
            })
            .is_some_and(|state| state != "ATTACHING")
    });
    let session = rig.service.session("COM0").expect("session");
    assert_eq!(
        session.get("last_error").and_then(PyValue::as_str),
        Some("LOGIN_REQUIRED"),
        "state={:?}; last_error={:?}",
        session.get("state"),
        session.get("last_error")
    );
    let detail = session
        .get("last_error_detail")
        .and_then(PyValue::as_str)
        .unwrap_or_default();
    assert!(
        !detail.contains(SECRET),
        "last_error_detail 外洩 secret：{detail:?}"
    );
    assert!(
        detail.contains(&"*".repeat(SECRET.len())),
        "last_error_detail 沒有包含實際回顯的等長遮罩：{detail:?}"
    );
    assert_echo_outputs_masked(&rig, &log_path, None);
}

#[test]
fn issue311_login_prompt_prefix_overlapping_secret_does_not_timeout() {
    let rig = build_prompt_prefix_overlap_rig(false);
    let log_path = start_log(&rig.service);
    let console = login_sentinel_console(&rig.service);
    const SECRET: &str = "# secret";
    set_event_rules_for_secret(&rig.service, SECRET, true);
    let baseline = controlled_output_baseline(&rig.service, &log_path, &console);
    let mut clock_driver = FakeClockDriver::start(&rig.service);
    rig.echo_release
        .send(b"\r\n# ".to_vec())
        .expect("DUT 不回顯 secret，只回傳以 secret 開頭的 prompt");
    wait_for_session_error(
        &rig.service,
        "SHELL_PROMPT_TIMEOUT",
        "secret 以 prompt 開頭且 DUT 不回顯時應 fail-safe 逾時",
    );
    clock_driver.stop();
    assert_no_secret_leak_since(&rig.service, &log_path, &console, SECRET, &baseline);
}

#[test]
fn issue311_console_unlock_prompt_prefix_overlapping_secret_does_not_timeout() {
    const SECRET: &str = "# secret";
    const PROMPT: &str = "# ";
    let rig = build_controlled_console_unlock_rig(SECRET, PROMPT);
    let log_path = start_log(&rig.service);
    let attach = rig
        .service
        .call("session.console_attach", r#"{"selector":"COM0"}"#);
    assert_eq!(attach.get("ok"), Some(&PyValue::Bool(true)), "{attach:?}");
    let console = rig
        .service
        .console
        .as_ref()
        .expect("fake PTY console")
        .created()
        .into_iter()
        .last()
        .expect("human console 已建立")
        .1;
    set_event_rules_for_secret(&rig.service, SECRET, true);
    let mut clock_driver = FakeClockDriver::start(&rig.service);
    let reboot = rig.service.call(
        "command.submit",
        r#"{"selector":"COM0","cmd":"reboot","timeout_s":1.0}"#,
    );
    assert_eq!(reboot.get("ok"), Some(&PyValue::Bool(true)), "{reboot:?}");
    rig.secret_seen
        .recv_timeout(Duration::from_secs(10))
        .expect("console unlock secret 已送出");
    let baseline = controlled_output_baseline(&rig.service, &log_path, &console);
    rig.echo_release
        .send(b"\r\n# ".to_vec())
        .expect("DUT 不回顯 secret，只回傳以 secret 開頭的 console prompt");
    wait_for_session_error(
        &rig.service,
        "CONSOLE_UNLOCK_PROMPT_TIMEOUT",
        "secret 以 console prompt 開頭且 DUT 不回顯時應 fail-safe 逾時",
    );
    clock_driver.stop();
    assert_no_secret_leak_since(&rig.service, &log_path, &console, SECRET, &baseline);
}

#[test]
fn issue311_console_unlock_echo_is_masked_in_human_console() {
    let rig = build_console_unlock_recovery_rig();
    let log_path = start_log(&rig.service);
    let attach = rig
        .service
        .call("session.console_attach", r#"{"selector":"COM0"}"#);
    assert_eq!(attach.get("ok"), Some(&PyValue::Bool(true)), "{attach:?}");
    let console = rig
        .service
        .console
        .as_ref()
        .expect("fake PTY console")
        .created()
        .into_iter()
        .last()
        .expect("human console 已建立")
        .1;
    set_event_rules(&rig.service);

    let stop_clock = Arc::new(AtomicBool::new(false));
    let driver_clock = rig.service.clock.clone();
    let driver_manager = rig.service.mgr.clone();
    let driver_stop = stop_clock.clone();
    let clock_driver = std::thread::spawn(move || {
        while !driver_stop.load(Ordering::SeqCst) {
            driver_clock.advance(Duration::from_millis(10));
            driver_manager.on_tick();
            std::thread::sleep(Duration::from_millis(2));
        }
    });
    let reboot = rig.service.call(
        "command.submit",
        r#"{"selector":"COM0","cmd":"reboot","timeout_s":1.0}"#,
    );
    let secret_result = rig.secret_seen.recv_timeout(Duration::from_secs(10));
    stop_clock.store(true, Ordering::SeqCst);
    clock_driver.join().expect("停止測試時鐘推進器");
    assert_eq!(reboot.get("ok"), Some(&PyValue::Bool(true)), "{reboot:?}");
    secret_result.unwrap_or_else(|error| {
        panic!(
            "reboot recovery 未送出 console unlock secret：{error}; tx_bytes={}; session={:?}",
            rig.device.tx_since(0).len(),
            rig.service.session("COM0")
        )
    });
    rig.echo_release
        .send(format!("{SECRET}\r\nlocked console\r\n").into_bytes())
        .expect("釋放 human console unlock echo");

    let mut rows = Vec::new();
    support::wait_until("console unlock event 記錄回顯", || {
        let tail = rig.service.call("event.tail", r#"{"n":-1}"#);
        rows = match tail.get("entries") {
            Some(PyValue::List(rows)) => rows.clone(),
            other => panic!("event.tail entries: {other:?}"),
        };
        rows.iter().any(|row| {
            row.get("type").and_then(PyValue::as_str) == Some("match_recorded")
                && row
                    .get("matched_text")
                    .and_then(PyValue::as_str)
                    .is_some_and(|text| {
                        text.contains(SECRET) || text.contains(&"*".repeat(SECRET.len()))
                    })
        })
    });
    let wal_text = String::from_utf8(
        rig.service
            .wfs
            .contents(&rig.service.wal.wal_path())
            .unwrap(),
    )
    .unwrap();
    assert!(
        !wal_text.contains(SECRET),
        "WAL JSON 外洩 secret：{wal_text:?}"
    );
    assert_secret_tx_meta(&wal_text);
    let wal = String::from_utf8(decode_wal_payloads(&wal_text)).unwrap();
    let mirror = String::from_utf8(
        rig.service
            .wfs
            .contents(&rig.service.wal.mirror_path())
            .unwrap(),
    )
    .unwrap();
    let agent_log = std::fs::read_to_string(Path::new(&log_path)).expect("agent log");
    let console_text = String::from_utf8_lossy(&console.written()).into_owned();
    for (sink, text) in [
        ("WAL", wal.as_str()),
        ("mirror", mirror.as_str()),
        ("agent log", agent_log.as_str()),
        ("human console", console_text.as_str()),
    ] {
        assert!(!text.contains(SECRET), "{sink} 外洩 secret：{text:?}");
        assert!(
            text.contains(&"*".repeat(SECRET.len())),
            "{sink} 未保留等長遮罩：{text:?}"
        );
    }
    let matched_events = rows
        .iter()
        .filter_map(|event| event.get("matched_text").and_then(PyValue::as_str))
        .filter(|text| text.contains(SECRET) || text.contains(&"*".repeat(SECRET.len())))
        .collect::<Vec<_>>();
    assert!(
        !matched_events.is_empty(),
        "event 未記錄 console unlock echo：{rows:?}"
    );
    for matched in matched_events {
        assert!(
            !matched.contains(SECRET),
            "event matched_text 外洩：{matched:?}"
        );
        assert!(
            matched.contains(&"*".repeat(SECRET.len())),
            "event 未保留等長遮罩：{matched:?}"
        );
    }
}

#[test]
fn issue311_secret_split_across_one_byte_rx_chunks_is_masked() {
    let rig = build_login_rig(false);
    let log_path = start_log(&rig.service);
    set_event_rules(&rig.service);

    rig.echo_release
        .send(Vec::new())
        .expect("先釋放 fake DUT 寫入");
    let mut read_bytes = rig.device.rx_bytes_read();
    for byte in SECRET.as_bytes() {
        // 只有在 fake reader 確認消耗前一 byte 後才送下一 byte，確定每次 SerialReader::read 都只收到一 byte。
        rig.device.push_rx(&[*byte]);
        let expected = read_bytes + 1;
        support::wait_until("reader 確認消耗一個 secret byte", || {
            rig.device.rx_bytes_read() >= expected
        });
        assert_eq!(
            rig.device.rx_bytes_read(),
            expected,
            "下一個 secret byte 送出前，reader 必須恰好消耗一 byte"
        );
        read_bytes = expected;
    }
    rig.device.push_rx(format!("\r\n{PROMPT}").as_bytes());
    rig.service.wait_state("COM0", "READY");
    assert_echo_outputs_masked(&rig, &log_path, None);
}
