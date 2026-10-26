# P6（Slice 2.2）Task 0～5 非實作者審查（Sonnet）

- 審查對象：`origin/feature/rust-refactor-p6` 相對 `origin/feature/rust-refactor-p5` 的 8 個 commit（`ae33e28..e5e2c56`）
  - `ae33e28` T0 契約登記／案例凍結　`5516137` T1 常數／骨架／harness　`a4921d6` T2～T5 TDD 紅燈測試
  - `f2b7d25` T2 Telnet　`7e86934` T3 line editor／line mode　`dcad8d0` T4 DeferredBuffer　`2070017` T5 出向 ring　`e5e2c56` marker 修補
- 權威依據：`plans/07-slice22-console.md`（Task 0～5、跨 Phase 介面）、`serialwrap-rust-spec-final.md`、`owner-decisions-2026-10-15.md`（OD-14～OD-20）、`plans/rust-architecture.md`
- 角色：嚴格 NON-IMPLEMENTER，只寫本報告，未改動其他檔案。

## 0. 總結

**沒有 High 級發現。** T2～T5 的行為實作與 Python 基線逐位元組一致，計畫列出的測試全數存在且斷言原文未被削弱，TDD 紅燈確實因「未實作」而紅。問題集中在流程與測試防線的細節：

- 3 項 Medium：上游 06 紅燈未揭露、`check_p6_prereqs` 前置缺漏被繞過、T0 commit 本身為紅燈。
- 5 項 Low、2 項 Info。

九項實作者宣告的偏離（a）～（i）：7 項 ACCEPT（其中 a、f 附條件）、(h) 部分 ACCEPT／部分 REJECT、(i) 流程面 REJECT（終態內容正確）。

## 1. 重現紀錄

| 項目 | 指令 | 結果 |
|---|---|---|
| Telnet | `cargo test -p sw-core --features test-fakes --test telnet_filter` | 40 passed |
| line editor | `… --test line_editor` | 15 passed |
| line mode | `… --test human_line_mode` | 13 passed |
| DeferredBuffer | `… --test deferred_bc02` | 18 passed |
| 出向 ring | `… --test out_ring` | 10 passed |
| shlex | `cargo test -p sw-proto --test shlex_golden` | 2 passed |
| 常數 | `cargo test -p sw-proto --test constants_slice22` | 1 passed |
| Python 側 | `pytest tests/test_contracts_slice22.py test_contracts_catalog.py test_acceptance_cases.py test_slice21_case_freeze.py test_slice21_skeleton_guards.py test_slice21_scope.py test_conftest_isolation.py test_telnet_filter.py test_rust_goldens_fresh.py` | 230 passed, 19 skipped（見 F11） |
| 鎖檔 | `python3 tests/acceptance/regen_lock.py --check` | OK |
| cfg 邊界 | `python3 scripts/rust/check_cfg_boundary.py` | exit 0 |
| Windows 目標 | `cargo check --workspace --target x86_64-pc-windows-gnu`；`cargo check -p sw-core -p sw-proto --tests --features test-fakes --target x86_64-pc-windows-gnu` | 皆通過 |
| 紅燈 commit | 於 `a4921d6` 跑五個 sw-core 測試 | 38＋28＋18＋10 個測試皆 `not yet implemented: P6 Task N`（telnet 的 `greeting_bytes`、`golden_tamper_is_detected` 例外 PASS；與 commit 訊息相符）；`shlex_golden` 2 PASS（06 T0c 已實作，commit 訊息已揭露） |
| golden 新鮮度 | 以 3.11 重跑四個產生器 | 與 checked-in 檔位元組完全相同（`test_golden_is_fresh` 因版本釘 3.12.3 在本機 skip，見 F11） |
| 差分模糊 | 2 萬筆含 Unicode 空白／`\x1c`／換行 env 賦值的字串，Python `_human_console_mode` vs Rust `human_console_mode` | 0 差異 |
| 06 基線比對 | 在 `p5` 與 `p6` 各跑 `cargo test --workspace --features test-fakes --no-fail-fast` 並比對逐 binary 結果 | P6 僅新增 T1～T5 的測試 binary，既有 binary 結果完全相同（含相同的 11 個失敗 target，見 F1） |

## 2. 與 Python 基線的行為對照（結論：無差異）

