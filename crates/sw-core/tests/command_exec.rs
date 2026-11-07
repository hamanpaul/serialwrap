//! Slice 2.1 Task 14：命令執行（line／background／completion_regex）、逾時救援、reboot、recover 與 passthrough
//! 命令路徑的紅燈測試（plan 06 Task 14 Step 1；parity-amendments-2026-10-26 #34、#45、#55、#94）。
//!
//! 斷言值以 Python 基線（`sw_core/session_manager.py` 的 `_execute_command_inner`、`_recover_after_failure`、
//! `_handle_reboot_command`、`recover_session`、`_force_recover`、`get_background_result`）為準。
//! - 需要真 actor thread 的列以 `ActorRig` 驅動（OpRunner 於 FakeClock 上等待；測試以 `advance_until` 推進）；
//! - capture 淘汰等 sans-IO 列以 `CoreHarness` 驅動。

#![cfg(feature = "test-fakes")]

mod support;

use std::cell::RefCell;
use std::sync::Arc;
use std::time::{Duration, Instant};

use support::*;
use sw_core::capture::{BG_CAPTURE_MAX_BYTES, BackgroundCapture};
use sw_core::mailbox::{ControlEvent, OpenFailure};
use sw_core::op_runner::{OpOutcome, extract_command_stdout};
use sw_core::session_core::{Effect, SessionRequest};
use sw_core::tx_writer::OpId;
use sw_pal::fakes::serial::FakeWriteStep;
use sw_pal::traits::PalError;
use sw_proto::pycompat::{PyErr, clean_text};
use sw_proto::pyjson::{PyDict, PyValue};
use sw_proto::pyre::{PyFlags, compile};

const BANNER: &[u8] = b"\r\nU-Boot 2024.04 (Oct 01 2024)\r\n";

fn pget(d: &PyDict, k: &str) -> PyValue {
    d.get(k).cloned().unwrap_or(PyValue::Null)
}

fn keys(d: &PyDict) -> Vec<String> {
    d.0.keys().cloned().collect()
}

fn is_done(h: &ActorRig, id: &str) -> bool {
    h.command_get(id)
        .get("done_at")
        .is_some_and(|v| !v.is_null())
}

/// 以小步推進 FakeClock（每步投遞 Tick 並讓出實時），直到條件成立（真實時間 10 s 上限）。
fn advance_until(
    h: &ActorRig,
    step: Duration,
    what: &str,
    mut cond: impl FnMut(&ActorRig) -> bool,
) {
    let deadline = Instant::now() + Duration::from_secs(10);
    loop {
        if cond(h) {
            return;
        }
        assert!(Instant::now() < deadline, "推進逾時：{what}");
        h.clock.advance(step);
        h.send_control(ControlEvent::Tick);
        std::thread::sleep(Duration::from_millis(3));
    }
}

/// 送出非同步 RPC，推進時鐘直到回覆。
fn request_advancing(h: &ActorRig, req: SessionRequest, step: Duration) -> Result<PyDict, PyErr> {
    let rx = h.request_async(req);
    let got: RefCell<Option<Result<PyDict, PyErr>>> = RefCell::new(None);
    advance_until(h, step, "RPC 回覆", |_| {
        if got.borrow().is_some() {
            return true;
        }
        if let Ok(result) = rx.try_recv() {
            *got.borrow_mut() = Some(result);
            return true;
        }
        false
    });
    got.into_inner().expect("已收到回覆")
}

fn tx_rows(h: &ActorRig) -> Vec<PyDict> {
    h.wal_all_rows()
        .into_iter()
        .filter(|r| r.get("dir").and_then(PyValue::as_str) == Some("TX"))
        .collect()
}

fn b64(row: &PyDict) -> Vec<u8> {
    use std::collections::HashMap;
    let alphabet = b"ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789+/";
    let map: HashMap<u8, u32> = alphabet
        .iter()
        .enumerate()
        .map(|(i, c)| (*c, i as u32))
        .collect();
    let s = row
        .get("payload_b64")
        .and_then(PyValue::as_str)
        .unwrap_or("");
    let mut out = Vec::new();
    let mut acc = 0u32;
    let mut bits = 0u32;
    for c in s.bytes() {
        let Some(v) = map.get(&c) else { continue };
        acc = (acc << 6) | v;
        bits += 6;
        if bits >= 8 {
            bits -= 8;
            out.push(((acc >> bits) & 0xff) as u8);
        }
    }
    out
}

fn wal_tx_has(h: &ActorRig, payload: &[u8]) -> bool {
    h.wal_tx_payloads().iter().any(|p| p == payload)
}

/// 等 WAL 出現指定 TX（OpRunner 已送出）。
fn wait_tx(h: &ActorRig, payload: &[u8]) {
    h.wait_for("TX 寫出", |h| wal_tx_has(h, payload));
}

fn state_of(h: &ActorRig) -> String {
    pget(&h.get_state(), "state")
        .as_str()
        .unwrap_or("")
        .to_string()
}

/// passthrough 安靜 30 s 升 READY（SC-35）。
fn passthrough_ready(profile: &str) -> ActorRig {
    let h = ActorRig::new(profile).attach();
    h.wait_state("ATTACHED");
    h.advance(Duration::from_secs(30));
    h.wait_state("READY");
    h
}

// ---------------------------------------------------------------------------
// Step 1 程式碼列
// ---------------------------------------------------------------------------

#[test]
fn line_command_stdout_extraction_and_done() {
    let h = ActorRig::new(SHELL_PROFILE).ready();
    h.dev
        .on_tx_reply(b"echo hi\n", b"echo hi\r\nhi\r\nroot@t:/# ");
    let id = h.submit_ok("echo hi");
    let rec = h.wait_done(&id);
    assert_eq!(
        (
            rec["status"].clone(),
            rec["stdout"].clone(),
            rec["partial"].clone()
        ),
        (
            PyValue::from("done"),
            PyValue::from("hi"),
            PyValue::Bool(false)
        )
    );
    assert_eq!(rec["execution_mode"], PyValue::from("line"));
}

#[test]
fn timeout_sends_ctrl_c_then_recovers_and_flushes_queue() {
    let h = ActorRig::new(SHELL_PROFILE).ready();
    h.dev.on_tx_reply(b"\x03", b"^C\r\nroot@t:/# ");
    let slow = h.submit_with("sleep 20", |r| r.timeout_s = 1.0);
    let queued = h.submit_ok("echo later");
    wait_tx(&h, b"sleep 20\n");
    advance_until(&h, Duration::from_millis(100), "救援完成", |h| {
        is_done(h, &slow)
    });
    let rec = h.wait_done(&slow);
    assert_eq!(
        (
            rec["status"].clone(),
            rec["error_code"].clone(),
            rec["recovery_action"].clone()
        ),
        (
            PyValue::from("done"),
            PyValue::Null,
            PyValue::from("CTRL_C")
        )
    ); // ok:true 的碼不入紀錄
    assert_eq!(rec["partial"], PyValue::Bool(true));
    assert_eq!(
        h.command_get(&queued)["error_code"],
        PyValue::from("FLUSHED_BY_RECOVERY")
    );
    assert_eq!(h.get_state()["state"], PyValue::from("READY"));
    assert!(h.wal_tx_payloads().contains(&b"\x03".to_vec()));
    assert!(
        !h.wal_tx_payloads().contains(&b"\x04".to_vec()),
        "CTRL_C 成功後不得送 CTRL_D"
    );
}

#[test]
fn submit_during_agent_lease_is_rejected_at_execute_time() {
    let h = ActorRig::new(SHELL_PROFILE).ready();
    let open = h.request(SessionRequest::InteractiveOpen {
        owner: "agent:f4".into(),
        timeout_s: 20.0,
        command: String::new(),
        allow_attached: false,
    });
    let iid = open["interactive_id"].as_str().unwrap().to_string();
    let id = h.submit_ok("echo during_lease");
    let rec = h.wait_done(&id);
    assert_eq!(rec["error_code"], PyValue::from("SESSION_INTERACTIVE_BUSY")); // f4-interactive-line-cmd-defined 合法終態 (a)
    assert!(
        !wal_tx_has(&h, b"echo during_lease\n"),
        "execute-time 拒絕零 TX"
    );
    let close = h.request(SessionRequest::InteractiveClose {
        interactive_id: iid,
    });
    assert_eq!(close["ok"], PyValue::Bool(true));
}

// ---------------------------------------------------------------------------
// command_exec.rs 表列
// ---------------------------------------------------------------------------

#[test]
fn silence_breaks_wait_loop() {
    // timeout 60 → silence_limit = min(60, 30) = 30 s；30 s 內完全無 RX 即停止等待、進救援。
    let h = ActorRig::new(SHELL_PROFILE).ready();
    h.dev.on_tx_reply(b"\x03", b"^C\r\nroot@t:/# ");
    let id = h.submit_with("sleep 99", |r| r.timeout_s = 60.0);
    wait_tx(&h, b"sleep 99\n");
    std::thread::sleep(Duration::from_millis(150)); // 讓 OpRunner 進入等待迴圈（期限以 FakeClock 起算）
    let start = h.clock.elapsed();
    // 29 s 內不得救援（timeout 60 s 尚未到，但 silence 30 s 未滿）。
    h.clock.advance(Duration::from_secs(29));
    h.send_control(ControlEvent::Tick);
    std::thread::sleep(Duration::from_millis(150));
    assert!(!wal_tx_has(&h, b"\x03"), "靜默未滿 30 s 不得救援");
    let deadline = Instant::now() + Duration::from_secs(10);
    while !wal_tx_has(&h, b"\x03") {
        assert!(Instant::now() < deadline, "未送出 CTRL_C");
        h.clock.advance(Duration::from_millis(250));
        std::thread::sleep(Duration::from_millis(80));
    }
    let waited = (h.clock.elapsed() - start).as_secs_f64();
    assert!(
        (30.0..31.0).contains(&waited),
        "應於靜默 30 s 後救援（而非 timeout 60 s），實際 {waited}"
    );
    let rec = h.wait_done(&id);
    assert_eq!(rec["recovery_action"], PyValue::from("CTRL_C"));
}

#[test]
fn rx_keeps_waiting_until_timeout() {
    // 有持續 RX 但無 prompt → 等到 max(timeout_s, expected_duration_s) 才救援。
    let h = ActorRig::new(SHELL_PROFILE).ready();
    h.dev.on_tx_reply(b"\x03", b"^C\r\nroot@t:/# ");
    let id = h.submit_with("make", |r| {
        r.timeout_s = 2.0;
        r.expected_duration_s = Some(6.0);
    });
    wait_tx(&h, b"make\n");
    let start = h.clock.elapsed();
    let deadline = Instant::now() + Duration::from_secs(10);
    while !wal_tx_has(&h, b"\x03") {
        assert!(Instant::now() < deadline, "未送出 CTRL_C");
        h.inject_rx(b"building...\r\n");
        h.clock.advance(Duration::from_millis(500));
        h.send_control(ControlEvent::Tick);
        std::thread::sleep(Duration::from_millis(5));
    }
    let waited = (h.clock.elapsed() - start).as_secs_f64();
    assert!(
        waited >= 6.0,
        "有 RX 時應等滿 effective timeout 6 s，實際 {waited}"
    );
    assert!(waited < 7.5, "effective timeout 到即救援，實際 {waited}");
    let rec = h.wait_done(&id);
    assert_eq!(rec["recovery_action"], PyValue::from("CTRL_C"));
}

#[test]
fn ctrl_d_after_ctrl_c_fails() {
    let h = ActorRig::new(SHELL_PROFILE).ready();
    h.dev.on_tx_reply(b"\x04", b"\r\nroot@t:/# ");
    let id = h.submit_with("cat", |r| r.timeout_s = 1.0);
    wait_tx(&h, b"cat\n");
    advance_until(&h, Duration::from_millis(100), "CTRL_D 救援完成", |h| {
        is_done(h, &id)
    });
    let rec = h.command_get(&id);
    assert_eq!(rec["status"], PyValue::from("done"));
    assert_eq!(rec["recovery_action"], PyValue::from("CTRL_D"));
    assert!(wal_tx_has(&h, b"\x03") && wal_tx_has(&h, b"\x04"));
    assert_eq!(state_of(&h), "READY");
}

#[test]
fn both_keys_fail_prompt_timeout_to_attached() {
    let h = ActorRig::new(SHELL_PROFILE).ready();
    let id = h.submit_with("cat", |r| r.timeout_s = 1.0);
    let queued = h.submit_ok("echo later");
    wait_tx(&h, b"cat\n");
    advance_until(&h, Duration::from_millis(100), "救援失敗收尾", |h| {
        is_done(h, &id)
    });
    let rec = h.command_get(&id);
    assert_eq!(rec["error_code"], PyValue::from("PROMPT_TIMEOUT"));
    assert_eq!(rec["partial"], PyValue::Bool(true));
    assert_eq!(rec["recovery_action"], PyValue::from("NONE"));
    let s = h.get_state();
    assert_eq!(s["state"], PyValue::from("ATTACHED"));
    assert_eq!(s["last_error"], PyValue::from("PROMPT_TIMEOUT"));
    assert_eq!(
        h.command_get(&queued)["error_code"],
        PyValue::from("FLUSHED_BY_RECOVERY")
    );
    // 回到 ATTACHED 後佇列解除註冊：再送命令 → SESSION_NOT_READY。
    let again = h.request(SessionRequest::Submit(agent_submit("ls")));
    assert_eq!(again["error_code"], PyValue::from("SESSION_NOT_READY"));
}

#[test]
fn quiet_rechecked_before_each_recover_key() {
    // `\x03` 送出後 RX 出現 banner（quiet 於兩次寫入之間 arm）→ `\x04` 被 fence `Quiet` 拒絕、write_calls 不增。
    let h = ActorRig::new(SHELL_PROFILE).ready();
    h.dev.on_tx_reply(b"\x03", BANNER);
    let id = h.submit_with("cat", |r| r.timeout_s = 1.0);
    wait_tx(&h, b"cat\n");
    advance_until(&h, Duration::from_millis(100), "CTRL_C 送出", |h| {
        wal_tx_has(h, b"\x03")
    });
    h.wait_for("banner arm quiet", |h| h.fence.with(|f| f.quiet_active));
    let calls_after_ctrl_c = h.dev.write_calls();
    advance_until(&h, Duration::from_millis(100), "救援收尾", |h| {
        is_done(h, &id)
    });
    assert_eq!(
        h.dev.write_calls(),
        calls_after_ctrl_c,
        "quiet 中 CTRL_D 不得進入 PAL"
    );
    assert!(!wal_tx_has(&h, b"\x04"));
    assert!(!h.dev.take_tx().contains(&0x04));
    assert_eq!(
        h.command_get(&id)["error_code"],
        PyValue::from("PROMPT_TIMEOUT")
    );
    assert_eq!(state_of(&h), "ATTACHED");
}

#[test]
fn completion_regex_meta_and_extraction() {
    let h = ActorRig::new(SHELL_PROFILE).ready();
    h.dev
        .on_tx_reply(b"runit\n", b"runit\r\nout\r\nRC=0\r\nroot@t:/# ");
    let id = h.submit_with("runit", |r| {
        r.completion_regex = Some("(?m)^RC=0\\r?$".into())
    });
    let rec = h.wait_done(&id);
    assert_eq!(rec["status"], PyValue::from("done"));
    // 保留完成標記、裁掉其後 prompt。
    assert_eq!(rec["stdout"], PyValue::from("out\nRC=0"));
    let metas: Vec<PyDict> = h
        .wal_all_rows()
        .into_iter()
        .filter(|r| r.get("dir").and_then(PyValue::as_str) == Some("META"))
        .filter(|r| {
            matches!(r.get("meta"), Some(PyValue::Dict(m)) if m.get("event").and_then(PyValue::as_str) == Some("command_completion"))
        })
        .collect();
    assert_eq!(metas.len(), 1);
    assert_eq!(metas[0]["cmd_id"], PyValue::from(id.clone()));
    let PyValue::Dict(meta) = metas[0]["meta"].clone() else {
        panic!("meta 應為 dict")
    };
    // WAL 以 sort_keys 序列化（Python `json.dumps(..., sort_keys=True)`）。
    assert_eq!(keys(&meta), vec!["completion_regex", "event"]);
    assert_eq!(meta["event"], PyValue::from("command_completion"));
    assert_eq!(meta["completion_regex"], PyValue::from("(?m)^RC=0\\r?$"));
}

#[test]
fn echo_line_removed_repeatedly() {
    let prompt = compile("(?m)^# ", PyFlags(0)).expect("prompt regex");
    assert_eq!(
        extract_command_stdout("ls\r\nls\r\na b\r\n# ", "ls", &prompt, None),
        "a b"
    );
    // 實機路徑：同樣的回顯重複兩次。
    let h = ActorRig::new(SHELL_PROFILE).ready();
    h.dev.on_tx_reply(b"ls\n", b"ls\r\nls\r\na b\r\nroot@t:/# ");
    let id = h.submit_ok("ls");
    assert_eq!(h.wait_done(&id)["stdout"], PyValue::from("a b"));
}

#[test]
fn background_capture_lifecycle() {
    let h = ActorRig::new(SHELL_PROFILE).ready();
    h.dev
        .on_tx_reply(b"dmesg\n", b"dmesg\r\nline1\r\nroot@t:/# ");
    let id = h.submit_with("dmesg", |r| r.mode = "bg".into());
    let rec = h.wait_done(&id);
    assert_eq!(rec["execution_mode"], PyValue::from("background"));
    assert_eq!(rec["background_capture_id"], PyValue::from(id.clone()));
    let tail = h.request(SessionRequest::ResultTail {
        cmd_id: id.clone(),
        from_chunk: 0,
        limit: 200,
    });
    assert_eq!(
        keys(&tail),
        vec![
            "ok",
            "cmd_id",
            "status",
            "error_code",
            "from_seq",
            "last_seq",
            "from_chunk",
            "next_chunk",
            "dropped_chunks",
            "lost",
            "chunks"
        ]
    );
    assert_eq!(tail["status"], PyValue::from("active"));
    let PyValue::List(chunks) = tail["chunks"].clone() else {
        panic!("chunks 應為 list")
    };
    let joined: String = chunks.iter().filter_map(PyValue::as_str).collect();
    assert!(joined.contains("line1"), "capture 應累積 RX：{joined:?}");
    // quiet_window 2.0 s 後 maybe_finalize → done。
    h.advance(Duration::from_millis(2_100));
    let tail = h.request(SessionRequest::ResultTail {
        cmd_id: id.clone(),
        from_chunk: 0,
        limit: 200,
    });
    assert_eq!(tail["status"], PyValue::from("done"));
}

