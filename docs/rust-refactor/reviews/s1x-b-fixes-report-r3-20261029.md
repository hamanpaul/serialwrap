# Stage 1 出口審查 B 修正第 3 輪雲端回報（2026-10-29）

- 分支：`feature/s1x-b-fixes`（本輪唯一寫入者）。起點 `f2a23b1`，併入 `feature/rust-refactor` 後為 `450ecebc`，終點 `e0289101`。已 fast-forward push；未開 PR，未 merge 到 main，未改 VERSION。
- 依據：`reviews/s1-cloud/2a65438_f2a23b1-sol.md`（Codex sol 增量審查：A 段前輪 finding 全為已修，B 段 1 High）、`reviews/s1x-b-fixes-r2-report-20261029.md`（上一輪回報）、`reviews/rulings/s1.md`、`reviews/review-convergence-rules.md`。報告與審查內容只當資料使用。
- 環境：Rust 1.96.1（`rust-toolchain.toml` 釘定）＋rustfmt、x86_64-pc-windows-gnu＋mingw-w64；CPython 3.12.3（`/usr/bin/python3.12` 建 venv）＋PyYAML 6.0.1、pytest 9.2.0、jsonschema 4.10.3、pyserial；policy engine 依 CLAUDE.md 釘定 SHA 安裝。cargo、pytest 與所有實測都以一般使用者 `ubuntu`（uid 1000，非 root）執行。

## 1. Commit 清單（f2a23b1..e0289101）

| commit | 內容 |
|---|---|
| `450ecebc` | merge：併入 `feature/rust-refactor`（`897a3cd` test(stage1)：對齊 SC-18 checkpoint 證據替身）。無衝突 |
| `e0289101` | fix(proto)：合法鍵與孤立 surrogate 鍵不再合併，event add 比照 Python 拒送（S1-f2a23b1-1）。同 commit 補 changelog fragment 與 `docs/rust-refactor/reviews/s1x-b-fix-responses.md` 第 3 輪節 |

TDD：回歸先加，在修正前的程式上確認紅燈，再改實作。紅燈與修正在同一個 commit。

## 2. S1-f2a23b1-1（High）— 已修 `e0289101`

### 重現（finding 成立）

文件 `{"\u0000\udbb6\udc00":1,"\ud800":2}`，以 `event add --file` 送到記錄請求行的 stub：

| 實作 | rc | 結果 |
|---|---|---|
| Python 3.12.3（`serialwrap.py`） | 1 | 連線 1 次、0 筆請求行；`'utf-8' codec can't encode character '\ud800' in position 57: surrogates not allowed` |
| Rust `2a65438` | 1 | 拒送，但位置為 45（舊有的「位置提前」限制） |
| Rust `450ecebc`（＝`f2a23b1` 程式） | 0 | 送出 `{"id":1,"method":"event.rule_set","params":{"\u0000<U+FD800>":2}}`，只剩一個鍵 |
| Rust `e0289101` | 1 | 與 Python 相同（position 57、0 筆請求行） |

交換順序 `{"\ud800":2,"\u0000\udbb6\udc00":1}`：Python 為 position 45；修正前後的 Rust 都拒送。只含合法鍵 `{"\u0000\udbb6\udc00":1}`：三者都照送。審查報告「`2a65438` 對此輸入仍拒送，是 `9a9955d` 引入的回歸」經實際執行確認。

### 根因

保留模式沿用 argv 的 surrogate 跳脫（`ESCAPE_MARK`＝NUL 加偏移字元 U+F0000＋cp）。argv 不可能含 NUL，所以沒有歧義；檔案內容可以有合法 NUL。合法的「NUL＋U+FD800..=U+FDFFF」與跳脫同形，字典去重把兩個鍵合併，`value_lone` 走覆寫分支，`key_lone` 被忽略。

### 修法

