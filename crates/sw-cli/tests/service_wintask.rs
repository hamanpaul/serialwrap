//! P8 Task 13：`windows-task` 的 `service stop`／`restart`／routed `daemon stop` 一律經 P4 `graceful_stop`（memory_only）。
//!
//! 計畫：`docs/rust-refactor/plans/09-slice24-transfer-flash.md` Task 13（`service_wintask.rs` 表列 7 列）。
//! 停止序（寫入 P4 契約，`05-stage1b-rust-cli-mgmt.md` Task 7 `graceful_stop`）：`/Change /DISABLE` → RPC `daemon.stop`
//! → `wait_daemon_gone(10 s)` → 只有逾時，或 RPC 不可達（`daemon.stop` 連線階段失敗）且排程工作查詢仍為 `Running`
//! 時才 `/End` → `/ENABLE`（`restart` 保持停用到 `/Run` 前才 `/ENABLE`）。daemon 已正常結束時不執行 `/End`，避免
//! 「無執行中實例」的非 0 rc 讓健康的 stop 報 `ok:false`。輸出形狀維持 P4 契約，不新增欄位。
//!
//! 外部 oracle：監管命令以 `FakeSysEnv` 實際收到的 argv 為準（`event_order` 的 `run:` 事件），RPC 以 `FakeRpcConnector`
//! 收到的請求與連線逾時為準；daemon 的存活以 `FakeProcScan::alive_while` 模型化（`daemon.stop` 之後或 `/End` 之後才結束）。
//! 「wait_stopped」在事件序上的證據是 `daemon.stop` 之後、`/ENABLE` 之前出現的 `rpc:health.ping#gone`（ping 不通且行程已不存在）。
mod support;

use std::collections::BTreeSet;
use std::sync::{Arc, Mutex};
use std::time::Duration;

use support::*;
use sw_proto::pyjson::{PyInt, PyValue};

/// `WindowsTaskCtl::query("serialwrap")` 的 argv（`Get-ScheduledTask` 的 State；PAL `power_shell_query_argv`）。
const TASK_QUERY: [&str; 5] = [
    "powershell.exe",
    "-NoProfile",
    "-NonInteractive",
    "-Command",
    "try { (Get-ScheduledTask -TaskName 'serialwrap' -ErrorAction Stop).State.ToString() } catch { if ($_.CategoryInfo.Category -eq 'ObjectNotFound') { 'NotFound' } else { exit 3 } }",
];
const END: [&str; 4] = ["schtasks", "/End", "/TN", "serialwrap"];
const PID: u32 = 900;

const RUN_DISABLE: &str = "run:schtasks /Change /TN serialwrap /DISABLE";
const RUN_ENABLE: &str = "run:schtasks /Change /TN serialwrap /ENABLE";
const RUN_END: &str = "run:schtasks /End /TN serialwrap";
const RUN_RUN: &str = "run:schtasks /Run /TN serialwrap";
const RPC_STOP: &str = "rpc:daemon.stop";
const PING_GONE: &str = "rpc:health.ping#gone";

fn task_query_event() -> String {
    format!("run:{}", TASK_QUERY.join(" "))
}

fn dict(v: &PyValue) -> &sw_proto::pyjson::PyDict {
    match v {
        PyValue::Dict(d) => d,
        other => panic!("JSON 不是物件：{other:?}"),
    }
}

fn keys(v: &PyValue) -> BTreeSet<String> {
    dict(v).0.keys().cloned().collect()
}

fn fbool(v: &PyValue, key: &str) -> bool {
    match dict(v).get(key) {
        Some(PyValue::Bool(b)) => *b,
        other => panic!("JSON 欄位 {key} 不是布林：{other:?}"),
    }
}

fn fstr(v: &PyValue, key: &str) -> String {
    match dict(v).get(key) {
        Some(PyValue::Str(s)) => s.clone(),
        other => panic!("JSON 欄位 {key} 不是字串：{other:?}"),
    }
}

