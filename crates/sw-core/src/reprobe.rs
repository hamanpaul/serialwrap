//! Readiness reprobe 與 bootloader 證據（P7 Task 5；`reconcile_readiness`、`_prepare_reprobe_locked`、
//! `_finish_probe_reprobe`、`_reprobe_target_still_valid_locked`、`_bootloader_evidence`，05 §6）。
//! P7 Task 0 骨架：本體為 `todo!("P7 Task 5")`。

use sw_proto::config_types::SessionProfile;

use crate::quiet_gate::QuietState;
use crate::Mono;

#[derive(Clone, Debug, Default, PartialEq)]
pub struct ReprobeState {
    pub attempts: u32,
    pub next_reprobe_at: Option<Mono>,
    pub exhausted: bool,
    pub inflight: bool,
}

#[derive(Clone, Copy, Debug, PartialEq, Eq)]
pub enum ReprobeAction {
    Probe,
    Attach,
}

pub struct ReprobeInput<'a> {
    pub state: &'a str,
    pub by_id: &'a str,
    pub released: bool,
    pub device_present: bool,
    pub last_error: Option<&'a str>,
    pub last_rx_mono: Option<Mono>,
    pub human_active: bool,
    pub has_bridge: bool,
    pub profile: &'a SessionProfile,
}

/// `_reprobe_target_still_valid_locked` 的輸入。
pub struct ValidInput<'a> {
    pub same_bridge: bool,
    pub by_id: &'a str,
    pub released: bool,
    pub device_present: bool,
    pub state: &'a str,
    pub last_error: Option<&'a str>,
    pub human_active: bool,
    pub rx_tail_512: &'a str,
    pub profile: &'a SessionProfile,
}

pub fn is_reprobe_prompt_error(_state: &str, _last_error: Option<&str>) -> bool {
    todo!("P7 Task 5")
}

/// `_prepare_reprobe_locked` 13 步。
pub fn prepare(_r: &mut ReprobeState, _q: &mut QuietState, _inp: &ReprobeInput, _now: Mono) -> Option<ReprobeAction> {
    todo!("P7 Task 5")
}

/// attempts+=1；>=10 → exhausted；否則 next=now+min(2*2^(attempts-1),15)。
pub fn record_attempt(_r: &mut ReprobeState, _now: Mono) {
    todo!("P7 Task 5")
}

pub fn reset(_r: &mut ReprobeState) {
    todo!("P7 Task 5")
}

/// `session_manager.py:1507-1545`；READY＋bootloader 尾行 → force expire、回 false、`last_error` 不動（D1）。
pub fn target_still_valid(_r: &ReprobeState, _q: &mut QuietState, _inp: &ValidInput, _now: Mono) -> bool {
    todo!("P7 Task 5")
}

pub struct ProbeFinish {
    pub new_state: String,
    pub last_error: Option<String>,
    pub last_error_detail: Option<String>,
}

pub fn finish_probe(
    _r: &mut ReprobeState,
    _q: &mut QuietState,
    _became_ready: bool,
    _bootloader_match: Option<String>,
    _now: Mono,
) -> Option<ProbeFinish> {
    todo!("P7 Task 5")
}

/// `clean_text` 後，`bootloader_prompts` 或 `UBOOT_FALLBACK_PROMPTS`。
pub fn bootloader_evidence(_rx_tail_512: &str, _sp: &SessionProfile) -> Option<String> {
    todo!("P7 Task 5")
}
