//! OD-48（含修訂）、S1X-c-1 與 D8 的行為測試：0.4.0 `setup` 遇到 0.3.x 舊 daemon 時的停止與還原。
//!
//! 規格：`docs/rust-refactor/reviews/od48-design-v2.md`（docs/parity-amendments 分支）D1～D10；
//! 裁決：`reviews/rulings/s1.md` 的 OD-48、OD-48 修訂、S1X-c-1。legacy 專用的中斷注入點另立清單
//! （`LEGACY_CRASH_POINTS`），不加進 `support::CRASH_POINTS` 的凍結迴圈。

mod support;

use support::*;
use sw_pal::service_ctl::{EnabledQuery, TaskQuery};
use sw_proto::pyjson::{PyInt, PyValue};

fn acts(t: &TestEnv) -> Vec<String> {
    t.service().actions().into_iter().map(|a| a.1).collect()
}

fn methods(t: &TestEnv) -> Vec<String> {
    t.rpc().requests().iter().map(|r| r.1.clone()).collect()
}

/// 已在跑、unit 卻是 disabled 的 systemd-user 監管（真實 systemd：`is-active` 為 active、`is-enabled` 為 disabled）。
fn disabled_but_running_user_unit(t: &TestEnv) {
    t.service().query_script(vec![TaskQuery::Running]);
    t.service().set_enabled_query(Some(EnabledQuery::Known(Some(false))));
}

#[test]
fn d8_disabled_running_unit_stays_disabled_after_update() {
    let t = TestEnv::posix()
        .with_install_root()
        .with_installed("0.4.0")
        .with_current("0.4.0", None)
        .with_bundle("0.4.1")
        .with_systemd(true)
        .with_var("USER", "alice")
        .with_supervisor_state("systemd-user", false, true)
        .with_idle_daemon_default(4242)
        .with_new_daemon_reporting_version("0.4.1");
    disabled_but_running_user_unit(&t);
    let (rc, out, _) = t.run_from_bundle(&["setup", "--user"]);
    assert_eq!(rc, 0, "{out}");
    let actions = acts(&t);
    assert!(
        !actions.iter().any(|a| a == "release_autostart:true" || a == "set_enabled:true"),
        "原本 disabled 的 unit 升級後不得被改成 enabled：{actions:?}"
    );
    assert!(actions.iter().any(|a| a == "release_autostart:false"), "{actions:?}");
}

#[test]
fn d8_disabled_running_unit_stays_disabled_after_refusal() {
    let t = TestEnv::posix()
        .with_install_root()
        .with_installed("0.4.0")
        .with_current("0.4.0", None)
        .with_bundle("0.4.1")
        .with_systemd(true)
        .with_var("USER", "alice")
        .with_supervisor_state("systemd-user", false, true)
        .with_idle_daemon_default(4242)
        .with_rpc_ok_default(
            "daemon.handoff_stop",
            "{\"ok\":false,\"error_code\":\"SESSION_BUSY\",\"busy\":[{\"com\":\"COM0\",\"state\":\"READY\",\"foreground_busy\":true,\"outstanding_commands\":0,\"interactive_session_id\":null,\"recovering\":false,\"ops\":[\"file.push\"]}]}",
        );
    disabled_but_running_user_unit(&t);
    let (rc, out, _) = t.run_from_bundle(&["setup", "--user"]);
    assert_eq!(rc, 2, "{out}");
    assert_eq!(s(&json(&out), "error_code"), "SESSION_BUSY");
    let actions = acts(&t);
    assert!(
        !actions.iter().any(|a| a == "release_autostart:true" || a == "set_enabled:true"),
        "拒絕後撤銷 hold 不得把 disabled unit 改成 enabled：{actions:?}"
    );
    assert!(!methods(&t).iter().any(|m| m == "daemon.stop"));
}

const SYS_SOCKET: &str = "/run/serialwrap/serialwrapd.sock";
const FLASHING_LIST: &str = "{\"ok\":true,\"sessions\":[{\"com\":\"COM0\",\"state\":\"FLASHING\",\"foreground_busy\":false,\"outstanding_commands\":0,\"interactive_session_id\":null,\"recovering\":false}]}";

fn system_daemon_flashing(t: TestEnv) -> TestEnv {
    t.with_supervisor_state("systemd-system", true, true)
        .with_rpc_ok(SYS_SOCKET, "health.ping", "{\"ok\":true,\"pid\":4242}")
        .with_rpc_ok(SYS_SOCKET, "health.status", "{\"ok\":true,\"pid\":4242,\"version\":\"0.4.0\"}")
        .with_rpc_ok(SYS_SOCKET, "mcu.status", "{\"flashing\":true,\"ok\":true}")
        .with_rpc_ok(SYS_SOCKET, "session.list", FLASHING_LIST)
        .with_procs(vec![DaemonProc {
            pid: 4242,
            socket: Some(SYS_SOCKET.into()),
            tree: vec![ProcIdentity { pid: 4242, start_ticks: Some(9) }],
        }])
}

