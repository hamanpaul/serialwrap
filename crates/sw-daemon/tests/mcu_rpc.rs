//! P8 Task 10：`mcu.patterns`／`mcu.status` RPC 與 daemon 的 `/dev/ttyMCU` 組裝（計畫 09 Task 10）。
//!
//! - Service 層：`ServiceRig::with_flash`（真 `SessionManager`＋真 `FlashCoordinator`＋真 `FlashEndpoint`，端點 PTY 為
//!   記憶體替身 `RigFlashConsole`，flasher 以 master 對端扮演）。期望為 RPC_WIRE 位元組（去 `version`），鍵序照 Python
//!   `service.py:1034-1047`。
//! - daemon 層：`run_with_hooks` in-process 執行（`in_process_platform`，console 換成 `RigFlashConsole`、
//!   `caps.flash_endpoint=true`），以 trace 與 link 表驗證啟動序（bootstrap attach 之後才建端點）、start 失敗 exit 1、
//!   RAII（RPC 綁定失敗、正常停止、panic unwind 三條退出路徑都 unlink）。
//!
//! 紅燈：`FlashState`／`Service::attach_flash` 為 `todo!("P8-T10")`；daemon 尚未啟動端點（斷言失敗）。
#![cfg(feature = "test-fakes")]

mod support;

use std::collections::BTreeMap;
use std::path::PathBuf;
use std::sync::atomic::{AtomicBool, Ordering};
use std::sync::{Arc, Mutex};
use std::thread;
use std::time::Duration;

use support::*;
use sw_pal::fakes::clock::FakeClock;
use sw_pal::traits::{
    Clock, EndpointSpec, ListenOptions, PalError, PalResult, RpcListener, RpcListenerFactory,
};
use sw_proto::pyjson::{self, PyValue};

/// threaded FakeClock 的起點（coordinator 專用；只由測試推進）。
const START_NS: u64 = 5_000_000_000;
const START_WALL_US: i64 = 1_790_000_000_000_000;

/// coordinator 的凍結時鐘（threaded：不推進就不會逾時）。
fn frozen_clock() -> Arc<FakeClock> {
    FakeClock::at(START_NS, START_WALL_US)
}

