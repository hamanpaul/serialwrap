#![cfg(feature = "test-fakes")]
//! P8-T4：`push_file`／`pull_file` 引擎流程與錯誤形狀（含 OD-62 例外語意、OD-80 分段 pull）。
mod support;

use std::time::Duration;

use support::fake_shell::{rej, FakeShell, FixedIds, MemFiles, ShellCaps};
use sw_core::file_transfer::templates::{self as t, md5_hex};
use sw_core::file_transfer::{pull_file, push_file, PullParams, PushParams, RejectReason, TransferAbort};
use sw_proto::pyjson::{dumps, DumpOpts, PyDict, PyValue, Sep};
use sw_proto::pypath::PathFlavor;

fn j(d: PyDict) -> String {
    dumps(
        &PyValue::Dict(d),
        &DumpOpts { sep: Sep::Compact, ensure_ascii: false, sort_keys: false, allow_nan: true },
    )
}

fn push_params(local: &str, remote: &str) -> PushParams {
    PushParams {
        local_path: local.into(),
        remote_path: remote.into(),
        chunk_size: 512,
        timeout: Duration::from_secs(10),
        prompt_regex: r"(?m)^# $".into(),
        source: "agent".into(),
        ack_mode: "auto".into(),
        echo_slice_size: 64,
        echo_timeout: Duration::from_secs(5),
        max_console_line_chars: None,
    }
}

fn pull_params(remote: &str) -> PullParams {
    PullParams {
        remote_path: remote.into(),
        local_path: None,
        timeout: Duration::from_secs(30),
        prompt_regex: r"(?m)^# $".into(),
        source: "agent".into(),
        max_console_line_chars: None,
        path_flavor: PathFlavor::Posix,
    }
}

fn data_n(n: usize) -> Vec<u8> {
    (0u8..=255).cycle().take(n).collect()
}

fn push_ids() -> FixedIds {
    FixedIds::new(&["0123456789ab", "bbbbbbbb"])
}

/// 跑一次 push，回 (結果, shell)。
fn run_push(
    caps: ShellCaps,
    data: &[u8],
    tweak: impl FnOnce(&mut PushParams),
) -> (Result<PyDict, TransferAbort>, FakeShell) {
    let files = MemFiles::with("/host/a.bin", data);
    let mut sh = FakeShell::new(caps);
    let mut p = push_params("/host/a.bin", "/tmp/dst.bin");
    tweak(&mut p);
    let r = push_file(&mut sh, &files, &mut push_ids(), &p);
    (r, sh)
}

fn run_pull(
    caps: ShellCaps,
    remote: Option<&[u8]>,
    tweak: impl FnOnce(&mut PullParams),
) -> (Result<PyDict, TransferAbort>, FakeShell, MemFiles) {
    let files = MemFiles::empty();
    let mut sh = FakeShell::new(caps);
    if let Some(d) = remote {
        sh.put_file("/etc/blob.bin", d);
    }
    let mut p = pull_params("/etc/blob.bin");
    tweak(&mut p);
    let r = pull_file(&mut sh, &files, &mut FixedIds::new(&["eeeeeeee"]), &p);
    (r, sh, files)
}

// ── 成功路徑 ──

