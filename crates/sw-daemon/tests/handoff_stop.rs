//! P8 Task 14：Rust daemon 承接 SC-11 交接停止 `daemon.handoff_stop`（R1-F1；arch §6.2 第 7 步、計畫 09 Task 14）。
//!
//! 行為基準＝origin/main Python：`sw_core/service.py::handoff_stop`／`rpc()` 的 admission、`sw_core/admission.py`、
//! `SessionManager.freeze_for_handoff`、`sw_core/daemon.py` 的 `HANDOFF_DRAIN_GRACE_S`（3 秒排空窗口），以及
//! `tests/test_stage0_handoff_stop.py` 的雙邊案例。
//!
//! - Service 層（`ServiceRig`：真 `SessionManager`＋`Service`，FakeSerial／FakeClock）：busy 判定、凍結後零副作用、
//!   TX fence 逾時的撤銷、shutdown 凍結不被撤銷、flash sync-probe 與交接的競態、四個 reason 同語意。
//! - daemon 層（`run_with_hooks` in-process，FakeSerial＋原生時鐘）：預檢後才開始的 `file.push` 擋下交接、放行後
//!   交接成立、framing 層回應無 `version`、排空窗口內有副作用請求回 `FLUSHED_BY_SHUTDOWN`／`handoff_frozen` 而
//!   `health.ping` 仍可回應、窗口結束才進停止序且 exit 0。
//! - 行程層（`CARGO_BIN_EXE_serialwrapd`＋PTY 假 DUT）：交接成立後舊 daemon 退出才啟動繼任者，全程連續取樣 tty
//!   holder，任一時刻至多一個持有者（兩個 daemon 不同時持有 UART／socket）。
#![cfg(feature = "test-fakes")]

mod support;

use std::collections::BTreeMap;
use std::panic::{catch_unwind, AssertUnwindSafe};
use std::path::{Path, PathBuf};
use std::sync::atomic::{AtomicBool, AtomicUsize, Ordering};
use std::sync::{Arc, Barrier, Mutex};
use std::thread;
use std::time::{Duration, Instant};

use support::*;
use sw_core::file_transfer::templates as t;
use sw_core::session_core::SessionState;
use sw_pal::fakes::serial::{FakeDevice, FakeWriteStep};
use sw_pal::traits::DeviceInfo;
use sw_proto::pycompat::a2b_base64_lenient;
use sw_proto::pydict;
use sw_proto::pyjson::{self, PyDict, PyValue};

/// Python `HANDOFF_DRAIN_GRACE_S`（`sw_core/daemon.py:134`）；黑箱不得 import 產品常數，於此重述。
const HANDOFF_DRAIN_GRACE: Duration = Duration::from_secs(3);
/// 停止序總上限（flash join 2.0 s＋步驟 3～5 共用 5 s）＋排程餘裕。
const STOP_CAP: Duration = Duration::from_millis(2_000 + 5_000 + 1_500);
const BUSY_ROW_KEYS: [&str; 7] = [
    "com",
    "state",
    "foreground_busy",
    "outstanding_commands",
    "interactive_session_id",
    "recovering",
    "ops",
];

// ---------------------------------------------------------------------------
// 共用 helper
// ---------------------------------------------------------------------------

fn keys(d: &PyDict) -> Vec<&str> {
    d.0.keys().map(String::as_str).collect()
}

fn wire(d: &PyDict) -> String {
    pyjson::dumps(&PyValue::Dict(d.clone()), &pyjson::RPC_WIRE)
}

fn s<'a>(d: &'a PyDict, k: &str) -> Option<&'a str> {
    d.get(k).and_then(PyValue::as_str)
}

fn is_hex32(v: &str) -> bool {
    v.len() == 32 && v.bytes().all(|b| b.is_ascii_digit() || (b'a'..=b'f').contains(&b))
}

fn reason(r: &str) -> PyDict {
    pydict! { "reason" => r }
}

/// 交接成功：`{"ok":true,"stopping":true,"handoff_token":<32 hex>}`，無 `version`（rpc_schema `version_absent_for`）。
fn assert_granted(r: &PyDict, what: &str) -> String {
    assert_eq!(keys(r), ["ok", "stopping", "handoff_token"], "{what}：{}", wire(r));
    assert_eq!(r.get("ok"), Some(&PyValue::Bool(true)), "{what}");
    assert_eq!(r.get("stopping"), Some(&PyValue::Bool(true)), "{what}");
    let token = s(r, "handoff_token").unwrap_or_default().to_string();
    assert!(is_hex32(&token), "{what}：token 須為 32 hex：{token}");
    token
}

/// 交接忙碌：`{"ok":false,"error_code":<code>,"busy":[{com,state,foreground_busy,outstanding_commands,
/// interactive_session_id,recovering,ops}]}`，無 `version`；回傳 busy 列。
fn assert_refused(r: &PyDict, code: &str, what: &str) -> Vec<PyDict> {
    assert_eq!(keys(r), ["ok", "error_code", "busy"], "{what}：{}", wire(r));
    assert_eq!(r.get("ok"), Some(&PyValue::Bool(false)), "{what}");
    assert_eq!(s(r, "error_code"), Some(code), "{what}：{}", wire(r));
    let rows: Vec<PyDict> = match r.get("busy") {
        Some(PyValue::List(items)) => items
            .iter()
            .map(|v| match v {
                PyValue::Dict(d) => d.clone(),
                other => panic!("{what}：busy 列須為 object：{other:?}"),
            })
            .collect(),
        other => panic!("{what}：busy 須為 list：{other:?}"),
    };
    assert!(!rows.is_empty(), "{what}：busy 不得為空");
    for row in &rows {
        assert_eq!(keys(row), BUSY_ROW_KEYS, "{what}：{}", wire(row));
    }
    rows
}

