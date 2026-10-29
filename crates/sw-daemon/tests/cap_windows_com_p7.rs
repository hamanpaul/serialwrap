//! P7 Task 19（08-slice23-login-events.md）：原生 Windows COM handle 下的登入、quiet 與事件（B-06；OD-08、OD-11、OD-15）。
//!
//! - daemon 以 throwaway 隔離實例啟動（`support::isolated_env` 的獨立 state／run／wal 目錄），
//!   `SERIALWRAP_TEST_DEVICE_ALLOWLIST` 只列 COMa（OD-11）；peer 端 COMb 由測試以 `support/com_peer.rs` 開啟並扮演 DUT。
//! - 擁有者 host：`SW_TEST_COM_PAIR=COM20:COM21`（com0com `CNCA0`／`CNCB0`）。缺此變數時三案保持 ignored，
//!   gate 對應列為 BLOCKED-OWNER（arch §9.4）。
//! - byte 證據：peer 收到的全部 bytes 與 daemon WAL 的 TX 記錄逐筆比對（相同順序、相同內容）；secret 只出現在 peer 端、
//!   不在 WAL（Python `send_secret(..., log=False)`）。
//! - RPC 走 TCP loopback（Windows daemon 預設 endpoint 型態），埠號由測試挑選後經 `SERIALWRAP_ENDPOINT` 指定。
//! - 平台限制：本檔不得用平台 cfg（`scripts/rust/check_cfg_boundary.py`）；Windows 判定以路徑分隔字元
//!   （同 `event_lifecycle.rs` 的 `on_windows`）。
//!
//! 原生執行：`SW_TEST_COM_PAIR=COM20:COM21 scripts/rust/win-test.sh --caps windows_com --package sw-daemon --test cap_windows_com_p7`
//! （LOCAL-WINDOWS-TODO；Linux 只做建置與 `cargo check --target x86_64-pc-windows-gnu`）。
#![cfg(feature = "test-fakes")]
#![allow(non_snake_case)]
#[path = "support/com_peer.rs"]
mod com_peer;
mod support;

use std::collections::BTreeMap;
use std::path::PathBuf;
use std::time::{Duration, Instant};

use com_peer::{ComPeer, On, Reaction};
use support::*;
use sw_pal::traits::{EndpointSpec, SockTimeout};
use sw_proto::pyjson::{self, PyValue};

const PROMPT: &str = "root@prplOS:/# ";
const SECRET: &str = "pw";
const BANNER: &[u8] = b"\r\nU-Boot 2024.01\r\nHit any key to stop autoboot:  3 \r\n";

/// COM pair（cap 案例只在明確要求 ignored 測試時執行；缺變數即失敗而非 PASS）。
fn pair() -> (String, String) {
    com_peer::com_pair()
        .expect("cap:windows_com 需設定 SW_TEST_COM_PAIR=COMa:COMb（擁有者 host 為 COM20:COM21）")
}

fn on_windows() -> bool {
    std::path::MAIN_SEPARATOR == '\\'
}

/// prpl 登入腳本（同 `bb_faketarget` 的登入流程）：空行 → login prompt；帳號 → Password；secret → prompt；
/// `echo X` → `X` 與 prompt（ready probe 的 `__READY__<nonce>` 由 `{rest}` 回放）。
fn prpl_reactions() -> Vec<Reaction> {
    vec![
        Reaction::new(On::Eq(String::new()), "\r\nprplOS login: "),
        Reaction::new(On::Eq("root".into()), "\r\nPassword: "),
        Reaction::new(On::Secret(SECRET.into()), &format!("\r\n{PROMPT}")),
        Reaction::new(
            On::Prefix("echo ".into()),
            &format!("\r\n{{rest}}\r\n{PROMPT}"),
        ),
        Reaction::new(On::Any, &format!("\r\n{PROMPT}")),
    ]
}

