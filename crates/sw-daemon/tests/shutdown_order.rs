//! P8 Task 12：`serialwrapd` 有序停止序（計畫 09 Task 12 的 1～7，含子步驟 1a～1c、5a）。
//!
//! 停止序（arch §6.1）：`server.stop_accept` → `manager.begin_shutdown`（stopping gate）→ `watcher.stop` →
//! `engine.close_intake` → `flash.stop` → `drain.phase1` → `server.join_handlers` → `drain.phase2` → `flush_shutdown` →
//! `engine.stop` → `wal.sync_close` → `lock.release`。任一步逾時或失敗都不跳過後續步驟，只累積到 `incomplete`，
//! 最後 stderr 一行 `serialwrapd: shutdown incomplete: <以 "; " 串接>` 並 exit 1。
//!
//! in-process 案例以 `run_with_hooks` 與 trace 驗證順序；需要斷言 stderr 的案例以子行程模式（`run_child_test`）
//! 執行同一個測試函式，子行程把 trace 步驟以 `TRACE:<步驟>` 寫到 stderr、觀測值以 `OBS:<鍵>=<值>` 寫到 stderr
//! 供父行程比對。時間一律用真時鐘（daemon 的 drain 期限與既有 `SessionManager::shutdown` 相同以 `Instant` 計）。
#![cfg(feature = "test-fakes")]

mod support;

use std::fs;
use std::path::{Path, PathBuf};
use std::process::Command;
use std::sync::atomic::{AtomicBool, Ordering};
use std::sync::{Arc, Condvar, Mutex};
use std::thread;
use std::time::{Duration, Instant};

use support::*;
use sw_core::mailbox::{ControlEvent, DetachReason};
use sw_core::session_manager::SessionManager;
use sw_pal::traits::{
    DeviceInfo, DevicePath, OpenError, PalError, PalResult, SerialBackend, SerialConfig, SerialHandle, SignalWait,
    WatcherEvent,
};
use sw_proto::pyjson::{self, PyValue};

/// 計畫 Task 12 `shutdown_order_steps` 的完整停止序。
const STEPS: [&str; 12] = [
    "server.stop_accept",
    "manager.begin_shutdown",
    "watcher.stop",
    "engine.close_intake",
    "flash.stop",
    "drain.phase1",
    "server.join_handlers",
    "drain.phase2",
    "flush_shutdown",
    "engine.stop",
    "wal.sync_close",
    "lock.release",
];

/// 停止序總上限：flash join 2.0 s＋步驟 3～5（與 5a）共用 5 s；另加排程餘裕。
const STOP_CAP: Duration = Duration::from_millis(2_000 + 5_000 + 1_500);

/// 不回應取消的 handler 停留時間（遠大於停止序總上限；子行程結束即一併終止）。
const HANDLER_STALL: Duration = Duration::from_secs(12);

const INCOMPLETE: &str = "serialwrapd: shutdown incomplete:";

fn device(by_id: &str, real_path: &str) -> (String, DeviceInfo) {
    (
        by_id.to_string(),
        DeviceInfo {
            by_id: by_id.to_string(),
            real_path: real_path.to_string(),
        },
    )
}

fn steps(trace: &[TraceEvent]) -> Vec<&'static str> {
    trace.iter().filter_map(TraceEvent::shutdown_step).collect()
}

fn pos(steps: &[&str], name: &str) -> usize {
    steps
        .iter()
        .position(|s| *s == name)
        .unwrap_or_else(|| panic!("trace 缺 {name}：{steps:?}"))
}

fn sink_hooks(sink: Arc<Mutex<Vec<TraceEvent>>>) -> DaemonHooks {
    DaemonHooks {
        trace: Some(Box::new(move |ev| sink.lock().unwrap().push(ev))),
        ..DaemonHooks::default()
    }
}

fn wait_state(socket: &std::path::Path, com: &str, want: &str) {
    let deadline = Instant::now() + LONG;
    while get_state(socket, com)["state"] != want {
        assert!(Instant::now() < deadline, "{com} 未在 {LONG:?} 內進入 {want}");
        thread::sleep(Duration::from_millis(50));
    }
}

fn push_line(local: &str) -> String {
    format!(
        r#"{{"id":7,"method":"file.push","params":{{"selector":"COM0","local_path":"{local}","remote_path":"/tmp/sw-p8t12.bin"}}}}"#
    )
}

fn incomplete_lines(stderr: &str) -> Vec<&str> {
    stderr.lines().filter(|l| l.starts_with(INCOMPLETE)).collect()
}

fn traced(stderr: &str) -> Vec<&str> {
    stderr.lines().filter_map(|l| l.strip_prefix("TRACE:")).collect()
}

/// 子行程以 `OBS:<鍵>=<值>` 回報的觀測值（缺鍵 panic）。
fn obs<'a>(stderr: &'a str, key: &str) -> &'a str {
    let prefix = format!("OBS:{key}=");
    stderr
        .lines()
        .find_map(|l| l.strip_prefix(prefix.as_str()))
        .unwrap_or_else(|| panic!("子行程未回報 OBS:{key}：{stderr}"))
}

