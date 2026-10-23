#![cfg(feature = "test-fakes")]

use sw_pal::serial::windows_model::*;
use sw_pal::*;

#[test]
fn dcb_matches_pyserial_defaults_rtscts() {
    let model = dcb_model(&SerialConfig::from_uart_profile(
        115200, 8, "N", 1, "rtscts", false,
    ));
    assert_eq!(
        (model.baud, model.byte_size, model.parity, model.stop_bits),
        (115200, 8, 0, 0)
    );
    assert!(model.f_binary && model.f_outx_cts_flow);
    assert_eq!(model.rts_control, 2);
    assert_eq!(model.dtr_control, 1);
    assert!(!model.f_outx && !model.f_inx);
    assert!(!model.f_dsr_sensitivity && !model.f_outx_dsr_flow);
    assert_eq!(PYSERIAL_ERROR_CHAR, 0);
    assert_eq!((PYSERIAL_XON_CHAR, PYSERIAL_XOFF_CHAR), (0x11, 0x13));
}

#[test]
fn dcb_flags_clear_inherited_error_and_null_modes() {
    let model = dcb_model(&SerialConfig::from_uart_profile(
        115200, 8, "N", 1, "none", false,
    ));
    let dirty = (1 << 10) | (1 << 11) | (1 << 14) | (1 << 7);
    let configured = dcb_flags(dirty, &model);
    assert_eq!(configured & ((1 << 10) | (1 << 11) | (1 << 14)), 0);
    assert_ne!(configured & (1 << 7), 0, "未管理的 DCB 旗標應保留");
}

#[test]
fn comm_timeouts_follow_pyserial_constants() {
    let timeouts = comm_timeouts_model();
    assert_eq!(
        (
            timeouts.read_interval,
            timeouts.read_total_multiplier,
            timeouts.read_total_constant
        ),
        (u32::MAX, u32::MAX, 200)
    );
    assert_eq!(
        (
            timeouts.write_total_multiplier,
            timeouts.write_total_constant
        ),
        (0, 5000)
    );
}

#[test]
fn device_name_normalization() {
    assert_eq!(win_device_name("com3"), r"\\.\COM3");
    assert_eq!(win_device_name(r"\\.\COM12"), r"\\.\COM12");
    assert_eq!(win_device_name("/dev/ttyUSB0"), "/dev/ttyUSB0");
}

#[test]
fn access_denied_is_not_a_holder_claim() {
    assert!(matches!(
        map_open_error(5, "[WinError 5] Access is denied.".into()),
        OpenErrorKind::AccessDenied
    ));
    assert!(matches!(
        map_open_error(2, "[WinError 2] x".into()),
        OpenErrorKind::NotFound
    ));
    assert!(matches!(
        map_open_error(3, "[WinError 3] x".into()),
        OpenErrorKind::NotFound
    ));
    assert!(matches!(
        map_open_error(31, "[WinError 31] x".into()),
        OpenErrorKind::Other
    ));
}

#[test]
fn overlapped_completion_mapping() {
    assert_eq!(
        map_overlapped_completion(true, 10, 0),
        (TxOutcome::Completed(10), None)
    );
    assert_eq!(
        map_overlapped_write_completion(false, 4, 10, 995, true),
        (TxOutcome::Completed(4), Some(PalErrorKind::Cancelled))
    );
    assert_eq!(
        map_overlapped_completion(false, 4, 995),
        (TxOutcome::Unknown { confirmed: 0 }, Some(PalErrorKind::Io))
    );
    assert_eq!(
        map_overlapped_completion(true, 3, 1460),
        (TxOutcome::Completed(3), Some(PalErrorKind::TimedOut))
    );
    assert_eq!(
        map_overlapped_write_completion(true, 3, 10, 0, false),
        (TxOutcome::Completed(3), Some(PalErrorKind::TimedOut))
    );
    assert_eq!(
        map_overlapped_write_completion(true, 3, 10, 0, true),
        (TxOutcome::Completed(3), Some(PalErrorKind::Cancelled))
    );
    assert_eq!(
        map_overlapped_write_completion(true, 0, 256, 0, false),
        (TxOutcome::Completed(0), Some(PalErrorKind::TimedOut))
    );
    assert_eq!(
        map_overlapped_write_completion(false, 4, 10, 995, false),
        (TxOutcome::Unknown { confirmed: 0 }, Some(PalErrorKind::Io))
    );
    assert_eq!(
        map_overlapped_write_completion(false, 0, 10, 1460, false),
        (TxOutcome::Completed(0), Some(PalErrorKind::TimedOut))
    );
    assert_eq!(
        map_overlapped_completion(false, 0, 1617),
        (TxOutcome::Unknown { confirmed: 0 }, Some(PalErrorKind::Io))
    );
    assert_eq!(
        map_overlapped_completion(false, 0, 22),
        (TxOutcome::Unknown { confirmed: 0 }, Some(PalErrorKind::Io))
    );
}