/// S1X-c-1／D9(i)：systemd-system 的 daemon 在固定 socket 上 flash 中，update 必須偵測到並拒絕。
#[test]
fn s1x_c1_update_detects_systemd_system_daemon_on_fixed_socket() {
    let t = system_daemon_flashing(
        TestEnv::posix().with_install_root().with_installed("0.4.0").with_current("0.4.0", None).with_bundle("0.4.1"),
    );
    let (rc, out, _) = t.run_from_bundle(&["setup", "--system", "--with-sudo"]);
    assert_eq!(rc, 2, "{out}");
    let j = json(&out);
    assert_eq!((s(&j, "error_code"), s(&j, "phase")), ("FLASHING_BUSY", "update_safety_check"), "{out}");
    let current = sw_cli::install_layout::read_current(&t.root()).unwrap().unwrap();
    assert_eq!(current.bundle_version, "0.4.0");
    assert!(methods(&t).iter().all(|m| m != "daemon.handoff_stop" && m != "daemon.stop"));
    assert!(t.journal_json().is_none());
}

/// S1X-c-1／D9(i)：首次安裝（無 current-release.json）同樣以 systemd-system 固定 socket 偵測舊 daemon。
#[test]
fn s1x_c1_first_install_detects_systemd_system_daemon_on_fixed_socket() {
    let t = system_daemon_flashing(TestEnv::posix().with_install_root().with_bundle("0.4.1"));
    let (rc, out, _) = t.run_from_bundle(&["setup", "--system", "--with-sudo"]);
    assert_eq!(rc, 2, "{out}");
    let j = json(&out);
    assert_eq!((s(&j, "error_code"), s(&j, "phase")), ("FLASHING_BUSY", "update_safety_check"), "{out}");
    assert!(sw_cli::install_layout::read_current(&t.root()).unwrap().is_none());
    assert!(methods(&t).iter().all(|m| m != "daemon.handoff_stop" && m != "daemon.stop"));
}

/// D3.2：ping 不通改掃 /proc 時，找到 socket 不符的同 uid serialwrapd 必須 fail-closed（DEVICE_STILL_HELD），
/// 不得當成 daemon 不存在繼續。
#[test]
fn d3_2_foreign_socket_daemon_without_rpc_fails_closed() {
    let other = "/tmp/elsewhere/serialwrapd.sock";
    let t = TestEnv::posix()
        .with_install_root()
        .with_installed("0.4.0")
        .with_current("0.4.0", None)
        .with_bundle("0.4.1")
        .with_supervisor_state("on-demand", false, false)
        .with_procs(vec![DaemonProc {
            pid: 5151,
            socket: Some(other.into()),
            tree: vec![ProcIdentity { pid: 5151, start_ticks: Some(3) }],
        }]);
    let (rc, out, _) = t.run_from_bundle(&["setup", "--on-demand"]);
    assert_eq!(rc, 2, "{out}");
    let j = json(&out);
    assert_eq!((s(&j, "error_code"), s(&j, "phase")), ("DEVICE_STILL_HELD", "update_safety_check"), "{out}");
    assert!(out.contains("5151") && out.contains(other), "需附 pids 與 socket：{out}");
    assert_eq!(sw_cli::install_layout::read_current(&t.root()).unwrap().unwrap().bundle_version, "0.4.0");
}

/// D3.2：監管查詢顯示 running，卻沒有可 RPC 的 daemon → fail-closed。
#[test]
fn d3_2_supervisor_running_without_rpc_daemon_fails_closed() {
    let t = TestEnv::posix()
        .with_install_root()
        .with_installed("0.4.0")
        .with_current("0.4.0", None)
        .with_bundle("0.4.1")
        .with_systemd(true)
        .with_var("USER", "alice")
        .with_supervisor_state("systemd-user", true, true);
    let (rc, out, _) = t.run_from_bundle(&["setup", "--user"]);
    assert_eq!(rc, 2, "{out}");
    let j = json(&out);
    assert_eq!((s(&j, "error_code"), s(&j, "phase")), ("DEVICE_STILL_HELD", "update_safety_check"), "{out}");
    assert_eq!(sw_cli::install_layout::read_current(&t.root()).unwrap().unwrap().bundle_version, "0.4.0");
}

