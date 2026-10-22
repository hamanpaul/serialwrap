//! P5-T15：`state.json` 的增量變更型別（紅燈階段骨架；T11 的 `Effect::Persist` 使用）。
//!
//! `StateStore`／`StateModel`／`load_state` 等由 Task 15 實作者補上。

use std::collections::HashSet;
use std::path::{Path, PathBuf};
use std::sync::Arc;

use indexmap::IndexMap;
use sw_pal::traits::{FsOps, PalError};
use sw_proto::pyjson::{PyDict, PyValue};

/// released 列（`state.json` 的 released 區段）。
#[derive(Clone, Debug, PartialEq)]
pub struct ReleasedRow {
    pub by_id: PyValue,
    pub released_by: PyValue,
    pub released_at: PyValue,
    pub reason: PyValue,
}

/// 狀態變更（一次 commit 的增量）。
#[derive(Clone, Debug)]
pub enum StateDelta {
    Released { sid: String, row: ReleasedRow },
    Unreleased { sid: String },
    Binding { sid: String, by_id: Option<String> },
    Pin { device_key: String, profile: Option<String> },
    Detected { by_id: String, profile: String },
    AliasSet { alias: String, row: Option<PyDict> },
    Touch,
}

// ---------------------------------------------------------------------------
// P5-T16 Step 0 最小骨架（Task 15 的公開簽章；本 Task 測試需要的 manager 依賴）。
// 函式本體為 `s21_todo!`，由 Task 15 實作者補上；欄位與簽章照 plan 06 Task 15「Produces」。
// ---------------------------------------------------------------------------

/// `state.json` 載入結果（Task 15 Produces）。
#[derive(Clone, Debug, Default, PartialEq)]
pub struct LoadedState {
    pub aliases: IndexMap<String, sw_proto::pyjson::PyDict>,
    pub bindings: IndexMap<String, String>,
    pub released: IndexMap<String, ReleasedRow>,
    pub profile_pins: IndexMap<String, String>,
    pub profile_detected: IndexMap<String, String>,
}

/// `state.json` 載入錯誤（Display 即 Python `StateLoadError` 訊息）。
#[derive(Debug)]
pub struct StateLoadError {
    pub message: String,
}

impl std::fmt::Display for StateLoadError {
    fn fmt(&self, f: &mut std::fmt::Formatter<'_>) -> std::fmt::Result {
        f.write_str(&self.message)
    }
}

impl std::error::Error for StateLoadError {}

/// 讀取 `state.json`；損毀時回 `(LoadedState::default(), Some(.corrupt 路徑))`。
pub fn load_state(
    _path: &Path,
    _fs: &dyn FsOps,
) -> Result<(LoadedState, Option<PathBuf>), StateLoadError> {
    s21_todo!("T15", "load_state")
}

/// `state.json` 的序列化位元組（`DISK_STABLE` ＋ `\n`；未知鍵於存檔時丟棄，WAL-D14）。
pub fn serialize_state(_s: &LoadedState) -> Vec<u8> {
    s21_todo!("T15", "serialize_state")
}

/// 記憶體狀態模型：`LoadedState` ＋ 已載入但未實體化的 released 列（DF-04）。
pub struct StateModel {
    _stub: (),
}

impl StateModel {
    pub fn new(_loaded: LoadedState) -> StateModel {
        s21_todo!("T15", "StateModel::new")
    }

    pub fn apply(&mut self, _d: StateDelta) {
        s21_todo!("T15", "StateModel::apply")
    }

    pub fn snapshot(&self) -> LoadedState {
        s21_todo!("T15", "StateModel::snapshot")
    }

    pub fn released_by_ids(&self) -> HashSet<String> {
        s21_todo!("T15", "StateModel::released_by_ids")
    }
}

/// `state.json` 的 `StateStore`（thread `serialwrap-state-store`，bounded(64)；Task 15 Produces）。
pub struct StateStore {
    _stub: (),
}

impl StateStore {
    pub fn spawn(_path: PathBuf, _fs: Arc<dyn FsOps>) -> StateStore {
        s21_todo!("T15", "StateStore::spawn")
    }

    /// 測試掛鉤（Review Focus 1）：`before_commit`／`after_commit` 於 store thread 中、`atomic_write` 前後呼叫。
    #[cfg(feature = "test-fakes")]
    pub fn spawn_with_hooks(
        _path: PathBuf,
        _fs: Arc<dyn FsOps>,
        _before_commit: Option<Box<dyn Fn() + Send + Sync>>,
        _after_commit: Option<Box<dyn Fn() + Send + Sync>>,
    ) -> StateStore {
        s21_todo!("T15", "StateStore::spawn_with_hooks")
    }

    /// 非同步提交：結果經 `done` 回傳（actor 以 `ControlEvent::StateCommitted` 收斂）。
    pub fn commit(&self, _snap: LoadedState, _done: crossbeam_channel::Sender<Result<(), PalError>>) {
        s21_todo!("T15", "StateStore::commit")
    }

    /// 同步提交；失敗回 `Err`（管理操作以此轉 `Err(PyErr)`）。
    pub fn commit_sync(&self, _snap: LoadedState) -> Result<(), PalError> {
        s21_todo!("T15", "StateStore::commit_sync")
    }
}
