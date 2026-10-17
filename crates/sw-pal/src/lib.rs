//! `sw-pal`：client 側平台抽象層。
//!
//! 平台相依 cfg 只允許出現在本 crate（arch §2.1）。業務 crate 只看 `ClientCaps` 的值，不比較 OS 名稱。
use std::sync::Arc;

pub mod clock;
#[cfg(feature = "test-fakes")]
pub mod fakes;
pub mod fs_ops;
pub mod osstr;
pub mod process;
pub mod rpc_transport;
pub mod select;
pub mod sysenv;
pub mod traits;

pub use osstr::{join_os_pieces, split_os_str, OsPiece};
pub use traits::*;

/// client 側平台能力包（arch §4 的 client 子集；P4 擴為 `Platform` 並保留別名）。
pub struct ClientPlatform {
    pub env: Arc<dyn SysEnv>,
    pub fs: Arc<dyn FsRead>,
    pub clock: Arc<dyn Clock>,
    pub rpc: Arc<dyn RpcConnector>,
    pub process: Arc<dyn ProcessCtl>,
    pub caps: ClientCaps,
}

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

/// 業務碼唯一可見的平台能力值（arch §2.1：不得比較 OS 名稱）。
#[derive(Clone, Copy, Debug)]
pub struct ClientCaps {
    pub path_flavor: PalPathFlavor,
    pub af_unix: bool,
    pub native_backend: BackendKind,
    pub default_skill: SkillPlatform,
    /// `"\n"` ｜ `"\r\n"`（Q-05）。
    pub text_newline: &'static str,
    /// POSIX execve；Windows 等待子行程。
    pub exec_replace: bool,
}

/// 組裝點呼叫：依目前平台建立 client 側能力包。
pub fn native_client() -> ClientPlatform {
    #[cfg(unix)]
    {
        use crate::clock::posix::NativeClock;
        use crate::fs_ops::posix::NativeFsRead;
        use crate::process::posix::NativeProcessCtl;
        use crate::rpc_transport::posix::NativeRpcConnector;
        use crate::sysenv::posix::NativeSysEnv;

        ClientPlatform {
            env: Arc::new(NativeSysEnv),
            fs: Arc::new(NativeFsRead),
            clock: Arc::new(NativeClock),
            rpc: Arc::new(NativeRpcConnector),
            process: Arc::new(NativeProcessCtl),
            caps: ClientCaps {
                path_flavor: PalPathFlavor::Posix,
                af_unix: true,
                native_backend: BackendKind::Posix,
                default_skill: SkillPlatform::Linux,
                text_newline: "\n",
                exec_replace: true,
            },
        }
    }

    #[cfg(windows)]
    {
        use crate::clock::windows::NativeClock;
        use crate::fs_ops::windows::NativeFsRead;
        use crate::process::windows::NativeProcessCtl;
        use crate::rpc_transport::windows::NativeRpcConnector;
        use crate::sysenv::windows::NativeSysEnv;

        ClientPlatform {
            env: Arc::new(NativeSysEnv),
            fs: Arc::new(NativeFsRead),
            clock: Arc::new(NativeClock),
            rpc: Arc::new(NativeRpcConnector),
            process: Arc::new(NativeProcessCtl),
            caps: ClientCaps {
                path_flavor: PalPathFlavor::Windows,
                af_unix: false,
                native_backend: BackendKind::Win,
                default_skill: SkillPlatform::Windows,
                text_newline: "\r\n",
                exec_replace: false,
            },
        }
    }
}