#[test]
fn background_capture_truncates_head_keeps_one() {
    let mut cap = BackgroundCapture::new("c".into(), "s".into(), 1, 0, 2.0, "t".into(), 0, 1);
    let huge = "x".repeat(BG_CAPTURE_MAX_BYTES + 10);
    cap.add_chunk(&huge, BG_CAPTURE_MAX_BYTES);
    assert_eq!(cap.chunks.len(), 1, "單一超大 chunk 仍保留");
    assert_eq!(cap.dropped_chunks, 0);
    cap.add_chunk("tail", BG_CAPTURE_MAX_BYTES);
    assert_eq!(cap.chunks.len(), 1);
    assert_eq!(cap.dropped_chunks, 1);
    let r = cap.result(0, 200, 0).expect("result");
    assert_eq!(
        r["lost"],
        PyValue::Bool(true),
        "from_chunk < dropped_chunks"
    );
    assert_eq!(
        r["next_chunk"],
        PyValue::Int(sw_proto::pyjson::PyInt::Small(2))
    );
    let r = cap.result(1, 200, 0).expect("result");
    assert_eq!(r["lost"], PyValue::Bool(false));
    assert_eq!(r["chunks"], PyValue::List(vec![PyValue::from("tail")]));
}

/// CoreHarness：出列一筆 bg 命令並以 OpFinished 完成。
fn harness_bg_done(h: &mut CoreHarness, cmd: &str) -> String {
    let mut req = agent_submit(cmd);
    req.mode = "bg".into();
    let (resp, effects) = h.rpc_full(SessionRequest::Submit(req));
    let id = resp.expect("submit")["cmd_id"]
        .as_str()
        .unwrap()
        .to_string();
    let grant = effects
        .iter()
        .find_map(|e| match e {
            Effect::StartOp { grant, .. } => Some(grant.clone()),
            _ => None,
        })
        .expect("應出列");
    let epoch = h.epoch();
    h.feed(ControlEvent::OpFinished {
        op_id: grant.op_id,
        epoch,
        outcome: OpOutcome::CommandResult(sw_proto::pydict! {
            "ok" => true, "execution_mode" => "background", "stdout" => "", "partial" => false,
            "background_capture_id" => id.clone(),
        }),
        after_rx_seq: None,
    });
    id
}

#[test]
fn bg_capture_count_evicts_finished_over_64() {
    let mut h = CoreHarness::new(SHELL_PROFILE);
    h.to_ready();
    let mut ids = Vec::new();
    for i in 0..64 {
        ids.push(harness_bg_done(&mut h, &format!("bg {i}")));
    }
    // quiet window 過後讀取一次 → 全部 finalize（done）。
    h.advance(Duration::from_secs(3));
    for id in &ids {
        let r = h
            .rpc(SessionRequest::ResultTail {
                cmd_id: id.clone(),
                from_chunk: 0,
                limit: 1,
            })
            .unwrap();
        assert_eq!(r["status"], PyValue::from("done"));
    }
    let newest = harness_bg_done(&mut h, "bg 64");
    let first = h
        .rpc(SessionRequest::ResultTail {
            cmd_id: ids[0].clone(),
            from_chunk: 0,
            limit: 1,
        })
        .unwrap();
    assert_eq!(
        first["error_code"],
        PyValue::from("CMD_NOT_FOUND"),
        "第 65 個建立時淘汰最舊已結束者"
    );
    let second = h
        .rpc(SessionRequest::ResultTail {
            cmd_id: ids[1].clone(),
            from_chunk: 0,
            limit: 1,
        })
        .unwrap();
    assert_eq!(second["ok"], PyValue::Bool(true));
    let latest = h
        .rpc(SessionRequest::ResultTail {
            cmd_id: newest,
            from_chunk: 0,
            limit: 1,
        })
        .unwrap();
    assert_eq!(latest["status"], PyValue::from("active"));
}

#[test]
fn evict_removes_session_background_cmd_ids() {
    // #45 D-94：capture 淘汰時同步移除 session 的 background_cmd_ids。
    let mut h = CoreHarness::new(SHELL_PROFILE);
    h.to_ready();
    let mut ids = Vec::new();
    for i in 0..64 {
        ids.push(harness_bg_done(&mut h, &format!("bg {i}")));
    }
    h.advance(Duration::from_secs(3));
    for id in &ids {
        let _ = h.rpc(SessionRequest::ResultTail {
            cmd_id: id.clone(),
            from_chunk: 0,
            limit: 1,
        });
    }
    assert!(h.core.background_cmd_ids_for_test().contains(&ids[0]));
    let _ = harness_bg_done(&mut h, "bg 64");
    let tracked = h.core.background_cmd_ids_for_test();
    assert!(
        !tracked.contains(&ids[0]),
        "被淘汰的 capture 不得留在 background_cmd_ids"
    );
    assert_eq!(tracked.len(), 64);
}

#[test]
fn bg_capture_marked_done_on_finish() {
    // #45 D-43：背景 capture 結束（quiet window 後讀取）設 done；line 逾時的 terminal capture 設 error。
    let h = ActorRig::new(SHELL_PROFILE).ready();
    h.dev.on_tx_reply(b"bgcmd\n", b"bgcmd\r\nx\r\nroot@t:/# ");
    let id = h.submit_with("bgcmd", |r| r.mode = "bg".into());
    h.wait_done(&id);
    h.advance(Duration::from_secs(3));
    let r = h.request(SessionRequest::ResultTail {
        cmd_id: id,
        from_chunk: 0,
        limit: 10,
    });
    assert_eq!(r["status"], PyValue::from("done"));
    assert_eq!(r["error_code"], PyValue::Null);
}

#[test]
fn line_mode_terminates_existing_bg_capture() {
    // #45 D-44：同 session line 模式命令開始前終結既有 bg capture（quiet window 未到也立即 done）。
    let h = ActorRig::new(SHELL_PROFILE).ready();
    h.dev.on_tx_reply(b"bgcmd\n", b"bgcmd\r\nx\r\nroot@t:/# ");
    h.dev
        .on_tx_reply(b"echo fg\n", b"echo fg\r\nfg\r\nroot@t:/# ");
    let bg = h.submit_with("bgcmd", |r| r.mode = "bg".into());
    h.wait_done(&bg);
    let before = h.request(SessionRequest::ResultTail {
        cmd_id: bg.clone(),
        from_chunk: 0,
        limit: 10,
    });
    assert_eq!(before["status"], PyValue::from("active"));
    let fg = h.submit_ok("echo fg");
    h.wait_done(&fg);
    let after = h.request(SessionRequest::ResultTail {
        cmd_id: bg.clone(),
        from_chunk: 0,
        limit: 10,
    });
    assert_eq!(after["status"], PyValue::from("done"));
    let PyValue::List(chunks) = after["chunks"].clone() else {
        panic!()
    };
    let joined: String = chunks.iter().filter_map(PyValue::as_str).collect();
    assert!(
        !joined.contains("fg"),
        "已終結的 capture 不得被後續命令汙染：{joined:?}"
    );
}

#[test]
fn result_tail_fallback_from_registry() {
    let mut h = CoreHarness::new(SHELL_PROFILE);
    h.to_ready();
    let _hold = h.start_foreground_command("sleep 9");
    let mut req = agent_submit("bgq");
    req.mode = "bg".into();
    let id = h.rpc(SessionRequest::Submit(req)).unwrap()["cmd_id"]
        .as_str()
        .unwrap()
        .to_string();
    let r = h
        .rpc(SessionRequest::ResultTail {
            cmd_id: id.clone(),
            from_chunk: 3,
            limit: 200,
        })
        .unwrap();
    assert_eq!(
        keys(&r),
        vec![
            "ok",
            "cmd_id",
            "status",
            "error_code",
            "from_seq",
            "last_seq",
            "from_chunk",
            "next_chunk",
            "chunks"
        ]
    );
    assert_eq!(r["status"], PyValue::from("accepted"));
    assert_eq!(r["error_code"], PyValue::Null);
    assert_eq!(
        r["from_seq"],
        PyValue::Int(sw_proto::pyjson::PyInt::Small(0))
    );
    assert_eq!(
        r["from_chunk"],
        PyValue::Int(sw_proto::pyjson::PyInt::Small(3))
    );
    assert_eq!(
        r["next_chunk"],
        PyValue::Int(sw_proto::pyjson::PyInt::Small(3))
    );
    assert_eq!(r["chunks"], PyValue::List(vec![]));
    // 非 background 紀錄 → CMD_NOT_FOUND。
    let fg = h.rpc(SessionRequest::Submit(agent_submit("fgq"))).unwrap()["cmd_id"]
        .as_str()
        .unwrap()
        .to_string();
    let r = h
        .rpc(SessionRequest::ResultTail {
            cmd_id: fg,
            from_chunk: 0,
            limit: 1,
        })
        .unwrap();
    assert_eq!(r["error_code"], PyValue::from("CMD_NOT_FOUND"));
}

#[test]
fn result_tail_negative_limit_exception() {
    let mut cap = BackgroundCapture::new("c".into(), "s".into(), 1, 0, 2.0, "t".into(), 0, 1);
    for c in ["a", "b", "c", "d"] {
        cap.add_chunk(c, BG_CAPTURE_MAX_BYTES);
    }
    assert!(
        cap.result(0, -1, 0).is_err(),
        "start + limit < 0 → islice ValueError"
    );
    let r = cap.result(3, -1, 0).expect("start 3、stop 2 → 空");
    assert_eq!(r["ok"], PyValue::Bool(true));
    assert_eq!(r["chunks"], PyValue::List(vec![]));
    assert_eq!(
        r["next_chunk"],
        PyValue::Int(sw_proto::pyjson::PyInt::Small(3))
    );
}

// ---------------------------------------------------------------------------
// reboot（#55 端到端）
// ---------------------------------------------------------------------------

#[test]
fn reboot_agent_enters_recovering_expect_reboot() {
    let h = ActorRig::new(SHELL_PROFILE).ready();
    let id = h.submit_ok("reboot");
    wait_tx(&h, b"reboot\n");
    advance_until(&h, Duration::from_millis(200), "reboot 收尾", |h| {
        is_done(h, &id)
    });
    let rec = h.command_get(&id);
    assert_eq!(rec["status"], PyValue::from("done"));
    assert_eq!(rec["execution_mode"], PyValue::from("line"));
    assert_eq!(rec["stdout"], PyValue::from(""));
    assert_eq!(rec["partial"], PyValue::Bool(true));
    assert_eq!(rec["recovery_action"], PyValue::from("EXPECT_REBOOT"));
    let s = h.get_state();
    assert_eq!(s["state"], PyValue::from("RECOVERING"));
    assert_eq!(s["recovering"], PyValue::Bool(true));
    assert_ne!(
        s["boot_quiet_remaining_s"],
        PyValue::Null,
        "reboot TX 結算後 arm quiet（#55）"
    );
    assert_eq!(s["ready_reconfirm_pending"], PyValue::Bool(true));
}

#[test]
fn reboot_arms_quiet_only_after_tx_settled_e2e() {
    // #55：真 actor＋OpRunner：reboot 出列後、TX 提交前 quiet 未 arm；TX 結算後 arm。
    let h = ActorRig::new(SHELL_PROFILE).ready();
    h.dev.script_write(vec![FakeWriteStep::PauseBeforeSubmit]);
    let id = h.submit_ok("reboot");
    assert!(
        h.dev.wait_paused(Duration::from_secs(2)),
        "reboot TX 應進入 PAL"
    );
    let s = h.get_state();
    assert_eq!(
        s["boot_quiet_remaining_s"],
        PyValue::Null,
        "TX 結算前不得 arm"
    );
    assert!(!h.fence.with(|f| f.quiet_active));
    h.dev.release_pause();
    // 審查 F14：fence 的 quiet 由 TxWriter 於 TX 臨界區同步設定，actor 隨後同步 QuietState——兩者都要等到。
    h.wait_for("TX 結算後 arm quiet", |h| {
        h.fence.with(|f| f.quiet_active) && h.get_state()["boot_quiet_remaining_s"] != PyValue::Null
    });
    advance_until(&h, Duration::from_millis(200), "reboot 收尾", |h| {
        is_done(h, &id)
    });
}

#[test]
fn reboot_not_submitted_does_not_arm_quiet_e2e() {
    // #55：TX 在提交點被拒（Cancelled → NotSubmitted）→ 不 arm、不進 RECOVERING。
    let h = ActorRig::new(SHELL_PROFILE).ready();
    h.dev.script_write(vec![FakeWriteStep::PauseBeforeSubmit]);
    let id = h.submit_ok("reboot");
    assert!(h.dev.wait_paused(Duration::from_secs(2)));
    // 審查 F35：先記下暫停點的 OS 提交數，最後斷言零新增（原斷言 `os_submits() == os_submits()` 恆真）。
    let submits = h.dev.os_submits();
    h.fence.with(|f| {
        // tombstone 上限 512、FIFO 淘汰（#38）：撤銷全部 rig 內可能的 op id（皆 < 512）。
        for n in 0..sw_core::tx_writer::CANCELLED_OPS_MAX as u64 {
            f.cancelled.insert(OpId(n));
        }
    });
    h.dev.release_pause();
    let rec = h.wait_done(&id);
    h.fence.with(|f| f.cancelled.clear());
    assert_eq!(rec["error_code"], PyValue::from("FLUSHED_BY_RECOVERY"));
    assert_eq!(h.dev.os_submits(), submits, "提交點被拒：零 OS 提交");
    let s = h.get_state();
    assert_eq!(s["state"], PyValue::from("READY"));
    assert_eq!(s["boot_quiet_remaining_s"], PyValue::Null);
    assert_eq!(s["ready_reconfirm_pending"], PyValue::Bool(false));
    assert!(!h.fence.with(|f| f.quiet_active));
    assert!(!wal_tx_has(&h, b"reboot\n"));
}

#[test]
fn reboot_recovery_waits_quiet_then_login_ready() {
    let h = ActorRig::new(SHELL_PROFILE).ready();
    let id = h.submit_ok("reboot");
    wait_tx(&h, b"reboot\n");
    advance_until(&h, Duration::from_millis(200), "reboot 收尾", |h| {
        is_done(h, &id)
    });
    h.wait_state("RECOVERING");
    let _ = h.dev.take_tx();
    let calls = h.dev.write_calls();
    // 開機訊息：quiet 期間不送任何 probe。
    h.inject_rx(BANNER);
    h.inject_rx(b"Starting kernel ...\r\n");
    h.advance(Duration::from_secs(5));
    assert_eq!(h.dev.write_calls(), calls, "quiet 期間不得送 probe");
    assert_eq!(state_of(&h), "RECOVERING");
    // prompt 出現解除 quiet；下一個 tick 以 ensure_ready 登入 → READY。
    h.dev.on_tx_reply(b"\n", b"\r\nroot@t:/# ");
    h.inject_rx(b"\r\nroot@t:/# ");
    advance_until(&h, Duration::from_millis(500), "恢復 READY", |h| {
        state_of(h) == "READY"
    });
    let s = h.get_state();
    assert_eq!(s["recovering"], PyValue::Bool(false));
    assert_eq!(s["ready_reconfirm_pending"], PyValue::Bool(false));
    assert_eq!(s["last_error"], PyValue::Null);
}

#[test]
fn reboot_recovery_timeout_to_attached() {
    // 硬上限 deadline_cap = start + 180 + hard_timeout(60)：板子持續吐 banner 使 quiet 不斷延長 → 到上限收尾。
    let h = ActorRig::new(SHELL_PROFILE).ready();
    let id = h.submit_ok("reboot");
    wait_tx(&h, b"reboot\n");
    advance_until(&h, Duration::from_millis(200), "reboot 收尾", |h| {
        is_done(h, &id)
    });
    h.wait_state("RECOVERING");
    let start = h.clock.elapsed();
    let deadline = Instant::now() + Duration::from_secs(15);
    while state_of(&h) == "RECOVERING" {
        assert!(Instant::now() < deadline, "reboot recovery 未在上限收尾");
        h.inject_rx(BANNER);
        h.advance(Duration::from_secs(10));
    }
    let waited = (h.clock.elapsed() - start).as_secs_f64();
    assert!(
        waited >= 238.0 && waited <= 252.0,
        "應於 ~240 s 上限收尾，實際 {waited}"
    );
    let s = h.get_state();
    assert_eq!(s["state"], PyValue::from("ATTACHED"));
    assert_eq!(s["last_error"], PyValue::from("RECOVERY_TIMEOUT"));
    assert_eq!(s["recovering"], PyValue::Bool(false));
}

#[test]
fn reboot_prompt_returns_quickly_ok_not_partial() {
    let h = ActorRig::new(SHELL_PROFILE).ready();
    h.dev.on_tx_reply(b"reboot\n", b"reboot\r\nroot@t:/# ");
    let id = h.submit_ok("reboot");
    let rec = h.wait_done(&id);
    assert_eq!(rec["status"], PyValue::from("done"));
    assert_eq!(rec["partial"], PyValue::Bool(false));
    assert_eq!(rec["recovery_action"], PyValue::Null);
    let s = h.get_state();
    assert_eq!(s["state"], PyValue::from("READY"));
    assert_eq!(s["boot_quiet_remaining_s"], PyValue::Null, "quiet 清除");
    assert_eq!(
        s["ready_reconfirm_pending"],
        PyValue::Bool(true),
        "#162 pending 保留"
    );
    assert!(!h.fence.with(|f| f.quiet_active));
    // pending 保留 → 下一個命令被 gate 擋下（AUTOBOOT_QUIET＋retry 5.0）。
    let next = h.request(SessionRequest::Submit(agent_submit("ls")));
    assert_eq!(next["error_code"], PyValue::from("AUTOBOOT_QUIET"));
    assert_eq!(next["retry_after_s"], PyValue::from(5.0));
}

