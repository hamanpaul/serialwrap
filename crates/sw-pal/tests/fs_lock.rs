//! `FsOps` 承接測試（P4-T5）：跨行程檔案鎖、目錄連結回退、遞迴複製與刪除。
//!
//! 跨行程測試以本測試二進位重新啟動自身持鎖：子行程（`cross_process_lock_holder_helper`）只在
//! 環境變數 `SW_PAL_FS_LOCK_HOLDER_ROLE=holder` 存在時動作，其餘情況立即返回。
//! Windows 專用測試以 `#[cfg(windows)]` 逐項標註，由 `scripts/rust/win-test.sh --package sw-pal -- fs_lock` 原生執行。
use std::fs;
use std::path::PathBuf;
use std::process::{Command, Stdio};
use std::sync::mpsc;
use std::time::{Duration, Instant, SystemTime, UNIX_EPOCH};

use sw_pal::fs_ops::LinkKind;
use sw_pal::PalErrorKind;

const HOLDER_ROLE_ENV: &str = "SW_PAL_FS_LOCK_HOLDER_ROLE";
const HOLDER_DIR_ENV: &str = "SW_PAL_FS_LOCK_HOLDER_DIR";

/// 現在時間（UNIX 奈秒）；父子行程在同一台機器比對釋放時點。
fn unix_nanos() -> i128 {
    SystemTime::now()
        .duration_since(UNIX_EPOCH)
        .expect("系統時鐘早於 UNIX_EPOCH")
        .as_nanos() as i128
}

/// POSIX 取得 mode（僅保留權限位）。
#[cfg(unix)]
fn mode_of(p: &std::path::Path) -> u32 {
    use std::os::unix::fs::PermissionsExt;
    fs::metadata(p).unwrap().permissions().mode() & 0o7777
}

#[test]
fn file_lock_nonblocking_contention_wouldblock() {
    let tmp = tempfile::tempdir().unwrap();
    let path = tmp.path().join("registry.lock");
    let fsops = sw_pal::native().fs;
    let first = fsops
        .file_lock_exclusive(&path, false)
        .expect("第一次非阻塞取鎖應成功");
    match fsops.file_lock_exclusive(&path, false) {
        Err(error) => assert_eq!(
            error.kind,
            PalErrorKind::WouldBlock,
            "同一 path 的第二個非阻塞鎖應回 WouldBlock"
        ),
        Ok(_) => panic!("第一個鎖仍持有時，第二個非阻塞鎖不得成功"),
    }
    drop(first);
    assert!(
        fsops.file_lock_exclusive(&path, false).is_ok(),
        "第一個 guard drop 後應可再次取得鎖"
    );
}

