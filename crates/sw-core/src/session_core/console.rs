//! P6 Task 6：`SessionCore` 的 human console 狀態機（07 D-02、D-03、D-15、D-19、D-22）。
//!
//! 與 06 `session_core.rs` 分檔以降低兩 Phase 的合併衝突；本檔是 `session_core` 的子模組，可直接操作
//! `SessionCore` 的私有欄位。所有 ownership、DeferredBuffer、human TX lane 與回放保留的決策都在這裡；
//! I/O 只以 `Effect` 輸出（`HumanTx`、`ConsoleOut`、`ConsoleClose*`、`ConsoleAdopt`、`Warn`）。

use std::sync::atomic::Ordering;

use sw_pal::traits::TxOutcome;
use sw_proto::pycompat::uuid4_hex;
use sw_proto::pydict;
use sw_proto::pyjson::PyValue;

use super::*;
use crate::arbiter::SubmitOrigin;
use crate::deferred::{DeferOutcome, Replay};
use crate::human_console::human_tx::{BacklogItem, BacklogKind, HumanTxTag, InFlightJob};
use crate::human_console::line_mode::human_console_mode;
use crate::human_console::ownership::InputRoute;
use crate::human_console::{ConsoleRecord, ConsoleScanSnapshot, ConsoleTransportKind, ReplayHold};
use crate::human_console::line_editor::LineEditor;
use crate::tx_writer::{TxClass, TxReport};

/// D-19 限速表上限（超過時整批清空；只影響 WARNING 次數）。
const DROP_WARN_KEYS_MAX: usize = 1024;

/// D-06：arbiter 拒絕時寫 WARNING 的錯誤碼集合。
const LINE_REJECT_WARN_CODES: [&str; 3] = ["CMD_TOO_LONG", "SESSION_QUEUE_FULL", "SESSION_NOT_READY"];

impl SessionCore {
    // ---------------- 公開入口（07 Task 6 Produces） ----------------

    /// 07 Task 6：admission 關；agent 且有 human lease 時 suspend；先撤銷 `replay_auth`（D-22(4)）。
    pub fn fg_begin(&mut self, source_is_agent: bool, fx: &mut Effects) -> FgAdmission {
        let _ = fx;
        self.revoke_replay();
        self.set_human_admission(false);
        let mut suspended_human = false;
        if source_is_agent && self.lease.as_ref().is_some_and(|l| l.is_human()) {
            let before = self.human.ownership.interactive_owner.clone();
            self.human.ownership.suspend();
            self.after_owner_change(before);
            suspended_human = true;
        }
        FgAdmission { suspended_human }
    }

    /// 07 Task 6：resume（若 suspend）→ admission 開；依序回放（建立 D-22 hold）。
    pub fn fg_finish(&mut self, adm: FgAdmission, fx: &mut Effects) {
        if adm.suspended_human {
            self.resume_human(&mut fx.0);
        }
        if self.set_human_admission(true) {
            self.replay_point(&mut fx.0);
        }
    }

    /// 07 Task 6：只 suspend（self-test、P8 file transfer）；先撤銷 `replay_auth`（D-22(4)）。
    pub fn human_suspend_begin(&mut self, fx: &mut Effects) -> bool {
        let _ = fx;
        self.revoke_replay();
        if !self.lease.as_ref().is_some_and(|l| l.is_human()) {
            return false;
        }
        let before = self.human.ownership.interactive_owner.clone();
        self.human.ownership.suspend();
        self.after_owner_change(before);
        true
    }

    /// 07 Task 6：回放時建立 D-22 hold。
    pub fn human_suspend_end(&mut self, suspended: bool, fx: &mut Effects) {
        if suspended {
            self.resume_human(&mut fx.0);
        }
    }

    /// 07 Task 6：BC-02 唯一入口（D-02）。遞增、fence 與 `input_epoch_pub` 在同一次 `Fence::with` 內完成。
    pub fn invalidate_human_input(&mut self, reason: InvalidateReason, fx: &mut Effects) {
        self.invalidate_input(reason, &mut fx.0);
    }

    /// human console 狀態（唯讀）。
    pub fn human_console(&self) -> &crate::human_console::HumanConsoleState {
        &self.human
    }

    /// 測試用：可變 human console 狀態（例如模擬 P8 的 `flash_mode`）。
    #[cfg(feature = "test-fakes")]
    pub fn human_console_mut(&mut self) -> &mut crate::human_console::HumanConsoleState {
        &mut self.human
    }

    /// 測試用：DeferredBuffer 附加（D-02：產品路徑不得直接操作 deferred）。
    #[cfg(feature = "test-fakes")]
    pub fn seed_deferred_for_test(&mut self, client_id: &str, bytes: &[u8]) {
        let lifetime = self
            .human
            .consoles
            .get(client_id)
            .map(|c| c.client_lifetime)
            .unwrap_or_default();
        let epoch = self.generations.input_epoch;
        let _ = self
            .human
            .deferred_mut()
            .append(client_id, lifetime, epoch, epoch, bytes);
    }

    /// drain Phase 1 時記下的 human lease owner（05 §3.1 第 1 步）。
    pub fn retained_human_owner(&self) -> Option<&str> {
        self.retained_human.as_ref().map(|(owner, _)| owner.as_str())
    }

