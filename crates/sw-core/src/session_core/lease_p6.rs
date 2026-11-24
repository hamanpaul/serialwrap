//! P6 Task 7：interactive lease 差量（07 Task 7、D-08、D-16、D-22、parity-amendments #56）。
//!
//! 06 T14 已交付 agent lease 的 open／send／status／close、tick 與 `LeaseExpiry`；本檔補 human lease、
//! soft preempt 的 stash 還原、`_PostCloseAction`、peer-loss grace、self-heal、retained human 與全域 LeaseIndex 效果。

use std::sync::Arc;

use sw_proto::constants::{ATTACHED_CONSOLE_LEASE_TIMEOUT_S, HUMAN_PEER_GRACE_S};
use sw_proto::pydict;
use sw_proto::pyjson::{PyDict, PyValue};

use super::*;
use crate::lease::{lease_context, PostClose};
use crate::rescue::{classify_attached_rescue, RescueClass};
use crate::uart_bridge::WorkerSnapshot;

pub(super) fn secs_to_ns(s: f64) -> u64 {
    (s.max(0.0) * 1_000_000_000.0) as u64
}

impl SessionCore {
    // ---------------- 公開存取子 ----------------

    /// 目前 active lease（唯讀）。
    pub fn active_lease(&self) -> Option<&InteractiveLease> {
        self.lease.as_ref()
    }

    /// soft preempt／ATTACHED 救援時 stash 的 human lease。
    pub fn stashed_human_lease(&self) -> Option<&InteractiveLease> {
        self.stashed_human.as_ref()
    }

    /// `_lease_context(lease, bridge)`（02 §6.2）。
    pub fn lease_context_now(&self, now: Mono) -> PyDict {
        lease_context(
            self.lease.as_ref(),
            self.human.ownership.last_human_input_at,
            now,
        )
    }

    /// worker 健康覆寫（`snapshot()["running"]`／`["serial_alive"]`；只供測試注入）。正式路徑取
    /// `SelfTestProbes::bridge_snapshot`（manager 注入）；兩者皆未設定時視為健康。
    pub fn set_worker_health(&mut self, probe: Arc<dyn Fn() -> WorkerSnapshot + Send + Sync>) {
        self.worker_health = Some(probe);
    }

    // ---------------- 緩衝效果 ----------------

    pub(super) fn lease_index(&mut self, op: LeaseIndexOp) {
        self.fx_buf.push(Effect::LeaseIndex(op));
    }

    fn insert_lease_index(&mut self, lease: &InteractiveLease) {
        let interactive_id = lease.interactive_id.clone();
        let op = if lease.mirror_client_lifetime.is_some() {
            LeaseIndexOp::InsertTcpMirror { interactive_id }
        } else {
            LeaseIndexOp::Insert { interactive_id }
        };
        self.lease_index(op);
    }

    /// 併入本事件緩衝的效果：`LeaseIndex` 一律排在本批最前（必然先於 `Reply` 與 `StartOp`，07 Task 7 效果順序不變式），其餘附加在尾端。
    pub(super) fn drain_fx_buf(&mut self, effects: &mut Vec<Effect>) {
        if self.fx_buf.is_empty() {
            return;
        }
        let buf = std::mem::take(&mut self.fx_buf);
        let (index, rest): (Vec<Effect>, Vec<Effect>) =
            buf.into_iter().partition(|e| matches!(e, Effect::LeaseIndex(_)));
        effects.splice(0..0, index);
        effects.extend(rest);
    }

    // ---------------- close（`_close_interactive_locked`＋`_PostCloseAction.execute`） ----------------

    pub(super) fn close_lease_tick(&mut self) {
        self.close_lease_inner(true, true);
        self.sync_tcp_console_lease(self.now_cache);
    }

    pub(super) fn close_lease_p6(&mut self, expired: bool) {
        self.close_lease_inner(expired, false);
        self.sync_tcp_console_lease(self.now_cache);
    }

