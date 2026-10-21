//! P4-T8 `remote`（`-R`／`-L`／`status`／`close`／`stop` 別名）原生與 BC-05 Windows 路徑（SC-21）的失敗測試。
//!
//! 紅燈階段（TDD）：`sw_cli::remote_tunnel` 與 `remote` 原生分派尚未實作，本檔案的案例應以
//! `not implemented: P4-T8` 或斷言不符失敗，不得因編譯錯誤、fixture 缺漏或 sentinel 委派失敗。
//! 替身與 `TestEnv` 由 P4-T1（`tests/support/mod.rs`）提供；本檔只使用 plan 記載的 API。
//! 規格來源：`docs/rust-refactor/plans/05-stage1b-rust-cli-mgmt.md` Task 8；盤點 `inventory/07-platform-mgmt.md` §8。
mod support;

use support::*;
use sw_cli::remote_tunnel::*;
use sw_pal::PalErrorKind;
use sw_proto::pyjson::{PyInt, PyValue};

/// 測試用 expose 規格：`ssh_opts` 含 `-p2222`，與 plan 的位元組級 argv 斷言一致。
fn spec(role: Role) -> TunnelSpec {
    TunnelSpec {
        role,
        ssh_target: "u@relay".into(),
        port: 48800,
        local: None,
        forward_src: Some("/run/sw.sock".into()),
        remote_socket: None,
        via: Via::Ssh,
        ssh_opts: vec!["-p2222".into()],
        ready_timeout: 10.0,
    }
}

/// connect（`-L`）規格；`forward_src` 不參與 connect 的 argv 與 identity 外的語意。
fn connect_spec(local: Option<u16>, remote_socket: Option<&str>) -> TunnelSpec {
    TunnelSpec {
        role: Role::Connect,
        ssh_target: "u@relay".into(),
        port: 48800,
        local,
        forward_src: None,
        remote_socket: remote_socket.map(str::to_string),
        via: Via::Ssh,
        ssh_opts: vec![],
        ready_timeout: 10.0,
    }
}

fn sv(items: &[&str]) -> Vec<String> {
    items.iter().map(|s| s.to_string()).collect()
}

/// Windows expose（tcp）的 `ss` 探測 argv：ssh_opts 在前、`-o BatchMode=yes` 之後接 target（BC-05，新連線，不用 ControlPath）。
const WIN_SS_PROBE: [&str; 8] = [
    "ssh",
    "-p2222",
    "-o",
    "BatchMode=yes",
    "u@relay",
    "ss",
    "-ltnH",
    "sport = :48800",
];

/// `-R u@relay:48800` 在指定 POSIX endpoint 下的 identity。
fn expose_identity(forward_src: &str) -> String {
    compute_identity(&TunnelSpec {
        role: Role::Expose,
        ssh_target: "u@relay".into(),
        port: 48800,
        local: None,
        forward_src: Some(forward_src.to_string()),
        remote_socket: None,
        via: Via::Ssh,
        ssh_opts: vec![],
        ready_timeout: 10.0,
    })
}

/// 把路徑字串嵌入 JSON 字串常值時所需的跳脫（`\\` 與 `"`）；Windows 主機的 tmp 路徑含反斜線。
fn json_escape(value: &str) -> String {
    value.replace('\\', "\\\\").replace('"', "\\\"")
}

/// `build_argv` 輸出中緊接在 `flag` 之後的參數值（`-L`／`-R` 的方向參數）。
fn arg_after<'a>(argv: &'a [String], flag: &str) -> &'a str {
    let index = argv
        .iter()
        .position(|a| a == flag)
        .expect("argv 缺少方向參數");
    &argv[index + 1]
}

/// `t.calls()` 中 argv 與 `want` 完全相同的次數。
fn count_argv(t: &TestEnv, want: &[&str]) -> usize {
    let want = sv(want);
    t.calls()
        .iter()
        .filter(|call| call.as_slice() == want.as_slice())
        .count()
}

/// 扁平 JSON 物件（registry state）的頂層鍵，依出現順序；鍵皆位於 `"key":` 形式。
fn flat_keys(doc: &str) -> Vec<String> {
    let mut keys = Vec::new();
    let mut rest = doc;
    while let Some(pos) = rest.find("\":") {
        let head = &rest[..pos];
        if let Some(start) = head.rfind('"') {
            keys.push(head[start + 1..].to_string());
        }
        rest = &rest[pos + 2..];
    }
    keys
}

