//! P4-T3 紅燈測試：`sw_pal::service_ctl`（systemd、windows-task、windows-service 與交接前監管拉起抑制）。
//!
//! 紅燈骨架的行為方法皆為 `unimplemented!("P4-T3")`，故本檔測試在實作前應全數以該訊息 panic。
//! 測試名稱與斷言取自 `docs/rust-refactor/plans/05-stage1b-rust-cli-mgmt.md` Task 3。
//! 兩個 `cap_supervisor_runtime__*` 測試標記 `#[ignore]`，只由擁有者授權的主機執行。
#![allow(dead_code, unused_imports)]

use std::path::{Path, PathBuf};
use std::sync::Arc;
use std::time::{Duration, Instant};

use sw_pal::fakes::*;
use sw_pal::service_ctl::*;
use sw_pal::traits::{Clock, SysEnv};
use sw_pal::PalErrorKind;

const PS_QUERY: &str = "try { (Get-ScheduledTask -TaskName 'serialwrap' -ErrorAction Stop).State.ToString() } catch { if ($_.CategoryInfo.Category -eq 'ObjectNotFound') { 'NotFound' } else { exit 3 } }";

const DROPIN_BYTES: &str = "# serialwrap update hold (R5-F1): removed by serialwrap setup\n[Unit]\nRefuseManualStart=yes\n\n[Service]\nRestart=no\n";
const HOLD_VERIFY: &str = "UnitFileState=disabled\nRefuseManualStart=yes\nRestart=no\n";
const RELEASE_OK: &str = "Restart=on-failure\nRefuseManualStart=no\n";

fn sm(s: &str) -> SupervisionMode {
    SupervisionMode::parse(s)
}

fn argv(v: &[&str]) -> Vec<String> {
    v.iter().map(|s| s.to_string()).collect()
}

fn ps_argv() -> Vec<&'static str> {
    vec!["powershell.exe", "-NoProfile", "-NonInteractive", "-Command", PS_QUERY]
}

fn user_dropin(home: &Path) -> PathBuf {
    home.join(".config/systemd/user/serialwrap.service.d/90-serialwrap-update-hold.conf")
}

/// 使用者 systemd 的 hold／release 腳本化回應（drop-in 寫入位於 `home` 下的真實暫存目錄）。
fn user_env(home: &Path, hold_verify: &str, release_verify: &str) -> Arc<FakeSysEnv> {
    FakeSysEnv::builder()
        .home(home)
        .command(&["systemctl", "--user", "show", "serialwrap", "-p", "Restart", "--value"], 0, "on-failure\n", "")
        .command(&["systemctl", "--user", "is-enabled", "serialwrap"], 0, "enabled\n", "")
        .command(
            &["systemctl", "--user", "show", "serialwrap", "-p", "Restart", "-p", "RefuseManualStart", "-p", "UnitFileState"],
            0,
            hold_verify,
            "",
        )
        .command(&["systemctl", "--user", "show", "serialwrap", "-p", "Restart", "-p", "RefuseManualStart"], 0, release_verify, "")
        .build()
}

// ───────── Task 3 行為表（代碼區塊內的測試） ─────────

#[test]
fn systemd_system_privileged_without_sudo_returns_needs_sudo_and_runs_nothing() {
    let env = FakeSysEnv::builder().build();
    let ctl = SystemdCtl::new(env.clone(), real_fs(), FakeClock::new());
    let r = ctl.action(&sm("systemd-system"), ServiceAction::Restart, false);
    assert!(!r.ok);
    assert_eq!(r.error_code, Some("NEEDS_SUDO"));
    assert_eq!(
        r.hint.as_deref(),
        Some("需 root：請執行 `sudo systemctl restart serialwrap`（或加 --with-sudo 讓本指令代跑）")
    );
    assert!(env.calls().is_empty());
}

#[test]
fn systemd_system_status_never_uses_sudo() {
    let env = FakeSysEnv::builder()
        .command(&["systemctl", "status", "serialwrap"], 3, "inactive", "")
        .build();
    let r = SystemdCtl::new(env.clone(), real_fs(), FakeClock::new()).action(&sm("systemd-system"), ServiceAction::Status, true);
    assert_eq!(env.calls(), vec![argv(&["systemctl", "status", "serialwrap"])]);
    assert_eq!((r.ok, r.rc), (false, Some(3)));
}

