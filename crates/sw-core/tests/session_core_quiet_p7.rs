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

/// 審查 c11aa89（Opus C-1）：console unlock 在 quiet 進行中起既有 bridge 再探時，fence 仍須保留 quiet：
/// Python 只把 `_gate_probe_quiet_until` 設 0，`_gate_self_test_quiet_until` 不變（`uart_io.py:635-649`）——
/// 救援鍵／self-test probe 在 quiet 期間仍回 quiet，在途 unlock login probe 放行。
#[test]
fn console_unlock_reprobe_during_quiet_keeps_recover_key_blocked() {
    let mut h = CoreHarness::new(UNLOCK_PROFILE);
    h.set_env("SW_T_UNLOCK", "k");
    let effs = attach(&mut h);
    let (epoch, gen, _) = start_login(&effs).expect("unlock login run");
    h.feed(ControlEvent::LoginFinished {
        epoch,
        fsm_gen: gen,
        result: LoginOutcome::NotReady("CONSOLE_UNLOCK_PROMPT_TIMEOUT".into()),
        after_rx_seq: None,
    });
    assert_eq!(h.core.state(), SessionState::Attached);
    h.rx(BANNER);
    let (_rx, effs) = h.rpc_deferred(sw_core::session_core::SessionRequest::SessionAttach);
    let (epoch, gen, _) = start_login(&effs).expect("console unlock 不被 quiet 擋：再探起 run");
    assert_ne!(h.public()["boot_quiet_remaining_s"], PyValue::Null);
    h.fence.with(|f| {
        assert!(f.quiet_active, "quiet 仍在進行中，fence 不得清除");
        let mut p = login_permit(epoch, gen);
        assert_eq!(Fence::admit(f, &p), Ok(()), "unlock login probe 放行");
        p.class = TxClass::SystemRecoverKey;
        assert_eq!(Fence::admit(f, &p), Err(FenceReject::Quiet));
        p.class = TxClass::SelfTestProbe;
        assert_eq!(Fence::admit(f, &p), Err(FenceReject::Quiet));
    });
}

/// DETACHED（拔插）並重新列舉：Python detach 不清 `boot_quiet_until`／`ready_reconfirm_pending`（只有 passthrough 重掛時
/// `reset_passthrough_boot_gate` 清，`session_manager.py:644-654`、`:4856-4858`），故 180 s 內重插的 attach 不送 probe、
/// 以 PROMPT_UNAVAILABLE 停 ATTACHED（#130：DUT reboot 期間 re-enumerate 觸發的 re-attach）。
fn detach_and_reattach(h: &mut CoreHarness) -> Vec<Effect> {
    h.feed(ControlEvent::DeviceDetached {
        by_id: DEVICE_BY_ID.into(),
        reason: sw_core::mailbox::DetachReason::Removed,
    });
    let (epoch, stop) = h.pending_stop();
    h.receipt(epoch, stop).feed();
    assert_eq!(h.public()["state"], PyValue::from("DETACHED"));
    attach(h)
}

#[test]
fn detach_keeps_quiet_and_reattach_within_window_is_zero_tx() {
    let mut h = CoreHarness::new(PRPL_PROFILE);
    h.to_ready();
    h.rx(BANNER);
    let effs = detach_and_reattach(&mut h);
    assert_eq!(
        start_login(&effs),
        None,
        "quiet 仍在：重插的 attach 不送 probe"
    );
    let p = h.public();
    assert_eq!(p["state"], PyValue::from("ATTACHED"));
    assert_eq!(p["last_error"], PyValue::from("PROMPT_UNAVAILABLE"));
    assert_eq!(p["ready_reconfirm_pending"], PyValue::Bool(true));
    assert!(matches!(p["boot_quiet_remaining_s"], PyValue::Float(_)));
    h.fence
        .with(|f| assert!(f.quiet_active, "新 bridge 的 fence 同步現行 quiet"));
}

