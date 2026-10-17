//! `remote` 子命令的隧道核心（P4-T8；07 §8.1～§8.5、BC-05）。
//!
//! P4-T1 紅燈骨架：只提供 plan Task 8 Interfaces 的簽章，函式本體一律 `unimplemented!("P4-T8")`，
//! 由 P4-T8 實作者依 plan「行為」段承接。
use std::path::{Path, PathBuf};

use sw_pal::fs_ops::{FileLockGuard, FsOps};
use sw_pal::traits::PalResult;
use sw_proto::pyjson::PyDict;

use crate::argparse::Namespace;
use crate::ctx::CliCtx;

/// 隧道方向：`-R`（expose）或 `-L`（connect）。
#[derive(Clone, Copy, Debug, PartialEq, Eq)]
pub enum Role {
    Expose,
    Connect,
}

/// 隧道程式：`ssh` 或 `autossh`。
#[derive(Clone, Copy, Debug, PartialEq, Eq)]
pub enum Via {
    Ssh,
    Autossh,
}

/// 一條隧道的完整規格（identity 由此計算）。
#[derive(Clone, Debug, PartialEq)]
pub struct TunnelSpec {
    pub role: Role,
    pub ssh_target: String,
    pub port: u16,
    pub local: Option<u16>,
    pub forward_src: Option<String>,
    pub remote_socket: Option<String>,
    pub via: Via,
    pub ssh_opts: Vec<String>,
    pub ready_timeout: f64,
}

/// 隧道錯誤：`code` 為穩定錯誤碼，`message` 逐字照 07 §8.5。
#[derive(Clone, Debug, PartialEq, Eq)]
pub struct TunnelError {
    pub code: &'static str,
    pub message: Option<String>,
}

/// 解析 `[user@]host:port`。
pub fn parse_target(t: &str) -> Result<(String, u16), TunnelError> {
    unimplemented!("P4-T8")
}

/// identity：各欄位以 `|` 串接後取 sha256 hex（含 `None` 字面）。
pub fn compute_identity(spec: &TunnelSpec) -> String {
    unimplemented!("P4-T8")
}

/// 預設 `-o` 選項序列（POSIX 含 ControlMaster 三組）。
pub fn default_ssh_opts(control_path: &str) -> Vec<String> {
    unimplemented!("P4-T8")
}

/// 組出 ssh／autossh argv；`control_master=false`（Windows）省略 Control* 三組 `-o`。
pub fn build_argv(spec: &TunnelSpec, cp: &str, control_master: bool) -> Vec<String> {
    unimplemented!("P4-T8")
}

/// `ss -ltnH` 輸出中該 port 的監聽列是否全為 loopback。
pub fn bind_is_loopback_only(ss_out: &str, port: u16) -> bool {
    unimplemented!("P4-T8")
}

/// `<run_dir>/remote/<port>.json` registry。
pub struct Registry {
    pub remote_dir: PathBuf,
}

impl Registry {
    pub fn new(run_dir: &Path) -> Self {
        unimplemented!("P4-T8")
    }

    pub fn lock(&self, fs: &dyn FsOps) -> PalResult<FileLockGuard> {
        unimplemented!("P4-T8")
    }

    pub fn read(&self, port: i64) -> Option<PyDict> {
        unimplemented!("P4-T8")
    }

    pub fn read_all(&self) -> Vec<PyDict> {
        unimplemented!("P4-T8")
    }

    pub fn write(&self, st: &PyDict, pid: u32) -> std::io::Result<()> {
        unimplemented!("P4-T8")
    }

    pub fn remove(&self, port: i64) {
        unimplemented!("P4-T8")
    }
}

pub fn open_tunnel(ctx: &CliCtx<'_>, spec: &TunnelSpec, run_dir: &Path) -> Result<PyDict, TunnelError> {
    unimplemented!("P4-T8")
}

pub fn status(ctx: &CliCtx<'_>, run_dir: &Path) -> PyDict {
    unimplemented!("P4-T8")
}

pub fn close(ctx: &CliCtx<'_>, run_dir: &Path, selector: &str) -> PyDict {
    unimplemented!("P4-T8")
}

pub fn run_remote(ns: &Namespace, ctx: &mut CliCtx<'_>) -> i32 {
    unimplemented!("P4-T8")
}
