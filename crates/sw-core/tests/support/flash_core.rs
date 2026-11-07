//! P8 Task 8：FLASHING 仲裁測試支援（`docs/rust-refactor/plans/09-slice24-transfer-flash.md` Task 8 Step 1 的 `kit`）。
//!
//! - [`kit`]：以 P5 `CoreHarness` 直接驅動 sans-IO `SessionCore`，斷言效果（`OpenBridge`／`BeginDrain`）、世代、fence
//!   與 human console 狀態；bridge 為真的 `IoWorker`＋FakeDevice（RX 經真 reader 進 WAL）。
//! - [`bb`]：**黑箱零 TX 證據**——以 `ActorRig`（真 actor thread＋OpRunner＋TxWriter＋human TX pump＋可選 ConsoleHub）
//!   驅動，只以 FakeDevice 的實際寫出序列（`tx_len`／`tx_since`）與 WAL 的 TX 紀錄判定有無送出。
//!
//! flash 事件（`FlashSyncProbeStart`／`FlashProbeReleased`／`FlashSyncMatched`／`FlashHolderGone`）在 T9 之前沒有產品
//! 送出端，本 kit 以測試替身扮演 FlashCoordinator：直接投遞事件，並經 `TxWriterHandle` 送 `FlashProbe`／`Flash` TX。

use std::time::{Duration, Instant};

use crossbeam_channel::bounded;
use sw_core::mailbox::ControlEvent;
use sw_core::session_core::{Effect, FlashReject, ProbeGrant, ProbeId, SessionRequest, SessionState};
use sw_core::tx_writer::{FlashMode, OpId, TxClass, TxJob, TxPermit, TxReport};
use sw_proto::pyjson::{loads, PyDict, PyValue};

use super::{ActorRig, CoreHarness, PASSTHROUGH_PROFILE, SHELL_PROFILE};

/// WAL 列的 `dir`。
fn dir_of(row: &PyDict) -> Option<&str> {
    row.get("dir").and_then(PyValue::as_str)
}

/// WAL 列的 payload（base64 解碼）。
pub fn payload_of(row: &PyDict) -> Vec<u8> {
    let b64 = row.get("payload_b64").and_then(PyValue::as_str).unwrap_or("");
    b64_decode(b64)
}

fn b64_decode(s: &str) -> Vec<u8> {
    const T: &[u8; 64] = b"ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789+/";
    let mut out = Vec::new();
    let mut buf = 0u32;
    let mut bits = 0u32;
    for c in s.bytes() {
        if c == b'=' {
            break;
        }
        let Some(v) = T.iter().position(|&t| t == c) else {
            continue;
        };
        buf = (buf << 6) | v as u32;
        bits += 6;
        if bits >= 8 {
            bits -= 8;
            out.push((buf >> bits) as u8);
        }
    }
    out
}

fn parse_wal(bytes: &[u8]) -> Vec<PyDict> {
    String::from_utf8_lossy(bytes)
        .lines()
        .filter(|l| !l.trim().is_empty())
        .filter_map(|l| match loads(l) {
            Ok(PyValue::Dict(d)) => Some(d),
            _ => None,
        })
        .collect()
}

/// 回應的 `error_code`。
pub fn code(r: &PyDict) -> Option<String> {
    r.get("error_code").and_then(PyValue::as_str).map(str::to_string)
}

/// 回應的鍵序（RPC 線路不排序，Python 鍵序即契約）。
pub fn keys(r: &PyDict) -> Vec<String> {
    r.0.keys().cloned().collect()
}

/// 巢狀 `session.state`。
pub fn session_state(r: &PyDict) -> Option<String> {
    match r.get("session") {
        Some(PyValue::Dict(s)) => s.get("state").and_then(PyValue::as_str).map(str::to_string),
        _ => None,
    }
}

pub fn is_open_bridge(e: &Effect) -> bool {
    matches!(e, Effect::OpenBridge { .. } | Effect::Reattach { .. })
}

pub fn is_begin_drain(e: &Effect) -> bool {
    matches!(e, Effect::BeginDrain { .. })
}

pub fn is_persist(e: &Effect) -> bool {
    matches!(e, Effect::Persist { .. })
}

/// 效果中的 HumanTx payload。
pub fn human_tx_payloads(fx: &[Effect]) -> Vec<Vec<u8>> {
    fx.iter()
        .filter_map(|e| match e {
            Effect::HumanTx { payload, .. } => Some(payload.clone()),
            _ => None,
        })
        .collect()
}