#[test] // parity #17（B:B-36）
fn passthrough_reattach_clears_previous_profile_quiet_and_reconfirm() {
    let mut h = CoreHarness::new(PRPL_PROFILE);
    h.to_ready();
    h.rx(BANNER);
    h.feed(ControlEvent::DeviceDetached {
        by_id: DEVICE_BY_ID.into(),
        reason: sw_core::mailbox::DetachReason::Removed,
    });
    let (epoch, stop) = h.pending_stop();
    h.receipt(epoch, stop).feed();
    // profile 改為 passthrough（P5-T16 SetProfile：`_rematerialize_profile_locked`）後重掛。
    let pt = profile_of(PASSTHROUGH_PROFILE);
    let r = h
        .rpc(sw_core::session_core::SessionRequest::SetProfile {
            profile: pt,
            profile_source: "yaml-target".into(),
        })
        .expect("SetProfile");
    assert_eq!(r["ok"], PyValue::Bool(true));
    attach(&mut h);
    let p = h.public();
    assert_eq!(p["state"], PyValue::from("ATTACHED"));
    assert_eq!(p["boot_quiet_remaining_s"], PyValue::Null);
    assert_eq!(p["ready_reconfirm_pending"], PyValue::Bool(false));
    assert_eq!(p["ready_reconfirm_failed"], PyValue::Bool(false));
    assert_eq!(p["ready_reconfirm_remaining_s"], PyValue::Null);
    h.fence.with(|f| {
        assert!(!f.quiet_active);
        assert!(!f.reconfirm_pending);
    });
}

#[test]
fn confirm_ready_clears_all() {
    // READY＋pending → 自動確認 probe（當前 (epoch, fsm_gen)）回 Ready → until=0、pending=false、failed=false、attempts=0。
    let mut h = CoreHarness::new(PRPL_PROFILE);
    h.to_ready();
    h.rx(BANNER);
    h.rx(b"root@prplOS:~# ");
    let mut fx = Vec::new();
    for _ in 0..4 {
        fx.extend(h.advance(std::time::Duration::from_secs(1)));
    }
    let (epoch, gen, _) = start_login(&fx).expect("確認 probe");
    h.feed(ControlEvent::LoginFinished {
        epoch,
        fsm_gen: gen,
        result: LoginOutcome::Ready,
        after_rx_seq: None,
    });
    let p = h.public();
    assert_eq!(p["boot_quiet_remaining_s"], PyValue::Null);
    assert_eq!(p["ready_reconfirm_pending"], PyValue::Bool(false));
    assert_eq!(p["ready_reconfirm_failed"], PyValue::Bool(false));
    assert_eq!(p["ready_reconfirm_remaining_s"], PyValue::Null);
    assert_eq!(p["reprobe_attempts"], PyValue::from(0));
    h.fence
        .with(|f| assert!(!f.quiet_active && !f.reconfirm_pending));
}

#[test]
fn bootloader_tail_blocks_clear() {
    // brcm 類寬鬆 prompt（`[>#]\s*$`）會誤配 `CFE> `；尾行命中 bootloader_prompts 時不解除 quiet（#130 Finding 3）。
    const BRCM: &str = "profiles:\n  b:\n    platform: bcm\n    prompt_regex: '(?m)[>#]\\s*$'\n    bootloader_prompts: ['^CFE> $']\n    timeout_s: 2.0\ntargets:\n  - {com: COM0, profile: b, act_no: 1, alias: b+1, device_by_id: /s/by-id/usb-A}\n";
    let mut h = CoreHarness::new(BRCM);
    h.to_ready();
    h.rx(b"\r\nCFE version 1.0\r\n");
    h.rx(b"\r\nCFE> ");
    assert_eq!(
        h.public()["boot_quiet_remaining_s"],
        PyValue::Float(180.0),
        "bootloader 尾行不解除 quiet"
    );
    h.rx(b"\r\n# ");
    assert_eq!(
        h.public()["boot_quiet_remaining_s"],
        PyValue::Null,
        "shell prompt 解除"
    );
}

#[test]
fn quiet_rx_runs_during_foreground_busy() {
    let mut h = CoreHarness::new(PRPL_PROFILE);
    h.to_ready();
    let _cmd = h.start_foreground_command("sleep 30");
    h.rx(BANNER);
    assert_eq!(h.public()["boot_quiet_remaining_s"], PyValue::Float(180.0));
    h.fence.with(|f| assert!(f.quiet_active));
}

