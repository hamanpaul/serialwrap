#![cfg(feature = "test-fakes")]
//! Slice 2.1 Task 8 紅燈測試：sw-core `CommandRegistry`／`SessionQueue`（admission、出列、history、SC-06）。
//!
//! 逐列展開 `docs/rust-refactor/plans/06-slice21-daemon-core.md` Task 8 的 Step 1 程式碼案例與表格。
//! 實作前每個測試應於執行期因 `not implemented: slice21 T8 ...` 失敗（缺行為，非編譯錯誤）。

use sw_core::arbiter::{AbortClass, CmdOutcome, CommandRegistry, RunDecision, SessionQueue, SubmitOrigin, SubmitReq};
use sw_proto::pyjson::{dumps, dumps_dict, PyDict, PyValue, RPC_WIRE};

const T: &str = "2026-10-15T03:06:17.663778+00:00";
const SID: &str = "p:COM0";

fn req(cmd: &str) -> SubmitReq {
    SubmitReq {
        session_id: SID.into(),
        command: cmd.into(),
        source: "agent".into(),
        mode: "line".into(),
        timeout_s: 10.0,
        priority: 10,
        expected_duration_s: None,
        completion_regex: None,
        origin: SubmitOrigin::Rpc,
    }
}

/// 依序組出 `PyDict`（保留插入順序）。
fn dict(pairs: Vec<(&str, PyValue)>) -> PyDict {
    PyDict::from_pairs(pairs.into_iter().map(|(k, v)| (k.to_string(), v)).collect())
}

/// 取欄位；缺欄位回 `Null`（對應 Python `dict.get`）。
fn field(d: &PyDict, k: &str) -> PyValue {
    d.get(k).cloned().unwrap_or(PyValue::Null)
}

/// 取得紀錄副本；不存在即失敗。
fn rec(r: &CommandRegistry, id: &str) -> PyDict {
    r.get(id).unwrap_or_else(|| panic!("record {id} 不存在"))
}

/// 單調遞增的 done_at 字串（零補齊，字典序即時間序）。
fn ts(i: usize) -> String {
    format!("2026-10-15T00:{:02}:{:02}.000000+00:00", i / 60, i % 60)
}

// ---------------------------------------------------------------------------
// 程式碼案例
// ---------------------------------------------------------------------------

#[test]
fn too_long_checked_before_newline_with_exact_response() {
    let r = CommandRegistry::new();
    let mut long = "a".repeat(16_385);
    long.push('\n');
    let e = r.admit(req(&long), true, "c1".into(), T).err().expect("超長應被拒絕");
    // 長度先於換行檢查：16385 個 'a' + '\n' = 16386 bytes。
    // 線路格式的結尾 '\n' 由 framing 補上，故以 format! 比對完整 wire 位元組。
    assert_eq!(
        format!("{}\n", dumps(&PyValue::Dict(e), &RPC_WIRE)),
        "{\"ok\":false,\"error_code\":\"CMD_TOO_LONG\",\"cmd_length\":16386,\"limit\":16384,\"hint\":\"Command exceeds 16 KB hard limit. Use file-based injection or split into smaller commands.\"}\n"
    );
}

#[test]
fn queue_full_counts_canceled_but_running() {
    let r = CommandRegistry::new();
    let mut q = SessionQueue::new();
    for i in 0..256 {
        let a = r.admit(req("x"), true, format!("c{i}"), T).expect("前 256 筆應 admit 成功");
        q.push(a.queued, &r);
    }
    let head = q.pop().expect("佇列應有命令");
    assert!(matches!(r.mark_running(&head.cmd_id, T), RunDecision::Run));
    r.set_in_flight(SID, Some(head.cmd_id.as_str()));
    r.cancel(&head.cmd_id, T); // 標 canceled、設 done_at；但 worker 仍在執行（在途）
    let e = r
        .admit(req("y"), true, "late".into(), T)
        .err()
        .expect("第 257 筆應被 SESSION_QUEUE_FULL 拒絕");
    assert_eq!(field(&e, "error_code"), PyValue::from("SESSION_QUEUE_FULL"));
    // 255 accepted + 1 canceled-running（SC-06）
    assert_eq!(field(&e, "pending"), PyValue::from(256_i64));
}

