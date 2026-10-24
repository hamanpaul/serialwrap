//! Human console ownership 狀態機（P6 Task 6；`uart_io.py:1310-1481` 的 bridge 層規則）。
//!
//! Task 1 骨架：型別與簽章凍結，函式本體為 `todo!("P6 Task 6")`。

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
    /// `uart_io.py:1345-1358`；回 true＝owner 實際改變。
    pub fn set_interactive_owner(&mut self, _owner: Option<&str>) -> bool {
        todo!("P6 Task 6")
    }
    /// `uart_io.py:1464-1481`。
    pub fn try_grant_if_idle(&mut self, _owner: &str) -> bool {
        todo!("P6 Task 6")
    }
    /// `uart_io.py:1360-1376`；回 true＝depth 0→1。
    pub fn suspend(&mut self) -> bool {
        todo!("P6 Task 6")
    }
    /// true＝最外層（depth 1→0），呼叫端回放。
    pub fn resume(&mut self, _has_client: &dyn Fn(&str) -> bool) -> bool {
        todo!("P6 Task 6")
    }
    /// true＝此刻回放。
    pub fn set_admission(&mut self, _allowed: bool, _has_client: &dyn Fn(&str) -> bool) -> bool {
        todo!("P6 Task 6")
    }
    /// `uart_io.py:1310-1329`。
    pub fn promote_pending(&mut self, _has_client: &dyn Fn(&str) -> bool) {
        todo!("P6 Task 6")
    }
    /// TCP／com0com accept（`uart_io.py:393-410`）。
    pub fn on_stream_accept(&mut self, _client_id: &str) {
        todo!("P6 Task 6")
    }
    /// `detach_console`（#78）。
    pub fn on_detach(&mut self, _client_id: &str) {
        todo!("P6 Task 6")
    }
    /// `_drop_console_client`（#136）。
    pub fn on_drop(&mut self, _client_id: &str) {
        todo!("P6 Task 6")
    }
    /// `_prune_stale_consoles_locked`：只清 owner。
    pub fn on_prune(&mut self, _client_id: &str) {
        todo!("P6 Task 6")
    }
    /// bridge.stop：清 owner／pending／suspend／depth／agent_active／admission；保留 flash_mode。
    pub fn reset_for_stop(&mut self) {
        todo!("P6 Task 6")
    }
    /// `_handle_console_rx` 步驟 2～5。
    pub fn route_input(&mut self, _client_id: &str, _now: Mono) -> InputRoute {
        todo!("P6 Task 6")
    }
}