- **Telnet**（`telnet_console.rs` vs `telnet_console.py:63-139`）：狀態表、`local_on` 初值、`DONT` 只回一次 `WONT`、CR 立即 emit、`IAC IAC` 在 CR 後不被吞、SB 子協商，皆逐位元組一致；`escape_iac` 無 `0xFF` 時回 `Cow::Borrowed`。plan 的 34 個 Python item 全部承接（Python 30 個 `def test_`＋2 個 parametrize 展開，Rust 40 個含 golden／chunk／tamper）。
- **LineEditor**（vs `uart_io.py:1657-1719`）：`last_terminator` 每次呼叫重設、`pop_last` 停在 index 0、`discarding` 吞所有非終止 byte、終止字元結束 discarding 且不回顯，與 Python 一致；溢位語意以 Python 產生的 golden 為準，`discarding_does_not_echo` 的註解與 golden `le-ovf-16386-z-bs-cr` 相符。
- **human_console_mode**（vs `service.py:89-160`）：`strip()`／`split()` 以 `py_space` 補上 U+001C～U+001F、env 賦值正規式處理 `$` 前的單一 `\n`、`SUDO_VALUE_OPTS` 吃兩格，皆等價（模糊測試為證）。
- **DeferredBuffer**（vs `uart_io.py:956-975`、`2676-2770`）：epoch 不符即取代 entry、附加後長度 > cap 刪除整個 entry（含本次 bytes）、回放只送非空且存活且 epoch 相符者，與 BC-02 一致；`restore_front` 的 D-22(4) 排序與計畫表格相符。
- **OutRing**：Control 永不丟、部分寫出的 front 受保護、IAC 對齊後移、`oversized` 截尾，與計畫演算法逐步一致（唯一副作用見 F7）。

## 3. 發現

### F1（Medium）T1 Step 4 工作區閘門無法達成：06 的 11 個測試 target 在 P5 基線即紅燈，commit 訊息未揭露

- 位置：`5516137` 提交說明；計畫 Task 1 Step 4（「`scripts/rust/cargo_nonzero.sh --workspace --features test-fakes` … 06 既有測試全部仍綠燈」）。
- 重現：`cargo test --workspace --features test-fakes --no-fail-fast`。失敗 target：`sw-core` 的 `arbiter`（0/25）、`login_quiet`（1/28）、`mailbox`（0/10）、`session_core`（0/37）、`session_manager`（0/27）、`tx_writer`（0/32）、`wal_writer`（0/40），`sw-daemon` 的 `daemon_process`（2/21）、`restart_after_commit`（0/1），另有兩個 target `oserror_golden`（4/5）、`rpc_client_conformance`（6/7）。在 `origin/feature/rust-refactor-p5` 上完全相同（`lock` 在 P5 側有一個 flaky 失敗、P6 側通過，與本 PR 無關）。`crates/sw-core/src/arbiter.rs:4` 明載函式本體為 `s21_todo!`（`unimplemented!`）。
- 影響：P5 分支交付的是紅燈骨架而非實作。P6 Task 6 起（`SessionCore`、`Fence::admit`、`TxWriter`、`CommandRegistry`）的所有測試依賴這些行為；T1 的「骨架只加變體、不改既有行為」無法被既有測試驗證（驗證是空洞的）。P6 目前能綠燈的測試只限不依賴 06 的 T2～T5 純函式。
- 修法：在 PR 說明與 `07` 計畫的進度欄明列「上游 06 T8～T14 實作未交付，P6 Task 6～13 為 BLOCKED-UPSTREAM」；T1 Step 4 改以「與 P5 基線逐 binary 比對相同」作為本 Task 的可執行 gate（可用本審查的比對方法腳本化），不要宣稱「全綠」。不得在 P6 補做 06 的實作（計畫原則）。

### F2（Medium）`check_p6_prereqs.py` 回報 4 項上游缺漏，計畫要求「停止並回報」，實作者新增 `--max-task` 旁路繼續施工