    fn close_lease_inner(&mut self, expired: bool, tick: bool) {
        let Some(lease) = self.lease.take() else {
            return;
        };
        let preempted_tcp_mirror_lifetime = lease.preempted_tcp_mirror_lifetime;
        let now = self.now_cache;
        self.bump_owner_gen();
        if expired {
            for evicted in self.tombstones.push_evicting(lease.interactive_id.clone(), "expired") {
                self.lease_index(LeaseIndexOp::Remove { interactive_id: evicted });
            }
        } else {
            self.lease_index(LeaseIndexOp::Remove {
                interactive_id: lease.interactive_id.clone(),
            });
        }
        if !lease.suspended_human {
            // 一般 close：`bridge.set_interactive_owner(None)`。
            self.human_set_owner(None);
            return;
        }
        let mut post = PostClose {
            needs_resume: true,
            ..PostClose::default()
        };
        let stash = self.stashed_human.as_ref().cloned();
        let live_tcp_owner = self.human.live_tcp_raw_owner();
        let mut restored = false;
        if let Some(stash) = stash.as_ref() {
            let cid = stash.human_client_id().unwrap_or("").to_string();
            let mirror_still_owned = stash.mirror_client_lifetime.is_some_and(|lifetime| {
                live_tcp_owner.as_ref().is_some_and(|(owner, live_lifetime)| {
                    owner == &stash.owner && *live_lifetime == lifetime
                })
            });
            let stash_alive = if stash.mirror_client_lifetime.is_some() {
                mirror_still_owned
            } else {
                !stash.expired(now) && self.human_peer_present(&cid)
            };
            if stash_alive {
                if let Some(client_lifetime) = stash.mirror_client_lifetime {
                    self.pending_tcp_suspend_settlement = Some(TcpSuspendSettlement {
                        source_lease_id: lease.interactive_id.clone(),
                        owner: stash.owner.clone(),
                        client_lifetime,
                    });
                    let before_hold = self.human.replay_hold.as_ref().map(|hold| hold.hold_id);
                    if !self.restore_stashed_tcp_mirror_if_allowed() {
                        // agent 租約已關閉，但保留暫停中的 TCP 所有權，等共用 guard 開放。
                        self.human_set_owner(None);
                        return;
                    }
                    if tick {
                        let after = self.human.replay_hold.as_ref().filter(|hold| !hold.revoked).map(|hold| hold.hold_id);
                        if after.is_some() && after != before_hold {
                            self.tick_close_hold = after;
                        }
                    }
                    return;
                } else {
                    self.stashed_human.take();
                    self.lease = Some(stash.clone());
                    self.insert_lease_index(stash);
                    self.human_set_owner(Some(&stash.owner));
                    restored = true;
                }
                post.replay_allowed = true;
            }
        }
        if !restored {
            self.stashed_human.take();
            // agent 持有租約期間，EOF／重新連線可能已換成新的暫停中 TCP client。
            // 清除失效 stash 與該 client 的延後輸入，但保留替換後的 raw 所有者以便恢復。
            // PTY 與一般 human 租約的 close 行為維持原狀。
            let replacement_tcp_owner = preempted_tcp_mirror_lifetime.is_some()
                && live_tcp_owner.as_ref().is_some_and(|(owner, lifetime)| {
                    !stash.as_ref().is_some_and(|old| {
                        old.owner == *owner && old.mirror_client_lifetime == Some(*lifetime)
                    })
                });
            if replacement_tcp_owner {
                if let Some((owner, client_lifetime)) = live_tcp_owner.as_ref() {
                    self.pending_tcp_suspend_settlement = Some(TcpSuspendSettlement {
                        source_lease_id: lease.interactive_id.clone(),
                        owner: owner.clone(),
                        client_lifetime: *client_lifetime,
                    });
                }
                // 替換後的 TCP 所有者必須等 sync_tcp_console_lease 取得共用 guard 與 ticket，
                // 建立鏡像後才解除暫停；否則 close／expiry 會先繞過 guard 重授 raw 所有權。
                if let Some(cid) = post.dropped_stash_client.as_deref() {
                    let dropped = self.human.deferred_mut().remove_client(cid);
                    if dropped > 0 {
                        self.fx_buf.push(Effect::Warn(format!(
                            "deferred input 因 lease 上下文失效清空（com={} client_id={cid} reason=lease_dropped dropped={dropped}）",
                            self.profile.com
                        )));
                    }
                }
                self.human_set_owner(None);
                return;
            } else {
                self.human_set_owner(None);
                post.clear_owner_after_resume = true;
            }
            post.dropped_stash_client = stash.as_ref().and_then(|s| s.human_client_id()).map(str::to_string);
        }
        self.execute_post_close(post, tick);
    }

    /// `_PostCloseAction.execute` 等價；D-16：stash 未還原時先刪除該 client 的 deferred 再 resume。
    fn execute_post_close(&mut self, post: PostClose, tick: bool) {
        if !post.needs_resume {
            return;
        }
        let mut fx = Vec::new();
        if let Some(cid) = post.dropped_stash_client.as_deref() {
            let dropped = self.human.deferred_mut().remove_client(cid);
            if dropped > 0 {
                fx.push(Effect::Warn(format!(
                    "deferred input 因 lease 上下文失效清空（com={} client_id={cid} reason=lease_dropped dropped={dropped}）",
                    self.profile.com
                )));
            }
        }
        let before_hold = self.human.replay_hold.as_ref().map(|h| h.hold_id);
        let mut out = Effects(Vec::new());
        self.human_suspend_end(true, &mut out);
        fx.extend(out.0);
        if tick {
            let after = self.human.replay_hold.as_ref().filter(|h| !h.revoked).map(|h| h.hold_id);
            if after.is_some() && after != before_hold {
                self.tick_close_hold = after;
            }
        }
        if post.clear_owner_after_resume {
            self.human_set_owner(None);
        }
        self.fx_buf.extend(fx);
    }

    /// DF-20（OD-37 範圍，rulings/p6.md round4-cont OWNER-BATCH #4）：`session recover` 受理與重新開機 banner 時，
    /// 既有 agent lease 失效（一般 close：不留 expired tombstone，舊 id 之後回 `INTERACTIVE_NOT_FOUND`；stash 的
    /// human lease 依 PostClose 還原）。human console lease 跟隨仍掛著的 console，不在此失效。Python 現行照送。
    pub(super) fn invalidate_agent_lease_df20(&mut self) {
        if self.lease.as_ref().is_some_and(|lease| !lease.is_human()) {
            self.close_lease_p6(false);
        }
    }

