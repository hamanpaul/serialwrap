//! P8 Task 8：SessionCore FLASHING 仲裁（probe 授權、Matched、HolderGone、SC-32）。
//!
//! 對照 `docs/rust-refactor/plans/09-slice24-transfer-flash.md` Task 8「狀態轉移」與 Python 基線
//! `session_manager.py` 的 `collect_flash_candidates_and_mark`／`enter_flashing`／`exit_flashing`／`_flash_busy_locked`、
//! `service.py` `_BridgeProbe.probe` 的 `set_flash_mode(True)`。
//!
//! 安全不變量：
//! - probe 窗口（`FlashMode::Probe(id)`）只放行帶同一 `probe_id` 的 `FlashProbe` TX；FLASHING 只放行 `Flash` TX
//!   （`Fence::admit`）；human console 輸入在兩者期間一律丟棄（`OwnershipState.flash_mode` → `DropFlash`）。
//! - Probe→Matched→HolderGone 是同一 bridge handle 上的模式切換：不產生 `OpenBridge`／`BeginDrain`、`conn_epoch` 不變。
//! - 所有 flash 結束路徑（未匹配釋放、Matched 後 HolderGone、I/O error、停止）無條件清 Fence 與 Ownership 的
//!   `flash_mode`；只解除同一 probe（舊 probe 的遲到事件忽略）。
//! - SC-32（Rust-only）：Probe／FLASHING 期間 `UartIoError` 照常進 Two-Phase Drain（→ RECOVERING）；`DeviceDetached`
//!   延後到 flash 結束後再 reconcile（等價 Python `_detach_by_id` 對 flash busy 的略過，#83 RACE-2）。
//! - P8 Task 9（編排者裁決 2026-11-08）：flash-critical 而 probe 窗口尚未開始時的 `DeviceDetached` 與 Python 相同直接略過。

use super::*;

impl SessionCore {
    /// Probe 或 FLASHING 期間（fence 的 `flash_mode` 非 Off）。
    pub(super) fn flash_active(&self) -> bool {
        self.deps.fence.with(|f| f.flash_mode != FlashMode::Off)
    }

    /// destructive 操作（release／clear／bind／recover）的 flash 臨界判定（Python `_flash_busy_locked`）：FLASHING、
    /// probe 窗口，或 manager 已標 flash-critical（`collect_flash_candidates_and_mark` 之後、probe 之前）。
    pub(super) fn flash_busy(&self) -> bool {
        self.state == SessionState::Flashing
            || self
                .deps
                .fence
                .with(|f| f.flash_mode != FlashMode::Off || f.flash_critical)
    }

    /// `FLASHING_BUSY` 回應（Python `clear_session`／`release_device`／`bind_session` 鍵序：ok／error_code／selector／session）。
    pub(super) fn flash_busy_reply(&self, now: Mono) -> PyDict {
        pydict! {
            "ok" => false,
            "error_code" => "FLASHING_BUSY",
            "selector" => self.profile.com.clone(),
            "session" => self.public(now, &self.public_ctx())
        }
    }

    /// `FlashSyncProbeStart` 的授權判定（09 計畫 Task 8 的拒絕順序）；通過時回授權的 epoch。
    fn flash_probe_verdict(&self) -> Result<u64, FlashReject> {
        if self.profile.template.command_capable() {
            return Err(FlashReject::CommandCapable);
        }
        if !matches!(self.state, SessionState::Ready | SessionState::Attached) {
            return Err(FlashReject::BadState(self.state));
        }
        if self.released_by.is_some() || self.release_persist_pending || self.pending_release.is_some() {
            return Err(FlashReject::Released);
        }
        if self.recovering {
            return Err(FlashReject::Recovering);
        }
        if self.drain_in_progress() {
            return Err(FlashReject::DrainInProgress);
        }
        if self.fg_slot_busy() {
            return Err(FlashReject::ForegroundBusy);
        }
        if self.flash_active() {
            return Err(FlashReject::FlashAlreadyActive);
        }
        match (self.generations.conn_epoch, self.deps.fence.with(|f| f.active_epoch)) {
            (Some(epoch), Some(active)) if epoch == active => Ok(epoch),
            _ => Err(FlashReject::BadState(self.state)),
        }
    }

