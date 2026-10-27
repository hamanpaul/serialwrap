//! P4-T3 行為測試：systemd、windows-task、windows-service 與交接前監管拉起抑制。
//!
//! 測試名稱與斷言對應 `docs/rust-refactor/plans/05-stage1b-rust-cli-mgmt.md` Task 3。
//! `cap_supervisor_runtime__*` 需在隔離的 systemd 容器或 Windows 環境執行。
#![allow(dead_code, unused_imports)]

use std::path::{Path, PathBuf};
use std::sync::Arc;
use std::time::{Duration, Instant};

use sw_pal::fakes::*;
use sw_pal::fs_ops::{FileLockGuard, FsOps, LinkKind};
use sw_pal::service_ctl::*;
use sw_pal::traits::{Clock, FsRead, PalError, PalErrorKind, PalResult, SysEnv};

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

fn supervisor_definition(
    mode: &str,
    name: &str,
    register_held: bool,
    definition_path: Option<PathBuf>,
) -> SupervisorDefinition {
    SupervisorDefinition {
        mode: sm(mode),
        name: name.into(),
        exec: vec![
            "C:\\p\\bin\\serialwrapd.exe".into(),
            "--socket".into(),
            "tcp://127.0.0.1:48700".into(),
        ],
        working_dir: None,
        run_user: None,
        env: vec![],
        definition_path,
        register_held,
    }
}

fn decode_utf16le_bom(bytes: &[u8]) -> String {
    assert_eq!(&bytes[..2], &[0xff, 0xfe]);
    let units = bytes[2..]
        .chunks_exact(2)
        .map(|chunk| u16::from_le_bytes([chunk[0], chunk[1]]))
        .collect::<Vec<_>>();
    String::from_utf16_lossy(&units)
}

struct IsolatedSystemdFs {
    inner: std::sync::Arc<dyn FsOps>,
}

impl FsRead for IsolatedSystemdFs {
    fn read(&self, path: &Path) -> PalResult<Vec<u8>> {
        if path.starts_with("/etc/systemd/system") {
            return Err(PalError::plain(
                PalErrorKind::NotFound,
                "isolated systemd destination is not present in the test filesystem",
            ));
        }
        self.inner.read(path)
    }

    fn exists(&self, path: &Path) -> bool {
        !path.starts_with("/etc/systemd/system") && self.inner.exists(path)
    }

    fn is_dir(&self, path: &Path) -> bool {
        self.inner.is_dir(path)
    }

    fn is_file(&self, path: &Path) -> bool {
        self.inner.is_file(path)
    }

    fn list_dir(&self, path: &Path) -> PalResult<Vec<String>> {
        self.inner.list_dir(path)
    }
}

impl FsOps for IsolatedSystemdFs {
    fn atomic_write(&self, path: &Path, data: &[u8], mode: Option<u32>) -> PalResult<()> {
        self.inner.atomic_write(path, data, mode)
    }

    fn mkdirs(&self, path: &Path) -> PalResult<()> {
        self.inner.mkdirs(path)
    }

    fn remove_file(&self, path: &Path) -> PalResult<()> {
        self.inner.remove_file(path)
    }

    fn remove_dir(&self, path: &Path) -> PalResult<()> {
        self.inner.remove_dir(path)
    }

    fn copy_preserving(&self, from: &Path, to: &Path) -> PalResult<()> {
        self.inner.copy_preserving(from, to)
    }

    fn copy_tree(&self, from: &Path, to: &Path) -> PalResult<()> {
        self.inner.copy_tree(from, to)
    }

    fn remove_tree(&self, path: &Path) -> PalResult<()> {
        self.inner.remove_tree(path)
    }

    fn replace(&self, from: &Path, to: &Path) -> PalResult<()> {
        self.inner.replace(from, to)
    }

    fn write_file(&self, path: &Path, data: &[u8]) -> PalResult<()> {
        self.inner.write_file(path, data)
    }

