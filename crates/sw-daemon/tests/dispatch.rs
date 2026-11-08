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
    assert_eq!(response.get("ok"), Some(&PyValue::Bool(true)), "{}", j(&response));
    let record = wp55_wait_done(&r, s(&response, "cmd_id"));
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