/// 扁平 JSON 物件中 `"key":` 之後、到下一個 `,` 或 `}` 為止的原始值文字。
fn json_field<'a>(doc: &'a str, key: &str) -> &'a str {
    let pattern = format!("\"{key}\":");
    let start = doc
        .find(&pattern)
        .unwrap_or_else(|| panic!("缺少欄位 {key}：{doc}"))
        + pattern.len();
    let rest = &doc[start..];
    let end = rest.find([',', '}']).unwrap_or(rest.len());
    &rest[..end]
}

// ---- 純函式（remote_tunnel 對外簽章）----

#[test]
fn posix_expose_argv_is_byte_exact() {
    assert_eq!(
        build_argv(&spec(Role::Expose), "/r/remote/cm-48800", true),
        sv(&[
            "ssh",
            "-N",
            "-T",
            "-o",
            "BatchMode=yes",
            "-o",
            "ExitOnForwardFailure=yes",
            "-o",
            "ControlMaster=auto",
            "-o",
            "ControlPath=/r/remote/cm-48800",
            "-o",
            "ControlPersist=no",
            "-o",
            "ServerAliveInterval=30",
            "-o",
            "ServerAliveCountMax=3",
            "-p2222",
            "-R",
            "127.0.0.1:48800:/run/sw.sock",
            "u@relay",
        ])
    );
}

#[test]
fn windows_argv_drops_control_master_but_identity_unchanged() {
    let a = build_argv(&spec(Role::Expose), "ignored", false);
    assert!(!a.iter().any(|x| x.starts_with("Control")));
    // 其餘參數相同：只省略 ControlMaster／ControlPath／ControlPersist 三組 `-o`。
    assert_eq!(
        a,
        sv(&[
            "ssh",
            "-N",
            "-T",
            "-o",
            "BatchMode=yes",
            "-o",
            "ExitOnForwardFailure=yes",
            "-o",
            "ServerAliveInterval=30",
            "-o",
            "ServerAliveCountMax=3",
            "-p2222",
            "-R",
            "127.0.0.1:48800:/run/sw.sock",
            "u@relay",
        ])
    );
    assert_eq!(
        compute_identity(&spec(Role::Expose)),
        sha256_hex("expose|u@relay|48800|None|/run/sw.sock|None|ssh|-p2222")
    );
}

#[test]
fn parse_target_errors() {
    let e = parse_target("relay").unwrap_err();
    assert_eq!(e.code, "INVALID_TARGET");
    assert_eq!(
        e.message.as_deref(),
        Some("target 需為 [user@]host:port，取得 'relay'")
    );

    let e = parse_target("h:0").unwrap_err();
    assert_eq!(e.code, "INVALID_TARGET");
    assert_eq!(e.message.as_deref(), Some("port 超出範圍：0"));

    let e = parse_target("h:999999999999999999999").unwrap_err();
    assert_eq!(e.code, "INVALID_TARGET");
    assert_eq!(
        e.message.as_deref(),
        Some("port 超出範圍：999999999999999999999")
    );

    assert_eq!(parse_target("h:22\n").unwrap(), ("h".to_string(), 22));
}

#[test]
fn connect_argv_default_dest() {
    // local 未給時沿用遠端 port；dest 預設 localhost:<port>。
    let a = build_argv(&connect_spec(None, None), "/r/cm", true);
    assert_eq!(arg_after(&a, "-L"), "127.0.0.1:48800:localhost:48800");

    // --remote-socket 取代 dest。
    let a = build_argv(&connect_spec(None, Some("/s")), "/r/cm", true);
    assert_eq!(arg_after(&a, "-L"), "127.0.0.1:48800:/s");

    // --local 指定本地 listen port。
    let a = build_argv(&connect_spec(Some(49000), None), "/r/cm", true);
    assert_eq!(arg_after(&a, "-L"), "127.0.0.1:49000:localhost:48800");
}

#[test]
fn autossh_prefix() {
    let mut s = spec(Role::Expose);
    s.via = Via::Autossh;
    let a = build_argv(&s, "/r/cm", true);
    assert_eq!(a[..5].to_vec(), sv(&["autossh", "-M", "0", "-N", "-T"]));
}

