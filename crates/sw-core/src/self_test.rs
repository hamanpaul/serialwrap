//! `session.self_test` 分類（Slice 2.1 Task 14；05 §11 順序 0–15）。
//!
//! 分類本體在 `SessionCore::self_test_pre_probe`（需讀取 session 私有狀態）；本模組提供凍結簽章
//! `classify_pre_probe` 與注入型別。nonce probe 由 `OpRunner`（`OpSpec::SelfTestProbe`）執行。

use std::sync::Arc;

use sw_pal::traits::{HolderReport, UartExclusive};
use sw_proto::pyjson::PyDict;

use crate::Mono;
use crate::session_core::SessionCore;

/// self-test 的前置觀測（由 actor 於 mailbox 外取得快照後注入）。
pub struct SelfTestCtx {
    pub device_present: bool,
    pub holder: Option<HolderReport>,
    pub by_id_realpath: Option<String>,
    pub vtty_alive: Option<bool>,
    pub strict_human_lock: bool,
    pub now: Mono,
}

/// probe 成功／失敗回應共用的 bridge 欄位（pre-check 當下擷取）。
#[derive(Clone, Debug)]
pub struct SelfTestBase {
    pub attached_real_path: Option<String>,
    pub current_real_path: Option<String>,
    pub bridge_generation: u64,
}

/// 需要送出 probe 才能完成分類時回傳的待辦（nonce 與 timeout 由呼叫端轉為 TX）。
pub struct NeedProbe {
    pub probe: String,
    pub nonce: String,
    pub timeout_s: f64,
    /// 已決定的欄位（回應字典的部分結果）。
    pub partial: PyDict,
    /// probe 結果回應的 bridge 欄位。
    pub base: SelfTestBase,
}

/// self-test 的外部觀測注入（manager／測試設定；未設定時 RELEASED holder 視為無、哨兵 PTY 視為存活）。
#[derive(Clone, Default)]
pub struct SelfTestProbes {
    /// RELEASED 分支的 `/proc` holder 掃描（`_probe_external_holder` 等價）。
    pub holders: Option<Arc<dyn UartExclusive>>,
    /// 哨兵 PTY slave 是否仍存在（`snapshot()["vtty_alive"]`；`None` 表示無哨兵）。
    pub vtty_alive: Option<Arc<dyn Fn() -> Option<bool> + Send + Sync>>,
}

/// probe 之前的分類（順序 0–15，05 §11）：能決定時回 `Ok(結果)`，否則回 `Err(NeedProbe)`。
pub fn classify_pre_probe(core: &SessionCore, ctx: &SelfTestCtx) -> Result<PyDict, NeedProbe> {
    core.self_test_pre_probe(ctx)
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