    /// drain Phase 1：lease 移除、stash 丟棄（無 resume／回放）。
    pub(super) fn clear_interactive_p6(&mut self) {
        if let Some(lease) = self.lease.take() {
            self.bump_owner_gen();
            self.lease_index(LeaseIndexOp::Remove {
                interactive_id: lease.interactive_id,
            });
            self.human_set_owner(None);
        }
        if let Some(stash) = self.stashed_human.take() {
            self.lease_index(LeaseIndexOp::Remove {
                interactive_id: stash.interactive_id,
            });
        }
        self.pending_tcp_suspend_settlement = None;
    }

    // ---------------- refresh（`_refresh_interactive_locked`） ----------------

    pub(super) fn human_peer_present(&self, client_id: &str) -> bool {
        self.human.consoles.contains_key(client_id)
            && self.human.peer_present.get(client_id).copied().unwrap_or(true)
    }

    /// RPC 路徑的完整 refresh。
    pub(super) fn refresh_lease_p6(&mut self, now: Mono) {
        self.sync_tcp_console_lease(now);
        self.refresh_inner(now, true);
        self.sync_tcp_console_lease(now);
    }

    /// TCP raw owner 的 session lease 鏡像。只更新 Lease／LeaseIndex，不授 raw owner、不改 owner_gen。
    pub(super) fn sync_tcp_console_lease(&mut self, now: Mono) {
        let logical_owner = self.human.live_tcp_raw_owner();
        let active_matches = self.lease.as_ref().is_some_and(|lease| {
            logical_owner.as_ref().is_some_and(|(owner, lifetime)| {
                lease.mirror_client_lifetime == Some(*lifetime) && lease.owner == *owner
            })
        });
        let stashed_matches = self.stashed_human.as_ref().is_some_and(|lease| {
            logical_owner.as_ref().is_some_and(|(owner, lifetime)| {
                lease.mirror_client_lifetime == Some(*lifetime) && lease.owner == *owner
            })
        });

        if self.lease.as_ref().is_some_and(|lease| lease.mirror_client_lifetime.is_some()) && !active_matches {
            self.forget_active_tcp_mirror();
        }
        if self
            .stashed_human
            .as_ref()
            .is_some_and(|lease| lease.mirror_client_lifetime.is_some())
            && !stashed_matches
        {
            self.forget_stashed_tcp_mirror();
        }
        if active_matches {
            return;
        }
        if stashed_matches {
            // agent 租約關閉時若 guard 凍結，TCP 鏡像留在 stash；guard 開放後由下一次同步還原並恢復。
            if self.lease.is_none() {
                self.restore_stashed_tcp_mirror_if_allowed();
            }
            return;
        }
        let Some((owner, lifetime)) = logical_owner else {
            return;
        };

        // 新鏡像沿用既有的交接 admission 與 bridge／state／stop／flash guards；它不接觸 ownership。
        if !self.tcp_mirror_guard_allows_new() {
            return;
        }
        let gate = self.deps.registry.admission_gate();
        let Some(ticket) = gate.admit("session.interactive_open", Some(&self.profile.com)) else {
            return;
        };
        let _admission = crate::admission::AdmissionTicket::new(gate, ticket);
        if !self.tcp_mirror_guard_allows_new()
            || !self.human.live_tcp_raw_owner().is_some_and(|(current_owner, current_lifetime)| {
                current_owner == owner && current_lifetime == lifetime
            })
        {
            return;
        }

        let id = self.next_uuid();
        let timeout = self
            .profile
            .template
            .hard_timeout_s
            .max(ATTACHED_CONSOLE_LEASE_TIMEOUT_S);
        let mut mirror = InteractiveLease::new(
            id.clone(),
            owner,
            self.wall_iso(),
            timeout,
            now,
            false,
        );
        mirror.mirror_client_lifetime = Some(lifetime);
        match self.lease.as_ref() {
            None if self.stashed_human.is_none() => {
                self.lease = Some(mirror);
                let mirror = self.lease.as_ref().expect("剛建立 TCP mirror").clone();
                self.insert_lease_index(&mirror);
                self.settle_pending_tcp_suspend_for(&mirror.owner, lifetime);
            }
            Some(active) if !active.is_human() && active.suspended_human && self.stashed_human.is_none() => {
                self.stashed_human = Some(mirror);
                let mirror = self.stashed_human.as_ref().expect("剛 stash TCP mirror").clone();
                self.insert_lease_index(&mirror);
            }
            // Agent／一般 human lease 與非 mirror stash 都是既有權威，不由 TCP mirror 覆蓋。
            _ => {}
        }
    }

    /// TCP self-heal 與 lease mirror 必須在同一組 lifecycle／flash／handoff 條件下才可授權或建帳。
    fn tcp_mirror_guard_allows_new(&self) -> bool {
        self.generations.conn_epoch.is_some()
            && matches!(self.state, SessionState::Ready | SessionState::Attached)
            && self.stop.is_none()
            && self.pending_open_stop.is_none()
            && !self.shutdown_requested
            && !self.shutdown_complete
            && !self.flash_busy()
            && !self.handoff_frozen()
    }

