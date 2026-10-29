> 審查者：雲端 Claude Opus xhigh（Codex／pi 停用期間代理 sol 單審位，擁有者 2026-10-29 指定）

# feature/rust-refactor-p7 增量審查 `b85eec5..a04aa69`

本輪只有一個 commit：`a04aa69`（6 檔，+83／−0），沒有 merge commit。這個 commit 沒有改 production code（`crates/`、`sw_core/` 沒有變動），改動範圍如下：

- 黑箱測試：新增 `tests/contract_blackbox/test_p7_invariants.py::test_force_recover_attached_closes_before_reopen`
- `scripts/rust/p7_gate_report.py`：results.json 與報告表頭記錄 `tool_versions`（python、pytest）
- 回歸測試 `tests/test_p7_gate_report.py::test_report_records_tool_versions`
- L1 鎖：三個 `p7_gate:*` 集合 add-only
- `p7-fix-responses.md`、changelog fragment

## A 前輪 finding 核對

| 編號 | 原嚴重度 | 狀態 | 累計未修輪數 | 說明 |
|---|---|---|---:|---|
| 1 | Medium | 已修 `f150a24` | 0 | 本輪沒有改 production code，修正保留。 |
| 2 | Medium | 已修 `f150a24` | 0 | 同上。 |
| 3 | Medium | 已修 `f150a24` | 0 | 同上。 |
| 4 | Medium | 已修 `f150a24` | 0 | 同上。 |
| P7-71fe017-1 | Medium | 已修 `030d294` | 0 | 同上。 |
| P7-71fe017-2 | High | 已修 `7e2da7a` | 0 | 同上。 |
| P7-71fe017-3 | High | 已修 `8134806` | 0 | 同上。rulings/p7.md 已移交 P5，P5 修正合入後的狀態保留。 |
| P7-71fe017-4 | Medium | 已修 `afebff0` | 0 | 同上。 |
| P7-27b5bf4-1 | High | 已修 `20c9454` | 0 | 同上。 |
| P7-030d294-1 | High | 已修 `1677489` | 0 | 同上。 |
| P7-9065f14-1 | Medium | 未修 | 7 | `tests/acceptance/slice22_evidence_map.yaml:47` 仍是 `l15-delayed-reply: {status: PENDING}`。p7-fix-responses 維持 NEEDS-RULING（OWNER-BATCH）。rulings/p7.md（origin/docs/parity-amendments `999a88c`）仍沒有對應裁決，早已超過收斂規則 §4 的 3 輪門檻。 |
| P7-c5f0fc9-1 | High | 已修 `7bb9cfd` | 0 | 本輪沒有改相關檔案，修正保留。 |
| P7-c5f0fc9-2 | High | 已修 `7bb9cfd` | 0 | 同上。 |
| P7-c5f0fc9-3 | Medium | 已修 `f9e90a9` | 0 | 同上（`p7_gate.sh` 本輪沒有變動）。 |
| P7-c5f0fc9-4 | High | 已修 `7bb9cfd` | 0 | 同上。 |
| P7-c5f0fc9-5 | High | 已修 `f9e90a9` | 0 | 同上。 |
| P7-c5f0fc9-6 | Medium | 已修 `29aaa7b` | 0 | 同上。 |
| P7-7bb9cfd-1 | Medium | 已修 `b85eec5` | 0 | 本輪沒有改 `p7_ab_report.py`，修正保留。 |
| P7-cd7acf0-1 | High | 已修 `f9e90a9` | 0 | 同上。 |
| P7-cd7acf0-2 | High | 已修 `f9e90a9` | 0 | 同上。 |
| P7-cd7acf0-3 | High | 已修 `f9e90a9` | 0 | 同上。 |
| P7-cd7acf0-4 | Medium | 未修 | 4 | L1 鎖仍沒有 `p7_gate:contract-blackbox-slice22-b`。p7-fix-responses 記「維持至 P6 合入」，這仍是實作者端的主張，沒有編排者裁決（NEEDS-RULING）。gate 仍依 `blocked` 標記記為 BLOCKED-UPSTREAM／INCOMPLETE，不會誤判成 PASS。 |
| P7-cd7acf0-5 | Medium | 已修 `f9e90a9` | 0 | 同上。 |
| P7-cd7acf0-6 | Medium | 已修 `f9e90a9` | 0 | 同上。 |
| P7-f9e90a9-1 | High | 已修 `b85eec5` | 0 | `p7_gate:python3-m-pytest-q-tests` 本輪 add-only 5616→5618（加本輪兩個新節點），selector 不變，舊節點全數保留。 |
| P7-f9e90a9-2 | Medium | 已修 `b85eec5` | 0 | 本輪沒有改相關檔案，修正保留。 |
| P7-f9e90a9-3 | High | 已修 `b85eec5` | 0 | 本輪沒有改 s23 情境，修正保留。 |
| P7-f9e90a9-4 | High | 未修 | 3 | 情境本輪沒有變動。p7-fix-responses 依前輪建議，把「probe 送出後到 READY（含 nonce）」這段界線列為 SPEC-GAP（runner 的字面比對無法表達隨機 nonce 之後的界線），但還沒有編排者裁決。前輪具體反例（`root\ns3cretP7\necho __READY__<nonce>\necho AGENT_P7\n` 仍 PASS）依然成立。依 §4 累計 3 輪，應進 NEEDS-RULING。 |
| P7-f9e90a9-5 | High | 已修 `a04aa69` | 0 | 詳見表後說明。 |
| P7-de7a95a-1 | Low | 未修 | 2 | 本輪沒有改 `p7_case_coverage.py`。low-backlog.md 尚未收錄。Low，不擋放行。 |
| P7-de7a95a-2 | Low | 未修 | 2 | 同上。 |
| P7-de7a95a-3 | Low | 未修 | 2 | 本輪沒有改 `tests/test_p7_gate_win_argv.py`。Low，不擋放行。 |
| P7-b85eec5-1 | Medium | 未修 | 1 | 實作者端能做的過渡項已完成，剩下的步驟還在等上游。詳見表後說明。 |
| P7-b85eec5-2 | Low | 未修 | 1 | p7-fix-responses 依收斂規則不修。Low，不擋放行。 |

