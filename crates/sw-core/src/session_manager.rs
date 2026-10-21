//! P5-T16：`SessionManager`（裝置、rank #100、動態 session、selector、管理操作）。
//!
//! 紅燈階段骨架（plan 06 Task 16「Produces」）：型別與公開簽章依計畫凍結，函式本體為 `s21_todo!`，
//! 由 Task 16 實作者補上。全域 actor thread `serialwrap-session-manager` 與各 `SessionActor` 尚未建立。

use std::sync::Arc;
use std::time::Duration;

use crossbeam_channel::Sender;
use sw_pal::traits::{
    Clock, ConsoleTransportFactory, DeviceInfo, FsOps, SerialBackend, SysEnv, UartExclusive,
    WatcherEvent,
};
use sw_pal::Capabilities;
use sw_proto::config_types::LoadResult;
use sw_proto::pycompat::PyErr;
use sw_proto::pyjson::PyDict;

use crate::arbiter::CommandRegistry;
use crate::session_core::SessionRequest;
use crate::state_store::{LoadedState, StateStore};
use crate::wal::{WalHealthFanout, WalWriter};

/// `SessionManager::new` 的注入依賴（plan 06 Task 16 Produces）。
pub struct ManagerDeps {
    pub serial: Arc<dyn SerialBackend>,
    pub uart_excl: Arc<dyn UartExclusive>,
    pub console: Arc<dyn ConsoleTransportFactory>,
    pub caps: Capabilities,
    pub wal: Arc<WalWriter>,
    pub registry: Arc<CommandRegistry>,
    pub state: StateStore,
    pub loaded: LoadedState,
    pub load: LoadResult,
    pub clock: Arc<dyn Clock>,
    pub env: Arc<dyn SysEnv>,
    pub fs: Arc<dyn FsOps>,
    pub warn: Arc<dyn Fn(String) + Send + Sync>,
}

/// 動態 COM 排序鍵（#100）：`(by_id, by_path 或空字串)`。
pub fn device_sort_key(_by_id: &str, _by_path: Option<&str>) -> (String, String) {
    s21_todo!("T16", "device_sort_key")
}

/// 全域 session 管理者（plan 06 Task 16）。
pub struct SessionManager {
    _stub: (),
}

impl SessionManager {
    pub fn new(_deps: ManagerDeps) -> Arc<SessionManager> {
        s21_todo!("T16", "SessionManager::new")
    }

    /// DF-14：於 lock 內依 `device_sort_key` 預先排定動態 COM（在 watcher 啟動前呼叫）。
    pub fn prepare_dynamic_rank(&self, _by_ids: &[String]) {
        s21_todo!("T16", "SessionManager::prepare_dynamic_rank")
    }

    /// 裝置快照變更（added／removed／changed）→ actor 事件與動態 session 建立。
    pub fn update_devices(&self, _snapshot: &[(String, DeviceInfo)]) {
        s21_todo!("T16", "SessionManager::update_devices")
    }

    /// 啟動時依設定 attach 所有已存在的 session。
    pub fn bootstrap_attach(&self) {
        s21_todo!("T16", "SessionManager::bootstrap_attach")
    }

    /// selector → session_id（session_id 精確優先；否則依建立順序取第一個 `com == sel || alias == sel`）。
    pub fn resolve(&self, _selector: &str) -> Option<String> {
        s21_todo!("T16", "SessionManager::resolve")
    }

    /// 將 session 請求路由到對應 actor 並等待回覆。
    pub fn route(
        &self,
        _sid: &str,
        _req: SessionRequest,
        _timeout: Duration,
    ) -> Result<PyDict, PyErr> {
        s21_todo!("T16", "SessionManager::route")
    }

    /// 依 session_id 排序的 session 公開欄位。
    pub fn list_sessions(&self) -> Vec<PyDict> {
        s21_todo!("T16", "SessionManager::list_sessions")
    }

    /// `{by_id, real_path}`，依 by_id 排序。
    pub fn list_devices(&self) -> Vec<PyDict> {
        s21_todo!("T16", "SessionManager::list_devices")
    }

    /// `state.json` 寫入失敗 → `Err`（framing 轉 EXCEPTION）。
    pub fn bind(&self, _selector: &str, _by_id: &str) -> Result<PyDict, PyErr> {
        s21_todo!("T16", "SessionManager::bind")
    }