    /// 自 EOF／owner 替換移除指定 client lifetime 的 mirror；不呼叫 human_set_owner。
    pub(super) fn forget_tcp_console_mirror_for_client(&mut self, client_id: &str, lifetime: u64) {
        let owner = format!("human:{client_id}");
        if self.lease.as_ref().is_some_and(|lease| {
            lease.owner == owner && lease.mirror_client_lifetime == Some(lifetime)
        }) {
            self.forget_active_tcp_mirror();
        }
        if self.stashed_human.as_ref().is_some_and(|lease| {
            lease.owner == owner && lease.mirror_client_lifetime == Some(lifetime)
        }) {
            self.forget_stashed_tcp_mirror();
        }
        let pending_matches = self.pending_tcp_suspend_matches(&owner, lifetime);
        if pending_matches {
            self.pending_tcp_suspend_settlement = None;
            self.human.ownership.settle_suspend_without_owner();
        }
    }

    fn forget_active_tcp_mirror(&mut self) {
        if let Some(lease) = self.lease.take() {
            self.lease_index(LeaseIndexOp::Remove {
                interactive_id: lease.interactive_id,
            });
        }
    }

    fn forget_stashed_tcp_mirror(&mut self) {
        if let Some(lease) = self.stashed_human.take() {
            self.lease_index(LeaseIndexOp::Remove {
                interactive_id: lease.interactive_id,
            });
        }
    }

    /// 共用 TCP 鏡像 guard 與 admission ticket，還原仍存活的軟式搶占 stash。
    fn restore_stashed_tcp_mirror_if_allowed(&mut self) -> bool {
        let Some(stash) = self
            .stashed_human
            .as_ref()
            .filter(|lease| lease.mirror_client_lifetime.is_some())
            .cloned()
        else {
            return false;
        };
        if !self.tcp_mirror_guard_allows_new() {
            return false;
        }
        let gate = self.deps.registry.admission_gate();
        let Some(ticket) = gate.admit("session.interactive_open", Some(&self.profile.com)) else {
            return false;
        };
        let _admission = crate::admission::AdmissionTicket::new(gate, ticket);
        if !self.tcp_mirror_guard_allows_new()
            || self.lease.is_some()
            || !self.stashed_tcp_mirror_is_live(&stash)
        {
            return false;
        }
        let Some(stash) = self.stashed_human.take() else {
            return false;
        };
        self.lease = Some(stash.clone());
        self.insert_lease_index(&stash);
        self.settle_pending_tcp_suspend_for(
            &stash.owner,
            stash.mirror_client_lifetime.expect("TCP stash lifetime"),
        );
        true
    }

    /// 只結算明確記錄且 owner/lifetime 相符的 agent soft-preempt suspend。
    fn settle_pending_tcp_suspend_for(&mut self, owner: &str, client_lifetime: u64) -> bool {
        if !self.pending_tcp_suspend_matches(owner, client_lifetime) {
            return false;
        }
        self.pending_tcp_suspend_settlement = None;
        let mut effects = Effects(Vec::new());
        self.human_suspend_end(true, &mut effects);
        self.fx_buf.extend(effects.0);
        true
    }

    fn pending_tcp_suspend_matches(&self, owner: &str, client_lifetime: u64) -> bool {
        self.pending_tcp_suspend_settlement.as_ref().is_some_and(|pending| {
            !pending.source_lease_id.is_empty()
                && pending.owner == owner
                && pending.client_lifetime == client_lifetime
        })
    }

    fn stashed_tcp_mirror_is_live(&self, stash: &InteractiveLease) -> bool {
        let Some(lifetime) = stash.mirror_client_lifetime else {
            return false;
        };
        self.stashed_human.as_ref().is_some_and(|current| {
            current.interactive_id == stash.interactive_id
                && current.owner == stash.owner
                && current.mirror_client_lifetime == Some(lifetime)
        }) && self
            .human
            .live_tcp_raw_owner()
            .is_some_and(|(owner, live_lifetime)| owner == stash.owner && live_lifetime == lifetime)
    }

    /// `owner_check`：RPC 路徑比照 Python 檢查 `snapshot()["interactive_owner"] == lease.owner`；
    /// hub 掃描路徑只在未 suspend 時檢查（Python 不會在 agent 命令期間週期 refresh，否則會誤拆 human lease）。
    fn refresh_inner(&mut self, now: Mono, rpc: bool) {
        let Some(lease) = self.lease.as_ref() else {
            return;
        };
        if lease.mirror_client_lifetime.is_some() {
            // TCP EOF／重連由 data lane 的 exact lifetime 與 sync_tcp_console_lease 結算；不可套
            // PTY peer grace 或 active-owner-only 檢查，否則 agent 命令 suspend 期間會拆掉 mirror。
            return;
        }
        if !lease.is_human() {
            if lease.expired(now) {
                self.close_lease_p6(true);
            }
            return;
        }
        if self.generations.conn_epoch.is_none() {
            self.close_lease_p6(false);
            return;
        }
        let cid = lease.human_client_id().unwrap_or("").to_string();
        if !self.human_peer_present(&cid) {
            let lost = lease.peer_lost_at;
            match lost {
                None => {
                    if let Some(l) = self.lease.as_mut() {
                        l.peer_lost_at = Some(now);
                    }
                }
                Some(at) if now.saturating_sub(at) <= secs_to_ns(HUMAN_PEER_GRACE_S) => {}
                Some(_) => {
                    // 超過 grace：`bridge.detach_console(client_id)` 後一般 close。
                    self.detach_console_p6(&cid);
                    self.close_lease_p6(false);
                }
            }
            return;
        }
        if let Some(l) = self.lease.as_mut() {
            l.peer_lost_at = None;
        }
        let owner_ok = self.lease.as_ref().map(|l| l.owner.clone()) == self.human.ownership.interactive_owner;
        let check = rpc || (self.human.ownership.suspend_depth == 0 && !self.human.ownership.admission_blocked);
        if check && !owner_ok {
            self.close_lease_p6(false);
        }
    }