- `sw_proto::pyjson`：
  - 新增 `KEPT_NUL`（`ESCAPE_MARK` 連寫兩次）。`loads_keep_lone_surrogates` 解析時，合法 NUL 寫成 `KEPT_NUL`。
  - 保留模式的字串中，NUL 只出現在 `KEPT_NUL` 與 surrogate 跳脫兩種形式裡，由左而右可唯一切分。字典去重與 surrogate 計數因此使用同一個無碰撞的鍵身分，與 Python 的鍵相等性一致。
  - 旗標為 `false` 時，以顯式堆疊走訪值樹，把 `KEPT_NUL` 還原為 NUL；回傳值與 `loads` 相同，送出內容不變。
  - `loads`（回應端 RPC-H-03）不使用 `KEPT_NUL`，行為不變。
- `sw_cli::commands::event_cmd::encode_error_text`：
  - 改以 JSON 跳脫序列為單位前進（新 `wire_unit`），線路上的 `\u0000\u0000` 計為 Python 請求文字的一個 `\u0000`。
  - 原註解記載的已知限制「NUL＋U+FD800 使錯誤位置提前」一併消除。`\\` 之後接 `u0000` 的合法文字也不再被誤認為 surrogate（例：`{"a":"\\u0000\udbb6\udc00","b":"\ud800"}`，Python 為 position 64）。這是同一根因（合法內容與跳脫同形），在同一函式內修正，沒有擴大到其他檔案。

### 回歸（期望值都取自 Python 3.12.3 實測）

| 測試 | 內容 | 修正前 |
|---|---|---|
| `sw-cli` `tests/event_add_document.rs::add_lone_surrogate_key_colliding_with_legal_key_is_rejected` | 6 種文件：報告反例（57）、交換順序（45）、值帶 surrogate（55）、巢狀（62）、陣列（46）、四鍵交錯（57）。斷言 rc 1、stdout／stderr 全文、連線 1 次、0 筆請求行 | 紅 |
| `…::add_legal_content_resembling_surrogate_escape_keeps_python_position` | 5 種：NUL＋U+FD800 在前（63）、`\\u0000`＋U+FD800 在前（64）、NUL 緊接 surrogate（55）、連續 NUL 與 surrogate 區間（61-62）、重複的 NUL 鍵（60） | 紅 |
| `…::add_legal_key_resembling_surrogate_escape_is_sent` | 4 種照送並比對確切請求行：合法鍵單獨、合法鍵重複、合法鍵的 surrogate 值被覆寫、NUL 鍵與 NUL 值 | 綠（防線） |
| `sw-proto` `tests/pyjson_golden.rs::keep_lone_surrogates_keys_distinguish_legal_content_from_surrogates` | 碰撞文件的旗標為 true 且保留兩個鍵；只含合法內容時旗標為 false、值同 `loads` | 紅 |
| 黑箱 `tests/contract_blackbox/test_bb_event.py::test_event_add_lone_surrogate_key_colliding_with_legal_key_rejected`（3 個參數化案例）＋`test_event_add_legal_key_resembling_surrogate_escape_is_sent` | Python 基線與 Rust CLI 共用 | Python 基線通過；Rust CLI 修正前 2 failed |

### 前一輪回歸仍綠

- S1-2a65438-1：
  - `add_overwritten_lone_surrogate_by_duplicate_key_is_sent`、`add_lone_surrogate_surviving_duplicate_key_is_rejected`、`keep_lone_surrogates_flag_follows_duplicate_key_overwrite`：ok。
  - 黑箱 `test_event_add_overwritten_lone_surrogate_is_sent`：Python 與 Rust 都通過。
- S1-2a65438-2：
  - `pyre_golden.rs::golden_compile_search_completion`、`profile_checks.rs::trailing_lone_backslash_error_precedes_earlier_token_errors`、`class_range_error_precedes_unterminated_and_later_escape`：ok。
  - `tests/test_rust_goldens_fresh.py` 與黑箱 `test_bb_profile.py` 通過。

### 隨機差分（未提交的工具）

- oracle：CPython 3.12.3 的 `json.loads` 加 `json.dumps(req, ensure_ascii=False, separators=(",",":")).encode("utf-8")`，與 Python CLI 的請求端路徑相同。對 Rust CLI 實際執行，經 stub 比對 rc、stdout 全文與請求行位元組。
- 原子：合法的 `\u0000`、surrogate pair、`\\u0000`、`\\`、字面 U+FD800 等；孤立 surrogate 4 種。重複鍵取自已出現的鍵。