- 位置：`scripts/rust/check_p6_prereqs.py:57-90`；計畫 Task 1 Step 2（「exit 0（否則停止並回報上游缺陷，不在 P6 補做）」）。
- 重現：`python3 scripts/rust/check_p6_prereqs.py; echo $?` → `rc=1`，缺 `crates/sw-daemon/tests/support/mod.rs :: pub struct ServiceRig`、`tests/contract_blackbox/bb_faketarget.py :: def emit`、`:: def written`、`tests/contract_blackbox/slice21/conftest.py :: def shell_daemon`。預設行為（不帶旗標）仍正確 exit 1，旁路旗標是新增的。
- 判斷：缺漏符號的最早消費者是 Task 10／11，T1～T5 確實不依賴，繼續做 T2～T5 在工程上合理；但這是未列入宣告清單的偏離，且 `--max-task` 讓「前置不齊」可被 CI 或後續腳本誤判為 OK。另外，第 22 列把計畫寫的 `tests/contract_blackbox/faketarget.py` 改查 `bb_faketarget.py`，理由（跨 Phase 介面註明 P2 沒有該檔）合理，已寫在註解。
- 修法：(1) 在計畫追蹤處登記 T10／T11 為 BLOCKED-UPSTREAM（與 F1 合併）；(2) 禁止在 CI／gate 腳本使用 `--max-task`（可在 `s22_gate_check.py` 以全表模式呼叫並斷言 exit 0）；(3) 在 `--max-task` 的輸出末行固定印出「仍有 N 項後續缺漏」並維持非零 exit 於 `--strict`。

### F3（Medium）T0 commit（`ae33e28`）本身有 8 個 pytest 失敗，之後才被 `5516137`／`e5e2c56` 修好

- 位置：`ae33e28`；計畫 Task 0 Step 4（Expected: PASS）；`CLAUDE.md` 測試政策。
- 重現：`git worktree add /tmp/t0 ae33e28 && cd /tmp/t0 && python3 -m pytest -q tests/test_slice21_skeleton_guards.py tests/test_resource_markers.py` → `test_resource_limit_rust_mirrors_equal_yaml`（`resource_limits.yaml` 新增 9 列、`MIRROR` 未同步）與 `test_resource_markers.py` 7 項（`tests/test_contracts_slice22.py` 缺平台／資源 marker，由 `e5e2c56` 補上）。
- 影響：bisect 到 `ae33e28`～`e5e2c56` 之間任一點都會得到紅燈；T0 commit 訊息只寫 `Review-pending`，未提這些失敗。終態（HEAD）全綠，故不影響內容正確性。
- 修法：合併前把 `e5e2c56` fixup 進 `ae33e28`，並把 `tests/test_slice21_skeleton_guards.py` 的 MIRROR 九列補在 `ae33e28`（目前在 `5516137`）；若不重寫歷史，至少在 PR 說明誠實記載「T0 單獨為紅燈，終態綠燈」。

### F4（Low）`test_slice21_case_freeze.py:57` 以字串比較 revision，且位元組指紋對已凍結集合永久失效

- 位置：`tests/test_slice21_case_freeze.py:57`（`assert fr["cases_revision"] <= cases_doc["revision"]`）與其後 `if fr["cases_revision"] == lock["cases_revision"]:`。
- 重現 1：revision 格式為 `YYYY-MM-DD.N`，字串比較在 N 進位時出錯：凍結值 `"2026-10-24.9"`、現值 `"2026-10-24.10"` 時，`"2026-10-24.9" <= "2026-10-24.10"` 為 False（`'9' > '1'`），斷言會誤判失敗；反向（現值倒退到 `.9`、凍結值 `.10`）反而會被放行。
- 重現 2：`slice_2.1.json` 凍結於 `2026-10-21.1`，鎖檔現為 `2026-10-24.1`，`cases_lock_sha256`／`cases_yaml_sha256` 的比對從此永遠被跳過。仍有保護的只有 `required_case_ids`、`denominator_sha256`（涵蓋 `ATTR_KEYS` 八個屬性）與由 cases.yaml 重算的集合；已凍結案例的 `prerequisites`、`contract`、`invariants`、`source_ref`、`evidence`、`behavior_changes` 被悄悄改動時不再有人偵測。
- 修法：把 revision 解析成 `(YYYY, MM, DD, N)` 整數 tuple 再比較；日後凍結檔（含已產生的 `slice_2.2.json` 之後的切片）新增 `case_fingerprints`（每個必要案例完整 YAML 的 sha256），並在測試中對 `required_case_ids` 逐案驗證，取代已失效的整檔位元組指紋。

### F5（Low）`test_acceptance_cases.py` 以自由文字 `implemented_by` 劃分 Stage 1A 群組

