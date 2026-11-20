//! P8 Task 11：外部工具裝置交接與 P8 新功能（file transfer、flash）的整合（Rust L1）。
//!
//! 行為基準＝origin/main 的 Python：`session_manager.py::release_device`／`attach_device`、`_flash_busy_locked`、
//! `_DrainJob`／`_mark_drain_stalled`。P5 已交付 release／attach 完整路徑；本檔只驗 P8 新增交互
//! （FileTransfer 占前景槽時 `aborted_cmd:true`、Probe 窗口／FLASHING 擋 release）與計畫 Task 11 表中外部無法
//! 決定性製造、改以 L1 驗證的 drain 族（`release_drain_stalled_then_completes`、`attach_while_drain_in_progress`、
//! `release_dedupe_concurrent`）。外部證據一律取自 FakeDevice 的實際寫出（Review Focus 6）。
#![cfg(feature = "test-fakes")]

mod support;

use std::time::Duration;

use support::fake_shell::ShellCaps;
use support::file_transfer_session::{code, int_of, wait_real, XferRig};
use support::flash_core::bb;
use support::handoff::*;
use support::{ActorRig, SHELL_PROFILE};
use sw_core::session_core::FlashReject;
use sw_pal::fakes::serial::FakeWriteStep;
use sw_pal::traits::Clock;
use sw_proto::pyjson::PyValue;

const RELEASE_OK_KEYS: [&str; 4] = ["ok", "session", "closed_consoles", "aborted_cmd"];
const RECOVERING_KEYS: [&str; 5] = ["ok", "error_code", "drain_stalled", "drain_timeout_epoch", "session"];
const STALLED_KEYS: [&str; 5] = ["ok", "error_code", "drain_stalled", "drain_timeout_epoch", "session"];
const FLASHING_BUSY_KEYS: [&str; 4] = ["ok", "error_code", "selector", "session"];

/// FileTransfer 占前景槽時 release：`aborted_cmd:true`、`closed_consoles`＝進入 release 時的 console 數、
/// console 端點關閉、push 以 `COMMAND_ABORTED_IN_FLIGHT` 結束、之後 DUT 零新增 bytes（計畫 Task 11 表）。
#[test]
fn release_during_push_aborted_cmd_true() {
    let x = XferRig::ready_with_hub(SHELL_PROFILE, ShellCaps::busybox_base64());
    let c1 = x.rig.attach_pty_console("c1");
    let c2 = x.rig.attach_pty_console("c2");
    let consoles = int_of(&x.rig.get_state(), "console_count").expect("console_count");
    assert!(consoles >= 2, "兩個 human console 已掛上：{consoles}");
    let push = push_held_in_chunk_loop(&x);
    x.wait_peer_holding();
    let rel = release(&x.rig, "push");
    assert_eq!(keys(&rel), RELEASE_OK_KEYS, "{rel:?}");
    assert_eq!(rel.get("ok"), Some(&PyValue::Bool(true)), "{rel:?}");
    assert_eq!(rel.get("aborted_cmd"), Some(&PyValue::Bool(true)), "FileTransfer 占前景槽＝前景命令進行中：{rel:?}");
    assert_eq!(int_of(&rel, "closed_consoles"), Some(consoles), "{rel:?}");
    assert_eq!(session_state(&rel).as_deref(), Some("RELEASED"), "{rel:?}");
    wait_real("兩個 console 端點皆已關閉", || c1.closed() && c2.closed());
    let r = recv(&push, "file.push");
    assert_eq!(code(&r).as_deref(), Some("COMMAND_ABORTED_IN_FLIGHT"), "{r:?}");
    let after = x.peer.tx_len();
    std::thread::sleep(Duration::from_millis(150));
    let _ = x.rig.get_state();
    assert_eq!(x.peer.tx_len(), after, "RELEASED 後 DUT 零新增 bytes（不重送 chunk、不送 cleanup）");
    assert_eq!(int_of(&x.rig.get_state(), "console_count"), Some(0));
}

/// `FLASHING_BUSY` 拒絕的共同斷言（Python `release_device` 鍵序 ok／error_code／selector／session；state 不變、bridge 未 detach）。
fn assert_release_flashing_busy(rig: &ActorRig, what: &str) {
    let busy = release(rig, what);
    assert_eq!(keys(&busy), FLASHING_BUSY_KEYS, "{what}：{busy:?}");
    assert_eq!(busy.get("ok"), Some(&PyValue::Bool(false)), "{what}");
    assert_eq!(str_field(&busy, "error_code").as_deref(), Some("FLASHING_BUSY"), "{what}");
    assert_eq!(str_field(&busy, "selector").as_deref(), Some("COM0"), "{what}");
    assert_eq!(session_state(&busy).as_deref(), Some("ATTACHED"), "{what}：probe 窗口不改 state");
    assert_eq!(rig.dev.close_count(), 0, "{what}：probe 窗口中 bridge 未被 detach");
}