#[test]
fn missing_com_error_code_maps_to_not_found() {
    assert_eq!(
        map_open_error(
            2,
            "[WinError 2] The system cannot find the file specified.".into()
        ),
        OpenErrorKind::NotFound
    );
}

#[cfg(windows)]
mod windows_native {
    #![allow(non_snake_case)]

    use std::sync::Mutex;
    use std::thread;
    use std::time::{Duration, Instant};

    use sw_pal::conformance::{serial_suite, PeerEnd};
    use sw_pal::serial::windows::WindowsSerialBackend;
    use sw_pal::*;
    use windows_sys::Win32::Devices::Communication::{
        GetCommState, SetCommState, SetCommTimeouts, SetupComm, COMMTIMEOUTS, DCB,
    };
    use windows_sys::Win32::Foundation::{
        CloseHandle, GetLastError, GENERIC_READ, GENERIC_WRITE, HANDLE, INVALID_HANDLE_VALUE,
    };
    use windows_sys::Win32::Storage::FileSystem::{
        CreateFileW, ReadFile, WriteFile, FILE_ATTRIBUTE_NORMAL, OPEN_EXISTING,
    };

    static COM_TEST_LOCK: Mutex<()> = Mutex::new(());

    fn configured(baud: u32, flow: &str) -> SerialConfig {
        SerialConfig::from_uart_profile(baud, 8, "N", 1, flow, false)
    }

    #[test]
    fn nonexistent_com_is_not_found() {
        let backend = WindowsSerialBackend::new();
        match backend.open_exclusive(&DevicePath("COM250".into()), &configured(115200, "none")) {
            Err(OpenError::NotFound(error)) => assert!(
                error.py_message.starts_with("[WinError 2] "),
                "CreateFileW error should preserve WinError 2, got {:?}",
                error.py_message
            ),
            Err(other) => panic!("COM250 應回 NotFound，實際 {other:?}"),
            Ok(_) => panic!("COM250 不存在時不得成功開啟"),
        }
    }

    #[test]
    #[ignore = "cap:windows_com"]
    fn cap_windows_com__dcb_matches_pyserial_after_dirty_state() {
        let _guard = COM_TEST_LOCK
            .lock()
            .unwrap_or_else(std::sync::PoisonError::into_inner);
        let (dut, _) = com_pair();
        let dirty = WindowsComPeer::open(&dut, false);
        let mut dcb = DCB::default();
        dcb.DCBlength = std::mem::size_of::<DCB>() as u32;
        // SAFETY: 測試 peer 持有有效的 COM20 handle，`dcb` 可寫。
        assert_ne!(unsafe { GetCommState(dirty.handle, &mut dcb) }, 0);
        dcb._bitfield |= (1 << 10) | (1 << 11) | (1 << 14);
        dcb.ErrorChar = 0x55;
        dcb.XonChar = 0x12;
        dcb.XoffChar = 0x14;
        // SAFETY: 只污染本測試專用的 COM20 DCB，隨後由後端重開並校正。
        assert_ne!(unsafe { SetCommState(dirty.handle, &dcb) }, 0);
        drop(dirty);

        let handle = WindowsSerialBackend::new()
            .open_exclusive(&DevicePath(dut.clone()), &configured(115200, "none"))
            .expect("COM20 開啟並校正 DCB");
        let (reader, writer, _, close) = handle.split();
        close.close(reader, writer);

        let verify = WindowsComPeer::open(&dut, false);
        let mut dcb = DCB::default();
        dcb.DCBlength = std::mem::size_of::<DCB>() as u32;
        // SAFETY: 測試 peer 持有有效的 COM20 handle，`dcb` 可寫。
        assert_ne!(unsafe { GetCommState(verify.handle, &mut dcb) }, 0);
        assert_eq!(dcb._bitfield & ((1 << 10) | (1 << 11) | (1 << 14)), 0);
        assert_eq!(dcb.ErrorChar, 0);
        assert_eq!((dcb.XonChar as u8, dcb.XoffChar as u8), (0x11, 0x13));
    }

