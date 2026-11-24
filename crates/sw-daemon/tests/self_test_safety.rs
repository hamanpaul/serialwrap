#![cfg(feature = "test-fakes")]

mod support;

use std::sync::Arc;
use std::thread;
use std::time::{Duration, Instant};

use support::*;
use sw_pal::fakes::serial::{FakeDevice, FakeWriteStep};
use sw_proto::pyjson::{PyDict, PyValue};

fn wait_for(what: &str, mut cond: impl FnMut() -> bool) {
    let deadline = Instant::now() + Duration::from_secs(5);
    while !cond() {
        assert!(Instant::now() < deadline, "等待逾時：{what}");
        thread::sleep(Duration::from_millis(2));
    }
}

struct TempTransferDir(std::path::PathBuf);

impl TempTransferDir {
    fn new() -> Self {
        let root = std::env::temp_dir().join(format!(
            "sw-i306-rpc-{}-{:?}",
            std::process::id(),
            thread::current().id()
        ));
        let _ = std::fs::remove_dir_all(&root);
        std::fs::create_dir_all(&root).unwrap();
        Self(root)
    }

    fn path(&self, name: &str) -> String {
        self.0.join(name).to_string_lossy().into_owned()
    }
}

impl Drop for TempTransferDir {
    fn drop(&mut self) {
        let _ = std::fs::remove_dir_all(&self.0);
    }
}

fn tx_records(rig: &ServiceRig) -> usize {
    let bytes = rig.wfs.contents(&rig.wal.wal_path()).unwrap_or_default();
    String::from_utf8_lossy(&bytes)
        .lines()
        .filter_map(|line| sw_proto::pyjson::loads(line).ok())
        .filter(|row| match row {
            PyValue::Dict(row) => row.get("dir").and_then(PyValue::as_str) == Some("TX"),
            _ => false,
        })
        .count()
}

#[derive(Debug)]
struct TxSnapshot {
    tx_len: usize,
    write_calls: usize,
    os_submits: usize,
    wal_tx: usize,
    last_probe_at: PyValue,
}

fn tx_snapshot(rig: &ServiceRig, dev: &FakeDevice) -> TxSnapshot {
    TxSnapshot {
        tx_len: dev.tx_len(),
        write_calls: dev.write_calls(),
        os_submits: dev.os_submits(),
        wal_tx: tx_records(rig),
        last_probe_at: rig.session("COM0").unwrap()["last_probe_at"].clone(),
    }
}

fn rx_window_bytes(rig: &ServiceRig) -> u64 {
    rig.session("COM0")
        .expect("COM0 session")
        .get("rx_bytes_last_10s")
        .and_then(PyValue::as_i64)
        .and_then(|bytes| u64::try_from(bytes).ok())
        .expect("READY session 應回報 RX bytes")
}

fn push_rpc_rx_and_wait(rig: &ServiceRig, dev: &FakeDevice, bytes: &[u8], before: u64, what: &str) {
    let expected = before + bytes.len() as u64;
    dev.push_rx(bytes);
    wait_for(what, || rx_window_bytes(rig) >= expected);
}

fn ready_reconfirm_pending_rig() -> ServiceRig {
    let rig = ServiceRig::build(RigSpec {
        yaml: "profiles:\n  t:\n    platform: shell\n    prompt_regex: '(?m)^root@t:.*# '\n    ready_probe: 'echo __READY__${nonce}'\n    boot_done_regex: 'BOOT_DONE'\n    timeout_s: 2.0\ntargets:\n  - {com: COM0, profile: t, device_by_id: /s/by-id/usb-A}\n".to_string(),
        devices: vec![(BY_ID_A.into(), REAL_A.into())],
        explicit_target_count: 1,
        allowlist: None,
        wait_ready: vec!["COM0".into()],
    });
    let dev = rig.device(REAL_A);
    dev.push_rx(b"\r\nU-Boot 2024.01 (Jan 01)\r\n");
    wait_for("boot banner 建立 reconfirm pending", || {
        rig.session("COM0").is_some_and(|session| {
            session["ready_reconfirm_pending"] == PyValue::Bool(true)
                && !session["boot_quiet_remaining_s"].is_null()
        })
    });
    dev.push_rx(b"\r\nBOOT_DONE\r\n");
    wait_for(
        "boot_done_regex 清 quiet 並保留 reconfirm pending",
        || {
            rig.session("COM0").is_some_and(|session| {
                session["ready_reconfirm_pending"] == PyValue::Bool(true)
                    && session["boot_quiet_remaining_s"].is_null()
            })
        },
    );
    rig
}