#[test]
fn reboot_flushes_commands_queued_behind_it() {
    let h = ActorRig::new(SHELL_PROFILE).ready();
    // reboot 的 TX 停在提交前，確保 q1／q2 於 reboot 結算（quiet arm → RECOVERING）前已排入佇列；
    // 否則 reboot 已結算時 submit 即被 SESSION_RECOVERING 拒絕（測試競態，與受測語意無關）。
    h.dev.script_write(vec![FakeWriteStep::PauseBeforeSubmit]);
    let reboot = h.submit_ok("reboot");
    assert!(h.dev.wait_paused(Duration::from_secs(5)), "reboot TX 應停在提交前");
    let q1 = h.submit_ok("echo a");
    let q2 = h.submit_ok("echo b");
    h.dev.release_pause();
    wait_tx(&h, b"reboot\n");
    advance_until(&h, Duration::from_millis(200), "reboot 收尾", |h| {
        is_done(h, &reboot)
    });
    assert_eq!(
        h.command_get(&q1)["error_code"],
        PyValue::from("FLUSHED_BY_RECOVERY")
    );
    assert_eq!(
        h.command_get(&q2)["error_code"],
        PyValue::from("FLUSHED_BY_RECOVERY")
    );
    assert!(!wal_tx_has(&h, b"echo a\n") && !wal_tx_has(&h, b"echo b\n"));
    let again = h.request(SessionRequest::Submit(agent_submit("ls")));
    assert_eq!(again["error_code"], PyValue::from("SESSION_RECOVERING"));
}

// ---------------------------------------------------------------------------
// WP5-3 第 1 項：`expect_reboot` 執行路由（G3；Python `origin/main:sw_core/session_manager.py:6840-6868`）
// ---------------------------------------------------------------------------

/// 串接式重開命令：`is_reboot_command()` 不猜中（不擴大猜法），只能靠 `expect_reboot` 旗標分流。
const WP53_CHAINED: &str = "echo a && reboot";

/// 是否送出過救援鍵（`_recover_after_failure` 的 CTRL_C／CTRL_D；WAL 與 PAL 兩端都查）。
fn wp53_recover_keys_sent(h: &ActorRig) -> bool {
    let wal = tx_rows(h).iter().any(|row| {
        row.get("source").and_then(PyValue::as_str) == Some("system:recover")
            || matches!(b64(row).as_slice(), b"\x03" | b"\x04")
    });
    wal || h.dev.take_tx().iter().any(|b| matches!(b, 0x03 | 0x04))
}

/// WAL 內帶 `completion_regex` 欄位的 META（Python `test_flag_wins_over_completion_regex` 斷言其不存在）。
fn wp53_completion_metas(h: &ActorRig) -> Vec<PyDict> {
    h.wal_meta_events()
        .into_iter()
        .filter(|meta| meta.get("completion_regex").is_some())
        .collect()
}

/// 對照 `tests/test_expect_reboot.py::test_flag_routes_chained_command_to_reboot_handler`：Python
/// `if expect_reboot or guessed_reboot: return self._handle_reboot_command(...)`——旗標不受命令文字限制。
/// 短 timeout、DUT 不回 prompt → reboot 分支結算為 `EXPECT_REBOOT`（partial），不得走一般逾時救援送 Ctrl-C／Ctrl-D。
#[test]
fn wp53_route_flag_uses_reboot_handler() {
    assert!(
        !sw_core::quiet_gate::is_reboot_command(WP53_CHAINED),
        "串接命令不得被猜中（不擴大猜法）"
    );
    let h = ActorRig::new(SHELL_PROFILE).ready();
    let id = h.submit_with(WP53_CHAINED, |r| {
        r.timeout_s = 1.0;
        r.expect_reboot = true;
    });
    wait_tx(&h, b"echo a && reboot\n");
    advance_until(&h, Duration::from_millis(100), "旗標命令收尾", |h| {
        is_done(h, &id)
    });
    let rec = h.command_get(&id);
    assert_eq!(rec["status"], PyValue::from("done"), "{rec:?}");
    assert_eq!(rec["execution_mode"], PyValue::from("line"), "{rec:?}");
    assert_eq!(rec["stdout"], PyValue::from(""), "{rec:?}");
    assert_eq!(rec["partial"], PyValue::Bool(true), "{rec:?}");
    assert_eq!(
        rec["recovery_action"],
        PyValue::from("EXPECT_REBOOT"),
        "{rec:?}"
    );
    assert_eq!(rec["error_code"], PyValue::Null, "{rec:?}");
    assert_eq!(rec["expect_reboot"], PyValue::Bool(true), "{rec:?}");
    assert!(
        !wp53_recover_keys_sent(&h),
        "旗標命令走 reboot 分支，不得送救援鍵"
    );
}

/// 對照 `tests/test_expect_reboot.py::test_flag_wins_over_completion_regex`：reboot 分流位於 completion pattern
/// 與 `command_completion` META 之前。假 DUT 只回 `DONE`、不回 prompt：不得因 `DONE` 正常完成，
/// 也不得寫出帶 `completion_regex` 的 META，結算為 reboot recovery。
#[test]
fn wp53_route_flag_wins_over_completion_regex() {
    let h = ActorRig::new(SHELL_PROFILE).ready();
    h.dev
        .on_tx_reply(b"echo a && reboot\n", b"echo a && reboot\r\na\r\nDONE\r\n");
    let id = h.submit_with(WP53_CHAINED, |r| {
        r.timeout_s = 1.0;
        r.expect_reboot = true;
        r.completion_regex = Some("(?m)^DONE\\r?$".into());
    });
    wait_tx(&h, b"echo a && reboot\n");
    advance_until(&h, Duration::from_millis(100), "旗標命令收尾", |h| {
        is_done(h, &id)
    });
    let rec = h.command_get(&id);
    assert_eq!(rec["status"], PyValue::from("done"), "{rec:?}");
    assert_eq!(
        rec["partial"],
        PyValue::Bool(true),
        "不得依 completion_regex 正常完成：{rec:?}"
    );
    assert_eq!(
        rec["recovery_action"],
        PyValue::from("EXPECT_REBOOT"),
        "{rec:?}"
    );
    assert_eq!(rec["stdout"], PyValue::from(""), "{rec:?}");
    assert!(
        wp53_completion_metas(&h).is_empty(),
        "reboot 分流先於 command_completion META：{:?}",
        wp53_completion_metas(&h)
    );
    assert!(!wp53_recover_keys_sent(&h), "不得送救援鍵");
}

/// 無旗標對照（`tests/test_expect_reboot.py::test_without_flag_chained_command_keeps_old_behaviour`）：
/// 串接命令不被猜中 → 維持一般命令路徑；逾時走 `_recover_after_failure` 送 CTRL_C。
#[test]
fn wp53_route_without_flag_keeps_timeout_recovery() {
    let h = ActorRig::new(SHELL_PROFILE).ready();
    h.dev.on_tx_reply(b"\x03", b"^C\r\nroot@t:/# ");
    let id = h.submit_with(WP53_CHAINED, |r| r.timeout_s = 1.0);
    wait_tx(&h, b"echo a && reboot\n");
    advance_until(&h, Duration::from_millis(100), "逾時救援收尾", |h| {
        is_done(h, &id)
    });
    let rec = h.command_get(&id);
    assert_eq!(rec["status"], PyValue::from("done"), "{rec:?}");
    assert_eq!(rec["partial"], PyValue::Bool(true), "{rec:?}");
    assert_eq!(rec["recovery_action"], PyValue::from("CTRL_C"), "{rec:?}");
    assert_eq!(rec["expect_reboot"], PyValue::Bool(false), "{rec:?}");
    assert!(wal_tx_has(&h, b"\x03"), "一般路徑逾時送 CTRL_C");
    assert_eq!(state_of(&h), "READY");
}

/// 無旗標對照：串接命令帶 `completion_regex` 時仍依 regex 正常完成，並寫出 `command_completion` META。
#[test]
fn wp53_route_without_flag_completion_regex_completes() {
    let h = ActorRig::new(SHELL_PROFILE).ready();
    h.dev
        .on_tx_reply(b"echo a && reboot\n", b"echo a && reboot\r\na\r\nDONE\r\n");
    let id = h.submit_with(WP53_CHAINED, |r| {
        r.timeout_s = 1.0;
        r.completion_regex = Some("(?m)^DONE\\r?$".into());
    });
    let rec = h.wait_done(&id);
    assert_eq!(rec["status"], PyValue::from("done"), "{rec:?}");
    assert_eq!(rec["partial"], PyValue::Bool(false), "{rec:?}");
    assert_eq!(rec["recovery_action"], PyValue::Null, "{rec:?}");
    assert_eq!(rec["stdout"], PyValue::from("a\nDONE"), "{rec:?}");
    let metas = wp53_completion_metas(&h);
    assert_eq!(metas.len(), 1, "{metas:?}");
    assert_eq!(metas[0]["event"], PyValue::from("command_completion"));
    assert!(!wp53_recover_keys_sent(&h));
    assert_eq!(state_of(&h), "READY");
}

// ---------------------------------------------------------------------------
// WP5-3 第 2 項：reboot prompt 等待時間（G3；Python `origin/main:sw_core/session_manager.py:6394`、
// `:6448-6449`、`:6860-6864`）。旗標且非猜中 → `max(timeout_s, expected_duration_s or 0.0)`；
// 其餘（猜中的單獨 reboot）→ `min(timeout_s, 2.0)`。
// ---------------------------------------------------------------------------

const WP53_PROMPT: &[u8] = b"\r\nroot@t:/# ";

/// 送出命令後讓 OpRunner 進入 prompt 等待（期限以 FakeClock 起算，此期間時鐘不動），回傳等待起點。
fn wp53_wait_started(h: &ActorRig, tx: &[u8]) -> Duration {
    wait_tx(h, tx);
    std::thread::sleep(Duration::from_millis(150));
    h.clock.elapsed()
}

/// 以 250 ms FakeClock 步進（每步讓出 80 ms 實時，長於 RX 等待的 50 ms 時鐘切片，確保 OpRunner 觀察到每一步），
/// 直到 `cond` 成立或自 `start` 起推進滿 `limit_s`；回傳成立時距 `start` 的 FakeClock 秒數（未成立為 `None`）。
fn wp53_step_until(
    h: &ActorRig,
    start: Duration,
    limit_s: f64,
    mut cond: impl FnMut(&ActorRig) -> bool,
) -> Option<f64> {
    loop {
        let waited = (h.clock.elapsed() - start).as_secs_f64();
        if cond(h) {
            return Some(waited);
        }
        if waited >= limit_s {
            return None;
        }
        h.clock.advance(Duration::from_millis(250));
        h.send_control(ControlEvent::Tick);
        std::thread::sleep(Duration::from_millis(80));
    }
}

/// 旗標串接命令：前段 3 s 後 prompt 才回來（`timeout_s`／`expected_duration_s` 依參數）→ 正常完成。
fn wp53_assert_chained_prompt_at_three_seconds(timeout_s: f64, expected_duration_s: Option<f64>) {
    let h = ActorRig::new(SHELL_PROFILE).ready();
    h.dev
        .on_tx_reply(b"echo a && reboot\n", b"echo a && reboot\r\na\r\n");
    let id = h.submit_with(WP53_CHAINED, |r| {
        r.timeout_s = timeout_s;
        r.expected_duration_s = expected_duration_s;
        r.expect_reboot = true;
    });
    let start = wp53_wait_started(&h, b"echo a && reboot\n");
    let early = wp53_step_until(&h, start, 3.0, |h| is_done(h, &id));
    assert_eq!(
        early,
        None,
        "prompt 等待期限應為 max(timeout {timeout_s}, expected {expected_duration_s:?})，不得在 3 s 前結算：{:?}",
        h.command_get(&id)
    );
    h.inject_rx(WP53_PROMPT);
    let rec = h.wait_done(&id);
    assert_eq!(rec["status"], PyValue::from("done"), "{rec:?}");
    assert_eq!(rec["partial"], PyValue::Bool(false), "{rec:?}");
    assert_eq!(
        rec["recovery_action"],
        PyValue::Null,
        "不得進 EXPECT_REBOOT：{rec:?}"
    );
    assert_eq!(rec["error_code"], PyValue::Null, "{rec:?}");
    assert_eq!(rec["stdout"], PyValue::from("a"), "{rec:?}");
    assert_eq!(rec["expect_reboot"], PyValue::Bool(true), "{rec:?}");
    let s = h.get_state();
    assert_eq!(
        s["state"],
        PyValue::from("READY"),
        "不得進 RECOVERING：{s:?}"
    );
    assert_eq!(s["recovering"], PyValue::Bool(false), "{s:?}");
    assert!(!wp53_recover_keys_sent(&h), "不得送救援鍵");
}

/// 對照 `tests/test_expect_reboot.py::test_chained_command_prompt_after_pre_segment_is_not_treated_as_recovery`
/// 與 `::test_pty_chained_reboot_with_flag`：timeout 6 s、prompt 3 s 後回來（> 舊的固定 2 s）仍判成功。
#[test]
fn wp53_wait_chained_prompt_after_three_seconds() {
    wp53_assert_chained_prompt_at_three_seconds(6.0, None);
}

/// `max(timeout_s, expected_duration_s)`：timeout 1 s、expected 5 s → 期限 5 s，3 s 回 prompt 仍成功
/// （對照 `::test_flag_routes_chained_command_to_reboot_handler` 的 `prompt_wait_s == 30.0`）。
#[test]
fn wp53_wait_expected_duration_extends_timeout() {
    wp53_assert_chained_prompt_at_three_seconds(1.0, Some(5.0));
}

/// `max(timeout_s, expected_duration_s)`：timeout 5 s、expected 1 s → timeout 為下限，3 s 回 prompt 仍成功。
#[test]
fn wp53_wait_timeout_remains_lower_bound() {
    wp53_assert_chained_prompt_at_three_seconds(5.0, Some(1.0));
}

/// 超出 `Duration` 範圍的期限（RPC `float(expected_duration_s)` 不設上限）：Python `deadline = monotonic() + 1e300`
/// 實質無限等待、不拋例外（`uart_io.py:2635-2648`）。Rust 須飽和而非 panic；prompt 回來即正常完成。
#[test]
fn wp53_wait_huge_expected_duration_saturates() {
    for expected in [1e300, f64::INFINITY] {
        let h = ActorRig::new(SHELL_PROFILE).ready();
        let id = h.submit_with(WP53_CHAINED, |r| {
            r.timeout_s = 1.0;
            r.expected_duration_s = Some(expected);
            r.expect_reboot = true;
        });
        let start = wp53_wait_started(&h, b"echo a && reboot\n");
        assert_eq!(
            wp53_step_until(&h, start, 3.0, |h| is_done(h, &id)),
            None,
            "expected {expected}：期限飽和為無限，不得提早結算：{:?}",
            h.command_get(&id)
        );
        h.inject_rx(WP53_PROMPT);
        let rec = h.wait_done(&id);
        assert_eq!(rec["status"], PyValue::from("done"), "{rec:?}");
        assert_eq!(rec["partial"], PyValue::Bool(false), "{rec:?}");
        assert_eq!(rec["recovery_action"], PyValue::Null, "{rec:?}");
        assert_eq!(state_of(&h), "READY");
    }
}

/// 單獨 `reboot` 帶旗標、DUT 不回 prompt：等待時間（自 prompt 等待起點起算的 FakeClock 秒數）與結算紀錄。
fn wp53_plain_reboot_flag_settle_s(timeout_s: f64) -> f64 {
    let h = ActorRig::new(SHELL_PROFILE).ready();
    let id = h.submit_with("reboot", |r| {
        r.timeout_s = timeout_s;
        r.expected_duration_s = Some(30.0);
        r.expect_reboot = true;
    });
    let start = wp53_wait_started(&h, b"reboot\n");
    let settled = wp53_step_until(&h, start, 10.0, |h| is_done(h, &id))
        .expect("單獨 reboot 應在 10 s 內結算");
    let rec = h.command_get(&id);
    assert_eq!(rec["partial"], PyValue::Bool(true), "{rec:?}");
    assert_eq!(
        rec["recovery_action"],
        PyValue::from("EXPECT_REBOOT"),
        "{rec:?}"
    );
    assert_eq!(state_of(&h), "RECOVERING");
    settled
}

/// 對照 `tests/test_expect_reboot.py::test_flag_with_plain_reboot_keeps_two_second_prompt_wait`：猜中的單獨 reboot
/// 帶旗標時 `prompt_wait_s is None` → `min(timeout_s, 2.0)`；timeout 10 s、expected 30 s 仍約 2 s 進 recovery。
#[test]
fn wp53_wait_plain_reboot_flag_keeps_two_seconds() {
    let settled = wp53_plain_reboot_flag_settle_s(10.0);
    assert!(
        (2.0..3.0).contains(&settled),
        "單獨 reboot 維持 min(timeout, 2 s)，實際 {settled}"
    );
}

/// 同上，timeout 小於 2 s 時取 timeout（`min(1.0, 2.0)`），不受 expected 30 s 影響。
#[test]
fn wp53_wait_plain_reboot_flag_short_timeout_uses_timeout() {
    let settled = wp53_plain_reboot_flag_settle_s(1.0);
    assert!(
        (1.0..2.0).contains(&settled),
        "timeout < 2 s 時取 timeout，實際 {settled}"
    );
}