    fn com_pair() -> (String, String) {
        let raw = std::env::var("SW_TEST_COM_PAIR")
            .expect("windows_com 測試需設定 SW_TEST_COM_PAIR=COM20:COM21");
        let (dut, peer) = raw
            .split_once(':')
            .expect("SW_TEST_COM_PAIR 格式須為 COM20:COM21");
        assert_eq!(dut.to_ascii_uppercase(), "COM20");
        assert_eq!(peer.to_ascii_uppercase(), "COM21");
        (dut.to_ascii_uppercase(), peer.to_ascii_uppercase())
    }

    fn com_pair_factory() -> impl Fn() -> (DevicePath, Box<dyn PeerEnd>) {
        let (dut, peer) = com_pair();
        move || {
            (
                DevicePath(dut.clone()),
                Box::new(WindowsComPeer::open(&peer, false)) as Box<dyn PeerEnd>,
            )
        }
    }

    struct WindowsComPeer {
        handle: HANDLE,
        reading: bool,
    }

    unsafe impl Send for WindowsComPeer {}

    impl WindowsComPeer {
        fn open(port: &str, rtscts: bool) -> Self {
            let name = sw_pal::serial::windows_model::win_device_name(port);
            let wide: Vec<u16> = name.encode_utf16().chain(std::iter::once(0)).collect();
            // SAFETY: `wide` 是 NUL 結尾路徑，成功 handle 由此測試 peer 持有。
            let handle = unsafe {
                CreateFileW(
                    wide.as_ptr(),
                    GENERIC_READ | GENERIC_WRITE,
                    0,
                    std::ptr::null(),
                    OPEN_EXISTING,
                    FILE_ATTRIBUTE_NORMAL,
                    std::ptr::null_mut(),
                )
            };
            assert_ne!(
                handle,
                INVALID_HANDLE_VALUE,
                "開啟 {port} 失敗：{}",
                last_error()
            );

            let mut dcb = DCB::default();
            dcb.DCBlength = std::mem::size_of::<DCB>() as u32;
            // SAFETY: `dcb` 為可寫結構，handle 已成功開啟。
            assert_ne!(unsafe { GetCommState(handle, &mut dcb) }, 0);
            dcb.BaudRate = 115200;
            dcb.ByteSize = 8;
            dcb.Parity = 0;
            dcb.StopBits = 0;
            let mut flags = dcb._bitfield | 1 | (1 << 4);
            flags &= !((1 << 2) | (0b11 << 12));
            if rtscts {
                flags |= (1 << 2) | (2 << 12);
            } else {
                flags |= 1 << 12;
            }
            dcb._bitfield = flags;
            // SAFETY: `dcb` 是有效的 DCB 結構。
            assert_ne!(unsafe { SetCommState(handle, &dcb) }, 0);
            let timeouts = COMMTIMEOUTS {
                ReadIntervalTimeout: u32::MAX,
                ReadTotalTimeoutMultiplier: u32::MAX,
                ReadTotalTimeoutConstant: 200,
                WriteTotalTimeoutMultiplier: 0,
                WriteTotalTimeoutConstant: 5000,
            };
            // SAFETY: `timeouts` 為有效的 COMMTIMEOUTS 結構。
            assert_ne!(unsafe { SetCommTimeouts(handle, &timeouts) }, 0);
            // SAFETY: handle 是有效的序列埠 handle。
            assert_ne!(unsafe { SetupComm(handle, 4096, 4096) }, 0);
            Self {
                handle,
                reading: true,
            }
        }
    }

    struct PanicAfterPendingGate;

    impl SubmitGate for PanicAfterPendingGate {
        fn submit(
            &self,
            os_submit: &mut dyn FnMut() -> SubmitStep,
        ) -> Result<SubmitStep, SubmitRevoked> {
            let step = os_submit();
            if matches!(step, SubmitStep::Pending) {
                panic!("測試：OS 已接受 overlapped 寫入後 gate panic");
            }
            Ok(step)
        }
    }

