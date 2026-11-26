//! Human TX lane（P6 Task 6；07 D-15、D-22）。
//!
//! 每 session 同時最多一筆 human TX（HumanRaw 或 HumanReplay）未結算；後續依序排在 backlog（上限
//! `HUMAN_TX_BACKLOG_MAX_BYTES`）。actor 對 pump 只 `try_send`（容量 1），pump thread 可阻塞送 TxWriter。

use std::collections::VecDeque;
use std::thread::JoinHandle;

use crossbeam_channel::{bounded, Sender};
use sw_pal::traits::{TxOutcome, TxResult};
use sw_proto::constants::HUMAN_TX_BACKLOG_MAX_BYTES;

use crate::mailbox::ControlEvent;
use crate::tx_writer::{TxClass, TxJob, TxPermit, TxReport, TxWriterHandle};
use crate::uart_bridge::{HumanInFlightGuard, RxShared};

/// 一筆 human TX job 的識別（結算時以 `HumanTxCompleted { tag, .. }` 帶回）。
#[derive(Debug, Clone, PartialEq, Eq)]
pub struct HumanTxTag {
    pub seq: u64,
    pub client_id: String,
    pub client_lifetime: u64,
    pub input_epoch: u64,
    pub class: TxClass,
    pub len: usize,
}

/// backlog 項目的種類。
#[derive(Debug, Clone, PartialEq, Eq)]
pub enum BacklogKind {
    /// 尚未路由的 console 輸入（07 D-15(2)：同 client 有未結算 job 或 backlog 時排在尾端，結算後再路由）。
    Input,
    /// 回放保留中的 HumanReplay（07 D-22）；`hold` 為所屬 `ReplayHold.hold_id`。
    Replay { hold: u64 },
}

/// backlog 中尚未送出的一筆（07 D-15(2)）。`tag.input_epoch` 為擷取當下的值。
#[derive(Debug, Clone)]
pub struct BacklogItem {
    pub tag: HumanTxTag,
    pub payload: Vec<u8>,
    pub kind: BacklogKind,
}

/// 在途的一筆 human TX。
#[derive(Debug, Clone)]
pub struct InFlightJob {
    pub tag: HumanTxTag,
    pub payload: Vec<u8>,
    /// HumanReplay 所屬的 hold。
    pub hold: Option<u64>,
    /// permit 的 `(op_id, epoch)`（Phase 1 後晚到結算時結算帳本用）。
    pub ledger: (crate::tx_writer::OpId, u64),
}

/// 每 session 一條 human TX lane 的簿記。
#[derive(Debug, Default)]
pub struct HumanLane {
    in_flight: Option<InFlightJob>,
    backlog: VecDeque<BacklogItem>,
    backlog_bytes: usize,
    next_seq: u64,
    /// drain Phase 1 `reset` 時仍在途那筆的 `(seq, op_id, epoch)`：晚到結算仍須結算舊帳本（D-15(6)）。
    orphan: Option<(u64, crate::tx_writer::OpId, u64)>,
}

impl HumanLane {
    /// in_flight 或 backlog 中有該 client。
    pub fn is_busy_for(&self, client_id: &str) -> bool {
        self.in_flight.as_ref().is_some_and(|j| j.tag.client_id == client_id)
            || self.backlog.iter().any(|b| b.tag.client_id == client_id)
    }

    pub fn in_flight(&self) -> Option<&HumanTxTag> {
        self.in_flight.as_ref().map(|j| &j.tag)
    }

    /// 在途 job 的完整資料。
    pub fn in_flight_job(&self) -> Option<&InFlightJob> {
        self.in_flight.as_ref()
    }

    /// 下一個 job 序號（`HumanTxTag.seq`）。
    pub fn next_seq(&mut self) -> u64 {
        self.next_seq = self.next_seq.saturating_add(1);
        self.next_seq
    }

    /// 設定在途 job；呼叫端保證目前沒有在途者（D-15(1)）。
    pub fn set_in_flight(&mut self, job: InFlightJob) {
        debug_assert!(self.in_flight.is_none(), "human lane 同時只能有一筆在途");
        self.in_flight = Some(job);
    }

    /// 結算：只有 `seq` 相符者才取出（舊 epoch 的晚到結算被忽略）。
    pub fn take_in_flight(&mut self, seq: u64) -> Option<InFlightJob> {
        if self.in_flight.as_ref().is_some_and(|j| j.tag.seq == seq) {
            self.in_flight.take()
        } else {
            None
        }
    }

    /// 排入 backlog 尾端；超過 `HUMAN_TX_BACKLOG_MAX_BYTES` 時清空該 client 的 backlog（連同本筆），
    /// 回 `Err(被丟棄的項目)`（呼叫端依其中的回放結算 hold）。
    pub fn push_backlog(&mut self, item: BacklogItem) -> Result<(), Vec<BacklogItem>> {
        if self.backlog_bytes + item.payload.len() > HUMAN_TX_BACKLOG_MAX_BYTES {
            let cid = item.tag.client_id.clone();
            let mut removed = self.remove_client(&cid);
            removed.push(item);
            return Err(removed);
        }
        self.backlog_bytes += item.payload.len();
        self.backlog.push_back(item);
        Ok(())
    }