#[test]
fn file_lock_cross_process() {
    let tmp = tempfile::tempdir().unwrap();
    let dir = tmp.path().to_path_buf();
    let lock_path = dir.join("shared.lock");
    let locked_mark = dir.join("locked");
    let released_mark = dir.join("released");

    let mut child = Command::new(std::env::current_exe().unwrap())
        .args([
            "--exact",
            "cross_process_lock_holder_helper",
            "--nocapture",
            "--test-threads=1",
        ])
        .env(HOLDER_ROLE_ENV, "holder")
        .env(HOLDER_DIR_ENV, &dir)
        .stdout(Stdio::null())
        .stderr(Stdio::piped())
        .spawn()
        .expect("無法啟動持鎖子行程");

    // 等待子行程宣告已持鎖；子行程若提前結束，帶出其 stderr 以便定位失敗原因。
    let deadline = Instant::now() + Duration::from_secs(10);
    while !locked_mark.exists() {
        if child.try_wait().unwrap().is_some() {
            let out = child.wait_with_output().unwrap();
            panic!("持鎖子行程提前結束：{}", String::from_utf8_lossy(&out.stderr));
        }
        if Instant::now() >= deadline {
            let _ = child.kill();
            panic!("子行程 10 s 內未宣告持鎖");
        }
        std::thread::sleep(Duration::from_millis(10));
    }

    // 持鎖期間，父行程的非阻塞取鎖必須回 WouldBlock。
    match sw_pal::native().fs.file_lock_exclusive(&lock_path, false) {
        Err(error) => assert_eq!(
            error.kind,
            PalErrorKind::WouldBlock,
            "子行程持鎖時非阻塞取鎖應回 WouldBlock"
        ),
        Ok(_) => panic!("子行程持鎖時父行程的非阻塞取鎖不得成功"),
    }

    // 阻塞版在另一執行緒等待，避免實作錯誤時測試永久卡住。
    let (tx, rx) = mpsc::channel::<Result<i128, String>>();
    let fsops = sw_pal::native().fs;
    let blocking_path = lock_path.clone();
    std::thread::spawn(move || {
        let outcome = match fsops.file_lock_exclusive(&blocking_path, true) {
            Ok(guard) => {
                let acquired = unix_nanos();
                drop(guard);
                Ok(acquired)
            }
            Err(error) => Err(format!("{error:?}")),
        };
        let _ = tx.send(outcome);
    });
    let acquired = rx
        .recv_timeout(Duration::from_secs(10))
        .expect("阻塞版在子行程釋放後 10 s 內仍未取得鎖")
        .expect("阻塞版取鎖失敗");

    // 阻塞鎖只能在子行程釋放後取得，且應於釋放後 1.5 s 內取得。
    let released_text = fs::read_to_string(&released_mark)
        .expect("阻塞鎖取得時子行程尚未寫入釋放時點（互斥失效）");
    let released: i128 = released_text.trim().parse().expect("釋放時點格式錯誤");
    assert!(acquired >= released, "阻塞鎖不得早於子行程釋放");
    assert!(
        acquired - released <= 1_500_000_000,
        "阻塞鎖應於子行程釋放後 1.5 s 內取得（實際 {} ns）",
        acquired - released
    );

    let status = child.wait().unwrap();
    assert!(status.success(), "持鎖子行程應正常結束");
}

/// 持鎖子行程本體（非獨立測試）：只在父行程設定 `SW_PAL_FS_LOCK_HOLDER_ROLE=holder` 時持鎖 1 s，否則立即返回。
#[test]
fn cross_process_lock_holder_helper() {
    let is_holder = std::env::var(HOLDER_ROLE_ENV)
        .map(|v| v == "holder")
        .unwrap_or(false);
    if !is_holder {
        return;
    }
    let dir = PathBuf::from(
        std::env::var(HOLDER_DIR_ENV).expect("缺少 SW_PAL_FS_LOCK_HOLDER_DIR"),
    );
    let guard = sw_pal::native()
        .fs
        .file_lock_exclusive(&dir.join("shared.lock"), true)
        .expect("持鎖子行程取鎖失敗");
    fs::write(dir.join("locked"), b"1").unwrap();
    std::thread::sleep(Duration::from_secs(1));
    // 先寫釋放時點再 drop，父行程的取得時點必定不早於此值。
    fs::write(dir.join("released"), unix_nanos().to_string()).unwrap();
    drop(guard);
}

#[test]
fn symlink_or_copy_dir_replaces_real_dir() {
    let tmp = tempfile::tempdir().unwrap();
    let target = tmp.path().join("payload");
    fs::create_dir_all(&target).unwrap();
    fs::write(target.join("new.txt"), b"new").unwrap();
    let link = tmp.path().join("link");
    fs::create_dir_all(&link).unwrap();
    fs::write(link.join("old.txt"), b"old").unwrap();

    let kind = sw_pal::native()
        .fs
        .symlink_or_copy_dir(&link, &target)
        .unwrap();
    #[cfg(unix)]
    {
        assert_eq!(kind, LinkKind::Symlink, "POSIX 應回 Symlink");
        assert!(fs::symlink_metadata(&link).unwrap().file_type().is_symlink());
    }
    #[cfg(windows)]
    {
        // Windows 能否建立 symlink 取決於開發者模式；兩種結果皆合法。
        assert!(
            matches!(kind, LinkKind::Symlink | LinkKind::Copied),
            "Windows 應回 Symlink 或 Copied"
        );
    }
    assert!(!link.join("old.txt").exists(), "舊的真目錄內容必須消失");
    assert_eq!(fs::read(link.join("new.txt")).unwrap(), b"new");
}

