//! P8 Task 14：更新／回滾守衛（P4 `safety_check`，`crates/sw-cli/src/rollback.rs`）對 Rust daemon 的驗證與 fail-closed 補強（SC-23）。
//!
//! 計畫 09 Task 14 Step 1 的 CLI 列：
//! - busy 條件含 interactive lease（`interactive_session_id` 非 null，背景 capture 不列入）；
//! - daemon 健康判定三態：**確認不存在**（連線被拒或端點不存在，且 `ProcScan` 找不到 `serialwrapd`）→ 放行；
//!   **健康** → 照常檢查；**未知**（RPC 逾時、壞回應、連線成功但回應不可解析、行程存在但 RPC 不通）→ 拒絕，
//!   探測 RPC 的碼為 `TIMEOUT`／`INVALID_RESPONSE`／`SOCKET_ERROR`（detail `probe:<method>`），行程在但 RPC 不通回
//!   `DEVICE_STILL_HELD`（detail `pids`），兩者 detail 皆帶 `health:"unknown"` 與 `reason`；
//! - `--force` 對 busy、健康三態、身分確認與步驟 3 交接全部無效：拒絕結果逐欄相同，只多 `force_ignored:true`，
//!   不得 disable／stop 監管、不得送 `daemon.stop`。
//!
//! stub daemon＝`FakeRpcConnector`（逐 method 腳本化回應、recv 逾時、連線拒絕）；監管呼叫序取自 `FakeServiceCtl`
//! 與 `SysEnv` 替身（`/Change /DISABLE`、`/End`、`systemctl stop`）。

mod support;

use support::*;
use sw_pal::PalErrorKind;
use sw_proto::pyjson::PyValue;

const IDLE_LIST: &str = "{\"ok\":true,\"sessions\":[{\"com\":\"COM0\",\"state\":\"READY\",\"foreground_busy\":false,\"outstanding_commands\":0,\"interactive_session_id\":null,\"recovering\":false}]}";
const FLASHING_LIST: &str = "{\"ok\":true,\"sessions\":[{\"com\":\"COM0\",\"state\":\"FLASHING\",\"foreground_busy\":false,\"outstanding_commands\":0,\"interactive_session_id\":null,\"recovering\":false}]}";
const FG_BUSY_LIST: &str = "{\"ok\":true,\"sessions\":[{\"com\":\"COM0\",\"state\":\"READY\",\"foreground_busy\":false,\"outstanding_commands\":0,\"interactive_session_id\":null,\"recovering\":false},{\"com\":\"COM1\",\"state\":\"READY\",\"foreground_busy\":true,\"outstanding_commands\":0,\"interactive_session_id\":null,\"recovering\":false}]}";
const LEASE_LIST: &str = "{\"ok\":true,\"sessions\":[{\"com\":\"COM0\",\"state\":\"READY\",\"foreground_busy\":false,\"outstanding_commands\":0,\"interactive_session_id\":\"i-1\",\"recovering\":false}]}";
const FLASH_MSG: &str = "flash 進行中，拒絕更新／回滾（--force 不適用，請待 flash 完成後重試）";

/// 拒絕情境（計畫 `guard_force_does_not_override` 的參數化列）。
#[derive(Clone, Copy, Debug)]
enum Refusal {
    Flashing,
    Foreground,
    InteractiveLease,
    RpcTimeout,
    MalformedResponse,
    ProcessAliveRpcDown,
}

const REFUSALS: [Refusal; 6] = [
    Refusal::Flashing,
    Refusal::Foreground,
    Refusal::InteractiveLease,
    Refusal::RpcTimeout,
    Refusal::MalformedResponse,
    Refusal::ProcessAliveRpcDown,
];

/// 執行入口：update（bundle 0.4.1 更新 Current 0.4.0）或 rollback（Current 0.4.1 → Previous 0.4.0）。
#[derive(Clone, Copy, Debug, PartialEq, Eq)]
enum Entry {
    Update,
    Rollback,
}

