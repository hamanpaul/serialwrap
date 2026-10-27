//! Windows 序列埠的純函式模型（arch §4.2，P5-T3）。
//!
//! 本檔不依賴 `windows-sys`，因此 Linux 上也能執行單元測試（DCB／COMMTIMEOUTS 數值、裝置名正規化、
//! `CreateFileW` 錯誤映射、overlapped 完成映射）。原生 `CreateFileW`／`SetCommState` 呼叫在 Windows 後端內使用本檔結果。
//!
//! 本檔提供供 Windows 後端與跨平台測試共用的純函式模型。

use crate::traits::{PalError, PalErrorKind, ReadError, SerialConfig, TxOutcome};

/// 等待 API 失敗時保留 `SELECT` 類來源，供 `SerialReader` 對齊 POSIX poll 語意。
pub fn classify_wait_error(error: PalError) -> ReadError {
    ReadError::Wait(error)
}

/// 序列埠讀取 API 失敗時保留 `SERIAL_READ` 類來源。
pub fn classify_read_error(error: PalError) -> ReadError {
    ReadError::Read(error)
}

pub const PYSERIAL_ERROR_CHAR: i8 = 0;
pub const PYSERIAL_XON_CHAR: i8 = 0x11;
pub const PYSERIAL_XOFF_CHAR: i8 = 0x13;

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
pub fn dcb_model(cfg: &SerialConfig) -> DcbModel {
    let parity = match cfg.parity {
        crate::traits::Parity::None => 0,
        crate::traits::Parity::Odd => 1,
        crate::traits::Parity::Even => 2,
    };
    let (rts_control, f_outx_cts_flow) = match cfg.flow {
        crate::traits::FlowControl::None => (1, false),
        crate::traits::FlowControl::RtsCts => (2, true),
    };
    DcbModel {
        baud: cfg.baud,
        byte_size: if cfg.data_bits == 7 { 7 } else { 8 },
        parity,
        stop_bits: if cfg.stop_bits == 2 { 2 } else { 0 },
        f_binary: true,
        f_outx_cts_flow,
        rts_control,
        dtr_control: 1,
        f_outx: cfg.xonxoff,
        f_inx: cfg.xonxoff,
        f_dsr_sensitivity: false,
        f_outx_dsr_flow: false,
    }
}

/// 將模型中受管理的 DCB 旗標套到 GetCommState 取得的原始值。
pub fn dcb_flags(mut flags: u32, model: &DcbModel) -> u32 {
    const F_BINARY: u32 = 1 << 0;
    const F_PARITY: u32 = 1 << 1;
    const F_OUTX_CTS_FLOW: u32 = 1 << 2;
    const F_OUTX_DSR_FLOW: u32 = 1 << 3;
    const DTR_CONTROL_MASK: u32 = 0b11 << 4;
    const F_DSR_SENSITIVITY: u32 = 1 << 6;
    const F_OUT_X: u32 = 1 << 8;
    const F_IN_X: u32 = 1 << 9;
    const F_ERROR_CHAR: u32 = 1 << 10;
    const F_NULL: u32 = 1 << 11;
    const RTS_CONTROL_MASK: u32 = 0b11 << 12;
    const F_ABORT_ON_ERROR: u32 = 1 << 14;

    set_bit(&mut flags, F_BINARY, model.f_binary);
    set_bit(&mut flags, F_PARITY, model.parity != 0);
    set_bit(&mut flags, F_OUTX_CTS_FLOW, model.f_outx_cts_flow);
    set_bit(&mut flags, F_OUTX_DSR_FLOW, model.f_outx_dsr_flow);
    set_field(&mut flags, DTR_CONTROL_MASK, 4, model.dtr_control as u32);
    set_bit(&mut flags, F_DSR_SENSITIVITY, model.f_dsr_sensitivity);
    set_bit(&mut flags, F_OUT_X, model.f_outx);
    set_bit(&mut flags, F_IN_X, model.f_inx);
    set_bit(&mut flags, F_ERROR_CHAR, false);
    set_bit(&mut flags, F_NULL, false);
    set_field(&mut flags, RTS_CONTROL_MASK, 12, model.rts_control as u32);
    set_bit(&mut flags, F_ABORT_ON_ERROR, false);
    flags
}