struct Rig {
    env: IsolatedEnv,
    port: u16,
    daemon: Option<DaemonProc>,
}

impl Rig {
    fn start(com_a: &str, extra: &[(&str, String)]) -> Rig {
        let env = isolated_env();
        let profile = format!(
            r#"profiles:
  prpl-com:
    platform: prpl
    prompt_regex: "root@prplOS"
    login_regex: "(?mi)login:\\s*$"
    password_regex: "(?mi)password:\\s*$"
    user_env: "SW_TEST_USER"
    pass_env: "SW_TEST_PASS"
    ready_probe: "echo __READY__${{nonce}}"
    timeout_s: 15
targets:
  - com: COM0
    profile: prpl-com
    device_by_id: "{com_a}"
"#
        );
        std::fs::write(env.profile_dir.join("target.yaml"), profile).expect("寫入 profile");
        let port = std::net::TcpListener::bind("127.0.0.1:0")
            .and_then(|l| l.local_addr())
            .map(|a| a.port())
            .expect("挑選 RPC 埠");
        let mut vars: BTreeMap<String, String> = env.vars.clone();
        vars.insert(
            "SERIALWRAP_ENDPOINT".into(),
            format!("tcp://127.0.0.1:{port}"),
        );
        vars.insert("SERIALWRAP_TEST_DEVICE_ALLOWLIST".into(), com_a.to_string());
        vars.insert("SW_TEST_PASS".into(), SECRET.into());
        for (k, v) in extra {
            vars.insert((*k).to_string(), v.clone());
        }
        let daemon = DaemonProc::spawn_with(&env, &vars, &[]);
        let rig = Rig {
            env,
            port,
            daemon: Some(daemon),
        };
        rig.wait(Duration::from_secs(30), "daemon 回應 health.ping", |r| {
            r.call("health.ping", "{}")["ok"] == true
        });
        rig
    }

    fn call(&self, method: &str, params: &str) -> PyValue {
        let ep = EndpointSpec::Tcp {
            host: "127.0.0.1".into(),
            port: self.port,
        };
        let Ok(mut s) = sw_pal::native()
            .rpc
            .connect(&ep, SockTimeout::Blocking(Duration::from_secs(20)))
        else {
            return PyValue::Null;
        };
        let line = format!("{{\"id\":1,\"method\":\"{method}\",\"params\":{params}}}\n");
        if s.send_all(line.as_bytes()).is_err() {
            return PyValue::Null;
        }
        let (mut out, mut buf) = (Vec::new(), [0u8; 4096]);
        while !out.contains(&b'\n') {
            match s.recv(&mut buf) {
                Ok(0) | Err(_) => break,
                Ok(n) => out.extend_from_slice(&buf[..n]),
            }
        }
        let text = String::from_utf8_lossy(&out);
        pyjson::loads(text.lines().next().unwrap_or("")).unwrap_or(PyValue::Null)
    }

