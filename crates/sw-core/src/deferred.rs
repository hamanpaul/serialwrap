//! DeferredBuffer（P6 Task 4；BC-02、07 D-02／D-07／D-15／D-22）。
//!
//! 型別為 `pub` 供 L1 測試直接驗證；產品中只作為 `SessionCore` 的私有欄位存在，
//! 其他 Phase 只能經 `SessionCore::invalidate_human_input` 影響它（07 D-02）。
//!
//! 溢位語意逐字照 P1 T09 規則 1（07 D-07）：某 client 的 entry 附加後長度 > cap 就刪除該 entry（連同本次 bytes），
//! 之後同一 client 的 bytes 開新 entry 照常保留（沒有 poison 狀態）。回放只送 `input_epoch` 等於現值、
//! 且 `client_id`＋`client_lifetime` 仍存活的非空 entry（規則 4）。

use indexmap::IndexMap;

use crate::tx_writer::SecretTx;

/// 單一 client 的 deferred 位元組（以 `client_id` 為 `IndexMap` key，保持首次插入序）。
#[derive(Debug, Clone, PartialEq, Eq)]
pub struct DeferredEntry {
    pub client_lifetime: u64,
    pub input_epoch: u64,
    pub bytes: Vec<u8>,
    spans: Vec<DeferredSpan>,
}

#[derive(Debug, Clone, PartialEq, Eq)]
struct DeferredSpan {
    /// active secret 的每次 append 各自保留 actor 邊界；相同 secret snapshot 不得跨行合併。
    start: usize,
    len: usize,
    secret_tx: Option<SecretTx>,
}

/// `append` 的結果。
#[derive(Debug, Clone, PartialEq, Eq)]
pub enum DeferOutcome {
    /// 附加成功；`len` 為該 entry 附加後的長度。
    Appended { len: usize },
    /// 擷取時的 `input_epoch` 不等於現值：不進 buffer。
    StaleEpoch,
    /// 附加後超過上限：刪除整個 entry（連同本次 bytes）；`dropped` 為刪除的 bytes 數（07 D-07）。
    Overflowed { dropped: usize },
}

/// BC-02 失效點（07 D-02）。
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum InvalidateReason {
    BootBanner,
    QuietRearm,
    RebootCommand,
    Recovery,
    Release,
    Flash,
}

impl InvalidateReason {
    /// WARNING 模板用的 reason：`boot_banner`｜`quiet_rearm`｜`reboot_command`｜`recovery`｜`release`｜`flash`。
    pub fn as_str(self) -> &'static str {
        match self {
            InvalidateReason::BootBanner => "boot_banner",
            InvalidateReason::QuietRearm => "quiet_rearm",
            InvalidateReason::RebootCommand => "reboot_command",
            InvalidateReason::Recovery => "recovery",
            InvalidateReason::Release => "release",
            InvalidateReason::Flash => "flash",
        }
    }
}

/// 一筆待回放的 client 輸入（`source` 為 `"human:<client_id>"`）。
#[derive(Debug, Clone, PartialEq, Eq)]
pub struct Replay {
    pub client_id: String,
    pub client_lifetime: u64,
    pub source: String,
    pub bytes: Vec<u8>,
}

/// 產品回放使用的內部分段；每段保留 actor 接收當下的判定。
pub(crate) struct ClassifiedReplay {
    pub replay: Replay,
    pub secret_tx: Option<SecretTx>,
}

/// 每 client 有上限的 deferred 輸入帳本。
#[derive(Debug)]
pub struct DeferredBuffer {
    entries: IndexMap<String, DeferredEntry>,
    cap: usize,
}

fn replay_of(client_id: String, e: DeferredEntry) -> Replay {
    Replay {
        source: format!("human:{client_id}"),
        client_lifetime: e.client_lifetime,
        client_id,
        bytes: e.bytes,
    }
}

fn classified_replay_of(client_id: String, e: DeferredEntry) -> Vec<ClassifiedReplay> {
    let source = format!("human:{client_id}");
    e.spans
        .into_iter()
        .map(|span| ClassifiedReplay {
            replay: Replay {
                source: source.clone(),
                client_lifetime: e.client_lifetime,
                client_id: client_id.clone(),
                bytes: e.bytes[span.start..span.start + span.len].to_vec(),
            },
            secret_tx: span.secret_tx,
        })
        .collect()
}

fn classified_spans_can_merge(
    previous: &Option<SecretTx>,
    incoming: &Option<SecretTx>,
) -> bool {
    previous == incoming
        && !incoming.as_ref().is_some_and(|secret| {
            secret
                .window_snapshot
                .is_some_and(|snapshot| snapshot.generation().is_some())
        })
}

