//! ReDoS 可疑樣式判定（P7 Task 11；`event_engine/schema.py` 的 `_ambiguity_score`／`_has_backref`／
//! `maybe_redos_suspicious`，EVT-D-S15：以無 google-re2 的 Python 為基準）。
//!
//! 08 §0.3 原稿以 `crate::pyre::sre::SubPattern` 為參數；實際 P3 的 AST（`parse::ParsedPattern`）為 pyre 私有型別，
//! 不能出現在公開簽章，故公開 API 以 `(pattern, flags)` 為輸入（P7 Task 0 核對註記）。P7 Task 0 骨架。

use super::{CompileError, PyFlags};

/// sre AST 的歧義分數（巢狀重複、重複中的分支等）。
pub fn ambiguity_score(_pattern: &str, _flags: PyFlags) -> Result<u32, CompileError> {
    todo!("P7 Task 11")
}

/// 是否含 back-reference（`GROUPREF`／`GROUPREF_EXISTS`）。
pub fn has_backref(_pattern: &str, _flags: PyFlags) -> Result<bool, CompileError> {
    todo!("P7 Task 11")
}

/// `score >= 2 || has_backref`。
pub fn maybe_redos_suspicious(_pattern: &str, _flags: PyFlags) -> Result<bool, CompileError> {
    todo!("P7 Task 11")
}