/// Probe(1) 窗口中 `device.release` → `FLASHING_BUSY`；probe 結束（未認線）且解除 flash-critical 後 release 成功；
/// 全程 MCU 線零 TX（passthrough explicit target attach 零 TX，release 不送任何 bytes）。
///
/// 窗口涵蓋兩段（Python `_flash_busy_locked`）：manager `collect_flash_candidates_and_mark` 標記 flash-critical 起、
/// 經 `FlashSyncProbeStart`（`FlashMode::Probe`）、`FlashProbeReleased`，到 coordinator `unmark_flash_critical` 為止。
/// 標記／解除以測試替身寫入本 session 的 fence（同 `SessionManager::mark_flash_critical` 的寫法）。
#[test]
fn release_flash_interaction_probe_window() {
    let rig = bb::attached(false);
    rig.fence.with(|f| f.flash_critical = true);
    assert_release_flashing_busy(&rig, "候選已標記、probe 未開始");
    bb::probe_start(&rig, 1).expect("ATTACHED passthrough 應授權 probe");
    assert_release_flashing_busy(&rig, "Probe(1) 窗口");
    let clear = rig.request(sw_core::session_core::SessionRequest::Clear);
    assert_eq!(str_field(&clear, "error_code").as_deref(), Some("FLASHING_BUSY"), "{clear:?}");
    assert_eq!(state(&rig), "ATTACHED");
    bb::released(&rig, 1);
    assert_release_flashing_busy(&rig, "probe 已結束、flash-critical 尚未解除");
    rig.fence.with(|f| f.flash_critical = false);
    let rel = release(&rig, "after probe");
    assert_eq!(keys(&rel), RELEASE_OK_KEYS, "{rel:?}");
    assert_eq!(rel.get("ok"), Some(&PyValue::Bool(true)), "{rel:?}");
    assert_eq!(rel.get("aborted_cmd"), Some(&PyValue::Bool(false)), "{rel:?}");
    assert_eq!(session_state(&rel).as_deref(), Some("RELEASED"));
    // release 後 coordinator 的下一個 probe 被拒（不得在 RELEASED 上開窗）。
    assert!(matches!(bb::probe_start(&rig, 2), Err(FlashReject::Released) | Err(FlashReject::BadState(_))));
    assert_eq!(rig.dev.tx_len(), 0, "外部 oracle：MCU 線全程零 TX");
}

/// FLASHING 中 `device.release` → `FLASHING_BUSY`＋`selector`＋`session(FLASHING)`；holder gone 回 ATTACHED 後 release 成功。
#[test]
fn release_flashing_busy() {
    let (rig, _grant) = bb::flashing(false, 1);
    let tx0 = rig.dev.tx_len();
    let busy = release(&rig, "flashing");
    assert_eq!(keys(&busy), FLASHING_BUSY_KEYS, "{busy:?}");
    assert_eq!(str_field(&busy, "error_code").as_deref(), Some("FLASHING_BUSY"));
    assert_eq!(str_field(&busy, "selector").as_deref(), Some("COM0"));
    assert_eq!(session_state(&busy).as_deref(), Some("FLASHING"));
    assert_eq!(rig.dev.close_count(), 0, "FLASHING 中 bridge 未被 detach");
    assert_dut_silent(&rig, tx0, Duration::from_millis(50), "FLASHING_BUSY 拒絕");
    bb::holder_gone(&rig, 1);
    rig.wait_state("ATTACHED");
    let rel = release(&rig, "after flash");
    assert_eq!(rel.get("ok"), Some(&PyValue::Bool(true)), "{rel:?}");
    assert_eq!(session_state(&rel).as_deref(), Some("RELEASED"));
    assert_eq!(rig.dev.tx_len(), tx0, "release 本身零 TX");
}

