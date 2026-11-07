//! P8-T7：PAL flash 端點能力（PTY symlink、raw、速率、master 寫入分身、serial line control）。
//!
//! 需要真 PTY 者命名 `cap_posix_pty__*` 並標 `#[ignore = "cap:posix_pty"]`。
#![allow(non_snake_case)]

use std::time::Duration;

use sw_pal::fakes::{
    posix_caps, windows_caps, FakeConsoleFactory, FakeConsoleIo, FakeSerialBackend,
};
use sw_pal::traits::{
    ConsoleIo, ConsoleTransportFactory, DevicePath, PalError, PalErrorKind, PtySlaveRef, Readiness,
    SerialBackend, SerialConfig, TermSpeed,
};
use sw_pal::ConsoleKind;

fn cfg() -> SerialConfig {
    SerialConfig::from_uart_profile(115200, 8, "N", 1, "none", false)
}

fn slave(path: &str) -> PtySlaveRef {
    PtySlaveRef { path: path.to_string() }
}

#[cfg(unix)]
#[test]
fn link_replaces_existing_symlink_and_makedirs() {
    let dir = tempfile::tempdir().unwrap();
    let link = dir.path().join("a/b/ttyMCU");
    let f = sw_pal::console_pty::NativeConsoleFactory::new(ConsoleKind::Pty);
    f.link("/dev/pts/7", &link).expect("link 建立父目錄");
    assert_eq!(std::fs::read_link(&link).unwrap().to_str().unwrap(), "/dev/pts/7");
    f.link("/dev/pts/9", &link).expect("link 取代既有 symlink");
    assert_eq!(std::fs::read_link(&link).unwrap().to_str().unwrap(), "/dev/pts/9");
    f.unlink(&link).expect("unlink");
    assert!(std::fs::symlink_metadata(&link).is_err());
    f.unlink(&link).expect("unlink 不存在視為成功");
}

#[cfg(unix)]
#[test]
fn link_refuses_to_replace_directory() {
    let dir = tempfile::tempdir().unwrap();
    let target = dir.path().join("ttyMCU");
    std::fs::create_dir(&target).unwrap();
    let f = sw_pal::console_pty::NativeConsoleFactory::new(ConsoleKind::Pty);
    assert!(f.link("/dev/pts/7", &target).is_err());
    assert!(target.is_dir());
}

#[test]
fn mirror_never_writes_b0_to_serial() {
    let be = FakeSerialBackend::new();
    let dev = be.add_device("/dev/fake0");
    let h = be.open_exclusive(&DevicePath("/dev/fake0".into()), &cfg()).ok().unwrap();
    let lc = dev.line_control_probe();
    let p = slave("/dev/pts/1");
    for (i, o) in [(0, 0), (0, 4098), (4098, 0)] {
        lc.set_slave_speed(&p.path, Ok(TermSpeed { ispeed: i, ospeed: o }));
        h.mirror_termios_from(&p, Some(115200)).unwrap();
        h.mirror_termios_from(&p, None).unwrap();
    }
    lc.set_slave_speed(&p.path, Err(PalError::plain(PalErrorKind::Io, "tcgetattr")));
    h.mirror_termios_from(&p, Some(57600)).unwrap();
    assert!(
        lc.speed_writes().iter().all(|&(i, o)| i != 0 && o != 0),
        "實體 UART 不得被寫入 B0：{:?}",
        lc.speed_writes()
    );
}

#[test]
fn mirror_writes_nonzero_slave_speed() {
    let be = FakeSerialBackend::new();
    let dev = be.add_device("/dev/fake0");
    let h = be.open_exclusive(&DevicePath("/dev/fake0".into()), &cfg()).ok().unwrap();
    let lc = dev.line_control_probe();
    lc.set_slave_speed("/dev/pts/1", Ok(TermSpeed { ispeed: 4098, ospeed: 4098 }));
    h.mirror_termios_from(&slave("/dev/pts/1"), Some(9600)).unwrap();
    assert_eq!(lc.speed_writes(), vec![(4098, 4098)]);
    assert!(lc.baud_calls().is_empty());
}