    /// 增補 #26：`foreground.is_some()` 或存在未撤銷的 `replay_hold`（D-22(2)）。
    pub fn foreground_busy(&self) -> bool {
        self.foreground.is_some() || self.human.replay_hold.as_ref().is_some_and(|h| !h.revoked)
    }

    // ---------------- ownership 與 fence 同步 ----------------

    /// owner 實際改變時遞增 `owner_gen` 並在同一次 `Fence::with` 寫入（07 Task 6 Step 3 第 4 點）。
    pub(super) fn after_owner_change(&mut self, before: Option<String>) {
        if self.human.ownership.interactive_owner != before {
            self.bump_owner_gen();
        }
    }

    /// `set_interactive_owner` 的 core 入口；改為非 human owner 前先撤銷回放（D-22(4)，human 之間不撤銷 D-22(5)）。
    pub(super) fn human_set_owner(&mut self, owner: Option<&str>) {
        if owner.is_some_and(|o| !o.starts_with("human:")) {
            self.revoke_replay();
        }
        let before = self.human.ownership.interactive_owner.clone();
        self.human.ownership.set_interactive_owner(owner);
        self.after_owner_change(before);
    }

    /// admission 切換只寫 `fence.human_admission_blocked`，不動 `owner_gen`（D-02）；回 true＝此刻回放。
    fn set_human_admission(&mut self, allowed: bool) -> bool {
        let before = self.human.ownership.interactive_owner.clone();
        let crate::human_console::HumanConsoleState {
            consoles, ownership, ..
        } = &mut self.human;
        let replay = ownership.set_admission(allowed, &|c| consoles.contains_key(c));
        self.deps
            .fence
            .with(|f| f.human_admission_blocked = !allowed);
        // pending human promote 屬 human 之間的 owner 變更（不撤銷），仍依 §5.3 遞增 owner_gen。
        self.after_owner_change(before);
        replay
    }

    /// resume；最外層時回放。
    fn resume_human(&mut self, fx: &mut Vec<Effect>) {
        let before = self.human.ownership.interactive_owner.clone();
        let crate::human_console::HumanConsoleState {
            consoles, ownership, ..
        } = &mut self.human;
        let outer = ownership.resume(&|c| consoles.contains_key(c));
        self.after_owner_change(before);
        if outer {
            self.replay_point(fx);
        }
    }

    // ---------------- BC-02 失效（D-02） ----------------

    pub(super) fn invalidate_input(&mut self, reason: InvalidateReason, fx: &mut Vec<Effect>) {
        self.generations.input_epoch = self.generations.input_epoch.saturating_add(1);
        let epoch = self.generations.input_epoch;
        let publish = self.deps.input_epoch_pub.clone();
        self.deps.fence.with(|f| {
            f.input_epoch = epoch;
            publish.store(epoch, Ordering::Release);
        });
        let mut dropped = self.human.deferred_mut().invalidate_all();
        let stale = self.human.lane.drop_stale(epoch);
        let mut replays_dropped = 0usize;
        for item in &stale {
            dropped += item.payload.len();
            if matches!(item.kind, BacklogKind::Replay { .. }) {
                replays_dropped += 1;
            }
        }
        if replays_dropped > 0 {
            self.hold_settled(replays_dropped);
        }
        if dropped > 0 {
            fx.push(Effect::Warn(format!(
                "deferred input 因上下文失效清空（com={} reason={} dropped={dropped}）",
                self.profile.com,
                reason.as_str()
            )));
        }
    }

    // ---------------- 回放保留（D-22） ----------------

    /// 回放點：有 revoked 在途者時延後（D-22(4)），否則取出全部可回放項目並建立／併入 hold。
    pub(super) fn replay_point(&mut self, fx: &mut Vec<Effect>) {
        if self.human.replay_hold.as_ref().is_some_and(|h| h.revoked) {
            self.human.replay_after_revoked = true;
            return;
        }
        self.human.replay_after_revoked = false;
        let epoch = self.generations.input_epoch;
        let items = self.human.take_replay(epoch);
        self.submit_replays(items, fx);
    }

    /// 07 D-15(4)：只回放某 client（context 允許時）。
    fn replay_client(&mut self, client_id: &str, fx: &mut Vec<Effect>) {
        if self.human.replay_hold.as_ref().is_some_and(|h| h.revoked) {
            self.human.replay_after_revoked = true;
            return;
        }
        let epoch = self.generations.input_epoch;
        let item = self.human.take_replay_for(client_id, epoch);
        self.submit_replays(item.into_iter().collect(), fx);
    }

    fn submit_replays(&mut self, items: Vec<Replay>, fx: &mut Vec<Effect>) {
        if items.is_empty() {
            return;
        }
        let hold_id = match self.human.replay_hold.as_mut() {
            Some(hold) if !hold.revoked => {
                hold.remaining += items.len();
                hold.hold_id
            }
            _ => {
                self.human.next_hold_id = self.human.next_hold_id.saturating_add(1);
                let id = self.human.next_hold_id;
                self.human.replay_hold = Some(ReplayHold {
                    hold_id: id,
                    remaining: items.len(),
                    revoked: false,
                });
                self.deps.fence.with(|f| f.replay_auth = Some(id));
                id
            }
        };
        let epoch = self.generations.input_epoch;
        for item in items {
            self.lane_submit(
                &item.client_id,
                item.client_lifetime,
                epoch,
                TxClass::HumanReplay,
                Some(hold_id),
                item.bytes,
                fx,
            );
        }
    }