### 表中標「詳見表後說明」的項目

**P7-f9e90a9-5（已修 `a04aa69`）**

新測試在 [tests/contract_blackbox/test_p7_invariants.py:170](tests/contract_blackbox/test_p7_invariants.py:170)，做法如下：

- 起手狀態為 ATTACHED＋`CREDENTIALS_UNRESOLVED`，`bridge_generation == 1`，daemon 恰持有一個 tty fd。
- `recover --force` 期間，以 5 ms 取樣 daemon 指向該 tty 的 fd 峰值，要求等於 1。
- 回覆後依序確認：回到 ATTACHED、`bridge_generation == 2`、fd 數為 1、target 收到 0 byte。

兩種錯誤實作的驗證結果：

- **no-op**（不關閉、不重開，只回覆）：`bridge_generation` 維持 1，`:205` 必紅。
- **回條前重開**：在 worktree 外的 `git archive` 副本中做突變。讓 Rust `IoWorker::finish_async`（`crates/sw-core/src/uart_bridge.rs:714`）的 drain 執行緒在 join／close 之前，就先送出 `WorkerStopped`（`handle_closed: true`），close 本身再以環境變數延後。

  | close 延遲 | 結果 | 說明 |
  |---|---|---|
  | 8 ms | 5/5＋診斷 1 次，全部 FAILED | 失敗點在 `:204` 的 `wait_state(ATTACHED, 15s)` 逾時。最後狀態為 `state: DETACHED`、`last_error: ATTACH_FAILED:DeviceHeldError`、`bridge_generation: 1`。strace 看到重開的 `openat("/dev/pts/0") = -1 EBUSY`。 |
  | 30 ms | 3/3 FAILED | 同上。 |
  | 0 ms（自然時序） | 5/5 PASS | close 仍早於重開，這個情境下違規沒有實際發生，外部無差異可觀察。在 strace 減速下，同一突變也會以 EBUSY 轉紅。 |

- **Python 側同型突變**：在 `_await_drain` 中，`finish_drain` 之前先以假的安全回條呼叫 `_complete_drain`，close 延遲 0／30／500 ms，6/6 PASS。原因是 Python 的 reattach 執行緒會被 `self._lock` 卡到 `_force_recover` 回覆時（見「既有碼待辦」）。strace 量到 close 在 `14:01:28.120`、重開在 `14:01:38.134`，所以在這個情境下「回條前重開」在 Python 不可能實際發生。這不是測試的缺陷。

結論：no-op 與「實際發生重疊」的回條前重開都會轉紅，滿足原修法要求。不過轉紅的路徑不是文件宣稱的「峰值 2」，見 P7-a04aa69-1。

