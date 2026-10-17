//! 假 `ProcessCtl`，記錄委派呼叫。

use crate::traits::{PalError, PalErrorKind, ProcessCtl};
use std::ffi::OsString;
use std::sync::Mutex;

#[derive(Debug, Default)]
pub struct RecordingProcess {
    pub calls: Mutex<Vec<(Vec<OsString>, Vec<(String, String)>)>>,
}

impl ProcessCtl for RecordingProcess {
    fn exec_or_wait(&self, argv: &[OsString], env_add: &[(String, String)]) -> PalError {
        self.calls
            .lock()
            .unwrap_or_else(|e| e.into_inner())
            .push((argv.to_vec(), env_add.to_vec()));
        PalError::plain(
            PalErrorKind::Unsupported,
            "recording process does not execute",
        )
    }
}