    /// Phase 1 前在途那筆的晚到結算：`seq` 相符時取出 `(op_id, epoch)`。
    pub fn take_orphan(&mut self, seq: u64) -> Option<(crate::tx_writer::OpId, u64)> {
        match self.orphan {
            Some((s, op_id, epoch)) if s == seq => {
                self.orphan = None;
                Some((op_id, epoch))
            }
            _ => None,
        }
    }

    pub fn pop_front(&mut self) -> Option<BacklogItem> {
        let item = self.backlog.pop_front()?;
        self.backlog_bytes -= item.payload.len();
        Some(item)
    }

    pub fn backlog_is_empty(&self) -> bool {
        self.backlog.is_empty()
    }

    pub fn backlog_front(&self) -> Option<&BacklogItem> {
        self.backlog.front()
    }

    /// 取出（依原順序）屬於某 hold 的回放項目（D-22(4) 撤銷時放回 deferred 前端）。
    pub fn take_replays_of(&mut self, hold: u64) -> Vec<BacklogItem> {
        let mut out = Vec::new();
        let mut keep = VecDeque::with_capacity(self.backlog.len());
        for item in self.backlog.drain(..) {
            if item.kind == (BacklogKind::Replay { hold }) {
                self.backlog_bytes -= item.payload.len();
                out.push(item);
            } else {
                keep.push_back(item);
            }
        }
        self.backlog = keep;
        out
    }

    /// 丟棄 `input_epoch` 不等於現值的 backlog 項目（BC-02 失效）；回傳被丟棄的項目。
    pub fn drop_stale(&mut self, current_epoch: u64) -> Vec<BacklogItem> {
        let mut out = Vec::new();
        let mut keep = VecDeque::with_capacity(self.backlog.len());
        for item in self.backlog.drain(..) {
            if item.tag.input_epoch == current_epoch {
                keep.push_back(item);
            } else {
                self.backlog_bytes -= item.payload.len();
                out.push(item);
            }
        }
        self.backlog = keep;
        out
    }

    /// 清掉該 client 的 backlog（detach／drop／溢位）；回傳被丟棄的項目（依原順序）。
    pub fn remove_client(&mut self, client_id: &str) -> Vec<BacklogItem> {
        let mut out = Vec::new();
        let mut keep = VecDeque::with_capacity(self.backlog.len());
        for item in self.backlog.drain(..) {
            if item.tag.client_id == client_id {
                self.backlog_bytes -= item.payload.len();
                out.push(item);
            } else {
                keep.push_back(item);
            }
        }
        self.backlog = keep;
        out
    }

    /// drain Phase 1：清空在途簿記與 backlog；在途那筆只留帳本身分（`take_orphan`），回傳被丟棄的 backlog 項目。
    pub fn reset(&mut self) -> Vec<BacklogItem> {
        self.orphan = self
            .in_flight
            .take()
            .map(|job| (job.tag.seq, job.ledger.0, job.ledger.1));
        self.backlog_bytes = 0;
        self.backlog.drain(..).collect()
    }
}

/// pump 容量 1 的 channel 已有一筆未取走。
#[derive(Debug, Clone, PartialEq, Eq)]
pub struct PumpFull;

type PumpItem = (HumanTxTag, TxPermit, Vec<u8>, HumanInFlightGuard);

/// per-session human TX pump thread（`serialwrap-human-tx-<COM>`）。
///
/// actor 只 `try_submit`（容量 1，永不阻塞）；pump 對 `TxWriterHandle.jobs` 阻塞送出、等 `TxReport`，
/// 再以 control lane 回 `HumanTxCompleted`。TxWriter 已結束（`Disconnected`／writer 收尾）時回 `NotSubmitted`。
pub struct HumanTxPump {
    tx: Sender<PumpItem>,
    rx: std::sync::Arc<RxShared>,
    join: Option<JoinHandle<()>>,
}

fn not_submitted(permit: &TxPermit) -> TxReport {
    TxReport {
        op_id: permit.op_id,
        epoch: permit.epoch,
        result: TxResult {
            outcome: TxOutcome::NotSubmitted,
            error: None,
        },
        wal: None,
        reject: None,
    }
}