- 位置：`tests/test_acceptance_cases.py:44-54`。
- 判斷：放寬本身正當（見偏離 a）。殘餘風險：`implemented_by` 不在 `regen_lock.py` 的 `ATTR_KEYS`，若有人把某個 `bb-*` Stage 1A 案例改標為 `07-slice22-console.md`，它會離開一對一檢查群組；仍被擋住的方向是「test_bb_daemon_e2e.py 多一個函式卻沒案例」（`expected_refs - actual_refs`），被放過的方向是「案例沒有對應函式」。
- 修法：Stage 1A 群組改以 `case_id.startswith("bb-")` 或 `source_ref.startswith("tests/contract_blackbox/test_bb_daemon_e2e.py::")` 判定，並斷言所有 `bb-*` 案例的 `implemented_by == "04-stage1a-rust-cli.md"`。

### F6（Low）`overflowed_lines` 只在終止字元到達時計數，SC-16 的 WARNING 時機與 Python 不同，golden 也不涵蓋

- 位置：`crates/sw-core/src/human_console/line_editor.rs:46-52`（計數）、`scripts/rust/goldens/gen_line_editor.py`（只輸出 lines＋echo）。
- 差異：Python 在第 16386 個 byte 到達當下 `logging.warning(...)`（`uart_io.py:1706-1711`）；Rust 在 discarding 期間收到 CR／LF 才 `overflowed_lines += 1`。
- 重現：`LineEditor::new().consume(&vec![b'a'; 20000])`（沒有終止字元）→ `overflowed_lines == 0`，hub 不會有任何 WARNING；若 console 之後斷線，該次丟棄永遠沒有診斷。Python 同輸入有 WARNING。計畫的 `byte_16386_discards_whole_line_until_terminator` 只斷言「含終止字元」的情境，因此測試全綠。
- 修法：維持計畫指定的 `LineOutput` 三欄不變（既有測試以結構相等比較），另加 `LineEditor::take_overflow_started(&mut self) -> bool`（discarding 由 false 轉 true 時設旗標），由 hub／core 在觸發當下寫 WARNING；並補一個 golden 欄位或手寫測試覆蓋「未終止」與「跨 chunk 終止」兩種情境。

### F7（Low）OutRing 保護部分寫出的 front 時，被丟掉的可能是剛推入的最新項，語意上不是「drop-oldest」

- 位置：`crates/sw-core/src/human_console/out_ring.rs:92-100`；測試 `partially_written_front_is_protected` 只斷言 front。
- 重現：`OutRing::new(8)`；push `"abcdefgh"`；`consume(3)`；push `"ZZZZ"` → 結果 `DroppedOldest{bytes:4}`，但 `back()` 仍是舊的 `abcdefgh`，被丟掉的是剛推入的 `ZZZZ`（display 5＋4 > 8，從 index 1 起唯一候選就是新項）。
- 判斷：與計畫 Step 3 的演算法一致（不算實作錯誤），但回傳名稱與日誌用詞「oldest」會誤導；慢速 console 上最新輸出被丟、舊的半截輸出留存。
- 修法：在 `PushOutcome` 加 `DroppedNewest { bytes }`（或在 doc 與 WARNING 文字註明），並新增測試固定此行為；或改為允許截去 front 尚未寫出的尾端（需規格 D-05 裁決，故先以文件化處理）。

### F8（Low）兩個測試工具的邊角

- `crates/sw-core/tests/common/golden.rs`：`XorShift(0)` 永遠產生 0。`SW_TEST_SEED=0` 時 `chunking_is_invisible` 的 `rng.below(64)` 恆為 0、資料長度恆為 0，測試變成空轉卻顯示 PASS。修法：建構時 `seed | 1` 或改用 splitmix64 初始化。
- `.gitattributes` 沒有涵蓋 `contracts/golden/**`（`git check-attr eol -- contracts/golden/telnet_filter.tsv` → unspecified，P3 既有 golden 同樣如此）。`rows()` 以 `split('\n')` 解析且 `unhex` 遇到 `\r` 會 panic；Windows 以 `core.autocrlf=true` 檢出時 `include_str!` 會讀到 CRLF。修法：加上 `contracts/golden/** text eol=lf`。

### F9（Low）三個 golden 產生器各自複製 `write_raw_tsv`，`_tsv.py` 與實際檔案格式不一致