impl DeferredBuffer {
    pub fn new(cap: usize) -> Self {
        DeferredBuffer { entries: IndexMap::new(), cap }
    }

    /// `input_epoch` 為擷取當下的值，`current_epoch` 為現值；不等即 `StaleEpoch`。
    pub fn append(
        &mut self,
        client_id: &str,
        client_lifetime: u64,
        input_epoch: u64,
        current_epoch: u64,
        bytes: &[u8],
    ) -> DeferOutcome {
        self.append_with_secret(
            client_id,
            client_lifetime,
            input_epoch,
            current_epoch,
            bytes,
            None,
        )
    }

    pub(crate) fn append_with_secret(
        &mut self,
        client_id: &str,
        client_lifetime: u64,
        input_epoch: u64,
        current_epoch: u64,
        bytes: &[u8],
        secret_tx: Option<SecretTx>,
    ) -> DeferOutcome {
        if input_epoch != current_epoch {
            return DeferOutcome::StaleEpoch;
        }
        let e = self
            .entries
            .entry(client_id.to_string())
            .or_insert_with(|| DeferredEntry {
                client_lifetime,
                input_epoch,
                bytes: Vec::new(),
                spans: Vec::new(),
            });
        if e.client_lifetime != client_lifetime || e.input_epoch != input_epoch {
            // 同 id 新生命週期或新上下文：舊內容作廢。
            *e = DeferredEntry {
                client_lifetime,
                input_epoch,
                bytes: Vec::new(),
                spans: Vec::new(),
            };
        }
        let merged = e.bytes.len() + bytes.len();
        if merged > self.cap {
            self.entries.shift_remove(client_id); // P1 T09 規則 1：刪除整個 entry
            return DeferOutcome::Overflowed { dropped: merged };
        }
        if bytes.is_empty() {
            return DeferOutcome::Appended { len: e.bytes.len() };
        }
        if let Some(last) = e
            .spans
            .last_mut()
            .filter(|span| classified_spans_can_merge(&span.secret_tx, &secret_tx))
        {
            last.len = last.len.saturating_add(bytes.len());
        } else {
            e.spans.push(DeferredSpan {
                start: e.bytes.len(),
                len: bytes.len(),
                secret_tx,
            });
        }
        e.bytes.extend_from_slice(bytes);
        DeferOutcome::Appended { len: e.bytes.len() }
    }

    /// 清空全部 entry；回傳丟棄的 bytes 總數。
    pub fn invalidate_all(&mut self) -> usize {
        let n = self.total_bytes();
        self.entries.clear();
        n
    }

    /// detach／drop／reap／lease_dropped：刪除該 client 的 entry；回傳丟棄 bytes 數。
    pub fn remove_client(&mut self, client_id: &str) -> usize {
        self.entries.shift_remove(client_id).map_or(0, |e| e.bytes.len())
    }

    /// 依首次插入序取出全部可回放項目並清空所有 entry（只回 `input_epoch == current_epoch`、client 存活且非空者）。
    pub fn take_replay(&mut self, current_epoch: u64, is_live: &dyn Fn(&str, u64) -> bool) -> Vec<Replay> {
        let drained: Vec<(String, DeferredEntry)> = self.entries.drain(..).collect();
        drained
            .into_iter()
            .filter(|(cid, e)| !e.bytes.is_empty() && e.input_epoch == current_epoch && is_live(cid, e.client_lifetime))
            .map(|(cid, e)| replay_of(cid, e))
            .collect()
    }

    pub(crate) fn take_classified_replay(
        &mut self,
        current_epoch: u64,
        is_live: &dyn Fn(&str, u64) -> bool,
    ) -> Vec<ClassifiedReplay> {
        let drained: Vec<(String, DeferredEntry)> = self.entries.drain(..).collect();
        drained
            .into_iter()
            .filter(|(cid, e)| !e.bytes.is_empty() && e.input_epoch == current_epoch && is_live(cid, e.client_lifetime))
            .flat_map(|(cid, e)| classified_replay_of(cid, e))
            .collect()
    }

    /// 07 D-15(4)：只取出該 client 的 entry（不論可否回放都移除）；可回放時回 `Some`，其他 entry 順序不變。
    pub fn take_replay_for(
        &mut self,
        client_id: &str,
        current_epoch: u64,
        is_live: &dyn Fn(&str, u64) -> bool,
    ) -> Option<Replay> {
        let e = self.entries.shift_remove(client_id)?;
        if e.bytes.is_empty() || e.input_epoch != current_epoch || !is_live(client_id, e.client_lifetime) {
            return None;
        }
        Some(replay_of(client_id.to_string(), e))
    }

