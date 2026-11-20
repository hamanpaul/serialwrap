---
type: fix
scope: realhw
---
PR #278 十輪 hosted CI 審查的一批修正（只動 workflow 與測試，不動產品碼）：`rust.yml`／`tests.yml` 的後續步驟改為 `if: !cancelled()`、Linux `cargo test` 加 `--no-fail-fast`，前面步驟失敗時仍跑完第二遍 pytest、L1 鎖檢查與 Stage 2 黑箱（job 結論仍為失敗）；`test_login_with_delay_and_noise` 失敗時印出 session 狀態變化序列；`test_cmd_judge_reloads_parity_like_assemble` 固定日期（原自 2027-04-08 起必紅）；`test_stop_during_push_flushes_wal` 記錄 issue #282 的可觀察量並限制重啟後至多多執行一個 chunk 命令；結構檢查改以 AST／Dockerfile 指令序判定，不再釘字面次數與行序；sw-daemon 測試 rig 的共用等待上限改回 10 秒，只有 reboot recovery 那一處保留 60 秒。
