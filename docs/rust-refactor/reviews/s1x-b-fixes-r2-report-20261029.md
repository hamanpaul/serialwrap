# Stage 1 出口審查 B 修正第 2 輪雲端回報（2026-10-29）

- 分支：`feature/s1x-b-fixes`（本輪唯一寫入者），起點 `2a65438`，終點 `f2a23b1`。已 push；未開 PR，未改 VERSION。
- 依據：`reviews/s1-cloud/62ffec7_2a65438-sol.md`（Codex sol 增量審查：A 段前輪 finding 全為已修，B 段 1 High＋1 Medium）、`reviews/s1x-b-fixes-report-20261029.md`（上一輪回報）、`reviews/rulings/s1.md` 的 S1X-b-1、S1X-b-2、`reviews/low-backlog.md` 的 S1X-b-X1、`reviews/review-convergence-rules.md`。報告內容只當資料使用。
- 環境：Rust 1.96.1（rust-toolchain 釘定）＋rustfmt、x86_64-pc-windows-gnu＋mingw-w64；CPython 3.12.3（`/usr/bin/python3.12` 建 venv）＋PyYAML 6、pytest 9.2.0、jsonschema 4.10.3、pyserial；policy engine 依 CLAUDE.md 釘定 SHA 安裝。測試、cargo、golden 產生器都以一般使用者 `tester`（非 root）執行。

## 1. Commit 清單（2a65438..f2a23b1）

| commit | 內容 |
|---|---|
| `9a9955d` | fix(proto)：重複鍵覆寫掉的孤立 surrogate 不再使 event add 拒送（S1-2a65438-1） |
| `f2a23b1` | fix(pyre)：樣式以單獨反斜線結尾時比照 CPython 預讀優先報 end of pattern（S1-2a65438-2、S1X-b-X1）；同 commit 補 `docs/rust-refactor/reviews/s1x-b-fix-responses.md` 第 2 輪節 |

TDD：每項都先加回歸，確認在修正前的程式上紅燈，再改實作。紅燈與修正在同一個 commit 內。

## 2. 逐項處理與回歸測試

### S1-2a65438-1（High）— 已修 `9a9955d`

- **修法**：`sw_proto::pyjson` 解析時，每個值都附帶其子樹內孤立 surrogate 的個數。
  - 物件依 key 記錄目前值的個數。重複鍵覆寫時扣掉舊值、加上新值；key 保留首次出現的那一個，兩者相等，個數也相同。
  - `loads_keep_lone_surrogates` 的旗標改成「最終值樹仍含孤立 surrogate」，比照 Python `json.load`（後值覆寫前值）之後的 `json.dumps(...).encode()`。
  - 不用「掃描最終值樹找跳脫」的做法，以免把合法內容「NUL 後接 U+FD800..」誤認成殘留的 surrogate。
  - `loads`（回應端 RPC-H-03）行為不變。
- **回歸**（期望值都取自 Python 3.12.3 實測）：
  - `sw-cli` `tests/event_add_document.rs::add_overwritten_lone_surrogate_by_duplicate_key_is_sent`：5 種文件，含報告的 `{"lone":"\ud800","lone":"ok"}`、被覆寫的巢狀物件、陣列值、雙層重複鍵、NUL＋U+FD800。斷言 rc 0 與確切請求行。修正前紅燈。
  - `sw-cli` `…::add_lone_surrogate_surviving_duplicate_key_is_rejected`：最終值仍含 surrogate 的 3 種（後值帶入、重複鍵本身含 surrogate、混合）照樣拒送。位置依最終請求行，例如 `{"a":"ok","a":"\udc80"}` 是 position 49。
  - `sw-proto` `tests/pyjson_golden.rs::keep_lone_surrogates_flag_follows_duplicate_key_overwrite`：修正前紅燈。
  - 黑箱 `tests/contract_blackbox/test_bb_event.py::test_event_add_overwritten_lone_surrogate_is_sent`（2 個參數化案例）：
    - Python 基線：10 passed。
    - Rust CLI（`SERIALWRAP_CLI_EXE=target/debug/serialwrap`）：修正前 2 failed，修正後 10 passed。
  - 上輪的拒送回歸（7 種文件、SOCKET_ERROR 優先、surrogate pair 照送）維持通過。

### S1-2a65438-2（Medium）— 已修 `f2a23b1`；S1X-b-X1（low-backlog）一併解決

**S1X-b-X1 已解決。** 請編排者把 `low-backlog.md` 該列標為已修 `f2a23b1`。它和 S1-2a65438-2 是同一個機制：CPython tokenizer 的預讀。

