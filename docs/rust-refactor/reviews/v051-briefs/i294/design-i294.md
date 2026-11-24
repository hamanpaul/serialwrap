=== DESIGN ===

### 1. 事實核對

本次唯讀核對基準為 `e5cee8c3c5242f25b9aca4fb0d8b6485de9ff112`，HEAD 與本機 `origin/main` 相同、detached、工作目錄乾淨。未連網、未修改檔案、未執行會產生檔案的測試或檢查。

| 項目 | 現行行為與證據 |
|---|---|
| 命令起點 | `crates/sw-core/src/op_runner.rs:715` 在 TX 前取得 `pre_offset`。它只隔離歷史 RX，不能排除 TX 後才抵達的 human 舊 prompt。 |
| 完成等待 | `op_runner.rs:710` 選擇 `completion_regex` 或 profile prompt；`:764` 等待命中。`crates/sw-core/src/uart_bridge.rs:309`、`:333` 對整段 snapshot 執行 regex search，每次 RX 更新可重新檢查。 |
| #294 | 完成條件沒有命令 echo 邊界；human prompt 可以完成 agent 命令。`crates/sw-core/src/session_core.rs:7320` 的出列判斷沒有 human 行在途閘門，`:7334` 只檢查前景槽與 READY 等條件。 |
| #262 | echo 即使已出現，之後命令自己的 `Path => ` 仍能命中寬鬆 prompt。內建 brcm regex 位於 `sw_core/assets/profiles/default.yaml:52`，確實容許此種輸出。只補 echo 錨不會修好。 |
| #271 | `op_runner.rs:196` 的 `prompt_cut_lines()` 使用最後一個 match 起點裁切；`:1045` 的 `last_match_start()` 沒有 prompt 字面身分。`abcroot@prplOS:/# ` 可整行被裁掉。 |
| 成功結果 | `op_runner.rs:793` 重新讀取 RX 後擷取 stdout，`:812` 起固定回 `partial=false`。等待判斷與擷取沒有共用同一份完成邊界。 |
| 已有折行處理 | `op_runner.rs:175`、`:218`、`:231`、`:257` 已處理 BusyBox `\r\r\n`、readline `" \r"`，保留至少 20 字元的折行間距限制；普通 CRLF 不得任意拼接成 echo。 |
| NUL | Rust `crates/sw-proto/src/pycompat.rs:1001` 與 Python `sw_core/util.py:65` 的 `clean_text()` 均保留 NUL；會清理 CR、ANSI、退格等，因此 stdout 本來就不是原始位元組介面。 |
| READY 證據 | `crates/sw-core/src/login_fsm.rs:554`～`:576` 有 nonce 與 prompt 等待，但 `LoginOutcome::Ready`（`:46`）沒有攜帶實際 prompt 字面值。目前沒有可直接拿來解 #271 的完成邊界。 |
| 結果結算 | `crates/sw-core/src/session_core.rs:8062` 的 `finish_command()` 處理 capture、狀態與 registry；一般 `PromptTimeout` 會轉 ATTACHED（`:8109`）。新增「完成證據不足」不能直接套用這個分支而意外改變 session。 |

Python 0.4.x 基線已讀 `sw_core/` 對照：

- `sw_core/session_manager.py:6874` 取得 RX 起點，`:6920` 起以整段 RX 的 regex search 等待完成，沒有 echo 錨。
- `sw_core/uart_io.py:2635` 每 50 ms 輪詢。這只是較低的檢查頻率，不是 human／agent 隔離保證。
- `sw_core/session_manager.py:5902`、`:5910` 同樣從最後一個 prompt match 起點裁切，保留 #262、#271 的弱點。
- 指定的長跑分析報告記錄 Python Windows 基線曾有相同 TX 交錯而未觀測到錯誤；這是歷史報告證據，本次沒有重新掃 WAL。

既有契約與測試：