/// 對照 `tests/test_expect_reboot.py::test_chained_command_without_prompt_still_falls_to_recovery`：旗標串接命令
/// 一直沒有 prompt → 等滿 `timeout_s`（3 s，`bridge.waits == [3.0]`）才回 `EXPECT_REBOOT`，不送救援鍵。
#[test]
fn wp53_wait_chained_without_prompt_waits_full_deadline() {
    let h = ActorRig::new(SHELL_PROFILE).ready();
    let id = h.submit_with(WP53_CHAINED, |r| {
        r.timeout_s = 3.0;
        r.expect_reboot = true;
    });
    let start = wp53_wait_started(&h, b"echo a && reboot\n");
    let settled = wp53_step_until(&h, start, 10.0, |h| is_done(h, &id))
        .expect("旗標串接命令應在 10 s 內結算");
    assert!(
        (3.0..4.0).contains(&settled),
        "旗標串接命令應等滿 timeout 3 s，實際 {settled}"
    );
    let rec = h.command_get(&id);
    assert_eq!(rec["partial"], PyValue::Bool(true), "{rec:?}");
    assert_eq!(
        rec["recovery_action"],
        PyValue::from("EXPECT_REBOOT"),
        "{rec:?}"
    );
    assert_eq!(state_of(&h), "RECOVERING");
    assert!(!wp53_recover_keys_sent(&h), "reboot 分支不得送救援鍵");
}

/// 無旗標對照：串接命令不被猜中 → 一般命令路徑，依一般 timeout（3 s，silence 上限 min(3, 30)）送 CTRL_C，
/// 不得誤套固定兩秒的 reboot 等待。
#[test]
fn wp53_wait_chained_without_flag_uses_command_timeout() {
    let h = ActorRig::new(SHELL_PROFILE).ready();
    h.dev.on_tx_reply(b"\x03", b"^C\r\nroot@t:/# ");
    let id = h.submit_with(WP53_CHAINED, |r| r.timeout_s = 3.0);
    let start = wp53_wait_started(&h, b"echo a && reboot\n");
    let rescued = wp53_step_until(&h, start, 10.0, |h| wal_tx_has(h, b"\x03"))
        .expect("應在 10 s 內送出 CTRL_C");
    assert!(
        (3.0..4.0).contains(&rescued),
        "無旗標串接命令依一般 timeout 3 s 救援，實際 {rescued}"
    );
    let rec = h.wait_done(&id);
    assert_eq!(rec["recovery_action"], PyValue::from("CTRL_C"), "{rec:?}");
    assert_eq!(rec["expect_reboot"], PyValue::Bool(false), "{rec:?}");
    assert_eq!(state_of(&h), "READY");
}

/// 裁決 s2 D2：passthrough 1:1 沿用 Python——`guessed_reboot = not passthrough and _is_reboot_command(...)`，
/// 故 passthrough 的 `reboot` 文字帶旗標屬「旗標且非猜中」，使用完整期限 `max(timeout_s, expected or 0)`（4 s），
/// 不是單獨 reboot 的兩秒。
#[test]
fn wp53_wait_passthrough_reboot_flag_uses_full_deadline() {
    let h = passthrough_ready(UBOOT_PASSTHROUGH_PROFILE);
    let id = h.submit_with("reboot", |r| {
        r.timeout_s = 4.0;
        r.expect_reboot = true;
    });
    let start = wp53_wait_started(&h, b"reboot\n");
    let settled = wp53_step_until(&h, start, 10.0, |h| is_done(h, &id))
        .expect("passthrough 旗標 reboot 應在 10 s 內結算");
    assert!(
        (4.0..5.0).contains(&settled),
        "passthrough 旗標 reboot 應等滿 timeout 4 s，實際 {settled}"
    );
    let rec = h.command_get(&id);
    assert_eq!(rec["partial"], PyValue::Bool(true), "{rec:?}");
    assert_eq!(
        rec["recovery_action"],
        PyValue::from("EXPECT_REBOOT"),
        "{rec:?}"
    );
    assert!(!wp53_recover_keys_sent(&h), "passthrough 不送救援鍵");
}

// ---------------------------------------------------------------------------
// WP5-3 第 3 項：旗標命令取得 actor reboot arming 與完成後 gate（G3；Python
// `origin/main:sw_core/session_manager.py:6412-6447`、`:6462-6473`）。`_handle_reboot_command` 不分旗標或猜中：
// 送出前 `invalidate_deferred("reboot")`；TX 已提交（confirmed > 0 或 unknown）後於同一寫入鎖內 `arm_boot_quiet()`；
// prompt 回來只 `clear_boot_quiet()`，不 confirm ready（#162）。
// ---------------------------------------------------------------------------

/// 旗標串接命令的 TX 停在提交前：quiet／reconfirm 不得提前 arm；放行並實際提交後兩者都成立（fence 與公開狀態）。
#[test]
fn wp53_arm_flag_after_tx_submission() {
    let h = ActorRig::new(SHELL_PROFILE).ready();
    h.dev.script_write(vec![FakeWriteStep::PauseBeforeSubmit]);
    let id = h.submit_with(WP53_CHAINED, |r| {
        r.timeout_s = 1.0;
        r.expect_reboot = true;
    });
    assert!(
        h.dev.wait_paused(Duration::from_secs(2)),
        "旗標命令 TX 應進入 PAL"
    );
    let s = h.get_state();
    assert_eq!(
        s["boot_quiet_remaining_s"],
        PyValue::Null,
        "TX 提交前不得 arm quiet：{s:?}"
    );
    assert_eq!(
        s["ready_reconfirm_pending"],
        PyValue::Bool(false),
        "TX 提交前不得 arm reconfirm：{s:?}"
    );
    assert!(!h.fence.with(|f| f.quiet_active || f.reconfirm_pending));
    h.dev.release_pause();
    // 審查 F14：fence 由 TxWriter 於提交的同一次 Fence 臨界區設定，actor 隨後同步 QuietState——兩者都要等到。
    h.wait_for("旗標命令 TX 提交後 arm quiet 與 reconfirm", |h| {
        let s = h.get_state();
        h.fence.with(|f| f.quiet_active && f.reconfirm_pending)
            && s["boot_quiet_remaining_s"] != PyValue::Null
            && s["ready_reconfirm_pending"] == PyValue::Bool(true)
    });
    assert!(wal_tx_has(&h, b"echo a && reboot\n"));
    advance_until(&h, Duration::from_millis(100), "旗標命令收尾", |h| {
        is_done(h, &id)
    });
    let rec = h.command_get(&id);
    assert_eq!(
        rec["recovery_action"],
        PyValue::from("EXPECT_REBOOT"),
        "{rec:?}"
    );
    assert_eq!(state_of(&h), "RECOVERING");
}

/// 對照 `tests/test_expect_reboot.py::test_chained_command_prompt_after_pre_segment_is_not_treated_as_recovery`：
/// 旗標串接命令 3 s 後 prompt 回來 → 正常完成，但只清 TX 靜默維度、`ready_reconfirm_pending` 保留（#162）；
/// 下一筆 shell agent 命令被 gate 擋為 `AUTOBOOT_QUIET`（retry 5.0），零新增 UART TX。
#[test]
fn wp53_arm_flag_prompt_keeps_reconfirm() {
    let h = ActorRig::new(SHELL_PROFILE).ready();
    h.dev
        .on_tx_reply(b"echo a && reboot\n", b"echo a && reboot\r\na\r\n");
    let id = h.submit_with(WP53_CHAINED, |r| {
        r.timeout_s = 6.0;
        r.expect_reboot = true;
    });
    let start = wp53_wait_started(&h, b"echo a && reboot\n");
    let early = wp53_step_until(&h, start, 3.0, |h| is_done(h, &id));
    assert_eq!(early, None, "prompt 未回不得結算：{:?}", h.command_get(&id));
    h.inject_rx(WP53_PROMPT);
    let rec = h.wait_done(&id);
    assert_eq!(rec["status"], PyValue::from("done"), "{rec:?}");
    assert_eq!(rec["partial"], PyValue::Bool(false), "{rec:?}");
    assert_eq!(rec["recovery_action"], PyValue::Null, "{rec:?}");
    let s = h.get_state();
    assert_eq!(s["state"], PyValue::from("READY"), "{s:?}");
    assert_eq!(s["boot_quiet_remaining_s"], PyValue::Null, "quiet 清除：{s:?}");
    assert_eq!(
        s["ready_reconfirm_pending"],
        PyValue::Bool(true),
        "#162：prompt 回來不是 READY 再確認證據，pending 保留：{s:?}"
    );
    assert!(!h.fence.with(|f| f.quiet_active), "TX 靜默維度清除");
    assert!(h.fence.with(|f| f.reconfirm_pending), "fence reconfirm 保留");

    let submits = h.dev.os_submits();
    let _ = h.dev.take_tx();
    let next = h.request(SessionRequest::Submit(agent_submit("ls")));
    assert_eq!(next["error_code"], PyValue::from("AUTOBOOT_QUIET"), "{next:?}");
    assert_eq!(next["retry_after_s"], PyValue::from(5.0), "{next:?}");
    assert!(next.get("cmd_id").is_none(), "{next:?}");
    std::thread::sleep(Duration::from_millis(100));
    assert_eq!(h.dev.os_submits(), submits, "被 gate 擋下：零新增 OS 提交");
    assert!(h.dev.take_tx().is_empty(), "被 gate 擋下：零新增 UART TX");
    assert!(!wal_tx_has(&h, b"ls\n"));
}

/// 對照（`reboot_not_submitted_does_not_arm_quiet_e2e` 的旗標版）：旗標串接命令的 TX 在提交點被撤銷、零 OS 提交 →
/// 不 arm quiet／reconfirm、不進 RECOVERING（Python 只在 `confirmed > 0 or outcome == "unknown"` 時 arm）。
#[test]
fn wp53_arm_flag_not_submitted_does_not_arm() {
    let h = ActorRig::new(SHELL_PROFILE).ready();
    h.dev.script_write(vec![FakeWriteStep::PauseBeforeSubmit]);
    let id = h.submit_with(WP53_CHAINED, |r| {
        r.timeout_s = 1.0;
        r.expect_reboot = true;
    });
    assert!(h.dev.wait_paused(Duration::from_secs(2)));
    let submits = h.dev.os_submits();
    h.fence.with(|f| {
        // tombstone 上限 512、FIFO 淘汰（#38）：撤銷全部 rig 內可能的 op id（皆 < 512）。
        for n in 0..sw_core::tx_writer::CANCELLED_OPS_MAX as u64 {
            f.cancelled.insert(OpId(n));
        }
    });
    h.dev.release_pause();
    let rec = h.wait_done(&id);
    h.fence.with(|f| f.cancelled.clear());
    assert_eq!(rec["error_code"], PyValue::from("FLUSHED_BY_RECOVERY"), "{rec:?}");
    assert_eq!(h.dev.os_submits(), submits, "提交點被拒：零 OS 提交");
    let s = h.get_state();
    assert_eq!(s["state"], PyValue::from("READY"), "{s:?}");
    assert_eq!(s["boot_quiet_remaining_s"], PyValue::Null, "{s:?}");
    assert_eq!(s["ready_reconfirm_pending"], PyValue::Bool(false), "{s:?}");
    assert!(!h.fence.with(|f| f.quiet_active || f.reconfirm_pending));
    assert!(!wal_tx_has(&h, b"echo a && reboot\n"));
}

/// 出列決策（sans-IO）：旗標串接命令與猜中的 `reboot` 同樣在送出前失效 deferred 輸入（BC-02 `RebootCommand`）、
/// 登記 `reboot_arm_op`，TX 結算後再失效一次（`QuietRearm`），reboot 在途的 human 按鍵不得回放；
/// 無旗標的同一命令三者皆不發生。
#[test]
fn wp53_arm_flag_invalidates_deferred_and_registers_arm_op() {
    use sw_core::op_runner::OpSpec;

    for flag in [true, false] {
        let case = format!("expect_reboot={flag}");
        let mut h = CoreHarness::new(SHELL_PROFILE);
        h.to_ready();
        h.attach_pty_console("c1", true);
        let e0 = h.core.generations().input_epoch;
        let mut req = agent_submit(WP53_CHAINED);
        req.expect_reboot = flag;
        let (resp, effects) = h.rpc_full(SessionRequest::Submit(req));
        assert_eq!(resp.expect("submit")["ok"], PyValue::Bool(true), "{case}");
        let op_id = effects
            .iter()
            .find_map(|effect| match effect {
                Effect::StartOp {
                    grant,
                    spec: OpSpec::Command(_),
                } => Some(grant.op_id),
                _ => None,
            })
            .expect("READY 空佇列應立即出列");
        let bumped = u64::from(flag);
        assert_eq!(
            h.core.generations().input_epoch,
            e0 + bumped,
            "{case}：送出前失效（RebootCommand）"
        );
        assert_eq!(
            h.fence.with(|f| f.reboot_arm_op),
            flag.then_some(op_id),
            "{case}：TxWriter 提交點 arming 登記"
        );
        h.input("c1", b"k");
        assert_eq!(h.deferred_len("c1"), 1, "{case}：agent 命令期間按鍵進 deferred");
        let epoch = h.epoch();
        h.feed(ControlEvent::UartTxCompleted {
            epoch,
            op_id,
            confirmed_bytes: WP53_CHAINED.len() + 1,
            unknown: false,
        });
        assert_eq!(
            h.core.generations().input_epoch,
            e0 + 2 * bumped,
            "{case}：TX 結算 arm quiet 為第二個失效點"
        );
        assert_eq!(
            h.deferred_len("c1"),
            if flag { 0 } else { 1 },
            "{case}：reboot 在途期間的按鍵被清掉"
        );
        assert_eq!(
            h.public()["ready_reconfirm_pending"],
            PyValue::Bool(flag),
            "{case}"
        );
    }
}

// ---------------------------------------------------------------------------
// recover
// ---------------------------------------------------------------------------

#[test]
fn recover_ready_path_returns_prompt_timeout_recovered() {
    let h = ActorRig::new(SHELL_PROFILE).ready();
    h.dev.on_tx_reply(b"\x03", b"^C\r\nroot@t:/# ");
    let r = h.request(SessionRequest::Recover {
        timeout_s: 2.0,
        force: false,
    });
    assert_eq!(
        keys(&r),
        vec!["ok", "error_code", "stdout", "partial", "recovery_action"]
    );
    assert_eq!(r["ok"], PyValue::Bool(true));
    assert_eq!(r["error_code"], PyValue::from("PROMPT_TIMEOUT_RECOVERED"));
    assert_eq!(r["partial"], PyValue::Bool(true));
    assert_eq!(r["recovery_action"], PyValue::from("CTRL_C"));
    assert_eq!(r["stdout"], PyValue::from("^C"));
    assert_eq!(state_of(&h), "READY");
}

#[test]
fn recover_keys_wal_source_system_recover() {
    let h = ActorRig::new(SHELL_PROFILE).ready();
    h.dev.on_tx_reply(b"\x04", b"\r\nroot@t:/# ");
    let r = request_advancing(
        &h,
        SessionRequest::Recover {
            timeout_s: 2.0,
            force: false,
        },
        Duration::from_millis(200),
    )
    .expect("recover");
    assert_eq!(r["recovery_action"], PyValue::from("CTRL_D"));
    let rows = tx_rows(&h);
    for key in [b"\x03".as_slice(), b"\x04".as_slice()] {
        let row = rows
            .iter()
            .find(|r| b64(r) == key)
            .expect("救援鍵應有 TX WAL");
        assert_eq!(row["source"], PyValue::from("system:recover"));
    }
    // self-test probe TX：source system:self_test。
    let st = h.request(SessionRequest::SelfTest {
        timeout_s: 2.0,
        strict_human_lock: false,
    });
    assert_eq!(st["classification"], PyValue::from("OK"));
    assert!(
        tx_rows(&h)
            .iter()
            .any(|r| r["source"] == PyValue::from("system:self_test"))
    );

    // ensure_ready 的密碼 TX：source system:secret、log=false → WAL 無該行。
    const LOGIN_ENV: &str = "profiles:\n  e:\n    platform: shell\n    prompt_regex: '(?m)^root@t:.*# '\n    login_regex: 'login: ?$'\n    password_regex: 'Password: ?$'\n    user_env: SW_T_U\n    pass_env: SW_T_P\n    timeout_s: 2.0\ntargets:\n  - {com: COM0, profile: e, act_no: 1, alias: e+1, device_by_id: /s/by-id/usb-A}\n";
    let l = ActorRig::new(LOGIN_ENV);
    l.set_env("SW_T_U", "root");
    l.set_env("SW_T_P", "s3cret");
    l.dev.on_tx_reply(b"\n", b"\r\nlogin: ");
    l.dev.on_tx_reply(b"root\n", b"root\r\nPassword: ");
    l.dev.on_tx_reply(b"s3cret\n", b"\r\nroot@t:/# ");
    l.dev.on_tx_match(
        r"__READY__[0-9a-f]+",
        Box::new(|line: &[u8]| {
            let text = String::from_utf8_lossy(line).into_owned();
            let token = text
                .split("__READY__")
                .nth(1)
                .unwrap_or("")
                .trim()
                .to_string();
            format!("__READY__{token}\r\nroot@t:/# ").into_bytes()
        }),
    );
    let l = l.attach();
    l.wait_state("READY");
    let rows = tx_rows(&l);
    assert!(
        !rows.iter().any(|r| b64(r) == b"s3cret\n"),
        "密碼 TX 不得寫入 WAL"
    );
    assert!(
        !rows
            .iter()
            .any(|r| r["source"] == PyValue::from("system:secret"))
    );
    assert!(rows.iter().any(|r| b64(r) == b"root\n"));
}