未突變時，A 共 4 次、B 共 6 次全部 PASS（每次約 11 s）。新節點以 add-only 加入 `p7_gate:contract-blackbox-p7-a`（38→39）、`-p7-b`（39→40）與全套 pytest 集合。`regen_lock --check --base b85eec5` 為 OK。

**P7-b85eec5-1（未修，過渡項已交付）**

已完成：

- 修法第 1 步：在 p7-fix-responses 登記依賴，並請編排者擴及 rulings/p5.md 的同一裁決。
- 修法第 4 步：results.json 與報告記錄 `tool_versions`（[scripts/rust/p7_gate_report.py:245](scripts/rust/p7_gate_report.py:245)、`:327-334`、`:348`）。
  - `init` 與全套 pytest 列用的是同一個 `python3`（`p7_gate.sh:34`、`:200`），所以記到的 pytest 版本就是實際跑全套列的版本。
  - 回歸測試 `test_report_records_tool_versions` 疊到 b85eec5 樹上會 FAILED（`KeyError: 'tool_versions'`）。

尚未完成：

- 第 2～3 步要等 P5 的明確 `ids=` 修正，但這個修正還不在任何分支上。`origin/feature/rust-refactor`、`origin/feature/rust-refactor-p5`（`5d5314e`）、`origin/claude/rust-refactor-p5-fix` 的 `test_invalid_regex_rpc_and_arbiter_reject_without_enqueue` 仍是 `parametrize("invalid", [..., "a" * 1025, ...])`，沒有 ids。
- 編排者也還沒把裁決擴及 `p7_gate:python3-m-pytest-q-tests`。
- 報告只列出版本，沒有在該列註明「9.2.0 下 INCOMPLETE 屬已知原因」。這點輕微。

原失敗情境不變：pytest 9.2.0 下該列為 INCOMPLETE `missing 3`。

建議：由編排者裁決為「延後」，交付點定為「P5 明確 ids 合入後的 P7 merge」，在那之前不再逐輪累計。

## B 新發現

**P7-a04aa69-1 [Low]** [tests/contract_blackbox/test_p7_invariants.py:203](tests/contract_blackbox/test_p7_invariants.py:203)（另見同檔 `:171-175` docstring、`docs/rust-refactor/reviews/p7-fix-responses.md:362`、commit 訊息）

**問題**：文件宣稱「回條前重開 → 峰值 2」，與 gate 的實際執行條件不符。

- gate 規定以一般使用者執行，而 Python（`sw_core/serial_port.py:393`）與 Rust（sw-pal POSIX serial）開埠時都會設 `TIOCEXCL`。
- 舊 fd 還沒關時，新的 open 會得到 EBUSY，所以 fd 峰值仍是 1。

**實測**：Rust 突變「回條早於 close、close 延 8／30 ms」共 8/8 轉紅，失敗點全在 `:204` 的 `wait_state(ATTACHED)` 逾時（DETACHED＋`ATTACH_FAILED:DeviceHeldError`）。`:203` 的峰值斷言每次都通過。也就是說，峰值斷言只在以 root 執行（CAP_SYS_ADMIN 會略過 TIOCEXCL）或實作沒設 TIOCEXCL 時才有作用。

另外，若錯誤實作在 EBUSY 後自動重試並成功，本案會 PASS。這目前沒有現行失敗情境。

**修法**：把 docstring 與 p7-fix-responses 改寫成實際的轉紅路徑：TIOCEXCL → EBUSY → `ATTACH_FAILED:DeviceHeldError` → `:204` 逾時；峰值 2 只在 root 下可見。斷言本身不用改。

**P7-a04aa69-2 [Low]** [tests/contract_blackbox/test_p7_invariants.py:205](tests/contract_blackbox/test_p7_invariants.py:205)

**問題**：docstring 寫「恰一次」，但只在 CLI 回覆後第一次看到 ATTACHED 時檢查 `bridge_generation == 2`。若回覆 `FORCE_RECOVER_TIMEOUT` 之後又重開一次（2→3），本案仍會 PASS。

這和 P7-b85eec5-2（func-test 版）同型。Python 基線的重開本來就貼著回覆時刻發生（約 10 s 後），所以這個時序窗口是真實存在的。目前 Python 與 Rust 實測都停在 2，沒有現行失敗情境。

**修法**：在 `:206` 之後隔 2～3 s 再讀一次 `session list`，斷言 `bridge_generation == 2`。

## 既有碼待辦（不計入結論）