- `docs/command-completion.md:1`：明示 producer completion 的等待、標記保留、prompt 裁切、超時及 passthrough 語意。
- `contracts/rpc_schema.json:548` 起：命令紀錄已有 `stdout`、`partial`、`error_code`、`recovery_action`，不需要新增欄位。
- `contracts/error_codes.yaml:851`：既有 `PROMPT_TIMEOUT` 表示命令未達完成條件，且禁止自動重送。
- `crates/sw-core/tests/command_exec.rs:145`：一般 line stdout。
- 同檔 `:604`：`completion_regex_meta_and_extraction`；`:635`：重複 echo 剝除；`:649`：background capture。
- 同檔 `:3885` 起：折行與誤剝 stdout 的既有保護，包括 `wrapped_echo_plain_crlf_stdout_spelling_command_is_kept`。
- `tests/test_command_completion.py:111`、`:127`、`:137`、`:148`、`:160`：Python producer、echo-only、歷史 marker、預設 prompt、前置 prompt。全部保留。
- 現有 fake DUT 多為整包回應，不能代替三票要求的分段時序驗收。

需求所稱「已知問題第 2 點」與目前文件編號不同：`docs/rust-refactor/stage2-release-install.md:67` 的 #294 是第 **3** 點；第 2 點是 #297。應依 issue 與內容更新，不依序號修改。

### 2. 方案

建議仍是一個 i294 包，分成兩個循序子項；共用 BC-22、同一份 completion evaluator，避免分別修三套判準。

#### 共用資料流

`command.submit` → 現有仲裁與 TX → 命令專用 completion evaluator → 回傳「snapshot＋echo 範圍＋prompt 範圍」→ 依同一組範圍擷取 stdout → actor 結算。

新增內部狀態：

```text
AwaitEcho
  ├─ 完整且可辨識的本命令 echo → AwaitPrompt
  └─ 等待結束仍無錨 → CompletionUncertain

AwaitPrompt
  ├─ 尾端出現可辨識的真正 prompt → Complete
  ├─ 只有寬鬆 regex 候選 → 繼續等待
  └─ 等待結束仍有歧義 → CompletionUncertain
```

沒有任何完成候選的真正 timeout，維持既有 recovery。取消、停機、epoch 失效與送出失敗仍優先處理。

#### 修改位置

| 檔案／函式 | 預定變更 |
|---|---|
| `crates/sw-core/src/op_runner.rs`／`run_command()` | 只替一般 agent、非 passthrough、未指定 `completion_regex` 的 line／background 命令接入 evaluator。 |
| 新增 `crates/sw-core/src/command_completion.rs` | 實作 echo 辨識、prompt 證據、完成範圍與歧義分類；註冊於 crate module。 |
| `op_runner.rs`／stdout 擷取 | 新增依明確範圍擷取的入口。既有公開 helper 與其他使用者保留；正常路徑不再重新 search 最後一個 prompt。重用既有 echo／折行剝除規則。 |
| `crates/sw-core/src/uart_bridge.rs`／`RxShared` | 提供 crate 內可用的 predicate 等待與一致 snapshot；沿用現有通知、期限、revision／watermark。既有 regex 等待語意不變。 |
| `RxShared` 的 epoch 內 prompt evidence | 僅保存一筆有來源的 prompt 字面證據，不持久化。clear、換 epoch、rebind 後失效。 |
| `crates/sw-core/src/login_fsm.rs`／`finalize_ready()` | 僅加觀測 hook：成功後若能獨立辨識 nonce 後的完整 prompt 行，記錄字面值；不改登入成功判準、不增加 TX 或等待。無法辨識就不記錄。 |
| `crates/sw-core/src/session_core.rs`／`finish_command()` | 新增內部 `CompletionUncertain` 結算，保留 stdout、結束 capture，使用既有錯誤碼，不誤進既有救援／降級分支。 |

prompt 證據另可來自「已確認 echo 前、同一行的完整 prompt 前綴」。不得從任意輸出行、任意 regex match 或人類輸入內容學習。

等待成功必須攜帶當時的 snapshot 與邊界，不能成功後另讀較新的 RX 再重新裁切。保留既有 RX 上限；若視窗修剪使必要證據遺失，回歧義，不擴大為無界 buffer。

#### 兩個子項

- **i294-i1：#294 echo 邊界與 prefix prompt。** 建立 evaluator、範圍擷取、無錨降階與 RPC 接線。此階段仍明列 #262／#271 未完成，不解除整包已知問題。
- **i294-i2：#262／#271 prompt 身分與尾端裁切。** 接入 prompt evidence，完成無換行／NUL、模糊 prompt 降階與三組整合驗收，最後更新修復狀態。

交界：

- **i306**：不重做 self-test 安全標記；其 READY 證據若要成為 prompt evidence，須使用已定案的標記後 prompt 邊界，不另發 probe。
- **p279**：不改 raw ownership、deferred replay 或 suspend／resume。
- **p290**：不改救援鍵設定或順序。
- **#264**：不清除板端半行、不插入換行、不包裝命令、不延後 agent TX。半行串接依既有行為；不把串接結果當成純 agent echo。

