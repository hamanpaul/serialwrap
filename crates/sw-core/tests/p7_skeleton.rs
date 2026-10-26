//! P7 Task 0（08-slice23-login-events.md）：Slice 2.3 新增公開項可達性 —— 只綁定函式指標與型別名，不呼叫任何 todo! 本體。
//!
//! 偏離計畫原文：`tx_writer::FenceReject::StaleFsmGen` 改綁 06 既有的 `FenceReject::FsmMismatch`
//! （Task 0 Step 1 核對：06 已以 `FsmMismatch` 承載 fsm_gen 拒絕；floor 語意由 Task 7 修訂，不另起別名）。
use sw_core::event_engine::{counter, dispatcher, event_log, line_buffer, matcher, registry, schema, EventEngine};
use sw_core::{capture, login_fsm, login_run, mono, quiet_gate, recover, reprobe, self_test, transport_stall, tx_writer};

#[test]
fn p7_public_items_are_reachable() {
    let _: fn(&str) -> bool = login_fsm::is_prompt_timeout_error;
    let _: fn(&str, &[String]) -> Option<String> = login_fsm::matches_any_bootloader_prompt;
    let _: fn(&str) -> bool = transport_stall::is_transport_refinable_error;
    let _: fn(usize) -> line_buffer::LineBuffer = line_buffer::LineBuffer::new;
    let _: fn(f64) -> sw_core::Mono = mono::from_secs_f64;
    let _ = tx_writer::FenceReject::FsmMismatch;
    let _ = std::any::type_name::<(
        reprobe::ReprobeState,
        self_test::SelfTestFinal,
        recover::RecoverPlan,
        capture::CaptureObserver,
        login_run::LoginRunKind,
        quiet_gate::QuietState,
        schema::Rule,
        matcher::PatternMatcher,
        counter::Counter,
        registry::RuleRegistry,
        dispatcher::Dispatcher,
        event_log::EventLogger,
        EventEngine,
    )>();
}

#[test]
fn p7_pal_and_proto_items_are_reachable() {
    let _ = std::any::type_name::<(
        sw_pal::traits::HandlerSpec,
        sw_pal::traits::HandlerOutcome,
        sw_pal::traits::HandlerAbort,
        sw_pal::fakes::FakeHandlerSpawner,
        sw_pal::fakes::FakeUsb,
    )>();
    let _: &dyn Fn(&dyn sw_pal::traits::UsbTopology) = &|_u| {};
    let _: &dyn Fn(&dyn sw_pal::traits::HandlerSpawner) = &|_h| {};
    let _: &dyn Fn(&dyn sw_pal::traits::AppendFile) = &|_f| {};
    let _: fn(&str, sw_proto::pyre::PyFlags) -> Result<bool, sw_proto::pyre::CompileError> =
        sw_proto::pyre::redos::maybe_redos_suspicious;
    assert_eq!(sw_proto::constants::FORCE_RECOVER_POLLS, 10);
    assert_eq!(sw_proto::constants::FORCE_RECOVER_POLL_S, 1.0);
}

/// Task 0 本身交付的 `mono` 換算（非 todo!）。
#[test]
fn mono_secs_round_trip() {
    assert_eq!(mono::from_secs_f64(1.5), 1_500_000_000);
    assert_eq!(mono::from_secs_f64(0.0), 0);
    assert_eq!(mono::from_secs_f64(-3.0), 0);
    assert_eq!(mono::from_secs_f64(f64::NAN), 0);
    assert_eq!(mono::from_secs_f64(1e30), u64::MAX);
    assert_eq!(mono::as_secs_f64(2_500_000_000), 2.5);
    assert_eq!(mono::as_secs_f64(mono::from_secs_f64(180.0)), 180.0);
}