/// 輪詢至條件成立或逾時；不 panic（驅動 thread 據此決定斷言，但一律送出 `daemon.stop`，避免 daemon 永不結束）。
fn poll(limit: Duration, mut f: impl FnMut() -> bool) -> bool {
    let end = Instant::now() + limit;
    while !f() {
        if Instant::now() >= end {
            return false;
        }
        thread::sleep(Duration::from_millis(20));
    }
    true
}

/// WAL 每一筆紀錄（`loads` 失敗的行視為測試失敗）。
fn wal_records(wal: &Path) -> Vec<PyValue> {
    fs::read_to_string(wal)
        .unwrap_or_default()
        .lines()
        .filter(|l| !l.trim().is_empty())
        .map(|l| pyjson::loads(l.trim_end_matches(['\r', '\n'])).unwrap_or_else(|e| panic!("WAL 行不完整：{l:?}：{e:?}")))
        .collect()
}

fn file_len(path: &Path) -> u64 {
    fs::metadata(path).map(|m| m.len()).unwrap_or(0)
}

fn stop_to_exit(stderr: &str) -> Duration {
    let ms = stderr
        .lines()
        .find_map(|l| l.strip_prefix("STOP_TO_EXIT_MS:"))
        .unwrap_or_else(|| panic!("子行程未回報 STOP_TO_EXIT_MS：{stderr}"));
    Duration::from_millis(ms.trim().parse().expect("STOP_TO_EXIT_MS 為整數"))
}

// ---------------------------------------------------------------------------
// 子行程情境：stderr 斷言（flash 端點停不下來、handler 不收斂、兩者同時）
// ---------------------------------------------------------------------------

/// 子行程的失敗注入組合。
#[derive(Clone, Copy)]
struct Inject {
    /// 端點 thread 忽略 `EndpointShutdown`（`flash endpoint stop incomplete`）。
    block_flash: bool,
    /// `file.push` handler 在分派前停留 [`HANDLER_STALL`]、不回應取消（`rpc handlers did not converge`）。
    stall_push: bool,
    /// COM0 READY 後提交一筆卡在 writer（BlockForever）的命令（`drain stalled`），且 WAL `sync` 一律失敗
    /// （`WAL sync failed`）。
    stuck_drain_and_wal: bool,
}

/// 子行程本體：`stuck_drain_and_wal` 時為 shell template＋target COM0，否則無裝置（只有 passthrough template）。
/// 驅動 thread 於 RPC 就緒後依注入項目做準備（等 READY 並提交卡住的命令、讓 `file.push` 進入 handler）再送
/// `daemon.stop`，並記錄送出時刻；結束前回報停止耗時。前提不成立時不 panic、照樣送 stop，由父行程的斷言收場。
fn child_main(inject: Inject) -> ! {
    let vars = process_vars();
    let socket = PathBuf::from(&vars["SERIALWRAP_RUN_DIR"]).join("serialwrapd.sock");
    let (devices, fixture) = if inject.stuck_drain_and_wal {
        (vec![device("usb-SER-A-if00", "/dev/pts/10")], Fixture::BlockForeverWriterWalSyncFailure)
    } else {
        (Vec::new(), Fixture::None)
    };
    let (platform, _h) = in_process_platform_with_handles(&vars, devices, fixture);
    let (block_flash, stall_push) = (inject.block_flash, inject.stall_push);
    let entered = Arc::new(AtomicBool::new(false));
    let stop_at: Arc<Mutex<Option<Instant>>> = Arc::new(Mutex::new(None));
    let rpc_call: Option<Box<dyn Fn(&str) + Send + Sync>> = if stall_push {
        let entered = entered.clone();
        Some(Box::new(move |m: &str| {
            if m == "file.push" {
                entered.store(true, Ordering::SeqCst);
                thread::sleep(HANDLER_STALL);
            }
        }))
    } else {
        None
    };
    {
        let socket = socket.clone();
        let stop_at = stop_at.clone();
        thread::spawn(move || {
            wait_socket(&socket, LONG);
            if inject.stuck_drain_and_wal {
                let ready = poll(LONG, || get_state(&socket, "COM0")["state"] == "READY");
                let submitted = ready && submit(&socket, "COM0", "stuck")["ok"] == true;
                let busy = submitted && poll(SHORT, || get_state(&socket, "COM0")["foreground_busy"] == true);
                eprintln!("OBS:stuck_running={busy}");
                thread::sleep(Duration::from_millis(300));
            }
            if stall_push {
                let s = socket.clone();
                thread::spawn(move || rpc_once(&s, &push_line("/nonexistent/sw-p8t12.bin")));
                // 守衛：不論 handler 是否進入都送 stop（未接線時以父行程的斷言失敗收場，不讓 daemon 一直服務）。
                let deadline = Instant::now() + SHORT;
                while !entered.load(Ordering::SeqCst) && Instant::now() < deadline {
                    thread::sleep(Duration::from_millis(10));
                }
            }
            *stop_at.lock().unwrap() = Some(Instant::now());
            rpc_once(&socket, STOP);
        });
    }
    let hooks = DaemonHooks {
        trace: Some(Box::new(|ev| {
            if let Some(step) = ev.shutdown_step() {
                eprintln!("TRACE:{step}");
            }
        })),
        block_flash_endpoint_stop: block_flash,
        rpc_call,
        ..DaemonHooks::default()
    };
    let code = run_with_hooks(vec!["serialwrapd".to_string()], platform, hooks);
    let ms = stop_at.lock().unwrap().map_or(u128::MAX, |t| t.elapsed().as_millis());
    eprintln!("STOP_TO_EXIT_MS:{ms}");
    std::process::exit(code);
}