- **CPython 語意**：`sre_parse.Tokenizer.get()` 每取走一個 token，就呼叫 `__next()` 預讀下一個。
  - 下一個 token 是樣式結尾單獨的 `\` 時，當下拋 `bad escape (end of pattern)`，位置為 `len-1`。
  - 所以「取走倒數第二個 token 之後才判定的錯誤」都輸給它；在這之前已判定的錯誤則不受影響。
  - 解析不可能在沒讀到結尾的情況下成功，所以這類樣式一定報錯。
- **修法**（`crates/sw-proto/src/pyre/parse.rs`）：
  - `parse()` 判斷結尾是否為落單反斜線（尾端反斜線個數為奇數）。如果是，且解析成功或解析器位置已到達該反斜線，就改報 end of pattern。
  - 讓解析器「已取走」的位置與 CPython 一致，共修 6 處：
    1. unknown extension（`?X`、`?<X`、`?P` 其他）：以 token 取走（`\` 連同下一字元）；
    2. 旗標群組裡的非法字元：同樣以 token 取走；
    3. `(?P<>`：先取走 `>`，再報 missing group name；
    4. `{m,n}`：先取走 `}`，再檢查次數；
    5. `\x`／`\u`／`\U`：只取走十六進位數字（`getwhile` 語意）；
    6. nothing to repeat／multiple repeat：在取走 `?`／`+` 修飾字元前判定。
  - 錯誤訊息文字與位置都沒改。
- **語料／golden**：
  - `scripts/rust/goldens/pyre_corpus.txt` 新增第七區 25 筆：`[z-a\`、`[\d-z\`、`[z\A\`、`a\8\`、`(?P<\`、`\`、`a\\\`、`a\\`、`a{2,1}\`、`(?<\Z\`、`(?Px\`、`(?P<>\`、`(?i\0\`、`++\`、`+?\`、`a**\`、`*\`、`(?#x\`、`(?x)a#c\`、`[a\`、`a|\`、`(a)\1\`、`(?z\`、`\x4\`、`[z-a\\]`。
  - 依既有程序（`gen_pyre.py`，CPython 3.12.3，沒有版本警告）重生 `contracts/golden/pyre.tsv`：+50 列，`git diff` 沒有 `-` 行（add-only）。
  - `pyre.limits.json` 不變。
- **額外修正的錯判**：修正前 Rust 把 `(?x)a#c\` 當成合法（verbose 註解讀到結尾），Python 則報錯，`profile test` 的 ok／error 判定因此相反。已收進 golden 與 CLI 回歸。
- **回歸**：
  - `sw-proto` `tests/pyre_golden.rs::golden_compile_search_completion`：修正前 17 筆不符，修正後 0。
  - `sw-cli` `tests/profile_checks.rs::trailing_lone_backslash_error_precedes_earlier_token_errors`：9 例比對 `checks.prompt_regex` 整段 JSON，期望值取自 Python 3.12.3 `profile test`。含報告指定的 `[z-a\`、`[\d-z\`，也含不受影響的 `++\`、`[z-a\\]`。修正前紅燈。
  - 上輪的 `class_range_error_precedes_unterminated_and_later_escape` 與第六區 golden 維持通過。
  - `tests/test_rust_goldens_fresh.py` 與 `test_bb_profile.py`（Python 基線及 Rust CLI）通過。

### (3) 其餘未修的 High／Medium

無。sol 報告 A 段 6 項都是「已修」，累計未修輪數 0。

## 3. 隨機差分（CPython 3.12.3 對 Rust `pyre::compile`）

- **方法**：
  - 從 56～70 種偏重錯誤構造的原子隨機組合樣式，60% 機率在結尾附加 `\`。
  - 「字元類別」模式以 `[` 開頭；「一般」模式不限。
  - 期望值為 CPython 3.12.3 `re.compile` 的 `str(re.error)`；Rust 端以暫時 example 呼叫 `pyre::compile`（未提交）。
  - 同一組樣式分別跑修正前（`2a65438`）與修正後的 binary。
  - 分類：「結尾反斜線優先序差異」指恰有一方為 `bad escape (end of pattern)`；其餘為「其他差異」。Python ok／Rust untranslatable 不計（屬已登記殘餘）。
- **結果**（種子 5～12，每種子字元類別／一般各 5000 例，共 80,000 例）：

| | 修正前 | 修正後 |
|---|---|---|
| 結尾反斜線優先序差異 | 10,580 | **0** |
| 其他差異 | 17,389 | 17,389（樣式集合相同，無新增、無減少） |

- 開發期另跑種子 1～4 × 2000 × 2：最終版同樣為 0。
- 「其他差異」都是既有的訊息文字差異，與結尾反斜線無關，見 §6。

## 4. 測試數字

| 檢查 | 結果 |
|---|---|
| `cargo test -p sw-proto -p sw-cli --features sw-cli/test-fakes` | 起點 715；`9a9955d` 718 passed／0 failed／3 ignored；`f2a23b1` 719 passed／0 failed／3 ignored |
| `cargo test --workspace --features sw-cli/test-fakes,sw-pal/test-fakes`（`f2a23b1`） | 914 passed／0 failed／7 ignored |
| `cargo check --workspace --target x86_64-pc-windows-gnu` | 每個 commit 都 rc 0 |
| 黑箱（Rust CLI）`test_bb_event.py`、`test_bb_profile.py`、`test_bb_rpc_params.py` | 71 passed |
| `python3 -m tools.contracts check` | 每個 commit 都 ok |
| `python3 -m policy_check --repo .` | 每個 commit 都 rc 0。只有既有 WARN：R-19、R-22（2883 筆既有懸空引用） |
| `python3 scripts/rust/check_cfg_boundary.py` | 每個 commit 都 rc 0；沒有新增 platform cfg |

全套 `python3 -m pytest -q tests/`：非 root、序列執行，一次只跑一套，各在獨立 worktree。

| 樹 | 結果 |
|---|---|
| 起點 `2a65438` | 3 failed、5297 passed、161 skipped、1 xfailed（30:33） |
| `9a9955d` | 3 failed、5299 passed、161 skipped、1 xfailed（31:40） |
| `f2a23b1` | 3 failed、5299 passed、161 skipped、1 xfailed（32:15） |

三棵樹的失敗集合相同，就是上一輪記錄的起點基準 3 項：

- `test_l1_judge.py::test_l1_lock_nodeids_subset_of_collect_only`
- `test_rust_bench_tools.py::test_cargo_workspace_tests_windows_native`
- `test_rust_stage1b_registration.py::test_p4_required_cases_have_passing_evidence`

本輪沒有新增失敗。passed 多出的 2 例是新增的黑箱參數化案例。

## 5. 不成立主張

無。兩項 finding 的重現與修法都成立；報告指定的回歸（`[z-a\`、`[\d-z\` 的 golden 與 CLI、重複鍵文件與巢狀覆寫、最終值仍含 surrogate 的拒送）都已加上。

## 6. 範圍外新觀察（未處理，請編排者裁決）

pyre 有既有的錯誤訊息差異，與結尾反斜線無關。上表 17,389 例在修正前的 binary 同樣不一致，不是本輪引入。詳細分類見分支上的 `s1x-b-fix-responses.md` 第 2 輪節。主要類別：

| 例數 | Python 3.12.3 | Rust | 例 |
|---|---|---|---|
| 11,107 | `incomplete escape \x4`／`\u00`（已讀到的原文） | `incomplete escape \x`（固定文字） | `\x4` |
| 2,688 | `unknown extension ?<\Z`、`?Px` | `unknown extension ?<\`、`?P` | `(?Px` |
| 2,120 | `missing }, unterminated name`／`missing character name` | `missing } in character name` | `\N{` |
| 519 | `missing ), unterminated name`／`missing group name` | `missing ), unterminated name reference` | `(?P=` |
| 503 | `missing >, unterminated name`（位置不同）／`missing group name` | `missing >, unterminated name` at 0 | `(?P<` |
| 238 | `unexpected end of pattern` | `unknown extension ?` | `(?` |
| 151 | `bad character in group name` | `unknown group name` | `(?P=,` |
| 22 | `cannot refer to an open group` | `cannot refer to group defined in the same lookbehind subpattern` | `(?<=(?(1)` |

- 影響面同 S1X-b-1：`profile test` 的 `checks.<regex>.error` 文字。差分中 ok／err 的判定一致，只差訊息或位置。
- 依收斂規則 §6，建議記入 low-backlog，或另立 finding。修法分散在各跳脫與群組擴充的讀取點。

## 7. OWNER-BATCH

無。兩項修正都沒有改 SC／BC／DF、凍結鎖或 oracle 語意：

- golden 只新增列，`pyre.limits.json` 不變；
- RPC-H-03 回應端行為不變。

## 8. 未完成項

- 依指示未處理 Low（含審查 B 的 S1X-b-3～7）。
- §6 的範圍外觀察未處理，待裁決。
- 全套 pytest 的 3 項起點基準失敗不屬本分支範圍，未處理。
- 沒有在 Windows 原生環境執行，只做了 `cargo check` 交叉檢查。
- 差分工具（暫時的 cargo example 與 Python 產生器）沒有提交。
