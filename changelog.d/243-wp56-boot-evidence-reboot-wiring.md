---
type: fix
scope: sw-core
---
Rust reboot 路徑接上非同步 reboot 開機證據等待（對齊 Python `_handle_reboot_command` 與 `_detach_session_locked`，`sw_core/session_manager.py:6409-6411`、`:6463-6466`、`:2704`，#243）：reboot 類命令（命令文字猜中或 `expect_reboot` 旗標，含串接命令）於送出命令前即歸零上一輪殘留的等待期限，不延後到 TX 結算；2 s 內 prompt 回來時只清 TX 靜默維度並起算 60 s 寬限，不確認 READY，舊 shell 在 banner 前的 nonce 應答不再清掉 Ready Reconfirm pending，等待期間 agent 命令回可重試的 `AUTOBOOT_QUIET`；看到 boot banner 即解除等待，寬限到期照常送確認 probe，且不延長 reconfirm 期限、不消耗 reprobe 次數或 backoff。2 s 內沒有 prompt 的 recovery 分支、一般命令與自發重開機都不起等待。無 bridge 的同步 detach（clear、release、拔除、重綁）歸零等待，quiet 與 reconfirm 照既有規則保留；有 bridge 的 drain 收斂與 Python `_complete_drain` 相同，不清等待，重掛後登入確認才清。
