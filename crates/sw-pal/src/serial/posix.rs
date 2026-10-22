//! POSIX 序列埠後端（P5 Task 2，arch §4.2）。
//!
//! T2 測試作者交付：介面骨架（函式本體為 `unimplemented!`）與 `#[cfg(test)]` 單元測試。
//! 實作者必須沿用這裡的名稱與簽章；單元測試的斷言不得改寫。
#![allow(dead_code, unused_variables)]

use std::path::Path;
use std::sync::Arc;

use crate::traits::{
    CloseReceipt, DevicePath, HolderReport, OpenError, PalError, PalResult, SerialBackend,
    SerialConfig, SerialHandle, UartExclusive,
};

/// POSIX 序列埠後端。`post_excl_fault` 只供 test-fakes 注入「TIOCEXCL 成功後」的失敗。
pub struct PosixSerialBackend {
    excl: Arc<dyn UartExclusive>,
    post_excl_fault: Option<PalError>,
}

impl PosixSerialBackend {
    /// 正式組裝（`native()`）使用的後端；建構本身不得失敗。
    pub fn new() -> Self {
        Self {
            excl: Arc::new(PosixUartExclusive),
            post_excl_fault: None,
        }
    }

    /// test-fakes only：TIOCEXCL 成功後立即回傳注入的錯誤（模擬 termios／eventfd 失敗）。
    #[cfg(feature = "test-fakes")]
    pub fn with_post_excl_fault(err: PalError) -> Self {
        s21_todo!("T2", "PosixSerialBackend::with_post_excl_fault")
    }

    /// test-fakes only：替換鎖後重掃所用的 holder 掃描（僅供需預持 fd 的測試情境，正式路徑不得使用）。
    #[cfg(feature = "test-fakes")]
    pub fn with_uart_exclusive(excl: Arc<dyn UartExclusive>) -> Self {
        s21_todo!("T2", "PosixSerialBackend::with_uart_exclusive")
    }
}

impl SerialBackend for PosixSerialBackend {
    fn open_exclusive(
        &self,
        dev: &DevicePath,
        cfg: &SerialConfig,
    ) -> Result<Box<dyn SerialHandle>, OpenError> {
        s21_todo!("T2", "PosixSerialBackend::open_exclusive")
    }
}

/// `/proc` 掃描為基礎的 holder 診斷（只供診斷，不是鎖；BC-04 的互斥來自 flock＋TIOCEXCL）。
pub struct PosixUartExclusive;

impl UartExclusive for PosixUartExclusive {
    fn probe_holders(&self, real_path: &str) -> HolderReport {
        probe_holders_in(Path::new("/proc"), real_path)
    }
}

/// 掃描 `proc_root/<pid>/fd/*`，排除自身 pid；readlink 等於 `path` 的 fd 其 pid 收入 `pids`（升冪）。
/// `proc_root` 不可列 → `{pids:[], holder_unknown:true, status:Unknown}`；任一 fd 目錄 EACCES → `status:Permission`（保留已收集的 pids）。
pub fn probe_holders_in(proc_root: &Path, path: &str) -> HolderReport {
    s21_todo!("T2", "probe_holders_in")
}

/// 鎖後重掃的判定（規格 §6.2；P1 T10 凍結規則）。
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum PostLockVerdict {
    Reject,
    Accept,
    AcceptUnverified,
}

/// 依 `pids` 與 `status` 判定鎖後重掃結果：`pids` 非空即 Reject（不論 status）。
pub fn post_lock_holder_verdict(rep: &HolderReport) -> PostLockVerdict {
    s21_todo!("T2", "post_lock_holder_verdict")
}

/// P1 T10 凍結規則：`holder_unknown` 只由 `pids` 是否為空決定，`status` 只作診斷。
pub fn finalize_holder_report(rep: HolderReport) -> HolderReport {
    s21_todo!("T2", "finalize_holder_report")
}

/// 由 `SerialConfig` 建出 termios：`c_iflag=c_oflag=c_lflag=0`、`c_cflag` 覆寫為
/// `CREAD|CLOCAL|CSx[|PARENB[|PARODD]][|CSTOPB][|CRTSCTS]`，`c_cc` 不動（arch §4.2）。
pub fn build_termios(input: libc::termios, cfg: &SerialConfig) -> libc::termios {
    s21_todo!("T2", "build_termios")
}

