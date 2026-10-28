# feature/rust-refactor-p5 9b81d9a..c66f65e 非實作者審查（Claude Opus，雲端）

審查日期：2026-10-28。範圍：`git log 9b81d9a..origin/feature/rust-refactor-p5`，共 4 個 commit，沒有 merge commit。
驗證環境：worktree 位於 `c66f65e`，`cargo build --release -p sw-daemon -p sw-cli`，Python 3.12。
- `cargo test -p sw-core --features test-fakes --test state_store --test session_manager`：57＋31 passed。
- `tests/test_slice21_gate_judge.py`、`func-test/tests/test_s21_actions.py`：28 passed、1 skipped。
- `tests/contract_blackbox/slice21/test_state_file_compat.py`，以 release 版 Rust daemon／CLI 執行（`SERIALWRAP_AB_LABEL=rust`）：10 passed。
- 另寫了臨時 A/B 探針（只在本地執行，已刪除、沒有 commit），結果見 4f4082f#1。

凍結鎖 `l1_required.lock.json` 只新增 3 筆（s21_core.linux），沒有移除。本範圍沒有新增 platform cfg（`CARGO_MANIFEST_DIR` 收斂到 `tests/common/mod.rs`，`check_cfg_boundary` 由實作者記錄 rc 0）。

### 4f4082f fix(sw-core): state.json 孤立 surrogate 比照 Python 啟動失敗；F28 manager 淘汰接線回歸；gate 承接率依斷路器判定

1. [Low] `crates/sw-core/src/state_store.rs:257-276`（`SURROGATE_MARK_BASE`）、`crates/sw-core/src/session_manager.rs:1677-1684`
   - 問題：孤立 surrogate 以 U+10F800 起的標記承載。啟動存檔時以 `serialize_state`（`sort_keys`，Rust 的 code point 序）組 payload，再找出第一個標記的位置。Python 的 `json.dumps(sort_keys=True)` 把 `\ud800` 當成 0xD800 排序，排在 U+E000..U+10FFFF 的鍵之前；Rust 的標記則排在這些鍵之後。孤立 surrogate 出現在 dict 鍵、且同層有 ≥ U+E000 的兄弟鍵時，`UnicodeEncodeError` 的 `position N` 會和 Python 不同。
   - 實測：輸入 `{"<sect>": {"t:\uff21": "v1", "t:\ud800": "v2"}}`（以 `json.dumps` ASCII 跳脫寫入）。`<sect>`＝`bindings` 時，Python 末行為 `… '\ud800' in position 106 …`，Rust 為 `position 117`。`profile_pins`、`profile_detected` 兩個 section 同樣不符（Rust 分別為 157、139），3/3 FAIL。rc 1、traceback 首行、原檔不動這幾點都一致；只有末行的位置不同。released／aliases 路徑不受影響，因為 alias 會先在 aliases 段帶出該 sid。
   - 修法：只在 `lone_surrogate_encode_error` 路徑使用與 Python 同序的序列化。在 `pyjson::dumps_dict` 加一個可選的 key 比較器，或在 state_store 內組一個鍵已依 Python 序重排的 `PyDict`：比較鍵時，逐字元把 `marked_surrogate(c)` 映回 `0xD800..` 再比，其餘字元用 `c as u32`。然後以這份 payload 計算 position。另在 `test_state_file_compat.py` 補參數化案例：`bindings`／`profile_pins`／`profile_detected` 三個 section，兩個鍵 `t:\uff21`、`t:\ud800`，以 Python 末行作為對照。

   其餘部分正確：UTF-8／16／32 的 surrogatepass 解碼與錯誤位置（含 BOM 位移、UTF-32 的 `S-E` 範圍）、`\uXXXX` 的配對規則與 `py_scanstring` 一致（high 後面必須緊接 low 的 `\u` 才合併）。改寫後解析失敗時，會以原文重新解析來取得 Python 的錯誤訊息。未知鍵內的 surrogate 在存檔時被丟棄，因此正常啟動，這點也和 Python 相同（黑箱案例通過）。commit 失敗的 EXCEPTION message 改用 `py_message`，不再輸出 Debug 結構。`with_filename` 只在有 errno／winerror 時附上路徑，與 `str(OSError)` 一致。F28 淘汰案例走真正的 manager 路徑（actor register → `ManagerNote::EvictCapture` → owner session），斷言只淘汰最舊一筆、新完成的保留。judge 的變更都是收緊：缺 `section_verdict` 判 EVIDENCE_MISSING，斷路器 tripped／OD-02 判 FAIL，`OWNER-DECISION-PENDING` 必須對應 `pending_decisions`。

### 2be8982 docs(rust-refactor): P5-T20 以 release artifact 重跑黑箱 A/B 並交付逐 nodeid 報告（F103）