- 位置：`scripts/rust/goldens/gen_telnet_filter.py`、`gen_line_editor.py`（各一份）；`_tsv.py` docstring 與 `read_tsv`。
- 影響：`_tsv.read_tsv` 對 `telnet_filter.tsv`／`line_editor.tsv` 會因逗號分隔的 hex 而 `bytes.fromhex` 失敗；日後有人用共用讀取器掃 `contracts/golden/` 會踩雷。
- 修法：把 `write_raw_tsv` 放進 `_tsv.py`（含唯一性檢查），docstring 註明「raw 欄位」與「hex(UTF-8) 欄位」兩種模式；偏離（f）的實質判斷見 §4。

### F10（Info）T1 另改了第 5 個既有測試：`tests/test_slice21_skeleton_guards.py`

- 該檔 `MIRROR` 追加 9 列並把 `deferred_input_max_bytes`、`interactive_tombstones` 兩列改指 `constants.rs`（`DEFERRED_INPUT_MAX_BYTES`／`INTERACTIVE_TOMBSTONE_MAX` 移入 sw-proto 的連帶修改）。只擴大、未削弱，且 `resources.rs` 以 `pub use` 保留 06 名稱。屬正當連帶修改，但不在宣告清單內；建議納入 PR 說明。

### F11（Info）`test_golden_is_fresh` 16＋1 項在本機被 skip（CPython 版本釘 3.12.3）

- 為補足證據，我用 3.11 重跑四個新產生器並 `cmp`，皆與 checked-in 檔位元組相同。CI（3.12.3）應能自行驗證，無需更動。

## 4. 偏離逐項裁定

| 偏離 | 裁定 | 理由 |
|---|---|---|
| (a-1) `tests/test_acceptance_cases.py` 一對一檢查改限 Stage 1A | **ACCEPT**（附條件，見 F5） | 原斷言假設全部 `contract-blackbox` 案例都來自 `test_bb_daemon_e2e.py`；計畫 T0 要求新增 Rust 單元與 `tests/contract_blackbox/slice22/` 的同 suite 案例，原斷言與計畫互斥。放寬後 `test_bb_daemon_e2e.py` 新增函式卻無案例的方向仍被擋，且對新案例加了 `implemented_by` 白名單、`source_ref` 前綴、`::` 格式與唯一性檢查，不是卸除防線。 |
| (a-2) `tests/test_slice21_case_freeze.py` 的 `cases_revision` 等值改為「不晚於」 | **ACCEPT**（附條件，見 F4） | 原等值斷言使任何 add-only（含計畫 T0 本身要求的）都必然失敗，與 `regen_lock.py --add-only` 的設計矛盾；`required_case_ids == lock["sets"]["slice_2.1"]`、`denominator_sha256`、由 cases.yaml 重算的集合三道主防線都保留。須修正字串比較與補強案例指紋，否則防線有縫。 |
| (b) SC 的 `approval.owner` 用 `spec-reconciliation` 而非計畫的 `hamanpaul` | **ACCEPT** | `tools/contracts/catalog.py:48,859-861`：`APPROVAL_OWNER_BY_BASIS["reconciliation"]="spec-reconciliation"`，owner 不符會被 loader 拒絕（「SC／DF 不得冒用擁有者名義」）；現有 21 筆調和列同樣如此；`test_catalog_still_loads`、`test_new_sc_rows_follow_p0_schema` 通過。應回頭修正計畫文字。 |
| (c) capability 名稱 `posix_pty` 取代 `cap_posix_pty` | **ACCEPT** | `cases.yaml` 第 8 行註解與全部既有案例（`grep -c cap_posix_pty cases.yaml` → 0）皆用 `posix_pty`；沿用既有詞彙才能被守衛測試與 `platform_capabilities` 機制辨識。應回頭修正計畫文字。 |
| (d) add-only 補登 4 個 Windows-only `win-telnet-*` 案例 | **ACCEPT** | 計畫 §Task 12／13／BLOCKED-OWNER 總表 #1 反覆以「`win-telnet-*` 四案」為出口依據，T0 測試也要求 `win-telnet-raw-owner ∈ lock`，而 P2 沒有登記。逐項核對鎖檔差異：`sets` 僅新增 14 個 id（`slice_2.2` 81→95，`cli_1B`／`slice_2.3`／`slice_2.4` 同步 +14）、無任何移除、既有 133 案 `case_attrs` 零變動、`regen_lock.py --check` 通過；`frozen_lock_sha1`（`dc692da1…`）與實際 blob 相符；`slice_2.2.json` 的三個雜湊與現檔相符。 |
| (e) `DEFERRED_INPUT_MAX_BYTES`／`INTERACTIVE_TOMBSTONE_MAX` 移到 sw-proto | **ACCEPT** | 計畫 T1 的測試 `constants_slice22.rs` 以 `use sw_proto::constants::*` 直接引用這兩個名稱，不移就無法編譯；`resources.rs` 以 `pub use` 保留舊名，無重複定義；連帶的 MIRROR 修改見 F10。arch §3.1 與值（262144／16）與 Python 一致。 |
| (f) golden 存 ASCII hex、不套 `_tsv.py` 的 hex(UTF-8) 包裝 | **ACCEPT**（附條件，見 F9） | 計畫 T2 同時要求「經 `_tsv.py` 寫出」與「`input`／`expected` 保留 TSV 原字串、欄位為逗號分隔的 chunk hex」，二者在 `write_tsv`（對每欄再 hex）下互斥；實作者選擇與 `golden.rs` 規格一致的路徑，Rust 與 Python 兩側都以產生器重跑逐位元組驗證。`human_console_mode.tsv`、`shlex_split.tsv` 仍走 `_tsv.write_tsv`，與計畫一致。 |
| (g) `ShlexError` 斷言沿用既有 `struct { message }` 而非計畫的 enum | **ACCEPT** | 計畫同一節自相矛盾（「沿用、不修改 06 的 `shlex_split`（含 `ShlexError`）」與「Produces：新增 enum」）；改型別會破壞 06 公開介面，且 `human_console_mode` 只需要 `Err(_)`。斷言字串 `No closing quotation`／`No escaped character` 與計畫一致。 |
| (h) T1 略過 `ServiceRig::with_console_backend` 與 `deferred_methods` | **部分 ACCEPT／部分 REJECT** | `ServiceRig` 在上游 06 T18 並不存在（F2），`with_console_backend`／`uart_writes`／`console_ring_stats` 無處可掛，ACCEPT。但 `deferred_methods(yaml, delivered) -> Vec<String>` 是獨立的自由函式，`crates/sw-daemon/tests/support/mod.rs` 已存在，簽章加 `todo!` 本體並不依賴 `ServiceRig`，**REJECT 此項略過**：請補上（Low，可併入 T10 前任一 commit）。 |
| (i) T0 commit 本身留下紅燈、T1 才修好 | **REJECT（流程面）／終態內容 ACCEPT** | 見 F3。HEAD 全綠，修補內容正確；但 T0 單獨 commit 違反計畫 Step 4 的 PASS 期望，應整併或在 PR 說明誠實揭露。 |

