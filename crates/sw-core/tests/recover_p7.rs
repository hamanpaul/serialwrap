//! P7 Task 9（08-slice23-login-events.md）：session recover／force recover。
//!
//! Python 基線：`session_manager.recover_session`（`:7528-7631`）與 `_force_recover`（`:7632-7655`）。
//! 回應形狀以 `contracts/golden/p7/recover_responses.ndjson`（CPython 3.12 擷取）為準。
//! 偏離計畫原文（P7 修訂）：分支順序以 Python 程式碼為準——`_pending_drain` 檢查在 RELEASED 之前，且 drain 早退
//! 不重置 reprobe 進度（計畫敘述把 drain 列在 reset 之後）；`plan_recover` 多收 `com`（FLASHING_BUSY 的 `selector`）。
#![cfg(feature = "test-fakes")]

mod common;
mod support;

use std::time::Duration;

use common::p7::golden_rows;
use support::*;
use sw_core::login_fsm::LoginOutcome;
use sw_core::mailbox::ControlEvent;
use sw_core::recover::*;
use sw_core::session_core::{Effect, SessionRequest, SessionState};
use sw_proto::pydict;
use sw_proto::pyjson::{dumps_dict, loads, PyDict, PyValue, RPC_WIRE};

fn req(force: bool) -> RecoverRequest {
    RecoverRequest {
        timeout_s: 2.0,
        force,
    }
}

fn plan(state: &str, bridge: bool, draining: bool, present: bool, released: bool, flash: bool, force: bool) -> RecoverPlan {
    plan_recover(state, bridge, draining, present, released, flash, "COM0", &req(force))
}

#[test]
fn plan_recover_order_matches_recover_session() {
    use RecoverPlan::*;
    // `_pending_drain` 先於 RELEASED／FLASHING（`:7534`）。
    assert_eq!(plan("RELEASED", false, true, true, true, false, false), DrainBusy);
    assert_eq!(plan("FLASHING", true, true, true, false, true, true), DrainBusy);
    let released = Reply(pydict! {"ok" => true, "released" => true, "recommended_action" => "device_attach"});
    assert_eq!(plan("RELEASED", false, false, false, true, false, true), released);
    // by_id 在 `_released_by_ids`（state 尚未是 RELEASED）同樣早退。
    assert_eq!(plan("READY", true, false, true, true, false, false), released);
    let flashing = Reply(pydict! {"ok" => false, "error_code" => "FLASHING_BUSY", "selector" => "COM0"});
    assert_eq!(plan("READY", true, false, true, false, true, true), flashing);
    let not_ready = Reply(pydict! {"ok" => false, "error_code" => "SESSION_NOT_READY"});
    assert_eq!(plan("DETACHED", false, false, true, false, false, false), Reattach);
    assert_eq!(plan("DETACHED", false, false, true, false, false, true), Reattach);
    assert_eq!(plan("DETACHED", false, false, false, false, false, true), not_ready);
    assert_eq!(plan("ATTACHED", true, false, true, false, false, true), Force);
    assert_eq!(plan("ATTACHED", true, false, true, false, false, false), Reprobe);
    assert_eq!(plan("ATTACHING", true, false, true, false, false, true), Force);
    assert_eq!(plan("RECOVERING", true, false, true, false, false, false), not_ready);
    assert_eq!(plan("READY", true, false, true, false, false, false), ReadyKeys);
    assert_eq!(plan("READY", true, false, true, false, false, true), ReadyKeys);
}

#[test]
fn plan_resets_progress_only_after_flash_gate() {
    use RecoverPlan::*;
    // `_reset_reprobe_progress_locked`／`credentials_warned = False` 位於 drain／RELEASED／FLASHING 早退之後（`:7557`）。
    for (p, want) in [
        (plan("ATTACHED", true, true, true, false, false, false), false),
        (plan("RELEASED", false, false, false, true, false, false), false),
        (plan("READY", true, false, true, false, true, false), false),
        (plan("DETACHED", false, false, false, false, false, false), true),
        (Reattach, true),
        (Reprobe, true),
        (Force, true),
        (ReadyKeys, true),
    ] {
        assert_eq!(plan_resets_progress(&p), want, "{p:?}");
    }
}

fn response_dict(text: &str) -> PyDict {
    match loads(text).expect("response JSON") {
        PyValue::Dict(d) => d,
        other => panic!("response 須為 object：{other:?}"),
    }
}

fn session_of(resp: &PyDict) -> PyDict {
    match resp.get("session") {
        Some(PyValue::Dict(s)) => s.clone(),
        _ => PyDict::new(),
    }
}

