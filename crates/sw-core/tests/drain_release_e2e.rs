//! P5-T12b：Drain／Release 端到端（真 actor thread＋IoWorker＋login＋命令執行）。
//!
//! 斷言值以 Python 基線（`sw_core/session_manager.py` 的 `release_device`、`_DrainJob`、
//! `_mark_drain_stalled`）為準；`ActorRig` 由 Task 13／14 建立。
#![cfg(feature = "test-fakes")]
mod support;
use std::time::Duration;
use support::*;
use sw_core::mailbox::{ControlEvent, DataEvent};
use sw_core::session_core::*;
use sw_core::tx_writer::OpId;
use sw_pal::fakes::serial::FakeWriteStep;
use sw_pal::traits::PalError;
use sw_proto::pyjson::PyValue;

#[test]
fn release_waits_for_receipt_and_aborts_partial_in_flight() {
    let h = ActorRig::new(SHELL_PROFILE).ready();
    h.dev.script_write(vec![FakeWriteStep::BlockUntilCancel { accept_before_cancel: 4 }]);
    let base = h.dev.os_submits();
    let cmd = h.submit_ok("sleep 20");
    h.wait_running(&cmd);
    wait_tx_in_flight(&h, base);
    // 停住 IoWorker 的 close（回條送不出）：drain 確定停在 Phase 1，中間態可嚴格斷言（審查 F46）。
    h.dev.pause_next_close();
    let pending = h.request_async(SessionRequest::Release { source: "agent:t".into(), reason: PyValue::from("r") });
    assert!(h.dev.wait_paused(Duration::from_secs(5)), "IoWorker 應已進入 close");
    let mid = h.get_state();
    assert_eq!(mid["state"], PyValue::from("RECOVERING"), "drain 中狀態應為 RECOVERING");
    assert_eq!(mid["last_error"], PyValue::from("RELEASED"));     // Python `_begin_drain_locked` Phase 1（F41）
    assert_eq!(mid["foreground_busy"], PyValue::Bool(false));
    assert!(pending.try_recv().is_err(), "release 應仍在等回條");
    h.dev.release_pause();
    let resp = pending.recv_timeout(Duration::from_secs(3)).unwrap().unwrap();
    assert_eq!(resp.get("ok"), Some(&PyValue::Bool(true)));
    assert_eq!(resp.get("aborted_cmd"), Some(&PyValue::Bool(true)));
    assert_eq!(h.dev.close_count(), 1);
    let rec = h.command_get(&cmd);
    assert_eq!((rec["status"].clone(), rec["error_code"].clone()), (PyValue::from("error"), PyValue::from("COMMAND_ABORTED_IN_FLIGHT")));
    assert_eq!(h.wal_tx_payloads().last().unwrap(), &b"slee".to_vec());   // 只記確認的 4 bytes
    assert!(h.state_store.last_released().contains_key("t:COM0"));
    assert_eq!(h.get_state()["state"], PyValue::from("RELEASED"));
}

#[test]
fn drain_timeout_marks_stalled_and_release_reports_drain_stalled() {
    let h = ActorRig::new(SHELL_PROFILE).ready();
    h.dev.script_write(vec![FakeWriteStep::BlockForever]);
    let base = h.dev.os_submits();
    let cmd = h.submit_ok("x"); h.wait_running(&cmd);
    wait_tx_in_flight(&h, base);
    let generation = h.get_state()["bridge_generation"].clone();
    let pending = h.request_async(SessionRequest::Release { source: "cli".into(), reason: PyValue::Null });
    h.wait_state("RECOVERING");                                                // drain timer 已排程後才推進時鐘
    h.clock.advance(Duration::from_millis(3001));                              // FakeClock::subscribe 立即喚醒 actor
    let resp = pending.recv_timeout(Duration::from_secs(5)).unwrap().unwrap();
    assert_eq!(resp.get("ok"), Some(&PyValue::Bool(false)));                   // 審查 F47
    assert_eq!(resp.get("error_code"), Some(&PyValue::from("DRAIN_STALLED")));        // OD-14 #4
    assert_eq!(resp.get("drain_stalled"), Some(&PyValue::Bool(true)));
    assert_eq!(resp.get("drain_timeout_epoch"), Some(&generation));            // Python `job.epoch`
    assert_eq!(h.get_state()["state"], PyValue::from("RECOVERING"));
    assert_eq!(h.dev.open_count(), 1, "no reopen while stalled");
    h.dev.release_block();                                                     // 回條遲到
    h.wait_state("RELEASED");
    let again = h.request(SessionRequest::Release { source: "cli".into(), reason: PyValue::Null });
    assert_eq!(again.get("already_released"), Some(&PyValue::Bool(true)));
}