#[test]
fn recover_force_timeout_after_10s() {
    // ATTACHED（nonce 未回）＋force → clear＋reattach，10 s 內未 READY → FORCE_RECOVER_TIMEOUT。
    let h = ActorRig::new(SHELL_PROFILE);
    h.dev.on_tx_reply(b"\n", b"\r\nroot@t:/# ");
    let h = h.attach();
    h.wait_state("ATTACHED");
    let r = request_advancing(
        &h,
        SessionRequest::Recover {
            timeout_s: 2.0,
            force: true,
        },
        Duration::from_millis(500),
    )
    .expect("recover");
    assert_eq!(
        keys(&r),
        vec!["ok", "error_code", "action", "recovered", "session"]
    );
    assert_eq!(r["ok"], PyValue::Bool(false));
    assert_eq!(r["error_code"], PyValue::from("FORCE_RECOVER_TIMEOUT"));
    assert_eq!(r["action"], PyValue::from("FORCE_CLEAR_REATTACH"));
    assert_eq!(r["recovered"], PyValue::Bool(false));
}

#[test]
fn recover_released() {
    let h = ActorRig::new(SHELL_PROFILE).ready();
    let rel = h.request(SessionRequest::Release {
        source: "cli".into(),
        reason: PyValue::Null,
    });
    assert_eq!(rel["ok"], PyValue::Bool(true));
    h.wait_state("RELEASED");
    let r = h.request(SessionRequest::Recover {
        timeout_s: 2.0,
        force: true,
    });
    assert_eq!(
        keys(&r),
        vec!["ok", "released", "recommended_action", "session"]
    );
    assert_eq!(r["released"], PyValue::Bool(true));
    assert_eq!(r["recommended_action"], PyValue::from("device_attach"));
}

#[test]
fn recover_no_bridge_reattach() {
    let mut h = CoreHarness::new(SHELL_PROFILE);
    h.feed(ControlEvent::DeviceAttached {
        by_id: DEVICE_BY_ID.into(),
        real_path: REAL_PATH.into(),
    });
    h.feed(ControlEvent::BridgeOpened {
        epoch_hint: 0,
        result: Err(OpenFailure::Other(PalError::io(5, "Input/output error"))),
    });
    assert_eq!(pget(&h.public(), "state"), PyValue::from("DETACHED"));
    let (r, effects) = h.rpc_full(SessionRequest::Recover {
        timeout_s: 2.0,
        force: false,
    });
    let r = r.unwrap();
    assert_eq!(keys(&r), vec!["ok", "recovering", "action", "session"]);
    assert_eq!(r["recovering"], PyValue::Bool(false));
    assert_eq!(r["action"], PyValue::from("REATTACH"));
    assert!(
        effects
            .iter()
            .any(|e| matches!(e, Effect::OpenBridge { .. }))
    );
}

#[test]
fn recover_attached_reprobe_not_recovered_last_error_field() {
    // SC-34：REPROBE 未恢復 → error_code SESSION_NOT_READY（不得等於 last_error）＋頂層 last_error／detail。
    let h = ActorRig::new(SHELL_PROFILE);
    h.dev.on_tx_reply(b"\n", b"\r\nroot@t:/# ");
    let h = h.attach();
    h.wait_state("ATTACHED");
    let _ = h.dev.take_tx();
    h.dev.on_tx_reply(b"\n", b"\r\nroot@t:/# ");
    let r = h.request(SessionRequest::Recover {
        timeout_s: 2.0,
        force: false,
    });
    assert_eq!(
        keys(&r),
        vec![
            "ok",
            "recovering",
            "action",
            "recovered",
            "session",
            "error_code",
            "last_error",
            "last_error_detail"
        ]
    );
    assert_eq!(r["ok"], PyValue::Bool(true));
    assert_eq!(r["recovered"], PyValue::Bool(false));
    assert_eq!(r["error_code"], PyValue::from("SESSION_NOT_READY"));
    assert_ne!(r["error_code"], PyValue::from("READY_NONCE_TIMEOUT"));
    assert_eq!(r["last_error"], PyValue::from("READY_NONCE_TIMEOUT"));
    let PyValue::Dict(session) = r["session"].clone() else {
        panic!()
    };
    assert_eq!(r["last_error_detail"], session["last_error_detail"]);
}

#[test]
fn recover_attached_reprobe_bootloader_classification() {
    let h = ActorRig::new(SHELL_PROFILE);
    h.dev.on_tx_reply(b"\n", b"\r\n=> ");
    let h = h.attach();
    h.wait_state("ATTACHED");
    let _ = h.dev.take_tx();
    h.dev.on_tx_reply(b"\n", b"\r\n=> ");
    let r = h.request(SessionRequest::Recover {
        timeout_s: 2.0,
        force: false,
    });
    assert_eq!(r["error_code"], PyValue::from("SESSION_NOT_READY"));
    assert_eq!(
        keys(&r),
        vec![
            "ok",
            "recovering",
            "action",
            "recovered",
            "session",
            "error_code",
            "last_error",
            "last_error_detail",
            "classification",
            "recommended_action",
            "matched_prompt"
        ]
    );
    assert_eq!(r["classification"], PyValue::from("BOOTLOADER"));
    assert_eq!(
        r["recommended_action"],
        PyValue::from("recover_interactive")
    );
    assert_eq!(r["matched_prompt"], PyValue::from("^=> $"));
}

#[test]
fn recover_ready_path_force_fallbacks() {
    // (a) 救援鍵皆失敗 → ATTACHED → force clear＋reattach → READY → PROMPT_TIMEOUT_FORCE_RECOVERED。
    let h = ActorRig::new(SHELL_PROFILE).ready();
    let _ = h.dev.take_tx();
    let rx = h.request_async(SessionRequest::Recover {
        timeout_s: 1.0,
        force: false,
    });
    advance_until(&h, Duration::from_millis(100), "CTRL_D 送出", |h| {
        wal_tx_has(h, b"\x04")
    });
    // 重新 attach 時的 login probe 回 prompt。
    h.dev.on_tx_reply(b"\n", b"\r\nroot@t:/# ");
    let got: RefCell<Option<Result<PyDict, PyErr>>> = RefCell::new(None);
    advance_until(&h, Duration::from_millis(100), "force 收尾", |_| {
        if let Ok(v) = rx.try_recv() {
            *got.borrow_mut() = Some(v);
        }
        got.borrow().is_some()
    });
    let r = got.into_inner().unwrap().unwrap();
    assert_eq!(
        keys(&r),
        vec!["ok", "error_code", "partial", "recovery_action", "session"]
    );
    assert_eq!(r["ok"], PyValue::Bool(true));
    assert_eq!(
        r["error_code"],
        PyValue::from("PROMPT_TIMEOUT_FORCE_RECOVERED")
    );
    assert_eq!(r["recovery_action"], PyValue::from("FORCE_CLEAR_REATTACH"));
    assert_eq!(state_of(&h), "READY");

    // (b) reattach 後 10 s 內未 READY → 回 PROMPT_TIMEOUT／NONE（L3921 附近 fall-through）。
    let h = ActorRig::new(SHELL_PROFILE).ready();
    let r = request_advancing(
        &h,
        SessionRequest::Recover {
            timeout_s: 1.0,
            force: true,
        },
        Duration::from_millis(250),
    )
    .expect("recover");
    assert_eq!(
        r,
        sw_proto::pydict! { "ok" => false, "error_code" => "PROMPT_TIMEOUT", "partial" => true, "recovery_action" => "NONE" }
    );
}

#[test]
fn recover_key_refused_frozen_maps_flushed_by_recovery() {
    // #34：READY recover 的救援鍵在提交點被 Frozen／StaleEpoch／Cancelled 拒絕 → FLUSHED_BY_RECOVERY；其他 → EXCEPTION。
    type Inject = fn(&mut sw_core::tx_writer::FenceState);
    let cases: [(&str, Inject); 3] = [
        ("frozen", |f| f.frozen = true),
        ("stale", |f| f.active_epoch = Some(u64::MAX)),
        ("cancelled", |f| {
            // tombstone 上限 512、FIFO 淘汰（#38）：撤銷全部 rig 內可能的 op id（皆 < 512）。
            for n in 0..sw_core::tx_writer::CANCELLED_OPS_MAX as u64 {
                f.cancelled.insert(OpId(n));
            }
        }),
    ];
    for (name, inject) in cases {
        let h = ActorRig::new(SHELL_PROFILE).ready();
        let saved = h.fence.with(|f| f.active_epoch);
        h.dev.script_write(vec![FakeWriteStep::PauseBeforeSubmit]);
        let rx = h.request_async(SessionRequest::Recover {
            timeout_s: 1.0,
            force: false,
        });
        assert!(
            h.dev.wait_paused(Duration::from_secs(2)),
            "{name}：救援鍵應進入 PAL"
        );
        h.fence.with(inject);
        let submits = h.dev.os_submits();
        h.dev.release_pause();
        let r = rx
            .recv_timeout(Duration::from_secs(5))
            .expect("回覆")
            .expect("ok 回應");
        assert_eq!(
            r,
            sw_proto::pydict! { "ok" => false, "error_code" => "FLUSHED_BY_RECOVERY" },
            "{name}"
        );
        assert_eq!(h.dev.os_submits(), submits, "{name}：零 TX");
        h.fence.with(|f| {
            f.frozen = false;
            f.active_epoch = saved;
            f.cancelled.clear();
        });
    }
    // 其他拒絕（WAL unhealthy）→ Python 例外穿出（framing EXCEPTION）。
    let h = ActorRig::new(SHELL_PROFILE).ready();
    h.dev.script_write(vec![FakeWriteStep::PauseBeforeSubmit]);
    let rx = h.request_async(SessionRequest::Recover {
        timeout_s: 1.0,
        force: false,
    });
    assert!(h.dev.wait_paused(Duration::from_secs(2)));
    h.fence.with(|f| f.wal_unhealthy = true);
    h.dev.release_pause();
    let r = rx.recv_timeout(Duration::from_secs(5)).expect("回覆");
    let err = r.expect_err("wal_unhealthy 應為例外");
    assert!(err.message.contains("wal_unhealthy"), "{}", err.message);
}

// ---------------------------------------------------------------------------
// passthrough（SC-35；#94）
// ---------------------------------------------------------------------------

#[test]
fn passthrough_completion_regex_producer() {
    let h = passthrough_ready(OTHERS_PASSTHROUGH_PROFILE);
    h.dev.on_tx_reply(b"run\n", b"run\r\nout\r\n__RC__=0\r\n");
    let id = h.submit_with("run", |r| {
        r.completion_regex = Some("(?m)^__RC__=0\\r*$".into())
    });
    let rec = h.wait_done(&id);
    assert_eq!(rec["status"], PyValue::from("done"));
    assert_eq!(rec["partial"], PyValue::Bool(false));
    assert_eq!(
        rec["stdout"],
        PyValue::from(clean_text("run\r\nout\r\n__RC__=0\r"))
    );
    assert_eq!(h.dev.take_tx(), b"run\n".to_vec(), "只有命令本身的 TX");
}

#[test]
fn passthrough_usable_prompt_completion() {
    let h = passthrough_ready(UBOOT_PASSTHROUGH_PROFILE);
    h.dev
        .on_tx_reply(b"printenv\n", b"printenv\r\nbootcmd=x\r\n=> ");
    let id = h.submit_ok("printenv");
    let rec = h.wait_done(&id);
    assert_eq!(rec["status"], PyValue::from("done"));
    assert_eq!(rec["partial"], PyValue::Bool(false));
    assert_eq!(rec["stdout"], PyValue::from("bootcmd=x"));
}

#[test]
fn passthrough_unusable_prompt_timeout_no_recovery() {
    let h = passthrough_ready(OTHERS_PASSTHROUGH_PROFILE);
    h.dev.on_tx_reply(b"ls\n", b"ls\r\nfoo\r\n");
    let id = h.submit_with("ls", |r| r.timeout_s = 1.0);
    wait_tx(&h, b"ls\n");
    advance_until(&h, Duration::from_millis(100), "逾時收尾", |h| {
        is_done(h, &id)
    });
    let rec = h.command_get(&id);
    assert_eq!(rec["error_code"], PyValue::from("PROMPT_TIMEOUT"));
    assert_eq!(rec["partial"], PyValue::Bool(true));
    assert_eq!(rec["recovery_action"], PyValue::from("NONE"));
    let tail = h.request(SessionRequest::ResultTail {
        cmd_id: id.clone(),
        from_chunk: 0,
        limit: 10,
    });
    assert_eq!(
        tail["chunks"],
        PyValue::List(vec![PyValue::from(clean_text("ls\r\nfoo\r\n"))])
    );
    assert_eq!(tail["status"], PyValue::from("error"));
    assert_eq!(h.dev.take_tx(), b"ls\n".to_vec(), "不含 \\x03／\\x04");
    let s = h.get_state();
    assert_eq!(s["state"], PyValue::from("READY"));
    assert_eq!(s["last_error"], PyValue::Null);
    h.dev.on_tx_reply(b"run\n", b"run\r\n__RC__=0\r\n");
    let next = h.submit_with("run", |r| {
        r.completion_regex = Some("(?m)^__RC__=0\\r*$".into())
    });
    assert_eq!(
        h.wait_done(&next)["status"],
        PyValue::from("done"),
        "之後排隊命令照常執行"
    );
}

#[test]
fn passthrough_timeout_no_rescue_keys_no_downgrade() {
    // #94（Python oracle tests/test_p1oc_sc35_commands.py）：逾時不送救援鍵、不轉 ATTACHED、不 force recover、不 flush。
    let h = passthrough_ready(OTHERS_PASSTHROUGH_PROFILE);
    let id = h.submit_with("hang", |r| r.timeout_s = 1.0);
    let queued = h.submit_with("hang2", |r| r.timeout_s = 1.0);
    wait_tx(&h, b"hang\n");
    advance_until(&h, Duration::from_millis(100), "逾時收尾", |h| {
        is_done(h, &id)
    });
    assert_eq!(
        h.command_get(&id)["error_code"],
        PyValue::from("PROMPT_TIMEOUT")
    );
    assert_ne!(
        h.command_get(&queued)["error_code"],
        PyValue::from("FLUSHED_BY_RECOVERY")
    );
    advance_until(&h, Duration::from_millis(100), "第二筆收尾", |h| {
        is_done(h, &queued)
    });
    let tx = h.dev.take_tx();
    assert!(
        !tx.contains(&0x03) && !tx.contains(&0x04),
        "passthrough 不得送救援鍵：{tx:?}"
    );
    assert_eq!(state_of(&h), "READY");
    assert_eq!(
        h.get_state()["bridge_generation"],
        PyValue::Int(sw_proto::pyjson::PyInt::Small(1)),
        "未 force recover"
    );
}

#[test]
fn passthrough_reboot_is_plain_line() {
    let h = passthrough_ready(OTHERS_PASSTHROUGH_PROFILE);
    let id = h.submit_with("reboot", |r| r.timeout_s = 1.0);
    wait_tx(&h, b"reboot\n");
    let s = h.get_state();
    assert_eq!(
        s["boot_quiet_remaining_s"],
        PyValue::Null,
        "passthrough reboot 不 arm quiet"
    );
    advance_until(&h, Duration::from_millis(100), "逾時收尾", |h| {
        is_done(h, &id)
    });
    let rec = h.command_get(&id);
    assert_eq!(rec["error_code"], PyValue::from("PROMPT_TIMEOUT"));
    assert_eq!(rec["recovery_action"], PyValue::from("NONE"));
    let s = h.get_state();
    assert_eq!(s["state"], PyValue::from("READY"));
    assert_eq!(s["recovering"], PyValue::Bool(false));
}

#[test]
fn passthrough_recover_no_probe() {
    // ATTACHED：REPROBE 不 probe、零 TX、SESSION_NOT_READY＋last_error null。
    let h = ActorRig::new(OTHERS_PASSTHROUGH_PROFILE).attach();
    h.wait_state("ATTACHED");
    h.advance(Duration::from_secs(10));
    let r = h.request(SessionRequest::Recover {
        timeout_s: 2.0,
        force: false,
    });
    assert_eq!(h.dev.write_calls(), 0);
    assert_eq!(
        keys(&r),
        vec![
            "ok",
            "recovering",
            "action",
            "recovered",
            "session",
            "error_code",
            "last_error",
            "last_error_detail"
        ]
    );
    assert_eq!(r["ok"], PyValue::Bool(true));
    assert_eq!(r["recovering"], PyValue::Bool(false));
    assert_eq!(r["action"], PyValue::from("REPROBE"));
    assert_eq!(r["recovered"], PyValue::Bool(false));
    assert_eq!(r["error_code"], PyValue::from("SESSION_NOT_READY"));
    assert_eq!(r["last_error"], PyValue::Null);
    assert_eq!(r["last_error_detail"], PyValue::Null);
    // READY：Python `recover_session` 走 `_recover_after_failure` 的 passthrough 分支 → PROMPT_TIMEOUT／NONE、零 TX。
    h.advance(Duration::from_secs(21));
    h.wait_state("READY");
    let r = h.request(SessionRequest::Recover {
        timeout_s: 2.0,
        force: false,
    });
    assert_eq!(
        r,
        sw_proto::pydict! { "ok" => false, "error_code" => "PROMPT_TIMEOUT", "partial" => true, "recovery_action" => "NONE" }
    );
    assert_eq!(h.dev.write_calls(), 0);
    assert_eq!(state_of(&h), "READY");
}

// ---------------------------------------------------------------------------
// R1-F8：TX 帳本有界
// ---------------------------------------------------------------------------

