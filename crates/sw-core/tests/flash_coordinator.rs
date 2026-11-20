#![cfg(feature = "test-fakes")]
//! P8 Task 9：`/dev/ttyMCU` 端點 thread 與認線協調（沉默、sync 回放、pump、holder-probe、baud 鏡射）。
//!
//! 對應 `docs/rust-refactor/plans/09-slice24-transfer-flash.md` Task 9（行為 1～8、Step 1 程式與「其餘族」表）；
//! Python 基線：`sw_core/flash_endpoint.py`（`FlashEndpoint._loop`、`detect_mcu_line`、`pump_endpoint_to_sink`）、
//! `sw_core/service.py`（`_BridgeProbe`、`_flash_rx_observer`、`_on_flash_open`、`_watch_flasher_disconnect`）。
//! 「送給 MCU 的 bytes」以 FakeDevice 實際寫出為準（`serial_tx` 另與 WAL TX 對帳），「送給 flasher 的 bytes」以端點
//! master 的寫入紀錄為準（CLAUDE.md MCU 坑 1～3）。

mod support;

use std::time::{Duration, Instant};

use support::flash_rig::{speed, wait_real, FlashRig, Mcu, ACK, SYNC};
use sw_core::flash_endpoint::detect::{detect_mcu_line, DetectResult, DetectStatus, ProbeTransport};
use sw_core::mcu_patterns::McuPatternRegistry;
use sw_core::tx_writer::FlashMode;
use sw_pal::fakes::serial::FakeWriteStep;
use sw_pal::traits::HolderScanStatus;

