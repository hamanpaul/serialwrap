//! Transport stall 精煉（P7 Task 6；`transport_stall.py`、`_refine_probe_failure`，05 §8.4、10 G-09）。
//! P7 Task 0 骨架：本體為 `todo!("P7 Task 6")`。

use sw_pal::traits::UsbTopology;
use sw_proto::pyjson::PyDict;

use crate::Mono;

/// `PROMPT_UNAVAILABLE`、`LOGIN_PROMPT_TIMEOUT`、`*_PROMPT_TIMEOUT`。
pub fn is_transport_refinable_error(_err: &str) -> bool {
    todo!("P7 Task 6")
}

pub fn classify_probe_failure(_err: &str, _rx_delta: u64, _last_rx_mono: Option<Mono>, _now: Mono) -> (String, bool) {
    todo!("P7 Task 6")
}

/// 10 G-09 逐字。
pub fn transport_stall_hint(_busid: Option<&str>, _rx_age_s: f64) -> String {
    todo!("P7 Task 6")
}

#[derive(Clone, Debug, PartialEq)]
pub struct Refined {
    pub err: String,
    pub detail: Option<String>,
    pub stall_warning: Option<StallWarning>,
}

/// `{"event":"transport_stall_suspected","real_path":…,"rx_age_s":round1}`。
#[derive(Clone, Debug, PartialEq)]
pub struct StallWarning {
    pub log_line: String,
    pub wal_meta: PyDict,
}

/// `already_warned` 為呼叫端傳入的 `session.transport_stall_warned`；本函式只決定是否產生 warning，
/// 去重狀態由 actor 在套用 outcome 時寫回（Task 7）。
#[allow(clippy::too_many_arguments)]
pub fn refine_probe_failure(
    _err: String,
    _rx_before: Option<u64>,
    _rx_after: Option<u64>,
    _last_rx_mono: Option<Mono>,
    _now: Mono,
    _real_path: Option<&str>,
    _usb: &dyn UsbTopology,
    _already_warned: bool,
    _rx_tail_4096: &str,
    _com: &str,
) -> Refined {
    todo!("P7 Task 6")
}
