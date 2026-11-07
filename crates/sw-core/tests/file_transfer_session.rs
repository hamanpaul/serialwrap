//! P8 Task 6：`file.push`／`file.pull` 的 session 整合（RPC 轉型、resolve／manager 閘門、前景槽、OD-62 中止映射）。
//!
//! 行為基準＝origin/main 的 Python：`service.py` 的 `file.push`／`file.pull` 分支、`_resolve_session_id(file_transfer=True)`、
//! `session_manager.py::file_push`／`file_pull`／`_file_tx_rejected_result`。
//! 外部證據一律取自 FakeDevice 的實際寫出（`ShellPeer`），「零 TX」「不重送」不以 WAL 推論。
#![cfg(feature = "test-fakes")]

mod support;

use std::time::Duration;

use support::fake_shell::ShellCaps;
use support::file_transfer_session::*;
use support::{ActorRig, PASSTHROUGH_PROFILE, SHELL_PROFILE};
use sw_core::file_transfer::templates as t;
use sw_core::file_transfer::{FileTransferKind, FileTransferRequest};
use sw_core::session_core::{SessionRequest, SessionState};
use sw_pal::fakes::serial::FakeWriteStep;
use sw_pal::traits::Clock;
use sw_proto::pydict;
use sw_proto::pyjson::{self, PyDict, PyInt, PyValue};

const BANNER: &[u8] = b"\r\nU-Boot 2024.04 (Jan 01 2024 - 00:00:00 +0000)\r\n";

fn invalid_args() -> PyDict {
    pydict! { "ok" => false, "error_code" => "INVALID_ARGS" }
}

fn params(json: &str) -> PyDict {
    match pyjson::loads(json).expect("params JSON") {
        PyValue::Dict(d) => d,
        other => panic!("params 應為 object：{other:?}"),
    }
}

/// `from_rpc` 的錯誤回應（內層 `Err`）；轉型成功或例外時 panic。
fn rejected(kind: FileTransferKind, p: PyDict) -> PyDict {
    match FileTransferRequest::from_rpc(kind, &p) {
        Ok(Err(resp)) => resp,
        Ok(Ok((req, _))) => panic!("應被拒，實際轉型成功：{req:?}"),
        Err(e) => panic!("應回錯誤字典，實際例外：{}", e.message),
    }
}

/// `from_rpc` 轉型成功的請求與來源檢查。
fn accepted(kind: FileTransferKind, p: PyDict) -> (FileTransferRequest, sw_proto::source_policy::SourceCheck) {
    match FileTransferRequest::from_rpc(kind, &p) {
        Ok(Ok(x)) => x,
        Ok(Err(resp)) => panic!("應轉型成功，實際 {resp:?}"),
        Err(e) => panic!("應轉型成功，實際例外：{}", e.message),
    }
}

const PUSH_OK: &str = r#""selector":"COM0","local_path":"/l","remote_path":"/r""#;
const PULL_OK: &str = r#""selector":"COM0","remote_path":"/r""#;

