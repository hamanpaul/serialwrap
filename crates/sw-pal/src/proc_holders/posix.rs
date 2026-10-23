//! POSIX `/proc` 序列埠 holder 診斷；此快照不取代 `flock`／`TIOCEXCL`。

use std::collections::BTreeSet;
use std::fs;
use std::os::unix::fs::{FileTypeExt, MetadataExt};
use std::path::{Path, PathBuf};

use crate::traits::{HolderReport, HolderScanStatus, UartExclusive};

/// POSIX `/proc` holder 掃描器。
pub struct PosixUartExclusive;

impl UartExclusive for PosixUartExclusive {
    fn probe_holders(&self, real_path: &str) -> HolderReport {
        probe_holders_in(Path::new("/proc"), real_path)
    }
}

/// 掃描 `proc_root/<pid>/fd/*`，排除自身 PID，並以 realpath 或相同的裝置 rdev 判斷 holder。
pub fn probe_holders_in(proc_root: &Path, path: &str) -> HolderReport {
    let target_path = fs::canonicalize(path).unwrap_or_else(|_| PathBuf::from(path));
    let target = target_path.to_string_lossy();
    let target_rdev = fs::metadata(path)
        .ok()
        .map(|metadata| metadata.rdev())
        .unwrap_or(0);
    let mut pids = BTreeSet::new();
    let mut status = HolderScanStatus::Ok;

    let entries = match fs::read_dir(proc_root) {
        Ok(entries) => entries,
        Err(error) => {
            status = scan_status(&error);
            return report(pids, status);
        }
    };

    for entry in entries {
        let entry = match entry {
            Ok(entry) => entry,
            Err(error) => {
                status = merge_status(status, scan_status(&error));
                continue;
            }
        };
        let name = entry.file_name();
        let name = name.to_string_lossy();
        if name.is_empty() || !name.bytes().all(|byte| byte.is_ascii_digit()) {
            continue;
        }
        let Ok(pid) = name.parse::<u32>() else {
            continue;
        };
        if pid == std::process::id() {
            continue;
        }

        let fd_dir = entry.path().join("fd");
        let fds = match fs::read_dir(fd_dir) {
            Ok(fds) => fds,
            Err(error) => {
                status = merge_status(status, scan_status(&error));
                continue;
            }
        };
        for fd in fds {
            let fd = match fd {
                Ok(fd) => fd,
                Err(error) => {
                    status = merge_status(status, scan_status(&error));
                    continue;
                }
            };
            let fd_path = fd.path();
            let link = match fs::read_link(&fd_path) {
                Ok(link) => link,
                Err(error) if error.kind() == std::io::ErrorKind::NotFound => continue,
                Err(error) => {
                    status = merge_status(status, scan_status(&error));
                    continue;
                }
            };
            let link_text = link.to_string_lossy();
            let mut matched = link_text == target || link_text == path;
            if !matched && target_rdev != 0 {
                match fs::metadata(&fd_path) {
                    Ok(metadata) => {
                        matched =
                            metadata.file_type().is_char_device() && metadata.rdev() == target_rdev;
                    }
                    Err(error) if error.kind() == std::io::ErrorKind::NotFound => {}
                    Err(error) => status = merge_status(status, scan_status(&error)),
                }
            }
            if matched {
                pids.insert(pid);
                break;
            }
        }
    }

    report(pids, status)
}

fn scan_status(error: &std::io::Error) -> HolderScanStatus {
    if error.kind() == std::io::ErrorKind::PermissionDenied {
        HolderScanStatus::Permission
    } else {
        HolderScanStatus::Unknown
    }
}

fn merge_status(current: HolderScanStatus, incoming: HolderScanStatus) -> HolderScanStatus {
    match (current, incoming) {
        (HolderScanStatus::Permission, _) | (_, HolderScanStatus::Permission) => {
            HolderScanStatus::Permission
        }
        (HolderScanStatus::Unknown, _) | (_, HolderScanStatus::Unknown) => {
            HolderScanStatus::Unknown
        }
        (status, _) => status,
    }
}

fn report(pids: BTreeSet<u32>, status: HolderScanStatus) -> HolderReport {
    let pids = pids.into_iter().collect::<Vec<_>>();
    HolderReport {
        holder_unknown: pids.is_empty(),
        pids,
        status,
    }
}
