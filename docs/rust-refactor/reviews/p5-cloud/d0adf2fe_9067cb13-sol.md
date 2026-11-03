> 審查者：pi＋gemini-4-argon（11 月 argon 已花 $17.63）（代理 sol 單審位，擁有者 2026-11-02 指定）

## A 前輪 finding 核對

| 編號 | 原嚴重度 | 狀態 | 累計未修輪數 | 說明 |
|---|---|---|---|---|
| 1 | Medium | 已修 `25ad3900` | 0 | `OpSpec::InteractiveOpenCmd` 呼叫 `lease_send` 時傳入 `grant.cmd_id.as_deref()`，未完整送出且已確認 >0 bytes／Unknown／無 reject 時改經 `map_send_failure` 結算為 `COMMAND_ABORTED_IN_FLIGHT` 並寫入 META；`command_exec::interactive_mode_command_*` 3 支回歸實跑通過。 |
| 2 | High | 已修 `25ad3900`（`0a7d0929` 補強） | 0 | `ManagerExec` 將 `OpenBridge` 開埠與 `Persist` commit 等待移至 actor 外的有界 `JobQueue` 工作者（`0a7d0929` 並將 `model_apply(delta)` 固定於 actor 順序同步套用）；`session_manager::product_executor_blocked_open_keeps_actor_responsive_and_release_drain_stalled` 與 `product_executor_blocked_release_commit_keeps_actor_responsive` 實跑通過。 |
| 3 | Medium | 已修 `25ad3900` | 0 | 新增 `SessionRequest::SetAlias` 同步更新 actor 持有的 `profile.alias` 並等待確認後才 commit；`session_manager::alias_set_reaches_actor_profile_for_get_state` 實跑通過。 |
| 4 | Medium | 已修 `25ad3900` | 0 | 啟動尾端 `commit_sync` 失敗時將 Python 例外末行寫入 `startup_error`，daemon 印 traceback 並以 rc 1 結束、不進入掃描與 attach；`session_manager::startup_save_failure_reports_python_exception_line` 與 `daemon_process::unwritable_state_dir_startup_save_exits_1_before_attach` 實跑通過。 |
| 5 | Medium | 已修 `25ad3900` | 0 | `daemon_main.rs:296` 遇裸 `--` 時一併推入 `extras`，對齊 Python 3.12 `argparse` 無 positional 時的 `unrecognized arguments: --`（rc 2）；`daemon_process::bare_double_dash_is_unrecognized_exit_2` 實跑通過。 |
| 6 | High | 已修 `25ad3900` | 0 | `probe_send` 初始化 PROBE `Fence` 後呼叫 `self.fanout.register(fence.clone())`；`session_manager::detect_probe_fence_observes_wal_unhealthy_published_before_submit` 實跑通過。 |
| 7 | Medium | 已修 `25ad3900` | 0 | `ManagerNote::EvictCapture` 改以 `e.profile.com == com` 精確查找 owner session，不經 alias-aware `resolve()`；`session_manager::global_bg_capture_eviction_owner_routing_ignores_alias_collision` 實跑通過。 |
| 8 | Medium | 已修 `25ad3900` | 0 | `scripts/rust/slice21_gate_judge.py` 抽出 `build_expect()` 供 `cmd_assemble` 與 `cmd_judge` 共用，`judge` 子命令會重新載入並驗證 `#104` parity 狀態與來源。 |

## B 新發現

- **P5-9067cb1-1** [Medium] `crates/sw-pal/src/conformance/mod.rs:12` — `9067cb13` 在 `pub use lock_suite::lock_suite;` 上誤加 `#[cfg(unix)]`，導致非 unix（Windows）目標下 `conformance::lock_suite` 只剩模組而無同名函式 re-export，破壞 Windows 測試建置。
  - **具體失敗情境**：執行 `CARGO_TARGET_DIR=/tmp/solrev-p5-target cargo check --workspace --tests --features test-fakes --target x86_64-pc-windows-gnu`（或單獨檢查 `-p sw-pal --test lock`）。預期 exit 0；實際在 `crates/sw-pal/tests/lock.rs:25` 的 `conformance::lock_suite(native().instance_lock, d.path());` 編譯失敗：`error[E0423]: expected function, found module conformance::lock_suite`，使跨平台 `lock_suite`（含凍結於 `l1_required.lock.json` `s21_core.windows` 的 `cargo:sw-pal::lock::lock_conformance_suite`）及 `tests/lock.rs` 內所有 Windows 測試無法編譯執行。
  - **確切修法**：移除 `crates/sw-pal/src/conformance/mod.rs:12` 於 `pub use lock_suite::lock_suite;` 前新增的 `#[cfg(unix)]`（僅保留下一行 `pub use lock_suite::bind_stale_unix_socket;` 的 `#[cfg(unix)]`），並以 `cargo check --workspace --tests --features test-fakes --target x86_64-pc-windows-gnu` 確認歸零。

- **P5-9067cb1-2** [Low] `crates/sw-pal/src/sysenv/posix.rs:249` — 新增的 7 處 `// SAFETY:` 註解（`:249`、`:256`、`:258`、`:276`、`:287`、`:300`、`:311`）以英文撰寫，與 `CLAUDE.md` 語言政策（註解一律使用繁體中文）及同檔既有中文 `// SAFETY:` 風格不一致。
  - **確切修法**：將上述 7 處 `// SAFETY:` 註解改為繁體中文。

- **P5-9067cb1-3** [Low] `crates/sw-pal/tests/sysenv.rs:101` — 新增的回歸測試 `cargo:sw-pal::sysenv::native_sysenv_spawn_pause_is_reached_and_released` 尚未登記入 `tests/acceptance/l1_required.lock.json`（`s21_core.linux` 與 `s21_gate:linux_cap_posix_pty.linux`）。
  - **確切修法**：於本子項或後續收斂子項執行 `regen_lock.py --l1-add-only` 將該 nodeid 納入對應 Linux L1 集合。

## 既有碼待辦（不計入結論）

無

## 對裁決的異議（不計入結論）

無

結論：需修正