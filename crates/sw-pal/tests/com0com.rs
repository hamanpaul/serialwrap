use std::path::Path;

use sw_pal::com0com::{install_pair, list_pairs_from_registry, parse_pairs};
#[cfg(feature = "test-fakes")]
use sw_pal::fakes::FakeRegistry;
use sw_pal::traits::PalErrorKind;

#[test]
fn parses_complete_com0com_pairs() {
    let pairs = parse_pairs(
        "CNCA0 PortName=COM20,EmuBR=yes\nCNCB0 PortName=COM21,EmuBR=yes\nCNCA1 PortName=COM30\n",
    );
    assert_eq!(pairs.len(), 1);
    assert_eq!(pairs[0].index, 0);
    assert_eq!(pairs[0].a_name, "COM20");
    assert_eq!(pairs[0].b_name, "COM21");
}

#[test]
fn validates_com_port_names() {
    let error = install_pair(Path::new("unused"), "COM20; whoami", "COM21")
        .expect_err("不合法的連接埠名稱須在執行 setupc 前拒絕");
    assert_eq!(error.kind, PalErrorKind::InvalidInput);
}

#[cfg(feature = "test-fakes")]
#[test]
fn lists_complete_pairs_from_registry_without_setupc() {
    let registry = FakeRegistry::new(vec![], &[]);
    registry.set_com0com_pairs(&[("COM20", "COM21"), ("COM30", "COM31")]);
    let pairs = list_pairs_from_registry(registry.as_ref()).expect("registry 列舉應成功");
    assert_eq!(pairs.len(), 2);
    assert_eq!(pairs[0].a_name, "COM20");
    assert_eq!(pairs[0].b_name, "COM21");
    assert_eq!(pairs[1].a_name, "COM30");
    assert_eq!(pairs[1].b_name, "COM31");
}

#[cfg(feature = "test-fakes")]
#[test]
fn registry_errors_are_returned_for_caller_to_handle() {
    let registry = FakeRegistry::new(vec![], &[]);
    registry.fail();
    let error = list_pairs_from_registry(registry.as_ref())
        .expect_err("registry 讀取失敗不得偽裝為空 pair 清單");
    assert_eq!(error.kind, PalErrorKind::Io);
}

#[test]
fn ignores_malformed_or_incomplete_setupc_rows() {
    let pairs = parse_pairs(
        "CNCA0 PortName=COM20,EmuBR=yes\nCNCB0 PortName=not-a-port\nCNCA1 PortName=COM31\nCNCB2 PortName=COM42,EmuBR=yes\nCNCAx PortName=COM40,EmuBR=yes\n",
    );
    assert!(pairs.is_empty(), "不完整或無效 pair 不得被視為已安裝：{pairs:?}");
}