#[test]
fn recover_responses_match_python_golden_bytes() {
    let mut checked = 0;
    for row in golden_rows("recover_responses") {
        let id = row.id().to_string();
        let n: u32 = id.rsplit(':').next().unwrap().parse().unwrap();
        let want = row.str("response");
        let resp = response_dict(want);
        let session = session_of(&resp);
        let s = |k: &str| match session.get(k) {
            Some(PyValue::Str(v)) => Some(v.clone()),
            _ => None,
        };
        let got = match n {
            // 立即回覆：plan 的 Reply 末尾補 `session`（`to_public_dict()`）。
            3 | 4 | 6 | 17 => {
                let (state, bridge, present, released, flash) = match n {
                    3 => ("RELEASED", false, false, true, false),
                    4 => ("FLASHING", true, true, false, true),
                    _ => ("DETACHED", false, false, false, false),
                };
                let RecoverPlan::Reply(mut d) = plan(state, bridge, false, present, released, flash, false) else {
                    panic!("case {id} 應為立即回覆");
                };
                d.insert("session", PyValue::Dict(session.clone()));
                d
            }
            7..=12 => {
                let recovered = resp.get("recovered") == Some(&PyValue::Bool(true));
                let matched = resp.get("matched_prompt").and_then(PyValue::as_str).map(str::to_string);
                reprobe_reply(
                    recovered,
                    s("last_error").as_deref(),
                    s("last_error_detail").as_deref(),
                    matched.as_deref(),
                    session.clone(),
                )
            }
            14..=16 => {
                let recovered = resp.get("recovered") == Some(&PyValue::Bool(true));
                force_reply(recovered, Some(session.clone()))
            }
            // 1：manager selector 解析；2：drain（SessionCore `session_recovering_reply`）；5：REATTACH；
            // 13：STATE_CHANGED；18～24：READY 救援鍵（op_runner），由 06 `command_exec::recover_*` 覆蓋。
            _ => continue,
        };
        assert_eq!(dumps_dict(&got, &RPC_WIRE), want, "case {id}");
        checked += 1;
    }
    assert_eq!(checked, 13);
}

fn start_login(effs: &[Effect]) -> Option<(u64, u64)> {
    effs.iter().find_map(|e| match e {
        Effect::StartLogin { epoch, fsm_gen, .. } => Some((*epoch, *fsm_gen)),
        _ => None,
    })
}

/// ATTACHED 後讓自動重探失敗一次（`reprobe_attempts == 1`）。
fn attached_with_one_failed_reprobe() -> CoreHarness {
    let mut h = CoreHarness::new(SHELL_PROFILE);
    h.to_attached(Some("SHELL_PROMPT_TIMEOUT"));
    let mut fx = Vec::new();
    for _ in 0..6 {
        fx.extend(h.advance(Duration::from_secs(1)));
        if start_login(&fx).is_some() {
            break;
        }
    }
    let (epoch, gen) = start_login(&fx).expect("自動重探");
    h.feed(ControlEvent::LoginFinished {
        epoch,
        fsm_gen: gen,
        result: LoginOutcome::NotReady("SHELL_PROMPT_TIMEOUT".into()),
        after_rx_seq: None,
    });
    assert_eq!(h.public()["reprobe_attempts"], PyValue::from(1));
    h
}

#[test]
fn recover_resets_reprobe_progress() {
    // Python `recover_session`：FLASHING 早退之後一律 `_reset_reprobe_progress_locked`（attempts 歸零、可再自動重探）。
    let mut h = attached_with_one_failed_reprobe();
    let (_reply, fx) = h.rpc_deferred(SessionRequest::Recover {
        timeout_s: 2.0,
        force: false,
    });
    assert!(start_login(&fx).is_some(), "ATTACHED recover 走既有 bridge 再探");
    assert_eq!(h.public()["reprobe_attempts"], PyValue::from(0));
    assert_eq!(h.public()["reprobe_exhausted"], PyValue::Bool(false));
}

#[test]
fn flashing_busy_does_not_reset_progress() {
    let mut h = attached_with_one_failed_reprobe();
    h.force_state(SessionState::Flashing);
    let r = h
        .rpc(SessionRequest::Recover {
            timeout_s: 2.0,
            force: true,
        })
        .unwrap();
    assert_eq!(r["error_code"], PyValue::from("FLASHING_BUSY"));
    assert_eq!(h.public()["reprobe_attempts"], PyValue::from(1));
}