#[test]
fn record_key_order_matches_python() {
    let r = CommandRegistry::new();
    r.admit(req("ls"), true, "c1".into(), T).expect("ls 應被接受");
    let keys: Vec<String> = rec(&r, "c1").0.keys().cloned().collect();
    assert_eq!(
        keys,
        [
            "cmd_id",
            "session_id",
            "command",
            "source",
            "mode",
            "execution_mode",
            "timeout_s",
            "expected_duration_s",
            "completion_regex",
            "priority",
            "status",
            "created_at",
            "accepted_at",
            "started_at",
            "done_at",
            "error_code",
            "stdout",
            "partial",
            "background_capture_id",
            "interactive_session_id",
            "recovery_action",
        ]
    );
}

// ---------------------------------------------------------------------------
// admission
// ---------------------------------------------------------------------------

#[test]
fn invalid_completion_regex_first() {
    let r = CommandRegistry::new();
    let mut q = req("a\nb"); // 同時含換行，證明 regex 檢查先於換行檢查
    q.completion_regex = Some(String::new());
    let e = r.admit(q, true, "c1".into(), T).err().expect("無效 completion regex 應被拒絕");
    assert_eq!(
        dumps_dict(&e, &RPC_WIRE),
        "{\"ok\":false,\"error_code\":\"INVALID_COMPLETION_REGEX\"}"
    );
}

#[test]
fn warning_above_4096() {
    let r = CommandRegistry::new();
    let resp = r
        .admit(req(&"a".repeat(4097)), true, "c1".into(), T)
        .expect("4097 bytes 應仍被接受（只警告）")
        .response;
    assert_eq!(field(&resp, "ok"), PyValue::from(true));
    assert_eq!(resp.0.keys().last().map(String::as_str), Some("warning"));
    assert_eq!(
        field(&resp, "warning"),
        PyValue::from(dict(vec![
            ("code", PyValue::from("CMD_LENGTH_WARNING")),
            ("cmd_length", PyValue::from(4097_i64)),
            ("soft_limit", PyValue::from(4096_i64)),
            (
                "hint",
                PyValue::from(
                    "Command exceeds 4 KB soft limit. Long commands may cause UART buffer overflow or prompt timeout.",
                ),
            ),
        ]))
    );
}

#[test]
fn length_counts_utf8_replace_bytes() {
    let r = CommandRegistry::new();
    let cmd = "中".repeat(5462); // 5462 × 3 bytes = 16386 bytes
    let e = r
        .admit(req(&cmd), true, "c1".into(), T)
        .err()
        .expect("16386 bytes 應 CMD_TOO_LONG");
    assert_eq!(field(&e, "error_code"), PyValue::from("CMD_TOO_LONG"));
    assert_eq!(field(&e, "cmd_length"), PyValue::from(16386_i64));
}

#[test]
fn newline_rejected_cr_allowed_empty_allowed() {
    let r = CommandRegistry::new();
    let e = r
        .admit(req("a\nb"), true, "c1".into(), T)
        .err()
        .expect("含 \\n 應被拒絕");
    assert_eq!(
        dumps_dict(&e, &RPC_WIRE),
        "{\"ok\":false,\"error_code\":\"CMD_CONTAINS_NEWLINE\",\"hint\":\"Command must not contain embedded newline characters. Split into multiple independent submissions.\"}"
    );
    assert!(r.admit(req("a\rb"), true, "c2".into(), T).is_ok(), "\\r 應允許");
    assert!(r.admit(req(""), true, "c3".into(), T).is_ok(), "空命令應允許");
}