/// S1X-c-1：supervision-mode set 也以舊模式的 instance endpoint 做安全檢查（systemd-system 固定 socket）。
#[test]
fn s1x_c1_supervision_switch_detects_systemd_system_daemon_on_fixed_socket() {
    let t = system_daemon_flashing(TestEnv::posix().with_systemd(true));
    let cfg = t.config_bytes();
    let (rc, out, _) = t.run(&["supervision-mode", "set", "on-demand", "--with-sudo"]);
    assert_eq!(rc, 2, "{out}");
    let j = json(&out);
    assert_eq!((s(&j, "error_code"), s(&j, "phase")), ("FLASHING_BUSY", "supervision_set_safety_check"), "{out}");
    assert_eq!(t.config_bytes(), cfg);
    assert!(methods(&t).iter().all(|m| m != "daemon.handoff_stop" && m != "daemon.stop"));
}

// ───────── OD-48 legacy 停止（D9 (a)～(l)） ─────────

fn legacy_capture_path(t: &TestEnv) -> std::path::PathBuf {
    t.install_root().join("install-journal.legacy.json")
}

fn step<'a>(j: &'a PyValue, op: &str, name: &str) -> PyValue {
    let steps = match dict(j, op) {
        PyValue::Dict(d) => match d.get("steps") { Some(PyValue::List(items)) => items.clone(), _ => Vec::new() },
        _ => Vec::new(),
    };
    steps
        .into_iter()
        .find(|item| matches!(item, PyValue::Dict(d) if d.get("step") == Some(&PyValue::Str(name.into()))))
        .unwrap_or(PyValue::Null)
}

fn detail_of(step: &PyValue) -> PyValue {
    match step {
        PyValue::Dict(d) => d.get("detail").cloned().unwrap_or(PyValue::Null),
        _ => PyValue::Null,
    }
}

fn legacy_on_demand_install() -> TestEnv {
    let base = TestEnv::posix().with_install_root().with_bundle("0.4.0").with_supervisor_state("on-demand", false, false);
    let ep = base.default_socket();
    base.with_legacy_daemon(&ep, 4242).with_new_daemon_after_legacy("0.4.0")
}

/// D9(a)：on-demand 的 0.3.2 daemon → RPC daemon.stop → 切換成功 → 新 daemon 由 setup 啟動。
#[test]
fn legacy_a_on_demand_install_stops_with_rpc_and_setup_starts_new_daemon() {
    let t = legacy_on_demand_install().with_legacy_linger(Some(std::time::Duration::from_secs(40)));
    let (rc, out, _) = t.run_from_bundle(&["setup", "--on-demand"]);
    assert_eq!(rc, 0, "{out}");
    let j = json(&out);
    let stop = detail_of(&step(&j, "install", "stop_supervisor"));
    assert_eq!(s(&stop, "handoff"), "legacy_stopped", "{out}");
    let legacy = dict(&stop, "legacy_stop");
    assert_eq!((s(legacy, "method"), s(legacy, "from_version")), ("rpc_daemon_stop", "0.3.2"));
    assert_eq!(legacy.get_key("stability_window_ms"), Some(&PyValue::Int(PyInt::Small(5000))));
    let waited = match legacy.get_key("waited_ms") { Some(PyValue::Int(PyInt::Small(ms))) => *ms, other => panic!("{other:?}") };
    assert!(waited >= 45_000, "必須等到行程結束後再穩定 5 s（socket 消失不代表 UART 已釋放）：{waited}");
    assert_eq!(methods(&t).iter().filter(|m| *m == "daemon.stop").count(), 1);
    assert_eq!(t.process().spawn_count(), 1, "新 daemon 必須由 setup 自己啟動");
    let start = detail_of(&step(&j, "install", "start_and_verify"));
    assert_eq!(start.get_key("started"), Some(&PyValue::Bool(true)), "{out}");
    assert_eq!(sw_cli::install_layout::read_current(&t.root()).unwrap().unwrap().bundle_version, "0.4.0");
    assert!(t.journal_json().is_none());
    assert!(!legacy_capture_path(&t).exists(), "成功後擷取檔必須清除");
}

/// D9(a)／(g)：systemd-user unit 的 MainPID 相符 → 在 hold 之下 systemctl stop，不送 daemon.stop。
#[test]
fn legacy_a_systemd_user_main_pid_uses_systemctl_stop() {
    let base = TestEnv::posix().with_install_root().with_bundle("0.4.0").with_systemd(true).with_var("USER", "alice")
        .with_supervisor_state("systemd-user", true, true);
    let ep = base.default_socket();
    let t = base.with_legacy_daemon(&ep, 4242).with_legacy_main_pid("systemd-user").with_new_daemon_after_legacy("0.4.0");
    let (rc, out, _) = t.run_from_bundle(&["setup", "--user"]);
    assert_eq!(rc, 0, "{out}");
    let j = json(&out);
    let stop = detail_of(&step(&j, "install", "stop_supervisor"));
    let legacy = dict(&stop, "legacy_stop");
    assert_eq!(s(legacy, "method"), "systemctl_stop");
    assert!(methods(&t).iter().all(|m| m != "daemon.stop"), "MainPID 相符時不得改送 RPC");
    let actions = acts(&t);
    let hold = actions.iter().position(|a| a == "hold_autostart").expect("hold");
    let stop = actions.iter().position(|a| a == "stop").expect("systemctl stop");
    let start = actions.iter().rposition(|a| a == "start").expect("新版由 setup 啟動");
    assert!(hold < stop && stop < start, "{actions:?}");
    assert!(!actions.iter().any(|a| a == "hard_stop"), "不得硬殺：{actions:?}");
}

