//! P6 Task 6：`Fence::admit` 的 HumanRaw／HumanReplay 規則（07 D-03、D-22；06 Task 1 FakeDevice＋`TxRig`）。
#![cfg(feature = "test-fakes")]
mod support;

use std::time::Duration;

use support::TxRig;
use sw_core::tx_writer::*;
use sw_pal::fakes::serial::FakeWriteStep;
use sw_pal::traits::TxOutcome;

#[test]
fn human_raw_queued_behind_blocked_write_rejected_after_admission_close() {
    // 失效先於 OS admission
    let r = TxRig::new();
    r.fence.with(|s| {
        s.owner_gen = 7;
        s.input_epoch = 3;
        s.human_admission_blocked = false;
    });
    r.dev.script_write(vec![FakeWriteStep::BlockForever]); // 第一筆 Command 卡在 OS write 內
    let cmd = r.submit(r.permit(TxClass::Command, 7, None, "agent"), b"ls\n");
    let raw = r.submit(r.permit(TxClass::HumanRaw, 7, Some(3), "human:c1"), b"k"); // 排在 TxWriter queue，尚未 admit
    r.dev.wait_blocked();
    r.fence.with(|s| s.human_admission_blocked = true);
    r.dev.release_block();
    assert!(matches!(cmd.recv().unwrap().result.outcome, TxOutcome::Completed(3)));
    let rep = raw.recv().unwrap();
    assert!(matches!(rep.result.outcome, TxOutcome::NotSubmitted));
    assert_eq!(rep.reject, Some(FenceReject::HumanAdmissionClosed));
    assert_eq!(r.dev.take_tx(), b"ls\n".to_vec(), "UART 不得收到 human byte");
    assert_eq!(r.wal_tx().len(), 1, "未提交的寫入不得產生 TX WAL");
}

#[test]
fn admission_close_after_os_submission_counts_as_written() {
    // 已標 in-flight 的照實記錄（arch §5.4）
    let r = TxRig::new();
    r.fence.with(|s| {
        s.owner_gen = 7;
        s.input_epoch = 3;
    });
    r.dev.script_write(vec![FakeWriteStep::BlockForever]);
    let raw = r.submit(r.permit(TxClass::HumanRaw, 7, Some(3), "human:c1"), b"k");
    r.dev.wait_blocked(); // job 已經 FenceSubmitGate 提交 OS，於 gate 外等完成（不持鎖）
    r.fence.with(|s| s.human_admission_blocked = true);
    r.dev.release_block();
    assert!(matches!(raw.recv().unwrap().result.outcome, TxOutcome::Completed(1)));
    assert_eq!(r.wal_tx().len(), 1);
}

#[test]
fn human_raw_input_epoch_bump_after_authorize_before_os_submit_is_zero_tx() {
    // R0-F1：出列檢查通過後、OS 提交前失效
    let r = TxRig::new();
    r.fence.with(|s| {
        s.owner_gen = 7;
        s.input_epoch = 3;
    });
    r.dev.script_write(vec![FakeWriteStep::PauseBeforeSubmit]);
    let raw = r.submit(r.permit(TxClass::HumanRaw, 7, Some(3), "human:c1"), b"k");
    assert!(r.dev.wait_paused(Duration::from_secs(2)));
    r.fence.with(|s| {
        s.input_epoch = 4;
        s.owner_gen = 8;
    }); // reboot／quiet re-arm／換手
    r.dev.release_pause();
    let rep = raw.recv().unwrap();
    assert!(matches!(rep.result.outcome, TxOutcome::NotSubmitted));
    assert_eq!(rep.reject, Some(FenceReject::InputStale));
    assert_eq!(r.dev.os_submits(), 0);
    assert!(r.dev.take_tx().is_empty(), "失效世代的 human byte 不得到達 UART");
    assert!(r.wal_tx().is_empty());
}

#[test]
fn human_replay_paused_before_submit_then_ownership_change_is_zero_tx() {
    // R2-F4：回放進 OS 前，下一個操作取得 ownership
    let r = TxRig::new();
    r.fence.with(|s| {
        s.owner_gen = 7;
        s.input_epoch = 3;
        s.replay_auth = Some(5);
    });
    r.dev.script_write(vec![FakeWriteStep::PauseBeforeSubmit]);
    let replay = r.submit(r.replay_permit(7, 3, 5, "human:c1"), b"old");
    assert!(r.dev.wait_paused(Duration::from_secs(2))); // 已過出列快速檢查，停在 OS 提交前
    // 下一命令 fg_begin 的等價 fence 變更（D-22(4)）：同一次 Fence::with 撤銷回放授權、關 admission、suspend 使 owner_gen++；
    // input_epoch 刻意不變——只比對 input_epoch 擋不住這個競態
    r.fence.with(|s| {
        s.replay_auth = None;
        s.human_admission_blocked = true;
        s.owner_gen = 8;
    });
    let cmd = r.submit(r.permit(TxClass::Command, 8, None, "agent"), b"ls\n");
    r.dev.release_pause();
    let rep = replay.recv().unwrap();
    assert!(matches!(rep.result.outcome, TxOutcome::NotSubmitted));
    assert_eq!(rep.reject, Some(FenceReject::ReplayRevoked));
    assert!(matches!(cmd.recv().unwrap().result.outcome, TxOutcome::Completed(3)));
    assert_eq!(r.dev.take_tx(), b"ls\n".to_vec(), "舊按鍵不得寫進下一個命令");
    assert_eq!(r.wal_tx().len(), 1, "被撤銷的回放不得產生 TX WAL");
}