    /// `bridge.detach_console`（#78）＋關閉該 console。
    pub(super) fn detach_console_p6(&mut self, client_id: &str) -> bool {
        if !self.human.consoles.contains_key(client_id) {
            return false;
        }
        let lifetime = self
            .human
            .consoles
            .get(client_id)
            .map(|record| record.client_lifetime);
        let before = self.human.ownership.interactive_owner.clone();
        self.human.ownership.on_detach(client_id);
        self.after_owner_change(before);
        if let Some(lifetime) = lifetime {
            self.forget_tcp_console_mirror_for_client(client_id, lifetime);
        }
        self.human.deferred_mut().remove_client(client_id);
        let removed = self.human.lane.remove_client(client_id);
        self.settle_removed_replays(&removed);
        self.human.remove_console(client_id);
        self.fx_buf.push(Effect::ConsoleClose {
            client_id: client_id.to_string(),
        });
        true
    }

    /// hub 掃描後：human lease 的 peer grace（D-14 `ConsolePeerGraceExpired`）與 self-heal（`session_manager.py` reaper 段）。
    pub(super) fn after_console_scan(&mut self, now: Mono) {
        self.sync_tcp_console_lease(now);
        self.refresh_inner(now, false);
        self.sync_tcp_console_lease(now);
        self.self_heal(now);
        self.sync_tcp_console_lease(now);
    }

    fn self_heal(&mut self, now: Mono) {
        let o = &self.human.ownership;
        if o.interactive_owner.is_some() || o.suspended_owner.is_some() || o.agent_active || o.flash_mode {
            return;
        }
        let Some(primary) = self.human.primary_client_id.clone() else {
            return;
        };
        let primary_is_tcp = self
            .human
            .consoles
            .get(&primary)
            .is_some_and(|console| console.kind == crate::human_console::ConsoleTransportKind::TcpTelnet);
        if primary_is_tcp && !self.tcp_mirror_guard_allows_new() {
            return;
        }
        if !self.human_peer_present(&primary) {
            return;
        }
        if !matches!(self.state, SessionState::Ready | SessionState::Attached) || self.lease.is_some() {
            return;
        }
        let gate = self.deps.registry.admission_gate();
        let Some(ticket) = gate.admit("session.interactive_open", Some(&self.profile.com)) else {
            return;
        };
        let _admission = crate::admission::AdmissionTicket::new(gate, ticket);
        if self.handoff_frozen() || (primary_is_tcp && !self.tcp_mirror_guard_allows_new()) {
            return;
        }
        let owner = format!("human:{primary}");
        if !self.human.ownership.try_grant_if_idle(&owner) {
            return;
        }
        // grant 屬 owner 變更（arch §5.3）；lease 只補記錄，不再 set_interactive_owner（`_record_self_heal_lease_locked`）。
        self.bump_owner_gen();
        let id = self.next_uuid();
        let timeout = self
            .profile
            .template
            .hard_timeout_s
            .max(ATTACHED_CONSOLE_LEASE_TIMEOUT_S);
        let mut lease = InteractiveLease::new(id.clone(), owner.clone(), self.wall_iso(), timeout, now, false);
        if let Some((_, lifetime)) = self
            .human
            .live_tcp_raw_owner()
            .filter(|(live_owner, _)| live_owner == &owner)
        {
            lease.mirror_client_lifetime = Some(lifetime);
            self.lease = Some(lease.clone());
            self.insert_lease_index(&lease);
        } else {
            self.lease = Some(lease);
            self.lease_index(LeaseIndexOp::Insert { interactive_id: id });
        }
    }

    // ---------------- open（`interactive_open` 的 READY／ATTACHED 分支） ----------------

    fn busy_reply(&self) -> PyDict {
        let mut busy = pydict! { "ok" => false, "error_code" => "SESSION_INTERACTIVE_BUSY" };
        busy.insert(
            "interactive_session_id",
            self.lease
                .as_ref()
                .map(|l| PyValue::from(l.interactive_id.clone()))
                .unwrap_or(PyValue::Null),
        );
        busy
    }

    fn open_lease_full(&mut self, owner: &str, timeout_s: f64, recovery_mode: bool, suspended_human: bool, now: Mono) -> String {
        let id = self.open_lease(owner, timeout_s, recovery_mode, now);
        if let Some(l) = self.lease.as_mut() {
            l.suspended_human = suspended_human;
        }
        id
    }

    /// 把目前 human lease 移入 stash 並 suspend human（#53／救援）。
    fn stash_human_and_suspend(&mut self) -> Option<u64> {
        let mut mirror_lifetime = None;
        if let Some(existing) = self.lease.take() {
            mirror_lifetime = existing.mirror_client_lifetime;
            self.lease_index(LeaseIndexOp::Remove {
                interactive_id: existing.interactive_id.clone(),
            });
            self.stashed_human = Some(existing);
        }
        let before = self.human.ownership.interactive_owner.clone();
        self.human.ownership.suspend();
        self.after_owner_change(before);
        mirror_lifetime
    }

