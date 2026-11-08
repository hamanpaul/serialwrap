//! sw-cli 測試替身支援模組（support）自測（P4-T1）。
//!
//! 每個自測只驗替身行為本身（不對產品骨架主體寫 should_panic）。審查修正回歸：事件跨來源合併、pid 與 ping 同輪翻轉、
//! 未配置元件的命名 panic、endpoint 不合併、fs 真實操作。
mod support;


use std::time::Duration;

use sw_pal::traits::{EndpointSpec, ProcessCtl, RpcConnector, SockTimeout};
use sw_pal::ProcScan;
use support::*;

const OK_LIST: &str = r#"{"ok":true,"sessions":[]}"#;

fn ping_ok(t: &TestEnv, ep: &str) -> bool {
    let spec = EndpointSpec::Unix(std::ffi::OsString::from(ep.trim_start_matches("unix://")));
    t.rpc()
        .connect(&spec, SockTimeout::Blocking(Duration::from_millis(500)))
        .is_ok()
}

/// 事件依全程單調序號合併：process spawn（`spawn:<argv0>`）先於 CLI 的 RPC（`rpc:session.list`）。
#[test]
fn event_order_merges_by_clock_then_sequence() {
    let base = TestEnv::posix().with_process();
    let sock = base.default_socket();
    let env = base.with_rpc_ok(&sock, "session.list", OK_LIST);
    env.process()
        .spawn_daemon(&["/opt/sw/serialwrapd".to_string()], &[], true)
        .expect("fake spawn");
    let (rc, _out, _err) = env.run(&["session", "list"]);
    assert_eq!(rc, 0, "session list 應成功");
    let order = env.event_order();
    let spawn = order.index_of("spawn:serialwrapd");
    let rpc = order.index_of("rpc:session.list");
    assert!(spawn < rpc, "spawn 必須早於其後的 RPC：spawn={spawn} rpc={rpc}");
}

/// pid 與 ping 在同一次輪詢翻轉（n=3）：前 2 次 ping 成功且 pid 存活，第 3 次起 ping refused 且 pid 不存活。
#[test]
fn daemon_alive_then_gone_flips_on_nth_poll() {
    let ep = "unix:///tmp/test.sock";
    let env = TestEnv::posix().with_daemon_alive_then_gone(ep, 1234, 3);
    let pid_alive = |t: &TestEnv| t.procs().pid_alive(1234);
    assert!(ping_ok(&env, ep), "第 1 次 ping 應成功");
    assert!(pid_alive(&env), "第 1 次後 pid 應存活");
    assert!(ping_ok(&env, ep), "第 2 次 ping 應成功");
    assert!(pid_alive(&env), "第 2 次後 pid 應存活");
    assert!(!ping_ok(&env, ep), "第 3 次 ping 應 refused");
    assert!(!pid_alive(&env), "第 3 次後 pid 必須同輪不存活");
    assert!(!env.procs().serialwrapd_instances().unwrap().iter().any(|d| d.pid == 1234));
}

/// 未配置的元件呼叫即 panic，訊息含元件名（不得落到真實 OS）。
#[test]
fn unconfigured_fake_panics_with_name() {
    let env = TestEnv::posix();
    let r = std::panic::catch_unwind(std::panic::AssertUnwindSafe(|| {
        let _ = env.service();
    }));
    let payload = r.expect_err("service 未配置時必須 panic");
    let msg = payload
        .downcast_ref::<String>()
        .cloned()
        .or_else(|| payload.downcast_ref::<&str>().map(|s| s.to_string()))
        .unwrap_or_default();
    assert!(msg.contains("fake component not configured: service"), "訊息為 {msg:?}");
    let r2 = std::panic::catch_unwind(std::panic::AssertUnwindSafe(|| {
        let _ = env.tunnel();
    }));
    assert!(r2.is_err(), "tunnel 預設即未配置");
}

/// 相異的 unix endpoint 不得合併；default_socket 映射到自身。
#[test]
fn isolated_endpoints_stay_distinct() {
    let env = TestEnv::posix();
    let a = env.isolated_endpoint("unix:///tmp/a.sock");
    let b = env.isolated_endpoint("unix:///tmp/b.sock");
    assert_ne!(a, b);
    assert_eq!(env.isolated_endpoint(&env.default_socket()), format!("unix://{}", env.default_socket()));
}

/// 隔離的 system config 目錄存在且位於 tmp（SC-09：不得穿透到宿主 /etc）。
#[test]
fn system_config_dir_is_isolated() {
    let env = TestEnv::posix();
    assert!(env.system_config_dir().is_dir());
    assert!(!env.system_config_dir().starts_with("/etc"));
}

/// 真實 fs 替身：原子寫入、rename、遞迴複製、移除目錄樹、symlink 讀取皆真實作用於 tmp。
#[test]
fn real_fs_fake_performs_real_operations() {
    let env = TestEnv::posix();
    let root = env.install_root().join("fs-selftest");
    let fs = sw_pal::fakes::real_fs();
    fs.mkdirs(&root.join("src/sub")).unwrap();
    fs.atomic_write(&root.join("src/a.txt"), b"alpha", Some(0o600)).unwrap();
    fs.copy_tree(&root.join("src"), &root.join("dst")).unwrap();
    assert_eq!(std::fs::read(root.join("dst/a.txt")).unwrap(), b"alpha");
    fs.replace(&root.join("dst/a.txt"), &root.join("dst/b.txt")).unwrap();
    assert!(root.join("dst/b.txt").exists() && !root.join("dst/a.txt").exists());
    fs.symlink_file(&root.join("link"), &root.join("dst/b.txt")).unwrap();
    assert_eq!(fs.read_link(&root.join("link")).unwrap(), Some(root.join("dst/b.txt")));
    fs.remove_tree(&root.join("dst")).unwrap();
    assert!(!root.join("dst").exists());
    let guard = fs.file_lock_exclusive(&root.join("lock"), false).unwrap();
    assert!(guard.is_held());
    let second = fs.file_lock_exclusive(&root.join("lock"), false);
    assert!(second.is_err(), "同一鎖檔第二次非阻塞取鎖必須失敗");
    drop(guard);
    assert!(fs.file_lock_exclusive(&root.join("lock"), false).is_ok(), "釋放後可再取得");
}

/// `with_daemon_alive_then_gone` 之外，spawn gate 的 refused → ok 轉換與 journal 讀取。
#[test]
fn spawn_gate_and_journal_reads() {
    let env = TestEnv::posix().with_rpc_after_spawn("unix:///x.sock", &["health.ping"]);
    assert!(!ping_ok(&env, "unix:///x.sock"), "spawn 之前必須 refused");
    env.process().spawn_daemon(&["serialwrapd".to_string()], &[], true).unwrap();
    assert!(ping_ok(&env, "unix:///x.sock"), "spawn 之後必須可連線");
    assert!(env.journal_json().is_none(), "無 journal 時回 None");
}
