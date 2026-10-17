//! POSIX `ProcessTunnel` 實作（P4-T5 承接）。本檔為紅燈骨架，主體皆 `unimplemented!("P4-T5")`。

use std::path::Path;
use std::time::Duration;

use super::{ProcessTunnel, TunnelProc};
use crate::traits::PalResult;

#[derive(Clone, Copy, Debug, Default)]
pub struct NativeProcessTunnel;

impl ProcessTunnel for NativeProcessTunnel {
    fn spawn_detached(&self, _argv: &[String], _log: &Path) -> PalResult<TunnelProc> {
        unimplemented!("P4-T5")
    }

    fn alive(&self, _p: &TunnelProc) -> bool {
        unimplemented!("P4-T5")
    }

    fn terminate(&self, _p: &TunnelProc, _grace: Duration) -> PalResult<()> {
        unimplemented!("P4-T5")
    }

    fn stderr_tail(&self, _log: &Path, _max: usize) -> String {
        unimplemented!("P4-T5")
    }

    fn supports_control_master(&self) -> bool {
        unimplemented!("P4-T5")
    }
}