### 3. 設計決策

下列均為建議，尚待依 OD-112 登錄裁決。

| 決策 | 選項／建議／理由 |
|---|---|
| **【需編排者定案】echo 錨** | **選項：**任意子字串、完整邏輯行、注入唯一標記。**建議：**完整命令 echo，命令字元與空白須一致；容許已知 prompt 前綴及既有具原始證據的終端折行。**理由：**任意子字串會誤認輸出；注入標記會改 TX 與命令語意。 |
| **【需編排者定案】echo 範圍** | **選項：**只找第一行、掃描 TX 後各個完整邏輯行。**建議：**掃描完整邏輯行，讓 human 殘餘輸出可出現在 echo 前；找到後排除該 echo 之前的資料。不得接受任意 human 半行前綴。**理由：**#294 的 echo 不一定是 RX 第一行。 |
| **【需編排者定案】無 echo／無法重建 echo** | **選項：**短暫等待後恢復舊成功判準、直接要求 echo、改送額外 probe。**建議：**沿用原等待上限，期間不退回舊判準；到原 silence／deadline 結束時，若存在完成候選但證據不足，回 `status=error`、`PROMPT_TIMEOUT`、`partial=true`、`recovery_action=NONE`，保留可用輸出；不自動重送、不額外送救援鍵、不僅因歧義降級 READY。**理由：**短暫靜默無法證明 prompt 歸屬；額外 TX 可能干擾 human 程式。這會改變 echo 關閉使用者的成功結果，必須公告。 |
| **【需編排者定案】prompt 位置** | **選項：**行首或最後一行即可、必須是尾端完整 prompt。**建議：**必須在 echo 結束後，且 prompt 的結束位置就是清理後 snapshot 結尾；一般情況同時要求位於最後一行行首。**理由：**「行首或最後一行」仍接受輸出中的舊 match。prefix regex 可以保留，但不能只命中 prefix 就完成。 |
| **【需編排者定案】無換行例外** | **選項：**一律要求行首、允許已知字面 prompt 作尾端 suffix。**建議：**只有已觀測的完整 prompt 字面值才可不在行首；裁切僅移除這段 suffix。**理由：**讓 `abcroot@prplOS:/# ` 得到 `abc`，避免以寬鬆 match 起點裁整行。 |
| **【需編排者定案】短 prompt** | **選項：**`>`／`#` 也可任意 suffix、限定行首。**建議：**只含空白與 `>#$` 的 prompt 僅接受行首，不用它切同一行輸出；無法分離時 partial。允許同行裁切的字面 prompt 至少須含字母或數字。**理由：**`Path => ` 的尾端也含 `> `，不能把字面 suffix 當成普遍解法。 |
| **【需編排者定案】prompt 改變** | **選項：**任意 regex 候選立即學習、只採具來源的證據。**建議：**只採 READY 證據或已確認 echo 前綴；cwd／PS1 改變但沒有可靠新邊界時回 partial，不猜測 stdout 與 prompt 分界。**理由：**從待判定輸出自行學習會把 #262 再帶回來。 |
| **【需編排者定案】NUL** | **選項：**移除、可視化、保留。**建議：**保留 U+0000，JSON 正常跳脫；WAL 不變。**理由：**符合現行 `clean_text()`，避免資料遺失。 |
| **【需編排者定案】`completion_regex` 相容性** | **選項：**一併加 echo／prompt 閘門、保留現有 producer 路徑。**建議：**保留現有等待、擷取、META、marker 與 timeout 語意，不要求 echo 或 shell prompt。**理由：**它本來就是無 prompt／無 echo producer 的逃生路徑；本包不偷偷改變它的完成契約。 |
| **【需編排者定案】適用來源** | **選項：**所有 line 命令、僅 agent。**建議：**使用既有可信身分判斷，僅改 agent；可信 human、interactive、reboot、recover、file transfer 與 passthrough 保留原路徑。**理由：**避免改變人類互動升級與 recovery 契約。 |

必須明列的資訊限制：