fn begin_drains(effs: &[Effect]) -> usize {
    effs.iter()
        .filter(|e| matches!(e, Effect::BeginDrain { .. }))
        .count()
}

/// force recover 的 clear 完成（drain 回條）→ 重新 attach → 登入 READY。
fn finish_clear_and_login_ready(h: &mut CoreHarness) {
    let (epoch, stop) = h.pending_stop();
    let fx = h.receipt(epoch, stop).feed();
    assert!(fx.iter().any(|e| matches!(e, Effect::Reattach { .. })), "clear 後 reattach");
    // manager `spawn_attach_with(after_clear)` → actor 收 `DeviceAttached` → OpenBridge。
    h.feed(ControlEvent::DeviceAttached {
        by_id: DEVICE_BY_ID.into(),
        real_path: REAL_PATH.into(),
    });
    let fx = h.bridge_ok();
    let (epoch, gen) = start_login(&fx).expect("reattach 後登入");
    h.feed(ControlEvent::LoginFinished {
        epoch,
        fsm_gen: gen,
        result: LoginOutcome::Ready,
        after_rx_seq: None,
    });
    assert_eq!(h.core.state(), SessionState::Ready);
}

#[test]
fn force_recover_replies_on_one_second_poll_grid() {
    // Python `_force_recover`：`clear_session`（有 bridge 時 `_await_drain` 等回條）返回後才
    // `for _ in range(10): time.sleep(1.0); if READY: return`——輪詢格以 clear 返回為起點（審查 a0cad06_4221ba2
    // a6b24f7-1 更正：drain 回條於 1.5 s、READY 同時 → Python 於 2.5 s 回覆；原期望 2.0 s 為偏離值）。
    let mut h = CoreHarness::new(SHELL_PROFILE);
    h.to_attached(Some("SHELL_PROMPT_TIMEOUT"));
    let (reply, fx) = h.rpc_deferred(SessionRequest::Recover {
        timeout_s: 2.0,
        force: true,
    });
    assert_eq!(begin_drains(&fx), 1);
    h.advance(Duration::from_millis(1500));
    finish_clear_and_login_ready(&mut h);
    assert!(reply.try_recv().is_err(), "READY 於 1.5 s，回覆須等到 clear 返回後第 1 秒的輪詢點");
    h.advance(Duration::from_millis(900));
    assert!(reply.try_recv().is_err(), "2.4 s 尚未到輪詢點（舊偏離值 2.0 s 不得回覆）");
    h.advance(Duration::from_millis(100));
    let r = reply.try_recv().expect("2.5 s 輪詢回覆").expect("ok");
    assert_eq!(
        r.0.keys().map(String::as_str).collect::<Vec<_>>(),
        ["ok", "recovering", "action", "recovered", "session"]
    );
    assert_eq!(r["recovered"], PyValue::Bool(true));
}

#[test]
fn force_recover_poll_grid_starts_after_clear_drain() {
    // 審查 a0cad06_4221ba2 a6b24f7-1 輸入 B：drain 回條逾時（3 s，`_await_drain` 回 None）、板子 11 s 才 READY。
    // Python 輪詢點 4…13 s → 12 s 回 `recovered:true`；舊實作輪詢點 1…10 s → 10 s 回 FORCE_RECOVER_TIMEOUT。
    let mut h = CoreHarness::new(SHELL_PROFILE);
    h.to_attached(Some("SHELL_PROMPT_TIMEOUT"));
    let (reply, fx) = h.rpc_deferred(SessionRequest::Recover {
        timeout_s: 2.0,
        force: true,
    });
    assert_eq!(begin_drains(&fx), 1);
    h.advance(Duration::from_millis(5_000));
    assert!(reply.try_recv().is_err());
    // 逾時後回條才到（5 s）→ reattach → 開埠，登入 11 s 才 READY。
    let (epoch, stop) = h.pending_stop();
    let fx = h.receipt(epoch, stop).feed();
    assert!(fx.iter().any(|e| matches!(e, Effect::Reattach { .. })), "clear 後 reattach");
    h.feed(ControlEvent::DeviceAttached {
        by_id: DEVICE_BY_ID.into(),
        real_path: REAL_PATH.into(),
    });
    let fx = h.bridge_ok();
    let (epoch, gen) = start_login(&fx).expect("reattach 後登入");
    h.advance(Duration::from_millis(6_000));
    assert!(reply.try_recv().is_err(), "10 s 不得以 FORCE_RECOVER_TIMEOUT 回覆（仍在 4…13 s 輪詢格內）");
    h.feed(ControlEvent::LoginFinished {
        epoch,
        fsm_gen: gen,
        result: LoginOutcome::Ready,
        after_rx_seq: None,
    });
    assert_eq!(h.core.state(), SessionState::Ready);
    h.advance(Duration::from_millis(900));
    assert!(reply.try_recv().is_err(), "11.9 s 尚未到輪詢點");
    h.advance(Duration::from_millis(100));
    let r = reply.try_recv().expect("12 s 輪詢回覆").expect("ok");
    assert_eq!(r["recovered"], PyValue::Bool(true));
    assert_eq!(r["action"], PyValue::from("FORCE_CLEAR_REATTACH"));
}

