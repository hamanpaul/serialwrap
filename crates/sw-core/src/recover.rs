//! session recover／force recover 的計畫與回覆組裝（P7 Task 9；`recover_session`、`_force_recover`，05 §12）。
//! 常數取自 `sw_proto::constants::{FORCE_RECOVER_POLLS, FORCE_RECOVER_POLL_S}`。force 輪詢由 SessionCore 的 `TimerEvent::ForceRecoverPoll` 驅動。

use sw_proto::pydict;
use sw_proto::pyjson::{PyDict, PyValue};

pub struct RecoverRequest {
    pub timeout_s: f64,
    pub force: bool,
}

#[derive(Debug, PartialEq)]
pub enum RecoverPlan {
    /// RELEASED／FLASHING_BUSY／SESSION_NOT_READY 等立即回覆。
    Reply(PyDict),
    /// 無 bridge 且裝置在、且無 drain 進行中：ATTACHING＋RequestAttach。
    Reattach,
    /// drain（RECOVERING）進行中：立即回 `{ok:false, error_code:"SESSION_RECOVERING", drain_stalled,
    /// drain_timeout_epoch, session}`，不掛 StopOp waiter、不 reattach（parity-amendments-2026-10-26 #57；
    /// 形狀照鎖內 `test_release_drain_timeout_shape`）。
    DrainBusy,
    /// ATTACHED 非 force：ExistingBridgeProbe{RecoverReprobe}。
    Reprobe,
    /// ATTACHED／非 READY 的 force：begin_stop(Clear) 後輪詢。
    Force,
    /// READY：CTRL_C／CTRL_D，兩鍵失敗後自動 Force。
    ReadyKeys,
}

/// 順序逐字照 `recover_session`（`session_manager.py:7528-7631`）：`_pending_drain` → RELEASED（state 或 by_id 在
/// released 集合）→ FLASHING_BUSY →（此時才）reset reprobe、`credentials_warned=false` → 無 bridge（裝置在 →
/// Reattach，否則 SESSION_NOT_READY）→ ATTACHED（force → Force，否則 Reprobe）→ 非 READY（force → Force，否則
/// SESSION_NOT_READY）→ READY → ReadyKeys（不看 force）。
///
/// `Reply` 不含 `session`：呼叫端於末尾補上 `to_public_dict()`（FLASHING_BUSY 的 `selector` 由 `com` 帶入）。
#[allow(clippy::too_many_arguments)]
pub fn plan_recover(
    state: &str,
    has_bridge: bool,
    draining: bool,
    device_present: bool,
    released: bool,
    flash_busy: bool,
    com: &str,
    req: &RecoverRequest,
) -> RecoverPlan {
    if draining {
        return RecoverPlan::DrainBusy;
    }
    if state == "RELEASED" || released {
        return RecoverPlan::Reply(pydict! {
            "ok" => true,
            "released" => true,
            "recommended_action" => "device_attach",
        });
    }
    if flash_busy {
        return RecoverPlan::Reply(pydict! {
            "ok" => false,
            "error_code" => "FLASHING_BUSY",
            "selector" => com,
        });
    }
    let not_ready = || RecoverPlan::Reply(pydict! { "ok" => false, "error_code" => "SESSION_NOT_READY" });
    if !has_bridge {
        return if device_present {
            RecoverPlan::Reattach
        } else {
            not_ready()
        };
    }
    match state {
        "ATTACHED" if req.force => RecoverPlan::Force,
        "ATTACHED" => RecoverPlan::Reprobe,
        "READY" => RecoverPlan::ReadyKeys,
        _ if req.force => RecoverPlan::Force,
        _ => not_ready(),
    }
}

/// 是否在回覆前重置 reprobe 進度與 `credentials_warned`：drain／RELEASED／FLASHING_BUSY 早退為 false，其餘 true。
pub fn plan_resets_progress(plan: &RecoverPlan) -> bool {
    match plan {
        RecoverPlan::DrainBusy => false,
        RecoverPlan::Reply(d) => !matches!(
            d.get("error_code"),
            Some(PyValue::Str(code)) if code == "FLASHING_BUSY"
        ) && d.get("released").is_none(),
        _ => true,
    }
}

/// REPROBE 的回覆（`recover_session` 的 reprobe 分支）；SC-34：`last_error`／`last_error_detail` 取自同一 session 快照。
/// 未恢復且有 bootloader 證據（`bootloader_match`，passthrough 由呼叫端傳 `None`）或 `last_error == "BOOTLOADER_STUCK"`
/// 時附 `classification`／`recommended_action`（命中時另附 `matched_prompt`）。
pub fn reprobe_reply(
    recovered: bool,
    last_error: Option<&str>,
    last_error_detail: Option<&str>,
    bootloader_match: Option<&str>,
    session: PyDict,
) -> PyDict {
    let mut out = pydict! {
        "ok" => true,
        "recovering" => false,
        "action" => "REPROBE",
        "recovered" => recovered,
    };
    out.insert("session", PyValue::Dict(session));
    if recovered {
        return out;
    }
    let opt = |v: Option<&str>| v.map_or(PyValue::Null, PyValue::from);
    out.insert("error_code", PyValue::from("SESSION_NOT_READY"));
    out.insert("last_error", opt(last_error));
    out.insert("last_error_detail", opt(last_error_detail));
    if bootloader_match.is_some() || last_error == Some("BOOTLOADER_STUCK") {
        out.insert("classification", PyValue::from("BOOTLOADER"));
        out.insert("recommended_action", PyValue::from("recover_interactive"));
        if let Some(matched) = bootloader_match {
            out.insert("matched_prompt", PyValue::from(matched));
        }
    }
    out
}

/// `_force_recover` 的回覆；逾時時 `session` 為 `get_session_state(...).get("session", {})`（缺則 `{}`）。
pub fn force_reply(recovered: bool, session: Option<PyDict>) -> PyDict {
    let session = PyValue::Dict(session.unwrap_or_default());
    if recovered {
        let mut out = pydict! {
            "ok" => true,
            "recovering" => false,
            "action" => "FORCE_CLEAR_REATTACH",
            "recovered" => true,
        };
        out.insert("session", session);
        return out;
    }
    let mut out = pydict! {
        "ok" => false,
        "error_code" => "FORCE_RECOVER_TIMEOUT",
        "action" => "FORCE_CLEAR_REATTACH",
        "recovered" => false,
    };
    out.insert("session", session);
    out
}