#[test]
fn bind_loopback_only_cases() {
    assert!(bind_is_loopback_only(
        "LISTEN 0 128 127.0.0.1:48800 0.0.0.0:*",
        48800
    ));
    assert!(!bind_is_loopback_only(
        "LISTEN 0 128 0.0.0.0:48800 0.0.0.0:*",
        48800
    ));
    assert!(bind_is_loopback_only(
        "LISTEN 0 128 [::1]:48800 [::]:*",
        48800
    ));
    assert!(!bind_is_loopback_only(
        "LISTEN 0 128 127.0.0.1:22 0.0.0.0:*",
        48800
    ));
}

#[test]
fn registry_write_disk_stable() {
    let t = TestEnv::posix();
    let mut st = alive_state_with_identity("expose", 999, "abc");
    st.insert("listen_port", PyValue::Int(PyInt::Small(48800)));
    Registry::new(&t.run_dir())
        .write(&st, 999)
        .expect("registry 寫入");

    // 檔名為 <port>.json，內容為 DISK_STABLE（sort_keys、緊湊、無尾端換行）。
    let bytes =
        std::fs::read(t.run_dir().join("remote").join("48800.json")).expect("檔名應為 <port>.json");
    assert!(!bytes.ends_with(b"\n"));
    let doc = String::from_utf8(bytes).expect("UTF-8");
    assert!(!doc.contains(": ") && !doc.contains(", "), "{doc}");
    let keys = flat_keys(&doc);
    let mut sorted = keys.clone();
    sorted.sort();
    assert_eq!(keys, sorted, "鍵必須依字典序排列：{doc}");
}

// ---- open／status／close（POSIX）----

#[test]
fn open_already_running_same_identity() {
    let base = TestEnv::posix().with_tunnel_ready();
    let identity = expose_identity(&base.default_socket());
    let t = base.with_registry_state(48800, alive_state_with_identity("expose", 999, &identity));
    t.tunnel().set_alive(999, true);

    let (rc, out, _) = t.run(&["remote", "-R", "u@relay:48800"]);
    assert_eq!(rc, 0, "{out}");
    assert!(out.contains("\"already_running\":true"), "{out}");
    assert!(out.contains("\"ok\":true"), "{out}");
    assert!(t.tunnel().spawned().is_empty(), "同 identity 不得 spawn");
}

#[test]
fn open_conflict_different_identity() {
    let t = TestEnv::posix().with_tunnel_ready().with_registry_state(
        48800,
        alive_state_with_identity("expose", 999, "not-this-identity"),
    );
    t.tunnel().set_alive(999, true);

    let (rc, out, _) = t.run(&["remote", "-R", "u@relay:48800"]);
    assert_eq!(rc, 1);
    assert_eq!(
        out,
        "{\"error_code\":\"TUNNEL_CONFLICT\",\"message\":\"port 48800 已有不同 identity 的隧道，請先 remote close 48800\",\"ok\":false}\n"
    );
    assert!(t.tunnel().spawned().is_empty());
}

#[test]
fn open_spawn_failed_reports_tail() {
    let t = TestEnv::posix().with_tunnel_ready();
    // 行程於第一次 alive 檢查即結束；stderr tail 為 `Permission denied`。
    t.tunnel().die_on_spawn();
    t.tunnel().set_tail("Permission denied");

    let (rc, out, _) = t.run(&["remote", "-R", "u@relay:48800"]);
    assert_eq!(rc, 1);
    assert_eq!(
        out,
        "{\"error_code\":\"TUNNEL_SPAWN_FAILED\",\"message\":\"Permission denied\",\"ok\":false}\n"
    );
    assert!(
        !t.run_dir().join("remote").join("48800.json").exists(),
        "失敗路徑必須移除 registry 項目"
    );
}

#[test]
fn open_spawn_failed_empty_tail_uses_error_code_as_message() {
    let t = TestEnv::posix().with_tunnel_ready();
    t.tunnel().die_on_spawn();
    t.tunnel().set_tail("");

    let (rc, out, _) = t.run(&["remote", "-R", "u@relay:48800"]);
    assert_eq!(rc, 1);
    assert_eq!(
        out,
        "{\"error_code\":\"TUNNEL_SPAWN_FAILED\",\"message\":\"TUNNEL_SPAWN_FAILED\",\"ok\":false}\n"
    );
}