- 若輸出刻意印出與真正 prompt 完全相同的文字，單靠 RX 無法判定哪一筆是 prompt；無限延長靜默也無法證明。要求嚴格完成保證的呼叫端仍須使用唯一 producer marker。
- raw human 半行與 agent 命令已在板端串接時，不能保證 agent 命令獨立執行；屬 #264。
- 因此可解除的是 #294 的已辨識故障及本包明列場景，不能宣稱「任意終端、任意輸出、無 echo 也能可靠完成」。

### 4. 契約與相容性

**BC-22** 登錄三組獨立行為差異：

1. human 舊 prompt 不再完成有可辨識 echo 的 agent 命令；stdout 排除 echo 前殘餘。
2. 命令輸出的 `=> ` 不再只因 regex 命中而提前完成。
3. 已知 prompt 黏在無換行 stdout 後時，只移除 prompt；NUL 保留。
4. echo／prompt 邊界不確定時，不再靜默回報完整成功，改為既有錯誤碼與 partial。

欄位採 `kind: BC`、`scope: rust_only`、`ab_participation: absolute_only`、`decision_ref: OD-111`，並引用 OD-112 與編排者實際定案。建議 `phases: ["P5"]`、coverage `owner_phase: P5`。

需要修改：

- `contracts/behavior_changes.yaml`：新增 BC-22，revision 單調前進，`approved_at_revision` 隨最後修改更新；兩子項共用一筆，不另配號。
- `contracts/coverage_map.yaml`：新增 `bc:BC-22`、Cargo nodeids 與規定的 `l1_only_approvals`。
- `tests/test_contracts_catalog.py`：在既有 `_V051_BC_IDS` 與日期表中 add-only 加入 BC-22。
- `tests/test_rust_contract_coverage.py`：add-only 加入 `bc:BC-22`；保留 269 與原有斷言。
- 不新增錯誤碼、不改 RPC／CLI schema 形狀、不修改 Python golden、既有 corpus 或 `cases.yaml` 分母。本方案不需要新增 golden。
- `contracts/` 變動會改變 `contract_revision`；需如實回報。

L1 新增節點：

- 第 5 節新增的 Cargo 節點，登記至 `p7_gate:cargo-test-workspace`、`s21_core`、`s22_gate:cargo_workspace`。
- 用工具 add-only 產生 `tests/acceptance/l1_required.lock.json`，collect 必須包含所屬集合既有節點。
- Windows 不在此環境登記；列出待原生收集節點。
- 不增加 pytest 產品案例，既有契約稽核測試增加 BC 資料不構成改動其判準。

文件：

- `README.md` 中英命令完成說明：echo、尾端 prompt、partial、無 echo 與 marker 建議。
- `docs/command-completion.md`：明確分開 Rust 預設完成與保持相容的 producer completion。
- `docs/serialwrap-spec.md:242` 起：更新一般 agent line／background 完成與擷取。
- `docs/rust-refactor/stage2-release-install.md:67` 及 README 中英對應段：以 issue 294 標示「0.5.1 已修」，保留 0.5.0 歷史描述；列出無 echo／#264／不可辨識 prompt 的限制。
- 共用 `changelog.d/294-command-completion.md`，列出三票及相容性影響。

### 5. 測試計畫

以下是新增測試名稱，尚未實作或執行。每個缺陷先取得舊 HEAD 紅燈，再修產品。

`crates/sw-core/tests/command_exec.rs`：

