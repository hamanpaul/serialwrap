//! P8 Task 13：`windows-task` 監管 Rust daemon 的實跑驗證（`cap:supervisor_runtime`，案例 `s24-sup-wintask-runtime`，SC-24）。
//!
//! 只在擁有者授權的 Windows host 由編排者執行（擁有者裁決 2026-11-08「T13 的 Windows 排程工作實跑」）：
//!
//! ```text
//! cargo build -p sw-daemon --bin serialwrapd --target x86_64-pc-windows-gnu
//! SW_TEST_ALLOW_SCHTASKS=1 SW_WIN_EXTRA_FILES="$PWD/target/x86_64-pc-windows-gnu/debug/serialwrapd.exe" \
//!   scripts/rust/win-test.sh --package sw-pal --caps supervisor_runtime
//! ```
//!
//! `win-runner.sh` 把 `serialwrapd.exe` 複製到其 staging 並以 `SW_TEST_SERIALWRAPD` 告知路徑；本測試再複製到自己的
//! staging（`%TMP%\sw-p8-test-<pid>\bin`），只以該完整路徑辨識與終止行程。未授權（缺 `SW_TEST_ALLOW_SCHTASKS=1`）或缺
//! daemon 執行檔時 panic（證據不足，不得計為 PASS）。三道 COM 防線與收尾見 `support/wintask.rs`。
//!
//! 與計畫 Step 1 範例的差異（皆為安全或實際 API 所需）：排程工作一律用具名 API（`uninstall_named`／`query_named`／
//! `hold_autostart_named`），不呼叫固定名稱 `serialwrap` 的 `uninstall`／`set_enabled`／`hard_stop`／`wait_stopped`
//! （P4 無 `wait_stopped`、`SupervisorDefinition::default_for_test`、`StopReport.ran_end`，改由 support 提供）；
//! sw-pal 不能依賴 sw-cli，P4 `graceful_stop` 固定操作正式工作，故以 `graceful_stop_for_test` 執行同一停止序。
#![allow(non_snake_case)]

mod support;

