//! 記憶體內唯讀檔案系統。

use crate::traits::{FsRead, PalError, PalErrorKind, PalResult};
use std::collections::{BTreeMap, BTreeSet};
use std::path::{Path, PathBuf};
use std::sync::Mutex;

#[derive(Debug, Default)]
pub struct MemFs {
    pub files: Mutex<BTreeMap<PathBuf, Result<Vec<u8>, PalError>>>,
    pub dirs: Mutex<BTreeSet<PathBuf>>,
}

impl FsRead for MemFs {
    fn read(&self, path: &Path) -> PalResult<Vec<u8>> {
        self.files
            .lock()
            .unwrap_or_else(|e| e.into_inner())
            .get(path)
            .cloned()
            .unwrap_or_else(|| {
                Err(PalError::plain(
                    PalErrorKind::NotFound,
                    "No such file or directory",
                ))
            })
    }

    fn exists(&self, path: &Path) -> bool {
        self.files
            .lock()
            .unwrap_or_else(|e| e.into_inner())
            .contains_key(path)
            || self
                .dirs
                .lock()
                .unwrap_or_else(|e| e.into_inner())
                .contains(path)
    }

    fn is_dir(&self, path: &Path) -> bool {
        self.dirs
            .lock()
            .unwrap_or_else(|e| e.into_inner())
            .contains(path)
    }

    fn list_dir(&self, path: &Path) -> PalResult<Vec<String>> {
        let files = self.files.lock().unwrap_or_else(|e| e.into_inner());
        if let Some(Err(error)) = files.get(path) {
            return Err(error.clone());
        }
        let dirs = self.dirs.lock().unwrap_or_else(|e| e.into_inner());
        if !dirs.contains(path) {
            return Err(PalError::plain(
                PalErrorKind::NotFound,
                "No such file or directory",
            ));
        }
        let mut names = BTreeSet::new();
        for child in files.keys().chain(dirs.iter()) {
            if child.parent() == Some(path) {
                if let Some(name) = child.file_name() {
                    names.insert(name.to_string_lossy().into_owned());
                }
            }
        }
        Ok(names.into_iter().collect())
    }
}
