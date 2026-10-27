//! 序列埠後端（arch §4.2）。POSIX 實作在 `posix`；Windows 後端由 Task 3 承接。
//!
//! 跨平台 trait（`SerialBackend`、`SerialHandle`、`UartExclusive` 等）定義於 `crate::traits`。

#[cfg(unix)]
pub mod posix;

#[cfg(windows)]
pub mod windows;

/// Windows 後端的純函式模型（DCB、COMMTIMEOUTS、錯誤映射；無平台依賴，P5 Task 3）。
pub mod windows_model;

/// Windows 的 holder 診斷（arch §4.2）：無法掃描 tty 持有者，回 `holder_unknown:true`、`status:Unsupported`。
#[cfg(windows)]
pub struct WindowsUartExclusive;

#[cfg(windows)]
impl crate::traits::UartExclusive for WindowsUartExclusive {
    fn probe_holders(&self, _real_path: &str) -> crate::traits::HolderReport {
        crate::traits::HolderReport {
            pids: Vec::new(),
            holder_unknown: true,
            status: crate::traits::HolderScanStatus::Unsupported,
        }
    }
}

/// 鎖後 holder 掃描不完整（`PostLockVerdict::AcceptUnverified`）而仍開啟中的裝置登錄
/// （對照 Python `serial_port._UNKNOWN_HOLDER_DEVICES`；`health.status` 的 `foreign_holders` 合併來源，
/// parity-amendments-2026-10-26 #11）。行程內共用、與 Python 模組層級 set 同語意。
static UNKNOWN_HOLDER_DEVICES: std::sync::Mutex<std::collections::BTreeSet<String>> =
    std::sync::Mutex::new(std::collections::BTreeSet::new());

/// Python `os.path.realpath`：可解析時取實體路徑，否則原樣。
fn holder_key(device_path: &str) -> String {
    std::fs::canonicalize(device_path)
        .map(|p| p.to_string_lossy().into_owned())
        .unwrap_or_else(|_| device_path.to_string())
}

/// 回傳目前仍開啟、且外部 holder 掃描結果不完整的裝置（依字典序；Python `unknown_holder_devices()`）。
pub fn unknown_holder_devices() -> Vec<String> {
    UNKNOWN_HOLDER_DEVICES
        .lock()
        .unwrap_or_else(std::sync::PoisonError::into_inner)
        .iter()
        .cloned()
        .collect()
}

/// 登記或移除「holder 掃描不完整」的裝置（Python `_set_holder_unknown`）。
/// 開檔成功時依鎖後重掃結果呼叫；關閉時以 `false` 呼叫。測試替身亦可直接呼叫以模擬 PAL 開檔結果。
pub fn set_holder_unknown(device_path: &str, unknown: bool) {
    let key = holder_key(device_path);
    let mut set = UNKNOWN_HOLDER_DEVICES
        .lock()
        .unwrap_or_else(std::sync::PoisonError::into_inner);
    if unknown {
        set.insert(key);
    } else {
        set.remove(&key);
    }
}