pub fn has_start_op(fx: &[Effect]) -> bool {
    fx.iter().any(|e| matches!(e, Effect::StartOp { .. }))
}

/// CoreHarness 版 kit（sans-IO 狀態機；效果與世代斷言）。
pub mod kit {
    use super::*;
    use sw_core::mailbox::DataEvent;

    /// passthrough explicit target 達 ATTACHED（attach 零 TX）。
    pub fn attached_passthrough() -> CoreHarness {
        let mut h = CoreHarness::new(PASSTHROUGH_PROFILE);
        h.to_attached(None);
        assert_eq!(h.fake_device.tx_len(), 0, "passthrough explicit target attach 零 TX");
        h
    }

    /// passthrough 安靜 30 s 升 READY（SC-35）。
    pub fn ready_passthrough() -> CoreHarness {
        let mut h = attached_passthrough();
        h.advance(Duration::from_secs(31));
        assert_eq!(h.core.state(), SessionState::Ready, "passthrough 30 s 安靜應升 READY");
        h
    }

    /// command_capable 的 shell profile 達 READY。
    pub fn ready_shell() -> CoreHarness {
        let mut h = CoreHarness::new(SHELL_PROFILE);
        h.to_ready();
        h
    }

    pub fn probe_start_fx(h: &mut CoreHarness, id: ProbeId) -> (Result<ProbeGrant, FlashReject>, Vec<Effect>) {
        let (tx, rx) = bounded(1);
        let fx = h.feed(ControlEvent::FlashSyncProbeStart { probe_id: id, reply: tx });
        (rx.try_recv().expect("FlashSyncProbeStart 應於同一事件回覆"), fx)
    }

    pub fn probe_start(h: &mut CoreHarness, id: ProbeId) -> Result<ProbeGrant, FlashReject> {
        probe_start_fx(h, id).0
    }

    pub fn matched_fx(h: &mut CoreHarness, id: ProbeId) -> (Result<(), FlashReject>, Vec<Effect>) {
        let (tx, rx) = bounded(1);
        let fx = h.feed(ControlEvent::FlashSyncMatched { probe_id: id, reply: tx });
        (rx.try_recv().expect("FlashSyncMatched 應於同一事件回覆"), fx)
    }

    pub fn matched(h: &mut CoreHarness, id: ProbeId) -> Result<(), FlashReject> {
        matched_fx(h, id).0
    }

    pub fn released(h: &mut CoreHarness, id: ProbeId) -> Vec<Effect> {
        h.feed(ControlEvent::FlashProbeReleased { probe_id: id })
    }

    pub fn holder_gone(h: &mut CoreHarness, id: ProbeId) -> Vec<Effect> {
        let (tx, rx) = bounded(1);
        let fx = h.feed(ControlEvent::FlashHolderGone { probe_id: id, reply: tx });
        rx.try_recv().expect("FlashHolderGone 應於同一事件回覆（含舊 probe 的遲到事件）");
        fx
    }

    /// passthrough ATTACHED → Probe(id) → Matched → FLASHING。
    pub fn flashing(id: ProbeId) -> CoreHarness {
        let mut h = attached_passthrough();
        probe_start(&mut h, id).expect("probe 應授權");
        matched(&mut h, id).expect("同一 probe 應可 Matched");
        assert_eq!(h.core.state(), SessionState::Flashing);
        h
    }

    pub fn state(h: &CoreHarness) -> SessionState {
        h.core.state()
    }

    pub fn flash_mode(h: &CoreHarness) -> FlashMode {
        h.fence.with(|f| f.flash_mode)
    }

    pub fn ownership_flash_mode(h: &CoreHarness) -> bool {
        h.core.human_console().ownership.flash_mode
    }

    pub fn input_epoch(h: &CoreHarness) -> u64 {
        h.core.generations().input_epoch
    }

    pub fn owner_gen(h: &CoreHarness) -> u64 {
        h.core.generations().owner_gen
    }

    pub fn conn_epoch(h: &CoreHarness) -> Option<u64> {
        h.core.generations().conn_epoch
    }

    /// DUT 實際收到的全部 bytes（FakeDevice 確認寫出）。
    pub fn dut_tx(h: &CoreHarness) -> Vec<u8> {
        h.fake_device.tx_since(0)
    }

    pub fn wal_rows(h: &CoreHarness) -> Vec<PyDict> {
        parse_wal(&h.wfs.contents(&h.wal.wal_path()).unwrap_or_default())
    }