#[test]
fn task_xml_is_byte_exact_golden() {
    let xml = render_task_xml(&TaskXmlSpec {
        name: "serialwrap".into(),
        user_id: "HOST\\alice".into(),
        command: "C:\\Users\\alice\\AppData\\Local\\Programs\\serialwrap\\bin\\serialwrapd.exe".into(),
        arguments: "--socket tcp://127.0.0.1:48700".into(),
        working_dir: "C:\\Users\\alice\\AppData\\Local\\Programs\\serialwrap\\bin".into(),
        enabled: true,
    });
    assert_eq!(xml, include_str!("golden/serialwrap.task.xml"));
    assert!(xml.contains("<ExecutionTimeLimit>PT0S</ExecutionTimeLimit>"));
    let bytes = encode_utf16le_bom(&xml);
    assert_eq!(&bytes[..2], &[0xFF, 0xFE]);
}

#[test]
fn task_xml_disabled_for_update_hold() {
    // R5-F1：hold 期間重新註冊不得以 /Create /F 把工作重新啟用（golden 只差 Settings/Enabled 一行）
    let spec = |enabled: bool| TaskXmlSpec {
        name: "serialwrap".into(),
        user_id: "HOST\\alice".into(),
        command: "C:\\x\\serialwrapd.exe".into(),
        arguments: String::new(),
        working_dir: "C:\\x".into(),
        enabled,
    };
    let (on, off) = (render_task_xml(&spec(true)), render_task_xml(&spec(false)));
    assert!(off.contains("    <AllowStartOnDemand>true</AllowStartOnDemand>\r\n    <Enabled>false</Enabled>\r\n"));
    assert_eq!(
        on.replace(
            "<AllowStartOnDemand>true</AllowStartOnDemand>\r\n    <Enabled>true</Enabled>",
            "<AllowStartOnDemand>true</AllowStartOnDemand>\r\n    <Enabled>false</Enabled>"
        ),
        off
    );
    assert!(
        off.contains("<LogonTrigger>\r\n      <Enabled>true</Enabled>"),
        "觸發器本身不改；停用的是整個工作"
    );
}

#[test]
fn windows_task_install_writes_xml_then_creates_task() {
    let tmp = tempfile::tempdir().unwrap();
    let env = FakeSysEnv::builder().user_identity("HOST\\alice").build();
    let ctl = WindowsTaskCtl::new(env.clone(), real_fs(), FakeClock::new());
    let def_path = tmp.path().join("serialwrap.task.xml");
    let def_s = def_path.display().to_string();
    let recs = ctl.install(
        &SupervisorDefinition {
            mode: sm("windows-task"),
            name: "serialwrap".into(),
            exec: vec!["C:\\p\\bin\\serialwrapd.exe".into(), "--socket".into(), "tcp://127.0.0.1:48700".into()],
            working_dir: Some("C:\\p\\bin".into()),
            run_user: None,
            env: vec![],
            definition_path: Some(def_path.clone()),
            register_held: false,
        },
        false,
    );
    assert_eq!(
        recs.iter().map(|r| r.argv.clone()).collect::<Vec<_>>(),
        vec![argv(&["schtasks", "/Create", "/XML", def_s.as_str(), "/TN", "serialwrap", "/F"])]
    );
    let raw = std::fs::read(&def_path).unwrap();
    assert_eq!(&raw[..2], &[0xFF, 0xFE]);
}

#[test]
fn windows_task_stop_and_restart_are_not_composed_in_pal() {
    let env = FakeSysEnv::builder().build();
    let ctl = WindowsTaskCtl::new(env.clone(), real_fs(), FakeClock::new());
    for a in [ServiceAction::Stop, ServiceAction::Restart] {
        let r = ctl.action(&sm("windows-task"), a, false);
        assert_eq!((r.ok, r.error_code), (false, Some("INTERNAL_ERROR")));
    }
    assert!(env.calls().is_empty(), "PAL 不得自行 /End 或 /Run");
}

#[test]
fn windows_task_query_failure_is_never_stopped() {
    for (rc, out, err) in [(3, "", "Access is denied."), (0, "", ""), (0, "Garbage\r\n", "")] {
        let env = FakeSysEnv::builder().command(&ps_argv(), rc, out, err).build();
        let q = WindowsTaskCtl::new(env, real_fs(), FakeClock::new()).query(&sm("windows-task"));
        assert!(matches!(q, TaskQuery::QueryFailed(_)), "rc={rc} out={out:?} → {q:?}");
    }
}

// ───────── Task 3 行為表（表列案例） ─────────