fn child_entry(test: &str) {
    let none = Inject {
        block_flash: false,
        stall_push: false,
        stuck_drain_and_wal: false,
    };
    match std::env::var("S21_CHILD").as_deref() {
        Ok("flash") if test == "flash_endpoint_stall_goes_to_incomplete_and_exit_1" => child_main(Inject {
            block_flash: true,
            ..none
        }),
        Ok("handler") if test == "handlers_not_converged_lists_methods" => child_main(Inject {
            stall_push: true,
            ..none
        }),
        Ok("all") if test == "multiple_failures_accumulate_in_stop_order" => child_main(Inject {
            block_flash: true,
            stall_push: true,
            stuck_drain_and_wal: true,
        }),
        Ok("hotplug") if test == "shutdown_hotplug_after_phase1_never_opens_uart" => hotplug_child(),
        _ => {}
    }
}

#[test]
fn flash_endpoint_stall_goes_to_incomplete_and_exit_1() {
    child_entry("flash_endpoint_stall_goes_to_incomplete_and_exit_1");
    let env = isolated_env();
    let out = run_child_test("flash_endpoint_stall_goes_to_incomplete_and_exit_1", "flash", &env);
    assert_eq!(out.code, Some(1), "flash 端點未收斂不得宣告 graceful success：{}", out.stderr);
    assert_eq!(
        incomplete_lines(&out.stderr),
        vec!["serialwrapd: shutdown incomplete: flash endpoint stop incomplete: endpoint"],
        "{}",
        out.stderr
    );
    // 2.0 s join 逾時後照常走完 3～7。
    assert_eq!(traced(&out.stderr), STEPS.to_vec(), "{}", out.stderr);
    let took = stop_to_exit(&out.stderr);
    assert!(took >= Duration::from_secs(2), "flash 端點 join 應等滿 2.0 s：{took:?}");
    assert!(took <= STOP_CAP, "停止序不得無界等待：{took:?}");
}

#[test]
fn handlers_not_converged_lists_methods() {
    child_entry("handlers_not_converged_lists_methods");
    let env = isolated_env();
    let out = run_child_test("handlers_not_converged_lists_methods", "handler", &env);
    assert_eq!(out.code, Some(1), "handler 未收斂不得 exit 0：{}", out.stderr);
    assert_eq!(
        incomplete_lines(&out.stderr),
        vec!["serialwrapd: shutdown incomplete: rpc handlers did not converge: file.push"],
        "{}",
        out.stderr
    );
    assert_eq!(traced(&out.stderr), STEPS.to_vec(), "join 逾時後照常走完後續步驟：{}", out.stderr);
    let took = stop_to_exit(&out.stderr);
    assert!(took <= STOP_CAP, "join_handlers 以剩餘預算為限、不得無界等待：{took:?}");
}

#[test]
fn multiple_failures_accumulate_in_stop_order() {
    // 計畫 `shutdown_multiple_failures_accumulate_exits_1`：flash receipt `joined:false`＋handler 逾時（file.push）＋
    // COM0 drain stalled＋WAL `sync` 失敗同時注入；四項依停止序累積成單行，任一步失敗都不提早返回（lock.release 照常）。
    child_entry("multiple_failures_accumulate_in_stop_order");
    let env = isolated_env();
    let out = run_child_test("multiple_failures_accumulate_in_stop_order", "all", &env);
    assert_eq!(obs(&out.stderr, "stuck_running"), "true", "前提：卡住的命令已在執行：{}", out.stderr);
    assert_eq!(out.code, Some(1), "{}", out.stderr);
    let wal_err = PalError::from_errno(5).py_message;
    let want = format!(
        "{INCOMPLETE} flash endpoint stop incomplete: endpoint; rpc handlers did not converge: file.push; \
         drain stalled: t:COM0; WAL sync failed: {wal_err}"
    );
    assert_eq!(
        incomplete_lines(&out.stderr),
        vec![want.as_str()],
        "失敗依停止序累積成單行：{}",
        out.stderr
    );
    assert_eq!(traced(&out.stderr), STEPS.to_vec(), "{}", out.stderr);
    assert!(stop_to_exit(&out.stderr) <= STOP_CAP, "{}", out.stderr);
}

// ---------------------------------------------------------------------------
// in-process 情境：步驟順序與長傳輸
// ---------------------------------------------------------------------------

/// 事件 log（`SERIALWRAP_EVENTS_RUNTIME_DIR/events.ndjson`）中每筆事件的 `type`。
fn event_types(log: &Path) -> Vec<String> {
    fs::read_to_string(log)
        .unwrap_or_default()
        .lines()
        .filter_map(|l| {
            let v = pyjson::loads(l).ok()?;
            v.get("type").and_then(|t| t.as_str()).map(str::to_string)
        })
        .collect()
}

