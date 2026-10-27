//! Slice 2.1 Task 14：agent Interactive Lease（SC-13、#36、#54）、self-test 分類（05 §11、SC-35、R5-F2）與
//! agent log capture 的紅燈測試（plan 06 Task 14 Step 1 `lease_selftest.rs` 表列）。
//!
//! 斷言值以 Python 基線（`session_manager.py` 的 `interactive_open／send／status／close`、
//! `_expire_interactive_leases_tick`、`_self_test_impl`、`log_start／log_stop／log_status`）為準。

#![cfg(feature = "test-fakes")]

mod common;
mod support;
use common::repo_path;

use std::collections::HashMap;
use std::sync::atomic::{AtomicBool, Ordering};
use std::sync::{Arc, Mutex};
use std::time::{Duration, Instant};

use support::*;
use sw_core::capture::CaptureEnv;
use sw_core::mailbox::{ControlEvent, OpenFailure};
use sw_core::op_runner::encode_interactive_payload;
use sw_core::self_test::SelfTestProbes;
use sw_core::session_core::SessionRequest;
use sw_core::tx_writer::{FlashMode, OpId};
use sw_pal::fakes::serial::{FakeUartExclusive, FakeWriteStep};
use sw_pal::traits::{Clock, PalError};
use sw_proto::pycompat::{PyErr, clean_text};
use sw_proto::pyjson::{PyDict, PyInt, PyValue, loads};

const BANNER: &[u8] = b"\r\nU-Boot 2024.04 (Oct 01 2024)\r\n";
const AUTOBOOT: &[u8] = b"\r\nHit any key to stop autoboot:  3 ";
const LEASE_KEYS: [&str; 4] = [
    "interactive_owner",
    "human_attached",
    "human_active",
    "recovery_mode",
];
const BOOTLOADER_LOGIN_PROFILE: &str = "profiles:\n  b:\n    platform: shell\n    prompt_regex: '(?m)^root@t:.*# '\n    login_regex: 'login: ?$'\n    password_regex: 'Password: ?$'\n    bootloader_prompts: ['(?m)^=> $']\n    timeout_s: 2.0\ntargets:\n  - {com: COM0, profile: b, act_no: 1, alias: b+1, device_by_id: /s/by-id/usb-A}\n";

fn pget(d: &PyDict, k: &str) -> PyValue {
    d.get(k).cloned().unwrap_or(PyValue::Null)
}

fn keys(d: &PyDict) -> Vec<String> {
    d.0.keys().cloned().collect()
}

fn int(n: i128) -> PyValue {
    PyValue::Int(PyInt::Small(n))
}

fn open_req(owner: &str, timeout_s: f64, command: &str, allow_attached: bool) -> SessionRequest {
    SessionRequest::InteractiveOpen {
        owner: owner.into(),
        timeout_s,
        command: command.into(),
        allow_attached,
    }
}

fn send_req(id: &str, data: &str, encoding: &str) -> SessionRequest {
    SessionRequest::InteractiveSend {
        interactive_id: id.into(),
        data: data.into(),
        encoding: encoding.into(),
    }
}

fn status_req(id: &str) -> SessionRequest {
    SessionRequest::InteractiveStatus {
        interactive_id: id.into(),
        screen_chars: 2048,
    }
}

fn close_req(id: &str) -> SessionRequest {
    SessionRequest::InteractiveClose {
        interactive_id: id.into(),
    }
}

fn self_test() -> SessionRequest {
    SessionRequest::SelfTest {
        timeout_s: 2.0,
        strict_human_lock: false,
    }
}

fn iid(open: &PyDict) -> String {
    open["interactive_id"]
        .as_str()
        .expect("interactive_id")
        .to_string()
}

fn tx_rows(h: &ActorRig) -> Vec<PyDict> {
    h.wal_all_rows()
        .into_iter()
        .filter(|r| r.get("dir").and_then(PyValue::as_str) == Some("TX"))
        .collect()
}

/// 推進 FakeClock 並投遞 Tick（真實時間讓出，讓 actor 處理）。
fn tick_advance(h: &ActorRig, d: Duration) {
    h.clock.advance(d);
    h.send_control(ControlEvent::Tick);
    std::thread::sleep(Duration::from_millis(20));
    let _ = h.get_state();
}

/// attach 至 READY，但 nonce 只回應登入那一次（之後的 self-test probe 不回應）。
fn ready_once(profile: &str) -> ActorRig {
    let h = ActorRig::new(profile);
    h.dev.on_tx_reply(b"\n", b"root@t:/# ");
    let answered = Arc::new(AtomicBool::new(false));
    h.dev.on_tx_match(
        r"__READY__[0-9a-f]+",
        Box::new(move |line: &[u8]| {
            if answered.swap(true, Ordering::SeqCst) {
                return Vec::new();
            }
            let text = String::from_utf8_lossy(line).into_owned();
            let token = text
                .split("__READY__")
                .nth(1)
                .unwrap_or("")
                .trim()
                .to_string();
            format!("__READY__{token}\r\nroot@t:/# ").into_bytes()
        }),
    );
    let h = h.attach();
    h.wait_state("READY");
    h
}

/// 停在 ATTACHED（prompt 有回、nonce 不回 → READY_NONCE_TIMEOUT）。
fn attached(profile: &str) -> ActorRig {
    let h = ActorRig::new(profile);
    h.dev.on_tx_reply(b"\n", b"\r\nroot@t:/# ");
    let h = h.attach();
    h.wait_state("ATTACHED");
    h
}

/// READY＋reconfirm pending（banner arm 後 prompt 清除 TX 靜默維度，pending 保留，#162）。
fn make_pending(h: &ActorRig) {
    h.inject_rx(BANNER);
    h.wait_for("quiet arm", |h| h.fence.with(|f| f.quiet_active));
    h.inject_rx(b"\r\nroot@t:/# ");
    h.wait_for("quiet 清除", |h| !h.fence.with(|f| f.quiet_active));
    assert_eq!(
        h.get_state()["ready_reconfirm_pending"],
        PyValue::Bool(true)
    );
}

fn assert_lease_ctx_keys(r: &PyDict) {
    for k in LEASE_KEYS {
        assert!(r.get(k).is_some(), "缺少 LeaseContext 鍵 {k}：{r:?}");
    }
    assert!(
        r.get("command_capable").is_some(),
        "缺少 command_capable：{r:?}"
    );
}

// ---------------------------------------------------------------------------
// interactive lease
// ---------------------------------------------------------------------------

