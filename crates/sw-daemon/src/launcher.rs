//! daemon 啟動器（P4 `launcher.rs`；P5 加入 `implementation == "rust"` 分支）。
//!
//! 紅燈階段替身：`resolve_launch` 本體為 `todo!()`，僅保留計畫指定的公開型別與簽章。
use sw_proto::release_manifest::ReleaseManifest;

/// 啟動計畫：原生 Rust daemon，或（僅未退役的舊 bundle）Python 子行程。
#[derive(Debug, Clone, PartialEq, Eq)]
pub enum LaunchPlan {
    RustNative,
    PythonChild { argv: Vec<String> },
}

/// 啟動錯誤。`PythonDaemonRetired` 對應 stderr `serialwrapd: INTERNAL_ERROR: python daemon retired by release manifest`（exit 2）。
#[derive(Debug, Clone, PartialEq, Eq)]
pub enum LaunchError {
    PythonDaemonRetired,
    ManifestInvalid(String),
}

/// 依 manifest 決定啟動方式。`python_daemon_retired()` 為真而 implementation 為 python（被竄改的 manifest）→ `PythonDaemonRetired`。
pub fn resolve_launch(_manifest: &ReleaseManifest) -> Result<LaunchPlan, LaunchError> {
    todo!("P9-T16 紅燈：resolve_launch 尚未實作")
}
