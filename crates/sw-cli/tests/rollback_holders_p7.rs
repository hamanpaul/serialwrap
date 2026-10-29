//! rulings/p7.md P7-71fe017-4：更新／回滾的「確認釋放」（`rollback::confirm_released`）必須維持 fail-closed
//! 掃描（`ProcScan::serialwrapd_tty_holders`）；即使 Python 相容的多開掃描（`multi_open_tty_holders`，
//! health 專用）回「無 holder、ok」，目標 tty 身分不明仍須以 `DEVICE_STILL_HELD`＋`holder_unknown:true`
//! 中止，且不得 spawn 新版。若 rollback 改呼叫 `multi_open_tty_holders`，本測試即紅。
mod support;

use support::*;
use sw_pal::traits::HolderScanStatus;

#[test]
fn update_confirm_released_stays_fail_closed_when_multi_open_scan_says_ok() {
    let base = TestEnv::posix();
    let endpoint = base.default_socket();
    let list = "{\"ok\":true,\"sessions\":[{\"com\":\"COM0\",\"state\":\"READY\",\"attached_real_path\":\"/isolated/vanished-tty\",\"foreground_busy\":false,\"outstanding_commands\":0,\"interactive_session_id\":null,\"recovering\":false}]}";
    let t = base
        .with_install_root()
        .with_installed("0.4.0")
        .with_current("0.4.0", None)
        .with_bundle("0.4.1")
        .with_systemd(true)
        .with_var("USER", "alice")
        .with_supervisor_state("systemd-user", true, true)
        .with_idle_daemon_default(4242)
        .with_new_daemon_reporting_version("0.4.1")
        .with_rpc_ok(&endpoint, "session.list", list)
        .with_rpc_after_relaunch("session.list", list);
    // fail-closed 掃描：目標 tty 取不到身分 → Unknown；Python 相容掃描：略過該 tty → ok。
    t.procs().holders(Vec::new(), HolderScanStatus::Unknown);
    t.procs().set_multi_open_holders(Vec::new(), HolderScanStatus::Ok);
    let (rc, out, _) = t.run_from_bundle(&["setup"]);
    assert_eq!(rc, 2, "{out}");
    let j = json(&out);
    assert_eq!(
        (s(&j, "error_code"), s(&j, "phase")),
        ("DEVICE_STILL_HELD", "update_confirm_released")
    );
    assert_eq!(j["holder_unknown"], true);
    assert!(t.process().spawns().is_empty());
}
