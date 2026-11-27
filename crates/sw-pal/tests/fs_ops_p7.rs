//! P7 Task 6（08-slice23-login-events.md）：`FsOps` 寫入側增補（`open_append`、`create_truncate`、`file_size`、
//! `remove_file_if_exists`）；兩平台同一份。錯誤訊息照 Python `str(OSError)`（含 `: '<path>'`）。
//! Windows 原生執行：`scripts/rust/win-test.sh --package sw-pal --test fs_ops_p7`（LOCAL-WINDOWS-TODO）。
use std::fs;

#[cfg(unix)]
#[test]
fn atomic_replace_preserves_owner_group_and_mode() {
    use std::os::unix::fs::{MetadataExt, PermissionsExt};

    let tmp = tempfile::tempdir().unwrap();
    let path = tmp.path().join("managed-profile.yaml");
    fs::write(&path, b"before\n").unwrap();
    fs::set_permissions(&path, fs::Permissions::from_mode(0o640)).unwrap();
    let before = fs::metadata(&path).unwrap();
    let expected = (before.uid(), before.gid(), before.mode() & 0o777);

    sw_pal::native()
        .fs
        .atomic_replace_preserving_metadata(&path, b"after\n")
        .unwrap();

    let after = fs::metadata(&path).unwrap();
    assert_eq!(fs::read(&path).unwrap(), b"after\n");
    assert_eq!(
        (after.uid(), after.gid(), after.mode() & 0o777),
        expected,
        "原子遷移須保留既有 profile owner、group 與 mode"
    );
}

#[cfg(target_os = "linux")]
fn p290_access_acl(named_uid: u32) -> Vec<u8> {
    // Linux POSIX ACL xattr 第 2 版後，欄位依序為 USER_OBJ、具名 USER、GROUP_OBJ、MASK、OTHER。
    let mut acl = 2u32.to_ne_bytes().to_vec();
    for (tag, perm, id) in [
        (0x01u16, 0o6u16, u32::MAX),
        (0x02u16, 0o4u16, named_uid),
        (0x04u16, 0o0u16, u32::MAX),
        (0x10u16, 0o4u16, u32::MAX),
        (0x20u16, 0o0u16, u32::MAX),
    ] {
        acl.extend_from_slice(&tag.to_ne_bytes());
        acl.extend_from_slice(&perm.to_ne_bytes());
        acl.extend_from_slice(&id.to_ne_bytes());
    }
    acl
}

#[cfg(target_os = "linux")]
fn set_p290_access_acl(path: &std::path::Path, acl: &[u8]) {
    use std::os::unix::ffi::OsStrExt;
    let path = std::ffi::CString::new(path.as_os_str().as_bytes()).unwrap();
    let name = b"system.posix_acl_access\0";
    // SAFETY: 路徑與名稱緩衝區皆以 NUL 結尾且仍有效；ACL 位元組切片亦在呼叫期間有效。
    let rc = unsafe {
        libc::setxattr(
            path.as_ptr(),
            name.as_ptr().cast(),
            acl.as_ptr().cast(),
            acl.len(),
            0,
        )
    };
    assert_eq!(
        rc,
        0,
        "設定測試 POSIX ACL 失敗：{}",
        std::io::Error::last_os_error()
    );
}

#[cfg(target_os = "linux")]
fn read_p290_access_acl(path: &std::path::Path) -> Vec<u8> {
    use std::os::unix::ffi::OsStrExt;
    let path = std::ffi::CString::new(path.as_os_str().as_bytes()).unwrap();
    let name = b"system.posix_acl_access\0";
    // SAFETY: 路徑與名稱緩衝區皆以 NUL 結尾且仍有效；value 為 null 時只查詢 ACL 大小。
    let size =
        unsafe { libc::getxattr(path.as_ptr(), name.as_ptr().cast(), std::ptr::null_mut(), 0) };
    assert!(
        size >= 0,
        "讀取測試 POSIX ACL 失敗：{}",
        std::io::Error::last_os_error()
    );
    let mut acl = vec![0u8; size as usize];
    // SAFETY: `acl` 的容量依前一個 getxattr 呼叫回傳的大小配置。
    let read = unsafe {
        libc::getxattr(
            path.as_ptr(),
            name.as_ptr().cast(),
            acl.as_mut_ptr().cast(),
            acl.len(),
        )
    };
    assert_eq!(
        read,
        size,
        "重讀測試 POSIX ACL 失敗：{}",
        std::io::Error::last_os_error()
    );
    acl
}

