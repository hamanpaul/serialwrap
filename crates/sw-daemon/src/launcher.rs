//! `serialwrapd` launcher（P4-T17；arch §6.1、§10.1）。
//!
//! 紅燈 stub：行為尚未實作。Stage 1 只承接 `components.daemon.implementation == "python"`。

use std::ffi::OsString;
use std::path::Path;

use sw_proto::release_manifest::ReleaseManifest;

/// launcher 失敗原因；失敗時 stderr 一行 `serialwrapd: <原因>`、exit 2（不印 JSON）。
#[derive(Debug, Clone, PartialEq, Eq)]
pub enum LauncherError {
    ManifestNotFound(String),
    ManifestInvalid(String),
    UnsupportedImplementation(String),
}

/// 由 manifest 的 `components.daemon.entry` 組出實際 argv：相對路徑轉為 `manifest_dir` 下的絕對路徑，
/// 其餘元素原樣保留，尾端接上 `argv_tail`。
pub fn launcher_argv(
    _manifest_dir: &Path,
    _m: &ReleaseManifest,
    _argv_tail: &[OsString],
) -> Result<Vec<OsString>, LauncherError> {
    unimplemented!("P4-T17")
}

/// 定位 manifest（`current_exe` 的上兩層目錄）→ `launcher_argv` → 以平台程序 primitive 承接。
/// `argv` 為完整 argv（`argv[0]` 為本程式）。回傳程序結束碼。
pub fn run(_argv: Vec<OsString>, _p: &sw_pal::Platform) -> i32 {
    unimplemented!("P4-T17")
}