#[test]
fn mirror_falls_back_when_slave_speed_unset() {
    let be = FakeSerialBackend::new();
    let dev = be.add_device("/dev/fake0");
    let h = be.open_exclusive(&DevicePath("/dev/fake0".into()), &cfg()).ok().unwrap();
    let lc = dev.line_control_probe();
    lc.set_slave_speed("/dev/pts/1", Ok(TermSpeed { ispeed: 0, ospeed: 0 }));
    h.mirror_termios_from(&slave("/dev/pts/1"), Some(115200)).unwrap();
    assert_eq!(lc.baud_calls(), vec![115200]);
    assert!(lc.speed_writes().is_empty());
    // fallback 的 set_baud 失敗要被忽略。
    lc.fail_set_baud(true);
    h.mirror_termios_from(&slave("/dev/pts/1"), Some(115200)).unwrap();
    // 無 fallback：忽略。
    h.mirror_termios_from(&slave("/dev/pts/1"), None).unwrap();
}

#[test]
fn line_control_survives_split_and_handle_delegates() {
    let be = FakeSerialBackend::new();
    let dev = be.add_device("/dev/fake0");
    let h = be.open_exclusive(&DevicePath("/dev/fake0".into()), &cfg()).ok().unwrap();
    let lc = h.line_control();
    let (_r, _w, _wk, _c) = h.split();
    lc.set_baud(57600).unwrap();
    assert_eq!(dev.line_control_probe().baud_calls(), vec![57600]);
}

#[test]
fn fake_console_clone_writer_try_write_eagain_is_ok_zero() {
    let (io, handle) = FakeConsoleIo::new();
    handle.set_tx_capacity(4);
    let mut w = io.clone_writer().unwrap();
    assert_eq!(w.try_write(b"abcdef").unwrap(), 4);
    assert_eq!(w.try_write(b"x").unwrap(), 0);
    assert_eq!(handle.take_tx(), b"abcd");
}

#[test]
fn fake_wait_readable_woken_ready_timeout() {
    let (mut io, handle) = FakeConsoleIo::new();
    assert_eq!(io.wait_readable(Some(Duration::from_millis(20))).unwrap(), Readiness::TimedOut);
    io.waker().wake();
    assert_eq!(io.wait_readable(Some(Duration::from_secs(2))).unwrap(), Readiness::Woken);
    handle.push_rx(b"x");
    assert_eq!(io.wait_readable(Some(Duration::from_secs(2))).unwrap(), Readiness::Ready);
}

#[test]
fn fake_console_factory_records_flash_ops() {
    let f = FakeConsoleFactory::with_flash(ConsoleKind::Pty, true);
    f.link("/dev/pts/3", std::path::Path::new("/x/ttyMCU")).unwrap();
    assert_eq!(f.links().get(std::path::Path::new("/x/ttyMCU")).map(String::as_str), Some("/dev/pts/3"));
    f.tty_setraw(&slave("/dev/pts/3")).unwrap();
    assert_eq!(f.raw_calls(), vec!["/dev/pts/3".to_string()]);
    f.set_speed("/dev/pts/3", TermSpeed { ispeed: 4098, ospeed: 4098 });
    assert_eq!(f.tcgetattr_speed(&slave("/dev/pts/3")).unwrap().ispeed, 4098);
    f.unlink(std::path::Path::new("/x/ttyMCU")).unwrap();
    assert!(f.links().is_empty());
}

#[test]
fn cloned_console_factory_shares_flash_state() {
    let f = FakeConsoleFactory::new(ConsoleKind::Pty);
    let copy = f.clone();
    copy.link("/dev/pts/3", std::path::Path::new("/x/ttyMCU")).unwrap();
    copy.tty_setraw(&slave("/dev/pts/3")).unwrap();
    copy.set_speed("/dev/pts/3", TermSpeed { ispeed: 4098, ospeed: 4098 });
    assert_eq!(f.links(), copy.links());
    assert_eq!(f.raw_calls(), vec!["/dev/pts/3"]);
    assert_eq!(f.tcgetattr_speed(&slave("/dev/pts/3")).unwrap().ospeed, 4098);
    f.unlink(std::path::Path::new("/x/ttyMCU")).unwrap();
    assert!(copy.links().is_empty());
}

