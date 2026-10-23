#![cfg(feature = "test-fakes")]

use std::path::Path;

fn native_suite(dir: &Path) {
    sw_pal::conformance::wal_fs_suite(
        sw_pal::native().wal_fs,
        dir,
        &sw_pal::conformance::NativeExternalFs,
    );
}

#[test]
fn native_conformance() {
    let dir = tempfile::tempdir().unwrap();
    native_suite(dir.path());
}

#[test]
fn fake_conformance() {
    let fs = sw_pal::fakes::wal_fs::FakeWalFs::new();
    let dir = Path::new("/w");
    sw_pal::conformance::wal_fs_suite(
        fs.clone(),
        dir,
        &sw_pal::conformance::FakeExternalFs(fs.clone()),
    );
    fs.set_dir_writable(dir, false);
    assert!(!sw_pal::WalFs::dir_writable(fs.as_ref(), dir));
}

#[cfg(unix)]
#[test]
fn posix_rlimit_fsize_partial_write_reports_sent() {
    use std::process::Command;

    if std::env::var_os("SW_PAL_FSIZE_CHILD").is_some() {
        let path = std::env::var_os("SW_PAL_FSIZE_PATH").unwrap();
        let path = std::path::PathBuf::from(path);
        // 安全性：子行程只忽略 SIGXFSZ，避免核心傾印，且只設定自身的 RLIMIT_FSIZE。
        unsafe {
            libc::signal(libc::SIGXFSZ, libc::SIG_IGN);
        }
        let limit = libc::rlimit {
            rlim_cur: 10,
            rlim_max: 10,
        };
        // 安全性：限制只作用於此測試子行程，不影響 Cargo 或其他測試 thread。
        assert_eq!(unsafe { libc::setrlimit(libc::RLIMIT_FSIZE, &limit) }, 0);

        let mut file = sw_pal::native().wal_fs.open_append(&path).unwrap();
        let error = file.write_all_flush(&[b'x'; 64]).unwrap_err();
        assert_eq!(error.sent, 10);
        assert_eq!(error.error.os_code, Some(libc::EFBIG));
        assert_eq!(error.error.py_message, "[Errno 27] File too large");
        file.close().unwrap();
        assert_eq!(std::fs::metadata(path).unwrap().len(), 10);
        return;
    }

    let dir = tempfile::tempdir().unwrap();
    let path = dir.path().join("partial-write.wal");
    let output = Command::new(std::env::current_exe().unwrap())
        .args([
            "--exact",
            "posix_rlimit_fsize_partial_write_reports_sent",
            "--nocapture",
        ])
        .env("SW_PAL_FSIZE_CHILD", "1")
        .env("SW_PAL_FSIZE_PATH", &path)
        .stdin(std::process::Stdio::null())
        .output()
        .unwrap();
    assert!(
        output.status.success(),
        "子行程失敗：{:?}\nstdout={}\nstderr={}",
        output.status,
        String::from_utf8_lossy(&output.stdout),
        String::from_utf8_lossy(&output.stderr)
    );
    assert_eq!(std::fs::read(path).unwrap(), vec![b'x'; 10]);
}

#[cfg(unix)]
#[test]
fn posix_dir_writable_respects_real_permissions() {
    use std::os::unix::fs::PermissionsExt;

    // root 的 access(W_OK|X_OK) 會略過一般 mode bits，該身分下無法證明拒絕語意。
    if unsafe { libc::geteuid() } == 0 {
        eprintln!("證據不足：root 的 access(W_OK|X_OK) 不受目錄 mode bits 限制");
        return;
    }

    let dir = tempfile::tempdir().unwrap();
    let fs = sw_pal::native().wal_fs;
    assert!(fs.dir_writable(dir.path()));
    let mut permissions = std::fs::metadata(dir.path()).unwrap().permissions();
    permissions.set_mode(0o500);
    std::fs::set_permissions(dir.path(), permissions).unwrap();
    let writable = fs.dir_writable(dir.path());
    let mut restore = std::fs::metadata(dir.path()).unwrap().permissions();
    restore.set_mode(0o700);
    std::fs::set_permissions(dir.path(), restore).unwrap();
    assert!(!writable);
}

#[cfg(windows)]
mod windows_tests {
    use std::ffi::OsStr;
    use std::io::{BufRead, BufReader, Write};
    use std::os::windows::ffi::OsStrExt;
    use std::process::Command;

