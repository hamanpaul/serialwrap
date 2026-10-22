//! `health.ping`／`health.status` 回應組裝（arch §6.3、02 §7.1 key 順序；P5-T18）。
//!
//! Task 0 骨架：函式簽章凍結，本體由 Task 18 交付。

use sw_proto::pyjson::PyDict;

use crate::service::Service;

/// `health.status` 回應（key 順序照 02 §7.1；含 OD-11 allowlist 顯示）。
pub fn health(_svc: &Service) -> PyDict {
    sw_core::s21_todo!("T18", "health")
}