#[test]
fn open_role_probe_unverified_no_retry() {
    // POSIX（PLT-O-10 KEEP）：ss 探測回空輸出即第一次失敗就 REMOTE_BIND_UNVERIFIED，不重試。
    let t = TestEnv::posix().with_tunnel_ready();

    let (rc, out, _) = t.run(&["remote", "-R", "u@relay:48800"]);
    assert_eq!(rc, 1);
    assert_eq!(
        out,
        "{\"error_code\":\"REMOTE_BIND_UNVERIFIED\",\"message\":\"無法在 relay 驗證遠端 bind\",\"ok\":false}\n"
    );
    let ss_calls = t
        .calls()
        .iter()
        .filter(|call| call.iter().any(|a| a == "ss"))
        .count();
    assert_eq!(ss_calls, 1, "ss 探測只能呼叫一次");
    assert_eq!(
        t.tunnel().terminated().len(),
        1,
        "探測失敗須 terminate 隧道"
    );
}

#[test]
fn open_role_probe_preserves_merged_stdout_stderr_order() {
    let t = TestEnv::posix().with_tunnel_ready();
    let cp = t
        .run_dir()
        .join("remote")
        .join("cm-48800")
        .display()
        .to_string();
    let argv = [
        "ssh".to_string(),
        "-o".to_string(),
        format!("ControlPath={cp}"),
        "u@relay".to_string(),
        "ss".to_string(),
        "-ltnH".to_string(),
        "sport = :48800".to_string(),
    ];
    let refs: Vec<&str> = argv.iter().map(String::as_str).collect();
    let t = t.with_command_merged(
        &refs,
        0,
        "Warning: relay first\nLISTEN 0 128 0.0.0.0:48800 0.0.0.0:*\n",
    );
    let (rc, out, _) = t.run(&["remote", "-R", "u@relay:48800"]);
    assert_eq!(rc, 1);
    assert!(
        out.contains("Warning: relay first\\nLISTEN 0 128 0.0.0.0:48800"),
        "合併輸出必須保留寫入順序：{out}"
    );
}

#[test]
fn open_role_probe_runner_error_fails_and_cleans_up() {
    let t = TestEnv::windows().with_tunnel_ready().with_command_err(
        &WIN_SS_PROBE,
        PalErrorKind::NotFound,
        "[Errno 2] No such file or directory: 'ssh'",
    );
    let (rc, out, _) = t.run(&[
        "remote",
        "-R",
        "u@relay:48800",
        "--ssh-opt=-p2222",
        "--ready-timeout",
        "1",
    ]);
    assert_eq!(rc, 1, "runner 例外須 fail-closed：{out}");
    assert!(out.contains("\"error_code\":\"INTERNAL_ERROR\""), "{out}");
    assert_eq!(t.tunnel().terminated(), vec![4000]);
    assert!(
        !t.run_dir().join("remote").join("48800.json").exists(),
        "清理成功後 registry 必須移除"
    );
}

#[test]
fn open_starting_when_deadline_exceeded() {
    let t = TestEnv::posix().with_tunnel_ready();
    let cp = t
        .run_dir()
        .join("remote")
        .join("cm-48800")
        .display()
        .to_string();
    let control_path = format!("ControlPath={cp}");
    // POSIX ssh_check：`ssh -O check` 一直失敗。
    let check = ["ssh", "-O", "check", "-o", control_path.as_str(), "u@relay"];
    let t = t.with_command(&check, 255, "", "");

    let (rc, out, _) = t.run(&["remote", "-R", "u@relay:48800", "--ready-timeout", "1"]);
    assert_eq!(rc, 0, "逾時仍為 starting，exit 0：{out}");
    assert!(out.contains("\"status\":\"starting\""), "{out}");
    assert!(
        t.tunnel().terminated().is_empty(),
        "starting 不得 terminate"
    );
}

#[test]
fn open_local_on_expose_invalid_args() {
    let t = TestEnv::posix();
    let (rc, out, _) = t.run(&["remote", "-R", "--local", "1", "h:1"]);
    assert_eq!(rc, 1);
    assert_eq!(
        out,
        "{\"error_code\":\"INVALID_ARGS\",\"message\":\"--local 僅用於 -L（connect）\",\"ok\":false}\n"
    );
}

