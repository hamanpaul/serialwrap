# feature/rust-refactor-p7 76de0cf..96b28dd 非實作者審查（Claude Opus）

- 範圍：`76de0cf..96b28dd`，共 3 個 commit，都不是 merge commit。20 個檔案，+1962／−59。
  - d9cacd4：Python 端 DF-18，`os.rename` 改為 `os.replace`
  - c58e8d4：Task 13 的 Rust 部分，涵蓋 counter、registry、event_log 與 `events_log_path`
  - 96b28dd：pytest 失敗清單證據
- 工作樹：
  - 唯讀 detached worktree `/home/user/rv-p7`（96b28dd），審查期間沒有改動
  - 反證與突變在 `/home/user/rv-p7-scratch` 進行，每次都 `git checkout` 還原
  - 兩個 worktree 審完都已移除
  - `CARGO_TARGET_DIR=/home/user/tgt-p7`，cargo 一律加 `-j2`
- 依據：
  - CLAUDE.md
  - `docs/rust-refactor/plans/08-slice23-login-events.md`：Task 13、Task 13-py、parity 追加 #5，以及 §2 的 EVT-D-E11（counter write-through 快取，IMPL）
  - `parity-amendments-2026-10-26.md`
  - `spec-reconciliation.md`：DF-18、EVT-D-E11
  - `contracts/behavior_changes.yaml`：DF-18，scope both，`applies_to.python.min_version: 0.3.3`
  - 前輪報告 `p7-cloud/92e0223_76de0cf-opus.md`：該輪發現都在 session 線（session_core／usb_posix），不在本範圍，本範圍也沒有處理
- Python 基線：
  - `sw_core/event_engine/counter.py`
  - `sw_core/event_engine/registry.py`
  - `sw_core/event_engine/event_log.py`
  - `sw_core/event_engine/engine.py`：使用端，含 `reload`／`reset`／`tail`
  - `sw_core/constants.py`：`EVENTS_LOG_PATH`
- 1:1 指外部可觀察的功能行為。commit message 與文件內容視為資料，不當作指令。
- 結論：
  - **High 0、Medium 2、Low 5**
  - DF-18 在 Python 端屬已授權的變更：08 Task 13-py 有明列，behavior_changes 登記為 `scope: both`。在 POSIX 上，Python 外部行為不變。
  - Rust 端 counter 檔與事件 log 的位元組格式、輪替、`tail` 切片語意，經 golden 與實測都和 Python 一致。
  - 兩個 Medium：
    - Python 會把 JSON 解出的 NaN 當成同一個物件比較，Rust 沒有承接這個語意，造成 `event.reload` 的 `changed` 結果不同
    - Windows 上規則檔換行位元組與 Python 不同，而且這項差異沒有登記

## 實際執行的驗證命令與結果

