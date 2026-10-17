//! `windows-task`（Task XML／schtasks；SC-24）。
//!
//! P4-T1 只交付建構子；行為由 P4-T3 實作。
#![allow(unused_variables)]

use std::sync::Arc;

use crate::fs_ops::FsOps;
use crate::service_ctl::ServiceCtl;
use crate::traits::{Clock, SysEnv};

#[allow(dead_code)]
pub struct WindowsTaskCtl {
    env: Arc<dyn SysEnv>,
    fs: Arc<dyn FsOps>,
    clock: Arc<dyn Clock>,
}

impl WindowsTaskCtl {
    pub fn new(env: Arc<dyn SysEnv>, fs: Arc<dyn FsOps>, clock: Arc<dyn Clock>) -> Self {
        Self { env, fs, clock }
    }
}

impl ServiceCtl for WindowsTaskCtl {}