#[test]
fn open_local_validation_matches_python_order_and_big_integer() {
    let t = TestEnv::posix();
    for value in ["0", "70000"] {
        let (rc, out, _) = t.run(&["remote", "-R", "--local", value, "h:1"]);
        assert_eq!(rc, 1);
        assert_eq!(
            out,
            "{\"error_code\":\"INVALID_ARGS\",\"message\":\"--local 僅用於 -L（connect）\",\"ok\":false}\n"
        );
    }

    let huge = "12345678901234567890123456789012345678901234567890";
    let (rc, out, _) = t.run(&["remote", "-L", "--local", huge, "h:1"]);
    assert_eq!(rc, 1);
    assert_eq!(
        out,
        format!(
            "{{\"error_code\":\"INVALID_ARGS\",\"message\":\"--local 超出範圍：{huge}\",\"ok\":false}}\n"
        )
    );
}

#[test]
fn open_r_and_l_invalid_args() {
    let t = TestEnv::posix();
    let (rc, out, _) = t.run(&["remote", "-R", "-L", "h:1"]);
    assert_eq!(rc, 1);
    assert_eq!(
        out,
        "{\"error_code\":\"INVALID_ARGS\",\"message\":\"-R 與 -L 不可同時指定\",\"ok\":false}\n"
    );
}

#[test]
fn status_prunes_dead_and_lists_orphan() {
    let t = TestEnv::posix().with_registry_state(48800, alive_state("expose", 111));
    t.tunnel().set_alive(111, false);
    std::fs::create_dir_all(t.run_dir().join("remote")).unwrap();
    std::fs::write(t.run_dir().join("remote").join("cm-49000"), b"").unwrap();

    let (rc, out, _) = t.run(&["remote"]);
    assert_eq!(rc, 0);
    let cm = t
        .run_dir()
        .join("remote")
        .join("cm-49000")
        .display()
        .to_string();
    let cm = json_escape(&cm);
    assert_eq!(
        out,
        format!(
            "{{\"ok\":true,\"tunnels\":[{{\"alive\":true,\"control_path\":\"{cm}\",\"status\":\"orphan\"}}]}}\n"
        )
    );
    assert!(
        !t.run_dir().join("remote").join("48800.json").exists(),
        "死亡的 state 應被 status 就地移除"
    );
}

#[test]
fn close_dead_pid_not_signalled() {
    let t = TestEnv::posix().with_registry_state(48800, alive_state("expose", 222));
    t.tunnel().set_alive(222, false);

    let (rc, out, _) = t.run(&["remote", "close", "48800"]);
    assert_eq!(rc, 0);
    assert_eq!(out, "{\"closed\":[48800],\"ok\":true}\n");
    assert!(
        t.tunnel().terminated().is_empty(),
        "pid 已死不得送出終止訊號"
    );
}

#[test]
fn close_terminate_error_marks_error() {
    let t = TestEnv::posix().with_registry_state(48800, alive_state("expose", 333));
    t.tunnel().set_alive(333, true);
    t.tunnel().terminate_fails(333);

    let (rc, out, _) = t.run(&["remote", "close", "48800"]);
    assert_eq!(rc, 0);
    assert_eq!(out, "{\"closed\":[],\"ok\":true}\n");
    let st = std::fs::read_to_string(t.run_dir().join("remote").join("48800.json")).unwrap();
    assert!(st.contains("\"status\":\"error\""), "{st}");
}

#[test]
fn remote_close_empty_selector_closes_nothing() {
    // `remote close ""` 篩出 0 筆，不得視為 all（cli.py:1564 的 words[1] 語意）。
    let t = TestEnv::posix()
        .with_registry_state(48800, alive_state("expose", 111))
        .with_registry_state(48801, alive_state("expose", 112));
    t.tunnel().set_alive(111, true);
    t.tunnel().set_alive(112, true);

    for argv in [["remote", "close", ""], ["remote", "stop", ""]] {
        let (rc, out, _) = t.run(&argv);
        assert_eq!(rc, 0);
        assert_eq!(out, "{\"closed\":[],\"ok\":true}\n", "argv={argv:?}");
    }
    assert!(t.tunnel().terminated().is_empty());
}