impl Entry {
    fn key(self) -> &'static str {
        match self {
            Entry::Update => "update",
            Entry::Rollback => "rollback",
        }
    }

    fn current(self) -> &'static str {
        match self {
            Entry::Update => "0.4.0",
            Entry::Rollback => "0.4.1",
        }
    }
}

fn base(entry: Entry) -> TestEnv {
    let t = TestEnv::windows()
        .with_supervisor_state("windows-task", false, false)
        .with_install_root()
        .with_installed("0.4.0");
    match entry {
        Entry::Update => t.with_current("0.4.0", None).with_bundle("0.4.1"),
        Entry::Rollback => t.with_installed("0.4.1").with_current("0.4.1", Some("0.4.0")),
    }
}

fn healthy(t: TestEnv, list: &str, flashing: bool) -> TestEnv {
    let ep = TestEnv::win_endpoint();
    t.with_rpc_ok(ep, "health.ping", "{\"ok\":true,\"pong\":true}")
        .with_rpc_ok(ep, "mcu.status", &format!("{{\"ok\":true,\"flashing\":{flashing}}}"))
        .with_rpc_ok(ep, "session.list", list)
}

fn scenario(entry: Entry, refusal: Refusal) -> TestEnv {
    let ep = TestEnv::win_endpoint();
    let t = base(entry);
    match refusal {
        Refusal::Flashing => healthy(t, FLASHING_LIST, true),
        Refusal::Foreground => healthy(t, FG_BUSY_LIST, false),
        Refusal::InteractiveLease => healthy(t, LEASE_LIST, false),
        Refusal::RpcTimeout => t
            .with_rpc_ok(ep, "health.ping", "{\"ok\":true,\"pong\":true}")
            .with_rpc_ok(ep, "mcu.status", "{\"ok\":true,\"flashing\":false}")
            .with_rpc_recv_timeout(ep, "session.list"),
        // stubd 對每個探測都回 `{not json`（連線成功但回應不可解析）。
        Refusal::MalformedResponse => t
            .with_rpc_ok(ep, "health.ping", "{not json")
            .with_rpc_ok(ep, "mcu.status", "{not json")
            .with_rpc_ok(ep, "session.list", "{not json"),
        Refusal::ProcessAliveRpcDown => t
            .with_rpc_refused(ep)
            .with_procs(vec![DaemonProc::win(4242, ep)]),
    }
}

fn argv(entry: Entry, force: bool) -> Vec<&'static str> {
    let mut a = vec!["setup"];
    if entry == Entry::Rollback {
        a.push("--rollback");
    }
    if force {
        a.push("--force");
    }
    a
}

fn run(t: &TestEnv, entry: Entry, force: bool) -> (i32, PyValue, String) {
    let (rc, out, err) = match entry {
        Entry::Update => t.run_from_bundle(&argv(entry, force)),
        Entry::Rollback => t.run(&argv(entry, force)),
    };
    (rc, json(&out), err)
}

fn opt<'a>(j: &'a PyValue, key: &str) -> Option<&'a PyValue> {
    match j {
        PyValue::Dict(d) => d.get(key),
        _ => None,
    }
}

fn steps(j: &PyValue, entry: Entry) -> Vec<PyValue> {
    j[entry.key()]["steps"].as_array().cloned().unwrap_or_default()
}

fn step_names(j: &PyValue, entry: Entry) -> Vec<String> {
    steps(j, entry)
        .iter()
        .map(|s| s["step"].as_str().unwrap_or_default().to_string())
        .collect()
}

