//! P8 Task 12：`serialwrapd` 有序停止序（計畫 09 Task 12 的 1～7，含子步驟 1a～1c、5a）。
//!
//! 停止序（arch §6.1）：`server.stop_accept` → `manager.begin_shutdown`（stopping gate）→ `watcher.stop` →
//! `engine.close_intake` → `flash.stop` → `drain.phase1` → `server.join_handlers` → `drain.phase2` → `flush_shutdown` →
//! `engine.stop` → `wal.sync_close` → `lock.release`。任一步逾時或失敗都不跳過後續步驟，只累積到 `incomplete`，
//! 最後 stderr 一行 `serialwrapd: shutdown incomplete: <以 "; " 串接>` 並 exit 1。
//!
//! in-process 案例以 `run_with_hooks` 與 trace 驗證順序；需要斷言 stderr 的案例以子行程模式（`run_child_test`）
//! 執行同一個測試函式，子行程把 trace 步驟以 `TRACE:<步驟>` 寫到 stderr 供父行程比對。時間一律用真時鐘
//! （daemon 的 drain 期限與既有 `SessionManager::shutdown` 相同以 `Instant` 計）。
#![cfg(feature = "test-fakes")]

mod support;

use std::fs;
use std::path::PathBuf;
use std::process::Command;
use std::sync::atomic::{AtomicBool, Ordering};
use std::sync::{Arc, Mutex};
use std::thread;
use std::time::{Duration, Instant};

use support::*;
use sw_pal::traits::{DeviceInfo, PalResult, SignalWait};
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

/// 子行程本體：無裝置（只有 passthrough template）；`block_flash` 使端點 thread 忽略 `EndpointShutdown`，
/// `stall_push` 使 `file.push` handler 在分派前停留 [`HANDLER_STALL`]（不回應取消）。驅動 thread 於 RPC 就緒後
/// （必要時先讓 `file.push` 進入 handler）送 `daemon.stop`，並記錄送出時刻；結束前回報停止耗時。
fn child_main(block_flash: bool, stall_push: bool) -> ! {
    let vars = process_vars();
    let socket = PathBuf::from(&vars["SERIALWRAP_RUN_DIR"]).join("serialwrapd.sock");
    let (platform, _h) = in_process_platform_with_handles(&vars, Vec::new(), Fixture::None);
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
    match std::env::var("S21_CHILD").as_deref() {
        Ok("flash") if test == "flash_endpoint_stall_goes_to_incomplete_and_exit_1" => child_main(true, false),
        Ok("handler") if test == "handlers_not_converged_lists_methods" => child_main(false, true),
        Ok("both") if test == "multiple_failures_accumulate_in_stop_order" => child_main(true, true),
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
    child_entry("multiple_failures_accumulate_in_stop_order");
    let env = isolated_env();
    let out = run_child_test("multiple_failures_accumulate_in_stop_order", "both", &env);
    assert_eq!(out.code, Some(1), "{}", out.stderr);
    assert_eq!(
        incomplete_lines(&out.stderr),
        vec![
            "serialwrapd: shutdown incomplete: flash endpoint stop incomplete: endpoint; rpc handlers did not converge: file.push"
        ],
        "失敗依停止序累積成單行：{}",
        out.stderr
    );
    assert_eq!(traced(&out.stderr), STEPS.to_vec(), "{}", out.stderr);
    assert!(stop_to_exit(&out.stderr) <= STOP_CAP, "{}", out.stderr);
}

// ---------------------------------------------------------------------------
// in-process 情境：步驟順序與長傳輸
// ---------------------------------------------------------------------------

#[test]
fn order_is_exactly_1_to_7() {
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
    assert_eq!(code, 0, "全部步驟收斂應 exit 0");
    let trace = sink.lock().unwrap().clone();
    assert_eq!(steps(&trace), STEPS.to_vec(), "{trace:?}");
    assert!(
        trace.contains(&TraceEvent::HandlersJoined { pending: vec![] }),
        "無進行中 handler 時 join 回空清單：{trace:?}"
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
            TraceEvent::DrainPhase2 { stalled } => Some(stalled.clone()),
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