#[test]
fn order_is_exactly_1_to_7() {
    // 計畫 `shutdown_order_steps`：含一條已啟用的 event rule（命中 bootstrap ready probe 回應的 `__READY__`），
    // engine 已實際派工過 handler，停止序仍恰為 12 步。
    let env = isolated_env();
    fs::write(
        PathBuf::from(&env.vars["SERIALWRAP_EVENTS_DIR"]).join("o.order.json"),
        r#"{"schema_version": 1, "owner": "o", "name": "order", "kind": "tool", "selectors": ["COM0"],
            "pattern": {"kind": "contains", "value": "__READY__"}, "handler": {"exec": ["true"]}}"#,
    )
    .expect("寫入 event rule");
    let sink = Arc::new(Mutex::new(Vec::new()));
    let (platform, _h) = in_process_platform_with_handles(
        &env.vars,
        vec![device("usb-SER-A-if00", "/dev/pts/10")],
        Fixture::ReadyShellBlockingWithQueue,
    );
    let socket = env.socket.clone();
    let log = PathBuf::from(&env.vars["SERIALWRAP_EVENTS_RUNTIME_DIR"]).join("events.ndjson");
    let events_log = log.clone();
    let client = thread::spawn(move || {
        wait_socket(&socket, SHORT);
        wait_state(&socket, "COM0", "READY");
        let fired = poll(SHORT, || event_types(&events_log).iter().any(|t| t == "fire_completed"));
        rpc_once(&socket, STOP);
        fired
    });
    let code = run_with_hooks(vec!["serialwrapd".to_string()], platform, sink_hooks(sink.clone()));
    let fired = client.join().expect("client thread");
    let types = event_types(&log);
    assert!(types.iter().any(|t| t == "rule_loaded"), "前提：event rule 已載入：{types:?}");
    assert!(fired, "前提：engine 已派工並完成一個 handler：{types:?}");
    assert_eq!(code, 0, "全部步驟收斂應 exit 0");
    let trace = sink.lock().unwrap().clone();
    assert_eq!(steps(&trace), STEPS.to_vec(), "{trace:?}");
    assert!(
        trace.contains(&TraceEvent::HandlersJoined { pending: vec![] }),
        "無進行中 handler 時 join 回空清單：{trace:?}"
    );
    assert!(
        trace.contains(&TraceEvent::EngineStopped { unreaped: vec![] }),
        "已完成的 handler 不列入 unreaped：{trace:?}"
    );
}

#[test]
fn stopping_gate_before_watcher_stop() {
    // gate（1a）先於 watcher.stop（1b）：watcher 最後一輪送出的 Delta 也被擋下，不得 open 新裝置。
    let env = isolated_env();
    let sink = Arc::new(Mutex::new(Vec::new()));
    let a = device("usb-SER-A-if00", "/dev/pts/10");
    let new = device("usb-SER-NEW-if00", "/dev/pts/11");
    let (platform, h) = in_process_platform_with_handles(&env.vars, vec![a.clone(), new.clone()], Fixture::None);
    let script = h.script.clone();
    let (dev_a, dev_new) = (h.devices[0].clone(), h.devices[1].clone());
    let s2 = sink.clone();
    let hooks = DaemonHooks {
        trace: Some(Box::new(move |ev| {
            match &ev {
                // 初始掃描只看見 A（NEW 只登錄在假序列埠後端）。
                TraceEvent::InitialScan => {
                    script.lock().unwrap().as_ref().expect("裝置來源已建立").set(vec![a.clone()]);
                }
                // gate 設下後才熱插 NEW，並等過 watcher 一輪輪詢（1 s），讓最後一輪 Delta 進 channel。
                TraceEvent::ShutdownGate => {
                    script.lock().unwrap().as_ref().expect("裝置來源已建立").set(vec![a.clone(), new.clone()]);
                    thread::sleep(Duration::from_millis(1_500));
                }
                _ => {}
            }
            s2.lock().unwrap().push(ev);
        })),
        ..DaemonHooks::default()
    };
    let socket = env.socket.clone();
    let opened_a = Arc::new(AtomicBool::new(false));
    let client = {
        let opened_a = opened_a.clone();
        let dev_a = dev_a.clone();
        thread::spawn(move || {
            wait_socket(&socket, SHORT);
            let deadline = Instant::now() + SHORT;
            while dev_a.open_count() == 0 && Instant::now() < deadline {
                thread::sleep(Duration::from_millis(20));
            }
            opened_a.store(dev_a.open_count() > 0, Ordering::SeqCst);
            rpc_once(&socket, STOP);
        })
    };
    let code = run_with_hooks(vec!["serialwrapd".to_string()], platform, hooks);
    client.join().expect("client thread");
    assert!(opened_a.load(Ordering::SeqCst), "前提：動態偵測會 open 已出現的裝置");
    assert_eq!(dev_new.open_count(), 0, "stopping gate 之後熱插的裝置不得被 open");
    assert_eq!(code, 0);
    let s = steps(&sink.lock().unwrap());
    assert!(pos(&s, "server.stop_accept") < pos(&s, "manager.begin_shutdown"), "{s:?}");
    assert!(pos(&s, "manager.begin_shutdown") < pos(&s, "watcher.stop"), "{s:?}");
    assert!(pos(&s, "watcher.stop") < pos(&s, "engine.close_intake"), "{s:?}");
    assert!(pos(&s, "engine.close_intake") < pos(&s, "drain.phase1"), "{s:?}");
}

