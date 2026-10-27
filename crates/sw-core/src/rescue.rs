//! ATTACHED 救援判定（P6 Task 7；自 06 T14 抽出，行為不變，P7 login FSM 必須重用、不得另寫）。

use sw_proto::config_types::SessionProfile;
use sw_proto::pycompat::clean_text;
use sw_proto::pyre::{compile, PyFlags};

/// `session_manager.py::_matches_any_bootloader_prompt`：最後一個非空白行命中任一 pattern 時回該 pattern；
/// 空輸入、空 patterns 回 `None`；不合法 regex 略過。
pub fn matches_any_bootloader_prompt(rx_tail: &str, patterns: &[String]) -> Option<String> {
    crate::quiet_gate::matches_any_bootloader_prompt(rx_tail, patterns)
}

/// `login_fsm.py::matches_login_or_password`：先比 `clean_text` 後再比原文；空 pattern 與不合法 regex 略過。
pub fn matches_login_or_password(text: &str, login_regex: &str, password_regex: &str) -> bool {
    if text.is_empty() {
        return false;
    }
    let cleaned = clean_text(text);
    [login_regex, password_regex]
        .into_iter()
        .filter(|pattern| !pattern.is_empty())
        .any(|pattern| match compile(pattern, PyFlags(0)) {
            Ok(re) => re.search(&cleaned).is_some() || re.search(text).is_some(),
            Err(_) => false,
        })
}

/// 05 §10.1 ATTACHED 分支的判定結果。
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum RescueClass {
    BootloaderPrompt,
    BootInterrupt,
    LoginRequired,
    NotBootloader,
}

/// 05 §10.1 ATTACHED 分支順序：bootloader prompt（只用 profile 的 `bootloader_prompts`，不用 fallback）→
/// boot banner（#114）→ login／password（#174）→ NOT_BOOTLOADER。
pub fn classify_attached_rescue(rx_tail_raw: &str, profile: &SessionProfile) -> RescueClass {
    let clean = clean_text(rx_tail_raw);
    if matches_any_bootloader_prompt(&clean, &profile.template.bootloader_prompts).is_some() {
        RescueClass::BootloaderPrompt
    } else if crate::quiet_gate::detect_boot_banner(&clean) {
        RescueClass::BootInterrupt
    } else if matches_login_or_password(&clean, &profile.template.login_regex, &profile.template.password_regex) {
        RescueClass::LoginRequired
    } else {
        RescueClass::NotBootloader
    }
}
