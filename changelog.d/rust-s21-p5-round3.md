---
type: fix
scope: rust-s21
---
Slice 2.1 第三輪審查修正：sw-daemon 行程測試的 `wait_ready` 逾時改為輸出 `last_error` 與假 DUT PTY 的外部持有者、自行 spawn 的子行程以守衛於 panic 時回收（F91／F111：外部行程搶開測試 PTY 時 daemon 依 #82 正確拒開，原本只剩狀態名且孤兒子行程會卡住 `cargo test`）。其餘處理見 `docs/rust-refactor/reviews/p5-fix-responses.md` 第三輪段。
