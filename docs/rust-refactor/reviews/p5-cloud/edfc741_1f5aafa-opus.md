## 審查：feature/rust-refactor-p5 `edfc741..1f5aafa`（3 個 commit）

**結論：** 共 4 項 Medium、3 項 Low，沒有 High。最主要的問題是兩處新的對外差異：一處是本範圍新造成的回應回歸（`session clear`），另一處是 banner 情境下舊世代登入結果與 Python 不同，且這個差異已寫進 L1 鎖。

**我實際跑過的項目：**
- **紅燈先行確認**：把 `session_core.rs` 換回 `edfc741` 版本後，兩個新增回歸測試確實會紅燈：`clear_reopen_attaching_clears_last_error` 得到 `left=Str("CLEARED")`，`attach_settles_attached_after_quiet_revokes_probe` 得到 `left=ATTACHING`。換回 HEAD 後，`login_quiet` 33、`drain_release` 65、`session_core` 56 全數通過。
- **lock 檔**：`tests/acceptance/l1_required.lock.json` 只新增 3 行，沒有刪除或修改。
- **平台 cfg**：這個範圍沒有任何 `cfg(` 變更。
- **其他檢查**：`tools.contracts check` 通過；`policy_check` fail 0。
- **Windows 原生**：這幾個 commit 沒有宣稱任何 Windows 原生項目完成，只寫了 `cargo check windows-gnu`。
- **黑箱對照**：用 HEAD 的 debug binary 與 Python 基線，在 FakeTarget 上逐一實測（細節見下方各項）。
- 審查用的暫存目錄與建置產物都在 `/tmp`，已刪除；worktree 沒有改動。

---

### 18abb03 fix(sw-core): attach probe 遇開機 banner 撤銷時落定 ATTACHED，不再永久停在 ATTACHING

1. **[Medium] `crates/sw-core/src/session_core.rs:2381-2391、2671-2686`；`crates/sw-core/tests/login_quiet.rs:1196-1199`**
   - **問題：** 收尾時完全不看舊世代 run 回報的結果，一律落定 `ATTACHED`＋`PROMPT_UNAVAILABLE`。Python `_attach_by_id` 不會以登入世代丟棄 probe 結果，而是直接套用結果（`session_manager.py:4730-4855`）。具體輸入與預期：
     - (a) attach 的 Probe run 期間收到 banner，`LoginFinished{fsm_gen: probe_gen, result: Ready}`：
       - Python：`READY`、`last_error=null`，並由 `confirm_ready()` 解除 quiet。
       - Rust：`ATTACHED`／`PROMPT_UNAVAILABLE`。
     - (b) EnsureReady run 的 TX 已全部送出才收到 banner，回 `Failed("LOGIN_PROMPT_TIMEOUT")`：
       - Python：沒有被 fence 拒絕，走 `DETACHED`＋`LOGIN_PROMPT_TIMEOUT`＋detail。
       - Rust：`ATTACHED`／`PROMPT_UNAVAILABLE`。
     - (c) Probe 回 `NotReady("READY_NONCE_TIMEOUT")`：
       - Python：`ATTACHED`＋`READY_NONCE_TIMEOUT`＋精煉後的 detail。
       - Rust：`PROMPT_UNAVAILABLE`、detail 為 null。
     - **時序**：落定延到下一個 1 s Tick。我在 `banner_target` 上兩邊各跑 3 次：Python 在 t≤0.5 s 已是 `ATTACHED`；Rust 在 t=0/0.5 s 仍是 `ATTACHING`，t≈1.0 s 才變 `ATTACHED`。
   - **為什麼要處理：** 新測試把 (a) 的非 Python 行為（Ready 結果卻斷言 `ATTACHED`）寫進 L1 鎖。OD-21 規定「之後新增的對外差異一律須擁有者逐項核准」，而 parity-amendments #49 已確立同類先例：以 run 自身的 `(epoch,fsm_gen)` 比對，在途 run 的結果必須套用。這個 commit 只在訊息裡寫「列為擁有者裁決」，並沒有登記成 OD／SC。
   - **修法：**
     - 在 `login_finished` 中，對 `kind==Attach` 且 `run.epoch==epoch && run.fsm_gen==fsm_gen` 的結果，立即走既有的 Ready／NotReady／Failed 分支，不延到 Tick，對應關係與 Python 一致。
     - 這會與計畫 T11 的 `boot_banner_invalidates_old_fsm_and_owner_generations`（舊結果無效果、維持 ATTACHING）衝突，必須依 §10.4 提請擁有者裁決並修訂該測試。
     - 裁決前，先從 `attach_settles_attached_after_quiet_revokes_probe` 移除 `LoginOutcome::Ready` 這一輪，或標記為待裁決，不要把非 Python 行為鎖進 L1。
     - 補 Failed 與 NotReady(非撤銷碼) 兩個案例。