/// 等本命令的 TX 已交給假 OS（`wait_running` 只代表命令登錄為 running，OpRunner 可能尚未送出）。
fn wait_tx_in_flight(h: &ActorRig, base: usize) {
    h.wait_for("命令 TX 已交 OS", |h| h.dev.os_submits() > base);
}

/// ready 握手的 `\n`→prompt 回覆為一次性；重新 attach 前需再裝（nonce 比對規則常駐）。
fn rearm_login_reply(h: &ActorRig) {
    let _ = h.dev.take_tx();
    h.dev.on_tx_reply(b"\n", b"root@t:/# ");
}

fn release_cli() -> SessionRequest {
    SessionRequest::Release {
        source: "cli".into(),
        reason: PyValue::Null,
    }
}

/// §5.2.4：drain 進行中 mailbox 不阻塞，GetState 立即回 `RECOVERING`（原列於 Task 11）。
#[test]
fn actor_answers_get_state_while_drain_pending() {
    let h = ActorRig::new(SHELL_PROFILE).ready();
    h.dev.script_write(vec![FakeWriteStep::BlockForever]);
    let base = h.dev.os_submits();
    let cmd = h.submit_ok("sleep 20");
    h.wait_running(&cmd);
    wait_tx_in_flight(&h, base);
    let pending = h.request_async(release_cli());
    let started = std::time::Instant::now();
    let resp = h.request(SessionRequest::GetState);
    assert!(started.elapsed() < Duration::from_millis(50), "GetState 應於 50 ms 內回應：{:?}", started.elapsed());
    assert_eq!(resp.get("ok"), Some(&PyValue::Bool(true)));
    match resp.get("session") {
        Some(PyValue::Dict(s)) => assert_eq!(s["state"], PyValue::from("RECOVERING")),
        other => panic!("GetState 回應應含 session dict：{other:?}"),
    }
    assert!(pending.try_recv().is_err(), "release 應仍在等回條");
    h.dev.release_block();
    let done = pending.recv_timeout(Duration::from_secs(5)).unwrap().unwrap();
    assert_eq!(done.get("ok"), Some(&PyValue::Bool(true)));
    h.wait_state("RELEASED");
}

/// writer 回 `Unknown` 時 release → 命令 `SEND_FAILED`，且之後不再寫 UART（不重送）。
#[test]
fn in_flight_unknown_is_send_failed_no_resend() {
    let h = ActorRig::new(SHELL_PROFILE).ready();
    h.dev.script_write(vec![FakeWriteStep::Unknown {
        confirmed: 0,
        error: PalError::io(5, "Input/output error"),
    }]);
    let base = h.dev.write_calls();
    let cmd = h.submit_ok("sleep 20");
    h.wait_for("writer 回 Unknown", |h| h.dev.write_calls() > base);
    let writes = h.dev.write_calls();
    let resp = h.request(release_cli());
    assert_eq!(resp.get("ok"), Some(&PyValue::Bool(true)), "{resp:?}");
    let rec = h.wait_done(&cmd);
    assert_eq!(rec["status"], PyValue::from("error"));
    assert_eq!(rec["error_code"], PyValue::from("SEND_FAILED"));
    h.wait_state("RELEASED");
    std::thread::sleep(Duration::from_millis(50));
    assert_eq!(h.dev.write_calls(), writes, "release 後不得重送");
}