    pub fn wal_dir_payloads(h: &CoreHarness, dir: &str) -> Vec<Vec<u8>> {
        wal_rows(h)
            .iter()
            .filter(|r| dir_of(r) == Some(dir))
            .map(payload_of)
            .collect()
    }

    pub fn last_error(h: &CoreHarness) -> Option<String> {
        h.public().get("last_error").and_then(PyValue::as_str).map(str::to_string)
    }

    /// 經真 IoWorker reader 注入 RX（WAL RX 寫入後才投遞 data lane）並送進 core。
    pub fn inject_rx(h: &mut CoreHarness, bytes: &[u8]) {
        let before = wal_dir_payloads(h, "RX").len();
        h.fake_device.push_rx(bytes);
        let deadline = Instant::now() + Duration::from_secs(5);
        while wal_dir_payloads(h, "RX").len() == before {
            assert!(Instant::now() < deadline, "RX 未進 WAL");
            std::thread::sleep(Duration::from_millis(2));
        }
        h.drain_data_lane_for_test();
    }

    /// 以指定 `input_epoch` 投遞 console 輸入（模擬 probe 前已排入 data lane、之後才處理的事件）。
    pub fn console_input_with_epoch(h: &mut CoreHarness, cid: &str, epoch: u64, bytes: &[u8]) -> Vec<Effect> {
        let lifetime = h.lifetime(cid);
        h.data(DataEvent::ConsoleInputBytes {
            client_id: cid.into(),
            client_lifetime: lifetime,
            input_epoch: epoch,
            bytes: bytes.to_vec(),
        })
    }

    pub fn release_req() -> SessionRequest {
        SessionRequest::Release { source: "cli".into(), reason: PyValue::Null }
    }

    /// release 走完 drain 回條到 RELEASED。
    pub fn to_released(h: &mut CoreHarness) {
        let (rx, _) = h.rpc_deferred(release_req());
        let (epoch, stop) = h.pending_stop();
        h.receipt(epoch, stop).feed();
        let r = rx.try_recv().expect("release 回覆").expect("release ok");
        assert_eq!(session_state(&r).as_deref(), Some("RELEASED"));
    }

    pub fn has_warn(h: &CoreHarness, needle: &str) -> bool {
        h.warns().iter().any(|w| w.contains(needle))
    }
}

/// 黑箱版（ActorRig）：真 actor＋executor；TX 只以 FakeDevice 寫出序列與 WAL 判定。
pub mod bb {
    use super::*;
    use std::sync::atomic::{AtomicU64, Ordering};

    const WAIT: Duration = Duration::from_secs(5);
    /// 測試替身 coordinator 的 op_id 區段（與 actor 的 op 配號不重疊）。
    static NEXT_OP: AtomicU64 = AtomicU64::new(9_000_000);

    /// passthrough explicit target 達 ATTACHED（attach 零 TX）。
    pub fn attached(hub: bool) -> ActorRig {
        let rig = if hub {
            ActorRig::new_with_hub(PASSTHROUGH_PROFILE)
        } else {
            ActorRig::new(PASSTHROUGH_PROFILE)
        };
        let rig = rig.attach();
        rig.wait_state("ATTACHED");
        assert_eq!(rig.dev.tx_len(), 0, "passthrough explicit target attach 零 TX");
        rig
    }

    /// passthrough 安靜 30 s 升 READY（SC-35）。
    pub fn ready(hub: bool) -> ActorRig {
        let rig = attached(hub);
        rig.advance(Duration::from_secs(31));
        rig.wait_state("READY");
        rig
    }

    pub fn probe_start(rig: &ActorRig, id: ProbeId) -> Result<ProbeGrant, FlashReject> {
        let (tx, rx) = bounded(1);
        rig.send_control(ControlEvent::FlashSyncProbeStart { probe_id: id, reply: tx });
        rx.recv_timeout(WAIT).expect("FlashSyncProbeStart 回覆")
    }

    pub fn matched(rig: &ActorRig, id: ProbeId) -> Result<(), FlashReject> {
        let (tx, rx) = bounded(1);
        rig.send_control(ControlEvent::FlashSyncMatched { probe_id: id, reply: tx });
        rx.recv_timeout(WAIT).expect("FlashSyncMatched 回覆")
    }

