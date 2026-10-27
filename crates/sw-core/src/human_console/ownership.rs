//! Human console ownership 狀態機（P6 Task 6；`uart_io.py:1310-1481` 的 bridge 層規則）。
//!

use crate::Mono;

/// raw ownership、admission gate、suspend 巢狀計數與 pending owner。
#[derive(Debug, Default, Clone, PartialEq)]
pub struct OwnershipState {
    pub interactive_owner: Option<String>,
    pub admission_blocked: bool,
    pub pending_owner: Option<String>,
    pub agent_active: bool,
    pub suspended_owner: Option<String>,
    pub suspend_depth: u32,
    pub last_human_input_at: Option<Mono>,
    pub flash_mode: bool,
}

/// `_handle_console_rx` 步驟 2～5 的路由結果。
#[derive(Debug, Clone, PartialEq, Eq)]
pub enum InputRoute {
    DropFlash,
    Raw { owner: String },
    Defer,
    Line,
}

impl OwnershipState {
    /// `uart_io.py` `set_interactive_owner`；回 true＝`interactive_owner` 實際改變（pending 變更不算）。
    ///
    /// admission 只攔 human raw grant（改記 pending）；agent owner 照常生效。
    pub fn set_interactive_owner(&mut self, owner: Option<&str>) -> bool {
        if let Some(o) = owner {
            if o.starts_with("human:") && self.admission_blocked {
                self.pending_owner = Some(o.to_string());
                return false;
            }
        }
        let changed = self.interactive_owner.as_deref() != owner;
        self.interactive_owner = owner.map(str::to_string);
        if owner.is_none() {
            self.pending_owner = None;
        }
        changed
    }

    /// `try_grant_interactive_if_idle`：完全 idle 才授予。
    pub fn try_grant_if_idle(&mut self, owner: &str) -> bool {
        if self.interactive_owner.is_none()
            && !self.admission_blocked
            && self.pending_owner.is_none()
            && self.suspended_owner.is_none()
            && !self.agent_active
            && !self.flash_mode
        {
            self.interactive_owner = Some(owner.to_string());
            return true;
        }
        false
    }

    /// `suspend_interactive`（可重入，#78）；回 true＝depth 0→1。
    pub fn suspend(&mut self) -> bool {
        let outer = self.suspend_depth == 0;
        if outer {
            self.suspended_owner = self.interactive_owner.take();
            self.agent_active = true;
        }
        self.suspend_depth += 1;
        outer
    }

    /// `resume_interactive`；true＝最外層（depth 1→0），呼叫端回放。不平衡的 resume 為 no-op。
    pub fn resume(&mut self, has_client: &dyn Fn(&str) -> bool) -> bool {
        if self.suspend_depth == 0 {
            return false;
        }
        self.suspend_depth -= 1;
        if self.suspend_depth > 0 {
            return false;
        }
        self.interactive_owner = self.suspended_owner.take();
        self.agent_active = false;
        self.promote_pending(has_client);
        true
    }

    /// `_set_interactive_admission`；true＝此刻回放（admission 開放且未 suspend）。
    pub fn set_admission(&mut self, allowed: bool, has_client: &dyn Fn(&str) -> bool) -> bool {
        self.admission_blocked = !allowed;
        if !allowed {
            return false;
        }
        self.promote_pending(has_client);
        self.suspend_depth == 0 && self.suspended_owner.is_none()
    }

    /// `_promote_pending_interactive_owner_locked`。
    pub fn promote_pending(&mut self, has_client: &dyn Fn(&str) -> bool) {
        if self.admission_blocked
            || self.interactive_owner.is_some()
            || self.suspend_depth > 0
            || self.suspended_owner.is_some()
        {
            return;
        }
        let Some(pending) = self.pending_owner.take() else {
            return;
        };
        match pending.strip_prefix("human:") {
            // interactive command 的 agent owner 沒有 console client；仍保留。
            None => self.interactive_owner = Some(pending),
            Some(cid) if !cid.is_empty() && has_client(cid) => self.interactive_owner = Some(pending),
            Some(_) => {}
        }
    }

    /// TCP／com0com accept（`_accept_console_conn`，#134）。
    pub fn on_stream_accept(&mut self, client_id: &str) {
        let owner = format!("human:{client_id}");
        if self.interactive_owner.is_none() && self.suspend_depth == 0 {
            if self.admission_blocked {
                if self.pending_owner.is_none() {
                    self.pending_owner = Some(owner);
                }
            } else {
                self.interactive_owner = Some(owner);
            }
        } else if self.suspend_depth > 0 && self.suspended_owner.is_none() {
            self.suspended_owner = Some(owner);
        }
    }

    /// `detach_console`（#78）：被 suspend 的 owner 斷線時放棄整個 suspend 簿記。
    pub fn on_detach(&mut self, client_id: &str) {
        let me = format!("human:{client_id}");
        if self.interactive_owner.as_deref() == Some(me.as_str()) {
            self.interactive_owner = None;
        }
        if self.pending_owner.as_deref() == Some(me.as_str()) {
            self.pending_owner = None;
        }
        if self.suspended_owner.as_deref() == Some(me.as_str()) {
            self.suspended_owner = None;
            self.agent_active = false;
            self.suspend_depth = 0;
        }
    }

    /// `_drop_console_client`（#136）：只讓位 suspended owner，不歸零 suspend 簿記。
    pub fn on_drop(&mut self, client_id: &str) {
        let me = format!("human:{client_id}");
        if self.interactive_owner.as_deref() == Some(me.as_str()) {
            self.interactive_owner = None;
        }
        if self.pending_owner.as_deref() == Some(me.as_str()) {
            self.pending_owner = None;
        }
        if self.suspended_owner.as_deref() == Some(me.as_str()) {
            self.suspended_owner = None;
        }
    }

    /// `_prune_stale_consoles_locked`：只清 owner。
    pub fn on_prune(&mut self, client_id: &str) {
        if self.interactive_owner.as_deref() == Some(format!("human:{client_id}").as_str()) {
            self.interactive_owner = None;
        }
    }

    /// bridge.stop：清 owner／pending／suspend／depth／agent_active／admission；保留 flash_mode。
    pub fn reset_for_stop(&mut self) {
        *self = OwnershipState {
            flash_mode: self.flash_mode,
            ..OwnershipState::default()
        };
    }

    /// `_handle_console_rx` 步驟 2～5；只有 Raw 更新 `last_human_input_at`。
    pub fn route_input(&mut self, client_id: &str, now: Mono) -> InputRoute {
        let route = self.peek_route(client_id);
        if matches!(route, InputRoute::Raw { .. }) {
            self.last_human_input_at = Some(now);
        }
        route
    }

    /// 與 `route_input` 相同的判定，但不更新任何狀態（D-15(4) 的回放條件用）。
    pub fn peek_route(&self, client_id: &str) -> InputRoute {
        if self.flash_mode {
            return InputRoute::DropFlash;
        }
        let me = format!("human:{client_id}");
        if self.interactive_owner.as_deref() == Some(me.as_str()) {
            if self.admission_blocked {
                return InputRoute::Defer;
            }
            return InputRoute::Raw { owner: me };
        }
        if self.agent_active && self.suspended_owner.as_deref() == Some(me.as_str()) {
            return InputRoute::Defer;
        }
        InputRoute::Line
    }
}