#[test]
fn open_ready_shape() {
    let h = ActorRig::new(SHELL_PROFILE).ready();
    let r = h.request(open_req("agent:a", 30.0, "", false));
    assert_eq!(
        keys(&r),
        vec!["ok", "interactive_id", "session", "recovery_mode"]
    );
    assert_eq!(r["ok"], PyValue::Bool(true));
    let id = iid(&r);
    assert_eq!(id.len(), 32);
    assert!(id.chars().all(|c| c.is_ascii_hexdigit()));
    assert_eq!(r["recovery_mode"], PyValue::Bool(false));
    let PyValue::Dict(session) = r["session"].clone() else {
        panic!()
    };
    assert_eq!(session["interactive_session_id"], PyValue::from(id.clone()));
    assert_eq!(h.request(close_req(&id))["ok"], PyValue::Bool(true));

    // command 非空：以 InteractiveOpenCmd class 送出（quiet 中也送）。
    h.inject_rx(BANNER);
    h.wait_for("quiet", |h| h.fence.with(|f| f.quiet_active));
    let r = h.request(open_req("agent:b", 30.0, "ls -l", false));
    assert_eq!(r["ok"], PyValue::Bool(true));
    let row = tx_rows(&h)
        .into_iter()
        .find(|r| r["source"] == PyValue::from("agent:b"))
        .expect("open 的 command 應寫出");
    assert_eq!(row["dir"], PyValue::from("TX"));
}

#[test]
fn open_busy_foreground() {
    let h = ActorRig::new(SHELL_PROFILE).ready();
    let hold = h.submit_with("sleep 60", |r| r.timeout_s = 120.0);
    h.wait_running(&hold);
    let r = h.request(open_req("agent:a", 30.0, "", false));
    assert_eq!(
        r,
        sw_proto::pydict! { "ok" => false, "error_code" => "SESSION_BUSY", "selector" => "COM0" }
    );
}

#[test]
fn open_existing_agent_lease() {
    let h = ActorRig::new(SHELL_PROFILE).ready();
    let first = h.request(open_req("agent:a", 30.0, "", false));
    let r = h.request(open_req("agent:b", 30.0, "", false));
    assert_eq!(keys(&r), vec!["ok", "error_code", "interactive_session_id"]);
    assert_eq!(r["error_code"], PyValue::from("SESSION_INTERACTIVE_BUSY"));
    assert_eq!(r["interactive_session_id"], first["interactive_id"]);
}

#[test]
fn open_attached_rescue_banner() {
    let h = attached(SHELL_PROFILE);
    h.inject_rx(AUTOBOOT);
    let r = h.request(open_req("agent:r", 500.0, "", true));
    assert_eq!(r["ok"], PyValue::Bool(true));
    assert_eq!(r["recovery_mode"], PyValue::Bool(true));
    assert_eq!(r["boot_interrupt"], PyValue::Bool(true));
    let id = iid(&r);
    // timeout 夾到 min(t, 120)：121 s 無存取即到期。
    tick_advance(&h, Duration::from_secs(119));
    assert_eq!(h.request(status_req(&id))["ok"], PyValue::Bool(true));
    tick_advance(&h, Duration::from_secs(121));
    assert_eq!(
        h.request(status_req(&id))["error_code"],
        PyValue::from("INTERACTIVE_EXPIRED")
    );
}

#[test]
fn open_attached_not_bootloader() {
    let h = attached(SHELL_PROFILE);
    h.inject_rx(b"\r\nsome random log\r\n");
    let r = h.request(open_req("agent:r", 30.0, "", true));
    assert_eq!(
        r,
        sw_proto::pydict! {
            "ok" => false, "error_code" => "SESSION_NOT_READY", "selector" => "COM0", "error_detail" => "NOT_BOOTLOADER"
        }
    );
    // allow_attached 未開：ATTACHED 一律 SESSION_NOT_READY。
    let r = h.request(open_req("agent:r", 30.0, "", false));
    assert_eq!(
        r,
        sw_proto::pydict! { "ok" => false, "error_code" => "SESSION_NOT_READY", "selector" => "COM0" }
    );
}

#[test]
fn open_attached_rescue_three_arms() {
    // bootloader prompt：recovery_mode 且無 boot_interrupt／login_required。
    let h = attached(BOOTLOADER_LOGIN_PROFILE);
    h.inject_rx(b"\r\n=> ");
    let r = h.request(open_req("agent:r", 30.0, "", true));
    assert_eq!(
        keys(&r),
        vec!["ok", "interactive_id", "session", "recovery_mode"]
    );
    assert_eq!(r["recovery_mode"], PyValue::Bool(true));
    // banner：boot_interrupt。
    let h = attached(BOOTLOADER_LOGIN_PROFILE);
    h.inject_rx(AUTOBOOT);
    let r = h.request(open_req("agent:r", 30.0, "", true));
    assert_eq!(r["boot_interrupt"], PyValue::Bool(true));
    assert!(r.get("login_required").is_none());
    // login prompt：login_required（#174）。
    let h = attached(BOOTLOADER_LOGIN_PROFILE);
    h.inject_rx(b"\r\nOpenWrt login: ");
    let r = h.request(open_req("agent:r", 30.0, "", true));
    assert_eq!(r["login_required"], PyValue::Bool(true));
    assert!(r.get("boot_interrupt").is_none());
    assert_eq!(r["recovery_mode"], PyValue::Bool(true));
}

#[test]
fn lease_send_after_close_is_zero_tx() {
    let h = ActorRig::new(SHELL_PROFILE).ready();
    let id = iid(&h.request(open_req("agent:a", 30.0, "", false)));
    // 已排入 TxWriter、尚未寫出的舊 LeaseOwner job：close 後被 fence OwnerMismatch 拒絕。
    let _ = h.dev.take_tx();
    h.dev.script_write(vec![FakeWriteStep::PauseBeforeSubmit]);
    let pending = h.request_async(send_req(&id, "ab", "plain"));
    assert!(h.dev.wait_paused(Duration::from_secs(2)));
    let submits = h.dev.os_submits();
    assert_eq!(h.request(close_req(&id))["ok"], PyValue::Bool(true));
    h.dev.release_pause();
    let r = pending
        .recv_timeout(Duration::from_secs(5))
        .unwrap()
        .unwrap();
    assert_eq!(
        r["error_code"],
        PyValue::from("INTERACTIVE_EXPIRED"),
        "lease 失效 → INTERACTIVE_EXPIRED（#36）"
    );
    assert_eq!(h.dev.os_submits(), submits, "§11.3 不回放失效上下文");
    assert!(h.dev.take_tx().is_empty());
    // close 後以舊 id 送出 → INTERACTIVE_NOT_FOUND、零 TX。
    let calls = h.dev.write_calls();
    let r = h.request(send_req(&id, "ab", "plain"));
    assert_eq!(
        r,
        sw_proto::pydict! { "ok" => false, "error_code" => "INTERACTIVE_NOT_FOUND", "interactive_id" => id.clone() }
    );
    assert_eq!(h.dev.write_calls(), calls);
}