#[test]
fn duplicate_force_recover_joins_same_stop() {
    // 兩個並發 force：Python `recover_session` 先查 `_pending_drain`——第二個在第一個的 clear drain 期間立即回
    // `SESSION_RECOVERING`（不另起 drain、不掛輪詢）；只有一個 StopOp，第一個以自己的輪詢點回覆。
    let mut h = CoreHarness::new(SHELL_PROFILE);
    h.to_attached(Some("SHELL_PROMPT_TIMEOUT"));
    let (first, fx1) = h.rpc_deferred(SessionRequest::Recover {
        timeout_s: 2.0,
        force: true,
    });
    h.advance(Duration::from_millis(300));
    let (second, fx2) = h.rpc_deferred(SessionRequest::Recover {
        timeout_s: 2.0,
        force: true,
    });
    assert_eq!(begin_drains(&fx1) + begin_drains(&fx2), 1, "只有一個 StopOp");
    let r2 = second.try_recv().expect("drain 中立即回覆").expect("ok");
    assert_eq!(r2["error_code"], PyValue::from("SESSION_RECOVERING"));
    assert!(r2.get("drain_stalled").is_some() && r2.get("drain_timeout_epoch").is_some());
    finish_clear_and_login_ready(&mut h);
    assert!(first.try_recv().is_err(), "第一個等到 clear 返回（0.3 s）後第 1 秒輪詢點");
    h.advance(Duration::from_millis(700));
    assert!(first.try_recv().is_err(), "1.0 s 不是輪詢點（格以 clear 返回 0.3 s 起算）");
    h.advance(Duration::from_millis(300));
    let r1 = first.try_recv().expect("第一個於 1.3 s 輪詢回覆").expect("ok");
    assert_eq!(r1["recovered"], PyValue::Bool(true));
}

#[test]
fn force_recover_timeout_after_ten_polls_exact_shape() {
    let mut h = CoreHarness::new(SHELL_PROFILE);
    h.to_attached(Some("SHELL_PROMPT_TIMEOUT"));
    let (reply, _) = h.rpc_deferred(SessionRequest::Recover {
        timeout_s: 2.0,
        force: true,
    });
    // drain 回條未到：Python `_await_drain` 於 DRAIN_TIMEOUT_MS（3 s）回 None → clear 返回 → 輪詢 4…13 s。
    h.advance(Duration::from_millis(12_900));
    assert!(reply.try_recv().is_err(), "10 次輪詢前不回覆");
    h.advance(Duration::from_millis(100));
    let r = reply.try_recv().expect("第 10 次輪詢後回覆").expect("ok");
    assert_eq!(
        r.0.keys().map(String::as_str).collect::<Vec<_>>(),
        ["ok", "error_code", "action", "recovered", "session"]
    );
    assert_eq!(r["error_code"], PyValue::from("FORCE_RECOVER_TIMEOUT"));
}

#[test]
fn stale_force_recover_poll_is_ignored() {
    // 過期或不存在的等待者 id 的輪詢事件：不回覆、不影響現行等待者的輪詢計數。
    let mut h = CoreHarness::new(SHELL_PROFILE);
    h.to_attached(Some("SHELL_PROMPT_TIMEOUT"));
    let (reply, _) = h.rpc_deferred(SessionRequest::Recover {
        timeout_s: 2.0,
        force: true,
    });
    let fx = h.feed(ControlEvent::Timer(sw_core::mailbox::TimerEvent::ForceRecoverPoll {
        id: 999,
        n: 10,
    }));
    assert!(fx.is_empty());
    assert!(reply.try_recv().is_err());
    // drain 逾時 3 s 後才開始輪詢（同上）。
    h.advance(Duration::from_millis(12_900));
    assert!(reply.try_recv().is_err(), "仍依現行等待者自己的 10 次輪詢");
    h.advance(Duration::from_millis(100));
    assert_eq!(
        reply.try_recv().expect("逾時回覆").expect("ok")["error_code"],
        PyValue::from("FORCE_RECOVER_TIMEOUT")
    );
}