#[test]
fn human_replay_with_live_auth_ignores_admission_and_owner_gen() {
    // Python parity：resume flush 早於 admission 開放，且不帶 raw token
    let r = TxRig::new();
    r.fence.with(|s| {
        s.owner_gen = 8;
        s.input_epoch = 3;
        s.human_admission_blocked = true;
        s.replay_auth = Some(5);
    });
    let rep = r.submit(r.replay_permit(7, 3, 5, "human:c1"), b"x").recv().unwrap();
    assert!(matches!(rep.result.outcome, TxOutcome::Completed(1)));
    assert_eq!(rep.reject, None);
    assert_eq!(r.dev.take_tx(), b"x".to_vec());
    assert_eq!(r.wal_tx().len(), 1);
}

#[test]
fn human_replay_without_matching_auth_rejected() {
    // D-03：hold id 不符、授權已撤銷、permit 未帶 hold 三案
    for (auth, hold) in [(Some(6u64), Some(5u64)), (None, Some(5)), (None, None)] {
        let r = TxRig::new();
        r.fence.with(|s| {
            s.owner_gen = 7;
            s.input_epoch = 3;
            s.replay_auth = auth;
        });
        let mut p = r.replay_permit(7, 3, 5, "human:c1");
        p.replay_hold = hold;
        let rep = r.submit(p, b"x").recv().unwrap();
        assert!(matches!(rep.result.outcome, TxOutcome::NotSubmitted), "auth={auth:?} hold={hold:?}");
        assert_eq!(rep.reject, Some(FenceReject::ReplayRevoked));
        assert!(r.dev.take_tx().is_empty());
        assert!(r.wal_tx().is_empty());
    }
}

#[test]
fn human_raw_owner_gen_mismatch() {
    let r = TxRig::new();
    r.fence.with(|s| {
        s.owner_gen = 8;
        s.input_epoch = 3;
    });
    let rep = r.submit(r.permit(TxClass::HumanRaw, 7, Some(3), "human:c1"), b"k").recv().unwrap();
    assert!(matches!(rep.result.outcome, TxOutcome::NotSubmitted));
    assert_eq!(rep.reject, Some(FenceReject::OwnerMismatch));
    assert!(r.dev.take_tx().is_empty());
    assert!(r.wal_tx().is_empty());
}

#[test]
fn human_raw_and_replay_input_stale() {
    // input_epoch 先於 owner_gen／replay_auth 判定
    let r = TxRig::new();
    r.fence.with(|s| {
        s.owner_gen = 7;
        s.input_epoch = 4;
        s.replay_auth = Some(5);
    });
    let raw = r.submit(r.permit(TxClass::HumanRaw, 7, Some(3), "human:c1"), b"k").recv().unwrap();
    assert!(matches!(raw.result.outcome, TxOutcome::NotSubmitted));
    assert_eq!(raw.reject, Some(FenceReject::InputStale));
    let replay = r.submit(r.replay_permit(7, 3, 5, "human:c1"), b"x").recv().unwrap();
    assert!(matches!(replay.result.outcome, TxOutcome::NotSubmitted));
    assert_eq!(replay.reject, Some(FenceReject::InputStale));
    assert!(r.dev.take_tx().is_empty());
    assert!(r.wal_tx().is_empty());
}

#[test]
fn human_raw_admission_closed_when_blocked() {
    let r = TxRig::new();
    r.fence.with(|s| {
        s.owner_gen = 7;
        s.input_epoch = 3;
        s.human_admission_blocked = true;
    });
    let rep = r.submit(r.permit(TxClass::HumanRaw, 7, Some(3), "human:c1"), b"k").recv().unwrap();
    assert_eq!(rep.reject, Some(FenceReject::HumanAdmissionClosed));
    assert!(r.dev.take_tx().is_empty());
}

#[test]
fn invalidate_linearizes_with_queued_replay() {
    let r = TxRig::new();
    r.fence.with(|s| {
        s.owner_gen = 7;
        s.input_epoch = 3;
        s.replay_auth = Some(5);
    });
    r.dev.script_write(vec![FakeWriteStep::BlockForever]);
    let cmd = r.submit(r.permit(TxClass::Command, 7, None, "agent"), b"ls\n");
    let replay = r.submit(r.replay_permit(7, 3, 5, "human:c1"), b"x");
    r.dev.wait_blocked();
    r.fence.with(|s| s.input_epoch += 1);
    r.dev.release_block();
    assert!(matches!(cmd.recv().unwrap().result.outcome, TxOutcome::Completed(3)));
    let rep = replay.recv().unwrap();
    assert!(matches!(rep.result.outcome, TxOutcome::NotSubmitted));
    assert_eq!(rep.reject, Some(FenceReject::InputStale));
    assert_eq!(r.dev.take_tx(), b"ls\n".to_vec());
}

#[test]
fn non_human_permit_ignores_human_fields() {
    // 06 既有 class 不受 admission／input_epoch／replay_auth 影響
    let r = TxRig::new();
    r.fence.with(|s| {
        s.owner_gen = 7;
        s.input_epoch = 9;
        s.human_admission_blocked = true;
        s.replay_auth = None;
    });
    let rep = r.submit(r.permit(TxClass::Command, 7, None, "agent"), b"ls\n").recv().unwrap();
    assert!(matches!(rep.result.outcome, TxOutcome::Completed(3)));
    assert_eq!(rep.reject, None);
}
