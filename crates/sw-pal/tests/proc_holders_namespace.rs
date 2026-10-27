//! holder 探測的 mount namespace 碰撞回歸（parity-amendments-2026-10-26 #64）：移植 Python
//! `tests/test_holder_probe_namespace.py::TestProbeHoldersNamespace` 與 `serial_port.probe_holders` 的錯誤分支。
//!
//! 以可注入的 `HolderFs` 佈置假 `/proc`：每條 fd 的 inode 身分可獨立指定，模擬「字串相同、inode 不同」的
//! 外部 namespace fd 與「同一 inode」的真實 holder。`TestUartBridgeHeldPathsNamespace`（console reaper）由
//! P6-T8 承接，`TestDetectMultiOpenNamespace` 由 `proc_scan`（multi_open）承接。
// sw-pal 是唯一允許平台 cfg 的 crate
#![cfg(unix)]

use std::collections::BTreeMap;
use std::io;
use std::path::{Path, PathBuf};

use sw_pal::proc_holders::posix::{HolderFs, TtyStat, probe_holders_with, same_tty};
use sw_pal::{HolderReport, HolderScanStatus};

const HOST_DEV: u64 = 60;
const CONTAINER_DEV_A: u64 = 140;
const CONTAINER_DEV_B: u64 = 148;
const PTS7_RDEV: u64 = (136 << 8) | 7;
const TTY: &str = "/dev/pts/7";
const PROC: &str = "/fakeproc";

#[derive(Clone)]
enum Node {
    Stat(TtyStat),
    Err(io::ErrorKind),
}

#[derive(Default)]
struct FakeProcNs {
    stats: BTreeMap<PathBuf, Node>,
    dirs: BTreeMap<PathBuf, Result<Vec<String>, io::ErrorKind>>,
}

fn chr(dev: u64, ino: u64, rdev: u64) -> Node {
    Node::Stat(TtyStat {
        dev,
        ino,
        is_char_device: true,
        rdev,
    })
}

impl FakeProcNs {
    fn new() -> FakeProcNs {
        let mut fake = FakeProcNs::default();
        fake.dirs.insert(PathBuf::from(PROC), Ok(Vec::new()));
        fake.set_path(TTY, chr(HOST_DEV, 10, PTS7_RDEV));
        fake
    }

    fn add_proc(&mut self, pid: u32) -> PathBuf {
        if let Some(Ok(names)) = self.dirs.get_mut(Path::new(PROC)) {
            if !names.contains(&pid.to_string()) {
                names.push(pid.to_string());
            }
        }
        let fd_dir = Path::new(PROC).join(pid.to_string()).join("fd");
        self.dirs
            .entry(fd_dir.clone())
            .or_insert_with(|| Ok(Vec::new()));
        fd_dir
    }

    fn add_fd(&mut self, pid: u32, fd: u32, node: Node) {
        let fd_dir = self.add_proc(pid);
        if let Some(Ok(names)) = self.dirs.get_mut(&fd_dir) {
            names.push(fd.to_string());
        }
        self.stats.insert(fd_dir.join(fd.to_string()), node);
    }

    fn set_path(&mut self, path: &str, node: Node) {
        self.stats.insert(PathBuf::from(path), node);
    }

    fn probe(&self, path: &str) -> HolderReport {
        probe_holders_with(self, Path::new(PROC), path, None)
    }
}

impl HolderFs for FakeProcNs {
    fn list_dir(&self, path: &Path) -> io::Result<Vec<String>> {
        match self.dirs.get(path) {
            Some(Ok(names)) => Ok(names.clone()),
            Some(Err(kind)) => Err(io::Error::from(*kind)),
            None => Err(io::Error::from(io::ErrorKind::NotFound)),
        }
    }

    fn stat(&self, path: &Path) -> io::Result<TtyStat> {
        match self.stats.get(path) {
            Some(Node::Stat(st)) => Ok(*st),
            Some(Node::Err(kind)) => Err(io::Error::from(*kind)),
            None => Err(io::Error::from(io::ErrorKind::NotFound)),
        }
    }
}

