//! Slice 2.1 前置交付檢查（計畫 06 §0.3 P3／P4 列）：逐符號 `use` 與型別斷言。
//!
//! 任一符號缺漏即編譯失敗，表示上游 P3／P4 前置未到位（屬上游缺陷，應停止並回報，不在本 Phase 自行補）。
//! 「本 Phase 補齊」（Task 0c）的符號不在本檔，見 `crates/sw-proto/tests/slice21_additions.rs`。
//! P5 Task 0 雙審修正：補齊 §0.3 列出的 FsOps／RuntimeConfig／RuntimePaths／Clock／RpcStream／EndpointSpec／PalError 符號。
#![allow(unused_imports, dead_code)]

use std::path::{Path, PathBuf};
use std::time::Duration;

use sw_proto::{constants, pyjson::{self, PyDict, PyValue, PyInt, DumpOpts, Sep, loads, dumps, CLI_STDOUT, RPC_WIRE, RPC_EARLY, DISK_STABLE}};
use sw_proto::pycompat::{py_int_parse, py_float_parse, py_strip, py_bytes_strip, splitlines, universal_newlines,
                         decode_utf8_replace, decode_utf8_strict, py_len_chars, py_truthy, PyOsError, sha256_hex};
use sw_proto::pyfmt::{py_str, py_repr_str, py_repr_float};
use sw_proto::pyre::{compile, PyFlags, PyRegex, valid_completion_regex};
use sw_proto::config_types::{load_profiles, ProfileFs, LoadResult, ProfileTemplate, SessionProfile, UartProfile, RuntimeConfig};
use sw_proto::runtime_paths::{RuntimePaths, EnvSnapshot, NativeTraits, PathsError, resolve_config_path};
use sw_proto::endpoint::{Endpoint, EndpointError, parse_endpoint, is_loopback_tcp};
use sw_proto::{pydict, rpc_types::Method, version::{version, version_line}, error_codes::ErrorCode, session_last_errors::SessionLastError};
use sw_pal::traits::{PalError, PalErrorKind, PalResult, SysEnv, Clock, EndpointSpec, RpcConnector, RpcStream, FileLockGuard};
use sw_pal::proc_scan::{DaemonProc, HolderScanStatus, ScanFailure};
use sw_pal::{FsOps, FsRead, ProcScan, ProcessCtl};

// §0.3 P4：方法存在性以「型別標註的閉包」鎖定簽章（閉包只做型別檢查，不執行）。
#[allow(clippy::type_complexity)]
fn _p4_signatures() {
    // ProcScan：計畫 §0.3 寫 PalResult<Vec<DaemonProc>>；P4 實際為 Result<Vec<DaemonProc>, ScanFailure>（sw-pal/src/proc_scan/mod.rs），以實際型別鎖定。
    let _ = |p: &dyn ProcScan| -> Result<Vec<DaemonProc>, ScanFailure> { p.serialwrapd_instances() };
    let _ = |p: &dyn ProcScan, t: &[PathBuf]| -> (Vec<u32>, HolderScanStatus) { p.serialwrapd_tty_holders(t) };
    let _ = |p: &dyn ProcScan, pid: u32| -> bool { p.pid_alive(pid) };
    // FsOps（P4 §0.3）：atomic_write(p, data, mode: Option<u32>) 與其餘七個方法。
    let _ = |fs: &dyn FsOps, p: &Path, d: &[u8], m: Option<u32>| -> PalResult<()> { fs.atomic_write(p, d, m) };
    let _ = |fs: &dyn FsOps, p: &Path, d: &[u8]| -> PalResult<()> { fs.write_file(p, d) };
    let _ = |fs: &dyn FsOps, p: &Path| -> PalResult<()> { fs.mkdirs(p) };
    let _ = |fs: &dyn FsOps, from: &Path, to: &Path| -> PalResult<()> { fs.replace(from, to) };
    let _ = |fs: &dyn FsOps, p: &Path| -> PalResult<()> { fs.remove_file(p) };
    let _ = |fs: &dyn FsOps, p: &Path| -> PalResult<()> { fs.sync_dir(p) };
    let _ = |fs: &dyn FsOps, p: &Path, mode: u32| -> PalResult<()> { fs.set_mode(p, mode) };
    let _ = |fs: &dyn FsOps, p: &Path, blocking: bool| -> PalResult<FileLockGuard> { fs.file_lock_exclusive(p, blocking) };
    // RuntimeConfig（P4 T1 寫入路徑）：set_mode／set_socket／to_yaml
    let _ = |c: &mut RuntimeConfig, mode: &str, sock: Option<&str>| c.set_mode(mode, sock);
    let _ = |c: &mut RuntimeConfig, ep: &str| c.set_socket(ep);
    let _ = |c: &RuntimeConfig| -> String { c.to_yaml() };
    // HolderScanStatus 變體
    let _ = [HolderScanStatus::Ok, HolderScanStatus::Permission, HolderScanStatus::Unknown, HolderScanStatus::Unsupported];
    // sw_pal::native() -> Platform（P4 組裝點）
    let _: fn() -> sw_pal::Platform = sw_pal::native;
}

