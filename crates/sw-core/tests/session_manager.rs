//! P5-T16：`SessionManager`（裝置排序 #100、動態 session、selector、管理操作）紅燈測試。
//!
//! 測試以真的 `SessionManager::new` 組裝（見 `support::ManagerRig`）；紅燈階段函式本體為 `s21_todo!`，
//! 因此本檔的測試應於執行期失敗（`not implemented: slice21 T<n> <symbol>`），而非編譯錯誤。
#![cfg(feature = "test-fakes")]

mod support;

use std::time::{Duration, Instant};

use support::*;
use sw_core::session_core::{SessionRequest, SessionState};
use sw_core::session_manager::SessionManager;
use sw_core::custody::CustodyPhaseSnapshot;
use sw_core::state_store::{LoadedState, ReleasedRow};
use sw_pal::fakes::serial::FakeWriteStep;
use sw_pal::traits::{
    DeviceInfo, DevicePath, HolderReport, HolderScanStatus, OpenError, PalError, PalErrorKind,
    SerialBackend, SerialConfig, WatcherEvent,
};
use sw_proto::pycompat::PyErr;
use sw_proto::pyjson::{PyDict, PyInt, PyValue};

/// 管理操作與 route 的 RPC 等待上限。
const RPC: Duration = Duration::from_secs(5);

/// `route`／管理操作的 `Result` 解包（Err 時把 PyErr 訊息帶進 panic 訊息）。
fn rpc(r: Result<PyDict, PyErr>) -> PyDict {
    match r {
        Ok(d) => d,
        Err(e) => panic!("RPC 不應回 Err：{}", e.message),
    }
}

fn wp510_release(manager: &SessionManager, sid: &str) -> Result<PyDict, PyErr> {
    manager.route(
        sid,
        SessionRequest::Release {
            source: "agent".to_string(),
            reason: PyValue::Null,
        },
        RPC,
    )
}

/// 回應 dict 的 `ok` 欄位是否為 true。
fn is_ok(d: &PyDict) -> bool {
    d.get("ok") == Some(&PyValue::Bool(true))
}

/// 回應 dict 的 `error_code`（字串）。
fn code_of(d: &PyDict) -> Option<String> {
    s_of(d, "error_code")
}

/// `session` 子 dict（回應中的 `session` 欄位）。
fn session_field(d: &PyDict) -> PyDict {
    match d.get("session") {
        Some(PyValue::Dict(s)) => s.clone(),
        other => panic!("回應應含 session dict，實際 {other:?}"),
    }
}

/// 單一 target 的 YAML（shell 模板 `t`）。
fn t_target(com: &str, act_no: u32, by_id: &str) -> String {
    format!(
        "{{com: {com}, profile: t, act_no: {act_no}, alias: t+{act_no}, device_by_id: {by_id}}}"
    )
}

/// 單一 target 的 YAML（passthrough 模板 `others-template`）。
fn others_target(com: &str, act_no: u32, by_id: &str, alias: &str) -> String {
    format!("{{com: {com}, profile: others-template, act_no: {act_no}, alias: {alias}, device_by_id: {by_id}}}")
}

#[test]
fn rank_assigns_com_by_sorted_by_id_before_watcher_start() {
    let m = ManagerRig::templates_only(); // default.yaml 的 templates，無 targets
    m.devices(&["/s/by-path/pci-0", "/s/by-id/usb-B", "/s/by-id/usb-A"]);
    m.mgr.prepare_dynamic_rank(&m.device_keys());
    m.mgr.update_devices(&m.snapshot());
    m.mgr.bootstrap_attach();
    m.wait_sessions(3);
    let by_com = m.com_by_id();
    assert_eq!(by_com["/s/by-id/usb-A"], "COM0");
    assert_eq!(by_com["/s/by-id/usb-B"], "COM1");
    assert_eq!(by_com["/s/by-path/pci-0"], "COM2");
}

#[test]
fn rebind_drains_before_reopen() {
    // Review Focus 2：同一 by_id 改指到另一實體路徑時，先完整 drain 舊 handle，才 open 新路徑。
    let m = ManagerRig::single_target(SHELL_PROFILE).ready();
    let old = m.dev.clone();
    m.retarget_by_id("/s/by-id/usb-A", "/dev/ttyNEW");
    m.wait_state("COM0", "READY");
    assert_eq!(old.close_count(), 1);
    let ev = m.events();
    let close_old = ev
        .iter()
        .position(|e| e == "close:/dev/ttyOLD")
        .expect("舊 handle 應被關閉");
    let open_new = ev
        .iter()
        .position(|e| e == "open:/dev/ttyNEW")
        .expect("新路徑應被開啟");
    assert!(
        close_old < open_new,
        "事件順序應為 close(舊) 先於 open(新)：{ev:?}"
    );
    assert_eq!(
        s_of(&m.session("COM0"), "attached_real_path").as_deref(),
        Some("/dev/ttyNEW")
    );
    assert!(m
        .last_error_history("COM0")
        .contains(&"DEVICE_REBOUND_REQUIRED".to_string()));
}

#[test]
fn rank_excludes_released_explicit_bound() {
    // released 的 by_id、YAML target 的 by_id、binding override 的值都不參與動態排序；已用的 COM（COM0）跳過。
    let yaml = manager_yaml(&[&t_target("COM0", 1, "/s/by-id/usb-Y")]);
    let mut loaded = LoadedState::default();
    loaded.released.insert(
        "prpl-template:COM9".to_string(),
        ReleasedRow {
            by_id: PyValue::Str("/s/by-id/usb-R".to_string()),
            released_by: PyValue::Str("agent".to_string()),
            released_at: PyValue::Str("2026-10-21T00:00:00".to_string()),
            reason: PyValue::Str("manual".to_string()),
        },
    );
    loaded.bindings.insert(
        "prpl-template:COM8".to_string(),
        "/s/by-id/usb-O".to_string(),
    );
    let m = ManagerRig::with(&yaml, loaded, None);
    m.devices(&[
        "/s/by-id/usb-A",
        "/s/by-id/usb-R",
        "/s/by-id/usb-O",
        "/s/by-id/usb-Y",
    ]);
    m.mgr.prepare_dynamic_rank(&m.device_keys());
    m.mgr.update_devices(&m.snapshot());
    m.mgr.bootstrap_attach();
    wait_until("usb-A 取得動態 COM", || {
        m.com_by_id().contains_key("/s/by-id/usb-A")
    });
    assert_eq!(m.com_by_id()["/s/by-id/usb-A"], "COM1");
    assert_eq!(m.com_by_id()["/s/by-id/usb-Y"], "COM0");
}

#[test]
fn next_dynamic_com_for_hotplug() {
    // 已有 COM0、COM2（explicit target，裝置未在線）；熱插的新裝置應得到最小的空號 COM1。
    let yaml = manager_yaml(&[
        &others_target("COM0", 1, "/s/by-id/usb-X1", "x1"),
        &others_target("COM2", 2, "/s/by-id/usb-X2", "x2"),
    ]);
    let m = ManagerRig::with(&yaml, LoadedState::default(), None);
    m.wait_sessions(2);
    // COM0／COM2 的裝置在線（使用中），否則依 Python `_attach_by_id` 的 DETACHED-rebind fallback，
    // 新裝置會改綁到離線且 act_no 最小的 DETACHED target（見 detached_rebind_fallback_smallest_act_no），
    // 而不是取得新的動態 COM。
    m.add_device_at("/s/by-id/usb-X1", "/dev/ttyX1");
    m.add_device_at("/s/by-id/usb-X2", "/dev/ttyX2");
    m.devices(&["/s/by-id/usb-N"]);
    m.mgr.prepare_dynamic_rank(&m.device_keys());
    m.mgr.update_devices(&m.snapshot());
    m.mgr.bootstrap_attach();
    wait_until("熱插裝置取得 COM", || {
        m.com_by_id().contains_key("/s/by-id/usb-N")
    });
    assert_eq!(m.com_by_id()["/s/by-id/usb-N"], "COM1");
}

#[test]
fn dynamic_session_naming() {
    // sticky 模板 prpl-template 的第 3 個動態 session：COM2 → session_id／alias／act_no。
    let mut loaded = LoadedState::default();
    for by_id in ["/s/by-id/usb-A", "/s/by-id/usb-B", "/s/by-id/usb-C"] {
        loaded
            .profile_detected
            .insert(by_id.to_string(), "prpl-template".to_string());
    }
    let m = ManagerRig::with(&manager_yaml(&[]), loaded, None);
    m.mgr.prepare_dynamic_rank(&[
        "/s/by-id/usb-A".to_string(),
        "/s/by-id/usb-B".to_string(),
        "/s/by-id/usb-C".to_string(),
    ]);
    // act_no＝建立當下的 session 數＋1（Python `_session_from_template`），並發 attach 時取決於完成順序；
    // 依序上線使「第 3 個建立的 session」確定為 usb-C（rank 預配 COM2）。
    for (n, by_id) in ["/s/by-id/usb-A", "/s/by-id/usb-B", "/s/by-id/usb-C"]
        .into_iter()
        .enumerate()
    {
        m.devices(&[by_id]);
        m.mgr.update_devices(&m.snapshot());
        m.wait_sessions(n + 1);
    }
    let s = m.session("COM2");
    assert_eq!(
        s_of(&s, "session_id").as_deref(),
        Some("prpl-template:COM2")
    );
    assert_eq!(s_of(&s, "alias").as_deref(), Some("prpl-template+3"));
    assert_eq!(s.get("act_no"), Some(&PyValue::Int(PyInt::Small(3))));
}

#[test]
fn resolution_order_pin_sticky_detect_fallback() {
    // (a) pin 存在 → profile_source "pin"（pin 優先於 sticky）。
    let mut loaded = LoadedState::default();
    loaded
        .profile_pins
        .insert("/s/by-id/usb-P".to_string(), "others-template".to_string());
    loaded
        .profile_detected
        .insert("/s/by-id/usb-P".to_string(), "prpl-template".to_string());
    let m = ManagerRig::with(&manager_yaml(&[]), loaded, None);
    m.devices(&["/s/by-id/usb-P"]);
    m.mgr.update_devices(&m.snapshot());
    m.mgr.bootstrap_attach();
    assert_eq!(m.wait_profile_source("COM0"), "pin");

    // (b) 無 pin、有 sticky → "sticky"。
    let mut loaded = LoadedState::default();
    loaded
        .profile_detected
        .insert("/s/by-id/usb-P".to_string(), "prpl-template".to_string());
    let m = ManagerRig::with(&manager_yaml(&[]), loaded, None);
    m.devices(&["/s/by-id/usb-P"]);
    m.mgr.update_devices(&m.snapshot());
    m.mgr.bootstrap_attach();
    assert_eq!(m.wait_profile_source("COM0"), "sticky");

    // (c) 無 pin、無 sticky；PROBE 送 `\n` 後 prompt 命中 prpl-template → "detected"，且探測 3.0 s。
    let m = ManagerRig::with(&manager_yaml(&[]), LoadedState::default(), None);
    m.devices(&["/s/by-id/usb-P"]);
    m.reply_probe(&m.real_of("/s/by-id/usb-P"), b"root@prpl:/# ");
    m.mgr.update_devices(&m.snapshot());
    m.mgr.bootstrap_attach();
    assert_eq!(m.wait_profile_source("COM0"), "detected");
    assert!(
        m.clock.sleeps_s().contains(&3.0),
        "偵測應等待 3.0 s（Clock::sleep）：{:?}",
        m.clock.sleeps_s()
    );

    // (d) 都不中 → 非 command_capable passthrough："fallback"。
    let m = ManagerRig::with(&manager_yaml(&[]), LoadedState::default(), None);
    m.devices(&["/s/by-id/usb-P"]);
    m.mgr.update_devices(&m.snapshot());
    m.mgr.bootstrap_attach();
    assert_eq!(m.wait_profile_source("COM0"), "fallback");
}

#[test]
fn detect_skips_passthrough_and_console_unlock() {
    // P7 Task 3（08 §0.6 退役清單，取代 06 T16 `detect_skips_passthrough_console_unlock_and_unsupported`）：
    // Python `login_fsm.detect_template` 只略過 passthrough 與 `console_unlock_env`；`post_login_cmd` 模板（brcm）恢復為候選。
    const DETECT_TEMPLATES: &str = "profiles:\n  pt-template:\n    platform: passthrough\n    prompt_regex: '.*'\n    login_regex: '$^'\n    password_regex: '$^'\n    ready_probe: ''\n  cu-template:\n    platform: shell\n    prompt_regex: '(?m)^root@x:.*# '\n    console_unlock_env: SW_UNLOCK\n    timeout_s: 2.0\n  pl-template:\n    platform: shell\n    prompt_regex: '(?m)^root@x:.*# '\n    post_login_cmd: sh\n    timeout_s: 2.0\n";
    let m = ManagerRig::with(DETECT_TEMPLATES, LoadedState::default(), None);
    m.devices(&["/s/by-id/usb-D"]);
    m.reply_probe(&m.real_of("/s/by-id/usb-D"), b"root@x:/# ");
    m.mgr.update_devices(&m.snapshot());
    m.mgr.bootstrap_attach();
    assert_eq!(m.wait_profile_source("COM0"), "detected");
    assert_eq!(
        s_of(&m.session("COM0"), "profile").as_deref(),
        Some("pl-template"),
        "略過 passthrough 與 console unlock 後，post_login_cmd 模板為第一個命中者"
    );
}

#[test]
fn probe_bridge_closed_with_receipt_before_real_open() {
    // parity-amendments-2026-10-26 #51 改正後語意（名稱保留於 L1 鎖）：Python P1 T10 已凍結「PROBE 與正式 bridge
    // 共用同一預開 port」（session_manager.py `_preopen_dynamic_attach_port`／`owns_port=False`）。
    // 命中模板時整段只有一次 open、中間沒有 close；只有 PROBE 未命中且無任何可用模板時才 close 並取得回條。
    let m = ManagerRig::with(&manager_yaml(&[]), LoadedState::default(), None);
    m.devices(&["/s/by-id/usb-D"]);
    let real = m.real_of("/s/by-id/usb-D");
    m.reply_probe(&real, b"root@prpl:/# ");
    m.mgr.update_devices(&m.snapshot());
    m.mgr.bootstrap_attach();
    assert_eq!(m.wait_profile_source("COM0"), "detected");
    wait_until("正式 bridge 接手（ATTACHING 結束）", || {
        m.state_of("COM0").as_deref() != Some("ATTACHING")
    });
    assert_eq!(
        m.events(),
        vec![format!("open:{real}")],
        "PROBE 與正式 bridge 共用同一 handle：只有一次 open、沒有 close"
    );
    assert_eq!(m.serial.open_attempts(&real), 1);
    assert_eq!(m.device(&real).close_count(), 0);
    let probe_rows = m
        .wal_rows()
        .into_iter()
        .filter(|r| s_of(r, "com").as_deref() == Some("PROBE"))
        .count();
    assert!(probe_rows >= 2, "偵測的 RX／TX 應以 com == PROBE 寫入 WAL");

    // 變體：PROBE 未命中且無任何 fallback 模板 → 關閉 PROBE handle（回條 excl_cleared:true → 外部可再 open）。
    const NO_FALLBACK: &str = "profiles:\n  prpl-template:\n    platform: prpl\n    prompt_regex: '(?m)^root@prpl:.*# '\n    timeout_s: 2.0\n";
    let m2 = ManagerRig::with(NO_FALLBACK, LoadedState::default(), None);
    m2.devices(&["/s/by-id/usb-D"]);
    let real2 = m2.real_of("/s/by-id/usb-D");
    m2.mgr.update_devices(&m2.snapshot());
    m2.mgr.bootstrap_attach();
    wait_until("PROBE handle 關閉", || {
        m2.events().contains(&format!("close:{real2}"))
    });
    assert_eq!(
        m2.events(),
        vec![format!("open:{real2}"), format!("close:{real2}")]
    );
    assert_eq!(m2.mgr.counts().0, 0, "無模板時不建立 session");
    assert!(
        m2.serial
            .open_exclusive(&DevicePath(real2.clone()), &probe_cfg())
            .is_ok(),
        "close 回條 excl_cleared:true 後外部可再開啟"
    );
}

#[test]
fn dynamic_probe_and_bridge_share_preopened_port() {
    // #51：動態 detect 的 PROBE handle 命中模板後原樣交給正式 bridge；正式 bridge 在同一 handle 上完成登入至 READY，
    // 期間外部 open 恆得 Held（無「PROBE 關閉 → 正式 open」的無鎖空窗）。
    let m = ManagerRig::with(&manager_yaml(&[]), LoadedState::default(), None);
    m.devices(&["/s/by-id/usb-S"]);
    let real = m.real_of("/s/by-id/usb-S");
    let dev = m.device(&real);
    install_shell_replies(&dev);
    dev.on_tx_reply(b"\n\n", b"root@t:/# ");
    m.mgr.update_devices(&m.snapshot());
    m.mgr.bootstrap_attach();
    m.wait_state("COM0", "READY");
    assert_eq!(s_of(&m.session("COM0"), "profile_source").as_deref(), Some("detected"));
    assert_eq!(m.serial.open_attempts(&real), 1, "整段只 open 一次");
    assert_eq!(dev.open_count(), 1);
    assert_eq!(dev.close_count(), 0, "PROBE 與正式 bridge 之間沒有 close");
    assert!(
        !m.events().iter().any(|e| e.starts_with("close:")),
        "無 CloseReceipt 介於 PROBE 與正式 bridge 之間：{:?}",
        m.events()
    );
    match m.serial.open_exclusive(&DevicePath(real.clone()), &probe_cfg()) {
        Err(OpenError::Held(_)) => {}
        Err(other) => panic!("外部 open 應得 Held，實際 {other:?}"),
        Ok(_) => panic!("外部 open 應得 Held，實際成功"),
    }
}

#[test]
fn sticky_persisted_only_when_ready_and_path_unchanged() {
    // READY 且 real_path 未變 → state.json.profile_detected[by_id] 寫入。
    let m = ManagerRig::with(&manager_yaml(&[]), LoadedState::default(), None);
    m.devices(&["/s/by-id/usb-S"]);
    let dev_s = m.device(&m.real_of("/s/by-id/usb-S"));
    install_shell_replies(&dev_s);
    // FakeDevice 的 `on_tx_reply` 只觸發一次：第一個 `\n` 被偵測 PROBE 用掉；登入 probe 的第二個 `\n`
    // 需另一個回應（實機每次都會回 prompt）。
    dev_s.on_tx_reply(b"\n\n", b"root@t:/# ");
    m.mgr.update_devices(&m.snapshot());
    m.mgr.bootstrap_attach();
    m.wait_state("COM0", "READY");
    wait_until("profile_detected 寫入 state.json", || {
        m.state_text().contains("profile_detected") && m.state_text().contains("usb-S")
    });

    // READY 前 real_path 改變 → 不寫 sticky。
    const BY_ID: &str = "/s/by-id/usb-S";
    let m2 = ManagerRig::with(&manager_yaml(&[]), LoadedState::default(), None);
    m2.devices(&[BY_ID]);
    let old_real = m2.real_of(BY_ID);
    // 只回應 login 的 prompt，不回應 ready_probe nonce，使 session 停在 READY 之前。
    m2.reply_probe(&old_real, b"root@t:/# ");
    let old_device = m2.device(&old_real);
    let (open_enter_tx, open_enter_rx) = crossbeam_channel::bounded(1);
    let (open_resume_tx, open_resume_rx) = crossbeam_channel::bounded(1);
    struct ResumeOnDrop(crossbeam_channel::Sender<()>);
    impl Drop for ResumeOnDrop {
        fn drop(&mut self) {
            let _ = self.0.try_send(());
        }
    }
    let _resume_open = ResumeOnDrop(open_resume_tx.clone());
    m2.mgr.set_open_bridge_post_take_hook_for_test(Box::new(move || {
        let _ = open_enter_tx.send(());
        let _ = open_resume_rx.recv();
    }));
    m2.mgr.update_devices(&m2.snapshot());
    m2.mgr.bootstrap_attach();
    assert!(
        open_enter_rx.recv_timeout(RPC).is_ok(),
        "舊路徑 open worker 應抵達已接手 custody 的同步屏障，events={:?}",
        m2.events()
    );
    let old_custody = m2
        .mgr
        .custody_for_test(BY_ID)
        .expect("open worker 接手時應有舊路徑 custody");
    assert!(
        matches!(
            &old_custody.phase,
            sw_core::custody::CustodyPhaseSnapshot::Taken { .. }
        ),
        "同步屏障須位於舊路徑已接手、尚未交給 session 的階段：{old_custody:?}"
    );
    assert_eq!(
        m2.state_of("COM0").as_deref(),
        Some("ATTACHING"),
        "open 屏障時 session 必須仍未 READY"
    );
    assert!(
        m2.events().iter().any(|event| event == &format!("open:{old_real}")),
        "舊路徑必須已實際進入 open：{:?}",
        m2.events()
    );
    open_resume_tx
        .send(())
        .expect("放行舊路徑 open worker");
    wait_until("舊路徑登入結束但尚未 READY", || {
        m2.state_of("COM0").as_deref() == Some("ATTACHED")
    });
    assert!(
        m2.mgr
            .wait_attach_idle_for_test(BY_ID, Duration::from_secs(5)),
        "改指前舊路徑 attach worker 必須已收尾，events={:?}",
        m2.events()
    );

    struct ReleasePauseOnDrop(sw_pal::fakes::serial::FakeDevice);
    impl Drop for ReleasePauseOnDrop {
        fn drop(&mut self) {
            self.0.release_pause();
        }
    }
    old_device.pause_next_close();
    let _release_close = ReleasePauseOnDrop(old_device.clone());
    m2.retarget_by_id_no_wait(BY_ID, "/dev/ttyMOVED");
    assert!(
        old_device.wait_paused(RPC),
        "改指後舊路徑 close 應停在尚未回覆 CloseReceipt 的屏障"
    );
    assert_eq!(
        m2.serial.open_attempts("/dev/ttyMOVED"),
        0,
        "單次在途規則要求舊路徑回條前不嘗試開新路徑：{:?}",
        m2.events()
    );
    old_device.release_pause();
    // close:<path> 只記錄 close 呼叫；以放行 close 後 actor 才能重開新路徑作為回條已消費的屏障。
    wait_until("新路徑已 open", || {
        m2.events()
            .iter()
            .any(|event| event == "open:/dev/ttyMOVED")
    });
    assert_eq!(old_device.close_count(), 1, "舊路徑 close 恰好一次");
    assert!(!old_device.is_open(), "舊路徑 close 後 handle 已關閉");
    let events = m2.events();
    let close_old = events
        .iter()
        .position(|event| event == &format!("close:{old_real}"))
        .expect("舊路徑應留下 close 記錄");
    let open_new = events
        .iter()
        .position(|event| event == "open:/dev/ttyMOVED")
        .expect("新路徑應留下 open 記錄");
    assert!(
        close_old < open_new,
        "新路徑不得早於舊路徑 close receipt 開啟：{events:?}"
    );
    assert!(
        m2.mgr
            .wait_attach_idle_for_test(BY_ID, Duration::from_secs(5)),
        "改指後 attach worker 必須已收尾，events={events:?}"
    );
    m2.wait_state("COM0", "READY");
    // state.json 一律寫出 profile_detected 區段（Python `_save_state` 的 5 個 key），故以載入結果判斷「未寫入」。
    assert!(
        m2.load_state_now().profile_detected.is_empty(),
        "READY 前 real_path 改變時不得寫入 sticky：{}",
        m2.state_text()
    );
}

#[test]
fn detached_rebind_fallback_smallest_act_no() {
    // 新 by_id 無 session；兩個 DETACHED 且其 by_id 離線的 session → 綁到 act_no 較小者（COM1），並寫 binding override。
    let yaml = manager_yaml(&[
        &others_target("COM0", 2, "/s/by-id/usb-X1", "a2"),
        &others_target("COM1", 1, "/s/by-id/usb-X2", "a1"),
    ]);
    let m = ManagerRig::with(&yaml, LoadedState::default(), None);
    m.devices(&["/s/by-id/usb-NEW"]);
    m.mgr.update_devices(&m.snapshot());
    wait_until("usb-NEW 綁到 COM1", || {
        m.com_by_id().get("/s/by-id/usb-NEW").map(String::as_str) == Some("COM1")
    });
    wait_until("binding override 寫入 state.json", || {
        m.state_text().contains("/s/by-id/usb-NEW")
    });
}

#[test]
fn max_sessions_warning() {
    // 已 16 個 session → 第 17 個裝置不建 session，並寫 WARNING。
    let m = ManagerRig::with(&manager_yaml(&[]), LoadedState::default(), None);
    let keys: Vec<String> = (0..16).map(|i| format!("/s/by-id/usb-{i:02}")).collect();
    let refs: Vec<&str> = keys.iter().map(String::as_str).collect();
    m.devices(&refs);
    m.mgr.prepare_dynamic_rank(&m.device_keys());
    m.mgr.update_devices(&m.snapshot());
    m.mgr.bootstrap_attach();
    m.wait_sessions(16);
    m.devices(&["/s/by-id/usb-16"]);
    m.mgr.update_devices(&m.snapshot());
    wait_until("max_sessions WARNING", || {
        m.warnings()
            .iter()
            .any(|w| w == "已達 max_sessions=16，忽略裝置 /s/by-id/usb-16")
    });
    assert_eq!(m.mgr.counts().0, 16, "第 17 個裝置不得建立 session");
}

#[test]
fn binding_override_cleanup_186() {
    // 啟動時 binding override 指向另一 target 宣告的 by_id → 刪除（#186）；session 維持 YAML 宣告的 by_id。
    let yaml = manager_yaml(&[
        &t_target("COM0", 1, "/s/by-id/usb-A"),
        &t_target("COM1", 2, "/s/by-id/usb-B"),
    ]);
    let mut loaded = LoadedState::default();
    loaded
        .bindings
        .insert("t:COM0".to_string(), "/s/by-id/usb-B".to_string());
    let m = ManagerRig::with(&yaml, loaded, None);
    m.mgr.bootstrap_attach();
    assert_eq!(
        s_of(&m.session("COM0"), "device_by_id").as_deref(),
        Some("/s/by-id/usb-A")
    );
    wait_until("磁碟上的 binding override 已刪除", || {
        !m.load_state_now().bindings.contains_key("t:COM0")
    });
}

#[test]
fn selector_resolution_order() {
    // session_id 精確優先；否則依建立順序取第一個 com == sel || alias == sel。
    let yaml = manager_yaml(&[
        &others_target("COM0", 1, "/s/by-id/usb-A", "COM1"),
        &others_target("COM1", 2, "/s/by-id/usb-B", "b"),
        &others_target("COM2", 3, "/s/by-id/usb-C", "others-template:COM1"),
    ]);
    let m = ManagerRig::with(&yaml, LoadedState::default(), None);
    let sid0 = m.mgr.resolve("COM0").expect("COM0 session");
    let sid1 = m.mgr.resolve("COM1").expect("COM1 session");
    // alias "COM1" 與 COM1 的 com 同時命中，COM0 的 session 先建立 → sid0。
    assert_eq!(m.mgr.resolve("COM1"), Some(sid0));
    // session_id 精確命中優先於 COM2 的 alias。
    assert_eq!(m.mgr.resolve(&sid1), Some(sid1.clone()));
    assert_eq!(m.mgr.resolve("nope"), None);
}

#[test]
fn device_removed_drains_to_detached() {
    let m = ManagerRig::single_target(SHELL_PROFILE).ready();
    m.mgr.update_devices(&[]);
    m.wait_state("COM0", "DETACHED");
    assert_eq!(
        s_of(&m.session("COM0"), "last_error").as_deref(),
        Some("DEVICE_REMOVED")
    );
    assert_eq!(m.dev.close_count(), 1);
}

#[test]
fn bind_paths() {
    let yaml = manager_yaml(&[
        &t_target("COM0", 1, "/s/by-id/usb-A"),
        &t_target("COM1", 2, "/s/by-id/usb-B"),
    ]);
    let m = ManagerRig::with(&yaml, LoadedState::default(), None);
    m.add_device_at("/s/by-id/usb-A", "/dev/ttyA");
    m.mgr.update_devices(&m.snapshot());
    m.mgr.bootstrap_attach();
    wait_until("COM0 已取得 usb-A", || {
        m.com_by_id().get("/s/by-id/usb-A").map(String::as_str) == Some("COM0")
    });
    // attach（登入 probe）完成才有 bridge＋ATTACHED；ATTACHING 期間 bind 同值不屬冪等分支（Python 同）。
    m.wait_state("COM0", "ATTACHED");

    // 空 by_id → INVALID_ARGS。
    let r = rpc_bind(&m, "COM0", "");
    assert_eq!(code_of(&r).as_deref(), Some("INVALID_ARGS"));

    // 他 session 已綁此 by_id → DEVICE_ALREADY_BOUND（附 device_by_id 與 session_id）。
    let r = rpc_bind(&m, "COM1", "/s/by-id/usb-A");
    assert_eq!(code_of(&r).as_deref(), Some("DEVICE_ALREADY_BOUND"));
    assert_eq!(s_of(&r, "device_by_id").as_deref(), Some("/s/by-id/usb-A"));
    assert_eq!(s_of(&r, "session_id"), m.mgr.resolve("COM0"));

    // 同值 → 成功且 already_bound:true。
    let r = rpc_bind(&m, "COM0", "/s/by-id/usb-A");
    assert!(is_ok(&r));
    assert_eq!(r.get("already_bound"), Some(&PyValue::Bool(true)));

    // 成功綁定到尚未在線的 by_id：COM1 改綁 usb-N（binding override），之後 usb-N 上線即由 COM1 接管。
    let r = rpc_bind(&m, "COM1", "/s/by-id/usb-N");
    assert!(is_ok(&r), "bind 應成功：{r:?}");
    m.add_device_at("/s/by-id/usb-N", "/dev/ttyN");
    m.mgr.update_devices(&m.snapshot());
    wait_until("usb-N 由 COM1 接管", || {
        m.com_by_id().get("/s/by-id/usb-N").map(String::as_str) == Some("COM1")
    });
    assert_ne!(m.state_of("COM1").as_deref(), Some("DETACHED"));

    // RELEASED 被 bind 視為離開 RELEASED。
    let mut loaded = LoadedState::default();
    loaded.released.insert(
        "t:COM1".to_string(),
        ReleasedRow {
            by_id: PyValue::Str("/s/by-id/usb-B".to_string()),
            released_by: PyValue::Str("agent".to_string()),
            released_at: PyValue::Str("2026-10-21T00:00:00".to_string()),
            reason: PyValue::Str("manual".to_string()),
        },
    );
    let m2 = ManagerRig::with(&yaml, loaded, None);
    assert_eq!(m2.state_of("COM1").as_deref(), Some("RELEASED"));
    let r = rpc_bind(&m2, "COM1", "/s/by-id/usb-N");
    assert!(is_ok(&r));
    assert_ne!(m2.state_of("COM1").as_deref(), Some("RELEASED"));
}

#[test]
fn pin_unpin_paths() {
    let yaml = manager_yaml(&[&t_target("COM0", 1, "/s/by-id/usb-X")]);
    let m = ManagerRig::with(&yaml, LoadedState::default(), None);
    // COM0 的裝置在線：否則 usb-D 會依 DETACHED-rebind fallback 改綁到離線的 COM0（Python 1:1）。
    m.add_device_at("/s/by-id/usb-X", "/dev/ttyX");
    m.devices(&["/s/by-id/usb-D"]);
    m.mgr.prepare_dynamic_rank(&m.device_keys());
    m.mgr.update_devices(&m.snapshot());
    m.mgr.bootstrap_attach();
    wait_until("動態 COM1 取得 usb-D", || {
        m.com_by_id().contains_key("/s/by-id/usb-D")
    });

    // 未知 template → UNKNOWN_PROFILE。
    let r = rpc(m.mgr.pin("COM1", "no-such-template"));
    assert_eq!(code_of(&r).as_deref(), Some("UNKNOWN_PROFILE"));
    // 解析不到 device key → DEVICE_NOT_FOUND。
    let r = rpc(m.mgr.pin("COM9", "prpl-template"));
    assert_eq!(code_of(&r).as_deref(), Some("DEVICE_NOT_FOUND"));
    // YAML target 的 session → PROFILE_IS_EXPLICIT。
    let r = rpc(m.mgr.pin("COM0", "prpl-template"));
    assert_eq!(code_of(&r).as_deref(), Some("PROFILE_IS_EXPLICIT"));

    // 成功：{"ok":true,"device_key":…,"profile":…}，且不 reattach（裝置 open 次數不變）。
    let real = m.real_of("/s/by-id/usb-D");
    let opens_before = m.serial.open_attempts(&real);
    let r = rpc(m.mgr.pin("COM1", "prpl-template"));
    assert!(is_ok(&r));
    assert_eq!(s_of(&r, "device_key").as_deref(), Some("/s/by-id/usb-D"));
    assert_eq!(s_of(&r, "profile").as_deref(), Some("prpl-template"));
    assert_eq!(
        m.serial.open_attempts(&real),
        opens_before,
        "pin 不得 reattach"
    );

    let r = rpc(m.mgr.unpin("COM1"));
    assert!(is_ok(&r));
}

#[test]
fn clear_paths() {
    // RELEASED → {"ok":true,"released":true,"session":…}。
    let m = ManagerRig::single_target(SHELL_PROFILE).ready();
    let sid = m.mgr.resolve("COM0").expect("COM0 session");
    rpc(m.mgr.route(
        &sid,
        SessionRequest::Release {
            source: "agent".to_string(),
            reason: PyValue::Str("t".to_string()),
        },
        RPC,
    ));
    m.wait_state("COM0", "RELEASED");
    let r = rpc(m.mgr.route(&sid, SessionRequest::Clear, RPC));
    assert!(is_ok(&r));
    assert_eq!(r.get("released"), Some(&PyValue::Bool(true)));
    assert!(matches!(r.get("session"), Some(PyValue::Dict(_))));

    // 非 RELEASED → drain(CLEARED) → 裝置重新 open（事件：close 先於第二次 open）。
    let m2 = ManagerRig::single_target(SHELL_PROFILE).ready();
    let sid2 = m2.mgr.resolve("COM0").expect("COM0 session");
    let before = m2.dev.open_count();
    let r = rpc(m2.mgr.route(&sid2, SessionRequest::Clear, RPC));
    assert!(is_ok(&r), "clear 應成功：{r:?}");
    wait_until("clear 後裝置重新 open", || {
        m2.dev.open_count() == before + 1
    });
    assert_eq!(m2.dev.close_count(), 1);
}

#[test]
fn session_attach_paths() {
    let rig_s = |yaml: &str| ManagerRig::with(yaml, LoadedState::default(), None);

    // 無 by_id 的 target：Python `load_profiles` 直接丟棄（config.py `if not device_by_id: continue`），
    // 不會建立 session，故 `attach_session` 的 DEVICE_NOT_BOUND 分支對外不可達（Rust 保留同一分支）。
    let m = rig_s(&manager_yaml(&[
        "{com: COM0, profile: t, act_no: 1, alias: t+1}",
    ]));
    assert_eq!(m.mgr.resolve("COM0"), None);
    assert_eq!(m.mgr.counts().0, 0);

    // 無 bridge 且裝置不在快照 → DEVICE_NOT_FOUND，並設 DETACHED。
    let m = rig_s(SHELL_PROFILE);
    let sid = m.mgr.resolve("COM0").expect("COM0 session");
    let r = rpc(m.mgr.route(&sid, SessionRequest::SessionAttach, RPC));
    assert_eq!(code_of(&r).as_deref(), Some("DEVICE_NOT_FOUND"));
    assert_eq!(m.state_of("COM0").as_deref(), Some("DETACHED"));

    // passthrough：ATTACHED 同步 attach 不 probe（SC-35），回 {"ok":true,"session":…}，零 TX。
    let m = ManagerRig::single_target(PASSTHROUGH_PROFILE);
    m.mgr.update_devices(&m.snapshot());
    m.mgr.bootstrap_attach();
    m.wait_state("COM0", "ATTACHED");
    let sid = m.mgr.resolve("COM0").expect("COM0 session");
    let r = rpc(m.mgr.route(&sid, SessionRequest::SessionAttach, RPC));
    assert!(is_ok(&r));
    assert!(matches!(r.get("session"), Some(PyValue::Dict(_))));
    assert!(
        m.dev.take_tx().is_empty(),
        "passthrough 的 session.attach 不得送出任何 TX"
    );

    // RELEASED → released:true＋recommended_action:"device_attach"。
    let m = ManagerRig::single_target(SHELL_PROFILE).ready();
    let sid = m.mgr.resolve("COM0").expect("COM0 session");
    rpc(m.mgr.route(
        &sid,
        SessionRequest::Release {
            source: "agent".to_string(),
            reason: PyValue::Str("t".to_string()),
        },
        RPC,
    ));
    m.wait_state("COM0", "RELEASED");
    let r = rpc(m.mgr.route(&sid, SessionRequest::SessionAttach, RPC));
    assert_eq!(r.get("released"), Some(&PyValue::Bool(true)));
    assert_eq!(
        s_of(&r, "recommended_action").as_deref(),
        Some("device_attach")
    );

    // ATTACHED＋CREDENTIALS_UNRESOLVED → SESSION_NOT_READY；頂層 last_error／last_error_detail 與 session 同名欄位一致。
    let m = rig_s(CRED_PROFILE);
    let dev = m.add_device_at("/s/by-id/usb-A", "/dev/ttyOLD");
    dev.on_tx_reply(b"\n", b"localhost login: ");
    m.mgr.update_devices(&m.snapshot());
    m.mgr.bootstrap_attach();
    wait_until("CREDENTIALS_UNRESOLVED", || {
        m.session_last_error("COM0").as_deref() == Some("CREDENTIALS_UNRESOLVED")
    });
    let sid = m.mgr.resolve("COM0").expect("COM0 session");
    let r = rpc(m.mgr.route(&sid, SessionRequest::SessionAttach, RPC));
    let keys: Vec<String> = r.0.keys().cloned().collect();
    assert_eq!(
        keys,
        [
            "ok",
            "error_code",
            "last_error",
            "last_error_detail",
            "session"
        ]
    );
    assert_eq!(r.get("ok"), Some(&PyValue::Bool(false)));
    assert_eq!(code_of(&r).as_deref(), Some("SESSION_NOT_READY"));
    let sess = session_field(&r);
    assert_eq!(r.get("last_error"), sess.get("last_error"));
    assert_eq!(r.get("last_error_detail"), sess.get("last_error_detail"));
    assert_eq!(
        s_of(&r, "last_error").as_deref(),
        Some("CREDENTIALS_UNRESOLVED")
    );

    // 不回應 prompt 時 Python／Rust 皆會把 probe 失敗碼（PROMPT_UNAVAILABLE）寫入 last_error，頂層兩欄與 session 同名欄位一致。
    // merge P7：原以 Slice 2.1 能力閘（post_login_cmd：不自動登入、不送 TX）取得「ATTACHED 且 last_error 為 None」；
    // P7 Task 3 依 08 §0.6 退役該閘後，post_login_cmd profile 與 Python 相同會送 probe 並寫入失敗碼（Python 無此閘，
    // 無 human lease 的 capable ATTACHED 不會停在 last_error None）。改斷言 Python 實際輸出；null 分支待 P6 human lease 可用時補回。
    const POST_LOGIN_PROFILE: &str = "profiles:\n  g:\n    platform: shell\n    prompt_regex: '(?m)^root@t:.*# '\n    post_login_cmd: sh\n    timeout_s: 2.0\ntargets:\n  - {com: COM0, profile: g, act_no: 1, alias: g+1, device_by_id: /s/by-id/usb-A}\n";
    let m = rig_s(POST_LOGIN_PROFILE);
    let dev = m.add_device_at("/s/by-id/usb-A", "/dev/ttyOLD");
    let _ = dev;
    m.mgr.update_devices(&m.snapshot());
    m.mgr.bootstrap_attach();
    m.wait_state("COM0", "ATTACHED");
    let sid = m.mgr.resolve("COM0").expect("COM0 session");
    let r = rpc(m.mgr.route(&sid, SessionRequest::SessionAttach, RPC));
    assert_eq!(code_of(&r).as_deref(), Some("SESSION_NOT_READY"));
    let sess = session_field(&r);
    assert_eq!(r.get("last_error"), Some(&PyValue::from("PROMPT_UNAVAILABLE")));
    assert_eq!(r.get("last_error"), sess.get("last_error"));
    assert_eq!(r.get("last_error_detail"), sess.get("last_error_detail"));

    // FLASHING → FLASHING_BUSY；RECOVERING → SESSION_RECOVERING（不變）。
    let m = ManagerRig::single_target(SHELL_PROFILE).ready();
    let sid = m.mgr.resolve("COM0").expect("COM0 session");
    m.mgr.force_session_state("COM0", SessionState::Flashing);
    let r = rpc(m.mgr.route(&sid, SessionRequest::SessionAttach, RPC));
    assert_eq!(code_of(&r).as_deref(), Some("FLASHING_BUSY"));
    m.mgr.force_session_state("COM0", SessionState::Recovering);
    let r = rpc(m.mgr.route(&sid, SessionRequest::SessionAttach, RPC));
    assert_eq!(code_of(&r).as_deref(), Some("SESSION_RECOVERING"));
}

#[test]
fn alias_set_persists_but_not_applied_after_restart() {
    let m = ManagerRig::single_target(SHELL_PROFILE).ready();
    let sid = m.mgr.resolve("COM0").expect("COM0 session");
    let r = rpc(m.mgr.alias_set(&sid, "x"));
    assert!(is_ok(&r));
    assert!(has_str(&PyValue::Dict(m.mgr.alias_list()), "x"));
    // 同一 state.json 重建 manager：alias 已保存，但 resolve 不套用（WAL-D15 KEEP）。
    let m2 = m.restart();
    assert_eq!(m2.mgr.resolve("x"), None);
}

#[test]
fn alias_unassign_saves_before_check() {
    let m = ManagerRig::single_target(SHELL_PROFILE).ready();
    let before = m.state_writes();
    let r = rpc(m.mgr.alias_unassign("nope"));
    assert_eq!(code_of(&r).as_deref(), Some("ALIAS_NOT_FOUND"));
    assert_eq!(
        m.state_writes(),
        before + 1,
        "不存在的 alias 仍須先重寫 state.json 一次"
    );
}

#[test]
fn shutdown_budget_5s() {
    // COM0 的 writer 永久阻塞；shutdown(5s) 於 FakeClock 的 5 s 內返回，COM0 drained:false，COM1 drained:true。
    let yaml = manager_yaml(&[
        &t_target("COM0", 1, "/s/by-id/usb-A"),
        &t_target("COM1", 2, "/s/by-id/usb-B"),
    ]);
    let m = ManagerRig::with(&yaml, LoadedState::default(), None);
    m.add_device_at("/s/by-id/usb-A", "/dev/ttyOLD");
    install_shell_replies(&m.device("/dev/ttyOLD"));
    m.add_device_at("/s/by-id/usb-B", "/dev/ttyB");
    install_shell_replies(&m.device("/dev/ttyB"));
    m.mgr.update_devices(&m.snapshot());
    m.mgr.bootstrap_attach();
    m.wait_state("COM0", "READY");
    m.wait_state("COM1", "READY");

    let sid0 = m.mgr.resolve("COM0").expect("COM0 session");
    m.device("/dev/ttyOLD")
        .script_write(vec![FakeWriteStep::BlockForever]);
    let dev0 = m.device("/dev/ttyOLD");
    let submits = dev0.os_submits();
    let _ = m
        .mgr
        .route(&sid0, SessionRequest::Submit(agent_submit("sleep 1")), RPC);
    // 命令 TX 已交 OS 並阻塞（writer 卡住）後才 shutdown；否則 Phase 1 先凍結 fence，TX 未提交即不會阻塞。
    wait_until("命令 TX 阻塞於 writer", || dev0.os_submits() > submits);

    let t0 = m.clock.elapsed();
    let res = m.mgr.shutdown(Duration::from_secs(5));
    let elapsed = m.clock.elapsed() - t0;
    assert!(
        elapsed <= Duration::from_secs(5),
        "shutdown 應於 5 s 內返回：{elapsed:?}"
    );
    let sid1 = m.mgr.resolve("COM1").expect("COM1 session");
    let drained = |sid: &str| {
        res.iter()
            .find(|(k, _)| k == sid)
            .map(|(_, d)| *d)
            .unwrap_or_else(|| panic!("結果應含 {sid}：{res:?}"))
    };
    assert!(!drained(&sid0), "COM0 的 writer 阻塞，應 drained:false");
    assert!(drained(&sid1), "COM1 應 drained:true");
}

/// wp510r 第 2 次整包審查 High：Phase 1 後回收者仍須接收 actor drain 送來的 worker 並結算回條。
#[test]
fn wp510r_r18_reaper_stays_live_through_phase1() {
    let _guard = SPAWN_TEST_LOCK.lock().unwrap_or_else(|e| e.into_inner());

    let manager = ManagerRig::templates_only();
    let harness = CoreHarness::new(SHELL_PROFILE);
    let path = "/dev/wp510r-r18-reaper-phase1";
    let device = harness.add_serial_device(path);
    let worker = harness.start_worker_at(1803, path);
    let token = manager.mgr.custody_register_bridged_worker_for_test(
        "COM83",
        "by-id-wp510r-r18-reaper-phase1",
        path,
        1803,
        worker,
    );
    let (actor, worker_token, epoch, worker) = manager
        .mgr
        .custody_take_bridged_worker_for_test("by-id-wp510r-r18-reaper-phase1")
        .expect("取得 actor 尚未接手的真實 IoWorker");
    assert_eq!(token, worker_token);

    let drain = manager
        .mgr
        .shutdown_phase1(Instant::now() + Duration::from_secs(1));
    std::thread::sleep(Duration::from_millis(100));
    let reaper_alive = manager.mgr.reaper_finished_for_test() == Some(false);
    let accepted = sw_core::custody::send_reclaim_or_finish(
        &manager.mgr.reaper_cmd_sender_for_test(),
        actor,
        worker_token,
        epoch,
        sw_core::custody::ReclaimWorker::Real(worker),
        None,
    );
    if accepted {
        wait_until("Phase 1 後送入的 worker 收到配對終態", || {
            manager.mgr.terminal_for_test(token).is_some()
        });
    }
    let report = manager.mgr.shutdown_phase2(drain);
    if !accepted {
        wait_until("回收者已停止時的 fallback 仍關閉 worker", || {
            device.close_count() == 1
        });
    }

    assert!(reaper_alive, "Phase 1 後回收者仍須接收 actor drain 工作");
    assert!(accepted, "Phase 1 後 Reclaim 必須成功入列");
    assert!(
        report.is_empty(),
        "Phase 2 開始前回收已收斂，不應回報未完成工作：{report:?}"
    );
    assert_eq!(device.close_count(), 1, "真實 worker handle 恰好關閉一次");
    assert_eq!(
        manager
            .mgr
            .terminal_for_test(token)
            .map(|terminal| terminal.handle),
        Some(sw_core::custody::TerminalHandle::Closed),
        "Phase 2 收到配對 Closed 終態"
    );
}

/// wp510r 第 2 次整包審查 High：回收者的 writer 或 close 卡住時，shutdown 沿用共用期限返回，背景收尾仍保有 worker。
#[test]
fn wp510r_r18_shutdown_reaper_deadline_preserves_blocked_worker_cleanup() {
    let _guard = SPAWN_TEST_LOCK.lock().unwrap_or_else(|e| e.into_inner());

    let writer_manager = ManagerRig::templates_only();
    let writer_harness = CoreHarness::new(SHELL_PROFILE);
    let writer_path = "/dev/wp510r-r18-reaper-deadline-writer";
    let writer_device = writer_harness.add_serial_device(writer_path);
    writer_device.script_write(vec![FakeWriteStep::BlockForever]);
    writer_harness
        .fence
        .with(|fence| fence.active_epoch = Some(1801));
    let writer = writer_harness.start_worker_at(1801, writer_path);
    let (report_tx, _report_rx) = crossbeam_channel::bounded(1);
    writer
        .tx()
        .jobs
        .send(sw_core::tx_writer::TxJob {
            permit: sw_core::tx_writer::TxPermit {
                op_id: sw_core::tx_writer::OpId(1),
                epoch: 1801,
                owner_gen: 0,
                fsm_gen: 0,
                class: sw_core::tx_writer::TxClass::Command,
                source: "agent".to_string(),
                cmd_id: None,
                log: false,
                input_epoch: None,
                replay_hold: None,
                probe_id: None,
            },
            payload: b"blocked\n".to_vec(),
            reply: report_tx,
            aggregate: None,
        })
        .expect("送出會卡在 fake writer 的 TX");
    writer_device.wait_blocked();
    let writer_token = writer_manager.mgr.custody_register_bridged_worker_for_test(
        "COM81",
        "by-id-wp510r-r18-deadline-writer",
        writer_path,
        1801,
        writer,
    );
    let (writer_actor, worker_token, writer_epoch, writer) = writer_manager
        .mgr
        .custody_take_bridged_worker_for_test("by-id-wp510r-r18-deadline-writer")
        .expect("取得 writer 阻塞中的真實 IoWorker");
    assert_eq!(writer_token, worker_token);
    writer_manager
        .mgr
        .reaper_cmd_sender_for_test()
        .send(sw_core::custody::ReaperCmd::Reclaim {
            actor: writer_actor,
            token: worker_token,
            epoch: writer_epoch,
            worker: Box::new(sw_core::custody::ReclaimWorker::Real(writer)),
        })
        .expect("回收者受理 writer 回收命令");
    wait_until("writer reclaim 已進入 BridgeDraining", || {
        matches!(
            writer_manager
                .mgr
                .custody_for_test("by-id-wp510r-r18-deadline-writer")
                .map(|entry| entry.phase),
            Some(CustodyPhaseSnapshot::BridgeDraining { .. })
        )
    });
    let start = Instant::now();
    let writer_report = writer_manager.mgr.shutdown(Duration::from_millis(120));
    assert!(
        start.elapsed() < Duration::from_millis(600),
        "writer 永久阻塞不得卡住 shutdown：{:?}",
        start.elapsed()
    );
    assert!(
        writer_report.contains(&("reaper".to_string(), false)),
        "reaper 必須在共用期限到時回報未收斂：{writer_report:?}"
    );
    assert!(
        writer_report.contains(&("attach:by-id-wp510r-r18-deadline-writer".to_string(), false)),
        "未完成的真實 worker 必須列入 shutdown 報告：{writer_report:?}"
    );
    writer_device.release_block();
    wait_until("writer 放行後保管終態完成", || {
        writer_manager.mgr.terminal_for_test(writer_token).is_some()
    });
    assert_eq!(writer_device.close_count(), 1, "背景收尾關閉 writer handle 一次");
    assert!(!writer_device.is_open(), "背景收尾最終釋放 writer handle");
    assert_eq!(
        writer_manager
            .mgr
            .terminal_for_test(writer_token)
            .map(|terminal| terminal.handle),
        Some(sw_core::custody::TerminalHandle::Closed),
        "writer 放行後有配對 Closed 終態"
    );

    let close_manager = ManagerRig::templates_only();
    let close_harness = CoreHarness::new(SHELL_PROFILE);
    let close_path = "/dev/wp510r-r18-reaper-deadline-close";
    let close_device = close_harness.add_serial_device(close_path);
    close_device.pause_next_close();
    let close_worker = close_harness.start_worker_at(1802, close_path);
    let close_token = close_manager.mgr.custody_register_bridged_worker_for_test(
        "COM82",
        "by-id-wp510r-r18-deadline-close",
        close_path,
        1802,
        close_worker,
    );
    let (close_actor, worker_token, close_epoch, close_worker) = close_manager
        .mgr
        .custody_take_bridged_worker_for_test("by-id-wp510r-r18-deadline-close")
        .expect("取得 close 暫停中的真實 IoWorker");
    assert_eq!(close_token, worker_token);
    close_manager
        .mgr
        .reaper_cmd_sender_for_test()
        .send(sw_core::custody::ReaperCmd::Reclaim {
            actor: close_actor,
            token: worker_token,
            epoch: close_epoch,
            worker: Box::new(sw_core::custody::ReclaimWorker::Real(close_worker)),
        })
        .expect("回收者受理 close 回收命令");
    assert!(close_device.wait_paused(Duration::from_secs(5)), "worker 應停在 fake close");
    let start = Instant::now();
    let close_report = close_manager.mgr.shutdown(Duration::from_millis(120));
    assert!(
        start.elapsed() < Duration::from_millis(600),
        "close 永久阻塞不得卡住 shutdown：{:?}",
        start.elapsed()
    );
    assert!(
        close_report.contains(&("reaper".to_string(), false)),
        "close 卡住時 reaper 應回報未收斂：{close_report:?}"
    );
    assert!(
        close_report.contains(&("attach:by-id-wp510r-r18-deadline-close".to_string(), false)),
        "close 卡住的 worker 必須列入 shutdown 報告：{close_report:?}"
    );
    close_device.release_pause();
    wait_until("close 放行後保管終態完成", || {
        close_manager.mgr.terminal_for_test(close_token).is_some()
    });
    assert_eq!(close_device.close_count(), 1, "背景收尾關閉暫停中的 handle 一次");
    assert!(!close_device.is_open(), "背景收尾最終釋放暫停中的 handle");
    assert_eq!(
        close_manager
            .mgr
            .terminal_for_test(close_token)
            .map(|terminal| terminal.handle),
        Some(sw_core::custody::TerminalHandle::Closed),
        "close 放行後有配對 Closed 終態"
    );
}

/// BC-03（產品 executor）：release 中止已部分送出的前景命令時，`command_aborted_in_flight` META 只寫一筆
/// （Python 僅 worker 的 `_map_aborted_result` 寫；Rust 的 drain 回條與 op runner 不得各寫一筆）。
#[test]
fn release_partial_in_flight_command_writes_single_aborted_meta() {
    let m = ManagerRig::single_target(SHELL_PROFILE).ready();
    let sid = m.mgr.resolve("COM0").expect("COM0 session");
    let dev = m.device("/dev/ttyOLD");
    dev.script_write(vec![FakeWriteStep::BlockUntilCancel {
        accept_before_cancel: 4,
    }]);
    let submits = dev.os_submits();
    let sub = rpc(m
        .mgr
        .route(&sid, SessionRequest::Submit(agent_submit("sleep 1")), RPC));
    let cmd_id = s_of(&sub, "cmd_id").expect("cmd_id");
    wait_until("命令 TX 阻塞於 writer", || dev.os_submits() > submits);
    let rel = rpc(m.mgr.route(
        &sid,
        SessionRequest::Release {
            source: "agent".to_string(),
            reason: PyValue::Null,
        },
        RPC,
    ));
    assert!(is_ok(&rel), "{rel:?}");
    m.wait_state("COM0", "RELEASED");
    let aborted = || {
        m.wal_rows()
            .into_iter()
            .filter(|row| {
                row.get("dir") == Some(&PyValue::from("META"))
                    && row.get("cmd_id") == Some(&PyValue::from(cmd_id.as_str()))
                    && matches!(row.get("meta"), Some(PyValue::Dict(meta))
                        if meta.get("event") == Some(&PyValue::from("command_aborted_in_flight")))
            })
            .count()
    };
    wait_until("command_aborted_in_flight META 落盤", || aborted() >= 1);
    // op runner 的 OpFinished 與 drain 回條先後不定：給雙方都完成的時間後再數。
    std::thread::sleep(Duration::from_millis(300));
    assert_eq!(aborted(), 1, "META 應只寫一筆：{:?}", m.wal_rows());
}

#[test]
fn begin_shutdown_blocks_hotplug_open() {
    // SC-11 stopping gate：begin_shutdown 之後的熱插、bootstrap、recover 一律不 open、不建 session。
    let m = ManagerRig::single_target(SHELL_PROFILE).ready();
    let sid0 = m.mgr.resolve("COM0").expect("COM0 session");
    m.mgr.begin_shutdown();

    let new_real = "/dev/ttyNEW";
    m.add_device_at("/s/by-id/usb-NEW", new_real);
    let snap = m.snapshot();
    let _ = m.mgr.watcher_sender().send(WatcherEvent::Delta {
        added: vec![DeviceInfo {
            by_id: "/s/by-id/usb-NEW".to_string(),
            real_path: new_real.to_string(),
        }],
        removed: Vec::new(),
        snapshot: snap,
    });
    m.mgr.bootstrap_attach();
    let r = m.mgr.route(
        &sid0,
        SessionRequest::Recover {
            timeout_s: 1.0,
            force: false,
        },
        RPC,
    );
    match r {
        Err(e) => assert!(e.message.contains("daemon stopping"), "{}", e.message),
        Ok(d) => panic!("stopping 後 recover 應回 Err，實際 {d:?}"),
    }
    assert_eq!(
        m.serial.open_attempts(new_real),
        0,
        "stopping 後不得 open 新裝置"
    );
    assert_eq!(m.mgr.counts().0, 1, "stopping 後不得建立動態 session");
    assert_eq!(m.state_of("COM0").as_deref(), Some("READY"));
    let logs = m
        .warnings()
        .into_iter()
        .filter(|w| w == "shutdown 中，略過 attach /s/by-id/usb-NEW")
        .count();
    assert_eq!(
        logs,
        1,
        "應恰記錄一筆略過 attach 的 log：{:?}",
        m.warnings()
    );

    // #29（parity-amendments-2026-10-26）：stopping gate 早退時既有 session 不得停在（預標的）ATTACHING——
    // 裝置於 begin_shutdown 後才上線的 explicit target 維持 DETACHED、零 open。
    let m2 = ManagerRig::with(SHELL_PROFILE, LoadedState::default(), None);
    m2.mgr.begin_shutdown();
    let real2 = "/dev/ttyLATE";
    m2.add_device_at("/s/by-id/usb-A", real2);
    let _ = m2.mgr.watcher_sender().send(WatcherEvent::Delta {
        added: vec![DeviceInfo {
            by_id: "/s/by-id/usb-A".to_string(),
            real_path: real2.to_string(),
        }],
        removed: Vec::new(),
        snapshot: m2.snapshot(),
    });
    m2.mgr.bootstrap_attach();
    std::thread::sleep(Duration::from_millis(50));
    assert_eq!(m2.state_of("COM0").as_deref(), Some("DETACHED"));
    assert_eq!(m2.serial.open_attempts(real2), 0);
}

#[test]
fn shutdown_includes_inflight_attach() {
    // 熱插的 attach worker 已越過 gate 並卡在 open；shutdown 必須把它納入 drain 集合，open 返回後立即 Phase 1、零 TX。
    let m = ManagerRig::single_target(SHELL_PROFILE).ready();
    let new_real = "/dev/ttyNEW";
    m.serial.block_open(new_real);
    let dev_new = m.add_device_at("/s/by-id/usb-NEW", new_real);
    install_shell_replies(&dev_new);
    let _ = m.mgr.watcher_sender().send(WatcherEvent::Delta {
        added: vec![DeviceInfo {
            by_id: "/s/by-id/usb-NEW".to_string(),
            real_path: new_real.to_string(),
        }],
        removed: Vec::new(),
        snapshot: m.snapshot(),
    });
    wait_until("attach worker 卡在 open", || {
        m.serial.open_attempts(new_real) == 1
    });

    let mgr = m.mgr.clone();
    let shutdown = std::thread::spawn(move || mgr.shutdown(Duration::from_secs(5)));
    std::thread::sleep(Duration::from_millis(50));
    m.serial.release_open(new_real);
    let res = shutdown.join().expect("shutdown thread");

    assert_eq!(
        res.len(),
        2,
        "drain 集合＝READY 的 COM0 ∪ inflight attach：{res:?}"
    );
    assert!(
        res.iter().all(|(_, drained)| *drained),
        "兩者皆應 drained:true：{res:?}"
    );
    assert_eq!(dev_new.close_count(), 1, "新 handle 應被關閉");
    assert!(
        dev_new.take_tx().is_empty(),
        "shutdown 途中的新 handle 不得送出登入 probe TX"
    );
    let attempts = m.serial.open_attempts(new_real);
    std::thread::sleep(Duration::from_millis(100));
    assert_eq!(
        m.serial.open_attempts(new_real),
        attempts,
        "shutdown 返回後不再有任何 open"
    );
}

#[test]
fn released_survives_two_restarts_dynamic() {
    // DF-04：動態 session release 後，以同一 state.json 重建兩次，都是 RELEASED 且不 open 裝置。
    let m = ManagerRig::with(&manager_yaml(&[]), LoadedState::default(), None);
    m.devices(&["/s/by-id/usb-D"]);
    m.mgr.update_devices(&m.snapshot());
    m.mgr.bootstrap_attach();
    m.wait_sessions(1);
    let sid = m.mgr.resolve("COM0").expect("COM0 session");
    rpc(m.mgr.route(
        &sid,
        SessionRequest::Release {
            source: "agent".to_string(),
            reason: PyValue::Str("manual".to_string()),
        },
        RPC,
    ));
    m.wait_state("COM0", "RELEASED");
    wait_until("RELEASED 寫入 state.json", || {
        m.state_text().contains("released")
    });

    let opens = m
        .serial
        .events()
        .iter()
        .filter(|e| e.starts_with("open:"))
        .count();
    let r1 = m.restart();
    r1.mgr.update_devices(&r1.snapshot());
    r1.mgr.bootstrap_attach();
    r1.wait_state("COM0", "RELEASED");
    let r2 = r1.restart();
    r2.mgr.update_devices(&r2.snapshot());
    r2.mgr.bootstrap_attach();
    r2.wait_state("COM0", "RELEASED");
    let opens_after = m
        .serial
        .events()
        .iter()
        .filter(|e| e.starts_with("open:"))
        .count();
    assert_eq!(
        opens_after, opens,
        "RELEASED 的 session 重啟後不得 open 裝置"
    );
}

#[test]
fn interactive_routes_by_id_without_selector() {
    let m = ManagerRig::single_target(SHELL_PROFILE).ready();
    let sid = m.mgr.resolve("COM0").expect("COM0 session");
    let r = rpc(m.mgr.route(
        &sid,
        SessionRequest::InteractiveOpen {
            owner: "agent".to_string(),
            timeout_s: 30.0,
            command: "sh".to_string(),
            allow_attached: false,
        },
        RPC,
    ));
    assert!(is_ok(&r), "interactive_open 應成功：{r:?}");
    let iid = s_of(&r, "interactive_id").expect("interactive_id");

    // 只帶 interactive_id（不帶 selector）即可路由到正確 session。
    let st = rpc(m.mgr.route_interactive(
        &iid,
        SessionRequest::InteractiveStatus {
            interactive_id: iid.clone(),
            screen_chars: 0,
        },
        RPC,
    ));
    assert!(is_ok(&st), "route_interactive 應到達正確 session：{st:?}");

    // 未知 id → INTERACTIVE_NOT_FOUND。
    let unknown = rpc(m.mgr.route_interactive(
        "nope",
        SessionRequest::InteractiveStatus {
            interactive_id: "nope".to_string(),
            screen_chars: 0,
        },
        RPC,
    ));
    assert_eq!(code_of(&unknown).as_deref(), Some("INTERACTIVE_NOT_FOUND"));
    assert_eq!(s_of(&unknown, "interactive_id").as_deref(), Some("nope"));

    // session drain 後索引移除。
    rpc(m.mgr.route(
        &sid,
        SessionRequest::Release {
            source: "agent".to_string(),
            reason: PyValue::Str("t".to_string()),
        },
        RPC,
    ));
    m.wait_state("COM0", "RELEASED");
    let gone = rpc(m.mgr.route_interactive(
        &iid,
        SessionRequest::InteractiveStatus {
            interactive_id: iid.clone(),
            screen_chars: 0,
        },
        RPC,
    ));
    assert_eq!(code_of(&gone).as_deref(), Some("INTERACTIVE_NOT_FOUND"));
}

#[test]
fn fanout_registers_every_session_fence() {
    let m = ManagerRig::with(&manager_yaml(&[]), LoadedState::default(), None);
    m.devices(&["/s/by-id/usb-1", "/s/by-id/usb-2", "/s/by-id/usb-3"]);
    m.mgr.prepare_dynamic_rank(&m.device_keys());
    m.mgr.update_devices(&m.snapshot());
    m.mgr.bootstrap_attach();
    m.wait_sessions(3);

    // WAL 進入 unhealthy（每次寫入失敗）→ 全域 fanout 與三個既有 fence 皆 wal_unhealthy。
    m.fail_wal_writes(PalError::io(5, "[Errno 5] Input/output error"));
    let _ = m.wal.append(sw_core::wal::WalAppendReq {
        com: "COM0",
        dir: "META",
        source: "system",
        payload: &[],
        cmd_id: None,
        loss_flag: false,
        meta: None,
    });
    wait_until("fanout unhealthy", || m.mgr.wal_health_fanout().current());
    for sel in ["COM0", "COM1", "COM2"] {
        assert_eq!(
            m.mgr.session_fence_wal_unhealthy(sel),
            Some(true),
            "{sel} fence"
        );
    }

    // 熱插第 4 個 → 建立即 wal_unhealthy == true（新 fence 繼承目前值）。
    m.devices(&["/s/by-id/usb-4"]);
    m.mgr.update_devices(&m.snapshot());
    m.wait_sessions(4);
    assert_eq!(m.mgr.session_fence_wal_unhealthy("COM3"), Some(true));
}

#[test]
fn management_ops_commit_failure_is_err() {
    let yaml = manager_yaml(&[&t_target("COM0", 1, "/s/by-id/usb-A")]);
    let m = ManagerRig::with(
        &yaml,
        LoadedState::default(),
        Some(("/s/by-id/usb-A", "/dev/ttyOLD")),
    );
    m.devices(&["/s/by-id/usb-D"]);
    m.mgr.update_devices(&m.snapshot());
    m.mgr.bootstrap_attach();
    m.wait_sessions(2);
    let sid0 = m.mgr.resolve("COM0").expect("COM0 session");

    // state.json 寫入失敗 → bind／pin／alias_set 回 Err(PyErr)（framing 轉 EXCEPTION）。
    let eio = || PalError::io(5, "[Errno 5] Input/output error");
    m.fs.fail_next_atomic_write(eio());
    assert!(
        m.mgr.bind("COM0", "/s/by-id/usb-Z").is_err(),
        "bind 的 commit 失敗應回 Err"
    );
    m.fs.fail_next_atomic_write(eio());
    assert!(
        m.mgr.pin("COM1", "prpl-template").is_err(),
        "pin 的 commit 失敗應回 Err"
    );
    m.fs.fail_next_atomic_write(eio());
    assert!(
        m.mgr.alias_set(&sid0, "x").is_err(),
        "alias_set 的 commit 失敗應回 Err"
    );
}

// ---------------------------------------------------------------------------
// parity-amendments-2026-10-26 追加（Task 16）。
// ---------------------------------------------------------------------------

/// `state.json` released 列。
fn released_row(by_id: &str) -> ReleasedRow {
    ReleasedRow {
        by_id: PyValue::Str(by_id.to_string()),
        released_by: PyValue::Str("agent".to_string()),
        released_at: PyValue::Str("2026-10-21T00:00:00".to_string()),
        reason: PyValue::Str("manual".to_string()),
    }
}

#[test]
fn wp510_a_orphan_released_record_materializes_placeholder() {
    const ONLY_PRPL: &str = "profiles:\n  prpl-template:\n    platform: prpl\n    prompt_regex: '(?m)^root@prpl:.*# '\n    timeout_s: 2.0\n";
    let yaml = format!(
        "{ONLY_PRPL}targets:\n  - {{com: COM7, profile: prpl-template, act_no: 1, alias: prpl-template+1, device_by_id: /s/by-id/usb-N}}\n"
    );
    let mut loaded = LoadedState::default();
    loaded.released.insert(
        "gone-template:COM3".to_string(),
        released_row("/s/by-id/usb-G"),
    );
    loaded.released.insert(
        "others-template:COM5".to_string(),
        released_row("/s/by-id/usb-O"),
    );
    loaded.released.insert(
        "passthrough:COM6".to_string(),
        released_row("/s/by-id/usb-P"),
    );
    let m = ManagerRig::with(&yaml, loaded, None);

    let sid = m
        .mgr
        .resolve("gone-template:COM3")
        .expect("缺失 template 的 RELEASED 記錄應可由 session_id selector 找到");
    assert_eq!(sid, "gone-template:COM3");
    let placeholder = m.session(&sid);
    assert_eq!(s_of(&placeholder, "state").as_deref(), Some("RELEASED"));
    assert_eq!(
        s_of(&placeholder, "session_id").as_deref(),
        Some("gone-template:COM3")
    );
    assert_eq!(s_of(&placeholder, "com").as_deref(), Some("COM3"));
    assert_eq!(
        s_of(&placeholder, "profile").as_deref(),
        Some("gone-template")
    );
    assert_eq!(
        s_of(&placeholder, "platform").as_deref(),
        Some("passthrough")
    );
    assert_eq!(
        placeholder.get("command_capable"),
        Some(&PyValue::Bool(false))
    );
    assert_eq!(
        s_of(&placeholder, "alias").as_deref(),
        Some("gone-template+4")
    );
    assert_eq!(
        s_of(&placeholder, "profile_source").as_deref(),
        Some("fallback")
    );
    assert_eq!(
        s_of(&placeholder, "device_by_id").as_deref(),
        Some("/s/by-id/usb-G")
    );
    assert_eq!(s_of(&placeholder, "released_by").as_deref(), Some("agent"));
    assert_eq!(
        s_of(&placeholder, "released_at").as_deref(),
        Some("2026-10-21T00:00:00")
    );
    assert_eq!(
        s_of(&placeholder, "released_reason").as_deref(),
        Some("manual")
    );
    assert_eq!(m.mgr.released_placeholder(&sid), Some(true));
    assert_eq!(placeholder.get("released_placeholder"), None);

    // 通用名稱仍依原有規則使用 passthrough；一般 YAML session 也可正常建立。
    for (sid, profile, com) in [
        ("others-template:COM5", "others-template", "COM5"),
        ("passthrough:COM6", "passthrough", "COM6"),
    ] {
        let session = m.session(sid);
        assert_eq!(s_of(&session, "state").as_deref(), Some("RELEASED"));
        assert_eq!(s_of(&session, "session_id").as_deref(), Some(sid));
        assert_eq!(s_of(&session, "profile").as_deref(), Some(profile));
        assert_eq!(s_of(&session, "com").as_deref(), Some(com));
        assert_eq!(s_of(&session, "platform").as_deref(), Some("passthrough"));
        assert_eq!(session.get("command_capable"), Some(&PyValue::Bool(false)));
        assert_eq!(m.mgr.released_placeholder(sid), Some(false));
    }
    assert_eq!(m.mgr.resolve("COM7").as_deref(), Some("prpl-template:COM7"));
    assert_eq!(m.mgr.released_placeholder("COM7"), Some(false));
    assert_eq!(m.mgr.counts().0, 4);

    let device_g = m.add_device_at("/s/by-id/usb-G", "/dev/ttyG");
    let device_o = m.add_device_at("/s/by-id/usb-O", "/dev/ttyO");
    let device_p = m.add_device_at("/s/by-id/usb-P", "/dev/ttyP");
    m.mgr.update_devices(&m.snapshot());
    m.mgr.bootstrap_attach();
    std::thread::sleep(Duration::from_millis(100));
    for path in ["/dev/ttyG", "/dev/ttyO", "/dev/ttyP"] {
        assert_eq!(m.serial.open_attempts(path), 0, "RELEASED 不得 open {path}");
    }
    for device in [device_g, device_o, device_p] {
        assert!(device.take_tx().is_empty(), "RELEASED 不得送 UART TX");
    }
}

#[test]
fn wp510_a_placeholder_persists_across_restarts() {
    const ONLY_PRPL: &str = "profiles:\n  prpl-template:\n    platform: prpl\n    prompt_regex: '(?m)^root@prpl:.*# '\n    timeout_s: 2.0\n";
    let mut loaded = LoadedState::default();
    loaded.released.insert(
        "gone-template:COM3".to_string(),
        released_row("/s/by-id/usb-G"),
    );
    let m = ManagerRig::with(ONLY_PRPL, loaded, None);
    m.add_device_at("/s/by-id/usb-G", "/dev/ttyG");

    let mut current = m;
    for restart_no in 0..=2 {
        let sid = current
            .mgr
            .resolve("gone-template:COM3")
            .unwrap_or_else(|| panic!("第 {restart_no} 次啟動應實體化佔位 session"));
        assert_eq!(sid, "gone-template:COM3");
        let session = current.session(&sid);
        assert_eq!(s_of(&session, "state").as_deref(), Some("RELEASED"));
        assert_eq!(s_of(&session, "platform").as_deref(), Some("passthrough"));
        assert_eq!(session.get("command_capable"), Some(&PyValue::Bool(false)));
        assert_eq!(current.mgr.released_placeholder(&sid), Some(true));
        assert_eq!(
            s_of(&session, "device_by_id").as_deref(),
            Some("/s/by-id/usb-G")
        );
        assert!(
            current
                .load_state_now()
                .released
                .contains_key("gone-template:COM3"),
            "第 {restart_no} 次啟動後 state.json 仍須保留 RELEASED 阻擋"
        );

        current.mgr.update_devices(&current.snapshot());
        current.mgr.bootstrap_attach();
        std::thread::sleep(Duration::from_millis(100));
        assert_eq!(current.serial.open_attempts("/dev/ttyG"), 0);
        assert!(current.serial.device("/dev/ttyG").take_tx().is_empty());

        if restart_no < 2 {
            current = current.restart();
        }
    }
}

/// 建立缺失 template 的 OD-63 佔位，供 WP5-10a 自動路徑回歸共用。
fn wp510_a_placeholder_rig() -> ManagerRig {
    const ONLY_PRPL: &str = "profiles:\n  prpl-template:\n    platform: prpl\n    prompt_regex: '(?m)^root@prpl:.*# '\n    timeout_s: 2.0\n";
    let mut loaded = LoadedState::default();
    loaded.released.insert(
        "gone-template:COM3".to_string(),
        released_row("/s/by-id/usb-G"),
    );
    ManagerRig::with(ONLY_PRPL, loaded, None)
}

/// 重現 Python 第三輪防線繞過情境：持久化 released 阻擋移除，核心狀態變成 DETACHED，但佔位標記保留。
fn wp510_a_clear_persisted_block_and_detach(m: &ManagerRig) {
    const SID: &str = "gone-template:COM3";
    assert_eq!(m.mgr.released_placeholder(SID), Some(true));
    assert!(m.mgr.clear_released_block_for_test(SID));
    assert!(!m.load_state_now().released.contains_key(SID));
    m.mgr.force_session_state(SID, SessionState::Detached);
    m.wait_state(SID, "DETACHED");
    assert_eq!(m.mgr.released_placeholder(SID), Some(true));
}

/// 佔位自動路徑必須沒有 open、UART 寫入、WAL TX 或正式 bridge。
fn wp510_a_assert_no_attach_io(m: &ManagerRig, paths: &[&str]) {
    for path in paths {
        assert_eq!(m.serial.open_attempts(path), 0, "佔位不得 open {path}");
        assert!(m.device(path).take_tx().is_empty(), "佔位不得寫入 {path}");
    }
    assert!(
        m.wal_rows()
            .iter()
            .all(|row| s_of(row, "dir").as_deref() != Some("TX")),
        "佔位不得留下 WAL TX：{:?}",
        m.wal_rows()
    );
    let session = m.session("gone-template:COM3");
    assert_eq!(
        session.get("attached_real_path"),
        Some(&PyValue::Null),
        "佔位不得建立 bridge：{session:?}"
    );
    assert_eq!(
        session.get("bridge_generation").and_then(PyValue::as_i64),
        Some(0),
        "佔位不得建立 bridge：{session:?}"
    );
}

#[test]
fn wp510_a_attach_worker_never_opens_placeholder() {
    const BY_ID: &str = "/s/by-id/usb-G";
    const REAL: &str = "/dev/ttyG";
    let m = wp510_a_placeholder_rig();
    let _device = m.add_device_at(BY_ID, REAL);
    wp510_a_clear_persisted_block_and_detach(&m);

    m.mgr.update_devices(&m.snapshot());
    m.mgr.bootstrap_attach();
    assert!(m
        .mgr
        .wait_attach_idle_for_test(BY_ID, Duration::from_secs(5)));

    assert_eq!(m.mgr.released_placeholder("gone-template:COM3"), Some(true));
    assert_eq!(
        m.state_of("gone-template:COM3").as_deref(),
        Some("DETACHED")
    );
    wp510_a_assert_no_attach_io(&m, &[REAL]);
}

#[test]
fn wp510_a_hotplug_keeps_placeholder_blocked() {
    const BY_ID: &str = "/s/by-id/usb-G";
    const FIRST: &str = "/dev/ttyG-first";
    const RETARGETED: &str = "/dev/ttyG-retargeted";
    let m = wp510_a_placeholder_rig();
    let _first = m.add_device_at(BY_ID, FIRST);
    wp510_a_clear_persisted_block_and_detach(&m);

    // 上線。
    m.mgr.update_devices(&m.snapshot());
    assert!(m
        .mgr
        .wait_attach_idle_for_test(BY_ID, Duration::from_secs(5)));
    wp510_a_assert_no_attach_io(&m, &[FIRST]);

    // 拔除。
    m.mgr.update_devices(&[]);
    assert!(m
        .mgr
        .wait_attach_idle_for_test(BY_ID, Duration::from_secs(5)));
    wp510_a_assert_no_attach_io(&m, &[FIRST]);

    // 重插同一 by_id。
    m.mgr.update_devices(&m.snapshot());
    assert!(m
        .mgr
        .wait_attach_idle_for_test(BY_ID, Duration::from_secs(5)));
    wp510_a_assert_no_attach_io(&m, &[FIRST]);

    // 同一 by_id 改指到新實體路徑。
    let _retargeted = m.add_device_at(BY_ID, RETARGETED);
    m.mgr.update_devices(&m.snapshot());
    assert!(m
        .mgr
        .wait_attach_idle_for_test(BY_ID, Duration::from_secs(5)));

    // 啟動掃描及 watcher tick 也不得替佔位重掛。
    m.mgr.bootstrap_attach();
    m.mgr
        .watcher_sender()
        .send(WatcherEvent::Tick)
        .expect("watcher tick 應排入");
    m.mgr.on_tick();
    assert!(m
        .mgr
        .wait_attach_idle_for_test(BY_ID, Duration::from_secs(5)));
    assert_eq!(m.mgr.released_placeholder("gone-template:COM3"), Some(true));
    assert_eq!(
        m.state_of("gone-template:COM3").as_deref(),
        Some("DETACHED")
    );
    wp510_a_assert_no_attach_io(&m, &[FIRST, RETARGETED]);
}

#[test]
fn wp510_a_placeholder_rejects_commands() {
    const BY_ID: &str = "/s/by-id/usb-G";
    const REAL: &str = "/dev/ttyG";
    let m = wp510_a_placeholder_rig();
    let _device = m.add_device_at(BY_ID, REAL);
    m.mgr.update_devices(&m.snapshot());
    assert_eq!(
        m.state_of("gone-template:COM3").as_deref(),
        Some("RELEASED")
    );

    let sid = m.mgr.resolve("gone-template:COM3").expect("佔位 session");
    let mut req = agent_submit("echo wp510-placeholder");
    req.session_id = sid.clone();
    let result = rpc(m.mgr.route(&sid, SessionRequest::Submit(req), RPC));

    // Python command.submit 對 RELEASED 的優先序為 SESSION_NOT_READY；file.push 另在
    // passthrough profile gate 回 PROFILE_NOT_COMMAND_CAPABLE（由 Python 基線回歸覆核）。
    assert_eq!(
        code_of(&result).as_deref(),
        Some("SESSION_NOT_READY"),
        "{result:?}"
    );
    assert_eq!(m.mgr.session_queue_lens("gone-template:COM3"), Some((0, 0)));
    assert_eq!(m.mgr.released_placeholder("gone-template:COM3"), Some(true));
    wp510_a_assert_no_attach_io(&m, &[REAL]);
}

/// WP5-10b：建立缺失 template 的佔位，並附上舊 sid 的持久化 alias／binding。
fn wp510_b_placeholder_rig(with_slot: bool, keep_profile_records: bool) -> ManagerRig {
    const SID: &str = "gone-template:COM3";
    const BY_ID: &str = "/s/by-id/usb-G";
    let mut loaded = LoadedState::default();
    loaded.released.insert(SID.to_string(), released_row(BY_ID));
    loaded
        .bindings
        .insert(SID.to_string(), "/s/by-id/old-override".to_string());
    let mut alias = PyDict::new();
    alias.insert("session_id", PyValue::from(SID));
    loaded.aliases.insert("retired-alias".to_string(), alias);
    if keep_profile_records {
        loaded
            .profile_pins
            .insert(BY_ID.to_string(), "removed-pin-template".to_string());
        loaded
            .profile_detected
            .insert(BY_ID.to_string(), "removed-detected-template".to_string());
    }
    let yaml = if with_slot {
        manager_yaml(&[&t_target("COM0", 1, "/s/by-id/offline-slot")])
    } else {
        manager_yaml(&[])
    };
    let m = ManagerRig::with(&yaml, loaded, None);
    m.add_device_at(BY_ID, "/dev/ttyG");
    install_shell_replies(&m.device("/dev/ttyG"));
    m
}

fn wp510_b_no_fallback_placeholder_rig() -> ManagerRig {
    const SID: &str = "gone-template:COM3";
    const BY_ID: &str = "/s/by-id/usb-G";
    let mut loaded = LoadedState::default();
    loaded.released.insert(SID.to_string(), released_row(BY_ID));
    let yaml = "profiles:\n  t:\n    platform: shell\n    prompt_regex: '(?m)^root@t:.*# '\n    timeout_s: 2.0\n";
    let m = ManagerRig::with(yaml, loaded, None);
    m.add_device_at(BY_ID, "/dev/ttyG");
    install_shell_replies(&m.device("/dev/ttyG"));
    m
}

fn wp510_b_permission_error() -> OpenError {
    OpenError::Other(PalError::plain(
        PalErrorKind::PermissionDenied,
        "Permission denied",
    ))
}

fn wp510_b_assert_placeholder_retained(m: &ManagerRig, sid: &str) {
    assert_eq!(m.mgr.released_placeholder(sid), Some(true));
    assert_eq!(m.state_of(sid).as_deref(), Some("RELEASED"));
    assert!(m.mgr.resolve(sid).is_some(), "拒絕交接後佔位須保留");
}

#[test]
fn wp510_b_handoff_reuses_preopened_handle() {
    let m = wp510_b_placeholder_rig(false, false);
    let sid = m.mgr.resolve("gone-template:COM3").expect("佔位 session");
    m.mgr.update_devices(&m.snapshot());

    let response = rpc(m.mgr.attach_released_placeholder(&sid, false));
    assert!(
        is_ok(&response),
        "device.attach 應接受在線裝置：{response:?}"
    );
    wait_until("t:COM3 正式 session", || {
        m.mgr.resolve("t:COM3").is_some()
    });
    assert_eq!(
        m.mgr.resolve("t:COM3").as_deref(),
        Some("t:COM3"),
        "成功 handoff 應以原 COM 建立真正 template session"
    );
    wait_until("COM3 正式 bridge", || {
        matches!(m.state_of("COM3").as_deref(), Some("ATTACHED" | "READY"))
    });
    let formal = m.session("COM3");
    assert_eq!(s_of(&formal, "session_id").as_deref(), Some("t:COM3"));
    assert_eq!(s_of(&formal, "profile").as_deref(), Some("t"));
    assert_eq!(s_of(&formal, "profile_source").as_deref(), Some("detected"));
    assert_eq!(s_of(&formal, "com").as_deref(), Some("COM3"));
    assert_eq!(
        m.serial.open_attempts("/dev/ttyG"),
        1,
        "偵測與正式 bridge 必須共用同一 handle"
    );
}

#[test]
fn wp510_b_handoff_skips_detached_slot_rebind() {
    let m = wp510_b_placeholder_rig(true, false);
    let sid = m.mgr.resolve("gone-template:COM3").expect("佔位 session");
    m.mgr.update_devices(&m.snapshot());

    let response = rpc(m.mgr.attach_released_placeholder(&sid, false));
    assert!(
        is_ok(&response),
        "device.attach 應接受在線裝置：{response:?}"
    );
    wait_until("t:COM3 正式 session", || {
        m.mgr.resolve("t:COM3").is_some()
    });
    assert_eq!(
        m.mgr.resolve("t:COM3").as_deref(),
        Some("t:COM3"),
        "dynamic-only handoff 應建立 COM3 正式 session"
    );
    wait_until("COM3 正式 bridge", || {
        matches!(m.state_of("COM3").as_deref(), Some("ATTACHED" | "READY"))
    });
    let slot = m.session("COM0");
    assert_eq!(
        s_of(&slot, "device_by_id").as_deref(),
        Some("/s/by-id/offline-slot")
    );
    assert_eq!(m.serial.open_attempts("/dev/ttyG"), 1);
}

#[test]
fn wp510_b_handoff_cleans_old_identity_and_restart_block() {
    const SID: &str = "gone-template:COM3";
    const BY_ID: &str = "/s/by-id/usb-G";
    let m = wp510_b_placeholder_rig(false, true);
    let sid = m.mgr.resolve(SID).expect("佔位 session");
    m.mgr.update_devices(&m.snapshot());

    let response = rpc(m.mgr.attach_released_placeholder(&sid, false));
    assert!(
        is_ok(&response),
        "device.attach 應接受在線裝置：{response:?}"
    );
    wait_until("t:COM3 正式 session", || {
        m.mgr.resolve("t:COM3").is_some()
    });
    assert_eq!(
        m.mgr.resolve("t:COM3").as_deref(),
        Some("t:COM3"),
        "handoff 應建立正式 session"
    );
    wait_until("COM3 正式 bridge", || {
        matches!(m.state_of("COM3").as_deref(), Some("ATTACHED" | "READY"))
    });

    let state = m.load_state_now();
    assert!(!state.released.contains_key(SID), "舊 RELEASED 列必須移除");
    assert!(
        !state.bindings.contains_key(SID),
        "舊 binding override 必須移除"
    );
    assert!(
        state
            .aliases
            .values()
            .all(|row| row.get("session_id").and_then(PyValue::as_str) != Some(SID)),
        "所有指向舊 sid 的 alias 都必須移除"
    );
    assert_eq!(
        state.profile_pins.get(BY_ID).map(String::as_str),
        Some("removed-pin-template")
    );
    assert_eq!(
        state.profile_detected.get(BY_ID).map(String::as_str),
        Some("removed-detected-template"),
        "handoff 必須保留 pin／detected 記錄"
    );
    assert!(m.mgr.resolve(SID).is_none(), "舊 sid 應在 handoff 後立即退休");
    let restarted = m.restart();
    assert!(restarted.mgr.resolve(SID).is_none(), "重啟不得復活舊佔位");
    assert!(!restarted.load_state_now().released.contains_key(SID));
    assert_eq!(restarted.mgr.released_placeholder(BY_ID), None);
}

#[test]
fn wp510_b_concurrent_placeholder_attach_returns_session_recovering() {
    const SID: &str = "gone-template:COM3";
    const REAL: &str = "/dev/ttyG";
    let m = wp510_b_placeholder_rig(false, false);
    let sid = m.mgr.resolve(SID).expect("佔位 session");
    m.mgr.update_devices(&m.snapshot());
    m.serial.block_open(REAL);

    let manager = m.mgr.clone();
    let attach_sid = sid.clone();
    let attach = std::thread::spawn(move || manager.attach_released_placeholder(&attach_sid, false));
    wait_until("第一次佔位 attach 停在 open_exclusive", || {
        m.serial.open_attempts(REAL) == 1
    });

    let second = rpc(m.mgr.attach_released_placeholder(&sid, false));
    m.serial.release_open(REAL);
    let first = rpc(attach.join().expect("第一次 attach thread"));

    assert_eq!(
        second.0.keys().map(String::as_str).collect::<Vec<_>>(),
        ["ok", "error_code", "drain_stalled", "drain_timeout_epoch", "session"],
        "佔位 attach 在途回應須有完整 recovering 欄位：{second:?}"
    );
    assert_eq!(code_of(&second).as_deref(), Some("SESSION_RECOVERING"));
    assert_eq!(second.get("ok"), Some(&PyValue::Bool(false)));
    assert_eq!(second.get("drain_stalled"), Some(&PyValue::Bool(false)));
    assert_eq!(second.get("drain_timeout_epoch"), Some(&PyValue::Null));
    assert!(is_ok(&first), "第一個未被取消的 attach 可正常交接：{first:?}");
}

#[test]
fn wp510_review_release_cancels_placeholder_attach_before_retirement() {
    const SID: &str = "gone-template:COM3";
    const REAL: &str = "/dev/ttyG";
    let m = wp510_b_placeholder_rig(false, false);
    let sid = m.mgr.resolve(SID).expect("佔位 session");
    let device = m.device(REAL);
    m.mgr.update_devices(&m.snapshot());
    m.serial.block_open(REAL);

    let (pause_tx, pause_rx) = crossbeam_channel::bounded::<()>(1);
    let (resume_tx, resume_rx) = crossbeam_channel::bounded::<()>(1);
    // 設計 §6 #60（H）：同步點由 ph-release-cancel（manager release 取得作業後暫停）改為 H-claim-pre-exec（佔位 actor 執行
    // release 的 `ClaimForStop` 之前暫停）；先後順序與耗時上界不變。
    m.mgr.set_claim_pre_exec_hook_for_test(
        &sid,
        Box::new(move || {
            let _ = pause_tx.send(());
            let _ = resume_rx.recv_timeout(Duration::from_secs(10));
        }),
    );

    let attach_manager = m.mgr.clone();
    let attach_sid = sid.clone();
    let (attach_tx, attach_rx) = crossbeam_channel::bounded(1);
    let attach = std::thread::spawn(move || {
        let _ = attach_tx.send(attach_manager.attach_released_placeholder(&attach_sid, false));
    });
    wait_until("佔位 attach 停在 open_exclusive", || {
        m.serial.open_attempts(REAL) == 1
    });

    let release_manager = m.mgr.clone();
    let release_sid = sid.clone();
    let (release_tx, release_rx) = crossbeam_channel::bounded(1);
    let release = std::thread::spawn(move || {
        let _ = release_tx.send(release_manager.route(
            &release_sid,
            SessionRequest::Release {
                source: "agent".to_string(),
                reason: PyValue::Null,
            },
            Duration::from_secs(10),
        ));
    });
    pause_rx
        .recv_timeout(Duration::from_secs(5))
        .expect("release 已取得佔位 attach op");
    m.serial.release_open(REAL);
    let attach_before_release_resumes = attach_rx.recv_timeout(Duration::from_millis(250)).ok();
    let attach_returned_early = attach_before_release_resumes.is_some();

    let _ = resume_tx.send(());
    let attach_result = attach_before_release_resumes
        .or_else(|| attach_rx.recv_timeout(Duration::from_secs(5)).ok())
        .expect("佔位 attach 應完成");
    let release_result = release_rx
        .recv_timeout(Duration::from_secs(5))
        .expect("release 應完成");
    attach.join().expect("attach thread 不應 panic");
    release.join().expect("release thread 不應 panic");

    assert!(
        !attach_returned_early,
        "release 尚未完成取消時 attach 不得先退休佔位：{attach_result:?}"
    );
    let attach_reply = rpc(attach_result);
    assert_eq!(
        code_of(&attach_reply).as_deref(),
        Some("SESSION_RECOVERING")
    );
    let release_reply = rpc(release_result);
    assert!(
        is_ok(&release_reply),
        "release 應完成佔位交接取消：{release_reply:?}"
    );
    assert_eq!(m.mgr.resolve(SID), Some(sid.clone()));
    assert_eq!(m.mgr.released_placeholder(&sid), Some(true));
    assert!(m
        .mgr
        .list_sessions()
        .iter()
        .all(|session| { s_of(session, "session_id").as_deref() != Some("others-template:COM0") }));
    assert!(device.take_tx().is_empty(), "取消後不得啟動動態偵測 TX");
}

#[test]
fn wp510_review_late_placeholder_attach_completion_clears_drain_stall() {
    const SID: &str = "gone-template:COM3";
    const REAL: &str = "/dev/ttyG";
    let m = wp510_b_placeholder_rig(false, false);
    let sid = m.mgr.resolve(SID).expect("佔位 session");
    m.mgr.update_devices(&m.snapshot());
    m.serial.block_open(REAL);

    let attach_manager = m.mgr.clone();
    let attach_sid = sid.clone();
    let (attach_tx, attach_rx) = crossbeam_channel::bounded(1);
    let attach = std::thread::spawn(move || {
        let _ = attach_tx.send(attach_manager.attach_released_placeholder(&attach_sid, false));
    });
    wait_until("佔位 attach 停在 open_exclusive", || m.serial.open_attempts(REAL) == 1);

    let release_manager = m.mgr.clone();
    let release_sid = sid.clone();
    let (release_tx, release_rx) = crossbeam_channel::bounded(1);
    let release = std::thread::spawn(move || {
        let _ = release_tx.send(release_manager.route(
            &release_sid,
            SessionRequest::Release {
                source: "agent".to_string(),
                reason: PyValue::Null,
            },
            Duration::from_secs(10),
        ));
    });
    let stalled = release_rx
        .recv_timeout(Duration::from_secs(5))
        .expect("release 排空期限內應回覆")
        .expect("release RPC 回應");
    assert_eq!(
        code_of(&stalled).as_deref(),
        Some("DRAIN_STALLED"),
        "{stalled:?}"
    );
    assert_eq!(stalled.get("drain_stalled"), Some(&PyValue::Bool(true)));
    assert!(matches!(
        stalled.get("drain_timeout_epoch"),
        Some(PyValue::Int(_))
    ));

    m.serial.release_open(REAL);
    let attach_reply = rpc(
        attach_rx
            .recv_timeout(Duration::from_secs(5))
            .expect("取消的佔位 attach 應在開埠返回後完成"),
    );
    attach.join().expect("attach thread 不應 panic");
    release.join().expect("release thread 不應 panic");
    assert_eq!(
        code_of(&attach_reply).as_deref(),
        Some("SESSION_RECOVERING")
    );

    let listed = m
        .mgr
        .list_sessions()
        .into_iter()
        .find(|session| s_of(session, "session_id").as_deref() == Some(SID))
        .expect("RELEASED 佔位仍在 session.list");
    assert_eq!(listed.get("drain_stalled"), Some(&PyValue::Bool(false)));
    assert_eq!(listed.get("drain_timeout_epoch"), Some(&PyValue::Null));

    let repeated = rpc(m.mgr.route(
        &sid,
        SessionRequest::Release {
            source: "agent".to_string(),
            reason: PyValue::Null,
        },
        RPC,
    ));
    assert_eq!(repeated.get("already_released"), Some(&PyValue::Bool(true)));
    let repeated_session = session_field(&repeated);
    assert_eq!(
        repeated_session.get("drain_stalled"),
        Some(&PyValue::Bool(false))
    );
    assert_eq!(
        repeated_session.get("drain_timeout_epoch"),
        Some(&PyValue::Null)
    );
}

/// wp510 第四次整包審查：`released_by`／`released_at` 為 null（reason 保留）的缺失 template 佔位。
fn wp510_review4_null_metadata_rig() -> ManagerRig {
    const SID: &str = "gone-template:COM3";
    const BY_ID: &str = "/s/by-id/usb-G";
    let mut loaded = LoadedState::default();
    loaded.released.insert(
        SID.to_string(),
        ReleasedRow {
            by_id: PyValue::Str(BY_ID.to_string()),
            released_by: PyValue::Null,
            released_at: PyValue::Null,
            reason: PyValue::Str("manual".to_string()),
        },
    );
    let m = ManagerRig::with(&manager_yaml(&[]), loaded, None);
    m.add_device_at(BY_ID, "/dev/ttyG");
    install_shell_replies(&m.device("/dev/ttyG"));
    m
}

/// 佔位 attach 停在 `open_exclusive` 時送出 `release(source, reason)`；release 取得在途作業後才放行開埠。
/// 回傳 `(attach 結果, release 結果)`。
fn wp510_review4_release_during_placeholder_open(
    m: &ManagerRig,
    sid: &str,
    real: &str,
    source: &str,
    reason: PyValue,
) -> (Result<PyDict, PyErr>, Result<PyDict, PyErr>) {
    m.serial.block_open(real);
    let (cancel_tx, cancel_rx) = crossbeam_channel::bounded::<()>(1);
    m.mgr.set_placeholder_release_cancel_hook_for_test(Box::new(move || {
        let _ = cancel_tx.try_send(());
    }));
    let attach_manager = m.mgr.clone();
    let attach_sid = sid.to_string();
    let attach = std::thread::spawn(move || {
        attach_manager.attach_released_placeholder(&attach_sid, false)
    });
    wait_until("佔位 attach 停在 open_exclusive", || {
        m.serial.open_attempts(real) == 1
    });
    let release_manager = m.mgr.clone();
    let release_sid = sid.to_string();
    let source = source.to_string();
    let release = std::thread::spawn(move || {
        release_manager.route(
            &release_sid,
            SessionRequest::Release { source, reason },
            Duration::from_secs(10),
        )
    });
    cancel_rx
        .recv_timeout(Duration::from_secs(5))
        .expect("release 應取得在途佔位 attach 作業");
    m.serial.release_open(real);
    let attach_result = attach.join().expect("attach thread 不應 panic");
    let release_result = release.join().expect("release thread 不應 panic");
    (attach_result, release_result)
}

/// wp510 第四次整包審查 Medium 1：null 來源與時間的佔位在開埠途中被 `release(source="cli")` 撤銷；handle
/// 安全關閉後，Python `_finish_attach_op` 以 `released_by or source`、`released_at or now_iso()` 補齊、reason 沿用
/// session 既有值並持久化，release 回 `already_released`。Python 對照：`origin/main:sw_core/session_manager.py:3117-3122`
/// （第一個 release 請求的 source／reason）、`:4341-4363`（收尾補齊與 `_persist_release_state`）、`:3206-3209`。
#[test]
fn wp510_review4_release_cancel_fills_null_release_metadata_and_persists() {
    const SID: &str = "gone-template:COM3";
    const REAL: &str = "/dev/ttyG";
    const WALL_US: i64 = 1_762_646_400_000_000;
    let m = wp510_review4_null_metadata_rig();
    let sid = m.mgr.resolve(SID).expect("佔位 session");
    m.mgr.update_devices(&m.snapshot());
    let before = m.session(SID);
    assert_eq!(before.get("released_by"), Some(&PyValue::Null), "前置：released_by 為 null");
    assert_eq!(before.get("released_at"), Some(&PyValue::Null), "前置：released_at 為 null");
    m.clock.set_wall_us(WALL_US);
    let writes_before = m.state_writes();

    let (attach, release) = wp510_review4_release_during_placeholder_open(
        &m,
        &sid,
        REAL,
        "cli",
        PyValue::from("ignored-reason"),
    );
    let attach = rpc(attach);
    let release = rpc(release);
    let expected_at = sw_proto::pycompat::iso_utc_now(WALL_US);

    assert_eq!(code_of(&attach).as_deref(), Some("SESSION_RECOVERING"));
    assert_eq!(release.get("ok"), Some(&PyValue::Bool(true)), "{release:?}");
    assert_eq!(
        release.get("already_released"),
        Some(&PyValue::Bool(true)),
        "{release:?}"
    );
    let session = session_field(&release);
    assert_eq!(s_of(&session, "state").as_deref(), Some("RELEASED"));
    assert_eq!(
        s_of(&session, "released_by").as_deref(),
        Some("cli"),
        "Python `released_by or source`"
    );
    assert_eq!(
        s_of(&session, "released_at").as_deref(),
        Some(expected_at.as_str()),
        "Python `released_at or now_iso()`"
    );
    assert_eq!(
        s_of(&session, "released_reason").as_deref(),
        Some("manual"),
        "已 RELEASED 的佔位沿用既有 reason，不取本次 release 的 reason"
    );
    assert_eq!(session.get("drain_stalled"), Some(&PyValue::Bool(false)));
    assert!(
        m.state_writes() > writes_before,
        "release 收尾必須寫入 state.json"
    );
    let row = m
        .load_state_now()
        .released
        .get(SID)
        .cloned()
        .expect("RELEASED 佔位列仍在 state.json");
    assert_eq!(row.released_by, PyValue::from("cli"));
    assert_eq!(row.released_at, PyValue::from(expected_at.as_str()));
    assert_eq!(row.reason, PyValue::from("manual"));
    assert_eq!(m.mgr.released_placeholder(&sid), Some(true));
    assert!(m.mgr.resolve("t:COM3").is_none(), "release 成功後不得交接 dynamic");
}

/// wp510 第四次整包審查 Medium 1（持久化失敗）：同一情境下 state.json 持續 ENOSPC。Python 收尾的
/// `_persist_release_state` 失敗只記錄錯誤（`origin/main:sw_core/session_manager.py:4354-4362`），release 隨後重試
/// （`:3206-3207`）仍失敗即拋出，RPC 不得回成功；佔位維持 RELEASED 阻擋，磁碟恢復後下一次 release 寫入補齊值。
#[test]
fn wp510_review4_release_cancel_persist_enospc_fails_release_then_retries() {
    const SID: &str = "gone-template:COM3";
    const REAL: &str = "/dev/ttyG";
    const WALL_US: i64 = 1_762_646_400_000_000;
    let m = wp510_review4_null_metadata_rig();
    let sid = m.mgr.resolve(SID).expect("佔位 session");
    m.mgr.update_devices(&m.snapshot());
    m.clock.set_wall_us(WALL_US);
    m.fs.fail_next_atomic_writes(PalError::io(28, "[Errno 28] No space left on device"), 2);

    let (attach, release) =
        wp510_review4_release_during_placeholder_open(&m, &sid, REAL, "cli", PyValue::Null);
    let attach = rpc(attach);
    let expected_at = sw_proto::pycompat::iso_utc_now(WALL_US);

    assert_eq!(code_of(&attach).as_deref(), Some("SESSION_RECOVERING"));
    let error = match release {
        Ok(response) => panic!("持續 ENOSPC 時 release 不得回成功：{response:?}"),
        Err(error) => error,
    };
    assert!(
        error.message.contains("No space left on device"),
        "release 必須沿 Python 例外路徑回報 ENOSPC：{}",
        error.message
    );
    let warnings = m.warnings();
    assert!(
        warnings.iter().any(|w| {
            w.contains("release 持久化失敗（session=gone-template:COM3）")
                && w.contains("No space left on device")
        }),
        "收尾持久化失敗必須留下 session 與錯誤告警：{warnings:?}"
    );
    assert_eq!(m.mgr.released_placeholder(&sid), Some(true));
    let listed = m.session(SID);
    assert_eq!(s_of(&listed, "state").as_deref(), Some("RELEASED"));
    assert_eq!(s_of(&listed, "released_by").as_deref(), Some("cli"));
    assert_eq!(
        m.load_state_now().released.get(SID).map(|row| row.released_by.clone()),
        Some(PyValue::Null),
        "落盤失敗時 state.json 仍是舊列"
    );

    let retry = rpc(wp510_release(&m.mgr, &sid));
    assert_eq!(retry.get("already_released"), Some(&PyValue::Bool(true)), "{retry:?}");
    let retried = session_field(&retry);
    assert_eq!(
        s_of(&retried, "released_by").as_deref(),
        Some("cli"),
        "重試沿用收尾補齊的來源，不改成重試請求的 source"
    );
    let row = m
        .load_state_now()
        .released
        .get(SID)
        .cloned()
        .expect("RELEASED 佔位列仍在 state.json");
    assert_eq!(row.released_by, PyValue::from("cli"));
    assert_eq!(row.released_at, PyValue::from(expected_at.as_str()));
    assert_eq!(row.reason, PyValue::from("manual"));
}

#[test]
fn wp510_lifecycle_shutdown_cancel_fail_close_keeps_placeholder_blocked() {
    const SID: &str = "gone-template:COM3";
    const BY_ID: &str = "/s/by-id/usb-G";
    const REAL: &str = "/dev/ttyG";
    let m = wp510_b_placeholder_rig(false, false);
    let sid = m.mgr.resolve(SID).expect("佔位 session");
    let device = m.device(REAL);
    m.mgr.update_devices(&m.snapshot());
    m.serial.block_open(REAL);

    let attach_manager = m.mgr.clone();
    let attach_sid = sid.clone();
    let (attach_tx, attach_rx) = crossbeam_channel::bounded(1);
    let attach = std::thread::spawn(move || {
        let _ = attach_tx.send(attach_manager.attach_released_placeholder(&attach_sid, false));
    });
    wait_until("佔位 attach 停在 open_exclusive", || m.serial.open_attempts(REAL) == 1);

    let shutdown = m.mgr.shutdown(Duration::from_millis(100));
    device.fail_close(PalError::io(5, "[Errno 5] Input/output error"));
    m.serial.release_open(REAL);
    let attach_reply = rpc(
        attach_rx
            .recv_timeout(Duration::from_secs(5))
            .expect("shutdown 取消的開埠應返回"),
    );
    attach.join().expect("attach worker 不應 panic");

    let custody = m.mgr.custody_for_test(BY_ID).expect("custody entry");
    assert!(matches!(custody.phase, CustodyPhaseSnapshot::CloseFailed { .. }), "CloseFailed");
    assert!(m.mgr.unconfirmed_close_for_test(BY_ID), "unconfirmed_close");
    let second_shutdown = m.mgr.shutdown(Duration::from_millis(100));
    let release_started = Instant::now();
    let release_reply = rpc(wp510_release(&m.mgr, &sid));
    let release_elapsed = release_started.elapsed();

    assert!(
        shutdown
            .iter()
            .any(|(target, drained)| target == &format!("attach:{BY_ID}") && !drained),
        "首次 shutdown 不得把阻塞開埠列為已排空：{shutdown:?}"
    );
    assert_eq!(code_of(&attach_reply).as_deref(), Some("SESSION_RECOVERING"));
    assert_eq!(device.close_count(), 1);
    assert!(device.is_open(), "fail_close 後 handle 仍須保持 open");
    assert!(
        second_shutdown
            .iter()
            .any(|(target, drained)| target == &format!("attach:{BY_ID}") && !drained),
        "後續 shutdown 不得把 close:false 作業列為已排空：{second_shutdown:?}"
    );
    assert_eq!(code_of(&release_reply).as_deref(), Some("DRAIN_STALLED"));
    assert!(release_elapsed >= Duration::from_millis(2500));
}

#[test]
fn wp510_lifecycle_device_path_change_cancel_fail_close_keeps_placeholder_blocked() {
    const SID: &str = "gone-template:COM3";
    const BY_ID: &str = "/s/by-id/usb-G";
    const REAL: &str = "/dev/ttyG";
    const CHANGED: &str = "/dev/ttyG-rebound";
    let m = wp510_b_placeholder_rig(false, false);
    let sid = m.mgr.resolve(SID).expect("佔位 session");
    let device = m.device(REAL);
    m.mgr.update_devices(&m.snapshot());
    m.serial.block_open(REAL);

    let attach_manager = m.mgr.clone();
    let attach_sid = sid.clone();
    let (attach_tx, attach_rx) = crossbeam_channel::bounded(1);
    let attach = std::thread::spawn(move || {
        let _ = attach_tx.send(attach_manager.attach_released_placeholder(&attach_sid, false));
    });
    wait_until("佔位 attach 停在 open_exclusive", || m.serial.open_attempts(REAL) == 1);

    m.serial.add_device(CHANGED);
    m.mgr.update_devices(&[(
        BY_ID.to_string(),
        DeviceInfo {
            by_id: BY_ID.to_string(),
            real_path: CHANGED.to_string(),
        },
    )]);
    device.fail_close(PalError::io(5, "[Errno 5] Input/output error"));
    m.serial.release_open(REAL);
    let attach_reply = rpc(
        attach_rx
            .recv_timeout(Duration::from_secs(5))
            .expect("裝置改指取消的開埠應返回"),
    );
    attach.join().expect("attach worker 不應 panic");

    let custody = m.mgr.custody_for_test(BY_ID).expect("custody entry");
    assert!(matches!(custody.phase, CustodyPhaseSnapshot::CloseFailed { .. }), "CloseFailed");
    assert!(m.mgr.unconfirmed_close_for_test(BY_ID), "unconfirmed_close");
    let release_started = Instant::now();
    let release_reply = rpc(wp510_release(&m.mgr, &sid));
    let release_elapsed = release_started.elapsed();

    assert_eq!(code_of(&attach_reply).as_deref(), Some("DEVICE_NOT_PRESENT"));
    assert_eq!(device.close_count(), 1);
    assert!(device.is_open(), "fail_close 後 handle 仍須保持 open");
    assert_eq!(code_of(&release_reply).as_deref(), Some("DRAIN_STALLED"));
    assert!(release_elapsed >= Duration::from_millis(2500));
}

/// 等待 daemon stderr 告警出現（最多 `RIG_RPC_TIMEOUT`）；回傳當下全部告警供斷言訊息使用。
fn wp510_review4_wait_warning(m: &ManagerRig, needle: &str) -> Vec<String> {
    let deadline = Instant::now() + Duration::from_secs(5);
    loop {
        let warnings = m.warnings();
        if warnings.iter().any(|w| w.contains(needle)) || Instant::now() >= deadline {
            return warnings;
        }
        std::thread::sleep(Duration::from_millis(2));
    }
}

/// Python `_finish_attach_op` 的關閉失敗告警（`origin/main:sw_core/session_manager.py:4326-4331`）。
const WP510_REVIEW4_CLOSE_WARNING: &str =
    "預開 UART handle 關閉失敗（by_id=/s/by-id/usb-G）：[Errno 5] Input/output error";

/// wp510 第四次整包審查 Medium 2：佔位已退休、dynamic PROBE 遭裝置改指取消，預開 handle 的關閉回條為
/// `closed:false`。舊 session 已消失、新 session 尚未建立；Python `_finish_attach_op`
/// （`origin/main:sw_core/session_manager.py:4322-4332`）先以 by_id 與關閉例外告警再保留阻擋，操作者才能由
/// daemon stderr 找到永久阻擋的原因。
#[test]
fn wp510_review4_failed_preopened_close_warns_with_by_id_and_error() {
    const SID: &str = "gone-template:COM3";
    const BY_ID: &str = "/s/by-id/usb-G";
    const REAL: &str = "/dev/ttyG";
    const CHANGED: &str = "/dev/ttyG-rebound";
    let m = wp510_b_placeholder_rig(false, false);
    let sid = m.mgr.resolve(SID).expect("佔位 session");
    let device = m.device(REAL);
    m.mgr.update_devices(&m.snapshot());
    m.serial.add_device(CHANGED);
    let changed = vec![(
        BY_ID.to_string(),
        DeviceInfo {
            by_id: BY_ID.to_string(),
            real_path: CHANGED.to_string(),
        },
    )];

    let weak = std::sync::Arc::downgrade(&m.mgr);
    let hook_device = device.clone();
    let (hook_tx, hook_rx) = crossbeam_channel::bounded(4);
    m.mgr.set_probe_pre_submit_hook_for_test(Box::new(move || {
        if let Some(manager) = weak.upgrade() {
            // 交接後的 PROBE 已越過 `still_valid()`：裝置改指取消作業，接著令關閉失敗。
            manager.update_devices(&changed);
            hook_device.fail_close(PalError::io(5, "[Errno 5] Input/output error"));
            let _ = hook_tx.send(());
        }
    }));

    let response = rpc(m.mgr.attach_released_placeholder(&sid, false));
    assert!(is_ok(&response), "device.attach 應接受在線佔位：{response:?}");
    hook_rx
        .recv_timeout(Duration::from_secs(5))
        .expect("交接後的 PROBE 應在提交前觸發裝置改指");
    wait_until("被取消的預開 handle 嘗試關閉", || device.close_count() == 1);
    let warnings = wp510_review4_wait_warning(&m, WP510_REVIEW4_CLOSE_WARNING);

    assert!(
        warnings.iter().any(|w| w == WP510_REVIEW4_CLOSE_WARNING),
        "關閉失敗必須以 by_id 與 CloseReceipt.error 告警：{warnings:?}"
    );
    assert_eq!(
        warnings
            .iter()
            .filter(|w| w.contains("預開 UART handle 關閉失敗"))
            .count(),
        1,
        "同一次失敗的關閉只告警一次：{warnings:?}"
    );
    assert!(device.is_open(), "closed:false 表示 handle 仍開啟");
    assert!(
        m.mgr.has_inflight_attach_for_test(BY_ID),
        "關閉失敗後仍須保留阻擋"
    );
    assert!(m.mgr.resolve(SID).is_none(), "舊佔位已於交接時退休");
    assert!(m.mgr.resolve("t:COM3").is_none(), "被取消的交接不得建立正式 session");
}

/// wp510 第四次整包審查 Medium 2（退休前）：佔位開埠途中裝置改指，`attach_released_placeholder` 收尾關閉晚到
/// handle 失敗。Python `attach_device` 的 `invalid_reason == "device"` 分支同樣經 `_finish_attach_op` 告警
/// （`origin/main:sw_core/session_manager.py:3614-3616`、`:4322-4332`）。
#[test]
fn wp510_review4_failed_late_handle_close_before_retirement_warns() {
    const SID: &str = "gone-template:COM3";
    const BY_ID: &str = "/s/by-id/usb-G";
    const REAL: &str = "/dev/ttyG";
    const CHANGED: &str = "/dev/ttyG-rebound";
    let m = wp510_b_placeholder_rig(false, false);
    let sid = m.mgr.resolve(SID).expect("佔位 session");
    let device = m.device(REAL);
    m.mgr.update_devices(&m.snapshot());
    m.serial.block_open(REAL);

    let attach_manager = m.mgr.clone();
    let attach_sid = sid.clone();
    let attach = std::thread::spawn(move || attach_manager.attach_released_placeholder(&attach_sid, false));
    wait_until("佔位 attach 停在 open_exclusive", || m.serial.open_attempts(REAL) == 1);
    m.serial.add_device(CHANGED);
    m.mgr.update_devices(&[(
        BY_ID.to_string(),
        DeviceInfo {
            by_id: BY_ID.to_string(),
            real_path: CHANGED.to_string(),
        },
    )]);
    device.fail_close(PalError::io(5, "[Errno 5] Input/output error"));
    m.serial.release_open(REAL);
    let attach_reply = rpc(attach.join().expect("attach thread 不應 panic"));
    let warnings = m.warnings();

    assert_eq!(code_of(&attach_reply).as_deref(), Some("DEVICE_NOT_PRESENT"));
    assert!(
        warnings.iter().any(|w| w == WP510_REVIEW4_CLOSE_WARNING),
        "晚到 handle 關閉失敗必須以 by_id 與錯誤告警：{warnings:?}"
    );
    assert!(m.mgr.has_inflight_attach_for_test(BY_ID), "關閉失敗後仍須保留阻擋");
    assert_eq!(m.mgr.released_placeholder(&sid), Some(true));
}

/// wp510 第四次整包審查盤點二：交接完成後預開 handle 已交給正式 session、正式 bridge 尚未接手時 shutdown，
/// `open_bridge` 關閉它失敗。Python 的預開 handle 在正式 bridge 接手前仍屬 `_preopened_ports` 作業，停機丟棄同樣
/// 經 `_finish_attach_op` 告警（`origin/main:sw_core/session_manager.py:1181-1187` 取消、`:1218-1226` 收尾、`:4322-4332` 告警）。
#[test]
fn wp510_review4_failed_session_preopened_close_on_shutdown_warns() {
    const SID: &str = "gone-template:COM3";
    const REAL: &str = "/dev/ttyG";
    let m = wp510_b_placeholder_rig(false, false);
    let sid = m.mgr.resolve(SID).expect("佔位 session");
    let device = m.device(REAL);
    m.mgr.update_devices(&m.snapshot());

    let weak = std::sync::Arc::downgrade(&m.mgr);
    let hook_device = device.clone();
    let (hook_tx, hook_rx) = crossbeam_channel::bounded(1);
    m.mgr.set_open_bridge_pre_take_hook_for_test(Box::new(move || {
        if let Some(manager) = weak.upgrade() {
            hook_device.fail_close(PalError::io(5, "[Errno 5] Input/output error"));
            let drained = manager.shutdown(Duration::from_millis(50));
            let _ = hook_tx.send(drained);
        }
    }));

    let response = rpc(m.mgr.attach_released_placeholder(&sid, false));
    assert!(is_ok(&response), "device.attach 應接受在線佔位：{response:?}");
    hook_rx
        .recv_timeout(Duration::from_secs(5))
        .expect("正式 session 的 open_bridge 應觸發停機");
    wait_until("預開 handle 嘗試關閉", || device.close_count() == 1);
    let warnings = wp510_review4_wait_warning(&m, WP510_REVIEW4_CLOSE_WARNING);

    assert!(
        warnings.iter().any(|w| w == WP510_REVIEW4_CLOSE_WARNING),
        "session 預開 handle 關閉失敗必須以 by_id 與錯誤告警：{warnings:?}"
    );
    assert_eq!(device.open_count(), 1, "停機後不得重新開埠");
    assert!(device.take_tx().iter().all(|b| *b == b'\n'), "停機後只允許交接 PROBE 的 \\n");
}

/// wp510 第四次整包審查 Medium 3：佔位在 T0 啟動建立、T1 才 `device.attach`。Python 啟動還原以 setter 寫入 T0
/// （`origin/main:sw_core/session_manager.py:1114-1115`、`:617-620`）；交接成功分支於 `:3599` 設 ATTACHING、
/// `:3602` 才產生回應，`last_state_change_at` 為 T1，不可沿用退休前快照的 T0。
#[test]
fn wp510_review4_handoff_response_stamps_last_state_change_at() {
    use sw_pal::traits::Clock;
    const SID: &str = "gone-template:COM3";
    let m = wp510_b_placeholder_rig(false, false);
    let sid = m.mgr.resolve(SID).expect("佔位 session");
    let t0 = sw_proto::pycompat::iso_utc_now(m.clock.wall_unix_us());
    assert_eq!(
        s_of(&m.session(SID), "last_state_change_at").as_deref(),
        Some(t0.as_str()),
        "啟動還原 RELEASED 必須寫入狀態變更時間"
    );
    m.mgr.update_devices(&m.snapshot());
    m.clock.set_wall_us(m.clock.wall_unix_us() + 5_000_000);
    let t1 = sw_proto::pycompat::iso_utc_now(m.clock.wall_unix_us());

    let response = rpc(m.mgr.attach_released_placeholder(&sid, false));
    let session = session_field(&response);

    assert!(is_ok(&response), "{response:?}");
    assert_eq!(s_of(&session, "state").as_deref(), Some("ATTACHING"));
    assert_eq!(
        s_of(&session, "last_state_change_at").as_deref(),
        Some(t1.as_str()),
        "RELEASED→ATTACHING 的交接回應必須帶 T1"
    );
}

/// wp510 第四次整包審查 Medium 3（一般 OSError）：同一情境下佔位開埠一般 OSError。Python `:3535` 設 DETACHED
/// （setter 寫入時間）、`:3536-3537` 設 last_error 與 detached_at，`:3550` 才產生回應。
#[test]
fn wp510_review4_handoff_oserror_response_stamps_last_state_change_at() {
    use sw_pal::traits::Clock;
    const SID: &str = "gone-template:COM3";
    const REAL: &str = "/dev/ttyG";
    let m = wp510_b_placeholder_rig(false, false);
    let sid = m.mgr.resolve(SID).expect("佔位 session");
    m.serial.fail_next_open(REAL, wp510_b_permission_error());
    m.mgr.update_devices(&m.snapshot());
    m.clock.set_wall_us(m.clock.wall_unix_us() + 5_000_000);
    let t1 = sw_proto::pycompat::iso_utc_now(m.clock.wall_unix_us());

    let response = rpc(m.mgr.attach_released_placeholder(&sid, false));
    let session = session_field(&response);

    assert!(is_ok(&response), "{response:?}");
    assert_eq!(s_of(&session, "state").as_deref(), Some("DETACHED"));
    assert_eq!(
        s_of(&session, "last_error").as_deref(),
        Some("ATTACH_FAILED:PermissionError")
    );
    assert_eq!(s_of(&session, "detached_at").as_deref(), Some(t1.as_str()));
    assert_eq!(
        s_of(&session, "last_state_change_at").as_deref(),
        Some(t1.as_str()),
        "RELEASED→DETACHED 的交接回應必須帶 T1"
    );
}

/// PROBE TX 的 WAL 列數（`com=PROBE`、`dir=TX`）。
fn wp510_probe_tx_rows(m: &ManagerRig) -> usize {
    m.wal_rows()
        .into_iter()
        .filter(|r| {
            s_of(r, "com").as_deref() == Some("PROBE") && s_of(r, "dir").as_deref() == Some("TX")
        })
        .count()
}

/// wp510 第三次整包審查 High：佔位交接後的 PROBE 已通過 `still_valid()`、尚未提交時執行 shutdown，
/// 取消權必須落在同一 attach 作業的 PROBE Fence 上，提交點重驗即拒絕、零 TX。
/// Python 對照：`origin/main:sw_core/session_manager.py:4367-4372` 登錄 probe bridge、`:1186-1193` shutdown
/// 對它 `begin_drain()`；`sw_core/uart_io.py:908-918` 凍結並取消 TX、`:623-627` 於提交點以 `draining` 拒絕。
#[test]
fn wp510_tx_shutdown_after_still_valid_before_placeholder_probe_submit_sends_zero_tx() {
    const SID: &str = "gone-template:COM3";
    const BY_ID: &str = "/s/by-id/usb-G";
    const REAL: &str = "/dev/ttyG";
    let m = wp510_b_placeholder_rig(false, false);
    let sid = m.mgr.resolve(SID).expect("佔位 session");
    let device = m.device(REAL);
    m.mgr.update_devices(&m.snapshot());

    let weak = std::sync::Arc::downgrade(&m.mgr);
    let (hook_tx, hook_rx) = crossbeam_channel::bounded(4);
    m.mgr.set_probe_pre_submit_hook_for_test(Box::new(move || {
        if let Some(manager) = weak.upgrade() {
            // 在 PROBE 已越過 `still_valid()`、TxWriter 提交前停機；不等待本 worker 收尾。
            let drained = manager.shutdown(Duration::from_millis(50));
            let _ = hook_tx.send(drained);
        }
    }));

    let response = rpc(m.mgr.attach_released_placeholder(&sid, false));
    assert!(is_ok(&response), "device.attach 應接受在線佔位：{response:?}");
    let drained = hook_rx
        .recv_timeout(Duration::from_secs(5))
        .expect("交接後的 PROBE 應在提交前觸發 shutdown");
    assert!(
        m.mgr.wait_attach_idle_for_test(BY_ID, Duration::from_secs(5)),
        "shutdown 取消的交接 worker 應收尾"
    );

    assert!(
        drained
            .iter()
            .any(|(target, ok)| target == &format!("attach:{BY_ID}") && !ok),
        "PROBE 尚在 worker 內時 shutdown 不得把交接作業列為已排空：{drained:?}"
    );
    assert_eq!(device.os_submits(), 0, "shutdown 後 PROBE 不得提交任何 bytes 給 OS");
    assert!(device.take_tx().is_empty(), "shutdown 後裝置不得收到 PROBE 的 \\n");
    assert_eq!(wp510_probe_tx_rows(&m), 0, "被撤銷的 PROBE TX 不得寫 WAL");
    assert_eq!(device.close_count(), 1, "未交接的預開 handle 應關閉一次");
    assert!(!device.is_open(), "shutdown 取消後不得保留預開 handle");
    assert!(
        m.mgr.resolve("t:COM3").is_none(),
        "shutdown 取消後不得建立正式 session"
    );
}

/// 同一缺陷的系統性盤點：一般熱插 dynamic attach 的 PROBE 亦須受同一作業取消。PROBE 已越過
/// `still_valid()`、提交前裝置改指（`update_devices` 取消在途作業，Python `:4031-4043` 對 probe bridge
/// `begin_drain()`），提交點必須拒絕、零 TX。
#[test]
fn wp510_tx_device_path_change_before_hotplug_probe_submit_sends_zero_tx() {
    const BY_ID: &str = "/s/by-id/usb-F";
    let m = ManagerRig::with(&manager_yaml(&[]), LoadedState::default(), None);
    m.devices(&[BY_ID]);
    let real = m.real_of(BY_ID);
    m.reply_probe(&real, b"root@prpl:/# ");
    let device = m.device(&real);
    let changed = vec![(
        BY_ID.to_string(),
        DeviceInfo {
            by_id: BY_ID.to_string(),
            real_path: format!("{real}-rebound"),
        },
    )];

    let weak = std::sync::Arc::downgrade(&m.mgr);
    let (hook_tx, hook_rx) = crossbeam_channel::bounded(4);
    m.mgr.set_probe_pre_submit_hook_for_test(Box::new(move || {
        if let Some(manager) = weak.upgrade() {
            manager.update_devices(&changed);
            let _ = hook_tx.send(());
        }
    }));

    m.mgr.update_devices(&m.snapshot());
    hook_rx
        .recv_timeout(Duration::from_secs(5))
        .expect("熱插的 PROBE 應在提交前觸發裝置改指");
    assert!(
        m.mgr.wait_attach_idle_for_test(BY_ID, Duration::from_secs(5)),
        "裝置改指取消的 attach worker 應收尾"
    );

    assert_eq!(device.os_submits(), 0, "裝置改指後 PROBE 不得提交任何 bytes 給 OS");
    assert!(device.take_tx().is_empty(), "裝置改指後舊路徑不得收到 PROBE 的 \\n");
    assert_eq!(wp510_probe_tx_rows(&m), 0, "被撤銷的 PROBE TX 不得寫 WAL");
    assert_eq!(device.close_count(), 1, "取消後的 PROBE handle 應關閉一次");
    assert!(!device.is_open(), "取消後不得保留舊路徑 handle");
}

/// wp510 第三次整包審查 Medium：`alias.set` 已更新 actor、尚未持久化時佔位退休，不得把 alias 寫回已退休 sid，
/// 也不得回 `ok:true`。Python 對照：`origin/main:sw_core/session_manager.py:2612-2620` 的檢查、更新與
/// `_save_state()` 全在 manager lock 內；`:3640-3652` 退休清理持同一把鎖，故後到的 `alias.set` 只會看到
/// session 已不存在並回 `SESSION_NOT_FOUND`。
#[test]
fn wp510_identity_alias_set_racing_placeholder_retirement_keeps_old_sid_retired() {
    const SID: &str = "gone-template:COM3";
    const LATE_ALIAS: &str = "late-alias";
    let m = wp510_b_placeholder_rig(false, false);
    let sid = m.mgr.resolve(SID).expect("佔位 session");
    m.mgr.update_devices(&m.snapshot());

    let weak = std::sync::Arc::downgrade(&m.mgr);
    let hook_sid = sid.clone();
    let fired = std::sync::Arc::new(std::sync::atomic::AtomicBool::new(false));
    let hook_fired = fired.clone();
    let (hook_tx, hook_rx) = crossbeam_channel::bounded(1);
    m.mgr.set_alias_pre_commit_hook_for_test(Box::new(move || {
        if hook_fired.swap(true, std::sync::atomic::Ordering::SeqCst) {
            return;
        }
        if let Some(manager) = weak.upgrade() {
            let _ = hook_tx.send(manager.attach_released_placeholder(&hook_sid, false));
        }
    }));

    let reply = rpc(m.mgr.alias_set(&sid, LATE_ALIAS));
    let attach = rpc(
        hook_rx
            .recv_timeout(Duration::from_secs(5))
            .expect("佔位退休應發生在 alias 持久化前"),
    );
    assert!(is_ok(&attach), "device.attach 應完成佔位退休：{attach:?}");
    wait_until("t:COM3 正式 session", || m.mgr.resolve("t:COM3").is_some());

    assert_eq!(
        sw_proto::pyjson::dumps_dict(&reply, &sw_proto::pyjson::RPC_WIRE),
        format!(r#"{{"ok":false,"error_code":"SESSION_NOT_FOUND","session_id":"{SID}"}}"#),
        "退休後才提交的 alias.set 應回 Python 的 SESSION_NOT_FOUND（逐位元組含鍵序）"
    );
    let listed = m.mgr.alias_list();
    let rows: Vec<PyDict> = match listed.get("aliases") {
        Some(PyValue::List(rows)) => rows
            .iter()
            .filter_map(|row| match row {
                PyValue::Dict(row) => Some(row.clone()),
                _ => None,
            })
            .collect(),
        other => panic!("alias.list 應回 aliases 清單：{other:?}"),
    };
    assert!(
        rows.iter()
            .all(|row| s_of(row, "session_id").as_deref() != Some(SID)),
        "alias.list 不得留下指向已退休 sid 的 alias：{rows:?}"
    );
    assert!(
        rows.iter()
            .all(|row| s_of(row, "alias").as_deref() != Some(LATE_ALIAS)),
        "退休後到的 alias 不得出現在 alias.list：{rows:?}"
    );
    let persisted = m.load_state_now();
    assert!(
        !persisted.aliases.contains_key(LATE_ALIAS),
        "state.json 不得寫入退休後到的 alias：{:?}",
        persisted.aliases
    );
    assert!(
        persisted
            .aliases
            .values()
            .all(|row| row.get("session_id").and_then(PyValue::as_str) != Some(SID)),
        "state.json 不得保留指向已退休 sid 的 alias：{:?}",
        persisted.aliases
    );
}

#[test]
fn wp510_lifecycle_repeated_release_uses_expired_attach_deadline() {
    const SID: &str = "gone-template:COM3";
    const REAL: &str = "/dev/ttyG";
    let m = wp510_b_placeholder_rig(false, false);
    let sid = m.mgr.resolve(SID).expect("佔位 session");
    m.mgr.update_devices(&m.snapshot());
    m.serial.block_open(REAL);

    let attach_manager = m.mgr.clone();
    let attach_sid = sid.clone();
    let (attach_tx, attach_rx) = crossbeam_channel::bounded(1);
    let attach = std::thread::spawn(move || {
        let _ = attach_tx.send(attach_manager.attach_released_placeholder(&attach_sid, false));
    });
    wait_until("佔位 attach 停在 open_exclusive", || m.serial.open_attempts(REAL) == 1);

    let first = rpc(wp510_release(&m.mgr, &sid));
    assert_eq!(code_of(&first).as_deref(), Some("DRAIN_STALLED"));
    let repeated_started = Instant::now();
    let repeated = rpc(wp510_release(&m.mgr, &sid));
    let repeated_elapsed = repeated_started.elapsed();

    m.serial.release_open(REAL);
    let _ = attach_rx
        .recv_timeout(Duration::from_secs(5))
        .expect("取消後的佔位 attach 應完成");
    attach.join().expect("attach worker 不應 panic");

    assert_eq!(code_of(&repeated).as_deref(), Some("DRAIN_STALLED"));
    assert!(
        repeated_elapsed < Duration::from_millis(100),
        "已過期的 release deadline 必須立即結束等待，實際等待 {repeated_elapsed:?}"
    );
}

#[test]
fn wp510_lifecycle_concurrent_releases_share_first_attach_deadline() {
    const SID: &str = "gone-template:COM3";
    const REAL: &str = "/dev/ttyG";
    let m = wp510_b_placeholder_rig(false, false);
    let sid = m.mgr.resolve(SID).expect("佔位 session");
    m.mgr.update_devices(&m.snapshot());
    m.serial.block_open(REAL);

    let (release_started_tx, release_started_rx) = crossbeam_channel::bounded(1);
    // 設計 §6 #38（H）：同步點由 ph-release-cancel（只通知）改為 H-release-wait-created（首次 ReleaseWait 建立時通知、不暫停）；
    // 先後順序與耗時上界不變。
    m.mgr.set_release_wait_created_hook_for_test(
        &sid,
        Box::new(move || {
            let _ = release_started_tx.try_send(());
        }),
    );

    let attach_manager = m.mgr.clone();
    let attach_sid = sid.clone();
    let (attach_tx, attach_rx) = crossbeam_channel::bounded(1);
    let attach = std::thread::spawn(move || {
        let _ = attach_tx.send(attach_manager.attach_released_placeholder(&attach_sid, false));
    });
    wait_until("佔位 attach 停在 open_exclusive", || m.serial.open_attempts(REAL) == 1);

    let first_manager = m.mgr.clone();
    let first_sid = sid.clone();
    let (first_tx, first_rx) = crossbeam_channel::bounded(1);
    let first = std::thread::spawn(move || {
        let started = Instant::now();
        let response = wp510_release(&first_manager, &first_sid);
        let _ = first_tx.send((response, started.elapsed()));
    });
    release_started_rx
        .recv_timeout(Duration::from_secs(5))
        .expect("第一個 release 已找到並取消 attach op");

    std::thread::sleep(Duration::from_millis(2200));
    let second_manager = m.mgr.clone();
    let second_sid = sid.clone();
    let (second_tx, second_rx) = crossbeam_channel::bounded(1);
    let second = std::thread::spawn(move || {
        let started = Instant::now();
        let response = wp510_release(&second_manager, &second_sid);
        let _ = second_tx.send((response, started.elapsed()));
    });

    let first_result = first_rx
        .recv_timeout(Duration::from_secs(5))
        .expect("第一個 release 應於其期限結束");
    let second_result = second_rx
        .recv_timeout(Duration::from_secs(5))
        .expect("並行 release 應共用第一個期限");

    m.serial.release_open(REAL);
    let _ = attach_rx
        .recv_timeout(Duration::from_secs(5))
        .expect("取消後的佔位 attach 應完成");
    attach.join().expect("attach worker 不應 panic");
    first.join().expect("第一個 release thread 不應 panic");
    second.join().expect("第二個 release thread 不應 panic");

    assert_eq!(code_of(&rpc(first_result.0)).as_deref(), Some("DRAIN_STALLED"));
    assert_eq!(code_of(&rpc(second_result.0)).as_deref(), Some("DRAIN_STALLED"));
    assert!(
        second_result.1 < Duration::from_millis(1500),
        "並行 release 重設期限，多等了 {:?}",
        second_result.1
    );
}

#[test]
fn wp510_review_open_failure_after_release_clears_drain_stall() {
    const SID: &str = "gone-template:COM3";
    const REAL: &str = "/dev/ttyG";
    let failures = [
        (
            "Held",
            OpenError::Held(HolderReport {
                pids: vec![771],
                holder_unknown: false,
                status: HolderScanStatus::Ok,
            }),
            "DEVICE_STILL_HELD",
        ),
        (
            "NotFound",
            OpenError::NotFound(PalError::plain(PalErrorKind::NotFound, "No such device")),
            "DEVICE_NOT_PRESENT",
        ),
        (
            "Other",
            OpenError::Other(PalError::plain(
                PalErrorKind::PermissionDenied,
                "Permission denied",
            )),
            "SESSION_RECOVERING",
        ),
    ];

    for (failure_name, failure, expected_attach_code) in failures {
        let m = wp510_b_placeholder_rig(false, false);
        let sid = m.mgr.resolve(SID).expect("佔位 session");
        m.mgr.update_devices(&m.snapshot());
        m.serial.block_open(REAL);
        m.serial.fail_next_open(REAL, failure);

        let attach_manager = m.mgr.clone();
        let attach_sid = sid.clone();
        let (attach_tx, attach_rx) = crossbeam_channel::bounded(1);
        let attach = std::thread::spawn(move || {
            let _ = attach_tx.send(attach_manager.attach_released_placeholder(&attach_sid, false));
        });
        wait_until("佔位 attach 停在 open_exclusive", || {
            m.serial.open_attempts(REAL) == 1
        });

        let release_manager = m.mgr.clone();
        let release_sid = sid.clone();
        let (release_tx, release_rx) = crossbeam_channel::bounded(1);
        let release = std::thread::spawn(move || {
            let _ = release_tx.send(release_manager.route(
                &release_sid,
                SessionRequest::Release {
                    source: "agent".to_string(),
                    reason: PyValue::Null,
                },
                Duration::from_secs(10),
            ));
        });
        let stalled = release_rx
            .recv_timeout(Duration::from_secs(5))
            .expect("release 排空期限內應回覆")
            .expect("release RPC 回應");
        assert_eq!(
            code_of(&stalled).as_deref(),
            Some("DRAIN_STALLED"),
            "{failure_name}: {stalled:?}"
        );
        assert_eq!(
            stalled.get("drain_stalled"),
            Some(&PyValue::Bool(true)),
            "{failure_name}: timeout 必須記錄 drain stall"
        );
        assert!(matches!(
            stalled.get("drain_timeout_epoch"),
            Some(PyValue::Int(_))
        ));

        m.serial.release_open(REAL);
        let attach_reply = rpc(
            attach_rx
                .recv_timeout(Duration::from_secs(5))
                .expect("取消的佔位 attach 應在開埠錯誤返回後完成"),
        );
        attach.join().expect("attach thread 不應 panic");
        release.join().expect("release thread 不應 panic");
        assert_eq!(
            code_of(&attach_reply).as_deref(),
            Some(expected_attach_code),
            "{failure_name}: {attach_reply:?}"
        );

        let listed = m
            .mgr
            .list_sessions()
            .into_iter()
            .find(|session| s_of(session, "session_id").as_deref() == Some(SID))
            .expect("RELEASED 佔位仍在 session.list");
        assert_eq!(
            listed.get("drain_stalled"),
            Some(&PyValue::Bool(false)),
            "{failure_name}: 沒有取得 handle 的作業已結束，應清除 stall"
        );
        assert_eq!(
            listed.get("drain_timeout_epoch"),
            Some(&PyValue::Null),
            "{failure_name}: 已完成排空後應清除 timeout epoch"
        );

        let repeated_release = rpc(m.mgr.route(
            &sid,
            SessionRequest::Release {
                source: "agent".to_string(),
                reason: PyValue::Null,
            },
            RPC,
        ));
        assert!(is_ok(&repeated_release), "{failure_name}: {repeated_release:?}");
        assert_eq!(
            repeated_release.get("already_released"),
            Some(&PyValue::Bool(true)),
            "{failure_name}: 排空完成後重複 release 應成功"
        );
        assert_eq!(m.serial.open_attempts(REAL), 1, "{failure_name}: release 不開埠");

        let retry = rpc(m.mgr.attach_released_placeholder(&sid, false));
        assert!(is_ok(&retry), "{failure_name}: 排空完成後應可重新 attach：{retry:?}");
        wait_until("重試 attach 成功交接預開 handle", || {
            m.serial.open_attempts(REAL) == 2 && m.mgr.resolve(SID).is_none()
        });
    }
}

/// 退休落盤 ENOSPC：Python `attach_device` 已在 manager lock 內退休佔位並把已開的 UART 作業留在 `_preopened_ports`
/// （`origin/main:sw_core/session_manager.py:3590-3607`、`:3640-3653`），`_save_state()`（`:3633`）例外穿出、不回滾記憶體、
/// 不 `_spawn_attach`（`:3634-3637`）：佔位不再存在（磁碟因寫入失敗仍是舊內容），預開 handle 不關閉、不偵測。
/// 函式名沿用凍結鎖節點名（交接確實停止；「keeps_placeholder」指磁碟上的舊列）。
#[test]
fn wp510_b_retirement_persist_enospc_keeps_placeholder_and_stops_handoff() {
    const SID: &str = "gone-template:COM3";
    const REAL: &str = "/dev/ttyG";
    let m = wp510_b_placeholder_rig(false, false);
    let sid = m.mgr.resolve(SID).expect("佔位 session");
    let device = m.device(REAL);
    m.mgr.update_devices(&m.snapshot());
    m.fs.fail_next_atomic_write(PalError::io(
        28,
        "[Errno 28] No space left on device",
    ));

    let response = m.mgr.attach_released_placeholder(&sid, false);
    std::thread::sleep(Duration::from_millis(100));

    assert!(response.is_err(), "退休落盤 ENOSPC 必須向上回錯：{response:?}");
    assert_eq!(m.mgr.resolve(SID), None, "落盤失敗不回滾：佔位已退休");
    assert_eq!(m.mgr.released_placeholder(SID), None);
    assert!(m.load_state_now().released.contains_key(SID), "寫入失敗，磁碟仍是舊的 RELEASED 列");
    assert_eq!(m.serial.open_attempts(REAL), 1, "不得啟動 dynamic worker 重開埠");
    assert_eq!(device.close_count(), 0, "預開 handle 留在作業內，不關閉");
    assert!(device.is_open(), "預開 handle 仍開啟");
    assert!(device.take_tx().is_empty(), "落盤失敗不得啟動偵測 TX");
}

#[test]
fn wp510_b_open_error_with_fallback_stays_reattachable() {
    const SID: &str = "gone-template:COM3";
    const REAL: &str = "/dev/ttyG";
    let m = wp510_b_placeholder_rig(false, false);
    let sid = m.mgr.resolve(SID).expect("佔位 session");
    m.serial.fail_next_open(REAL, wp510_b_permission_error());
    m.serial.fail_next_open(REAL, wp510_b_permission_error());
    m.mgr.update_devices(&m.snapshot());

    let response = rpc(m.mgr.attach_released_placeholder(&sid, false));

    assert!(is_ok(&response), "一般開埠錯誤應交給 dynamic：{response:?}");
    let old_snapshot = session_field(&response);
    assert_eq!(s_of(&old_snapshot, "state").as_deref(), Some("DETACHED"));
    assert_eq!(s_of(&old_snapshot, "last_error").as_deref(), Some("ATTACH_FAILED:PermissionError"));
    wait_until("佔位 handoff 的第二次 dynamic 開埠", || {
        m.serial.open_attempts(REAL) == 2 && m.mgr.resolve("COM3").is_some()
    });
    wait_until("fallback session DETACHED", || {
        m.state_of("COM3").as_deref() == Some("DETACHED")
            && m.session_last_error("COM3").as_deref() == Some("ATTACH_FAILED:PermissionError")
    });
    assert_eq!(m.mgr.resolve(SID), None, "舊佔位須退休");
    assert_eq!(m.mgr.released_placeholder("COM3"), Some(false));
    assert_eq!(s_of(&m.session("COM3"), "profile_source").as_deref(), Some("fallback"));
    assert!(m.mgr.resolve("COM3").is_some(), "原 COM 應有 fallback 列");
    assert_eq!(m.serial.open_attempts(REAL), 2);

    m.mgr.bootstrap_attach();
    wait_until("fallback DETACHED session 再次開埠", || {
        m.serial.open_attempts(REAL) == 3
    });
    wait_until("fallback session 重新 attach", || {
        matches!(m.state_of("COM3").as_deref(), Some("ATTACHED" | "READY"))
    });
    assert!(m.mgr.resolve("COM3").is_some(), "重新 attach 後原 COM 仍可查詢");
    assert_eq!(m.mgr.released_placeholder("COM3"), Some(false));
}

#[test]
fn wp510_b_open_error_without_fallback_reserves_com_until_unplug() {
    const SID: &str = "gone-template:COM3";
    const BY_ID: &str = "/s/by-id/usb-G";
    const REAL: &str = "/dev/ttyG";
    const OTHER: &str = "/s/by-id/usb-next";
    let m = wp510_b_no_fallback_placeholder_rig();
    let sid = m.mgr.resolve(SID).expect("佔位 session");
    m.serial.fail_next_open(REAL, wp510_b_permission_error());
    m.serial.fail_next_open(REAL, wp510_b_permission_error());
    m.mgr.update_devices(&m.snapshot());

    let response = rpc(m.mgr.attach_released_placeholder(&sid, false));

    assert!(is_ok(&response), "一般開埠錯誤應交給 dynamic：{response:?}");
    let old_snapshot = session_field(&response);
    assert_eq!(s_of(&old_snapshot, "state").as_deref(), Some("DETACHED"));
    assert_eq!(s_of(&old_snapshot, "last_error").as_deref(), Some("ATTACH_FAILED:PermissionError"));
    wait_until("無 fallback 時 dynamic 第二次開埠", || m.serial.open_attempts(REAL) == 2);
    assert_eq!(m.mgr.resolve(SID), None);
    assert_eq!(m.mgr.resolve("COM3"), None);
    assert_eq!(m.mgr.com_for_by_id(BY_ID).as_deref(), Some("COM3"));
    assert_eq!(
        m.mgr.attach_released_placeholder(SID, false).unwrap().get("error_code"),
        Some(&PyValue::from("SESSION_NOT_FOUND"))
    );

    m.mgr.update_devices(&m.snapshot());
    std::thread::sleep(Duration::from_millis(50));
    assert_eq!(m.serial.open_attempts(REAL), 2, "相同裝置快照不得重試");
    assert_eq!(m.mgr.com_for_by_id(BY_ID).as_deref(), Some("COM3"));
    assert!(m.device(REAL).take_tx().is_empty());

    m.mgr.prepare_dynamic_rank(&[OTHER.to_string()]);
    assert_eq!(m.mgr.com_for_by_id(OTHER).as_deref(), Some("COM0"));

    m.mgr.update_devices(&snapshot_without(&m, BY_ID));
    assert_eq!(m.mgr.com_for_by_id(BY_ID), None, "拔除後釋放原 COM 預留");
    assert_eq!(m.mgr.com_for_by_id(OTHER), None);
}

#[test]
fn wp510_b_no_fallback_com_reservation_does_not_survive_restart() {
    const SID: &str = "gone-template:COM3";
    const BY_ID: &str = "/s/by-id/usb-G";
    const REAL: &str = "/dev/ttyG";
    let m = wp510_b_no_fallback_placeholder_rig();
    let sid = m.mgr.resolve(SID).expect("佔位 session");
    m.serial.fail_next_open(REAL, wp510_b_permission_error());
    m.serial.fail_next_open(REAL, wp510_b_permission_error());
    m.mgr.update_devices(&m.snapshot());

    let response = rpc(m.mgr.attach_released_placeholder(&sid, false));

    assert!(is_ok(&response), "一般開埠錯誤應交給 dynamic：{response:?}");
    wait_until("無 fallback handoff 的第二次 dynamic 開埠", || {
        m.serial.open_attempts(REAL) == 2 && m.mgr.resolve(SID).is_none()
    });
    assert_eq!(m.mgr.resolve("COM3"), None);
    assert_eq!(m.mgr.com_for_by_id(BY_ID).as_deref(), Some("COM3"));

    // OD-63 編排者裁決：預留只存在於裝置仍在線且 daemon 未重啟的期間。
    m.mgr.begin_shutdown();
    let restarted = m.restart();
    assert_eq!(restarted.mgr.com_for_by_id(BY_ID), None);
    restarted.mgr.prepare_dynamic_rank(&[BY_ID.to_string()]);
    assert_eq!(
        restarted.mgr.com_for_by_id(BY_ID).as_deref(),
        Some("COM0"),
        "重啟後應按 dynamic 規則重配，不沿用舊佔位 COM3"
    );
}

#[test]
fn wp510_b_missing_or_held_keeps_placeholder() {
    const SID: &str = "gone-template:COM3";
    const BY_ID: &str = "/s/by-id/usb-G";
    const REAL: &str = "/dev/ttyG";

    let offline = wp510_b_placeholder_rig(false, false);
    let sid = offline.mgr.resolve(SID).expect("佔位 session");
    offline.mgr.update_devices(&snapshot_without(&offline, BY_ID));
    let response = rpc(offline.mgr.attach_released_placeholder(&sid, false));
    assert_eq!(code_of(&response).as_deref(), Some("DEVICE_NOT_PRESENT"));
    assert_eq!(s_of(&response, "selector").as_deref(), Some("COM3"));
    assert_eq!(s_of(&response, "device_by_id").as_deref(), Some(BY_ID));
    wp510_b_assert_placeholder_retained(&offline, SID);
    assert_eq!(offline.serial.open_attempts(REAL), 0);
    assert!(offline.device(REAL).take_tx().is_empty());

    let prescan = wp510_b_placeholder_rig(false, false);
    let sid = prescan.mgr.resolve(SID).expect("佔位 session");
    prescan.serial.set_probe_holders(REAL, HolderReport {
        pids: vec![771],
        holder_unknown: false,
        status: HolderScanStatus::Ok,
    });
    prescan.mgr.update_devices(&prescan.snapshot());
    let response = rpc(prescan.mgr.attach_released_placeholder(&sid, false));
    assert_eq!(code_of(&response).as_deref(), Some("DEVICE_STILL_HELD"));
    assert_eq!(response.get("pids"), Some(&PyValue::List(vec![PyValue::Int(PyInt::Small(771))])));
    assert_eq!(response.get("holder_unknown"), Some(&PyValue::Bool(false)));
    wp510_b_assert_placeholder_retained(&prescan, SID);
    assert_eq!(prescan.serial.open_attempts(REAL), 0);
    assert!(prescan.device(REAL).take_tx().is_empty());

    for variant in [0, 1, 2] {
        let m = wp510_b_placeholder_rig(false, false);
        let sid = m.mgr.resolve(SID).expect("佔位 session");
        let (error, expected_code, expected_pids, expected_unknown) = match variant {
            0 => (
                OpenError::Held(HolderReport {
                    pids: vec![772],
                    holder_unknown: false,
                    status: HolderScanStatus::Ok,
                }),
                "DEVICE_STILL_HELD",
                Some(vec![PyValue::Int(PyInt::Small(772))]),
                Some(false),
            ),
            1 => (
                OpenError::AccessDenied {
                    os: PalError::plain(PalErrorKind::AccessDenied, "Access denied"),
                    holder: HolderReport {
                        pids: Vec::new(),
                        holder_unknown: true,
                        status: HolderScanStatus::Unknown,
                    },
                },
                "DEVICE_STILL_HELD",
                Some(Vec::new()),
                Some(true),
            ),
            _ => (
                OpenError::NotFound(PalError::plain(PalErrorKind::NotFound, "No such device")),
                "DEVICE_NOT_PRESENT",
                None,
                None,
            ),
        };
        m.serial.fail_next_open(REAL, error);
        m.mgr.update_devices(&m.snapshot());
        let response = rpc(m.mgr.attach_released_placeholder(&sid, false));
        assert_eq!(code_of(&response).as_deref(), Some(expected_code));
        if let Some(pids) = expected_pids {
            assert_eq!(response.get("pids"), Some(&PyValue::List(pids)));
        }
        if let Some(unknown) = expected_unknown {
            assert_eq!(response.get("holder_unknown"), Some(&PyValue::Bool(unknown)));
        }
        wp510_b_assert_placeholder_retained(&m, SID);
        assert_eq!(m.serial.open_attempts(REAL), 1);
        assert!(m.device(REAL).take_tx().is_empty());
    }
}

#[test]
fn wp510_b_handoff_invalidated_during_open_closes_handle() {
    const SID: &str = "gone-template:COM3";
    const BY_ID: &str = "/s/by-id/usb-G";
    const REAL: &str = "/dev/ttyG";
    const CHANGED: &str = "/dev/ttyG-rebound";

    for invalidation in ["unplug", "rebind", "shutdown"] {
        let m = wp510_b_placeholder_rig(false, false);
        let sid = m.mgr.resolve(SID).expect("佔位 session");
        let old_device = m.device(REAL);
        m.mgr.update_devices(&m.snapshot());
        m.serial.block_open(REAL);
        let manager = m.mgr.clone();
        let attach_sid = sid.clone();
        let attach = std::thread::spawn(move || manager.attach_released_placeholder(&attach_sid, false));
        wait_until("佔位 handoff 開埠進入阻塞鉤子", || m.serial.open_attempts(REAL) == 1);

        match invalidation {
            "unplug" => m.mgr.update_devices(&snapshot_without(&m, BY_ID)),
            "rebind" => {
                m.serial.add_device(CHANGED);
                m.mgr.update_devices(&[(BY_ID.to_string(), DeviceInfo {
                    by_id: BY_ID.to_string(),
                    real_path: CHANGED.to_string(),
                })]);
            }
            "shutdown" => m.mgr.begin_shutdown(),
            _ => unreachable!(),
        }
        m.serial.release_open(REAL);
        let response = rpc(attach.join().expect("handoff worker 不應 panic"));
        match invalidation {
            "unplug" | "rebind" => assert_eq!(code_of(&response).as_deref(), Some("DEVICE_NOT_PRESENT")),
            "shutdown" => {
                assert_eq!(
                    response.0.keys().map(String::as_str).collect::<Vec<_>>(),
                    ["ok", "error_code", "drain_stalled", "drain_timeout_epoch", "session"],
                    "shutdown 使佔位交接失效時須回完整 recovering 鍵：{response:?}"
                );
                assert_eq!(code_of(&response).as_deref(), Some("SESSION_RECOVERING"));
                assert_eq!(response.get("drain_stalled"), Some(&PyValue::Bool(false)));
                assert_eq!(response.get("drain_timeout_epoch"), Some(&PyValue::Null));
            }
            _ => unreachable!(),
        }
        assert_eq!(old_device.close_count(), 1, "{invalidation} 後 handle 須關閉");
        assert_eq!(m.mgr.resolve("COM3"), Some(sid.clone()));
        assert_eq!(m.mgr.resolve("t:COM3"), None);
        assert_eq!(m.mgr.released_placeholder(&sid), Some(true));
        assert!(old_device.take_tx().is_empty());
    }
}

#[test]
fn wp510_b_open_error_invalidated_returns_complete_recovering_response() {
    const SID: &str = "gone-template:COM3";
    const REAL: &str = "/dev/ttyG";
    let m = wp510_b_placeholder_rig(false, false);
    let sid = m.mgr.resolve(SID).expect("佔位 session");
    m.mgr.update_devices(&m.snapshot());
    m.serial.block_open(REAL);
    m.serial.fail_next_open(
        REAL,
        OpenError::Other(PalError::io(13, "[Errno 13] Permission denied")),
    );

    let manager = m.mgr.clone();
    let attach_sid = sid.clone();
    let attach = std::thread::spawn(move || manager.attach_released_placeholder(&attach_sid, false));
    wait_until("佔位開埠錯誤路徑停在 open_exclusive", || {
        m.serial.open_attempts(REAL) == 1
    });
    m.mgr.begin_shutdown();
    m.serial.release_open(REAL);
    let response = rpc(attach.join().expect("attach thread"));

    assert_eq!(
        response.0.keys().map(String::as_str).collect::<Vec<_>>(),
        ["ok", "error_code", "drain_stalled", "drain_timeout_epoch", "session"],
        "一般開埠錯誤遇 shutdown 時須回完整 recovering 鍵：{response:?}"
    );
    assert_eq!(code_of(&response).as_deref(), Some("SESSION_RECOVERING"));
    assert_eq!(response.get("drain_stalled"), Some(&PyValue::Bool(false)));
    assert_eq!(response.get("drain_timeout_epoch"), Some(&PyValue::Null));
}

/// 拔除 by_id 後的快照（ManagerRig 的登錄不變，供之後重插）。
fn snapshot_without(m: &ManagerRig, by_id: &str) -> Vec<(String, DeviceInfo)> {
    m.snapshot().into_iter().filter(|(k, _)| k != by_id).collect()
}

/// 建立 fallback（others-template）動態 session，等它 ATTACHED 後拔除並等 DETACHED；回傳實體路徑。
fn fallback_session_then_unplug(m: &ManagerRig, by_id: &str) -> String {
    m.devices(&[by_id]);
    let real = m.real_of(by_id);
    m.mgr.update_devices(&m.snapshot());
    m.mgr.bootstrap_attach();
    assert_eq!(m.wait_profile_source("COM0"), "fallback");
    m.wait_state("COM0", "ATTACHED");
    m.mgr.update_devices(&snapshot_without(m, by_id));
    m.wait_state("COM0", "DETACHED");
    real
}

#[test]
fn released_dynamic_materialize_template_fallback() {
    // #14：重啟時動態 RELEASED 列的 template 後備（session_manager.py `__init__` released 實體化）：
    // - template 缺定義但名稱為 others-template／passthrough → 最小 passthrough profile 實體化（非 command_capable）；
    // - 其他缺定義的 template（如 uboot-template）→ 以 passthrough 佔位實體化、WARNING、保留持久化阻擋（裝置上線也不 open）。
    const ONLY_PRPL: &str = "profiles:\n  prpl-template:\n    platform: prpl\n    prompt_regex: '(?m)^root@prpl:.*# '\n    timeout_s: 2.0\n";
    let mut loaded = LoadedState::default();
    loaded
        .released
        .insert("others-template:COM3".to_string(), released_row("/s/by-id/usb-R"));
    loaded
        .released
        .insert("uboot-template:COM4".to_string(), released_row("/s/by-id/usb-U"));
    let m = ManagerRig::with(ONLY_PRPL, loaded, None);
    let sid = m.mgr.resolve("others-template:COM3").expect("others-template 應以最小 profile 實體化");
    let s = m.session(&sid);
    assert_eq!(s_of(&s, "state").as_deref(), Some("RELEASED"));
    assert_eq!(s_of(&s, "platform").as_deref(), Some("passthrough"));
    assert_eq!(s.get("command_capable"), Some(&PyValue::Bool(false)));
    assert_eq!(s_of(&s, "profile_source").as_deref(), Some("fallback"));
    assert_eq!(s.get("act_no"), Some(&PyValue::Int(PyInt::Small(4))));
    assert_eq!(s_of(&s, "alias").as_deref(), Some("others-template+4"));
    assert_eq!(
        m.mgr.released_placeholder("others-template:COM3"),
        Some(false)
    );
    let uboot = m
        .mgr
        .resolve("uboot-template:COM4")
        .expect("缺失 uboot template 應以 RELEASED 佔位實體化");
    let uboot_session = m.session(&uboot);
    assert_eq!(s_of(&uboot_session, "state").as_deref(), Some("RELEASED"));
    assert_eq!(
        s_of(&uboot_session, "platform").as_deref(),
        Some("passthrough")
    );
    assert_eq!(
        uboot_session.get("command_capable"),
        Some(&PyValue::Bool(false))
    );
    assert_eq!(
        s_of(&uboot_session, "profile_source").as_deref(),
        Some("fallback")
    );
    assert_eq!(
        s_of(&uboot_session, "alias").as_deref(),
        Some("uboot-template+5")
    );
    assert_eq!(
        s_of(&uboot_session, "device_by_id").as_deref(),
        Some("/s/by-id/usb-U")
    );
    assert_eq!(s_of(&uboot_session, "released_by").as_deref(), Some("agent"));
    assert_eq!(
        s_of(&uboot_session, "released_at").as_deref(),
        Some("2026-10-21T00:00:00")
    );
    assert_eq!(
        s_of(&uboot_session, "released_reason").as_deref(),
        Some("manual")
    );
    assert_eq!(m.mgr.released_placeholder(&uboot), Some(true));
    assert!(m.warnings().iter().any(|w| w
        == "released session 的 template 不存在（session=uboot-template:COM4、profile=uboot-template），保留持久化阻擋"));
    assert!(
        m.load_state_now().released.contains_key("uboot-template:COM4"),
        "未實體化的 released 列須保留在 state.json"
    );
    let device_r = m.add_device_at("/s/by-id/usb-R", "/dev/ttyR");
    let device_u = m.add_device_at("/s/by-id/usb-U", "/dev/ttyU");
    m.mgr.update_devices(&m.snapshot());
    m.mgr.bootstrap_attach();
    std::thread::sleep(Duration::from_millis(100));
    assert_eq!(m.serial.open_attempts("/dev/ttyR"), 0);
    assert_eq!(m.serial.open_attempts("/dev/ttyU"), 0, "佔位仍阻擋該 tty");
    assert!(device_r.take_tx().is_empty());
    assert!(device_u.take_tx().is_empty());
    assert_eq!(m.mgr.counts().0, 2);
}

#[test]
fn released_dynamic_act_no_and_profile_source() {
    // #14：act_no 由 alias 列（<template>+N）推導，否則 COM 號＋1；profile_source 依 pin／detected／fallback；
    // session_id 沿用 state.json 的 key（template 經 pin 改變時不重算）。
    let mut loaded = LoadedState::default();
    loaded
        .released
        .insert("prpl-template:COM2".to_string(), released_row("/s/by-id/usb-P1"));
    loaded
        .released
        .insert("prpl-template:COM5".to_string(), released_row("/s/by-id/usb-P2"));
    loaded
        .released
        .insert("others-template:COM8".to_string(), released_row("/s/by-id/usb-P3"));
    let mut row = PyDict::new();
    row.insert("session_id", PyValue::from("prpl-template:COM2"));
    loaded.aliases.insert("prpl-template+7".to_string(), row);
    loaded
        .profile_pins
        .insert("/s/by-id/usb-P2".to_string(), "others-template".to_string());
    loaded
        .profile_detected
        .insert("/s/by-id/usb-P3".to_string(), "prpl-template".to_string());
    let m = ManagerRig::with(&manager_yaml(&[]), loaded, None);

    let a = m.session("prpl-template:COM2");
    assert_eq!(a.get("act_no"), Some(&PyValue::Int(PyInt::Small(7))));
    assert_eq!(s_of(&a, "alias").as_deref(), Some("prpl-template+7"));
    assert_eq!(s_of(&a, "profile_source").as_deref(), Some("fallback"));

    let b = m.session("prpl-template:COM5");
    assert_eq!(s_of(&b, "session_id").as_deref(), Some("prpl-template:COM5"));
    assert_eq!(s_of(&b, "profile").as_deref(), Some("others-template"));
    assert_eq!(s_of(&b, "profile_source").as_deref(), Some("pin"));
    assert_eq!(b.get("act_no"), Some(&PyValue::Int(PyInt::Small(6))));
    assert_eq!(s_of(&b, "alias").as_deref(), Some("others-template+6"));

    let c = m.session("others-template:COM8");
    assert_eq!(s_of(&c, "profile").as_deref(), Some("prpl-template"));
    assert_eq!(s_of(&c, "profile_source").as_deref(), Some("detected"));
    assert_eq!(c.get("act_no"), Some(&PyValue::Int(PyInt::Small(9))));
    for s in [a, b, c] {
        assert_eq!(s_of(&s, "state").as_deref(), Some("RELEASED"));
    }
}

#[test]
fn com_for_by_id_session_first_then_pending() {
    // #20：既有 session 的 COM 優先；否則 startup 預配（pending）COM；都沒有 → None。
    let yaml = manager_yaml(&[&others_target("COM5", 6, "/s/by-id/usb-X", "x")]);
    let m = ManagerRig::with(&yaml, LoadedState::default(), None);
    m.mgr.prepare_dynamic_rank(&[
        "/s/by-id/usb-X".to_string(),
        "/s/by-id/usb-A".to_string(),
    ]);
    assert_eq!(m.mgr.com_for_by_id("/s/by-id/usb-X").as_deref(), Some("COM5"));
    assert_eq!(m.mgr.com_for_by_id("/s/by-id/usb-A").as_deref(), Some("COM0"));
    assert_eq!(m.mgr.com_for_by_id("/s/by-id/usb-none"), None);
    m.add_device_at("/s/by-id/usb-X", "/dev/ttyX");
    m.devices(&["/s/by-id/usb-A"]);
    m.mgr.update_devices(&m.snapshot());
    m.mgr.bootstrap_attach();
    wait_until("usb-A 建立 session", || m.com_by_id().contains_key("/s/by-id/usb-A"));
    assert_eq!(m.mgr.com_for_by_id("/s/by-id/usb-A").as_deref(), Some("COM0"));
}

#[test]
fn bind_with_inflight_attach_op_cancels_waits_3000ms() {
    // #26：bind 遇舊 by_id 的 attach op（此處為卡在 open 的 #181 再偵測）→ 取消並等待 3000 ms；
    // 逾時 → SESSION_RECOVERING＋drain_stalled；op 結束後重試即成功。
    let m = ManagerRig::with(&manager_yaml(&[]), LoadedState::default(), None);
    let real = fallback_session_then_unplug(&m, "/s/by-id/usb-D");
    m.serial.block_open(&real);
    m.mgr.update_devices(&m.snapshot());
    wait_until("再偵測卡在 open", || m.serial.open_attempts(&real) == 2);
    let t0 = std::time::Instant::now();
    let r = rpc(m.mgr.bind("COM0", "/s/by-id/usb-Z"));
    let waited = t0.elapsed();
    assert!(waited >= Duration::from_millis(3000), "應等待 3000 ms：{waited:?}");
    assert_eq!(code_of(&r).as_deref(), Some("SESSION_RECOVERING"));
    assert_eq!(r.get("drain_stalled"), Some(&PyValue::Bool(true)));
    assert_eq!(
        s_of(&session_field(&r), "state").as_deref(),
        Some("RECOVERING")
    );
    // 審查 F64：Python `bind_session` 逾時時寫入活的 session（state、drain_stalled、drain_timeout_epoch），
    // 之後 `session.get_state` 同為 RECOVERING＋drain_stalled，`command.submit` 回 SESSION_RECOVERING。
    let live = m.session("COM0");
    assert_eq!(s_of(&live, "state").as_deref(), Some("RECOVERING"), "{live:?}");
    assert_eq!(live.get("drain_stalled"), Some(&PyValue::Bool(true)));
    assert_eq!(
        live.get("drain_timeout_epoch"),
        r.get("drain_timeout_epoch"),
        "活 session 的 epoch 與回應一致"
    );
    let sid = m.mgr.resolve("COM0").expect("COM0 session");
    let sub = rpc(m.mgr.route(&sid, SessionRequest::Submit(agent_submit("ls")), RPC));
    assert_eq!(code_of(&sub).as_deref(), Some("SESSION_RECOVERING"), "{sub:?}");
    m.serial.release_open(&real);
    // 取消的 op 拿到 handle 後立即關閉（不交給 session）。
    wait_until("取消的 op 關閉 handle", || m.device(&real).close_count() == 2);
    let r = rpc(m.mgr.bind("COM0", "/s/by-id/usb-Z"));
    assert!(is_ok(&r), "op 結束後 bind 應成功：{r:?}");
    assert_eq!(
        s_of(&m.session("COM0"), "device_by_id").as_deref(),
        Some("/s/by-id/usb-Z")
    );
}

/// wp510 第 14 項：既有 fallback session 重插後的 #181 再偵測（attach worker 持有作業、卡在 open）期間 `release`。
/// Python 作業在 `_attach_ops`（`_preopen_auto_attach_port` `origin/main:sw_core/session_manager.py:4191`）或
/// `_active_attach_ops`（`:4238`），`release_device` 取得作業後取消並等待其完成（`:3104-3107`、`:3117-3125`、
/// `:3188-3204`）；開埠返回見取消即關閉 handle（`:4216-4245`、`_finish_attach_op` `:4320-4365`），release 才回 ok
/// 並完成 RELEASED。取消後不得送 PROBE、不得把 handle 交給 session。
#[test]
fn wp510_review9_release_during_existing_session_redetect_cancels_and_waits() {
    let m = ManagerRig::with(&manager_yaml(&[]), LoadedState::default(), None);
    let real = fallback_session_then_unplug(&m, "/s/by-id/usb-D");
    m.serial.block_open(&real);
    m.mgr.update_devices(&m.snapshot());
    wait_until("再偵測卡在 open", || m.serial.open_attempts(&real) == 2);
    let closes_before = m.device(&real).close_count();
    let tx_before = m.device(&real).tx_len();
    let sid = m.mgr.resolve("COM0").expect("COM0 session");

    let manager = m.mgr.clone();
    let release_sid = sid.clone();
    let (reply_tx, reply_rx) = crossbeam_channel::bounded(1);
    let release = std::thread::spawn(move || {
        let _ = reply_tx.send(wp510_release(&manager, &release_sid));
    });
    let early = reply_rx.recv_timeout(Duration::from_millis(300)).ok();
    let replied_before_open_returned = early.is_some();
    m.serial.release_open(&real);
    let reply = match early {
        Some(reply) => Some(reply),
        None => reply_rx.recv_timeout(RPC).ok(),
    };
    release.join().expect("release thread 不應 panic");

    assert!(
        !replied_before_open_returned,
        "release 必須等待在途 attach 作業收尾才回覆"
    );
    let reply = rpc(reply.expect("release 應在作業收尾後回覆"));
    assert!(is_ok(&reply), "{reply:?}");
    wait_until("取消的作業關閉 handle", || {
        m.device(&real).close_count() == closes_before + 1
    });
    std::thread::sleep(Duration::from_millis(200));
    assert!(!m.device(&real).is_open(), "release 後不得留下開啟的 handle");
    assert_eq!(m.device(&real).tx_len(), tx_before, "取消後不得送 PROBE");
    assert_eq!(s_of(&m.session("COM0"), "state").as_deref(), Some("RELEASED"));
    // S2-PAR-wp510r R4（設計 §6 #57，H）：W6 作業改為外部 attach，在途改以帳本紀錄判定：收尾後 `custody_for_test(B)`＝None。
    assert!(m.mgr.custody_for_test("/s/by-id/usb-D").is_none());
}

/// wp510 第八次整包審查 High（P8-e25b69c-2）：同上再偵測卡在 open 超過 release 期限。Python `release_device` 期限到回
/// DRAIN_STALLED（`origin/main:sw_core/session_manager.py:3192-3204`）；開埠遲到返回見取消即關閉並 `_finish_attach_op`
/// （`:4216-4245`），`release_request` 為第一個 release 的 `(source, reason)`、作業的 `session_id` 即該 session
/// （`:4185-4186`），故轉 RELEASED、清 stall 欄位並持久化（`:4340-4363`）。
#[test]
fn wp510_review10_late_redetect_finish_after_release_timeout_completes_release() {
    let m = ManagerRig::with(&manager_yaml(&[]), LoadedState::default(), None);
    let real = fallback_session_then_unplug(&m, "/s/by-id/usb-D");
    m.serial.block_open(&real);
    m.mgr.update_devices(&m.snapshot());
    wait_until("再偵測卡在 open", || m.serial.open_attempts(&real) == 2);
    let closes_before = m.device(&real).close_count();
    let tx_before = m.device(&real).tx_len();
    let sid = m.mgr.resolve("COM0").expect("COM0 session");

    let started = Instant::now();
    let reply = m.mgr.route(
        &sid,
        SessionRequest::Release {
            source: "agent".to_string(),
            reason: PyValue::from("flash"),
        },
        RPC,
    );
    let waited = started.elapsed();
    m.serial.release_open(&real);
    let reply = rpc(reply);

    assert!(waited >= Duration::from_millis(2500), "release 須等待作業至期限：{waited:?}");
    assert_eq!(code_of(&reply).as_deref(), Some("DRAIN_STALLED"), "{reply:?}");
    assert_eq!(reply.get("drain_stalled"), Some(&PyValue::Bool(true)));
    m.wait_state("COM0", "RELEASED");
    // S2-PAR-wp510r R4（設計 §6 #42，H）：在途改以帳本紀錄判定，收尾後 `custody_for_test(B)`＝None。
    wait_until("作業收尾", || m.mgr.custody_for_test("/s/by-id/usb-D").is_none());
    let session = m.session("COM0");
    assert_eq!(s_of(&session, "released_by").as_deref(), Some("agent"), "{session:?}");
    assert_eq!(s_of(&session, "released_reason").as_deref(), Some("flash"), "{session:?}");
    assert_eq!(session.get("drain_stalled"), Some(&PyValue::Bool(false)), "{session:?}");
    assert_eq!(session.get("drain_timeout_epoch"), Some(&PyValue::Null), "{session:?}");
    wait_until("RELEASED 落盤", || {
        m.state_text().contains("\"released\"") && m.state_text().contains("/s/by-id/usb-D")
    });
    assert_eq!(m.device(&real).close_count(), closes_before + 1);
    assert!(!m.device(&real).is_open());
    assert_eq!(m.device(&real).tx_len(), tx_before, "取消後不得送 PROBE");
}

#[test]
fn hotplug_invalidates_pending_attach_op_and_closes_unused_port() {
    // #27：attach op 進行中裝置被拔除 → op 失效；之後拿到的 port 未被消費即關閉（零 TX、不建立 bridge）。
    let m = ManagerRig::with(&manager_yaml(&[]), LoadedState::default(), None);
    let real = fallback_session_then_unplug(&m, "/s/by-id/usb-D");
    let dev = m.device(&real);
    let _ = dev.take_tx();
    m.serial.block_open(&real);
    m.mgr.update_devices(&m.snapshot());
    wait_until("再偵測卡在 open", || m.serial.open_attempts(&real) == 2);
    m.mgr.update_devices(&snapshot_without(&m, "/s/by-id/usb-D"));
    m.serial.release_open(&real);
    wait_until("未消費的 port 關閉", || dev.close_count() == 2);
    std::thread::sleep(Duration::from_millis(100));
    assert_eq!(dev.open_count(), 2);
    assert!(dev.take_tx().is_empty(), "失效的 op 不得送出任何 TX");
    assert_eq!(m.state_of("COM0").as_deref(), Some("DETACHED"));
    assert_eq!(m.serial.open_attempts(&real), 2, "之後不得再 open");
}

#[test]
fn pending_rank_pruned_on_removal() {
    // #28：裝置離線時 prune 其預配 COM；熱插的新裝置可回收該號，不得保留給已離線的板。
    let m = ManagerRig::templates_only();
    m.devices(&["/s/by-id/usb-B"]);
    m.mgr.prepare_dynamic_rank(&[
        "/s/by-id/usb-A".to_string(),
        "/s/by-id/usb-B".to_string(),
    ]);
    assert_eq!(m.mgr.com_for_by_id("/s/by-id/usb-A").as_deref(), Some("COM0"));
    m.mgr.update_devices(&m.snapshot()); // usb-A 不在快照 → prune
    assert_eq!(m.mgr.com_for_by_id("/s/by-id/usb-A"), None);
    m.mgr.bootstrap_attach();
    wait_until("usb-B 建立 session", || m.com_by_id().contains_key("/s/by-id/usb-B"));
    assert_eq!(m.com_by_id()["/s/by-id/usb-B"], "COM1");
    m.devices(&["/s/by-id/usb-C"]);
    m.mgr.update_devices(&m.snapshot());
    wait_until("usb-C 建立 session", || m.com_by_id().contains_key("/s/by-id/usb-C"));
    assert_eq!(m.com_by_id()["/s/by-id/usb-C"], "COM0");
}

#[test]
fn rematerialize_profile_recomputes_session_id_and_alias() {
    // #31：pin 後重新 attach → 換 template，session_id 依新 profile 重算、自動 alias 跟著改名、alias 列搬移；
    // 使用者自訂的 alias 保留。
    let m = ManagerRig::with(&manager_yaml(&[]), LoadedState::default(), None);
    fallback_session_then_unplug(&m, "/s/by-id/usb-D");
    assert_eq!(m.mgr.resolve("COM0").as_deref(), Some("others-template:COM0"));
    assert!(is_ok(&rpc(m.mgr.pin("COM0", "prpl-template"))));
    m.mgr.update_devices(&m.snapshot());
    wait_until("rematerialize 完成", || {
        m.mgr.resolve("COM0").as_deref() == Some("prpl-template:COM0")
    });
    let s = m.session("COM0");
    assert_eq!(s_of(&s, "session_id").as_deref(), Some("prpl-template:COM0"));
    assert_eq!(s_of(&s, "alias").as_deref(), Some("prpl-template+1"));
    assert_eq!(s_of(&s, "profile_source").as_deref(), Some("pin"));
    assert_eq!(m.mgr.resolve("others-template:COM0"), None);
    let aliases = m.load_state_now().aliases;
    assert_eq!(
        aliases
            .get("prpl-template+1")
            .and_then(|r| r.get("session_id"))
            .and_then(PyValue::as_str),
        Some("prpl-template:COM0")
    );
    assert!(!aliases.contains_key("others-template+1"), "舊自動 alias 應移除：{aliases:?}");

    // 自訂 alias：rematerialize 不改名。
    let m2 = ManagerRig::with(&manager_yaml(&[]), LoadedState::default(), None);
    fallback_session_then_unplug(&m2, "/s/by-id/usb-D");
    assert!(is_ok(&rpc(m2.mgr.alias_set("others-template:COM0", "mybox"))));
    assert!(is_ok(&rpc(m2.mgr.pin("COM0", "prpl-template"))));
    m2.mgr.update_devices(&m2.snapshot());
    wait_until("rematerialize 完成", || {
        m2.mgr.resolve("mybox").as_deref() == Some("prpl-template:COM0")
    });
    assert_eq!(s_of(&m2.session("COM0"), "alias").as_deref(), Some("mybox"));
}

#[test]
fn pin_applies_on_reattach() {
    // #32（#181）：既有 fallback session 被 pin 後，重新 attach 套用 pin（繞過偵測：不送 PROBE）。
    let m = ManagerRig::with(&manager_yaml(&[]), LoadedState::default(), None);
    let real = fallback_session_then_unplug(&m, "/s/by-id/usb-D");
    let dev = m.device(&real);
    let _ = dev.take_tx();
    let probe_rows_before = m
        .wal_rows()
        .iter()
        .filter(|r| s_of(r, "com").as_deref() == Some("PROBE"))
        .count();
    assert!(is_ok(&rpc(m.mgr.pin("COM0", "prpl-template"))));
    m.mgr.update_devices(&m.snapshot());
    wait_until("pin 套用並重新 attach", || {
        m.mgr.resolve("COM0").as_deref() == Some("prpl-template:COM0")
            && m.state_of("COM0").as_deref() == Some("ATTACHED")
    });
    assert_eq!(
        s_of(&m.session("COM0"), "profile_source").as_deref(),
        Some("pin")
    );
    assert_eq!(s_of(&m.session("COM0"), "platform").as_deref(), Some("prpl"));
    let probe_rows_after = m
        .wal_rows()
        .iter()
        .filter(|r| s_of(r, "com").as_deref() == Some("PROBE"))
        .count();
    assert_eq!(probe_rows_after, probe_rows_before, "pin 繞過偵測，不得送 PROBE");
}

#[test]
fn fallback_redetects_on_reattach() {
    // #32（#181）：fallback 只是暫時分類——重新 attach 時再偵測一次；命中 → profile 換成偵測結果（detected）。
    let m = ManagerRig::with(&manager_yaml(&[]), LoadedState::default(), None);
    let real = fallback_session_then_unplug(&m, "/s/by-id/usb-D");
    // 第一次偵測已送過 `\n`（FakeDevice 以累積 TX 比對 trigger）：重插後的再偵測以第二個 `\n` 觸發 prompt。
    m.device(&real).on_tx_reply(b"\n\n", b"root@prpl:/# ");
    m.mgr.update_devices(&m.snapshot());
    wait_until("再偵測命中 prpl-template", || {
        m.mgr.resolve("COM0").as_deref() == Some("prpl-template:COM0")
    });
    let s = m.session("COM0");
    assert_eq!(s_of(&s, "profile_source").as_deref(), Some("detected"));
    assert_eq!(s_of(&s, "platform").as_deref(), Some("prpl"));
    // 再偵測與正式 bridge 共用同一 handle（#51）：本次重插只有一次 open。
    assert_eq!(m.serial.open_attempts(&real), 2);
}

#[test]
fn unresolvable_pin_falls_through() {
    // #32（#181）：指向不存在 template 的 pin 視同無 pin，fallback session 仍再偵測（不被失效 pin 卡死）。
    let mut loaded = LoadedState::default();
    loaded
        .profile_pins
        .insert("/s/by-id/usb-D".to_string(), "no-such-template".to_string());
    let m = ManagerRig::with(&manager_yaml(&[]), loaded, None);
    let real = fallback_session_then_unplug(&m, "/s/by-id/usb-D");
    // 第一次偵測已送過 `\n`（FakeDevice 以累積 TX 比對 trigger）：重插後的再偵測以第二個 `\n` 觸發 prompt。
    m.device(&real).on_tx_reply(b"\n\n", b"root@prpl:/# ");
    m.mgr.update_devices(&m.snapshot());
    wait_until("再偵測命中", || {
        m.mgr.resolve("COM0").as_deref() == Some("prpl-template:COM0")
    });
    assert_eq!(
        s_of(&m.session("COM0"), "profile_source").as_deref(),
        Some("detected")
    );
}

#[test]
fn attach_finish_respawns_when_state_requires() {
    // #50：開啟途中裝置改指（BridgeOpened 為舊路徑）→ 舊 worker 交 executor 退休 drain，且因 session 仍需 attach，
    // actor 以目前 real_path 再送一次 OpenBridge（Python `_spawn_attach` 收尾自動再 spawn）。對外狀態維持 ATTACHING。
    // 擁有者 2026-11-11 裁決（SPEC-GAP 1，列入 D5 的 S 類）：單次在途（設計 DR-8；規格 §5.2.4）下，新路徑的
    // OpenBridge 改在舊路徑回條（handle_closed:true）到了才送；回條前不得送出。其他斷言不變。
    let mut h = CoreHarness::new(SHELL_PROFILE);
    let first = h.feed(sw_core::mailbox::ControlEvent::DeviceAttached {
        by_id: DEVICE_BY_ID.to_string(),
        real_path: "/fake/tty-old".to_string(),
    });
    assert!(first.iter().any(|e| matches!(e, sw_core::session_core::Effect::OpenBridge { dev, .. } if dev.0 == "/fake/tty-old")));
    let second = h.feed(sw_core::mailbox::ControlEvent::DeviceAttached {
        by_id: DEVICE_BY_ID.to_string(),
        real_path: "/fake/tty-new".to_string(),
    });
    assert!(
        !second.iter().any(|e| matches!(e, sw_core::session_core::Effect::OpenBridge { .. })),
        "ATTACHING 中不得並行第二個 open"
    );
    let opened = h.opened_bridge("/fake/tty-old");
    let effects = h.feed(sw_core::mailbox::ControlEvent::BridgeOpened {
        epoch_hint: 0,
        result: Ok(opened),
    });
    assert!(
        effects.iter().any(|e| matches!(e, sw_core::session_core::Effect::BeginDrain { .. })),
        "舊路徑的 worker 須退休 drain"
    );
    assert!(
        !effects.iter().any(|e| matches!(e, sw_core::session_core::Effect::OpenBridge { .. })),
        "單次在途：舊路徑回條前不得送出新路徑的 OpenBridge"
    );
    let (old_epoch, old_stop) = h.pending_stop();
    let effects = h.receipt(old_epoch, old_stop).handle_closed(true).feed();
    assert!(
        effects.iter().any(|e| matches!(e, sw_core::session_core::Effect::OpenBridge { dev, .. } if dev.0 == "/fake/tty-new")),
        "仍需 attach → 舊路徑回條（handle_closed:true）到了才以新路徑再送 OpenBridge"
    );
    assert_eq!(s_of(&h.public(), "state").as_deref(), Some("ATTACHING"));
}

#[test]
fn clear_during_shutdown_errors() {
    // #97：shutdown 中 session.clear → Err("daemon stopping")（framing 轉 EXCEPTION）；RELEASED 早退先於 stopping。
    let m = ManagerRig::single_target(SHELL_PROFILE).ready();
    let sid = m.mgr.resolve("COM0").expect("COM0 session");
    m.mgr.begin_shutdown();
    match m.mgr.route(&sid, SessionRequest::Clear, RPC) {
        Err(e) => assert_eq!(e.message, "daemon stopping"),
        Ok(d) => panic!("stopping 後 clear 應回 Err，實際 {d:?}"),
    }
    assert_eq!(m.dev.close_count(), 0, "clear 被拒時不得 drain");
    assert_eq!(m.state_of("COM0").as_deref(), Some("READY"));

    let m2 = ManagerRig::single_target(SHELL_PROFILE).ready();
    let sid2 = m2.mgr.resolve("COM0").expect("COM0 session");
    rpc(m2.mgr.route(
        &sid2,
        SessionRequest::Release {
            source: "agent".to_string(),
            reason: PyValue::Str("t".to_string()),
        },
        RPC,
    ));
    m2.wait_state("COM0", "RELEASED");
    m2.mgr.begin_shutdown();
    let r = rpc(m2.mgr.route(&sid2, SessionRequest::Clear, RPC));
    assert_eq!(r.get("released"), Some(&PyValue::Bool(true)));
}

#[test]
fn device_attach_during_shutdown_errors() {
    // #97：shutdown 中 device.attach → Err("daemon stopping")，不 open。
    let m = ManagerRig::single_target(SHELL_PROFILE).ready();
    let sid = m.mgr.resolve("COM0").expect("COM0 session");
    rpc(m.mgr.route(
        &sid,
        SessionRequest::Release {
            source: "agent".to_string(),
            reason: PyValue::Str("t".to_string()),
        },
        RPC,
    ));
    m.wait_state("COM0", "RELEASED");
    let opens = m.dev.open_count();
    m.mgr.begin_shutdown();
    match m.mgr.route(&sid, SessionRequest::DeviceAttach { force: false }, RPC) {
        Err(e) => assert_eq!(e.message, "daemon stopping"),
        Ok(d) => panic!("stopping 後 device.attach 應回 Err，實際 {d:?}"),
    }
    assert_eq!(m.dev.open_count(), opens);
    assert_eq!(m.state_of("COM0").as_deref(), Some("RELEASED"));
}

#[test]
fn dynamic_attach_skipped_during_shutdown_or_drain() {
    // #97：shutdown 中新裝置不建立動態 session、不 open；已越過 gate 的動態 attach 於 open 返回後即關閉（零 TX）。
    let m = ManagerRig::templates_only();
    m.mgr.begin_shutdown();
    m.devices(&["/s/by-id/usb-N"]);
    let real = m.real_of("/s/by-id/usb-N");
    m.mgr.update_devices(&m.snapshot());
    m.mgr.bootstrap_attach();
    std::thread::sleep(Duration::from_millis(50));
    assert_eq!(m.mgr.counts().0, 0);
    assert_eq!(m.serial.open_attempts(&real), 0);

    let m2 = ManagerRig::templates_only();
    m2.devices(&["/s/by-id/usb-N"]);
    let real2 = m2.real_of("/s/by-id/usb-N");
    m2.serial.block_open(&real2);
    m2.mgr.update_devices(&m2.snapshot());
    wait_until("動態 attach 卡在 open", || m2.serial.open_attempts(&real2) == 1);
    m2.mgr.begin_shutdown();
    m2.serial.release_open(&real2);
    wait_until("open 返回後關閉", || m2.device(&real2).close_count() == 1);
    std::thread::sleep(Duration::from_millis(50));
    assert_eq!(m2.mgr.counts().0, 0, "drain／shutdown 中不建立動態 session");
    assert!(m2.device(&real2).take_tx().is_empty(), "零 TX（不送偵測 probe）");
}

// ---------------------------------------------------------------------------
// 測試輔助（本檔專用）。
// ---------------------------------------------------------------------------

/// PROBE 預設 UART 設定（外部 open 驗證用）。
fn probe_cfg() -> SerialConfig {
    SerialConfig::from_uart_profile(115_200, 8, "N", 1, "none", false)
}

/// `bind` 的 RPC 呼叫（Result 解包）。
fn rpc_bind(m: &ManagerRig, selector: &str, by_id: &str) -> PyDict {
    rpc(m.mgr.bind(selector, by_id))
}

/// P5-T19 fix（skeleton guard `test_business_crates_cannot_bypass_fence_with_always_open`）：偵測 PROBE 的 `\n` 必須經
/// TxWriter／Fence 送出（Python PROBE bridge 的 send 同樣走 TX gate），TX WAL 只由 TxWriter 寫一筆（不重複）。
/// 重現：PROBE 的 Fence 凍結時 OS 提交為零、無 PROBE TX WAL；未凍結時恰一次提交、恰一筆 PROBE TX，且 handle 仍只 open 一次（#51）。
#[test]
fn detect_probe_tx_goes_through_fence() {
    let tx_rows = |m: &ManagerRig| {
        m.wal_rows()
            .into_iter()
            .filter(|r| s_of(r, "com").as_deref() == Some("PROBE") && s_of(r, "dir").as_deref() == Some("TX"))
            .count()
    };

    // 凍結：零 TX、偵測不中 → fallback（passthrough 不送任何 bytes）。
    let m = ManagerRig::with(&manager_yaml(&[]), LoadedState::default(), None);
    m.devices(&["/s/by-id/usb-F"]);
    let real = m.real_of("/s/by-id/usb-F");
    m.reply_probe(&real, b"root@prpl:/# ");
    m.mgr.freeze_probe_tx_for_test(true);
    m.mgr.update_devices(&m.snapshot());
    m.mgr.bootstrap_attach();
    assert_eq!(m.wait_profile_source("COM0"), "fallback");
    assert_eq!(m.device(&real).os_submits(), 0, "PROBE Fence 凍結時不得提交任何 bytes 給 OS");
    assert_eq!(tx_rows(&m), 0, "未送出的 PROBE TX 不得寫 WAL");
    // 審查 F71：Python `detect_template` 的 `send_command("")` 被拒即拋例外，呼叫端 `except` → `detected=None`，
    // 不睡 3 s、不比對 RX（否則 DUT 在視窗內自吐的 prompt 會被誤判為 detected）。
    assert!(
        !m.clock.sleeps_s().contains(&3.0),
        "PROBE 被拒後不得再等偵測視窗：{:?}",
        m.clock.sleeps_s()
    );

    // 未凍結：恰一次 OS 提交、恰一筆 PROBE TX WAL（由 TxWriter 寫），PROBE 與正式 bridge 共用同一 handle。
    let m = ManagerRig::with(&manager_yaml(&[]), LoadedState::default(), None);
    m.devices(&["/s/by-id/usb-F"]);
    let real = m.real_of("/s/by-id/usb-F");
    m.reply_probe(&real, b"root@prpl:/# ");
    m.mgr.update_devices(&m.snapshot());
    m.mgr.bootstrap_attach();
    assert_eq!(m.wait_profile_source("COM0"), "detected");
    assert_eq!(tx_rows(&m), 1, "PROBE TX 只由 TxWriter 寫一筆 WAL");
    assert_eq!(m.serial.open_attempts(&real), 1);
    // 審查 F72：PROBE 的唯一一筆 TX 為 Python `send_command("")` 補上的 `\n`（payload_b64 `Cg==`），
    // 且裝置收到的第一筆提交即此 `\n`（之後的提交屬正式 bridge 的登入 probe）。
    let probe_payloads: Vec<String> = m
        .wal_rows()
        .into_iter()
        .filter(|r| s_of(r, "com").as_deref() == Some("PROBE") && s_of(r, "dir").as_deref() == Some("TX"))
        .filter_map(|r| s_of(&r, "payload_b64"))
        .collect();
    assert_eq!(probe_payloads, vec!["Cg==".to_string()]);
    assert!(
        m.device(&real).take_tx().starts_with(b"\n"),
        "PROBE payload 應為 \\n"
    );
}

/// sol-backfill p5 #6：PROBE Fence 必須向 WAL 健康 fanout 註冊。重現順序：PROBE 讀到健康快照、建立 Fence →
/// 另一處 WAL append 失敗並發布 unhealthy → writer 才提交。Python `_send_bytes_locked` 於送出時讀共用 WAL
/// unhealthy，自動 TX 應被拒且零 OS 提交、零 PROBE TX WAL；偵測不中 → fallback。
#[test]
fn detect_probe_fence_observes_wal_unhealthy_published_before_submit() {
    let m = ManagerRig::with(&manager_yaml(&[]), LoadedState::default(), None);
    m.devices(&["/s/by-id/usb-F"]);
    let real = m.real_of("/s/by-id/usb-F");
    m.reply_probe(&real, b"root@prpl:/# ");
    let fanout = m.mgr.wal_health_fanout();
    m.mgr.set_probe_pre_submit_hook_for_test(Box::new(move || {
        sw_core::wal::WalHealthSink::set_wal_unhealthy(&*fanout, true);
    }));
    m.mgr.update_devices(&m.snapshot());
    m.mgr.bootstrap_attach();
    assert_eq!(m.wait_profile_source("COM0"), "fallback");
    assert_eq!(m.device(&real).os_submits(), 0, "WAL 已 unhealthy 時 PROBE 不得提交任何 bytes 給 OS");
    let probe_tx = m
        .wal_rows()
        .into_iter()
        .filter(|r| s_of(r, "com").as_deref() == Some("PROBE") && s_of(r, "dir").as_deref() == Some("TX"))
        .count();
    assert_eq!(probe_tx, 0, "被拒的 PROBE TX 不得寫 WAL");
}

#[test]
fn watcher_tick_services_deferred_wal_rotation() {
    // 審查 F10（OD-19）：Python `Service._on_watcher_tick` 每輪先 `wal.tick()`——延後輪替的完成不得依賴後續 append。
    // 現行檔達門檻、rename 連 4 次 EACCES 轉入延遲佇列；之後不再 append，只送 watcher Tick → 輪替完成並寫
    // `WAL_ROTATED_AFTER_DEFERRAL`。
    let m = ManagerRig::templates_only();
    let wal_path = std::path::Path::new("/fake/wal/raw.wal.ndjson");
    m.wfs.seed(wal_path, &vec![b'\n'; 67_108_864]);
    m.wfs
        .fail_next_renames(4, sw_pal::PalError::io(13, "[Errno 13] Permission denied"));
    let append = || {
        m.wal.append(sw_core::wal::WalAppendReq {
            com: "COM0",
            dir: "RX",
            source: "uart",
            payload: b"x",
            cmd_id: None,
            loss_flag: false,
            meta: None,
        })
    };
    m.clock.advance(Duration::from_millis(300));
    for backoff_ms in [11u64, 51, 201, 0] {
        append();
        m.clock.advance(Duration::from_millis(backoff_ms));
    }
    let h = m.wal.health().to_pydict();
    assert_eq!(h.get("deferred_delete_pending").and_then(PyValue::as_i64), Some(1));
    m.clock.advance(Duration::from_millis(1001));
    m.mgr.watcher_sender().send(WatcherEvent::Tick).unwrap();
    let deadline = std::time::Instant::now() + Duration::from_secs(5);
    loop {
        let h = m.wal.health().to_pydict();
        if h.get("deferred_delete_pending").and_then(PyValue::as_i64) == Some(0) {
            break;
        }
        assert!(std::time::Instant::now() < deadline, "watcher Tick 未推進延後輪替：{h:?}");
        std::thread::sleep(Duration::from_millis(10));
    }
    let text = String::from_utf8(m.wfs.contents(wal_path).expect("新現行檔")).unwrap();
    assert!(text.contains("WAL_ROTATED_AFTER_DEFERRAL"), "{text}");
}

// ---------------------------------------------------------------------------
// 審查 F43／F30（afe4853／8b8a40f）：POSIX bridge 開成時建 internal 哨兵 primary PTY——Python
// `UARTBridge.start` 的 `_create_console_client("primary")`：`console_count == 1`、`vtty == "/dev/pts/N"`，
// release 回應 `closed_consoles == 1`；無 PTY 平台（create_pty Unsupported）為 0／null。
// ---------------------------------------------------------------------------

fn native_console_rig() -> ManagerRig {
    ManagerRig::with_console(
        SHELL_PROFILE,
        LoadedState::default(),
        Some(("/s/by-id/usb-A", "/dev/ttyOLD")),
        sw_pal::native().console.clone(),
    )
    .ready()
}

fn has_pty() -> bool {
    sw_pal::native().console.create_pty().is_ok()
}

#[test]
fn sentinel_pty_reported_in_public_and_release() {
    let m = native_console_rig();
    let s = m.session("COM0");
    if has_pty() {
        assert_eq!(s.get("console_count").and_then(PyValue::as_i64), Some(1), "{s:?}");
        let vtty = s.get("vtty").and_then(PyValue::as_str).unwrap_or_default().to_string();
        assert!(vtty.starts_with("/dev/pts/") && std::path::Path::new(&vtty).exists(), "vtty={vtty}");
    } else {
        assert_eq!(s.get("console_count").and_then(PyValue::as_i64), Some(0));
        assert_eq!(s.get("vtty"), Some(&PyValue::Null));
    }
    let sid = m.mgr.resolve("COM0").expect("COM0 session");
    let r = rpc(m.mgr.route(
        &sid,
        SessionRequest::Release {
            source: "agent".to_string(),
            reason: PyValue::Null,
        },
        RPC,
    ));
    assert!(is_ok(&r), "{r:?}");
    assert_eq!(
        r.get("closed_consoles").and_then(PyValue::as_i64),
        Some(i64::from(has_pty()))
    );
    let s = session_field(&r);
    assert_eq!(s.get("console_count").and_then(PyValue::as_i64), Some(0));
    assert_eq!(s.get("vtty"), Some(&PyValue::Null));
}

#[test]
fn selftest_fallback_passthrough_suggests_pin_profile() {
    // 審查 F31（8b8a40f）：fallback passthrough（ATTACHED）且 RX tail 命中非 passthrough template 的 prompt →
    // Python `PASSTHROUGH`＋`recommended_action:"pin_profile"`＋`suggested_profile`＋`hint`，零 TX。
    let m = ManagerRig::with(&manager_yaml(&[]), LoadedState::default(), None);
    m.devices(&["/s/by-id/usb-F"]);
    m.mgr.update_devices(&m.snapshot());
    wait_until("fallback session", || m.wait_profile_source("COM0") == "fallback");
    m.wait_state("COM0", "ATTACHED");
    let real = m.real_of("/s/by-id/usb-F");
    let dev = m.device(&real);
    dev.push_rx(b"\r\nroot@prpl:/# ");
    wait_until("RX 進入視窗", || {
        m.session("COM0")
            .get("rx_bytes_last_10s")
            .and_then(PyValue::as_i64)
            .is_some_and(|n| n > 0)
    });
    let submits = dev.os_submits();
    let sid = m.mgr.resolve("COM0").expect("COM0 session");
    let r = rpc(m.mgr.route(
        &sid,
        SessionRequest::SelfTest {
            timeout_s: 2.0,
            strict_human_lock: false,
        },
        RPC,
    ));
    assert_eq!(s_of(&r, "classification").as_deref(), Some("PASSTHROUGH"), "{r:?}");
    assert_eq!(s_of(&r, "recommended_action").as_deref(), Some("pin_profile"));
    assert_eq!(s_of(&r, "suggested_profile").as_deref(), Some("prpl-template"));
    let hint = s_of(&r, "hint").unwrap_or_default();
    assert!(
        hint.contains("serialwrap session pin --selector COM0 --profile prpl-template"),
        "{hint}"
    );
    assert_eq!(dev.os_submits(), submits, "PASSTHROUGH 分類零 TX");
}

#[test]
fn log_filename_uses_local_time_by_default() {
    // 審查 F34（8b8a40f）：Python `time.strftime("%y%m%d-%H%M%S")` 為本地時間。manager 預設以平台 Clock
    // （`local_utc_offset_s`）組檔名，不得以 UTC 代替。wall=1970-01-01T00:00:00Z、本地 UTC+8 → `COM0_700101-080000.log`。
    let dir = tempfile::tempdir().expect("tempdir");
    let yaml = format!(
        "profiles:\n  t:\n    platform: shell\n    prompt_regex: '(?m)^root@t:.*# '\n    timeout_s: 2.0\n    log_dir: {}\ntargets:\n  - {{com: COM0, profile: t, act_no: 1, alias: t+1, device_by_id: /s/by-id/usb-A}}\n",
        dir.path().display()
    );
    let m = ManagerRig::single_target(&yaml).ready();
    m.clock.set_local_offset_s(8 * 3600);
    let sid = m.mgr.resolve("COM0").expect("COM0 session");
    let r = rpc(m.mgr.route(&sid, SessionRequest::LogStart, RPC));
    assert!(is_ok(&r), "{r:?}");
    let path = s_of(&r, "log_path").unwrap_or_default();
    let expected = sw_proto::pycompat::strftime_local(
        sw_pal::traits::Clock::wall_unix_us(&*m.clock),
        8 * 3600,
        "%y%m%d-%H%M%S",
    );
    assert!(path.ends_with(&format!("COM0_{expected}.log")), "{path} vs {expected}");
    let _ = rpc(m.mgr.route(&sid, SessionRequest::LogStop, RPC));
}

#[test]
fn dynamic_session_applies_template_uart_config() {
    // 審查 F59（High，73634db）：動態 session 以預設設定預開 PROBE，偵測命中模板後，正式 bridge 必須對同一預開
    // handle 套用模板的 UART 設定（Python bridge 啟動時 `port.configure(self.profile)`），不得停在 115200。
    let yaml = format!(
        "{}  fast:\n    platform: shell\n    prompt_regex: '(?m)^root@fast:.*# '\n    timeout_s: 2.0\n    uart:\n      baud: 921600\n",
        manager_yaml(&[])
    );
    let m = ManagerRig::with(&yaml, LoadedState::default(), None);
    m.devices(&["/s/by-id/usb-F"]);
    let real = m.real_of("/s/by-id/usb-F");
    let dev = m.device(&real);
    dev.on_tx_reply(b"\n", b"root@fast:/# ");
    m.mgr.update_devices(&m.snapshot());
    m.mgr.bootstrap_attach();
    wait_until("偵測命中 fast", || m.mgr.resolve("COM0").as_deref() == Some("fast:COM0"));
    wait_until("正式 bridge 已套用設定", || {
        dev.configs().last().is_some_and(|cfg| cfg.baud == 921_600)
    });
    let configs = dev.configs();
    assert_eq!(configs.first().map(|c| c.baud), Some(115_200), "PROBE 以預設設定開啟：{configs:?}");
    assert_eq!(m.serial.open_attempts(&real), 1, "PROBE 與正式 bridge 共用同一 handle（#51）");
}

#[test]
fn missing_explicit_target_marked_device_not_found() {
    // 審查 F60（High，73634db）：Python `update_devices` 每次掃描以 `_mark_missing_devices_locked` 標記
    // 「非 RELEASED、無 bridge、裝置不在快照」的 session：state DETACHED、last_error（None 時）DEVICE_NOT_FOUND、
    // detached_at（None 時）填入。
    let m = ManagerRig::with(SHELL_PROFILE, LoadedState::default(), None);
    m.mgr.update_devices(&[]);
    wait_until("DEVICE_NOT_FOUND", || {
        m.session_last_error("COM0").as_deref() == Some("DEVICE_NOT_FOUND")
    });
    let s = m.session("COM0");
    assert_eq!(s_of(&s, "state").as_deref(), Some("DETACHED"));
    assert!(s_of(&s, "detached_at").is_some(), "{s:?}");
}

#[test]
fn pin_then_clear_on_attached_session_applies_pin() {
    // 審查 F62（High，73634db）：parity #32／#181「session pin + session clear 對既有 session 生效」。
    // Python `clear_session` drain 後 `_complete_drain` → `_spawn_attach` → `_attach_by_id` 開埠前
    // `_reresolve_profile_on_reattach`：pin 可解析即換 template（profile_source pin）、不送 PROBE。
    // 具體輸入：fallback session（others-template:COM0）已 ATTACHED → pin prpl-template → clear。
    let m = ManagerRig::with(&manager_yaml(&[]), LoadedState::default(), None);
    m.devices(&["/s/by-id/usb-D"]);
    m.mgr.update_devices(&m.snapshot());
    m.mgr.bootstrap_attach();
    assert_eq!(m.wait_profile_source("COM0"), "fallback");
    m.wait_state("COM0", "ATTACHED");
    let probe_rows = |m: &ManagerRig| {
        m.wal_rows()
            .iter()
            .filter(|r| s_of(r, "com").as_deref() == Some("PROBE"))
            .count()
    };
    let before = probe_rows(&m);
    assert!(is_ok(&rpc(m.mgr.pin("COM0", "prpl-template"))));
    let sid = m.mgr.resolve("COM0").expect("COM0 session");
    let r = rpc(m.mgr.route(&sid, SessionRequest::Clear, RPC));
    assert!(is_ok(&r), "clear 應成功：{r:?}");
    wait_until("clear 後套用 pin", || {
        m.mgr.resolve("COM0").as_deref() == Some("prpl-template:COM0")
            && m.state_of("COM0").as_deref() == Some("ATTACHED")
    });
    let s = m.session("COM0");
    assert_eq!(s_of(&s, "profile_source").as_deref(), Some("pin"));
    assert_eq!(s_of(&s, "platform").as_deref(), Some("prpl"));
    assert_eq!(probe_rows(&m), before, "pin 繞過偵測，不得送 PROBE");
}


#[test]
fn shutdown_gate_after_open_started_stops_bridge_without_tx() {
    // 審查 F66（Medium，73634db）：原 #29 斷言在 begin_shutdown 之後才送熱插，update_devices 早退，修正前也綠。
    // 改為「open 已越過 gate、阻塞中 → begin_shutdown → 放行」：Python `_attach_by_id` 在 `bridge.start()` 後持鎖
    // 檢查 `_shutting_down` → `_begin_attach_bridge_stop_locked` 收掉剛開的 bridge，不送登入 probe；
    // session 回 DETACHED（parity #29：凍結早退不停在 ATTACHING）。
    let m = ManagerRig::with(SHELL_PROFILE, LoadedState::default(), None);
    let real = "/dev/ttyGATE";
    m.add_device_at("/s/by-id/usb-A", real);
    m.serial.block_open(real);
    m.mgr.update_devices(&m.snapshot());
    m.mgr.bootstrap_attach();
    wait_until("open 進入阻塞", || m.serial.open_attempts(real) == 1);
    m.mgr.begin_shutdown();
    m.serial.release_open(real);
    wait_until("COM0 DETACHED", || m.state_of("COM0").as_deref() == Some("DETACHED"));
    std::thread::sleep(Duration::from_millis(200));
    assert_eq!(m.state_of("COM0").as_deref(), Some("DETACHED"));
    assert!(m.device(real).take_tx().is_empty(), "stopping 後不得送登入 probe");
}

#[test]
fn recover_released_during_stopping_returns_released() {
    // 審查 F68（Low，73634db）：Python `recover_session` 先判 RELEASED（回
    // `{"ok":true,"released":true,"recommended_action":"device_attach",…}`），才輪到 stopping 的例外。
    let m = ManagerRig::single_target(SHELL_PROFILE).ready();
    let sid = m.mgr.resolve("COM0").expect("COM0 session");
    rpc(m.mgr.route(
        &sid,
        SessionRequest::Release {
            source: "agent".to_string(),
            reason: PyValue::Str("t".to_string()),
        },
        RPC,
    ));
    m.wait_state("COM0", "RELEASED");
    m.mgr.begin_shutdown();
    let r = rpc(m.mgr.route(
        &sid,
        SessionRequest::Recover {
            timeout_s: 1.0,
            force: false,
        },
        RPC,
    ));
    assert_eq!(r.get("ok"), Some(&PyValue::Bool(true)), "{r:?}");
    assert_eq!(r.get("released"), Some(&PyValue::Bool(true)));
    assert_eq!(r.get("recommended_action"), Some(&PyValue::from("device_attach")));
}

#[test]
fn bind_to_online_device_live_state_attaching_before_reply() {
    // 審查 F70（Low，73634db）：Python `bind_session` 在回覆前把活 session 設 ATTACHING＋`last_error=None`；
    // 回覆後立即 `session.get_state` 不得看到 DETACHED＋DEVICE_NOT_FOUND（舊實作只改回應副本，另以 control lane
    // 非同步送 DeviceAttached，請求通道的 GetState 會先被處理）。輸入：RELEASED 的 COM0 改綁回仍在線的原 by_id。
    let m = ManagerRig::single_target(SHELL_PROFILE).ready();
    let sid = m.mgr.resolve("COM0").expect("COM0 session");
    let by_id = s_of(&m.session("COM0"), "device_by_id").expect("by_id");
    rpc(m.mgr.route(
        &sid,
        SessionRequest::Release {
            source: "agent".to_string(),
            reason: PyValue::Null,
        },
        RPC,
    ));
    m.wait_state("COM0", "RELEASED");
    let r = rpc(m.mgr.bind("COM0", &by_id));
    assert!(is_ok(&r), "{r:?}");
    let live = m.session("COM0");
    let reply = session_field(&r);
    assert_eq!(s_of(&reply, "state").as_deref(), Some("ATTACHING"));
    assert_eq!(reply.get("last_error"), Some(&PyValue::Null));
    assert_ne!(s_of(&live, "state").as_deref(), Some("DETACHED"), "{live:?}");
    assert_ne!(
        s_of(&live, "last_error").as_deref(),
        Some("DEVICE_NOT_FOUND"),
        "{live:?}"
    );
}

#[test]
fn release_persist_point_failure_fails_release_commit() {
    // 審查 F85（Low，3938032）：Python `_test_release_persist_point` 的 `os.makedirs`／寫 `arrived` 失敗會拋出，
    // `_save_release_state` 不落盤、release 持久化失敗（不得吞掉錯誤後照常 commit）。
    struct FailingPoint;
    impl sw_core::session_manager::PersistObserver for FailingPoint {
        fn before_release_commit(&self) -> Result<(), sw_pal::PalError> {
            Err(sw_pal::PalError::io(20, "[Errno 20] Not a directory: '/dev/null/x'"))
        }
        fn after_commit(&self, _release: bool) {}
    }
    let m = ManagerRig::single_target(SHELL_PROFILE).ready();
    m.mgr.set_persist_observer(std::sync::Arc::new(FailingPoint));
    let sid = m.mgr.resolve("COM0").expect("COM0 session");
    let r = m.mgr.route(
        &sid,
        SessionRequest::Release {
            source: "agent".to_string(),
            reason: PyValue::Null,
        },
        RPC,
    );
    assert!(r.is_err(), "暫停點失敗時 release 持久化不得成功：{r:?}");
    assert!(
        m.load_state_now().released.is_empty(),
        "state.json 不得寫入 released 列"
    );
}

#[test]
fn manager_startup_always_rewrites_state_json() {
    // 審查 F56（Medium，c96a91c）：Python `SessionManager.__init__` 尾端一律 `_save_state()`（#13），即使內容與磁碟
    // 相同也重寫；原 `state_store::startup_always_rewrites_state_json` 只驗 store 自身的 commit，不證明啟動會寫。
    let m = ManagerRig::with(SHELL_PROFILE, LoadedState::default(), None);
    assert!(m.state_writes() >= 1, "SessionManager 建構即應寫一次 state.json");
    let text = m.state_text();
    assert!(text.contains("\"aliases\"") && text.ends_with('\n'), "{text:?}");
}

#[test]
fn reattach_redetect_to_ready_does_not_write_sticky() {
    // 審查 F65（manager 層）：sticky 只由「動態 attach 本身」到 READY 時寫入（Python `_attach_by_id_dynamic`）。
    // 既有 fallback session 重插經 `_reresolve_profile_on_reattach` 再偵測命中 `t` 並到 READY，Python 不寫
    // `profile_detected`。
    let m = ManagerRig::with(&manager_yaml(&[]), LoadedState::default(), None);
    let real = fallback_session_then_unplug(&m, "/s/by-id/usb-D");
    let dev = m.device(&real);
    install_shell_replies(&dev);
    // 第一次偵測已送過 `\n`：重插後的再偵測（第二個 `\n`）與登入 probe（第三個 `\n`）各需一次 prompt。
    dev.on_tx_reply(b"\n\n", b"root@t:/# ");
    dev.on_tx_reply(b"\n\n\n", b"root@t:/# ");
    m.mgr.update_devices(&m.snapshot());
    wait_until("再偵測命中 t 並 READY", || {
        m.mgr.resolve("COM0").as_deref() == Some("t:COM0")
            && m.state_of("COM0").as_deref() == Some("READY")
    });
    assert_eq!(
        s_of(&m.session("COM0"), "profile_source").as_deref(),
        Some("detected")
    );
    std::thread::sleep(Duration::from_millis(200));
    assert!(
        m.load_state_now().profile_detected.is_empty(),
        "重新 attach 的再偵測到 READY 不得寫 sticky：{}",
        m.state_text()
    );
}

#[test]
fn global_bg_capture_eviction_reaches_owner_session_via_manager() {
    // 審查 d2d29a6_7af6567 3b5927a #1（F28 manager 接線）：跨 session 淘汰須經 actor `register` →
    // `ManagerNote::EvictCapture` → manager 依 COM 解析擁有者 → `ControlEvent::EvictCapture`。
    // Python `SessionManager._background` 為全域 dict，`_evict_background_locked` 直接 `pop`，之後
    // `result_tail` 回 CMD_NOT_FOUND。輸入：COM1 先完成 64 筆 bg capture，COM0 再完成 1 筆 → 淘汰 COM1 最舊者。
    let yaml = manager_yaml(&[
        &t_target("COM0", 1, "/s/by-id/usb-A"),
        &t_target("COM1", 2, "/s/by-id/usb-B"),
    ]);
    let m = ManagerRig::with(&yaml, LoadedState::default(), Some(("/s/by-id/usb-A", "/dev/ttyA")));
    let dev_b = m.add_device_at("/s/by-id/usb-B", "/dev/ttyB");
    install_shell_replies(&dev_b);
    m.mgr.update_devices(&m.snapshot());
    m.mgr.bootstrap_attach();
    m.wait_state("COM0", "READY");
    m.wait_state("COM1", "READY");
    // `on_tx_reply` 只推一次；65 筆命令改用逐行比對的 `on_tx_match`。
    for dev in [&m.dev, &dev_b] {
        dev.on_tx_match("dmesg", Box::new(|_| b"dmesg\r\nline1\r\nroot@t:/# ".to_vec()));
    }
    let sid0 = m.mgr.resolve("COM0").unwrap();
    let sid1 = m.mgr.resolve("COM1").unwrap();
    let bg = |sid: &str| -> String {
        let mut req = agent_submit("dmesg");
        req.mode = "bg".into();
        let r = rpc(m.mgr.route(sid, SessionRequest::Submit(req), RPC));
        s_of(&r, "cmd_id").expect("cmd_id")
    };
    let tail = |sid: &str, id: &str| -> PyDict {
        rpc(m.mgr.route(
            sid,
            SessionRequest::BackgroundResult {
                cmd_id: id.to_string(),
                from_chunk: 0,
                limit: 200,
            },
            RPC,
        ))
    };
    let wait_done = |_sid: &str, id: &str| {
        wait_until(&format!("{id} 完成"), || {
            m.reg
                .get(id)
                .and_then(|c| c.get("done_at").cloned())
                .is_some_and(|v| !matches!(v, PyValue::Null))
        });
    };
    let mut ids1 = Vec::new();
    for _ in 0..64 {
        let id = bg(&sid1);
        wait_done(&sid1, &id);
        ids1.push(id);
    }
    // capture 於最後活動後 quiet window 才轉 done（`maybe_finalize`，讀取時評估）：推進時鐘並逐筆讀取，
    // 使 COM1 的 64 筆在索引中皆為已終結（可淘汰）。
    m.clock.advance(Duration::from_secs(120));
    for id in &ids1 {
        assert_eq!(s_of(&tail(&sid1, id), "status").as_deref(), Some("done"));
    }
    let id0 = bg(&sid0);
    wait_done(&sid0, &id0);
    wait_until("COM1 最舊 capture 被淘汰", || {
        s_of(&tail(&sid1, &ids1[0]), "error_code").as_deref() == Some("CMD_NOT_FOUND")
    });
    assert_eq!(tail(&sid1, &ids1[1])["ok"], PyValue::Bool(true), "只淘汰最舊一筆");
    assert_eq!(tail(&sid0, &id0)["ok"], PyValue::Bool(true), "新完成者保留");
}

#[test]
fn global_bg_capture_eviction_owner_routing_ignores_alias_collision() {
    // 同上一案，另加 alias 衝突（sol-backfill p5 #7）。原註：審查 d2d29a6_7af6567 3b5927a #1（F28 manager 接線）：跨 session 淘汰須經 actor `register` →
    // `ManagerNote::EvictCapture` → manager 依 COM 解析擁有者 → `ControlEvent::EvictCapture`。
    // Python `SessionManager._background` 為全域 dict，`_evict_background_locked` 直接 `pop`，之後
    // `result_tail` 回 CMD_NOT_FOUND。輸入：COM1 先完成 64 筆 bg capture，COM0 再完成 1 筆 → 淘汰 COM1 最舊者。
    let yaml = manager_yaml(&[
        &t_target("COM0", 1, "/s/by-id/usb-A"),
        &t_target("COM1", 2, "/s/by-id/usb-B"),
    ]);
    let m = ManagerRig::with(&yaml, LoadedState::default(), Some(("/s/by-id/usb-A", "/dev/ttyA")));
    let dev_b = m.add_device_at("/s/by-id/usb-B", "/dev/ttyB");
    install_shell_replies(&dev_b);
    m.mgr.update_devices(&m.snapshot());
    m.mgr.bootstrap_attach();
    m.wait_state("COM0", "READY");
    m.wait_state("COM1", "READY");
    // `on_tx_reply` 只推一次；65 筆命令改用逐行比對的 `on_tx_match`。
    for dev in [&m.dev, &dev_b] {
        dev.on_tx_match("dmesg", Box::new(|_| b"dmesg\r\nline1\r\nroot@t:/# ".to_vec()));
    }
    let sid0 = m.mgr.resolve("COM0").unwrap();
    let sid1 = m.mgr.resolve("COM1").unwrap();
    // sol-backfill p5 #7：COM0 的 alias 設為 "COM1"——使用者 selector 規則會讓 "COM1" 先命中 COM0；
    // 淘汰通知攜帶的 COM 是內部 owner 身分，必須以 `profile.com` 精確比對，不得走 alias-aware 解析。
    assert!(is_ok(&rpc(m.mgr.alias_set(&sid0, "COM1"))));
    let bg = |sid: &str| -> String {
        let mut req = agent_submit("dmesg");
        req.mode = "bg".into();
        let r = rpc(m.mgr.route(sid, SessionRequest::Submit(req), RPC));
        s_of(&r, "cmd_id").expect("cmd_id")
    };
    let tail = |sid: &str, id: &str| -> PyDict {
        rpc(m.mgr.route(
            sid,
            SessionRequest::BackgroundResult {
                cmd_id: id.to_string(),
                from_chunk: 0,
                limit: 200,
            },
            RPC,
        ))
    };
    let wait_done = |_sid: &str, id: &str| {
        wait_until(&format!("{id} 完成"), || {
            m.reg
                .get(id)
                .and_then(|c| c.get("done_at").cloned())
                .is_some_and(|v| !matches!(v, PyValue::Null))
        });
    };
    let mut ids1 = Vec::new();
    for _ in 0..64 {
        let id = bg(&sid1);
        wait_done(&sid1, &id);
        ids1.push(id);
    }
    // capture 於最後活動後 quiet window 才轉 done（`maybe_finalize`，讀取時評估）：推進時鐘並逐筆讀取，
    // 使 COM1 的 64 筆在索引中皆為已終結（可淘汰）。
    m.clock.advance(Duration::from_secs(120));
    for id in &ids1 {
        assert_eq!(s_of(&tail(&sid1, id), "status").as_deref(), Some("done"));
    }
    let id0 = bg(&sid0);
    wait_done(&sid0, &id0);
    wait_until("COM1 最舊 capture 被淘汰", || {
        s_of(&tail(&sid1, &ids1[0]), "error_code").as_deref() == Some("CMD_NOT_FOUND")
    });
    assert_eq!(tail(&sid1, &ids1[1])["ok"], PyValue::Bool(true), "只淘汰最舊一筆");
    assert_eq!(tail(&sid0, &id0)["ok"], PyValue::Bool(true), "新完成者保留");
}

/// sol-backfill p5 #2：產品 executor 不得在 actor 執行緒同步等開埠。開埠卡住時 actor 仍須回覆查詢，
/// pending-open 的 `device.release` 須於 3 s 排空期限回 `DRAIN_STALLED`（CoreHarness 版見
/// drain_release.rs::release_during_pending_open_drain_stalled；本案經 ManagerExec 實際路徑）。
#[test]
fn product_executor_blocked_open_keeps_actor_responsive_and_release_drain_stalled() {
    let m = ManagerRig::single_target(SHELL_PROFILE);
    m.serial.block_open("/dev/ttyOLD");
    m.mgr.update_devices(&m.snapshot());
    m.mgr.bootstrap_attach();
    wait_until("open 進入閘門", || m.serial.open_attempts("/dev/ttyOLD") >= 1);
    let sid = m.mgr.resolve("COM0").expect("COM0 session");
    let state = m
        .mgr
        .route(&sid, SessionRequest::GetState, Duration::from_millis(800))
        .expect("開埠卡住時 actor 仍須回覆 GetState");
    assert_eq!(
        s_of(&session_field(&state), "state").as_deref(),
        Some("ATTACHING"),
        "{state:?}"
    );

    let mgr = m.mgr.clone();
    let sid2 = sid.clone();
    let (done_tx, done_rx) = crossbeam_channel::bounded(1);
    std::thread::spawn(move || {
        let r = mgr.route(
            &sid2,
            SessionRequest::Release {
                source: "agent".to_string(),
                reason: PyValue::Null,
            },
            Duration::from_secs(10),
        );
        let _ = done_tx.send(r);
    });
    // 等 release 進入 actor（排空計時器已排程）後推進 3 s 期限。
    std::thread::sleep(Duration::from_millis(200));
    m.clock.advance(Duration::from_millis(3001));
    let reply = done_rx
        .recv_timeout(Duration::from_secs(5))
        .expect("release 須於排空期限內回覆")
        .expect("release 回 dict");
    assert_eq!(code_of(&session_field(&reply)).or_else(|| code_of(&reply)), Some("DRAIN_STALLED".to_string()), "{reply:?}");
    m.serial.release_open("/dev/ttyOLD");
}

/// sol-backfill p5 #2：release 持久化暫停鉤子卡住 commit 時，actor 仍須回覆查詢（Persist 不得在 actor 執行緒同步等）。
#[test]
fn product_executor_blocked_release_commit_keeps_actor_responsive() {
    struct BlockingPoint {
        entered: crossbeam_channel::Sender<()>,
        release: crossbeam_channel::Receiver<()>,
    }
    impl sw_core::session_manager::PersistObserver for BlockingPoint {
        fn before_release_commit(&self) -> Result<(), sw_pal::PalError> {
            let _ = self.entered.send(());
            let _ = self.release.recv_timeout(Duration::from_secs(20));
            Ok(())
        }
        fn after_commit(&self, _release: bool) {}
    }
    let m = ManagerRig::single_target(SHELL_PROFILE).ready();
    let (entered_tx, entered_rx) = crossbeam_channel::bounded(1);
    let (release_tx, release_rx) = crossbeam_channel::bounded(1);
    m.mgr.set_persist_observer(std::sync::Arc::new(BlockingPoint {
        entered: entered_tx,
        release: release_rx,
    }));
    let sid = m.mgr.resolve("COM0").expect("COM0 session");
    let mgr = m.mgr.clone();
    let sid2 = sid.clone();
    let (done_tx, done_rx) = crossbeam_channel::bounded(1);
    std::thread::spawn(move || {
        let r = mgr.route(
            &sid2,
            SessionRequest::Release {
                source: "agent".to_string(),
                reason: PyValue::Null,
            },
            Duration::from_secs(20),
        );
        let _ = done_tx.send(r);
    });
    entered_rx
        .recv_timeout(Duration::from_secs(5))
        .expect("release 持久化進入暫停點");
    let state = m
        .mgr
        .route(&sid, SessionRequest::GetState, Duration::from_millis(800));
    let _ = release_tx.send(());
    let state = state.expect("release commit 卡住時 actor 仍須回覆 GetState");
    assert!(is_ok(&state), "{state:?}");
    let reply = done_rx
        .recv_timeout(Duration::from_secs(10))
        .expect("release 完成")
        .expect("release 回 dict");
    assert!(is_ok(&reply), "{reply:?}");
}

/// sol P5-5cc4623-1：release 持久化於暫停點卡住時，bind 到離線裝置 B 成功；恢復後不得回放舊 `Released`，
/// 重啟後 COM0 保留 binding B 且非 RELEASED（Python `_save_release_state` 暫停後重建當前快照）。
#[test]
fn release_commit_paused_then_bind_keeps_binding_without_stale_released_row() {
    struct BlockingPoint {
        entered: crossbeam_channel::Sender<()>,
        release: crossbeam_channel::Receiver<()>,
    }
    impl sw_core::session_manager::PersistObserver for BlockingPoint {
        fn before_release_commit(&self) -> Result<(), sw_pal::PalError> {
            let _ = self.entered.send(());
            let _ = self.release.recv_timeout(Duration::from_secs(20));
            Ok(())
        }
        fn after_commit(&self, _release: bool) {}
    }
    const OFFLINE_B: &str = "/dev/serial/by-id/usb-OFFLINE-B";
    let m = ManagerRig::single_target(SHELL_PROFILE).ready();
    let (entered_tx, entered_rx) = crossbeam_channel::bounded(1);
    let (release_tx, release_rx) = crossbeam_channel::bounded(1);
    m.mgr.set_persist_observer(std::sync::Arc::new(BlockingPoint {
        entered: entered_tx,
        release: release_rx,
    }));
    let sid = m.mgr.resolve("COM0").expect("COM0 session");
    let mgr = m.mgr.clone();
    let sid2 = sid.clone();
    let (done_tx, done_rx) = crossbeam_channel::bounded(1);
    std::thread::spawn(move || {
        let r = mgr.route(
            &sid2,
            SessionRequest::Release {
                source: "agent".to_string(),
                reason: PyValue::Null,
            },
            Duration::from_secs(20),
        );
        let _ = done_tx.send(r);
    });
    entered_rx
        .recv_timeout(Duration::from_secs(5))
        .expect("release 持久化進入暫停點");
    let bound = m.mgr.bind("COM0", OFFLINE_B);
    let _ = release_tx.send(());
    let bound = rpc(bound);
    assert!(is_ok(&bound), "{bound:?}");
    let reply = done_rx
        .recv_timeout(Duration::from_secs(10))
        .expect("release 完成")
        .expect("release 回 dict");
    assert!(is_ok(&reply), "{reply:?}");
    let saved = m.load_state_now();
    assert!(
        saved.bindings.values().any(|v| v == OFFLINE_B),
        "binding B 須落盤：{:?}",
        saved.bindings
    );
    assert!(
        !saved.released.contains_key(&sid),
        "已改綁的 session 不得留下舊 released 列（重啟會再次成為 RELEASED）：{:?}",
        saved.released
    );
    // rulings/p5.md P5-5cc4623-1：重啟後實體化——COM0 依 binding B 建立、不得因舊 delta 成為 RELEASED。
    let r = m.restart();
    r.mgr.update_devices(&r.snapshot());
    r.mgr.bootstrap_attach();
    let after = r.session("COM0");
    assert_ne!(s_of(&after, "state").as_deref(), Some("RELEASED"), "{after:?}");
    assert_eq!(s_of(&after, "device_by_id").as_deref(), Some(OFFLINE_B), "{after:?}");
    assert!(r.load_state_now().released.is_empty(), "重啟後不得重新出現 released 列");
}

/// sol-backfill p5 #4：Python `SessionManager.__init__` 尾端 `_save_state()` 失敗（EACCES／ENOSPC）即拋出，
/// daemon rc 1、尚未掃描／attach。Rust 須以 `startup_error()` 回報 Python 例外末行，不得只 warn 後繼續。
#[test]
fn startup_save_failure_reports_python_exception_line() {
    for (err, prefix) in [
        (
            PalError::from_errno(13),
            "PermissionError: [Errno 13] Permission denied",
        ),
        (
            PalError::from_errno(28),
            "OSError: [Errno 28] No space left on device",
        ),
    ] {
        let (mgr, serial) = ManagerRig::startup_commit_failure(SHELL_PROFILE, err);
        let msg = mgr.startup_error().expect("啟動存檔失敗須回報 startup_error");
        assert!(msg.starts_with(prefix), "{msg:?}");
        assert_eq!(serial.open_attempts("/dev/ttyOLD"), 0, "啟動存檔失敗不得開埠");
    }
}

/// sol-backfill p5 #3：Python `set_alias_for_session` 更新唯一的 session profile——之後經 actor 的
/// `session.get_state`（以新 alias 解析）與 `session.list` 皆回新 alias，不得只更新 manager 鏡像。
#[test]
fn alias_set_reaches_actor_profile_for_get_state() {
    let m = ManagerRig::single_target(SHELL_PROFILE).ready();
    let sid = m.mgr.resolve("COM0").expect("COM0 session");
    assert!(is_ok(&rpc(m.mgr.alias_set(&sid, "dut"))));
    let via = m.mgr.resolve("dut").expect("新 alias 可解析");
    let state = rpc(m.mgr.route(&via, SessionRequest::GetState, RPC));
    assert_eq!(s_of(&session_field(&state), "alias").as_deref(), Some("dut"), "{state:?}");
    let listed = m.session("COM0");
    assert_eq!(s_of(&listed, "alias").as_deref(), Some("dut"), "{listed:?}");
}

/// wp510 第五次整包審查：佔位交接後正式 session 的 `open_bridge` 停在取預開 handle 之前；回傳放行送端。
fn wp510_review5_pause_open_bridge(m: &ManagerRig) -> crossbeam_channel::Sender<()> {
    let (entered_tx, entered_rx) = crossbeam_channel::bounded(1);
    let (go_tx, go_rx) = crossbeam_channel::bounded::<()>(1);
    m.mgr.set_open_bridge_pre_take_hook_for_test(Box::new(move || {
        let _ = entered_tx.send(());
        let _ = go_rx.recv_timeout(Duration::from_secs(10));
    }));
    let sid = m.mgr.resolve("gone-template:COM3").expect("佔位 session");
    let response = rpc(m.mgr.attach_released_placeholder(&sid, false));
    assert!(is_ok(&response), "device.attach 應接受在線佔位：{response:?}");
    entered_rx
        .recv_timeout(Duration::from_secs(5))
        .expect("正式 session 的 open_bridge 應停在接手預開 handle 之前");
    go_tx
}

fn wp510_review5_bool(d: &PyDict, key: &str) -> Option<bool> {
    match d.get(key) {
        Some(PyValue::Bool(b)) => Some(*b),
        _ => None,
    }
}

/// wp510 第五次整包審查 High：佔位交接已建立正式 session、預開 handle 尚未由 `open_bridge` 接手時 shutdown，
/// 關閉回條為 `closed:false`。Python 的預開 handle 在正式 bridge 接手前仍屬 attach 作業，`_finish_attach_op`
/// 關閉失敗即保留作業（`origin/main:sw_core/session_manager.py:4322-4332`），`shutdown_drain` 末段把仍未完成的
/// 作業列入 stalled（`:1231-1239`）。之後的一般開埠錯誤（daemon stopping）不得清除 stall 而回報排空成功，
/// 阻擋亦須持續到確認關閉為止。
#[test]
fn wp510_review5_shutdown_before_bridge_takes_preopened_fail_close_reports_stalled() {
    const BY_ID: &str = "/s/by-id/usb-G";
    const REAL: &str = "/dev/ttyG";
    let m = wp510_b_placeholder_rig(false, false);
    let device = m.device(REAL);
    m.mgr.update_devices(&m.snapshot());
    let go = wp510_review5_pause_open_bridge(&m);
    device.fail_close(PalError::io(5, "[Errno 5] Input/output error"));

    let manager = m.mgr.clone();
    let shutdown = std::thread::spawn(move || manager.shutdown(Duration::from_millis(1500)));
    wait_until("shutdown 已送達正式 session", || {
        m.state_of("t:COM3").as_deref() == Some("RECOVERING")
    });
    go.send(()).expect("放行 open_bridge");
    let drained = shutdown.join().expect("shutdown thread 不應 panic");

    let attach_key = format!("attach:{BY_ID}");
    assert!(
        drained.iter().any(|(key, ok)| key == &attach_key && !ok),
        "未確認關閉的預開 handle 必須列入 shutdown stalled：{drained:?}"
    );
    assert!(
        drained.iter().any(|(key, ok)| key == "t:COM3" && !ok),
        "daemon stopping 的開埠錯誤不得把正式 session 計為已排空：{drained:?}"
    );
    assert!(device.is_open(), "closed:false 表示 handle 仍開啟");
    assert_eq!(device.close_count(), 1, "handle 只關閉一次");
    assert!(
        m.mgr.has_inflight_attach_for_test(BY_ID),
        "未確認關閉的 handle 必須持續阻擋"
    );
    let session = m.session("t:COM3");
    assert_eq!(wp510_review5_bool(&session, "drain_stalled"), Some(true), "{session:?}");
    let again = m.mgr.shutdown(Duration::from_millis(50));
    assert!(
        again.iter().any(|(key, ok)| key == &attach_key && !ok),
        "重複 shutdown 仍須回報未收斂：{again:?}"
    );
    assert_eq!(device.open_count(), 1, "停機後不得重新開埠");
}

/// wp510 第五次整包審查 High（release 排空）：同一窗口改由 release 等待開埠收斂，隨後 stopping 使 `open_bridge`
/// 關閉預開 handle 失敗。Python release 以 attach 作業的 `done` 判定，關閉失敗不設 `done`，回 DRAIN_STALLED
/// （`origin/main:sw_core/session_manager.py:3192-3204`、`:4322-4332`）；不得以後續一般開埠錯誤完成 release。
#[test]
fn wp510_review5_release_waiting_bridge_open_fail_close_reports_drain_stalled() {
    const BY_ID: &str = "/s/by-id/usb-G";
    const REAL: &str = "/dev/ttyG";
    let m = wp510_b_placeholder_rig(false, false);
    let device = m.device(REAL);
    m.mgr.update_devices(&m.snapshot());
    let go = wp510_review5_pause_open_bridge(&m);
    device.fail_close(PalError::io(5, "[Errno 5] Input/output error"));
    let formal = m.mgr.resolve("t:COM3").expect("正式 session");

    let manager = m.mgr.clone();
    let release_sid = formal.clone();
    let release = std::thread::spawn(move || wp510_release(&manager, &release_sid));
    wait_until("release 已送達正式 session", || {
        m.state_of("t:COM3").as_deref() == Some("RECOVERING")
    });
    m.mgr.begin_shutdown();
    go.send(()).expect("放行 open_bridge");
    let reply = rpc(release.join().expect("release thread 不應 panic"));

    assert_eq!(code_of(&reply).as_deref(), Some("DRAIN_STALLED"), "{reply:?}");
    assert_eq!(wp510_review5_bool(&reply, "drain_stalled"), Some(true), "{reply:?}");
    assert_ne!(
        m.state_of("t:COM3").as_deref(),
        Some("RELEASED"),
        "未確認關閉的 handle 不得完成 release"
    );
    assert!(device.is_open(), "closed:false 表示 handle 仍開啟");
    let custody = m.mgr.custody_for_test(BY_ID).expect("custody entry");
    assert!(matches!(custody.phase, CustodyPhaseSnapshot::CloseFailed { .. }));
    assert!(m.mgr.unconfirmed_close_for_test(BY_ID));
    assert!(
        !m.load_state_now().released.contains_key(&formal),
        "release 未收斂不得持久化 RELEASED"
    );
}

/// wp510 第五次整包審查 High（盤點：正式 bridge 套用 UART 設定失敗）：`open_bridge` 已取得預開 handle，configure
/// 失敗且關閉回條為 `closed:false`，沒有 stop 在途。Python `_attach_by_id` 把預開作業移入 `_active_attach_ops`
/// （`origin/main:sw_core/session_manager.py:4852-4854`）後，中止路徑經 `_finish_attach_op` 關閉，失敗即保留作業
/// （`:4322-4332`）；後續 release 等待該作業至期限回 DRAIN_STALLED（`:3192-3204`），shutdown 列為 stalled（`:1231-1239`）。
#[test]
fn wp510_review5_configure_failure_fail_close_keeps_handle_in_drain() {
    const BY_ID: &str = "/s/by-id/usb-G";
    const REAL: &str = "/dev/ttyG";
    let m = wp510_b_placeholder_rig(false, false);
    let device = m.device(REAL);
    m.mgr.update_devices(&m.snapshot());
    let go = wp510_review5_pause_open_bridge(&m);
    device.fail_configure(PalError::io(5, "[Errno 5] Input/output error"));
    device.fail_close(PalError::io(5, "[Errno 5] Input/output error"));
    go.send(()).expect("放行 open_bridge");
    wait_until("正式 session 開埠失敗", || {
        m.state_of("t:COM3").as_deref() == Some("DETACHED")
    });

    assert!(device.is_open(), "closed:false 表示 handle 仍開啟");
    let custody = m.mgr.custody_for_test(BY_ID).expect("custody entry");
    assert!(matches!(custody.phase, CustodyPhaseSnapshot::CloseFailed { .. }), "CloseFailed");
    assert!(m.mgr.unconfirmed_close_for_test(BY_ID), "unconfirmed_close");
    let started = Instant::now();
    let reply = rpc(wp510_release(&m.mgr, "t:COM3"));
    assert_eq!(code_of(&reply).as_deref(), Some("DRAIN_STALLED"), "{reply:?}");
    assert!(started.elapsed() >= Duration::from_millis(2500), "release 等待作業至期限");
    assert_ne!(m.state_of("t:COM3").as_deref(), Some("RELEASED"));
    let drained = m.mgr.shutdown(Duration::from_millis(100));
    assert!(
        drained.iter().any(|(key, ok)| key == &format!("attach:{BY_ID}") && !ok),
        "未確認關閉的 handle 必須列入 shutdown stalled：{drained:?}"
    );
    assert_eq!(device.open_count(), 1, "不得重新開埠");
}

/// wp510 第六輪增量審查 P8-fdc1413-1（High）：佔位交接的 attach worker 送出 `DeviceAttached` 後進入收尾，停在
/// `finish_attach_op` 取得 manager 鎖之前；此時正式 session 的 `open_bridge` 套用 UART 設定失敗、關閉回條
/// `closed:false`，`retain_unconfirmed_close` 把同一作業標為未確認關閉。worker 恢復後必須以鎖內的最新標記判定，
/// 保留作業。Python `_finish_attach_op` 在作業鎖內判定 close 結果，失敗即保留作業、不設 `done`
/// （`origin/main:sw_core/session_manager.py:4322-4332`）；release 等 `done` 逾期回 DRAIN_STALLED（`:3192-3204`），
/// shutdown 把未完成作業列入 stalled（`:1231-1239`）。
#[test]
fn wp510_review6_finish_attach_op_rereads_close_failed_under_lock() {
    const BY_ID: &str = "/s/by-id/usb-G";
    const REAL: &str = "/dev/ttyG";
    let m = wp510_b_placeholder_rig(false, false);
    let device = m.device(REAL);
    m.mgr.update_devices(&m.snapshot());
    let (finish_entered_tx, finish_entered_rx) = crossbeam_channel::bounded(1);
    let (finish_go_tx, finish_go_rx) = crossbeam_channel::bounded::<()>(1);
    m.mgr.set_finish_attach_op_pre_lock_hook_for_test(
        BY_ID,
        Box::new(move || {
            let _ = finish_entered_tx.send(());
            let _ = finish_go_rx.recv_timeout(Duration::from_secs(10));
        }),
    );
    device.fail_configure(PalError::io(5, "[Errno 5] Input/output error"));
    device.fail_close(PalError::io(5, "[Errno 5] Input/output error"));
    let bridge_go = wp510_review5_pause_open_bridge(&m);
    finish_entered_rx
        .recv_timeout(Duration::from_secs(5))
        .expect("attach worker 應停在 finish_attach_op 取鎖之前");

    // open_bridge 在 worker 收尾取鎖前完成 configure 失敗與 closed:false 的關閉（retain 先於 BridgeOpened 回報）。
    bridge_go.send(()).expect("放行 open_bridge");
    wait_until("正式 session 開埠失敗", || {
        m.state_of("t:COM3").as_deref() == Some("DETACHED")
    });
    finish_go_tx.send(()).expect("放行 attach worker 收尾");
    // worker 恢復後只剩取鎖與判定；以有界等待觀察作業是否被移除（保留時於期限後回 false）。
    let removed = m.mgr.wait_attach_idle_for_test(BY_ID, Duration::from_millis(500));

    let inflight_kept = m.mgr.has_inflight_attach_for_test(BY_ID);
    let release = rpc(wp510_release(&m.mgr, "t:COM3"));
    let state_after_release = m.state_of("t:COM3");
    let drained = m.mgr.shutdown(Duration::from_millis(100));
    let attach_key = format!("attach:{BY_ID}");
    let observed = format!(
        "removed={removed} inflight={inflight_kept} release={release:?} state={state_after_release:?} shutdown={drained:?}"
    );

    assert!(inflight_kept, "未確認關閉的 handle 必須保留 inflight 阻擋：{observed}");
    assert_eq!(code_of(&release).as_deref(), Some("DRAIN_STALLED"), "{observed}");
    assert_eq!(wp510_review5_bool(&release, "drain_stalled"), Some(true), "{observed}");
    assert_ne!(
        state_after_release.as_deref(),
        Some("RELEASED"),
        "未確認關閉的 handle 不得完成 release：{observed}"
    );
    assert!(
        drained.iter().any(|(key, ok)| key == &attach_key && !ok),
        "未確認關閉的 handle 必須列入 shutdown stalled：{observed}"
    );
    assert!(
        !m.load_state_now().released.contains_key("t:COM3"),
        "release 未收斂不得持久化 RELEASED：{observed}"
    );
    assert!(device.is_open(), "closed:false 表示 handle 仍開啟");
    assert_eq!(device.close_count(), 1, "handle 只關閉一次");
    assert_eq!(device.open_count(), 1, "不得重新開埠");
}

/// wp510 第五次整包審查 Medium：佔位交接的 attach worker 建立失敗（`Builder::spawn()` 回錯）。Python
/// `attach_device` 同步呼叫 `_spawn_attach`（`origin/main:sw_core/session_manager.py:3633-3638`），`worker.start()`
/// 的例外穿出 RPC（`:4147-4150`），不會回成功；佔位與持久化阻擋須維持，不可新舊 session 皆不存在。
#[test]
fn wp510_review5_handoff_worker_spawn_failure_returns_error_and_keeps_placeholder() {
    const SID: &str = "gone-template:COM3";
    const BY_ID: &str = "/s/by-id/usb-G";
    const REAL: &str = "/dev/ttyG";
    let m = wp510_b_placeholder_rig(false, false);
    let sid = m.mgr.resolve(SID).expect("佔位 session");
    let device = m.device(REAL);
    m.mgr.update_devices(&m.snapshot());
    let persisted_before = m.load_state_now().released.contains_key(SID);
    assert!(persisted_before, "前提：佔位的 RELEASED 阻擋已持久化");
    m.mgr.fail_next_attach_spawn_for_test();

    let error = m
        .mgr
        .attach_released_placeholder(&sid, false)
        .expect_err("attach worker 建立失敗必須傳回 RPC 錯誤");

    assert_eq!(error.message, "can't start new thread");
    let warnings = m.warnings();
    assert!(
        warnings
            .iter()
            .any(|w| w.starts_with("attach worker 建立失敗（by_id=/s/by-id/usb-G）")),
        "建立失敗必須留下診斷：{warnings:?}"
    );
    wp510_b_assert_placeholder_retained(&m, SID);
    assert_eq!(
        m.load_state_now().released.contains_key(SID),
        persisted_before,
        "持久化阻擋不得因建立失敗改變"
    );
    assert!(m.mgr.resolve("t:COM3").is_none(), "未建立 worker 不得出現正式 session");
    assert!(!device.is_open(), "未交接的 handle 必須關閉");
    assert!(!m.mgr.has_inflight_attach_for_test(BY_ID), "確認關閉後不得殘留阻擋");

    let retry = rpc(m.mgr.attach_released_placeholder(&sid, false));
    assert!(is_ok(&retry), "建立失敗後佔位仍可重新交接：{retry:?}");
    wait_until("t:COM3 正式 session", || m.mgr.resolve("t:COM3").is_some());
}

/// 同上（一般 OSError 分支）：佔位開埠回一般 OSError 時同樣先建立 worker；建立失敗須傳回 RPC 錯誤並保留佔位。
#[test]
fn wp510_review5_oserror_handoff_worker_spawn_failure_returns_error_and_keeps_placeholder() {
    const SID: &str = "gone-template:COM3";
    const BY_ID: &str = "/s/by-id/usb-G";
    const REAL: &str = "/dev/ttyG";
    let m = wp510_b_placeholder_rig(false, false);
    let sid = m.mgr.resolve(SID).expect("佔位 session");
    m.serial.fail_next_open(REAL, wp510_b_permission_error());
    m.mgr.update_devices(&m.snapshot());
    let persisted_before = m.load_state_now().released.contains_key(SID);
    assert!(persisted_before, "前提：佔位的 RELEASED 阻擋已持久化");
    m.mgr.fail_next_attach_spawn_for_test();

    let error = m
        .mgr
        .attach_released_placeholder(&sid, false)
        .expect_err("attach worker 建立失敗必須傳回 RPC 錯誤");

    assert_eq!(error.message, "can't start new thread");
    assert!(
        m.warnings()
            .iter()
            .any(|w| w.starts_with("attach worker 建立失敗（by_id=/s/by-id/usb-G）")),
        "建立失敗必須留下診斷：{:?}",
        m.warnings()
    );
    wp510_b_assert_placeholder_retained(&m, SID);
    assert_eq!(m.load_state_now().released.contains_key(SID), persisted_before);
    assert!(m.mgr.resolve("t:COM3").is_none(), "未建立 worker 不得出現正式 session");
    assert!(!m.mgr.has_inflight_attach_for_test(BY_ID), "無 handle 的作業不得殘留阻擋");
}

/// wp510 第 13 項（開埠 gate）：`device.attach` 已由 actor 發出 `OpenBridge`、`open_bridge` 取預開 handle 之前，by_id 才
/// 登記為未確認關閉。gate 必須於 `open_exclusive` 之前拒絕（零開埠嘗試），回應照 Python `attach_device` 開埠後重驗
/// `_draining_by_ids` 的分支回 SESSION_RECOVERING（`origin/main:sw_core/session_manager.py:3576-3588`、`:3614-3631`）。
#[test]
fn wp510_review7_open_bridge_gate_rejects_late_unconfirmed_close() {
    const BY_ID: &str = "/s/by-id/usb-G";
    const REAL: &str = "/dev/ttyG";
    let m = wp510_b_placeholder_rig(false, false);
    let device = m.device(REAL);
    m.mgr.update_devices(&m.snapshot());
    device.fail_configure(PalError::io(5, "[Errno 5] Input/output error"));
    let sid = m.mgr.resolve("gone-template:COM3").expect("佔位 session");
    assert!(is_ok(&rpc(m.mgr.attach_released_placeholder(&sid, false))));
    wait_until("正式 session 套用設定失敗後退回 DETACHED", || {
        m.state_of("t:COM3").as_deref() == Some("DETACHED")
    });
    assert!(m.mgr.wait_attach_idle_for_test(BY_ID, Duration::from_secs(5)));
    assert!(!m.mgr.unconfirmed_close_for_test(BY_ID), "closed:true 不登記阻擋");
    let attempts = m.serial.open_attempts(REAL);

    let (entered_tx, entered_rx) = crossbeam_channel::bounded(1);
    let (go_tx, go_rx) = crossbeam_channel::bounded::<()>(1);
    m.mgr.set_open_bridge_pre_take_hook_for_test(Box::new(move || {
        let _ = entered_tx.send(());
        let _ = go_rx.recv_timeout(Duration::from_secs(10));
    }));
    let formal = m.mgr.resolve("t:COM3").expect("正式 session");
    let manager = m.mgr.clone();
    let attach = std::thread::spawn(move || {
        manager.route(&formal, SessionRequest::DeviceAttach { force: true }, RPC)
    });
    entered_rx
        .recv_timeout(Duration::from_secs(5))
        .expect("device.attach 應進入 open_bridge");
    m.mgr.mark_unconfirmed_close_for_test(BY_ID);
    go_tx.send(()).expect("放行 open_bridge");
    let reply = rpc(attach.join().expect("attach thread 不應 panic"));

    assert_eq!(code_of(&reply).as_deref(), Some("SESSION_RECOVERING"), "{reply:?}");
    assert_eq!(wp510_review5_bool(&reply, "drain_stalled"), Some(false), "{reply:?}");
    assert_eq!(m.serial.open_attempts(REAL), attempts, "gate 必須在 open_exclusive 之前拒絕");
    assert_eq!(m.state_of("t:COM3").as_deref(), Some("DETACHED"));
}

/// B-1 獨立探針：佔位 attach 接手至正式 session 並到 READY，release 後帳本正確結算移除，不殘留 Opening。
#[test]
fn test_b1_placeholder_attach_handoff_and_release_settles_custody() {
    const BY_ID: &str = "/s/by-id/usb-G";
    const PLACEHOLDER_SID: &str = "gone-template:COM3";
    const FORMAL_SID: &str = "t:COM3";

    let m = wp510_b_placeholder_rig(false, false);
    m.mgr.update_devices(&m.snapshot());
    let placeholder_actor = m.mgr.actor_id_for_test(PLACEHOLDER_SID).expect("佔位 actor 身分");

    // 1. 對 gone-template:COM3 執行佔位 attach
    let res = m.mgr.attach_released_placeholder(PLACEHOLDER_SID, false).expect("attach_released_placeholder ok");
    assert_eq!(res.get("ok"), Some(&PyValue::Bool(true)));

    // 2. 等待正式 t:COM3 到 ATTACHED / READY
    wait_until("t:COM3 正式 session", || m.mgr.resolve(FORMAL_SID).is_some());
    wait_until("COM3 正式 bridge", || {
        matches!(m.state_of("COM3").as_deref(), Some("ATTACHED" | "READY"))
    });

    // 檢查步驟 2：正式 session 接手後，帳本不得殘留舊佔位的 Opening 紀錄
    let custody_step2 = m.mgr.custody_for_test(BY_ID);
    if let Some(c) = &custody_step2 {
        assert_ne!(
            c.owner,
            sw_core::custody::CustodyOwner::Session {
                actor: placeholder_actor,
            },
            "正式 session 接手後帳本 owner 不得仍為舊佔位 session"
        );
    }

    // 3. 執行 release
    let rel_res = m.mgr.route(
        FORMAL_SID,
        SessionRequest::Release {
            source: "cli".into(),
            reason: PyValue::Null,
        },
        Duration::from_secs(5),
    ).expect("release 成功");
    assert_eq!(rel_res.get("ok"), Some(&PyValue::Bool(true)));

    // 4. 驗證 release 成功後帳本完全收尾（C3）
    let custody_step3 = m.mgr.custody_for_test(BY_ID);
    assert!(custody_step3.is_none(), "成功 release 後帳本紀錄須被結算移除，實際為：{custody_step3:?}");
}

fn wp510_b_custom_placeholder_rig(sid: &str, by_id: &str, real: &str) -> ManagerRig {
    let mut loaded = LoadedState::default();
    loaded.released.insert(sid.to_string(), released_row(by_id));
    loaded
        .bindings
        .insert(sid.to_string(), "/s/by-id/old-override".to_string());
    let mut alias = PyDict::new();
    alias.insert("session_id", PyValue::from(sid));
    loaded.aliases.insert(format!("retired-alias-{sid}"), alias);
    let yaml = manager_yaml(&[]);
    let m = ManagerRig::with(&yaml, loaded, None);
    m.add_device_at(by_id, real);
    install_shell_replies(&m.device(real));
    m
}

static SPAWN_TEST_LOCK: std::sync::Mutex<()> = std::sync::Mutex::new(());

#[test]
fn test_placeholder_attach_next_actor_spawn_failure_closes_handle_and_settles_custody() {
    let _guard = SPAWN_TEST_LOCK.lock().unwrap_or_else(|e| e.into_inner());
    const BY_ID: &str = "/s/by-id/usb-G98";
    const REAL: &str = "/dev/ttyG98";
    const PLACEHOLDER_SID: &str = "gone-template:COM98";

    let m = wp510_b_custom_placeholder_rig(PLACEHOLDER_SID, BY_ID, REAL);
    let device = m.device(REAL);
    m.mgr.update_devices(&m.snapshot());

    sw_core::actor::inject_actor_spawn_fail_for("t:COM98");
    let res = m.mgr.attach_released_placeholder(PLACEHOLDER_SID, false).expect("attach_released_placeholder ok");
    assert_eq!(res.get("ok"), Some(&PyValue::Bool(true)));

    // 等待 attach 作業收尾
    assert!(m.mgr.wait_attach_idle_for_test(BY_ID, Duration::from_secs(5)));
    sw_core::actor::inject_actor_spawn_fail_clear();

    // 斷言：已開 handle 必須明確關閉，且只關閉 1 次
    assert_eq!(device.close_count(), 1, "actor 建構失敗時已開 handle 必須明確關閉 1 次");
    assert!(!device.is_open(), "關閉成功後 handle 不得保持開啟");
    assert!(m.mgr.session_ids().is_empty(), "建構失敗不得留下 session");
    let c = m.mgr.custody_for_test(BY_ID);
    assert!(c.is_none(), "關閉成功後帳本紀錄須被結算移除，實際為：{c:?}");
}

#[test]
fn test_placeholder_attach_next_actor_spawn_failure_close_failed_enters_unconfirmed() {
    let _guard = SPAWN_TEST_LOCK.lock().unwrap_or_else(|e| e.into_inner());
    const BY_ID: &str = "/s/by-id/usb-G99";
    const REAL: &str = "/dev/ttyG99";
    const PLACEHOLDER_SID: &str = "gone-template:COM99";

    let m = wp510_b_custom_placeholder_rig(PLACEHOLDER_SID, BY_ID, REAL);
    let device = m.device(REAL);
    m.mgr.update_devices(&m.snapshot());
    device.fail_close(PalError::io(5, "close failed"));

    sw_core::actor::inject_actor_spawn_fail_for("t:COM99");
    let res = m.mgr.attach_released_placeholder(PLACEHOLDER_SID, false).expect("attach_released_placeholder ok");
    assert_eq!(res.get("ok"), Some(&PyValue::Bool(true)));

    // 等待 attach 作業收尾
    assert!(m.mgr.wait_attach_idle_for_test(BY_ID, Duration::from_secs(5)));
    sw_core::actor::inject_actor_spawn_fail_clear();

    // 斷言：嘗試關閉 1 次，但 closed:false，進 unconfirmed 且帳本為 CloseFailed
    assert_eq!(device.close_count(), 1, "actor 建構失敗時已開 handle 必須嘗試關閉 1 次");
    assert!(device.is_open(), "關閉失敗後 handle 仍保持開啟");
    assert!(m.mgr.session_ids().is_empty(), "建構失敗不得留下 session");
    let c = m.mgr.custody_for_test(BY_ID);
    assert!(matches!(c.as_ref().map(|x| &x.phase), Some(sw_core::custody::CustodyPhaseSnapshot::CloseFailed { .. })), "關閉失敗帳本須為 CloseFailed：{c:?}");
    assert!(m.mgr.unconfirmed_close_for_test(BY_ID), "關閉失敗須進入未確認集合");
}

#[test]
fn test_empty_manager_shutdown_drop_releases_dependencies() {
    let _guard = SPAWN_TEST_LOCK.lock().unwrap_or_else(|e| e.into_inner());
    let m = ManagerRig::templates_only();
    let weak_serial = std::sync::Arc::downgrade(&m.serial);
    let _ = m.mgr.shutdown(Duration::from_millis(100));
    drop(m);
    std::thread::sleep(Duration::from_millis(500));
    assert!(
        weak_serial.upgrade().is_none(),
        "manager drop 後 serial 依賴應被釋放，目前強引用未歸零"
    );
}

#[test]
fn test_placeholder_attach_bridge_opened_send_failure_close_failed_retains_block() {
    let _guard = SPAWN_TEST_LOCK.lock().unwrap_or_else(|e| e.into_inner());
    const BY_ID: &str = "/s/by-id/usb-G98";
    const REAL: &str = "/dev/ttyG98";
    const PLACEHOLDER_SID: &str = "gone-template:COM98";

    let m = wp510_b_custom_placeholder_rig(PLACEHOLDER_SID, BY_ID, REAL);
    let device = m.device(REAL);
    m.mgr.update_devices(&m.snapshot());
    device.fail_close(PalError::io(5, "close failed"));

    let mgr = m.mgr.clone();
    let (entered_tx, entered_rx) = crossbeam_channel::bounded::<()>(1);
    let (resume_tx, resume_rx) = crossbeam_channel::bounded::<()>(1);

    mgr.set_open_bridge_post_take_hook_for_test(Box::new(move || {
        let _ = entered_tx.send(());
        let _ = resume_rx.recv();
    }));

    let res = m.mgr.attach_released_placeholder(PLACEHOLDER_SID, false).expect("attach_released_placeholder ok");
    assert_eq!(res.get("ok"), Some(&PyValue::Bool(true)));

    // 等待進入 post_take_hook
    assert!(entered_rx.recv_timeout(Duration::from_secs(3)).is_ok());

    // 此時 preopened 已被取出，正式 bridge 正準備開啟。
    // 注入 executor 回呼 unwind，使 actor 在事件處理的 catch_unwind 外退出
    let resolved_sid = m.mgr.resolve("COM98").expect("session 存在");
    m.mgr.set_executor_unwind_hook_for_test(&resolved_sid, Box::new(|| {
        panic!("測試注入的 executor unwind（catch_unwind 之外）");
    }));
    if let Some(ctrl) = m.mgr.actor_control_for_test(&resolved_sid) {
        let (tx, _rx) = crossbeam_channel::bounded(1);
        let _ = ctrl.send(sw_core::mailbox::ControlEvent::Request {
            req: sw_core::session_core::SessionRequest::GetState,
            reply: tx,
        });
    }
    std::thread::sleep(Duration::from_millis(100));

    // 放行開埠工作，此時 actor 已退出，BridgeOpened 發送必失敗
    let _ = resume_tx.send(());

    // 等待回收者結算到帳本終態（CloseFailed）：關閉計數遞增與結算之間有時間差，不以關閉計數為同步點。
    let t0 = Instant::now();
    while t0.elapsed() < Duration::from_secs(3) {
        let settled = matches!(
            m.mgr.custody_for_test(BY_ID).map(|c| c.phase),
            Some(sw_core::custody::CustodyPhaseSnapshot::CloseFailed { .. })
        );
        if settled {
            break;
        }
        std::thread::sleep(Duration::from_millis(20));
    }

    // 斷言：嘗試關閉 1 次，但 closed:false，進 unconfirmed 且帳本保留 CloseFailed，阻擋後續開埠
    assert_eq!(device.close_count(), 1, "送達失敗時已開 handle 必須嘗試關閉 1 次");
    assert!(device.is_open(), "關閉失敗後 handle 仍保持開啟");
    let c = m.mgr.custody_for_test(BY_ID);
    assert!(matches!(c.as_ref().map(|x| &x.phase), Some(sw_core::custody::CustodyPhaseSnapshot::CloseFailed { .. })), "送達失敗且關閉失敗時帳本須保留 CloseFailed：{c:?}");
    assert!(m.mgr.unconfirmed_close_for_test(BY_ID), "關閉失敗須進入未確認集合阻擋開埠");
}

#[test]
fn test_placeholder_attach_bridge_opened_send_failure_closed_settles() {
    let _guard = SPAWN_TEST_LOCK.lock().unwrap_or_else(|e| e.into_inner());
    const BY_ID: &str = "/s/by-id/usb-G97";
    const REAL: &str = "/dev/ttyG97";
    const PLACEHOLDER_SID: &str = "gone-template:COM97";

    let m = wp510_b_custom_placeholder_rig(PLACEHOLDER_SID, BY_ID, REAL);
    let device = m.device(REAL);
    m.mgr.update_devices(&m.snapshot());

    let mgr = m.mgr.clone();
    let (entered_tx, entered_rx) = crossbeam_channel::bounded::<()>(1);
    let (resume_tx, resume_rx) = crossbeam_channel::bounded::<()>(1);

    mgr.set_open_bridge_post_take_hook_for_test(Box::new(move || {
        let _ = entered_tx.send(());
        let _ = resume_rx.recv();
    }));

    let res = m.mgr.attach_released_placeholder(PLACEHOLDER_SID, false).expect("attach_released_placeholder ok");
    assert_eq!(res.get("ok"), Some(&PyValue::Bool(true)));

    // 等待進入 post_take_hook
    assert!(entered_rx.recv_timeout(Duration::from_secs(3)).is_ok());

    // 此時 preopened 已被取出，正式 bridge 正準備開啟。
    // 注入 executor 回呼 unwind，使 actor 在事件處理的 catch_unwind 外退出
    let resolved_sid = m.mgr.resolve("COM97").expect("session 存在");
    m.mgr.set_executor_unwind_hook_for_test(&resolved_sid, Box::new(|| {
        panic!("測試注入的 executor unwind（catch_unwind 之外）");
    }));
    if let Some(ctrl) = m.mgr.actor_control_for_test(&resolved_sid) {
        let (tx, _rx) = crossbeam_channel::bounded(1);
        let _ = ctrl.send(sw_core::mailbox::ControlEvent::Request {
            req: sw_core::session_core::SessionRequest::GetState,
            reply: tx,
        });
    }
    std::thread::sleep(Duration::from_millis(100));

    // 放行開埠工作，此時 actor 已退出，BridgeOpened 發送必失敗
    let _ = resume_tx.send(());

    // 等待回收者結算到帳本終態（紀錄移除）：關閉計數遞增與結算之間有時間差，不以關閉計數為同步點。
    let t0 = Instant::now();
    while t0.elapsed() < Duration::from_secs(3) {
        if device.close_count() >= 1 && m.mgr.custody_for_test(BY_ID).is_none() {
            break;
        }
        std::thread::sleep(Duration::from_millis(20));
    }

    // 斷言：嘗試關閉 1 次，closed:true，帳本紀錄結算並移除，未確認集合不含該 by_id
    assert_eq!(device.close_count(), 1, "送達失敗時已開 handle 必須嘗試關閉 1 次");
    assert!(!device.is_open(), "關閉成功後 handle 不得保持開啟");
    let c = m.mgr.custody_for_test(BY_ID);
    assert!(c.is_none(), "關閉成功後帳本紀錄須被結算移除，實際為：{c:?}");
    assert!(!m.mgr.unconfirmed_close_for_test(BY_ID), "關閉成功不得進入未確認集合");
}

/// WP5-1（設計第 5 版 §1.4；S2-PAR-wp510r 第 8 項）：佔位交接後 `tpl-a:COM3` 已建立（出生即接管，紀錄 `Held{Session}`），
/// 開埠工作停在 pre-take 時 shutdown，關閉回 closed:false：stderr 一筆含 by_id 與錯誤的告警、`CloseFailed`、B3 進未確認
/// 集合、shutdown 報告未排空、IoWorker 未啟動、零 TX（Python `:5647-5704`、`:1187`、`:5701`、`:4322-4332`、`:1228-1239`）。
#[test]
fn wp510r_r3_wp5_1_shutdown_pre_take_close_failure_warns() {
    const SID: &str = "gone-template:COM3";
    const BY_ID: &str = "/s/by-id/usb-G";
    const REAL: &str = "/dev/ttyG";
    let m = wp510_b_placeholder_rig(false, false);
    let sid = m.mgr.resolve(SID).expect("佔位 session");
    let device = m.device(REAL);
    m.mgr.update_devices(&m.snapshot());
    let (entered_tx, entered_rx) = crossbeam_channel::bounded(1);
    let (go_tx, go_rx) = crossbeam_channel::bounded::<()>(1);
    m.mgr.set_open_bridge_pre_take_hook_for_test(Box::new(move || {
        let _ = entered_tx.send(());
        let _ = go_rx.recv_timeout(Duration::from_secs(10));
    }));
    let attached = rpc(m.mgr.attach_released_placeholder(&sid, false));
    assert!(is_ok(&attached), "{attached:?}");
    entered_rx
        .recv_timeout(Duration::from_secs(5))
        .expect("正式 session 的開埠工作應停在 pre-take");
    let held = m.mgr.custody_for_test(BY_ID).expect("出生即接管的帳本紀錄");
    assert_eq!(held.phase, CustodyPhaseSnapshot::Held, "{held:?}");
    let tx_before = device.tx_len();
    device.fail_close(sw_pal::traits::PalError::io(5, "[Errno 5] Input/output error"));
    let drained = m.mgr.shutdown(Duration::from_millis(1500));
    let _ = go_tx.send(());
    std::thread::sleep(Duration::from_millis(200));

    assert!(
        drained.iter().any(|(target, ok)| target == &format!("attach:{BY_ID}") && !ok),
        "{drained:?}"
    );
    let custody = m.mgr.custody_for_test(BY_ID).expect("帳本紀錄保留");
    assert!(matches!(custody.phase, CustodyPhaseSnapshot::CloseFailed { .. }), "{custody:?}");
    assert!(m.mgr.unconfirmed_close_for_test(BY_ID));
    let warnings = m.warnings();
    assert_eq!(
        warnings
            .iter()
            .filter(|w| w.contains(&format!("by_id={BY_ID}")) && w.contains("Input/output error"))
            .count(),
        1,
        "關閉失敗恰好一筆含 by_id 與錯誤的告警：{warnings:?}"
    );
    assert!(device.is_open(), "closed:false 表示 handle 仍開啟");
    assert_eq!(device.open_count(), 1, "IoWorker 未啟動、不重開");
    assert_eq!(device.tx_len(), tx_before, "零 TX");
}

/// S2-PAR-wp510r R4：`custody_for_test` 的紀錄是否為 W6 worker 的開埠在途（`Opening{Worker}`，owner＝該 session）。
fn wp510r_r4_is_w6_opening(rec: &sw_core::custody::CustodySnapshot, actor: sw_core::custody::ActorId) -> bool {
    matches!(
        &rec.phase,
        CustodyPhaseSnapshot::Opening {
            taker: sw_core::custody::CustodyTaker::Worker(_),
        }
    ) && rec.owner == (sw_core::custody::CustodyOwner::Session { actor })
}

/// S2-PAR-wp510r 第 18 項紅燈：shutdown 停止回收者時，已成功入列的兩筆 Reclaim 都須關閉並結算配對回條。
#[test]
fn wp510r_r18_reaper_shutdown_finishes_accepted_reclaims_and_receipts() {
    let _guard = SPAWN_TEST_LOCK.lock().unwrap_or_else(|e| e.into_inner());
    let m = ManagerRig::templates_only();
    let h = CoreHarness::new(SHELL_PROFILE);
    let first_path = "/dev/wp510r-r18-reaper-first";
    let second_path = "/dev/wp510r-r18-reaper-second";
    let first_dev = h.add_serial_device(first_path);
    let second_dev = h.add_serial_device(second_path);
    let first_worker = h.start_worker_at(1801, first_path);
    let second_worker = h.start_worker_at(1802, second_path);
    let first_token = m.mgr.custody_register_bridged_worker_for_test(
        "COM81",
        "by-id-wp510r-r18-first",
        first_path,
        1801,
        first_worker,
    );
    let second_token = m.mgr.custody_register_bridged_worker_for_test(
        "COM82",
        "by-id-wp510r-r18-second",
        second_path,
        1802,
        second_worker,
    );
    let (first_actor, first_worker_token, first_epoch, first_worker) = m
        .mgr
        .custody_take_bridged_worker_for_test("by-id-wp510r-r18-first")
        .expect("第一筆真實 worker");
    let (second_actor, second_worker_token, second_epoch, second_worker) = m
        .mgr
        .custody_take_bridged_worker_for_test("by-id-wp510r-r18-second")
        .expect("第二筆真實 worker");
    assert_eq!(first_worker_token, first_token);
    assert_eq!(second_worker_token, second_token);

    let (entered_tx, entered_rx) = crossbeam_channel::bounded(1);
    let (resume_tx, resume_rx) = crossbeam_channel::bounded(1);
    m.mgr.set_reaper_cmd_hook_for_test(Box::new(move || {
        let _ = entered_tx.send(());
        let _ = resume_rx.recv();
    }));
    let reaper = m.mgr.reaper_cmd_sender_for_test();
    reaper
        .send(sw_core::custody::ReaperCmd::Reclaim {
            actor: first_actor,
            token: first_worker_token,
            epoch: first_epoch,
            worker: Box::new(sw_core::custody::ReclaimWorker::Real(first_worker)),
        })
        .expect("第一筆回收命令已受理");
    entered_rx
        .recv_timeout(Duration::from_secs(5))
        .expect("回收者停在第一筆命令");
    reaper
        .send(sw_core::custody::ReaperCmd::Reclaim {
            actor: second_actor,
            token: second_worker_token,
            epoch: second_epoch,
            worker: Box::new(sw_core::custody::ReclaimWorker::Real(second_worker)),
        })
        .expect("第二筆回收命令已成功入列");

    let mgr = m.mgr.clone();
    let (shutdown_tx, shutdown_rx) = crossbeam_channel::bounded(1);
    std::thread::spawn(move || {
        let _ = shutdown_tx.send(mgr.shutdown(Duration::from_millis(120)));
    });
    wait_until("shutdown 設定 stopping", || m.mgr.stopping_for_test());
    std::thread::sleep(Duration::from_millis(300));
    resume_tx.send(()).expect("放行第一筆回收命令");
    let report = shutdown_rx
        .recv_timeout(Duration::from_secs(5))
        .expect("shutdown 在回收者收尾後返回");

    assert!(
        report.contains(&("attach:by-id-wp510r-r18-first".to_string(), false)),
        "期限內未排空的第一筆須回報 false：{report:?}"
    );
    assert!(
        report.contains(&("attach:by-id-wp510r-r18-second".to_string(), false)),
        "期限內未排空的第二筆須回報 false：{report:?}"
    );
    wait_until("兩筆 Reclaim worker 都關閉", || {
        first_dev.close_count() == 1 && second_dev.close_count() == 1
    });
    assert_eq!(first_dev.close_count(), 1, "第一筆真實 worker 關閉一次");
    assert_eq!(
        second_dev.close_count(),
        1,
        "已入列的第二筆真實 worker 關閉一次"
    );
    wait_until("兩筆 Reclaim 都收到配對終態", || {
        m.mgr.terminal_for_test(first_token).is_some()
            && m.mgr.terminal_for_test(second_token).is_some()
    });
    assert!(
        !first_dev.is_open() && !second_dev.is_open(),
        "兩個 handle 都已關閉"
    );
    for token in [first_token, second_token] {
        assert_eq!(
            m.mgr
                .terminal_for_test(token)
                .map(|terminal| terminal.handle),
            Some(sw_core::custody::TerminalHandle::Closed),
            "token={token} 必須收到 Closed 回條"
        );
    }
    assert!(m.mgr.custody_for_test("by-id-wp510r-r18-first").is_none());
    assert!(m.mgr.custody_for_test("by-id-wp510r-r18-second").is_none());
}

/// S2-PAR-wp510r 第 18 項紅燈：第三次同型 panic 後，真實 W6 Held handle 仍須經 ClaimForStop 關閉並結算。
#[test]
fn wp510r_r18_repeated_panic_claims_real_w6_held_handle() {
    let _guard = SPAWN_TEST_LOCK.lock().unwrap_or_else(|e| e.into_inner());
    const BY_ID: &str = "/s/by-id/usb-wp510r-r18-panic";
    let m = ManagerRig::with(&manager_yaml(&[]), LoadedState::default(), None);
    let real = fallback_session_then_unplug(&m, BY_ID);
    let device = m.device(&real);
    let closes_before = device.close_count();
    let old_sid = m.mgr.resolve("COM0").expect("fallback actor");

    let (rematerialize_entered_tx, rematerialize_entered_rx) = crossbeam_channel::bounded(1);
    let (rematerialize_resume_tx, rematerialize_resume_rx) = crossbeam_channel::bounded(1);
    m.mgr.set_rematerialize_after_set_profile_hook_for_test(
        &old_sid,
        Box::new(move || {
            let _ = rematerialize_entered_tx.send(());
            let _ = rematerialize_resume_rx.recv();
        }),
    );
    let _ = device.take_tx();
    m.reply_probe(&real, b"root@t:/# ");
    m.mgr.update_devices(&m.snapshot());
    rematerialize_entered_rx
        .recv_timeout(Duration::from_secs(5))
        .expect("W6 已接受 SetProfile，停在改名之前");

    let (actor_entered_rx, actor_resume_tx) = m
        .mgr
        .pause_next_request_for_test(&old_sid)
        .expect("fallback actor 存在");
    let query_mgr = m.mgr.clone();
    let query_sid = old_sid.clone();
    let query = std::thread::spawn(move || query_mgr.session_queue_lens(&query_sid));
    actor_entered_rx
        .recv_timeout(Duration::from_secs(5))
        .expect("actor 暫停在查詢請求");

    let control = m
        .mgr
        .actor_control_for_test(&old_sid)
        .expect("actor control lane");
    control
        .send(sw_core::mailbox::ControlEvent::ArmPanic { count: 3 })
        .expect("排入 panic 設定");
    for _ in 0..3 {
        control
            .send(sw_core::mailbox::ControlEvent::DeviceMissing)
            .expect("排入同型 panic 事件");
    }
    let mut filled = 0;
    while control
        .try_send(sw_core::mailbox::ControlEvent::ConsoleCountChanged { count: 0 })
        .is_ok()
    {
        filled += 1;
    }
    assert!(
        filled > 0,
        "control lane 必須確實填滿以阻止 W6 offer 被接手"
    );

    rematerialize_resume_tx
        .send(())
        .expect("放行 W6 改名與 offer");
    let token = {
        let deadline = Instant::now() + Duration::from_secs(10);
        loop {
            if let Some(record) = m.mgr.custody_for_test(BY_ID) {
                if record.phase == CustodyPhaseSnapshot::Held {
                    break record.token;
                }
            }
            assert!(
                Instant::now() < deadline,
                "W6 真實 handle 應先存入 Held 帳本"
            );
            std::thread::sleep(Duration::from_millis(10));
        }
    };
    assert!(
        device.is_open(),
        "offer 尚未被 actor 接手時真實 handle 仍開啟"
    );
    assert_eq!(device.close_count(), closes_before, "停止前不得提早關閉");
    let tx_before_panic = device.tx_len();

    actor_resume_tx.send(()).expect("放行 actor 連續 panic");
    let _ = query.join();
    wait_until("panic 復原的 ClaimForStop 關閉 handle", || {
        m.mgr.terminal_for_test(token).is_some()
    });
    assert_eq!(
        device.close_count(),
        closes_before + 1,
        "Held 真實 handle 恰好關閉一次"
    );
    assert!(!device.is_open(), "Held 真實 handle 已關閉");
    assert!(
        m.mgr.custody_for_test(BY_ID).is_none(),
        "配對回條後移除帳本紀錄"
    );
    assert_eq!(
        m.mgr
            .terminal_for_test(token)
            .map(|terminal| terminal.handle),
        Some(sw_core::custody::TerminalHandle::Closed),
        "ClaimForStop 必須取得配對 Closed 回條"
    );
    assert_eq!(
        device.tx_len(),
        tx_before_panic,
        "停止收尾不得再送出登入 TX"
    );
    let renamed_sid = m.mgr.resolve("t:COM0").expect("W6 已正式改名");
    wait_until("actor 由 panic 停止機制收斂", || {
        s_of(&m.session(&renamed_sid), "state").as_deref() == Some("DETACHED")
    });
}

/// S2-PAR-wp510r 第 18 項紅燈：一般 Opening 帳本 token 的 bind 必須取消開埠、等終態後再套用改綁。
#[test]
fn wp510r_r18_bind_waits_for_plain_opening_custody_terminal() {
    const TARGET: &str = "/s/by-id/usb-wp510r-r18-bind-target";
    let m = ManagerRig::single_target(SHELL_PROFILE);
    m.serial.block_open("/dev/ttyOLD");
    m.mgr.update_devices(&m.snapshot());
    m.mgr.bootstrap_attach();
    wait_until("一般 OpenBridge 已進入開埠", || {
        m.serial.open_attempts("/dev/ttyOLD") == 1
    });
    let opening = m
        .mgr
        .custody_for_test("/s/by-id/usb-A")
        .expect("一般開埠已登記 Opening");
    assert!(
        matches!(opening.phase, CustodyPhaseSnapshot::Opening { .. }),
        "{opening:?}"
    );
    let token = opening.token;
    let tx_before = m.dev.tx_len();

    let mgr = m.mgr.clone();
    let (done_tx, done_rx) = crossbeam_channel::bounded(1);
    std::thread::spawn(move || {
        let _ = done_tx.send(mgr.bind("COM0", TARGET));
    });
    let first_response = done_rx.recv_timeout(Duration::from_millis(150));
    let waited = matches!(
        first_response,
        Err(crossbeam_channel::RecvTimeoutError::Timeout)
    );
    m.serial.release_open("/dev/ttyOLD");
    let response = rpc(match first_response {
        Ok(response) => response,
        Err(crossbeam_channel::RecvTimeoutError::Timeout) => done_rx
            .recv_timeout(Duration::from_secs(5))
            .expect("bind 在開埠終態後返回"),
        Err(crossbeam_channel::RecvTimeoutError::Disconnected) => {
            panic!("bind worker 不應提早斷線")
        }
    });
    if waited {
        let terminal_deadline = Instant::now() + Duration::from_secs(5);
        while m.mgr.terminal_for_test(token).is_none() {
            assert!(
                Instant::now() < terminal_deadline,
                "Opening token 應有配對終態"
            );
            std::thread::sleep(Duration::from_millis(2));
        }
    }
    let public = m.session("COM0");
    let tx_after = m.dev.tx_len();
    let _ = m.mgr.shutdown(Duration::from_millis(500));

    assert!(
        waited,
        "bind 應等待帳本 Opening 的關閉終態，不可立即回 SESSION_RECOVERING"
    );
    assert!(is_ok(&response), "關閉終態成功後應套用改綁：{response:?}");
    assert_eq!(
        s_of(&public, "device_by_id").as_deref(),
        Some(TARGET),
        "新綁定已套用：{public:?}"
    );
    assert_eq!(m.dev.close_count(), 1, "取消的一般開埠 handle 關閉一次");
    assert!(!m.dev.is_open(), "取消後不保留 handle");
    assert_eq!(tx_after, tx_before, "一般開埠取消後不得有登入 TX");
}

/// wp510r 第 2 次整包審查 Medium：首次 bind 等待逾時後，重試仍須等待同一 Opening token 的終態。
#[test]
fn wp510r_r18_bind_after_timeout_waits_for_same_plain_opening_terminal() {
    const TARGET: &str = "/s/by-id/usb-wp510r-r18-bind-retry-target";
    let m = ManagerRig::single_target(SHELL_PROFILE);
    m.serial.block_open("/dev/ttyOLD");
    m.mgr.update_devices(&m.snapshot());
    m.mgr.bootstrap_attach();
    wait_until("一般 OpenBridge 已進入開埠", || {
        m.serial.open_attempts("/dev/ttyOLD") == 1
    });
    let device = m.device("/dev/ttyOLD");
    let opening = m
        .mgr
        .custody_for_test("/s/by-id/usb-A")
        .expect("一般開埠已登記 Opening");
    let token = opening.token;
    assert!(matches!(
        opening.phase,
        CustodyPhaseSnapshot::Opening { .. }
    ));

    let first = rpc(m.mgr.bind("COM0", "/s/by-id/usb-wp510r-r18-first-target"));
    assert_eq!(
        code_of(&first).as_deref(),
        Some("SESSION_RECOVERING"),
        "首次期限應回 RECOVERING：{first:?}"
    );
    assert_eq!(first.get("drain_stalled"), Some(&PyValue::Bool(true)));
    assert!(
        m.mgr.terminal_for_test(token).is_none(),
        "首次 bind 逾時時 Opening 尚未收到終態"
    );
    let tx_before = device.tx_len();

    let mgr = m.mgr.clone();
    let (done_tx, done_rx) = crossbeam_channel::bounded(1);
    std::thread::spawn(move || {
        let _ = done_tx.send(mgr.bind("COM0", TARGET));
    });
    let early = done_rx.recv_timeout(Duration::from_millis(150));
    let waited = matches!(early, Err(crossbeam_channel::RecvTimeoutError::Timeout));
    m.serial.release_open("/dev/ttyOLD");
    let second = rpc(match early {
        Ok(response) => response,
        Err(crossbeam_channel::RecvTimeoutError::Timeout) => done_rx
            .recv_timeout(Duration::from_secs(5))
            .expect("第二次 bind 在相同 Opening 終態後返回"),
        Err(crossbeam_channel::RecvTimeoutError::Disconnected) => {
            panic!("第二次 bind worker 不應提早斷線")
        }
    });
    wait_until("Opening 收到配對終態", || {
        m.mgr.terminal_for_test(token).is_some()
    });
    let public = m.session("COM0");
    let tx_after = device.tx_len();
    let _ = m.mgr.shutdown(Duration::from_millis(500));

    assert!(
        waited,
        "第二次 bind 不得在 Opening 終態前立即回覆：{second:?}"
    );
    assert!(
        is_ok(&second),
        "Opening 關閉後第二次 bind 應套用：{second:?}"
    );
    assert_eq!(
        s_of(&public, "device_by_id").as_deref(),
        Some(TARGET),
        "等待同一 token 終態後套用新的綁定：{public:?}"
    );
    assert_eq!(
        device.close_count(),
        1,
        "被取消的 Opening handle 恰好關閉一次"
    );
    assert!(!device.is_open(), "被取消的 Opening handle 已關閉");
    assert_eq!(tx_after, tx_before, "等待期間不得有登入 TX");
}

/// S2-PAR-wp510r 第 17 項紅燈：正式 W6 改名後，該 actor 在接手 offer 前退出，必須依穩定身分回收 Held handle。
#[test]
fn wp510r_r17_w6_actor_exit_before_offer_adoption_reclaims_renamed_handle() {
    let _guard = SPAWN_TEST_LOCK.lock().unwrap_or_else(|e| e.into_inner());
    const BY_ID: &str = "/s/by-id/usb-D";
    let m = ManagerRig::with(&manager_yaml(&[]), LoadedState::default(), None);
    let historical = CoreHarness::new(SHELL_PROFILE);
    let mut historical_actors = Vec::new();
    let mut historical_records = Vec::new();
    for generation in 1..=2 {
        let path = format!("/dev/wp510r-r17-historical-{generation}");
        let by_id = format!("by-id-wp510r-r17-historical-{generation}");
        let device = historical.add_serial_device(&path);
        let worker = historical.start_worker_at(900 + generation, &path);
        let token = m.mgr.custody_register_bridged_worker_for_test(
            "t:COM0",
            &by_id,
            &path,
            900 + generation,
            worker,
        );
        historical_actors.push(m.mgr.custody_actor_for_test(&by_id).expect("歷史 owner ActorId"));
        historical_records.push((device, token));
    }
    let real = fallback_session_then_unplug(&m, BY_ID);
    let closes_before = m.device(&real).close_count();
    let old_sid = m.mgr.resolve("COM0").expect("fallback actor");

    let (rematerialize_entered_tx, rematerialize_entered_rx) = crossbeam_channel::bounded(1);
    let (rematerialize_resume_tx, rematerialize_resume_rx) = crossbeam_channel::bounded(1);
    m.mgr.set_rematerialize_after_set_profile_hook_for_test(
        &old_sid,
        Box::new(move || {
            let _ = rematerialize_entered_tx.send(());
            let _ = rematerialize_resume_rx.recv();
        }),
    );
    let _ = m.device(&real).take_tx();
    m.reply_probe(&real, b"root@t:/# ");
    m.mgr.update_devices(&m.snapshot());
    rematerialize_entered_rx
        .recv_timeout(Duration::from_secs(5))
        .expect("正式 W6 已由 actor 接受 SetProfile，停在 manager 改名前");

    // actor 暫停在已受理的查詢；填滿 control lane，確保 W6 的 CustodyOffer 尚未被 actor 接手。
    let (actor_entered_rx, actor_resume_tx) = m
        .mgr
        .pause_next_request_for_test(&old_sid)
        .expect("fallback actor 存在");
    let query_mgr = m.mgr.clone();
    let query_sid = old_sid.clone();
    let query = std::thread::spawn(move || query_mgr.session_queue_lens(&query_sid));
    actor_entered_rx
        .recv_timeout(Duration::from_secs(5))
        .expect("actor 已停在查詢 Request");
    let control = m.mgr.actor_control_for_test(&old_sid).expect("actor control lane");
    let mut filled = 0;
    while control
        .try_send(sw_core::mailbox::ControlEvent::ConsoleCountChanged { count: 0 })
        .is_ok()
    {
        filled += 1;
    }
    assert!(filled > 0, "control lane 必須確實被填滿");
    rematerialize_resume_tx.send(()).expect("放行正式 W6 rematerialize");

    let token = {
        let deadline = Instant::now() + Duration::from_secs(10);
        loop {
            if let Some(rec) = m.mgr.custody_for_test(BY_ID) {
                if rec.phase == CustodyPhaseSnapshot::Held {
                    break rec.token;
                }
            }
            assert!(Instant::now() < deadline, "正式 W6 應將 handle 存回 Held");
            std::thread::sleep(Duration::from_millis(10));
        }
    };
    let new_sid = m.mgr.resolve("t:COM0").expect("W6 已正式改名到偵測 template");
    let held = m.mgr.custody_for_test(BY_ID).expect("Held 紀錄");
    let renamed_actor = m.mgr.actor_id_for_test(&new_sid).expect("改名後仍為同一 actor");
    assert!(matches!(
        &held.owner,
        sw_core::custody::CustodyOwner::Session { actor } if *actor == renamed_actor
    ));
    assert!(m.device(&real).is_open(), "actor 接手前 handle 由帳本保管");

    // 延遲送達佔用相同公開 sid 舊世代的退出通知，不得誤關 W6 handle。
    let reaper = m.mgr.reaper_cmd_sender_for_test();
    for ((device, old_token), old_actor) in historical_records.iter().zip(historical_actors) {
        reaper
            .send(sw_core::custody::ReaperCmd::ActorExited { actor: old_actor })
            .expect("延遲歷史 actor 退出通知送達");
        wait_until("歷史 actor 關閉完成", || m.mgr.terminal_for_test(*old_token).is_some());
        assert_eq!(device.close_count(), 1, "歷史 handle 關閉一次");
    }
    assert!(m.device(&real).is_open(), "歷史 ActorId 不得關閉 W6 handle");
    assert!(m.mgr.terminal_for_test(token).is_none(), "歷史 ActorId 不產生 W6 終態");

    // 讓 actor 的真實 ExitReclaim Drop 發送退出通知，不直接呼叫帳本回收 helper。
    m.mgr.set_executor_unwind_hook_for_test(
        &new_sid,
        Box::new(|| panic!("注入 actor executor unwind")),
    );
    actor_resume_tx.send(()).expect("放行 actor 查詢");
    let _ = query.join();
    wait_until("W6 改名 actor 自身退出已結算回條", || {
        m.mgr.terminal_for_test(token).is_some()
    });

    assert_eq!(m.device(&real).close_count(), closes_before + 1, "自身退出須關閉 W6 handle 一次");
    assert!(!m.device(&real).is_open(), "自身退出後 handle 已關閉");
    assert!(m.mgr.custody_for_test(BY_ID).is_none(), "成功回條後移除帳本紀錄");
    assert_eq!(
        m.mgr.terminal_for_test(token).map(|terminal| terminal.handle),
        Some(sw_core::custody::TerminalHandle::Closed),
        "W6 offer 未接手時，actor 自身退出須取得配對 Closed 回條"
    );
}

/// S2-PAR-wp510r R4 紅燈 P8-e25b69c-2（設計 §1.6、§3.1.4 W6）：既有 fallback session 重插後的 #181 再解析卡在開埠超過
/// release 首次期限。W6 先以 `BeginExternalAttach` 讓 actor 記下 token，帳本紀錄於開埠前登記為 `Opening{Worker}`、
/// owner＝該 session；release 由 actor 以 `ClaimForStop` 寫入取消（cancel＝Release），期限到回 DRAIN_STALLED＋
/// RECOVERING（Python `release_device` 找到作業取消並等到期限，`origin/main:sw_core/session_manager.py:3104-3138`、
/// `:3192-3204`）。開埠遲到返回見取消即關閉、不送 PROBE（`:4216-4245`），`_finish_attach_op` 以第一個 release 的
/// source 完成 RELEASED、清 stall 欄位並落盤（`:4339-4355`）；不需第二次 release，帳本收尾後 None。
#[test]
fn wp510r_r4_p8_e25b69c_2_redetect_release_stall_ledger_opening_then_released() {
    let m = ManagerRig::with(&manager_yaml(&[]), LoadedState::default(), None);
    let real = fallback_session_then_unplug(&m, "/s/by-id/usb-D");
    m.serial.block_open(&real);
    m.mgr.update_devices(&m.snapshot());
    wait_until("再偵測卡在 open", || m.serial.open_attempts(&real) == 2);
    let closes_before = m.device(&real).close_count();
    let tx_before = m.device(&real).tx_len();
    let sid = m.mgr.resolve("COM0").expect("COM0 session");
    let blocked = m.mgr.custody_for_test("/s/by-id/usb-D").expect("W6 開埠在途須登記帳本紀錄");
    assert!(wp510r_r4_is_w6_opening(
        &blocked,
        m.mgr.actor_id_for_test(&sid).expect("actor 身分"),
    ), "{blocked:?}");
    assert_eq!(blocked.cancel, None, "release 前未取消：{blocked:?}");

    let started = Instant::now();
    let reply = m.mgr.route(
        &sid,
        SessionRequest::Release {
            source: "cli".to_string(),
            reason: PyValue::Null,
        },
        RPC,
    );
    let waited = started.elapsed();
    let reply = rpc(reply);
    assert!(waited >= Duration::from_millis(2500), "release 須等到首次期限：{waited:?}");
    assert_eq!(code_of(&reply).as_deref(), Some("DRAIN_STALLED"), "{reply:?}");
    assert_eq!(reply.get("drain_stalled"), Some(&PyValue::Bool(true)));
    assert_eq!(s_of(&session_field(&reply), "state").as_deref(), Some("RECOVERING"), "{reply:?}");
    let stalled = m.mgr.custody_for_test("/s/by-id/usb-D").expect("阻塞期間紀錄保留");
    assert!(wp510r_r4_is_w6_opening(
        &stalled,
        m.mgr.actor_id_for_test(&sid).expect("actor 身分"),
    ), "{stalled:?}");
    assert_eq!(stalled.token, blocked.token, "同一筆紀錄");
    assert_eq!(stalled.cancel, Some(sw_core::custody::CustodyCancel::Release), "{stalled:?}");

    m.serial.release_open(&real);
    m.wait_state("COM0", "RELEASED");
    wait_until("帳本紀錄收尾", || m.mgr.custody_for_test("/s/by-id/usb-D").is_none());
    let session = m.session("COM0");
    assert_eq!(s_of(&session, "released_by").as_deref(), Some("cli"), "{session:?}");
    assert_eq!(session.get("released_reason"), Some(&PyValue::Null), "{session:?}");
    assert_eq!(session.get("drain_stalled"), Some(&PyValue::Bool(false)), "{session:?}");
    assert_eq!(session.get("drain_timeout_epoch"), Some(&PyValue::Null), "{session:?}");
    wait_until("RELEASED 落盤", || {
        m.state_text().contains("\"released\"") && m.state_text().contains("/s/by-id/usb-D")
    });
    assert_eq!(m.device(&real).close_count(), closes_before + 1, "遲到的 handle 關閉一次");
    assert!(!m.device(&real).is_open(), "沒有 IoWorker 持有 handle");
    assert_eq!(m.device(&real).tx_len(), tx_before, "取消後不得送 PROBE");
    std::thread::sleep(Duration::from_millis(200));
    assert_eq!(m.serial.open_attempts(&real), 2, "不得另開");
    assert_eq!(
        m.mgr.terminal_for_test(blocked.token).map(|t| t.handle),
        Some(sw_core::custody::TerminalHandle::Closed),
        "同 token 的終態為 Closed"
    );
}

/// S2-PAR-wp510r R4（設計 §3.1.4 W6 第 2 步「有 tombstone 就中止」）：`BeginExternalAttach` 已被接受、W6 worker 登記帳本
/// 紀錄之前 release 先到。actor 的 `ClaimForStop` 查無紀錄即寫入 tombstone 並完成 release（沒有 handle 要等），worker
/// 之後的登記看到 tombstone 就中止、不開埠（Python release 在作業登記前到達時 session 沒有作業，直接 RELEASED；
/// `_preopen_auto_attach_port` 見 RELEASED 即不登記、不開埠，`origin/main:sw_core/session_manager.py:4159-4166`）。
#[test]
fn wp510r_r4_w6_release_before_register_tombstone_aborts_open() {
    let m = ManagerRig::with(&manager_yaml(&[]), LoadedState::default(), None);
    let real = fallback_session_then_unplug(&m, "/s/by-id/usb-D");
    let (entered_tx, entered_rx) = crossbeam_channel::bounded(1);
    let (go_tx, go_rx) = crossbeam_channel::bounded::<()>(1);
    m.mgr.set_external_attach_pre_register_hook_for_test(
        "/s/by-id/usb-D",
        Box::new(move || {
            let _ = entered_tx.send(());
            let _ = go_rx.recv_timeout(Duration::from_secs(10));
        }),
    );
    m.mgr.update_devices(&m.snapshot());
    entered_rx
        .recv_timeout(Duration::from_secs(10))
        .expect("W6 worker 應停在 BeginExternalAttach 接受後、登記前");
    assert!(m.mgr.custody_for_test("/s/by-id/usb-D").is_none(), "登記前沒有帳本紀錄");
    let sid = m.mgr.resolve("COM0").expect("COM0 session");

    let started = Instant::now();
    let reply = rpc(wp510_release(&m.mgr, &sid));
    assert!(is_ok(&reply), "{reply:?}");
    assert!(started.elapsed() < Duration::from_millis(2000), "沒有 handle 要等：{:?}", started.elapsed());
    assert_eq!(s_of(&session_field(&reply), "state").as_deref(), Some("RELEASED"), "{reply:?}");

    go_tx.send(()).expect("放行 worker");
    wait_until("worker 收尾", || !m.mgr.has_inflight_attach_for_test("/s/by-id/usb-D"));
    std::thread::sleep(Duration::from_millis(200));
    assert_eq!(m.serial.open_attempts(&real), 1, "tombstone 令 worker 中止、不開埠");
    assert!(m.mgr.custody_for_test("/s/by-id/usb-D").is_none(), "不得留下紀錄");
    assert_eq!(s_of(&m.session("COM0"), "state").as_deref(), Some("RELEASED"));
}

/// S2-PAR-wp510r R4 DR-1(b)（設計 §1.8、C4、§3.1.5「W2／W6 offer 通知 Full」）：W6 worker 完成 PROBE、把 handle 存回
/// `Held{Session}` 時該 session 的 control lane 已滿（H-actor-hold＋填滿 lane），`CustodyOffer` 的 `try_send` 回 Full。
/// 紀錄保持 `Held{owner:Session}` 可查；放行後 actor 的 `CustodyPoll` 計時器（500 ms）補收 offer，以同一 handle 照常
/// adopt，開埠次數不增加、不重複 TX。
#[test]
fn wp510r_r4_dr1_b_w6_offer_lane_full_custody_poll_claims_same_handle() {
    let m = ManagerRig::with(&manager_yaml(&[]), LoadedState::default(), None);
    let real = fallback_session_then_unplug(&m, "/s/by-id/usb-D");
    let sid = m.mgr.resolve("COM0").expect("COM0 session");
    m.serial.block_open(&real);
    m.mgr.update_devices(&m.snapshot());
    wait_until("再偵測卡在 open", || m.serial.open_attempts(&real) == 2);
    let token = m.mgr.custody_for_test("/s/by-id/usb-D").expect("W6 紀錄").token;

    // H-actor-hold：actor 停在一筆無關查詢，再灌滿 control lane。
    let (entered, resume) = m.mgr.pause_next_request_for_test(&sid).expect("actor 存在");
    let query_mgr = m.mgr.clone();
    let query_sid = sid.clone();
    let query = std::thread::spawn(move || {
        let _ = query_mgr.session_queue_lens(&query_sid);
    });
    entered
        .recv_timeout(Duration::from_secs(5))
        .expect("actor 應停在無關的查詢 Request");
    let ctrl = m.mgr.actor_control_for_test(&sid).expect("control sender");
    let mut filled = 0;
    while ctrl
        .try_send(sw_core::mailbox::ControlEvent::ConsoleCountChanged { count: 0 })
        .is_ok()
    {
        filled += 1;
    }
    assert!(filled > 0, "灌滿迴圈必須實際送入事件");

    m.serial.release_open(&real);
    wait_until("W6 worker 存回 Held{Session}", || {
        m.mgr
            .custody_for_test("/s/by-id/usb-D")
            .is_some_and(|rec| rec.phase == CustodyPhaseSnapshot::Held)
    });
    let held = m.mgr.custody_for_test("/s/by-id/usb-D").expect("紀錄");
    assert_eq!(held.token, token, "同一筆紀錄");
    assert!(
        matches!(&held.owner, sw_core::custody::CustodyOwner::Session { actor } if Some(*actor) == m.mgr.actor_id_for_test(&sid)),
        "lane 滿時紀錄保持 Held{{owner:Session}}：{held:?}"
    );
    let open_attempts = m.serial.open_attempts(&real);
    assert!(m.device(&real).is_open(), "handle 仍在帳本");

    resume.send(()).expect("放行 actor");
    query.join().expect("查詢執行緒不應 panic");
    // CustodyPoll 是 actor 的本地計時器：推進 mono 讓補收觸發。
    let deadline = Instant::now() + Duration::from_secs(10);
    while m.mgr.custody_for_test("/s/by-id/usb-D").is_some() && Instant::now() < deadline {
        m.clock.advance(Duration::from_millis(500));
        std::thread::sleep(Duration::from_millis(50));
    }
    assert!(
        m.mgr.custody_for_test("/s/by-id/usb-D").is_none(),
        "CustodyPoll 補收 offer 後由正式 bridge 接手：{:?}",
        m.mgr.custody_for_test("/s/by-id/usb-D")
    );
    m.wait_state("COM0", "ATTACHED");
    assert_eq!(m.serial.open_attempts(&real), open_attempts, "以原 handle adopt，不另開");
    assert_eq!(m.device(&real).close_count(), 1, "只有拔除時那一次關閉");
    assert!(m.device(&real).is_open(), "正式 bridge 持有 handle");
}

/// S2-PAR-wp510r R4（設計 §3.1.4 W6、§3.1.6 shutdown）：W6 再解析卡在開埠時 shutdown。actor 的 Shutdown 以
/// `ClaimForStop{Shutdown}` 寫入帳本紀錄的取消，開埠返回後 W6 worker 於檢查點關閉（零 TX、不 PROBE），回條成功後報告
/// 已排空、紀錄移除（Python shutdown 取消作業並等 `done`，`origin/main:sw_core/session_manager.py:1181-1185`、
/// `:1228-1239`；開埠返回見取消即 `_finish_attach_op`，`:4216-4245`）。
#[test]
fn wp510r_r4_w6_shutdown_during_open_closes_and_reports_drained() {
    let m = ManagerRig::with(&manager_yaml(&[]), LoadedState::default(), None);
    let real = fallback_session_then_unplug(&m, "/s/by-id/usb-D");
    m.serial.block_open(&real);
    m.mgr.update_devices(&m.snapshot());
    wait_until("再偵測卡在 open", || m.serial.open_attempts(&real) == 2);
    let closes_before = m.device(&real).close_count();
    let tx_before = m.device(&real).tx_len();
    let serial = m.serial.clone();
    let release_real = real.clone();
    let unblock = std::thread::spawn(move || {
        std::thread::sleep(Duration::from_millis(300));
        serial.release_open(&release_real);
    });
    let drained = m.mgr.shutdown(Duration::from_secs(5));
    unblock.join().expect("放行執行緒不應 panic");
    assert!(drained.iter().all(|(_, ok)| *ok), "回條成功後報告已排空：{drained:?}");
    assert_eq!(m.device(&real).close_count(), closes_before + 1, "遲到的 handle 關閉一次");
    assert!(!m.device(&real).is_open());
    assert_eq!(m.device(&real).tx_len(), tx_before, "零 TX");
    assert!(m.mgr.custody_for_test("/s/by-id/usb-D").is_none(), "紀錄移除");
    assert!(!m.mgr.unconfirmed_close_for_test("/s/by-id/usb-D"));
}

/// S2-PAR-wp510r R4（設計 §3.1.4 W6、C5、§3.1.9 零 TX；WP3-1 的 W6 版本）：W6 再解析已越過開埠檢查點、PROBE 尚未提交時
/// release。actor 的 `ClaimForStop` 在 manager 鎖內寫入帳本紀錄的取消時，必須一併凍結該作業的 PROBE fence 並撤銷進行中
/// 的寫入（Python release 對作業 `probe_bridge.begin_drain`，`origin/main:sw_core/session_manager.py:3125-3131`；
/// `uart_io.py:908-918`、`:623-627`）：PROBE 零 TX，worker 於 PROBE 後的檢查點關閉而不交出，release 完成 RELEASED。
#[test]
fn wp510r_r4_w6_release_during_probe_freezes_probe_tx() {
    let m = ManagerRig::with(&manager_yaml(&[]), LoadedState::default(), None);
    let real = fallback_session_then_unplug(&m, "/s/by-id/usb-D");
    let sid = m.mgr.resolve("COM0").expect("COM0 session");
    let tx_before = m.device(&real).tx_len();
    let closes_before = m.device(&real).close_count();
    let weak = std::sync::Arc::downgrade(&m.mgr);
    let hook_sid = sid.clone();
    let (release_tx, release_rx) = crossbeam_channel::bounded(1);
    let (cancelled_tx, cancelled_rx) = crossbeam_channel::bounded(1);
    let fired = std::sync::Arc::new(std::sync::atomic::AtomicBool::new(false));
    let hook_fired = fired.clone();
    m.mgr.set_probe_pre_submit_hook_for_test(Box::new(move || {
        if hook_fired.swap(true, std::sync::atomic::Ordering::SeqCst) {
            return;
        }
        let Some(manager) = weak.upgrade() else {
            return;
        };
        let release_mgr = manager.clone();
        let release_sid = hook_sid.clone();
        let tx = release_tx.clone();
        std::thread::spawn(move || {
            let _ = tx.send(wp510_release(&release_mgr, &release_sid));
        });
        let deadline = Instant::now() + Duration::from_secs(5);
        while Instant::now() < deadline {
            if manager
                .custody_for_test("/s/by-id/usb-D")
                .is_some_and(|rec| rec.cancel == Some(sw_core::custody::CustodyCancel::Release))
            {
                let _ = cancelled_tx.send(());
                return;
            }
            std::thread::sleep(Duration::from_millis(5));
        }
    }));
    m.mgr.update_devices(&m.snapshot());
    cancelled_rx
        .recv_timeout(Duration::from_secs(10))
        .expect("PROBE 提交前 release 已寫入帳本紀錄的取消");
    let reply = rpc(release_rx.recv_timeout(RPC).expect("release 應在 W6 收尾後回覆"));
    assert!(is_ok(&reply), "{reply:?}");
    m.wait_state("COM0", "RELEASED");
    wait_until("帳本紀錄收尾", || m.mgr.custody_for_test("/s/by-id/usb-D").is_none());
    assert_eq!(m.device(&real).tx_len(), tx_before, "取消後 PROBE 不得送出任何 TX");
    assert_eq!(m.device(&real).close_count(), closes_before + 1, "W6 handle 關閉一次");
    assert!(!m.device(&real).is_open());
}

/// S2-PAR-wp510r R4（設計 §3.1.6 D1 iii＋T-1、§3.1.4 W6）：W6 再解析卡在開埠時 release，期限到回 DRAIN_STALLED；之後開埠
/// 返回、W6 worker 於檢查點關閉 handle 失敗（closed:false）。紀錄轉 CloseFailed 並登記未確認關閉（永久阻擋），session
/// 維持 RECOVERING、不落盤 RELEASED；之後的 release 首次期限已過、立即回 DRAIN_STALLED（Python `_finish_attach_op` 關閉
/// 失敗保留作業、`done` 永不設定，`origin/main:sw_core/session_manager.py:4320-4332`、`:3188-3204`）。
#[test]
fn wp510r_r4_w6_release_close_failure_keeps_blocked_and_stalled() {
    let m = ManagerRig::with(&manager_yaml(&[]), LoadedState::default(), None);
    let real = fallback_session_then_unplug(&m, "/s/by-id/usb-D");
    m.serial.block_open(&real);
    m.mgr.update_devices(&m.snapshot());
    wait_until("再偵測卡在 open", || m.serial.open_attempts(&real) == 2);
    let sid = m.mgr.resolve("COM0").expect("COM0 session");
    let token = m.mgr.custody_for_test("/s/by-id/usb-D").expect("W6 紀錄").token;
    let reply = rpc(wp510_release(&m.mgr, &sid));
    assert_eq!(code_of(&reply).as_deref(), Some("DRAIN_STALLED"), "{reply:?}");
    let device = m.device(&real);
    device.fail_close(sw_pal::traits::PalError::io(5, "[Errno 5] Input/output error"));
    m.serial.release_open(&real);
    wait_until("W6 關閉失敗轉 CloseFailed", || {
        m.mgr.custody_for_test("/s/by-id/usb-D").is_some_and(|rec| {
            rec.token == token && matches!(rec.phase, CustodyPhaseSnapshot::CloseFailed { .. })
        })
    });
    assert!(m.mgr.unconfirmed_close_for_test("/s/by-id/usb-D"), "D1 iii：永久阻擋");
    std::thread::sleep(Duration::from_millis(200));
    assert_eq!(s_of(&m.session("COM0"), "state").as_deref(), Some("RECOVERING"));
    let released_rows = match sw_proto::pyjson::loads(&m.state_text()) {
        Ok(PyValue::Dict(state)) => match state.get("released") {
            Some(PyValue::Dict(rows)) => rows
                .0
                .values()
                .filter(|row| match row {
                    PyValue::Dict(row) => row.get("by_id").and_then(PyValue::as_str) == Some("/s/by-id/usb-D"),
                    _ => false,
                })
                .count(),
            _ => 0,
        },
        _ => 0,
    };
    assert_eq!(released_rows, 0, "關閉未確認不得落盤 RELEASED：{}", m.state_text());
    let started = Instant::now();
    let again = rpc(wp510_release(&m.mgr, &sid));
    assert_eq!(code_of(&again).as_deref(), Some("DRAIN_STALLED"), "{again:?}");
    assert!(started.elapsed() < Duration::from_millis(1000), "首次期限已過，立即回：{:?}", started.elapsed());
    assert_eq!(m.serial.open_attempts(&real), 2, "不重開");
    assert!(device.is_open(), "closed:false 表示 handle 仍開啟");
}

// ---------------------------------------------------------------------------
// S2-PAR-wp510r R5（設計 §4 R5、§3.1.4 W1、§1.2、§1.3）：佔位 W1 改由 actor（正式 manager／actor 路徑）。
// ---------------------------------------------------------------------------

const WP510R_R5_SID: &str = "gone-template:COM3";
const WP510R_R5_BY_ID: &str = "/s/by-id/usb-G";
const WP510R_R5_REAL: &str = "/dev/ttyG";

/// WP1-1（設計 v3 §1.2）：開埠阻塞時帳本為佔位 actor 世代的 `Opening{PlaceholderOpen}`；release 在紀錄上寫入 cancel＝Release，
/// 關閉回條之前不回覆；放行開埠後 A 回 SESSION_RECOVERING（RELEASED、drain false/null），B 回 already_released（鍵序
/// ok／already_released／session，Python `release_device` `origin/main:sw_core/session_manager.py:3209`）。open 1、close 1、
/// is_open false；佔位保留、沒有正式 session、零 TX；帳本最後為 None。
#[test]
fn wp510r_r5_wp1_1_release_during_placeholder_open_cancels_ledger_record() {
    let m = wp510_b_placeholder_rig(false, false);
    let sid = m.mgr.resolve(WP510R_R5_SID).expect("佔位 session");
    let device = m.device(WP510R_R5_REAL);
    m.mgr.update_devices(&m.snapshot());
    m.serial.block_open(WP510R_R5_REAL);

    let (attach_tx, attach_rx) = crossbeam_channel::bounded(1);
    let attach_manager = m.mgr.clone();
    let attach_sid = sid.clone();
    let attach = std::thread::spawn(move || {
        let _ = attach_tx.send(attach_manager.attach_released_placeholder(&attach_sid, false));
    });
    wait_until("佔位 attach 停在 open_exclusive", || m.serial.open_attempts(WP510R_R5_REAL) == 1);
    let rec = m.mgr.custody_for_test(WP510R_R5_BY_ID).expect("開埠在途須有帳本紀錄");
    assert!(
        matches!(
            &rec.phase,
            CustodyPhaseSnapshot::Opening { taker: sw_core::custody::CustodyTaker::PlaceholderOpen { actor } }
                if Some(*actor) == m.mgr.actor_id_for_test(WP510R_R5_SID)
        ),
        "開埠阻塞時為 Opening{{PlaceholderOpen}}：{rec:?}"
    );
    assert!(
        matches!(&rec.owner, sw_core::custody::CustodyOwner::Session { actor } if Some(*actor) == m.mgr.actor_id_for_test(WP510R_R5_SID)),
        "owner 為佔位 actor 的世代（C7）：{rec:?}"
    );
    assert_eq!(rec.cancel, None);

    let (release_tx, release_rx) = crossbeam_channel::bounded(1);
    let release_manager = m.mgr.clone();
    let release_sid = sid.clone();
    let release = std::thread::spawn(move || {
        let _ = release_tx.send(wp510_release(&release_manager, &release_sid));
    });
    wait_until("release 在帳本紀錄寫入取消", || {
        m.mgr.custody_for_test(WP510R_R5_BY_ID).is_some_and(|r| {
            r.token == rec.token && r.cancel == Some(sw_core::custody::CustodyCancel::Release)
        })
    });
    assert!(release_rx.recv_timeout(Duration::from_millis(300)).is_err(), "關閉回條前 release 不得回覆");
    assert!(attach_rx.try_recv().is_err(), "開埠返回前 attach 不得回覆");

    m.serial.release_open(WP510R_R5_REAL);
    let a = rpc(attach_rx.recv_timeout(RPC).expect("attach 應完成"));
    let b = rpc(release_rx.recv_timeout(RPC).expect("release 應完成"));
    attach.join().expect("attach thread");
    release.join().expect("release thread");

    assert_eq!(code_of(&a).as_deref(), Some("SESSION_RECOVERING"), "{a:?}");
    assert_eq!(a.get("drain_stalled"), Some(&PyValue::Bool(false)));
    assert_eq!(a.get("drain_timeout_epoch"), Some(&PyValue::Null));
    assert_eq!(s_of(&session_field(&a), "state").as_deref(), Some("RELEASED"));
    assert_eq!(
        b.0.keys().map(String::as_str).collect::<Vec<_>>(),
        ["ok", "already_released", "session"],
        "G-2：Python `release_device` 鍵序（origin/main:sw_core/session_manager.py:3209）：{b:?}"
    );
    let session = session_field(&b);
    assert_eq!(s_of(&session, "state").as_deref(), Some("RELEASED"));
    assert_eq!(s_of(&session, "released_by").as_deref(), Some("agent"), "沿用佔位的 released_by");
    assert_eq!(session.get("drain_stalled"), Some(&PyValue::Bool(false)));
    assert_eq!(session.get("drain_timeout_epoch"), Some(&PyValue::Null));
    assert_eq!(device.open_count(), 1);
    assert_eq!(device.close_count(), 1);
    assert!(!device.is_open());
    assert!(m.mgr.custody_for_test(WP510R_R5_BY_ID).is_none(), "帳本最後為 None");
    assert_eq!(m.mgr.released_placeholder(&sid), Some(true), "佔位保留");
    assert!(m.mgr.resolve("t:COM3").is_none(), "沒有正式 session");
    assert!(device.take_tx().is_empty(), "零 TX");
}

/// WP5-2（設計 v3 §1.3；D3）：交接 worker 建立失敗時回 EXC `can't start new thread`，開埠次數 0、帳本與 prepared 皆空、佔位
/// 不變並告警含 by_id；之後可以再交接。
#[test]
fn wp510r_r5_wp5_2_prepare_failure_leaves_no_ledger_or_prepared_worker() {
    let m = wp510_b_placeholder_rig(false, false);
    let sid = m.mgr.resolve(WP510R_R5_SID).expect("佔位 session");
    m.mgr.update_devices(&m.snapshot());
    m.mgr.fail_next_attach_spawn_for_test();
    let error = m
        .mgr
        .attach_released_placeholder(&sid, false)
        .expect_err("worker 建立失敗回 RPC 錯誤");
    assert_eq!(error.message, "can't start new thread");
    assert_eq!(m.serial.open_attempts(WP510R_R5_REAL), 0, "D3：worker 在開埠前準備");
    assert!(m.mgr.custody_for_test(WP510R_R5_BY_ID).is_none());
    assert_eq!(m.mgr.prepared_len_for_test(), 0, "prepared 為空");
    assert!(
        m.warnings().iter().any(|w| w.contains(WP510R_R5_BY_ID)),
        "告警含 by_id：{:?}",
        m.warnings()
    );
    wp510_b_assert_placeholder_retained(&m, &sid);
    let again = rpc(m.mgr.attach_released_placeholder(&sid, false));
    assert!(is_ok(&again), "之後可以再交接：{again:?}");
    assert_eq!(m.mgr.prepared_len_for_test(), 0, "交接成功後 prepared 亦清空");
}

/// WP6-2(a)（設計 v3 §1.3；D2）：開埠成功後退休落盤 ENOSPC → 回 EXC、不回滾；帳本以同一 token 存回 `Held{Parked}`（不關閉、
/// is_open true）；三種 selector 都查無佔位；零 TX。
#[test]
fn wp510r_r5_wp6_2_retire_persist_failure_parks_same_token() {
    let m = wp510_b_placeholder_rig(false, false);
    let sid = m.mgr.resolve(WP510R_R5_SID).expect("佔位 session");
    let device = m.device(WP510R_R5_REAL);
    m.mgr.update_devices(&m.snapshot());
    m.serial.block_open(WP510R_R5_REAL);
    let (attach_tx, attach_rx) = crossbeam_channel::bounded(1);
    let attach_manager = m.mgr.clone();
    let attach_sid = sid.clone();
    let attach = std::thread::spawn(move || {
        let _ = attach_tx.send(attach_manager.attach_released_placeholder(&attach_sid, false));
    });
    wait_until("佔位 attach 停在 open_exclusive", || m.serial.open_attempts(WP510R_R5_REAL) == 1);
    let token = m.mgr.custody_for_test(WP510R_R5_BY_ID).expect("開埠在途紀錄").token;
    m.fs.fail_next_atomic_write(PalError::io(28, "No space left on device"));
    m.serial.release_open(WP510R_R5_REAL);
    let result = attach_rx.recv_timeout(RPC).expect("attach 應完成");
    attach.join().expect("attach thread");
    let error = result.expect_err("退休落盤失敗回 EXC");
    assert!(error.message.contains("No space left on device"), "{}", error.message);
    let rec = m.mgr.custody_for_test(WP510R_R5_BY_ID).expect("parked 紀錄");
    assert_eq!(rec.token, token, "同一 token 存回 parked");
    assert_eq!(rec.phase, CustodyPhaseSnapshot::Held);
    assert_eq!(rec.owner, sw_core::custody::CustodyOwner::Parked);
    assert!(device.is_open(), "D2：保留 parked handle");
    assert_eq!(device.close_count(), 0);
    for selector in [WP510R_R5_SID, "COM3", "gone-template+4"] {
        assert!(m.mgr.resolve(selector).is_none(), "{selector} 已退休");
    }
    assert!(device.take_tx().is_empty(), "零 TX");
}

/// G-8（設計 v1 §1.8）：重啟後載入的一般 RELEASED session 從未收到 `DeviceAttached`；`device.attach` 以 manager 在鎖內取得的
/// 裝置快照判斷在線（Python `attach_device` 讀 `self._devices`，`origin/main:sw_core/session_manager.py:3422-3423`、
/// `:3482-3488`），不得誤回 DEVICE_NOT_PRESENT。
#[test]
fn wp510r_r5_g8_restored_released_session_attach_uses_manager_device_snapshot() {
    const BY_ID: &str = "/s/by-id/usb-R";
    let mut loaded = LoadedState::default();
    loaded.released.insert("t:COM1".to_string(), released_row(BY_ID));
    let m = ManagerRig::with(&manager_yaml(&[&t_target("COM1", 1, BY_ID)]), loaded, None);
    let dev = m.add_device_at(BY_ID, "/dev/ttyR");
    install_shell_replies(&dev);
    m.mgr.update_devices(&m.snapshot());
    m.mgr.bootstrap_attach();
    assert_eq!(m.state_of("COM1").as_deref(), Some("RELEASED"));
    let reply = rpc(m.mgr.route("t:COM1", SessionRequest::DeviceAttach { force: false }, RPC));
    assert!(is_ok(&reply), "manager 快照中在線的裝置不得回 DEVICE_NOT_PRESENT：{reply:?}");
    wait_until("t:COM1 開埠", || m.serial.open_attempts("/dev/ttyR") >= 1);
}

/// WP2-2、P8-19df186-2（設計 v3 §1.2）：開埠阻塞不放行時，t0 的 release#1 與 t0+1 s 的 release#2 共用首次 release 的絕對期限，
/// 都在約 t0+3 s 回 DRAIN_STALLED（頂層 epoch＝g、session RELEASED、session.drain_stalled true／epoch g）；期限過後的
/// release#3 在 200 ms 內回覆（Python `release_device` `origin/main:sw_core/session_manager.py:3117-3124`、`:3192-3204`；
/// `tests/test_p1oc_drain_stalled.py:250-281`）。放行並關閉成功後收斂為 false/null、RELEASED，再 release 回 already_released。
#[test]
fn wp510r_r5_wp2_2_releases_share_first_absolute_deadline() {
    let m = wp510_b_placeholder_rig(false, false);
    let sid = m.mgr.resolve(WP510R_R5_SID).expect("佔位 session");
    let device = m.device(WP510R_R5_REAL);
    m.mgr.update_devices(&m.snapshot());
    m.serial.block_open(WP510R_R5_REAL);
    let attach_manager = m.mgr.clone();
    let attach_sid = sid.clone();
    let attach = std::thread::spawn(move || attach_manager.attach_released_placeholder(&attach_sid, false));
    wait_until("佔位 attach 停在 open_exclusive", || m.serial.open_attempts(WP510R_R5_REAL) == 1);
    let t0 = Instant::now();
    let spawn_release = |delay: Duration| {
        let manager = m.mgr.clone();
        let sid = sid.clone();
        std::thread::spawn(move || {
            std::thread::sleep(delay);
            let response = wp510_release(&manager, &sid);
            (response, t0.elapsed())
        })
    };
    let first = spawn_release(Duration::ZERO);
    let second = spawn_release(Duration::from_millis(1000));
    let (first, first_at) = first.join().expect("release#1");
    let (second, second_at) = second.join().expect("release#2");
    for (name, response, at) in [("release#1", rpc(first), first_at), ("release#2", rpc(second), second_at)] {
        assert_eq!(code_of(&response).as_deref(), Some("DRAIN_STALLED"), "{name}：{response:?}");
        assert_eq!(response.get("drain_stalled"), Some(&PyValue::Bool(true)), "{name}");
        let epoch = response.get("drain_timeout_epoch").cloned().expect("epoch");
        assert!(matches!(epoch, PyValue::Int(_)), "{name}：epoch 為 g：{response:?}");
        let session = session_field(&response);
        assert_eq!(s_of(&session, "state").as_deref(), Some("RELEASED"), "{name}");
        assert_eq!(session.get("drain_stalled"), Some(&PyValue::Bool(true)), "{name}");
        assert_eq!(session.get("drain_timeout_epoch"), Some(&epoch), "{name}");
        assert!(
            at >= Duration::from_millis(2500) && at < Duration::from_millis(4500),
            "{name} 於首次期限（約 t0+3 s）回覆：{at:?}"
        );
    }
    let started = Instant::now();
    let third = rpc(wp510_release(&m.mgr, &sid));
    assert_eq!(code_of(&third).as_deref(), Some("DRAIN_STALLED"));
    assert!(started.elapsed() < Duration::from_millis(200), "期限已過立即回：{:?}", started.elapsed());
    m.serial.release_open(WP510R_R5_REAL);
    let a = rpc(attach.join().expect("attach thread"));
    assert_eq!(code_of(&a).as_deref(), Some("SESSION_RECOVERING"), "{a:?}");
    wait_until("遲到的關閉收斂", || {
        let session = m.session(&sid);
        session.get("drain_stalled") == Some(&PyValue::Bool(false))
            && session.get("drain_timeout_epoch") == Some(&PyValue::Null)
    });
    assert_eq!(m.state_of(&sid).as_deref(), Some("RELEASED"));
    let again = rpc(wp510_release(&m.mgr, &sid));
    assert_eq!(again.get("already_released"), Some(&PyValue::Bool(true)), "{again:?}");
    assert_eq!(device.open_count(), 1);
    assert_eq!(device.close_count(), 1);
}

/// WP1-4(a)（設計 v3 §1.2）：開埠阻塞時 shutdown，放行開埠成功 → A 回 SESSION_RECOVERING（五鍵），佔位不退休，handle 關閉一次，
/// shutdown 報告已排空（Python shutdown 取消作業 `origin/main:sw_core/session_manager.py:1187`，開埠返回判「operation」
/// `:3581`、`:3625-3631`）。
#[test]
fn wp510r_r5_wp1_4_shutdown_during_placeholder_open_reports_drained() {
    let m = wp510_b_placeholder_rig(false, false);
    let sid = m.mgr.resolve(WP510R_R5_SID).expect("佔位 session");
    let device = m.device(WP510R_R5_REAL);
    m.mgr.update_devices(&m.snapshot());
    m.serial.block_open(WP510R_R5_REAL);
    let attach_manager = m.mgr.clone();
    let attach_sid = sid.clone();
    let attach = std::thread::spawn(move || attach_manager.attach_released_placeholder(&attach_sid, false));
    wait_until("佔位 attach 停在 open_exclusive", || m.serial.open_attempts(WP510R_R5_REAL) == 1);
    let shutdown_manager = m.mgr.clone();
    let shutdown = std::thread::spawn(move || shutdown_manager.shutdown(Duration::from_secs(5)));
    wait_until("shutdown 取消帳本紀錄", || {
        m.mgr
            .custody_for_test(WP510R_R5_BY_ID)
            .is_some_and(|rec| rec.cancel == Some(sw_core::custody::CustodyCancel::Shutdown))
    });
    m.serial.release_open(WP510R_R5_REAL);
    let a = rpc(attach.join().expect("attach thread"));
    let drained = shutdown.join().expect("shutdown thread");
    assert_eq!(
        a.0.keys().map(String::as_str).collect::<Vec<_>>(),
        ["ok", "error_code", "drain_stalled", "drain_timeout_epoch", "session"],
        "{a:?}"
    );
    assert_eq!(code_of(&a).as_deref(), Some("SESSION_RECOVERING"));
    assert!(drained.iter().all(|(_, ok)| *ok), "shutdown 報告已排空：{drained:?}");
    assert_eq!(device.close_count(), 1);
    assert!(!device.is_open());
    assert!(m.mgr.resolve(WP510R_R5_SID).is_some(), "不退休");
    assert!(m.mgr.custody_for_test(WP510R_R5_BY_ID).is_none());
    assert!(device.take_tx().is_empty(), "零 TX");
}

/// G-1（設計 v1 §1.8）：一般 session 開埠在途時兩個並行 release 進入時都不是 RELEASED（Python `release_device`
/// `origin/main:sw_core/session_manager.py:3113-3138`），收尾後都回 REL（`:3210-3215`）：兩個回應位元組相同，都不帶
/// already_released。
#[test]
fn wp510r_r5_g1_concurrent_releases_of_inflight_attach_share_identical_reply() {
    let m = ManagerRig::single_target(&manager_yaml(&[&t_target("COM0", 1, "/s/by-id/usb-A")]));
    m.serial.block_open("/dev/ttyOLD");
    m.mgr.update_devices(&m.snapshot());
    m.mgr.bootstrap_attach();
    wait_until("COM0 開埠在途", || m.serial.open_attempts("/dev/ttyOLD") == 1);
    let spawn_release = || {
        let manager = m.mgr.clone();
        std::thread::spawn(move || wp510_release(&manager, "t:COM0"))
    };
    let first = spawn_release();
    wait_until("COM0 進入 RECOVERING", || m.state_of("COM0").as_deref() == Some("RECOVERING"));
    let second = spawn_release();
    std::thread::sleep(Duration::from_millis(200));
    m.serial.release_open("/dev/ttyOLD");
    let first = rpc(first.join().expect("release#1"));
    let second = rpc(second.join().expect("release#2"));
    assert!(is_ok(&first), "{first:?}");
    assert!(first.get("already_released").is_none(), "{first:?}");
    let wire = |d: &PyDict| sw_proto::pyjson::dumps(&PyValue::Dict(d.clone()), &sw_proto::pyjson::RPC_WIRE);
    assert_eq!(wire(&first), wire(&second), "兩個並行 release 的回應位元組相同");
    assert_eq!(m.state_of("COM0").as_deref(), Some("RELEASED"));
}

/// G-2（設計 v1 §1.8）：已 RELEASED 的 session 再 release 回 already_released，鍵序依 Python `release_device` 為
/// ok／already_released／session（`origin/main:sw_core/session_manager.py:3209`、`:3219`）。
#[test]
fn wp510r_r5_g2_already_released_reply_key_order_matches_python() {
    let m = ManagerRig::single_target(&manager_yaml(&[&t_target("COM0", 1, "/s/by-id/usb-A")])).ready();
    let first = rpc(wp510_release(&m.mgr, "t:COM0"));
    assert!(is_ok(&first), "{first:?}");
    let again = rpc(wp510_release(&m.mgr, "t:COM0"));
    assert_eq!(
        again.0.keys().map(String::as_str).collect::<Vec<_>>(),
        ["ok", "already_released", "session"],
        "{again:?}"
    );
    assert_eq!(again.get("already_released"), Some(&PyValue::Bool(true)));
}

// ---------------------------------------------------------------------------
// S2-PAR-wp510r R5 第 2 輪（審查 P8-a07fdfb-1）：佔位開埠一般 OSError 收尾必須保留已發生的裝置取消，並在退休的鎖區段內
// 核對原路徑。Python `attach_device` 的 OSError 分支在同一鎖區段內讀 `op.cancelled`，已取消即回 SESSION_RECOVERING、
// 不退休（`origin/main:sw_core/session_manager.py:3527-3567`）；`update_devices` 對路徑改變的在途作業設 `op.cancelled`
// （`:4031-4034`），RELEASED 的 by_id 不另起 attach。
// ---------------------------------------------------------------------------

const WP510R_R5_NEW_REAL: &str = "/dev/ttyG2";

/// 佔位 by_id 改指 `WP510R_R5_NEW_REAL` 後的裝置快照（新路徑登錄為實體裝置；rig 的快照登錄不變）。
fn wp510r_r5_retargeted_snapshot(m: &ManagerRig) -> Vec<(String, DeviceInfo)> {
    install_shell_replies(&m.serial.add_device(WP510R_R5_NEW_REAL));
    m.snapshot()
        .into_iter()
        .map(|(by_id, mut info)| {
            if by_id == WP510R_R5_BY_ID {
                info.real_path = WP510R_R5_NEW_REAL.to_string();
            }
            (by_id, info)
        })
        .collect()
}

/// 已取消的佔位 OSError 收尾：A 為 SESSION_RECOVERING（RELEASED 佔位快照、drain false/null），佔位與持久化 RELEASED 列
/// 保留、帳本與預先準備的 worker 皆空、新路徑零開埠、沒有正式 session。
fn wp510r_r5_assert_cancelled_oserror_kept(m: &ManagerRig, sid: &str, a: &PyDict) {
    assert_eq!(
        a.0.keys().map(String::as_str).collect::<Vec<_>>(),
        ["ok", "error_code", "drain_stalled", "drain_timeout_epoch", "session"],
        "Python 已取消的 OSError 回應鍵序（origin/main:sw_core/session_manager.py:3561-3567）：{a:?}"
    );
    assert_eq!(code_of(a).as_deref(), Some("SESSION_RECOVERING"), "{a:?}");
    assert_eq!(a.get("drain_stalled"), Some(&PyValue::Bool(false)));
    assert_eq!(a.get("drain_timeout_epoch"), Some(&PyValue::Null));
    let session = session_field(a);
    assert_eq!(s_of(&session, "state").as_deref(), Some("RELEASED"), "{session:?}");
    assert_eq!(s_of(&session, "session_id").as_deref(), Some(sid));
    assert_eq!(s_of(&session, "last_error"), None, "已取消不得寫 ATTACH_FAILED：{session:?}");
    assert_eq!(m.mgr.resolve(WP510R_R5_SID).as_deref(), Some(sid), "佔位保留");
    assert_eq!(m.mgr.released_placeholder(sid), Some(true), "不解除 RELEASED");
    assert!(m.load_state_now().released.contains_key(sid), "持久化 RELEASED 列保留：{}", m.state_text());
    assert!(m.mgr.custody_for_test(WP510R_R5_BY_ID).is_none(), "帳本無殘留紀錄");
    assert_eq!(m.mgr.prepared_len_for_test(), 0, "預先準備的交接 worker 已丟棄");
    std::thread::sleep(Duration::from_millis(300));
    assert_eq!(m.serial.open_attempts(WP510R_R5_NEW_REAL), 0, "不啟動新路徑交接");
    assert!(m.mgr.resolve("t:COM3").is_none(), "沒有正式 session");
    assert_eq!(m.state_of(WP510R_R5_SID).as_deref(), Some("RELEASED"));
}

/// P8-a07fdfb-1（審查第 1 輪 High）：佔位 W1 開埠阻塞時同一 by_id 改指新路徑（帳本寫入 cancel＝Device），舊開埠接著回
/// PermissionError。Python 已取消的 OSError 分支回 SESSION_RECOVERING、保留 RELEASED 佔位、不交接新路徑
/// （`origin/main:sw_core/session_manager.py:3529-3531`、`:3561-3567`）。
#[test]
fn wp510r_r5_p8_a07fdfb_1_device_cancel_during_open_then_oserror_keeps_placeholder() {
    let m = wp510_b_placeholder_rig(false, false);
    let sid = m.mgr.resolve(WP510R_R5_SID).expect("佔位 session");
    m.mgr.update_devices(&m.snapshot());
    m.serial.block_open(WP510R_R5_REAL);
    m.serial.fail_next_open(WP510R_R5_REAL, wp510_b_permission_error());

    let (attach_tx, attach_rx) = crossbeam_channel::bounded(1);
    let attach_manager = m.mgr.clone();
    let attach_sid = sid.clone();
    let attach = std::thread::spawn(move || {
        let _ = attach_tx.send(attach_manager.attach_released_placeholder(&attach_sid, false));
    });
    wait_until("佔位 attach 停在 open_exclusive", || m.serial.open_attempts(WP510R_R5_REAL) == 1);
    let token = m.mgr.custody_for_test(WP510R_R5_BY_ID).expect("開埠在途紀錄").token;

    m.mgr.update_devices(&wp510r_r5_retargeted_snapshot(&m));
    wait_until("裝置改指在帳本紀錄寫入取消", || {
        m.mgr.custody_for_test(WP510R_R5_BY_ID).is_some_and(|rec| {
            rec.token == token && rec.cancel == Some(sw_core::custody::CustodyCancel::Device)
        })
    });
    assert!(attach_rx.try_recv().is_err(), "開埠返回前 attach 不得回覆");

    m.serial.release_open(WP510R_R5_REAL);
    let a = rpc(attach_rx.recv_timeout(RPC).expect("attach 應完成"));
    attach.join().expect("attach thread");
    wp510r_r5_assert_cancelled_oserror_kept(&m, &sid, &a);
}

/// P8-a07fdfb-1（無 handle 退休的鎖內核對）：OSError 已結算帳本紀錄後、佔位 actor 取 manager 鎖退休之前（H-retire-pre-commit），
/// 同一 by_id 改指新路徑。退休須在鎖內核對原路徑，不符即視同已取消（Python 檢查與退休在同一鎖區段，改指只會落在檢查之前，
/// `origin/main:sw_core/session_manager.py:3527-3531`、`:4031-4034`），回 SESSION_RECOVERING、保留佔位、新路徑零開埠。
#[test]
fn wp510r_r5_p8_a07fdfb_1_device_change_before_oserror_retire_keeps_placeholder() {
    let m = wp510_b_placeholder_rig(false, false);
    let sid = m.mgr.resolve(WP510R_R5_SID).expect("佔位 session");
    m.mgr.update_devices(&m.snapshot());
    m.serial.fail_next_open(WP510R_R5_REAL, wp510_b_permission_error());
    let retargeted = wp510r_r5_retargeted_snapshot(&m);
    let hook_manager = m.mgr.clone();
    let (hooked_tx, hooked_rx) = crossbeam_channel::bounded(1);
    m.mgr.set_placeholder_retire_pre_commit_hook_for_test(
        WP510R_R5_SID,
        Box::new(move || {
            hook_manager.update_devices(&retargeted);
            let _ = hooked_tx.send(());
        }),
    );

    let a = rpc(m.mgr.attach_released_placeholder(&sid, false));
    hooked_rx.try_recv().expect("退休提交前已改指");
    assert_eq!(m.serial.open_attempts(WP510R_R5_REAL), 1);
    wp510r_r5_assert_cancelled_oserror_kept(&m, &sid, &a);
}

/// P8-a07fdfb-1（審查第 2 輪殘留，High）：OSError 已結算帳本紀錄後、佔位 actor 取 manager 鎖退休之前（H-retire-pre-commit），
/// 同一 by_id 先改指新路徑、再改回原路徑。Python 的 `update_devices` 對在途作業的取消不可撤銷（路徑恢復不清除
/// `op.cancelled`，`origin/main:sw_core/session_manager.py:4031-4034`），OSError 收尾在同一鎖區段讀到已取消即回
/// SESSION_RECOVERING、不退休（`:3529-3531`、`:3561-3567`）。只比對退休當下的路徑字串分不出「改指再改回」：須保留本次作業的
/// 取消到退休提交。斷言佔位保留、原路徑開埠總次數仍為 1（不交接再開）、新路徑零開埠。
#[test]
fn wp510r_r5_p8_a07fdfb_1_retarget_and_back_before_oserror_retire_keeps_placeholder() {
    let m = wp510_b_placeholder_rig(false, false);
    let sid = m.mgr.resolve(WP510R_R5_SID).expect("佔位 session");
    m.mgr.update_devices(&m.snapshot());
    m.serial.fail_next_open(WP510R_R5_REAL, wp510_b_permission_error());
    let retargeted = wp510r_r5_retargeted_snapshot(&m);
    let original = m.snapshot();
    let hook_manager = m.mgr.clone();
    let (hooked_tx, hooked_rx) = crossbeam_channel::bounded(1);
    m.mgr.set_placeholder_retire_pre_commit_hook_for_test(
        WP510R_R5_SID,
        Box::new(move || {
            hook_manager.update_devices(&retargeted);
            hook_manager.update_devices(&original);
            let _ = hooked_tx.send(());
        }),
    );

    let a = rpc(m.mgr.attach_released_placeholder(&sid, false));
    hooked_rx.try_recv().expect("退休提交前已改指再改回");
    assert_eq!(m.serial.open_attempts(WP510R_R5_REAL), 1);
    wp510r_r5_assert_cancelled_oserror_kept(&m, &sid, &a);
    assert_eq!(m.serial.open_attempts(WP510R_R5_REAL), 1, "原路徑不得由交接 worker 再開埠");
}

/// P8-a07fdfb-1 對照組：退休提交前的裝置更新若沒有改變本 by_id 的路徑（同一快照重掃），Python 不取消作業
/// （`origin/main:sw_core/session_manager.py:4031-4034` 只在路徑不同時 `op.cancelled.set()`），OSError 照 B-7 退休並交接
/// dynamic 再開一次（`:3529-3560`）。防止以「任何裝置更新都取消」矯枉過正。
#[test]
fn wp510r_r5_p8_a07fdfb_1_same_path_update_before_oserror_retire_still_retires() {
    let m = wp510_b_placeholder_rig(false, false);
    let sid = m.mgr.resolve(WP510R_R5_SID).expect("佔位 session");
    m.mgr.update_devices(&m.snapshot());
    m.serial.fail_next_open(WP510R_R5_REAL, wp510_b_permission_error());
    let same = m.snapshot();
    let hook_manager = m.mgr.clone();
    let (hooked_tx, hooked_rx) = crossbeam_channel::bounded(1);
    m.mgr.set_placeholder_retire_pre_commit_hook_for_test(
        WP510R_R5_SID,
        Box::new(move || {
            hook_manager.update_devices(&same);
            let _ = hooked_tx.send(());
        }),
    );

    let a = rpc(m.mgr.attach_released_placeholder(&sid, false));
    hooked_rx.try_recv().expect("退休提交前已重掃同一快照");
    assert!(is_ok(&a), "路徑未變不取消，OSError 照 B-7 退休：{a:?}");
    let snapshot = session_field(&a);
    assert_eq!(s_of(&snapshot, "state").as_deref(), Some("DETACHED"));
    assert_eq!(s_of(&snapshot, "last_error").as_deref(), Some("ATTACH_FAILED:PermissionError"));
    wait_until("佔位交接的 dynamic 開埠", || m.serial.open_attempts(WP510R_R5_REAL) == 2);
    assert_eq!(m.mgr.resolve(WP510R_R5_SID), None, "舊佔位已退休");
}

// ---------------------------------------------------------------------------
// S2-PAR-wp510r R6：失敗情境目錄補齊（設計 §1；DR-1 h、DR-1 a）
// ---------------------------------------------------------------------------

/// DR-1(h1)(h2)（設計 v3 §1.8、§3.1.5 W1 Raw）：佔位 Raw 開埠在途時佔位 actor 在 catch_unwind 之外結束；開埠工作
/// 成功後把 handle 存回帳本 `Held{Session(PH)}`，`PlaceholderOpened` 送不出去（actor 已不在）時由開埠工作自行轉
/// Closing 並關閉：(h1) closed:true 紀錄移除、終態 Closed、不登記未確認關閉；(h2) closed:false 轉 CloseFailed、
/// 進未確認集合、handle 仍開著。兩者都不退休佔位、不開第二次、零 TX（Python 在鎖內移交、沒有通道失敗點，
/// `origin/main:sw_core/session_manager.py:4374-4384`、`:5695-5707`；關閉失敗保留阻擋 `:4322-4332`）。
#[test]
fn wp510r_r6_dr1_h_placeholder_raw_open_undelivered_closes_via_ledger() {
    for closed in [true, false] {
        let m = wp510_b_placeholder_rig(false, false);
        let sid = m.mgr.resolve(WP510R_R5_SID).expect("佔位 session");
        let device = m.device(WP510R_R5_REAL);
        if !closed {
            device.fail_close(PalError::io(5, "[Errno 5] Input/output error"));
        }
        m.mgr.update_devices(&m.snapshot());
        m.serial.block_open(WP510R_R5_REAL);
        let attach_manager = m.mgr.clone();
        let attach_sid = sid.clone();
        let attach = std::thread::spawn(move || attach_manager.attach_released_placeholder(&attach_sid, false));
        wait_until("佔位 attach 停在 open_exclusive", || m.serial.open_attempts(WP510R_R5_REAL) == 1);
        let rec = m.mgr.custody_for_test(WP510R_R5_BY_ID).expect("佔位 Raw 開埠的帳本紀錄");
        let token = rec.token;
        // 佔位 actor 在開埠在途時結束（executor 在 catch_unwind 之外 unwind）。
        m.mgr.set_executor_unwind_hook_for_test(
            &sid,
            Box::new(|| panic!("測試注入：佔位 actor 在 PlaceholderOpened 送達前結束")),
        );
        let ctrl = m.mgr.actor_control_for_test(&sid).expect("佔位 actor");
        let (tx, _rx) = crossbeam_channel::bounded(1);
        ctrl.send(sw_core::mailbox::ControlEvent::Request { req: SessionRequest::GetState, reply: tx })
            .expect("送入查詢以觸發 unwind");
        std::thread::sleep(Duration::from_millis(150));
        m.serial.release_open(WP510R_R5_REAL);
        let _ = attach.join().expect("attach thread 不應 panic");

        if closed {
            wait_until("(h1) 開埠工作自行關閉並結算", || {
                m.mgr.terminal_for_test(token).is_some() && m.mgr.custody_for_test(WP510R_R5_BY_ID).is_none()
            });
            assert_eq!(
                m.mgr.terminal_for_test(token).map(|t| t.handle),
                Some(sw_core::custody::TerminalHandle::Closed),
                "(h1) 終態 Closed"
            );
            assert!(!device.is_open(), "(h1) handle 已關閉");
            assert!(!m.mgr.unconfirmed_close_for_test(WP510R_R5_BY_ID), "(h1) 不登記未確認關閉");
        } else {
            wait_until("(h2) 關閉失敗轉 CloseFailed", || {
                matches!(
                    m.mgr.custody_for_test(WP510R_R5_BY_ID).map(|rec| rec.phase),
                    Some(sw_core::custody::CustodyPhaseSnapshot::CloseFailed { .. })
                )
            });
            let rec = m.mgr.custody_for_test(WP510R_R5_BY_ID).expect("CloseFailed 紀錄保留");
            assert_eq!(rec.token, token, "(h2) 同一 token");
            assert!(m.mgr.unconfirmed_close_for_test(WP510R_R5_BY_ID), "(h2) 進未確認集合");
            assert!(device.is_open(), "(h2) handle 仍開著");
        }
        assert_eq!(device.close_count(), 1, "只關閉一次（closed={closed}）");
        assert_eq!(device.open_count(), 1, "不開第二次（closed={closed}）");
        assert!(m.mgr.resolve(WP510R_R5_SID).is_some(), "不退休佔位（closed={closed}）");
        assert!(device.take_tx().is_empty(), "零 TX（closed={closed}）");
    }
}

/// DR-5(b)（設計 v3 §1.8、§3.1.7 `BridgeOpened`（Raw）：Pending 延後、Permanent 只關閉不退休）：佔位 Raw 開埠在凍結前
/// 送出、開埠結果在凍結後交付。Permanent：佔位 actor 只關閉（`ClaimForStop(Freeze)`），配對回條後紀錄移除、終態
/// Closed，不退休、零 TX、沒有 IoWorker（不建正式 session），attach 回 SESSION_RECOVERING。Pending：開埠結果延後、
/// handle 留在帳本；撤銷凍結後重播並照常退休交接（Python 凍結期間不推進 attach，`origin/main:sw_core/session_manager.py:1127-1137`、
/// `:1181-1239`）。
#[test]
fn wp510r_r6_dr5_b_placeholder_raw_opened_after_freeze() {
    for permanent in [true, false] {
        let m = wp510_b_placeholder_rig(false, false);
        let sid = m.mgr.resolve(WP510R_R5_SID).expect("佔位 session");
        let device = m.device(WP510R_R5_REAL);
        m.mgr.update_devices(&m.snapshot());
        m.serial.block_open(WP510R_R5_REAL);
        let attach_manager = m.mgr.clone();
        let attach_sid = sid.clone();
        let attach = std::thread::spawn(move || attach_manager.attach_released_placeholder(&attach_sid, false));
        wait_until("佔位 attach 停在 open_exclusive", || m.serial.open_attempts(WP510R_R5_REAL) == 1);
        let token = m.mgr.custody_for_test(WP510R_R5_BY_ID).expect("佔位 Raw 開埠紀錄").token;
        let gate = m.reg.admission_gate();
        let freeze = if permanent {
            gate.freeze();
            None
        } else {
            match gate.freeze_if_idle("wp510r-r6-dr5b") {
                sw_core::admission::FreezeOutcome::Frozen { token, .. } => Some(token),
                outcome => panic!("應建立暫時凍結：{outcome:?}"),
            }
        };
        m.serial.release_open(WP510R_R5_REAL);
        match freeze {
            None => {
                let a = rpc(attach.join().expect("attach thread"));
                assert_eq!(code_of(&a).as_deref(), Some("SESSION_RECOVERING"), "{a:?}");
                wait_until("Permanent：只關閉並結算", || {
                    m.mgr.custody_for_test(WP510R_R5_BY_ID).is_none() && m.mgr.terminal_for_test(token).is_some()
                });
                assert_eq!(
                    m.mgr.terminal_for_test(token).map(|t| t.handle),
                    Some(sw_core::custody::TerminalHandle::Closed),
                    "配對回條結算為 Closed"
                );
                assert_eq!(device.close_count(), 1);
                assert!(!device.is_open());
                assert!(m.mgr.resolve(WP510R_R5_SID).is_some(), "Permanent 不退休");
                assert!(m.mgr.resolve("t:COM3").is_none(), "不建正式 session（沒有 IoWorker）");
                assert!(device.take_tx().is_empty(), "零 TX");
            }
            Some(freeze) => {
                std::thread::sleep(Duration::from_millis(300));
                assert!(!attach.is_finished(), "Pending：開埠結果延後，attach 尚未回覆");
                assert!(device.is_open(), "handle 留在帳本");
                assert_eq!(m.mgr.custody_for_test(WP510R_R5_BY_ID).map(|r| r.token), Some(token));
                assert!(m.mgr.resolve(WP510R_R5_SID).is_some(), "Pending 期間不退休");
                assert!(gate.thaw(&freeze), "撤銷凍結");
                // 交接延後的事件在下一個事件分派時重播。
                let _ = m.mgr.session_queue_lens(&sid);
                let a = rpc(attach.join().expect("attach thread"));
                assert!(is_ok(&a), "撤銷凍結後照常退休交接：{a:?}");
                assert!(m.mgr.resolve(WP510R_R5_SID).is_none(), "佔位已退休");
                assert_eq!(device.close_count(), 0, "交接沿用原 handle");
                assert_eq!(device.open_count(), 1);
            }
        }
    }
}

/// DR-5(a)（設計 v3 §1.8、§3.1.7 `CustodyOffer`：Permanent 只關閉；manager 凍結 Permanent 時寫 cancel(Freeze)）：既有
/// fallback session 重插後的 #181 再解析（W6）開埠在凍結前送出；正式交接凍結（`freeze_if_idle` → manager
/// `freeze_for_handoff` → `commit_handoff`，同 `Service::handoff_stop`）成立後開埠才返回。W6 的 handle 只關閉、不交給
/// actor adopt：配對回條（紀錄移除、終態 Closed）、零 TX（沒有 PROBE、沒有登入）、沒有 IoWorker（session 沒有
/// attached_real_path）、不重新開埠（Python 凍結期間不推進 attach／readiness，`origin/main:sw_core/session_manager.py:1127-1137`、
/// `:1181-1239`）。
#[test]
fn wp510r_r6_dr5_a_w6_open_returns_after_handoff_commit_closes_only() {
    let m = ManagerRig::with(&manager_yaml(&[]), LoadedState::default(), None);
    let real = fallback_session_then_unplug(&m, "/s/by-id/usb-D");
    m.serial.block_open(&real);
    m.mgr.update_devices(&m.snapshot());
    wait_until("再偵測卡在 open", || m.serial.open_attempts(&real) == 2);
    let sid = m.mgr.resolve("COM0").expect("COM0 session");
    let blocked = m.mgr.custody_for_test("/s/by-id/usb-D").expect("W6 開埠在途須登記帳本紀錄");
    assert!(wp510r_r4_is_w6_opening(
        &blocked,
        m.mgr.actor_id_for_test(&sid).expect("actor 身分"),
    ), "{blocked:?}");
    let token = blocked.token;
    let device = m.device(&real);
    let closes_before = device.close_count();
    let tx_before = device.tx_len();

    let gate = m.reg.admission_gate();
    let freeze = match gate.freeze_if_idle("update") {
        sw_core::admission::FreezeOutcome::Frozen { token, .. } => token,
        outcome => panic!("閒置的 gate 應新建交接凍結：{outcome:?}"),
    };
    let _unconverged = m.mgr.freeze_for_handoff(Duration::from_millis(500));
    assert!(gate.commit_handoff(&freeze), "交接提交 → Permanent");

    m.serial.release_open(&real);
    wait_until("凍結後返回的 W6 handle 只關閉並結算", || {
        m.mgr.custody_for_test("/s/by-id/usb-D").is_none() && m.mgr.terminal_for_test(token).is_some()
    });
    assert_eq!(
        m.mgr.terminal_for_test(token).map(|t| t.handle),
        Some(sw_core::custody::TerminalHandle::Closed),
        "配對回條結算為 Closed"
    );
    assert_eq!(device.close_count(), closes_before + 1, "關閉一次");
    std::thread::sleep(Duration::from_millis(300));
    assert_eq!(device.tx_len(), tx_before, "零 TX（沒有 PROBE、沒有登入）");
    assert_eq!(m.serial.open_attempts(&real), 2, "不重新開埠");
    let session = m.session("COM0");
    assert_eq!(session.get("attached_real_path"), Some(&PyValue::Null), "沒有 IoWorker：{session:?}");
}

/// 第 20 項 r3-M1／DR-9：actor 建構失敗且關閉卡住時，manager 查詢、release 與 shutdown 各自有界。
#[test]
fn wp510r_i20_actor_spawn_failure_close_pause_keeps_manager_responsive() {
    let _guard = SPAWN_TEST_LOCK.lock().unwrap_or_else(|e| e.into_inner());
    const BY_ID: &str = "/s/by-id/usb-I20";
    const REAL: &str = "/dev/ttyI20Spawn";
    const SID: &str = "gone-template:COM120";
    let mut loaded = LoadedState::default();
    loaded.released.insert(SID.into(), released_row(BY_ID));
    let target = t_target("COM0", 1, "/s/by-id/i20-offline");
    let m = ManagerRig::with(&manager_yaml(&[&target]), loaded, None);
    m.add_device_at(BY_ID, REAL);
    let dev = m.device(REAL);
    install_shell_replies(&dev);
    dev.pause_next_close();
    sw_core::actor::inject_actor_spawn_fail_for("t:COM120");
    m.mgr.update_devices(&m.snapshot());
    assert!(is_ok(&m.mgr.attach_released_placeholder(SID, false).unwrap()));
    assert!(dev.wait_paused(Duration::from_secs(5)), "建構失敗後關閉已暫停");

    let (query_tx, query_rx) = crossbeam_channel::bounded(1);
    let mgr = m.mgr.clone();
    let query = std::thread::spawn(move || {
        let start = Instant::now();
        let ids = mgr.session_ids();
        let token = mgr.custody_for_test(BY_ID).map(|rec| rec.token);
        query_tx.send((ids, token, start.elapsed())).unwrap();
    });
    let (release_tx, release_rx) = crossbeam_channel::bounded(1);
    let mgr = m.mgr.clone();
    let release = std::thread::spawn(move || {
        let start = Instant::now();
        let reply = mgr.route("t:COM0", SessionRequest::Release { source: "agent".into(), reason: PyValue::Null }, RPC);
        release_tx.send((reply, start.elapsed())).unwrap();
    });
    let query_result = query_rx.recv_timeout(Duration::from_secs(1));
    let release_result = release_rx.recv_timeout(Duration::from_secs(1));
    let (shutdown_tx, shutdown_rx) = crossbeam_channel::bounded(1);
    let mgr = m.mgr.clone();
    let shutdown = std::thread::spawn(move || {
        let start = Instant::now();
        let report = mgr.shutdown(Duration::from_millis(300));
        shutdown_tx.send((report, start.elapsed())).unwrap();
    });
    let shutdown_result = shutdown_rx.recv_timeout(Duration::from_secs(1));
    dev.release_pause();
    query.join().unwrap();
    release.join().unwrap();
    shutdown.join().unwrap();
    sw_core::actor::inject_actor_spawn_fail_clear();

    let (ids, token, query_elapsed) = query_result.expect("建構失敗的 close 暫停不能阻塞 manager 查詢");
    assert!(!ids.contains(&"t:COM120".to_string()));
    assert!(query_elapsed < Duration::from_secs(1), "DR-9 查詢：{query_elapsed:?}");
    let (reply, elapsed) = release_result.expect("其他 session release 不得卡在 manager 鎖");
    assert!(is_ok(&reply.unwrap()));
    assert!(elapsed < Duration::from_secs(1), "release：{elapsed:?}");
    let (report, elapsed) = shutdown_result.expect("shutdown 不得卡在 manager 鎖");
    assert!(elapsed < Duration::from_secs(1), "shutdown：{elapsed:?}");
    assert!(report.iter().any(|(key, ok)| key == &format!("attach:{BY_ID}") && !ok), "{report:?}");
    wait_until("建構失敗的 handle 配對回條", || !dev.is_open() && m.mgr.custody_for_test(BY_ID).is_none());
    assert_eq!(dev.close_count(), 1, "handle 恰好關閉一次");
    assert_eq!(m.mgr.terminal_for_test(token.expect("Closing token")).unwrap().handle, sw_core::custody::TerminalHandle::Closed);
}
