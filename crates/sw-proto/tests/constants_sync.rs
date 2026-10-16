mod common;

use std::collections::BTreeSet;

fn get_string_val(text: &str, key: &str) -> String {
    let pattern = format!("\"{}\":", key);
    let idx = text.find(&pattern).unwrap_or_else(|| panic!("key {key} not found"));
    let rest = &text[idx + pattern.len()..];
    let start_quote = rest.find('"').unwrap_or_else(|| panic!("value for {key} is not a string"));
    let val_rest = &rest[start_quote + 1..];
    let end_quote = val_rest.find('"').unwrap_or_else(|| panic!("unterminated string for {key}"));
    val_rest[..end_quote].to_string()
}

fn get_int_val(text: &str, key: &str) -> i64 {
    let pattern = format!("\"{}\":", key);
    let idx = text.find(&pattern).unwrap_or_else(|| panic!("key {key} not found"));
    let rest = text[idx + pattern.len()..].trim_start();
    let end = rest.find(|c: char| c == ',' || c == '\n' || c == '}' || c.is_whitespace()).unwrap_or(rest.len());
    rest[..end].parse::<i64>().unwrap_or_else(|e| panic!("failed to parse int for {key}: {e}"))
}

fn get_float_val(text: &str, key: &str) -> f64 {
    let pattern = format!("\"{}\":", key);
    let idx = text.find(&pattern).unwrap_or_else(|| panic!("key {key} not found"));
    let rest = text[idx + pattern.len()..].trim_start();
    let end = rest.find(|c: char| c == ',' || c == '\n' || c == '}' || c.is_whitespace()).unwrap_or(rest.len());
    rest[..end].parse::<f64>().unwrap_or_else(|e| panic!("failed to parse float for {key}: {e}"))
}

fn get_str_list(text: &str, key: &str) -> Vec<String> {
    let pattern = format!("\"{}\":", key);
    let idx = text.find(&pattern).unwrap_or_else(|| panic!("key {key} not found"));
    let rest = &text[idx + pattern.len()..];
    let start_bracket = rest.find('[').unwrap_or_else(|| panic!("value for {key} is not a list"));
    let list_rest = &rest[start_bracket + 1..];
    let end_bracket = list_rest.find(']').unwrap_or_else(|| panic!("unterminated list for {key}"));
    let list_content = &list_rest[..end_bracket];

    let mut out = Vec::new();
    let mut cur = list_content;
    while let Some(q1) = cur.find('"') {
        let after_q1 = &cur[q1 + 1..];
        let q2 = after_q1.find('"').unwrap_or_else(|| panic!("unterminated item string in list for {key}"));
        out.push(after_q1[..q2].to_string());
        cur = &after_q1[q2 + 1..];
    }
    out
}

fn to_set<'a>(it: impl IntoIterator<Item = &'a str>) -> BTreeSet<&'a str> {
    it.into_iter().collect()
}