    fn mark_preempted_tcp_mirror(&mut self, lifetime: Option<u64>) {
        if let (Some(lease), Some(lifetime)) = (self.lease.as_mut(), lifetime) {
            lease.preempted_tcp_mirror_lifetime = Some(lifetime);
        }
    }

    pub(super) fn interactive_open_branches(
        &mut self,
        owner: &str,
        timeout_s: f64,
        allow_attached: bool,
        now: Mono,
    ) -> Result<PyDict, PyDict> {
        if self.state == SessionState::Ready {
            // #56：tick 到期還原的 hold 期間回 SESSION_INTERACTIVE_BUSY（Python 此時前景 token 未持有）。
            if self.tick_close_hold.is_some() {
                return Err(self.busy_reply());
            }
            if self.fg_slot_busy() {
                return Err(self.selector_error("SESSION_BUSY"));
            }
            self.refresh_lease_p6(now);
            // Python `post.needs_resume → retry`：refresh 若惰性結束過期 lease 並建立回放 hold（或 tick 還原），
            // 本次不再 soft preempt（否則 revoke 會撤銷剛建立的回放），依 D-22(2) 回 busy；結算後下一次 open 再評估。
            if self.tick_close_hold.is_some() {
                return Err(self.busy_reply());
            }
            if self.fg_slot_busy() {
                return Err(self.selector_error("SESSION_BUSY"));
            }
            let mut result;
            match self.lease.as_ref() {
                Some(existing) => {
                    let human_active = self
                        .lease_context_now(now)
                        .get("human_active")
                        .and_then(PyValue::as_bool)
                        .unwrap_or(false);
                    if existing.is_human() && !human_active && !owner.starts_with("human:") {
                        // #53 soft preempt：非破壞性降級，human 輸入進 deferred，close 後還原並回放。
                        self.revoke_replay();
                        let stashed_tcp_lifetime = self.stash_human_and_suspend();
                        let id = self.open_lease_full(owner, timeout_s, false, true, now);
                        self.mark_preempted_tcp_mirror(stashed_tcp_lifetime);
                        result = pydict! {
                            "ok" => true,
                            "interactive_id" => id,
                            "session" => self.session_value(now),
                            "recovery_mode" => false,
                        };
                        result.insert("soft_preempted", PyValue::Bool(true));
                    } else {
                        return Err(self.busy_reply());
                    }
                }
                None => {
                    let id = self.open_lease(owner, timeout_s, false, now);
                    result = pydict! {
                        "ok" => true,
                        "interactive_id" => id,
                        "session" => self.session_value(now),
                        "recovery_mode" => false,
                    };
                }
            }
            return Ok(result);
        }
        if !allow_attached {
            return Err(self.selector_error("SESSION_NOT_READY"));
        }
        if self.state != SessionState::Attached {
            return Err(self.selector_error("SESSION_NOT_READY"));
        }
        // Python `bridge.snapshot()` 的 running／serial_alive：正式路徑取 manager 注入的現行 epoch IoWorker 快照
        // （`SelfTestProbes::bridge_snapshot`，與 self-test 共用；接線稽核 R3-8）；探針有設但查無該 epoch 的 worker
        // 視為未運行。兩者皆未注入（單元 harness）時視為健康。
        let health = match self.worker_health.as_ref() {
            Some(probe) => Some(probe()),
            None => match (self.self_test_probes.bridge_snapshot.as_ref(), self.generations.conn_epoch) {
                (Some(snapshot), Some(epoch)) => Some(snapshot(epoch).unwrap_or(WorkerSnapshot {
                    serial_alive: false,
                    running: false,
                })),
                _ => None,
            },
        };
        let worker_ok = health.is_none_or(|h| h.running && h.serial_alive);
        if !worker_ok || self.vtty_alive() == Some(false) {
            return Err(self.selector_error("SESSION_NOT_READY"));
        }
        let tail = self.deps.rx.tail(BOOTLOADER_RX_TAIL_CHARS);
        let class = classify_attached_rescue(&tail, &self.profile);
        if class == RescueClass::NotBootloader {
            let mut out = self.selector_error("SESSION_NOT_READY");
            out.insert("error_detail", PyValue::from("NOT_BOOTLOADER"));
            return Err(out);
        }
        if self.tick_close_hold.is_some() {
            return Err(self.busy_reply());
        }
        self.refresh_lease_p6(now);
        // tick 到期還原的 hold 期間回 SESSION_INTERACTIVE_BUSY（#56）；其餘依 D-22(4) 撤銷回放後照常開 lease
        // （Python ATTACHED 分支無 SESSION_BUSY 出口，審查 7acf7f6 Medium）。
        if self.tick_close_hold.is_some() {
            return Err(self.busy_reply());
        }
        let clamped = timeout_s.min(MAX_RECOVERY_LEASE_S);
        // D-22(4)：ATTACHED 救援分支不判前景（Python parity），確定開 lease 時才撤銷回放授權。
        let revoke = !owner.starts_with("human:");
        let id = match self.lease.as_ref() {
            None => {
                if revoke {
                    self.revoke_replay();
                }
                self.open_lease_full(owner, clamped, true, false, now)
            }
            Some(existing) if existing.is_human() => {
                if revoke {
                    self.revoke_replay();
                }
                let stashed_tcp_lifetime = self.stash_human_and_suspend();
                let id = self.open_lease_full(owner, clamped, true, true, now);
                self.mark_preempted_tcp_mirror(stashed_tcp_lifetime);
                id
            }
            Some(_) => return Err(self.busy_reply()),
        };
        let mut result = pydict! {
            "ok" => true,
            "interactive_id" => id,
            "session" => self.session_value(now),
            "recovery_mode" => true,
        };
        match class {
            RescueClass::BootInterrupt => result.insert("boot_interrupt", PyValue::Bool(true)),
            RescueClass::LoginRequired => result.insert("login_required", PyValue::Bool(true)),
            _ => {}
        }
        Ok(result)
    }