#[test]
fn systemd_user_start_runs_user_scope() {
    let env = FakeSysEnv::builder().build();
    let r = SystemdCtl::new(env.clone(), real_fs(), FakeClock::new()).action(&sm("systemd-user"), ServiceAction::Start, false);
    assert_eq!(env.calls(), vec![argv(&["systemctl", "--user", "start", "serialwrap"])]);
    assert_eq!(r.ok, r.rc == Some(0));

    let env_fail = FakeSysEnv::builder()
        .command(&["systemctl", "--user", "start", "serialwrap"], 5, "", "Failed to start serialwrap.service")
        .build();
    let r2 = SystemdCtl::new(env_fail, real_fs(), FakeClock::new()).action(&sm("systemd-user"), ServiceAction::Start, false);
    assert_eq!(r2.ok, r2.rc == Some(0));
    assert!(!r2.ok);
}

#[test]
fn systemd_system_start_with_sudo_prefixes_sudo() {
    let env = FakeSysEnv::builder().build();
    let _ = SystemdCtl::new(env.clone(), real_fs(), FakeClock::new()).action(&sm("systemd-system"), ServiceAction::Start, true);
    assert_eq!(env.calls(), vec![argv(&["sudo", "systemctl", "start", "serialwrap"])]);
}

#[test]
fn on_demand_returns_no_systemd_shape() {
    let env = FakeSysEnv::builder().build();
    let r = SystemdCtl::new(env.clone(), real_fs(), FakeClock::new()).action(&sm("on-demand"), ServiceAction::Start, false);
    assert_eq!(r.error_code, Some("NO_SYSTEMD"));
    assert_eq!(
        r.hint.as_deref(),
        Some("目前為 on-demand 監管模式（無 systemd 服務）。使用 `serialwrap daemon start/stop`。")
    );
    assert_eq!(r.rc, None);
    assert!(env.calls().is_empty());
}

#[test]
fn unknown_mode_foo_returns_no_systemd() {
    let env = FakeSysEnv::builder().build();
    let r = SystemdCtl::new(env.clone(), real_fs(), FakeClock::new()).action(&sm("foo"), ServiceAction::Status, false);
    assert_eq!(r.mode, "foo");
    assert_eq!(r.error_code, Some("NO_SYSTEMD"));
    assert!(env.calls().is_empty());
}

#[test]
fn windows_task_start_status_hard_stop_argv() {
    let env = FakeSysEnv::builder().build();
    let ctl = WindowsTaskCtl::new(env.clone(), real_fs(), FakeClock::new());
    let w = sm("windows-task");
    let _ = ctl.action(&w, ServiceAction::Start, false);
    let _ = ctl.action(&w, ServiceAction::Status, false);
    let _ = ctl.hard_stop(&w, false);
    assert_eq!(
        env.calls(),
        vec![
            argv(&["schtasks", "/Run", "/TN", "serialwrap"]),
            argv(&["schtasks", "/Query", "/TN", "serialwrap", "/FO", "LIST", "/V"]),
            argv(&["schtasks", "/End", "/TN", "serialwrap"]),
        ]
    );
}

#[test]
fn windows_task_set_enabled_false() {
    let env = FakeSysEnv::builder().build();
    let recs = WindowsTaskCtl::new(env.clone(), real_fs(), FakeClock::new()).set_enabled(&sm("windows-task"), false, false);
    let want = vec![argv(&["schtasks", "/Change", "/TN", "serialwrap", "/DISABLE"])];
    assert_eq!(recs.iter().map(|r| r.argv.clone()).collect::<Vec<_>>(), want);
    assert_eq!(env.calls(), want);
}

#[test]
fn windows_task_query_ready_is_not_running() {
    let env_ready = FakeSysEnv::builder().command(&ps_argv(), 0, "Ready\r\n", "").build();
    let q = WindowsTaskCtl::new(env_ready, real_fs(), FakeClock::new()).query(&sm("windows-task"));
    assert_eq!(q, TaskQuery::NotRunning("Ready".into()));

    let env_queued = FakeSysEnv::builder().command(&ps_argv(), 0, "Queued\r\n", "").build();
    let q2 = WindowsTaskCtl::new(env_queued, real_fs(), FakeClock::new()).query(&sm("windows-task"));
    assert_eq!(q2, TaskQuery::Running);
}

#[test]
fn windows_task_query_object_not_found() {
    let env = FakeSysEnv::builder().command(&ps_argv(), 0, "NotFound\r\n", "").build();
    let q = WindowsTaskCtl::new(env, real_fs(), FakeClock::new()).query(&sm("windows-task"));
    assert_eq!(q, TaskQuery::NotFound);
}