/// 未知 baud 回退 `B115200`（UIO-12.1 KEEP）。
pub fn speed_for_baud(baud: u32) -> libc::speed_t {
    s21_todo!("T2", "speed_for_baud")
}

/// `set_baud` 的 termios 存取抽象（測試以錄製替身取代 libc）。
pub trait TtyTermiosOps {
    fn tcgetattr(&mut self) -> Result<libc::termios, i32>;
    fn tcsetattr(&mut self, t: &libc::termios) -> Result<(), i32>;
}

/// 依 baud 設定速率；未知 baud 規則見 `unknown_baud_falls_back_115200` 單元測試。
pub fn set_baud_with(ops: &mut dyn TtyTermiosOps, baud: u32) -> PalResult<()> {
    s21_todo!("T2", "set_baud_with")
}

/// `CloseToken::close` 的系統呼叫序列（記錄順序即為 `CloseOp` 順序）。
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum CloseOp {
    Nxcl,
    FlockUnlock,
    CloseFd,
    CloseWakeR,
    CloseWakeW,
}

/// 關閉序的 OS 操作抽象（arch §4.2，R0-F3）。
pub trait TtyCloseOps {
    /// `ioctl(fd, TIOCNXCL)`；失敗回 errno。
    fn nxcl(&mut self) -> Result<(), i32>;
    fn flock_unlock(&mut self);
    /// `close(fd)`；失敗回 errno。
    fn close_fd(&mut self) -> Result<(), i32>;
    fn close_wake_r(&mut self);
    fn close_wake_w(&mut self);
    /// daemon stderr WARNING（整行，不含換行）。
    fn warn(&mut self, line: &str);
}

/// 關閉序純邏輯核心：`Nxcl` 回 `EIO`／`ENXIO`／`ENODEV` 視為已解除（`excl_cleared=true`、不警告）；
/// 其他 errno 照序繼續關閉、`excl_cleared=false`、`error` 為該 errno、警告一筆；`has_wake=false` 時略過 wake fd 兩步。
pub fn close_sequence(ops: &mut dyn TtyCloseOps, dev: &str, has_wake: bool) -> CloseReceipt {
    s21_todo!("T2", "close_sequence")
}

/// 開啟序的 OS 操作抽象（arch §4.2；`open_sequence` 的純邏輯核心）。
#[derive(Debug, Clone, PartialEq, Eq)]
pub enum OpenOp {
    Open,
    Flock,
    Excl,
    ProbeHolders,
    Termios(Result<(), i32>),
    Nxcl,
    CloseFd,
}

pub trait TtyOpenOps {
    /// `open(O_RDWR|O_NOCTTY|O_NONBLOCK|O_CLOEXEC)`；失敗回 errno。
    fn open(&mut self) -> Result<(), i32>;
    /// `flock(LOCK_EX|LOCK_NB)`；`EWOULDBLOCK` → `Held`。
    fn flock(&mut self) -> Result<(), i32>;
    /// `ioctl(fd, TIOCEXCL)`。
    fn excl(&mut self) -> Result<(), i32>;
    /// 鎖後重掃（排除自身 pid）。
    fn probe_holders(&mut self) -> HolderReport;
    /// `tcgetattr` → `build_termios` → `tcsetattr(TCSANOW)`。
    fn termios(&mut self) -> Result<(), i32>;
    fn nxcl(&mut self) -> Result<(), i32>;
    fn close_fd(&mut self) -> Result<(), i32>;
    fn warn(&mut self, line: &str);
}

/// 開啟序純邏輯核心：步驟順序為 open → flock → TIOCEXCL → 鎖後重掃 → termios；
/// TIOCEXCL 之後的任何失敗必先 TIOCNXCL 再 close(fd)，TIOCEXCL 之前的失敗不得出現 TIOCNXCL。
pub fn open_sequence(ops: &mut dyn TtyOpenOps, dev: &str) -> Result<(), OpenError> {
    s21_todo!("T2", "open_sequence")
}