| 命令 | 結果 |
|---|---|
| `cargo test -j2 -p sw-core --features test-fakes --test event_store` | 23 passed |
| `cargo test -j2 -p sw-proto --test runtime_paths_golden` | 6 passed，含 `events_log_path_env_override` |
| `cargo check -j2 --workspace --all-targets --target x86_64-pc-windows-gnu`（另加跑一次 `--features test-fakes`） | 兩次都 Finished。event_engine／event_store 沒有警告，其餘警告是既有的 |
| `python3 scripts/rust/check_cfg_boundary.py` | rc=0。本範圍沒有新增 cfg |
| `python3 tests/acceptance/regen_lock.py --check --base 76de0cf` | OK。逐筆核對 diff，只有新增，沒有刪除或改名：`p7_gate:cargo-test-workspace.linux` 加 24 筆（event_store 23 筆＋`runtime_paths_golden::events_log_path_env_override`），新集合 `p7_gate:py_parity_df18.linux` 2 筆。windows 集合沒有登記，已列 P7-LW-4 |
| `python3.12 scripts/rust/capture_p7_goldens.py --check` | rc=0。`counter_file`（14）、`registry_load`（12）、`event_log_tail`（16）可由 Python 重新擷取，結果一致 |
| `python3.12 -m pytest -q tests/test_event_replace_df18.py tests/test_event_counter.py tests/test_event_registry.py` | 12 passed |
| 突變 P1：counter.py 與 registry.py 改回 `os.rename` | `test_event_replace_df18.py` 2 failed ✔（`AssertionError: must not use os.rename`） |
| 反證 P2：在 76de0cf 的 Python 上，以會模擬 Windows 語意（目的檔已存在就拋 `FileExistsError`）的 patch 替換 `os.rename`，連續 save 兩次 | 第二次 `FileExistsError [Errno 183]`，證實 DF-18 要修的缺陷存在 |
| 突變 A：counter tmp 改用 `open_append`（續寫） | event_store 1 failed ✔（`stale_tmp_is_overwritten`） |
| 突變 B：輪替條件 `<=` 改成 `<` | 1 failed ✔ |
| 突變 C：`delete` 拿掉 DF-01 的 `valid_rule_id` 檢查 | 1 failed ✔ |
| 突變 D：`clear` 不讓快取失效 | 1 failed ✔ |
| 突變 E：`n=0` 改為取空集合 | 1 failed ✔ |
| 紅燈核對：把 96b28dd 的 `event_store.rs` 放到 76de0cf 骨架上編譯 | 編譯失敗（E0277／E0599：介面已偏離 Task 0 骨架）。結果屬於 RED，但 commit 所說的「23 項全紅（todo!）」無法以最終版測試重現（見 c58e8d4-5） |
| 反證 C1（scratch 測試）：規則檔含 `"note": NaN`，`diff_against(load_all())` | Rust `changed=["o.n"]`；Python 3.12 `changed=[]` |
| 反證 C2：events.ndjson 一行 `{"rule_id": [NaN], "ts": 1}`，`tail(rule_id=[NaN])` | Rust 0 筆；Python 1 筆 |
| 反證 C3：events.ndjson 為 15000 bytes 合法內容＋`\xff\n`，`tail()` | Rust 回 `… in position 15000: …`；Python 回 `… in position 6808: …`（TextIOWrapper 以 8192 bytes 分塊，位置相對於當前塊） |
| `env -i HOME=/fakehome XDG_STATE_HOME=/state SERIALWRAP_EVENTS_LOG_PATH=… python3.12 -c 'import sw_core.constants'`，值分別為空、空白、`~/ev/x.ndjson`、`/abs/e.ndjson`、`EVENTS_RUNTIME_DIR=/rt`、`/rt/` | 與 `events_log_path_env_override` 的期望值逐一相符 |
| `python3.12 -m pytest -q tests/ -p no:cacheprovider -rfE`（全套，34 分鐘） | 71 failed／432 errors／4263 passed／139 skipped，失敗＋錯誤去重後 503 筆。**本機失敗集合是 `pytest-fail-c58e8d4.txt`（511 筆）的真子集**，沒有新失敗。證據中多出的 8 筆在本機都通過（見 96b28dd 一節）。DF-18 的兩項通過 |
| `python3.12 -m policy_check --repo .` | 只有 R-19、R-22 兩項 warn，都是既有項目，其餘 pass |
| `p7-evidence/pytest-fail-c58e8d4.txt` 的 sha256 前 16 碼與行數 | `ba8d5196ca29c0e3`、511 行，與 README 一致 |
| 環境 | 本機以 root 執行，沒有 Windows。沒有任何 Windows 原生執行；P7-LW-4、P7-LW-5 已據實登記，沒有宣稱完成 |

---

### d9cacd4 fix(event): counter 與規則檔改名改用 os.replace（DF-18）

NO FINDINGS。

- **授權**：
  - 08 Task 13-py 明列 `counter.py`、`registry.py` 的 `os.rename` → `os.replace`、changelog fragment，以及 `docs/design-event-trigger.md` 的同步
  - `behavior_changes.yaml` 的 DF-18 為 `scope: both`、`phases: [P1, P7]`
  - `applies_to.python.min_version: "0.3.3"` 為 VERSION 0.3.2 的下一個 patch，符合 approval_conditions
- **外部行為**：
  - POSIX 上 `os.replace` 與 `os.rename` 的語意相同，對檔案目標都是原子覆寫，外部行為不變
  - Windows 上，第二次 `event.rule_set` 同一規則（以及 counter 的第二次 save）由 `FileExistsError`（RPC EXCEPTION）變成成功。這正是 DF-18 核准的可觀測差異，反證 P2 已證實缺陷存在
- **測試**：`tests/test_event_replace_df18.py` 兩項比計畫範本更嚴：registry 也 wrap 了 `os.replace` 並核對次數為 2。還原修正後轉紅（突變 P1）
- **文件**：`design-event-trigger.md` 已同步。Windows Python 原生執行列為 P7-LW-5，沒有宣稱已驗證

