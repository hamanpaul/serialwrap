//! issue 311：自動登入／console unlock 的 secret 回顯在所有 reader 出口均已遮罩。
#![cfg(feature = "test-fakes")]
mod support;

use std::path::Path;
use std::sync::atomic::{AtomicBool, Ordering};
use std::sync::Arc;
use crossbeam_channel::{Receiver, Sender};
use std::time::Duration;

use support::{RigSpec, ServiceRig, BY_ID_A, REAL_A};
use sw_core::uart_bridge::RxObserver;
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
    build_controlled_login_rig_with_step(
        secret,
        prompt,
        pause_secret_tx.then_some(FakeWriteStep::PauseBeforeSubmit),
        false,
    )
}

fn build_controlled_login_rig_with_result(
    secret: &str,
    prompt: &str,
    secret_tx_step: FakeWriteStep,
) -> ControlledLoginRig {
    build_controlled_login_rig_with_step(secret, prompt, Some(secret_tx_step), true)
}

fn build_controlled_login_rig_with_step(
    secret: &str,
    prompt: &str,
    secret_tx_step: Option<FakeWriteStep>,
    pause_after_secret_tx: bool,
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
    let submit_secret_crlf = Arc::new(AtomicBool::new(false));
    let submit_secret_crlf_hook = Arc::clone(&submit_secret_crlf);
    let submit_secret_crlf_device = device.clone();
    device.on_os_submit(Box::new(move || {
        if submit_secret_crlf_hook.swap(false, Ordering::SeqCst) {
            // 在 secret bytes 被 fake OS 接受前先把舊 CRLF 放入 RX queue，並讓實際 reader 等待受控釋放。
            submit_secret_crlf_device.hold_rx_reads();
            submit_secret_crlf_device.push_rx(b"\r\n");
        }
    }));
    let secret_tx_step_for_password_prompt = secret_tx_step.clone();
    let submit_secret_crlf_on_next_tx = Arc::clone(&submit_secret_crlf);
    device.on_tx_match(
        "^root$",
        Box::new(move |_| {
            if let Some(step) = &secret_tx_step_for_password_prompt {
                password_prompt_device.script_write(vec![step.clone()]);
                if pause_after_secret_tx {
                    submit_secret_crlf_on_next_tx.store(true, Ordering::SeqCst);
                }
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
            if pause_after_secret_tx {
                Vec::new()
            } else {
                echo_release_rx
                    .recv_timeout(Duration::from_secs(30))
                    .expect("測試釋放分段登入 secret 回顯")
            }
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
    if secret_tx_step.is_some() {
        assert!(
            device.wait_paused(Duration::from_secs(10)),
            "受控登入 secret TX 未停在指定提交點；tx={:?}; session={:?}",
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

struct UnblockFakeTxOnDrop(sw_pal::fakes::serial::FakeDevice);

impl Drop for UnblockFakeTxOnDrop {
    fn drop(&mut self) {
        self.0.release_pause();
        self.0.release_rx_reads();
    }
}

fn run_login_secret_echo_while_tx_report_is_pending(
    secret_tx_step: FakeWriteStep,
    expected_tx_suffix: &[u8],
    tx_result: &str,
) {
    let rig = build_controlled_login_rig_with_result(SECRET, PROMPT, secret_tx_step);
    let _unblock = UnblockFakeTxOnDrop(rig.device.clone());
    let log_path = start_log(&rig.service);
    let console = login_sentinel_console(&rig.service);
    set_event_rules_for_secret(&rig.service, SECRET, true);
    let baseline = controlled_output_baseline(&rig.service, &log_path, &console);

    let tx = rig.device.tx_since(0);
    assert!(
        tx.ends_with(expected_tx_suffix),
        "{tx_result} fake UART TX bytes 不符合受控部分提交；tx={tx:?}"
    );
    assert_eq!(
        rig.device.rx_bytes_pending(),
        2,
        "secret 提交前放入的 CRLF 必須留在 fake OS RX queue"
    );

    let read_before_echo = rig.device.rx_bytes_read();
    let echo = format!("{SECRET}\r\n").into_bytes();
    let queued = rig.device.rx_bytes_pending() + echo.len();
    rig.device.push_rx(&echo);
    rig.device.release_rx_reads();
    wait_controlled_rx(
        &rig.device,
        read_before_echo + queued,
        "正式 RX reader 消耗提交前 CRLF 與受控 secret echo",
    );
    let state = rig.service.session("COM0").expect("登入中的 session");
    assert_eq!(
        state.get("state").and_then(PyValue::as_str),
        Some("ATTACHING"),
        "TX 結果仍暫停，五出口檢查必須發生在登入清理之前：{state:?}"
    );
    let leaks = controlled_output_leaks(&rig.service, &log_path, &console, SECRET);
    assert!(
        leaks.is_empty(),
        "{tx_result} TX 回報尚未結算，正式 reader 五個出口都不得收到 secret 明文；leaks={leaks:?}"
    );
    assert_controlled_outputs(
        &rig.service,
        &log_path,
        &console,
        SECRET,
        true,
        false,
        &baseline,
    );

    rig.device.release_pause();
    support::wait_until("部分／未知 secret TX 結束登入流程", || {
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
    let crlf_buffered = Arc::new(AtomicBool::new(false));
    let crlf_buffered_in_submit = crlf_buffered.clone();
    rig.device.on_os_submit(Box::new(move || {
        if !crlf_buffered_in_submit.swap(true, Ordering::SeqCst) {
            device.push_rx(b"\r\n");
        }
    }));
    let read_before = rig.device.rx_bytes_read();
    rig.device.hold_rx_reads();
    rig.device.release_pause();
    rig.secret_seen
        .recv_timeout(Duration::from_secs(3))
        .expect("secret TX 已抵達 fake OS submit，CRLF 先前已放入 RX buffer");
    assert_eq!(
        rig.device.rx_bytes_read(),
        read_before,
        "secret submit 時，OS RX buffer 中的 CRLF 尚未被 reader 讀取"
    );
    assert_eq!(
        rig.device.rx_bytes_pending(),
        2,
        "secret submit 時，CRLF 必須仍留在 fake OS RX buffer"
    );
    rig.device.release_rx_reads();
    wait_controlled_rx(
        &rig.device,
        read_before + 2,
        "reader 在 secret submit 後才消耗已緩衝 CRLF",
    );
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
fn issue311_login_partial_secret_tx_masks_echo_before_login_cleanup() {
    run_login_secret_echo_while_tx_report_is_pending(
        FakeWriteStep::AcceptThenErrorPause(
            SECRET.len(),
            sw_pal::traits::PalError::io(5, "受控部分 TX"),
        ),
        SECRET.as_bytes(),
        "partial",
    );
}

#[test]
fn issue311_login_unknown_secret_tx_masks_echo_before_login_cleanup() {
    run_login_secret_echo_while_tx_report_is_pending(
        FakeWriteStep::UnknownPauseAfterSubmit {
            confirmed: 1,
            submitted: SECRET.len(),
            error: sw_pal::traits::PalError::io(5, "受控未知 TX"),
        },
        SECRET.as_bytes(),
        "unknown",
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

const ISSUE312_HUMAN_SECRET: &str = "manual-password-312";

struct Issue312HumanRig {
    service: ServiceRig,
    device: sw_pal::fakes::serial::FakeDevice,
    _event: Option<EventRig>,
    _log_dir: tempfile::TempDir,
}

fn build_issue312_human_rig(ready: bool) -> Issue312HumanRig {
    build_issue312_human_rig_with_unlock_prompt(ready, false)
}

fn build_issue312_human_rig_with_unlock_prompt(
    ready: bool,
    unlock_prompt: bool,
) -> Issue312HumanRig {
    build_issue312_human_rig_with_observers(ready, unlock_prompt, Vec::new())
}

fn build_issue312_human_rig_with_observers(
    ready: bool,
    unlock_prompt: bool,
    observers: Vec<Arc<dyn RxObserver>>,
) -> Issue312HumanRig {
    let event = event_rig();
    let log_dir = tempfile::tempdir().expect("agent log 目錄");
    let prompt_field = if unlock_prompt {
        "    password_regex: '$^'\n    console_unlock_env: SW_I312_UNLOCK\n"
    } else {
        "    password_regex: 'Password:'\n"
    };
    let profile = format!(
        "profiles:\n  t:\n    platform: {}\n    prompt_regex: '(?m)^root@t:/# '\n{}    timeout_s: 2.0\ndefaults:\n  log_dir: '{}'\ntargets:\n  - {{com: COM0, profile: t, device_by_id: {BY_ID_A}}}\n",
        if ready { "shell" } else { "passthrough" },
        prompt_field,
        log_dir.path().display()
    );
    let env = Arc::new(FakeEnv::default());
    let serial = FakeSerialBackend::new();
    let device = serial.add_device(REAL_A);
    if ready {
        support::install_shell_replies(&device);
    }
    let mut observers = observers;
    observers.push(event.engine.clone());
    let service = ServiceRig::build_with_env_serial_console_observers(
        RigSpec {
            yaml: profile,
            devices: vec![(BY_ID_A.into(), REAL_A.into())],
            explicit_target_count: 1,
            allowlist: None,
            wait_ready: if ready {
                vec!["COM0".into()]
            } else {
                Vec::new()
            },
        },
        env,
        serial as Arc<dyn SerialBackend>,
        observers,
    );
    assert!(service.svc.attach_engine(event.engine.clone()));
    if !ready {
        service.wait_state("COM0", "ATTACHED");
    }
    Issue312HumanRig {
        service,
        device,
        _event: Some(event),
        _log_dir: log_dir,
    }
}

struct PasswordPromptBarrier {
    seen: Sender<()>,
    release: Receiver<()>,
}

impl RxObserver for PasswordPromptBarrier {
    fn on_rx(&self, _com: &str, data: &[u8], _wal_seq: u64) {
        if data
            .windows(b"Password: ".len())
            .any(|part| part == b"Password: ")
        {
            let _ = self.seen.send(());
            self.release
                .recv_timeout(Duration::from_secs(10))
                .expect("測試釋放 password prompt reader barrier");
        }
    }
}

fn issue312_password_prompt_barrier() -> (Arc<dyn RxObserver>, Receiver<()>, Sender<()>) {
    let (seen_tx, seen_rx) = crossbeam_channel::bounded(1);
    let (release_tx, release_rx) = crossbeam_channel::bounded(1);
    (
        Arc::new(PasswordPromptBarrier {
            seen: seen_tx,
            release: release_rx,
        }),
        seen_rx,
        release_tx,
    )
}

struct IsolatedCrBarrier {
    armed: Arc<AtomicBool>,
    seen: Sender<()>,
    release: Receiver<()>,
}

impl RxObserver for IsolatedCrBarrier {
    fn on_rx(&self, _com: &str, data: &[u8], _wal_seq: u64) {
        if data == b"\r" && self.armed.swap(false, Ordering::SeqCst) {
            let _ = self.seen.send(());
            self.release
                .recv_timeout(Duration::from_secs(10))
                .expect("測試釋放 isolated CR reader barrier");
        }
    }
}

fn issue312_isolated_cr_barrier() -> (Arc<dyn RxObserver>, Arc<AtomicBool>, Receiver<()>, Sender<()>) {
    let (seen_tx, seen_rx) = crossbeam_channel::bounded(1);
    let (release_tx, release_rx) = crossbeam_channel::bounded(1);
    let armed = Arc::new(AtomicBool::new(false));
    (
        Arc::new(IsolatedCrBarrier {
            armed: armed.clone(),
            seen: seen_tx,
            release: release_rx,
        }),
        armed,
        seen_rx,
        release_tx,
    )
}

fn issue312_attach_console(
    rig: &Issue312HumanRig,
) -> (String, sw_pal::fakes::console::FakeConsoleRef) {
    let created_before = rig.service.console.as_ref().expect("fake PTY factory").created().len();
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
    support::wait_until("issue312 fake console 建立", || factory.created().len() > created_before);
    let io = factory.created()[created_before].1.clone();
    support::wait_until("issue312 console client 登記", || {
        matches!(rig.service.call("session.console_list", r#"{"selector":"COM0"}"#).get("consoles"),
            Some(PyValue::List(rows)) if rows.iter().any(|row| row.get("client_id").and_then(PyValue::as_str) == Some(client_id.as_str())))
    });
    (client_id, io)
}

fn issue312_wait_tx(device: &sw_pal::fakes::serial::FakeDevice, expected_len: usize, label: &str) {
    support::wait_until_within(label, Duration::from_secs(3), || device.tx_len() >= expected_len);
    assert_eq!(device.tx_len(), expected_len, "{label} 不得多送 UART bytes");
}

fn issue312_wait_console_prompt(console: &sw_pal::fakes::console::FakeConsoleRef, prompt: &[u8]) {
    support::wait_until_within(
        "human 輸入前自己的 console 已收到提示",
        Duration::from_secs(3),
        || {
            console
                .written()
                .windows(prompt.len())
                .any(|part| part == prompt)
        },
    );
}

fn issue312_records(response: &sw_proto::pyjson::PyDict) -> Vec<PyValue> {
    response
        .get("records")
        .and_then(PyValue::as_array)
        .expect("RPC records")
        .clone()
}

fn issue312_assert_secret_wal(rig: &Issue312HumanRig, secret: &str, client_id: &str, visible_tx: &[u8]) {
    issue312_assert_secret_wal_chunks(rig, secret, &[secret.len()], client_id, visible_tx);
}

fn issue312_assert_secret_wal_chunks(
    rig: &Issue312HumanRig,
    secret: &str,
    expected_chunk_lens: &[usize],
    client_id: &str,
    visible_tx: &[u8],
) {
    let source = format!("human:{client_id}");
    support::wait_until_within(
        "已確認 UART write 的 secret META 完成 WAL append",
        Duration::from_secs(3),
        || {
            let bytes = rig
                .service
                .wfs
                .contents(&rig.service.wal.wal_path())
                .expect("raw.wal.ndjson");
            String::from_utf8_lossy(&bytes)
                .lines()
                .filter_map(|line| sw_proto::pyjson::loads(line).ok())
                .filter(|row| {
                    row.get("dir").and_then(PyValue::as_str) == Some("META")
                        && row.get("source").and_then(PyValue::as_str)
                            == Some(source.as_str())
                        && row
                            .get("meta")
                            .and_then(|meta| meta.get("event"))
                            .and_then(PyValue::as_str)
                            == Some("secret_tx")
                })
                .count()
                >= expected_chunk_lens.len()
        },
    );
    let wal_text = String::from_utf8(
        rig.service
            .wfs
            .contents(&rig.service.wal.wal_path())
            .expect("raw.wal.ndjson"),
    )
    .expect("WAL UTF-8");
    assert!(!wal_text.contains(secret), "WAL JSON 外洩 human password：{wal_text:?}");
    let wal_payload = decode_wal_payloads(&wal_text);
    assert!(
        !String::from_utf8_lossy(&wal_payload).contains(secret),
        "WAL TX payload 外洩 human password：{wal_payload:?}"
    );
    for chunk in visible_tx
        .split(|&b| b == b'\r' || b == b'\n')
        .filter(|s| !s.is_empty())
    {
        let chunk_str = String::from_utf8_lossy(chunk);
        assert!(
            String::from_utf8_lossy(&wal_payload).contains(&*chunk_str),
            "提示外或換行後的反例 TX 應照常保留：{wal_payload:?}"
        );
    }
    let mirror = String::from_utf8(
        rig.service
            .wfs
            .contents(&rig.service.wal.mirror_path())
            .expect("raw.mirror.log"),
    )
    .expect("mirror UTF-8");
    assert!(!mirror.contains(secret), "mirror 外洩 human password：{mirror:?}");
    assert!(!mirror.is_empty(), "mirror 應保留可讀的 WAL 紀錄");

    for (method, params) in [
        ("log.tail_raw", r#"{"selector":"COM0","from_seq":0,"limit":500}"#),
        ("wal.range", r#"{"from_seq":0,"limit":500}"#),
    ] {
        let response = rig.service.call(method, params);
        assert_eq!(response.get("ok"), Some(&PyValue::Bool(true)), "{method}: {response:?}");
        let rendered = format!("{response:?}");
        assert!(!rendered.contains(secret), "{method} 外洩 human password：{rendered}");
        let records = issue312_records(&response);
        let public_human_tx = records
            .iter()
            .filter(|row| {
                row.get("dir").and_then(PyValue::as_str) == Some("TX")
                    && row.get("source").and_then(PyValue::as_str)
                        == Some(format!("human:{client_id}").as_str())
            })
            .flat_map(|row| row.get("payload_b64").and_then(PyValue::as_str).into_iter())
            .flat_map(|payload| a2b_base64_lenient(payload.as_bytes()).expect("RPC human TX payload"))
            .collect::<Vec<_>>();
        assert_eq!(public_human_tx, visible_tx, "{method} 應只省略密碼本文並保留 human TX 其他 bytes");
        for row in records {
            if let Some(payload) = row.get("payload_b64").and_then(PyValue::as_str) {
                let decoded = a2b_base64_lenient(payload.as_bytes()).expect("RPC payload base64");
                assert!(!String::from_utf8_lossy(&decoded).contains(secret), "{method} payload 外洩：{row:?}");
            }
        }
    }
    let tail_text = rig
        .service
        .call("log.tail_text", r#"{"selector":"COM0","from_seq":0,"limit":500}"#);
    let rendered = format!("{tail_text:?}");
    assert!(!rendered.contains(secret), "log.tail_text 外洩 human password：{rendered}");
    let text_lines = tail_text
        .get("lines")
        .and_then(PyValue::as_array)
        .expect("log.tail_text lines")
        .iter()
        .filter_map(PyValue::as_str)
        .collect::<Vec<_>>()
        .join("\n");
    assert!(!text_lines.contains(secret), "log.tail_text 內容外洩 human password：{text_lines:?}");

    let rows = wal_text
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
        .flat_map(|row| row.get("payload_b64").and_then(PyValue::as_str).into_iter())
        .flat_map(|payload| a2b_base64_lenient(payload.as_bytes()).expect("human TX payload"))
        .collect::<Vec<_>>();
    assert_eq!(human_tx, visible_tx, "human TX WAL 僅省略密碼內容，換行與 UART 行為要保留");
    let human_secret_metas = rows
        .iter()
        .filter(|row| {
            row.get("dir").and_then(PyValue::as_str) == Some("META")
                && row.get("source").and_then(PyValue::as_str) == Some(format!("human:{client_id}").as_str())
                && row
                    .get("meta")
                    .and_then(|meta| meta.get("event"))
                    .and_then(PyValue::as_str)
                    == Some("secret_tx")
        })
        .collect::<Vec<_>>();
    assert_eq!(
        human_secret_metas.len(),
        expected_chunk_lens.len(),
        "每個已寫 human secret chunk 恰有一筆 META：{human_secret_metas:?}"
    );
    let actual_chunk_lens = human_secret_metas
        .iter()
        .map(|row| {
            let meta = row.get("meta").expect("secret meta");
            let field_count = match meta {
                PyValue::Dict(fields) => fields.0.len(),
                other => panic!("secret meta 應為 dict：{other:?}"),
            };
            assert_eq!(field_count, 2, "META 只含事件與長度：{meta:?}");
            meta.get("secret_len")
                .and_then(PyValue::as_i64)
                .expect("secret_len") as usize
        })
        .collect::<Vec<_>>();
    assert_eq!(
        actual_chunk_lens, expected_chunk_lens,
        "逐筆 META 長度須對應 UART 寫出的 secret chunk"
    );
}

fn issue312_set_event_probes(rig: &Issue312HumanRig, needles: &[&str]) {
    for (index, needle) in needles.iter().enumerate() {
        for (suffix, value) in [
            ("plain", (*needle).to_string()),
            ("masked", "*".repeat(needle.len())),
        ] {
            let rule = format!(
                r#"{{"schema_version":1,"owner":"o","name":"i312-{suffix}-{index}","kind":"tool","selectors":["COM0"],"pattern":{{"kind":"contains","value":"{value}"}},"handler":{{"exec":["true"]}}}}"#
            );
            let response = rig.service.call("event.rule_set", &rule);
            assert_eq!(
                response.get("ok"),
                Some(&PyValue::Bool(true)),
                "event probe: {response:?}"
            );
        }
    }
    let enabled = rig
        .service
        .call("event.com_enable", r#"{"selector":"COM0"}"#);
    assert_eq!(
        enabled.get("ok"),
        Some(&PyValue::Bool(true)),
        "event probes enable: {enabled:?}"
    );
}

fn issue312_push_secret_echo(rig: &Issue312HumanRig, secret: &str) {
    let mut echo = secret.as_bytes().to_vec();
    echo.extend_from_slice(b"\r\n");
    rig.device.push_rx(&echo);
}

fn issue312_wait_secret_event(rig: &Issue312HumanRig, needles: &[&str]) {
    let deadline = std::time::Instant::now() + Duration::from_secs(10);
    loop {
        let tail = rig.service.call("event.tail", r#"{"n":-1}"#);
        let recorded = matches!(tail.get("entries"), Some(PyValue::List(rows)) if rows.iter().any(|row| {
            row.get("type").and_then(PyValue::as_str) == Some("match_recorded")
                && row.get("matched_text").and_then(PyValue::as_str).is_some_and(|text| {
                    needles.iter().any(|needle| {
                        text.contains(needle) || text.contains(&"*".repeat(needle.len()))
                    })
                })
        }));
        if recorded {
            return;
        }
        if std::time::Instant::now() >= deadline {
            let wal_bytes = rig
                .service
                .wfs
                .contents(&rig.service.wal.wal_path())
                .expect("raw.wal.ndjson");
            let wal = String::from_utf8_lossy(&wal_bytes);
            panic!("event 未記錄 human password echo：{tail:?}; WAL={wal}");
        }
        std::thread::sleep(Duration::from_millis(10));
    }
}

fn issue312_assert_all_sinks_masked(
    rig: &Issue312HumanRig,
    needles: &[&str],
    consoles: &[&sw_pal::fakes::console::FakeConsoleRef],
    log_path: &str,
) {
    issue312_wait_secret_event(rig, needles);
    issue312_assert_sinks_do_not_contain(rig, needles, consoles, log_path);
}

fn issue312_assert_sinks_do_not_contain(
    rig: &Issue312HumanRig,
    needles: &[&str],
    consoles: &[&sw_pal::fakes::console::FakeConsoleRef],
    log_path: &str,
) {
    let wal = String::from_utf8_lossy(
        &rig.service
            .wfs
            .contents(&rig.service.wal.wal_path())
            .expect("raw.wal.ndjson"),
    )
    .into_owned();
    let mirror = String::from_utf8_lossy(
        &rig.service
            .wfs
            .contents(&rig.service.wal.mirror_path())
            .expect("raw.mirror.log"),
    )
    .into_owned();
    let agent_log = std::fs::read_to_string(Path::new(log_path)).expect("agent log");
    let event_tail = rig.service.call("event.tail", r#"{"n":-1}"#);
    let event_text = format!("{event_tail:?}");
    let event_rows = match event_tail.get("entries") {
        Some(PyValue::List(rows)) => rows,
        other => panic!("event.tail entries: {other:?}"),
    };
    assert!(
        event_rows.iter().any(|row| {
            row.get("type").and_then(PyValue::as_str) == Some("match_recorded")
                && row.get("matched_text").and_then(PyValue::as_str).is_some()
        }),
        "event sink 必須實際記錄受控 echo：{event_tail:?}"
    );
    let decoded_wal = issue312_decode_wal_payloads(&wal);
    let console_texts = consoles
        .iter()
        .map(|console| String::from_utf8_lossy(&console.written()).into_owned())
        .collect::<Vec<_>>();
    let leaks = issue312_find_sink_leaks(
        &wal,
        &decoded_wal,
        &mirror,
        &event_text,
        &agent_log,
        &console_texts,
        needles,
    );
    assert!(leaks.is_empty(), "human password sink 外洩：{leaks:?}");
}

fn issue312_decode_wal_payloads(wal_json: &str) -> String {
    let wal_rows = wal_json
        .lines()
        .filter_map(|line| sw_proto::pyjson::loads(line).ok())
        .collect::<Vec<_>>();
    let decoded = wal_rows
        .iter()
        .filter_map(|row| row.get("payload_b64").and_then(PyValue::as_str))
        .flat_map(|payload| a2b_base64_lenient(payload.as_bytes()).expect("WAL payload base64"))
        .collect::<Vec<_>>();
    String::from_utf8_lossy(&decoded).into_owned()
}

fn issue312_find_sink_leaks(
    wal_json: &str,
    decoded_wal: &str,
    mirror: &str,
    event: &str,
    agent_log: &str,
    consoles: &[String],
    needles: &[&str],
) -> Vec<String> {
    let mut leaks = Vec::new();
    for needle in needles.iter().filter(|needle| !needle.is_empty()) {
        for (sink, text) in [
            ("WAL JSON", wal_json),
            ("mirror", mirror),
            ("event", event),
            ("agent log", agent_log),
        ] {
            if text.contains(needle) {
                leaks.push(format!("{sink} contains {needle:?}"));
            }
        }
        if decoded_wal.contains(needle) {
            leaks.push(format!("WAL payload contains {needle:?}"));
        }
        for (index, console) in consoles.iter().enumerate() {
            if console.contains(needle) {
                leaks.push(format!("console[{index}] contains {needle:?}"));
            }
        }
    }
    leaks
}

#[test]
fn issue312_sink_guard_detects_plaintext_mutations_in_every_sink() {
    let secret = "manual-password-312";
    let encoded = sw_proto::pycompat::b2a_base64(secret.as_bytes());
    let encoded_wal = format!(r#"{{"payload_b64":"{encoded}"}}"#);
    let decoded_wal = issue312_decode_wal_payloads(&encoded_wal);
    assert_eq!(decoded_wal, secret, "WAL fixture must decode the payload bytes");
    let mutations = [
        ("WAL JSON", secret, "", "", "", "", vec![]),
        ("WAL payload", &encoded_wal, &decoded_wal, "", "", "", vec![]),
        ("mirror", "", "", secret, "", "", vec![]),
        ("event", "", "", "", secret, "", vec![]),
        ("agent log", "", "", "", "", secret, vec![]),
        ("console", "", "", "", "", "", vec![secret.to_string()]),
    ];
    for (expected_sink, wal, decoded, mirror, event, agent_log, console_texts) in mutations {
        let leaks = issue312_find_sink_leaks(
            wal,
            decoded,
            mirror,
            event,
            agent_log,
            &console_texts,
            &[secret],
        );
        assert!(
            leaks.iter().any(|leak| leak.starts_with(expected_sink)),
            "{expected_sink} 洩漏突變未被 sink guard 偵測：{leaks:?}"
        );
    }
}

#[test]
fn issue312_raw_password_input_is_meta_only_until_enter() {
    let rig = build_issue312_human_rig(false);
    let log_path = start_log(&rig.service);
    let (client_id, console) = issue312_attach_console(&rig);
    issue312_set_event_probes(&rig, &[ISSUE312_HUMAN_SECRET]);
    let tx_start = rig.device.tx_len();

    console.push_input(b"before-312\r");
    issue312_wait_tx(&rig.device, tx_start + b"before-312\r".len(), "raw prompt 外輸入");
    let rx_before = rig.device.rx_bytes_read();
    rig.device.push_rx(b"\r\nPassword: ");
    wait_controlled_rx(&rig.device, rx_before + b"\r\nPassword: ".len(), "reader 收到 password prompt");
    issue312_wait_console_prompt(&console, b"Password: ");
    rig.service.settle();
    rig.device.set_loopback(true);

    let echo_rx_start = rig.device.rx_bytes_read();
    for byte in ISSUE312_HUMAN_SECRET.as_bytes() {
        let expected = rig.device.tx_len() + 1;
        console.push_input(&[*byte]);
        issue312_wait_tx(&rig.device, expected, "raw password 逐鍵送出");
    }
    let expected = rig.device.tx_len() + 1;
    console.push_input(b"\r");
    issue312_wait_tx(&rig.device, expected, "raw password 結尾 CR 送出");
    let echo_wal = [b"*".repeat(ISSUE312_HUMAN_SECRET.len()), b"\r".to_vec()].concat();
    support::wait_until("raw password echo 完成 WAL 記錄", || {
        let bytes = rig
            .service
            .wfs
            .contents(&rig.service.wal.wal_path())
            .expect("raw.wal.ndjson");
        String::from_utf8_lossy(&bytes)
            .lines()
            .filter_map(|line| sw_proto::pyjson::loads(line).ok())
            .any(|row| {
                row.get("dir").and_then(PyValue::as_str) == Some("RX")
                    && row
                        .get("payload_b64")
                        .and_then(PyValue::as_str)
                        .and_then(|payload| a2b_base64_lenient(payload.as_bytes()).ok())
                        .as_deref()
                        == Some(echo_wal.as_slice())
            })
    });
    wait_controlled_rx(
        &rig.device,
        echo_rx_start + ISSUE312_HUMAN_SECRET.len() + 1,
        "DUT 逐鍵回顯到 CR",
    );
    rig.device.push_rx(b"\n");
    wait_controlled_rx(
        &rig.device,
        echo_rx_start + ISSUE312_HUMAN_SECRET.len() + 2,
        "DUT 回顯行尾 LF，結束 event 行",
    );
    rig.service.settle();
    console.push_input(b"after-312");
    issue312_wait_tx(&rig.device, expected + b"after-312".len(), "raw 換行後輸入");
    rig.service.settle();
    support::wait_until("raw 換行後 TX WAL 落盤", || {
        let bytes = rig
            .service
            .wfs
            .contents(&rig.service.wal.wal_path())
            .expect("raw.wal.ndjson");
        String::from_utf8_lossy(&decode_wal_payloads(&String::from_utf8_lossy(&bytes)))
            .contains("after-312")
    });

    assert_eq!(
        rig.device.tx_since(tx_start),
        [b"before-312\r".as_slice(), ISSUE312_HUMAN_SECRET.as_bytes(), b"\rafter-312"].concat(),
        "送到 UART 的位元組不得改變"
    );
    issue312_assert_secret_wal_chunks(
        &rig,
        ISSUE312_HUMAN_SECRET,
        &vec![1; ISSUE312_HUMAN_SECRET.len()],
        &client_id,
        b"before-312\r\rafter-312",
    );
    issue312_assert_all_sinks_masked(&rig, &[ISSUE312_HUMAN_SECRET], &[&console], &log_path);
}

#[test]
fn issue312_console_unlock_prompt_without_password_regex_match_is_meta_only() {
    let rig = build_issue312_human_rig_with_unlock_prompt(false, true);
    let log_path = start_log(&rig.service);
    let (client_id, console) = issue312_attach_console(&rig);
    issue312_set_event_probes(&rig, &[ISSUE312_HUMAN_SECRET]);
    let tx_start = rig.device.tx_len();

    console.push_input(b"before-312\r");
    issue312_wait_tx(&rig.device, tx_start + b"before-312\r".len(), "unlock prompt 外輸入");
    let rx_before = rig.device.rx_bytes_read();
    rig.device.push_rx(b"Unlock passphrase: ");
    wait_controlled_rx(
        &rig.device,
        rx_before + b"Unlock passphrase: ".len(),
        "reader 收到 console unlock prompt",
    );
    issue312_wait_console_prompt(&console, b"Unlock passphrase: ");
    rig.service.settle();
    rig.device.set_loopback(true);

    let echo_rx_start = rig.device.rx_bytes_read();
    for byte in ISSUE312_HUMAN_SECRET.as_bytes() {
        let expected = rig.device.tx_len() + 1;
        console.push_input(&[*byte]);
        issue312_wait_tx(&rig.device, expected, "unlock password 逐鍵送出");
    }
    let expected = rig.device.tx_len() + 1;
    console.push_input(b"\r");
    issue312_wait_tx(&rig.device, expected, "unlock password 結尾 CR 送出");
    wait_controlled_rx(
        &rig.device,
        echo_rx_start + ISSUE312_HUMAN_SECRET.len() + 1,
        "DUT 逐鍵回顯到 CR",
    );
    rig.device.push_rx(b"\n");
    wait_controlled_rx(
        &rig.device,
        echo_rx_start + ISSUE312_HUMAN_SECRET.len() + 2,
        "DUT 回顯行尾 LF，結束 event 行",
    );
    console.push_input(b"after-312");
    issue312_wait_tx(&rig.device, expected + b"after-312".len(), "unlock 換行後輸入");
    rig.service.settle();
    support::wait_until("unlock 換行後 TX WAL 落盤", || {
        let bytes = rig
            .service
            .wfs
            .contents(&rig.service.wal.wal_path())
            .expect("raw.wal.ndjson");
        let wal_text = String::from_utf8_lossy(&bytes);
        let decoded = decode_wal_payloads(&wal_text);
        let payload = String::from_utf8_lossy(&decoded);
        payload.contains("after-312")
    });

    assert_eq!(
        rig.device.tx_since(tx_start),
        [b"before-312\r".as_slice(), ISSUE312_HUMAN_SECRET.as_bytes(), b"\rafter-312"].concat(),
        "送到 UART 的位元組不得改變"
    );
    issue312_assert_secret_wal_chunks(
        &rig,
        ISSUE312_HUMAN_SECRET,
        &vec![1; ISSUE312_HUMAN_SECRET.len()],
        &client_id,
        b"before-312\r\rafter-312",
    );
    issue312_assert_all_sinks_masked(&rig, &[ISSUE312_HUMAN_SECRET], &[&console], &log_path);
}

#[test]
fn issue312_line_buffer_password_input_is_meta_only_until_enter() {
    let rig = build_issue312_human_rig(true);
    let log_path = start_log(&rig.service);
    issue312_set_event_probes(&rig, &[ISSUE312_HUMAN_SECRET]);
    let (raw_id, raw_console) = issue312_attach_console(&rig);
    let (line_id, line_console) = issue312_attach_console(&rig);
    assert_ne!(raw_id, line_id);
    assert_eq!(
        rig.service
            .call("session.console_list", r#"{"selector":"COM0"}"#)
            .get("consoles")
            .and_then(PyValue::as_array)
            .expect("consoles")
            .iter()
            .find(|row| row.get("client_id").and_then(PyValue::as_str) == Some(line_id.as_str()))
            .and_then(|row| row.get("interactive_owner")),
        Some(&PyValue::Bool(false)),
        "第二個 console 應走 line-buffer"
    );
    let detached = rig.service.call(
        "session.console_detach",
        &format!(r#"{{"selector":"COM0","client_id":"{raw_id}"}}"#),
    );
    assert_eq!(detached.get("ok"), Some(&PyValue::Bool(true)), "{detached:?}");
    assert_eq!(
        rig.service
            .call("session.console_list", r#"{"selector":"COM0"}"#)
            .get("consoles")
            .and_then(PyValue::as_array)
            .expect("保留 line-buffer console")
            .iter()
            .find(|row| row.get("client_id").and_then(PyValue::as_str) == Some(line_id.as_str()))
            .and_then(|row| row.get("interactive_owner")),
        Some(&PyValue::Bool(false)),
        "移除 raw owner 後保留的 console 仍走 line-buffer"
    );
    rig.device.on_tx_match("^before-line-312$", Box::new(|_| b"\r\nroot@t:/# ".to_vec()));
    rig.device.on_tx_match(
        ISSUE312_HUMAN_SECRET,
        Box::new(|_| format!("{ISSUE312_HUMAN_SECRET}\r\nroot@t:/# ").into_bytes()),
    );
    rig.device.on_tx_match("^after-line-312$", Box::new(|_| b"\r\nroot@t:/# ".to_vec()));

    let tx_start = rig.device.tx_len();
    line_console.push_input(b"before-line-312\r");
    support::wait_until("line editor 收到 prompt 外輸入", || {
        String::from_utf8_lossy(&line_console.written()).contains("before-line-312")
    });
    issue312_wait_tx(&rig.device, tx_start + b"before-line-312\n".len(), "line prompt 外輸入");
    rig.service.settle();
    let rx_before = rig.device.rx_bytes_read();
    rig.device.push_rx(b"\r\nPassword: ");
    wait_controlled_rx(&rig.device, rx_before + b"\r\nPassword: ".len(), "reader 收到 line password prompt");
    issue312_wait_console_prompt(&line_console, b"Password: ");
    rig.service.settle();

    let secret_tx = [ISSUE312_HUMAN_SECRET.as_bytes(), b"\n"].concat();
    line_console.push_input(&[ISSUE312_HUMAN_SECRET.as_bytes(), b"\r"].concat());
    issue312_wait_tx(&rig.device, tx_start + b"before-line-312\n".len() + secret_tx.len(), "line-buffer password 送出");
    rig.service.settle();
    line_console.push_input(b"after-line-312\r");
    issue312_wait_tx(
        &rig.device,
        tx_start + b"before-line-312\n".len() + secret_tx.len() + b"after-line-312\n".len(),
        "line 換行後輸入",
    );
    rig.service.settle();

    let mut expected_uart = b"before-line-312\n".to_vec();
    expected_uart.extend_from_slice(&secret_tx);
    expected_uart.extend_from_slice(b"after-line-312\n");
    assert_eq!(rig.device.tx_since(tx_start), expected_uart, "送到 UART 的位元組不得改變");
    issue312_assert_secret_wal(
        &rig,
        ISSUE312_HUMAN_SECRET,
        &line_id,
        b"before-line-312\n\nafter-line-312\n",
    );
    issue312_assert_all_sinks_masked(
        &rig,
        &[ISSUE312_HUMAN_SECRET],
        &[&raw_console, &line_console],
        &log_path,
    );
}

#[test]
fn issue312_line_buffer_command_shaped_password_is_meta_only() {
    let rig = build_issue312_human_rig(true);
    let log_path = start_log(&rig.service);
    issue312_set_event_probes(&rig, &["vim"]);
    let (raw_id, raw_console) = issue312_attach_console(&rig);
    let (line_id, line_console) = issue312_attach_console(&rig);
    let detached = rig.service.call(
        "session.console_detach",
        &format!(r#"{{"selector":"COM0","client_id":"{raw_id}"}}"#),
    );
    assert_eq!(detached.get("ok"), Some(&PyValue::Bool(true)));

    rig.device.on_tx_match("^before-line-312$", Box::new(|_| b"\r\nroot@t:/# ".to_vec()));
    rig.device.on_tx_match("^vim$", Box::new(|_| b"vim\r\nroot@t:/# ".to_vec()));
    rig.device.on_tx_match("^after-cmd$", Box::new(|_| b"\r\nroot@t:/# ".to_vec()));

    let tx_start = rig.device.tx_len();
    line_console.push_input(b"before-line-312\r");
    issue312_wait_tx(&rig.device, tx_start + b"before-line-312\n".len(), "line prompt 外輸入");
    rig.service.settle();

    let rx_before = rig.device.rx_bytes_read();
    rig.device.push_rx(b"\r\nPassword: ");
    wait_controlled_rx(&rig.device, rx_before + b"\r\nPassword: ".len(), "reader 收到 password prompt");
    issue312_wait_console_prompt(&line_console, b"Password: ");
    rig.service.settle();

    let secret = "vim";
    line_console.push_input(b"vim\r");
    issue312_wait_tx(&rig.device, tx_start + b"before-line-312\n".len() + b"vim\n".len(), "line-buffer vim 密碼送出");
    rig.service.settle();

    let state = rig.service.call("session.get_state", r#"{"selector":"COM0"}"#);
    let session = match state.get("session") {
        Some(PyValue::Dict(session)) => session,
        other => panic!("interactive command session state: {other:?}"),
    };
    let interactive_id = session
        .get("interactive_session_id")
        .and_then(PyValue::as_str)
        .expect("vim 命令開啟 interactive lease");
    let closed = rig.service.call(
        "session.interactive_close",
        &format!(r#"{{"interactive_id":"{interactive_id}"}}"#),
    );
    assert_eq!(closed.get("ok"), Some(&PyValue::Bool(true)), "{closed:?}");

    line_console.push_input(b"after-cmd\r");
    issue312_wait_tx(
        &rig.device,
        tx_start + b"before-line-312\n".len() + b"vim\n".len() + b"after-cmd\n".len(),
        "line-buffer 換行後輸入送出",
    );
    rig.service.settle();

    issue312_assert_secret_wal(&rig, secret, &line_id, b"before-line-312\n\nafter-cmd\n");
    assert_eq!(rig.device.tx_since(tx_start), b"before-line-312\nvim\nafter-cmd\n", "送到 UART 的位元組不得改變");
    issue312_assert_all_sinks_masked(&rig, &[secret], &[&raw_console, &line_console], &log_path);
}

#[test]
fn issue312_split_utf8_prompt_and_isolated_cr_masking() {
    let log_dir = tempfile::tempdir().expect("agent log 目錄");
    let profile = format!(
        "profiles:\n  t:\n    platform: shell\n    prompt_regex: '(?m)^root@t:/# '\n    password_regex: '密碼[:：] *'\n    timeout_s: 2.0\ndefaults:\n  log_dir: '{}'\ntargets:\n  - {{com: COM0, profile: t, device_by_id: {BY_ID_A}}}\n",
        log_dir.path().display()
    );
    let env = Arc::new(FakeEnv::default());
    let serial = FakeSerialBackend::new();
    let device = serial.add_device(REAL_A);
    let event = event_rig();
    support::install_shell_replies(&device);
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
    let rig = Issue312HumanRig {
        service,
        device,
        _event: Some(event),
        _log_dir: log_dir,
    };
    let log_path = start_log(&rig.service);
    let (client_id, console) = issue312_attach_console(&rig);
    issue312_set_event_probes(&rig, &["secret888"]);
    let tx_start = rig.device.tx_len();

    console.push_input(b"before-312\r");
    issue312_wait_tx(&rig.device, tx_start + b"before-312\r".len(), "提示前輸入");
    rig.service.settle();

    // 跨 chunk 分段 UTF-8："密" 是 0xE5 0xAF 0x86
    let rx_before = rig.device.rx_bytes_read();
    rig.device.push_rx(b"\r\n\xe5");
    wait_controlled_rx(&rig.device, rx_before + 3, "第一段 utf8 chunk");
    rig.service.settle();

    let rx_before2 = rig.device.rx_bytes_read();
    let chunk2 = [b"\xaf\x86".as_slice(), "碼: ".as_bytes()].concat();
    rig.device.push_rx(&chunk2);
    wait_controlled_rx(&rig.device, rx_before2 + chunk2.len(), "第二段 utf8 chunk 拼出完整提示");
    issue312_wait_console_prompt(&console, "密碼: ".as_bytes());
    rig.service.settle();

    let secret = "secret888";
    for byte in secret.as_bytes() {
        let expected = rig.device.tx_len() + 1;
        console.push_input(&[*byte]);
        issue312_wait_tx(&rig.device, expected, "分段 utf8 後逐鍵送出");
    }
    let expected = rig.device.tx_len() + 1;
    console.push_input(b"\r");
    issue312_wait_tx(&rig.device, expected, "以 CR 換行");
    rig.service.settle();

    console.push_input(b"after-utf8\r");
    issue312_wait_tx(&rig.device, expected + b"after-utf8\r".len(), "CR 後普通輸入");
    rig.service.settle();
    let rx_before_echo = rig.device.rx_bytes_read();
    issue312_push_secret_echo(&rig, secret);
    wait_controlled_rx(
        &rig.device,
        rx_before_echo + secret.len() + 2,
        "reader 收到分段 UTF-8 後的 secret echo",
    );

    assert_eq!(
        rig.device.tx_since(tx_start),
        [b"before-312\r".as_slice(), secret.as_bytes(), b"\rafter-utf8\r"].concat(),
        "送到 UART 的位元組不得改變"
    );
    issue312_assert_secret_wal_chunks(
        &rig,
        secret,
        &vec![1; secret.len()],
        &client_id,
        b"before-312\r\rafter-utf8\r",
    );
    issue312_assert_all_sinks_masked(&rig, &[secret], &[&console], &log_path);
}

#[test]
fn issue312_reader_cr_clears_prompt_before_actor_and_preserves_general_tx() {
    let (observer, armed, cr_seen, release_cr) = issue312_isolated_cr_barrier();
    let rig = build_issue312_human_rig_with_observers(false, false, vec![observer]);
    let log_path = start_log(&rig.service);
    let (client_id, console) = issue312_attach_console(&rig);
    issue312_set_event_probes(&rig, &[ISSUE312_HUMAN_SECRET]);

    let mut rx_seen = rig.device.rx_bytes_read();
    rig.device.push_rx(b"\r\nPassword: ");
    rx_seen += b"\r\nPassword: ".len();
    wait_controlled_rx(&rig.device, rx_seen, "reader 收到第一個 password prompt");
    issue312_wait_console_prompt(&console, b"Password: ");
    rig.service.settle();

    let tx_start = rig.device.tx_len();
    console.push_input(&[ISSUE312_HUMAN_SECRET.as_bytes(), b"\r"].concat());
    issue312_wait_tx(
        &rig.device,
        tx_start + ISSUE312_HUMAN_SECRET.len() + 1,
        "第一個 prompt 的 secret 輸入",
    );
    issue312_push_secret_echo(&rig, ISSUE312_HUMAN_SECRET);
    rx_seen += ISSUE312_HUMAN_SECRET.len() + 2;
    wait_controlled_rx(&rig.device, rx_seen, "reader 收到第一個 secret echo");
    rig.service.settle();
    issue312_wait_secret_event(&rig, &[ISSUE312_HUMAN_SECRET]);

    rig.device.push_rx(b"\r\nPassword: ");
    rx_seen += b"\r\nPassword: ".len();
    wait_controlled_rx(&rig.device, rx_seen, "reader 收到第二個 password prompt");
    issue312_wait_console_prompt(&console, b"Password: ");
    rig.service.settle();

    armed.store(true, Ordering::SeqCst);
    rig.device.push_rx(b"\r");
    cr_seen
        .recv_timeout(Duration::from_secs(3))
        .expect("reader 已清除提示狀態；actor 尚未收到獨立 CR event");
    let ordinary = b"ordinary-after-reader-cr\r";
    console.push_input(ordinary);
    issue312_wait_tx(
        &rig.device,
        tx_start + ISSUE312_HUMAN_SECRET.len() + 1 + ordinary.len(),
        "reader 已清提示後的一般 raw TX",
    );
    release_cr
        .send(())
        .expect("釋放 isolated CR reader barrier");
    rx_seen += 1;
    wait_controlled_rx(&rig.device, rx_seen, "actor 收到獨立 CR event");
    rig.service.settle();

    assert_eq!(
        rig.device.tx_since(tx_start),
        [ISSUE312_HUMAN_SECRET.as_bytes(), b"\r", ordinary].concat(),
        "UART 上的 secret 與提示窗外一般 TX 位元組都不得改變"
    );
    issue312_assert_secret_wal(
        &rig,
        ISSUE312_HUMAN_SECRET,
        &client_id,
        &[b"\r".as_slice(), ordinary].concat(),
    );
    issue312_assert_all_sinks_masked(&rig, &[ISSUE312_HUMAN_SECRET], &[&console], &log_path);
}

#[test]
fn issue312_raw_password_without_enter_settles_meta_on_disconnect() {
    let rig = build_issue312_human_rig(false);
    let log_path = start_log(&rig.service);
    let (client_id, console) = issue312_attach_console(&rig);
    let partial_secret = b"partial-312";
    issue312_set_event_probes(&rig, &["partial-312"]);

    let rx_before = rig.device.rx_bytes_read();
    rig.device.push_rx(b"\r\nPassword: ");
    wait_controlled_rx(
        &rig.device,
        rx_before + b"\r\nPassword: ".len(),
        "reader 收到 password prompt",
    );
    issue312_wait_console_prompt(&console, b"Password: ");
    rig.service.settle();

    for byte in partial_secret {
        let expected = rig.device.tx_len() + 1;
        console.push_input(&[*byte]);
        issue312_wait_tx(&rig.device, expected, "raw 部分密碼");
    }
    issue312_assert_secret_wal_chunks(
        &rig,
        "partial-312",
        &vec![1; partial_secret.len()],
        &client_id,
        b"",
    );
    rig.service.settle();

    rig.device.hold_rx_reads();
    let echo_rx_start = rig.device.rx_bytes_read();
    issue312_push_secret_echo(&rig, "partial-312");
    support::wait_until("斷線前的 partial secret echo 留在 reader buffer", || {
        rig.device.rx_bytes_pending() == partial_secret.len() + 2
    });

    let detached = rig.service.call(
        "session.console_detach",
        &format!(r#"{{"selector":"COM0","client_id":"{client_id}"}}"#),
    );
    assert_eq!(detached.get("ok"), Some(&PyValue::Bool(true)));
    rig.service.settle();
    rig.device.release_rx_reads();
    wait_controlled_rx(
        &rig.device,
        echo_rx_start + partial_secret.len() + 2,
        "結算後 reader 遮罩未完成密碼的 echo",
    );

    let wal_text = String::from_utf8(
        rig.service
            .wfs
            .contents(&rig.service.wal.wal_path())
            .expect("raw.wal.ndjson"),
    )
    .expect("WAL UTF-8");
    assert!(
        !wal_text.contains("partial-312"),
        "WAL 不得含有未完成密碼明文：{wal_text}"
    );
    let rows = wal_text
        .lines()
        .filter_map(|line| sw_proto::pyjson::loads(line).ok())
        .collect::<Vec<_>>();
    let metas = rows
        .iter()
        .filter(|row| {
            row.get("dir").and_then(PyValue::as_str) == Some("META")
                && row
                    .get("meta")
                    .and_then(|meta| meta.get("event"))
                    .and_then(PyValue::as_str)
                    == Some("secret_tx")
        })
        .collect::<Vec<_>>();
    let actual_chunk_lens = metas
        .iter()
        .map(|row| {
            row.get("meta")
                .and_then(|meta| meta.get("secret_len"))
                .and_then(PyValue::as_i64)
                .expect("secret_len") as usize
        })
        .collect::<Vec<_>>();
    assert_eq!(
        actual_chunk_lens,
        vec![1; partial_secret.len()],
        "斷線不應延後或合併已寫 chunk 的 META：{metas:?}"
    );
    issue312_assert_all_sinks_masked(&rig, &["partial-312"], &[&console], &log_path);
}

#[test]
fn issue312_disconnect_halfway_clears_password_state_for_reconnect() {
    let rig = build_issue312_human_rig(false);
    let log_path = start_log(&rig.service);
    let (client_id, console) = issue312_attach_console(&rig);
    issue312_set_event_probes(&rig, &["abc"]);

    let rx_before = rig.device.rx_bytes_read();
    rig.device.push_rx(b"\r\nPassword: ");
    wait_controlled_rx(&rig.device, rx_before + b"\r\nPassword: ".len(), "reader 收到 password prompt");
    issue312_wait_console_prompt(&console, b"Password: ");
    rig.service.settle();

    for byte in b"abc" {
        let expected = rig.device.tx_len() + 1;
        console.push_input(&[*byte]);
        issue312_wait_tx(&rig.device, expected, "raw 部分密碼");
    }
    issue312_assert_secret_wal_chunks(&rig, "abc", &[1, 1, 1], &client_id, b"");
    rig.service.settle();

    rig.device.hold_rx_reads();
    let echo_rx_start = rig.device.rx_bytes_read();
    issue312_push_secret_echo(&rig, "abc");
    support::wait_until("明確 detach 前的 partial echo 留在 reader buffer", || {
        rig.device.rx_bytes_pending() == b"abc\r\n".len()
    });

    let detached = rig.service.call(
        "session.console_detach",
        &format!(r#"{{"selector":"COM0","client_id":"{client_id}"}}"#),
    );
    assert_eq!(detached.get("ok"), Some(&PyValue::Bool(true)));
    rig.service.settle();
    rig.device.release_rx_reads();
    wait_controlled_rx(
        &rig.device,
        echo_rx_start + b"abc\r\n".len(),
        "detach 結算後 reader 遮罩 partial echo",
    );

    let (client_id2, console2) = issue312_attach_console(&rig);
    let tx_start2 = rig.device.tx_len();

    console2.push_input(b"status\r");
    issue312_wait_tx(&rig.device, tx_start2 + b"status\r".len(), "重連後普通輸入");
    rig.service.settle();

    let wal_text = String::from_utf8(
        rig.service
            .wfs
            .contents(&rig.service.wal.wal_path())
            .expect("raw.wal.ndjson"),
    )
    .expect("WAL UTF-8");
    let wal_payload = decode_wal_payloads(&wal_text);
    assert!(
        String::from_utf8_lossy(&wal_payload).contains("status"),
        "重連後的普通 human 輸入應恢復明文記錄：{wal_text}"
    );
    let rows = wal_text
        .lines()
        .filter_map(|line| sw_proto::pyjson::loads(line).ok())
        .collect::<Vec<_>>();
    let human2_tx = rows
        .iter()
        .filter(|row| {
            row.get("dir").and_then(PyValue::as_str) == Some("TX")
                && row.get("source").and_then(PyValue::as_str)
                    == Some(format!("human:{client_id2}").as_str())
        })
        .flat_map(|row| row.get("payload_b64").and_then(PyValue::as_str).into_iter())
        .flat_map(|payload| a2b_base64_lenient(payload.as_bytes()).expect("human2 TX payload"))
        .collect::<Vec<_>>();
    assert_eq!(human2_tx, b"status\r", "新 console 的普通輸入應完整保留");
    issue312_assert_all_sinks_masked(&rig, &["abc"], &[&console, &console2], &log_path);
}

#[test]
fn issue312_reader_actor_console_interleave_masks_raw_password_before_rx_delivery() {
    let (observer, prompt_seen, release_reader) = issue312_password_prompt_barrier();
    let rig = build_issue312_human_rig_with_observers(false, false, vec![observer]);
    let log_path = start_log(&rig.service);
    let (client_id, console) = issue312_attach_console(&rig);
    issue312_set_event_probes(&rig, &[ISSUE312_HUMAN_SECRET]);

    let tx_start = rig.device.tx_len();
    let rx_start = rig.device.rx_bytes_read();
    rig.device.push_rx(b"\r\nPassword: ");
    prompt_seen
        .recv_timeout(Duration::from_secs(3))
        .expect("reader 已讀取提示；actor 尚未收到該 RX event");
    release_reader
        .send(())
        .expect("釋放 raw console password prompt reader");
    issue312_wait_console_prompt(&console, b"Password: ");

    console.push_input(&[ISSUE312_HUMAN_SECRET.as_bytes(), b"\r"].concat());
    issue312_wait_tx(
        &rig.device,
        tx_start + ISSUE312_HUMAN_SECRET.len() + 1,
        "reader 發布提示前送出 raw password",
    );
    issue312_push_secret_echo(&rig, ISSUE312_HUMAN_SECRET);
    wait_controlled_rx(
        &rig.device,
        rx_start + b"\r\nPassword: ".len() + ISSUE312_HUMAN_SECRET.len() + 2,
        "reader 發布 prompt 與遮罩後的 raw password echo",
    );
    rig.service.settle();
    issue312_wait_secret_event(&rig, &[ISSUE312_HUMAN_SECRET]);

    assert_eq!(
        rig.device.tx_since(tx_start),
        [ISSUE312_HUMAN_SECRET.as_bytes(), b"\r"].concat(),
        "raw console 的 UART bytes 不得改變"
    );
    issue312_assert_secret_wal(&rig, ISSUE312_HUMAN_SECRET, &client_id, b"\r");
    issue312_assert_all_sinks_masked(&rig, &[ISSUE312_HUMAN_SECRET], &[&console], &log_path);
}

#[test]
fn issue312_reader_actor_console_interleave_masks_line_buffer_password_before_rx_delivery() {
    let (observer, prompt_seen, release_reader) = issue312_password_prompt_barrier();
    let rig = build_issue312_human_rig_with_observers(true, false, vec![observer]);
    let log_path = start_log(&rig.service);
    let (raw_id, raw_console) = issue312_attach_console(&rig);
    let (line_id, line_console) = issue312_attach_console(&rig);
    assert_ne!(raw_id, line_id);
    let detached = rig.service.call(
        "session.console_detach",
        &format!(r#"{{"selector":"COM0","client_id":"{raw_id}"}}"#),
    );
    assert_eq!(
        detached.get("ok"),
        Some(&PyValue::Bool(true)),
        "{detached:?}"
    );
    issue312_set_event_probes(&rig, &[ISSUE312_HUMAN_SECRET]);
    rig.device.on_tx_match(
        &format!("^{ISSUE312_HUMAN_SECRET}$"),
        Box::new(|_| [ISSUE312_HUMAN_SECRET.as_bytes(), b"\r\nroot@t:/# "].concat()),
    );

    let tx_start = rig.device.tx_len();
    let rx_start = rig.device.rx_bytes_read();
    rig.device.push_rx(b"\r\nPassword: ");
    prompt_seen
        .recv_timeout(Duration::from_secs(3))
        .expect("reader 已讀取提示；actor 尚未收到該 RX event");
    release_reader
        .send(())
        .expect("釋放 line-buffer password prompt reader");
    issue312_wait_console_prompt(&line_console, b"Password: ");

    line_console.push_input(&[ISSUE312_HUMAN_SECRET.as_bytes(), b"\r"].concat());
    issue312_wait_tx(
        &rig.device,
        tx_start + ISSUE312_HUMAN_SECRET.len() + 1,
        "reader 發布提示前送出 line-buffer password",
    );
    wait_controlled_rx(
        &rig.device,
        rx_start + b"\r\nPassword: ".len() + ISSUE312_HUMAN_SECRET.len() + b"\r\nroot@t:/# ".len(),
        "reader 發布 prompt 與遮罩後的 line-buffer password echo",
    );
    rig.service.settle();
    issue312_wait_secret_event(&rig, &[ISSUE312_HUMAN_SECRET]);

    assert_eq!(
        rig.device.tx_since(tx_start),
        [ISSUE312_HUMAN_SECRET.as_bytes(), b"\n"].concat(),
        "line-buffer console 的 UART bytes 不得改變"
    );
    issue312_assert_secret_wal(&rig, ISSUE312_HUMAN_SECRET, &line_id, b"\n");
    issue312_assert_all_sinks_masked(
        &rig,
        &[ISSUE312_HUMAN_SECRET],
        &[&raw_console, &line_console],
        &log_path,
    );
}

#[test]
fn issue312_invalid_utf8_before_password_prompt_does_not_disable_masking() {
    let (observer, prompt_seen, release_reader) = issue312_password_prompt_barrier();
    let rig = build_issue312_human_rig_with_observers(false, false, vec![observer]);
    let log_path = start_log(&rig.service);
    let (client_id, console) = issue312_attach_console(&rig);
    issue312_set_event_probes(&rig, &[ISSUE312_HUMAN_SECRET]);

    let tx_start = rig.device.tx_len();
    let rx_start = rig.device.rx_bytes_read();
    let prompt = b"\xffPassword: ";
    rig.device.push_rx(prompt);
    prompt_seen
        .recv_timeout(Duration::from_secs(3))
        .expect("reader 已讀取含無效 byte 的提示；actor 尚未收到該 RX event");
    release_reader
        .send(())
        .expect("釋放含無效 byte 的 password prompt reader");
    issue312_wait_console_prompt(&console, b"Password: ");
    console.push_input(&[ISSUE312_HUMAN_SECRET.as_bytes(), b"\r"].concat());
    issue312_wait_tx(
        &rig.device,
        tx_start + ISSUE312_HUMAN_SECRET.len() + 1,
        "無效 UTF-8 後逐鍵送出 human password",
    );
    issue312_push_secret_echo(&rig, ISSUE312_HUMAN_SECRET);
    wait_controlled_rx(
        &rig.device,
        rx_start + prompt.len() + ISSUE312_HUMAN_SECRET.len() + 2,
        "reader 收到 password echo",
    );
    rig.service.settle();
    issue312_wait_secret_event(&rig, &[ISSUE312_HUMAN_SECRET]);

    assert_eq!(
        rig.device.tx_since(tx_start),
        [ISSUE312_HUMAN_SECRET.as_bytes(), b"\r"].concat(),
        "UART bytes 不得因 tracker 的 UTF-8 解碼改變"
    );
    issue312_assert_secret_wal(&rig, ISSUE312_HUMAN_SECRET, &client_id, b"\r");
    issue312_assert_all_sinks_masked(&rig, &[ISSUE312_HUMAN_SECRET], &[&console], &log_path);
}

#[test]
fn issue312_buffered_crlf_cannot_revoke_human_echo_mask() {
    let rig = build_issue312_human_rig(false);
    let log_path = start_log(&rig.service);
    let (client_id, console) = issue312_attach_console(&rig);
    issue312_set_event_probes(&rig, &[ISSUE312_HUMAN_SECRET]);

    let rx_start = rig.device.rx_bytes_read();
    rig.device.push_rx(b"\r\nPassword: ");
    wait_controlled_rx(
        &rig.device,
        rx_start + b"\r\nPassword: ".len(),
        "reader 收到 password prompt",
    );
    issue312_wait_console_prompt(&console, b"Password: ");
    rig.service.settle();

    rig.device.hold_rx_reads();
    let pending_rx_start = rig.device.rx_bytes_read();
    rig.device.push_rx(b"\r\n");
    support::wait_until("submit 前 CRLF 留在 fake OS RX buffer", || {
        rig.device.rx_bytes_pending() == 2
    });
    let tx_start = rig.device.tx_len();
    console.push_input(&[ISSUE312_HUMAN_SECRET.as_bytes(), b"\r"].concat());
    issue312_wait_tx(
        &rig.device,
        tx_start + ISSUE312_HUMAN_SECRET.len() + 1,
        "reader 尚未消耗緩衝 CRLF 時送出 human password",
    );
    issue312_push_secret_echo(&rig, ISSUE312_HUMAN_SECRET);
    rig.device.release_rx_reads();
    wait_controlled_rx(
        &rig.device,
        pending_rx_start + 2 + ISSUE312_HUMAN_SECRET.len() + 2,
        "reader 依序消耗 submit 前 CRLF 與 password echo",
    );
    rig.service.settle();
    issue312_wait_secret_event(&rig, &[ISSUE312_HUMAN_SECRET]);

    assert_eq!(
        rig.device.tx_since(tx_start),
        [ISSUE312_HUMAN_SECRET.as_bytes(), b"\r"].concat(),
        "UART bytes 不得因 reader 先消耗 CRLF 而改變"
    );
    issue312_assert_secret_wal(&rig, ISSUE312_HUMAN_SECRET, &client_id, b"\r");
    issue312_assert_all_sinks_masked(&rig, &[ISSUE312_HUMAN_SECRET], &[&console], &log_path);
}

#[test]
fn issue312_detaching_second_console_does_not_settle_raw_owner_secret() {
    let rig = build_issue312_human_rig(false);
    let log_path = start_log(&rig.service);
    let (owner_id, owner_console) = issue312_attach_console(&rig);
    let (other_id, other_console) = issue312_attach_console(&rig);
    issue312_set_event_probes(
        &rig,
        &["first-part-", "second-part", "first-part-second-part"],
    );

    let rx_start = rig.device.rx_bytes_read();
    rig.device.push_rx(b"\r\nPassword: ");
    wait_controlled_rx(
        &rig.device,
        rx_start + b"\r\nPassword: ".len(),
        "reader 收到雙 console password prompt",
    );
    issue312_wait_console_prompt(&owner_console, b"Password: ");
    rig.service.settle();
    let tx_start = rig.device.tx_len();
    owner_console.push_input(b"first-part-");
    issue312_wait_tx(
        &rig.device,
        tx_start + b"first-part-".len(),
        "raw owner 輸入部分 password",
    );

    let detached = rig.service.call(
        "session.console_detach",
        &format!(r#"{{"selector":"COM0","client_id":"{other_id}"}}"#),
    );
    assert_eq!(
        detached.get("ok"),
        Some(&PyValue::Bool(true)),
        "{detached:?}"
    );
    owner_console.push_input(b"second-part\r");
    issue312_wait_tx(
        &rig.device,
        tx_start + b"first-part-second-part\r".len(),
        "raw owner 完成同一 password 行",
    );
    issue312_push_secret_echo(&rig, "first-part-second-part");
    wait_controlled_rx(
        &rig.device,
        rx_start + b"\r\nPassword: ".len() + b"first-part-second-part\r\n".len(),
        "reader 收到雙 console password echo",
    );
    rig.service.settle();
    issue312_wait_secret_event(&rig, &["first-part-second-part"]);

    assert_eq!(
        rig.device.tx_since(tx_start),
        b"first-part-second-part\r",
        "送到 UART 的 password bytes 不得改變"
    );
    issue312_assert_secret_wal_chunks(
        &rig,
        "first-part-second-part",
        &[b"first-part-".len(), b"second-part".len()],
        &owner_id,
        b"\r",
    );
    issue312_assert_all_sinks_masked(
        &rig,
        &["first-part-", "second-part", "first-part-second-part"],
        &[&owner_console, &other_console],
        &log_path,
    );
}

#[test]
fn issue312_natural_disconnect_settles_meta_with_human_source() {
    let rig = build_issue312_human_rig(false);
    let log_path = start_log(&rig.service);
    let (client_id, console) = issue312_attach_console(&rig);
    issue312_set_event_probes(&rig, &["natural-secret-312"]);

    let rx_start = rig.device.rx_bytes_read();
    rig.device.push_rx(b"\r\nPassword: ");
    wait_controlled_rx(
        &rig.device,
        rx_start + b"\r\nPassword: ".len(),
        "reader 收到自然斷線測試的 password prompt",
    );
    issue312_wait_console_prompt(&console, b"Password: ");
    rig.service.settle();
    let tx_before_partial = rig.device.tx_len();
    console.push_input(b"natural-secret-312");
    issue312_wait_tx(
        &rig.device,
        tx_before_partial + b"natural-secret-312".len(),
        "自然斷線前輸入未完成 password",
    );
    issue312_push_secret_echo(&rig, "natural-secret-312");
    wait_controlled_rx(
        &rig.device,
        rx_start + b"\r\nPassword: ".len() + b"natural-secret-312\r\n".len(),
        "reader 收到未完成 password 的 echo",
    );

    console.hangup();
    support::wait_until("自然 EOF 從 console_list 移除 owner", || {
        !matches!(
            rig.service
                .call("session.console_list", r#"{"selector":"COM0"}"#)
                .get("consoles"),
            Some(PyValue::List(rows)) if rows.iter().any(|row| {
                row.get("client_id").and_then(PyValue::as_str) == Some(client_id.as_str())
            })
        )
    });
    rig.service.settle();

    issue312_assert_secret_wal(&rig, "natural-secret-312", &client_id, b"");
    let wal = String::from_utf8_lossy(
        &rig.service
            .wfs
            .contents(&rig.service.wal.wal_path())
            .expect("raw.wal.ndjson"),
    )
    .into_owned();
    assert!(
        wal.contains(&format!("\"source\":\"human:{client_id}\"")),
        "自然斷線 META source 必須指向實際輸入者 human:{client_id}：{wal}"
    );
    issue312_assert_all_sinks_masked(&rig, &["natural-secret-312"], &[&console], &log_path);
}

#[test]
fn issue312_raw_line_latches_prompt_seen_after_first_byte() {
    let (observer, prompt_seen, release_reader) = issue312_password_prompt_barrier();
    let rig = build_issue312_human_rig_with_observers(false, false, vec![observer]);
    let log_path = start_log(&rig.service);
    let (client_id, console) = issue312_attach_console(&rig);
    let secret = "manual-password-312";
    let prefix = "manual-";
    let suffix = "password-312";
    issue312_set_event_probes(&rig, &[secret, "Password:"]);
    let tx_start = rig.device.tx_len();
    console.push_input(prefix.as_bytes());
    issue312_wait_tx(&rig.device, tx_start + prefix.len(), "prompt 前 raw line prefix");
    rig.service.settle();

    let rx_start = rig.device.rx_bytes_read();
    rig.device.push_rx(b"\r\nPassword: ");
    prompt_seen
        .recv_timeout(Duration::from_secs(3))
        .expect("reader 已讀取提示；actor 尚未收到該 RX event");
    release_reader
        .send(())
        .expect("釋放 raw line password prompt reader");
    issue312_wait_console_prompt(&console, b"Password: ");
    console.push_input(&[suffix.as_bytes(), b"\r"].concat());
    issue312_wait_tx(
        &rig.device,
        tx_start + secret.len() + 1,
        "raw line 在 prompt 後完成",
    );
    rig.service.settle();
    issue312_push_secret_echo(&rig, secret);
    wait_controlled_rx(
        &rig.device,
        rx_start + b"\r\nPassword: ".len() + secret.len() + 2,
        "reader 收到完整 raw password echo",
    );
    rig.service.settle();
    issue312_wait_secret_event(&rig, &["Password:"]);

    assert_eq!(
        rig.device.tx_since(tx_start),
        [prefix.as_bytes(), suffix.as_bytes(), b"\r"].concat(),
        "提示前綴與提示後輸入的 UART bytes 不得改變"
    );
    issue312_assert_sinks_do_not_contain(&rig, &[suffix], &[&console], &log_path);
    issue312_assert_secret_wal_chunks(
        &rig,
        suffix,
        &[suffix.len()],
        &client_id,
        &[prefix.as_bytes(), b"\r"].concat(),
    );
    let wal_contents = rig
        .service
        .wfs
        .contents(&rig.service.wal.wal_path())
        .expect("raw.wal.ndjson");
    let wal_text = String::from_utf8_lossy(&wal_contents);
    let wal_payload = decode_wal_payloads(&wal_text);
    assert!(
        String::from_utf8_lossy(&wal_payload).contains(prefix),
        "提示窗口前已寫出的 TX prefix 應留在 WAL：{wal_payload:?}"
    );
    assert!(
        String::from_utf8_lossy(&console.written()).contains(prefix),
        "提示窗口外的 echo prefix 應照常送到 console"
    );
}

#[test]
fn issue312_line_buffer_open_line_latches_prompt_before_enter() {
    let (observer, prompt_seen, release_reader) = issue312_password_prompt_barrier();
    let rig = build_issue312_human_rig_with_observers(true, false, vec![observer]);
    let log_path = start_log(&rig.service);
    let (raw_id, raw_console) = issue312_attach_console(&rig);
    let (line_id, line_console) = issue312_attach_console(&rig);
    let detached = rig.service.call(
        "session.console_detach",
        &format!(r#"{{"selector":"COM0","client_id":"{raw_id}"}}"#),
    );
    assert_eq!(detached.get("ok"), Some(&PyValue::Bool(true)), "{detached:?}");

    let secret = "manual-password-312";
    let prefix = "manual-";
    let suffix = "password-312";
    issue312_set_event_probes(&rig, &[secret]);
    let tx_start = rig.device.tx_len();
    let rx_start = rig.device.rx_bytes_read();

    line_console.push_input(prefix.as_bytes());
    support::wait_until("line editor 回顯提示前 prefix", || {
        String::from_utf8_lossy(&line_console.written()).contains(prefix)
    });
    rig.device.push_rx(b"\r\nPassword: ");
    prompt_seen
        .recv_timeout(Duration::from_secs(3))
        .expect("reader 已讀取 password prompt；actor 尚未收到 RX event");
    release_reader
        .send(())
        .expect("釋放 line-buffer password prompt reader");
    issue312_wait_console_prompt(&line_console, b"Password: ");
    line_console.push_input(&[suffix.as_bytes(), b"\r"].concat());
    issue312_wait_tx(
        &rig.device,
        tx_start + secret.len() + 1,
        "line-buffer 在 prompt 後完成既有輸入行",
    );
    issue312_push_secret_echo(&rig, secret);
    wait_controlled_rx(
        &rig.device,
        rx_start + b"\r\nPassword: ".len() + secret.len() + 2,
        "reader 收到 line-buffer password echo",
    );
    rig.service.settle();

    assert_eq!(
        rig.device.tx_since(tx_start),
        [secret.as_bytes(), b"\n"].concat(),
        "line-buffer UART bytes 不得改變"
    );
    issue312_assert_all_sinks_masked(
        &rig,
        &[secret],
        &[&raw_console, &line_console],
        &log_path,
    );
    issue312_assert_secret_wal(&rig, secret, &line_id, b"\n");
}

#[test]
fn issue312_buffered_prefix_crlf_same_rx_chunk_keeps_echo_mask_armed() {
    let rig = build_issue312_human_rig(false);
    let log_path = start_log(&rig.service);
    let (client_id, console) = issue312_attach_console(&rig);
    issue312_set_event_probes(&rig, &[ISSUE312_HUMAN_SECRET]);
    let rx_start = rig.device.rx_bytes_read();
    rig.device.push_rx(b"\r\nPassword: ");
    wait_controlled_rx(
        &rig.device,
        rx_start + b"\r\nPassword: ".len(),
        "reader 收到 password prompt",
    );
    issue312_wait_console_prompt(&console, b"Password: ");
    rig.service.settle();

    let tx_start = rig.device.tx_len();
    console.push_input(&[ISSUE312_HUMAN_SECRET.as_bytes(), b"\r"].concat());
    issue312_wait_tx(
        &rig.device,
        tx_start + ISSUE312_HUMAN_SECRET.len() + 1,
        "送出 human password 與 Enter",
    );
    rig.service.settle();
    let echo = [b"m\r\n".as_slice(), ISSUE312_HUMAN_SECRET.as_bytes(), b"\r\n"].concat();
    rig.device.push_rx(&echo);
    wait_controlled_rx(
        &rig.device,
        rx_start + b"\r\nPassword: ".len() + echo.len(),
        "同一 RX chunk 含部分前綴、CRLF 與真正 password echo",
    );
    rig.service.settle();

    assert_eq!(
        rig.device.tx_since(tx_start),
        [ISSUE312_HUMAN_SECRET.as_bytes(), b"\r"].concat(),
        "UART password bytes 不得改變"
    );
    issue312_assert_all_sinks_masked(
        &rig,
        &[ISSUE312_HUMAN_SECRET],
        &[&console],
        &log_path,
    );
    issue312_assert_secret_wal(&rig, ISSUE312_HUMAN_SECRET, &client_id, b"\r");
}

#[test]
fn issue312_buffered_prefix_crlf_across_rx_chunks_keeps_echo_mask_armed() {
    let rig = build_issue312_human_rig(false);
    let log_path = start_log(&rig.service);
    let (client_id, console) = issue312_attach_console(&rig);
    issue312_set_event_probes(&rig, &[ISSUE312_HUMAN_SECRET]);
    let rx_start = rig.device.rx_bytes_read();
    rig.device.push_rx(b"\r\nPassword: ");
    wait_controlled_rx(
        &rig.device,
        rx_start + b"\r\nPassword: ".len(),
        "reader 收到 password prompt",
    );
    issue312_wait_console_prompt(&console, b"Password: ");
    rig.service.settle();

    let tx_start = rig.device.tx_len();
    console.push_input(&[ISSUE312_HUMAN_SECRET.as_bytes(), b"\r"].concat());
    issue312_wait_tx(
        &rig.device,
        tx_start + ISSUE312_HUMAN_SECRET.len() + 1,
        "送出 human password 與 Enter",
    );
    rig.service.settle();
    let echo_start = rx_start + b"\r\nPassword: ".len();
    rig.device.push_rx(b"m");
    wait_controlled_rx(&rig.device, echo_start + 1, "reader 收到未決 echo 前綴");
    rig.device.push_rx(b"\r\n");
    wait_controlled_rx(&rig.device, echo_start + 3, "reader 收到前綴後 CRLF");
    issue312_push_secret_echo(&rig, ISSUE312_HUMAN_SECRET);
    wait_controlled_rx(
        &rig.device,
        echo_start + 3 + ISSUE312_HUMAN_SECRET.len() + 2,
        "reader 收到跨 chunk 的真正 password echo",
    );
    rig.service.settle();

    assert_eq!(
        rig.device.tx_since(tx_start),
        [ISSUE312_HUMAN_SECRET.as_bytes(), b"\r"].concat(),
        "UART password bytes 不得改變"
    );
    issue312_assert_all_sinks_masked(
        &rig,
        &[ISSUE312_HUMAN_SECRET],
        &[&console],
        &log_path,
    );
    issue312_assert_secret_wal(&rig, ISSUE312_HUMAN_SECRET, &client_id, b"\r");
}

#[test]
fn issue312_line_buffer_filters_echo_per_line_in_one_input_chunk() {
    let rig = build_issue312_human_rig(true);
    let log_path = start_log(&rig.service);
    let (raw_id, raw_console) = issue312_attach_console(&rig);
    let (line_id, line_console) = issue312_attach_console(&rig);
    let detached = rig.service.call(
        "session.console_detach",
        &format!(r#"{{"selector":"COM0","client_id":"{raw_id}"}}"#),
    );
    assert_eq!(detached.get("ok"), Some(&PyValue::Bool(true)), "{detached:?}");
    issue312_set_event_probes(&rig, &[ISSUE312_HUMAN_SECRET]);
    rig.device.on_tx_match(
        &format!("^{ISSUE312_HUMAN_SECRET}$"),
        Box::new(|_| [ISSUE312_HUMAN_SECRET.as_bytes(), b"\r\nroot@t:/# "].concat()),
    );
    rig.device.on_tx_match(
        "^status$",
        Box::new(|_| b"OK\r\nroot@t:/# ".to_vec()),
    );

    let tx_start = rig.device.tx_len();
    let rx_start = rig.device.rx_bytes_read();
    rig.device.push_rx(b"Password: ");
    wait_controlled_rx(
        &rig.device,
        rx_start + b"Password: ".len(),
        "reader 收到 line-buffer 多行測試的 password prompt",
    );
    issue312_wait_console_prompt(&line_console, b"Password: ");
    rig.service.settle();
    line_console.push_input(&[ISSUE312_HUMAN_SECRET.as_bytes(), b"\rstatus\r"].concat());
    issue312_wait_tx(
        &rig.device,
        tx_start + ISSUE312_HUMAN_SECRET.len() + 1 + b"status\n".len(),
        "同一輸入 chunk 內送出 secret 行與後續一般命令",
    );
    let response_len = ISSUE312_HUMAN_SECRET.len() + b"\r\nroot@t:/# ".len()
        + b"OK\r\nroot@t:/# ".len();
    wait_controlled_rx(
        &rig.device,
        rx_start + b"Password: ".len() + response_len,
        "reader 收到 secret 與一般命令的 DUT echo",
    );

    issue312_assert_all_sinks_masked(
        &rig,
        &[ISSUE312_HUMAN_SECRET],
        &[&raw_console, &line_console],
        &log_path,
    );
    let console_bytes = line_console.written();
    let console_text = String::from_utf8_lossy(&console_bytes);
    assert!(
        !console_text.contains(ISSUE312_HUMAN_SECRET),
        "line-buffer console 外洩 secret：{console_text:?}"
    );
    assert!(
        console_text.contains("status"),
        "secret 行之後的普通命令必須照常本地回顯：{console_text:?}"
    );
    assert_eq!(
        rig.device.tx_since(tx_start),
        [ISSUE312_HUMAN_SECRET.as_bytes(), b"\nstatus\n"].concat(),
        "line-buffer UART bytes 不得改變"
    );
    issue312_assert_secret_wal(&rig, ISSUE312_HUMAN_SECRET, &line_id, b"\nstatus\n");
}

#[test]
fn issue312_set_profile_updates_password_regex() {
    let log_dir = tempfile::tempdir().expect("agent log 目錄");
    let profile = format!(
        "profiles:\n  t1:\n    platform: passthrough\n    prompt_regex: '(?m)^root@t:/# '\n    password_regex: 'Password:'\n    timeout_s: 2.0\n  t2:\n    platform: passthrough\n    prompt_regex: '(?m)^root@t:/# '\n    password_regex: 'Passcode:'\n    timeout_s: 2.0\ndefaults:\n  log_dir: '{}'\n",
        log_dir.path().display()
    );
    let env = Arc::new(FakeEnv::default());
    let serial = FakeSerialBackend::new();
    let device = serial.add_device(REAL_A);
    let event = event_rig();
    let service = ServiceRig::build_with_env_serial_console_observers(
        RigSpec {
            yaml: profile,
            devices: vec![(BY_ID_A.into(), REAL_A.into())],
            explicit_target_count: 0,
            allowlist: None,
            wait_ready: Vec::new(),
        },
        env,
        serial as Arc<dyn SerialBackend>,
        vec![event.engine.clone()],
    );
    support::wait_until("動態 COM0 session 建立", || {
        service.session("COM0").is_some()
    });
    assert!(service.svc.attach_engine(event.engine.clone()));
    let rig = Issue312HumanRig {
        service,
        device,
        _event: Some(event),
        _log_dir: log_dir,
    };
    let log_path = start_log(&rig.service);

    // 初始狀態為動態 session，先 pin 到 t1
    let pinned1 = rig
        .service
        .call("session.pin", r#"{"selector":"COM0","profile":"t1"}"#);
    assert_eq!(pinned1.get("ok"), Some(&PyValue::Bool(true)), "{pinned1:?}");

    // 切換 profile 為 t2（新的 password_regex 為 Passcode:）
    let pinned2 = rig
        .service
        .call("session.pin", r#"{"selector":"COM0","profile":"t2"}"#);
    assert_eq!(pinned2.get("ok"), Some(&PyValue::Bool(true)), "{pinned2:?}");
    let cleared = rig.service.call("session.clear", r#"{"selector":"COM0"}"#);
    assert_eq!(cleared.get("ok"), Some(&PyValue::Bool(true)), "{cleared:?}");
    support::wait_until("COM0 依新 profile t2 re-attach", || {
        rig.service.session("COM0").and_then(|session| {
            session
                .get("profile")
                .and_then(PyValue::as_str)
                .map(str::to_string)
        }) == Some("t2".to_string())
    });
    rig.service.wait_state("COM0", "ATTACHED");

    let (client_id, console) = issue312_attach_console(&rig);
    issue312_set_event_probes(&rig, &["passcode123"]);
    let tx_start = rig.device.tx_len();

    console.push_input(b"before-312\r");
    issue312_wait_tx(&rig.device, tx_start + b"before-312\r".len(), "提示前輸入");
    rig.service.settle();

    let rx_before = rig.device.rx_bytes_read();
    rig.device.push_rx(b"\r\nPasscode: ");
    wait_controlled_rx(
        &rig.device,
        rx_before + b"\r\nPasscode: ".len(),
        "reader 收到新 profile 的 passcode prompt",
    );
    issue312_wait_console_prompt(&console, b"Passcode: ");
    rig.service.settle();

    let secret = "passcode123";
    for byte in secret.as_bytes() {
        let expected = rig.device.tx_len() + 1;
        console.push_input(&[*byte]);
        issue312_wait_tx(&rig.device, expected, "passcode 逐鍵送出");
    }
    let expected = rig.device.tx_len() + 1;
    console.push_input(b"\r");
    issue312_wait_tx(&rig.device, expected, "passcode 結尾 CR 送出");
    console.push_input(b"after-312\r");
    issue312_wait_tx(&rig.device, expected + b"after-312\r".len(), "換行後普通輸入");
    rig.service.settle();

    let rx_before_echo = rig.device.rx_bytes_read();
    issue312_push_secret_echo(&rig, secret);
    wait_controlled_rx(
        &rig.device,
        rx_before_echo + secret.len() + 2,
        "reader 收到 profile 切換後的 passcode echo",
    );
    rig.service.settle();
    issue312_wait_secret_event(&rig, &[secret]);

    assert_eq!(
        rig.device.tx_since(tx_start),
        [b"before-312\r".as_slice(), secret.as_bytes(), b"\rafter-312\r"].concat()
    );
    issue312_assert_secret_wal_chunks(
        &rig,
        secret,
        &vec![1; secret.len()],
        &client_id,
        b"before-312\r\rafter-312\r",
    );
    issue312_assert_all_sinks_masked(&rig, &[secret], &[&console], &log_path);
}
