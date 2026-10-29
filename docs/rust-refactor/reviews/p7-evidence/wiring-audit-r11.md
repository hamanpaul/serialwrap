# P7 接線＋端到端驗收（第 11 輪，審查收斂規則 §5）

> 範圍：Slice 2.3（P7）新增的 sw-core／sw-pal 模組。方法：掃描各檔 `pub fn`／`pub(crate) fn`，以字詞邊界在
> `crates/*/src/**`（排除 `#[cfg(test)]` 之後、`fakes/`、`conformance/`）尋找定義檔以外的呼叫端；再為每個模組指定一條
> daemon 級端到端黑箱（Rust CLI＋daemon，`SERIALWRAP_AB_LABEL=rust-p7`），該段改為 no-op 時必紅。
> 掃描腳本與原始輸出見本輪回報；基準 commit 為本檔所在 commit。

## 無正式呼叫端的 pub fn

| 檔案 | 函式 | 判定 |
|---|---|---|
| `transport_stall.rs` | `refine_probe_failure` | **接線缺口（已修，392daa5）**：修前 0 個呼叫端；現由 `SessionCore::refine_login_failure` 於三個 probe 失敗收斂點呼叫 |
| `login_fsm.rs` | `login_error_detail` | 被 `refine_probe_failure` 的未翻轉分支取代（等價：clean_text 後末 300 字元），僅剩單元測試 `login_quiet::login_error_detail_carries_rx_tail` 使用；非缺口（Low：可刪除） |
| `self_test.rs` | `assemble`、`nonce_probe_result`、`released_result` | Task 0 骨架（本體 `todo!("P7 Task 8")`）；行為實作在 `SessionCore::self_test_pre_probe`／probe 收斂，端到端由 `test_p7_selftest_recover.py::test_selftest_classifications` 六子案覆蓋。非缺口（Low：骨架殘留） |
| `login_run.rs` | `apply_login_finished` | Task 0 骨架（`todo!("P7 Task 7")`）；收斂表實作在 `SessionCore::login_finished`／`auto_reprobe_converge`。非缺口（Low） |
| `capture.rs` | `log_path_for` | 與 `SessionCore` 的 `{com}_{%y%m%d-%H%M%S}.log` 組法重複（golden 測試用）；非缺口（Low） |
| `capture.rs` | `set_counts_hook` | test-fakes 注入點，設計上無正式呼叫端 |
| `event_engine/schema.rs` | `Rule::cooldown_ms_sat` | 僅測試使用；matcher 以任意精度 `int_cmp` 比較（不飽和），非缺口（Low） |

## 模組 → daemon 級端到端（no-op 即紅）

| 模組（正式呼叫路徑） | 端到端黑箱 | no-op 時的失敗 | 已以突變實證 |
|---|---|---|---|
| `transport_stall`（`refine_login_failure`） | `test_p7_stall.py::test_transport_stall_flip` | 不翻轉 TRANSPORT_STALL、無 META | 是（修前 Rust 紅） |
| `process/handler_posix`（`ExecPlan::reset_signals`） | `test_p7_events.py::test_handler_timeout_kills_group` | 背景 job 擋 SIGTERM，10 s 內無 `fire_timeout` | 是（修前 Rust 紅） |
| `login_fsm`／login run | `test_p7_login.py::test_platform_matrix[*]` | 不登入 → 不進 READY | 否（構造上：READY 只由 LoginOutcome::Ready 推進） |
| `session_auth` | `test_p7_login.py::test_unresolved_credentials_terminal_zero_empty_secret_tx` | 送出空帳密、無 CREDENTIALS_UNRESOLVED | 否 |
| `quiet_gate` | `test_p7_quiet_reconfirm.py::test_spontaneous_reboot_gates_agent` | submit 不回 AUTOBOOT_QUIET、送出 bytes | 否 |
| `reprobe` | `test_p7_quiet_reconfirm.py::test_attached_bootloader_probe_sets_stuck` | 無自動 reprobe → 不落 BOOTLOADER_STUCK | 否 |
| `self_test` 分類 | `test_p7_selftest_recover.py::test_selftest_classifications` | 分類／建議動作不符 | 否 |
| `recover`（force／keys） | `test_p7_selftest_recover.py::test_recover_force_*` | 錯誤碼與耗時窗不符 | 否 |
| `capture`（reader observer） | `test_p7_log_capture.py::test_capture_skips_foreground` | capture 檔無背景行或含前景輸出 | 否 |
| `event_engine`（registry／matcher／dispatcher／counter／event_log） | `test_p7_events.py` 六案 | 規則不落檔、不觸發、tail 順序不符 | 否 |
| `device_source::usb_posix`（busid） | 無（PTY 無 USB busid；真實 USB 屬 B-03 BLOCKED-OWNER） | — | — |
