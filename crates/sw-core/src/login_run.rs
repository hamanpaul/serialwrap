//! Login run 協調與收斂（P7 Task 7；`session_manager.py:2496-2547`、`:2626-2729`、05 §3）。
//!
//! 只放收斂函式；`SessionCore` 的 `active_login`、`Effect::CancelLogin`、`StartLogin.kind`、`LoginFinished.report`
//! 隨 Task 7 加入（Task 0 不改 06 既有 enum／struct）。P7 Task 0 骨架：本體為 `todo!("P7 Task 7")`。

use crate::login_fsm::LoginOutcome;
use crate::session_auth::AuthResolution;
use crate::session_core::{Effects, SessionCore};
use crate::transport_stall::StallWarning;
use crate::Mono;

/// 既有 bridge probe 的來源。
#[derive(Clone, Copy, Debug, PartialEq, Eq)]
pub enum ProbeOrigin {
    AttachSession,
    RecoverReprobe,
    AutoReprobe,
}

/// login run 的種類（不 derive Eq：含 f64）。
#[derive(Clone, Debug, PartialEq)]
pub enum LoginRunKind {
    AttachProbe { gen_before: u64 },
    ExistingBridgeProbe { origin: ProbeOrigin },
    DynamicAttach,
    RebootRecovery { hard_timeout_s: f64 },
}

/// 實際採用的入口；只有 `Ensure` 失敗會拆 bridge。
#[derive(Clone, Copy, Debug, PartialEq, Eq)]
pub enum EntryUsed {
    Ensure,
    Probe,
}

/// run 結束時隨 `LoginFinished` 回報的附帶資訊。
#[derive(Clone, Debug, PartialEq)]
pub struct LoginReport {
    pub kind: LoginRunKind,
    pub entry: Option<EntryUsed>,
    pub detail: Option<String>,
    pub stall_warning: Option<StallWarning>,
    pub credentials: Option<AuthResolution>,
}

/// 依 08 Task 7 收斂表套用 run 結果（`(epoch, fsm_gen)` 不符 `active_login` 時不改狀態）。
pub fn apply_login_finished(
    _core: &mut SessionCore,
    _epoch: u64,
    _fsm_gen: u64,
    _result: LoginOutcome,
    _report: LoginReport,
    _now: Mono,
    _fx: &mut Effects,
) {
    todo!("P7 Task 7")
}