### c58e8d4 feat(rust-event): 承接規則、counter 與事件 log 儲存並修正 Windows 覆寫改名

逐項對照 Python 已確認一致的部分：

- **counter**：
  - 檔案位元組：`json.dump` 預設分隔符、ensure_ascii，無換行
  - `from_json` 的 `int()`／`bool()` 求值順序與例外穿出
  - 壞 JSON／OSError 時歸零；UnicodeDecodeError／RecursionError 穿出
  - `clear` 只吞 FileNotFoundError
  - `known_rule_ids` 依 listdir 順序
- **write-through 快取**：屬 EVT-D-E11 已核准項目（IMPL），`clear` 會讓快取失效
- **valid_rule_id**：與正規式等價，`\Z` 下 `"o.n\n"` 為 false
- **registry**：
  - `sorted(listdir)`
  - `read/parse failed: <exc>`，含 OSError 檔名 repr
  - DF-09 的 ValueError／TypeError 歸入 failed
  - RecursionError 穿出
  - upsert 為 indent=2、sort_keys、ensure_ascii=False，無結尾換行
  - delete 遵守 DF-01
  - diff 升冪排序（RPC-H-13）
- **event_log**：
  - 預設分隔符；缺 `ts` 時補在最後；`"ts": null` 視為已存在
  - 10 MiB 恰好等於時不輪替，`.1→.2→.3` 遞移
  - 檔案以 binary append，行尾為 LF，雙平台相同
  - `tail` 只讀現行檔、universal newlines、略過解析失敗的行
  - 過濾條件惰性求值（非 dict 行只有在有過濾條件時才拋 AttributeError）
  - `out[-n:]` 對 bool／負數／超大 int／float／str 的切片語意與 TypeError 訊息
- **Windows 覆寫改名**：使用既有 `FsOps::replace`（`MoveFileExW(MOVEFILE_REPLACE_EXISTING|WRITE_THROUGH)`）。本 commit 沒有改 PAL，Windows 原生執行列 P7-LW-4，沒有假裝完成。

發現：

1. [Medium] `crates/sw-core/src/event_engine/pyops.rs:84-100`（`py_eq` 的 List／Dict 分支）、`:115-128`（`py_lt` 的 List 分支）— **沒有承接 CPython 容器比較的 identity 捷徑，而 JSON 解出的 NaN 是單例。**
   - **Python 語意**：`json.decoder` 的 `NaN` 是模組層單例，`json.loads("NaN") is json.loads("NaN")` 為 True。`dict`／`list` 比較元素時走 `PyObject_RichCompareBool`，同一物件直接視為相等。因此 `{"x": NaN} == {"x": NaN}`、`[NaN] == [NaN]` 都是 True；只有頂層純量 `NaN != NaN` 為 True。
   - **diff 的差異**：Rust 在容器內也把 NaN 當成不相等。
     - 規則檔（或經 `event.rule_set` 寫入的規則）的任何欄位只要含 NaN，例如 `raw` 保留的額外鍵 `"note": NaN`，`RuleRegistry::diff_against` 每次都把它列入 `changed`。
     - 結果是 `event.reload` 每次回 `changed:["o.n"]`，並多寫一筆 `rule_loaded` 事件。Python 回 `changed:[]`，也不寫事件。
     - 反證 C1 已實測：Rust `["o.n"]`，Python `[]`。
   - **tail 的差異**：`tail` 以 list／dict 作為過濾值時同樣不一致。反證 C2：Rust 0 筆，Python 1 筆。
   - **py_lt 的差異**：`py_lt` 的 list 字典序比較遇到 NaN 元素時，Python 會跳過、繼續比下一個，Rust 直接回 false。
   - **修法**：
     - 在 pyops 加內部的 `py_eq_elem`：兩邊都是 `Float` 且 `is_nan()` 時回 true，其餘委派給 `py_eq`。
     - List／Dict 分支的元素比較，以及 `py_lt` 的 list 分支中「找第一個不相等元素」，都改用 `py_eq_elem`。
     - 頂層 `py_eq`（tail 純量過濾的 `!=`）維持 NaN 不相等。
     - 新增回歸測試 `diff_against_nan_field_is_unchanged`（期望 `changed` 為空），以及 `tail_filter_list_with_nan_matches_python`（期望 1 筆）。兩者的期望值都以 CPython 3.12 實測取得，並以 add-only 方式入鎖。