/// 審查 F44：release 時命令仍在途、writer 於 cancel 後才回 `Unknown { confirmed: 0 }` → `aborted_cmd:true`、
/// 命令 `SEND_FAILED`（Python `_map_aborted_result` confirmed 0＋unknown），重新 attach 後不重送。
#[test]
fn in_flight_unknown_during_drain_is_send_failed_no_resend() {
    let h = ActorRig::new(SHELL_PROFILE).ready();
    h.dev.script_write(vec![FakeWriteStep::BlockUntilCancelThenUnknown {
        confirmed: 0,
        error: PalError::io(5, "Input/output error"),
    }]);
    let base = h.dev.os_submits();
    let cmd = h.submit_ok("sleep 20");
    h.wait_running(&cmd);
    wait_tx_in_flight(&h, base);
    assert_eq!(h.command_get(&cmd)["status"], PyValue::from("running"));
    let resp = h
        .request_async(release_cli())
        .recv_timeout(Duration::from_secs(5))
        .unwrap()
        .unwrap();
    assert_eq!(resp.get("ok"), Some(&PyValue::Bool(true)), "{resp:?}");
    assert_eq!(resp.get("aborted_cmd"), Some(&PyValue::Bool(true)));
    let rec = h.wait_done(&cmd);
    assert_eq!(rec["status"], PyValue::from("error"));
    assert_eq!(rec["error_code"], PyValue::from("SEND_FAILED"));
    h.wait_state("RELEASED");
    rearm_login_reply(&h);
    let attach = h.request(SessionRequest::DeviceAttach { force: false });
    assert_eq!(attach.get("ok"), Some(&PyValue::Bool(true)), "{attach:?}");
    h.wait_state("READY");
    std::thread::sleep(Duration::from_millis(50));
    let tx = h.dev.take_tx();
    assert!(
        !tx.windows(b"sleep 20".len()).any(|w| w == b"sleep 20"),
        "重新 attach 後不得重送：{:?}",
        String::from_utf8_lossy(&tx)
    );
}

/// drain 期間 WAL 不可寫 → release 仍完成、WAL 不健康、stderr WARNING 列出未落盤證據。
#[test]
fn wal_unwritable_does_not_block_close_but_lists_gap() {
    let h = ActorRig::new(SHELL_PROFILE).ready();
    h.dev.script_write(vec![FakeWriteStep::BlockUntilCancel { accept_before_cancel: 4 }]);
    let base = h.dev.os_submits();
    let cmd = h.submit_ok("sleep 20");
    h.wait_running(&cmd);
    wait_tx_in_flight(&h, base);
    h.fail_wal_writes(PalError::io(28, "No space left on device"));
    let resp = h
        .request_async(release_cli())
        .recv_timeout(Duration::from_secs(5))
        .unwrap()
        .unwrap();
    assert_eq!(resp.get("ok"), Some(&PyValue::Bool(true)), "{resp:?}");
    assert_eq!(h.dev.close_count(), 1);
    assert_eq!(h.get_state()["state"], PyValue::from("RELEASED"));
    assert!(!h.wal_health().healthy, "WAL 應標記不健康");
    let warns = h.stderr_lines();
    assert!(
        warns.iter().any(|l| l.contains("未落盤")),
        "stderr 應含未落盤 WARNING：{warns:?}"
    );
}

/// close 失敗 → 維持 `RECOVERING`、`drain_stalled:true`，release waiter 收 `DRAIN_STALLED`（OD-14 #4）。
#[test]
fn close_failure_keeps_recovering_and_no_success() {
    let h = ActorRig::new(SHELL_PROFILE).ready();
    h.dev.fail_close(PalError::io(5, "Input/output error"));
    let resp = h
        .request_async(release_cli())
        .recv_timeout(Duration::from_secs(5))
        .unwrap()
        .unwrap();
    assert_eq!(resp.get("ok"), Some(&PyValue::Bool(false)), "{resp:?}");
    assert_eq!(resp.get("error_code"), Some(&PyValue::from("DRAIN_STALLED")));
    assert_eq!(resp.get("drain_stalled"), Some(&PyValue::Bool(true)));
    assert_eq!(resp.get("drain_timeout_epoch"), Some(&h.get_state()["bridge_generation"])); // 審查 F47
    let s = h.get_state();
    assert_eq!(s["state"], PyValue::from("RECOVERING"));
    assert_eq!(s["drain_stalled"], PyValue::Bool(true));
    assert!(h.state_store.last_released().is_empty(), "close 失敗不得落 released 列");
    let again = h.request(release_cli());
    assert_eq!(again.get("error_code"), Some(&PyValue::from("DRAIN_STALLED")));
}

/// data lane 滿載的背景執行緒（drop 時停止）：
/// - FakeDevice 持續 push RX（真 IoWorker reader 路徑）；
/// - reader 單獨推不滿 data lane（actor 消化較快），另以 data lane 送出端直接注入現行 epoch 的 RX chunk，
///   讓 lane 維持滿載（bounded send 阻塞＝已滿）。
struct Flood {
    stop: std::sync::Arc<std::sync::atomic::AtomicBool>,
    joins: Vec<std::thread::JoinHandle<()>>,
    /// 注入端遇到 data lane 已滿（bounded send 逾時）的次數。
    full_hits: std::sync::Arc<std::sync::atomic::AtomicU64>,
}

