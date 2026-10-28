//! P5-T17：原生 RPC listener（arch §4.6）。UDS 權限維持 Python 基線 0660（SC-08 撤回，OD-14 #3）；
//! TCP 只允許 loopback（訊息逐字照 `sw_core/rpc_win.py`）。parity #99：既有 Python `JsonRpcUnixServer`
//! 行為（0660、`SERIALWRAP_SOCKET_GROUP` chgrp 失敗忽略、unlink 舊檔、stop 清 socket）以本檔 `uds_*` 承接。

use std::io::{Read, Write};
use std::net::TcpStream;
use std::time::{Duration, Instant};

use sw_pal::traits::{EndpointSpec, ListenOptions, RpcListener, RpcServerStream};

fn baseline(group: Option<&str>) -> ListenOptions {
    ListenOptions {
        mode: 0o660,
        group: group.map(str::to_string),
        ensure_dir_mode: None,
    }
}

fn accept_within(listener: &mut dyn RpcListener, total: Duration) -> Box<dyn RpcServerStream> {
    let deadline = Instant::now() + total;
    loop {
        if let Some(s) = listener.accept(Duration::from_millis(50)).expect("accept") {
            return s;
        }
        assert!(Instant::now() < deadline, "accept 逾時");
    }
}

#[cfg(feature = "test-fakes")]
#[test]
fn rpc_listener_conformance_suite() {
    let d = tempfile::tempdir().unwrap();
    let p = sw_pal::native();
    sw_pal::conformance::rpc_suite(p.rpc_listen, p.rpc, d.path());
}

#[test]
fn tcp_non_loopback_rejected_verbatim() {
    let f = sw_pal::native().rpc_listen;
    let ep = EndpointSpec::Tcp {
        host: "0.0.0.0".into(),
        port: 0,
    };
    match f.bind(&ep, &baseline(None)) {
        Ok(_) => panic!("0.0.0.0 不得 bind"),
        Err(e) => assert_eq!(
            e.py_message,
            "RPC server 僅允許 loopback 位址（127.0.0.1/localhost/::1），拒絕 '0.0.0.0' 避免遠端暴露（RPC 無認證，非 loopback 會讓遠端呼叫所有命令）"
        ),
    }
}

#[test]
fn tcp_loopback_accepts() {
    let port = std::net::TcpListener::bind("127.0.0.1:0")
        .unwrap()
        .local_addr()
        .unwrap()
        .port();
    let ep = EndpointSpec::Tcp {
        host: "127.0.0.1".into(),
        port,
    };
    let mut l = sw_pal::native()
        .rpc_listen
        .bind(&ep, &baseline(None))
        .expect("bind 127.0.0.1");
    let mut c = TcpStream::connect(("127.0.0.1", port)).unwrap();
    c.set_read_timeout(Some(Duration::from_secs(5))).unwrap();
    let mut s = accept_within(l.as_mut(), Duration::from_secs(5));
    c.write_all(b"ping\n").unwrap();
    s.set_read_timeout(Some(Duration::from_secs(5))).unwrap();
    let mut got = [0u8; 5];
    s.read_exact(&mut got).unwrap();
    assert_eq!(&got, b"ping\n");
    s.write_all(b"pong\n").unwrap();
    let mut back = [0u8; 5];
    c.read_exact(&mut back).unwrap();
    assert_eq!(&back, b"pong\n");
    l.close();
}

#[cfg(unix)]
use std::os::unix::fs::{MetadataExt, PermissionsExt};
#[cfg(unix)]
use std::os::unix::net::UnixStream;
#[cfg(unix)]
use std::path::Path;

#[cfg(unix)]
fn mode(p: &Path) -> u32 {
    std::fs::metadata(p).unwrap().permissions().mode() & 0o777
}

#[cfg(unix)]
fn bind(path: &Path, group: Option<&str>) -> Box<dyn RpcListener> {
    sw_pal::native()
        .rpc_listen
        .bind(
            &EndpointSpec::Unix(path.as_os_str().to_os_string()),
            &baseline(group),
        )
        .expect("bind uds")
}

#[cfg(unix)]
/// `/etc/group` 中名稱對應的 gid。
fn group_gid(name: &str) -> Option<u32> {
    let text = std::fs::read_to_string("/etc/group").ok()?;
    text.lines().find_map(|l| {
        let mut f = l.split(':');
        (f.next()? == name).then(|| f.nth(1)?.parse().ok())?
    })
}