    /// `FlashProbeReleased`（無回覆；以一次同步 RPC 確認 actor 已處理）。
    pub fn released(rig: &ActorRig, id: ProbeId) {
        rig.send_control(ControlEvent::FlashProbeReleased { probe_id: id });
        let _ = rig.get_state();
    }

    pub fn holder_gone(rig: &ActorRig, id: ProbeId) {
        let (tx, rx) = bounded(1);
        rig.send_control(ControlEvent::FlashHolderGone { probe_id: id, reply: tx });
        rx.recv_timeout(WAIT).expect("FlashHolderGone 回覆");
    }

    /// passthrough ATTACHED → Probe(id) → Matched → FLASHING；回 probe 授權。
    pub fn flashing(hub: bool, id: ProbeId) -> (ActorRig, ProbeGrant) {
        let rig = attached(hub);
        let grant = probe_start(&rig, id).expect("probe 應授權");
        matched(&rig, id).expect("同一 probe 應可 Matched");
        rig.wait_state("FLASHING");
        (rig, grant)
    }

    /// 以測試替身 coordinator 身分經現行 bridge 的 TxWriter 送一筆 TX（`source`＝"flash"）。
    pub fn tx(rig: &ActorRig, epoch: u64, class: TxClass, probe_id: Option<u64>, payload: &[u8]) -> TxReport {
        let (reply, rx) = bounded(1);
        let op = NEXT_OP.fetch_add(1, Ordering::SeqCst);
        rig.tx_handle()
            .jobs
            .send(TxJob {
                permit: TxPermit {
                    op_id: OpId(op),
                    epoch,
                    owner_gen: rig.fence.with(|f| f.owner_gen),
                    fsm_gen: rig.fence.with(|f| f.fsm_gen),
                    class,
                    source: "flash".into(),
                    cmd_id: None,
                    log: true,
                    input_epoch: None,
                    replay_hold: None,
                    probe_id,
                },
                payload: payload.to_vec(),
                reply,
            })
            .expect("TxWriter 接收 job");
        rx.recv_timeout(WAIT).expect("TxReport")
    }

    pub fn flash_mode(rig: &ActorRig) -> FlashMode {
        rig.fence.with(|f| f.flash_mode)
    }

    pub fn state(rig: &ActorRig) -> String {
        rig.get_state().get("state").and_then(PyValue::as_str).unwrap_or("").to_string()
    }

    /// WAL TX 列（source, payload）。
    pub fn wal_tx(rig: &ActorRig) -> Vec<(String, Vec<u8>)> {
        rig.wal_rows()
            .iter()
            .filter(|r| dir_of(r) == Some("TX"))
            .map(|r| {
                (
                    r.get("source").and_then(PyValue::as_str).unwrap_or("").to_string(),
                    payload_of(r),
                )
            })
            .collect()
    }

    /// 給 hub／TX pump／OpRunner 一段真實時間消化（零 TX 斷言前的穩定窗）。
    pub fn settle(rig: &ActorRig) {
        std::thread::sleep(Duration::from_millis(80));
        let _ = rig.get_state();
        std::thread::sleep(Duration::from_millis(20));
    }

    /// 等 DUT 收到 `want`（相對 `from` 偏移之後的序列包含它）。
    pub fn wait_dut_contains(rig: &ActorRig, from: usize, want: &[u8]) {
        let deadline = Instant::now() + WAIT;
        loop {
            let got = rig.dev.tx_since(from);
            if got.windows(want.len()).any(|w| w == want) {
                return;
            }
            assert!(Instant::now() < deadline, "DUT 未收到 {want:?}（實際 {got:?}）");
            std::thread::sleep(Duration::from_millis(2));
        }
    }

    /// 反覆鍵入 `key` 直到 DUT 收到（上限 20 次）：上下文切換（input_epoch 前進）前取的 hub 快照所帶的輸入依 P6
    /// D-02 以 stale_epoch 丟棄，只有之後的按鍵會送達。
    pub fn type_until_dut_receives(
        rig: &ActorRig,
        console: &sw_pal::fakes::console::FakeConsoleRef,
        from: usize,
        key: &[u8],
    ) {
        for _ in 0..20 {
            console.push_input(key);
            settle(rig);
            if rig.dev.tx_since(from).windows(key.len()).any(|w| w == key) {
                return;
            }
        }
        panic!("DUT 未收到 {key:?}：stderr={:?}", rig.stderr_lines());
    }

    pub fn request(rig: &ActorRig, req: SessionRequest) -> PyDict {
        rig.request(req)
    }
}