    pub fn pin(&self, _selector: &str, _profile: &str) -> Result<PyDict, PyErr> {
        s21_todo!("T16", "SessionManager::pin")
    }

    pub fn unpin(&self, _selector: &str) -> Result<PyDict, PyErr> {
        s21_todo!("T16", "SessionManager::unpin")
    }

    pub fn alias_list(&self) -> PyDict {
        s21_todo!("T16", "SessionManager::alias_list")
    }

    pub fn alias_set(&self, _sid: &str, _alias: &str) -> Result<PyDict, PyErr> {
        s21_todo!("T16", "SessionManager::alias_set")
    }

    pub fn alias_assign(
        &self,
        _by_id: &str,
        _alias: &str,
        _profile: Option<&str>,
    ) -> Result<PyDict, PyErr> {
        s21_todo!("T16", "SessionManager::alias_assign")
    }

    pub fn alias_unassign(&self, _alias: &str) -> Result<PyDict, PyErr> {
        s21_todo!("T16", "SessionManager::alias_unassign")
    }

    /// interactive_send／status／close 只帶 interactive_id，以 manager 的索引路由；查無 → `INTERACTIVE_NOT_FOUND`。
    pub fn route_interactive(
        &self,
        _interactive_id: &str,
        _req: SessionRequest,
        _timeout: Duration,
    ) -> Result<PyDict, PyErr> {
        s21_todo!("T16", "SessionManager::route_interactive")
    }

    /// `new()` 時建立並 `wal.set_health_sink` 一次；每個 session fence 建立時 register。
    pub fn wal_health_fanout(&self) -> Arc<WalHealthFanout> {
        s21_todo!("T16", "SessionManager::wal_health_fanout")
    }

    pub fn on_tick(&self) {
        s21_todo!("T16", "SessionManager::on_tick")
    }

    /// `(sessions, devices)`。
    pub fn counts(&self) -> (usize, usize) {
        s21_todo!("T16", "SessionManager::counts")
    }

    pub fn attached_real_paths(&self) -> Vec<String> {
        s21_todo!("T16", "SessionManager::attached_real_paths")
    }

    /// `mcu.status` 用（02 §7.2）。
    pub fn flash_candidates(&self) -> Vec<PyDict> {
        s21_todo!("T16", "SessionManager::flash_candidates")
    }

    /// DeviceWatcher → manager 的事件送端（Delta 轉 `update_devices`、Tick 轉 `on_tick`）。
    pub fn watcher_sender(&self) -> Sender<WatcherEvent> {
        s21_todo!("T16", "SessionManager::watcher_sender")
    }

    /// SC-11 stopping gate：冪等設定 `stopping=true`；之後任何自動或 RPC 路徑的 attach 都不開啟裝置。
    pub fn begin_shutdown(&self) {
        s21_todo!("T16", "SessionManager::begin_shutdown")
    }

    /// 先隱含呼叫 `begin_shutdown`；回傳每個 drain 目標是否在 `budget` 內收斂。
    pub fn shutdown(&self, _budget: Duration) -> Vec<(String, bool)> {
        s21_todo!("T16", "SessionManager::shutdown")
    }

    /// 只供測試（test-fakes）：session 的 fence `wal_unhealthy`（`None`＝查無 session）。
    #[cfg(feature = "test-fakes")]
    pub fn session_fence_wal_unhealthy(&self, _selector: &str) -> Option<bool> {
        s21_todo!("T16", "SessionManager::session_fence_wal_unhealthy")
    }

    /// 只供測試（test-fakes）：session 的 `last_error` 變更歷史（依發生順序）。
    #[cfg(feature = "test-fakes")]
    pub fn last_error_history(&self, _com: &str) -> Vec<String> {
        s21_todo!("T16", "SessionManager::last_error_history")
    }

    /// 只供測試（test-fakes）：直接設定 session 狀態（FLASHING／RECOVERING 等無法由 RPC 到達的狀態）。
    #[cfg(feature = "test-fakes")]
    pub fn force_session_state(&self, _selector: &str, _state: crate::session_core::SessionState) {
        s21_todo!("T16", "SessionManager::force_session_state")
    }
}
