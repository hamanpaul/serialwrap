//! P5-T13：boot quiet gate 與 Ready Reconfirm 期限（紅燈階段骨架）。
//!
//! 函式本體為 `s21_todo!`，由 Task 13 實作者補上（05 §7）。
//! `valid_completion_regex` 為 P3 已交付的 `sw_proto::pyre` 函式，此處只重新匯出。

use sw_proto::config_types::SessionProfile;

use crate::Mono;

pub use sw_proto::pyre::valid_completion_regex;

/// 開機 banner 特徵（05 §7，逐字）。
pub const BOOT_BANNER_PATTERNS: [&str; 3] =
    ["U-Boot", "Hit any key to stop autoboot", "CFE version "];

/// boot quiet 與 Ready Reconfirm 的狀態（05 §7）。
#[derive(Default, Debug, Clone)]
pub struct QuietState {
    pub boot_quiet_until: Mono,
    pub boot_banner_tail: String,
    pub pending: bool,
    pub deadline: Mono,
    pub attempts: u32,
    pub failed: bool,
}

/// `on_rx_text` 的狀態轉移結果。
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum QuietTransition {
    Armed,
    Cleared,
    None,
}

impl QuietState {
    /// until = now+180 s；tail=""；未 pending → pending、deadline=now+300 s、attempts=0、failed=false。
    pub fn arm(&mut self, _now: Mono) {
        s21_todo!("T13", "QuietState::arm")
    }

    /// 只清 until／tail。
    pub fn clear_quiet(&mut self) {
        s21_todo!("T13", "QuietState::clear_quiet")
    }

    /// Ready 再確認成功：清除 pending、deadline、attempts、failed 與 quiet。
    pub fn confirm_ready(&mut self) {
        s21_todo!("T13", "QuietState::confirm_ready")
    }

    /// 期限檢查；`force` 表示不依時間、直接判定耗盡。
    pub fn expire(&mut self, _now: Mono, _force: bool) -> bool {
        s21_todo!("T13", "QuietState::expire")
    }

    /// quiet 視窗仍有效（`now < boot_quiet_until`）。
    pub fn quiet_active(&self, _now: Mono) -> bool {
        s21_todo!("T13", "QuietState::quiet_active")
    }

    /// quiet 剩餘秒數（`py_round(…, 1)`）；未 armed 為 None。
    pub fn remaining_s(&self, _now: Mono) -> Option<f64> {
        s21_todo!("T13", "QuietState::remaining_s")
    }

    /// Ready Reconfirm 剩餘秒數（`py_round(…, 1)`）；未 pending 為 None。
    pub fn reconfirm_remaining_s(&self, _now: Mono) -> Option<f64> {
        s21_todo!("T13", "QuietState::reconfirm_remaining_s")
    }

    /// RX 文字進入 quiet 判定（05 §7 arm／clear 點）。
    pub fn on_rx_text(
        &mut self,
        _chunk: &str,
        _profile: &SessionProfile,
        _now: Mono,
    ) -> QuietTransition {
        s21_todo!("T13", "QuietState::on_rx_text")
    }
}

/// 文字是否含開機 banner（跨 chunk 以呼叫端的 tail 組合後判定）。
pub fn detect_boot_banner(_text: &str) -> bool {
    s21_todo!("T13", "detect_boot_banner")
}

/// 05 §5.2 step 2：命令是否為重開機類（`reboot`、`shutdown -r`、`systemctl reboot`、sysrq `b` 等）。
pub fn is_reboot_command(_cmd: &str) -> bool {
    s21_todo!("T13", "is_reboot_command")
}

/// Slice 2.1 不支援、不得自動送 TX 的 profile 能力（`"post_login_cmd"` | `"console_unlock_env"`）。
pub fn slice21_unsupported_feature(_p: &SessionProfile) -> Option<&'static str> {
    s21_todo!("T13", "slice21_unsupported_feature")
}

// ── P7 Task 0（08-slice23-login-events.md）凍結的新增公開項；本體由標示的 Task 實作 ──

/// `quiet_active && console_unlock_env.is_none()`（`session_manager.py:1193`）；passthrough 恆 false（SC-35）。
pub fn boot_quiet_blocks_probe(_q: &QuietState, _sp: &SessionProfile, _now: Mono) -> bool {
    todo!("P7 Task 4")
}

impl QuietState {
    /// 先 `expire(now, false)`，再 `quiet_active || pending`。
    pub fn agent_gate_active(&mut self, _now: Mono) -> bool {
        todo!("P7 Task 4")
    }
}

/// `service._resolve_session_id` 的 gate 段。
pub fn submit_gate_reject(
    _public_session: &sw_proto::pyjson::PyDict,
    _source: &str,
) -> Option<sw_proto::pyjson::PyDict> {
    todo!("P7 Task 4")
}

/// `_agent_gate_reject_locked`＋SC-03 `retry_after_s`。
pub fn execute_gate_reject(_q: &mut QuietState, _com: &str, _now: Mono) -> Option<sw_proto::pyjson::PyDict> {
    todo!("P7 Task 4")
}

/// READY 確認逾 300 s／5 次的提示文字（字面量；測試另以常數值核對 300／5）。
pub const READY_UNCONFIRMED_HINT: &str = concat!(
    "READY 已逾 300s／5 次未能經 nonce probe 再確認（可能卡在 bootloader）；",
    "請 session self-test 取分類後 interactive-open 處理"
);