    use sw_pal::wal_fs::NativeWalFs;
    use sw_pal::{FileIdentity, WalFs};
    use windows_sys::Win32::Foundation::{CloseHandle, GetLastError, HANDLE, INVALID_HANDLE_VALUE};
    use windows_sys::Win32::Storage::FileSystem::{
        CreateFileW, FileDispositionInfo, FileDispositionInfoEx, FileIdInfo, GetFileAttributesW,
        GetFileInformationByHandleEx, SetFileAttributesW, SetFileInformationByHandle, DELETE,
        FILE_ATTRIBUTE_NORMAL, FILE_ATTRIBUTE_READONLY, FILE_DISPOSITION_FLAG_DELETE,
        FILE_DISPOSITION_FLAG_POSIX_SEMANTICS, FILE_DISPOSITION_INFO, FILE_DISPOSITION_INFO_EX,
        FILE_ID_INFO, FILE_READ_ATTRIBUTES, FILE_SHARE_DELETE, FILE_SHARE_READ, FILE_SHARE_WRITE,
        OPEN_EXISTING,
    };

    fn wide(value: &OsStr) -> Vec<u16> {
        value.encode_wide().chain(std::iter::once(0)).collect()
    }

    fn open_read_attributes(path: &std::path::Path, access: u32) -> HANDLE {
        let path = wide(path.as_os_str());
        // 安全性：路徑是 NUL 結尾 UTF-16；回傳的有效 handle 由呼叫端唯一關閉。
        unsafe {
            CreateFileW(
                path.as_ptr(),
                access,
                FILE_SHARE_READ | FILE_SHARE_WRITE | FILE_SHARE_DELETE,
                std::ptr::null(),
                OPEN_EXISTING,
                FILE_ATTRIBUTE_NORMAL,
                std::ptr::null_mut(),
            )
        }
    }

    fn file_id(path: &std::path::Path) -> FileIdentity {
        let handle = open_read_attributes(path, FILE_READ_ATTRIBUTES);
        assert_ne!(handle, INVALID_HANDLE_VALUE);
        let mut info = FILE_ID_INFO::default();
        // 安全性：輸出緩衝區大小與 FILE_ID_INFO 相符，handle 在查詢期間有效。
        let ok = unsafe {
            GetFileInformationByHandleEx(
                handle,
                FileIdInfo,
                (&mut info as *mut FILE_ID_INFO).cast(),
                std::mem::size_of::<FILE_ID_INFO>() as u32,
            )
        };
        if ok == 0 {
            let error = unsafe { GetLastError() };
            // 安全性：此測試擁有成功開啟的 handle。
            unsafe { CloseHandle(handle) };
            panic!("FileIdInfo 查詢失敗：{error}");
        }
        // 安全性：此測試擁有成功開啟的 handle。
        assert_ne!(unsafe { CloseHandle(handle) }, 0);
        FileIdentity(
            info.VolumeSerialNumber as u128,
            u128::from_le_bytes(info.FileId.Identifier),
        )
    }

    fn run_cmd(args: &[&str]) -> std::process::Output {
        Command::new("cmd.exe")
            .args(["/d", "/c"])
            .args(args)
            .output()
            .unwrap()
    }

    struct ExternalReader {
        child: Option<std::process::Child>,
    }

    impl ExternalReader {
        fn hold(path: &std::path::Path) -> Self {
            // Windows Python open() 不分享刪除，會讓計畫中的 cmd del 失敗；此 reader 明確採用 R/W/D share。
            let child = Command::new("py")
                .args([
                    "-3.12",
                    "-c",
                    "import ctypes, sys; from ctypes import wintypes; k=ctypes.WinDLL('kernel32', use_last_error=True); create=k.CreateFileW; create.argtypes=[wintypes.LPCWSTR,wintypes.DWORD,wintypes.DWORD,wintypes.LPVOID,wintypes.DWORD,wintypes.DWORD,wintypes.HANDLE]; create.restype=wintypes.HANDLE; h=create(sys.argv[1],0x80000000,7,None,3,0x80,None); assert h != wintypes.HANDLE(-1).value, ctypes.WinError(ctypes.get_last_error()); print('ready',flush=True); sys.stdin.buffer.read(1); assert k.CloseHandle(h), ctypes.WinError(ctypes.get_last_error())",
                ])
                .arg(path)
                .stdin(std::process::Stdio::piped())
                .stdout(std::process::Stdio::piped())
                .stderr(std::process::Stdio::piped())
                .spawn()
                .expect("無法啟動外部 Python reader");
            let mut reader = Self { child: Some(child) };
            let stdout = reader
                .child
                .as_mut()
                .expect("Python reader 子行程已結束")
                .stdout
                .take()
                .expect("Python stdout 未連接");
            let mut stdout = BufReader::new(stdout);
            let mut ready = String::new();
            stdout
                .read_line(&mut ready)
                .expect("讀取 Python reader 握手失敗");
            assert_eq!(
                ready.trim_end_matches(['\r', '\n']),
                "ready",
                "Python reader 未成功開啟檔案"
            );
            drop(stdout);
            reader
        }

