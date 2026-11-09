//! P8 Task 13：`windows-task` 監管 Rust daemon 的實跑驗證（`cap:supervisor_runtime`，案例 `s24-sup-wintask-runtime`，SC-24）。
//!
//! 只在擁有者授權的 Windows host 由編排者執行（擁有者裁決 2026-11-08「T13 的 Windows 排程工作實跑」）：
//!
//! ```text
//! cargo build -p sw-daemon --features test-fakes --bin serialwrapd --target x86_64-pc-windows-gnu
//! cargo build -p sw-cli --features test-fakes --bin wintask_stop_harness --target x86_64-pc-windows-gnu
//! SW_TEST_ALLOW_SCHTASKS=1 SW_WIN_EXTRA_FILES="$PWD/target/x86_64-pc-windows-gnu/debug/serialwrapd.exe:$PWD/target/x86_64-pc-windows-gnu/debug/wintask_stop_harness.exe" \
//!   scripts/rust/win-test.sh --package sw-pal --caps supervisor_runtime -- --test wintask_runtime
//! ```
//!
//! `-- --test wintask_runtime` 只建置並執行本測試檔：sw-pal 另有既有的
//! `service_ctl::cap_supervisor_runtime__windows_task_roundtrip`（建立 `serialwrap-captest-<pid>` 工作），不在本次擁有者
//! 授權的工作名稱（`sw-p8-test-<pid>`）內，不得一併執行。
//!
//! `win-runner.sh` 把 daemon 與 harness 複製到測試執行檔旁，另以 `SW_TEST_SERIALWRAPD` 告知 daemon 路徑；本測試再複製到自己的
//! staging（`%TMP%\sw-p8-test-<pid>\bin`），只以該完整路徑辨識與終止行程。未授權（缺 `SW_TEST_ALLOW_SCHTASKS=1`）或缺
//! daemon 執行檔時 panic（證據不足，不得計為 PASS）。三道 COM 防線與收尾見 `support/wintask.rs`。
//!
//! 排程工作建立與收尾使用具名 API（`uninstall_named`／`query_named`）。正式停止由同 staging 的
//! `wintask_stop_harness.exe` 執行 `sw-cli` 的 P4 `graceful_stop`；PAL 邊界只允許測試工作與 staging RPC 埠。
#![allow(non_snake_case)]

mod support;

#[cfg(windows)]
#[test]
#[ignore = "cap:supervisor_runtime"]
// 函式名與 nodeid 已列入 L1 鎖；目前驗證 OD-83 的手動恢復路徑。
fn cap_supervisor_runtime__task_pt0s_restart_and_graceful_stop() {
    use std::path::PathBuf;
    use std::sync::Arc;
    use std::time::Duration;

    use support::wintask::*;
    use sw_pal::service_ctl::{
        ServiceAction, ServiceCtl, SupervisionMode, SupervisorDefinition, TaskQuery, WindowsTaskCtl,
    };

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
    let harness = locate_harness().unwrap_or_else(|e| panic!("{e}"));

    let ctl: Arc<dyn ServiceCtl> = Arc::new(WindowsTaskCtl::new(p.env.clone(), p.fs.clone(), p.clock.clone()));
    // 收尾守衛先於 install：任何 panic 都會刪除測試工作並結束 staging daemon。
    let guard = TaskGuard::new(p.env.clone(), ctl.clone(), &name, &spec.daemon_exe());

    // 防線 3：daemon 起來後的第一件事；不符即有序停止、刪除工作並判失敗。
    let checked = |pid: u32, when: &str| -> u32 {
        match verify_daemon_isolated(spec.port) {
            Ok(seen) if seen == pid => seen,
            outcome => {
                let stop = formal_service_stop(&*p.env, &harness, &spec, &name);
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
    assert!(
        wait_task_not_running(&*ctl, &name, Duration::from_secs(10)),
        "taskkill_force 後排程工作應停止，才能開始 OD-83 觀察窗"
    );
    let crash_observation = observe_no_task_restart(
        &*p.env,
        &*ctl,
        spec.port,
        &name,
        &spec.daemon_exe(),
        pid0,
        Duration::from_secs(90),
    )
    .unwrap_or_else(|e| {
        panic!(
            "OD-83：0.5.0 已知限制應為強制終止後 90 s 內沒有新 daemon pid、排程工作不是 Running；長期要對齊 Linux 自動重啟。{e}\ndaemon.log：{}",
            spec.log_tail(4000)
        )
    });
    assert!(
        crash_observation.elapsed >= Duration::from_secs(90)
            && matches!(crash_observation.final_task_state, TaskQuery::NotRunning(_))
            && crash_observation.final_process_pids.is_empty(),
        "OD-83：0.5.0 已知限制應為強制終止後 90 s 內沒有新 daemon pid、排程工作不是 Running；長期要對齊 Linux 自動重啟。觀察結果：{crash_observation:?}"
    );
    eprintln!("[wintask] OD-83 崩潰後不自動重啟觀察：{crash_observation:?}");

    let started = ctl.action_named(&w, &name, ServiceAction::Start, false);
    assert!(started.ok, "正式 service start（/Run）失敗：{started:?}");
    let pid1 = try_wait_new_daemon_pid(spec.port, pid0, Duration::from_secs(20)).unwrap_or_else(|| {
        panic!(
            "正式 service start 後 20 s 內沒有新的 daemon pid（舊 pid {pid0}）\ndaemon.log：{}",
            spec.log_tail(4000)
        )
    });
    checked(pid1, "service start 恢復後");

    let r = formal_service_stop(&*p.env, &harness, &spec, &name)
        .unwrap_or_else(|e| panic!("正式 service stop 失敗：{e}\ndaemon.log：{}", spec.log_tail(4000)));
    assert!(r.ok, "{r:?}\ndaemon.log：{}", spec.log_tail(4000));
    assert!(r.sent_stop, "daemon.stop 必須送達：{r:?}");
    assert_eq!(r.graceful, Some(true), "正式 graceful_stop 必須回報 graceful=true：{r:?}");
    assert!(!r.ran_end, "daemon 正常結束時不得執行 /End：{r:?}");
    assert!(wait_task_not_running(&*ctl, &name, Duration::from_secs(10)), "停止後工作須回到非執行中");
    guard.finish().unwrap_or_else(|e| panic!("收尾未完成：{e}"));

    let wal_dir = PathBuf::from(spec.wal_dir());
    let wal = wal_tail_state(&PathBuf::from(spec.wal_dir()));
    eprintln!("[wintask] WAL 尾端：{wal:?}");
    let evidence = wal_evidence(&wal_dir)
        .unwrap_or_else(|e| panic!("停止後 WAL 沒有完整紀錄：{e}; tail={wal:?}"));
    let staged_evidence = wal_test_record_evidence(&wal_dir)
        .unwrap_or_else(|e| panic!("停止後沒有可核對的 staging daemon WAL 紀錄：{e}; tail={wal:?}"));
    eprintln!("[wintask] staging WAL 證據（records, last_seq）：{evidence:?}");
    assert_eq!(staged_evidence, evidence, "通用 WAL 與 staging 紀錄證據應指向同一尾端");
}