    /// D-22(4)：撤銷回放授權（同一次 `Fence::with`）；backlog 中屬該 hold 的回放立即 `restore_front`。
    pub(super) fn revoke_replay(&mut self) {
        let Some(hold_id) = self
            .human
            .replay_hold
            .as_ref()
            .filter(|h| !h.revoked)
            .map(|h| h.hold_id)
        else {
            return;
        };
        self.deps.fence.with(|f| f.replay_auth = None);
        if let Some(hold) = self.human.replay_hold.as_mut() {
            hold.revoked = true;
        }
        let pending = self.human.lane.take_replays_of(hold_id);
        let n = pending.len();
        self.restore_front(pending.into_iter().map(|b| (b.tag, b.payload)).collect());
        self.hold_settled(n);
    }

    /// hold 結算 `n` 筆；降到 0 時清 `replay_auth`（未撤銷者）與 hold（D-22(3)）。回 true＝hold 已釋放。
    fn hold_settled(&mut self, n: usize) -> bool {
        let Some(hold) = self.human.replay_hold.as_mut() else {
            return false;
        };
        hold.remaining = hold.remaining.saturating_sub(n);
        if hold.remaining > 0 {
            return false;
        }
        let (id, revoked) = (hold.hold_id, hold.revoked);
        self.human.replay_hold = None;
        if self.tick_close_hold == Some(id) {
            self.tick_close_hold = None;
        }
        self.deps.fence.with(|f| {
            if !revoked && f.replay_auth == Some(id) {
                f.replay_auth = None;
            }
        });
        true
    }

    fn restore_front(&mut self, items: Vec<(HumanTxTag, Vec<u8>)>) {
        if items.is_empty() {
            return;
        }
        let epoch = self.generations.input_epoch;
        let live: Vec<(Replay, u64)> = items
            .into_iter()
            .filter(|(tag, _)| self.human.is_live(&tag.client_id, tag.client_lifetime))
            .map(|(tag, bytes)| {
                (
                    Replay {
                        source: format!("human:{}", tag.client_id),
                        client_id: tag.client_id,
                        client_lifetime: tag.client_lifetime,
                        bytes,
                    },
                    tag.input_epoch,
                )
            })
            .collect();
        let _ = self.human.deferred_mut().restore_front(live, epoch);
    }

    // ---------------- human TX lane（D-15） ----------------

    /// 送出或排入 backlog（同時最多一筆在途）。
    #[allow(clippy::too_many_arguments)]
    fn lane_submit(
        &mut self,
        client_id: &str,
        lifetime: u64,
        input_epoch: u64,
        class: TxClass,
        hold: Option<u64>,
        payload: Vec<u8>,
        fx: &mut Vec<Effect>,
    ) {
        let seq = self.human.lane.next_seq();
        let tag = HumanTxTag {
            seq,
            client_id: client_id.to_string(),
            client_lifetime: lifetime,
            input_epoch,
            class,
            len: payload.len(),
        };
        if self.human.lane.in_flight().is_some() {
            let kind = match hold {
                Some(hold) => BacklogKind::Replay { hold },
                None => BacklogKind::Input,
            };
            self.push_backlog(BacklogItem { tag, payload, kind }, fx);
            return;
        }
        self.emit_human_tx(tag, hold, payload, fx);
    }

    fn push_backlog(&mut self, item: BacklogItem, fx: &mut Vec<Effect>) {
        let cid = item.tag.client_id.clone();
        if let Err(removed) = self.human.lane.push_backlog(item) {
            let dropped: usize = removed.iter().map(|b| b.payload.len()).sum();
            fx.push(Effect::Warn(format!(
                "human TX backlog 超過 262144 bytes，整段清空（com={} client_id={cid} dropped={dropped}）",
                self.profile.com
            )));
            self.settle_removed_replays(&removed);
        }
    }

    fn emit_human_tx(&mut self, tag: HumanTxTag, hold: Option<u64>, payload: Vec<u8>, fx: &mut Vec<Effect>) {
        self.next_op_id = self.next_op_id.saturating_add(1);
        let permit = TxPermit {
            op_id: OpId(self.next_op_id),
            epoch: self.generations.conn_epoch.unwrap_or_default(),
            owner_gen: self.generations.owner_gen,
            fsm_gen: self.generations.fsm_gen,
            class: tag.class,
            source: format!("human:{}", tag.client_id),
            cmd_id: None,
            log: true,
            input_epoch: Some(tag.input_epoch),
            replay_hold: hold,
        };
        self.human.lane.set_in_flight(InFlightJob {
            tag: tag.clone(),
            payload: payload.clone(),
            hold,
            ledger: (permit.op_id, permit.epoch),
        });
        fx.push(Effect::HumanTx { tag, permit, payload });
    }