#[test]
fn systemd_query_maps_is_active() {
    let q_argv = ["systemctl", "--user", "is-active", "serialwrap"];
    let mk = |rc: i32, out: &str| {
        let env = FakeSysEnv::builder().command(&q_argv, rc, out, "").build();
        SystemdCtl::new(env, real_fs(), FakeClock::new()).query(&sm("systemd-user"))
    };
    assert_eq!(mk(0, "active\n"), TaskQuery::Running);
    assert!(matches!(mk(3, "inactive\n"), TaskQuery::NotRunning(_)));
    assert!(matches!(mk(3, "failed\n"), TaskQuery::NotRunning(_)));
    assert_eq!(mk(4, ""), TaskQuery::NotFound);

    let env_missing = FakeSysEnv::builder()
        .command_err(&q_argv, PalErrorKind::NotFound, "No such file or directory")
        .build();
    let q = SystemdCtl::new(env_missing, real_fs(), FakeClock::new()).query(&sm("systemd-user"));
    assert!(matches!(q, TaskQuery::QueryFailed(_)), "執行檔不存在 → {q:?}");
}

#[test]
fn windows_service_start_without_sudo_needs_sudo() {
    let env = FakeSysEnv::builder().build();
    let r = WindowsServiceCtl::new(env.clone(), FakeClock::new()).action(&sm("windows-service"), ServiceAction::Start, false);
    assert!(!r.ok);
    assert_eq!(r.error_code, Some("NEEDS_SUDO"));
    assert_eq!(
        r.hint.as_deref(),
        Some("需系統管理員權限：請在系統管理員 PowerShell 執行 `sc.exe start serialwrap`（或加 --with-sudo 讓本指令代跑）")
    );
    assert!(env.calls().is_empty());
}

#[test]
fn windows_service_install_never_executes() {
    let env = FakeSysEnv::builder().build();
    let ctl = WindowsServiceCtl::new(env.clone(), FakeClock::new());
    let def = SupervisorDefinition {
        mode: sm("windows-service"),
        name: "serialwrap".into(),
        exec: vec!["C:\\p\\bin\\serialwrapd.exe".into(), "--socket".into(), "tcp://127.0.0.1:48700".into()],
        working_dir: None,
        run_user: None,
        env: vec![],
        definition_path: None,
        register_held: false,
    };
    assert!(ctl.install(&def, false).is_empty());
    assert!(env.calls().is_empty());
    assert_eq!(
        ctl.install_plan(&def),
        vec![
            argv(&[
                "sc.exe",
                "create",
                "serialwrap",
                "binPath=",
                "\"C:\\p\\bin\\serialwrapd.exe\" --socket tcp://127.0.0.1:48700",
                "start=",
                "auto",
                "obj=",
                "NT AUTHORITY\\LocalService",
            ]),
            argv(&["sc.exe", "failure", "serialwrap", "reset=", "86400", "actions=", "restart/60000/restart/60000/restart/60000"]),
            argv(&["sc.exe", "start", "serialwrap"]),
        ]
    );
}

#[test]
fn windows_service_query_parses_state_code() {
    let q = |rc: i32, out: &str| {
        let env = FakeSysEnv::builder().command(&["sc.exe", "query", "serialwrap"], rc, out, "").build();
        WindowsServiceCtl::new(env, FakeClock::new()).query(&sm("windows-service"))
    };
    assert_eq!(
        q(0, "        STATE              : 1  STOPPED\r\n"),
        TaskQuery::NotRunning("STOPPED".into())
    );
    assert_eq!(q(0, "        STATE              : 4  RUNNING\r\n"), TaskQuery::Running);
    assert_eq!(q(1060, ""), TaskQuery::NotFound);
    assert!(matches!(q(5, "Access is denied.\r\n"), TaskQuery::QueryFailed(_)));
}

/// `contracts/golden/windows_cmdline.ndjson` 的一行：`args`（輸入）、`joined`（Windows Python `list2cmdline`）、`split`（原生 `CommandLineToArgvW`）。
struct GoldenCmdline {
    args: Vec<String>,
    joined: String,
    split: Vec<String>,
}

/// 最小 JSON 解析器：golden 由 `json.dumps(ensure_ascii=True)` 產生，只含字串與字串陣列物件。
struct MiniJson<'a> {
    s: &'a [u8],
    i: usize,
}

