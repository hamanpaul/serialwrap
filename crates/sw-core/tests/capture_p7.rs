//! P7 Task 10（08-slice23-login-events.md）：agent log capture 改由 reader 端寫入（`CaptureObserver`）。
//!
//! Python 基線：`session_manager._on_bridge_rx` 在 RX callback（reader 執行緒）內同步 `fp.write(chunk); fp.flush()`，
//! 從不丟資料；`chunk = clean_text(data.decode("utf-8", errors="replace"))`，空 chunk 不寫；`foreground_busy`
//! 期間不寫（EVT-D-L1）；`byte_count += len(chunk.encode("utf-8"))`、`line_count += chunk.count("\n")`；
//! 檔案以文字模式 `open(path, "a", encoding="utf-8")` 開啟（Windows 寫出時 `\n`→`\r\n`，計數取轉換前）。
//! 回應形狀以 `contracts/golden/p7/log_responses.ndjson`（CPython 3.12 擷取）為準。
//!
//! 偏離計畫原文（P7 修訂）：檔案開關仍在 actor 內同步執行（Python 亦在 manager 鎖內同步 `os.makedirs`／`open`），
//! 不新增 `Effect::CaptureOpen／CaptureClose`；`slow_capture_fs_does_not_block_actor_control_lane` 未承接（見
//! `docs/rust-refactor/reviews/p7-fix-responses.md` 第 8 輪 Task 10 列）。
#![cfg(feature = "test-fakes")]

mod common;
mod support;

use std::sync::atomic::{AtomicBool, AtomicUsize, Ordering};
use std::sync::{Arc, Mutex};
use std::time::Duration;

use common::p7::golden_rows;
use support::*;
use sw_core::capture::{
    log_open_failed_reply, log_path_for, log_start_reply, log_status_reply, log_stop_reply,
    no_active_capture_reply, AgentLogCapture, CaptureEnv, CaptureObserver, LogFile,
};
use sw_core::session_core::SessionRequest;
use sw_core::uart_bridge::RxObserver;
use sw_pal::traits::Clock;
use sw_proto::pyjson::{dumps_dict, loads, PyDict, PyInt, PyValue, RPC_WIRE};

fn int(n: i128) -> PyValue {
    PyValue::Int(PyInt::Small(n))
}

fn keys(d: &PyDict) -> Vec<&str> {
    d.0.keys().map(String::as_str).collect()
}

fn log_profile(dir: &str) -> String {
    format!(
        "profiles:\n  t:\n    platform: shell\n    prompt_regex: '(?m)^root@t:.*# '\n    timeout_s: 2.0\n    log_dir: {dir}\ntargets:\n  - {{com: COM0, profile: t, act_no: 1, alias: t+1, device_by_id: /s/by-id/usb-A}}\n"
    )
}

fn rig_with_log_dir(dir: &str) -> ActorRig {
    let clock = sw_pal::fakes::clock::FakeClock::new();
    let wall_clock: Arc<dyn Clock> = clock.clone();
    ActorRig::new_with(&log_profile(dir), move |core| {
        core.set_capture_env(CaptureEnv {
            clock: Some(wall_clock),
            ..CaptureEnv::default()
        });
    })
    .ready()
}

/// 記憶體 log 檔（可選：每次寫入先等待 `gate` 放行）。
#[derive(Clone, Default)]
struct MemFile {
    data: Arc<Mutex<Vec<u8>>>,
    gate: Arc<AtomicBool>,
    entered: Arc<AtomicUsize>,
}

impl LogFile for MemFile {
    fn append_flush(&mut self, data: &[u8]) -> std::io::Result<()> {
        self.entered.fetch_add(1, Ordering::SeqCst);
        while self.gate.load(Ordering::SeqCst) {
            std::thread::sleep(Duration::from_millis(1));
        }
        self.data.lock().unwrap().extend_from_slice(data);
        Ok(())
    }
}

#[test]
fn log_path_uses_local_time() {
    // Python `time.strftime("%y%m%d-%H%M%S")`（本地時間）：1_700_000_000 為 2023-11-14 22:13:20 UTC，UTC+8 → 231115-061320。
    assert_eq!(
        log_path_for("/x", "COM2", 1_700_000_000, 28800),
        "/x/COM2_231115-061320.log"
    );
    assert_eq!(
        log_path_for("/x", "COM0", 0, 0),
        "/x/COM0_700101-000000.log"
    );
}