    /// backlog 依序：Replay 直接送出、Input 重新路由；直到有在途者或 backlog 清空。
    fn pump_lane(&mut self, now: Mono, fx: &mut Vec<Effect>) {
        while self.human.lane.in_flight().is_none() {
            let Some(item) = self.human.lane.pop_front() else {
                return;
            };
            match item.kind {
                BacklogKind::Replay { hold } => self.emit_human_tx(item.tag, Some(hold), item.payload, fx),
                BacklogKind::Input => {
                    if item.tag.input_epoch != self.generations.input_epoch {
                        self.warn_drop(&item.tag.client_id, "stale_epoch", item.tag.input_epoch, item.payload.len(), fx);
                        continue;
                    }
                    if !self.human.is_live(&item.tag.client_id, item.tag.client_lifetime) {
                        continue;
                    }
                    self.route_console_input(
                        &item.tag.client_id,
                        item.tag.client_lifetime,
                        item.tag.input_epoch,
                        item.payload,
                        now,
                        fx,
                    );
                }
            }
        }
    }

    /// 延後回放（D-22(4)）可兌現的 context：前景空閒、admission 開、未 suspend。
    fn deferred_replay_context_open(&self) -> bool {
        let o = &self.human.ownership;
        self.foreground.is_none() && !o.admission_blocked && o.suspend_depth == 0 && o.suspended_owner.is_none()
    }

    /// D-15(4) 的「context 允許」：前景空閒且該 client 此刻會被路由為 Raw。
    fn immediate_replay_allowed(&self, client_id: &str) -> bool {
        self.foreground.is_none() && matches!(self.human.ownership.peek_route(client_id), InputRoute::Raw { .. })
    }

    /// `ControlEvent::HumanTxCompleted`：依 D-15(3) 結算、D-15(6) 結算帳本、D-22(3) 釋放 hold。
    pub(super) fn human_tx_completed(&mut self, tag: HumanTxTag, report: TxReport, now: Mono) -> Vec<Effect> {
        let mut fx = Vec::new();
        let Some(job) = self.human.lane.take_in_flight(tag.seq) else {
            // drain Phase 1 前在途者：只結算帳本（conn_epoch 仍相符時），不重新路由（D-15(6)）。
            if let Some((op_id, epoch)) = self.human.lane.take_orphan(tag.seq) {
                if self.generations.conn_epoch == Some(epoch) && report.op_id == op_id {
                    fx.push(Effect::SettleTx { epoch, op_id });
                }
            }
            return fx;
        };
        if self.generations.conn_epoch == Some(report.epoch) {
            fx.push(Effect::SettleTx {
                epoch: report.epoch,
                op_id: report.op_id,
            });
        }
        let cid = job.tag.client_id.clone();
        let len = job.tag.len;
        match report.result.outcome {
            TxOutcome::Completed(n) if n >= len => {}
            TxOutcome::Completed(n) => fx.push(self.tx_incomplete_warn(&cid, "partial", n, len)),
            TxOutcome::Unknown { confirmed } => fx.push(self.tx_incomplete_warn(&cid, "unknown", confirmed, len)),
            TxOutcome::NotSubmitted => {
                if job.tag.input_epoch != self.generations.input_epoch {
                    self.warn_drop(&cid, "stale_epoch", job.tag.input_epoch, len, &mut fx);
                } else if job.tag.class == TxClass::HumanReplay {
                    // D-15(3)／D-22(4)：回放（含 ReplayRevoked）放回前端；同一 hold 尚未送出的也一併放回。
                    let mut items = vec![(job.tag.clone(), job.payload.clone())];
                    if let Some(hold) = job.hold {
                        let rest = self.human.lane.take_replays_of(hold);
                        let n = rest.len();
                        items.extend(rest.into_iter().map(|b| (b.tag, b.payload)));
                        self.hold_settled(n);
                    }
                    self.restore_front(items);
                } else if self.human.is_live(&cid, job.tag.client_lifetime) {
                    // Python send_bytes 第三個 defer 點：仍有效的 HumanRaw 轉入 deferred。
                    let epoch = self.generations.input_epoch;
                    let outcome = self
                        .human
                        .deferred_mut()
                        .append(&cid, job.tag.client_lifetime, job.tag.input_epoch, epoch, &job.payload);
                    self.defer_outcome_warn(&cid, outcome, &mut fx);
                    if self.human.deferred_len_for(&cid) > 0 && self.immediate_replay_allowed(&cid) {
                        self.replay_client(&cid, &mut fx);
                    }
                }
            }
        }
        let released = job.hold.is_some() && self.hold_settled(1);
        self.pump_lane(now, &mut fx);
        if self.human.replay_after_revoked
            && !self.human.replay_hold.as_ref().is_some_and(|h| h.revoked)
            && self.deferred_replay_context_open()
        {
            // 延後的回放點只在前景空閒、admission 開且未 suspend 時兌現；否則留給下一個回放點
            // （該前景的 fg_finish／resume）處理，被撤銷者已 restore_front 不會遺失（D-22(4)）。
            self.replay_point(&mut fx);
        }
        if released && !self.foreground_busy() {
            fx.extend(self.start_next(now));
        }
        fx
    }

    fn tx_incomplete_warn(&self, cid: &str, outcome: &str, written: usize, len: usize) -> Effect {
        Effect::Warn(format!(
            "human TX 未完整確認，不重送（com={} client_id={cid} outcome={outcome} written={written} len={len}）",
            self.profile.com
        ))
    }