fn ops(row: &PyDict) -> Vec<String> {
    match row.get("ops") {
        Some(PyValue::List(items)) => items.iter().filter_map(PyValue::as_str).map(str::to_string).collect(),
        _ => Vec::new(),
    }
}

fn row_for<'a>(rows: &'a [PyDict], com: &str) -> &'a PyDict {
    rows.iter()
        .find(|r| s(r, "com") == Some(com))
        .unwrap_or_else(|| panic!("busy 缺 {com}：{rows:?}"))
}

fn flushed_frozen(r: &PyDict) -> bool {
    r.get("ok") == Some(&PyValue::Bool(false))
        && s(r, "error_code") == Some("FLUSHED_BY_SHUTDOWN")
        && s(r, "reason") == Some("handoff_frozen")
}

fn wait_for(what: &str, limit: Duration, mut cond: impl FnMut() -> bool) {
    let deadline = Instant::now() + limit;
    while !cond() {
        assert!(Instant::now() < deadline, "等待逾時：{what}");
        thread::sleep(Duration::from_millis(5));
    }
}

/// `contracts/rpc_schema.json` 中 `side_effects` 非空且 `retryable_readonly==false` 的方法（排除 daemon.stop／handoff_stop）。
/// 計數由契約動態產生，不硬編碼（OD-06）。
fn mutating_methods() -> Vec<String> {
    let path = support::serde_free::repo_file("contracts/rpc_schema.json");
    let text = std::fs::read_to_string(path).expect("讀取 rpc_schema.json");
    let PyValue::Dict(schema) = pyjson::loads(&text).expect("rpc_schema 為合法 JSON") else {
        panic!("rpc_schema 頂層須為 object");
    };
    let Some(PyValue::Dict(methods)) = schema.get("x-methods") else {
        panic!("rpc_schema 缺 x-methods");
    };
    let mut out: Vec<String> = methods
        .0
        .iter()
        .filter(|(name, _)| !matches!(name.as_str(), "daemon.stop" | "daemon.handoff_stop"))
        .filter(|(_, spec)| {
            let PyValue::Dict(spec) = spec else { return false };
            let effects = matches!(spec.get("side_effects"), Some(PyValue::List(items)) if !items.is_empty());
            let readonly = spec.get("retryable_readonly") == Some(&PyValue::Bool(true));
            effects && !readonly
        })
        .map(|(name, _)| name.clone())
        .collect();
    out.sort();
    assert!(!out.is_empty(), "rpc_schema 至少有一個有副作用方法");
    out
}

// ---------------------------------------------------------------------------
// busybox 傳檔 shell（可扣住第一個 chunk 的回應，讓 file.push 停在進行中）
// ---------------------------------------------------------------------------

type Fs = Arc<Mutex<BTreeMap<String, Vec<u8>>>>;

fn words(cmd: &str) -> Vec<String> {
    cmd.split(' ').map(|w| w.replace('\'', "")).collect()
}

/// busybox shell 的最小解譯（只認 T3 樣板；同 `file_transfer_rpc.rs`）；回傳 prompt 之前的輸出。
fn mini_shell(fs: &Fs, cmd: &str) -> String {
    if cmd == t::decoder_probe_command() {
        return format!("{}\r\n", t::DECODER_B64_SENTINEL);
    }
    if cmd == t::encoder_probe_command() {
        return format!("{}\r\n", t::ENCODER_B64_SENTINEL);
    }
    let w = words(cmd);
    let mut fs = fs.lock().unwrap();
    match w.first().map(String::as_str) {
        Some("printf") if w.get(1).map(String::as_str) == Some("%s") => {
            let data = a2b_base64_lenient(w[2].as_bytes()).expect("chunk base64");
            let (op, path) = (w[6].as_str(), w[7].clone());
            if op == ">" {
                fs.insert(path, data);
            } else {
                fs.entry(path).or_default().extend_from_slice(&data);
            }
            format!("{}\r\n", t::CHUNK_SENTINEL)
        }
        Some("md5sum") => {
            let data = fs.get(&w[1]).cloned().unwrap_or_default();
            format!("{}  {}\r\n{}\r\n", t::md5_hex(&data), w[1], t::MD5_SENTINEL)
        }
        Some("mv") => {
            let data = fs.remove(&w[1]).unwrap_or_default();
            fs.insert(w[2].clone(), data);
            format!("{}\r\n", t::MV_SENTINEL)
        }
        Some("rm") => {
            fs.remove(&w[2]);
            format!("{}\r\n", t::CLEANUP_SENTINEL)
        }
        _ => format!("sh: {cmd}: not found\r\n"),
    }
}

/// DUT 端傳檔 shell：`hold` 為 true 時扣住第一個 chunk 的回應（push 停在等 chunk ACK，傳輸進行中）。
struct XferShell {
    dev: FakeDevice,
    fs: Fs,
    held: Arc<Mutex<Option<Vec<u8>>>>,
}

impl XferShell {
    fn install(dev: &FakeDevice, prompt: &'static str, hold: bool) -> XferShell {
        let fs: Fs = Arc::new(Mutex::new(BTreeMap::new()));
        let held: Arc<Mutex<Option<Vec<u8>>>> = Arc::new(Mutex::new(None));
        let armed = Arc::new(AtomicBool::new(hold));
        let (f, h) = (fs.clone(), held.clone());
        dev.on_tx_match(
            "SW_XFER_",
            Box::new(move |line: &[u8]| {
                let cmd = String::from_utf8_lossy(line).trim_end_matches(['\r', '\n']).to_string();
                let reply = format!("{}{prompt}", mini_shell(&f, &cmd)).into_bytes();
                if cmd.starts_with("printf '%s' ") && armed.swap(false, Ordering::SeqCst) {
                    *h.lock().unwrap() = Some(reply);
                    return Vec::new();
                }
                reply
            }),
        );
        XferShell { dev: dev.clone(), fs, held }
    }