fn legacy_on_demand_update() -> TestEnv {
    let base = TestEnv::posix().with_install_root().with_installed("0.3.9").with_current("0.3.9", None).with_bundle("0.4.0")
        .with_supervisor_state("on-demand", false, false);
    let ep = base.default_socket();
    base.with_legacy_daemon(&ep, 4242)
}

fn assert_fail_closed_untouched(t: &TestEnv, out: &str) {
    let j = json(out);
    assert_eq!((s(&j, "error_code"), s(&j, "phase")), ("METHOD_NOT_FOUND", "update_safety_check"), "{out}");
    assert!(methods(t).iter().all(|m| m != "daemon.stop"), "fail-closed 不得送 daemon.stop");
    assert!(!acts(t).iter().any(|a| a == "stop" || a == "hard_stop"));
    assert!(t.journal_json().is_none() && !legacy_capture_path(t).exists());
    assert_eq!(t.service().hold_state_now(), HoldState::NotHeld);
}

/// D9(b)：METHOD_NOT_FOUND 帶 version 0.3.2，但行程是 bundle 下的原生執行檔（P5～P7 Rust daemon 也回 0.3.2）→ fail-closed。
#[test]
fn legacy_b_native_exe_in_bundle_path_fails_closed() {
    let t = legacy_on_demand_install();
    let native = t.install_root().join("versions/0.4.0/bin/serialwrapd").display().to_string();
    let t = t.with_legacy_details(|details| {
        details.exe = Some(native.clone());
        details.argv = vec![native.clone(), "--socket".into(), "x".into()];
    });
    let (rc, out, _) = t.run_from_bundle(&["setup", "--on-demand"]);
    assert_eq!(rc, 2, "{out}");
    assert_fail_closed_untouched(&t, &out);
    assert!(sw_cli::install_layout::read_current(&t.root()).unwrap().is_none());
}

/// D9(b)：Python 直譯器但 script 位於 `<install_root>/versions/`（bundle 管理的 daemon）→ fail-closed。
#[test]
fn legacy_b_python_script_under_versions_fails_closed() {
    let t = legacy_on_demand_install();
    let script = t.install_root().join("versions/0.3.9/py/serialwrapd.py").display().to_string();
    let t = t.with_legacy_details(|details| {
        details.argv = vec!["/usr/bin/python3".into(), "-I".into(), "-B".into(), script.clone()];
    });
    let (rc, out, _) = t.run_from_bundle(&["setup", "--on-demand"]);
    assert_eq!(rc, 2, "{out}");
    assert_fail_closed_untouched(&t, &out);
}

/// D1.2：版本 ≥ 0.4.0 的 METHOD_NOT_FOUND 不屬 legacy → fail-closed。
#[test]
fn legacy_b_version_not_below_040_fails_closed() {
    let t = legacy_on_demand_install();
    let ep = t.default_socket();
    let t = t
        .with_rpc_ok(&ep, "daemon.handoff_stop", "{\"ok\":false,\"error_code\":\"METHOD_NOT_FOUND\",\"version\":\"0.4.0\"}")
        .with_rpc_ok(&ep, "health.status", "{\"ok\":true,\"pid\":4242,\"version\":\"0.4.0\"}");
    let (rc, out, _) = t.run_from_bundle(&["setup", "--on-demand"]);
    assert_eq!(rc, 2, "{out}");
    assert_fail_closed_untouched(&t, &out);
}

/// D9(b)：版本取不到（拒絕回應與 health.status 皆無 version）→ fail-closed。
#[test]
fn legacy_b_unknown_version_fails_closed() {
    let t = legacy_on_demand_install();
    let ep = t.default_socket();
    let t = t
        .with_rpc_ok(&ep, "daemon.handoff_stop", "{\"ok\":false,\"error_code\":\"METHOD_NOT_FOUND\"}")
        .with_rpc_ok(&ep, "health.status", "{\"ok\":true,\"pid\":4242}");
    let (rc, out, _) = t.run_from_bundle(&["setup", "--on-demand"]);
    assert_eq!(rc, 2, "{out}");
    assert_fail_closed_untouched(&t, &out);
}