    // ---------------- retained human、human reboot ----------------

    /// `_restore_retained_human_console_locked`：新 epoch 進 READY 或 ATTACHED 時重開 human lease（`session_manager.py:2728`）。
    pub(super) fn restore_retained_human(&mut self, now: Mono) {
        if self.retained_human.is_none()
            || self.generations.conn_epoch.is_none()
            || self.stop.is_some()
            || !matches!(self.state, SessionState::Ready | SessionState::Attached)
            || self.tick_close_hold.is_some()
        {
            return;
        }
        let Some((owner, timeout_s, retained_tcp_lifetime)) = self.retained_human.as_ref().cloned() else {
            return;
        };
        let Some(cid) = owner.strip_prefix("human:").filter(|c| !c.is_empty()) else {
            self.retained_human.take();
            return;
        };
        if !self.human.consoles.contains_key(cid) {
            self.retained_human.take();
            return;
        }
        let mirror_lifetime = match retained_tcp_lifetime {
            Some(lifetime) if self.retained_tcp_owner_is_live(&owner, cid, lifetime) => Some(lifetime),
            Some(_) => {
                self.retained_human.take();
                return;
            }
            None => None,
        };

        if let Some(lifetime) = mirror_lifetime {
            // 保留中的 TCP 還原與一般鏡像／自癒共用同一完整 guard 及 admission ticket；
            // guard 暫時關閉時不取出保留資料，留待後續 actor 事件重試。
            if !self.tcp_mirror_guard_allows_new() {
                return;
            }
            let gate = self.deps.registry.admission_gate();
            let Some(ticket) = gate.admit("session.interactive_open", Some(&self.profile.com)) else {
                return;
            };
            let _admission = crate::admission::AdmissionTicket::new(gate, ticket);
            if !self.tcp_mirror_guard_allows_new()
                || !self.retained_tcp_owner_is_live(&owner, cid, lifetime)
            {
                return;
            }

            self.refresh_lease_p6(now);
            if !self.tcp_mirror_guard_allows_new()
                || !self.retained_tcp_owner_is_live(&owner, cid, lifetime)
            {
                return;
            }
            self.retained_human.take();
            if self.lease.is_none() {
                self.open_lease_with_tcp_mirror(&owner, timeout_s, false, now, Some(lifetime));
            }
            return;
        }

        // PTY 還原維持原先的取出、更新與建立順序。
        let Some((owner, timeout_s, _)) = self.retained_human.take() else {
            return;
        };
        self.refresh_lease_p6(now);
        if self.lease.is_none() {
            self.open_lease_with_tcp_mirror(&owner, timeout_s, false, now, None);
        }
    }

    fn retained_tcp_owner_is_live(&self, owner: &str, client_id: &str, lifetime: u64) -> bool {
        self.retained_human.as_ref().is_some_and(|(retained_owner, _, retained_lifetime)| {
            retained_owner == owner && *retained_lifetime == Some(lifetime)
        }) && self.human.consoles.get(client_id).is_some_and(|console| {
            console.kind == crate::human_console::ConsoleTransportKind::TcpTelnet
                && self.human.is_live(client_id, lifetime)
                && console.client_lifetime == lifetime
                && self.human.peer_present.get(client_id).copied().unwrap_or(true)
        })
    }

    /// 目前前景命令是否為可信 human（OD-74、rulings s2 D5）：line editor 的 `HumanConsole` OR 請求的 `trusted_human`
    /// 快照（與 `SubmitReq::is_trusted_human()` 同式）；永不由 `source` 字首或 `source_class` 推定。
    pub(super) fn fg_origin_is_human(&self) -> bool {
        matches!(self.fg_origin, Some(SubmitOrigin::HumanConsole { .. })) || self.fg_trusted_human
    }

    /// human 來源 open-or-refresh lease（`_refresh_interactive_locked`＋`_open_interactive_locked`）；
    /// `#56` tick 還原結算前回 `Err(SESSION_INTERACTIVE_BUSY)`。
    fn human_promote_lease(&mut self, timeout_s: f64, now: Mono) -> Result<String, PyDict> {
        if self.tick_close_hold.is_some() {
            return Err(pydict! { "ok" => false, "error_code" => "SESSION_INTERACTIVE_BUSY" });
        }
        self.refresh_lease_p6(now);
        if self.lease.is_none() {
            // Python `owner=source`：console 為 `human:<client_id>`；可信 RPC 為保存的正規化 source（bare human → `human:cli`）。
            let source = match (&self.fg_origin, &self.fg_source) {
                (Some(SubmitOrigin::HumanConsole { client_id }), _) => format!("human:{client_id}"),
                (_, Some(source)) if self.fg_trusted_human => source.clone(),
                _ => "human:".to_string(),
            };
            self.open_lease(&source, timeout_s, false, now);
        }
        Ok(self.lease.as_ref().map(|l| l.interactive_id.clone()).unwrap_or_default())
    }

