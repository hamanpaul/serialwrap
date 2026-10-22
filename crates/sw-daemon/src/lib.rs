//! `sw-daemon` 函式庫面（P4-T17）：供 `serialwrapd` 入口與整合測試共用 launcher 模組。
//! P5-T19：新增 `daemon_main`（daemon 本體組裝、有序關機、exit code）。

pub mod daemon_main;
pub mod framing;
pub mod health;
pub mod launcher;
pub mod service;
