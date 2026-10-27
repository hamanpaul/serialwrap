//! P5-T13：boot quiet gate 與 Ready Reconfirm 期限（05 §7；對照 `session_manager.py` 的
//! `SessionRuntime.arm_boot_quiet`／`clear_boot_quiet`／`confirm_ready`／`expire_ready_reconfirm`
//! 與 `_update_boot_quiet_locked`）。
//!
//! 本模組為純狀態（sans-IO）；fence 的 `quiet_active`／`reconfirm_pending` 由 `SessionCore` 在同一次
//! `Fence::with` 內同步。`valid_completion_regex` 為 P3 已交付的 `sw_proto::pyre` 函式，此處只重新匯出。

use sw_proto::config_types::SessionProfile;
use sw_proto::pycompat::{py_round, py_strip, shlex_split, splitlines};
use sw_proto::pypath::{PathFlavor, basename};
use sw_proto::pyre::{PyFlags, compile};

use crate::Mono;

pub use sw_proto::pyre::valid_completion_regex;

/// 開機 banner 特徵（05 §7，逐字）。
pub const BOOT_BANNER_PATTERNS: [&str; 3] =
    ["U-Boot", "Hit any key to stop autoboot", "CFE version "];

/// `BOOT_QUIET_WINDOW_S`（180 s）。
const BOOT_QUIET_WINDOW_NS: Mono = 180_000_000_000;
/// `READY_RECONFIRM_MAX_S`（＝quiet 180 s＋120 s）。
const READY_RECONFIRM_MAX_NS: Mono = 300_000_000_000;
/// `READY_RECONFIRM_MAX_ATTEMPTS`。
pub const READY_RECONFIRM_MAX_ATTEMPTS: u32 = 5;
/// `BOOT_BANNER_TAIL_CHARS`。
const BOOT_BANNER_TAIL_CHARS: usize = 256;

/// boot quiet 與 Ready Reconfirm 的狀態（05 §7）。`0` 表示未設定（Python 以 `0.0` 表示）。
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

fn last_chars(text: &str, n: usize) -> &str {
    let count = text.chars().count();
    if count <= n {
        return text;
    }
    let skip = count - n;
    let index = text.char_indices().nth(skip).map_or(text.len(), |(i, _)| i);
    &text[index..]
}

fn py_search(pattern: &str, text: &str) -> Option<bool> {
    compile(pattern, PyFlags(0))
        .ok()
        .map(|re| re.search(text).is_some())
}

impl QuietState {
    /// until = now+180 s；tail=""；未 pending → pending、deadline=now+300 s、attempts=0、failed=false。
    /// 重臂不延長 reconfirm 期限（#162）。
    pub fn arm(&mut self, now: Mono) {
        self.boot_quiet_until = now.saturating_add(BOOT_QUIET_WINDOW_NS);
        self.boot_banner_tail.clear();
        if !self.pending {
            self.pending = true;
            self.deadline = now.saturating_add(READY_RECONFIRM_MAX_NS);
            self.attempts = 0;
            self.failed = false;
        }
    }

    /// 只清 until／tail（不清 reconfirm pending，#162）。
    pub fn clear_quiet(&mut self) {
        self.boot_quiet_until = 0;
        self.boot_banner_tail.clear();
    }

    /// Ready 再確認成功：清除 pending、deadline、attempts、failed 與 quiet。
    pub fn confirm_ready(&mut self) {
        self.clear_quiet();
        self.pending = false;
        self.deadline = 0;
        self.attempts = 0;
        self.failed = false;
    }

    /// 期限／次數檢查（`expire_ready_reconfirm`）；`force` 表示不依時間、直接判定耗盡。回傳本次是否翻轉。
    pub fn expire(&mut self, now: Mono, force: bool) -> bool {
        if !self.pending {
            return false;
        }
        if !force {
            let deadline_hit = self.deadline > 0 && now >= self.deadline;
            let attempts_hit = self.attempts >= READY_RECONFIRM_MAX_ATTEMPTS;
            if !deadline_hit && !attempts_hit {
                return false;
            }
        }
        self.pending = false;
        self.failed = true;
        true
    }

    /// quiet 視窗仍有效（`now < boot_quiet_until`）。
    pub fn quiet_active(&self, now: Mono) -> bool {
        self.boot_quiet_until > 0 && now < self.boot_quiet_until
    }

