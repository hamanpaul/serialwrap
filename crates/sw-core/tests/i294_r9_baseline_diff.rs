//! 第 9 輪 i294 逾時 stdout 與基線的 byte-for-byte 對照。

#![cfg(feature = "test-fakes")]

mod support;

use std::time::{Duration, Instant};

use support::*;
use sw_core::session_core::SessionRequest;
use sw_proto::pyjson::PyValue;

fn wait_for_tx(h: &ActorRig, payload: &[u8]) {
    h.wait_for("WAL TX", |rig| {
        rig.wal_tx_payloads().iter().any(|tx| tx == payload)
    });
}

fn decode_base64(value: &str) -> Vec<u8> {
    const ALPHABET: &[u8] = b"ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789+/";
    let mut out = Vec::new();
    let mut accumulator = 0u32;
    let mut bits = 0u32;
    for byte in value.bytes() {
        if byte == b'=' {
            break;
        }
        let value = ALPHABET
            .iter()
            .position(|candidate| *candidate == byte)
            .expect("WAL payload 必須使用標準 base64") as u32;
        accumulator = (accumulator << 6) | value;
        bits += 6;
        if bits >= 8 {
            bits -= 8;
            out.push((accumulator >> bits) as u8);
            accumulator &= (1 << bits) - 1;
        }
    }
    out
}

fn wait_until_done(h: &ActorRig, id: &str) {
    let deadline = Instant::now() + Duration::from_secs(10);
    loop {
        if h.command_get(id)["done_at"].as_str().is_some() {
            return;
        }
        assert!(Instant::now() < deadline, "逾時命令未收斂：{id}");
        h.advance(Duration::from_millis(100));
    }
}

fn wait_until_tx(h: &ActorRig, payload: &[u8]) {
    let deadline = Instant::now() + Duration::from_secs(15);
    loop {
        if h.wal_tx_payloads().iter().any(|tx| tx == payload) {
            return;
        }
        assert!(Instant::now() < deadline, "未觀察到 TX：{payload:?}");
        h.advance(Duration::from_millis(500));
        std::thread::yield_now();
    }
}

fn inject_and_wait_for_rx(h: &ActorRig, bytes: &[u8]) {
    let end = h.rx.snapshot_len().saturating_add(bytes.len() as u64);
    h.dev.push_rx(bytes);
    h.wait_for("RX reader 已追加 recovery bytes", |rig| rig.rx.snapshot_len() >= end);
}

fn tail_text(h: &ActorRig, id: &str) -> (PyValue, Vec<u8>) {
    let tail = h.request(SessionRequest::ResultTail {
        cmd_id: id.to_string(),
        from_chunk: 0,
        limit: 10,
    });
    let PyValue::List(chunks) = &tail["chunks"] else {
        panic!("ResultTail chunks 格式錯誤：{tail:?}");
    };
    let text = chunks
        .iter()
        .filter_map(PyValue::as_str)
        .collect::<String>()
        .into_bytes();
    (tail["error_code"].clone(), text)
}

