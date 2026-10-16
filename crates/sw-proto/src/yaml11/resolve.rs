//! 事件流建樹與 PyYAML implicit resolver（P3-T6 紅燈 stub，行為尚未實作）。
//!
//! 實作時：`yaml_rust2::parser::Parser` 事件（`Scalar(value, TScalarStyle, anchor, tag)`）→ 樹；
//! plain scalar 依 PyYAML `resolver.py` 的 regex 與首字元表判型；quoted scalar 一律 `str`。
#![allow(dead_code, unused_variables)]

use super::{YValue, YamlError};

/// 由事件流建出單一文件的 `YValue`（stub 階段未被呼叫）。
pub(super) fn build_document(text: &str) -> Result<YValue, YamlError> {
    todo!("P3-T6: resolve::build_document")
}

/// PyYAML implicit resolver：回傳 plain scalar 的 tag 類別（`bool`／`int`／`float`／`null`／`timestamp`／`merge`／`value`）。
pub(super) fn resolve_plain(s: &str) -> Option<&'static str> {
    todo!("P3-T6: resolve::resolve_plain")
}