#[test]
fn flash_console_io_preserves_mux_and_writer_behavior() {
    use sw_pal::console_mux::MuxEvent;
    let f = FakeConsoleFactory::new(ConsoleKind::Pty);
    let (mut io, handle) = FakeConsoleIo::new();
    let mut writer = io.clone_writer().unwrap();
    handle.push_rx(b"rx");
    assert_eq!(io.wait_readable(Some(Duration::ZERO)).unwrap(), Readiness::Ready);
    let mut mux = f.new_mux().unwrap();
    mux.add_io(1, Box::new(io)).unwrap();
    assert!(mux.wait(Duration::ZERO).unwrap().contains(&MuxEvent::Readable(1)));
    let mut buf = [0; 2];
    assert!(matches!(mux.read(1, &mut buf).unwrap(), sw_pal::traits::ReadOutcome::Data(2)));
    assert_eq!(&buf, b"rx");
    drop(mux.remove(1).unwrap());
    assert_eq!(writer.try_write(b"tx").unwrap(), 2);
    assert_eq!(handle.take_tx(), b"tx");
}

#[test]
fn windows_flash_caps_unsupported() {
    assert!(posix_caps().flash_endpoint);
    assert!(!windows_caps().flash_endpoint);
    // Windows 形狀的替身：flash 端點方法全回 Unsupported。
    let f = FakeConsoleFactory::with_flash(ConsoleKind::TcpTelnet, false);
    let unsup = |e: PalError| assert_eq!(e.kind, PalErrorKind::Unsupported);
    unsup(f.link("/dev/pts/1", std::path::Path::new("/x")).unwrap_err());
    unsup(f.unlink(std::path::Path::new("/x")).unwrap_err());
    unsup(f.tty_setraw(&slave("p")).unwrap_err());
    unsup(f.tcgetattr_speed(&slave("p")).unwrap_err());
    // 沒覆寫任何新方法的既有替身：預設 Unsupported。
    struct Legacy;
    impl ConsoleTransportFactory for Legacy {
        fn kind(&self) -> ConsoleKind {
            ConsoleKind::TcpTelnet
        }
    }
    unsup(Legacy.link("a", std::path::Path::new("/x")).unwrap_err());
    unsup(Legacy.tty_setraw(&slave("p")).unwrap_err());
}

#[cfg(windows)]
#[test]
fn windows_native_flash_unsupported() {
    let f = sw_pal::console_pty::NativeConsoleFactory::new(ConsoleKind::TcpTelnet);
    assert_eq!(f.tty_setraw(&slave("p")).unwrap_err().kind, PalErrorKind::Unsupported);
    assert!(!sw_pal::native().caps.flash_endpoint);
}

#[cfg(unix)]
mod posix_pty {
    use super::*;
    use sw_pal::console_pty::NativeConsoleFactory;

    unsafe fn tc(path: &str) -> libc::termios {
        let c = std::ffi::CString::new(path).unwrap();
        let fd = libc::open(c.as_ptr(), libc::O_RDONLY | libc::O_NOCTTY | libc::O_NONBLOCK);
        assert!(fd >= 0, "open slave");
        let mut t: libc::termios = std::mem::zeroed();
        assert_eq!(libc::tcgetattr(fd, &mut t), 0);
        libc::close(fd);
        t
    }