#[test]
fn long_file_push_does_not_block_stop() {
    // 600 s 上限的 file.push 進行中（第一筆 TX 卡在 writer）收到停止：Phase 1 取消傳輸，handler 回覆後
    // join 不逾時，整個停止序在上限內走完。
    let env = isolated_env();
    let local = env.root.join("push.bin");
    fs::write(&local, vec![0x5a_u8; 64 * 1024]).expect("寫入 push 來源檔");
    let local = local.to_string_lossy().into_owned();
    let sink = Arc::new(Mutex::new(Vec::new()));
    let (platform, _h) = in_process_platform_with_handles(
        &env.vars,
        vec![device("usb-SER-A-if00", "/dev/pts/10")],
        Fixture::ReadyShellBlockingWithQueue,
    );
    let socket = env.socket.clone();
    let client = thread::spawn(move || {
        wait_socket(&socket, SHORT);
        wait_state(&socket, "COM0", "READY");
        let s = socket.clone();
        let push = thread::spawn(move || rpc_once(&s, &push_line(&local)));
        let deadline = Instant::now() + SHORT;
        while get_state(&socket, "COM0")["foreground_busy"] != true {
            assert!(Instant::now() < deadline, "file.push 未進入前景");
            thread::sleep(Duration::from_millis(20));
        }
        thread::sleep(Duration::from_millis(300));
        let t0 = Instant::now();
        rpc_once(&socket, STOP);
        (t0, push.join().expect("push thread"))
    });
    let code = run_with_hooks(vec!["serialwrapd".to_string()], platform, sink_hooks(sink.clone()));
    let returned = Instant::now();
    let (t0, reply) = client.join().expect("client thread");
    let took = returned.duration_since(t0);
    assert!(took <= STOP_CAP, "進行中的長傳輸不得卡住停止序：{took:?}");
    let trace = sink.lock().unwrap().clone();
    assert!(
        trace.contains(&TraceEvent::HandlersJoined { pending: vec![] }),
        "Phase 1 取消傳輸後 handler 應在 join 前收斂：{trace:?}"
    );
    assert_eq!(steps(&trace), STEPS.to_vec(), "{trace:?}");
    let stalled = trace
        .iter()
        .find_map(|e| match e {
            TraceEvent::DrainPhase2 { stalled, .. } => Some(stalled.clone()),
            _ => None,
        })
        .expect("trace 含 drain.phase2");
    assert_eq!(code, if stalled.is_empty() { 0 } else { 1 }, "exit code 依 drain 收斂而定：{trace:?}");
    let reply = match pyjson::loads(&reply) {
        Ok(PyValue::Dict(d)) => d,
        other => panic!("file.push 應收到回覆：{reply:?} → {other:?}"),
    };
    assert_eq!(reply.get("ok"), Some(&PyValue::Bool(false)), "{reply:?}");
    let code = reply.get("error_code").and_then(PyValue::as_str).unwrap_or_default().to_string();
    assert!(
        ["COMMAND_ABORTED_IN_FLIGHT", "FLUSHED_BY_RECOVERY"].contains(&code.as_str()),
        "OD-62：停止中止的傳輸回 COMMAND_ABORTED_IN_FLIGHT／FLUSHED_BY_RECOVERY：{reply:?}"
    );
}

/// 停止訊號替身：`wait_stop` 等測試注入的訊號或 `daemon.stop`。
struct TestSignals(crossbeam_channel::Receiver<()>);

impl SignalWait for TestSignals {
    fn install(&self) -> PalResult<()> {
        Ok(())
    }
    fn wait_stop(&self, extra: crossbeam_channel::Receiver<()>) {
        crossbeam_channel::select! {
            recv(self.0) -> _ => {},
            recv(extra) -> _ => {},
        }
    }
}

#[test]
fn second_sigterm_changes_nothing() {
    // 停止序進行中（drain.phase1 時）再收到一次停止訊號：每一步仍只執行一次，結果不變。
    let env = isolated_env();
    let sink = Arc::new(Mutex::new(Vec::new()));
    let (sig_tx, sig_rx) = crossbeam_channel::bounded(4);
    let (mut platform, _h) = in_process_platform_with_handles(
        &env.vars,
        vec![device("usb-SER-A-if00", "/dev/pts/10")],
        Fixture::ReadyShellBlockingWithQueue,
    );
    platform.signals = Arc::new(TestSignals(sig_rx));
    let s2 = sink.clone();
    let again = sig_tx.clone();
    let hooks = DaemonHooks {
        trace: Some(Box::new(move |ev| {
            if ev == TraceEvent::DrainPhase1 {
                let _ = again.try_send(());
            }
            s2.lock().unwrap().push(ev);
        })),
        ..DaemonHooks::default()
    };
    let socket = env.socket.clone();
    let client = thread::spawn(move || {
        wait_socket(&socket, SHORT);
        wait_state(&socket, "COM0", "READY");
        sig_tx.send(()).expect("送出第一次停止訊號");
    });
    let code = run_with_hooks(vec!["serialwrapd".to_string()], platform, hooks);
    client.join().expect("client thread");
    assert_eq!(code, 0);
    assert_eq!(steps(&sink.lock().unwrap()), STEPS.to_vec(), "每一步恰一次");
}