struct Scripted(Vec<(&'static str, bool)>, Vec<String>);
impl ProbeTransport for Scripted {
    fn probe(&mut self, by_id: &str, _p: &[u8], _e: &[u8], _t: u64) -> bool {
        self.1.push(by_id.to_string());
        self.0.iter().find(|(b, _)| *b == by_id).map(|x| x.1).unwrap_or(false)
    }
}

/// 一條 passthrough MCU 線（COM0）。
fn one_line(mcu: Mcu) -> FlashRig {
    let rig = FlashRig::posix_fake();
    rig.add_passthrough("COM0", "by-id/mcu", mcu);
    rig
}

/// COM0 進 FLASHING（flasher 送 sync、MCU 回 ACK）。
fn flashing(mcu: Mcu) -> FlashRig {
    let rig = one_line(mcu);
    rig.flasher_write(SYNC);
    rig.run_until_flashing("COM0");
    rig
}

// ---------------------------------------------------------------------------
// 計畫 Step 1
// ---------------------------------------------------------------------------

#[test]
fn detect_excludes_command_capable_and_flags_ambiguity() {
    let reg = McuPatternRegistry::default_registry();
    let cands = vec![
        FlashRig::cand("COM0", Some("a"), false),
        FlashRig::cand("COM1", Some("b"), true),
        FlashRig::cand("COM2", Some("c"), false),
    ];
    let mut t = Scripted(vec![("a", true), ("b", true), ("c", true)], vec![]);
    let r = detect_mcu_line(&cands, &reg, &mut t);
    assert_eq!(
        r,
        DetectResult { status: DetectStatus::Ambiguous, by_id: None, family: None, hits: vec!["a".into(), "c".into()] }
    );
    assert_eq!(t.1, vec!["a", "c"], "command_capable 不得被 probe");
}

#[test]
fn sync_bytes_are_flashers_own_and_ack_replayed_once() {
    let rig = FlashRig::posix_fake(); // FakeSerial＋fake PTY＋FakeClock
    rig.add_passthrough("COM0", "by-id/mcu", Mcu::ti_sbl()); // 收到 55 55 回 00 CC
    rig.flasher_write(b"\x55\x55");
    rig.run_until_flashing("COM0");
    assert_eq!(rig.serial_tx("COM0"), vec![b"\x55\x55".to_vec()], "只送 flasher 自己的 sync，不另注入");
    assert_eq!(rig.master_writes(), vec![b"\x00\xcc".to_vec()], "MCU 回應整段回放給 flasher");
    rig.flasher_write(b"\x03\x10\x20");
    rig.pump_once();
    assert_eq!(rig.serial_tx("COM0").last().unwrap(), b"\x03\x10\x20");
    assert!(rig.wal_tx("COM0").iter().all(|r| r.source == "flash"));
}

#[test]
fn endpoint_never_writes_when_unbridged() {
    let rig = FlashRig::posix_fake();
    rig.add_passthrough("COM0", "by-id/mcu", Mcu::silent());
    rig.advance_secs(30.0);
    assert!(rig.master_writes().is_empty());
    rig.flasher_write(b"\x55\x55"); // 無 ACK → none
    rig.run_detection();
    assert!(rig.master_writes().is_empty(), "未匹配：端點保持沉默");
    assert_eq!(rig.last_detect_json(), r#"{"status":"none","com":null,"family":null,"hits":[]}"#);
    assert_eq!(rig.state("COM0"), "ATTACHED");
}

// R2-F5：單次 flash 結束（holder gone）只取消該次 FlashRunCancel，常駐端點必須繼續服務下一次燒錄
#[test]
fn two_consecutive_flashes_same_endpoint() {
    let rig = FlashRig::posix_fake();
    rig.add_passthrough("COM0", "by-id/mcu", Mcu::ti_sbl());
    rig.holder_sequence(&[vec![4242], vec![4242], vec![]]); // 第一次 flash：armed 後歸零
    rig.flasher_write(b"\x55\x55");
    rig.run_until_flashing("COM0");
    let run1 = rig.coord().current_run_for_test().expect("第一次 flash 有 run");
    rig.advance_secs(1.5); // holder watcher 第 3 次 poll → 結束
    rig.run_until_state("COM0", "ATTACHED");
    assert!(run1.is_cancelled(), "holder gone 取消本次 run");
    assert!(!rig.endpoint_shutdown().is_set(), "holder gone 不得設 EndpointShutdown");
    assert!(rig.endpoint().thread_alive_for_test(), "端點 thread 必須仍在服務");
    wait_real("current_run 清回 None", || rig.coord().current_run_for_test().is_none());
    assert!(rig.ttymcu_link_exists());
    assert_eq!(rig.events_of("COM0", "FlashHolderGone").len(), 1);

    rig.holder_sequence(&[vec![4343], vec![4343], vec![]]); // 第二次 flash（新 flasher）
    rig.flasher_write(b"\x55\x55");
    rig.run_until_flashing("COM0");
    let run2 = rig.coord().current_run_for_test().expect("第二次 flash 有新 run");
    assert!(!run2.is_cancelled(), "第二次 run 為新建值，不沿用第一次已取消的旗標");
    assert_eq!(rig.serial_tx("COM0"), vec![b"\x55\x55".to_vec(), b"\x55\x55".to_vec()], "第二次 sync 仍轉送 MCU");
    assert_eq!(rig.master_writes(), vec![b"\x00\xcc".to_vec(), b"\x00\xcc".to_vec()], "第二次 ACK 仍回放");
    let starts = rig.events_of("COM0", "FlashSyncProbeStart");
    assert_eq!(starts.len(), 2);
    assert_ne!(starts[0].probe_id, starts[1].probe_id);
    rig.flasher_write(b"\x03\x10\x20");
    rig.pump_once();
    assert_eq!(rig.serial_tx("COM0").last().unwrap(), b"\x03\x10\x20");
    rig.advance_secs(1.5);
    rig.run_until_state("COM0", "ATTACHED");
    wait_real("第二次 FlashHolderGone", || rig.events_of("COM0", "FlashHolderGone").len() == 2);
    assert!(rig.endpoint().thread_alive_for_test());
    let r = rig.stop_endpoint();
    assert_eq!((r.joined, r.unlinked), (true, true));
    assert!(r.stalled_threads.is_empty());
}

// R2-F5：daemon 停止恰在第二次 flash 開始時到達——本次 run 必須立即視為取消，零 TX、不進 FLASHING、stop 正常收斂
#[test]
fn shutdown_at_second_flash_start() {
    let rig = FlashRig::posix_fake();
    rig.add_passthrough("COM0", "by-id/mcu", Mcu::ti_sbl());
    rig.holder_sequence(&[vec![4242], vec![4242], vec![]]);
    rig.flasher_write(b"\x55\x55");
    rig.run_until_flashing("COM0");
    rig.advance_secs(1.5);
    rig.run_until_state("COM0", "ATTACHED"); // 第一次 flash 正常結束
    wait_real("第一次 on_flash_open 返回", || rig.coord().opens_completed_for_test() == 1);
    // hook：第二次 on_flash_open 登記 current_run 之後、第一個 ProbeTransport::probe 之前呼叫 stop()（另一 thread）
    let stopper = rig.stop_endpoint_on_next_run_registered();
    rig.flasher_write(b"\x55\x55");
    let r = stopper.join_within(Duration::from_secs(2));
    assert_eq!((r.joined, r.unlinked), (true, true), "stop 在 2.0 s 內返回並收斂");
    assert!(r.stalled_threads.is_empty());
    assert!(rig.endpoint_shutdown().is_set());
    assert_eq!(rig.serial_tx("COM0"), vec![b"\x55\x55".to_vec()], "第二次 sync 零 TX");
    assert_eq!(rig.master_writes(), vec![b"\x00\xcc".to_vec()], "第二次不回放 ACK");
    assert_eq!(rig.events_of("COM0", "FlashSyncProbeStart").len(), 1, "第二次未開始 probe");
    assert_eq!(rig.events_of("COM0", "FlashSyncMatched").len(), 1);
    assert_eq!(rig.events_of("COM0", "FlashHolderGone").len(), 1);
    assert_eq!(rig.state("COM0"), "ATTACHED");
    assert_eq!(rig.flash_mode("COM0"), "Off");
    assert!(!rig.flash_critical("COM0"), "候選已 unmark");
    assert!(!rig.ttymcu_link_exists());
}

// ---------------------------------------------------------------------------
// 端點 thread（行為 1）
// ---------------------------------------------------------------------------

#[test]
fn empty_first_read_falls_back_to_pattern_probe() {
    let rig = one_line(Mcu::ti_sbl());
    rig.master_fail_next_read(); // 可讀後首讀錯誤 → first=b""
    rig.run_until_flashing("COM0");
    assert_eq!(rig.serial_tx("COM0"), vec![b"\x55\x55".to_vec()], "first 為空時改送 pattern probe bytes");
    assert!(rig.endpoint().thread_alive_for_test(), "讀取錯誤不結束端點 thread");
}

#[test]
fn wait_readable_error_ends_thread() {
    let rig = one_line(Mcu::ti_sbl());
    rig.master_fail_wait();
    wait_real("端點 thread 結束", || !rig.endpoint().thread_alive_for_test());
    assert!(
        rig.warnings().iter().any(|w| w.contains("ttyMCU")),
        "等待失敗須 WARNING：{:?}",
        rig.warnings()
    );
    assert_eq!(rig.coord().opens_completed_for_test(), 0, "未呼叫 on_flash_open");
    assert!(rig.events_of("COM0", "FlashSyncProbeStart").is_empty());
    assert!(rig.serial_tx("COM0").is_empty());
}

#[test]
fn second_open_while_active_ignored() {
    let rig = flashing(Mcu::ti_sbl());
    rig.flasher_write(b"\x03\x10\x20");
    rig.pump_once();
    assert_eq!(rig.events_of("COM0", "FlashSyncProbeStart").len(), 1, "flash 中的寫入不重入 on_flash_open");
    assert_eq!(rig.coord().opens_completed_for_test(), 0);
    assert_eq!(rig.serial_tx("COM0"), vec![b"\x55\x55".to_vec(), b"\x03\x10\x20".to_vec()]);
}

#[test]
fn endpoint_thread_survives_on_flash_open_panic() {
    let rig = one_line(Mcu::ti_sbl());
    rig.coord().set_run_registered_hook_for_test(Box::new(|| panic!("注入：on_flash_open panic")));
    rig.flasher_write(SYNC);
    wait_real("panic 後 on_flash_open 收尾", || rig.coord().opens_completed_for_test() == 1);
    assert!(rig.endpoint().thread_alive_for_test(), "panic 不得殺死端點 thread（C1）");
    assert!(rig.coord().current_run_for_test().is_none());
    assert!(!rig.flash_critical("COM0"));
    assert!(rig.serial_tx("COM0").is_empty(), "panic 發生在 probe 之前：零 TX");
    rig.flasher_write(SYNC);
    rig.run_until_flashing("COM0");
    assert_eq!(rig.serial_tx("COM0"), vec![SYNC.to_vec()]);
}

#[test]
fn windows_caps_no_endpoint() {
    let rig = FlashRig::windows_fake();
    rig.add_passthrough("COM0", "by-id/mcu", Mcu::ti_sbl());
    assert!(!rig.endpoint_started(), "caps.flash_endpoint=false → Ok(None)");
    assert!(!rig.ttymcu_link_exists());
    assert_eq!(rig.pty_creates(), 0, "不建立端點 PTY");
}

#[test]
fn endpoint_start_sets_raw_and_links_slave() {
    let rig = one_line(Mcu::silent());
    assert!(rig.ttymcu_link_exists());
    assert_eq!(rig.raw_calls(), vec![rig.endpoint_slave_path()], "start 對端點 slave 套 raw");
    assert!(!rig.endpoint().is_flashing());
}

// ---------------------------------------------------------------------------
// 認線 probe（行為 2～4）
// ---------------------------------------------------------------------------

#[test]
fn candidate_without_by_id_is_skipped() {
    let reg = McuPatternRegistry::default_registry();
    let cands = vec![FlashRig::cand("COM0", None, false), FlashRig::cand("COM1", Some("b"), false)];
    let mut t = Scripted(vec![("b", true)], vec![]);
    let r = detect_mcu_line(&cands, &reg, &mut t);
    assert_eq!(t.1, vec!["b"], "by_id 為 None 的候選不呼叫 probe");
    assert_eq!(r.status, DetectStatus::Matched);
    assert_eq!(r.by_id.as_deref(), Some("b"));
    assert_eq!(r.family.as_deref(), Some("ti-cc26xx"));
    assert!(r.hits.is_empty());
}

#[test]
fn detect_none_when_nothing_hits() {
    let reg = McuPatternRegistry::default_registry();
    let cands = vec![FlashRig::cand("COM0", Some("a"), false)];
    let mut t = Scripted(vec![("a", false)], vec![]);
    let r = detect_mcu_line(&cands, &reg, &mut t);
    assert_eq!(r, DetectResult { status: DetectStatus::None, by_id: None, family: None, hits: vec![] });
}

#[test]
fn ack_substring_not_prefix() {
    let rig = one_line(Mcu::replying(b"\xff\x00\xcc\x01"));
    rig.flasher_write(SYNC);
    rig.run_until_flashing("COM0");
    assert_eq!(rig.master_writes(), vec![b"\xff\x00\xcc\x01".to_vec()], "expect 為子字串即命中，回放整段");
}

#[test]
fn rx_buffer_keeps_last_4096() {
    let rig = one_line(Mcu::ti_sbl().flood_before_ack(5000));
    rig.flasher_write(SYNC);
    rig.run_until_flashing("COM0");
    let w = rig.master_writes();
    assert_eq!(w.len(), 1);
    assert_eq!(w[0].len(), 4096, "probe 緩衝只保留最後 4096 bytes");
    assert!(w[0].ends_with(ACK));
}

#[test]
fn probe_timeout_uses_pattern_timeout_ms() {
    let rig = one_line(Mcu::ti_sbl().delayed(Duration::from_millis(520)));
    rig.flasher_write(SYNC);
    rig.run_detection();
    assert!(rig.coord().last_detect().is_some_and(|d| d.status == DetectStatus::None));
    let starts = rig.events_of("COM0", "FlashSyncProbeStart");
    let released = rig.events_of("COM0", "FlashProbeReleased");
    assert_eq!(starts.len(), 1);
    assert_eq!(released.len(), 1, "逾時送 FlashProbeReleased");
    assert_eq!(released[0].probe_id, starts[0].probe_id);
    assert!(rig.events_of("COM0", "FlashSyncMatched").is_empty());
    assert!(rig.master_writes().is_empty());
    assert_eq!(rig.flash_mode_of("COM0"), FlashMode::Off);
    assert_eq!(rig.state("COM0"), "ATTACHED");
}

#[test]
fn held_gate_kept_during_ambiguity_scan() {
    let rig = FlashRig::posix_fake();
    rig.add_passthrough("COM0", "by-id/mcu-a", Mcu::ti_sbl());
    rig.add_passthrough("COM1", "by-id/mcu-b", Mcu::silent());
    rig.flasher_write(SYNC);
    rig.drive_until("COM1 probe 等待中", |r| {
        !r.events_of("COM1", "FlashSyncProbeStart").is_empty() && r.clock.waiters() > 0
    });
    assert!(matches!(rig.flash_mode_of("COM0"), FlashMode::Probe(_)), "掃第二個候選期間，已命中者保持 gate");
    assert!(matches!(rig.flash_mode_of("COM1"), FlashMode::Probe(_)));
    assert!(rig.events_of("COM0", "FlashProbeReleased").is_empty());
    rig.run_until_flashing("COM0");
    assert_eq!(rig.flash_mode_of("COM1"), FlashMode::Off);
    assert_eq!(rig.events_of("COM1", "FlashProbeReleased").len(), 1);
}

#[test]
fn matched_releases_other_held() {
    let rig = FlashRig::posix_fake();
    rig.add_passthrough("COM0", "by-id/mcu-a", Mcu::silent());
    rig.add_passthrough("COM1", "by-id/mcu-b", Mcu::ti_sbl());
    rig.add_passthrough("COM2", "by-id/mcu-c", Mcu::silent());
    rig.flasher_write(SYNC);
    rig.run_until_flashing("COM1");
    assert_eq!(rig.flash_mode_of("COM0"), FlashMode::Off);
    assert_eq!(rig.flash_mode_of("COM2"), FlashMode::Off);
    assert_eq!(rig.flash_mode_of("COM1"), FlashMode::Flashing);
    assert_eq!(rig.state("COM0"), "ATTACHED");
    assert_eq!(rig.state("COM2"), "ATTACHED");
    assert_eq!(rig.serial_tx("COM0"), vec![SYNC.to_vec()], "每個候選各收一次 flasher 的 sync");
    assert_eq!(rig.serial_tx("COM2"), vec![SYNC.to_vec()]);
    let d = rig.coord().last_detect().expect("last_detect");
    assert_eq!((d.status, d.com.as_deref(), d.family.as_deref()), (DetectStatus::Matched, Some("COM1"), Some("ti-cc26xx")));
}

#[test]
fn ambiguous_two_hits_stays_silent() {
    let rig = FlashRig::posix_fake();
    rig.add_passthrough("COM0", "by-id/mcu-a", Mcu::ti_sbl());
    rig.add_passthrough("COM1", "by-id/mcu-b", Mcu::ti_sbl());
    rig.flasher_write(SYNC);
    rig.run_detection();
    assert_eq!(
        rig.last_detect_json(),
        r#"{"status":"ambiguous","com":null,"family":null,"hits":[{"by_id":"by-id/mcu-a","com":"COM0"},{"by_id":"by-id/mcu-b","com":"COM1"}]}"#
    );
    assert!(rig.master_writes().is_empty(), "多義：不回放、不進 FLASHING");
    assert_eq!(rig.flash_mode_of("COM0"), FlashMode::Off);
    assert_eq!(rig.flash_mode_of("COM1"), FlashMode::Off);
    assert!(!rig.flash_critical("COM0") && !rig.flash_critical("COM1"));
    assert_eq!(rig.events_of("COM0", "FlashProbeReleased").len(), 1);
    assert_eq!(rig.events_of("COM1", "FlashProbeReleased").len(), 1);
}

#[test]
fn flash_critical_unmarked_after_matched() {
    let rig = FlashRig::posix_fake();
    rig.add_passthrough("COM0", "by-id/mcu-a", Mcu::ti_sbl());
    rig.add_passthrough("COM1", "by-id/mcu-b", Mcu::silent());
    rig.flasher_write(SYNC);
    rig.run_until_flashing("COM0");
    assert!(!rig.flash_critical("COM0"), "命中線改由 FLASHING 守護");
    assert!(!rig.flash_critical("COM1"));
    assert_eq!(rig.state("COM0"), "FLASHING");
}

// ---------------------------------------------------------------------------
// holder watch（行為 5）
// ---------------------------------------------------------------------------

#[test]
fn holder_watch_armed_then_gone() {
    let rig = one_line(Mcu::ti_sbl());
    rig.holder_sequence(&[vec![4242], vec![4242], vec![]]);
    rig.flasher_write(SYNC);
    rig.run_until_flashing("COM0");
    let run = rig.coord().current_run_for_test().expect("run");
    rig.advance_secs(1.0);
    assert!(run.is_cancelled(), "第 3 次 poll（armed 後歸零）取消本次 run");
    assert_eq!(rig.holder_calls().len(), 3);
    assert!(rig.holder_calls().iter().all(|p| *p == rig.endpoint_slave_path()), "只掃端點 slave");
    rig.run_until_state("COM0", "ATTACHED");
    assert!(!rig.endpoint_shutdown().is_set());
    assert!(rig.endpoint().thread_alive_for_test());
    wait_real("FlashHolderGone", || rig.events_of("COM0", "FlashHolderGone").len() == 1);
    let gone = rig.events_of("COM0", "FlashHolderGone");
    assert_eq!(gone[0].probe_id, rig.events_of("COM0", "FlashSyncMatched")[0].probe_id);
}

#[test]
fn holder_watch_never_armed_hits_max() {
    let rig = flashing(Mcu::ti_sbl()); // holder 恆空
    let run = rig.coord().current_run_for_test().expect("run");
    rig.advance_raw(Duration::from_millis(1_799_500));
    assert!(!run.is_cancelled(), "1799.5 s 時仍在等 flasher");
    assert_eq!(rig.state("COM0"), "FLASHING");
    rig.advance_raw(Duration::from_millis(500));
    wait_real("1800 s 上限取消本次 run", || run.is_cancelled());
    assert!(!rig.endpoint_shutdown().is_set());
    rig.run_until_state("COM0", "ATTACHED");
    assert!(rig.endpoint().thread_alive_for_test());
}

#[test]
fn holder_watch_permission_status_never_arms() {
    let rig = one_line(Mcu::ti_sbl());
    rig.holder_status(HolderScanStatus::Permission);
    rig.flasher_write(SYNC);
    rig.run_until_flashing("COM0");
    let run = rig.coord().current_run_for_test().expect("run");
    rig.advance_secs(3.0);
    assert!(rig.holder_calls().len() >= 6);
    assert!(!run.is_cancelled(), "/proc 不可讀與無 holder 不可區分：永不 armed");
    rig.advance_raw(Duration::from_secs(1800));
    wait_real("上限取消", || run.is_cancelled());
    assert!(!rig.endpoint_shutdown().is_set());
    assert!(rig.endpoint().thread_alive_for_test());
}

// ---------------------------------------------------------------------------
// pump（行為 6～7）
// ---------------------------------------------------------------------------

#[test]
fn pump_stops_on_not_submitted() {
    let rig = flashing(Mcu::ti_sbl());
    let run = rig.coord().current_run_for_test().expect("run");
    rig.fence("COM0").with(|f| f.frozen = true); // pump 中 fence freeze
    rig.flasher_write(b"\x03\x10\x20");
    wait_real("pump 結束、本次 run 取消", || run.is_cancelled());
    wait_real("FlashHolderGone", || rig.events_of("COM0", "FlashHolderGone").len() == 1);
    rig.fence("COM0").with(|f| f.frozen = false);
    assert!(!rig.endpoint_shutdown().is_set());
    assert!(rig.endpoint().thread_alive_for_test());
    rig.run_until_state("COM0", "ATTACHED");
    assert_eq!(rig.serial_tx("COM0"), vec![SYNC.to_vec()], "NotSubmitted：封包零 TX");
}

#[test]
fn pump_stops_on_master_eof() {
    let rig = flashing(Mcu::ti_sbl());
    let run = rig.coord().current_run_for_test().expect("run");
    rig.flasher_hangup_once();
    wait_real("pump 結束、本次 run 取消", || run.is_cancelled());
    wait_real("FlashHolderGone", || rig.events_of("COM0", "FlashHolderGone").len() == 1);
    assert!(!rig.endpoint_shutdown().is_set());
    assert!(rig.endpoint().thread_alive_for_test());
    rig.run_until_state("COM0", "ATTACHED");
    wait_real("current_run 清回 None", || rig.coord().current_run_for_test().is_none());
}

#[test]
fn baud_mirror_uses_slave_speed() {
    let rig = one_line(Mcu::ti_sbl());
    let lc = rig.line_control("COM0");
    lc.set_slave_speed(&rig.endpoint_slave_path(), speed(0o010001)); // B57600
    rig.flasher_write(SYNC);
    rig.run_until_flashing("COM0");
    assert_eq!(lc.speed_writes(), vec![(0o010001, 0o010001)], "slave 速率鏡射到實體 UART 一次");
    assert!(lc.baud_calls().is_empty(), "鏡射成功不走 fallback");
}

#[test]
fn baud_mirror_falls_back_to_pattern_baud() {
    let rig = one_line(Mcu::ti_sbl()); // slave 速率讀取失敗
    rig.flasher_write(SYNC);
    rig.run_until_flashing("COM0");
    let lc = rig.line_control("COM0");
    assert!(lc.speed_writes().is_empty());
    assert_eq!(lc.baud_calls(), vec![115_200], "fallback＝命中 pattern 的 registry baud");
}

#[test]
fn baud_mirror_never_writes_b0() {
    let rig = one_line(Mcu::ti_sbl());
    let lc = rig.line_control("COM0");
    lc.set_slave_speed(&rig.endpoint_slave_path(), speed(0)); // flasher 未設速率（B0）
    rig.flasher_write(SYNC);
    rig.run_until_flashing("COM0");
    assert!(lc.speed_writes().iter().all(|(i, o)| *i != 0 && *o != 0), "絕不把 B0 寫到實體 UART");
    assert!(lc.speed_writes().is_empty());
    assert_eq!(lc.baud_calls(), vec![115_200], "B0 視為未設 → fallback baud");
}

#[test]
fn baud_mirror_once_only() {
    let rig = one_line(Mcu::ti_sbl());
    let lc = rig.line_control("COM0");
    lc.set_slave_speed(&rig.endpoint_slave_path(), speed(0o010001));
    rig.flasher_write(SYNC);
    rig.run_until_flashing("COM0");
    lc.set_slave_speed(&rig.endpoint_slave_path(), speed(0o000015)); // flash 中途改 B9600
    rig.flasher_write(b"\x03\x10\x20");
    rig.pump_once();
    assert_eq!(lc.speed_writes(), vec![(0o010001, 0o010001)], "只在 flash 開始時鏡射一次（UIO-12.9-f）");
}

// ---------------------------------------------------------------------------
// RX observer（行為 8）
// ---------------------------------------------------------------------------

#[test]
fn rx_observer_forwards_device_rx_to_flasher_during_flash() {
    let rig = flashing(Mcu::ti_sbl());
    rig.flasher_write(b"\x03\x10\x20");
    rig.pump_once();
    wait_real("封包 ACK 轉回 flasher", || rig.master_writes().len() == 2);
    assert_eq!(rig.master_writes(), vec![ACK.to_vec(), ACK.to_vec()]);
}

#[test]
fn rx_observer_non_blocking() {
    let rig = flashing(Mcu::ti_sbl());
    rig.master_writer_zero(true);
    let a0 = rig.master_write_attempts();
    let w0 = rig.master_writes();
    rig.device("COM0").push_rx(b"\x01\x02");
    rig.settle();
    wait_real("observer 嘗試寫一次", || rig.master_write_attempts() == a0 + 1);
    rig.device("COM0").push_rx(b"\x03\x04");
    rig.settle();
    wait_real("reader 未被卡住：下一筆 RX 照常送達 observer", || rig.master_write_attempts() == a0 + 2);
    std::thread::sleep(Duration::from_millis(30));
    assert_eq!(rig.master_write_attempts(), a0 + 2, "Ok(0) 不重試");
    assert_eq!(rig.master_writes(), w0);
}

#[test]
fn rx_observer_only_active_com() {
    let rig = FlashRig::posix_fake();
    rig.add_passthrough("COM0", "by-id/mcu-a", Mcu::ti_sbl());
    rig.add_passthrough("COM1", "by-id/mcu-b", Mcu::silent());
    rig.flasher_write(SYNC);
    rig.run_until_flashing("COM0");
    let a0 = rig.master_write_attempts();
    rig.device("COM1").push_rx(b"noise-from-com1");
    rig.settle();
    std::thread::sleep(Duration::from_millis(30));
    assert_eq!(rig.master_write_attempts(), a0, "非 active COM 的 RX 不寫 master");
}

// ---------------------------------------------------------------------------
// 停止（FlashEndpoint::stop／Drop）
// ---------------------------------------------------------------------------

#[test]
fn stop_during_multi_candidate_probe() {
    let rig = FlashRig::posix_fake();
    rig.add_passthrough("COM0", "by-id/mcu-a", Mcu::ti_sbl());
    rig.add_passthrough("COM1", "by-id/mcu-b", Mcu::silent());
    rig.add_passthrough("COM2", "by-id/mcu-c", Mcu::ti_sbl());
    rig.flasher_write(SYNC);
    rig.drive_until("COM1 probe 等待中", |r| {
        !r.events_of("COM1", "FlashSyncProbeStart").is_empty() && r.clock.waiters() > 0
    });
    let t0 = Instant::now();
    let r = rig.stop_endpoint();
    assert!(t0.elapsed() < Duration::from_secs(2), "stop() 在 2.0 s 內返回");
    assert_eq!((r.joined, r.unlinked), (true, true));
    assert!(r.stalled_threads.is_empty());
    for com in ["COM0", "COM1", "COM2"] {
        assert_eq!(rig.flash_mode_of(com), FlashMode::Off, "{com}");
        assert!(!rig.flash_critical(com), "{com} 已 unmark");
    }
    assert_eq!(rig.events_of("COM0", "FlashProbeReleased").len(), 1, "已命中者於收尾 Released");
    assert_eq!(rig.events_of("COM1", "FlashProbeReleased").len(), 1);
    assert!(rig.events_of("COM2", "FlashSyncProbeStart").is_empty(), "取消後不再 probe 下一個候選");
    assert!(rig.serial_tx("COM2").is_empty());
    assert!(rig.master_writes().is_empty());
    assert!(!rig.ttymcu_link_exists());
}

#[test]
fn stop_while_holder_present() {
    let rig = one_line(Mcu::ti_sbl());
    rig.holder_default(vec![4242]); // holder 恆存在
    rig.flasher_write(SYNC);
    rig.run_until_flashing("COM0");
    let run = rig.coord().current_run_for_test().expect("run");
    rig.advance_secs(1.0);
    assert!(!run.is_cancelled());
    let r = rig.stop_endpoint();
    assert!(rig.endpoint_shutdown().is_set());
    assert!(run.is_cancelled(), "request_shutdown 取消本次 run");
    assert_eq!((r.joined, r.unlinked), (true, true));
    assert!(r.stalled_threads.is_empty());
    assert_eq!(rig.events_of("COM0", "FlashHolderGone").len(), 1);
    assert!(!rig.ttymcu_link_exists());
    assert!(rig.master_closed(), "join 成功後 master 已關閉");
    rig.run_until_state("COM0", "ATTACHED");
}

#[test]
fn stop_reports_stalled_thread() {
    let rig = one_line(Mcu::ti_sbl());
    rig.holder_default(vec![4242]);
    rig.flasher_write(SYNC);
    rig.run_until_flashing("COM0");
    let dev = rig.device("COM0");
    dev.script_write(vec![FakeWriteStep::BlockForever]); // 不回應 stop 的 pump sink
    rig.flasher_write(b"\x03\x10\x20");
    dev.wait_blocked();
    let r = rig.stop_endpoint();
    assert!(!r.joined);
    assert_eq!(r.stalled_threads, vec!["pump"]);
    assert!(r.unlinked);
    assert!(!rig.master_closed(), "不關閉仍被 stalled thread 使用的 master");
    assert!(!rig.ttymcu_link_exists());
    dev.release_block();
    wait_real("stalled thread 收尾後關閉 master", || rig.master_closed());
    wait_real("FlashHolderGone", || rig.events_of("COM0", "FlashHolderGone").len() == 1);
}

#[test]
fn drop_without_stop_unlinks() {
    let rig = one_line(Mcu::ti_sbl());
    assert!(rig.ttymcu_link_exists());
    rig.drop_endpoint();
    assert!(!rig.ttymcu_link_exists(), "未呼叫 stop 的 drop 仍 unlink ttyMCU");
    assert!(rig.endpoint_shutdown().is_set());
    wait_real("端點 thread 收尾", || rig.master_closed());
}

// ---------------------------------------------------------------------------
// 真 PTY（cap:posix_pty；編排者 T9 裁決由 sw-pal flash_pty.rs 移到此處——需要 sw-core 的 FlashEndpoint）
// ---------------------------------------------------------------------------

/// 計畫 Task 9「其餘族」表（R0-F3 延伸）：daemon 從未對 `/dev/ttyMCU` 端點設 `TIOCEXCL`。`FlashEndpoint::start` 後、
/// 完成一次 matched→pump→`FlashHolderGone` 循環後，各以一般 open 開 `ttyMCU` 並 `ioctl(TIOCGEXCL)` 皆讀回 0；第二次
/// flasher 一般 open 成功，送 `55 55` 仍得 `00 cc` 回放。flasher 每輪讀完 ACK 再關（MCU 坑：未讀的 ACK 會殘留在 tty
/// 輸入佇列）。端點 PTY／raw／symlink 為真（`NativeConsoleFactory`），MCU、holder 掃描與 Clock 為替身。
#[test]
#[ignore = "cap:posix_pty"]
#[allow(non_snake_case)]
fn cap_posix_pty__endpoint_slave_never_exclusive() {
    use sw_pal::testutil::{tty_exclusive, PlainTty};

    let dir = tempfile::tempdir().expect("tempdir");
    let link = dir.path().join("run/dev/ttyMCU");
    let rig = FlashRig::posix_real_pty(&link);
    rig.add_passthrough("COM0", "by-id/mcu", Mcu::ti_sbl());
    assert!(rig.endpoint_started(), "caps.flash_endpoint=true 必須啟動端點");
    assert!(std::fs::symlink_metadata(&link).is_ok(), "ttyMCU symlink 已建立");
    let excl = |when: &str| match tty_exclusive(&link) {
        Ok(v) => v,
        Err(e) => panic!("{when}：一般 open／TIOCGEXCL 失敗（端點被設為 exclusive 時一般 open 會 EBUSY）：{}", e.py_message),
    };
    assert!(!excl("start 後"), "start 後端點不得為 exclusive");

    // 第一次 flash：matched → pump → FlashHolderGone。
    rig.holder_sequence(&[vec![4242], vec![4242], vec![]]);
    let mut flasher = PlainTty::open(&link).expect("flasher 一般 open");
    flasher.write_all(SYNC).expect("送 sync");
    rig.drive_until("COM0 FLASHING", |r| r.state("COM0") == "FLASHING");
    assert_eq!(flasher.read_within(ACK.len(), Duration::from_secs(5)).expect("讀 ACK"), ACK, "MCU 的 ACK 回放給 flasher");
    wait_real("holder watch thread 登記第一次等待", || rig.clock.waiters() > 0);
    rig.settle();
    flasher.write_all(b"\x03\x10\x20").expect("送封包");
    wait_real("pump 送到 MCU 線", || rig.device("COM0").tx_since(0).ends_with(b"\x03\x10\x20"));
    assert_eq!(flasher.read_within(ACK.len(), Duration::from_secs(5)).expect("讀封包 ACK"), ACK, "封包 ACK 轉給 flasher");
    drop(flasher);
    rig.advance_secs(1.5);
    rig.run_until_state("COM0", "ATTACHED");
    wait_real("第一次 FlashHolderGone", || rig.events_of("COM0", "FlashHolderGone").len() == 1);
    assert!(!excl("FlashHolderGone 循環後"), "循環後端點仍不得為 exclusive");

    // 第二次 flasher：一般 open 成功，sync 仍得 ACK 回放。
    rig.holder_sequence(&[vec![4343], vec![4343], vec![]]);
    let mut second = PlainTty::open(&link).expect("第二次 flasher 一般 open 必須成功");
    second.write_all(SYNC).expect("第二次 sync");
    rig.drive_until("COM0 第二次 FLASHING", |r| r.state("COM0") == "FLASHING");
    assert_eq!(second.read_within(ACK.len(), Duration::from_secs(5)).expect("讀 ACK"), ACK, "第二次 sync 仍得 00 cc 回放");
    wait_real("第二次 holder watch 登記等待", || rig.clock.waiters() > 0);
    rig.settle();
    drop(second);
    rig.advance_secs(1.5);
    rig.run_until_state("COM0", "ATTACHED");
    wait_real("第二次 FlashHolderGone", || rig.events_of("COM0", "FlashHolderGone").len() == 2);
    assert_eq!(
        rig.serial_tx("COM0"),
        vec![SYNC.to_vec(), b"\x03\x10\x20".to_vec(), SYNC.to_vec()],
        "送到 MCU 的只有兩次 flasher 自己的 sync 與一個封包"
    );
    let r = rig.stop_endpoint();
    assert_eq!((r.joined, r.unlinked), (true, true));
    assert!(std::fs::symlink_metadata(&link).is_err(), "stop 後 ttyMCU symlink 已移除");
}

// ---------------------------------------------------------------------------
// S2-PAR-wp510r R6：G-6 flash 入口讀未確認集合（設計 v1 §1.8、v3 §1.9）
// ---------------------------------------------------------------------------

/// G-6 端到端（真 manager＋FlashCoordinator＋端點）：by_id 在未確認關閉集合內的線不列為 flash 候選、不標 flash-critical
/// （Python `collect_flash_candidates_and_mark` 略過 `_draining_by_ids`，`origin/main:sw_core/session_manager.py:3282-3288`）。
/// flasher 的 sync 不得送往 MCU、端點保持沉默、偵測結果 none、不進 FLASHING。對照組：同一條線未登記時照常認線。
#[test]
fn wp510r_r6_g6_unconfirmed_by_id_is_not_flash_candidate() {
    let rig = one_line(Mcu::ti_sbl());
    let mgr = rig.mgr();
    mgr.mark_unconfirmed_close_for_test("by-id/mcu");
    rig.flasher_write(SYNC);
    rig.run_detection();
    assert!(rig.serial_tx("COM0").is_empty(), "未確認關閉的線不得收到 probe");
    assert!(rig.master_writes().is_empty(), "未匹配：端點保持沉默");
    assert_eq!(rig.last_detect_json(), r#"{"status":"none","com":null,"family":null,"hits":[]}"#);
    assert_eq!(rig.state("COM0"), "ATTACHED");
    assert!(!mgr.flash_critical("COM0"), "略過的候選不得留下 flash-critical 標記");

    let control = flashing(Mcu::ti_sbl());
    assert_eq!(control.state("COM0"), "FLASHING", "對照組：未登記時照常認線");
}
