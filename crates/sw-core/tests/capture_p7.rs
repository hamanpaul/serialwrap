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

#[test]
fn first_log_start_reply_session_has_active_capture() {
    // 審查 sol-backfill p7-20261028 #4：Python `log_start` 先設 `session.active_capture` 再 `to_public_dict()`，
    // 首次成功回應的巢狀 `session.capture` 即為同一 capture（status active、零計數），非 null。
    let tmp = tempfile::tempdir().unwrap();
    let dir = tmp.path().join("logs").to_string_lossy().into_owned();
    let h = rig_with_log_dir(&dir);
    let start = h.request(SessionRequest::LogStart);
    assert_eq!(start["ok"], PyValue::Bool(true), "{start:?}");
    let session = match &start["session"] {
        PyValue::Dict(d) => d.clone(),
        other => panic!("session 應為 dict：{other:?}"),
    };
    let cap = match &session["capture"] {
        PyValue::Dict(d) => d.clone(),
        other => panic!("首次 log_start 的 session.capture 不得為 null：{other:?}"),
    };
    assert_eq!(cap["capture_id"], start["capture_id"]);
    assert_eq!(cap["log_path"], start["log_path"]);
    assert_eq!(cap["status"], PyValue::from("active"));
    assert_eq!(cap["line_count"], int(0));
    assert_eq!(cap["byte_count"], int(0));
}

#[test]
fn session_capture_counts_are_live() {
    // 審查 p7-cloud a0cad06_4221ba2 397a473-1：Python `to_public_dict()` 的 capture 子 dict 讀
    // `active_capture.line_count／byte_count`（`_on_bridge_rx` 即時累加）。LogStart → RX `a\nb\n` → LogStatus：
    // 頂層與 `session.capture` 皆 2／4，`get_state` 快照亦同。
    let tmp = tempfile::tempdir().unwrap();
    let dir = tmp.path().join("logs").to_string_lossy().into_owned();
    let h = rig_with_log_dir(&dir);
    h.request(SessionRequest::LogStart);
    h.inject_rx(b"a\nb\n");
    h.wait_for("a、b 寫入", |h| {
        h.request(SessionRequest::LogStatus)["line_count"] == int(2)
    });
    let status = h.request(SessionRequest::LogStatus);
    assert_eq!(status["byte_count"], int(4));
    let nested = |d: &PyDict| match &d["session"] {
        PyValue::Dict(s) => match &s["capture"] {
            PyValue::Dict(c) => (c["line_count"].clone(), c["byte_count"].clone()),
            other => panic!("session.capture：{other:?}"),
        },
        other => panic!("session：{other:?}"),
    };
    assert_eq!(nested(&status), (int(2), int(4)), "log_status 的 session.capture 須為即時計數");
    let state = h.get_state();
    match &state["capture"] {
        PyValue::Dict(c) => assert_eq!((c["line_count"].clone(), c["byte_count"].clone()), (int(2), int(4))),
        other => panic!("get_state capture：{other:?}"),
    }
}

// --- 審查 a0cad06_4221ba2 397a473-2：08 Task 10 表列的 actor 層 log RPC 測試（`session_manager.py:7893-7968`）。

fn session_capture(d: &PyDict) -> PyValue {
    match &d["session"] {
        PyValue::Dict(s) => s["capture"].clone(),
        other => panic!("session：{other:?}"),
    }
}

#[test]
fn log_start_already_active() {
    let tmp = tempfile::tempdir().unwrap();
    let dir = tmp.path().join("logs").to_string_lossy().into_owned();
    let h = rig_with_log_dir(&dir);
    let first = h.request(SessionRequest::LogStart);
    let second = h.request(SessionRequest::LogStart);
    assert_eq!(keys(&second), ["ok", "already_active", "capture_id", "log_path", "session"]);
    assert_eq!(second["ok"], PyValue::Bool(true));
    assert_eq!(second["already_active"], PyValue::Bool(true));
    assert_eq!(second["capture_id"], first["capture_id"]);
    assert_eq!(second["log_path"], first["log_path"]);
    assert!(matches!(session_capture(&second), PyValue::Dict(_)));
}

#[test]
fn log_stop_without_capture() {
    let tmp = tempfile::tempdir().unwrap();
    let dir = tmp.path().join("logs").to_string_lossy().into_owned();
    let h = rig_with_log_dir(&dir);
    let r = h.request(SessionRequest::LogStop);
    assert_eq!(keys(&r), ["ok", "error_code", "session"]);
    assert_eq!(r["ok"], PyValue::Bool(false));
    assert_eq!(r["error_code"], PyValue::from("NO_ACTIVE_CAPTURE"));
    assert_eq!(session_capture(&r), PyValue::Null);
}

