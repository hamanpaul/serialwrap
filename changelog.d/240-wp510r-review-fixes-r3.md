---
type: fix
scope: rust-core
---
依據 S2-PAR-wp510r R1 審查修正保管帳本（CustodyLedger）與送達者（Deliverer）時間結算缺失：
- P8-50e1e79-1 [Medium]：`Deliverer` 於進入 Pending 時先結算截至凍結時點的 Open 耗時，解凍時記錄實際轉態時點，下一次投遞計入解凍後耗時；補齊「凍結前尚有未入帳 Open 區間」與「解凍後延遲輪詢」逾時丟棄回歸測試。
- P8-057a065-4 [Medium]：詳述 actor.rs panic 3 次轉入 RECOVERING 與既有 L1 凍結節點一致之技術事實，提出不成立理由並交由編排者裁決。
