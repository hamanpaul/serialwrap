//! P4-T9 `connect`／`benches` 原生（SC-27）的失敗測試。
//!
//! 紅燈階段（TDD）：`connect` 與 `benches` 原生實作尚未完成，本檔案案例應以
//! `not implemented: P4-T9` 或斷言不符失敗，不得因語法或編譯錯誤失敗。
//! 替身與 `TestEnv` 由 `tests/support/mod.rs` 提供。
//! 規格來源：`docs/rust-refactor/plans/05-stage1b-rust-cli-mgmt.md` Task 9；盤點 `inventory/07-platform-mgmt.md` §9.3、§9.4。

mod support;

use support::*;
use sw_proto::pyjson::{PyDict, PyInt, PyValue};

const B1_YAML: &str = "benches:\n  B1:\n    target: u@relay\n    remote_socket: /run/serialwrap/serialwrapd.sock\n    local_port: 49001\n    ssh_opts: []\n    autossh: false\n";

fn s<'a>(v: &'a PyValue, key: &str) -> &'a str {
    match v {
        PyValue::Dict(d) => match d.get(key) {
            Some(PyValue::Str(s)) => s.as_str(),
            other => panic!("key {key:?} 不是字串：{other:?}"),
        },
        other => panic!("不是 dict：{other:?}"),
    }
}

fn b(v: &PyValue, key: &str) -> bool {
    match v {
        PyValue::Dict(d) => match d.get(key) {
            Some(PyValue::Bool(b)) => *b,
            other => panic!("key {key:?} 不是 bool：{other:?}"),
        },
        other => panic!("不是 dict：{other:?}"),
    }
}

#[test]
fn connect_open_remembers_flat_endpoint() {
    let t = TestEnv::posix()
        .with_benches_yaml("benches:\n  B1:\n    target: u@relay\n    remote_socket: /run/serialwrap/serialwrapd.sock\n    local_port: 49001\n    ssh_opts: []\n    autossh: false\n")
        .with_tunnel_ready();
    let (rc, _, _) = t.run(&["connect", "B1"]);
    assert_eq!(rc, 0);
    assert_eq!(
        std::fs::read_to_string(t.bench_state_path()).unwrap(),
        "{\"B1\":\"tcp://127.0.0.1:49001\"}"
    );
}

#[test]
fn connect_close_refuses_mismatched_identity() {
    let t = TestEnv::posix()
        .with_benches_yaml(B1_YAML)
        .with_bench_state("{\"B1\":\"tcp://127.0.0.1:49001\"}")
        .with_registry_state(49001, alive_state_with_identity("connect", 77, "deadbeef"));
    let (rc, out, _) = t.run(&["connect", "B1", "--close"]);
    assert_eq!(rc, 1);
    assert_eq!(
        s(&json(&out), "message"),
        "local_port=49001 已有不相符的 stateful tunnel；僅可關閉相符的 connect tunnel"
    );
}

#[test]
fn connect_unknown_code() {
    let t = TestEnv::posix().with_benches_yaml(B1_YAML);
    let (rc, out, _) = t.run(&["connect", "B9"]);
    assert_eq!(rc, 1);
    let doc = json(&out);
    assert_eq!(s(&doc, "error_code"), "UNKNOWN_BENCH_CODE");
    assert_eq!(s(&doc, "message"), "unknown bench code: B9");
    assert_eq!(b(&doc, "ok"), false);
}

#[test]
fn connect_invalid_config_extra_key() {
    let yaml = "benches:\n  B1:\n    target: u@relay\n    remote_socket: /run/serialwrap/serialwrapd.sock\n    local_port: 49001\n    ssh_opts: []\n    autossh: false\n    host: relay\n";
    let t = TestEnv::posix().with_benches_yaml(yaml);
    let (rc, out, _) = t.run(&["connect", "B1"]);
    assert_eq!(rc, 1);
    let doc = json(&out);
    assert_eq!(s(&doc, "error_code"), "INVALID_BENCH_CONFIG");
    assert!(
        s(&doc, "message").starts_with("bench \"B1\" 包含不支援欄位"),
        "got message: {}",
        s(&doc, "message")
    );
    assert_eq!(b(&doc, "ok"), false);
}

#[test]
fn connect_target_requires_user_at_host() {
    let yaml = "benches:\n  B1:\n    target: relay\n    remote_socket: /run/serialwrap/serialwrapd.sock\n    local_port: 49001\n    ssh_opts: []\n    autossh: false\n";
    let t = TestEnv::posix().with_benches_yaml(yaml);
    let (rc, out, _) = t.run(&["connect", "B1"]);
    assert_eq!(rc, 1);
    let doc = json(&out);
    assert_eq!(s(&doc, "error_code"), "INVALID_BENCH_CONFIG");
    assert_eq!(s(&doc, "message"), "bench \"B1\" 的 target 必須是 user@host 形式");
    assert_eq!(b(&doc, "ok"), false);
}