#[test]
fn lease_send_after_expiry_or_quiet_is_zero_tx() {
    // (a) lease 到期（tick 收回）→ 已排入的 job OwnerMismatch。
    let h = ActorRig::new(SHELL_PROFILE).ready();
    let id = iid(&h.request(open_req("agent:a", 5.0, "", false)));
    let _ = h.dev.take_tx();
    h.dev.script_write(vec![FakeWriteStep::PauseBeforeSubmit]);
    let pending = h.request_async(send_req(&id, "ab", "plain"));
    assert!(h.dev.wait_paused(Duration::from_secs(2)));
    let submits = h.dev.os_submits();
    tick_advance(&h, Duration::from_secs(6));
    h.wait_for("lease 到期", |h| {
        h.get_state()["interactive_session_id"].is_null()
    });
    h.dev.release_pause();
    let r = pending
        .recv_timeout(Duration::from_secs(5))
        .unwrap()
        .unwrap();
    assert_eq!(r["error_code"], PyValue::from("INTERACTIVE_EXPIRED"));
    assert_eq!(h.dev.os_submits(), submits);
    assert!(h.dev.take_tx().is_empty());

    // (b) banner 觸發 quiet re-arm（owner_gen 遞增）→ 已排入的 job OwnerMismatch。
    let h = ActorRig::new(SHELL_PROFILE).ready();
    let id = iid(&h.request(open_req("agent:a", 30.0, "", false)));
    let _ = h.dev.take_tx();
    h.dev.script_write(vec![FakeWriteStep::PauseBeforeSubmit]);
    let pending = h.request_async(send_req(&id, "ab", "plain"));
    assert!(h.dev.wait_paused(Duration::from_secs(2)));
    let submits = h.dev.os_submits();
    h.inject_rx(BANNER);
    h.wait_for("quiet arm", |h| h.fence.with(|f| f.quiet_active));
    h.dev.release_pause();
    let r = pending
        .recv_timeout(Duration::from_secs(5))
        .unwrap()
        .unwrap();
    assert_eq!(r["error_code"], PyValue::from("INTERACTIVE_EXPIRED"));
    assert_eq!(h.dev.os_submits(), submits);
    assert!(h.dev.take_tx().is_empty());
}

#[test]
fn lease_send_refused_at_commit_maps_codes() {
    // #36：interactive_send 提交點被拒：lease 失效 → INTERACTIVE_EXPIRED；drain／cancelled／stale／flashing → SESSION_BUSY。
    // （quiet → AUTOBOOT_QUIET 的映射在 Python 與 Rust 皆不可達：lease class 不受 quiet gate 管制。）
    type Inject = fn(&mut sw_core::tx_writer::FenceState);
    let cases: [(&str, Inject, &str); 5] = [
        ("lease", |f| f.owner_gen += 1, "INTERACTIVE_EXPIRED"),
        ("frozen", |f| f.frozen = true, "SESSION_BUSY"),
        ("stale", |f| f.active_epoch = Some(u64::MAX), "SESSION_BUSY"),
        (
            "cancelled",
            |f| {
                for n in 0..4096 {
                    f.cancelled.insert(OpId(n));
                }
            },
            "SESSION_BUSY",
        ),
        (
            "flashing",
            |f| f.flash_mode = FlashMode::Flashing,
            "SESSION_BUSY",
        ),
    ];
    for (name, inject, code) in cases {
        let h = ActorRig::new(SHELL_PROFILE).ready();
        let id = iid(&h.request(open_req("agent:a", 30.0, "", false)));
        h.dev.script_write(vec![FakeWriteStep::PauseBeforeSubmit]);
        let pending = h.request_async(send_req(&id, "ab", "plain"));
        assert!(h.dev.wait_paused(Duration::from_secs(2)), "{name}");
        let submits = h.dev.os_submits();
        h.fence.with(inject);
        h.dev.release_pause();
        let r = pending
            .recv_timeout(Duration::from_secs(5))
            .unwrap()
            .unwrap();
        assert_eq!(
            r,
            sw_proto::pydict! { "ok" => false, "error_code" => code, "interactive_id" => id.clone() },
            "{name}"
        );
        assert_eq!(h.dev.os_submits(), submits, "{name}：零 TX");
    }
}

#[test]
fn rescue_lease_send_during_quiet_reaches_uart() {
    // R2-F2：有效 Lease 可中斷 autoboot。
    let h = attached(SHELL_PROFILE);
    h.inject_rx(AUTOBOOT);
    h.wait_for("quiet", |h| h.fence.with(|f| f.quiet_active));
    let open = h.request(open_req("agent:rescue", 30.0, "", true));
    assert_eq!(open["recovery_mode"], PyValue::Bool(true));
    assert_eq!(open["boot_interrupt"], PyValue::Bool(true));
    let id = iid(&open);
    let _ = h.dev.take_tx();
    let calls = h.dev.write_calls();
    let r = h.request(send_req(&id, "ctrl-c", "key"));
    assert_eq!(keys(&r), vec!["ok", "interactive_id", "bytes"]);
    assert_eq!(r["ok"], PyValue::Bool(true));
    assert_eq!(r["bytes"], int(1));
    assert_eq!(h.dev.write_calls(), calls + 1);
    assert_eq!(h.dev.take_tx(), b"\x03".to_vec());
    let rows: Vec<PyDict> = tx_rows(&h)
        .into_iter()
        .filter(|r| r["source"] == PyValue::from("agent:rescue"))
        .collect();
    assert_eq!(rows.len(), 1);
    assert!(h.fence.with(|f| f.quiet_active), "送出後 quiet 仍為 true");

    // 對照：READY＋banner quiet＋有效 agent lease：命令被 AUTOBOOT_QUIET 擋下，lease send 照常寫出。
    let h = ActorRig::new(SHELL_PROFILE).ready();
    h.inject_rx(BANNER);
    h.wait_for("quiet", |h| h.fence.with(|f| f.quiet_active));
    let id = iid(&h.request(open_req("agent:x", 2.0, "", false)));
    let calls = h.dev.write_calls();
    let sub = h.request(SessionRequest::Submit(agent_submit("ls")));
    assert_eq!(sub["error_code"], PyValue::from("AUTOBOOT_QUIET"));
    assert!(matches!(sub["retry_after_s"], PyValue::Float(_)));
    assert_eq!(h.dev.write_calls(), calls);
    assert_eq!(
        h.request(send_req(&id, "enter", "key"))["ok"],
        PyValue::Bool(true)
    );
    assert_eq!(h.dev.write_calls(), calls + 1);
    tick_advance(&h, Duration::from_secs(3));
    let r = h.request(send_req(&id, "enter", "key"));
    assert_eq!(r["error_code"], PyValue::from("INTERACTIVE_EXPIRED"));
    assert_eq!(h.dev.write_calls(), calls + 1);
}

