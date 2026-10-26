//! `support::deferred_methods` 自測（parity-amendments-2026-10-26 #186，P6 審查偏離 (h)；07 增補 #24）。
//!
//! 06 的 `deferred_methods_return_method_not_found_without_side_effects` 於 P6 Task 10 改為只檢查
//! slice 晚於已交付切片的列；helper 先行交付且不依賴 `ServiceRig`。
#![cfg(feature = "test-fakes")]

mod support;

const SCOPE: &str = include_str!("../../../tests/acceptance/slice21_scope.yaml");

#[test]
fn deferred_methods_excludes_delivered_slices() {
    let all = support::deferred_methods(SCOPE, &["2.1"]);
    assert_eq!(all.len(), 15, "slice21_scope.yaml 的 deferred_methods 鎖定 15 項：{all:?}");
    assert_eq!(&all[..3], ["session.console_attach", "session.console_detach", "session.console_list"]);

    let after_22 = support::deferred_methods(SCOPE, &["2.1", "2.2"]);
    assert_eq!(after_22.len(), 12);
    assert!(after_22.iter().all(|m| !m.starts_with("session.console_")), "{after_22:?}");
    assert_eq!(after_22.first().map(String::as_str), Some("event.rule_set"));
    assert_eq!(after_22.last().map(String::as_str), Some("file.pull"));

    let after_24 = support::deferred_methods(SCOPE, &["2.1", "2.2", "2.3", "2.4"]);
    assert!(after_24.is_empty(), "{after_24:?}");
}

#[test]
fn deferred_methods_only_reads_deferred_section() {
    let yaml = "unsupported_catalog_methods:\n  - {method: daemon.handoff_stop, slice: \"2.4\", response: METHOD_NOT_FOUND}\n\
                deferred_methods:\n  - {method: a.b, slice: \"2.3\"}\n  - {method: c.d, slice: '2.2'}\n\
                unsupported_profile_features: [x]\n";
    assert_eq!(support::deferred_methods(yaml, &["2.1"]), ["a.b", "c.d"]);
    assert_eq!(support::deferred_methods(yaml, &["2.1", "2.2"]), ["a.b"]);
}