impl Flood {
    fn start(h: &ActorRig) -> Flood {
        let stop = std::sync::Arc::new(std::sync::atomic::AtomicBool::new(false));
        let epoch = match h.get_state()["bridge_generation"] {
            PyValue::Int(sw_proto::pyjson::PyInt::Small(n)) => n as u64,
            ref other => panic!("bridge_generation 應為整數：{other:?}"),
        };
        let dev = h.dev.clone();
        let flag = stop.clone();
        let pusher = std::thread::spawn(move || {
            let chunk = [b'f'; 64];
            while !flag.load(std::sync::atomic::Ordering::Relaxed) {
                dev.push_rx(&chunk);
                std::thread::yield_now();
            }
        });
        let data = h.data_sender();
        let flag = stop.clone();
        let full_hits = std::sync::Arc::new(std::sync::atomic::AtomicU64::new(0));
        let hits = full_hits.clone();
        let injector = std::thread::spawn(move || {
            while !flag.load(std::sync::atomic::Ordering::Relaxed) {
                let ev = DataEvent::UartRxChunk { epoch, rx_seq: 0, wal_seq: 0, bytes: b"flood".to_vec() };
                if let Err(crossbeam_channel::TrySendError::Full(ev)) = data.try_send(ev) {
                    hits.fetch_add(1, std::sync::atomic::Ordering::Relaxed);
                    let _ = data.send_timeout(ev, Duration::from_millis(5));
                }
            }
        });
        Flood { stop, joins: vec![pusher, injector], full_hits }
    }

    /// 等 data lane 首次滿載（注入端 try_send 回 Full）。
    fn wait_full(&self) {
        let deadline = std::time::Instant::now() + Duration::from_secs(5);
        while self.full_hits.load(std::sync::atomic::Ordering::Relaxed) == 0 {
            assert!(std::time::Instant::now() < deadline, "data lane 未達滿載");
            std::thread::sleep(Duration::from_millis(1));
        }
    }

    /// 停止 flood，並斷言期間 data lane 確實曾經滿載（注入端 send 逾時至少一次）。
    fn finish(self) {
        let hits = self.full_hits.clone();
        drop(self);
        assert!(hits.load(std::sync::atomic::Ordering::Relaxed) > 0, "flood 期間 data lane 應曾滿載");
    }
}

impl Drop for Flood {
    fn drop(&mut self) {
        self.stop.store(true, std::sync::atomic::Ordering::Relaxed);
        for j in self.joins.drain(..) {
            let _ = j.join();
        }
    }
}

/// 等 actor 在 flood 下已處理足夠 data item（確認 data lane 確實有負載）。
fn wait_flowing(h: &ActorRig, more: u64) {
    let probe = h.actor_probe();
    let base = probe.data_items();
    let deadline = std::time::Instant::now() + Duration::from_secs(5);
    while probe.data_items() < base + more {
        assert!(std::time::Instant::now() < deadline, "flood 未產生 data item");
        std::thread::sleep(Duration::from_millis(1));
    }
}

/// 斷言標籤事件在 data 滿載下等待期間 actor 最多再處理 1 個 data item（event_latency.l1_bound）。
fn assert_within_one_data_item(h: &ActorRig, label: &str) {
    let entries = h.actor_probe().entries(label);
    assert!(!entries.is_empty(), "探針應記錄 {label}");
    for e in &entries {
        assert!(e.data_while_waiting <= 1, "{label} 等待期間處理了 {} 個 data item（上限 1）：{e:?}", e.data_while_waiting);
    }
}

/// OD-64（擁有者裁決 (a)，2026-10-29）：drain_receipt（`WorkerStopped`）的界限——receipt 送出後至多處理「依賴等待前已讀入
/// mailbox 的同 epoch data」再加 1 筆（`ProbeEntry::pre_dependency_data`；無依賴等待者為 0，即 1 筆）。
fn assert_drain_receipt_within_od64_bound(h: &ActorRig) {
    let entries = h.actor_probe().entries("WorkerStopped");
    assert!(!entries.is_empty(), "探針應記錄 WorkerStopped");
    for e in &entries {
        assert!(
            e.data_while_waiting <= e.pre_dependency_data + 1,
            "WorkerStopped 等待期間處理了 {} 個 data item（OD-64 上限 {}＋1）：{e:?}",
            e.data_while_waiting,
            e.pre_dependency_data
        );
    }
}