impl MiniJson<'_> {
    fn peek(&self) -> u8 {
        self.s.get(self.i).copied().unwrap_or(0)
    }

    fn expect(&mut self, c: u8) {
        assert_eq!(self.peek(), c, "位置 {} 預期 {:?}", self.i, c as char);
        self.i += 1;
    }

    fn string(&mut self) -> String {
        self.expect(b'"');
        let mut units: Vec<u16> = Vec::new();
        loop {
            let c = self.peek();
            self.i += 1;
            match c {
                b'"' => break,
                0 => panic!("字串未結束"),
                b'\\' => {
                    let e = self.peek();
                    self.i += 1;
                    match e {
                        b'"' => units.push(u16::from(b'"')),
                        b'\\' => units.push(u16::from(b'\\')),
                        b'/' => units.push(u16::from(b'/')),
                        b'b' => units.push(8),
                        b'f' => units.push(12),
                        b'n' => units.push(10),
                        b'r' => units.push(13),
                        b't' => units.push(9),
                        b'u' => {
                            let hex = std::str::from_utf8(&self.s[self.i..self.i + 4]).unwrap();
                            units.push(u16::from_str_radix(hex, 16).unwrap());
                            self.i += 4;
                        }
                        other => panic!("不支援的轉義 \\{}", other as char),
                    }
                }
                other => {
                    assert!(other.is_ascii(), "ensure_ascii 輸出不應含非 ASCII 位元組");
                    units.push(u16::from(other));
                }
            }
        }
        String::from_utf16(&units).expect("合法 UTF-16")
    }

    fn string_array(&mut self) -> Vec<String> {
        self.expect(b'[');
        let mut out = Vec::new();
        if self.peek() == b']' {
            self.i += 1;
            return out;
        }
        loop {
            out.push(self.string());
            match self.peek() {
                b',' => self.i += 1,
                b']' => {
                    self.i += 1;
                    break;
                }
                c => panic!("預期 , 或 ]，得到 {:?}", c as char),
            }
        }
        out
    }
}

fn parse_golden_line(line: &str) -> GoldenCmdline {
    let mut p = MiniJson { s: line.as_bytes(), i: 0 };
    let mut out = GoldenCmdline { args: vec![], joined: String::new(), split: vec![] };
    p.expect(b'{');
    loop {
        let key = p.string();
        p.expect(b':');
        match key.as_str() {
            "args" => out.args = p.string_array(),
            "joined" => out.joined = p.string(),
            "split" => out.split = p.string_array(),
            other => panic!("未知欄位 {other}"),
        }
        if p.peek() == b',' {
            p.i += 1;
        } else {
            break;
        }
    }
    p.expect(b'}');
    out
}

#[test]
fn shell_join_split_windows_roundtrip_golden() {
    let text = include_str!("../../../contracts/golden/windows_cmdline.ndjson");
    let mut count = 0usize;
    for line in text.lines().filter(|l| !l.trim().is_empty()) {
        let g = parse_golden_line(line);
        assert_eq!(shell_join_windows(&g.args), g.joined, "join 與 list2cmdline 不符：{line}");
        assert_eq!(split_windows_cmdline(&g.joined), g.split, "split 與 CommandLineToArgvW 不符：{line}");
        assert_eq!(g.split, g.args, "往返不等：{line}");
        count += 1;
    }
    assert!(count >= 12, "golden 案例數不足：{count}");
}

#[test]
fn task_xml_escapes_ampersand_in_path() {
    let xml = render_task_xml(&TaskXmlSpec {
        name: "serialwrap".into(),
        user_id: "HOST\\alice".into(),
        command: "C:\\A&B\\serialwrapd.exe".into(),
        arguments: String::new(),
        working_dir: "C:\\A&B".into(),
        enabled: true,
    });
    assert!(xml.contains("<Command>C:\\A&amp;B\\serialwrapd.exe</Command>"));
}

#[test]
fn supervision_mode_parse_roundtrip() {
    for s in ["on-demand", "systemd-user", "systemd-system", "windows-task", "windows-service"] {
        assert_eq!(sm(s).as_str(), s);
    }
    assert_eq!(sm("Systemd-User"), SupervisionMode::Other("Systemd-User".into()));
}

// ───────── R5-F1：交接前監管拉起抑制（hold／release） ─────────