- **Python 在 force recover 期間持鎖等待，與 Rust 的對外行為不同，尚未登記為 DF**
  - 成因：`sw_core/session_manager.py` 的 `recover_session`，在 `with self._lock:`（`:7699` 起）區塊內呼叫 `_force_recover`。對應分支是 ATTACHED＋force（`:7740`）與非 READY＋force（`:7745`）。`_force_recover`（`:7801`）會做 10 次 `time.sleep(1.0)`，期間一直持有 RLock，約 10 s。
  - Python 的現象：
    - clear 的 reattach 執行緒（`_spawn_attach` → `_attach_by_id` 要取 `self._lock`）被卡到回覆時才重開。strace 量到 close `14:01:28.120`、重開 `14:01:38.134`。
    - 同一窗口內，另一個 `session list` 在 CLI 端約 6 s 後回 `{"ok":false,"error_code":"TIMEOUT","daemon_reachable":false}`。
  - Rust 的現象：close→reopen 約 1.6 ms（前輪 strace）。同一窗口的 `session list` 0.01 s 就回應，狀態為 ATTACHED、gen 2。
  - 影響：這是對外可觀察的差異，目前沒有 DF／SC 登記。新測試只在回覆後讀狀態，所以兩邊都通過。Python 持鎖 sleep 疑似是缺陷，建議編排者判定是否比照 OD-57 登記 DF。
- `scripts/rust/p7_gate.sh:95-98`：快照來源仍寫死 `$ROOT/target/debug/`，沒有遵從 `CARGO_TARGET_DIR`。前輪已列，本輪沒有變動。
- `s0_bc`／`s0_win_pytest` 早已凍結 pytest 版本相依的參數化 id，`tests.yml:30` 的 Linux pytest job 也沒有釘 pytest 版本。這部分在 rulings/p5.md 的同一裁決範圍內，本輪沒有變動。

## 對裁決的異議（不計入結論）

無。

## 驗證

**環境與建置**

- 測試以一般使用者 `rev` 執行，使用 Python 3.12 venv：pytest 9.0.3、PyYAML 6.0.3、jsonschema 4.10.3，與 CI 釘選一致。
- Rust 1.97.0，`CARGO_TARGET_DIR=/tmp/solrev-p7-target`，`cargo build --locked --workspace --bins`；artifact 快照到 worktree 外。

**本輪觸及的測試**

- 以下共 230 passed：
  - `tests/test_p7_gate_report.py`
  - `test_p7_ab_report.py`
  - `test_p7_gate_win_argv.py`
  - `test_contracts_catalog.py`
  - `test_acceptance_cases.py`
  - `test_p7_od56_ci_allow.py`
  - `test_resource_report.py`
  - `test_p7_invariants.py::test_evidence_insufficient_is_not_pass`
- 新黑箱 `test_force_recover_attached_closes_before_reopen`：A（Python）4/4、B（Rust）6/6 passed。
- 回歸紅綠：把 a04aa69 的 `tests/test_p7_gate_report.py` 疊到 b85eec5 的 `git archive` 樹上，`test_report_records_tool_versions` 為 FAILED（`KeyError`）。

**突變**

只在 worktree 外的 `git archive` 副本中進行，設計與結果見 A 節 P7-f9e90a9-5 說明。

- Rust：close 延 8 ms 為 6 FAILED、延 30 ms 為 3 FAILED、自然時序為 5 PASS。
- Python：6 PASS，原因已說明。

**L1 鎖**

- b85eec5→a04aa69 只動了三個集合，皆為 add-only，selector 與非集合欄位不變，沒有任何移除：
  - `p7_gate:contract-blackbox-p7-a`：+1
  - `p7_gate:contract-blackbox-p7-b`：+1
  - `p7_gate:python3-m-pytest-q-tests`：+2
- 新節點的同檔兄弟節點所在的每個集合，新節點都已加入。
- `regen_lock.py --check --base b85eec5` 與 `--check` 皆 OK。

**其他檢查**

- `tools.contracts check`：ok。
- `check_cfg_boundary`：無輸出，rc 0。
- `bash -n p7_gate.sh`：通過。
- `git diff --check b85eec5 a04aa69`：乾淨。
- policy_check（Python 3.12 venv）：25 pass／0 fail／2 warn（R-19、R-22）。

**未執行**

- 完整全套 pytest、全 workspace cargo test、原生 Windows、`p7_gate.sh` 全流程。

審查 worktree 未修改：HEAD 為 `a04aa69`，工作樹乾淨。

結論：需修正
