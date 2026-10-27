//! P6 Task 6：`OwnershipState`（`uart_io.py` bridge 層 ownership 規則；07 Task 6 Step 1）。
//!
//! 對照 `tests/test_suspend_resume_reentrant.py`、`tests/test_refine_ownership.py`、`tests/test_interactive_raw.py`
//! 的 bridge 層斷言；`Mono` 是 `u64`。

use sw_core::human_console::ownership::*;

fn yes(_: &str) -> bool {
    true
}

#[test]
fn nested_double_suspend_resume_restores_owner() {
    let mut o = OwnershipState::default();
    o.set_interactive_owner(Some("human:c1"));
    o.suspend();
    o.suspend();
    assert!(!o.resume(&yes));
    assert_eq!(o.interactive_owner, None);
    assert!(o.resume(&yes));
    assert_eq!(o.interactive_owner.as_deref(), Some("human:c1"));
    assert!(!o.agent_active);
    assert_eq!(o.suspend_depth, 0);
}

#[test]
fn extra_unbalanced_resume_is_noop() {
    let mut o = OwnershipState::default();
    assert!(!o.resume(&yes));
    assert_eq!(o.suspend_depth, 0);
}

#[test]
fn human_owner_during_admission_block_goes_pending() {
    let mut o = OwnershipState::default();
    o.set_admission(false, &yes);
    o.set_interactive_owner(Some("human:c1"));
    assert_eq!(o.interactive_owner, None);
    assert_eq!(o.pending_owner.as_deref(), Some("human:c1"));
    assert!(o.set_admission(true, &yes));
    assert_eq!(o.interactive_owner.as_deref(), Some("human:c1"));
}

#[test]
fn detach_suspended_owner_abandons_suspend_but_drop_does_not() {
    let mut a = OwnershipState::default();
    a.set_interactive_owner(Some("human:c1"));
    a.suspend();
    let mut b = a.clone();
    a.on_detach("c1");
    assert_eq!((a.suspended_owner.clone(), a.agent_active, a.suspend_depth), (None, false, 0)); // #78
    b.on_drop("c1");
    assert_eq!((b.suspended_owner.clone(), b.agent_active, b.suspend_depth), (None, true, 1)); // #136
}

#[test]
fn route_raw_updates_last_human_input_only_on_raw() {
    let mut o = OwnershipState::default();
    o.set_interactive_owner(Some("human:c1"));
    assert_eq!(o.route_input("c1", 5), InputRoute::Raw { owner: "human:c1".into() });
    assert_eq!(o.last_human_input_at, Some(5));
    assert_eq!(o.route_input("c2", 6), InputRoute::Line);
    assert_eq!(o.last_human_input_at, Some(5));
}

#[test]
fn single_suspend_resume_restores_owner() {
    let mut o = OwnershipState::default();
    o.set_interactive_owner(Some("human:c1"));
    assert!(o.suspend());
    assert!(o.resume(&yes));
    assert_eq!(o.interactive_owner.as_deref(), Some("human:c1"));
}

#[test]
fn deferred_flushed_only_on_outermost_resume() {
    let mut o = OwnershipState::default();
    o.set_interactive_owner(Some("human:c1"));
    assert!(o.suspend());
    assert!(!o.suspend(), "depth 1→2 不是 0→1");
    assert!(!o.resume(&yes), "depth 2→1 不回放");
    assert_eq!(o.suspend_depth, 1);
}

#[test]
fn deep_nesting_balanced() {
    let mut o = OwnershipState::default();
    o.set_interactive_owner(Some("human:c1"));
    for _ in 0..5 {
        o.suspend();
    }
    let outer: Vec<bool> = (0..5).map(|_| o.resume(&yes)).collect();
    assert_eq!(outer, vec![false, false, false, false, true]);
    assert_eq!(o.interactive_owner.as_deref(), Some("human:c1"));
    assert_eq!(o.suspend_depth, 0);
}

#[test]
fn route_owner_with_admission_blocked_defers() {
    let mut o = OwnershipState::default();
    o.set_interactive_owner(Some("human:c1"));
    o.set_admission(false, &yes);
    assert_eq!(o.route_input("c1", 1), InputRoute::Defer);
    assert_eq!(o.last_human_input_at, None, "Defer 不更新 last_human_input_at");
}

#[test]
fn route_suspended_human_defers() {
    let mut o = OwnershipState::default();
    o.set_interactive_owner(Some("human:c1"));
    o.suspend();
    assert_eq!(o.route_input("c1", 1), InputRoute::Defer);
    assert_eq!(o.route_input("c2", 1), InputRoute::Line);
}