#[test]
fn unregistered_queue_not_ready() {
    let r = CommandRegistry::new();
    let e = r
        .admit(req("ls"), false, "c1".into(), T)
        .err()
        .expect("queue 未註冊應拒絕");
    assert_eq!(
        dumps_dict(&e, &RPC_WIRE),
        "{\"ok\":false,\"error_code\":\"SESSION_NOT_READY\",\"session_id\":\"p:COM0\"}"
    );
}

#[test]
fn accepted_response_shape() {
    let r = CommandRegistry::new();
    let resp = r
        .admit(req("ls"), true, "c1".into(), T)
        .expect("ls 應被接受")
        .response;
    assert_eq!(
        dumps_dict(&resp, &RPC_WIRE),
        "{\"ok\":true,\"cmd_id\":\"c1\",\"status\":\"accepted\",\"session_id\":\"p:COM0\"}"
    );
}

// ---------------------------------------------------------------------------
// 出列
// ---------------------------------------------------------------------------

#[test]
fn priority_then_fifo() {
    let r = CommandRegistry::new();
    let mut q = SessionQueue::new();
    // 依序 admit：c0(20)、c1(5)、c2(5)、c3(10)
    for (id, prio) in [("c0", 20_i128), ("c1", 5), ("c2", 5), ("c3", 10)] {
        let mut rq = req(id);
        rq.priority = prio;
        let a = r.admit(rq, true, id.into(), T).expect("admit 應成功");
        q.push(a.queued, &r);
    }
    let order: Vec<String> = std::iter::from_fn(|| q.pop()).map(|c| c.cmd_id).collect();
    assert_eq!(order, ["c1", "c2", "c3", "c0"]);
}

#[test]
fn execution_mode_alias() {
    let r = CommandRegistry::new();
    for (i, (mode, want)) in [("fg", "line"), ("bg", "background"), ("weird", "weird")]
        .into_iter()
        .enumerate()
    {
        let id = format!("m{i}");
        let mut rq = req("ls");
        rq.mode = mode.into();
        r.admit(rq, true, id.clone(), T).expect("admit 應成功");
        assert_eq!(field(&rec(&r, &id), "execution_mode"), PyValue::from(want));
    }
}

#[test]
fn mark_running_skips_canceled_or_done() {
    let r = CommandRegistry::new();

    // 已取消者應跳過
    r.admit(req("a"), true, "c1".into(), T).expect("admit");
    r.cancel("c1", T);
    assert!(matches!(r.mark_running("c1", T), RunDecision::Skip));

    // 已終結者應跳過
    r.admit(req("b"), true, "c3".into(), T).expect("admit");
    assert!(matches!(r.mark_running("c3", T), RunDecision::Run));
    r.complete("c3", ok_result_for_test(), T);
    assert!(matches!(r.mark_running("c3", T), RunDecision::Skip));

    // 正常者轉為 running
    r.admit(req("c"), true, "c2".into(), T).expect("admit");
    assert!(matches!(r.mark_running("c2", T), RunDecision::Run));
    let c2 = rec(&r, "c2");
    assert_eq!(field(&c2, "status"), PyValue::from("running"));
    assert_eq!(field(&c2, "started_at"), PyValue::from(T));
}

/// `{"ok":true}` 的完成結果（供多個測試共用）。
fn ok_result_for_test() -> CmdOutcome {
    CmdOutcome::Result(dict(vec![("ok", PyValue::from(true))]))
}

// ---------------------------------------------------------------------------
// 完成與終結
// ---------------------------------------------------------------------------