#[test]
fn foreign_namespace_same_string_not_holder() {
    let mut proc_ns = FakeProcNs::new();
    proc_ns.add_fd(4321, 3, chr(CONTAINER_DEV_B, 10, PTS7_RDEV));
    let rep = proc_ns.probe(TTY);
    assert!(rep.pids.is_empty());
    assert_eq!(rep.status, HolderScanStatus::Ok);
    assert!(!rep.holder_unknown);
}

#[test]
fn devpts_equal_rdev_different_inode_not_holder() {
    // Python `test_rdev_equal_alias_other_instance_not_holder`：rdev 相同、st_dev 不同 → 非 holder。
    let mut proc_ns = FakeProcNs::new();
    proc_ns.add_fd(4322, 3, chr(CONTAINER_DEV_A, 10, PTS7_RDEV));
    assert!(proc_ns.probe(TTY).pids.is_empty());
}

#[test]
fn same_inode_holder_still_counts() {
    let mut proc_ns = FakeProcNs::new();
    proc_ns.add_fd(4323, 3, chr(HOST_DEV, 10, PTS7_RDEV));
    let rep = proc_ns.probe(TTY);
    assert_eq!(rep.pids, vec![4323]);
    assert_eq!(rep.status, HolderScanStatus::Ok);
}

#[test]
fn alias_link_same_inode_counts() {
    // fd 經由別名路徑指向同一 inode：以 inode 判定（假 /proc 不提供 readlink，字串無從參與）。
    let mut proc_ns = FakeProcNs::new();
    proc_ns.add_fd(4324, 3, chr(HOST_DEV, 10, PTS7_RDEV));
    assert_eq!(proc_ns.probe(TTY).pids, vec![4324]);
}

#[test]
fn fd_stat_not_found_skipped() {
    // Python `test_string_only_match_without_identity_not_holder`：fd stat 失敗（消失）→ 不計入、status 不變。
    let mut proc_ns = FakeProcNs::new();
    proc_ns.add_fd(4325, 3, Node::Err(io::ErrorKind::NotFound));
    let rep = proc_ns.probe(TTY);
    assert!(rep.pids.is_empty());
    assert_eq!(rep.status, HolderScanStatus::Ok);
    assert!(!rep.holder_unknown);
}

#[test]
fn mixed_foreign_and_true_holder_reports_only_true() {
    let mut proc_ns = FakeProcNs::new();
    proc_ns.add_fd(5001, 3, chr(CONTAINER_DEV_A, 10, PTS7_RDEV));
    proc_ns.add_fd(5002, 3, chr(HOST_DEV, 10, PTS7_RDEV));
    assert_eq!(proc_ns.probe(TTY).pids, vec![5002]);
}

#[test]
fn fd_stat_permission_sets_permission_status() {
    // Python `test_permission_status_semantics_preserved`。
    let mut proc_ns = FakeProcNs::new();
    proc_ns.add_fd(6001, 3, Node::Err(io::ErrorKind::PermissionDenied));
    let rep = proc_ns.probe(TTY);
    assert!(rep.pids.is_empty());
    assert_eq!(rep.status, HolderScanStatus::Permission);
    assert!(rep.holder_unknown);
}

#[test]
fn target_stat_permission_error_is_holder_unknown_permission() {
    let mut proc_ns = FakeProcNs::new();
    proc_ns.set_path(TTY, Node::Err(io::ErrorKind::PermissionDenied));
    proc_ns.add_fd(6002, 3, chr(HOST_DEV, 10, PTS7_RDEV));
    let rep = proc_ns.probe(TTY);
    assert_eq!(
        rep,
        HolderReport {
            pids: vec![],
            holder_unknown: true,
            status: HolderScanStatus::Permission,
        },
        "目標 stat 失敗即停止掃描，不得回「無 holder、status=ok」"
    );
}

#[test]
fn target_stat_oserror_is_holder_unknown() {
    // Python `test_target_missing_is_unknown_not_safe`。
    let mut proc_ns = FakeProcNs::new();
    proc_ns.set_path(TTY, Node::Err(io::ErrorKind::NotFound));
    let rep = proc_ns.probe(TTY);
    assert_eq!(
        rep,
        HolderReport {
            pids: vec![],
            holder_unknown: true,
            status: HolderScanStatus::Unknown,
        }
    );
}

