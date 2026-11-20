---
type: fix
scope: sw-core
---
Rust 開埠在途（Attaching、bridge 尚未接上）收到 clear、拔除、重綁或 release 時，現在同樣歸零非同步 reboot 的開機證據等待，對齊 Python 的同步 `_detach_session_locked`（`sw_core/session_manager.py:2704`），#243。clear 與拔除／重綁在請求當下歸零（`:3059`、`:4470`）。release 在開埠收斂時歸零（`_finish_attach_op` `:4344`），開埠成功或失敗都一樣；已 RELEASED 的冪等 release 不歸零。修正前，async reboot 後經 clear 重掛、開埠未完成時送 release，session 進 RELEASED 後 `boot_evidence_wait_remaining_s` 仍會倒數最多 60 s。quiet 與 READY reconfirm 照既有規則保留。