#[cfg(test)]
mod tests {
    use super::*;
    use crate::traits::{FlowControl, HolderScanStatus, Parity, PalErrorKind};
    use std::os::unix::fs::PermissionsExt;

    fn raw_cfg(
        data_bits: u8,
        parity: Parity,
        stop_bits: u8,
        flow: FlowControl,
        xonxoff: bool,
    ) -> SerialConfig {
        SerialConfig {
            baud: 115200,
            data_bits,
            parity,
            stop_bits,
            flow,
            xonxoff,
        }
    }

    /// 輸入 termios：預置 `HUPCL|CS7|PARENB` 與 `c_iflag=ICRNL`，供驗證 build_termios 會完全覆寫 c_cflag。
    fn input_termios() -> libc::termios {
        let mut t: libc::termios = unsafe { std::mem::zeroed() };
        t.c_cflag = libc::HUPCL | libc::CS7 | libc::PARENB;
        t.c_iflag = libc::ICRNL;
        t.c_cc[libc::VMIN] = 1;
        t.c_cc[libc::VTIME] = 0;
        t.c_cc[libc::VINTR] = 3;
        t
    }

    #[test]
    fn termios_bits_8n1_rtscts() {
        let cfg = raw_cfg(8, Parity::None, 1, FlowControl::RtsCts, false);
        let out = build_termios(input_termios(), &cfg);
        assert_eq!(out.c_iflag, 0);
        assert_eq!(out.c_oflag, 0);
        assert_eq!(out.c_lflag, 0);
        // 完全相等：HUPCL 必須被清除（避免每次 close 掉 DTR 重置板子）
        assert_eq!(
            out.c_cflag & !libc::CBAUD,
            libc::CREAD | libc::CLOCAL | libc::CS8 | libc::CRTSCTS
        );
        assert_eq!(out.c_cc, input_termios().c_cc);
    }

    #[test]
    fn termios_bits_7e2() {
        let cfg = raw_cfg(7, Parity::Even, 2, FlowControl::None, false);
        let out = build_termios(input_termios(), &cfg);
        assert_eq!(
            out.c_cflag & !libc::CBAUD,
            libc::CREAD | libc::CLOCAL | libc::CS7 | libc::PARENB | libc::CSTOPB
        );
    }

    #[test]
    fn termios_odd() {
        let cfg = raw_cfg(8, Parity::Odd, 1, FlowControl::None, false);
        let out = build_termios(input_termios(), &cfg);
        assert_eq!(
            out.c_cflag & !libc::CBAUD,
            libc::CREAD | libc::CLOCAL | libc::CS8 | libc::PARENB | libc::PARODD
        );
    }

    #[test]
    fn xonxoff_ignored_on_posix() {
        let cfg = raw_cfg(8, Parity::None, 1, FlowControl::None, true);
        let out = build_termios(input_termios(), &cfg);
        assert_eq!(out.c_iflag, 0);
    }

    #[derive(Default)]
    struct RecTermios {
        tcsetattr_calls: usize,
    }

    impl TtyTermiosOps for RecTermios {
        fn tcgetattr(&mut self) -> Result<libc::termios, i32> {
            Ok(input_termios())
        }
        fn tcsetattr(&mut self, _t: &libc::termios) -> Result<(), i32> {
            self.tcsetattr_calls += 1;
            Ok(())
        }
    }

    #[test]
    fn unknown_baud_falls_back_115200() {
        assert_eq!(speed_for_baud(1234), libc::B115200);
        let mut ops = RecTermios::default();
        assert!(set_baud_with(&mut ops, 1234).is_ok());
        assert_eq!(ops.tcsetattr_calls, 0, "未知 baud 不得呼叫 tcsetattr");
    }

    #[test]
    fn vmin_vtime_untouched() {
        let mut input = input_termios();
        input.c_cc[libc::VMIN] = 7;
        input.c_cc[libc::VTIME] = 9;
        let out = build_termios(input, &raw_cfg(8, Parity::None, 1, FlowControl::None, false));
        assert_eq!(out.c_cc[libc::VMIN], 7);
        assert_eq!(out.c_cc[libc::VTIME], 9);
    }

