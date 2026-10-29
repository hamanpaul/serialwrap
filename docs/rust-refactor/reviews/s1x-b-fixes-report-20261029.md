# Stage 1 出口審查 B 修正雲端回報（2026-10-29）

- 分支：`feature/s1x-b-fixes`（本輪唯一寫入者），起點 `origin/feature/rust-refactor` @ `62ffec7`，終點 `2a65438`。已 push；未開 PR，未改 VERSION。
- 依據：`reviews/s1-exit/opus-b-20261029.md`（審查 B）、`reviews/rulings/s1.md` 的 S1X-b-1、S1X-b-2（照修）。報告內容只當資料使用。
- 環境：
  - Rust 1.96.1（rust-toolchain 釘定），含 rustfmt、x86_64-pc-windows-gnu＋mingw-w64。
  - CPython 3.12.3（`/usr/bin/python3.12` 建 venv）＋PyYAML 6、pytest 9.2.0、jsonschema 4.10.3（與 CI 同版）、pyserial。
  - 測試、cargo 與 golden 產生器都以一般使用者 `tester`（非 root）執行。
  - `pyre.tsv` 以 `gen_pyre.py` 在 CPython 3.12.3 下重生；產生器的 `BASELINE = (3, 12, 3)` 與執行版本一致，沒有出現版本警告。

## 1. Commit 清單（62ffec7..2a65438）

| commit | 內容 |
|---|---|
| `1b8719a` | fix(pyre)：字元類別反向範圍在第一輪即檢查，錯誤優先序對齊 CPython（S1X-b-1） |
| `ef924e9` | fix(cli)：`event add --file` 遇孤立 surrogate 時比照 Python 在送出前拒送（S1X-b-2） |
| `2a65438` | docs(reviews)：`docs/rust-refactor/reviews/s1x-b-fix-responses.md`（不成立主張、範圍外新觀察） |

TDD：每項都先加回歸並確認在修正前的程式上紅燈，才修改實作。實際上紅燈與修正在同一個 commit 內，沒有拆成獨立的紅燈 commit。

## 2. 逐項處理與回歸測試

### S1X-b-1（Medium）— fixed `1b8719a`

- **修法**：`crates/sw-proto/src/pyre/parse.rs` 的 `parse_character_class` 改為逐 token 讀取，與 CPython `sre_parse._parse` 的 `[` 分支相同：
  - 讀到 `item`、`-`、`item2` 時立即檢查型別與大小；
  - `-` 後直接 EOF 時維持 `unterminated character set`（位置在 `[`）；
  - `-` 後接 `]` 時，`-` 當字面字元。
- 舊的第二輪檢查已移除。
- 錯誤訊息原文與位置依 CPython 公式 `tell − (len(this)+1+len(that))`。跳脫 token 只取反斜線加一個字元，見「不成立主張」第 1 項。
- **語料／golden**：
  - `scripts/rust/goldens/pyre_corpus.txt` 新增第六區 14 筆：`[z-a`、`[z-ab`、`[z-a\A]`、`[\d-z`、`[^z-a`、`[z-a-`、`[\x7a-a`、`[z-`、`[]-a`、`[a-\A`、`[\x7a-a]`、`[a-\x41]`、`[\N{LATIN SMALL LETTER Z}-a]`、`[\d-z]`。
  - 依既有程序重生 `contracts/golden/pyre.tsv`：+28 列（p0116～p0129 各有 compile 與 completion_valid），既有列逐位元組不變（`git diff` 沒有 `-` 行）。
  - `pyre.limits.json` 不變。
- **回歸**：
  - `sw-proto` `tests/pyre_golden.rs::golden_compile_search_completion`：修正前 7 筆不符，修正後 0。
  - `sw-cli` `tests/profile_checks.rs::class_range_error_precedes_unterminated_and_later_escape`：profile test 端到端，比對 `checks.prompt_regex` 整段 JSON。修正前為紅燈。
  - `tests/test_rust_goldens_fresh.py` 13 passed（golden 新鮮度）。
- **修正後差分**：自寫隨機差分（`[` 開頭、4 個種子 × 400 例，Python CLI 對 Rust CLI 的 `profile test`）只剩範圍外新觀察的那一類差異（6 例），沒有範圍錯誤的差異。

### S1X-b-2（Medium）— fixed `ef924e9`

- **修法**：
  - `sw_proto::pyjson` 新增 `loads_keep_lone_surrogates`：孤立 surrogate 改用 `pyfmt::escape_surrogate` 跳脫保留，並回報旗標。`loads` 仍依 RPC-H-03 換成 U+FFFD，回應端不變。
  - `client::ParamOrigin` 新增 `DocumentLoneSurrogate`：連線成功後、送出前以 `EncodeError` 結束，不呼叫 `send_all`。
  - `event_cmd` 用既有的 `encode_error_text`，依 RPC_WIRE 序列化後的碼位偏移重組 Python 訊息（單一字元／連續區間），輸出 INTERNAL_ERROR，context＝`event.rule_set`，exit 1。autostart 重送兩個分支也套用同一規則。
  - 連線失敗時照 Python 先回 SOCKET_ERROR。
