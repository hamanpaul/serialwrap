//! POSIX 監管模式清單（arch §4.8）：`on-demand`、`systemd-user`、`systemd-system`。
use crate::service_ctl::SupervisionMode;

pub fn host_modes() -> Vec<SupervisionMode> {
    vec![
        SupervisionMode::OnDemand,
        SupervisionMode::SystemdUser,
        SupervisionMode::SystemdSystem,
    ]
}