    /// human 來源命令逾時（`_recover_after_failure` human 分支，DF-02）：不送救援鍵、不改 state。
    pub(super) fn human_timeout_promote(&mut self, cmd_timeout_s: f64, now: Mono) -> PyDict {
        let timeout = cmd_timeout_s.max(self.profile.template.hard_timeout_s);
        let lease_id = match self.human_promote_lease(timeout, now) {
            Ok(id) => id,
            Err(busy) => return busy,
        };
        pydict! {
            "ok" => true,
            "execution_mode" => "interactive",
            "interactive_session_id" => lease_id,
            "status" => "interactive",
            "stdout" => "",
            "partial" => true,
            "recovery_action" => "PROMOTE_HUMAN_INTERACTIVE",
        }
    }

    /// human 來源 reboot 的 `PROMOTE_HUMAN_INTERACTIVE`（`_execute_command_inner` human 分支）。
    pub(super) fn human_reboot_promote(&mut self, now: Mono) -> PyDict {
        let timeout = self
            .profile
            .template
            .hard_timeout_s
            .max(ATTACHED_CONSOLE_LEASE_TIMEOUT_S);
        let lease_id = match self.human_promote_lease(timeout, now) {
            Ok(id) => id,
            Err(busy) => return busy,
        };
        self.set_state(SessionState::Attached, now);
        self.set_last_error(Some("REBOOTING".to_string()), None);
        self.recovering = false;
        self.pending_auto_login = false;
        self.on_detached_flush();
        pydict! {
            "ok" => true,
            "execution_mode" => "interactive",
            "interactive_session_id" => lease_id,
            "status" => "interactive",
            "stdout" => "",
            "partial" => true,
            "recovery_action" => "PROMOTE_HUMAN_INTERACTIVE",
        }
    }

    // ---------------- console_detach／console_list ----------------

    /// `session.vtty_path = bridge.vtty_path`（primary 的 slave path；D-20 快取更新點）。
    fn recompute_vtty_cache(&mut self) {
        self.human.vtty_cache = self
            .human
            .primary_client_id
            .as_ref()
            .and_then(|p| self.human.consoles.get(p))
            .map(|c| c.vtty.clone());
    }

    /// `detach_console`（`session_manager.py:6775-6797`）。
    pub(super) fn console_detach_request(&mut self, client_id: &str, now: Mono) -> PyDict {
        if self.generations.conn_epoch.is_none() {
            return self.selector_error("SESSION_NOT_READY");
        }
        let human_owner = format!("human:{client_id}");
        self.refresh_lease_p6(now);
        let ok = self.detach_console_p6(client_id);
        if self.lease.as_ref().is_some_and(|l| l.owner == human_owner) {
            // human lease 的 suspended_human 恆為 false → 一般 close（post 為 no-op）。
            self.close_lease_p6(false);
        }
        self.recompute_vtty_cache();
        if !ok {
            return pydict! { "ok" => false, "error_code" => "CONSOLE_NOT_FOUND", "client_id" => client_id };
        }
        pydict! { "ok" => true, "client_id" => client_id, "session" => self.session_value(now) }
    }

    /// `list_consoles`：先 `_prune_stale_consoles_locked`（非 primary、已過 grace、無 peer）再依 (label, client_id) 排序。
    pub(super) fn console_list_request(&mut self, now: Mono) -> PyDict {
        if self.generations.conn_epoch.is_none() {
            return self.selector_error("SESSION_NOT_READY");
        }
        let grace = secs_to_ns(sw_proto::constants::STALE_CONSOLE_GRACE_S);
        let stale: Vec<String> = self
            .human
            .consoles
            .values()
            .filter(|c| Some(&c.client_id) != self.human.primary_client_id.as_ref())
            .filter(|c| now.saturating_sub(c.attached_at) >= grace)
            .filter(|c| !self.human_peer_present(&c.client_id))
            .map(|c| c.client_id.clone())
            .collect();
        for cid in stale {
            let before = self.human.ownership.interactive_owner.clone();
            self.human.ownership.on_prune(&cid);
            self.after_owner_change(before);
            self.human.deferred_mut().remove_client(&cid);
            let removed = self.human.lane.remove_client(&cid);
            self.settle_removed_replays(&removed);
            self.human.remove_console(&cid);
            self.fx_buf.push(Effect::ConsoleClose { client_id: cid });
        }
        let owner = self.human.ownership.interactive_owner.clone();
        let mut rows: Vec<&crate::human_console::ConsoleRecord> = self.human.consoles.values().collect();
        rows.sort_by(|a, b| (a.label.as_str(), a.client_id.as_str()).cmp(&(b.label.as_str(), b.client_id.as_str())));
        let list: Vec<PyValue> = rows
            .into_iter()
            .map(|c| {
                PyValue::Dict(pydict! {
                    "client_id" => c.client_id.clone(),
                    "label" => c.label.clone(),
                    "vtty" => c.vtty.clone(),
                    "interactive_owner" => owner.as_deref() == Some(format!("human:{}", c.client_id).as_str()),
                })
            })
            .collect();
        pydict! { "ok" => true, "consoles" => PyValue::List(list), "session" => self.session_value(now) }
    }
}