        fn release_and_wait(&mut self) {
            let Some(mut child) = self.child.take() else {
                return;
            };
            child
                .stdin
                .take()
                .expect("Python stdin 未連接")
                .write_all(b"x")
                .expect("釋放 Python reader 失敗");
            let output = child.wait_with_output().expect("等待 Python reader 失敗");
            assert!(
                output.status.success(),
                "Python reader 子行程失敗：{output:?}"
            );
        }
    }

    impl Drop for ExternalReader {
        fn drop(&mut self) {
            if let Some(mut child) = self.child.take() {
                // 安全性：測試失敗路徑也終止並回收此測試啟動的子行程。
                let _ = child.kill();
                let _ = child.wait();
            }
        }
    }

    #[test]
    fn windows_share_flags_allow_external_delete_and_rename() {
        let dir = tempfile::tempdir().unwrap();
        let fs = NativeWalFs;
        let delete_path = dir.path().join("delete.wal");
        let mut writer = fs.open_append(&delete_path).unwrap();
        writer.write_all_flush(b"delete\n").unwrap();
        let deleted = run_cmd(&["del", "/f", "/q", delete_path.to_str().unwrap()]);
        assert!(deleted.status.success(), "cmd del 失敗：{deleted:?}");
        writer.close().unwrap();

        let rename_path = dir.path().join("rename.wal");
        let renamed_path = dir.path().join("renamed.wal");
        let mut writer = fs.open_append(&rename_path).unwrap();
        writer.write_all_flush(b"rename\n").unwrap();
        let renamed = run_cmd(&[
            "ren",
            rename_path.to_str().unwrap(),
            renamed_path.file_name().unwrap().to_str().unwrap(),
        ]);
        assert!(renamed.status.success(), "cmd ren 失敗：{renamed:?}");
        writer.close().unwrap();
        assert_eq!(std::fs::read(renamed_path).unwrap(), b"rename\n");
    }

    #[test]
    fn windows_classic_delete_pending_blocks_recreate_until_last_handle_closes() {
        let dir = tempfile::tempdir().unwrap();
        let path = dir.path().join("pending.wal");
        let fs = NativeWalFs;
        let mut writer = fs.open_append(&path).unwrap();
        writer.write_all_flush(b"held\n").unwrap();
        let mut reader = ExternalReader::hold(&path);
        let delete_handle = open_read_attributes(&path, DELETE | FILE_READ_ATTRIBUTES);
        assert_ne!(delete_handle, INVALID_HANDLE_VALUE);
        let disposition = FILE_DISPOSITION_INFO { DeleteFile: true };
        // 安全性：FILE_DISPOSITION_INFO 大小、handle 與緩衝區均符合 API 契約。
        let ok = unsafe {
            SetFileInformationByHandle(
                delete_handle,
                FileDispositionInfo,
                (&disposition as *const FILE_DISPOSITION_INFO).cast(),
                std::mem::size_of::<FILE_DISPOSITION_INFO>() as u32,
            )
        };
        if ok == 0 {
            let error = unsafe { GetLastError() };
            // 安全性：此測試擁有成功開啟的刪除 handle。
            unsafe { CloseHandle(delete_handle) };
            panic!("classic DeletePending 設定失敗：{error}");
        }
        // 安全性：此測試擁有成功開啟的刪除 handle。
        assert_ne!(unsafe { CloseHandle(delete_handle) }, 0);

        assert!(
            path.exists(),
            "classic DeletePending 應保留名稱直到最後一個 handle 關閉"
        );
        assert!(writer.unlinked().unwrap());
        assert_eq!(fs.path_identity(&path).unwrap(), Some(FileIdentity(0, 0)));
        let reopen_error = fs
            .open_append(&path)
            .err()
            .expect("classic DeletePending 期間不得重建同名 WAL");
        assert_eq!(reopen_error.kind, sw_pal::PalErrorKind::AccessDenied);

        writer.close().unwrap();
        let still_blocked = fs
            .open_append(&path)
            .err()
            .expect("外部 reader 尚未關閉時仍不得重建同名 WAL");
        assert_eq!(still_blocked.kind, sw_pal::PalErrorKind::AccessDenied);
        reader.release_and_wait();
        let recreated = fs.open_append(&path).unwrap();
        recreated.close().unwrap();
    }