fn ready_newline_prompt_rig() -> ServiceRig {
    let rig = ServiceRig::build(RigSpec {
        yaml: "profiles:\n  t:\n    platform: shell\n    prompt_regex: '\\r?\\nroot@t:/# '\n    ready_probe: 'echo __READY__${nonce}'\n    timeout_s: 5.0\ntargets:\n  - {com: COM0, profile: t, device_by_id: /s/by-id/usb-A}\n".to_string(),
        devices: vec![(BY_ID_A.into(), REAL_A.into())],
        explicit_target_count: 1,
        allowlist: None,
        wait_ready: Vec::new(),
    });
    rig.device(REAL_A).push_rx(b"\r\nroot@t:/# ");
    rig.wait_state("COM0", "READY");
    rig
}

fn assert_busy(reply: &PyDict) {
    assert_eq!(reply["ok"], PyValue::Bool(true), "{reply:?}");
    assert!(
        matches!(
            reply.get("classification").and_then(PyValue::as_str),
            Some("SESSION_BUSY" | "SESSION_INTERACTIVE_BUSY")
        ),
        "忙碌 RPC 分類：{reply:?}"
    );
    assert_eq!(reply["recommended_action"], PyValue::from("wait"));
    assert!(
        reply.get("probe_ok").is_none(),
        "busy 不應宣稱探測結果：{reply:?}"
    );
    assert!(
        reply.get("probe_evidence").is_none(),
        "busy 不應捏造 probe_evidence：{reply:?}"
    );
}

fn assert_no_probe_delta(rig: &ServiceRig, dev: &FakeDevice, before: &TxSnapshot, scenario: &str) {
    assert_eq!(
        dev.tx_len(),
        before.tx_len,
        "{scenario}：busy RPC 不得新增 UART bytes"
    );
    assert_eq!(
        dev.write_calls(),
        before.write_calls,
        "{scenario}：busy RPC 不得新增 writer submit"
    );
    assert_eq!(
        dev.os_submits(),
        before.os_submits,
        "{scenario}：busy RPC 不得新增 OS submit"
    );
    assert_eq!(
        tx_records(rig),
        before.wal_tx,
        "{scenario}：busy RPC 不得新增 WAL TX"
    );
    assert_eq!(
        rig.session("COM0").unwrap()["last_probe_at"],
        before.last_probe_at,
        "{scenario}：busy RPC 不得更新 last_probe_at"
    );
}

fn advance_clock_until(rig: &ServiceRig, handle: &thread::JoinHandle<PyDict>, scenario: &str) {
    for _ in 0..80 {
        if handle.is_finished() {
            return;
        }
        rig.clock.advance(Duration::from_secs(1));
        rig.mgr.on_tick();
        thread::sleep(Duration::from_millis(2));
    }
    assert!(
        handle.is_finished(),
        "{scenario}：FakeClock 前進後背景 RPC 應完成"
    );
}

fn echo_only_target(dev: &FakeDevice) {
    dev.clear_replies();
    dev.on_tx_match(
        r"__READY__[0-9a-f]+",
        Box::new(|line: &[u8]| {
            let text = String::from_utf8_lossy(line);
            let nonce = text
                .split("__READY__")
                .nth(1)
                .unwrap_or("")
                .chars()
                .take_while(char::is_ascii_hexdigit)
                .collect::<String>();
            format!("__READY__{nonce}\r\nroot@t:/# ").into_bytes()
        }),
    );
}

