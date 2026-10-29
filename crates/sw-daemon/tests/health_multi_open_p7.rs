//! rulings/p7.md P7-71fe017-4：`health.status` 的 `multi_open_detail.holders_status` 走 Python
//! `multi_open.detect_multi_open` 語意（已 attach 的 tty 取不到身分 → 略過該 tty、status 維持 `ok`），
//! 不得沿用 rollback 的 fail-closed 掃描（那會回 `unknown`）。
//!
//! 以真實 `PosixProcScan`（暫存 proc root；POSIX-only）組裝 `Service`，正式呼叫路徑為 `Service::rpc("health.status")`
//! → `health::detect_multi_open` → `ProcScan::multi_open_tty_holders`；若 health 改回呼叫
//! `serialwrapd_tty_holders`，本測試即紅（`unknown != ok`）。
#![cfg(feature = "test-fakes")]
mod support;

use std::sync::Arc;

use support::{REAL_A, RIG_PID, ServiceRig};
use sw_daemon::service::{Service, ServiceDeps};
use sw_pal::proc_scan::PosixProcScan;
use sw_proto::pyjson::{PyDict, PyValue, RPC_WIRE, dumps};

fn sub(d: &PyDict, k: &str) -> PyDict {
    match d.get(k) {
        Some(PyValue::Dict(x)) => x.clone(),
        other => panic!("{k} 應為 object：{other:?}"),
    }
}

/// 平台以路徑分隔字元判定（測試不得以 cfg 分辨平台，arch §2.1）。
fn on_windows() -> bool {
    std::path::MAIN_SEPARATOR == '\\'
}

#[test]
fn health_status_vanished_tty_holders_status_ok_like_detect_multi_open() {
    // `/proc` 掃描與 inode 比對僅 POSIX；本案只登記於 L1 linux 集合，Windows 不計入 gate。
    if on_windows() {
        eprintln!("POSIX-only：Windows 無 /proc，本案不適用（未登記 windows 集合）");
        return;
    }
    let rig = ServiceRig::ready_shell();
    // 暫存 proc root：唯一的 serialwrapd（pid＝RIG_PID，無 --socket）、fd 目錄可讀且為空。
    let proc_root = tempfile::tempdir().unwrap();
    let pid_dir = proc_root.path().join(RIG_PID.to_string());
    std::fs::create_dir_all(pid_dir.join("fd")).unwrap();
    std::fs::write(pid_dir.join("cmdline"), b"serialwrapd\0").unwrap();
    // 前提：session 已 attach 的實體路徑在檔案系統上不存在（Python `tty_stat` 回 None）。
    assert!(std::fs::metadata(REAL_A).is_err(), "{REAL_A} 不應存在");

    let svc = Arc::new(Service::new(ServiceDeps {
        manager: rig.mgr.clone(),
        wal: rig.wal.clone(),
        registry: rig.registry.clone(),
        procs: Arc::new(PosixProcScan::with_root(proc_root.path())),
        version: rig.version.clone(),
        started_at: "2026-10-29T00:00:00+00:00".to_string(),
        explicit_target_count: 1,
        test_device_allowlist: None,
        pid: RIG_PID,
    }));
    let h = svc.rpc("health.status", &PyDict::new()).expect("health.status 應回字典");
    let detail = sub(&h, "multi_open_detail");
    assert_eq!(
        dumps(&PyValue::Dict(detail), &RPC_WIRE),
        format!("{{\"daemons\":[{{\"pid\":{RIG_PID},\"socket\":null}}],\"holders_status\":\"ok\"}}"),
        "Python detect_multi_open：tty stat 失敗即略過，holders_status 維持 ok"
    );
    assert_eq!(h.get("multi_open"), Some(&PyValue::Bool(false)));
}