    pub(crate) fn take_classified_replay_for(
        &mut self,
        client_id: &str,
        current_epoch: u64,
        is_live: &dyn Fn(&str, u64) -> bool,
    ) -> Vec<ClassifiedReplay> {
        let Some(e) = self.entries.shift_remove(client_id) else {
            return Vec::new();
        };
        if e.bytes.is_empty() || e.input_epoch != current_epoch || !is_live(client_id, e.client_lifetime) {
            return Vec::new();
        }
        classified_replay_of(client_id.to_string(), e)
    }

    /// 07 D-15(3)／D-22(4)：被撤銷或 `NotSubmitted` 的回放放回前端。`items` 依原送出順序、各帶擷取時的 `input_epoch`；
    /// epoch 不等於 `current_epoch` 者丟棄；其餘依序排在所有既有 entry 之前，同 client 已有 entry 時 bytes 接在該 entry
    /// 前面並把 entry 移到該位置；合併後長度 > cap 依 D-07 刪除整個 entry。回傳丟棄的 bytes 總數。
    /// client 存活與 lifetime 由呼叫端先行判定；既有 entry 的 lifetime 或 epoch 與放回項目不同時，既有內容視為失效一併丟棄。
    pub fn restore_front(&mut self, items: Vec<(Replay, u64)>, current_epoch: u64) -> usize {
        self.restore_classified_front(
            items
                .into_iter()
                .map(|(replay, epoch)| {
                    (
                        ClassifiedReplay {
                            replay,
                            secret_tx: None,
                        },
                        epoch,
                    )
                })
                .collect(),
            current_epoch,
        )
    }

    pub(crate) fn restore_classified_front(
        &mut self,
        items: Vec<(ClassifiedReplay, u64)>,
        current_epoch: u64,
    ) -> usize {
        let mut dropped = 0usize;
        let mut front: IndexMap<String, DeferredEntry> = IndexMap::new();
        for (classified, epoch) in items {
            let ClassifiedReplay { replay: r, secret_tx } = classified;
            if epoch != current_epoch {
                dropped += r.bytes.len();
                continue;
            }
            let e = front.entry(r.client_id).or_insert_with(|| DeferredEntry {
                client_lifetime: r.client_lifetime,
                input_epoch: current_epoch,
                bytes: Vec::new(),
                spans: Vec::new(),
            });
            if e.client_lifetime != r.client_lifetime {
                dropped += e.bytes.len();
                *e = DeferredEntry {
                    client_lifetime: r.client_lifetime,
                    input_epoch: current_epoch,
                    bytes: Vec::new(),
                    spans: Vec::new(),
                };
            }
            let start = e.bytes.len();
            if let Some(last) = e
                .spans
                .last_mut()
                .filter(|span| classified_spans_can_merge(&span.secret_tx, &secret_tx))
            {
                last.len = last.len.saturating_add(r.bytes.len());
            } else {
                e.spans.push(DeferredSpan {
                    start,
                    len: r.bytes.len(),
                    secret_tx,
                });
            }
            e.bytes.extend_from_slice(&r.bytes);
        }
        let mut rebuilt: IndexMap<String, DeferredEntry> = IndexMap::new();
        for (cid, mut e) in front {
            if let Some(old) = self.entries.shift_remove(&cid) {
                if old.client_lifetime == e.client_lifetime && old.input_epoch == current_epoch {
                    let offset = e.bytes.len();
                    for span in old.spans {
                        if let Some(last) = e.spans.last_mut().filter(|current| {
                            classified_spans_can_merge(&current.secret_tx, &span.secret_tx)
                        }) {
                            last.len = last.len.saturating_add(span.len);
                        } else {
                            e.spans.push(DeferredSpan {
                                start: offset + span.start,
                                len: span.len,
                                secret_tx: span.secret_tx,
                            });
                        }
                    }
                    e.bytes.extend_from_slice(&old.bytes);
                } else {
                    dropped += old.bytes.len();
                }
            }
            if e.bytes.len() > self.cap {
                dropped += e.bytes.len(); // 07 D-07：刪除整個 entry
                continue;
            }
            rebuilt.insert(cid, e);
        }
        rebuilt.extend(self.entries.drain(..));
        self.entries = rebuilt;
        dropped
    }

    pub fn total_bytes(&self) -> usize {
        self.entries.values().map(|e| e.bytes.len()).sum()
    }

    pub fn len_for(&self, client_id: &str) -> usize {
        self.entries.get(client_id).map_or(0, |e| e.bytes.len())
    }
}
