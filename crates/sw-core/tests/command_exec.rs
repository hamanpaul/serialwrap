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
use sw_pal::traits::{Clock, PalError};
use sw_proto::pycompat::{PyErr, clean_text, py_strip, splitlines};
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

fn wal_tx_bytes_since(h: &ActorRig, count: usize) -> Vec<u8> {
    h.wal_tx_payloads()
        .into_iter()
        .skip(count)
        .flatten()
        .collect()
}

/// 等 WAL 出現指定 TX（OpRunner 已送出）。
fn wait_tx(h: &ActorRig, payload: &[u8]) {
    h.wait_for("TX 寫出", |h| wal_tx_has(h, payload));
}

fn run_i294_shipped_prefix_case(prompt_regex: &str, prompt: &str, co_packed: bool) {
    let profile = SHELL_PROFILE.replace("(?m)^root@t:.*# ", prompt_regex);
    let h = ActorRig::new_with_hub(&profile).ready_with_prompt(prompt.as_bytes());
    let console = h.attach_pty_console("i294-prefix-human");
    let tx_count = h.wal_tx_payloads().len();
    let human_generation = h.rx.human_tx_state().generation;
    console.push_input(b"echo HUMAN\n");
    wait_tx(&h, b"echo HUMAN\n");
    h.wait_for("human TX observation 已提交", |h| {
        h.rx.human_tx_state().generation > human_generation
    });

    let evaluator_checks = h.rx.snapshot_wait_progress_for_test().0;
    let id = h.submit_ok("echo PREFIX");
    wait_tx(&h, b"echo PREFIX\n");
    h.wait_running(&id);
    assert!(
        h.rx.wait_for_snapshot_wait_progress_for_test(
            evaluator_checks,
            h.rx.snapshot_len(),
            Duration::from_secs(5),
        ),
        "command evaluator 應先檢查 TX 後的既有 RX snapshot"
    );
    assert_eq!(
        h.command_get(&id)["status"],
        PyValue::from("running"),
        "舊 prompt／echo 尚未到達時命令應維持 running"
    );
    if co_packed {
        let packet = format!(
            "{prompt}\r\n{prompt}echo PREFIX\r\nPREFIX\r\n{prompt}"
        );
        h.inject_rx_processed(packet.as_bytes());
    } else {
        h.inject_rx_processed(prompt.as_bytes());
        assert_eq!(
            h.command_get(&id)["status"],
            PyValue::from("running"),
            "分包的 human 舊 prompt 不得完成命令"
        );

        let response = format!("echo PREFIX\r\nPREFIX\r\n{prompt} extra\r\nmore");
        h.inject_rx_processed(response.as_bytes());
        assert_eq!(
            h.command_get(&id)["status"],
            PyValue::from("running"),
            "prefix 後仍有資料時，命令必須維持 running"
        );
        h.inject_rx_processed(format!("\r\n{prompt}").as_bytes());
    }

    let result = h.wait_done(&id);
    assert_eq!(result["status"], PyValue::from("done"));
    let expected_stdout = if co_packed {
        "PREFIX".to_string()
    } else {
        format!("PREFIX\n{prompt} extra\nmore")
    };
    assert_eq!(result["stdout"], PyValue::from(expected_stdout));
    assert_eq!(result["partial"], PyValue::Bool(false));
    assert_eq!(
        wal_tx_bytes_since(&h, tx_count),
        b"echo HUMAN\necho PREFIX\n".to_vec(),
        "prefix 完成判定不增加或重送 TX"
    );
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
fn i294_human_prompt_before_agent_echo_does_not_complete() {
    let h = ActorRig::new_with_hub(SHELL_PROFILE).ready();
    let console = h.attach_pty_console("i294-human");
    let tx_count = h.wal_tx_payloads().len();
    console.push_input(b"echo HUMAN\n");
    wait_tx(&h, b"echo HUMAN\n");

    let id = h.submit_ok("echo AGENT");
    wait_tx(&h, b"echo AGENT\n");
    h.inject_rx_processed(b"root@t:/# ");
    let before_agent_echo = h.command_get(&id);
    assert_eq!(
        before_agent_echo["status"],
        PyValue::from("running"),
        "human 的舊 prompt 不得完成 agent 命令：{before_agent_echo:?}"
    );

    h.inject_rx_processed(b"echo AGENT\r\nAGENT\r\nroot@t:/# ");
    let rec = h.wait_done(&id);
    assert_eq!(rec["status"], PyValue::from("done"));
    assert_eq!(rec["stdout"], PyValue::from("AGENT"));
    assert_eq!(rec["partial"], PyValue::Bool(false));
    assert!(
        !h.wal_tx_payloads().iter().any(|payload| payload == b"\x03" || payload == b"\x04"),
        "完成判定不得送救援鍵"
    );
    assert_eq!(
        wal_tx_bytes_since(&h, tx_count),
        b"echo HUMAN\necho AGENT\n".to_vec(),
        "只送原本的人類行與 agent 命令"
    );
}

#[test]
fn i294_unrelated_busybox_wrap_marker_does_not_join_plain_crlf_echo() {
    let h = ActorRig::new_with_hub(SHELL_PROFILE).ready();
    let console = h.attach_pty_console("i294-unrelated-busybox-wrap");
    let tx_count = h.wal_tx_payloads().len();
    console.push_input(b"echo HUMAN\n");
    wait_tx(&h, b"echo HUMAN\n");
    h.inject_rx_reader_processed(b"echo HUMAN\r\nHUMAN\r\n");

    let command = format!("/tmp/{}", "x".repeat(60));
    let id = h.submit_ok(&command);
    let tx = format!("{command}\n");
    wait_tx(&h, tx.as_bytes());

    // BusyBox 折行標記屬於前面的雜訊行；ANSI 與 backspace 清理後仍不得把證據移到候選 echo。
    let unrelated = format!(
        "root@t:/# \r\nnoiseX\x08\x1b[31m\r\r\n\x1b[0m{}\r\n{}\r\nroot@t:/# ",
        &command[..40],
        &command[40..]
    );
    let checks = h.rx.snapshot_wait_progress_for_test().0;
    let expected_end = h
        .rx
        .snapshot_len()
        .saturating_add(unrelated.chars().count() as u64);
    h.inject_rx_reader_processed(unrelated.as_bytes());
    h.wait_for("BusyBox 無關標記的 evaluator 判定", |rig| {
        let status = rig.command_get(&id)["status"].clone();
        let (_, checked_end) = rig.rx.snapshot_wait_progress_for_test();
        status != "running"
            || (rig.rx.snapshot_wait_progress_for_test().0 > checks && checked_end >= expected_end)
    });
    assert_eq!(
        h.command_get(&id)["status"],
        PyValue::from("running"),
        "無關的 BusyBox 標記不得拼接普通 CRLF；未完成 human TX 後的舊 prompt 也不能完成命令"
    );

    h.inject_rx_processed(format!("{command}\r\nOK\r\nroot@t:/# ").as_bytes());
    let result = h.wait_done(&id);
    assert_eq!(result["status"], PyValue::from("done"));
    assert_eq!(result["stdout"], PyValue::from("OK"));
    assert_eq!(result["partial"], PyValue::Bool(false));
    assert_eq!(wal_tx_bytes_since(&h, tx_count), format!("echo HUMAN\n{tx}").as_bytes());
}

#[test]
fn i294_unrelated_readline_wrap_marker_does_not_join_plain_space_echo() {
    let h = ActorRig::new_with_hub(SHELL_PROFILE).ready();
    let console = h.attach_pty_console("i294-unrelated-readline-wrap");
    let tx_count = h.wal_tx_payloads().len();
    console.push_input(b"echo HUMAN\n");
    wait_tx(&h, b"echo HUMAN\n");
    h.inject_rx_reader_processed(b"echo HUMAN\r\nHUMAN\r\n");

    let command = format!("/tmp/{}", "x".repeat(60));
    let id = h.submit_ok(&command);
    let tx = format!("{command}\n");
    wait_tx(&h, tx.as_bytes());

    // readline 標記只在前面的雜訊行；候選 echo 中的普通空白不是折行插入點。
    let unrelated = format!(
        "root@t:/# \r\nnoise \r\n{} {}\r\nroot@t:/# ",
        &command[..40],
        &command[40..]
    );
    let checks = h.rx.snapshot_wait_progress_for_test().0;
    let expected_end = h
        .rx
        .snapshot_len()
        .saturating_add(unrelated.chars().count() as u64);
    h.inject_rx_reader_processed(unrelated.as_bytes());
    h.wait_for("readline 無關標記的 evaluator 判定", |rig| {
        let status = rig.command_get(&id)["status"].clone();
        let (_, checked_end) = rig.rx.snapshot_wait_progress_for_test();
        status != "running"
            || (rig.rx.snapshot_wait_progress_for_test().0 > checks && checked_end >= expected_end)
    });
    assert_eq!(
        h.command_get(&id)["status"],
        PyValue::from("running"),
        "無關的 readline 標記不得授權候選 echo 的普通空白；human 舊 prompt 也不能完成命令"
    );

    h.inject_rx_processed(format!("{command}\r\nOK\r\nroot@t:/# ").as_bytes());
    let result = h.wait_done(&id);
    assert_eq!(result["status"], PyValue::from("done"));
    assert_eq!(result["stdout"], PyValue::from("OK"));
    assert_eq!(result["partial"], PyValue::Bool(false));
    assert_eq!(wal_tx_bytes_since(&h, tx_count), format!("echo HUMAN\n{tx}").as_bytes());
}

#[test]
fn i294_human_reply_arriving_inside_submit_does_not_poison_next_no_echo_command() {
    use std::sync::atomic::{AtomicBool, Ordering};

    let h = ActorRig::new_with_hub(SHELL_PROFILE).ready();
    let console = h.attach_pty_console("i294-human-submit-race");
    let before_generation = h.rx.human_tx_state().generation;
    let reply = b"echo HUMAN\r\nHUMAN\r\nroot@t:/# ".to_vec();
    let rx = Arc::clone(&h.rx);
    let dev = h.dev.clone();
    let called = Arc::new(AtomicBool::new(false));
    let called_hook = Arc::clone(&called);
    let (arrived_tx, arrived_rx) = crossbeam_channel::bounded(1);
    let (resume_tx, resume_rx) = crossbeam_channel::bounded(1);
    h.dev.on_os_submit_after_write(Box::new(move || {
        if called_hook.swap(true, Ordering::SeqCst) {
            return;
        }
        let expected = rx.total_bytes().saturating_add(reply.len() as u64);
        dev.push_rx(&reply);
        assert!(rx.wait_for_reader_bytes_for_test(expected, Duration::from_secs(5)));
        arrived_tx.send(()).expect("測試仍等待 submit 交錯點");
        resume_rx
            .recv_timeout(Duration::from_secs(5))
            .expect("測試應釋放仍在 OS submit 閉包內的 writer");
    }));

    console.push_input(b"echo HUMAN\n");
    arrived_rx
        .recv_timeout(Duration::from_secs(5))
        .expect("human TX 已寫入，但 submit 尚未回傳");
    assert!(
        h.rx.text_from(0).ends_with("echo HUMAN\r\nHUMAN\r\nroot@t:/# "),
        "完整 human echo、輸出與 prompt 已由 reader 收到"
    );
    assert_eq!(
        h.rx.human_tx_state().generation,
        before_generation,
        "TX observation 尚未提交，writer 仍停在 submit 閉包內"
    );
    resume_tx.send(()).expect("writer hook 尚在等待釋放");
    wait_tx(&h, b"echo HUMAN\n");
    h.wait_for("human TX observation 已提交", |rig| {
        rig.rx.human_tx_state().generation > before_generation
    });

    let tx_count = h.wal_tx_payloads().len();
    let (deadline_started_tx, deadline_started_rx) = crossbeam_channel::bounded(1);
    h.rx
        .set_snapshot_wait_enter_hook_for_test(Some(Arc::new(move || {
            deadline_started_tx
                .send(())
                .expect("completion waiter 已建立 operation deadline");
        })));
    let id = h.submit_with("printf NO_ECHO", |request| request.timeout_s = 0.2);
    wait_tx(&h, b"printf NO_ECHO\n");
    deadline_started_rx
        .recv_timeout(Duration::from_secs(5))
        .expect("推進假時鐘前 operation deadline 已建立");
    h.inject_rx_processed(b"NO_ECHO\r\nroot@t:/# ");
    h.advance(Duration::from_millis(200));
    let result = h.wait_done(&id);
    assert_eq!(result["status"], PyValue::from("done"));
    assert_eq!(result["stdout"], PyValue::from("NO_ECHO"));
    assert_eq!(result["partial"], PyValue::Bool(false));
    assert_eq!(wal_tx_bytes_since(&h, tx_count), b"printf NO_ECHO\n");
}

#[test]
fn i294_incomplete_human_reply_still_makes_no_echo_command_partial() {
    let h = ActorRig::new_with_hub(SHELL_PROFILE).ready();
    let console = h.attach_pty_console("i294-human-incomplete-reply");
    let tx_count = h.wal_tx_payloads().len();
    console.push_input(b"echo HUMAN\n");
    wait_tx(&h, b"echo HUMAN\n");
    h.inject_rx_reader_processed(b"echo HUMAN\r\nHUMAN\r\n");

    let (deadline_started_tx, deadline_started_rx) = crossbeam_channel::bounded(1);
    h.rx
        .set_snapshot_wait_enter_hook_for_test(Some(Arc::new(move || {
            deadline_started_tx
                .send(())
                .expect("completion waiter 已建立 operation deadline");
        })));
    let id = h.submit_with("printf AGENT", |request| request.timeout_s = 0.2);
    wait_tx(&h, b"printf AGENT\n");
    deadline_started_rx
        .recv_timeout(Duration::from_secs(5))
        .expect("推進假時鐘前 operation deadline 已建立");
    h.inject_rx_processed(b"AGENT\r\nroot@t:/# ");
    assert_eq!(
        h.command_get(&id)["status"],
        PyValue::from("running"),
        "human 回應尚無 prompt 時，不可由無 echo agent prompt 降階完成"
    );

    h.advance(Duration::from_millis(200));
    let result = h.wait_done(&id);
    assert_eq!(result["status"], PyValue::from("error"));
    assert_eq!(result["error_code"], PyValue::from("PROMPT_TIMEOUT"));
    assert_eq!(result["stdout"], PyValue::from("AGENT"));
    assert_eq!(result["partial"], PyValue::Bool(true));
    assert_eq!(result["recovery_action"], PyValue::from("NONE"));
    assert_eq!(wal_tx_bytes_since(&h, tx_count), b"echo HUMAN\nprintf AGENT\n");
}

#[test]
fn i294_shipped_prefix_prompts_handle_split_and_co_packed_rx() {
    run_i294_shipped_prefix_case(r"(?m)^root@prplOS:.*# ", "root@prplOS:~# ", false);
    run_i294_shipped_prefix_case(r"(?m)^root@prplOS:.*# ", "root@prplOS:~# ", true);
    run_i294_shipped_prefix_case(
        r"(?m)^root@Speedport Smart 4 Typ [AB]:[^\r\n#]*#[ \t]*",
        "root@Speedport Smart 4 Typ A:~# ",
        false,
    );
    run_i294_shipped_prefix_case(
        r"(?m)^root@Speedport Smart 4 Typ [AB]:[^\r\n#]*#[ \t]*",
        "root@Speedport Smart 4 Typ A:~# ",
        true,
    );
}

#[test]
fn i294_completion_snapshot_ignores_later_rx() {
    let h = ActorRig::new(SHELL_PROFILE).ready();
    let tx_count = h.wal_tx_payloads().len();
    let (matched_tx, matched_rx) = crossbeam_channel::bounded(1);
    let (resume_tx, resume_rx) = crossbeam_channel::bounded(1);
    h.rx.set_snapshot_wait_match_hook_for_test(Some(Arc::new(move |snapshot| {
        matched_tx
            .send(snapshot.clone())
            .expect("測試仍等待 completion snapshot");
        resume_rx
            .recv()
            .expect("測試釋放 completion snapshot waiter");
    })));

    let id = h.submit_ok("echo SNAPSHOT");
    wait_tx(&h, b"echo SNAPSHOT\n");
    h.dev
        .push_rx(b"echo SNAPSHOT\r\nSNAPSHOT\r\nroot@t:/# ");
    let accepted = matched_rx
        .recv_timeout(Duration::from_secs(5))
        .expect("evaluator 應接受原始 completion snapshot");
    assert!(accepted.text.ends_with("SNAPSHOT\r\nroot@t:/# "));

    h.inject_rx_reader_processed(b"\r\nLATE_RX");
    resume_tx
        .send(())
        .expect("completion snapshot waiter 應仍在暫停");

    let result = h.wait_done(&id);
    assert_eq!(result["status"], PyValue::from("done"));
    assert_eq!(result["stdout"], PyValue::from("SNAPSHOT"));
    assert_eq!(result["partial"], PyValue::Bool(false));
    assert_eq!(
        wal_tx_bytes_since(&h, tx_count),
        b"echo SNAPSHOT\n".to_vec(),
        "接受 snapshot 後抵達的 RX 不得改變 stdout 或增加 TX"
    );
}

#[test]
fn i294_r8_completion_decided_before_deadline_remains_done() {
    let h = ActorRig::new(SHELL_PROFILE).ready();
    h.dev.on_tx_reply(b"\x03", b"^C\r\nroot@t:/# ");
    let (matched_tx, matched_rx) = crossbeam_channel::bounded(1);
    let (resume_tx, resume_rx) = crossbeam_channel::bounded(1);
    h.rx.set_snapshot_wait_match_hook_for_test(Some(Arc::new(move |snapshot| {
        matched_tx
            .send(snapshot.clone())
            .expect("test waits for pre-deadline completion decision");
        resume_rx
            .recv()
            .expect("test releases pre-deadline completion decision");
    })));

    let tx_count = h.wal_tx_payloads().len();
    let id = h.submit_with("echo PRE_DEADLINE", |request| request.timeout_s = 0.2);
    wait_tx(&h, b"echo PRE_DEADLINE\n");
    h.inject_rx_reader_processed(b"echo PRE_DEADLINE\r\nPRE_DEADLINE\r\nroot@t:/# ");
    let accepted = matched_rx
        .recv_timeout(Duration::from_secs(5))
        .expect("evaluator 已在 deadline 前判定完成");
    assert!(accepted.text.ends_with("PRE_DEADLINE\r\nroot@t:/# "));
    assert!(h.clock.mono_ns() < 200_000_000, "FakeClock 仍在 deadline 前");
    h.advance(Duration::from_millis(201));
    resume_tx.send(()).expect("釋放 pre-deadline completion barrier");

    let result = h.wait_done(&id);
    assert_eq!(result["status"], PyValue::from("done"), "{result:?}");
    assert_eq!(result["stdout"], PyValue::from("PRE_DEADLINE"), "{result:?}");
    assert_eq!(result["partial"], PyValue::Bool(false), "{result:?}");
    assert_eq!(wal_tx_bytes_since(&h, tx_count), b"echo PRE_DEADLINE\n".to_vec());
}

#[test]
fn i294_late_echo_and_prompt_after_deadline_do_not_complete() {
    let h = ActorRig::new(SHELL_PROFILE).ready();
    h.dev.on_tx_reply(b"\x03", b"^C\r\nroot@t:/# ");
    let tx_count = h.wal_tx_payloads().len();
    let (deadline_tx, deadline_rx) = crossbeam_channel::bounded(1);
    let (resume_tx, resume_rx) = crossbeam_channel::bounded(1);
    h.rx
        .set_snapshot_wait_deadline_hook_for_test(Some(Arc::new(move || {
            deadline_tx.send(()).expect("test waits for deadline barrier");
            resume_rx.recv().expect("test releases deadline barrier");
        })));

    let (checks, _) = h.rx.snapshot_wait_progress_for_test();
    let id = h.submit_with("echo LATE", |request| request.timeout_s = 0.2);
    wait_tx(&h, b"echo LATE\n");
    assert!(h.rx.wait_for_snapshot_wait_progress_for_test(
        checks,
        h.rx.snapshot_len(),
        Duration::from_secs(5),
    ));

    h.advance(Duration::from_millis(201));
    deadline_rx
        .recv_timeout(Duration::from_secs(5))
        .expect("waiter must reach the configured deadline before late RX");
    h.inject_rx_reader_processed(b"echo LATE\r\nLATE\r\nroot@t:/# ");
    h.rx
        .record_human_tx_for_test(b"late human TX", &*h.clock);
    resume_tx.send(()).expect("deadline waiter remains paused");

    let result = h.wait_done(&id);
    assert_eq!(result["status"], PyValue::from("error"), "截止後、timeout 決策前才開始的 human submit 保守走 human timeout：{result:?}");
    assert_eq!(result["error_code"], PyValue::from("PROMPT_TIMEOUT"), "{result:?}");
    assert_eq!(result["stdout"], PyValue::from(""), "截止後 RX 不回填 partial snapshot：{result:?}");
    assert_eq!(result["partial"], PyValue::Bool(true), "{result:?}");
    assert_eq!(result["recovery_action"], PyValue::from("NONE"), "{result:?}");
    assert_eq!(wal_tx_bytes_since(&h, tx_count), b"echo LATE\n".to_vec());
    let recovered = h.request(SessionRequest::ResultTail {
        cmd_id: id,
        from_chunk: 0,
        limit: 10,
    });
    assert_eq!(recovered["ok"], PyValue::Bool(true), "{recovered:?}");
    assert_eq!(
        recovered["error_code"],
        PyValue::from("PROMPT_TIMEOUT"),
        "截止後、決策前開始的 submit 必須走 timeout partial：{recovered:?}"
    );
}

#[test]
fn i294_r7_absolute_deadline_is_not_restarted_before_snapshot_wait() {
    let h = ActorRig::new(SHELL_PROFILE).ready();
    let (entered_tx, entered_rx) = crossbeam_channel::bounded(1);
    let (resume_tx, resume_rx) = crossbeam_channel::bounded(1);
    h.rx.set_snapshot_wait_enter_hook_for_test(Some(Arc::new(move || {
        entered_tx.send(()).expect("test waits for inner wait entry");
        resume_rx.recv().expect("test releases inner wait entry");
    })));

    let tx_count = h.wal_tx_payloads().len();
    let id = h.submit_with("echo OUTER_DEADLINE", |request| request.timeout_s = 0.2);
    wait_tx(&h, b"echo OUTER_DEADLINE\n");
    entered_rx
        .recv_timeout(Duration::from_secs(5))
        .expect("外層 deadline 已計算，等待器尚未起算");

    h.advance(Duration::from_millis(201));
    h.inject_rx_reader_processed(b"echo OUTER_DEADLINE\r\nOUTER_DEADLINE\r\nroot@t:/# ");
    resume_tx.send(()).expect("釋放等待器入口 barrier");

    // 以救援 TX 的 WAL 作同步點，讓救援等待本身也透過 FakeClock 到期；late prompt 不得被當成
    // 命令完成或救援回 prompt 的證據。
    wait_tx(&h, b"\x03");
    h.advance(Duration::from_secs(2));
    wait_tx(&h, b"\x04");
    h.advance(Duration::from_secs(2));
    let result = h.wait_done(&id);
    assert_ne!(result["status"], PyValue::from("done"), "外層期限後的 echo 不得完成：{result:?}");
    assert_eq!(result["partial"], PyValue::Bool(true), "{result:?}");
    assert_eq!(result["error_code"], PyValue::from("PROMPT_TIMEOUT"), "{result:?}");
    assert_eq!(result["recovery_action"], PyValue::from("NONE"), "{result:?}");
    assert_eq!(wal_tx_bytes_since(&h, tx_count), b"echo OUTER_DEADLINE\n\x03\x04".to_vec());
}

#[test]
fn i294_r7_deadline_settlement_includes_pre_deadline_human_tx() {
    let h = ActorRig::new(SHELL_PROFILE).ready();
    h.dev.on_tx_reply(b"\x03", b"^C\r\nroot@t:/# ");
    let (evaluated_tx, evaluated_rx) = crossbeam_channel::bounded(1);
    let (resume_tx, resume_rx) = crossbeam_channel::bounded(1);
    h.rx
        .set_snapshot_wait_evaluated_hook_for_test(Some(Arc::new(move |_| {
            evaluated_tx.send(()).expect("test waits for evaluator barrier");
            resume_rx.recv().expect("test releases evaluator barrier");
        })));

    let tx_count = h.wal_tx_payloads().len();
    let id = h.submit_with("printf NO_ECHO", |request| request.timeout_s = 1.0);
    wait_tx(&h, b"printf NO_ECHO\n");
    evaluated_rx
        .recv_timeout(Duration::from_secs(5))
        .expect("evaluator 已釋鎖並檢查舊 snapshot");

    h.clock.advance(Duration::from_millis(500));
    h.rx.record_human_tx_for_test(b"echo HUMAN\n", &*h.clock);
    h.clock.advance(Duration::from_millis(500));
    resume_tx.send(()).expect("釋放 evaluator barrier");

    let result = h.wait_done(&id);
    assert_eq!(result["status"], PyValue::from("error"), "{result:?}");
    assert_eq!(result["error_code"], PyValue::from("PROMPT_TIMEOUT"), "{result:?}");
    assert_eq!(result["partial"], PyValue::Bool(true), "{result:?}");
    assert_eq!(result["recovery_action"], PyValue::from("NONE"), "{result:?}");
    assert_eq!(h.get_state()["state"], PyValue::from("READY"));
    assert_eq!(wal_tx_bytes_since(&h, tx_count), b"printf NO_ECHO\n".to_vec());
}

#[test]
fn i294_r7_deadline_settlement_includes_pre_deadline_rx() {
    let h = ActorRig::new(SHELL_PROFILE).ready();
    h.dev.on_tx_reply(b"\x03", b"^C\r\nroot@t:/# ");
    let (evaluated_tx, evaluated_rx) = crossbeam_channel::bounded(1);
    let (resume_tx, resume_rx) = crossbeam_channel::bounded(1);
    h.rx
        .set_snapshot_wait_evaluated_hook_for_test(Some(Arc::new(move |_| {
            evaluated_tx.send(()).expect("test waits for evaluator barrier");
            resume_rx.recv().expect("test releases evaluator barrier");
        })));

    let tx_count = h.wal_tx_payloads().len();
    let id = h.submit_with("echo DEADLINE_RX", |request| request.timeout_s = 1.0);
    wait_tx(&h, b"echo DEADLINE_RX\n");
    evaluated_rx
        .recv_timeout(Duration::from_secs(5))
        .expect("evaluator 已釋鎖並檢查舊 snapshot");

    h.clock.advance(Duration::from_millis(500));
    h.inject_rx_reader_processed(b"echo DEADLINE_RX\r\nDEADLINE_RX\r\nroot@t:/# ");
    h.clock.advance(Duration::from_millis(501));
    h.inject_rx_reader_processed(&vec![b'z'; 140_000]);
    resume_tx.send(()).expect("釋放 evaluator barrier");

    let result = h.wait_done(&id);
    assert_eq!(result["status"], PyValue::from("done"), "期限後不得事後採用 RX 完成判定；只有 Ctrl-C 找回 prompt 才回成功：{result:?}");
    let baseline_stdout = format!(
        "{}^C",
        "z".repeat(sw_core::resources::RX_WINDOW_MAX_CHARS - b"^C\r\nroot@t:/# ".len())
    );
    assert_eq!(
        result["stdout"],
        PyValue::from(baseline_stdout),
        "無 human TX 時 timeout stdout 必須採用完整 recovery 結果，與 9af187ca 基線相同：{result:?}"
    );
    assert_eq!(result["partial"], PyValue::Bool(true), "{result:?}");
    assert_eq!(result["recovery_action"], PyValue::from("CTRL_C"), "無 human TX 時保留基線救援：{result:?}");
    assert_eq!(
        wal_tx_bytes_since(&h, tx_count),
        b"echo DEADLINE_RX\n\x03".to_vec(),
        "期限內尚未判定完成且無 human TX 時維持基線 Ctrl-C 救援"
    );
    let recovered = h.request(SessionRequest::ResultTail {
        cmd_id: id,
        from_chunk: 0,
        limit: 10,
    });
    assert_eq!(recovered["ok"], PyValue::Bool(true), "{recovered:?}");
    assert_eq!(
        recovered["error_code"],
        PyValue::from("PROMPT_TIMEOUT_RECOVERED"),
        "期限前 RX 不得事後改判正常完成：{recovered:?}"
    );
}

#[test]
fn i294_r8_expired_before_first_wait_returns_timeout_partial() {
    for (human_state, echo_found) in [
        ("none", true),
        ("none", false),
        ("registered", true),
        ("registered", false),
        ("pending", true),
        ("pending", false),
    ] {
        let h = ActorRig::new(SHELL_PROFILE).ready();
        h.dev.on_tx_reply(b"\x03", b"^C\r\nroot@t:/# ");
        let (entered_tx, entered_rx) = crossbeam_channel::bounded(1);
        let (resume_tx, resume_rx) = crossbeam_channel::bounded(1);
        h.rx.set_snapshot_wait_enter_hook_for_test(Some(Arc::new(move || {
            entered_tx.send(()).expect("test waits before first snapshot wait");
            resume_rx.recv().expect("test releases first snapshot wait");
        })));

        let tx_count = h.wal_tx_payloads().len();
        let id = h.submit_with("echo FIRST_WAIT", |request| request.timeout_s = 0.2);
        wait_tx(&h, b"echo FIRST_WAIT\n");
        entered_rx
            .recv_timeout(Duration::from_secs(5))
            .expect("已進入首次 snapshot wait 前的 barrier");
        match human_state {
            "registered" => h.rx.record_human_tx_for_test(b"echo HUMAN\n", &*h.clock),
            "pending" => h.rx.set_tx_submit_pending_for_test(true),
            "none" => {}
            _ => unreachable!("unknown human state"),
        }
        let raw = if echo_found {
            b"echo FIRST_WAIT\r\nFIRST_WAIT\r\nroot@t:/# ".as_slice()
        } else {
            b"partial output\r\nroot@t:/# ".as_slice()
        };
        h.inject_rx_reader_processed(raw);
        h.advance(Duration::from_millis(201));
        resume_tx.send(()).expect("釋放首次 snapshot wait");

        let result = h.wait_done(&id);
        if human_state == "pending" {
            h.rx.set_tx_submit_pending_for_test(false);
        }
        assert_eq!(result["partial"], PyValue::Bool(true), "state={human_state}, echo={echo_found}: {result:?}");
        if human_state == "none" {
            assert_eq!(result["status"], PyValue::from("done"), "期限後只可由 recovery 找回 prompt：{result:?}");
            assert_eq!(result["error_code"], PyValue::Null, "{result:?}");
            assert_eq!(result["recovery_action"], PyValue::from("CTRL_C"), "{result:?}");
            assert_eq!(wal_tx_bytes_since(&h, tx_count), b"echo FIRST_WAIT\n\x03".to_vec());
            let recovered = h.request(SessionRequest::ResultTail {
                cmd_id: id,
                from_chunk: 0,
                limit: 10,
            });
            assert_eq!(recovered["ok"], PyValue::Bool(true), "{recovered:?}");
            assert_eq!(recovered["error_code"], PyValue::from("PROMPT_TIMEOUT_RECOVERED"), "{recovered:?}");
        } else {
            assert_eq!(result["status"], PyValue::from("error"), "{result:?}");
            assert_eq!(result["error_code"], PyValue::from("PROMPT_TIMEOUT"), "{result:?}");
            assert_eq!(result["recovery_action"], PyValue::from("NONE"), "{result:?}");
            assert_eq!(result["stdout"], PyValue::from(if echo_found { "FIRST_WAIT" } else { "partial output" }), "{result:?}");
            assert_eq!(h.get_state()["state"], PyValue::from("READY"));
            assert_eq!(wal_tx_bytes_since(&h, tx_count), b"echo FIRST_WAIT\n".to_vec());
        }
    }
}

#[test]
fn i294_r8_timeout_after_deadline_snapshot_keeps_partial_and_baseline_recovery() {
    let h = ActorRig::new(SHELL_PROFILE).ready();
    h.dev.on_tx_reply(b"\x03", b"^C\r\nroot@t:/# ");
    let (deadline_tx, deadline_rx) = crossbeam_channel::bounded(1);
    let (resume_tx, resume_rx) = crossbeam_channel::bounded(1);
    h.rx
        .set_snapshot_wait_deadline_hook_for_test(Some(Arc::new(move || {
            deadline_tx.send(()).expect("test waits for operation deadline snapshot");
            resume_rx.recv().expect("test releases deadline settlement");
        })));

    let tx_count = h.wal_tx_payloads().len();
    let id = h.submit_with("printf NO_OUTPUT", |request| request.timeout_s = 0.2);
    let (checks, _) = h.rx.snapshot_wait_progress_for_test();
    wait_tx(&h, b"printf NO_OUTPUT\n");
    assert!(h.rx.wait_for_snapshot_wait_progress_for_test(
        checks,
        h.rx.snapshot_len(),
        Duration::from_secs(5),
    ));
    h.advance(Duration::from_millis(201));
    deadline_rx
        .recv_timeout(Duration::from_secs(5))
        .expect("等待器已取得逾時 snapshot");
    resume_tx.send(()).expect("釋放逾時結算");

    let result = h.wait_done(&id);
    assert_eq!(result["status"], PyValue::from("done"), "{result:?}");
    assert_eq!(result["partial"], PyValue::Bool(true), "{result:?}");
    assert_eq!(result["recovery_action"], PyValue::from("CTRL_C"), "{result:?}");
    assert_eq!(wal_tx_bytes_since(&h, tx_count), b"printf NO_OUTPUT\n\x03".to_vec());
    let recovered = h.request(SessionRequest::ResultTail {
        cmd_id: id,
        from_chunk: 0,
        limit: 10,
    });
    assert_eq!(recovered["ok"], PyValue::Bool(true), "{recovered:?}");
    assert_eq!(
        recovered["error_code"],
        PyValue::from("PROMPT_TIMEOUT_RECOVERED"),
        "只有救援鍵找回 prompt 才回報基線 recovery code：{recovered:?}"
    );
}

#[test]
fn i294_r8_tx_submit_pending_at_settlement_suppresses_recovery() {
    for pending_before_deadline in [true, false] {
        let h = ActorRig::new(SHELL_PROFILE).ready();
        h.dev.on_tx_reply(b"\x03", b"^C\r\nroot@t:/# ");
        let (deadline_tx, deadline_rx) = crossbeam_channel::bounded(1);
        let (resume_tx, resume_rx) = crossbeam_channel::bounded(1);
        h.rx
            .set_snapshot_wait_deadline_hook_for_test(Some(Arc::new(move || {
                deadline_tx.send(()).expect("operation deadline settlement reached");
                resume_rx.recv().expect("test releases deadline settlement");
            })));

        let tx_count = h.wal_tx_payloads().len();
        let id = h.submit_with("printf HUMAN_PENDING", |request| request.timeout_s = 0.2);
        let (checks, _) = h.rx.snapshot_wait_progress_for_test();
        wait_tx(&h, b"printf HUMAN_PENDING\n");
        assert!(h.rx.wait_for_snapshot_wait_progress_for_test(
            checks,
            h.rx.snapshot_len(),
            Duration::from_secs(5),
        ));
        if pending_before_deadline {
            h.rx.set_tx_submit_pending_for_test(true);
        }
        h.advance(Duration::from_millis(201));
        deadline_rx
            .recv_timeout(Duration::from_secs(5))
            .expect("deadline snapshot 已建立；第一輪在途、第二輪在截止後啟動");
        if !pending_before_deadline {
            h.rx.set_tx_submit_pending_for_test(true);
        }
        resume_tx.send(()).expect("釋放 deadline settlement");

        let result = h.wait_done(&id);
        h.rx.set_tx_submit_pending_for_test(false);
        assert_eq!(result["status"], PyValue::from("error"), "{result:?}");
        assert_eq!(result["error_code"], PyValue::from("PROMPT_TIMEOUT"), "{result:?}");
        assert_eq!(result["partial"], PyValue::Bool(true), "{result:?}");
        assert_eq!(result["recovery_action"], PyValue::from("NONE"), "{result:?}");
        assert_eq!(h.get_state()["state"], PyValue::from("READY"));
        assert_eq!(
            wal_tx_bytes_since(&h, tx_count),
            b"printf HUMAN_PENDING\n".to_vec(),
            "結算時有在途 human submit 必須抑制所有救援 TX；pending_before_deadline={pending_before_deadline}"
        );
    }
}

fn i294_r9_run_silence_timeout_with_human(pending_submit: bool, echo_found: bool) {
    let h = ActorRig::new(SHELL_PROFILE).ready();
    h.dev.on_tx_reply(b"\x03", b"^C\r\nroot@t:/# ");

    let (first_wait_tx, first_wait_rx) = crossbeam_channel::bounded(1);
    let (release_first_wait_tx, release_first_wait_rx) = crossbeam_channel::bounded(1);
    h.rx
        .set_snapshot_wait_enter_hook_for_test(Some(Arc::new(move || {
            first_wait_tx.send(()).expect("等待第一個靜默 chunk");
            release_first_wait_rx.recv().expect("釋放第一個靜默 chunk");
        })));
    let (first_timeout_tx, first_timeout_rx) = crossbeam_channel::bounded(1);
    let (release_first_timeout_tx, release_first_timeout_rx) = crossbeam_channel::bounded(1);
    let (second_timeout_tx, second_timeout_rx) = crossbeam_channel::bounded(1);
    let (release_second_timeout_tx, release_second_timeout_rx) = crossbeam_channel::bounded(1);
    let timeout_count = Arc::new(std::sync::atomic::AtomicUsize::new(0));
    let timeout_count_hook = Arc::clone(&timeout_count);
    h.rx
        .set_snapshot_wait_deadline_hook_for_test(Some(Arc::new(move || {
            match timeout_count_hook.fetch_add(1, std::sync::atomic::Ordering::AcqRel) {
                0 => {
                    first_timeout_tx.send(()).expect("等待第一個靜默 chunk 到期");
                    release_first_timeout_rx
                        .recv()
                        .expect("釋放第一個靜默 chunk 到期點");
                }
                1 => {
                    second_timeout_tx.send(()).expect("等待第二個靜默 chunk 到期");
                    release_second_timeout_rx
                        .recv()
                        .expect("釋放第二個靜默 chunk 到期點");
                }
                _ => {}
            }
        })));

    let command = "printf R9_SILENCE";
    let tx_count = h.wal_tx_payloads().len();
    let id = h.submit_with(command, |request| {
        request.timeout_s = 30.0;
        request.expected_duration_s = Some(120.0);
    });
    first_wait_rx
        .recv_timeout(Duration::from_secs(5))
        .expect("命令已到第一個 snapshot wait barrier");
    wait_tx(&h, b"printf R9_SILENCE\n");

    if pending_submit {
        h.rx.set_tx_submit_pending_for_test(true);
    } else {
        h.rx.record_human_tx_for_test(b"echo HUMAN\n", &*h.clock);
        assert!(h.rx.human_tx_state().generation > 0);
    }

    let rx_before = h.rx.total_bytes();
    let partial = if echo_found {
        b"printf R9_SILENCE\r\npartial output\r\n".as_slice()
    } else {
        b"partial output\r\n".as_slice()
    };
    h.dev.push_rx(partial);
    assert!(h.rx.wait_for_reader_bytes_for_test(
        rx_before + partial.len() as u64,
        Duration::from_secs(5),
    ));
    release_first_wait_tx
        .send(())
        .expect("釋放第一個 snapshot wait");

    h.clock.advance(Duration::from_secs(30));
    h.send_control(ControlEvent::Tick);
    let _ = h.get_state();
    first_timeout_rx
        .recv_timeout(Duration::from_secs(5))
        .expect("第一個靜默 chunk 已到期；operation deadline 仍在 90 秒後");
    assert!(h.clock.mono_ns() < 120_000_000_000);

    let (second_wait_tx, second_wait_rx) = crossbeam_channel::bounded(1);
    let (release_second_wait_tx, release_second_wait_rx) = crossbeam_channel::bounded(1);
    h.rx
        .set_snapshot_wait_enter_hook_for_test(Some(Arc::new(move || {
            second_wait_tx.send(()).expect("等待第二個靜默 chunk");
            release_second_wait_rx.recv().expect("釋放第二個靜默 chunk");
        })));
    release_first_timeout_tx
        .send(())
        .expect("釋放第一個靜默 chunk 結算");
    second_wait_rx
        .recv_timeout(Duration::from_secs(5))
        .expect("RX 改變後重新開始第二個靜默 chunk");
    release_second_wait_tx
        .send(())
        .expect("釋放第二個靜默 chunk");
    h.clock.advance(Duration::from_secs(30));
    h.send_control(ControlEvent::Tick);
    let _ = h.get_state();
    second_timeout_rx
        .recv_timeout(Duration::from_secs(5))
        .expect("第二個靜默 chunk 已到期；operation deadline 尚未到期");
    release_second_timeout_tx
        .send(())
        .expect("釋放第二個靜默 chunk 結算");

    let result = h.wait_done(&id);
    if pending_submit {
        h.rx.set_tx_submit_pending_for_test(false);
    }
    assert_eq!(result["status"], PyValue::from("error"), "{result:?}");
    assert_eq!(result["error_code"], PyValue::from("PROMPT_TIMEOUT"), "{result:?}");
    assert_eq!(result["partial"], PyValue::Bool(true), "{result:?}");
    assert_eq!(result["recovery_action"], PyValue::from("NONE"), "{result:?}");
    let expected_stdout = if echo_found {
        "printf R9_SILENCE\npartial output"
    } else {
        "partial output\n"
    };
    assert_eq!(result["stdout"], PyValue::from(expected_stdout), "{result:?}");
    assert_eq!(h.get_state()["state"], PyValue::from("READY"));

    assert_eq!(wal_tx_bytes_since(&h, tx_count), b"printf R9_SILENCE\n");
}

#[test]
fn i294_r9_silence_timeout_full_echo_with_human_tx_suppresses_recovery() {
    i294_r9_run_silence_timeout_with_human(false, true);
}

#[test]
fn i294_r9_silence_timeout_full_echo_with_pending_submit_suppresses_recovery() {
    i294_r9_run_silence_timeout_with_human(true, true);
}

#[test]
fn i294_r9_silence_timeout_without_echo_with_human_tx_suppresses_recovery() {
    i294_r9_run_silence_timeout_with_human(false, false);
}

#[test]
fn i294_r9_silence_timeout_without_echo_with_pending_submit_suppresses_recovery() {
    i294_r9_run_silence_timeout_with_human(true, false);
}

#[derive(Clone, Copy, Debug)]
enum I294R10SubmitRacePoint {
    BeforeCutoffObserverAfterCutoffBeforeSnapshot,
    SnapshotContainsPendingThenSubmitCompletes,
    SubmitStartsAfterCutoffBeforeSettlement,
}

fn i294_r10_advance_clock(h: &ActorRig, duration: Duration) {
    let mut left = duration;
    while left > Duration::ZERO {
        let step = left.min(Duration::from_secs(1));
        left -= step;
        h.clock.advance(step);
        h.send_control(ControlEvent::Tick);
    }
}

fn i294_r10_begin_human_submit_after_write(
    h: &ActorRig,
) -> (
    u64,
    crossbeam_channel::Receiver<sw_core::tx_writer::TxReport>,
    crossbeam_channel::Sender<()>,
) {
    use std::sync::atomic::{AtomicBool, Ordering};

    let generation_before = h.rx.human_tx_state().generation;
    let (accepted_tx, accepted_rx) = crossbeam_channel::bounded(1);
    let (release_tx, release_rx) = crossbeam_channel::bounded(1);
    let called = Arc::new(AtomicBool::new(false));
    let called_hook = Arc::clone(&called);
    h.dev.on_os_submit_after_write(Box::new(move || {
        if called_hook.swap(true, Ordering::SeqCst) {
            return;
        }
        accepted_tx.send(()).expect("等待 human OS submit barrier");
        release_rx
            .recv_timeout(Duration::from_secs(10))
            .expect("測試釋放已由 fake OS 接受的 human submit");
    }));
    let tx = h.tx_handle();
    let permit = i294_r10_human_line_permit(h, 9_294_100);
    let (reply, report_rx) = crossbeam_channel::bounded(1);
    let job = sw_core::tx_writer::TxJob {
        permit,
        payload: b"echo HUMAN\n".to_vec(),
        reply,
        aggregate: None,
    };
    let queued = h.rx.with_human_submit_start_for_test(|| {
        tx.jobs.send(job)
    });
    assert!(queued.is_ok());
    accepted_rx
        .recv_timeout(Duration::from_secs(5))
        .expect("human line 已由 fake OS 接受、observer 尚未登錄");
    assert_eq!(
        h.rx.human_tx_state().generation,
        generation_before,
        "OS accept barrier 尚未返回，human observer 尚未登錄"
    );
    assert!(h.rx.snapshot_from(0).tx_submit_pending);
    (generation_before, report_rx, release_tx)
}

fn i294_r10_human_line_permit(h: &ActorRig, op_id: u64) -> sw_core::tx_writer::TxPermit {
    let (epoch, owner_gen, fsm_gen) = h.fence.with(|state| {
        (
            state.active_epoch.expect("ActorRig bridge 已開啟"),
            state.owner_gen,
            state.fsm_gen,
        )
    });
    sw_core::tx_writer::TxPermit {
        op_id: OpId(op_id),
        epoch,
        owner_gen,
        fsm_gen,
        class: sw_core::tx_writer::TxClass::HumanLine,
        source: "human:r10-test".into(),
        cmd_id: None,
        log: true,
        input_epoch: None,
        replay_hold: None,
        probe_id: None,
    }
}

fn i294_r10_release_human_submit(
    h: &ActorRig,
    generation_before: u64,
    report_rx: crossbeam_channel::Receiver<sw_core::tx_writer::TxReport>,
    release: crossbeam_channel::Sender<()>,
) {
    release.send(()).expect("human OS submit barrier 尚在等待");
    h.wait_for("human observer 登錄並清除 submit pending", |rig| {
        rig.rx.human_tx_state().generation > generation_before
            && !rig.rx.snapshot_from(0).tx_submit_pending
    });
    report_rx
        .recv_timeout(Duration::from_secs(5))
        .expect("HumanLine TxWriter report 已完成");
    wait_tx(h, b"echo HUMAN\n");
}

fn i294_r10_run_silence_submit_race(
    point: I294R10SubmitRacePoint,
    echo_found: bool,
) {
    let h = ActorRig::new(SHELL_PROFILE).ready();
    h.dev.on_tx_reply(b"\x03", b"^C\r\nroot@t:/# ");
    let command = "printf R10_RACE";
    let response = if echo_found {
        format!("{command}\r\npartial output\r\n").into_bytes()
    } else {
        b"partial output\r\n".to_vec()
    };
    let rx = Arc::clone(&h.rx);
    let dev = h.dev.clone();
    h.dev.on_os_submit_after_write(Box::new(move || {
        let expected = rx.total_bytes().saturating_add(response.len() as u64);
        dev.push_rx(&response);
        assert!(rx.wait_for_reader_bytes_for_test(expected, Duration::from_secs(5)));
    }));

    let (timeout_tx, timeout_rx) = crossbeam_channel::bounded(1);
    let (release_timeout_tx, release_timeout_rx) = crossbeam_channel::bounded(1);
    let timeout_hook: Arc<dyn Fn() + Send + Sync> = Arc::new(move || {
        timeout_tx.send(()).expect("等待 timeout snapshot barrier");
        release_timeout_rx
            .recv_timeout(Duration::from_secs(10))
            .expect("測試釋放 timeout snapshot barrier");
    });
    match point {
        I294R10SubmitRacePoint::BeforeCutoffObserverAfterCutoffBeforeSnapshot => h
            .rx
            .set_snapshot_wait_before_deadline_snapshot_hook_for_test(Some(timeout_hook)),
        I294R10SubmitRacePoint::SnapshotContainsPendingThenSubmitCompletes
        | I294R10SubmitRacePoint::SubmitStartsAfterCutoffBeforeSettlement => h
            .rx
            .set_snapshot_wait_deadline_hook_for_test(Some(timeout_hook)),
    }

    let (wait_started_tx, wait_started_rx) = crossbeam_channel::bounded(1);
    h.rx.set_snapshot_wait_enter_hook_for_test(Some(Arc::new(move || {
        wait_started_tx
            .send(())
            .expect("completion wait deadline 已建立");
    })));

    let tx_count = h.wal_tx_payloads().len();
    let id = h.submit_with(command, |request| {
        request.timeout_s = 30.0;
        request.expected_duration_s = Some(120.0);
    });
    wait_tx(&h, b"printf R10_RACE\n");
    wait_started_rx
        .recv_timeout(Duration::from_secs(5))
        .expect("command 已建立 quiet cutoff 的 snapshot wait");
    let command_started_at = h.clock.mono_ns();

    match point {
        I294R10SubmitRacePoint::BeforeCutoffObserverAfterCutoffBeforeSnapshot => {
            let (before, _accepted_rx, release) =
                i294_r10_begin_human_submit_after_write(&h);
            i294_r10_advance_clock(&h, Duration::from_secs(29));
            assert!(
                h.clock.mono_ns() < command_started_at + 30_000_000_000,
                "submit 開始仍早於 30 秒靜默 cutoff"
            );
            i294_r10_advance_clock(&h, Duration::from_secs(2));
            timeout_rx
                .recv_timeout(Duration::from_secs(5))
                .expect("quiet cutoff 已到，snapshot 建立前暫停");
            assert!(h.clock.mono_ns() < command_started_at + 120_000_000_000);
            i294_r10_release_human_submit(&h, before, _accepted_rx, release);
        }
        I294R10SubmitRacePoint::SnapshotContainsPendingThenSubmitCompletes => {
            let (before, _accepted_rx, release) =
                i294_r10_begin_human_submit_after_write(&h);
            i294_r10_advance_clock(&h, Duration::from_secs(31));
            timeout_rx
                .recv_timeout(Duration::from_secs(5))
                .expect("timeout snapshot 已在 submit pending 時建立");
            assert!(h.clock.mono_ns() < command_started_at + 120_000_000_000);
            i294_r10_release_human_submit(&h, before, _accepted_rx, release);
        }
        I294R10SubmitRacePoint::SubmitStartsAfterCutoffBeforeSettlement => {
            i294_r10_advance_clock(&h, Duration::from_secs(31));
            timeout_rx
                .recv_timeout(Duration::from_secs(5))
                .expect("無 human 的 timeout snapshot 已建立，settlement 暫停");
            assert!(h.clock.mono_ns() < command_started_at + 120_000_000_000);
            let (before, _accepted_rx, release) =
                i294_r10_begin_human_submit_after_write(&h);
            i294_r10_release_human_submit(&h, before, _accepted_rx, release);
        }
    }

    release_timeout_tx
        .send(())
        .expect("timeout snapshot barrier 尚在等待");
    let result = h.wait_done(&id);
    let expected_stdout = if echo_found {
        "printf R10_RACE\npartial output"
    } else {
        "partial output\n"
    };
    assert_eq!(result["status"], PyValue::from("error"), "{result:?}");
    assert_eq!(result["error_code"], PyValue::from("PROMPT_TIMEOUT"), "{result:?}");
    assert_eq!(result["partial"], PyValue::Bool(true), "{result:?}");
    assert_eq!(result["recovery_action"], PyValue::from("NONE"), "{result:?}");
    assert_eq!(result["stdout"], PyValue::from(expected_stdout), "{result:?}");
    assert_eq!(h.get_state()["state"], PyValue::from("READY"));
    let mut expected_tx = format!("{command}\n").into_bytes();
    expected_tx.extend_from_slice(b"echo HUMAN\n");
    assert_eq!(
        wal_tx_bytes_since(&h, tx_count),
        expected_tx,
        "human 交錯 timeout 必須是零救援 TX；point={point:?}, echo_found={echo_found}"
    );
}

#[test]
fn i294_r10_submit_before_cutoff_observed_after_cutoff_before_snapshot_with_echo() {
    i294_r10_run_silence_submit_race(
        I294R10SubmitRacePoint::BeforeCutoffObserverAfterCutoffBeforeSnapshot,
        true,
    );
}

#[test]
fn i294_r10_submit_before_cutoff_observed_after_cutoff_before_snapshot_without_echo() {
    i294_r10_run_silence_submit_race(
        I294R10SubmitRacePoint::BeforeCutoffObserverAfterCutoffBeforeSnapshot,
        false,
    );
}

#[test]
fn i294_r10_snapshot_pending_then_submit_completes_before_settlement_with_echo() {
    i294_r10_run_silence_submit_race(
        I294R10SubmitRacePoint::SnapshotContainsPendingThenSubmitCompletes,
        true,
    );
}

#[test]
fn i294_r10_snapshot_pending_then_submit_completes_before_settlement_without_echo() {
    i294_r10_run_silence_submit_race(
        I294R10SubmitRacePoint::SnapshotContainsPendingThenSubmitCompletes,
        false,
    );
}

#[test]
fn i294_r10_submit_starts_after_cutoff_before_decision_with_echo() {
    i294_r10_run_silence_submit_race(
        I294R10SubmitRacePoint::SubmitStartsAfterCutoffBeforeSettlement,
        true,
    );
}

#[test]
fn i294_r10_submit_starts_after_cutoff_before_decision_without_echo() {
    i294_r10_run_silence_submit_race(
        I294R10SubmitRacePoint::SubmitStartsAfterCutoffBeforeSettlement,
        false,
    );
}

#[test]
fn i294_r10_no_human_decision_serializes_first_rescue_key_before_new_submit() {
    let h = ActorRig::new(SHELL_PROFILE).ready();
    h.dev.on_tx_reply(b"\x03", b"^C\r\nroot@t:/# ");
    let command = "printf R10_RESCUE_ORDER";
    let response = format!("{command}\r\npartial output\r\n").into_bytes();
    let rx = Arc::clone(&h.rx);
    let dev = h.dev.clone();
    h.dev.on_os_submit_after_write(Box::new(move || {
        let expected = rx.total_bytes().saturating_add(response.len() as u64);
        dev.push_rx(&response);
        assert!(rx.wait_for_reader_bytes_for_test(expected, Duration::from_secs(5)));
    }));
    let (wait_started_tx, wait_started_rx) = crossbeam_channel::bounded(1);
    h.rx.set_snapshot_wait_enter_hook_for_test(Some(Arc::new(move || {
        wait_started_tx.send(()).expect("completion wait 已開始");
    })));
    let (decision_tx, decision_rx) = crossbeam_channel::bounded(1);
    let (release_decision_tx, release_decision_rx) = crossbeam_channel::bounded(1);
    h.rx
        .set_timeout_rescue_decision_hook_for_test(Some(Arc::new(move || {
            decision_tx.send(()).expect("timeout 已判定無 human");
            release_decision_rx
                .recv_timeout(Duration::from_secs(10))
                .expect("釋放 timeout rescue decision barrier");
        })));

    let tx_count = h.wal_tx_payloads().len();
    let id = h.submit_with(command, |request| {
        request.timeout_s = 30.0;
        request.expected_duration_s = Some(120.0);
    });
    wait_tx(&h, format!("{command}\n").as_bytes());
    wait_started_rx
        .recv_timeout(Duration::from_secs(5))
        .expect("command quiet deadline 已建立");
    i294_r10_advance_clock(&h, Duration::from_secs(31));
    decision_rx
        .recv_timeout(Duration::from_secs(5))
        .expect("無 human timeout 已完成救援決策，第一鍵尚未送出");

    let gate_available_at_decision = h.rx.timeout_rescue_gate_available_for_test();
    let submit_count_before = h.rx.human_submit_start_count_for_test();
    let tx_start = h.dev.tx_len();
    let (human_accepted_tx, human_accepted_rx) = crossbeam_channel::bounded(1);
    let (release_human_tx, release_human_rx) = crossbeam_channel::bounded(1);
    let human_hook_dev = h.dev.clone();
    h.dev.on_os_submit_after_write(Box::new(move || {
        if human_hook_dev.tx_since(tx_start).ends_with(b"echo HUMAN\n") {
            human_accepted_tx
                .send(())
                .expect("等待 human OS submit barrier");
            release_human_rx
                .recv_timeout(Duration::from_secs(10))
                .expect("釋放 human OS submit barrier");
        }
    }));

    let tx = h.tx_handle();
    let permit = i294_r10_human_line_permit(&h, 9_294_101);
    let submit_rx = Arc::clone(&h.rx);
    let (enqueued_tx, enqueued_rx) = crossbeam_channel::bounded(1);
    let (human_done_tx, human_done_rx) = crossbeam_channel::bounded(1);
    let human_submit = std::thread::spawn(move || {
        let (reply, report_rx) = crossbeam_channel::bounded(1);
        let job = sw_core::tx_writer::TxJob {
            permit,
            payload: b"echo HUMAN\n".to_vec(),
            reply,
            aggregate: None,
        };
        let queued = submit_rx.with_human_submit_start_for_test(|| {
            tx.jobs.send(job)
        });
        queued.expect("human submit 已排入 TX writer");
        enqueued_tx.send(()).expect("回報 human job 已排入 writer");
        report_rx
            .recv_timeout(Duration::from_secs(10))
            .expect("HumanLine writer report");
        human_done_tx.send(()).expect("回報 human TX 完成");
    });

    h.wait_for("human submit 抵達 timeout rescue gate", |rig| {
        rig.rx.human_submit_attempts_for_test() > 0
    });
    let submit_count_while_decision_paused = h.rx.human_submit_start_count_for_test();
    if gate_available_at_decision {
        enqueued_rx
            .recv_timeout(Duration::from_secs(5))
            .expect("未持有 rescue gate 時 human job 先排入 writer");
        human_accepted_rx
            .recv_timeout(Duration::from_secs(5))
            .expect("未持有 rescue gate 時 human bytes 先到 OS");
        release_human_tx
            .send(())
            .expect("釋放先送出的 human TX");
        human_done_rx
            .recv_timeout(Duration::from_secs(5))
            .expect("先送出的 human TX 已結算");
    }
    release_decision_tx
        .send(())
        .expect("timeout decision barrier 尚在等待");
    if !gate_available_at_decision {
        enqueued_rx
            .recv_timeout(Duration::from_secs(5))
            .expect("rescue key 排隊後 human job 才可進 writer");
        human_accepted_rx
            .recv_timeout(Duration::from_secs(5))
            .expect("第一個救援鍵後 human bytes 才到 OS");
        release_human_tx
            .send(())
            .expect("釋放 rescue key 後的 human TX");
        human_done_rx
            .recv_timeout(Duration::from_secs(5))
            .expect("rescue key 後的 human TX 已結算");
    }
    human_submit.join().expect("human submit worker 正常結束");
    let result = h.wait_done(&id);
    assert_eq!(result["status"], PyValue::from("done"), "{result:?}");
    assert_eq!(result["error_code"], PyValue::Null, "{result:?}");
    assert_eq!(result["partial"], PyValue::Bool(true), "{result:?}");
    assert_eq!(result["recovery_action"], PyValue::from("CTRL_C"), "{result:?}");
    assert!(
        !gate_available_at_decision,
        "timeout 無 human 判定至第一救援鍵入列期間必須持有同一 human submit gate"
    );
    assert_eq!(
        submit_count_while_decision_paused,
        submit_count_before,
        "decision 與第一救援鍵互斥期間，新 human submit 尚未開始"
    );
    let mut expected_tx = format!("{command}\n").into_bytes();
    expected_tx.extend_from_slice(b"\x03echo HUMAN\n");
    assert_eq!(wal_tx_bytes_since(&h, tx_count), expected_tx);
}

#[derive(Clone, Copy, Debug)]
enum I294R11HumanState {
    QueuedAtDecision,
    RegisteredBeforeDecision,
    FenceRejectedBeforeReport,
}

#[derive(Clone, Copy, Debug)]
enum I294R11TimeoutEntry {
    Silence,
    OperationDeadline,
}

struct I294R11HumanSubmission {
    completed_rx: crossbeam_channel::Receiver<sw_core::tx_writer::TxReport>,
    join: Option<std::thread::JoinHandle<()>>,
}

fn i294_r11_permit(
    h: &ActorRig,
    op_id: u64,
    class: sw_core::tx_writer::TxClass,
) -> sw_core::tx_writer::TxPermit {
    let (epoch, owner_gen, fsm_gen, input_epoch) = h.fence.with(|state| {
        (
            state.active_epoch.expect("ActorRig bridge 已開啟"),
            state.owner_gen,
            state.fsm_gen,
            state.input_epoch,
        )
    });
    sw_core::tx_writer::TxPermit {
        op_id: OpId(op_id),
        epoch,
        owner_gen,
        fsm_gen,
        class,
        source: "human:r11-test".into(),
        cmd_id: None,
        log: true,
        input_epoch: matches!(class, sw_core::tx_writer::TxClass::HumanRaw | sw_core::tx_writer::TxClass::HumanReplay)
            .then_some(input_epoch),
        replay_hold: None,
        probe_id: None,
    }
}

fn i294_r11_enqueue_human(
    h: &ActorRig,
    class: sw_core::tx_writer::TxClass,
    reply_capacity: usize,
    hold_until_report: bool,
) -> I294R11HumanSubmission {
    let tx = h.tx_handle();
    let permit = i294_r11_permit(h, 9_294_110, class);
    let (reply, report_rx) = crossbeam_channel::bounded(reply_capacity);
    let report_waiter = report_rx.clone();
    let (completed_tx, completed_rx) = crossbeam_channel::bounded(1);
    let (queued_tx, queued_rx) = crossbeam_channel::bounded(1);
    let rx = Arc::clone(&h.rx);
    let join = std::thread::spawn(move || {
        let report = rx.with_human_submit_start_for_test(|| {
            tx.jobs
                .send(sw_core::tx_writer::TxJob {
                    permit,
                    payload: b"echo HUMAN\n".to_vec(),
                    reply,
                    aggregate: None,
                })
                .expect("human job 已排入 TX writer");
            queued_tx.send(()).expect("回報 human job 已排入 writer");
            if hold_until_report {
                Some(report_waiter
                    .recv_timeout(Duration::from_secs(10))
                    .expect("Human TX writer report"))
            } else {
                None
            }
        });
        if let Some(report) = report {
            completed_tx.send(report).expect("回報 Human TX 結算");
        }
    });
    queued_rx
        .recv_timeout(Duration::from_secs(5))
        .expect("human submit 已開始並排入 writer");
    I294R11HumanSubmission {
        completed_rx,
        join: Some(join),
    }
}

fn i294_r11_enqueue_blocker(
    h: &ActorRig,
    op_id: u64,
) -> crossbeam_channel::Receiver<sw_core::tx_writer::TxReport> {
    let tx = h.tx_handle();
    let permit = i294_r11_permit(h, op_id, sw_core::tx_writer::TxClass::LeaseOwner);
    let (reply, report_rx) = crossbeam_channel::bounded(1);
    tx.jobs
        .send(sw_core::tx_writer::TxJob {
            permit,
            payload: b"\n".to_vec(),
            reply,
            aggregate: None,
        })
        .expect("writer blocker 已排入 TX queue");
    report_rx
}

fn i294_r11_run_timeout_case(
    human_state: I294R11HumanState,
    timeout_entry: I294R11TimeoutEntry,
    echo_found: bool,
) {
    let h = ActorRig::new(SHELL_PROFILE).ready();
    h.dev.on_tx_reply(b"\x03", b"^C\r\nroot@t:/# ");
    let command = "printf R11_HUMAN";
    let tx_count = h.wal_tx_payloads().len();
    let writes_before = h.dev.write_calls();
    let mut steps = vec![FakeWriteStep::BlockForever, FakeWriteStep::Accept(usize::MAX)];
    if matches!(human_state, I294R11HumanState::QueuedAtDecision) {
        steps.push(FakeWriteStep::BlockForever);
    }
    h.dev.script_write(steps);
    let initial_blocker_report = i294_r11_enqueue_blocker(&h, 9_294_109);
    h.dev.wait_blocked();

    let response = if echo_found {
        format!("{command}\r\npartial output\r\n")
    } else {
        "partial output\r\n".to_string()
    };
    let rx_before_command = h.rx.total_bytes();
    h.dev
        .on_tx_reply(format!("{command}\n").as_bytes(), response.as_bytes());
    let (command_write_tx, command_write_rx) = crossbeam_channel::bounded(1);
    let (release_command_tx, release_command_rx) = crossbeam_channel::bounded(1);
    let command_hook_called = Arc::new(std::sync::atomic::AtomicBool::new(false));
    let command_hook_called_once = Arc::clone(&command_hook_called);
    h.dev.on_os_submit_after_write(Box::new(move || {
        if command_hook_called_once.swap(true, std::sync::atomic::Ordering::AcqRel) {
            return;
        }
        command_write_tx
            .send(())
            .expect("command OS submit 已完成");
        release_command_rx
            .recv_timeout(Duration::from_secs(10))
            .expect("測試釋放 command OS submit barrier");
    }));

    let (wait_entered_tx, wait_entered_rx) = crossbeam_channel::bounded(1);
    h.rx
        .set_snapshot_wait_enter_hook_for_test(Some(Arc::new(move || {
            wait_entered_tx.send(()).expect("completion waiter 已開始");
        })));

    let (report_paused_tx, report_paused_rx) = crossbeam_channel::bounded(1);
    let (release_report_tx, release_report_rx) = crossbeam_channel::bounded(1);
    if matches!(human_state, I294R11HumanState::FenceRejectedBeforeReport) {
        h.rx
            .set_human_tx_report_hook_for_test(Some(Arc::new(move || {
                report_paused_tx
                    .send(())
                    .expect("human rejection report 到達 barrier");
                release_report_rx
                    .recv_timeout(Duration::from_secs(10))
                    .expect("測試釋放 human rejection report");
            })));
    }

    let id = h.submit_with(command, |request| match timeout_entry {
        I294R11TimeoutEntry::Silence => {
            request.timeout_s = 30.0;
            request.expected_duration_s = Some(120.0);
        }
        I294R11TimeoutEntry::OperationDeadline => request.timeout_s = 0.2,
    });
    h.wait_for("command job 已排在 writer 前方", |rig| {
        rig.tx_handle().jobs.len() == 1
    });

    let blocker_report_rx = if matches!(human_state, I294R11HumanState::QueuedAtDecision) {
        Some(i294_r11_enqueue_blocker(&h, 9_294_113))
    } else {
        None
    };
    let human_class = if matches!(human_state, I294R11HumanState::FenceRejectedBeforeReport) {
        sw_core::tx_writer::TxClass::HumanRaw
    } else {
        sw_core::tx_writer::TxClass::HumanLine
    };
    let hold_until_report = true;
    let report_capacity = if matches!(human_state, I294R11HumanState::FenceRejectedBeforeReport) {
        0
    } else {
        1
    };
    let human = i294_r11_enqueue_human(&h, human_class, report_capacity, hold_until_report);
    let submitted_count = h.rx.human_submit_start_count_for_test();
    assert_eq!(submitted_count, 1, "human submit 在 command OS submit 前開始");
    assert_eq!(h.rx.human_in_flight_for_test(), 1, "writer report 前 human job 保持在途");

    if matches!(human_state, I294R11HumanState::FenceRejectedBeforeReport) {
        let permit = i294_r11_permit(
            &h,
            9_294_112,
            sw_core::tx_writer::TxClass::HumanRaw,
        );
        assert_eq!(
            h.tx_handle().fence_verdict(&permit),
            Err(sw_core::tx_writer::FenceReject::HumanAdmissionClosed),
            "actor foreground 已關閉 HumanRaw admission；排隊 permit 會由 writer fence 拒絕"
        );
    }

    h.dev.release_block();
    initial_blocker_report
        .recv_timeout(Duration::from_secs(5))
        .expect("初始 writer barrier 已收斂");
    command_write_rx
        .recv_timeout(Duration::from_secs(5))
        .expect("command 已到 OS submit 後置 barrier");
    assert!(h.rx.wait_for_reader_bytes_for_test(
        rx_before_command + response.len() as u64,
        Duration::from_secs(5),
    ));
    release_command_tx
        .send(())
        .expect("釋放 command OS submit barrier");
    wait_tx(&h, format!("{command}\n").as_bytes());
    if matches!(human_state, I294R11HumanState::QueuedAtDecision) {
        h.dev.wait_blocked();
        assert_eq!(
            h.dev.write_calls(),
            writes_before + 3,
            "writer 正停在 command 後的 blocker，human job 仍留在佇列"
        );
    }
    if matches!(human_state, I294R11HumanState::RegisteredBeforeDecision) {
        let report = human
            .completed_rx
            .recv_timeout(Duration::from_secs(5))
            .expect("human TX 已完成 observer 登錄與 writer report");
        assert_eq!(report.result.outcome, sw_pal::TxOutcome::Completed(b"echo HUMAN\n".len()));
        assert!(report.reject.is_none());
        assert!(h.rx.human_tx_state().generation > 0);
        assert!(!h.rx.snapshot_from(0).tx_submit_pending);
        assert_eq!(h.rx.human_in_flight_for_test(), 0, "writer report 回來後 guard 已釋放");
    }
    if matches!(human_state, I294R11HumanState::FenceRejectedBeforeReport) {
        report_paused_rx
            .recv_timeout(Duration::from_secs(5))
            .expect("writer 已拒絕 human permit，report 尚未回傳 submitter");
        assert_eq!(h.rx.human_tx_state().generation, 0);
        assert!(!h.rx.snapshot_from(0).tx_submit_pending);
        assert_eq!(h.rx.human_in_flight_for_test(), 1, "拒絕 report 尚未回傳時仍在途");
    }

    wait_entered_rx
        .recv_timeout(Duration::from_secs(5))
        .expect("命令 TX 後 completion waiter 已建立");
    match timeout_entry {
        I294R11TimeoutEntry::Silence => i294_r10_advance_clock(&h, Duration::from_secs(31)),
        I294R11TimeoutEntry::OperationDeadline => {
            i294_r10_advance_clock(&h, Duration::from_millis(201))
        }
    }

    let decision_deadline = Instant::now() + Duration::from_secs(5);
    loop {
        if is_done(&h, &id) || !h.rx.timeout_rescue_gate_available_for_test() {
            break;
        }
        assert!(Instant::now() < decision_deadline, "timeout decision 未抵達 human／rescue 分流");
        std::thread::sleep(Duration::from_millis(2));
    }
    if !matches!(human_state, I294R11HumanState::RegisteredBeforeDecision) {
        assert_eq!(h.rx.human_in_flight_for_test(), 1, "timeout 決策時 human report 尚未回來");
    }

    if let Some(blocker_report) = blocker_report_rx.as_ref() {
        h.dev.release_block();
        blocker_report
            .recv_timeout(Duration::from_secs(5))
            .expect("writer blocker 已收斂");
    }
    if matches!(human_state, I294R11HumanState::FenceRejectedBeforeReport) {
        release_report_tx
            .send(())
            .expect("釋放 fence rejection report barrier");
        h.rx.set_human_tx_report_hook_for_test(None);
    }
    if !matches!(human_state, I294R11HumanState::RegisteredBeforeDecision) {
        let report = human
            .completed_rx
            .recv_timeout(Duration::from_secs(5))
            .expect("human writer report 已回來且 in-flight guard 已釋放");
        match human_state {
            I294R11HumanState::FenceRejectedBeforeReport => assert_eq!(
                report.reject,
                Some(sw_core::tx_writer::FenceReject::HumanAdmissionClosed)
            ),
            I294R11HumanState::QueuedAtDecision => {
                assert!(report.reject.is_none());
                assert_eq!(
                    report.result.outcome,
                    sw_pal::TxOutcome::Completed(b"echo HUMAN\n".len())
                );
            }
            I294R11HumanState::RegisteredBeforeDecision => unreachable!(),
        }
        assert_eq!(h.rx.human_in_flight_for_test(), 0, "writer report 回來後 in-flight 清零");
    }
    if let Some(join) = human.join {
        join.join().expect("human submit worker 正常結束");
    }

    let result = h.wait_done(&id);
    let expected_stdout = if echo_found {
        format!("{command}\npartial output")
    } else {
        "partial output\n".to_string()
    };
    assert_eq!(result["status"], PyValue::from("error"), "{result:?}");
    assert_eq!(result["error_code"], PyValue::from("PROMPT_TIMEOUT"), "{result:?}");
    assert_eq!(result["partial"], PyValue::Bool(true), "{result:?}");
    assert_eq!(result["recovery_action"], PyValue::from("NONE"), "{result:?}");
    assert_eq!(result["stdout"], PyValue::from(expected_stdout), "{result:?}");
    assert_eq!(h.get_state()["state"], PyValue::from("READY"));

    let tx_bytes = wal_tx_bytes_since(&h, tx_count);
    assert!(!tx_bytes.contains(&0x03) && !tx_bytes.contains(&0x04), "timeout human route has zero rescue TX: {tx_bytes:?}");
}

fn i294_r11_run_matrix(human_state: I294R11HumanState) {
    for timeout_entry in [
        I294R11TimeoutEntry::Silence,
        I294R11TimeoutEntry::OperationDeadline,
    ] {
        for echo_found in [true, false] {
            i294_r11_run_timeout_case(human_state, timeout_entry, echo_found);
        }
    }
}

#[test]
fn i294_r11_human_job_queued_before_command_submit_suppresses_rescue() {
    i294_r11_run_matrix(I294R11HumanState::QueuedAtDecision);
}

#[test]
fn i294_r11_human_tx_registered_before_timeout_decision_suppresses_rescue() {
    i294_r11_run_matrix(I294R11HumanState::RegisteredBeforeDecision);
}

#[test]
fn i294_r11_fence_rejected_human_job_before_report_suppresses_rescue() {
    i294_r11_run_matrix(I294R11HumanState::FenceRejectedBeforeReport);
}

#[derive(Clone, Copy)]
enum I294R12PumpPause {
    BeforeTake,
    AfterTake,
}

#[derive(Clone, Copy)]
enum I294R12TimeoutEntry {
    Silence,
    OperationDeadline,
}

fn i294_r12_run_pump_timeout_case(
    pump_pause: I294R12PumpPause,
    timeout_entry: I294R12TimeoutEntry,
    echo_found: bool,
) {
    let h = ActorRig::new_with_hub(SHELL_PROFILE).ready();
    h.dev.on_tx_reply(b"\x03", b"^C\r\nroot@t:/# ");
    let (pump_paused_tx, pump_paused_rx) = crossbeam_channel::bounded(1);
    let (release_pump_tx, release_pump_rx) = crossbeam_channel::bounded(1);
    let pause = Arc::new(move || {
        pump_paused_tx
            .send(())
            .expect("human pump 到達指定 pause point");
        let _ = release_pump_rx.recv_timeout(Duration::from_secs(10));
    });
    match pump_pause {
        I294R12PumpPause::BeforeTake => {
            h.rx.set_human_pump_before_take_hook_for_test(Some(pause));
        }
        I294R12PumpPause::AfterTake => {
            h.rx.set_human_pump_after_take_hook_for_test(Some(pause));
        }
    }

    let console = h.attach_pty_console("i294-r12-pump");
    let submitted_before = h.rx.human_pump_submissions_for_test();
    console.push_input(b"echo HUMAN\n");
    pump_paused_rx
        .recv_timeout(Duration::from_secs(5))
        .expect("pump 到達受控 pause point");
    h.wait_for("human job 已排入 pump channel", |rig| {
        rig.rx.human_pump_submissions_for_test() > submitted_before
    });
    let in_flight_at_pause = h.rx.human_in_flight_for_test();

    let command = "printf R12_PUMP_HUMAN";
    let response = if echo_found {
        format!("{command}\r\npartial output\r\n")
    } else {
        "partial output\r\n".to_string()
    };
    h.dev
        .on_tx_reply(format!("{command}\n").as_bytes(), response.as_bytes());
    let (deadline_started_tx, deadline_started_rx) = crossbeam_channel::bounded(1);
    h.rx
        .set_snapshot_wait_enter_hook_for_test(Some(Arc::new(move || {
            deadline_started_tx
                .send(())
                .expect("completion waiter 已建立 operation deadline");
        })));
    let tx_count = h.wal_tx_payloads().len();
    let id = h.submit_with(command, |request| match timeout_entry {
        I294R12TimeoutEntry::Silence => {
            request.timeout_s = 2.0;
            request.expected_duration_s = Some(120.0);
        }
        I294R12TimeoutEntry::OperationDeadline => request.timeout_s = 0.2,
    });
    wait_tx(&h, format!("{command}\n").as_bytes());
    deadline_started_rx
        .recv_timeout(Duration::from_secs(5))
        .expect("operation deadline 已建立，現在才推進假時鐘");
    match timeout_entry {
        I294R12TimeoutEntry::Silence => h.advance(Duration::from_secs(3)),
        I294R12TimeoutEntry::OperationDeadline => h.advance(Duration::from_millis(201)),
    }

    let result = h.wait_done(&id);
    let expected_stdout = if echo_found {
        format!("{command}\npartial output")
    } else {
        "partial output\n".to_string()
    };
    assert_eq!(result["status"], PyValue::from("error"), "{result:?}");
    assert_eq!(result["error_code"], PyValue::from("PROMPT_TIMEOUT"), "{result:?}");
    assert_eq!(result["partial"], PyValue::Bool(true), "{result:?}");
    assert_eq!(result["recovery_action"], PyValue::from("NONE"), "{result:?}");
    assert_eq!(result["stdout"], PyValue::from(expected_stdout), "{result:?}");
    assert_eq!(
        in_flight_at_pause, 1,
        "actor lane 已產生 HumanTx effect 且 job 位於 pump channel／pump local 時，guard 必須已可見"
    );
    assert_eq!(
        wal_tx_bytes_since(&h, tx_count),
        format!("{command}\n").as_bytes(),
        "human job 在途時 timeout 不可送救援 TX"
    );

    release_pump_tx.send(()).expect("釋放 human pump");
    h.rx.set_human_pump_before_take_hook_for_test(None);
    h.rx.set_human_pump_after_take_hook_for_test(None);
    h.wait_for("human writer report 收尾後 guard 清零", |rig| {
        rig.rx.human_in_flight_for_test() == 0
    });
    assert_eq!(h.rx.human_in_flight_for_test(), 0, "pump report 後不可洩漏 guard");
}

fn i294_r12_run_pump_timeout_matrix(pump_pause: I294R12PumpPause) {
    for timeout_entry in [
        I294R12TimeoutEntry::Silence,
        I294R12TimeoutEntry::OperationDeadline,
    ] {
        for echo_found in [true, false] {
            i294_r12_run_pump_timeout_case(pump_pause, timeout_entry, echo_found);
        }
    }
}

#[test]
fn i294_r12_human_job_guarded_before_pump_take_suppresses_rescue() {
    i294_r12_run_pump_timeout_matrix(I294R12PumpPause::BeforeTake);
}

#[test]
fn i294_r12_human_job_guarded_after_pump_take_suppresses_rescue() {
    i294_r12_run_pump_timeout_matrix(I294R12PumpPause::AfterTake);
}

#[test]
fn i294_r13_actor_transition_effect_barrier_holds_unstarted_guard() {
    let h = ActorRig::new_with_hub(SHELL_PROFILE).ready();
    let console = h.attach_pty_console("i294-r13-actor-barrier");
    let starts_before = h.rx.human_submit_start_count_for_test();
    let (entered_tx, entered_rx) = crossbeam_channel::bounded(1);
    let (release_tx, release_rx) = crossbeam_channel::bounded(1);
    h.rx
        .set_human_tx_before_effect_hook_for_test(Some(Arc::new(move || {
            entered_tx
                .send(())
                .expect("actor transition 已完成並產生 HumanTx effect");
            release_rx
                .recv_timeout(Duration::from_secs(10))
                .expect("測試釋放 transition/effect 屏障");
        })));

    console.push_input(b"echo HUMAN\n");
    entered_rx
        .recv_timeout(Duration::from_secs(5))
        .expect("executor 在處理 effect 前進入屏障");
    assert_eq!(h.rx.human_in_flight_for_test(), 1);
    assert_eq!(
        h.rx.human_submit_start_count_for_test(),
        starts_before,
        "transition guard 在 HumanTx effect 執行前不增加 submit 歷史"
    );

    release_tx.send(()).expect("actor executor 仍在屏障上");
    wait_tx(&h, b"echo HUMAN\n");
    h.wait_for("HumanTx report 後 in-flight guard 清理", |rig| {
        rig.rx.human_in_flight_for_test() == 0
    });
    assert_eq!(
        h.rx.human_submit_start_count_for_test(),
        starts_before + 1,
        "只有 effect executor 接手 job 後才增加 submit 歷史"
    );
}

#[test]
fn i294_r13_transition_to_effect_barrier_guards_both_timeout_entries() {
    for timeout_entry in [
        I294R12TimeoutEntry::Silence,
        I294R12TimeoutEntry::OperationDeadline,
    ] {
        for echo_found in [true, false] {
            let h = ActorRig::new(SHELL_PROFILE).ready();
            let command = "printf R13_HUMAN";
            let response = if echo_found {
                format!("{command}\r\npartial output\r\n")
            } else {
                "partial output\r\n".to_string()
            };
            h.dev
                .on_tx_reply(format!("{command}\n").as_bytes(), response.as_bytes());

            let (wait_started_tx, wait_started_rx) = crossbeam_channel::bounded(1);
            h.rx
                .set_snapshot_wait_enter_hook_for_test(Some(Arc::new(move || {
                    wait_started_tx
                        .send(())
                        .expect("command completion waiter 已開始");
                })));
            let tx_count = h.wal_tx_payloads().len();
            let starts_before = h.rx.human_submit_start_count_for_test();
            let id = h.submit_with(command, |request| match timeout_entry {
                I294R12TimeoutEntry::Silence => {
                    request.timeout_s = 2.0;
                    request.expected_duration_s = Some(120.0);
                }
                I294R12TimeoutEntry::OperationDeadline => request.timeout_s = 0.2,
            });
            wait_tx(&h, format!("{command}\n").as_bytes());
            wait_started_rx
                .recv_timeout(Duration::from_secs(5))
                .expect("推進假時鐘前 completion waiter 已進入");
            h.wait_for("命令已收到無 prompt 的部分輸出", |rig| {
                rig.rx.text_from(0).contains("partial output")
            });

            // 這個 guard 模擬 actor transition 已完成、Effect::HumanTx 尚未執行的窗口。
            // 讓兩種操作 timeout 分流都在 effect barrier 期間讀取 guard，結果結算後再釋放它。
            let transition_guard = h.rx.begin_human_submit_before_actor_step();
            assert_eq!(h.rx.human_in_flight_for_test(), 1);
            let (decision_entered_tx, decision_entered_rx) = crossbeam_channel::bounded(1);
            let (release_decision_tx, release_decision_rx) = crossbeam_channel::bounded(1);
            h.rx
                .set_human_timeout_decision_hook_for_test(Some(Arc::new(move || {
                    decision_entered_tx
                        .send(())
                        .expect("timeout 已把本窗口分類為 human 交錯");
                    release_decision_rx
                        .recv_timeout(Duration::from_secs(10))
                        .expect("測試釋放 timeout 分流屏障");
                })));

            assert_eq!(
                h.rx.human_submit_start_count_for_test(),
                starts_before,
                "transition 前 guard 不可提前增加實際 submit 歷史"
            );

            match timeout_entry {
                I294R12TimeoutEntry::Silence => h.clock.advance(Duration::from_secs(3)),
                I294R12TimeoutEntry::OperationDeadline => {
                    h.clock.advance(Duration::from_millis(201))
                }
            }
            decision_entered_rx
                .recv_timeout(Duration::from_secs(5))
                .expect("逾時入口在 effect 執行前讀到 transition guard");
            assert_eq!(h.rx.human_in_flight_for_test(), 1);
            assert_eq!(
                wal_tx_bytes_since(&h, tx_count),
                format!("{command}\n").as_bytes(),
                "timeout 分流完成前不得送出救援鍵"
            );

            release_decision_tx
                .send(())
                .expect("timeout 決策仍停在受控屏障");
            let result = h.wait_done(&id);
            let expected_stdout = if echo_found {
                format!("{command}\npartial output")
            } else {
                "partial output\n".to_string()
            };
            assert_eq!(result["status"], PyValue::from("error"), "{result:?}");
            assert_eq!(result["error_code"], PyValue::from("PROMPT_TIMEOUT"), "{result:?}");
            assert_eq!(result["partial"], PyValue::Bool(true), "{result:?}");
            assert_eq!(result["recovery_action"], PyValue::from("NONE"), "{result:?}");
            assert_eq!(result["stdout"], PyValue::from(expected_stdout), "{result:?}");
            assert_eq!(
                h.rx.human_submit_start_count_for_test(),
                starts_before,
                "effect 尚未執行時，timeout 分流不留下 submit 歷史"
            );
            assert_eq!(
                wal_tx_bytes_since(&h, tx_count),
                format!("{command}\n").as_bytes(),
                "echo 有無與 timeout 入口均不得增加救援 TX"
            );
            drop(transition_guard);
            h.wait_for("HumanTx report 後 transition guard 清理", |rig| {
                rig.rx.human_in_flight_for_test() == 0
            });
        }
    }
}

fn i294_r13_wait_without_actor_rpc(
    h: &ActorRig,
    what: &str,
    mut condition: impl FnMut(&ActorRig) -> bool,
) {
    let deadline = Instant::now() + Duration::from_secs(5);
    loop {
        if condition(h) {
            return;
        }
        assert!(Instant::now() < deadline, "等待逾時：{what}");
        std::thread::sleep(Duration::from_millis(2));
    }
}

#[test]
fn i294_r13_actor_effect_barrier_covers_timeout_entries_and_echo_states() {
    use sw_core::arbiter::{CommandRegistry, QueuedCommand};
    use sw_core::op_runner::{OpCtx, OpRunner, OpSpec};
    use sw_core::session_core::OpGrant;
    use sw_core::wal::{WalConfig, WalWriter};
    use sw_pal::fakes::wal_fs::FakeWalFs;
    use std::path::PathBuf;

    for (case_index, timeout_entry) in [
        I294R12TimeoutEntry::Silence,
        I294R12TimeoutEntry::OperationDeadline,
    ]
    .into_iter()
    .enumerate()
    {
        for echo_found in [true, false] {
            let h = ActorRig::new_with_hub(SHELL_PROFILE).ready();
            let command = "printf R13_EFFECT";
            let response = if echo_found {
                format!("{command}\r\npartial output\r\n")
            } else {
                "partial output\r\n".to_string()
            };
            h.dev
                .on_tx_reply(format!("{command}\n").as_bytes(), response.as_bytes());
            let console = h.attach_pty_console("i294-r13-real-effect-barrier");
            let starts_before = h.rx.human_submit_start_count_for_test();
            let tx_count = h.wal_tx_payloads().len();
            let (effect_entered_tx, effect_entered_rx) = crossbeam_channel::bounded(1);
            let (release_effect_tx, release_effect_rx) = crossbeam_channel::bounded(1);
            h.rx
                .set_human_tx_before_effect_hook_for_test(Some(Arc::new(move || {
                    effect_entered_tx
                        .send(())
                        .expect("actor transition 已產生 HumanTx effect");
                    release_effect_rx
                        .recv_timeout(Duration::from_secs(10))
                        .expect("測試釋放 actor transition/effect 屏障");
                })));
            console.push_input(b"echo HUMAN\n");
            effect_entered_rx
                .recv_timeout(Duration::from_secs(5))
                .expect("actor transition 完成、effect executor 尚未接手");
            assert_eq!(h.rx.human_in_flight_for_test(), 1);
            assert_eq!(h.rx.human_submit_start_count_for_test(), starts_before);

            let (wait_started_tx, wait_started_rx) = crossbeam_channel::bounded(1);
            h.rx
                .set_snapshot_wait_enter_hook_for_test(Some(Arc::new(move || {
                    wait_started_tx
                        .send(())
                        .expect("獨立 OpRunner 已開始 command completion wait");
                })));
            let op_wal = Arc::new(WalWriter::open(
                WalConfig {
                    dir: PathBuf::from(format!("/fake/i294-r13-op-{case_index}-{echo_found}")),
                    rotate_bytes: 67_108_864,
                },
                FakeWalFs::new(),
                h.clock.clone(),
            ));
            let (op_tx, op_rx) = crossbeam_channel::bounded(1);
            let runner = OpRunner::spawn(OpCtx {
                com: "COM0".into(),
                sid: String::new(),
                profile: profile_of(SHELL_PROFILE),
                rx: h.rx.clone(),
                tx: h.tx_handle(),
                wal: op_wal,
                clock: h.clock.clone(),
                control: op_tx,
                rng: Arc::new(|buf: &mut [u8]| {
                    for (index, byte) in buf.iter_mut().enumerate() {
                        *byte = index as u8;
                    }
                }),
                registry: CommandRegistry::new(),
            });
            let cmd_id = format!("i294-r13-direct-{case_index}-{echo_found}");
            let mut request = agent_submit(command);
            match timeout_entry {
                I294R12TimeoutEntry::Silence => {
                    request.timeout_s = 2.0;
                    request.expected_duration_s = Some(120.0);
                }
                I294R12TimeoutEntry::OperationDeadline => request.timeout_s = 0.2,
            }
            let (epoch, owner_gen, fsm_gen) = h.fence.with(|state| {
                (
                    state.active_epoch.expect("ActorRig 已 READY"),
                    state.owner_gen,
                    state.fsm_gen,
                )
            });
            runner.run(
                OpGrant {
                    op_id: OpId(0x294_1300 + (case_index * 2 + usize::from(!echo_found)) as u64),
                    epoch,
                    owner_gen,
                    fsm_gen,
                    cmd_id: Some(cmd_id.clone()),
                },
                OpSpec::Command(QueuedCommand {
                    sort_key: (0, 0),
                    cmd_id,
                    req: request,
                }),
            );
            i294_r13_wait_without_actor_rpc(&h, "獨立 OpRunner command TX", |rig| {
                wal_tx_has(rig, format!("{command}\n").as_bytes())
            });
            wait_started_rx
                .recv_timeout(Duration::from_secs(5))
                .expect("OpRunner 已進入 completion wait");
            i294_r13_wait_without_actor_rpc(&h, "OpRunner 已收到無 prompt 的部分輸出", |rig| {
                rig.rx.text_from(0).contains("partial output")
            });

            let (decision_entered_tx, decision_entered_rx) = crossbeam_channel::bounded(1);
            let (release_decision_tx, release_decision_rx) = crossbeam_channel::bounded(1);
            h.rx
                .set_human_timeout_decision_hook_for_test(Some(Arc::new(move || {
                    decision_entered_tx
                        .send(())
                        .expect("timeout 分流已讀取真 actor transition guard");
                    release_decision_rx
                        .recv_timeout(Duration::from_secs(10))
                        .expect("測試釋放 timeout 決策屏障");
                })));
            match timeout_entry {
                I294R12TimeoutEntry::Silence => h.clock.advance(Duration::from_secs(3)),
                I294R12TimeoutEntry::OperationDeadline => {
                    h.clock.advance(Duration::from_millis(201))
                }
            }
            decision_entered_rx
                .recv_timeout(Duration::from_secs(5))
                .expect("兩種 timeout 入口都在 HumanTx effect 執行前觀察到 guard");
            assert_eq!(h.rx.human_in_flight_for_test(), 1);
            assert_eq!(h.rx.human_submit_start_count_for_test(), starts_before);
            assert_eq!(
                wal_tx_bytes_since(&h, tx_count),
                format!("{command}\n").as_bytes(),
                "effect barrier 期間不得有 human 或救援 TX"
            );

            release_decision_tx
                .send(())
                .expect("timeout 決策仍在屏障上");
            let finished = op_rx
                .recv_timeout(Duration::from_secs(5))
                .expect("OpRunner 已回報 timeout 結果");
            if let ControlEvent::OpFinished { outcome, .. } = finished {
                if let OpOutcome::CompletionUncertain { stdout } = outcome {
                    let expected_stdout = if echo_found {
                        format!("{command}\npartial output")
                    } else {
                        "partial output\n".to_string()
                    };
                    assert_eq!(stdout, expected_stdout);
                } else {
                    panic!("human guard 下應回 CompletionUncertain");
                }
            } else {
                panic!("OpRunner 應回報 OpFinished");
            }

            release_effect_tx
                .send(())
                .expect("actor effect executor 仍停在屏障");
            wait_tx(&h, b"echo HUMAN\n");
            h.wait_for("HumanTx report 後 transition guard 清理", |rig| {
                rig.rx.human_in_flight_for_test() == 0
            });
            assert_eq!(
                h.rx.human_submit_start_count_for_test(),
                starts_before + 1,
                "只在 HumanTx effect 執行後建立 submit 歷史"
            );
        }
    }
}

fn i294_r9_run_deadline_timeout_full_echo_with_human(pending_submit: bool) {
    let h = ActorRig::new(SHELL_PROFILE).ready();
    h.dev.on_tx_reply(b"\x03", b"^C\r\nroot@t:/# ");
    let (deadline_started_tx, deadline_started_rx) = crossbeam_channel::bounded(1);
    h.rx
        .set_snapshot_wait_enter_hook_for_test(Some(Arc::new(move || {
            deadline_started_tx
                .send(())
                .expect("completion waiter 已建立 operation deadline");
        })));
    let tx_count = h.wal_tx_payloads().len();
    let id = h.submit_with("printf R9_DEADLINE", |request| request.timeout_s = 0.2);
    wait_tx(&h, b"printf R9_DEADLINE\n");
    deadline_started_rx
        .recv_timeout(Duration::from_secs(5))
        .expect("推進假時鐘前 operation deadline 已建立");
    if pending_submit {
        h.rx.set_tx_submit_pending_for_test(true);
    } else {
        h.rx.record_human_tx_for_test(b"echo HUMAN\n", &*h.clock);
        assert!(h.rx.human_tx_state().generation > 0);
    }
    let raw = b"printf R9_DEADLINE\r\npartial output\r\n";
    if pending_submit {
        let before = h.rx.total_bytes();
        h.dev.push_rx(raw);
        assert!(h.rx.wait_for_reader_bytes_for_test(
            before + raw.len() as u64,
            Duration::from_secs(5),
        ));
    } else {
        h.inject_rx_processed(raw);
    }
    h.advance(Duration::from_millis(201));
    let result = h.wait_done(&id);
    if pending_submit {
        h.rx.set_tx_submit_pending_for_test(false);
    }
    assert_eq!(result["status"], PyValue::from("error"), "{result:?}");
    assert_eq!(result["error_code"], PyValue::from("PROMPT_TIMEOUT"), "{result:?}");
    assert_eq!(result["partial"], PyValue::Bool(true), "{result:?}");
    assert_eq!(result["recovery_action"], PyValue::from("NONE"), "{result:?}");
    assert_eq!(result["stdout"], PyValue::from("printf R9_DEADLINE\npartial output"), "{result:?}");
    assert_eq!(wal_tx_bytes_since(&h, tx_count), b"printf R9_DEADLINE\n");
}

#[test]
fn i294_r9_deadline_timeout_full_echo_with_human_tx_suppresses_recovery() {
    i294_r9_run_deadline_timeout_full_echo_with_human(false);
}

#[test]
fn i294_r9_deadline_timeout_full_echo_with_pending_submit_suppresses_recovery() {
    i294_r9_run_deadline_timeout_full_echo_with_human(true);
}

fn i294_r9_assert_stale_no_echo_completion_rechecked(pending_submit: bool) {
    let h = ActorRig::new(SHELL_PROFILE).ready();
    let (enter_tx, enter_rx) = crossbeam_channel::bounded(1);
    let (release_enter_tx, release_enter_rx) = crossbeam_channel::bounded(1);
    h.rx
        .set_snapshot_wait_enter_hook_for_test(Some(Arc::new(move || {
            enter_tx.send(()).expect("等待第一次 snapshot");
            release_enter_rx.recv().expect("釋放第一次 snapshot");
        })));
    let (evaluated_tx, evaluated_rx) = crossbeam_channel::bounded(1);
    let (release_evaluated_tx, release_evaluated_rx) = crossbeam_channel::bounded(1);
    h.rx
        .set_snapshot_wait_evaluated_hook_for_test(Some(Arc::new(move |snapshot| {
            evaluated_tx
                .send(snapshot.clone())
                .expect("等待 evaluator 返回 Complete 的 snapshot");
            release_evaluated_rx
                .recv()
                .expect("釋放成功判定線性化 barrier");
        })));

    let tx_count = h.wal_tx_payloads().len();
    let initial_rx = h.rx.total_bytes();
    let id = h.submit_with("printf R9_STALE", |request| request.timeout_s = 0.2);
    wait_tx(&h, b"printf R9_STALE\n");
    enter_rx
        .recv_timeout(Duration::from_secs(5))
        .expect("等待器已進入 snapshot barrier");
    let prompt = b"root@t:/# ";
    h.dev.push_rx(prompt);
    assert!(h.rx.wait_for_reader_bytes_for_test(
        initial_rx + prompt.len() as u64,
        Duration::from_secs(5),
    ));
    release_enter_tx.send(()).expect("釋放 snapshot 建立");
    let stale = evaluated_rx
        .recv_timeout(Duration::from_secs(5))
        .expect("舊 snapshot 已由無 echo 相容路徑判定 Complete");
    assert_eq!(stale.text, "root@t:/# ");

    if pending_submit {
        h.rx.set_tx_submit_pending_for_test(true);
    } else {
        h.rx.record_human_tx_for_test(b"echo HUMAN\n", &*h.clock);
        assert!(h.rx.human_tx_state().generation > 0);
    }
    h.advance(Duration::from_millis(201));
    release_evaluated_tx
        .send(())
        .expect("釋放 evaluator 與 RX 鎖之間的 barrier");

    let result = h.wait_done(&id);
    if pending_submit {
        h.rx.set_tx_submit_pending_for_test(false);
    }
    assert_eq!(result["status"], PyValue::from("error"), "{result:?}");
    assert_eq!(result["error_code"], PyValue::from("PROMPT_TIMEOUT"), "{result:?}");
    assert_eq!(result["partial"], PyValue::Bool(true), "{result:?}");
    assert_eq!(result["recovery_action"], PyValue::from("NONE"), "{result:?}");
    assert_eq!(result["stdout"], PyValue::from(""), "逾時 stdout 必須取自重新結算的 snapshot：{result:?}");
    assert_eq!(wal_tx_bytes_since(&h, tx_count), b"printf R9_STALE\n");
    assert_eq!(h.get_state()["state"], PyValue::from("READY"));
}

#[test]
fn i294_r9_stale_no_echo_completion_rechecked_after_human_tx() {
    i294_r9_assert_stale_no_echo_completion_rechecked(false);
}

#[test]
fn i294_r9_stale_no_echo_completion_rechecked_after_pending_submit() {
    i294_r9_assert_stale_no_echo_completion_rechecked(true);
}

fn i294_r7_assert_long_prompt_prefix_echo(human_tx: bool) {
    let h = ActorRig::new(SHELL_PROFILE).ready();
    let tx_count = h.wal_tx_payloads().len();
    let (deadline_started_tx, deadline_started_rx) = crossbeam_channel::bounded(1);
    h.rx
        .set_snapshot_wait_enter_hook_for_test(Some(Arc::new(move || {
            deadline_started_tx
                .send(())
                .expect("completion waiter 已建立 operation deadline");
        })));
    let id = h.submit_with("echo x", |request| request.timeout_s = 0.2);
    wait_tx(&h, b"echo x\n");
    deadline_started_rx
        .recv_timeout(Duration::from_secs(5))
        .expect("推進假時鐘前 operation deadline 已建立");

    if human_tx {
        h.rx.record_human_tx_for_test(b"echo HUMAN\n", &*h.clock);
        h.inject_rx_reader_processed(b"echo HUMAN\r\nHUMAN\r\nroot@t:/# ");
    }

    let raw = format!(
        "root@t:/{}# echo x\r\nx\r\nroot@t:/# ",
        "e".repeat(2_000)
    );
    h.inject_rx_processed(raw.as_bytes());
    h.advance(Duration::from_millis(200));

    let result = h.wait_done(&id);
    assert_eq!(result["status"], PyValue::from("done"), "human_tx={human_tx}: {result:?}");
    assert_eq!(result["stdout"], PyValue::from("x"), "human_tx={human_tx}: {result:?}");
    assert_eq!(result["partial"], PyValue::Bool(false), "human_tx={human_tx}: {result:?}");
    assert_eq!(wal_tx_bytes_since(&h, tx_count), b"echo x\n".to_vec(), "不得額外 TX 或重送");
}

#[test]
fn i294_r7_long_prompt_prefix_echo_completes_without_human_tx() {
    i294_r7_assert_long_prompt_prefix_echo(false);
}

#[test]
fn i294_r7_long_prompt_prefix_echo_completes_with_human_tx() {
    i294_r7_assert_long_prompt_prefix_echo(true);
}

#[test]
fn i294_completion_before_deadline_still_completes() {
    let h = ActorRig::new(SHELL_PROFILE).ready();
    let (checks, _) = h.rx.snapshot_wait_progress_for_test();
    let id = h.submit_with("echo ON_TIME", |request| request.timeout_s = 0.2);
    wait_tx(&h, b"echo ON_TIME\n");
    assert!(h.rx.wait_for_snapshot_wait_progress_for_test(
        checks,
        h.rx.snapshot_len(),
        Duration::from_secs(5),
    ));
    h.inject_rx_processed(b"echo ON_TIME\r\nON_TIME\r\nroot@t:/# ");

    let result = h.wait_done(&id);
    assert_eq!(result["status"], PyValue::from("done"), "{result:?}");
    assert_eq!(result["stdout"], PyValue::from("ON_TIME"), "{result:?}");
    assert_eq!(result["partial"], PyValue::Bool(false), "{result:?}");
}

#[test]
fn i294_deferred_human_prompt_between_queued_commands_does_not_complete_next() {
    let h = ActorRig::new_with_hub(SHELL_PROFILE).ready();
    let console = h.attach_pty_console("i294-deferred-order");
    let tx_count = h.wal_tx_payloads().len();

    // A 以 producer marker 收尾，不在 B 的命令邊界前留下 shell prompt；
    // 讓延後 human line 的 RX prompt 明確落在 B TX 之後，驗證該 human TX 證據。
    let first = h.submit_with("echo FIRST", |r| {
        r.completion_regex = Some("(?m)^FIRST\\r?$".into())
    });
    wait_tx(&h, b"echo FIRST\n");
    let second = h.submit_ok("echo SECOND");
    console.push_input(b"echo HUMAN\n");
    assert_eq!(h.command_get(&first)["status"], PyValue::from("running"));

    h.inject_rx_reader_processed(b"echo FIRST\r\nFIRST\r\n");
    let first_result = h.wait_done(&first);
    assert_eq!(first_result["stdout"], PyValue::from("FIRST"));
    wait_tx(&h, b"echo HUMAN\n");

    wait_tx(&h, b"echo SECOND\n");
    assert_eq!(
        wal_tx_bytes_since(&h, tx_count),
        b"echo FIRST\necho HUMAN\necho SECOND\n".to_vec(),
        "A 完成後 human TX 與命令 B TX 的邊界都已建立"
    );
    h.inject_rx_processed(b"echo HUMAN\r\nHUMAN\r\nroot@t:/# ");
    h.inject_rx_processed(b"root@t:/# ");
    assert_eq!(
        h.command_get(&second)["status"],
        PyValue::from("running"),
        "deferred human 的 prompt 在第二筆 command echo 前不得完成"
    );

    h.inject_rx_processed(b"echo SECOND\r\nSECOND\r\nroot@t:/# ");
    let second_result = h.wait_done(&second);
    assert_eq!(second_result["status"], PyValue::from("done"));
    assert_eq!(second_result["stdout"], PyValue::from("SECOND"));
    assert_eq!(second_result["partial"], PyValue::Bool(false));
    assert_eq!(
        wal_tx_bytes_since(&h, tx_count),
        b"echo FIRST\necho HUMAN\necho SECOND\n".to_vec(),
        "deferred 回放與兩筆 agent 命令依既有仲裁送出，不增加 TX"
    );
}

#[test]
fn i294_background_uses_command_echo_boundary() {
    let h = ActorRig::new_with_hub(SHELL_PROFILE).ready();
    let console = h.attach_pty_console("i294-background-human");
    let tx_count = h.wal_tx_payloads().len();
    console.push_input(b"echo HUMAN\n");
    wait_tx(&h, b"echo HUMAN\n");
    h.inject_rx(b"echo HUMAN\r\nHUMAN\r\n");

    let id = h.submit_with("echo BACKGROUND", |r| r.mode = "background".into());
    wait_tx(&h, b"echo BACKGROUND\n");
    h.inject_rx(b"root@t:/# ");
    let before_echo = h.command_get(&id);
    assert_eq!(
        before_echo["status"],
        PyValue::from("running"),
        "背景命令同樣不得由命令前的 prompt 完成：{before_echo:?}"
    );

    h.inject_rx(b"echo BACKGROUND\r\nBACKGROUND\r\nroot@t:/# ");
    let rec = h.wait_done(&id);
    assert_eq!(rec["status"], PyValue::from("done"));
    assert_eq!(rec["execution_mode"], PyValue::from("background"));
    let tail = h.request(SessionRequest::ResultTail {
        cmd_id: id,
        from_chunk: 0,
        limit: 200,
    });
    assert_eq!(tail["status"], PyValue::from("active"));
    let PyValue::List(chunks) = tail["chunks"].clone() else {
        panic!("capture 應為 list")
    };
    let joined: String = chunks.iter().filter_map(PyValue::as_str).collect();
    assert!(joined.contains("BACKGROUND"), "背景輸出應擷取：{joined:?}");
    assert_eq!(
        wal_tx_bytes_since(&h, tx_count),
        b"echo HUMAN\necho BACKGROUND\n".to_vec(),
        "背景完成判定不增加 TX"
    );
}

#[test]
fn i294_no_echo_without_pending_human_tx_uses_tail_prompt() {
    let h = ActorRig::new(SHELL_PROFILE).ready();
    let tx_count = h.wal_tx_payloads().len();
    let id = h.submit_with("printf x", |r| r.timeout_s = 0.2);
    wait_tx(&h, b"printf x\n");
    h.inject_rx(b"x\r\nroot@t:/# ");

    let rec = h.wait_done(&id);
    assert_eq!(rec["status"], PyValue::from("done"));
    assert_eq!(rec["stdout"], PyValue::from("x"));
    assert_eq!(rec["partial"], PyValue::Bool(false));
    assert_eq!(wal_tx_bytes_since(&h, tx_count), b"printf x\n".to_vec());
}

#[test]
fn i294_prompt_before_command_tx_is_outside_no_echo_window() {
    let h = ActorRig::new(SHELL_PROFILE).ready();
    let tx_count = h.wal_tx_payloads().len();
    h.dev.script_write(vec![FakeWriteStep::PauseBeforeSubmit]);

    let id = h.submit_with("printf x", |r| r.timeout_s = 0.2);
    assert!(h.dev.wait_paused(Duration::from_secs(2)), "命令應停在 OS submit 前");
    h.inject_rx(b"root@t:/# ");
    h.dev.release_pause();
    wait_tx(&h, b"printf x\n");
    assert_eq!(
        h.command_get(&id)["status"],
        PyValue::from("running"),
        "command TX 前到達的舊 prompt 不得觸發無 echo 降階"
    );

    h.inject_rx(b"x\r\nroot@t:/# ");
    let rec = h.wait_done(&id);
    assert_eq!(rec["status"], PyValue::from("done"));
    assert_eq!(rec["stdout"], PyValue::from("x"));
    assert_eq!(rec["partial"], PyValue::Bool(false));
    assert_eq!(wal_tx_bytes_since(&h, tx_count), b"printf x\n".to_vec());
}

#[test]
fn i294_no_echo_with_pending_human_tx_returns_partial_without_recovery_tx() {
    let h = ActorRig::new_with_hub(SHELL_PROFILE).ready();
    let console = h.attach_pty_console("i294-no-echo-human");
    let tx_count = h.wal_tx_payloads().len();
    console.push_input(b"echo HUMAN\n");
    wait_tx(&h, b"echo HUMAN\n");

    let (deadline_started_tx, deadline_started_rx) = crossbeam_channel::bounded(1);
    h.rx
        .set_snapshot_wait_enter_hook_for_test(Some(Arc::new(move || {
            deadline_started_tx
                .send(())
                .expect("completion waiter 已建立 operation deadline");
        })));
    let id = h.submit_with("printf x", |r| r.timeout_s = 0.2);
    wait_tx(&h, b"printf x\n");
    deadline_started_rx
        .recv_timeout(Duration::from_secs(5))
        .expect("推進假時鐘前 operation deadline 已建立");
    h.inject_rx(b"root@t:/# ");
    assert_eq!(h.command_get(&id)["status"], PyValue::from("running"));

    h.advance(Duration::from_millis(200));
    let rec = h.wait_done(&id);
    assert_eq!(rec["status"], PyValue::from("error"));
    assert_eq!(rec["error_code"], PyValue::from("PROMPT_TIMEOUT"));
    assert_eq!(rec["partial"], PyValue::Bool(true));
    assert_eq!(rec["recovery_action"], PyValue::from("NONE"));
    assert_eq!(h.get_state()["state"], PyValue::from("READY"));
    assert!(
        !h.wal_tx_payloads().iter().any(|payload| payload == b"\x03" || payload == b"\x04"),
        "歧義結算不得送救援鍵"
    );
    assert_eq!(
        wal_tx_bytes_since(&h, tx_count),
        b"echo HUMAN\nprintf x\n".to_vec(),
        "無 echo 歧義不額外送救援鍵或重送"
    );
}

#[test]
fn i294_raw_half_line_does_not_anchor_agent_echo() {
    let h = ActorRig::new_with_hub(SHELL_PROFILE).ready();
    let console = h.attach_pty_console("i294-half-line");
    let tx_count = h.wal_tx_payloads().len();
    console.push_input(b"e");
    wait_tx(&h, b"e");

    let (deadline_started_tx, deadline_started_rx) = crossbeam_channel::bounded(1);
    h.rx
        .set_snapshot_wait_enter_hook_for_test(Some(Arc::new(move || {
            deadline_started_tx
                .send(())
                .expect("completion waiter 已建立 operation deadline");
        })));
    let id = h.submit_with("cho AGENT", |r| r.timeout_s = 0.2);
    wait_tx(&h, b"cho AGENT\n");
    deadline_started_rx
        .recv_timeout(Duration::from_secs(5))
        .expect("推進假時鐘前 operation deadline 已建立");
    h.inject_rx(b"root@t:/# echo AGENT\r\nAGENT\r\nroot@t:/# ");
    h.advance(Duration::from_millis(200));

    let rec = h.wait_done(&id);
    assert_eq!(rec["status"], PyValue::from("error"));
    assert_eq!(rec["error_code"], PyValue::from("PROMPT_TIMEOUT"));
    assert_eq!(rec["partial"], PyValue::Bool(true));
    assert!(rec["stdout"].as_str().is_some_and(|stdout| stdout.contains("AGENT")));
    assert_eq!(h.get_state()["state"], PyValue::from("READY"));
    assert!(
        !h.wal_tx_payloads().iter().any(|payload| payload == b"\x03" || payload == b"\x04"),
        "歧義結算不得送救援鍵"
    );
    assert_eq!(
        wal_tx_bytes_since(&h, tx_count),
        b"echo AGENT\n".to_vec(),
        "half-line 歧義不額外送救援鍵或重送"
    );
}

#[test]
fn i294_raw_half_line_does_not_poison_next_agent_echo() {
    let h = ActorRig::new_with_hub(SHELL_PROFILE).ready();
    let console = h.attach_pty_console("i294-half-line-next");
    console.push_input(b"e");
    wait_tx(&h, b"e");

    let (deadline_started_tx, deadline_started_rx) = crossbeam_channel::bounded(1);
    h.rx
        .set_snapshot_wait_enter_hook_for_test(Some(Arc::new(move || {
            deadline_started_tx
                .send(())
                .expect("completion waiter 已建立 operation deadline");
        })));
    let first = h.submit_with("cho AGENT", |r| r.timeout_s = 0.2);
    wait_tx(&h, b"cho AGENT\n");
    deadline_started_rx
        .recv_timeout(Duration::from_secs(5))
        .expect("推進假時鐘前 operation deadline 已建立");
    h.inject_rx(b"root@t:/# echo AGENT\r\nAGENT\r\nroot@t:/# ");
    h.advance(Duration::from_millis(200));
    let first_result = h.wait_done(&first);
    assert_eq!(first_result["partial"], PyValue::Bool(true));

    let second = h.submit_ok("echo NEXT");
    wait_tx(&h, b"echo NEXT\n");
    h.inject_rx(b"echo NEXT\r\nNEXT\r\nroot@t:/# ");
    let second_result = h.wait_done(&second);
    assert_eq!(second_result["status"], PyValue::from("done"));
    assert_eq!(second_result["stdout"], PyValue::from("NEXT"));
    assert_eq!(second_result["partial"], PyValue::Bool(false));
}

#[test]
fn i294_fragmented_wrapped_echo_waits_for_complete_command() {
    let h = ActorRig::new_with_hub(SHELL_PROFILE).ready();
    let console = h.attach_pty_console("i294-wrapped-echo-human");
    console.push_input(b"echo HUMAN\n");
    wait_tx(&h, b"echo HUMAN\n");
    let tx_count = h.wal_tx_payloads().len();
    let command = format!("echo {} {}", "x".repeat(50), "y".repeat(40));
    let id = h.submit_ok(&command);
    let tx = format!("{command}\n");
    wait_tx(&h, tx.as_bytes());

    let incomplete = format!(
        "noiseX\x08\r\nroot@t:/# {}\x1b[31m\r\r\n\x1b[0mroot@t:/# ",
        &command[..65]
    );
    // 第一段只有折行命令前綴，後面刻意有舊 prompt。human TX 使無 echo 降階保持歧義；
    // 若把折行前綴誤認為完整 echo，這個 prompt 會讓命令提早 done，下面 running 斷言會失敗。
    h.inject_rx_processed(incomplete.as_bytes());
    assert_eq!(h.command_get(&id)["status"], PyValue::from("running"));

    let first = format!(
        "\r\nroot@t:/# {}\x1b[31m\r\r\n\x1b[0m",
        &command[..65]
    );
    h.inject_rx_processed(first.as_bytes());
    assert_eq!(h.command_get(&id)["status"], PyValue::from("running"));

    let tail = format!("{}\r\nWRAPPED\r\nroot@t:/# ", &command[65..]);
    h.inject_rx_processed(tail.as_bytes());
    let rec = h.wait_done(&id);
    assert_eq!(rec["status"], PyValue::from("done"));
    assert_eq!(rec["stdout"], PyValue::from("WRAPPED"));
    assert_eq!(rec["partial"], PyValue::Bool(false));
    assert_eq!(
        wal_tx_bytes_since(&h, tx_count),
        tx.as_bytes().to_vec(),
        "折行 echo 判定不重送命令"
    );
}

#[test]
fn i294_busybox_wrap_after_command_space_keeps_actor_completion_anchored() {
    let h = ActorRig::new_with_hub(SHELL_PROFILE).ready();
    let console = h.attach_pty_console("i294-wrap-command-space");
    console.push_input(b"echo HUMAN\n");
    wait_tx(&h, b"echo HUMAN\n");

    let command = format!("echo {} {}", "x".repeat(40), "y".repeat(40));
    let cut = 5 + 40 + 1;
    let tx_count = h.wal_tx_payloads().len();
    let (completion_matched_tx, completion_matched_rx) = crossbeam_channel::bounded(1);
    h.rx.set_snapshot_wait_match_hook_for_test(Some(Arc::new(move |_| {
        completion_matched_tx
            .send(())
            .expect("completion evaluator 已判定完整 echo 與 prompt");
    })));
    let id = h.submit_with(&command, |r| r.timeout_s = 0.2);
    wait_tx(&h, format!("{command}\n").as_bytes());
    let raw = format!(
        "{}\r\r\n{}\r\nOUT\r\nroot@t:/# ",
        &command[..cut],
        &command[cut..]
    );
    h.inject_rx_processed(raw.as_bytes());
    completion_matched_rx
        .recv_timeout(Duration::from_secs(5))
        .expect("operation deadline 已建立，且完成判定先於假時鐘推進");
    h.advance(Duration::from_millis(200));
    let result = h.wait_done(&id);
    assert_eq!(result["status"], PyValue::from("done"));
    assert_eq!(result["stdout"], PyValue::from("OUT"));
    assert_eq!(result["partial"], PyValue::Bool(false));
    assert_eq!(
        wal_tx_bytes_since(&h, tx_count),
        format!("{command}\n").as_bytes(),
        "折行 echo 不增加或重送 TX"
    );
}

#[test]
fn i294_documented_repeated_echo_compatibility_drops_command_equal_stdout_line() {
    let h = ActorRig::new(SHELL_PROFILE).ready();
    let tx_count = h.wal_tx_payloads().len();
    let command = "cat /tmp/job";
    let id = h.submit_ok(command);
    wait_tx(&h, b"cat /tmp/job\n");

    h.inject_rx_processed(
        b"cat /tmp/job\r\ncat /tmp/job\r\nnext\r\nroot@t:/# ",
    );

    let result = h.wait_done(&id);
    assert_eq!(result["status"], PyValue::from("done"));
    assert_eq!(result["stdout"], PyValue::from("next"));
    assert_eq!(result["partial"], PyValue::Bool(false));
    assert_eq!(
        wal_tx_bytes_since(&h, tx_count),
        b"cat /tmp/job\n".to_vec(),
        "文件化的重複 echo 相容處理不增加或重送 TX"
    );
}

#[test]
fn timeout_sends_ctrl_c_then_recovers_and_flushes_queue() {
    let h = ActorRig::new(SHELL_PROFILE).ready();
    h.dev.on_tx_reply(b"\x03", b"^C\r\nroot@t:/# ");
    let tx_count = h.wal_tx_payloads().len();
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
    ); // 命令紀錄沿用基線成功終態；recovery code 保存在結果 capture。
    assert_eq!(rec["partial"], PyValue::Bool(true));
    let recovered = h.request(SessionRequest::ResultTail {
        cmd_id: slow,
        from_chunk: 0,
        limit: 10,
    });
    assert_eq!(recovered["ok"], PyValue::Bool(true), "{recovered:?}");
    assert_eq!(
        recovered["error_code"],
        PyValue::from("PROMPT_TIMEOUT_RECOVERED"),
        "救援成功的錯誤碼必須與基線相同：{recovered:?}"
    );
    let tx: Vec<u8> = h
        .wal_tx_payloads()
        .into_iter()
        .skip(tx_count)
        .flatten()
        .collect();
    assert_eq!(tx, b"sleep 20\n\x03".to_vec(), "TX 序列為原命令再加成功救援的 Ctrl-C");
    println!(
        "i294-r8b baseline tuple: status={:?}, error_code={:?}, partial={:?}, recovery_action={:?}, tx={tx:?}",
        rec["status"], recovered["error_code"], rec["partial"], rec["recovery_action"]
    );
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

// ---------------------------------------------------------------------------
// WP6-1 第 3 項（G7b，OD-74、rulings s2 D5）：OpRunner 逾時分流的可信判定
// ---------------------------------------------------------------------------
//
// Python `origin/main:sw_core/session_manager.py:6988-7012`（`_recover_after_failure`）：`human = trusted_human`
// 成立即 open-or-refresh human lease（`owner=source`）並回 `PROMOTE_HUMAN_INTERACTIVE`，不送 CTRL_C／CTRL_D；
// 否則才進 passthrough 判定與救援鍵迴圈（`:7014` 起）。RPC 的 `trusted_human` 依原始 source 判定：bare `human`
// （及 `human:`）→ true 並正規化為 `human:cli`；明寫 `human:<x>`（含 `human:cli`）、其他前綴 → false（OD-74）。
// line editor 的 `HumanConsole` 一律可信，與旗標取 OR（D5，`SubmitReq::is_trusted_human()`）。

/// 裝置對救援鍵的應答：CTRL_C 即回 prompt，或只有 CTRL_D 回 prompt（證明兩階段救援仍在）。
#[derive(Clone, Copy, Debug)]
enum Wp61KeyReply {
    CtrlC,
    CtrlDOnly,
}

/// 逾時後的預期結局。
#[derive(Clone, Copy, Debug)]
enum Wp61Expect {
    /// 升格 interactive lease，lease owner 為指定字串，零救援鍵。
    Promote { owner: &'static str },
    /// agent 救援：以指定鍵回 prompt。
    Rescue { key: &'static str },
}

struct Wp61Case {
    label: &'static str,
    source: &'static str,
    trusted_human: bool,
    origin: sw_core::arbiter::SubmitOrigin,
    reply: Wp61KeyReply,
    expect: Wp61Expect,
}

/// 單一情境（獨立 rig）：送出不會回 prompt 的命令，推進到逾時結算，斷言 UART／WAL TX、紀錄終態、
/// `recovery_action` 與 lease owner。
fn wp61_runner_timeout_case(c: &Wp61Case) {
    let label = c.label;
    let h = ActorRig::new(SHELL_PROFILE).ready();
    match c.reply {
        Wp61KeyReply::CtrlC => h.dev.on_tx_reply(b"\x03", b"^C\r\nroot@t:/# "),
        Wp61KeyReply::CtrlDOnly => h.dev.on_tx_reply(b"\x04", b"\r\nroot@t:/# "),
    }
    let uart_from = h.dev.tx_len();
    let wal_from = tx_rows(&h).len();
    let id = h.submit_with("sleep 20", |r| {
        r.timeout_s = 1.0;
        r.source = c.source.into();
        r.trusted_human = c.trusted_human;
        r.origin = c.origin.clone();
    });
    wait_tx(&h, b"sleep 20\n");
    advance_until(&h, Duration::from_millis(100), "逾時結算", |h| {
        is_done(h, &id)
    });
    let rec = h.wait_done(&id);
    let uart = h.dev.tx_since(uart_from);
    let wal: Vec<(String, Vec<u8>)> = tx_rows(&h)
        .into_iter()
        .skip(wal_from)
        .map(|r| {
            let source = r
                .get("source")
                .and_then(PyValue::as_str)
                .unwrap_or("")
                .to_string();
            (source, b64(&r))
        })
        .collect();
    let wal_payloads: Vec<Vec<u8>> = wal.iter().map(|(_, p)| p.clone()).collect();
    assert_eq!(
        rec["source"],
        PyValue::from(c.source),
        "{label}：紀錄 source：{rec:?}"
    );
    if matches!(c.expect, Wp61Expect::Rescue { .. }) {
        assert_eq!(
            rec["error_code"],
            PyValue::Null,
            "{label}：成功救援的命令紀錄沿用基線不記 error_code：{rec:?}"
        );
    }
    assert_eq!(rec["partial"], PyValue::Bool(true), "{label}：{rec:?}");
    assert_eq!(
        state_of(&h),
        "READY",
        "{label}：逾時結算後維持 READY：{rec:?}"
    );
    match c.expect {
        Wp61Expect::Promote { owner } => {
            assert_eq!(
                rec["error_code"],
                PyValue::Null,
                "{label}：trusted human 升格仍為成功結算：{rec:?}"
            );
            assert_eq!(
                uart, b"sleep 20\n",
                "{label}：UART TX 只有命令本身，零 CTRL_C／CTRL_D"
            );
            assert_eq!(
                wal_payloads,
                vec![b"sleep 20\n".to_vec()],
                "{label}：WAL TX 只有命令本身，零 CTRL_C／CTRL_D：{wal:?}"
            );
            assert_eq!(
                rec["status"],
                PyValue::from("interactive"),
                "{label}：{rec:?}"
            );
            assert_eq!(
                rec["execution_mode"],
                PyValue::from("interactive"),
                "{label}：{rec:?}"
            );
            assert_eq!(
                rec["recovery_action"],
                PyValue::from("PROMOTE_HUMAN_INTERACTIVE"),
                "{label}：{rec:?}"
            );
            assert_eq!(rec["stdout"], PyValue::from(""), "{label}：{rec:?}");
            let iid = h.get_state()["interactive_session_id"]
                .as_str()
                .map(str::to_string)
                .unwrap_or_else(|| panic!("{label}：升格後應持有 interactive lease：{rec:?}"));
            assert_eq!(
                rec["interactive_session_id"],
                PyValue::from(iid.clone()),
                "{label}：紀錄指向本次升格的 lease"
            );
            let status = h.request(SessionRequest::InteractiveStatus {
                interactive_id: iid,
                screen_chars: 0,
            });
            assert_eq!(status["ok"], PyValue::Bool(true), "{label}：{status:?}");
            assert_eq!(
                status["owner"],
                PyValue::from(owner),
                "{label}：lease owner（Python `owner=source`）"
            );
        }
        Wp61Expect::Rescue { key } => {
            let mut expected = b"sleep 20\n".to_vec();
            let mut expected_wal = vec![(c.source.to_string(), b"sleep 20\n".to_vec())];
            expected.push(0x03);
            expected_wal.push(("system:recover".to_string(), b"\x03".to_vec()));
            if key == "CTRL_D" {
                expected.push(0x04);
                expected_wal.push(("system:recover".to_string(), b"\x04".to_vec()));
            }
            assert_eq!(
                uart, expected,
                "{label}：UART TX 依序為命令、救援鍵（{key}）"
            );
            assert_eq!(
                wal, expected_wal,
                "{label}：WAL TX 依序為命令、system:recover 救援鍵（{key}）"
            );
            assert_eq!(rec["status"], PyValue::from("done"), "{label}：{rec:?}");
            assert_eq!(
                rec["execution_mode"],
                PyValue::from("line"),
                "{label}：{rec:?}"
            );
            assert_eq!(
                rec["recovery_action"],
                PyValue::from(key),
                "{label}：{rec:?}"
            );
            assert!(
                rec.get("interactive_session_id")
                    .is_none_or(PyValue::is_null),
                "{label}：agent 救援不開 lease：{rec:?}"
            );
            assert_eq!(
                h.get_state()["interactive_session_id"],
                PyValue::Null,
                "{label}：agent 救援後無 lease"
            );
        }
    }
}

#[test]
fn wp61_runner_timeout_trust_matrix() {
    use sw_core::arbiter::SubmitOrigin;
    let cases = [
        // OD-74：RPC bare `human` 經 service 正規化後的形狀（`trusted_human=true`、`source=human:cli`）。
        Wp61Case {
            label: "RPC trusted human:cli",
            source: "human:cli",
            trusted_human: true,
            origin: SubmitOrigin::Rpc,
            reply: Wp61KeyReply::CtrlC,
            expect: Wp61Expect::Promote { owner: "human:cli" },
        },
        // OD-74：明寫 `human:cli`／`human:<x>` 與其他前綴一律比照 agent。
        Wp61Case {
            label: "RPC untrusted human:cli",
            source: "human:cli",
            trusted_human: false,
            origin: SubmitOrigin::Rpc,
            reply: Wp61KeyReply::CtrlC,
            expect: Wp61Expect::Rescue { key: "CTRL_C" },
        },
        Wp61Case {
            label: "RPC untrusted human:x",
            source: "human:x",
            trusted_human: false,
            origin: SubmitOrigin::Rpc,
            reply: Wp61KeyReply::CtrlC,
            expect: Wp61Expect::Rescue { key: "CTRL_C" },
        },
        Wp61Case {
            label: "RPC untrusted codex",
            source: "codex",
            trusted_human: false,
            origin: SubmitOrigin::Rpc,
            reply: Wp61KeyReply::CtrlC,
            expect: Wp61Expect::Rescue { key: "CTRL_C" },
        },
        // 兩階段救援：CTRL_C 無回應 → CTRL_D 回 prompt（`:7030` 起的鍵序不變）。
        Wp61Case {
            label: "RPC untrusted human:x CTRL_D",
            source: "human:x",
            trusted_human: false,
            origin: SubmitOrigin::Rpc,
            reply: Wp61KeyReply::CtrlDOnly,
            expect: Wp61Expect::Rescue { key: "CTRL_D" },
        },
        // D5：line editor 的 `HumanConsole` 即使 `trusted_human=false` 仍可信（OR），owner 為 `human:<client_id>`。
        Wp61Case {
            label: "HumanConsole untrusted flag",
            source: "human:c1",
            trusted_human: false,
            origin: SubmitOrigin::HumanConsole {
                client_id: "c1".into(),
            },
            reply: Wp61KeyReply::CtrlC,
            expect: Wp61Expect::Promote { owner: "human:c1" },
        },
    ];
    let failed: Vec<&str> = cases
        .iter()
        .filter(|c| {
            std::panic::catch_unwind(std::panic::AssertUnwindSafe(|| wp61_runner_timeout_case(c)))
                .is_err()
        })
        .map(|c| c.label)
        .collect();
    assert!(failed.is_empty(), "失敗情境：{failed:?}");
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
    assert_eq!(rec["error_code"], PyValue::Null);
    assert_eq!(rec["partial"], PyValue::Bool(true));
    assert_eq!(rec["recovery_action"], PyValue::from("CTRL_D"));
    assert!(wal_tx_has(&h, b"\x03") && wal_tx_has(&h, b"\x04"));
    assert_eq!(state_of(&h), "READY");
}

#[test]
fn both_keys_fail_prompt_timeout_to_attached() {
    let h = ActorRig::new(SHELL_PROFILE).ready();
    let tx_count = h.wal_tx_payloads().len();
    let id = h.submit_with("cat", |r| r.timeout_s = 1.0);
    let queued = h.submit_ok("echo later");
    wait_tx(&h, b"cat\n");
    advance_until(&h, Duration::from_millis(100), "救援失敗收尾", |h| {
        is_done(h, &id)
    });
    let rec = h.command_get(&id);
    assert_eq!(rec["status"], PyValue::from("error"));
    assert_eq!(rec["error_code"], PyValue::from("PROMPT_TIMEOUT"));
    assert_eq!(rec["partial"], PyValue::Bool(true));
    assert_eq!(rec["recovery_action"], PyValue::from("NONE"));
    assert_eq!(wal_tx_bytes_since(&h, tx_count), b"cat\n\x03\x04".to_vec());
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

/// 9af187ca 的 producer 擷取器原樣保存在測試內，供 producer 路徑差分使用。
/// 此 oracle 刻意保留整份 raw 的 BusyBox/readline 標記判斷；i294 evaluator 不得改變 producer 語意。
fn producer_oracle_9af187ca(
    raw: &str,
    command: &str,
    prompt_regex: &sw_proto::pyre::PyRegex,
    completion: Option<&sw_proto::pyre::PyRegex>,
) -> String {
    #[derive(Clone, Copy)]
    struct WrapEvidence {
        newline: bool,
        space: bool,
    }

    fn char_prefix(text: &str, chars: usize) -> String {
        text.chars().take(chars).collect()
    }

    fn last_match_start(re: &sw_proto::pyre::PyRegex, text: &str) -> Option<usize> {
        let total = text.chars().count();
        let mut pos = 0;
        let mut last = None;
        while pos <= total {
            let Some(m) = re.search_from(text, pos) else {
                break;
            };
            last = Some(m.start);
            pos = if m.end > m.start { m.end } else { m.end + 1 };
        }
        last
    }

    fn match_wrapped_echo(
        text: &[char],
        start: usize,
        command: &[char],
        wraps: WrapEvidence,
    ) -> Option<usize> {
        let (mut i, mut j) = (start, 0usize);
        let mut last_wrap: Option<usize> = None;
        while j < command.len() {
            let ch = *text.get(i)?;
            if ch == command[j] {
                i += 1;
                j += 1;
                continue;
            }
            let allowed = (ch == '\n' && wraps.newline) || (ch == ' ' && wraps.space);
            if j == 0 || !allowed {
                return None;
            }
            let column = last_wrap.map_or(start + j, |w| j - w);
            if column < 20 {
                return None;
            }
            last_wrap = Some(j);
            i += 1;
        }
        Some(i)
    }

    fn strip_wrapped_command_echo(
        lines: &[String],
        command: &str,
        wraps: WrapEvidence,
    ) -> Option<usize> {
        if command.is_empty() || lines.is_empty() {
            return None;
        }
        let cmd: Vec<char> = command.chars().collect();
        let window = &lines[..lines.len().min(cmd.len() / 20 + 2)];
        let text: Vec<char> = window.join("\n").chars().collect();
        for start in 0..lines[0].chars().count() {
            if let Some(end) = match_wrapped_echo(&text, start, &cmd, wraps) {
                if end == text.len() || text[end] == '\n' {
                    return Some(text[..end].iter().filter(|c| **c == '\n').count() + 1);
                }
            }
        }
        None
    }

    let cleaned = clean_text(raw);
    let prompt_start = last_match_start(prompt_regex, &cleaned);
    let completion_end = completion
        .and_then(|re| re.search(raw))
        .map(|m| clean_text(&char_prefix(raw, m.end)).chars().count());
    let mut text = cleaned;
    if let Some(start) = prompt_start {
        let cut = match completion {
            None => true,
            Some(_) => completion_end.is_some_and(|end| start >= end),
        };
        if cut {
            text = char_prefix(&text, start);
        }
    }
    let mut lines: Vec<String> = splitlines(text.trim_start_matches('\n'))
        .into_iter()
        .map(str::to_string)
        .collect();
    if !command.is_empty() {
        let stripped = py_strip(command);
        let mut removed = false;
        while lines
            .first()
            .is_some_and(|line| py_strip(line).ends_with(stripped))
        {
            lines.remove(0);
            removed = true;
        }
        if !removed {
            let wraps = WrapEvidence {
                newline: raw.contains("\r\r\n"),
                space: raw.contains(" \r"),
            };
            if wraps.newline || wraps.space {
                if let Some(n) = strip_wrapped_command_echo(&lines, stripped, wraps) {
                    lines.drain(..n);
                }
            }
        }
    }
    lines.join("\n").trim_matches('\n').to_string()
}

#[test]
fn i294_producer_stdout_matches_base_9af187ca_oracle() {
    let prompt = compile("(?m)^root@t:.*# ", PyFlags(0)).expect("prompt regex");
    let marker = compile("(?m)^DONE\\r?$", PyFlags(0)).expect("completion marker regex");

    // R5-01 固定反例：輸出行的 ` \\r\\n` 不得切換 producer 的重複 echo 剝除策略。
    let raw = "ls\r\nls\r\na b \r\nDONE\r\n";
    assert_eq!(
        producer_oracle_9af187ca(raw, "ls", &prompt, Some(&marker)),
        "a b \nDONE"
    );
    assert_eq!(
        extract_command_stdout(raw, "ls", &prompt, Some(&marker)),
        "a b \nDONE"
    );

    // 有界 producer 輸入空間：重複 echo、兩種終端折行、無關行尾空白、ANSI／backspace、
    // completion marker 與 prompt 的相對位置都逐一對照 9af187ca oracle。
    let long = wrap_cmd96();
    let mut cases = Vec::<(String, String)>::new();
    for command in ["ls".to_string(), long.clone()] {
        for repeats in [1usize, 2] {
            for output in [
                "OUT\r\n",
                "a b \r\n",
                "\x1b[31mA\x08B\x07\x1b[0m\r\n",
            ] {
                for tail in [
                    "DONE\r\nroot@t:/# ",
                    "root@t:/# \r\nDONE\r\n",
                    "DONE\r\n",
                ] {
                    let echo = format!("{command}\r\n").repeat(repeats);
                    cases.push((format!("{echo}{output}{tail}"), command.clone()));
                }
            }
        }
    }
    let mixed_command = format!("echo {} {} {}", "a".repeat(30), "b".repeat(35), "c".repeat(40));
    let mixed_echo = format!(
        "{}\r\r\n{} \r{}\r\n",
        &mixed_command[..25],
        &mixed_command[25..60],
        &mixed_command[60..]
    );
    cases.push((
        format!("{mixed_echo}\x1b[32mOUT\x1b[0m\r\nDONE\r\nroot@t:/# "),
        mixed_command,
    ));
    let mut prefixed_prompt_case = format!(
        "root@t:/# old\r\n\x1b[33m{long}\x1b[0mX\x08\r\nOUT\r\nDONE\r\nroot@t:/# "
    );
    cases.push((prefixed_prompt_case.clone(), long.clone()));
    prefixed_prompt_case.push_str("AFTER-MARKER\r\n");
    cases.push((prefixed_prompt_case, long));

    for (raw, command) in cases {
        let completion = Some(&marker);
        assert_eq!(
            extract_command_stdout(&raw, &command, &prompt, completion),
            producer_oracle_9af187ca(&raw, &command, &prompt, completion),
            "producer output differs from 9af187ca for command={command:?}, raw={raw:?}"
        );
    }

    // 確認完成標記實際經一般 producer 呼叫路徑時也維持基線結果。
    let h = ActorRig::new(SHELL_PROFILE).ready();
    h.dev
        .on_tx_reply(b"ls\n", b"ls\r\nls\r\na b \r\nDONE\r\nroot@t:/# ");
    let tx_count = h.wal_tx_payloads().len();
    let id = h.submit_with("ls", |request| {
        request.completion_regex = Some("(?m)^DONE\\r?$".into())
    });
    let rec = h.wait_done(&id);
    assert_eq!(rec["status"], PyValue::from("done"));
    assert_eq!(rec["stdout"], PyValue::from("a b \nDONE"));
    assert_eq!(wal_tx_bytes_since(&h, tx_count), b"ls\n");
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

/// S2-GP-gpx ITEM 5：expect_reboot 與命令猜中 reboot 皆不建 background capture／id，result_tail 走 registry fallback。
/// Python `origin/main:sw_core/session_manager.py:6840-6880`；擁有者裁決 `origin/docs/parity-amendments:docs/rust-refactor/reviews/rulings/s2.md`（2026-11-12，第 1 點第 4 小點）。
#[test]
fn gpx_background_reboot_command_creates_no_capture() {
    for (command, expect_reboot) in [("sync && reboot", true), ("reboot", false)] {
        let mut h = CoreHarness::new(SHELL_PROFILE);
        h.to_ready();
        let mut req = agent_submit(command);
        req.mode = "background".into();
        req.expect_reboot = expect_reboot;
        let response = h.rpc(SessionRequest::Submit(req)).unwrap();
        let id = response["cmd_id"].as_str().expect("cmd_id").to_string();

        assert!(
            h.core.background_cmd_ids_for_test().is_empty(),
            "reboot 分流不建立 background_cmd_ids（command={command}, expect_reboot={expect_reboot})"
        );
        let tail = h
            .rpc(SessionRequest::ResultTail {
                cmd_id: id.clone(),
                from_chunk: 3,
                limit: 20,
            })
            .unwrap();
        assert_eq!(
            h.wire(&tail),
            format!(
                r#"{{"ok":true,"cmd_id":"{id}","status":"running","error_code":null,"from_seq":0,"last_seq":0,"from_chunk":3,"next_chunk":3,"chunks":[]}}"#
            ),
            "Python pre-capture fallback（command={command}, expect_reboot={expect_reboot})"
        );
    }
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
// WP5-6 第 5 項：非同步 reboot 開機證據等待的 reboot 接線與清除生命週期（G8，#243）
// Python：`origin/main:sw_core/session_manager.py:6409-6411`（新一輪 reboot 於送出前歸零舊期限）、
// `:6463-6466`（2 s 內 prompt 回來：只 `clear_boot_quiet()`＋`begin_boot_evidence_wait()`，不 `confirm_ready()`）、
// `:2704`（無 bridge 的 `_detach_session_locked` 歸零）。情境對照 `tests/test_async_reboot_evidence.py:74-372`
// 與 `tests/test_boot_quiet.py:735-758`。負向路徑一律以 PAL 寫入次數、OS 提交次數與 WAL TX 筆數三者斷言。
// ---------------------------------------------------------------------------

const WP56_PROMPT: &[u8] = b"\r\nroot@t:/# ";

/// PAL 寫入呼叫數、OS 提交數、WAL TX 筆數（零 TX 斷言用）。
fn wp56_tx_counts(h: &ActorRig) -> (usize, usize, usize) {
    (h.dev.write_calls(), h.dev.os_submits(), tx_rows(h).len())
}

/// 重新掛上「舊 shell」的 `\n`→prompt 應答（`on_tx_reply` 為一次性、比對累積 TX，故先 `take_tx` 清緩衝）。
/// 搭配 `ready()` 常駐的 nonce 應答替身，等同 Python `FakeBridge(prompt_within_2s=True)`：daemon 一旦送出確認
/// probe，舊 shell 就會成功應答——pending 能否被清完全取決於 daemon 有沒有送 probe。
fn wp56_arm_stale_shell(h: &ActorRig) {
    let _ = h.dev.take_tx();
    h.dev.on_tx_reply(b"\n", WP56_PROMPT);
}

/// READY session 送出 reboot 類命令，替身立即重印 prompt（非同步 reboot，實機 TX 後 11 ms 即回 prompt）；
/// 回傳後 quiet 已清、pending 保留（#139／#162 不變），並預掛舊 shell 應答（`wp56_arm_stale_shell`）。
fn wp56_async_reboot(h: &ActorRig, cmd: &str, expect_reboot: bool) {
    wp56_async_reboot_unarmed(h, cmd, expect_reboot);
    wp56_arm_stale_shell(h);
}

/// 同 `wp56_async_reboot`，但不預掛 `\n` 應答（下一筆含 `\n` 的 TX 不會得到 prompt）。
fn wp56_async_reboot_unarmed(h: &ActorRig, cmd: &str, expect_reboot: bool) {
    let mut tx = cmd.as_bytes().to_vec();
    tx.push(b'\n');
    let mut reply = cmd.as_bytes().to_vec();
    reply.extend_from_slice(WP56_PROMPT);
    h.dev.on_tx_reply(&tx, &reply);
    let id = h.submit_with(cmd, |r| r.expect_reboot = expect_reboot);
    let rec = h.wait_done(&id);
    assert_eq!(rec["status"], PyValue::from("done"), "{rec:?}");
    assert_eq!(rec["partial"], PyValue::Bool(false), "{rec:?}");
    assert_eq!(rec["recovery_action"], PyValue::Null, "{rec:?}");
    let s = h.get_state();
    assert_eq!(s["state"], PyValue::from("READY"));
    assert_eq!(
        s["boot_quiet_remaining_s"],
        PyValue::Null,
        "TX 靜默維度已解除（#139）"
    );
    assert_eq!(
        s["ready_reconfirm_pending"],
        PyValue::Bool(true),
        "#162 pending 保留"
    );
}

fn wp56_pending(h: &ActorRig) -> bool {
    h.get_state()["ready_reconfirm_pending"] == PyValue::Bool(true)
}

fn wp56_tx_contains(h: &ActorRig, needle: &[u8]) -> bool {
    h.wal_tx_payloads()
        .iter()
        .any(|p| p.windows(needle.len()).any(|w| w == needle))
}

/// 確認 READY 後 agent 命令實際送出並完成（`test_banner_then_boot_done_confirms_ready` 的 follow 段）。
fn wp56_follow_command_runs(h: &ActorRig) {
    h.dev
        .on_tx_reply(b"echo hi\n", b"echo hi\r\nhi\r\nroot@t:/# ");
    let id = h.submit_ok("echo hi");
    let rec = h.wait_done(&id);
    assert_eq!(rec["status"], PyValue::from("done"), "{rec:?}");
    assert_eq!(rec["stdout"], PyValue::from("hi"), "{rec:?}");
    assert!(wal_tx_has(h, b"echo hi\n"), "命令必須實際寫到 UART");
}

#[test]
fn wp56_async_reboot_stale_shell_cannot_confirm_ready() {
    // `test_wait_started_by_async_reboot`／`test_stale_shell_nonce_does_not_confirm_ready`／
    // `test_agent_command_gets_autoboot_quiet_before_banner`（`tests/test_async_reboot_evidence.py:74-104`）。
    let h = ActorRig::new(SHELL_PROFILE).ready();
    wp56_async_reboot(&h, "reboot", false);
    assert_eq!(
        h.get_state()["boot_evidence_wait_remaining_s"],
        PyValue::Float(60.0),
        "2 s 內 prompt 回來即起算 BOOT_EVIDENCE_GRACE_S（`:6466`）"
    );
    let before = wp56_tx_counts(&h);

    // RX 閒置超過 REPROBE_RX_IDLE_S（3 s）：實機 +3.1～+6.3 s 正是此刻送出 `\n`＋nonce，舊 shell 會應答。
    h.advance(Duration::from_secs(10));
    assert_eq!(
        wp56_tx_counts(&h),
        before,
        "banner 前不得送確認 probe（PAL 寫入／OS 提交／WAL TX 皆零新增）"
    );
    let s = h.get_state();
    assert_eq!(s["state"], PyValue::from("READY"));
    assert_eq!(
        s["ready_reconfirm_pending"],
        PyValue::Bool(true),
        "pending 不得被舊 shell 的 nonce 清掉"
    );
    assert_eq!(
        s["boot_evidence_wait_remaining_s"],
        PyValue::Float(50.0),
        "等待仍有效"
    );
    assert_eq!(s["reprobe_attempts"], PyValue::from(0));
    assert_eq!(s["next_reprobe_at"], PyValue::Null);
    assert_eq!(s["last_error"], PyValue::Null);

    // stale-READY 窗：agent 命令回 AUTOBOOT_QUIET（可重試），零 TX。
    let resp = h.request(SessionRequest::Submit(agent_submit(
        "cat /proc/sys/kernel/random/boot_id",
    )));
    assert_eq!(resp["ok"], PyValue::Bool(false));
    assert_eq!(resp["error_code"], PyValue::from("AUTOBOOT_QUIET"));
    assert_eq!(resp["retry_after_s"], PyValue::from(5.0));
    assert!(resp.get("cmd_id").is_none(), "gate 拒絕不配 cmd_id");
    match &resp["session"] {
        PyValue::Dict(session) => assert_eq!(
            session["boot_evidence_wait_remaining_s"],
            PyValue::Float(50.0),
            "回應帶出剩餘寬限（`TestSubmitTimeGateDuringWait`）"
        ),
        other => panic!("session 不是 object：{other:?}"),
    }
    assert_eq!(wp56_tx_counts(&h), before, "gate 拒絕必須零 TX 副作用");
}

#[test]
fn wp56_banner_then_prompt_restores_ready() {
    // `TestBannerReleasesWait`（`tests/test_async_reboot_evidence.py:144-187`）與
    // `test_confirm_probe_after_reboot_restores_execute`（`tests/test_boot_quiet.py:735-758`）。
    let h = ActorRig::new(SHELL_PROFILE).ready();
    wp56_async_reboot(&h, "reboot", false);
    assert_eq!(
        h.get_state()["boot_evidence_wait_remaining_s"],
        PyValue::Float(60.0)
    );

    // shutdown 訊息（含平台字串）不是開機證據：等待維持。
    h.inject_rx(b"EXT4-fs (flash-data): unmounting filesystem\r\nSMCOS: rebooting system...\r\n");
    assert_eq!(
        h.get_state()["boot_evidence_wait_remaining_s"],
        PyValue::Float(60.0)
    );

    // banner：解除等待、重臂 quiet（#130 不變），pending 仍在。
    h.inject_rx(BANNER);
    let s = h.get_state();
    assert_eq!(
        s["boot_evidence_wait_remaining_s"],
        PyValue::Null,
        "banner 即解除證據等待"
    );
    assert_eq!(
        s["boot_quiet_remaining_s"],
        PyValue::Float(180.0),
        "banner 重臂 quiet"
    );
    assert_eq!(s["ready_reconfirm_pending"], PyValue::Bool(true));
    let before = wp56_tx_counts(&h);
    h.advance(Duration::from_secs(5));
    assert_eq!(
        wp56_tx_counts(&h),
        before,
        "banner 後仍在 quiet：不送 probe"
    );
    assert!(wp56_pending(&h));

    // 開機完成（prompt）解除 quiet → RX 閒置後送確認 probe → nonce 實證 → confirm_ready。
    h.inject_rx(WP56_PROMPT);
    assert_eq!(
        h.get_state()["boot_quiet_remaining_s"],
        PyValue::Null,
        "prompt 解除 quiet"
    );
    advance_until(&h, Duration::from_millis(500), "確認 probe 成功", |h| {
        !wp56_pending(h)
    });
    assert!(wp56_tx_counts(&h).0 > before.0, "確認 probe 必須實際送出");
    assert!(
        wp56_tx_contains(&h, b"__READY__"),
        "WAL 記到 nonce probe TX"
    );
    let s = h.get_state();
    assert_eq!(s["state"], PyValue::from("READY"));
    assert_eq!(s["boot_evidence_wait_remaining_s"], PyValue::Null);
    assert_eq!(s["last_error"], PyValue::Null);
    wp56_follow_command_runs(&h);
}

#[test]
fn wp56_no_banner_resumes_after_sixty_seconds() {
    // `TestGraceExpiryDoesNotDeadlock::test_no_banner_confirms_after_grace`／
    // `test_grace_does_not_extend_ready_reconfirm_deadline`（`tests/test_async_reboot_evidence.py:190-232`）。
    let h = ActorRig::new(SHELL_PROFILE).ready();
    wp56_async_reboot(&h, "reboot", false);
    let s = h.get_state();
    assert_eq!(s["ready_reconfirm_remaining_s"], PyValue::Float(300.0));
    let before = wp56_tx_counts(&h);

    // 寬限內（59.9 s）零 probe；reconfirm 期限照原 deadline 遞減（寬限不延長 READY_RECONFIRM_MAX_S）。
    h.advance(Duration::from_secs(59));
    assert_eq!(wp56_tx_counts(&h), before, "寬限內零 probe");
    let s = h.get_state();
    assert_eq!(s["boot_evidence_wait_remaining_s"], PyValue::Float(1.0));
    assert_eq!(
        s["ready_reconfirm_remaining_s"],
        PyValue::Float(241.0),
        "原 deadline 不延長"
    );
    h.advance(Duration::from_millis(900));
    assert_eq!(wp56_tx_counts(&h), before, "59.9 s 仍在寬限內");
    assert!(wp56_pending(&h));

    // 到期（now >= until）後下一個 tick 照常送確認 probe，成功確認、不卡死。
    advance_until(
        &h,
        Duration::from_millis(100),
        "寬限到期後確認",
        |h| !wp56_pending(h),
    );
    assert!(
        wp56_tx_counts(&h).0 > before.0,
        "到期後確認 probe 必須實際送出"
    );
    assert!(wp56_tx_contains(&h, b"__READY__"));
    let s = h.get_state();
    assert_eq!(s["state"], PyValue::from("READY"));
    assert_eq!(s["boot_evidence_wait_remaining_s"], PyValue::Null);
    assert_eq!(s["ready_reconfirm_remaining_s"], PyValue::Null);
    assert_eq!(s["last_error"], PyValue::Null);
    wp56_follow_command_runs(&h);
}

#[test]
fn wp56_wait_ticks_preserve_reprobe_budget() {
    // `test_wait_does_not_burn_reprobe_attempts`（`tests/test_async_reboot_evidence.py:106-115`）：等待期間
    // 不排 probe——不累加 attempts、不排 backoff、不耗盡（`session_manager.py:2218-2223`）。
    let h = ActorRig::new(SHELL_PROFILE).ready();
    wp56_async_reboot(&h, "reboot", false);
    let before = wp56_tx_counts(&h);
    for round in 1..=5u32 {
        h.advance(Duration::from_secs(10));
        let s = h.get_state();
        assert_eq!(
            s["reprobe_attempts"],
            PyValue::from(0),
            "第 {round} 輪：不加 attempts"
        );
        assert_eq!(
            s["next_reprobe_at"],
            PyValue::Null,
            "第 {round} 輪：不排 backoff"
        );
        assert_eq!(
            s["reprobe_exhausted"],
            PyValue::Bool(false),
            "第 {round} 輪"
        );
        assert_eq!(
            s["ready_reconfirm_failed"],
            PyValue::Bool(false),
            "第 {round} 輪"
        );
        assert_eq!(
            s["ready_reconfirm_pending"],
            PyValue::Bool(true),
            "第 {round} 輪"
        );
        assert_eq!(
            s["boot_evidence_wait_remaining_s"],
            PyValue::Float(60.0 - 10.0 * f64::from(round)),
            "第 {round} 輪"
        );
    }
    assert_eq!(wp56_tx_counts(&h), before, "50 s 內 50 個 tick 皆零 TX");
}

#[test]
fn wp56_spontaneous_reboot_has_no_evidence_wait() {
    // `TestUnaffectedPaths::test_spontaneous_reboot_is_not_delayed`（`tests/test_async_reboot_evidence.py:264-277`）：
    // daemon 沒送 reboot → 沒有證據等待，banner → prompt → RX 閒置後立即確認（不等 60 s）。
    let h = ActorRig::new(SHELL_PROFILE).ready();
    wp56_arm_stale_shell(&h);
    h.inject_rx(BANNER);
    let s = h.get_state();
    assert_eq!(s["ready_reconfirm_pending"], PyValue::Bool(true));
    assert_eq!(
        s["boot_evidence_wait_remaining_s"],
        PyValue::Null,
        "自發重開機不起等待"
    );
    h.inject_rx(WP56_PROMPT);
    let before = wp56_tx_counts(&h);
    let start = h.clock.elapsed();
    advance_until(
        &h,
        Duration::from_millis(500),
        "自發重開機確認",
        |h| !wp56_pending(h),
    );
    let waited = (h.clock.elapsed() - start).as_secs_f64();
    assert!(
        waited < 10.0,
        "無證據等待：RX 閒置後即確認，實際 {waited} s"
    );
    assert!(wp56_tx_counts(&h).0 > before.0, "確認 probe 實際送出");
    assert_eq!(
        h.get_state()["boot_evidence_wait_remaining_s"],
        PyValue::Null
    );
}

#[test]
fn wp56_recovery_branch_clears_old_wait_without_starting_new_wait() {
    // `TestUnaffectedPaths::test_recovery_branch_does_not_start_wait`／`test_human_source_recovery_path_unchanged`
    // （`tests/test_async_reboot_evidence.py:279-302`）。

    // (1) agent reboot 2 s 內沒有 prompt → recovery 迴圈（EXPECT_REBOOT）：全程不起證據等待，恢復後仍無等待。
    let h = ActorRig::new(SHELL_PROFILE).ready();
    let id = h.submit_ok("reboot");
    wait_tx(&h, b"reboot\n");
    advance_until(&h, Duration::from_millis(200), "reboot 收尾", |h| {
        is_done(h, &id)
    });
    assert_eq!(
        h.command_get(&id)["recovery_action"],
        PyValue::from("EXPECT_REBOOT")
    );
    let s = h.get_state();
    assert_eq!(s["state"], PyValue::from("RECOVERING"));
    assert_eq!(
        s["boot_evidence_wait_remaining_s"],
        PyValue::Null,
        "recovery 分支不起等待"
    );
    wp56_arm_stale_shell(&h);
    h.inject_rx(BANNER);
    h.inject_rx(WP56_PROMPT);
    advance_until(&h, Duration::from_millis(500), "恢復 READY", |h| {
        state_of(h) == "READY"
    });
    let s = h.get_state();
    assert_eq!(s["ready_reconfirm_pending"], PyValue::Bool(false));
    assert_eq!(s["boot_evidence_wait_remaining_s"], PyValue::Null);

    // (2) 上一輪非同步 reboot 殘留等待時，新一輪 reboot 於送出命令前即歸零（`:6409-6411`），不延後到 TX 結算；
    // 2 s 內沒有 prompt 也不起新等待。等待期間 pending 擋 agent，故以可信 human（不受 agent gate 擋）送第二輪；
    // 不預掛 `\n` 應答，第二輪命令得不到 prompt。
    let h = ActorRig::new(SHELL_PROFILE).ready();
    wp56_async_reboot_unarmed(&h, "reboot", false);
    assert_eq!(
        h.get_state()["boot_evidence_wait_remaining_s"],
        PyValue::Float(60.0)
    );
    h.dev.script_write(vec![FakeWriteStep::PauseBeforeSubmit]);
    let submits = h.dev.os_submits();
    let id = h.submit_with(WP53_CHAINED, |r| {
        r.expect_reboot = true;
        r.trusted_human = true;
        r.source = "human:cid-7".into();
    });
    assert!(
        h.dev.wait_paused(Duration::from_secs(2)),
        "第二輪 reboot TX 應停在提交前"
    );
    assert_eq!(h.dev.os_submits(), submits, "尚未 OS 提交");
    assert_eq!(
        h.get_state()["boot_evidence_wait_remaining_s"],
        PyValue::Null,
        "送出前即歸零舊等待"
    );
    h.dev.release_pause();
    wait_tx(&h, b"echo a && reboot\n");
    advance_until(
        &h,
        Duration::from_millis(200),
        "第二輪 reboot 收尾",
        |h| is_done(h, &id),
    );
    let rec = h.command_get(&id);
    assert_eq!(
        rec["partial"],
        PyValue::Bool(true),
        "走 2 s 無 prompt 的 recovery 分支：{rec:?}"
    );
    assert!(rec["recovery_action"].as_str().is_some(), "{rec:?}");
    assert_eq!(
        h.get_state()["boot_evidence_wait_remaining_s"],
        PyValue::Null,
        "recovery 分支不起新等待"
    );
}

#[test]
fn wp56_detach_clears_evidence_wait() {
    // `TestUnaffectedPaths::test_detach_clears_wait`（`tests/test_async_reboot_evidence.py:338-343`）：
    // 無 bridge 的 `_detach_session_locked`（`session_manager.py:2704`）歸零等待；quiet／reconfirm 照既有規則保留。
    // 有 bridge 的 drain 收斂 `_complete_drain`（`:2884-2960`）不碰 `boot_evidence_wait_until`：保留到重掛後
    // 登入確認 `confirm_ready()`（`:718`）才清；新 attach 的 probe gate 只看 quiet（`_attach_by_id` `:5032`）。

    // (1) 有 worker：拔除 → drain 收斂 DETACHED，等待保留；接著無 worker 的 clear → 歸零。
    let h = ActorRig::new(SHELL_PROFILE).ready();
    wp56_async_reboot(&h, "reboot", false);
    h.send_control(ControlEvent::DeviceDetached {
        by_id: DEVICE_BY_ID.into(),
        reason: sw_core::mailbox::DetachReason::Removed,
    });
    h.wait_state("DETACHED");
    let s = h.get_state();
    assert_eq!(s["last_error"], PyValue::from("DEVICE_REMOVED"));
    assert_eq!(
        s["boot_evidence_wait_remaining_s"],
        PyValue::Float(60.0),
        "有 worker 的 drain 收斂不清等待（`_complete_drain`）"
    );
    assert_eq!(s["ready_reconfirm_pending"], PyValue::Bool(true));
    let r = h.request(SessionRequest::Clear);
    assert_eq!(r["ok"], PyValue::Bool(true), "{r:?}");
    let s = h.get_state();
    assert_eq!(s["state"], PyValue::from("DETACHED"));
    assert_eq!(s["last_error"], PyValue::from("CLEARED"));
    assert_eq!(
        s["boot_evidence_wait_remaining_s"],
        PyValue::Null,
        "無 worker 的 detach 歸零等待（`_detach_session_locked`）"
    );
    assert_eq!(
        s["ready_reconfirm_pending"],
        PyValue::Bool(true),
        "只清等待，reconfirm 保存規則不變"
    );

    // (2) 有 worker：drain 後重插 → 新 attach 照常送登入 probe（等待不擋），成功 confirm_ready 歸零。
    let h = ActorRig::new(SHELL_PROFILE).ready();
    wp56_async_reboot(&h, "reboot", false);
    h.send_control(ControlEvent::DeviceDetached {
        by_id: DEVICE_BY_ID.into(),
        reason: sw_core::mailbox::DetachReason::Removed,
    });
    h.wait_state("DETACHED");
    assert_eq!(
        h.get_state()["boot_evidence_wait_remaining_s"],
        PyValue::Float(60.0)
    );
    let before = wp56_tx_counts(&h);
    let h = h.attach();
    h.wait_state("READY");
    assert!(
        wp56_tx_counts(&h).0 > before.0,
        "新 attach 實際送出登入 probe"
    );
    let s = h.get_state();
    assert_eq!(s["boot_evidence_wait_remaining_s"], PyValue::Null);
    assert_eq!(s["ready_reconfirm_pending"], PyValue::Bool(false));
}

/// async reboot（等待 60 s 起算）後以有 worker 的 clear 讓 session 進 Attaching、開埠在途，在此窗口送入 `queue` 的事件
/// （Rust `pending_open_stop`，即 Python attach op 在途、`session.bridge is None` 的窗口）。排程：暫停在 clear 的 drain
/// 回條（`WorkerStopped`）前，改掛「下一筆 Control」暫停後放行——`request_reattach` 的 `Effect::Reattach` 由替身回送
/// `DeviceAttached`，actor 停在它之前時排入 `queue`；放行後 `device_attached` 的 OpenBridge 於 actor thread 同步執行，
/// `BridgeOpened` 落在 control lane 末端，已排入的事件（safety lane 優先、control lane FIFO）因此在開埠收斂前處理。
/// `queue` 之後、放行之前呼叫 `before_resume`（例如讓開埠失敗）。回傳暫停前的 open 次數與 TX 計數。
fn wp56_stop_while_attaching(
    h: &ActorRig,
    queue: impl FnOnce(&ActorRig),
    before_resume: impl FnOnce(&ActorRig),
) -> (usize, (usize, usize, usize)) {
    wp56_async_reboot(h, "reboot", false);
    assert_eq!(
        h.get_state()["boot_evidence_wait_remaining_s"],
        PyValue::Float(60.0)
    );
    let opens = h.dev.open_count();
    let tx = wp56_tx_counts(h);
    let receipt = h.pause_before_event("WorkerStopped");
    let clear = h.request_async(SessionRequest::Clear);
    receipt
        .entered
        .recv_timeout(Duration::from_secs(5))
        .expect("clear 應進入 drain 回條");
    let attached = h.pause_before_event("Control");
    receipt.resume.send(()).expect("放行 drain 回條");
    attached
        .entered
        .recv_timeout(Duration::from_secs(5))
        .expect("Reattach 應回送 DeviceAttached");
    queue(h);
    // 送入的事件須在放行前已入列。
    std::thread::sleep(Duration::from_millis(30));
    before_resume(h);
    attached.resume.send(()).expect("放行 DeviceAttached");
    let r = clear
        .recv_timeout(Duration::from_secs(5))
        .expect("clear 應收斂")
        .expect("clear 不應丟例外");
    assert_eq!(r["ok"], PyValue::Bool(true), "{r:?}");
    (opens, tx)
}

#[test]
fn wp56_release_during_attaching_clears_evidence_wait() {
    // 審查 P8-21072a2-1：Python `_finish_attach_op`（`origin/main:sw_core/session_manager.py:4339-4344`）——attach op 在途
    // 收到 release，`release_request` 分支以 `_detach_session_locked`（`:2704`）同步 detach、歸零
    // `boot_evidence_wait_until`；quiet／READY reconfirm 照既有規則保留。Rust 對應：`pending_open_stop`（kind=Release）
    // 於開埠收斂時——成功走 `drain_opened_bridge`、失敗走 `finish_pending_open_stop`——都要歸零。

    // (1) 開埠成功：bridge 立即進 drain（`drain_opened_bridge`）→ RELEASED。
    let h = ActorRig::new(SHELL_PROFILE).ready();
    let release = RefCell::new(None);
    let (opens, tx) = wp56_stop_while_attaching(
        &h,
        |h| {
            *release.borrow_mut() = Some(h.request_async(SessionRequest::Release {
                source: "cli".into(),
                reason: PyValue::Null,
            }));
        },
        |_| {},
    );
    let r = release
        .into_inner()
        .unwrap()
        .recv_timeout(Duration::from_secs(5))
        .expect("release 應收斂")
        .expect("release 不應丟例外");
    assert_eq!(r["ok"], PyValue::Bool(true), "{r:?}");
    h.wait_state("RELEASED");
    assert_eq!(
        h.dev.open_count(),
        opens + 1,
        "clear 收斂後確實重開 bridge，release 落在開埠在途窗口（`drain_opened_bridge`）"
    );
    let s = h.get_state();
    assert_eq!(
        s["boot_evidence_wait_remaining_s"],
        PyValue::Null,
        "開埠在途的 release 同步 detach 歸零等待（`_finish_attach_op` → `_detach_session_locked`）"
    );
    assert_eq!(
        s["ready_reconfirm_pending"],
        PyValue::Bool(true),
        "只清等待，reconfirm 保存規則不變"
    );
    assert_eq!(wp56_tx_counts(&h), tx, "整段零 TX（重開的 bridge 立即 drain、不登入不 probe）");

    // (2) 開埠失敗：`finish_pending_open_stop` → RELEASED。
    let h = ActorRig::new(SHELL_PROFILE).ready();
    let release = RefCell::new(None);
    let (opens, tx) = wp56_stop_while_attaching(
        &h,
        |h| {
            *release.borrow_mut() = Some(h.request_async(SessionRequest::Release {
                source: "cli".into(),
                reason: PyValue::Null,
            }));
        },
        |h| h.dev.remove(),
    );
    let r = release
        .into_inner()
        .unwrap()
        .recv_timeout(Duration::from_secs(5))
        .expect("release 應收斂")
        .expect("release 不應丟例外");
    assert_eq!(r["ok"], PyValue::Bool(true), "{r:?}");
    h.wait_state("RELEASED");
    assert_eq!(h.dev.open_count(), opens, "開埠失敗（NotFound），未接上新 bridge");
    let s = h.get_state();
    assert_eq!(
        s["boot_evidence_wait_remaining_s"],
        PyValue::Null,
        "開埠失敗的收斂同樣歸零等待（`_finish_attach_op` 於 open 例外後亦執行）"
    );
    assert_eq!(s["ready_reconfirm_pending"], PyValue::Bool(true));
    assert_eq!(wp56_tx_counts(&h), tx, "整段零 TX");
}

#[test]
fn wp56_removed_during_attaching_clears_evidence_wait() {
    // 審查 P8-21072a2-1 同一缺口的拔除面：Python `_detach_by_id`（`origin/main:sw_core/session_manager.py:4462-4470`）
    // 在 `session.bridge is None`（attach op 在途）時 `_begin_drain_locked` 回 None，改以 `_detach_session_locked`
    // 同步 detach、歸零 `boot_evidence_wait_until`。Rust 對應：`pending_open_stop`（kind=Removed）。
    let h = ActorRig::new(SHELL_PROFILE).ready();
    let (opens, tx) = wp56_stop_while_attaching(
        &h,
        |h| {
            h.send_control(ControlEvent::DeviceDetached {
                by_id: DEVICE_BY_ID.into(),
                reason: sw_core::mailbox::DetachReason::Removed,
            })
        },
        |_| {},
    );
    h.wait_state("DETACHED");
    assert_eq!(
        h.dev.open_count(),
        opens + 1,
        "拔除落在 clear 重開 bridge 的開埠在途窗口"
    );
    let s = h.get_state();
    assert_eq!(s["last_error"], PyValue::from("DEVICE_REMOVED"));
    assert_eq!(
        s["boot_evidence_wait_remaining_s"],
        PyValue::Null,
        "bridge 尚未接上的拔除為同步 detach，歸零等待（`:4470` → `_detach_session_locked`）"
    );
    assert_eq!(
        s["ready_reconfirm_pending"],
        PyValue::Bool(true),
        "只清等待，reconfirm 保存規則不變"
    );
    assert_eq!(wp56_tx_counts(&h), tx, "整段零 TX");
}

#[test]
fn wp56_plain_command_no_wait_and_chained_expect_reboot_starts_wait() {
    // `test_non_reboot_command_does_not_start_wait`／`test_execute_command_reboot_path_starts_wait`／
    // `test_chained_reboot_via_handler_starts_wait`（`tests/test_async_reboot_evidence.py:304-336`）：判定掛在 reboot
    // handler，`expect_reboot` 串接命令（命令文字猜不到）與單獨 `reboot` 走同一入口。
    let h = ActorRig::new(SHELL_PROFILE).ready();
    wp56_follow_command_runs(&h);
    let s = h.get_state();
    assert_eq!(
        s["boot_evidence_wait_remaining_s"],
        PyValue::Null,
        "一般命令不起等待"
    );
    assert_eq!(s["ready_reconfirm_pending"], PyValue::Bool(false));

    assert!(!sw_core::quiet_gate::is_reboot_command("sync && reboot"));
    wp56_async_reboot(&h, "sync && reboot", true);
    assert_eq!(
        h.get_state()["boot_evidence_wait_remaining_s"],
        PyValue::Float(60.0),
        "expect_reboot 串接命令同樣起算等待"
    );
    let before = wp56_tx_counts(&h);
    h.advance(Duration::from_secs(10));
    assert_eq!(wp56_tx_counts(&h), before, "串接命令後同樣不送確認 probe");
    assert!(wp56_pending(&h));
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
    assert_eq!(rec["error_code"], PyValue::Null, "{rec:?}");
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
// WP5-3 第 4 項：passthrough 旗標命令與 SC-35 的底層 TX gate（G3；裁決 s2 D2）。Python
// `origin/main:sw_core/session_manager.py:814-826` 的 `agent_gate_active()` 對 passthrough 固定回 False，
// `_push_tx_gate()`（`:778-785`）以此值發布 bridge 的 `_gate_quiet`（`uart_io.py:640` 只擋 `agent`）：
// 旗標命令照常走 `_handle_reboot_command` 並 `arm_boot_quiet()`（quiet／`ready_reconfirm_pending` 照記），
// 但 quiet／pending 不擋 passthrough 後續合法 agent TX。
// ---------------------------------------------------------------------------

/// U-Boot 的重開命令（`reset`）帶旗標：回 `=> ` 完成後，reboot 的 reconfirm 記錄仍保留（fence 與公開狀態，
/// 不以清空假裝完成 SC-35）；下一筆合法 agent 命令必須真正寫入 UART 並成功，不只是 submit accepted。
#[test]
fn wp53_passthrough_flag_followup_tx_not_gated() {
    let h = passthrough_ready(UBOOT_PASSTHROUGH_PROFILE);
    h.dev
        .on_tx_reply(b"reset\n", b"reset\r\nresetting ...\r\n=> ");
    let id = h.submit_with("reset", |r| {
        r.timeout_s = 6.0;
        r.expect_reboot = true;
    });
    let rec = h.wait_done(&id);
    assert_eq!(rec["status"], PyValue::from("done"), "{rec:?}");
    assert_eq!(rec["partial"], PyValue::Bool(false), "{rec:?}");
    assert_eq!(rec["recovery_action"], PyValue::Null, "{rec:?}");
    assert!(wal_tx_has(&h, b"reset\n"), "旗標命令本身已寫出");
    // Python `arm_boot_quiet()` 不分 platform：pending 由 False 翻 True；prompt 回來只 `clear_boot_quiet()`。
    assert!(
        h.fence.with(|f| f.reconfirm_pending),
        "fence 保留 reboot 的 reconfirm 記錄"
    );
    let s = h.get_state();
    assert_eq!(s["state"], PyValue::from("READY"), "{s:?}");
    assert_eq!(s["ready_reconfirm_pending"], PyValue::Bool(true), "{s:?}");

    let submits = h.dev.os_submits();
    let _ = h.dev.take_tx();
    h.dev
        .on_tx_reply(b"printenv\n", b"printenv\r\nbootcmd=x\r\n=> ");
    let next = h.submit_ok("printenv");
    let rec = h.wait_done(&next);
    assert_eq!(rec["status"], PyValue::from("done"), "{rec:?}");
    assert_eq!(rec["error_code"], PyValue::Null, "{rec:?}");
    assert_eq!(rec["partial"], PyValue::Bool(false), "{rec:?}");
    assert_eq!(rec["stdout"], PyValue::from("bootcmd=x"), "{rec:?}");
    assert!(
        h.dev.os_submits() > submits,
        "後續 agent 命令必須真正提交到 OS"
    );
    assert_eq!(
        h.dev.take_tx(),
        b"printenv\n".to_vec(),
        "UART 實際寫出後續命令"
    );
    assert!(wal_tx_has(&h, b"printenv\n"), "WAL 記錄後續命令 TX");
    assert!(!wp53_recover_keys_sent(&h), "passthrough 不送救援鍵");
    assert_eq!(state_of(&h), "READY");
}

/// 文字為 `reboot` 的旗標命令在 passthrough 不屬「猜中」（`guessed_reboot = not passthrough and ...`），
/// 等待期限為 `max(timeout_s, expected or 0)` = 6 s：prompt 3 s 後才回來仍正常完成，不得套兩秒規則。
#[test]
fn wp53_passthrough_plain_reboot_flag_uses_full_wait() {
    let h = passthrough_ready(UBOOT_PASSTHROUGH_PROFILE);
    h.dev.on_tx_reply(b"reboot\n", b"reboot\r\n");
    let id = h.submit_with("reboot", |r| {
        r.timeout_s = 6.0;
        r.expect_reboot = true;
    });
    let start = wp53_wait_started(&h, b"reboot\n");
    let early = wp53_step_until(&h, start, 3.0, |h| is_done(h, &id));
    assert_eq!(
        early,
        None,
        "prompt 未回不得結算（兩秒規則不適用 passthrough）：{:?}",
        h.command_get(&id)
    );
    h.inject_rx(b"\r\n=> ");
    let rec = h.wait_done(&id);
    assert_eq!(rec["status"], PyValue::from("done"), "{rec:?}");
    assert_eq!(rec["error_code"], PyValue::Null, "{rec:?}");
    assert_eq!(rec["partial"], PyValue::Bool(false), "{rec:?}");
    assert_eq!(rec["recovery_action"], PyValue::Null, "{rec:?}");
    assert!(!wp53_recover_keys_sent(&h), "passthrough 不送救援鍵");
    assert_eq!(state_of(&h), "READY");
}

/// `adopt_opened_bridge` 每次 adopt 都以目前 profile 明確覆寫 fence 的 `bridge_passthrough`（Python
/// `_push_tx_gate` 每次發布都重算 `agent_gate_active()`）：passthrough 為 true；shell 即使 fence 殘留 true 也覆寫為 false。
#[test]
fn wp53_passthrough_adopt_publishes_bridge_flag() {
    let mut h = CoreHarness::new(UBOOT_PASSTHROUGH_PROFILE);
    assert!(!h.fence.with(|f| f.bridge_passthrough), "預設 false");
    h.attach_device();
    h.bridge_ok();
    assert!(
        h.fence.with(|f| f.bridge_passthrough),
        "passthrough adopt 發布 true"
    );

    let mut h = CoreHarness::new(SHELL_PROFILE);
    h.fence.with(|f| f.bridge_passthrough = true);
    h.attach_device();
    h.bridge_ok();
    assert!(
        !h.fence.with(|f| f.bridge_passthrough),
        "shell adopt 明確覆寫為 false"
    );
}

// ---------------------------------------------------------------------------
// WP5-3 第 5 項：`command_expect_reboot` WAL META（G3；Python `origin/main:sw_core/session_manager.py:6842-6851`、
// 契約 `contracts/wal_meta_events.schema.json:63-85`）。只在旗標 true 時寫，位置在 reboot 分流與命令 UART TX 之前；
// 不分命令文字是否猜中、也不排除 passthrough（裁決 s2 D2）。
// ---------------------------------------------------------------------------

/// WAL 紀錄中隨時間變動的欄位（`seq`／`mono_ts_ns`／`wall_ts`）；其餘欄位逐位元組比對。
const WP53_DYNAMIC_WAL_KEYS: [&str; 3] = ["seq", "mono_ts_ns", "wall_ts"];

fn wp53_seq(row: &PyDict) -> i64 {
    row.get("seq")
        .and_then(PyValue::as_i64)
        .unwrap_or_else(|| panic!("WAL 紀錄缺 seq：{row:?}"))
}

/// 去除動態欄位後以 RPC_WIRE 序列化（WAL 落盤為 sort_keys，載回的鍵順序即落盤順序）。
fn wp53_static_row(row: &PyDict) -> String {
    let pairs = row
        .0
        .iter()
        .filter(|(k, _)| !WP53_DYNAMIC_WAL_KEYS.contains(&k.as_str()))
        .map(|(k, v)| (k.clone(), v.clone()))
        .collect();
    sw_proto::pyjson::dumps(
        &PyValue::Dict(PyDict::from_pairs(pairs)),
        &sw_proto::pyjson::RPC_WIRE,
    )
}

/// 契約 `command_expect_reboot` 的完整外層紀錄（去除動態欄位）：`dir=META`、`source=system`、非 null `cmd_id`、
/// 空 payload（`len=0`、空 payload 的 CRC32 `00000000`）、`meta={"event":"command_expect_reboot","expect_reboot":true}`。
fn wp53_expected_meta_row(cmd_id: &str) -> String {
    format!(
        "{{\"cmd_id\":\"{cmd_id}\",\"com\":\"COM0\",\"crc32\":\"00000000\",\"dir\":\"META\",\"len\":0,\
         \"loss_flag\":false,\"meta\":{{\"event\":\"command_expect_reboot\",\"expect_reboot\":true}},\
         \"payload_b64\":\"\",\"source\":\"system\"}}"
    )
}

fn wp53_meta_event(row: &PyDict) -> Option<String> {
    match row.get("meta") {
        Some(PyValue::Dict(m)) => m.get("event").and_then(PyValue::as_str).map(str::to_string),
        _ => None,
    }
}

/// WAL 內 `event == command_expect_reboot` 的 META 紀錄（整筆外層紀錄）。
fn wp53_expect_reboot_rows(h: &ActorRig) -> Vec<PyDict> {
    h.wal_all_rows()
        .into_iter()
        .filter(|r| r.get("dir").and_then(PyValue::as_str) == Some("META"))
        .filter(|r| wp53_meta_event(r).as_deref() == Some("command_expect_reboot"))
        .collect()
}

/// WAL 內屬於 `cmd_id` 的紀錄（任何 dir），依寫入順序。
fn wp53_cmd_rows(h: &ActorRig, cmd_id: &str) -> Vec<PyDict> {
    h.wal_all_rows()
        .into_iter()
        .filter(|r| r.get("cmd_id").and_then(PyValue::as_str) == Some(cmd_id))
        .collect()
}

/// 旗標命令恰有一筆契約形狀的 `command_expect_reboot` META，且它是本 cmd_id 的第一筆 WAL 紀錄，
/// seq 小於本命令 TX 的 seq（送 UART 前寫入）。
fn assert_wp53_meta_before_tx(h: &ActorRig, case: &str, cmd_id: &str, tx: &[u8]) {
    let metas = wp53_expect_reboot_rows(h);
    assert_eq!(metas.len(), 1, "{case}：旗標命令恰一筆 META：{metas:?}");
    assert_eq!(
        wp53_static_row(&metas[0]),
        wp53_expected_meta_row(cmd_id),
        "{case}：META 外層紀錄須符合契約"
    );
    let rows = wp53_cmd_rows(h, cmd_id);
    let tx_rows: Vec<&PyDict> = rows
        .iter()
        .filter(|r| r.get("dir").and_then(PyValue::as_str) == Some("TX") && b64(r) == tx)
        .collect();
    assert_eq!(tx_rows.len(), 1, "{case}：本命令 TX 恰一筆：{rows:?}");
    assert!(
        wp53_seq(&metas[0]) < wp53_seq(tx_rows[0]),
        "{case}：META seq {} 須小於命令 TX seq {}",
        wp53_seq(&metas[0]),
        wp53_seq(tx_rows[0])
    );
    assert_eq!(
        wp53_seq(&rows[0]),
        wp53_seq(&metas[0]),
        "{case}：META 為本 cmd_id 的第一筆 WAL 紀錄：{rows:?}"
    );
}

/// 對照 `tests/test_expect_reboot.py::test_flag_routes_chained_command_to_reboot_handler` 的
/// `any(event == "command_expect_reboot")`：Python 於 `if expect_reboot or guessed_reboot` 分流之前寫 META，
/// 不看命令文字是否猜中，也不排除 passthrough。三個獨立 rig：串接命令、單獨 `reboot`（猜中＋旗標）、
/// passthrough 的 `reset`。
#[test]
fn wp53_meta_before_command_tx() {
    let h = ActorRig::new(SHELL_PROFILE).ready();
    let id = h.submit_with(WP53_CHAINED, |r| {
        r.timeout_s = 1.0;
        r.expect_reboot = true;
    });
    wait_tx(&h, b"echo a && reboot\n");
    advance_until(&h, Duration::from_millis(100), "串接旗標命令收尾", |h| {
        is_done(h, &id)
    });
    assert_eq!(
        h.command_get(&id)["recovery_action"],
        PyValue::from("EXPECT_REBOOT")
    );
    assert_wp53_meta_before_tx(&h, "串接命令", &id, b"echo a && reboot\n");

    let h = ActorRig::new(SHELL_PROFILE).ready();
    let id = h.submit_with("reboot", |r| {
        r.timeout_s = 10.0;
        r.expect_reboot = true;
    });
    wait_tx(&h, b"reboot\n");
    advance_until(&h, Duration::from_millis(100), "單獨 reboot 旗標命令收尾", |h| {
        is_done(h, &id)
    });
    assert_wp53_meta_before_tx(&h, "單獨 reboot", &id, b"reboot\n");

    let h = passthrough_ready(UBOOT_PASSTHROUGH_PROFILE);
    h.dev
        .on_tx_reply(b"reset\n", b"reset\r\nresetting ...\r\n=> ");
    let id = h.submit_with("reset", |r| {
        r.timeout_s = 6.0;
        r.expect_reboot = true;
    });
    let rec = h.wait_done(&id);
    assert_eq!(rec["partial"], PyValue::Bool(false), "{rec:?}");
    assert_wp53_meta_before_tx(&h, "passthrough reset", &id, b"reset\n");
}

/// 對照 `tests/test_expect_reboot.py::test_flag_wins_over_completion_regex`：旗標＋合法 `completion_regex` 時
/// 只寫 `command_expect_reboot`，不得寫 `command_completion`（Python 的 completion META 在 reboot 分流之後）。
#[test]
fn wp53_meta_flag_suppresses_completion_meta() {
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
    let events: Vec<Option<String>> = wp53_cmd_rows(&h, &id)
        .iter()
        .filter(|r| r.get("dir").and_then(PyValue::as_str) == Some("META"))
        .map(wp53_meta_event)
        .collect();
    assert_eq!(
        events,
        vec![Some("command_expect_reboot".to_string())],
        "本 cmd_id 的 META 只有 command_expect_reboot"
    );
    assert_wp53_meta_before_tx(&h, "旗標＋completion_regex", &id, b"echo a && reboot\n");
}

/// 無旗標對照（`tests/test_expect_reboot.py::test_without_flag_chained_command_keeps_old_behaviour`、
/// `::test_plain_reboot_without_flag_is_unchanged`）：串接命令與猜中的單獨 `reboot` 都不寫 expect-reboot META；
/// 一般 completion 命令維持原本恰一筆 `command_completion` META（鍵與值不變）。
#[test]
fn wp53_meta_absent_without_flag() {
    let h = ActorRig::new(SHELL_PROFILE).ready();
    h.dev.on_tx_reply(b"\x03", b"^C\r\nroot@t:/# ");
    let id = h.submit_with(WP53_CHAINED, |r| r.timeout_s = 1.0);
    wait_tx(&h, b"echo a && reboot\n");
    advance_until(&h, Duration::from_millis(100), "無旗標串接命令收尾", |h| {
        is_done(h, &id)
    });
    assert_eq!(h.command_get(&id)["recovery_action"], PyValue::from("CTRL_C"));
    assert!(
        wp53_expect_reboot_rows(&h).is_empty(),
        "無旗標串接命令不寫 META：{:?}",
        wp53_expect_reboot_rows(&h)
    );

    let h = ActorRig::new(SHELL_PROFILE).ready();
    let id = h.submit_with("reboot", |r| r.timeout_s = 10.0);
    wait_tx(&h, b"reboot\n");
    advance_until(&h, Duration::from_millis(100), "無旗標 reboot 收尾", |h| {
        is_done(h, &id)
    });
    assert_eq!(
        h.command_get(&id)["recovery_action"],
        PyValue::from("EXPECT_REBOOT"),
        "猜中的單獨 reboot 仍走 reboot 分支"
    );
    assert!(
        wp53_expect_reboot_rows(&h).is_empty(),
        "無旗標 reboot 不寫 META：{:?}",
        wp53_expect_reboot_rows(&h)
    );

    let h = ActorRig::new(SHELL_PROFILE).ready();
    h.dev
        .on_tx_reply(b"runit\n", b"runit\r\nout\r\nRC=0\r\nroot@t:/# ");
    let id = h.submit_with("runit", |r| {
        r.completion_regex = Some("(?m)^RC=0\\r?$".into())
    });
    let rec = h.wait_done(&id);
    assert_eq!(rec["stdout"], PyValue::from("out\nRC=0"), "{rec:?}");
    let metas: Vec<PyDict> = wp53_cmd_rows(&h, &id)
        .into_iter()
        .filter(|r| r.get("dir").and_then(PyValue::as_str) == Some("META"))
        .collect();
    assert_eq!(metas.len(), 1, "{metas:?}");
    assert_eq!(
        wp53_static_row(&metas[0]),
        format!(
            "{{\"cmd_id\":\"{id}\",\"com\":\"COM0\",\"crc32\":\"00000000\",\"dir\":\"META\",\"len\":0,\
             \"loss_flag\":false,\"meta\":{{\"completion_regex\":\"(?m)^RC=0\\\\r?$\",\"event\":\"command_completion\"}},\
             \"payload_b64\":\"\",\"source\":\"system\"}}"
        ),
        "一般 completion META 不變"
    );
    assert!(wp53_expect_reboot_rows(&h).is_empty());
}

/// 被 submit-time gate（尚未 READY）或 execute-time gate（agent interactive lease）拒絕的旗標命令不寫 META，且零 TX
/// （Python META 位於 `execute_command` 的 gate（`session_manager.py:6561-6592`）與 `_execute_command_inner` 的
/// operation 檢查之後）。
#[test]
fn wp53_meta_not_written_when_gated() {
    // 假 DUT 不回 prompt：登入 probe 卡住，session 停在 READY 之前（ATTACHING／ATTACHED 皆回 SESSION_NOT_READY）。
    let h = ActorRig::new(SHELL_PROFILE).attach();
    let mut req = agent_submit(WP53_CHAINED);
    req.expect_reboot = true;
    let resp = h.request(SessionRequest::Submit(req));
    assert_eq!(resp["error_code"], PyValue::from("SESSION_NOT_READY"), "{resp:?}");
    assert!(wp53_expect_reboot_rows(&h).is_empty(), "submit-time 拒絕不寫 META");
    assert!(!wal_tx_has(&h, b"echo a && reboot\n"), "submit-time 拒絕零 TX");

    let h = ActorRig::new(SHELL_PROFILE).ready();
    let open = h.request(SessionRequest::InteractiveOpen {
        owner: "agent:wp53".into(),
        timeout_s: 20.0,
        command: String::new(),
        allow_attached: false,
    });
    let iid = open["interactive_id"].as_str().unwrap().to_string();
    let id = h.submit_with(WP53_CHAINED, |r| r.expect_reboot = true);
    let rec = h.wait_done(&id);
    assert_eq!(
        rec["error_code"],
        PyValue::from("SESSION_INTERACTIVE_BUSY"),
        "{rec:?}"
    );
    assert!(wp53_expect_reboot_rows(&h).is_empty(), "execute-time 拒絕不寫 META");
    assert!(wp53_cmd_rows(&h, &id).is_empty(), "execute-time 拒絕零 WAL 紀錄");
    assert!(!wal_tx_has(&h, b"echo a && reboot\n"), "execute-time 拒絕零 TX");
    let close = h.request(SessionRequest::InteractiveClose {
        interactive_id: iid,
    });
    assert_eq!(close["ok"], PyValue::Bool(true));
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
// Python 的前景 operation，不設 `foreground_busy`、不擋 agent log RX。Rust 0.5.1 起 self-test 依 BC-13
// 在前景忙碌時被動回報；recover 仍照 F25 行為送救援鍵。
// ---------------------------------------------------------------------------

const PRPL_BOOT_DONE_PROFILE: &str = "profiles:\n  r:\n    platform: prpl\n    prompt_regex: '(?m)^root@prplOS:.*# '\n    boot_done_regex: 'Please press Enter to activate this console'\n    timeout_s: 2.0\ntargets:\n  - {com: COM0, profile: r, act_no: 1, alias: r+1, device_by_id: /s/by-id/usb-A}\n";

fn ready_prpl_with_boot_done() -> ActorRig {
    let h = ActorRig::new(PRPL_BOOT_DONE_PROFILE);
    h.dev.on_tx_reply(b"\n", b"root@prplOS:~# ");
    h.dev.on_tx_match(
        r"__READY__[0-9a-f]+",
        Box::new(|line: &[u8]| {
            let text = String::from_utf8_lossy(line).into_owned();
            let token = text
                .split("__READY__")
                .nth(1)
                .unwrap_or("")
                .trim()
                .to_string();
            format!("__READY__{token}\r\nroot@prplOS:~# ").into_bytes()
        }),
    );
    let h = h.attach();
    h.wait_state("READY");
    h
}

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
    // `sends_probe` 是歷史名稱；依 BC-13，這個忙碌情境現在必須被動回覆。
    let (h, _id) = ready_with_hung_command();
    h.wait_for("前景命令 TX 完整提交", |h| {
        h.dev.os_submits() > 0 && h.dev.tx_since(0).ends_with(b"sleep 20\n")
    });
    let before_bytes = h.dev.tx_len();
    let before_writes = h.dev.write_calls();
    let before_submits = h.dev.os_submits();
    let before_wal_tx = tx_rows(&h).len();
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
        .expect("self-test busy");
    assert_eq!(r["classification"], PyValue::from("SESSION_BUSY"));
    assert_eq!(r["recommended_action"], PyValue::from("wait"));
    assert!(r.get("probe_ok").is_none(), "{r:?}");
    assert_eq!(h.dev.tx_len(), before_bytes, "前景命令在途時不得新增 UART bytes");
    assert_eq!(h.dev.write_calls(), before_writes, "前景命令在途時不得新增 UART write");
    assert_eq!(h.dev.os_submits(), before_submits, "前景命令在途時不得新增 OS submit");
    assert_eq!(tx_rows(&h).len(), before_wal_tx, "前景命令在途時不得新增 WAL TX");
}

#[test]
fn self_test_during_recover_after_boot_done_is_passive() {
    let h = ready_prpl_with_boot_done();
    h.dev.on_tx_reply(b"\x03", BANNER);
    h.dev.on_tx_reply(b"\x04", b"\r\nroot@prplOS:~# ");

    let recover = h.request_async(SessionRequest::Recover {
        timeout_s: 2.0,
        force: false,
    });
    h.wait_for("Recover Ctrl-C 後 U-Boot quiet 已 arm", |h| {
        tx_rows(h)
            .iter()
            .any(|row| b64(row) == b"\x03" && row["source"] == "system:recover")
            && h.get_state()
                .get("boot_quiet_remaining_s")
                .is_some_and(|remaining| !remaining.is_null())
    });

    h.inject_rx(b"\r\nPlease press Enter to activate this console\r\n");
    h.wait_for(
        "boot_done_regex 解除 quiet 且仍等待 READY 再確認",
        |h| {
            let session = h.get_state();
            session.get("boot_quiet_remaining_s") == Some(&PyValue::Null)
                && session.get("ready_reconfirm_pending") == Some(&PyValue::Bool(true))
        },
    );

    let before_bytes = h.dev.tx_len();
    let before_writes = h.dev.write_calls();
    let before_submits = h.dev.os_submits();
    let before_wal_tx = tx_rows(&h).len();
    let self_test = h.request_async(SessionRequest::SelfTest {
        timeout_s: 2.0,
        strict_human_lock: false,
    });
    let passive_reply = self_test.recv_timeout(Duration::from_secs(1));
    let after_self_test = (
        h.dev.tx_len(),
        h.dev.write_calls(),
        h.dev.os_submits(),
        tx_rows(&h).len(),
    );

    // FakeClock 仍停在原時刻時，Recover 的首鍵等待不會自行逾時；推進後須照常送 Ctrl-D。
    h.advance(Duration::from_secs(2));
    let recovered = recover
        .recv_timeout(Duration::from_secs(5))
        .expect("Recover 回覆")
        .expect("Recover ok");
    assert_eq!(
        recovered["error_code"],
        PyValue::from("PROMPT_TIMEOUT_RECOVERED")
    );
    assert_eq!(recovered["recovery_action"], PyValue::from("CTRL_D"));
    assert!(wal_tx_has(&h, b"\x04"), "Recover 後續鍵 Ctrl-D 應照常送出");

    assert!(
        passive_reply.is_ok(),
        "self-test 必須在 Recover 後續鍵送出前被動回覆，實際為 {passive_reply:?}"
    );
    let result = passive_reply
        .expect("已收到被動 self-test 回覆")
        .expect("self-test ok");
    assert_eq!(result["classification"], PyValue::from("SESSION_BUSY"));
    assert_eq!(result["recommended_action"], PyValue::from("wait"));
    assert!(result.get("probe_ok").is_none(), "{result:?}");
    assert_eq!(
        after_self_test.0, before_bytes,
        "Recover 等待中 self-test 不得新增 UART bytes"
    );
    assert_eq!(
        after_self_test.1, before_writes,
        "Recover 等待中 self-test 不得新增 UART write"
    );
    assert_eq!(
        after_self_test.2, before_submits,
        "Recover 等待中 self-test 不得新增 OS submit"
    );
    assert_eq!(
        after_self_test.3, before_wal_tx,
        "Recover 等待中 self-test 不得新增 WAL TX"
    );
}

#[test]
fn self_test_during_recover_stays_busy_when_wal_health_recovers() {
    // WAL 暫時故障不撤銷仍有效的 Recover side-op；恢復寫入後不得讓既有 probe 競爭救援鍵。
    let h = ActorRig::new(SHELL_PROFILE).ready();
    h.dev.on_tx_reply(b"\x03", b"^C\r\n");
    h.dev.on_tx_reply(b"\x04", b"^D\r\nroot@t:/# ");

    let recover = h.request_async(SessionRequest::Recover {
        timeout_s: 2.0,
        force: false,
    });
    h.wait_for("Recover 已送 Ctrl-C 並等待 prompt", |h| {
        tx_rows(h)
            .iter()
            .any(|row| b64(row) == b"\x03" && row["source"] == "system:recover")
    });

    h.fail_next_wal_write(PalError::io(28, "injected temporary WAL write failure"));
    assert!(
        !h.append_wal_for_test("RX", "test:wal-health", b"temporary failure"),
        "受控的 WAL append 必須失敗"
    );
    assert!(!h.wal_health().healthy, "失敗 append 應發布 WAL unhealthy");
    assert!(h.fence.with(|fence| fence.wal_unhealthy));

    h.dev.script_write(vec![FakeWriteStep::PauseBeforeSubmit]);
    h.dev.on_tx_match(
        r"__READY__[0-9a-f]+",
        Box::new(|line: &[u8]| {
            let text = String::from_utf8_lossy(line).into_owned();
            let token = text
                .split("__READY__")
                .nth(1)
                .unwrap_or("")
                .trim()
                .to_string();
            format!("__READY__{token}\r\n").into_bytes()
        }),
    );
    let before = (
        h.dev.tx_len(),
        h.dev.write_calls(),
        h.dev.os_submits(),
        tx_rows(&h)
            .iter()
            .filter(|row| row["source"] == "system:self_test")
            .count(),
    );
    let self_test = h.request_async(SessionRequest::SelfTest {
        timeout_s: 2.0,
        strict_human_lock: false,
    });

    let passive_before_recovery = self_test.recv_timeout(Duration::from_millis(100)).ok();
    let probe_paused_before_recovery = h.dev.wait_paused(Duration::from_millis(100));
    assert!(
        h.append_wal_for_test("RX", "test:wal-health", b"recovered WAL"),
        "後續成功 append 應恢復 WAL health"
    );
    assert!(h.wal_health().healthy);
    assert!(!h.fence.with(|fence| fence.wal_unhealthy));
    h.dev.release_pause();

    let result = passive_before_recovery.unwrap_or_else(|| {
        self_test
            .recv_timeout(Duration::from_secs(5))
            .expect("self-test 回覆")
    });
    let result = result.expect("self-test RPC 成功");
    let after = (
        h.dev.tx_len(),
        h.dev.write_calls(),
        h.dev.os_submits(),
        tx_rows(&h)
            .iter()
            .filter(|row| row["source"] == "system:self_test")
            .count(),
    );

    h.advance(Duration::from_secs(2));
    let recovered = recover
        .recv_timeout(Duration::from_secs(5))
        .expect("Recover 回覆")
        .expect("Recover RPC 成功");

    assert!(
        !probe_paused_before_recovery,
        "仍有效 Recover 等待中，self-test 不得啟動 probe"
    );
    assert_eq!(result["classification"], PyValue::from("SESSION_BUSY"));
    assert_eq!(result["recommended_action"], PyValue::from("wait"));
    assert_eq!(after, before, "WAL health 恢復前後不得多送 self-test probe");
    assert_eq!(recovered["error_code"], PyValue::from("PROMPT_TIMEOUT_RECOVERED"));
    assert_eq!(recovered["recovery_action"], PyValue::from("CTRL_D"));
    assert!(wal_tx_has(&h, b"\x04"), "Recover 後續 Ctrl-D 應照常送出");
}

#[test]
fn self_test_and_get_state_do_not_wait_for_recover_tx_fence() {
    // PauseInsideSubmit 持有 TX fence；self-test 與 actor 查詢都必須在 OS submit 返回前回覆。
    let h = ActorRig::new(SHELL_PROFILE).ready();
    h.dev.script_write(vec![FakeWriteStep::PauseInsideSubmit]);
    h.dev.on_tx_reply(b"\x03", b"^C\r\n");
    h.dev.on_tx_reply(b"\x04", b"^D\r\nroot@t:/# ");

    let recover = h.request_async(SessionRequest::Recover {
        timeout_s: 2.0,
        force: false,
    });
    assert!(
        h.dev.wait_paused(Duration::from_secs(2)),
        "Recover 救援鍵應在持有 fence 的 OS 提交內暫停"
    );

    let before_self_test_tx = tx_rows(&h)
        .iter()
        .filter(|row| row["source"] == "system:self_test")
        .count();
    let self_test = h.request_async(SessionRequest::SelfTest {
        timeout_s: 2.0,
        strict_human_lock: false,
    });
    let query = h.request_async(SessionRequest::GetState);
    let self_test_before_release = self_test.recv_timeout(Duration::from_millis(250)).ok();
    let query_before_release = query.recv_timeout(Duration::from_millis(250)).ok();
    let self_test_replied_before_release = self_test_before_release.is_some();
    let query_replied_before_release = query_before_release.is_some();

    h.dev.release_pause();
    let self_test_reply = self_test_before_release.unwrap_or_else(|| {
        self_test
            .recv_timeout(Duration::from_secs(5))
            .expect("self-test 回覆")
    });
    let query_reply = query_before_release.unwrap_or_else(|| {
        query
            .recv_timeout(Duration::from_secs(5))
            .expect("GetState 回覆")
    });
    let result = self_test_reply.expect("self-test RPC 成功");
    let query_result = query_reply.expect("GetState RPC 成功");

    h.wait_for("Recover Ctrl-C 提交完成", |h| {
        tx_rows(h)
            .iter()
            .any(|row| b64(row) == b"\x03" && row["source"] == "system:recover")
    });
    assert_eq!(
        result["classification"],
        PyValue::from("SESSION_BUSY"),
        "Recover 等待期間 self-test 應被動回覆：{result:?}"
    );
    assert!(
        self_test_replied_before_release && query_replied_before_release,
        "self-test 與 GetState 應在 fence 解鎖前回覆"
    );
    assert_eq!(result["recommended_action"], PyValue::from("wait"));
    assert!(query_result.get("session").is_some(), "GetState 應正常回覆");
    assert_eq!(
        tx_rows(&h)
            .iter()
            .filter(|row| row["source"] == "system:self_test")
            .count(),
        before_self_test_tx,
        "Recover 提交持鎖期間 self-test 不得新增 probe TX"
    );

    h.advance(Duration::from_secs(2));
    let recovered = recover
        .recv_timeout(Duration::from_secs(5))
        .expect("Recover 回覆")
        .expect("Recover RPC 成功");
    assert_eq!(recovered["error_code"], PyValue::from("PROMPT_TIMEOUT_RECOVERED"));
    assert_eq!(recovered["recovery_action"], PyValue::from("CTRL_D"));
}

#[test]
fn self_test_probe_in_flight_is_not_foreground_busy() {
    // F27：完整執行 probe 未獲回應時，仍在途且不計入 foreground_busy／outstanding_commands。
    let h = ActorRig::new(SHELL_PROFILE).ready();
    h.dev.clear_replies();
    let wait_start = h.rx.regex_wait_starts_for_test();
    let rx = h.request_async(SessionRequest::SelfTest {
        timeout_s: 2.0,
        strict_human_lock: false,
    });
    h.wait_for("probe 已送出", |h| {
        tx_rows(h)
            .iter()
            .any(|r| String::from_utf8_lossy(&b64(r)).contains("__READY__"))
    });
    h.wait_for("self-test 等待 marker", |h| {
        h.rx.regex_wait_starts_for_test() > wait_start
    });
    assert!(rx.is_empty(), "probe 等待執行證據時 RPC 必須尚未完成");
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
// WP5-8 第 1 項（G11，S1X-a-1）：interactive 命令未完整送出時，只關閉本命令開啟的 lease。
// 基準 `origin/main:sw_core/session_manager.py:6790-6822`：送出拋例外或被拒時，僅在
// `session.interactive_session_id == lease.interactive_id` 時 `_close_interactive_locked`（非 expired、無 tombstone），
// 期間被換掉的 lease 不動；Python 回歸 `tests/test_s1x_a1_interactive_permit.py:130`、`:150`。
// ---------------------------------------------------------------------------

/// 本 session 目前的 `interactive_session_id`（`null` → `None`）。
fn wp58_lease_of(h: &ActorRig) -> Option<String> {
    h.get_state()["interactive_session_id"]
        .as_str()
        .map(str::to_string)
}

/// lease 已收回：後續 line 命令不得被遺留 lease 擋成 `SESSION_INTERACTIVE_BUSY`，interactive 命令照常送出。
fn wp58_assert_session_usable_after_failure(h: &ActorRig) {
    h.dev.on_tx_reply(b"echo ok\n", b"echo ok\r\nok\r\nroot@t:/# ");
    let line = h.submit_ok("echo ok");
    let rec = h.wait_done(&line);
    assert_ne!(
        rec.get("error_code"),
        Some(&PyValue::from("SESSION_INTERACTIVE_BUSY")),
        "遺留 lease 擋住後續命令：{rec:?}"
    );
    assert_eq!(rec["status"], PyValue::from("done"), "{rec:?}");
    let tx_before = h.dev.tx_len();
    let next = h.submit_with("top", |r| r.mode = "interactive".into());
    let rec = h.wait_done(&next);
    assert_eq!(rec["status"], PyValue::from("interactive"), "{rec:?}");
    assert_eq!(h.dev.tx_since(tx_before), b"top\n".to_vec());
    assert_eq!(
        wp58_lease_of(h),
        rec["interactive_session_id"].as_str().map(str::to_string),
        "新 interactive 命令的 lease 應為目前 lease"
    );
}

#[test]
fn wp58_g11_rejected_interactive_send_closes_opened_lease() {
    // Python `test_rejected_interactive_send_closes_opened_lease`：drain fence（`bridge._tx_frozen = True`）在提交點拒絕。
    let h = ActorRig::new(SHELL_PROFILE).ready();
    let tx_before = h.dev.tx_len();
    let rows_before = interactive_tx_rows(&h).len();
    h.fence.with(|f| f.frozen = true);
    let id = h.submit_with("top", |r| r.mode = "interactive".into());
    let rec = h.wait_done(&id);
    // 錯誤碼沿用既有 `lease_refused_command_code(Frozen)` 映射（本項不改）。
    assert_eq!(rec["error_code"], PyValue::from("FLUSHED_BY_RECOVERY"), "{rec:?}");
    assert_eq!(h.dev.tx_len(), tx_before, "被拒的 interactive 命令不得寫 UART");
    assert_eq!(interactive_tx_rows(&h).len(), rows_before, "被拒的 interactive 命令不得寫 TX WAL");
    assert_eq!(wp58_lease_of(&h), None, "送出被拒後不得遺留本命令開的 lease");
    h.fence.with(|f| f.frozen = false);
    wp58_assert_session_usable_after_failure(&h);
}

#[test]
fn wp58_g11_partial_interactive_send_closes_opened_lease() {
    // 部分寫入：錯誤碼與 META 沿用 `interactive_mode_command_partial_send_maps_aborted_in_flight`；lease 同樣收回
    // （Python `bridge.send_command` 拋 TxIncomplete → `except BaseException` 關 lease 後交 `_map_aborted_result`）。
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
    assert_eq!(wp58_lease_of(&h), None, "部分寫入後不得遺留本命令開的 lease");
    wp58_assert_session_usable_after_failure(&h);
}

#[test]
fn wp58_g11_unknown_interactive_send_closes_opened_lease() {
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
    assert_eq!(wp58_lease_of(&h), None, "結果不明時不得遺留本命令開的 lease");
    wp58_assert_session_usable_after_failure(&h);
}

/// CoreHarness：出列一條非空 interactive 命令，回 `(cmd_id, InteractiveOpenCmd 的 grant, 本命令開的 lease id)`。
fn wp58_start_interactive(h: &mut CoreHarness, cmd: &str) -> (String, sw_core::session_core::OpGrant, String) {
    use sw_core::op_runner::OpSpec;
    let mut req = agent_submit(cmd);
    req.mode = "interactive".into();
    let (resp, fx) = h.rpc_full(SessionRequest::Submit(req));
    let resp = resp.expect("interactive 命令 admission 應成功");
    let cmd_id = resp["cmd_id"].as_str().expect("cmd_id").to_string();
    let grant = fx
        .iter()
        .find_map(|e| match e {
            Effect::StartOp {
                grant,
                spec: OpSpec::InteractiveOpenCmd { command, .. },
            } if command == cmd => Some(grant.clone()),
            _ => None,
        })
        .expect("非空 interactive 命令應產生 StartOp(InteractiveOpenCmd)");
    let lease = h.public()["interactive_session_id"]
        .as_str()
        .expect("送出前 lease 已開")
        .to_string();
    (cmd_id, grant, lease)
}

fn wp58_finish(h: &mut CoreHarness, grant: &sw_core::session_core::OpGrant, outcome: OpOutcome) -> Vec<Effect> {
    let epoch = h.epoch();
    h.feed(ControlEvent::OpFinished {
        op_id: grant.op_id,
        epoch,
        outcome,
        after_rx_seq: None,
    })
}

fn wp58_removed(ops: &[sw_core::session_core::LeaseIndexOp], id: &str) -> bool {
    ops.iter().any(|op| {
        matches!(op, sw_core::session_core::LeaseIndexOp::Remove { interactive_id } if interactive_id == id)
    })
}

#[test]
fn wp58_g11_rejected_send_preserves_replacement_lease() {
    // Python `test_rejected_send_does_not_close_lease_replaced_meanwhile`：送出失敗通知抵達前 lease 已被換掉，
    // 結算原命令時不得誤關現行 lease。換 lease 沿用既有流程：agent `interactive_close` 原 lease → human console
    // 接入開出 `human:c1` lease（`console.rs` 的 open-or-refresh）。
    use sw_core::tx_writer::FenceReject;
    let mut h = CoreHarness::new(SHELL_PROFILE);
    h.to_ready();
    let (cmd_id, grant, original) = wp58_start_interactive(&mut h, "top");
    let closed = h
        .rpc(SessionRequest::InteractiveClose {
            interactive_id: original.clone(),
        })
        .expect("interactive_close");
    assert_eq!(closed["ok"], PyValue::Bool(true), "{closed:?}");
    h.attach_pty_console("c1", true);
    let replacement = h.public()["interactive_session_id"]
        .as_str()
        .expect("human console 應開出新 lease")
        .to_string();
    assert_ne!(replacement, original);
    let ops_before = h.lease_index_ops().len();
    wp58_finish(&mut h, &grant, OpOutcome::SendRefused(FenceReject::Frozen));
    let rec = h.reg.get(&cmd_id).expect("命令紀錄");
    assert_eq!(rec["error_code"], PyValue::from("FLUSHED_BY_RECOVERY"), "{rec:?}");
    assert_eq!(
        h.public()["interactive_session_id"],
        PyValue::from(replacement.clone()),
        "期間換入的 lease 必須保留"
    );
    assert!(
        !wp58_removed(&h.lease_index_ops()[ops_before..], &replacement),
        "不得自 LeaseIndex 移除現行 lease"
    );
}

#[test]
fn wp58_g11_non_sent_outcomes_close_only_own_lease() {
    // `SendRefused`／`SendFailed`／失敗的 `CommandResult` 皆關閉本命令開的 lease 並自 LeaseIndex 移除；`Sent` 保留。
    use sw_core::tx_writer::FenceReject;
    type MakeOutcome = fn() -> OpOutcome;
    let cases: [(&str, MakeOutcome, Option<&str>); 4] = [
        ("SendRefused", || OpOutcome::SendRefused(FenceReject::Frozen), Some("FLUSHED_BY_RECOVERY")),
        ("SendFailed", || OpOutcome::SendFailed, None),
        (
            "CommandResult",
            || {
                OpOutcome::CommandResult(sw_proto::pydict! {
                    "ok" => false,
                    "error_code" => "COMMAND_ABORTED_IN_FLIGHT",
                })
            },
            Some("COMMAND_ABORTED_IN_FLIGHT"),
        ),
        ("Sent", || OpOutcome::Sent { bytes: 4 }, None),
    ];
    for (case, outcome, code) in cases {
        let mut h = CoreHarness::new(SHELL_PROFILE);
        h.to_ready();
        let (cmd_id, grant, lease) = wp58_start_interactive(&mut h, "top");
        let ops_before = h.lease_index_ops().len();
        wp58_finish(&mut h, &grant, outcome());
        let rec = h.reg.get(&cmd_id).expect("命令紀錄");
        let removed = wp58_removed(&h.lease_index_ops()[ops_before..], &lease);
        if case == "Sent" {
            assert_eq!(rec["status"], PyValue::from("interactive"), "{case}：{rec:?}");
            assert_eq!(h.public()["interactive_session_id"], PyValue::from(lease.clone()), "{case}：lease 應保留");
            assert!(!removed, "{case}：不得移除 LeaseIndex");
            continue;
        }
        assert_ne!(rec["status"], PyValue::from("interactive"), "{case}：{rec:?}");
        if let Some(code) = code {
            assert_eq!(rec["error_code"], PyValue::from(code), "{case}：{rec:?}");
        }
        assert!(h.public()["interactive_session_id"].is_null(), "{case}：不得遺留本命令開的 lease");
        assert!(removed, "{case}：應自 LeaseIndex 移除本命令開的 lease");
        // 後續 line 命令照常出列（不被遺留 lease 擋成 SESSION_INTERACTIVE_BUSY）。
        let (resp, fx) = h.rpc_full(SessionRequest::Submit(agent_submit("echo ok")));
        let next = resp.expect("line 命令 admission").get("cmd_id").and_then(PyValue::as_str).expect("cmd_id").to_string();
        let next_rec = h.reg.get(&next).expect("line 命令紀錄");
        assert_ne!(
            next_rec.get("error_code"),
            Some(&PyValue::from("SESSION_INTERACTIVE_BUSY")),
            "{case}：{next_rec:?}"
        );
        assert!(
            fx.iter().any(|e| matches!(e, Effect::StartOp { grant, .. } if grant.cmd_id.as_deref() == Some(next.as_str()))),
            "{case}：line 命令應進入執行"
        );
    }
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

#[test]
fn wrapped_echo_whitespace_boundary_matrix() {
    let prompt = WRAP_PROMPT;
    let check = |raw: String, command: &str, expected: String| {
        let profile = SHELL_PROFILE.replace("(?m)^root@t:.*# ", WRAP_PROMPT_RE);
        let h = ActorRig::new_with_hub(&profile).ready_with_prompt(prompt.as_bytes());
        let tx = format!("{command}\n");
        h.dev.on_tx_reply(tx.as_bytes(), raw.as_bytes());
        let tx_count = h.wal_tx_payloads().len();
        let id = h.submit_ok(command);
        let record = h.wait_done(&id);
        assert_eq!(record["status"], PyValue::from("done"));
        assert_eq!(
            record["stdout"],
            PyValue::from(expected),
            "command={command:?} raw={raw:?}"
        );
        assert_eq!(wal_tx_bytes_since(&h, tx_count), tx.as_bytes());
    };

    // R4-01：命令原有空白緊接 BusyBox 折行；該空白既像 readline 標記，也必須保留為命令字元。
    let command = format!("echo {} {}", "x".repeat(40), "y".repeat(40));
    let cut = 5 + 40 + 1;
    check(
        format!("{}\r\r\n{}\r\nOUT\r\n{prompt}", &command[..cut], &command[cut..]),
        &command,
        "OUT".into(),
    );

    // readline 插入空白位於命令空白前後，以及連續兩個命令空白之間。
    let command = format!("echo {} {}", "x".repeat(74), "y".repeat(20));
    for cut in [5 + 74, 5 + 74 + 1] {
        let echo = readline_wrapped(&command, &[cut]);
        check(format!("{echo}\r\nOUT\r\n{prompt}"), &command, "OUT".into());
    }
    let command = format!("echo {}  {}", "x".repeat(40), "y".repeat(40));
    let echo = readline_wrapped(&command, &[5 + 40 + 1]);
    check(format!("{echo}\r\nOUT\r\n{prompt}"), &command, "OUT".into());

    // 同一 echo 內多次折行並混用 BusyBox 與 readline 標記。
    let command = format!("echo {} {} {}", "a".repeat(30), "b".repeat(35), "c".repeat(40));
    let echo = format!(
        "{}\r\r\n{} \r{}",
        &command[..25],
        &command[25..60],
        &command[60..]
    );
    check(
        format!("{echo}\r\nOUT\r\n{prompt}"),
        &command,
        format!("{}\n{} {}\nOUT", &command[..25], &command[25..60], &command[60..]),
    );

    // 折行標記不得出現在第一個命令字元之前，亦不得出現在命令完成後。
    let command = format!("echo {}", "z".repeat(40));
    check(
        format!(" \r{command}\r\nOUT\r\n{prompt}"),
        &command,
        format!(" {command}\nOUT"),
    );
    check(
        format!("\r\r\n{command}\r\nOUT\r\n{prompt}"),
        &command,
        format!("{command}\nOUT"),
    );
    check(
        format!("{command} \r\r\nOUT\r\n{prompt}"),
        &command,
        format!("{command} \nOUT"),
    );

    // BusyBox 的換行緊接完整命令，可作為 echo 的行尾。
    check(
        format!("{command}\r\r\nOUT\r\n{prompt}"),
        &command,
        "OUT".into(),
    );
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

// ---------------------------------------------------------------------------
// WP7-3：console unlock 的 reboot 前清 RX（G10，Low）
// 對照 Python `sw_core/session_manager.py:6421-6423`：
//   if session.profile.console_unlock_env:
//       # 已重開的舊 root prompt 不可在 recovery 中被當成解鎖證據。
//       bridge.clear_rx_buffer()
// ---------------------------------------------------------------------------

#[test]
fn wp73_reboot_clears_rx_for_console_unlock_avoids_stale_prompt_evidence() {
    const PROFILE: &str = "profiles:\n  cu:\n    platform: shell\n    prompt_regex: '(?m)^root@t:.*# '\n    console_unlock_env: SW_T_UNLOCK\n    console_unlock_idle_s: 0.05\n    timeout_s: 2.0\ntargets:\n  - {com: COM0, profile: cu, act_no: 1, alias: cu+1, device_by_id: /s/by-id/usb-A}\n";
    let h = ActorRig::new(PROFILE);
    h.set_env("SW_T_UNLOCK", "secret123");
    let h = h.ready();
    // 初始 attach 時，假 DUT 回覆 prompt，此時 h.rx 中留有舊的 root prompt。
    assert!(h.rx.tail(100).contains("root@t:"));

    // 模擬 DUT 重開機：清除對一般 \n 的預設回覆；
    // DUT 在收到 reboot 後進入靜默（超過 console_unlock_idle_s），只有在收到 unlock 密鑰時才回覆 prompt。
    h.dev.clear_replies();
    h.dev.on_tx_match(
        r"secret123",
        Box::new(|_| b"\r\nroot@t:/# ".to_vec()),
    );
    h.dev.on_tx_match(
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

    let id = h.submit_ok("reboot");
    wait_tx(&h, b"reboot\n");
    advance_until(&h, Duration::from_millis(100), "reboot 收尾", |h| {
        is_done(h, &id)
    });
    h.wait_state("RECOVERING");

    // 推進時鐘以越過 boot quiet window (2.0s) 與 console_unlock_idle_s (0.05s)
    advance_until(&h, Duration::from_millis(200), "恢復 READY", |h| {
        state_of(h) == "READY"
    });

    let tx = String::from_utf8_lossy(&h.dev.take_tx()).into_owned();
    assert!(
        tx.contains("secret123"),
        "reboot 後的 unlock 流程必須送出 unlock 密鑰，不得誤用舊 root prompt 當作解鎖證據；實際 TX: {tx:?}"
    );
}

