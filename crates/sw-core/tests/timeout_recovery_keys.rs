//! profile `timeout_recovery_keys`：命令逾時與 READY 時 `session recover` 依設定送救援鍵。
//!
//! 斷言值以 Python 基線為準（`sw_core/config.py` 的 `_as_timeout_recovery_keys`、
//! `sw_core/session_manager.py` 的 `_recover_after_failure`；Python 測試 `tests/test_timeout_recovery_keys.py`）：
//! - 未設定＝先 CTRL_C 再 CTRL_D（既有行為）；
//! - `[CTRL_C]` 永不送 CTRL_D，CTRL_C 救不回即 PROMPT_TIMEOUT、session 轉 ATTACHED；
//! - `[]` 一個鍵都不送，直接落到 PROMPT_TIMEOUT 收尾；
//! - 清單順序即送出順序；target 覆寫 template。

#![cfg(feature = "test-fakes")]

mod common;
mod support;

use std::cell::RefCell;
use std::time::Duration;

use support::*;
use sw_core::session_core::SessionRequest;
use sw_proto::pycompat::PyErr;
use sw_proto::pyjson::{PyDict, PyValue};

fn shell_profile(template_keys: Option<&str>, target_keys: Option<&str>) -> String {
    let tpl = template_keys
        .map(|k| format!("    timeout_recovery_keys: {k}\n"))
        .unwrap_or_default();
    let target = target_keys
        .map(|k| format!(", timeout_recovery_keys: {k}"))
        .unwrap_or_default();
    format!(
        "profiles:\n  t:\n    platform: shell\n    prompt_regex: '(?m)^root@t:.*# '\n    timeout_s: 2.0\n{tpl}\
         targets:\n  - {{com: COM0, profile: t, act_no: 1, alias: t+1, device_by_id: /s/by-id/usb-A{target}}}\n"
    )
}

fn is_done(h: &ActorRig, id: &str) -> bool {
    !matches!(
        h.command_get(id).get("done_at"),
        None | Some(PyValue::Null)
    )
}

fn advance_until(h: &ActorRig, what: &str, mut cond: impl FnMut(&ActorRig) -> bool) {
    let deadline = std::time::Instant::now() + Duration::from_secs(10);
    while !cond(h) {
        assert!(std::time::Instant::now() < deadline, "等待逾時：{what}");
        h.advance(Duration::from_millis(100));
        std::thread::sleep(Duration::from_millis(2));
    }
}

fn wal_tx_has(h: &ActorRig, payload: &[u8]) -> bool {
    h.wal_tx_payloads().iter().any(|p| p.as_slice() == payload)
}

/// 救援鍵在 WAL TX 的出現順序（只取 `\x03`／`\x04` 單 byte 列）。
fn recovery_keys_sent(h: &ActorRig) -> Vec<u8> {
    h.wal_tx_payloads()
        .into_iter()
        .filter(|p| p.as_slice() == b"\x03" || p.as_slice() == b"\x04")
        .map(|p| p[0])
        .collect()
}

fn state_of(h: &ActorRig) -> String {
    h.get_state()["state"].as_str().unwrap_or("").to_string()
}

/// 命令逾時並等救援收尾，回傳 command 紀錄。
fn timed_out_command(h: &ActorRig) -> PyDict {
    let id = h.submit_with("cat", |r| r.timeout_s = 1.0);
    h.wait_for("命令 TX 寫出", |h| wal_tx_has(h, b"cat\n"));
    advance_until(h, "救援收尾", |h| is_done(h, &id));
    h.command_get(&id)
}

#[test]
fn default_keys_still_send_ctrl_d_after_ctrl_c_fails() {
    let h = ActorRig::new(&shell_profile(None, None)).ready();
    h.dev.on_tx_reply(b"\x04", b"\r\nroot@t:/# ");
    let rec = timed_out_command(&h);
    assert_eq!(rec["recovery_action"], PyValue::from("CTRL_D"));
    assert_eq!(recovery_keys_sent(&h), vec![0x03, 0x04]);
    assert_eq!(state_of(&h), "READY");
}

#[test]
fn ctrl_c_only_never_sends_ctrl_d() {
    // 裝置只對 CTRL_D 回 prompt：預設鍵序會被 CTRL_D 救回，`[CTRL_C]` 則必須照樣逾時、不送 CTRL_D。
    let h = ActorRig::new(&shell_profile(Some("[CTRL_C]"), None)).ready();
    h.dev.on_tx_reply(b"\x04", b"\r\nroot@t:/# ");
    let rec = timed_out_command(&h);
    assert_eq!(rec["error_code"], PyValue::from("PROMPT_TIMEOUT"));
    assert_eq!(rec["recovery_action"], PyValue::from("NONE"));
    assert_eq!(recovery_keys_sent(&h), vec![0x03]);
    assert!(!h.dev.take_tx().contains(&0x04), "UART 不得出現 CTRL_D");
    let s = h.get_state();
    assert_eq!(s["state"], PyValue::from("ATTACHED"));
    assert_eq!(s["last_error"], PyValue::from("PROMPT_TIMEOUT"));
}

#[test]
fn ctrl_c_only_still_recovers_with_ctrl_c() {
    let h = ActorRig::new(&shell_profile(Some("[CTRL_C]"), None)).ready();
    h.dev.on_tx_reply(b"\x03", b"^C\r\nroot@t:/# ");
    let rec = timed_out_command(&h);
    assert_eq!(rec["status"], PyValue::from("done"));
    assert_eq!(rec["recovery_action"], PyValue::from("CTRL_C"));
    assert_eq!(rec["partial"], PyValue::Bool(true));
    assert_eq!(recovery_keys_sent(&h), vec![0x03]);
    assert_eq!(state_of(&h), "READY");
}

