//! RPC 方法表與型別（P3-T7 stub）。
#![allow(unused_variables, dead_code)]

#[derive(Clone, Debug, PartialEq, Eq)]
pub enum Method {
    HealthPing,
    HealthStatus,
    DaemonStop,
    DaemonHandoffStop,
    DeviceList,
    DeviceRelease,
    DeviceAttach,
    SessionList,
    SessionGetState,
    SessionClear,
    SessionBind,
    SessionPin,
    SessionUnpin,
    SessionAttach,
    SessionSelfTest,
    SessionActivity,
    SessionRecover,
    SessionConsoleAttach,
    SessionConsoleDetach,
    SessionConsoleList,
    SessionInteractiveOpen,
    SessionInteractiveSend,
    SessionInteractiveStatus,
    SessionInteractiveClose,
    SessionLogStart,
    SessionLogStop,
    SessionLogStatus,
    AliasList,
    AliasSet,
    AliasAssign,
    AliasUnassign,
    CommandSubmit,
    CommandGet,
    CommandResultTail,
    CommandCancel,
    ResultTail,
    LogTailRaw,
    LogTailText,
    FilePush,
    FilePull,
    WalRange,
    WalReset,
    WalCurrentSeq,
    McuPatterns,
    McuStatus,
    EventRuleSet,
    EventRuleDelete,
    EventRuleList,
    EventRuleGet,
    EventComEnable,
    EventComDisable,
    EventComStatus,
    EventReset,
    EventReload,
    EventTail,
    Unknown(String),
}

impl Method {
    pub fn as_str(&self) -> &str {
        todo!("P3-T7: Method::as_str")
    }

    pub fn parse(s: &str) -> Method {
        todo!("P3-T7: Method::parse")
    }

    pub fn known() -> &'static [&'static str] {
        todo!("P3-T7: Method::known")
    }

    pub fn is_retryable_readonly(&self) -> bool {
        todo!("P3-T7: Method::is_retryable_readonly")
    }

    pub fn uses_long_timeout(&self) -> bool {
        todo!("P3-T7: Method::uses_long_timeout")
    }
}
