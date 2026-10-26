//! `session.self_test` 分類（Slice 2.1 Task 14；05 §11 順序 0–15）。
//!
//! Task 0 骨架：型別與簽章凍結，分類本體由 Task 14 交付。

use sw_pal::traits::HolderReport;
use sw_proto::pyjson::PyDict;

use crate::session_core::SessionCore;
use crate::Mono;

/// self-test 的前置觀測（由 actor 於 mailbox 外取得快照後注入）。
pub struct SelfTestCtx {
    pub device_present: bool,
    pub holder: Option<HolderReport>,
    pub by_id_realpath: Option<String>,
    pub vtty_alive: Option<bool>,
    pub strict_human_lock: bool,
    pub now: Mono,
}

/// 需要送出 probe 才能完成分類時回傳的待辦（nonce 與 timeout 由呼叫端轉為 TX）。
pub struct NeedProbe {
    pub probe: String,
    pub nonce: String,
    pub timeout_s: f64,
    /// 已決定的欄位（回應字典的部分結果）。
    pub partial: PyDict,
}

/// probe 之前的分類（順序 0–15，05 §11）：能決定時回 `Ok(結果)`，否則回 `Err(NeedProbe)`。
pub fn classify_pre_probe(_core: &SessionCore, _ctx: &SelfTestCtx) -> Result<PyDict, NeedProbe> {
    crate::s21_todo!("T14", "classify_pre_probe")
}

// ── P7 Task 0（08-slice23-login-events.md）凍結的新增公開項；本體由標示的 Task 實作 ──

/// self-test 最終回覆組裝器命名空間（P7 Task 8）。
pub struct SelfTestFinal;

impl SelfTestFinal {
    /// 依 `self_test_responses` golden 的分支模板插入 session 與 lease ctx（各分支 key 序不同）。
    pub fn assemble(
        _classification: &str,
        _extra: PyDict,
        _session_public: PyDict,
        _lease_ctx: PyDict,
        _command_capable: bool,
    ) -> PyDict {
        todo!("P7 Task 8")
    }
}

/// 分類順序 16（含 flood → `RX_FLOOD`／`wait`）。
pub fn nonce_probe_result(
    _probe_ok: bool,
    _rx_bytes_last_10s: Option<u64>,
    _attached_real_path: Option<&str>,
    _current_real_path: Option<&str>,
    _attached_vtty: Option<&str>,
    _bridge_generation: u64,
) -> PyDict {
    todo!("P7 Task 8")
}

/// RELEASED 分支；`holder_unknown=true` → `external_holder:"none"`、`reclaimable:false`、`wait_external_flash`（BC-04）。
pub fn released_result(
    _released_by: Option<&str>,
    _released_at: Option<&str>,
    _reason: Option<&str>,
    _holder_pids: &[u32],
    _holder_unknown: bool,
) -> PyDict {
    todo!("P7 Task 8")
}