const BUSY_FG_LIST: &str = "{\"ok\":true,\"sessions\":[{\"com\":\"COM0\",\"state\":\"READY\",\"foreground_busy\":true,\"outstanding_commands\":1,\"interactive_session_id\":null,\"recovering\":false,\"attached_real_path\":\"/dev/ttyUSB0\"}]}";

/// D9(c)：停止前檢查（D3.3）發現 flash 進行中 → FLASHING_BUSY，未送任何 stop，hold 撤銷。
#[test]
fn legacy_c_flashing_at_precheck_refuses_without_stop() {
    let t = legacy_on_demand_install();
    let ep = t.default_socket();
    t.rpc().respond_seq(&t.isolated_endpoint(&ep), "mcu.status", &["{\"ok\":true,\"flashing\":false}", "{\"ok\":true,\"flashing\":true}"]);
    let (rc, out, _) = t.run_from_bundle(&["setup", "--on-demand"]);
    assert_eq!(rc, 2, "{out}");
    let j = json(&out);
    assert_eq!((s(&j, "error_code"), s(&j, "phase")), ("FLASHING_BUSY", "update_safety_check"), "{out}");
    assert_eq!(j.get_key("legacy_daemon_stopped"), Some(&PyValue::Bool(false)));
    assert!(methods(&t).iter().all(|m| m != "daemon.stop"));
    assert!(t.journal_json().is_none() && !legacy_capture_path(&t).exists());
    assert!(!t.autostart_lock_held());
}

/// D9(c)：停止前檢查發現 session 忙碌 → SESSION_BUSY 附明細，未送 stop（systemd：未送 systemctl stop、hold 撤銷）。
#[test]
fn legacy_c_session_busy_at_precheck_refuses_without_stop() {
    let base = TestEnv::posix().with_install_root().with_bundle("0.4.0").with_systemd(true).with_var("USER", "alice")
        .with_supervisor_state("systemd-user", true, true);
    let ep = base.default_socket();
    let t = base.with_legacy_daemon(&ep, 4242).with_legacy_main_pid("systemd-user");
    t.rpc().respond_seq(&t.isolated_endpoint(&ep), "session.list", &[LEGACY_SESSION_LIST, BUSY_FG_LIST]);
    let (rc, out, _) = t.run_from_bundle(&["setup", "--user"]);
    assert_eq!(rc, 2, "{out}");
    let j = json(&out);
    assert_eq!((s(&j, "error_code"), s(&j, "phase")), ("SESSION_BUSY", "update_safety_check"), "{out}");
    assert!(out.contains("\"busy\""), "需附忙碌明細：{out}");
    assert!(!acts(&t).iter().any(|a| a == "stop"), "{:?}", acts(&t));
    assert!(methods(&t).iter().all(|m| m != "daemon.stop"));
    assert_eq!(t.service().hold_state_now(), HoldState::NotHeld);
}

/// D9(d)：stop 後 330 s 仍未結束 → legacy_stop_pending；不重啟任何 daemon，journal 與擷取檔保留（含 pid＋start_ticks）。
#[test]
fn legacy_d_never_exits_is_pending_without_restart() {
    let base = TestEnv::posix().with_install_root().with_bundle("0.4.0").with_systemd(true).with_var("USER", "alice")
        .with_supervisor_state("systemd-user", true, true);
    let ep = base.default_socket();
    let t = base.with_legacy_daemon(&ep, 4242).with_legacy_main_pid("systemd-user").with_legacy_linger(None)
        .with_new_daemon_after_legacy("0.4.0");
    let (rc, out, _) = t.run_from_bundle(&["setup", "--user"]);
    assert_eq!(rc, 2, "{out}");
    let j = json(&out);
    assert_eq!((s(&j, "error_code"), s(&j, "phase"), s(&j, "reason")), ("TIMEOUT", "update_confirm_released", "legacy_stop_pending"), "{out}");
    let legacy = dict(&j, "legacy_daemon");
    assert_eq!(legacy.get_key("pid"), Some(&PyValue::Int(PyInt::Small(4242))));
    assert_eq!(legacy.get_key("start_ticks"), Some(&PyValue::Int(PyInt::Small(74242))));
    let waited = match j.get_key("waited_ms") { Some(PyValue::Int(PyInt::Small(ms))) => *ms, other => panic!("{other:?}") };
    assert!(waited >= 330_000, "等待上限 330 s：{waited}");
    assert!(!acts(&t).iter().any(|a| a == "start" || a == "hard_stop"), "不得重啟或硬殺：{:?}", acts(&t));
    assert_eq!(t.process().spawn_count(), 0);
    let journal = t.journal_json().expect("journal 保留");
    assert_eq!(s(dict(&journal, "legacy"), "stage"), "stop_sent");
    assert!(legacy_capture_path(&t).exists());
    assert_eq!(t.service().hold_state_now(), HoldState::Held, "hold 保留");
    // 下一次 recover：舊行程仍在 → 維持 pending，不動任何東西。
    let pending = t.recover().expect("journal 存在");
    assert_eq!((pending.ok, pending.reason.as_deref()), (false, Some("legacy_stop_pending")));
    assert!(t.journal_json().is_some());
    // 舊行程結束後 → 依 D6 還原，仍不啟動任何 daemon。
    t.procs().alive_while(4242, std::sync::Arc::new(|| false));
    let recovered = t.recover().expect("journal 存在");
    assert!(recovered.ok, "{recovered:?}");
    assert_eq!(recovered.detail.get("legacy_daemon_stopped"), Some(&PyValue::Bool(true)));
    assert!(t.journal_json().is_none() && !legacy_capture_path(&t).exists());
    assert!(!acts(&t).iter().any(|a| a == "start"), "{:?}", acts(&t));
    assert_eq!(t.process().spawn_count(), 0);
}

