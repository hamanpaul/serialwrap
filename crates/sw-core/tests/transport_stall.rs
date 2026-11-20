//! P7 Task 6（08-slice23-login-events.md）：transport stall 分類與 host 層復原提示（`transport_stall.py`、
//! `_refine_probe_failure`）。斷言值以 Python golden `contracts/golden/p7/transport_stall.ndjson` 與 CPython 3.12 實測為準。
//! 偏離計畫原文：`support::{golden_rows, t}` 放 `tests/common/p7.rs`／本檔。
mod common;

use common::p7::golden_rows;
use sw_core::transport_stall::*;
use sw_core::Mono;
use sw_pal::fakes::usb::FakeUsb;
use sw_proto::pyjson::PyValue;

const STALL: &str = "TRANSPORT_STALL";
const PU: &str = "PROMPT_UNAVAILABLE";

fn t(s: f64) -> Mono {
    (s * 1e9).round() as Mono
}

fn num(v: &PyValue) -> f64 {
    match v {
        PyValue::Float(f) => *f,
        PyValue::Int(_) => v.as_i64().unwrap() as f64,
        other => panic!("{other:?}"),
    }
}

#[test]
fn hint_text_is_byte_identical() {
    for row in golden_rows("transport_stall")
        .into_iter()
        .filter(|r| r.str("kind") == "hint")
    {
        assert_eq!(
            transport_stall_hint(row.opt_str("busid"), num(&row["rx_age_s"])),
            row.str("text"),
            "case {}",
            row.id()
        );
    }
}

#[test]
fn refinable_and_classify_golden_table() {
    for row in golden_rows("transport_stall") {
        match row.str("kind") {
            "refinable" => assert_eq!(
                is_transport_refinable_error(row.str("err")),
                row.bool("refinable"),
                "case {}",
                row.id()
            ),
            "classify" => {
                // Python last_rx_mono 0.0 表示從未有 RX。
                let last = num(&row["last_rx_mono"]);
                let last = (last > 0.0).then(|| t(last));
                let got = classify_probe_failure(
                    row.str("err"),
                    row["rx_delta"].as_i64().unwrap() as u64,
                    last,
                    t(num(&row["now"])),
                );
                assert_eq!(
                    got,
                    (row.str("result").to_string(), row.bool("flipped")),
                    "case {}",
                    row.id()
                );
            }
            _ => {}
        }
    }
}

#[test]
fn refine_flips_only_when_zero_rx_and_frozen_30s_and_warns_once() {
    let usb = FakeUsb(Some("1-1".into()));
    let r = refine_probe_failure(
        PU.into(),
        Some(500),
        Some(500),
        Some(t(10.0)),
        t(40.0),
        Some("/dev/ttyUSB0"),
        &usb,
        false,
        "",
        "COM0",
    );
    assert_eq!(r.err, STALL);
    let detail = r.detail.clone().unwrap();
    assert!(detail.contains("/sys/bus/usb/devices/1-1/authorized"));
    let w = r.stall_warning.expect("首次須告警");
    assert_eq!(
        w.log_line,
        format!("session COM0 疑似 transport stall（零 RX 30.0s、probe 全程無 echo，real_path=/dev/ttyUSB0）：{detail}")
    );
    assert_eq!(
        sw_proto::pyjson::dumps_dict(&w.wal_meta, &sw_proto::pyjson::RPC_WIRE),
        r#"{"event":"transport_stall_suspected","real_path":"/dev/ttyUSB0","rx_age_s":30.0}"#
    );
    let again = refine_probe_failure(
        PU.into(),
        Some(500),
        Some(500),
        Some(t(10.0)),
        t(41.0),
        Some("/dev/ttyUSB0"),
        &usb,
        true,
        "",
        "COM0",
    );
    assert_eq!(again.err, STALL);
    assert!(again.stall_warning.is_none(), "transport_stall_warned 去重");
    let not = refine_probe_failure(
        PU.into(),
        Some(500),
        Some(500),
        Some(t(10.0)),
        t(39.99),
        Some("/dev/ttyUSB0"),
        &usb,
        false,
        "",
        "COM0",
    );
    assert_eq!(
        (not.err.as_str(), not.detail, not.stall_warning),
        (PU, None, None)
    );
}