/// COM1（passthrough、ATTACHED、usb-B）作為候選時的 RPC_WIRE 片段（Python `_flash_candidates` 鍵序）。
fn com1_candidate() -> String {
    format!(r#"{{"com":"COM1","by_id":"{BY_ID_B}","real_path":"{REAL_B}","command_capable":false}}"#)
}

/// 無 session 的 flash rig（端點已啟動）。
fn idle_flash_rig() -> ServiceRig {
    ServiceRig::with_flash(FlashSetup {
        yaml: FLASH_TEMPLATES.to_string(),
        devices: Vec::new(),
        caps_flash: true,
        coord_clock: frozen_clock(),
        wait_states: Vec::new(),
    })
}

/// COM0 shell READY（usb-A）、COM1 passthrough ATTACHED（usb-B，行為 `kind`）、COM2 passthrough DETACHED（usb-C 不在快照）。
fn three_targets(kind: DevKind, caps_flash: bool, coord_clock: Arc<dyn Clock>) -> ServiceRig {
    ServiceRig::with_flash(FlashSetup {
        yaml: format!(
            "{FLASH_TEMPLATES}targets:\n{}{}{}",
            flash_target_line("COM0", "t", BY_ID_A),
            flash_target_line("COM1", "m", BY_ID_B),
            flash_target_line("COM2", "m", BY_ID_C)
        ),
        devices: vec![
            (BY_ID_A.into(), REAL_A.into(), DevKind::Shell),
            (BY_ID_B.into(), REAL_B.into(), kind),
        ],
        caps_flash,
        coord_clock,
        wait_states: vec![
            ("COM0".into(), "READY".into()),
            ("COM1".into(), "ATTACHED".into()),
            ("COM2".into(), "DETACHED".into()),
        ],
    })
}

fn status(rig: &ServiceRig) -> sw_proto::pyjson::PyDict {
    rig.call("mcu.status", "{}")
}

/// `contracts/golden/mcu_patterns.ndjson` 的 `{"fn":"rpc","in":null}` 列（Python 預設 registry 的 `mcu.patterns` rows）。
fn golden_default_patterns() -> String {
    let path = support::serde_free::repo_file("contracts/golden/mcu_patterns.ndjson");
    let text = std::fs::read_to_string(path).expect("讀取 mcu_patterns golden");
    for line in text.lines().filter(|l| !l.trim().is_empty()) {
        let PyValue::Dict(row) = pyjson::loads(line).expect("golden 每列為 JSON") else {
            continue;
        };
        if row.get("fn").and_then(PyValue::as_str) == Some("rpc") && row.get("in") == Some(&PyValue::Null) {
            return pyjson::dumps(row.get("out").expect("rpc 列有 out"), &pyjson::RPC_WIRE);
        }
    }
    panic!("mcu_patterns golden 缺 fn=rpc、in=null 列");
}

// ---------------------------------------------------------------------------
// Service 層
// ---------------------------------------------------------------------------

/// `mcu.patterns`：掛上 flash 端點後，回應位元組仍與 Python golden 相同（T2 `patterns_rpc`，`service.py:1034-1038`）。
#[test]
fn mcu_patterns_matches_python_golden() {
    let rig = idle_flash_rig();
    assert_eq!(
        rig.wire_without_version("mcu.patterns"),
        format!(r#"{{"ok":true,"patterns":{}}}"#, golden_default_patterns())
    );
}

/// 無 session、端點閒置：`{"ok":true,"candidates":[],"flashing":false,"flashing_com":null,"last_detect":null}`。
#[test]
fn mcu_status_idle_no_sessions() {
    let rig = idle_flash_rig();
    assert_eq!(
        rig.wire_without_version("mcu.status"),
        r#"{"ok":true,"candidates":[],"flashing":false,"flashing_com":null,"last_detect":null}"#
    );
}

/// candidates 依 session 順序取「非 command_capable 且 READY／ATTACHED」：只有 COM1；`mcu.status` 是唯讀，
/// 不得標記 flash-critical（不可用 `collect_flash_candidates_and_mark`）。
#[test]
fn mcu_status_candidates_order_and_read_only() {
    let rig = three_targets(DevKind::Silent, true, frozen_clock());
    assert_eq!(
        rig.wire_without_version("mcu.status"),
        format!(
            r#"{{"ok":true,"candidates":[{}],"flashing":false,"flashing_com":null,"last_detect":null}}"#,
            com1_candidate()
        )
    );
    assert!(!rig.mgr.flash_critical("COM1"), "mcu.status 不得標記 flash-critical");
    assert!(rig.flash_events().is_empty(), "mcu.status 不得觸發任何 flash 事件");
}

/// MCU-STATUS-01（KEEP）：flasher 首讀之後、偵測（probe）進行中 `flashing` 即為 true，`flashing_com` 仍為 null、
/// `last_detect` 尚未產生；probe 逾時後 `flashing` 回 false，`last_detect` 為 none。
#[test]
fn mcu_status_flashing_true_during_probe() {
    let clock = frozen_clock();
    let rig = three_targets(DevKind::Silent, true, clock.clone());
    rig.flasher_write(MCU_SYNC);
    wait_until("COM1 probe 已開始", || {
        rig.flash_events()
            .iter()
            .any(|(com, kind)| com == "COM1" && *kind == "FlashSyncProbeStart")
    });
    // coordinator 的時鐘凍結：probe 停在等 ACK（不會逾時）。
    assert_eq!(
        rig.wire_without_version("mcu.status"),
        format!(
            r#"{{"ok":true,"candidates":[{}],"flashing":true,"flashing_com":null,"last_detect":null}}"#,
            com1_candidate()
        )
    );
    // 推進時間讓 probe 逾時（timeout_ms=500）：偵測結束、端點回到閒置。
    wait_until("probe 逾時後 flashing 回 false", || {
        clock.advance(Duration::from_millis(50));
        thread::sleep(Duration::from_millis(5));
        status(&rig).get("flashing") == Some(&PyValue::Bool(false))
    });
    rig.wait_state("COM1", "ATTACHED");
    assert_eq!(
        rig.wire_without_version("mcu.status"),
        format!(
            r#"{{"ok":true,"candidates":[{}],"flashing":false,"flashing_com":null,"last_detect":{{"status":"none","com":null,"family":null,"hits":[]}}}}"#,
            com1_candidate()
        )
    );
}

/// matched：`flashing_com` 與 `last_detect` 取自 coordinator（Python `_flash_active_com`／`_flash_last_detect`），
/// FLASHING 的 COM1 不再是候選；停止端點後 `flashing`／`flashing_com` 復原、`last_detect` 保留。
#[test]
fn mcu_status_last_detect_shape_after_matched() {
    let rig = three_targets(DevKind::Mcu, true, sw_pal::native().clock.clone());
    rig.flasher_write(MCU_SYNC);
    wait_until("COM1 進入 FLASHING 且 flashing_com=COM1", || {
        status(&rig).get("flashing_com").and_then(PyValue::as_str) == Some("COM1")
            && rig
                .session("COM1")
                .and_then(|s| s.get("state").and_then(PyValue::as_str).map(str::to_string))
                .as_deref()
                == Some("FLASHING")
    });
    let matched = r#"{"status":"matched","com":"COM1","family":"ti-cc26xx","hits":[]}"#;
    assert_eq!(
        rig.wire_without_version("mcu.status"),
        format!(r#"{{"ok":true,"candidates":[],"flashing":true,"flashing_com":"COM1","last_detect":{matched}}}"#)
    );
    let mut to_flasher = Vec::new();
    wait_until("ACK 回放給 flasher", || {
        to_flasher.extend(rig.flasher_take());
        to_flasher.len() >= MCU_ACK.len()
    });
    assert_eq!(to_flasher, MCU_ACK, "MCU 回應整段回放一次");

    let receipt = rig.stop_flash_endpoint().expect("POSIX caps 應有端點");
    assert!(receipt.joined && receipt.unlinked, "{receipt:?}");
    rig.wait_state("COM1", "ATTACHED");
    assert_eq!(
        rig.wire_without_version("mcu.status"),
        format!(
            r#"{{"ok":true,"candidates":[{}],"flashing":false,"flashing_com":null,"last_detect":{matched}}}"#,
            com1_candidate()
        )
    );
}

/// Windows／`caps.flash_endpoint=false`：不建 PTY、不建 link；`flashing:false`、`last_detect:null`，仍回 candidates。
#[test]
fn mcu_status_flashing_false_without_endpoint() {
    let rig = three_targets(DevKind::Silent, false, frozen_clock());
    let parts = rig.flash.as_ref().expect("with_flash");
    assert_eq!(parts.console.pty_count(), 0, "無端點能力不得建立 PTY");
    assert!(parts.console.links().is_empty(), "無端點能力不得建立 link");
    assert_eq!(
        rig.wire_without_version("mcu.status"),
        format!(
            r#"{{"ok":true,"candidates":[{}],"flashing":false,"flashing_com":null,"last_detect":null}}"#,
            com1_candidate()
        )
    );
    assert!(rig.stop_flash_endpoint().is_none());
    assert!(!rig.svc.attach_flash(parts.state.clone()), "attach_flash 只可一次");
}

// ---------------------------------------------------------------------------
// daemon 層（in-process）
// ---------------------------------------------------------------------------

/// in-process daemon 平台：無裝置、passthrough 模板；端點 PTY 為記憶體替身。
fn flash_daemon_platform(vars: &BTreeMap<String, String>, console: Arc<RigFlashConsole>) -> sw_pal::Platform {
    let mut p = in_process_platform(vars, Vec::new(), Fixture::None);
    p.console = console;
    p.caps.flash_endpoint = true;
    p
}

fn default_link(env: &IsolatedEnv) -> PathBuf {
    env.run_dir.join("dev").join("ttyMCU")
}

/// 記錄每個 trace 事件當下的 link 數。
fn link_trace(console: Arc<RigFlashConsole>, sink: Arc<Mutex<Vec<(TraceEvent, usize)>>>) -> DaemonHooks {
    DaemonHooks {
        before_state_commit: None,
        after_state_commit: None,
        trace: Some(Box::new(move |ev| {
            let n = console.links().len();
            sink.lock().unwrap().push((ev, n));
        })),
    }
}

fn links_at(seen: &[(TraceEvent, usize)], ev: TraceEvent) -> Option<usize> {
    seen.iter().find(|(e, _)| *e == ev).map(|(_, n)| *n)
}

/// 啟動 in-process daemon，服務中記錄 `link` 是否存在後送 `daemon.stop`；回 (exit code, 服務中的 link 表)。
fn run_and_stop(env: &IsolatedEnv, platform: sw_pal::Platform, hooks: DaemonHooks, console: Arc<RigFlashConsole>) -> (i32, BTreeMap<PathBuf, String>) {
    let socket = env.socket.clone();
    let client = thread::spawn(move || {
        wait_socket(&socket, SHORT);
        let during = console.links();
        rpc_once(&socket, STOP);
        during
    });
    let code = run_with_hooks(vec!["serialwrapd".to_string()], platform, hooks);
    (code, client.join().expect("client thread"))
}

/// 啟動序：`<RUN_DIR>/dev/ttyMCU` 在 bootstrap attach（與 watcher 啟動 trace）之後、RPC bind 之前才建立；正常停止後移除。
#[test]
fn daemon_starts_ttymcu_after_bootstrap() {
    let env = isolated_env();
    let console = RigFlashConsole::new();
    let seen = Arc::new(Mutex::new(Vec::new()));
    let platform = flash_daemon_platform(&env.vars, console.clone());
    let (code, during) = run_and_stop(&env, platform, link_trace(console.clone(), seen.clone()), console.clone());
    assert_eq!(code, 0);
    let link = default_link(&env);
    assert!(during.contains_key(&link), "服務中應有 {}：{during:?}", link.display());
    let seen = seen.lock().unwrap().clone();
    assert_eq!(links_at(&seen, TraceEvent::BootstrapAttach), Some(0), "{seen:?}");
    assert_eq!(links_at(&seen, TraceEvent::WatcherStart), Some(0), "bootstrap attach 之前不得建立端點：{seen:?}");
    assert_eq!(links_at(&seen, TraceEvent::Bound), Some(1), "RPC bind 之前端點已建立：{seen:?}");
    assert!(console.links().is_empty(), "正常停止後 link 已移除：{:?}", console.links());
}

/// `SERIALWRAP_TTYMCU_PATH` 覆寫端點路徑（`RuntimePaths.ttymcu_path`）。
#[test]
fn daemon_ttymcu_path_env_override() {
    let env = isolated_env();
    let alt = env.root.join("alt").join("mcu-link");
    let mut vars = env.vars.clone();
    vars.insert("SERIALWRAP_TTYMCU_PATH".to_string(), alt.to_string_lossy().into_owned());
    let console = RigFlashConsole::new();
    let platform = flash_daemon_platform(&vars, console.clone());
    let hooks = DaemonHooks {
        before_state_commit: None,
        after_state_commit: None,
        trace: None,
    };
    let (code, during) = run_and_stop(&env, platform, hooks, console.clone());
    assert_eq!(code, 0);
    assert_eq!(during.keys().cloned().collect::<Vec<_>>(), vec![alt], "{during:?}");
    assert!(console.links().is_empty());
}

/// `FlashEndpoint::start` 失敗（link 目錄無法建立）→ exit 1；不得進入 RPC bind、不留 link。
#[test]
fn daemon_start_failure_of_flash_endpoint_exits_1() {
    let env = isolated_env();
    let console = RigFlashConsole::new();
    console.fail_link();
    let seen = Arc::new(Mutex::new(Vec::new()));
    let platform = flash_daemon_platform(&env.vars, console.clone());
    let done = Arc::new(AtomicBool::new(false));
    let guard = stop_if_serving(env.socket.clone(), done.clone());
    let code = run_with_hooks(
        vec!["serialwrapd".to_string()],
        platform,
        link_trace(console.clone(), seen.clone()),
    );
    done.store(true, Ordering::SeqCst);
    let served = guard.join().expect("guard thread");
    assert!(!served, "端點啟動失敗不得進入 RPC 服務");
    assert_eq!(code, 1, "端點啟動失敗必須 exit 1");
    assert_eq!(console.pty_count(), 1, "應已嘗試建立端點 PTY");
    assert!(console.links().is_empty());
    let seen = seen.lock().unwrap().clone();
    assert!(links_at(&seen, TraceEvent::BootstrapAttach).is_some(), "端點在 bootstrap attach 之後才啟動：{seen:?}");
    assert!(links_at(&seen, TraceEvent::Bound).is_none(), "端點啟動失敗不得進入 RPC bind：{seen:?}");
    assert!(!env.socket.exists(), "不得留下 RPC socket");
}

/// 守衛：daemon 若進入 RPC 服務（socket 出現）即送 `daemon.stop`，避免「本應失敗卻持續服務」時測試卡住；
/// 回傳是否曾送出 stop。`done` 設定或 30 s 後結束。
fn stop_if_serving(socket: PathBuf, done: Arc<AtomicBool>) -> thread::JoinHandle<bool> {
    thread::spawn(move || {
        let deadline = std::time::Instant::now() + LONG;
        while !done.load(Ordering::SeqCst) && std::time::Instant::now() < deadline {
            if socket.exists() {
                wait_socket(&socket, SHORT);
                rpc_once(&socket, STOP);
                return true;
            }
            thread::sleep(Duration::from_millis(20));
        }
        false
    })
}

/// RPC listener 工廠替身：記錄 bind 當下的 link 數後回 `EACCES`。
struct FailBind {
    console: Arc<RigFlashConsole>,
    at_bind: Arc<Mutex<Option<usize>>>,
}

impl RpcListenerFactory for FailBind {
    fn bind(&self, _ep: &EndpointSpec, _opts: &ListenOptions) -> PalResult<Box<dyn RpcListener>> {
        *self.at_bind.lock().unwrap() = Some(self.console.links().len());
        Err(PalError::from_errno(13))
    }
}

const PANIC_MODE: &str = "ttymcu-panic";
const PANIC_MSG: &str = "P8-T10 injected panic after ttyMCU start";

/// 子行程：端點建立後（`Bound` trace）注入 panic；回傳前檢查 link 已移除（以 exit code 回報）。
fn child_panic_after_flash_start() -> ! {
    let vars = process_vars();
    let console = RigFlashConsole::new();
    let platform = flash_daemon_platform(&vars, console.clone());
    let probe = console.clone();
    let hooks = DaemonHooks {
        before_state_commit: None,
        after_state_commit: None,
        trace: Some(Box::new(move |ev| {
            if ev == TraceEvent::Bound {
                if probe.links().len() != 1 {
                    eprintln!("ttyMCU link missing at Bound");
                    std::process::exit(98);
                }
                panic!("{PANIC_MSG}");
            }
        })),
    };
    let code = run_with_hooks(vec!["serialwrapd".to_string()], platform, hooks);
    if console.links().is_empty() {
        eprintln!("ttyMCU link removed after unwind");
        std::process::exit(code);
    }
    eprintln!("ttyMCU link still present after unwind: {:?}", console.links());
    std::process::exit(97);
}

/// RAII：RPC 綁定失敗（exit 1）、正常停止（exit 0）、panic unwind（exit 1）三條退出路徑都移除 ttyMCU link。
#[test]
fn ttymcu_unlinked_after_daemon_exit_all_paths() {
    if std::env::var("S21_CHILD").as_deref() == Ok(PANIC_MODE) {
        child_panic_after_flash_start();
    }

    // (1) RPC 綁定失敗。
    {
        let env = isolated_env();
        let console = RigFlashConsole::new();
        let at_bind = Arc::new(Mutex::new(None));
        let mut platform = flash_daemon_platform(&env.vars, console.clone());
        platform.rpc_listen = Arc::new(FailBind {
            console: console.clone(),
            at_bind: at_bind.clone(),
        });
        let hooks = DaemonHooks {
            before_state_commit: None,
            after_state_commit: None,
            trace: None,
        };
        let code = run_with_hooks(vec!["serialwrapd".to_string()], platform, hooks);
        assert_eq!(code, 1, "RPC 綁定失敗 exit 1");
        assert_eq!(*at_bind.lock().unwrap(), Some(1), "RPC bind 時端點已建立");
        assert!(console.links().is_empty(), "RPC 綁定失敗後 link 已移除：{:?}", console.links());
    }

    // (2) 正常停止（daemon.stop）。
    {
        let env = isolated_env();
        let console = RigFlashConsole::new();
        let platform = flash_daemon_platform(&env.vars, console.clone());
        let hooks = DaemonHooks {
            before_state_commit: None,
            after_state_commit: None,
            trace: None,
        };
        let (code, during) = run_and_stop(&env, platform, hooks, console.clone());
        assert_eq!(code, 0);
        assert!(during.contains_key(&default_link(&env)), "{during:?}");
        assert!(console.links().is_empty(), "正常停止後 link 已移除：{:?}", console.links());
    }

    // (3) panic unwind（子行程；同 `uncaught_panic_exit_1` 的做法）。
    {
        let env = isolated_env();
        let out = run_child_test("ttymcu_unlinked_after_daemon_exit_all_paths", PANIC_MODE, &env);
        assert!(out.stderr.contains(PANIC_MSG), "應走到注入的 panic：{}", out.stderr);
        assert!(out.stderr.contains("ttyMCU link removed after unwind"), "{}", out.stderr);
        assert_eq!(out.code, Some(1), "未攔截 panic 必須 exit 1：{}", out.stderr);
    }
}