#[test]
fn complete_ok_true_drops_error_code() {
    let r = CommandRegistry::new();
    r.admit(req("a"), true, "c1".into(), T).expect("admit");
    r.mark_running("c1", T);
    r.complete(
        "c1",
        CmdOutcome::Result(dict(vec![
            ("ok", PyValue::from(true)),
            ("error_code", PyValue::from("PROMPT_TIMEOUT_RECOVERED")),
            ("recovery_action", PyValue::from("CTRL_C")),
            ("stdout", PyValue::from("o")),
            ("partial", PyValue::from(true)),
        ])),
        T,
    );
    let c = rec(&r, "c1");
    assert_eq!(field(&c, "status"), PyValue::from("done"));
    assert_eq!(field(&c, "error_code"), PyValue::Null);
    assert_eq!(field(&c, "recovery_action"), PyValue::from("CTRL_C"));
    assert_eq!(field(&c, "stdout"), PyValue::from("o"));
    assert_eq!(field(&c, "partial"), PyValue::from(true));
}

#[test]
fn complete_ok_false() {
    let r = CommandRegistry::new();

    r.admit(req("a"), true, "c1".into(), T).expect("admit");
    r.mark_running("c1", T);
    r.complete(
        "c1",
        CmdOutcome::Result(dict(vec![
            ("ok", PyValue::from(false)),
            ("error_code", PyValue::from("PROMPT_TIMEOUT")),
        ])),
        T,
    );
    let c1 = rec(&r, "c1");
    assert_eq!(field(&c1, "status"), PyValue::from("error"));
    assert_eq!(field(&c1, "error_code"), PyValue::from("PROMPT_TIMEOUT"));

    // 無錯誤碼時回 COMMAND_FAILED
    r.admit(req("b"), true, "c2".into(), T).expect("admit");
    r.mark_running("c2", T);
    r.complete("c2", CmdOutcome::Result(dict(vec![("ok", PyValue::from(false))])), T);
    let c2 = rec(&r, "c2");
    assert_eq!(field(&c2, "status"), PyValue::from("error"));
    assert_eq!(field(&c2, "error_code"), PyValue::from("COMMAND_FAILED"));
}

#[test]
fn complete_interactive_status() {
    let r = CommandRegistry::new();
    r.admit(req("a"), true, "c1".into(), T).expect("admit");
    r.mark_running("c1", T);
    r.complete(
        "c1",
        CmdOutcome::Result(dict(vec![
            ("ok", PyValue::from(true)),
            ("status", PyValue::from("interactive")),
            ("interactive_session_id", PyValue::from("i")),
        ])),
        T,
    );
    let c = rec(&r, "c1");
    assert_eq!(field(&c, "status"), PyValue::from("interactive"));
    assert_eq!(field(&c, "interactive_session_id"), PyValue::from("i"));
}

#[test]
fn complete_on_canceled_keeps_canceled() {
    let r = CommandRegistry::new();
    r.admit(req("a"), true, "c1".into(), T).expect("admit");
    r.mark_running("c1", T);
    r.cancel("c1", T);
    let before = rec(&r, "c1");
    assert_eq!(field(&before, "status"), PyValue::from("canceled"));
    r.complete("c1", ok_result_for_test(), T);
    assert_eq!(rec(&r, "c1"), before, "canceled 後的 complete 不得改寫紀錄");
}

#[test]
fn send_failed() {
    let r = CommandRegistry::new();
    r.admit(req("a"), true, "c1".into(), T).expect("admit");
    r.mark_running("c1", T);
    r.complete("c1", CmdOutcome::SendFailed, T);
    let c = rec(&r, "c1");
    assert_eq!(field(&c, "status"), PyValue::from("error"));
    assert_eq!(field(&c, "error_code"), PyValue::from("SEND_FAILED"));
}

