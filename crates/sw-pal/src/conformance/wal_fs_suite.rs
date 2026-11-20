//! WalFs conformance（arch §9.1、計畫 Task 6）：同一套斷言跑在 fakes 與原生檔案系統上。

use std::fs::{self, File};
#[cfg(windows)]
use std::io;
use std::path::{Path, PathBuf};
use std::sync::atomic::{AtomicU64, Ordering};
use std::sync::Arc;

use crate::fakes::wal_fs::FakeWalFs;
use crate::traits::WalFs;

/// 外部行為者（另一行程或 std::fs）：用來在 WAL 寫入期間模擬 unlink、覆寫與常駐 reader。
pub trait ExternalFsOps {
    fn unlink(&self, p: &Path);
    fn replace_with(&self, p: &Path, bytes: &[u8]);
    fn hold_reader(&self, p: &Path) -> Box<dyn Send>;
}

/// 原生外部行為者（標準檔案 API 實作）。
pub struct NativeExternalFs;

impl ExternalFsOps for NativeExternalFs {
    fn unlink(&self, path: &Path) {
        fs::remove_file(path).expect("外部刪除 WAL 檔失敗");
    }

    fn replace_with(&self, path: &Path, bytes: &[u8]) {
        replace_path(path, bytes)
            .unwrap_or_else(|error| panic!("外部取代 WAL 檔失敗：path={path:?}，error={error:?}"));
    }

    fn hold_reader(&self, path: &Path) -> Box<dyn Send> {
        Box::new(File::open(path).expect("外部開啟 WAL reader 失敗"))
    }
}

/// fake 外部行為者（包住 `FakeWalFs`）。
pub struct FakeExternalFs(pub Arc<FakeWalFs>);

impl ExternalFsOps for FakeExternalFs {
    fn unlink(&self, path: &Path) {
        self.0.external_unlink(path);
    }

    fn replace_with(&self, path: &Path, bytes: &[u8]) {
        self.0.external_replace(path, bytes);
    }

    fn hold_reader(&self, path: &Path) -> Box<dyn Send> {
        Box::new(self.0.hold_external_reader(path))
    }
}