#[test]
fn refine_without_real_path_uses_dash_and_generic_hint() {
    let usb = FakeUsb(None);
    let r = refine_probe_failure(
        PU.into(),
        Some(1),
        Some(1),
        Some(t(1.0)),
        t(100.0),
        None,
        &usb,
        false,
        "",
        "COM3",
    );
    let w = r.stall_warning.unwrap();
    assert!(w.log_line.contains("real_path=-）"), "{}", w.log_line);
    assert_eq!(w.wal_meta.get("real_path"), Some(&PyValue::from("")));
    assert!(r
        .detail
        .unwrap()
        .contains("/sys/bus/usb/devices/<busid>/authorized"));
}

#[test]
fn login_detail_attached_when_not_flipped() {
    let usb = FakeUsb(None);
    let tail = format!("{}\x1b[31mroot@x:~# \x1b[0m", "y".repeat(400));
    let r = refine_probe_failure(
        "READY_NONCE_TIMEOUT".into(),
        Some(10),
        Some(15),
        Some(t(1.0)),
        t(100.0),
        None,
        &usb,
        false,
        &tail,
        "COM0",
    );
    assert_eq!(r.err, "READY_NONCE_TIMEOUT");
    let d = r.detail.unwrap();
    assert_eq!(d.chars().count(), 300);
    assert!(d.ends_with("root@x:~# "));
    let empty = refine_probe_failure(
        "READY_NONCE_TIMEOUT".into(),
        Some(10),
        Some(15),
        Some(t(1.0)),
        t(100.0),
        None,
        &usb,
        false,
        "",
        "COM0",
    );
    assert_eq!(empty.detail, None);
}

#[test]
fn flood_value_not_in_detail_set_and_never_rx_is_not_stall() {
    let usb = FakeUsb(None);
    let flood = refine_probe_failure(
        "RX_FLOOD".into(),
        Some(10),
        Some(99999),
        Some(t(1.0)),
        t(100.0),
        None,
        &usb,
        false,
        "abc",
        "COM0",
    );
    assert_eq!((flood.err.as_str(), flood.detail), ("RX_FLOOD", None));
    let never = refine_probe_failure(
        PU.into(),
        Some(0),
        Some(0),
        None,
        t(100.0),
        None,
        &usb,
        false,
        "",
        "COM0",
    );
    assert_eq!(
        (never.err.as_str(), never.stall_warning.is_none()),
        (PU, true)
    );
}

#[test]
fn rx_before_none_returns_err_without_detail() {
    let usb = FakeUsb(None);
    let r = refine_probe_failure(
        "READY_NONCE_TIMEOUT".into(),
        None,
        Some(5),
        Some(t(1.0)),
        t(100.0),
        None,
        &usb,
        false,
        "tail",
        "COM0",
    );
    assert_eq!(
        (r.err.as_str(), r.detail, r.stall_warning),
        ("READY_NONCE_TIMEOUT", None, None)
    );
}

#[test]
fn wal_meta_rx_age_rounded() {
    // Python `round(31.05, 1)` == 31.1（二進位 31.05000000000000071）。
    let usb = FakeUsb(None);
    let r = refine_probe_failure(
        PU.into(),
        Some(0),
        Some(0),
        Some(t(10.0)),
        t(41.05),
        None,
        &usb,
        false,
        "",
        "COM0",
    );
    let w = r.stall_warning.unwrap();
    assert_eq!(
        w.wal_meta.get("rx_age_s"),
        Some(&PyValue::Float(sw_proto::pycompat::py_round(31.05, 1)))
    );
}

#[test]
fn refine_regressing_rx_counter_is_not_zero_rx() {
    // 審查 c11aa89_92e0223 L-1：Python `rx_after - rx_before` 為負 → `!= 0` → 不翻轉。
    let usb = FakeUsb(Some("1-1".into()));
    let r = refine_probe_failure(
        PU.into(),
        Some(500),
        Some(400),
        Some(t(10.0)),
        t(40.0),
        Some("/dev/ttyUSB0"),
        &usb,
        false,
        "",
        "COM0",
    );
    assert_eq!(r.err, PU);
    assert!(r.stall_warning.is_none());
}