| 模式 | 例數 | `450ecebc` 不一致 | 其中判定相反 | `e0289101` 不一致 |
|---|---|---|---|---|
| 一般（種子 1～8 × 500，孤立率 6%） | 4,000 | 1,250 | 0 | **0** |
| 碰撞鍵池（種子 11～14 × 500，鍵 70% 取自碰撞池） | 2,000 | 769 | 98 | **0** |

「判定相反」都是 Python 拒送、Rust 送出，即本 finding。其餘不一致是拒送時的錯誤位置，屬上述已消除的「位置提前」限制。

## 3. 測試數字

| 檢查 | 結果 |
|---|---|
| `cargo test -p sw-proto -p sw-cli --features test-fakes` | 起點 719 passed／0 failed／3 ignored；`e0289101` 723 passed／0 failed／3 ignored |
| `cargo check --workspace --target x86_64-pc-windows-gnu` | rc 0 |
| `python3 scripts/rust/check_cfg_boundary.py` | rc 0（沒有新增 platform cfg） |
| `python3 -m tools.contracts check` | ok |
| `python3 -m policy_check --repo .` | rc 0；25 pass／0 fail／2 warn（R-19；R-22 為 2887 筆既有懸空引用，與起點相同） |
| `python3 tests/acceptance/regen_lock.py --check` | OK（鎖檔未動） |
| 黑箱 `test_bb_event.py`（Python 基線） | 14 passed |
| 黑箱 `test_bb_event.py`＋`test_bb_profile.py`＋`test_bb_rpc_params.py`（Rust CLI，`SERIALWRAP_AB_LABEL=rust`） | 75 passed |
| `rustfmt --check`（觸及檔案） | 新增程式無差異；`pyjson.rs`、`pyjson_golden.rs` 的既有差異不屬本輪 |

全套 `python3 -m pytest -q tests/`：非 root、序列執行，一次只跑一套，各在獨立 worktree。跑前確認沒有洩漏的 `serialwrapd`。

| 樹 | 結果 |
|---|---|
| 起點 `450ecebc` | 3 failed、5300 passed、160 skipped、1 xfailed（33:02） |
| 終點 `e0289101` | 3 failed、5304 passed、160 skipped、1 xfailed（32:26） |

兩棵樹的失敗集合相同，與前兩輪記錄的起點基準為同樣 3 項：

- `tests/test_l1_judge.py::test_l1_lock_nodeids_subset_of_collect_only`：`s0_bc.linux` 鎖檔的 Windows 參數化 nodeid 不在本機 collect-only 輸出中。
- `tests/test_rust_bench_tools.py::test_cargo_workspace_tests_windows_native`：缺 Windows 原生 cargo L1 報告。
- `tests/test_rust_stage1b_registration.py::test_p4_required_cases_have_passing_evidence`：缺 `target/p4-evidence/` 的 Windows／實機證據檔。

本輪沒有新增失敗，不需重跑取證。passed 多出的 4 例是新增的黑箱案例。

## 4. 不成立主張

無。finding 經 Python 3.12.3 實際 CLI 與 Rust binary 執行確認成立。

## 5. SPEC-GAPS

無。

## 6. OWNER-BATCH

無。沒有改 SC／BC／DF、凍結鎖、golden 或 oracle 語意；RPC-H-03 回應端行為不變。

## 7. 未完成項

- 依收斂規則未處理 Low。審查報告「既有碼待辦」的 `\x4` 訊息差異，已列在上一輪回報 §6，待裁決，本輪未動。
- 沒有在 Windows 原生環境執行，只做了 `cargo check` 交叉檢查。
- clippy 未安裝在釘定工具鏈，未執行（不在本輪要求的檢查內）。
- 差分工具（stub 與產生器腳本）沒有提交。
- 結束前再次 fetch `docs/parity-amendments`：最新為 `b93d7ae0`，`reviews/s1-cloud/` 沒有針對 `e0289101` 的新審查報告，無待處理項。