/// event_latency 審閱 2 C4：l1_gate 每個標籤（Release、CancelQueued、LeaseExpiry、Shutdown、WorkerStopped）的 flood 情境
/// 重複次數——單次取樣對間歇性違規（如 data 越過 control 至多 3 筆、timer 延後檢查）的偵測力不足。
const L1_GATE_REPEATS: usize = 20;

/// §5.2.4／`resource_limits.event_latency.l1_bound`：data lane 滿載時 release／cancel／lease expiry
/// 皆在最多再處理 1 個 data item 後被處理（各重複 `L1_GATE_REPEATS` 次）。
#[test]
fn release_handled_within_one_data_item_under_flood() {
    for _ in 0..L1_GATE_REPEATS {
        release_flood_round();
    }
    for _ in 0..L1_GATE_REPEATS {
        cancel_flood_round();
    }
    for _ in 0..L1_GATE_REPEATS {
        lease_flood_round();
    }
}

fn release_flood_round() {
    // release
    let h = ActorRig::new(SHELL_PROFILE).ready();
    let flood = Flood::start(&h);
    wait_flowing(&h, 200);
    flood.wait_full();
    let resp = h
        .request_async(release_cli())
        .recv_timeout(Duration::from_secs(5))
        .unwrap()
        .unwrap();
    assert_eq!(resp.get("ok"), Some(&PyValue::Bool(true)), "{resp:?}");
    flood.finish();
    assert_within_one_data_item(&h, "Release");
    h.wait_state("RELEASED");
    // OD-45 drain_receipt：release 的排空回條（WorkerStopped）同樣最多再處理 1 個 data item（本情境無同 epoch 依賴，
    // OD-64 界限亦為 1 筆）。
    assert_within_one_data_item(&h, "WorkerStopped");
    assert_drain_receipt_within_od64_bound(&h);
}

fn cancel_flood_round() {
    // command.cancel（排隊中的命令）
    let h = ActorRig::new(SHELL_PROFILE).ready();
    h.dev.script_write(vec![FakeWriteStep::BlockForever]);
    let base = h.dev.os_submits();
    let hold = h.submit_ok("sleep 20");
    h.wait_running(&hold);
    wait_tx_in_flight(&h, base);
    let queued = h.submit_ok("echo q");
    let flood = Flood::start(&h);
    wait_flowing(&h, 200);
    flood.wait_full();
    let resp = h.request(SessionRequest::CancelQueued { cmd_id: queued.clone() });
    assert_eq!(resp.get("ok"), Some(&PyValue::Bool(true)), "{resp:?}");
    flood.finish();
    assert_within_one_data_item(&h, "CancelQueued");
    h.dev.release_block();
}

fn lease_flood_round() {
    // agent interactive lease expiry
    let h = ActorRig::new(SHELL_PROFILE).ready();
    let open = h.request(SessionRequest::InteractiveOpen {
        owner: "agent:a".into(),
        timeout_s: 3.0,
        command: String::new(),
        allow_attached: false,
    });
    assert_eq!(open.get("ok"), Some(&PyValue::Bool(true)), "{open:?}");
    let flood = Flood::start(&h);
    wait_flowing(&h, 200);
    flood.wait_full();
    h.clock.advance(Duration::from_millis(3_500));
    let probe = h.actor_probe();
    let deadline = std::time::Instant::now() + Duration::from_secs(5);
    while probe.entries("LeaseExpiry").is_empty() {
        assert!(std::time::Instant::now() < deadline, "lease expiry timer 未處理");
        std::thread::sleep(Duration::from_millis(1));
    }
    flood.finish();
    assert_within_one_data_item(&h, "LeaseExpiry");
    h.wait_for("lease 到期收回", |h| h.get_state()["interactive_session_id"].is_null());
}