#[test]
fn systemd_user_hold_writes_dropin_reload_disable_verify() {
    let home = tempfile::tempdir().unwrap();
    let env = user_env(home.path(), HOLD_VERIFY, RELEASE_OK);
    let ctl = SystemdCtl::new(env.clone(), real_fs(), FakeClock::new());
    let hold = ctl.hold_autostart(&sm("systemd-user"), false).expect("hold 應成功");
    assert_eq!(hold.kind, AutostartHoldKind::SystemdDropIn);
    assert_eq!(hold.prior_restart.as_deref(), Some("on-failure"));
    assert_eq!(hold.prior_enabled, Some(true));
    let path = user_dropin(home.path());
    assert_eq!(std::fs::read(&path).unwrap(), DROPIN_BYTES.as_bytes());
    assert_eq!(hold.dropin_path.as_deref(), Some(path.as_path()));
    assert_eq!(
        env.calls(),
        vec![
            argv(&["systemctl", "--user", "show", "serialwrap", "-p", "Restart", "--value"]),
            argv(&["systemctl", "--user", "is-enabled", "serialwrap"]),
            argv(&["systemctl", "--user", "daemon-reload"]),
            argv(&["systemctl", "--user", "disable", "serialwrap"]),
            argv(&["systemctl", "--user", "show", "serialwrap", "-p", "Restart", "-p", "RefuseManualStart", "-p", "UnitFileState"]),
        ]
    );
    for c in env.calls() {
        assert!(
            !c.iter().any(|a| a == "stop" || a == "--now" || a == "--runtime"),
            "禁止的參數出現於 {c:?}"
        );
    }
}

#[test]
fn systemd_hold_verify_mismatch_reverts_and_fails() {
    let home = tempfile::tempdir().unwrap();
    let env = user_env(home.path(), "UnitFileState=enabled\nRefuseManualStart=no\nRestart=on-failure\n", RELEASE_OK);
    let ctl = SystemdCtl::new(env.clone(), real_fs(), FakeClock::new());
    let e = ctl.hold_autostart(&sm("systemd-user"), false).expect_err("驗證不符必須回 Err");
    assert_eq!(e.error_code, Some("INTERNAL_ERROR"));
    assert_eq!(e.phase, Some("autostart_hold"));
    assert!(!user_dropin(home.path()).exists(), "失敗後 drop-in 必須已撤銷");
    let calls = env.calls();
    let verify = argv(&["systemctl", "--user", "show", "serialwrap", "-p", "Restart", "-p", "RefuseManualStart", "-p", "UnitFileState"]);
    let idx = calls.iter().position(|c| c == &verify).expect("應執行驗證");
    let tail = &calls[idx + 1..];
    assert!(tail.contains(&argv(&["systemctl", "--user", "daemon-reload"])), "撤銷後必須 daemon-reload");
    assert!(tail.contains(&argv(&["systemctl", "--user", "enable", "serialwrap"])), "撤銷時還原 prior_enabled");
    assert_eq!(ctl.autostart_hold_state(&sm("systemd-user")), HoldState::NotHeld);
}

#[test]
fn systemd_system_hold_without_sudo_needs_sudo() {
    let home = tempfile::tempdir().unwrap();
    let staging = home.path().join(".local/share/serialwrap/update-hold.conf");

    let env = FakeSysEnv::builder().home(home.path()).build();
    let e = SystemdCtl::new(env.clone(), real_fs(), FakeClock::new())
        .hold_autostart(&sm("systemd-system"), false)
        .expect_err("無 sudo 必須 NEEDS_SUDO");
    assert_eq!(e.error_code, Some("NEEDS_SUDO"));
    assert_eq!(
        e.hint.as_deref(),
        Some("需 root：更新／回滾須先停用 systemd 自動拉起，請加 --with-sudo")
    );
    assert!(env.calls().is_empty());
    assert!(!staging.exists(), "無 sudo 時不得寫入 staging");

    let env_sudo = FakeSysEnv::builder().home(home.path()).build();
    let _ = SystemdCtl::new(env_sudo.clone(), real_fs(), FakeClock::new()).hold_autostart(&sm("systemd-system"), true);
    let staging_s = staging.display().to_string();
    let calls = env_sudo.calls();
    let install = vec![
        "sudo".to_string(),
        "install".into(),
        "-D".into(),
        "-m".into(),
        "0644".into(),
        staging_s,
        "/etc/systemd/system/serialwrap.service.d/90-serialwrap-update-hold.conf".into(),
    ];
    assert!(calls.contains(&install), "calls={calls:?}");
    assert!(calls.contains(&argv(&["sudo", "systemctl", "daemon-reload"])), "calls={calls:?}");
    assert!(calls.contains(&argv(&["sudo", "systemctl", "disable", "serialwrap"])), "calls={calls:?}");
}

