//! RPC transport 實作放在 `posix`／`windows` 子模組，依目標平台擇一編譯。
#[cfg(unix)]
pub mod posix;
#[cfg(windows)]
pub mod windows;

/// 原生 RPC listener 工廠（arch §4.6；P5-T17 交付 POSIX AF_UNIX／Windows loopback TCP）。
pub struct NativeRpcListenerFactory;

impl crate::traits::RpcListenerFactory for NativeRpcListenerFactory {
    fn bind(
        &self,
        _ep: &crate::traits::EndpointSpec,
        _opts: &crate::traits::ListenOptions,
    ) -> crate::traits::PalResult<Box<dyn crate::traits::RpcListener>> {
        crate::s21_todo!("T17", "NativeRpcListenerFactory::bind")
    }
}