fn set_bit(flags: &mut u32, mask: u32, enabled: bool) {
    if enabled {
        *flags |= mask;
    } else {
        *flags &= !mask;
    }
}

fn set_field(flags: &mut u32, mask: u32, shift: u32, value: u32) {
    *flags = (*flags & !mask) | ((value << shift) & mask);
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
    CommTimeoutsModel {
        read_interval: u32::MAX,
        read_total_multiplier: u32::MAX,
        read_total_constant: 200,
        write_total_multiplier: 0,
        write_total_constant: 5000,
    }
}

/// `_win_device_name`：`COMn`（n≥10 亦同）轉為 `\\.\COMn`；已含 `\\.\` 前綴者不變；其他路徑原樣回傳。
pub fn win_device_name(path: &str) -> String {
    if path.starts_with(r"\\.\") {
        return path.to_string();
    }
    let is_com = path
        .get(..3)
        .is_some_and(|prefix| prefix.eq_ignore_ascii_case("COM"));
    let suffix = path.get(3..).unwrap_or_default();
    if is_com && !suffix.is_empty() && suffix.chars().all(|ch| ch.is_ascii_digit()) {
        format!(r"\\.\{}", path.to_ascii_uppercase())
    } else {
        path.to_string()
    }
}

/// `CreateFileW` 失敗的分類（`map_open_error` 的輸出）。
#[derive(Clone, Copy, Debug, PartialEq, Eq)]
pub enum OpenErrorKind {
    NotFound,
    AccessDenied,
    Other,
}

/// 依 Win32 錯誤碼分類（2／3 → `NotFound`；5 → `AccessDenied`；其他 → `Other`）。
pub fn map_open_error(win32: u32, _py_message: String) -> OpenErrorKind {
    match win32 {
        2 | 3 => OpenErrorKind::NotFound,
        5 | 32 => OpenErrorKind::AccessDenied,
        _ => OpenErrorKind::Other,
    }
}

/// 映射不含取消來源資訊的 overlapped 完成碼。
/// 995 無法單靠錯誤碼分辨本地取消與裝置拔除，因此保守回報結果不明及 I/O 錯誤。
pub fn map_overlapped_completion(
    get_result_ok: bool,
    transferred: u32,
    win32: u32,
) -> (TxOutcome, Option<PalErrorKind>) {
    let transferred = transferred as usize;
    if win32 == 995 {
        return (TxOutcome::Unknown { confirmed: 0 }, Some(PalErrorKind::Io));
    }
    if win32 == 1460 {
        return (
            TxOutcome::Completed(transferred),
            Some(PalErrorKind::TimedOut),
        );
    }
    if get_result_ok && win32 == 0 {
        return (TxOutcome::Completed(transferred), None);
    }
    (TxOutcome::Unknown { confirmed: 0 }, Some(PalErrorKind::Io))
}

/// 映射 Windows serial 寫入完成，保留本地取消與已提交長度資訊。
/// 成功但不足 requested 的完成視為終止短寫，不可重送餘段。
pub fn map_overlapped_write_completion(
    get_result_ok: bool,
    transferred: u32,
    requested: usize,
    win32: u32,
    cancel_requested: bool,
) -> (TxOutcome, Option<PalErrorKind>) {
    let transferred = transferred as usize;
    if transferred > requested {
        return (TxOutcome::Unknown { confirmed: 0 }, Some(PalErrorKind::Io));
    }
    if win32 == 995 && cancel_requested {
        return (
            TxOutcome::Completed(transferred),
            Some(PalErrorKind::Cancelled),
        );
    }
    if win32 == 0 && get_result_ok && transferred < requested {
        let kind = if cancel_requested {
            PalErrorKind::Cancelled
        } else {
            PalErrorKind::TimedOut
        };
        return (TxOutcome::Completed(transferred), Some(kind));
    }
    map_overlapped_completion(get_result_ok, transferred as u32, win32)
}