/// OD-45：`stop_request`（daemon.stop → actor 收到 Shutdown）與 `drain_receipt`（WorkerStopped）的 L1 結構 gate——
/// data lane 滿載時兩者皆在最多再處理 1 個 data item 後被處理（`resource_limits.event_latency.l1_bound`；重複
/// `L1_GATE_REPEATS` 次，審閱 2 C4）。
#[test]
fn shutdown_and_drain_receipt_handled_within_one_data_item_under_flood() {
    for _ in 0..L1_GATE_REPEATS {
        shutdown_flood_round();
    }
}

fn shutdown_flood_round() {
    let h = ActorRig::new(SHELL_PROFILE).ready();
    let flood = Flood::start(&h);
    wait_flowing(&h, 200);
    flood.wait_full();
    let (reply, rx) = crossbeam_channel::bounded(1);
    h.send_control(ControlEvent::Shutdown { reply });
    let resp = rx
        .recv_timeout(Duration::from_secs(10))
        .expect("Shutdown 須於排空期限內回覆")
        .expect("Shutdown 回 dict");
    assert!(resp.get("ok").is_some(), "{resp:?}");
    flood.finish();
    assert_within_one_data_item(&h, "Shutdown");
    assert_within_one_data_item(&h, "WorkerStopped");
    assert_drain_receipt_within_od64_bound(&h);
}

/// 依賴情境 gate 的 executor：`BeginDrain` 即以同 `(epoch, stop_id)` 送出 `WorkerStopped`（模擬 finisher），其餘只回覆。
struct DrainReceiptExec;

impl sw_core::actor::EffectExecutor for DrainReceiptExec {
    fn execute(
        &mut self,
        effect: Effect,
        _tx: &crossbeam_channel::Sender<ControlEvent>,
        safety: &crossbeam_channel::Sender<ControlEvent>,
    ) {
        match effect {
            Effect::BeginDrain { stop_id, epoch } => {
                let _ = safety.send(ControlEvent::WorkerStopped(receipt(epoch, stop_id.0)));
            }
            Effect::Reply(reply, result) => {
                let _ = reply.try_send(result);
            }
            _ => {}
        }
    }
}

/// OD-64／event_latency 審閱 2 C3：在途命令的 `OpFinished` 帶 `after_rx_seq = backlog`、data lane 有限 backlog（前置 1 筆
/// 舊 epoch data，其後 `backlog` 筆同 epoch data）時觸發排空（release 或 shutdown）。receipt 因同 epoch 依賴移入 `stopped`，
/// N_pre＝`backlog`（舊 epoch 那筆計入「＋1」）；回傳 receipt 的探針紀錄。
fn receipt_entry_with_dependency_backlog(backlog: u64, stop_event: ControlEvent) -> sw_core::actor::ProbeEntry {
    let mut h = CoreHarness::new(SHELL_PROFILE);
    h.to_ready();
    let started = h.start_foreground_command("sleep 5");
    let epoch = h.epoch();
    let clock: std::sync::Arc<dyn sw_pal::traits::Clock> = h.clock.clone();
    let core = h.core;
    let mb = sw_core::mailbox::Mailbox::new(256, 1024);
    mb.control_sender()
        .send(ControlEvent::OpFinished {
            op_id: started.op_id,
            epoch,
            outcome: sw_core::op_runner::OpOutcome::SendFailed,
            after_rx_seq: Some(backlog),
        })
        .expect("OpFinished 送出");
    let data = mb.data_sender();
    data.send(DataEvent::UartRxChunk { epoch: epoch - 1, rx_seq: 99, wal_seq: 0, bytes: b"stale".to_vec() })
        .expect("舊 epoch data 送出");
    for seq in 1..=backlog {
        data.send(DataEvent::UartRxChunk { epoch, rx_seq: seq, wal_seq: seq, bytes: b"rx".to_vec() })
            .expect("data 送出");
    }
    mb.control_sender().send(stop_event).expect("排空請求送出");
    let handle = sw_core::actor::spawn_actor(core, mb, Box::new(DrainReceiptExec), clock);
    let deadline = std::time::Instant::now() + Duration::from_secs(5);
    while handle.probe.entries("WorkerStopped").is_empty() {
        assert!(std::time::Instant::now() < deadline, "actor 未處理 receipt");
        std::thread::sleep(Duration::from_millis(1));
    }
    let entry = handle.probe.entries("WorkerStopped")[0].clone();
    // 在途命令的 OpFinished 須在 receipt 之前被處理（Python 同序：reader join 後才有回條）。
    assert_eq!(handle.probe.entries("Control").len(), 1, "OpFinished 應先於 receipt 被處理：{entry:?}");
    entry
}

