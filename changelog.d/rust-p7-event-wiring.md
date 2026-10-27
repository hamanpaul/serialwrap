### Fixed

- DF-15：事件引擎取用該 RX chunk 的精確 WAL seq（reader 於 WAL append 後依序呼叫 RX observer，observer 例外不影響 reader）。

### Added

- Rust daemon：Event Engine 隨 daemon 啟停（先於 bootstrap attach 啟動；停機時於 drain 前停止受理、drain 後以共用期限收尾 handler），10 個 `event.*` RPC 正式提供；`SessionManager` 承接 `BridgeQueries`（`known_coms`／`profile_for`／`active_cmd_id_for`）。
