> 審查者：claude-fable-5-1（重型任務，擁有者 2026-11-07 指定）

# feature/rust-refactor-p8 增量審查：4f0471c8..e5eb4cda（WP5-6 第 6／6 項）

審查者：claude-fable-5-1（重型任務，擁有者 2026-11-07 指定）。工作樹 detach 於 e5eb4cda，未修改任何檔案。

## A 前輪 finding 核對

無（編排者提供的前輪報告為空）。

## B 新發現

NO FINDINGS（High／Medium）。

核對內容與證據：

- **aa606e6f（RPC 端到端測試）**：`crates/sw-daemon/tests/dispatch.rs:3618-3966` 新增 `wp56_rpc_async_reboot_evidence_wait`，純新增、未改動既有斷言。八個階段的預期回應逐一對照 Python 基線確認形狀與鍵序一致：reboot 完成後 `clear_boot_quiet()`＋`begin_boot_evidence_wait()`（`sw_core/session_manager.py:6457-6459`）→ `boot_evidence_wait_remaining_s=60.0`、`ready_reconfirm_remaining_s=300.0`（`READY_RECONFIRM_MAX_S = 180+120`）；pending-only 拒絕 `retry_after_s=5.0`＋hint 字面值（`service.py:829-838`）；self-test 於等待中走 `boot_quiet_active() or boot_evidence_wait_active()` 分支回 `AUTOBOOT_QUIET`／`wait`＋4 個 lease context 鍵＋外層 `command_capable`（`:7600-7617`、`:7816-7829`），Rust 端 `session_core.rs:5826-5829`／`:5492-5501` 同序；banner 先 `note_boot_evidence()` 再 `arm_boot_quiet()` 且重臂不延長 reconfirm deadline（`:4560-4564`、`:684-689`），Rust `quiet_gate.rs:188-193` 相同，測試斷言 banner 後 `boot_quiet_remaining_s=180.0`、`ready_reconfirm_remaining_s=295.0`（未被推回 300）正確反映此語意。確認時刻後取實測的 9 個動態欄位皆為時間戳／年齡／RX 統計，另以 `assert_ne!(…, WP56_EPOCH)` 強制 `last_ready_at`／`last_tx_at`／`last_probe_at` 已更新，不是空泛放行。changelog fragment `changelog.d/243-wp56-boot-evidence.md` 為 direct child `.md`。
- **db61edee（golden 刷新）**：以腳本獨立重新擷取（`python3 -I scripts/rust/capture_p7_goldens.py --out <tmp>`，rc=0）比對，`gate_responses.ndjson` 與現行 Python 基線逐位元組相同；列數 32→32 未刪案例；以程式逐列核對 24 列變動，移除 `boot_evidence_wait_remaining_s` 鍵後新舊列完全相等，新鍵值一律 `null`，位置分別為 `public_session`（sort_keys）的 `attached_real_path`→`boot_quiet_remaining_s` 之間（24 處）與 response 內嵌 session 的 `ready_reconfirm_remaining_s`→`idle_for_ms` 之間（14 處），與 commit 訊息所述一致。比較器 `crates/sw-core/tests/common/p7.rs::golden_rows` 無正規化，`gate_responses_are_byte_identical_to_python` 實跑通過。
- **e5eb4cda（L1 鎖）**：`regen_lock.py --check --base 4f0471c8` 通過，移除 0 筆；以程式比對鎖中 24 筆 wp56 nodeid 與 crates 內帶 `#[test]` 的 24 個 `wp56_*` 函式一一對應（無漏登、無幽靈節點），s21_core／linux 與 p7_gate:cargo-test-workspace／linux 各 24 筆。
- **定向實測**（`CARGO_TARGET_DIR=/tmp/solrev-p8-rust-refactor-p8-target`）：`dispatch wp56_` 2 passed；`quiet_gate_p7` 10 passed；`session_core public_dict_key_order_with_sc04_fields` passed；`recover_p7` 13 passed、`capture_p7` 14 passed。
- **裁決條件**：s2 裁決「對照 Python 例外擴大」要求同步更新鍵序文件——第 2 項 3ec29f1c 已更新 `docs/rust-refactor/inventory/02-rpc.md` §6.1 `SessionPublic` 表，條件已滿足。

Low（不擋放行）：

- P8-e5eb4cd-1 [Low] crates/sw-daemon/tests/dispatch.rs:3445 — `wp56_advance_secs` 以每秒 `advance(1 s)`＋固定 `sleep(25 ms)` 讓 actor 逐一處理 Tick；高負載下相鄰 advance 可能合併成一個 Tick，等待期間的 reprobe_tick 評估次數少於 5 次。因 session 欄位由 FakeClock 投影、零 TX 與 attempts 斷言不依賴 Tick 次數，且 RX 已閒置 ≥3 s 後至少一個 Tick 必會觸發（guard 若被拿掉仍會送 probe 而轉紅），不會假綠，只是覆蓋強度隨負載波動。建議改以可觀察條件（例如 actor 已處理的 tick 計數或 `next_reprobe_at` 推進）等待，而非時間。

## 既有碼待辦（不計入結論）

- **三個 p7 golden 仍過期（本包獲准範圍外）**：`contracts/golden/p7/self_test_responses.ndjson`（29 列）、`recover_responses.ndjson`（17 列）、`log_responses.ndjson`（5 列）與現行 Python 基線不同；以程式核對三檔差異**全部**僅為 session 物件新增 `boot_evidence_wait_remaining_s: null`，無其他變動。後果：`tests/test_p7_goldens_fresh.py::test_goldens_exist_and_are_fresh`（登記於 `p7_gate:python3-m-pytest-q-tests`／linux）在本包前後皆 FAIL，本包只是把失敗點從 `gate_responses` 推到 `self_test_responses`。Rust 端 `recover_p7`／`capture_p7` 的位元組比對直接取 golden 內的 session dict 組回應，故不受影響；`self_test_responses` 無 Rust 消費者。刷新為純機械動作、不涉及比較器與分母，建議編排者直接授權以 `capture_p7_goldens.py` 一次刷新三檔。
- 第 5 項審查已記的 Python clear 在 `bridge is None` 時直接轉 `ATTACHING`／`DETACHED` 與 Rust 先進 `Recovering` 的時序差異，仍歸 wp58。

## 對裁決的異議（不計入結論）

無。

結論：可放行