#[test]
fn rpc_human_source_is_treated_as_agent() {
    // DF-02：RPC 帶入的 `human:` source 一律視同 agent（submit 層 gate）。
    let mut h = CoreHarness::new(PRPL_PROFILE);
    h.to_ready();
    h.rx(BANNER);
    let mut req = agent_submit("ls");
    req.source = "human:x".into();
    let r = h
        .rpc(sw_core::session_core::SessionRequest::Submit(req))
        .expect("rpc");
    assert_eq!(r["error_code"], PyValue::from("AUTOBOOT_QUIET"));
    assert_eq!(r["retry_after_s"], PyValue::Float(180.0));
}

const OTHERS_PT: &str = "profiles:\n  o:\n    platform: passthrough\n    prompt_regex: '.*'\n    login_regex: '$^'\n    password_regex: '$^'\n    ready_probe: ''\ntargets:\n  - {com: COM0, profile: o, act_no: 1, alias: o+1, device_by_id: /s/by-id/usb-A}\n";
const UBOOT_PT: &str = "profiles:\n  u:\n    platform: passthrough\n    prompt_regex: '(?mi)^(=>|u-boot>|cfe>) $'\n    login_regex: '$^'\n    ready_probe: 'echo __READY__${nonce}'\ntargets:\n  - {com: COM0, profile: u, act_no: 1, alias: u+1, device_by_id: /s/by-id/usb-A}\n";

#[test] // SC-35：uboot-template／others-template × ATTACHED／READY
fn passthrough_banner_never_arms_quiet_both_templates_both_states() {
    for yaml in [OTHERS_PT, UBOOT_PT] {
        for ready in [false, true] {
            let mut h = CoreHarness::new(yaml);
            attach(&mut h);
            if ready {
                h.advance(std::time::Duration::from_secs(31));
                assert_eq!(h.core.state(), SessionState::Ready, "30 s 安靜 → READY");
            }
            let gen = h.core.generations().fsm_gen;
            h.rx(b"U-Boot 2024.04\r\nHit any key to stop autoboot:  3");
            assert_eq!(h.core.generations().fsm_gen, gen);
            let p = h.public();
            assert_eq!(p["boot_quiet_remaining_s"], PyValue::Null);
            assert_eq!(p["ready_reconfirm_pending"], PyValue::Bool(false));
            h.fence.with(|f| {
                assert!(!f.quiet_active && !f.probe_quiet_exempt && !f.reconfirm_pending)
            });
            if ready {
                // passthrough_ready_never_gated：banner 後立即 Submit → 進 Arbiter（回 cmd_id），不回 gate 碼。
                let r = h
                    .rpc(sw_core::session_core::SessionRequest::Submit(agent_submit(
                        "ls",
                    )))
                    .expect("rpc");
                assert!(r.get("cmd_id").is_some(), "{r:?}");
                assert_eq!(h.public()["ready_reconfirm_failed"], PyValue::Bool(false));
                assert_eq!(h.public()["ready_reconfirm_remaining_s"], PyValue::Null);
            }
        }
    }
}

#[test] // SC-35
fn passthrough_banner_restarts_quiet_window() {
    let mut h = CoreHarness::new(OTHERS_PT);
    attach(&mut h);
    h.advance(std::time::Duration::from_secs(29));
    h.rx(b"U-Boot 2024.04\r\n");
    h.advance(std::time::Duration::from_secs(29));
    assert_eq!(
        h.core.state(),
        SessionState::Attached,
        "banner chunk 重算安靜視窗"
    );
    h.advance(std::time::Duration::from_secs(1));
    assert_eq!(h.core.state(), SessionState::Ready);
}

#[test]
fn stale_login_finished_keeps_pending() {
    // §5.3 過期事件：READY＋pending 時，舊世代 LoginFinished{Ready} 不清 pending、state 不變。
    let mut h = CoreHarness::new(PRPL_PROFILE);
    h.to_ready();
    h.rx(BANNER);
    h.rx(b"root@prplOS:~# ");
    let (epoch, gen) = (h.epoch(), h.core.generations().fsm_gen);
    h.feed(ControlEvent::LoginFinished {
        epoch,
        fsm_gen: gen.wrapping_sub(1),
        result: LoginOutcome::Ready,
        after_rx_seq: None,
    });
    assert_eq!(h.core.state(), SessionState::Ready);
    assert_eq!(h.public()["ready_reconfirm_pending"], PyValue::Bool(true));
}

