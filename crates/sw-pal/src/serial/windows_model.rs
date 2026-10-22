//! Windows 序列埠的純函式模型（arch §4.2，P5-T3）。
//!
//! 本檔不依賴 `windows-sys`，因此 Linux 上也能執行單元測試（DCB／COMMTIMEOUTS 數值、裝置名正規化、
//! `CreateFileW` 錯誤映射、overlapped 完成映射）。原生 `CreateFileW`／`SetCommState` 呼叫在 Windows 後端內使用本檔結果。
//!
//! Task 0 骨架：型別與簽章已凍結，函式本體由 Task 3 交付（`s21_todo!`）。

use crate::traits::{PalErrorKind, SerialConfig, TxOutcome};

/// DCB 欄位模型（`SetCommState` 的輸入）。數值對照 Win32 常數，註解列於各欄位。
pub struct DcbModel {
    pub baud: u32,
    pub byte_size: u8,
    /// 0 NOPARITY、1 ODDPARITY、2 EVENPARITY。
    pub parity: u8,
    /// 0 ONESTOPBIT、2 TWOSTOPBITS。
    pub stop_bits: u8,
    pub f_binary: bool,
    pub f_outx_cts_flow: bool,
    /// 1 RTS_CONTROL_ENABLE、2 RTS_CONTROL_HANDSHAKE。
    pub rts_control: u8,
    /// 1 DTR_CONTROL_ENABLE。
    pub dtr_control: u8,
    pub f_outx: bool,
    pub f_inx: bool,
    pub f_dsr_sensitivity: bool,
    pub f_outx_dsr_flow: bool,
}

/// `SerialConfig` 轉 DCB 模型（對齊 pyserial 預設：`fBinary=1`、無軟體流控）。
pub fn dcb_model(_cfg: &SerialConfig) -> DcbModel {
    s21_todo!("T3", "dcb_model")
}

/// `SetCommTimeouts` 的輸入模型（對齊 pyserial 常數；`PurgeComm` 不呼叫）。
pub struct CommTimeoutsModel {
    pub read_interval: u32,
    pub read_total_multiplier: u32,
    pub read_total_constant: u32,
    pub write_total_multiplier: u32,
    pub write_total_constant: u32,
}

/// 回傳 Task 3 規定的 COMMTIMEOUTS（讀取間隔與寫入逾時的固定值）。
pub fn comm_timeouts_model() -> CommTimeoutsModel {
    s21_todo!("T3", "comm_timeouts_model")
}

/// `_win_device_name`：`COMn`（n≥10 亦同）轉為 `\\.\COMn`；已含 `\\.\` 前綴者不變；其他路徑原樣回傳。
pub fn win_device_name(_path: &str) -> String {
    s21_todo!("T3", "win_device_name")
}

/// `CreateFileW` 失敗的分類（`map_open_error` 的輸出）。
#[derive(Clone, Copy, Debug, PartialEq, Eq)]
pub enum OpenErrorKind {
    NotFound,
    AccessDenied,
    Other,
}

/// 依 Win32 錯誤碼分類（2／3 → `NotFound`；5 → `AccessDenied`；其他 → `Other`）。
pub fn map_open_error(_win32: u32, _py_message: String) -> OpenErrorKind {
    s21_todo!("T3", "map_open_error")
}

/// overlapped `WriteFile` 完成的映射（arch §4.2 TX 三態）：
/// 成功 → `Completed(transferred)`；`ERROR_OPERATION_ABORTED`(995) → `Completed(transferred)` ＋ `Cancelled`；
/// `ERROR_TIMEOUT`(1460) → `Completed(transferred)` ＋ `TimedOut`（部分完成）。
pub fn map_overlapped_completion(
    _get_result_ok: bool,
    _transferred: u32,
    _win32: u32,
) -> (TxOutcome, Option<PalErrorKind>) {
    s21_todo!("T3", "map_overlapped_completion")
}
