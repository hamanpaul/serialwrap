//! RX 判定 helper（逐字承接 `sw_core/file_transfer.py`）。P8-T3 紅燈骨架。

pub fn strip_command_echo<'a>(_text: &'a str, _command: Option<&str>) -> &'a str {
    todo!("P8-T3")
}
pub fn runtime_sentinel_seen(_text: &str, _sentinel: &str, _command: Option<&str>) -> bool {
    todo!("P8-T3")
}
pub fn failure_sentinel_seen(_text: &str, _sentinel: &str, _command: Option<&str>) -> bool {
    todo!("P8-T3")
}
pub fn probe_shell_error(_text: &str, _command: &str) -> bool {
    todo!("P8-T3")
}
pub fn extract_between_sentinels(_text: &str, _command: Option<&str>) -> Option<String> {
    todo!("P8-T3")
}
pub fn remote_md5_from_output(_raw: &str, _command: &str) -> Option<String> {
    todo!("P8-T3")
}