#[test]
fn reattach_within_quiet_fence_expires_with_window() {
    // 審查 92e0223_76de0cf bea2d8e-1：重掛後 fence 同步 quiet，須為新 epoch 排到期計時器；Python `_push_probe_gate`
    // 以 `time.monotonic() < self_test_quiet_until` 判定，到期即放行 self-test／救援鍵。
    let mut h = CoreHarness::new(PRPL_PROFILE);
    h.to_ready();
    h.rx(BANNER);
    detach_and_reattach(&mut h);
    h.fence.with(|f| assert!(f.quiet_active));
    // 持續非 banner／非 prompt RX（自動重探被 RX idle 擋住），推進過 quiet 視窗。
    for _ in 0..200 {
        h.rx(b"booting...\r\n");
        h.advance(std::time::Duration::from_secs(1));
    }
    assert_eq!(h.public()["boot_quiet_remaining_s"], PyValue::Null);
    h.fence.with(|f| {
        assert!(
            !f.quiet_active,
            "quiet 到期後 fence 解除（self-test／救援鍵放行）"
        )
    });
}

/// #234：profile 宣告 `boot_done_regex`（YAML 經 sw-proto `load_profiles` 承接）。
const PRPL_BOOT_DONE_PROFILE: &str = "profiles:\n  r:\n    platform: prpl\n    prompt_regex: '(?m)^root@prplOS:.*# '\n    boot_done_regex: '  Please press Enter to activate this console  '\n    timeout_s: 2.0\ntargets:\n  - {com: COM0, profile: r, act_no: 1, alias: r+1, device_by_id: /s/by-id/usb-A}\n";

#[test]
fn boot_done_regex_clears_quiet_but_keeps_reconfirm_pending() {
    // Python `_update_boot_quiet_locked`（#234）：quiet 期間 RX 命中 boot_done_regex 即 `clear_boot_quiet()`；
    // `ready_reconfirm_pending` 不清（仍由 nonce probe 確認），agent 顯式命令仍回 AUTOBOOT_QUIET。
    // config.py：`str(raw.get("boot_done_regex") or "").strip()`——前後空白去除後才比對。
    let mut h = CoreHarness::new(PRPL_BOOT_DONE_PROFILE);
    h.to_ready();
    h.rx(BANNER);
    assert_eq!(h.public()["boot_quiet_remaining_s"], PyValue::Float(180.0));
    let gen = h.core.generations().fsm_gen;
    h.rx(b"\r\nPlease press Enter to activate this console.\r\n");
    let p = h.public();
    assert_eq!(p["boot_quiet_remaining_s"], PyValue::Null, "boot_done_regex 命中即解除 quiet");
    assert_eq!(p["ready_reconfirm_pending"], PyValue::Bool(true), "不清 ready_reconfirm_pending");
    assert_eq!(h.core.generations().fsm_gen, gen);
    h.fence.with(|f| {
        assert!(!f.quiet_active);
        assert!(f.reconfirm_pending);
    });
    let r = h.rpc(sw_core::session_core::SessionRequest::Submit(agent_submit("ls")));
    assert_eq!(r.expect("submit")["error_code"], PyValue::from("AUTOBOOT_QUIET"));
}

#[test]
fn boot_done_regex_absent_keeps_quiet_on_askconsole() {
    // 對照：未設 boot_done_regex 時 askconsole 不匹配 login／prompt，quiet 只能等 180 s 過期（#162 根因）。
    let mut h = CoreHarness::new(PRPL_PROFILE);
    h.to_ready();
    h.rx(BANNER);
    h.rx(b"\r\nPlease press Enter to activate this console.\r\n");
    assert_eq!(h.public()["boot_quiet_remaining_s"], PyValue::Float(180.0));
}