/// WalFs 共用斷言（Task 6）。
pub fn wal_fs_suite(fs: Arc<dyn WalFs>, dir: &Path, external: &dyn ExternalFsOps) {
    fs.ensure_dir(dir).unwrap();

    let append_path = dir.join("append.wal");
    let mut append_file = fs.open_append(&append_path).unwrap();
    append_file.write_all_flush(b"a\n").unwrap();
    append_file.write_all_flush(b"b\n").unwrap();
    assert_eq!(fs.read_all(&append_path).unwrap(), Some(b"a\nb\n".to_vec()));

    let identity_path = dir.join("identity.wal");
    let identity_file = fs.open_append(&identity_path).unwrap();
    assert_eq!(
        identity_file.identity().unwrap(),
        fs.path_identity(&identity_path).unwrap().unwrap()
    );
    identity_file.close().unwrap();

    let unlink_path = dir.join("unlink.wal");
    let unlink_file = fs.open_append(&unlink_path).unwrap();
    external.unlink(&unlink_path);
    let handle_reports_unlinked = unlink_file.unlinked().unwrap();
    let path_disappeared = fs.path_identity(&unlink_path).unwrap().is_none();
    #[cfg(unix)]
    assert!(handle_reports_unlinked);
    #[cfg(unix)]
    assert!(path_disappeared);
    assert!(handle_reports_unlinked || path_disappeared);
    unlink_file.close().unwrap();

    let replace_path = dir.join("replace.wal");
    let replace_file = fs.open_append(&replace_path).unwrap();
    let original_identity = replace_file.identity().unwrap();
    external.replace_with(&replace_path, b"x");
    let replacement_identity = fs.path_identity(&replace_path).unwrap().unwrap();
    assert_ne!(replacement_identity, original_identity);
    replace_file.close().unwrap();

    let tail_path = dir.join("tail.wal");
    external.replace_with(&tail_path, b"partial");
    assert!(fs.tail_needs_termination(&tail_path).unwrap());
    external.replace_with(&tail_path, b"partial\n");
    assert!(!fs.tail_needs_termination(&tail_path).unwrap());
    external.replace_with(&tail_path, b"");
    assert!(!fs.tail_needs_termination(&tail_path).unwrap());

    let rename_path = dir.join("rename-open.wal");
    let archive_path = rename_path.with_extension("ndjson.20261015-000000");
    let mut renamed_file = fs.open_append(&rename_path).unwrap();
    renamed_file.write_all_flush(b"before\n").unwrap();
    assert_eq!(
        fs.rename_noreplace_or_suffix(&rename_path, &archive_path)
            .unwrap(),
        archive_path
    );
    renamed_file.write_all_flush(b"after\n").unwrap();
    assert_eq!(
        fs.read_all(&archive_path).unwrap(),
        Some(b"before\nafter\n".to_vec())
    );
    assert_eq!(fs.read_all(&rename_path).unwrap(), None);
    renamed_file.close().unwrap();

    let collision_source = dir.join("collision-source.wal");
    let collision_base = dir.join("archive.wal");
    external.replace_with(&collision_base, b"existing");
    external.replace_with(&collision_source, b"first");
    let first_archive = fs
        .rename_noreplace_or_suffix(&collision_source, &collision_base)
        .unwrap();
    assert_eq!(first_archive, suffixed_path(&collision_base, 1));
    external.replace_with(&collision_source, b"second");
    let second_archive = fs
        .rename_noreplace_or_suffix(&collision_source, &collision_base)
        .unwrap();
    assert_eq!(second_archive, suffixed_path(&collision_base, 2));
    assert_eq!(
        fs.read_all(&collision_base).unwrap(),
        Some(b"existing".to_vec())
    );
    assert_eq!(
        fs.read_all(&first_archive).unwrap(),
        Some(b"first".to_vec())
    );
    assert_eq!(
        fs.read_all(&second_archive).unwrap(),
        Some(b"second".to_vec())
    );

    let size_path = dir.join("size.wal");
    assert_eq!(fs.size(&size_path).unwrap(), None);
    let mut size_file = fs.open_append(&size_path).unwrap();
    size_file.write_all_flush(b"abc").unwrap();
    assert_eq!(fs.size(&size_path).unwrap(), Some(3));
    size_file.sync().unwrap();
    fs.sync_dir(dir).unwrap();
    size_file.close().unwrap();

    let read_path = dir.join("read-all.wal");
    assert_eq!(fs.read_all(&read_path).unwrap(), None);
    let mut read_file = fs.open_append(&read_path).unwrap();
    read_file.write_all_flush(b"a\n").unwrap();
    assert_eq!(fs.read_all(&read_path).unwrap(), Some(b"a\n".to_vec()));
    read_file.close().unwrap();
    append_file.close().unwrap();

    assert!(fs.dir_writable(dir));
    assert!(!fs.dir_writable(&dir.join("missing-dir")));
    let not_a_directory = dir.join("not-a-directory");
    external.replace_with(&not_a_directory, b"file");
    assert!(!fs.dir_writable(&not_a_directory));
}

fn suffixed_path(base: &Path, suffix: usize) -> PathBuf {
    let mut value = base.as_os_str().to_os_string();
    value.push(format!(".{suffix}"));
    PathBuf::from(value)
}

#[cfg(unix)]
fn replace_path(path: &Path, bytes: &[u8]) -> std::io::Result<()> {
    static NEXT_TEMP: AtomicU64 = AtomicU64::new(0);
    let mut temp_name = path.as_os_str().to_os_string();
    temp_name.push(format!(
        ".replace.{}.{}",
        std::process::id(),
        NEXT_TEMP.fetch_add(1, Ordering::Relaxed)
    ));
    let temp_path = PathBuf::from(temp_name);
    fs::write(&temp_path, bytes)?;
    match fs::rename(&temp_path, path) {
        Ok(()) => Ok(()),
        Err(error) => {
            let _ = fs::remove_file(temp_path);
            Err(error)
        }
    }
}

