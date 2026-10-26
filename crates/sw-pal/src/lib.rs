//! `sw-pal`：平台抽象層（client 與管理面組裝點）。
//!
//! 平台相依 cfg 只允許出現在本 crate（arch §2.1）。業務 crate 只看 `Capabilities` 的值，不比較 OS 名稱。
//!
//! P4-T1：`Platform` 為 `ClientPlatform` 的超集（P3 呼叫端不改型別名稱，`ClientPlatform = Platform`、`ClientCaps = Capabilities`）。
use std::sync::Arc;

/// Slice 2.1 骨架未實作函式的統一 panic（與 `sw_core::s21_todo!` 同格式：`not implemented: slice21 <Task> <symbol>`）。
#[macro_export]
macro_rules! s21_todo {
    ($t:literal, $sym:literal) => {
        unimplemented!(concat!("slice21 ", $t, " ", $sym))
    };
}

pub mod clock;
pub mod com0com;
#[cfg(feature = "test-fakes")]
pub mod conformance;
pub mod console_com0com;
pub mod console_mux;
pub mod console_pty;
pub mod console_tcp;
pub mod device_source;
pub mod device_watcher;
#[cfg(feature = "test-fakes")]
pub mod fakes;
pub mod fs_ops;
pub mod lock;
pub mod osstr;
pub mod path_norm;
pub mod proc_scan;
pub mod process;
pub mod process_tunnel;
#[cfg(unix)]
pub mod proc_holders {
    #[path = "posix.rs"]
    pub mod posix;
}
pub mod rpc_transport;
pub mod select;
pub mod serial;
pub mod service_ctl;
pub mod signal;
pub mod sysenv;
#[cfg(feature = "test-fakes")]
pub mod testutil;
pub mod traits;
pub mod wal_fs;

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

/// 裝置來源後端（arch §4 `Capabilities.native_device`；Task 0 只凍結型別，欄位接線見 Task 5／Task 19）。
#[derive(Clone, Copy, Debug, PartialEq, Eq)]
pub enum DeviceBackendKind {
    Posix,
    Win,
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
    /// 裝置來源後端（arch §4 `Capabilities.native_device`；Task 5 的 `devices` 工廠與 Task 19 組裝點讀取）。
    pub native_device: DeviceBackendKind,
    /// OD-01 選配 com0com Serial console（SC-29；只在 Windows native 為 true，arch §4）。
    pub com0com_console: bool,
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
    /// 序列埠後端（arch §4.2）：依平台組裝 POSIX 或 Windows overlapped 實作。
    pub serial: Arc<dyn SerialBackend>,
    /// holder 診斷（arch §4.2；`/proc` 掃描或 Windows 的 `holder_unknown`）。
    pub uart_excl: Arc<dyn UartExclusive>,
    /// 裝置來源工廠（arch §4.3；Task 5 實作，Task 19 呼叫）。
    pub devices: DeviceFactory,
    /// 實例鎖工廠（arch §4.4；Task 4）。
    pub instance_lock: Arc<dyn InstanceLockFactory>,
    /// WAL 檔案層（arch §4.5；Task 6）。
    pub wal_fs: Arc<dyn WalFs>,
    /// RPC listener 工廠（arch §4.6；Task 17）。
    pub rpc_listen: Arc<dyn RpcListenerFactory>,
    /// human console 承接工廠（arch §4.7；Task 2 的 POSIX `create_pty`）。
    pub console: Arc<dyn ConsoleTransportFactory>,
    /// 停止訊號等待（arch §4.9；Task 4）。
    pub signals: Arc<dyn SignalWait>,
    pub caps: Capabilities,
}

/// 裝置來源工廠的型別（arch §4.3 `Platform.devices`）。
pub type DeviceFactory = Box<dyn Fn(DeviceSelection) -> Box<dyn DeviceSource> + Send + Sync>;

/// 依平台建立裝置來源工廠。
pub fn native_devices() -> DeviceFactory {
    #[cfg(unix)]
    {
        Box::new(|selection: DeviceSelection| -> Box<dyn DeviceSource> {
            Box::new(crate::device_source::PosixDeviceSource::new(
                vec![selection.by_id_dir, selection.by_path_dir],
                selection.test_allowlist,
            ))
        })
    }

    #[cfg(windows)]
    {
        use std::collections::HashSet;

        use crate::clock::windows::NativeClock;
        use crate::device_source::windows_registry::WinRegistryView;

        let clock: Arc<dyn Clock> = Arc::new(NativeClock);
        Box::new(move |selection: DeviceSelection| -> Box<dyn DeviceSource> {
            Box::new(crate::device_source::WindowsDeviceSource::new(
                Box::new(WinRegistryView),
                Arc::clone(&clock),
                selection.exclude_coms.into_iter().collect::<HashSet<_>>(),
                selection.test_allowlist,
            ))
        })
    }
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
        let console_kind = native_console_kind(&*env);
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
            uart_excl: Arc::new(crate::proc_holders::posix::PosixUartExclusive),
            devices: native_devices(),
            instance_lock: crate::lock::native_instance_lock(),
            wal_fs: Arc::new(crate::wal_fs::NativeWalFs),
            rpc_listen: Arc::new(crate::rpc_transport::NativeRpcListenerFactory),
            console: Arc::new(crate::console_pty::NativeConsoleFactory::new(console_kind)),
            signals: Arc::new(crate::signal::NativeSignalWait),
            caps: Capabilities {
                path_flavor: PalPathFlavor::Posix,
                af_unix: true,
                native_backend: BackendKind::Posix,
                default_skill: SkillPlatform::Linux,
                text_newline: "\n",
                exec_replace: true,
                on_demand_autostart: false,
                console_kind,
                native_device: DeviceBackendKind::Posix,
                com0com_console: false,
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
        let console_kind = native_console_kind(&*env);
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
            serial: Arc::new(crate::serial::windows::WindowsSerialBackend::new()),
            uart_excl: Arc::new(crate::serial::WindowsUartExclusive),
            devices: native_devices(),
            instance_lock: crate::lock::native_instance_lock(),
            wal_fs: Arc::new(crate::wal_fs::NativeWalFs),
            rpc_listen: Arc::new(crate::rpc_transport::NativeRpcListenerFactory),
            console: Arc::new(crate::console_pty::NativeConsoleFactory::new(console_kind)),
            signals: Arc::new(crate::signal::NativeSignalWait),
            caps: Capabilities {
                path_flavor: PalPathFlavor::Windows,
                af_unix: false,
                native_backend: BackendKind::Win,
                default_skill: SkillPlatform::Windows,
                text_newline: "\r\n",
                exec_replace: false,
                on_demand_autostart: true,
                console_kind,
                native_device: DeviceBackendKind::Win,
                com0com_console: true,
            },
        }
    }
}

/// SC-28（07 D-11）：`SERIALWRAP_CONSOLE_BACKEND` 只在此讀取；回 WARNING 時寫 stderr 一行。
fn native_console_kind(env: &dyn SysEnv) -> ConsoleKind {
    let raw = env.var("SERIALWRAP_CONSOLE_BACKEND");
    let (kind, warning) =
        console_mux::select_console_kind(raw.as_deref(), console_pty::native_has_pty());
    if let Some(w) = warning {
        eprintln!("{w}");
    }
    kind
}

/// P3 名稱保留：`native_client()` 與 `native()` 相同（P4-T1 起的 client 組裝點）。
pub fn native_client() -> ClientPlatform {
    native()
}