    #[test]
    #[ignore = "cap:posix_pty"]
    fn cap_posix_pty__tty_setraw_matches_cpython_setraw() {
        let f = NativeConsoleFactory::new(ConsoleKind::Pty);
        let pair = f.create_pty().unwrap();
        // 先弄成「像 cooked」的初始狀態，讓每個旗標都有東西可清。
        let before = unsafe {
            let c = std::ffi::CString::new(pair.slave_path.clone()).unwrap();
            let fd = libc::open(c.as_ptr(), libc::O_RDWR | libc::O_NOCTTY | libc::O_NONBLOCK);
            let mut t: libc::termios = std::mem::zeroed();
            assert_eq!(libc::tcgetattr(fd, &mut t), 0);
            t.c_iflag |= libc::BRKINT | libc::ICRNL | libc::INPCK | libc::ISTRIP | libc::IXON;
            t.c_oflag |= libc::OPOST;
            t.c_cflag &= !(libc::CSIZE | libc::PARENB);
            t.c_cflag |= libc::CS7 | libc::PARENB;
            t.c_lflag |= libc::ECHO | libc::ICANON | libc::IEXTEN | libc::ISIG;
            t.c_cc[libc::VMIN] = 0;
            t.c_cc[libc::VTIME] = 5;
            assert_eq!(libc::tcsetattr(fd, libc::TCSANOW, &t), 0);
            libc::close(fd);
            t
        };
        f.tty_setraw(&pair.slave_ref).unwrap();
        let after = unsafe { tc(&pair.slave_path) };
        // CPython tty.setraw 公式。
        let mut exp = before;
        exp.c_iflag &= !(libc::BRKINT | libc::ICRNL | libc::INPCK | libc::ISTRIP | libc::IXON);
        exp.c_oflag &= !libc::OPOST;
        exp.c_cflag &= !(libc::CSIZE | libc::PARENB);
        exp.c_cflag |= libc::CS8;
        exp.c_lflag &= !(libc::ECHO | libc::ICANON | libc::IEXTEN | libc::ISIG);
        exp.c_cc[libc::VMIN] = 1;
        exp.c_cc[libc::VTIME] = 0;
        assert_eq!(after.c_iflag, exp.c_iflag, "c_iflag");
        assert_eq!(after.c_oflag, exp.c_oflag, "c_oflag");
        assert_eq!(after.c_lflag, exp.c_lflag, "c_lflag");
        assert_eq!(after.c_cflag, exp.c_cflag, "c_cflag");
        assert_eq!(after.c_cc[libc::VMIN], 1);
        assert_eq!(after.c_cc[libc::VTIME], 0);
    }

    #[test]
    #[ignore = "cap:posix_pty"]
    fn cap_posix_pty__create_pty_preserves_kernel_default_speed() {
        let f = NativeConsoleFactory::new(ConsoleKind::Pty);
        let pair = f.create_pty().unwrap();
        let sp = f.tcgetattr_speed(&pair.slave_ref).unwrap();
        assert_ne!(sp.ispeed, libc::B0 as u32, "ispeed 不得為 B0");
        assert_ne!(sp.ospeed, libc::B0 as u32, "ospeed 不得為 B0");
    }

    #[test]
    #[ignore = "cap:posix_pty"]
    fn cap_posix_pty__clone_writer_try_write_eagain_is_ok_zero() {
        let pair = NativeConsoleFactory::new(ConsoleKind::Pty).create_pty().unwrap();
        let mut w = pair.master.clone_writer().unwrap();
        let chunk = [b'a'; 1024];
        let mut total = 0usize;
        let mut hit_zero = false;
        for _ in 0..4096 {
            let n = w.try_write(&chunk).expect("EAGAIN 必須是 Ok(0) 而非 Err");
            if n == 0 {
                hit_zero = true;
                break;
            }
            total += n;
        }
        assert!(hit_zero, "寫了 {total} 位元組仍未 EAGAIN");
        assert!(total > 0);
    }

    #[test]
    #[ignore = "cap:posix_pty"]
    fn cap_posix_pty__wait_readable_woken_by_waker() {
        use std::io::Write;
        use std::os::unix::fs::OpenOptionsExt;
        let mut pair = NativeConsoleFactory::new(ConsoleKind::Pty).create_pty().unwrap();
        assert_eq!(
            pair.master.wait_readable(Some(Duration::from_millis(30))).unwrap(),
            Readiness::TimedOut
        );
        let waker = pair.master.waker();
        let t = std::thread::spawn(move || {
            std::thread::sleep(Duration::from_millis(50));
            waker.wake();
        });
        assert_eq!(pair.master.wait_readable(Some(Duration::from_secs(5))).unwrap(), Readiness::Woken);
        t.join().unwrap();
        // 喚醒已消耗，不殘留。
        assert_eq!(
            pair.master.wait_readable(Some(Duration::from_millis(30))).unwrap(),
            Readiness::TimedOut
        );
        let mut s = std::fs::OpenOptions::new()
            .write(true)
            .custom_flags(libc::O_NOCTTY | libc::O_NONBLOCK)
            .open(&pair.slave_path)
            .unwrap();
        s.write_all(b"x").unwrap();
        assert_eq!(pair.master.wait_readable(Some(Duration::from_secs(5))).unwrap(), Readiness::Ready);
        // wait_readable 不讀：資料仍在。
        let mut buf = [0u8; 4];
        assert!(matches!(
            pair.master.read(&mut buf, Some(Duration::from_secs(1))),
            Ok(sw_pal::traits::ReadOutcome::Data(1))
        ));
    }
}