fn fint(v: &PyValue, key: &str) -> i128 {
    match dict(v).get(key) {
        Some(PyValue::Int(PyInt::Small(n))) => *n,
        other => panic!("JSON 欄位 {key} 不是整數：{other:?}"),
    }
}

fn set_of(items: &[&str]) -> BTreeSet<String> {
    items.iter().map(|s| s.to_string()).collect()
}

fn ran_end(t: &TestEnv) -> bool {
    t.calls().iter().any(|c| c.get(1).map(String::as_str) == Some("/End"))
}

fn ran_task_query(t: &TestEnv) -> bool {
    t.calls().iter().any(|c| c.first().map(String::as_str) == Some("powershell.exe"))
}

fn count_event(t: &TestEnv, label: &str) -> usize {
    t.event_order().count_prefix(label)
}

/// windows-task 的基本環境：config 為 windows-task、監管命令走真 `WindowsTaskCtl`（argv 落在 `FakeSysEnv`）。
fn wintask() -> TestEnv {
    TestEnv::windows()
        .with_config("supervision_mode: windows-task\n")
        .with_real_service()
}

/// daemon 收到 `daemon.stop` 後再存活 `delay`（假時鐘）才結束：結束時關閉監聽並使行程不存活。
fn exits_after_stop_rpc(t: &TestEnv, delay: Duration) {
    let ep = t.isolated_endpoint(TestEnv::win_endpoint());
    t.rpc().respond(&ep, "daemon.stop", r#"{"ok":true,"stopping":true}"#);
    let rpc = Arc::clone(t.rpc());
    let clock = Arc::clone(t.clock());
    let asked_at: Mutex<Option<Duration>> = Mutex::new(None);
    t.procs().alive_while(
        PID,
        Arc::new(move || {
            if rpc.request_count(&ep, "daemon.stop") == 0 {
                return true;
            }
            let now = clock.elapsed();
            let at = *asked_at
                .lock()
                .unwrap_or_else(|e| e.into_inner())
                .get_or_insert(now);
            if now >= at + delay {
                if !rpc.is_stopped(&ep) {
                    rpc.stop_endpoint(&ep);
                }
                return false;
            }
            true
        }),
    );
}

/// daemon 不理會 `daemon.stop`，只有 `/End` 執行後才結束；回傳「第一次觀察到 `/End` 時的假時鐘」。
fn exits_only_after_end(t: &TestEnv) -> Arc<Mutex<Option<Duration>>> {
    let ep = t.isolated_endpoint(TestEnv::win_endpoint());
    let rpc = Arc::clone(t.rpc());
    let clock = Arc::clone(t.clock());
    let env = t.sysenv();
    let first_seen = Arc::new(Mutex::new(None));
    let seen = Arc::clone(&first_seen);
    t.procs().alive_while(
        PID,
        Arc::new(move || {
            let ended = env
                .calls()
                .iter()
                .any(|c| c.get(1).map(String::as_str) == Some("/End"));
            if !ended {
                return true;
            }
            seen.lock()
                .unwrap_or_else(|e| e.into_inner())
                .get_or_insert(clock.elapsed());
            if !rpc.is_stopped(&ep) {
                rpc.stop_endpoint(&ep);
            }
            false
        }),
    );
    first_seen
}

/// RPC 連不上（connect 階段 ConnectionRefused）且排程工作查詢回 `state`。
fn unreachable_with_task_state(state: &str) -> TestEnv {
    wintask()
        .with_rpc_refused(TestEnv::win_endpoint())
        .with_command(&TASK_QUERY, 0, &format!("{state}\r\n"), "")
}

// ---------------------------------------------------------------------------
// 計畫表列
// ---------------------------------------------------------------------------

#[test]
fn stop_sends_rpc_before_end() {
    let t = wintask()
        .with_daemon_alive_forever(TestEnv::win_endpoint(), PID)
        .with_gone_ping_event();
    exits_after_stop_rpc(&t, Duration::from_secs(1));
    let (rc, out, err) = t.run(&["service", "stop"]);
    assert_eq!(rc, 0, "out={out} err={err}");
    let o = t.event_order();
    let (disable, stop, gone, enable) = (
        o.index_of(RUN_DISABLE),
        o.index_of(RPC_STOP),
        o.index_of(PING_GONE),
        o.index_of(RUN_ENABLE),
    );
    assert!(
        disable < stop && stop < gone && gone < enable,
        "期望 /DISABLE → daemon.stop → wait_stopped（ping 不通且行程消失）→ /ENABLE"
    );
    assert!(!ran_end(&t), "daemon 已正常結束時不得 /End");
    assert!(!ran_task_query(&t), "RPC 可達時不需查詢排程工作狀態：{:?}", t.calls());
    assert_eq!(count_event(&t, RUN_DISABLE), 1);
    assert_eq!(count_event(&t, RUN_ENABLE), 1);
    assert!(
        t.clock().elapsed() >= Duration::from_secs(1),
        "必須等到 daemon 實際結束（1 s）才 /ENABLE"
    );
    let j = json(&out);
    assert_eq!(keys(&j), set_of(&["action", "graceful", "mode", "ok"]), "P4 形狀：{out}");
    assert!(fbool(&j, "ok"));
    assert!(fbool(&j, "graceful"));
    assert_eq!(fstr(&j, "mode"), "windows-task");
    assert_eq!(fstr(&j, "action"), "stop");
}

#[test]
fn stop_daemon_unreachable_task_running_ends() {
    for (end_rc, want_ok) in [(0, true), (1, false)] {
        let end_err = if end_rc == 0 { "" } else { "ERROR: 工作目前不在執行中。\r\n" };
        let t = unreachable_with_task_state("Running").with_command(&END, end_rc, "", end_err);
        let (rc, out, err) = t.run(&["service", "stop"]);
        let o = t.event_order();
        let query = task_query_event();
        let (disable, q, end, enable) = (
            o.index_of(RUN_DISABLE),
            o.index_of(&query),
            o.index_of(RUN_END),
            o.index_of(RUN_ENABLE),
        );
        assert!(
            disable < q && q < end && end < enable,
            "end_rc={end_rc}：期望 /DISABLE → rpc（失敗）→ wait_stopped → 查詢 Running → /End → /ENABLE"
        );
        assert!(
            t.rpc().timeouts_s().contains(&2.0),
            "end_rc={end_rc}：必須先嘗試 RPC daemon.stop（固定 2.0 s）：{:?}",
            t.rpc().timeouts_s()
        );
        assert_eq!(count_event(&t, RUN_END), 1, "end_rc={end_rc}：/End 恰一次");
        let j = json(&out);
        assert_eq!(fbool(&j, "ok"), want_ok, "end_rc={end_rc}：ok 依 /End rc：{out}");
        assert_eq!(rc, if want_ok { 0 } else { 2 }, "end_rc={end_rc} out={out} err={err}");
        assert!(!fbool(&j, "graceful"), "end_rc={end_rc}：執行過 /End 即非 graceful");
        assert_eq!(fint(&j, "rc"), i128::from(end_rc), "end_rc={end_rc}：rc 為 /End 的 rc");
        assert_eq!(fstr(&j, "action"), "stop");
        assert_eq!(fstr(&j, "mode"), "windows-task");
    }
}

#[test]
fn stop_daemon_unreachable_task_not_running_no_end() {
    let t = unreachable_with_task_state("Ready");
    let (rc, out, err) = t.run(&["service", "stop"]);
    assert_eq!(rc, 0, "out={out} err={err}");
    let o = t.event_order();
    let query = task_query_event();
    assert!(
        o.index_of(RUN_DISABLE) < o.index_of(&query) && o.index_of(&query) < o.index_of(RUN_ENABLE),
        "RPC 不可達時必須在 /ENABLE 前查詢排程工作狀態"
    );
    assert!(t.rpc().timeouts_s().contains(&2.0), "必須先嘗試 RPC daemon.stop");
    assert!(!ran_end(&t), "工作未在執行時不得 /End：{:?}", t.calls());
    let j = json(&out);
    assert_eq!(keys(&j), set_of(&["action", "graceful", "mode", "ok"]), "P4 形狀：{out}");
    assert!(fbool(&j, "ok"));
    assert!(fbool(&j, "graceful"));
}

#[test]
fn stop_wait_timeout_then_end() {
    for (end_rc, want_ok) in [(0, true), (1, false)] {
        let end_err = if end_rc == 0 { "" } else { "ERROR: 存取被拒。\r\n" };
        let t = wintask()
            .with_daemon_alive_forever(TestEnv::win_endpoint(), PID)
            .with_command(&END, end_rc, "", end_err);
        let first_end = exits_only_after_end(&t);
        let (rc, out, err) = t.run(&["service", "stop"]);
        let o = t.event_order();
        assert!(
            o.index_of(RUN_DISABLE) < o.index_of(RPC_STOP)
                && o.index_of(RPC_STOP) < o.index_of(RUN_END)
                && o.index_of(RUN_END) < o.index_of(RUN_ENABLE),
            "end_rc={end_rc}：期望 /DISABLE → daemon.stop → 等待逾時 → /End → /ENABLE"
        );
        assert_eq!(count_event(&t, RUN_END), 1, "end_rc={end_rc}：/End 恰一次");
        assert!(!ran_task_query(&t), "end_rc={end_rc}：RPC 可達的逾時路徑不需查詢工作狀態");
        if want_ok {
            let seen = first_end
                .lock()
                .unwrap_or_else(|e| e.into_inner())
                .expect("/End 後必須再確認 daemon 已結束");
            assert!(seen >= Duration::from_secs(10), "/End 只能在 10 s 等待逾時之後：{seen:?}");
        }
        let j = json(&out);
        assert_eq!(fbool(&j, "ok"), want_ok, "end_rc={end_rc}：ok 依 /End rc：{out}");
        assert_eq!(rc, if want_ok { 0 } else { 2 }, "end_rc={end_rc} out={out} err={err}");
        assert!(!fbool(&j, "graceful"));
        assert_eq!(fint(&j, "rc"), i128::from(end_rc));
    }
}

#[test]
fn restart_is_graceful_stop_then_run() {
    let t = wintask()
        .with_daemon_alive_forever(TestEnv::win_endpoint(), PID)
        .with_gone_ping_event();
    exits_after_stop_rpc(&t, Duration::from_secs(1));
    let (rc, out, err) = t.run(&["service", "restart"]);
    assert_eq!(rc, 0, "out={out} err={err}");
    let o = t.event_order();
    let (disable, stop, gone, enable, run) = (
        o.index_of(RUN_DISABLE),
        o.index_of(RPC_STOP),
        o.index_of(PING_GONE),
        o.index_of(RUN_ENABLE),
        o.index_of(RUN_RUN),
    );
    assert!(
        disable < stop && stop < gone && gone < enable && enable < run,
        "期望 graceful_stop（/DISABLE → daemon.stop → wait_stopped）→ /ENABLE → /Run"
    );
    assert!(!ran_end(&t), "daemon 已正常結束時 restart 不得 /End");
    assert_eq!(count_event(&t, RUN_ENABLE), 1, "restart 只在 /Run 前 /ENABLE 一次");
    let j = json(&out);
    assert!(fbool(&j, "ok"));
    assert!(fbool(&j, "graceful"));
    assert_eq!(fstr(&j, "action"), "restart");
    assert_eq!(fstr(&j, "mode"), "windows-task");
}

#[test]
fn daemon_stop_cli_routes_to_service_in_wintask() {
    let run = |argv: &[&str]| {
        let t = wintask()
            .with_daemon_alive_forever(TestEnv::win_endpoint(), PID)
            .with_gone_ping_event();
        exits_after_stop_rpc(&t, Duration::from_secs(1));
        let (rc, out, err) = t.run(argv);
        let o = t.event_order();
        assert!(
            o.index_of(RUN_DISABLE) < o.index_of(RPC_STOP)
                && o.index_of(RPC_STOP) < o.index_of(PING_GONE)
                && o.index_of(PING_GONE) < o.index_of(RUN_ENABLE),
            "{argv:?}：期望 /DISABLE → daemon.stop → wait_stopped → /ENABLE"
        );
        assert!(!ran_end(&t), "{argv:?}：不得 /End");
        (rc, json(&out), t.calls(), err)
    };
    let (rc_d, j_d, calls_d, err_d) = run(&["daemon", "stop"]);
    let (rc_s, j_s, calls_s, _) = run(&["service", "stop"]);
    assert_eq!(rc_d, 0, "err={err_d}");
    assert_eq!(fstr(&j_d, "_routed_to"), "service stop");
    assert_eq!(calls_d, calls_s, "routed daemon stop 與 service stop 必須走同一停止序");
    let mut without_route = dict(&j_d).clone();
    without_route.0.shift_remove("_routed_to");
    assert_eq!(PyValue::Dict(without_route), j_s, "除 _routed_to 外輸出與 service stop 相同");
    assert_eq!(rc_d, rc_s);
}

#[test]
fn systemd_modes_unchanged() {
    let t = TestEnv::posix()
        .with_config("supervision_mode: systemd-user\n")
        .with_systemd(true)
        .with_real_service();
    let (rc, out, err) = t.run(&["service", "stop"]);
    assert_eq!(rc, 0, "out={out} err={err}");
    assert_eq!(
        t.calls(),
        vec![vec!["systemctl".to_string(), "--user".into(), "stop".into(), "serialwrap".into()]],
        "systemd-user 只執行 systemctl --user stop（Python 形狀不變）"
    );
    assert!(t.rpc().requests().is_empty(), "systemd 模式不送停止 RPC：{:?}", t.rpc().requests());
    let j = json(&out);
    assert_eq!(
        keys(&j),
        set_of(&["action", "mode", "ok", "rc", "stderr", "stdout"]),
        "Python service_action 形狀：{out}"
    );
    assert_eq!(fstr(&j, "mode"), "systemd-user");
}

// ---------------------------------------------------------------------------
// 表外：RPC 不可達且查不到工作狀態
// ---------------------------------------------------------------------------

/// `TaskQuery::QueryFailed` 永不視為已停止：不 `/End`（規則只允許 Running 時硬停），也不得回報健康的 stop；
/// `/ENABLE` 照常還原，輸出沿用 P4 `ServiceResult` 形狀（`INTERNAL_ERROR`＋hint），不新增欄位。
#[test]
fn stop_daemon_unreachable_task_query_failed_reports_failure() {
    let t = wintask()
        .with_rpc_refused(TestEnv::win_endpoint())
        .with_command(&TASK_QUERY, 3, "", "存取被拒\r\n");
    let (rc, out, err) = t.run(&["service", "stop"]);
    assert_eq!(rc, 2, "out={out} err={err}");
    assert!(!ran_end(&t), "工作狀態未知時不得 /End");
    let o = t.event_order();
    let query = task_query_event();
    assert!(o.index_of(RUN_DISABLE) < o.index_of(&query) && o.index_of(&query) < o.index_of(RUN_ENABLE));
    let j = json(&out);
    assert!(!fbool(&j, "ok"));
    assert!(!fbool(&j, "graceful"));
    assert_eq!(fstr(&j, "error_code"), "INTERNAL_ERROR");
    assert!(fstr(&j, "hint").contains("排程工作"), "hint 須指出無法確認排程工作狀態：{out}");
    assert_eq!(fstr(&j, "action"), "stop");
}
