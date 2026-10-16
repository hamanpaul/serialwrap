//! client 側 RPC transport 與 Python 基線（`oserror.<os>.tsv`）對照（P3-T9）。
//!
//! golden 為唯一 oracle：`message`、`errno` 必須逐字相同（Q-10～Q-13）；不得改寫 golden 遷就實作。
mod common;

use std::time::Duration;

#[cfg(unix)]
use std::ffi::OsString;

use sw_pal::traits::{EndpointSpec, PalErrorKind, SockTimeout};
#[cfg(unix)]
use sw_pal::traits::PalError;

fn pal() -> sw_pal::ClientPlatform {
    sw_pal::native_client()
}

/// connect 類 case（`resolve_tcp` 列另由 `golden_resolve` 取得）。
fn golden_case(id: &str) -> common::OsErrCase {
    common::oserror_cases(common::oserror_file())
        .into_iter()
        .find(|c| c.id == id && c.op != "resolve_tcp")
        .unwrap_or_else(|| panic!("golden 缺少 case {id}"))
}

fn golden_resolve(id: &str) -> common::OsErrCase {
    common::oserror_cases(common::oserror_file())
        .into_iter()
        .find(|c| c.id == id && c.op == "resolve_tcp")
        .unwrap_or_else(|| panic!("golden 缺少 resolve_tcp case {id}"))
}

/// 不 accept 的 listener：連線由 backlog 完成，recv 永遠不會有資料。回傳值需保留到測試結束。
#[cfg(unix)]
fn silent_listener() -> (EndpointSpec, Box<dyn std::any::Any>) {
    let d = tempfile::tempdir().unwrap();
    let p = d.path().join("silent.sock");
    let l = std::os::unix::net::UnixListener::bind(&p).unwrap();
    (EndpointSpec::Unix(p.into_os_string()), Box::new((d, l)))
}

#[cfg(windows)]
fn silent_listener() -> (EndpointSpec, Box<dyn std::any::Any>) {
    let l = std::net::TcpListener::bind("127.0.0.1:0").unwrap();
    let port = l.local_addr().unwrap().port();
    (
        EndpointSpec::Tcp {
            host: "127.0.0.1".into(),
            port,
        },
        Box::new(l),
    )
}

#[cfg(unix)]
#[test]
fn unix_enoent_matches_python_message() {
    let d = tempfile::tempdir().unwrap();
    let p = d.path().join("nope.sock");
    let e = pal()
        .rpc
        .connect(
            &EndpointSpec::Unix(p.into_os_string()),
            SockTimeout::Blocking(Duration::from_secs(1)),
        )
        .err()
        .unwrap();
    let g = golden_case("unix_enoent");
    assert_eq!((e.py_message.as_str(), e.os_code), (g.message.as_str(), g.errno));
}

#[cfg(unix)]
#[test]
fn unix_path_too_long_has_no_errno() {
    let p = OsString::from(format!("/tmp/{}", "x".repeat(200)));
    let e = pal()
        .rpc
        .connect(&EndpointSpec::Unix(p), SockTimeout::Blocking(Duration::from_secs(1)))
        .err()
        .unwrap();
    assert_eq!(e.py_message, "AF_UNIX path too long");
    assert_eq!(e.os_code, None);
}

#[test]
fn recv_times_out_per_call() {
    let (ep, _keep) = silent_listener(); // 不 accept：連線由 backlog 完成，recv 不會有資料
    let mut s = pal()
        .rpc
        .connect(&ep, SockTimeout::Blocking(Duration::from_millis(200)))
        .unwrap();
    s.send_all(b"{}\n").unwrap();
    let t0 = std::time::Instant::now();
    let e = s.recv(&mut [0u8; 16]).err().unwrap();
    assert_eq!(e.kind, PalErrorKind::TimedOut);
    assert!(t0.elapsed() >= Duration::from_millis(180) && t0.elapsed() < Duration::from_secs(2));
}

#[test]
fn tcp_refused_and_gai_and_timeout0_match_python() {
    for id in ["tcp_refused", "tcp_localhost_refused", "tcp_gai", "tcp_timeout0"] {
        let g = golden_case(id);
        let e = pal()
            .rpc
            .connect(&g.endpoint(), g.timeout())
            .err()
            .unwrap_or_else(|| panic!("{id} 應失敗"));
        assert_eq!((e.py_message.as_str(), e.os_code), (g.message.as_str(), g.errno), "{id}");
    }
}

