# P7 待原生 Windows／擁有者處理項目（樹內追蹤）

> 審查 fa847c7／94526b4（Opus、grok）指出 `LOCAL-WINDOWS-TODO` 只寫在 commit message、樹內無追蹤；本檔為唯一追蹤處。
> 在證據到位前，下列項目一律不得標 PASS。

## BLOCKED-OWNER

| ID | 項目 | 需要的裁決／動作 | 來源 |
|---|---|---|---|
| P7-BO-1 | `s21_core.linux` 依 08 §0.6 退役清單以替換測試逐名移除 `cargo:sw-core::login_quiet::unsupported_profile_no_auto_tx`（→ `post_login_cmd_profile_logs_in`）與 `cargo:sw-core::session_manager::detect_skips_passthrough_console_unlock_and_unsupported`（→ `detect_skips_passthrough_and_console_unlock`） | OD-16：縮減／替換須擁有者明示核准；P7 PR 的 CI `regen_lock.py --check --base … --allow-removal` 清單需由擁有者加入上述兩個 nodeid（本分支不自行修改 CI 白名單） | fa847c7、94526b4 |
| P7-BO-2 | Task 14 B-08：測試團隊 Windows 工作站（`windows-task` 監管、第三方 EDR）實際觸發 handler 的行為與延遲；B-06（OD-08）：本 PR 動到 PAL，需原生 Windows COM handle 環境的回歸證據 | 擁有者於實機環境執行並提供報告 | Task 14 |

## LOCAL-WINDOWS-TODO

| ID | 項目 | 程序 | 來源 |
|---|---|---|---|
| P7-LW-1 | `s21_core.windows` 的一對一替換：原生收集含 `post_login_cmd_profile_logs_in`、`detect_skips_passthrough_and_console_unlock` 後，以 `regen_lock.py --l1-add-only --set s21_core --platform windows --collect <native-list> --allow-removal <舊 nodeid>` 替換。在此之前 windows 集合保留舊 nodeid（Windows gate 會報缺測、不得視為 PASS） | `cargo test -p sw-core --features test-fakes -- --list`（Windows 原生） | fa847c7、94526b4（本輪 add-only 還原 windows 集合 627 筆） |
| P7-LW-2 | sw-pal `fs_ops_p7`（7 項，含 `create_truncate_text_uses_platform_newline`——Windows 上必須斷言 `TEXT_NEWLINE == "\r\n"`）、`usb_topology::usb_windows_is_always_none` 的 Windows 原生執行與 `p7_gate` windows 集合登記 | `scripts/rust/win-test.sh --package sw-pal --test fs_ops_p7 --test usb_topology`，核對報告各 exe `passed == 該檔 Windows 適用 #[test] 數` 且 `ignored == 0` | 92e0223（Task 6） |
| P7-LW-3 | P7 新增 sw-core 測試的 Windows 原生執行（`p7_gate:cargo-test-workspace` 目前只有 linux） | 08 Task 19 Windows 原生 L1 | 全 P7 |
| P7-LW-4 | Task 13 `event_store`（27 項，含 DF-18 第二次覆寫與 `upsert_rule_file_indent2_sorted_no_newline` 的 CRLF 期望值）與 `runtime_paths_golden::events_log_path_env_override` 的 Windows 原生執行與 `p7_gate` windows 集合登記；`runtime_paths.windows.tsv` 尚無 daemon 目錄欄位，`events_log_path` 的 ntpath 期望值待 Windows Python 實測 | `scripts/rust/win-test.sh --package sw-core --test event_store`、`--package sw-proto --test runtime_paths_golden`，核對報告 `passed == 總數` 且 `ignored == 0` | Task 13 |
| P7-LW-5 | 13-py：Windows Python 3.12 執行 `tests/test_event_replace_df18.py`（DF-18 的 Windows 可觀測差異） | `scripts/stage0/win-pytest.sh tests/test_event_replace_df18.py` | Task 13-py |
| P7-LW-6 | Task 14 `handler_spawner`（9 項）Windows 原生：`CreateProcessW`＋Job Object、`cmd.exe /d /s /c` 引號、handle 白名單、`[WinError 2]` 訊息；報告 `passed == 10`（9 項 conformance＋`py_returncode_matches_python_popen`；`#[cfg(unix)]` 的 2 項不計）、`ignored == 0` 後登記 `p7_gate` windows 集合 | `scripts/rust/win-test.sh --package sw-pal --test handler_spawner`（`--test` 選 binary，禁用 `-- handler_spawner` 名稱過濾） ；另觀察：sysenv／com0com 的 `std::process::Command` spawn 不經 `WIN_SPAWN_LOCK`，與 handler spawn 同時發生時可能繼承 handler 子端 pipe（審查 96b28dd_06c61d0 L3 殘留，需原生壓測確認） | Task 14 |

## BLOCKED-UPSTREAM（P6）

| ID | 項目 | 依賴 |
|---|---|---|
| P7-BU-1 | quiet arm 改呼叫 `invalidate_human_input`（`input_epoch`＋`Log reason=boot_banner`／`QuietRearm`）、owner_gen 不再遞增；`banner_arm_bumps_fsm_gen_once_and_invalidates_human_input` 全名測試 | 07 T6 D-02 |
| P7-BU-2 | reprobe 的 `human_active`（`lease_context(...)["human_active"]`）；`human_active_blocks_reprobe` | 07 T7 |
| P7-BU-3 | `session_attach_paths` 的「ATTACHED 且 last_error None」null 子案（需 human lease 持有中的 capable ATTACHED） | 07 T7／T10 |
| P7-BU-4 | production executor 對 P6 `Effect::Console*`／`HumanTx`／`LeaseIndex` 為 `todo!`（同 tests/support）；P6 合入前 SessionCore 不產生 | 07 T6／T7／T9 |