#[test]
fn second_sigterm_real_signal_exits_0() {
    // 真 daemon 行程連收兩次 SIGTERM：第二次只記 log，仍有序停止 exit 0、不報 shutdown incomplete。
    let env = isolated_env_with_pty_target();
    let mut d = DaemonProc::spawn(&env, &[]);
    wait_ready(&env, "COM0");
    for _ in 0..2 {
        let status = Command::new("kill")
            .arg("-TERM")
            .arg(d.pid().to_string())
            .status()
            .expect("送出 SIGTERM");
        assert!(status.success());
        thread::sleep(Duration::from_millis(30));
    }
    assert_eq!(d.wait(LONG).code(), Some(0), "{}", d.stderr());
    assert!(!d.stderr().contains(INCOMPLETE), "{}", d.stderr());
    assert!(!env.socket.exists(), "有序停機後 socket 檔應已移除");
}

#[test]
fn engine_stop_before_wal_close() {
    // 5a engine.stop 在 flush_shutdown 之後、wal.sync_close 之前：handler 行程樹不得活過 WAL close 與 lock.release。
    let env = isolated_env();
    let sink = Arc::new(Mutex::new(Vec::new()));
    let (platform, _h) = in_process_platform_with_handles(
        &env.vars,
        vec![device("usb-SER-A-if00", "/dev/pts/10")],
        Fixture::ReadyShellBlockingWithQueue,
    );
    let socket = env.socket.clone();
    let client = thread::spawn(move || {
        wait_socket(&socket, SHORT);
        wait_state(&socket, "COM0", "READY");
        rpc_once(&socket, STOP);
    });
    let code = run_with_hooks(vec!["serialwrapd".to_string()], platform, sink_hooks(sink.clone()));
    client.join().expect("client thread");
    assert_eq!(code, 0);
    let trace = sink.lock().unwrap().clone();
    let s = steps(&trace);
    assert!(pos(&s, "flush_shutdown") < pos(&s, "engine.stop"), "{s:?}");
    assert!(pos(&s, "engine.stop") < pos(&s, "wal.sync_close"), "{s:?}");
    assert!(pos(&s, "wal.sync_close") < pos(&s, "lock.release"), "{s:?}");
    assert!(
        trace.contains(&TraceEvent::EngineStopped { unreaped: vec![] }),
        "無執行中 handler 時 engine.stop 回條 unreaped 為空：{trace:?}"
    );
}

// ---------------------------------------------------------------------------
// R1-F2：Phase 1 之後的熱插與延後 reconcile 不得開 UART；越過 gate 的在途 attach 納入 drain
// ---------------------------------------------------------------------------

const BY_ID_A: &str = "usb-SER-A-if00";
const BY_ID_NEW: &str = "usb-SER-NEW-if00";
/// COM0 的 by_id 改指（Rebound）後的新 real_path。
const REBOUND_PATH: &str = "/dev/pts/12";

