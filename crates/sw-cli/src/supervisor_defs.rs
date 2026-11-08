//! 監管定義（systemd unit、Windows 工作排程器 XML、服務定義）與 `supervision_mode` 讀取（P4-T1 空殼＋輔助函式）。
//!
//! 監管定義的產生與安裝由 P4-T3／P4-T10 承接。本檔目前只承接 §0.2 的 `mode_str` 輔助函式。
use sw_proto::config_types::RuntimeConfig;
use sw_proto::yaml11::YValue;

/// 輸出 systemd user unit 文字，欄位順序與 Python 基線相同。
pub fn render_user_unit(exec_start: &str) -> String {
    format!(
        "[Unit]\nDescription=serialwrap UART broker daemon (user)\nAfter=default.target\n\n[Service]\nType=simple\nExecStart={exec_start}\nRestart=on-failure\nRestartSec=2\n\n[Install]\nWantedBy=default.target"
    )
}

/// 輸出與 Python 基線位元組一致的 systemd system unit（DF-10）。
pub fn render_system_unit(exec_start: &str, run_user: &str) -> String {
    format!(
        "[Unit]\nDescription=serialwrap UART broker daemon (system)\nAfter=multi-user.target\n\n[Service]\nType=simple\nUser={run_user}\nSupplementaryGroups=dialout\nRuntimeDirectory=serialwrap\nStateDirectory=serialwrap\nConfigurationDirectory=serialwrap\nUMask=0117\nEnvironment=SERIALWRAP_SOCKET_GROUP=dialout\nExecStart={exec_start}\nRestart=on-failure\nRestartSec=2\n\n[Install]\nWantedBy=multi-user.target"
    )
}

/// `cfg.mode()` 為 `YValue::Str(s)` 時回 `Some(s)`；其他型別回 `None`（對應 P3 Q-18 的字串化前狀態）。
pub fn mode_str(cfg: &RuntimeConfig) -> Option<String> {
    match cfg.mode() {
        Some(YValue::Str(s)) => Some(s.clone()),
        _ => None,
    }
}