    /// `FlashSyncProbeStart{probe_id}`：授權後於同一次 `Fence::with` 進 `Probe(probe_id)`（其他 class 的 TX 自此
    /// `NotSubmitted`），並開 human 輸入的 flash gate；回覆在 fence 更新之後送出。
    pub(super) fn flash_probe_start(
        &mut self,
        probe_id: ProbeId,
        reply: FlashReply<Result<ProbeGrant, FlashReject>>,
        _now: Mono,
    ) -> Vec<Effect> {
        let result = self.flash_probe_verdict().map(|epoch| {
            self.deps.fence.with(|f| f.flash_mode = FlashMode::Probe(probe_id));
            self.flash_probe_id = Some(probe_id);
            self.human.ownership.flash_mode = true;
            ProbeGrant { epoch, probe_id }
        });
        let _ = reply.try_send(result);
        Vec::new()
    }

    /// `FlashProbeReleased{probe_id}`：只解除同一 probe 的窗口；已 Matched（FLASHING）者只由 `FlashHolderGone` 結束。
    pub(super) fn flash_probe_released(&mut self, probe_id: ProbeId, now: Mono) -> Vec<Effect> {
        if self.flash_probe_id != Some(probe_id) {
            return Vec::new();
        }
        let flashing = self.deps.fence.with(|f| {
            if f.flash_mode == FlashMode::Flashing {
                return true;
            }
            if f.flash_mode == FlashMode::Probe(probe_id) {
                f.flash_mode = FlashMode::Off;
            }
            false
        });
        if flashing {
            return Vec::new();
        }
        self.flash_probe_id = None;
        self.human.ownership.flash_mode = false;
        self.flash_reconcile_detach(now)
    }

    /// `FlashSyncMatched{probe_id}`（Python `enter_flashing`）：同 handle 切到 FLASHING——不 OpenBridge、不 drain、
    /// `conn_epoch` 不變；BC-02 經 `invalidate_human_input(Flash)` 使舊 `input_epoch` 的 deferred 全部失效。
    pub(super) fn flash_sync_matched(
        &mut self,
        probe_id: ProbeId,
        reply: FlashReply<Result<(), FlashReject>>,
        now: Mono,
    ) -> Vec<Effect> {
        let same_probe = self.flash_probe_id == Some(probe_id)
            && self.deps.fence.with(|f| f.flash_mode == FlashMode::Probe(probe_id));
        if !same_probe {
            let _ = reply.try_send(Err(FlashReject::ProbeMismatch));
            return Vec::new();
        }
        if !matches!(self.state, SessionState::Ready | SessionState::Attached) {
            let _ = reply.try_send(Err(FlashReject::BadState(self.state)));
            return Vec::new();
        }
        self.flash_prev_state = Some(self.state);
        self.set_state(SessionState::Flashing, now);
        self.deps.fence.with(|f| f.flash_mode = FlashMode::Flashing);
        let mut fx = Effects(Vec::new());
        self.invalidate_human_input(InvalidateReason::Flash, &mut fx);
        let _ = reply.try_send(Ok(()));
        fx.0
    }

    /// `FlashHolderGone{probe_id}`（Python `exit_flashing`）：舊 probe 的遲到事件只回覆；同一 probe 無條件清兩處
    /// `flash_mode`，FLASHING 時還原 `flash_prev_state`（無則依 bridge 有無取 READY／DETACHED），最後補做延後的 detach。
    pub(super) fn flash_holder_gone(&mut self, probe_id: ProbeId, reply: FlashReply<()>, now: Mono) -> Vec<Effect> {
        if self.flash_probe_id != Some(probe_id) {
            let _ = reply.try_send(());
            return Vec::new();
        }
        self.deps.fence.with(|f| f.flash_mode = FlashMode::Off);
        self.human.ownership.flash_mode = false;
        self.flash_probe_id = None;
        if self.state == SessionState::Flashing {
            let next = self.flash_prev_state.take().unwrap_or(if self.generations.conn_epoch.is_some() {
                SessionState::Ready
            } else {
                SessionState::Detached
            });
            self.set_state(next, now);
        } else {
            self.flash_prev_state = None;
        }
        let _ = reply.try_send(());
        self.flash_reconcile_detach(now)
    }