/// 子行程本體（`shutdown_hotplug_after_phase1_never_opens_uart`）：COM0 READY；stopping gate 時記下
/// `spawned_worker_count` 基準並讓 COM0 的下一次 close 停在暫停點（Phase 1 後 COM0 仍在 drain 中、actor 仍存活），
/// `drain.phase1` 後：(a) 裝置來源新增 NEW 並直接經 `watcher_sender()` 注入 watcher 最後一輪殘留的 Delta
/// （同時 COM0 的 by_id 改指 [`REBOUND_PATH`]），(b) 對 COM0 注入 `DeviceDetached(Rebound)` 與新 real_path 的
/// `DeviceAttached`（模擬 SC-32 延後的 reconcile），等 manager／actor 處理後才放行 close。
fn hotplug_child() -> ! {
    let vars = process_vars();
    let socket = PathBuf::from(&vars["SERIALWRAP_RUN_DIR"]).join("serialwrapd.sock");
    let a = device(BY_ID_A, "/dev/pts/10");
    let new = device(BY_ID_NEW, "/dev/pts/11");
    let a_rebound = device(BY_ID_A, REBOUND_PATH);
    let (platform, h) = in_process_platform_with_handles(
        &vars,
        vec![a.clone(), new.clone()],
        Fixture::ReadyShellBlockingWithQueue,
    );
    let dev_rebound = h.serial.add_device(REBOUND_PATH);
    let (dev_a, dev_new) = (h.devices[0].clone(), h.devices[1].clone());
    let script = h.script.clone();
    let mgr: Arc<Mutex<Option<Arc<SessionManager>>>> = Arc::new(Mutex::new(None));
    let baseline = Arc::new(Mutex::new(None::<usize>));
    let slot = mgr.clone();
    let hooks = DaemonHooks {
        on_manager: Some(Box::new(move |m| *slot.lock().unwrap() = Some(m))),
        trace: Some(Box::new(move |ev| {
            if let Some(step) = ev.shutdown_step() {
                eprintln!("TRACE:{step}");
            }
            let m = mgr.lock().unwrap().clone();
            match &ev {
                // 初始掃描只看見 A（NEW 只登錄在假序列埠後端）。
                TraceEvent::InitialScan => {
                    script.lock().unwrap().as_ref().expect("裝置來源已建立").set(vec![a.clone()]);
                }
                TraceEvent::ShutdownGate => {
                    let m = m.expect("on_manager 已提供 manager");
                    *baseline.lock().unwrap() = Some(m.spawned_worker_count());
                    dev_a.pause_next_close();
                }
                TraceEvent::DrainPhase1 => {
                    let m = m.expect("on_manager 已提供 manager");
                    eprintln!("OBS:mid_drain={}", dev_a.wait_paused(SHORT));
                    let snapshot = vec![a_rebound.clone(), new.clone()];
                    script.lock().unwrap().as_ref().expect("裝置來源已建立").set(snapshot.clone());
                    let delta = m
                        .watcher_sender()
                        .send(WatcherEvent::Delta {
                            added: vec![new.1.clone()],
                            removed: Vec::new(),
                            snapshot,
                        })
                        .is_ok();
                    let detached = m.send_control(
                        "COM0",
                        ControlEvent::DeviceDetached {
                            by_id: BY_ID_A.to_string(),
                            reason: DetachReason::Rebound,
                        },
                    );
                    let attached = m.send_control(
                        "COM0",
                        ControlEvent::DeviceAttached {
                            by_id: BY_ID_A.to_string(),
                            real_path: REBOUND_PATH.to_string(),
                        },
                    );
                    eprintln!("OBS:injected={delta},{detached},{attached}");
                    // 讓 manager thread 處理 Delta、actor 處理兩個 control 事件後才放行 COM0 的 close。
                    thread::sleep(Duration::from_millis(500));
                    dev_a.release_pause();
                }
                TraceEvent::DrainPhase2 { drained, .. } => {
                    let set: Vec<String> = drained.iter().map(|(s, ok)| format!("{s}:{ok}")).collect();
                    eprintln!("OBS:drained={}", set.join(","));
                }
                TraceEvent::LockReleased => {
                    let m = m.expect("on_manager 已提供 manager");
                    let base = baseline.lock().unwrap().expect("gate 時已記錄基準");
                    eprintln!("OBS:worker_delta={}", m.spawned_worker_count() as i64 - base as i64);
                    eprintln!("OBS:new_open={}", dev_new.open_count());
                    eprintln!("OBS:rebound_open={}", dev_rebound.open_count());
                    // 釋放測試持有的 manager，使其 thread 隨 daemon 收尾結束。
                    mgr.lock().unwrap().take();
                }
                _ => {}
            }
        })),
        ..DaemonHooks::default()
    };
    thread::spawn(move || {
        wait_socket(&socket, LONG);
        let ready = poll(LONG, || get_state(&socket, "COM0")["state"] == "READY");
        eprintln!("OBS:ready={ready}");
        rpc_once(&socket, STOP);
    });
    let code = run_with_hooks(vec!["serialwrapd".to_string()], platform, hooks);
    std::process::exit(code);
}

#[test]
fn shutdown_hotplug_after_phase1_never_opens_uart() {
    child_entry("shutdown_hotplug_after_phase1_never_opens_uart");
    let env = isolated_env();
    let out = run_child_test("shutdown_hotplug_after_phase1_never_opens_uart", "hotplug", &env);
    let err = &out.stderr;
    assert_eq!(obs(err, "ready"), "true", "前提：COM0 READY：{err}");
    assert_eq!(obs(err, "mid_drain"), "true", "前提：注入時 COM0 仍在 drain 中（actor 存活）：{err}");
    assert_eq!(obs(err, "injected"), "true,true,true", "前提：Delta 與兩個 control 事件都已送達：{err}");
    assert_eq!(obs(err, "new_open"), "0", "Phase 1 之後熱插的裝置不得被 open：{err}");
    assert_eq!(obs(err, "rebound_open"), "0", "Phase 1 之後的 Rebound reconcile 不得重開新 real_path：{err}");
    assert_eq!(obs(err, "worker_delta"), "0", "stopping gate 之後不得新增 session／attach worker：{err}");
    assert_eq!(obs(err, "drained"), "t:COM0:true", "drain 回條集合恰為 COM0：{err}");
    assert!(
        err.lines().any(|l| l == format!("shutdown 中，略過 attach {BY_ID_NEW}")),
        "gate 擋下熱插須記 log：{err}"
    );
    assert_eq!(traced(err), STEPS.to_vec(), "{err}");
    assert!(incomplete_lines(err).is_empty(), "{err}");
    assert_eq!(out.code, Some(0), "{err}");
    let coms: Vec<String> = wal_records(&env.wal_dir.join("raw.wal.ndjson"))
        .iter()
        .filter_map(|r| r.get("com").and_then(PyValue::as_str).map(str::to_string))
        .filter(|c| c != "COM0")
        .collect();
    assert!(coms.is_empty(), "WAL 不得有 COM0 以外（熱插裝置）的 TX／RX 紀錄：{coms:?}");
}

/// open 閘門：指定路徑的 `open_exclusive` 在呼叫委派前阻塞到 `release`（上限 [`LONG`]，避免測試永久卡住）。
struct OpenGate {
    path: String,
    entered: AtomicBool,
    released: Mutex<bool>,
    cv: Condvar,
}

impl OpenGate {
    fn new(path: &str) -> Arc<OpenGate> {
        Arc::new(OpenGate {
            path: path.to_string(),
            entered: AtomicBool::new(false),
            released: Mutex::new(false),
            cv: Condvar::new(),
        })
    }