    fn defer_outcome_warn(&self, cid: &str, outcome: DeferOutcome, fx: &mut Vec<Effect>) {
        if let DeferOutcome::Overflowed { dropped } = outcome {
            fx.push(Effect::Warn(format!(
                "deferred input 超過 262144 bytes，整段清空（com={} client_id={cid} dropped={dropped}）",
                self.profile.com
            )));
        }
    }

    /// D-19：每 `(client_id, reason, input_epoch)` 至多一筆。
    fn warn_drop(&mut self, cid: &str, reason: &'static str, epoch: u64, bytes: usize, fx: &mut Vec<Effect>) {
        if self.input_drop_warned.len() >= DROP_WARN_KEYS_MAX {
            self.input_drop_warned.clear();
        }
        if !self.input_drop_warned.insert((cid.to_string(), reason, epoch)) {
            return;
        }
        fx.push(Effect::Warn(format!(
            "console 輸入因上下文失效丟棄（com={} client_id={cid} reason={reason} bytes={bytes}）",
            self.profile.com
        )));
    }

    // ---------------- console 事件 ----------------

    /// `DataEvent::ConsoleInputBytes`（07 Task 6 Step 3 第 2 點的檢查順序）。
    pub(super) fn console_input(
        &mut self,
        client_id: &str,
        lifetime: u64,
        input_epoch: u64,
        bytes: Vec<u8>,
        now: Mono,
    ) -> Vec<Effect> {
        let mut fx = Vec::new();
        if bytes.is_empty() {
            return fx;
        }
        if input_epoch != self.generations.input_epoch {
            self.warn_drop(client_id, "stale_epoch", input_epoch, bytes.len(), &mut fx);
            return fx;
        }
        if !self.human.is_live(client_id, lifetime) {
            self.warn_drop(client_id, "unknown_client", input_epoch, bytes.len(), &mut fx);
            return fx;
        }
        if self.deps.fence.with(|f| f.active_epoch.is_none()) {
            // SC-31(a)
            self.warn_drop(client_id, "no_active_epoch", input_epoch, bytes.len(), &mut fx);
            return fx;
        }
        if self.human.lane.is_busy_for(client_id) {
            // D-15(2)：同源順序
            let seq = self.human.lane.next_seq();
            let tag = HumanTxTag {
                seq,
                client_id: client_id.to_string(),
                client_lifetime: lifetime,
                input_epoch,
                class: TxClass::HumanRaw,
                len: bytes.len(),
            };
            self.push_backlog(
                BacklogItem {
                    tag,
                    payload: bytes,
                    kind: BacklogKind::Input,
                },
                &mut fx,
            );
            return fx;
        }
        self.route_console_input(client_id, lifetime, input_epoch, bytes, now, &mut fx);
        fx
    }

    fn route_console_input(
        &mut self,
        client_id: &str,
        lifetime: u64,
        input_epoch: u64,
        bytes: Vec<u8>,
        now: Mono,
        fx: &mut Vec<Effect>,
    ) {
        match self.human.ownership.route_input(client_id, now) {
            InputRoute::DropFlash => self.warn_drop(client_id, "flash", input_epoch, bytes.len(), fx),
            InputRoute::Raw { .. } => {
                if self.human.deferred_len_for(client_id) > 0 {
                    let epoch = self.generations.input_epoch;
                    let outcome = self
                        .human
                        .deferred_mut()
                        .append(client_id, lifetime, input_epoch, epoch, &bytes);
                    self.defer_outcome_warn(client_id, outcome, fx);
                    if self.immediate_replay_allowed(client_id) {
                        self.replay_client(client_id, fx);
                    }
                } else {
                    self.lane_submit(client_id, lifetime, input_epoch, TxClass::HumanRaw, None, bytes, fx);
                }
            }
            InputRoute::Defer => {
                let epoch = self.generations.input_epoch;
                let outcome = self
                    .human
                    .deferred_mut()
                    .append(client_id, lifetime, input_epoch, epoch, &bytes);
                self.defer_outcome_warn(client_id, outcome, fx);
            }
            InputRoute::Line => self.console_line_input(client_id, &bytes, now, fx),
        }
    }

    /// line editor：echo 先於提交；每行以 `SubmitOrigin::HumanConsole` 進 arbiter（`_on_console_line`）。
    fn console_line_input(&mut self, client_id: &str, bytes: &[u8], now: Mono, fx: &mut Vec<Effect>) {
        let Some(rec) = self.human.consoles.get_mut(client_id) else {
            return;
        };
        let out = rec.line.consume(bytes);
        let overflow = rec.line.take_overflow_started();
        for _ in 0..overflow {
            fx.push(Effect::Warn(format!(
                "console line 超過 16385 bytes，整行丟棄（com={} client_id={client_id}）",
                self.profile.com
            )));
        }
        if !out.echo.is_empty() {
            fx.push(Effect::ConsoleOut {
                client_id: client_id.to_string(),
                item: OutItem::Display {
                    bytes: out.echo,
                    iac_escaped: false,
                },
            });
        }
        for line in out.lines {
            self.submit_console_line(client_id, line, now, fx);
        }
    }