    fn state(&self) -> PyValue {
        self.call("session.get_state", r#"{"selector":"COM0"}"#)["session"].clone()
    }

    fn wait(&self, limit: Duration, what: &str, mut f: impl FnMut(&Rig) -> bool) {
        let end = Instant::now() + limit;
        while !f(self) {
            assert!(
                Instant::now() < end,
                "等待逾時：{what}；COM0 狀態 {:?}",
                self.state()
            );
            std::thread::sleep(Duration::from_millis(100));
        }
    }

    fn wait_ready(&self) {
        self.wait(Duration::from_secs(60), "COM0 READY", |r| {
            r.state()["state"] == "READY"
        });
    }

    /// WAL 中 COM0 的 TX payload（依 seq 順序串接）。
    fn wal_tx(&self) -> Vec<u8> {
        let path = self.env.wal_dir.join("raw.wal.ndjson");
        let mut rows: Vec<(i64, Vec<u8>)> = std::fs::read_to_string(path)
            .unwrap_or_default()
            .lines()
            .filter_map(|l| pyjson::loads(l).ok())
            .filter(|r| r["dir"] == "TX" && r["com"] == "COM0")
            .map(|r| {
                let seq = r["seq"].as_i64().unwrap_or(0);
                let b64 = r["payload_b64"].as_str().unwrap_or("").to_string();
                (seq, b64_decode(&b64))
            })
            .collect();
        rows.sort_by_key(|(s, _)| *s);
        rows.into_iter().flat_map(|(_, b)| b).collect()
    }

    fn events_log(&self) -> PathBuf {
        PathBuf::from(&self.env.vars["SERIALWRAP_EVENTS_RUNTIME_DIR"]).join("events.ndjson")
    }
}

impl Drop for Rig {
    fn drop(&mut self) {
        let _ = self.call("daemon.stop", "{}");
        if let Some(mut d) = self.daemon.take() {
            let _ = d.wait(Duration::from_secs(30));
        }
    }
}

fn b64_decode(s: &str) -> Vec<u8> {
    const T: &[u8; 64] = b"ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789+/";
    let (mut out, mut acc, mut bits) = (Vec::new(), 0u32, 0u32);
    for c in s.bytes().filter(|&c| c != b'=') {
        let Some(v) = T.iter().position(|&t| t == c) else {
            continue;
        };
        acc = (acc << 6) | v as u32;
        bits += 6;
        if bits >= 8 {
            bits -= 8;
            out.push((acc >> bits) as u8);
        }
    }
    out
}

/// peer 收到的 bytes 去掉 secret 一行後，須與 WAL TX 串接逐位元組相同；secret 恰出現在 peer 一次、不在 WAL。
fn assert_peer_matches_wal(peer: &[u8], wal: &[u8]) {
    let secret_line = format!("{SECRET}\n").into_bytes();
    let hits: Vec<usize> = peer
        .windows(secret_line.len())
        .enumerate()
        .filter(|(_, w)| *w == &secret_line[..])
        .map(|(i, _)| i)
        .collect();
    assert_eq!(
        hits.len(),
        1,
        "secret 須恰送出一次：{:?}",
        String::from_utf8_lossy(peer)
    );
    assert!(
        !wal.windows(SECRET.len() + 1).any(|w| w == &secret_line[..]),
        "secret 不得寫入 WAL"
    );
    let mut expect = peer[..hits[0]].to_vec();
    expect.extend_from_slice(&peer[hits[0] + secret_line.len()..]);
    assert_eq!(
        String::from_utf8_lossy(&expect),
        String::from_utf8_lossy(wal),
        "peer bytes 與 WAL TX 不一致"
    );
}

#[test]
#[ignore = "cap:windows_com"]
fn cap_windows_com__p7_login_ready() {
    let (a, b) = pair();
    let peer = ComPeer::open(&b, prpl_reactions());
    let rig = Rig::start(&a, &[]);
    rig.wait_ready();
    let sub = rig.call(
        "command.submit",
        r#"{"selector":"COM0","cmd":"echo P7_COM_OK","source":"agent:cap"}"#,
    );
    assert_eq!(sub["ok"], true, "{sub:?}");
    let cmd_id = sub["cmd_id"].as_str().unwrap_or("").to_string();
    rig.wait(Duration::from_secs(20), "命令完成", |r| {
        r.call("command.get", &format!(r#"{{"cmd_id":"{cmd_id}"}}"#))["command"]["status"] == "done"
    });
    assert!(peer.wait_contains(b"echo P7_COM_OK\n", Duration::from_secs(5)));
    assert_peer_matches_wal(&peer.received(), &rig.wal_tx());
}

#[test]
#[ignore = "cap:windows_com"]
fn cap_windows_com__p7_quiet_banner_zero_tx() {
    let (a, b) = pair();
    let peer = ComPeer::open(&b, prpl_reactions());
    let rig = Rig::start(&a, &[]);
    rig.wait_ready();
    peer.emit(BANNER);
    rig.wait(Duration::from_secs(10), "boot quiet 生效", |r| {
        !r.state()["boot_quiet_remaining_s"].is_null()
    });
    let mark = peer.mark();
    let sub = rig.call(
        "command.submit",
        r#"{"selector":"COM0","cmd":"echo X","source":"agent:cap"}"#,
    );
    assert_eq!(sub["ok"], false, "{sub:?}");
    assert_eq!(sub["error_code"], "AUTOBOOT_QUIET", "{sub:?}");
    std::thread::sleep(Duration::from_secs(3));
    assert_eq!(
        peer.received_since(mark),
        b"",
        "banner 之後 peer 必須收到 0 byte"
    );
    assert_peer_matches_wal(&peer.received(), &rig.wal_tx());
}

#[test]
#[ignore = "cap:windows_com"]
fn cap_windows_com__p7_event_fire() {
    let (a, b) = pair();
    let peer = ComPeer::open(&b, prpl_reactions());
    let rig = Rig::start(&a, &[]);
    let marker = rig.env.root.join("ev-fired");
    let tag = format!("swp7com{}", std::process::id());
    let handler = if on_windows() {
        // SC-26：shell handler 走 `cmd.exe /d /s /c`；`rem <tag>` 讓行程樹可辨識。
        format!(
            r#"{{"shell": "echo fired> \"{}\" & rem {tag}"}}"#,
            marker.display().to_string().replace('\\', "\\\\")
        )
    } else {
        format!(
            r#"{{"exec": ["/bin/sh", "-c", "touch '{}'; : {tag}"]}}"#,
            marker.display()
        )
    };
    std::fs::write(
        PathBuf::from(&rig.env.vars["SERIALWRAP_EVENTS_DIR"]).join("o.comfire.json"),
        format!(
            r#"{{"schema_version": 1, "owner": "o", "name": "comfire", "kind": "tool", "selectors": ["COM0"],
                "pattern": {{"kind": "contains", "value": "P7_EVENT_HIT"}}, "handler": {handler}}}"#
        ),
    )
    .expect("寫入事件規則");
    let reload = rig.call("event.reload", "{}");
    assert_eq!(reload["ok"], true, "{reload:?}");
    rig.wait_ready();
    peer.emit(b"\r\nP7_EVENT_HIT\r\n");
    rig.wait(Duration::from_secs(20), "fire_completed", |r| {
        std::fs::read_to_string(r.events_log())
            .unwrap_or_default()
            .lines()
            .any(|l| l.contains("\"fire_completed\""))
    });
    assert!(marker.exists(), "handler 未執行（缺 {}）", marker.display());
    let completed: Vec<PyValue> = std::fs::read_to_string(rig.events_log())
        .unwrap_or_default()
        .lines()
        .filter_map(|l| pyjson::loads(l).ok())
        .filter(|e| e["type"] == "fire_completed")
        .collect();
    assert_eq!(completed.len(), 1, "{completed:?}");
    assert_eq!(completed[0]["exit_code"].as_i64(), Some(0), "{completed:?}");
    // Job 結束後無殘留行程：命令列含 tag 的行程不得存在（Windows 以 PowerShell 查 Win32_Process）。
    if on_windows() {
        let query = format!(
            "@(Get-CimInstance Win32_Process | Where-Object {{ $_.CommandLine -like '*{tag}*' -and $_.Name -ne 'powershell.exe' }}).Count"
        );
        let out = std::process::Command::new("powershell")
            .args(["-NoProfile", "-Command", &query])
            .output();
        let n = out.ok().and_then(|o| {
            String::from_utf8_lossy(&o.stdout)
                .trim()
                .parse::<u32>()
                .ok()
        });
        assert_eq!(n, Some(0), "handler Job 結束後仍有殘留行程");
    }
    assert_peer_matches_wal(&peer.received(), &rig.wal_tx());
}
