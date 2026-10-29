---
type: added
scope: func-test
---
OD-39：func-test runner 新增 `pre_daemon_steps`（daemon 啟動前步驟）、`assert_when`／`assert_value`（條件式斷言）、`assert_wal_tx_total`（cmd_id TX 總和聚合斷言）、`assert_any`（OR 斷言）、`query_dut_generation`、`assert_wal_window`（以 WAL 紀錄判定 op 窗口）與 `assert_wire_offsets`；Slice 2.1 的 exclusive-conflict／credentials／partial-tx-abort 情境改用之，bridge 工具樹未改。OD-43：`rx_rate_window_entries` 的 NC-P5-01 依 DF-22 結案。OD-41 依裁決停下回報（五種事件在兩個 daemon 的 WAL 皆無對應紀錄）。