- **回歸**（期望值都取自 Python 3.12.3 CLI 對記錄請求行的 echo stub 實測，stub 收到 0 bytes）：
  - `sw-cli` `tests/event_add_document.rs`：
    - `add_lone_surrogate_in_document_fails_after_connect_without_sending`：7 種文件，含孤立 high／low、high 後接非 low、連續區間、key 內、與 NUL 混用。比對 rc、stdout、stderr、connect 次數 1、送出 0 筆。修正前為紅燈。
    - `add_lone_surrogate_connect_failure_reports_socket_error_first`：exit 2、SOCKET_ERROR。
    - `add_paired_surrogate_escape_is_sent_as_one_code_point`：合法的 surrogate pair 仍照送。
  - `sw-proto` `tests/pyjson_golden.rs::keep_lone_surrogates_escapes_and_flags`：含 RPC-H-03 `loads` 仍為 U+FFFD 的斷言。
  - 黑箱 `tests/contract_blackbox/test_bb_event.py::test_event_add_lone_surrogate_rejected_before_send`：斷言 `s.requests() == []`。
    - Python 基線：8 passed。
    - Rust CLI（`SERIALWRAP_CLI_EXE=target/debug/serialwrap SERIALWRAP_AB_LABEL=rust-1b`）：修正前 1 failed（rc 0），修正後 8 passed。

## 3. 測試數字

| 檢查 | 結果 |
|---|---|
| `cargo test -p sw-proto -p sw-cli --features sw-cli/test-fakes` | 修正前 711 passed；修正後 715 passed／0 failed／3 ignored |
| `cargo test --workspace --features sw-cli/test-fakes,sw-pal/test-fakes`（`ef924e9`） | 910 passed／0 failed／7 ignored |
| `cargo check --workspace --target x86_64-pc-windows-gnu` | 每個 commit 都 rc 0 |
| 黑箱子集（Rust CLI）：`test_bb_event.py`、`test_bb_profile.py`、`test_bb_rpc_params.py` | 8＋61 passed |
| `python3 -m tools.contracts check` | 每個 commit 都 ok |
| `python3 -m policy_check --repo .` | 每個 commit 都 fail 0（只有 workflow 安裝行的既有 WARN） |
| `python3 scripts/rust/check_cfg_boundary.py` | 每個 commit 都 rc 0；沒有新增 platform cfg |
| `tests/acceptance/regen_lock.py --check --base origin/feature/rust-refactor` | OK |

全套 `python3 -m pytest -q tests/`（非 root）：

| 樹 | 結果 | 失敗集合 |
|---|---|---|
| 起點 `62ffec7` | 3 failed、5296 passed、161 skipped、1 xfailed | 基準 3 項（見下） |
| `1b8719a` | 3 failed、5296 passed | 與起點相同 |
| `ef924e9` | 4 failed、5296 passed | 起點 3 項，另加 `test_cli_mgmt_daemon.py::test_daemon_start_stop_ab_real_daemon`。原因是 4 套全套平行執行，其他套的 daemon 被 `/proc` 多開偵測報成 `multi_open: true`；單獨重跑 1 passed |
| `2a65438` | 43 failed＋1 error | 平行執行時，這個 worktree 的 git 中繼資料歸 root 所有，`git` 報 dubious ownership（rc 128），影響 mkbundle、架構文件與 provenance 等測試。修正擁有者後，序列重跑這 44 項只剩起點 3 項失敗 |

所以每個 commit 的失敗集合都沒有超出起點基準。

起點基準的 3 項失敗（起點本身也紅，與本分支無關，未處理）：

- `tests/test_l1_judge.py::test_l1_lock_nodeids_subset_of_collect_only`：`s0_bc.linux` 鎖檔中的 pyserial `COM9` 參數化 nodeid 不在 collect-only 輸出中。
- `tests/test_rust_bench_tools.py::test_cargo_workspace_tests_windows_native`。
- `tests/test_rust_stage1b_registration.py::test_p4_required_cases_have_passing_evidence`。

## 4. 不成立主張

詳見 `feature/s1x-b-fixes` 的 `docs/rust-refactor/reviews/s1x-b-fix-responses.md`。

1. **S1X-b-1 修法「錯誤位置取 `item` 的 `start`」只對單字元 token 成立。** CPython 用 `len(this)+1+len(that)` 回推位置，且跳脫 token 原文只有兩個字元。CPython 3.12.3 實測：
   - `[\x7a-a]` → `bad character range \x-a at position 3`；
   - `[\N{LATIN SMALL LETTER Z}-a]` → `\N-a at position 23`；
   - `[a-\x41]` → `a-\x at position 3`。

   修正前的 Rust 對 `[\x7a-a]` 回 `\x7a-a at position 1`。本分支照 CPython 實作，上述案例已收進 golden。
   報告指出的四個主要案例（`[z-a`、`[z-ab`、`[z-a\A]`、`[\d-z` → `at position 1`）全部成立。

## 5. 範圍外新觀察（未處理，請裁決）

- **pyre：樣式以單獨 `\` 結尾時，CPython tokenizer 預讀的 `bad escape (end of pattern)` 優先於前一個 token 的錯誤。** 這是既有碼問題，起點 binary 同樣如此。
  - 例：`[z\A\`（Py：`bad escape (end of pattern) at position 4`／Rust：`bad escape \A at position 2`）、`a\8\`、`(?P<\`。
  - 影響面同 S1X-b-1：`profile test` 的 `error` 欄位。
  - 修法要在 pyre 所有 token 讀取點加上預讀語意，超出 S1X-b-1 裁決範圍，建議另立 finding。

## 6. OWNER-BATCH

無。兩項修正都沒有改 SC／BC／DF、凍結鎖（lock 檢查 OK）或 oracle 語意。golden 只新增列；RPC-H-03 回應端不變。

## 7. 未完成項

- 審查 B 的 Low（S1X-b-3～7）依指示未處理。
- 範圍外新觀察（§5）未處理，待裁決。
- 全套 pytest 的 3 項起點基準失敗不屬本分支範圍，未處理。
- 沒有在 Windows 原生環境執行（只做 `cargo check` 交叉檢查）。
