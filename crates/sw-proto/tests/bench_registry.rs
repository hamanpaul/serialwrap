//! `bench_registry`：`load_benches`／`resolve` 的錯誤訊息逐字對齊 `inventory/07-platform-mgmt.md` §9.1（P4-T17）。
//!
//! 每條 Python 基線錯誤訊息一案（`sw_core/bench_registry.py` 的驗證順序）。`BenchConfigError` 的 `code`
//! 為 §9.2 既有錯誤碼，本檔只斷言既有碼，不新增碼。

use std::collections::BTreeMap;

use sw_proto::bench_registry::{load_benches, resolve, BenchConfigError, BenchEntry};

/// 單一 `dut` 條目的 YAML（每個字串為一行，自動縮排 4 空格）。
fn dut_doc(lines: &[&str]) -> String {
    let mut s = String::from("benches:\n  dut:\n");
    for line in lines {
        s.push_str("    ");
        s.push_str(line);
        s.push('\n');
    }
    s
}

/// 五個必填欄位皆合法的 `dut` 條目，供其餘案例替換單一欄位。
const FULL: [&str; 5] = [
    "target: user@host",
    "remote_socket: /run/s",
    "local_port: 2222",
    "ssh_opts: []",
    "autossh: false",
];

fn with_field(field: &str, replacement: &str) -> String {
    let lines: Vec<&str> = FULL
        .iter()
        .map(|&line| {
            if line.split(':').next() == Some(field) {
                replacement
            } else {
                line
            }
        })
        .collect();
    dut_doc(&lines)
}

fn expect_invalid(yaml: &str, want: &str) {
    let err: BenchConfigError = load_benches(yaml).expect_err("預期 INVALID_BENCH_CONFIG 錯誤");
    assert_eq!(err.code, "INVALID_BENCH_CONFIG", "錯誤碼不符；yaml=\n{yaml}");
    assert_eq!(err.message, want, "錯誤訊息須逐字對齊 07 §9.1；yaml=\n{yaml}");
}

#[test]
fn root_not_object_is_rejected() {
    expect_invalid("- a\n- b\n", "benches.yaml 必須是 object");
}

#[test]
fn unknown_root_keys_are_rejected_sorted() {
    expect_invalid(
        "benches: {}\nzeta: 1\nalpha: 2\n",
        "benches.yaml 包含不支援根欄位 alpha, zeta；只能使用 benches",
    );
}

#[test]
fn benches_not_object_is_rejected() {
    expect_invalid("benches: [1]\n", "benches.yaml 的 benches 必須是 object");
}

#[test]
fn bench_code_not_string_is_rejected() {
    expect_invalid(
        "benches:\n  1:\n    target: user@host\n",
        "bench \"<root>\" 的 code 必須是字串",
    );
}

#[test]
fn bench_code_empty_is_rejected() {
    expect_invalid(
        "benches:\n  \"   \":\n    target: user@host\n",
        "bench \"<root>\" 的 code 不可為空",
    );
}

#[test]
fn bench_entry_not_object_is_rejected() {
    expect_invalid("benches:\n  dut: 1\n", "bench \"dut\" 必須是 object");
}

#[test]
fn extra_entry_fields_are_rejected_sorted() {
    // remote_port 不在 07 §9.1 的欄位清單內；與 alpha 一併列出並依字典序排列。
    let yaml = dut_doc(&[
        "target: user@host",
        "remote_socket: /run/s",
        "local_port: 2222",
        "ssh_opts: []",
        "autossh: false",
        "remote_port: 2200",
        "alpha: 1",
    ]);
    expect_invalid(
        &yaml,
        "bench \"dut\" 包含不支援欄位 alpha, remote_port；benches.yaml 必須維持 provider-neutral，只能使用 autossh, local_port, remote_socket, ssh_opts, target",
    );
}

#[test]
fn missing_required_entry_fields_are_rejected_sorted() {
    let yaml = dut_doc(&[
        "target: user@host",
        "remote_socket: /run/s",
        "local_port: 2222",
    ]);
    expect_invalid(&yaml, "bench \"dut\" 缺少必要欄位 autossh, ssh_opts");
}

#[test]
fn target_not_string_is_rejected() {
    expect_invalid(
        &with_field("target", "target: 1"),
        "bench \"dut\" 的 target 必須是字串",
    );
}

#[test]
fn target_empty_is_rejected() {
    expect_invalid(
        &with_field("target", "target: \"   \""),
        "bench \"dut\" 的 target 不可為空",
    );
}

#[test]
fn target_not_user_at_host_is_rejected() {
    expect_invalid(
        &with_field("target", "target: u@h@x"),
        "bench \"dut\" 的 target 必須是 user@host 形式",
    );
    expect_invalid(
        &with_field("target", "target: hostonly"),
        "bench \"dut\" 的 target 必須是 user@host 形式",
    );
}

#[test]
fn remote_socket_not_string_is_rejected() {
    expect_invalid(
        &with_field("remote_socket", "remote_socket: 1"),
        "bench \"dut\" 的 remote_socket 必須是字串",
    );
}

#[test]
fn remote_socket_empty_is_rejected() {
    expect_invalid(
        &with_field("remote_socket", "remote_socket: \"\""),
        "bench \"dut\" 的 remote_socket 不可為空",
    );
}

#[test]
fn local_port_not_integer_is_rejected() {
    expect_invalid(
        &with_field("local_port", "local_port: \"2222\""),
        "bench \"dut\" 的 local_port 必須是整數",
    );
    expect_invalid(
        &with_field("local_port", "local_port: true"),
        "bench \"dut\" 的 local_port 必須是整數",
    );
}

#[test]
fn local_port_out_of_range_is_rejected() {
    let want = "bench \"dut\" 的 local_port 必須介於 1 到 65535";
    expect_invalid(&with_field("local_port", "local_port: 0"), want);
    expect_invalid(&with_field("local_port", "local_port: 65536"), want);
}

#[test]
fn ssh_opts_not_string_array_is_rejected() {
    let want = "bench \"dut\" 的 ssh_opts 必須是字串陣列";
    expect_invalid(&with_field("ssh_opts", "ssh_opts: \"-v\""), want);
    expect_invalid(&with_field("ssh_opts", "ssh_opts: [1]"), want);
}

#[test]
fn autossh_not_bool_is_rejected() {
    expect_invalid(
        &with_field("autossh", "autossh: \"yes\""),
        "bench \"dut\" 的 autossh 必須是布林值",
    );
}

#[test]
fn resolve_unknown_code_is_rejected() {
    let benches: BTreeMap<String, BenchEntry> = BTreeMap::new();
    let err = resolve(&benches, "nope").expect_err("預期 UNKNOWN_BENCH_CODE 錯誤");
    assert_eq!(err.code, "UNKNOWN_BENCH_CODE");
    assert_eq!(err.message, "unknown bench code: nope");
}
