//! state.json 遷移（P4-T10 承接）。

use std::path::Path;
use sw_pal::fs_ops::FsOps;

pub fn migrate_legacy_state(fs: &dyn FsOps, legacy: &Path, dest: &Path) -> bool {
    todo!("not implemented: P4-T10: migrate_legacy_state")
}