    fn make_fd_link(root: &Path, pid: u32, target: &str) {
        let fd_dir = root.join(pid.to_string()).join("fd");
        std::fs::create_dir_all(&fd_dir).expect("mkdir fd");
        std::os::unix::fs::symlink(target, fd_dir.join("3")).expect("symlink fd");
    }

    #[test]
    fn holder_scan_excludes_self_and_matches_rdev() {
        let tmp = tempfile::tempdir().expect("tempdir");
        let root = tmp.path();
        make_fd_link(root, 123, "/dev/pts/9");
        make_fd_link(root, std::process::id(), "/dev/pts/9"); // 自身 pid 必須被排除
        let rep = probe_holders_in(root, "/dev/pts/9");
        assert_eq!(rep.pids, vec![123]);
        assert_eq!(rep.status, HolderScanStatus::Ok);
    }

    #[test]
    fn holder_scan_unreadable_proc_is_unknown() {
        let rep = probe_holders_in(Path::new("/nonexistent/serialwrap-s21-proc"), "/dev/pts/9");
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
    fn holder_scan_permission() {
        if unsafe { libc::geteuid() } == 0 {
            eprintln!("EVIDENCE_MISSING: root 下 chmod 000 無效，本案例證據不足");
            return;
        }
        let tmp = tempfile::tempdir().expect("tempdir");
        let root = tmp.path();
        make_fd_link(root, 123, "/dev/pts/9");
        let fd_dir = root.join("123").join("fd");
        std::fs::set_permissions(&fd_dir, std::fs::Permissions::from_mode(0o000))
            .expect("chmod 000");
        let rep = probe_holders_in(root, "/dev/pts/9");
        // 還原權限，讓 tempdir 可被清除
        let _ = std::fs::set_permissions(&fd_dir, std::fs::Permissions::from_mode(0o755));
        assert_eq!(rep.status, HolderScanStatus::Permission);
        assert!(rep.holder_unknown);
    }

    #[test]
    fn holder_unknown_only_when_no_pids() {
        // probe 回 pids:[123]、status:Permission（部分目錄不可讀）→ holder_unknown 必為 false
        let rep = finalize_holder_report(HolderReport {
            pids: vec![123],
            holder_unknown: true,
            status: HolderScanStatus::Permission,
        });
        assert!(!rep.holder_unknown);
        // pids 為空 → holder_unknown 必為 true（與 status 無關）
        let rep = finalize_holder_report(HolderReport {
            pids: vec![],
            holder_unknown: false,
            status: HolderScanStatus::Ok,
        });
        assert!(rep.holder_unknown);
    }

    #[derive(Default)]
    struct RecClose {
        ops: Vec<CloseOp>,
        warns: Vec<String>,
        nxcl_err: Option<i32>,
        close_fd_err: Option<i32>,
    }

    impl TtyCloseOps for RecClose {
        fn nxcl(&mut self) -> Result<(), i32> {
            self.ops.push(CloseOp::Nxcl);
            match self.nxcl_err {
                Some(e) => Err(e),
                None => Ok(()),
            }
        }
        fn flock_unlock(&mut self) {
            self.ops.push(CloseOp::FlockUnlock);
        }
        fn close_fd(&mut self) -> Result<(), i32> {
            self.ops.push(CloseOp::CloseFd);
            match self.close_fd_err {
                Some(e) => Err(e),
                None => Ok(()),
            }
        }
        fn close_wake_r(&mut self) {
            self.ops.push(CloseOp::CloseWakeR);
        }
        fn close_wake_w(&mut self) {
            self.ops.push(CloseOp::CloseWakeW);
        }
        fn warn(&mut self, line: &str) {
            self.warns.push(line.to_string());
        }
    }

    const FULL_CLOSE: [CloseOp; 5] = [
        CloseOp::Nxcl,
        CloseOp::FlockUnlock,
        CloseOp::CloseFd,
        CloseOp::CloseWakeR,
        CloseOp::CloseWakeW,
    ];

    #[test]
    fn close_sequence_order_nxcl_before_close() {
        let mut ops = RecClose::default();
        let rc = close_sequence(&mut ops, "/dev/pts/9", true);
        assert_eq!(ops.ops, FULL_CLOSE.to_vec());
        assert!(rc.closed && rc.excl_cleared && rc.error.is_none(), "{rc:?}");
    }

    #[test]
    fn close_sequence_nxcl_hangup_errno_is_cleared() {
        for code in [libc::EIO, libc::ENXIO, libc::ENODEV] {
            let mut ops = RecClose {
                nxcl_err: Some(code),
                ..Default::default()
            };
            let rc = close_sequence(&mut ops, "/dev/pts/9", true);
            assert_eq!(ops.ops, FULL_CLOSE.to_vec(), "errno {code}");
            assert!(rc.closed, "errno {code}: {rc:?}");
            assert!(rc.excl_cleared, "errno {code}: {rc:?}");
            assert!(rc.error.is_none(), "errno {code}: {rc:?}");
            assert!(ops.warns.is_empty(), "errno {code}: 不得有 WARNING");
        }
    }

    #[test]
    fn close_sequence_nxcl_other_errno_still_closes() {
        let mut ops = RecClose {
            nxcl_err: Some(libc::ENOTTY),
            ..Default::default()
        };
        let rc = close_sequence(&mut ops, "/dev/pts/9", true);
        assert_eq!(
            ops.ops,
            vec![
                CloseOp::Nxcl,
                CloseOp::FlockUnlock,
                CloseOp::CloseFd,
                CloseOp::CloseWakeR,
                CloseOp::CloseWakeW
            ]
        );
        assert!(rc.closed);
        assert!(!rc.excl_cleared);
        assert_eq!(
            rc.error.as_ref().map(|e| e.py_message.as_str()),
            Some("[Errno 25] Inappropriate ioctl for device")
        );
        assert_eq!(ops.warns.len(), 1, "WARNING 恰一筆：{:?}", ops.warns);
        assert!(
            ops.warns[0].starts_with("serialwrapd: TIOCNXCL failed on "),
            "{:?}",
            ops.warns
        );
    }

    #[test]
    fn close_sequence_close_fd_failure() {
        let mut ops = RecClose {
            close_fd_err: Some(libc::EIO),
            ..Default::default()
        };
        let rc = close_sequence(&mut ops, "/dev/pts/9", true);
        assert!(!rc.closed, "{rc:?}");
        assert!(rc.excl_cleared, "TIOCNXCL 已成功：{rc:?}");
        assert_eq!(rc.error.as_ref().map(|e| e.kind), Some(PalErrorKind::Io));
        assert!(ops.ops.contains(&CloseOp::CloseWakeR), "wake fd 仍須關閉");
        assert!(ops.ops.contains(&CloseOp::CloseWakeW), "wake fd 仍須關閉");
    }

    #[derive(Default)]
    struct RecOpen {
        ops: Vec<OpenOp>,
        open_err: Option<i32>,
        flock_err: Option<i32>,
        probe: Option<HolderReport>,
        termios_err: Option<i32>,
        warns: Vec<String>,
    }

    impl TtyOpenOps for RecOpen {
        fn open(&mut self) -> Result<(), i32> {
            self.ops.push(OpenOp::Open);
            match self.open_err {
                Some(e) => Err(e),
                None => Ok(()),
            }
        }
        fn flock(&mut self) -> Result<(), i32> {
            self.ops.push(OpenOp::Flock);
            match self.flock_err {
                Some(e) => Err(e),
                None => Ok(()),
            }
        }
        fn excl(&mut self) -> Result<(), i32> {
            self.ops.push(OpenOp::Excl);
            Ok(())
        }
        fn probe_holders(&mut self) -> HolderReport {
            self.ops.push(OpenOp::ProbeHolders);
            self.probe.clone().unwrap_or(HolderReport {
                pids: vec![],
                holder_unknown: true,
                status: HolderScanStatus::Ok,
            })
        }
        fn termios(&mut self) -> Result<(), i32> {
            let r = match self.termios_err {
                Some(e) => Err(e),
                None => Ok(()),
            };
            self.ops.push(OpenOp::Termios(r));
            r
        }
        fn nxcl(&mut self) -> Result<(), i32> {
            self.ops.push(OpenOp::Nxcl);
            Ok(())
        }
        fn close_fd(&mut self) -> Result<(), i32> {
            self.ops.push(OpenOp::CloseFd);
            Ok(())
        }
        fn warn(&mut self, line: &str) {
            self.warns.push(line.to_string());
        }
    }

    #[test]
    fn open_failure_after_excl_runs_nxcl_before_close() {
        // (a) termios 失敗：TIOCEXCL 之後必須 Nxcl 再 CloseFd。
        // 注意：計畫文字寫「尾段恰為 [Excl, Termios(Err), Nxcl, CloseFd]」，但計畫 Step 3 的程式順序是
        // 「TIOCEXCL → 鎖後重掃 → termios」，故完整序列含 ProbeHolders；此處以 Step 3 的順序為準。
        let mut ops = RecOpen {
            termios_err: Some(libc::EIO),
            ..Default::default()
        };
        match open_sequence(&mut ops, "/dev/pts/9") {
            Err(OpenError::Other(_)) => {}
            other => panic!("termios 失敗應回 Other，實際 {other:?}"),
        }
        assert_eq!(
            ops.ops,
            vec![
                OpenOp::Open,
                OpenOp::Flock,
                OpenOp::Excl,
                OpenOp::ProbeHolders,
                OpenOp::Termios(Err(libc::EIO)),
                OpenOp::Nxcl,
                OpenOp::CloseFd,
            ]
        );
        // (b) 鎖後重掃 Reject：尾段恰為 [Excl, ProbeHolders, Nxcl, CloseFd]，不得出現 Termios
        let mut ops = RecOpen {
            probe: Some(HolderReport {
                pids: vec![123],
                holder_unknown: false,
                status: HolderScanStatus::Ok,
            }),
            ..Default::default()
        };
        match open_sequence(&mut ops, "/dev/pts/9") {
            Err(OpenError::Held(rep)) => assert_eq!(rep.pids, vec![123]),
            other => panic!("鎖後重掃 Reject 應回 Held，實際 {other:?}"),
        }
        assert_eq!(
            ops.ops,
            vec![
                OpenOp::Open,
                OpenOp::Flock,
                OpenOp::Excl,
                OpenOp::ProbeHolders,
                OpenOp::Nxcl,
                OpenOp::CloseFd,
            ]
        );
        assert!(
            !ops.ops.iter().any(|o| matches!(o, OpenOp::Termios(_))),
            "Reject 時不得改寫 termios"
        );
        // (c) TIOCEXCL 之前失敗（open／flock）：序列中不得出現 Nxcl
        let mut ops = RecOpen {
            open_err: Some(libc::EACCES),
            ..Default::default()
        };
        let _ = open_sequence(&mut ops, "/dev/pts/9");
        assert!(!ops.ops.contains(&OpenOp::Nxcl), "open 失敗時不得 TIOCNXCL：{:?}", ops.ops);
        let mut ops = RecOpen {
            flock_err: Some(libc::EWOULDBLOCK),
            ..Default::default()
        };
        let _ = open_sequence(&mut ops, "/dev/pts/9");
        assert!(!ops.ops.contains(&OpenOp::Nxcl), "flock 失敗時不得 TIOCNXCL：{:?}", ops.ops);
    }

    #[test]
    fn post_lock_holder_verdict_matrix() {
        let rep = |pids: Vec<u32>, status: HolderScanStatus| HolderReport {
            pids,
            holder_unknown: false,
            status,
        };
        assert_eq!(
            post_lock_holder_verdict(&rep(vec![123], HolderScanStatus::Ok)),
            PostLockVerdict::Reject
        );
        assert_eq!(
            post_lock_holder_verdict(&rep(vec![123], HolderScanStatus::Permission)),
            PostLockVerdict::Reject,
            "已識別即拒絕，不論 status"
        );
        assert_eq!(
            post_lock_holder_verdict(&rep(vec![], HolderScanStatus::Ok)),
            PostLockVerdict::Accept
        );
        assert_eq!(
            post_lock_holder_verdict(&rep(vec![], HolderScanStatus::Unknown)),
            PostLockVerdict::AcceptUnverified
        );
        assert_eq!(
            post_lock_holder_verdict(&rep(vec![], HolderScanStatus::Permission)),
            PostLockVerdict::AcceptUnverified
        );
    }
}