    fn release(&self) {
        *self.released.lock().unwrap() = true;
        self.cv.notify_all();
    }
}

/// 序列埠後端包裝：[`OpenGate`] 指定的路徑在 open 前阻塞（模擬已越過 stopping gate、卡在 OS open 的 attach worker）。
struct GatedSerial {
    inner: Arc<dyn SerialBackend>,
    gate: Arc<OpenGate>,
}

impl SerialBackend for GatedSerial {
    fn open_exclusive(&self, dev: &DevicePath, cfg: &SerialConfig) -> Result<Box<dyn SerialHandle>, OpenError> {
        if dev.0 == self.gate.path {
            self.gate.entered.store(true, Ordering::SeqCst);
            let released = self.gate.released.lock().unwrap();
            let _ = self.gate.cv.wait_timeout_while(released, LONG, |r| !*r).unwrap();
        }
        self.inner.open_exclusive(dev, cfg)
    }
}

#[test]
fn shutdown_inflight_attach_drained_before_wal_close() {
    // 計畫 `shutdown_inflight_attach_drained_before_wal_close`（R1-F2）：熱插 NEW 使 attach worker 卡在 open（已越過
    // gate）時送 stop，於 `drain.phase1` 後才放行 open。該 attach 列入 drain 回條且已收斂；其 handle 恰關閉一次且
    // 在 `wal.sync_close` 之前；假 DUT 零 bytes；`wal.sync_close` 之後 WAL 不再增長。
    let env = isolated_env();
    let new = device(BY_ID_NEW, "/dev/pts/11");
    let (mut platform, h) = in_process_platform_with_handles(&env.vars, vec![new.clone()], Fixture::None);
    let dev_new = h.devices[0].clone();
    let gate = OpenGate::new("/dev/pts/11");
    platform.serial = Arc::new(GatedSerial {
        inner: platform.serial.clone(),
        gate: gate.clone(),
    });
    let wal = env.wal_dir.join("raw.wal.ndjson");
    let sink = Arc::new(Mutex::new(Vec::new()));
    let closes_before_wal_close = Arc::new(Mutex::new(None::<usize>));
    let wal_len_at_close = Arc::new(Mutex::new(None::<u64>));
    let script = h.script.clone();
    let hooks = {
        let (sink, gate, dev_new) = (sink.clone(), gate.clone(), dev_new.clone());
        let (closes, wal_len, wal) = (closes_before_wal_close.clone(), wal_len_at_close.clone(), wal.clone());
        let script = script.clone();
        DaemonHooks {
            trace: Some(Box::new(move |ev| {
                match &ev {
                    // 初始掃描看不到任何裝置；NEW 於 daemon 就緒後才熱插。
                    TraceEvent::InitialScan => {
                        script.lock().unwrap().as_ref().expect("裝置來源已建立").set(Vec::new());
                    }
                    TraceEvent::DrainPhase1 => gate.release(),
                    // 5a 緊接在 6 `wal.sync_close` 之前。
                    TraceEvent::EngineStopped { .. } => *closes.lock().unwrap() = Some(dev_new.close_count()),
                    TraceEvent::WalShutdown { .. } => *wal_len.lock().unwrap() = Some(file_len(&wal)),
                    _ => {}
                }
                sink.lock().unwrap().push(ev);
            })),
            ..DaemonHooks::default()
        }
    };
    let socket = env.socket.clone();
    let client = {
        let gate = gate.clone();
        thread::spawn(move || {
            wait_socket(&socket, SHORT);
            script.lock().unwrap().as_ref().expect("裝置來源已建立").set(vec![new]);
            let entered = poll(SHORT, || gate.entered.load(Ordering::SeqCst));
            rpc_once(&socket, STOP);
            entered
        })
    };
    let code = run_with_hooks(vec!["serialwrapd".to_string()], platform, hooks);
    let entered = client.join().expect("client thread");
    thread::sleep(Duration::from_millis(200));
    assert!(entered, "前提：熱插的 attach worker 已越過 gate、卡在 open");
    assert_eq!(code, 0);
    let trace = sink.lock().unwrap().clone();
    assert_eq!(steps(&trace), STEPS.to_vec(), "{trace:?}");
    let drained = trace
        .iter()
        .find_map(|e| match e {
            TraceEvent::DrainPhase2 { drained, .. } => Some(drained.clone()),
            _ => None,
        })
        .expect("trace 含 drain.phase2");
    assert_eq!(
        drained,
        vec![(format!("attach:{BY_ID_NEW}"), true)],
        "在途 attach 納入 drain 回條且已收斂：{trace:?}"
    );
    assert_eq!(dev_new.open_count(), 1, "放行後 open 恰一次");
    assert_eq!(*closes_before_wal_close.lock().unwrap(), Some(1), "handle 必須在 wal.sync_close 之前關閉");
    assert_eq!(dev_new.close_count(), 1, "handle 恰關閉一次");
    assert_eq!(dev_new.tx_len(), 0, "已 stopping 的 attach 不得對假 DUT 送任何 bytes");
    let at_close = wal_len_at_close.lock().unwrap().expect("trace 含 wal.sync_close");
    assert_eq!(file_len(&wal), at_close, "wal.sync_close 之後 WAL 不得再增長");
}
