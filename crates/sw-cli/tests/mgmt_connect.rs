//! P4-T9 `connect`／`benches` 原生（SC-27）的失敗測試。
//!
//! 綠燈狀態：`connect` 與 `benches` 已原生實作，本檔案案例全數須通過。
//! 跨行程案例以本測試二進位啟動子行程（`bench_state_writer_helper`，僅在環境變數設定時動作）。
//! 替身與 `TestEnv` 由 `tests/support/mod.rs` 提供。
//! 規格來源：`docs/rust-refactor/plans/05-stage1b-rust-cli-mgmt.md` Task 9；盤點 `inventory/07-platform-mgmt.md` §9.3、§9.4。

mod support;

use std::path::Path;
use std::process::{Child, Command, Stdio};
use std::time::Duration;

use support::*;
use sw_cli::ctx::{CliCtx, TextIo};
use sw_pal::PalPathFlavor;
use sw_proto::pyjson::{PyDict, PyValue};
use sw_proto::pypath::{HomeLookup, PathFlavor};
use sw_proto::runtime_paths::{EnvSnapshot, NativeTraits, RuntimePaths};

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
    t.tunnel().set_alive(77, true);
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
    assert!(!b(&doc, "ok"));
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
    assert!(!b(&doc, "ok"));
}

#[test]
fn connect_target_requires_user_at_host() {
    let yaml = "benches:\n  B1:\n    target: relay\n    remote_socket: /run/serialwrap/serialwrapd.sock\n    local_port: 49001\n    ssh_opts: []\n    autossh: false\n";
    let t = TestEnv::posix().with_benches_yaml(yaml);
    let (rc, out, _) = t.run(&["connect", "B1"]);
    assert_eq!(rc, 1);
    let doc = json(&out);
    assert_eq!(s(&doc, "error_code"), "INVALID_BENCH_CONFIG");
    assert_eq!(
        s(&doc, "message"),
        "bench \"B1\" 的 target 必須是 user@host 形式"
    );
    assert!(!b(&doc, "ok"));
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
    assert_eq!(
        benches.get("B1"),
        Some(&PyValue::Str("tcp://127.0.0.1:49001".into()))
    );
    assert_eq!(
        benches.get("B2"),
        Some(&PyValue::Str("tcp://127.0.0.1:49002".into()))
    );
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
    assert!(!b(&doc, "ok"));
    assert!(
        !t.tunnel().terminated().is_empty(),
        "tunnel 未在狀態記憶失敗時 rollback close"
    );
}

#[test]
fn connect_close_still_active() {
    // orphan：只有 cm-49001 control socket、無對應 state（07 §8.3）。
    // 若寫成 state 檔（缺 pid），close 會將其視為死亡並移除，與 Python 基線不符。
    let t = TestEnv::posix()
        .with_benches_yaml(B1_YAML)
        .with_bench_state("{\"B1\":\"tcp://127.0.0.1:49001\"}")
        .with_tunnel_ready();
    let remote_dir = t.run_dir().join("remote");
    std::fs::create_dir_all(&remote_dir).expect("remote 目錄建立失敗");
    std::fs::write(remote_dir.join("cm-49001"), b"").expect("cm-49001 寫入失敗");
    let (rc, out, _) = t.run(&["connect", "B1", "--close"]);
    assert_eq!(rc, 1);
    let doc = json(&out);
    assert_eq!(s(&doc, "error_code"), "TUNNEL_STILL_ACTIVE");
    assert_eq!(
        s(&doc, "message"),
        "local_port=49001 的 tunnel 仍存在，未清除 endpoint 記憶"
    );
    assert!(!b(&doc, "ok"));
    let state_body = std::fs::read_to_string(t.bench_state_path()).unwrap();
    assert!(
        state_body.contains("B1"),
        "狀態檔記憶不應被清除：{state_body}"
    );
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
    assert!(b(&doc, "ok"));
    let state_body = std::fs::read_to_string(t.bench_state_path()).unwrap();
    assert!(
        !state_body.contains("B1"),
        "close 成功後狀態檔不應含 B1：{state_body}"
    );
}

#[test]
fn connect_missing_code_json_boundary() {
    let t = TestEnv::posix();
    let (rc, out, err) = t.run(&["connect"]);
    assert_eq!(rc, 1);
    let doc = json(&out);
    assert_eq!(s(&doc, "error_code"), "INVALID_ARGS");
    assert!(!b(&doc, "ok"));
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
    t.tunnel().set_alive(88, true);
    let (rc, out, _) = t.run(&["benches"]);
    assert_eq!(rc, 0);
    let doc = json(&out);
    assert!(b(&doc, "ok"));
    let benches_val = match doc {
        PyValue::Dict(d) => d.get("benches").cloned().expect("缺少 benches 欄位"),
        _ => panic!("回應非 dict: {out}"),
    };
    let expected = json(
        r#"[{"alive":false,"code":"B1","endpoint":null},{"alive":true,"code":"B2","endpoint":"tcp://127.0.0.1:49002"}]"#,
    );
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
    assert_eq!(
        err3.message.as_deref(),
        Some("remote status 回應缺少 tunnels 清單")
    );
}

