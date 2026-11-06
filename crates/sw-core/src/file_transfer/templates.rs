//! 檔案傳輸板端命令樣板（逐字承接 `sw_core/file_transfer.py`）。P8-T3 紅燈骨架。

pub const SENTINEL_BEGIN: &str = "===SW_XFER_BEGIN===";
pub const SENTINEL_END: &str = "===SW_XFER_END===";
pub const TOOL_PROBE_PAYLOAD: &str = "c3ctcHJvYmU=";
pub const DECODER_B64_SENTINEL: &str = "SW_XFER_DECODER_B64_42";
pub const DECODER_OPENSSL_SENTINEL: &str = "SW_XFER_DECODER_OPENSSL_42";
pub const ENCODER_B64_SENTINEL: &str = "SW_XFER_ENCODER_B64_42";
pub const ENCODER_OPENSSL_SENTINEL: &str = "SW_XFER_ENCODER_OPENSSL_42";
pub const CHUNK_SENTINEL: &str = "SW_XFER_CHUNK_42";
pub const MD5_SENTINEL: &str = "SW_XFER_MD5_42";
pub const MV_SENTINEL: &str = "SW_XFER_MV_42";
pub const CLEANUP_SENTINEL: &str = "SW_XFER_CLEANUP_42";
pub const ENCODER_FAILURE_SENTINEL: &str = "SW_XFER_ENCODER_FAILED_42";
pub const DECODERS: [&str; 2] = ["base64", "openssl"];
pub const ENCODERS: [&str; 2] = ["base64", "openssl"];
pub const ACK_MODES: [&str; 3] = ["auto", "echo", "none"];
pub const DEFAULT_CHUNK_SIZE: i128 = 512;
pub const DEFAULT_ECHO_SLICE_SIZE: usize = 64;
pub const DEFAULT_ECHO_TIMEOUT_S: f64 = 5.0;

#[derive(Debug, PartialEq, Eq)]
pub enum TemplateError {
    UnsupportedMarker(String),
    UnknownTool(String),
}

pub fn runtime_sentinel(_prefix: &str) -> String {
    todo!("P8-T3")
}
pub fn marker_expression(_marker: &str) -> Result<String, TemplateError> {
    todo!("P8-T3")
}
pub fn decoder_probe_command() -> String {
    todo!("P8-T3")
}
pub fn encoder_probe_command() -> String {
    todo!("P8-T3")
}
pub fn decoder_command_name(_decoder: &str) -> Result<&'static str, TemplateError> {
    todo!("P8-T3")
}
pub fn encoder_command_name(_encoder: &str) -> Result<&'static str, TemplateError> {
    todo!("P8-T3")
}
pub fn chunk_command(_encoded: &str, _decoder: &str, _op: &str, _path: &str) -> Result<String, TemplateError> {
    todo!("P8-T3")
}
pub fn md5_command(_path: &str) -> String {
    todo!("P8-T3")
}
pub fn mv_command(_source_path: &str, _remote_path: &str) -> String {
    todo!("P8-T3")
}
pub fn cleanup_command(_path: &str) -> String {
    todo!("P8-T3")
}
pub fn pull_command(_remote_path: &str, _encoder: &str) -> Result<String, TemplateError> {
    todo!("P8-T3")
}
pub fn split_chunks(_data: &[u8], _chunk_size: usize) -> Vec<&[u8]> {
    todo!("P8-T3")
}
pub fn line_budget_exceeded(_commands: &[&str], _budget: Option<i128>) -> bool {
    todo!("P8-T3")
}
pub fn b64encode(_data: &[u8]) -> String {
    todo!("P8-T3")
}
pub fn md5_hex(_data: &[u8]) -> String {
    todo!("P8-T3")
}
