---
type: change
scope: acceptance
---
Slice 2.2 出口對照表依 OD-40 調整繼承案例 `l15-wal-interference` 的證據：自 L1.5 runner 判定逐名移出（`compare: none` 不產比較檔，Linux 上 not_applicable 段恆為證據不足），改由 `slice21_gate_expect.yaml` `l15_ab_excluded` 所列 Rust 側 WAL 黑箱三案與 `sw-core::wal_writer` 三個 L1 測試於 2.2 artifact 重跑承擔；六個 nodeid 以 `--l1-add-only` 登記到 `s22_gate:blackbox_rust`／`s22_gate:cargo_workspace`。情境檔與必要案例保留。