    struct RevokeAfterPendingGate;

    impl SubmitGate for RevokeAfterPendingGate {
        fn submit(
            &self,
            os_submit: &mut dyn FnMut() -> SubmitStep,
        ) -> Result<SubmitStep, SubmitRevoked> {
            let step = os_submit();
            if matches!(step, SubmitStep::Pending) {
                Err(SubmitRevoked)
            } else {
                Ok(step)
            }
        }
    }

    impl Drop for WindowsComPeer {
        fn drop(&mut self) {
            // SAFETY: 此測試 peer 唯一持有 handle，且只在 drop 時關閉。
            unsafe {
                CloseHandle(self.handle);
            }
        }
    }

    impl PeerEnd for WindowsComPeer {
        fn read_available(&mut self, timeout: Duration) -> Vec<u8> {
            if !self.reading {
                thread::sleep(timeout);
                return Vec::new();
            }
            let mut buffer = vec![0u8; 4096];
            let mut count = 0u32;
            // SAFETY: 同步 handle 與 `buffer` 均在呼叫期間有效。
            let ok = unsafe {
                ReadFile(
                    self.handle,
                    buffer.as_mut_ptr(),
                    buffer.len() as u32,
                    &mut count,
                    std::ptr::null_mut(),
                )
            };
            assert_ne!(ok, 0, "COM peer ReadFile 失敗：{}", last_error());
            buffer.truncate(count as usize);
            buffer
        }

        fn write(&mut self, bytes: &[u8]) {
            let mut sent = 0usize;
            while sent < bytes.len() {
                let mut count = 0u32;
                // SAFETY: 同步 handle 與 bytes 子切片均在呼叫期間有效。
                let ok = unsafe {
                    WriteFile(
                        self.handle,
                        bytes[sent..].as_ptr(),
                        (bytes.len() - sent) as u32,
                        &mut count,
                        std::ptr::null_mut(),
                    )
                };
                assert_ne!(ok, 0, "COM peer WriteFile 失敗：{}", last_error());
                assert!(count > 0, "COM peer WriteFile 未寫入資料");
                sent += count as usize;
            }
        }

        fn stop_reading(&mut self) {
            self.reading = false;
        }

        fn resume_reading(&mut self) {
            self.reading = true;
        }
    }

    fn last_error() -> u32 {
        // SAFETY: 讀取目前測試執行緒的 Win32 錯誤碼。
        unsafe { GetLastError() }
    }

    #[test]
    #[ignore = "cap:windows_com"]
    fn cap_windows_com__second_open_access_denied() {
        let _guard = COM_TEST_LOCK
            .lock()
            .unwrap_or_else(std::sync::PoisonError::into_inner);
        let (dut, _) = com_pair();
        let backend = WindowsSerialBackend::new();
        let dev = DevicePath(dut);
        let first = backend
            .open_exclusive(&dev, &configured(115200, "none"))
            .expect("第一個開啟應成功");
        match backend.open_exclusive(&dev, &configured(115200, "none")) {
            Err(OpenError::AccessDenied { holder, .. }) => {
                assert!(holder.pids.is_empty());
                assert!(holder.holder_unknown);
                assert_eq!(holder.status, HolderScanStatus::Unsupported);
            }
            Err(other) => panic!("第二次開啟預期 AccessDenied，實際 {other:?}"),
            Ok(_) => panic!("COM20 的第二次獨占開啟必須失敗"),
        }
        drop(first);
    }

    #[test]
    #[ignore = "cap:windows_com"]
    fn cap_windows_com__cancel_pending_read_returns_woken() {
        let _guard = COM_TEST_LOCK
            .lock()
            .unwrap_or_else(std::sync::PoisonError::into_inner);
        let (dut, _) = com_pair();
        let backend = std::sync::Arc::new(WindowsSerialBackend::new());
        let handle = backend
            .open_exclusive(&DevicePath(dut), &configured(115200, "none"))
            .expect("COM20 開啟");
        let (reader, writer, waker, close) = handle.split();
        let wake_for_thread = std::sync::Arc::clone(&waker);
        let join = thread::spawn(move || {
            let mut reader = reader;
            let mut buffer = [0u8; 64];
            let outcome = reader.read(&mut buffer, None);
            (reader, outcome)
        });
        let deadline = Instant::now() + Duration::from_secs(3);
        while backend.pending_ops() == 0 && Instant::now() < deadline {
            thread::sleep(Duration::from_millis(5));
        }
        assert!(backend.pending_ops() > 0, "ReadFile 應進入 pending");
        thread::sleep(Duration::from_millis(250));
        assert!(
            !join.is_finished(),
            "read(None) 應略過 COMMTIMEOUTS 的 200 ms idle timeout"
        );
        wake_for_thread.wake();
        let (reader, outcome) = join.join().expect("reader thread");
        assert_eq!(outcome.expect("read"), ReadOutcome::Woken);
        assert_eq!(backend.pending_ops(), 0, "回傳前須收斂 OVERLAPPED");
        close.close(reader, writer);
    }

