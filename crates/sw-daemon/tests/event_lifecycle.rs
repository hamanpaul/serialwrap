//! P7 Task 16-wire（08-slice23-login-events.md）：Event Engine 隨 daemon 啟停、`BridgeQueries for SessionManager`。
//!
//! 偏離計畫原文：計畫的 `shutdown_closes_event_intake_before_drain` 以 trace 新增 `EngineCloseIntake`／`EngineStop` 斷言；
//! P5 `daemon_process::sigterm_graceful_exit_0_and_flushes_queue` 鎖定停機 trace 末 6 筆，本 Phase 不新增 trace 事件，
//! 停止序改以行為斷言：`shutdown_closes_event_intake_before_drain_behavior`（drain 期間 RX 不再被 matcher 受理）與
//! `shutdown_aborts_long_handler_within_budget`（與 drain 共用期限的終止時間）。完整停止序權威為 P8 Task 12。
#![cfg(feature = "test-fakes")]
mod support;

use std::path::{Path, PathBuf};
use std::thread;
use std::time::{Duration, Instant};

use support::*;
use sw_core::event_engine::BridgeQueries;
use sw_daemon::daemon_main::TraceEvent;
use sw_daemon::daemon_main::{run_with_hooks, DaemonHooks};
use sw_pal::traits::DeviceInfo;

fn device(by_id: &str, real_path: &str) -> (String, DeviceInfo) {
    (
        by_id.to_string(),
        DeviceInfo {
            by_id: by_id.to_string(),
            real_path: real_path.to_string(),
        },
    )
}

fn no_hooks() -> DaemonHooks {
    DaemonHooks {
        before_state_commit: None,
        after_state_commit: None,
        trace: None,
    }
}

fn events_log(env: &IsolatedEnv) -> PathBuf {
    PathBuf::from(&env.vars["SERIALWRAP_EVENTS_RUNTIME_DIR"]).join("events.ndjson")
}

fn write_rule(env: &IsolatedEnv, name: &str, handler: &str) {
    let dir = PathBuf::from(&env.vars["SERIALWRAP_EVENTS_DIR"]);
    std::fs::write(
        dir.join(format!("o.{name}.json")),
        format!(
            r#"{{"schema_version": 1, "owner": "o", "name": "{name}", "kind": "tool", "selectors": ["COM0"],
                "pattern": {{"kind": "contains", "value": "__READY__"}}, "handler": {handler}}}"#
        ),
    )
    .unwrap();
}

/// events log 的每筆事件（`loads` 失敗的行略過）。
fn events(path: &Path) -> Vec<sw_proto::pyjson::PyValue> {
    std::fs::read_to_string(path)
        .unwrap_or_default()
        .lines()
        .filter_map(|l| sw_proto::pyjson::loads(l).ok())
        .collect()
}

fn field<'a>(ev: &'a sw_proto::pyjson::PyValue, key: &str) -> &'a str {
    ev.get(key).and_then(|v| v.as_str()).unwrap_or("")
}

fn types(path: &Path) -> Vec<String> {
    std::fs::read_to_string(path)
        .unwrap_or_default()
        .lines()
        .filter_map(|l| {
            let v = sw_proto::pyjson::loads(l).ok()?;
            v.get("type").and_then(|t| t.as_str()).map(str::to_string)
        })
        .collect()
}

fn wait_for(what: &str, limit: Duration, f: impl FnMut() -> bool) {
    assert!(poll(limit, f), "等待逾時：{what}");
}

/// 輪詢至條件成立或逾時；client thread 用此結果決定斷言，但一律送出 `daemon.stop`（避免 daemon 永不結束）。
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

