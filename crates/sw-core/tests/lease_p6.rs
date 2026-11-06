//! P6 Task 7：lease.rs 的 P6 追加（`is_human`／`human_client_id`、`PostClose`、`lease_context`）。

use sw_core::lease::*;
use sw_proto::pyjson::{dumps_dict, RPC_WIRE};

const S: u64 = 1_000_000_000;

fn lease(owner: &str, recovery: bool) -> InteractiveLease {
    InteractiveLease::new("id0".into(), owner.into(), "t".into(), 60.0, 0, recovery)
}

fn wire(d: &sw_proto::pyjson::PyDict) -> String {
    dumps_dict(d, &RPC_WIRE).trim_end_matches('\n').to_string()
}

#[test]
fn is_human_by_owner_prefix() {
    assert!(lease("human:c1", false).is_human());
    assert!(!lease("agent", false).is_human());
    assert!(!lease("humanx", false).is_human());
}

#[test]
fn human_client_id_strips_prefix() {
    assert_eq!(lease("human:c1", false).human_client_id(), Some("c1"));
    assert_eq!(lease("agent", false).human_client_id(), None);
    assert_eq!(lease("human:", false).human_client_id(), Some(""));
}

#[test]
fn new_lease_p6_fields_default() {
    let l = lease("human:c1", false);
    assert!(!l.suspended_human);
    assert_eq!(l.peer_lost_at, None);
}

#[test]
fn post_close_defaults() {
    let p = PostClose::default();
    assert!(!p.needs_resume && !p.clear_owner_after_resume && !p.replay_allowed);
    assert_eq!(p.dropped_stash_client, None);
}

#[test]
fn lease_state_default_is_empty() {
    let s = LeaseState::default();
    assert!(s.active.is_none() && s.stashed_human.is_none());
    assert!(s.tombstones.is_empty());
    assert_eq!(s.retained_human_owner, None);
    assert_eq!(s.retained_human_timeout_s, None);
}

#[test]
fn lease_context_shapes() {
    // 無 lease
    assert_eq!(
        wire(&lease_context(None, None, 100 * S)),
        r#"{"interactive_owner":null,"human_attached":false,"human_active":false,"recovery_mode":false}"#
    );
    // human idle（從未鍵入）
    let h = lease("human:c1", false);
    assert_eq!(
        wire(&lease_context(Some(&h), None, 100 * S)),
        r#"{"interactive_owner":"human:c1","human_attached":true,"human_active":false,"recovery_mode":false}"#
    );
    // human idle（逾 60 s）
    assert_eq!(
        wire(&lease_context(Some(&h), Some(10 * S), 71 * S)),
        r#"{"interactive_owner":"human:c1","human_attached":true,"human_active":false,"recovery_mode":false}"#
    );
    // human active（60 s 內，含邊界）
    assert_eq!(
        wire(&lease_context(Some(&h), Some(10 * S), 70 * S)),
        r#"{"interactive_owner":"human:c1","human_attached":true,"human_active":true,"recovery_mode":false}"#
    );
    // recovery（agent）
    let r = lease("agent", true);
    assert_eq!(
        wire(&lease_context(Some(&r), Some(10 * S), 11 * S)),
        r#"{"interactive_owner":"agent","human_attached":false,"human_active":false,"recovery_mode":true}"#
    );
}

#[test]
fn lease_context_never_contains_suspended_human() {
    let mut l = lease("agent", true);
    l.suspended_human = true;
    let d = lease_context(Some(&l), None, 0);
    assert!(d.get("suspended_human").is_none());
}