#[cfg(windows)]
#[test]
fn symlink_or_copy_dir_windows_fallback_copies() {
    // 前提：原生 Windows 且未啟用開發者模式（啟用時 symlink 會成功，不會走回退）。
    let tmp = tempfile::tempdir().unwrap();
    let target = tmp.path().join("payload");
    fs::create_dir_all(target.join("sub")).unwrap();
    fs::write(target.join("a.txt"), b"alpha").unwrap();
    fs::write(target.join("sub").join("b.txt"), b"beta").unwrap();
    let link = tmp.path().join("link");

    // 回退路徑：以 ERROR_PRIVILEGE_NOT_HELD（非開發者模式）取代實際建立，主機設定不影響判定。
    let kind = sw_pal::fs_ops::windows::symlink_or_copy_dir_privilege_not_held(&link, &target).unwrap();
    assert_eq!(kind, LinkKind::Copied, "非開發者模式的 Windows 應回退為遞迴複製");
    assert!(!fs::symlink_metadata(&link).unwrap().file_type().is_symlink(), "回退結果必須是真目錄");
    assert_eq!(fs::read(link.join("a.txt")).unwrap(), b"alpha");
    assert_eq!(fs::read(link.join("sub").join("b.txt")).unwrap(), b"beta");

    // 原生路徑：主機啟用開發者模式時建立 symlink，否則回退複製；兩者內容都必須可讀。
    let native_link = tmp.path().join("native-link");
    let kind = sw_pal::native()
        .fs
        .symlink_or_copy_dir(&native_link, &target)
        .unwrap();
    let is_symlink = fs::symlink_metadata(&native_link).unwrap().file_type().is_symlink();
    assert_eq!(is_symlink, kind == LinkKind::Symlink, "回報種類須與實際檔案型別一致");
    assert_eq!(fs::read(native_link.join("a.txt")).unwrap(), b"alpha");
    assert_eq!(fs::read(native_link.join("sub").join("b.txt")).unwrap(), b"beta");
}

#[test]
fn copy_tree_and_remove_tree() {
    let tmp = tempfile::tempdir().unwrap();
    let src = tmp.path().join("src");
    fs::create_dir_all(src.join("a").join("b")).unwrap();
    fs::write(src.join("top.txt"), b"top").unwrap();
    fs::write(src.join("a").join("b").join("nested.txt"), b"nested").unwrap();
    #[cfg(unix)]
    {
        use std::os::unix::fs::PermissionsExt;
        fs::set_permissions(src.join("top.txt"), fs::Permissions::from_mode(0o640)).unwrap();
        fs::set_permissions(
            src.join("a").join("b").join("nested.txt"),
            fs::Permissions::from_mode(0o600),
        )
        .unwrap();
    }

    let fsops = sw_pal::native().fs;
    let dst = tmp.path().join("dst");
    fsops.copy_tree(&src, &dst).unwrap();

    assert_eq!(fs::read(dst.join("top.txt")).unwrap(), b"top");
    assert_eq!(
        fs::read(dst.join("a").join("b").join("nested.txt")).unwrap(),
        b"nested"
    );
    #[cfg(unix)]
    {
        assert_eq!(
            mode_of(&dst.join("top.txt")),
            mode_of(&src.join("top.txt")),
            "複製後頂層檔案 mode 必須相同"
        );
        assert_eq!(
            mode_of(&dst.join("a").join("b").join("nested.txt")),
            mode_of(&src.join("a").join("b").join("nested.txt")),
            "複製後巢狀檔案 mode 必須相同"
        );
    }

    // remove_tree 不跟隨指向外部的 symlink：外部檔案必須仍在。
    let outside = tmp.path().join("outside");
    fs::create_dir_all(&outside).unwrap();
    fs::write(outside.join("keep.txt"), b"keep").unwrap();
    #[cfg(unix)]
    {
        std::os::unix::fs::symlink(&outside, dst.join("escape")).unwrap();
    }
    fsops.remove_tree(&dst).unwrap();
    assert!(!dst.exists(), "remove_tree 後目錄必須消失");
    assert_eq!(
        fs::read(outside.join("keep.txt")).unwrap(),
        b"keep",
        "remove_tree 不得跟隨外部 symlink 刪除外部檔案"
    );
}