#[test]
fn push_roundtrip_ok() {
    let data = data_n(1300);
    let (r, sh) = run_push(ShellCaps::busybox_base64(), &data, |_| {});
    assert_eq!(
        j(r.unwrap()),
        format!(r#"{{"ok":true,"bytes":1300,"chunks":3,"md5":"{}","remote_path":"/tmp/dst.bin"}}"#, md5_hex(&data))
    );
    assert_eq!(sh.file("/tmp/dst.bin").unwrap(), data);
    assert!(sh.file("/tmp/.sw_upload_0123456789ab").is_none());
    assert_eq!(
        sh.cmd_ids(),
        ["ft-bbbbbbbb-probe", "ft-bbbbbbbb-0", "ft-bbbbbbbb-1", "ft-bbbbbbbb-2", "ft-bbbbbbbb-md5", "ft-bbbbbbbb-mv"]
    );
    assert_eq!(sh.paced_cmd_ids(), ["ft-bbbbbbbb-0", "ft-bbbbbbbb-1", "ft-bbbbbbbb-2"], "auto 模式下只有 chunk 走 echo-paced");
}

#[test]
fn push_roundtrip_openssl_decoder() {
    let data = data_n(700);
    let (r, sh) = run_push(ShellCaps::openssl_only(), &data, |_| {});
    assert!(j(r.unwrap()).starts_with(r#"{"ok":true,"bytes":700,"chunks":2,"#));
    assert_eq!(sh.file("/tmp/dst.bin").unwrap(), data);
}

#[test]
fn pull_roundtrip_ok() {
    let data = data_n(1300);
    let (r, sh, files) = run_pull(ShellCaps::busybox_base64(), Some(&data), |_| {});
    assert_eq!(
        j(r.unwrap()),
        format!(r#"{{"ok":true,"bytes":1300,"md5":"{}","local_path":"blob.bin"}}"#, md5_hex(&data))
    );
    assert_eq!(files.get("blob.bin").unwrap(), data);
    assert_eq!(sh.cmd_ids(), ["ft-eeeeeeee-probe", "ft-eeeeeeee-b64", "ft-eeeeeeee-md5"]);
}

#[test]
fn pull_roundtrip_openssl_keeps_newlines_and_writes_basename_in_cwd() {
    let data = b"hello\n\x00\xff world".repeat(40);
    let (r, sh, files) = run_pull(ShellCaps::openssl_only(), Some(&data), |_| {});
    assert_eq!(
        j(r.unwrap()),
        format!(r#"{{"ok":true,"bytes":{},"md5":"{}","local_path":"blob.bin"}}"#, data.len(), md5_hex(&data))
    );
    assert_eq!(files.get("blob.bin").unwrap(), data);
    assert_eq!(sh.cmd_ids(), ["ft-eeeeeeee-probe", "ft-eeeeeeee-b64", "ft-eeeeeeee-md5"]);
}

#[test]
fn push_echo_stall_sends_ctrl_u_once_then_cleanup() {
    let mut caps = ShellCaps::busybox_base64();
    caps.echo_stall_after_chars = Some(100);
    let (r, sh) = run_push(caps, &[7u8; 600], |_| {});
    assert_eq!(
        j(r.unwrap()),
        r#"{"ok":false,"error_code":"TRANSFER_ECHO_STALL","chunks_sent":0,"chunks_total":2,"acked_chars":64,"sent_chars":128}"#
    );
    assert_eq!(sh.raw_tx_count(b"\x15\n"), 1);
    assert_eq!(sh.cmd_ids().last().unwrap(), "ft-bbbbbbbb-cleanup");
    assert_eq!(sh.send_attempts(), 4, "probe、chunk0、^U、cleanup");
}

// ── 錯誤形狀族（push）──

#[test]
fn push_invalid_ack_mode() {
    let (r, sh) = run_push(ShellCaps::busybox_base64(), b"x", |p| p.ack_mode = "ehco".into());
    assert_eq!(j(r.unwrap()), r#"{"ok":false,"error_code":"INVALID_ARGS","ack_mode":"ehco"}"#);
    assert_eq!(sh.send_attempts(), 0);
}

#[test]
fn push_budget_zero() {
    let (r, sh) = run_push(ShellCaps::busybox_base64(), b"x", |p| p.max_console_line_chars = Some(0));
    assert_eq!(j(r.unwrap()), r#"{"ok":false,"error_code":"CONSOLE_LINE_LIMIT_TOO_SMALL","max_console_line_chars":0}"#);
    assert_eq!(sh.send_attempts(), 0);
}

#[test]
fn push_chunk_size_zero_direct_api() {
    let (r, sh) = run_push(ShellCaps::busybox_base64(), b"x", |p| p.chunk_size = 0);
    assert_eq!(j(r.unwrap()), r#"{"ok":false,"error_code":"INVALID_ARGS","chunk_size":0}"#);
    assert_eq!(sh.send_attempts(), 0);
}

#[test]
fn push_local_missing() {
    let files = MemFiles::empty();
    let mut sh = FakeShell::new(ShellCaps::busybox_base64());
    let r = push_file(&mut sh, &files, &mut push_ids(), &push_params("/nope", "/t/x")).unwrap();
    assert_eq!(j(r), r#"{"ok":false,"error_code":"LOCAL_FILE_NOT_FOUND","local_path":"/nope"}"#);
    assert_eq!(sh.send_attempts(), 0);
}

#[test]
fn push_echo_unsupported() {
    let mut caps = ShellCaps::busybox_base64();
    caps.supports_echo_paced = false;
    let (r, sh) = run_push(caps, b"x", |p| p.ack_mode = "echo".into());
    assert_eq!(j(r.unwrap()), r#"{"ok":false,"error_code":"ECHO_ACK_UNSUPPORTED"}"#);
    assert_eq!(sh.send_attempts(), 0);
}

#[test]
fn push_auto_without_paced_support_falls_back_to_plain_send() {
    let mut caps = ShellCaps::busybox_base64();
    caps.supports_echo_paced = false;
    let (r, sh) = run_push(caps, b"hello", |_| {});
    assert!(j(r.unwrap()).starts_with(r#"{"ok":true,"bytes":5,"#));
    assert!(sh.paced_cmd_ids().is_empty());
}

#[test]
fn push_budget_fixed_cmds() {
    let (r, sh) = run_push(ShellCaps::busybox_base64(), b"x", |p| p.max_console_line_chars = Some(60));
    assert_eq!(j(r.unwrap()), r#"{"ok":false,"error_code":"CONSOLE_LINE_LIMIT_TOO_SMALL","max_console_line_chars":60}"#);
    assert_eq!(sh.send_attempts(), 0);
}

#[test]
fn push_budget_raw_capacity_zero() {
    let tmp = "/tmp/.sw_upload_0123456789ab";
    let overhead = [("base64", ">"), ("base64", ">>"), ("openssl", ">"), ("openssl", ">>")]
        .iter()
        .map(|(d, op)| t::chunk_command("", d, op, tmp).unwrap().len())
        .max()
        .unwrap();
    let budget = (overhead + 3) as i128;
    let (r, sh) = run_push(ShellCaps::busybox_base64(), b"x", |p| p.max_console_line_chars = Some(budget));
    assert_eq!(
        j(r.unwrap()),
        format!(r#"{{"ok":false,"error_code":"CONSOLE_LINE_LIMIT_TOO_SMALL","max_console_line_chars":{budget}}}"#)
    );
    assert_eq!(sh.send_attempts(), 0);
}

#[test]
fn push_budget_limits_effective_chunk_size() {
    // raw_capacity = (300 - 116) // 4 * 3 = 138 → 300 bytes 分 138/138/24 三段。
    let data = data_n(300);
    let (r, sh) = run_push(ShellCaps::busybox_base64(), &data, |p| p.max_console_line_chars = Some(300));
    assert!(j(r.unwrap()).starts_with(r#"{"ok":true,"bytes":300,"chunks":3,"#));
    assert_eq!(sh.file("/tmp/dst.bin").unwrap(), data);
}

#[test]
fn push_probe_timeout() {
    let mut caps = ShellCaps::busybox_base64();
    caps.drop_prompt_on = Some("-probe");
    let (r, sh) = run_push(caps, b"x", |_| {});
    assert_eq!(j(r.unwrap()), r#"{"ok":false,"error_code":"TRANSFER_TIMEOUT"}"#);
    assert_eq!(sh.send_attempts(), 1);
}

#[test]
fn push_probe_shell_error() {
    let mut caps = ShellCaps::busybox_base64();
    caps.probe_shell_error = true;
    let (r, sh) = run_push(caps, b"x", |_| {});
    assert_eq!(j(r.unwrap()), r#"{"ok":false,"error_code":"TRANSFER_PROBE_FAILED"}"#);
    assert_eq!(sh.send_attempts(), 1);
}

#[test]
fn push_decoder_missing() {
    let (r, sh) = run_push(ShellCaps::no_tools(), b"x", |_| {});
    assert_eq!(j(r.unwrap()), r#"{"ok":false,"error_code":"TARGET_DECODER_MISSING"}"#);
    assert_eq!(sh.send_attempts(), 1);
}

#[test]
fn push_chunk_timeout() {
    let mut caps = ShellCaps::busybox_base64();
    caps.drop_prompt_on = Some("-1");
    let (r, sh) = run_push(caps, &data_n(1300), |_| {});
    assert_eq!(j(r.unwrap()), r#"{"ok":false,"error_code":"TRANSFER_TIMEOUT","chunks_sent":1,"chunks_total":3}"#);
    assert_eq!(sh.send_attempts(), 4, "probe＋2 chunk＋cleanup");
    assert_eq!(sh.cmd_ids().last().unwrap(), "ft-bbbbbbbb-cleanup");
}

#[test]
fn push_prompt_timeout_and_cleanup_rejection_match_python_response_keys() {
    // Python push_file 在 chunk 已送出但 prompt 逾時時回傳進度（origin/main:sw_core/file_transfer.py:260-267）。
    let mut timeout_caps = ShellCaps::busybox_base64();
    timeout_caps.drop_prompt_on = Some("-0");
    let (timeout, _) = run_push(timeout_caps, &data_n(100), |_| {});
    assert_eq!(
        j(timeout.unwrap()),
        r#"{"ok":false,"error_code":"TRANSFER_TIMEOUT","chunks_sent":0,"chunks_total":1}"#
    );

    // Python 的 cleanup TxIncompleteError 在 _file_tx_rejected_result 前未附 chunk 進度：
    // cleanup 呼叫位於 chunk send 的 TxIncompleteError handler 之外（file_transfer.py:252-267、
    // session_manager.py:8216-8238）。Rust cleanup 被拒不得沿用被清理 chunk 的進度。
    let mut cleanup_caps = ShellCaps::busybox_base64();
    cleanup_caps.drop_prompt_on = Some("-0");
    cleanup_caps.reject_on_send_n = Some((3, rej(RejectReason::Other, 0)));
    let (cleanup, sh) = run_push(cleanup_caps, &data_n(100), |_| {});
    let Err(TransferAbort::TxRejected { rej: got, chunks, .. }) = cleanup else {
        panic!("cleanup TX 被拒時應上拋 TxRejected")
    };
    assert_eq!(got, rej(RejectReason::Other, 0));
    assert_eq!(sh.send_attempts(), 3, "decoder probe、chunk、被拒的 cleanup");
    let response = sw_core::op_runner::file_error_with_progress("TRANSFER_TIMEOUT", chunks);
    assert_eq!(j(response), r#"{"ok":false,"error_code":"TRANSFER_TIMEOUT"}"#);
}

#[test]
fn push_decoder_failed() {
    let mut caps = ShellCaps::busybox_base64();
    caps.decoder_fails_chunk = Some(0);
    let (r, sh) = run_push(caps, &data_n(1300), |_| {});
    assert_eq!(j(r.unwrap()), r#"{"ok":false,"error_code":"TARGET_DECODER_FAILED","chunks_sent":0,"chunks_total":3}"#);
    assert_eq!(sh.send_attempts(), 3, "probe＋1＋cleanup");
}

#[test]
fn push_md5_unavailable() {
    let mut caps = ShellCaps::busybox_base64();
    caps.md5sum = false;
    let (r, sh) = run_push(caps, &data_n(100), |_| {});
    assert_eq!(j(r.unwrap()), r#"{"ok":false,"error_code":"CHECKSUM_VERIFY_FAILED"}"#);
    assert_eq!(sh.cmd_ids().last().unwrap(), "ft-bbbbbbbb-cleanup");
    assert_eq!(sh.send_attempts(), 4);
}

#[test]
fn push_md5_mismatch() {
    let mut caps = ShellCaps::busybox_base64();
    caps.corrupt_md5 = true;
    let data = data_n(100);
    let want = md5_hex(&data);
    let first = if want.starts_with('0') { "1" } else { "0" };
    let actual = format!("{first}{}", &want[1..]);
    let (r, sh) = run_push(caps, &data, |_| {});
    assert_eq!(
        j(r.unwrap()),
        format!(r#"{{"ok":false,"error_code":"CHECKSUM_MISMATCH","expected":"{want}","actual":"{actual}"}}"#)
    );
    assert_eq!(sh.cmd_ids().last().unwrap(), "ft-bbbbbbbb-cleanup");
}

#[test]
fn push_move_timeout_no_cleanup() {
    let mut caps = ShellCaps::busybox_base64();
    caps.drop_prompt_on = Some("-mv");
    let (r, sh) = run_push(caps, &data_n(100), |_| {});
    assert_eq!(j(r.unwrap()), r#"{"ok":false,"error_code":"MOVE_TIMEOUT"}"#);
    assert_eq!(sh.cmd_ids().last().unwrap(), "ft-bbbbbbbb-mv");
}

#[test]
fn push_move_failed_no_cleanup() {
    let mut caps = ShellCaps::busybox_base64();
    caps.mv_fails = true;
    let (r, sh) = run_push(caps, &data_n(100), |_| {});
    assert_eq!(j(r.unwrap()), r#"{"ok":false,"error_code":"MOVE_FAILED"}"#);
    assert_eq!(sh.cmd_ids().last().unwrap(), "ft-bbbbbbbb-mv");
}

#[test]
fn push_empty_file_one_chunk() {
    let (r, sh) = run_push(ShellCaps::busybox_base64(), b"", |_| {});
    assert_eq!(
        j(r.unwrap()),
        r#"{"ok":true,"bytes":0,"chunks":1,"md5":"d41d8cd98f00b204e9800998ecf8427e","remote_path":"/tmp/dst.bin"}"#
    );
    assert_eq!(sh.send_attempts(), 4, "probe＋1＋md5＋mv");
    assert_eq!(sh.file("/tmp/dst.bin").unwrap(), b"");
}

#[test]
fn push_ack_none_uses_plain_send() {
    let (r, sh) = run_push(ShellCaps::busybox_base64(), &data_n(100), |p| p.ack_mode = "none".into());
    assert!(j(r.unwrap()).starts_with(r#"{"ok":true,"#));
    assert!(sh.paced_cmd_ids().is_empty());
}

#[test]
fn push_bad_prompt_regex() {
    let (r, sh) = run_push(ShellCaps::busybox_base64(), b"x", |p| p.prompt_regex = "(".into());
    assert!(matches!(r, Err(TransferAbort::BadPromptRegex(_))), "{r:?}");
    assert_eq!(sh.send_attempts(), 1);
}

// ── 錯誤形狀族（pull）──

#[test]
fn pull_probe_timeout() {
    let mut caps = ShellCaps::busybox_base64();
    caps.drop_prompt_on = Some("-probe");
    let (r, sh, _) = run_pull(caps, Some(b"x"), |_| {});
    assert_eq!(j(r.unwrap()), r#"{"ok":false,"error_code":"TRANSFER_TIMEOUT"}"#);
    assert_eq!(sh.send_attempts(), 1);
}

#[test]
fn pull_probe_shell_error() {
    let mut caps = ShellCaps::busybox_base64();
    caps.probe_shell_error = true;
    let (r, sh, _) = run_pull(caps, Some(b"x"), |_| {});
    assert_eq!(j(r.unwrap()), r#"{"ok":false,"error_code":"TRANSFER_PROBE_FAILED"}"#);
    assert_eq!(sh.send_attempts(), 1);
}

#[test]
fn pull_encoder_missing() {
    let (r, sh, _) = run_pull(ShellCaps::no_tools(), Some(b"x"), |_| {});
    assert_eq!(j(r.unwrap()), r#"{"ok":false,"error_code":"TARGET_ENCODER_MISSING"}"#);
    assert_eq!(sh.send_attempts(), 1);
}

#[test]
fn pull_timeout() {
    let mut caps = ShellCaps::busybox_base64();
    caps.drop_prompt_on = Some("-b64");
    let (r, sh, _) = run_pull(caps, Some(b"x"), |p| p.timeout = Duration::from_secs(1));
    assert_eq!(j(r.unwrap()), r#"{"ok":false,"error_code":"TRANSFER_TIMEOUT"}"#);
    assert_eq!(sh.send_attempts(), 2);
}

#[test]
fn pull_timeout_zero_checks_nothing() {
    let (r, sh, _) = run_pull(ShellCaps::busybox_base64(), Some(b"x"), |p| p.timeout = Duration::ZERO);
    assert_eq!(j(r.unwrap()), r#"{"ok":false,"error_code":"TRANSFER_TIMEOUT"}"#, "probe 的 wait 在 timeout=0 即失敗");
    assert_eq!(sh.send_attempts(), 1);
}

#[test]
fn pull_encoder_failed() {
    let (r, sh, _) = run_pull(ShellCaps::busybox_base64(), None, |_| {});
    assert_eq!(j(r.unwrap()), r#"{"ok":false,"error_code":"TARGET_ENCODER_FAILED"}"#);
    assert_eq!(sh.send_attempts(), 2);
}

#[test]
fn pull_parse_failed() {
    let mut caps = ShellCaps::busybox_base64();
    caps.drop_end_marker = true;
    let (r, sh, _) = run_pull(caps, Some(b"abc"), |_| {});
    assert_eq!(j(r.unwrap()), r#"{"ok":false,"error_code":"PULL_PARSE_FAILED"}"#);
    assert_eq!(sh.send_attempts(), 2);
}

#[test]
fn pull_b64_non_ascii() {
    let mut caps = ShellCaps::busybox_base64();
    caps.pull_payload = Some("YWJjé".into());
    let (r, sh, _) = run_pull(caps, Some(b"abc"), |_| {});
    assert_eq!(j(r.unwrap()), r#"{"ok":false,"error_code":"BASE64_DECODE_FAILED"}"#);
    assert_eq!(sh.send_attempts(), 2);
}

#[test]
fn pull_b64_bad_padding() {
    let mut caps = ShellCaps::busybox_base64();
    caps.pull_payload = Some("abc".into());
    let (r, sh, _) = run_pull(caps, Some(b"abc"), |_| {});
    assert_eq!(j(r.unwrap()), r#"{"ok":false,"error_code":"BASE64_DECODE_FAILED"}"#);
    assert_eq!(sh.send_attempts(), 2);
}

#[test]
fn pull_b64_noise_tolerated() {
    let data = data_n(60);
    let mut caps = ShellCaps::busybox_base64();
    let good = t::b64encode(&data);
    caps.pull_payload = Some(format!("{}*{}", &good[..8], &good[8..]));
    let (r, sh, files) = run_pull(caps, Some(&data), |_| {});
    assert!(j(r.unwrap()).starts_with(r#"{"ok":true,"bytes":60,"#));
    assert_eq!(files.get("blob.bin").unwrap(), data);
    assert_eq!(sh.send_attempts(), 3);
}

#[test]
fn pull_md5_mismatch() {
    let mut caps = ShellCaps::busybox_base64();
    caps.corrupt_md5 = true;
    let data = data_n(60);
    let local = md5_hex(&data);
    let first = if local.starts_with('0') { "1" } else { "0" };
    let remote = format!("{first}{}", &local[1..]);
    let (r, sh, files) = run_pull(caps, Some(&data), |_| {});
    assert_eq!(
        j(r.unwrap()),
        format!(r#"{{"ok":false,"error_code":"CHECKSUM_MISMATCH","expected":"{remote}","actual":"{local}"}}"#)
    );
    assert_eq!(sh.send_attempts(), 3);
    assert!(files.get("blob.bin").is_none());
}

#[test]
fn pull_md5_unavailable() {
    let mut caps = ShellCaps::busybox_base64();
    caps.md5sum = false;
    let (r, sh, files) = run_pull(caps, Some(&data_n(60)), |_| {});
    assert_eq!(j(r.unwrap()), r#"{"ok":false,"error_code":"CHECKSUM_VERIFY_FAILED"}"#);
    assert_eq!(sh.send_attempts(), 3);
    assert!(files.get("blob.bin").is_none(), "不寫本地檔");
}

#[test]
fn pull_local_write_error() {
    let files = MemFiles::empty();
    files.fail_write_on("blob.bin");
    let mut sh = FakeShell::new(ShellCaps::busybox_base64());
    sh.put_file("/etc/blob.bin", b"hello");
    let r = pull_file(&mut sh, &files, &mut FixedIds::new(&["eeeeeeee"]), &pull_params("/etc/blob.bin"));
    assert_eq!(r, Err(TransferAbort::LocalIo("[Errno 13] Permission denied: 'blob.bin'".into())));
    assert_eq!(sh.send_attempts(), 3);
}

#[test]
fn pull_budget_pull_cmd() {
    let remote = format!("/etc/{}", "a".repeat(300));
    let probe = t::encoder_probe_command().len();
    let md5 = t::md5_command(&remote).len();
    let pulls = t::ENCODERS.iter().map(|e| t::pull_command(&remote, e).unwrap().len()).min().unwrap();
    let budget = probe.max(md5);
    assert!(budget < pulls, "測試前提：budget 介於 probe/md5 與 pull 命令之間");
    let files = MemFiles::empty();
    let mut sh = FakeShell::new(ShellCaps::busybox_base64());
    let mut p = pull_params(&remote);
    p.max_console_line_chars = Some(budget as i128);
    let r = pull_file(&mut sh, &files, &mut FixedIds::new(&["eeeeeeee"]), &p).unwrap();
    assert_eq!(
        j(r),
        format!(r#"{{"ok":false,"error_code":"CONSOLE_LINE_LIMIT_TOO_SMALL","max_console_line_chars":{budget}}}"#)
    );
    assert_eq!(sh.send_attempts(), 0);
}

#[test]
fn pull_windows_basename() {
    let files = MemFiles::empty();
    let mut sh = FakeShell::new(ShellCaps::busybox_base64());
    sh.put_file("/a/b\\c.bin", b"hello");
    let mut p = pull_params("/a/b\\c.bin");
    p.path_flavor = PathFlavor::Windows;
    let r = pull_file(&mut sh, &files, &mut FixedIds::new(&["eeeeeeee"]), &p).unwrap();
    assert!(j(r).ends_with(r#""local_path":"c.bin"}"#));
    assert_eq!(sh.send_attempts(), 3);
}

#[test]
fn pull_explicit_local_path_wins() {
    let (r, _, files) = run_pull(ShellCaps::busybox_base64(), Some(b"hello"), |p| p.local_path = Some("out/x.bin".into()));
    assert!(j(r.unwrap()).ends_with(r#""local_path":"out/x.bin"}"#));
    assert_eq!(files.get("out/x.bin").unwrap(), b"hello");
}

// ── OD-80：分段 pull 不依賴 131072 字元 RX 視窗 ──

fn windowed_caps(release: usize) -> ShellCaps {
    let mut caps = ShellCaps::busybox_base64();
    caps.rx_max_chars = Some(131_072);
    caps.release_per_wait = Some(release);
    caps.release_only_suffix = Some("-b64");
    caps
}

#[test]
fn pull_over_rx_window_succeeds_od80() {
    // 192 KiB 原始資料 → base64 約 262144 字元 + 換行，遠超過 131072 字元視窗。
    let data = data_n(192 * 1024);
    let (r, sh, files) = run_pull(windowed_caps(8192), Some(&data), |p| p.timeout = Duration::from_secs(120));
    assert_eq!(
        j(r.unwrap()),
        format!(r#"{{"ok":true,"bytes":{},"md5":"{}","local_path":"blob.bin"}}"#, data.len(), md5_hex(&data))
    );
    assert_eq!(files.get("blob.bin").unwrap(), data);
    assert_eq!(sh.cmd_ids(), ["ft-eeeeeeee-probe", "ft-eeeeeeee-b64", "ft-eeeeeeee-md5"]);
    assert!(sh.wait_calls > 20, "必須分多輪輪詢，不是單次等待");
}

#[test]
fn pull_one_mebibyte_succeeds_od80() {
    let data = data_n(1024 * 1024);
    let (r, _, files) = run_pull(windowed_caps(8192), Some(&data), |p| p.timeout = Duration::from_secs(600));
    assert!(j(r.unwrap()).starts_with(r#"{"ok":true,"bytes":1048576,"#));
    assert_eq!(files.get("blob.bin").unwrap(), data);
}

#[test]
fn pull_char_by_char_arrival_keeps_partial_lines_and_prompt() {
    let data = data_n(100);
    let (r, _, files) = run_pull(windowed_caps(1), Some(&data), |p| p.timeout = Duration::from_secs(3600));
    assert!(j(r.unwrap()).starts_with(r#"{"ok":true,"bytes":100,"#));
    assert_eq!(files.get("blob.bin").unwrap(), data);
}

#[test]
fn pull_when_poll_slower_than_window_fills_is_parse_failed() {
    // 單次釋出 > 視窗：開頭 BEGIN 在引擎讀到之前就被修剪，無法復原；必為失敗而非靜默成功。
    let data = data_n(192 * 1024);
    let (r, _, files) = run_pull(windowed_caps(400_000), Some(&data), |p| p.timeout = Duration::from_secs(120));
    assert_eq!(j(r.unwrap()), r#"{"ok":false,"error_code":"PULL_PARSE_FAILED"}"#);
    assert!(files.get("blob.bin").is_none());
}

#[test]
fn pull_timeout_counts_poll_slices() {
    // 每輪最多等 250 ms：1 秒預算至多 4 輪；釋出速度太慢 → TRANSFER_TIMEOUT。
    let (r, sh, _) = run_pull(windowed_caps(1), Some(&data_n(100)), |p| p.timeout = Duration::from_secs(1));
    assert_eq!(j(r.unwrap()), r#"{"ok":false,"error_code":"TRANSFER_TIMEOUT"}"#);
    assert!(sh.wait_calls <= 1 + 4, "probe 1 次＋輪詢至多 4 次，實際 {}", sh.wait_calls);
}

// ── OD-62：TX 被拒的證據 ──

#[test]
fn push_tx_rejected_quiet_first_chunk_gives_rejection_without_cleanup() {
    let mut caps = ShellCaps::busybox_base64();
    caps.reject_on_send_n = Some((2, rej(RejectReason::Quiet, 0)));
    let (r, sh) = run_push(caps, &data_n(1300), |_| {});
    let Err(TransferAbort::TxRejected { rej: got, chunks, transfer_cmd_id }) = r else { panic!("{r:?}") };
    assert_eq!(got, rej(RejectReason::Quiet, 0));
    assert_eq!(chunks, Some((0, 3)));
    assert_eq!(transfer_cmd_id, "ft-bbbbbbbb");
    assert_eq!(sh.send_attempts(), 2, "被拒後不得再送任何命令（含 cancel／cleanup）");
    assert_eq!(sh.raw_tx_count(b"\x15\n"), 0);
}

#[test]
fn push_tx_rejected_in_chunk_loop_carries_progress() {
    let mut caps = ShellCaps::busybox_base64();
    caps.reject_on_send_n = Some((3, rej(RejectReason::Draining, 17)));
    let (r, sh) = run_push(caps, &data_n(1300), |_| {});
    let Err(TransferAbort::TxRejected { rej: got, chunks, transfer_cmd_id }) = r else { panic!("{r:?}") };
    assert_eq!(got, rej(RejectReason::Draining, 17));
    assert_eq!(chunks, Some((1, 3)));
    assert_eq!(transfer_cmd_id, "ft-bbbbbbbb");
    assert_eq!(sh.send_attempts(), 3);
}

#[test]
fn push_tx_rejected_in_probe_has_no_progress() {
    let mut caps = ShellCaps::busybox_base64();
    caps.reject_on_send_n = Some((1, rej(RejectReason::StaleGeneration, 0)));
    let (r, sh) = run_push(caps, &data_n(100), |_| {});
    let Err(TransferAbort::TxRejected { chunks, transfer_cmd_id, .. }) = r else { panic!("{r:?}") };
    assert_eq!((chunks, transfer_cmd_id.as_str()), (None, "ft-bbbbbbbb"));
    assert_eq!(sh.send_attempts(), 1);
}

#[test]
fn push_tx_rejected_at_md5_and_mv_have_no_progress() {
    for n in [3usize, 4] {
        // 單 chunk：1=probe、2=chunk0、3=md5、4=mv。
        let mut caps = ShellCaps::busybox_base64();
        caps.reject_on_send_n = Some((n, rej(RejectReason::Other, 0)));
        let (r, sh) = run_push(caps, &data_n(100), |_| {});
        let Err(TransferAbort::TxRejected { chunks, .. }) = r else { panic!("{r:?}") };
        assert_eq!(chunks, None, "n={n}");
        assert_eq!(sh.send_attempts(), n);
    }
}

#[test]
fn push_rejection_on_cancel_during_echo_stall_propagates() {
    let mut caps = ShellCaps::busybox_base64();
    caps.echo_stall_after_chars = Some(100);
    caps.reject_on_send_n = Some((3, rej(RejectReason::Cancelled, 0)));
    let (r, sh) = run_push(caps, &[7u8; 600], |_| {});
    let Err(TransferAbort::TxRejected { chunks, .. }) = r else { panic!("{r:?}") };
    assert_eq!(chunks, Some((0, 2)));
    assert_eq!(sh.send_attempts(), 3, "^U 被拒後不再送 cleanup");
}

#[test]
fn pull_tx_rejected_has_no_progress() {
    let mut caps = ShellCaps::busybox_base64();
    caps.reject_on_send_n = Some((2, rej(RejectReason::Quiet, 0)));
    let (r, sh, files) = run_pull(caps, Some(b"hello"), |_| {});
    let Err(TransferAbort::TxRejected { rej: got, chunks, transfer_cmd_id }) = r else { panic!("{r:?}") };
    assert_eq!(got, rej(RejectReason::Quiet, 0));
    assert_eq!((chunks, transfer_cmd_id.as_str()), (None, "ft-eeeeeeee"));
    assert_eq!(sh.send_attempts(), 2);
    assert!(files.get("blob.bin").is_none());
}

#[test]
fn pull_tx_rejected_at_probe_and_md5_have_no_progress() {
    for n in [1usize, 3] {
        let mut caps = ShellCaps::busybox_base64();
        caps.reject_on_send_n = Some((n, rej(RejectReason::Other, 0)));
        let (r, sh, _) = run_pull(caps, Some(b"hello"), |_| {});
        let Err(TransferAbort::TxRejected { chunks, transfer_cmd_id, .. }) = r else { panic!("{r:?}") };
        assert_eq!((chunks, transfer_cmd_id.as_str()), (None, "ft-eeeeeeee"), "n={n}");
        assert_eq!(sh.send_attempts(), n);
    }
}

#[test]
fn id_order_is_tmp_then_prefix() {
    // FixedIds 會 assert 長度：先 hex(12) 後 hex(8)，順序顛倒即 panic。
    let (r, sh) = run_push(ShellCaps::busybox_base64(), b"x", |_| {});
    assert!(r.is_ok());
    assert!(sh.commands().iter().any(|c| c.contains("/tmp/.sw_upload_0123456789ab")));
    assert!(sh.cmd_ids()[0].starts_with("ft-bbbbbbbb-"));
}