/// 監管與 daemon 皆未被觸碰：無 disable／stop／`/End`／`systemctl stop`、無停止或交接 RPC、Current 未變。
fn assert_untouched(t: &TestEnv, entry: Entry, what: &str) {
    assert!(t.service().actions().is_empty(), "{what}：不得 disable／stop 監管：{:?}", t.service().actions());
    for call in t.calls() {
        let joined = call.join(" ");
        assert!(
            !joined.contains("/Change") && !joined.contains("/DISABLE") && !joined.contains("/End"),
            "{what}：不得停用或結束排程工作：{joined}"
        );
        assert!(!(joined.contains("systemctl") && joined.contains("stop")), "{what}：不得 systemctl stop：{joined}");
    }
    assert!(
        t.rpc().requests().iter().all(|r| r.1 != "daemon.stop" && r.1 != "daemon.handoff_stop"),
        "{what}：不得要求 daemon 停止或交接：{:?}",
        t.rpc().requests()
    );
    assert_eq!(
        sw_cli::install_layout::read_current(&t.root()).unwrap().unwrap().bundle_version,
        entry.current(),
        "{what}：current-release.json 不得改變"
    );
}

/// 拒絕結果的比對欄位（`--force` 前後須逐欄相同）。
fn refusal_fields(j: &PyValue) -> Vec<(String, Option<PyValue>)> {
    ["ok", "error_code", "phase", "busy", "probe", "pids", "health", "reason", "message", "holder_unknown"]
        .iter()
        .map(|k| (k.to_string(), opt(j, k).cloned()))
        .collect()
}

#[test]
fn guard_flashing_blocks_rollback() {
    let t = scenario(Entry::Rollback, Refusal::Flashing);
    let (rc, j, err) = run(&t, Entry::Rollback, false);
    assert_eq!(rc, 2);
    assert_eq!((s(&j, "error_code"), s(&j, "phase")), ("FLASHING_BUSY", "update_safety_check"));
    assert_eq!(s(&j, "message"), FLASH_MSG);
    assert!(err.starts_with("serialwrap: setup failed: FLASHING_BUSY: "), "{err}");
    assert_eq!(step_names(&j, Entry::Rollback).last().map(String::as_str), Some("safety_check"));
    assert_untouched(&t, Entry::Rollback, "flashing");
}

#[test]
fn guard_foreground_blocks_rollback() {
    let t = scenario(Entry::Rollback, Refusal::Foreground);
    let (rc, j, _) = run(&t, Entry::Rollback, false);
    assert_eq!(rc, 2);
    assert_eq!((s(&j, "error_code"), s(&j, "phase")), ("SESSION_BUSY", "update_safety_check"));
    let busy = j["busy"].as_array().expect("detail busy");
    assert!(
        busy.iter().any(|row| row["com"].as_str() == Some("COM1") && row["foreground_busy"] == PyValue::Bool(true)),
        "busy 須含 COM1：{busy:?}"
    );
    assert_untouched(&t, Entry::Rollback, "foreground");
}

#[test]
fn guard_interactive_lease_blocks_rollback() {
    let t = scenario(Entry::Rollback, Refusal::InteractiveLease);
    let (rc, j, _) = run(&t, Entry::Rollback, false);
    assert_eq!(rc, 2);
    assert_eq!((s(&j, "error_code"), s(&j, "phase")), ("SESSION_BUSY", "update_safety_check"));
    let busy = j["busy"].as_array().expect("detail busy");
    assert_eq!(busy.len(), 1, "{busy:?}");
    assert_eq!(busy[0]["com"].as_str(), Some("COM0"));
    assert_eq!(busy[0]["foreground_busy"], PyValue::Bool(false));
    assert_eq!(busy[0]["interactive_session_id"].as_str(), Some("i-1"), "detail 每筆帶 interactive_session_id");
    assert_untouched(&t, Entry::Rollback, "lease");
}