    fn wait_held(&self) {
        wait_for("第一個 chunk 已送達 DUT（回應扣住）", LONG, || self.held.lock().unwrap().is_some());
    }

    fn release(&self) {
        let reply = self.held.lock().unwrap().take().expect("有扣住的 chunk 回應");
        self.dev.push_rx(&reply);
    }

    fn file(&self, path: &str) -> Option<Vec<u8>> {
        self.fs.lock().unwrap().get(path).cloned()
    }
}

struct Tmp(PathBuf);

impl Tmp {
    fn new(tag: &str) -> Tmp {
        static N: AtomicUsize = AtomicUsize::new(0);
        let p = std::env::temp_dir().join(format!(
            "sw-p8t14-{tag}-{}-{}",
            std::process::id(),
            N.fetch_add(1, Ordering::SeqCst)
        ));
        let _ = std::fs::remove_dir_all(&p);
        std::fs::create_dir_all(&p).unwrap();
        Tmp(p)
    }

    fn write(&self, name: &str, data: &[u8]) -> String {
        let path = self.0.join(name);
        std::fs::write(&path, data).unwrap();
        path.to_string_lossy().into_owned()
    }
}

impl Drop for Tmp {
    fn drop(&mut self) {
        let _ = std::fs::remove_dir_all(&self.0);
    }
}

fn payload(n: usize) -> Vec<u8> {
    (0..n).map(|i| (i % 251) as u8).collect()
}

fn push_params(local: &str, remote: &str) -> String {
    format!(
        r#"{{"selector":"COM0","local_path":"{local}","remote_path":"{remote}","ack_mode":"none","chunk_size":256,"chunk_timeout_s":60}}"#
    )
}

// ---------------------------------------------------------------------------
// Service 層
// ---------------------------------------------------------------------------

/// 閒置 → 交接成立：凍結後對 rpc_schema 每個有副作用方法皆回 `FLUSHED_BY_SHUTDOWN`＋`reason:"handoff_frozen"`
/// （零 UART 寫入、WAL 無新紀錄、state.json 未改寫）；`health.ping`／`session.list`／`mcu.status` 仍 ok。
#[test]
fn handoff_frozen_rejects_mutating_methods() {
    let r = ServiceRig::ready_shell();
    let granted = r.svc.handoff_stop(&reason("update"));
    assert_granted(&granted, "閒置交接");
    assert!(r.svc.admission().frozen(), "交接成立後 admission 凍結");
    let before = r.side_effect_fingerprint();
    let params = r#"{"selector":"COM0","command":"echo 1","data_b64":"eA==","cmd_id":"c-1","interactive_id":"i-1","local_path":"/nonexistent/x","remote_path":"/tmp/x","by_id":"usb-x","device_by_id":"usb-x","alias":"a","profile":"t","rule_id":"r1","session_id":"t:COM0"}"#;
    for method in mutating_methods() {
        let resp = r.call(&method, params);
        assert_eq!(
            keys(&resp),
            ["ok", "error_code", "reason", "version"],
            "{method}：{}",
            wire(&resp)
        );
        assert!(flushed_frozen(&resp), "{method}：{}", wire(&resp));
    }
    assert_eq!(r.side_effect_fingerprint(), before, "凍結後零 TX、WAL 無新紀錄、state.json 未改寫");
    for method in ["health.ping", "session.list", "mcu.status"] {
        let resp = r.call(method, "{}");
        assert_eq!(resp.get("ok"), Some(&PyValue::Bool(true)), "{method} 仍可回應：{}", wire(&resp));
    }
    // 重複交接：回既有 token（冪等），仍凍結。
    let again = r.svc.handoff_stop(&reason("update"));
    assert_eq!(assert_granted(&again, "重複交接"), s(&granted, "handoff_token").unwrap());
}