impl HumanTxPump {
    pub fn spawn(
        com: &str,
        tx: TxWriterHandle,
        rx: std::sync::Arc<RxShared>,
        control: Sender<ControlEvent>,
    ) -> HumanTxPump {
        let (sender, receiver) = bounded::<PumpItem>(1);
        let pump_rx = std::sync::Arc::clone(&rx);
        let join = std::thread::Builder::new()
            .name(format!("serialwrap-human-tx-{com}"))
            .spawn(move || {
                loop {
                    #[cfg(feature = "test-fakes")]
                    rx.human_pump_before_take_for_test();
                    let Ok((tag, permit, payload, mut in_flight)) = receiver.recv() else {
                        break;
                    };
                    #[cfg(feature = "test-fakes")]
                    rx.human_pump_after_take_for_test();
                    let (reply, report_rx) = bounded::<TxReport>(1);
                    let job = TxJob {
                        permit: permit.clone(),
                        payload,
                        reply,
                        aggregate: None,
                    };
                    let queued = rx.with_human_submit_gate(&mut in_flight, || {
                        tx.jobs.send(job).map_err(|_| ())
                    });
                    let report = match queued {
                        Ok(()) => tx
                            .wait_report(&report_rx)
                            .unwrap_or_else(|| not_submitted(&permit)),
                        Err(_) => not_submitted(&permit),
                    };
                    drop(in_flight);
                    if control
                        .send(ControlEvent::HumanTxCompleted { tag, report })
                        .is_err()
                    {
                        break;
                    }
                }
            })
            .expect("spawn human tx pump");
        HumanTxPump {
            tx: sender,
            rx: pump_rx,
            join: Some(join),
        }
    }

    /// bounded(1)；actor 永不阻塞。
    pub fn try_submit(
        &self,
        tag: HumanTxTag,
        permit: TxPermit,
        payload: Vec<u8>,
        in_flight: HumanInFlightGuard,
    ) -> Result<(), PumpFull> {
        let sent = self.tx.try_send((tag, permit, payload, in_flight));
        if sent.is_ok() {
            #[cfg(feature = "test-fakes")]
            self.rx.note_human_pump_submission_for_test();
        }
        sent.map_err(|_| PumpFull)
    }

    /// 關閉入口；pump 送完手上那筆後結束（不阻塞呼叫端）。
    pub fn stop(mut self) {
        drop(self.join.take());
    }
}

#[cfg(all(test, feature = "test-fakes"))]
mod tests {
    use std::sync::Arc;
    use std::time::{Duration, Instant};

    use crossbeam_channel::bounded;
    use super::{HumanTxPump, HumanTxTag};
    use crate::tx_writer::{
        Fence, LedgerGaugeCells, OpId, TxClass, TxJob, TxPermit, TxWriterHandle,
    };
    use crate::uart_bridge::RxShared;

    fn tag(seq: u64) -> HumanTxTag {
        HumanTxTag {
            seq,
            client_id: "i294-r12".into(),
            client_lifetime: 1,
            input_epoch: 0,
            class: TxClass::HumanRaw,
            len: 1,
        }
    }

    fn permit(seq: u64) -> TxPermit {
        TxPermit {
            op_id: OpId(seq),
            epoch: 0,
            owner_gen: 0,
            fsm_gen: 0,
            class: TxClass::HumanRaw,
            source: "human:i294-r12".into(),
            cmd_id: None,
            log: false,
            input_epoch: Some(0),
            replay_hold: None,
            probe_id: None,
        }
    }

    #[test]
    fn i294_r12_pump_full_and_writer_channel_closed_drop_guards() {
        let rx = RxShared::new(1024, 0);
        let (pause_tx, pause_rx) = bounded(1);
        let (resume_tx, resume_rx) = bounded(1);
        rx.set_human_pump_before_take_hook_for_test(Some(Arc::new(move || {
            pause_tx.send(()).expect("pump 到達取件屏障");
            resume_rx.recv().expect("釋放 pump 取件屏障");
        })));

        let (writer_jobs, writer_receiver) = bounded::<TxJob>(1);
        drop(writer_receiver);
        let (settle, _settle_receiver) = bounded(1);
        let writer = TxWriterHandle::new(
            writer_jobs,
            settle,
            Arc::new(LedgerGaugeCells::default()),
            Fence::new(),
        );
        let (control, control_receiver) = bounded(1);
        drop(control_receiver);
        let mut pump = HumanTxPump::spawn("i294-r12", writer, Arc::clone(&rx), control);
        pause_rx
            .recv_timeout(Duration::from_secs(5))
            .expect("pump 尚未取件");

        assert!(pump
            .try_submit(tag(1), permit(1), b"a".to_vec(), rx.begin_human_submit())
            .is_ok());
        assert_eq!(rx.human_in_flight_for_test(), 1);
        assert!(pump
            .try_submit(tag(2), permit(2), b"b".to_vec(), rx.begin_human_submit())
            .is_err());
        assert_eq!(rx.human_in_flight_for_test(), 1, "channel 滿載時拒絕的 guard 已釋放");

        resume_tx.send(()).expect("釋放 pump");
        let deadline = Instant::now() + Duration::from_secs(5);
        while rx.human_in_flight_for_test() != 0 {
            assert!(Instant::now() < deadline, "writer channel 關閉後 guard 未歸零");
            std::thread::yield_now();
        }
        assert_eq!(rx.human_in_flight_for_test(), 0, "writer channel 關閉時 pump 已釋放 guard");

        let join = pump.join.take().expect("pump thread");
        drop(pump);
        join.join().expect("channel 關閉後 pump 正常結束");
    }
}