// --- 審查 a0cad06_4221ba2 a6b24f7-2：parity-amendments 必要列 #57／#117（`session_manager.py:7694-7797`）。

#[test]
fn recover_during_drain_returns_session_recovering_immediately() {
    // #57：`_pending_drain is not None` 為 recover_session 第一個分支——立即回 SESSION_RECOVERING＋drain 欄位，
    // 不掛 StopOp waiter、不開 bridge、不另起 drain；回條後是否 reattach 只依該 drain 的 kind（此處 clear）。
    let mut h = CoreHarness::new(SHELL_PROFILE);
    h.to_attached(Some("SHELL_PROMPT_TIMEOUT"));
    let (clear_reply, fx) = h.rpc_deferred(SessionRequest::Clear);
    assert_eq!(begin_drains(&fx), 1);
    for force in [false, true] {
        let (r, fx) = h.rpc_full(SessionRequest::Recover {
            timeout_s: 2.0,
            force,
        });
        let r = r.expect("drain 中 recover 立即回覆");
        assert_eq!(
            r.0.keys().map(String::as_str).collect::<Vec<_>>(),
            ["ok", "error_code", "drain_stalled", "drain_timeout_epoch", "session"],
            "force={force}"
        );
        assert_eq!(r["error_code"], PyValue::from("SESSION_RECOVERING"));
        assert!(
            !fx.iter().any(|e| matches!(
                e,
                Effect::OpenBridge { .. } | Effect::BeginDrain { .. } | Effect::Reattach { .. }
            )),
            "回條前不得重開端點或另起 drain（§11.3-4）"
        );
    }
    let (epoch, stop) = h.pending_stop();
    // recover 已同步回覆（rpc_full 斷言同步路徑）＝未掛 waiter；回條後 reattach 屬 clear。
    let fx = h.receipt(epoch, stop).feed();
    assert!(fx.iter().any(|e| matches!(e, Effect::Reattach { .. })), "clear 的回條照常 reattach");
    assert!(clear_reply.try_recv().is_ok(), "clear 的 waiter 於回條後回覆");
}

#[test]
fn recover_not_ready_non_force_returns_not_ready() {
    // #117：有 bridge、state 非 READY／ATTACHED（ATTACHING：登入進行中）且非 force → `SESSION_NOT_READY`＋session。
    let mut h = CoreHarness::new(SHELL_PROFILE);
    h.attach_device();
    let fx = h.bridge_ok();
    assert!(start_login(&fx).is_some());
    assert_eq!(h.core.state(), SessionState::Attaching);
    let (r, fx) = h.rpc_full(SessionRequest::Recover {
        timeout_s: 2.0,
        force: false,
    });
    let r = r.expect("ok");
    assert_eq!(
        r.0.keys().map(String::as_str).collect::<Vec<_>>(),
        ["ok", "error_code", "session"]
    );
    assert_eq!(r["ok"], PyValue::Bool(false));
    assert_eq!(r["error_code"], PyValue::from("SESSION_NOT_READY"));
    assert!(!fx.iter().any(|e| matches!(e, Effect::BeginDrain { .. } | Effect::OpenBridge { .. })));
    assert_eq!(h.core.state(), SessionState::Attaching);
}

#[test]
fn recover_resets_credentials_warned() {
    // #117／#140：手動 recover 為重新介入，`credentials_warned = False`——ATTACHED（CREDENTIALS_UNRESOLVED 已告警
    // 一次）recover 非 force → 再探重新解析帳密，仍未補齊 → 再告警一次。
    let h = ActorRig::new(CRED_PROFILE).attach();
    h.wait_state("ATTACHED");
    let warned = |h: &ActorRig| {
        h.stderr_lines()
            .iter()
            .filter(|l| l.contains("帳密解析未完成"))
            .count()
    };
    assert_eq!(warned(&h), 1);
    let r = h.request(SessionRequest::Recover {
        timeout_s: 2.0,
        force: false,
    });
    assert_eq!(r["action"], PyValue::from("REPROBE"), "{r:?}");
    assert_eq!(r["recovered"], PyValue::Bool(false));
    assert_eq!(r["last_error"], PyValue::from("CREDENTIALS_UNRESOLVED"));
    assert_eq!(warned(&h), 2, "recover 重置 credentials_warned 後再遇終態須再告警");
}