#[test]
fn send_encodings() {
    assert_eq!(
        encode_interactive_payload("héllo", "plain").unwrap(),
        "héllo".as_bytes()
    );
    assert_eq!(
        encode_interactive_payload("aGk=\n!", "base64").unwrap(),
        b"hi"
    );
    let table: [(&str, &[u8]); 9] = [
        ("enter", b"\n"),
        ("tab", b"\t"),
        ("escape", b"\x1b"),
        ("ctrl-c", b"\x03"),
        ("ctrl-d", b"\x04"),
        ("up", b"\x1b[A"),
        ("down", b"\x1b[B"),
        ("right", b"\x1b[C"),
        ("left", b"\x1b[D"),
    ];
    for (key, bytes) in table {
        assert_eq!(
            encode_interactive_payload(key, "key").unwrap(),
            bytes,
            "{key}"
        );
    }
    assert_eq!(
        encode_interactive_payload("  Enter \n", "key").unwrap(),
        b"\n"
    );
    let err: PyErr = encode_interactive_payload("f1", "key").unwrap_err();
    assert_eq!(err.message, "INVALID_INTERACTIVE_ENCODING");
    let err: PyErr = encode_interactive_payload("x", "hex").unwrap_err();
    assert_eq!(err.message, "INVALID_INTERACTIVE_ENCODING");
    assert!(
        encode_interactive_payload("a", "base64").is_err(),
        "padding 錯誤為例外"
    );
}

#[test]
fn send_expired_closes() {
    let h = ActorRig::new(SHELL_PROFILE).ready();
    let id = iid(&h.request(open_req("agent:a", 1.0, "", false)));
    h.clock.advance(Duration::from_millis(1_001));
    let expected = sw_proto::pydict! { "ok" => false, "error_code" => "INTERACTIVE_EXPIRED", "interactive_id" => id.clone() };
    assert_eq!(h.request(send_req(&id, "x", "plain")), expected);
    // lazy 到期同樣進 tombstone：之後 send／status／close 仍回 INTERACTIVE_EXPIRED（#54）。
    assert_eq!(h.request(send_req(&id, "x", "plain")), expected);
    assert_eq!(h.request(status_req(&id)), expected);
    assert_eq!(h.request(close_req(&id)), expected);
    // 被每 session FIFO 16 擠出後才 INTERACTIVE_NOT_FOUND。
    for _ in 0..15 {
        let other = iid(&h.request(open_req("agent:b", 1.0, "", false)));
        h.clock.advance(Duration::from_millis(1_001));
        assert_eq!(
            h.request(status_req(&other))["error_code"],
            PyValue::from("INTERACTIVE_EXPIRED")
        );
    }
    assert_eq!(
        h.request(status_req(&id))["error_code"],
        PyValue::from("INTERACTIVE_EXPIRED"),
        "仍在 16 筆內"
    );
    let last = iid(&h.request(open_req("agent:c", 1.0, "", false)));
    h.clock.advance(Duration::from_millis(1_001));
    // Python `interactive_close` 不做 lazy 到期判定（只看 tick／lazy 已設的 status）：以 status 觸發 lazy 到期。
    assert_eq!(
        h.request(status_req(&last))["error_code"],
        PyValue::from("INTERACTIVE_EXPIRED")
    );
    assert_eq!(
        h.request(status_req(&id)),
        sw_proto::pydict! { "ok" => false, "error_code" => "INTERACTIVE_NOT_FOUND", "interactive_id" => id.clone() }
    );
}

#[test]
fn lazy_expiry_keeps_tombstone_expired() {
    // sans-IO（無 tick）：lazy 到期（status 觸發）留 tombstone；重複查詢仍 EXPIRED（#54，Python keep_tombstone=True）。
    let mut h = CoreHarness::new(SHELL_PROFILE);
    h.to_ready();
    let id = iid(&h.rpc(open_req("agent:a", 1.0, "", false)).unwrap());
    h.clock.advance(Duration::from_millis(1_001));
    for _ in 0..3 {
        let r = h.rpc(status_req(&id)).unwrap();
        assert_eq!(r["error_code"], PyValue::from("INTERACTIVE_EXPIRED"));
    }
    assert!(pget(&h.public(), "interactive_session_id").is_null());
    // 一般 close（非到期）不留 tombstone → NOT_FOUND。
    let other = iid(&h.rpc(open_req("agent:b", 30.0, "", false)).unwrap());
    assert_eq!(h.rpc(close_req(&other)).unwrap()["ok"], PyValue::Bool(true));
    assert_eq!(
        h.rpc(close_req(&other)).unwrap()["error_code"],
        PyValue::from("INTERACTIVE_NOT_FOUND")
    );
}

#[test]
fn status_touches_and_returns_screen() {
    let h = ActorRig::new(SHELL_PROFILE).ready();
    let id = iid(&h.request(open_req("agent:a", 2.0, "", false)));
    h.inject_rx(b"\x1b[1mhello\x1b[0m\r\nroot@t:/# ");
    tick_advance(&h, Duration::from_millis(1_500));
    let r = h.request(status_req(&id));
    assert_eq!(
        keys(&r),
        vec![
            "ok",
            "interactive_id",
            "owner",
            "status",
            "recovery_mode",
            "screen",
            "session"
        ]
    );
    assert_eq!(r["owner"], PyValue::from("agent:a"));
    assert_eq!(r["status"], PyValue::from("active"));
    assert_eq!(r["recovery_mode"], PyValue::Bool(false));
    assert_eq!(r["screen"], PyValue::from(clean_text(&h.rx.tail(2048))));
    assert!(r["screen"].as_str().unwrap().contains("hello"));
    // status 延長期限：自 touch 起 1.5 s 仍有效（原期限已過 3 s）。
    tick_advance(&h, Duration::from_millis(1_500));
    assert_eq!(h.request(status_req(&id))["ok"], PyValue::Bool(true));
}

#[test]
fn tick_expires_lease_sc13() {
    let h = ActorRig::new(SHELL_PROFILE).ready();
    let id = iid(&h.request(open_req("agent:a", 3.0, "", false)));
    tick_advance(&h, Duration::from_secs(2));
    assert!(!h.get_state()["interactive_session_id"].is_null());
    tick_advance(&h, Duration::from_millis(1_500));
    // 無存取超過 timeout → 1 s tick 主動移除 lease 並留 tombstone。
    h.wait_for("tick 收回 lease", |h| {
        h.get_state()["interactive_session_id"].is_null()
    });
    // Python `_expire_interactive_leases_tick` 以 keep_tombstone=True 收回：status 回 INTERACTIVE_EXPIRED。
    assert_eq!(
        h.request(status_req(&id)),
        sw_proto::pydict! { "ok" => false, "error_code" => "INTERACTIVE_EXPIRED", "interactive_id" => id.clone() }
    );
    // lease 已移除：命令不再被 SESSION_INTERACTIVE_BUSY 擋下。
    h.dev.on_tx_reply(b"echo z\n", b"echo z\r\nz\r\nroot@t:/# ");
    let cmd = h.submit_ok("echo z");
    assert_eq!(h.wait_done(&cmd)["stdout"], PyValue::from("z"));
}

