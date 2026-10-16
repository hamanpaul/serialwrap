//! RPC／CLI `error_code` 命名空間（arch §3.2；P3-T8 承接，P3-T1 骨架）。
//!
//! Task 8 會由 `scripts/rust/gen_error_codes.py` 依 `contracts/error_codes.yaml` 生成本檔的 variant 與 META；
//! 在 P3-T8 完成前 variant 為空，`META` 為空陣列，行為函式一律 `todo!`。
#![allow(unused_variables)]

/// 每個碼一個 variant（P3-T8 生成）。
#[derive(Clone, Copy, Debug, PartialEq, Eq, Hash)]
pub enum ErrorCode {}

#[derive(Clone, Copy, Debug, PartialEq, Eq)]
pub enum Interface {
    Rpc,
    RpcFrame,
    CliClient,
    CliLocal,
    Rec,
    Proc,
}

#[derive(Clone, Copy, Debug, PartialEq, Eq)]
pub enum Retryable {
    Yes,
    No,
    Conditional,
    FixArgs,
}

#[derive(Clone, Copy, Debug, PartialEq, Eq)]
pub enum Status {
    Existing,
    Approved,
    Implemented,
    Verified,
}

#[derive(Clone, Copy, Debug, PartialEq, Eq)]
pub struct ErrorMeta {
    pub code: &'static str,
    pub interfaces: &'static [Interface],
    pub retryable: Retryable,
    pub reachable: bool,
    pub status: Status,
}

impl ErrorCode {
    /// P3-T8 以生成資料取代；骨架階段為空。
    pub const META: &'static [ErrorMeta] = &[];

    pub fn as_str(self) -> &'static str {
        todo!("P3-T8: ErrorCode::as_str")
    }

    pub fn parse(s: &str) -> Option<ErrorCode> {
        todo!("P3-T8: ErrorCode::parse")
    }

    pub fn meta(self) -> &'static ErrorMeta {
        todo!("P3-T8: ErrorCode::meta")
    }
}
