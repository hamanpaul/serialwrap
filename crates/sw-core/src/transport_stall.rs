//! Transport stall 精煉（P7 Task 6；`transport_stall.py`、`_refine_probe_failure`，05 §8.4、10 G-09）。

use sw_pal::traits::UsbTopology;
use sw_proto::pycompat::{clean_text, py_round};
use sw_proto::pyfmt::py_repr_float;
use sw_proto::pyjson::{PyDict, PyValue};

use crate::login_fsm::LOGIN_FSM_DETAIL_ERRORS;

use crate::Mono;

/// `TRANSPORT_STALL_MIN_RX_AGE_S`（30 s）。
const TRANSPORT_STALL_MIN_RX_AGE_NS: Mono = 30_000_000_000;

/// `PROMPT_UNAVAILABLE`、`LOGIN_PROMPT_TIMEOUT`、`*_PROMPT_TIMEOUT`。
pub fn is_transport_refinable_error(err: &str) -> bool {
    err == "PROMPT_UNAVAILABLE" || err == "LOGIN_PROMPT_TIMEOUT" || err.ends_with("_PROMPT_TIMEOUT")
}

/// `classify_probe_failure`：可精煉碼＋probe 全程零 RX＋曾有 RX（`last_rx_mono` 非 None）＋RX 凍結 ≥ 30 s → `(TRANSPORT_STALL, true)`。
pub fn classify_probe_failure(
    err: &str,
    rx_delta: u64,
    last_rx_mono: Option<Mono>,
    now: Mono,
) -> (String, bool) {
    if err.is_empty() || !is_transport_refinable_error(err) || rx_delta != 0 {
        return (err.to_string(), false);
    }
    let Some(last) = last_rx_mono else {
        return (err.to_string(), false);
    };
    if now.saturating_sub(last) < TRANSPORT_STALL_MIN_RX_AGE_NS || now < last {
        return (err.to_string(), false);
    }
    ("TRANSPORT_STALL".to_string(), true)
}

/// 10 G-09 逐字（`transport_stall_hint`；busid 由呼叫端以 `UsbTopology` 解析）。
pub fn transport_stall_hint(busid: Option<&str>, rx_age_s: f64) -> String {
    let base = format!(
        "零 RX {} 秒且 probe 期間連 echo 都無：疑似 USB/usbip read-endpoint stall（dmesg 常見 `urb stopped: -32`）；\
         serialwrap 的 recover/release+attach 無法自復，需 host 層 USB re-enumeration。\
         亦可能為 DUT 斷電/當機，請先以 dmesg 佐證再動作。",
        py_repr_float(py_round(rx_age_s, 1))
    );
    match busid.filter(|b| !b.is_empty()) {
        Some(busid) => format!(
            "{base} 復原指令：sudo sh -c 'echo 0 > /sys/bus/usb/devices/{busid}/authorized; \
             echo 1 > /sys/bus/usb/devices/{busid}/authorized'（usbip 環境亦可 usbipd detach/attach 後重新 attach）。"
        ),
        None => format!(
            "{base} 復原方式：對該 USB 裝置做 authorized 0→1 toggle\
             （/sys/bus/usb/devices/<busid>/authorized）或 usbipd detach/attach 後重新 attach。"
        ),
    }
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
    err: String,
    rx_before: Option<u64>,
    rx_after: Option<u64>,
    last_rx_mono: Option<Mono>,
    now: Mono,
    real_path: Option<&str>,
    usb: &dyn UsbTopology,
    already_warned: bool,
    rx_tail_4096: &str,
    com: &str,
) -> Refined {
    let (Some(before), Some(after)) = (rx_before, rx_after) else {
        return Refined {
            err,
            detail: None,
            stall_warning: None,
        };
    };
    // Python `rx_after - rx_before` 可為負（計數倒退）→ 非零、不翻轉；不可飽和截成 0。
    let rx_delta = after.checked_sub(before).unwrap_or(1);
    let (refined, flipped) = classify_probe_failure(&err, rx_delta, last_rx_mono, now);
    if !flipped {
        let detail = if LOGIN_FSM_DETAIL_ERRORS.contains(&err.as_str()) {
            rx_tail_detail(rx_tail_4096)
        } else {
            None
        };
        return Refined {
            err,
            detail,
            stall_warning: None,
        };
    }
    let last = last_rx_mono.unwrap_or(0);
    let rx_age_s = now.saturating_sub(last) as f64 / 1_000_000_000.0;
    let busid = real_path.and_then(|p| usb.usb_busid(p));
    let detail = transport_stall_hint(busid.as_deref(), rx_age_s);
    let stall_warning = (!already_warned).then(|| {
        let mut wal_meta = PyDict::new();
        wal_meta.insert("event", PyValue::from("transport_stall_suspected"));
        wal_meta.insert("real_path", PyValue::from(real_path.unwrap_or("")));
        wal_meta.insert("rx_age_s", PyValue::Float(py_round(rx_age_s, 1)));
        StallWarning {
            log_line: format!(
                "session {com} 疑似 transport stall（零 RX {rx_age_s:.1}s、probe 全程無 echo，real_path={}）：{detail}",
                real_path.filter(|p| !p.is_empty()).unwrap_or("-")
            ),
            wal_meta,
        }
    });
    Refined {
        err: refined,
        detail: Some(detail),
        stall_warning,
    }
}

/// `_login_fsm_rx_tail_detail`：`clean_text(rx_tail)` 最後 300 字元；空 → None。
fn rx_tail_detail(raw: &str) -> Option<String> {
    if raw.is_empty() {
        return None;
    }
    let cleaned = clean_text(raw);
    let count = cleaned.chars().count();
    let tail: String = cleaned.chars().skip(count.saturating_sub(300)).collect();
    (!tail.is_empty()).then_some(tail)
}