#[test]
fn aborted_mapping_bc03() {
    let r = CommandRegistry::new();
    let cases: [(AbortClass, &str); 3] = [
        (AbortClass::InFlight, "COMMAND_ABORTED_IN_FLIGHT"),
        (AbortClass::NotSubmitted, "FLUSHED_BY_RECOVERY"),
        (AbortClass::Unknown, "SEND_FAILED"),
    ];
    for (i, (class, code)) in cases.into_iter().enumerate() {
        let id = format!("ab{i}");
        r.admit(req("a"), true, id.clone(), T).expect("admit");
        r.mark_running(&id, T);
        r.complete(&id, CmdOutcome::Aborted(class), T);
        let c = rec(&r, &id);
        assert_eq!(field(&c, "status"), PyValue::from("error"), "{id}");
        assert_eq!(field(&c, "error_code"), PyValue::from(code), "{id}");
        assert_eq!(field(&c, "done_at"), PyValue::from(T), "{id}");
    }
}

// ---------------------------------------------------------------------------
// 取消與 flush
// ---------------------------------------------------------------------------

#[test]
fn cancel_semantics() {
    let r = CommandRegistry::new();

    // 不存在
    assert_eq!(
        dumps_dict(&r.cancel("x", T), &RPC_WIRE),
        "{\"ok\":false,\"error_code\":\"CMD_NOT_FOUND\",\"cmd_id\":\"x\"}"
    );

    // accepted 可取消；canceled 可再取消
    r.admit(req("a"), true, "acc".into(), T).expect("admit");
    assert_eq!(
        dumps_dict(&r.cancel("acc", T), &RPC_WIRE),
        "{\"ok\":true,\"cmd_id\":\"acc\",\"status\":\"canceled\"}"
    );
    assert_eq!(
        dumps_dict(&r.cancel("acc", T), &RPC_WIRE),
        "{\"ok\":true,\"cmd_id\":\"acc\",\"status\":\"canceled\"}"
    );

    // running 可取消（只標記、不中止）
    r.admit(req("b"), true, "run".into(), T).expect("admit");
    r.mark_running("run", T);
    assert_eq!(
        dumps_dict(&r.cancel("run", T), &RPC_WIRE),
        "{\"ok\":true,\"cmd_id\":\"run\",\"status\":\"canceled\"}"
    );

    // interactive 可取消
    r.admit(req("c"), true, "ia".into(), T).expect("admit");
    r.mark_running("ia", T);
    r.complete(
        "ia",
        CmdOutcome::Result(dict(vec![
            ("ok", PyValue::from(true)),
            ("status", PyValue::from("interactive")),
            ("interactive_session_id", PyValue::from("i")),
        ])),
        T,
    );
    assert_eq!(
        dumps_dict(&r.cancel("ia", T), &RPC_WIRE),
        "{\"ok\":true,\"cmd_id\":\"ia\",\"status\":\"canceled\"}"
    );

    // done 不可取消
    r.admit(req("d"), true, "done".into(), T).expect("admit");
    r.mark_running("done", T);
    r.complete("done", ok_result_for_test(), T);
    assert_eq!(
        dumps_dict(&r.cancel("done", T), &RPC_WIRE),
        "{\"ok\":false,\"error_code\":\"CMD_NOT_CANCELABLE\",\"cmd_id\":\"done\"}"
    );

    // error 不可取消
    r.admit(req("e"), true, "err".into(), T).expect("admit");
    r.mark_running("err", T);
    r.complete("err", CmdOutcome::SendFailed, T);
    assert_eq!(
        dumps_dict(&r.cancel("err", T), &RPC_WIRE),
        "{\"ok\":false,\"error_code\":\"CMD_NOT_CANCELABLE\",\"cmd_id\":\"err\"}"
    );
}