#[test]
fn log_responses_match_python_golden_bytes() {
    let mut checked = 0;
    for row in golden_rows("log_responses") {
        let setup = row.get("setup").clone();
        let op = setup.get("op").and_then(PyValue::as_str).unwrap_or_default().to_string();
        let active = setup.get("capture_active").and_then(PyValue::as_bool) == Some(true);
        let is_dir = setup.get("log_path_is_dir").and_then(PyValue::as_bool) == Some(true);
        if setup.get("call").is_some() || row.opt_str("raised").is_some() {
            // SESSION_NOT_FOUND 由 manager 的 selector 解析產生；makedirs 例外穿出為 framing EXCEPTION
            //（`lease_selftest::log_capture_dir_not_creatable_is_exception`），皆非 capture 回應函式的範圍。
            continue;
        }
        let want = row.str("response");
        let PyValue::Dict(resp) = loads(want).expect("response JSON") else {
            panic!("response 須為 object")
        };
        let session = match resp.get("session") {
            Some(PyValue::Dict(s)) => s.clone(),
            _ => PyDict::new(),
        };
        let str_of = |k: &str| resp.get(k).and_then(PyValue::as_str).unwrap_or_default().to_string();
        let cap = AgentLogCapture {
            capture_id: str_of("capture_id"),
            log_path: str_of("log_path"),
            started_at: if str_of("started_at").is_empty() {
                "2026-10-26T00:00:00.000000+00:00".to_string()
            } else {
                str_of("started_at")
            },
            line_count: 0,
            byte_count: 0,
            status: "active",
        };
        let got = match (op.as_str(), active, is_dir) {
            ("start", false, true) => log_open_failed_reply(
                "[Errno 21] Is a directory: '/golden/blog/COM0_261026-000000.log'",
            ),
            ("start", already, false) => log_start_reply(&cap, already, session),
            ("stop", true, _) => log_stop_reply(&cap, 0, 0),
            ("stop", false, _) => no_active_capture_reply(session),
            ("status", true, _) => log_status_reply(Some((&cap, 0, 0)), session),
            ("status", false, _) => log_status_reply(None, session),
            other => panic!("未涵蓋的 setup {other:?}"),
        };
        assert_eq!(dumps_dict(&got, &RPC_WIRE), want, "case {}", row.id());
        checked += 1;
    }
    assert_eq!(checked, 7, "golden 11 列扣除 manager 層 3 列與例外 1 列");
}

#[test]
fn observer_writes_clean_text_and_counts_like_python() {
    let obs = CaptureObserver::new();
    let file = MemFile::default();
    let stats = obs.install("cap-1".into(), Box::new(file.clone()));
    obs.on_rx("COM0", b"hello\r\n\xe4\xb8\x96\xe7\x95\x8c\n", 1);
    obs.on_rx("COM0", b"\r\r", 2); // clean_text 後為空 → 不寫、不計
    obs.set_foreground_busy(true);
    obs.on_rx("COM0", b"SECRET-FG\n", 3);
    obs.set_foreground_busy(false);
    let expected = "hello\n世界\n";
    assert_eq!(
        (stats.line_count.load(Ordering::SeqCst), stats.byte_count.load(Ordering::SeqCst)),
        (2, expected.len() as u64)
    );
    assert_eq!(obs.remove("cap-1"), Some((2, expected.len() as u64)));
    // 文字模式寫出：Windows 每個 `\n` 為 `\r\n`（計數取轉換前，同 Python）。
    assert_eq!(
        *file.data.lock().unwrap(),
        sw_pal::fs_ops::text_mode_bytes(expected)
    );
    // 移除後不再寫入；錯誤 id 回 None。
    obs.on_rx("COM0", b"late\n", 4);
    assert_eq!(*file.data.lock().unwrap(), sw_pal::fs_ops::text_mode_bytes(expected));
    assert_eq!(obs.remove("cap-1"), None);
}