2. **[Low] `changelog.d/rust-s21-blackbox.md:5`**
   - **問題：** 寫「改與 Python 一致落定 ATTACHED」，但 commit 本身承認還有差異（時序、舊世代 Ready），上一項的 (b)(c) 也不一致。
   - **修法：** 改成「不再永久停在 ATTACHING；與 Python 的剩餘差異待擁有者裁決（見 OD-xx）」。

### 9363d9b fix(sw-core): session clear 重新開埠進 ATTACHING 時清除 last_error

1. **[Medium] `crates/sw-core/src/session_core.rs:2937-2941`**
   - **問題：** `set_last_error(None)` 放在回覆等待者（`for waiter in stop.waiters`）之前，導致 `session clear` 的 RPC 回應從 Python 的值變成 null，這是本 commit 新造成的回歸。我實測 READY 狀態下執行 `serialwrap session clear --selector COM0`，各跑 3 次：
     - Python：3/3 為 `{"ok":true,"session":{"state":"ATTACHING","last_error":"CLEARED"}}`。原因是 drain 收尾設 CLEARED 並 `done.set()` 後，RPC 執行緒立刻回應；attach 執行緒要等預先開埠之後才清 `last_error`。
     - Rust（HEAD）：3/3 為 `last_error: null`。
     - 修正前，Rust 的回應本來就是 `CLEARED`。
   - **修法：**
     - 把清除移到回覆迴圈之後，先以 `ok_session` 回 `CLEARED`，再 `set_last_error(None, None)`。
     - 在 `drain_release::clear_reopen_attaching_clears_last_error` 加斷言：回覆的 `session.last_error == "CLEARED"`，之後 `public()` 為 null。
     - 另外，`contracts/session_last_errors.yaml:99` 的 CLEARED `states: ["DETACHED"]` 沒有涵蓋 Python 回應中的 `ATTACHING`，應交契約維護者 add-only 補上。

### 1f5aafa test(contract): Slice 2.1 黑箱 A/B 正規化映射與兩處預寫案例補強

1. **[Medium] commit 訊息「驗證」段**
   - **問題：** 證據無法追溯，使用的 artifact 也不符計畫。
     - 兩輪結果都只寫「見交付報告」，沒有路徑；「仍失敗且需擁有者裁決的預寫案例」也沒有列出 nodeid。repo 內搜不到這份交付報告。
     - 用的是 `target/debug`，但 06 Task 20 的 Interfaces 與 §15.9.1 規定 A/B 固定用 `target/x86_64-unknown-linux-gnu/release/*`。
     - 我在 HEAD 實測 `test_safety_core.py::test_reject_paths_zero_tx`：Rust 輪 4 failed、Python 輪 6 failed（含 `READY_UNCONFIRMED`、`AUTOBOOT_QUIET`）。Step 4「兩個 artifact 全部 PASS」顯然沒有達成，但 repo 裡看不出來。
   - **修法：**
     - 把交付報告納入版控（例如 `docs/rust-refactor/reviews/p5-t20-blackbox-report.md`），內容逐 nodeid 列出兩輪的 PASS/FAIL、確切命令、artifact 路徑與 sha256、失敗案例的 Python 對照與待裁決編號。
     - 用 release artifact 重跑。
     - Task 20 在完成前標示為未完成。

