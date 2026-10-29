//! P7 Task 6 接線（slice 出口「接線＋端到端驗收」發現）：`transport_stall::refine_probe_failure` 原本沒有正式呼叫端，
//! probe 失敗永遠不會翻轉為 `TRANSPORT_STALL`、也不寫 WAL META `transport_stall_suspected`
//! （Python `_refine_probe_failure`，`session_manager.py:1965`；呼叫點 `_probe_existing_bridge` `:4621`、
//! `_attach_by_id` `:4899`／`:4927`、dynamic attach `:5594`／`:5610`）。
//!
//! 本檔以 actor 層（`CoreHarness`）重現：曾有 RX、之後 RX 凍結 ≥ 30 s、probe 全程零 RX → 失敗碼翻轉為
//! `TRANSPORT_STALL`、`last_error_detail` 以 `零 RX ` 開頭、一次性 Warn＋WAL META；RX 恢復重臂後再 stall 再告警一次。
#![cfg(feature = "test-fakes")]

mod common;
mod support;

use std::time::Duration;

use support::*;
use sw_core::login_fsm::LoginOutcome;
use sw_core::mailbox::ControlEvent;
use sw_core::session_core::{Effect, SessionRequest, SessionState};
use sw_pal::traits::Clock;
use sw_proto::pyjson::PyValue;

fn start_login(effs: &[Effect]) -> Option<(u64, u64)> {
    effs.iter().find_map(|e| match e {
        Effect::StartLogin { epoch, fsm_gen, .. } => Some((*epoch, *fsm_gen)),
        _ => None,
    })
}

fn stall_metas(effs: &[Effect]) -> usize {
    effs.iter()
        .filter(|e| {
            matches!(e, Effect::WalMeta { meta, .. }
                if meta.get("event") == Some(&PyValue::from("transport_stall_suspected")))
        })
        .count()
}

fn s<'a>(d: &'a sw_proto::pyjson::PyDict, k: &str) -> Option<&'a str> {
    d.get(k).and_then(PyValue::as_str)
}

fn rx_now(h: &mut CoreHarness, bytes: &[u8], seq: u64) {
    let now = h.clock.mono_ns();
    let epoch = h.epoch();
    h.rx.append(bytes, now, epoch, seq);
    h.rx(bytes);
}

fn finish(h: &mut CoreHarness, (epoch, gen): (u64, u64)) -> Vec<Effect> {
    h.feed(ControlEvent::LoginFinished {
        epoch,
        fsm_gen: gen,
        result: LoginOutcome::NotReady("LOGIN_PROMPT_TIMEOUT".into()),
        after_rx_seq: None,
    })
}

/// 手動 recover 起既有 bridge 再探（RX 剛到、自動 reprobe 尚未合格）；probe 期間 35 s 全程零 RX。
fn stalled_manual_reprobe(h: &mut CoreHarness, seq: u64) -> Vec<Effect> {
    rx_now(h, b"root@t:~# ", seq);
    let (_reply, fx) = h.rpc_deferred(SessionRequest::Recover {
        force: false,
        timeout_s: 10.0,
    });
    let run = start_login(&fx).expect("ATTACHED 的 recover 起再探 run");
    h.advance(Duration::from_secs(35));
    finish(h, run)
}

#[test]
fn reprobe_zero_rx_after_30s_flips_to_transport_stall_once() {
    let mut h = CoreHarness::new(SHELL_PROFILE);
    h.to_attached(Some("SHELL_PROMPT_TIMEOUT"));
    let fx = stalled_manual_reprobe(&mut h, 1);
    assert_eq!(h.core.state(), SessionState::Attached);
    let p = h.public();
    assert_eq!(
        s(&p, "last_error"),
        Some("TRANSPORT_STALL"),
        "probe 全程零 RX 且 RX 凍結 ≥ 30 s 應翻轉"
    );
    assert!(
        s(&p, "last_error_detail").is_some_and(|d| d.starts_with("零 RX ")),
        "detail 應為 transport stall 提示：{:?}",
        p.get("last_error_detail")
    );
    assert_eq!(
        stall_metas(&fx),
        1,
        "首次 stall 應寫一筆 WAL META transport_stall_suspected"
    );
    assert!(
        fx.iter()
            .any(|e| matches!(e, Effect::Warn(line) if line.contains("疑似 transport stall"))),
        "應有一次 stderr WARNING"
    );
}

#[test]
fn auto_reprobe_zero_rx_flips_to_transport_stall() {
    let mut h = CoreHarness::new(SHELL_PROFILE);
    h.to_attached(Some("SHELL_PROMPT_TIMEOUT"));
    rx_now(&mut h, b"root@t:~# ", 1);
    let mut run = None;
    for _ in 0..20 {
        let fx = h.advance(Duration::from_secs(1));
        if let Some(r) = start_login(&fx) {
            run = Some(r);
            break;
        }
    }
    let run = run.expect("ATTACHED＋prompt 錯誤：RX 靜默後自動 reprobe 起 run");
    h.advance(Duration::from_secs(35));
    let fx = finish(&mut h, run);
    assert_eq!(s(&h.public(), "last_error"), Some("TRANSPORT_STALL"));
    assert_eq!(stall_metas(&fx), 1);
}

#[test]
fn repeated_stall_warns_once_until_rx_resumes() {
    let mut h = CoreHarness::new(SHELL_PROFILE);
    h.to_attached(Some("SHELL_PROMPT_TIMEOUT"));
    let first = stalled_manual_reprobe(&mut h, 1);
    assert_eq!(stall_metas(&first), 1);
    // 持續 stall：再探仍翻轉、但不重複告警（`transport_stall_warned` 去重）。
    let (_r, fx) = h.rpc_deferred(SessionRequest::Recover {
        force: false,
        timeout_s: 10.0,
    });
    let run = start_login(&fx).expect("再探 run");
    h.advance(Duration::from_secs(1));
    let again = finish(&mut h, run);
    assert_eq!(s(&h.public(), "last_error"), Some("TRANSPORT_STALL"));
    assert_eq!(stall_metas(&again), 0, "未見 RX 恢復前不得重複告警");
    // RX 恢復即重臂（Python `_mark_session_rx`）：再 stall 再告警一次。
    let third = stalled_manual_reprobe(&mut h, 2);
    assert_eq!(s(&h.public(), "last_error"), Some("TRANSPORT_STALL"));
    assert_eq!(stall_metas(&third), 1, "RX 恢復後再 stall 應再告警一次");
}

#[test]
fn probe_with_rx_delta_is_not_stall() {
    let mut h = CoreHarness::new(SHELL_PROFILE);
    h.to_attached(Some("SHELL_PROMPT_TIMEOUT"));
    rx_now(&mut h, b"x", 1);
    let (_reply, fx) = h.rpc_deferred(SessionRequest::Recover {
        force: false,
        timeout_s: 10.0,
    });
    let run = start_login(&fx).expect("再探 run");
    h.advance(Duration::from_secs(35));
    // probe 期間有 RX（例如 TX echo）但未經 actor 記為 session RX：計數差非零 → 不翻轉，維持原失敗碼。
    let now = h.clock.mono_ns();
    let epoch = h.epoch();
    h.rx.append(b"\r\n=> ", now, epoch, 2);
    let fx = finish(&mut h, run);
    assert_eq!(s(&h.public(), "last_error"), Some("LOGIN_PROMPT_TIMEOUT"));
    assert_eq!(stall_metas(&fx), 0);
}
