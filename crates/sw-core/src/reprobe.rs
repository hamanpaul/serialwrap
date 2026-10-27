//! Readiness reprobe 與 bootloader 證據（P7 Task 5；`reconcile_readiness`、`_prepare_reprobe_locked`、
//! `_finish_probe_reprobe`、`_reprobe_target_still_valid_locked`、`_bootloader_evidence`，05 §6）。

use sw_proto::config_types::SessionProfile;

use sw_proto::pycompat::clean_text;

use crate::quiet_gate::{boot_quiet_blocks_probe, matches_any_bootloader_prompt, QuietState};
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

/// `REPROBE_RX_IDLE_S`（3 s）。
const REPROBE_RX_IDLE_NS: Mono = 3_000_000_000;
/// `REPROBE_BACKOFF_S`（2 s）。
const REPROBE_BACKOFF_S: f64 = 2.0;
/// `REPROBE_MAX_INTERVAL_S`（15 s）。
const REPROBE_MAX_INTERVAL_S: f64 = 15.0;
/// `REPROBE_MAX_ATTEMPTS`。
pub const REPROBE_MAX_ATTEMPTS: u32 = 10;
/// `UBOOT_FALLBACK_PROMPTS`。
pub const UBOOT_FALLBACK_PROMPTS: [&str; 3] = ["^=> $", "^U-Boot> $", "^CFE> $"];

fn secs_ns(s: f64) -> Mono {
    (s * 1_000_000_000.0).round() as Mono
}

/// `min(REPROBE_BACKOFF_S * 2**max(exp, 0), REPROBE_MAX_INTERVAL_S)`。
fn backoff(exp: u32) -> Mono {
    let factor = 2f64.powi(exp.min(16) as i32);
    secs_ns((REPROBE_BACKOFF_S * factor).min(REPROBE_MAX_INTERVAL_S))
}

/// `_is_reprobe_prompt_error`：ATTACHED 的 prompt 類失敗（含 RX_FLOOD／TRANSPORT_STALL）與 DETACHED 的逾時類失敗才可重探。
pub fn is_reprobe_prompt_error(state: &str, last_error: Option<&str>) -> bool {
    let Some(e) = last_error.filter(|e| !e.is_empty()) else {
        return false;
    };
    let timeout_like = e == "PROMPT_TIMEOUT"
        || e.ends_with("_PROMPT_TIMEOUT")
        || e == "RX_FLOOD"
        || e == "TRANSPORT_STALL";
    match state {
        "ATTACHED" => e == "PROMPT_UNAVAILABLE" || timeout_like,
        "DETACHED" => timeout_like,
        _ => false,
    }
}

fn rx_idle_enough(last_rx_mono: Option<Mono>, now: Mono) -> bool {
    match last_rx_mono {
        None => true,
        Some(last) => now.saturating_sub(last) >= REPROBE_RX_IDLE_NS,
    }
}

/// `_prepare_reprobe_locked` 13 步。`Attach` 時呼叫端負責 `state=ATTACHING`、`last_error=None`（本函式已記一次 attempt）；
/// `Probe` 時設 `inflight`（single-flight）與下次時間。
pub fn prepare(
    r: &mut ReprobeState,
    q: &mut QuietState,
    inp: &ReprobeInput,
    now: Mono,
) -> Option<ReprobeAction> {
    if inp.by_id.is_empty() || inp.released || !inp.device_present {
        return None;
    }
    if matches!(
        inp.state,
        "RELEASED" | "FLASHING" | "ATTACHING" | "RECOVERING"
    ) {
        return None;
    }
    if inp.profile.template.platform == "passthrough" && matches!(inp.state, "ATTACHED" | "READY") {
        return None;
    }
    if inp.state == "READY" {
        q.expire(now, false);
    }
    let blocks = boot_quiet_blocks_probe(q, inp.profile, now);
    let ready_reconfirm = inp.state == "READY" && q.pending && !blocks;
    if inp.state == "READY" && !ready_reconfirm {
        return None;
    }
    if inp.last_error == Some("CREDENTIALS_UNRESOLVED") {
        return None;
    }
    if r.exhausted || r.attempts >= REPROBE_MAX_ATTEMPTS {
        r.exhausted = true;
        r.next_reprobe_at = None;
        if ready_reconfirm {
            q.expire(now, true);
        }
        return None;
    }
    if r.next_reprobe_at.is_some_and(|next| now < next) {
        return None;
    }
    if blocks {
        return None;
    }
    if !ready_reconfirm && !is_reprobe_prompt_error(inp.state, inp.last_error) {
        return None;
    }
    if !rx_idle_enough(inp.last_rx_mono, now) {
        return None;
    }
    if inp.human_active {
        return None;
    }
    match inp.state {
        "ATTACHED" | "READY" => {
            if !inp.has_bridge || r.inflight {
                return None;
            }
            r.next_reprobe_at = Some(now.saturating_add(backoff(r.attempts)));
            r.inflight = true;
            Some(ReprobeAction::Probe)
        }
        "DETACHED" => {
            record_attempt(r, now);
            Some(ReprobeAction::Attach)
        }
        _ => None,
    }
}