#[test]
fn route_flash_drops() {
    let mut o = OwnershipState::default();
    o.set_interactive_owner(Some("human:c1"));
    o.flash_mode = true;
    assert_eq!(o.route_input("c1", 1), InputRoute::DropFlash);
    assert_eq!(o.route_input("c2", 1), InputRoute::DropFlash);
}

#[test]
fn tcp_accept_first_gets_owner() {
    let mut o = OwnershipState::default();
    o.on_stream_accept("t1");
    assert_eq!(o.interactive_owner.as_deref(), Some("human:t1"));
}

#[test]
fn tcp_accept_during_admission_sets_pending_once() {
    let mut o = OwnershipState::default();
    o.set_admission(false, &yes);
    o.on_stream_accept("t1");
    o.on_stream_accept("t2");
    assert_eq!(o.pending_owner.as_deref(), Some("human:t1"));
    assert_eq!(o.interactive_owner, None);
}

#[test]
fn tcp_accept_during_suspend_becomes_suspended_owner() {
    let mut o = OwnershipState::default();
    o.suspend();
    o.on_stream_accept("t1");
    assert_eq!(o.suspended_owner.as_deref(), Some("human:t1")); // #134
    assert_eq!(o.interactive_owner, None);
}

#[test]
fn tcp_accept_with_existing_owner_is_line() {
    let mut o = OwnershipState::default();
    o.set_interactive_owner(Some("human:c1"));
    o.on_stream_accept("t2");
    assert_eq!(o.interactive_owner.as_deref(), Some("human:c1"));
    assert_eq!(o.route_input("t2", 1), InputRoute::Line);
}

#[test]
fn try_grant_refuses_when_pending_or_flash() {
    let mut a = OwnershipState::default();
    a.pending_owner = Some("human:p".into());
    assert!(!a.try_grant_if_idle("human:c1"));
    let mut b = OwnershipState::default();
    b.flash_mode = true;
    assert!(!b.try_grant_if_idle("human:c1"));
    let mut c = OwnershipState::default();
    assert!(c.try_grant_if_idle("human:c1"));
    assert_eq!(c.interactive_owner.as_deref(), Some("human:c1"));
}

#[test]
fn promote_pending_agent_owner_kept() {
    let mut o = OwnershipState::default();
    o.set_admission(false, &yes);
    assert!(o.set_interactive_owner(Some("agent")));
    assert_eq!(o.interactive_owner.as_deref(), Some("agent"), "非 human 不受 gate");
    assert_eq!(o.pending_owner, None);
}

#[test]
fn promote_pending_human_requires_client() {
    let mut o = OwnershipState::default();
    o.set_admission(false, &yes);
    o.set_interactive_owner(Some("human:gone"));
    o.set_admission(true, &|c: &str| c != "gone");
    assert_eq!(o.interactive_owner, None);
    assert_eq!(o.pending_owner, None);
}

#[test]
fn reset_for_stop_keeps_flash() {
    let mut o = OwnershipState::default();
    o.set_interactive_owner(Some("human:c1"));
    o.suspend();
    o.flash_mode = true;
    o.pending_owner = Some("human:p".into());
    o.set_admission(false, &yes);
    o.reset_for_stop();
    let expected = OwnershipState { flash_mode: true, ..OwnershipState::default() };
    assert_eq!(o, expected);
}

#[test]
fn set_owner_reports_change_only_when_owner_differs() {
    let mut o = OwnershipState::default();
    assert!(o.set_interactive_owner(Some("human:c1")));
    assert!(!o.set_interactive_owner(Some("human:c1")));
    assert!(o.set_interactive_owner(None));
    assert!(!o.set_interactive_owner(None));
}

#[test]
fn set_owner_none_clears_pending() {
    let mut o = OwnershipState::default();
    o.set_admission(false, &yes);
    o.set_interactive_owner(Some("human:c1"));
    o.set_interactive_owner(None);
    assert_eq!(o.pending_owner, None);
}

#[test]
fn prune_clears_owner_only() {
    let mut o = OwnershipState::default();
    o.set_interactive_owner(Some("human:c1"));
    o.suspend();
    o.resume(&yes);
    o.on_prune("c1");
    assert_eq!(o.interactive_owner, None);
    let mut s = OwnershipState::default();
    s.set_interactive_owner(Some("human:c1"));
    s.suspend();
    s.on_prune("c1");
    assert_eq!(s.suspended_owner.as_deref(), Some("human:c1"), "prune 不動 suspended owner");
}

#[test]
fn resume_promotes_pending_when_no_suspended_owner() {
    let mut o = OwnershipState::default();
    o.suspend();
    o.pending_owner = Some("human:p".into());
    assert!(o.resume(&yes));
    assert_eq!(o.interactive_owner.as_deref(), Some("human:p"));
    assert_eq!(o.pending_owner, None);
}
