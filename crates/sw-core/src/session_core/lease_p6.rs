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

    /// worker 健康（`snapshot()["running"]`／`["serial_alive"]`；executor 注入，未注入視為健康）。
    pub fn set_worker_health(&mut self, probe: Arc<dyn Fn() -> WorkerSnapshot + Send + Sync>) {
        self.worker_health = Some(probe);
    }

    // ---------------- 緩衝效果 ----------------

    pub(super) fn lease_index(&mut self, op: LeaseIndexOp) {
        self.fx_buf.push(Effect::LeaseIndex(op));
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
    }

    pub(super) fn close_lease_p6(&mut self, expired: bool) {
        self.close_lease_inner(expired, false);
    }

    fn close_lease_inner(&mut self, expired: bool, tick: bool) {
        let Some(lease) = self.lease.take() else {
            return;
        };
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
        let stash = self.stashed_human.take();
        let mut restored = false;
        if let Some(stash) = stash.as_ref() {
            let cid = stash.human_client_id().unwrap_or("").to_string();
            if !stash.expired(now) && self.human_peer_present(&cid) {
                self.lease = Some(stash.clone());
                self.lease_index(LeaseIndexOp::Insert {
                    interactive_id: stash.interactive_id.clone(),
                });
                self.human_set_owner(Some(&stash.owner));
                restored = true;
                post.replay_allowed = true;
            }
        }
        if !restored {
            self.human_set_owner(None);
            post.clear_owner_after_resume = true;
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
    }

    // ---------------- refresh（`_refresh_interactive_locked`） ----------------

    pub(super) fn human_peer_present(&self, client_id: &str) -> bool {
        self.human.consoles.contains_key(client_id)
            && self.human.peer_present.get(client_id).copied().unwrap_or(true)
    }

    /// RPC 路徑的完整 refresh。
    pub(super) fn refresh_lease_p6(&mut self, now: Mono) {
        self.refresh_inner(now, true);
    }

    /// `owner_check`：RPC 路徑比照 Python 檢查 `snapshot()["interactive_owner"] == lease.owner`；
    /// hub 掃描路徑只在未 suspend 時檢查（Python 不會在 agent 命令期間週期 refresh，否則會誤拆 human lease）。
    fn refresh_inner(&mut self, now: Mono, rpc: bool) {
        let Some(lease) = self.lease.as_ref() else {
            return;
        };
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
        let before = self.human.ownership.interactive_owner.clone();
        self.human.ownership.on_detach(client_id);
        self.after_owner_change(before);
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
        self.refresh_inner(now, false);
        self.self_heal(now);
    }

    fn self_heal(&mut self, now: Mono) {
        let o = &self.human.ownership;
        if o.interactive_owner.is_some() || o.suspended_owner.is_some() || o.agent_active || o.flash_mode {
            return;
        }
        let Some(primary) = self.human.primary_client_id.clone() else {
            return;
        };
        if !self.human_peer_present(&primary) {
            return;
        }
        if !matches!(self.state, SessionState::Ready | SessionState::Attached) || self.lease.is_some() {
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
        self.lease = Some(InteractiveLease::new(id.clone(), owner, self.wall_iso(), timeout, now, false));
        self.lease_index(LeaseIndexOp::Insert { interactive_id: id });
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
    fn stash_human_and_suspend(&mut self) {
        if let Some(existing) = self.lease.take() {
            self.lease_index(LeaseIndexOp::Remove {
                interactive_id: existing.interactive_id.clone(),
            });
            self.stashed_human = Some(existing);
        }
        let before = self.human.ownership.interactive_owner.clone();
        self.human.ownership.suspend();
        self.after_owner_change(before);
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
                        self.stash_human_and_suspend();
                        let id = self.open_lease_full(owner, timeout_s, false, true, now);
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
        let health = self.worker_health.as_ref().map(|probe| probe());
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
                self.stash_human_and_suspend();
                self.open_lease_full(owner, clamped, true, true, now)
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
        let Some((owner, timeout_s)) = self.retained_human.take() else {
            return;
        };
        let Some(cid) = owner.strip_prefix("human:").filter(|c| !c.is_empty()) else {
            return;
        };
        if !self.human.consoles.contains_key(cid) {
            return;
        }
        self.refresh_lease_p6(now);
        if self.lease.is_none() {
            self.open_lease(&owner, timeout_s, false, now);
        }
    }

    pub(super) fn fg_origin_is_human(&self) -> bool {
        matches!(self.fg_origin, Some(SubmitOrigin::HumanConsole { .. }))
    }

    /// human 來源 open-or-refresh lease（`_refresh_interactive_locked`＋`_open_interactive_locked`）；
    /// `#56` tick 還原結算前回 `Err(SESSION_INTERACTIVE_BUSY)`。
    fn human_promote_lease(&mut self, timeout_s: f64, now: Mono) -> Result<String, PyDict> {
        if self.tick_close_hold.is_some() {
            return Err(pydict! { "ok" => false, "error_code" => "SESSION_INTERACTIVE_BUSY" });
        }
        self.refresh_lease_p6(now);
        if self.lease.is_none() {
            let source = match &self.fg_origin {
                Some(SubmitOrigin::HumanConsole { client_id }) => format!("human:{client_id}"),
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
