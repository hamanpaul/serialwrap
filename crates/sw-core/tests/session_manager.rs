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
use sw_pal::traits::{DeviceInfo, PalError, WatcherEvent};
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
    m.devices(&["/s/by-id/usb-A", "/s/by-id/usb-B", "/s/by-id/usb-C"]);
    m.mgr.prepare_dynamic_rank(&m.device_keys());
    m.mgr.update_devices(&m.snapshot());
    m.mgr.bootstrap_attach();
    m.wait_sessions(3);
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
    // PROBE handle 的 CloseReceipt{closed:true, excl_cleared:true} 收到後才 open(real)。
    let m = ManagerRig::with(&manager_yaml(&[]), LoadedState::default(), None);
    m.devices(&["/s/by-id/usb-D"]);
    let real = m.real_of("/s/by-id/usb-D");
    m.reply_probe(&real, b"root@prpl:/# ");
    m.mgr.update_devices(&m.snapshot());
    m.mgr.bootstrap_attach();
    assert_eq!(m.wait_profile_source("COM0"), "detected");
    let ev = m.events();
    let want = [
        format!("open:{real}"),
        format!("close:{real}"),
        format!("open:{real}"),
    ];
    assert!(
        ev.len() >= 3,
        "至少應有 open(PROBE)、close(PROBE)、open(real)：{ev:?}"
    );
    assert_eq!(
        &ev[..3],
        &want[..],
        "時序應為 open(PROBE)→close(PROBE)→open(real)"
    );
    let probe_rows = m
        .wal_rows()
        .into_iter()
        .filter(|r| s_of(r, "com").as_deref() == Some("PROBE"))
        .count();
    assert!(probe_rows >= 2, "偵測的 RX／TX 應以 com == PROBE 寫入 WAL");

    // 變體：excl_cleared:false 時仍嘗試 open(real)，得 Held → DETACHED＋ATTACH_FAILED:DeviceHeldError（不得靜默吞掉）。
    let m2 = ManagerRig::with(&manager_yaml(&[]), LoadedState::default(), None);
    m2.devices(&["/s/by-id/usb-D"]);
    let real2 = m2.real_of("/s/by-id/usb-D");
    m2.reply_probe(&real2, b"root@prpl:/# ");
    m2.serial.fail_excl_clear(&real2);
    m2.mgr.update_devices(&m2.snapshot());
    m2.mgr.bootstrap_attach();
    wait_until("COM0 以 DeviceHeldError 失敗", || {
        m2.session_last_error("COM0").as_deref() == Some("ATTACH_FAILED:DeviceHeldError")
    });
    assert_eq!(m2.state_of("COM0").as_deref(), Some("DETACHED"));
}

#[test]
fn sticky_persisted_only_when_ready_and_path_unchanged() {
    // READY 且 real_path 未變 → state.json.profile_detected[by_id] 寫入。
    let m = ManagerRig::with(&manager_yaml(&[]), LoadedState::default(), None);
    m.devices(&["/s/by-id/usb-S"]);
    install_shell_replies(&m.device(&m.real_of("/s/by-id/usb-S")));
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
    assert!(
        !m2.state_text().contains("profile_detected"),
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

    // 無 by_id → DEVICE_NOT_BOUND。
    let m = rig_s(&manager_yaml(&[
        "{com: COM0, profile: t, act_no: 1, alias: t+1}",
    ]));
    let sid = m.mgr.resolve("COM0").expect("COM0 session");
    let r = rpc(m.mgr.route(&sid, SessionRequest::SessionAttach, RPC));
    assert_eq!(code_of(&r).as_deref(), Some("DEVICE_NOT_BOUND"));

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
    let m = rig_s(SHELL_PROFILE);
    let dev = m.add_device_at("/s/by-id/usb-A", "/dev/ttyOLD");
    let _ = dev; // 不回應任何 prompt：停在 ATTACHED，無 last_error。
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
    let _ = m
        .mgr
        .route(&sid0, SessionRequest::Submit(agent_submit("sleep 1")), RPC);

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
// 測試輔助（本檔專用）。
// ---------------------------------------------------------------------------

/// `bind` 的 RPC 呼叫（Result 解包）。
fn rpc_bind(m: &ManagerRig, selector: &str, by_id: &str) -> PyDict {
    rpc(m.mgr.bind(selector, by_id))
}
