//! `FsRead` 的 OSError 基準對照（`oserror.<os>.tsv`）與 errno 名稱基準（`errno_names.<os>.tsv`）（P3-T9）。
//!
//! 比對 `strerror` 與 `os_code`；檔名部分由 `sw-proto::PyOsError` 組裝，於 P3-T12 驗證。
mod common;

use sw_pal::traits::PalError;
use sw_pal::native_client;

fn file_case(id: &str) -> common::OsErrCase {
    common::oserror_cases(common::oserror_file())
        .into_iter()
        .find(|c| c.id == id)
        .unwrap_or_else(|| panic!("golden 缺少 case {id}"))
}

fn assert_fs_error(id: &str, e: &PalError) {
    let g = file_case(id);
    assert_eq!(
        (e.strerror.as_str(), e.os_code),
        (g.strerror.as_str(), g.errno),
        "{id}"
    );
}

#[test]
fn file_enoent_matches_golden() {
    let d = tempfile::tempdir().unwrap();
    let e = native_client()
        .fs
        .read(&d.path().join("missing.json"))
        .err()
        .expect("不存在的檔案應失敗");
    assert_fs_error("file_enoent", &e);
}

#[test]
fn file_isdir_matches_golden() {
    let d = tempfile::tempdir().unwrap();
    let e = native_client()
        .fs
        .read(d.path())
        .err()
        .expect("讀取目錄應失敗");
    assert_fs_error("file_isdir", &e);
}

#[cfg(unix)]
#[test]
fn file_eacces_matches_golden() {
    use std::os::unix::fs::PermissionsExt;
    let d = tempfile::tempdir().unwrap();
    let p = d.path().join("locked.json");
    std::fs::write(&p, b"{}").unwrap();
    std::fs::set_permissions(&p, std::fs::Permissions::from_mode(0o000)).unwrap();
    let e = native_client().fs.read(&p).err().expect("mode 000 應失敗");
    assert_fs_error("file_eacces", &e);
}

/// Windows 的 EACCES 來源為 sharing violation：以 share mode 0 佔住檔案（與 `gen_oserror.py` 相同手法）。
#[cfg(windows)]
#[test]
fn file_eacces_matches_golden() {
    use std::os::windows::fs::OpenOptionsExt;
    let d = tempfile::tempdir().unwrap();
    let p = d.path().join("locked.json");
    std::fs::write(&p, b"{}").unwrap();
    let _hold = std::fs::OpenOptions::new()
        .read(true)
        .share_mode(0)
        .open(&p)
        .unwrap();
    let e = native_client().fs.read(&p).err().expect("共用衝突應失敗");
    assert_fs_error("file_eacces", &e);
}

#[test]
fn listdir_enoent_matches_golden() {
    let d = tempfile::tempdir().unwrap();
    let e = native_client()
        .fs
        .list_dir(&d.path().join("missing-dir"))
        .err()
        .expect("不存在的目錄應失敗");
    assert_fs_error("listdir_enoent", &e);
}

#[test]
fn errno_names_match_golden() {
    let env = native_client().env;
    let mut fails = Vec::new();
    for r in common::golden_rows(common::errno_names_file()) {
        let code: i32 = r.input[0].parse().expect("golden errno 必須為整數");
        let want = r.expected[0].as_str();
        let got = env.errno_name(code);
        if got != Some(want) {
            fails.push(format!("{}: errno {code} got {got:?} want {want:?}", r.id));
        }
    }
    assert!(fails.is_empty(), "{}", fails.join("\n"));
}