#[test]
fn tx_ledger_bounded_over_long_session_without_reconnect() {
    let h = ActorRig::new(SHELL_PROFILE).ready();
    h.dev.on_tx_match(
        r"echo [0-9]+$",
        Box::new(|line: &[u8]| {
            // 同一行前可能殘留無換行的 lease TX／救援鍵（FakeDevice 以完整行比對）。
            let text = String::from_utf8_lossy(line).trim().to_string();
            let n = text.rsplit("echo ").next().unwrap_or("").to_string();
            format!("echo {n}\r\n{n}\r\nroot@t:/# ").into_bytes()
        }),
    );
    let generation = h.get_state()["bridge_generation"].clone();
    let max_live = Arc::new(std::sync::atomic::AtomicUsize::new(0));
    let sample = |h: &ActorRig| {
        let live = h.tx_ledger_gauge().live_ops;
        max_live.fetch_max(live, std::sync::atomic::Ordering::Relaxed);
    };
    for i in 0..700 {
        let id = h.submit_ok(&format!("echo {i}"));
        let rec = h.wait_done(&id);
        assert_eq!(rec["status"], PyValue::from("done"), "echo {i}");
        sample(&h);
        if i % 35 == 0 {
            let open = h.request(SessionRequest::InteractiveOpen {
                owner: "agent:ledger".into(),
                timeout_s: 30.0,
                command: String::new(),
                allow_attached: false,
            });
            let iid = open["interactive_id"].as_str().unwrap().to_string();
            let sent = h.request(SessionRequest::InteractiveSend {
                interactive_id: iid.clone(),
                data: "x\n".into(),
                encoding: "plain".into(),
            });
            assert_eq!(sent["ok"], PyValue::Bool(true));
            let closed = h.request(SessionRequest::InteractiveClose {
                interactive_id: iid,
            });
            assert_eq!(closed["ok"], PyValue::Bool(true));
            sample(&h);
        }
        if i % 140 == 0 {
            let st = h.request(SessionRequest::SelfTest {
                timeout_s: 2.0,
                strict_human_lock: false,
            });
            assert_eq!(st["classification"], PyValue::from("OK"));
            sample(&h);
        }
        if i == 350 {
            h.dev.on_tx_reply(b"\x03", b"^C\r\nroot@t:/# ");
            let slow = h.submit_with("sleep 5", |r| r.timeout_s = 1.0);
            wait_tx(&h, b"sleep 5\n");
            advance_until(&h, Duration::from_millis(100), "救援", |h| {
                is_done(h, &slow)
            });
            assert_eq!(
                h.command_get(&slow)["recovery_action"],
                PyValue::from("CTRL_C")
            );
            sample(&h);
        }
    }
    assert_eq!(h.get_state()["bridge_generation"], generation, "全程未重連");
    h.wait_for("帳本收斂", |h| h.tx_ledger_gauge().live_ops == 0);
    let gauge = h.tx_ledger_gauge();
    assert!(
        max_live.load(std::sync::atomic::Ordering::Relaxed) <= 2,
        "live_ops 上限"
    );
    assert!(!gauge.saturated);
    assert_eq!(gauge.settle_dropped, 0);
}

#[test]
fn command_cancel_running_revokes_tx_op() {
    // 審查 F13／#52：`command.cancel` 在途命令 → Python `revoke_tx_op` 撤銷其 permit；gate 外等待中的尾段不得交 OS。
    let h = ActorRig::new(SHELL_PROFILE).ready();
    let _ = h.dev.take_tx();
    let submits = h.dev.os_submits();
    h.dev.script_write(vec![FakeWriteStep::AcceptThenWouldBlock(3)]);
    let id = h.submit_ok("echo hello");
    assert!(h.dev.wait_paused(Duration::from_secs(5)), "命令 TX 未進入 gate 外等待");
    let rep = h.request(SessionRequest::CancelQueued { cmd_id: id.clone() });
    assert_eq!(pget(&rep, "ok"), PyValue::Bool(true));
    h.dev.release_pause();
    let deadline = Instant::now() + Duration::from_secs(5);
    while h.dev.os_submits() == submits {
        assert!(Instant::now() < deadline, "首段未提交");
        std::thread::sleep(Duration::from_millis(5));
    }
    std::thread::sleep(Duration::from_millis(100));
    assert_eq!(h.dev.os_submits(), submits + 1, "撤銷後尾段不得再交 OS");
    assert_eq!(h.dev.take_tx(), b"ech".to_vec());
}

// ---------------------------------------------------------------------------
// 審查 F25（High，8b8a40f）／F27（Medium）：Python `recover_session`／`self_test` 不看 `foreground_busy`——
// READY 且前景命令仍在等 prompt 時，recover 照送 `\x03`／`\x04`、self-test 照送 nonce probe；兩者都不是
// Python 的前景 operation，不設 `foreground_busy`、不擋 agent log RX。
// ---------------------------------------------------------------------------

fn ready_with_hung_command() -> (ActorRig, String) {
    let h = ActorRig::new(SHELL_PROFILE).ready();
    let id = h.submit_ok("sleep 20");
    h.wait_running(&id);
    wait_tx(&h, b"sleep 20\n");
    (h, id)
}

#[test]
fn recover_while_foreground_command_sends_recover_keys() {
    let (h, _id) = ready_with_hung_command();
    h.dev.on_tx_reply(b"\x03", b"^C\r\nroot@t:/# ");
    let r = h
        .request_async(SessionRequest::Recover {
            timeout_s: 2.0,
            force: false,
        })
        .recv_timeout(Duration::from_secs(10))
        .expect("recover 回覆")
        .expect("recover ok");
    assert_ne!(r["error_code"], PyValue::from("SESSION_BUSY"), "{r:?}");
    assert_eq!(r["error_code"], PyValue::from("PROMPT_TIMEOUT_RECOVERED"));
    assert_eq!(r["recovery_action"], PyValue::from("CTRL_C"));
    assert!(wal_tx_has(&h, b"\x03"), "救援鍵應寫出");
}

#[test]
fn self_test_while_foreground_command_sends_probe() {
    let (h, _id) = ready_with_hung_command();
    h.dev.on_tx_match(
        r"__READY__[0-9a-f]+",
        Box::new(|line: &[u8]| {
            let text = String::from_utf8_lossy(line).into_owned();
            let token = text.split("__READY__").nth(1).unwrap_or("").trim().to_string();
            format!("__READY__{token}\r\nroot@t:/# ").into_bytes()
        }),
    );
    let r = h
        .request_async(SessionRequest::SelfTest {
            timeout_s: 2.0,
            strict_human_lock: false,
        })
        .recv_timeout(Duration::from_secs(10))
        .expect("self-test 回覆")
        .expect("self-test ok");
    assert_ne!(r["error_code"], PyValue::from("SESSION_BUSY"), "{r:?}");
    assert_eq!(r["classification"], PyValue::from("OK"));
}

#[test]
fn self_test_probe_in_flight_is_not_foreground_busy() {
    // F27：nonce probe 等待中（裝置不回 nonce）：`foreground_busy` 為 false、`outstanding_commands` 為 0。
    let h = ActorRig::new(SHELL_PROFILE).ready();
    let rx = h.request_async(SessionRequest::SelfTest {
        timeout_s: 2.0,
        strict_human_lock: false,
    });
    h.wait_for("probe 已送出", |h| {
        tx_rows(h)
            .iter()
            .any(|r| String::from_utf8_lossy(&b64(r)).contains("__READY__"))
    });
    let s = h.get_state();
    assert_eq!(s["foreground_busy"], PyValue::Bool(false));
    assert_eq!(s["outstanding_commands"], PyValue::from(0));
    advance_until(&h, Duration::from_millis(200), "self-test 收尾", |_| {
        !rx.is_empty()
    });
}

#[test]
fn outstanding_commands_counts_background_ids() {
    // F27：Python `outstanding = len(background_cmd_ids) + (1 if foreground_busy else 0)`。
    let h = ActorRig::new(SHELL_PROFILE).ready();
    h.dev.on_tx_reply(b"echo bg", b"bg\r\nroot@t:/# ");
    let id = h.submit_with("echo bg", |r| r.mode = "background".into());
    h.wait_done(&id);
    let s = h.get_state();
    assert_eq!(s["foreground_busy"], PyValue::Bool(false));
    assert_eq!(s["outstanding_commands"], PyValue::from(1), "{s:?}");
}


#[test]
fn recover_key_other_reject_in_command_maps_aborted_in_flight() {
    // 審查 F26（Medium，8b8a40f）：命令逾時後送救援鍵，若救援鍵被 Frozen／StaleEpoch／Cancelled／Quiet 以外的原因
    // 拒絕（此處 WAL 不健康），Python `_recover_after_failure` 拋 TxIncompleteError → `execute_command` 以
    // `_map_aborted_result(cmd_id, exc)` 結算：`tx_account(cmd_id)` 已有命令本身的 9 bytes（`sleep 20\n`）→
    // META `{"event":"command_aborted_in_flight","confirmed_tx_bytes":9,"tx_outcome":"partial"}`＋
    // `{ok:false, error_code:"COMMAND_ABORTED_IN_FLIGHT"}`。舊 Rust：SEND_FAILED、無 META。
    let h = ActorRig::new(SHELL_PROFILE).ready();
    let slow = h.submit_with("sleep 20", |r| r.timeout_s = 1.0);
    wait_tx(&h, b"sleep 20\n");
    h.fence.with(|f| f.wal_unhealthy = true);
    advance_until(&h, Duration::from_millis(100), "命令結算", |h| is_done(h, &slow));
    let rec = h.wait_done(&slow);
    assert_eq!(rec["error_code"], PyValue::from("COMMAND_ABORTED_IN_FLIGHT"), "{rec:?}");
    let meta = h
        .wal_meta_events()
        .into_iter()
        .find(|m| m.get("event") == Some(&PyValue::from("command_aborted_in_flight")))
        .expect("應寫 command_aborted_in_flight META");
    assert_eq!(meta.get("confirmed_tx_bytes"), Some(&PyValue::from(9)));
    assert_eq!(meta.get("tx_outcome"), Some(&PyValue::from("partial")));
    assert!(!h.wal_tx_payloads().contains(&b"\x03".to_vec()), "救援鍵被拒，零 TX");
}

#[test]
fn global_background_capture_limit_evicts_oldest_across_sessions() {
    // 審查 F28（Medium，8b8a40f）：Python `SessionManager._background` 為 manager 層全域 dict，`BG_CAPTURE_MAX_COUNT`
    // （64）是整個 daemon 的上限，`_evict_background_locked` 依全域 `created_at` 淘汰最舊的已終結 capture（跨 session）。
    // 輸入：COM1 已有 64 筆已終結 capture（較舊），COM0 再完成 1 筆 background 命令 → 總數 65 → 淘汰 COM1 最舊的一筆。
    let index = sw_core::capture::BackgroundIndex::new();
    let evicted = Arc::new(std::sync::Mutex::new(Vec::<(String, String)>::new()));
    let sink = evicted.clone();
    index.set_remote_evictor(Arc::new(move |com: &str, id: &str| {
        sink.lock().unwrap().push((com.to_string(), id.to_string()));
    }));
    for i in 0..64 {
        let id = format!("old{i:02}");
        assert!(index.register(&id, "COM1", "2000-01-01T00:00:00").is_empty());
        index.set_active(&id, false);
    }
    let h = ActorRig::new_with_bg_index(SHELL_PROFILE, |_| {}, index.clone()).ready();
    h.dev.on_tx_reply(b"dmesg\n", b"dmesg\r\nline1\r\nroot@t:/# ");
    let id = h.submit_with("dmesg", |r| r.mode = "bg".into());
    h.wait_done(&id);
    assert_eq!(
        evicted.lock().unwrap().clone(),
        vec![("COM1".to_string(), "old00".to_string())],
        "應淘汰全域最舊的已終結 capture"
    );
    assert_eq!(index.len(), 64);
}

#[test]
fn evict_capture_event_drops_local_capture() {
    // 審查 F28：被全域淘汰的 capture 由擁有者丟棄，之後 `result_tail` 回 CMD_NOT_FOUND（Python `_background.pop`）。
    let h = ActorRig::new(SHELL_PROFILE).ready();
    h.dev.on_tx_reply(b"dmesg\n", b"dmesg\r\nline1\r\nroot@t:/# ");
    let id = h.submit_with("dmesg", |r| r.mode = "bg".into());
    h.wait_done(&id);
    let before = h.request(SessionRequest::BackgroundResult {
        cmd_id: id.clone(),
        from_chunk: 0,
        limit: 200,
    });
    assert_eq!(before["ok"], PyValue::Bool(true));
    h.send_control(ControlEvent::EvictCapture { cmd_id: id.clone() });
    let after = h.request(SessionRequest::BackgroundResult {
        cmd_id: id.clone(),
        from_chunk: 0,
        limit: 200,
    });
    assert_eq!(after["error_code"], PyValue::from("CMD_NOT_FOUND"));
}

/// sol-backfill p5 #1：`command.submit {mode:"interactive"}` 的命令 TX 須帶原 `cmd_id` 寫 WAL（Python
/// `_send_command_with_permit(..., cmd_id=cmd_id)`）；未完整送出時依 `_map_aborted_result` 結算：已確認 >0 bytes
/// → `COMMAND_ABORTED_IN_FLIGHT`＋META `command_aborted_in_flight`（partial／unknown）。
fn interactive_tx_rows(h: &ActorRig) -> Vec<PyDict> {
    h.wal_rows()
        .into_iter()
        .filter(|r| r.get("dir").and_then(|v| v.as_str()) == Some("TX"))
        .collect()
}

#[test]
fn interactive_mode_command_tx_carries_cmd_id() {
    let h = ActorRig::new(SHELL_PROFILE).ready();
    let before = interactive_tx_rows(&h).len();
    let id = h.submit_with("echo x", |r| r.mode = "interactive".into());
    let rec = h.wait_done(&id);
    assert_eq!(rec["status"], PyValue::from("interactive"), "{rec:?}");
    let rows = interactive_tx_rows(&h);
    let new_rows = &rows[before..];
    assert!(!new_rows.is_empty(), "interactive 命令應寫 TX");
    for row in new_rows {
        assert_eq!(row.get("cmd_id"), Some(&PyValue::from(id.as_str())), "{row:?}");
    }
}

#[test]
fn interactive_mode_command_partial_send_maps_aborted_in_flight() {
    let h = ActorRig::new(SHELL_PROFILE).ready();
    h.dev.script_write(vec![FakeWriteStep::AcceptThenError(
        4,
        PalError::io(5, "[Errno 5] Input/output error"),
    )]);
    let id = h.submit_with("echo x", |r| r.mode = "interactive".into());
    let rec = h.wait_done(&id);
    assert_eq!(rec["error_code"], PyValue::from("COMMAND_ABORTED_IN_FLIGHT"), "{rec:?}");
    let meta = h
        .wal_meta_events()
        .into_iter()
        .find(|m| m.get("event") == Some(&PyValue::from("command_aborted_in_flight")))
        .expect("應寫 command_aborted_in_flight META");
    assert_eq!(meta.get("confirmed_tx_bytes"), Some(&PyValue::from(4)));
    assert_eq!(meta.get("tx_outcome"), Some(&PyValue::from("partial")));
}

#[test]
fn interactive_mode_command_unknown_send_maps_aborted_in_flight_unknown() {
    let h = ActorRig::new(SHELL_PROFILE).ready();
    h.dev.script_write(vec![FakeWriteStep::Unknown {
        confirmed: 2,
        error: PalError::io(5, "[Errno 5] Input/output error"),
    }]);
    let id = h.submit_with("echo x", |r| r.mode = "interactive".into());
    let rec = h.wait_done(&id);
    assert_eq!(rec["error_code"], PyValue::from("COMMAND_ABORTED_IN_FLIGHT"), "{rec:?}");
    let meta = h
        .wal_meta_events()
        .into_iter()
        .find(|m| m.get("event") == Some(&PyValue::from("command_aborted_in_flight")))
        .expect("應寫 command_aborted_in_flight META");
    assert_eq!(meta.get("tx_outcome"), Some(&PyValue::from("unknown")));
}

// ---------------------------------------------------------------------------
// PR 237 承接（修正後語意，origin/fix/wrapped-echo-false-positive 34c3a84b）：長命令回顯被終端折行拆開時的剝除。
// 只接受「命令原文＋折行插入字元」：命令每個字元（含空白）依序出現，只有換行或多出的一個空白可跳過，
// 插入點彼此至少相隔 20 字元（第一個插入點以第一行欄位計、含殘留 prompt），第一個命令字元前不得插入，
// 回顯須從第一行開始並結束在行尾。期望值與 Python 修正版 tests/test_wrapped_echo_strip.py 相同。
// ---------------------------------------------------------------------------

const WRAP_PROMPT_RE: &str = "(?m)^root@prplOS:.*# ";
const WRAP_PROMPT: &str = "root@prplOS:~# ";

fn wrap_cmd96() -> String {
    format!("echo {} {}", "x".repeat(50), "y".repeat(40))
}

fn wrap_cmd200() -> String {
    format!("echo {} {} {}", "a".repeat(60), "b".repeat(60), "c".repeat(73))
}

fn wrap_extract(raw: &str, command: &str) -> String {
    let prompt = compile(WRAP_PROMPT_RE, PyFlags(0)).expect("prompt regex");
    extract_command_stdout(raw, command, &prompt, None)
}

#[test]
fn wrapped_echo_busybox_wrap_is_stripped() {
    let c = wrap_cmd96();
    let raw = format!("{}\r\r\n{}\r\nOUT\r\n{WRAP_PROMPT}", &c[..65], &c[65..]);
    assert_eq!(wrap_extract(&raw, &c), "OUT");
}

#[test]
fn wrapped_echo_three_line_wrap_is_stripped() {
    let c = wrap_cmd200();
    let raw = format!("{}\r\r\n{}\r\r\n{}\r\nOUT\r\n{WRAP_PROMPT}", &c[..65], &c[65..145], &c[145..]);
    assert_eq!(wrap_extract(&raw, &c), "OUT");
}

#[test]
fn wrapped_echo_readline_space_wrap_is_stripped() {
    let c = wrap_cmd96();
    let raw = format!("{} \r{}\r\nOUT\r\n{WRAP_PROMPT}", &c[..79], &c[79..]);
    assert_eq!(wrap_extract(&raw, &c), "OUT");
}

