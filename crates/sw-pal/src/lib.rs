//! `sw-pal`：平台抽象層（client 與管理面組裝點）。
//!
//! 平台相依 cfg 只允許出現在本 crate（arch §2.1）。業務 crate 只看 `Capabilities` 的值，不比較 OS 名稱。
//!
//! P4-T1：`Platform` 為 `ClientPlatform` 的超集（P3 呼叫端不改型別名稱，`ClientPlatform = Platform`、`ClientCaps = Capabilities`）。
use std::sync::Arc;

pub mod clock;
pub mod com0com;
#[cfg(feature = "test-fakes")]
pub mod conformance;
pub mod device_source;
pub mod device_watcher;
#[cfg(feature = "test-fakes")]
pub mod fakes;
pub mod fs_ops;
pub mod osstr;
pub mod proc_scan;
pub mod process;
pub mod process_tunnel;
pub mod rpc_transport;
pub mod select;
pub mod serial;
pub mod service_ctl;
pub mod sysenv;
pub mod traits;

#[cfg(feature = "test-fakes")]
pub use conformance::PeerEnd;
pub use device_watcher::{DeviceWatcher, WatcherHandle};
pub use osstr::{join_os_pieces, os_string_from_bytes, split_os_str, OsPiece};
pub use traits::*;

#[derive(Clone, Copy, Debug, PartialEq, Eq)]
pub enum PalPathFlavor {
    Posix,
    Windows,
}

#[derive(Clone, Copy, Debug, PartialEq, Eq)]
pub enum BackendKind {
    Posix,
    Win,
}

#[derive(Clone, Copy, Debug, PartialEq, Eq)]
pub enum SkillPlatform {
    Linux,
    Windows,
}

/// human console 的承接方式（arch §4.9）：POSIX 為 PTY，無 PTY 平台為 `127.0.0.1` TCP。
#[derive(Clone, Copy, Debug, PartialEq, Eq)]
pub enum ConsoleKind {
    Pty,
    TcpTelnet,
}

/// 業務碼唯一可見的平台能力值（arch §2.1：不得比較 OS 名稱）。
///
/// P3 `ClientCaps` 的全部欄位保留；P4-T1 新增 `on_demand_autostart`（SC-18，僅 Windows native 為 true）與 `console_kind`。
#[derive(Clone, Copy, Debug)]
pub struct Capabilities {
    pub path_flavor: PalPathFlavor,
    pub af_unix: bool,
    pub native_backend: BackendKind,
    pub default_skill: SkillPlatform,
    /// `"\n"` ｜ `"\r\n"`（Q-05）。
    pub text_newline: &'static str,
    /// POSIX execve；Windows 等待子行程。
    pub exec_replace: bool,
    /// SC-18：on-demand 首次需求是否允許自動拉起（僅 Windows native 為 true）。
    pub on_demand_autostart: bool,
    pub console_kind: ConsoleKind,
}

/// P3 名稱保留（`ClientCaps` 為 `Capabilities` 的別名）。
pub type ClientCaps = Capabilities;

/// 組裝點（arch §4）：client 與管理面共用的平台能力包。
pub struct Platform {
    pub env: Arc<dyn SysEnv>,
    pub fs: Arc<dyn FsOps>,
    pub clock: Arc<dyn Clock>,
    pub rpc: Arc<dyn RpcConnector>,
    pub process: Arc<dyn ProcessCtl>,
    pub procs: Arc<dyn ProcScan>,
    pub tunnel: Arc<dyn ProcessTunnel>,
    pub service: Arc<dyn ServiceCtl>,
    pub registry: Arc<dyn RegistryView + Sync>,
    /// 序列埠後端（arch §4.2）：POSIX 為 `PosixSerialBackend`，Windows 為 Task 3 骨架。
    pub serial: Arc<dyn SerialBackend>,
    pub caps: Capabilities,
}

/// P3 名稱保留（`ClientPlatform` 為 `Platform` 的別名）。
pub type ClientPlatform = Platform;

/// 組裝點呼叫：依目前平台建立完整能力包（P4-T1 起由 `Platform` 承接）。
pub fn native() -> Platform {
    #[cfg(unix)]
    {
        use crate::clock::posix::NativeClock;
        use crate::device_source::windows_model::NullRegistryView;
        use crate::fs_ops::posix::NativeFsOps;
        use crate::proc_scan::PosixProcScan;
        use crate::process::posix::NativeProcessCtl;
        use crate::process_tunnel::posix::NativeProcessTunnel;
        use crate::rpc_transport::posix::NativeRpcConnector;
        use crate::sysenv::posix::NativeSysEnv;

        let env: Arc<dyn SysEnv> = Arc::new(NativeSysEnv);
        let clock: Arc<dyn Clock> = Arc::new(NativeClock);
        let fs: Arc<dyn FsOps> = Arc::new(NativeFsOps);
        Platform {
            service: Arc::new(CompositeServiceCtl::native(
                Arc::clone(&env),
                Arc::clone(&fs),
                Arc::clone(&clock),
            )),
            env,
            fs,
            clock,
            rpc: Arc::new(NativeRpcConnector),
            process: Arc::new(NativeProcessCtl),
            procs: Arc::new(PosixProcScan::new()),
            tunnel: Arc::new(NativeProcessTunnel),
            registry: Arc::new(NullRegistryView),
            serial: Arc::new(crate::serial::posix::PosixSerialBackend::new()),
            caps: Capabilities {
                path_flavor: PalPathFlavor::Posix,
                af_unix: true,
                native_backend: BackendKind::Posix,
                default_skill: SkillPlatform::Linux,
                text_newline: "\n",
                exec_replace: true,
                on_demand_autostart: false,
                console_kind: ConsoleKind::Pty,
            },
        }
    }

    #[cfg(windows)]
    {
        use crate::clock::windows::NativeClock;
        use crate::device_source::windows_registry::WinRegistryView;
        use crate::fs_ops::windows::NativeFsOps;
        use crate::proc_scan::WindowsProcScan;
        use crate::process::windows::NativeProcessCtl;
        use crate::process_tunnel::windows::NativeProcessTunnel;
        use crate::rpc_transport::windows::NativeRpcConnector;
        use crate::sysenv::windows::NativeSysEnv;

        let env: Arc<dyn SysEnv> = Arc::new(NativeSysEnv);
        let clock: Arc<dyn Clock> = Arc::new(NativeClock);
        let fs: Arc<dyn FsOps> = Arc::new(NativeFsOps);
        Platform {
            service: Arc::new(CompositeServiceCtl::native(
                Arc::clone(&env),
                Arc::clone(&fs),
                Arc::clone(&clock),
            )),
            env,
            fs,
            clock,
            rpc: Arc::new(NativeRpcConnector),
            process: Arc::new(NativeProcessCtl),
            procs: Arc::new(WindowsProcScan::new()),
            tunnel: Arc::new(NativeProcessTunnel),
            registry: Arc::new(WinRegistryView),
            serial: Arc::new(crate::serial::WindowsSerialStub),
            caps: Capabilities {
                path_flavor: PalPathFlavor::Windows,
                af_unix: false,
                native_backend: BackendKind::Win,
                default_skill: SkillPlatform::Windows,
                text_newline: "\r\n",
                exec_replace: false,
                on_demand_autostart: true,
                console_kind: ConsoleKind::TcpTelnet,
            },
        }
    }
}

/// P3 名稱保留：`native_client()` 與 `native()` 相同（P4-T1 起的 client 組裝點）。
pub fn native_client() -> ClientPlatform {
    native()
}
