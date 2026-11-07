---
type: feat
scope: rust-core
---
P8（Slice 2.4）Task 9：Rust daemon 承接 `/dev/ttyMCU` flash 端點與認線協調（`sw_core::flash_endpoint`）。端點 thread 只等 flasher 寫入、未 bridge 時從不寫 master；認線 probe 只轉送 flasher 自己的 sync bytes（首讀為空才退回 pattern probe），命中後把 MCU 回應整段回放給 flasher 一次、不重送；matched 後鏡射 flasher 的 baud 一次（B0 視為未設、走 pattern baud），pump 每筆以 `source:"flash"` 記 WAL；flasher 斷線以 holder-probe（armed 後持有者歸零，每 0.5 s、上限 1800 s，依 `pids` 判定，`/proc` 不可讀時永不 armed）結束本次 flash 並回 ATTACHED。停止旗標分兩層：單次 flash 結束只取消該次 `FlashRunCancel`，常駐端點繼續服務下一次燒錄；只有 `FlashEndpoint::stop`／`Drop` 設 `EndpointShutdown`，stop 於 2.0 s 內收斂、逾時以 `stalled_threads` 回報且不關閉仍在使用的 master，任何退出路徑都移除 ttyMCU symlink。`SessionManager` 新增 `tx_for`／`line_control_for`／`send_control`（以目前 bridge epoch 取值）。依編排者裁決，flash-critical 期間、probe 開始前收到的 `DeviceDetached` 與 Python 相同直接略過（不 drain、不延後）。daemon 組裝（啟動端點、`mcu.status` 接線）由 Task 10 承接。測試台 `FlashRig::run_until_flashing` 另等 holder watch thread 登記第一次等待後才返回（高負載下 thread 晚起跑會錯過之後的時間推進）。