/// D9(e)：stop 後一再被重拉 → 再停，總停止次數超過 3 → RelaunchLoop；不重啟任何 daemon。
#[test]
fn legacy_e_relaunch_loop_after_three_stops() {
    let t = legacy_on_demand_install().with_legacy_relaunches(&[5001, 5002, 5003]);
    let (rc, out, _) = t.run_from_bundle(&["setup", "--on-demand"]);
    assert_eq!(rc, 2, "{out}");
    let j = json(&out);
    assert_eq!((s(&j, "error_code"), s(&j, "reason")), ("DEVICE_STILL_HELD", "relaunch_loop"), "{out}");
    assert_eq!(methods(&t).iter().filter(|m| *m == "daemon.stop").count(), 3, "總共最多停 3 次");
    assert_eq!(j.get_key("legacy_daemon_stopped"), Some(&PyValue::Bool(true)));
    assert!(s(&j, "restart_hint").contains("serialwrapd"), "{out}");
    assert_eq!(t.process().spawn_count(), 0);
    assert!(t.journal_json().is_none());
    assert!(sw_cli::install_layout::read_current(&t.root()).unwrap().is_none());
}

/// D9(e)：重拉一次 → 對它重做 D1＋D3 再停 → 成功，relaunch_count 為 1，擷取檔記錄兩筆。
#[test]
fn legacy_e_single_relaunch_is_restopped() {
    let t = legacy_on_demand_install().with_legacy_relaunches(&[5001]);
    let (rc, out, _) = t.run_from_bundle(&["setup", "--on-demand"]);
    assert_eq!(rc, 0, "{out}");
    let j = json(&out);
    let stop = detail_of(&step(&j, "install", "stop_supervisor"));
    assert_eq!(dict(&stop, "legacy_stop").get_key("relaunch_count"), Some(&PyValue::Int(PyInt::Small(1))));
    assert_eq!(methods(&t).iter().filter(|m| *m == "daemon.stop").count(), 2);
    assert_eq!(t.process().spawn_count(), 1);
}

/// D9(f)：legacy stop 後、切換後失敗（新 daemon 立即結束）→ 安裝還原，之後不再啟動任何 daemon，報告附啟動指令。
#[test]
fn legacy_f_post_switch_failure_restores_install_without_restart() {
    let t = legacy_on_demand_install().with_spawn_exits(1);
    let (rc, out, _) = t.run_from_bundle(&["setup", "--on-demand"]);
    assert_eq!(rc, 2, "{out}");
    let j = json(&out);
    assert_eq!(s(&j, "phase"), "update_start_and_verify", "{out}");
    assert!(b(&dict(&j, "install"), "reverted"), "{out}");
    assert_eq!(j.get_key("legacy_daemon_stopped"), Some(&PyValue::Bool(true)));
    assert_eq!(j.get_key("restored"), Some(&PyValue::Bool(true)));
    assert!(s(&j, "restart_hint").contains("/opt/legacy-home/.local/bin/serialwrapd"), "{out}");
    assert_eq!(t.process().spawn_count(), 1, "還原後不得再啟動任何 daemon");
    assert!(sw_cli::install_layout::read_current(&t.root()).unwrap().is_none());
    assert!(t.journal_json().is_none());
}

