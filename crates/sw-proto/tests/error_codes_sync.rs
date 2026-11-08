//! RPC／CLI 錯誤碼與 session last_error 命名空間同步測試（P3-T8）。
mod common;
use sw_proto::error_codes::ErrorCode;
use sw_proto::yaml11::{safe_load, YValue};

fn yaml(name: &str) -> YValue {
    let p = common::data_root().join("contracts").join(name);
    safe_load(&std::fs::read_to_string(p).expect("P0 contracts 缺少")).unwrap()
}

fn common_codes(val: &YValue) -> Vec<String> {
    let mut codes = Vec::new();
    if let YValue::Map(m) = val {
        if let Some(YValue::Seq(seq)) = m.get_str("codes") {
            for item in seq {
                if let YValue::Map(item_map) = item {
                    if let Some(YValue::Str(s)) = item_map.get_str("code") {
                        codes.push(s.clone());
                    }
                }
            }
        }
    }
    codes
}

#[test]
fn every_yaml_code_has_variant_and_meta_in_order() {
    let codes = common_codes(&yaml("error_codes.yaml")); // Vec<String>，依 YAML 順序
    let meta: Vec<&str> = ErrorCode::META.iter().map(|m| m.code).collect();
    assert_eq!(meta, codes.iter().map(String::as_str).collect::<Vec<_>>());
    for c in &codes {
        assert_eq!(
            ErrorCode::parse(c).map(|e| e.as_str()),
            Some(c.as_str())
        );
    }
}

#[test]
fn credentials_unresolved_is_not_an_rpc_error_code_variant() {
    assert!(ErrorCode::parse("CREDENTIALS_UNRESOLVED").is_none());
    assert!(sw_proto::session_last_errors::SessionLastError::parse("CREDENTIALS_UNRESOLVED").is_some());
}

#[test]
fn cli_codes_used_by_p3_exist_with_cli_interface() {
    use sw_proto::error_codes::Interface::*;
    for c in [
        "INVALID_ENDPOINT",
        "SOCKET_ERROR",
        "TIMEOUT",
        "EMPTY_RESPONSE",
        "INVALID_RESPONSE",
        "INVALID_BENCH_STATE",
        "BENCH_STATE_IO_ERROR",
        "BENCH_ENDPOINT_NOT_REMEMBERED",
        "INTERNAL_ERROR",
        "INVALID_ARGS",
        "INVALID_COMPLETION_REGEX",
        "UNKNOWN_PROFILE",
        "SAMPLE_READ_FAILED",
        "UNKNOWN_EVENT_CMD",
    ] {
        let m = ErrorCode::parse(c)
            .unwrap_or_else(|| panic!("{c} 不在 error_codes.yaml"))
            .meta();
        assert!(
            m.interfaces.iter().any(|i| matches!(i, CliClient | CliLocal)),
            "{c}"
        );
    }
}
