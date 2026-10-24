//! ATTACHED 救援判定（P6 Task 7；自 06 T14 抽出，行為不變，P7 login FSM 必須重用、不得另寫）。
//!
//! Task 1 骨架：型別與簽章凍結，函式本體為 `todo!("P6 Task 7")`。

use sw_proto::config_types::SessionProfile;

/// `session_manager.py:125-152`：RX 尾端命中任一 bootloader prompt 時回該 prompt。
pub fn matches_any_bootloader_prompt(_rx_tail: &str, _patterns: &[String]) -> Option<String> {
    todo!("P6 Task 7")
}

/// `login_fsm.py:140-162`。
pub fn matches_login_or_password(_text: &str, _login_regex: &str, _password_regex: &str) -> bool {
    todo!("P6 Task 7")
}

/// 05 §10.1 ATTACHED 分支的判定結果。
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum RescueClass {
    BootloaderPrompt,
    BootInterrupt,
    LoginRequired,
    NotBootloader,
}

/// 05 §10.1 ATTACHED 分支順序。
pub fn classify_attached_rescue(_rx_tail_raw: &str, _profile: &SessionProfile) -> RescueClass {
    todo!("P6 Task 7")
}
