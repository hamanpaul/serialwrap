//! WP5-9 ITEM 1：fd stat 權限錯誤依行程 owner UID 分類，並接到開埠判定。
#![cfg(all(unix, feature = "test-fakes"))]

use std::io;
use std::path::{Path, PathBuf};

use sw_pal::fakes::FakeHolderFs;
use sw_pal::proc_holders::posix::{probe_holders_with, TtyStat};
use sw_pal::serial::posix::{open_sequence, TtyOpenOps};
use sw_pal::{HolderReport, HolderScanStatus};

const PROC: &str = "/fakeproc";
const TTY: &str = "/dev/ttyS0";
const SELF_UID: u32 = 1000;
const OTHER_UID: u32 = 2000;

fn tty_stat() -> TtyStat {
    TtyStat {
        dev: 60,
        ino: 1,
        is_char_device: true,
        rdev: (4 << 8) | 64,
    }
}

fn non_tty_stat() -> TtyStat {
    TtyStat {
        dev: 60,
        ino: 2,
        is_char_device: false,
        rdev: 0,
    }
}

fn fd_path(pid: u32) -> PathBuf {
    Path::new(PROC).join(pid.to_string()).join("fd/3")
}

fn proc_with_pids(pids: &[u32]) -> FakeHolderFs {
    let mut fs = FakeHolderFs::default();
    fs.set_stat(TTY, tty_stat());
    fs.set_current_uid(Some(SELF_UID));
    fs.set_dir(PROC, pids.iter().map(u32::to_string).collect());
    for pid in pids {
        fs.set_dir(
            Path::new(PROC).join(pid.to_string()).join("fd"),
            vec!["3".to_string()],
        );
    }
    fs
}

fn other_uid_probe() -> (FakeHolderFs, HolderReport) {
    let mut fs = proc_with_pids(&[7001, 7002]);
    fs.set_process_uid(7001, Some(OTHER_UID));
    fs.fail_stat(fd_path(7001), io::ErrorKind::PermissionDenied);
    fs.set_stat(fd_path(7002), non_tty_stat());
    let report = probe_holders_with(&fs, Path::new(PROC), TTY, None);
    (fs, report)
}

#[test]
fn wp59_other_uid_unreadable_fd_is_partial_not_unknown() {
    let (_, report) = other_uid_probe();

    assert!(report.pids.is_empty());
    assert_eq!(
        report.status.py_name(),
        "partial_other_uid",
        "預期 partial_other_uid，實際 {}",
        report.status.py_name()
    );
    assert!(!report.holder_unknown);
}

#[test]
fn wp59_same_uid_unreadable_fd_stays_permission() {
    let mut fs = proc_with_pids(&[7101]);
    fs.set_process_uid(7101, Some(SELF_UID));
    fs.fail_stat(fd_path(7101), io::ErrorKind::PermissionDenied);

    let report = probe_holders_with(&fs, Path::new(PROC), TTY, None);

    assert!(report.pids.is_empty());
    assert_eq!(report.status, HolderScanStatus::Permission);
    assert!(report.holder_unknown);
}

#[test]
fn wp59_unknown_owner_unreadable_fd_stays_permission() {
    let mut fs = proc_with_pids(&[7201]);
    fs.fail_stat(fd_path(7201), io::ErrorKind::PermissionDenied);

    let report = probe_holders_with(&fs, Path::new(PROC), TTY, None);

    assert!(report.pids.is_empty());
    assert_eq!(report.status, HolderScanStatus::Permission);
    assert!(report.holder_unknown);
}

#[derive(Default)]
struct RecordingOpenOps {
    report: Option<HolderReport>,
    warnings: Vec<String>,
}

impl TtyOpenOps for RecordingOpenOps {
    fn open(&mut self) -> Result<(), i32> {
        Ok(())
    }

    fn flock(&mut self) -> Result<(), i32> {
        Ok(())
    }

    fn excl(&mut self) -> Result<(), i32> {
        Ok(())
    }

    fn probe_holders(&mut self) -> HolderReport {
        self.report.clone().expect("測試已設定 holder 掃描結果")
    }

    fn termios(&mut self) -> Result<(), i32> {
        Ok(())
    }

    fn nxcl(&mut self) -> Result<(), i32> {
        Ok(())
    }

    fn close_fd(&mut self) -> Result<(), i32> {
        Ok(())
    }

    fn warn(&mut self, line: &str) {
        self.warnings.push(line.to_string());
    }
}

#[test]
fn wp59_open_accepts_partial_other_uid_without_warning() {
    let (_, report) = other_uid_probe();
    let mut ops = RecordingOpenOps {
        report: Some(report),
        ..RecordingOpenOps::default()
    };

    assert!(open_sequence(&mut ops, TTY).is_ok());
    assert!(ops.warnings.is_empty());
}
