---
type: fix
scope: rust-refactor
---
Rust daemon 的 `session.clear` 在 session 有 bridge（drain 收尾）且裝置在場時，回應與即時狀態即為 `state:"ATTACHING"`、`last_error:null`，不再先帶 `CLEARED` 等再解析後的 `DeviceAttached` 才清除（取代先前依審查 F97 保留 `CLEARED` 的行為，裁決 D6）；裝置不在場仍為 `DETACHED`＋`CLEARED`，drain 進行中仍為 `RECOVERING`＋`CLEARED`，無 bridge 路徑行為不變（對齊 Python `sw_core/session_manager.py` `_complete_drain` S1X-a-7）。