#[cfg(windows)]
fn replace_path(path: &Path, bytes: &[u8]) -> std::io::Result<()> {
    use std::os::windows::ffi::OsStrExt;

    use windows_sys::Win32::Foundation::{CloseHandle, GetLastError, HANDLE, INVALID_HANDLE_VALUE};
    use windows_sys::Win32::Storage::FileSystem::{
        CreateFileW, FileRenameInfoEx, SetFileInformationByHandle, FILE_RENAME_INFO,
        FILE_RENAME_INFO_0, FILE_SHARE_DELETE, FILE_SHARE_READ, FILE_SHARE_WRITE, OPEN_EXISTING,
    };

    static NEXT_TEMP: AtomicU64 = AtomicU64::new(0);
    let mut temp_name = path.as_os_str().to_os_string();
    temp_name.push(format!(
        ".replace.{}.{}",
        std::process::id(),
        NEXT_TEMP.fetch_add(1, Ordering::Relaxed)
    ));
    let temp_path = PathBuf::from(temp_name);
    fs::write(&temp_path, bytes)?;
    let target_exists = match fs::symlink_metadata(path) {
        Ok(_) => true,
        Err(error) if error.kind() == io::ErrorKind::NotFound => false,
        Err(error) => {
            let _ = fs::remove_file(&temp_path);
            return Err(error);
        }
    };
    if !target_exists {
        // 安全性：目的路徑不存在時，標準 rename 原子建立檔案，亦涵蓋無副檔名檔名。
        return match fs::rename(&temp_path, path) {
            Ok(()) => Ok(()),
            Err(error) => {
                let _ = fs::remove_file(&temp_path);
                Err(error)
            }
        };
    }
    let from: Vec<u16> = temp_path
        .as_os_str()
        .encode_wide()
        .chain(std::iter::once(0))
        .collect();
    let to: Vec<u16> = path
        .as_os_str()
        .encode_wide()
        .chain(std::iter::once(0))
        .collect();
    // 安全性：路徑為 NUL 結尾 UTF-16；此 handle 唯一持有 source 的 DELETE 存取權。
    let handle = unsafe {
        CreateFileW(
            from.as_ptr(),
            windows_sys::Win32::Storage::FileSystem::DELETE,
            FILE_SHARE_READ | FILE_SHARE_WRITE | FILE_SHARE_DELETE,
            std::ptr::null(),
            OPEN_EXISTING,
            windows_sys::Win32::Storage::FileSystem::FILE_ATTRIBUTE_NORMAL,
            std::ptr::null_mut(),
        )
    };
    if handle == INVALID_HANDLE_VALUE {
        let code = unsafe { GetLastError() } as i32;
        let _ = fs::remove_file(&temp_path);
        return Err(io::Error::other(format!(
            "CreateFileW 開啟取代暫存檔 {temp_path:?} 失敗：Win32 {code}"
        )));
    }

    // FileRenameInfoEx 的 flags 同時指定「允許取代」與 POSIX 語意，讓舊 WAL handle
    // 繼續指向原檔，而路徑原子地指向外部建立的新檔。
    const FILE_RENAME_FLAG_REPLACE_IF_EXISTS: u32 = 0x1;
    const FILE_RENAME_FLAG_POSIX_SEMANTICS: u32 = 0x2;
    let flags = FILE_RENAME_FLAG_REPLACE_IF_EXISTS | FILE_RENAME_FLAG_POSIX_SEMANTICS;
    let name_len = (to.len() - 1) * std::mem::size_of::<u16>();
    let name_offset = std::mem::offset_of!(FILE_RENAME_INFO, FileName);
    let bytes_len = name_offset + name_len;
    let mut storage = vec![0u64; bytes_len.div_ceil(std::mem::size_of::<u64>())];
    let info = storage.as_mut_ptr().cast::<FILE_RENAME_INFO>();
    // 安全性：storage 已按 u64 對齊並足以容納標頭與完整 UTF-16 名稱；name_len 以 bytes 計。
    unsafe {
        std::ptr::write(
            info,
            FILE_RENAME_INFO {
                Anonymous: FILE_RENAME_INFO_0 { Flags: flags },
                RootDirectory: std::ptr::null_mut::<std::ffi::c_void>() as HANDLE,
                FileNameLength: name_len as u32,
                FileName: [0],
            },
        );
        std::ptr::copy_nonoverlapping(
            to.as_ptr(),
            std::ptr::addr_of_mut!((*info).FileName).cast::<u16>(),
            to.len() - 1,
        );
    }
    // 安全性：handle 由本函式持有，輸入緩衝區大小符合 FILE_RENAME_INFO_EX。
    let renamed = unsafe {
        SetFileInformationByHandle(handle, FileRenameInfoEx, info.cast(), bytes_len as u32)
    };
    let error = if renamed == 0 {
        Some(unsafe { GetLastError() } as i32)
    } else {
        None
    };
    // 安全性：此函式擁有成功開啟的 source handle。
    unsafe { CloseHandle(handle) };
    if let Some(code) = error {
        let _ = fs::remove_file(&temp_path);
        return Err(io::Error::other(format!(
            "FileRenameInfoEx 取代 {path:?} 時來源為 {temp_path:?}，Win32 {code}"
        )));
    }

    Ok(())
}
