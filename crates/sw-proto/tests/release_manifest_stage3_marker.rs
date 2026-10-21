//! P9-T16 審查 F2：stage 3 缺退役標記時，錯誤型別一律為 `RetirementInconsistent`。
//!
//! 先前 `parse_manifest` 在 delegate 判定先回 `Schema`，使「忘了寫 marker 且無 delegate」的真實 Stage 3 bundle
//! 被歸為結構錯誤。此處以獨立夾具鎖定正確型別，不改動 P9 既有的 `release_manifest_retirement.rs`。
use sw_proto::release_manifest::{ManifestError, ReleaseManifest};

#[test]
fn stage3_without_marker_and_without_delegate_is_retirement_inconsistent() {
    let text = include_str!("fixtures/stage3_no_marker_no_delegate.json");
    assert!(matches!(
        ReleaseManifest::parse(text),
        Err(ManifestError::RetirementInconsistent(
            "stage 3 requires retirement marker"
        ))
    ));
}

#[test]
fn stage3_without_marker_with_delegate_is_retirement_inconsistent() {
    let text = include_str!("fixtures/stage3_not_retired.json");
    assert!(matches!(
        ReleaseManifest::parse(text),
        Err(ManifestError::RetirementInconsistent(
            "stage 3 requires retirement marker"
        ))
    ));
}

#[test]
fn unretired_non_stage3_without_delegate_stays_schema_error() {
    // P3 既有行為不變：非 stage 3 的 manifest 缺 delegate 仍為 Schema。
    let text = include_str!("fixtures/stage1b_missing_delegate.json");
    assert!(matches!(
        ReleaseManifest::parse(text),
        Err(ManifestError::Schema(_))
    ));
}