#[test]
fn test_constants_sync_with_golden() {
    let p = common::data_root().join("contracts/golden/constants.json");
    let text = std::fs::read_to_string(&p).unwrap_or_else(|e| panic!("read {}: {e}", p.display()));

    // 1. DEFAULT_TCP_PORT_RAW_DEFAULT
    assert_eq!(
        sw_proto::constants::DEFAULT_TCP_PORT_RAW_DEFAULT,
        get_string_val(&text, "constants.DEFAULT_TCP_PORT_RAW_DEFAULT")
    );

    // 2. LOOPBACK_TCP_HOSTS
    let golden_loopback = get_str_list(&text, "constants.LOOPBACK_TCP_HOSTS");
    assert_eq!(
        to_set(sw_proto::constants::LOOPBACK_TCP_HOSTS.iter().copied()),
        to_set(golden_loopback.iter().map(String::as_str))
    );

    // 3. RETRYABLE_READONLY_METHODS
    let golden_retryable = get_str_list(&text, "client.RETRYABLE_READONLY_METHODS");
    assert_eq!(
        to_set(sw_proto::constants::RETRYABLE_READONLY_METHODS.iter().copied()),
        to_set(golden_retryable.iter().map(String::as_str))
    );
    assert_eq!(sw_proto::constants::RETRYABLE_READONLY_METHODS.len(), 11);

    // 4. RETRY_BACKOFF_BASE_S
    assert_eq!(
        sw_proto::constants::RETRY_BACKOFF_BASE_S,
        get_float_val(&text, "client.RETRY_BACKOFF_BASE_S")
    );

    // 5. RETRY_BACKOFF_MAX_S
    assert_eq!(
        sw_proto::constants::RETRY_BACKOFF_MAX_S,
        get_float_val(&text, "client.RETRY_BACKOFF_MAX_S")
    );

    // 6. TRANSIENT_ERROR_CODES
    let golden_transient = get_str_list(&text, "client.TRANSIENT_ERROR_CODES");
    assert_eq!(
        to_set(sw_proto::constants::TRANSIENT_ERROR_CODES.iter().copied()),
        to_set(golden_transient.iter().map(String::as_str))
    );

    // 7. PROBE_TIMEOUT_S
    assert_eq!(
        sw_proto::constants::PROBE_TIMEOUT_S,
        get_float_val(&text, "client.PROBE_TIMEOUT_S")
    );

    // 8. PROBE_DEADLINE_S
    assert_eq!(
        sw_proto::constants::PROBE_DEADLINE_S,
        get_float_val(&text, "client.PROBE_DEADLINE_S")
    );

    // 9. PROBE_METHODS
    let golden_probe_methods = get_str_list(&text, "client.PROBE_METHODS");
    assert_eq!(
        to_set(sw_proto::constants::PROBE_METHODS.iter().copied()),
        to_set(golden_probe_methods.iter().map(String::as_str))
    );

    // 10. RECV_CHUNK
    assert_eq!(
        sw_proto::constants::RECV_CHUNK as i64,
        get_int_val(&text, "client.RECV_CHUNK")
    );

    // 11. CLIENT_RESPONSE_MAX_BYTES
    assert_eq!(
        sw_proto::constants::CLIENT_RESPONSE_MAX_BYTES as i64,
        get_int_val(&text, "arch.CLIENT_RESPONSE_MAX_BYTES")
    );

    // 12. DEFAULT_RPC_TIMEOUT_S
    assert_eq!(
        sw_proto::constants::DEFAULT_RPC_TIMEOUT_S,
        get_float_val(&text, "cli.DEFAULT_RPC_TIMEOUT_S")
    );

    // 13. LONG_RPC_TIMEOUT_FLOOR_S
    assert_eq!(
        sw_proto::constants::LONG_RPC_TIMEOUT_FLOOR_S,
        get_float_val(&text, "cli.LONG_RPC_TIMEOUT_FLOOR_S")
    );

    // 14. LONG_RPC_METHODS
    let golden_long_methods = get_str_list(&text, "cli.LONG_RPC_METHODS");
    assert_eq!(
        to_set(sw_proto::constants::LONG_RPC_METHODS.iter().copied()),
        to_set(golden_long_methods.iter().map(String::as_str))
    );
    assert_eq!(sw_proto::constants::LONG_RPC_METHODS.len(), 4);

    // 15. ENDPOINT_PROBE_TIMEOUT_S
    assert_eq!(
        sw_proto::constants::ENDPOINT_PROBE_TIMEOUT_S,
        get_float_val(&text, "cli.ENDPOINT_PROBE_TIMEOUT_S")
    );

    // 16. SYSTEM_SOCKET
    assert_eq!(
        sw_proto::constants::SYSTEM_SOCKET,
        get_string_val(&text, "constants.SYSTEM_SOCKET")
    );

    // 17. COMPLETION_REGEX_MAX_BYTES
    assert_eq!(
        sw_proto::constants::COMPLETION_REGEX_MAX_BYTES as i64,
        get_int_val(&text, "command_completion.COMPLETION_REGEX_MAX_BYTES")
    );

    // 18. CMD_WARN_BYTES
    assert_eq!(
        sw_proto::constants::CMD_WARN_BYTES,
        get_int_val(&text, "arbiter.CMD_WARN_BYTES")
    );

    // 19. CMD_REJECT_BYTES
    assert_eq!(
        sw_proto::constants::CMD_REJECT_BYTES,
        get_int_val(&text, "arbiter.CMD_REJECT_BYTES")
    );

    // 20. FILE_DEFAULT_CHUNK_SIZE
    assert_eq!(
        sw_proto::constants::FILE_DEFAULT_CHUNK_SIZE,
        get_int_val(&text, "file_transfer.FILE_DEFAULT_CHUNK_SIZE")
    );

    // 21. FILE_ACK_MODES
    let golden_ack_modes = get_str_list(&text, "file_transfer.FILE_ACK_MODES");
    assert_eq!(
        to_set(sw_proto::constants::FILE_ACK_MODES.iter().copied()),
        to_set(golden_ack_modes.iter().map(String::as_str))
    );

    // 22. DELEGATE_DEPTH_ENV
    assert_eq!(
        sw_proto::constants::DELEGATE_DEPTH_ENV,
        get_string_val(&text, "arch.DELEGATE_DEPTH_ENV")
    );

    // 23. DELEGATE_ARGV_SHA_ENV
    assert_eq!(
        sw_proto::constants::DELEGATE_ARGV_SHA_ENV,
        get_string_val(&text, "arch.DELEGATE_ARGV_SHA_ENV")
    );

    // 24. MAX_NUMERIC_LOG_LEVEL_DIGITS
    assert_eq!(
        sw_proto::constants::MAX_NUMERIC_LOG_LEVEL_DIGITS as i64,
        get_int_val(&text, "cli_trace.MAX_NUMERIC_LOG_LEVEL_DIGITS")
    );
}
