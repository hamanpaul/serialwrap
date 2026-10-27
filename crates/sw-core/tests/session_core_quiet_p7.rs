//! P7 Task 4（08-slice23-login-events.md）：SessionCore 的 quiet arm／clear 與 fence 同步、console unlock 例外、SC-35。
//!
//! 偏離計畫原文：`banner_arm_bumps_fsm_gen_once_and_invalidates_human_input`／`rearm_uses_quiet_rearm_reason` 的
//! `input_epoch`／`Log reason=boot_banner` 斷言依賴 07 T6 `invalidate_human_input`（P6 尚為骨架），列 BLOCKED-UPSTREAM，
//! 本檔只斷言 fsm_gen／fence 部分（`banner_arm_bumps_fsm_gen_once`）。計畫的 `auto_probe_blocked` 以
//! `FenceState.probe_quiet_exempt` 表示（`auto_probe_blocked == quiet_active && !probe_quiet_exempt`），保留 06 T9
//! 「probe 類＋quiet_active → Quiet」的既有測試語意。
#![cfg(feature = "test-fakes")]
mod support;

use support::*;
use sw_core::login_fsm::{LoginOutcome, LoginPlan};
use sw_core::mailbox::ControlEvent;
use sw_core::session_core::{Effect, SessionState};
use sw_core::tx_writer::{Fence, FenceReject, OpId, TxClass, TxPermit};
use sw_pal::traits::Clock;
use sw_proto::pyjson::PyValue;

const PRPL_PROFILE: &str = "profiles:\n  r:\n    platform: prpl\n    prompt_regex: '(?m)^root@prplOS:.*# '\n    timeout_s: 2.0\ntargets:\n  - {com: COM0, profile: r, act_no: 1, alias: r+1, device_by_id: /s/by-id/usb-A}\n";
const UNLOCK_PROFILE: &str = "profiles:\n  c:\n    platform: shell\n    prompt_regex: '(?m)^root@t:.*# '\n    console_unlock_env: SW_T_UNLOCK\n    timeout_s: 2.0\ntargets:\n  - {com: COM0, profile: c, act_no: 1, alias: c+1, device_by_id: /s/by-id/usb-A}\n";
const BANNER: &[u8] = b"\r\nU-Boot 2024.04 (Jan 01)\r\n";

fn attach(h: &mut CoreHarness) -> Vec<Effect> {
    h.feed(ControlEvent::DeviceAttached {
        by_id: DEVICE_BY_ID.into(),
        real_path: REAL_PATH.into(),
    });
    h.bridge_ok()
}

fn start_login(effs: &[Effect]) -> Option<(u64, u64, &'static str)> {
    effs.iter().find_map(|e| match e {
        Effect::StartLogin {
            epoch,
            fsm_gen,
            plan,
        } => Some((
            *epoch,
            *fsm_gen,
            match plan {
                LoginPlan::Probe => "Probe",
                LoginPlan::EnsureReady { .. } => "EnsureReady",
                LoginPlan::None => "None",
                LoginPlan::CredentialsUnresolved { .. } => "CredentialsUnresolved",
            },
        )),
        _ => None,
    })
}

fn login_permit(epoch: u64, fsm_gen: u64) -> TxPermit {
    TxPermit {
        op_id: OpId(9_000),
        epoch,
        owner_gen: 0,
        fsm_gen,
        class: TxClass::LoginProbe,
        source: "system".into(),
        cmd_id: None,
        log: true,
        input_epoch: None,
        replay_hold: None,
    }
}

#[test]
fn banner_arm_bumps_fsm_gen_once() {
    let mut h = CoreHarness::new(PRPL_PROFILE);
    h.to_ready();
    let before = h.core.generations().fsm_gen;
    // 同一 chunk 含兩個 banner 特徵：只 arm 一次。
    h.rx(b"\r\nU-Boot 2024.04\r\nHit any key to stop autoboot:  3");
    assert_eq!(h.core.generations().fsm_gen, before + 1);
    let core_gen = h.core.generations().fsm_gen;
    h.fence.with(|f| {
        assert!(f.quiet_active);
        assert!(
            !f.probe_quiet_exempt,
            "會擋 probe 的 arm：auto_probe_blocked=true"
        );
        assert_eq!(
            f.fsm_gen, core_gen,
            "會擋 probe 的 arm 推進 fence 世代（floor）"
        );
    });
}

