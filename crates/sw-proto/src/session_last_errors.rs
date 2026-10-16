//! session `last_error` 命名空間（arch §3.2；與 `ErrorCode` 互不轉換；P3-T8 承接，P3-T1 骨架）。
//!
//! 具名值（03 §5.2 的 31 個）由 P3-T8 生成；骨架只保留動態前綴 variant。
#![allow(unused_variables)]

#[derive(Clone, Debug, PartialEq, Eq)]
pub enum SessionLastError {
    ReprobeFailed(String),
    AttachFailed(String),
    BridgeDown(String),
}

impl SessionLastError {
    pub fn as_string(&self) -> String {
        todo!("P3-T8: SessionLastError::as_string")
    }

    pub fn parse(s: &str) -> Option<SessionLastError> {
        todo!("P3-T8: SessionLastError::parse")
    }
}