    fn submit_console_line(&mut self, client_id: &str, line: String, now: Mono, fx: &mut Vec<Effect>) {
        let mode = human_console_mode(&line, self.path_flavor).as_str().to_string();
        let bytes = line.len();
        let req = SubmitReq {
            session_id: self.sid.clone(),
            command: line,
            source: format!("human:{client_id}"),
            mode,
            timeout_s: 30.0,
            priority: 100,
            expected_duration_s: None,
            completion_regex: None,
            origin: SubmitOrigin::HumanConsole {
                client_id: client_id.to_string(),
            },
        };
        let cmd_id = self.next_uuid();
        match self
            .deps
            .registry
            .admit(req, self.queue_registered, cmd_id, &self.wall_iso())
        {
            Ok(admitted) => {
                self.note_tick_close_interactive(&admitted.queued);
                self.queue.push(admitted.queued, &self.deps.registry);
                fx.extend(self.start_next(now));
            }
            Err(result) => {
                let code = result.get("error_code").and_then(PyValue::as_str).unwrap_or("");
                if LINE_REJECT_WARN_CODES.contains(&code) {
                    fx.push(Effect::Warn(format!(
                        "console line 被拒（com={} client_id={client_id} error_code={code} bytes={bytes}）",
                        self.profile.com
                    )));
                }
            }
        }
    }

    /// `ControlEvent::ConsoleConnected`（TCP／com0com accept；ownership 依 `on_stream_accept`）。
    pub(super) fn console_connected(
        &mut self,
        client_id: String,
        lifetime: u64,
        label: String,
        vtty: String,
        kind: ConsoleTransportKind,
        now: Mono,
    ) -> Vec<Effect> {
        self.human.next_lifetime = self.human.next_lifetime.max(lifetime);
        self.human.consoles.insert(
            client_id.clone(),
            ConsoleRecord {
                client_id: client_id.clone(),
                client_lifetime: lifetime,
                label,
                vtty,
                kind,
                internal: false,
                attached_at: now,
                line: LineEditor::new(),
            },
        );
        if self.human.primary_client_id.is_none() {
            self.human.primary_client_id = Some(client_id.clone());
        }
        self.human.peer_present.insert(client_id.clone(), true);
        let before = self.human.ownership.interactive_owner.clone();
        self.human.ownership.on_stream_accept(&client_id);
        self.after_owner_change(before);
        Vec::new()
    }

    /// `DataEvent::ConsoleDisconnected`（`_drop_console_client`，#136；與同 client 輸入同一條 data lane）。
    pub(super) fn console_disconnected(&mut self, client_id: &str, lifetime: u64) -> Vec<Effect> {
        if !self.human.is_live(client_id, lifetime) {
            return Vec::new();
        }
        let before = self.human.ownership.interactive_owner.clone();
        self.human.ownership.on_drop(client_id);
        self.after_owner_change(before);
        self.human.deferred_mut().remove_client(client_id);
        let removed = self.human.lane.remove_client(client_id);
        self.settle_removed_replays(&removed);
        self.human.remove_console(client_id);
        Vec::new()
    }

    /// lane 刪除（斷線／溢位／prune）的項目中屬於現行 hold 的回放一併結算，避免 hold 永不釋放（D-22(3)）。
    /// hold 因此釋放時，事件結尾由 `kick_after_hold_release` 讓佇列繼續出列。
    pub(super) fn settle_removed_replays(&mut self, removed: &[BacklogItem]) {
        let Some(current) = self.human.replay_hold.as_ref().map(|h| h.hold_id) else {
            return;
        };
        let n = removed
            .iter()
            .filter(|b| b.kind == (BacklogKind::Replay { hold: current }))
            .count();
        if n > 0 && self.hold_settled(n) {
            self.hold_release_kick = true;
        }
    }

    /// `ControlEvent::ConsoleScan`：更新 peer 判定（D-09）；hub 掃描另做 peer grace 與 self-heal（Task 7）。
    pub(super) fn console_scan(&mut self, scan: ConsoleScanSnapshot, source: crate::human_console::ScanSource, now: Mono) -> Vec<Effect> {
        let crate::human_console::HumanConsoleState {
            consoles, peer_present, ..
        } = &mut self.human;
        for (cid, rec) in consoles.iter() {
            let present = match (&scan.held, rec.kind) {
                (_, ConsoleTransportKind::TcpTelnet | ConsoleTransportKind::Com0com) => true,
                (None, _) => true,
                (Some(held), ConsoleTransportKind::Pty) => held.contains(&rec.vtty),
            };
            peer_present.insert(cid.clone(), present);
        }
        let mut fx = Vec::new();
        if matches!(source, crate::human_console::ScanSource::Hub) {
            // 07 Task 9 Step 3.4：先 reap（Python `reap_stale_consoles`，#76），再做 Task 7 的 peer grace 與 self-heal。
            if let Some(held) = scan.held.as_ref() {
                self.reap_stale_consoles(held, now, &mut fx);
            }
            self.after_console_scan(now);
        }
        fx
    }