#[test]
fn guard_background_capture_alone_does_not_block() {
    // 背景 capture 為被動 RX、可安全中止，不列入 busy（計畫 Task 14 行為變更）：閒置 session 帶 background capture 仍放行。
    let ep = TestEnv::win_endpoint();
    let list = "{\"ok\":true,\"sessions\":[{\"com\":\"COM0\",\"state\":\"READY\",\"foreground_busy\":false,\"outstanding_commands\":0,\"interactive_session_id\":null,\"recovering\":false,\"background_capture_active\":true}]}";
    let t = base(Entry::Rollback)
        .with_supervisor_state("windows-task", true, true)
        .with_idle_daemon(ep, 4242)
        .with_rpc_ok(ep, "session.list", list);
    let (_, j, _) = run(&t, Entry::Rollback, false);
    let names = step_names(&j, Entry::Rollback);
    let at = names.iter().position(|n| n == "safety_check").expect("有 safety_check 步驟");
    assert_eq!(steps(&j, Entry::Rollback)[at]["ok"], PyValue::Bool(true), "{j:?}");
    assert_eq!(names.get(at + 1).map(String::as_str), Some("stop_supervisor"), "{names:?}");
}

#[test]
fn guard_rpc_timeout_refuses() {
    let t = scenario(Entry::Rollback, Refusal::RpcTimeout);
    let (rc, j, _) = run(&t, Entry::Rollback, false);
    assert_eq!(rc, 2);
    assert_eq!(
        (s(&j, "error_code"), s(&j, "phase"), s(&j, "probe"), s(&j, "health")),
        ("TIMEOUT", "update_safety_check", "session.list", "unknown")
    );
    assert!(!s(&j, "reason").is_empty(), "健康不明須帶 reason：{j:?}");
    assert_untouched(&t, Entry::Rollback, "session.list timeout");
}

#[test]
fn guard_ping_timeout_refuses() {
    // health.ping 連線成功但逾時、且 ProcScan 找不到 serialwrapd：健康不明，不得視為「確認不存在」。
    let ep = TestEnv::win_endpoint();
    // stubd 在線（已配置其他 method，`FakeRpcConnector` 才接受連線），只有 health.ping recv 逾時。
    let t = base(Entry::Rollback)
        .with_rpc_ok(ep, "mcu.status", "{\"ok\":true,\"flashing\":false}")
        .with_rpc_recv_timeout(ep, "health.ping");
    let (rc, j, _) = run(&t, Entry::Rollback, false);
    assert_eq!(rc, 2, "{j:?}");
    assert_eq!(
        (s(&j, "error_code"), s(&j, "phase"), s(&j, "probe"), s(&j, "health")),
        ("TIMEOUT", "update_safety_check", "health.ping", "unknown")
    );
    assert!(!s(&j, "reason").is_empty(), "{j:?}");
    assert_eq!(step_names(&j, Entry::Rollback).last().map(String::as_str), Some("safety_check"));
    assert_untouched(&t, Entry::Rollback, "ping timeout");
}

#[test]
fn guard_malformed_response_refuses() {
    let t = scenario(Entry::Rollback, Refusal::MalformedResponse);
    let (rc, j, _) = run(&t, Entry::Rollback, false);
    assert_eq!(rc, 2, "{j:?}");
    assert_eq!(
        (s(&j, "error_code"), s(&j, "phase"), s(&j, "probe"), s(&j, "health")),
        ("INVALID_RESPONSE", "update_safety_check", "health.ping", "unknown")
    );
    assert!(!s(&j, "reason").is_empty(), "{j:?}");
    assert_eq!(step_names(&j, Entry::Rollback).last().map(String::as_str), Some("safety_check"));
    assert_untouched(&t, Entry::Rollback, "malformed");
}

#[test]
fn guard_ping_socket_error_other_than_refused_refuses() {
    // 連線失敗但不是「被拒／端點不存在」（例如權限不足）：無法確認 daemon 不存在 → SOCKET_ERROR、健康不明。
    let ep = TestEnv::win_endpoint();
    let t = base(Entry::Rollback).with_rpc_refused_kind(ep, PalErrorKind::PermissionDenied);
    let (rc, j, _) = run(&t, Entry::Rollback, false);
    assert_eq!(rc, 2, "{j:?}");
    assert_eq!(
        (s(&j, "error_code"), s(&j, "phase"), s(&j, "probe"), s(&j, "health")),
        ("SOCKET_ERROR", "update_safety_check", "health.ping", "unknown")
    );
    assert_untouched(&t, Entry::Rollback, "permission denied");
}