#[test]
fn log_status_inactive_and_active() {
    let tmp = tempfile::tempdir().unwrap();
    let dir = tmp.path().join("logs").to_string_lossy().into_owned();
    let h = rig_with_log_dir(&dir);
    let idle = h.request(SessionRequest::LogStatus);
    assert_eq!(keys(&idle), ["ok", "active", "session"]);
    assert_eq!(idle["active"], PyValue::Bool(false));
    assert_eq!(session_capture(&idle), PyValue::Null);
    let start = h.request(SessionRequest::LogStart);
    h.inject_rx(b"line\n");
    h.wait_for("line 寫入", |h| {
        h.request(SessionRequest::LogStatus)["line_count"] == int(1)
    });
    let active = h.request(SessionRequest::LogStatus);
    assert_eq!(
        keys(&active),
        ["ok", "active", "capture_id", "log_path", "line_count", "byte_count", "started_at", "session"]
    );
    assert_eq!(active["active"], PyValue::Bool(true));
    assert_eq!(active["capture_id"], start["capture_id"]);
    assert_eq!((active["line_count"].clone(), active["byte_count"].clone()), (int(1), int(5)));
    match session_capture(&active) {
        PyValue::Dict(c) => {
            assert_eq!(c["capture_id"], start["capture_id"]);
            assert_eq!((c["line_count"].clone(), c["byte_count"].clone()), (int(1), int(5)));
        }
        other => panic!("session.capture：{other:?}"),
    }
}

#[test]
fn log_open_failed_detail_is_python_oserror_str() {
    // root 下唯讀目錄無效，改以「log 檔路徑已是目錄」觸發 `open(path, "a")` 失敗（FakeClock 不前進 → 第二次
    // log_start 的檔名相同）。Python：POSIX `IsADirectoryError` → `[Errno 21] Is a directory: '<path>'`；Windows
    // 開目錄為 `PermissionError` → `[Errno 13] Permission denied: '<path>'`（Windows 原生列 P7-LW-3）。
    let tmp = tempfile::tempdir().unwrap();
    let dir = tmp.path().join("logs").to_string_lossy().into_owned();
    let h = rig_with_log_dir(&dir);
    let first = h.request(SessionRequest::LogStart);
    let path = first["log_path"].as_str().unwrap().to_string();
    h.request(SessionRequest::LogStop);
    std::fs::remove_file(&path).unwrap();
    std::fs::create_dir(&path).unwrap();
    let r = h.request(SessionRequest::LogStart);
    assert_eq!(keys(&r), ["ok", "error_code", "detail"]);
    assert_eq!(r["error_code"], PyValue::from("LOG_OPEN_FAILED"));
    let expected = if std::path::MAIN_SEPARATOR == '\\' {
        format!("[Errno 13] Permission denied: '{path}'")
    } else {
        format!("[Errno 21] Is a directory: '{path}'")
    };
    assert_eq!(r["detail"], PyValue::from(expected));
}

#[test]
fn log_status_counts_single_snapshot_under_concurrent_rx() {
    // 審查 71fe017_27b5bf4 P7-27b5bf4-1：Python `log_status` 與 `_on_bridge_rx` 的計數更新共用 manager lock，
    // 頂層與 `session.capture` 計數必然一致。Rust 以 test-fakes hook 在第一次取樣後注入第二筆 RX
    // （模擬 reader 在兩次取樣之間更新），兩處仍須相同。
    let tmp = tempfile::tempdir().unwrap();
    let dir = tmp.path().join("logs").to_string_lossy().into_owned();
    let mut h = CoreHarness::new(&log_profile(&dir));
    let clock: Arc<dyn Clock> = sw_pal::fakes::clock::FakeClock::new();
    h.core.set_capture_env(CaptureEnv {
        clock: Some(clock),
        ..CaptureEnv::default()
    });
    h.to_ready();
    let start = h.rpc(SessionRequest::LogStart).expect("log_start");
    assert_eq!(start["ok"], PyValue::Bool(true), "{start:?}");
    let obs = h.core.capture_observer_for_test();
    obs.write_rx(b"a\n");
    let injected = Arc::new(AtomicBool::new(false));
    {
        let (obs2, injected) = (obs.clone(), injected.clone());
        obs.set_counts_hook(Some(Box::new(move || {
            if !injected.swap(true, Ordering::SeqCst) {
                obs2.write_rx(b"b\n");
            }
        })));
    }
    let r = h.rpc(SessionRequest::LogStatus).expect("log_status");
    obs.set_counts_hook(None);
    assert!(injected.load(Ordering::SeqCst), "hook 須觸發（取樣之間注入 RX）");
    let top = (r["line_count"].clone(), r["byte_count"].clone());
    let nested = match &r["session"] {
        PyValue::Dict(s) => match &s["capture"] {
            PyValue::Dict(c) => (c["line_count"].clone(), c["byte_count"].clone()),
            other => panic!("session.capture：{other:?}"),
        },
        other => panic!("session：{other:?}"),
    };
    assert_eq!(top, nested, "同一回應的頂層與 session.capture 計數須來自同一快照");
}
