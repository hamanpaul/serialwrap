---
type: fix
scope: contracts
---

- Slice 2.4 覆蓋判定：`--longrun` 改為逐項驗證並匯入長跑證據，有效才解除 `lr-mixed` 的擁有者阻擋；參數化基底展開為宣告的完整參數集合；Cargo 節點只驗 Rust、雙邊黑箱與 Python contract_tests 分別驗證；artifact 雜湊改依平台分組並以 Windows runner `SHA256SUMS` 核對；Rust label 納入 rust-l1 案例的 pytest 檔；補齊 `mcu.patterns`／`mcu.status` 的 P8 映射。
