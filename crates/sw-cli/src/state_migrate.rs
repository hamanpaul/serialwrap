//! state.json 遷移（P4-T10 承接）。

use std::path::Path;
use sw_pal::fs_ops::FsOps;

pub fn migrate_legacy_state(fs: &dyn FsOps, legacy: &Path, dest: &Path) -> bool {
    if !fs.exists(legacy) || fs.exists(dest) {
        return false;
    }
    if let Some(parent) = dest.parent() {
        if fs.mkdirs(parent).is_err() {
            return false;
        }
    }
    fs.copy_preserving(legacy, dest).is_ok()
}