// §0.3 P3：Clock／RpcStream／RuntimeConfig 讀取面、PalError 與 EndpointSpec 形狀。
#[allow(dead_code)]
fn _p3_signatures() {
    let _ = |c: &dyn Clock| -> u64 { c.mono_ns() };
    let _ = |c: &dyn Clock| c.sleep(Duration::ZERO);
    let _ = |s: &mut dyn RpcStream, d: &[u8]| -> PalResult<()> { s.send_all(d) };
    let _ = |s: &mut dyn RpcStream, b: &mut [u8]| -> PalResult<usize> { s.recv(b) };
    let _ = |t: Option<&str>| RuntimeConfig::from_text(t);
    let _ = |c: &RuntimeConfig| {
        let _ = c.mode();
        let _ = c.socket_path();
        let _ = &c.data;
    };
    // RuntimePaths::resolve(env, native, home)：fn 項目引用即可鎖定名稱存在。
    let _ = RuntimePaths::resolve;
    let _ = load_profiles;
    let _ = parse_endpoint;
    let _ = is_loopback_tcp;
    let _ = |e: PalError| {
        let _ = (e.kind, e.os_code, e.winerror, e.strerror, e.py_message);
    };
    let _ = |e: EndpointSpec| match e {
        EndpointSpec::Unix(_path) => {}
        EndpointSpec::Tcp { host: _host, port: _port } => {}
    };
}

// 欄位型別斷言（閉包只做型別檢查，不執行）。
#[allow(dead_code)]
fn _field_shapes() {
    let _ = |r: LoadResult| {
        let _ = (r.profiles, r.templates, r.max_sessions);
    };
    let _ = |s: SessionProfile| {
        let _ = (s.com, s.act_no, s.alias, s.device_by_id, s.template);
    };
    let _ = |p: RuntimePaths| {
        let _ = (p.config_dir, p.state_dir, p.run_dir, p.data_dir, p.lock_path, p.socket_path,
                 p.default_tcp_port, p.default_endpoint, p.profile_dir, p.wal_dir, p.log_dir, p.config_yaml);
    };
    let _ = |e: Endpoint| match e {
        Endpoint::Unix(_path) => {}
        Endpoint::Tcp { host: _host, port: _port } => {}
    };
    let _ = |fs: &dyn FsOps, p: &Path| fs.atomic_write(p, b"", Some(0o600));
}

#[test]
fn prereq_symbols_exist() {
    assert_eq!(constants::CMD_PENDING_MAX, 256);
    assert_eq!(constants::DEFAULT_WAL_ROTATE_BYTES, 67_108_864);
    // Task 8／passthrough 常數（P5 Task 0 雙審修正：補入 sw_proto::constants）。
    assert_eq!(constants::CMD_HISTORY_MAX, 512);
    assert_eq!(constants::PASSTHROUGH_READY_QUIET_S, 30.0);
    assert!(!version().is_empty());
    assert_eq!(pyjson::dumps(&PyValue::Dict(PyDict::new()), &DISK_STABLE), "{}");
    assert!(valid_completion_regex(None));
    let _ = sw_core::SLICE;
}