/// OD-14 #4：前景命令已提交 TX 後 writer 卡住 → release 於 3,000 ms（虛擬時間）後回 `DRAIN_STALLED`、state
/// RECOVERING；再次 release 同形、不產生第二個 StopOp；期間 attach（含 force）回 `SESSION_RECOVERING`、零 open；
/// 放開 writer 送回條 → RELEASED、`drain_stalled:false`、state.json 有 released 列；下一次 release 為 `already_released`。
#[test]
fn release_drain_stalled_then_completes() {
    let h = ActorRig::new(SHELL_PROFILE).ready();
    h.dev.script_write(vec![FakeWriteStep::BlockForever]);
    let base = h.dev.os_submits();
    let cmd = h.submit_ok("sleep 1");
    h.wait_running(&cmd);
    wait_tx_in_flight(&h, base);
    let generation = h.get_state()["bridge_generation"].clone();
    let opens = h.dev.open_count();
    let t0 = h.clock.mono_ns();
    let pending = release_async(&h, "stall");
    h.wait_state("RECOVERING");
    h.clock.advance(Duration::from_millis(3001));
    let resp = recv(&pending, "device.release");
    let elapsed_ms = (h.clock.mono_ns() - t0) / 1_000_000;
    assert!(elapsed_ms >= 3000, "DRAIN_STALLED 不得早於 3,000 ms（實際 {elapsed_ms} ms）");
    assert_eq!(keys(&resp), STALLED_KEYS, "{resp:?}");
    assert_eq!(resp.get("ok"), Some(&PyValue::Bool(false)));
    assert_eq!(str_field(&resp, "error_code").as_deref(), Some("DRAIN_STALLED"));
    assert_eq!(resp.get("drain_stalled"), Some(&PyValue::Bool(true)));
    assert_eq!(resp.get("drain_timeout_epoch"), Some(&generation));
    assert_eq!(session_state(&resp).as_deref(), Some("RECOVERING"));

    let again = release(&h, "stall again");
    assert_eq!(keys(&again), STALLED_KEYS, "{again:?}");
    assert_eq!(str_field(&again, "error_code").as_deref(), Some("DRAIN_STALLED"));
    assert_eq!(again.get("drain_timeout_epoch"), Some(&generation));
    for force in [false, true] {
        let att = device_attach(&h, force);
        assert_eq!(keys(&att), RECOVERING_KEYS, "force={force}：{att:?}");
        assert_eq!(str_field(&att, "error_code").as_deref(), Some("SESSION_RECOVERING"), "{att:?}");
        assert_eq!(att.get("drain_stalled"), Some(&PyValue::Bool(true)), "{att:?}");
        assert_eq!(att.get("drain_timeout_epoch"), Some(&generation), "{att:?}");
    }
    assert_eq!(h.dev.open_count(), opens, "drain 卡住期間 attach 不得 open_exclusive");
    assert_eq!(state(&h), "RECOVERING");

    h.dev.release_block();
    h.wait_state("RELEASED");
    let s = h.get_state();
    assert_eq!(s.get("drain_stalled"), Some(&PyValue::Bool(false)), "{s:?}");
    assert!(h.state_store.last_released().contains_key("t:COM0"), "state.json 有 released 列");
    assert_eq!(h.dev.close_count(), 1, "兩次 release 只產生一個 StopOp（handle 只關一次）");
    assert_eq!(h.dev.open_count(), opens, "drain 完成後不自動重開");
    let later = release(&h, "later");
    assert_eq!(later.get("already_released"), Some(&PyValue::Bool(true)), "{later:?}");
}

/// release drain 尚未回條（未逾時）時 attach（含 force）不得 open：回 `SESSION_RECOVERING`＋`drain_stalled:false`
/// （SC-04 形狀；OD-14 #4 不改 attach 的碼）。
#[test]
fn attach_while_drain_in_progress() {
    let h = ActorRig::new(SHELL_PROFILE).ready();
    let opens = h.dev.open_count();
    h.dev.pause_next_close();
    let pending = release_async(&h, "drain");
    assert!(h.dev.wait_paused(HANDOFF_WAIT), "IoWorker 應已進入 close");
    for force in [false, true] {
        let att = device_attach(&h, force);
        assert_eq!(keys(&att), RECOVERING_KEYS, "force={force}：{att:?}");
        assert_eq!(str_field(&att, "error_code").as_deref(), Some("SESSION_RECOVERING"), "{att:?}");
        assert_eq!(att.get("drain_stalled"), Some(&PyValue::Bool(false)), "{att:?}");
        assert_eq!(att.get("drain_timeout_epoch"), Some(&PyValue::Null), "{att:?}");
    }
    assert_eq!(h.dev.open_count(), opens, "drain 進行中 attach 不得 open_exclusive");
    assert!(pending.try_recv().is_err(), "release 應仍在等回條");
    h.dev.release_pause();
    let rel = recv(&pending, "device.release");
    assert_eq!(rel.get("ok"), Some(&PyValue::Bool(true)), "{rel:?}");
    assert_eq!(session_state(&rel).as_deref(), Some("RELEASED"));
    assert_eq!(h.dev.open_count(), opens);
}

/// 兩個 `device.release` 同時送：皆 ok（第二個加入同一個 stop，回完整形狀）；只產生一次 StopOp 與一次 released 持久化。
#[test]
fn release_dedupe_concurrent() {
    let h = ActorRig::new(SHELL_PROFILE).ready();
    let commits0 = h.state_store.commits();
    h.dev.pause_next_close();
    let a = release_async(&h, "dedupe");
    assert!(h.dev.wait_paused(HANDOFF_WAIT), "IoWorker 應已進入 close");
    let b = release_async(&h, "dedupe");
    std::thread::sleep(Duration::from_millis(50));
    let _ = h.get_state();
    assert!(b.try_recv().is_err(), "第二個 release 應加入同一 stop 等回條");
    h.dev.release_pause();
    let (ra, rb) = (recv(&a, "release a"), recv(&b, "release b"));
    for r in [&ra, &rb] {
        assert_eq!(keys(r), RELEASE_OK_KEYS, "{r:?}");
        assert_eq!(r.get("ok"), Some(&PyValue::Bool(true)), "{r:?}");
        assert_eq!(session_state(r).as_deref(), Some("RELEASED"), "{r:?}");
    }
    assert_eq!(h.dev.close_count(), 1, "只產生一個 StopOp");
    assert_eq!(h.state_store.commits() - commits0, 1, "released 列只持久化一次");
    let later = release(&h, "later");
    assert_eq!(later.get("already_released"), Some(&PyValue::Bool(true)), "{later:?}");
}