    fn read_link(&self, path: &Path) -> PalResult<Option<PathBuf>> {
        self.inner.read_link(path)
    }

    fn set_mode(&self, path: &Path, mode: u32) -> PalResult<()> {
        self.inner.set_mode(path, mode)
    }

    fn sync_dir(&self, path: &Path) -> PalResult<()> {
        self.inner.sync_dir(path)
    }

    fn symlink_file(&self, link: &Path, target: &Path) -> PalResult<()> {
        self.inner.symlink_file(link, target)
    }

    fn symlink_or_copy_dir(&self, link: &Path, target: &Path) -> PalResult<LinkKind> {
        self.inner.symlink_or_copy_dir(link, target)
    }

    fn file_lock_exclusive(&self, path: &Path, blocking: bool) -> PalResult<FileLockGuard> {
        self.inner.file_lock_exclusive(path, blocking)
    }
}

fn isolated_systemd_fs() -> std::sync::Arc<dyn FsOps> {
    std::sync::Arc::new(IsolatedSystemdFs { inner: real_fs() })
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
fn task_xml_omits_empty_working_directory() {
    // 原生 Windows 實測：空的 <WorkingDirectory></WorkingDirectory> 使 schtasks /Create /XML
    // 回「value which is incorrectly formatted or out of range」；未指定工作目錄時必須省略整個元素。
    let spec = |working_dir: &str| TaskXmlSpec {
        name: "serialwrap".into(),
        user_id: "HOST\\alice".into(),
        command: "C:\\x\\serialwrapd.exe".into(),
        arguments: String::new(),
        working_dir: working_dir.into(),
        enabled: true,
    };
    let (with_dir, without_dir) = (render_task_xml(&spec("C:\\x")), render_task_xml(&spec("")));
    assert!(!without_dir.contains("WorkingDirectory"), "空工作目錄不得輸出元素");
    assert_eq!(
        with_dir.replace("      <WorkingDirectory>C:\\x</WorkingDirectory>\r\n", ""),
        without_dir,
        "除 WorkingDirectory 一行外其餘位元組必須相同"
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
    assert_eq!(mk(4, "inactive\n"), TaskQuery::NotFound);

    let env_missing = FakeSysEnv::builder()
        .command_err(&q_argv, PalErrorKind::NotFound, "No such file or directory")
        .build();
    let q = SystemdCtl::new(env_missing, real_fs(), FakeClock::new()).query(&sm("systemd-user"));
    assert!(matches!(q, TaskQuery::QueryFailed(_)), "執行檔不存在 → {q:?}");
}

#[test]
fn systemd_install_register_held_never_enables_or_starts() {
    let user_env = FakeSysEnv::builder().build();
    let user_ctl = SystemdCtl::new(user_env.clone(), real_fs(), FakeClock::new());
    let user_def = supervisor_definition("systemd-user", "serialwrap", true, None);
    user_ctl.install(&user_def, false);
    assert_eq!(
        user_env.calls(),
        vec![argv(&["systemctl", "--user", "daemon-reload"])]
    );

    let system_env = FakeSysEnv::builder().build();
    let system_ctl = SystemdCtl::new(system_env.clone(), real_fs(), FakeClock::new());
    let system_def = supervisor_definition("systemd-system", "serialwrap", true, None);
    system_ctl.install(&system_def, true);
    assert_eq!(
        system_env.calls(),
        vec![argv(&["sudo", "systemctl", "daemon-reload"])]
    );
    assert_eq!(
        system_ctl.install_plan(&system_def),
        vec![argv(&["sudo", "systemctl", "daemon-reload"])]
    );
}

#[test]
fn windows_service_install_plan_register_held_is_disabled_and_not_started() {
    let env = FakeSysEnv::builder().build();
    let ctl = WindowsServiceCtl::new(env, FakeClock::new());
    let def = supervisor_definition("windows-service", "serialwrap", true, None);
    let plan = ctl.install_plan(&def);
    assert_eq!(plan.len(), 2);
    assert!(plan[0]
        .windows(2)
        .any(|pair| pair[0] == "start=" && pair[1] == "disabled"));
    assert!(!plan.iter().any(|argv| argv.get(1).is_some_and(|arg| arg == "start")));
}

#[test]
fn windows_task_install_register_held_writes_disabled_xml() {
    let tmp = tempfile::tempdir().unwrap();
    let env = FakeSysEnv::builder().user_identity("HOST\\alice").build();
    let ctl = WindowsTaskCtl::new(env.clone(), real_fs(), FakeClock::new());
    let path = tmp.path().join("held.task.xml");
    let def = supervisor_definition("windows-task", "serialwrap", true, Some(path.clone()));
    let records = ctl.install(&def, false);
    assert_eq!(records.len(), 1);
    assert_eq!(records[0].rc, 0);
    let xml = decode_utf16le_bom(&std::fs::read(path).unwrap());
    assert!(xml.contains(
        "    <AllowStartOnDemand>true</AllowStartOnDemand>\r\n    <Enabled>false</Enabled>"
    ));
}

#[test]
fn windows_task_install_write_failures_report_nonzero_without_running_create() {
    let tmp = tempfile::tempdir().unwrap();
    let blocker = tmp.path().join("not-a-directory");
    std::fs::write(&blocker, b"x").unwrap();
    let env = FakeSysEnv::builder().build();
    let ctl = WindowsTaskCtl::new(env.clone(), real_fs(), FakeClock::new());

    let def = supervisor_definition(
        "windows-task",
        "serialwrap",
        false,
        Some(blocker.join("task.xml")),
    );
    let records = ctl.install(&def, false);
    assert_eq!(records.len(), 1);
    assert_eq!(records[0].rc, -1);
    assert_eq!(records[0].argv, ctl.install_plan(&def)[0]);
    assert!(env.calls().is_empty(), "XML 寫入失敗後不得執行 /Create");

    let mut no_path = supervisor_definition("windows-task", "serialwrap", false, None);
    no_path.definition_path = None;
    let records = ctl.install(&no_path, false);
    assert_eq!(records.len(), 1);
    assert_eq!(records[0].rc, -1);

    let mut no_exec = supervisor_definition(
        "windows-task",
        "serialwrap",
        false,
        Some(tmp.path().join("empty.task.xml")),
    );
    no_exec.exec.clear();
    let records = ctl.install(&no_exec, false);
    assert_eq!(records.len(), 1);
    assert_eq!(records[0].rc, -1);
    assert!(env.calls().is_empty());
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
    assert!(matches!(q(0, "unrecognized output\r\n"), TaskQuery::QueryFailed(_)));
}

#[test]
fn windows_service_named_hold_release_restores_the_same_service() {
    let name = "serialwrap-captest-review";
    let qc = ["sc.exe", "qc", name];
    let env = FakeSysEnv::builder()
        .command_seq(
            &qc,
            &[
                (0, "START_TYPE : 2 AUTO_START\r\n", ""),
                (0, "START_TYPE : 4 DISABLED\r\n", ""),
                (0, "START_TYPE : 2 AUTO_START\r\n", ""),
            ],
        )
        .build();
    let ctl = WindowsServiceCtl::new(env.clone(), FakeClock::new());
    let hold = ctl
        .hold_autostart_named(&sm("windows-service"), name, true)
        .expect("named hold");
    assert_eq!(hold.name.as_deref(), Some(name));
    ctl.release_autostart(&hold, Some(true), true)
        .expect("named release");
    let calls = env.calls();
    assert!(calls.iter().all(|call| !call.contains(&"serialwrap".to_string())));
    assert!(calls.contains(&argv(&["sc.exe", "config", name, "start=", "disabled"])));
    assert!(calls.contains(&argv(&["sc.exe", "config", name, "start=", "auto"])));
    assert!(calls.contains(&argv(&["sc.exe", "qc", name])));
}

#[test]
fn windows_service_hold_state_uses_return_code_not_error_text() {
    let missing = FakeSysEnv::builder()
        .command(&["sc.exe", "qc", "serialwrap"], 1060, "", "")
        .build();
    assert_eq!(
        WindowsServiceCtl::new(missing, FakeClock::new())
            .autostart_hold_state(&sm("windows-service")),
        HoldState::NotHeld
    );

    let denied = FakeSysEnv::builder()
        .command(
            &["sc.exe", "qc", "serialwrap"],
            5,
            "",
            "服務不存在，但拒絕存取",
        )
        .build();
    assert!(matches!(
        WindowsServiceCtl::new(denied, FakeClock::new())
            .autostart_hold_state(&sm("windows-service")),
        HoldState::QueryFailed(_)
    ));
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
    assert_eq!(ctl.autostart_hold_state(&sm("systemd-user")), HoldState::Held);
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
fn systemd_home_and_staging_paths_prefer_home_over_passwd_home() {
    let home = tempfile::tempdir().unwrap();
    let passwd_home = tempfile::tempdir().unwrap();
    let user_verify = argv(&[
        "systemctl",
        "--user",
        "show",
        "serialwrap",
        "-p",
        "Restart",
        "-p",
        "RefuseManualStart",
        "-p",
        "UnitFileState",
    ]);
    let user_verify_refs = user_verify.iter().map(String::as_str).collect::<Vec<_>>();
    let user_env = FakeSysEnv::builder()
        .home(home.path())
        .passwd_home(passwd_home.path())
        .command(&user_verify_refs, 0, HOLD_VERIFY, "")
        .build();
    let user_ctl = SystemdCtl::new(user_env, real_fs(), FakeClock::new());
    let user_hold = user_ctl
        .hold_autostart(&sm("systemd-user"), false)
        .expect("user hold");
    assert_eq!(user_hold.dropin_path.as_deref(), Some(user_dropin(home.path()).as_path()));
    assert!(user_dropin(home.path()).exists());
    assert!(!user_dropin(passwd_home.path()).exists());

    let system_verify = argv(&[
        "systemctl",
        "show",
        "serialwrap",
        "-p",
        "Restart",
        "-p",
        "RefuseManualStart",
        "-p",
        "UnitFileState",
    ]);
    let system_verify_refs = system_verify.iter().map(String::as_str).collect::<Vec<_>>();
    let system_env = FakeSysEnv::builder()
        .home(home.path())
        .passwd_home(passwd_home.path())
        .command(&system_verify_refs, 0, HOLD_VERIFY, "")
        .build();
    let system_ctl = SystemdCtl::new(system_env, isolated_systemd_fs(), FakeClock::new());
    let _system_hold = system_ctl
        .hold_autostart(&sm("systemd-system"), true)
        .expect("system hold");
    let staging = home.path().join(".local/share/serialwrap/update-hold.conf");
    assert_eq!(std::fs::read(staging).unwrap(), DROPIN_BYTES.as_bytes());
    assert!(!passwd_home
        .path()
        .join(".local/share/serialwrap/update-hold.conf")
        .exists());
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
fn systemd_user_hold_rollback_removes_empty_dropin_dir() {
    // hold 驗證不符時的撤銷路徑與 release 一致：空的 `<unit>.service.d` 一併移除。
    let home = tempfile::tempdir().unwrap();
    let env = user_env(home.path(), "UnitFileState=enabled\nRefuseManualStart=no\nRestart=on-failure\n", RELEASE_OK);
    let ctl = SystemdCtl::new(env, real_fs(), FakeClock::new());
    let dropin_dir = user_dropin(home.path()).parent().unwrap().to_path_buf();
    ctl.hold_autostart(&sm("systemd-user"), false).expect_err("驗證不符必須回 Err");
    assert!(!user_dropin(home.path()).exists(), "撤銷後 drop-in 不得殘留");
    assert!(!dropin_dir.exists(), "撤銷後空的 drop-in 目錄必須移除：{}", dropin_dir.display());
}

#[test]
fn systemd_system_hold_rollback_rmdirs_dropin_dir_best_effort() {
    // system 模式撤銷：`sudo rm -f` 之後 `sudo rmdir --ignore-fail-on-non-empty`；rmdir 失敗不新增撤銷失敗。
    let home = tempfile::tempdir().unwrap();
    let dir = "/etc/systemd/system/serialwrap.service.d";
    let dropin = format!("{dir}/90-serialwrap-update-hold.conf");
    let env = FakeSysEnv::builder()
        .home(home.path())
        .command(&["sudo", "rmdir", "--ignore-fail-on-non-empty", dir], 1, "", "rmdir: failed")
        .build();
    let ctl = SystemdCtl::new(env.clone(), isolated_systemd_fs(), FakeClock::new());
    let e = ctl
        .hold_autostart(&sm("systemd-system"), true)
        .expect_err("驗證輸出為空必須回 Err");
    assert_eq!(e.phase, Some("autostart_hold"));
    let hint = e.hint.clone().unwrap_or_default();
    assert!(!hint.contains("rmdir"), "rmdir 為 best-effort，不得列為撤銷失敗：{hint}");
    let calls = env.calls();
    let rm = argv(&["sudo", "rm", "-f", &dropin]);
    let rmdir = argv(&["sudo", "rmdir", "--ignore-fail-on-non-empty", dir]);
    let rm_at = calls.iter().position(|c| c == &rm).unwrap_or_else(|| panic!("calls={calls:?}"));
    let rmdir_at = calls.iter().position(|c| c == &rmdir).unwrap_or_else(|| panic!("calls={calls:?}"));
    assert!(rm_at < rmdir_at, "rmdir 必須在移除 drop-in 之後：{calls:?}");
}

#[test]
fn systemd_hold_over_existing_dropin_does_not_record_post_hold_values() {
    let home = tempfile::tempdir().unwrap();
    let path = user_dropin(home.path());
    std::fs::create_dir_all(path.parent().unwrap()).unwrap();
    std::fs::write(&path, DROPIN_BYTES).unwrap();
    let env = user_env(home.path(), HOLD_VERIFY, RELEASE_OK);
    let ctl = SystemdCtl::new(env.clone(), real_fs(), FakeClock::new());
    let hold = ctl
        .hold_autostart(&sm("systemd-user"), false)
        .expect("既有 hold 應可重入");
    assert_eq!(hold.prior_restart, None);
    assert_eq!(hold.prior_enabled, None);
    assert_eq!(
        env.calls(),
        vec![
            argv(&["systemctl", "--user", "daemon-reload"]),
            argv(&["systemctl", "--user", "disable", "serialwrap"]),
            argv(&[
                "systemctl",
                "--user",
                "show",
                "serialwrap",
                "-p",
                "Restart",
                "-p",
                "RefuseManualStart",
                "-p",
                "UnitFileState",
            ]),
        ]
    );
    ctl.release_autostart(&hold, None, false)
        .expect("缺少原始值時應能依解除後狀態驗證");
    assert!(!path.exists());
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
    let _ = SystemdCtl::new(env_sudo.clone(), isolated_systemd_fs(), FakeClock::new())
        .hold_autostart(&sm("systemd-system"), true);
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
    assert_eq!(ctl.autostart_hold_state(&sm("systemd-user")), HoldState::NotHeld);
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
fn systemd_user_release_removes_empty_dropin_dir_but_keeps_foreign_dropins() {
    // release 後若 `<unit>.service.d` 只剩空目錄，須一併移除（不在使用者 unit 目錄留下痕跡）。
    let home = tempfile::tempdir().unwrap();
    let env = user_env(home.path(), HOLD_VERIFY, RELEASE_OK);
    let ctl = SystemdCtl::new(env, real_fs(), FakeClock::new());
    let hold = ctl.hold_autostart(&sm("systemd-user"), false).expect("hold");
    let dropin_dir = user_dropin(home.path()).parent().unwrap().to_path_buf();
    ctl.release_autostart(&hold, Some(true), false).expect("release");
    assert!(!dropin_dir.exists(), "空的 drop-in 目錄必須移除：{}", dropin_dir.display());

    // 目錄內另有非本工具的 drop-in 時只移除 hold 檔，目錄與他人檔案保留。
    let home2 = tempfile::tempdir().unwrap();
    let env2 = user_env(home2.path(), HOLD_VERIFY, RELEASE_OK);
    let ctl2 = SystemdCtl::new(env2, real_fs(), FakeClock::new());
    let hold2 = ctl2.hold_autostart(&sm("systemd-user"), false).expect("hold2");
    let dir2 = user_dropin(home2.path()).parent().unwrap().to_path_buf();
    let foreign = dir2.join("10-owner.conf");
    std::fs::write(&foreign, "[Service]\n").unwrap();
    ctl2.release_autostart(&hold2, Some(true), false).expect("release2");
    assert!(!user_dropin(home2.path()).exists(), "hold drop-in 必須移除");
    assert!(foreign.exists(), "他人 drop-in 不得刪除");
}

#[test]
fn systemd_system_release_rmdirs_dropin_dir_ignoring_non_empty() {
    let dropin = PathBuf::from("/etc/systemd/system/serialwrap.service.d/90-serialwrap-update-hold.conf");
    // rmdir 回非零（例如目錄已不存在）屬 best-effort，不得讓 release 失敗。
    let env = FakeSysEnv::builder()
        .command(&["systemctl", "show", "serialwrap", "-p", "Restart", "-p", "RefuseManualStart"], 0, RELEASE_OK, "")
        .command(
            &["sudo", "rmdir", "--ignore-fail-on-non-empty", "/etc/systemd/system/serialwrap.service.d"],
            1,
            "",
            "rmdir: failed to remove '/etc/systemd/system/serialwrap.service.d': No such file or directory",
        )
        .build();
    let ctl = SystemdCtl::new(env.clone(), isolated_systemd_fs(), FakeClock::new());
    let hold = AutostartHoldRecord {
        mode: sm("systemd-system"),
        kind: AutostartHoldKind::SystemdDropIn,
        name: Some("serialwrap".into()),
        dropin_path: Some(dropin.clone()),
        prior_enabled: Some(true),
        prior_restart: Some("on-failure".into()),
        ran: Vec::new(),
    };
    ctl.release_autostart(&hold, None, true).expect("system release");
    let calls = env.calls();
    let rm = argv(&["sudo", "rm", "-f", &dropin.display().to_string()]);
    let rmdir = argv(&[
        "sudo",
        "rmdir",
        "--ignore-fail-on-non-empty",
        &dropin.parent().unwrap().display().to_string(),
    ]);
    let rm_at = calls.iter().position(|c| c == &rm).unwrap_or_else(|| panic!("calls={calls:?}"));
    let rmdir_at = calls.iter().position(|c| c == &rmdir).unwrap_or_else(|| panic!("calls={calls:?}"));
    assert!(rm_at < rmdir_at, "rmdir 必須在移除 drop-in 之後：{calls:?}");
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
fn windows_task_named_hold_release_uses_the_registered_name() {
    let name = "serialwrap-captest-review";
    let ps = format!(
        "try {{ (Get-ScheduledTask -TaskName '{name}' -ErrorAction Stop).State.ToString() }} catch {{ if ($_.CategoryInfo.Category -eq 'ObjectNotFound') {{ 'NotFound' }} else {{ exit 3 }} }}"
    );
    let ps_argv = ["powershell.exe", "-NoProfile", "-NonInteractive", "-Command", ps.as_str()];
    let xml_argv = ["schtasks", "/Query", "/TN", name, "/XML"];
    let xml = "<Task><Settings><Enabled>true</Enabled></Settings></Task>";
    let env = FakeSysEnv::builder()
        .command(&xml_argv, 0, xml, "")
        .command(&[
            "schtasks",
            "/Change",
            "/TN",
            name,
            "/DISABLE",
        ], 0, "", "")
        .command(&ps_argv, 0, "Disabled\r\n", "")
        .build();
    let ctl = WindowsTaskCtl::new(env.clone(), real_fs(), FakeClock::new());
    let hold = ctl
        .hold_autostart_named(&sm("windows-task"), name, false)
        .expect("named hold");
    assert_eq!(hold.name.as_deref(), Some(name));
    ctl.release_autostart(&hold, Some(true), false)
        .expect("named release");
    let calls = env.calls();
    assert!(calls.iter().all(|call| !call.contains(&"serialwrap".to_string())));
    assert!(calls.contains(&argv(&["schtasks", "/Change", "/TN", name, "/DISABLE"])));
    assert!(calls.contains(&argv(&["schtasks", "/Change", "/TN", name, "/ENABLE"])));
}

#[test]
fn windows_task_hold_rolls_back_when_running_or_query_is_ambiguous() {
    let xml_argv = ["schtasks", "/Query", "/TN", "serialwrap", "/XML"];
    let enabled_xml = "<Task><Settings><Enabled>true</Enabled></Settings></Task>";
    for state in ["Running\r\n", "Unexpected\r\n"] {
        let env = FakeSysEnv::builder()
            .command(&xml_argv, 0, enabled_xml, "")
            .command(
                &["schtasks", "/Change", "/TN", "serialwrap", "/DISABLE"],
                0,
                "",
                "",
            )
            .command(&ps_argv(), 0, state, "")
            .build();
        let ctl = WindowsTaskCtl::new(env.clone(), real_fs(), FakeClock::new());
        assert!(ctl
            .hold_autostart(&sm("windows-task"), false)
            .is_err());
        let calls = env.calls();
        assert!(calls.contains(&argv(&[
            "schtasks",
            "/Change",
            "/TN",
            "serialwrap",
            "/ENABLE",
        ])));
    }
}

#[test]
fn windows_task_hold_state_parses_utf16_bom_and_ansi_xml() {
    let task_xml = "<Task><Settings><Enabled>false</Enabled></Settings></Task>";
    let utf16 = encode_utf16le_bom(task_xml);
    let xml_argv = ["schtasks", "/Query", "/TN", "serialwrap", "/XML"];
    let env_utf16 = FakeSysEnv::builder()
        .command(&ps_argv(), 0, "Ready\r\n", "")
        .command_raw(&xml_argv, 0, &utf16, b"")
        .build();
    assert_eq!(
        WindowsTaskCtl::new(env_utf16, real_fs(), FakeClock::new())
            .autostart_hold_state(&sm("windows-task")),
        HoldState::Held
    );

    let ansi_xml = "<Task><Settings><Enabled>true</Enabled></Settings></Task>";
    let env_ansi = FakeSysEnv::builder()
        .command(&ps_argv(), 0, "Ready\r\n", "")
        .command(&xml_argv, 0, ansi_xml, "")
        .build();
    assert_eq!(
        WindowsTaskCtl::new(env_ansi, real_fs(), FakeClock::new())
            .autostart_hold_state(&sm("windows-task")),
        HoldState::NotHeld
    );
}

#[test]
fn supervision_controllers_set_enabled_and_uninstall_use_expected_argv() {
    let system_env = FakeSysEnv::builder().build();
    let systemd = SystemdCtl::new(system_env.clone(), real_fs(), FakeClock::new());
    systemd.set_enabled(&sm("systemd-user"), false, false);
    systemd.uninstall(&sm("systemd-user"), false);
    assert_eq!(
        system_env.calls(),
        vec![
            argv(&["systemctl", "--user", "disable", "serialwrap"]),
            argv(&["systemctl", "--user", "disable", "serialwrap"]),
        ]
    );

    let task_env = FakeSysEnv::builder().build();
    let task = WindowsTaskCtl::new(task_env.clone(), real_fs(), FakeClock::new());
    task.set_enabled(&sm("windows-task"), true, false);
    task.uninstall(&sm("windows-task"), false);
    assert_eq!(
        task_env.calls(),
        vec![
            argv(&["schtasks", "/Change", "/TN", "serialwrap", "/ENABLE"]),
            argv(&["schtasks", "/Delete", "/TN", "serialwrap", "/F"]),
        ]
    );

    let service_env = FakeSysEnv::builder().build();
    let service = WindowsServiceCtl::new(service_env.clone(), FakeClock::new());
    service.set_enabled(&sm("windows-service"), true, false);
    service.uninstall(&sm("windows-service"), false);
    assert_eq!(
        service_env.calls(),
        vec![
            argv(&["sc.exe", "config", "serialwrap", "start=", "auto"]),
            argv(&["sc.exe", "delete", "serialwrap"]),
        ]
    );
}

#[test]
fn composite_service_ctl_dispatches_and_rejects_unsupported_modes() {
    let task_query = ps_argv();
    let task_query_refs = task_query;
    let xml_argv = ["schtasks", "/Query", "/TN", "serialwrap", "/XML"];
    let env = FakeSysEnv::builder()
        .command(
            &["schtasks", "/Run", "/TN", "review-task"],
            0,
            "",
            "",
        )
        .command(&task_query_refs, 0, "Ready\r\n", "")
        .command(
            &xml_argv,
            0,
            "<Task><Settings><Enabled>false</Enabled></Settings></Task>",
            "",
        )
        .build();
    let fs = real_fs();
    let composite = CompositeServiceCtl::new(
        vec![sm("windows-task")],
        SystemdCtl::new(env.clone(), fs.clone(), FakeClock::new()),
        WindowsTaskCtl::new(env.clone(), fs, FakeClock::new()),
        WindowsServiceCtl::new(env.clone(), FakeClock::new()),
    );
    assert!(composite
        .action_named(
            &sm("windows-task"),
            "review-task",
            ServiceAction::Start,
            false,
        )
        .ok);
    assert_eq!(
        composite.autostart_hold_state(&sm("windows-task")),
        HoldState::Held
    );
    assert_eq!(
        composite.action(&sm("windows-service"), ServiceAction::Start, false).error_code,
        Some("NO_SYSTEMD")
    );
    assert!(matches!(
        composite.query(&sm("windows-service")),
        TaskQuery::QueryFailed(_)
    ));
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
    let recs = ctl.install(&def, false);
    assert!(
        !recs.is_empty() && recs.iter().all(|r| r.rc == 0),
        "schtasks /Create /XML 必須成功：{recs:?}"
    );

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
            // release 已 best-effort 移除空的 `<unit>.service.d`；測試失敗或中斷時仍由此兜底清掉（OD-29：結束不得留下暫時 unit 痕跡）。
            let _ = std::fs::remove_dir(self.unit.with_extension("service.d"));
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
        "[Unit]\nDescription=serialwrap hold captest\n\n[Service]\nExecStart=/bin/sh -c 'sleep 3; exit 1'\nRestart=on-failure\nRestartSec=1\n\n[Install]\nWantedBy=default.target\n",
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

    assert_eq!(systemctl(&["enable", name.as_str()]).status.code(), Some(0));
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