/// `_record_reprobe_attempt_locked`：attempts+=1；>=10 → exhausted；否則 next=now+min(2*2^(attempts-1),15)。
pub fn record_attempt(r: &mut ReprobeState, now: Mono) {
    r.attempts = r.attempts.saturating_add(1);
    if r.attempts >= REPROBE_MAX_ATTEMPTS {
        r.exhausted = true;
        r.next_reprobe_at = None;
        return;
    }
    r.next_reprobe_at = Some(now.saturating_add(backoff(r.attempts.saturating_sub(1))));
}

/// `reset_reprobe_progress`（READY 轉移點）。
pub fn reset(r: &mut ReprobeState) {
    r.attempts = 0;
    r.next_reprobe_at = None;
    r.exhausted = false;
}

/// `session_manager.py:2133-2177`；READY＋bootloader 尾行 → force expire、回 false、`last_error` 不動（D1）。
pub fn target_still_valid(
    r: &ReprobeState,
    q: &mut QuietState,
    inp: &ValidInput,
    now: Mono,
) -> bool {
    if !inp.same_bridge {
        return false;
    }
    if inp.by_id.is_empty() || inp.released || !inp.device_present {
        return false;
    }
    if !matches!(inp.state, "ATTACHED" | "READY") {
        return false;
    }
    if inp.profile.template.platform == "passthrough" {
        return false;
    }
    if inp.state == "READY" {
        q.expire(now, false);
        if !q.pending {
            return false;
        }
        if bootloader_evidence(inp.rx_tail_512, inp.profile).is_some() {
            q.expire(now, true);
            return false;
        }
    }
    if r.exhausted {
        return false;
    }
    if inp.state == "ATTACHED" && !is_reprobe_prompt_error(inp.state, inp.last_error) {
        return false;
    }
    if boot_quiet_blocks_probe(q, inp.profile, now) {
        return false;
    }
    !inp.human_active
}

/// `_finish_probe_reprobe` 的 session 欄位變更；`last_error`／`last_error_detail` 為 `None` 表示不變。
pub struct ProbeFinish {
    pub new_state: String,
    pub last_error: Option<String>,
    pub last_error_detail: Option<String>,
}

/// `_finish_probe_reprobe`：`became_ready`（session 或 probe 結果為 READY）→ reset、回 `None`；否則記 attempt，
/// pending 中再確認失敗累加 `q.attempts` 並評估終態；`bootloader_match` → `BOOTLOADER_STUCK`＋exhausted＋force expire。
/// 失敗時 probe 已把 session 收斂成 ATTACHED，`new_state` 恆為 `"ATTACHED"`。
pub fn finish_probe(
    r: &mut ReprobeState,
    q: &mut QuietState,
    became_ready: bool,
    bootloader_match: Option<String>,
    now: Mono,
) -> Option<ProbeFinish> {
    r.inflight = false;
    if became_ready {
        reset(r);
        return None;
    }
    record_attempt(r, now);
    if q.pending {
        q.attempts = q.attempts.saturating_add(1);
        q.expire(now, false);
    }
    let mut fin = ProbeFinish {
        new_state: "ATTACHED".to_string(),
        last_error: None,
        last_error_detail: None,
    };
    if let Some(matched) = bootloader_match {
        fin.last_error = Some("BOOTLOADER_STUCK".to_string());
        fin.last_error_detail = Some(format!("matched_prompt={matched}"));
        r.exhausted = true;
        r.next_reprobe_at = None;
        q.expire(now, true);
    }
    Some(fin)
}

/// `_bootloader_evidence`：`clean_text(rx_tail(512))` 尾行命中 `bootloader_prompts`（空時 `UBOOT_FALLBACK_PROMPTS`）。
pub fn bootloader_evidence(rx_tail_512: &str, sp: &SessionProfile) -> Option<String> {
    let tail = clean_text(rx_tail_512);
    let prompts = &sp.template.bootloader_prompts;
    if prompts.is_empty() {
        let fallback: Vec<String> = UBOOT_FALLBACK_PROMPTS
            .iter()
            .map(|p| p.to_string())
            .collect();
        return matches_any_bootloader_prompt(&tail, &fallback);
    }
    matches_any_bootloader_prompt(&tail, prompts)
}