#[test]
fn systemd_release_removes_dropin_and_restores() {
    let home = tempfile::tempdir().unwrap();
    let env = user_env(home.path(), HOLD_VERIFY, RELEASE_OK);
    let ctl = SystemdCtl::new(env.clone(), real_fs(), FakeClock::new());
    let hold = ctl.hold_autostart(&sm("systemd-user"), false).expect("hold");

    let n0 = env.calls().len();
    ctl.release_autostart(&hold, Some(true), false).expect("release 應成功");
    assert!(!user_dropin(home.path()).exists(), "release 必須移除 drop-in");
    assert_eq!(
        env.calls()[n0..].to_vec(),
        vec![
            argv(&["systemctl", "--user", "daemon-reload"]),
            argv(&["systemctl", "--user", "enable", "serialwrap"]),
            argv(&["systemctl", "--user", "show", "serialwrap", "-p", "Restart", "-p", "RefuseManualStart"]),
        ]
    );

    // restore_enabled=None 不得 enable；drop-in 已不存在時再次 release 仍成功（冪等）
    let n1 = env.calls().len();
    ctl.release_autostart(&hold, None, false).expect("冪等 release 應成功");
    assert!(!env.calls()[n1..].contains(&argv(&["systemctl", "--user", "enable", "serialwrap"])));

    // 驗證回 Restart=no（與 prior_restart 不符）→ Err(phase:"autostart_release")
    let home2 = tempfile::tempdir().unwrap();
    let env2 = user_env(home2.path(), HOLD_VERIFY, "Restart=no\nRefuseManualStart=no\n");
    let ctl2 = SystemdCtl::new(env2.clone(), real_fs(), FakeClock::new());
    let hold2 = ctl2.hold_autostart(&sm("systemd-user"), false).expect("hold2");
    let e = ctl2.release_autostart(&hold2, Some(true), false).expect_err("Restart=no 必須失敗");
    assert_eq!(e.error_code, Some("INTERNAL_ERROR"));
    assert_eq!(e.phase, Some("autostart_release"));
}

#[test]
fn windows_task_hold_is_disable_and_release_restores() {
    let mk = || FakeSysEnv::builder().command(&ps_argv(), 0, "Disabled\r\n", "").build();

    let env = mk();
    let ctl = WindowsTaskCtl::new(env.clone(), real_fs(), FakeClock::new());
    let hold = ctl.hold_autostart(&sm("windows-task"), false).expect("hold");
    assert_eq!(hold.kind, AutostartHoldKind::TaskDisabled);
    assert!(env.calls().contains(&argv(&["schtasks", "/Change", "/TN", "serialwrap", "/DISABLE"])));
    let n0 = env.calls().len();
    ctl.release_autostart(&hold, Some(true), false).expect("release");
    assert!(env.calls()[n0..].contains(&argv(&["schtasks", "/Change", "/TN", "serialwrap", "/ENABLE"])));

    let env2 = mk();
    let ctl2 = WindowsTaskCtl::new(env2.clone(), real_fs(), FakeClock::new());
    let hold2 = ctl2.hold_autostart(&sm("windows-task"), false).expect("hold2");
    ctl2.release_autostart(&hold2, Some(false), false).expect("release2");
    assert!(!env2.calls().iter().any(|c| c.contains(&"/ENABLE".to_string())), "Some(false) 不得 /ENABLE");
}

#[test]
fn on_demand_hold_is_noop_record() {
    let env = FakeSysEnv::builder().build();
    let h = SystemdCtl::new(env.clone(), real_fs(), FakeClock::new())
        .hold_autostart(&sm("on-demand"), false)
        .expect("on-demand hold 不得失敗");
    assert_eq!(h.kind, AutostartHoldKind::NoSupervisor);
    assert!(h.ran.is_empty());
    assert!(env.calls().is_empty());
}

// ───────── 真機能力測試（ignored；只由擁有者授權主機執行） ─────────