    #[test]
    #[ignore = "cap:windows_com"]
    fn cap_windows_com__cancel_blocked_write_partial() {
        let _guard = COM_TEST_LOCK
            .lock()
            .unwrap_or_else(std::sync::PoisonError::into_inner);
        let (dut, peer_name) = com_pair();
        let mut peer = WindowsComPeer::open(&peer_name, true);
        peer.stop_reading();
        let backend = std::sync::Arc::new(WindowsSerialBackend::new());
        let handle = backend
            .open_exclusive(&DevicePath(dut), &configured(115200, "rtscts"))
            .expect("COM20 開啟");
        let (reader, mut writer, waker, close) = handle.split();
        let cancel = CancelToken::new(waker);
        let cancel_for_thread = cancel.clone();
        let payload = vec![b'W'; 1 << 20];
        let join = thread::spawn(move || {
            let result = writer.write(&payload, &cancel_for_thread);
            (reader, writer, result)
        });
        let deadline = Instant::now() + Duration::from_secs(5);
        while backend.pending_ops() == 0 && Instant::now() < deadline {
            thread::sleep(Duration::from_millis(5));
        }
        assert!(backend.pending_ops() > 0, "WriteFile 應進入 pending");
        thread::sleep(Duration::from_millis(200));
        cancel.cancel();
        let (reader, writer, result) = join.join().expect("writer thread");
        match result.outcome {
            TxOutcome::Completed(count) => assert!(
                count > 0 && count < (1 << 20),
                "取消應回報部分寫入 bytes，實際 count={count}"
            ),
            other => panic!("取消後預期部分 Completed，實際 {other:?}"),
        }
        assert_eq!(
            result.error.as_ref().map(|error| error.kind),
            Some(PalErrorKind::Cancelled)
        );
        assert_eq!(backend.pending_ops(), 0, "回傳前須收斂 OVERLAPPED");
        close.close(reader, writer);
        peer.resume_reading();
        let deadline = Instant::now() + Duration::from_secs(2);
        let mut quiet_reads = 0;
        while Instant::now() < deadline && quiet_reads < 2 {
            if peer.read_available(Duration::from_millis(200)).is_empty() {
                quiet_reads += 1;
            } else {
                quiet_reads = 0;
            }
        }
    }

    #[test]
    #[ignore = "cap:windows_com"]
    fn cap_windows_com__submit_gate_panic_reaps_pending_write() {
        let _guard = COM_TEST_LOCK
            .lock()
            .unwrap_or_else(std::sync::PoisonError::into_inner);
        let (dut, peer_name) = com_pair();
        let mut peer = WindowsComPeer::open(&peer_name, true);
        peer.stop_reading();
        let backend = WindowsSerialBackend::new();
        let handle = backend
            .open_exclusive(&DevicePath(dut), &configured(115200, "rtscts"))
            .expect("COM20 開啟");
        let (reader, mut writer, waker, close) = handle.split();
        let cancel = CancelToken::new(waker);
        let payload = vec![b'P'; 1 << 20];
        let panic = std::panic::catch_unwind(std::panic::AssertUnwindSafe(|| {
            writer.write_gated(&payload, &cancel, &PanicAfterPendingGate)
        }));
        assert!(panic.is_err(), "gate 應在 WriteFile pending 後 panic");
        assert_eq!(
            backend.pending_ops(),
            0,
            "gate panic unwind 前須取消並收斂 OVERLAPPED"
        );
        assert_eq!(
            backend.cancel_reaps(),
            1,
            "gate panic 必須經 CancelIoEx 與 GetOverlappedResult 收斂一次"
        );
        close.close(reader, writer);
        peer.resume_reading();
    }