#[cfg(target_os = "linux")]
#[test]
fn atomic_replace_preserves_named_access_acl_for_other_user() {
    use std::os::unix::fs::{MetadataExt, PermissionsExt};

    const NAMED_UID: u32 = 65_534;
    let tmp = tempfile::tempdir().unwrap();
    fs::set_permissions(tmp.path(), fs::Permissions::from_mode(0o755)).unwrap();
    let path = tmp.path().join("system-profile.yaml");
    fs::write(&path, b"before\n").unwrap();
    fs::set_permissions(&path, fs::Permissions::from_mode(0o600)).unwrap();
    set_p290_access_acl(&path, &p290_access_acl(NAMED_UID));
    let expected_acl = read_p290_access_acl(&path);
    let before = fs::metadata(&path).unwrap();

    sw_pal::native()
        .fs
        .atomic_replace_preserving_metadata(&path, b"after\n")
        .unwrap();

    let after = fs::metadata(&path).unwrap();
    assert_eq!(fs::read(&path).unwrap(), b"after\n");
    assert_eq!(
        read_p290_access_acl(&path),
        expected_acl,
        "原子替換須原樣保留 access ACL"
    );
    assert_eq!(
        (after.uid(), after.gid(), after.mode() & 0o7777),
        (before.uid(), before.gid(), before.mode() & 0o7777)
    );

    if unsafe { libc::geteuid() } == 0 {
        use std::os::unix::process::CommandExt;
        let output = std::process::Command::new("cat")
            .arg(&path)
            .uid(NAMED_UID)
            .gid(NAMED_UID)
            .output()
            .unwrap();
        assert!(
            output.status.success(),
            "ACL 授權的非 owner 無法讀取替換後檔案"
        );
        assert_eq!(output.stdout, b"after\n");
    } else {
        eprintln!("目前 euid 非 root；此輪僅完成 ACL xattr 比對，需另以 root 執行此節點驗證 uid {NAMED_UID} 讀取");
    }
}

#[test]
fn create_truncate_overwrites_stale_tmp() {
    let tmp = tempfile::tempdir().unwrap();
    let p = tmp.path().join("counter.json.tmp");
    fs::write(&p, b"stale-longer-content").unwrap();
    let fsops = sw_pal::native().fs;
    fsops.create_truncate(&p, b"{}").unwrap();
    assert_eq!(
        fs::read(&p).unwrap(),
        b"{}",
        "殘留 tmp 必須被覆寫，不得續寫"
    );
}

#[test]
fn mkdirs_exist_ok() {
    let tmp = tempfile::tempdir().unwrap();
    let d = tmp.path().join("a").join("b");
    let fsops = sw_pal::native().fs;
    fsops.mkdirs(&d).unwrap();
    fsops.mkdirs(&d).unwrap();
    assert!(d.is_dir());
}

#[test]
fn remove_file_if_exists_missing_is_false() {
    let tmp = tempfile::tempdir().unwrap();
    let p = tmp.path().join("x.log");
    let fsops = sw_pal::native().fs;
    assert!(!fsops.remove_file_if_exists(&p).unwrap());
    fs::write(&p, b"1").unwrap();
    assert!(fsops.remove_file_if_exists(&p).unwrap());
    assert!(!p.exists());
}

#[test]
fn file_size_none_when_missing() {
    let tmp = tempfile::tempdir().unwrap();
    let p = tmp.path().join("y.log");
    let fsops = sw_pal::native().fs;
    assert_eq!(fsops.file_size(&p).unwrap(), None);
    fs::write(&p, b"12345").unwrap();
    assert_eq!(fsops.file_size(&p).unwrap(), Some(5));
}