#[cfg(unix)]
#[test]
fn unix_len_107_vs_108() {
    for (id, len) in [("unix_len_107", 107usize), ("unix_len_108", 108usize)] {
        let g = golden_case(id);
        let ep = g.endpoint();
        match &ep {
            EndpointSpec::Unix(p) => assert_eq!(p.len(), len, "{id} 路徑位元組數"),
            other => panic!("{id}: 預期 Unix 端點，實得 {other:?}"),
        }
        let e = pal()
            .rpc
            .connect(&ep, g.timeout())
            .err()
            .expect("unix 長度案例應失敗");
        assert_eq!((e.py_message.as_str(), e.os_code), (g.message.as_str(), g.errno), "{id}");
    }
}

#[test]
fn resolve_tcp_gai_matches_python() {
    let g = golden_resolve("tcp_gai");
    let e = pal()
        .rpc
        .resolve_tcp("no-such-host.invalid", 1)
        .err()
        .expect("resolve_tcp 應失敗");
    assert_eq!((e.py_message.as_str(), e.os_code), (g.message.as_str(), g.errno));
}

/// 需要檔案系統 fixture 的 unix 案例（golden 列 `tmp:` 標籤）；回傳實作的結果。
#[cfg(unix)]
fn unix_case_result(id: &str, dir: &std::path::Path) -> Result<(), PalError> {
    use std::os::unix::fs::PermissionsExt;
    use std::os::unix::net::UnixListener;

    let p = pal();
    let t = SockTimeout::Blocking(Duration::from_secs(1));
    match id {
        "unix_refused" => {
            let path = dir.join("refused.sock");
            drop(UnixListener::bind(&path).unwrap()); // 留下未監聽的 socket 檔
            p.rpc
                .connect(&EndpointSpec::Unix(path.into_os_string()), t)
                .map(|_| ())
        }
        "unix_not_socket" => {
            let path = dir.join("plain.txt");
            std::fs::write(&path, b"x").unwrap();
            p.rpc
                .connect(&EndpointSpec::Unix(path.into_os_string()), t)
                .map(|_| ())
        }
        "unix_eacces" => {
            let locked = dir.join("locked");
            std::fs::create_dir(&locked).unwrap();
            let sock = locked.join("s.sock");
            drop(UnixListener::bind(&sock).unwrap());
            std::fs::set_permissions(&locked, std::fs::Permissions::from_mode(0o000)).unwrap();
            let r = p
                .rpc
                .connect(&EndpointSpec::Unix(sock.into_os_string()), t)
                .map(|_| ());
            std::fs::set_permissions(&locked, std::fs::Permissions::from_mode(0o700)).unwrap();
            r
        }
        "unix_empty" => p
            .rpc
            .connect(&EndpointSpec::Unix(OsString::new()), t)
            .map(|_| ()),
        "unix_recv_nonblock" => {
            let path = dir.join("silent.sock");
            let _listener = UnixListener::bind(&path).unwrap(); // 不 accept
            // Python 基線為「blocking connect → settimeout(0) → recv」；此處以 NonBlocking connect 取得同一非阻塞 recv 語意。
            let mut s = p
                .rpc
                .connect(&EndpointSpec::Unix(path.into_os_string()), SockTimeout::NonBlocking)?;
            s.recv(&mut [0u8; 16]).map(|_| ())
        }
        other => panic!("未知 fixture case：{other}"),
    }
}

#[cfg(unix)]
#[test]
fn unix_fixture_rows_match_python() {
    let mut fails = Vec::new();
    for id in [
        "unix_refused",
        "unix_not_socket",
        "unix_eacces",
        "unix_empty",
        "unix_recv_nonblock",
    ] {
        let g = golden_case(id);
        let d = tempfile::tempdir().unwrap();
        match unix_case_result(id, d.path()) {
            Ok(()) => fails.push(format!("{id}: golden 為 {} 但實作成功", g.exc)),
            Err(e) => {
                if (e.py_message.as_str(), e.os_code) != (g.message.as_str(), g.errno) {
                    fails.push(format!(
                        "{id}:\n  got  {:?} {:?}\n  want {:?} {:?}",
                        e.py_message, e.os_code, g.message, g.errno
                    ));
                }
            }
        }
    }
    assert!(fails.is_empty(), "{}", fails.join("\n"));
}