    /// `uart_io.py:1458-1507`：回收非 internal、PTY、非 owner／suspended owner、已滿 `STALE_CONSOLE_GRACE_S`、
    /// 不在 held 的 console；primary 被回收時轉給剩下的第一個（`remove_console`）。`held=None`（掃描失敗）不回收。
    fn reap_stale_consoles(&mut self, held: &std::collections::HashSet<String>, now: Mono, fx: &mut Vec<Effect>) {
        let human_cid = |o: &Option<String>| o.as_deref().and_then(|o| o.strip_prefix("human:")).map(str::to_string);
        let protected = [
            human_cid(&self.human.ownership.interactive_owner),
            human_cid(&self.human.ownership.suspended_owner),
        ];
        let grace = super::lease_p6::secs_to_ns(sw_proto::constants::STALE_CONSOLE_GRACE_S);
        let stale: Vec<String> = self
            .human
            .consoles
            .values()
            .filter(|c| !c.internal && c.kind == ConsoleTransportKind::Pty)
            .filter(|c| !protected.iter().any(|p| p.as_deref() == Some(c.client_id.as_str())))
            .filter(|c| now.saturating_sub(c.attached_at) >= grace)
            .filter(|c| !held.contains(&c.vtty))
            .map(|c| c.client_id.clone())
            .collect();
        for cid in stale {
            self.human.deferred_mut().remove_client(&cid);
            let removed = self.human.lane.remove_client(&cid);
            self.settle_removed_replays(&removed);
            self.human.remove_console(&cid);
            self.human.peer_present.remove(&cid);
            fx.push(Effect::ConsoleClose { client_id: cid });
        }
    }

    /// TCP console 後端的 `attach_console`（`session_manager.py:6744-6756`、`uart_io.py:2578-2590`）：human 直接連端點，
    /// client 於 accept 時建立、raw ownership 由 `on_stream_accept` 授予；此處只回端點、不開 lease。
    fn console_attach_tcp(&mut self, label: Option<String>, now: Mono) -> PyDict {
        if !self.human.tcp_console
            || self.generations.conn_epoch.is_none()
            || !matches!(self.state, SessionState::Ready | SessionState::Attached)
        {
            return self.selector_error("SESSION_NOT_READY");
        }
        if self.tick_close_hold.is_some() {
            return self.selector_error("SESSION_INTERACTIVE_BUSY");
        }
        let endpoint = self.human.console_endpoint.clone();
        if self.human.vtty_cache.is_none() {
            self.human.vtty_cache = endpoint.clone();
        }
        let ep = |v: &Option<String>| v.clone().map(PyValue::from).unwrap_or(PyValue::Null);
        let mut payload = pydict! {
            "ok" => true,
            "client_id" => PyValue::Null,
            "label" => label.map(PyValue::from).unwrap_or(PyValue::Null),
            "vtty" => ep(&endpoint),
            "endpoint" => ep(&endpoint),
            "transport" => "tcp",
            "protocol" => "telnet",
        };
        payload.insert("session", self.session_value(now));
        payload
    }

    /// `SessionRequest::ConsoleAttach`（PTY；`attach_console`，`session_manager.py:6740-6773`）。
    pub(super) fn console_attach(&mut self, label: Option<String>, pty: Option<PtyInfo>, reply: Reply, now: Mono) -> Vec<Effect> {
        let mut fx = Vec::new();
        let Some(pty) = pty else {
            fx.push(Effect::Reply(reply, Ok(self.console_attach_tcp(label, now))));
            return fx;
        };
        if self.generations.conn_epoch.is_none()
            || !matches!(self.state, SessionState::Ready | SessionState::Attached)
        {
            fx.push(Effect::ConsoleDiscard { token: pty.token });
            fx.push(Effect::Reply(reply, Ok(self.selector_error("SESSION_NOT_READY"))));
            return fx;
        }
        if self.tick_close_hold.is_some() {
            // #56：tick 到期還原結算前（`_interactive_post_close`）。
            fx.push(Effect::ConsoleDiscard { token: pty.token });
            fx.push(Effect::Reply(reply, Ok(self.selector_error("SESSION_INTERACTIVE_BUSY"))));
            return fx;
        }
        let client_id = pty.client_id.clone();
        let label = label
            .as_deref()
            .map(str::trim)
            .filter(|l| !l.is_empty())
            .map(str::to_string)
            .unwrap_or_else(|| client_id.clone());
        let lifetime = self.human.alloc_lifetime();
        self.human.consoles.insert(
            client_id.clone(),
            ConsoleRecord {
                client_id: client_id.clone(),
                client_lifetime: lifetime,
                label: label.clone(),
                vtty: pty.slave_path.clone(),
                kind: ConsoleTransportKind::Pty,
                internal: false,
                attached_at: now,
                line: LineEditor::new(),
            },
        );
        if self.human.primary_client_id.is_none() {
            self.human.primary_client_id = Some(client_id.clone());
        }
        if self.human.vtty_cache.is_none() {
            self.human.vtty_cache = Some(pty.slave_path.clone());
        }
        fx.push(Effect::ConsoleAdopt {
            token: pty.token,
            client_id: client_id.clone(),
            client_lifetime: lifetime,
            label: label.clone(),
        });
        let mut payload = pydict! {
            "ok" => true,
            "client_id" => client_id.clone(),
            "label" => label,
            "vtty" => pty.slave_path,
        };
        self.refresh_lease(now);
        if self.lease.is_none() {
            let timeout = self
                .profile
                .template
                .hard_timeout_s
                .max(sw_proto::constants::ATTACHED_CONSOLE_LEASE_TIMEOUT_S);
            let id = self.open_lease(&format!("human:{client_id}"), timeout, false, now);
            payload.insert("interactive_session_id", PyValue::from(id));
            payload.insert("interactive_owner", PyValue::Bool(!self.foreground_busy()));
        }
        payload.insert("session", self.session_value(now));
        fx.push(Effect::Reply(reply, Ok(payload)));
        fx
    }