#[test]
fn guard_daemon_absent_passes() {
    // 連線被拒或端點不存在，且無 serialwrapd 行程：確認不存在 → 放行進第 3 步。
    for kind in [PalErrorKind::ConnectionRefused, PalErrorKind::NotFound] {
        let t = base(Entry::Rollback)
            .with_supervisor_state("windows-task", true, false)
            .with_rpc_refused_kind(TestEnv::win_endpoint(), kind);
        let (_, j, _) = run(&t, Entry::Rollback, false);
        let names = step_names(&j, Entry::Rollback);
        let at = names.iter().position(|n| n == "safety_check").unwrap_or_else(|| panic!("{kind:?}：{j:?}"));
        assert_eq!(steps(&j, Entry::Rollback)[at]["ok"], PyValue::Bool(true), "{kind:?}：{j:?}");
        assert_eq!(names.get(at + 1).map(String::as_str), Some("stop_supervisor"), "{kind:?}：{names:?}");
        assert_ne!(opt(&j, "phase").and_then(PyValue::as_str), Some("update_safety_check"), "{kind:?}：{j:?}");
    }
}

#[test]
fn guard_process_alive_rpc_down_refuses() {
    let t = scenario(Entry::Rollback, Refusal::ProcessAliveRpcDown);
    let (rc, j, _) = run(&t, Entry::Rollback, false);
    assert_eq!(rc, 2);
    assert_eq!((s(&j, "error_code"), s(&j, "phase")), ("DEVICE_STILL_HELD", "update_safety_check"));
    assert_eq!(j["pids"][0], PyValue::from(4242i64), "{j:?}");
    assert_eq!(s(&j, "health"), "unknown", "{j:?}");
    assert!(!s(&j, "reason").is_empty(), "{j:?}");
    assert_untouched(&t, Entry::Rollback, "process alive");
}

#[test]
fn guard_force_does_not_override() {
    for entry in [Entry::Update, Entry::Rollback] {
        for refusal in REFUSALS {
            let what = format!("{entry:?}／{refusal:?}");
            let plain = scenario(entry, refusal);
            let (rc0, j0, _) = run(&plain, entry, false);
            let forced = scenario(entry, refusal);
            let (rc1, j1, _) = run(&forced, entry, true);
            assert_eq!((rc0, rc1), (2, 2), "{what}：{j0:?} {j1:?}");
            assert_eq!(s(&j1, "phase"), "update_safety_check", "{what}");
            assert_eq!(refusal_fields(&j0), refusal_fields(&j1), "{what}：--force 不得改變拒絕結果");
            let last0 = steps(&j0, entry).last().cloned().expect("steps");
            let last1 = steps(&j1, entry).last().cloned().expect("steps");
            assert_eq!(last1["step"].as_str(), Some("safety_check"), "{what}：steps 止於 safety_check");
            assert_eq!(last1["detail"]["force_ignored"], PyValue::Bool(true), "{what}");
            assert!(opt(&last1["detail"], "skipped").is_none(), "{what}：不得出現 skipped");
            assert!(opt(&last0["detail"], "force_ignored").is_none(), "{what}：未帶 --force 不記 force_ignored");
            assert_eq!(step_names(&j0, entry), step_names(&j1, entry), "{what}");
            assert_untouched(&plain, entry, &what);
            assert_untouched(&forced, entry, &what);
        }
    }
}