#[test]
fn engine_started_before_bootstrap_attach() {
    // Python `service.start()`：engine.start → add_rx_observer → scan／bootstrap（parity #121）；bootstrap attach 的
    // ready probe 回應（含 `__READY__<nonce>` 行）即被 engine 看到。
    let env = isolated_env();
    write_rule(&env, "boot", r#"{"exec": ["true"]}"#);
    let platform = in_process_platform(
        &env.vars,
        vec![device("usb-SER-A-if00", "/dev/pts/10")],
        Fixture::ReadyShellBlockingWithQueue,
    );
    let socket = env.socket.clone();
    let log = events_log(&env);
    let log2 = log.clone();
    let client = thread::spawn(move || {
        wait_socket(&socket, Duration::from_secs(10));
        let seen = poll(Duration::from_secs(10), || {
            types(&log2).iter().any(|t| t == "match_recorded")
        });
        rpc_once(&socket, STOP);
        seen
    });
    let code = run_with_hooks(vec!["serialwrapd".to_string()], platform, no_hooks());
    assert!(
        client.join().unwrap(),
        "bootstrap attach 的 RX 未被 engine 看到"
    );
    assert_eq!(code, 0);
    let t = types(&log);
    assert_eq!(&t[..2], ["rule_loaded", "com_enabled"], "{t:?}");
    assert!(t.contains(&"match_recorded".to_string()), "{t:?}");
}

/// SC-11 細化：handler 與 drain 共用 5 s 期限，逾時終止整個 process group 後 100 ms 寬限內返回。
///
/// 審查 01c474c_a0cad06（0722f75-2，Medium）：改為子行程執行以斷言 stderr——須有
/// `event dispatcher shutdown 逾時，終止 1 個 handler` WARNING，exit code 非 0 時附上 `shutdown incomplete:` 行。
/// 期限依計畫自 `engine.close_intake()` 起算（緊接 `WatcherStopped` trace 之後，drain 的 5 s 共用期限亦自此起算）：
/// `WatcherStopped` → `run_with_hooks` 返回 < 5.1 s；另以 `daemon.stop` 送出起算的總時間 < 5.6 s 作上限
/// （含 server stop 與 watcher stop）。handler 與行程樹檢查依平台（POSIX `sh`＋`/proc`；Windows `cmd.exe`＋
/// `Win32_Process`），Windows 原生核對見 P7-LW-3。
#[test]
fn shutdown_aborts_long_handler_within_budget() {
    if std::env::var("S21_CHILD").as_deref() == Ok("longhandler") {
        use std::sync::{Arc, Mutex};
        let vars = process_vars();
        let marker = long_handler_marker(&vars);
        let platform = in_process_platform(
            &vars,
            vec![device("usb-SER-A-if00", "/dev/pts/10")],
            Fixture::ReadyShellBlockingWithQueue,
        );
        let socket = PathBuf::from(&vars["SERIALWRAP_RUN_DIR"]).join("serialwrapd.sock");
        let stop_sent: Arc<Mutex<Option<Instant>>> = Arc::new(Mutex::new(None));
        let sent = stop_sent.clone();
        thread::spawn(move || {
            wait_socket(&socket, Duration::from_secs(10));
            if !poll(Duration::from_secs(10), || marker_alive(&marker)) {
                eprintln!("RESULT handler_not_started");
            }
            *sent.lock().unwrap() = Some(Instant::now());
            rpc_once(&socket, STOP);
        });
        let watcher_stopped: Arc<Mutex<Option<Instant>>> = Arc::new(Mutex::new(None));
        let ws = watcher_stopped.clone();
        let hooks = DaemonHooks {
            before_state_commit: None,
            after_state_commit: None,
            trace: Some(Box::new(move |ev| {
                if ev == TraceEvent::WatcherStopped {
                    *ws.lock().unwrap() = Some(Instant::now());
                }
            })),
        };
        let code = run_with_hooks(vec!["serialwrapd".to_string()], platform, hooks);
        let end = Instant::now();
        let ms = |t: Option<Instant>| t.map(|t| (end - t).as_millis()).unwrap_or(u128::MAX);
        eprintln!(
            "RESULT intake_to_exit_ms={} stop_to_exit_ms={}",
            ms(*watcher_stopped.lock().unwrap()),
            ms(*stop_sent.lock().unwrap())
        );
        std::process::exit(code);
    }
    let env = isolated_env();
    let marker = long_handler_marker(&env.vars);
    write_rule(&env, "long", &long_handler_json(&marker));
    let out = run_child_test("shutdown_aborts_long_handler_within_budget", "longhandler", &env);
    let incomplete: Vec<&str> = out
        .stderr
        .lines()
        .filter(|l| l.contains("shutdown incomplete:"))
        .collect();
    assert!(!out.stderr.contains("RESULT handler_not_started"), "handler 未啟動：{}", out.stderr);
    assert_eq!(out.code, Some(0), "aborted handler 已回收（unreaped 空）：{incomplete:?}\n{}", out.stderr);
    assert!(
        out.stderr
            .contains("event dispatcher shutdown 逾時，終止 1 個 handler"),
        "{}",
        out.stderr
    );
    let num = |key: &str| -> u128 {
        out.stderr
            .split_whitespace()
            .find_map(|w| w.strip_prefix(key))
            .and_then(|v| v.parse().ok())
            .unwrap_or_else(|| panic!("缺 {key}：{}", out.stderr))
    };
    let intake = num("intake_to_exit_ms=");
    let total = num("stop_to_exit_ms=");
    eprintln!("shutdown_aborts_long_handler_within_budget: intake→exit {intake} ms、stop→exit {total} ms");
    assert!(intake < 5100, "close_intake 起算 {intake} ms（計畫 5.1 s）");
    assert!(total < 5600, "daemon.stop 起算 {total} ms");
    wait_for(
        "handler 行程樹已不存在",
        Duration::from_secs(5),
        || !marker_alive(&marker),
    );
    assert!(types(&events_log(&env)).contains(&"fire_timeout".to_string()));
}

/// 審查 01c474c_a0cad06（0722f75-1，Medium）：計畫 R1-F2「修正前必 FAIL」的停止序回歸以行為斷言承接。
///
/// `engine.stop` 內部也會呼叫 `close_intake`，且 drain 一開始 reader 即停止（drain 期間不再有 RX 進 matcher），
/// 故可觀測差異只在「停機當下 matcher 佇列中的積壓」：`WatcherStopped`（緊接 `engine.close_intake()` 之前）的
/// hook 內先關閉 events log 的 append 閘門、注入 50 行 `__B2__` 並等它們全數落 WAL（reader observer 已送入
/// matcher 佇列），matcher 處理第一行時卡在寫 `match_recorded`；800 ms 後放閘。drain 因 BlockForever writer
/// 卡到 5 s 期限。daemon 在 drain **之前**關 intake → matcher 停止、積壓丟棄，`__B2__` 至多記 1 筆；
/// 若只在 drain 之後（`engine.stop`）才關，matcher 於 drain 期間處理完 50 行積壓。規則 `scope: any`：卡住的前景命令
/// 使 RX 歸類為 command_output（預設 `spontaneous` 不比對）。
/// 子行程執行（卡住的 writer thread 無法收回，同 `daemon_process::shutdown_with_stalled_drain_exits_1_with_diagnostic`）。
#[test]
fn shutdown_closes_event_intake_before_drain_behavior() {
    if std::env::var("S21_CHILD").as_deref() == Ok("intake") {
        use std::sync::atomic::{AtomicBool, Ordering};
        use std::sync::Arc;
        use sw_pal::fakes::{FakeSerialBackend, FakeWriteStep};
        let vars = process_vars();
        let mut platform = in_process_platform(
            &vars,
            vec![device("usb-SER-A-if00", "/dev/pts/10")],
            Fixture::ReadyShellBlockingWithQueue,
        );
        // 換上自備的 FakeSerial（保留裝置 handle 以注入 RX）；READY 後第一筆寫入（agent 命令）永久阻塞。
        let serial = FakeSerialBackend::new();
        let dev = serial.add_device("/dev/pts/10");
        // shell 假 DUT（同 support `install_shell_replies`；support 內同名私有函式遮蔽了 service_rig 的 pub 版本）。
        dev.on_tx_reply(b"\n", b"root@t:/# ");
        dev.on_tx_match(
            r"__READY__[0-9a-f]+",
            Box::new(|line: &[u8]| {
                let text = String::from_utf8_lossy(line).into_owned();
                let token = text.split("__READY__").nth(1).unwrap_or("").trim().to_string();
                format!("__READY__{token}\r\nroot@t:/# ").into_bytes()
            }),
        );
        let handle = dev.clone();
        let armed = AtomicBool::new(false);
        dev.on_tx_match(
            r"__READY__[0-9a-f]+",
            Box::new(move |_line: &[u8]| {
                if !armed.swap(true, Ordering::SeqCst) {
                    handle.script_write(vec![FakeWriteStep::BlockForever]);
                }
                Vec::new()
            }),
        );
        platform.serial = serial;
        let gate = Arc::new(AtomicBool::new(false));
        platform.fs = Arc::new(GatedLogFs {
            inner: platform.fs.clone(),
            closed: gate.clone(),
        });
        let socket = PathBuf::from(&vars["SERIALWRAP_RUN_DIR"]).join("serialwrapd.sock");
        let log = PathBuf::from(&vars["SERIALWRAP_EVENTS_RUNTIME_DIR"]).join("events.ndjson");
        let mirror = PathBuf::from(&vars["SERIALWRAP_WAL_DIR"]).join("raw.mirror.log");
        let driver_dev = dev.clone();
        thread::spawn(move || {
            wait_socket(&socket, Duration::from_secs(10));
            wait_for("COM0 READY", Duration::from_secs(10), || {
                get_state(&socket, "COM0")["state"] == "READY"
            });
            driver_dev.push_rx(b"__B1__\r\n");
            wait_for("對照行 __B1__ 被 matcher 受理", Duration::from_secs(10), || {
                events(&log).iter().any(|e| {
                    field(e, "type") == "match_recorded" && field(e, "matched_text") == "__B1__"
                })
            });
            let r = submit(&socket, "COM0", "stuck");
            assert!(r["ok"] == true, "BlockForever 的命令應被接受：{r:?}");
            thread::sleep(Duration::from_millis(300));
            rpc_once(&socket, STOP);
        });
        let hook_dev = dev.clone();
        let hooks = DaemonHooks {
            before_state_commit: None,
            after_state_commit: None,
            trace: Some(Box::new(move |ev| {
                if ev != TraceEvent::WatcherStopped {
                    return;
                }
                gate.store(true, Ordering::SeqCst);
                hook_dev.push_rx(&b"__B2__\r\n".repeat(50));
                wait_for("50 行 __B2__ 落 WAL", Duration::from_secs(5), || {
                    std::fs::read_to_string(&mirror)
                        .unwrap_or_default()
                        .matches("__B2__")
                        .count()
                        == 50
                });
                thread::sleep(Duration::from_millis(50));
                let g = gate.clone();
                thread::spawn(move || {
                    thread::sleep(Duration::from_millis(800));
                    g.store(false, Ordering::SeqCst);
                });
            })),
        };
        let code = run_with_hooks(vec!["serialwrapd".to_string()], platform, hooks);
        std::process::exit(code);
    }
    let env = isolated_env();
    std::fs::write(
        PathBuf::from(&env.vars["SERIALWRAP_EVENTS_DIR"]).join("o.b.json"),
        r#"{"schema_version": 1, "owner": "o", "name": "b", "kind": "tool", "selectors": ["COM0"],
            "scope": "any", "pattern": {"kind": "regex", "value": "__B[12]__"}, "handler": {"exec": ["true"]}}"#,
    )
    .unwrap();
    let out = run_child_test("shutdown_closes_event_intake_before_drain_behavior", "intake", &env);
    assert!(
        out.stderr.contains("drain stalled: t:COM0"),
        "drain 須卡到期限：{}",
        out.stderr
    );
    let evs = events(&events_log(&env));
    let b2 = evs
        .iter()
        .filter(|e| field(e, "type") == "match_recorded" && field(e, "matched_text") == "__B2__")
        .count();
    assert!(
        b2 <= 1,
        "close_intake 須在 drain 前：matcher 積壓不得於 drain 期間續處理（__B2__ match_recorded {b2} 筆）"
    );
}