/// D9(f)：legacy stop 後、切換前失敗（confirm_released 發現 UART 仍被持有）→ 撤銷 hold，不啟動任何 daemon。
#[test]
fn legacy_f_pre_switch_failure_releases_hold_without_restart() {
    let base = TestEnv::posix().with_install_root().with_bundle("0.4.0").with_systemd(true).with_var("USER", "alice")
        .with_supervisor_state("systemd-user", true, true);
    let ep = base.default_socket();
    let t = base.with_legacy_daemon(&ep, 4242).with_legacy_main_pid("systemd-user").with_new_daemon_after_legacy("0.4.0");
    t.procs().uart_holders_script(vec![(Vec::new(), sw_pal::proc_scan::HolderScanStatus::Ok)]);
    t.procs().holders(vec![7777], sw_pal::proc_scan::HolderScanStatus::Ok);
    let (rc, out, _) = t.run_from_bundle(&["setup", "--user"]);
    assert_eq!(rc, 2, "{out}");
    let j = json(&out);
    assert_eq!((s(&j, "error_code"), s(&j, "phase")), ("DEVICE_STILL_HELD", "update_confirm_released"), "{out}");
    assert_eq!(j.get_key("legacy_daemon_stopped"), Some(&PyValue::Bool(true)));
    assert_eq!(s(&j, "restart_hint"), "systemctl --user start serialwrap");
    let actions = acts(&t);
    assert!(!actions.iter().any(|a| a == "start"), "不得啟動任何 daemon：{actions:?}");
    assert!(actions.iter().any(|a| a == "release_autostart:true"), "撤銷 hold 時還原原 enabled：{actions:?}");
    assert_eq!(t.service().hold_state_now(), HoldState::NotHeld);
}

/// D9(g)：systemd 模式但 MainPID 不是該 daemon（例如手動啟動）→ 改用 RPC daemon.stop。
#[test]
fn legacy_g_main_pid_mismatch_uses_rpc_stop() {
    let base = TestEnv::posix().with_install_root().with_bundle("0.4.0").with_systemd(true).with_var("USER", "alice")
        .with_supervisor_state("systemd-user", true, true);
    let ep = base.default_socket();
    let t = base.with_legacy_daemon(&ep, 4242).with_new_daemon_after_legacy("0.4.0");
    t.service().set_main_pid(&SupervisionMode::SystemdUser, Ok(Some(9999)));
    let (rc, out, _) = t.run_from_bundle(&["setup", "--user"]);
    assert_eq!(rc, 0, "{out}");
    let j = json(&out);
    let stop = detail_of(&step(&j, "install", "stop_supervisor"));
    assert_eq!(s(dict(&stop, "legacy_stop"), "method"), "rpc_daemon_stop");
    assert_eq!(methods(&t).iter().filter(|m| *m == "daemon.stop").count(), 1);
}

/// D9(h)：除目標外還有同 uid 的 serialwrapd → 停止前 fail-closed（附 pids 與 socket）。
#[test]
fn legacy_h_other_same_uid_daemon_fails_closed_before_stop() {
    let t = legacy_on_demand_install().with_other_serialwrapd(6060, Some("/tmp/other/serialwrapd.sock"), LEGACY_UID);
    let (rc, out, _) = t.run_from_bundle(&["setup", "--on-demand"]);
    assert_eq!(rc, 2, "{out}");
    let j = json(&out);
    assert_eq!((s(&j, "error_code"), s(&j, "reason")), ("DEVICE_STILL_HELD", "other_daemon_present"), "{out}");
    assert!(out.contains("6060") && out.contains("/tmp/other/serialwrapd.sock"), "{out}");
    assert!(methods(&t).iter().all(|m| m != "daemon.stop"));
    assert!(t.journal_json().is_none());
}

/// D3.2：他 uid 的 serialwrapd 排除在掃描之外並在報告標註，不阻擋升級。
#[test]
fn legacy_h_other_uid_daemon_is_noted_not_blocking() {
    let t = legacy_on_demand_install().with_other_serialwrapd(6061, Some("/run/other-user/serialwrapd.sock"), LEGACY_UID + 1);
    let (rc, out, _) = t.run_from_bundle(&["setup", "--on-demand"]);
    assert_eq!(rc, 0, "{out}");
    let j = json(&out);
    let stop = detail_of(&step(&j, "install", "stop_supervisor"));
    assert_eq!(dict(&stop, "legacy_stop").get_key("other_uid_pids"), Some(&PyValue::List(vec![PyValue::Int(PyInt::Small(6061))])));
}

/// D9(i)：systemd-system 固定 socket 上的 0.3.2 daemon（update 與 first-install）都被偵測、flash 中拒絕。
#[test]
fn legacy_i_systemd_system_fixed_socket_detected_for_update_and_install() {
    for update in [true, false] {
        let base = TestEnv::posix().with_install_root().with_bundle("0.4.0").with_supervisor_state("systemd-system", true, true);
        let base = if update { base.with_installed("0.3.9").with_current("0.3.9", None) } else { base };
        let t = base.with_legacy_daemon(SYS_SOCKET, 4242)
            .with_rpc_ok(SYS_SOCKET, "mcu.status", "{\"ok\":true,\"flashing\":true}");
        let (rc, out, _) = t.run_from_bundle(&["setup", "--system", "--with-sudo"]);
        assert_eq!(rc, 2, "{out}");
        let j = json(&out);
        assert_eq!((s(&j, "error_code"), s(&j, "phase")), ("FLASHING_BUSY", "update_safety_check"), "update={update} {out}");
        assert!(methods(&t).iter().all(|m| m != "daemon.stop" && m != "daemon.handoff_stop"));
    }
}

