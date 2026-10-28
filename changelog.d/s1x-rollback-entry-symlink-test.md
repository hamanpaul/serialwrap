---
type: fix
scope: rust-cli
---

Stage 1 出口 G9b：補上 POSIX 回滾入口 symlink 的啟動回歸測試（sol 72a887e，Low）。新增 `crates/sw-cli/tests/rollback_entry_symlink.rs`：穩定入口 `bin/serialwrapd` 為指向回滾目標版本 launcher 的 symlink 時，on-demand 回滾的 `spawn_daemon` 必須以該入口啟動，測試直接斷言 spawn 的 argv[0]、入口解析結果與入口位元組，不再只依賴版本回報替身。spawn 路徑以原生 `Path` 比較，保留入口身分斷言並接受 Windows 的 `/`、`\` 等價分隔符表示。以暫時把入口判定還原為 `is_file()` 語意驗證測試會失敗（spawn 改用 CLI bundle launcher），驗證後已恢復。本次未變更產品碼 `crates/sw-cli/src/rollback.rs`。
