mod common;

use sw_proto::rpc_types::Method;

fn set<'a>(it: impl IntoIterator<Item = &'a str>) -> std::collections::BTreeSet<&'a str> {
    it.into_iter().collect()
}

#[test]
fn known_has_55_unique() {
    let k = Method::known();
    assert_eq!(k.len(), 55);
    let s: std::collections::BTreeSet<_> = k.iter().collect();
    assert_eq!(s.len(), 55);
    assert!(!Method::DaemonHandoffStop.is_retryable_readonly() && !Method::DaemonHandoffStop.uses_long_timeout());
    for m in k {
        assert_eq!(Method::parse(m).as_str(), *m);
    }
    assert_eq!(Method::parse("x.y"), Method::Unknown("x.y".into()));
}

#[test]
fn retry_whitelist_is_exactly_11() {
    let r = set(Method::known().iter().filter(|m| Method::parse(m).is_retryable_readonly()).copied());
    assert_eq!(r, set(sw_proto::constants::RETRYABLE_READONLY_METHODS));
    assert_eq!(r.len(), 11);
    assert_eq!(
        set(sw_proto::constants::RETRYABLE_READONLY_METHODS).len(),
        sw_proto::constants::RETRYABLE_READONLY_METHODS.len()
    );
    assert!(!Method::SessionInteractiveStatus.is_retryable_readonly());
}

// 測試名稱為 l1_required.lock.json 凍結的 nodeid，不得改名；P1 主線（#123 延伸）把
// device.release／device.attach／session.clear／session.bind 併入長操作，集合由 4 增為 8。
#[test]
fn long_timeout_is_exactly_4() {
    let l = set(Method::known().iter().filter(|m| Method::parse(m).uses_long_timeout()).copied());
    assert_eq!(l, set(sw_proto::constants::LONG_RPC_METHODS));
    assert_eq!(l.len(), 8);
}

#[test]
fn known_equals_p0_rpc_schema_methods() {
    let schema_methods = common::rpc_schema_methods();
    assert_eq!(
        set(Method::known().iter().copied()),
        schema_methods.iter().map(String::as_str).collect()
    );
}
