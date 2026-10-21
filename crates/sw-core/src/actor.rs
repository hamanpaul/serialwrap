//! P5-T11：`SessionActor` 執行緒（紅燈階段骨架）。
//!
//! 每個 session 一條 thread（名稱 `serialwrap-session-<COM>`），以 `Mailbox::recv_next` 取事件、
//! `catch_unwind(core.on_event(..))` 隔離 panic，效果交 `EffectExecutor` 執行；1 s `Tick`。
//! 函式本體為 `s21_todo!`，由 Task 11 實作者補上。

use std::sync::Arc;
use std::thread::JoinHandle;
use std::time::Duration;

use crossbeam_channel::Sender;
use sw_pal::traits::Clock;
use sw_proto::pycompat::PyErr;
use sw_proto::pyjson::PyDict;

use crate::mailbox::{ControlEvent, DataEvent, Mailbox};
use crate::session_core::{Effect, SessionCore, SessionRequest};

/// actor 的外部控制代碼（RPC 分派與測試共用）。
pub struct ActorHandle {
    pub control: Sender<ControlEvent>,
    pub data: Sender<DataEvent>,
    pub join: JoinHandle<()>,
}

impl ActorHandle {
    /// 送出請求並等待回覆；逾時回 `PyErr("internal timeout")`；actor 已停止回 `PyErr("session actor stopped")`。
    pub fn request(&self, _req: SessionRequest, _timeout: Duration) -> Result<PyDict, PyErr> {
        s21_todo!("T11", "ActorHandle::request")
    }
}

/// 效果執行器（executor 在 actor thread 外的 IO worker 側實作；測試以替身實作）。
pub trait EffectExecutor: Send {
    fn execute(&mut self, e: Effect, tx: &Sender<ControlEvent>);
}

/// 啟動 actor thread。
pub fn spawn_actor(
    _core: SessionCore,
    _mailbox: Mailbox,
    _exec: Box<dyn EffectExecutor>,
    _clock: Arc<dyn Clock>,
) -> ActorHandle {
    s21_todo!("T11", "spawn_actor")
}