/// events log（`events.ndjson`）的 append 可由閘門暫停的 FsOps；其餘一律委派原生實作。
struct GatedLogFs {
    inner: std::sync::Arc<dyn sw_pal::traits::FsOps>,
    closed: std::sync::Arc<std::sync::atomic::AtomicBool>,
}

impl sw_pal::traits::FsRead for GatedLogFs {
    fn read(&self, p: &Path) -> sw_pal::traits::PalResult<Vec<u8>> {
        self.inner.read(p)
    }
    fn exists(&self, p: &Path) -> bool {
        self.inner.exists(p)
    }
    fn is_dir(&self, p: &Path) -> bool {
        self.inner.is_dir(p)
    }
    fn is_file(&self, p: &Path) -> bool {
        self.inner.is_file(p)
    }
    fn list_dir(&self, p: &Path) -> sw_pal::traits::PalResult<Vec<String>> {
        self.inner.list_dir(p)
    }
}

impl sw_pal::traits::FsOps for GatedLogFs {
    fn atomic_write(&self, p: &Path, data: &[u8], mode: Option<u32>) -> sw_pal::traits::PalResult<()> {
        self.inner.atomic_write(p, data, mode)
    }
    fn write_file(&self, p: &Path, data: &[u8]) -> sw_pal::traits::PalResult<()> {
        self.inner.write_file(p, data)
    }
    fn mkdirs(&self, p: &Path) -> sw_pal::traits::PalResult<()> {
        self.inner.mkdirs(p)
    }
    fn replace(&self, from: &Path, to: &Path) -> sw_pal::traits::PalResult<()> {
        self.inner.replace(from, to)
    }
    fn remove_file(&self, p: &Path) -> sw_pal::traits::PalResult<()> {
        self.inner.remove_file(p)
    }
    fn copy_preserving(&self, from: &Path, to: &Path) -> sw_pal::traits::PalResult<()> {
        self.inner.copy_preserving(from, to)
    }
    fn sync_file(&self, p: &Path) -> sw_pal::traits::PalResult<()> {
        self.inner.sync_file(p)
    }
    fn copy_tree(&self, from: &Path, to: &Path) -> sw_pal::traits::PalResult<()> {
        self.inner.copy_tree(from, to)
    }
    fn remove_tree(&self, p: &Path) -> sw_pal::traits::PalResult<()> {
        self.inner.remove_tree(p)
    }
    fn read_link(&self, p: &Path) -> sw_pal::traits::PalResult<Option<PathBuf>> {
        self.inner.read_link(p)
    }
    fn set_mode(&self, p: &Path, mode: u32) -> sw_pal::traits::PalResult<()> {
        self.inner.set_mode(p, mode)
    }
    fn sync_dir(&self, p: &Path) -> sw_pal::traits::PalResult<()> {
        self.inner.sync_dir(p)
    }
    fn symlink_file(&self, link: &Path, target: &Path) -> sw_pal::traits::PalResult<()> {
        self.inner.symlink_file(link, target)
    }
    fn symlink_or_copy_dir(
        &self,
        link: &Path,
        target: &Path,
    ) -> sw_pal::traits::PalResult<sw_pal::traits::LinkKind> {
        self.inner.symlink_or_copy_dir(link, target)
    }
    fn file_lock_exclusive(
        &self,
        p: &Path,
        blocking: bool,
    ) -> sw_pal::traits::PalResult<sw_pal::traits::FileLockGuard> {
        self.inner.file_lock_exclusive(p, blocking)
    }
    fn open_append(&self, p: &Path) -> sw_pal::traits::PalResult<Box<dyn sw_pal::traits::AppendFile>> {
        if p.file_name().is_some_and(|n| n == "events.ndjson") {
            while self.closed.load(std::sync::atomic::Ordering::SeqCst) {
                thread::sleep(Duration::from_millis(5));
            }
        }
        self.inner.open_append(p)
    }
    fn create_truncate(&self, p: &Path, bytes: &[u8]) -> sw_pal::traits::PalResult<()> {
        self.inner.create_truncate(p, bytes)
    }
    fn create_truncate_text(&self, p: &Path, text: &str) -> sw_pal::traits::PalResult<()> {
        self.inner.create_truncate_text(p, text)
    }
    fn file_size(&self, p: &Path) -> sw_pal::traits::PalResult<Option<u64>> {
        self.inner.file_size(p)
    }
    fn remove_file_if_exists(&self, p: &Path) -> sw_pal::traits::PalResult<bool> {
        self.inner.remove_file_if_exists(p)
    }
}