#[test]
fn rpc_probe_evidence_preserves_each_boolean() {
    let rig = ServiceRig::ready_shell();
    let dev = rig.device(REAL_A);
    let tx_start = dev.tx_len();
    let reply = rig.call("session.self_test", r#"{"selector":"COM0"}"#);
    assert_eq!(reply["probe_ok"], PyValue::Bool(true), "{reply:?}");
    let evidence = match &reply["probe_evidence"] {
        PyValue::Dict(evidence) => evidence,
        other => panic!("RPC 必須保留 probe_evidence object：{other:?}"),
    };
    for name in [
        "tx_complete",
        "execution_marker",
        "probe_rc_zero",
        "prompt_after_marker",
    ] {
        assert_eq!(evidence.get(name), Some(&PyValue::Bool(true)), "{reply:?}");
    }
    let tx = String::from_utf8_lossy(&dev.tx_since(tx_start)).into_owned();
    assert!(
        tx.contains("( echo __READY__"),
        "UART 應收到完整 probe 框架：{tx:?}"
    );
    let nonce = tx
        .split("__READY__")
        .nth(1)
        .unwrap()
        .chars()
        .take_while(char::is_ascii_hexdigit)
        .collect::<String>();
    assert_eq!(nonce.len(), 8);
    assert!(
        !tx.contains(&format!("__SW_ST_{nonce}_OK__")),
        "預期執行標記不得出現在 UART TX：{tx:?}"
    );
    println!("UART_TX={tx:?}");
}

#[test]
fn rpc_echo_only_target_remains_unconfirmed() {
    let rig = Arc::new(ServiceRig::ready_shell());
    let dev = rig.device(REAL_A);
    echo_only_target(dev);
    let tx_start = dev.tx_len();
    let rpc_rig = Arc::clone(&rig);
    let handle = thread::spawn(move || {
        rpc_rig.call(
            "session.self_test",
            r#"{"selector":"COM0","timeout_s":0.1}"#,
        )
    });
    wait_for("echo-only probe TX", || dev.tx_len() > tx_start);
    advance_clock_until(&rig, &handle, "echo-only self-test");
    let reply = handle.join().unwrap();
    assert_eq!(reply["probe_ok"], PyValue::Bool(false), "{reply:?}");
    let evidence = match &reply["probe_evidence"] {
        PyValue::Dict(evidence) => evidence,
        other => panic!("probe_evidence object：{other:?}"),
    };
    assert_eq!(evidence["tx_complete"], PyValue::Bool(true));
    assert_eq!(evidence["execution_marker"], PyValue::Bool(false));
    assert_eq!(evidence["probe_rc_zero"], PyValue::Bool(false));
    assert_eq!(evidence["prompt_after_marker"], PyValue::Bool(false));
    assert_eq!(
        reply["classification"],
        PyValue::from("TARGET_UNRESPONSIVE")
    );
}

#[test]
fn rpc_probe_evidence_reports_failure_scenarios_and_pending_state() {
    for scenario in [0, 1, 2, 3] {
        let rig = Arc::new(ready_reconfirm_pending_rig());
        let dev = rig.device(REAL_A);
        dev.clear_replies();
        let pending_before = rig.session("COM0").unwrap()["ready_reconfirm_pending"].clone();
        assert_eq!(
            pending_before,
            PyValue::Bool(true),
            "驗收前置必須是 READY reconfirm pending，而不是只記錄 READY 初值"
        );
        let tx_start = dev.tx_len();
        let rpc_rig = Arc::clone(&rig);
        let handle = thread::spawn(move || {
            rpc_rig.call(
                "session.self_test",
                r#"{"selector":"COM0","timeout_s":0.5}"#,
            )
        });
        wait_for("RPC probe TX", || dev.tx_len() > tx_start);
        let tx = dev.tx_since(tx_start);
        let tx_text = String::from_utf8_lossy(&tx).into_owned();
        let nonce = tx_text
            .split("__READY__")
            .nth(1)
            .unwrap_or("")
            .chars()
            .take_while(char::is_ascii_hexdigit)
            .collect::<String>();
        assert_eq!(nonce.len(), 8, "probe TX 應含完整 nonce：{tx_text:?}");
        let expected_tx =
            format!("( echo __READY__{nonce} ) && printf '\\n__SW_ST_%s_OK__\\n' '{nonce}'\n");
        assert_eq!(tx_text, expected_tx, "測試 DUT 只依完整 shell 框架回覆");
        assert!(!tx_text.contains(&format!("__SW_ST_{nonce}_OK__")));

        let response = match scenario {
            // 完整實際 TX 回顯不能把 printf 格式字串當執行標記。
            0 => tx.clone(),
            // marker 有完整行界，但 marker 後沒有 prompt。
            1 => format!("__SW_ST_{nonce}_OK__\r\n").into_bytes(),
            // nonce 回顯後，執行標記被其他輸出插斷，並帶有 prompt。
            2 => format!("__READY__{nonce}\r\n__SW_ST_{nonce}_\r\nnoise_OK__\r\nroot@t:/# ")
                .into_bytes(),
            // marker 前後雜訊不影響完整 marker 與後續 prompt。
            _ => format!("noise before\r\n__SW_ST_{nonce}_OK__\r\nnoise after\r\nroot@t:/# ")
                .into_bytes(),
        };
        let rx_before = rx_window_bytes(&rig);
        push_rpc_rx_and_wait(
            &rig,
            dev,
            &response,
            rx_before,
            "RPC reader 已把完整情境回應寫入 RX snapshot",
        );

        let expected = match scenario {
            0 | 2 => [true, false, false, false],
            1 => [true, true, true, false],
            _ => [true, true, true, true],
        };
        if expected.iter().all(|value| *value) {
            wait_for("完整 marker 與後續 prompt 完成 RPC", || {
                handle.is_finished()
            });
        } else {
            advance_clock_until(&rig, &handle, "RPC self-test evidence scenario");
        }
        let reply = handle.join().unwrap();
        assert_eq!(
            reply["probe_ok"],
            PyValue::Bool(expected.iter().all(|value| *value)),
            "scenario={scenario}: {reply:?}"
        );
        let evidence = match &reply["probe_evidence"] {
            PyValue::Dict(evidence) => evidence,
            other => panic!("probe_evidence object：{other:?}"),
        };
        for (name, expected_value) in [
            "tx_complete",
            "execution_marker",
            "probe_rc_zero",
            "prompt_after_marker",
        ]
        .into_iter()
        .zip(expected)
        {
            assert_eq!(
                evidence.get(name),
                Some(&PyValue::Bool(expected_value)),
                "scenario={scenario}, field={name}: {reply:?}"
            );
        }
        let expected_pending = PyValue::Bool(!expected.iter().all(|value| *value));
        assert_eq!(
            rig.session("COM0").unwrap()["ready_reconfirm_pending"],
            expected_pending,
            "scenario={scenario}：失敗須保留 pending，完整成功才可清除"
        );
    }
}

#[test]
fn rpc_newline_prefixed_prompt_after_marker_confirms_ready() {
    let rig = Arc::new(ready_newline_prompt_rig());
    let dev = rig.device(REAL_A);
    dev.clear_replies();
    let rx_before = rx_window_bytes(&rig);
    let tx_start = dev.tx_len();
    let rpc_rig = Arc::clone(&rig);
    let handle = thread::spawn(move || {
        rpc_rig.call(
            "session.self_test",
            r#"{"selector":"COM0","timeout_s":0.5}"#,
        )
    });
    wait_for("自訂換行 prompt 的 RPC probe TX", || {
        dev.tx_len() > tx_start
    });
    let tx = dev.tx_since(tx_start);
    let tx_text = String::from_utf8_lossy(&tx);
    let nonce = tx_text
        .split("__READY__")
        .nth(1)
        .unwrap_or("")
        .chars()
        .take_while(char::is_ascii_hexdigit)
        .collect::<String>();
    assert_eq!(nonce.len(), 8, "probe TX 應含完整 nonce：{tx_text:?}");

    let response = format!("__SW_ST_{nonce}_OK__\r\nroot@t:/# ").into_bytes();
    push_rpc_rx_and_wait(
        &rig,
        dev,
        &response,
        rx_before,
        "RPC reader 已處理 marker 與 newline-prefixed prompt",
    );
    if !handle.is_finished() {
        advance_clock_until(&rig, &handle, "newline-prefixed RPC prompt");
    }
    let reply = handle.join().unwrap();
    assert_eq!(reply["probe_ok"], PyValue::Bool(true), "{reply:?}");
    assert_eq!(
        reply["probe_evidence"],
        PyValue::Dict(sw_proto::pydict! {
            "tx_complete" => true,
            "execution_marker" => true,
            "probe_rc_zero" => true,
            "prompt_after_marker" => true,
        })
    );
}

#[test]
fn rpc_self_test_busy_has_zero_probe_tx() {
    // 前景 line command。
    {
        let rig = ServiceRig::ready_shell();
        let dev = rig.device(REAL_A);
        dev.on_tx_reply(b"sleep 99", b"root@t:/# ");
        dev.script_write(vec![FakeWriteStep::PauseBeforeSubmit]);
        let sub = rig.call(
            "command.submit",
            r#"{"selector":"COM0","cmd":"sleep 99","timeout_s":30}"#,
        );
        let cmd_id = sub["cmd_id"].as_str().unwrap().to_string();
        wait_for("前景 command running", || {
            let got = rig.call("command.get", &format!(r#"{{"cmd_id":"{cmd_id}"}}"#));
            matches!(got.get("command"), Some(PyValue::Dict(row)) if row.get("status") == Some(&PyValue::from("running")))
        });
        assert!(
            dev.wait_paused(Duration::from_secs(2)),
            "line command 停在提交前"
        );
        let before = tx_snapshot(&rig, dev);
        let reply = rig.call("session.self_test", r#"{"selector":"COM0"}"#);
        assert_busy(&reply);
        assert_no_probe_delta(&rig, dev, &before, "foreground line");
        dev.release_pause();
        wait_for("line command timeout 收尾", || {
            let got = rig.call("command.get", &format!(r#"{{"cmd_id":"{cmd_id}"}}"#));
            matches!(got.get("command"), Some(PyValue::Dict(row)) if row.get("status") == Some(&PyValue::from("done")))
        });
    }

    // 背景命令已完成，但 result capture 仍為 active。
    {
        let rig = ServiceRig::ready_shell();
        let dev = rig.device(REAL_A);
        dev.on_tx_reply(b"sleep 30", b"root@t:/# ");
        let sub = rig.call(
            "command.submit",
            r#"{"selector":"COM0","cmd":"sleep 30","mode":"background","timeout_s":30}"#,
        );
        let cmd_id = sub["cmd_id"].as_str().unwrap().to_string();
        wait_for("background command 完成", || {
            let got = rig.call("command.get", &format!(r#"{{"cmd_id":"{cmd_id}"}}"#));
            matches!(got.get("command"), Some(PyValue::Dict(row)) if row.get("status") == Some(&PyValue::from("done")))
        });
        wait_for("background capture active", || {
            let tail = rig.call(
                "command.result_tail",
                &format!(r#"{{"cmd_id":"{cmd_id}","from_chunk":0,"limit":200}}"#),
            );
            tail.get("status") == Some(&PyValue::from("active"))
        });
        let before = tx_snapshot(&rig, dev);
        let reply = rig.call("session.self_test", r#"{"selector":"COM0"}"#);
        assert_busy(&reply);
        assert_no_probe_delta(&rig, dev, &before, "active background capture");
        rig.clock.advance(Duration::from_secs(90));
        rig.mgr.on_tick();
    }

    // Agent interactive lease。
    {
        let rig = ServiceRig::ready_shell();
        let dev = rig.device(REAL_A);
        let opened = rig.call(
            "session.interactive_open",
            r#"{"selector":"COM0","owner":"agent:i306"}"#,
        );
        let interactive_id = opened["interactive_id"].as_str().unwrap().to_string();
        let before = tx_snapshot(&rig, dev);
        let reply = rig.call("session.self_test", r#"{"selector":"COM0"}"#);
        assert_busy(&reply);
        assert_eq!(
            reply["classification"],
            PyValue::from("SESSION_INTERACTIVE_BUSY")
        );
        assert_no_probe_delta(&rig, dev, &before, "agent lease");
        rig.call(
            "session.interactive_close",
            &format!(r#"{{"interactive_id":"{interactive_id}"}}"#),
        );
    }

    // Recover 由 RPC 啟動並停在 Ctrl-C 後的 RX 等待。
    {
        let rig = Arc::new(ServiceRig::ready_shell());
        let dev = rig.device(REAL_A);
        let tx_start = dev.tx_len();
        let rpc_rig = Arc::clone(&rig);
        let handle = thread::spawn(move || {
            rpc_rig.call("session.recover", r#"{"selector":"COM0","timeout_s":30}"#)
        });
        wait_for("Recover 救援鍵 TX", || dev.tx_len() > tx_start);
        let before = tx_snapshot(&rig, dev);
        let reply = rig.call("session.self_test", r#"{"selector":"COM0"}"#);
        assert_busy(&reply);
        assert_no_probe_delta(&rig, dev, &before, "Recover");
        dev.push_rx(b"root@t:/# ");
        let _recover = handle.join().unwrap();
    }

    // 另一個 probe 已取得前景槽，停在第一次實際提交前。
    {
        let rig = Arc::new(ServiceRig::ready_shell());
        let dev = rig.device(REAL_A);
        let tx_start = dev.tx_len();
        dev.script_write(vec![FakeWriteStep::PauseBeforeSubmit]);
        let rpc_rig = Arc::clone(&rig);
        let handle =
            thread::spawn(move || rpc_rig.call("session.self_test", r#"{"selector":"COM0"}"#));
        assert!(
            dev.wait_paused(Duration::from_secs(2)),
            "第一個 probe 應停在提交前"
        );
        let before = tx_snapshot(&rig, dev);
        let reply = rig.call("session.self_test", r#"{"selector":"COM0"}"#);
        assert_busy(&reply);
        assert_no_probe_delta(&rig, dev, &before, "another probe");
        dev.release_pause();
        assert_eq!(handle.join().unwrap()["probe_ok"], PyValue::Bool(true));
        assert!(dev.tx_len() > tx_start);
    }

    // file.push／file.pull 各自先取得前景槽，再由假 writer 停在提交前。
    for push in [true, false] {
        let rig = Arc::new(ServiceRig::ready_shell());
        let dev = rig.device(REAL_A);
        dev.script_write(vec![FakeWriteStep::PauseBeforeSubmit]);
        let tmp = TempTransferDir::new();
        if push {
            std::fs::write(tmp.path("source.bin"), b"i306").unwrap();
        }
        let params = if push {
            format!(
                r#"{{"selector":"COM0","local_path":{},"remote_path":"/tmp/i306"}}"#,
                json_string(&tmp.path("source.bin"))
            )
        } else {
            format!(
                r#"{{"selector":"COM0","local_path":{},"remote_path":"/tmp/i306"}}"#,
                json_string(&tmp.path("destination.bin"))
            )
        };
        let rpc_rig = Arc::clone(&rig);
        let method = if push { "file.push" } else { "file.pull" };
        let handle = thread::spawn(move || rpc_rig.call(method, &params));
        if !dev.wait_paused(Duration::from_secs(2)) {
            let finished = handle.is_finished();
            let result = finished.then(|| handle.join().unwrap());
            panic!("{method} 未停在 UART 提交前；finished={finished} result={result:?}");
        }
        let before = tx_snapshot(&rig, dev);
        let reply = rig.call("session.self_test", r#"{"selector":"COM0"}"#);
        assert_busy(&reply);
        assert_no_probe_delta(&rig, dev, &before, method);
        dev.release_pause();
        advance_clock_until(&rig, &handle, method);
        let _transfer = handle.join().unwrap();
    }
}

#[test]
fn rpc_probe_and_submit_have_deterministic_order() {
    // Probe 先取得槽：command submit 只能排在整個 probe 後面。
    {
        let rig = Arc::new(ServiceRig::ready_shell());
        let dev = rig.device(REAL_A);
        dev.on_tx_reply(b"echo after", b"root@t:/# ");
        let tx_start = dev.tx_len();
        dev.script_write(vec![FakeWriteStep::PauseBeforeSubmit]);
        let rpc_rig = Arc::clone(&rig);
        let probe =
            thread::spawn(move || rpc_rig.call("session.self_test", r#"{"selector":"COM0"}"#));
        assert!(dev.wait_paused(Duration::from_secs(2)), "probe 停在提交前");
        let command = rig.call(
            "command.submit",
            r#"{"selector":"COM0","cmd":"echo after","timeout_s":10}"#,
        );
        let cmd_id = command["cmd_id"].as_str().unwrap().to_string();
        assert_eq!(
            dev.tx_len(),
            tx_start,
            "probe 尚未提交時 command 不得插入 UART"
        );
        dev.release_pause();
        assert_eq!(probe.join().unwrap()["probe_ok"], PyValue::Bool(true));
        wait_for("probe 後的 command 完成", || {
            let got = rig.call("command.get", &format!(r#"{{"cmd_id":"{cmd_id}"}}"#));
            matches!(got.get("command"), Some(PyValue::Dict(row)) if row.get("status") == Some(&PyValue::from("done")))
        });
        let tx = String::from_utf8_lossy(&dev.tx_since(tx_start)).into_owned();
        let command_pos = tx.find("echo after\n").expect("UART command bytes");
        let probe_end = tx.find("\n").expect("probe TX line") + 1;
        assert!(command_pos >= probe_end, "UART TX 不得穿插：{tx:?}");
    }

    // Command 先取得槽：self-test 被動回覆，command TX 不被打斷。
    {
        let rig = Arc::new(ServiceRig::ready_shell());
        let dev = rig.device(REAL_A);
        dev.on_tx_reply(b"echo first", b"root@t:/# ");
        let tx_start = dev.tx_len();
        dev.script_write(vec![FakeWriteStep::PauseBeforeSubmit]);
        let rpc_rig = Arc::clone(&rig);
        let command = thread::spawn(move || {
            rpc_rig.call(
                "command.submit",
                r#"{"selector":"COM0","cmd":"echo first","timeout_s":10}"#,
            )
        });
        assert!(
            dev.wait_paused(Duration::from_secs(2)),
            "command 停在提交前"
        );
        let before = tx_snapshot(&rig, dev);
        let busy = rig.call("session.self_test", r#"{"selector":"COM0"}"#);
        assert_busy(&busy);
        assert_no_probe_delta(&rig, dev, &before, "command first");
        dev.release_pause();
        let command = command.join().unwrap();
        let cmd_id = command["cmd_id"].as_str().unwrap().to_string();
        wait_for("先行 command 完成", || {
            let got = rig.call("command.get", &format!(r#"{{"cmd_id":"{cmd_id}"}}"#));
            matches!(got.get("command"), Some(PyValue::Dict(row)) if row.get("status") == Some(&PyValue::from("done")))
        });
        let tx = String::from_utf8_lossy(&dev.tx_since(tx_start)).into_owned();
        assert!(
            tx.contains("echo first\n"),
            "先行 UART command bytes：{tx:?}"
        );
        assert!(!tx.contains("__SW_ST_"), "busy probe 不得 TX：{tx:?}");
    }
}
