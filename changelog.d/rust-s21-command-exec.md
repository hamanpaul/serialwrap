---
type: feat
scope: rust-s21
---
Rust 命令執行：line／background／completion_regex、逾時後 CTRL_C／CTRL_D 救援並清空佇列、reboot 命令進 RECOVERING 與重開機恢復、agent interactive lease（1 s tick 主動到期，SC-13）、self-test 分類與 background／agent log capture；passthrough 命令依 completion_regex／可用 prompt／timeout 三規則完成，未命中不送 CTRL_C／CTRL_D、不降級，self-test 零 TX（SC-35）；recover REPROBE 未恢復回 SESSION_NOT_READY＋頂層 last_error（SC-34）。