#[test]
fn windows_connect_supported() {
    let t = TestEnv::windows()
        .with_benches_yaml(B1_YAML)
        .with_tunnel_ready();
    let (rc, out, err) = t.run(&["connect", "B1"]);
    assert_eq!(rc, 0);
    assert!(
        !out.contains("REMOTE_NOT_SUPPORTED"),
        "stdout 不應含 REMOTE_NOT_SUPPORTED: {out}"
    );
    assert!(
        !err.contains("REMOTE_NOT_SUPPORTED"),
        "stderr 不應含 REMOTE_NOT_SUPPORTED: {err}"
    );
    let spawned = t.tunnel().spawned();
    assert!(!spawned.is_empty(), "未在 Windows 上 spawn ssh 隧道行程");
    for (argv, _) in spawned {
        for arg in &argv {
            assert!(
                !arg.starts_with("ControlMaster="),
                "argv 不應含 ControlMaster: {arg}"
            );
            assert!(
                !arg.starts_with("ControlPath="),
                "argv 不應含 ControlPath: {arg}"
            );
            assert!(
                !arg.starts_with("ControlPersist="),
                "argv 不應含 ControlPersist: {arg}"
            );
        }
    }
}

/// 子行程寫入角色與參數的環境變數名（僅 `bench_state_writer_helper` 讀取）。
const WRITER_ROLE_ENV: &str = "SW_CLI_BENCH_STATE_WRITER";
const WRITER_PREFIX_ENV: &str = "SW_CLI_BENCH_STATE_PREFIX";
const WRITER_COUNT_ENV: &str = "SW_CLI_BENCH_STATE_COUNT";

/// 以原生平台（`sw_pal::native_client()`）建立 `CliCtx`，來源與 `sw_cli::run` 相同；子行程以此走 `bench_state::remember` 的實際路徑。
fn with_native_cli_ctx<R>(f: impl FnOnce(&mut CliCtx<'_>) -> R) -> R {
    let pf = sw_pal::native_client();
    let pairs = pf.env.vars();
    let (env, flavor) = match pf.caps.path_flavor {
        PalPathFlavor::Windows => (
            EnvSnapshot::from_pairs_fold_upper(pairs),
            PathFlavor::Windows,
        ),
        PalPathFlavor::Posix => (EnvSnapshot::from_pairs(pairs), PathFlavor::Posix),
    };
    let native = NativeTraits {
        default_endpoint_is_tcp: pf.caps.native_backend == sw_pal::BackendKind::Win,
        path_flavor: flavor,
    };
    let home = NativeHome(pf.env.as_ref());
    let paths = RuntimePaths::resolve(&env, native, &home).expect("RuntimePaths 解析失敗");
    let mut out: Vec<u8> = Vec::new();
    let mut err: Vec<u8> = Vec::new();
    let mut ctx = CliCtx {
        p: &pf,
        env,
        paths,
        io: TextIo::new(&mut out, &mut err, "\n"),
        verbose: 0,
        cli_version: "test",
        autostart_lock: None,
    };
    f(&mut ctx)
}

struct NativeHome<'a>(&'a dyn sw_pal::traits::SysEnv);

impl HomeLookup for NativeHome<'_> {
    fn current_user_home(&self) -> Option<String> {
        self.0.current_user_home()
    }
    fn user_home(&self, user: &str) -> Option<String> {
        self.0.user_home(user)
    }
}

/// 子行程本體（非獨立測試）：只在父行程設定 `SW_CLI_BENCH_STATE_WRITER=writer` 時，以真實 `bench_state::remember`
/// 寫入 `SW_CLI_BENCH_STATE_COUNT` 筆不同代號（前綴取 `SW_CLI_BENCH_STATE_PREFIX`），否則立即返回。
#[test]
fn bench_state_writer_helper() {
    if std::env::var(WRITER_ROLE_ENV).ok().as_deref() != Some("writer") {
        return;
    }
    let prefix = std::env::var(WRITER_PREFIX_ENV).expect("缺少 SW_CLI_BENCH_STATE_PREFIX");
    let count: usize = std::env::var(WRITER_COUNT_ENV)
        .expect("缺少 SW_CLI_BENCH_STATE_COUNT")
        .parse()
        .expect("SW_CLI_BENCH_STATE_COUNT 格式錯誤");
    with_native_cli_ctx(|ctx| {
        for index in 0..count {
            let code = format!("{prefix}-{index:03}");
            sw_cli::bench_state::remember(ctx, &code, "tcp://127.0.0.1:49001")
                .expect("子行程 remember 失敗");
        }
    });
}

