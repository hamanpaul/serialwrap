//! P5-T16：`SessionManager`（裝置排序 #100、動態 session、selector、管理操作）紅燈測試。
//!
//! 測試以真的 `SessionManager::new` 組裝（見 `support::ManagerRig`）；紅燈階段函式本體為 `s21_todo!`，
//! 因此本檔的測試應於執行期失敗（`not implemented: slice21 T<n> <symbol>`），而非編譯錯誤。
#![cfg(feature = "test-fakes")]

mod support;

use std::time::Duration;

use support::*;
use sw_core::session_core::{SessionRequest, SessionState};
use sw_core::state_store::{LoadedState, ReleasedRow};
use sw_pal::fakes::serial::FakeWriteStep;
use sw_pal::traits::{
    DeviceInfo, DevicePath, OpenError, PalError, SerialBackend, SerialConfig, WatcherEvent,
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
fn detect_skips_passthrough_console_unlock_and_unsupported() {
    // 偵測候選只含 command-capable 且不需 console_unlock_env／post_login_cmd 的模板；
    // 其餘（passthrough、console_unlock、post_login）即使 prompt 可匹配也不得被嘗試。
    const DETECT_TEMPLATES: &str = "profiles:\n  pt-template:\n    platform: passthrough\n    prompt_regex: '.*'\n    login_regex: '$^'\n    password_regex: '$^'\n    ready_probe: ''\n  cu-template:\n    platform: shell\n    prompt_regex: '(?m)^root@x:.*# '\n    console_unlock_env: SW_UNLOCK\n    timeout_s: 2.0\n  pl-template:\n    platform: shell\n    prompt_regex: '(?m)^root@x:.*# '\n    post_login_cmd: sh\n    timeout_s: 2.0\n";
    let m = ManagerRig::with(DETECT_TEMPLATES, LoadedState::default(), None);
    m.devices(&["/s/by-id/usb-D"]);
    m.reply_probe(&m.real_of("/s/by-id/usb-D"), b"root@x:/# ");
    m.mgr.update_devices(&m.snapshot());
    m.mgr.bootstrap_attach();
    assert_eq!(m.wait_profile_source("COM0"), "fallback");
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
    let m2 = ManagerRig::with(&manager_yaml(&[]), LoadedState::default(), None);
    m2.devices(&["/s/by-id/usb-S"]);
    // 只回應 login 的 prompt，不回應 ready_probe nonce，使 session 停在 READY 之前。
    m2.reply_probe(&m2.real_of("/s/by-id/usb-S"), b"root@t:/# ");
    m2.mgr.update_devices(&m2.snapshot());
    m2.mgr.bootstrap_attach();
    wait_until("偵測完成（非 READY）", || {
        m2.state_of("COM0").is_some()
    });
    m2.retarget_by_id("/s/by-id/usb-S", "/dev/ttyMOVED");
    std::thread::sleep(Duration::from_millis(100));
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

    // last_error 為 None → 兩欄皆 null（不再回 NOT_READY）。
    // 不回應 prompt 時 Python／Rust 皆會把 probe 失敗碼（PROMPT_UNAVAILABLE）寫入 last_error；
    // 以 Slice 2.1 能力閘（post_login_cmd：不自動登入、不送 TX）取得「ATTACHED 且 last_error 為 None」的 capable session。
    const GATED_PROFILE: &str = "profiles:\n  g:\n    platform: shell\n    prompt_regex: '(?m)^root@t:.*# '\n    post_login_cmd: sh\n    timeout_s: 2.0\ntargets:\n  - {com: COM0, profile: g, act_no: 1, alias: g+1, device_by_id: /s/by-id/usb-A}\n";
    let m = rig_s(GATED_PROFILE);
    let dev = m.add_device_at("/s/by-id/usb-A", "/dev/ttyOLD");
    let _ = dev;
    m.mgr.update_devices(&m.snapshot());
    m.mgr.bootstrap_attach();
    m.wait_state("COM0", "ATTACHED");
    let sid = m.mgr.resolve("COM0").expect("COM0 session");
    let r = rpc(m.mgr.route(&sid, SessionRequest::SessionAttach, RPC));
    assert_eq!(code_of(&r).as_deref(), Some("SESSION_NOT_READY"));
    assert_eq!(r.get("last_error"), Some(&PyValue::Null));
    assert_eq!(r.get("last_error_detail"), Some(&PyValue::Null));

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
    // - 其他缺定義的 template（如 uboot-template）→ 不實體化、WARNING、保留持久化阻擋（裝置上線也不 open）。
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
    assert_eq!(m.mgr.resolve("COM4"), None, "uboot-template 缺定義不得降級實體化");
    assert!(m.warnings().iter().any(|w| w
        == "released session 的 template 不存在（session=uboot-template:COM4、profile=uboot-template），保留持久化阻擋"));
    assert!(
        m.load_state_now().released.contains_key("uboot-template:COM4"),
        "未實體化的 released 列須保留在 state.json"
    );
    m.add_device_at("/s/by-id/usb-R", "/dev/ttyR");
    m.add_device_at("/s/by-id/usb-U", "/dev/ttyU");
    m.mgr.update_devices(&m.snapshot());
    m.mgr.bootstrap_attach();
    std::thread::sleep(Duration::from_millis(100));
    assert_eq!(m.serial.open_attempts("/dev/ttyR"), 0);
    assert_eq!(m.serial.open_attempts("/dev/ttyU"), 0, "未實體化列仍阻擋該 tty");
    assert_eq!(m.mgr.counts().0, 1);
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
        effects.iter().any(|e| matches!(e, sw_core::session_core::Effect::OpenBridge { dev, .. } if dev.0 == "/fake/tty-new")),
        "仍需 attach → 以新路徑再送 OpenBridge"
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