#[test]
fn wrapped_echo_prompt_prefix_on_first_echo_line() {
    let c = wrap_cmd96();
    let raw = format!("{WRAP_PROMPT}{}\r\r\n{}\r\nOUT\r\n{WRAP_PROMPT}", &c[..65], &c[65..]);
    assert_eq!(wrap_extract(&raw, &c), "OUT");
    // readline 折行＋prompt 前綴
    let raw = format!("{WRAP_PROMPT}{} \r{}\r\nOUT\r\n{WRAP_PROMPT}", &c[..64], &c[64..]);
    assert_eq!(wrap_extract(&raw, &c), "OUT");
}

#[test]
fn wrapped_echo_short_command_and_no_echo_unchanged() {
    let raw = format!("cut -d. -f1 /proc/uptime\r\n65\r\n{WRAP_PROMPT}");
    assert_eq!(wrap_extract(&raw, "cut -d. -f1 /proc/uptime"), "65");
    let raw = format!("line1\r\nline2\r\n{WRAP_PROMPT}");
    assert_eq!(wrap_extract(&raw, &wrap_cmd96()), "line1\nline2");
}

#[test]
fn wrapped_echo_not_starting_on_first_line_is_kept() {
    let c = wrap_cmd96();
    let raw = format!("header\r\n{}\r\n{}\r\n{WRAP_PROMPT}", &c[..65], &c[65..]);
    assert_eq!(wrap_extract(&raw, &c), format!("header\n{}\n{}", &c[..65], &c[65..]));
}

#[test]
fn wrapped_echo_single_line_echo_not_stripped_twice() {
    let c = wrap_cmd96();
    let raw = format!("{c}\r\n{}\r\n{}\r\n{WRAP_PROMPT}", &c[..65], &c[65..]);
    assert_eq!(wrap_extract(&raw, &c), format!("{}\n{}", &c[..65], &c[65..]));
}

#[test]
fn wrapped_echo_output_equal_to_command_without_spaces_is_kept() {
    // echo 關閉時 `cat file` 印出 `catfile`：不得被當成回顯。
    let raw = format!("catfile\r\n{WRAP_PROMPT}");
    assert_eq!(wrap_extract(&raw, "cat file"), "catfile");
}

#[test]
fn wrapped_echo_short_output_split_like_command_is_kept() {
    let raw = format!("ls -la\r\n/tmp\r\n{WRAP_PROMPT}");
    assert_eq!(wrap_extract(&raw, "ls -la /tmp"), "ls -la\n/tmp");
}

#[test]
fn wrapped_echo_long_output_missing_command_space_is_kept() {
    let squeezed = wrap_cmd96().replace(' ', "");
    let raw = format!("{}\r\n{}\r\n{WRAP_PROMPT}", &squeezed[..60], &squeezed[60..]);
    assert_eq!(
        wrap_extract(&raw, &wrap_cmd96()),
        format!("{}\n{}", &squeezed[..60], &squeezed[60..])
    );
}

#[test]
fn wrapped_echo_insertion_before_first_char_or_too_close_is_kept() {
    let c = wrap_cmd96();
    // 插入點距離不足 20 字元（第二段只有 10 字元就再折行）→ 不是終端折行。
    let raw = format!("{}\r\n{}\r\n{}\r\n{WRAP_PROMPT}", &c[..30], &c[30..40], &c[40..]);
    assert_eq!(wrap_extract(&raw, &c), format!("{}\n{}\n{}", &c[..30], &c[30..40], &c[40..]));
    // 回顯未結束在行尾（命令後同行還有其他字）→ 不剝。
    let raw = format!("{}\r\r\n{}TAIL\r\n{WRAP_PROMPT}", &c[..65], &c[65..]);
    assert_eq!(wrap_extract(&raw, &c), format!("{}\n{}TAIL", &c[..65], &c[65..]));
}

/// 以 readline 折行標記 `" \r"` 於 `cuts` 處切開 `command`（Python `" \r".join(parts)`）。
fn readline_wrapped(command: &str, cuts: &[usize]) -> String {
    let mut bounds = vec![0];
    bounds.extend_from_slice(cuts);
    bounds.push(command.len());
    bounds
        .windows(2)
        .map(|w| &command[w[0]..w[1]])
        .collect::<Vec<_>>()
        .join(" \r")
}

#[test]
fn wrapped_echo_plain_crlf_stdout_spelling_command_is_kept() {
    // sol P5-70f6ce7-1：echo 關閉時 `cat` 的檔案內容恰為命令兩段＋OUT；原始串流沒有 `\r\r\n`／`" \r"`
    // 折行證據，Python 基線全文保留（不得當成折行回顯刪掉有效 stdout）。
    let cmd = format!("cat /tmp/{}", "x".repeat(60));
    let raw = format!("{}\r\n{}\r\nOUT\r\n{WRAP_PROMPT}", &cmd[..40], &cmd[40..]);
    assert_eq!(wrap_extract(&raw, &cmd), format!("{}\n{}\nOUT", &cmd[..40], &cmd[40..]));
    // 普通換行切在命令空白處也一樣（Python `test_normal_newline_output_is_not_treated_as_terminal_wrap`）。
    let raw = format!("echo x \r\ny\r\n{WRAP_PROMPT}");
    assert_eq!(wrap_extract(&raw, "echo x y"), "echo x \ny");
}

#[test]
fn wrapped_echo_readline_wrap_before_command_space_is_stripped() {
    // sol P5-5cc4623-2（Python 842c014）：readline 折行點緊貼命令原有空白時，標記須優先於命令字元比對。
    let command = format!("echo {} {}", "x".repeat(74), "y".repeat(20));
    for prefix in ["\n", "\r\n"] {
        let raw = format!("{prefix}{}\r\nOUT\r\n{WRAP_PROMPT}", readline_wrapped(&command, &[30, 79]));
        assert_eq!(wrap_extract(&raw, &command), "OUT", "prefix={prefix:?}");
    }
}

#[test]
fn wrapped_echo_readline_python_golden_cases() {
    // Python `tests/test_wrapped_echo_strip.py`（主線 842c014）其餘 readline 案例逐案對照。
    let c200 = wrap_cmd200();
    for cuts in [&[79usize, 159][..], &[50, 100, 150][..]] {
        let raw = format!("{}\r\nOUT\r\n{WRAP_PROMPT}", readline_wrapped(&c200, cuts));
        assert_eq!(wrap_extract(&raw, &c200), "OUT", "cuts={cuts:?}");
    }
    for prefix in ["\n", "\r\n"] {
        for cuts in [&[79usize][..], &[79, 159][..]] {
            let raw = format!("{prefix}{}\r\nOUT\r\n{WRAP_PROMPT}", readline_wrapped(&c200, cuts));
            assert_eq!(wrap_extract(&raw, &c200), "OUT", "prefix={prefix:?} cuts={cuts:?}");
        }
    }
    // 標記距命令起點不足 20 字元（且首行無殘留 prompt）：不接受。
    let x60 = "x".repeat(60);
    let raw = format!("{}\r\nOUT\r\n{WRAP_PROMPT}", readline_wrapped(&x60, &[10]));
    assert_eq!(wrap_extract(&raw, &x60), format!("{} {}\nOUT", &x60[..10], &x60[10..]));
    // 原 842c014 主線「殘留 prompt 不計入距離」案依 rulings/p5.md P5-70f6ce7-1 改依修正 PR 語意
    // （第一個插入點以首行欄位計、含殘留 prompt）：見 `wrapped_echo_first_insertion_counts_first_line_column`。
}

/// rulings/p5.md P5-70f6ce7-1（修正 PR 34c3a84 語意）：第一個插入點以首行欄位計（含殘留 prompt）——
/// prompt 15 欄＋命令 10 字元＝第 25 欄折行，視為終端折行回顯而剝除。
#[test]
fn wrapped_echo_first_insertion_counts_first_line_column() {
    let x60 = "x".repeat(60);
    let raw = format!("{WRAP_PROMPT}{}\r\nOUT\r\n{WRAP_PROMPT}", readline_wrapped(&x60, &[10]));
    assert_eq!(wrap_extract(&raw, &x60), "OUT");
    let c = wrap_cmd96();
    let raw = format!("{WRAP_PROMPT}{}\r\r\n{}\r\nOUT\r\n{WRAP_PROMPT}", &c[..10], &c[10..]);
    assert_eq!(wrap_extract(&raw, &c), "OUT");
}

/// rulings/p5.md P5-70f6ce7-1：即使原始串流有 BusyBox `\r\r\n` 折行證據，插入點彼此（第一個以首行欄位計）
/// 不足 20 字元者不是終端折行，全文保留（842c014 主線只接合行、不檢查間距，會誤剝）。
#[test]
fn wrapped_echo_busybox_insertions_closer_than_20_are_kept() {
    let c = wrap_cmd96();
    let raw = format!("{}\r\r\n{}\r\nOUT\r\n{WRAP_PROMPT}", &c[..10], &c[10..]);
    assert_eq!(wrap_extract(&raw, &c), format!("{}\n{}\nOUT", &c[..10], &c[10..]));
    let raw = format!("{}\r\r\n{}\r\r\n{}\r\nOUT\r\n{WRAP_PROMPT}", &c[..30], &c[30..40], &c[40..]);
    assert_eq!(
        wrap_extract(&raw, &c),
        format!("{}\n{}\n{}\nOUT", &c[..30], &c[30..40], &c[40..])
    );
}

/// rulings/p5.md P5-70f6ce7-1：BusyBox 折行只插入換行；命令本身的空白不能少（換行取代命令空白者不是回顯）。
#[test]
fn wrapped_echo_busybox_newline_replacing_command_space_is_kept() {
    let c = wrap_cmd96();
    // 第 55 字元為命令原有空白：以 `\r\r\n` 取代它（而非插入）→ 少一個命令字元，不得剝除。
    assert_eq!(&c[55..56], " ");
    let raw = format!("{}\r\r\n{}\r\nOUT\r\n{WRAP_PROMPT}", &c[..55], &c[56..]);
    assert_eq!(wrap_extract(&raw, &c), format!("{}\n{}\nOUT", &c[..55], &c[56..]));
}

// ---------------------------------------------------------------------------
// WP5-2 第 5 項：`expect_reboot`／`trusted_human` 經佇列送進 `OpSpec::Command`，且不繞過 gate
// ---------------------------------------------------------------------------

/// Python `tests/test_expect_reboot.py` 的 `CHAINED`。
const WP52_CHAINED: &str = "firstboot -y && reboot";

/// 出列的 `Effect::StartOp { spec: OpSpec::Command(..) }` 保留兩個提交旗標，`is_trusted_human()` 為明示欄位與
/// `HumanConsole` 來源取 OR（ITEM 1；Python `arbiter.py:411-416` 只在旗標成立時把 `expect_reboot`／`trusted_human`
/// 交給 `execute_command`）。只驗資料抵達執行入口；reboot 分支與 G7b 消費端屬後續工作包。
#[test]
fn wp52_start_op_command_keeps_submit_flags() {
    use sw_core::arbiter::SubmitOrigin;
    use sw_core::op_runner::OpSpec;

    let console = || SubmitOrigin::HumanConsole {
        client_id: "c1".into(),
    };
    for (expect_reboot, trusted_human, origin, source, trusted) in [
        (false, false, SubmitOrigin::Rpc, "agent", false),
        (true, false, SubmitOrigin::Rpc, "agent", false),
        (false, true, SubmitOrigin::Rpc, "human:cli", true),
        (true, true, SubmitOrigin::Rpc, "human:cli", true),
        (true, false, console(), "human:c1", true),
        (false, true, console(), "human:c1", true),
    ] {
        let case = format!(
            "expect_reboot={expect_reboot} trusted_human={trusted_human} origin={origin:?}"
        );
        let mut h = CoreHarness::new(SHELL_PROFILE);
        h.to_ready();
        let mut req = agent_submit("echo wp52");
        req.source = source.into();
        req.expect_reboot = expect_reboot;
        req.trusted_human = trusted_human;
        req.origin = origin.clone();
        let (resp, effects) = h.rpc_full(SessionRequest::Submit(req));
        let resp = resp.expect("submit");
        assert_eq!(resp["ok"], PyValue::Bool(true), "{case}：{resp:?}");
        assert_eq!(
            resp.get("expect_reboot").cloned(),
            expect_reboot.then_some(PyValue::Bool(true)),
            "{case}：回聲"
        );
        let cmd_id = resp["cmd_id"].as_str().expect("cmd_id").to_string();
        let started: Vec<_> = effects
            .iter()
            .filter_map(|effect| match effect {
                Effect::StartOp {
                    spec: OpSpec::Command(queued),
                    ..
                } => Some(queued),
                _ => None,
            })
            .collect();
        assert_eq!(started.len(), 1, "{case}：READY 空佇列應立即出列一次");
        let queued = started[0];
        assert_eq!(queued.cmd_id, cmd_id, "{case}");
        assert_eq!(queued.req.command, "echo wp52", "{case}");
        assert_eq!(queued.req.expect_reboot, expect_reboot, "{case}");
        assert_eq!(queued.req.trusted_human, trusted_human, "{case}");
        assert_eq!(queued.req.origin, origin, "{case}");
        assert_eq!(
            queued.req.is_trusted_human(),
            trusted,
            "{case}：可信來源 OR 判定"
        );
        assert_eq!(
            h.reg.get(&cmd_id).expect("registry").get("expect_reboot"),
            Some(&PyValue::Bool(expect_reboot)),
            "{case}：record 旗標"
        );
    }
}

/// 對照 `tests/test_expect_reboot.py::test_flag_does_not_bypass_autoboot_quiet_gate`：命令已入列（hold 佔住前景）
/// 後 banner 才武裝 quiet；agent＋true 的鏈式命令出列時仍被 execute-time gate 擋為 AUTOBOOT_QUIET，零 TX、
/// record 保留 true；提交時 gate 對帶旗標與不帶旗標的請求回應相同（同 `execute_time_gate_has_retry_after_sc03`）。
#[test]
fn wp52_flag_does_not_bypass_gates() {
    let h = ActorRig::new(SHELL_PROFILE).ready();
    let hold = h.submit_with("sleep 60", |r| r.timeout_s = 120.0);
    h.wait_running(&hold);
    let queued = h.submit_with(WP52_CHAINED, |r| {
        r.source = "agent:t".into();
        r.expect_reboot = true;
    });
    h.inject_rx(BANNER);
    // hold 以 prompt 正常完成（只清 TX 靜默維度、reconfirm pending 保留，#162），queued 出列時仍被 gate 擋下。
    h.inject_rx(b"root@t:/# ");
    let _ = h.wait_done(&hold);
    let done = h.wait_done(&queued);
    // Python `arbiter.py:465-467`：結果 ok:false → status "error"＋error_code。
    assert_eq!(done["status"], PyValue::from("error"), "{done:?}");
    assert_eq!(
        done["error_code"],
        PyValue::from("AUTOBOOT_QUIET"),
        "{done:?}"
    );
    assert_eq!(done["expect_reboot"], PyValue::Bool(true));
    let tx = String::from_utf8_lossy(&h.dev.take_tx()).into_owned();
    assert!(
        !tx.contains(WP52_CHAINED),
        "被 gate 擋下的旗標命令不得寫出 TX：{tx:?}"
    );
    assert!(!wal_tx_has(&h, WP52_CHAINED.as_bytes()));

    let flagged = SessionRequest::Submit({
        let mut req = agent_submit(WP52_CHAINED);
        req.expect_reboot = true;
        req
    });
    let direct = h.request(flagged);
    let plain = h.request(SessionRequest::Submit(agent_submit(WP52_CHAINED)));
    assert_eq!(pget(&direct, "error_code"), PyValue::from("AUTOBOOT_QUIET"));
    assert_eq!(direct, plain, "旗標不改變提交時 gate 的回應");
    assert!(direct.get("cmd_id").is_none());
    assert!(h.dev.take_tx().is_empty(), "提交時拒絕零 TX");
}

// ---------------------------------------------------------------------------
// WP5-5 第 1 項：execute-time 可信來源判定（G7a、OD-74、rulings s2 D5）
// ---------------------------------------------------------------------------

/// 效果中是否含指定命令的 `Effect::StartOp { spec: OpSpec::Command(..) }`（命令實際進入執行）。
fn wp55_started(effects: &[Effect], cmd_id: &str) -> bool {
    use sw_core::op_runner::OpSpec;
    effects.iter().any(|effect| {
        matches!(
            effect,
            Effect::StartOp { spec: OpSpec::Command(queued), .. } if queued.cmd_id == cmd_id
        )
    })
}

