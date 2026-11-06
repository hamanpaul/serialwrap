# Rust 重構 Low 待辦

> 依 [`review-convergence-rules.md`](review-convergence-rules.md) §1：Low 不擋放行，統一收在這裡，Stage 3 Python daemon 退役前以一個收尾包處理。由編排者從審查報告收錄。
> 欄位：來源報告 | finding 編號 | 檔案:行 | 摘要 | 優先（安全／資料完整性疑慮標 ★）| 狀態

| 來源報告 | 編號 | 檔案:行 | 摘要 | 優先 | 狀態 |
|---|---|---|---|---|---|
| s1-exit/opus-c-20261029.md | S1X-c-3 | crates/sw-cli/src/rollback.rs:41 | 正式二進位仍編入 `crash_if` 注入點與 `pub fn set_test_crash_point`／`set_test_hook`（未設定時不作用） | ★（release bundle 不含故障注入的精神） | 待處理 |
| s1-exit/opus-c-20261029.md | S1X-c-4 | crates/sw-cli/src/setup_cmd.rs:1032 | `windows_task_definition` 固定 `register_held: false`，不理會 `ReconcileRequest.hold_target` |  | 待處理 |
| s1-exit/opus-a-20261029.md | S1X-a-9 | sw_core/session_manager.py:6536 | BC-03 新增 WAL META command_aborted_in_flight 欄位未在 BC-03 列舉 |  | 待處理 |
| s1-exit/opus-a-20261029.md | S1X-a-10 | sw_core/session_manager.py:4428 | revoke_tx_op 把 interactive:<id> 永久加入 revoke ledger，同 lease 後續 interactive_send 得 SESSION_BUSY；cancel 語意待補登記 | ★ | 待處理 |
| s1-exit/opus-a-20261029.md | S1X-a-11 | sw_core/uart_io.py:1915 | captured_owner_gen 在 select 前取快照，arm_boot_quiet 同批 raw 按鍵被丟（狂按鍵打斷 autoboot 場景） | ★ | 待處理 |
| s1-exit/opus-a-20261029.md | S1X-a-12 | sw_core/session_manager.py:606-635 | arm_boot_quiet 每個 banner chunk 都 owner_gen++，有效 lease 的 interactive_send 誤判 INTERACTIVE_EXPIRED |  | 待處理 |
| s1-exit/opus-a-20261029.md | S1X-a-13 | sw_core/uart_io.py:2400 | send_command_echo_paced 等 echo 期間持有 _write_lock，窄競態下 TRANSFER_ECHO_STALL |  | 待處理 |
| s1-exit/opus-a-20261029.md | S1X-a-14 | sw_core/arbiter.py:~161 | _flush_session_locked 提早歸還已 cancel 仍在執行命令的 admission ticket |  | 待處理 |
| s1-exit/opus-a-20261029.md | S1X-a-15 | sw_core/serial_port.py probe_holders | 對所有 /proc/*/fd/* os.stat 未以 /dev/ 前綴過濾，卡住的 NFS／FUSE fd 會讓 attach 卡住 | ★ | 待處理 |
| s1-exit/opus-a-20261029.md | S1X-a-16 | sw_core/session_manager.py:4277 | _detach_by_id 在 DeviceWatcher 執行緒同步等 drain，1 s tick 最多延後 3 s |  | 待處理 |
| s1-exit/opus-a-20261029.md | S1X-a-17 | sw_core/service.py _ADMISSION_METHODS | 唯讀 session.interactive_status／command.result_tail／result.tail 列在 admission 內，凍結窗回 FLUSHED_BY_SHUTDOWN |  | 待處理 |
| s1-exit/opus-b-20261029.md | S1X-b-3 | crates/sw-proto/src/pyre | CPython 對可疑集合語法的 FutureWarning 未重現 |  | 待處理 |
| s1-exit/opus-b-20261029.md | S1X-b-4 | crates/sw-cli/src/lib.rs:131-145 | 測試用 SERIALWRAP_TEST_DELEGATE_GROUP 無 feature 閘，編進正式 binary | ★ | 待處理 |
| s1-exit/opus-b-20261029.md | S1X-b-5 | crates/sw-cli/src/commands/supervision_cmd.rs:59-92 | supervision-mode <非 set 字詞> 錯誤形狀與 Python 不同 |  | 待處理 |
| s1-exit/opus-b-20261029.md | S1X-b-6 | crates/sw-cli/src/endpoint_resolve.rs:229-256 | 非字串 config 值的兩處 stderr 文字與 Python 不同 |  | 待處理 |
| s1-exit/opus-b-20261029.md | S1X-b-7 | crates/sw-cli/src/commands/daemon_cmd.rs:1260-1275 | process_home／process_cwd 繞過 PAL 直接讀行程環境 |  | 待處理 |
| s1x-b-fixes-report-20261029.md §5（既有碼待辦） | S1X-b-X1 | crates/sw-proto/src/pyre | 樣式以單獨 `\` 結尾時 CPython tokenizer 預讀的 `bad escape (end of pattern)` 優先於前一 token 錯誤（如 `[z\A\`、`a\8\`、`(?P<\`），Rust 回報前一 token；影響 `profile test` 的 error 文字 | ★（對外 JSON 欄位） | 待處理 |
| S1-109f659-2 | Low | sw_core OD-63 佔位：template 已移除且舊板永久離線時，沒有 RPC／CLI 出口可移除佔位 | 候選 (a) `session bind` 目標 by_id 在線時佔位 retire 並交 dynamic；(b) 追認已知限制並寫明處置（加回 template 名後 `device attach`，或停 daemon 改 state.json） | Stage 3 |
| S1X-a-既有碼-revoke_tx_op | Low | `revoke_tx_op`：取消落在 interactive TX 完成後時一併撤銷 `interactive:<lease_id>`，lease 之後持續回 `SESSION_BUSY` | 先以 Python 實測確認可達性，再定修法或開 issue | Stage 3 |
| S1-607ea76-1 | Low | `run_upgrade_container.py` 手動降級 stop 等待逾時後仍可能執行後續 pipx／setup | 2026-11-05 自 Stage 1 清單 D10 移入 | 待處理 |
| S1-607ea76-2 | Low | `release-0.4.0-plan.md` 舊證據取代方向描述文字顛倒 | 同上 | 待處理 |
| S1-607ea76-3 | Low | `run_upgrade_container.py` 部分 driver 讀取混用固定 `/proc` 與 `proc_root` | 同上 | 待處理 |
| S1-607ea76-4 | Low | Windows 測試替身以 Timer 刪除目錄，可能遭遇檔案分享競態 | 同上 | 待處理 |
| S1-ed46551-1 | Low | `run-upgrade.sh` dry-run 在重用 `--bundle` 時仍列出自建步驟 | 同上 | 待處理 |
| S1-ed46551-2 | Low | 系統 Python 版本與 build venv 基底的 fail-closed 路徑缺少指定回歸測試 | 同上 | 待處理 |
| S1-ed46551-3 | Low | `require_system_python` 未先檢查 `venv` 與 `ensurepip` 模組可用性 | 同上 | 待處理 |
| 既有碼-support-imports | Low | `crates/sw-cli/tests/support/mod.rs` 4 處未使用 import 警告 | 審查 78cbc7a3_d929cac4 | 待處理 |
| 既有碼-win-test-warnings | Low | `crates/sw-pal/tests/osstr.rs`、`process_ctl.rs` Windows 交叉編譯警告 | 審查 f59ce805_b70bb35f | 待處理 |
| 既有碼-d11-r16-python | Low | policy_check R-16 的 CLI help 比對結果取決於 `python3` 版本（非 3.12 會不同）；Stage 1 的修正已撤回 | stage1-ledger D11 | 待處理 |
| 既有碼-bridge-close-timeout | Low | bridge EvidenceWriter 的 `close_timeout_s=10` 寫死，慢磁碟加 1 MiB 推送會 `HARNESS_EVIDENCE_WRITE_FAILED`（exit 3）；改 `src/` 需重新核准 bridge 雜湊 | Stage 1 出口 | 待處理（Stage 2） |
| 既有碼-run-upgrade-dirty | Low | `test_run_upgrade_*` 單元測試對工作樹是否乾淨敏感 | Stage 1 出口 | 待處理 |
| 既有碼-common-dead-code | Low | `crates/sw-proto/tests/common/mod.rs` 有 `dead_code`／`unused_imports` 警告，與既有碼-support-imports 一併清理 | 審查 5cac8744_62276c32 | 待處理 |
| 既有碼-fs-lock-once | Low | `sw-pal::fs_lock::file_lock_nonblocking_contention_wouldblock` 曾紅燈一次，單獨與 workspace 重跑皆過，尚無證據定性 | 審查 5cac8744_8572fbbd | 觀察中 |
| SG-CI3-01 | Low | Windows／UNC 路徑下的 WAL 輪替（`tests/test_wal.py::TestWalTailLatest::test_tail_raw_latest_scope_is_current_file_after_rotation`）與 4 支資源報告（`tests/test_resource_report.py`）在原生 Windows 驗證受阻，本輪依 OD-75 與 brief 停止修改 Python daemon，亦未改動凍結鎖，保留原始 FAIL 狀態登記於 `docs/rust-refactor/reviews/s1m-ci3-round1.md`，待擁有者裁決驗證環境或指派 Python daemon 調查。 | 審查 5cac8744_a634cc75 | 待處理 |
| 既有碼-config-profiles-escape | Low | `tests/test_config_profiles.py:229` 字串未加 raw，Python 3.12 有 `SyntaxWarning: invalid escape sequence` | 審查 5cac8744_a634cc75 | 待處理 |
| 既有碼-win-test-stderr | Low | `test_win_test_report_schema` 斷言沒有印 `r.stderr`；負載 47～56 時第 9 個執行檔 `--list` 回 rc=1 失敗一次，原因未知，同範圍重跑 3 次皆 132 支全過 | PR 249 本機測試 | 待處理 |
| 既有碼-pages-jekyll | Low | `docs/rust-refactor/inventory/02-rpc.md:548` 的 `{%` 讓 GitHub Pages 的 Jekyll 建置失敗，PR 248 合入 main 後 Pages 無法更新 | main 0ea3cc9c | 已修（PR 251，d5eaa57b；Pages 已建置成功） |
