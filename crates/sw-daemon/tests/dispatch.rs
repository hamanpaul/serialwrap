//! P5-T18：RPC 平面分派、參數轉型與 `health.status`（plan 06 Task 18 Step 1；parity-amendments-2026-10-26 #11／#69／#100）。
//!
//! 斷言值以 Python 基線（`sw_core/service.py` `_dispatch`／`health`）為準；回應以 `RPC_WIRE` 序列化後逐位元組比對。
#![cfg(feature = "test-fakes")]
mod support;

use std::sync::atomic::{AtomicUsize, Ordering};
use std::sync::{Arc, Condvar, Mutex};
use std::time::{Duration, Instant};

use support::{BY_ID_A, BY_ID_B, REAL_A, REAL_B, RIG_PID, RigSpec, ServiceRig, wait_until};
use sw_core::state_store::{LoadedState, ReleasedRow};
use sw_pal::fakes::{FakeDevice, FakeEnv, FakeSerialBackend};
use sw_pal::proc_scan::{DaemonProc, ProcIdentity};
use sw_pal::traits::{
    Clock, DeviceInfo, DevicePath, HolderReport, HolderScanStatus, OpenError, PalError,
    PalErrorKind, SerialBackend, SerialConfig, SerialHandle, UartExclusive,
};
use sw_proto::pycompat::iso_utc_now;
use sw_proto::pyjson::{PyDict, PyValue, RPC_WIRE, dumps};
use sw_core::custody::CustodyPhaseSnapshot;

/// `foreign_holders` 會讀取行程內共用的 holder_unknown 登錄（#11）；登錄／精確比對 foreign_holders 的測試彼此序列化。
static HOLDER_REGISTRY: Mutex<()> = Mutex::new(());

/// P8-ea9e03d-1：正式 bind 已入列但等滿 8 秒逾時，晚到完成仍須發布綁定世代。
fn i25_bind_completes_after_rpc_timeout() -> (ServiceRig, FakeDevice) {
    let r = ServiceRig::ready_shell();
    let dev = r.device(REAL_A).clone();
    assert_eq!(
        r.call("device.release", r#"{"selector":"COM0"}"#).get("ok"),
        Some(&PyValue::Bool(true))
    );
    // 共用 fixture 的 newline trigger 只回一次；本案每次重新開埠都要能看到 prompt。
    dev.on_tx_match(r"^$", Box::new(|_| b"root@t:/# ".to_vec()));
    let (entered, resume) = r.mgr.pause_next_bind_for_test("COM0").unwrap();
    let r = Arc::new(r);
    let binding_r = r.clone();
    let binding = std::thread::spawn(move || {
        binding_r.call_err(
            "session.bind",
            &format!(r#"{{"selector":"COM0","device_by_id":"{BY_ID_A}"}}"#),
        )
    });
    entered.recv_timeout(Duration::from_secs(5)).unwrap();
    let error = binding.join().unwrap();
    resume.send(()).unwrap();
    assert_eq!(error.message, "internal timeout");
    // 同 lane 查詢作為屏障，確定晚到 bind 已實際完成；不以固定 sleep 猜測。
    let late = r.session("COM0").unwrap();
    assert_eq!(s(&late, "state"), "DETACHED");
    assert_eq!(s(&late, "device_by_id"), BY_ID_A);
    assert_eq!(dev.open_attempts(), 1, "逾時的 RPC 不得自行重新開埠");
    (
        Arc::try_unwrap(r).unwrap_or_else(|_| panic!("bind 執行緒須已結束")),
        dev,
    )
}

#[test]
fn wp510r_i25_rpc_timed_out_bind_late_completion_allows_retry() {
    let (r, dev) = i25_bind_completes_after_rpc_timeout();
    let retry = r.call(
        "session.bind",
        &format!(r#"{{"selector":"COM0","device_by_id":"{BY_ID_A}"}}"#),
    );
    assert_eq!(retry.get("ok"), Some(&PyValue::Bool(true)));
    assert_eq!(
        jv(sub(&retry, "session").get("state").unwrap()),
        jv(&PyValue::from("ATTACHING")),
        "有效通知不得被世代失配拒絕：{}",
        j(&retry)
    );
    r.wait_state("COM0", "READY");
    assert_eq!(dev.open_attempts(), 2);
    let test = r.call("session.self_test", r#"{"selector":"COM0"}"#);
    assert_ne!(s(&test, "classification"), "DEVICE_REBOUND_REQUIRED");
    assert_eq!(s(&test, "current_real_path"), REAL_A);
    assert_eq!(s(&test, "attached_real_path"), REAL_A);
}

#[test]
fn wp510r_i25_rpc_timed_out_bind_late_completion_allows_replug() {
    let (r, dev) = i25_bind_completes_after_rpc_timeout();
    // 審查反例的原步驟：逾時晚到完成 → 再次成功 bind → 拔插仍須能重新開埠。
    let retry = r.call(
        "session.bind",
        &format!(r#"{{"selector":"COM0","device_by_id":"{BY_ID_A}"}}"#),
    );
    assert_eq!(retry.get("ok"), Some(&PyValue::Bool(true)));
    assert_eq!(
        s(&sub(&retry, "session"), "state"),
        "ATTACHING",
        "{}",
        j(&retry)
    );
    r.wait_state("COM0", "READY");
    r.mgr.update_devices(&[]);
    r.wait_state("COM0", "DETACHED");
    let before_replug = dev.open_attempts();
    r.mgr.update_devices(&[(
        BY_ID_A.into(),
        DeviceInfo {
            by_id: BY_ID_A.into(),
            real_path: REAL_A.into(),
        },
    )]);
    r.wait_state("COM0", "READY");
    assert_eq!(before_replug, 2);
    assert_eq!(
        dev.open_attempts(),
        3,
        "晚到完成後 watcher 的有效通知仍須開埠"
    );
    let test = r.call("session.self_test", r#"{"selector":"COM0"}"#);
    assert_eq!(s(&test, "current_real_path"), REAL_A);
    assert_eq!(s(&test, "attached_real_path"), REAL_A);
    assert!(
        !r.mgr
            .delivery_obligation_for_test(&r.mgr.resolve("COM0").unwrap())
    );
}

/// 第 25 項 Medium：bind 通知逾時後才登記舊義務，裝置已改指並完成重新連線。
/// Python self_test 讀目前 _devices（origin/main:sw_core/session_manager.py:7658-7685）。
#[test]
fn wp510r_i25_rpc_late_delivery_registration_cannot_revert_current_path() {
    let serial = FakeSerialBackend::new();
    let old = serial.add_device(REAL_A);
    let new = serial.add_device("/fake/i25-new");
    support::install_shell_replies(&old);
    support::install_shell_replies(&new);
    let r = Arc::new(ServiceRig::build_with_serial_backend(RigSpec {
        yaml: format!("profiles:\n  t:\n    platform: shell\n    prompt_regex: '(?m)^root@t:.*# '\n    timeout_s: 2.0\ntargets:\n  - {{com: COM0, profile: t, device_by_id: {BY_ID_A}}}\n"),
        devices: vec![(BY_ID_A.into(), REAL_A.into())],
        explicit_target_count: 1, allowlist: None, wait_ready: vec!["COM0".into()],
    }, Some(serial)));
    let sid = r.mgr.resolve("COM0").unwrap();
    assert_eq!(r.call("device.release", r#"{"selector":"COM0"}"#).get("ok"), Some(&PyValue::Bool(true)));
    let (held_tx, held_rx) = crossbeam_channel::bounded(1);
    let manager = r.mgr.clone();
    let hold_sid = sid.clone();
    r.mgr.set_bind_pre_device_online_hook_for_test(Box::new(move || {
        let (entered, resume) = manager.pause_next_request_for_test(&hold_sid).unwrap();
        let query_mgr = manager.clone();
        let query_sid = hold_sid.clone();
        let query = std::thread::spawn(move || { let _ = query_mgr.session_queue_lens(&query_sid); });
        entered.recv_timeout(Duration::from_secs(5)).unwrap();
        let control = manager.actor_control_for_test(&hold_sid).unwrap();
        let mut filled = 0;
        while control.try_send(sw_core::mailbox::ControlEvent::ConsoleCountChanged { count: 0 }).is_ok() { filled += 1; }
        assert!(filled > 0);
        held_tx.send((resume, query)).unwrap();
    }));
    let (entered_tx, entered_rx) = crossbeam_channel::bounded(1);
    let (resume_tx, resume_rx) = crossbeam_channel::bounded(1);
    r.mgr.set_bind_delivery_pre_register_hook_for_test(Box::new(move || {
        entered_tx.send(()).unwrap();
        resume_rx.recv_timeout(Duration::from_secs(15)).unwrap();
    }));
    let bound_r = r.clone();
    let bound = std::thread::spawn(move || bound_r.call("session.bind", &format!(r#"{{"selector":"COM0","device_by_id":"{BY_ID_A}"}}"#)));
    let (actor_resume, query) = held_rx.recv_timeout(Duration::from_secs(10)).unwrap();
    entered_rx.recv_timeout(Duration::from_secs(10)).unwrap();
    actor_resume.send(()).unwrap();
    query.join().unwrap();
    r.mgr.update_devices(&[(BY_ID_A.into(), DeviceInfo { by_id: BY_ID_A.into(), real_path: "/fake/i25-new".into() })]);
    r.wait_state("COM0", "READY");
    let before = r.call("session.self_test", r#"{"selector":"COM0"}"#);
    resume_tx.send(()).unwrap();
    let bind = bound.join().unwrap();
    wait_until("過期義務已被丟棄", || !r.mgr.delivery_obligation_for_test(&sid));
    // 同 control lane 的查詢屏障，確定已入列的舊通知也處理完畢。
    let _ = r.session("COM0").unwrap();
    let after = r.call("session.self_test", r#"{"selector":"COM0"}"#);
    assert_eq!(bind.get("ok"), Some(&PyValue::Bool(true)));
    assert_eq!(s(&before, "current_real_path"), "/fake/i25-new");
    assert_ne!(s(&after, "classification"), "DEVICE_REBOUND_REQUIRED", "過期通知不得污染診斷：{}", j(&after));
    assert_eq!(jv(after.get("current_real_path").unwrap()), jv(&PyValue::from("/fake/i25-new")));
    assert_eq!(s(&after, "attached_real_path"), "/fake/i25-new");
    assert_eq!(r.mgr.delivery_delivered_for_test(&sid), 0, "過期義務必須丟棄，不能送達");
    assert_eq!(new.open_attempts(), 1, "只開啟目前路徑一次");
}

fn holder_lock() -> std::sync::MutexGuard<'static, ()> {
    HOLDER_REGISTRY.lock().unwrap_or_else(|e| e.into_inner())
}

fn j(d: &PyDict) -> String {
    dumps(&PyValue::Dict(d.clone()), &RPC_WIRE)
}

fn jv(v: &PyValue) -> String {
    dumps(v, &RPC_WIRE)
}

fn keys(d: &PyDict) -> Vec<&str> {
    d.0.keys().map(String::as_str).collect()
}

fn s<'a>(d: &'a PyDict, k: &str) -> &'a str {
    d.get(k)
        .and_then(PyValue::as_str)
        .unwrap_or_else(|| panic!("缺字串欄位 {k}：{}", j(d)))
}

fn sub(d: &PyDict, k: &str) -> PyDict {
    match d.get(k) {
        Some(PyValue::Dict(x)) => x.clone(),
        other => panic!("{k} 應為 object：{other:?}"),
    }
}

fn daemon(pid: u32, socket: &str) -> DaemonProc {
    DaemonProc {
        pid,
        socket: Some(socket.to_string()),
        tree: vec![ProcIdentity {
            pid,
            start_ticks: None,
        }],
    }
}

struct Wp59HolderProbe {
    report: HolderReport,
    calls: Arc<AtomicUsize>,
}

impl UartExclusive for Wp59HolderProbe {
    fn probe_holders(&self, _real_path: &str) -> HolderReport {
        self.calls.fetch_add(1, Ordering::SeqCst);
        self.report.clone()
    }
}

fn wp59_posix_rig(path: &str, status: HolderScanStatus) -> (ServiceRig, Arc<AtomicUsize>) {
    let calls = Arc::new(AtomicUsize::new(0));
    let holder_probe: Arc<dyn UartExclusive> = Arc::new(Wp59HolderProbe {
        report: HolderReport {
            pids: Vec::new(),
            holder_unknown: status != HolderScanStatus::PartialOtherUid,
            status,
        },
        calls: Arc::clone(&calls),
    });
    let serial = sw_pal::fakes::proc::serial_backend_with_uart_exclusive(holder_probe)
        .expect("POSIX 測試替身應建立原生序列埠後端");
    let spec = RigSpec {
        yaml: "profiles:\n  t:\n    platform: passthrough\ntargets:\n  - {com: COM0, profile: t, device_by_id: /s/by-id/wp59}\n".to_string(),
        devices: vec![("/s/by-id/wp59".to_string(), path.to_string())],
        explicit_target_count: 1,
        allowlist: None,
        wait_ready: Vec::new(),
    };
    (ServiceRig::build_with_serial_backend(spec, Some(serial)), calls)
}

fn wp59_configure_health_scan(rig: &ServiceRig) {
    rig.procs
        .script(vec![vec![daemon(RIG_PID, "/run/wp59.sock")]]);
    rig.procs
        .set_multi_open_holders(vec![RIG_PID], HolderScanStatus::Permission);
}

/// 送出一個會卡住的前景命令（假 DUT 不回 prompt），回傳 cmd_id；等到 running。
fn submit_blocking(r: &ServiceRig) -> String {
    let resp = r.call(
        "command.submit",
        r#"{"selector":"COM0","cmd":"sleep 100","timeout_s":30}"#,
    );
    assert_eq!(resp.get("ok"), Some(&PyValue::Bool(true)), "{}", j(&resp));
    let id = s(&resp, "cmd_id").to_string();
    wait_until("前景命令 running", || {
        let g = r.call("command.get", &format!(r#"{{"cmd_id":"{id}"}}"#));
        sub(&g, "command").get("status") == Some(&PyValue::from("running"))
    });
    id
}

/// S2-GP-gpb：經正式 `command.submit` RPC 接線到 UART 與 WAL；WAL TX 完整落盤後才取副作用基準。
#[test]
fn gpb_command_submit_reaches_uart_and_wal() {
    let r = ServiceRig::ready_shell();
    r.settle();
    let dev = r.device(REAL_A);
    let line = b"sleep 100\n";
    let tx_mark = dev.tx_len();
    let submitted = r.call(
        "command.submit",
        r#"{"selector":"COM0","cmd":"sleep 100","timeout_s":30}"#,
    );
    let cmd_id = s(&submitted, "cmd_id").to_string();
    let session_id = r.mgr.resolve("COM0").expect("COM0 session");
    assert_eq!(
        j(&submitted),
        format!(
            "{{\"ok\":true,\"cmd_id\":\"{cmd_id}\",\"status\":\"accepted\",\"session_id\":\"{session_id}\",\"source_class\":\"agent\",\"version\":\"{}\"}}",
            r.version
        ),
        "command.submit 完整 RPC wire 回應"
    );

    wait_until("命令經 command.get 進入 running", || {
        let get = r.call("command.get", &format!(r#"{{"cmd_id":"{cmd_id}"}}"#));
        sub(&get, "command").get("status") == Some(&PyValue::from("running"))
    });
    let running = r.call("command.get", &format!(r#"{{"cmd_id":"{cmd_id}"}}"#));
    assert_eq!(
        s(&sub(&running, "command"), "status"),
        "running",
        "{}",
        j(&running)
    );

    wait_until("相同 cmd_id 的完整 WAL TX", || {
        wp55_wal_tx(&r, &cmd_id) == vec![("agent".to_string(), wp55_b64(line))]
    });

    // 先等到 WAL TX 已含完整命令，再固定副作用基準供下方 RPC 狀態查詢前後比較。
    let observed_uart = dev.tx_since(tx_mark);
    let wal_rows = wp53_wal_rows(&r, &cmd_id);
    let side_effect_baseline = (dev.tx_len(), r.wal.current_seq());
    let state_reply = r.call("session.get_state", r#"{"selector":"COM0"}"#);
    let state = sub(&state_reply, "session");

    assert_eq!(state.get("session_id"), Some(&PyValue::from(session_id)));
    assert_eq!(
        state.get("foreground_busy"),
        Some(&PyValue::Bool(true)),
        "{}",
        j(&state)
    );
    assert_eq!(observed_uart, line, "UART bytes 必須恰為完整命令列");
    assert_eq!(
        wal_rows.len(),
        1,
        "本 cmd_id 須恰有一筆完整 TX：{wal_rows:?}"
    );
    assert_eq!(s(&wal_rows[0], "dir"), "TX");
    assert_eq!(s(&wal_rows[0], "source"), "agent");
    assert_eq!(s(&wal_rows[0], "payload_b64"), wp55_b64(line));
    assert_eq!(
        wal_rows[0].get("len").and_then(PyValue::as_i64),
        Some(line.len() as i64),
        "WAL 長度必須涵蓋完整 UART 命令"
    );
    assert_eq!(
        (dev.tx_len(), r.wal.current_seq()),
        side_effect_baseline,
        "讀取 RPC session 狀態不得額外送 UART 或寫 WAL"
    );
}

// ---------------------------------------------------------------------------
// Step 1 計畫列
// ---------------------------------------------------------------------------

#[test]
fn deferred_methods_return_method_not_found_without_side_effects() {
    let r = ServiceRig::ready_shell();
    let scope = std::fs::read_to_string(support::data_root().join("tests/acceptance/slice21_scope.yaml")).unwrap();
    let methods = support::deferred_methods(&scope, &["2.1", sw_core::SLICE]); // 增補 #24：已交付切片之後的延後 method
    // P7←P6 合併 D4：console 已交付、event 已移入 served；P8 Task 6：file.push／file.pull 已移入 served，
    // 已無延後 method（下方迴圈保留，日後新增延後列時仍檢查 METHOD_NOT_FOUND 與零副作用）。
    assert!(methods.is_empty(), "{methods:?}");
    for m in methods {
        let before = r.side_effect_fingerprint();
        let resp = r.call(
            &m,
            "{\"selector\":\"COM0\",\"local_path\":\"/tmp/x\",\"remote_path\":\"/tmp/y\"}",
        );
        // 計畫原文對 dumps 結果加 `\n`；`pyjson::dumps` 不附換行（換行由 framing 補），此處比對不含換行的全文。
        assert_eq!(
            j(&resp),
            format!(
                "{{\"ok\":false,\"error_code\":\"METHOD_NOT_FOUND\",\"method\":\"{m}\",\"version\":\"{}\"}}",
                r.version
            )
        );
        assert_eq!(
            r.side_effect_fingerprint(),
            before,
            "{m} must not touch WAL/state/UART"
        );
    }
}

#[test]
fn health_status_key_order_and_limits() {
    let r = ServiceRig::ready_shell();
    let h = r.call("health.status", "{}");
    assert_eq!(
        keys(&h),
        [
            "ok",
            "pid",
            "version",
            "code_fingerprint",
            "code_path",
            "running",
            "started_at",
            "sessions",
            "devices",
            "commands",
            "wal_path",
            "mirror_path",
            "wal",
            "limits",
            "multi_open",
            "foreign_holders",
            "multi_open_detail"
        ]
    ); // warnings 只在非空時出現（另測）
    assert_eq!(
        jv(h.get("limits").unwrap()),
        "{\"max_submit_cmd_bytes\":16384,\"warn_submit_cmd_bytes\":4096,\"reject_error_code\":\"CMD_TOO_LONG\",\"newline_error_code\":\"CMD_CONTAINS_NEWLINE\",\"warning_code\":\"CMD_LENGTH_WARNING\",\"newline_forbidden\":true}"
    );
    assert_eq!(h.get("pid"), Some(&PyValue::from(i64::from(RIG_PID))));
    assert_eq!(h.get("running"), Some(&PyValue::Bool(true)));
    assert_eq!(h.get("sessions"), Some(&PyValue::from(1i64)));
    assert_eq!(h.get("devices"), Some(&PyValue::from(1i64)));
    assert_eq!(s(&h, "wal_path"), "/fake/wal/raw.wal.ndjson");
    assert_eq!(s(&h, "mirror_path"), "/fake/wal/raw.mirror.log");
    let wal = sub(&h, "wal");
    assert_eq!(
        keys(&wal),
        [
            "wal_dir",
            "wal_path",
            "wal_dir_exists",
            "wal_file_exists",
            "wal_dir_writable",
            "current_seq",
            "write_failures",
            "last_write_error",
            "recreated_count",
            "rotate_retry",
            "rotate_deferred",
            "deferred_delete_pending",
            "healthy",
            "unhealthy_reason"
        ]
    );
}

#[test]
fn health_ping_pong() {
    let r = ServiceRig::ready_shell();
    assert_eq!(
        j(&r.call("health.ping", "{}")),
        format!(
            "{{\"ok\":true,\"pong\":true,\"version\":\"{}\"}}",
            r.version
        )
    );
}

#[test]
fn submit_priority_zero_becomes_ten() {
    let r = ServiceRig::ready_shell();
    let resp = r.call(
        "command.submit",
        r#"{"selector":"COM0","cmd":"ls","priority":0}"#,
    );
    assert_eq!(resp.get("ok"), Some(&PyValue::Bool(true)), "{}", j(&resp));
    let rec = r.registry.get(s(&resp, "cmd_id")).expect("命令紀錄");
    assert_eq!(
        rec.get("priority"),
        Some(&PyValue::from(10i64)),
        "int(0 or 10)"
    );
}

#[test]
fn wp52_default_expect_reboot_flows_through_rpc_record_and_get() {
    let r = ServiceRig::ready_shell();
    let response = r.call("command.submit", r#"{"selector":"COM0","cmd":"echo wp52"}"#);
    assert_eq!(
        response.get("ok"),
        Some(&PyValue::Bool(true)),
        "{}",
        j(&response)
    );
    assert!(
        response.get("expect_reboot").is_none(),
        "false 不應回聲旗標"
    );

    let cmd_id = s(&response, "cmd_id").to_string();
    let command = sub(
        &r.call("command.get", &format!(r#"{{"cmd_id":"{cmd_id}"}}"#)),
        "command",
    );
    assert_eq!(
        command.get("expect_reboot"),
        Some(&PyValue::Bool(false)),
        "RPC submit 建立的 command record 必須保留 false"
    );
    assert!(command.get("trusted_human").is_none());
}

#[derive(Debug, PartialEq)]
struct Wp52RpcSnapshot {
    uart: Vec<(String, Vec<u8>, usize)>,
    registry_len: usize,
    pending: usize,
    queue_lens: Vec<(&'static str, Option<(i64, i64)>)>,
    effects: (u64, Vec<u8>, usize),
}

fn wp52_rpc_snapshot(r: &ServiceRig) -> Wp52RpcSnapshot {
    let session_id = r.mgr.resolve("COM0").expect("COM0 session");
    Wp52RpcSnapshot {
        uart: r
            .devices
            .iter()
            .map(|(path, dev)| (path.clone(), dev.tx_since(0), dev.write_calls()))
            .collect(),
        registry_len: r.registry.len(),
        pending: r.registry.pending(&session_id),
        queue_lens: ["COM0", "WP52_UNKNOWN"]
            .into_iter()
            .map(|selector| (selector, r.mgr.session_queue_lens(selector)))
            .collect(),
        effects: r.side_effect_fingerprint(),
    }
}

fn wp52_rpc_error(r: &ServiceRig, code: &str) -> PyDict {
    sw_proto::pydict! {
        "ok" => false,
        "error_code" => code,
        "version" => r.version.clone(),
    }
}

fn assert_wp52_rpc_rejected_without_side_effects(r: &ServiceRig, params: &str, expected: &str) {
    r.settle();
    let before = wp52_rpc_snapshot(r);
    let response = r.call("command.submit", params);
    assert_eq!(j(&response), expected, "request={params}");
    r.settle();
    assert_eq!(wp52_rpc_snapshot(r), before, "request={params}");
}

#[test]
fn wp52_rpc_expect_reboot_invalid_type_zero_tx() {
    let r = ServiceRig::ready_shell();
    let expected = j(&wp52_rpc_error(&r, "INVALID_EXPECT_REBOOT"));
    for (raw_value, selector) in [
        (r#""false""#, Some("COM0")),
        ("1", Some("WP52_UNKNOWN")),
        (r#""false""#, None),
        ("1", Some("COM0")),
        (r#""false""#, Some("WP52_UNKNOWN")),
        ("1", None),
    ] {
        let selector = selector
            .map(|s| format!(",\"selector\":\"{s}\""))
            .unwrap_or_default();
        let params =
            format!(r#"{{"cmd":"wp52 type check"{selector},"expect_reboot":{raw_value}}}"#);
        assert_wp52_rpc_rejected_without_side_effects(&r, &params, &expected);
    }
}

#[test]
fn wp52_rpc_expect_reboot_interactive_rejected() {
    let r = ServiceRig::ready_shell();
    let expected = j(&sw_proto::pydict! {
        "ok" => false,
        "error_code" => "EXPECT_REBOOT_MODE_UNSUPPORTED",
        "hint" => "--expect-reboot 只適用 line／background 模式；interactive 是 lease 按鍵通道，不走 reboot 路徑。",
        "version" => r.version.clone(),
    });
    assert_wp52_rpc_rejected_without_side_effects(
        &r,
        r#"{"selector":"WP52_UNKNOWN","cmd":"","mode":"interactive","expect_reboot":true}"#,
        &expected,
    );
}

#[test]
fn wp52_rpc_expect_reboot_validation_order() {
    let r = ServiceRig::ready_shell();
    let invalid_completion = j(&sw_proto::pydict! {
        "ok" => false,
        "error_code" => "INVALID_COMPLETION_REGEX",
        "version" => r.version.clone(),
    });
    let reserved_source = wp51_source_reserved_response(&r, "system:recover");
    let invalid_expect_reboot = j(&wp52_rpc_error(&r, "INVALID_EXPECT_REBOOT"));
    let not_found = j(&sw_proto::pydict! {
        "ok" => false,
        "error_code" => "SESSION_NOT_FOUND",
        "selector" => "WP52_UNKNOWN",
        "version" => r.version.clone(),
    });

    assert_wp52_rpc_rejected_without_side_effects(
        &r,
        r#"{"selector":"WP52_UNKNOWN","cmd":"x","completion_regex":"[","source":"system:recover","expect_reboot":"false"}"#,
        &invalid_completion,
    );
    assert_wp52_rpc_rejected_without_side_effects(
        &r,
        r#"{"selector":"WP52_UNKNOWN","cmd":"x","completion_regex":"x+","source":"system:recover","expect_reboot":"false"}"#,
        &reserved_source,
    );
    assert_wp52_rpc_rejected_without_side_effects(
        &r,
        r#"{"selector":"WP52_UNKNOWN","cmd":"x","completion_regex":"x+","expect_reboot":"false"}"#,
        &invalid_expect_reboot,
    );
    assert_wp52_rpc_rejected_without_side_effects(
        &r,
        r#"{"selector":"WP52_UNKNOWN","cmd":"x","completion_regex":"x+","expect_reboot":false}"#,
        &not_found,
    );
    assert_wp52_rpc_rejected_without_side_effects(
        &r,
        r#"{"selector":"WP52_UNKNOWN","cmd":"x","timeout_s":"invalid","expect_reboot":1}"#,
        &invalid_expect_reboot,
    );
}

#[test]
fn wp52_rpc_expect_reboot_false_interactive_allowed() {
    for raw_value in [Some("false"), Some("null"), None] {
        let r = ServiceRig::ready_shell();
        let flag = raw_value
            .map(|value| format!(",\"expect_reboot\":{value}"))
            .unwrap_or_default();
        let params = format!(r#"{{"selector":"COM0","cmd":"","mode":"interactive"{flag}}}"#);
        let response = r.call("command.submit", &params);
        let cmd_id = s(&response, "cmd_id").to_string();
        assert_eq!(
            j(&response),
            wp51_submit_success_response(&r, &cmd_id, None, None, "agent"),
            "interactive falsy/default flag must be admitted: {params}"
        );
        assert!(response.get("expect_reboot").is_none());
        wait_until("interactive 空命令建立 lease", || {
            r.registry
                .get(&cmd_id)
                .and_then(|record| record.get("interactive_session_id").cloned())
                .is_some_and(|value| !value.is_null())
        });
        let command = sub(
            &r.call("command.get", &format!(r#"{{"cmd_id":"{cmd_id}"}}"#)),
            "command",
        );
        assert!(
            command
                .get("interactive_session_id")
                .is_some_and(|value| !value.is_null()),
            "interactive lease ID 未寫入 command record：{}",
            j(&command)
        );
    }
}

// ---------------------------------------------------------------------------
// WP5-2 第 5 項：`command.submit` → 佇列 → OpRunner → registry → `command.get` 的旗標接線
// ---------------------------------------------------------------------------

/// Python `tests/test_expect_reboot.py` 的 `CHAINED`（含 reboot 段的鏈式命令）。
const WP52_CHAINED: &str = "firstboot -y && reboot";

const WP52_MODES: [(&str, &str); 4] = [
    ("line", "line"),
    ("fg", "line"),
    ("background", "background"),
    ("bg", "background"),
];

// ---------------------------------------------------------------------------
// WP5-4：未帶旗標的串接 reboot 提示與 command.submit 接線
// ---------------------------------------------------------------------------

const WP54_CHAINED_WARNING_HINT: &str = "命令在 &&／||／;／& 之後含 reboot，但未帶 --expect-reboot：broker 只認單獨一條的 reboot，這條會被當一般命令，不會立即 arm 開機靜默窗；shell 不回 prompt 的板子會逾時並送 Ctrl-C／Ctrl-D，shell 立刻重印 prompt 的板子則後續命令可能落進正在 shutdown 的系統。含重開機的串接命令請改用 `cmd submit --expect-reboot`。命令仍照常送出，本提示不改變執行。";

fn wp54_warning(segment: &str) -> PyDict {
    sw_proto::pydict! {
        "code" => "CHAINED_REBOOT_WARNING",
        "segment" => segment,
        "hint" => WP54_CHAINED_WARNING_HINT,
    }
}

#[test]
fn wp54_chained_warning_is_nonfatal_and_reaches_uart() {
    let r = ServiceRig::ready_shell();
    let command = WP52_CHAINED;
    let dev = r.device(REAL_A);
    dev.on_tx_reply(
        format!("{command}\n").as_bytes(),
        b"\r\nwp54-out\r\nroot@t:/# ",
    );
    r.settle();
    let tx_mark = dev.tx_len();

    let response = r.call(
        "command.submit",
        &format!(r#"{{"selector":"COM0","cmd":"{command}"}}"#),
    );
    assert_eq!(
        response.get("ok"),
        Some(&PyValue::Bool(true)),
        "{}",
        j(&response)
    );
    let cmd_id = s(&response, "cmd_id").to_string();
    let warning = wp54_warning("reboot");
    assert_eq!(
        response.get("warning"),
        Some(&PyValue::Dict(warning.clone()))
    );
    assert_eq!(
        response.get("warnings"),
        Some(&PyValue::List(vec![PyValue::Dict(warning.clone())]))
    );

    let mut expected = sw_proto::pydict! {
        "ok" => true,
        "cmd_id" => cmd_id.clone(),
        "status" => "accepted",
        "session_id" => r.mgr.resolve("COM0").expect("COM0 session"),
    };
    expected.insert(
        "warnings",
        PyValue::List(vec![PyValue::Dict(warning.clone())]),
    );
    expected.insert("warning", PyValue::Dict(warning));
    expected.insert("source_class", PyValue::from("agent"));
    expected.insert("version", PyValue::from(r.version.clone()));
    assert_eq!(
        keys(&response),
        [
            "ok",
            "cmd_id",
            "status",
            "session_id",
            "warnings",
            "warning",
            "source_class",
            "version"
        ]
    );
    assert_eq!(j(&response), j(&expected), "command.submit wire bytes");

    wait_until("串接命令照常完成", || {
        let record = sub(
            &r.call("command.get", &format!(r#"{{"cmd_id":"{cmd_id}"}}"#)),
            "command",
        );
        record.get("status") == Some(&PyValue::from("done"))
    });
    assert_eq!(
        dev.tx_since(tx_mark),
        format!("{command}\n").as_bytes(),
        "警告不可阻止命令實際送上 UART"
    );
}

#[test]
fn wp54_chained_warning_keeps_length_warning_first() {
    let r = ServiceRig::ready_shell();
    let command = format!("{WP52_CHAINED} # {}", "x".repeat(5000));
    let dev = r.device(REAL_A);
    dev.on_tx_reply(
        format!("{command}\n").as_bytes(),
        b"\r\nwp54-long-out\r\nroot@t:/# ",
    );
    r.settle();
    let response = r.call(
        "command.submit",
        &format!(r#"{{"selector":"COM0","cmd":"{command}"}}"#),
    );
    assert_eq!(
        response.get("ok"),
        Some(&PyValue::Bool(true)),
        "{}",
        j(&response)
    );

    let length_warning = sw_proto::pydict! {
        "code" => "CMD_LENGTH_WARNING",
        "cmd_length" => command.len() as i64,
        "soft_limit" => 4096,
        "hint" => "Command exceeds 4 KB soft limit. Long commands may cause UART buffer overflow or prompt timeout.",
    };
    let chain_warning = wp54_warning(&format!("reboot # {}", "x".repeat(5000)));
    assert_eq!(
        response.get("warning"),
        Some(&PyValue::Dict(length_warning.clone())),
        "warning 單數相容欄位必須保留既有第一則警告"
    );
    assert_eq!(
        response.get("warnings"),
        Some(&PyValue::List(vec![
            PyValue::Dict(length_warning),
            PyValue::Dict(chain_warning),
        ]))
    );
    assert_eq!(
        keys(&response),
        [
            "ok",
            "cmd_id",
            "status",
            "session_id",
            "warning",
            "warnings",
            "source_class",
            "version"
        ]
    );
}

#[test]
fn wp54_chained_warning_is_omitted_when_flagged() {
    let r = ServiceRig::ready_shell();
    let response = r.call(
        "command.submit",
        &format!(r#"{{"selector":"COM0","cmd":"{WP52_CHAINED}","expect_reboot":true}}"#),
    );
    assert_eq!(
        response.get("ok"),
        Some(&PyValue::Bool(true)),
        "{}",
        j(&response)
    );
    assert!(response.get("warning").is_none(), "{}", j(&response));
    assert!(response.get("warnings").is_none(), "{}", j(&response));
}

#[test]
fn wp54_chained_warning_is_omitted_for_interactive_mode() {
    let r = ServiceRig::ready_shell();
    let response = r.call(
        "command.submit",
        &format!(r#"{{"selector":"COM0","cmd":"{WP52_CHAINED}","mode":"interactive"}}"#),
    );
    assert_eq!(
        response.get("ok"),
        Some(&PyValue::Bool(true)),
        "{}",
        j(&response)
    );
    assert!(response.get("warning").is_none(), "{}", j(&response));
    assert!(response.get("warnings").is_none(), "{}", j(&response));
}

/// 成功回應：`arbiter.py:307-310` 先依旗標回聲 `expect_reboot`，再由 `_annotate_source` 補 `source_class`。
fn wp52_submit_echo(r: &ServiceRig, cmd_id: &str, expect_reboot: bool) -> String {
    let session_id = r.mgr.resolve("COM0").expect("COM0 session");
    let mut expected = sw_proto::pydict! {
        "ok" => true,
        "cmd_id" => cmd_id,
        "status" => "accepted",
        "session_id" => session_id,
    };
    if expect_reboot {
        expected.insert("expect_reboot", PyValue::Bool(true));
    }
    expected.insert("source_class", PyValue::from("agent"));
    expected.insert("version", PyValue::from(r.version.clone()));
    j(&expected)
}

/// 送出假 DUT 會正常回 prompt 的普通命令並等到真正執行完畢；回傳 (submit 回應, `command.get` 的 command, 本命令 UART TX)。
fn wp52_submit_and_finish(
    r: &ServiceRig,
    command: &str,
    flag: Option<&str>,
    mode: &str,
) -> (PyDict, PyDict, Vec<u8>) {
    let dev = r.device(REAL_A);
    dev.on_tx_reply(
        format!("{command}\n").as_bytes(),
        b"\r\nwp52-out\r\nroot@t:/# ",
    );
    r.settle();
    let tx_mark = dev.tx_len();
    let flag = flag
        .map(|value| format!(r#","expect_reboot":{value}"#))
        .unwrap_or_default();
    let params =
        format!(r#"{{"selector":"COM0","cmd":"{command}","mode":"{mode}","timeout_s":10{flag}}}"#);
    let response = r.call("command.submit", &params);
    assert_eq!(
        response.get("ok"),
        Some(&PyValue::Bool(true)),
        "{params}：{}",
        j(&response)
    );
    let cmd_id = s(&response, "cmd_id").to_string();
    let mut record = PyDict::default();
    wait_until("命令真正執行完畢", || {
        record = sub(
            &r.call("command.get", &format!(r#"{{"cmd_id":"{cmd_id}"}}"#)),
            "command",
        );
        !matches!(record.get("done_at"), None | Some(PyValue::Null))
    });
    (response, record, dev.tx_since(tx_mark))
}

/// 已執行完畢命令的共同斷言：回聲、UART TX、終態與 registry／`command.get` 的旗標。
fn assert_wp52_finished(
    r: &ServiceRig,
    case: &str,
    command: &str,
    mode: (&str, &str),
    expect_reboot: bool,
    finished: &(PyDict, PyDict, Vec<u8>),
) {
    let (response, record, tx) = finished;
    let cmd_id = s(response, "cmd_id");
    assert_eq!(
        j(response),
        wp52_submit_echo(r, cmd_id, expect_reboot),
        "{case}：submit 回聲"
    );
    assert_eq!(
        String::from_utf8_lossy(tx),
        format!("{command}\n"),
        "{case}：UART 只送出本命令一行"
    );
    assert_eq!(s(record, "command"), command, "{case}");
    assert_eq!(s(record, "mode"), mode.0, "{case}：record 保留原始 mode");
    assert_eq!(s(record, "execution_mode"), mode.1, "{case}：fg／bg 正規化");
    assert_eq!(s(record, "status"), "done", "{case}：命令真正完成");
    assert_eq!(
        record.get("error_code"),
        Some(&PyValue::Null),
        "{case}：{}",
        j(record)
    );
    assert_eq!(
        record.get("expect_reboot"),
        Some(&PyValue::Bool(expect_reboot)),
        "{case}：command.get 旗標"
    );
    assert_eq!(
        r.registry
            .get(cmd_id)
            .expect("registry 命令紀錄")
            .get("expect_reboot"),
        Some(&PyValue::Bool(expect_reboot)),
        "{case}：registry 旗標"
    );
    assert!(
        record.get("trusted_human").is_none(),
        "{case}：record 不外露 trusted_human"
    );
}

/// 對照 `tests/test_expect_reboot.py::test_rpc_queue_worker_execute_and_command_get_propagate_flag`：
/// 明示 true 經 RPC 入口寫進 `SubmitReq`，回聲 `expect_reboot:true`，命令實際送上 UART 並完成，`command.get` 回 true。
/// 只驗資料面接線；reboot 分支、WAL META 與 G7b 消費端屬後續工作包，故每個 mode 用獨立 rig、不斷言 session 後態。
#[test]
fn wp52_end_to_end_flag_record_and_echo() {
    for (mode, normalized) in WP52_MODES {
        let r = ServiceRig::ready_shell();
        let command = format!("echo wp52-true-{mode}");
        let finished = wp52_submit_and_finish(&r, &command, Some("true"), mode);
        assert_wp52_finished(
            &r,
            &format!("true／{mode}"),
            &command,
            (mode, normalized),
            true,
            &finished,
        );
    }
}

/// 缺省／null／false：不回聲、record 固定為 false，命令照常執行完畢且 session 維持 READY。
#[test]
fn wp52_end_to_end_falsy_flag_not_echoed() {
    let r = ServiceRig::ready_shell();
    for (label, flag) in [
        ("default", None),
        ("null", Some("null")),
        ("false", Some("false")),
    ] {
        for (mode, normalized) in WP52_MODES {
            let command = format!("echo wp52-{label}-{mode}");
            let finished = wp52_submit_and_finish(&r, &command, flag, mode);
            let case = format!("{label}／{mode}");
            assert_wp52_finished(&r, &case, &command, (mode, normalized), false, &finished);
            assert!(
                finished.0.get("expect_reboot").is_none(),
                "{case}：false 類型不回聲"
            );
            assert_eq!(
                r.session("COM0")
                    .and_then(|session| session.get("state").cloned()),
                Some(PyValue::from("READY")),
                "{case}"
            );
        }
    }
}

/// 同一 gate 下，帶 true 與不帶旗標的拒絕回應逐位元組相同，且皆零 UART TX、零命令紀錄、零入列。
fn assert_wp52_gate_unchanged_by_flag(r: &ServiceRig, case: &str, expected_code: &str) {
    let plain = format!(r#"{{"selector":"COM0","cmd":"{WP52_CHAINED}","source":"agent:wp52"}}"#);
    let flagged = format!(
        r#"{{"selector":"COM0","cmd":"{WP52_CHAINED}","source":"agent:wp52","expect_reboot":true}}"#
    );
    r.settle();
    let before = wp52_rpc_snapshot(r);
    let baseline = r.call("command.submit", &plain);
    r.settle();
    assert_eq!(
        wp52_rpc_snapshot(r),
        before,
        "{case}：無旗標拒絕亦須零副作用"
    );
    assert_eq!(
        s(&baseline, "error_code"),
        expected_code,
        "{case}：{}",
        j(&baseline)
    );
    assert!(baseline.get("cmd_id").is_none(), "{case}");
    assert_wp52_rpc_rejected_without_side_effects(r, &flagged, &j(&baseline));
}

/// 對照 `tests/test_expect_reboot.py::test_rpc_flag_does_not_bypass_submit_time_quiet_gate`：
/// agent＋true 在非 READY、boot quiet、READY_UNCONFIRMED 的提交時 gate 維持原錯誤且零 TX。
#[test]
fn wp52_end_to_end_flag_does_not_bypass_submit_gates() {
    use sw_core::session_core::SessionState;
    for (state, code) in [
        (SessionState::Attaching, "SESSION_NOT_READY"),
        (SessionState::Attached, "SESSION_NOT_READY"),
        (SessionState::Recovering, "SESSION_RECOVERING"),
        (SessionState::Flashing, "FLASHING_BUSY"),
    ] {
        let r = ServiceRig::ready_shell();
        r.mgr.force_session_state("COM0", state);
        assert_wp52_gate_unchanged_by_flag(&r, &format!("{state:?}"), code);
    }

    let r = ServiceRig::ready_shell();
    // 自發重開機 banner → 武裝 boot quiet 與 Ready Reconfirm（同 `ready_unconfirmed_response_has_session_field`）。
    r.device(REAL_A).push_rx(b"\r\nU-Boot 2024.01 (Jan 01)\r\n");
    wait_until("boot quiet 武裝", || {
        r.session("COM0")
            .and_then(|session| session.get("boot_quiet_remaining_s").cloned())
            .is_some_and(|value| !value.is_null())
    });
    assert_wp52_gate_unchanged_by_flag(&r, "boot quiet", "AUTOBOOT_QUIET");
    r.clock.advance(Duration::from_secs(301));
    assert_wp52_gate_unchanged_by_flag(&r, "READY_UNCONFIRMED", "READY_UNCONFIRMED");
}

// ---------------------------------------------------------------------------
// WP5-3 第 1 項：`expect_reboot` 執行路由端到端（G3；Python `origin/main:sw_core/session_manager.py:6840-6868`）
//
// 呼叫路徑：`Service::rpc(command.submit)` → `expect_reboot_rejection`／`SubmitReq.expect_reboot` →
// `SessionCore::submit` → queue → `start_next` → `OpSpec::Command` → `OpRunner::run_command`
// （`expect_reboot || guessed_reboot`）→ `run_reboot` → `OpOutcome::RebootExpected` → `SessionCore::finish_command`
// → registry → `command.get`；UART 端由 `TxWriter` 寫進 FakeDevice。
// ---------------------------------------------------------------------------

/// 串接式重開命令：`is_reboot_command()` 不猜中，只能靠旗標分流。
const WP53_CHAINED: &str = "echo a && reboot";

/// 送出 `WP53_CHAINED`（假 DUT 不回 prompt、timeout 1 s），推進 FakeClock 直到結案；回傳 (命令紀錄, 本命令起的 UART TX)。
fn wp53_submit_chained(r: &ServiceRig, flag: &str) -> (PyDict, Vec<u8>) {
    let dev = r.device(REAL_A);
    r.settle();
    let tx_mark = dev.tx_len();
    let params = format!(r#"{{"selector":"COM0","cmd":"{WP53_CHAINED}","timeout_s":1{flag}}}"#);
    let response = r.call("command.submit", &params);
    assert_eq!(
        response.get("ok"),
        Some(&PyValue::Bool(true)),
        "{params}：{}",
        j(&response)
    );
    let cmd_id = s(&response, "cmd_id").to_string();
    let mut record = PyDict::default();
    wait_until(&format!("{cmd_id} 結案"), || {
        r.clock.advance(Duration::from_millis(100));
        record = sub(
            &r.call("command.get", &format!(r#"{{"cmd_id":"{cmd_id}"}}"#)),
            "command",
        );
        !matches!(record.get("done_at"), None | Some(PyValue::Null))
    });
    (record, dev.tx_since(tx_mark))
}

/// 對照 `tests/test_expect_reboot.py::test_flag_routes_chained_command_to_reboot_handler` 與
/// `::test_without_flag_chained_command_keeps_old_behaviour`：RPC 帶 `expect_reboot:true` 的串接命令在 prompt 未回時
/// 結算為 `EXPECT_REBOOT`（partial），UART 先送出本命令且全程無 Ctrl-C／Ctrl-D；不帶旗標的同一命令走一般逾時救援，
/// UART 送出 Ctrl-C。
#[test]
fn wp53_rpc_flag_routes_chained_command_to_reboot() {
    let line = format!("{WP53_CHAINED}\n");

    let r = ServiceRig::ready_shell();
    let (record, tx) = wp53_submit_chained(&r, r#","expect_reboot":true"#);
    assert_eq!(s(&record, "status"), "done", "{}", j(&record));
    assert_eq!(
        record.get("partial"),
        Some(&PyValue::Bool(true)),
        "{}",
        j(&record)
    );
    assert_eq!(s(&record, "recovery_action"), "EXPECT_REBOOT", "{}", j(&record));
    assert_eq!(record.get("error_code"), Some(&PyValue::Null), "{}", j(&record));
    assert_eq!(record.get("expect_reboot"), Some(&PyValue::Bool(true)));
    assert!(
        tx.starts_with(line.as_bytes()),
        "UART 先送出本命令：{:?}",
        String::from_utf8_lossy(&tx)
    );
    assert!(
        !tx.iter().any(|b| matches!(b, 0x03 | 0x04)),
        "旗標命令走 reboot 分支，UART 不得出現救援鍵：{:?}",
        String::from_utf8_lossy(&tx)
    );

    let r = ServiceRig::ready_shell();
    let (record, tx) = wp53_submit_chained(&r, "");
    assert_eq!(
        record.get("partial"),
        Some(&PyValue::Bool(true)),
        "{}",
        j(&record)
    );
    assert_ne!(
        record.get("recovery_action"),
        Some(&PyValue::from("EXPECT_REBOOT")),
        "無旗標不得走 reboot 分支：{}",
        j(&record)
    );
    assert_eq!(record.get("expect_reboot"), Some(&PyValue::Bool(false)));
    assert!(tx.starts_with(line.as_bytes()), "{:?}", String::from_utf8_lossy(&tx));
    assert!(
        tx.contains(&0x03),
        "無旗標逾時走一般救援送 Ctrl-C：{:?}",
        String::from_utf8_lossy(&tx)
    );
}

// WP5-3 第 2 項：reboot prompt 等待時間端到端（Python `origin/main:sw_core/session_manager.py:6394`、`:6448-6449`、
// `:6860-6864`）。呼叫路徑同上：RPC `timeout_s`／`expected_duration_s` → `SubmitReq` → `OpRunner::run_command`
// 計算 `prompt_wait_s`（旗標且非猜中 → `max(timeout_s, expected_duration_s or 0)`；猜中 → `None`）→ `run_reboot`
// 的 prompt 等待期限。

/// 以 RPC 送出帶旗標的命令（假 DUT 不回 prompt），等 UART 送出本命令並讓 OpRunner 進入 prompt 等待，再以 250 ms
/// FakeClock 步進（每步讓出 80 ms 實時，長於 RX 等待的 50 ms 時鐘切片）直到結案或推進滿 `limit_s`；
/// 回傳 (cmd_id, 結案時距等待起點的 FakeClock 秒數，未結案為 `None`)。
fn wp53_rpc_step_flagged(
    r: &ServiceRig,
    cmd: &str,
    extra: &str,
    limit_s: f64,
) -> (String, Option<f64>) {
    let dev = r.device(REAL_A);
    r.settle();
    let tx_mark = dev.tx_len();
    let params = format!(r#"{{"selector":"COM0","cmd":"{cmd}","expect_reboot":true{extra}}}"#);
    let response = r.call("command.submit", &params);
    assert_eq!(
        response.get("ok"),
        Some(&PyValue::Bool(true)),
        "{params}：{}",
        j(&response)
    );
    let cmd_id = s(&response, "cmd_id").to_string();
    let line = format!("{cmd}\n");
    wait_until("UART 送出本命令", || {
        dev.tx_since(tx_mark).starts_with(line.as_bytes())
    });
    std::thread::sleep(Duration::from_millis(150));
    let start = r.clock.elapsed();
    loop {
        let waited = (r.clock.elapsed() - start).as_secs_f64();
        let record = sub(
            &r.call("command.get", &format!(r#"{{"cmd_id":"{cmd_id}"}}"#)),
            "command",
        );
        if !matches!(record.get("done_at"), None | Some(PyValue::Null)) {
            return (cmd_id, Some(waited));
        }
        if waited >= limit_s {
            return (cmd_id, None);
        }
        r.clock.advance(Duration::from_millis(250));
        std::thread::sleep(Duration::from_millis(80));
    }
}

/// 對照 `tests/test_expect_reboot.py::test_flag_routes_chained_command_to_reboot_handler`（`prompt_wait_s ==
/// max(timeout, expected)`）、`::test_chained_command_prompt_after_pre_segment_is_not_treated_as_recovery` 與
/// `::test_flag_with_plain_reboot_keeps_two_second_prompt_wait`：
/// - 旗標串接命令 `timeout_s:1`、`expected_duration_s:5`：3 s 內不結算；3 s 時 prompt 回來 → 正常完成（partial:false、
///   無 recovery_action），session 仍 READY，UART 無救援鍵。
/// - 單獨 `reboot` 帶旗標 `timeout_s:10`、`expected_duration_s:30`：維持 `min(timeout, 2 s)`，約 2 s 結算為 EXPECT_REBOOT。
#[test]
fn wp53_rpc_flag_prompt_wait_uses_timeout_and_expected_duration() {
    let r = ServiceRig::ready_shell();
    let dev = r.device(REAL_A);
    let tx_mark = dev.tx_len();
    let (cmd_id, early) = wp53_rpc_step_flagged(
        &r,
        WP53_CHAINED,
        r#","timeout_s":1,"expected_duration_s":5"#,
        3.0,
    );
    assert_eq!(early, None, "期限應為 max(1, 5) = 5 s，不得在 3 s 前結算");
    dev.push_rx(b"\r\nroot@t:/# ");
    let mut record = PyDict::default();
    wait_until(&format!("{cmd_id} 結案"), || {
        record = sub(
            &r.call("command.get", &format!(r#"{{"cmd_id":"{cmd_id}"}}"#)),
            "command",
        );
        !matches!(record.get("done_at"), None | Some(PyValue::Null))
    });
    assert_eq!(s(&record, "status"), "done", "{}", j(&record));
    assert_eq!(
        record.get("partial"),
        Some(&PyValue::Bool(false)),
        "{}",
        j(&record)
    );
    assert_eq!(
        record.get("recovery_action"),
        Some(&PyValue::Null),
        "不得進 EXPECT_REBOOT：{}",
        j(&record)
    );
    assert_eq!(
        record.get("error_code"),
        Some(&PyValue::Null),
        "{}",
        j(&record)
    );
    assert_eq!(record.get("expect_reboot"), Some(&PyValue::Bool(true)));
    let session = r.session("COM0").expect("COM0 session");
    assert_eq!(
        session.get("state"),
        Some(&PyValue::from("READY")),
        "不得進 RECOVERING：{}",
        j(&session)
    );
    let tx = dev.tx_since(tx_mark);
    assert!(
        !tx.iter().any(|b| matches!(b, 0x03 | 0x04)),
        "reboot 分支不得送救援鍵：{:?}",
        String::from_utf8_lossy(&tx)
    );

    let r = ServiceRig::ready_shell();
    let (cmd_id, settled) = wp53_rpc_step_flagged(
        &r,
        "reboot",
        r#","timeout_s":10,"expected_duration_s":30"#,
        10.0,
    );
    let settled = settled.expect("單獨 reboot 應在 10 s 內結算");
    assert!(
        (2.0..3.0).contains(&settled),
        "單獨 reboot 維持 min(timeout, 2 s)，實際 {settled}"
    );
    let record = sub(
        &r.call("command.get", &format!(r#"{{"cmd_id":"{cmd_id}"}}"#)),
        "command",
    );
    assert_eq!(
        s(&record, "recovery_action"),
        "EXPECT_REBOOT",
        "{}",
        j(&record)
    );
    assert_eq!(
        record.get("partial"),
        Some(&PyValue::Bool(true)),
        "{}",
        j(&record)
    );
}

// WP5-3 第 3 項：旗標命令的 reboot arming 與完成後 gate 端到端（Python `origin/main:sw_core/session_manager.py:6412-6447`、
// `:6462-6473`；submit-time gate `service.py:831-838`）。呼叫路徑：`Service::rpc(command.submit)` →
// `SubmitReq.expect_reboot` → `SessionCore::submit` → `start_next`（`reboot = expect_reboot || 猜中`）→
// `Fence.reboot_arm_op` → `OpRunner::run_command` → `run_reboot` → `TxWriter` 實際提交後於同一 Fence 臨界區 arm
// quiet／reconfirm → `UartTxCompleted` → `reboot_tx_settled`（actor QuietState）→ prompt 回來 → `finish_command`
// （只清 quiet，#162）→ 下一筆 `command.submit` 的 submit-time gate。

/// Python `service.py:831-838` pending-only 分支的 hint（逐字）。
const WP53_RECONFIRM_HINT: &str =
    "boot quiet window 已結束但 READY 尚未重新確認；等 daemon 重新確認 READY 後重送";

/// 對照 `tests/test_expect_reboot.py::test_chained_command_prompt_after_pre_segment_is_not_treated_as_recovery`：
/// RPC 帶 `expect_reboot:true` 的串接命令 TX 提交後公開狀態即 arm quiet 與 reconfirm；3 s 後 prompt 回來 → 正常完成，
/// quiet 清除但 `ready_reconfirm_pending` 保留；下一筆 agent 命令以 pending-only `AUTOBOOT_QUIET`（retry 5.0）拒絕，
/// 回應逐位元組等於 Python 分支字面量，UART／registry／queue 前後不變。無旗標對照：同一命令 prompt 回來後不留 pending，
/// 下一筆照常送出。
#[test]
fn wp53_rpc_flag_arms_reconfirm_and_gates_next_command() {
    let r = ServiceRig::ready_shell();
    let dev = r.device(REAL_A);
    let (cmd_id, early) = wp53_rpc_step_flagged(&r, WP53_CHAINED, r#","timeout_s":6"#, 3.0);
    assert_eq!(early, None, "期限 6 s，3 s 內不得結算");
    let armed = r.session("COM0").expect("COM0 session");
    assert_ne!(
        armed.get("boot_quiet_remaining_s"),
        Some(&PyValue::Null),
        "TX 提交後 arm quiet：{}",
        j(&armed)
    );
    assert_eq!(
        armed.get("ready_reconfirm_pending"),
        Some(&PyValue::Bool(true)),
        "TX 提交後 arm reconfirm：{}",
        j(&armed)
    );
    dev.push_rx(b"\r\nroot@t:/# ");
    let record = wp55_wait_done(&r, &cmd_id);
    assert_eq!(s(&record, "status"), "done", "{}", j(&record));
    assert_eq!(
        record.get("partial"),
        Some(&PyValue::Bool(false)),
        "{}",
        j(&record)
    );
    assert_eq!(
        record.get("recovery_action"),
        Some(&PyValue::Null),
        "{}",
        j(&record)
    );
    r.settle();
    let session = r.session("COM0").expect("COM0 session");
    assert_eq!(
        session.get("state"),
        Some(&PyValue::from("READY")),
        "{}",
        j(&session)
    );
    assert_eq!(
        session.get("boot_quiet_remaining_s"),
        Some(&PyValue::Null),
        "prompt 回來只清 quiet：{}",
        j(&session)
    );
    assert_eq!(
        session.get("ready_reconfirm_pending"),
        Some(&PyValue::Bool(true)),
        "#162：pending 保留：{}",
        j(&session)
    );
    let expected = sw_proto::pydict! {
        "ok" => false,
        "error_code" => "AUTOBOOT_QUIET",
        "retry_after_s" => 5.0,
        "hint" => WP53_RECONFIRM_HINT,
        "session" => PyValue::Dict(session.clone()),
        "version" => r.version.clone(),
    };
    assert_wp52_rpc_rejected_without_side_effects(
        &r,
        r#"{"selector":"COM0","cmd":"ls","timeout_s":10}"#,
        &j(&expected),
    );

    let r = ServiceRig::ready_shell();
    let dev = r.device(REAL_A);
    dev.on_tx_reply(
        format!("{WP53_CHAINED}\n").as_bytes(),
        b"echo a && reboot\r\na\r\nroot@t:/# ",
    );
    r.settle();
    let response = r.call(
        "command.submit",
        &format!(r#"{{"selector":"COM0","cmd":"{WP53_CHAINED}","timeout_s":6}}"#),
    );
    let record = wp55_wait_done(&r, s(&response, "cmd_id"));
    assert_eq!(record.get("partial"), Some(&PyValue::Bool(false)), "{}", j(&record));
    r.settle();
    let session = r.session("COM0").expect("COM0 session");
    assert_eq!(
        session.get("ready_reconfirm_pending"),
        Some(&PyValue::Bool(false)),
        "無旗標不 arm：{}",
        j(&session)
    );
    dev.on_tx_reply(b"ls\n", b"ls\r\nroot@t:/# ");
    let tx_mark = dev.tx_len();
    let response = r.call("command.submit", r#"{"selector":"COM0","cmd":"ls","timeout_s":10}"#);
    assert_eq!(response.get("ok"), Some(&PyValue::Bool(true)), "{}", j(&response));
    let record = wp55_wait_done(&r, s(&response, "cmd_id"));
    assert_eq!(s(&record, "status"), "done", "{}", j(&record));
    assert_eq!(String::from_utf8_lossy(&dev.tx_since(tx_mark)), "ls\n");
}

// WP5-3 第 4 項：passthrough 旗標命令與 SC-35 的底層 TX gate 端到端（裁決 s2 D2；Python
// `origin/main:sw_core/session_manager.py:814-826` `agent_gate_active()` 對 passthrough 固定回 False、`:778-785`
// `_push_tx_gate()` 以此發布 bridge 的 `_gate_quiet`）。呼叫路徑：`Service::rpc(command.submit)` →
// `SessionCore::submit`（passthrough 不套 submit-time gate）→ `start_next`（`reboot = expect_reboot || 猜中`）→
// `Fence.reboot_arm_op` → `OpRunner::run_command` → `run_reboot` → `TxWriter` 提交並 arm quiet／reconfirm →
// prompt 回來 `finish_command` → 下一筆 `command.submit` → `OpRunner::send` → `Fence::admit`（`bridge_passthrough`
// 由 `SessionCore::adopt_opened_bridge` 發布）→ FakeDevice＋WAL。

/// passthrough target（COM0）：pin 選定後零 TX 停 ATTACHED，連續 30 s 無 RX 升 READY（SC-35）。
fn wp53_passthrough_ready() -> ServiceRig {
    let r = ServiceRig::build(support::RigSpec {
        yaml: format!(
            "profiles:\n  u:\n    platform: passthrough\n    prompt_regex: '(?m)^=> '\n    ready_probe: ''\n\
             targets:\n  - {{com: COM0, profile: u, device_by_id: {BY_ID_A}}}\n"
        ),
        devices: vec![(BY_ID_A.into(), REAL_A.into())],
        explicit_target_count: 1,
        allowlist: None,
        wait_ready: vec![],
    });
    r.wait_state("COM0", "ATTACHED");
    assert_eq!(r.device(REAL_A).write_calls(), 0, "passthrough attach 零 TX");
    wait_until("passthrough 30 s 無 RX 升 READY", || {
        r.clock.advance(Duration::from_secs(1));
        r.mgr.on_tick();
        std::thread::sleep(Duration::from_millis(5));
        r.session("COM0")
            .and_then(|s| s.get("state").and_then(PyValue::as_str).map(str::to_string))
            .as_deref()
            == Some("READY")
    });
    r.settle();
    r
}

/// 旗標命令（U-Boot `reset`）經 RPC 走 reboot 路徑、TX 提交後 arm reconfirm，回 `=> ` 完成後 pending 照記
/// （不以清空假裝完成 SC-35）；下一筆合法 agent 命令經 RPC 提交後真正寫入 UART、WAL 有本 cmd_id 的 TX、紀錄 `done`。
#[test]
fn wp53_rpc_passthrough_flag_followup_tx_not_gated() {
    let r = wp53_passthrough_ready();
    let dev = r.device(REAL_A);
    dev.on_tx_reply(b"reset\n", b"reset\r\nresetting ...\r\n=> ");
    let tx_mark = dev.tx_len();
    let response = r.call(
        "command.submit",
        r#"{"selector":"COM0","cmd":"reset","timeout_s":6,"expect_reboot":true}"#,
    );
    assert_eq!(response.get("ok"), Some(&PyValue::Bool(true)), "{}", j(&response));
    let record = wp55_wait_done(&r, s(&response, "cmd_id"));
    assert_eq!(s(&record, "status"), "done", "{}", j(&record));
    assert_eq!(record.get("partial"), Some(&PyValue::Bool(false)), "{}", j(&record));
    assert_eq!(record.get("recovery_action"), Some(&PyValue::Null), "{}", j(&record));
    assert_eq!(String::from_utf8_lossy(&dev.tx_since(tx_mark)), "reset\n");
    r.settle();
    let session = r.session("COM0").expect("COM0 session");
    assert_eq!(session.get("state"), Some(&PyValue::from("READY")), "{}", j(&session));
    assert_eq!(
        session.get("ready_reconfirm_pending"),
        Some(&PyValue::Bool(true)),
        "Python `arm_boot_quiet()` 不分 platform，pending 照記：{}",
        j(&session)
    );

    dev.on_tx_reply(b"printenv\n", b"printenv\r\nbootcmd=x\r\n=> ");
    let tx_mark = dev.tx_len();
    let response = r.call(
        "command.submit",
        r#"{"selector":"COM0","cmd":"printenv","timeout_s":10}"#,
    );
    assert_eq!(response.get("ok"), Some(&PyValue::Bool(true)), "{}", j(&response));
    let cmd_id = s(&response, "cmd_id").to_string();
    let record = wp55_wait_done(&r, &cmd_id);
    assert_eq!(s(&record, "status"), "done", "{}", j(&record));
    assert_eq!(record.get("error_code"), Some(&PyValue::Null), "{}", j(&record));
    assert_eq!(s(&record, "stdout"), "bootcmd=x", "{}", j(&record));
    assert_eq!(
        String::from_utf8_lossy(&dev.tx_since(tx_mark)),
        "printenv\n",
        "UART 實際寫出後續命令"
    );
    assert_eq!(
        wp55_wal_tx(&r, &cmd_id),
        vec![("agent".to_string(), wp55_b64(b"printenv\n"))],
        "WAL 須有後續命令的完整 TX"
    );
}

// WP5-3 第 5 項：`command_expect_reboot` WAL META 與 RPC→UART 端到端（G3；Python
// `origin/main:sw_core/session_manager.py:6840-6868`、契約 `contracts/wal_meta_events.schema.json:63-85`）。
// 呼叫路徑：`Service::rpc(command.submit)` → `SubmitReq{expect_reboot}` → `SessionRequest::Submit` →
// `SessionCore::submit` → queue → `start_next` → `OpSpec::Command` → `OpRunner::execute` → `run_command`
// （先寫 `command_expect_reboot` META，再分流 `run_reboot`）→ `TxWriter` → FakeDevice＋WAL → `op_finished` →
// registry → `command.get`。

/// `command.get` 的命令紀錄鍵（Python `origin/main:sw_core/arbiter.py:237-259` 的 rec 順序）。
const WP53_RECORD_KEYS: [&str; 22] = [
    "cmd_id",
    "session_id",
    "command",
    "source",
    "mode",
    "execution_mode",
    "timeout_s",
    "expected_duration_s",
    "completion_regex",
    "expect_reboot",
    "priority",
    "status",
    "created_at",
    "accepted_at",
    "started_at",
    "done_at",
    "error_code",
    "stdout",
    "partial",
    "background_capture_id",
    "interactive_session_id",
    "recovery_action",
];

/// 每次執行都不同的紀錄欄位：ID 與時間戳取實測值，其餘欄位必須等於呼叫端給的字面值。
const WP53_RECORD_DYNAMIC: [&str; 5] =
    ["cmd_id", "created_at", "accepted_at", "started_at", "done_at"];

/// `command.get` 的完整預期回應：動態欄位取實測值（且必須非 null），其餘依 `fixed`；
/// `session_id` 由 manager 解析，`source_class` 依 `_annotate_source` 接在最後。
fn wp53_expected_get(r: &ServiceRig, measured: &PyDict, fixed: &PyDict) -> String {
    let mut record = PyDict::new();
    for key in WP53_RECORD_KEYS {
        let value = if WP53_RECORD_DYNAMIC.contains(&key) {
            let value = measured
                .get(key)
                .cloned()
                .unwrap_or_else(|| panic!("實測紀錄缺 {key}：{}", j(measured)));
            assert!(!value.is_null(), "{key} 應已填值：{}", j(measured));
            value
        } else if key == "session_id" {
            PyValue::from(r.mgr.resolve("COM0").expect("COM0 session"))
        } else {
            fixed
                .get(key)
                .cloned()
                .unwrap_or_else(|| panic!("預期缺 {key}"))
        };
        record.insert(key, value);
    }
    record.insert("source_class", PyValue::from("agent"));
    j(&sw_proto::pydict! {
        "ok" => true,
        "command" => PyValue::Dict(record),
        "version" => r.version.clone(),
    })
}

/// WAL 內屬於 `cmd_id` 的全部紀錄（任何 dir），依寫入順序。
fn wp53_wal_rows(r: &ServiceRig, cmd_id: &str) -> Vec<PyDict> {
    let bytes = r.wfs.contents(WP55_WAL.as_ref()).unwrap_or_default();
    String::from_utf8(bytes)
        .expect("WAL 應為 UTF-8")
        .lines()
        .filter_map(|line| {
            let PyValue::Dict(record) = sw_proto::pyjson::loads(line).expect("WAL 行應為 JSON")
            else {
                panic!("WAL 行應為 object：{line}");
            };
            (record.get("cmd_id") == Some(&PyValue::from(cmd_id))).then_some(record)
        })
        .collect()
}

/// 去除 `seq`／`mono_ts_ns`／`wall_ts` 後的 WAL 紀錄（WAL 落盤為 sort_keys，載回順序即落盤順序）。
fn wp53_wal_static(row: &PyDict) -> String {
    let pairs = row
        .0
        .iter()
        .filter(|(k, _)| !["seq", "mono_ts_ns", "wall_ts"].contains(&k.as_str()))
        .map(|(k, v)| (k.clone(), v.clone()))
        .collect();
    j(&PyDict::from_pairs(pairs))
}

/// 旗標命令本 cmd_id 的 WAL 恰為 [`command_expect_reboot` META, 命令 TX]，META 外層形狀符合契約、seq 較小；
/// `wal_tx` 為 TX 的 (source, 原始 bytes)。
fn assert_wp53_wal_meta_then_tx(r: &ServiceRig, case: &str, cmd_id: &str, wal_tx: (&str, &[u8])) {
    let rows = wp53_wal_rows(r, cmd_id);
    assert_eq!(rows.len(), 2, "{case}：本 cmd_id 恰 META＋TX 兩筆：{rows:?}");
    assert_eq!(
        wp53_wal_static(&rows[0]),
        format!(
            "{{\"cmd_id\":\"{cmd_id}\",\"com\":\"COM0\",\"crc32\":\"00000000\",\"dir\":\"META\",\"len\":0,\
             \"loss_flag\":false,\"meta\":{{\"event\":\"command_expect_reboot\",\"expect_reboot\":true}},\
             \"payload_b64\":\"\",\"source\":\"system\"}}"
        ),
        "{case}：第一筆為契約形狀的 command_expect_reboot META"
    );
    assert_eq!(s(&rows[1], "dir"), "TX", "{case}：{}", j(&rows[1]));
    assert_eq!(s(&rows[1], "source"), wal_tx.0, "{case}");
    assert_eq!(
        s(&rows[1], "payload_b64"),
        wp55_b64(wal_tx.1),
        "{case}：WAL TX 為本命令完整一行"
    );
    let seq = |row: &PyDict| row.get("seq").and_then(PyValue::as_i64).expect("seq");
    assert!(
        seq(&rows[0]) < seq(&rows[1]),
        "{case}：META seq {} 須小於 TX seq {}",
        seq(&rows[0]),
        seq(&rows[1])
    );
}

/// 對照 `tests/test_expect_reboot.py::test_flag_routes_chained_command_to_reboot_handler`（META）、
/// `::test_chained_command_prompt_after_pre_segment_is_not_treated_as_recovery`（3 s 後 prompt 仍成功）與
/// `::test_flag_does_not_bypass_autoboot_quiet_gate`（後續 agent 命令被 gate）：RPC 提交旗標串接命令
/// （`timeout_s:1`、`expected_duration_s:5`），submit 回聲逐位元組；UART 恰送出本命令一行；3 s 內不結算，
/// 3 s 後注入 prompt → `command.get` 完整等於正常完成；WAL 為 [META, TX]；下一筆 shell agent 命令以 pending-only
/// `AUTOBOOT_QUIET` 拒絕，UART／WAL／registry／queue 前後不變（零 TX）。
#[test]
fn wp53_rpc_flag_route_wal_and_followup_gate() {
    let r = ServiceRig::ready_shell();
    let dev = r.device(REAL_A);
    r.settle();
    let tx_mark = dev.tx_len();
    let response = r.call(
        "command.submit",
        &format!(
            r#"{{"selector":"COM0","cmd":"{WP53_CHAINED}","timeout_s":1,"expected_duration_s":5,"expect_reboot":true}}"#
        ),
    );
    let cmd_id = s(&response, "cmd_id").to_string();
    assert_eq!(j(&response), wp52_submit_echo(&r, &cmd_id, true), "submit 回聲");
    let line = format!("{WP53_CHAINED}\n");
    wait_until("UART 送出本命令", || {
        dev.tx_since(tx_mark).starts_with(line.as_bytes())
    });
    std::thread::sleep(Duration::from_millis(150));
    let start = r.clock.elapsed();
    while (r.clock.elapsed() - start).as_secs_f64() < 3.0 {
        let record = sub(
            &r.call("command.get", &format!(r#"{{"cmd_id":"{cmd_id}"}}"#)),
            "command",
        );
        assert_eq!(
            record.get("done_at"),
            Some(&PyValue::Null),
            "期限 max(1, 5) = 5 s，3 s 內不得結算：{}",
            j(&record)
        );
        r.clock.advance(Duration::from_millis(250));
        std::thread::sleep(Duration::from_millis(80));
    }
    dev.push_rx(b"\r\nroot@t:/# ");
    let measured = wp55_wait_done(&r, &cmd_id);
    let get = r.call("command.get", &format!(r#"{{"cmd_id":"{cmd_id}"}}"#));
    let fixed = sw_proto::pydict! {
        "command" => WP53_CHAINED,
        "source" => "agent",
        "mode" => "line",
        "execution_mode" => "line",
        "timeout_s" => 1.0,
        "expected_duration_s" => 5.0,
        "completion_regex" => PyValue::Null,
        "expect_reboot" => true,
        "priority" => 10,
        "status" => "done",
        "error_code" => PyValue::Null,
        "stdout" => "",
        "partial" => false,
        "background_capture_id" => PyValue::Null,
        "interactive_session_id" => PyValue::Null,
        "recovery_action" => PyValue::Null,
    };
    assert_eq!(
        j(&get),
        wp53_expected_get(&r, &measured, &fixed),
        "command.get 終態"
    );
    assert_eq!(
        String::from_utf8_lossy(&dev.tx_since(tx_mark)),
        line,
        "UART 恰送出本命令一行（無救援鍵）"
    );
    r.settle();
    assert_wp53_wal_meta_then_tx(&r, "shell 串接", &cmd_id, ("agent", line.as_bytes()));

    let session = r.session("COM0").expect("COM0 session");
    assert_eq!(session.get("state"), Some(&PyValue::from("READY")), "{}", j(&session));
    assert_eq!(
        session.get("ready_reconfirm_pending"),
        Some(&PyValue::Bool(true)),
        "{}",
        j(&session)
    );
    let expected = sw_proto::pydict! {
        "ok" => false,
        "error_code" => "AUTOBOOT_QUIET",
        "retry_after_s" => 5.0,
        "hint" => WP53_RECONFIRM_HINT,
        "session" => PyValue::Dict(session.clone()),
        "version" => r.version.clone(),
    };
    assert_wp52_rpc_rejected_without_side_effects(
        &r,
        r#"{"selector":"COM0","cmd":"ls","timeout_s":10}"#,
        &j(&expected),
    );
}

/// passthrough（裁決 s2 D2）：RPC 提交旗標 `reset`，submit 回聲逐位元組；UART 恰送出 `reset\n`；`command.get`
/// 完整等於正常完成；WAL 為 [META, TX]（passthrough 也寫 META）；完成後下一筆合法 agent 命令經 RPC 真正送上 UART、
/// WAL 有其 TX、`command.get` 完整等於 `done`。
#[test]
fn wp53_rpc_passthrough_flag_route_and_followup_tx() {
    let r = wp53_passthrough_ready();
    let dev = r.device(REAL_A);
    dev.on_tx_reply(b"reset\n", b"reset\r\nresetting ...\r\n=> ");
    let tx_mark = dev.tx_len();
    let response = r.call(
        "command.submit",
        r#"{"selector":"COM0","cmd":"reset","timeout_s":6,"expect_reboot":true}"#,
    );
    let cmd_id = s(&response, "cmd_id").to_string();
    assert_eq!(j(&response), wp52_submit_echo(&r, &cmd_id, true), "submit 回聲");
    let measured = wp55_wait_done(&r, &cmd_id);
    let get = r.call("command.get", &format!(r#"{{"cmd_id":"{cmd_id}"}}"#));
    let fixed = sw_proto::pydict! {
        "command" => "reset",
        "source" => "agent",
        "mode" => "line",
        "execution_mode" => "line",
        "timeout_s" => 6.0,
        "expected_duration_s" => PyValue::Null,
        "completion_regex" => PyValue::Null,
        "expect_reboot" => true,
        "priority" => 10,
        "status" => "done",
        "error_code" => PyValue::Null,
        "stdout" => "resetting ...",
        "partial" => false,
        "background_capture_id" => PyValue::Null,
        "interactive_session_id" => PyValue::Null,
        "recovery_action" => PyValue::Null,
    };
    assert_eq!(
        j(&get),
        wp53_expected_get(&r, &measured, &fixed),
        "command.get 終態"
    );
    assert_eq!(String::from_utf8_lossy(&dev.tx_since(tx_mark)), "reset\n");
    r.settle();
    assert_wp53_wal_meta_then_tx(&r, "passthrough reset", &cmd_id, ("agent", b"reset\n"));

    dev.on_tx_reply(b"printenv\n", b"printenv\r\nbootcmd=x\r\n=> ");
    let tx_mark = dev.tx_len();
    let response = r.call(
        "command.submit",
        r#"{"selector":"COM0","cmd":"printenv","timeout_s":10}"#,
    );
    let next_id = s(&response, "cmd_id").to_string();
    assert_eq!(j(&response), wp52_submit_echo(&r, &next_id, false), "後續命令 submit 回聲");
    let measured = wp55_wait_done(&r, &next_id);
    let get = r.call("command.get", &format!(r#"{{"cmd_id":"{next_id}"}}"#));
    let fixed = sw_proto::pydict! {
        "command" => "printenv",
        "source" => "agent",
        "mode" => "line",
        "execution_mode" => "line",
        "timeout_s" => 10.0,
        "expected_duration_s" => PyValue::Null,
        "completion_regex" => PyValue::Null,
        "expect_reboot" => false,
        "priority" => 10,
        "status" => "done",
        "error_code" => PyValue::Null,
        "stdout" => "bootcmd=x",
        "partial" => false,
        "background_capture_id" => PyValue::Null,
        "interactive_session_id" => PyValue::Null,
        "recovery_action" => PyValue::Null,
    };
    assert_eq!(
        j(&get),
        wp53_expected_get(&r, &measured, &fixed),
        "後續命令 command.get 終態"
    );
    assert_eq!(
        String::from_utf8_lossy(&dev.tx_since(tx_mark)),
        "printenv\n",
        "UART 實際寫出後續命令"
    );
    assert_eq!(
        wp55_wal_tx(&r, &next_id),
        vec![("agent".to_string(), wp55_b64(b"printenv\n"))],
        "WAL 須有後續命令的完整 TX"
    );
    assert!(
        wp53_wal_rows(&r, &next_id)
            .iter()
            .all(|row| row.get("dir") != Some(&PyValue::from("META"))),
        "無旗標後續命令不寫 META"
    );
}

// ---------------------------------------------------------------------------
// WP5-5 第 3 項：可信 human 命令的 TX gate 端到端（G7a、G7b、OD-74、rulings s2 D5）
//
// 呼叫路徑：`Service::rpc(command.submit)` → `check_source`／`is_bare_human` → `SessionRequest::Submit` →
// `SessionCore::submit`（submit-time gate）→ queue → `start_next`／`execute_time_reject` → `OpSpec::Command` →
// `OpRunner::run_command` → `TxWriter`／`Fence::admit` → FakeDevice＋WAL。
// ---------------------------------------------------------------------------

const WP55_WAL: &str = "/fake/wal/raw.wal.ndjson";
/// Python `origin/main:sw_core/service.py:821` 的 quiet 分支 hint（逐字）。
const WP55_QUIET_HINT: &str =
    "boot quiet window 進行中（疑似板卡自發重開機）；等 session 重新確認 READY 後重送";
/// 假 DUT 對待測命令的回覆（立即回 prompt）。
const WP55_REPLY: &[u8] = b"\r\nwp55-out\r\nroot@t:/# ";

/// 自發重開機 banner 武裝 boot quiet 與 Ready Reconfirm；等公開狀態確實進 quiet 後回傳當下的公開 session。
fn wp55_arm_quiet(r: &ServiceRig) -> PyDict {
    r.device(REAL_A).push_rx(b"\r\nU-Boot 2024.01 (Jan 01)\r\n");
    wait_until("boot quiet 武裝", || {
        r.session("COM0")
            .and_then(|session| session.get("boot_quiet_remaining_s").cloned())
            .is_some_and(|value| !value.is_null())
    });
    r.settle();
    let session = r.session("COM0").expect("COM0 session");
    assert_eq!(
        session.get("state"),
        Some(&PyValue::from("READY")),
        "{}",
        j(&session)
    );
    assert_eq!(
        session.get("ready_reconfirm_pending"),
        Some(&PyValue::Bool(true)),
        "{}",
        j(&session)
    );
    session
}

/// 標準 base64（含 `=` 補齊），比對 WAL TX 紀錄的 `payload_b64`。
fn wp55_b64(bytes: &[u8]) -> String {
    const T: &[u8; 64] = b"ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789+/";
    let mut out = String::new();
    for chunk in bytes.chunks(3) {
        let b = [
            chunk[0],
            *chunk.get(1).unwrap_or(&0),
            *chunk.get(2).unwrap_or(&0),
        ];
        let n = (u32::from(b[0]) << 16) | (u32::from(b[1]) << 8) | u32::from(b[2]);
        for i in 0..4 {
            if i <= chunk.len() {
                out.push(T[((n >> (18 - 6 * i)) & 63) as usize] as char);
            } else {
                out.push('=');
            }
        }
    }
    out
}

/// WAL 中屬於 `cmd_id` 的 TX 紀錄：`(source, payload_b64)`，依寫入順序。
fn wp55_wal_tx(r: &ServiceRig, cmd_id: &str) -> Vec<(String, String)> {
    let bytes = r.wfs.contents(WP55_WAL.as_ref()).unwrap_or_default();
    String::from_utf8(bytes)
        .expect("WAL 應為 UTF-8")
        .lines()
        .filter_map(|line| {
            let PyValue::Dict(record) = sw_proto::pyjson::loads(line).expect("WAL 行應為 JSON")
            else {
                panic!("WAL 行應為 object：{line}");
            };
            (record.get("dir") == Some(&PyValue::from("TX"))
                && record.get("cmd_id") == Some(&PyValue::from(cmd_id)))
            .then(|| {
                (
                    s(&record, "source").to_string(),
                    s(&record, "payload_b64").to_string(),
                )
            })
        })
        .collect()
}

/// 等命令進終態（`done_at` 非 null），回傳 `command.get` 的命令紀錄。
fn wp55_wait_done(r: &ServiceRig, cmd_id: &str) -> PyDict {
    let mut record = PyDict::default();
    wait_until(&format!("{cmd_id} 結案"), || {
        record = sub(
            &r.call("command.get", &format!(r#"{{"cmd_id":"{cmd_id}"}}"#)),
            "command",
        );
        !matches!(record.get("done_at"), None | Some(PyValue::Null))
    });
    record
}

/// 可信命令真正送上 UART 並完成：`done`、無錯誤、stdout 取自假 DUT、UART 與 WAL TX 皆恰為本命令一行。
fn assert_wp55_sent_and_done(
    r: &ServiceRig,
    case: &str,
    cmd_id: &str,
    command: &str,
    source: &str,
    tx_mark: usize,
) {
    let record = wp55_wait_done(r, cmd_id);
    let tx = r.device(REAL_A).tx_since(tx_mark);
    assert_eq!(s(&record, "status"), "done", "{case}：{}", j(&record));
    assert_eq!(
        record.get("error_code"),
        Some(&PyValue::Null),
        "{case}：{}",
        j(&record)
    );
    assert_eq!(s(&record, "source"), source, "{case}");
    assert_eq!(s(&record, "stdout"), "wp55-out", "{case}：{}", j(&record));
    let line = format!("{command}\n");
    assert_eq!(
        String::from_utf8_lossy(&tx),
        line,
        "{case}：UART 只送出本命令一行"
    );
    assert_eq!(
        wp55_wal_tx(r, cmd_id),
        vec![(source.to_string(), wp55_b64(line.as_bytes()))],
        "{case}：WAL 須有本 cmd_id 的完整 TX"
    );
}

/// 對照 `tests/test_stage0_gate_codes.py::test_rpc_bare_human_bypasses_quiet_but_explicit_human_cli_does_not`：
/// RPC 原始值 bare `human`／空名 `human:`（`source_policy.py:48-64` `is_bare_human`）於 boot quiet 期間提交即時可得
/// prompt 的命令——admission 成功、回應帶正規化標註，且 TX 經 `human_line` 類 permit 不受 quiet 擋
/// （`session_manager.py:6603-6607`、`uart_io.py:640`），命令真正送上 UART、WAL 有 TX、紀錄 `done`。
#[test]
fn wp55_rpc_bare_human_bypasses_quiet() {
    for raw in ["human", "human:"] {
        let r = ServiceRig::ready_shell();
        let command = "echo wp55-bare";
        let dev = r.device(REAL_A);
        dev.on_tx_reply(format!("{command}\n").as_bytes(), WP55_REPLY);
        wp55_arm_quiet(&r);
        let tx_mark = dev.tx_len();
        let params =
            format!(r#"{{"selector":"COM0","cmd":"{command}","source":"{raw}","timeout_s":10}}"#);
        let response = r.call("command.submit", &params);
        assert_eq!(
            response.get("ok"),
            Some(&PyValue::Bool(true)),
            "{raw}：{}",
            j(&response)
        );
        let cmd_id = s(&response, "cmd_id").to_string();
        assert_eq!(
            j(&response),
            wp51_submit_success_response(&r, &cmd_id, Some("human:cli"), Some(raw), "human"),
            "{raw}：成功回應"
        );
        assert_wp55_sent_and_done(&r, raw, &cmd_id, command, "human:cli", tx_mark);
    }
}

/// 對照 `tests/test_stage0_gate_codes.py::test_rpc_human_source_is_gated_like_agent` 與
/// `::test_rpc_bare_human_bypasses_quiet_but_explicit_human_cli_does_not` 的 explicit 段：明寫 `human:cli`、
/// `human:spoof` 與 `agent:x` 於 quiet 時以 `AUTOBOOT_QUIET` 拒絕（`service.py:800-822`），回應逐位元組等於
/// Python 分支字面量，UART、WAL、registry、queue 前後完全不變。RPC 參數 `trusted_human:true` 不被讀取
/// （`service.py:1228-1235` 只取 `source_check.is_bare_human`），不得授權明寫前綴。
#[test]
fn wp55_rpc_explicit_human_and_agent_are_gated() {
    let r = ServiceRig::ready_shell();
    let command = "echo wp55-gated";
    // 若被誤放行，假 DUT 會立即回 prompt 讓命令成功；拒絕時此回覆永不觸發。
    r.device(REAL_A)
        .on_tx_reply(format!("{command}\n").as_bytes(), WP55_REPLY);
    let session = wp55_arm_quiet(&r);
    let expected = sw_proto::pydict! {
        "ok" => false,
        "error_code" => "AUTOBOOT_QUIET",
        "retry_after_s" => session
            .get("boot_quiet_remaining_s")
            .cloned()
            .expect("boot_quiet_remaining_s"),
        "hint" => WP55_QUIET_HINT,
        "session" => PyValue::Dict(session.clone()),
        "version" => r.version.clone(),
    };
    for source in ["human:cli", "human:spoof", "agent:x"] {
        for extra in ["", r#","trusted_human":true"#] {
            let params = format!(
                r#"{{"selector":"COM0","cmd":"{command}","source":"{source}","timeout_s":10{extra}}}"#
            );
            assert_wp52_rpc_rejected_without_side_effects(&r, &params, &j(&expected));
        }
    }
    assert_eq!(
        r.session("COM0").map(|session| j(&session)),
        Some(j(&session)),
        "拒絕不改變 session 公開狀態"
    );
}

/// 佇列競態案例：(原始 source, 額外參數, 正規化 (source, normalized_from), source_class, 預期可信)。
type Wp55QueuedCase = (
    &'static str,
    &'static str,
    Option<(&'static str, &'static str)>,
    &'static str,
    bool,
);

/// 佇列競態（G7a）：前景佔住時經 RPC 入列、確認仍在佇列，之後 banner 才武裝 quiet，最後前景以 prompt 正常結束。
/// 出列時 execute-time gate（`session_manager.py:6573-6577`）與 TX fence 只對可信 human 豁免：bare `human`／`human:`
/// 真正送出並完成；明寫 `human:` 前綴（含額外 `trusted_human:true` 參數）與 agent 以 `AUTOBOOT_QUIET` 結案、
/// 待測命令零 TX、WAL 無其 TX 紀錄。
#[test]
fn wp55_rpc_queued_source_trust() {
    let matrix: [Wp55QueuedCase; 6] = [
        ("human", "", Some(("human:cli", "human")), "human", true),
        ("human:", "", Some(("human:cli", "human:")), "human", true),
        ("human:cli", "", None, "human", false),
        (
            "human:cli",
            r#","trusted_human":true"#,
            None,
            "human",
            false,
        ),
        ("human:spoof", "", None, "human", false),
        ("agent:x", "", None, "agent", false),
    ];
    for (raw, extra, normalized, source_class, trusted) in matrix {
        let case = format!("source={raw}{extra}");
        let r = ServiceRig::ready_shell();
        let command = "echo wp55-queued";
        let dev = r.device(REAL_A);
        dev.on_tx_reply(format!("{command}\n").as_bytes(), WP55_REPLY);
        let hold = submit_blocking(&r);
        // 前景須真正佔住 UART：`running` 只代表已出列，OpRunner 送出 TX 尚有毫秒級延遲；banner 若先到，
        // 佔用命令本身會被 quiet 擋下（與 Python 相同語意），就不是本案要驗的「入列後才進 quiet」。
        wait_until("佔用命令已送上 UART", || {
            wp55_wal_tx(&r, &hold) == vec![("agent".to_string(), wp55_b64(b"sleep 100\n"))]
        });
        let params = format!(
            r#"{{"selector":"COM0","cmd":"{command}","source":"{raw}","timeout_s":10{extra}}}"#
        );
        let response = r.call("command.submit", &params);
        assert_eq!(
            response.get("ok"),
            Some(&PyValue::Bool(true)),
            "{case}：quiet 前提交應通過：{}",
            j(&response)
        );
        let cmd_id = s(&response, "cmd_id").to_string();
        assert_eq!(
            j(&response),
            wp51_submit_success_response(
                &r,
                &cmd_id,
                normalized.map(|(source, _)| source),
                normalized.map(|(_, from)| from),
                source_class,
            ),
            "{case}：成功回應"
        );
        let queued = sub(
            &r.call("command.get", &format!(r#"{{"cmd_id":"{cmd_id}"}}"#)),
            "command",
        );
        assert_eq!(s(&queued, "status"), "accepted", "{case}：{}", j(&queued));
        assert_eq!(
            queued.get("started_at"),
            Some(&PyValue::Null),
            "{case}：前景佔用中不得出列"
        );
        assert_eq!(
            r.mgr.session_queue_lens("COM0").map(|(len, _)| len),
            Some(1),
            "{case}：待測命令在佇列中"
        );

        wp55_arm_quiet(&r);
        let tx_mark = dev.tx_len();
        // 前景以 prompt 正常結束（OpRunner 比對到 prompt → CommandResult），待測命令隨即出列。
        dev.push_rx(b"\r\nroot@t:/# ");
        let held = wp55_wait_done(&r, &hold);
        assert_eq!(
            s(&held, "status"),
            "done",
            "{case}：{} hold TX={:?} UART={:?}",
            j(&held),
            wp55_wal_tx(&r, &hold),
            String::from_utf8_lossy(&dev.tx_since(0))
        );
        let expected_source = normalized.map_or(raw, |(source, _)| source);
        if trusted {
            assert_wp55_sent_and_done(&r, &case, &cmd_id, command, expected_source, tx_mark);
        } else {
            let record = wp55_wait_done(&r, &cmd_id);
            assert_eq!(s(&record, "status"), "error", "{case}：{}", j(&record));
            assert_eq!(
                s(&record, "error_code"),
                "AUTOBOOT_QUIET",
                "{case}：{}",
                j(&record)
            );
            let tx = dev.tx_since(tx_mark);
            assert!(
                !String::from_utf8_lossy(&tx).contains(command),
                "{case}：被 gate 擋下的命令不得寫出 TX：{:?}",
                String::from_utf8_lossy(&tx)
            );
            assert!(
                wp55_wal_tx(&r, &cmd_id).is_empty(),
                "{case}：WAL 不得有被擋命令的 TX"
            );
        }
        assert_eq!(
            r.mgr.session_queue_lens("COM0").map(|(len, _)| len),
            Some(0),
            "{case}：佇列已清空"
        );
    }
}

fn wp51_source_reserved_response(r: &ServiceRig, raw_source: &str) -> String {
    wp51_reserved_source_response(r, "source", raw_source)
}

fn wp51_reserved_source_response(r: &ServiceRig, field: &str, raw_source: &str) -> String {
    let hint = "`system` 與 `system:*` 是 daemon 內部保留的來源前綴（WAL 歸因與內部恢復旗標），外部呼叫不可使用。自動化請用 `agent:<名>`，人工操作請用 `human:<名>`；逾時行為差異見 skill「`--source` 的語意」。";
    j(&sw_proto::pydict! {
        "ok" => false,
        "error_code" => "SOURCE_RESERVED",
        "field" => field,
        "value" => raw_source,
        "reserved_prefix" => "system",
        "hint" => hint,
        "version" => r.version.clone(),
    })
}

#[test]
fn wp51_submit_reserved_source_rejected_before_queue() {
    let r = ServiceRig::ready_shell();
    let session_id = r.mgr.resolve("COM0").expect("COM0 session");

    for raw_source in [
        "system:recover",
        "system",
        "SYSTEM:self_test",
        "system :recover",
        "ſystem:recover",
    ] {
        let before_effects = r.side_effect_fingerprint();
        let before_registry = r.registry.len();
        let before_pending = r.registry.pending(&session_id);
        let before_queue = r.mgr.session_queue_lens("COM0").expect("COM0 佇列");
        let params =
            format!(r#"{{"selector":"COM0","cmd":"echo source-policy","source":"{raw_source}"}}"#);

        let response = r.call("command.submit", &params);

        assert_eq!(
            j(&response),
            wp51_source_reserved_response(&r, raw_source),
            "保留來源必須原樣拒絕：{raw_source}"
        );
        assert_eq!(r.side_effect_fingerprint(), before_effects, "{raw_source}");
        assert_eq!(
            r.registry.len(),
            before_registry,
            "不得建立命令紀錄：{raw_source}"
        );
        assert_eq!(
            r.registry.pending(&session_id),
            before_pending,
            "不得增加 pending：{raw_source}"
        );
        assert_eq!(
            r.mgr.session_queue_lens("COM0"),
            Some(before_queue),
            "不得進入 session queue：{raw_source}"
        );
    }

    let invalid_completion = r.call(
        "command.submit",
        r#"{"selector":"COM0","cmd":"echo source-policy","completion_regex":"[","source":"system:recover"}"#,
    );
    assert_eq!(
        j(&invalid_completion),
        j(&sw_proto::pydict! {
            "ok" => false,
            "error_code" => "INVALID_COMPLETION_REGEX",
            "version" => r.version.clone(),
        }),
        "completion regex 驗證先於來源驗證"
    );

    let missing_selector = r.call("command.submit", r#"{"source":"system:recover"}"#);
    assert_eq!(
        j(&missing_selector),
        wp51_source_reserved_response(&r, "system:recover"),
        "來源驗證先於 selector admission"
    );

    let invalid_timeout = r
        .call_raw(
            "command.submit",
            r#"{"selector":"COM0","cmd":"echo source-policy","source":"system:recover","timeout_s":"invalid"}"#,
        )
        .expect("來源驗證先於 timeout 轉型");
    assert_eq!(
        j(&invalid_timeout),
        wp51_source_reserved_response(&r, "system:recover"),
        "來源驗證先於 timeout 轉型"
    );
}

fn wp51_submit_success_response(
    r: &ServiceRig,
    cmd_id: &str,
    source: Option<&str>,
    normalized_from: Option<&str>,
    source_class: &str,
) -> String {
    let session_id = r.mgr.resolve("COM0").expect("COM0 session");
    let mut expected = sw_proto::pydict! {
        "ok" => true,
        "cmd_id" => cmd_id,
        "status" => "accepted",
        "session_id" => session_id,
    };
    if let Some(source) = source {
        expected.insert("source", PyValue::from(source));
        expected.insert(
            "source_normalized_from",
            PyValue::from(normalized_from.expect("正規化來源必有原始值")),
        );
    }
    expected.insert("source_class", PyValue::from(source_class));
    expected.insert("version", PyValue::from(r.version.clone()));
    j(&expected)
}

#[test]
fn wp51_submit_success_source_annotations() {
    let r = ServiceRig::ready_shell();
    for (params, source, normalized_from, source_class) in [
        (
            r#"{"selector":"COM0","cmd":"echo source-policy","source":"human"}"#,
            Some("human:cli"),
            Some("human"),
            "human",
        ),
        (
            r#"{"selector":"COM0","cmd":"echo source-policy","source":"human:"}"#,
            Some("human:cli"),
            Some("human:"),
            "human",
        ),
        (
            r#"{"selector":"COM0","cmd":"echo source-policy","source":"human:cli"}"#,
            None,
            None,
            "human",
        ),
        (
            r#"{"selector":"COM0","cmd":"echo source-policy"}"#,
            None,
            None,
            "agent",
        ),
        (
            r#"{"selector":"COM0","cmd":"echo source-policy","source":"vendor:sync"}"#,
            None,
            None,
            "agent",
        ),
        (
            r#"{"selector":"COM0","cmd":"echo source-policy","source":"Human:Bob"}"#,
            Some("human:Bob"),
            Some("Human:Bob"),
            "human",
        ),
    ] {
        let response = r.call("command.submit", params);
        assert_eq!(response.get("ok"), Some(&PyValue::Bool(true)), "{}", j(&response));
        let cmd_id = s(&response, "cmd_id").to_string();
        assert_eq!(
            j(&response),
            wp51_submit_success_response(
                &r,
                &cmd_id,
                source,
                normalized_from,
                source_class,
            ),
            "成功回應的來源標註或欄位順序錯誤：{params}"
        );
    }
}

#[test]
fn wp51_submit_failure_has_no_source_annotations() {
    let r = ServiceRig::ready_shell();
    let session = sub(
        &r.call("session.get_state", r#"{"selector":"COM0"}"#),
        "session",
    );
    // 此請求會經 service route → SessionManager actor → SessionCore::submit，並由 CommandRegistry::admit 拒絕。
    let response = r.call(
        "command.submit",
        r#"{"selector":"COM0","cmd":"line1\nline2","source":"human"}"#,
    );
    let expected = sw_proto::pydict! {
        "ok" => false,
        "error_code" => "CMD_CONTAINS_NEWLINE",
        "hint" => "Command must not contain embedded newline characters. Split into multiple independent submissions.",
        "session" => PyValue::Dict(session),
        "version" => r.version.clone(),
    };
    assert_eq!(j(&response), j(&expected));
}

#[test]
fn wp51_submit_normalized_source_reaches_registry() {
    let r = ServiceRig::ready_shell();
    for (params, expected_source) in [
        (
            r#"{"selector":"COM0","cmd":"echo source-policy","source":"human"}"#,
            "human:cli",
        ),
        (
            r#"{"selector":"COM0","cmd":"echo source-policy","source":"human:"}"#,
            "human:cli",
        ),
        (r#"{"selector":"COM0","cmd":"echo source-policy"}"#, "agent"),
        (
            r#"{"selector":"COM0","cmd":"echo source-policy","source":"vendor:sync"}"#,
            "vendor:sync",
        ),
        (
            r#"{"selector":"COM0","cmd":"echo source-policy","source":42}"#,
            "42",
        ),
        (
            r#"{"selector":"COM0","cmd":"echo source-policy","source":0}"#,
            "agent",
        ),
    ] {
        let response = r.call("command.submit", params);
        assert_eq!(
            response.get("ok"),
            Some(&PyValue::Bool(true)),
            "{}",
            j(&response)
        );
        let record = r
            .registry
            .get(s(&response, "cmd_id"))
            .expect("command.submit 必須建命令紀錄");
        assert_eq!(
            record.get("source"),
            Some(&PyValue::from(expected_source)),
            "registry source：{params}"
        );
    }
}

#[test]
fn wp51_source_policy_end_to_end() {
    let r = ServiceRig::ready_shell();
    r.settle();
    let session_id = r.mgr.resolve("COM0").expect("COM0 session");
    let dev = r.device(REAL_A);

    let before_effects = r.side_effect_fingerprint();
    let before_tx = dev.tx_len();
    let before_registry = r.registry.len();
    let before_pending = r.registry.pending(&session_id);
    let before_queue = r.mgr.session_queue_lens("COM0");
    let rejected = r.call(
        "command.submit",
        r#"{"selector":"COM0","cmd":"echo rejected","source":"system:recover"}"#,
    );
    assert_eq!(
        j(&rejected),
        wp51_source_reserved_response(&r, "system:recover")
    );
    r.settle();
    assert_eq!(r.side_effect_fingerprint(), before_effects);
    assert_eq!(dev.tx_len(), before_tx, "保留來源不得送出任何 UART bytes");
    assert_eq!(
        r.registry.len(),
        before_registry,
        "保留來源不得建立命令紀錄"
    );
    assert_eq!(r.registry.pending(&session_id), before_pending);
    assert_eq!(r.mgr.session_queue_lens("COM0"), before_queue);

    let human_command = "wp51-human-normalized";
    let agent_command = "wp51-default-agent";
    let explicit_human_command = "wp51-explicit-human-cli";
    for command in [human_command, agent_command, explicit_human_command] {
        dev.on_tx_reply(format!("{command}\n").as_bytes(), b"\r\nroot@t:/# ");
    }
    let tx_mark = dev.tx_len();

    let response = r.call(
        "command.submit",
        r#"{"selector":"COM0","cmd":"wp51-human-normalized","source":"human"}"#,
    );
    assert_eq!(
        response.get("ok"),
        Some(&PyValue::Bool(true)),
        "{}",
        j(&response)
    );
    let human_id = s(&response, "cmd_id").to_string();
    assert_eq!(
        j(&response),
        wp51_submit_success_response(&r, &human_id, Some("human:cli"), Some("human"), "human")
    );
    wait_until("human 正規化命令完成", || {
        r.registry
            .get(&human_id)
            .and_then(|record| record.get("done_at").cloned())
            .is_some_and(|done_at| !done_at.is_null())
    });
    let tx = dev.tx_since(tx_mark);
    assert!(
        tx.windows(human_command.len())
            .any(|window| window == human_command.as_bytes()),
        "fake UART 未收到命令：{:?}",
        String::from_utf8_lossy(&tx)
    );

    let fetched = r.call("command.get", &format!(r#"{{"cmd_id":"{human_id}"}}"#));
    let human_record = sub(&fetched, "command");
    assert_eq!(s(&human_record, "source"), "human:cli");
    assert_eq!(s(&human_record, "source_class"), "human");
    assert!(
        r.registry
            .get(&human_id)
            .expect("registry 命令紀錄")
            .get("source_class")
            .is_none(),
        "service 顯示分類不得污染 arbiter registry"
    );

    for (command, params, expected_class) in [
        (
            agent_command,
            r#"{"selector":"COM0","cmd":"wp51-default-agent"}"#,
            "agent",
        ),
        (
            explicit_human_command,
            r#"{"selector":"COM0","cmd":"wp51-explicit-human-cli","source":"human:cli"}"#,
            "human",
        ),
    ] {
        let response = r.call("command.submit", params);
        assert_eq!(
            response.get("ok"),
            Some(&PyValue::Bool(true)),
            "{}",
            j(&response)
        );
        let cmd_id = s(&response, "cmd_id").to_string();
        let normalized = expected_class == "human";
        assert_eq!(
            j(&response),
            wp51_submit_success_response(&r, &cmd_id, None, None, expected_class,),
            "提交來源分類：{command}"
        );
        wait_until(&format!("{command} 完成"), || {
            r.registry
                .get(&cmd_id)
                .and_then(|record| record.get("done_at").cloned())
                .is_some_and(|done_at| !done_at.is_null())
        });
        let fetched = r.call("command.get", &format!(r#"{{"cmd_id":"{cmd_id}"}}"#));
        let record = sub(&fetched, "command");
        assert_eq!(s(&record, "source_class"), expected_class);
        assert_eq!(
            s(&record, "source"),
            if normalized { "human:cli" } else { "agent" }
        );
        assert!(r
            .registry
            .get(&cmd_id)
            .unwrap()
            .get("source_class")
            .is_none());
    }

    assert_eq!(
        j(&r.call("command.get", r#"{"cmd_id":"nope"}"#)),
        format!(
            "{{\"ok\":false,\"error_code\":\"CMD_NOT_FOUND\",\"cmd_id\":\"nope\",\"version\":\"{}\"}}",
            r.version
        )
    );

    let r = ServiceRig::ready_shell();
    r.settle();
    let session_id = r.mgr.resolve("COM0").expect("COM0 session");
    for (method, params, field, raw_source) in [
        (
            "device.release",
            r#"{"selector":"COM0","source":"system:recover"}"#,
            "source",
            "system:recover",
        ),
        (
            "session.interactive_open",
            r#"{"selector":"COM0","owner":"system:x"}"#,
            "owner",
            "system:x",
        ),
    ] {
        let before_effects = r.side_effect_fingerprint();
        let before_tx = r.device(REAL_A).tx_len();
        let before_registry = r.registry.len();
        let before_pending = r.registry.pending(&session_id);
        let before_queue = r.mgr.session_queue_lens("COM0");
        let response = r.call(method, params);
        r.settle();
        assert_eq!(
            j(&response),
            wp51_reserved_source_response(&r, field, raw_source)
        );
        assert_eq!(r.side_effect_fingerprint(), before_effects, "{method}");
        assert_eq!(r.device(REAL_A).tx_len(), before_tx, "{method} 零 UART TX");
        assert_eq!(r.registry.len(), before_registry, "{method} 不建立 command");
        assert_eq!(r.registry.pending(&session_id), before_pending, "{method}");
        assert_eq!(r.mgr.session_queue_lens("COM0"), before_queue, "{method}");
    }

    let opened = r.call(
        "session.interactive_open",
        r#"{"selector":"COM0","owner":"human"}"#,
    );
    let interactive_id = s(&opened, "interactive_id").to_string();
    let status = r.call(
        "session.interactive_status",
        &format!(r#"{{"interactive_id":"{interactive_id}"}}"#),
    );
    assert_eq!(s(&status, "owner"), "human", "interactive owner 不正規化");
    assert_eq!(
        r.call(
            "session.interactive_close",
            &format!(r#"{{"interactive_id":"{interactive_id}"}}"#)
        )
        .get("ok"),
        Some(&PyValue::Bool(true))
    );
    let released = r.call("device.release", r#"{"selector":"COM0","source":"human"}"#);
    assert_eq!(
        released.get("ok"),
        Some(&PyValue::Bool(true)),
        "{}",
        j(&released)
    );
    assert_eq!(s(&released, "source"), "human:cli");
    assert_eq!(s(&released, "source_normalized_from"), "human");
}

// WP6-1 第 1 項：RPC bare human 逾時升格的 ServiceRig 規格（OD-74、rulings s2 D5）。
// 呼叫路徑：Service::rpc(command.submit) 以原始 source 填 SubmitReq.trusted_human → SessionManager route →
// SessionCore::submit／queue → OpRunner 送命令並逾時 → SessionCore 結案與 lease → command.get／interactive_status；
// FakeDevice 與 WAL 同時檢查命令和任何不帶 cmd_id 的救援 TX。

fn wp61_decode_b64(value: &str) -> Vec<u8> {
    fn digit(byte: u8) -> u8 {
        match byte {
            b'A'..=b'Z' => byte - b'A',
            b'a'..=b'z' => byte - b'a' + 26,
            b'0'..=b'9' => byte - b'0' + 52,
            b'+' => 62,
            b'/' => 63,
            _ => panic!("WAL payload_b64 字元無效：{byte}"),
        }
    }

    let mut decoded = Vec::new();
    for chunk in value.as_bytes().chunks_exact(4) {
        let a = digit(chunk[0]);
        let b = digit(chunk[1]);
        let c = if chunk[2] == b'=' { 0 } else { digit(chunk[2]) };
        let d = if chunk[3] == b'=' { 0 } else { digit(chunk[3]) };
        decoded.push((a << 2) | (b >> 4));
        if chunk[2] != b'=' {
            decoded.push((b << 4) | (c >> 2));
        }
        if chunk[3] != b'=' {
            decoded.push((c << 6) | d);
        }
    }
    assert_eq!(value.len() % 4, 0, "WAL payload_b64 長度無效：{value}");
    decoded
}

/// 讀取指定 WAL 序號之後的全部 TX，不以命令 cmd_id 過濾救援鍵。
fn wp61_wal_tx_since(r: &ServiceRig, after_seq: u64) -> Vec<(String, Vec<u8>)> {
    let bytes = r.wfs.contents(WP55_WAL.as_ref()).unwrap_or_default();
    String::from_utf8(bytes)
        .expect("WAL 應為 UTF-8")
        .lines()
        .filter_map(|line| {
            let PyValue::Dict(record) = sw_proto::pyjson::loads(line).expect("WAL 行應為 JSON")
            else {
                panic!("WAL 行應為 object：{line}");
            };
            let seq = record
                .get("seq")
                .and_then(PyValue::as_i64)
                .expect("WAL seq 應為整數");
            (seq > after_seq as i64 && record.get("dir") == Some(&PyValue::from("TX"))).then(|| {
                (
                    s(&record, "source").to_string(),
                    wp61_decode_b64(s(&record, "payload_b64")),
                )
            })
        })
        .collect()
}

struct Wp61TimeoutEvidence {
    cmd_id: String,
    record: PyDict,
    uart_tx: Vec<u8>,
    wal_tx: Vec<(String, Vec<u8>)>,
}

/// 先確認命令已寫上假 UART，再以小步 FakeClock 推進到終態；每步留出 actor 的真實排程時間。
fn wp61_submit_timeout(
    r: &ServiceRig,
    raw_source: &str,
    command: &str,
    annotation: Option<(&str, &str)>,
    source_class: &str,
    reply_on_ctrl_c: bool,
) -> Wp61TimeoutEvidence {
    wp61_submit_timeout_with(
        r,
        raw_source,
        "",
        command,
        annotation,
        source_class,
        reply_on_ctrl_c,
    )
}

/// 同 `wp61_submit_timeout`，另把 `extra_params`（以逗號開頭的 JSON 片段）原樣接在 `command.submit` 參數尾端。
fn wp61_submit_timeout_with(
    r: &ServiceRig,
    raw_source: &str,
    extra_params: &str,
    command: &str,
    annotation: Option<(&str, &str)>,
    source_class: &str,
    reply_on_ctrl_c: bool,
) -> Wp61TimeoutEvidence {
    let dev = r.device(REAL_A);
    r.settle();
    if reply_on_ctrl_c {
        dev.on_tx_reply(b"\x03", b"\r\nroot@t:/# ");
    }
    let tx_mark = dev.tx_len();
    let wal_seq = r.wal.current_seq();
    let params = format!(
        r#"{{"selector":"COM0","cmd":"{command}","source":"{raw_source}","timeout_s":1{extra_params}}}"#
    );
    let response = r.call("command.submit", &params);
    assert_eq!(
        response.get("ok"),
        Some(&PyValue::Bool(true)),
        "{raw_source}：{}",
        j(&response)
    );
    let cmd_id = s(&response, "cmd_id").to_string();
    assert_eq!(
        j(&response),
        wp51_submit_success_response(
            r,
            &cmd_id,
            annotation.map(|(source, _)| source),
            annotation.map(|(_, from)| from),
            source_class,
        ),
        "{raw_source}：成功回應"
    );
    let command_line = format!("{command}\n");
    wait_until(&format!("{raw_source} 命令 TX"), || {
        dev.tx_since(tx_mark).starts_with(command_line.as_bytes())
    });

    let deadline = std::time::Instant::now() + Duration::from_secs(8);
    let record = loop {
        let record = sub(
            &r.call("command.get", &format!(r#"{{"cmd_id":"{cmd_id}"}}"#)),
            "command",
        );
        if record
            .get("done_at")
            .is_some_and(|done_at| !done_at.is_null())
        {
            break record;
        }
        assert!(
            std::time::Instant::now() < deadline,
            "等待 {raw_source} 命令逾時終態：{}",
            j(&record)
        );
        r.clock.advance(Duration::from_millis(100));
        std::thread::sleep(Duration::from_millis(80));
    };

    Wp61TimeoutEvidence {
        cmd_id,
        record,
        uart_tx: dev.tx_since(tx_mark),
        wal_tx: wp61_wal_tx_since(r, wal_seq),
    }
}

#[test]
fn wp61_rpc_bare_human_timeout_promotes_without_keys() {
    let mut outcomes = Vec::new();
    for raw_source in ["human", "human:"] {
        // 每個原始來源各用新 rig，避免 lease 或 UART / WAL 狀態互相影響。
        let r = ServiceRig::ready_shell();
        let evidence = wp61_submit_timeout(
            &r,
            raw_source,
            "vim notes.txt",
            Some(("human:cli", raw_source)),
            "human",
            false,
        );
        outcomes.push((raw_source, evidence, r));
    }

    for (raw_source, evidence, r) in outcomes {
        assert_eq!(
            s(&evidence.record, "status"),
            "interactive",
            "{}",
            j(&evidence.record)
        );
        assert_eq!(
            s(&evidence.record, "execution_mode"),
            "interactive",
            "{}",
            j(&evidence.record)
        );
        assert_eq!(evidence.record.get("partial"), Some(&PyValue::Bool(true)));
        assert_eq!(s(&evidence.record, "stdout"), "");
        assert_eq!(
            s(&evidence.record, "recovery_action"),
            "PROMOTE_HUMAN_INTERACTIVE",
            "{}",
            j(&evidence.record)
        );
        let interactive_id = s(&evidence.record, "interactive_session_id");
        let lease = r.call(
            "session.interactive_status",
            &format!(r#"{{"interactive_id":"{interactive_id}"}}"#),
        );
        assert_eq!(s(&lease, "owner"), "human:cli", "{raw_source} lease owner");
        let public = r.call(
            "session.self_test",
            r#"{"selector":"COM0","strict_human_lock":true}"#,
        );
        assert_eq!(
            public.get("interactive_owner"),
            Some(&PyValue::from("human:cli")),
            "{raw_source} 公開 interactive_owner：{}",
            j(&public)
        );
        let command_line = b"vim notes.txt\n";
        assert_eq!(
            evidence.uart_tx, command_line,
            "{raw_source} UART TX 不得含救援鍵"
        );
        let wal_tx: Vec<u8> = evidence
            .wal_tx
            .iter()
            .flat_map(|(_, payload)| payload.iter().copied())
            .collect();
        assert_eq!(
            wal_tx, command_line,
            "{raw_source} WAL 所有 TX（含無 cmd_id 的救援 TX）不得含 CTRL-C／D；cmd_id={}",
            evidence.cmd_id
        );
    }
}

/// 驗證明確 agent 來源在逾時後走 CTRL-C，且救援 TX 以 system:recover WAL 記錄。
fn assert_wp61_ctrl_c_recovery(
    evidence: &Wp61TimeoutEvidence,
    public: &PyDict,
    command: &str,
    source_class: &str,
) {
    assert_eq!(
        s(&evidence.record, "status"),
        "done",
        "{}",
        j(&evidence.record)
    );
    assert_eq!(
        s(&evidence.record, "recovery_action"),
        "CTRL_C",
        "{}",
        j(&evidence.record)
    );
    assert_eq!(s(&evidence.record, "source_class"), source_class);
    assert_eq!(
        public.get("interactive_owner"),
        Some(&PyValue::Null),
        "不可升格 lease：{}",
        j(public)
    );
    let expected = [command.as_bytes(), b"\n", b"\x03"].concat();
    assert_eq!(evidence.uart_tx, expected, "UART 僅送命令及 CTRL-C");
    let wal_tx: Vec<u8> = evidence
        .wal_tx
        .iter()
        .flat_map(|(_, payload)| payload.iter().copied())
        .collect();
    assert_eq!(wal_tx, expected, "WAL 全部 TX（含無 cmd_id 的救援 TX）");
    assert!(
        evidence
            .wal_tx
            .iter()
            .any(|(source, payload)| source == "system:recover" && payload == b"\x03"),
        "WAL 必須包含未依命令 cmd_id 篩選的 system:recover CTRL-C：{:?}",
        evidence.wal_tx
    );
    assert!(!wal_tx.contains(&0x04), "WAL 不得含 CTRL-D");
}

#[test]
fn wp61_rpc_explicit_human_cli_timeout_uses_agent_recovery() {
    for raw_source in ["human:cli", "human:x"] {
        let r = ServiceRig::ready_shell();
        let command = "printf 'broken";
        let evidence = wp61_submit_timeout(&r, raw_source, command, None, "human", true);
        let public = r.call(
            "session.self_test",
            r#"{"selector":"COM0","strict_human_lock":true}"#,
        );
        assert_wp61_ctrl_c_recovery(&evidence, &public, command, "human");
    }
}

#[test]
fn wp61_rpc_unknown_prefix_timeout_uses_agent_recovery() {
    let r = ServiceRig::ready_shell();
    let command = "printf 'broken";
    let evidence = wp61_submit_timeout(&r, "codex", command, None, "agent", true);
    let public = r.call(
        "session.self_test",
        r#"{"selector":"COM0","strict_human_lock":true}"#,
    );
    assert_wp61_ctrl_c_recovery(&evidence, &public, command, "agent");
}

/// WP6-1 第 4 項：RPC 參數夾帶 `trusted_human:true` 不得替明寫 `human:<x>` 授權。
/// Python `command.submit` 只取 `source_check.is_bare_human`（`origin/main:sw_core/service.py:1235`），
/// 從不讀 `params["trusted_human"]`；Rust `service.rs` 同樣只由原始 source 判定，夾帶欄位應被忽略：
/// 成功回應逐位元組同未夾帶者、逾時走 CTRL-C 救援、不升格 lease，命令紀錄也不外洩該欄位。
#[test]
fn wp61_rpc_injected_trusted_human_param_cannot_authorize_explicit_human() {
    for raw_source in ["human:cli", "human:x"] {
        let r = ServiceRig::ready_shell();
        let command = "printf 'broken";
        let evidence = wp61_submit_timeout_with(
            &r,
            raw_source,
            r#","trusted_human":true"#,
            command,
            None,
            "human",
            true,
        );
        let public = r.call(
            "session.self_test",
            r#"{"selector":"COM0","strict_human_lock":true}"#,
        );
        assert_wp61_ctrl_c_recovery(&evidence, &public, command, "human");
        assert_eq!(
            s(&evidence.record, "source"),
            raw_source,
            "{raw_source}：來源不得被改寫"
        );
        assert_eq!(
            evidence.record.get("interactive_session_id"),
            Some(&PyValue::Null),
            "{raw_source}：不得開 lease：{}",
            j(&evidence.record)
        );
        assert!(
            evidence.record.get("trusted_human").is_none(),
            "{raw_source}：命令紀錄不得出現 trusted_human：{}",
            j(&evidence.record)
        );
    }
}

#[test]
fn wp61_rpc_system_recover_rejected_without_effects() {
    let r = ServiceRig::ready_shell();
    r.settle();
    let dev = r.device(REAL_A);
    let session_id = r.mgr.resolve("COM0").expect("COM0 session");
    let before_effects = r.side_effect_fingerprint();
    let before_session = r.session("COM0").expect("session 前態");
    let before_registry = r.registry.len();
    let before_pending = r.registry.pending(&session_id);
    let before_queue = r.mgr.session_queue_lens("COM0");
    let before_uart = dev.tx_since(0);
    let before_wal = r.wfs.contents(WP55_WAL.as_ref()).expect("WAL 初始內容");
    let before_open_attempts = dev.open_attempts();

    let response = r.call(
        "command.submit",
        r#"{"selector":"COM0","cmd":"echo source-policy","source":"system:recover"}"#,
    );
    r.settle();

    assert_eq!(
        j(&response),
        wp51_source_reserved_response(&r, "system:recover"),
        "保留來源必須逐位元組回覆 SOURCE_RESERVED"
    );
    assert_eq!(
        r.side_effect_fingerprint(),
        before_effects,
        "WAL／state／UART 副作用"
    );
    assert_eq!(r.session("COM0").expect("session 後態"), before_session);
    assert_eq!(r.registry.len(), before_registry, "不得增加 registry 紀錄");
    assert_eq!(
        r.registry.pending(&session_id),
        before_pending,
        "不得增加 pending"
    );
    assert_eq!(
        r.mgr.session_queue_lens("COM0"),
        before_queue,
        "不得增加 queue"
    );
    assert_eq!(dev.tx_since(0), before_uart, "不得新增 UART TX");
    assert_eq!(
        r.wfs.contents(WP55_WAL.as_ref()).expect("WAL 後態"),
        before_wal,
        "不得新增 WAL 記錄"
    );
    assert_eq!(dev.open_attempts(), before_open_attempts, "不得重開裝置");
}

#[test]
fn submit_bad_timeout_raises_before_selector_check() {
    let r = ServiceRig::ready_shell();
    let e = r.call_err(
        "command.submit",
        r#"{"selector":"COM0","cmd":"ls","timeout_s":"abc"}"#,
    );
    assert_eq!(e.message, "could not convert string to float: 'abc'");
    // 轉型在 selector 解析之前：查無 selector 仍先拋例外。
    let e = r.call_err(
        "command.submit",
        r#"{"selector":"NOSUCH","cmd":"ls","timeout_s":"abc"}"#,
    );
    assert_eq!(e.message, "could not convert string to float: 'abc'");
    // 空 selector 亦同（INVALID_ARGS 檢查在轉型之後）。
    r.call_err("command.submit", r#"{"cmd":"ls","timeout_s":"abc"}"#);
}

#[test]
fn submit_without_selector_invalid_args() {
    let r = ServiceRig::ready_shell();
    assert_eq!(
        j(&r.call("command.submit", r#"{"cmd":"ls"}"#)),
        format!(
            "{{\"ok\":false,\"error_code\":\"INVALID_ARGS\",\"version\":\"{}\"}}",
            r.version
        )
    );
}

#[test]
fn submit_expected_duration_zero_is_float_not_none() {
    let r = ServiceRig::ready_shell();
    let resp = r.call(
        "command.submit",
        r#"{"com":"COM0","command":"ls","expected_duration_s":0}"#,
    );
    assert_eq!(resp.get("ok"), Some(&PyValue::Bool(true)), "{}", j(&resp));
    let rec = r.registry.get(s(&resp, "cmd_id")).expect("命令紀錄");
    assert_eq!(rec.get("expected_duration_s"), Some(&PyValue::Float(0.0)));
    assert_eq!(
        rec.get("command"),
        Some(&PyValue::from("ls")),
        "cmd → command 參數鏈"
    );
}

#[test]
fn submit_unknown_selector_session_not_found() {
    let r = ServiceRig::ready_shell();
    assert_eq!(
        j(&r.call("command.submit", r#"{"selector":"NOSUCH","cmd":"x"}"#)),
        format!(
            "{{\"ok\":false,\"error_code\":\"SESSION_NOT_FOUND\",\"selector\":\"NOSUCH\",\"version\":\"{}\"}}",
            r.version
        )
    );
}

#[test]
fn submit_empty_completion_regex_rejected_before_selector() {
    let r = ServiceRig::ready_shell();
    let expected = format!(
        "{{\"ok\":false,\"error_code\":\"INVALID_COMPLETION_REGEX\",\"version\":\"{}\"}}",
        r.version
    );
    assert_eq!(
        j(&r.call(
            "command.submit",
            r#"{"selector":"COM0","cmd":"x","completion_regex":""}"#
        )),
        expected
    );
    assert_eq!(
        j(&r.call(
            "command.submit",
            r#"{"selector":"NOSUCH","cmd":"x","completion_regex":""}"#
        )),
        expected,
        "selector 解析之前"
    );
}

#[test]
fn get_state_empty_selector_session_not_found() {
    let r = ServiceRig::ready_shell();
    assert_eq!(
        j(&r.call("session.get_state", "{}")),
        format!(
            "{{\"ok\":false,\"error_code\":\"SESSION_NOT_FOUND\",\"selector\":\"\",\"version\":\"{}\"}}",
            r.version
        )
    );
}

#[test]
fn activity_without_selector_invalid_args() {
    let r = ServiceRig::ready_shell();
    assert_eq!(
        j(&r.call("session.activity", "{}")),
        format!(
            "{{\"ok\":false,\"error_code\":\"INVALID_ARGS\",\"version\":\"{}\"}}",
            r.version
        )
    );
}

#[test]
fn activity_selector_chain_session_id() {
    let r = ServiceRig::ready_shell();
    for params in [
        r#"{"session_id":"t:COM0"}"#,
        r#"{"com":"COM0"}"#,
        r#"{"selector":"","session_id":"t:COM0"}"#,
    ] {
        let resp = r.call("session.activity", params);
        assert_eq!(
            keys(&resp),
            ["ok", "session", "version"],
            "Python 回 get_session_state 形狀：{params}"
        );
        assert_eq!(s(&sub(&resp, "session"), "com"), "COM0");
    }
}

#[test]
fn device_release_reason_is_raw_json() {
    let r = ServiceRig::ready_shell();
    let resp = r.call("device.release", r#"{"selector":"COM0","reason":{"k":1}}"#);
    assert_eq!(resp.get("ok"), Some(&PyValue::Bool(true)), "{}", j(&resp));
    let session = r.session("COM0").expect("session");
    assert_eq!(s(&session, "state"), "RELEASED");
    assert_eq!(jv(session.get("released_reason").unwrap()), "{\"k\":1}");
    assert_eq!(s(&session, "released_by"), "cli", "source 預設 cli");
}

#[test]
fn wp51_release_source_policy() {
    let r = ServiceRig::ready_shell();
    r.settle();
    let session_id = r.mgr.resolve("COM0").expect("COM0 session");

    for raw_source in ["system", "system:recover"] {
        let before_effects = r.side_effect_fingerprint();
        let before_session = r.session("COM0").expect("session 前態");
        let before_registry = r.registry.len();
        let before_pending = r.registry.pending(&session_id);
        let before_queue = r.mgr.session_queue_lens("COM0");
        let params = format!(r#"{{"selector":"COM0","source":"{raw_source}"}}"#);

        let response = r.call("device.release", &params);
        r.settle();

        assert_eq!(
            j(&response),
            wp51_source_reserved_response(&r, raw_source),
            "release 必須在 session route 前拒絕：{raw_source}"
        );
        assert_eq!(r.side_effect_fingerprint(), before_effects, "{raw_source}");
        assert_eq!(r.session("COM0").expect("session 後態"), before_session);
        assert_eq!(r.registry.len(), before_registry, "registry：{raw_source}");
        assert_eq!(
            r.registry.pending(&session_id),
            before_pending,
            "pending：{raw_source}"
        );
        assert_eq!(
            r.mgr.session_queue_lens("COM0"),
            before_queue,
            "queue：{raw_source}"
        );
    }

    let missing_selector = r.call(
        "device.release",
        r#"{"selector":"","source":"system:recover"}"#,
    );
    assert_eq!(
        j(&missing_selector),
        format!(
            "{{\"ok\":false,\"error_code\":\"INVALID_ARGS\",\"version\":\"{}\"}}",
            r.version
        ),
        "release 空 selector 的 INVALID_ARGS 須先於來源檢查"
    );

    let r = ServiceRig::ready_shell();
    let response = r.call(
        "device.release",
        r#"{"selector":"COM0","source":"human","reason":[null,3,true,{"k":[2,"x"]}]}"#,
    );
    assert_eq!(
        response.get("ok"),
        Some(&PyValue::Bool(true)),
        "{}",
        j(&response)
    );
    assert_eq!(
        keys(&response),
        [
            "ok",
            "session",
            "closed_consoles",
            "aborted_cmd",
            "source",
            "source_normalized_from",
            "version"
        ]
    );
    assert_eq!(s(&response, "source"), "human:cli");
    assert_eq!(s(&response, "source_normalized_from"), "human");
    assert!(response.get("source_class").is_none());
    let released = sub(&response, "session");
    assert_eq!(s(&released, "state"), "RELEASED");
    assert_eq!(s(&released, "released_by"), "human:cli");
    assert_eq!(
        jv(released.get("released_reason").expect("reason")),
        r#"[null,3,true,{"k":[2,"x"]}]"#
    );

    let r = ServiceRig::ready_shell();
    let response = r.call("device.release", r#"{"selector":"COM0","reason":false}"#);
    assert_eq!(
        response.get("ok"),
        Some(&PyValue::Bool(true)),
        "{}",
        j(&response)
    );
    assert!(response.get("source").is_none());
    assert!(response.get("source_normalized_from").is_none());
    assert!(response.get("source_class").is_none());
    let released = r.session("COM0").expect("released session");
    assert_eq!(s(&released, "released_by"), "cli", "省略來源預設為 cli");
    assert_eq!(
        jv(released.get("released_reason").expect("reason")),
        "false",
        "任意 JSON reason 原樣保存"
    );
}

#[test]
fn device_release_and_attach_require_selector() {
    let r = ServiceRig::ready_shell();
    let expected = format!(
        "{{\"ok\":false,\"error_code\":\"INVALID_ARGS\",\"version\":\"{}\"}}",
        r.version
    );
    // device.* 只認 `selector`（不走 session_id／com／alias 鏈）。
    assert_eq!(j(&r.call("device.release", r#"{"com":"COM0"}"#)), expected);
    assert_eq!(j(&r.call("device.attach", r#"{"com":"COM0"}"#)), expected);
}

#[test]
fn device_attach_force_string_false_is_truthy() {
    let r = ServiceRig::ready_shell();
    let resp = r.call("device.release", r#"{"selector":"COM0"}"#);
    assert_eq!(resp.get("ok"), Some(&PyValue::Bool(true)), "{}", j(&resp));
    r.excl.hold(REAL_A, vec![999]);
    // force=false（bool）→ 前置 holder 檢查生效 → 不得成功。
    let blocked = r.call("device.attach", r#"{"selector":"COM0","force":false}"#);
    assert_eq!(
        blocked.get("ok"),
        Some(&PyValue::Bool(false)),
        "{}",
        j(&blocked)
    );
    r.settle();
    // RPC-H-06 KEEP：字串 "false" 以 truthiness 解為 true → 略過前置檢查 → 成功。
    let forced = r.call("device.attach", r#"{"selector":"COM0","force":"false"}"#);
    assert_eq!(
        forced.get("ok"),
        Some(&PyValue::Bool(true)),
        "{}",
        j(&forced)
    );
}

#[test]
fn self_test_strict_human_lock_yes_is_coerced_true() {
    let r = ServiceRig::ready_shell();
    let open = r.call(
        "session.interactive_open",
        r#"{"selector":"COM0","owner":"human:tester"}"#,
    );
    assert_eq!(open.get("ok"), Some(&PyValue::Bool(true)), "{}", j(&open));
    let strict = r.call(
        "session.self_test",
        r#"{"selector":"COM0","strict_human_lock":"yes"}"#,
    );
    assert_eq!(
        s(&strict, "classification"),
        "HUMAN_INTERACTIVE_ACTIVE",
        "{}",
        j(&strict)
    );
    let lax = r.call(
        "session.self_test",
        r#"{"selector":"COM0","strict_human_lock":"maybe"}"#,
    );
    assert_ne!(
        s(&lax, "classification"),
        "HUMAN_INTERACTIVE_ACTIVE",
        "非真值字串 → false：{}",
        j(&lax)
    );
}

#[test]
fn self_test_unknown_selector_has_lease_context_and_command_capable() {
    let r = ServiceRig::ready_shell();
    assert_eq!(
        j(&r.call("session.self_test", r#"{"selector":"NOSUCH"}"#)),
        format!(
            "{{\"ok\":false,\"error_code\":\"SESSION_NOT_FOUND\",\"selector\":\"NOSUCH\",\"interactive_owner\":null,\"human_attached\":false,\"human_active\":false,\"recovery_mode\":false,\"command_capable\":false,\"version\":\"{}\"}}",
            r.version
        )
    );
}

#[test]
fn log_tail_raw_empty_from_seq_invalid_args() {
    let r = ServiceRig::ready_shell();
    assert_eq!(
        j(&r.call("log.tail_raw", r#"{"selector":"COM0","from_seq":""}"#)),
        format!(
            "{{\"ok\":false,\"error_code\":\"INVALID_ARGS\",\"version\":\"{}\"}}",
            r.version
        )
    );
    assert_eq!(
        j(&r.call("log.tail_raw", r#"{"selector":"COM0","from_seq":[]}"#)),
        format!(
            "{{\"ok\":false,\"error_code\":\"INVALID_ARGS\",\"version\":\"{}\"}}",
            r.version
        ),
        "TypeError 亦回 INVALID_ARGS"
    );
}

#[test]
fn log_tail_raw_latest_mode_keys() {
    let r = ServiceRig::ready_shell();
    let resp = r.call("log.tail_raw", r#"{"com":"COM0"}"#);
    assert_eq!(
        keys(&resp),
        [
            "ok",
            "records",
            "returned",
            "from_seq",
            "last_seq",
            "current_seq",
            "truncated",
            "wal_path",
            "wal_file_exists",
            "version"
        ]
    );
    assert_eq!(resp.get("from_seq"), Some(&PyValue::Null));
    assert_eq!(
        resp.get("current_seq"),
        Some(&PyValue::from(r.wal.current_seq() as i64))
    );
    let records = resp
        .get("records")
        .and_then(PyValue::as_array)
        .expect("records");
    assert_eq!(
        resp.get("returned"),
        Some(&PyValue::from(records.len() as i64))
    );
    assert!(!records.is_empty(), "READY 過程應有 COM0 的 WAL 紀錄");
    assert_eq!(
        resp.get("last_seq"),
        records
            .last()
            .unwrap()
            .as_dict_ref()
            .and_then(|d| d.get("seq"))
    );
    // 顯式 from_seq 0 → range 模式，回報實際值。
    let range = r.call("log.tail_raw", r#"{"com":"COM0","from_seq":0}"#);
    assert_eq!(range.get("from_seq"), Some(&PyValue::from(0i64)));
}

trait AsDictRef {
    fn as_dict_ref(&self) -> Option<&PyDict>;
}

impl AsDictRef for PyValue {
    fn as_dict_ref(&self) -> Option<&PyDict> {
        match self {
            PyValue::Dict(d) => Some(d),
            _ => None,
        }
    }
}

#[test]
fn log_tail_text_limit_zero_means_200() {
    let r = ServiceRig::ready_shell();
    let zero = r.call("log.tail_text", r#"{"com":"COM0","limit":0}"#);
    let full = r.call("log.tail_text", r#"{"com":"COM0","limit":200}"#);
    assert_eq!(
        keys(&zero),
        [
            "ok",
            "lines",
            "returned",
            "from_seq",
            "last_seq",
            "current_seq",
            "truncated",
            "wal_path",
            "wal_file_exists",
            "version"
        ]
    );
    assert_ne!(
        zero.get("returned"),
        Some(&PyValue::from(0i64)),
        "int(0 or 200) → 200，不是 0"
    );
    assert_eq!(
        jv(zero.get("lines").unwrap()),
        jv(full.get("lines").unwrap())
    );
}

#[test]
fn log_tail_unknown_selector_returns_state_error() {
    let r = ServiceRig::ready_shell();
    assert_eq!(
        j(&r.call("log.tail_raw", r#"{"selector":"NOSUCH"}"#)),
        format!(
            "{{\"ok\":false,\"error_code\":\"SESSION_NOT_FOUND\",\"selector\":\"NOSUCH\",\"version\":\"{}\"}}",
            r.version
        )
    );
}

#[test]
fn wal_range_non_int_from_seq_raises() {
    let r = ServiceRig::ready_shell();
    let e = r.call_err("wal.range", r#"{"from_seq":"x"}"#);
    assert_eq!(
        e.message, "invalid literal for int() with base 10: 'x'",
        "WAL-D13：不是 INVALID_ARGS"
    );
}

#[test]
fn wal_range_keys_without_truncated() {
    let r = ServiceRig::ready_shell();
    let resp = r.call("wal.range", r#"{"from_seq":0,"limit":2}"#);
    assert_eq!(
        keys(&resp),
        [
            "ok",
            "records",
            "wal_path",
            "wal_file_exists",
            "available_from_seq",
            "rotated_out",
            "version"
        ]
    );
    assert_eq!(
        resp.get("records")
            .and_then(PyValue::as_array)
            .map(Vec::len),
        Some(2)
    );
    assert_eq!(resp.get("available_from_seq"), Some(&PyValue::from(1i64)));
    assert_eq!(resp.get("rotated_out"), Some(&PyValue::Bool(false)));
    // to_seq 過濾。
    let one = r.call("wal.range", r#"{"from_seq":0,"to_seq":1}"#);
    assert_eq!(
        one.get("records").and_then(PyValue::as_array).map(Vec::len),
        Some(1)
    );
}

#[test]
fn wal_current_seq_shape() {
    let r = ServiceRig::ready_shell();
    let n = r.wal.current_seq();
    assert!(n > 0);
    assert_eq!(
        j(&r.call("wal.current_seq", "{}")),
        format!("{{\"ok\":true,\"seq\":{n},\"version\":\"{}\"}}", r.version)
    );
}

#[test]
fn wal_reset_shape() {
    let r = ServiceRig::ready_shell();
    let n = r.wal.current_seq();
    let resp = r.call("wal.reset", "{}");
    assert_eq!(
        keys(&resp),
        ["ok", "previous_seq", "rotated_suffix", "version"]
    );
    assert_eq!(resp.get("previous_seq"), Some(&PyValue::from(n as i64)));
    let suffix = s(&resp, "rotated_suffix");
    assert_eq!(suffix.len(), 15, "YYYYmmdd-HHMMSS：{suffix}");
    assert_eq!(&suffix[8..9], "-");
    assert!(
        suffix
            .chars()
            .filter(|c| *c != '-')
            .all(|c| c.is_ascii_digit())
    );
}

#[test]
fn log_tail_raw_wal_missing_shape() {
    let r = ServiceRig::ready_shell();
    let n = r.wal.current_seq();
    assert!(n > 0);
    r.wfs.external_unlink(&r.wal.wal_path());
    let resp = r.call("log.tail_raw", r#"{"com":"COM0"}"#);
    let hint = format!(
        "WAL 已寫過 {n} 筆紀錄，但現行 WAL 檔不存在——稽核紀錄在 daemon 運行期間被移除（常見於外部工具 rmtree WAL 目錄）。消失期間的紀錄無法復原；daemon 會在下一次寫入時自動重建目錄續寫。以 `serialwrap doctor` 的 wal_writable 檢查確認目錄狀態。"
    );
    assert_eq!(
        j(&resp),
        format!(
            "{{\"ok\":false,\"error_code\":\"WAL_MISSING\",\"wal_path\":\"/fake/wal/raw.wal.ndjson\",\"wal_dir\":\"/fake/wal\",\"wal_dir_exists\":true,\"wal_file_exists\":false,\"current_seq\":{n},\"hint\":\"{hint}\",\"version\":\"{}\"}}",
            r.version
        )
    );
    // wal.range 亦檢查；result.tail legacy 路徑不檢查。
    assert_eq!(s(&r.call("wal.range", "{}"), "error_code"), "WAL_MISSING");
    let legacy = r.call("result.tail", r#"{"com":"COM0"}"#);
    assert_eq!(
        j(&legacy),
        format!(
            "{{\"ok\":true,\"records\":[],\"version\":\"{}\"}}",
            r.version
        )
    );
}

#[test]
fn result_tail_without_cmd_id_is_legacy_wal_tail() {
    let r = ServiceRig::ready_shell();
    let resp = r.call("result.tail", r#"{"com":"COM0"}"#);
    assert_eq!(keys(&resp), ["ok", "records", "version"]);
    assert!(
        !resp
            .get("records")
            .and_then(PyValue::as_array)
            .unwrap()
            .is_empty()
    );
    let resp = r.call("result.tail", r#"{"com":"NOSUCH"}"#);
    assert_eq!(s(&resp, "error_code"), "SESSION_NOT_FOUND");
}

#[test]
fn command_result_tail_unknown_and_invalid() {
    let r = ServiceRig::ready_shell();
    assert_eq!(
        j(&r.call("command.result_tail", "{}")),
        format!(
            "{{\"ok\":false,\"error_code\":\"INVALID_ARGS\",\"version\":\"{}\"}}",
            r.version
        )
    );
    assert_eq!(
        j(&r.call("command.result_tail", r#"{"cmd_id":"nope"}"#)),
        format!(
            "{{\"ok\":false,\"error_code\":\"CMD_NOT_FOUND\",\"cmd_id\":\"nope\",\"version\":\"{}\"}}",
            r.version
        )
    );
    assert_eq!(
        j(&r.call("command.get", r#"{"cmd_id":"nope"}"#)),
        format!(
            "{{\"ok\":false,\"error_code\":\"CMD_NOT_FOUND\",\"cmd_id\":\"nope\",\"version\":\"{}\"}}",
            r.version
        )
    );
}

#[test]
fn alias_set_empty_alias_allowed() {
    let r = ServiceRig::ready_shell();
    assert_eq!(
        j(&r.call("alias.set", r#"{"session_id":"t:COM0","alias":""}"#)),
        format!(
            "{{\"ok\":true,\"session_id\":\"t:COM0\",\"alias\":\"\",\"version\":\"{}\"}}",
            r.version
        )
    );
}

#[test]
fn alias_assign_shape() {
    let r = ServiceRig::ready_shell();
    assert_eq!(
        j(&r.call("alias.assign", r#"{"by_id":"b","alias":"a"}"#)),
        format!(
            "{{\"ok\":true,\"alias\":\"a\",\"device_by_id\":\"b\",\"version\":\"{}\"}}",
            r.version
        )
    );
    assert_eq!(
        j(&r.call("alias.assign", r#"{"by_id":"b"}"#)),
        format!(
            "{{\"ok\":false,\"error_code\":\"INVALID_ARGS\",\"version\":\"{}\"}}",
            r.version
        )
    );
}

#[test]
fn mcu_patterns_exact() {
    let r = ServiceRig::ready_shell();
    assert_eq!(
        j(&r.call("mcu.patterns", "{}")),
        format!(
            "{{\"ok\":true,\"patterns\":[{{\"family\":\"ti-cc26xx\",\"probe\":\"55 55\",\"expect\":\"00 cc\",\"baud\":115200}}],\"version\":\"{}\"}}",
            r.version
        )
    );
}

#[test]
fn mcu_status_shape() {
    let r = ServiceRig::ready_shell();
    // shell target 為 command_capable → 不是 flash 候選。
    assert_eq!(
        j(&r.call("mcu.status", "{}")),
        format!(
            "{{\"ok\":true,\"candidates\":[],\"flashing\":false,\"flashing_com\":null,\"last_detect\":null,\"version\":\"{}\"}}",
            r.version
        )
    );
}

#[test]
fn session_list_sorted_by_session_id() {
    let r = ServiceRig::two_shells();
    let resp = r.call("session.list", "{}");
    assert_eq!(keys(&resp), ["ok", "sessions", "version"]);
    let ids: Vec<String> = resp
        .get("sessions")
        .and_then(PyValue::as_array)
        .unwrap()
        .iter()
        .map(|v| v["session_id"].as_str().unwrap().to_string())
        .collect();
    assert_eq!(ids, ["t:COM0", "t:COM1"]);
}

#[test]
fn device_list_sorted_by_by_id() {
    let r = ServiceRig::two_shells();
    let resp = r.call("device.list", "{}");
    assert_eq!(
        jv(resp.get("devices").unwrap()),
        format!(
            "[{{\"by_id\":\"{BY_ID_A}\",\"real_path\":\"{REAL_A}\"}},{{\"by_id\":\"{BY_ID_B}\",\"real_path\":\"{REAL_B}\"}}]"
        )
    );
}

#[test]
fn unknown_method_not_found() {
    let r = ServiceRig::ready_shell();
    assert_eq!(
        j(&r.call("no.such", "{}")),
        format!(
            "{{\"ok\":false,\"error_code\":\"METHOD_NOT_FOUND\",\"method\":\"no.such\",\"version\":\"{}\"}}",
            r.version
        )
    );
    // daemon.stop 不經 Service（framing 攔截）。daemon.handoff_stop 自 P8 Task 14 起由 Service 承接（計畫 09 Task 14
    // 「Rust daemon 承接 SC-11 交接停止」；Python `service.rpc` 直接回 `handoff_stop(params)`，不補 version）：
    // 閒置時回完整的成功形狀 {"ok":true,"stopping":true,"handoff_token":<32 hex>}。
    let h = r.call("daemon.handoff_stop", "{}");
    let token = h.get("handoff_token").and_then(PyValue::as_str).unwrap_or_default().to_string();
    assert!(
        token.len() == 32 && token.bytes().all(|b| b.is_ascii_digit() || (b'a'..=b'f').contains(&b)),
        "handoff_token 須為 32 hex：{}",
        j(&h)
    );
    assert_eq!(
        j(&h),
        format!("{{\"ok\":true,\"stopping\":true,\"handoff_token\":\"{token}\"}}")
    );
}

#[test]
fn health_status_templates_only_with_device_warns_no_profiles() {
    let r = ServiceRig::templates_only(&[(BY_ID_A, REAL_A)]);
    let h = r.call("health.status", "{}");
    assert_eq!(
        jv(h.get("warnings").unwrap()),
        "[\"no_profiles_loaded\"]",
        "RPC-H-17：只看 explicit target"
    );
    let k = keys(&h);
    let w = k.iter().position(|x| *x == "warnings").unwrap();
    assert_eq!(k[w - 1], "limits");
    assert_eq!(k[w + 1], "multi_open");
}

#[test]
fn wp57_status_carries_fingerprint_and_code_path() {
    let r = ServiceRig::ready_shell_with_code_identity(
        Some("0123456789ab"),
        Some("/opt/serialwrap"),
    );
    let resp = r.call("health.status", "{}");
    assert_eq!(
        resp.get("code_fingerprint"),
        Some(&PyValue::from("0123456789ab")),
        "health.status 應帶 manifest 指紋：{}",
        j(&resp)
    );
    assert_eq!(resp.get("code_path"), Some(&PyValue::from("/opt/serialwrap")));
    let k = keys(&resp);
    let version = k.iter().position(|key| *key == "version").unwrap();
    assert_eq!(&k[version..version + 3], ["version", "code_fingerprint", "code_path"]);
}

#[test]
fn wp57_every_rpc_response_carries_available_fingerprint() {
    let r = ServiceRig::ready_shell_with_code_identity(Some("0123456789ab"), Some("/opt/serialwrap"));
    for (method, params) in [("health.ping", "{}"), ("no.such.method", "{}")] {
        let resp = r.call(method, params);
        assert_eq!(
            resp.get("code_fingerprint"),
            Some(&PyValue::from("0123456789ab")),
            "{method} 應帶啟動指紋：{}",
            j(&resp)
        );
        assert_eq!(keys(&resp).last(), Some(&"code_fingerprint"));
    }
}

#[test]
fn wp57_rpc_reuses_startup_fingerprint_without_recomputing() {
    let r = ServiceRig::ready_shell_with_code_identity(Some("startupfp001"), Some("/opt/serialwrap"));
    for _ in 0..3 {
        let ping = r.call("health.ping", "{}");
        let unknown = r.call("no.such.method", "{}");
        assert_eq!(ping.get("code_fingerprint"), Some(&PyValue::from("startupfp001")));
        assert_eq!(unknown.get("code_fingerprint"), Some(&PyValue::from("startupfp001")));
    }
}

#[test]
fn wp57_unknown_fingerprint_is_null_in_status_and_omitted_from_rpc() {
    let r = ServiceRig::ready_shell();
    let status = r.call("health.status", "{}");
    assert_eq!(status.get("code_fingerprint"), Some(&PyValue::Null));
    assert_eq!(status.get("code_path"), Some(&PyValue::from("/fake/bundle")));
    for method in ["health.ping", "no.such.method"] {
        let resp = r.call(method, "{}");
        assert_eq!(resp.get("code_fingerprint"), None, "{method}: {}", j(&resp));
    }
}

#[test]
fn health_status_no_devices_warnings_order() {
    let r = ServiceRig::templates_only(&[]);
    let h = r.call("health.status", "{}");
    assert_eq!(
        jv(h.get("warnings").unwrap()),
        "[\"no_profiles_loaded\",\"no_devices_found\"]"
    );
    // explicit target 但無裝置 → 只有 no_devices_found。
    let r = ServiceRig::build(support::RigSpec {
        yaml: "profiles:\n  t:\n    platform: shell\n    prompt_regex: '(?m)^root@t:.*# '\ntargets:\n  - {com: COM0, profile: t, device_by_id: /s/by-id/usb-A}\n".into(),
        devices: Vec::new(),
        explicit_target_count: 1,
        allowlist: None,
        wait_ready: Vec::new(),
    });
    let h = r.call("health.status", "{}");
    assert_eq!(jv(h.get("warnings").unwrap()), "[\"no_devices_found\"]");
}

#[test]
fn health_status_allowlist_after_multi_open_detail() {
    let r = ServiceRig::with_allowlist(&["COM9"]);
    let h = r.call("health.status", "{}");
    let k = keys(&h);
    assert_eq!(
        k[k.len() - 2..],
        ["multi_open_detail", "test_device_allowlist"]
    );
    assert_eq!(
        jv(h.get("test_device_allowlist").unwrap()),
        "{\"enabled\":true,\"coms\":[\"COM9\"],\"source\":\"env\"}"
    );
    let plain = ServiceRig::ready_shell().call("health.status", "{}");
    assert!(
        plain.get("test_device_allowlist").is_none(),
        "未啟用時無此鍵"
    );
}

#[test]
fn interactive_status_routes_by_interactive_id() {
    let r = ServiceRig::ready_shell();
    let open = r.call("session.interactive_open", r#"{"selector":"COM0"}"#);
    assert_eq!(open.get("ok"), Some(&PyValue::Bool(true)), "{}", j(&open));
    let iid = s(&open, "interactive_id").to_string();
    let st = r.call(
        "session.interactive_status",
        &format!(r#"{{"interactive_id":"{iid}"}}"#),
    );
    assert_eq!(st.get("ok"), Some(&PyValue::Bool(true)), "{}", j(&st));
    assert_eq!(
        j(&r.call("session.interactive_status", r#"{"interactive_id":"nope"}"#)),
        format!(
            "{{\"ok\":false,\"error_code\":\"INTERACTIVE_NOT_FOUND\",\"interactive_id\":\"nope\",\"version\":\"{}\"}}",
            r.version
        )
    );
    assert_eq!(
        j(&r.call("session.interactive_status", r#"{"selector":"COM0"}"#)),
        format!(
            "{{\"ok\":false,\"error_code\":\"INVALID_ARGS\",\"version\":\"{}\"}}",
            r.version
        ),
        "不解析 selector"
    );
    let e = r.call_err(
        "session.interactive_status",
        r#"{"interactive_id":"nope","screen_chars":"x"}"#,
    );
    assert_eq!(e.message, "invalid literal for int() with base 10: 'x'");
    let close = r.call(
        "session.interactive_close",
        &format!(r#"{{"interactive_id":"{iid}"}}"#),
    );
    assert_eq!(close.get("ok"), Some(&PyValue::Bool(true)), "{}", j(&close));
}

#[test]
fn wp51_interactive_owner_policy() {
    let r = ServiceRig::ready_shell();
    r.device(REAL_A)
        .on_tx_reply(b"wp51-initial-command", b"\r\nroot@t:/# ");
    r.settle();
    let session_id = r.mgr.resolve("COM0").expect("COM0 session");

    let rejected_params = [
        r#"{"selector":"COM0","owner":"system:x","command":"wp51-initial-command"}"#,
        r#"{"owner":"system:x"}"#,
        r#"{"selector":"COM0","owner":"system:x","timeout_s":"invalid"}"#,
    ];
    for params in rejected_params {
        let before_effects = r.side_effect_fingerprint();
        let before_session = r.session("COM0").expect("session 前態");
        let before_registry = r.registry.len();
        let before_pending = r.registry.pending(&session_id);
        let before_queue = r.mgr.session_queue_lens("COM0");

        let response = r
            .call_raw("session.interactive_open", params)
            .expect("保留 owner 須先於 selector／timeout 回傳 SOURCE_RESERVED");
        r.settle();

        assert_eq!(
            j(&response),
            wp51_reserved_source_response(&r, "owner", "system:x"),
            "owner 保留前綴必須在 session route 前拒絕：{params}"
        );
        assert_eq!(r.side_effect_fingerprint(), before_effects, "{params}");
        assert_eq!(
            r.session("COM0").expect("session 後態"),
            before_session,
            "{params}"
        );
        assert_eq!(r.registry.len(), before_registry, "registry：{params}");
        assert_eq!(
            r.registry.pending(&session_id),
            before_pending,
            "pending：{params}"
        );
        assert_eq!(
            r.mgr.session_queue_lens("COM0"),
            before_queue,
            "queue：{params}"
        );
    }

    let r = ServiceRig::ready_shell();
    let opened = r.call(
        "session.interactive_open",
        r#"{"selector":"COM0","owner":"human"}"#,
    );
    assert_eq!(
        opened.get("ok"),
        Some(&PyValue::Bool(true)),
        "{}",
        j(&opened)
    );
    assert!(opened.get("source").is_none());
    assert!(opened.get("source_normalized_from").is_none());
    assert!(opened.get("source_class").is_none());
    let interactive_id = s(&opened, "interactive_id").to_string();
    let status = r.call(
        "session.interactive_status",
        &format!(r#"{{"interactive_id":"{interactive_id}"}}"#),
    );
    assert_eq!(
        s(&status, "owner"),
        "human",
        "interactive owner human 保持原值"
    );
    let closed = r.call(
        "session.interactive_close",
        &format!(r#"{{"interactive_id":"{interactive_id}"}}"#),
    );
    assert_eq!(
        closed.get("ok"),
        Some(&PyValue::Bool(true)),
        "{}",
        j(&closed)
    );

    let r = ServiceRig::ready_shell();
    let opened = r.call("session.interactive_open", r#"{"selector":"COM0"}"#);
    assert_eq!(
        opened.get("ok"),
        Some(&PyValue::Bool(true)),
        "{}",
        j(&opened)
    );
    let interactive_id = s(&opened, "interactive_id").to_string();
    let status = r.call(
        "session.interactive_status",
        &format!(r#"{{"interactive_id":"{interactive_id}"}}"#),
    );
    assert_eq!(s(&status, "owner"), "agent", "未指定 owner 預設為 agent");
}

#[test]
fn command_cancel_queued_command() {
    let r = ServiceRig::ready_shell();
    let _running = submit_blocking(&r);
    let queued = r.call(
        "command.submit",
        r#"{"selector":"COM0","cmd":"echo queued"}"#,
    );
    assert_eq!(
        queued.get("ok"),
        Some(&PyValue::Bool(true)),
        "{}",
        j(&queued)
    );
    let id = s(&queued, "cmd_id").to_string();
    let (len_before, physical_before) = r.mgr.session_queue_lens("COM0").expect("COM0 佇列");
    let resp = r.call("command.cancel", &format!(r#"{{"cmd_id":"{id}"}}"#));
    assert_eq!(
        j(&resp),
        format!(
            "{{\"ok\":true,\"cmd_id\":\"{id}\",\"status\":\"canceled\",\"version\":\"{}\"}}",
            r.version
        )
    );
    let rec = r.registry.get(&id).unwrap();
    assert_eq!(rec.get("status"), Some(&PyValue::from("canceled")));
    assert_eq!(
        rec.get("started_at"),
        Some(&PyValue::Null),
        "排隊中取消，不得開始執行"
    );
    // 審查 F74：計畫 Task 18 要求 RPC cancel 經 `route(sid, CancelQueued)` → `SessionQueue::remove`，
    // 前景仍阻塞時佇列實體項目即減 1（不等出列時 Skip）。
    assert_eq!(
        r.mgr.session_queue_lens("COM0"),
        Some((len_before - 1, physical_before - 1)),
        "cancel 後佇列 len／physical_len 皆應減 1"
    );
    // 再取消：Python 只擋 done／error，canceled 可重複取消。
    let again = r.call("command.cancel", &format!(r#"{{"cmd_id":"{id}"}}"#));
    assert_eq!(again.get("ok"), Some(&PyValue::Bool(true)));
}

/// DF-25（OD-77 B1，`contracts/behavior_changes.yaml` DF-25）早段取消：對照
/// `origin/main:tests/test_s1m_b1_interactive_cancel.py:53`（`test_cancel_before_interactive_tx_still_blocks_command`
/// 的 `before_lease`）——只撤銷命令自己的 op，仍須擋下尚未出列的 interactive 命令：零 TX、不遺留 lease。
/// RPC 路徑：前景由阻塞命令佔住時 `command.submit {mode:"interactive"}` 入列；`Service::rpc("command.cancel")` →
/// `CommandRegistry::cancel`（紀錄 canceled）→ `route(SessionRequest::CancelQueued)` → `SessionCore`
/// `queue.remove`（前景 grant 的 cmd_id 不符，不撤銷佔用命令的 op）；前景以 prompt 收尾後 `start_next` 不再出列
/// 被取消者，故不開 lease、不發 `OpSpec::InteractiveOpenCmd`。
#[test]
fn df25_cancel_queued_interactive_zero_tx() {
    let r = ServiceRig::ready_shell();
    let dev = r.device(REAL_A);
    r.settle();
    let session_id = r.mgr.resolve("COM0").expect("COM0 session");
    let tx_mark = dev.tx_len();
    let os_mark = dev.os_submits();
    let wal_mark = wp58_g12_wal_rows(&r).len();
    let hold = submit_blocking(&r);
    wait_until("佔用命令已送上 UART", || {
        wp55_wal_tx(&r, &hold) == vec![("agent".to_string(), wp55_b64(b"sleep 100\n"))]
    });

    let submitted = r.call(
        "command.submit",
        r#"{"selector":"COM0","cmd":"top","mode":"interactive"}"#,
    );
    assert_eq!(submitted.get("ok"), Some(&PyValue::Bool(true)), "{}", j(&submitted));
    let cmd_id = s(&submitted, "cmd_id").to_string();
    let queued = wp58_g12_get(&r, &cmd_id);
    assert_eq!(s(&queued, "status"), "accepted", "前置：前景佔用中仍在佇列：{}", j(&queued));
    assert_eq!(queued.get("started_at"), Some(&PyValue::Null), "前置：尚未出列");
    let (len_before, physical_before) = r.mgr.session_queue_lens("COM0").expect("COM0 佇列");
    assert_eq!(len_before, 1, "前置：interactive 命令在佇列中");
    let session = r.session("COM0").expect("COM0 session");
    assert_eq!(
        session.get("interactive_session_id"),
        Some(&PyValue::Null),
        "前置：尚未開 lease：{}",
        j(&session)
    );

    let canceled = r.call("command.cancel", &format!(r#"{{"cmd_id":"{cmd_id}"}}"#));
    assert_eq!(j(&canceled), wp58_g12_cancel_reply(&r, &cmd_id));
    let canceled_record = wp58_g12_get(&r, &cmd_id);
    assert_eq!(s(&canceled_record, "status"), "canceled", "{}", j(&canceled_record));
    assert_eq!(
        canceled_record.get("started_at"),
        Some(&PyValue::Null),
        "排隊中取消，不得開始執行"
    );
    assert_eq!(
        r.mgr.session_queue_lens("COM0"),
        Some((len_before - 1, physical_before - 1)),
        "cancel 後佇列 len／physical_len 皆應減 1"
    );

    // 前景以 prompt 正常收尾（取消不得撤銷佔用命令的 op），之後不得再出列被取消的 interactive 命令。
    dev.push_rx(b"\r\nroot@t:/# ");
    let held = wp55_wait_done(&r, &hold);
    assert_eq!(s(&held, "status"), "done", "{}", j(&held));
    wait_until("佇列與在途指標清空", || r.registry.pending(&session_id) == 0);
    r.settle();

    assert_eq!(dev.os_submits(), os_mark + 1, "只有佔用命令一次 OS submit");
    assert_eq!(
        String::from_utf8_lossy(&dev.tx_since(tx_mark)),
        "sleep 100\n",
        "被取消的 interactive 命令不得寫出任何 UART bytes"
    );
    assert_eq!(
        wp58_g12_tx_rows_since(&r, wal_mark),
        vec![(format!("\"{hold}\""), wp55_b64(b"sleep 100\n"))],
        "TX WAL 恰為佔用命令一筆"
    );
    assert!(wp55_wal_tx(&r, &cmd_id).is_empty(), "被取消命令不得有 TX WAL");
    assert_eq!(
        j(&wp58_g12_get(&r, &cmd_id)),
        j(&canceled_record),
        "前景收尾不得覆寫已取消紀錄"
    );
    let session = r.session("COM0").expect("COM0 session");
    assert_eq!(
        session.get("interactive_session_id"),
        Some(&PyValue::Null),
        "被取消命令不得開 lease：{}",
        j(&session)
    );
    assert_eq!(
        r.mgr.session_queue_lens("COM0").map(|(len, _)| len),
        Some(0),
        "佇列已清空"
    );
}

// ---------------------------------------------------------------------------
// WP5-8 第 5 項（G12，S1X-a-1 延伸；裁決 s2 D7）：原命令的取消必須在 TX fence 內擋下 interactive 命令的送出。
//
// 基準 `origin/main:sw_core/uart_io.py:635-638`（`_tx_reject_reason` 同時查 `permit.op_id` 與 `cancel_op_id`）與
// `tests/test_s1x_r2_interactive_cancel_race.py:24-51`。Rust 不複製 `cancel_op_id`：interactive 命令的 TX permit
// 本身就是原命令的前景 grant（`start_next` interactive 分支 `new_grant(Some(cmd_id))` → `OpSpec::InteractiveOpenCmd`
// → `lease_send` 以 `grant.op_id` 建 permit），之後的 `session.interactive_send` 各自 `new_grant(None)` 取新 op。
// 呼叫路徑：`Service::rpc("command.cancel")` → `CommandRegistry::cancel`（紀錄 canceled）→
// `route(SessionRequest::CancelQueued)` → `SessionCore`：`queue.remove`＋前景 grant 的 cmd_id 相符時
// `Fence::revoke_ops([grant.op_id])` → TxWriter 的 `write_gated` 於 `gate.submit` 內 `Fence::admit` 重驗 →
// `FenceReject::Cancelled`（`NotSubmitted`）→ `lease_send` 回 `SendRefused` → `on_op_finished` 關閉本命令的 lease
// （G11）、`registry.complete` 不覆寫 canceled 紀錄（`arbiter.rs` `complete`；Python `arbiter.py:453`）。
// ---------------------------------------------------------------------------

/// WAL 全部紀錄（依落盤順序）。
fn wp58_g12_wal_rows(r: &ServiceRig) -> Vec<PyDict> {
    let bytes = r.wfs.contents(WP55_WAL.as_ref()).unwrap_or_default();
    String::from_utf8(bytes)
        .expect("WAL 應為 UTF-8")
        .lines()
        .map(|line| {
            let PyValue::Dict(record) = sw_proto::pyjson::loads(line).expect("WAL 行應為 JSON")
            else {
                panic!("WAL 行應為 object：{line}");
            };
            record
        })
        .collect()
}

/// 自 `wal_mark` 起新增的 TX 紀錄：`(cmd_id JSON, payload_b64)`。
fn wp58_g12_tx_rows_since(r: &ServiceRig, wal_mark: usize) -> Vec<(String, String)> {
    let rows = wp58_g12_wal_rows(r);
    for row in &rows[wal_mark..] {
        println!("[wp58_g12] WAL 新增：{}", j(row));
    }
    rows[wal_mark..]
        .iter()
        .filter(|row| row.get("dir") == Some(&PyValue::from("TX")))
        .map(|row| {
            (
                jv(row.get("cmd_id").unwrap_or(&PyValue::Null)),
                s(row, "payload_b64").to_string(),
            )
        })
        .collect()
}

fn wp58_g12_get(r: &ServiceRig, cmd_id: &str) -> PyDict {
    sub(
        &r.call("command.get", &format!(r#"{{"cmd_id":"{cmd_id}"}}"#)),
        "command",
    )
}

/// session 目前的 lease id（`interactive_session_id`；無 lease 時 panic）。
fn wp58_g12_lease_id(r: &ServiceRig) -> String {
    let session = r.session("COM0").expect("COM0 session");
    session
        .get("interactive_session_id")
        .and_then(PyValue::as_str)
        .unwrap_or_else(|| panic!("應有 lease：{}", j(&session)))
        .to_string()
}

fn wp58_g12_cancel_reply(r: &ServiceRig, cmd_id: &str) -> String {
    format!(
        "{{\"ok\":true,\"cmd_id\":\"{cmd_id}\",\"status\":\"canceled\",\"version\":\"{}\"}}",
        r.version
    )
}

/// 取消落在 lease 已開、writer 已進入 `write_gated` 但尚未經 fence 提交之間（Python
/// `test_cancel_after_precheck_blocks_interactive_tx` 的交錯）：零 OS submit、零 UART bytes、零 TX WAL，
/// 本命令開的 lease 關閉；`command.get` 維持 `canceled`（不改寫為執行器內部的 `FLUSHED_BY_RECOVERY`）。
#[test]
fn wp58_g12_cancel_before_interactive_submit_zero_tx() {
    use sw_pal::fakes::FakeWriteStep;
    let r = ServiceRig::ready_shell();
    let dev = r.device(REAL_A);
    r.settle();
    let session_id = r.mgr.resolve("COM0").expect("COM0 session");
    let tx_mark = dev.tx_len();
    let os_mark = dev.os_submits();
    let wal_mark = wp58_g12_wal_rows(&r).len();
    dev.script_write(vec![FakeWriteStep::PauseBeforeSubmit]);

    let submitted = r.call(
        "command.submit",
        r#"{"selector":"COM0","cmd":"top","mode":"interactive"}"#,
    );
    assert_eq!(submitted.get("ok"), Some(&PyValue::Bool(true)), "{}", j(&submitted));
    let cmd_id = s(&submitted, "cmd_id").to_string();
    assert!(
        dev.wait_paused(Duration::from_secs(5)),
        "writer 未抵達 fence 提交前的暫停點"
    );
    // 前置：本命令的 lease 已開、writer 停在提交前（尚未交 OS）、命令 running。
    let lease_id = wp58_g12_lease_id(&r);
    assert_eq!(dev.os_submits(), os_mark, "前置：尚未交 OS");
    assert!(dev.tx_since(tx_mark).is_empty(), "前置：UART 尚無 bytes");
    assert_eq!(s(&wp58_g12_get(&r, &cmd_id), "status"), "running");

    let canceled = r.call("command.cancel", &format!(r#"{{"cmd_id":"{cmd_id}"}}"#));
    assert_eq!(j(&canceled), wp58_g12_cancel_reply(&r, &cmd_id));
    let canceled_record = wp58_g12_get(&r, &cmd_id);
    assert_eq!(s(&canceled_record, "status"), "canceled", "{}", j(&canceled_record));
    assert_eq!(
        r.registry.pending(&session_id),
        1,
        "canceled-running 在執行器收尾前仍占額度（Python `arbiter.py:314-333`）"
    );

    // 取消回覆後才放行 writer：fence 內重驗必須擋下。
    dev.release_pause();
    wait_until("執行器收尾（清除 canceled-running 在途指標）", || {
        r.registry.pending(&session_id) == 0
    });
    r.settle();

    assert_eq!(dev.os_submits(), os_mark, "取消後不得有任何 OS submit");
    assert_eq!(
        String::from_utf8_lossy(&dev.tx_since(tx_mark)),
        "",
        "取消後 UART 不得出現任何 bytes"
    );
    assert_eq!(wp58_g12_tx_rows_since(&r, wal_mark), Vec::new(), "取消後不得有 TX WAL");
    assert!(wp55_wal_tx(&r, &cmd_id).is_empty(), "本命令不得有 TX WAL");
    assert_eq!(
        j(&wp58_g12_get(&r, &cmd_id)),
        j(&canceled_record),
        "執行器收尾不得覆寫已取消紀錄"
    );
    let session = r.session("COM0").expect("COM0 session");
    assert_eq!(
        session.get("interactive_session_id"),
        Some(&PyValue::Null),
        "不得遺留本命令開的 lease：{}",
        j(&session)
    );
    assert_eq!(
        j(&r.call(
            "session.interactive_status",
            &format!(r#"{{"interactive_id":"{lease_id}"}}"#),
        )),
        format!(
            "{{\"ok\":false,\"error_code\":\"INTERACTIVE_NOT_FOUND\",\"interactive_id\":\"{lease_id}\",\"version\":\"{}\"}}",
            r.version
        ),
        "lease 已非 expired 關閉"
    );

    // 撤銷只綁被取消的 op：後續 line 命令照常寫到 UART 並完成。
    dev.on_tx_reply(b"ls\n", b"ls\r\nroot@t:/# ");
    let response = r.call("command.submit", r#"{"selector":"COM0","cmd":"ls","timeout_s":10}"#);
    assert_eq!(response.get("ok"), Some(&PyValue::Bool(true)), "{}", j(&response));
    let record = wp55_wait_done(&r, s(&response, "cmd_id"));
    assert_eq!(s(&record, "status"), "done", "{}", j(&record));
    assert_eq!(String::from_utf8_lossy(&dev.tx_since(tx_mark)), "ls\n");
}

/// 原命令 TX 已經 fence 交 OS、writer 仍在 gate 外阻塞（op 未結算，仍是前景）時取消：撤銷的是原命令的 op，
/// 同一存活 lease 之後兩次 `session.interactive_send` 皆成功送達（Python
/// `test_lease_op_cancel_identity_does_not_leak_to_later_sends`）。以 `BlockForever`＋`wait_blocked` 為同步屏障，
/// 並以取消前的 lease send 回 `SESSION_BUSY` 證明取消當下原命令仍為前景（`CancelQueued` 會命中並撤銷其 op）。
#[test]
fn wp58_g12_original_cancel_does_not_poison_lease_sends() {
    use sw_pal::fakes::FakeWriteStep;
    let r = ServiceRig::ready_shell();
    let dev = r.device(REAL_A);
    r.settle();
    let session_id = r.mgr.resolve("COM0").expect("COM0 session");
    let tx_mark = dev.tx_len();
    let os_mark = dev.os_submits();
    let wal_mark = wp58_g12_wal_rows(&r).len();
    dev.script_write(vec![FakeWriteStep::BlockForever]);

    let submitted = r.call(
        "command.submit",
        r#"{"selector":"COM0","cmd":"top","mode":"interactive"}"#,
    );
    assert_eq!(submitted.get("ok"), Some(&PyValue::Bool(true)), "{}", j(&submitted));
    let cmd_id = s(&submitted, "cmd_id").to_string();
    dev.wait_blocked();
    assert_eq!(dev.os_submits(), os_mark + 1, "原命令 TX 已交 OS");
    assert_eq!(String::from_utf8_lossy(&dev.tx_since(tx_mark)), "top\n");
    let lease_id = wp58_g12_lease_id(&r);

    let send = |data: &str| {
        r.call(
            "session.interactive_send",
            &format!(r#"{{"interactive_id":"{lease_id}","data":"{data}","encoding":"plain"}}"#),
        )
    };
    assert_eq!(
        j(&send("q")),
        format!(
            "{{\"ok\":false,\"error_code\":\"SESSION_BUSY\",\"interactive_id\":\"{lease_id}\",\"version\":\"{}\"}}",
            r.version
        ),
        "取消前原命令 op 仍為前景"
    );
    assert_eq!(dev.os_submits(), os_mark + 1, "SESSION_BUSY 不送出");

    let canceled = r.call("command.cancel", &format!(r#"{{"cmd_id":"{cmd_id}"}}"#));
    assert_eq!(j(&canceled), wp58_g12_cancel_reply(&r, &cmd_id));
    let canceled_record = wp58_g12_get(&r, &cmd_id);
    assert_eq!(s(&canceled_record, "status"), "canceled", "{}", j(&canceled_record));

    dev.release_block();
    wait_until("執行器收尾（清除 canceled-running 在途指標）", || {
        r.registry.pending(&session_id) == 0
    });
    assert_eq!(
        wp58_g12_lease_id(&r),
        lease_id,
        "已完整送出：lease 保持存活"
    );

    for data in ["q", "j"] {
        assert_eq!(
            j(&send(data)),
            format!(
                "{{\"ok\":true,\"interactive_id\":\"{lease_id}\",\"bytes\":1,\"version\":\"{}\"}}",
                r.version
            ),
            "lease send {data} 必須送達"
        );
    }
    r.settle();
    assert_eq!(dev.os_submits(), os_mark + 3);
    assert_eq!(String::from_utf8_lossy(&dev.tx_since(tx_mark)), "top\nqj");
    assert_eq!(
        wp58_g12_tx_rows_since(&r, wal_mark),
        vec![
            (format!("\"{cmd_id}\""), wp55_b64(b"top\n")),
            ("null".to_string(), wp55_b64(b"q")),
            ("null".to_string(), wp55_b64(b"j")),
        ],
        "TX WAL 恰為原命令一筆＋lease send 兩筆"
    );
    assert_eq!(
        j(&wp58_g12_get(&r, &cmd_id)),
        j(&canceled_record),
        "執行器收尾不得覆寫已取消紀錄"
    );
    let closed = r.call(
        "session.interactive_close",
        &format!(r#"{{"interactive_id":"{lease_id}"}}"#),
    );
    assert_eq!(closed.get("ok"), Some(&PyValue::Bool(true)), "{}", j(&closed));
}

// ---------------------------------------------------------------------------
// DF-25（OD-77 B1，`contracts/behavior_changes.yaml` DF-25）：interactive 命令 TX 完成後、執行器返回前取消，
// 不得撤銷存活的 lease。
//
// 基準 `origin/main:sw_core/session_manager.py:4595`（`revoke_tx_op` 只撤銷 `[cmd_id]`，lease 本身仍可續用）與
// `origin/main:tests/test_s1m_b1_interactive_cancel.py:16`
// （`test_cancel_after_interactive_tx_before_executor_return_preserves_lease`）。Rust 的 lease 歸屬依實際
// interactive id：`PendingOp::InteractiveCmd { lease_id }` 只在未完整送出（非 `OpOutcome::Sent`）且目前 lease
// 仍是 `lease_id` 時關閉；取消撤銷的是原命令前景 grant 的 op，之後 `session.interactive_send` 各自
// `new_grant(None)` 取新 op，不受撤銷影響。
// 呼叫路徑：`Service::rpc("command.cancel")` → `CommandRegistry::cancel`（紀錄 canceled）→
// `route(SessionRequest::CancelQueued)` → `SessionCore`：前景 grant 的 cmd_id 相符 → `Fence::revoke_ops([grant.op_id])`；
// 放行後 TxWriter 回 `TxReport`（已確認 4 bytes）→ `lease_send` 判 `fully_sent` → `OpOutcome::Sent` →
// `on_op_finished` 的 `PendingOp::InteractiveCmd` 分支保留 lease、`registry.complete` 不覆寫 canceled 紀錄。
// 同步屏障為 FakeWalFs 的寫後暫停（DF-25 第 1 項）：TxWriter 在 TX WAL 行完整寫入後停在 `write_all_flush` 內，
// `TxReport` 尚未回給 OpRunner、`lease_send` 尚未返回——與上方 `BlockForever`（已交 OS、OS write 未返回、
// 尚無 TX WAL）是不同窗口。
// ---------------------------------------------------------------------------

/// 原命令 TX 已完整交 OS 並落 TX WAL、執行器尚未返回時經 RPC 取消：取消回應與紀錄語意不變（`canceled`、
/// canceled-running 在收尾前仍占額度、收尾與後續 send 不覆寫），同一 lease 之後連送 `q`、`x` 皆成功，
/// UART 與 TX WAL 恰為 `top\nqx`。
#[test]
fn df25_interactive_cancel_preserves_lease() {
    let r = ServiceRig::ready_shell();
    let dev = r.device(REAL_A);
    r.settle();
    let session_id = r.mgr.resolve("COM0").expect("COM0 session");
    let tx_mark = dev.tx_len();
    let os_mark = dev.os_submits();
    let wal_mark = wp58_g12_wal_rows(&r).len();
    let pause = r.wfs.pause_after_next_write(WP55_WAL.as_ref());

    let submitted = r.call(
        "command.submit",
        r#"{"selector":"COM0","cmd":"top","mode":"interactive"}"#,
    );
    assert_eq!(submitted.get("ok"), Some(&PyValue::Bool(true)), "{}", j(&submitted));
    let cmd_id = s(&submitted, "cmd_id").to_string();
    assert!(
        pause.wait_paused(Duration::from_secs(5)),
        "TxWriter 未抵達 TX WAL 寫後暫停點"
    );
    // 前置：TX 已完成（UART 恰為 `top\n`、對應 TX WAL 已落盤），執行器尚未返回（命令 running、lease 已建立）。
    assert_eq!(dev.os_submits(), os_mark + 1, "前置：原命令 TX 已交 OS");
    assert_eq!(String::from_utf8_lossy(&dev.tx_since(tx_mark)), "top\n");
    assert_eq!(
        wp58_g12_tx_rows_since(&r, wal_mark),
        vec![(format!("\"{cmd_id}\""), wp55_b64(b"top\n"))],
        "前置：原命令 TX WAL 已存在"
    );
    assert_eq!(s(&wp58_g12_get(&r, &cmd_id), "status"), "running");
    let lease_id = wp58_g12_lease_id(&r);

    let send = |data: &str| {
        r.call(
            "session.interactive_send",
            &format!(r#"{{"interactive_id":"{lease_id}","data":"{data}","encoding":"plain"}}"#),
        )
    };
    // 以取消前的 lease send 回 `SESSION_BUSY` 證明取消當下原命令 op 仍為前景（`CancelQueued` 會命中並撤銷其 op）。
    assert_eq!(
        j(&send("q")),
        format!(
            "{{\"ok\":false,\"error_code\":\"SESSION_BUSY\",\"interactive_id\":\"{lease_id}\",\"version\":\"{}\"}}",
            r.version
        ),
        "前置：執行器尚未返回，原命令 op 仍為前景"
    );
    assert_eq!(dev.os_submits(), os_mark + 1, "SESSION_BUSY 不送出");

    let canceled = r.call("command.cancel", &format!(r#"{{"cmd_id":"{cmd_id}"}}"#));
    assert_eq!(j(&canceled), wp58_g12_cancel_reply(&r, &cmd_id));
    let canceled_record = wp58_g12_get(&r, &cmd_id);
    assert_eq!(s(&canceled_record, "status"), "canceled", "{}", j(&canceled_record));
    assert_eq!(
        r.registry.pending(&session_id),
        1,
        "canceled-running 在執行器收尾前仍占額度（Python `arbiter.py:314-333`）"
    );

    pause.release();
    wait_until("執行器收尾（清除 canceled-running 在途指標）", || {
        r.registry.pending(&session_id) == 0
    });
    assert_eq!(
        wp58_g12_lease_id(&r),
        lease_id,
        "TX 已完整送出：取消不得撤銷存活 lease"
    );
    assert_eq!(
        j(&wp58_g12_get(&r, &cmd_id)),
        j(&canceled_record),
        "執行器收尾不得覆寫已取消紀錄"
    );

    for data in ["q", "x"] {
        assert_eq!(
            j(&send(data)),
            format!(
                "{{\"ok\":true,\"interactive_id\":\"{lease_id}\",\"bytes\":1,\"version\":\"{}\"}}",
                r.version
            ),
            "lease send {data} 必須送達"
        );
    }
    r.settle();
    assert_eq!(dev.os_submits(), os_mark + 3);
    assert_eq!(String::from_utf8_lossy(&dev.tx_since(tx_mark)), "top\nqx");
    assert_eq!(
        wp58_g12_tx_rows_since(&r, wal_mark),
        vec![
            (format!("\"{cmd_id}\""), wp55_b64(b"top\n")),
            ("null".to_string(), wp55_b64(b"q")),
            ("null".to_string(), wp55_b64(b"x")),
        ],
        "TX WAL 恰為原命令一筆＋lease send 兩筆"
    );
    assert_eq!(
        j(&wp58_g12_get(&r, &cmd_id)),
        j(&canceled_record),
        "後續 lease send 不得覆寫已取消紀錄"
    );
    let closed = r.call(
        "session.interactive_close",
        &format!(r#"{{"interactive_id":"{lease_id}"}}"#),
    );
    assert_eq!(
        j(&closed),
        format!(
            "{{\"ok\":true,\"interactive_id\":\"{lease_id}\",\"version\":\"{}\"}}",
            r.version
        )
    );
    let session = r.session("COM0").expect("COM0 session");
    assert_eq!(
        session.get("interactive_session_id"),
        Some(&PyValue::Null),
        "lease 已關閉：{}",
        j(&session)
    );
}

#[test]
fn health_status_multi_open_two_daemons() {
    let _g = holder_lock();
    let r = ServiceRig::ready_shell();
    r.procs.script(vec![vec![
        daemon(5000, "/run/b.sock"),
        daemon(RIG_PID, "/run/a.sock"),
    ]]);
    r.procs.holders(vec![RIG_PID], HolderScanStatus::Ok);
    let h = r.call("health.status", "{}");
    assert_eq!(h.get("multi_open"), Some(&PyValue::Bool(true)));
    assert_eq!(
        jv(h.get("multi_open_detail").unwrap()),
        format!(
            "{{\"daemons\":[{{\"pid\":{RIG_PID},\"socket\":\"/run/a.sock\"}},{{\"pid\":5000,\"socket\":\"/run/b.sock\"}}],\"holders_status\":\"ok\"}}"
        )
    );
    // RPC-D-10：一般值包含 daemon 自身 pid。
    assert_eq!(
        jv(h.get("foreign_holders").unwrap()),
        format!("{{\"{REAL_A}\":{RIG_PID}}}")
    );
}

#[test]
fn health_status_scan_failure_shape() {
    let _g = holder_lock();
    let r = ServiceRig::ready_shell();
    r.procs.fail();
    let h = r.call("health.status", "{}");
    assert_eq!(h.get("multi_open"), Some(&PyValue::Bool(false)));
    assert_eq!(jv(h.get("foreign_holders").unwrap()), "{}");
    assert_eq!(
        jv(h.get("multi_open_detail").unwrap()),
        "{\"daemons\":[],\"holders_status\":\"unknown\"}"
    );
}

#[test]
fn health_status_holders_status_permission() {
    let _g = holder_lock();
    let r = ServiceRig::ready_shell();
    r.procs.script(vec![vec![daemon(RIG_PID, "/run/a.sock")]]);
    r.procs.holders(Vec::new(), HolderScanStatus::Permission);
    let h = r.call("health.status", "{}");
    assert_eq!(h.get("multi_open"), Some(&PyValue::Bool(false)));
    assert_eq!(jv(h.get("foreign_holders").unwrap()), "{}");
    assert_eq!(
        s(&sub(&h, "multi_open_detail"), "holders_status"),
        "permission"
    );
}

// ---------------------------------------------------------------------------
// parity-amendments-2026-10-26 追加
// ---------------------------------------------------------------------------

/// #11：鎖後 holder 掃描不完整的裝置以 `{device, holder_unknown, pids?}` 物件合併進 `foreign_holders`
/// （`service.py` health、`rpc_schema.json` x-note；對照鎖內 Python
/// `test_stage0_uart_exclusive.py::TestManagerAttach::test_health_status_exposes_unknown_holder_with_device`）。
#[test]
fn health_status_foreign_holders_holder_unknown_object() {
    let _g = holder_lock();
    let r = ServiceRig::two_shells();
    r.procs.script(vec![vec![daemon(RIG_PID, "/run/a.sock")]]);
    r.procs.holders_script(vec![
        (vec![123], HolderScanStatus::Ok),
        (vec![RIG_PID], HolderScanStatus::Ok),
    ]);
    // REAL_A：PAL 開檔 AcceptUnverified 登記（替身直接登記）；/fake/tty-Z：未 attach 但登記中。
    sw_pal::serial::set_holder_unknown(REAL_A, true);
    sw_pal::serial::set_holder_unknown("/fake/tty-Z", true);
    let h = r.call("health.status", "{}");
    sw_pal::serial::set_holder_unknown(REAL_A, false);
    sw_pal::serial::set_holder_unknown("/fake/tty-Z", false);
    // 依 session_id 排序掃描：COM0（REAL_A）→ 123；COM1（REAL_B）→ 自身 pid。
    assert_eq!(
        jv(h.get("foreign_holders").unwrap()),
        format!(
            "{{\"{REAL_A}\":{{\"device\":\"{REAL_A}\",\"holder_unknown\":true,\"pids\":[123]}},\"{REAL_B}\":{RIG_PID},\"/fake/tty-Z\":{{\"device\":\"/fake/tty-Z\",\"holder_unknown\":true}}}}"
        )
    );
    // 移除登記後恢復 path→pid。
    let h = r.call("health.status", "{}");
    assert!(!jv(h.get("foreign_holders").unwrap()).contains("holder_unknown"));
}

/// WP5-9：真 PTY 經 `device.release`／`device.attach` 重開後，RPC 保留 Python 的 holder 形狀。
#[test]
#[ignore = "cap:posix_pty"]
#[allow(non_snake_case)]
fn cap_posix_pty__wp59_attach_health_shape() {
    let _g = holder_lock();

    let partial_pty = sw_pal::fakes::proc::posix_pty_device()
        .expect("此能力案例須使用 POSIX PTY");
    let partial_path = partial_pty.device.0.clone();
    let (partial, partial_probes) =
        wp59_posix_rig(&partial_path, HolderScanStatus::PartialOtherUid);
    partial.wait_state("COM0", "ATTACHED");
    let probes_before_attach = partial_probes.load(Ordering::SeqCst);
    let released = partial.call("device.release", r#"{"selector":"COM0"}"#);
    assert_eq!(released.get("ok"), Some(&PyValue::Bool(true)), "{}", j(&released));
    partial.wait_state("COM0", "RELEASED");
    let attached = partial.call("device.attach", r#"{"selector":"COM0"}"#);
    assert_eq!(attached.get("ok"), Some(&PyValue::Bool(true)), "{}", j(&attached));
    partial.wait_state("COM0", "ATTACHED");
    assert_eq!(
        partial_probes.load(Ordering::SeqCst),
        probes_before_attach + 1,
        "device.attach 必須經 POSIX backend 再做一次鎖後 holder 掃描"
    );

    wp59_configure_health_scan(&partial);
    let health = partial.call("health.status", "{}");
    assert_eq!(
        jv(health.get("foreign_holders").unwrap()),
        format!("{{\"{partial_path}\":{RIG_PID}}}"),
        "PartialOtherUid 不應包成 holder_unknown 物件"
    );
    assert_eq!(
        jv(health.get("multi_open_detail").unwrap()),
        format!(
            "{{\"daemons\":[{{\"pid\":{RIG_PID},\"socket\":\"/run/wp59.sock\"}}],\"holders_status\":\"permission\"}}"
        ),
        "health 自己的 FakeProcScan Permission 必須維持 permission"
    );
    let released = partial.call("device.release", r#"{"selector":"COM0"}"#);
    assert_eq!(released.get("ok"), Some(&PyValue::Bool(true)), "{}", j(&released));
    partial.wait_state("COM0", "RELEASED");
    assert!(
        !sw_pal::serial::unknown_holder_devices().contains(&partial_path),
        "PartialOtherUid 裝置不應殘留 unknown 登記"
    );

    let permission_pty = sw_pal::fakes::proc::posix_pty_device()
        .expect("此能力案例須使用 POSIX PTY");
    let permission_path = permission_pty.device.0.clone();
    let (permission, _) = wp59_posix_rig(&permission_path, HolderScanStatus::Permission);
    permission.wait_state("COM0", "ATTACHED");
    let released = permission.call("device.release", r#"{"selector":"COM0"}"#);
    assert_eq!(released.get("ok"), Some(&PyValue::Bool(true)), "{}", j(&released));
    permission.wait_state("COM0", "RELEASED");
    let attached = permission.call("device.attach", r#"{"selector":"COM0"}"#);
    assert_eq!(attached.get("ok"), Some(&PyValue::Bool(true)), "{}", j(&attached));
    permission.wait_state("COM0", "ATTACHED");
    wp59_configure_health_scan(&permission);
    let health = permission.call("health.status", "{}");
    assert_eq!(
        jv(health.get("foreign_holders").unwrap()),
        format!(
            "{{\"{permission_path}\":{{\"device\":\"{permission_path}\",\"holder_unknown\":true,\"pids\":[{RIG_PID}]}}}}"
        ),
        "Permission 對照組仍須回傳既有 holder_unknown 物件"
    );
    assert_eq!(
        jv(health.get("multi_open_detail").unwrap()),
        format!(
            "{{\"daemons\":[{{\"pid\":{RIG_PID},\"socket\":\"/run/wp59.sock\"}}],\"holders_status\":\"permission\"}}"
        )
    );
    let released = permission.call("device.release", r#"{"selector":"COM0"}"#);
    assert_eq!(released.get("ok"), Some(&PyValue::Bool(true)), "{}", j(&released));
    permission.wait_state("COM0", "RELEASED");
    assert!(
        !sw_pal::serial::unknown_holder_devices().contains(&permission_path),
        "關閉 Permission 對照組後必須清除 unknown 登記"
    );
}

/// #69：`READY_UNCONFIRMED` 回應帶 `session`（Python 基線：service 層 `_resolve_session_id` 附 `session`；
/// Rust 由 `SessionCore::submit` 承接，經 service 原樣回傳；`tests/test_ready_reconfirm_bounds.py::TestSubmitGateTerminal`）。
#[test]
fn ready_unconfirmed_response_has_session_field() {
    let r = ServiceRig::ready_shell();
    // 自發重開機 banner → 武裝 boot quiet 與 Ready Reconfirm；之後 300 s 未經 nonce probe 再確認。
    r.device(REAL_A).push_rx(b"\r\nU-Boot 2024.01 (Jan 01)\r\n");
    wait_until("boot quiet 武裝", || {
        r.session("COM0")
            .and_then(|s| s.get("boot_quiet_remaining_s").cloned())
            .is_some_and(|v| !v.is_null())
    });
    r.clock.advance(Duration::from_secs(301));
    let resp = r.call(
        "command.submit",
        r#"{"selector":"COM0","cmd":"echo hi","source":"agent:test"}"#,
    );
    assert_eq!(s(&resp, "error_code"), "READY_UNCONFIRMED", "{}", j(&resp));
    assert_eq!(
        keys(&resp),
        [
            "ok",
            "error_code",
            "hint",
            "recommended_action",
            "session",
            "version"
        ]
    );
    assert_eq!(s(&resp, "recommended_action"), "self_test");
    assert!(
        resp.get("retry_after_s").is_none(),
        "不可重試：不得帶 retry_after_s"
    );
    assert!(
        resp.get("cmd_id").is_none(),
        "submit-time 拒絕不產生 cmd_id"
    );
    assert_eq!(
        sub(&resp, "session").get("ready_reconfirm_failed"),
        Some(&PyValue::Bool(true))
    );
}

#[test]
fn interactive_open_unknown_selector_session_not_ready() {
    // 審查 F73（High，8a7986e）：Python `SessionManager.interactive_open` 查無 session 回
    // `{"ok":false,"error_code":"SESSION_NOT_READY","selector":<原 selector>}`（非 SESSION_NOT_FOUND）。
    let r = ServiceRig::ready_shell();
    assert_eq!(
        j(&r.call("session.interactive_open", r#"{"selector":"NOSUCH"}"#)),
        format!(
            "{{\"ok\":false,\"error_code\":\"SESSION_NOT_READY\",\"selector\":\"NOSUCH\",\"version\":\"{}\"}}",
            r.version
        )
    );
}

#[test]
fn result_tail_stale_owner_finds_capture_in_other_session() {
    // 審查 F76（Medium，8a7986e）：Python `command.result_tail` 先以 cmd_id 全域查 `_background`
    // （`get_background_result`），只有 CMD_NOT_FOUND 才走 `_bg_fallback_from_arbiter`。
    // 具體輸入：COM0、COM1 皆在；背景命令的 capture 在 COM1；命令紀錄的 session_id 已對不上（rematerialize 後）。
    // Python：回 COM1 capture 的結果（含 `dropped_chunks`／`lost`）。舊 Rust：排序較前的 COM0 以登錄表 fallback
    // 先回 `ok:true`、空 chunks、無 `dropped_chunks`。
    let r = ServiceRig::two_shells();
    let resp = r.call(
        "command.submit",
        r#"{"selector":"COM1","cmd":"sleep 100","mode":"bg","timeout_s":30}"#,
    );
    assert_eq!(resp.get("ok"), Some(&PyValue::Bool(true)), "{}", j(&resp));
    let id = s(&resp, "cmd_id").to_string();
    wait_until("背景命令 running", || {
        r.registry.get(&id).and_then(|rec| rec.get("status").cloned())
            == Some(PyValue::from("running"))
    });
    r.registry.set_session_id_for_test(&id, "t:GONE");
    let tail = r.call("command.result_tail", &format!(r#"{{"cmd_id":"{id}"}}"#));
    assert_eq!(tail.get("ok"), Some(&PyValue::Bool(true)), "{}", j(&tail));
    assert!(
        tail.get("dropped_chunks").is_some(),
        "應回 COM1 capture 的結果，而非登錄表 fallback：{}",
        j(&tail)
    );
}

// ---------------------------------------------------------------------------
// 第五輪接線＋端到端驗收（rulings/p5.md P5-70f6ce7-1）
// ---------------------------------------------------------------------------

/// service `command.submit` → actor → OpRunner → `extract_command_stdout` 的折行回顯剝除（line mode）。
/// BusyBox `\r\r\n` 折行回顯須剝除；沒有折行證據、恰為命令兩段的一般 CRLF stdout 須全文保留。
/// 把 `strip_wrapped_command_echo` 改成 no-op 時第一案紅；改成無條件拼接（不看原始證據）時第二案紅。
#[test]
fn command_submit_line_mode_wrapped_echo_end_to_end() {
    let r = ServiceRig::ready_shell();
    let wrapped = format!("echo {} {}", "x".repeat(50), "y".repeat(40));
    let plain = format!("cat /tmp/{}", "x".repeat(60));
    let dev = r.device(REAL_A);
    dev.on_tx_reply(
        format!("{wrapped}\n").as_bytes(),
        format!("{}\r\r\n{}\r\nOUT\r\nroot@t:/# ", &wrapped[..65], &wrapped[65..]).as_bytes(),
    );
    dev.on_tx_reply(
        format!("{plain}\n").as_bytes(),
        format!("{}\r\n{}\r\nOUT\r\nroot@t:/# ", &plain[..40], &plain[40..]).as_bytes(),
    );
    for (cmd, want) in [
        (wrapped.clone(), "OUT".to_string()),
        (plain.clone(), format!("{}\n{}\nOUT", &plain[..40], &plain[40..])),
    ] {
        let resp = r.call(
            "command.submit",
            &format!(r#"{{"selector":"COM0","cmd":"{cmd}","timeout_s":10}}"#),
        );
        assert_eq!(resp.get("ok"), Some(&PyValue::Bool(true)), "{}", j(&resp));
        let id = s(&resp, "cmd_id").to_string();
        let mut got = PyDict::default();
        wait_until("命令完成", || {
            got = sub(&r.call("command.get", &format!(r#"{{"cmd_id":"{id}"}}"#)), "command");
            !matches!(got.get("done_at"), None | Some(PyValue::Null))
        });
        assert_eq!(got.get("stdout"), Some(&PyValue::from(want.as_str())), "{}", j(&got));
    }
}

// ---------------------------------------------------------------------------
// WP5-6 第 5 項：非同步 reboot 開機證據等待的 RPC 端到端（G8，#243；Python
// `origin/main:sw_core/session_manager.py:6409-6411`、`:6463-6466`、`:2218-2223`、`service.py` submit gate）。
//
// 呼叫路徑：`Service::rpc(command.submit)` → `SubmitReq` → `SessionRequest::Submit` → `SessionCore::submit` → queue →
// `start_next`（reboot 判定成立：送出前歸零舊等待）→ `OpSpec::Command` → `OpRunner::run_command` → `run_reboot`
// （2 s 內 prompt 回來 → `OpOutcome::CommandResult(ok)`）→ `SessionCore::finish_command`（`clear_quiet`＋
// `begin_boot_evidence_wait`）→ registry；之後 actor 依 FakeClock 每秒 Tick → `reprobe_tick` → `reprobe::prepare`
// （等待有效 → 不排確認 probe）；`session.get`／submit gate 的 session 由 `SessionCore::public` 投影剩餘寬限。
// ---------------------------------------------------------------------------

/// 逐秒推進 FakeClock，讓 actor 的 1 s Tick 逐一觸發（一次跳多秒只會合併成一個 Tick）。
fn wp56_advance_secs(r: &ServiceRig, secs: u64) {
    for _ in 0..secs {
        r.clock.advance(Duration::from_secs(1));
        std::thread::sleep(Duration::from_millis(25));
    }
    r.settle();
}

/// 對照 `tests/test_async_reboot_evidence.py` 的 `TestStaleShellNonceBeforeBanner`、`TestGraceExpiryDoesNotDeadlock`
/// 與 `TestSubmitTimeGateDuringWait`：RPC 提交 `reboot`，假 DUT 立即重印 prompt（非同步 reboot）→ `command.get` 正常
/// 完成、session `boot_evidence_wait_remaining_s=60.0`；舊 shell 持續能應答 `\n` 與 nonce，但 10 s 內 UART 零 TX；
/// 後續 agent 命令以可重試 `AUTOBOOT_QUIET` 拒絕且 UART／WAL／registry／queue 不變；60 s 寬限到期後確認 probe
/// 實際送出並清 pending，後續命令經 RPC 真正寫到 UART。
#[test]
fn wp56_rpc_async_reboot_holds_confirm_until_grace() {
    let r = ServiceRig::ready_shell();
    let dev = r.device(REAL_A);
    dev.on_tx_reply(b"reboot\n", b"reboot\r\nroot@t:/# ");
    r.settle();
    let response = r.call(
        "command.submit",
        r#"{"selector":"COM0","cmd":"reboot","timeout_s":10}"#,
    );
    assert_eq!(
        response.get("ok"),
        Some(&PyValue::Bool(true)),
        "{}",
        j(&response)
    );
    let cmd_id = s(&response, "cmd_id").to_string();
    let record = wp55_wait_done(&r, &cmd_id);
    assert_eq!(s(&record, "status"), "done", "{}", j(&record));
    assert_eq!(
        record.get("partial"),
        Some(&PyValue::Bool(false)),
        "{}",
        j(&record)
    );
    assert_eq!(
        record.get("recovery_action"),
        Some(&PyValue::Null),
        "{}",
        j(&record)
    );
    r.settle();
    let session = r.session("COM0").expect("COM0 session");
    assert_eq!(
        session.get("state"),
        Some(&PyValue::from("READY")),
        "{}",
        j(&session)
    );
    assert_eq!(
        session.get("boot_quiet_remaining_s"),
        Some(&PyValue::Null),
        "{}",
        j(&session)
    );
    assert_eq!(
        session.get("ready_reconfirm_pending"),
        Some(&PyValue::Bool(true)),
        "{}",
        j(&session)
    );
    assert_eq!(
        session.get("boot_evidence_wait_remaining_s"),
        Some(&PyValue::Float(60.0)),
        "2 s 內 prompt 回來即起算寬限：{}",
        j(&session)
    );

    // 舊 shell 仍活著：每個空行都回 prompt、nonce 照常回顯（`install_shell_replies` 的 nonce 應答常駐）。
    dev.on_tx_match("^$", Box::new(|_line: &[u8]| b"root@t:/# ".to_vec()));
    let calls = dev.write_calls();
    let tx_mark = dev.tx_len();
    wp56_advance_secs(&r, 10);
    assert_eq!(
        dev.write_calls(),
        calls,
        "banner 前不得送確認 probe（PAL 寫入零新增）"
    );
    assert!(dev.tx_since(tx_mark).is_empty(), "UART 零 TX");
    let session = r.session("COM0").expect("COM0 session");
    assert_eq!(
        session.get("ready_reconfirm_pending"),
        Some(&PyValue::Bool(true)),
        "{}",
        j(&session)
    );
    assert_eq!(
        session.get("boot_evidence_wait_remaining_s"),
        Some(&PyValue::Float(50.0)),
        "{}",
        j(&session)
    );
    assert_eq!(
        session.get("reprobe_attempts"),
        Some(&PyValue::from(0)),
        "{}",
        j(&session)
    );
    assert_eq!(
        session.get("next_reprobe_at"),
        Some(&PyValue::Null),
        "{}",
        j(&session)
    );
    let expected = sw_proto::pydict! {
        "ok" => false,
        "error_code" => "AUTOBOOT_QUIET",
        "retry_after_s" => 5.0,
        "hint" => WP53_RECONFIRM_HINT,
        "session" => PyValue::Dict(session.clone()),
        "version" => r.version.clone(),
    };
    assert_wp52_rpc_rejected_without_side_effects(
        &r,
        r#"{"selector":"COM0","cmd":"cat /proc/sys/kernel/random/boot_id","timeout_s":10}"#,
        &j(&expected),
    );

    // 49 s 後（共 59 s）仍在寬限內：零 TX；第 60 s 起照常確認。
    wp56_advance_secs(&r, 49);
    assert_eq!(dev.write_calls(), calls, "59 s 仍在寬限內");
    let mut session = PyDict::default();
    wait_until("寬限到期後確認 READY", || {
        r.clock.advance(Duration::from_millis(250));
        session = r.session("COM0").expect("COM0 session");
        session.get("ready_reconfirm_pending") == Some(&PyValue::Bool(false))
    });
    assert!(dev.write_calls() > calls, "到期後確認 probe 實際送出");
    assert!(
        String::from_utf8_lossy(&dev.tx_since(tx_mark)).contains("__READY__"),
        "UART 送出 nonce probe"
    );
    assert_eq!(
        session.get("state"),
        Some(&PyValue::from("READY")),
        "{}",
        j(&session)
    );
    assert_eq!(
        session.get("boot_evidence_wait_remaining_s"),
        Some(&PyValue::Null),
        "{}",
        j(&session)
    );
    assert_eq!(
        session.get("last_error"),
        Some(&PyValue::Null),
        "{}",
        j(&session)
    );

    dev.on_tx_reply(b"echo hi\n", b"echo hi\r\nhi\r\nroot@t:/# ");
    let tx_mark = dev.tx_len();
    let response = r.call(
        "command.submit",
        r#"{"selector":"COM0","cmd":"echo hi","timeout_s":10}"#,
    );
    assert_eq!(
        response.get("ok"),
        Some(&PyValue::Bool(true)),
        "{}",
        j(&response)
    );
    let next_id = s(&response, "cmd_id").to_string();
    let record = wp55_wait_done(&r, &next_id);
    assert_eq!(s(&record, "status"), "done", "{}", j(&record));
    assert_eq!(s(&record, "stdout"), "hi", "{}", j(&record));
    assert_eq!(String::from_utf8_lossy(&dev.tx_since(tx_mark)), "echo hi\n");
}

// ---------------------------------------------------------------------------
// WP5-6 第 6 項：banner 解除路徑與 self-test 的 RPC 端到端（G8，#243；Python
// `origin/main:sw_core/session_manager.py:6463-6466`（reboot 回 prompt 起算等待）、`:591`／`:800-812`（banner 解除）、
// `:2218-2223`（reprobe 規劃不送確認 probe）、`:7816`（self-test 回 AUTOBOOT_QUIET）、`service.py:829-838`（submit gate）。
//
// 呼叫路徑：`Service::rpc("command.submit")` → submit gate → `SessionManager::route` → `SessionCore::submit` →
// `start_next` → `OpRunner::run_reboot` → `SessionCore::finish_command`（`clear_quiet`＋`begin_boot_evidence_wait`）；
// RX：FakeDevice → `SessionCore::rx_chunk` → `QuietState::on_rx_text`（banner → `note_boot_evidence`＋arm quiet；
// prompt → 解除 quiet）；Tick：`SessionCore::reprobe_tick` → `reprobe::prepare`／送出前重驗 → login probe →
// `confirm_ready`；`Service::rpc("session.self_test")` → `SessionRequest::SelfTest` → `self_test_pre_probe`。
// ---------------------------------------------------------------------------

/// FakeClock 的牆鐘起點；`ready_shell` 的 bootstrap 確認都發生在這一刻。
const WP56_EPOCH: &str = "1970-01-01T00:00:00+00:00";

/// `ready_shell` 的 COM0 於 bootstrap 確認 READY、FakeClock 尚未推進時的完整公開 session，鍵序依 Python
/// `SessionRuntime.to_public_dict()`（`origin/main:sw_core/session_manager.py:850-925`）。值由 rig 組態推得：
/// template `t`（shell、無 ready_probe 覆寫 → 預設 nonce probe，command_capable）綁 `/s/by-id/usb-A`
/// （→ `/fake/tty-A`），alias 為 `<profile>+<act_no>`；bootstrap RX 為 `\n` 的 prompt（10 B）＋nonce 回應
/// `__READY__<8 hex>\r\nroot@t:/# `（29 B）＝39 B，10 s 速率 3 B/s；mono 0 的 RX／TX 依 Python `> 0.0` 判定視同未設
/// （idle_for_ms／兩個 age 為 null → `newly-attached`）。`overrides` 只能改既有鍵（打錯鍵即 panic）。
fn wp56_session(overrides: &[(&str, PyValue)]) -> PyDict {
    let mut session = sw_proto::pydict! {
        "session_id" => "t:COM0",
        "profile" => "t",
        "com" => "COM0",
        "alias" => "t+1",
        "act_no" => 1,
        "device_by_id" => BY_ID_A,
        "platform" => "shell",
        "profile_source" => "yaml-target",
        "command_capable" => true,
        "state" => "READY",
        "last_error" => PyValue::Null,
        "last_error_detail" => PyValue::Null,
        "detached_at" => PyValue::Null,
        "last_ready_at" => WP56_EPOCH,
        "vtty" => PyValue::Null,
        "attached_real_path" => REAL_A,
        "bridge_generation" => 1,
        "recovering" => false,
        "interactive_session_id" => PyValue::Null,
        "foreground_busy" => false,
        "fg_cmd_expected_duration_s" => PyValue::Null,
        "console_count" => 0,
        "last_state_change_at" => WP56_EPOCH,
        "last_rx_at" => WP56_EPOCH,
        "last_tx_at" => WP56_EPOCH,
        "last_probe_at" => WP56_EPOCH,
        "reprobe_attempts" => 0,
        "next_reprobe_at" => PyValue::Null,
        "reprobe_exhausted" => false,
        "boot_quiet_remaining_s" => PyValue::Null,
        "ready_reconfirm_pending" => false,
        "ready_reconfirm_failed" => false,
        "ready_reconfirm_remaining_s" => PyValue::Null,
        "boot_evidence_wait_remaining_s" => PyValue::Null,
        "idle_for_ms" => PyValue::Null,
        "last_rx_age_s" => PyValue::Null,
        "last_tx_age_s" => PyValue::Null,
        "rx_bytes_last_10s" => 39,
        "rx_rate_bps" => 3,
        "outstanding_commands" => 0,
        "activity_classification" => "newly-attached",
        "released_by" => PyValue::Null,
        "released_at" => PyValue::Null,
        "released_reason" => PyValue::Null,
        "capture" => PyValue::Null,
        "drain_stalled" => false,
        "drain_timeout_epoch" => PyValue::Null,
    };
    for (key, value) in overrides {
        assert!(session.get(key).is_some(), "未知 session 欄位 {key}");
        session.insert(key, value.clone());
    }
    session
}

/// 對照 `tests/test_async_reboot_evidence.py` 的 `TestStaleShellNonceBeforeBanner`、`TestBannerReleasesWait`、
/// `TestSubmitTimeGateDuringWait` 與 self-test 分類（`session_manager.py:7816`）：RPC 提交 `reboot`，假 DUT 立即重印
/// prompt → 命令正常完成、session 公開 `boot_evidence_wait_remaining_s=60.0`；舊 shell 持續能應答 `\n` 與 nonce，
/// 但 5 s 內 UART 零 TX；第二筆 agent 命令以 pending-only `AUTOBOOT_QUIET` 拒絕（無 cmd_id、UART／WAL／registry／
/// queue／state.json 不變）；self-test 回 `AUTOBOOT_QUIET`／`wait` 且零 TX；RX 見 U-Boot banner 即解除等待並重臂
/// quiet（仍零 TX），開機完成的 prompt 解除 quiet 後確認 probe 真正送上 UART、舊 shell 應答 nonce → `confirm_ready`，
/// 後續命令經 RPC 寫到 UART 並完成。各階段回應以完整預期 dict 的 RPC_WIRE 位元組比對；僅確認後由時序決定的
/// 時間戳／年齡／RX 計數列為動態欄位（取實測並檢查已更新）。
#[test]
fn wp56_rpc_async_reboot_evidence_wait() {
    let r = ServiceRig::ready_shell();
    let dev = r.device(REAL_A);
    r.settle();
    assert_eq!(
        j(&r.session("COM0").expect("COM0 session")),
        j(&wp56_session(&[])),
        "起點：bootstrap 確認後的 READY"
    );

    // (1) RPC 提交 reboot；假 DUT 在 2 s 內重印 prompt（非同步 reboot：舊 shell 尚未關閉）。
    dev.on_tx_reply(b"reboot\n", b"reboot\r\nroot@t:/# ");
    let tx_mark = dev.tx_len();
    let response = r.call(
        "command.submit",
        r#"{"selector":"COM0","cmd":"reboot","timeout_s":10}"#,
    );
    let cmd_id = s(&response, "cmd_id").to_string();
    assert_eq!(
        j(&response),
        wp52_submit_echo(&r, &cmd_id, false),
        "reboot submit 回聲"
    );
    let measured = wp55_wait_done(&r, &cmd_id);
    let fixed = sw_proto::pydict! {
        "command" => "reboot",
        "source" => "agent",
        "mode" => "line",
        "execution_mode" => "line",
        "timeout_s" => 10.0,
        "expected_duration_s" => PyValue::Null,
        "completion_regex" => PyValue::Null,
        "expect_reboot" => false,
        "priority" => 10,
        "status" => "done",
        "error_code" => PyValue::Null,
        "stdout" => "",
        "partial" => false,
        "background_capture_id" => PyValue::Null,
        "interactive_session_id" => PyValue::Null,
        "recovery_action" => PyValue::Null,
    };
    assert_eq!(
        j(&r.call("command.get", &format!(r#"{{"cmd_id":"{cmd_id}"}}"#))),
        wp53_expected_get(&r, &measured, &fixed),
        "reboot 正常完成（非 recovery）"
    );
    assert_eq!(
        String::from_utf8_lossy(&dev.tx_since(tx_mark)),
        "reboot\n",
        "UART 只送出 reboot 一行"
    );
    r.settle();
    // RX 多了 `reboot\r\nroot@t:/# `（18 B）→ 57 B／5 B/s；clear_quiet＋begin：quiet 為 null、pending 保留、寬限 60 s。
    let waiting = |reconfirm: f64, evidence: f64| {
        wp56_session(&[
            ("ready_reconfirm_pending", PyValue::Bool(true)),
            ("ready_reconfirm_remaining_s", PyValue::Float(reconfirm)),
            ("boot_evidence_wait_remaining_s", PyValue::Float(evidence)),
            ("rx_bytes_last_10s", PyValue::from(57)),
            ("rx_rate_bps", PyValue::from(5)),
        ])
    };
    assert_eq!(
        j(&r.session("COM0").expect("COM0 session")),
        j(&waiting(300.0, 60.0)),
        "reboot 回 prompt：等待欄位可見"
    );

    // (2) 舊 shell 仍活著：空行回 prompt、nonce 照常回顯（`install_shell_replies` 常駐）——若送出確認 probe 必定被
    // 舊 shell 應答而誤確認；等待期間 actor 每秒 Tick 都不得送出。
    dev.on_tx_match("^$", Box::new(|_line: &[u8]| b"root@t:/# ".to_vec()));
    let calls = dev.write_calls();
    let tx_mark = dev.tx_len();
    wp56_advance_secs(&r, 5);
    assert_eq!(
        dev.write_calls(),
        calls,
        "等待中 reprobe 不送確認 probe（PAL 寫入零新增）"
    );
    assert!(dev.tx_since(tx_mark).is_empty(), "等待中 UART 零 TX");
    assert_eq!(
        j(&r.session("COM0").expect("COM0 session")),
        j(&waiting(295.0, 55.0)),
        "等待不加 attempts、不排 backoff、不延長 reconfirm 期限"
    );

    // (3) 第二筆 agent 命令：pending-only 分支（`service.py:829-838`），不產生 cmd_id、零副作用。
    let rejected = sw_proto::pydict! {
        "ok" => false,
        "error_code" => "AUTOBOOT_QUIET",
        "retry_after_s" => 5.0,
        "hint" => WP53_RECONFIRM_HINT,
        "session" => PyValue::Dict(waiting(295.0, 55.0)),
        "version" => r.version.clone(),
    };
    assert_wp52_rpc_rejected_without_side_effects(
        &r,
        r#"{"selector":"COM0","cmd":"uname -a","timeout_s":10}"#,
        &j(&rejected),
    );

    // (4) self-test：等待中不送 nonce probe，回 AUTOBOOT_QUIET／wait（`session_manager.py:7816`），外層補
    // `_lease_context(None)` 與 `command_capable`（`:7600-7617`）；UART／WAL／registry／queue／state.json 不變。
    let before = wp52_rpc_snapshot(&r);
    let self_test = r.call("session.self_test", r#"{"selector":"COM0"}"#);
    let expected_self_test = sw_proto::pydict! {
        "ok" => true,
        "classification" => "AUTOBOOT_QUIET",
        "session" => PyValue::Dict(waiting(295.0, 55.0)),
        "recommended_action" => "wait",
        "interactive_owner" => PyValue::Null,
        "human_attached" => false,
        "human_active" => false,
        "recovery_mode" => false,
        "command_capable" => true,
        "version" => r.version.clone(),
    };
    assert_eq!(
        j(&self_test),
        j(&expected_self_test),
        "self-test 於等待中的分類"
    );
    r.settle();
    assert_eq!(wp52_rpc_snapshot(&r), before, "self-test 零 TX、零副作用");
    assert_eq!(
        j(&r.session("COM0").expect("COM0 session")),
        j(&waiting(295.0, 55.0)),
        "self-test 不更新 last_probe_at"
    );

    // (5) 新開機 banner（t=5 s）：解除證據等待、重臂 boot quiet（180 s），pending 保留；quiet 中仍零 TX。
    dev.push_rx(b"\r\nU-Boot 2024.01 (Jan 01)\r\n");
    wait_until("banner 解除等待", || {
        r.session("COM0")
            .and_then(|session| session.get("boot_evidence_wait_remaining_s").cloned())
            == Some(PyValue::Null)
    });
    r.settle();
    // banner 27 B → 84 B／8 B/s；RX 剛到：idle 0、rx age 0.0、active。
    assert_eq!(
        j(&r.session("COM0").expect("COM0 session")),
        j(&wp56_session(&[
            ("last_rx_at", PyValue::from("1970-01-01T00:00:05+00:00")),
            ("boot_quiet_remaining_s", PyValue::Float(180.0)),
            ("ready_reconfirm_pending", PyValue::Bool(true)),
            ("ready_reconfirm_remaining_s", PyValue::Float(295.0)),
            ("idle_for_ms", PyValue::from(0)),
            ("last_rx_age_s", PyValue::Float(0.0)),
            ("rx_bytes_last_10s", PyValue::from(84)),
            ("rx_rate_bps", PyValue::from(8)),
            ("activity_classification", PyValue::from("active")),
        ])),
        "banner 即解除等待並重臂 quiet"
    );
    wp56_advance_secs(&r, 3);
    assert_eq!(dev.write_calls(), calls, "banner 後 quiet 中仍不送 probe");

    // (6) 開機完成的 prompt（t=8 s）：解除 quiet，pending 仍在，尚未送 probe。
    dev.push_rx(b"\r\nroot@t:/# ");
    wait_until("prompt 解除 quiet", || {
        r.session("COM0")
            .and_then(|session| session.get("boot_quiet_remaining_s").cloned())
            == Some(PyValue::Null)
    });
    r.settle();
    assert_eq!(
        j(&r.session("COM0").expect("COM0 session")),
        j(&wp56_session(&[
            ("last_rx_at", PyValue::from("1970-01-01T00:00:08+00:00")),
            ("ready_reconfirm_pending", PyValue::Bool(true)),
            ("ready_reconfirm_remaining_s", PyValue::Float(292.0)),
            ("idle_for_ms", PyValue::from(0)),
            ("last_rx_age_s", PyValue::Float(0.0)),
            ("rx_bytes_last_10s", PyValue::from(96)),
            ("rx_rate_bps", PyValue::from(9)),
            ("activity_classification", PyValue::from("active")),
        ])),
        "prompt 解除 quiet"
    );
    assert_eq!(
        dev.write_calls(),
        calls,
        "reboot 後到開機完成之間 UART 零 TX"
    );

    // (7) RX 閒置後 reprobe 送確認 probe：`\n`＋`echo __READY__<nonce>`，舊 shell 回顯 nonce → confirm_ready。
    let mut session = PyDict::default();
    wait_until("開機完成後確認 READY", || {
        r.clock.advance(Duration::from_millis(250));
        session = r.session("COM0").expect("COM0 session");
        session.get("ready_reconfirm_pending") == Some(&PyValue::Bool(false))
    });
    r.settle();
    let tx = String::from_utf8_lossy(&dev.tx_since(tx_mark)).into_owned();
    let nonce = tx
        .strip_prefix("\necho __READY__")
        .and_then(|rest| rest.strip_suffix('\n'))
        .unwrap_or_else(|| panic!("確認 probe 應為 `\\n`＋nonce probe：{tx:?}"));
    assert!(
        nonce.len() == 8 && nonce.bytes().all(|b| b.is_ascii_hexdigit()),
        "nonce 為 8 位 hex：{tx:?}"
    );
    let session = r.session("COM0").expect("COM0 session");
    // 只有確認時刻由輪詢步進決定的欄位取實測；確認點必須已更新 last_ready_at／last_probe_at／last_tx_at。
    let dynamic = [
        "last_ready_at",
        "last_rx_at",
        "last_tx_at",
        "last_probe_at",
        "idle_for_ms",
        "last_rx_age_s",
        "last_tx_age_s",
        "rx_bytes_last_10s",
        "rx_rate_bps",
    ];
    let mut overrides = vec![("activity_classification", PyValue::from("active"))];
    for key in dynamic {
        let value = session
            .get(key)
            .cloned()
            .unwrap_or_else(|| panic!("缺 {key}：{}", j(&session)));
        assert!(!value.is_null(), "{key} 應已填值：{}", j(&session));
        overrides.push((key, value));
    }
    for key in ["last_ready_at", "last_tx_at", "last_probe_at"] {
        assert_ne!(
            s(&session, key),
            WP56_EPOCH,
            "{key} 須於確認時更新：{}",
            j(&session)
        );
    }
    assert_eq!(
        j(&session),
        j(&wp56_session(&overrides)),
        "確認後 READY：pending／等待／quiet 全清、last_error 為 null"
    );

    // (8) 確認後放行：後續 agent 命令經 RPC 真正寫到 UART 並完成。
    dev.on_tx_reply(b"echo hi\n", b"echo hi\r\nhi\r\nroot@t:/# ");
    let tx_mark = dev.tx_len();
    let response = r.call(
        "command.submit",
        r#"{"selector":"COM0","cmd":"echo hi","timeout_s":10}"#,
    );
    let next_id = s(&response, "cmd_id").to_string();
    assert_eq!(
        j(&response),
        wp52_submit_echo(&r, &next_id, false),
        "後續命令 submit 回聲"
    );
    let measured = wp55_wait_done(&r, &next_id);
    let mut fixed = fixed;
    fixed.insert("command", PyValue::from("echo hi"));
    fixed.insert("stdout", PyValue::from("hi"));
    assert_eq!(
        j(&r.call("command.get", &format!(r#"{{"cmd_id":"{next_id}"}}"#))),
        wp53_expected_get(&r, &measured, &fixed),
        "後續命令完成"
    );
    assert_eq!(String::from_utf8_lossy(&dev.tx_since(tx_mark)), "echo hi\n");
}

// ---------------------------------------------------------------------------
// WP5-8 第 1 項（G11，S1X-a-1）：interactive 命令未完整送出時收回本命令開的 lease（RPC 端到端）。
// 基準 `origin/main:sw_core/session_manager.py:6790-6822`（`except BaseException` 內僅在
// `session.interactive_session_id == lease.interactive_id` 時 `_close_interactive_locked`）。呼叫路徑：
// `Service::rpc(command.submit {mode:"interactive"})` → `SessionCore::submit` → `start_next` interactive 分支
// （`open_lease`＋`PendingOp::InteractiveCmd { lease_id }`）→ `OpRunner` `OpSpec::InteractiveOpenCmd` → `lease_send`
// → `TxWriter` 寫 FakeDevice（I/O 錯誤）→ `OpFinished` → `on_op_finished` 比對 lease id 後 `close_lease(false)`
// （`LeaseIndex(Remove)` 交 `SessionManager`）→ `command.get`／`session.get_state`／下一筆 `command.submit`。
// ---------------------------------------------------------------------------

#[test]
fn wp58_g11_rpc_failed_interactive_submit_releases_lease() {
    use sw_pal::fakes::FakeWriteStep;
    use sw_pal::traits::PalError;
    let r = ServiceRig::ready_shell();
    let dev = r.device(REAL_A);
    r.settle();
    dev.script_write(vec![FakeWriteStep::AcceptThenError(
        3,
        PalError::io(5, "[Errno 5] Input/output error"),
    )]);
    let response = r.call(
        "command.submit",
        r#"{"selector":"COM0","cmd":"top","mode":"interactive"}"#,
    );
    let cmd_id = s(&response, "cmd_id").to_string();
    assert_eq!(
        j(&response),
        wp51_submit_success_response(&r, &cmd_id, None, None, "agent"),
        "interactive submit 的完整 RPC 回應"
    );
    let record = wp55_wait_done(&r, &cmd_id);
    // 錯誤碼沿用既有部分寫入映射（`interactive_mode_command_partial_send_maps_aborted_in_flight`）。
    assert_eq!(s(&record, "error_code"), "COMMAND_ABORTED_IN_FLIGHT", "{}", j(&record));
    r.settle();
    let session = r.session("COM0").expect("COM0 session");
    assert_eq!(
        session.get("interactive_session_id"),
        Some(&PyValue::Null),
        "未完整送出後不得遺留本命令開的 lease：{}",
        j(&session)
    );

    // 後續 line 命令不被遺留 lease 擋成 SESSION_INTERACTIVE_BUSY，經 RPC 真正寫到 UART 並完成。
    dev.on_tx_reply(b"ls\n", b"ls\r\nroot@t:/# ");
    let tx_mark = dev.tx_len();
    let response = r.call("command.submit", r#"{"selector":"COM0","cmd":"ls","timeout_s":10}"#);
    assert_eq!(response.get("ok"), Some(&PyValue::Bool(true)), "{}", j(&response));
    let record = wp55_wait_done(&r, s(&response, "cmd_id"));
    assert_eq!(s(&record, "status"), "done", "{}", j(&record));
    assert_eq!(String::from_utf8_lossy(&dev.tx_since(tx_mark)), "ls\n");

    // 下一條 interactive 命令照常送出，其 lease 可經全域 LeaseIndex 以 interactive_id 查到。
    let tx_mark = dev.tx_len();
    let response = r.call(
        "command.submit",
        r#"{"selector":"COM0","cmd":"top","mode":"interactive"}"#,
    );
    let record = wp55_wait_done(&r, s(&response, "cmd_id"));
    assert_eq!(s(&record, "status"), "interactive", "{}", j(&record));
    assert_eq!(String::from_utf8_lossy(&dev.tx_since(tx_mark)), "top\n");
    let lease = s(&record, "interactive_session_id").to_string();
    let status = r.call(
        "session.interactive_status",
        &format!(r#"{{"interactive_id":"{lease}"}}"#),
    );
    assert_eq!(status.get("ok"), Some(&PyValue::Bool(true)), "{}", j(&status));
}

/// WP5-8 第 2 項（G13，S1X-a-3）：RPC `device.attach`（`Service::rpc` → manager 路由 → `SessionCore::attach_request`）
/// 遇一般 attach 登入途中再呼叫一次，回 `{"ok":true,"session"}`（＋RPC 層 `version`），沒有 `error_code`、沒有
/// `already_attached`，也不再開一次埠（Python `tests/test_s1x_a3_attach_inflight.py::
/// test_second_attach_during_login_returns_ok_session`；`origin/main:sw_core/session_manager.py:3428-3443`）。
/// release 後重新 attach：`\n` 的一次性 prompt 應答已在啟動登入時用掉，登入停在等 prompt（FakeClock 不前進不逾時）。
#[test]
fn wp58_g13_rpc_device_attach_during_login_returns_ok_session() {
    let r = ServiceRig::ready_shell();
    let dev = r.device(REAL_A);
    let released = r.call("device.release", r#"{"selector":"COM0"}"#);
    assert_eq!(released.get("ok"), Some(&PyValue::Bool(true)), "{}", j(&released));
    r.settle();
    let opens = dev.open_count();
    let tx_mark = dev.tx_len();

    let first = r.call("device.attach", r#"{"selector":"COM0"}"#);
    assert_eq!(first.get("ok"), Some(&PyValue::Bool(true)), "{}", j(&first));
    assert_eq!(s(&sub(&first, "session"), "state"), "ATTACHING", "{}", j(&first));
    wait_until("attach 登入 probe 已送出", || dev.tx_since(tx_mark).contains(&b'\n'));
    assert_eq!(dev.open_count(), opens + 1);
    assert_eq!(s(&r.session("COM0").expect("COM0"), "state"), "ATTACHING");

    let second = r.call("device.attach", r#"{"selector":"COM0"}"#);
    assert_eq!(keys(&second), vec!["ok", "session", "version"], "{}", j(&second));
    assert_eq!(second.get("ok"), Some(&PyValue::Bool(true)), "{}", j(&second));
    let session = sub(&second, "session");
    assert_eq!(s(&session, "state"), "ATTACHING", "{}", j(&second));
    assert_eq!(s(&session, "com"), "COM0");
    let expected = sw_proto::pydict! {
        "ok" => true,
        "session" => PyValue::Dict(session.clone()),
        "version" => r.version.clone(),
    };
    assert_eq!(j(&second), j(&expected), "在途 attach 的完整 RPC 回應");
    // 回應的 session 即同一時點的公開欄位（session.get_state），逐位元組一致。
    let state = r.session("COM0").expect("COM0");
    assert_eq!(j(&session), j(&state));
    assert_eq!(dev.open_count(), opens + 1, "第二次 attach 不得再開埠");

    // 第一次 attach 的登入照常完成：補上 prompt → ready_probe 應答 → READY。
    dev.push_rx(b"root@t:/# ");
    r.wait_state("COM0", "READY");
    assert_eq!(dev.open_count(), opens + 1);
}

/// WP5-8 第 3 項（G14，S1X-a-7）：RPC `session.clear` 在 READY（有 bridge）且裝置在場時回 `{"ok":true,"session"}`
/// （＋RPC 層 `version`），session 為 `ATTACHING`＋`last_error:null`，不帶 `CLEARED`（裁決 D6，取代審查 F97；
/// `origin/main:sw_core/session_manager.py:2939-2953`、`:3065-3082`；Python `tests/test_s1x_a7_drain_fields.py::
/// test_clear_with_device_present_clears_last_error`）。呼叫路徑：`Service::rpc("session.clear")` →
/// `route(SessionRequest::Clear)` → `SessionCore` clear 請求 → `begin_stop(Clear)`（`BeginDrain`）→ IoWorker 停止後
/// `WorkerStopped` 回條 → `worker_stopped` Clear 分支（`request_reattach` 發 `Effect::Reattach`、裝置在場清
/// `last_error`）→ 回覆 waiter；之後 manager 再解析、送 `DeviceAttached` → 重新開埠並登入回 READY。
#[test]
fn wp58_g14_rpc_clear_ready_with_device_returns_null_last_error() {
    let r = ServiceRig::ready_shell();
    let dev = r.device(REAL_A);
    r.settle();
    let opens = dev.open_count();
    let gen0 = r.session("COM0").expect("COM0").get("bridge_generation").cloned();
    let tx_mark = dev.tx_len();

    let cleared = r.call("session.clear", r#"{"selector":"COM0"}"#);
    assert_eq!(keys(&cleared), vec!["ok", "session", "version"], "{}", j(&cleared));
    assert_eq!(cleared.get("ok"), Some(&PyValue::Bool(true)), "{}", j(&cleared));
    let session = sub(&cleared, "session");
    assert_eq!(s(&session, "com"), "COM0");
    assert_eq!(s(&session, "state"), "ATTACHING", "{}", j(&cleared));
    assert_eq!(session.get("last_error"), Some(&PyValue::Null), "{}", j(&cleared));
    let expected = sw_proto::pydict! {
        "ok" => true,
        "session" => PyValue::Dict(session.clone()),
        "version" => r.version.clone(),
    };
    assert_eq!(j(&cleared), j(&expected), "clear 回應的完整 wire 形狀");

    // 再解析後重新開埠並登入：一次性 `\n` 應答已在啟動登入時用掉，等登入 probe 送出後補 prompt。
    wait_until("clear 後重新開埠", || dev.open_count() > opens);
    wait_until("重新 attach 的登入 probe", || dev.tx_since(tx_mark).contains(&b'\n'));
    dev.push_rx(b"root@t:/# ");
    r.wait_state("COM0", "READY");
    let state = r.session("COM0").expect("COM0");
    assert_eq!(state.get("last_error"), Some(&PyValue::Null), "{}", j(&state));
    assert_ne!(state.get("bridge_generation").cloned(), gen0, "以新 bridge 重建");
}

/// WP5-8 第 4 項（G15，S1X-a-7）：RPC `device.release` 的 drain 收尾更新 `detached_at` 並重置 reprobe 進度
/// （`origin/main:sw_core/session_manager.py:2920-2927`、`reset_reprobe_progress` `:651-654`；Python
/// `tests/test_s1x_a7_drain_fields.py::test_release_refreshes_detached_at_and_resets_reprobe`）。
/// 前置以真實 tick 累積進度：release 後重新 attach，一次性 `\n` 應答已用掉 → 登入停在 ATTACHED＋
/// `PROMPT_UNAVAILABLE`，自動重探逐次失敗（attempts 遞增、`next_reprobe_at` 非 null）；`reprobe_exhausted` 由
/// 非 false 歸零的路徑見 sw-core `drain_release.rs` 的 `wp58_g15_*`（CoreHarness 以 bootloader 尾行累積）。呼叫路徑：`Service::rpc("device.release")` →
/// `route(SessionRequest::Release)` → `SessionCore` release 請求 → `begin_stop(Release)`（`BeginDrain`）→ IoWorker
/// 停止後 `WorkerStopped` 回條 → `worker_stopped` Release 分支 → `finish_release`（`detached_at`＋`reprobe::reset`）
/// → `persist_release` 落盤後回覆 waiter；之後 `session.get_state` 讀到同一組欄位。
#[test]
fn wp58_g15_rpc_release_refreshes_detached_at_and_resets_reprobe() {
    use sw_pal::traits::Clock;
    let r = ServiceRig::ready_shell();
    let released = r.call("device.release", r#"{"selector":"COM0"}"#);
    assert_eq!(released.get("ok"), Some(&PyValue::Bool(true)), "{}", j(&released));
    r.settle();
    let attached = r.call("device.attach", r#"{"selector":"COM0"}"#);
    assert_eq!(attached.get("ok"), Some(&PyValue::Bool(true)), "{}", j(&attached));

    let mut session = r.session("COM0").expect("COM0");
    for _ in 0..30 {
        if session.get("reprobe_attempts") != Some(&PyValue::from(0)) {
            break;
        }
        wp56_advance_secs(&r, 1);
        session = r.session("COM0").expect("COM0");
    }
    assert_eq!(s(&session, "state"), "ATTACHED", "{}", j(&session));
    assert_ne!(session.get("reprobe_attempts"), Some(&PyValue::from(0)), "前置：attempts 非零：{}", j(&session));
    assert!(
        matches!(session.get("next_reprobe_at"), Some(PyValue::Float(_))),
        "前置：next_reprobe_at 非 null：{}",
        j(&session)
    );
    assert_eq!(s(&session, "last_error"), "PROMPT_UNAVAILABLE", "{}", j(&session));
    // 舊值為首次 release 收尾的時間戳（修正前該處不寫入，為 null）；兩者都必須被本次收尾取代。
    let old_detached_at = session.get("detached_at").cloned();

    // 推進 wall，讓收尾寫入的時間戳可與舊值區分。
    r.clock.set_wall_us(r.clock.wall_unix_us() + 3_600_000_000);
    wp56_advance_secs(&r, 1);
    let response = r.call("device.release", r#"{"selector":"COM0"}"#);
    assert_eq!(
        keys(&response),
        vec!["ok", "session", "closed_consoles", "aborted_cmd", "version"],
        "{}",
        j(&response)
    );
    assert_eq!(response.get("ok"), Some(&PyValue::Bool(true)), "{}", j(&response));
    let session = sub(&response, "session");
    assert_eq!(s(&session, "state"), "RELEASED", "{}", j(&response));
    assert_eq!(s(&session, "last_error"), "RELEASED", "{}", j(&response));
    assert_ne!(session.get("detached_at").cloned(), old_detached_at, "detached_at 須更新：{}", j(&response));
    assert_eq!(session.get("detached_at"), session.get("released_at"), "detached_at＝收尾時刻：{}", j(&response));
    assert_eq!(session.get("reprobe_attempts"), Some(&PyValue::from(0)), "{}", j(&response));
    assert_eq!(session.get("reprobe_exhausted"), Some(&PyValue::Bool(false)), "{}", j(&response));
    assert_eq!(session.get("next_reprobe_at"), Some(&PyValue::Null), "{}", j(&response));
    let expected = sw_proto::pydict! {
        "ok" => true,
        "session" => PyValue::Dict(session.clone()),
        "closed_consoles" => 0,
        "aborted_cmd" => false,
        "version" => r.version.clone(),
    };
    assert_eq!(j(&response), j(&expected), "release 回應的完整 wire 形狀");
    // 回應的 session 即同一時點的公開欄位（session.get_state），逐位元組一致。
    let state = r.session("COM0").expect("COM0");
    assert_eq!(j(&session), j(&state));
}

/// 以 ServiceRig 的 RPC session 快照組成完整 SESSION_RECOVERING 預期；session 動態欄位沿用同一 fixture，
/// 外層鍵與值則逐位元組核對 `RPC_WIRE`，對應 Python `device.attach` 的 drain／release 在途回應。
fn wp58_assert_device_attach_recovering(r: &ServiceRig, response: &PyDict) {
    assert_eq!(response.get("ok"), Some(&PyValue::Bool(false)), "{}", j(response));
    assert_eq!(s(response, "error_code"), "SESSION_RECOVERING", "{}", j(response));
    assert_eq!(response.get("drain_stalled"), Some(&PyValue::Bool(false)), "{}", j(response));
    assert_eq!(response.get("drain_timeout_epoch"), Some(&PyValue::Null), "{}", j(response));
    let session = sub(response, "session");
    assert_eq!(s(&session, "state"), "RECOVERING", "{}", j(response));
    assert_eq!(j(&session), j(&r.session("COM0").expect("COM0 session")),
        "RPC 回應的 session 必須等於同一時點的 session.get_state");
    let expected = sw_proto::pydict! {
        "ok" => false,
        "error_code" => "SESSION_RECOVERING",
        "drain_stalled" => false,
        "drain_timeout_epoch" => PyValue::Null,
        "session" => PyValue::Dict(session),
        "version" => r.version.clone(),
    };
    assert_eq!(j(response), j(&expected), "SESSION_RECOVERING 完整 RPC 回應");
}

/// 正式 RPC 接線：`device.attach` 遇 `session.clear` drain 等待已提交 UART write 收尾時，仍回完整
/// `SESSION_RECOVERING`；放開 FakeDevice 後 clear 完成並重新回 READY。這與 G13 的一般登入在途成功分流互補。
#[test]
fn wp58_rpc_device_attach_during_clear_drain_returns_session_recovering() {
    use std::sync::Arc;
    use sw_pal::fakes::FakeWriteStep;

    let r = Arc::new(ServiceRig::ready_shell());
    let dev = r.device(REAL_A);
    let tx_mark = dev.tx_len();
    dev.script_write(vec![FakeWriteStep::BlockForever]);
    let submitted = r.call(
        "command.submit",
        r#"{"selector":"COM0","cmd":"echo wp58-drain","timeout_s":10}"#,
    );
    let cmd_id = s(&submitted, "cmd_id").to_string();
    assert_eq!(j(&submitted), wp51_submit_success_response(&r, &cmd_id, None, None, "agent"),
        "被 drain 的命令 submit 完整 RPC 回應");
    dev.wait_blocked();
    assert_eq!(String::from_utf8_lossy(&dev.tx_since(tx_mark)), "echo wp58-drain\n");
    let opens_before_clear = dev.open_count();
    let reattach_tx_mark = dev.tx_len();

    let clear_rig = Arc::clone(&r);
    let clear = std::thread::spawn(move || clear_rig.call("session.clear", r#"{"selector":"COM0"}"#));
    wait_until("clear drain 進入 RECOVERING", || {
        r.session("COM0")
            .and_then(|session| session.get("state").and_then(PyValue::as_str).map(str::to_string))
            .as_deref()
            == Some("RECOVERING")
    });

    let attach = r.call("device.attach", r#"{"selector":"COM0"}"#);
    wp58_assert_device_attach_recovering(&r, &attach);
    assert_eq!(String::from_utf8_lossy(&dev.tx_since(tx_mark)), "echo wp58-drain\n", "在途 attach 零額外 TX");

    dev.release_block();
    let clear_response = clear.join().expect("clear RPC thread");
    assert_eq!(clear_response.get("ok"), Some(&PyValue::Bool(true)), "{}", j(&clear_response));
    wait_until("clear drain 重開 UART", || dev.open_count() > opens_before_clear);
    wait_until("clear drain 重開後送登入 probe", || dev.tx_since(reattach_tx_mark).contains(&b'\n'));
    dev.push_rx(b"root@t:/# ");
    wait_until("clear 後重掛回 READY", || {
        r.session("COM0")
            .and_then(|session| session.get("state").and_then(PyValue::as_str).map(str::to_string))
            .as_deref()
            == Some("READY")
    });
    assert_eq!(s(&wp55_wait_done(&r, &cmd_id), "status"), "error");
}

/// 正式 RPC 接線：`device.attach` 遇 `device.release` 尚在等待 worker 關閉時仍回完整
/// `SESSION_RECOVERING`；放開 FakeDevice 後 release 成功並停在 RELEASED。
#[test]
fn wp58_rpc_device_attach_during_release_returns_session_recovering() {
    use std::sync::Arc;
    use sw_pal::fakes::FakeWriteStep;

    let r = Arc::new(ServiceRig::ready_shell());
    let dev = r.device(REAL_A);
    let tx_mark = dev.tx_len();
    dev.script_write(vec![FakeWriteStep::BlockForever]);
    let submitted = r.call(
        "command.submit",
        r#"{"selector":"COM0","cmd":"echo wp58-release","timeout_s":10}"#,
    );
    let cmd_id = s(&submitted, "cmd_id").to_string();
    assert_eq!(j(&submitted), wp51_submit_success_response(&r, &cmd_id, None, None, "agent"),
        "被 release 的命令 submit 完整 RPC 回應");
    dev.wait_blocked();
    assert_eq!(String::from_utf8_lossy(&dev.tx_since(tx_mark)), "echo wp58-release\n");

    let release_rig = Arc::clone(&r);
    let release = std::thread::spawn(move || release_rig.call("device.release", r#"{"selector":"COM0"}"#));
    wait_until("release drain 進入 RECOVERING", || {
        r.session("COM0")
            .and_then(|session| session.get("state").and_then(PyValue::as_str).map(str::to_string))
            .as_deref()
            == Some("RECOVERING")
    });

    let attach = r.call("device.attach", r#"{"selector":"COM0"}"#);
    wp58_assert_device_attach_recovering(&r, &attach);
    assert_eq!(String::from_utf8_lossy(&dev.tx_since(tx_mark)), "echo wp58-release\n", "在途 attach 零額外 TX");

    dev.release_block();
    let released = release.join().expect("release RPC thread");
    assert_eq!(released.get("ok"), Some(&PyValue::Bool(true)), "{}", j(&released));
    r.wait_state("COM0", "RELEASED");
    assert_eq!(s(&wp55_wait_done(&r, &cmd_id), "status"), "error");
}

const WP510_PLACEHOLDER_SID: &str = "gone-template:COM3";
const WP510_PLACEHOLDER_ALIAS: &str = "gone-template+4";
const WP510_PLACEHOLDER_BY_ID: &str = "/s/by-id/usb-G";
const WP510_FREE_BY_ID: &str = "/s/by-id/usb-C";
const WP510_REAL_G: &str = "/fake/tty-G";
const WP510_REAL_C: &str = "/fake/tty-C";

struct Wp510GeneralOpenErrorBackend {
    delegate: Arc<FakeSerialBackend>,
    remaining: AtomicUsize,
    failed: AtomicUsize,
}

fn wp510_install_shell_replies(dev: &FakeDevice) {
    dev.on_tx_reply(b"\n", b"root@t:/# ");
    dev.on_tx_match(
        r"__READY__[0-9a-f]+",
        Box::new(|line: &[u8]| {
            let text = String::from_utf8_lossy(line).into_owned();
            let token = text
                .split("__READY__")
                .nth(1)
                .unwrap_or("")
                .trim()
                .to_string();
            format!("__READY__{token}\r\nroot@t:/# ").into_bytes()
        }),
    );
}

impl SerialBackend for Wp510GeneralOpenErrorBackend {
    fn open_exclusive(
        &self,
        dev: &DevicePath,
        cfg: &SerialConfig,
    ) -> Result<Box<dyn SerialHandle>, OpenError> {
        if dev.0 == WP510_REAL_G
            && self
                .remaining
                .fetch_update(Ordering::SeqCst, Ordering::SeqCst, |n| n.checked_sub(1))
                .is_ok()
        {
            self.failed.fetch_add(1, Ordering::SeqCst);
            return Err(OpenError::Other(PalError::plain(
                PalErrorKind::PermissionDenied,
                "Permission denied",
            )));
        }
        self.delegate.open_exclusive(dev, cfg)
    }
}

fn wp510_rpc_open_error_rig(
    with_fallback: bool,
) -> (ServiceRig, Arc<Wp510GeneralOpenErrorBackend>, FakeDevice) {
    let delegate = FakeSerialBackend::new();
    for path in [REAL_A, REAL_B, WP510_REAL_G, WP510_REAL_C] {
        wp510_install_shell_replies(&delegate.add_device(path));
    }
    let placeholder_device = delegate.add_device(WP510_REAL_G);
    let backend = Arc::new(Wp510GeneralOpenErrorBackend {
        delegate,
        remaining: AtomicUsize::new(2),
        failed: AtomicUsize::new(0),
    });
    let serial: Arc<dyn SerialBackend> = backend.clone();
    let rig = ServiceRig::build_with_loaded_state_and_serial_backend(
        wp510_b_placeholder_spec(with_fallback),
        wp510_b_placeholder_loaded_state(),
        serial,
    );
    (rig, backend, placeholder_device)
}

struct Wp510BlockingOpenBackend {
    delegate: Arc<FakeSerialBackend>,
    path: String,
    entered: crossbeam_channel::Sender<()>,
    gate: Arc<(Mutex<bool>, Condvar)>,
    error_after_gate: Mutex<Option<OpenError>>,
}

impl Wp510BlockingOpenBackend {
    fn release(&self) {
        let (released, cv) = &*self.gate;
        *released.lock().unwrap_or_else(|e| e.into_inner()) = true;
        cv.notify_all();
    }
}

impl SerialBackend for Wp510BlockingOpenBackend {
    fn open_exclusive(
        &self,
        dev: &DevicePath,
        cfg: &SerialConfig,
    ) -> Result<Box<dyn SerialHandle>, OpenError> {
        if dev.0 == self.path {
            let _ = self.entered.send(());
            let (released, cv) = &*self.gate;
            let mut open = released.lock().unwrap_or_else(|e| e.into_inner());
            while !*open {
                open = cv.wait(open).unwrap_or_else(|e| e.into_inner());
            }
            if let Some(error) = self
                .error_after_gate
                .lock()
                .unwrap_or_else(|e| e.into_inner())
                .take()
            {
                return Err(error);
            }
        }
        self.delegate.open_exclusive(dev, cfg)
    }
}

fn wp510_b_placeholder_blocking_rig() -> (
    ServiceRig,
    Arc<Wp510BlockingOpenBackend>,
    crossbeam_channel::Receiver<()>,
    FakeDevice,
) {
    wp510_b_placeholder_blocking_rig_with_open_error(None)
}

fn wp510_b_placeholder_blocking_rig_with_open_error(
    error_after_gate: Option<OpenError>,
) -> (
    ServiceRig,
    Arc<Wp510BlockingOpenBackend>,
    crossbeam_channel::Receiver<()>,
    FakeDevice,
) {
    wp510_b_placeholder_blocking_rig_with(wp510_b_placeholder_loaded_state(), error_after_gate)
}

fn wp510_b_placeholder_blocking_rig_with(
    loaded: LoadedState,
    error_after_gate: Option<OpenError>,
) -> (
    ServiceRig,
    Arc<Wp510BlockingOpenBackend>,
    crossbeam_channel::Receiver<()>,
    FakeDevice,
) {
    let delegate = FakeSerialBackend::new();
    for path in [REAL_A, REAL_B, WP510_REAL_G, WP510_REAL_C] {
        wp510_install_shell_replies(&delegate.add_device(path));
    }
    let placeholder_device = delegate.add_device(WP510_REAL_G);
    let (entered_tx, entered_rx) = crossbeam_channel::bounded(1);
    let backend = Arc::new(Wp510BlockingOpenBackend {
        delegate,
        path: WP510_REAL_G.to_string(),
        entered: entered_tx,
        gate: Arc::new((Mutex::new(false), Condvar::new())),
        error_after_gate: Mutex::new(error_after_gate),
    });
    let serial: Arc<dyn SerialBackend> = backend.clone();
    let rig = ServiceRig::build_with_loaded_state_and_serial_backend(
        wp510_b_placeholder_spec(false),
        loaded,
        serial,
    );
    (rig, backend, entered_rx, placeholder_device)
}

/// 建立有缺失 template 佔位、另一個在線未綁定裝置與已綁定裝置的 RPC rig。
/// 2 個 YAML session 加 14 個 RELEASED 佔位達 max_sessions，故 usb-C 在線但不會被背景偵測搶先實體化。
fn wp510_b_placeholder_loaded_state() -> LoadedState {
    let mut loaded = LoadedState::default();
    loaded.bindings.insert(
        WP510_PLACEHOLDER_SID.to_string(),
        WP510_PLACEHOLDER_BY_ID.to_string(),
    );
    let mut alias_row = PyDict::new();
    alias_row.insert("session_id", PyValue::from(WP510_PLACEHOLDER_SID));
    alias_row.insert("updated_at", PyValue::from("2026-11-09T00:00:00"));
    loaded.aliases.insert("gone-template+4".to_string(), alias_row);
    loaded.released.insert(
        WP510_PLACEHOLDER_SID.to_string(),
        ReleasedRow {
            by_id: PyValue::from(WP510_PLACEHOLDER_BY_ID),
            released_by: PyValue::from("agent"),
            released_at: PyValue::from("2026-11-08T12:34:56"),
            reason: PyValue::from("manual"),
        },
    );
    for index in 0..13 {
        let sid = format!("missing-template-{index}:COM{}", 100 + index);
        loaded.released.insert(
            sid,
            ReleasedRow {
                by_id: PyValue::from(format!("/s/by-id/offline-{index}")),
                released_by: PyValue::from("agent"),
                released_at: PyValue::from("2026-11-08T12:34:56"),
                reason: PyValue::from("manual"),
            },
        );
    }

    loaded
}

fn wp510_b_placeholder_spec(with_fallback: bool) -> RigSpec {
    let fallback = if with_fallback {
        "  passthrough:\n    platform: passthrough\n    prompt_regex: '.*'\n    ready_probe: ''\n"
    } else {
        ""
    };
    RigSpec {
        yaml: format!(
            "profiles:\n  t:\n    platform: shell\n    prompt_regex: '(?m)^root@t:.*# '\n    timeout_s: 2.0\n{fallback}targets:\n  - {{com: COM0, profile: t, device_by_id: {BY_ID_A}}}\n  - {{com: COM1, profile: t, device_by_id: {BY_ID_B}}}\n"
        ),
        devices: vec![
            (BY_ID_A.to_string(), REAL_A.to_string()),
            (BY_ID_B.to_string(), REAL_B.to_string()),
            (WP510_PLACEHOLDER_BY_ID.to_string(), WP510_REAL_G.to_string()),
            (WP510_FREE_BY_ID.to_string(), WP510_REAL_C.to_string()),
        ],
        explicit_target_count: 2,
        allowlist: None,
        wait_ready: vec!["COM0".to_string(), "COM1".to_string()],
    }
}

fn wp510_b_placeholder_rig() -> ServiceRig {
    ServiceRig::build_with_loaded_state(
        wp510_b_placeholder_spec(false),
        wp510_b_placeholder_loaded_state(),
    )
}

fn wp510_b_assert_placeholder_bind_is_early_and_side_effect_free(r: &ServiceRig, by_id: &str) {
    assert_eq!(
        r.mgr.released_placeholder(WP510_PLACEHOLDER_SID),
        Some(true),
        "測試前置必須是 OD-63 佔位"
    );
    let before_session = r
        .session(WP510_PLACEHOLDER_SID)
        .expect("session_id selector 應找到佔位");
    assert_eq!(s(&before_session, "device_by_id"), WP510_PLACEHOLDER_BY_ID);
    assert_eq!(s(&before_session, "alias"), "gone-template+4");
    for field in ["released_by", "released_at", "released_reason"] {
        assert!(before_session.get(field).is_some(), "佔位缺少 {field}");
    }

    let state_bytes = r.fs.bytes(support::STATE_PATH);
    let state_writes = r.fs.writes();
    let wal_seq = r.wal.current_seq();
    let wal_path = std::path::Path::new("/fake/wal/raw.wal.ndjson");
    let mirror_path = std::path::Path::new("/fake/wal/raw.mirror.log");
    let wal_bytes = r.wfs.contents(wal_path);
    let mirror_bytes = r.wfs.contents(mirror_path);
    let serial: Vec<_> = r
        .devices
        .iter()
        .map(|(real, dev)| {
            (
                real.clone(),
                dev.open_count(),
                dev.write_calls(),
                dev.tx_len(),
            )
        })
        .collect();

    let response = r.call(
        "session.bind",
        &format!(r#"{{"selector":"{WP510_PLACEHOLDER_SID}","device_by_id":"{by_id}"}}"#),
    );
    assert_eq!(
        keys(&response),
        ["ok", "released", "recommended_action", "session", "version"],
        "佔位回應 payload 鍵序須符合 Python dict 插入順序，Service 再附 version：{}",
        j(&response)
    );
    assert_eq!(
        j(&response),
        format!(
            "{{\"ok\":true,\"released\":true,\"recommended_action\":\"device_attach\",\"session\":{},\"version\":\"{}\"}}",
            j(&before_session),
            r.version
        ),
        "佔位 bind 必須完整早退：{}",
        j(&response)
    );
    assert_eq!(
        r.mgr.released_placeholder(WP510_PLACEHOLDER_SID),
        Some(true),
        "bind 不得解除佔位"
    );
    let after_session = r
        .session(WP510_PLACEHOLDER_SID)
        .expect("bind 後佔位仍存在");
    assert_eq!(j(&after_session), j(&before_session), "binding／alias／released metadata 不得變動");
    assert_eq!(r.fs.bytes(support::STATE_PATH), state_bytes, "state.json bytes 不得改變");
    assert_eq!(r.fs.writes(), state_writes, "不得寫 state.json");
    assert_eq!(r.wal.current_seq(), wal_seq, "不得追加 WAL");
    assert_eq!(r.wfs.contents(wal_path), wal_bytes, "WAL bytes 不得改變");
    assert_eq!(r.wfs.contents(mirror_path), mirror_bytes, "WAL mirror bytes 不得改變");
    assert_eq!(
        r.devices
            .iter()
            .map(|(real, dev)| (real.clone(), dev.open_count(), dev.write_calls(), dev.tx_len()))
            .collect::<Vec<_>>(),
        serial,
        "bind 不得開埠或送 UART TX"
    );
}

#[test]
fn wp510_b_bind_placeholder_has_no_side_effects() {
    for by_id in [WP510_PLACEHOLDER_BY_ID, WP510_FREE_BY_ID, BY_ID_B] {
        let rig = wp510_b_placeholder_rig();
        assert!(
            rig.mgr
                .list_devices()
                .iter()
                .any(|device| device.get("by_id") == Some(&PyValue::from(by_id))),
            "{by_id} 必須在線"
        );
        let owners: Vec<String> = rig
            .mgr
            .list_sessions()
            .iter()
            .filter(|session| session.get("device_by_id") == Some(&PyValue::from(by_id)))
            .filter_map(|session| {
                session
                    .get("session_id")
                    .and_then(PyValue::as_str)
                    .map(str::to_string)
            })
            .collect();
        match by_id {
            WP510_PLACEHOLDER_BY_ID => assert_eq!(owners, [WP510_PLACEHOLDER_SID]),
            WP510_FREE_BY_ID => assert!(owners.is_empty(), "usb-C 應在線但未綁定"),
            BY_ID_B => assert!(
                owners.iter().any(|sid| sid != WP510_PLACEHOLDER_SID),
                "usb-B 應由其他 session 使用"
            ),
            _ => unreachable!(),
        }
        wp510_b_assert_placeholder_bind_is_early_and_side_effect_free(&rig, by_id);
    }
}

#[test]
fn wp510_b_bind_placeholder_precedes_inflight_cancellation() {
    let rig = Arc::new(wp510_b_placeholder_rig());
    let cancelled = rig
        .mgr
        .insert_inflight_attach_for_test(WP510_PLACEHOLDER_BY_ID);
    let (reply_tx, reply_rx) = crossbeam_channel::bounded(1);
    let bind_rig = Arc::clone(&rig);
    let bind_thread = std::thread::spawn(move || {
        let reply = bind_rig.call(
            "session.bind",
            &format!(
                r#"{{"selector":"{WP510_PLACEHOLDER_SID}","device_by_id":"{WP510_FREE_BY_ID}"}}"#
            ),
        );
        let _ = reply_tx.send(reply);
    });

    let immediate_reply = reply_rx.recv_timeout(Duration::from_millis(500));
    let waited_for_attach = immediate_reply.is_err();
    if waited_for_attach {
        // 舊路徑會取消後等 attach；移除測試 op 以有限時間收束並取得實際錯誤回應。
        rig.mgr
            .remove_inflight_attach_for_test(WP510_PLACEHOLDER_BY_ID);
    }
    let response = match immediate_reply {
        Ok(reply) => reply,
        Err(_) => reply_rx
            .recv_timeout(Duration::from_secs(2))
            .expect("解除測試在途 op 後 bind 應有限時間結束"),
    };
    bind_thread.join().expect("bind RPC thread");

    assert!(
        !waited_for_attach,
        "佔位 bind 不得等待在途 attach"
    );
    assert!(
        !cancelled.load(Ordering::SeqCst),
        "佔位 bind 不得取消在途 attach"
    );
    assert!(
        rig.mgr
            .has_inflight_attach_for_test(WP510_PLACEHOLDER_BY_ID),
        "佔位 bind 不得移除在途 op"
    );
    assert_eq!(
        keys(&response),
        ["ok", "released", "recommended_action", "session", "version"]
    );
    assert_eq!(
        s(&response, "recommended_action"),
        "device_attach",
        "佔位 bind 應立即回早退結果：{}",
        j(&response)
    );
    let session = rig
        .session(WP510_PLACEHOLDER_SID)
        .expect("早退後佔位仍存在");
    assert_eq!(
        j(&response),
        format!(
            "{{\"ok\":true,\"released\":true,\"recommended_action\":\"device_attach\",\"session\":{},\"version\":\"{}\"}}",
            j(&session),
            rig.version
        )
    );
    rig.mgr
        .remove_inflight_attach_for_test(WP510_PLACEHOLDER_BY_ID);
}

/// 正式 RPC 經 bind 早退後接 device.attach：佔位身份退休、原 COM 建立 template session，並能送命令與寫 WAL。
#[test]
fn wp510_rpc_bind_then_device_attach_replaces_placeholder() {
    let r = wp510_b_placeholder_rig();
    let dev = r.device(WP510_REAL_G);
    // ServiceRig 的預設空行回覆是一次性；交接先用 PROBE 消耗它，正式 bridge 仍需 shell prompt。
    dev.on_tx_match("^$", Box::new(|_| b"root@t:/# ".to_vec()));
    let tx_mark = dev.tx_len();
    let open_mark = dev.open_count();
    assert_eq!(
        r.mgr.resolve(WP510_PLACEHOLDER_SID).as_deref(),
        Some(WP510_PLACEHOLDER_SID)
    );
    assert_eq!(
        s(
            &r.session(WP510_PLACEHOLDER_SID).expect("placeholder"),
            "state"
        ),
        "RELEASED"
    );
    assert_eq!(open_mark, 0, "啟動時的佔位不得開埠");

    wp510_b_assert_placeholder_bind_is_early_and_side_effect_free(&r, WP510_FREE_BY_ID);
    let before_attach = r
        .session(WP510_PLACEHOLDER_SID)
        .expect("bind 早退後佔位仍存在");
    // 佔位於 T0 建立、T1 才 device.attach：Python `attach_device` 於 `origin/main:sw_core/session_manager.py:3599`
    // 設 ATTACHING，state setter（`:617-620`）寫入 T1 後 `:3602` 才產生回應（wp510 第四次整包審查 Medium 3）。
    r.clock.set_wall_us(r.clock.wall_unix_us() + 5_000_000);
    let mut handoff_snapshot = before_attach.clone();
    assert_ne!(
        handoff_snapshot.get("last_state_change_at"),
        Some(&PyValue::from(iso_utc_now(r.clock.wall_unix_us()))),
        "前置：交接前快照不得已是 T1"
    );
    handoff_snapshot.insert("state", PyValue::from("ATTACHING"));
    handoff_snapshot.insert(
        "last_state_change_at",
        PyValue::from(iso_utc_now(r.clock.wall_unix_us())),
    );
    handoff_snapshot.insert("last_error", PyValue::Null);
    handoff_snapshot.insert("released_by", PyValue::Null);
    handoff_snapshot.insert("released_at", PyValue::Null);
    handoff_snapshot.insert("released_reason", PyValue::Null);

    let attached = r.call(
        "device.attach",
        &format!(r#"{{"selector":"{WP510_PLACEHOLDER_SID}"}}"#),
    );
    let expected = sw_proto::pydict! {
        "ok" => true,
        "session" => PyValue::Dict(handoff_snapshot),
        "version" => r.version.clone(),
    };
    assert_eq!(j(&attached), j(&expected), "device.attach RPC bytes");

    assert!(
        r.mgr.resolve(WP510_PLACEHOLDER_SID).is_none(),
        "交接後舊 placeholder 身份必須消失"
    );
    let attach_deadline = std::time::Instant::now() + Duration::from_secs(10);
    while r.mgr.resolve("t:COM3").as_deref() != Some("t:COM3")
        && std::time::Instant::now() < attach_deadline
    {
        std::thread::sleep(Duration::from_millis(2));
    }
    assert_eq!(
        r.mgr.resolve("t:COM3").as_deref(),
        Some("t:COM3"),
        "交接後應由 dynamic worker 以原 COM 建立 template session；sessions={:?}, open_count={}, tx={:?}, response={}",
        r.mgr.list_sessions().iter().map(j).collect::<Vec<_>>(),
        dev.open_count(),
        String::from_utf8_lossy(&dev.tx_since(tx_mark)),
        j(&attached)
    );
    let ready_deadline = std::time::Instant::now() + Duration::from_secs(2);
    while r
        .session("COM3")
        .and_then(|s| s.get("state").and_then(PyValue::as_str).map(str::to_string))
        .as_deref()
        != Some("READY")
        && std::time::Instant::now() < ready_deadline
    {
        std::thread::sleep(Duration::from_millis(2));
    }
    let formal = r.session("COM3").expect("正式 session 狀態");
    assert_eq!(
        s(&formal, "state"),
        "READY",
        "正式 session 應 ready：{}；TX={:?}",
        j(&formal),
        String::from_utf8_lossy(&dev.tx_since(tx_mark))
    );
    assert_eq!(s(&formal, "session_id"), "t:COM3");
    assert_eq!(s(&formal, "com"), "COM3");
    assert_eq!(s(&formal, "profile"), "t");
    assert_eq!(s(&formal, "profile_source"), "detected");
    assert_eq!(dev.open_count(), open_mark + 1, "預開 handle 應只開埠一次");

    let line = b"echo wp510-handoff\n";
    let command_tx_mark = dev.tx_len();
    let submitted = r.call(
        "command.submit",
        r#"{"selector":"COM3","cmd":"echo wp510-handoff","timeout_s":10}"#,
    );
    let cmd_id = s(&submitted, "cmd_id").to_string();
    assert_eq!(
        j(&submitted),
        format!(
            "{{\"ok\":true,\"cmd_id\":\"{cmd_id}\",\"status\":\"accepted\",\"session_id\":\"t:COM3\",\"source_class\":\"agent\",\"version\":\"{}\"}}",
            r.version
        ),
        "正式 session command.submit RPC bytes"
    );
    wait_until("正式 session 命令 TX", || {
        dev.tx_since(command_tx_mark).starts_with(line)
    });
    assert_eq!(
        dev.tx_since(command_tx_mark),
        line,
        "UART 應只送正式命令的一行"
    );
    dev.push_rx(b"\r\nroot@t:/# ");
    let record = wp55_wait_done(&r, &cmd_id);
    assert_eq!(
        s(&record, "status"),
        "done",
        "命令應由正式 bridge 執行：{}",
        j(&record)
    );
    assert_eq!(
        wp55_wal_tx(&r, &cmd_id),
        vec![("agent".to_string(), wp55_b64(line))],
        "正式 session 命令 TX 必須寫入 WAL"
    );
}

#[test]
fn wp510_rpc_placeholder_offline_and_held_remain_blocked() {
    for selector in [WP510_PLACEHOLDER_SID, "COM3", WP510_PLACEHOLDER_ALIAS] {
        let offline = wp510_b_placeholder_rig();
        let snapshot: Vec<_> = offline
            .mgr
            .list_devices()
            .into_iter()
            .filter_map(|row| {
                let by_id = s(&row, "by_id").to_string();
                (by_id != WP510_PLACEHOLDER_BY_ID).then(|| {
                    let real_path = s(&row, "real_path").to_string();
                    (by_id.clone(), DeviceInfo { by_id, real_path })
                })
            })
            .collect();
        offline.mgr.update_devices(&snapshot);
        let offline_response =
            offline.call("device.attach", &format!(r#"{{"selector":"{selector}"}}"#));
        let offline_expected = sw_proto::pydict! {
            "ok" => false,
            "error_code" => "DEVICE_NOT_PRESENT",
            "selector" => selector,
            "device_by_id" => WP510_PLACEHOLDER_BY_ID,
            "version" => offline.version.clone(),
        };
        assert_eq!(
            j(&offline_response),
            j(&offline_expected),
            "離線 device.attach RPC bytes（selector={selector}）"
        );
        assert_eq!(
            offline.mgr.released_placeholder(WP510_PLACEHOLDER_SID),
            Some(true),
            "離線裝置必須保留佔位"
        );
        assert_eq!(offline.device(WP510_REAL_G).open_count(), 0);

        let held = wp510_b_placeholder_rig();
        held.excl.hold(WP510_REAL_G, vec![771]);
        let held_response = held.call("device.attach", &format!(r#"{{"selector":"{selector}"}}"#));
        let held_expected = sw_proto::pydict! {
            "ok" => false,
            "error_code" => "DEVICE_STILL_HELD",
            "pids" => PyValue::List(vec![PyValue::from(771)]),
            "holder_unknown" => false,
            "selector" => selector,
            "version" => held.version.clone(),
        };
        assert_eq!(
            j(&held_response),
            j(&held_expected),
            "holder device.attach RPC bytes（selector={selector}）"
        );
        assert_eq!(
            held.mgr.released_placeholder(WP510_PLACEHOLDER_SID),
            Some(true),
            "holder 衝突必須保留佔位"
        );
        assert_eq!(held.device(WP510_REAL_G).open_count(), 0);
    }
}

#[test]
fn wp510_rpc_general_oserror_handoff_with_and_without_fallback() {
    for with_fallback in [false, true] {
        let (r, backend, dev) = wp510_rpc_open_error_rig(with_fallback);
        let before = r
            .session(WP510_PLACEHOLDER_SID)
            .expect("OSError 測試前置佔位");
        // 佔位於 T0 建立、T1 才 device.attach：Python `origin/main:sw_core/session_manager.py:3535` 設 DETACHED，
        // state setter（`:617-620`）寫入 T1（wp510 第四次整包審查 Medium 3）。
        r.clock.set_wall_us(r.clock.wall_unix_us() + 5_000_000);
        let response = r.call(
            "device.attach",
            &format!(r#"{{"selector":"{WP510_PLACEHOLDER_SID}"}}"#),
        );
        let mut detached = before;
        assert_ne!(
            detached.get("last_state_change_at"),
            Some(&PyValue::from(iso_utc_now(r.clock.wall_unix_us()))),
            "前置：交接前快照不得已是 T1"
        );
        detached.insert("state", PyValue::from("DETACHED"));
        detached.insert(
            "last_state_change_at",
            PyValue::from(iso_utc_now(r.clock.wall_unix_us())),
        );
        detached.insert("last_error", PyValue::from("ATTACH_FAILED:PermissionError"));
        detached.insert(
            "detached_at",
            PyValue::from(iso_utc_now(r.clock.wall_unix_us())),
        );
        detached.insert("released_by", PyValue::Null);
        detached.insert("released_at", PyValue::Null);
        detached.insert("released_reason", PyValue::Null);
        let expected = sw_proto::pydict! {
            "ok" => true,
            "session" => PyValue::Dict(detached),
            "version" => r.version.clone(),
        };
        assert_eq!(
            j(&response),
            j(&expected),
            "一般 OSError 時 device.attach RPC bytes（fallback={with_fallback}）"
        );
        wait_until("dynamic worker 的第二次一般 OSError", || {
            backend.failed.load(Ordering::SeqCst) == 2
        });
        assert!(
            r.mgr.resolve(WP510_PLACEHOLDER_SID).is_none(),
            "一般 OSError 仍需退休舊佔位（fallback={with_fallback}）"
        );
        assert_eq!(
            r.mgr.com_for_by_id(WP510_PLACEHOLDER_BY_ID).as_deref(),
            Some("COM3")
        );
        assert_eq!(dev.open_count(), 0, "兩次一般 OSError 都不得建立 handle");
        assert!(
            dev.tx_since(0).is_empty(),
            "兩次一般 OSError 都不得送 UART TX"
        );

        if with_fallback {
            wait_until("passthrough fallback session", || r.session("COM3").is_some());
            let fallback = r.session("COM3").expect("有 fallback 時保留 COM3 session");
            assert_eq!(s(&fallback, "session_id"), "passthrough:COM3");
            assert_eq!(s(&fallback, "profile_source"), "fallback");
            assert_eq!(s(&fallback, "state"), "DETACHED");
            assert_eq!(s(&fallback, "last_error"), "ATTACH_FAILED:PermissionError");
            assert_eq!(fallback.get("command_capable"), Some(&PyValue::Bool(false)));
        } else {
            assert!(
                r.session("COM3").is_none(),
                "沒有 passthrough fallback 時只保留 COM 預留，不建立 session"
            );
        }
    }
}

/// 正式 RPC 競態：placeholder 開埠停住時，重複 attach 回 SESSION_RECOVERING；release 撤銷並排空開埠，
/// 之後 placeholder 維持 RELEASED，dynamic 偵測與 UART TX 均不得啟動。
#[test]
fn wp510_rpc_release_cancels_inflight_placeholder_attach() {
    let (r, backend, entered, dev) = wp510_b_placeholder_blocking_rig();
    let r = Arc::new(r);
    let before = r
        .session(WP510_PLACEHOLDER_SID)
        .expect("release 測試前置佔位");
    let tx_mark = dev.tx_len();
    let attach_rig = Arc::clone(&r);
    let attach = std::thread::spawn(move || {
        attach_rig.call(
            "device.attach",
            &format!(r#"{{"selector":"{WP510_PLACEHOLDER_SID}"}}"#),
        )
    });
    entered
        .recv_timeout(Duration::from_secs(5))
        .expect("placeholder open_exclusive 必須進入阻塞點");

    let duplicate = r.call(
        "device.attach",
        &format!(r#"{{"selector":"{WP510_PLACEHOLDER_SID}"}}"#),
    );
    let release_rig = Arc::clone(&r);
    let (release_tx, release_rx) = crossbeam_channel::bounded(1);
    let release = std::thread::spawn(move || {
        let response = release_rig.call(
            "device.release",
            &format!(r#"{{"selector":"{WP510_PLACEHOLDER_SID}"}}"#),
        );
        let _ = release_tx.send(response);
    });
    let early_release = release_rx.recv_timeout(Duration::from_millis(100));
    let release_waited = early_release.is_err();
    backend.release();
    let released = match early_release {
        Ok(response) => response,
        Err(_) => release_rx
            .recv_timeout(Duration::from_secs(5))
            .expect("attach 排空後 release 應完成"),
    };
    release.join().expect("release RPC thread");
    let attached = attach.join().expect("attach RPC thread");
    std::thread::sleep(Duration::from_millis(100));

    let recovering = sw_proto::pydict! {
        "ok" => false,
        "error_code" => "SESSION_RECOVERING",
        "drain_stalled" => false,
        "drain_timeout_epoch" => PyValue::Null,
        "session" => PyValue::Dict(before.clone()),
        "version" => r.version.clone(),
    };
    assert!(
        release_waited,
        "device.release 必須等待或逾時回 DRAIN_STALLED，不可忽略 manager 開埠：{}",
        j(&released)
    );
    assert_eq!(j(&duplicate), j(&recovering), "重複 attach 須回完整 recovering RPC");
    assert_eq!(j(&attached), j(&recovering), "被 release 撤銷的 attach 須回完整 recovering RPC");
    assert_eq!(released.get("ok"), Some(&PyValue::Bool(true)), "{}", j(&released));
    assert_eq!(r.mgr.released_placeholder(WP510_PLACEHOLDER_SID), Some(true));
    assert_eq!(
        r.mgr.resolve(WP510_PLACEHOLDER_SID).as_deref(),
        Some(WP510_PLACEHOLDER_SID),
        "release 成功後佔位身份不得退休"
    );
    assert!(r.mgr.resolve("t:COM3").is_none(), "release 成功後不得建立 dynamic session");
    assert_eq!(dev.open_count(), 1, "撤銷後不得再開埠");
    assert!(dev.tx_since(tx_mark).is_empty(), "release 成功後不得啟動偵測或 UART TX");
}

/// 正式 RPC 回歸：佔位開埠超時後 release 回 DRAIN_STALLED；晚到 handle 的 close 回條為 closed:false 時，
/// 必須保留原 epoch 的阻擋與診斷，後續 release／attach 不得重新開埠。
#[test]
fn wp510_rpc_failed_close_keeps_placeholder_drain_stalled() {
    let (rig, backend, entered, dev) = wp510_b_placeholder_blocking_rig();
    let rig = Arc::new(rig);
    let attach_rig = Arc::clone(&rig);
    let attach = std::thread::spawn(move || {
        attach_rig.call(
            "device.attach",
            &format!(r#"{{"selector":"{WP510_PLACEHOLDER_SID}"}}"#),
        )
    });
    entered
        .recv_timeout(Duration::from_secs(5))
        .expect("placeholder open_exclusive 必須進入阻塞點");

    let release_rig = Arc::clone(&rig);
    let release = std::thread::spawn(move || {
        release_rig.call(
            "device.release",
            &format!(r#"{{"selector":"{WP510_PLACEHOLDER_SID}"}}"#),
        )
    });
    let stalled = release.join().expect("第一次 release RPC thread");
    assert_eq!(
        s(&stalled, "error_code"),
        "DRAIN_STALLED",
        "{}",
        j(&stalled)
    );
    assert_eq!(stalled.get("drain_stalled"), Some(&PyValue::Bool(true)));
    let epoch = stalled
        .get("drain_timeout_epoch")
        .cloned()
        .expect("DRAIN_STALLED 必須帶原 timeout epoch");
    assert!(matches!(epoch, PyValue::Int(_)), "epoch={epoch:?}");
    let stalled_session = sub(&stalled, "session");
    assert_eq!(
        stalled_session.get("drain_stalled"),
        Some(&PyValue::Bool(true))
    );
    assert_eq!(stalled_session.get("drain_timeout_epoch"), Some(&epoch));
    let expected_stalled = sw_proto::pydict! {
        "ok" => false,
        "error_code" => "DRAIN_STALLED",
        "drain_stalled" => true,
        "drain_timeout_epoch" => epoch.clone(),
        "session" => PyValue::Dict(stalled_session),
        "version" => rig.version.clone(),
    };
    assert_eq!(
        j(&stalled),
        j(&expected_stalled),
        "第一次 release RPC bytes"
    );

    // 在 timeout 後才注入 close 失敗，再放行開埠；FakeDevice 保留 is_open()==true。
    dev.fail_close(PalError::io(5, "[Errno 5] Input/output error"));
    backend.release();
    let attached = attach.join().expect("被 release 撤銷的 attach RPC thread");
    assert_eq!(
        s(&attached, "error_code"),
        "SESSION_RECOVERING",
        "{}",
        j(&attached)
    );
    assert_eq!(dev.close_count(), 1, "晚到 handle 必須恰好嘗試關閉一次");
    assert!(dev.is_open(), "closed:false 回條表示 handle 仍開啟");
    assert_eq!(dev.open_count(), 1, "首次交接只開埠一次");

    let state = rig
        .session(WP510_PLACEHOLDER_SID)
        .expect("placeholder 仍須可查詢");
    assert_eq!(
        state.get("drain_stalled"),
        Some(&PyValue::Bool(true)),
        "{}",
        j(&state)
    );
    assert_eq!(
        state.get("drain_timeout_epoch"),
        Some(&epoch),
        "{}",
        j(&state)
    );
    assert_eq!(s(&state, "state"), "RELEASED", "{}", j(&state));
    assert_eq!(
        rig.mgr.released_placeholder(WP510_PLACEHOLDER_SID),
        Some(true)
    );
    let attached_session = sub(&attached, "session");
    let expected_attach = sw_proto::pydict! {
        "ok" => false,
        "error_code" => "SESSION_RECOVERING",
        "drain_stalled" => true,
        "drain_timeout_epoch" => epoch.clone(),
        "session" => PyValue::Dict(attached_session),
        "version" => rig.version.clone(),
    };
    assert_eq!(
        j(&attached),
        j(&expected_attach),
        "close 失敗後 attach RPC bytes"
    );

    let repeated_release = rig.call(
        "device.release",
        &format!(r#"{{"selector":"{WP510_PLACEHOLDER_SID}"}}"#),
    );
    assert_eq!(
        s(&repeated_release, "error_code"),
        "DRAIN_STALLED",
        "{}",
        j(&repeated_release)
    );
    assert_eq!(
        repeated_release.get("drain_stalled"),
        Some(&PyValue::Bool(true))
    );
    assert_eq!(repeated_release.get("drain_timeout_epoch"), Some(&epoch));
    let repeated_release_session = sub(&repeated_release, "session");
    let expected_repeated_release = sw_proto::pydict! {
        "ok" => false,
        "error_code" => "DRAIN_STALLED",
        "drain_stalled" => true,
        "drain_timeout_epoch" => epoch.clone(),
        "session" => PyValue::Dict(repeated_release_session),
        "version" => rig.version.clone(),
    };
    assert_eq!(
        j(&repeated_release),
        j(&expected_repeated_release),
        "重複 release RPC bytes"
    );

    let repeated_attach = rig.call(
        "device.attach",
        &format!(r#"{{"selector":"{WP510_PLACEHOLDER_SID}"}}"#),
    );
    assert_eq!(
        s(&repeated_attach, "error_code"),
        "SESSION_RECOVERING",
        "{}",
        j(&repeated_attach)
    );
    assert_eq!(
        repeated_attach.get("drain_stalled"),
        Some(&PyValue::Bool(true))
    );
    assert_eq!(repeated_attach.get("drain_timeout_epoch"), Some(&epoch));
    assert_eq!(dev.open_count(), 1, "close 未確認時不得再次開埠");
    assert!(dev.is_open(), "後續 RPC 不得掩蓋未關閉 handle");
}

/// WAL 中 `com=PROBE`、`dir=TX` 的列數。
fn wp510_probe_tx_rows(r: &ServiceRig) -> usize {
    let bytes = r.wfs.contents(WP55_WAL.as_ref()).unwrap_or_default();
    String::from_utf8(bytes)
        .expect("WAL 應為 UTF-8")
        .lines()
        .filter(|line| {
            let PyValue::Dict(record) = sw_proto::pyjson::loads(line).expect("WAL 行應為 JSON")
            else {
                panic!("WAL 行應為 object：{line}");
            };
            record.get("com") == Some(&PyValue::from("PROBE"))
                && record.get("dir") == Some(&PyValue::from("TX"))
        })
        .count()
}

/// wp510 第三次整包審查 High（RPC 端到端）：`device.attach` → `route_with_selector` → 佔位交接 →
/// dynamic worker 的 PROBE 已越過 `still_valid()`、提交前 shutdown，提交點必須以同一 attach 作業的
/// Fence 拒絕，裝置與 WAL 皆零 PROBE TX（Python `session_manager.py:1186-1193`、`uart_io.py:623-627`）。
#[test]
fn wp510_rpc_shutdown_before_placeholder_probe_submit_sends_zero_tx() {
    let r = wp510_b_placeholder_rig();
    let dev = r.device(WP510_REAL_G);
    let tx_mark = dev.tx_len();
    let weak = Arc::downgrade(&r.mgr);
    let (hook_tx, hook_rx) = crossbeam_channel::bounded(4);
    r.mgr.set_probe_pre_submit_hook_for_test(Box::new(move || {
        if let Some(manager) = weak.upgrade() {
            let drained = manager.shutdown(Duration::from_millis(50));
            let _ = hook_tx.send(drained);
        }
    }));

    let attached = r.call(
        "device.attach",
        &format!(r#"{{"selector":"{WP510_PLACEHOLDER_SID}"}}"#),
    );
    assert_eq!(
        attached.get("ok"),
        Some(&PyValue::Bool(true)),
        "device.attach 應交接在線佔位：{}",
        j(&attached)
    );
    let drained = hook_rx
        .recv_timeout(Duration::from_secs(5))
        .expect("交接後的 PROBE 應在提交前觸發 shutdown");
    assert!(
        r.mgr
            .wait_attach_idle_for_test(WP510_PLACEHOLDER_BY_ID, Duration::from_secs(5)),
        "shutdown 取消的交接 worker 應收尾"
    );

    assert!(
        drained.iter().any(|(target, ok)| {
            target == &format!("attach:{WP510_PLACEHOLDER_BY_ID}") && !ok
        }),
        "PROBE 尚在 worker 內時 shutdown 不得把交接作業列為已排空：{drained:?}"
    );
    assert_eq!(dev.os_submits(), 0, "shutdown 後 PROBE 不得提交任何 bytes 給 OS");
    assert!(
        dev.tx_since(tx_mark).is_empty(),
        "shutdown 後裝置不得收到 PROBE 的 \\n：{:?}",
        String::from_utf8_lossy(&dev.tx_since(tx_mark))
    );
    assert_eq!(wp510_probe_tx_rows(&r), 0, "被撤銷的 PROBE TX 不得寫 WAL");
    assert_eq!(dev.close_count(), 1, "未交接的預開 handle 應關閉一次");
    assert!(!dev.is_open(), "shutdown 取消後不得保留預開 handle");
    assert!(
        r.mgr.resolve("t:COM3").is_none(),
        "shutdown 取消後不得建立正式 session"
    );
}

/// wp510 第三次整包審查 Medium（RPC 端到端）：`alias.set` 已更新 actor、持久化前由 `device.attach` 退休佔位，
/// 回應須為 Python `set_alias_for_session` 的 `SESSION_NOT_FOUND`（`session_manager.py:2612-2620` 全程持
/// manager lock，`:3640-3652` 退休清理同鎖），且 `alias.list` 不得留下指向已退休 sid 的 alias。
#[test]
fn wp510_rpc_alias_set_racing_placeholder_retirement_returns_session_not_found() {
    const LATE_ALIAS: &str = "late-alias";
    let r = wp510_b_placeholder_rig();
    let weak_svc = Arc::downgrade(&r.svc);
    let fired = Arc::new(std::sync::atomic::AtomicBool::new(false));
    let hook_fired = fired.clone();
    let (hook_tx, hook_rx) = crossbeam_channel::bounded(1);
    r.mgr.set_alias_pre_commit_hook_for_test(Box::new(move || {
        if hook_fired.swap(true, Ordering::SeqCst) {
            return;
        }
        if let Some(svc) = weak_svc.upgrade() {
            let PyValue::Dict(params) = sw_proto::pyjson::loads(&format!(
                r#"{{"selector":"{WP510_PLACEHOLDER_SID}"}}"#
            ))
            .expect("params JSON") else {
                unreachable!("params 應為 object");
            };
            let _ = hook_tx.send(svc.rpc("device.attach", &params));
        }
    }));

    let reply = r.call(
        "alias.set",
        &format!(r#"{{"session_id":"{WP510_PLACEHOLDER_SID}","alias":"{LATE_ALIAS}"}}"#),
    );
    let attached = hook_rx
        .recv_timeout(Duration::from_secs(5))
        .expect("佔位退休應發生在 alias 持久化前")
        .unwrap_or_else(|e| panic!("device.attach 應回字典：{}", e.message));
    assert_eq!(
        attached.get("ok"),
        Some(&PyValue::Bool(true)),
        "device.attach 應完成佔位退休：{}",
        j(&attached)
    );
    assert_eq!(
        j(&reply),
        format!(
            r#"{{"ok":false,"error_code":"SESSION_NOT_FOUND","session_id":"{WP510_PLACEHOLDER_SID}","version":"{}"}}"#,
            r.version
        ),
        "alias.set RPC bytes"
    );
    wait_until("t:COM3 正式 session", || {
        r.mgr.resolve("t:COM3").as_deref() == Some("t:COM3")
    });
    let listed = r.call("alias.list", "{}");
    let PyValue::List(rows) = listed.get("aliases").cloned().expect("alias.list 應含 aliases") else {
        panic!("aliases 應為 list：{}", j(&listed));
    };
    for row in &rows {
        let PyValue::Dict(row) = row else {
            panic!("alias 列應為 object：{}", j(&listed));
        };
        assert_ne!(
            row.get("session_id"),
            Some(&PyValue::from(WP510_PLACEHOLDER_SID)),
            "alias.list 不得留下指向已退休 sid 的 alias：{}",
            j(&listed)
        );
        assert_ne!(
            row.get("alias"),
            Some(&PyValue::from(LATE_ALIAS)),
            "退休後到的 alias 不得寫入：{}",
            j(&listed)
        );
    }
}

/// 正式 RPC 回歸：blocked placeholder open 在 release timeout 後回 Held（未取得 handle），應解除 stall 並可重試。
#[test]
fn wp510_rpc_open_error_after_release_settles_without_handle() {
    let (rig, backend, entered, dev) = wp510_b_placeholder_blocking_rig_with_open_error(Some(
        OpenError::Held(HolderReport {
            pids: vec![771],
            holder_unknown: false,
            status: HolderScanStatus::Ok,
        }),
    ));
    let rig = Arc::new(rig);
    let attach_rig = Arc::clone(&rig);
    let attach = std::thread::spawn(move || {
        attach_rig.call(
            "device.attach",
            &format!(r#"{{"selector":"{WP510_PLACEHOLDER_SID}"}}"#),
        )
    });
    entered
        .recv_timeout(Duration::from_secs(5))
        .expect("placeholder open_exclusive 必須進入阻塞點");

    let release_rig = Arc::clone(&rig);
    let release = std::thread::spawn(move || {
        release_rig.call(
            "device.release",
            &format!(r#"{{"selector":"{WP510_PLACEHOLDER_SID}"}}"#),
        )
    });
    let stalled = release.join().expect("release RPC thread");
    assert_eq!(s(&stalled, "error_code"), "DRAIN_STALLED", "{}", j(&stalled));
    assert_eq!(stalled.get("drain_stalled"), Some(&PyValue::Bool(true)));
    let epoch = stalled
        .get("drain_timeout_epoch")
        .cloned()
        .expect("DRAIN_STALLED 必須帶 timeout epoch");
    assert!(matches!(epoch, PyValue::Int(_)), "epoch={epoch:?}");

    backend.release();
    let attached = attach.join().expect("被 release 撤銷的 attach RPC thread");
    let expected_attached = sw_proto::pydict! {
        "ok" => false,
        "error_code" => "DEVICE_STILL_HELD",
        "pids" => PyValue::List(vec![PyValue::from(771)]),
        "holder_unknown" => false,
        "selector" => WP510_PLACEHOLDER_SID,
        "version" => rig.version.clone(),
    };
    assert_eq!(j(&attached), j(&expected_attached), "開埠回 Held 的 attach RPC bytes");
    assert_eq!(dev.open_count(), 0, "Held 開埠未取得 handle");

    let state = rig
        .session(WP510_PLACEHOLDER_SID)
        .expect("placeholder 仍須可查詢");
    assert_eq!(s(&state, "state"), "RELEASED");
    assert_eq!(state.get("drain_stalled"), Some(&PyValue::Bool(false)));
    assert_eq!(state.get("drain_timeout_epoch"), Some(&PyValue::Null));
    assert_eq!(rig.mgr.released_placeholder(WP510_PLACEHOLDER_SID), Some(true));

    let repeated_release = rig.call(
        "device.release",
        &format!(r#"{{"selector":"{WP510_PLACEHOLDER_SID}"}}"#),
    );
    assert_eq!(repeated_release.get("ok"), Some(&PyValue::Bool(true)), "{}", j(&repeated_release));

    let retry = rig.call(
        "device.attach",
        &format!(r#"{{"selector":"{WP510_PLACEHOLDER_SID}"}}"#),
    );
    assert_eq!(retry.get("ok"), Some(&PyValue::Bool(true)), "{}", j(&retry));
    wait_until("retry attach opens the device", || dev.open_count() == 1);
}

/// 正式 RPC 落盤回歸：退休 placeholder 的 state.json 寫入 ENOSPC 時回 EXCEPTION（`_save_state` 例外穿出 RPC）。
/// Python `attach_device` 已在 manager lock 內退休佔位（移除 session、binding、aliases，`_pending_com` 保留原 COM）
/// 並把已開的 UART 作業留在 `_preopened_ports`（`origin/main:sw_core/session_manager.py:3590-3607`、`:3640-3653`），
/// `_save_state()`（`:3633`）的例外不回滾記憶體、也不執行其後的 `_spawn_attach`（`:3634-3637`）：佔位不再存在、
/// 預開 handle 不關閉、不啟動 dynamic 偵測。函式名沿用凍結鎖節點名（交接確實停止）。
#[test]
fn wp510_rpc_placeholder_retirement_enospc_stops_handoff() {
    let r = wp510_b_placeholder_rig();
    let dev = r.device(WP510_REAL_G);
    let state_before = r.fs.bytes(support::STATE_PATH);
    r.fs.fail_next_atomic_write(PalError::io(
        28,
        "[Errno 28] No space left on device",
    ));

    let error = r.call_err(
        "device.attach",
        &format!(r#"{{"selector":"{WP510_PLACEHOLDER_SID}"}}"#),
    );
    std::thread::sleep(Duration::from_millis(100));

    assert!(
        error.message.contains("No space left on device"),
        "state.json ENOSPC 必須沿 RPC error 路徑傳回：{}",
        error.message
    );
    assert_eq!(r.mgr.released_placeholder(WP510_PLACEHOLDER_SID), None);
    assert_eq!(
        r.mgr.resolve(WP510_PLACEHOLDER_SID),
        None,
        "落盤失敗不回滾：佔位已自 manager 退休"
    );
    assert_eq!(dev.open_count(), 1);
    assert_eq!(dev.close_count(), 0, "預開 handle 留在作業內，不關閉（Python `_preopened_ports`）");
    assert!(dev.is_open(), "預開 handle 仍開啟");
    assert!(dev.tx_len() == 0, "落盤失敗不得啟動 dynamic 偵測 TX");
    assert_eq!(r.fs.bytes(support::STATE_PATH), state_before, "寫入失敗不得改動 state.json");
    wp510_review7_assert_retired_placeholder_queries(&r);
}

/// 落盤失敗後舊佔位的查詢、alias、bind 與 attach 結果（Python 佔位已自 `_sessions` 移除、alias 已 unassign，
/// `origin/main:sw_core/session_manager.py:3640-3653`）：`get_session` 查無即 SESSION_NOT_FOUND
/// （`attach_device` `:3405-3406`、`bind_session` `:3663-3665`），alias 表不再含舊 alias。
fn wp510_review7_assert_retired_placeholder_queries(r: &ServiceRig) {
    for selector in [WP510_PLACEHOLDER_SID, "COM3", WP510_PLACEHOLDER_ALIAS] {
        let state = r.call(
            "session.get_state",
            &format!(r#"{{"selector":"{selector}"}}"#),
        );
        assert_eq!(s(&state, "error_code"), "SESSION_NOT_FOUND", "{selector}：{}", j(&state));
        let bound = r.call(
            "session.bind",
            &format!(r#"{{"selector":"{selector}","device_by_id":"{WP510_FREE_BY_ID}"}}"#),
        );
        let expected_bind = sw_proto::pydict! {
            "ok" => false,
            "error_code" => "SESSION_NOT_FOUND",
            "selector" => selector,
            "version" => r.version.clone(),
        };
        assert_eq!(j(&bound), j(&expected_bind), "{selector}：session.bind bytes");
        let attached = r.call("device.attach", &format!(r#"{{"selector":"{selector}"}}"#));
        let expected_attach = sw_proto::pydict! {
            "ok" => false,
            "error_code" => "SESSION_NOT_FOUND",
            "selector" => selector,
            "version" => r.version.clone(),
        };
        assert_eq!(j(&attached), j(&expected_attach), "{selector}：device.attach bytes");
    }
    let listed = r.call("alias.list", "{}");
    assert!(
        !j(&listed).contains(WP510_PLACEHOLDER_ALIAS),
        "退休後 alias 不得殘留：{}",
        j(&listed)
    );
}

/// 落盤失敗後下一次成功存檔寫入的是記憶體中已退休的狀態（Python `_save_state` 由記憶體重建 payload，
/// `origin/main:sw_core/session_manager.py:1445-1471`）：舊佔位的 released、binding 與 alias 都不再出現。
#[test]
fn wp510_review7_rpc_retirement_enospc_next_save_persists_retirement() {
    let r = wp510_b_placeholder_rig();
    r.fs.fail_next_atomic_write(PalError::io(
        28,
        "[Errno 28] No space left on device",
    ));
    let _ = r.call_err(
        "device.attach",
        &format!(r#"{{"selector":"{WP510_PLACEHOLDER_SID}"}}"#),
    );
    let before = String::from_utf8(r.fs.bytes(support::STATE_PATH)).expect("UTF-8");
    assert!(before.contains(WP510_PLACEHOLDER_SID), "前置：磁碟仍是舊內容");

    let set = r.call("alias.set", r#"{"session_id":"t:COM0","alias":"dut0"}"#);
    assert_eq!(set.get("ok"), Some(&PyValue::Bool(true)), "{}", j(&set));

    let after = String::from_utf8(r.fs.bytes(support::STATE_PATH)).expect("UTF-8");
    assert!(
        !after.contains(WP510_PLACEHOLDER_SID),
        "下一次存檔須寫入退休結果（不得復原佔位列）：{after}"
    );
    assert!(!after.contains(WP510_PLACEHOLDER_ALIAS), "{after}");
    assert!(after.contains("dut0"), "{after}");
}

/// OSError 分支：佔位開埠回 OSError 後退休、`_finish_attach_op`，`_save_state()` 例外穿出 RPC，不回滾、不
/// `_spawn_attach`（`origin/main:sw_core/session_manager.py:3529-3559`）。
#[test]
fn wp510_review7_rpc_oserror_handoff_retirement_enospc_does_not_roll_back() {
    let (r, backend, _entered, dev) = wp510_b_placeholder_blocking_rig_with_open_error(Some(
        OpenError::Other(PalError::io(5, "[Errno 5] Input/output error")),
    ));
    backend.release();
    r.fs.fail_next_atomic_write(PalError::io(
        28,
        "[Errno 28] No space left on device",
    ));

    let error = r.call_err(
        "device.attach",
        &format!(r#"{{"selector":"{WP510_PLACEHOLDER_SID}"}}"#),
    );
    std::thread::sleep(Duration::from_millis(200));

    assert!(error.message.contains("No space left on device"), "{}", error.message);
    assert_eq!(r.mgr.resolve(WP510_PLACEHOLDER_SID), None, "落盤失敗不回滾");
    assert_eq!(r.mgr.resolve("t:COM3"), None, "不得啟動 dynamic 重試");
    assert_eq!(dev.open_attempts(), 0, "不得啟動 dynamic 重試開埠");
    assert!(
        !r.mgr.has_inflight_attach_for_test(WP510_PLACEHOLDER_BY_ID),
        "OSError 分支的作業已收尾"
    );
    wp510_review7_assert_retired_placeholder_queries(&r);
}

/// 落盤失敗後把 COM1 改綁到該裝置：Python `bind_session` 收尾 `_spawn_attach`，`_attach_by_id` 取走
/// `_preopened_ports` 的作業並以同一 handle 啟動 bridge（`origin/main:sw_core/session_manager.py:2955-2988`、
/// `:4852-4854`、`:4982`、`:4996-4999`），不重新開埠。
#[test]
fn wp510_review7_rpc_bind_after_retirement_enospc_adopts_preopened_handle() {
    let r = wp510_b_placeholder_rig();
    let dev = r.device(WP510_REAL_G).clone();
    r.fs.fail_next_atomic_write(PalError::io(
        28,
        "[Errno 28] No space left on device",
    ));
    let _ = r.call_err(
        "device.attach",
        &format!(r#"{{"selector":"{WP510_PLACEHOLDER_SID}"}}"#),
    );
    let attempts = dev.open_attempts();

    let bound = r.call(
        "session.bind",
        &format!(r#"{{"selector":"COM1","device_by_id":"{WP510_PLACEHOLDER_BY_ID}"}}"#),
    );

    assert_eq!(bound.get("ok"), Some(&PyValue::Bool(true)), "{}", j(&bound));
    r.wait_state("COM1", "READY");
    assert_eq!(dev.open_attempts(), attempts, "沿用預開 handle，不重新開埠");
    assert_eq!(dev.close_count(), 0);
    assert!(
        !r.mgr.has_inflight_attach_for_test(WP510_PLACEHOLDER_BY_ID),
        "作業已交給正式 bridge"
    );
}

/// 審查 P8-503c341-1 的前置：佔位退休落盤失敗留下未消費的預開作業（Python `_preopened_ports`），再把 COM1 改綁到
/// 該 by_id 且存檔第二次 ENOSPC。Python `bind_session` 已在記憶體改綁（`origin/main:sw_core/session_manager.py:3775-3791`，
/// 有 bridge 時由 `_complete_drain` `:2955-2967`），`_save_state()` 例外穿出（`:3810`、`:2981-2985`），其後的
/// `_spawn_attach` 未執行，作業仍留在 `_preopened_ports`。`release_first` 為 true 時 COM1 先 `device.release`（無 bridge）。
fn wp510_review8_double_enospc_bind_setup(r: &ServiceRig, release_first: bool) -> FakeDevice {
    let dev = r.device(WP510_REAL_G).clone();
    if release_first {
        let released = r.call("device.release", r#"{"selector":"COM1"}"#);
        assert_eq!(released.get("ok"), Some(&PyValue::Bool(true)), "{}", j(&released));
        r.wait_state("COM1", "RELEASED");
    }
    r.fs.fail_next_atomic_write(PalError::io(
        28,
        "[Errno 28] No space left on device",
    ));
    let _ = r.call_err(
        "device.attach",
        &format!(r#"{{"selector":"{WP510_PLACEHOLDER_SID}"}}"#),
    );
    assert!(dev.is_open(), "前置：預開 handle 留在未消費的作業內");
    assert!(r.mgr.has_inflight_attach_for_test(WP510_PLACEHOLDER_BY_ID));

    r.fs.fail_next_atomic_write(PalError::io(
        28,
        "[Errno 28] No space left on device",
    ));
    let error = r.call_err(
        "session.bind",
        &format!(r#"{{"selector":"COM1","device_by_id":"{WP510_PLACEHOLDER_BY_ID}"}}"#),
    );
    assert!(error.message.contains("No space left on device"), "{}", error.message);
    assert_eq!(dev.close_count(), 0, "前置：第二次落盤失敗未接手、也未關閉預開 handle");
    assert!(dev.is_open());
    dev
}

/// 重試改綁的期望（Python `bind_session` `origin/main:sw_core/session_manager.py:3680-3694`）：舊 by_id 已是 usb-G，
/// `_preopened_ports.pop(old_by_id)` 取出未消費作業，`_finish_attach_op` 直接關閉 handle（`:4320-4332`）、自表移除
/// （`:4335-4338`）後遞迴重試 bind（`:3727`）；改綁收尾設 ATTACHING＋last_error=None（`:3786-3791`），`_save_state()`
/// 成功後 `_spawn_attach` 重新開埠（`:3810-3813`）。
fn wp510_review8_assert_bind_retry_closes_parked_handle(r: &ServiceRig, dev: &FakeDevice) {
    let attempts = dev.open_attempts();
    let started = std::time::Instant::now();
    let bound = r.call(
        "session.bind",
        &format!(r#"{{"selector":"COM1","device_by_id":"{WP510_PLACEHOLDER_BY_ID}"}}"#),
    );
    let elapsed = started.elapsed();

    assert_eq!(bound.get("ok"), Some(&PyValue::Bool(true)), "{}", j(&bound));
    let session = match bound.get("session") {
        Some(PyValue::Dict(session)) => session.clone(),
        other => panic!("bind 回應須帶 session：{other:?}"),
    };
    assert_eq!(s(&session, "state"), "ATTACHING", "{}", j(&bound));
    assert_eq!(session.get("last_error"), Some(&PyValue::Null), "{}", j(&bound));
    assert_eq!(s(&session, "device_by_id"), WP510_PLACEHOLDER_BY_ID);
    assert_eq!(session.get("drain_stalled"), Some(&PyValue::Bool(false)), "{}", j(&bound));
    assert!(
        elapsed < Duration::from_millis(2500),
        "未消費作業直接收尾，不得等待 attach 作業期限：{elapsed:?}"
    );
    assert_eq!(dev.close_count(), 1, "未消費的預開 handle 關閉一次");
    wait_until("重新排程 attach 重新開埠", || dev.open_attempts() > attempts);
    r.wait_state("COM1", "READY");
    assert!(!r.mgr.unconfirmed_close_for_test(WP510_PLACEHOLDER_BY_ID));
}

/// S2-PAR-wp510r 第 16 項 Medium 1：一般 `OpenBridge` 遇到同 real_path 的 parked 記錄時，以同 token 接手，不能重開 UART。
#[test]
fn wp510r_m16_device_attach_claims_parked_custody_token() {
    let r = wp510_b_placeholder_rig();
    let dev = wp510_review8_double_enospc_bind_setup(&r, false);
    let parked = r
        .mgr
        .custody_for_test(WP510_PLACEHOLDER_BY_ID)
        .expect("前置：雙 ENOSPC 留下 parked 帳本紀錄");
    assert!(matches!(parked.owner, sw_core::custody::CustodyOwner::Parked));
    let token = parked.token;
    let attempts = dev.open_attempts();

    let attached = r.call("device.attach", r#"{"selector":"COM1"}"#);

    assert_eq!(attached.get("ok"), Some(&PyValue::Bool(true)), "{}", j(&attached));
    r.wait_state("COM1", "READY");
    assert_eq!(dev.open_attempts(), attempts, "一般 attach 必須沿用 parked handle");
    assert_eq!(dev.close_count(), 0, "接手時不得關閉 parked handle");
    assert!(dev.is_open(), "正式 bridge 接手同一個已開 handle");
    assert!(
        r.mgr.custody_for_test(WP510_PLACEHOLDER_BY_ID).is_none(),
        "AdoptBridge 確認接手後移除原 token={token}"
    );
}

/// 審查 P8-503c341-1：COM1 已 RELEASED（無 bridge）時，連續兩次 ENOSPC 後重試同一 bind。
#[test]
fn wp510_review8_rpc_bind_retry_after_double_enospc_closes_parked_handle() {
    let r = wp510_b_placeholder_rig();
    let dev = wp510_review8_double_enospc_bind_setup(&r, true);
    wp510_review8_assert_bind_retry_closes_parked_handle(&r, &dev);
}

/// 審查 P8-503c341-1：COM1 為 READY（有 bridge，改綁走 drain）時，連續兩次 ENOSPC 後重試同一 bind。
#[test]
fn wp510_review8_rpc_bind_retry_after_double_enospc_from_ready_closes_parked_handle() {
    let r = wp510_b_placeholder_rig();
    let dev = wp510_review8_double_enospc_bind_setup(&r, false);
    wp510_review8_assert_bind_retry_closes_parked_handle(&r, &dev);
}

/// 審查 P8-503c341-1 的關閉未確認分支：重試 bind 關閉未消費的預開 handle 回 `closed:false`。Python 已先把作業自
/// `_preopened_ports` 取出（`origin/main:sw_core/session_manager.py:3681`），`_finish_attach_op` 關閉失敗只告警並回
/// False（`:4322-4332`），不寫入 `_draining_by_ids`（該表只由 `_begin_attach_bridge_stop_locked` `:1324` 與
/// `_begin_drain_locked` `:2774-2775` 寫入）；該作業的 `session_id` 為 `dynamic:<by_id>`（`:3608`），`_sessions` 查無，
/// 不走 RECOVERING 回覆（`:3697-3700`），release 亦未請求（`:3716-3719`），遞迴重試 bind（`:3727`）：回 ok＋ATTACHING、
/// last_error=None（`:3786-3791`、`:3813`），`_save_state()` 成功後 `_spawn_attach` 照常排程（`:3810-3812`）。
/// 舊 handle 仍被持有，重新開埠由 `_preopen_auto_attach_port` 收尾為 DETACHED＋`ATTACH_FAILED:DeviceHeldError`
/// （`:4194-4214`）；之後的 bind 同樣重新嘗試，`device.attach` 的開埠失敗回 DEVICE_STILL_HELD、`pids:[]`、
/// `holder_unknown:true`（`:3510-3523`；holder 掃描排除自身 pid `:3370-3376`）。
///
/// wp510 第 14 項依 rulings/s2.md「對照 Python」例外改正第 13 項固定下來的差異（未確認關閉登記為永久阻擋、之後不再開埠）；
/// 測試名稱已登記於 L1 凍結鎖，沿用不改。
#[test]
fn wp510_review8_rpc_bind_retry_parked_close_unconfirmed_keeps_block() {
    let r = wp510_b_placeholder_rig();
    let dev = wp510_review8_double_enospc_bind_setup(&r, true);
    dev.fail_close(PalError::io(5, "[Errno 5] Input/output error"));
    let attempts = dev.open_attempts();
    let tx_before = dev.tx_len();

    let started = std::time::Instant::now();
    let bound = r.call(
        "session.bind",
        &format!(r#"{{"selector":"COM1","device_by_id":"{WP510_PLACEHOLDER_BY_ID}"}}"#),
    );
    let elapsed = started.elapsed();

    assert_eq!(bound.get("ok"), Some(&PyValue::Bool(true)), "{}", j(&bound));
    let session = match bound.get("session") {
        Some(PyValue::Dict(session)) => session.clone(),
        other => panic!("bind 回應須帶 session：{other:?}"),
    };
    assert_eq!(s(&session, "state"), "ATTACHING", "{}", j(&bound));
    assert_eq!(session.get("last_error"), Some(&PyValue::Null), "{}", j(&bound));
    assert_eq!(s(&session, "device_by_id"), WP510_PLACEHOLDER_BY_ID);
    assert!(
        elapsed < Duration::from_millis(2500),
        "關閉回條已到，不得等待 attach 作業期限：{elapsed:?}"
    );
    assert_eq!(dev.close_count(), 1, "已嘗試關閉未消費的預開 handle");
    assert!(dev.is_open(), "closed:false 表示 handle 仍開啟");
    assert!(
        r.mgr.unconfirmed_close_for_test(WP510_PLACEHOLDER_BY_ID),
        "D1: 關閉失敗必須登記未確認關閉阻擋"
    );
    let custody = r.mgr.custody_for_test(WP510_PLACEHOLDER_BY_ID).expect("custody entry");
    assert!(
        matches!(custody.phase, CustodyPhaseSnapshot::CloseFailed { .. }),
        "D1: 帳本紀錄必須轉為 CloseFailed"
    );

    std::thread::sleep(Duration::from_millis(200));
    assert_eq!(dev.open_attempts(), attempts, "D1: 未確認關閉不得重新開埠");
    let com1_session = r.session("COM1").expect("COM1 session");
    assert_eq!(s(&com1_session, "state"), "ATTACHING");
    assert_eq!(dev.tx_len(), tx_before, "開埠失敗不得送 TX");

    let again = r.call(
        "session.bind",
        &format!(r#"{{"selector":"COM1","device_by_id":"{WP510_PLACEHOLDER_BY_ID}"}}"#),
    );
    assert_eq!(again.get("ok"), Some(&PyValue::Bool(true)), "{}", j(&again));
    std::thread::sleep(Duration::from_millis(100));
    assert_eq!(dev.open_attempts(), attempts, "D1: 之後的 bind 同樣不得開埠");

    let attached = r.call("device.attach", r#"{"selector":"COM1"}"#);
    assert_eq!(s(&attached, "error_code"), "SESSION_RECOVERING", "{}", j(&attached));
    assert_eq!(dev.open_attempts(), attempts, "device.attach 不得嘗試開埠");
    assert_eq!(dev.tx_len(), tx_before);
    assert!(r.mgr.unconfirmed_close_for_test(WP510_PLACEHOLDER_BY_ID));
}

/// wp510 第 14 項前置：佔位退休落盤失敗留下未消費的預開作業（Python `_preopened_ports`），COM1 先 `device.release`
/// （無 bridge），再經 `session.bind` 改綁到該 by_id；bind 已把 handle 交給 COM1 的 session、送出 `DeviceOnline` 之前
/// 停住（Python 此時作業由 `_spawn_attach` 的 `_attach_by_id` 自 `_preopened_ports` 取到 `_active_attach_ops`，
/// `origin/main:sw_core/session_manager.py:4852-4854`，持續追蹤到正式 bridge 接手 `:4374-4383` 或關閉 `:4320-4365`）。
fn wp510_review9_bind_paused_before_device_online(
    r: &Arc<ServiceRig>,
) -> (
    std::thread::JoinHandle<PyDict>,
    crossbeam_channel::Sender<()>,
    FakeDevice,
) {
    let dev = r.device(WP510_REAL_G).clone();
    let released = r.call("device.release", r#"{"selector":"COM1"}"#);
    assert_eq!(released.get("ok"), Some(&PyValue::Bool(true)), "{}", j(&released));
    r.wait_state("COM1", "RELEASED");
    r.fs.fail_next_atomic_write(PalError::io(
        28,
        "[Errno 28] No space left on device",
    ));
    let _ = r.call_err(
        "device.attach",
        &format!(r#"{{"selector":"{WP510_PLACEHOLDER_SID}"}}"#),
    );
    assert!(dev.is_open(), "前置：預開 handle 留在未消費的作業內");
    let (entered_tx, entered_rx) = crossbeam_channel::bounded(1);
    let (go_tx, go_rx) = crossbeam_channel::bounded::<()>(1);
    r.mgr.set_bind_pre_device_online_hook_for_test(Box::new(move || {
        let _ = entered_tx.send(());
        let _ = go_rx.recv_timeout(Duration::from_secs(10));
    }));
    let bind_rig = Arc::clone(r);
    let bind = std::thread::spawn(move || {
        bind_rig.call(
            "session.bind",
            &format!(r#"{{"selector":"COM1","device_by_id":"{WP510_PLACEHOLDER_BY_ID}"}}"#),
        )
    });
    entered_rx
        .recv_timeout(Duration::from_secs(5))
        .expect("bind 應停在送出 DeviceOnline 之前");
    assert!(dev.is_open(), "前置：handle 已交給 COM1、尚未由正式 bridge 接手");
    assert_eq!(dev.close_count(), 0);
    (bind, go_tx, dev)
}

/// wp510 第七次整包審查 High：bind 接手 parked 預開 handle 後、`DeviceOnline` 被處理前插入 `device.release`。Python
/// `release_device` 取得同一作業（`_active_attach_ops`／`_attach_ops` `:3104-3107`，或未消費的 `_preopened_ports`
/// `:3108-3111`）後關閉或等待其完成（`:3188-3204`），handle 關閉後才回 ok；之後的 attach 收尾見 RELEASED 中止
/// （`:4933-4944`），不重新開埠、零 TX。
#[test]
fn wp510_review9_rpc_release_before_bind_device_online_closes_adopted_handle() {
    let r = Arc::new(wp510_b_placeholder_rig());
    let (bind, go, dev) = wp510_review9_bind_paused_before_device_online(&r);
    let attempts = dev.open_attempts();
    let tx_before = dev.tx_len();

    let released = r.call("device.release", r#"{"selector":"COM1"}"#);
    let open_at_reply = dev.is_open();
    go.send(()).expect("放行 bind");
    let bound = bind.join().expect("bind thread 不應 panic");

    assert_eq!(released.get("ok"), Some(&PyValue::Bool(true)), "{}", j(&released));
    assert!(!open_at_reply, "release 回 ok 之前必須關閉交給 session 的預開 handle");
    assert_eq!(dev.close_count(), 1, "預開 handle 關閉一次");
    assert_eq!(bound.get("ok"), Some(&PyValue::Bool(true)), "{}", j(&bound));
    std::thread::sleep(Duration::from_millis(200));
    assert_eq!(dev.open_attempts(), attempts, "release 之後不得重新開埠");
    assert_eq!(dev.tx_len(), tx_before, "release 之後不得送 TX");
    assert!(!dev.is_open());
    r.wait_state("COM1", "RELEASED");
    assert!(!r.mgr.unconfirmed_close_for_test(WP510_PLACEHOLDER_BY_ID));
    assert!(!r.mgr.has_inflight_attach_for_test(WP510_PLACEHOLDER_BY_ID));
}

/// 同上時點、關閉回條為 `closed:false`：Python 作業仍在 `_active_attach_ops`（`_attach_by_id` 已自 `_preopened_ports`
/// 取到 `:4852-4854`），release 取消並等待 `done`（`:3104-3107`、`:3188-3191`）；attach 中止後 `_finish_attach_op`
/// 關閉失敗只回 False、作業留在表內（`:4322-4332`），release 於期限到回 DRAIN_STALLED＋`drain_stalled:true`、
/// state RECOVERING（`:3192-3204`）。之後的 release 取得同一作業、沿用已到期的首次期限，立即回 DRAIN_STALLED
/// （`:3117-3125`）；不重新開埠、零 TX。
#[test]
fn wp510_review9_rpc_release_before_bind_device_online_close_unconfirmed_drain_stalled() {
    let r = Arc::new(wp510_b_placeholder_rig());
    let (bind, go, dev) = wp510_review9_bind_paused_before_device_online(&r);
    let attempts = dev.open_attempts();
    let tx_before = dev.tx_len();
    dev.fail_close(PalError::io(5, "[Errno 5] Input/output error"));

    let started = std::time::Instant::now();
    let released = r.call("device.release", r#"{"selector":"COM1"}"#);
    let elapsed = started.elapsed();
    go.send(()).expect("放行 bind");
    let _ = bind.join().expect("bind thread 不應 panic");

    assert_eq!(s(&released, "error_code"), "DRAIN_STALLED", "{}", j(&released));
    assert_eq!(released.get("drain_stalled"), Some(&PyValue::Bool(true)), "{}", j(&released));
    let session = match released.get("session") {
        Some(PyValue::Dict(session)) => session.clone(),
        other => panic!("DRAIN_STALLED 回應須帶 session：{other:?}"),
    };
    assert_eq!(s(&session, "state"), "RECOVERING", "{}", j(&released));
    assert!(elapsed >= Duration::from_millis(2500), "release 須等待作業至期限：{elapsed:?}");
    assert_eq!(dev.close_count(), 1, "已嘗試關閉預開 handle");
    assert!(dev.is_open(), "closed:false 表示 handle 仍開啟");
    let custody = r.mgr.custody_for_test(WP510_PLACEHOLDER_BY_ID).expect("custody entry");
    assert!(matches!(custody.phase, CustodyPhaseSnapshot::CloseFailed { .. }));
    assert!(r.mgr.unconfirmed_close_for_test(WP510_PLACEHOLDER_BY_ID));
    std::thread::sleep(Duration::from_millis(200));
    assert_eq!(dev.open_attempts(), attempts, "DRAIN_STALLED 之後不得重新開埠");
    assert_eq!(dev.tx_len(), tx_before);

    let again_started = std::time::Instant::now();
    let again = r.call("device.release", r#"{"selector":"COM1"}"#);
    assert_eq!(s(&again, "error_code"), "DRAIN_STALLED", "{}", j(&again));
    assert!(
        again_started.elapsed() < Duration::from_millis(1000),
        "之後的 release 沿用已到期的首次期限"
    );
    assert_eq!(dev.close_count(), 1, "handle 只關閉一次");
    assert_eq!(dev.open_attempts(), attempts);
}

/// 同上時點插入 shutdown：Python `shutdown_drain` 取消三張表的作業，未消費者直接 `_finish_attach_op` 關閉
/// （`:1175-1227`），關閉成功不列入 stalled（`:1231-1239`）；之後的 attach 收尾見 shutdown 不開埠。
#[test]
fn wp510_review9_rpc_shutdown_before_bind_device_online_closes_adopted_handle() {
    let r = Arc::new(wp510_b_placeholder_rig());
    let (bind, go, dev) = wp510_review9_bind_paused_before_device_online(&r);
    let attempts = dev.open_attempts();
    let tx_before = dev.tx_len();

    let drained = r.mgr.shutdown(Duration::from_millis(1500));
    let open_after_shutdown = dev.is_open();
    go.send(()).expect("放行 bind");
    let _ = bind.join().expect("bind thread 不應 panic");

    assert!(!open_after_shutdown, "shutdown 必須關閉交給 session 的預開 handle：{drained:?}");
    assert_eq!(dev.close_count(), 1);
    assert!(
        drained.iter().all(|(_, ok)| *ok),
        "已確認關閉的預開 handle 不得列為 stalled：{drained:?}"
    );
    std::thread::sleep(Duration::from_millis(200));
    assert_eq!(dev.open_attempts(), attempts, "停機後不得重新開埠");
    assert_eq!(dev.tx_len(), tx_before);
}

/// 同上時點插入 shutdown、關閉回條為 `closed:false`：未消費作業未被取出，`_finish_attach_op` 關閉失敗保留作業
/// （`:4322-4332`），`shutdown_drain` 末段列為 stalled（`:1231-1239`）。
#[test]
fn wp510_review9_rpc_shutdown_before_bind_device_online_close_unconfirmed_reports_stalled() {
    let r = Arc::new(wp510_b_placeholder_rig());
    let (bind, go, dev) = wp510_review9_bind_paused_before_device_online(&r);
    dev.fail_close(PalError::io(5, "[Errno 5] Input/output error"));

    let drained = r.mgr.shutdown(Duration::from_millis(1500));
    go.send(()).expect("放行 bind");
    let _ = bind.join().expect("bind thread 不應 panic");

    assert_eq!(dev.close_count(), 1, "shutdown 已嘗試關閉預開 handle：{drained:?}");
    assert!(dev.is_open());
    assert!(
        drained
            .iter()
            .any(|(target, ok)| target == &format!("attach:{WP510_PLACEHOLDER_BY_ID}") && !ok),
        "未確認關閉的預開 handle 必須列入 shutdown stalled：{drained:?}"
    );
    assert!(r.mgr.has_inflight_attach_for_test(WP510_PLACEHOLDER_BY_ID));
}

/// 同上時點把 COM1 再改綁到另一個裝置：Python `bind_session` 取得舊 by_id 的作業後關閉或等待（`:3676-3697`），再遞迴
/// 重試改綁（`:3727`）；新裝置照常 attach，舊裝置的 handle 關閉且不再開啟。
#[test]
fn wp510_review9_rpc_rebind_before_bind_device_online_closes_adopted_handle() {
    let r = Arc::new(wp510_b_placeholder_rig());
    let (bind, go, dev) = wp510_review9_bind_paused_before_device_online(&r);
    let attempts = dev.open_attempts();
    let tx_before = dev.tx_len();

    let rebound = r.call(
        "session.bind",
        &format!(r#"{{"selector":"COM1","device_by_id":"{WP510_FREE_BY_ID}"}}"#),
    );
    let open_at_reply = dev.is_open();
    go.send(()).expect("放行第一個 bind");
    let _ = bind.join().expect("bind thread 不應 panic");

    assert_eq!(rebound.get("ok"), Some(&PyValue::Bool(true)), "{}", j(&rebound));
    assert!(!open_at_reply, "改綁回覆前必須關閉舊 by_id 的預開 handle");
    assert_eq!(dev.close_count(), 1);
    r.wait_state("COM1", "READY");
    let session = r.session("COM1").expect("COM1 session");
    assert_eq!(s(&session, "device_by_id"), WP510_FREE_BY_ID);
    std::thread::sleep(Duration::from_millis(200));
    assert_eq!(dev.open_attempts(), attempts, "舊裝置不得再開埠");
    assert_eq!(dev.tx_len(), tx_before, "舊裝置不得送 TX");
    assert!(!r.mgr.has_inflight_attach_for_test(WP510_PLACEHOLDER_BY_ID));
}

/// 雙 ENOSPC 後 COM1 已於記憶體改綁到 parked 作業的 by_id、接手未執行時 `device.release`：Python `release_device`
/// 自 `_preopened_ports` 取出未消費作業（`:3108-3111`）並 `_finish_attach_op` 關閉（`:3188-3191`），handle 關閉後才回
/// ok；之後不重新開埠。
#[test]
fn wp510_review9_rpc_release_with_parked_handle_of_bound_session_closes_it() {
    let r = wp510_b_placeholder_rig();
    let dev = wp510_review8_double_enospc_bind_setup(&r, true);
    let attempts = dev.open_attempts();
    let tx_before = dev.tx_len();

    let released = r.call("device.release", r#"{"selector":"COM1"}"#);
    let open_at_reply = dev.is_open();

    assert_eq!(released.get("ok"), Some(&PyValue::Bool(true)), "{}", j(&released));
    assert!(!open_at_reply, "release 回 ok 之前必須關閉未消費的預開 handle");
    assert_eq!(dev.close_count(), 1);
    assert!(!r.mgr.has_inflight_attach_for_test(WP510_PLACEHOLDER_BY_ID));
    std::thread::sleep(Duration::from_millis(200));
    assert_eq!(dev.open_attempts(), attempts);
    assert_eq!(dev.tx_len(), tx_before);
    r.wait_state("COM1", "RELEASED");
}

/// 佔位交接的 dynamic session 已建立、正式 bridge 的 `open_bridge` 取預開 handle 之前插入 `device.release`：Python 作業
/// 由 `_attach_by_id_dynamic` 持有於 `_active_attach_ops`（`:4852-4854`、`:5418`），release 取消並等待（`:3104-3107`、
/// `:3188-3204`），attach 見取消中止（`:4986-4996`）並關閉 handle；不另開新 handle、零 TX。
#[test]
fn wp510_review9_rpc_release_before_bridge_takes_dynamic_handoff_closes_without_reopen() {
    let r = Arc::new(wp510_b_placeholder_rig());
    let dev = r.device(WP510_REAL_G).clone();
    let (entered_tx, entered_rx) = crossbeam_channel::bounded(1);
    let (go_tx, go_rx) = crossbeam_channel::bounded::<()>(1);
    r.mgr.set_open_bridge_pre_take_hook_for_test(Box::new(move || {
        let _ = entered_tx.send(());
        let _ = go_rx.recv_timeout(Duration::from_secs(10));
    }));
    let attached = r.call(
        "device.attach",
        &format!(r#"{{"selector":"{WP510_PLACEHOLDER_SID}"}}"#),
    );
    assert_eq!(attached.get("ok"), Some(&PyValue::Bool(true)), "{}", j(&attached));
    entered_rx
        .recv_timeout(Duration::from_secs(5))
        .expect("正式 session 的 open_bridge 應停在接手預開 handle 之前");
    let attempts = dev.open_attempts();
    let tx_before = dev.tx_len();

    let release_rig = Arc::clone(&r);
    let release =
        std::thread::spawn(move || release_rig.call("device.release", r#"{"selector":"t:COM3"}"#));
    // release 進入處理（manager 取消交接或 actor 排入 pending stop）後才放行 open_bridge。
    std::thread::sleep(Duration::from_millis(300));
    go_tx.send(()).expect("放行 open_bridge");
    let released = release.join().expect("release thread 不應 panic");

    assert_eq!(released.get("ok"), Some(&PyValue::Bool(true)), "{}", j(&released));
    assert!(!dev.is_open(), "release 回 ok 時 handle 已關閉");
    assert_eq!(dev.close_count(), 1);
    assert_eq!(dev.open_attempts(), attempts, "取消的交接不得另開新 handle");
    assert_eq!(dev.tx_len(), tx_before, "release 之後不得送 TX");
    r.wait_state("t:COM3", "RELEASED");
}

/// `state.json` 的 `released` 是否有指定 by_id 的列（Python `_persist_release_state` 寫入的 released 段）。
fn wp510_review10_state_has_released_by_id(r: &ServiceRig, by_id: &str) -> bool {
    let bytes = r.fs.bytes(support::STATE_PATH);
    let Ok(PyValue::Dict(state)) = sw_proto::pyjson::loads(&String::from_utf8_lossy(&bytes)) else {
        return false;
    };
    match state.get("released") {
        Some(PyValue::Dict(rows)) => rows.0.values().any(|row| match row {
            PyValue::Dict(row) => row.get("by_id").and_then(PyValue::as_str) == Some(by_id),
            _ => false,
        }),
        _ => false,
    }
}

/// wp510 第八次整包審查 High（P8-e25b69c-1）：bind 接手 parked 預開 handle、`DeviceOnline` 之前，第一個
/// `device.release` 認領交接列後關閉 handle 的途中（關閉暫停），第二個 `device.release` 插入。Python 的作業此時仍在
/// `_active_attach_ops`（`_attach_by_id` 自 `_preopened_ports` 取到 `origin/main:sw_core/session_manager.py:4852-4854`，
/// 關閉由 `_finish_attach_op` 完成後才移出 `:4320-4338`）；第二個 release 取得同一作業（`:3104-3107`），沿用第一個
/// release 的期限（`:3117-3125`）等待 `done`（`:3188-3191`），handle 確認關閉後才回 ok。
#[test]
fn wp510_review10_rpc_second_release_waits_handoff_close_in_progress() {
    let r = Arc::new(wp510_b_placeholder_rig());
    let (bind, go, dev) = wp510_review9_bind_paused_before_device_online(&r);
    let attempts = dev.open_attempts();
    let tx_before = dev.tx_len();
    dev.pause_next_close();

    let (first_tx, first_rx) = crossbeam_channel::bounded(1);
    let first_rig = Arc::clone(&r);
    let first_dev = dev.clone();
    let first = std::thread::spawn(move || {
        let reply = first_rig.call("device.release", r#"{"selector":"COM1"}"#);
        let _ = first_tx.send((reply, first_dev.is_open()));
    });
    let paused = dev.wait_paused(Duration::from_secs(5));

    let (second_tx, second_rx) = crossbeam_channel::bounded(1);
    let second_rig = Arc::clone(&r);
    let second_dev = dev.clone();
    let second = std::thread::spawn(move || {
        let reply = second_rig.call("device.release", r#"{"selector":"COM1"}"#);
        let _ = second_tx.send((reply, second_dev.is_open()));
    });
    let second_early = second_rx.recv_timeout(Duration::from_millis(500)).ok();
    let open_while_second_early = dev.is_open();
    dev.release_pause();
    let replied_early = second_early.is_some();
    let (second_reply, open_at_second) = second_early
        .or_else(|| second_rx.recv_timeout(Duration::from_secs(10)).ok())
        .expect("第二個 release 應回覆");
    let (first_reply, open_at_first) = first_rx
        .recv_timeout(Duration::from_secs(10))
        .expect("第一個 release 應回覆");
    second.join().expect("第二個 release thread 不應 panic");
    first.join().expect("第一個 release thread 不應 panic");
    go.send(()).expect("放行 bind");
    let _ = bind.join().expect("bind thread 不應 panic");

    assert!(paused, "第一個 release 應停在關閉交接 handle 的途中");
    assert!(
        !replied_early,
        "關閉尚未完成（handle 仍開啟={open_while_second_early}），第二個 release 不得先回覆：{}",
        j(&second_reply)
    );
    assert_eq!(second_reply.get("ok"), Some(&PyValue::Bool(true)), "{}", j(&second_reply));
    assert!(!open_at_second, "第二個 release 回 ok 時 handle 必須已關閉");
    assert_eq!(first_reply.get("ok"), Some(&PyValue::Bool(true)), "{}", j(&first_reply));
    assert!(!open_at_first, "第一個 release 回 ok 時 handle 必須已關閉");
    assert_eq!(dev.close_count(), 1, "交接 handle 只關閉一次");
    r.wait_state("COM1", "RELEASED");
    std::thread::sleep(Duration::from_millis(200));
    assert_eq!(dev.open_attempts(), attempts, "release 之後不得重新開埠");
    assert_eq!(dev.tx_len(), tx_before, "release 之後不得送 TX");
    assert!(!dev.is_open());
    assert!(!r.mgr.has_inflight_attach_for_test(WP510_PLACEHOLDER_BY_ID));
    assert!(!r.mgr.unconfirmed_close_for_test(WP510_PLACEHOLDER_BY_ID));
    assert!(wp510_review10_state_has_released_by_id(&r, WP510_PLACEHOLDER_BY_ID));
}

/// 同上時點、關閉一直暫停到首次期限之後：Python 兩個 release 等的是同一作業與同一期限（`:3117-3125`），期限到都回
/// DRAIN_STALLED＋`drain_stalled:true`、state RECOVERING（`:3192-3204`），回覆時 handle 仍開啟；之後放行的
/// `DeviceOnline` 不開埠（`_attach_by_id` 見作業取消中止 `:4933-4944`）。關閉遲到完成後 handle 已關閉、不重新開埠、
/// 零 TX，下一個 `device.release` 回 ok 並落盤 RELEASED。
#[test]
fn wp510_review10_rpc_releases_drain_stalled_while_handoff_close_blocked() {
    let r = Arc::new(wp510_b_placeholder_rig());
    let (bind, go, dev) = wp510_review9_bind_paused_before_device_online(&r);
    let attempts = dev.open_attempts();
    let tx_before = dev.tx_len();
    dev.pause_next_close();

    let (first_tx, first_rx) = crossbeam_channel::bounded(1);
    let first_rig = Arc::clone(&r);
    let first_dev = dev.clone();
    let first = std::thread::spawn(move || {
        let started = std::time::Instant::now();
        let reply = first_rig.call("device.release", r#"{"selector":"COM1"}"#);
        let _ = first_tx.send((reply, first_dev.is_open(), started.elapsed()));
    });
    let paused = dev.wait_paused(Duration::from_secs(5));
    let second = r.call("device.release", r#"{"selector":"COM1"}"#);
    let open_at_second = dev.is_open();
    let first_reply = first_rx.recv_timeout(Duration::from_secs(6)).ok();
    // 失敗時也要放行暫停中的關閉，避免殘留執行緒卡住 rig 收尾。
    go.send(()).expect("放行 bind");
    let _ = bind.join().expect("bind thread 不應 panic");
    std::thread::sleep(Duration::from_millis(200));
    let attempts_before_close = dev.open_attempts();
    dev.release_pause();
    let first_reply = first_reply.or_else(|| {
        first_rx
            .recv_timeout(Duration::from_secs(10))
            .ok()
            .map(|(reply, open, elapsed)| (reply, open, elapsed + Duration::from_secs(3600)))
    });
    first.join().expect("第一個 release thread 不應 panic");

    assert!(paused, "第一個 release 應停在關閉交接 handle 的途中");
    assert_eq!(s(&second, "error_code"), "DRAIN_STALLED", "{}", j(&second));
    assert_eq!(second.get("drain_stalled"), Some(&PyValue::Bool(true)), "{}", j(&second));
    assert_eq!(s(&sub(&second, "session"), "state"), "RECOVERING", "{}", j(&second));
    assert!(open_at_second, "前置：第二個 release 回覆時關閉仍暫停");
    let (first_reply, open_at_first, first_elapsed) = first_reply.expect("第一個 release 應回覆");
    assert!(
        first_elapsed < Duration::from_secs(6),
        "第一個 release 須於首次期限回覆，不得等到關閉完成：{first_elapsed:?}"
    );
    assert_eq!(s(&first_reply, "error_code"), "DRAIN_STALLED", "{}", j(&first_reply));
    assert_eq!(first_reply.get("drain_stalled"), Some(&PyValue::Bool(true)));
    assert!(open_at_first, "第一個 release 回覆時關閉仍暫停");
    assert_eq!(dev.open_attempts(), attempts_before_close, "關閉完成前 DeviceOnline 不得開埠");

    wait_until("遲到的關閉完成", || !dev.is_open());
    wait_until("作業收尾", || {
        !r.mgr.has_inflight_attach_for_test(WP510_PLACEHOLDER_BY_ID)
    });
    std::thread::sleep(Duration::from_millis(200));
    assert_eq!(dev.close_count(), 1, "交接 handle 只關閉一次");
    assert_eq!(dev.open_attempts(), attempts, "關閉後不得重新開埠");
    assert_eq!(dev.tx_len(), tx_before, "不得送 TX");
    assert!(!r.mgr.unconfirmed_close_for_test(WP510_PLACEHOLDER_BY_ID));

    let again = r.call("device.release", r#"{"selector":"COM1"}"#);
    assert_eq!(again.get("ok"), Some(&PyValue::Bool(true)), "{}", j(&again));
    assert_eq!(s(&sub(&again, "session"), "state"), "RELEASED", "{}", j(&again));
    assert!(wp510_review10_state_has_released_by_id(&r, WP510_PLACEHOLDER_BY_ID));
    assert_eq!(dev.open_attempts(), attempts);
    assert!(!dev.is_open());
}

/// 佔位交接的 dynamic session 已發出 `OpenBridge`、開埠 worker 停在取預開 handle 之前時 `device.release` 認領交接列；
/// release 關閉 handle 的途中（關閉暫停）放行開埠 worker，使其先撞到撤銷標記回報 `CloseBlocked`，release 之後才交 actor。
/// Python `release_device` 找到作業即於鎖內設 RECOVERING（`origin/main:sw_core/session_manager.py:3133-3139`），
/// `_attach_by_id` 見取消中止（`:4986-4996`），`_finish_attach_op` 完成 RELEASED（`:4340-4363`），release 回 ok
/// （`:3206-3215`）；不得把已結束的開埠當成在途而等待排空期限。
#[test]
fn wp510_review10_rpc_release_after_revoked_bridge_open_reported_completes() {
    let r = Arc::new(wp510_b_placeholder_rig());
    let dev = r.device(WP510_REAL_G).clone();
    let (entered_tx, entered_rx) = crossbeam_channel::bounded(1);
    let (go_tx, go_rx) = crossbeam_channel::bounded::<()>(1);
    r.mgr.set_open_bridge_pre_take_hook_for_test(Box::new(move || {
        let _ = entered_tx.send(());
        let _ = go_rx.recv_timeout(Duration::from_secs(10));
    }));
    let attached = r.call(
        "device.attach",
        &format!(r#"{{"selector":"{WP510_PLACEHOLDER_SID}"}}"#),
    );
    assert_eq!(attached.get("ok"), Some(&PyValue::Bool(true)), "{}", j(&attached));
    entered_rx
        .recv_timeout(Duration::from_secs(5))
        .expect("正式 session 的 open_bridge 應停在接手預開 handle 之前");
    let attempts = dev.open_attempts();
    let tx_before = dev.tx_len();
    dev.pause_next_close();

    let (reply_tx, reply_rx) = crossbeam_channel::bounded(1);
    let release_rig = Arc::clone(&r);
    let _release = std::thread::spawn(move || {
        let _ = reply_tx.send(release_rig.call("device.release", r#"{"selector":"t:COM3"}"#));
    });
    let paused = dev.wait_paused(Duration::from_secs(5));
    go_tx.send(()).expect("放行 open_bridge");
    // 開埠 worker 撞到撤銷標記、actor 處理其 `BridgeOpened` 之後才完成關閉。
    std::thread::sleep(Duration::from_millis(300));
    dev.release_pause();
    let released = reply_rx.recv_timeout(Duration::from_secs(10)).ok();

    assert!(paused, "release 應停在關閉交接 handle 的途中");
    let released = released.expect("release 不得等待已結束的開埠至逾時");
    assert_eq!(released.get("ok"), Some(&PyValue::Bool(true)), "{}", j(&released));
    assert_eq!(s(&sub(&released, "session"), "state"), "RELEASED", "{}", j(&released));
    assert!(!dev.is_open(), "release 回 ok 時 handle 已關閉");
    assert_eq!(dev.close_count(), 1);
    assert_eq!(dev.open_attempts(), attempts, "取消的交接不得另開新 handle");
    assert_eq!(dev.tx_len(), tx_before, "release 之後不得送 TX");
    assert!(!r.mgr.has_inflight_attach_for_test(WP510_PLACEHOLDER_BY_ID));
}

/// wp510 第 15 項（第九次整包審查 High P8-8463b22-1）：同上時點，但 release 關閉交接 handle 暫停到首次期限之後：
/// release 回 DRAIN_STALLED（`origin/main:sw_core/session_manager.py:3192-3204`）後先放行開埠 worker、再放行關閉。
/// Python 的取消旗標隨作業保留（`:3125`），`_attach_by_id` 見取消中止、不開埠（`:4986-4996`），遲到的關閉由
/// `_finish_attach_op` 完成 RELEASED 並落盤（`:4340-4363`）；不需第二次 release。
#[test]
fn wp510_review11_rpc_release_timeout_keeps_revoke_until_bridge_open_reports() {
    let r = Arc::new(wp510_b_placeholder_rig());
    let dev = r.device(WP510_REAL_G).clone();
    let (entered_tx, entered_rx) = crossbeam_channel::bounded(1);
    let (go_tx, go_rx) = crossbeam_channel::bounded::<()>(1);
    r.mgr.set_open_bridge_pre_take_hook_for_test(Box::new(move || {
        let _ = entered_tx.send(());
        let _ = go_rx.recv_timeout(Duration::from_secs(20));
    }));
    let attached = r.call(
        "device.attach",
        &format!(r#"{{"selector":"{WP510_PLACEHOLDER_SID}"}}"#),
    );
    assert_eq!(attached.get("ok"), Some(&PyValue::Bool(true)), "{}", j(&attached));
    entered_rx
        .recv_timeout(Duration::from_secs(5))
        .expect("正式 session 的 open_bridge 應停在接手預開 handle 之前");
    let attempts = dev.open_attempts();
    let tx_before = dev.tx_len();
    let closes_before = dev.close_count();
    dev.pause_next_close();

    let (reply_tx, reply_rx) = crossbeam_channel::bounded(1);
    let release_rig = Arc::clone(&r);
    let release_dev = dev.clone();
    let release = std::thread::spawn(move || {
        let started = std::time::Instant::now();
        let reply = release_rig.call("device.release", r#"{"selector":"t:COM3"}"#);
        let _ = reply_tx.send((reply, release_dev.is_open(), started.elapsed()));
    });
    let paused = dev.wait_paused(Duration::from_secs(5));
    let stalled = reply_rx.recv_timeout(Duration::from_secs(8)).ok();
    // 失敗時也要放行 open_bridge 與關閉，避免殘留執行緒卡住 rig 收尾。
    go_tx.send(()).expect("放行 open_bridge");
    // 開埠 worker 回報、actor 處理其結果之後才完成關閉。
    std::thread::sleep(Duration::from_millis(300));
    let attempts_before_close = dev.open_attempts();
    dev.release_pause();
    release.join().expect("release thread 不應 panic");

    assert!(paused, "release 應停在關閉交接 handle 的途中");
    let (stalled, open_at_reply, elapsed) = stalled.expect("release 須於首次期限回覆");
    assert!(elapsed < Duration::from_secs(6), "release 須於首次期限回覆：{elapsed:?}");
    assert_eq!(s(&stalled, "error_code"), "DRAIN_STALLED", "{}", j(&stalled));
    assert_eq!(stalled.get("drain_stalled"), Some(&PyValue::Bool(true)), "{}", j(&stalled));
    assert_eq!(s(&sub(&stalled, "session"), "state"), "RECOVERING", "{}", j(&stalled));
    assert!(open_at_reply, "前置：release 回覆時關閉仍暫停");
    assert_eq!(
        attempts_before_close, attempts,
        "release 逾時後被取消的開埠不得另開新 handle"
    );

    r.wait_state("t:COM3", "RELEASED");
    wait_until("作業收尾", || {
        !r.mgr.has_inflight_attach_for_test(WP510_PLACEHOLDER_BY_ID)
    });
    wait_until("RELEASED 落盤", || {
        wp510_review10_state_has_released_by_id(&r, WP510_PLACEHOLDER_BY_ID)
    });
    let session = r.session("t:COM3").expect("t:COM3 session");
    assert_eq!(s(&session, "released_by"), "cli", "{}", j(&session));
    assert_eq!(session.get("released_reason"), Some(&PyValue::Null), "{}", j(&session));
    assert!(session.get("released_at").and_then(PyValue::as_str).is_some(), "{}", j(&session));
    assert_eq!(session.get("drain_stalled"), Some(&PyValue::Bool(false)), "{}", j(&session));
    assert_eq!(session.get("drain_timeout_epoch"), Some(&PyValue::Null), "{}", j(&session));
    std::thread::sleep(Duration::from_millis(200));
    assert_eq!(dev.close_count(), closes_before + 1, "交接 handle 只關閉一次");
    assert!(!dev.is_open());
    assert_eq!(dev.open_attempts(), attempts, "被取消的開埠不得另開新 handle");
    assert_eq!(dev.tx_len(), tx_before, "release 之後不得送 TX");
    assert!(!r.mgr.unconfirmed_close_for_test(WP510_PLACEHOLDER_BY_ID));
    assert!(
        !r.mgr.has_handoff_for_test(WP510_PLACEHOLDER_BY_ID),
        "被取消的開埠回報後撤銷標記即移除"
    );
}

/// 同上，但 release 逾時後先放行關閉、再放行開埠 worker：遲到的關閉收尾（`PlaceholderAttachSettled`）時開埠仍在途，
/// actor 等該開埠結束才完成 RELEASED；開埠 worker 撞到仍保留的撤銷標記即不開埠（Python `_attach_by_id` 見取消中止，
/// `origin/main:sw_core/session_manager.py:4986-4996`），不得在已關閉的裝置上另開新 handle。
#[test]
fn wp510_review11_rpc_release_timeout_late_close_before_bridge_open_keeps_revoke() {
    let r = Arc::new(wp510_b_placeholder_rig());
    let dev = r.device(WP510_REAL_G).clone();
    let (entered_tx, entered_rx) = crossbeam_channel::bounded(1);
    let (go_tx, go_rx) = crossbeam_channel::bounded::<()>(1);
    r.mgr.set_open_bridge_pre_take_hook_for_test(Box::new(move || {
        let _ = entered_tx.send(());
        let _ = go_rx.recv_timeout(Duration::from_secs(20));
    }));
    let attached = r.call(
        "device.attach",
        &format!(r#"{{"selector":"{WP510_PLACEHOLDER_SID}"}}"#),
    );
    assert_eq!(attached.get("ok"), Some(&PyValue::Bool(true)), "{}", j(&attached));
    entered_rx
        .recv_timeout(Duration::from_secs(5))
        .expect("正式 session 的 open_bridge 應停在接手預開 handle 之前");
    let attempts = dev.open_attempts();
    let tx_before = dev.tx_len();
    let closes_before = dev.close_count();
    dev.pause_next_close();

    let (reply_tx, reply_rx) = crossbeam_channel::bounded(1);
    let release_rig = Arc::clone(&r);
    let release_dev = dev.clone();
    let release = std::thread::spawn(move || {
        let reply = release_rig.call("device.release", r#"{"selector":"t:COM3"}"#);
        let _ = reply_tx.send((reply, release_dev.is_open()));
    });
    let paused = dev.wait_paused(Duration::from_secs(5));
    let stalled = reply_rx.recv_timeout(Duration::from_secs(8)).ok();
    dev.release_pause();
    // 遲到的關閉收尾送達 actor 後才放行開埠 worker（失敗時也要放行，避免殘留執行緒卡住 rig 收尾）。
    let close_deadline = std::time::Instant::now() + Duration::from_secs(5);
    while dev.is_open() && std::time::Instant::now() < close_deadline {
        std::thread::sleep(Duration::from_millis(2));
    }
    let closed = !dev.is_open();
    std::thread::sleep(Duration::from_millis(300));
    let state_before_open = r
        .session("t:COM3")
        .map(|session| s(&session, "state").to_string());
    go_tx.send(()).expect("放行 open_bridge");
    release.join().expect("release thread 不應 panic");

    assert!(paused, "release 應停在關閉交接 handle 的途中");
    let (stalled, open_at_reply) = stalled.expect("release 須於首次期限回覆");
    assert_eq!(s(&stalled, "error_code"), "DRAIN_STALLED", "{}", j(&stalled));
    assert_eq!(stalled.get("drain_stalled"), Some(&PyValue::Bool(true)), "{}", j(&stalled));
    assert!(open_at_reply, "前置：release 回覆時關閉仍暫停");
    assert!(closed, "遲到的關閉應完成");
    assert_eq!(
        state_before_open.as_deref(),
        Some("RECOVERING"),
        "開埠仍在途時不得先完成 RELEASED"
    );

    r.wait_state("t:COM3", "RELEASED");
    wait_until("作業收尾", || {
        !r.mgr.has_inflight_attach_for_test(WP510_PLACEHOLDER_BY_ID)
    });
    wait_until("RELEASED 落盤", || {
        wp510_review10_state_has_released_by_id(&r, WP510_PLACEHOLDER_BY_ID)
    });
    let session = r.session("t:COM3").expect("t:COM3 session");
    assert_eq!(s(&session, "released_by"), "cli", "{}", j(&session));
    assert_eq!(session.get("drain_stalled"), Some(&PyValue::Bool(false)), "{}", j(&session));
    std::thread::sleep(Duration::from_millis(200));
    assert_eq!(dev.open_attempts(), attempts, "被取消的開埠不得另開新 handle");
    assert_eq!(dev.close_count(), closes_before + 1, "交接 handle 只關閉一次");
    assert!(!dev.is_open());
    assert_eq!(dev.tx_len(), tx_before, "release 之後不得送 TX");
    assert!(
        !r.mgr.has_handoff_for_test(WP510_PLACEHOLDER_BY_ID),
        "被取消的開埠回報後撤銷標記即移除"
    );
}

/// wp510 第十次整包審查 High（P8-14fe592-1）共用前置：佔位交接的正式 session（t:COM3）`open_bridge` 已取走預開 handle
/// （交接列 `taken`）、開埠尚未返回時 `session.bind` 等待逾時，回 SESSION_RECOVERING 並把 session 設為 RECOVERING
/// （Python `bind_session` `origin/main:sw_core/session_manager.py:3676-3711`）。之後放行開埠：actor 不接手、退休 bridge，
/// 退休 bridge 的 handle 關閉停在暫停點（`pause_next_close`）。回傳 (rig, 裝置, 開埠次數, TX 長度, 關閉次數)。
fn wp510_review12_retired_bridge_closing_after_bind_stall(
) -> (Arc<ServiceRig>, FakeDevice, usize, usize, usize) {
    let r = Arc::new(wp510_b_placeholder_rig());
    let dev = r.device(WP510_REAL_G).clone();
    let (entered_tx, entered_rx) = crossbeam_channel::bounded(1);
    let (go_tx, go_rx) = crossbeam_channel::bounded::<()>(1);
    r.mgr.set_open_bridge_post_take_hook_for_test(Box::new(move || {
        let _ = entered_tx.send(());
        let _ = go_rx.recv_timeout(Duration::from_secs(20));
    }));
    let attached = r.call(
        "device.attach",
        &format!(r#"{{"selector":"{WP510_PLACEHOLDER_SID}"}}"#),
    );
    assert_eq!(attached.get("ok"), Some(&PyValue::Bool(true)), "{}", j(&attached));
    let entered = entered_rx.recv_timeout(Duration::from_secs(5)).is_ok();
    let attempts = dev.open_attempts();
    let tx_before = dev.tx_len();
    let closes_before = dev.close_count();
    let bind = r.call(
        "session.bind",
        &format!(r#"{{"selector":"t:COM3","device_by_id":"{WP510_FREE_BY_ID}"}}"#),
    );
    dev.pause_next_close();
    // 失敗時也要放行 open_bridge，避免殘留執行緒卡住 rig 收尾。
    let _ = go_tx.send(());
    assert!(entered, "正式 session 的 open_bridge 應停在取走預開 handle 之後");
    assert_eq!(s(&bind, "error_code"), "SESSION_RECOVERING", "{}", j(&bind));
    assert_eq!(bind.get("drain_stalled"), Some(&PyValue::Bool(true)), "{}", j(&bind));
    let paused = dev.wait_paused(Duration::from_secs(5));
    if !paused {
        dev.release_pause();
    }
    assert!(paused, "開埠成功後退休 bridge 的 handle 關閉應停在暫停點");
    assert!(dev.is_open(), "前置：退休 bridge 的 handle 尚未關閉");
    assert_eq!(dev.open_attempts(), attempts, "前置：沿用交接 handle，未另開");
    (r, dev, attempts, tx_before, closes_before)
}

/// bind 逾時後開埠成功、actor 退休 bridge，關閉回條抵達前呼叫 `device.release`：release 不得在 handle 仍開啟時回成功、
/// 轉 RELEASED 或落盤；handle 確認關閉後才回 ok 並完成 RELEASED 與落盤，不需第二次 release（Python release 等待被取消
/// 作業的 `done`，`_finish_attach_op` 確認關閉後才完成 RELEASED，`origin/main:sw_core/session_manager.py:3188-3215`、
/// `:4320-4363`）。
#[test]
fn wp510_review12_rpc_release_waits_retired_bridge_close_after_bind_stall() {
    let (r, dev, attempts, tx_before, closes_before) =
        wp510_review12_retired_bridge_closing_after_bind_stall();
    let (reply_tx, reply_rx) = crossbeam_channel::bounded(1);
    let release_rig = Arc::clone(&r);
    let release_dev = dev.clone();
    let release = std::thread::spawn(move || {
        let reply = release_rig.call("device.release", r#"{"selector":"t:COM3"}"#);
        let _ = reply_tx.send((reply, release_dev.is_open()));
    });
    let early = reply_rx.recv_timeout(Duration::from_millis(500)).ok();
    let state_while_open = r.session("t:COM3").map(|session| s(&session, "state").to_string());
    let persisted_while_open = wp510_review10_state_has_released_by_id(&r, WP510_PLACEHOLDER_BY_ID);
    dev.release_pause();
    let reply = reply_rx.recv_timeout(Duration::from_secs(10)).ok().or(early.clone());
    release.join().expect("release thread 不應 panic");

    assert!(
        early.is_none(),
        "退休 bridge 的 handle 尚未關閉時 release 不得回覆：{:?}",
        early.as_ref().map(|(reply, open)| (j(reply), *open))
    );
    assert_eq!(state_while_open.as_deref(), Some("RECOVERING"), "handle 關閉前不得轉 RELEASED");
    assert!(!persisted_while_open, "handle 關閉前不得落盤 RELEASED");
    let (reply, open_at_reply) = reply.expect("handle 確認關閉後 release 應回覆");
    assert_eq!(reply.get("ok"), Some(&PyValue::Bool(true)), "{}", j(&reply));
    assert_eq!(s(&sub(&reply, "session"), "state"), "RELEASED", "{}", j(&reply));
    assert!(!open_at_reply, "release 回 ok 時 handle 已關閉");
    assert!(wp510_review10_state_has_released_by_id(&r, WP510_PLACEHOLDER_BY_ID), "RELEASED 必須落盤");
    let session = r.session("t:COM3").expect("t:COM3 session");
    assert_eq!(s(&session, "released_by"), "cli", "{}", j(&session));
    assert_eq!(session.get("drain_stalled"), Some(&PyValue::Bool(false)), "{}", j(&session));
    std::thread::sleep(Duration::from_millis(200));
    assert_eq!(dev.close_count(), closes_before + 1, "handle 只關閉一次");
    assert!(!dev.is_open());
    assert_eq!(dev.open_attempts(), attempts, "不得另開新 handle");
    assert_eq!(dev.tx_len(), tx_before, "不得送 TX");
    assert!(!r.mgr.unconfirmed_close_for_test(WP510_PLACEHOLDER_BY_ID));
}

/// 同上，但退休 bridge 的 handle 關閉失敗（`closed:false`）：release 回 DRAIN_STALLED，不轉 RELEASED、不落盤，by_id
/// 登記為未確認關閉（Python `_finish_attach_op` 關閉失敗即回 False、保留作業且不設 `done`，release 回 DRAIN_STALLED，
/// `origin/main:sw_core/session_manager.py:4322-4332`、`:3192-3204`）；之後的 release 仍為 DRAIN_STALLED。
#[test]
fn wp510_review12_rpc_release_retired_bridge_close_failure_reports_drain_stalled() {
    let (r, dev, attempts, tx_before, _) = wp510_review12_retired_bridge_closing_after_bind_stall();
    let (reply_tx, reply_rx) = crossbeam_channel::bounded(1);
    let release_rig = Arc::clone(&r);
    let release = std::thread::spawn(move || {
        let _ = reply_tx.send(release_rig.call("device.release", r#"{"selector":"t:COM3"}"#));
    });
    let early = reply_rx.recv_timeout(Duration::from_millis(500)).ok();
    dev.fail_close(PalError::io(5, "injected close failure"));
    dev.release_pause();
    let reply = reply_rx.recv_timeout(Duration::from_secs(10)).ok().or(early.clone());
    release.join().expect("release thread 不應 panic");

    assert!(
        early.is_none(),
        "退休 bridge 的 handle 關閉回條前 release 不得回覆：{:?}",
        early.as_ref().map(j)
    );
    let reply = reply.expect("關閉失敗後 release 應回覆");
    assert_eq!(s(&reply, "error_code"), "DRAIN_STALLED", "{}", j(&reply));
    assert_eq!(reply.get("drain_stalled"), Some(&PyValue::Bool(true)), "{}", j(&reply));
    assert_eq!(s(&sub(&reply, "session"), "state"), "RECOVERING", "{}", j(&reply));
    assert!(
        r.mgr.unconfirmed_close_for_test(WP510_PLACEHOLDER_BY_ID),
        "退休 bridge 的 handle 關閉未確認須登記阻擋"
    );
    let again = r.call("device.release", r#"{"selector":"t:COM3"}"#);
    assert_eq!(s(&again, "error_code"), "DRAIN_STALLED", "{}", j(&again));
    let session = r.session("t:COM3").expect("t:COM3 session");
    assert_eq!(s(&session, "state"), "RECOVERING", "{}", j(&session));
    assert!(
        !wp510_review10_state_has_released_by_id(&r, WP510_PLACEHOLDER_BY_ID),
        "關閉未確認不得落盤 RELEASED"
    );
    assert_eq!(dev.open_attempts(), attempts, "不得另開新 handle");
    assert_eq!(dev.tx_len(), tx_before, "不得送 TX");
}

const WP510_R10_BY_ID_D: &str = "/s/by-id/usb-r10-D";
const WP510_R10_REAL_D: &str = "/dev/ttyR10D";

/// 可切換阻塞的開埠後端：`block()` 之後對指定路徑的開埠停在閘門，`unblock()` 放行。
struct Wp510ToggleOpenBackend {
    delegate: Arc<FakeSerialBackend>,
    path: String,
    blocked: Mutex<bool>,
    cv: Condvar,
    attempts: AtomicUsize,
}

impl Wp510ToggleOpenBackend {
    fn block(&self) {
        *self.blocked.lock().unwrap_or_else(|e| e.into_inner()) = true;
    }

    fn unblock(&self) {
        *self.blocked.lock().unwrap_or_else(|e| e.into_inner()) = false;
        self.cv.notify_all();
    }

    fn attempts(&self) -> usize {
        self.attempts.load(Ordering::SeqCst)
    }
}

impl SerialBackend for Wp510ToggleOpenBackend {
    fn open_exclusive(
        &self,
        dev: &DevicePath,
        cfg: &SerialConfig,
    ) -> Result<Box<dyn SerialHandle>, OpenError> {
        if dev.0 == self.path {
            self.attempts.fetch_add(1, Ordering::SeqCst);
            let mut blocked = self.blocked.lock().unwrap_or_else(|e| e.into_inner());
            while *blocked {
                blocked = self.cv.wait(blocked).unwrap_or_else(|e| e.into_inner());
            }
        }
        self.delegate.open_exclusive(dev, cfg)
    }
}

/// 失敗時放行阻塞的開埠，避免殘留 worker 卡住 rig 收尾。
struct Wp510UnblockOnDrop(Arc<Wp510ToggleOpenBackend>);

impl Drop for Wp510UnblockOnDrop {
    fn drop(&mut self) {
        self.0.unblock();
    }
}

/// wp510 第八次整包審查 High（P8-e25b69c-2）：既有 fallback session 重插後的 #181 再偵測卡在開埠（作業在
/// `_attach_ops`，`origin/main:sw_core/session_manager.py:4191`，`session_id` 為該 session `:4185-4186`）時
/// `device.release`：期限到回 DRAIN_STALLED＋`drain_stalled:true`、state RECOVERING（`:3192-3204`）。開埠遲到返回後
/// 見取消即關閉並 `_finish_attach_op`（`:4216-4245`）：`release_request` 存在且查得到 session，故轉 RELEASED、
/// `released_by` 取第一個 release 的 source、`released_reason` 取其 reason、清 `drain_stalled`／`drain_timeout_epoch`
/// 並持久化（`:4340-4363`）；之後的 release 回 `already_released`（`:3142-3144`）。
#[test]
fn wp510_review10_rpc_release_timeout_then_late_redetect_finish_completes_release() {
    let delegate = FakeSerialBackend::new();
    // 不安裝 shell 應答：動態偵測無 template 命中，以 passthrough fallback 建 session。
    let dev = delegate.add_device(WP510_R10_REAL_D);
    let backend = Arc::new(Wp510ToggleOpenBackend {
        delegate,
        path: WP510_R10_REAL_D.to_string(),
        blocked: Mutex::new(false),
        cv: Condvar::new(),
        attempts: AtomicUsize::new(0),
    });
    let _unblock = Wp510UnblockOnDrop(Arc::clone(&backend));
    let serial: Arc<dyn SerialBackend> = backend.clone();
    let spec = RigSpec {
        yaml: "profiles:\n  t:\n    platform: shell\n    prompt_regex: '(?m)^root@t:.*# '\n    timeout_s: 2.0\n  others-template:\n    platform: passthrough\n    prompt_regex: '.*'\n    login_regex: '$^'\n    password_regex: '$^'\n    ready_probe: ''\n".to_string(),
        devices: vec![(WP510_R10_BY_ID_D.to_string(), WP510_R10_REAL_D.to_string())],
        explicit_target_count: 0,
        allowlist: None,
        wait_ready: Vec::new(),
    };
    let r = ServiceRig::build_with_loaded_state_and_serial_backend(spec, LoadedState::default(), serial);
    r.wait_state("COM0", "ATTACHED");
    let attached = r.session("COM0").expect("COM0 session");
    assert_eq!(s(&attached, "profile_source"), "fallback", "{}", j(&attached));
    let snapshot = vec![(
        WP510_R10_BY_ID_D.to_string(),
        DeviceInfo {
            by_id: WP510_R10_BY_ID_D.to_string(),
            real_path: WP510_R10_REAL_D.to_string(),
        },
    )];
    r.mgr.update_devices(&[]);
    r.wait_state("COM0", "DETACHED");
    backend.block();
    let attempts_before = backend.attempts();
    r.mgr.update_devices(&snapshot);
    wait_until("再偵測卡在開埠", || backend.attempts() > attempts_before);
    let closes_before = dev.close_count();
    let tx_before = dev.tx_len();

    let started = std::time::Instant::now();
    let released = r.call("device.release", r#"{"selector":"COM0"}"#);
    let elapsed = started.elapsed();
    assert_eq!(s(&released, "error_code"), "DRAIN_STALLED", "{}", j(&released));
    assert_eq!(released.get("drain_stalled"), Some(&PyValue::Bool(true)), "{}", j(&released));
    assert_eq!(s(&sub(&released, "session"), "state"), "RECOVERING", "{}", j(&released));
    assert!(elapsed >= Duration::from_millis(2500), "release 須等待作業至期限：{elapsed:?}");
    // S2-PAR-wp510r R4（設計 §6 #67，H）：W6 作業改為外部 attach，在途改以帳本紀錄判定：阻塞時 `Opening{Worker}`、
    // cancel＝Release；收尾後 `custody_for_test(B)`＝None。
    let inflight = r.mgr.custody_for_test(WP510_R10_BY_ID_D).expect("阻塞時帳本紀錄在途");
    assert!(
        matches!(
            inflight.phase,
            sw_core::custody::CustodyPhaseSnapshot::Opening {
                taker: sw_core::custody::CustodyTaker::Worker(_)
            }
        ) && inflight.cancel == Some(sw_core::custody::CustodyCancel::Release),
        "{inflight:?}"
    );

    backend.unblock();
    r.wait_state("COM0", "RELEASED");
    wait_until("作業收尾", || r.mgr.custody_for_test(WP510_R10_BY_ID_D).is_none());
    wait_until("RELEASED 落盤", || wp510_review10_state_has_released_by_id(&r, WP510_R10_BY_ID_D));
    let session = r.session("COM0").expect("COM0 session");
    assert_eq!(s(&session, "released_by"), "cli", "{}", j(&session));
    assert_eq!(session.get("released_reason"), Some(&PyValue::Null), "{}", j(&session));
    assert!(session.get("released_at").and_then(PyValue::as_str).is_some(), "{}", j(&session));
    assert_eq!(session.get("drain_stalled"), Some(&PyValue::Bool(false)), "{}", j(&session));
    assert_eq!(session.get("drain_timeout_epoch"), Some(&PyValue::Null), "{}", j(&session));
    assert_eq!(s(&session, "last_error"), "RELEASED", "{}", j(&session));
    assert_eq!(dev.close_count(), closes_before + 1, "取消的作業關閉遲到的 handle");
    assert!(!dev.is_open());
    assert_eq!(dev.tx_len(), tx_before, "取消後不得送 PROBE");

    let again = r.call("device.release", r#"{"selector":"COM0"}"#);
    assert_eq!(again.get("ok"), Some(&PyValue::Bool(true)), "{}", j(&again));
    assert_eq!(again.get("already_released"), Some(&PyValue::Bool(true)), "{}", j(&again));
}

/// 落盤失敗後再次 `_spawn_attach`（bootstrap）：`_attach_by_id` 取走 `_preopened_ports` 的作業，dynamic 偵測沿用同一
/// handle，於保留的原 COM 建立正式 session（`origin/main:sw_core/session_manager.py:4852-4854`、`:4886-4888`）。
#[test]
fn wp510_review7_rpc_spawn_after_retirement_enospc_adopts_preopened_handle() {
    let r = wp510_b_placeholder_rig();
    let dev = r.device(WP510_REAL_G).clone();
    // ServiceRig 的預設空行回覆是一次性；dynamic 偵測的 PROBE 會消耗它，正式 bridge 仍需 shell prompt。
    dev.on_tx_match("^$", Box::new(|_| b"root@t:/# ".to_vec()));
    r.fs.fail_next_atomic_write(PalError::io(
        28,
        "[Errno 28] No space left on device",
    ));
    let _ = r.call_err(
        "device.attach",
        &format!(r#"{{"selector":"{WP510_PLACEHOLDER_SID}"}}"#),
    );
    // usb-C 亦在線且無 session：bootstrap 會與 usb-G 搶最後一個 max_sessions 名額（Python 同為依序 `_spawn_attach`）。
    // 先把 usb-C 自快照移除，只觀察 usb-G 的接手；usb-G 的路徑不變，未消費的作業不受影響。
    let without_free: Vec<(String, DeviceInfo)> = r
        .mgr
        .list_devices()
        .into_iter()
        .filter_map(|row| {
            let by_id = s(&row, "by_id").to_string();
            let real_path = s(&row, "real_path").to_string();
            (by_id != WP510_FREE_BY_ID).then(|| (by_id.clone(), DeviceInfo { by_id, real_path }))
        })
        .collect();
    r.mgr.update_devices(&without_free);
    assert!(dev.is_open(), "usb-G 未變動，預開 handle 仍在作業內");
    let attempts = dev.open_attempts();

    r.mgr.bootstrap_attach();

    r.wait_state("t:COM3", "READY");
    assert_eq!(dev.open_attempts(), attempts, "沿用預開 handle，不重新開埠");
    assert_eq!(dev.close_count(), 0);
}

/// 落盤失敗後裝置拔除：Python `update_devices` 取出未消費的 `_preopened_ports` 作業並關閉（`:4045-4063`）；插回後
/// 照常 dynamic attach 重新開埠。
#[test]
fn wp510_review7_hotplug_after_retirement_enospc_closes_preopened_handle() {
    let r = wp510_b_placeholder_rig();
    let dev = r.device(WP510_REAL_G).clone();
    r.fs.fail_next_atomic_write(PalError::io(
        28,
        "[Errno 28] No space left on device",
    ));
    let _ = r.call_err(
        "device.attach",
        &format!(r#"{{"selector":"{WP510_PLACEHOLDER_SID}"}}"#),
    );
    let full: Vec<(String, DeviceInfo)> = r
        .mgr
        .list_devices()
        .into_iter()
        .map(|row| {
            let by_id = s(&row, "by_id").to_string();
            let real_path = s(&row, "real_path").to_string();
            (by_id.clone(), DeviceInfo { by_id, real_path })
        })
        .collect();
    let without: Vec<_> = full
        .iter()
        .filter(|(by_id, _)| by_id != WP510_PLACEHOLDER_BY_ID)
        .cloned()
        .collect();

    r.mgr.update_devices(&without);

    assert_eq!(dev.close_count(), 1, "拔除時關閉未消費的預開 handle");
    assert!(!dev.is_open());
    assert!(!r.mgr.has_inflight_attach_for_test(WP510_PLACEHOLDER_BY_ID));
    let attempts = dev.open_attempts();
    r.mgr.update_devices(&full);
    wait_until("插回後重新開埠", || dev.open_attempts() > attempts);
}

/// 落盤失敗後 shutdown：Python `shutdown_drain` 對未消費的 `_preopened_ports` 作業執行 `_finish_attach_op` 關閉
/// handle，關閉成功即完成、不列 stalled（`origin/main:sw_core/session_manager.py:1218-1227`）。
#[test]
fn wp510_review7_shutdown_after_retirement_enospc_closes_preopened_handle() {
    let r = wp510_b_placeholder_rig();
    let dev = r.device(WP510_REAL_G).clone();
    r.fs.fail_next_atomic_write(PalError::io(
        28,
        "[Errno 28] No space left on device",
    ));
    let _ = r.call_err(
        "device.attach",
        &format!(r#"{{"selector":"{WP510_PLACEHOLDER_SID}"}}"#),
    );

    let drained = r.mgr.shutdown(Duration::from_millis(1500));

    assert!(
        drained
            .iter()
            .any(|(target, ok)| target == &format!("attach:{WP510_PLACEHOLDER_BY_ID}") && *ok),
        "未消費的預開 handle 關閉後即排空：{drained:?}"
    );
    assert_eq!(dev.close_count(), 1);
    assert!(!dev.is_open());
}

/// wp510 第四次整包審查：佔位列的 `released_by`／`released_at` 為 null（reason 保留）。
fn wp510_review4_null_metadata_loaded_state() -> LoadedState {
    let mut loaded = wp510_b_placeholder_loaded_state();
    loaded.released.insert(
        WP510_PLACEHOLDER_SID.to_string(),
        ReleasedRow {
            by_id: PyValue::from(WP510_PLACEHOLDER_BY_ID),
            released_by: PyValue::Null,
            released_at: PyValue::Null,
            reason: PyValue::from("manual"),
        },
    );
    loaded
}

/// state.json 中指定 sid 的 released 列（不存在 panic）。
fn wp510_review4_released_row(r: &ServiceRig, sid: &str) -> PyDict {
    let text = String::from_utf8(r.fs.bytes(support::STATE_PATH)).expect("state.json 應為 UTF-8");
    let value = sw_proto::pyjson::loads(&text).expect("state.json 應可解析");
    let PyValue::Dict(state) = value else {
        panic!("state.json 頂層應為 dict：{text}");
    };
    let released = sub(&state, "released");
    sub(&released, sid)
}

/// 佔位 device.attach 停在開埠時送 device.release（RPC 預設 source=cli）；release 取得在途作業後才放行開埠。
/// 回傳 `(attach 回應, release 結果)`。
fn wp510_review4_rpc_release_during_placeholder_open(
    r: &Arc<ServiceRig>,
    backend: &Wp510BlockingOpenBackend,
    entered: &crossbeam_channel::Receiver<()>,
) -> (PyDict, Result<PyDict, sw_proto::pycompat::PyErr>) {
    let (cancel_tx, cancel_rx) = crossbeam_channel::bounded::<()>(1);
    r.mgr.set_placeholder_release_cancel_hook_for_test(Box::new(move || {
        let _ = cancel_tx.try_send(());
    }));
    let attach_rig = Arc::clone(r);
    let attach = std::thread::spawn(move || {
        attach_rig.call(
            "device.attach",
            &format!(r#"{{"selector":"{WP510_PLACEHOLDER_SID}"}}"#),
        )
    });
    entered
        .recv_timeout(Duration::from_secs(5))
        .expect("placeholder open_exclusive 必須進入阻塞點");
    let release_rig = Arc::clone(r);
    let release = std::thread::spawn(move || {
        release_rig.call_raw(
            "device.release",
            &format!(r#"{{"selector":"{WP510_PLACEHOLDER_SID}"}}"#),
        )
    });
    cancel_rx
        .recv_timeout(Duration::from_secs(5))
        .expect("device.release 應取得在途佔位 attach 作業");
    backend.release();
    let attached = attach.join().expect("attach RPC thread");
    let released = release.join().expect("release RPC thread");
    (attached, released)
}

/// wp510 第四次整包審查 Medium 1（RPC）：null 來源與時間的佔位開埠在途時 `device.release`（預設 source=cli）撤銷作業；
/// handle 安全關閉後 release 回 `already_released`，補齊 `released_by`／`released_at` 並寫入 state.json。Python 對照：
/// `origin/main:sw_core/session_manager.py:3117-3122`、`:4341-4363`、`:3206-3209`。呼叫路徑：`Service::rpc("device.release")`
/// → `SessionManager::route_with_selector` → actor 的 release（第 15 項起取代 `release_attach_handles`；記錄 source）→ attach worker
/// `finish_attach_op` → `SessionRequest::PlaceholderAttachSettled` → `SessionCore::persist_release` → `Effect::Persist`。
#[test]
fn wp510_review4_rpc_release_cancel_fills_null_release_metadata_and_persists() {
    let (r, backend, entered, dev) =
        wp510_b_placeholder_blocking_rig_with(wp510_review4_null_metadata_loaded_state(), None);
    let r = Arc::new(r);
    let before = r
        .session(WP510_PLACEHOLDER_SID)
        .expect("release 測試前置佔位");
    assert_eq!(before.get("released_by"), Some(&PyValue::Null), "前置：released_by 為 null");
    assert_eq!(before.get("released_at"), Some(&PyValue::Null), "前置：released_at 為 null");
    r.clock.set_wall_us(r.clock.wall_unix_us() + 5_000_000);
    let filled_at = iso_utc_now(r.clock.wall_unix_us());
    let tx_mark = dev.tx_len();

    let (attached, released) = wp510_review4_rpc_release_during_placeholder_open(&r, &backend, &entered);
    let released = released.unwrap_or_else(|e| panic!("device.release 不應回 Err：{}", e.message));

    let mut expected_session = before.clone();
    expected_session.insert("released_by", PyValue::from("cli"));
    expected_session.insert("released_at", PyValue::from(filled_at.as_str()));
    assert_eq!(s(&attached, "error_code"), "SESSION_RECOVERING", "{}", j(&attached));
    assert_eq!(released.get("ok"), Some(&PyValue::Bool(true)), "{}", j(&released));
    assert_eq!(released.get("already_released"), Some(&PyValue::Bool(true)), "{}", j(&released));
    assert_eq!(
        j(&sub(&released, "session")),
        j(&expected_session),
        "release 回應的 session 須補齊來源與時間，其餘欄位不變"
    );
    let row = wp510_review4_released_row(&r, WP510_PLACEHOLDER_SID);
    assert_eq!(
        j(&row),
        j(&sw_proto::pydict! {
            "by_id" => WP510_PLACEHOLDER_BY_ID,
            "reason" => "manual",
            "released_at" => filled_at.as_str(),
            "released_by" => "cli",
        }),
        "state.json 的 released 列須寫入補齊值"
    );
    assert_eq!(r.mgr.released_placeholder(WP510_PLACEHOLDER_SID), Some(true));
    assert!(r.mgr.resolve("t:COM3").is_none(), "release 成功後不得建立 dynamic session");
    assert!(dev.tx_since(tx_mark).is_empty(), "release 成功後不得送 UART TX");
}

/// wp510 第四次整包審查 Medium 1（RPC 持久化失敗）：同一情境下 state.json 持續 ENOSPC，Python 收尾持久化失敗只記錄
/// （`:4354-4362`），release 重試（`:3206-3207`）仍失敗即拋出；`device.release` 必須走 RPC 錯誤路徑而非回成功，
/// 磁碟恢復後下一次 `device.release` 寫入補齊值。
#[test]
fn wp510_review4_rpc_release_cancel_persist_enospc_returns_error_then_retries() {
    let (r, backend, entered, _dev) =
        wp510_b_placeholder_blocking_rig_with(wp510_review4_null_metadata_loaded_state(), None);
    let r = Arc::new(r);
    r.clock.set_wall_us(r.clock.wall_unix_us() + 5_000_000);
    let filled_at = iso_utc_now(r.clock.wall_unix_us());
    r.fs.fail_next_atomic_writes(
        PalError::io(28, "[Errno 28] No space left on device"),
        2,
    );

    let (attached, released) = wp510_review4_rpc_release_during_placeholder_open(&r, &backend, &entered);
    assert_eq!(s(&attached, "error_code"), "SESSION_RECOVERING", "{}", j(&attached));
    let error = match released {
        Ok(response) => panic!("持續 ENOSPC 時 device.release 不得回成功：{}", j(&response)),
        Err(error) => error,
    };
    assert!(
        error.message.contains("No space left on device"),
        "ENOSPC 必須沿 RPC error 路徑傳回：{}",
        error.message
    );
    assert_eq!(r.mgr.released_placeholder(WP510_PLACEHOLDER_SID), Some(true));
    assert_eq!(
        wp510_review4_released_row(&r, WP510_PLACEHOLDER_SID).get("released_by"),
        Some(&PyValue::Null),
        "落盤失敗時 state.json 仍是舊列"
    );

    let retry = r.call(
        "device.release",
        &format!(r#"{{"selector":"{WP510_PLACEHOLDER_SID}","source":"agent"}}"#),
    );
    assert_eq!(retry.get("already_released"), Some(&PyValue::Bool(true)), "{}", j(&retry));
    let retry_session = sub(&retry, "session");
    assert_eq!(s(&retry_session, "released_by"), "cli", "重試沿用收尾補齊的來源");
    let row = wp510_review4_released_row(&r, WP510_PLACEHOLDER_SID);
    assert_eq!(s(&row, "released_by"), "cli");
    assert_eq!(s(&row, "released_at"), filled_at);
    assert_eq!(s(&row, "reason"), "manual");
}

/// wp510 第四次整包審查盤點一（RPC）：一般 session 於 T0 release、T1 `device.attach`；開埠成功且 unreleased 落盤後的
/// 回應須帶 T1（Python `attach_device` `origin/main:sw_core/session_manager.py:3599` 設 ATTACHING，setter `:617-620`，
/// `:3602` 產生回應）。呼叫路徑：`Service::rpc("device.attach")` → `SessionManager::route_with_selector` →
/// `SessionCore::attach_request` → `Effect::OpenBridge` → `bridge_opened` → `Persist(Unreleased)` → `state_committed`。
#[test]
fn wp510_review4_rpc_attach_from_released_stamps_last_state_change_at() {
    let r = ServiceRig::ready_shell();
    let released = r.call("device.release", r#"{"selector":"COM0"}"#);
    assert_eq!(released.get("ok"), Some(&PyValue::Bool(true)), "{}", j(&released));
    r.wait_state("COM0", "RELEASED");
    let t0 = s(&r.session("COM0").expect("COM0"), "last_state_change_at").to_string();
    r.clock.set_wall_us(r.clock.wall_unix_us() + 5_000_000);
    let t1 = iso_utc_now(r.clock.wall_unix_us());

    let attached = r.call("device.attach", r#"{"selector":"COM0"}"#);
    let session = sub(&attached, "session");

    assert_eq!(attached.get("ok"), Some(&PyValue::Bool(true)), "{}", j(&attached));
    assert_eq!(s(&session, "state"), "ATTACHING", "{}", j(&attached));
    assert_ne!(t0, t1, "前置：release 與 attach 的 wall 不同");
    assert_eq!(
        s(&session, "last_state_change_at"),
        t1,
        "RELEASED→ATTACHING 的 device.attach 回應必須帶 T1"
    );
}

/// wp510 第五次整包審查 Medium（RPC 端到端）：`device.attach` 經 `Service::rpc` → `route_with_selector` →
/// `attach_released_placeholder`，交接 worker 的 `Builder::spawn()` 失敗。Python `attach_device` 同步
/// `_spawn_attach`（`origin/main:sw_core/session_manager.py:3633-3638`），`worker.start()` 的 `RuntimeError`
/// 穿出 RPC（`:4147-4150`）成為 EXCEPTION；佔位與持久化阻擋須保留，之後可重新交接。
#[test]
fn wp510_review5_rpc_handoff_worker_spawn_failure_returns_exception() {
    let r = wp510_b_placeholder_rig();
    let dev = r.device(WP510_REAL_G);
    r.mgr.fail_next_attach_spawn_for_test();

    let error = r.call_err(
        "device.attach",
        &format!(r#"{{"selector":"{WP510_PLACEHOLDER_SID}"}}"#),
    );

    assert_eq!(error.message, "can't start new thread");
    assert_eq!(r.mgr.released_placeholder(WP510_PLACEHOLDER_SID), Some(true));
    assert_eq!(
        s(
            &r.session(WP510_PLACEHOLDER_SID).expect("建立失敗後佔位仍存在"),
            "state"
        ),
        "RELEASED"
    );
    assert!(r.mgr.resolve("t:COM3").is_none(), "未建立 worker 不得出現正式 session");
    assert_eq!(dev.open_count(), 0, "worker 建立失敗前不得開埠");
    assert_eq!(dev.tx_len(), 0, "worker 建立失敗不得送出任何 TX");
    assert!(!r.mgr.has_inflight_attach_for_test(WP510_PLACEHOLDER_BY_ID));

    let retry = r.call(
        "device.attach",
        &format!(r#"{{"selector":"{WP510_PLACEHOLDER_SID}"}}"#),
    );
    assert_eq!(
        retry.get("ok"),
        Some(&PyValue::Bool(true)),
        "建立失敗後佔位仍可交接：{}",
        j(&retry)
    );
}

/// wp510 第五次整包審查 High（RPC 端到端）：`device.attach` 交接後、正式 bridge 的 `open_bridge` 接手預開 handle 前
/// shutdown，關閉回條為 `closed:false`。Python 作業關閉失敗即保留（`origin/main:sw_core/session_manager.py:4322-4332`），
/// `shutdown_drain` 末段列入 stalled（`:1231-1239`）；不得因 daemon stopping 的開埠錯誤回報已排空。
#[test]
fn wp510_review5_rpc_shutdown_before_bridge_takes_preopened_fail_close_reports_stalled() {
    let r = wp510_b_placeholder_rig();
    let dev = r.device(WP510_REAL_G);
    let (entered_tx, entered_rx) = crossbeam_channel::bounded(1);
    let (go_tx, go_rx) = crossbeam_channel::bounded::<()>(1);
    r.mgr.set_open_bridge_pre_take_hook_for_test(Box::new(move || {
        let _ = entered_tx.send(());
        let _ = go_rx.recv_timeout(Duration::from_secs(10));
    }));

    let attached = r.call(
        "device.attach",
        &format!(r#"{{"selector":"{WP510_PLACEHOLDER_SID}"}}"#),
    );
    assert_eq!(
        attached.get("ok"),
        Some(&PyValue::Bool(true)),
        "device.attach 應交接在線佔位：{}",
        j(&attached)
    );
    entered_rx
        .recv_timeout(Duration::from_secs(5))
        .expect("正式 session 的 open_bridge 應停在接手預開 handle 之前");
    dev.fail_close(PalError::io(5, "[Errno 5] Input/output error"));
    let manager = r.mgr.clone();
    let shutdown = std::thread::spawn(move || manager.shutdown(Duration::from_millis(1500)));
    let deadline = std::time::Instant::now() + Duration::from_secs(5);
    while r.session("t:COM3").map(|session| s(&session, "state").to_string()).as_deref()
        != Some("RECOVERING")
        && std::time::Instant::now() < deadline
    {
        std::thread::sleep(Duration::from_millis(2));
    }
    go_tx.send(()).expect("放行 open_bridge");
    let drained = shutdown.join().expect("shutdown thread 不應 panic");

    assert!(
        drained
            .iter()
            .any(|(target, ok)| target == &format!("attach:{WP510_PLACEHOLDER_BY_ID}") && !ok),
        "未確認關閉的預開 handle 必須列入 shutdown stalled：{drained:?}"
    );
    assert!(
        drained.iter().any(|(target, ok)| target == "t:COM3" && !ok),
        "daemon stopping 的開埠錯誤不得把正式 session 計為已排空：{drained:?}"
    );
    assert!(dev.is_open(), "closed:false 表示 handle 仍開啟");
    assert!(
        r.mgr.has_inflight_attach_for_test(WP510_PLACEHOLDER_BY_ID),
        "未確認關閉的 handle 必須持續阻擋"
    );
    assert_eq!(dev.open_count(), 1, "停機後不得重新開埠");
}

/// wp510 第六輪增量審查 P8-fdc1413-1（High，RPC 端到端）：`device.attach` 經 `Service::rpc` → `route_with_selector`
/// → `attach_released_placeholder` 交接；attach worker 送出 `DeviceAttached` 後停在 `finish_attach_op` 取鎖之前，
/// 正式 session 的 `open_bridge` 套用 UART 設定失敗且關閉回條 `closed:false`（`retain_unconfirmed_close` 標記同一作業）。
/// worker 恢復後必須保留作業：Python `_finish_attach_op` 關閉失敗不設 `done`、保留作業
/// （`origin/main:sw_core/session_manager.py:4322-4332`），`device.release` 等待逾期回 DRAIN_STALLED（`:3192-3204`），
/// shutdown 列入 stalled（`:1231-1239`）。
#[test]
fn wp510_review6_rpc_finish_attach_op_race_keeps_unconfirmed_close_blocking() {
    let r = wp510_b_placeholder_rig();
    let dev = r.device(WP510_REAL_G);
    let (finish_entered_tx, finish_entered_rx) = crossbeam_channel::bounded(1);
    let (finish_go_tx, finish_go_rx) = crossbeam_channel::bounded::<()>(1);
    r.mgr.set_finish_attach_op_pre_lock_hook_for_test(
        WP510_PLACEHOLDER_BY_ID,
        Box::new(move || {
            let _ = finish_entered_tx.send(());
            let _ = finish_go_rx.recv_timeout(Duration::from_secs(10));
        }),
    );
    let (bridge_entered_tx, bridge_entered_rx) = crossbeam_channel::bounded(1);
    let (bridge_go_tx, bridge_go_rx) = crossbeam_channel::bounded::<()>(1);
    r.mgr.set_open_bridge_pre_take_hook_for_test(Box::new(move || {
        let _ = bridge_entered_tx.send(());
        let _ = bridge_go_rx.recv_timeout(Duration::from_secs(10));
    }));
    dev.fail_configure(PalError::io(5, "[Errno 5] Input/output error"));
    dev.fail_close(PalError::io(5, "[Errno 5] Input/output error"));

    let attached = r.call(
        "device.attach",
        &format!(r#"{{"selector":"{WP510_PLACEHOLDER_SID}"}}"#),
    );
    assert_eq!(
        attached.get("ok"),
        Some(&PyValue::Bool(true)),
        "device.attach 應交接在線佔位：{}",
        j(&attached)
    );
    bridge_entered_rx
        .recv_timeout(Duration::from_secs(5))
        .expect("正式 session 的 open_bridge 應停在接手預開 handle 之前");
    finish_entered_rx
        .recv_timeout(Duration::from_secs(5))
        .expect("attach worker 應停在 finish_attach_op 取鎖之前");
    // 偵測 PROBE 已在 worker 收尾前送出；之後的設定失敗與收尾不得再有 TX。
    let tx_before_bridge = dev.tx_len();
    bridge_go_tx.send(()).expect("放行 open_bridge");
    let deadline = std::time::Instant::now() + Duration::from_secs(5);
    while r.session("t:COM3").map(|session| s(&session, "state").to_string()).as_deref()
        != Some("DETACHED")
        && std::time::Instant::now() < deadline
    {
        std::thread::sleep(Duration::from_millis(2));
    }
    assert_eq!(
        r.session("t:COM3").map(|session| s(&session, "state").to_string()).as_deref(),
        Some("DETACHED"),
        "正式 session 的開埠失敗應先完成"
    );
    finish_go_tx.send(()).expect("放行 attach worker 收尾");
    // worker 恢復後只剩取鎖與判定；以有界等待觀察作業是否被移除（保留時於期限後回 false）。
    let removed = r
        .mgr
        .wait_attach_idle_for_test(WP510_PLACEHOLDER_BY_ID, Duration::from_millis(500));

    let inflight_kept = r.mgr.has_inflight_attach_for_test(WP510_PLACEHOLDER_BY_ID);
    let released = r.call("device.release", r#"{"selector":"t:COM3"}"#);
    let drained = r.mgr.shutdown(Duration::from_millis(100));
    let observed = format!(
        "removed={removed} inflight={inflight_kept} release={} shutdown={drained:?}",
        j(&released)
    );

    assert!(inflight_kept, "未確認關閉的 handle 必須保留 inflight 阻擋：{observed}");
    assert_eq!(s(&released, "error_code"), "DRAIN_STALLED", "{observed}");
    assert_eq!(released.get("drain_stalled"), Some(&PyValue::Bool(true)), "{observed}");
    assert_ne!(
        s(&sub(&released, "session"), "state"),
        "RELEASED",
        "未確認關閉的 handle 不得完成 release：{observed}"
    );
    assert!(
        drained
            .iter()
            .any(|(target, ok)| target == &format!("attach:{WP510_PLACEHOLDER_BY_ID}") && !ok),
        "未確認關閉的 handle 必須列入 shutdown stalled：{observed}"
    );
    assert!(dev.is_open(), "closed:false 表示 handle 仍開啟");
    assert_eq!(dev.open_count(), 1, "不得重新開埠");
    assert_eq!(dev.tx_len(), tx_before_bridge, "設定失敗的 handle 不得再送出任何 TX");
}

// ---------------------------------------------------------------------------------------------------------------
// wp510 第六次整包審查 High（第 13 項）：未確認關閉的 by_id 須擋下所有重新開埠入口。
//
// Python 在 `bridge.start()` 失敗時以 `_track_attach_bridge_stop` 把 attach 清理納入 `_draining_by_ids`
// （`origin/main:sw_core/session_manager.py:5185-5207`、`:1286-1348`），只有安全回條能解除；`attach_device` 在任何
// 再次開埠前回 `SESSION_RECOVERING`（`:3410-3417`），其餘入口經 `_spawn_attach`（`:4113`）／`_attach_by_id`
// （`:4847`）早退、不開埠。各入口的回應形狀照 Python（`attach_session` `:3876-3999`、`recover_session`
// `:7970-7976`、`clear_session` `:3024-3082`、`bind_session` `:3655-3812`）。
// ---------------------------------------------------------------------------------------------------------------

/// 佔位交接後正式 session `t:COM3` 的 `open_bridge` 套用 UART 設定失敗、關閉回條 `closed:false`：session 退回
/// DETACHED（`ATTACH_FAILED:OSError`），by_id 的未確認關閉阻擋成立。回傳 rig 與開埠嘗試／TX 基準。
fn wp510_review7_unconfirmed_close_rig() -> (ServiceRig, usize, usize) {
    let r = wp510_b_placeholder_rig();
    let dev = r.device(WP510_REAL_G).clone();
    dev.fail_configure(PalError::io(5, "[Errno 5] Input/output error"));
    dev.fail_close(PalError::io(5, "[Errno 5] Input/output error"));
    let attached = r.call(
        "device.attach",
        &format!(r#"{{"selector":"{WP510_PLACEHOLDER_SID}"}}"#),
    );
    assert_eq!(attached.get("ok"), Some(&PyValue::Bool(true)), "{}", j(&attached));
    wait_until("正式 session 套用設定失敗後退回 DETACHED", || {
        r.session("t:COM3").is_some_and(|session| {
            s(&session, "state") == "DETACHED"
                && session.get("last_error").and_then(PyValue::as_str) == Some("ATTACH_FAILED:OSError")
        })
    });
    // 交接 worker 收尾後作業仍因 closed:false 保留（僅等待其收尾，保留時於期限後回 false）。
    let _ = r
        .mgr
        .wait_attach_idle_for_test(WP510_PLACEHOLDER_BY_ID, Duration::from_millis(300));
    assert!(dev.is_open(), "closed:false 表示 handle 仍開啟");
    assert!(
        r.mgr.has_inflight_attach_for_test(WP510_PLACEHOLDER_BY_ID),
        "未確認關閉的 handle 必須保留阻擋"
    );
    let attempts = dev.open_attempts();
    let tx = dev.tx_len();
    (r, attempts, tx)
}

/// 呼叫入口後短暫等待（讓任何非同步開埠有機會發生），再斷言未再嘗試開埠、未送 TX。
fn wp510_review7_assert_no_reopen(r: &ServiceRig, attempts: usize, tx: usize, entry: &str) {
    std::thread::sleep(Duration::from_millis(200));
    let dev = r.device(WP510_REAL_G);
    assert_eq!(
        dev.open_attempts(),
        attempts,
        "{entry}：未確認關閉的 by_id 不得再嘗試開埠"
    );
    assert_eq!(dev.tx_len(), tx, "{entry}：未確認關閉的 by_id 不得送出 TX");
    assert!(
        r.mgr.has_inflight_attach_for_test(WP510_PLACEHOLDER_BY_ID),
        "{entry}：阻擋須持續"
    );
}

/// 入口一：`device.attach`（`Service::rpc("device.attach")` → `route_with_selector` → actor `attach_request`）。
/// Python `attach_device` 先檢查 `_draining_by_ids`，回 `SESSION_RECOVERING` 並附 session 的 drain 欄位
/// （`origin/main:sw_core/session_manager.py:3410-3417`）。
#[test]
fn wp510_review7_rpc_device_attach_after_unconfirmed_close_returns_session_recovering() {
    let (r, attempts, tx) = wp510_review7_unconfirmed_close_rig();
    let before = r.session("t:COM3").expect("正式 session");

    let response = r.call("device.attach", r#"{"selector":"t:COM3"}"#);

    let expected = sw_proto::pydict! {
        "ok" => false,
        "error_code" => "SESSION_RECOVERING",
        "drain_stalled" => false,
        "drain_timeout_epoch" => PyValue::Null,
        "session" => before,
        "version" => r.version.clone(),
    };
    assert_eq!(j(&response), j(&expected), "device.attach 回應 bytes");
    wp510_review7_assert_no_reopen(&r, attempts, tx, "device.attach");
}

/// 入口二：`session.attach`（`Service::rpc("session.attach")` → actor `attach_request`）。Python `attach_session`
/// 對無 bridge、裝置在線的 session 設 ATTACHING、清 last_error 後 `_spawn_attach`，後者因 by_id 在
/// `_draining_by_ids` 早退（`origin/main:sw_core/session_manager.py:3933-3944`、`:3999-4000`、`:4113-4114`）。
#[test]
fn wp510_review7_rpc_session_attach_after_unconfirmed_close_does_not_reopen() {
    let (r, attempts, tx) = wp510_review7_unconfirmed_close_rig();

    let response = r.call("session.attach", r#"{"selector":"t:COM3"}"#);

    let after = r.session("t:COM3").expect("正式 session");
    let expected = sw_proto::pydict! {
        "ok" => true,
        "session" => after.clone(),
        "version" => r.version.clone(),
    };
    assert_eq!(j(&response), j(&expected), "session.attach 回應 bytes");
    assert_eq!(s(&after, "state"), "ATTACHING");
    assert_eq!(after.get("last_error"), Some(&PyValue::Null));
    wp510_review7_assert_no_reopen(&r, attempts, tx, "session.attach");
    assert_eq!(
        r.session("t:COM3").map(|session| s(&session, "state").to_string()).as_deref(),
        Some("ATTACHING"),
        "被阻擋的 attach 停在 ATTACHING（Python `_spawn_attach` 早退不改狀態）"
    );
}

/// 入口三：`session.recover`（`Service::rpc("session.recover")` → actor `recover_request` 的 Reattach 計畫）。
/// Python `recover_session` 對無 bridge、裝置在線者設 ATTACHING 後 `_spawn_attach` 早退，回 REATTACH
/// （`origin/main:sw_core/session_manager.py:7970-7976`、`:4113-4114`）。
#[test]
fn wp510_review7_rpc_session_recover_after_unconfirmed_close_does_not_reopen() {
    let (r, attempts, tx) = wp510_review7_unconfirmed_close_rig();

    let response = r.call("session.recover", r#"{"selector":"t:COM3"}"#);

    let after = r.session("t:COM3").expect("正式 session");
    let expected = sw_proto::pydict! {
        "ok" => true,
        "recovering" => false,
        "action" => "REATTACH",
        "session" => after.clone(),
        "version" => r.version.clone(),
    };
    assert_eq!(j(&response), j(&expected), "session.recover 回應 bytes");
    assert_eq!(s(&after, "state"), "ATTACHING");
    wp510_review7_assert_no_reopen(&r, attempts, tx, "session.recover");
}

/// 入口四：`session.clear`（manager `clear` → actor Clear → `Effect::Reattach` → `spawn_attach_with`）。Python
/// `clear_session` 對無 bridge 者設 ATTACHING 後 `_spawn_attach` 早退（`origin/main:sw_core/session_manager.py:3058-3082`、
/// `:4113-4114`）。
#[test]
fn wp510_review7_rpc_session_clear_after_unconfirmed_close_does_not_reopen() {
    let (r, attempts, tx) = wp510_review7_unconfirmed_close_rig();

    let response = r.call("session.clear", r#"{"selector":"t:COM3"}"#);

    assert_eq!(response.get("ok"), Some(&PyValue::Bool(true)), "{}", j(&response));
    assert_eq!(s(&sub(&response, "session"), "state"), "ATTACHING", "{}", j(&response));
    wp510_review7_assert_no_reopen(&r, attempts, tx, "session.clear");
}

/// 入口五：hotplug／watcher（`update_devices` 拔除再插回 → `spawn_attach`）。Python `update_devices` 對插回的
/// by_id 呼叫 `_spawn_attach`，因 `_draining_by_ids` 早退（`origin/main:sw_core/session_manager.py:4081-4082`、
/// `:4113-4114`）。
#[test]
fn wp510_review7_hotplug_after_unconfirmed_close_does_not_reopen() {
    let (r, attempts, tx) = wp510_review7_unconfirmed_close_rig();
    let full: Vec<(String, DeviceInfo)> = r
        .mgr
        .list_devices()
        .into_iter()
        .map(|row| {
            let by_id = s(&row, "by_id").to_string();
            let real_path = s(&row, "real_path").to_string();
            (by_id.clone(), DeviceInfo { by_id, real_path })
        })
        .collect();
    let without: Vec<_> = full
        .iter()
        .filter(|(by_id, _)| by_id != WP510_PLACEHOLDER_BY_ID)
        .cloned()
        .collect();

    r.mgr.update_devices(&without);
    r.mgr.update_devices(&full);

    wp510_review7_assert_no_reopen(&r, attempts, tx, "hotplug");
    assert_eq!(
        r.session("t:COM3").map(|session| s(&session, "state").to_string()).as_deref(),
        Some("DETACHED")
    );
}

/// 入口六：`session.bind` 改綁到未確認關閉的 by_id（manager `bind` → actor Bind → `DeviceOnline`）。情境：佔位交接
/// 的 dynamic 偵測 PROBE 提交前裝置拔除，作業取消後關閉回條 `closed:false`，by_id 無任何 session 但仍被阻擋；裝置插回後
/// 把 COM1 改綁過去。Python 的作業關閉失敗即留在 `_active_attach_ops`（`origin/main:sw_core/session_manager.py:4320-4332`），
/// `bind_session` 收尾設 ATTACHING 後 `_spawn_attach`，`_preopen_auto_attach_port` 見在途作業即不開埠
/// （`:2955-2988`、`:4171-4176`）。
#[test]
fn wp510_review7_rpc_bind_to_unconfirmed_close_by_id_does_not_reopen() {
    let r = wp510_b_placeholder_rig();
    let dev = r.device(WP510_REAL_G).clone();
    let full: Vec<(String, DeviceInfo)> = r
        .mgr
        .list_devices()
        .into_iter()
        .map(|row| {
            let by_id = s(&row, "by_id").to_string();
            let real_path = s(&row, "real_path").to_string();
            (by_id.clone(), DeviceInfo { by_id, real_path })
        })
        .collect();
    let without: Vec<_> = full
        .iter()
        .filter(|(by_id, _)| by_id != WP510_PLACEHOLDER_BY_ID)
        .cloned()
        .collect();
    let weak = Arc::downgrade(&r.mgr);
    let fired = Arc::new(std::sync::atomic::AtomicBool::new(false));
    let hook_fired = fired.clone();
    let hook_dev = dev.clone();
    r.mgr.set_probe_pre_submit_hook_for_test(Box::new(move || {
        if hook_fired.swap(true, Ordering::SeqCst) {
            return;
        }
        hook_dev.fail_close(PalError::io(5, "[Errno 5] Input/output error"));
        if let Some(manager) = weak.upgrade() {
            manager.update_devices(&without);
        }
    }));

    let attached = r.call(
        "device.attach",
        &format!(r#"{{"selector":"{WP510_PLACEHOLDER_SID}"}}"#),
    );
    assert_eq!(attached.get("ok"), Some(&PyValue::Bool(true)), "{}", j(&attached));
    wait_until("交接 PROBE 觸發拔除", || fired.load(Ordering::SeqCst));
    let _ = r
        .mgr
        .wait_attach_idle_for_test(WP510_PLACEHOLDER_BY_ID, Duration::from_millis(500));
    assert!(dev.is_open(), "closed:false 表示 handle 仍開啟");
    assert!(
        r.mgr.has_inflight_attach_for_test(WP510_PLACEHOLDER_BY_ID),
        "未確認關閉的 handle 必須保留阻擋"
    );
    assert!(r.mgr.resolve("t:COM3").is_none(), "偵測被取消，不得建立正式 session");
    r.mgr.update_devices(&full);
    let attempts = dev.open_attempts();
    let tx = dev.tx_len();

    let response = r.call(
        "session.bind",
        &format!(r#"{{"selector":"COM1","device_by_id":"{WP510_PLACEHOLDER_BY_ID}"}}"#),
    );

    assert_eq!(response.get("ok"), Some(&PyValue::Bool(true)), "{}", j(&response));
    let session = sub(&response, "session");
    assert_eq!(s(&session, "device_by_id"), WP510_PLACEHOLDER_BY_ID, "{}", j(&response));
    assert_eq!(s(&session, "state"), "ATTACHING", "{}", j(&response));
    assert_eq!(session.get("last_error"), Some(&PyValue::Null), "{}", j(&response));
    wp510_review7_assert_no_reopen(&r, attempts, tx, "session.bind");
    assert_eq!(
        r.session("COM1").map(|session| s(&session, "state").to_string()).as_deref(),
        Some("ATTACHING")
    );
}

/// 邊界對照：關閉回條 `closed:true` 時不登記阻擋，之後的 `device.attach` 照常重新開埠（Python 安全回條解除
/// `_draining_by_ids`，`origin/main:sw_core/session_manager.py:1346-1368`）。
#[test]
fn wp510_review7_rpc_confirmed_close_allows_reopen() {
    let r = wp510_b_placeholder_rig();
    let dev = r.device(WP510_REAL_G).clone();
    dev.fail_configure(PalError::io(5, "[Errno 5] Input/output error"));
    let attached = r.call(
        "device.attach",
        &format!(r#"{{"selector":"{WP510_PLACEHOLDER_SID}"}}"#),
    );
    assert_eq!(attached.get("ok"), Some(&PyValue::Bool(true)), "{}", j(&attached));
    wait_until("正式 session 套用設定失敗後退回 DETACHED", || {
        r.session("t:COM3")
            .is_some_and(|session| s(&session, "state") == "DETACHED")
    });
    assert!(
        r.mgr
            .wait_attach_idle_for_test(WP510_PLACEHOLDER_BY_ID, Duration::from_secs(5)),
        "確認關閉後作業應收尾"
    );
    assert!(!dev.is_open(), "closed:true 後 handle 已關閉");
    let attempts = dev.open_attempts();

    let response = r.call("device.attach", r#"{"selector":"t:COM3"}"#);

    assert_ne!(
        response.get("error_code").and_then(PyValue::as_str),
        Some("SESSION_RECOVERING"),
        "{}",
        j(&response)
    );
    wait_until("確認關閉後可再次開埠", || dev.open_attempts() > attempts);
}

// ---------------------------------------------------------------------------
// OD-81／DF-27 第 6 項：登入後命令逾時的重探 RPC 端到端（docs/rust-refactor/reviews/rulings/s2.md「OD-81」）。
//
// Python 基線：`_attach_by_id` 的 ensure_ready 回 `POST_LOGIN_CMD_TIMEOUT`（`origin/main:sw_core/login_fsm.py:174`）→ 關
// bridge、DETACHED；`_is_reprobe_prompt_error`（`origin/main:sw_core/session_manager.py:2112-2136`）不含此碼 → 永不重探；
// 期間 agent `command.submit` 經 `_resolve_session_id` 回 `{"ok":false,"error_code":"SESSION_NOT_READY","session":…}`
// （`origin/main:sw_core/service.py:793`）。DF-27：Rust 沿用既有退避重探並回 READY（Python 依 OD-75 不改）。
//
// 呼叫路徑：熱插 `SessionManager::update_devices` → DeviceAttached → `Effect::OpenBridge` → 開橋成功 →
// `Effect::StartLogin` → `ManagerExec::start_login`（登入執行緒）→ `login_fsm::run_login_plan`／`ensure_ready` →
// `finalize_ready`（post_login_cmd 逾時 → `POST_LOGIN_CMD_TIMEOUT`）→ `ControlEvent::LoginFinished` →
// `SessionCore::login_finished`（`Failed` → `begin_stop(StopKind::LoginFailed)`）→ drain／stop receipt → DETACHED →
// actor 依 FakeClock 每秒 Tick → `reprobe_tick` → `reprobe::prepare`（`is_reprobe_prompt_error` 納入此碼）→
// `ReprobeAction::Attach` → `Effect::OpenBridge`／`StartLogin` → READY。拒絕：`Service::rpc("command.submit")` →
// session gate（非 READY）→ 同步回應、不進 actor queue、UART／WAL／registry／state.json 皆不變。
// ---------------------------------------------------------------------------

/// 需帳密登入（帳密來自 env_file）的 shell profile：prompt 刻意與 `install_shell_replies` 的 `root@t` 不同，使替身
/// 預設的一次性 `\n` → `root@t:/# ` 應答不被當成 prompt，登入必走 login／password 分支（`maybe_login` 成功後清 RX
/// 視窗），post_login_cmd 的 prompt 等待因此只看 `sh` 之後的 RX。
const DF27_YAML: &str = "profiles:\n  c:\n    platform: shell\n    prompt_regex: '(?m)^df27@t:.*# '\n    login_regex: '(?m)login: ?$'\n    password_regex: '(?m)Password: ?$'\n    user_env: SW_DF27_U\n    pass_env: SW_DF27_P\n    env_file: /fake/df27.env\n    post_login_cmd: sh\n    timeout_s: 2.0\ntargets:\n  - {com: COM0, profile: c, device_by_id: /s/by-id/usb-A}\n";
const DF27_ENV_FILE: &str = "/fake/df27.env";
const DF27_PROMPT: &[u8] = b"\r\ndf27@t:/# ";
const DF27_CODE: &str = "POST_LOGIN_CMD_TIMEOUT";

fn df27_field(r: &ServiceRig, key: &str) -> PyValue {
    r.session("COM0")
        .and_then(|session| session.get(key).cloned())
        .unwrap_or(PyValue::Null)
}

/// 逐秒推進 FakeClock（一次跳多秒只合併成一個 Tick），每步等背景穩定後檢查；回傳第一個使 `cond` 成立的 session
/// 快照，`max_s` 秒內不成立則 panic。注意 ServiceRig 的 FakeClock 為非 threaded：登入執行緒等待 prompt 時會自行以
/// 10 ms 切片推進，故登入在途期間的時間欄位不固定，斷言一律針對回傳的同一份快照。
fn df27_advance_until(
    r: &ServiceRig,
    max_s: u64,
    what: &str,
    mut cond: impl FnMut(&PyDict) -> bool,
) -> PyDict {
    for _ in 0..=max_s {
        let session = r.session("COM0").expect("COM0 session");
        if cond(&session) {
            return session;
        }
        r.clock.advance(Duration::from_secs(1));
        std::thread::sleep(Duration::from_millis(25));
        r.settle();
    }
    panic!(
        "{max_s} s 內未達成：{what}；session={}",
        j(&r.session("COM0").expect("COM0 session"))
    );
}

/// agent `command.submit` 在 DETACHED 時的拒絕：回應以 RPC_WIRE 逐位元組等於 Python `service.py:793` 形狀
/// （session 取自同一時刻的 `session.get_state`），且 UART／WAL／registry／queue／state.json 全不變。只用於無登入
/// 執行緒在途的 DETACHED 窗口（時鐘不自行推進，session 投影穩定）。
fn df27_assert_rejected_zero_tx(r: &ServiceRig, marker: &str) {
    r.settle();
    let session = r.session("COM0").expect("COM0 session");
    let expected = sw_proto::pydict! {
        "ok" => false,
        "error_code" => "SESSION_NOT_READY",
        "session" => PyValue::Dict(session),
        "version" => r.version.clone(),
    };
    assert_wp52_rpc_rejected_without_side_effects(
        r,
        &format!(
            r#"{{"selector":"COM0","cmd":"echo {marker}","source":"agent:df27","timeout_s":5}}"#
        ),
        &j(&expected),
    );
}

/// 對照 `tests/contract_blackbox/test_p7_latency_noise.py::test_df27_rust_post_login_timeout_reprobes_ready`（真實
/// daemon 黑箱）與 `crates/sw-core/tests/session_core_reprobe_p7.rs::df27_failed_login_drains_then_retries_to_ready`
/// （核心）：真 `SessionManager`＋`Service` 上，熱插 attach 登入的 post_login_cmd 逾時 → DETACHED＋`POST_LOGIN_CMD_TIMEOUT`；
/// 兩個 DETACHED 窗口（首次失敗後、退避中）的 agent 提交以 Python 形狀逐位元組拒絕且零副作用，重探開橋在途（ATTACHING）
/// 的提交亦以 SESSION_NOT_READY 拒絕；第一次重探（attempt 1）仍逾時 → 回 DETACHED 並排定退避；修好 DUT 後第二次重探回
/// READY 並重置進度，之後 agent 命令真正寫到 UART；三筆被拒命令全程未出現在 UART。
#[test]
fn df27_rpc_post_login_timeout_rejects_zero_tx_then_reprobes_ready() {
    use std::sync::atomic::AtomicBool;
    use sw_pal::traits::FsOps;

    let r = ServiceRig::build(RigSpec {
        yaml: DF27_YAML.to_string(),
        devices: vec![(BY_ID_A.into(), REAL_A.into())],
        explicit_target_count: 1,
        allowlist: None,
        wait_ready: vec![],
    });
    // 啟動時 env_file 不存在：帳密未解析 → ATTACHED＋CREDENTIALS_UNRESOLVED（零 TX、不重探）。
    r.wait_state("COM0", "ATTACHED");
    assert_eq!(
        df27_field(&r, "last_error"),
        PyValue::from("CREDENTIALS_UNRESOLVED")
    );

    let dev = r.device(REAL_A);
    let sh_ok = Arc::new(AtomicBool::new(false));
    dev.on_tx_match("^$", Box::new(|_line: &[u8]| b"\r\nlogin: ".to_vec()));
    dev.on_tx_match(
        "^root$",
        Box::new(|_line: &[u8]| b"\r\nPassword: ".to_vec()),
    );
    dev.on_tx_match("^pw-df27$", Box::new(|_line: &[u8]| DF27_PROMPT.to_vec()));
    let flag = sh_ok.clone();
    dev.on_tx_match(
        "^sh$",
        Box::new(move |_line: &[u8]| {
            if flag.load(Ordering::SeqCst) {
                DF27_PROMPT.to_vec()
            } else {
                // 進了子 shell 卻沒有回 prompt：post_login_cmd 的 prompt 等待逾時。
                b"\r\n[subshell waiting]\r\n".to_vec()
            }
        }),
    );
    dev.on_tx_match(
        r"__READY__[0-9a-f]+",
        Box::new(|line: &[u8]| {
            let text = String::from_utf8_lossy(line).into_owned();
            let token = text
                .split("__READY__")
                .nth(1)
                .unwrap_or("")
                .trim()
                .to_string();
            let mut out = format!("__READY__{token}").into_bytes();
            out.extend_from_slice(DF27_PROMPT);
            out
        }),
    );
    dev.on_tx_match(
        "^echo df27-after$",
        Box::new(|_line: &[u8]| {
            let mut out = b"\r\ndf27-after".to_vec();
            out.extend_from_slice(DF27_PROMPT);
            out
        }),
    );

    // 補齊帳密後以熱插觸發全新 attach（`_attach_by_id`；不經手動 attach／recover）。
    r.fs.atomic_write(
        std::path::Path::new(DF27_ENV_FILE),
        b"SW_DF27_U=root\nSW_DF27_P=pw-df27\n",
        None,
    )
    .expect("寫入 env_file");
    r.mgr.update_devices(&[]);
    r.wait_state("COM0", "DETACHED");
    r.settle();
    let tx_mark = dev.tx_len();
    r.mgr.update_devices(&[(
        BY_ID_A.to_string(),
        DeviceInfo {
            by_id: BY_ID_A.to_string(),
            real_path: REAL_A.to_string(),
        },
    )]);

    // 1. attach 登入：login／password 成功、送出 post_login_cmd，prompt 未回 → DETACHED＋POST_LOGIN_CMD_TIMEOUT。
    let failed = df27_advance_until(&r, 10, "attach 登入逾時進 DETACHED", |session| {
        s(session, "state") == "DETACHED"
            && session.get("last_error") == Some(&PyValue::from(DF27_CODE))
    });
    let sent = String::from_utf8_lossy(&dev.tx_since(tx_mark)).into_owned();
    assert!(
        sent.contains("root\n") && sent.contains("sh\n"),
        "登入與 post_login_cmd 應已送出：{sent:?}"
    );
    assert!(
        !sent.contains("__READY__"),
        "post_login_cmd 逾時不得送 nonce probe：{sent:?}"
    );
    assert_eq!(
        failed.get("reprobe_attempts"),
        Some(&PyValue::from(0)),
        "attach 失敗本身不計重探 attempt：{}",
        j(&failed)
    );
    df27_assert_rejected_zero_tx(&r, "df27-reject-detached");

    // 2. 依 RX idle 起第一次重探：開橋在途（ATTACHING）只計一次 attempt、清 last_error。此窗口登入執行緒在途、時鐘
    //    自行推進（見 `df27_advance_until`），提交只斷言拒絕碼與未產生 cmd_id，零 TX 由第 6 步全程 UART 檢查證明；
    //    逐位元組回應比對在下方兩個 DETACHED 窗口。
    let reopening = df27_advance_until(
        &r,
        10,
        "DF-27：DETACHED＋POST_LOGIN_CMD_TIMEOUT 應自動重探開橋",
        |session| s(session, "state") == "ATTACHING",
    );
    assert_eq!(
        reopening.get("reprobe_attempts"),
        Some(&PyValue::from(1)),
        "真正開橋一次記一次 attempt：{}",
        j(&reopening)
    );
    assert_eq!(
        reopening.get("last_error"),
        Some(&PyValue::Null),
        "重新 attach 時清 last_error：{}",
        j(&reopening)
    );
    let in_flight = r.call(
        "command.submit",
        r#"{"selector":"COM0","cmd":"echo df27-reject-attaching","source":"agent:df27","timeout_s":5}"#,
    );
    assert_eq!(
        s(&in_flight, "error_code"),
        "SESSION_NOT_READY",
        "{}",
        j(&in_flight)
    );
    assert_eq!(
        keys(&in_flight),
        ["ok", "error_code", "session", "version"],
        "{}",
        j(&in_flight)
    );
    assert_eq!(
        s(&sub(&in_flight, "session"), "state"),
        "ATTACHING",
        "{}",
        j(&in_flight)
    );

    // 3. 第一次重探仍逾時 → 回 DETACHED、attempts 不變且已排定退避。
    let backoff = df27_advance_until(&r, 10, "第一次重探逾時回 DETACHED", |session| {
        s(session, "state") == "DETACHED"
            && session.get("last_error") == Some(&PyValue::from(DF27_CODE))
    });
    assert_eq!(
        backoff.get("reprobe_attempts"),
        Some(&PyValue::from(1)),
        "drain 收尾不計 attempt：{}",
        j(&backoff)
    );
    assert_ne!(
        backoff.get("next_reprobe_at"),
        Some(&PyValue::Null),
        "失敗後排定退避：{}",
        j(&backoff)
    );
    assert_eq!(
        backoff.get("reprobe_exhausted"),
        Some(&PyValue::Bool(false))
    );
    df27_assert_rejected_zero_tx(&r, "df27-reject-backoff");

    // 4. DUT 恢復：退避到期後第二次重探 → READY，錯誤與重探進度重置；不呼叫 attach／recover。
    sh_ok.store(true, Ordering::SeqCst);
    let ready = df27_advance_until(&r, 20, "DF-27：第二次重探回 READY", |session| {
        s(session, "state") == "READY"
    });
    assert_eq!(
        ready.get("last_error"),
        Some(&PyValue::Null),
        "{}",
        j(&ready)
    );
    assert_eq!(
        ready.get("reprobe_attempts"),
        Some(&PyValue::from(0)),
        "READY 後重置 attempts：{}",
        j(&ready)
    );
    assert_eq!(
        ready.get("next_reprobe_at"),
        Some(&PyValue::Null),
        "READY 後清 backoff：{}",
        j(&ready)
    );
    assert_eq!(ready.get("reprobe_exhausted"), Some(&PyValue::Bool(false)));

    // 5. READY 後 agent 命令經 RPC 真正寫到 UART 並完成。
    let after_mark = dev.tx_len();
    let response = r.call(
        "command.submit",
        r#"{"selector":"COM0","cmd":"echo df27-after","source":"agent:df27","timeout_s":5}"#,
    );
    assert_eq!(
        response.get("ok"),
        Some(&PyValue::Bool(true)),
        "{}",
        j(&response)
    );
    let cmd_id = s(&response, "cmd_id").to_string();
    let mut record = PyDict::default();
    wait_until("READY 後命令完成", || {
        r.clock.advance(Duration::from_millis(100));
        record = sub(
            &r.call("command.get", &format!(r#"{{"cmd_id":"{cmd_id}"}}"#)),
            "command",
        );
        !matches!(record.get("done_at"), None | Some(PyValue::Null))
    });
    assert_eq!(s(&record, "status"), "done", "{}", j(&record));
    assert_eq!(
        dev.tx_since(after_mark),
        b"echo df27-after\n".to_vec(),
        "READY 後命令原樣寫到 UART"
    );

    // 6. 被拒的三筆提交全程未進 UART（無延後送出）。
    let all_tx = String::from_utf8_lossy(&dev.tx_since(0)).into_owned();
    for marker in [
        "df27-reject-detached",
        "df27-reject-attaching",
        "df27-reject-backoff",
    ] {
        assert!(
            !all_tx.contains(marker),
            "被拒命令 {marker} 不得寫到 UART：{all_tx:?}"
        );
    }
}

// ---------------------------------------------------------------------------
// WP7-3：console unlock 的 reboot 前清 RX（G10，Low）端到端 RPC 驗證
// 呼叫路徑：`Service::rpc(command.submit)` → `SubmitReq` → `SessionCore::submit`
// → queue → `start_next` → `OpSpec::Command` → `OpRunner::run_command`
// → `run_reboot`（清空 RX：若 `console_unlock_env` 已宣告，丟棄 `ctx.rx` 舊資料）
// → UART 送出 reboot → prompt 逾時 → `OpOutcome::RebootExpected`
// → `SessionCore::finish_command` → `reboot_recovery_tick` → `EnsureReady`
// → `ensure_console_unlocked`：此時舊 prompt 已清，送出 unlock secret 解鎖。
// ---------------------------------------------------------------------------

#[test]
fn wp73_rpc_reboot_clears_rx_for_console_unlock_service_rig() {
    const PROFILE: &str = "profiles:\n  cu:\n    platform: shell\n    prompt_regex: '(?m)^root@t:.*# '\n    console_unlock_env: SW_T_UNLOCK\n    console_unlock_idle_s: 0.05\n    timeout_s: 2.0\ntargets:\n  - {com: COM0, profile: cu, device_by_id: /s/by-id/usb-A}\n";
    let env = Arc::new(FakeEnv::default());
    env.vars
        .lock()
        .unwrap()
        .insert("SW_T_UNLOCK".to_string(), "dummy-unlock".to_string());
    let spec = RigSpec {
        yaml: PROFILE.to_string(),
        devices: vec![(BY_ID_A.into(), REAL_A.into())],
        explicit_target_count: 1,
        allowlist: None,
        wait_ready: vec!["COM0".into()],
    };
    let r = ServiceRig::build_with_env(spec, env);
    let dev = r.device(REAL_A);
    r.settle();

    // 初始 attach 已進入 READY，此時 dev 已有 initial TX，RX 亦有舊的 root@t:/#。
    // 模擬 DUT 重開機：清除預設回覆，只有在收到 "dummy-unlock" 時才回覆 prompt。
    dev.clear_replies();
    dev.on_tx_match(
        r"dummy-unlock",
        Box::new(|_| b"\r\nroot@t:/# ".to_vec()),
    );
    dev.on_tx_match(
        r"__READY__[0-9a-f]+",
        Box::new(|line: &[u8]| {
            let text = String::from_utf8_lossy(line).into_owned();
            let token = text
                .split("__READY__")
                .nth(1)
                .unwrap_or("")
                .trim()
                .to_string();
            format!("__READY__{token}\r\nroot@t:/# ").into_bytes()
        }),
    );

    let tx_mark = dev.tx_len();
    let response = r.call(
        "command.submit",
        r#"{"selector":"COM0","cmd":"reboot","timeout_s":1}"#,
    );
    assert_eq!(
        response.get("ok"),
        Some(&PyValue::Bool(true)),
        "{}",
        j(&response)
    );
    let cmd_id = s(&response, "cmd_id").to_string();
    let mut record = PyDict::default();
    wait_until("reboot 命令結案", || {
        r.clock.advance(Duration::from_millis(100));
        record = sub(
            &r.call("command.get", &format!(r#"{{"cmd_id":"{cmd_id}"}}"#)),
            "command",
        );
        !matches!(record.get("done_at"), None | Some(PyValue::Null))
    });
    assert_eq!(s(&record, "status"), "done", "{}", j(&record));
    assert_eq!(
        record.get("partial"),
        Some(&PyValue::Bool(true)),
        "{}",
        j(&record)
    );

    // 推進時鐘等待 recovery 完成並回到 READY
    wait_until("session 恢復 READY", || {
        r.clock.advance(Duration::from_millis(200));
        r.session("COM0")
            .and_then(|session| session.get("state").cloned())
            == Some(PyValue::from("READY"))
    });

    let reboot_tx = String::from_utf8_lossy(&dev.tx_since(tx_mark)).into_owned();
    assert!(
        reboot_tx.contains("dummy-unlock"),
        "reboot 後的 unlock 流程必須送出 unlock 密鑰，不得誤用舊 root prompt 作為證據；實際 TX: {reboot_tx:?}"
    );
}


// ---------------------------------------------------------------------------
// S2-PAR-wp510r R3（設計第 5 版 §1.6 DR-4、§3.1.3 retired_drains）：ServiceRig 端到端。
// ---------------------------------------------------------------------------

/// DR-4(b)(c) 端到端：RELEASED session 的 `device.attach` 開埠成功但 unreleased 落盤 ENOSPC → actor 還原 RELEASED 並
/// 退休 bridge（S2 退休來源）。退休 bridge 的關閉停在暫停點（回條前）時：(c) 再次 `device.attach` 回 SESSION_RECOVERING
/// （五鍵）且不開埠；(b) shutdown 不得報已排空。放行關閉後回條 handle_closed:true，shutdown 收斂。
/// 路徑：`Service::rpc("device.attach")` → `SessionManager::route_with_selector` → actor `attach_request`
/// （retired_drains 未收斂視同在途）；shutdown：`SessionManager::shutdown` → actor `begin_stop(Shutdown)`（retired_drains
/// 未收斂建立待決停止）→ 回收者轉送的 `WorkerStopped` → `retired_drain_receipt`。
#[test]
fn wp510r_r3_rpc_dr4_retired_drain_blocks_attach_and_shutdown_until_receipt() {
    let r = ServiceRig::ready_shell();
    let dev = r.device(support::REAL_A).clone();
    let released = r.call("device.release", r#"{"selector":"COM0"}"#);
    assert_eq!(released.get("ok"), Some(&PyValue::Bool(true)), "{}", j(&released));
    r.wait_state("COM0", "RELEASED");
    let opens_before = dev.open_attempts();

    dev.pause_next_close();
    r.fs.fail_next_atomic_write(PalError::io(28, "[Errno 28] No space left on device"));
    let error = r.call_err("device.attach", r#"{"selector":"COM0","force":true}"#);
    assert!(error.message.contains("No space left"), "{}", error.message);
    assert!(dev.wait_paused(Duration::from_secs(5)), "前置：退休 bridge 的關閉停在暫停點");
    assert_eq!(dev.open_attempts(), opens_before + 1, "前置：device.attach 開埠一次");
    let session = r.session("COM0").expect("COM0 session");
    assert_eq!(s(&session, "state"), "RELEASED", "{}", j(&session));
    let sid = r.mgr.resolve("COM0").expect("COM0 sid");
    assert_eq!(r.mgr.retired_drains_for_test(&sid).len(), 1, "retired_drains 登記退休 epoch");

    let again = r.call("device.attach", r#"{"selector":"COM0","force":true}"#);
    assert_eq!(
        j(&again)
            .split(",\"session\":")
            .next()
            .map(str::to_string),
        Some(format!(
            "{{\"ok\":false,\"error_code\":\"SESSION_RECOVERING\",\"drain_stalled\":false,\"drain_timeout_epoch\":null"
        )),
        "{}",
        j(&again)
    );
    assert_eq!(s(&sub(&again, "session"), "state"), "RELEASED");
    assert_eq!(dev.open_attempts(), opens_before + 1, "retired drain 回條前不得再開埠");

    let drained = r.mgr.shutdown(Duration::from_millis(400));
    assert!(
        drained.iter().any(|(name, ok)| name == &sid && !ok),
        "退休 bridge 回條前 shutdown 不得報已排空：{drained:?}"
    );
    dev.release_pause();
    wait_until("退休 bridge 回條結算", || r.mgr.retired_drains_for_test(&sid).is_empty());
    assert!(!dev.is_open(), "退休 bridge 的 handle 已關閉");
    assert_eq!(dev.open_attempts(), opens_before + 1, "全程不另開");
}

// ---------------------------------------------------------------------------
// S2-PAR-wp510r R3 第 2 輪（審查 P8-566d90b-3、-4）：ServiceRig 端到端。
// ---------------------------------------------------------------------------

const WP510R_FREE_BY_ID: &str = "/s/by-id/usb-wp510r-free";

/// DR-4 前置（ServiceRig）：RELEASED 的 COM0 `device.attach` 開埠成功但 unreleased 落盤 ENOSPC，退休 bridge 的關閉停在
/// 暫停點。回傳 `(rig, device, sid, 開埠次數)`。
fn wp510r_rpc_retired_bridge_paused() -> (Arc<ServiceRig>, FakeDevice, String, usize) {
    let r = Arc::new(ServiceRig::ready_shell());
    let dev = r.device(support::REAL_A).clone();
    let released = r.call("device.release", r#"{"selector":"COM0"}"#);
    assert_eq!(released.get("ok"), Some(&PyValue::Bool(true)), "{}", j(&released));
    r.wait_state("COM0", "RELEASED");
    dev.pause_next_close();
    r.fs.fail_next_atomic_write(PalError::io(28, "[Errno 28] No space left on device"));
    let error = r.call_err("device.attach", r#"{"selector":"COM0","force":true}"#);
    assert!(error.message.contains("No space left"), "{}", error.message);
    assert!(dev.wait_paused(Duration::from_secs(5)), "前置：退休 bridge 的關閉停在暫停點");
    let sid = r.mgr.resolve("COM0").expect("COM0 sid");
    assert_eq!(r.mgr.retired_drains_for_test(&sid).len(), 1, "前置：retired_drains 登記退休 epoch");
    let opens = dev.open_attempts();
    (r, dev, sid, opens)
}

/// P8-566d90b-3 端到端：退休 bridge 回條前 `session.bind`：回條前不回覆，成功回條後改綁並回 ok（DETACHED／
/// DEVICE_NOT_FOUND，新 by_id 不在線）。路徑：`Service::rpc("session.bind")` → `SessionManager::route_with_selector`
/// → `SessionManager::bind` → actor `bind_request`（`bind_wait_retired_drains` 掛起）→ 回收者轉送的 `WorkerStopped`
/// → `retired_drain_receipt` → `resume_bind_waits` → `bind_request` → `apply_bind`。
#[test]
fn wp510r_r3_rpc_bind_waits_retired_receipt_then_rebinds() {
    let (r, dev, sid, opens) = wp510r_rpc_retired_bridge_paused();
    let (reply_tx, reply_rx) = crossbeam_channel::bounded(1);
    let bind_rig = Arc::clone(&r);
    let bind = std::thread::spawn(move || {
        let _ = reply_tx.send(bind_rig.call(
            "session.bind",
            &format!(r#"{{"selector":"COM0","device_by_id":"{WP510R_FREE_BY_ID}"}}"#),
        ));
    });
    let early = reply_rx.recv_timeout(Duration::from_millis(500)).ok();
    dev.release_pause();
    let reply = reply_rx.recv_timeout(Duration::from_secs(10)).ok().or(early.clone());
    bind.join().expect("bind thread 不應 panic");
    assert!(early.is_none(), "退休 bridge 回條前 bind 不得回覆：{:?}", early.as_ref().map(j));
    let reply = reply.expect("成功回條後 bind 應回覆");
    assert_eq!(reply.get("ok"), Some(&PyValue::Bool(true)), "{}", j(&reply));
    let session = r.session("COM0").expect("COM0 session");
    assert_eq!(s(&session, "state"), "DETACHED", "{}", j(&session));
    assert_eq!(s(&session, "last_error"), "DEVICE_NOT_FOUND", "{}", j(&session));
    assert_eq!(s(&session, "device_by_id"), WP510R_FREE_BY_ID, "{}", j(&session));
    assert!(r.mgr.retired_drains_for_test(&sid).is_empty());
    assert!(!dev.is_open(), "退休 bridge 的 handle 已關閉");
    assert_eq!(dev.open_attempts(), opens, "全程不另開");
}

/// P8-566d90b-3 端到端：bind 等待期間退休 bridge 回條 `handle_closed:false`：收到終態即回 SESSION_RECOVERING＋
/// drain_stalled，state RECOVERING，不改綁、登記未確認關閉。
#[test]
fn wp510r_r3_rpc_bind_retired_close_failure_reports_recovering_stalled() {
    let (r, dev, _sid, opens) = wp510r_rpc_retired_bridge_paused();
    let (reply_tx, reply_rx) = crossbeam_channel::bounded(1);
    let bind_rig = Arc::clone(&r);
    let bind = std::thread::spawn(move || {
        let started = Instant::now();
        let reply = bind_rig.call(
            "session.bind",
            &format!(r#"{{"selector":"COM0","device_by_id":"{WP510R_FREE_BY_ID}"}}"#),
        );
        let _ = reply_tx.send((reply, started.elapsed()));
    });
    let early = reply_rx.recv_timeout(Duration::from_millis(300)).ok();
    dev.fail_close(PalError::io(5, "injected close failure"));
    dev.release_pause();
    let reply = reply_rx.recv_timeout(Duration::from_secs(10)).ok().or(early.clone());
    bind.join().expect("bind thread 不應 panic");
    assert!(early.is_none(), "退休 bridge 回條前 bind 不得回覆");
    let (reply, elapsed) = reply.expect("失敗回條後 bind 應回覆");
    assert!(elapsed < Duration::from_millis(2500), "bind 收到失敗終態即回，耗時 {elapsed:?}");
    assert_eq!(s(&reply, "error_code"), "SESSION_RECOVERING", "{}", j(&reply));
    assert_eq!(reply.get("drain_stalled"), Some(&PyValue::Bool(true)), "{}", j(&reply));
    let session = r.session("COM0").expect("COM0 session");
    assert_eq!(s(&session, "state"), "RECOVERING", "{}", j(&session));
    assert_eq!(s(&session, "device_by_id"), support::BY_ID_A, "關閉未確認不得改綁");
    assert!(r.mgr.unconfirmed_close_for_test(support::BY_ID_A), "退休 bridge 關閉未確認須登記阻擋");
    assert_eq!(dev.open_attempts(), opens, "全程不另開");
}

/// P8-566d90b-4 端到端（設計 §3.1.6 第二處 T-1）：release 等待退休 bridge 時失敗回條先到：release 不提前回覆，於首次
/// 期限（RPC 進入 +3 s，實時）回 DRAIN_STALLED；期限已過的 release 立即回。路徑：`Service::rpc("device.release")` →
/// `SessionManager::route_inner`（起算首次期限）→ `route_after_release_settle` → `ActorHandle::request_with_notice` →
/// actor `begin_stop(Release)`（retired_drains 未收斂建立待決停止）→ `retired_drain_receipt`（失敗只登記、不回覆）→ 首次
/// 期限 `ReleaseDeadlineElapsed` → `release_deadline_elapsed` → `mark_drain_timeout`。
#[test]
fn wp510r_r3_rpc_release_retired_failure_waits_first_deadline() {
    let (r, dev, _sid, opens) = wp510r_rpc_retired_bridge_paused();
    let (reply_tx, reply_rx) = crossbeam_channel::bounded(1);
    let release_rig = Arc::clone(&r);
    let release = std::thread::spawn(move || {
        let started = Instant::now();
        let reply = release_rig.call("device.release", r#"{"selector":"COM0"}"#);
        let _ = reply_tx.send((reply, started.elapsed()));
    });
    std::thread::sleep(Duration::from_millis(500));
    dev.fail_close(PalError::io(5, "injected close failure"));
    dev.release_pause();
    wait_until("失敗回條登記未確認關閉", || r.mgr.unconfirmed_close_for_test(support::BY_ID_A));
    let early = reply_rx.recv_timeout(Duration::from_millis(1500)).ok();
    let reply = reply_rx.recv_timeout(Duration::from_secs(10)).ok().or(early.clone());
    release.join().expect("release thread 不應 panic");
    assert!(
        early.is_none(),
        "失敗回條在等待期間到達不得提前回覆：{:?}",
        early.as_ref().map(|(reply, elapsed)| (j(reply), *elapsed))
    );
    let (reply, elapsed) = reply.expect("首次期限到應回覆");
    assert!(elapsed >= Duration::from_millis(2500), "首次期限前不得回覆，耗時 {elapsed:?}");
    assert!(elapsed < Duration::from_millis(5000), "首次期限後應回覆，耗時 {elapsed:?}");
    assert_eq!(s(&reply, "error_code"), "DRAIN_STALLED", "{}", j(&reply));
    assert_eq!(reply.get("drain_stalled"), Some(&PyValue::Bool(true)), "{}", j(&reply));
    assert_eq!(s(&sub(&reply, "session"), "state"), "RELEASED", "已 RELEASED 不改 state");
    let started = Instant::now();
    let again = r.call("device.release", r#"{"selector":"COM0"}"#);
    assert!(started.elapsed() < Duration::from_millis(1000), "期限已過的 release 立即回");
    assert_eq!(s(&again, "error_code"), "DRAIN_STALLED", "{}", j(&again));
    assert_eq!(dev.open_attempts(), opens, "全程不另開");
}

// ---------------------------------------------------------------------------------------------------------------
// S2-PAR-wp510r 第 8 項（R3 之三）：出生即接管、adopt 提交點的取消、帳本路徑的 release／bind 等待收進 actor。
//
// 呼叫路徑（接線證據）：`Service::rpc("device.attach")` → `route_with_selector` → `attach_released_placeholder` →
// dynamic worker `attach_dynamic` → `create_session_locked`（`place_session_custody_locked` 存 `Held{Session}`、
// `SessionCore::init_external_attach`）→ 新 actor 啟動效果 `Effect::ClaimCustody` → `OpenCtx::open_bridge_custody`；
// `Service::rpc("device.release")` → `route_inner` → `route_after_release_settle` → `ActorHandle::request_with_ack_notice`
// （`ReleaseWithAck`、`WaitAck::Release`）→ actor `release_request` → `begin_stop` 發 `Effect::ClaimForStop` →
// `ManagerExec::execute_with_feedback`（`claim_custody_for_stop_locked`、`spawn_custody_closer`、立即回饋
// `CustodyClaimed`）→ 關閉工作者 `finish_custody_close` → `CustodyTerminal` → actor `settle_retired` → `try_converge`。
// ---------------------------------------------------------------------------------------------------------------

/// 佔位交接後正式 session（t:COM3）的開埠工作停在 pre-take（`open_bridge_pre_take_hook`）。回傳 (rig, 裝置, 放行).
fn wp510r_r3_pre_take_rig() -> (Arc<ServiceRig>, FakeDevice, crossbeam_channel::Sender<()>) {
    let r = Arc::new(wp510_b_placeholder_rig());
    let dev = r.device(WP510_REAL_G).clone();
    let (entered_tx, entered_rx) = crossbeam_channel::bounded(1);
    let (go_tx, go_rx) = crossbeam_channel::bounded::<()>(1);
    r.mgr.set_open_bridge_pre_take_hook_for_test(Box::new(move || {
        let _ = entered_tx.send(());
        let _ = go_rx.recv_timeout(Duration::from_secs(20));
    }));
    let attached = r.call(
        "device.attach",
        &format!(r#"{{"selector":"{WP510_PLACEHOLDER_SID}"}}"#),
    );
    assert_eq!(attached.get("ok"), Some(&PyValue::Bool(true)), "{}", j(&attached));
    entered_rx
        .recv_timeout(Duration::from_secs(5))
        .expect("正式 session 的開埠工作應停在 pre-take");
    // 出生即接管（設計 §3.1.4 W2、C7）：紀錄 `Held`、owner 為新 session。
    let held = r.mgr.custody_for_test(WP510_PLACEHOLDER_BY_ID).expect("出生即接管的帳本紀錄");
    assert_eq!(held.phase, CustodyPhaseSnapshot::Held, "{held:?}");
    assert!(
        matches!(&held.owner, sw_core::custody::CustodyOwner::Session { actor } if Some(*actor) == r.mgr.actor_id_for_test("t:COM3")),
        "owner 為新建的正式 session：{held:?}"
    );
    (r, dev, go_tx)
}

/// P8-8463b22-1（設計 §1.4）：`tpl-a:COM3` 已建立、開埠工作停在 pre-take → release，關閉暫停超過 3 s → STALL（RECOVERING、
/// true、g）。開埠工作 claim 失敗（紀錄已是 Closing），不另開。關閉完成後：RELEASED、"cli"、時間已填、false/null、有落盤列；
/// open_attempts 不增加、close 1、零 TX；下一次 release 回 already_released（Python `:3105-3107`、`:3133`、`:3193-3204`、
/// `:5701`、`:4339-4355`）。`open_first`：(a) 先放行開埠工作；否則 (b) 先放行關閉。
fn wp510r_r3_rpc_p8_8463b22(open_first: bool) {
    let (r, dev, go_tx) = wp510r_r3_pre_take_rig();
    let attempts = dev.open_attempts();
    let tx_before = dev.tx_len();
    let closes_before = dev.close_count();
    dev.pause_next_close();
    let (reply_tx, reply_rx) = crossbeam_channel::bounded(1);
    let release_rig = Arc::clone(&r);
    let release = std::thread::spawn(move || {
        let started = std::time::Instant::now();
        let reply = release_rig.call("device.release", r#"{"selector":"t:COM3"}"#);
        let _ = reply_tx.send((reply, started.elapsed()));
    });
    let paused = dev.wait_paused(Duration::from_secs(5));
    let closing = r.mgr.custody_for_test(WP510_PLACEHOLDER_BY_ID);
    let stalled = reply_rx.recv_timeout(Duration::from_secs(8)).ok();
    let g = r.session("t:COM3").and_then(|s| s.get("bridge_generation").cloned());
    if open_first {
        let _ = go_tx.send(());
        std::thread::sleep(Duration::from_millis(300));
    }
    let state_mid = r.session("t:COM3").map(|session| s(&session, "state").to_string());
    dev.release_pause();
    if !open_first {
        wait_until("關閉完成", || !dev.is_open());
        std::thread::sleep(Duration::from_millis(300));
        assert_eq!(
            r.session("t:COM3").map(|session| s(&session, "state").to_string()).as_deref(),
            Some("RECOVERING"),
            "開埠工作仍在途時不得先完成 RELEASED"
        );
        let _ = go_tx.send(());
    }
    release.join().expect("release thread 不應 panic");

    assert!(paused, "release 的關閉應停在暫停點");
    let closing = closing.expect("關閉中的帳本紀錄");
    assert!(
        matches!(closing.phase, CustodyPhaseSnapshot::Closing { .. }),
        "release 把紀錄轉 Closing，開埠工作之後的 claim 會失敗：{closing:?}"
    );
    let (stalled, elapsed) = stalled.expect("release 須於首次期限回覆");
    assert!(elapsed < Duration::from_secs(6), "首次期限回覆：{elapsed:?}");
    assert_eq!(s(&stalled, "error_code"), "DRAIN_STALLED", "{}", j(&stalled));
    assert_eq!(stalled.get("drain_stalled"), Some(&PyValue::Bool(true)));
    assert_eq!(stalled.get("drain_timeout_epoch").cloned(), g, "{}", j(&stalled));
    assert_eq!(s(&sub(&stalled, "session"), "state"), "RECOVERING");
    assert_eq!(state_mid.as_deref(), Some("RECOVERING"));

    r.wait_state("t:COM3", "RELEASED");
    wait_until("RELEASED 落盤", || {
        wp510_review10_state_has_released_by_id(&r, WP510_PLACEHOLDER_BY_ID)
    });
    let session = r.session("t:COM3").expect("t:COM3 session");
    assert_eq!(s(&session, "released_by"), "cli", "{}", j(&session));
    assert!(session.get("released_at").and_then(PyValue::as_str).is_some());
    assert_eq!(session.get("drain_stalled"), Some(&PyValue::Bool(false)), "{}", j(&session));
    assert_eq!(session.get("drain_timeout_epoch"), Some(&PyValue::Null), "{}", j(&session));
    std::thread::sleep(Duration::from_millis(200));
    assert_eq!(dev.open_attempts(), attempts, "開埠工作 claim 失敗，不另開");
    assert_eq!(dev.close_count(), closes_before + 1, "handle 只關閉一次");
    assert_eq!(dev.tx_len(), tx_before, "零 TX");
    assert!(r.mgr.custody_for_test(WP510_PLACEHOLDER_BY_ID).is_none(), "帳本紀錄已結算");
    let again = r.call("device.release", r#"{"selector":"t:COM3"}"#);
    assert_eq!(again.get("ok"), Some(&PyValue::Bool(true)), "{}", j(&again));
    assert_eq!(again.get("already_released"), Some(&PyValue::Bool(true)), "{}", j(&again));
}

#[test]
fn wp510r_r3_rpc_p8_8463b22_release_stall_then_open_job_first() {
    wp510r_r3_rpc_p8_8463b22(true);
}

#[test]
fn wp510r_r3_rpc_p8_8463b22_release_stall_then_close_first() {
    wp510r_r3_rpc_p8_8463b22(false);
}

/// G-3（設計 v1 §1.8；Python `release_device` `:3132-3138`）：release 等待在途作業時先設 RECOVERING：等待期間 list 為
/// RECOVERING、`command.submit` 回 SESSION_RECOVERING；首次 release 清 `drain_stalled`／`drain_timeout_epoch`。
#[test]
fn wp510r_r3_rpc_g3_release_waiting_custody_sets_recovering() {
    let (r, dev, go_tx) = wp510r_r3_pre_take_rig();
    dev.pause_next_close();
    let release_rig = Arc::clone(&r);
    let release = std::thread::spawn(move || release_rig.call("device.release", r#"{"selector":"t:COM3"}"#));
    let paused = dev.wait_paused(Duration::from_secs(5));
    let listed = r.session("t:COM3").expect("t:COM3 session");
    let submit = r.call("command.submit", r#"{"selector":"t:COM3","cmd":"echo g3"}"#);
    dev.release_pause();
    let _ = go_tx.send(());
    let released = release.join().expect("release thread 不應 panic");

    assert!(paused, "前置：release 的關閉停在暫停點");
    assert_eq!(s(&listed, "state"), "RECOVERING", "{}", j(&listed));
    assert_eq!(listed.get("drain_stalled"), Some(&PyValue::Bool(false)), "{}", j(&listed));
    assert_eq!(listed.get("drain_timeout_epoch"), Some(&PyValue::Null), "{}", j(&listed));
    assert_eq!(s(&submit, "error_code"), "SESSION_RECOVERING", "{}", j(&submit));
    assert_eq!(released.get("ok"), Some(&PyValue::Bool(true)), "{}", j(&released));
}

/// WP5-1（設計 §1.4）：`tpl-a:COM3` 已建立，開埠工作在 pre-take 時 shutdown，closed:false：shutdown 報告未排空；有告警；
/// `CloseFailed`；B3 進集合；IoWorker 未啟動、零 TX（Python `:5647-5704`、`:1187`、`:5701`、`:4322-4332`、`:1228-1239`）。
#[test]
fn wp510r_r3_rpc_wp5_1_shutdown_pre_take_close_failure_not_drained() {
    let (r, dev, go_tx) = wp510r_r3_pre_take_rig();
    let tx_before = dev.tx_len();
    dev.fail_close(PalError::io(5, "[Errno 5] Input/output error"));
    let manager = r.mgr.clone();
    let shutdown = std::thread::spawn(move || manager.shutdown(Duration::from_millis(1500)));
    let drained = shutdown.join().expect("shutdown thread 不應 panic");
    let _ = go_tx.send(());
    std::thread::sleep(Duration::from_millis(200));

    assert!(
        drained.iter().any(|(target, ok)| target == "t:COM3" && !ok),
        "正式 session 未排空：{drained:?}"
    );
    assert!(
        drained
            .iter()
            .any(|(target, ok)| target == &format!("attach:{WP510_PLACEHOLDER_BY_ID}") && !ok),
        "未確認關閉的帳本紀錄列入 stalled：{drained:?}"
    );
    let custody = r.mgr.custody_for_test(WP510_PLACEHOLDER_BY_ID).expect("帳本紀錄保留");
    assert!(matches!(custody.phase, CustodyPhaseSnapshot::CloseFailed { .. }), "{custody:?}");
    assert!(r.mgr.unconfirmed_close_for_test(WP510_PLACEHOLDER_BY_ID), "B3 進未確認集合");
    // 告警內容由 sw-core `wp510r_r3_wp5_1_shutdown_pre_take_close_failure_warns`（ManagerRig 擷取 stderr）驗證。
    assert!(dev.is_open(), "closed:false 表示 handle 仍開啟");
    assert_eq!(dev.open_count(), 1, "IoWorker 未啟動、不重開");
    assert_eq!(dev.tx_len(), tx_before, "零 TX");
}

/// P8-fdc1413-1（設計第 5 版 §1.4，D1 iii＋T-1）：交接後 configure 失敗且 closed:false：`CloseFailed`、B3 進集合；之後的
/// 首次 release 等到首次期限才回 DRAIN_STALLED（失敗在首次 release 之前已知），期限已過的 release 立即回；session
/// DETACHED、不重開、零 TX（Python `:1278-1324`、`:1346-1368`、`:3188-3204`、`:4320-4332`）。
#[test]
fn wp510r_r3_rpc_p8_fdc1413_configure_close_failure_release_t1() {
    let r = wp510_b_placeholder_rig();
    let dev = r.device(WP510_REAL_G).clone();
    dev.fail_configure(PalError::io(5, "[Errno 5] Input/output error"));
    dev.fail_close(PalError::io(5, "[Errno 5] Input/output error"));
    let attached = r.call(
        "device.attach",
        &format!(r#"{{"selector":"{WP510_PLACEHOLDER_SID}"}}"#),
    );
    assert_eq!(attached.get("ok"), Some(&PyValue::Bool(true)), "{}", j(&attached));
    wait_until("configure 失敗後退回 DETACHED", || {
        r.session("t:COM3").is_some_and(|session| s(&session, "state") == "DETACHED")
    });
    wait_until("帳本紀錄轉 CloseFailed", || {
        r.mgr
            .custody_for_test(WP510_PLACEHOLDER_BY_ID)
            .is_some_and(|c| matches!(c.phase, CustodyPhaseSnapshot::CloseFailed { .. }))
    });
    let attempts = dev.open_attempts();
    let tx_before = dev.tx_len();
    assert!(r.mgr.unconfirmed_close_for_test(WP510_PLACEHOLDER_BY_ID));

    let started = std::time::Instant::now();
    let first = r.call("device.release", r#"{"selector":"t:COM3"}"#);
    let first_elapsed = started.elapsed();
    let started = std::time::Instant::now();
    let second = r.call("device.release", r#"{"selector":"t:COM3"}"#);
    let second_elapsed = started.elapsed();
    let drained = r.mgr.shutdown(Duration::from_millis(100));

    assert_eq!(s(&first, "error_code"), "DRAIN_STALLED", "{}", j(&first));
    assert!(first_elapsed >= Duration::from_millis(2500), "首次 release 等到首次期限：{first_elapsed:?}");
    assert_eq!(s(&second, "error_code"), "DRAIN_STALLED", "{}", j(&second));
    assert!(second_elapsed < Duration::from_millis(1000), "期限已過的 release 立即回：{second_elapsed:?}");
    assert!(
        drained
            .iter()
            .any(|(target, ok)| target == &format!("attach:{WP510_PLACEHOLDER_BY_ID}") && !ok),
        "shutdown 未排空：{drained:?}"
    );
    std::thread::sleep(Duration::from_millis(200));
    assert_eq!(dev.open_attempts(), attempts, "不重開");
    assert_eq!(dev.tx_len(), tx_before, "零 TX");
}

/// P8-14fe592-1 共用前置（設計第 5 版 §1.4）：① 開埠工作已取得交接 handle、停在 post-take ② `session.bind t:COM3 →
/// 空閒 by_id` ③ 3 s 後 bind 回 RECOV{true, g, RECOVERING}（FakeClock 不推進：BindWait 由 RPC 執行緒的實時通知收尾，
/// SPEC-GAP 2）④ 放行 post-take：adopt 提交點看到取消，關閉而不接手（不啟動 IoWorker、沒有登入 TX）；關閉停在暫停點；
/// is_open true、open_attempts 不變。回傳 (rig, 裝置, 開埠次數, TX 長度, 關閉次數)。
fn wp510r_r3_p8_14fe592_commit_cancel_closing() -> (Arc<ServiceRig>, FakeDevice, usize, usize, usize) {
    let r = Arc::new(wp510_b_placeholder_rig());
    let dev = r.device(WP510_REAL_G).clone();
    let (entered_tx, entered_rx) = crossbeam_channel::bounded(1);
    let (go_tx, go_rx) = crossbeam_channel::bounded::<()>(1);
    r.mgr.set_open_bridge_post_take_hook_for_test(Box::new(move || {
        let _ = entered_tx.send(());
        let _ = go_rx.recv_timeout(Duration::from_secs(20));
    }));
    let attached = r.call(
        "device.attach",
        &format!(r#"{{"selector":"{WP510_PLACEHOLDER_SID}"}}"#),
    );
    assert_eq!(attached.get("ok"), Some(&PyValue::Bool(true)), "{}", j(&attached));
    let entered = entered_rx.recv_timeout(Duration::from_secs(5)).is_ok();
    let taken = r.mgr.custody_for_test(WP510_PLACEHOLDER_BY_ID);
    let attempts = dev.open_attempts();
    let tx_before = dev.tx_len();
    let closes_before = dev.close_count();
    let started = std::time::Instant::now();
    let bind = r.call(
        "session.bind",
        &format!(r#"{{"selector":"t:COM3","device_by_id":"{WP510_FREE_BY_ID}"}}"#),
    );
    let bind_elapsed = started.elapsed();
    let g = r.session("t:COM3").and_then(|s| s.get("bridge_generation").cloned());
    dev.pause_next_close();
    let _ = go_tx.send(());
    let paused = dev.wait_paused(Duration::from_secs(5));
    if !paused {
        dev.release_pause();
    }
    assert!(entered, "開埠工作應停在 post-take");
    assert!(
        taken.as_ref().is_some_and(|c| matches!(c.phase, CustodyPhaseSnapshot::Taken { .. })),
        "post-take：紀錄為 Taken{{OpenJob}}：{taken:?}"
    );
    assert_eq!(s(&bind, "error_code"), "SESSION_RECOVERING", "{}", j(&bind));
    assert_eq!(bind.get("drain_stalled"), Some(&PyValue::Bool(true)), "{}", j(&bind));
    assert_eq!(bind.get("drain_timeout_epoch").cloned(), g, "{}", j(&bind));
    assert_eq!(s(&sub(&bind, "session"), "state"), "RECOVERING", "{}", j(&bind));
    assert!(
        bind_elapsed >= Duration::from_millis(2500) && bind_elapsed < Duration::from_secs(5),
        "bind 等 BindWait 期限（實時）：{bind_elapsed:?}"
    );
    assert!(paused, "adopt 提交點看到取消後的關閉應停在暫停點");
    let closing = r.mgr.custody_for_test(WP510_PLACEHOLDER_BY_ID).expect("關閉中的帳本紀錄");
    assert!(
        matches!(closing.phase, CustodyPhaseSnapshot::Closing { .. }),
        "adopt 提交點看到取消：關閉而不接手（不轉 Bridged）：{closing:?}"
    );
    assert!(dev.is_open(), "前置：handle 尚未關閉");
    assert_eq!(dev.open_attempts(), attempts, "前置：沿用交接 handle，未另開");
    (r, dev, attempts, tx_before, closes_before)
}

/// P8-14fe592-1(a)：⑤ 關閉暫停時 `device.release t:COM3`：回條前不回覆、不轉 RELEASED、不落盤；關閉成功後回 OK，
/// RELEASED 並落盤，close 加 1，零 TX。
#[test]
fn wp510r_r3_rpc_p8_14fe592_commit_cancel_release_close_ok() {
    let (r, dev, attempts, tx_before, closes_before) = wp510r_r3_p8_14fe592_commit_cancel_closing();
    let (reply_tx, reply_rx) = crossbeam_channel::bounded(1);
    let release_rig = Arc::clone(&r);
    let release = std::thread::spawn(move || {
        let _ = reply_tx.send(release_rig.call("device.release", r#"{"selector":"t:COM3"}"#));
    });
    let early = reply_rx.recv_timeout(Duration::from_millis(500)).ok();
    let state_mid = r.session("t:COM3").map(|session| s(&session, "state").to_string());
    let persisted_mid = wp510_review10_state_has_released_by_id(&r, WP510_PLACEHOLDER_BY_ID);
    dev.release_pause();
    let released = early.or_else(|| reply_rx.recv_timeout(Duration::from_secs(10)).ok());
    release.join().expect("release thread 不應 panic");

    let released = released.expect("關閉後 release 應回覆");
    assert_eq!(state_mid.as_deref(), Some("RECOVERING"), "回條前不轉 RELEASED");
    assert!(!persisted_mid, "回條前不落盤");
    assert_eq!(released.get("ok"), Some(&PyValue::Bool(true)), "{}", j(&released));
    assert_eq!(s(&sub(&released, "session"), "state"), "RELEASED", "{}", j(&released));
    assert!(wp510_review10_state_has_released_by_id(&r, WP510_PLACEHOLDER_BY_ID));
    assert_eq!(dev.close_count(), closes_before + 1);
    assert_eq!(dev.open_attempts(), attempts);
    assert_eq!(dev.tx_len(), tx_before, "零 TX（沒有 IoWorker、沒有登入 TX）");
}

/// P8-14fe592-1(b)：同 (a)，關閉回 closed:false：收到失敗回條時不提前回覆；release 在首次期限（⑤ 加 3 s）回
/// DRAIN_STALLED（T-1），drain 欄位在回覆時才設為 true/g；B3 進集合；期限已過的 release 立即回 DRAIN_STALLED；零 TX。
#[test]
fn wp510r_r3_rpc_p8_14fe592_commit_cancel_release_close_failure_t1() {
    let (r, dev, attempts, tx_before, _) = wp510r_r3_p8_14fe592_commit_cancel_closing();
    dev.fail_close(PalError::io(5, "[Errno 5] Input/output error"));
    let (reply_tx, reply_rx) = crossbeam_channel::bounded(1);
    let release_rig = Arc::clone(&r);
    let release = std::thread::spawn(move || {
        let started = std::time::Instant::now();
        let reply = release_rig.call("device.release", r#"{"selector":"t:COM3"}"#);
        let _ = reply_tx.send((reply, started.elapsed()));
    });
    std::thread::sleep(Duration::from_millis(300));
    let reset = r.session("t:COM3").expect("t:COM3 session");
    dev.release_pause();
    let early = reply_rx.recv_timeout(Duration::from_millis(1000)).ok();
    let (stalled, elapsed) = early
        .clone()
        .or_else(|| reply_rx.recv_timeout(Duration::from_secs(10)).ok())
        .expect("release 應於首次期限回覆");
    release.join().expect("release thread 不應 panic");
    let started = std::time::Instant::now();
    let again = r.call("device.release", r#"{"selector":"t:COM3"}"#);
    let again_elapsed = started.elapsed();

    assert_eq!(reset.get("drain_stalled"), Some(&PyValue::Bool(false)), "首次 release 重設 drain：{}", j(&reset));
    assert!(early.is_none(), "收到失敗回條時不提前回覆（T-1）");
    assert!(elapsed >= Duration::from_millis(2500), "首次期限回覆：{elapsed:?}");
    assert_eq!(s(&stalled, "error_code"), "DRAIN_STALLED", "{}", j(&stalled));
    assert_eq!(stalled.get("drain_stalled"), Some(&PyValue::Bool(true)));
    let g = r.session("t:COM3").and_then(|s| s.get("bridge_generation").cloned());
    assert_eq!(stalled.get("drain_timeout_epoch").cloned(), g);
    assert!(r.mgr.unconfirmed_close_for_test(WP510_PLACEHOLDER_BY_ID), "B3 進集合");
    assert_eq!(s(&again, "error_code"), "DRAIN_STALLED", "{}", j(&again));
    assert!(again_elapsed < Duration::from_millis(1000), "期限已過的 release 立即回：{again_elapsed:?}");
    assert_eq!(dev.open_attempts(), attempts);
    assert_eq!(dev.tx_len(), tx_before, "零 TX");
}

// ---------------------------------------------------------------------------------------------------------------
// S2-PAR-wp510r 第 8 項第 2 輪：Fable 審查 P8-7bb9935-1（High）的 ServiceRig 端到端。
//
// 呼叫路徑：`Service::rpc("session.bind")` → manager bind（`offer_custody`，`AcceptCustody{open_now:false}`）→ 掛鉤停在
// `DeviceOnline` 之前；`Service::rpc("session.recover")` → actor `recover_request` 的 Reattach 計畫 → 持有帳本紀錄時發
// `Effect::ClaimCustody` → `OpenCtx::open_bridge_custody` 以原 handle 開 bridge；`device.release` → `begin_stop` →
// 正式 bridge 排空、關閉該 handle。
// ---------------------------------------------------------------------------------------------------------------

/// P8-7bb9935-1：bind 已把 parked handle 交給 COM1（`AcceptCustody`）、送出 `DeviceOnline` 之前插入 `session.recover`。
/// Python `recover_session` 無 bridge、裝置在線即 ATTACHING 並 `_spawn_attach`（`origin/main:sw_core/session_manager.py:7970-7977`），
/// 由同一作業的 handle 續行、不另開（`:4115-4116`、`:4852-4854`），回 REATTACH。放行 bind 後 COM1 以原 handle 到 READY、
/// 帳本紀錄移除；release 關閉該 handle（close 1），之後的 `device.attach` 照常成功。
#[test]
fn wp510r_r3_rpc_p8_7bb9935_recover_before_bind_device_online_uses_held_handle() {
    let r = Arc::new(wp510_b_placeholder_rig());
    let (bind, go, dev) = wp510_review9_bind_paused_before_device_online(&r);
    let attempts = dev.open_attempts();

    let recovered = r.call("session.recover", r#"{"selector":"COM1"}"#);
    let attempts_after_recover = dev.open_attempts();
    go.send(()).expect("放行 bind");
    let bound = bind.join().expect("bind thread 不應 panic");

    assert_eq!(recovered.get("ok"), Some(&PyValue::Bool(true)), "{}", j(&recovered));
    assert_eq!(s(&recovered, "action"), "REATTACH", "{}", j(&recovered));
    assert_eq!(attempts_after_recover, attempts, "recover 以原 handle 續行，不另開埠");
    assert_eq!(bound.get("ok"), Some(&PyValue::Bool(true)), "{}", j(&bound));
    r.wait_state("COM1", "READY");
    assert_eq!(dev.open_attempts(), attempts, "沿用交給 session 的 handle，不另開埠");
    assert_eq!(dev.close_count(), 0);
    assert!(r.mgr.custody_for_test(WP510_PLACEHOLDER_BY_ID).is_none(), "正式 bridge 接手後帳本紀錄移除");
    assert!(!r.mgr.has_inflight_attach_for_test(WP510_PLACEHOLDER_BY_ID));

    let released = r.call("device.release", r#"{"selector":"COM1"}"#);
    assert_eq!(released.get("ok"), Some(&PyValue::Bool(true)), "{}", j(&released));
    assert!(!dev.is_open(), "release 回 ok 時 handle 已關閉");
    assert_eq!(dev.close_count(), 1);
    r.wait_state("COM1", "RELEASED");

    let attached = r.call("device.attach", r#"{"selector":"COM1"}"#);
    assert_eq!(attached.get("ok"), Some(&PyValue::Bool(true)), "{}", j(&attached));
}

/// S2-PAR-wp510r R4 紅燈 P8-e25b69c-2 的端到端證據（設計 §1.6、§3.1.4 W6）：接線為 `Service::rpc("device.release")` →
/// `SessionManager::route`（manager 不等待 W6 作業；第 15 項起 release 的等待全由 actor 主導）→ `ReleaseWithAck` → actor `begin_stop(Release)` →
/// `Effect::ClaimForStop{token}` → executor 在 manager 鎖內對 W6 紀錄（`Opening{Worker}`、owner＝COM0）寫入 cancel＝Release →
/// RPC 執行緒於首次期限送 `ReleaseDeadlineElapsed` → DRAIN_STALLED。開埠遲到返回後 W6 worker 於檢查點關閉（零 PROBE TX）、
/// `finish_custody_close` 結算並送 `CustodyTerminal` → actor 完成 RELEASED（released_by＝cli、drain 欄位清空）並落盤。
/// Python 對照見 `wp510_review10_rpc_release_timeout_then_late_redetect_finish_completes_release`。
#[test]
fn wp510r_r4_rpc_p8_e25b69c_2_redetect_release_stall_ledger_then_released() {
    let delegate = FakeSerialBackend::new();
    let dev = delegate.add_device(WP510_R10_REAL_D);
    let backend = Arc::new(Wp510ToggleOpenBackend {
        delegate,
        path: WP510_R10_REAL_D.to_string(),
        blocked: Mutex::new(false),
        cv: Condvar::new(),
        attempts: AtomicUsize::new(0),
    });
    let _unblock = Wp510UnblockOnDrop(Arc::clone(&backend));
    let serial: Arc<dyn SerialBackend> = backend.clone();
    let spec = RigSpec {
        yaml: "profiles:\n  t:\n    platform: shell\n    prompt_regex: '(?m)^root@t:.*# '\n    timeout_s: 2.0\n  others-template:\n    platform: passthrough\n    prompt_regex: '.*'\n    login_regex: '$^'\n    password_regex: '$^'\n    ready_probe: ''\n".to_string(),
        devices: vec![(WP510_R10_BY_ID_D.to_string(), WP510_R10_REAL_D.to_string())],
        explicit_target_count: 0,
        allowlist: None,
        wait_ready: Vec::new(),
    };
    let r = ServiceRig::build_with_loaded_state_and_serial_backend(spec, LoadedState::default(), serial);
    r.wait_state("COM0", "ATTACHED");
    let sid = s(&r.session("COM0").expect("COM0 session"), "session_id").to_string();
    let snapshot = vec![(
        WP510_R10_BY_ID_D.to_string(),
        DeviceInfo {
            by_id: WP510_R10_BY_ID_D.to_string(),
            real_path: WP510_R10_REAL_D.to_string(),
        },
    )];
    r.mgr.update_devices(&[]);
    r.wait_state("COM0", "DETACHED");
    backend.block();
    let attempts_before = backend.attempts();
    r.mgr.update_devices(&snapshot);
    wait_until("再偵測卡在開埠", || backend.attempts() > attempts_before);
    let blocked = r.mgr.custody_for_test(WP510_R10_BY_ID_D).expect("W6 開埠前已登記帳本紀錄");
    assert!(
        matches!(
            blocked.phase,
            CustodyPhaseSnapshot::Opening {
                taker: sw_core::custody::CustodyTaker::Worker(_)
            }
        ) && matches!(&blocked.owner, sw_core::custody::CustodyOwner::Session { actor } if Some(*actor) == r.mgr.actor_id_for_test(&sid)),
        "{blocked:?}"
    );
    let closes_before = dev.close_count();
    let tx_before = dev.tx_len();

    let started = Instant::now();
    let released = r.call("device.release", r#"{"selector":"COM0"}"#);
    let elapsed = started.elapsed();
    assert_eq!(s(&released, "error_code"), "DRAIN_STALLED", "{}", j(&released));
    assert_eq!(released.get("drain_stalled"), Some(&PyValue::Bool(true)), "{}", j(&released));
    assert_eq!(s(&sub(&released, "session"), "state"), "RECOVERING", "{}", j(&released));
    assert!(elapsed >= Duration::from_millis(2500), "release 須等到首次期限：{elapsed:?}");
    let stalled = r.mgr.custody_for_test(WP510_R10_BY_ID_D).expect("阻塞時紀錄保留");
    assert_eq!(stalled.token, blocked.token);
    assert_eq!(stalled.cancel, Some(sw_core::custody::CustodyCancel::Release), "{stalled:?}");
    let listed = r.call("session.list", "{}");
    assert!(j(&listed).contains("\"RECOVERING\""), "等待期間 list 為 RECOVERING：{}", j(&listed));

    backend.unblock();
    r.wait_state("COM0", "RELEASED");
    wait_until("帳本紀錄收尾", || r.mgr.custody_for_test(WP510_R10_BY_ID_D).is_none());
    wait_until("RELEASED 落盤", || wp510_review10_state_has_released_by_id(&r, WP510_R10_BY_ID_D));
    let session = r.session("COM0").expect("COM0 session");
    assert_eq!(s(&session, "released_by"), "cli", "{}", j(&session));
    assert_eq!(session.get("drain_stalled"), Some(&PyValue::Bool(false)), "{}", j(&session));
    assert_eq!(session.get("drain_timeout_epoch"), Some(&PyValue::Null), "{}", j(&session));
    assert_eq!(dev.close_count(), closes_before + 1, "遲到的 handle 關閉一次");
    assert!(!dev.is_open(), "沒有 IoWorker 持有 handle");
    assert_eq!(dev.tx_len(), tx_before, "取消後不得送 PROBE");
    std::thread::sleep(Duration::from_millis(200));
    assert_eq!(backend.attempts(), attempts_before + 1, "不另開");
    let again = r.call("device.release", r#"{"selector":"COM0"}"#);
    assert_eq!(again.get("already_released"), Some(&PyValue::Bool(true)), "不需第二次 release：{}", j(&again));
}

// ---------------------------------------------------------------------------
// S2-PAR-wp510r R5（設計 §4 R5、§3.1.4 W1、§1.2、§1.3）：佔位 W1 改由 actor（RPC 端到端）。
// ---------------------------------------------------------------------------

/// P8-19df186-1 (ii)（設計 v3 §1.3）：退休在佔位 actor 自己的執行緒上完成；release 已取得佔位、但落在退休之後時回
/// `SESSION_NOT_FOUND{selector}`（Python release 與退休各在一個鎖區段，`origin/main:sw_core/session_manager.py:3095-3138`、
/// `:3573-3612`），A 回 OKS，正式 `t:COM3` 照常 attach；不得出現新舊 session 都不存在。
#[test]
fn wp510r_r5_rpc_p8_19df186_1_release_after_retirement_returns_not_found() {
    let r = Arc::new(wp510_b_placeholder_rig());
    let (entered_tx, entered_rx) = crossbeam_channel::bounded(1);
    let (resume_tx, resume_rx) = crossbeam_channel::bounded::<()>(1);
    r.mgr.set_placeholder_retire_pre_commit_hook_for_test(
        WP510_PLACEHOLDER_SID,
        Box::new(move || {
            let _ = entered_tx.send(());
            let _ = resume_rx.recv_timeout(Duration::from_secs(10));
        }),
    );
    let attach_rig = Arc::clone(&r);
    let attach = std::thread::spawn(move || {
        attach_rig.call("device.attach", &format!(r#"{{"selector":"{WP510_PLACEHOLDER_SID}"}}"#))
    });
    entered_rx
        .recv_timeout(Duration::from_secs(5))
        .expect("佔位 actor 停在退休提交前");
    let release_rig = Arc::clone(&r);
    let (release_tx, release_rx) = crossbeam_channel::bounded(1);
    let release = std::thread::spawn(move || {
        let _ = release_tx.send(release_rig.call("device.release", r#"{"selector":"COM3"}"#));
    });
    // release 已解析到佔位並排入它的 mailbox（佔位 actor 正在執行退休效果）。
    assert!(release_rx.recv_timeout(Duration::from_millis(300)).is_err(), "退休提交前 release 不得回覆");
    let _ = resume_tx.send(());
    let attached = attach.join().expect("attach RPC thread");
    let released = release_rx.recv_timeout(Duration::from_secs(5)).expect("release 應回覆");
    release.join().expect("release RPC thread");
    assert_eq!(attached.get("ok"), Some(&PyValue::Bool(true)), "{}", j(&attached));
    assert_eq!(
        j(&released),
        format!(
            r#"{{"ok":false,"error_code":"SESSION_NOT_FOUND","selector":"COM3","version":"{}"}}"#,
            r.version
        ),
        "退休後的 release RPC bytes"
    );
    wait_until("t:COM3 正式 session", || r.mgr.resolve("t:COM3").as_deref() == Some("t:COM3"));
    assert!(r.mgr.resolve(WP510_PLACEHOLDER_SID).is_none(), "舊 sid 已退休");
}

/// P8-3786805-1（設計 v3 §1.2）：`force:true` 跳過預掃、開埠回 Held 時，回應位元組帶呼叫者原 selector（Python
/// `origin/main:sw_core/session_manager.py:3510-3518`），佔位不變；三種 selector（sid、COM、alias）皆同。
#[test]
fn wp510r_r5_rpc_p8_3786805_1_force_open_held_echoes_original_selector() {
    for selector in [WP510_PLACEHOLDER_SID, "COM3", WP510_PLACEHOLDER_ALIAS] {
        let (r, backend, entered, dev) = wp510_b_placeholder_blocking_rig_with_open_error(Some(OpenError::Held(
            HolderReport {
                pids: vec![4242],
                holder_unknown: false,
                status: HolderScanStatus::Ok,
            },
        )));
        let r = Arc::new(r);
        let attach_rig = Arc::clone(&r);
        let sel = selector.to_string();
        let attach = std::thread::spawn(move || {
            attach_rig.call("device.attach", &format!(r#"{{"selector":"{sel}","force":true}}"#))
        });
        entered.recv_timeout(Duration::from_secs(5)).expect("開埠進入阻塞點");
        backend.release();
        let response = attach.join().expect("attach RPC thread");
        assert_eq!(
            j(&response),
            format!(
                r#"{{"ok":false,"error_code":"DEVICE_STILL_HELD","pids":[4242],"holder_unknown":false,"selector":"{selector}","version":"{}"}}"#,
                r.version
            ),
            "force＋Held RPC bytes（selector={selector}）"
        );
        assert_eq!(r.mgr.released_placeholder(WP510_PLACEHOLDER_SID), Some(true));
        assert!(r.mgr.custody_for_test(WP510_PLACEHOLDER_BY_ID).is_none(), "開埠失敗不留帳本紀錄");
        assert!(!dev.is_open());
    }
}

/// WP1-1／WP1-3（設計 v3 §1.2）端到端：開埠阻塞時帳本為 `Opening{PlaceholderOpen}`；重複 attach 回 SESSION_RECOVERING（open 仍為
/// 1）；release 寫入 cancel＝Release 並等到關閉回條；放行後 A 回 SESSION_RECOVERING、B 回 already_released（Python 鍵序
/// `origin/main:sw_core/session_manager.py:3209`）。呼叫路徑：`device.release` → `SessionManager::route` → `ReleaseWithAck` →
/// actor `begin_stop(Release)` → `Effect::ClaimForStop{token}` → 開埠工作提交點關閉 → `CustodyTerminal` →
/// `PlaceholderOpened(Cancelled)` → 收斂 → 落盤 → 回覆。
#[test]
fn wp510r_r5_rpc_wp1_1_release_during_placeholder_open_waits_ledger_close() {
    let (r, backend, entered, dev) = wp510_b_placeholder_blocking_rig();
    let r = Arc::new(r);
    let attach_rig = Arc::clone(&r);
    let attach = std::thread::spawn(move || {
        attach_rig.call("device.attach", &format!(r#"{{"selector":"{WP510_PLACEHOLDER_SID}"}}"#))
    });
    entered.recv_timeout(Duration::from_secs(5)).expect("開埠進入阻塞點");
    let rec = r.mgr.custody_for_test(WP510_PLACEHOLDER_BY_ID).expect("開埠在途紀錄");
    assert!(
        matches!(&rec.phase, CustodyPhaseSnapshot::Opening { taker: sw_core::custody::CustodyTaker::PlaceholderOpen { .. } }),
        "{rec:?}"
    );
    let duplicate = r.call("device.attach", &format!(r#"{{"selector":"{WP510_PLACEHOLDER_SID}"}}"#));
    assert_eq!(s(&duplicate, "error_code"), "SESSION_RECOVERING", "{}", j(&duplicate));
    let release_rig = Arc::clone(&r);
    let (release_tx, release_rx) = crossbeam_channel::bounded(1);
    let release = std::thread::spawn(move || {
        let _ = release_tx.send(release_rig.call("device.release", r#"{"selector":"COM3"}"#));
    });
    wait_until("release 在帳本紀錄寫入取消", || {
        r.mgr.custody_for_test(WP510_PLACEHOLDER_BY_ID).is_some_and(|rec2| {
            rec2.token == rec.token && rec2.cancel == Some(sw_core::custody::CustodyCancel::Release)
        })
    });
    assert!(release_rx.recv_timeout(Duration::from_millis(200)).is_err(), "關閉回條前不回覆");
    backend.release();
    let attached = attach.join().expect("attach RPC thread");
    let released = release_rx.recv_timeout(Duration::from_secs(5)).expect("release 應回覆");
    release.join().expect("release RPC thread");
    assert_eq!(s(&attached, "error_code"), "SESSION_RECOVERING", "{}", j(&attached));
    let keys: Vec<&str> = released.0.keys().map(String::as_str).collect();
    assert_eq!(keys, ["ok", "already_released", "session", "version"], "{}", j(&released));
    assert_eq!(dev.open_count(), 1, "WP1-3：重複 attach 不另開");
    assert_eq!(dev.close_count(), 1);
    assert!(r.mgr.custody_for_test(WP510_PLACEHOLDER_BY_ID).is_none());
    assert_eq!(r.mgr.released_placeholder(WP510_PLACEHOLDER_SID), Some(true));
}

/// P8-a07fdfb-1（審查第 1 輪 High）端到端：佔位 `device.attach` 開埠阻塞時同一 by_id 改指新路徑（`update_devices` →
/// `detach_by_id` → `close_session_custody_for(Device)` 在帳本紀錄寫入 cancel＝Device），舊開埠接著回 PermissionError。
/// 呼叫路徑：`device.attach` → `SessionManager::route_with_selector` → `DeviceAttachRouted` → 佔位 actor
/// `PrepareHandoffWorker` → `OpenPlaceholderRaw` → `open_placeholder_raw` 的無 handle 收尾（帶回紀錄的取消）→
/// `PlaceholderOpened(Other, cancel=Device)` → 不退休、丟棄 worker → 回覆。Python 已取消的 OSError 分支回
/// SESSION_RECOVERING、保留 RELEASED 佔位（`origin/main:sw_core/session_manager.py:3529-3531`、`:3561-3567`）；新路徑零開埠。
#[test]
fn wp510r_r5_rpc_p8_a07fdfb_1_device_cancel_then_oserror_keeps_placeholder() {
    const NEW_REAL: &str = "/fake/tty-G2";
    let (r, backend, entered, dev) = wp510_b_placeholder_blocking_rig_with_open_error(Some(OpenError::Other(
        PalError::plain(PalErrorKind::PermissionDenied, "Permission denied"),
    )));
    let new_dev = backend.delegate.add_device(NEW_REAL);
    wp510_install_shell_replies(&new_dev);
    let r = Arc::new(r);
    let state_before = r.fs.bytes(support::STATE_PATH);
    let attach_rig = Arc::clone(&r);
    let attach = std::thread::spawn(move || {
        attach_rig.call("device.attach", &format!(r#"{{"selector":"{WP510_PLACEHOLDER_SID}"}}"#))
    });
    entered.recv_timeout(Duration::from_secs(5)).expect("開埠進入阻塞點");
    let token = r.mgr.custody_for_test(WP510_PLACEHOLDER_BY_ID).expect("開埠在途紀錄").token;
    let retargeted: Vec<(String, DeviceInfo)> = r
        .mgr
        .list_devices()
        .into_iter()
        .map(|row| {
            let by_id = s(&row, "by_id").to_string();
            let real_path = if by_id == WP510_PLACEHOLDER_BY_ID {
                NEW_REAL.to_string()
            } else {
                s(&row, "real_path").to_string()
            };
            (by_id.clone(), DeviceInfo { by_id, real_path })
        })
        .collect();
    r.mgr.update_devices(&retargeted);
    wait_until("裝置改指在帳本紀錄寫入取消", || {
        r.mgr.custody_for_test(WP510_PLACEHOLDER_BY_ID).is_some_and(|rec| {
            rec.token == token && rec.cancel == Some(sw_core::custody::CustodyCancel::Device)
        })
    });
    backend.release();
    let attached = attach.join().expect("attach RPC thread");
    let session = r.session(WP510_PLACEHOLDER_SID).expect("佔位保留");
    assert_eq!(s(&session, "state"), "RELEASED", "{}", j(&session));
    assert_eq!(
        j(&attached),
        format!(
            r#"{{"ok":false,"error_code":"SESSION_RECOVERING","drain_stalled":false,"drain_timeout_epoch":null,"session":{},"version":"{}"}}"#,
            j(&session),
            r.version
        ),
        "已取消的 OSError RPC bytes"
    );
    assert_eq!(r.mgr.released_placeholder(WP510_PLACEHOLDER_SID), Some(true));
    assert!(r.mgr.custody_for_test(WP510_PLACEHOLDER_BY_ID).is_none(), "帳本無殘留紀錄");
    std::thread::sleep(Duration::from_millis(300));
    assert_eq!(new_dev.open_attempts(), 0, "不啟動新路徑交接");
    assert_eq!(dev.open_count(), 0, "舊路徑開埠失敗、未取得 handle");
    assert!(r.mgr.resolve("t:COM3").is_none(), "沒有正式 session");
    assert_eq!(r.fs.bytes(support::STATE_PATH), state_before, "不落盤退休");
}

/// P8-a07fdfb-1（審查第 2 輪殘留，High）端到端：佔位 `device.attach` 開埠回 PermissionError，開埠工作以 NoHandle 結算帳本
/// 紀錄；佔位 actor 執行 `RetirePlaceholder`、取 manager 鎖之前（H-retire-pre-commit）同一 by_id 先改指新路徑、再改回原路徑。
/// 呼叫路徑：`device.attach` → `SessionManager::route_with_selector` → `DeviceAttachRouted` → 佔位 actor `PrepareHandoffWorker`
/// → `OpenPlaceholderRaw` → `open_placeholder_raw` 無 handle 收尾 → `PlaceholderOpened(Other)` → `RetirePlaceholder{OsError}` →
/// `retire_placeholder` 鎖內核對本次作業的取消 → `RetireResult::Invalid` → 回覆。Python 的取消不因路徑恢復撤銷
/// （`origin/main:sw_core/session_manager.py:4031-4034`），OSError 收尾讀到已取消即回 SESSION_RECOVERING、保留 RELEASED 佔位
/// （`:3529-3531`、`:3561-3567`）；原路徑不再開埠、新路徑零開埠、不落盤退休。
#[test]
fn wp510r_r5_rpc_p8_a07fdfb_1_retarget_and_back_before_oserror_retire_keeps_placeholder() {
    const NEW_REAL: &str = "/fake/tty-G2";
    let (r, backend, entered, dev) = wp510_b_placeholder_blocking_rig_with_open_error(Some(OpenError::Other(
        PalError::plain(PalErrorKind::PermissionDenied, "Permission denied"),
    )));
    let new_dev = backend.delegate.add_device(NEW_REAL);
    wp510_install_shell_replies(&new_dev);
    let r = Arc::new(r);
    let state_before = r.fs.bytes(support::STATE_PATH);
    let original: Vec<(String, DeviceInfo)> = r
        .mgr
        .list_devices()
        .into_iter()
        .map(|row| {
            let by_id = s(&row, "by_id").to_string();
            let real_path = s(&row, "real_path").to_string();
            (by_id.clone(), DeviceInfo { by_id, real_path })
        })
        .collect();
    let retargeted: Vec<(String, DeviceInfo)> = original
        .iter()
        .map(|(by_id, info)| {
            let mut info = info.clone();
            if by_id == WP510_PLACEHOLDER_BY_ID {
                info.real_path = NEW_REAL.to_string();
            }
            (by_id.clone(), info)
        })
        .collect();
    let hook_rig = Arc::clone(&r);
    let (hooked_tx, hooked_rx) = crossbeam_channel::bounded(1);
    r.mgr.set_placeholder_retire_pre_commit_hook_for_test(
        WP510_PLACEHOLDER_SID,
        Box::new(move || {
            hook_rig.mgr.update_devices(&retargeted);
            hook_rig.mgr.update_devices(&original);
            let _ = hooked_tx.send(());
        }),
    );
    let attach_rig = Arc::clone(&r);
    let attach = std::thread::spawn(move || {
        attach_rig.call("device.attach", &format!(r#"{{"selector":"{WP510_PLACEHOLDER_SID}"}}"#))
    });
    entered.recv_timeout(Duration::from_secs(5)).expect("開埠進入阻塞點");
    backend.release();
    let attached = attach.join().expect("attach RPC thread");
    hooked_rx.try_recv().expect("退休提交前已改指再改回");
    let session = r.session(WP510_PLACEHOLDER_SID).expect("佔位保留");
    assert_eq!(s(&session, "state"), "RELEASED", "{}", j(&session));
    assert_eq!(
        j(&attached),
        format!(
            r#"{{"ok":false,"error_code":"SESSION_RECOVERING","drain_stalled":false,"drain_timeout_epoch":null,"session":{},"version":"{}"}}"#,
            j(&session),
            r.version
        ),
        "已取消的 OSError RPC bytes"
    );
    assert_eq!(r.mgr.released_placeholder(WP510_PLACEHOLDER_SID), Some(true));
    assert!(r.mgr.custody_for_test(WP510_PLACEHOLDER_BY_ID).is_none(), "帳本無殘留紀錄");
    std::thread::sleep(Duration::from_millis(300));
    assert!(entered.try_recv().is_err(), "原路徑開埠總次數仍為 1");
    assert_eq!(dev.open_attempts(), 0, "原路徑不得由交接 worker 再開埠");
    assert_eq!(new_dev.open_attempts(), 0, "不啟動新路徑交接");
    assert!(r.mgr.resolve("t:COM3").is_none(), "沒有正式 session");
    assert_eq!(r.fs.bytes(support::STATE_PATH), state_before, "不落盤退休");
}

// ---------------------------------------------------------------------------
// S2-PAR-wp510r R6（設計 §3.1.7、§1.8 DR-5）：T14 交接凍結下的保管紀錄（ServiceRig 端到端）
// ---------------------------------------------------------------------------

/// DR-5(c)（設計 v3 §1.8、§3.1.7 Request `Release`：admission 拒絕新請求、帳本不受影響、由 shutdown 掃描收尾）：parked B3
/// 綁在 COM1 上（雙 ENOSPC：COM1 已於記憶體改綁、parked 未被取走）時 `daemon.handoff_stop` 成立；之後的 `device.release`
/// 由 RPC admission 回 `FLUSHED_BY_SHUTDOWN`＋`reason:"handoff_frozen"`（逐位元組，Python `rpc()` 的凍結早退，
/// `origin/main:sw_core/service.py:870-874`），parked handle 不關閉、帳本紀錄不變；shutdown 掃描帳本關閉 parked 並配對回條
/// （關閉一次、紀錄移除、終態 Closed、報告已排空）。
#[test]
fn wp510r_r6_rpc_dr5_c_release_rejected_under_handoff_freeze_parked_closed_by_shutdown() {
    let r = wp510_b_placeholder_rig();
    let dev = wp510_review8_double_enospc_bind_setup(&r, true);
    let parked = r.mgr.custody_for_test(WP510_PLACEHOLDER_BY_ID).expect("前置：parked 帳本紀錄");
    assert_eq!(parked.owner, sw_core::custody::CustodyOwner::Parked, "{parked:?}");
    assert_eq!(parked.phase, sw_core::custody::CustodyPhaseSnapshot::Held);
    let token = parked.token;

    let granted = r.svc.handoff_stop(&sw_proto::pydict! { "reason" => "update" });
    assert_eq!(granted.get("ok"), Some(&PyValue::Bool(true)), "{}", j(&granted));
    assert_eq!(
        r.mgr.deliverer_gate_for_test().handoff_freeze_state(),
        sw_core::admission::HandoffFreezeState::Permanent
    );
    let released = r.call("device.release", r#"{"selector":"COM1"}"#);
    assert_eq!(
        j(&released),
        j(&sw_proto::pydict! {
            "ok" => false,
            "error_code" => "FLUSHED_BY_SHUTDOWN",
            "reason" => "handoff_frozen",
            "version" => support::RIG_VERSION,
        }),
        "admission 拒絕凍結後的 release"
    );
    std::thread::sleep(Duration::from_millis(200));
    assert_eq!(dev.close_count(), 0, "release 被拒：parked handle 不關閉");
    assert!(dev.is_open());
    let after = r.mgr.custody_for_test(WP510_PLACEHOLDER_BY_ID).expect("帳本紀錄不變");
    assert_eq!((after.token, after.owner.clone(), after.phase.clone()), (token, parked.owner.clone(), parked.phase.clone()));

    let drained = r.mgr.shutdown(Duration::from_millis(1500));
    assert!(
        drained
            .iter()
            .any(|(target, ok)| target == &format!("attach:{WP510_PLACEHOLDER_BY_ID}") && *ok),
        "shutdown 掃描關閉 parked 並報已排空：{drained:?}"
    );
    assert_eq!(dev.close_count(), 1, "parked handle 由 shutdown 掃描關閉一次");
    assert!(!dev.is_open());
    assert!(r.mgr.custody_for_test(WP510_PLACEHOLDER_BY_ID).is_none(), "配對回條後紀錄移除：{:?} term={:?}", r.mgr.custody_for_test(WP510_PLACEHOLDER_BY_ID), r.mgr.terminal_for_test(token));
    assert_eq!(
        r.mgr.terminal_for_test(token).map(|t| t.handle),
        Some(sw_core::custody::TerminalHandle::Closed)
    );
}

// ---------------------------------------------------------------------------
// S2-PAR-wp510r 第 12 項第 2 輪：審查 P8-add3ca0-1（ServiceRig 端到端）
// ---------------------------------------------------------------------------

/// `state.json` 的 `released` 段是否有 session_id 為 `sid` 的列。
fn wp510r_r6_state_has_released_sid(r: &ServiceRig, sid: &str) -> bool {
    let bytes = r.fs.bytes(support::STATE_PATH);
    let Ok(PyValue::Dict(state)) = sw_proto::pyjson::loads(&String::from_utf8_lossy(&bytes)) else {
        return false;
    };
    matches!(state.get("released"), Some(PyValue::Dict(rows)) if rows.get(sid).is_some())
}

/// 讓 update_devices 先認領 parked handle 並停在 close，首次 release 等滿期限後仍保留晚到完成義務。
fn wp510r_r6_parked_release_wait_after_timeout(
    r: &ServiceRig,
    dev: &FakeDevice,
) -> (std::thread::JoinHandle<()>, String, u64) {
    let token = r
        .mgr
        .custody_for_test(WP510_PLACEHOLDER_BY_ID)
        .expect("前置：parked 帳本紀錄")
        .token;
    let without: Vec<(String, DeviceInfo)> = r
        .mgr
        .list_devices()
        .into_iter()
        .filter_map(|row| {
            let by_id = s(&row, "by_id").to_string();
            let real_path = s(&row, "real_path").to_string();
            (by_id != WP510_PLACEHOLDER_BY_ID)
                .then(|| (by_id.clone(), DeviceInfo { by_id, real_path }))
        })
        .collect();
    dev.pause_next_close();
    let unplug_mgr = r.mgr.clone();
    let unplug = std::thread::spawn(move || unplug_mgr.update_devices(&without));
    assert!(
        dev.wait_paused(Duration::from_secs(5)),
        "拔除執行緒停在 parked handle 關閉"
    );

    let started = Instant::now();
    let stalled = r.call("device.release", r#"{"selector":"COM1"}"#);
    assert_eq!(
        stalled.get("ok"),
        Some(&PyValue::Bool(false)),
        "{}",
        j(&stalled)
    );
    assert_eq!(
        stalled.get("error_code"),
        Some(&PyValue::from("DRAIN_STALLED")),
        "{}",
        j(&stalled)
    );
    assert_eq!(
        stalled.get("drain_stalled"),
        Some(&PyValue::Bool(true)),
        "{}",
        j(&stalled)
    );
    assert!(
        started.elapsed() >= Duration::from_millis(2900),
        "T-1 等滿首次期限：{:?}",
        started.elapsed()
    );
    let sid = s(&r.session("COM1").expect("COM1 session"), "session_id").to_string();
    assert!(!wp510r_r6_state_has_released_sid(r, &sid), "逾時時尚未落盤");
    (unplug, sid, token)
}

/// 審查 P8-add3ca0-1（設計 DR-2 b、D1 ii、T-1）：parked usb-G 綁在 COM1 上（雙 ENOSPC：COM1 已於記憶體改綁、parked 未被
/// 取走）時拔除 usb-G、關閉暫停；`device.release COM1` 經 `Service::rpc` → `SessionManager::route` →
/// actor 的 `ClaimByIdForRelease`（第 15 項起取代 manager 的 `WaitCustodyClose`，登記為該紀錄的 watcher）等滿首次 3 秒期限回
/// DRAIN_STALLED。之後放行關閉（closed:true），熱插執行緒的 `close_session_preopened` 結算紀錄並以 `CustodyTerminal` 通知
/// COM1 的 actor，由其保留的停止待決完成首次 release：不需第二次
/// release 即轉 RELEASED（`released_by` 取首次 release 的 `cli`）、清 `drain_stalled`／`drain_timeout_epoch`、落盤，
/// usb-G 不重開；之後的 `device.release` 回 already_released（Python `_finish_attach_op` 的 release 分支，
/// `origin/main:sw_core/session_manager.py:4340-4363`）。
#[test]
fn wp510r_r6_rpc_p8_add3ca0_1_release_stalled_on_parked_unplug_close_completes_late() {
    let r = wp510_b_placeholder_rig();
    let dev = wp510_review8_double_enospc_bind_setup(&r, false);
    let parked = r.mgr.custody_for_test(WP510_PLACEHOLDER_BY_ID).expect("前置：parked 帳本紀錄");
    assert_eq!(parked.owner, sw_core::custody::CustodyOwner::Parked, "{parked:?}");
    let token = parked.token;
    let without: Vec<(String, DeviceInfo)> = r
        .mgr
        .list_devices()
        .into_iter()
        .filter_map(|row| {
            let by_id = s(&row, "by_id").to_string();
            let real_path = s(&row, "real_path").to_string();
            (by_id != WP510_PLACEHOLDER_BY_ID).then(|| (by_id.clone(), DeviceInfo { by_id, real_path }))
        })
        .collect();
    dev.pause_next_close();
    let unplug_mgr = r.mgr.clone();
    let unplug = std::thread::spawn(move || unplug_mgr.update_devices(&without));
    assert!(dev.wait_paused(Duration::from_secs(5)), "拔除的熱插執行緒停在 parked handle 的關閉");

    let started = Instant::now();
    let stalled = r.call("device.release", r#"{"selector":"COM1"}"#);
    let elapsed = started.elapsed();
    assert_eq!(stalled.get("ok"), Some(&PyValue::Bool(false)), "{}", j(&stalled));
    assert_eq!(stalled.get("error_code"), Some(&PyValue::from("DRAIN_STALLED")), "{}", j(&stalled));
    assert_eq!(stalled.get("drain_stalled"), Some(&PyValue::Bool(true)), "{}", j(&stalled));
    assert!(elapsed >= Duration::from_millis(2900), "T-1：等滿首次 3 秒期限：{elapsed:?}");
    assert!(dev.is_open(), "期限到時關閉仍暫停");
    let com1_sid = s(&r.session("COM1").expect("COM1 session"), "session_id").to_string();
    assert!(!wp510r_r6_state_has_released_sid(&r, &com1_sid), "逾時當下 COM1 未落盤");
    let attempts = dev.open_attempts();

    dev.release_pause();
    unplug.join().expect("拔除的熱插執行緒不應 panic");
    r.wait_state("COM1", "RELEASED");
    let session = r.session("COM1").expect("COM1 session");
    assert_eq!(s(&session, "released_by"), "cli", "{}", j(&session));
    assert_eq!(session.get("drain_stalled"), Some(&PyValue::Bool(false)), "{}", j(&session));
    assert_eq!(session.get("drain_timeout_epoch"), Some(&PyValue::Null), "{}", j(&session));
    wait_until("遲到完成的 RELEASED 落盤", || wp510r_r6_state_has_released_sid(&r, &com1_sid));
    assert_eq!(dev.close_count(), 1, "parked handle 只關閉一次");
    assert!(r.mgr.custody_for_test(WP510_PLACEHOLDER_BY_ID).is_none(), "回條後紀錄移除");
    assert_eq!(
        r.mgr.terminal_for_test(token).map(|t| t.handle),
        Some(sw_core::custody::TerminalHandle::Closed)
    );
    std::thread::sleep(Duration::from_millis(300));
    assert_eq!(dev.open_attempts(), attempts, "release 完成後不自動重開");
    let again = r.call("device.release", r#"{"selector":"COM1"}"#);
    assert_eq!(again.get("ok"), Some(&PyValue::Bool(true)), "{}", j(&again));
    assert_eq!(again.get("already_released"), Some(&PyValue::Bool(true)), "{}", j(&again));
}

/// 第 15 項（擁有者 2026-11-11「wp510r release 等待改為 actor 主導」）共用：讓 COM1 的 actor 停在一筆無關的
/// `session.get_state`（H-actor-hold）；回傳 (放行通道, 查詢執行緒)。
fn wp510r_i15_hold_com1_actor(
    r: &ServiceRig,
) -> (
    crossbeam_channel::Sender<()>,
    std::thread::JoinHandle<Result<PyDict, sw_proto::pycompat::PyErr>>,
) {
    let (actor_paused, actor_resume) = r
        .mgr
        .pause_next_request_for_test("COM1")
        .expect("COM1 actor");
    let query_service = r.svc.clone();
    let query = std::thread::spawn(move || {
        query_service.rpc(
            "session.get_state",
            &sw_proto::pydict! { "selector" => "COM1" },
        )
    });
    actor_paused
        .recv_timeout(Duration::from_secs(5))
        .expect("actor 已停在查詢 dispatch");
    assert!(!query.is_finished(), "查詢仍卡在暫停的 actor");
    (actor_resume, query)
}

/// 第 15 項共用：在背景送 `device.release COM1`，回傳 (結果通道, 執行緒)。
fn wp510r_i15_spawn_release(
    r: &ServiceRig,
) -> (
    crossbeam_channel::Receiver<Result<PyDict, sw_proto::pycompat::PyErr>>,
    std::thread::JoinHandle<()>,
) {
    let service = r.svc.clone();
    let (tx, rx) = crossbeam_channel::bounded(1);
    let handle = std::thread::spawn(move || {
        let result = service.rpc(
            "device.release",
            &sw_proto::pydict! { "selector" => "COM1" },
        );
        let _ = tx.send(result);
    });
    (rx, handle)
}

/// 第 21 項 Medium 1：parked 已被 COM1 接手、尚未開埠時 release；真實關閉通知遇 Full 後由 C4 補收。
/// Python `_finish_attach_op` 在關閉後完成 RELEASED、清 drain 並落盤（origin/main:sw_core/session_manager.py:4335-4364）。
#[test]
fn wp510r_i21_rpc_own_custody_terminal_full_poll_completes_release() {
    let r = Arc::new(wp510_b_placeholder_rig());
    let (bind, bind_go, dev) = wp510_review9_bind_paused_before_device_online(&r);
    let sid = r.mgr.resolve("COM1").expect("COM1");
    let record = r.mgr.custody_for_test(WP510_PLACEHOLDER_BY_ID).expect("COM1 已認領 parked");
    assert_eq!(record.owner, sw_core::custody::CustodyOwner::Session {
        actor: r.mgr.actor_id_for_test(&sid).unwrap(),
    });
    assert!(matches!(record.phase, CustodyPhaseSnapshot::Taken { .. }));
    let attempts = dev.open_attempts();
    let tx_before = dev.tx_len();
    dev.pause_next_close();
    let (release_rx, release) = wp510r_i15_spawn_release(&r);
    assert!(dev.wait_paused(Duration::from_secs(5)), "自身 ClaimForStop 關閉 handle");
    assert_eq!(r.mgr.custody_awaits_for_test(&sid), vec![record.token]);
    let stalled = release_rx.recv_timeout(Duration::from_secs(5)).unwrap().unwrap();
    release.join().unwrap();
    assert_eq!(s(&stalled, "error_code"), "DRAIN_STALLED");

    let (actor_go, query) = wp510r_i15_hold_com1_actor(&r);
    let control = r.mgr.actor_control_for_test(&sid).unwrap();
    while control.try_send(sw_core::mailbox::ControlEvent::ConsoleCountChanged { count: 0 }).is_ok() {}
    assert!(control.is_full(), "actor 暫停、control lane 滿載");
    dev.release_pause();
    wait_until("真實 CustodyTerminal 遇 Full", || r.mgr.custody_terminal_dropped_for_test(record.token));
    assert_eq!(r.mgr.terminal_for_test(record.token).unwrap().handle, sw_core::custody::TerminalHandle::Closed);
    actor_go.send(()).unwrap();
    query.join().unwrap().unwrap();
    bind_go.send(()).unwrap();
    assert_eq!(bind.join().unwrap().get("ok"), Some(&PyValue::Bool(true)));
    assert_eq!(r.mgr.custody_awaits_for_test(&sid), vec![record.token], "補收前保留自身等待");

    r.clock.advance(Duration::from_millis(600));
    r.wait_state("COM1", "RELEASED");
    let session = r.session("COM1").unwrap();
    assert_eq!(session.get("drain_stalled"), Some(&PyValue::Bool(false)));
    assert_eq!(session.get("drain_timeout_epoch"), Some(&PyValue::Null));
    assert!(r.mgr.custody_awaits_for_test(&sid).is_empty(), "自身等待已補收");
    wait_until("RELEASED 落盤", || wp510r_r6_state_has_released_sid(&r, &sid));
    let again = r.call("device.release", r#"{"selector":"COM1"}"#);
    assert_eq!(j(&again), j(&sw_proto::pydict! {
        "ok" => true, "already_released" => true, "session" => session, "version" => r.version.clone(),
    }));
    assert_eq!(dev.close_count(), 1);
    assert_eq!(dev.open_attempts(), attempts);
    assert_eq!(dev.tx_len(), tx_before, "沒有登入 TX");
}

/// 第 23 項 Medium：正式 Raw 開埠成功後、退休提交前離線；關閉通知遇 Full 時必須補收。
/// Python 驗證失敗先同步關閉、清作業，再回 DEVICE_NOT_PRESENT
/// （origin/main:sw_core/session_manager.py:3614-3622、4320-4365）。
#[test]
fn wp510r_i23_rpc_offline_invalid_retire_terminal_full_poll_completes_attach() {
    let r = Arc::new(wp510_b_placeholder_rig());
    let dev = r.device(WP510_REAL_G).clone();
    // 交接 PROBE 與正式 bridge 都會送空行，替身須提供可重複的 shell prompt。
    dev.on_tx_match("^$", Box::new(|_| b"root@t:/# ".to_vec()));
    let state_before = r.fs.bytes(support::STATE_PATH);
    let original: Vec<(String, DeviceInfo)> = r.mgr.list_devices().into_iter().map(|row| {
        let by_id = s(&row, "by_id").to_string();
        let real_path = s(&row, "real_path").to_string();
        (by_id.clone(), DeviceInfo { by_id, real_path })
    }).collect();
    let changed: Vec<_> = original.iter()
        .filter(|(by_id, _)| by_id != WP510_PLACEHOLDER_BY_ID).cloned().collect();
    let (retire_tx, retire_rx) = crossbeam_channel::bounded(1);
    let (retire_go, retire_resume) = crossbeam_channel::bounded(1);
    r.mgr.set_placeholder_retire_pre_commit_hook_for_test(WP510_PLACEHOLDER_SID, Box::new(move || {
        retire_tx.send(()).unwrap();
        retire_resume.recv_timeout(Duration::from_secs(10)).unwrap();
    }));
    let attach_rig = Arc::clone(&r);
    let (attach_tx, attach_rx) = crossbeam_channel::bounded(1);
    let attach = std::thread::spawn(move || {
        attach_tx.send(attach_rig.call("device.attach", &format!(
            r#"{{"selector":"{WP510_PLACEHOLDER_ALIAS}"}}"#,
        ))).unwrap();
    });
    retire_rx.recv_timeout(Duration::from_secs(5)).expect("Raw 開埠後停在 RetirePlaceholder");
    let record = r.mgr.custody_for_test(WP510_PLACEHOLDER_BY_ID).expect("Raw handle 仍在帳本");
    assert!(matches!(record.phase, CustodyPhaseSnapshot::Held));
    assert_eq!(dev.open_count(), 1);
    assert_eq!(dev.tx_len(), 0, "退休前零 TX");

    let control = r.mgr.actor_control_for_test(WP510_PLACEHOLDER_SID).unwrap();
    while control.try_send(sw_core::mailbox::ControlEvent::ConsoleCountChanged { count: 0 }).is_ok() {}
    assert!(control.is_full());
    let watcher_rig = Arc::clone(&r);
    let watcher = std::thread::spawn(move || watcher_rig.mgr.update_devices(&changed));
    // update_devices 已換掉正式裝置表；通知被滿載 lane 擋住，尚未進入 detach_by_id 認領 handle。
    wait_until("watcher 已更新裝置表", || {
        r.mgr.list_devices().iter().find(|row| s(row, "by_id") == WP510_PLACEHOLDER_BY_ID)
            .map(|row| s(row, "real_path")) != Some(WP510_REAL_G)
    });
    assert!(!watcher.is_finished(), "watcher 停在滿載通知，handle 由退休驗證失敗分支關閉");
    assert_eq!(r.mgr.custody_for_test(WP510_PLACEHOLDER_BY_ID).unwrap().token, record.token);

    // RetireResult 是立即回饋；下一筆 request 暫停時，它已登記 ClosingInvalid 與 retired drain。
    let (actor_paused, actor_go) = r.mgr.pause_next_request_for_test(WP510_PLACEHOLDER_SID).unwrap();
    let query_service = r.svc.clone();
    let query = std::thread::spawn(move || query_service.rpc(
        "session.get_state", &sw_proto::pydict! { "selector" => WP510_PLACEHOLDER_SID },
    ));
    dev.pause_next_close();
    retire_go.send(()).unwrap();
    actor_paused.recv_timeout(Duration::from_secs(5)).expect("退休驗證失敗後 actor 暫停");
    assert!(dev.wait_paused(Duration::from_secs(5)), "retire-invalid closer 已啟動");
    let closing = r.mgr.custody_for_test(WP510_PLACEHOLDER_BY_ID).unwrap();
    assert_eq!(closing.token, record.token);
    assert_eq!(closing.phase, CustodyPhaseSnapshot::Closing { job: "retire-invalid".into() });
    while control.try_send(sw_core::mailbox::ControlEvent::ConsoleCountChanged { count: 0 }).is_ok() {}
    assert!(control.is_full(), "關閉完成時 control queue 滿載");
    dev.release_pause();
    wait_until("真實 CustodyTerminal 遇 Full", || r.mgr.custody_terminal_dropped_for_test(record.token));
    assert_eq!(r.mgr.terminal_for_test(record.token).unwrap().handle, sw_core::custody::TerminalHandle::Closed);
    assert!(!dev.is_open());
    assert!(attach_rx.try_recv().is_err(), "通知未補收前 attach 不回覆");
    actor_go.send(()).unwrap();
    query.join().unwrap().unwrap();
    watcher.join().unwrap();
    assert_eq!(r.mgr.custody_awaits_for_test(WP510_PLACEHOLDER_SID), vec![record.token]);

    r.clock.advance(Duration::from_millis(600));
    let response = attach_rx.recv_timeout(Duration::from_secs(2)).expect("補收後 attach 應完成 DEVICE_NOT_PRESENT");
    attach.join().unwrap();
    assert_eq!(j(&response), j(&sw_proto::pydict! {
        "ok" => false, "error_code" => "DEVICE_NOT_PRESENT", "selector" => WP510_PLACEHOLDER_ALIAS,
        "device_by_id" => WP510_PLACEHOLDER_BY_ID, "version" => r.version.clone(),
    }), "錯誤形狀與原 selector 依 Python 逐位元組比對");
    assert_eq!(r.mgr.released_placeholder(WP510_PLACEHOLDER_SID), Some(true));
    let session = r.session(WP510_PLACEHOLDER_SID).unwrap();
    assert_eq!(s(&session, "state"), "RELEASED");
    assert_eq!(session.get("drain_stalled"), Some(&PyValue::Bool(false)));
    assert_eq!(session.get("drain_timeout_epoch"), Some(&PyValue::Null));
    assert!(r.mgr.custody_for_test(WP510_PLACEHOLDER_BY_ID).is_none(), "帳本已清除");
    assert!(r.mgr.custody_awaits_for_test(WP510_PLACEHOLDER_SID).is_empty(), "等待已清除");
    assert!(r.mgr.retired_drains_for_test(WP510_PLACEHOLDER_SID).is_empty());
    assert!(!r.mgr.has_inflight_attach_for_test(WP510_PLACEHOLDER_BY_ID));
    assert_eq!(r.mgr.prepared_len_for_test(), 0);
    assert_eq!(r.fs.bytes(support::STATE_PATH), state_before, "佔位未退休、不落盤");
    assert_eq!(dev.open_count(), 1);
    assert_eq!(dev.close_count(), 1);
    assert_eq!(dev.tx_len(), 0, "失效交接零 TX");

    r.mgr.update_devices(&original);
    let retry = r.call("device.attach", r#"{"selector":"COM3"}"#);
    assert_eq!(retry.get("ok"), Some(&PyValue::Bool(true)), "恢復後可再次 attach：{}", j(&retry));
    assert_eq!(s(&sub(&retry, "session"), "state"), "ATTACHING");
    r.wait_state("t:COM3", "READY");
    assert!(r.mgr.resolve(WP510_PLACEHOLDER_SID).is_none(), "重試成功才退休佔位");
    assert_eq!(dev.open_count(), 2, "重試只另開一次");
    assert!(r.mgr.custody_for_test(WP510_PLACEHOLDER_BY_ID).is_none(), "正式 actor 已確認接手");
}

/// 第 24 項 High：watcher 先拿走 Raw handle 並停在 close，退休失敗與已入列的 release 都必須等配對回條。
/// Python release 等 attach 作業 done，關閉失敗不設 done（origin/main:sw_core/session_manager.py:3188-3209、4320-4364）。
fn wp510r_i24_watcher_closing(late: bool, failed: bool) {
    let r = Arc::new(wp510_b_placeholder_rig());
    let dev = r.device(WP510_REAL_G).clone();
    let state_before = r.fs.bytes(support::STATE_PATH);
    let original: Vec<(String, DeviceInfo)> = r.mgr.list_devices().into_iter().map(|row| {
        let by_id = s(&row, "by_id").to_string();
        let real_path = s(&row, "real_path").to_string();
        (by_id.clone(), DeviceInfo { by_id, real_path })
    }).collect();
    let changed: Vec<_> = original.into_iter()
        .filter(|(by_id, _)| by_id != WP510_PLACEHOLDER_BY_ID).collect();
    let (retire_tx, retire_rx) = crossbeam_channel::bounded(1);
    let (retire_go, retire_resume) = crossbeam_channel::bounded(1);
    r.mgr.set_placeholder_retire_pre_commit_hook_for_test(WP510_PLACEHOLDER_SID, Box::new(move || {
        retire_tx.send(()).unwrap();
        retire_resume.recv_timeout(Duration::from_secs(15)).unwrap();
    }));
    let attach_rig = Arc::clone(&r);
    let (attach_tx, attach_rx) = crossbeam_channel::bounded(1);
    let attach = std::thread::spawn(move || {
        attach_tx.send(attach_rig.call("device.attach", &format!(
            r#"{{"selector":"{WP510_PLACEHOLDER_ALIAS}"}}"#,
        ))).unwrap();
    });
    retire_rx.recv_timeout(Duration::from_secs(5)).unwrap();
    let record = r.mgr.custody_for_test(WP510_PLACEHOLDER_BY_ID).unwrap();
    assert!(matches!(record.phase, CustodyPhaseSnapshot::Held));

    // release 先通過 manager pump 並排入 safety lane，才讓 watcher 把 handle 接走。
    let safety = r.mgr.safety_receiver_for_test(WP510_PLACEHOLDER_SID).unwrap();
    let release_rig = Arc::clone(&r);
    let (release_tx, release_rx) = crossbeam_channel::bounded(1);
    let release = std::thread::spawn(move || {
        let started = Instant::now();
        let result = release_rig.call("device.release", r#"{"selector":"COM3"}"#);
        release_tx.send((result, started.elapsed())).unwrap();
    });
    wait_until("release 已入 actor safety lane", || !safety.is_empty());
    if failed {
        dev.fail_close(PalError::plain(PalErrorKind::Io, "i24 close failed"));
    }
    dev.pause_next_close();
    let watcher_rig = Arc::clone(&r);
    let watcher = std::thread::spawn(move || watcher_rig.mgr.update_devices(&changed));
    assert!(dev.wait_paused(Duration::from_secs(5)), "watcher 已拿走 handle、尚未有回條");
    let closing = r.mgr.custody_for_test(WP510_PLACEHOLDER_BY_ID).unwrap();
    assert_eq!(closing.token, record.token);
    assert!(matches!(closing.phase, CustodyPhaseSnapshot::Closing { .. }));
    assert_eq!(closing.owner, record.owner, "熱插關閉仍屬原 actor，不得因 owner 略過等待");
    retire_go.send(()).unwrap();
    // 紅燈也先完整放行／join，避免遺留 throwaway 工作者。
    let early_release = release_rx.recv_timeout(Duration::from_millis(200)).ok();
    let early_attach = attach_rx.try_recv().ok();
    let timed = if late || failed {
        early_release.clone().or_else(|| release_rx.recv_timeout(Duration::from_secs(5)).ok())
    } else { None };
    dev.release_pause();
    watcher.join().unwrap();
    let response = early_attach.clone().or_else(|| attach_rx.recv_timeout(Duration::from_secs(5)).ok()).unwrap();
    attach.join().unwrap();
    let (released, elapsed) = timed.or(early_release.clone())
        .or_else(|| release_rx.recv_timeout(Duration::from_secs(5)).ok()).unwrap();
    release.join().unwrap();
    assert!(early_release.is_none(), "回條前 release 不得成功：{:?}", early_release.map(|(r, _)| j(&r)));
    assert!(early_attach.is_none(), "退休失敗也須等帳本終態");
    assert_eq!(j(&response), j(&sw_proto::pydict! {
        "ok" => false, "error_code" => "DEVICE_NOT_PRESENT", "selector" => WP510_PLACEHOLDER_ALIAS,
        "device_by_id" => WP510_PLACEHOLDER_BY_ID, "version" => r.version.clone(),
    }));
    if late || failed {
        assert_eq!(s(&released, "error_code"), "DRAIN_STALLED");
        assert!(elapsed >= Duration::from_millis(2500) && elapsed < Duration::from_secs(5), "首次期限：{elapsed:?}");
        assert_eq!(released.get("drain_stalled"), Some(&PyValue::Bool(true)));
    } else {
        assert_eq!(released.get("ok"), Some(&PyValue::Bool(true)));
        assert_eq!(released.get("already_released"), Some(&PyValue::Bool(true)));
    }
    assert_eq!(dev.open_count(), 1);
    assert_eq!(dev.close_count(), 1);
    assert_eq!(dev.tx_len(), 0);
    assert_eq!(r.mgr.prepared_len_for_test(), 0);
    if failed {
        assert!(r.mgr.unconfirmed_close_for_test(WP510_PLACEHOLDER_BY_ID));
        let started = Instant::now();
        assert_eq!(s(&r.call("device.release", r#"{"selector":"COM3"}"#), "error_code"), "DRAIN_STALLED");
        assert!(started.elapsed() < Duration::from_secs(1), "後續 release 沿用已過的首次期限");
        assert_eq!(r.fs.bytes(support::STATE_PATH), state_before, "失敗不落盤");
    } else {
        wait_until("成功回條收斂所有等待", || r.mgr.custody_awaits_for_test(WP510_PLACEHOLDER_SID).is_empty());
        assert!(r.mgr.custody_for_test(WP510_PLACEHOLDER_BY_ID).is_none());
        let again = r.call("device.release", r#"{"selector":"COM3"}"#);
        assert_eq!(again.get("ok"), Some(&PyValue::Bool(true)));
        assert_eq!(again.get("already_released"), Some(&PyValue::Bool(true)));
    }
}

#[test]
fn wp510r_i24_rpc_watcher_closing_retire_release_waits_success() {
    wp510r_i24_watcher_closing(false, false);
}

#[test]
fn wp510r_i24_rpc_watcher_closing_retire_release_times_out_then_converges() {
    wp510r_i24_watcher_closing(true, false);
}

#[test]
fn wp510r_i24_rpc_watcher_closing_retire_release_failure_keeps_first_deadline() {
    wp510r_i24_watcher_closing(false, true);
}

/// P8-5f67955-1：拔除後重插的正常 OpenBridge 已提交，重複 attach 只能觀察，不能取消。
/// 對照組不插入重複 RPC；兩組都要求 handle 保留，不把 READY 當成本項驗收。
fn wp510r_i24_rpc_observer_attach_during_hotplug(duplicate: bool, release: bool) {
    let r = Arc::new(ServiceRig::ready_shell());
    let dev = r.device(REAL_A).clone();
    let sid = r.mgr.resolve("COM0").unwrap();
    r.mgr.update_devices(&[]);
    r.wait_state("COM0", "DETACHED");
    wait_until("舊 bridge 已關閉", || !dev.is_open());
    let attempts = dev.open_attempts();
    let closes = dev.close_count();
    let (entered, opened) = crossbeam_channel::bounded(1);
    let (go, resume) = crossbeam_channel::bounded(1);
    r.mgr.set_open_job_post_commit_hook_for_test(&sid, Box::new(move || {
        entered.send(()).unwrap();
        resume.recv_timeout(Duration::from_secs(10)).unwrap();
    }));
    r.mgr.update_devices(&[(BY_ID_A.into(), DeviceInfo { by_id: BY_ID_A.into(), real_path: REAL_A.into() })]);
    opened.recv_timeout(Duration::from_secs(5)).unwrap();
    let record = r.mgr.custody_for_test(BY_ID_A).unwrap();
    assert!(matches!(record.phase, CustodyPhaseSnapshot::Bridged { worker_in_ledger: true, .. }));
    let (reply_tx, reply_rx) = crossbeam_channel::bounded(1);
    let attached = duplicate.then(|| {
        let rig = Arc::clone(&r);
        std::thread::spawn(move || reply_tx.send(rig.call("device.attach", r#"{"selector":"COM0"}"#)).unwrap())
    });
    if duplicate {
        wait_until("重複 attach 已登記觀察等待", || r.mgr.custody_attach_observers_for_test(&sid) == vec![record.token]);
        assert!(reply_rx.try_recv().is_err(), "接手確認前不回覆成功");
        assert_eq!(r.mgr.custody_for_test(BY_ID_A).unwrap().cancel, None, "純觀察不寫取消");
    }
    let (release_tx, release_rx) = crossbeam_channel::bounded(1);
    let released = release.then(|| {
        let rig = Arc::clone(&r);
        std::thread::spawn(move || release_tx.send(rig.call("device.release", r#"{"selector":"COM0"}"#)).unwrap())
    });
    if release {
        wait_until("release 已取消同 token 並登記真正關閉義務", || {
            r.mgr.custody_for_test(BY_ID_A).is_some_and(|rec| {
                rec.token == record.token && rec.cancel == Some(sw_core::custody::CustodyCancel::Release)
            }) && r.mgr.custody_awaits_for_test(&sid).contains(&record.token)
        });
        dev.pause_next_close();
    }
    go.send(()).unwrap();
    if release {
        assert!(dev.wait_paused(Duration::from_secs(5)));
        assert!(release_rx.recv_timeout(Duration::from_millis(100)).is_err(), "觀察等待不能替代配對關閉回條");
        dev.release_pause();
        assert_eq!(release_rx.recv_timeout(Duration::from_secs(5)).unwrap().get("ok"), Some(&PyValue::Bool(true)));
        released.unwrap().join().unwrap();
        r.wait_state("COM0", "RELEASED");
    }
    wait_until("帳本已完成接手或關閉", || r.mgr.custody_for_test(BY_ID_A).is_none());
    // 接手後讓既有 CustodyPoll 補收；這是 FakeClock，不以睡眠猜測 actor 順序。
    r.clock.advance(Duration::from_millis(600));
    if let Some(attached) = attached {
        let response = reply_rx.recv_timeout(Duration::from_secs(5)).unwrap();
        attached.join().unwrap();
        let session = sub(&response, "session");
        assert_eq!(j(&response), j(&sw_proto::pydict! { "ok" => true, "session" => session, "version" => r.version.clone() }));
        assert!(r.mgr.custody_attach_observers_for_test(&sid).is_empty(), "等待回覆正常完成");
    }
    let session = r.session("COM0").unwrap();
    assert_eq!(dev.is_open(), !release, "重複 attach 不得關閉有效新 bridge");
    assert_eq!(dev.open_attempts(), attempts + 1, "沒有額外重開");
    assert_eq!(dev.close_count(), closes + usize::from(release), "只有真正 release 才新增 close");
    assert_eq!(session.get("attached_real_path"), Some(&if release { PyValue::Null } else { PyValue::from(REAL_A) }));
    assert!(r.mgr.custody_awaits_for_test(&sid).is_empty());
    assert!(r.mgr.retired_drains_for_test(&sid).is_empty());
}

#[test]
fn wp510r_i24_rpc_observer_duplicate_attach_keeps_hotplug_bridge() {
    wp510r_i24_rpc_observer_attach_during_hotplug(true, false);
}

#[test]
fn wp510r_i24_rpc_observer_hotplug_without_duplicate_keeps_bridge() {
    wp510r_i24_rpc_observer_attach_during_hotplug(false, false);
}

#[test]
fn wp510r_i24_rpc_observer_then_release_still_waits_close_receipt() {
    wp510r_i24_rpc_observer_attach_during_hotplug(true, true);
}

/// 安全網接到一般 OpenBridge 的 token 時，也必須把義務原子換成真實 epoch 回條，不能等待 ack 的假關閉。
#[test]
fn wp510r_i24_rpc_plain_bridged_release_waits_receipt() {
    let r = Arc::new(ServiceRig::ready_shell());
    let dev = r.device(REAL_A).clone();
    assert_eq!(r.call("device.release", r#"{"selector":"COM0"}"#).get("ok"), Some(&PyValue::Bool(true)));
    let sid = r.mgr.resolve("COM0").unwrap();
    let (entered, opened) = crossbeam_channel::bounded(1);
    let (go, resume) = crossbeam_channel::bounded(1);
    r.mgr.set_open_job_post_commit_hook_for_test(&sid, Box::new(move || {
        entered.send(()).unwrap();
        resume.recv_timeout(Duration::from_secs(10)).unwrap();
    }));
    let attach_rig = Arc::clone(&r);
    let attach = std::thread::spawn(move || attach_rig.call("device.attach", r#"{"selector":"COM0"}"#));
    opened.recv_timeout(Duration::from_secs(5)).unwrap();
    let record = r.mgr.custody_for_test(BY_ID_A).unwrap();
    let epoch = match record.phase {
        CustodyPhaseSnapshot::Bridged { epoch, worker_in_ledger: true } => epoch,
        other => panic!("帳本應持有真實 worker：{other:?}"),
    };
    let attempts = dev.open_attempts();
    let tx_before = dev.tx_len();
    let release_rig = Arc::clone(&r);
    let (released, release_rx) = crossbeam_channel::bounded(1);
    let release = std::thread::spawn(move || released.send(release_rig.call("device.release", r#"{"selector":"COM0"}"#)).unwrap());
    wait_until("release 已認領一般 Bridged token", || r.mgr.custody_awaits_for_test(&sid).contains(&record.token));
    dev.pause_next_close();
    go.send(()).unwrap();
    assert!(dev.wait_paused(Duration::from_secs(5)));
    assert!(release_rx.recv_timeout(Duration::from_millis(100)).is_err(), "真實關閉回條前不得回覆");
    let pending_epochs = r.mgr.retired_drains_for_test(&sid);
    let pending_tokens = r.mgr.custody_awaits_for_test(&sid);
    dev.release_pause();
    let result = release_rx.recv_timeout(Duration::from_secs(5)).unwrap();
    release.join().unwrap();
    // 紅燈也讓補收計時器收完舊 token，避免原 attach caller 留在 join。
    r.clock.advance(Duration::from_millis(600));
    let attached = attach.join().unwrap();
    assert_eq!(pending_epochs, vec![epoch], "一般開埠也以配對 epoch 承接責任");
    assert!(pending_tokens.is_empty(), "移交排空後不再等 ack tombstone 的假關閉");
    assert_eq!(s(&attached, "error_code"), "SESSION_RECOVERING");
    let session = r.session("COM0").unwrap();
    assert_eq!(j(&result), j(&sw_proto::pydict! { "ok" => true, "already_released" => true, "session" => session.clone(), "version" => r.version.clone() }));
    assert_eq!(s(&session, "state"), "RELEASED");
    assert_eq!(session.get("drain_stalled"), Some(&PyValue::Bool(false)));
    assert_eq!(session.get("drain_timeout_epoch"), Some(&PyValue::Null));
    assert!(r.mgr.custody_for_test(BY_ID_A).is_none());
    assert_eq!(dev.open_attempts(), attempts);
    assert_eq!(dev.tx_len(), tx_before, "release 取消後零新增 TX");
    assert!(!dev.is_open());
}

/// 第 21 項 Medium 2：一般 OpenBridge 已提交 Bridged，bind 必須取消並等排空；期限前成功才改綁。
/// Python bind 取消在途並等 done 後重試（origin/main:sw_core/session_manager.py:3676-3727、3757-3813）。
fn wp510r_i21_rpc_bind_bridged(timeout: bool) {
    const TARGET: &str = "/s/by-id/i21-offline";
    let r = Arc::new(ServiceRig::ready_shell());
    let dev = r.device(REAL_A).clone();
    assert_eq!(r.call("device.release", r#"{"selector":"COM0"}"#).get("ok"), Some(&PyValue::Bool(true)));
    let sid = r.mgr.resolve("COM0").unwrap();
    let (entered_tx, entered_rx) = crossbeam_channel::bounded(1);
    let (go_tx, go_rx) = crossbeam_channel::bounded(1);
    r.mgr.set_open_job_post_commit_hook_for_test(&sid, Box::new(move || {
        entered_tx.send(()).unwrap();
        go_rx.recv_timeout(Duration::from_secs(15)).unwrap();
    }));
    let attach_rig = Arc::clone(&r);
    let attach = std::thread::spawn(move || attach_rig.call("device.attach", r#"{"selector":"COM0"}"#));
    entered_rx.recv_timeout(Duration::from_secs(5)).unwrap();
    let bridged = r.mgr.custody_for_test(BY_ID_A).unwrap();
    let epoch = match bridged.phase {
        CustodyPhaseSnapshot::Bridged { epoch, worker_in_ledger: true } => epoch,
        other => panic!("前置必須是帳本持有 IoWorker：{other:?}"),
    };
    let attempts = dev.open_attempts();
    let closes = dev.close_count();
    let tx_before = dev.tx_len();
    let bind_rig = Arc::clone(&r);
    let (bind_tx, bind_rx) = crossbeam_channel::bounded(1);
    let bind = std::thread::spawn(move || {
        let started = Instant::now();
        let result = bind_rig.call("session.bind", &format!(r#"{{"selector":"COM0","device_by_id":"{TARGET}"}}"#));
        bind_tx.send((result, started.elapsed())).unwrap();
    });
    // 保留舊版的早回覆以便先清理工作者，再讓核心斷言報出紅燈。
    let early = bind_rx.recv_timeout(Duration::from_millis(300)).ok();
    let cancelled = r.mgr.custody_for_test(BY_ID_A).and_then(|rec| rec.cancel);
    let timed = if timeout {
        early.clone().or_else(|| bind_rx.recv_timeout(Duration::from_secs(5)).ok())
    } else {
        None
    };
    dev.pause_next_close();
    go_tx.send(()).unwrap();
    let paused = dev.wait_paused(Duration::from_secs(2));
    let retiring = r.mgr.retired_drains_for_test(&sid);
    let before_receipt = if early.is_none() && !timeout { bind_rx.try_recv().ok() } else { None };
    dev.release_pause();
    let (bound, elapsed) = timed.or(early.clone()).or(before_receipt.clone())
        .or_else(|| bind_rx.recv_timeout(Duration::from_secs(5)).ok()).expect("bind 回覆");
    bind.join().unwrap();
    let attached = attach.join().unwrap();
    assert!(early.is_none(), "Bridged bind 不得立即拒絕：{:?}", early.as_ref().map(|(r, _)| j(r)));
    assert_eq!(cancelled, Some(sw_core::custody::CustodyCancel::Bind), "同 token 寫入取消");
    assert!(paused, "actor 收到 BridgeOpened 後只排空、不登入");
    assert_eq!(retiring, vec![epoch], "配對關閉回條前仍保留該 epoch 的等待");
    assert!(before_receipt.is_none(), "配對回條前不得改綁");
    assert_eq!(s(&attached, "error_code"), "SESSION_RECOVERING", "原 attach 已取消");
    wait_until("遲到 worker 已關閉", || dev.close_count() == closes + 1 && !dev.is_open());
    wait_until("配對退休回條已結算", || r.mgr.retired_drains_for_test(&sid).is_empty());
    assert_eq!(dev.close_count(), closes + 1, "配對 epoch={epoch} 的 worker 已關閉");
    assert!(!dev.is_open());
    assert!(r.mgr.custody_for_test(BY_ID_A).is_none());
    assert!(r.mgr.retired_drains_for_test(&sid).is_empty(), "配對回條後退休排空收斂");
    assert_eq!(dev.open_attempts(), attempts, "不另開舊埠");
    assert_eq!(dev.tx_len(), tx_before, "取消後沒有登入 TX");
    if timeout {
        assert!(elapsed >= Duration::from_millis(2500) && elapsed < Duration::from_secs(5), "BindWait 期限：{elapsed:?}");
        assert_eq!(s(&bound, "error_code"), "SESSION_RECOVERING");
        assert_eq!(bound.get("drain_stalled"), Some(&PyValue::Bool(true)));
        assert_eq!(bound.get("drain_timeout_epoch"), sub(&bound, "session").get("bridge_generation"));
        assert_eq!(s(&sub(&bound, "session"), "state"), "RECOVERING");
        assert_eq!(s(&r.session("COM0").unwrap(), "device_by_id"), BY_ID_A, "逾時後不套用改綁");
    } else {
        let session = r.session("COM0").unwrap();
        assert_eq!(j(&bound), j(&sw_proto::pydict! { "ok" => true, "session" => session.clone(), "version" => r.version.clone() }));
        assert_eq!(s(&session, "device_by_id"), TARGET);
        assert_eq!(s(&session, "state"), "DETACHED");
        let state = String::from_utf8(r.fs.bytes(support::STATE_PATH)).unwrap();
        assert!(state.contains(TARGET), "改綁已落盤");
    }
}

#[test]
fn wp510r_i21_rpc_bind_bridged_waits_close_then_applies() {
    wp510r_i21_rpc_bind_bridged(false);
}

#[test]
fn wp510r_i21_rpc_bind_bridged_timeout_cancels_late_login() {
    wp510r_i21_rpc_bind_bridged(true);
}

/// P8-e385ed0-1：Bridged 的 bind／release 共用在途關閉，遲到回條仍須走 attach 作業的 release 收尾。
/// Python `_finish_attach_op` 在關閉後恢復 RELEASED、清 drain、落盤（origin/main:sw_core/session_manager.py:4339-4364）。
fn wp510r_i21_rpc_bind_release_bridged(late: bool) {
    const TARGET: &str = "/s/by-id/i21-release-offline";
    let r = Arc::new(ServiceRig::ready_shell());
    let dev = r.device(REAL_A).clone();
    assert_eq!(r.call("device.release", r#"{"selector":"COM0"}"#).get("ok"), Some(&PyValue::Bool(true)));
    let sid = r.mgr.resolve("COM0").unwrap();
    let initial = r.session("COM0").unwrap();
    let writes_before = r.fs.writes();
    let (entered_tx, entered_rx) = crossbeam_channel::bounded(1);
    let (go_tx, go_rx) = crossbeam_channel::bounded(1);
    r.mgr.set_open_job_post_commit_hook_for_test(&sid, Box::new(move || {
        entered_tx.send(()).unwrap();
        go_rx.recv_timeout(Duration::from_secs(15)).unwrap();
    }));
    let attach_rig = Arc::clone(&r);
    let attach = std::thread::spawn(move || attach_rig.call("device.attach", r#"{"selector":"COM0"}"#));
    entered_rx.recv_timeout(Duration::from_secs(5)).unwrap();
    let bridged = r.mgr.custody_for_test(BY_ID_A).unwrap();
    let epoch = match bridged.phase {
        CustodyPhaseSnapshot::Bridged { epoch, worker_in_ledger: true } => epoch,
        other => panic!("前置必須是帳本持有 IoWorker：{other:?}"),
    };
    let attempts = dev.open_attempts();
    let closes = dev.close_count();
    let tx_before = dev.tx_len();
    let bind_rig = Arc::clone(&r);
    let (bind_tx, bind_rx) = crossbeam_channel::bounded(1);
    let bind = std::thread::spawn(move || {
        let started = Instant::now();
        let result = bind_rig.call("session.bind", &format!(r#"{{"selector":"COM0","device_by_id":"{TARGET}"}}"#));
        bind_tx.send((result, started.elapsed())).unwrap();
    });
    wait_until("Bind 取消已寫入原 token", || {
        r.mgr.custody_for_test(BY_ID_A).is_some_and(|rec| {
            rec.token == bridged.token && rec.cancel == Some(sw_core::custody::CustodyCancel::Bind)
        }) && r.mgr.custody_awaits_for_test(&sid).contains(&bridged.token)
    });
    let (release_wait_tx, release_wait_rx) = crossbeam_channel::bounded(1);
    r.mgr.set_release_wait_created_hook_for_test(&sid, Box::new(move || {
        release_wait_tx.send(()).unwrap();
    }));
    let release_rig = Arc::clone(&r);
    let (release_tx, release_rx) = crossbeam_channel::bounded(1);
    let release = std::thread::spawn(move || {
        let started = Instant::now();
        let result = release_rig.call("device.release", r#"{"selector":"COM0"}"#);
        release_tx.send((result, started.elapsed())).unwrap();
    });
    release_wait_rx.recv_timeout(Duration::from_secs(2)).unwrap();
    let timed = if late {
        let bound = bind_rx.recv_timeout(Duration::from_secs(5)).unwrap();
        let released = release_rx.recv_timeout(Duration::from_secs(5)).unwrap();
        Some((bound, released))
    } else {
        assert!(bind_rx.recv_timeout(Duration::from_millis(100)).is_err(), "bind 等配對關閉回條");
        assert!(release_rx.recv_timeout(Duration::from_millis(100)).is_err(), "release 等配對關閉回條");
        None
    };
    assert_eq!(r.fs.writes(), writes_before, "關閉前不落盤");
    dev.pause_next_close();
    go_tx.send(()).unwrap();
    let paused = dev.wait_paused(Duration::from_secs(2));
    let retiring = r.mgr.retired_drains_for_test(&sid);
    let fence_pending = r.mgr.fence_for_test("COM0").unwrap().with(|f| f.retired_drains_pending);
    dev.release_pause();
    let (bound, released) = timed.unwrap_or_else(|| (
        bind_rx.recv_timeout(Duration::from_secs(5)).unwrap(),
        release_rx.recv_timeout(Duration::from_secs(5)).unwrap(),
    ));
    bind.join().unwrap();
    release.join().unwrap();
    let attached = attach.join().unwrap();
    assert!(paused, "BridgeOpened 後只排空、不登入");
    assert_eq!(s(&attached, "error_code"), "SESSION_RECOVERING", "原 attach 已取消");
    assert_eq!(s(&bound.0, "error_code"), "SESSION_RECOVERING", "release 加入後不得改綁");
    if late {
        assert!(bound.1 >= Duration::from_millis(2500) && bound.1 < Duration::from_secs(5), "BindWait：{:?}", bound.1);
        assert!(released.1 >= Duration::from_millis(2500) && released.1 < Duration::from_secs(5), "ReleaseWait：{:?}", released.1);
        for response in [&bound.0, &released.0] {
            assert_eq!(response.get("drain_stalled"), Some(&PyValue::Bool(true)));
            assert_eq!(response.get("drain_timeout_epoch"), initial.get("bridge_generation"));
        }
        assert_eq!(s(&released.0, "error_code"), "DRAIN_STALLED");
    } else {
        assert_eq!(released.0.get("ok"), Some(&PyValue::Bool(true)));
        assert_eq!(released.0.get("already_released"), Some(&PyValue::Bool(true)));
    }
    wait_until("配對關閉回條已結算", || {
        dev.close_count() == closes + 1 && !dev.is_open()
            && r.mgr.retired_drains_for_test(&sid).is_empty()
    });
    let session = r.session("COM0").unwrap();
    assert_eq!(s(&session, "state"), "RELEASED", "遲到成功必須完成在途 release");
    assert_eq!(session.get("drain_stalled"), Some(&PyValue::Bool(false)));
    assert_eq!(session.get("drain_timeout_epoch"), Some(&PyValue::Null));
    assert!(!r.mgr.fence_for_test("COM0").unwrap().with(|f| f.retired_drains_pending), "退休等待鏡像同步清除");
    assert_eq!(retiring, vec![epoch], "token 等待必須換成同資源的 epoch 等待");
    assert!(fence_pending, "配對回條前的退休等待鏡像為 true");
    assert_eq!(s(&session, "device_by_id"), BY_ID_A, "release 介入後不套用改綁");
    assert_eq!(session.get("released_by"), initial.get("released_by"));
    assert_eq!(session.get("released_at"), initial.get("released_at"));
    assert!(r.fs.writes() > writes_before, "在途 release 收尾已重新落盤");
    assert!(wp510r_r6_state_has_released_sid(&r, &sid));
    assert!(r.mgr.custody_for_test(BY_ID_A).is_none());
    assert!(r.mgr.custody_awaits_for_test(&sid).is_empty());
    assert_eq!(dev.close_count(), closes + 1);
    assert_eq!(dev.open_attempts(), attempts, "不另開舊埠");
    assert_eq!(dev.tx_len(), tx_before, "取消後沒有登入 TX");
    assert_eq!(j(&r.call("device.release", r#"{"selector":"COM0"}"#)), j(&sw_proto::pydict! {
        "ok" => true, "already_released" => true, "session" => session, "version" => r.version.clone(),
    }));
}

#[test]
fn wp510r_i21_rpc_bind_release_bridged_before_deadlines() {
    wp510r_i21_rpc_bind_release_bridged(false);
}

#[test]
fn wp510r_i21_rpc_bind_release_bridged_after_both_deadlines() {
    wp510r_i21_rpc_bind_release_bridged(true);
}

/// P8-22acfbb-1：shutdown 已讀到 RELEASED 時，token 換成 epoch 的窗口與阻塞 close 都不能漏列。
/// Python 在同一把鎖下追蹤 attach bridge，並等配對關閉終態（origin/main:sw_core/session_manager.py:1181-1239）。
fn wp510r_i22_rpc_shutdown_during_token_epoch_replacement(timeout: bool) {
    let r = Arc::new(ServiceRig::ready_shell());
    let dev = r.device(REAL_A).clone();
    assert_eq!(r.call("device.release", r#"{"selector":"COM0"}"#).get("ok"), Some(&PyValue::Bool(true)));
    let sid = r.mgr.resolve("COM0").unwrap();
    let fence = r.mgr.fence_for_test("COM0").unwrap();
    let control = r.mgr.actor_control_for_test(&sid).unwrap();
    let (opened_tx, opened_rx) = crossbeam_channel::bounded(1);
    let (open_go_tx, open_go_rx) = crossbeam_channel::bounded(1);
    r.mgr.set_open_job_post_commit_hook_for_test(&sid, Box::new(move || {
        opened_tx.send(()).unwrap();
        open_go_rx.recv_timeout(Duration::from_secs(10)).unwrap();
    }));
    let attach_rig = Arc::clone(&r);
    let attach = std::thread::spawn(move || attach_rig.call("device.attach", r#"{"selector":"COM0"}"#));
    opened_rx.recv_timeout(Duration::from_secs(5)).unwrap();
    let record = r.mgr.custody_for_test(BY_ID_A).unwrap();
    let epoch = match record.phase {
        CustodyPhaseSnapshot::Bridged { epoch, worker_in_ledger: true } => epoch,
        other => panic!("前置必須是帳本持有 IoWorker：{other:?}"),
    };
    let attempts = dev.open_attempts();
    let closes = dev.close_count();
    let tx_before = dev.tx_len();
    let bind_rig = Arc::clone(&r);
    let bind = std::thread::spawn(move || bind_rig.call(
        "session.bind", r#"{"selector":"COM0","device_by_id":"/s/by-id/i22-offline"}"#,
    ));
    wait_until("bind 已取消並等待原 token", || {
        r.mgr.custody_for_test(BY_ID_A).is_some_and(|rec| {
            rec.token == record.token && rec.cancel == Some(sw_core::custody::CustodyCancel::Bind)
        }) && r.mgr.custody_awaits_for_test(&sid) == vec![record.token]
    });
    assert_eq!(s(&r.session("COM0").unwrap(), "state"), "RELEASED");
    assert!(fence.with(|f| f.retired_drains_pending));

    let (snapshot_tx, snapshot_rx) = crossbeam_channel::bounded(1);
    let (check_go_tx, check_go_rx) = crossbeam_channel::bounded(1);
    r.mgr.set_shutdown_pre_retired_check_hook_for_test(&sid, Box::new(move || {
        snapshot_tx.send(()).unwrap();
        check_go_rx.recv_timeout(Duration::from_secs(10)).unwrap();
    }));
    let (window_tx, window_rx) = crossbeam_channel::bounded(1);
    let (replace_go_tx, replace_go_rx) = crossbeam_channel::bounded(1);
    r.mgr.set_retired_drain_replace_hook_for_test(&sid, Box::new(move || {
        window_tx.send(()).unwrap();
        replace_go_rx.recv_timeout(Duration::from_secs(10)).unwrap();
    }));
    let shutdown_rig = Arc::clone(&r);
    let deadline = Instant::now() + Duration::from_secs(2);
    let (phase1_tx, phase1_rx) = crossbeam_channel::bounded(1);
    let phase1 = std::thread::spawn(move || {
        phase1_tx.send(shutdown_rig.mgr.shutdown_phase1(deadline)).unwrap();
    });
    snapshot_rx.recv_timeout(Duration::from_secs(1)).unwrap();
    // GetState 已回覆 RELEASED，現在才交付 BridgeOpened，並停在 token 移除、epoch 加入之間。
    dev.pause_next_close();
    open_go_tx.send(()).unwrap();
    window_rx.recv_timeout(Duration::from_secs(1)).unwrap();
    let pending_in_window = fence.with(|f| f.retired_drains_pending);
    check_go_tx.send(()).unwrap();
    // actor 仍暫停：正確路徑已排入 Shutdown；舊版跳過 session，直接完成 Phase 1。
    wait_until("shutdown 已讀取替換窗口的鏡像", || !control.is_empty() || !phase1_rx.is_empty());
    replace_go_tx.send(()).unwrap();
    let close_paused = dev.wait_paused(Duration::from_secs(1));
    let drain = phase1_rx.recv_timeout(Duration::from_secs(1)).unwrap();
    phase1.join().unwrap();
    let retiring = r.mgr.retired_drains_for_test(&sid);
    let pending_during_close = fence.with(|f| f.retired_drains_pending);
    assert!(r.mgr.custody_for_test(BY_ID_A).is_none(), "AdoptBridge 已移除帳本紀錄，Phase 2 只能依 actor 等待");
    let shutdown_rig = Arc::clone(&r);
    let (report_tx, report_rx) = crossbeam_channel::bounded(1);
    let phase2 = std::thread::spawn(move || {
        report_tx.send(shutdown_rig.mgr.shutdown_phase2(drain)).unwrap();
    });
    // 保留舊版早回覆，先放行 close、收好所有執行緒，再讓斷言回報真正的紅燈。
    let early = report_rx.recv_timeout(Duration::from_millis(100)).ok();
    let timed = if timeout {
        Some(early.clone().unwrap_or_else(|| report_rx.recv_timeout(Duration::from_secs(3)).unwrap()))
    } else {
        None
    };
    let open_before_receipt = dev.is_open();
    let deadline_elapsed = Instant::now() >= deadline;
    let pending_before_receipt = fence.with(|f| f.retired_drains_pending);
    dev.release_pause();
    let report = timed.or_else(|| early.clone())
        .unwrap_or_else(|| report_rx.recv_timeout(Duration::from_secs(3)).unwrap());
    phase2.join().unwrap();
    let attached = attach.join().unwrap();
    let bound = bind.join().unwrap();
    wait_until("成功關閉回條清除鏡像", || {
        !dev.is_open() && dev.close_count() == closes + 1 && !fence.with(|f| f.retired_drains_pending)
    });

    assert!(pending_in_window, "token→epoch 替換窗口不可發布 retired_drains_pending=false；shutdown={report:?}");
    assert!(close_paused && open_before_receipt, "真實 IoWorker 的 handle 在 close 暫停期間仍開啟");
    assert_eq!(retiring, vec![epoch], "同資源等待已換成退休 epoch");
    assert!(pending_during_close && pending_before_receipt, "成功回條前持續發布待排空");
    assert!(early.is_none(), "close 阻塞時 shutdown 不得提前完成：{early:?}");
    assert_eq!(report.iter().find(|(target, _)| target == &sid), Some(&(sid.clone(), !timeout)), "shutdown 不得漏列 session：{report:?}");
    if timeout {
        assert!(deadline_elapsed, "未排空判定必須等到共用期限");
    }
    assert_eq!(s(&attached, "error_code"), "SESSION_RECOVERING");
    assert_eq!(s(&bound, "error_code"), "SESSION_RECOVERING");
    for response in [&attached, &bound] {
        let session = sub(response, "session");
        assert_eq!(s(&session, "device_by_id"), BY_ID_A, "取消後保留原綁定");
        assert_eq!(j(response), j(&sw_proto::pydict! {
            "ok" => false, "error_code" => "SESSION_RECOVERING",
            "drain_stalled" => session.get("drain_stalled").unwrap().clone(),
            "drain_timeout_epoch" => session.get("drain_timeout_epoch").unwrap().clone(),
            "session" => session, "version" => r.version.clone(),
        }), "RPC 完整形狀與 drain 投影逐位元組一致");
    }
    assert_eq!(dev.close_count(), closes + 1);
    assert_eq!(dev.open_attempts(), attempts, "取消後不增開埠");
    assert_eq!(dev.tx_len(), tx_before, "取消後零登入 TX");
}

#[test]
fn wp510r_i22_rpc_shutdown_token_epoch_window_waits_successful_close() {
    wp510r_i22_rpc_shutdown_during_token_epoch_replacement(false);
}

#[test]
fn wp510r_i22_rpc_shutdown_token_epoch_window_reports_blocked_close() {
    wp510r_i22_rpc_shutdown_during_token_epoch_replacement(true);
}

/// 第 15 項共用：拔除 usb-G 並讓熱插執行緒停在 parked handle 的關閉。
fn wp510r_i15_unplug_parked_paused(r: &ServiceRig, dev: &FakeDevice) -> std::thread::JoinHandle<()> {
    let without: Vec<(String, DeviceInfo)> = r
        .mgr
        .list_devices()
        .into_iter()
        .filter_map(|row| {
            let by_id = s(&row, "by_id").to_string();
            let real_path = s(&row, "real_path").to_string();
            (by_id != WP510_PLACEHOLDER_BY_ID)
                .then(|| (by_id.clone(), DeviceInfo { by_id, real_path }))
        })
        .collect();
    dev.pause_next_close();
    let unplug_mgr = r.mgr.clone();
    let unplug = std::thread::spawn(move || unplug_mgr.update_devices(&without));
    assert!(
        dev.wait_paused(Duration::from_secs(5)),
        "拔除執行緒停在 parked handle 關閉"
    );
    unplug
}

/// 審查 P8-add3ca0-1 的 lane 滿載交錯。第 15 項改寫（擁有者 2026-11-11「wp510r release 等待改為 actor 主導」；比照
/// 同日「包內新增 L1 節點改寫」的裁決保留結果斷言）：原本驗 manager 的遲到補送在 safety lane 滿載時保留義務；actor
/// 主導後，完成義務就是 actor 自己的停止待決，關閉終態以 `CustodyTerminal`（關閉者鎖外 `try_send`）通知，送不進去時
/// 留在帳本、由 actor 的 `CustodyPoll` 補收（設計 C4）。改驗同類交錯：首次 release 逾時回 DRAIN_STALLED 後 actor 暫停、
/// control lane 灌滿，期間第二次 release 排入、parked 關閉成功（終態送不進 control lane）；恢復後第二次 release 沿用首次
/// 期限回 DRAIN_STALLED，終態補收後完成首次 release（RELEASED、清 drain 欄位、落盤），不重開。Python `release_device`
/// 在 RPC 執行緒同步處理（`origin/main:sw_core/session_manager.py:3095-3219`），逾時後作業才收尾時 `_finish_attach_op`
/// 完成 release（`:4340-4363`）。
#[test]
fn wp510r_r6_rpc_p8_add3ca0_1_release_late_close_retries_when_safety_lane_full() {
    let r = wp510_b_placeholder_rig();
    let dev = wp510_review8_double_enospc_bind_setup(&r, false);
    let (unplug, sid, token) = wp510r_r6_parked_release_wait_after_timeout(&r, &dev);
    let attempts = dev.open_attempts();

    let (actor_resume, query) = wp510r_i15_hold_com1_actor(&r);
    let control = r.mgr.actor_control_for_test(&sid).expect("COM1 control lane");
    let mut filled = 0;
    while control
        .try_send(sw_core::mailbox::ControlEvent::ConsoleCountChanged { count: 0 })
        .is_ok()
    {
        filled += 1;
    }
    assert!(filled > 0, "actor 暫停時 control lane 灌滿");
    let (duplicate_rx, duplicate) = wp510r_i15_spawn_release(&r);

    dev.release_pause();
    wait_until("關閉回條結算：紀錄移除、終態留在帳本供補收", || {
        r.mgr.custody_for_test(WP510_PLACEHOLDER_BY_ID).is_none()
            && r.mgr.terminal_for_test(token).map(|t| t.handle)
                == Some(sw_core::custody::TerminalHandle::Closed)
    });
    // 關閉者結算後鎖外 `try_send` 的 `CustodyTerminal` 因 control lane 滿而送不進去；熱插執行緒之後的 `DeviceMissing`
    // 為阻塞送出，要等 actor 恢復才完成，故先恢復 actor 再 join。
    assert!(
        duplicate_rx.recv_timeout(Duration::from_millis(300)).is_err(),
        "actor 暫停期間 release 不被回覆"
    );

    actor_resume.send(()).expect("恢復 actor");
    unplug.join().expect("拔除的熱插執行緒不應 panic");
    assert!(
        query.join().expect("查詢執行緒不應 panic").is_ok(),
        "actor 查詢正常回覆"
    );
    let duplicate_result = duplicate_rx
        .recv_timeout(Duration::from_secs(5))
        .expect("恢復後第二次 release 回覆")
        .expect("release RPC");
    duplicate.join().expect("重複 release 執行緒不應 panic");
    assert_eq!(
        duplicate_result.get("error_code"),
        Some(&PyValue::from("DRAIN_STALLED")),
        "{}",
        j(&duplicate_result)
    );
    // 拔除的 `DeviceMissing` 會把無 bridge 的 session 標為 DETACHED（既有 `mark_missing_device`，Python
    // `_mark_missing_devices_locked`）；終態尚未補收前不得完成 release。
    assert_ne!(
        s(&r.session("COM1").expect("COM1 session"), "state"),
        "RELEASED",
        "終態送不進 control lane，尚未補收"
    );
    assert!(!wp510r_r6_state_has_released_sid(&r, &sid), "補收前不落盤");

    // `CustodyPoll`（500 ms）自帳本補收終態。
    r.clock.advance(Duration::from_millis(600));
    r.wait_state("COM1", "RELEASED");
    let session = r.session("COM1").expect("COM1 session");
    assert_eq!(s(&session, "released_by"), "cli", "{}", j(&session));
    assert_eq!(
        session.get("drain_stalled"),
        Some(&PyValue::Bool(false)),
        "{}",
        j(&session)
    );
    assert_eq!(
        session.get("drain_timeout_epoch"),
        Some(&PyValue::Null),
        "{}",
        j(&session)
    );
    wait_until("滿載後遲到完成的 RELEASED 落盤", || {
        wp510r_r6_state_has_released_sid(&r, &sid)
    });
    assert_eq!(dev.close_count(), 1, "parked handle 只關閉一次");
    assert!(
        r.mgr.custody_for_test(WP510_PLACEHOLDER_BY_ID).is_none(),
        "回條後紀錄移除"
    );
    std::thread::sleep(Duration::from_millis(300));
    assert_eq!(dev.open_attempts(), attempts, "release 完成後不自動重開");
}

/// P8-2c5eb6b-2：遲到的 release 完成不得作用在改綁之後的新綁定。第 15 項改寫（擁有者 2026-11-11「wp510r release 等待
/// 改為 actor 主導」）：manager 的遲到送達者及其暫停點已刪除，改為 actor 模型下可達的交錯：首次 release 逾時回
/// DRAIN_STALLED 後 actor 暫停，期間 parked 關閉成功（終態排入 actor）、`session.bind` 改綁排在其後；actor 恢復後依序先
/// 以終態完成首次 release（RELEASED），再由 bind 改綁到新 by_id（解除 released、DETACHED）。舊 release 的完成不得作用在
/// 新綁定上：之後不再有遲到收尾把新綁定轉 RELEASED，state.json 也沒有該 session 的 released 列。Python release 與 bind
/// 在 manager lock 內依序處理（`origin/main:sw_core/session_manager.py:3095-3219`、`:3664-3812`）。
#[test]
fn wp510r_r6_rpc_late_release_snapshot_is_invalidated_by_bind() {
    let r = wp510_b_placeholder_rig();
    let dev = wp510_review8_double_enospc_bind_setup(&r, false);
    let (unplug, sid, token) = wp510r_r6_parked_release_wait_after_timeout(&r, &dev);
    let (actor_resume, query) = wp510r_i15_hold_com1_actor(&r);

    dev.release_pause();
    unplug.join().expect("拔除執行緒不應 panic");
    let rebound = "/s/by-id/wp510r-r6-rebound";
    let bind_service = r.svc.clone();
    let bind = std::thread::spawn(move || {
        bind_service.rpc(
            "session.bind",
            &sw_proto::pydict! { "selector" => "COM1", "device_by_id" => rebound },
        )
    });
    std::thread::sleep(Duration::from_millis(200));
    assert!(!bind.is_finished(), "actor 暫停期間 bind 不被回覆");

    actor_resume.send(()).expect("恢復 actor");
    assert!(query.join().expect("查詢執行緒不應 panic").is_ok());
    let bound = bind
        .join()
        .expect("bind 執行緒不應 panic")
        .expect("bind RPC");
    assert_eq!(bound.get("ok"), Some(&PyValue::Bool(true)), "{}", j(&bound));
    assert_eq!(
        s(&r.session("COM1").expect("COM1 session"), "device_by_id"),
        rebound
    );
    // 給任何遲到收尾（含 `CustodyPoll`）機會作用。
    r.clock.advance(Duration::from_millis(600));
    std::thread::sleep(Duration::from_millis(300));
    let session = r.session("COM1").expect("改綁後的 session");
    assert_eq!(
        s(&session, "state"),
        "DETACHED",
        "舊 release 不得釋放新綁定：{}",
        j(&session)
    );
    assert!(
        !wp510r_r6_state_has_released_sid(&r, &sid),
        "改綁後不得留下 released 列"
    );
    assert_eq!(dev.close_count(), 1, "parked handle 僅關閉一次");
    assert!(r.mgr.custody_for_test(WP510_PLACEHOLDER_BY_ID).is_none());
    assert_eq!(
        r.mgr.terminal_for_test(token).map(|t| t.handle),
        Some(sw_core::custody::TerminalHandle::Closed)
    );
}

/// P8-2c5eb6b-3：較晚的 release 共用首次絕對期限。第 15 項改寫（擁有者 2026-11-11「wp510r release 等待改為 actor
/// 主導」）：原本驗 lane 滿載時 manager 立即代答；actor 主導後 release 的回覆由 actor 依自己的狀態組成，actor 暫停期間
/// 不回覆（RPC 以既有逾時處理）。改驗：首次 release 逾時回 DRAIN_STALLED 後 actor 暫停，期間 parked 關閉成功、第二次
/// release 排入；恢復後第二次 release 不重新起算自己的 3 秒，立即沿用首次期限回 DRAIN_STALLED，之後終態完成首次 release
/// 並落盤。Python 依據：首次期限記在作業上、之後的 release 沿用（`origin/main:sw_core/session_manager.py:3117-3124`、
/// `:3192-3204`），逾時後作業才收尾時完成 release（`:4340-4363`）。
#[test]
fn wp510r_r6_rpc_late_release_retry_wait_uses_first_deadline() {
    let r = wp510_b_placeholder_rig();
    let dev = wp510_review8_double_enospc_bind_setup(&r, false);
    let (unplug, sid, _token) = wp510r_r6_parked_release_wait_after_timeout(&r, &dev);
    let (actor_resume, query) = wp510r_i15_hold_com1_actor(&r);

    dev.release_pause();
    unplug.join().expect("拔除執行緒不應 panic");
    let (duplicate_rx, duplicate) = wp510r_i15_spawn_release(&r);
    assert!(
        duplicate_rx.recv_timeout(Duration::from_millis(300)).is_err(),
        "actor 暫停期間 release 不被回覆"
    );
    let resumed = Instant::now();
    actor_resume.send(()).expect("恢復 actor");
    assert!(
        query.join().expect("查詢執行緒不應 panic").is_ok(),
        "查詢正常回覆"
    );
    let duplicate_result = duplicate_rx
        .recv_timeout(Duration::from_secs(5))
        .expect("恢復後 release RPC 應完成")
        .expect("release RPC");
    let waited = resumed.elapsed();
    duplicate.join().expect("第二次 release 執行緒不應 panic");
    assert!(
        waited < Duration::from_millis(1000),
        "首次期限已過，第二次 release 不得重新等待自己的 3 秒：{waited:?}"
    );
    assert_eq!(
        duplicate_result.get("error_code"),
        Some(&PyValue::from("DRAIN_STALLED")),
        "沿用首次期限回 stalled：{}",
        j(&duplicate_result)
    );

    r.wait_state("COM1", "RELEASED");
    let session = r.session("COM1").expect("COM1 session");
    assert_eq!(
        session.get("drain_stalled"),
        Some(&PyValue::Bool(false)),
        "{}",
        j(&session)
    );
    wait_until("背景義務完成並落盤", || {
        wp510r_r6_state_has_released_sid(&r, &sid)
    });
}

/// 第 15 項共用：STALL 回應的完整形狀（頂層欄位順序與正常 STALL 回應相同、session 欄位順序與完整快照相同、原 timeout
/// epoch、RECOVERING）。
fn wp510r_i15_assert_full_stall(result: &PyDict, snapshot: &PyDict, sid: &str, epoch: i64) {
    assert_eq!(
        result.get("error_code"),
        Some(&PyValue::from("DRAIN_STALLED")),
        "{}",
        j(result)
    );
    assert_eq!(
        result.get("drain_timeout_epoch"),
        Some(&PyValue::from(epoch)),
        "沿用原 timeout epoch：{}",
        j(result)
    );
    let stalled_session = sub(result, "session");
    assert_eq!(s(&stalled_session, "session_id"), sid);
    assert_eq!(s(&stalled_session, "device_by_id"), WP510_PLACEHOLDER_BY_ID);
    assert_eq!(s(&stalled_session, "state"), "RECOVERING");
    assert_eq!(
        stalled_session.get("drain_stalled"),
        Some(&PyValue::Bool(true))
    );
    assert_eq!(
        stalled_session.get("drain_timeout_epoch"),
        Some(&PyValue::from(epoch))
    );
    let shape = |keys: Vec<&str>| PyValue::List(keys.into_iter().map(PyValue::from).collect());
    let normal_stall_response_shape = PyValue::List(
        [
            "ok",
            "error_code",
            "drain_stalled",
            "drain_timeout_epoch",
            "session",
            "version",
        ]
        .into_iter()
        .map(PyValue::from)
        .collect(),
    );
    assert_eq!(
        jv(&shape(keys(result))),
        jv(&normal_stall_response_shape),
        "頂層欄位順序須與正常 STALL 回應逐位元組同形：{}",
        j(result)
    );
    assert_eq!(
        jv(&shape(keys(&stalled_session))),
        jv(&shape(keys(snapshot))),
        "session 欄位及順序須與完整 session 快照逐位元組同形：{}",
        j(result)
    );
}

/// P8-6ca9bc8-1：首次 release 等待 parked 關閉期間 actor 暫停、首次期限在暫停中到期、關閉成功、第二次 release 排入。
/// 第 15 項改寫（擁有者 2026-11-11「wp510r release 等待改為 actor 主導」）：原本驗 manager 以首次等待前保存的快照代答；
/// actor 主導後回覆一律由 actor 依自己的狀態組成，不需另外的快照，actor 暫停期間兩個 release 都不被回覆（RPC 以既有逾時
/// 處理）。恢復後：首次 release 由期限通知回 DRAIN_STALLED，第二次 release 沿用首次期限立即回 DRAIN_STALLED，帶完整
/// session 欄位（與正常 STALL 回應逐位元組同形）與原 timeout epoch；之後終態完成首次 release 並落盤。Python release 在
/// RPC 執行緒同步處理（`origin/main:sw_core/session_manager.py:3095-3219`），首次期限記在作業上（`:3117-3124`）。
#[test]
fn wp510r_r6_rpc_late_release_uses_snapshot_when_first_stall_reply_pending() {
    let r = wp510_b_placeholder_rig();
    let dev = wp510_review8_double_enospc_bind_setup(&r, false);
    let snapshot = r.session("COM1").expect("COM1 完整 session 快照");
    let sid = s(&snapshot, "session_id").to_string();
    let epoch = snapshot
        .get("bridge_generation")
        .and_then(PyValue::as_i64)
        .expect("原 timeout epoch");
    let unplug = wp510r_i15_unplug_parked_paused(&r, &dev);

    let (first_rx, first_release) = wp510r_i15_spawn_release(&r);
    wait_until("首次 release 已由 actor 認領、登記為 watcher", || {
        r.mgr
            .custody_for_test(WP510_PLACEHOLDER_BY_ID)
            .is_some_and(|rec| rec.watchers.contains(&sid))
    });
    assert!(first_rx.try_recv().is_err(), "首次 release 仍在等待 parked close");
    let safety_observer = r
        .mgr
        .safety_receiver_for_test("COM1")
        .expect("COM1 safety lane");
    wait_until("暫停 actor 前 safety lane 已清空", || safety_observer.is_empty());
    let (actor_resume, query) = wp510r_i15_hold_com1_actor(&r);
    wait_until("首次期限到期，期限通知已排入 safety lane", || {
        safety_observer.len() == 1
    });

    dev.release_pause();
    unplug.join().expect("拔除執行緒不應 panic");
    assert!(r.mgr.custody_for_test(WP510_PLACEHOLDER_BY_ID).is_none());
    let (duplicate_rx, duplicate_release) = wp510r_i15_spawn_release(&r);
    wait_until("第二次 release 已排入 safety lane", || safety_observer.len() == 2);
    assert!(first_rx.try_recv().is_err(), "actor 暫停期間首次 release 不被回覆");
    assert!(
        duplicate_rx.recv_timeout(Duration::from_millis(300)).is_err(),
        "actor 暫停期間 manager 不代答第二次 release"
    );

    actor_resume.send(()).expect("恢復 actor");
    assert!(query.join().expect("查詢執行緒不應 panic").is_ok(), "actor 查詢在恢復後正常完成");
    let first_result = first_rx
        .recv_timeout(Duration::from_secs(5))
        .expect("首次 release 在 actor 恢復後取得回覆")
        .expect("首次 release RPC");
    first_release.join().expect("首次 release 執行緒不應 panic");
    wp510r_i15_assert_full_stall(&first_result, &snapshot, &sid, epoch);
    let duplicate_result = duplicate_rx
        .recv_timeout(Duration::from_secs(5))
        .expect("期限已過的第二次 release 應完成")
        .expect("第二次 release RPC");
    duplicate_release
        .join()
        .expect("第二次 release 執行緒不應 panic");
    wp510r_i15_assert_full_stall(&duplicate_result, &snapshot, &sid, epoch);

    r.wait_state("COM1", "RELEASED");
    wait_until("完成義務落盤", || wp510r_r6_state_has_released_sid(&r, &sid));
    assert_eq!(dev.close_count(), 1, "parked handle 僅關閉一次");
}

/// P8-0c50830-1／wp510r 第 16 項 Medium 4：actor 暫停期間，release 的絕對期限仍從 RPC 進入時起算。
/// 第 15 項改寫（擁有者 2026-11-11「wp510r release 等待改為 actor 主導」）：actor 暫停時 release 排在 mailbox；恢復後
/// actor 認領正在關閉的 parked 紀錄，但必須沿用 RPC 進入時攜入的期限。Python 在鎖前起算（`origin/main:sw_core/session_manager.py:3084-3086`）、
/// 並把首次期限記在等待對象上（`:3122-3124`）。關閉成功後仍完成 RELEASED、落盤、`released_by=cli`，只關閉一次。
#[test]
fn wp510r_r6_rpc_snapshot_query_timeout_preserves_late_release_completion() {
    let r = wp510_b_placeholder_rig();
    let dev = wp510_review8_double_enospc_bind_setup(&r, false);
    let snapshot = r.session("COM1").expect("COM1 完整 session 快照");
    let sid = s(&snapshot, "session_id").to_string();
    let epoch = snapshot
        .get("bridge_generation")
        .and_then(PyValue::as_i64)
        .expect("原 timeout epoch");
    let unplug = wp510r_i15_unplug_parked_paused(&r, &dev);

    let (actor_resume, query) = wp510r_i15_hold_com1_actor(&r);
    let service = r.svc.clone();
    let (entered_tx, entered_rx) = crossbeam_channel::bounded(1);
    let (release_tx, release_rx) = crossbeam_channel::bounded(1);
    let release = std::thread::spawn(move || {
        let entered = Instant::now();
        let _ = entered_tx.send(entered);
        let result = service.rpc(
            "device.release",
            &sw_proto::pydict! { "selector" => "COM1" },
        );
        let _ = release_tx.send(result);
    });
    let rpc_entered = entered_rx.recv().expect("取得 RPC 進入時刻");
    // actor 尚在處理前一筆查詢時，release 已進 RPC；兩秒後恢復，首次期限只剩約一秒。
    std::thread::sleep(Duration::from_secs(2));
    assert!(release_rx.try_recv().is_err(), "actor 暫停期間 release 不被回覆");
    assert!(
        r.mgr
            .custody_for_test(WP510_PLACEHOLDER_BY_ID)
            .is_some_and(|rec| !rec.watchers.contains(&sid)),
        "actor 尚未受理 release"
    );
    actor_resume.send(()).expect("恢復 actor");
    assert!(query.join().expect("查詢執行緒不應 panic").is_ok());
    let allowed_deadline = rpc_entered + Duration::from_secs(3) + Duration::from_millis(500);
    let early_result = release_rx.recv_timeout(allowed_deadline.saturating_duration_since(Instant::now()));
    let response_at = early_result.as_ref().ok().map(|_| Instant::now());
    let release_result = match early_result {
        Ok(result) => result,
        Err(_) => release_rx
            .recv_timeout(Duration::from_secs(4))
            .expect("actor 恢復後 release 最終應回覆"),
    };
    release.join().expect("首次 release 執行緒不應 panic");
    let stalled = release_result.expect("首次 release 回 DRAIN_STALLED");
    wp510r_i15_assert_full_stall(&stalled, &snapshot, &sid, epoch);

    dev.release_pause();
    unplug.join().expect("拔除執行緒不應 panic");
    r.wait_state("COM1", "RELEASED");
    wait_until("晚到 release 已落盤", || wp510r_r6_state_has_released_sid(&r, &sid));
    let released = r.session("COM1").expect("RELEASED session");
    assert_eq!(s(&released, "released_by"), "cli", "首次 release source 須保留");
    assert_eq!(dev.close_count(), 1, "parked handle 僅關閉一次");
    assert!(
        response_at.is_some_and(|at| at <= allowed_deadline),
        "首次 release 應不晚於 RPC 進入 + 3 s（含 500 ms 排程容差）；RPC 進入={rpc_entered:?}，回覆={response_at:?}"
    );
}

/// 審查 P8-990a04b-1（High，HEAD `990a04b3`）：首次 release 的快照查詢因 actor 暫停超過 5 秒而失敗、parked 關閉在暫停中
/// 成功、之後再送第二次 release；原實作由 manager 代答第二次 release 時缺少快照而回例外。第 15 項（擁有者 2026-11-11
/// 「wp510r release 等待改為 actor 主導」）後 manager 不保存快照、不代答：actor 暫停期間兩個 release 都排在 mailbox；
/// 恢復後首次 release 認領時紀錄已結算（沒有要等的對象），照一般 release 完成 RELEASED 並落盤（`released_by` 取首次
/// release 的 `cli`），第二次 release 冪等回 already_released；零重開、只關閉一次。Python release 在 RPC 執行緒同步處理，
/// 找不到作業即照常釋放（`origin/main:sw_core/session_manager.py:3095-3219`）。
#[test]
fn wp510r_i15_rpc_p8_990a04b_1_releases_queued_during_actor_pause_after_parked_close() {
    let r = wp510_b_placeholder_rig();
    let dev = wp510_review8_double_enospc_bind_setup(&r, false);
    let sid = s(&r.session("COM1").expect("COM1 session"), "session_id").to_string();
    let token = r
        .mgr
        .custody_for_test(WP510_PLACEHOLDER_BY_ID)
        .expect("前置：parked 帳本紀錄")
        .token;
    let unplug = wp510r_i15_unplug_parked_paused(&r, &dev);
    let attempts = dev.open_attempts();

    let (actor_resume, query) = wp510r_i15_hold_com1_actor(&r);
    let (first_rx, first) = wp510r_i15_spawn_release(&r);
    std::thread::sleep(Duration::from_millis(5300));
    dev.release_pause();
    unplug.join().expect("拔除執行緒不應 panic");
    assert!(r.mgr.custody_for_test(WP510_PLACEHOLDER_BY_ID).is_none(), "關閉回條移除帳本紀錄");
    assert_eq!(
        r.mgr.terminal_for_test(token).map(|t| t.handle),
        Some(sw_core::custody::TerminalHandle::Closed)
    );
    let (second_rx, second) = wp510r_i15_spawn_release(&r);
    std::thread::sleep(Duration::from_millis(300));
    assert!(first_rx.try_recv().is_err(), "actor 暫停期間 manager 不代答首次 release");
    assert!(second_rx.try_recv().is_err(), "actor 暫停期間 manager 不代答第二次 release");

    actor_resume.send(()).expect("恢復 actor");
    assert!(query.join().expect("查詢執行緒不應 panic").is_ok());
    let first_result = first_rx
        .recv_timeout(Duration::from_secs(5))
        .expect("恢復後首次 release 回覆")
        .expect("首次 release RPC");
    first.join().expect("首次 release 執行緒不應 panic");
    assert_eq!(first_result.get("ok"), Some(&PyValue::Bool(true)), "{}", j(&first_result));
    assert_eq!(first_result.get("already_released"), None, "{}", j(&first_result));
    assert_eq!(s(&sub(&first_result, "session"), "state"), "RELEASED");
    let second_result = second_rx
        .recv_timeout(Duration::from_secs(5))
        .expect("恢復後第二次 release 回覆")
        .expect("第二次 release RPC");
    second.join().expect("第二次 release 執行緒不應 panic");
    assert_eq!(
        second_result.get("already_released"),
        Some(&PyValue::Bool(true)),
        "{}",
        j(&second_result)
    );
    wait_until("RELEASED 落盤", || wp510r_r6_state_has_released_sid(&r, &sid));
    let session = r.session("COM1").expect("COM1 session");
    assert_eq!(s(&session, "released_by"), "cli", "{}", j(&session));
    assert_eq!(session.get("drain_stalled"), Some(&PyValue::Bool(false)), "{}", j(&session));
    assert_eq!(dev.close_count(), 1, "parked handle 只關閉一次");
    std::thread::sleep(Duration::from_millis(300));
    assert_eq!(dev.open_attempts(), attempts, "release 之後不重開");
}

/// 已知差異 N-14（設計第 5 版 §1.10、D1 iii＋T-1）：未消費的 parked（`Held{Parked}`）由 release 自己觸發關閉且關閉失敗
/// （closed:false）時，首次 release 等滿自己的 3 秒絕對期限才回 DRAIN_STALLED，期限已過的 release 立即回；紀錄轉
/// CloseFailed、登記未確認關閉，之後不重開、零 TX、不落盤 released。第 15 項前 manager 在 RPC 執行緒同步關閉並立即回
/// DRAIN_STALLED（Python 在 release 內同步關閉後立即回，`origin/main:sw_core/session_manager.py:3109-3111`、`:3188-3190`、
/// `:3193`）；actor 主導後關閉交關閉者、終態以 `CustodyTerminal` 通知，依 T-1 於首次期限回覆。
#[test]
fn wp510r_i15_rpc_release_triggered_parked_close_failure_waits_first_deadline() {
    let r = wp510_b_placeholder_rig();
    let dev = wp510_review8_double_enospc_bind_setup(&r, false);
    let sid = s(&r.session("COM1").expect("COM1 session"), "session_id").to_string();
    let parked = r
        .mgr
        .custody_for_test(WP510_PLACEHOLDER_BY_ID)
        .expect("前置：parked 帳本紀錄");
    assert_eq!(parked.owner, sw_core::custody::CustodyOwner::Parked, "{parked:?}");
    let attempts = dev.open_attempts();
    let tx_before = dev.tx_len();
    dev.fail_close(PalError::io(5, "[Errno 5] Input/output error"));

    let started = Instant::now();
    let stalled = r.call("device.release", r#"{"selector":"COM1"}"#);
    let elapsed = started.elapsed();
    assert_eq!(stalled.get("error_code"), Some(&PyValue::from("DRAIN_STALLED")), "{}", j(&stalled));
    assert_eq!(stalled.get("drain_stalled"), Some(&PyValue::Bool(true)), "{}", j(&stalled));
    assert!(
        elapsed >= Duration::from_millis(2900),
        "T-1：失敗終態不提前回覆，等滿首次 3 秒期限：{elapsed:?}"
    );
    assert_eq!(s(&sub(&stalled, "session"), "state"), "RECOVERING", "{}", j(&stalled));
    let rec = r
        .mgr
        .custody_for_test(WP510_PLACEHOLDER_BY_ID)
        .expect("CloseFailed 紀錄保留");
    assert_eq!(rec.token, parked.token, "同一 token");
    assert!(matches!(rec.phase, CustodyPhaseSnapshot::CloseFailed { .. }), "{rec:?}");
    assert!(r.mgr.unconfirmed_close_for_test(WP510_PLACEHOLDER_BY_ID), "登記未確認關閉");

    let again_at = Instant::now();
    let again = r.call("device.release", r#"{"selector":"COM1"}"#);
    assert_eq!(again.get("error_code"), Some(&PyValue::from("DRAIN_STALLED")), "{}", j(&again));
    assert!(
        again_at.elapsed() < Duration::from_millis(1000),
        "期限已過的 release 立即回：{:?}",
        again_at.elapsed()
    );
    assert!(!wp510r_r6_state_has_released_sid(&r, &sid), "關閉未確認不得落盤 released");
    std::thread::sleep(Duration::from_millis(300));
    assert_eq!(dev.close_count(), 1, "只嘗試關閉一次");
    assert_eq!(dev.open_attempts(), attempts, "不重開");
    assert_eq!(dev.tx_len(), tx_before, "零 TX");
}

/// P8-2c5eb6b-1：release 首次期限到期後才進入 Permanent，晚到成功回條仍完成既有 release。
#[test]
fn wp510r_r6_rpc_late_release_completion_survives_shutdown_freeze() {
    let r = wp510_b_placeholder_rig();
    let dev = wp510_review8_double_enospc_bind_setup(&r, false);
    let (unplug, sid, token) = wp510r_r6_parked_release_wait_after_timeout(&r, &dev);

    r.svc.shutdown_freeze();
    assert_eq!(
        r.mgr.deliverer_gate_for_test().handoff_freeze_state(),
        sw_core::admission::HandoffFreezeState::Permanent,
        "測試前置：shutdown admission 已進入 Permanent"
    );
    dev.release_pause();
    unplug.join().expect("拔除執行緒不應 panic");

    r.wait_state("COM1", "RELEASED");
    let session = r.session("COM1").expect("COM1 session");
    assert_eq!(
        session.get("drain_stalled"),
        Some(&PyValue::Bool(false)),
        "{}",
        j(&session)
    );
    assert_eq!(
        session.get("drain_timeout_epoch"),
        Some(&PyValue::Null),
        "{}",
        j(&session)
    );
    wait_until("Permanent 凍結下遲到 release 已落盤", || {
        wp510r_r6_state_has_released_sid(&r, &sid)
    });
    assert!(r.mgr.custody_for_test(WP510_PLACEHOLDER_BY_ID).is_none());
    assert_eq!(
        r.mgr.terminal_for_test(token).map(|t| t.handle),
        Some(sw_core::custody::TerminalHandle::Closed)
    );
}

/// 第 20 項的 RPC 接線證據：回收者的 Held close 暫停時，正式查詢與另一 session release 仍照常完成。
#[test]
fn wp510r_i20_rpc_query_and_release_while_reaper_held_close_paused() {
    let r = ServiceRig::ready_shell();
    let serial = FakeSerialBackend::new();
    let path = "/dev/ttyI20RpcHeld";
    let dev = serial.add_device(path);
    let config = SerialConfig::from_uart_profile(115200, 8, "none", 1, "none", false);
    let handle = serial.open_exclusive(&DevicePath(path.into()), &config).unwrap();
    let (actor, token) = r.mgr.custody_register_held_parts_for_test(
        "by-id-i20-rpc-held", path, sw_core::session_manager::HandleParts::split(handle),
    );
    dev.pause_next_close();
    r.mgr.reaper_cmd_sender_for_test().send(sw_core::custody::ReaperCmd::ActorExited { actor }).unwrap();
    assert!(dev.wait_paused(Duration::from_secs(3)));
    let started = Instant::now();
    let listed = r.call("session.list", "{}");
    let released = r.call("device.release", r#"{"selector":"COM0","source":"agent"}"#);
    let elapsed = started.elapsed();
    let state = r.call("session.get_state", r#"{"selector":"COM0"}"#);
    let pending = r.mgr.terminal_for_test(token).is_none();
    dev.release_pause();
    wait_until("Held 配對回條", || r.mgr.terminal_for_test(token).is_some());

    assert_eq!(listed.get("ok"), Some(&PyValue::Bool(true)), "{}", j(&listed));
    assert!(elapsed < Duration::from_secs(1), "查詢與另一 COM release 須在期限內完成：{elapsed:?}");
    assert_eq!(j(&released), format!(r#"{{"ok":true,"session":{},"closed_consoles":0,"aborted_cmd":false,"version":"{}"}}"#, j(&sub(&state, "session")), r.version));
    assert_eq!(s(&sub(&state, "session"), "state"), "RELEASED");
    assert_eq!(r.devices[REAL_A].close_count(), 1, "RPC 確實走到 COM0 的 IoWorker close");
    assert!(pending, "另一個 Held 尚無回條，不能宣告其成功");
    assert_eq!(dev.close_count(), 1);
    assert_eq!(r.mgr.terminal_for_test(token).unwrap().handle, sw_core::custody::TerminalHandle::Closed);
}

/// P8-849e605-1：正式 RPC 在未接受交接的 close 回條前不得成功；回條後逐位元組比對完整回應。
#[test]
fn wp510r_i20_r2_rpc_release_waits_for_unoffered_close_receipt() {
    let r = Arc::new(wp510_b_placeholder_rig());
    let dev = r.device(WP510_REAL_G);
    r.fs.fail_next_atomic_write(PalError::io(28, "[Errno 28] No space left on device"));
    let error = r.call_err("device.attach", &format!(r#"{{"selector":"{WP510_PLACEHOLDER_SID}"}}"#));
    assert!(error.message.contains("No space left on device"));
    let token = r.mgr.custody_for_test(WP510_PLACEHOLDER_BY_ID).unwrap().token;
    r.mgr.update_devices(&[(WP510_PLACEHOLDER_BY_ID.into(), DeviceInfo {
        by_id: WP510_PLACEHOLDER_BY_ID.into(), real_path: WP510_REAL_G.into(),
    })]);
    r.wait_state("COM0", "DETACHED");
    r.wait_state("COM1", "DETACHED");
    let (entered, at_write) = crossbeam_channel::bounded(1);
    let (resume, go) = crossbeam_channel::bounded(1);
    r.fs.before_next_atomic_write(Box::new(move || {
        entered.send(()).unwrap();
        go.recv().unwrap();
    }));
    r.mgr.bootstrap_attach();
    at_write.recv_timeout(Duration::from_secs(3)).expect("binding 落盤暫停");
    dev.pause_next_close();
    let (result, reply) = crossbeam_channel::bounded(1);
    let caller = r.clone();
    let release = std::thread::spawn(move || {
        result.send(caller.call("device.release", r#"{"selector":"COM0","source":"agent"}"#)).unwrap();
    });
    r.wait_state("COM0", "RECOVERING");
    resume.send(()).unwrap();
    assert!(dev.wait_paused(Duration::from_secs(3)));
    let early = reply.recv_timeout(Duration::from_millis(200));
    let no_early_reply = early.is_err();
    let open = dev.is_open();
    let no_terminal = r.mgr.terminal_for_test(token).is_none();
    let inflight = r.mgr.has_inflight_attach_for_test(WP510_PLACEHOLDER_BY_ID);
    let during = r.call("session.get_state", r#"{"selector":"COM0"}"#);
    dev.release_pause();
    let response = early.unwrap_or_else(|_| reply.recv_timeout(Duration::from_secs(5)).unwrap());
    release.join().unwrap();
    assert!(no_early_reply, "回條前 release 不得回覆：{}", j(&response));
    assert!(open && no_terminal && inflight, "回條前保留 handle、token 與作業");
    assert_eq!(s(&sub(&during, "session"), "state"), "RECOVERING");
    let state = r.call("session.get_state", r#"{"selector":"COM0"}"#);
    assert_eq!(j(&response), format!(r#"{{"ok":true,"session":{},"closed_consoles":0,"aborted_cmd":false,"version":"{}"}}"#, j(&sub(&state, "session")), r.version));
    assert_eq!(s(&sub(&state, "session"), "state"), "RELEASED");
    assert_eq!(r.mgr.terminal_for_test(token).unwrap().handle, sw_core::custody::TerminalHandle::Closed);
    assert_eq!(dev.close_count(), 1);
    assert_eq!(dev.open_attempts(), 1);
    assert_eq!(dev.tx_len(), 0, "取消的交接不能開始 PROBE 或登入");
}
