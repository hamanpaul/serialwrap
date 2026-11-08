---
type: fix
scope: rust-refactor
---
Rust daemon 完成非同步 reboot 開機證據等待（G8，#243，WP5-6 併 WP7-1）的整體驗收：以 ServiceRig 黑箱證明 `command.submit` 送出 `reboot`、假 DUT 立即重印 prompt 後，session 公開 `boot_evidence_wait_remaining_s`，舊 shell 仍能應答 nonce 但等待期間 UART 零 TX；後續 agent 命令以可重試的 `AUTOBOOT_QUIET` 拒絕（無 cmd_id、零副作用），`session.self_test` 回 `AUTOBOOT_QUIET`／`wait` 且不送 nonce probe；RX 見 boot banner 即解除等待並重臂 boot quiet，開機完成的 prompt 解除 quiet 後確認 probe 才真正送出並 `confirm_ready`，後續命令照常寫到 UART。Python 基線新增欄位後已過期的 `contracts/golden/p7/gate_responses.ndjson` 以 `scripts/rust/capture_p7_goldens.py` 重新擷取，差異僅為各列 session 新增 `boot_evidence_wait_remaining_s: null`，Rust 回應與之逐位元組一致。