#[test]
fn lease_expiry_checks_current_deadline() {
    let mut h = CoreHarness::new(SHELL_PROFILE);
    h.to_ready();
    let id = iid(&h.rpc(open_req("agent:a", 10.0, "", false)).unwrap());
    h.advance(Duration::from_secs(8));
    assert_eq!(h.rpc(status_req(&id)).unwrap()["ok"], PyValue::Bool(true));
    // 舊 LeaseExpiry{deadline: 10 s} 到達：touch 後期限為 18 s → 不關閉。
    h.advance(Duration::from_secs(3));
    assert_eq!(
        pget(&h.public(), "interactive_session_id"),
        PyValue::from(id.clone())
    );
    h.advance(Duration::from_secs(8));
    assert!(pget(&h.public(), "interactive_session_id").is_null());
}

// ---------------------------------------------------------------------------
// self-test
// ---------------------------------------------------------------------------

fn class_of(r: &PyDict) -> (String, String) {
    (
        r["classification"].as_str().unwrap_or("").to_string(),
        r["recommended_action"].as_str().unwrap_or("").to_string(),
    )
}

#[test]
fn selftest_order_table() {
    // RELEASED
    let h = ActorRig::new(SHELL_PROFILE).ready();
    h.request(SessionRequest::Release {
        source: "cli".into(),
        reason: PyValue::Null,
    });
    h.wait_state("RELEASED");
    let r = h.request(self_test());
    assert_eq!(class_of(&r), ("RELEASED".into(), "device_attach".into()));
    assert_lease_ctx_keys(&r);
    assert_eq!(r["external_holder"], PyValue::from("none"));

    // recovering（reboot 後 RECOVERING）
    let h = ActorRig::new(SHELL_PROFILE).ready();
    let id = h.submit_ok("reboot");
    let deadline = Instant::now() + Duration::from_secs(5);
    while h
        .command_get(&id)
        .get("done_at")
        .is_none_or(PyValue::is_null)
    {
        assert!(Instant::now() < deadline);
        tick_advance(&h, Duration::from_millis(500));
    }
    let r = h.request(self_test());
    assert_eq!(class_of(&r), ("SESSION_RECOVERING".into(), "wait".into()));
    assert_lease_ctx_keys(&r);

    // DEVICE_MISSING
    let h = ActorRig::new(SHELL_PROFILE);
    let r = h.request(self_test());
    assert_eq!(
        class_of(&r),
        ("DEVICE_MISSING".into(), "check_cable_or_bind".into())
    );
    assert_lease_ctx_keys(&r);

    // BRIDGE_DOWN（裝置在、bridge 開啟失敗）
    let mut c = CoreHarness::new(SHELL_PROFILE);
    c.feed(ControlEvent::DeviceAttached {
        by_id: DEVICE_BY_ID.into(),
        real_path: REAL_PATH.into(),
    });
    c.feed(ControlEvent::BridgeOpened {
        epoch_hint: 0,
        result: Err(OpenFailure::Other(PalError::io(5, "Input/output error"))),
    });
    let r = c.rpc(self_test()).unwrap();
    assert_eq!(class_of(&r), ("BRIDGE_DOWN".into(), "attach".into()));
    assert_eq!(
        keys(&r)[..4],
        ["ok", "classification", "session", "current_real_path"]
    );
    assert_lease_ctx_keys(&r);

    // PASSTHROUGH（ATTACHED）
    let mut c = CoreHarness::new(PASSTHROUGH_PROFILE);
    c.to_attached(None);
    let r = c.rpc(self_test()).unwrap();
    assert_eq!(
        class_of(&r),
        ("PASSTHROUGH".into(), "console_attach".into())
    );
    assert_lease_ctx_keys(&r);

    // LOGIN_REQUIRED／BOOTLOADER（fallback prompts）／RX_FLOOD／ATTACHED_NOT_READY
    for (last_error, rx, expected, action) in [
        (
            Some("LOGIN_REQUIRED"),
            &b""[..],
            "LOGIN_REQUIRED",
            "console_attach",
        ),
        (None, &b"\r\n=> "[..], "BOOTLOADER", "recover_interactive"),
        (Some("RX_FLOOD"), &b""[..], "RX_FLOOD", "wait"),
        (
            None,
            &b"\r\nnoise\r\n"[..],
            "ATTACHED_NOT_READY",
            "console_attach",
        ),
    ] {
        let mut c = CoreHarness::new(SHELL_PROFILE);
        c.to_attached(last_error);
        if !rx.is_empty() {
            let epoch = c.epoch();
            c.rx.append(rx, c.clock.mono_ns(), epoch, 1);
        }
        let r = c.rpc(self_test()).unwrap();
        assert_eq!(
            class_of(&r),
            (expected.to_string(), action.to_string()),
            "{expected}"
        );
        assert_eq!(
            keys(&r)[..8],
            [
                "ok",
                "classification",
                "session",
                "attached_real_path",
                "current_real_path",
                "attached_vtty",
                "bridge_generation",
                "recommended_action"
            ]
        );
        if expected == "BOOTLOADER" {
            assert_eq!(r["matched_prompt"], PyValue::from("^=> $"));
            assert!(r.get("rx_tail").is_some());
        }
        if expected == "RX_FLOOD" {
            assert!(r.get("rx_bytes_last_10s").is_some());
        }
        assert_lease_ctx_keys(&r);
    }

    // AUTOBOOT_QUIET（READY 但 banner 已 arm）
    let mut c = CoreHarness::new(SHELL_PROFILE);
    c.to_ready();
    c.rx(BANNER);
    let r = c.rpc(self_test()).unwrap();
    assert_eq!(class_of(&r), ("AUTOBOOT_QUIET".into(), "wait".into()));
    assert_eq!(
        keys(&r),
        vec![
            "ok",
            "classification",
            "session",
            "recommended_action",
            "interactive_owner",
            "human_attached",
            "human_active",
            "recovery_mode",
            "command_capable"
        ]
    );
}

#[test]
fn selftest_passthrough_zero_tx() {
    let h = ActorRig::new(UBOOT_PASSTHROUGH_PROFILE).attach();
    h.wait_state("ATTACHED");
    h.advance(Duration::from_secs(12));
    let r = h.request(self_test());
    assert_eq!(
        class_of(&r),
        ("PASSTHROUGH".into(), "console_attach".into())
    );
    assert_eq!(r["passthrough_ready_in_s"], PyValue::from(18.0));
    assert!(r.get("probe_ok").is_none());
    assert_eq!(h.dev.write_calls(), 0);
    h.advance(Duration::from_secs(18));
    h.wait_state("READY");
    let r = h.request(self_test());
    assert_eq!(class_of(&r), ("PASSTHROUGH".into(), "none".into()));
    assert!(r.get("probe_ok").is_none());
    assert_eq!(h.dev.write_calls(), 0, "ready_probe 非空也不得送 probe");
    assert_lease_ctx_keys(&r);
}