#[cfg(unix)]
/// 可 chgrp、且與 socket 建立時群組（目前主要群組）不同的既有群組，使 chgrp 可觀察（審查 F07）：
/// root 下選 `daemon`；非 root 選第一個不同於主要群組的附加群組。皆無 → 證據不足即 FAIL（不得以主要群組
/// 充數——拿掉 chgrp 實作時測試仍會綠）。
fn usable_group() -> (String, u32) {
    let out = |args: &[&str]| {
        String::from_utf8(
            std::process::Command::new("id")
                .args(args)
                .output()
                .unwrap()
                .stdout,
        )
        .unwrap()
        .trim()
        .to_string()
    };
    let primary: u32 = out(&["-g"]).parse().unwrap();
    if out(&["-u"]) == "0" {
        if let Some(gid) = group_gid("daemon") {
            if gid != primary {
                return ("daemon".into(), gid);
            }
        }
    }
    let gids = out(&["-G"]);
    let names = out(&["-Gn"]);
    gids.split_whitespace()
        .zip(names.split_whitespace())
        .find_map(|(gid, name)| {
            let gid: u32 = gid.parse().ok()?;
            (gid != primary).then(|| (name.to_string(), gid))
        })
        .expect("證據不足：找不到與主要群組不同、可 chgrp 的附加群組")
}

#[cfg(unix)]
/// 取代 `uds_mode_0600_and_dir_0700`（SC-08 撤回，OD-14 #3）：0660、目錄權限不被修改、chgrp 失敗忽略。
#[test]
fn uds_mode_0660_baseline() {
    let d = tempfile::tempdir().unwrap();
    let dir = d.path().join("run");
    std::fs::create_dir(&dir).unwrap();
    std::fs::set_permissions(&dir, std::fs::Permissions::from_mode(0o755)).unwrap();
    let before = std::fs::metadata(&dir).unwrap().mode();

    let p = dir.join("a.sock");
    let l = bind(&p, None);
    assert_eq!(mode(&p), 0o660);
    assert_eq!(
        std::fs::metadata(&dir).unwrap().mode(),
        before,
        "目錄權限不得被 listener 修改"
    );
    l.close();

    let (name, gid) = usable_group();
    let p = dir.join("b.sock");
    let l = bind(&p, Some(&name));
    assert_eq!(mode(&p), 0o660);
    assert_eq!(std::fs::metadata(&p).unwrap().gid(), gid, "chgrp 至 {name}");
    l.close();

    let p = dir.join("c.sock");
    let l = bind(&p, Some("no-such-group"));
    assert_eq!(mode(&p), 0o660);
    let c = UnixStream::connect(&p);
    assert!(c.is_ok(), "chgrp 失敗被忽略、bind 成功");
    l.close();
    assert_eq!(std::fs::metadata(&dir).unwrap().mode(), before);
}

#[cfg(unix)]
/// ensure_dir_mode=None 時不建立父目錄（Python 基線：bind 直接失敗）。
#[test]
fn uds_missing_parent_not_created() {
    let d = tempfile::tempdir().unwrap();
    let p = d.path().join("missing/a.sock");
    let r = sw_pal::native().rpc_listen.bind(
        &EndpointSpec::Unix(p.as_os_str().to_os_string()),
        &baseline(None),
    );
    assert!(r.is_err());
    assert!(!d.path().join("missing").exists());
}

#[cfg(unix)]
#[test]
fn uds_existing_file_unlinked() {
    let d = tempfile::tempdir().unwrap();
    let p = d.path().join("s.sock");
    std::fs::write(&p, b"stale regular file").unwrap();
    let mut l = bind(&p, None);
    let mut c = UnixStream::connect(&p).expect("connect 新 socket");
    let mut s = accept_within(l.as_mut(), Duration::from_secs(5));
    c.write_all(b"x\n").unwrap();
    let mut b = [0u8; 2];
    s.read_exact(&mut b).unwrap();
    assert_eq!(&b, b"x\n");
    l.close();
}

#[cfg(unix)]
#[test]
fn uds_existing_socket_unlinked() {
    let d = tempfile::tempdir().unwrap();
    let p = d.path().join("s.sock");
    let old = std::os::unix::net::UnixListener::bind(&p).unwrap();
    drop(old); // 殘留 stale socket 檔
    assert!(p.exists());
    let l = bind(&p, None);
    assert!(UnixStream::connect(&p).is_ok());
    l.close();
}

#[cfg(unix)]
#[test]
fn uds_close_removes_socket() {
    let d = tempfile::tempdir().unwrap();
    let p = d.path().join("s.sock");
    let l = bind(&p, None);
    assert!(p.exists());
    l.close();
    assert!(!p.exists());
}

#[cfg(unix)]
#[test]
fn uds_accept_is_bounded() {
    let d = tempfile::tempdir().unwrap();
    let p = d.path().join("s.sock");
    let mut l = bind(&p, None);
    let t0 = Instant::now();
    assert!(l.accept(Duration::from_millis(100)).unwrap().is_none());
    let e = t0.elapsed();
    assert!(
        e >= Duration::from_millis(90) && e < Duration::from_secs(2),
        "{e:?}"
    );
    l.close();
}