#[test]
fn rpc_invalid_args_matrix() {
    use FileTransferKind::{Pull, Push};
    // 必填缺（push：selector／local_path／remote_path；pull：selector／remote_path）。
    for (kind, p) in [
        (Push, "{}"),
        (Push, r#"{"selector":"COM0","remote_path":"/r"}"#),
        (Push, r#"{"selector":"COM0","local_path":"/l"}"#),
        (Push, r#"{"local_path":"/l","remote_path":"/r"}"#),
        (Push, r#"{"selector":"","local_path":"/l","remote_path":"/r"}"#),
        (Pull, "{}"),
        (Pull, r#"{"selector":"COM0"}"#),
        (Pull, r#"{"remote_path":"/r"}"#),
    ] {
        assert_eq!(rejected(kind, params(p)), invalid_args(), "{kind:?} {p}");
    }
    // 轉型失敗（`int()`／`float()` 的 TypeError／ValueError）→ INVALID_ARGS，不穿越 RPC 邊界。
    for extra in [
        r#""chunk_size":"abc""#,
        r#""chunk_size":[1]"#,
        r#""chunk_size":"1.5""#,
        r#""chunk_timeout_s":"x""#,
        r#""chunk_timeout_s":"""#,
        r#""chunk_timeout_s":[]"#,
        r#""chunk_timeout_s":{}"#,
    ] {
        let p = params(&format!("{{{PUSH_OK},{extra}}}"));
        assert_eq!(rejected(Push, p), invalid_args(), "push {extra}");
    }
    for extra in [r#""chunk_timeout_s":"x""#, r#""chunk_timeout_s":"""#, r#""chunk_timeout_s":[0]"#] {
        let p = params(&format!("{{{PULL_OK},{extra}}}"));
        assert_eq!(rejected(Pull, p), invalid_args(), "pull {extra}");
    }
    // ack_mode 白名單（#161）：push、pull 都只回 {ok,error_code}（無 ack_mode 欄）。
    for kind in [Push, Pull] {
        let base = if kind == Push { PUSH_OK } else { PULL_OK };
        for bad in [r#""ehco""#, r#""AUTO""#, "1"] {
            let p = params(&format!("{{{base},\"ack_mode\":{bad}}}"));
            assert_eq!(rejected(kind, p), invalid_args(), "{kind:?} ack_mode={bad}");
        }
    }
    // 順序：數值轉型 → ack_mode → source → 必填（`service.py` 1426～1500）。
    assert_eq!(
        rejected(Push, params(r#"{"chunk_size":"abc","source":"system:recover"}"#)),
        invalid_args(),
        "轉型失敗先於 SOURCE_RESERVED"
    );
    assert_eq!(
        rejected(Push, params(r#"{"ack_mode":"x","source":"system"}"#)),
        invalid_args(),
        "ack_mode 先於 SOURCE_RESERVED"
    );
    let reserved = rejected(Push, params(r#"{"source":"system:recover"}"#));
    assert_eq!(
        keys(&reserved),
        ["ok", "error_code", "field", "value", "reserved_prefix", "hint"],
        "SOURCE_RESERVED 先於必填：{reserved:?}"
    );
    assert_eq!(code(&reserved).as_deref(), Some("SOURCE_RESERVED"));
    assert_eq!(reserved.get("value"), Some(&PyValue::from("system:recover")));
    assert_eq!(reserved.get("reserved_prefix"), Some(&PyValue::from("system")));
    assert_eq!(code(&rejected(Pull, params(r#"{"source":" System "}"#))).as_deref(), Some("SOURCE_RESERVED"));
}

#[test]
fn rpc_coercions_follow_python() {
    use FileTransferKind::{Pull, Push};
    // `max(1, int(params.get("chunk_size") or 512))`。
    for (raw, want) in [("0", 512), ("-5", 1), ("true", 1), (r#""7""#, 7), ("3.9", 3), (r#""""#, 512), ("null", 512), (r#"" 12 ""#, 12)] {
        let (req, _) = accepted(Push, params(&format!("{{{PUSH_OK},\"chunk_size\":{raw}}}")));
        assert_eq!(req.chunk_size, want, "chunk_size={raw}");
    }
    // `float(raw) if raw is not None else None`：0／false 照收（不是預設值）。
    for (raw, want) in [("null", None), ("0", Some(0.0)), ("-1", Some(-1.0)), ("false", Some(0.0)), (r#""2.5""#, Some(2.5)), ("7", Some(7.0))] {
        let (req, _) = accepted(Push, params(&format!("{{{PUSH_OK},\"chunk_timeout_s\":{raw}}}")));
        assert_eq!(req.chunk_timeout_s, want, "push chunk_timeout_s={raw}");
        let (req, _) = accepted(Pull, params(&format!("{{{PULL_OK},\"chunk_timeout_s\":{raw}}}")));
        assert_eq!(req.chunk_timeout_s, want, "pull chunk_timeout_s={raw}");
    }
    // `str(x or "")`、`str(x or "auto")`。
    let (req, check) = accepted(Push, params(r#"{"selector":7,"local_path":5,"remote_path":"/r","ack_mode":"none"}"#));
    assert_eq!(req.kind, Push);
    assert_eq!((req.selector.as_str(), req.local_path.as_deref(), req.ack_mode.as_str()), ("7", Some("5"), "none"));
    assert_eq!((req.source.as_str(), check.normalized_from.as_deref()), ("agent", None));
    let (req, _) = accepted(Push, params(&format!("{{{PUSH_OK},\"ack_mode\":\"\"}}")));
    assert_eq!(req.ack_mode, "auto");
    // pull：local_path 只有 truthy 才 `str()`；ack_mode 只驗不轉傳。
    for (raw, want) in [(r#""""#, None), ("0", None), ("null", None), (r#""x""#, Some("x")), ("5", Some("5"))] {
        let (req, _) = accepted(Pull, params(&format!("{{{PULL_OK},\"local_path\":{raw}}}")));
        assert_eq!(req.local_path.as_deref(), want, "pull local_path={raw}");
    }
    let (req, _) = accepted(Pull, params(&format!("{{{PULL_OK},\"ack_mode\":\"none\"}}")));
    assert_eq!((req.kind, req.ack_mode.as_str(), req.chunk_size), (Pull, "auto", 512));
    // 來源正規化（OD-74）：bare human → human:cli（normalized_from 保留原文）；明寫 human:<x> 原樣。
    let (req, check) = accepted(Push, params(&format!("{{{PUSH_OK},\"source\":\"human\"}}")));
    assert_eq!((req.source.as_str(), check.normalized_from.as_deref()), ("human:cli", Some("human")));
    let (req, check) = accepted(Pull, params(&format!("{{{PULL_OK},\"source\":\"human:x\"}}")));
    assert_eq!((req.source.as_str(), check.normalized_from.as_deref()), ("human:x", None));
}

#[test]
fn rpc_overflow_is_exception_not_invalid_args() {
    // Python `int(float("inf"))`／`float(10**400)` 是 OverflowError，不在 `except (TypeError, ValueError)` 內：
    // 穿越 RPC 邊界成為 framing EXCEPTION。
    let mut p = params(&format!("{{{PUSH_OK}}}"));
    p.insert("chunk_size", PyValue::Float(f64::INFINITY));
    assert!(FileTransferRequest::from_rpc(FileTransferKind::Push, &p).is_err(), "int(inf) 應為例外");
    let mut p = params(&format!("{{{PULL_OK}}}"));
    p.insert("chunk_timeout_s", PyValue::Int(PyInt::Big(format!("1{}", "0".repeat(400)))));
    assert!(FileTransferRequest::from_rpc(FileTransferKind::Pull, &p).is_err(), "float(10**400) 應為例外");
    // NaN 的 int() 是 ValueError → INVALID_ARGS。
    let mut p = params(&format!("{{{PUSH_OK}}}"));
    p.insert("chunk_size", PyValue::Float(f64::NAN));
    assert_eq!(rejected(FileTransferKind::Push, p), invalid_args());
}

#[test]
fn push_pull_roundtrip_through_actor() {
    let x = XferRig::ready(SHELL_PROFILE, ShellCaps::busybox_base64());
    let data: Vec<u8> = (0..20).flat_map(|_| 0u8..=255).collect();
    let local = x.tmp.write("blob.bin", &data);
    let r = x.transfer_ok(push_req(&local, "/tmp/dst.bin"));
    assert_eq!(
        r,
        pydict! {
            "ok" => true,
            "bytes" => PyValue::Int(PyInt::Small(5120)),
            "chunks" => PyValue::Int(PyInt::Small(10)),
            "md5" => t::md5_hex(&data),
            "remote_path" => "/tmp/dst.bin",
        }
    );
    assert_eq!(x.peer.file("/tmp/dst.bin").as_deref(), Some(&data[..]), "外部 oracle：DUT 端檔案內容");
    assert_eq!(x.peer.lines_containing("SW_XFER_CHUNK_"), 10, "DUT 收到的 chunk 命令恰 10 行（不重送）");
    assert!(
        x.wal_tx_cmd_ids().iter().any(|id| id.starts_with("ft-") && id.ends_with("-0")),
        "chunk TX 以 ft-<8hex>-<idx> 歸因：{:?}",
        x.wal_tx_cmd_ids()
    );
    assert_eq!(x.rig.get_state().get("foreground_busy"), Some(&PyValue::Bool(false)));

    let back = x.tmp.join("back.bin");
    let r = x.transfer_ok(pull_req("/tmp/dst.bin", Some(&back)));
    assert_eq!(
        r,
        pydict! {
            "ok" => true,
            "bytes" => PyValue::Int(PyInt::Small(5120)),
            "md5" => t::md5_hex(&data),
            "local_path" => back.clone(),
        }
    );
    assert_eq!(std::fs::read(&back).expect("取回檔"), data);
}

#[test]
fn passthrough_ready_returns_profile_not_command_capable_zero_tx() {
    let rig = ActorRig::new(PASSTHROUGH_PROFILE).attach();
    rig.wait_state("ATTACHED");
    let expect_reject = |rig: &ActorRig, state: &str| {
        for req in [push_req("/nonexistent", "/r"), pull_req("/r", None)] {
            let r = rig.request(SessionRequest::FileTransfer(req));
            assert_eq!(keys(&r), ["ok", "error_code", "hint", "session"], "{state}：{r:?}");
            assert_eq!(code(&r).as_deref(), Some("PROFILE_NOT_COMMAND_CAPABLE"));
            assert_eq!(
                r.get("hint"),
                Some(&PyValue::from("passthrough profile 不支援 file.push／file.pull；請使用互動 console。"))
            );
            match r.get("session") {
                Some(PyValue::Dict(s)) => assert_eq!(s.get("state"), Some(&PyValue::from(state))),
                other => panic!("session 應為 dict：{other:?}"),
            }
        }
        assert_eq!(rig.get_state().get("foreground_busy"), Some(&PyValue::Bool(false)), "不占前景槽");
    };
    // SC-35：passthrough 任何 state 皆同（explicit target attach 零 TX、停在 ATTACHED）。
    expect_reject(&rig, "ATTACHED");
    // 30 s 無 RX 自動升 READY 後仍拒絕。
    rig.advance(Duration::from_secs(31));
    rig.wait_state("READY");
    expect_reject(&rig, "READY");
    assert_eq!(rig.dev.tx_len(), 0, "外部 oracle：DUT 零 byte");
    assert!(rig.wal_tx_payloads().is_empty(), "WAL 無 TX 行");
    // 對照：同一 session 的 command.submit 仍可進 Arbiter。
    let _ = rig.submit_ok("echo hi");
}

#[test]
fn quiet_before_start_zero_tx() {
    let x = XferRig::ready(SHELL_PROFILE, ShellCaps::busybox_base64());
    let local = x.tmp.write("a.bin", b"hello");
    x.peer.emit(BANNER);
    x.wait_quiet_armed();
    let before = x.peer.tx_len();
    let wal_before = x.rig.wal_tx_payloads().len();
    // DF-02／OD-74：RPC 路徑 trusted_human=False，明寫 human:<x> 與 bare human 皆比照 agent。
    for source in ["agent", "human:x", "human:cli"] {
        let mut push = push_req(&local, "/tmp/q.bin");
        push.source = source.into();
        let mut pull = pull_req("/tmp/q.bin", None);
        pull.source = source.into();
        for req in [push, pull] {
            let r = x.transfer_ok(req);
            assert_eq!(keys(&r), ["ok", "error_code", "retry_after_s", "hint", "session"], "{source}：{r:?}");
            assert_eq!(code(&r).as_deref(), Some("AUTOBOOT_QUIET"));
            match r.get("retry_after_s") {
                Some(PyValue::Float(v)) => assert!(*v > 0.0 && *v <= 180.0, "{v}"),
                other => panic!("retry_after_s 應為浮點：{other:?}"),
            }
        }
    }
    std::thread::sleep(Duration::from_millis(50));
    assert_eq!(x.peer.tx_len(), before, "外部 oracle：被拒時 DUT 零新增 bytes");
    assert_eq!(x.rig.wal_tx_payloads().len(), wal_before, "WAL 無新 TX 行");
    assert_eq!(x.rig.get_state().get("foreground_busy"), Some(&PyValue::Bool(false)));
}

/// 3 個 chunk 的 push，peer 停在第 2 個 chunk 命令的第一個 slice（64 字元）之後。
fn push_held_in_chunk_loop(x: &XferRig) -> crossbeam_channel::Receiver<Result<PyDict, sw_proto::pycompat::PyErr>> {
    let data: Vec<u8> = (0..1536u32).map(|i| (i * 7 % 251) as u8).collect();
    let local = x.tmp.write("three.bin", &data);
    x.peer.hold_at_line(2, t::DEFAULT_ECHO_SLICE_SIZE);
    let rx = x.transfer_async(push_req(&local, "/tmp/three.bin"));
    x.wait_peer_reached_hold();
    assert_eq!(x.peer.lines_containing("SW_XFER_CHUNK_"), 1, "chunk 0 已完成、chunk 1 卡在第一個 slice");
    rx
}

#[test]
fn quiet_midway_returns_autoboot_quiet_no_cleanup() {
    let x = XferRig::ready(SHELL_PROFILE, ShellCaps::busybox_base64());
    let rx = push_held_in_chunk_loop(&x);
    x.wait_peer_holding();
    // 傳輸中 RX 出現 boot banner：quiet arm（fence 同步）；之後 peer 回顯該 slice，引擎下一個 slice 被 fence 拒絕（零寫出）。
    x.peer.emit(BANNER);
    x.wait_quiet_armed();
    let held = x.peer.tx_len();
    x.peer.release_hold();
    let r = rx.recv_timeout(XFER_WAIT).expect("回覆").expect("字典");
    assert_eq!(keys(&r), ["ok", "error_code", "selector", "retry_after_s", "chunks_sent", "chunks_total"], "{r:?}");
    assert_eq!(code(&r).as_deref(), Some("AUTOBOOT_QUIET"));
    assert_eq!(r.get("selector"), Some(&PyValue::from("COM0")));
    match r.get("retry_after_s") {
        Some(PyValue::Float(v)) => assert!(*v > 0.0 && *v <= 180.0, "{v}"),
        other => panic!("retry_after_s 應為浮點：{other:?}"),
    }
    assert_eq!((int_of(&r, "chunks_sent"), int_of(&r, "chunks_total")), (Some(1), Some(3)));
    std::thread::sleep(Duration::from_millis(50));
    assert_eq!(x.peer.tx_len(), held, "外部 oracle：被拒之後 DUT 零新增 bytes（不送 cleanup、不重送）");
    assert_eq!(x.peer.lines_containing("rm -f"), 0, "無 cleanup 命令");
    assert!(x.rig.wal_meta_events().iter().all(|m| m.get("event") != Some(&PyValue::from("command_aborted_in_flight"))));
    assert_eq!(x.rig.get_state().get("foreground_busy"), Some(&PyValue::Bool(false)));
}

fn release(x: &XferRig) -> PyDict {
    x.rig
        .request_async(SessionRequest::Release { source: "agent:t6".into(), reason: PyValue::Null })
        .recv_timeout(XFER_WAIT)
        .expect("release 回覆")
        .expect("release 字典")
}

#[test]
fn drain_midway_with_written_bytes_returns_aborted_in_flight_and_meta() {
    let x = XferRig::ready(SHELL_PROFILE, ShellCaps::busybox_base64());
    let rx = push_held_in_chunk_loop(&x);
    x.wait_peer_holding();
    let written = x.peer.tx_len();
    let rel = release(&x);
    assert_eq!(rel.get("ok"), Some(&PyValue::Bool(true)), "{rel:?}");
    assert_eq!(rel.get("aborted_cmd"), Some(&PyValue::Bool(true)), "{rel:?}");
    // drain 經 T5 取消機制喚醒等待中的引擎（FakeClock 不推進，等待不會自行逾時）。
    let r = rx.recv_timeout(XFER_WAIT).expect("回覆").expect("字典");
    assert_eq!(
        r,
        pydict! {
            "ok" => false,
            "error_code" => "COMMAND_ABORTED_IN_FLIGHT",
            "chunks_sent" => PyValue::Int(PyInt::Small(1)),
            "chunks_total" => PyValue::Int(PyInt::Small(3)),
        }
    );
    let metas: Vec<PyDict> = x
        .rig
        .wal_rows()
        .into_iter()
        .filter(|row| {
            matches!(row.get("meta"), Some(PyValue::Dict(m)) if m.get("event") == Some(&PyValue::from("command_aborted_in_flight")))
        })
        .collect();
    assert_eq!(metas.len(), 1, "恰一筆 command_aborted_in_flight META：{metas:?}");
    let cmd_id = metas[0].get("cmd_id").and_then(PyValue::as_str).unwrap_or("").to_string();
    assert!(
        cmd_id.len() == 11 && cmd_id.starts_with("ft-") && cmd_id[3..].chars().all(|c| c.is_ascii_hexdigit()),
        "cmd_id 為傳輸前綴 ft-<8hex>：{cmd_id}"
    );
    let meta = match metas[0].get("meta") {
        Some(PyValue::Dict(m)) => m.clone(),
        _ => unreachable!(),
    };
    assert_eq!(meta.get("confirmed_tx_bytes"), Some(&PyValue::Int(PyInt::Small(written as i128))), "整段累計已確認 bytes");
    assert_eq!(meta.get("tx_outcome"), Some(&PyValue::from("partial")));
    std::thread::sleep(Duration::from_millis(50));
    assert_eq!(x.peer.tx_len(), written, "外部 oracle：中止後 DUT 零新增 bytes（不重送、不 cleanup）");
}

#[test]
fn drain_midway_zero_written_returns_flushed_by_recovery() {
    let x = XferRig::ready(SHELL_PROFILE, ShellCaps::busybox_base64());
    let local = x.tmp.write("z.bin", b"zero");
    let submits = x.rig.dev.os_submits();
    // 第一筆傳輸 TX（decoder probe）交 OS 後零確認、阻塞到 drain 取消。
    x.rig.dev.script_write(vec![FakeWriteStep::BlockUntilCancel { accept_before_cancel: 0 }]);
    let rx = x.transfer_async(push_req(&local, "/tmp/z.bin"));
    wait_real("probe TX 交 OS 後阻塞", || x.rig.dev.os_submits() > submits);
    x.wait_foreground_busy(true);
    let rel = release(&x);
    assert_eq!(rel.get("ok"), Some(&PyValue::Bool(true)), "{rel:?}");
    let r = rx.recv_timeout(XFER_WAIT).expect("回覆").expect("字典");
    assert_eq!(r, pydict! { "ok" => false, "error_code" => "FLUSHED_BY_RECOVERY" });
    assert_eq!(x.peer.tx_len(), 0, "外部 oracle：DUT 零 byte");
    assert!(
        x.rig.wal_meta_events().iter().all(|m| m.get("event") != Some(&PyValue::from("command_aborted_in_flight"))),
        "零寫出不寫 aborted META"
    );
}

#[test]
fn foreground_busy_returns_session_busy() {
    let x = XferRig::ready(SHELL_PROFILE, ShellCaps::busybox_base64());
    x.peer.hold_now();
    let cmd = x.rig.submit_ok("sleep 100");
    x.rig.wait_running(&cmd);
    wait_real("前景命令已送出", || x.peer.tx_len() > 0);
    let before = x.peer.tx_len();
    for req in [push_req("/nonexistent", "/r"), pull_req("/r", None)] {
        let r = x.transfer_ok(req);
        assert_eq!(r, pydict! { "ok" => false, "error_code" => "SESSION_BUSY", "selector" => "COM0" });
    }
    std::thread::sleep(Duration::from_millis(50));
    assert_eq!(x.peer.tx_len(), before, "外部 oracle：SESSION_BUSY 零 TX");
    x.peer.release_hold();
    let _ = x.rig.wait_done(&cmd);
}

#[test]
fn agent_lease_returns_interactive_busy_zero_tx() {
    let x = XferRig::ready(SHELL_PROFILE, ShellCaps::busybox_base64());
    let open = x.rig.request(SessionRequest::InteractiveOpen {
        owner: "agent:lease".into(),
        timeout_s: 60.0,
        command: String::new(),
        allow_attached: false,
    });
    assert_eq!(open.get("ok"), Some(&PyValue::Bool(true)), "{open:?}");
    let before = x.peer.tx_len();
    for req in [push_req("/nonexistent", "/r"), pull_req("/r", None)] {
        assert_eq!(x.transfer_ok(req), pydict! { "ok" => false, "error_code" => "SESSION_INTERACTIVE_BUSY" });
    }
    std::thread::sleep(Duration::from_millis(50));
    assert_eq!(x.peer.tx_len(), before, "外部 oracle：lease 拒絕零 TX");
}

#[test]
fn non_ready_states_rejected_with_session_zero_tx() {
    let x = XferRig::ready(SHELL_PROFILE, ShellCaps::busybox_base64());
    let before = x.peer.tx_len();
    for (state, want) in [
        (SessionState::Recovering, "SESSION_RECOVERING"),
        (SessionState::Flashing, "FLASHING_BUSY"),
        (SessionState::Attached, "SESSION_NOT_READY"),
        (SessionState::Detached, "SESSION_NOT_READY"),
    ] {
        let _ = x.rig.request(SessionRequest::ForceStateForTest(state));
        for req in [push_req("/nonexistent", "/r"), pull_req("/r", None)] {
            let r = x.transfer_ok(req);
            assert_eq!(keys(&r), ["ok", "error_code", "session"], "{state:?}：{r:?}");
            assert_eq!(code(&r).as_deref(), Some(want), "{state:?}");
        }
    }
    std::thread::sleep(Duration::from_millis(50));
    assert_eq!(x.peer.tx_len(), before, "外部 oracle：非 READY 拒絕零 TX");
}

#[test]
fn timeout_derivation_push_and_pull() {
    let x = XferRig::ready(SHELL_PROFILE, ShellCaps::busybox_base64());
    let local = x.tmp.write("t.bin", b"t");
    // SHELL_PROFILE timeout_s=2.0：push chunk／probe 等待＝max(2.0, 5.0)；pull＝max(2.0, 30.0)；peer 不回應 probe。
    x.peer.hold_now();
    for (req, total_s) in [(push_req(&local, "/tmp/t.bin"), 5u64), (pull_req("/tmp/t.bin", Some(&x.tmp.join("t.out"))), 30)] {
        let lines_before = x.peer.tx_len();
        let rx = x.transfer_async(req);
        wait_real("probe 已送達 DUT", || x.peer.dut_bytes()[lines_before..].contains(&b'\n'));
        std::thread::sleep(Duration::from_millis(30));
        x.rig.advance(Duration::from_secs(total_s - 1));
        std::thread::sleep(Duration::from_millis(30));
        assert!(rx.try_recv().is_err(), "期限前不得逾時（{total_s}s）");
        x.rig.advance(Duration::from_secs(1));
        let r = rx.recv_timeout(XFER_WAIT).expect("回覆").expect("字典");
        assert_eq!(r, pydict! { "ok" => false, "error_code" => "TRANSFER_TIMEOUT" }, "{total_s}s");
    }
}

#[test]
fn timeout_zero_or_negative_accepted_and_probe_times_out() {
    let x = XferRig::ready(SHELL_PROFILE, ShellCaps::busybox_base64());
    let local = x.tmp.write("z.bin", b"z");
    for value in [0.0, -1.0] {
        let before = x.peer.lines_containing("SW_XFER_DECODER_");
        let mut req = push_req(&local, "/tmp/z.bin");
        req.chunk_timeout_s = Some(value);
        // EVT-D-F1：timeout <= 0 時不做任何檢查即逾時；probe 已送出 1 筆 TX。
        assert_eq!(x.transfer_ok(req), pydict! { "ok" => false, "error_code" => "TRANSFER_TIMEOUT" }, "{value}");
        wait_real("probe 行送達", || x.peer.lines_containing("SW_XFER_DECODER_") == before + 1);
    }
}

#[test]
fn line_limit_from_profile_zero_tx() {
    let yaml = SHELL_PROFILE.replace("    timeout_s: 2.0\n", "    timeout_s: 2.0\n    max_console_line_chars: 200\n");
    let x = XferRig::ready(&yaml, ShellCaps::busybox_base64());
    let local = x.tmp.write("l.bin", b"line");
    let r = x.transfer_ok(push_req(&local, "/tmp/l.bin"));
    assert_eq!(
        r,
        pydict! {
            "ok" => false,
            "error_code" => "CONSOLE_LINE_LIMIT_TOO_SMALL",
            "max_console_line_chars" => PyValue::Int(PyInt::Small(200)),
        }
    );
    assert_eq!(x.peer.tx_len(), 0, "外部 oracle：行長預檢失敗零 TX");
}

#[test]
fn pull_local_write_error_is_exception() {
    let x = XferRig::ready(SHELL_PROFILE, ShellCaps::busybox_base64());
    x.peer.put_file("/tmp/src.bin", b"payload");
    let bad = x.tmp.join("no/such/dir/x.bin");
    let err = x.transfer(pull_req("/tmp/src.bin", Some(&bad))).expect_err("本地寫入失敗應穿出為例外");
    assert!(err.message.contains("No such file or directory") && err.message.contains(&bad), "{}", err.message);
    assert!(x.peer.lines_containing("base64 < ") >= 1, "pull 命令已送出（有 TX）");
    assert_eq!(x.rig.get_state().get("foreground_busy"), Some(&PyValue::Bool(false)));
}

/// 接線驗收（review-convergence-rules §5）：drain 必須經 T5 的傳輸取消（`RunCancel::watching_transfers`）喚醒等待中的
/// 引擎，而不是等 echo-ACK 逾時（`max(timeout_s, 5.0)`＝5 s）自然結束。ActorRig 的 FakeClock 於等待時自行推進，故以
/// 「release 到回覆之間的虛擬時間」判定：取消生效時引擎立即返回；取消檢查失效時須耗盡 5 s 的 echo 等待。
#[test]
fn drain_midway_wakes_engine_without_waiting_echo_timeout() {
    let x = XferRig::ready(SHELL_PROFILE, ShellCaps::busybox_base64());
    let rx = push_held_in_chunk_loop(&x);
    x.wait_peer_holding();
    let t0 = x.rig.clock.mono_ns();
    let rel = release(&x);
    assert_eq!(rel.get("ok"), Some(&PyValue::Bool(true)), "{rel:?}");
    let r = rx.recv_timeout(XFER_WAIT).expect("回覆").expect("字典");
    let elapsed_ns = x.rig.clock.mono_ns().saturating_sub(t0);
    assert_eq!(code(&r).as_deref(), Some("COMMAND_ABORTED_IN_FLIGHT"), "{r:?}");
    assert!(
        elapsed_ns < 2_000_000_000,
        "drain 應立即喚醒引擎（虛擬時間經過 {elapsed_ns} ns，echo 等待上限 5 s）"
    );
}

#[test]
fn attached_not_command_capable_rejected_with_hint_zero_tx() {
    // 非 passthrough、無 ready_probe 的 profile 停在 ATTACHED：與 command.submit 相同的 PROFILE_NOT_COMMAND_CAPABLE＋hint。
    let rig = ActorRig::new(support::SHELL_NO_PROBE_PROFILE).attach();
    rig.wait_state("ATTACHED");
    let before = rig.dev.tx_len();
    for req in [push_req("/nonexistent", "/r"), pull_req("/r", None)] {
        let r = rig.request(SessionRequest::FileTransfer(req));
        assert_eq!(keys(&r), ["ok", "error_code", "hint", "session"], "{r:?}");
        assert_eq!(code(&r).as_deref(), Some("PROFILE_NOT_COMMAND_CAPABLE"));
        let hint = r.get("hint").and_then(PyValue::as_str).unwrap_or("");
        assert!(hint.starts_with("此 profile 僅支援 console。") && hint.contains("ready_probe"), "{hint}");
    }
    std::thread::sleep(Duration::from_millis(50));
    assert_eq!(rig.dev.tx_len(), before, "外部 oracle：零 TX");
}

/// human raw console 的按鍵在 push 期間進 DeferredBuffer（admission 關），傳輸結束（resume＋admission 開）後才回放；
/// 外部 byte 序列中按鍵不夾在任何傳輸命令行內（no_forbidden_interleave）。
#[test]
fn human_keys_during_push_replayed_after_transfer_not_interleaved() {
    let x = XferRig::ready_with_hub(SHELL_PROFILE, ShellCaps::busybox_base64());
    let console = x.rig.attach_pty_console("c1");
    let rx = push_held_in_chunk_loop(&x);
    x.wait_peer_holding();
    assert_eq!(x.rig.get_state().get("foreground_busy"), Some(&PyValue::Bool(true)));
    console.push_input(b"KEYZ");
    std::thread::sleep(Duration::from_millis(80));
    let _ = x.rig.get_state();
    assert!(
        !x.peer.dut_bytes().windows(4).any(|w| w == b"KEYZ"),
        "傳輸期間 human 按鍵不得上 UART（應暫存於 deferred）"
    );
    x.peer.release_hold();
    let r = rx.recv_timeout(XFER_WAIT).expect("回覆").expect("字典");
    assert_eq!(r.get("ok"), Some(&PyValue::Bool(true)), "{r:?}");
    wait_real("傳輸結束後回放 human 按鍵", || x.peer.dut_bytes().windows(4).any(|w| w == b"KEYZ"));
    let dut = x.peer.dut_bytes();
    let key_at = dut.windows(4).position(|w| w == b"KEYZ").expect("KEYZ");
    let last_nl = dut.iter().rposition(|b| *b == b'\n').expect("傳輸命令行");
    assert!(key_at > last_nl, "按鍵必須在最後一個傳輸命令行之後（不夾入 chunk 行）：key_at={key_at} last_nl={last_nl}");
    assert_eq!(dut.windows(4).filter(|w| *w == b"KEYZ").count(), 1, "回放恰一次");
    assert_eq!(x.peer.lines_containing("SW_XFER_CHUNK_"), 3);
}

/// push 期間進 deferred 的 human 按鍵，在傳輸被 `device.release` 中止後永不回放（BC-02 input_epoch；no_stale_context_replay）。
#[test]
fn release_during_push_discards_deferred_human_bytes() {
    let x = XferRig::ready_with_hub(SHELL_PROFILE, ShellCaps::busybox_base64());
    let console = x.rig.attach_pty_console("c1");
    let rx = push_held_in_chunk_loop(&x);
    x.wait_peer_holding();
    console.push_input(b"KEYZ");
    std::thread::sleep(Duration::from_millis(80));
    let _ = x.rig.get_state();
    let rel = release(&x);
    assert_eq!(rel.get("ok"), Some(&PyValue::Bool(true)), "{rel:?}");
    let r = rx.recv_timeout(XFER_WAIT).expect("回覆").expect("字典");
    assert_eq!(code(&r).as_deref(), Some("COMMAND_ABORTED_IN_FLIGHT"), "{r:?}");
    let after = x.peer.tx_len();
    std::thread::sleep(Duration::from_millis(150));
    assert_eq!(x.peer.tx_len(), after, "RELEASED 後 DUT 零新增 bytes");
    assert!(!x.peer.dut_bytes().windows(4).any(|w| w == b"KEYZ"), "deferred 按鍵不得於 release 後回放");
}