#[test]
fn flush_only_accepted() {
    let r = CommandRegistry::new();
    r.admit(req("a1"), true, "a1".into(), T).expect("admit");
    r.admit(req("a2"), true, "a2".into(), T).expect("admit");
    r.admit(req("run"), true, "run".into(), T).expect("admit");
    r.mark_running("run", T);
    r.admit(req("ia"), true, "ia".into(), T).expect("admit");
    r.mark_running("ia", T);
    r.complete(
        "ia",
        CmdOutcome::Result(dict(vec![
            ("ok", PyValue::from(true)),
            ("status", PyValue::from("interactive")),
            ("interactive_session_id", PyValue::from("i")),
        ])),
        T,
    );
    r.admit(req("a5"), true, "a5".into(), T).expect("admit");
    // 另一個 session 的 accepted 命令，不應被 SID 的 flush 影響
    let mut other = req("o");
    other.session_id = "q:COM1".into();
    r.admit(other, true, "o1".into(), T).expect("admit");

    let flushed = r.flush_session(SID, "FLUSHED_BY_RECOVERY", T);
    assert_eq!(flushed, ["a1", "a2", "a5"], "只 flush accepted，依 admit 順序回傳");
    for id in ["a1", "a2", "a5"] {
        let c = rec(&r, id);
        assert_eq!(field(&c, "status"), PyValue::from("error"), "{id}");
        assert_eq!(field(&c, "error_code"), PyValue::from("FLUSHED_BY_RECOVERY"), "{id}");
        assert_eq!(field(&c, "done_at"), PyValue::from(T), "{id}");
    }
    assert_eq!(field(&rec(&r, "run"), "status"), PyValue::from("running"));
    assert_eq!(field(&rec(&r, "ia"), "status"), PyValue::from("interactive"));
    assert_eq!(field(&rec(&r, "o1"), "status"), PyValue::from("accepted"));

    // FLUSHED_BY_SHUTDOWN 同理
    let flushed_other = r.flush_session("q:COM1", "FLUSHED_BY_SHUTDOWN", T);
    assert_eq!(flushed_other, ["o1"]);
    assert_eq!(field(&rec(&r, "o1"), "error_code"), PyValue::from("FLUSHED_BY_SHUTDOWN"));
}

// ---------------------------------------------------------------------------
// history 淘汰與 SC-06 計數
// ---------------------------------------------------------------------------

#[test]
fn history_evicts_oldest_done_by_done_at_string() {
    let r = CommandRegistry::new();

    // 1 筆仍在執行中（不可淘汰）
    r.admit(req("run"), true, "run".into(), &ts(0)).expect("admit");
    r.mark_running("run", &ts(0));

    // 600 筆依序完成（done_at 字串遞增）
    for i in 0..600 {
        let id = format!("c{i}");
        r.admit(req("x"), true, id.clone(), &ts(i)).expect("admit");
        r.mark_running(&id, &ts(i));
        r.complete(&id, ok_result_for_test(), &ts(i));
    }

    assert!(r.len() <= 512, "len={} 應 <= CMD_HISTORY_MAX", r.len());
    assert!(r.get("c0").is_none(), "done_at 最小的已完成紀錄應被淘汰");
    assert!(r.get("c599").is_some(), "最新的已完成紀錄應保留");
    assert_eq!(
        field(&rec(&r, "run"), "status"),
        PyValue::from("running"),
        "仍在執行中的紀錄不得被淘汰"
    );

    // flush 不觸發淘汰：以 in-flight 紀錄撐高 len 超過 512（accepted 不可淘汰）
    let r2 = CommandRegistry::new();
    for i in 0..256 {
        r2.admit(req("a"), true, format!("a{i}"), T).expect("session A admit");
    }
    for i in 0..256 {
        let mut b = req("b");
        b.session_id = "q:COM1".into();
        r2.admit(b, true, format!("b{i}"), T).expect("session B admit");
    }
    let mut cc = req("c");
    cc.session_id = "r:COM2".into();
    r2.admit(cc, true, "c0".into(), T).expect("session C admit");
    assert_eq!(r2.len(), 513, "全為 accepted 時不可淘汰");

    let flushed = r2.flush_session(SID, "FLUSHED_BY_RECOVERY", T);
    assert_eq!(flushed.len(), 256);
    assert_eq!(r2.len(), 513, "flush 本身不得觸發淘汰");
    assert_eq!(field(&rec(&r2, "a0"), "error_code"), PyValue::from("FLUSHED_BY_RECOVERY"));
}