#[test]
fn connect_nested_state_preserved() {
    let t = TestEnv::posix()
        .with_benches_yaml(B1_YAML)
        .with_bench_state(r#"{"benches":{"B2":"tcp://127.0.0.1:49002"}}"#)
        .with_tunnel_ready();
    let (rc, _, _) = t.run(&["connect", "B1"]);
    assert_eq!(rc, 0);
    let body = std::fs::read_to_string(t.bench_state_path()).unwrap();
    let doc = json(&body);
    let benches = match doc {
        PyValue::Dict(d) => match d.get("benches") {
            Some(PyValue::Dict(b)) => b.clone(),
            _ => panic!("缺少 benches 物件: {body}"),
        },
        _ => panic!("根非物件: {body}"),
    };
    assert_eq!(benches.get("B1"), Some(&PyValue::Str("tcp://127.0.0.1:49001".into())));
    assert_eq!(benches.get("B2"), Some(&PyValue::Str("tcp://127.0.0.1:49002".into())));
}

#[test]
fn connect_remember_failure_rolls_back() {
    // 以 FaultFs 讓 benches.state.json 的寫入失敗（不用 cfg(unix) chmod；check_cfg_boundary 禁止平台謂詞）。
    let t = TestEnv::posix()
        .with_benches_yaml(B1_YAML)
        .with_tunnel_ready()
        .with_fs_fault_write_suffix("benches.state.json");
    let (rc, out, _) = t.run(&["connect", "B1"]);
    assert_eq!(rc, 1);
    let doc = json(&out);
    assert_eq!(s(&doc, "error_code"), "BENCH_STATE_IO_ERROR");
    assert_eq!(b(&doc, "ok"), false);
    assert!(!t.tunnel().terminated().is_empty(), "tunnel 未在狀態記憶失敗時 rollback close");
}

#[test]
fn connect_close_still_active() {
    let mut orphan = PyDict::new();
    orphan.insert("status", PyValue::Str("orphan".into()));
    orphan.insert("control_path", PyValue::Str("/run/remote/cm-49001".into()));
    orphan.insert("listen_port", PyValue::Int(PyInt::Small(49001)));

    let t = TestEnv::posix()
        .with_benches_yaml(B1_YAML)
        .with_bench_state("{\"B1\":\"tcp://127.0.0.1:49001\"}")
        .with_registry_state(49001, orphan);
    let (rc, out, _) = t.run(&["connect", "B1", "--close"]);
    assert_eq!(rc, 1);
    let doc = json(&out);
    assert_eq!(s(&doc, "error_code"), "TUNNEL_STILL_ACTIVE");
    assert_eq!(
        s(&doc, "message"),
        "local_port=49001 的 tunnel 仍存在，未清除 endpoint 記憶"
    );
    assert_eq!(b(&doc, "ok"), false);
    let state_body = std::fs::read_to_string(t.bench_state_path()).unwrap();
    assert!(state_body.contains("B1"), "狀態檔記憶不應被清除：{state_body}");
}

#[test]
fn connect_close_clears_memory() {
    let t = TestEnv::posix()
        .with_benches_yaml(B1_YAML)
        .with_bench_state("{\"B1\":\"tcp://127.0.0.1:49001\"}")
        .with_tunnel_ready();
    let (rc, out, _) = t.run(&["connect", "B1", "--close"]);
    assert_eq!(rc, 0);
    let doc = json(&out);
    assert_eq!(b(&doc, "ok"), true);
    let state_body = std::fs::read_to_string(t.bench_state_path()).unwrap();
    assert!(!state_body.contains("B1"), "close 成功後狀態檔不應含 B1：{state_body}");
}

#[test]
fn connect_missing_code_json_boundary() {
    let t = TestEnv::posix();
    let (rc, out, err) = t.run(&["connect"]);
    assert_eq!(rc, 1);
    let doc = json(&out);
    assert_eq!(s(&doc, "error_code"), "INVALID_ARGS");
    assert_eq!(b(&doc, "ok"), false);
    let err_trimmed = err.trim_end();
    assert!(
        err_trimmed.ends_with("serialwrap: connect failed: INVALID_ARGS"),
        "stderr 末行不符：{err}"
    );
}

#[test]
fn benches_sorted_with_alive() {
    let yaml = "benches:\n  B2:\n    target: u@relay\n    remote_socket: /run/sw2.sock\n    local_port: 49002\n    ssh_opts: []\n    autossh: false\n  B1:\n    target: u@relay\n    remote_socket: /run/sw1.sock\n    local_port: 49001\n    ssh_opts: []\n    autossh: false\n";
    let t = TestEnv::posix()
        .with_benches_yaml(yaml)
        .with_bench_state(r#"{"B2":"tcp://127.0.0.1:49002"}"#)
        .with_registry_state(49002, alive_state("connect", 88));
    let (rc, out, _) = t.run(&["benches"]);
    assert_eq!(rc, 0);
    let doc = json(&out);
    assert_eq!(b(&doc, "ok"), true);
    let benches_val = match doc {
        PyValue::Dict(d) => d.get("benches").cloned().expect("缺少 benches 欄位"),
        _ => panic!("回應非 dict: {out}"),
    };
    let expected = json(r#"[{"alive":false,"code":"B1","endpoint":null},{"alive":true,"code":"B2","endpoint":"tcp://127.0.0.1:49002"}]"#);
    assert_eq!(benches_val, expected);
}

#[test]
fn benches_status_tunnels_from_error_shapes() {
    use sw_cli::commands::benches_cmd::status_tunnels_from;

    // 1. {"ok": false}
    let mut resp1 = PyDict::new();
    resp1.insert("ok", PyValue::Bool(false));
    let err1 = status_tunnels_from(&resp1).unwrap_err();
    assert_eq!(err1.code, "TUNNEL_STATUS_ERROR");
    assert_eq!(err1.message.as_deref(), Some("無法確認 tunnel 狀態"));

    // 2. {"ok": false, "error_code": "X", "message": "m"}
    let mut resp2 = PyDict::new();
    resp2.insert("ok", PyValue::Bool(false));
    resp2.insert("error_code", PyValue::Str("X".into()));
    resp2.insert("message", PyValue::Str("m".into()));
    let err2 = status_tunnels_from(&resp2).unwrap_err();
    assert_eq!(err2.code, "X");
    assert_eq!(err2.message.as_deref(), Some("m"));

    // 3. {"ok": true} (缺少 tunnels 清單)
    let mut resp3 = PyDict::new();
    resp3.insert("ok", PyValue::Bool(true));
    let err3 = status_tunnels_from(&resp3).unwrap_err();
    assert_eq!(err3.code, "TUNNEL_STATUS_ERROR");
    assert_eq!(err3.message.as_deref(), Some("remote status 回應缺少 tunnels 清單"));
}

#[test]
fn windows_connect_supported() {
    let t = TestEnv::windows()
        .with_benches_yaml(B1_YAML)
        .with_tunnel_ready();
    let (rc, out, err) = t.run(&["connect", "B1"]);
    assert_eq!(rc, 0);
    assert!(!out.contains("REMOTE_NOT_SUPPORTED"), "stdout 不應含 REMOTE_NOT_SUPPORTED: {out}");
    assert!(!err.contains("REMOTE_NOT_SUPPORTED"), "stderr 不應含 REMOTE_NOT_SUPPORTED: {err}");
    let spawned = t.tunnel().spawned();
    assert!(!spawned.is_empty(), "未在 Windows 上 spawn ssh 隧道行程");
    for (argv, _) in spawned {
        for arg in &argv {
            assert!(!arg.starts_with("ControlMaster="), "argv 不應含 ControlMaster: {arg}");
            assert!(!arg.starts_with("ControlPath="), "argv 不應含 ControlPath: {arg}");
            assert!(!arg.starts_with("ControlPersist="), "argv 不應含 ControlPersist: {arg}");
        }
    }
}

#[test]
fn bench_state_lock_cross_process() {
    let t = TestEnv::posix();
    t.with_cli_ctx(|ctx| {
        sw_cli::bench_state::remember(ctx, "B1", "tcp://127.0.0.1:49001").expect("remember B1 失敗");
        sw_cli::bench_state::remember(ctx, "B2", "tcp://127.0.0.1:49002").expect("remember B2 失敗");
        let doc = sw_cli::bench_state::load_bench_state_doc(ctx, &t.bench_state_path().display().to_string())
            .expect("load_bench_state_doc 失敗");
        assert_eq!(
            sw_cli::bench_state::remembered_bench_endpoint(&doc, "B1").unwrap(),
            Some("tcp://127.0.0.1:49001".to_string())
        );
        assert_eq!(
            sw_cli::bench_state::remembered_bench_endpoint(&doc, "B2").unwrap(),
            Some("tcp://127.0.0.1:49002".to_string())
        );
    });
}