#[test]
fn guard_force_recheck_after_disable_still_refuses() {
    let ep = TestEnv::win_endpoint();
    let t = TestEnv::windows()
        .with_config("supervision_mode: windows-task\n")
        .with_install_root()
        .with_installed("0.4.0")
        .with_installed("0.4.1")
        .with_current("0.4.1", Some("0.4.0"))
        .with_supervisor_state("windows-task", true, true)
        .with_idle_daemon(ep, 4242)
        .with_rpc_ok(ep, "session.list", IDLE_LIST)
        .with_rpc_ok(
            ep,
            "daemon.handoff_stop",
            "{\"ok\":false,\"error_code\":\"FLASHING_BUSY\",\"busy\":[{\"com\":\"COM0\",\"state\":\"FLASHING\",\"foreground_busy\":false,\"outstanding_commands\":0,\"interactive_session_id\":null,\"recovering\":false,\"ops\":[\"flash.sync_probe\"]}]}",
        );
    let (rc, j, _) = run(&t, Entry::Rollback, true);
    assert_eq!(rc, 2, "{j:?}");
    assert_eq!((s(&j, "error_code"), s(&j, "phase")), ("FLASHING_BUSY", "update_safety_check"));
    assert_eq!(s(&j, "handoff"), "refused");
    let acts: Vec<String> = t.service().actions().into_iter().map(|a| a.1).collect();
    assert!(acts.iter().any(|a| a == "release_autostart:true"), "set_enabled(true) 須已呼叫：{acts:?}");
    assert!(t.rpc().requests().iter().all(|r| r.1 != "daemon.stop"), "不得送一般 daemon.stop");
    assert_eq!(
        t.rpc().requests().iter().filter(|r| r.1 == "daemon.handoff_stop").count(),
        1,
        "交接只問一次"
    );
    assert!(!t.calls().iter().any(|c| c.get(1).map(String::as_str) == Some("/End")), "不得硬停");
    assert_eq!(sw_cli::install_layout::read_current(&t.root()).unwrap().unwrap().bundle_version, "0.4.1");
}

#[test]
fn guard_daemon_absent_force_same_as_without() {
    for entry in [Entry::Update, Entry::Rollback] {
        let make = || {
            base(entry)
                .with_supervisor_state("windows-task", true, false)
                .with_rpc_refused(TestEnv::win_endpoint())
        };
        let (t0, t1) = (make(), make());
        let (rc0, j0, _) = run(&t0, entry, false);
        let (rc1, j1, _) = run(&t1, entry, true);
        assert_eq!(rc0, rc1, "{entry:?}：{j0:?} {j1:?}");
        assert_eq!(step_names(&j0, entry), step_names(&j1, entry), "{entry:?}：--force 不改變通過路徑");
        let names = step_names(&j1, entry);
        let at = names.iter().position(|n| n == "safety_check").expect("safety_check");
        assert_eq!(names.get(at + 1).map(String::as_str), Some("stop_supervisor"), "{entry:?}：{names:?}");
        assert_eq!(steps(&j1, entry)[at]["detail"]["force_ignored"], PyValue::Bool(true), "{entry:?}");
        assert_eq!(opt(&j0, "error_code"), opt(&j1, "error_code"), "{entry:?}");
    }
}

#[test]
fn setup_guard_message_unchanged() {
    // 一般 setup（非 update 路徑）flashing：Python 對等原文與 exit 2（`--force` 覆寫屬 Python 對等例外，不受本 Task 影響）。
    let t = TestEnv::posix().with_real_service();
    let ep = t.default_socket();
    let t = t
        .with_rpc_ok(&ep, "health.ping", "{\"ok\":true,\"pong\":true}")
        .with_rpc_ok(&ep, "mcu.status", "{\"ok\":true,\"flashing\":true}");
    let (rc, out, _) = t.run(&["setup", "--user"]);
    assert_eq!(rc, 2, "{out}");
    let j = json(&out);
    assert_eq!(s(&j, "error_code"), "FLASHING_BUSY");
    assert_eq!(s(&j, "message"), "flash 進行中，拒絕 setup（可用 --force 覆寫）");
}
