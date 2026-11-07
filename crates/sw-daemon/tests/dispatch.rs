//! P5-T18：RPC 平面分派、參數轉型與 `health.status`（plan 06 Task 18 Step 1；parity-amendments-2026-10-26 #11／#69／#100）。
//!
//! 斷言值以 Python 基線（`sw_core/service.py` `_dispatch`／`health`）為準；回應以 `RPC_WIRE` 序列化後逐位元組比對。
#![cfg(feature = "test-fakes")]
mod support;

use std::sync::Mutex;
use std::time::Duration;

use support::{BY_ID_A, BY_ID_B, REAL_A, REAL_B, RIG_PID, ServiceRig, wait_until};
use sw_pal::proc_scan::{DaemonProc, ProcIdentity};
use sw_pal::traits::HolderScanStatus;
use sw_proto::pyjson::{PyDict, PyValue, RPC_WIRE, dumps};

/// `foreign_holders` 會讀取行程內共用的 holder_unknown 登錄（#11）；登錄／精確比對 foreign_holders 的測試彼此序列化。
static HOLDER_REGISTRY: Mutex<()> = Mutex::new(());

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

// ---------------------------------------------------------------------------
// Step 1 計畫列
// ---------------------------------------------------------------------------

#[test]
fn deferred_methods_return_method_not_found_without_side_effects() {
    let r = ServiceRig::ready_shell();
    let scope = std::fs::read_to_string(support::data_root().join("tests/acceptance/slice21_scope.yaml")).unwrap();
    let methods = support::deferred_methods(&scope, &["2.1", sw_core::SLICE]); // 增補 #24：已交付切片之後的延後 method
    // P7←P6 合併 D4：console 已交付、event 已移入 served，只剩 file.push／file.pull。
    assert_eq!(methods, ["file.push", "file.pull"]);
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
    // daemon.stop／daemon.handoff_stop 不經 Service（framing 攔截；handoff 屬 2.4）。
    assert_eq!(
        s(&r.call("daemon.handoff_stop", "{}"), "error_code"),
        "METHOD_NOT_FOUND"
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