1. [Medium] `docs/rust-refactor/reviews/p5-t20-blackbox-report.md`（失敗案例分類表 `test_f2_backpressure.py::test_recovery_flushes_queue` 列、結論段）、`tests/acceptance/slice21_gate_expect.yaml:67-69`（PA-OD-T20）
   - 問題：報告自己的探針 5/5 實測顯示，recover 時 Python 讓前景 `sleep 30` 完成後，arbiter 先送出 `echo q0`，只有 q1～q4 是 `FLUSHED_BY_RECOVERY`；Rust 則是 5 筆全部 `FLUSHED_BY_RECOVERY`，Ctrl-C 後零 TX。這是外部可觀察的 wire 與命令終態差異。報告和 commit 訊息卻寫「無 Rust 對 Python 的待修差異」「Rust 維持較安全行為」，並把它歸為「Python 基線缺陷候選」，和其他 Python／Rust 一致的案例併在 PA-OD-T20 一筆。依 1:1（功能面）原則，Rust 偏離 Python 必須先有擁有者核准的 DF／SC 登記；否則 Rust 應重現 Python 行為，或者提出證據證明這是 Python 競態允許的結果之一（例如在 Python 上實測得到 5 筆全 flush 的交錯）。目前三者都沒有，parity 狀態容易被讀成「無差異」。
   - 修法：(a) 把報告結論改為「1 案 Rust 與 Python 外部可觀察行為不同（`test_recovery_flushes_queue`：Python 送出 q0、4 筆 flushed；Rust 5 筆 flushed、零 TX）」。(b) 在 `slice21_gate_expect.yaml` 的 `pending_decisions` 另立 `PA-OD-T20-Q0`，選項寫明「登記 DF（`contracts/behavior_changes.yaml`）並由擁有者核准」或「Rust 重現 Python 的先送 q0 行為」。(c) 擁有者裁決前，該案不得計入「無差異」，承接率／parity 列也不得以此案記 PASS。若要主張這屬於 Python 競態的允許結果，須附上 Python 側能出現全 flush 交錯的實測或程式路徑分析（`sw_core/arbiter.py`／`session_manager.py` 的 recover flush 與 worker 取件順序）。
2. [Low] 同報告 `test_reject_paths_zero_tx[SESSION_NOT_READY-ATTACHING]` 列
   - 問題：探針顯示 Python 在 attach RPC 回覆**之前**送出 `\n`＋`echo __READY__`，Rust 則在回覆之後約 40 ms 才送。報告的結論是「非 Rust 對 Python 的行為差異」。但「RPC 回覆」與「wire TX」的先後是 client 可觀察的順序，報告沒有說明這個順序是否屬於契約（例如 attach 回覆時 probe 是否已送出）。
   - 修法：在該列記錄這個順序差異，並查核 Python `session.attach` 回覆前是否同步送出 probe（`sw_core/session_manager.py` 的 attach 路徑）。若屬契約，就列為 parity 差異並登記；若不屬契約，寫明理由與依據（規格條目），再交給 PA-OD-T20 裁決。

### 6636694 test(l15): P5-T21 L1.5 A/B 實跑；補 list_contains 斷言、reboot 情境依 #162 等待再確認、gate 證據收集模式

NO FINDINGS。
- `list_contains` 是新增的斷言，非 list 值不算命中，有測試。兩個 exclusive-conflict 情境在原 expect 上追加 `pids` 斷言，是加強而不是削弱；把註解改成「單點取樣」也如實說明了 runner 的能力限制（gaps #9）。
- `s21-reboot-minimal-guard` 在 `echo AFTER` 之前等待 `ready_reconfirm_pending == false`。依 #162，Python 在再確認之前同樣回 `AUTOBOOT_QUIET`，原本的「reboot 後零 TX／payload 恰一次」斷言都保留。Rust 若不回 READY 會在這一步逾時 FAIL，並沒有被掩蓋，且已登記於 PA-OD-REPROBE。
- `--evidence-run` 只用於收集證據，judge 依 `event_latency_frozen: false` 一律判 INCOMPLETE，有測試。`l15_ab` 在 pytest 全綠但 `PA-OD-L15-*` 未結時判 EVIDENCE_MISSING；yaml 讀取失敗時退回非空字串，同樣不放行。

### c66f65e fix(sw-daemon): 測試支援不再依賴 cwd；gate Rust 黑箱帶 Rust CLI；T22 gate 非 dry-run 實跑報告與校準更新

NO FINDINGS。`data_root()` 以 `SW_TEST_DATA_DIR`／`CARGO_MANIFEST_DIR` 定位，不改測試斷言。`wait_ready` 先 canonicalize 再比對 fd readlink，屬測試輔助的修正。gate 的 Rust 黑箱 section 補上 `SERIALWRAP_CLI_EXE="$RUST_CLI"`，和 conftest 的 artifact 身分檢查一致（本審查以 `SERIALWRAP_AB_LABEL=rust`＋Rust CLI 實跑同一個 conftest 通過）。parity `#6`／`#71` 加上 `decision: PA-OD-1`，該 id 存在於 `pending_decisions`（gate_expect.yaml:46）。另外記一點：gate_expect 已寫「OD-21 已核准 SC-37，待寫入 behavior_changes.yaml 後解除」，請在寫入後同步把這兩列改為對應狀態，不要長期停在 PENDING。

## High／Medium 摘要表

| 嚴重度 | commit | 位置 | 摘要 |
|---|---|---|---|
| Medium | 2be8982 | `docs/rust-refactor/reviews/p5-t20-blackbox-report.md`（`test_recovery_flushes_queue` 列、結論）、`tests/acceptance/slice21_gate_expect.yaml:67-69` | recover 時 Python 送出 q0（4 筆 flushed），Rust 5 筆全 flushed、零 TX。這是外部可觀察差異，卻被寫成「無 Rust 對 Python 的待修差異」。修法：另立 `PA-OD-T20-Q0`，由擁有者裁決登記 DF 或要求 Rust 重現 Python 行為；裁決前不得記為無差異／PASS。 |

High：0。Medium：1。Low：2（4f4082f#1、2be8982#2）。