#[test]
fn non_devpts_char_device_equal_rdev_is_alias_holder() {
    // Python `test_mknod_alias_non_devpts_same_rdev_counts`：major=4 的 UART 以 mknod 別名（inode 不同、rdev 相同）。
    let uart = "/dev/ttyS0";
    let uart_rdev = (4 << 8) | 64;
    let mut proc_ns = FakeProcNs::new();
    proc_ns.set_path(uart, chr(HOST_DEV, 11, uart_rdev));
    proc_ns.add_fd(4340, 3, chr(CONTAINER_DEV_A, 55, uart_rdev));
    let rep = proc_ns.probe(uart);
    assert_eq!(rep.pids, vec![4340]);
    assert!(!rep.holder_unknown);
}

#[test]
fn proc_listdir_error_is_permission() {
    // Python：os.listdir(proc_root) 的 PermissionError 與其他 OSError 皆回 permission。
    for kind in [io::ErrorKind::PermissionDenied, io::ErrorKind::NotFound] {
        let mut proc_ns = FakeProcNs::new();
        proc_ns.dirs.insert(PathBuf::from(PROC), Err(kind));
        assert_eq!(
            proc_ns.probe(TTY),
            HolderReport {
                pids: vec![],
                holder_unknown: true,
                status: HolderScanStatus::Permission,
            },
            "{kind:?}"
        );
    }
}

#[test]
fn fd_dir_listdir_errors_permission_then_unknown() {
    // fd 目錄：PermissionError → permission（覆寫）；其他 OSError 只在 status 仍為 ok 時改 unknown。
    let mut proc_ns = FakeProcNs::new();
    let fd_dir = proc_ns.add_proc(7001);
    proc_ns.dirs.insert(fd_dir, Err(io::ErrorKind::Other));
    assert_eq!(proc_ns.probe(TTY).status, HolderScanStatus::Unknown);
    let fd_dir = proc_ns.add_proc(7002);
    proc_ns
        .dirs
        .insert(fd_dir, Err(io::ErrorKind::PermissionDenied));
    assert_eq!(proc_ns.probe(TTY).status, HolderScanStatus::Permission);
}

#[test]
fn holder_found_with_partial_permission_is_not_unknown() {
    let mut proc_ns = FakeProcNs::new();
    proc_ns.add_fd(8001, 3, Node::Err(io::ErrorKind::PermissionDenied));
    proc_ns.add_fd(8002, 3, chr(HOST_DEV, 10, PTS7_RDEV));
    let rep = proc_ns.probe(TTY);
    assert_eq!(rep.pids, vec![8002]);
    assert_eq!(rep.status, HolderScanStatus::Permission);
    assert!(
        !rep.holder_unknown,
        "holder_unknown = pids 空且 status 非 ok"
    );
}

#[test]
fn exclude_pid_and_non_numeric_entries_skipped() {
    let mut proc_ns = FakeProcNs::new();
    proc_ns.add_fd(9001, 3, chr(HOST_DEV, 10, PTS7_RDEV));
    if let Some(Ok(names)) = proc_ns.dirs.get_mut(Path::new(PROC)) {
        names.push("self".into());
    }
    let rep = probe_holders_with(&proc_ns, Path::new(PROC), TTY, Some(9001));
    assert!(rep.pids.is_empty());
    assert_eq!(rep.status, HolderScanStatus::Ok);
}

#[test]
fn same_tty_matrix() {
    let pts = |dev, ino| TtyStat {
        dev,
        ino,
        is_char_device: true,
        rdev: PTS7_RDEV,
    };
    assert!(same_tty(&pts(HOST_DEV, 10), &pts(HOST_DEV, 10)));
    assert!(
        !same_tty(&pts(CONTAINER_DEV_A, 10), &pts(HOST_DEV, 10)),
        "devpts 不以 rdev 判定"
    );
    let regular = TtyStat {
        dev: 1,
        ino: 2,
        is_char_device: false,
        rdev: (4 << 8) | 64,
    };
    let uart = TtyStat {
        dev: 3,
        ino: 4,
        is_char_device: true,
        rdev: (4 << 8) | 64,
    };
    assert!(!same_tty(&regular, &uart), "非字元裝置不走 rdev 別名");
    let zero = TtyStat { rdev: 0, ..uart };
    assert!(
        !same_tty(&TtyStat { dev: 9, ..zero }, &zero),
        "rdev 0 不視為別名"
    );
}