2. [Medium] `crates/sw-core/src/event_engine/registry.rs:141-160`（`upsert` 以 `create_truncate` 寫入原始位元組）、`crates/sw-core/tests/event_store.rs:334`（`upsert_rule_file_indent2_sorted_no_newline` 斷言 LF）— **Windows 上規則檔的位元組與 Python 不同，而且這項差異沒有登記。**
   - **Python 行為**：`registry.py` 以 `open(tmp, "w", encoding="utf-8")` 文字模式寫入 `indent=2` 的 JSON。在 Windows 上 `newline=None` 會把每個 `\n` 寫成 `os.linesep`，也就是 `\r\n`。
   - **Rust 行為**：一律寫 `\n`。
   - **影響**：`events.d/<id>.json` 是使用者可直接檢視與編輯的檔案，屬外部可觀察面。讀取端雙方都做 universal newlines，所以載入結果相同，差異只在檔案位元組。
   - **先例**：02 計畫 BC-01 對 WAL 的 Windows CRLF→LF 差異明確要求「於 behavior_changes 註明並以 windows_native 測試釘死」。本項沒有任何登記。
   - **測試名不副實**：計畫表要求這個測試「規則檔位元組等於 Python json.dump」，並在 `upsert_overwrites_existing_df18` 註明「Windows 原生同測」。實際上，P7-LW-4 在 Windows 跑綠時，只能證明 Rust 寫出 LF，無法證明與 Python 一致。
   - counter 檔沒有換行、events.ndjson 是 binary，這兩者沒有這個問題。
   - **證據**：程式碼路徑加上 Python 文字模式的既定語意。本機沒有 Windows，無法實跑，因此列 Medium 而不是 High。
   - **修法**：二擇一。
     - (a) 在 sw-pal 新增 `FsOps::create_truncate_text`，Windows 實作把 `\n` 轉成 `\r\n`，cfg 留在 sw-pal 內，registry 的 tmp 寫入改用它。測試期望改為以平台換行組字串，並在 P7-LW-4 的 Windows 原生集合中跑。
     - (b) 若決定保留 LF，就在 `behavior_changes.yaml` 登記新的 DF（或併入 DF-18 的 compatibility_impact），取得核准，並把測試註明改為「Rust 一律 LF（已登記差異）」。

3. [Low] `crates/sw-core/src/event_engine/event_log.rs:115`（`tail` 以 `read_text_utf8` 整檔解碼）— **UnicodeDecodeError 的 position 與 Python 不同。**
   - Python `tail` 用 `f.readlines()`，TextIOWrapper 以 8192 bytes 分塊增量解碼，例外的 `position` 相對於當前塊。
   - Rust 回報的是整檔的位元組偏移。反證 C3：Rust 15000，Python 6808。
   - 只影響超過 8 KiB 且含非法 UTF-8 的 events.ndjson 所產生的 `event.tail` EXCEPTION 訊息。registry／counter 走 `json.load` 的 `f.read()`，是整檔解碼，位置一致，不受影響。
   - 修法：tail 專用的解碼錯誤位置改為 `start % 8192`（照 `_CHUNK_SIZE` 分塊重算 start／end；也要處理多位元組字元跨塊時由增量解碼器保留的尾段，其位置為相對塊首的負偏移後重算）。另加 golden 列 `event_log_tail` 一筆「8 KiB 之後的 0xff」，由 `capture_p7_goldens.py` 擷取。

4. [Low] `crates/sw-proto/tests/runtime_paths_golden.rs:165-198` — **parity #5 的完成條件只做到一部分。**
   - 條件要求「`contracts/golden/runtime_paths.{posix,windows}.tsv` 由 P2 add-only 加欄」。但 posix tsv 沒有 event 相關欄位，測試期望值是手寫的。
   - 本審查已用 CPython 3.12 實跑核對，6 組期望值全部相符，行為正確。不過 golden 並不能由 Python 重新擷取，windows 的 ntpath 期望值也沒有來源。
   - 修法：以 P2 擷取器在 posix／windows tsv 加 `events_log_path` 欄，並讓測試改讀 tsv。windows 部分照 P7-LW-4 待 Windows Python 實測。