    /// 停止（drain Phase 1 起點，含 SC-32 的 I/O error 與 shutdown）也是 flash 結束路徑：無條件清 Fence 與
    /// Ownership 的 `flash_mode` 與 `flash_prev_state`；`flash_probe_id` 保留到 coordinator 的 `FlashHolderGone`／
    /// `FlashProbeReleased` 收尾（之後對狀態為 no-op）。
    pub(super) fn flash_end_for_stop(&mut self) {
        self.deps.fence.with(|f| f.flash_mode = FlashMode::Off);
        self.human.ownership.flash_mode = false;
        self.flash_prev_state = None;
    }

    /// 編排者裁決（2026-11-08，rulings/s2.md「P8 T8 的兩項 SPEC-GAP」）：manager 已標 flash-critical
    /// （`collect_flash_candidates_and_mark` 之後）而 probe 窗口尚未開始時的 `DeviceDetached` 對齊 Python 略過——
    /// `session_manager.py:4443-4455` `_detach_by_id` 以 `_flash_busy_locked`（含 `_flash_critical_coms`）過濾 target，
    /// 不 drain、不延後（flash 結束後也不補做；Python 只在下一次裝置變化時才 reconcile）。只對候選狀態（READY／
    /// ATTACHED）生效：manager「先標記後取快照」期間短暫被標記的非候選 session 不受影響（Python 只標記候選）。
    pub(super) fn flash_critical_skips_detach(&self) -> bool {
        matches!(self.state, SessionState::Ready | SessionState::Attached)
            && self
                .deps
                .fence
                .with(|f| f.flash_critical && f.flash_mode == FlashMode::Off)
    }

    /// SC-32：Probe／FLASHING 期間的 `DeviceDetached` 延後（只記原因，不 drain、不改狀態）；回 true 表示已延後。
    pub(super) fn flash_defer_detach(&mut self, reason: DetachReason) -> bool {
        if !self.flash_active()
            || matches!(self.state, SessionState::Released | SessionState::Detached)
        {
            return false;
        }
        self.pending_detach = Some(reason);
        true
    }

    /// flash 結束後補做延後的 detach（等價 Python 下一個 device watcher reconcile）：裝置仍不在 → 依原因 detach；
    /// 已回到另一實體路徑 → 改綁（DEVICE_REBOUND_REQUIRED）；已回到同一路徑 → 無差異、不動作。
    pub(super) fn flash_reconcile_detach(&mut self, now: Mono) -> Vec<Effect> {
        let Some(pending) = self.pending_detach.take() else {
            return Vec::new();
        };
        let reason = if !self.device_present {
            pending
        } else if self.known_real_path.as_deref() != self.attached_real_path.as_deref() {
            DetachReason::Rebound
        } else {
            return Vec::new();
        };
        let by_id = self.profile.device_by_id.clone();
        self.device_detached(&by_id, reason, now)
    }

    /// KEEP UIO-12.3-d：probe 窗口（state 仍 READY／ATTACHED）的 `interactive_send` 被 flash gate 靜默丟棄——Python
    /// `send_bytes` 的 flash pre-check 回 not_submitted 不 raise，`interactive_send` 照回 ok＋bytes 並 touch lease；
    /// 零 TX、不記 WAL、不佔前景槽。flash 若在提交點才生效則仍由 TX 結果映射為 SESSION_BUSY（`revoked:flashing`）。
    pub(super) fn flash_silent_interactive_send(&mut self, interactive_id: &str, bytes: usize, now: Mono) -> Option<PyDict> {
        if !self.flash_active() {
            return None;
        }
        if let Some(lease) = self.lease.as_mut().filter(|l| l.interactive_id == interactive_id) {
            lease.touch(now);
        }
        Some(pydict! { "ok" => true, "interactive_id" => interactive_id, "bytes" => py_int(bytes as i128) })
    }
}