/// 父子行程共用的 handler 識別字串（取隔離根目錄名，每個測試唯一）。
fn long_handler_marker(vars: &std::collections::BTreeMap<String, String>) -> String {
    let run = PathBuf::from(&vars["SERIALWRAP_RUN_DIR"]);
    let root = run.parent().and_then(|p| p.file_name()).map(|n| n.to_string_lossy().into_owned());
    format!("swp7life{}", root.unwrap_or_default()).replace(|c: char| !c.is_ascii_alphanumeric(), "")
}

/// 平台以路徑分隔字元判定（測試不得以 cfg 分辨平台，arch §2.1；同 P5 `state_store` 測試的寫法）。
fn on_windows() -> bool {
    std::path::MAIN_SEPARATOR == '\\'
}

/// 長時間 handler（`sleep 60`／`ping -n 60`），命令列帶 `marker` 以便辨識行程樹。
fn long_handler_json(marker: &str) -> String {
    if on_windows() {
        format!(r#"{{"shell": "ping -n 60 127.0.0.1 > nul & rem {marker}"}}"#)
    } else {
        format!(r#"{{"exec": ["/bin/sh", "-c", "sleep 60; : {marker}"]}}"#)
    }
}

/// 命令列含 `marker` 且非 zombie 的行程是否存在：POSIX 掃 `/proc`；Windows 以 PowerShell 查 `Win32_Process`
/// （Windows 原生核對列 P7-LW-3）。
fn marker_alive(marker: &str) -> bool {
    if on_windows() {
        let query = format!(
            "@(Get-CimInstance Win32_Process | Where-Object {{ $_.CommandLine -like '*{marker}*' -and $_.Name -ne 'powershell.exe' }}).Count"
        );
        return std::process::Command::new("powershell")
            .args(["-NoProfile", "-Command", &query])
            .output()
            .ok()
            .and_then(|o| String::from_utf8_lossy(&o.stdout).trim().parse::<u32>().ok())
            .is_some_and(|n| n > 0);
    }
    let Ok(dir) = std::fs::read_dir("/proc") else {
        return false;
    };
    dir.flatten().any(|e| {
        let cmd = std::fs::read(e.path().join("cmdline")).unwrap_or_default();
        let stat = std::fs::read_to_string(e.path().join("stat")).unwrap_or_default();
        let zombie = stat
            .rsplit_once(')')
            .is_some_and(|(_, rest)| rest.trim_start().starts_with('Z'));
        !zombie && String::from_utf8_lossy(&cmd).contains(marker)
    })
}

#[test]
fn bridge_queries_reflect_manager_state() {
    let rig = ServiceRig::two_shells();
    let q: &dyn BridgeQueries = &*rig.mgr;
    assert_eq!(q.known_coms(), ["COM0", "COM1"], "sorted(com)");
    assert_eq!(q.profile_for("COM0").as_deref(), Some("t"));
    assert_eq!(q.profile_for("COM9"), None);
    assert_eq!(q.active_cmd_id_for("COM0"), None);
    // 假 DUT 不回應此命令：前景等到 timeout_s（1 s）才結束。
    let sub = rig.call(
        "command.submit",
        r#"{"selector":"COM0","cmd":"sleep 9","timeout_s":1}"#,
    );
    assert_eq!(
        sub.get("ok"),
        Some(&sw_proto::pyjson::PyValue::Bool(true)),
        "{sub:?}"
    );
    wait_until("COM0 前景中", || {
        q.active_cmd_id_for("COM0").as_deref() == Some("foreground")
    });
    assert_eq!(q.active_cmd_id_for("COM1"), None, "閒置 com");
    assert_eq!(q.active_cmd_id_for("COM9"), None, "不存在的 com");
    // rig 為 FakeClock：推進虛擬時間讓 1 s 逾時到期。
    wait_until("前景結束", || {
        rig.clock.advance(Duration::from_millis(200));
        rig.mgr.on_tick();
        q.active_cmd_id_for("COM0").is_none()
    });
}
