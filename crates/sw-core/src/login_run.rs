//! Login run 協調與收斂（P7 Task 7；`session_manager.py:2496-2547`、`:2626-2729`、05 §3）。
//!
//! 提供 login run 的型別；結果收斂由 `SessionCore::login_finished` 與 `auto_reprobe_converge` 執行。

use crate::session_auth::AuthResolution;
use crate::transport_stall::StallWarning;

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