| 新測試名 | 斷言重點 |
|---|---|
| `i294_human_prompt_before_agent_echo_does_not_complete` | human prompt 單獨一筆先到；命令仍 running；其後 echo、輸出、真 prompt 才 done，stdout 精確為 agent 輸出。 |
| `i294_prefix_prompts_wait_for_own_echo` | 使用出貨 prpl、Speedport regex，涵蓋分包及同包 human prompt＋agent echo。 |
| `i294_deferred_human_line_between_queued_commands` | A 執行中累積 human 整行、B 排隊；回放後 human prompt 不能完成 B，human bytes 恰好一次。 |
| `i294_echo_fragmentation_and_supported_wraps` | RX 任意分包、BusyBox／readline 折行、UTF-8；普通 CRLF 拼接不得冒充折行。 |
| `i294_missing_echo_returns_partial_without_extra_tx` | 無 echo、只有舊 prompt；原等待結束後 error／PROMPT_TIMEOUT／partial，保留輸出、無額外 TX／重送，session 不因歧義降級。 |
| `i294_raw_half_line_is_not_a_clean_echo` | human 半行＋agent 字串不能成為純 echo 證據；TX 與 deferred 行為維持既有 B7。 |
| `i262_arrow_output_at_rx_tail_does_not_complete` | 分段送 `A => \n`、`B => `；停頓超過 50 ms，仍 running；再送 `\nC => 1\n` 與真 prompt，stdout 完整。 |
| `i262_historical_prompt_inside_output_is_ignored` | echo 後舊 prompt／prefix 在中間行出現，尾端還有資料，不得完成。 |
| `i271_no_newline_stdout_keeps_payload` | `printf abc` → 精確 `abc`；覆蓋寬鬆行首 regex 與 prefix regex。 |
| `i271_nul_terminated_stdout_preserves_nul` | `model\0` 黏真 prompt，stdout 精確保留 NUL；一般 `echo hi` 仍為 `hi`。 |
| `i271_unknown_or_short_prompt_does_not_discard_output` | 新 cwd／PS1、短 prompt、未知邊界：保留資料並 partial，不整行裁掉。 |
| `i294_completion_regex_keeps_existing_semantics` | 無 echo、無 prompt producer 仍完成；歷史 marker 不完成；META、marker 保留與 reboot 優先序不變。 |
| `i294_completion_snapshot_survives_later_rx` | 完成判斷後再到 RX，stdout 使用已接受 snapshot；trim／epoch 失效不可誤用舊證據。 |

`crates/sw-daemon/tests/rpc_console.rs`：

- `i294_rpc_human_agent_completion_matrix`：ServiceRig 從 console／`command.submit`／`command.get` 真正接線，涵蓋 shell、prpl、Speedport及 line／background。
- `i262_rpc_arrow_output_is_complete`：RPC 可見 A、B、C 全文，完成前不得非 partial done。
- `i271_rpc_no_newline_and_nul_stdout`：RPC JSON 解碼後精確比較 `abc`、`model\0`。

分段測試使用 TX／RX 已處理 barrier 與 FakeClock，不以任意 sleep 製造綠燈。無 echo 測試須主動推進既有虛擬期限；不得延長等待期限、補假 echo 或刪既有斷言來消除失敗。

保留並執行受影響既有測試：一般完成、重複 echo、所有 wrapped echo、background capture、timeout/recovery、passthrough、reboot，以及登入 #174／#224 對應樣本。若既有鎖定斷言與新無 echo 語意衝突，列出精確節點交編排者裁決，不自行改期望。

驗收命令，套用共同規則的環境、heavy-slot、nice 與 log 保存：

```bash
cargo test -p sw-core --features test-fakes --test command_exec --no-fail-fast
cargo test -p sw-daemon --features test-fakes --test rpc_console --no-fail-fast
cargo test -p sw-core --features test-fakes --no-fail-fast
cargo test -p sw-daemon --features test-fakes --no-fail-fast
cargo check --workspace --tests --features test-fakes --target x86_64-pc-windows-gnu
cargo clippy -p sw-core -p sw-daemon --tests --features test-fakes -- --cap-lints warn
python3 -m pytest -q tests/test_contracts_catalog.py tests/test_contracts_slice22.py tests/test_rust_p4_case_map.py tests/test_check_p4_cases.py tests/test_rust_contract_coverage.py
python3 -m tools.contracts check
python3 -m policy_check --repo . --pr-base-ref origin/main
python3 tests/acceptance/regen_lock.py --check
```

不跑全套 pytest、workspace test、正式 gate、容器或 Windows 原生。Windows cross-check 不代表 Windows console／真 UART 已驗收。

### 6. 風險與不做的事

- 無 echo 改為 partial／error 是可觀察相容性變更；必須先定案，不能用未登錄的短暫 fallback 混過。
- prompt 字面值可能因 cwd、權限、PS1 改變；無證據時保守保留輸出，不能反過來從任意輸出學 prompt。
- 不建立通用終端模擬器，不改 `clean_text()` 全域語意，不處理任意 escape redraw。
- 不以靜默時間當完成證明；不修改出貨 profile regex 來掩蓋 #262。
- 不改 Python、登入判準、producer completion、passthrough、reboot、recover、file transfer、human TX 順序或 #264 行為。
- 三票必須分別通過；echo 錨完成不能視為整包完成。
- 真機長跑與 Windows 原生仍由編排者安排；本次只有設計與靜態核對，沒有實作、實跑、commit、push、PR 或部署。

=== BRIEF ===