## 5. 其他確認無誤的項目

- T0：`behavior_changes.yaml` diff 僅新增列與 `revision 4→5`，P1 已 `IMPLEMENTED` 的 BC-02／SC-13／SC-15／SC-16 文字未動；SC-28／29／31 的 `compatibility_impact`、`approval_conditions` 與 D-11／D-12／D-13／D-16／D-05／D-18 內容相符；`resource_limits.yaml` 九列值與 `overflow` 與 `test_resource_limits_slice22_rows` 相符；架構與調和表的 `owner_gen_at_capture` 已清除、`input_epoch`／`ConsoleMux`／`MuxHandle`／`HumanAdmissionClosed`／`InputStale`／`ReplayRevoked` 就位，統計 299→302、KEEP 148→151 一致；`SERIALWRAP_CONSOLE_BACKEND` 已加入 `conftest.py` 的 pop 清單與 `bb_harness.isolated_env`（後者因前綴迴圈已涵蓋而屬冗餘但無害）；`behavior_changes.yaml` 中 `phases` 含 P6 的 10 個 `case_ids` 全部已收錄於 `cases.yaml`。
- T1：常數值逐一對照 `sw_core/constants.py`、`uart_io.py:31`、`session_manager.py:100`、`service.py:313-322,1094,1111`、`session_manager.py:7048-7058`，全部一致；骨架簽章與計畫 T6／T7／T9 的 Produces 逐字相符；harness 簽章（`CoreHarness`、`TxRig`、`HubHarness`、`FakeIo`／`FakeConsoleRef`／`FakeConsoleFactory`、`testutil`）全部存在；PAL 新增只在 `sw-pal` 內有 `cfg`，`check_cfg_boundary.py` 通過；`ConsoleTransportFactory::new_mux` 預設 `Unsupported`，既有實作不受影響；Windows 目標可編譯。
- T2～T5：實作 commit 只改 `src/` 與 changelog fragment，測試與 golden 自紅燈 commit 起位元組未變（`git diff a4921d6 HEAD -- crates/*/tests contracts scripts tests` 僅 `tests/test_contracts_slice22.py` 的 12 行 marker）。
- 每個 commit 都有對應的 `changelog.d/rust-s22-*.md`，frontmatter 與計畫指定的 `type`／`scope` 相符。
- `python3 -m policy_check` 在本機環境未安裝（`No module named policy_check`），未能執行；R-09／R-17 等需在 CI 確認。

