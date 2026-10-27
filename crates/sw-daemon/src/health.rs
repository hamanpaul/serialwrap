//! `health.ping`／`health.status` 回應組裝（arch §6.3、02 §7.1 key 順序；P5-T18）。
//!
//! 對照 Python `SerialwrapService.health()`：
//! `ok, pid, version, running, started_at, sessions, devices, commands, wal_path, mirror_path, wal, limits,
//! [warnings], multi_open, foreign_holders, multi_open_detail, [test_device_allowlist]`。
//! `multi_open` 系列對照 `multi_open.detect_multi_open`（#101）與 `serial_port.unknown_holder_devices()`（#11）。

use std::path::PathBuf;

use sw_pal::traits::HolderScanStatus;
use sw_proto::constants::{CMD_REJECT_BYTES, CMD_WARN_BYTES};
use sw_proto::pydict;
use sw_proto::pyjson::{PyDict, PyValue};

use crate::service::{Service, int};

/// `holders_status` 字串（Python 僅 `ok`／`permission`／`unknown`；`unsupported` 為 Windows PAL 專屬）。
fn status_str(s: HolderScanStatus) -> &'static str {
    match s {
        HolderScanStatus::Ok => "ok",
        HolderScanStatus::Permission => "permission",
        HolderScanStatus::Unknown => "unknown",
        HolderScanStatus::Unsupported => "unsupported",
    }
}

/// 多次掃描的狀態合併：`permission` 優先於 `unknown`（Python：權限錯誤一律改寫，其餘只在 ok 時降級）。
fn status_rank(s: HolderScanStatus) -> u8 {
    match s {
        HolderScanStatus::Ok => 0,
        HolderScanStatus::Unknown => 1,
        HolderScanStatus::Unsupported => 2,
        HolderScanStatus::Permission => 3,
    }
}

/// `detect_multi_open` 等價：`(multi_open, daemons, holders{tty: pid}, holders_status)`。
fn detect_multi_open(svc: &Service, tty_paths: &[PathBuf]) -> (bool, Vec<PyValue>, PyDict, &'static str) {
    let Ok(mut daemons) = svc.procs.serialwrapd_instances() else {
        return (false, Vec::new(), PyDict::new(), "unknown");
    };
    daemons.sort_by_key(|d| d.pid);
    let multi_open = daemons.len() > 1;
    let rows: Vec<PyValue> = daemons
        .iter()
        .map(|d| {
            PyValue::Dict(pydict! {
                "pid" => i64::from(d.pid),
                "socket" => d.socket.clone().map(PyValue::from).unwrap_or(PyValue::Null),
            })
        })
        .collect();
    let mut holders = PyDict::new();
    let mut status = HolderScanStatus::Ok;
    let mut merge = |st: HolderScanStatus| {
        if status_rank(st) > status_rank(status) {
            status = st;
        }
    };
    if tty_paths.is_empty() {
        let (_, st) = svc.procs.serialwrapd_tty_holders(&[]);
        merge(st);
    }
    for tty in tty_paths {
        let (pids, st) = svc.procs.serialwrapd_tty_holders(std::slice::from_ref(tty));
        merge(st);
        // Python `holders[tty] = pid`：同一 tty 多個持有者時保留最後一個（pid 升冪掃描）。
        if let Some(pid) = pids.iter().max() {
            holders.insert(&tty.to_string_lossy(), int(i128::from(*pid)));
        }
    }
    (multi_open, rows, holders, status_str(status))
}

/// `health.status` 回應（key 順序照 02 §7.1；含 OD-11 allowlist 顯示）。
pub fn health(svc: &Service) -> PyDict {
    let wal_health = svc.wal.health();
    let sessions = svc.manager.list_sessions();
    let devices = svc.manager.list_devices();
    let mut warnings: Vec<PyValue> = Vec::new();
    if svc.profile_count == 0 {
        warnings.push(PyValue::from("no_profiles_loaded"));
    }
    if devices.is_empty() {
        warnings.push(PyValue::from("no_devices_found"));
    }
    let mut result = pydict! {
        "ok" => true,
        "pid" => i64::from(svc.pid),
        "version" => svc.version.clone(),
        "running" => svc.is_running(),
        "started_at" => svc.started_at.clone(),
        "sessions" => int(sessions.len() as i128),
        "devices" => int(devices.len() as i128),
        "commands" => int(svc.registry.len() as i128),
        "wal_path" => svc.wal.wal_path().to_string_lossy().into_owned(),
        "mirror_path" => svc.wal.mirror_path().to_string_lossy().into_owned(),
        "wal" => wal_health.to_pydict(),
        "limits" => pydict! {
            "max_submit_cmd_bytes" => CMD_REJECT_BYTES,
            "warn_submit_cmd_bytes" => CMD_WARN_BYTES,
            "reject_error_code" => "CMD_TOO_LONG",
            "newline_error_code" => "CMD_CONTAINS_NEWLINE",
            "warning_code" => "CMD_LENGTH_WARNING",
            "newline_forbidden" => true,
        },
    };
    if !warnings.is_empty() {
        result.insert("warnings", PyValue::List(warnings));
    }

    let tty_paths = Service::attached_paths(&sessions);
    let (multi_open, daemons, mut foreign_holders, holders_status) = detect_multi_open(svc, &tty_paths);
    // #11：鎖後 holder 掃描不完整的裝置以物件回報 `{device, holder_unknown, pids?}`，其餘仍為 path→pid。
    for device in sw_pal::serial::unknown_holder_devices() {
        let mut entry = pydict! { "device" => device.clone(), "holder_unknown" => true };
        if let Some(pid) = foreign_holders.get(&device).cloned() {
            entry.insert("pids", PyValue::List(vec![pid]));
        }
        foreign_holders.insert(&device, PyValue::Dict(entry));
    }
    result.insert("multi_open", PyValue::Bool(multi_open));
    result.insert("foreign_holders", PyValue::Dict(foreign_holders));
    result.insert(
        "multi_open_detail",
        PyValue::Dict(pydict! { "daemons" => daemons, "holders_status" => holders_status }),
    );
    if let Some(allow) = &svc.allowlist {
        // SC-19：啟用時必須顯示（OD-11）；未啟用時不輸出此鍵。
        result.insert(
            "test_device_allowlist",
            PyValue::Dict(pydict! {
                "enabled" => true,
                "coms" => allow.coms.iter().cloned().map(PyValue::from).collect::<Vec<_>>(),
                "source" => allow.source,
            }),
        );
    }
    result
}