0.5.1 工作包 i294：修正命令完成判定三個缺陷，三組獨立驗收，共用 BC-22。

起點：分支 `<由編排者填入>`，HEAD `<由編排者填入>`。設計核對基準為 main `e5cee8c3c5242f25b9aca4fb0d8b6485de9ff112`；實作前依指定起點重查位置。

**背景**

- issue 294：human 整行已送出但 prompt 尚未回來時，agent 命令可被 human prompt 提前完成，stdout 為空或殘餘輸出。
- issue 262：echo 後的 `Path => ` 是命令自己的輸出，仍可誤中寬鬆 prompt；echo 錨不能單獨修好。
- issue 271：無換行 stdout 與 prompt 同行，現行依 regex match 起點裁切，會刪掉輸出。
- OD-111 凍結 Python；OD-112 要求編排者定案細節；OD-114 採兩個循序子項，各一輪實作及一次 terra 審查。最後僅重跑整包驗收與一次完整性審查，不另加多視角流程。
- 本 brief 的產品選擇須先由編排者將 DESIGN 第 3 節定案附入；不得由實作者自行切換降階策略。

**本包要做的事**

1. **i294-i1：echo 邊界與 prefix prompt**
   - 在 Rust 一般 agent line／background 路徑建立 completion evaluator。
   - 使用完整 echo，支援分包與既有具證據的 BusyBox／readline 折行；echo 前 human 殘餘不得進入正常 stdout。
   - 完成與擷取共用 snapshot／邊界；不成功後重新 search 全段 RX。
   - 實作編排者定案的無 echo／半行降階；不額外 TX、不重送。
   - 補 `i294_*` 對應 ActorRig 與 RPC 測試、BC-22 第一階段登錄、L1 及共用 changelog。
   - 明列 #262、#271 尚待 i2，不宣稱整包完成。

2. **i294-i2：prompt 身分、#262 與 #271**
   - 在 i1 結果上接入 epoch 內 prompt evidence；只從可辨識 READY 結果或完整 echo 前綴取得，不從任意 regex match 學習。
   - 一般 prompt 必須在 echo 後、最後一行行首且結束於 snapshot 尾端。
   - 同行 stdout 僅以已知、具辨識性的完整 prompt suffix 裁切；短／未知 prompt 不猜測。
   - 分別驗收 `=> ` 分段輸出、`printf abc`、NUL、一般換行輸出及未知 prompt 的 partial。
   - 完成 BC-22、coverage、L1 與文件。以 issue 294 定位更新 README 中英及 release install 文件，不誤改目前第 2 點 #297。

預定主要檔案：`crates/sw-core/src/command_completion.rs`、`op_runner.rs`、`uart_bridge.rs`、`login_fsm.rs` 的純觀測 hook、`session_core.rs` 的歧義結算、`command_exec.rs`、`crates/sw-daemon/tests/rpc_console.rs`，以及本包明列的契約、鎖檔、文件與 fragment。

**不得做的事**

- 不改 Python 程式、既有 golden、schema 形狀、錯誤碼集合、既有案例分母與鎖定節點。
- 不改 `completion_regex`、passthrough、登入判準、reboot／recover／file transfer。
- 不注入 sentinel、不包裝命令、不先送換行、不清 human 半行、不新增 human 在途閘門。
- 不改 ownership、deferred replay、救援鍵或 #264；不得順手承接其他工作包。
- 不為相容性失敗補假 echo、延長期限、放寬斷言或刪測試；衝突列出精確節點交編排者裁決。

**驗收**

- 先貼出指定缺陷測試在修正前的紅燈，再提交綠燈與 RPC 接線證據。
- i1：human 舊 prompt、prpl／Speedport prefix、deferred A→human→B、分包折行、無 echo、raw 半行及 TX 不增加。
- i2：#262 的 A／B／C 分段全文；#271 的 `abc`／NUL 精確結果；未知 prompt 保留輸出與 partial；三票互不回歸。
- DESIGN 第 5 節命令全數通過；契約登錄、coverage 與 L1 add-only 完整，policy 0 fail。
- 既有 producer、wrapped echo、background、timeout、登入樣本及 passthrough 斷言保留。
- BC-22 完整記錄 Rust／Python 差異；回報實際 `contract_revision`、新增 nodeids、Windows 待收集節點及 SPEC-GAPS。
- 三票全部驗收後才解除對應已知問題；未完成部分明列後續，不以 i1 完成代替整包交付。