    #[test]
    #[ignore = "cap:windows_com"]
    fn cap_windows_com__gate_revoke_after_pending_write_reaps_and_accounts() {
        let _guard = COM_TEST_LOCK
            .lock()
            .unwrap_or_else(std::sync::PoisonError::into_inner);
        let (dut, peer_name) = com_pair();
        let mut peer = WindowsComPeer::open(&peer_name, true);
        peer.stop_reading();
        let backend = std::sync::Arc::new(WindowsSerialBackend::new());
        let handle = backend
            .open_exclusive(&DevicePath(dut), &configured(115200, "rtscts"))
            .expect("COM20 開啟");
        let (reader, mut writer, waker, close) = handle.split();
        let cancel = CancelToken::new(waker);
        let payload = vec![b'R'; 1 << 20];
        let result = writer.write_gated(&payload, &cancel, &RevokeAfterPendingGate);
        match result.outcome {
            TxOutcome::Completed(count) => assert!(count > 0, "應記錄先前已確認的 bytes"),
            TxOutcome::Unknown { confirmed } => {
                assert!(confirmed > 0, "Unknown 必須保留先前確認的 bytes")
            }
            TxOutcome::NotSubmitted => panic!("gate 在 OS 提交後撤銷，不可誤報零 TX"),
        }
        assert_eq!(backend.pending_ops(), 0, "返回前須收斂 OVERLAPPED");
        close.close(reader, writer);
        peer.resume_reading();
    }

    #[test]
    #[ignore = "cap:windows_com"]
    fn cap_windows_com__write_timeout_returns_partial() {
        let _guard = COM_TEST_LOCK
            .lock()
            .unwrap_or_else(std::sync::PoisonError::into_inner);
        let (dut, peer_name) = com_pair();
        let mut peer = WindowsComPeer::open(&peer_name, true);
        peer.stop_reading();
        let backend = std::sync::Arc::new(WindowsSerialBackend::new());
        let handle = backend
            .open_exclusive(&DevicePath(dut), &configured(115200, "rtscts"))
            .expect("COM20 開啟");
        let (reader, writer, waker, close) = handle.split();
        let cancel = CancelToken::new(waker);
        let cancel_for_thread = cancel.clone();
        let payload = vec![b'T'; 1 << 20];
        let payload_len = payload.len();
        let join = thread::spawn(move || {
            let mut writer = writer;
            let result = writer.write(&payload, &cancel_for_thread);
            (reader, writer, result)
        });
        let deadline = Instant::now() + Duration::from_secs(8);
        while !join.is_finished() && Instant::now() < deadline {
            thread::sleep(Duration::from_millis(10));
        }
        let finished_before_watchdog = join.is_finished();
        if !finished_before_watchdog {
            cancel.cancel();
        }
        let (reader, writer, result) = join.join().expect("writer thread");
        assert!(
            finished_before_watchdog,
            "WriteFile write timeout did not complete before watchdog: {result:?}"
        );
        assert!(matches!(result.outcome, TxOutcome::Completed(count) if count < payload_len));
        assert_eq!(
            result.error.as_ref().map(|error| error.kind),
            Some(PalErrorKind::TimedOut),
            "COMMTIMEOUTS 短寫應回報 timeout"
        );
        assert_eq!(backend.pending_ops(), 0, "回傳前須收斂 OVERLAPPED");
        close.close(reader, writer);
        peer.resume_reading();
        let deadline = Instant::now() + Duration::from_secs(2);
        let mut quiet_reads = 0;
        while Instant::now() < deadline && quiet_reads < 2 {
            if peer.read_available(Duration::from_millis(200)).is_empty() {
                quiet_reads += 1;
            } else {
                quiet_reads = 0;
            }
        }
    }

    #[test]
    #[ignore = "cap:windows_com"]
    fn cap_windows_com__conformance_suite() {
        let _guard = COM_TEST_LOCK
            .lock()
            .unwrap_or_else(std::sync::PoisonError::into_inner);
        let backend = std::sync::Arc::new(WindowsSerialBackend::new());
        let factory = com_pair_factory();
        serial_suite(
            backend,
            &factory,
            sw_pal::conformance::SerialPairCaps {
                eof_on_peer_close: false,
            },
        );
    }
}
