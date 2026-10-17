//! `windows-service`（sc.exe；OD-12 限制，本 Phase 只產生 `pending_sudo` 指令）。
//!
//! P4-T1 只交付建構子；行為由 P4-T3 實作。
#![allow(unused_variables)]

use std::sync::Arc;

use crate::service_ctl::ServiceCtl;
use crate::traits::{Clock, SysEnv};

#[allow(dead_code)]
pub struct WindowsServiceCtl {
    env: Arc<dyn SysEnv>,
    clock: Arc<dyn Clock>,
}

impl WindowsServiceCtl {
    pub fn new(env: Arc<dyn SysEnv>, clock: Arc<dyn Clock>) -> Self {
        Self { env, clock }
    }
}

impl ServiceCtl for WindowsServiceCtl {}
