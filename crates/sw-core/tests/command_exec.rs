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
    h.wait_for("TX 結算後 arm quiet", |h| {
        h.fence.with(|f| f.quiet_active)
    });
    assert_ne!(h.get_state()["boot_quiet_remaining_s"], PyValue::Null);
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
    let reboot = h.submit_ok("reboot");
    let q1 = h.submit_ok("echo a");
    let q2 = h.submit_ok("echo b");
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