#[test]
fn registry_reads_python_written_pgid_null() {
    // 1A 委派期間由 Python 寫出的 state（Windows 形狀：pgid／pid_start_ticks 為 null）必須可讀、可關閉。
    let t = TestEnv::posix().with_tunnel_ready();
    let dir = t.run_dir().join("remote");
    std::fs::create_dir_all(&dir).unwrap();
    let cp = json_escape(&dir.join("cm-48800").display().to_string());
    let doc = format!(
        "{{\"control_path\":\"{cp}\",\"endpoint\":\"tcp://127.0.0.1:48800\",\"forward_target\":\"/run/sw.sock\",\"identity\":\"x\",\"listen_port\":48800,\"pgid\":null,\"pid\":999,\"pid_start_ticks\":null,\"remote_bind\":\"127.0.0.1:48800\",\"role\":\"expose\",\"status\":\"active\",\"target\":\"u@relay\",\"via\":\"ssh\"}}"
    );
    std::fs::write(dir.join("48800.json"), doc).unwrap();
    t.tunnel().set_alive(999, true);

    let (rc, out, _) = t.run(&["remote"]);
    assert_eq!(rc, 0);
    assert!(out.contains("\"pid\":999"), "{out}");
    assert!(out.contains("\"status\":\"active\""), "{out}");

    let (rc, out, _) = t.run(&["remote", "close", "48800"]);
    assert_eq!(rc, 0);
    assert_eq!(out, "{\"closed\":[48800],\"ok\":true}\n");
    assert_eq!(t.tunnel().terminated(), vec![999]);
}

// ---- Windows（BC-05）----

#[test]
fn windows_role_probe_tcp_uses_new_connection() {
    let t = TestEnv::windows().with_tunnel_ready();
    let (rc, out, _) = t.run(&[
        "remote",
        "-R",
        "u@relay:48800",
        "--ssh-opt=-p2222",
        "--ready-timeout",
        "1",
    ]);
    assert_eq!(rc, 0, "{out}");
    assert!(
        count_argv(&t, &WIN_SS_PROBE) >= 1,
        "Windows expose tcp 探測必須以新連線執行：{:?}",
        t.calls()
    );
    assert!(
        !t.calls()
            .iter()
            .any(|call| call.iter().any(|a| a.starts_with("ControlPath"))),
        "Windows 探測不得使用 ControlPath"
    );
}

#[test]
fn windows_connect_ready_via_ping() {
    let t = TestEnv::windows().with_tunnel_ready().with_rpc_ok(
        "tcp://127.0.0.1:48800",
        "health.ping",
        "{\"ok\":true}",
    );
    let (rc, out, _) = t.run(&["remote", "-L", "u@relay:48800"]);
    assert_eq!(rc, 0, "{out}");
    assert!(out.contains("\"status\":\"active\""), "{out}");
}

#[test]
fn windows_state_matches_p1_schema() {
    let t = TestEnv::windows().with_tunnel_ready().with_rpc_ok(
        "tcp://127.0.0.1:48800",
        "health.ping",
        "{\"ok\":true}",
    );
    let (rc, out, _) = t.run(&["remote", "-L", "u@relay:48800"]);
    assert_eq!(rc, 0, "{out}");

    let st = std::fs::read_to_string(t.run_dir().join("remote").join("48800.json")).unwrap();
    assert_eq!(json_field(&st, "pgid"), "null", "{st}");
    assert!(
        json_field(&st, "pid_start_ticks").parse::<u64>().is_ok(),
        "pid_start_ticks 應為 CreationTime 整數：{st}"
    );
    assert!(
        json_field(&st, "control_path").starts_with('"'),
        "control_path 仍須寫入字串：{st}"
    );
    assert!(
        !t.run_dir().join("remote").join("cm-48800").exists(),
        "Windows 不得建立 ControlPath 檔案"
    );
}

#[test]
fn windows_expose_probe_before_forward_ready_retries() {
    // 前 3 次 ss 回空（forward 尚未建立），第 4 次回 loopback 監聽列 → active；共 4 次，且不 terminate。
    let t = TestEnv::windows().with_tunnel_ready().with_command_seq(
        &WIN_SS_PROBE,
        &[
            (0, "", ""),
            (0, "", ""),
            (0, "", ""),
            (0, "LISTEN 0 128 127.0.0.1:48800 0.0.0.0:*", ""),
        ],
    );
    let (rc, out, _) = t.run(&["remote", "-R", "u@relay:48800", "--ssh-opt=-p2222"]);
    assert_eq!(rc, 0, "{out}");
    assert!(out.contains("\"status\":\"active\""), "{out}");
    assert_eq!(count_argv(&t, &WIN_SS_PROBE), 4);
    assert!(t.tunnel().terminated().is_empty());
}