#[cfg(unix)]
#[test]
fn atomic_write_uses_expected_modes_and_removes_failed_temp_files() {
    use std::os::unix::fs::PermissionsExt;

    let tmp = tempfile::tempdir().unwrap();
    let fsops = sw_pal::native().fs;
    let default_mode = tmp.path().join("default.json");
    fsops.atomic_write(&default_mode, b"default", None).unwrap();
    assert_eq!(mode_of(&default_mode), 0o600, "未指定 mode 時須限制為 owner-only");

    let explicit_mode = tmp.path().join("explicit.json");
    fsops
        .atomic_write(&explicit_mode, b"explicit", Some(0o644))
        .unwrap();
    assert_eq!(mode_of(&explicit_mode), 0o644, "明確 mode 必須套用到結果檔案");

    let readonly_destination = tmp.path().join("readonly-destination.json");
    fs::write(&readonly_destination, b"old").unwrap();
    fs::set_permissions(&readonly_destination, fs::Permissions::from_mode(0o400)).unwrap();
    fsops
        .atomic_write(&readonly_destination, b"new", None)
        .unwrap();
    assert_eq!(
        fs::read(&readonly_destination).unwrap(),
        b"new",
        "POSIX 原子取代應可覆寫唯讀目的檔"
    );

    let blocked = tmp.path().join("blocked.json");
    fs::create_dir(&blocked).unwrap();
    fs::write(blocked.join("keep"), b"keep").unwrap();
    assert!(fsops.atomic_write(&blocked, b"cannot replace", None).is_err());
    let leftovers: Vec<_> = fs::read_dir(tmp.path())
        .unwrap()
        .map(|entry| entry.unwrap().file_name().to_string_lossy().into_owned())
        .filter(|name| name.starts_with("blocked.json.tmp."))
        .collect();
    assert!(leftovers.is_empty(), "失敗後不得殘留原子寫入暫存檔：{leftovers:?}");
}

#[test]
fn write_mkdirs_replace_and_remove_file_operate_on_paths() {
    let tmp = tempfile::tempdir().unwrap();
    let fsops = sw_pal::native().fs;
    let parent = tmp.path().join("nested").join("directory");
    fsops.mkdirs(&parent).unwrap();
    assert!(parent.is_dir());

    let source = parent.join("source.txt");
    let destination = parent.join("destination.txt");
    fsops.write_file(&source, b"payload").unwrap();
    fs::write(&destination, b"previous").unwrap();
    fsops.replace(&source, &destination).unwrap();
    assert_eq!(fs::read(&destination).unwrap(), b"payload");
    fsops.remove_file(&destination).unwrap();
    assert!(!destination.exists());
}

#[test]
fn copy_preserving_copies_file_contents() {
    let tmp = tempfile::tempdir().unwrap();
    let source = tmp.path().join("source.txt");
    let destination = tmp.path().join("destination.txt");
    fs::write(&source, b"payload").unwrap();
    sw_pal::native()
        .fs
        .copy_preserving(&source, &destination)
        .unwrap();
    assert_eq!(fs::read(&destination).unwrap(), b"payload");
}

#[cfg(unix)]
#[test]
fn copy_preserving_and_copy_tree_accept_readonly_files() {
    use std::os::unix::fs::PermissionsExt;

    let tmp = tempfile::tempdir().unwrap();
    let fsops = sw_pal::native().fs;
    let readonly = tmp.path().join("readonly.txt");
    fs::write(&readonly, b"single").unwrap();
    fs::set_permissions(&readonly, fs::Permissions::from_mode(0o444)).unwrap();
    let copied = tmp.path().join("copied.txt");
    fsops.copy_preserving(&readonly, &copied).unwrap();
    assert_eq!(fs::read(&copied).unwrap(), b"single");
    assert_eq!(mode_of(&copied), 0o444);

    let source_tree = tmp.path().join("source-tree");
    fs::create_dir_all(source_tree.join("nested")).unwrap();
    let nested = source_tree.join("nested").join("readonly.sh");
    fs::write(&nested, b"#!/bin/sh\n").unwrap();
    fs::set_permissions(&nested, fs::Permissions::from_mode(0o555)).unwrap();
    let destination_tree = tmp.path().join("destination-tree");
    fsops.copy_tree(&source_tree, &destination_tree).unwrap();
    let copied_nested = destination_tree.join("nested").join("readonly.sh");
    assert_eq!(fs::read(&copied_nested).unwrap(), b"#!/bin/sh\n");
    assert_eq!(mode_of(&copied_nested), 0o555);
}