/// 啟動寫入子行程：`SERIALWRAP_STATE_DIR` 指向父行程的狀態目錄，兩者共用同一個 `benches.state.json`。
fn spawn_writer(state_dir: &Path, prefix: &str, count: usize) -> Child {
    Command::new(std::env::current_exe().expect("current_exe 不可得"))
        .args([
            "--exact",
            "bench_state_writer_helper",
            "--nocapture",
            "--test-threads=1",
        ])
        .env(WRITER_ROLE_ENV, "writer")
        .env(WRITER_PREFIX_ENV, prefix)
        .env(WRITER_COUNT_ENV, count.to_string())
        .env("SERIALWRAP_STATE_DIR", state_dir)
        .stdout(Stdio::null())
        .stderr(Stdio::piped())
        .spawn()
        .expect("無法啟動寫入子行程")
}

/// 讀回 `benches.state.json` 的根物件（flat 格式）。
fn read_state_root(t: &TestEnv) -> PyDict {
    let text = std::fs::read_to_string(t.bench_state_path()).expect("benches.state.json 不存在");
    match json(&text) {
        PyValue::Dict(dict) => dict,
        other => panic!("benches.state.json 不是 object：{other:?}"),
    }
}

#[test]
fn bench_state_lock_held_refuses_write() {
    // 另一行程持有 benches.state.json.lock：remember 必須回 Os 錯誤，且既有 benches.state.json 位元組不變。
    let t = TestEnv::posix()
        .with_bench_state("{\"B1\":\"tcp://127.0.0.1:49001\"}")
        .with_bench_state_lock_holder();
    let before = std::fs::read(t.bench_state_path()).expect("讀取 benches.state.json 失敗");
    t.with_cli_ctx(|ctx| {
        let result = sw_cli::bench_state::remember(ctx, "B2", "tcp://127.0.0.1:49002");
        assert!(
            matches!(result, Err(sw_cli::bench_state::BenchStateError::Os(_))),
            "取鎖失敗應回 BenchStateError::Os：{result:?}"
        );
    });
    assert_eq!(
        std::fs::read(t.bench_state_path()).expect("讀取 benches.state.json 失敗"),
        before,
        "取鎖失敗時不得改寫 benches.state.json"
    );
}

#[test]
fn bench_state_lock_cross_process() {
    // 父行程以真實 flock／LockFileEx 持有 benches.state.json.lock；子行程的 remember 必須等待，釋放前不得寫入。
    let t = TestEnv::posix();
    let lock_path = t
        .bench_state_path()
        .with_file_name("benches.state.json.lock");
    let held = sw_pal::native()
        .fs
        .file_lock_exclusive(&lock_path, true)
        .expect("父行程取鎖失敗");
    let mut child = spawn_writer(&t.state_dir(), "XP", 1);
    std::thread::sleep(Duration::from_millis(500));
    assert!(
        child.try_wait().expect("try_wait 失敗").is_none(),
        "父行程持鎖期間子行程不得完成 remember"
    );
    assert!(
        !t.bench_state_path().exists(),
        "父行程持鎖期間不得寫入 benches.state.json"
    );
    drop(held);
    let out = child.wait_with_output().expect("等待寫入子行程失敗");
    assert!(
        out.status.success(),
        "寫入子行程失敗：{}",
        String::from_utf8_lossy(&out.stderr)
    );
    assert_eq!(
        read_state_root(&t).get("XP-000"),
        Some(&PyValue::Str("tcp://127.0.0.1:49001".into())),
        "釋放鎖後子行程的記憶應存在"
    );
}

#[test]
fn bench_state_concurrent_writers_keep_all_entries() {
    // 四個子行程各寫入 25 筆不同代號；持鎖 read-modify-write 不得遺失任何更新（SC-27 多行程記憶完整性）。
    const CHILDREN: usize = 4;
    const PER_CHILD: usize = 25;
    let t = TestEnv::posix();
    let children: Vec<Child> = (0..CHILDREN)
        .map(|n| spawn_writer(&t.state_dir(), &format!("C{n}"), PER_CHILD))
        .collect();
    for child in children {
        let out = child.wait_with_output().expect("等待寫入子行程失敗");
        assert!(
            out.status.success(),
            "寫入子行程失敗：{}",
            String::from_utf8_lossy(&out.stderr)
        );
    }
    let root = read_state_root(&t);
    assert_eq!(
        root.0.len(),
        CHILDREN * PER_CHILD,
        "多行程寫入遺失更新（鍵數不符）"
    );
    for n in 0..CHILDREN {
        for index in 0..PER_CHILD {
            let code = format!("C{n}-{index:03}");
            assert!(root.get(&code).is_some(), "缺少代號 {code}");
        }
    }
}