2. **[Medium] `contracts/ab_normalization.yaml:21-24`（及其他 key 映射）**
   - **問題：** 沒有規定 null／型別要保留。依現有寫法，一邊是 `last_ready_at: null`、另一邊是時間戳時，兩者都會被換成 `<TS>`，真正的差異被抹掉。這違反檔頭第 8 行自己寫的「不得以寬鬆比對抹除真正差異」。
   - **修法：** 每條映射加 `apply_to: non_null_string`（`pid` 用 `non_null_int`），值為 null 或型別不同時保留原值並視為差異；修改後提升 `approved_revision`，並由獨立審閱者填入 `reviewed_by`。

3. **[Low] `contracts/ab_normalization.yaml:39-42`**
   - **問題：** Python 對外輸出裡沒有 `nonce` 這個 JSON 鍵。nonce 只出現在 TX 位元組或 WAL `payload_b64` 裡，所以這條具名映射實際上不會作用；`existing_pid`（`cli.py:540`）也沒有涵蓋。
   - **修法：** 註明 WAL TX 中的 nonce 依 §15.6.3 以位元組流對照處理（或由擁有者核准專用規則）；`pid` 映射補上 `existing_pid`。

4. **[Low] `tests/contract_blackbox/slice21/test_slice_scope.py:17-24`**
   - **問題：** `except ImportError: AB_ENV = {}` 會在匯入失敗時靜默回退成空 label，Rust 輪會再度恆 skip，等於重現原本要修的缺陷。
   - **修法：** 改用既有的 `from bb_harness import ab_label`，或在 ImportError 時直接 raise／`pytest.fail`，不要回退。

   另外，`test_activity_classification` 改讀 `session.activity_classification`，與 `regression/.../f04_session_semantics.py:56` 承接的 L2 case 一致，又新增了 `ok`／`_rc` 斷言，屬於收緊、不是放寬，沒有問題。

---

### High／Medium 摘要

| 等級 | commit | 位置 | 問題 | 修法 |
|---|---|---|---|---|
| Medium | 18abb03 | `session_core.rs:2381-2391、2671-2686`；`login_quiet.rs:1196-1199` | banner 撤銷的 attach run 不看結果一律 ATTACHED／PROMPT_UNAVAILABLE，且延到 Tick（實測 ≈1 s，Python ≤0.5 s）；舊世代 Ready／Failed／NotReady 與 Python 不同，非 Python 行為已鎖進 L1，未經 OD-21 核准 | 依 run 自身的 `(epoch,fsm_gen)` 立即套用結果；T11 測試衝突提請擁有者依 §10.4 裁決；裁決前移除 Ready 這輪斷言 |
| Medium | 9363d9b | `session_core.rs:2937-2941` | `session clear` 回應的 `last_error` 由 Python 的 `CLEARED` 變成 null（實測 3/3），本 commit 造成的回歸 | 清除移到回覆迴圈之後；測試加回覆 `CLEARED` 斷言；契約 `states` add-only 補 ATTACHING |
| Medium | 1f5aafa | commit 訊息驗證段 | 「見交付報告」無路徑、失敗 nodeid 未列、用 debug 而非 release artifact；HEAD 實測 Rust 輪仍有 4 failed，Step 4 未達成 | 交付報告入版控（逐 nodeid、命令、artifact sha256）、用 release 重跑、Task 20 標示未完成 |
| Medium | 1f5aafa | `contracts/ab_normalization.yaml:21-24` 等 | 映射未保留 null／型別，會抹掉 null 與時間戳之間的真正差異 | 加 `apply_to: non_null_*`，提升 `approved_revision`，填 `reviewed_by` |

High：無。