#[cfg(unix)]
#[test]
fn copy_tree_follows_source_file_symlinks_and_rejects_existing_destination() {
    use std::os::unix::fs::symlink;

    let tmp = tempfile::tempdir().unwrap();
    let source = tmp.path().join("source");
    fs::create_dir(&source).unwrap();
    let target = tmp.path().join("target.txt");
    fs::write(&target, b"target").unwrap();
    symlink(&target, source.join("alias.txt")).unwrap();

    let fsops = sw_pal::native().fs;
    let destination = tmp.path().join("destination");
    fsops.copy_tree(&source, &destination).unwrap();
    assert_eq!(fs::read(destination.join("alias.txt")).unwrap(), b"target");
    assert!(
        !fs::symlink_metadata(destination.join("alias.txt"))
            .unwrap()
            .file_type()
            .is_symlink(),
        "copy_tree 應依 shutil.copytree 預設跟隨來源 symlink"
    );

    let error = fsops.copy_tree(&source, &destination).unwrap_err();
    assert_eq!(error.kind, PalErrorKind::InvalidInput);

    let dangling_source = tmp.path().join("dangling-source");
    fs::create_dir(&dangling_source).unwrap();
    symlink(tmp.path().join("does-not-exist"), dangling_source.join("dangling.txt")).unwrap();
    let dangling_error = fsops
        .copy_tree(&dangling_source, &tmp.path().join("dangling-destination"))
        .unwrap_err();
    assert_eq!(dangling_error.kind, PalErrorKind::NotFound);
}

#[cfg(all(feature = "test-fakes", unix))]
#[test]
fn realfs_copy_tree_matches_native_destination_and_symlink_contract() {
    use std::os::unix::fs::symlink;
    use sw_pal::fakes::realfs::RealFs;
    use sw_pal::fs_ops::FsOps;

    let tmp = tempfile::tempdir().unwrap();
    let source = tmp.path().join("source");
    fs::create_dir(&source).unwrap();
    let target = tmp.path().join("target.txt");
    fs::write(&target, b"target").unwrap();
    symlink(&target, source.join("alias.txt")).unwrap();

    let destination = tmp.path().join("destination");
    RealFs.copy_tree(&source, &destination).unwrap();
    assert_eq!(fs::read(destination.join("alias.txt")).unwrap(), b"target");
    assert!(
        !fs::symlink_metadata(destination.join("alias.txt"))
            .unwrap()
            .file_type()
            .is_symlink()
    );
    assert_eq!(
        RealFs.copy_tree(&source, &destination).unwrap_err().kind,
        PalErrorKind::InvalidInput
    );
}

#[test]
fn symlink_or_copy_dir_creates_missing_link_parent() {
    let tmp = tempfile::tempdir().unwrap();
    let target = tmp.path().join("payload");
    fs::create_dir(&target).unwrap();
    fs::write(target.join("item.txt"), b"payload").unwrap();
    let link = tmp.path().join("missing").join("parents").join("skill");

    sw_pal::native()
        .fs
        .symlink_or_copy_dir(&link, &target)
        .unwrap();
    assert_eq!(fs::read(link.join("item.txt")).unwrap(), b"payload");
}

