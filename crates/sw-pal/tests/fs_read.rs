//! `FsRead` 基本語意（P3-T9）：對照 `open()`／`os.path.exists`／`os.path.isdir`／`os.listdir` 的可觀察結果。
use sw_pal::native_client;
use sw_pal::traits::PalErrorKind;

#[test]
fn read_returns_file_bytes_unchanged() {
    let d = tempfile::tempdir().unwrap();
    let p = d.path().join("blob.bin");
    let data: Vec<u8> = vec![0, 1, 2, 0xff, b'\n', 0x80];
    std::fs::write(&p, &data).unwrap();
    assert_eq!(native_client().fs.read(&p).unwrap(), data);
}

#[test]
fn read_missing_file_is_not_found() {
    let d = tempfile::tempdir().unwrap();
    let e = native_client()
        .fs
        .read(&d.path().join("missing.json"))
        .err()
        .expect("不存在的檔案應失敗");
    assert_eq!(e.kind, PalErrorKind::NotFound);
}

#[test]
fn exists_and_is_dir_follow_os_path_semantics() {
    let d = tempfile::tempdir().unwrap();
    let f = d.path().join("f.txt");
    std::fs::write(&f, b"x").unwrap();
    let fs = native_client().fs;
    assert!(fs.exists(&f));
    assert!(!fs.is_dir(&f));
    assert!(fs.exists(d.path()));
    assert!(fs.is_dir(d.path()));
    let missing = d.path().join("missing");
    assert!(!fs.exists(&missing));
    assert!(!fs.is_dir(&missing));
}

#[cfg(unix)]
#[test]
fn exists_follows_symlinks_like_os_path_exists() {
    let d = tempfile::tempdir().unwrap();
    let link = d.path().join("dangling");
    std::os::unix::fs::symlink(d.path().join("nowhere"), &link).unwrap();
    // os.path.exists 跟隨 symlink：懸空連結視為不存在。
    assert!(!native_client().fs.exists(&link));
}

#[test]
fn list_dir_returns_entry_names() {
    let d = tempfile::tempdir().unwrap();
    std::fs::write(d.path().join("a.json"), b"{}").unwrap();
    std::fs::create_dir(d.path().join("sub")).unwrap();
    let mut names = native_client().fs.list_dir(d.path()).unwrap();
    names.sort();
    assert_eq!(names, vec!["a.json".to_string(), "sub".to_string()]);
}