/// 審閱 2 C3／C4：同一情境重複次數（間歇性違規的偵測力）。
const DEPENDENCY_GATE_REPEATS: usize = 20;

fn assert_dependency_receipt_within_od64_bound(e: &sw_core::actor::ProbeEntry, backlog: u64) {
    assert_eq!(e.pre_dependency_data, backlog, "receipt 須走到依賴等待（`stopped`），N_pre＝同 epoch backlog：{e:?}");
    assert_eq!(e.data_while_waiting, backlog + 1, "receipt 送入後處理 backlog＋1 筆舊 epoch data：{e:?}");
    assert!(
        e.data_while_waiting <= e.pre_dependency_data + 1,
        "OD-64：receipt 等待期間處理了 {} 個 data item（上限 N_pre {}＋1）：{e:?}",
        e.data_while_waiting,
        e.pre_dependency_data
    );
}

/// OD-64（drain_receipt l1_only）＋審閱 2 C3：release 排空時 receipt 因在途命令的同 epoch 依賴進入等待，
/// 至多處理依賴等待前已讀入的同 epoch data＋1 筆。
#[test]
fn drain_receipt_dependency_wait_on_release_within_od64_bound() {
    for round in 0..DEPENDENCY_GATE_REPEATS {
        let backlog = 3 + round as u64 % 5;
        let e = receipt_entry_with_dependency_backlog(
            backlog,
            ControlEvent::Request { req: release_cli(), reply: crossbeam_channel::bounded(1).0 },
        );
        assert_dependency_receipt_within_od64_bound(&e, backlog);
    }
}

/// OD-64＋審閱 2 C3：shutdown 排空的 receipt 同上。
#[test]
fn drain_receipt_dependency_wait_on_shutdown_within_od64_bound() {
    for round in 0..DEPENDENCY_GATE_REPEATS {
        let backlog = 3 + round as u64 % 5;
        let e = receipt_entry_with_dependency_backlog(backlog, ControlEvent::Shutdown { reply: crossbeam_channel::bounded(1).0 });
        assert_dependency_receipt_within_od64_bound(&e, backlog);
    }
}

/// release→attach→READY 後，舊 epoch writer 晚到的 `UartTxCompleted` 不改變新 session；WAL 保有該 TX 紀錄。
#[test]
fn old_epoch_tx_completion_after_reattach_keeps_evidence() {
    let h = ActorRig::new(SHELL_PROFILE).ready();
    h.dev.on_tx_reply(b"echo old\n", b"echo old\r\nold\r\nroot@t:/# ");
    let cmd = h.submit_ok("echo old");
    assert_eq!(h.wait_done(&cmd)["stdout"], PyValue::from("old"));
    let old_tx = h
        .wal_all_rows()
        .into_iter()
        .filter(|r| r.get("dir").and_then(|v| v.as_str()) == Some("TX"))
        .find(|r| r.get("cmd_id").and_then(|v| v.as_str()) == Some(cmd.as_str()))
        .expect("舊 epoch TX 應已由 TxWriter 寫入 WAL");
    assert_eq!(h.request(release_cli()).get("ok"), Some(&PyValue::Bool(true)));
    h.wait_state("RELEASED");
    rearm_login_reply(&h);
    let attach = h.request(SessionRequest::DeviceAttach { force: false });
    assert_eq!(attach.get("ok"), Some(&PyValue::Bool(true)), "{attach:?}");
    h.wait_state("READY");
    assert_eq!(h.dev.open_count(), 2);
    // 推進時鐘：若誤收舊 epoch 完成事件，`last_tx_age_s` 會被歸零而可觀察。
    h.advance(Duration::from_secs(2));
    let before = h.get_state();
    assert_eq!(before["state"], PyValue::from("READY"));
    let rows_before = h.wal_all_rows().len();
    h.send_control(ControlEvent::UartTxCompleted {
        epoch: 1,
        op_id: OpId(1),
        confirmed_bytes: 9,
        unknown: false,
    });
    let after = h.get_state();
    assert_eq!(after, before, "舊 epoch 完成事件不得改變新 session");
    let rows = h.wal_all_rows();
    assert_eq!(rows.len(), rows_before, "舊 epoch 完成事件不另寫 WAL");
    assert!(rows.contains(&old_tx), "舊 epoch TX 證據應保留");
}