#[cfg(windows)]
#[test]
#[ignore = "cap:supervisor_runtime"]
fn cap_supervisor_runtime__task_pt0s_restart_and_graceful_stop() {
    use std::path::PathBuf;
    use std::sync::Arc;
    use std::time::{Duration, Instant};

    use support::wintask::*;
    use sw_pal::service_ctl::{ServiceAction, ServiceCtl, SupervisionMode, SupervisorDefinition, WindowsTaskCtl};

    if !allowed() {
        panic!("需擁有者設定 SW_TEST_ALLOW_SCHTASKS=1 才可建立測試排程工作（未授權＝證據不足）");
    }
    let daemon_src = std::env::var("SW_TEST_SERIALWRAPD")
        .map(PathBuf::from)
        .expect("需 SW_TEST_SERIALWRAPD 指向 Windows serialwrapd.exe（以 SW_WIN_EXTRA_FILES 帶入 win-runner）");
    assert!(daemon_src.is_file(), "SW_TEST_SERIALWRAPD 不存在：{}", daemon_src.display());

    let p = sw_pal::native();
    let name = test_task_name(std::process::id());
    assert_test_task_name(&name);
    let w = SupervisionMode::WindowsTask;

    // 防線 2：測試開始時 registry 的全部 COM port（`reg query` ∪ daemon 使用的 RegistryView）。
    let coms = all_registry_com_ports(&*p.env, &*p.registry).unwrap_or_else(|e| panic!("讀取 SERIALCOMM 失敗：{e}"));
    eprintln!("[wintask] registry COM：{coms:?}");
    let root = std::env::temp_dir().join(&name);
    let spec = StagingSpec::new(&root.to_string_lossy(), pick_free_port(), coms);
    spec.write(&daemon_src).unwrap_or_else(|e| panic!("建立 staging 失敗：{e}"));

    let ctl: Arc<dyn ServiceCtl> = Arc::new(WindowsTaskCtl::new(p.env.clone(), p.fs.clone(), p.clock.clone()));
    // 收尾守衛先於 install：任何 panic 都會刪除測試工作並結束 staging daemon。
    let guard = TaskGuard::new(p.env.clone(), ctl.clone(), &name, &spec.daemon_exe());

    // 防線 3：daemon 起來後的第一件事；不符即有序停止、刪除工作並判失敗。
    let checked = |pid: u32, when: &str| -> u32 {
        match verify_daemon_isolated(spec.port) {
            Ok(seen) if seen == pid => seen,
            outcome => {
                let stop = graceful_stop_for_test(&*ctl, &*p.env, &*p.procs, &name, spec.port, pid);
                let _ = ctl.uninstall_named(&w, &name, false);
                panic!(
                    "{when}：隔離防線不成立（{outcome:?}），已停止並刪除測試工作（{stop:?}）\ndaemon.log：{}",
                    spec.log_tail(4000)
                );
            }
        }
    };

    let def = SupervisorDefinition {
        mode: w.clone(),
        name: name.clone(),
        exec: vec![spec.batch_path()],
        working_dir: Some(PathBuf::from(spec.dir())),
        run_user: None,
        env: vec![],
        definition_path: Some(PathBuf::from(spec.task_xml_path())),
        register_held: false,
    };
    let ran = ctl.install(&def, false);
    assert!(!ran.is_empty() && ran.iter().all(|c| c.rc == 0), "{ran:?}");
    let xml = query_xml(&*p.env, &name);
    assert_eq!(xml_text(&xml, "Settings/ExecutionTimeLimit"), "PT0S");
    assert_eq!(xml_text(&xml, "Settings/MultipleInstancesPolicy"), "IgnoreNew");
    assert_eq!(xml_text(&xml, "Settings/RestartOnFailure/Interval"), "PT1M");
    assert_eq!(xml_text(&xml, "Settings/RestartOnFailure/Count"), "999");

    assert!(ctl.action_named(&w, &name, ServiceAction::Start, false).ok, "/Run 失敗");
    let pid0 = try_wait_daemon_pid(spec.port, Duration::from_secs(20))
        .unwrap_or_else(|| panic!("20 s 內測試 daemon 未回應 health.status\ndaemon.log：{}", spec.log_tail(4000)));
    let pid0 = checked(pid0, "首次啟動");

    taskkill_force(&*p.env, pid0, &spec.daemon_exe());
    let t0 = Instant::now();
    let pid1 = try_wait_new_daemon_pid(spec.port, pid0, Duration::from_secs(150)).unwrap_or_else(|| {
        panic!(
            "150 s 內排程未重新拉起 daemon（舊 pid {pid0}；BLOCKED-OWNER (3)：TerminateProcess 是否觸發 RestartOnFailure）\ndaemon.log：{}",
            spec.log_tail(4000)
        )
    });
    assert!(t0.elapsed() <= Duration::from_secs(150) && pid1 != pid0, "150 s 內須由 RestartOnFailure 重新拉起");
    let pid1 = checked(pid1, "失敗重啟後");

    let r = graceful_stop_for_test(&*ctl, &*p.env, &*p.procs, &name, spec.port, pid1);
    assert!(r.ok, "{r:?}\ndaemon.log：{}", spec.log_tail(4000));
    assert!(r.reachable, "daemon.stop 必須送達：{r:?}");
    assert!(!r.ran_end, "daemon 正常結束時不得執行 /End：{r:?}");
    assert!(wait_task_not_running(&*ctl, &name, Duration::from_secs(10)), "停止後工作須回到非執行中");
    let wal = wal_tail_state(&PathBuf::from(spec.wal_dir()));
    eprintln!("[wintask] WAL 尾端：{wal:?}");
    assert!(wal_last_line_crc_ok(&PathBuf::from(spec.wal_dir())), "有序停止後 WAL 最後一筆完整：{wal:?}");

    guard.finish().unwrap_or_else(|e| panic!("收尾未完成：{e}"));
}