#[test]
fn stop_waits_for_inflight_write() {
    // reader 正寫入時 stop：`remove` 取同一把鎖，回傳的計數包含該 chunk（Python 同在 manager 鎖內）。
    let obs = CaptureObserver::new();
    let file = MemFile::default();
    file.gate.store(true, Ordering::SeqCst);
    obs.install("cap-2".into(), Box::new(file.clone()));
    let writer = {
        let obs = obs.clone();
        std::thread::spawn(move || obs.on_rx("COM0", &vec![b'a'; 65536], 9))
    };
    while file.entered.load(Ordering::SeqCst) == 0 {
        std::thread::sleep(Duration::from_millis(1));
    }
    let remover = {
        let obs = obs.clone();
        std::thread::spawn(move || obs.remove("cap-2"))
    };
    std::thread::sleep(Duration::from_millis(50));
    assert!(!remover.is_finished(), "remove 須等待進行中的寫入");
    file.gate.store(false, Ordering::SeqCst);
    writer.join().unwrap();
    assert_eq!(remover.join().unwrap(), Some((0, 65536)));
}

#[test]
fn foreground_rx_is_not_captured_and_counts_match_python_rules() {
    let tmp = tempfile::tempdir().unwrap();
    let dir = tmp.path().join("logs").to_string_lossy().into_owned();
    let h = rig_with_log_dir(&dir);
    let start = h.request(SessionRequest::LogStart);
    assert_eq!(keys(&start), ["ok", "capture_id", "log_path", "session"]);
    let path = start["log_path"].as_str().unwrap().to_string();
    h.inject_rx(b"hello\r\n\xe4\xb8\x96\xe7\x95\x8c\n");
    h.dev
        .on_tx_reply(b"echo fg\n", b"echo fg\r\nSECRET-FG\r\nroot@t:/# ");
    let id = h.submit_ok("echo fg");
    h.wait_done(&id);
    let stop = h.request(SessionRequest::LogStop);
    assert_eq!(
        keys(&stop),
        ["ok", "capture_id", "log_path", "line_count", "byte_count", "started_at"]
    );
    let expected = "hello\n世界\n";
    assert_eq!(
        (stop["line_count"].clone(), stop["byte_count"].clone()),
        (int(2), int(expected.len() as i128))
    );
    assert_eq!(
        std::fs::read(&path).unwrap(),
        sw_pal::fs_ops::text_mode_bytes(expected)
    );
}

#[test]
fn no_drop_under_burst() {
    // 10000 個 chunk 連續 RX：檔案內容＝byte_count＝輸入 clean_text 總長（reader 端同步寫入，不經 actor 佇列）。
    let tmp = tempfile::tempdir().unwrap();
    let dir = tmp.path().join("logs").to_string_lossy().into_owned();
    let h = rig_with_log_dir(&dir);
    let start = h.request(SessionRequest::LogStart);
    let path = start["log_path"].as_str().unwrap().to_string();
    let mut expected = String::new();
    for i in 0..10_000 {
        let line = format!("burst-{i:05}\n");
        h.dev.push_rx(line.as_bytes());
        expected.push_str(&line);
    }
    let want = expected.len() as i128;
    h.wait_for("burst 全數寫入", |h| {
        h.request(SessionRequest::LogStatus)["byte_count"] == int(want)
    });
    let stop = h.request(SessionRequest::LogStop);
    assert_eq!(stop["byte_count"], int(want));
    assert_eq!(stop["line_count"], int(10_000));
    assert_eq!(
        std::fs::read(&path).unwrap(),
        sw_pal::fs_ops::text_mode_bytes(&expected)
    );
}

#[test]
fn detach_auto_stops_capture() {
    // Python `_stop_capture_locked`：session detach 時停止 capture（檔案關閉、之後 RX 不再寫入）。
    let tmp = tempfile::tempdir().unwrap();
    let dir = tmp.path().join("logs").to_string_lossy().into_owned();
    let h = rig_with_log_dir(&dir);
    let start = h.request(SessionRequest::LogStart);
    let path = start["log_path"].as_str().unwrap().to_string();
    h.inject_rx(b"before\n");
    h.wait_for("before 寫入", |h| {
        h.request(SessionRequest::LogStatus)["line_count"] == int(1)
    });
    h.dev.remove();
    h.wait_state("DETACHED");
    let status = h.request(SessionRequest::LogStatus);
    assert_eq!(status["active"], PyValue::Bool(false));
    assert_eq!(
        std::fs::read(&path).unwrap(),
        sw_pal::fs_ops::text_mode_bytes("before\n")
    );
}