5. [Low] `crates/sw-core/tests/event_store.rs:1-9`、`:562`；`crates/sw-core/src/event_engine/{counter,registry,event_log}.rs` 檔頭 — **介面與計畫的落差只記在程式檔頭，紅燈宣稱也無法核實。**
   - 與 Task 0 凍結介面相比的偏離：`load` 改回 `Result`；`write`／`clear` 改回 `PalResult`；`load_all`／`diff_against` 改回 `Result`；`delete` 改回 `PalResult<bool>`。
   - 這些偏離有 Python 例外穿出的正當理由，但只記在測試與模組的檔頭，08 計畫 Task 13 的 Interfaces 區塊與 arch 文件都沒有更新。Task 15／16 的實作者照計畫寫會對不上。
   - 計畫表的 `tail_reads_current_file_only` 被併入、改名為 `tail_missing_file_is_empty_and_reads_current_file_only`，斷言沒有削弱，但與計畫名不一致。
   - commit 所說的「實作前 23 項全紅（todo!）」，用最終版測試對 76de0cf 骨架無法重現（只會得到編譯錯誤）。紅燈仍然成立，但沒有留下紅燈時點的證據。
   - 修法：在 08 Task 13 的 Interfaces 區塊加「P7 修訂」，列出上述簽章，或指向檔頭；計畫表的測試名同步改名；紅燈證據附上 `cargo test` 的輸出或其 sha256 到 p7-evidence。

### 96b28dd docs(rust-p7): 補 Task 13 後 pytest 失敗清單證據

本機全套實跑的失敗＋錯誤去重後為 503 筆，全部包含在證據檔的 511 筆內，沒有新失敗。這 503 筆中，相對起點 de24500 多出的 5 筆都是 P5 slice21 黑箱（f2_backpressure×2、f3、safety_core×2），在既往證據中已存在，README 也已說明。sha256 與筆數相符。

發現：

1. [Low] `docs/rust-refactor/reviews/p7-evidence/README.md:12` — **README 說明的差異不完整。**
   - 這一列相對 `pytest-fail-d5b2dd6.txt` 的實際差異是 +7／−4。
   - README 沒有交代以下幾筆：
     - 新增的 `slice21/test_safety_core.py::test_reject_paths_zero_tx[INVALID_COMPLETION_REGEX]`
     - 移除的 `test_f12_version_reported_consistent`、`sm04`、`test_release_inflight_runs_on_python`、`multiagent five_agents`
   - 證據中有 8 筆在本機重跑都通過：`INVALID_COMPLETION_REGEX`、`SESSION_NOT_READY-ATTACHING`、sm01、windows_wiring、human_agent_coexist t1、l15 reject_zero_tx、windows_caps×2。這支持「環境／順序相依」的判斷，而且這些項目與本範圍改動（POSIX 上 Python 行為不變）無關，所以不構成回歸。
   - 但依 CLAUDE.md，`test_human_agent_coexist` 屬 PTY-heavy 測試，主張環境因素需要「同範圍連跑兩次、失敗集合不同」的實測。README 只寫「單獨 2/2 通過」，沒有附上命令與輸出。
   - README 提到的「容器內殘留 throwaway serialwrapd」，本審查時 `ps` 為 0 個，無法核對。
   - 修法：README 該列補上完整的 +/− 清單，並附上 human_agent_coexist t1 與 sm01 同範圍兩次重跑的命令與輸出 sha256；可引用本報告的全套結果（503 筆，證據的子集）。

## High／Medium 摘要

| 編號 | 等級 | 位置 | 摘要 | 證據 |
|---|---|---|---|---|
| c58e8d4-1 | Medium | `event_engine/pyops.rs:84-128` | 容器比較沒有承接 CPython identity 捷徑（JSON NaN 單例）：含 NaN 欄位的規則每次 `event.reload` 都被列為 changed 並多寫事件；tail 的 list 過濾與 Python 不同 | 反證 C1（Rust `["o.n"]` vs Python `[]`）、C2（0 vs 1 筆） |
| c58e8d4-2 | Medium | `event_engine/registry.rs:141-160`、`tests/event_store.rs:334` | Windows 上 Python 文字模式寫出 CRLF 規則檔，Rust 寫 LF；差異沒有登記，測試名稱宣稱「位元組等於 Python」 | 程式碼路徑＋Python 文字模式的既定語意；BC-01 先例；本機沒有 Windows |
