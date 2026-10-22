//! `serialwrapd` daemon 本體組裝（arch §6.1、計畫 P5-T19）。
//!
//! 本檔目前只含 P5-T19 紅燈階段的介面骨架：函式本體為 `s21_todo!`（即 `unimplemented!`），
//! 讓整合測試可編譯並於執行期以「功能未實作」失敗。啟動順序、有序關機與 exit code 由實作補上。

use std::path::PathBuf;

use sw_pal::traits::{EndpointSpec, FsOps, ListenOptions, PalResult, SysEnv};
use sw_pal::{Capabilities, Platform};
use sw_proto::config_types::{ProfileTemplate, RuntimeConfig};
use sw_proto::endpoint::Endpoint;
use sw_proto::runtime_paths::{EnvSnapshot, RuntimePaths};

pub use crate::service::TestAllowlist;

/// `parse_args` 的結果（Task 19 Produces）。
#[derive(Clone, Debug, PartialEq, Eq)]
pub struct DaemonArgs {
    pub profile_dir: PathBuf,
    pub socket: String,
    pub lock: PathBuf,
}

/// 解析 daemon 的 argv（Task 19 Produces）；錯誤回 `(exit code, stderr 訊息)`。
pub fn parse_args(_argv: &[String], _paths: &RuntimePaths) -> Result<DaemonArgs, (i32, String)> {
    sw_core::s21_todo!("T19", "parse_args")
}

/// `SERIALWRAP_TEST_DEVICE_ALLOWLIST`（env 優先）與 `windows.test_device_allowlist`（config）的解析（SC-19，P1 T13 規格 1～3 逐字）。
pub fn resolve_test_allowlist(
    _env: &dyn SysEnv,
    _cfg: &RuntimeConfig,
    _defaults: &RuntimePaths,
) -> TestAllowlist {
    sw_core::s21_todo!("T19", "resolve_test_allowlist")
}

/// SC-09：config 讀取路徑（直接委派 P3 `resolve_config_path`；`SERIALWRAP_CONFIG` 原樣勝出）。
#[allow(dead_code)] // Task 19 實作前尚未被呼叫。
fn config_read_path(_env: &EnvSnapshot, _paths: &RuntimePaths) -> Option<String> {
    sw_core::s21_todo!("T19", "config_read_path")
}

/// SC-09：config 寫入路徑（`SERIALWRAP_CONFIG` > `CONFIG_DIR/config.yaml`；永不寫系統檔）。
#[allow(dead_code)] // Task 19 實作前尚未被呼叫。
fn config_write_path(_env: &EnvSnapshot, _paths: &RuntimePaths) -> String {
    sw_core::s21_todo!("T19", "config_write_path")
}

/// sw-proto → sw-pal 的唯一端點轉換點（sw-pal 不依賴 sw-proto）。
#[allow(dead_code)] // Task 19 實作前尚未被呼叫。
fn to_endpoint_spec(_e: &Endpoint) -> EndpointSpec {
    sw_core::s21_todo!("T19", "to_endpoint_spec")
}

/// 解析 `RuntimePaths`（GAP-X1：`SERIALWRAP_TCP_PORT` 非整數 → `Err`）。
#[allow(dead_code)] // Task 19 實作前尚未被呼叫。
fn resolve_paths(_p: &Platform) -> Result<RuntimePaths, String> {
    sw_core::s21_todo!("T19", "resolve_paths")
}

/// `SERIALWRAP_{RPC,LOCK,DEVICE}_BACKEND` 合法性檢查（非法值或非本平台後端 → `Err`）。
#[allow(dead_code)] // Task 19 實作前尚未被呼叫。
fn check_backend_env(_env: &dyn SysEnv, _caps: &Capabilities) -> Result<(), String> {
    sw_core::s21_todo!("T19", "check_backend_env")
}

/// CONFIG／STATE／RUN／WAL／EVENTS／EVENTS_RUNTIME 目錄建立。
#[allow(dead_code)] // Task 19 實作前尚未被呼叫。
fn ensure_runtime_dirs(_paths: &RuntimePaths, _fs: &dyn FsOps) -> PalResult<()> {
    sw_core::s21_todo!("T19", "ensure_runtime_dirs")
}

/// Windows 原生：無 passthrough template 時追加 `windows-default-passthrough`（同受 SC-35）。
#[allow(dead_code)] // Task 19 實作前尚未被呼叫。
fn inject_windows_passthrough(_templates: &mut Vec<ProfileTemplate>) {
    sw_core::s21_todo!("T19", "inject_windows_passthrough")
}

/// RPC listener 選項（基線 SC-08 撤回：mode `0o660`、`ensure_dir_mode` None；`SERIALWRAP_SOCKET_GROUP` 非空 → group）。
#[allow(dead_code)] // Task 19 實作前尚未被呼叫。
fn listen_options(_env: &dyn SysEnv) -> ListenOptions {
    sw_core::s21_todo!("T19", "listen_options")
}

/// 產品入口：解析 argv、組裝 daemon、回傳 exit code（0／1／2／3）。
pub fn run(argv: Vec<String>, platform: Platform) -> i32 {
    let _ = (argv, platform);
    sw_core::s21_todo!("T19", "run")
}

/// 啟動／停機順序的觀測事件（僅 `test-fakes` 編譯；產品不產生副作用）。
///
/// 計畫原列的變體表缺 `ShutdownGate`，但 `sigterm_graceful_exit_0_and_flushes_queue` 的停機序列斷言需要它
/// （stopping gate 先於 watcher 停止，R1-F2），故於此補上。
#[cfg(feature = "test-fakes")]
#[derive(Debug, Clone, PartialEq)]
pub enum TraceEvent {
    InitialScan,
    Rank,
    UpdateDevices,
    BootstrapAttach,
    WatcherStart,
    Bound,
    EndpointWritten,
    ServerStopped,
    ShutdownGate,
    WatcherStopped,
    SessionsDrained {
        flushed: Vec<(String, String)>,
        stalled: Vec<String>,
    },
    WalShutdown {
        ok: bool,
    },
    LockReleased,
}

/// 測試掛鉤（僅 `test-fakes` 編譯）。兩個 commit hook 經 `StateStore::spawn_with_hooks` 交給 store thread。
#[cfg(feature = "test-fakes")]
pub struct DaemonHooks {
    pub before_state_commit: Option<Box<dyn Fn() + Send + Sync>>,
    pub after_state_commit: Option<Box<dyn Fn() + Send + Sync>>,
    pub trace: Option<Box<dyn Fn(TraceEvent) + Send + Sync>>,
}

/// in-process 執行 daemon（僅 `test-fakes` 編譯）；語意與 `run` 相同，另接受測試掛鉤。
#[cfg(feature = "test-fakes")]
pub fn run_with_hooks(argv: Vec<String>, platform: Platform, hooks: DaemonHooks) -> i32 {
    let _ = (argv, platform, hooks);
    sw_core::s21_todo!("T19", "run_with_hooks")
}