## 6. 結論

**Verdict：有條件通過（Conditional PASS）。** T2～T5 的實作可併入，無需修改行為；建議在進入 Task 6 之前處理 F1／F2（把上游 06 阻塞明文化，避免後續 Task 以空洞的綠燈宣稱完成）與 F3（整併或揭露 T0 紅燈），F4～F9 為低風險強化項。

## 7. 摘要表

| # | 嚴重度 | 類別 | 位置 | 一句話 | 修法 |
|---|---|---|---|---|---|
| F1 | Medium | 上游／流程 | `5516137`；plan T1 Step 4 | 06 的 11 個測試 target 在 P5 基線即紅燈（`s21_todo!`），T1 的「既有測試仍綠」為空洞 | 明列 BLOCKED-UPSTREAM，gate 改為與 P5 基線逐 binary 比對 |
| F2 | Medium | 流程 | `check_p6_prereqs.py` | 4 項前置缺漏被 `--max-task` 旁路，計畫要求停止 | 登記 T10／T11 阻塞，gate 以全表模式 |
| F3 | Medium | 流程 | `ae33e28` | T0 commit 單獨有 8 個 pytest 失敗，後續 commit 才修 | fixup 整併或在 PR 揭露 |
| F4 | Low | 測試防線 | `test_slice21_case_freeze.py:57` | revision 以字串比較；位元組指紋對已凍結集合永久失效 | tuple 比較＋逐案指紋 |
| F5 | Low | 測試防線 | `test_acceptance_cases.py:44` | 以自由文字 `implemented_by` 分群，可被改標逃出 | 以 `bb-` 前綴／來源路徑判定 |
| F6 | Low | 行為差異 | `line_editor.rs:46-52` | 溢位 WARNING 到終止字元才計數，Python 在觸發當下警告；golden 未涵蓋 | 新增觸發旗標＋補測 |
| F7 | Low | 行為語意 | `out_ring.rs:92-100` | 保護 front 時被丟的可能是最新項，卻回報 DroppedOldest | 新結果變體或文件化，補測 |
| F8 | Low | 測試工具 | `golden.rs`；`.gitattributes` | `SW_TEST_SEED=0` 空轉；golden 缺 `eol=lf` | seed 修正；加屬性 |
| F9 | Low | 工具一致性 | `gen_*`／`_tsv.py` | `write_raw_tsv` 複製兩份，`read_tsv` 讀不了新格式 | 收進 `_tsv.py` 並雙模式註明 |
| F10 | Info | 揭露 | `test_slice21_skeleton_guards.py` | 第 5 個被改動的既有測試未列於宣告 | 納入 PR 說明 |
| F11 | Info | 環境 | `test_rust_goldens_fresh.py` | 本機 3.11／3.13 下 17 項 skip；已手動驗證位元組相同 | 無 |

| 偏離 | 裁定 |
|---|---|
| (a) 兩個既有測試修改 | ACCEPT（附條件 F4／F5） |
| (b) `approval.owner = spec-reconciliation` | ACCEPT |
| (c) `posix_pty` | ACCEPT |
| (d) 4 個 `win-telnet-*` add-only | ACCEPT |
| (e) 常數移入 sw-proto | ACCEPT |
| (f) golden 為 ASCII hex 原字串 | ACCEPT（附條件 F9） |
| (g) `ShlexError` 沿用 struct | ACCEPT |
| (h) 略過 `with_console_backend`／`deferred_methods` | 部分 ACCEPT（`with_console_backend`）／部分 REJECT（`deferred_methods` 應補） |
| (i) T0 commit 留紅燈 | REJECT（流程面；終態 ACCEPT） |