    /// quiet 剩餘秒數（`py_round(…, 1)`）；未 armed 或已到期為 None。
    pub fn remaining_s(&self, now: Mono) -> Option<f64> {
        self.quiet_active(now).then(|| {
            py_round(
                (self.boot_quiet_until - now) as f64 / 1_000_000_000.0,
                1,
            )
        })
    }

    /// Ready Reconfirm 剩餘秒數（`py_round(max(…, 0), 1)`）；未 pending 為 None。
    pub fn reconfirm_remaining_s(&self, now: Mono) -> Option<f64> {
        if !self.pending || self.deadline == 0 {
            return None;
        }
        Some(py_round(
            self.deadline.saturating_sub(now) as f64 / 1_000_000_000.0,
            1,
        ))
    }

    /// RX 文字（已 `clean_text`）進入 quiet 判定（05 §7 arm／clear 點；`_update_boot_quiet_locked`）。
    /// passthrough session 不得呼叫（SC-35），由呼叫端過濾。
    pub fn on_rx_text(
        &mut self,
        chunk: &str,
        profile: &SessionProfile,
        now: Mono,
    ) -> QuietTransition {
        let mut window = last_chars(&self.boot_banner_tail, BOOT_BANNER_TAIL_CHARS).to_string();
        window.push_str(chunk);
        if detect_boot_banner(&window) {
            self.arm(now);
            return QuietTransition::Armed;
        }
        let tail = last_chars(&window, BOOT_BANNER_TAIL_CHARS).to_string();
        self.boot_banner_tail = tail.clone();
        if self.boot_quiet_until == 0 {
            return QuietTransition::None;
        }
        let t = &profile.template;
        if matches_any_bootloader_prompt(&tail, &t.bootloader_prompts).is_some() {
            return QuietTransition::None;
        }
        for pattern in [t.login_regex.as_str(), t.prompt_regex.as_str()] {
            if pattern.is_empty() {
                continue;
            }
            if py_search(pattern, &tail) == Some(true) {
                self.clear_quiet();
                return QuietTransition::Cleared;
            }
        }
        QuietTransition::None
    }
}

/// `rx_tail` 最後一個非空白行是否命中任一 bootloader regex（`_matches_any_bootloader_prompt`）。
/// 回傳第一個命中的 pattern；不合法的 regex 照 Python 靜默略過（#93）。
pub fn matches_any_bootloader_prompt(rx_tail: &str, patterns: &[String]) -> Option<String> {
    if rx_tail.is_empty() || patterns.is_empty() {
        return None;
    }
    let last_line = splitlines(rx_tail)
        .into_iter()
        .rev()
        .find(|line| !py_strip(line).is_empty())?;
    patterns
        .iter()
        .find(|pattern| py_search(pattern, last_line) == Some(true))
        .cloned()
}

/// 文字是否含開機 banner（大小寫敏感 substring；跨 chunk 以呼叫端的 tail 組合後判定）。
pub fn detect_boot_banner(text: &str) -> bool {
    !text.is_empty() && BOOT_BANNER_PATTERNS.iter().any(|p| text.contains(p))
}

/// 05 §5.2 step 2：命令是否為重開機類（`_is_reboot_command` 逐規則）。
pub fn is_reboot_command(cmd: &str) -> bool {
    const SYSRQ: &str = r#"\A(?:\s*echo\s+(?:b|'b'|"b")\s*>\s*/proc/sysrq-trigger\s*)\Z"#;
    if py_search(SYSRQ, cmd) == Some(true) {
        return true;
    }
    let Ok(tokens) = shlex_split(cmd) else {
        return false;
    };
    let Some(first) = tokens.first() else {
        return false;
    };
    let base = basename(PathFlavor::Posix, first);
    if base == "reboot" {
        return true;
    }
    if base == "systemctl" && tokens.get(1).is_some_and(|t| t == "reboot") {
        return true;
    }
    if base != "shutdown" {
        return false;
    }
    tokens[1..]
        .iter()
        .any(|t| t == "-r" || t == "--reboot" || t.starts_with("-r"))
}

/// Slice 2.1 不支援、不得自動送 TX 的 profile 能力（`"post_login_cmd"` | `"console_unlock_env"`）。
pub fn slice21_unsupported_feature(p: &SessionProfile) -> Option<&'static str> {
    let t = &p.template;
    if !t.post_login_cmd.is_empty() {
        return Some("post_login_cmd");
    }
    if t.console_unlock_env.as_deref().is_some_and(|v| !v.is_empty()) {
        return Some("console_unlock_env");
    }
    None
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
