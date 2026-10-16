//! 後端選擇（`platform_backends._select`）：P3-T9 紅燈 stub。
#![allow(unused_variables)]

use crate::BackendKind;

/// `raw` 為 `SERIALWRAP_RPC_BACKEND`（Q-17）；錯誤字串為 `"unsupported backend: 'x'"`。
pub fn select_backend(raw: Option<&str>, native: BackendKind) -> Result<BackendKind, String> {
    todo!("P3-T9: select_backend")
}
