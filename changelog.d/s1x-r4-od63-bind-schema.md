---
type: docs
scope: contracts
---

契約補記 OD-63 佔位 session 的兩個既有行為（編排者裁決 S1X-a 第 3 輪 SPEC-GAP 1、2），程式不變：`contracts/rpc_schema.json` 的 `session.bind` result 比照 `session.attach` 的 RELEASED 分支補 `released`、`recommended_action` 屬性與 `x-order`，side_effects 的一般路徑一筆加 x-condition 排除佔位，另補一筆佔位早退項（佔位不離開 RELEASED、無其他副作用），`x-catalog-revision` 依 P0 規則遞增（rpc_schema 14→15；file.* META 說明同輪修改的 wal_meta_events 4→5）；DF-04 的 compatibility_impact、`openspec/specs/device-handoff/spec.md` 的 OD-63 情境與 README（中英）補述佔位 `session bind` 的回應，以及佔位開埠失敗且無 passthrough fallback template 時不留列（`device attach` 回 `SESSION_NOT_FOUND`）、`_pending_com` 保留原 COM 至裝置拔除或 daemon 重啟，之後依 dynamic 規則重新分配 COM。