fn run_timeout_case(echo_found: bool, recovery_succeeds: bool, quiet_timeout: bool) {
    let h = ActorRig::new(SHELL_PROFILE).ready();

    let command = "printf R9_BASELINE";
    let command_tx = b"printf R9_BASELINE\n";
    let rx_before = h.rx.snapshot_len();
    let tx_count = h.wal_tx_payloads().len();
    let id = h.submit_with(command, |request| {
        request.timeout_s = if quiet_timeout { 30.0 } else { 3.0 };
        if quiet_timeout {
            request.expected_duration_s = Some(120.0);
        }
    });
    wait_for_tx(&h, command_tx);

    let early = if echo_found {
        b"printf R9_BASELINE\r\nbefore\r\n".as_slice()
    } else {
        b"before\r\n".as_slice()
    };
    h.dev.push_rx(early);
    h.wait_for("期限前輸出由 RX reader 追加", |rig| {
        rig.rx.snapshot_len() >= rx_before + early.len() as u64
    });

    if recovery_succeeds {
        wait_until_tx(&h, b"\x03");
        inject_and_wait_for_rx(&h, b"late\r\n^C\r\nroot@t:/# ");
    } else {
        wait_until_tx(&h, b"\x03");
        inject_and_wait_for_rx(&h, b"late-c\r\n");
        h.advance(Duration::from_millis(250));
        wait_until_tx(&h, b"\x04");
        inject_and_wait_for_rx(&h, b"late-d\r\n");
        h.advance(Duration::from_millis(250));
    }
    wait_until_done(&h, &id);
    let record = h.command_get(&id);
    let (tail_error, stdout) = tail_text(&h, &id);
    let tx_rows: Vec<_> = h
        .wal_rows()
        .into_iter()
        .filter(|row| row.get("dir").and_then(PyValue::as_str) == Some("TX"))
        .collect();
    let tx: Vec<u8> = tx_rows
        .into_iter()
        .skip(tx_count)
        .filter(|row| {
            row.get("source").and_then(PyValue::as_str) == Some("agent")
                || row.get("source").and_then(PyValue::as_str) == Some("system:recover")
        })
        .flat_map(|row| {
            decode_base64(row.get("payload_b64").and_then(PyValue::as_str).unwrap_or(""))
        })
        .collect();

    let (expected_status, expected_error, expected_action, expected_tail_error, expected_stdout, expected_tx) =
        match (echo_found, recovery_succeeds) {
            (false, true) => (
                "done",
                PyValue::Null,
                "CTRL_C",
                PyValue::from("PROMPT_TIMEOUT_RECOVERED"),
                b"before\nlate\n^C".as_slice(),
                b"printf R9_BASELINE\n\x03".as_slice(),
            ),
            (true, true) => (
                "done",
                PyValue::Null,
                "CTRL_C",
                PyValue::from("PROMPT_TIMEOUT_RECOVERED"),
                b"before\nlate\n^C".as_slice(),
                b"printf R9_BASELINE\n\x03".as_slice(),
            ),
            (false, false) => (
                "error",
                PyValue::from("PROMPT_TIMEOUT"),
                "NONE",
                PyValue::from("PROMPT_TIMEOUT"),
                b"before\nlate-c\nlate-d\n".as_slice(),
                b"printf R9_BASELINE\n\x03\x04".as_slice(),
            ),
            (true, false) => (
                "error",
                PyValue::from("PROMPT_TIMEOUT"),
                "NONE",
                PyValue::from("PROMPT_TIMEOUT"),
                b"printf R9_BASELINE\nbefore\nlate-c\nlate-d\n".as_slice(),
                b"printf R9_BASELINE\n\x03\x04".as_slice(),
            ),
        };

    println!(
        "i294-r9 baseline diff: quiet_timeout={quiet_timeout}, echo_found={echo_found}, recovery_succeeds={recovery_succeeds}, status={:?}, error_code={:?}, partial={:?}, recovery_action={:?}, tail_error={tail_error:?}, stdout={stdout:?}, tx={tx:?}",
        record["status"], record["error_code"], record["partial"], record["recovery_action"]
    );
    assert_eq!(record["status"], PyValue::from(expected_status), "{record:?}");
    assert_eq!(record["error_code"], expected_error, "{record:?}");
    assert_eq!(record["partial"], PyValue::Bool(true), "{record:?}");
    assert_eq!(record["recovery_action"], PyValue::from(expected_action), "{record:?}");
    assert_eq!(tail_error, expected_tail_error, "{record:?}");
    assert_eq!(stdout, expected_stdout, "stdout 必須逐位元組符合 9af187ca 基線");
    assert_eq!(tx, expected_tx, "TX 序列必須逐位元組符合 9af187ca 基線");
}

#[test]
fn i294_r9_no_human_timeout_stdout_matches_baseline_for_both_echo_states() {
    for quiet_timeout in [false, true] {
        for echo_found in [false, true] {
            run_timeout_case(echo_found, true, quiet_timeout);
            run_timeout_case(echo_found, false, quiet_timeout);
        }
    }
}