#[test]
fn selftest_ok_confirms_ready() {
    let h = ActorRig::new(SHELL_PROFILE).ready();
    make_pending(&h);
    let r = h.request(self_test());
    assert_eq!(class_of(&r), ("OK".into(), "none".into()));
    assert_eq!(r["probe_ok"], PyValue::Bool(true));
    assert_eq!(
        keys(&r),
        vec![
            "ok",
            "classification",
            "session",
            "attached_real_path",
            "current_real_path",
            "attached_vtty",
            "bridge_generation",
            "probe_ok",
            "recommended_action",
            "interactive_owner",
            "human_attached",
            "human_active",
            "recovery_mode",
            "command_capable"
        ]
    );
    assert_eq!(
        h.get_state()["ready_reconfirm_pending"],
        PyValue::Bool(false)
    );
}

#[test]
fn selftest_quiet_armed_after_precheck_before_submit_zero_tx() {
    let h = ActorRig::new(SHELL_PROFILE).ready();
    assert!(!h.fence.with(|f| f.quiet_active));
    let _ = h.dev.take_tx();
    let calls = h.dev.write_calls();
    let submits = h.dev.os_submits();
    h.dev.script_write(vec![FakeWriteStep::PauseBeforeSubmit]);
    let pending = h.request_async(self_test());
    assert!(
        h.dev.wait_paused(Duration::from_secs(2)),
        "probe 應已過 TxWriter 出列檢查"
    );
    h.inject_rx(b"U-Boot 2024.04 (Oct 01 2024)\r\n");
    h.wait_for("quiet arm", |h| h.fence.with(|f| f.quiet_active));
    let clock_before = h.clock.elapsed();
    h.dev.release_pause();
    let r = pending
        .recv_timeout(Duration::from_secs(2))
        .expect("不等 nonce 即回覆")
        .unwrap();
    assert_eq!(h.clock.elapsed(), clock_before, "不推進 FakeClock");
    let expected_keys = vec![
        "ok",
        "classification",
        "session",
        "recommended_action",
        "interactive_owner",
        "human_attached",
        "human_active",
        "recovery_mode",
        "command_capable",
    ];
    assert_eq!(keys(&r), expected_keys);
    assert_eq!(class_of(&r), ("AUTOBOOT_QUIET".into(), "wait".into()));
    assert!(r.get("probe_ok").is_none());
    assert_eq!(h.dev.write_calls(), calls + 1, "probe job 已進 PAL");
    assert_eq!(h.dev.os_submits(), submits, "FenceSubmitGate 以 Quiet 拒絕");
    assert!(h.dev.take_tx().is_empty());
    assert!(
        !tx_rows(&h)
            .iter()
            .any(|r| r["source"] == PyValue::from("system:self_test"))
    );
    let s = h.get_state();
    assert_eq!(s["ready_reconfirm_pending"], PyValue::Bool(true));
    assert_eq!(s["state"], PyValue::from("READY"));
    let again = h.request(self_test());
    assert_eq!(keys(&again), expected_keys);
    assert_eq!(class_of(&again), class_of(&r));
    assert_eq!(again["ok"], r["ok"]);
    assert_eq!(h.dev.os_submits(), submits);
}

#[test]
fn selftest_quiet_armed_during_submit_counts_committed() {
    let h = ready_once(SHELL_PROFILE);
    let submits = h.dev.os_submits();
    h.dev.script_write(vec![FakeWriteStep::PauseInsideSubmit]);
    let pending = h.request_async(self_test());
    assert!(
        h.dev.wait_paused(Duration::from_secs(2)),
        "probe 已在 FenceSubmitGate 內（持 Fence 鎖）"
    );
    h.dev.push_rx(BANNER);
    std::thread::sleep(Duration::from_millis(200));
    assert!(
        h.fence.try_with(|f| f.quiet_active).is_none(),
        "actor 的 arm 須等 Fence 鎖"
    );
    h.dev.release_pause();
    h.wait_for("probe 提交", |h| {
        tx_rows(h)
            .iter()
            .any(|r| r["source"] == PyValue::from("system:self_test"))
    });
    assert_eq!(
        h.dev.os_submits(),
        submits + 1,
        "probe bytes 恰 1 次 OS 提交"
    );
    let rows: Vec<PyDict> = tx_rows(&h)
        .into_iter()
        .filter(|r| r["source"] == PyValue::from("system:self_test"))
        .collect();
    assert_eq!(rows.len(), 1);
    let got: Mutex<Option<Result<PyDict, PyErr>>> = Mutex::new(None);
    let deadline = Instant::now() + Duration::from_secs(5);
    while got.lock().unwrap().is_none() {
        assert!(Instant::now() < deadline);
        h.clock.advance(Duration::from_millis(250));
        std::thread::sleep(Duration::from_millis(60));
        if let Ok(v) = pending.try_recv() {
            *got.lock().unwrap() = Some(v);
        }
    }
    let r = got.into_inner().unwrap().unwrap().unwrap();
    assert_eq!(
        class_of(&r),
        ("TARGET_UNRESPONSIVE".into(), "recover".into())
    );
    assert_eq!(r["probe_ok"], PyValue::Bool(false));
    assert!(h.fence.with(|f| f.quiet_active));
}

#[test]
fn selftest_probe_other_reject_zero_tx_matches_python() {
    let golden_text = std::fs::read_to_string(repo_path("contracts/golden/selftest_refused.json"))
    .expect("golden");
    let PyValue::Dict(golden) = loads(&golden_text).expect("json") else {
        panic!()
    };
    type Inject = fn(&mut sw_core::tx_writer::FenceState);
    let cases: [(&str, Inject); 4] = [
        ("drain", |f| f.frozen = true),
        ("stale_generation", |f| f.active_epoch = Some(u64::MAX)),
        ("cancelled", |f| {
            for n in 0..4096 {
                f.cancelled.insert(OpId(n));
            }
        }),
        ("wal_unhealthy", |f| f.wal_unhealthy = true),
    ];
    for (name, inject) in cases {
        let h = ActorRig::new(SHELL_PROFILE).ready();
        let _ = h.dev.take_tx();
        let pending_before = h.get_state()["ready_reconfirm_pending"].clone();
        h.dev.script_write(vec![FakeWriteStep::PauseBeforeSubmit]);
        let rx = h.request_async(self_test());
        assert!(h.dev.wait_paused(Duration::from_secs(2)), "{name}");
        let clock_before = h.clock.elapsed();
        h.fence.with(inject);
        h.dev.release_pause();
        let r = rx.recv_timeout(Duration::from_secs(3)).expect("不等 nonce");
        let err = r.expect_err("Python 例外穿出 manager → framing EXCEPTION");
        let PyValue::Dict(expected) = golden[name].clone() else {
            panic!()
        };
        assert_eq!(expected["error_code"], PyValue::from("EXCEPTION"));
        assert_eq!(
            PyValue::from(err.message.clone()),
            expected["message"],
            "{name}"
        );
        assert!(
            h.clock.elapsed() - clock_before < Duration::from_secs(2),
            "{name}：不等 nonce"
        );
        assert!(h.dev.take_tx().is_empty(), "{name}：零 TX");
        h.fence.with(|f| {
            f.frozen = false;
            f.wal_unhealthy = false;
            f.cancelled.clear();
        });
        assert_eq!(
            h.get_state()["ready_reconfirm_pending"],
            pending_before,
            "{name}"
        );
    }
}

