//! P7 Task 6（08-slice23-login-events.md）：`FsOps` 寫入側增補（`open_append`、`create_truncate`、`file_size`、
//! `remove_file_if_exists`）；兩平台同一份。錯誤訊息照 Python `str(OSError)`（含 `: '<path>'`）。
//! Windows 原生執行：`scripts/rust/win-test.sh --package sw-pal --test fs_ops_p7`（LOCAL-WINDOWS-TODO）。
use std::fs;

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
    assert_eq!(
        err.py_message,
        format!("[Errno 2] No such file or directory: '{}'", p.display())
    );
}

#[test]
fn create_truncate_text_uses_platform_newline() {
    // 審查 76de0cf_96b28dd c58e8d4-2：Python `open(p, "w")` 文字模式在 Windows 把 `\n` 寫成 `\r\n`。
    use sw_pal::fs_ops::{text_mode_bytes, TEXT_NEWLINE};
    let fs = sw_pal::native().fs;
    let d = tempfile::tempdir().unwrap();
    let p = d.path().join("r.json.tmp");
    std::fs::write(&p, b"stale-longer-content").unwrap();
    fs.create_truncate_text(&p, "{\n  \"a\": 1\n}").unwrap();
    let want = format!("{{{TEXT_NEWLINE}  \"a\": 1{TEXT_NEWLINE}}}");
    assert_eq!(std::fs::read(&p).unwrap(), want.as_bytes());
    assert_eq!(
        text_mode_bytes("x\ny"),
        format!("x{TEXT_NEWLINE}y").into_bytes()
    );
    #[cfg(windows)]
    assert_eq!(TEXT_NEWLINE, "\r\n");
    #[cfg(not(windows))]
    assert_eq!(TEXT_NEWLINE, "\n");
}