#[test]
fn read_link_set_mode_sync_dir_and_symlink_file_follow_contract() {
    let tmp = tempfile::tempdir().unwrap();
    let fsops = sw_pal::native().fs;
    let target = tmp.path().join("target.txt");
    fs::write(&target, b"target").unwrap();
    assert_eq!(fsops.read_link(&target).unwrap(), None);
    fsops.sync_dir(tmp.path()).unwrap();

    #[cfg(unix)]
    {
        let link = tmp.path().join("links").join("file.txt");
        fs::create_dir_all(link.parent().unwrap()).unwrap();
        fs::write(&link, b"replaced").unwrap();
        fsops.symlink_file(&link, &target).unwrap();
        assert_eq!(fsops.read_link(&link).unwrap().as_deref(), Some(target.as_path()));
        fsops.set_mode(&target, 0o400).unwrap();
        assert_eq!(mode_of(&target), 0o400);
    }

    #[cfg(windows)]
    {
        let error = fsops.symlink_file(&tmp.path().join("file-link"), &target).unwrap_err();
        assert_eq!(error.kind, PalErrorKind::Unsupported);
        fsops.set_mode(&target, 0o444).unwrap();
        assert!(fs::metadata(&target).unwrap().permissions().readonly());
    }
}

#[cfg(windows)]
fn create_dir_symlink_or_skip(target: &std::path::Path, link: &std::path::Path) -> bool {
    match std::os::windows::fs::symlink_dir(target, link) {
        Ok(()) => true,
        Err(error)
            if error.kind() == std::io::ErrorKind::PermissionDenied
                || error.raw_os_error() == Some(1314) =>
        {
            eprintln!("SKIP：此 Windows 環境不允許建立目錄 symlink：{error}");
            false
        }
        Err(error) => panic!("建立目錄 symlink 失敗：{error}"),
    }
}

#[cfg(windows)]
#[test]
fn windows_symlink_or_copy_dir_replaces_existing_directory_symlink() {
    let tmp = tempfile::tempdir().unwrap();
    let old_target = tmp.path().join("old-target");
    let new_target = tmp.path().join("new-target");
    fs::create_dir(&old_target).unwrap();
    fs::create_dir(&new_target).unwrap();
    fs::write(old_target.join("old.txt"), b"old").unwrap();
    fs::write(new_target.join("new.txt"), b"new").unwrap();
    let link = tmp.path().join("directory-link");
    if !create_dir_symlink_or_skip(&old_target, &link) {
        return;
    }

    let kind = sw_pal::native()
        .fs
        .symlink_or_copy_dir(&link, &new_target)
        .unwrap();
    assert_eq!(fs::read(old_target.join("old.txt")).unwrap(), b"old");
    assert_eq!(fs::read(link.join("new.txt")).unwrap(), b"new");
    if kind == LinkKind::Symlink {
        assert_eq!(fs::read_link(&link).unwrap(), new_target);
    }
}

#[cfg(windows)]
#[test]
fn windows_remove_tree_removes_directory_symlink_without_target() {
    let tmp = tempfile::tempdir().unwrap();
    let target = tmp.path().join("target");
    fs::create_dir(&target).unwrap();
    fs::write(target.join("keep.txt"), b"keep").unwrap();
    let link = tmp.path().join("directory-link");
    if !create_dir_symlink_or_skip(&target, &link) {
        return;
    }

    sw_pal::native().fs.remove_tree(&link).unwrap();
    assert!(fs::symlink_metadata(&link).is_err(), "目錄 symlink 本身必須被移除");
    assert_eq!(fs::read(target.join("keep.txt")).unwrap(), b"keep");
}

#[cfg(windows)]
#[test]
fn windows_atomic_write_retries_while_destination_denies_delete_sharing() {
    use std::os::windows::fs::OpenOptionsExt;

    let tmp = tempfile::tempdir().unwrap();
    let target = tmp.path().join("state.json");
    fs::write(&target, b"old").unwrap();
    // 不分享 FILE_SHARE_DELETE，讓第一次 MoveFileExW 在 handle 關閉前無法取代目的檔。
    let held = std::fs::OpenOptions::new()
        .read(true)
        .write(true)
        .share_mode(0x0000_0001 | 0x0000_0002)
        .open(&target)
        .unwrap();
    let release = std::thread::spawn(move || {
        std::thread::sleep(Duration::from_millis(150));
        drop(held);
    });

    sw_pal::native()
        .fs
        .atomic_write(&target, b"new", None)
        .unwrap();
    release.join().unwrap();
    assert_eq!(fs::read(&target).unwrap(), b"new");
}