#[test]
fn selftest_during_reconfirm_pending_not_fenced() {
    let h = ActorRig::new(SHELL_PROFILE).ready();
    make_pending(&h);
    let calls = h.dev.write_calls();
    // 同狀態 CmdSubmit：Python `_agent_gate_reject_locked` → pending 仍在期限內 → AUTOBOOT_QUIET（retry 5.0）。
    let sub = h.request(SessionRequest::Submit(agent_submit("ls")));
    assert_eq!(sub["error_code"], PyValue::from("AUTOBOOT_QUIET"));
    assert_eq!(sub["retry_after_s"], PyValue::from(5.0));
    assert_eq!(h.dev.write_calls(), calls);
    let r = h.request(self_test());
    assert_eq!(class_of(&r), ("OK".into(), "none".into()));
    assert_eq!(h.dev.write_calls(), calls + 1, "只多 probe 那 1 次");
}

#[test]
fn selftest_unresponsive_counts_attempt() {
    // Python 基線：self-test 的 TARGET_UNRESPONSIVE 不累加 ready_reconfirm_attempts（只有自動 reprobe 計次，
    // `_finish_probe_reprobe`）；pending 保留、不落 READY_UNCONFIRMED。
    let h = ready_once(SHELL_PROFILE);
    make_pending(&h);
    for _ in 0..6 {
        let rx = h.request_async(self_test());
        let deadline = Instant::now() + Duration::from_secs(5);
        let r = loop {
            if let Ok(v) = rx.try_recv() {
                break v.unwrap();
            }
            assert!(Instant::now() < deadline);
            h.clock.advance(Duration::from_millis(250));
            std::thread::sleep(Duration::from_millis(60));
        };
        assert_eq!(
            class_of(&r),
            ("TARGET_UNRESPONSIVE".into(), "recover".into())
        );
        assert_eq!(r["probe_ok"], PyValue::Bool(false));
    }
    let s = h.get_state();
    assert_eq!(s["ready_reconfirm_pending"], PyValue::Bool(true));
    assert_eq!(
        s["ready_reconfirm_failed"],
        PyValue::Bool(false),
        "self-test 不計入 5 次上限"
    );
}

#[test]
fn selftest_released_external_holder() {
    let h = ActorRig::new_with(SHELL_PROFILE, |core| {
        core.set_self_test_probes(SelfTestProbes {
            holders: Some(Arc::new(FakeUartExclusive {
                report: Mutex::new(HashMap::new()),
            })),
            vtty_alive: None,
        });
    })
    .ready();
    h.request(SessionRequest::Release {
        source: "flash".into(),
        reason: PyValue::from("upgrade"),
    });
    h.wait_state("RELEASED");
    let r = h.request(self_test());
    assert_eq!(
        keys(&r)[..10],
        [
            "ok",
            "classification",
            "session",
            "released_by",
            "released_at",
            "reason",
            "external_holder",
            "reclaimable",
            "recommended_action",
            "interactive_owner"
        ]
    );
    assert_eq!(r["classification"], PyValue::from("RELEASED"));
    assert_eq!(r["released_by"], PyValue::from("flash"));
    assert_eq!(r["reason"], PyValue::from("upgrade"));
    assert_eq!(r["external_holder"], PyValue::from("none"));
    assert_eq!(
        r["reclaimable"],
        PyValue::Bool(false),
        "BC-04：holder 不明不得當安全證明"
    );
    assert_eq!(
        r["recommended_action"],
        PyValue::from("wait_external_flash")
    );
}

#[test]
fn selftest_device_rebound_required() {
    let h = ActorRig::new(SHELL_PROFILE).ready();
    h.send_control(ControlEvent::DeviceAttached {
        by_id: DEVICE_BY_ID.into(),
        real_path: "/fake/tty-B".into(),
    });
    let r = h.request(self_test());
    assert_eq!(
        class_of(&r),
        ("DEVICE_REBOUND_REQUIRED".into(), "reattach".into())
    );
    assert_eq!(r["attached_real_path"], PyValue::from(REAL_PATH));
    assert_eq!(r["current_real_path"], PyValue::from("/fake/tty-B"));
    assert_lease_ctx_keys(&r);
}

#[test]
fn selftest_vtty_stale() {
    let alive = Arc::new(AtomicBool::new(true));
    let probe = alive.clone();
    let h = ActorRig::new_with(SHELL_PROFILE, move |core| {
        core.set_self_test_probes(SelfTestProbes {
            holders: None,
            vtty_alive: Some(Arc::new(move || Some(probe.load(Ordering::SeqCst)))),
        });
    })
    .ready();
    alive.store(false, Ordering::SeqCst);
    let r = h.request(self_test());
    assert_eq!(class_of(&r), ("VTTY_STALE".into(), "console_attach".into()));
    assert_eq!(
        keys(&r)[..5],
        [
            "ok",
            "classification",
            "session",
            "attached_vtty",
            "recommended_action"
        ]
    );
}

// ---------------------------------------------------------------------------
// agent log capture
// ---------------------------------------------------------------------------

fn log_profile(dir: &str) -> String {
    format!(
        "profiles:\n  t:\n    platform: shell\n    prompt_regex: '(?m)^root@t:.*# '\n    timeout_s: 2.0\n    log_dir: {dir}\ntargets:\n  - {{com: COM0, profile: t, act_no: 1, alias: t+1, device_by_id: /s/by-id/usb-A}}\n"
    )
}

