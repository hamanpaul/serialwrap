//! `%LOCALAPPDATA%\Programs\serialwrap\versions\<v>\` 與 `bin\` 安裝佈局（P4-T11 承接）。
//!
//! 紅燈階段（TDD）骨架：本檔只提供計畫 05 Task 11「Produces」指定的公開介面，函式本體一律
//! `unimplemented!("P4-T11: <name>")`。行為規範見 `crates/sw-cli/tests/mgmt_layout.rs`。
#![allow(unused_variables)]

use std::path::{Path, PathBuf};

use sw_pal::fs_ops::FileLockGuard;
use sw_proto::pyjson::PyDict;
use sw_proto::release_manifest::ReleaseManifest;

use crate::ctx::CliCtx;
pub use crate::pairing::{check_pairing, pairing_item, PairingDiff};

/// 計畫中的 `Ctx`（即 [`CliCtx`]；見 05 計畫 §0.2）。
pub type Ctx<'a> = CliCtx<'a>;

/// 入口檔的放置方式（Windows 複製、POSIX symlink）。
#[derive(Clone, Copy, Debug, PartialEq, Eq)]
pub enum EntryStyle {
    /// POSIX：`<bin>/<name>` 為指向 `versions/<v>/bin/<name>` 的 symlink。
    Symlink,
    /// Windows：`<bin>\<name>` 為複本（執行中 exe 以改名保留舊檔）。
    Copy,
}

/// 安裝根目錄的解析結果（計畫 Task 11「Produces」）。
#[derive(Clone, Debug, PartialEq, Eq)]
pub struct InstallRoot {
    pub root: PathBuf,
    pub versions: PathBuf,
    pub bin: PathBuf,
    pub backups: PathBuf,
    pub supervisor: PathBuf,
    pub current_file: PathBuf,
    pub entry_style: EntryStyle,
    pub entry_names: Vec<String>,
}

/// `current-release.json` 內的版本參照（`previous` 欄位）。
#[derive(Clone, Debug, PartialEq, Eq)]
pub struct ReleaseRef {
    pub bundle_version: String,
    pub dir: String,
}

/// `current-release.json` 的完整內容（P4-T11 新增 `previous`、`backup_dir`、`switched_at`）。
#[derive(Clone, Debug, PartialEq, Eq)]
pub struct CurrentRelease {
    pub bundle_version: String,
    pub dir: String,
    pub previous: Option<ReleaseRef>,
    pub backup_dir: Option<String>,
    pub switched_at: String,
}

/// 安裝佈局錯誤。本 Phase 不新增錯誤碼；呼叫端依 §9.2 既有碼映射。
#[derive(Clone, Debug, PartialEq, Eq)]
pub enum LayoutError {
    /// `versions/<v>` 已存在但驗證失敗：不覆寫，回報。
    ExistingCorrupt(PathBuf),
    /// 檔案系統操作失敗。
    Io { path: PathBuf, message: String },
    /// `current-release.json` 或 manifest 無法解析。
    Parse { path: PathBuf, message: String },
    /// 無法定位 release manifest。
    ManifestNotFound(String),
}

/// 執行中 CLI 的位置分類。
#[derive(Clone, Debug, PartialEq, Eq)]
pub enum ExeLocation {
    /// `versions/<v>/bin/<name>`。
    InstalledVersion(String),
    /// 穩定入口 `<bin>/<name>`。
    StableEntry,
    /// 其他位置（例如開發 bundle 的解壓目錄）。
    External(PathBuf),
}

/// `switch_entries` 的結果；`revert_entries` 以此還原。
#[derive(Clone, Debug, PartialEq, Eq)]
pub struct SwitchReport {
    /// 每個入口的 (名稱, 切換前的舊目標或 None)。
    pub replaced: Vec<(String, Option<String>)>,
    /// 本次產生的 `*.old.<ts>` 保存檔。
    pub old_files: Vec<PathBuf>,
}

/// 安裝根目錄解析（`SERIALWRAP_INSTALL_ROOT` > Windows `%LOCALAPPDATA%\Programs\serialwrap` > POSIX `DATA_DIR`）。
pub fn resolve_install_root(ctx: &Ctx) -> InstallRoot {
    unimplemented!("P4-T11: resolve_install_root")
}

/// 讀取 `current-release.json`；不存在回 `Ok(None)`，未知鍵忽略。
pub fn read_current(root: &InstallRoot) -> Result<Option<CurrentRelease>, LayoutError> {
    unimplemented!("P4-T11: read_current")
}

/// 原子寫入 `current-release.json`（DISK_STABLE 格式）。
pub fn write_current(
    ctx: &Ctx,
    root: &InstallRoot,
    cur: &CurrentRelease,
) -> Result<(), LayoutError> {
    unimplemented!("P4-T11: write_current")
}

/// 判定執行中 exe 的位置。
pub fn classify_exe(ctx: &Ctx, root: &InstallRoot) -> ExeLocation {
    unimplemented!("P4-T11: classify_exe")
}

/// 驗證 bundle 目錄：所有 `components.*.sha256`、`tree_sha256` 與 `SHA256SUMS`。
pub fn verify_bundle(ctx: &Ctx, dir: &Path, m: &ReleaseManifest) -> Result<(), Vec<PairingDiff>> {
    unimplemented!("P4-T11: verify_bundle")
}

/// 複製 bundle 到 `versions/<v>.staging`、驗證後改名為 `versions/<v>`；已存在且驗證通過則冪等回傳。
pub fn stage_version(
    ctx: &Ctx,
    root: &InstallRoot,
    bundle_dir: &Path,
    m: &ReleaseManifest,
) -> Result<PathBuf, LayoutError> {
    unimplemented!("P4-T11: stage_version")
}

/// 把穩定入口切換到 `version_dir`（Windows 複製＋改名保存；POSIX symlink 原子覆蓋）。
pub fn switch_entries(
    ctx: &Ctx,
    root: &InstallRoot,
    version_dir: &Path,
) -> Result<SwitchReport, LayoutError> {
    unimplemented!("P4-T11: switch_entries")
}

/// 依 `report` 反向還原入口（切換失敗或後續步驟失敗時使用）。
pub fn revert_entries(
    ctx: &Ctx,
    root: &InstallRoot,
    report: &SwitchReport,
) -> Result<(), LayoutError> {
    unimplemented!("P4-T11: revert_entries")
}

/// 部署 Python 程式碼的樹狀雜湊（逐字照 P1 `tree_sha256`：排除 `__pycache__` 與 `*.pyc`，relpath 以 `/`，依位元組序）。
pub fn tree_sha256(ctx: &Ctx, py_root: &Path) -> std::io::Result<String> {
    unimplemented!("P4-T11: tree_sha256")
}

/// 定位 release manifest（`SERIALWRAP_RELEASE_MANIFEST` > realpath(current_exe) 上兩層 > Windows `current-release.json` 的 dir）。
pub fn locate_manifest(ctx: &Ctx) -> Result<(PathBuf, ReleaseManifest), LayoutError> {
    unimplemented!("P4-T11: locate_manifest")
}

/// 取得 `<root>/.install.lock`；每 0.2 s 非阻塞嘗試，5 s 內取不到回 `TIMEOUT`／`phase:"install_lock"` 的 PyDict。
pub fn install_lock(ctx: &Ctx, root: &InstallRoot) -> Result<FileLockGuard, PyDict> {
    unimplemented!("P4-T11: install_lock")
}