    #[test]
    fn windows_dir_writable_ignores_readonly_directory_attribute() {
        let dir = tempfile::tempdir().unwrap();
        let fs = NativeWalFs;
        let path = wide(dir.path().as_os_str());
        // 安全性：路徑 NUL 結尾；GetFileAttributesW 僅查詢目錄屬性。
        let original = unsafe { GetFileAttributesW(path.as_ptr()) };
        assert_ne!(original, u32::MAX);
        // 安全性：僅為測試暫設唯讀屬性，Drop guard 會還原原始值。
        struct AttributeGuard {
            path: Vec<u16>,
            original: u32,
        }
        impl Drop for AttributeGuard {
            fn drop(&mut self) {
                // 安全性：路徑仍由 TempDir 持有，設定值取自原始屬性快照。
                unsafe { SetFileAttributesW(self.path.as_ptr(), self.original) };
            }
        }
        let _restore = AttributeGuard {
            path: path.clone(),
            original,
        };
        // 安全性：唯讀旗標不阻止 Windows 目錄建立子檔，亦不改動其他屬性。
        assert_ne!(
            unsafe { SetFileAttributesW(path.as_ptr(), original | FILE_ATTRIBUTE_READONLY) },
            0
        );
        assert!(fs.dir_writable(dir.path()));
        let child = dir.path().join("writable-under-readonly-directory");
        std::fs::write(child, b"ok").unwrap();
    }

    #[test]
    fn windows_posix_semantics_delete() {
        let dir = tempfile::tempdir().unwrap();
        let path = dir.path().join("posix-delete.wal");
        let fs = NativeWalFs;
        let mut writer = fs.open_append(&path).unwrap();
        writer.write_all_flush(b"delete\n").unwrap();
        let handle = open_read_attributes(&path, DELETE | FILE_READ_ATTRIBUTES);
        assert_ne!(handle, INVALID_HANDLE_VALUE);
        let info = FILE_DISPOSITION_INFO_EX {
            Flags: FILE_DISPOSITION_FLAG_DELETE | FILE_DISPOSITION_FLAG_POSIX_SEMANTICS,
        };
        // 安全性：FILE_DISPOSITION_INFO_EX 大小、handle 與緩衝區均符合 API 契約。
        let ok = unsafe {
            SetFileInformationByHandle(
                handle,
                FileDispositionInfoEx,
                (&info as *const FILE_DISPOSITION_INFO_EX).cast(),
                std::mem::size_of::<FILE_DISPOSITION_INFO_EX>() as u32,
            )
        };
        if ok == 0 {
            let error = unsafe { GetLastError() };
            // 安全性：此測試擁有成功開啟的 handle。
            unsafe { CloseHandle(handle) };
            panic!("POSIX 語意刪除失敗：{error}");
        }
        // 安全性：此測試擁有成功開啟的 handle。
        assert_ne!(unsafe { CloseHandle(handle) }, 0);
        assert_eq!(fs.path_identity(&path).unwrap(), None);
        let replacement = fs.open_append(&path).unwrap();
        replacement.close().unwrap();
        writer.close().unwrap();
    }

    #[test]
    fn windows_identity_uses_fileid128() {
        let dir = tempfile::tempdir().unwrap();
        let path = dir.path().join("identity.wal");
        let fs = NativeWalFs;
        let writer = fs.open_append(&path).unwrap();
        assert_eq!(writer.identity().unwrap(), file_id(&path));
        writer.close().unwrap();
    }
}