#[test]
fn history_never_evicts_in_flight_canceled() {
    let r = CommandRegistry::new();

    // 前景命令：先取消（done_at 最舊），但 worker 仍在執行（在途）
    r.admit(req("fg"), true, "fg".into(), &ts(0)).expect("admit");
    r.mark_running("fg", &ts(0));
    r.set_in_flight(SID, Some("fg"));
    r.cancel("fg", &ts(0));

    // 600 筆其他命令依序完成，觸發多次淘汰
    for i in 1..=600 {
        let id = format!("c{i}");
        r.admit(req("x"), true, id.clone(), &ts(i)).expect("admit");
        r.mark_running(&id, &ts(i));
        r.complete(&id, ok_result_for_test(), &ts(i));
    }

    assert!(r.get("fg").is_some(), "仍在途的 canceled 紀錄不得被淘汰（§5.4）");
    assert_eq!(field(&rec(&r, "fg"), "status"), PyValue::from("canceled"));
    assert!(r.len() <= 512, "len={}", r.len());
    assert_eq!(r.pending(SID), 1, "在途 canceled 仍計入 pending（SC-06）");
}

#[test]
fn submit_cancel_loop_keeps_queue_bounded() {
    let r = CommandRegistry::new();
    let mut q = SessionQueue::new();

    // 前景永遠忙：一筆 running 命令，在途不結束
    r.admit(req("fg"), true, "fg".into(), T).expect("admit");
    r.mark_running("fg", T);
    r.set_in_flight(SID, Some("fg"));

    for i in 0..10_000 {
        let id = format!("L{i}");
        let a = r.admit(req("x"), true, id.clone(), T).expect("admit");
        q.push(a.queued, &r);
        r.cancel(&id, T);
    }

    assert!(q.physical_len() <= 256, "physical_len={} 應 <= CMD_PENDING_MAX", q.physical_len());
    assert!(r.len() <= 512 + 1, "len={} 應 <= CMD_HISTORY_MAX + 在途", r.len());
}

#[test]
fn cancel_queued_removes_from_heap() {
    let r = CommandRegistry::new();
    let mut q = SessionQueue::new();
    let a = r.admit(req("a"), true, "c1".into(), T).expect("admit");
    q.push(a.queued, &r);
    let b = r.admit(req("b"), true, "c2".into(), T).expect("admit");
    q.push(b.queued, &r);
    assert_eq!(q.physical_len(), 2);

    r.cancel("c1", T);
    assert!(q.remove("c1"), "待執行項目應可被移除");
    assert_eq!(q.physical_len(), 1, "physical_len 應減 1");
    assert_eq!(q.len(), 1);

    let next = q.pop().expect("仍應有 c2");
    assert_eq!(next.cmd_id, "c2", "pop 不得回傳已移除的 c1");
    assert!(q.pop().is_none());
}

#[test]
fn pending_excludes_interactive() {
    let r = CommandRegistry::new();
    r.admit(req("a"), true, "c1".into(), T).expect("admit");
    r.mark_running("c1", T);
    r.complete(
        "c1",
        CmdOutcome::Result(dict(vec![
            ("ok", PyValue::from(true)),
            ("status", PyValue::from("interactive")),
            ("interactive_session_id", PyValue::from("i")),
        ])),
        T,
    );
    let c = rec(&r, "c1");
    assert_eq!(field(&c, "status"), PyValue::from("interactive"));
    assert!(field(&c, "done_at") != PyValue::Null, "interactive 紀錄應已有 done_at");
    assert_eq!(r.pending(SID), 0, "interactive 不計入 pending");

    r.admit(req("b"), true, "c2".into(), T).expect("admit");
    assert_eq!(r.pending(SID), 1);
}
