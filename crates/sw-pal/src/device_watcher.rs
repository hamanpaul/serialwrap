//! DeviceWatcher（arch §4.3，P5-T5）。thread 名 `serialwrap-device-watcher`。
use std::any::Any;
use std::collections::BTreeMap;
use std::panic::{catch_unwind, AssertUnwindSafe};
use std::sync::Arc;
use std::thread::{self, JoinHandle};
use std::time::Duration;

use crossbeam_channel::{bounded, select, Receiver, RecvTimeoutError, Sender};

use crate::traits::{Clock, DeviceInfo, DeviceSource, PalResult, WatcherEvent};

/// 裝置輪詢 watcher（arch §4.3）。
pub struct DeviceWatcher;

/// watcher thread 的控制柄。
pub struct WatcherHandle {
    stop_tx: Sender<()>,
    done_rx: Receiver<()>,
    thread: Option<JoinHandle<()>>,
}

const STOP_TIMEOUT: Duration = Duration::from_secs(2);

impl DeviceWatcher {
    /// 未提供既有快照時，以空集合為基線啟動 watcher。
    pub fn start(
        src: Box<dyn DeviceSource>,
        poll: Duration,
        tx: Sender<WatcherEvent>,
        clock: Arc<dyn Clock>,
    ) -> WatcherHandle {
        Self::start_with_baseline(src, poll, tx, clock, Vec::new())
    }

    /// 以呼叫端已完成 rank 的初始掃描快照作為基線（DF-14）。
    pub fn start_with_baseline(
        mut src: Box<dyn DeviceSource>,
        poll: Duration,
        tx: Sender<WatcherEvent>,
        clock: Arc<dyn Clock>,
        baseline: Vec<(String, DeviceInfo)>,
    ) -> WatcherHandle {
        let (stop_tx, stop_rx) = bounded(1);
        let (done_tx, done_rx) = bounded(1);
        let thread = thread::Builder::new()
            .name("serialwrap-device-watcher".into())
            .spawn(move || {
                let mut previous = baseline;
                let poll_stop_rx = stop_rx.clone();
                loop {
                    if !poll_stop_rx.is_empty() {
                        break;
                    }

                    let result = catch_unwind(AssertUnwindSafe(|| {
                        Self::poll_once(src.as_mut(), &mut previous, &tx, &poll_stop_rx)
                    }));
                    match result {
                        Ok(true) => {}
                        Ok(false) => break,
                        Err(error) => eprintln!(
                            "WARNING device watcher tick 例外，略過本輪：{}",
                            panic_message(error.as_ref())
                        ),
                    }

                    let poll_ns = poll.as_nanos().min(u64::MAX as u128) as u64;
                    let deadline = clock.mono_ns().saturating_add(poll_ns);
                    if !clock.wait_until_or_cancel(deadline, &poll_stop_rx) {
                        break;
                    }
                }
                let _ = done_tx.send(());
            })
            .expect("無法建立 serialwrap-device-watcher thread");

        WatcherHandle {
            stop_tx,
            done_rx,
            thread: Some(thread),
        }
    }

    pub fn initial_scan(src: &mut dyn DeviceSource) -> PalResult<Vec<(String, DeviceInfo)>> {
        src.scan()
    }

    /// 差異演算法照 07 §1.4：`added` 先 sorted(cur−prev)、`changed` 的 cur 接在後；`removed` 先 sorted(prev−cur)、`changed` 的 prev 接在後。
    pub fn diff(
        prev: &[(String, DeviceInfo)],
        cur: &[(String, DeviceInfo)],
    ) -> (Vec<DeviceInfo>, Vec<DeviceInfo>) {
        let prev_by_id: BTreeMap<&str, &DeviceInfo> = prev
            .iter()
            .map(|(by_id, info)| (by_id.as_str(), info))
            .collect();
        let cur_by_id: BTreeMap<&str, &DeviceInfo> = cur
            .iter()
            .map(|(by_id, info)| (by_id.as_str(), info))
            .collect();

        let mut added = Vec::new();
        let mut changed_added = Vec::new();
        for (by_id, info) in &cur_by_id {
            match prev_by_id.get(*by_id) {
                None => added.push((*info).clone()),
                Some(old) if old.real_path != info.real_path => changed_added.push((*info).clone()),
                Some(_) => {}
            }
        }

        let mut removed = Vec::new();
        let mut changed_removed = Vec::new();
        for (by_id, info) in &prev_by_id {
            match cur_by_id.get(*by_id) {
                None => removed.push((*info).clone()),
                Some(new) if new.real_path != info.real_path => {
                    changed_removed.push((*info).clone())
                }
                Some(_) => {}
            }
        }

        added.extend(changed_added);
        removed.extend(changed_removed);
        (added, removed)
    }

    fn poll_once(
        src: &mut dyn DeviceSource,
        previous: &mut Vec<(String, DeviceInfo)>,
        tx: &Sender<WatcherEvent>,
        stop_rx: &Receiver<()>,
    ) -> bool {
        let current = match src.scan() {
            Ok(current) => current,
            Err(error) => {
                eprintln!(
                    "WARNING device watcher tick 失敗，略過本輪：{}",
                    error.py_message
                );
                return send_or_stop(tx, stop_rx, WatcherEvent::ScanFailed(error));
            }
        };

        let (added, removed) = Self::diff(previous, &current);
        *previous = current.clone();
        if (!added.is_empty() || !removed.is_empty())
            && !send_or_stop(
                tx,
                stop_rx,
                WatcherEvent::Delta {
                    added,
                    removed,
                    snapshot: current,
                },
            )
        {
            return false;
        }
        send_or_stop(tx, stop_rx, WatcherEvent::Tick)
    }
}

fn panic_message(error: &(dyn Any + Send)) -> &str {
    if let Some(message) = error.downcast_ref::<&str>() {
        message
    } else if let Some(message) = error.downcast_ref::<String>() {
        message
    } else {
        "非字串 panic payload"
    }
}

fn send_or_stop(tx: &Sender<WatcherEvent>, stop_rx: &Receiver<()>, event: WatcherEvent) -> bool {
    select! {
        recv(stop_rx) -> _ => false,
        send(tx, event) -> result => result.is_ok(),
    }
}

impl WatcherHandle {
    pub fn stop(mut self) {
        self.stop_and_join();
    }

    fn stop_and_join(&mut self) {
        let _ = self.stop_tx.try_send(());
        if let Some(thread) = self.thread.take() {
            match self.done_rx.recv_timeout(STOP_TIMEOUT) {
                Ok(()) | Err(RecvTimeoutError::Disconnected) => {
                    let _ = thread.join();
                }
                Err(RecvTimeoutError::Timeout) => {
                    eprintln!("WARNING device watcher 未於 2s 內結束");
                    drop(thread);
                }
            }
        }
    }
}

impl Drop for WatcherHandle {
    fn drop(&mut self) {
        self.stop_and_join();
    }
}