/// 出列決策只看 `SubmitReq::is_trusted_human()`（`trusted_human` 旗標 OR `HumanConsole` 來源），永不看 `source` 字首。
///
/// 對照 Python `origin/main:sw_core/session_manager.py:6549`：`human = source.startswith("human:") if trusted_human is
/// None else trusted_human`，RPC 路徑一律帶明確布林（`arbiter.py:411-416` 只在旗標成立時傳 `trusted_human=True`，
/// 否則 `service.py` 的 `_send_cb` 帶 False），故 RPC 的 `human:` 字首不授權豁免；`:6573-6588` 的 quiet gate 與
/// lease 判斷共用同一個 `human`。
///
/// (A) quiet：前景佔住時命令入列，banner 才武裝 quiet，前景以正式 `OpFinished` 結束後待測命令出列——
///     可信者取得 `StartOp`；非可信者被 execute-time gate 以 `AUTOBOOT_QUIET` 終結且無 `StartOp`。
/// (B) quiet 關閉、已有 human lease：可信者回 `SESSION_INTERACTIVE_BUSY`；非可信者沿用 agent 判斷（Python
///     `suspend_human_interactive`），照常出列。
#[test]
fn wp55_execute_time_trust_matrix() {
    use sw_core::arbiter::SubmitOrigin;

    let console = || SubmitOrigin::HumanConsole {
        client_id: "c1".into(),
    };
    // (source, trusted_human, origin, 預期可信)
    let matrix = [
        // OD-74：RPC 依原始 source 判定的可信 human（`trusted_human=true`）。
        ("human:cli", true, SubmitOrigin::Rpc, true),
        // G7a：明寫 `human:` 字首、非可信的 RPC 一律以 agent 對待。
        ("human:cli", false, SubmitOrigin::Rpc, false),
        ("agent:x", false, SubmitOrigin::Rpc, false),
        // D5：HumanConsole 來源即使旗標為 false 仍可信（OR 語意）。
        ("human:c1", false, console(), true),
    ];

    for (source, trusted_human, origin, trusted) in matrix.clone() {
        let case =
            format!("(A) quiet source={source} trusted_human={trusted_human} origin={origin:?}");
        let mut h = CoreHarness::new(SHELL_PROFILE);
        h.to_ready();
        let hold = h.start_foreground_command("sleep 9");
        let mut req = agent_submit("echo wp55");
        req.source = source.into();
        req.trusted_human = trusted_human;
        req.origin = origin.clone();
        let (resp, effects) = h.rpc_full(SessionRequest::Submit(req));
        let resp = resp.expect("submit");
        assert_eq!(
            resp["ok"],
            PyValue::Bool(true),
            "{case}：quiet 前提交應通過：{resp:?}"
        );
        let cmd_id = resp["cmd_id"].as_str().expect("cmd_id").to_string();
        assert!(
            !wp55_started(&effects, &cmd_id),
            "{case}：前景佔用中不得出列"
        );
        assert_eq!(
            h.reg.get(&cmd_id).expect("registry")["status"],
            PyValue::from("accepted"),
            "{case}：命令應已入列"
        );

        h.rx(BANNER);
        assert!(
            h.fence.with(|f| f.quiet_active),
            "{case}：banner 應於命令入列後武裝 quiet"
        );
        assert_ne!(
            pget(&h.public(), "boot_quiet_remaining_s"),
            PyValue::Null,
            "{case}"
        );

        let epoch = h.epoch();
        let effects = h.feed(ControlEvent::OpFinished {
            op_id: hold.op_id,
            epoch,
            outcome: OpOutcome::CommandResult(sw_proto::pydict! {
                "ok" => true, "stdout" => "", "partial" => false,
            }),
            after_rx_seq: None,
        });
        assert_eq!(
            h.reg.get(&hold.cmd_id).expect("hold")["status"],
            PyValue::from("done"),
            "{case}：前景以正式 OpFinished 結束"
        );
        let rec = h.reg.get(&cmd_id).expect("registry");
        if trusted {
            assert!(
                wp55_started(&effects, &cmd_id),
                "{case}：可信 human 出列時應略過 quiet gate 並取得 StartOp：{rec:?}"
            );
            assert_eq!(rec["status"], PyValue::from("running"), "{case}：{rec:?}");
            assert_eq!(
                rec.get("error_code"),
                Some(&PyValue::Null),
                "{case}：{rec:?}"
            );
            assert_eq!(rec.get("done_at"), Some(&PyValue::Null), "{case}：{rec:?}");
        } else {
            assert!(
                !wp55_started(&effects, &cmd_id),
                "{case}：非可信命令不得取得 StartOp"
            );
            // Python `arbiter.py:465-467`：ok:false → status "error"＋error_code；`retry_after_s` 不在
            // `arbiter.py:455-462` 的複製清單，command.get 不含（同 `execute_time_gate_has_retry_after_sc03`）。
            assert_eq!(rec["status"], PyValue::from("error"), "{case}：{rec:?}");
            assert_eq!(
                rec["error_code"],
                PyValue::from("AUTOBOOT_QUIET"),
                "{case}：{rec:?}"
            );
            assert!(rec.get("retry_after_s").is_none(), "{case}：{rec:?}");
            // 同 source 直接提交：gate 回應本身帶 `retry_after_s`（SC-03），提交時拒絕不產生命令。
            let mut again = agent_submit("echo wp55");
            again.source = source.into();
            again.trusted_human = trusted_human;
            again.origin = origin.clone();
            let (direct, effects) = h.rpc_full(SessionRequest::Submit(again));
            let direct = direct.expect("submit");
            assert_eq!(
                pget(&direct, "error_code"),
                PyValue::from("AUTOBOOT_QUIET"),
                "{case}：{direct:?}"
            );
            assert!(
                matches!(pget(&direct, "retry_after_s"), PyValue::Float(_)),
                "{case}：{direct:?}"
            );
            assert!(direct.get("cmd_id").is_none(), "{case}");
            assert!(
                !effects.iter().any(|e| matches!(e, Effect::StartOp { .. })),
                "{case}：提交時拒絕不得出列"
            );
        }
    }

    for (source, trusted_human, origin, trusted) in matrix {
        let case = format!(
            "(B) human lease source={source} trusted_human={trusted_human} origin={origin:?}"
        );
        let mut h = CoreHarness::new(SHELL_PROFILE);
        h.to_ready();
        let open = h
            .rpc(SessionRequest::InteractiveOpen {
                owner: "human:operator".into(),
                timeout_s: 60.0,
                command: String::new(),
                allow_attached: false,
            })
            .expect("RPC");
        assert_eq!(pget(&open, "ok"), PyValue::Bool(true), "{case}：{open:?}");
        let iid = pget(&open, "interactive_id");
        assert!(!h.fence.with(|f| f.quiet_active), "{case}：quiet 關閉");
        let mut req = agent_submit("echo wp55");
        req.source = source.into();
        req.trusted_human = trusted_human;
        req.origin = origin.clone();
        let (resp, effects) = h.rpc_full(SessionRequest::Submit(req));
        let resp = resp.expect("submit");
        assert_eq!(resp["ok"], PyValue::Bool(true), "{case}：{resp:?}");
        let cmd_id = resp["cmd_id"].as_str().expect("cmd_id").to_string();
        let rec = h.reg.get(&cmd_id).expect("registry");
        if trusted {
            // Python `session_manager.py:6584-6588`：human 且 lease 存在 → SESSION_INTERACTIVE_BUSY，
            // `interactive_session_id` 在 `arbiter.py:455-462` 複製清單內。
            assert!(
                !wp55_started(&effects, &cmd_id),
                "{case}：可信 human 遇 human lease 不得出列執行"
            );
            assert_eq!(rec["status"], PyValue::from("error"), "{case}：{rec:?}");
            assert_eq!(
                rec["error_code"],
                PyValue::from("SESSION_INTERACTIVE_BUSY"),
                "{case}：{rec:?}"
            );
            assert_eq!(rec["interactive_session_id"], iid, "{case}：{rec:?}");
        } else {
            // Python `:6582-6583`：非 human 遇 human lease → suspend_human_interactive，照常執行。
            assert!(
                wp55_started(&effects, &cmd_id),
                "{case}：非可信命令沿用 agent 判斷照常出列：{rec:?}"
            );
            assert_eq!(rec["status"], PyValue::from("running"), "{case}：{rec:?}");
        }
    }
}

// ---------------------------------------------------------------------------
// WP5-5 第 2 項：submit-time 可信來源判定（G7b、OD-74、rulings s2 D5）
// ---------------------------------------------------------------------------

/// submit-time gate 的三種武裝狀態（Python `service._resolve_session_id` 的三個拒絕分支）。
#[derive(Clone, Copy, Debug)]
enum Wp55Gate {
    /// banner 武裝 boot quiet（`boot_quiet_remaining_s` 非 None）。
    Quiet,
    /// prompt 清掉 quiet 但 READY 尚未經 nonce probe 再確認（`ready_reconfirm_pending`）。
    ReconfirmPending,
    /// 逾 `READY_RECONFIRM_MAX_S` 落有界化終態（`ready_reconfirm_failed`）。
    ReconfirmFailed,
}

/// 以前景命令佔住 worker 後建立指定 gate 狀態；回傳佔用命令的 cmd_id。
fn wp55_arm_gate(h: &mut CoreHarness, gate: Wp55Gate) -> String {
    h.to_ready();
    let hold = h.start_foreground_command("sleep 9");
    h.rx(BANNER);
    assert!(
        h.fence.with(|f| f.quiet_active),
        "{gate:?}：banner 應武裝 quiet"
    );
    match gate {
        Wp55Gate::Quiet => {}
        Wp55Gate::ReconfirmPending => {
            h.rx(b"\r\nroot@t:~# ");
        }
        Wp55Gate::ReconfirmFailed => {
            // 推到逾時前 1 秒（timer／Tick 照常投遞），最後 1 秒只推時鐘、不投 Tick：終態翻轉必定發生在
            // submit 的 `expire_reconfirm`（Python `get_session_state` 於 gate 前先 `expire_ready_reconfirm`）。
            h.advance(Duration::from_secs(299));
            h.clock.advance(Duration::from_secs(1));
        }
    }
    let p = h.public();
    assert_eq!(p["state"], PyValue::from("READY"), "{gate:?}：{p:?}");
    assert_eq!(
        p["boot_quiet_remaining_s"] != PyValue::Null,
        matches!(gate, Wp55Gate::Quiet),
        "{gate:?}：{p:?}"
    );
    // 三態武裝當下皆為 pending、尚未落終態（ReconfirmFailed 的翻轉留給 submit，由呼叫端於提交後斷言）。
    assert_eq!(
        p["ready_reconfirm_pending"],
        PyValue::Bool(true),
        "{gate:?}：{p:?}"
    );
    assert_eq!(
        p["ready_reconfirm_failed"],
        PyValue::Bool(false),
        "{gate:?}：{p:?}"
    );
    assert_eq!(
        h.reg.get(&hold.cmd_id).expect("hold")["status"],
        PyValue::from("running"),
        "{gate:?}：佔用命令仍在前景執行"
    );
    hold.cmd_id
}

/// 提交時的 quiet／reconfirm gate 只對非可信者生效：`SubmitReq::is_trusted_human()`（`trusted_human` 旗標 OR
/// `HumanConsole` 來源），永不看 `source` 字首，也不改 `quiet_gate::submit_gate_reject` 的輸出契約。
///
/// 對照 Python `origin/main:sw_core/service.py:1234-1250`（`trusted_human = source_check.is_bare_human` 傳入
/// `_resolve_session_id`）與 `:800-839`（`if not trusted_human and platform != "passthrough"` 才進三個拒絕分支）；
/// `tests/test_stage0_gate_codes.py:123-155`：bare `human` 於 reconfirm pending 時 `ok` 為 True，明寫
/// `human:cli`／`human:spoof` 回 `AUTOBOOT_QUIET`。
///
/// (A) 三種 gate 狀態 × 四種來源：可信者 admission 成功、取得 cmd_id 並以 `accepted` 入列（前景佔用中，不宣稱 TX
///     已放行）；非可信者回應逐鍵等於 Python 分支字面量，且沒有新命令紀錄、沒有入列、沒有 `StartOp`。
/// (B) 既有前置檢查（非 READY、ATTACHED、RECOVERING、FLASHING）不因可信標記而繞過：可信與非可信回應完全相同。
#[test]
fn wp55_submit_time_trust_matrix() {
    use sw_core::arbiter::SubmitOrigin;
    use sw_core::quiet_gate::READY_UNCONFIRMED_HINT;
    use sw_core::session_core::SessionState;

    let console = || SubmitOrigin::HumanConsole {
        client_id: "c1".into(),
    };
    // (source, trusted_human, origin, 預期可信)
    let matrix = [
        // OD-74：RPC 原始值 bare `human` 經 `check_source` 正規化為 `human:cli` 並帶 `trusted_human=true`。
        ("human:cli", true, SubmitOrigin::Rpc, true),
        // 明寫 `human:cli`（含 DF-02 的 `human:` 偽造字首）非可信，一律以 agent 對待。
        ("human:cli", false, SubmitOrigin::Rpc, false),
        ("agent:x", false, SubmitOrigin::Rpc, false),
        // D5：HumanConsole 來源即使旗標為 false 仍可信（OR 語意）。
        ("human:c1", false, console(), true),
    ];
    let submit = |source: &str, trusted_human: bool, origin: &SubmitOrigin| {
        let mut req = agent_submit("echo wp55");
        req.source = source.into();
        req.trusted_human = trusted_human;
        req.origin = origin.clone();
        SessionRequest::Submit(req)
    };

    for gate in [
        Wp55Gate::Quiet,
        Wp55Gate::ReconfirmPending,
        Wp55Gate::ReconfirmFailed,
    ] {
        for (source, trusted_human, origin, trusted) in matrix.clone() {
            let case = format!(
                "(A) {gate:?} source={source} trusted_human={trusted_human} origin={origin:?}"
            );
            let mut h = CoreHarness::new(SHELL_PROFILE);
            let hold = wp55_arm_gate(&mut h, gate);
            let records_before = h.reg.len();
            let pending_before = h.reg.pending(h.core.session_id());
            let (resp, effects) = h.rpc_full(submit(source, trusted_human, &origin));
            let resp = resp.expect("submit");
            // 拒絕回應的 `session` 為提交當下（含 `expire_reconfirm` 翻轉後）的 public session 原樣。
            let public = h.public();
            assert!(
                !effects.iter().any(|e| matches!(e, Effect::StartOp { .. })),
                "{case}：前景佔用中不得出列"
            );
            if trusted {
                assert_eq!(resp["ok"], PyValue::Bool(true), "{case}：{resp:?}");
                let cmd_id = resp["cmd_id"].as_str().expect("cmd_id").to_string();
                assert_ne!(cmd_id, hold, "{case}");
                assert_eq!(h.reg.len(), records_before + 1, "{case}：應新增命令紀錄");
                assert_eq!(
                    h.reg.pending(h.core.session_id()),
                    pending_before + 1,
                    "{case}：應入列"
                );
                let rec = h.reg.get(&cmd_id).expect("registry");
                assert_eq!(rec["status"], PyValue::from("accepted"), "{case}：{rec:?}");
                assert_eq!(rec["source"], PyValue::from(source), "{case}：{rec:?}");
            } else {
                let (code, retry, hint) = match gate {
                    Wp55Gate::Quiet => (
                        "AUTOBOOT_QUIET",
                        Some(pget(&public, "boot_quiet_remaining_s")),
                        "boot quiet window 進行中（疑似板卡自發重開機）；等 session 重新確認 READY 後重送",
                    ),
                    Wp55Gate::ReconfirmPending => (
                        "AUTOBOOT_QUIET",
                        Some(PyValue::Float(5.0)),
                        "boot quiet window 已結束但 READY 尚未重新確認；等 daemon 重新確認 READY 後重送",
                    ),
                    Wp55Gate::ReconfirmFailed => ("READY_UNCONFIRMED", None, READY_UNCONFIRMED_HINT),
                };
                let mut expected = sw_proto::pydict! { "ok" => false, "error_code" => code };
                match retry {
                    Some(retry) => {
                        assert!(matches!(retry, PyValue::Float(_)), "{case}：{retry:?}");
                        expected.insert("retry_after_s", retry);
                        expected.insert("hint", PyValue::from(hint));
                    }
                    None => {
                        expected.insert("hint", PyValue::from(hint));
                        expected.insert("recommended_action", PyValue::from("self_test"));
                    }
                }
                expected.insert("session", PyValue::Dict(public.clone()));
                assert_eq!(keys(&resp), keys(&expected), "{case}：鍵序");
                assert_eq!(resp, expected, "{case}");
                assert_eq!(h.reg.len(), records_before, "{case}：不得新增命令紀錄");
                assert_eq!(
                    h.reg.pending(h.core.session_id()),
                    pending_before,
                    "{case}：不得入列"
                );
            }
            if matches!(gate, Wp55Gate::ReconfirmFailed) {
                assert_eq!(
                    public["ready_reconfirm_failed"],
                    PyValue::Bool(true),
                    "{case}：submit 時的 expire_reconfirm 不因可信而略過，須落有界化終態：{public:?}"
                );
            }
        }
    }

    // (B) 前置檢查不因可信標記而繞過。
    #[derive(Debug)]
    enum Pre {
        Detached,
        Attached,
        NotCommandCapable,
        Recovering,
        Flashing,
    }
    for (pre, code) in [
        (Pre::Detached, "SESSION_NOT_READY"),
        (Pre::Attached, "SESSION_NOT_READY"),
        (Pre::NotCommandCapable, "PROFILE_NOT_COMMAND_CAPABLE"),
        (Pre::Recovering, "SESSION_RECOVERING"),
        (Pre::Flashing, "FLASHING_BUSY"),
    ] {
        let mut h = CoreHarness::new(match pre {
            Pre::NotCommandCapable => SHELL_NO_PROBE_PROFILE,
            _ => SHELL_PROFILE,
        });
        match pre {
            Pre::Detached => {}
            Pre::Attached | Pre::NotCommandCapable => {
                h.to_attached(None);
            }
            Pre::Recovering => {
                h.to_ready();
                h.force_state(SessionState::Recovering);
            }
            Pre::Flashing => {
                h.to_ready();
                h.force_state(SessionState::Flashing);
            }
        }
        let records_before = h.reg.len();
        let (baseline, _) = h.rpc_full(submit("agent:x", false, &SubmitOrigin::Rpc));
        let baseline = baseline.expect("submit");
        assert_eq!(
            pget(&baseline, "error_code"),
            PyValue::from(code),
            "{pre:?}：{baseline:?}"
        );
        for (source, trusted_human, origin, _) in matrix.clone() {
            let case = format!(
                "(B) {pre:?} source={source} trusted_human={trusted_human} origin={origin:?}"
            );
            let (resp, effects) = h.rpc_full(submit(source, trusted_human, &origin));
            let resp = resp.expect("submit");
            assert_eq!(keys(&resp), keys(&baseline), "{case}：鍵序");
            assert_eq!(resp, baseline, "{case}：可信標記不得改變前置檢查回應");
            assert!(resp.get("cmd_id").is_none(), "{case}");
            assert!(
                !effects.iter().any(|e| matches!(e, Effect::StartOp { .. })),
                "{case}"
            );
        }
        assert_eq!(
            h.reg.len(),
            records_before,
            "{pre:?}：前置檢查拒絕不得新增命令紀錄"
        );
    }
}
