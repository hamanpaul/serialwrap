//! Windows 監管模式清單（arch §4.8）：`on-demand`、`windows-task`（預設）、`windows-service`（OD-12）。
use crate::service_ctl::SupervisionMode;

pub fn host_modes() -> Vec<SupervisionMode> {
    vec![
        SupervisionMode::OnDemand,
        SupervisionMode::WindowsTask,
        SupervisionMode::WindowsService,
    ]
}
