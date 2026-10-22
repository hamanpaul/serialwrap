//! `sw-proto`：協定型別與 Python 相容語意層（P3-T3～P3-T8 stub，行為尚未實作）。

/// Slice 2.1 骨架未實作函式的統一 panic（與 `sw_core::s21_todo!` 同格式）。
#[macro_export]
macro_rules! s21_todo {
    ($t:literal, $sym:literal) => {
        unimplemented!(concat!("slice21 ", $t, " ", $sym))
    };
}

pub mod bench_registry;
pub mod config_types;
pub mod constants;
pub mod digest;
pub mod endpoint;
pub mod error_codes;
pub mod pycompat;
pub mod pyfmt;
pub mod pyjson;
pub mod pypath;
pub mod pyre;
pub mod release_manifest;
pub mod rpc_types;
pub mod runtime_paths;
pub mod session_last_errors;
pub mod version;
pub mod wal_types;
pub mod yaml11;