#[cfg(windows)]
#[test]
#[ignore = "cap:supervisor_runtime"]
#[allow(non_snake_case)]
fn cap_supervisor_runtime__windows_task_roundtrip() {
    use sw_pal::clock::windows::NativeClock;
    use sw_pal::sysenv::windows::NativeSysEnv;

    let name = format!("serialwrap-captest-{}", std::process::id());
    let tmp = tempfile::tempdir().unwrap();
    let def_path = tmp.path().join("captest.task.xml");
    let env: Arc<dyn SysEnv> = Arc::new(NativeSysEnv);
    let clock: Arc<dyn Clock> = Arc::new(NativeClock);
    let ctl = WindowsTaskCtl::new(env, real_fs(), clock);
    let w = sm("windows-task");
    let def = SupervisorDefinition {
        mode: w.clone(),
        name: name.clone(),
        exec: argv(&["cmd.exe", "/c", "exit", "0"]),
        working_dir: None,
        run_user: None,
        env: vec![],
        definition_path: Some(def_path.clone()),
        register_held: false,
    };
    ctl.install(&def, false);

    // 註冊後以 schtasks 查回 XML，確認 ExecutionTimeLimit=PT0S 已落地
    let out = std::process::Command::new("schtasks")
        .args(["/Query", "/TN", name.as_str(), "/XML"])
        .output()
        .expect("schtasks 可執行");
    let body = if out.stdout.starts_with(&[0xFF, 0xFE]) {
        let units: Vec<u16> = out.stdout[2..].chunks(2).map(|c| u16::from_le_bytes([c[0], c[1]])).collect();
        String::from_utf16_lossy(&units)
    } else {
        String::from_utf8_lossy(&out.stdout).into_owned()
    };
    assert!(body.contains("PT0S"), "查回 XML 應含 PT0S");

    assert!(ctl.action_named(&w, &name, ServiceAction::Start, false).ok, "/Run 失敗");
    let deadline = Instant::now() + Duration::from_secs(10);
    loop {
        match ctl.query_named(&w, &name) {
            TaskQuery::NotRunning(_) => break,
            other if Instant::now() >= deadline => panic!("10 s 內未回到 NotRunning：{other:?}"),
            _ => std::thread::sleep(Duration::from_millis(200)),
        }
    }
    ctl.uninstall_named(&w, &name, false);
    assert_eq!(ctl.query_named(&w, &name), TaskQuery::NotFound);
}

#[cfg(unix)]
#[test]
#[ignore = "cap:supervisor_runtime"]
#[allow(non_snake_case)]
fn cap_supervisor_runtime__systemd_hold_blocks_restart_on_failure() {
    use sw_pal::clock::posix::NativeClock;
    use sw_pal::sysenv::posix::NativeSysEnv;

    struct Cleanup {
        name: String,
        unit: PathBuf,
    }
    impl Drop for Cleanup {
        fn drop(&mut self) {
            for args in [["stop", self.name.as_str()], ["disable", self.name.as_str()], ["reset-failed", self.name.as_str()]] {
                let _ = std::process::Command::new("systemctl").arg("--user").args(args).output();
            }
            let _ = std::fs::remove_file(&self.unit);
            let _ = std::process::Command::new("systemctl").args(["--user", "daemon-reload"]).output();
        }
    }

    let name = format!("sw-hold-captest-{}", std::process::id());
    let home = std::env::var("HOME").expect("HOME");
    let unit_dir = PathBuf::from(&home).join(".config/systemd/user");
    std::fs::create_dir_all(&unit_dir).unwrap();
    let unit = unit_dir.join(format!("{name}.service"));
    std::fs::write(
        &unit,
        "[Unit]\nDescription=serialwrap hold captest\n\n[Service]\nExecStart=/bin/sh -c 'sleep 3; exit 1'\nRestart=on-failure\nRestartSec=1\n",
    )
    .unwrap();
    let _cleanup = Cleanup { name: name.clone(), unit: unit.clone() };

    let systemctl = |args: &[&str]| std::process::Command::new("systemctl").arg("--user").args(args).output().unwrap();
    let show = |prop: &str| -> String {
        let o = systemctl(&["show", name.as_str(), "-p", prop, "--value"]);
        String::from_utf8_lossy(&o.stdout).trim().to_string()
    };
    systemctl(&["daemon-reload"]);

    let env: Arc<dyn SysEnv> = Arc::new(NativeSysEnv);
    let clock: Arc<dyn Clock> = Arc::new(NativeClock);
    let ctl = SystemdCtl::new(env, real_fs(), clock);
    let user = sm("systemd-user");

    let _ = ctl.action_named(&user, &name, ServiceAction::Start, false);
    std::thread::sleep(Duration::from_millis(500));
    let hold = ctl.hold_autostart_named(&user, &name, false).expect("hold");
    std::thread::sleep(Duration::from_secs(6));
    assert_eq!(show("NRestarts"), "0", "hold 期間不得自動重啟");
    assert_eq!(show("ActiveState"), "failed");
    assert_ne!(systemctl(&["start", name.as_str()]).status.code(), Some(0), "hold 期間手動 start 必須被拒");

    ctl.release_autostart(&hold, hold.prior_enabled, false).expect("release");
    assert_eq!(systemctl(&["start", name.as_str()]).status.code(), Some(0), "release 後 start 應成功");
    std::thread::sleep(Duration::from_secs(6));
    let restarts: u32 = show("NRestarts").parse().unwrap_or(0);
    assert!(restarts >= 1, "release 後 Restart=on-failure 應生效（NRestarts={restarts}）");
}