#[cfg(target_os = "linux")]
#[test]
fn file_io_timeout_preserves_errno_strerror_and_full_filename() {
    use std::io;
    use std::path::Path;

    struct FakeFs;
    impl FakeFs {
        fn open(&self, _path: &Path) -> io::Result<()> {
            Err(io::Error::from_raw_os_error(110)) // Linux ETIMEDOUT，完全不碰 host filesystem。
        }
    }

    let path = Path::new("/tmp/serialwrap-timeout.bin");
    let mapped = sw_pal::fs_ops::map_file_error(FakeFs.open(path).unwrap_err());
    assert_eq!(mapped.os_code, Some(110));
    assert_eq!(mapped.strerror, "Connection timed out");
    assert_eq!(
        format!("{}: '{}'", mapped.py_message, path.display()),
        "[Errno 110] Connection timed out: '/tmp/serialwrap-timeout.bin'",
    );
}

#[test]
fn open_append_appends_and_creates() {
    let tmp = tempfile::tempdir().unwrap();
    let p = tmp.path().join("COM0_261027-000000.log");
    let fsops = sw_pal::native().fs;
    {
        let mut f = fsops.open_append(&p).unwrap();
        f.write_all_flush(b"a\n").unwrap();
        f.sync().unwrap();
    }
    let mut f = fsops.open_append(&p).unwrap();
    f.write_all_flush(b"b\n").unwrap();
    drop(f);
    assert_eq!(fs::read(&p).unwrap(), b"a\nb\n");
}

#[test]
fn open_append_error_message_has_quoted_path() {
    // CPython 3.12：open('<tmp>/missing/x.log', 'ab') → FileNotFoundError；
    // str(exc) == "[Errno 2] No such file or directory: '<tmp>/missing/x.log'"。
    let tmp = tempfile::tempdir().unwrap();
    let p = tmp.path().join("missing").join("x.log");
    let fsops = sw_pal::native().fs;
    let err = match fsops.open_append(&p) {
        Ok(_) => panic!("父目錄不存在應失敗"),
        Err(e) => e,
    };
    assert_eq!(err.kind, sw_pal::PalErrorKind::NotFound);
    // Python session_manager 將 open 的 OSError 以 str(exc) 回覆（origin/main sw_core/session_manager.py:8158-8160）。
    let python_path = p.display().to_string().replace('\\', "\\\\");
    assert_eq!(
        err.py_message,
        format!("[Errno 2] No such file or directory: '{python_path}'")
    );
}

#[test]
fn create_truncate_text_uses_platform_newline() {
    // 審查 76de0cf_96b28dd c58e8d4-2：Python `open(p, "w")` 文字模式在 Windows 把 `\n` 寫成 `\r\n`。
    use sw_pal::fs_ops::{text_mode_bytes, TEXT_NEWLINE, TEXT_NEWLINE_STR};
    let fs = sw_pal::native().fs;
    let d = tempfile::tempdir().unwrap();
    let p = d.path().join("r.json.tmp");
    std::fs::write(&p, b"stale-longer-content").unwrap();
    fs.create_truncate_text(&p, "{\n  \"a\": 1\n}").unwrap();
    let want = format!("{{{TEXT_NEWLINE_STR}  \"a\": 1{TEXT_NEWLINE_STR}}}");
    assert_eq!(std::fs::read(&p).unwrap(), want.as_bytes());
    assert_eq!(
        text_mode_bytes("x\ny"),
        format!("x{TEXT_NEWLINE_STR}y").into_bytes()
    );
    #[cfg(windows)]
    assert_eq!(TEXT_NEWLINE_STR, "\r\n");
    #[cfg(not(windows))]
    assert_eq!(TEXT_NEWLINE_STR, "\n");
    // merge P5 47c7b25：位元組形與字串形同值。
    assert_eq!(TEXT_NEWLINE, TEXT_NEWLINE_STR.as_bytes());
}