/// lease 開啟 → `SESSION_BUSY`（`ops:["session.interactive_open"]`）；recovering → `SESSION_BUSY`（`recovering:true`）；
/// 兩者皆不凍結，之後 `command.submit` 照常受理（不回 `FLUSHED_BY_SHUTDOWN`）。
#[test]
fn handoff_busy_lease_and_recovering() {
    let r = ServiceRig::ready_shell();
    let open = r.call("session.interactive_open", r#"{"selector":"COM0","owner":"agent:t14","timeout_s":60}"#);
    assert_eq!(open.get("ok"), Some(&PyValue::Bool(true)), "{}", wire(&open));
    let iid = s(&open, "interactive_id").expect("interactive_id").to_string();
    let busy = r.svc.handoff_stop(&reason("update"));
    let rows = assert_refused(&busy, "SESSION_BUSY", "lease");
    let row = row_for(&rows, "COM0");
    assert_eq!(ops(row), ["session.interactive_open"], "{}", wire(row));
    assert_eq!(s(row, "interactive_session_id"), Some(iid.as_str()), "{}", wire(row));
    assert!(!r.svc.admission().frozen(), "忙碌不得凍結");
    let sub = r.call("command.submit", r#"{"selector":"COM0","command":"echo 1"}"#);
    assert!(!flushed_frozen(&sub), "lease 忙碌未凍結，submit 不得回 handoff_frozen：{}", wire(&sub));
    let close = r.call("session.interactive_close", &format!(r#"{{"interactive_id":"{iid}"}}"#));
    assert_eq!(close.get("ok"), Some(&PyValue::Bool(true)), "{}", wire(&close));

    let r = ServiceRig::ready_shell();
    r.mgr.force_session_state("COM0", SessionState::Recovering);
    let busy = r.svc.handoff_stop(&reason("update"));
    let rows = assert_refused(&busy, "SESSION_BUSY", "recovering");
    assert_eq!(row_for(&rows, "COM0").get("recovering"), Some(&PyValue::Bool(true)), "{}", wire(&busy));
    assert!(!r.svc.admission().frozen(), "忙碌不得凍結");
    r.mgr.force_session_state("COM0", SessionState::Ready);
    let sub = r.call("command.submit", r#"{"selector":"COM0","command":"echo 1"}"#);
    assert_eq!(sub.get("ok"), Some(&PyValue::Bool(true)), "recovering 解除後 submit 照常受理：{}", wire(&sub));
}

/// 預檢（session.list 閒置）之後才開始的 `file.push`：交接回 `SESSION_BUSY`（`ops:["file.push"]`），不凍結；push 完成且
/// DUT 端檔案相符後，交接才成立。
#[test]
fn handoff_service_push_ticket_blocks_until_done() {
    let r = Arc::new(ServiceRig::ready_shell());
    let shell = XferShell::install(r.device(REAL_A), "root@t:/# ", true);
    let list = r.call("session.list", "{}");
    assert!(
        wire(&list).contains("\"foreground_busy\":false"),
        "CLI 預檢看到閒置：{}",
        wire(&list)
    );
    let tmp = Tmp::new("svc-push");
    let data = payload(700);
    let local = tmp.write("src.bin", &data);
    let rig = r.clone();
    let params = push_params(&local, "/tmp/svc.bin");
    let push = thread::spawn(move || rig.call("file.push", &params));
    shell.wait_held();
    let busy = r.svc.handoff_stop(&reason("update"));
    let rows = assert_refused(&busy, "SESSION_BUSY", "push 進行中");
    assert_eq!(ops(row_for(&rows, "COM0")), ["file.push"], "{}", wire(&busy));
    assert!(!r.svc.admission().frozen());
    shell.release();
    let done = push.join().expect("push thread");
    assert_eq!(done.get("ok"), Some(&PyValue::Bool(true)), "{}", wire(&done));
    assert_eq!(shell.file("/tmp/svc.bin"), Some(data), "DUT 端檔案內容完整");
    assert_granted(&r.svc.handoff_stop(&reason("update")), "push 完成後交接");
}

/// 開 console、讓 human raw 按鍵的 TX 停在 fence 內（FakeSerial `BlockForever`：已經 gate 提交、in_flight 未清）。
fn console_rig_with_inflight_key() -> (ServiceRig, sw_pal::fakes::console::FakeConsoleRef) {
    let r = ServiceRig::with_console_backend("pty");
    let attach = r.call("session.console_attach", r#"{"selector":"COM0"}"#);
    assert_eq!(attach.get("ok"), Some(&PyValue::Bool(true)), "{}", wire(&attach));
    let (_, io) = r.console.as_ref().unwrap().created()[1].clone();
    let dev = r.device(REAL_A).clone();
    let _ = dev.take_tx();
    dev.script_write(vec![FakeWriteStep::BlockForever]);
    io.push_input(b"k");
    dev.wait_blocked();
    (r, io)
}

/// 已通過提交檢查的背景 TX 停在 fence 內超過交接收斂期限：`SESSION_BUSY`（`ops` 含 `tx.in_flight`），交接未成立、
/// admission／bridge 回到未凍結；放行原 TX 後新按鍵照常寫出，之後再交接成立且成功回覆後零 TX
/// （Python `test_handoff_refused_when_background_write_holds_tx_fence`）。
#[test]
fn handoff_tx_fence_timeout_thaws_without_stop() {
    let (r, io) = console_rig_with_inflight_key();
    let dev = r.device(REAL_A).clone();
    let busy = r.svc.handoff_stop(&reason("update"));
    let rows = assert_refused(&busy, "SESSION_BUSY", "TX fence 未收斂");
    assert!(ops(row_for(&rows, "COM0")).contains(&"tx.in_flight".to_string()), "{}", wire(&busy));
    assert!(!dev.take_tx().contains(&b'k'), "回覆當下在途按鍵尚未落地");
    assert!(!r.svc.admission().frozen(), "撤銷本次凍結：admission 恢復受理");
    dev.release_block();
    let mut seen = Vec::new();
    wait_for("在途按鍵放行後寫出", SHORT, || {
        seen.extend(dev.take_tx());
        seen.contains(&b'k')
    });
    io.push_input(b"z");
    wait_for("交接未成立：bridge 未凍結、新按鍵照常寫出", SHORT, || {
        seen.extend(dev.take_tx());
        seen.contains(&b'z')
    });
    let sub = r.call("command.submit", r#"{"selector":"COM0","command":"echo after"}"#);
    assert!(!flushed_frozen(&sub), "{}", wire(&sub));
    r.settle();
    wait_for("命令完成", SHORT, || {
        r.session("COM0").and_then(|s| s.get("foreground_busy").cloned()) == Some(PyValue::Bool(false))
    });
    assert_granted(&r.svc.handoff_stop(&reason("update")), "放行後再交接");
    let at_reply = dev.tx_len();
    io.push_input(b"q");
    thread::sleep(Duration::from_millis(300));
    assert_eq!(dev.tx_len(), at_reply, "交接成功回覆後 console 按鍵零 TX");
}

/// shutdown 凍結（admission `freeze()`、stopping gate、bridge fence frozen）已建立時，交接遇 TX fence 未收斂：
/// 回滾只能保留 shutdown 凍結——admission 與 bridge 仍凍結、新按鍵零 TX，原在途 TX 可完成；不得 thaw。
#[test]
fn handoff_rollback_preserves_shutdown_freeze() {
    let (r, io) = console_rig_with_inflight_key();
    let dev = r.device(REAL_A).clone();
    r.svc.shutdown_freeze();
    r.mgr.begin_shutdown();
    let fence = r.mgr.fence_for_test("COM0").expect("COM0 fence");
    fence.with(|f| f.frozen = true);
    let busy = r.svc.handoff_stop(&reason("update"));
    let rows = assert_refused(&busy, "SESSION_BUSY", "shutdown 中 TX fence 未收斂");
    assert!(ops(row_for(&rows, "COM0")).contains(&"tx.in_flight".to_string()), "{}", wire(&busy));
    assert!(r.svc.admission().frozen(), "shutdown 凍結不得被交接回滾撤銷");
    assert!(fence.with(|f| f.frozen), "bridge 的 shutdown 凍結不得被撤銷");
    let sub = r.call("command.submit", r#"{"selector":"COM0","command":"echo x"}"#);
    assert!(flushed_frozen(&sub), "admission 仍凍結：{}", wire(&sub));
    dev.release_block();
    let mut seen = Vec::new();
    wait_for("原在途 TX 可完成", SHORT, || {
        seen.extend(dev.take_tx());
        seen.contains(&b'k')
    });
    io.push_input(b"z");
    thread::sleep(Duration::from_millis(300));
    seen.extend(dev.take_tx());
    assert!(!seen.contains(&b'z'), "bridge 仍拒絕新 TX：{seen:?}");
}

/// flash rig：COM0 shell READY、COM1 passthrough MCU（ATTACHED，sync→ACK）；admission 已接到 coordinator。
fn flash_rig() -> ServiceRig {
    ServiceRig::with_flash(FlashSetup {
        yaml: format!(
            "{FLASH_TEMPLATES}targets:\n{}{}",
            flash_target_line("COM0", "t", BY_ID_A),
            flash_target_line("COM1", "m", BY_ID_B)
        ),
        devices: vec![
            (BY_ID_A.into(), REAL_A.into(), DevKind::Shell),
            (BY_ID_B.into(), REAL_B.into(), DevKind::Mcu),
        ],
        caps_flash: true,
        coord_clock: sw_pal::native().clock.clone(),
        wait_states: vec![("COM0".into(), "READY".into()), ("COM1".into(), "ATTACHED".into())],
    })
}

fn state_of(r: &ServiceRig, com: &str) -> Option<String> {
    r.session(com)
        .and_then(|s| s.get("state").and_then(PyValue::as_str).map(str::to_string))
}

fn probe_started(r: &ServiceRig) -> bool {
    r.flash_events().iter().any(|(_, kind)| *kind == "FlashSyncProbeStart")
}

fn finish_flash_rig(r: &ServiceRig) {
    let _ = r.stop_flash_endpoint();
    let _ = r.mgr.shutdown(Duration::from_secs(3));
}

/// `first == "handoff"` 的結果：交接成立 ⇒ probe 被拒、MCU 收到 0 bytes、session 從未 FLASHING。
fn assert_probe_refused(r: &ServiceRig, what: &str) {
    let flash = r.flash.as_ref().expect("flash rig");
    wait_for(&format!("{what}：端點已處理 flasher 寫入且 admission 拒絕"), SHORT, || {
        flash.coord.admission_refusals_for_test() >= 1
    });
    thread::sleep(Duration::from_millis(50));
    assert!(!probe_started(r), "{what}：凍結後不得開始 sync-probe：{:?}", r.flash_events());
    assert!(r.device(REAL_B).tx_since(0).is_empty(), "{what}：MCU 收到 0 bytes");
    assert_ne!(state_of(r, "COM1").as_deref(), Some("FLASHING"), "{what}");
    assert!(r.flasher_take().is_empty(), "{what}：端點保持沉默");
}

/// `first == "probe"` 的結果：probe 先 admit ⇒ 交接 `FLASHING_BUSY`、未凍結，flash 照常進行（FLASHING、ACK 回放）。
fn assert_flash_proceeds(r: &ServiceRig, resp: &PyDict, what: &str) {
    assert_refused(resp, "FLASHING_BUSY", what);
    assert!(!r.svc.admission().frozen(), "{what}：未凍結");
    wait_for(&format!("{what}：COM1 進入 FLASHING"), SHORT, || state_of(r, "COM1").as_deref() == Some("FLASHING"));
    let mut to_flasher = Vec::new();
    wait_for(&format!("{what}：ACK 回放給 flasher"), SHORT, || {
        to_flasher.extend(r.flasher_take());
        to_flasher.len() >= MCU_ACK.len()
    });
    assert_eq!(to_flasher, MCU_ACK, "{what}");
}

#[test]
fn handoff_flash_races_prepare_probe_first() {
    let r = flash_rig();
    r.flasher_write(MCU_SYNC);
    wait_for("sync-probe 已 admit 並開始", SHORT, || probe_started(&r));
    let resp = r.svc.handoff_stop(&reason("rollback"));
    assert_flash_proceeds(&r, &resp, "probe 先");
    finish_flash_rig(&r);
}

#[test]
fn handoff_flash_races_prepare_handoff_first() {
    let r = flash_rig();
    assert_granted(&r.svc.handoff_stop(&reason("rollback")), "交接先");
    r.flasher_write(MCU_SYNC);
    assert_probe_refused(&r, "交接先");
    finish_flash_rig(&r);
}

#[test]
fn handoff_flash_races_prepare() {
    const ROUNDS: usize = 200;
    let (mut granted, mut busy) = (0usize, 0usize);
    for round in 0..ROUNDS {
        let r = Arc::new(flash_rig());
        let barrier = Arc::new(Barrier::new(2));
        let (r1, b1) = (r.clone(), barrier.clone());
        let probe = thread::spawn(move || {
            b1.wait();
            r1.flasher_write(MCU_SYNC);
        });
        let (r2, b2) = (r.clone(), barrier.clone());
        let handoff = thread::spawn(move || {
            b2.wait();
            r2.svc.handoff_stop(&reason("rollback"))
        });
        probe.join().expect("probe thread");
        let resp = handoff.join().expect("handoff thread");
        let what = format!("第 {round} 輪");
        if resp.get("ok") == Some(&PyValue::Bool(true)) {
            assert_granted(&resp, &what);
            assert_probe_refused(&r, &what);
            granted += 1;
        } else {
            assert_flash_proceeds(&r, &resp, &what);
            busy += 1;
        }
        finish_flash_rig(&r);
    }
    assert_eq!(granted + busy, ROUNDS);
}

/// 四個 reason（update／rollback／revert／supervision_set）語意相同：file.push ticket → `SESSION_BUSY`；flash sync-probe
/// ticket → `FLASHING_BUSY`；全閒置 → 32 hex token 且凍結；除 token 外回應逐欄相同。
#[test]
fn handoff_reason_supervision_set_same_semantics() {
    let mut push_resps = Vec::new();
    let mut flash_resps = Vec::new();
    let mut idle_resps = Vec::new();
    for why in ["update", "rollback", "revert", "supervision_set"] {
        // file.push ticket
        let r = Arc::new(ServiceRig::ready_shell());
        let shell = XferShell::install(r.device(REAL_A), "root@t:/# ", true);
        let tmp = Tmp::new("reason");
        let data = payload(600);
        let local = tmp.write("src.bin", &data);
        let rig = r.clone();
        let params = push_params(&local, "/tmp/reason.bin");
        let push = thread::spawn(move || rig.call("file.push", &params));
        shell.wait_held();
        let resp = r.svc.handoff_stop(&reason(why));
        let rows = assert_refused(&resp, "SESSION_BUSY", why);
        assert!(ops(row_for(&rows, "COM0")).contains(&"file.push".to_string()), "{why}：{}", wire(&resp));
        assert!(!r.svc.admission().frozen(), "{why}：未凍結");
        shell.release();
        let done = push.join().expect("push");
        assert_eq!(done.get("ok"), Some(&PyValue::Bool(true)), "{why}：{}", wire(&done));
        assert_eq!(s(&done, "md5"), Some(t::md5_hex(&data).as_str()), "{why}");
        assert_eq!(shell.file("/tmp/reason.bin"), Some(data), "{why}：DUT 端內容相符");
        push_resps.push(wire(&resp));

        // flash sync-probe ticket
        let f = flash_rig();
        f.flasher_write(MCU_SYNC);
        // 等 COM1 進入 FLASHING 再交接，使四個 reason 的 busy 列（state）可逐欄比較。
        wait_for(&format!("{why}：COM1 FLASHING"), SHORT, || state_of(&f, "COM1").as_deref() == Some("FLASHING"));
        let resp = f.svc.handoff_stop(&reason(why));
        assert_flash_proceeds(&f, &resp, why);
        flash_resps.push(wire(&resp));
        finish_flash_rig(&f);

        // 全閒置
        let r = ServiceRig::ready_shell();
        let mut resp = r.svc.handoff_stop(&reason(why));
        assert_granted(&resp, why);
        assert!(r.svc.admission().frozen(), "{why}：閒置交接凍結");
        resp.0.shift_remove("handoff_token");
        idle_resps.push(wire(&resp));
    }
    for set in [&push_resps, &flash_resps, &idle_resps] {
        assert!(set.windows(2).all(|w| w[0] == w[1]), "四個 reason 的回應須逐欄相同：{set:?}");
    }
}

// ---------------------------------------------------------------------------
// daemon 層（in-process，FakeSerial＋原生時鐘）
// ---------------------------------------------------------------------------

const BY_ID: &str = "usb-SER-A-if00";
const REAL: &str = "/dev/pts/10";

fn device(by_id: &str, real_path: &str) -> (String, DeviceInfo) {
    (
        by_id.to_string(),
        DeviceInfo {
            by_id: by_id.to_string(),
            real_path: real_path.to_string(),
        },
    )
}

fn rpc_dict(socket: &Path, method: &str, params: &str) -> PyDict {
    let line = format!(r#"{{"id":9,"method":"{method}","params":{params}}}"#);
    let text = rpc_once(socket, &line);
    match pyjson::loads(text.trim_end()).unwrap_or_else(|e| panic!("{method} 回應非 JSON：{text:?}：{e:?}")) {
        PyValue::Dict(mut d) => {
            assert_eq!(d.get("id"), Some(&PyValue::from(9i64)), "framing 回應帶原 id：{text}");
            d.0.shift_remove("id");
            d
        }
        other => panic!("{method} 回應非 object：{other:?}"),
    }
}

fn wait_ready(socket: &Path) {
    wait_for("COM0 READY", LONG, || get_state(socket, "COM0")["state"] == "READY");
}

/// in-process daemon（READY shell 目標 COM0，FakeSerial）；`client` 在 daemon 執行期間運行，結束（含 panic）後若 daemon
/// 仍在服務則送 `daemon.stop`，避免測試卡住。回傳（exit code、trace、client 結果）。
fn run_daemon<T: Send + 'static>(
    client: impl FnOnce(PathBuf, FakeDevice, Arc<Mutex<Vec<TraceEvent>>>) -> T + Send + 'static,
) -> (i32, Vec<TraceEvent>, std::thread::Result<T>, Instant) {
    let env = isolated_env();
    let (platform, h) = in_process_platform_with_handles(&env.vars, vec![device(BY_ID, REAL)], Fixture::ReadyShell);
    let dev = h.devices[0].clone();
    let sink: Arc<Mutex<Vec<TraceEvent>>> = Arc::new(Mutex::new(Vec::new()));
    let hooks = {
        let sink = sink.clone();
        DaemonHooks {
            trace: Some(Box::new(move |ev| sink.lock().unwrap().push(ev))),
            ..DaemonHooks::default()
        }
    };
    let socket = env.socket.clone();
    let client_thread = {
        let (socket, sink) = (socket.clone(), sink.clone());
        thread::spawn(move || {
            wait_socket(&socket, LONG);
            let out = catch_unwind(AssertUnwindSafe(|| client(socket.clone(), dev, sink)));
            if out.is_err() {
                // client 失敗：確保 daemon 結束（STOP 送不到時 daemon 已在停止）。
                let _ = std::panic::catch_unwind(|| rpc_once(&socket, STOP));
            }
            out
        })
    };
    let code = run_with_hooks(vec!["serialwrapd".to_string()], platform, hooks);
    let exited = Instant::now();
    let out = client_thread.join().unwrap_or_else(Err);
    let trace = sink.lock().unwrap().clone();
    (code, trace, out, exited)
}

fn shutdown_steps(trace: &[TraceEvent]) -> Vec<&'static str> {
    trace.iter().filter_map(TraceEvent::shutdown_step).collect()
}

/// 預檢（session.list 閒置）後才開始的 file.push：交接回 `SESSION_BUSY{ops:["file.push"]}`，停止序未開始、push 未被中止；
/// 放行後 push ok 且 DUT 收到完整檔案；再交接 → `ok/stopping/token`、回應無 `version`；排空窗口內 file.push 回
/// `FLUSHED_BY_SHUTDOWN`／`handoff_frozen`、`health.ping` 仍 ok；窗口結束後才進停止序，`SessionsDrained{flushed:[],stalled:[]}`、
/// exit 0（Python `test_handoff_file_push_after_last_probe`＋`test_handoff_drain_window_flushes_mutating_answers_ping_and_exits_bounded`）。
#[test]
fn handoff_file_push_after_last_probe() {
    let tmp = Arc::new(Tmp::new("daemon-push"));
    let data = payload(900);
    let local = tmp.write("src.bin", &data);
    let data_c = data.clone();
    let (code, trace, out, exited) = run_daemon(move |socket, dev, sink| {
        wait_ready(&socket);
        let shell = XferShell::install(&dev, "root@t:/# ", true);
        let list = rpc_dict(&socket, "session.list", "{}");
        assert!(wire(&list).contains("\"foreground_busy\":false"), "預檢閒置：{}", wire(&list));
        let (s2, params) = (socket.clone(), push_params(&local, "/tmp/daemon.bin"));
        let push = thread::spawn(move || rpc_dict(&s2, "file.push", &params));
        shell.wait_held();
        let busy = rpc_dict(&socket, "daemon.handoff_stop", r#"{"reason":"update"}"#);
        let rows = assert_refused(&busy, "SESSION_BUSY", "daemon：push 進行中");
        assert_eq!(rows.len(), 1, "{}", wire(&busy));
        assert_eq!(s(&rows[0], "com"), Some("COM0"));
        assert_eq!(ops(&rows[0]), ["file.push"]);
        assert!(shutdown_steps(&sink.lock().unwrap()).is_empty(), "交接被拒：停止序不得開始");
        thread::sleep(HANDOFF_DRAIN_GRACE + Duration::from_millis(500));
        assert_eq!(rpc_dict(&socket, "health.ping", "{}").get("ok"), Some(&PyValue::Bool(true)), "被拒的交接不停止 daemon");
        assert!(shutdown_steps(&sink.lock().unwrap()).is_empty());
        shell.release();
        let done = push.join().expect("push thread");
        assert_eq!(done.get("ok"), Some(&PyValue::Bool(true)), "{}", wire(&done));
        assert_eq!(shell.file("/tmp/daemon.bin"), Some(data_c), "DUT 收到完整 payload");
        let granted = rpc_dict(&socket, "daemon.handoff_stop", r#"{"reason":"update"}"#);
        assert_granted(&granted, "daemon：push 完成後交接");
        let replied = Instant::now();
        let tx_at_reply = dev.tx_len();
        let flushed = rpc_dict(&socket, "file.push", &push_params("/nonexistent/a.bin", "/tmp/a.bin"));
        assert!(flushed_frozen(&flushed), "排空窗口內有副作用請求：{}", wire(&flushed));
        assert_eq!(rpc_dict(&socket, "health.ping", "{}").get("ok"), Some(&PyValue::Bool(true)), "窗口內 ping 仍可回應");
        assert!(
            shutdown_steps(&sink.lock().unwrap()).is_empty(),
            "排空窗口內停止序尚未開始：{:?}",
            sink.lock().unwrap()
        );
        assert_eq!(dev.tx_len(), tx_at_reply, "交接成功回覆後零 TX");
        (replied, tx_at_reply, dev)
    });
    let (replied, tx_at_reply, dev) = out.expect("client 斷言");
    let took = exited.saturating_duration_since(replied);
    assert_eq!(code, 0, "交接成立的停止為有序停止：{trace:?}");
    assert!(took + Duration::from_millis(500) >= HANDOFF_DRAIN_GRACE, "窗口結束前不得結束（{took:?}）");
    assert!(took <= HANDOFF_DRAIN_GRACE + STOP_CAP, "停止有界：{took:?}");
    assert!(
        trace.contains(&TraceEvent::SessionsDrained { flushed: vec![], stalled: vec![] }),
        "{trace:?}"
    );
    assert_eq!(shutdown_steps(&trace).first(), Some(&"server.stop_accept"), "{trace:?}");
    assert_eq!(dev.tx_len(), tx_at_reply, "停止序（drain）期間同樣零 TX");
}

/// 閒置交接成立：framing 層回應無 `version`、窗口內 `session.recover` 與 `command.submit` 回 `handoff_frozen`；
/// DeviceWatcher 重探與 console 行零 TX；停止序在窗口後才開始、exit 0（Python `test_handoff_ok_then_sc11_stop_order`）。
#[test]
fn handoff_ok_then_sc11_stop_order() {
    let (code, trace, out, exited) = run_daemon(move |socket, dev, sink| {
        wait_ready(&socket);
        let granted = rpc_dict(&socket, "daemon.handoff_stop", r#"{"reason":"update"}"#);
        assert_granted(&granted, "閒置");
        let replied = Instant::now();
        let tx = dev.tx_len();
        for (method, params) in [
            ("command.submit", r#"{"selector":"COM0","command":"echo 1"}"#),
            ("session.recover", r#"{"selector":"COM0"}"#),
            ("device.release", r#"{"selector":"COM0"}"#),
        ] {
            let r = rpc_dict(&socket, method, params);
            assert!(flushed_frozen(&r), "{method}：{}", wire(&r));
        }
        thread::sleep(Duration::from_millis(1_500));
        assert!(shutdown_steps(&sink.lock().unwrap()).is_empty(), "窗口內停止序未開始");
        assert_eq!(dev.tx_len(), tx, "窗口內零 TX");
        replied
    });
    let replied = out.expect("client 斷言");
    let took = exited.saturating_duration_since(replied);
    assert_eq!(code, 0, "{trace:?}");
    assert!(took + Duration::from_millis(500) >= HANDOFF_DRAIN_GRACE, "窗口結束前不得結束：{took:?}");
    assert!(took <= HANDOFF_DRAIN_GRACE + STOP_CAP, "停止有界：{took:?}");
    assert!(trace.contains(&TraceEvent::SessionsDrained { flushed: vec![], stalled: vec![] }), "{trace:?}");
}

// ---------------------------------------------------------------------------
// 行程層：交接後才啟動繼任者，任何時刻至多一個 daemon 持有 UART
// ---------------------------------------------------------------------------

/// 背景連續取樣 tty holder（10 ms）：回傳觀察到的最大同時持有數與曾出現過的 pid 序。
struct HolderSampler {
    stop: Arc<AtomicBool>,
    handle: Option<thread::JoinHandle<(usize, Vec<u32>)>>,
}

impl HolderSampler {
    fn start(path: PathBuf) -> HolderSampler {
        let stop = Arc::new(AtomicBool::new(false));
        let flag = stop.clone();
        let handle = thread::spawn(move || {
            let mut max = 0usize;
            let mut seen: Vec<u32> = Vec::new();
            while !flag.load(Ordering::SeqCst) {
                let holders: Vec<u32> = path_holders(&path)
                    .into_iter()
                    .filter(|(_, comm)| comm.contains("serialwrapd"))
                    .map(|(pid, _)| pid)
                    .collect();
                max = max.max(holders.len());
                for pid in holders {
                    if !seen.contains(&pid) {
                        seen.push(pid);
                    }
                }
                thread::sleep(Duration::from_millis(10));
            }
            (max, seen)
        });
        HolderSampler { stop, handle: Some(handle) }
    }

    fn finish(mut self) -> (usize, Vec<u32>) {
        self.stop.store(true, Ordering::SeqCst);
        self.handle.take().expect("sampler").join().expect("sampler thread")
    }
}

impl Drop for HolderSampler {
    fn drop(&mut self) {
        self.stop.store(true, Ordering::SeqCst);
    }
}

/// 交接成立後舊 daemon 在排空窗口結束、停止序完成後才退出並釋放 UART／socket；繼任者只在舊 daemon 退出後啟動，
/// 全程 holder 取樣 `max_concurrent()<=1`、pid 依序為「舊 → 新」，最終繼任者 READY。
#[test]
fn handoff_then_successor_never_double_held() {
    let env = isolated_env_with_pty_target();
    let sampler = HolderSampler::start(env.pty_slave.clone());
    let mut old = DaemonProc::spawn(&env, &[]);
    support::wait_ready(&env, "COM0");
    let old_pid = old.pid();
    let granted = match pyjson::loads(rpc_once(&env.socket, r#"{"id":3,"method":"daemon.handoff_stop","params":{"reason":"update"}}"#).trim_end()) {
        Ok(PyValue::Dict(mut d)) => {
            d.0.shift_remove("id");
            d
        }
        other => panic!("handoff 回應：{other:?}"),
    };
    assert_granted(&granted, "行程層閒置交接");
    // 舊 daemon 仍在排空窗口：繼任者不得取得實例鎖（不得出現兩個 daemon 同時監聽 socket）。
    assert!(old.try_alive(), "排空窗口內舊 daemon 仍在");
    let status = old.wait(HANDOFF_DRAIN_GRACE + STOP_CAP + Duration::from_secs(2));
    assert_eq!(status.code(), Some(0), "交接後有序停止：{}", old.stderr());
    assert!(path_holders(&env.pty_slave).iter().all(|(pid, _)| *pid != old_pid), "舊 daemon 已釋放 UART");
    let new = DaemonProc::spawn(&env, &[]);
    support::wait_ready(&env, "COM0");
    let new_pid = new.pid();
    let (max, seen) = sampler.finish();
    assert!(max <= 1, "任一時刻至多一個 daemon 持有 UART：max={max} seen={seen:?}");
    assert_eq!(seen, vec![old_pid, new_pid], "持有者依序為舊 → 新：{seen:?}");
    drop(new);
}