    /// P6 Task 9 步驟 5：登記哨兵 primary（`label="primary"`、`internal=true`），`session.vtty_path = bridge.vtty_path`；
    /// 已有保留 console 時（Python 沿用 preserved）丟棄預建 PTY。
    pub(super) fn register_sentinel(&mut self, pty: PtyInfo, now: Mono) -> Vec<Effect> {
        if !self.human.consoles.is_empty() {
            return vec![Effect::ConsoleDiscard { token: pty.token }];
        }
        let lifetime = self.human.alloc_lifetime();
        let label = "primary".to_string();
        let client_id = pty.client_id.clone();
        self.human.consoles.insert(
            client_id.clone(),
            ConsoleRecord {
                client_id: client_id.clone(),
                client_lifetime: lifetime,
                label: label.clone(),
                vtty: pty.slave_path.clone(),
                kind: ConsoleTransportKind::Pty,
                internal: true,
                attached_at: now,
                line: LineEditor::new(),
            },
        );
        self.human.primary_client_id = Some(client_id.clone());
        self.human.vtty_cache = Some(pty.slave_path);
        vec![Effect::ConsoleAdopt {
            token: pty.token,
            client_id,
            client_lifetime: lifetime,
            label,
        }]
    }

    /// drain Phase 1 差量（增補 #11）：回放撤銷、BC-02 失效、ownership 重設、retained human、Release 關閉全部 console。
    pub(super) fn human_phase1(&mut self, kind: &StopKind, fx: &mut Vec<Effect>) {
        self.revoke_replay();
        self.deps.fence.with(|f| f.replay_auth = None);
        self.human.replay_hold = None;
        self.human.replay_after_revoked = false;
        self.tick_close_hold = None;
        self.tick_close_interactive.clear();
        self.fg_adm = None;
        self.fg_origin = None;
        self.self_test_suspended = false;
        let reason = if *kind == StopKind::Release {
            InvalidateReason::Release
        } else {
            InvalidateReason::Recovery
        };
        self.invalidate_input(reason, fx);
        self.human.lane.reset();
        // `uart_io.py:1113-1115`：bridge 停止時關閉 listener、端點清為 None（executor 於 BeginDrain 停 hub listener）。
        self.human.console_endpoint = None;
        if let Some(lease) = self.lease.as_ref().filter(|l| l.is_human()) {
            self.retained_human = Some((lease.owner.clone(), lease.timeout_s));
        }
        let before = self.human.ownership.interactive_owner.clone();
        self.human.ownership.reset_for_stop();
        self.after_owner_change(before);
        if *kind == StopKind::Release {
            if !self.human.consoles.is_empty() {
                fx.push(Effect::ConsoleCloseAll);
            }
            self.human.consoles.clear();
            self.human.peer_present.clear();
            self.human.primary_client_id = None;
            self.retained_human = None;
        }
        // Python drain：`session.vtty_path = preserved.primary_vtty()`；release 時 console 全關 → None。
        self.human.vtty_cache = self
            .human
            .primary_client_id
            .as_ref()
            .and_then(|p| self.human.consoles.get(p))
            .map(|c| c.vtty.clone());
    }

    /// 06 命令出列時的前景 hook（D-10：只有 `SubmitOrigin::HumanConsole` 不 suspend）。
    pub(super) fn command_fg_begin(&mut self, origin: &SubmitOrigin, interactive_mode: bool) {
        self.fg_origin = Some(origin.clone());
        let source_is_agent = !matches!(origin, SubmitOrigin::HumanConsole { .. }) && !interactive_mode;
        let mut fx = Effects(Vec::new());
        let adm = self.fg_begin(source_is_agent, &mut fx);
        self.fg_adm = Some(adm);
    }

    /// 06 命令結算時的前景 hook。
    pub(super) fn command_fg_finish(&mut self, fx: &mut Vec<Effect>) {
        self.fg_origin = None;
        if let Some(adm) = self.fg_adm.take() {
            let mut out = Effects(Vec::new());
            self.fg_finish(adm, &mut out);
            fx.extend(out.0);
        }
    }

    /// self-test probe 的 suspend 與回放（增補 #12）。
    pub(super) fn self_test_suspend_begin(&mut self) {
        let mut fx = Effects(Vec::new());
        self.self_test_suspended = self.human_suspend_begin(&mut fx);
    }

    pub(super) fn self_test_suspend_end(&mut self, fx: &mut Vec<Effect>) {
        if std::mem::take(&mut self.self_test_suspended) {
            let mut out = Effects(Vec::new());
            self.human_suspend_end(true, &mut out);
            fx.extend(out.0);
        }
    }

    /// 測試與 RPC 共用：產生 12-hex client id（`uuid.uuid4().hex[:12]`）。
    pub fn new_console_client_id(rng: &dyn Fn(&mut [u8])) -> String {
        let mut fill = |b: &mut [u8]| rng(b);
        uuid4_hex(&mut fill).chars().take(12).collect()
    }
}