#[test]
fn console_unlock_arm_keeps_auto_probe_open() {
    // Python `_boot_quiet_blocks_probe` 對 console unlock 為假、`_update_boot_quiet_locked` 不遞增 login_gen：
    // 在途 unlock run 的 TX 不被 quiet 撤銷（probe gate quiet_until=0）。
    let mut h = CoreHarness::new(UNLOCK_PROFILE);
    h.set_env("SW_T_UNLOCK", "k");
    let effs = attach(&mut h);
    let (epoch, run_gen, plan) = start_login(&effs).expect("unlock profile 應起 login run");
    assert_eq!(plan, "EnsureReady");
    h.rx(BANNER);
    assert!(
        h.core.generations().fsm_gen > run_gen,
        "arm 仍遞增 core fsm_gen（隔離 quiet timer）"
    );
    h.fence.with(|f| {
        assert!(f.quiet_active, "quiet_active=true");
        assert!(
            f.probe_quiet_exempt,
            "console unlock：auto_probe_blocked=false"
        );
        assert_eq!(
            f.fsm_gen, run_gen,
            "console unlock 的 arm 不推進 fence 世代"
        );
        assert_eq!(
            Fence::admit(f, &login_permit(epoch, run_gen)),
            Ok(()),
            "在途 unlock run 的 TX 放行"
        );
        let mut st = login_permit(epoch, run_gen);
        st.class = TxClass::SelfTestProbe;
        assert_eq!(
            Fence::admit(f, &st),
            Err(FenceReject::Quiet),
            "SelfTestProbe 仍以 quiet_active 擋（R5-F2）"
        );
    });
}

#[test]
fn console_unlock_attach_during_quiet_still_starts_login() {
    // Python `_attach_by_id`：boot_quiet = `_boot_quiet_blocks_probe`，console unlock 為假 → 照常 ensure_ready。
    let mut h = CoreHarness::new(UNLOCK_PROFILE);
    h.set_env("SW_T_UNLOCK", "k");
    let far = h.clock.mono_ns() + 100_000_000_000;
    h.core.set_boot_quiet_until_for_test(far);
    let effs = attach(&mut h);
    assert_eq!(start_login(&effs).map(|s| s.2), Some("EnsureReady"));
    assert_ne!(
        h.public()["last_error"],
        PyValue::from("PROMPT_UNAVAILABLE")
    );
}

#[test]
fn quiet_blocking_attach_is_zero_tx_prompt_unavailable() {
    // 回歸（06 T13）：一般 profile 於 quiet 期間 attach → 不起 login、PROMPT_UNAVAILABLE。
    let mut h = CoreHarness::new(PRPL_PROFILE);
    let far = h.clock.mono_ns() + 100_000_000_000;
    h.core.set_boot_quiet_until_for_test(far);
    let effs = attach(&mut h);
    assert_eq!(start_login(&effs), None);
    assert_eq!(h.public()["state"], PyValue::from("ATTACHED"));
    assert_eq!(
        h.public()["last_error"],
        PyValue::from("PROMPT_UNAVAILABLE")
    );
}

#[test]
fn prompt_clears_quiet_but_not_pending_nor_fsm_gen() {
    let mut h = CoreHarness::new(PRPL_PROFILE);
    h.to_ready();
    h.rx(BANNER);
    let gen = h.core.generations().fsm_gen;
    h.rx(b"root@prplOS:~# ");
    let p = h.public();
    assert_eq!(p["boot_quiet_remaining_s"], PyValue::Null);
    assert_eq!(p["ready_reconfirm_pending"], PyValue::Bool(true));
    assert_eq!(
        h.core.generations().fsm_gen,
        gen,
        "clear 不動 fsm_gen（arch §5.3 (c) 已刪）"
    );
    h.fence.with(|f| {
        assert!(!f.quiet_active);
        assert!(!f.probe_quiet_exempt);
    });
}

#[test]
fn banner_arm_keeps_external_state() {
    let mut h = CoreHarness::new(PRPL_PROFILE);
    h.to_ready();
    h.rx(BANNER);
    let p = h.public();
    assert_eq!(p["state"], PyValue::from("READY"));
    assert_eq!(p["ready_reconfirm_pending"], PyValue::Bool(true));
    assert_eq!(p["boot_quiet_remaining_s"], PyValue::Float(180.0));
}

#[test]
fn passthrough_banner_never_arms_quiet() {
    // SC-35：passthrough 不 arm quiet、不設 READY reconfirm、fence 不變（`_update_boot_quiet_locked` 首行早退）。
    let mut h = CoreHarness::new(PASSTHROUGH_PROFILE);
    attach(&mut h);
    assert_eq!(h.core.state(), SessionState::Attached);
    let gen = h.core.generations().fsm_gen;
    h.rx(b"U-Boot 2024.04\r\nHit any key to stop autoboot:  3");
    assert_eq!(h.core.generations().fsm_gen, gen);
    let p = h.public();
    assert_eq!(p["boot_quiet_remaining_s"], PyValue::Null);
    assert_eq!(p["ready_reconfirm_pending"], PyValue::Bool(false));
    h.fence.with(|f| {
        assert!(!f.quiet_active);
        assert!(!f.probe_quiet_exempt);
    });
}

#[test]
fn stale_login_finished_is_ignored() {
    // §5.3「過期 timer／ownership 事件」：fsm_gen 不符的 LoginFinished{Ready} 不推 READY、不清 pending。
    let mut h = CoreHarness::new(PRPL_PROFILE);
    let effs = attach(&mut h);
    let (epoch, gen, _) = start_login(&effs).expect("attach 起 probe");
    h.feed(ControlEvent::LoginFinished {
        epoch,
        fsm_gen: gen.wrapping_add(7),
        result: LoginOutcome::Ready,
        after_rx_seq: None,
    });
    assert_eq!(h.core.state(), SessionState::Attaching);
}