#[test]
fn empty_list_sends_no_keys() {
    let h = ActorRig::new(&shell_profile(Some("[]"), None)).ready();
    h.dev.on_tx_reply(b"\x03", b"^C\r\nroot@t:/# ");
    h.dev.on_tx_reply(b"\x04", b"\r\nroot@t:/# ");
    let _ = h.dev.take_tx();
    let rec = timed_out_command(&h);
    assert_eq!(rec["error_code"], PyValue::from("PROMPT_TIMEOUT"));
    assert_eq!(rec["recovery_action"], PyValue::from("NONE"));
    assert!(recovery_keys_sent(&h).is_empty(), "WAL 不得有救援鍵");
    let uart = h.dev.take_tx();
    assert!(
        !uart.contains(&0x03) && !uart.contains(&0x04),
        "UART 只有命令本身：{uart:?}"
    );
    assert_eq!(state_of(&h), "ATTACHED");
}

#[test]
fn list_order_is_send_order() {
    let h = ActorRig::new(&shell_profile(Some("[CTRL_D, CTRL_C]"), None)).ready();
    h.dev.on_tx_reply(b"\x03", b"^C\r\nroot@t:/# ");
    let rec = timed_out_command(&h);
    assert_eq!(rec["recovery_action"], PyValue::from("CTRL_C"));
    assert_eq!(recovery_keys_sent(&h), vec![0x04, 0x03]);
    assert_eq!(state_of(&h), "READY");
}

#[test]
fn target_override_replaces_template_keys() {
    // template `[]`，target 覆寫回 `[CTRL_C]`：以 target 為準。
    let h = ActorRig::new(&shell_profile(Some("[]"), Some("[CTRL_C]"))).ready();
    h.dev.on_tx_reply(b"\x03", b"^C\r\nroot@t:/# ");
    let rec = timed_out_command(&h);
    assert_eq!(rec["recovery_action"], PyValue::from("CTRL_C"));
    assert_eq!(recovery_keys_sent(&h), vec![0x03]);
}

#[test]
fn ready_recover_ctrl_c_only_never_sends_ctrl_d() {
    // READY 時 `session recover`（source system:recover）走同一組救援鍵。
    let h = ActorRig::new(&shell_profile(Some("[CTRL_C]"), None)).ready();
    h.dev.on_tx_reply(b"\x03", b"^C\r\nroot@t:/# ");
    let r = h.request(SessionRequest::Recover {
        timeout_s: 2.0,
        force: false,
    });
    assert_eq!(r["error_code"], PyValue::from("PROMPT_TIMEOUT_RECOVERED"));
    assert_eq!(r["recovery_action"], PyValue::from("CTRL_C"));
    assert_eq!(recovery_keys_sent(&h), vec![0x03]);
    assert_eq!(state_of(&h), "READY");
}

#[test]
fn ready_recover_empty_list_goes_straight_to_force() {
    // `[]`：不送任何救援鍵，直接 ATTACHED → force clear＋reattach（Python auto-fallback）。
    let h = ActorRig::new(&shell_profile(Some("[]"), None)).ready();
    // `on_tx_reply` 是一次性且註冊時就比對累積 TX：先清掉初始 login 的 `\n`，重新 attach 的 login probe 才拿得到 prompt。
    let _ = h.dev.take_tx();
    h.dev.on_tx_reply(b"\n", b"\r\nroot@t:/# ");
    let rx = h.request_async(SessionRequest::Recover {
        timeout_s: 1.0,
        force: false,
    });
    let got: RefCell<Option<Result<PyDict, PyErr>>> = RefCell::new(None);
    advance_until(&h, "force 收尾", |_| {
        if let Ok(v) = rx.try_recv() {
            *got.borrow_mut() = Some(v);
        }
        got.borrow().is_some()
    });
    let r = got.into_inner().unwrap().unwrap();
    assert_eq!(
        r["error_code"],
        PyValue::from("PROMPT_TIMEOUT_FORCE_RECOVERED")
    );
    assert_eq!(r["recovery_action"], PyValue::from("FORCE_CLEAR_REATTACH"));
    assert!(recovery_keys_sent(&h).is_empty(), "WAL 不得有救援鍵");
    let uart = h.dev.take_tx();
    assert!(
        !uart.contains(&0x03) && !uart.contains(&0x04),
        "UART 不得有救援鍵：{uart:?}"
    );
    assert_eq!(state_of(&h), "READY");
}

#[test]
fn shipped_prpl_template_sends_ctrl_c_only() {
    // 出貨 `sw_core/assets/profiles/default.yaml`（Python 與 Rust 共用）：prpl-template 只送 CTRL_C，其餘維持預設。
    use sw_proto::config_types::{DEFAULT_TIMEOUT_RECOVERY_KEYS, TimeoutRecoveryKey};
    let templates = common::p7::default_templates();
    let mut seen_prpl = false;
    for t in &templates {
        if t.profile_name == "prpl-template" {
            seen_prpl = true;
            assert_eq!(t.timeout_recovery_keys, vec![TimeoutRecoveryKey::CtrlC]);
        } else {
            assert_eq!(
                t.timeout_recovery_keys,
                DEFAULT_TIMEOUT_RECOVERY_KEYS.to_vec(),
                "{}",
                t.profile_name
            );
        }
    }
    assert!(seen_prpl, "出貨 profile 缺 prpl-template");
}