#[test]
fn windows_expose_probe_rc_nonzero_is_not_ready() {
    let t = TestEnv::windows()
        .with_tunnel_ready()
        .with_command(&WIN_SS_PROBE, 255, "", "");
    let (rc, out, _) = t.run(&[
        "remote",
        "-R",
        "u@relay:48800",
        "--ssh-opt=-p2222",
        "--ready-timeout",
        "1",
    ]);
    assert_eq!(rc, 0, "{out}");
    assert!(out.contains("\"status\":\"starting\""), "{out}");
    assert!(t.tunnel().terminated().is_empty(), "尚未就緒不得 terminate");
}

#[test]
fn windows_expose_wildcard_bind_fails_closed() {
    // ss 列出的 bind 為 wildcard（0.0.0.0）→ fail-closed：REMOTE_BIND_UNVERIFIED 並 terminate。
    let t = TestEnv::windows().with_tunnel_ready().with_command_seq(
        &WIN_SS_PROBE,
        &[(0, "LISTEN 0 128 0.0.0.0:48800 0.0.0.0:*", "")],
    );
    let (rc, out, _) = t.run(&["remote", "-R", "u@relay:48800", "--ssh-opt=-p2222"]);
    assert_eq!(rc, 1, "{out}");
    assert!(
        out.contains("\"error_code\":\"REMOTE_BIND_UNVERIFIED\""),
        "{out}"
    );
    assert_eq!(count_argv(&t, &WIN_SS_PROBE), 1);
    assert_eq!(t.tunnel().terminated().len(), 1);
}

#[test]
fn windows_wildcard_probe_retains_error_state_when_terminate_fails() {
    let t = TestEnv::windows().with_tunnel_ready().with_command_seq(
        &WIN_SS_PROBE,
        &[(0, "LISTEN 0 128 0.0.0.0:48800 0.0.0.0:*", "")],
    );
    t.tunnel().terminate_fails(4000);
    let (rc, out, _) = t.run(&["remote", "-R", "u@relay:48800", "--ssh-opt=-p2222"]);
    assert_eq!(rc, 1, "清理失敗須回報：{out}");
    assert!(out.contains("\"error_code\":\"INTERNAL_ERROR\""), "{out}");
    let path = t.run_dir().join("remote").join("48800.json");
    let state = std::fs::read_to_string(path).expect("終止失敗時保留 registry");
    assert!(state.contains("\"status\":\"error\""), "{state}");
    assert!(state.contains("\"pid\":4000"), "{state}");
    assert!(state.contains("\"identity\":"), "{state}");
}

#[test]
fn remote_ssh_resolution_precedes_endpoint_and_registry() {
    let t = TestEnv::posix();
    let (rc, out, _) = t.run(&[
        "--endpoint",
        "http://invalid",
        "remote",
        "-R",
        "u@relay:48800",
    ]);
    assert_eq!(rc, 1);
    assert!(out.contains("\"error_code\":\"SSH_NOT_FOUND\""), "{out}");
    assert!(
        !t.run_dir().join("remote").exists(),
        "ssh 缺失檢查不得建立 registry 副作用"
    );

    let base = TestEnv::posix()
        .with_registry_state(48800, alive_state_with_identity("expose", 999, "existing"));
    base.tunnel().set_alive(999, true);
    let (rc, out, _) = base.run(&["remote", "-R", "u@relay:48800"]);
    assert_eq!(rc, 1);
    assert!(out.contains("\"error_code\":\"SSH_NOT_FOUND\""), "{out}");
    assert!(
        base.run_dir().join("remote").join("48800.json").exists(),
        "ssh 缺失不得刪除或改寫既有 state"
    );
    assert!(
        !base
            .run_dir()
            .join("remote")
            .join(".registry.lock")
            .exists(),
        "ssh 缺失檢查必須先於 registry lock"
    );
}

// ---- 別名與 CLI 分派（SC-21）----

#[test]
fn remote_stop_is_alias_of_close() {
    let t = TestEnv::posix().with_registry_state(48800, alive_state("expose", 999));
    t.tunnel().set_alive(999, true);
    let (rc, out, _) = t.run(&["remote", "stop", "48800"]);
    assert_eq!(
        (rc, out.as_str()),
        (0, "{\"closed\":[48800],\"ok\":true}\n")
    );
    assert_eq!(t.tunnel().terminated(), vec![999]);
}