#[test]
fn log_capture_start_stop_status() {
    let tmp = tempfile::tempdir().unwrap();
    let dir = tmp.path().join("logs").to_string_lossy().into_owned();
    let yaml = log_profile(&dir);
    let clock = sw_pal::fakes::clock::FakeClock::new();
    let wall_clock: Arc<dyn Clock> = clock.clone();
    let h = ActorRig::new_with(&yaml, move |core| {
        core.set_capture_env(CaptureEnv {
            clock: Some(wall_clock),
            ..CaptureEnv::default()
        });
    })
    .ready();
    let st = h.request(SessionRequest::LogStatus);
    assert_eq!(keys(&st), vec!["ok", "active", "session"]);
    assert_eq!(st["active"], PyValue::Bool(false));
    let start = h.request(SessionRequest::LogStart);
    assert_eq!(
        keys(&start),
        vec!["ok", "capture_id", "log_path", "session"]
    );
    let path = start["log_path"].as_str().unwrap().to_string();
    assert_eq!(path, format!("{dir}/COM0_700101-000000.log"));
    assert!(start["capture_id"].as_str().unwrap().contains('-'));
    // 重複 start → already_active。
    let again = h.request(SessionRequest::LogStart);
    assert_eq!(again["already_active"], PyValue::Bool(true));
    assert_eq!(again["capture_id"], start["capture_id"]);
    // 前景執行中 RX 不寫入 log（EVT-D-L1）。
    h.dev
        .on_tx_reply(b"echo fg\n", b"echo fg\r\nFGDATA\r\nroot@t:/# ");
    let id = h.submit_ok("echo fg");
    h.wait_done(&id);
    h.inject_rx(b"IDLE\r\n");
    h.wait_for("log 寫入", |h| {
        h.request(SessionRequest::LogStatus)["line_count"] == int(1)
    });
    let status = h.request(SessionRequest::LogStatus);
    assert_eq!(
        keys(&status),
        vec![
            "ok",
            "active",
            "capture_id",
            "log_path",
            "line_count",
            "byte_count",
            "started_at",
            "session"
        ]
    );
    let stop = h.request(SessionRequest::LogStop);
    assert_eq!(
        keys(&stop),
        vec![
            "ok",
            "capture_id",
            "log_path",
            "line_count",
            "byte_count",
            "started_at"
        ]
    );
    assert_eq!(stop["line_count"], int(1));
    assert_eq!(stop["byte_count"], int(5));
    let text = std::fs::read_to_string(&path).unwrap();
    assert_eq!(text, "IDLE\n");
    assert!(!text.contains("FGDATA"));
    let stop2 = h.request(SessionRequest::LogStop);
    assert_eq!(stop2["error_code"], PyValue::from("NO_ACTIVE_CAPTURE"));
}

#[test]
fn log_capture_open_failure() {
    let tmp = tempfile::tempdir().unwrap();
    let dir = tmp.path().to_string_lossy().into_owned();
    // 目標檔名已被目錄佔用 → open 失敗（root 環境下權限擋不住，改以 EISDIR 模擬）。
    std::fs::create_dir_all(tmp.path().join("COM0_700101-000000.log")).unwrap();
    let clock = sw_pal::fakes::clock::FakeClock::new();
    let wall_clock: Arc<dyn Clock> = clock.clone();
    let h = ActorRig::new_with(&log_profile(&dir), move |core| {
        core.set_capture_env(CaptureEnv {
            clock: Some(wall_clock),
            ..CaptureEnv::default()
        });
    })
    .ready();
    let r = h.request(SessionRequest::LogStart);
    assert_eq!(keys(&r), vec!["ok", "error_code", "detail"]);
    assert_eq!(r["ok"], PyValue::Bool(false));
    assert_eq!(r["error_code"], PyValue::from("LOG_OPEN_FAILED"));
    assert!(r["detail"].as_str().unwrap().contains("Errno"));
}

#[test]
fn log_capture_dir_not_creatable_is_exception() {
    let tmp = tempfile::tempdir().unwrap();
    let file = tmp.path().join("plain-file");
    std::fs::write(&file, b"x").unwrap();
    let dir = file.join("sub").to_string_lossy().into_owned();
    let h = ActorRig::new(&log_profile(&dir)).ready();
    let rx = h.request_async(SessionRequest::LogStart);
    let r = rx.recv_timeout(Duration::from_secs(5)).unwrap();
    let err = r.expect_err("makedirs 失敗為例外（framing EXCEPTION），不是 LOG_OPEN_FAILED");
    assert!(err.message.contains("Errno"), "{}", err.message);
}

#[test]
fn selftest_probe_drain_race_golden() {
    // R5 審查後續：drain（release）與 probe 提交競態——結果只有「probe 已提交（回 nonce 結果）」或
    // 「零 TX＋golden 碼（drain → EXCEPTION `tx incomplete: not_submitted 0/23 draining`）」兩種。
    let golden_text = std::fs::read_to_string(repo_path("contracts/golden/selftest_refused.json"))
    .expect("golden");
    let PyValue::Dict(golden) = loads(&golden_text).expect("json") else {
        panic!()
    };
    let PyValue::Dict(drain) = golden["drain"].clone() else {
        panic!()
    };
    let iterations: usize = std::env::var("SW_T14_RACE_ITERS")
        .ok()
        .and_then(|v| v.parse().ok())
        .unwrap_or(1000);
    let h = ActorRig::new(SHELL_PROFILE).ready();
    let (mut committed, mut refused) = (0usize, 0usize);
    for i in 0..iterations {
        let _ = h.dev.take_tx();
        let submits = h.dev.os_submits();
        let probe_rx = h.request_async(self_test());
        // 錯開 0～1.9 ms，讓兩種結果（提交前／提交後被 drain）都會出現。
        std::thread::sleep(Duration::from_micros((i % 20) as u64 * 100));
        // release 經 control lane（與 self-test 同 FIFO）：保證 pre-check 先通過，競態只落在 probe 提交點。
        let (release_tx, release_rx) = crossbeam_channel::bounded(1);
        h.send_control(ControlEvent::Request {
            req: SessionRequest::Release {
                source: "cli".into(),
                reason: PyValue::Null,
            },
            reply: release_tx,
        });
        let deadline = Instant::now() + Duration::from_secs(5);
        let result = loop {
            if let Ok(v) = probe_rx.try_recv() {
                break v;
            }
            assert!(Instant::now() < deadline, "第 {i} 輪 self-test 未回覆");
            // probe 已提交但 nonce 回應被 drain 截斷時，以 FakeClock 推進讓 nonce 等待逾時。
            h.clock.advance(Duration::from_millis(100));
            std::thread::sleep(Duration::from_millis(1));
        };
        match result {
            Ok(r) => {
                let class = r["classification"].as_str().unwrap_or("").to_string();
                assert!(
                    class == "OK" || class == "TARGET_UNRESPONSIVE",
                    "第 {i} 輪：probe 已提交只能回 nonce 結果，實際 {r:?}"
                );
                assert_eq!(h.dev.os_submits(), submits + 1, "第 {i} 輪：計入 committed");
                committed += 1;
            }
            Err(err) => {
                assert_eq!(
                    PyValue::from(err.message.clone()),
                    drain["message"],
                    "第 {i} 輪"
                );
                assert_eq!(h.dev.os_submits(), submits, "第 {i} 輪：零 TX");
                refused += 1;
            }
        }
        let rel = release_rx
            .recv_timeout(Duration::from_secs(5))
            .expect("release 回覆")
            .expect("release ok");
        assert_eq!(rel["ok"], PyValue::Bool(true));
        h.wait_state("RELEASED");
        let _ = h.dev.take_tx();
        h.dev.on_tx_reply(b"\n", b"root@t:/# ");
        let attach = h.request(SessionRequest::DeviceAttach { force: true });
        assert_eq!(
            attach["ok"],
            PyValue::Bool(true),
            "第 {i} 輪 reattach：{attach:?}"
        );
        h.wait_state("READY");
    }
    eprintln!("selftest_probe_drain_race_golden: committed={committed} refused={refused}");
    assert_eq!(committed + refused, iterations);
}
