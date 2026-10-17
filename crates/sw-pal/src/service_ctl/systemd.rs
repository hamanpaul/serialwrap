//! systemd `--user`／system 的控制（07 §5.2、§5.4）。
//!
//! P4-T1 只交付建構子；行為（命令產生、`hold_autostart` drop-in）由 P4-T3 實作。
#![allow(unused_variables)]

use std::sync::Arc;

use crate::fs_ops::FsOps;
use crate::service_ctl::ServiceCtl;
use crate::traits::{Clock, SysEnv};

/// 無 `cfg`：只產生並執行 argv；`fs` 只用於 R5-F1 hold drop-in（P4-T3）。
#[allow(dead_code)]
pub struct SystemdCtl {
    env: Arc<dyn SysEnv>,
    fs: Arc<dyn FsOps>,
    clock: Arc<dyn Clock>,
}

impl SystemdCtl {
    pub fn new(env: Arc<dyn SysEnv>, fs: Arc<dyn FsOps>, clock: Arc<dyn Clock>) -> Self {
        Self { env, fs, clock }
    }
}

impl ServiceCtl for SystemdCtl {}