/// D9(k)：行程結束後的穩定窗內出現新 serialwrapd → 不算已停；視為重拉並再停。
#[test]
fn legacy_k_relaunch_inside_stability_window_is_not_stopped() {
    let t = legacy_on_demand_install().with_legacy_linger(Some(std::time::Duration::from_secs(2))).with_legacy_relaunches(&[5001]);
    // 5001 在第一次 daemon.stop 後 4 s 才出現（4242 已於 2 s 結束，穩定窗尚未滿 5 s）。
    let rpc = std::sync::Arc::clone(t.rpc());
    let clock = std::sync::Arc::clone(t.clock());
    let ep = t.isolated_endpoint(&t.default_socket());
    let seen = std::sync::Arc::new(std::sync::Mutex::new(None::<u64>));
    t.procs().alive_while(5001, std::sync::Arc::new(move || {
        let stops = rpc.request_count(&ep, "daemon.stop");
        if stops == 0 { return false; }
        let now = clock.mono_ns();
        let at = *seen.lock().unwrap().get_or_insert(now);
        stops < 2 && now.saturating_sub(at) >= 4_000_000_000
    }));
    let (rc, out, _) = t.run_from_bundle(&["setup", "--on-demand"]);
    assert_eq!(rc, 0, "{out}");
    let j = json(&out);
    let legacy = dict(&detail_of(&step(&j, "install", "stop_supervisor")), "legacy_stop").clone();
    assert_eq!(legacy.get_key("relaunch_count"), Some(&PyValue::Int(PyInt::Small(1))), "{out}");
    let waited = match legacy.get_key("waited_ms") { Some(PyValue::Int(PyInt::Small(ms))) => *ms, other => panic!("{other:?}") };
    assert!(waited >= 9_000, "重拉後必須重新計穩定窗：{waited}");
    assert_eq!(methods(&t).iter().filter(|m| *m == "daemon.stop").count(), 2);
}

/// D9(l)：legacy 專用中斷注入點（`support::LEGACY_CRASH_POINTS`，不屬凍結的 CRASH_POINTS 迴圈）。

#[test]
fn legacy_l_crash_points_are_separate_from_frozen_list() {
    for point in LEGACY_CRASH_POINTS {
        assert!(!CRASH_POINTS.contains(point), "{point} 不得加進凍結的 CRASH_POINTS");
    }
}

#[test]
fn legacy_l_crash_loop_recover_journal() {
    for point in LEGACY_CRASH_POINTS {
        let mut t = legacy_on_demand_update().with_legacy_linger(Some(std::time::Duration::from_secs(40))).with_legacy_crash_at(point);
        let cur0 = std::fs::read(t.root().current_file).unwrap();
        let (rc, _, _) = t.run_from_bundle(&["setup", "--on-demand"]);
        assert_eq!(rc, 137, "{point}");
        t.clear_crash();
        assert!(t.journal_json().is_some() && legacy_capture_path(&t).exists(), "{point}：中斷後 journal 與擷取檔保留");
        let stop_sent = methods(&t).iter().any(|m| m == "daemon.stop");
        assert_eq!(stop_sent, *point != "legacy_stop:after_capture", "{point}");
        if *point == "legacy_stop:after_stop_sent" {
            // stop 已送出但舊行程仍在收尾 → 不得還原或繼續。
            let pending = t.recover().expect("journal");
            assert_eq!(pending.reason.as_deref(), Some("legacy_stop_pending"), "{point}: {pending:?}");
            t.clock().advance(std::time::Duration::from_secs(60));
        }
        let recovered = t.recover().expect("journal");
        assert!(recovered.ok, "{point}: {recovered:?}");
        assert!(t.journal_json().is_none() && !legacy_capture_path(&t).exists(), "{point}");
        assert_eq!(std::fs::read(t.root().current_file).unwrap(), cur0, "{point}：current 回到升級前");
        assert_eq!(t.process().spawn_count(), 0, "{point}：recover 不得啟動任何 daemon");
        let stopped = *point != "legacy_stop:after_capture";
        assert_eq!(recovered.detail.get("legacy_daemon_stopped").cloned().unwrap_or(PyValue::Bool(false)), PyValue::Bool(stopped), "{point}: {recovered:?}");
        if !stopped {
            assert_eq!(methods(&t).iter().filter(|m| *m == "daemon.stop").count(), 0, "擷取後 stop 前中斷：舊 daemon 繼續服務");
        }
    }
}
