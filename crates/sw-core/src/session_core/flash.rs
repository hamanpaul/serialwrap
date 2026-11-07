//! P8 Task 8：SessionCore FLASHING 仲裁（probe 授權、Matched、HolderGone、SC-32）。
//!
//! 對照 `docs/rust-refactor/plans/09-slice24-transfer-flash.md` Task 8「狀態轉移」與 Python 基線
//! `session_manager.py` 的 `collect_flash_candidates_and_mark`／`enter_flashing`／`exit_flashing`、
//! `service.py` `_BridgeProbe.probe` 的 `set_flash_mode(True)`。

use super::*;

impl SessionCore {
    /// `FlashSyncProbeStart{probe_id}`。
    pub(super) fn flash_probe_start(
        &mut self,
        _probe_id: ProbeId,
        _reply: FlashReply<Result<ProbeGrant, FlashReject>>,
        _now: Mono,
    ) -> Vec<Effect> {
        todo!("P8-T8")
    }

    /// `FlashProbeReleased{probe_id}`。
    pub(super) fn flash_probe_released(&mut self, _probe_id: ProbeId, _now: Mono) -> Vec<Effect> {
        todo!("P8-T8")
    }

    /// `FlashSyncMatched{probe_id}`。
    pub(super) fn flash_sync_matched(
        &mut self,
        _probe_id: ProbeId,
        _reply: FlashReply<Result<(), FlashReject>>,
        _now: Mono,
    ) -> Vec<Effect> {
        todo!("P8-T8")
    }

    /// `FlashHolderGone{probe_id}`。
    pub(super) fn flash_holder_gone(
        &mut self,
        _probe_id: ProbeId,
        _reply: FlashReply<()>,
        _now: Mono,
    ) -> Vec<Effect> {
        todo!("P8-T8")
    }
}
