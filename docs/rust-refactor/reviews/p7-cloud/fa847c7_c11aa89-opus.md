# feature/rust-refactor-p7 fa847c7..c11aa89 非實作者審查（Claude Opus）

- 工作樹：`/home/user/wt-p7`（HEAD＝c11aa89，審查後無任何改動；反證用 scratch worktree 已移除並 prune）
- 範圍：be89081（merge P5 b0f2853..1f5aafa，只審衝突解法與對 P7 的影響）、94526b4、c11aa89
- 依據：CLAUDE.md、`docs/rust-refactor/plans/08-slice23-login-events.md` §0.6／Task 3／Task 4（含 parity-amendments 追加表 #17、#49）、`docs/rust-refactor/plans/parity-amendments-2026-10-26.md`、Python 基線 `sw_core/login_fsm.py`、`sw_core/session_manager.py`、`sw_core/service.py`、`sw_core/uart_io.py`
- `/home/user/pa/docs/rust-refactor/reviews/p7-cloud/be89081_c11aa89-*.md`、`fa847c7_be89081-*.md` 都是 0 位元組的空檔，沒有可參考的既有結論。以下結論都由我自己驗證。

## 實際執行的驗證

| 命令 | 結果 |
|---|---|
| `cargo test -p sw-core --features test-fakes --test quiet_gate_p7 --test session_core_quiet_p7 --test login_quiet --test session_manager --test tx_writer --test session_core` | 全綠：5／8／36／43／37／56 passed（#49 `console_unlock_arm_mid_login_finish_applied` 已轉綠） |
| `cargo test --workspace --features test-fakes --no-fail-fast` | 失敗 4 項，都是以 root 執行造成的環境差異：`unreadable_config_isolated_fail_closed`、`socket_probe_eacces_is_socket_unavailable`、`file_eacces_matches_golden`、`unix_fixture_rows_match_python` |
| 同上（第一次跑，沒加 `--no-fail-fast`） | `lease_selftest::selftest_probe_drain_race_golden` 在第 629 輪偶發失敗（`partial 0/23 [Errno 125]` 與 `not_submitted 0/23 draining` 不符）。單獨重跑 3/3 綠。此測試在 fa847c7 之前就已存在（8b8a40f），不屬本範圍 |
| `cargo check --workspace --target x86_64-pc-windows-gnu --features test-fakes --tests`；`cargo check --workspace --target x86_64-pc-windows-gnu` | 皆綠（只有既有 warning） |
| `python3 scripts/rust/check_cfg_boundary.py` | rc=0。在 `crates` 範圍 diff 中排除 sw-pal 後，沒有新增的 `cfg(target_os/windows/unix)` |
| `python3 tests/acceptance/regen_lock.py --check --base {fa847c7,be89081}` | FAIL，唯一的消失項是 `s21_core.{linux,windows} …detect_skips_passthrough_console_unlock_and_unsupported` |
| `regen_lock.py --check --base be89081 --allow-removal cargo:sw-core::session_manager::detect_skips_passthrough_console_unlock_and_unsupported` | OK |
| `regen_lock.py --check --base 94526b4` | OK（c11aa89 只新增） |
| `python3.12 scripts/rust/capture_p7_goldens.py --out <tmp>`，再 `cmp` `quiet_rx`／`gate_responses` | 兩者逐位元組相同（預設的 `python3` 是 3.11，`tests/test_p7_goldens_fresh.py` 會因版本守衛 exit 2；python3.12 沒裝 pytest，所以改為直接執行擷取腳本） |
| `python3 -m pytest -q tests/contract_blackbox/test_bb_faketarget_p7_selftest.py tests/contract_blackbox/test_bb_harness_selftest.py` | 17 passed（合併後的 bb_faketarget 兩側自測都綠） |
| 反證 1（94526b4）：在 scratch worktree 把 `session_manager.rs` 還原成 94526b4^ | `detect_skips_passthrough_and_console_unlock` 紅（left `"fallback"`，right `"detected"`） |
| 反證 2（c11aa89）：只還原 `session_core.rs` | `session_core_quiet_p7::{console_unlock_arm_keeps_auto_probe_open, console_unlock_attach_during_quiet_still_starts_login}` 紅，`login_quiet::console_unlock_arm_mid_login_finish_applied` 紅，其餘綠，與 commit 宣稱一致 |
| 反證 3（c11aa89）：只刪掉 `on_rx_text` 的 passthrough 早退 | `quiet_gate_p7::rx_sequences_match_python_golden` 紅（`case quiet_rx:009 t=0 until`）。但 `session_core_quiet_p7::passthrough_banner_never_arms_quiet` 仍綠，因為 session_core 在 `apply_quiet` 已先排除 passthrough，所以這個測試只是回歸測試 |
| 反證 4（c11aa89 新問題，見 C-1）：scratch 測試，console unlock profile：ATTACHED → banner arm → `SessionAttach` 再探 | 輸出 `still_quiet=true fence.quiet_active=false`，`Fence::admit(SystemRecoverKey)` 回 `Ok(())`，Python 對應路徑會回 `quiet` |

---

### be89081 merge: 同步 P5 T15～T19 至 P7

衝突檔只有 11 個：`arbiter.rs`、`session_core.rs`、`session_manager.rs`、`drain_release.rs`、`login_quiet.rs`、`session_manager.rs`（tests）、`daemon_main.rs`、`service.rs`、`rust-architecture.md`、`l1_required.lock.json`、`bb_faketarget.py`。我逐段比對了 `git show --cc be89081`：

- `SessionRequest` 同時保留 P6 的 `Console*` 與 P5 的 `SetProfile`／`ForceStateForTest`，正確。
- `login_quiet.rs` 兩側的尾段並存，P7 的 #49／帳密延後解析／TxIncomplete 測試沒被改動，P5 的 `attach_settles_attached_after_quiet_revokes_probe` 也完整保留，36 項全綠。
- bb_faketarget 自測 17 passed。
- workspace 只剩環境性失敗。P7 既有行為沒被破壞。

發現：

1. [Low] `crates/sw-core/tests/session_manager.rs:681-704`（`session_attach_paths`）
   - 問題：merge 時修改了 P5 既有測試的斷言，`last_error` 由 `Null` 改成 `"PROMPT_UNAVAILABLE"`，「ATTACHED 且 last_error None → 頂層兩欄 null」這個分支的覆蓋就此消失。08 計畫第 42 行規定「P5 既有測試除 §0.6 明列的更正與退役外不得修改」，而 §0.6 退役清單沒有列這個測試。新斷言本身符合 Python（`_classify_non_ready_state` 回 `PROMPT_UNAVAILABLE`，`login_fsm.py:132-137`），不是削弱成錯值；變數名 `GATED_PROFILE` 也已名不符實。
   - 修法：
     - 在 08 §0.6 退役清單補一列「06 T16 `session_attach_paths` 的 null 子案：隨 Slice 2.1 閘退役改斷言 PROMPT_UNAVAILABLE；null 子案待 P6 human lease 補回」，並在 evidence 留追蹤條目。
     - 把 `GATED_PROFILE` 改名為 `POST_LOGIN_PROFILE`。
     - P6 human lease 可用時，以「human lease 持有中的 capable ATTACHED」補回 null 子案。
2. [Low] `crates/sw-core/src/session_manager.rs:1836-1843`
   - 問題：merge 接合時，P6 的 `Effect::Console*`／`HumanTx`／`LeaseIndex` 在 production executor 是 `todo!()`。目前 `SessionCore` 不產生這些 effect（`console_request` 由 P6 Task 10 交付），所以無法觸發；但 P6 合入前只要有任何路徑產生它們，daemon 的 actor executor 會直接 panic。
   - 修法：改成 `self.shared.warn(...)` 加上 `debug_assert!(false, "P6 Task N")`，或保留 `todo!` 但在 `tests/test_p7_skeleton_boundary.py`（或 `check_p7_prereqs`）登記這三個 todo，並在 P6 合入時強制檢查。

### 94526b4 refactor(rust-p7): 動態偵測依 08 §0.6 恢復 post_login_cmd 模板為候選

與 Python 1:1 驗證：`login_fsm.detect_template`（`sw_core/login_fsm.py:348-350`）只跳過 `platform == "passthrough" or tpl.console_unlock_env`。Rust 移除 `|| !tpl.post_login_cmd.is_empty()` 後一致。

- 具體輸入：模板依序 `pt-template`（passthrough、`.*`）、`cu-template`（console_unlock_env）、`pl-template`（`post_login_cmd: sh`），PROBE 回 `root@x:/# `。
- 預期 Python 輸出：命中 `pl-template`，`profile_source=detected`。
- 新測試同時斷言 `profile=="pl-template"`，所以 passthrough 與 console unlock 的略過仍被覆蓋（若沒略過，就會先命中 pt 或 cu）。斷言沒有削弱。
- 反證 1 確認還原後測試會紅。

鎖：
- 移除 `s21_core.{linux,windows}` 舊 nodeid，有 08 §0.6 退役清單第 3 列逐名授權（`08-slice23-login-events.md:169`）。
- linux 新增到 `s21_core` 與 `p7_gate:cargo-test-workspace`。
- `--base 94526b4^ --allow-removal <舊名>` 通過。

發現：

1. [Medium] `tests/acceptance/l1_required.lock.json`（`s21_core.windows` 集合）
   - 問題：Windows 必要集合把 `detect_skips_passthrough_console_unlock_and_unsupported` 逐名移除，卻沒有補登替代的 `detect_skips_passthrough_and_console_unlock`。fa847c7 的 `unsupported_profile_no_auto_tx` 也一樣，Windows 端沒有 `post_login_cmd_profile_logs_in`。
   - 結果：Windows 必要集合淨減 2 項。commit 訊息寫的「LOCAL-WINDOWS-TODO」在 repo 中找不到任何追蹤條目（`grep -rn LOCAL-WINDOWS-TODO` 在 docs／acceptance 都沒有命中）。§0.6 的授權是「改名」，不是「刪除」，所以 Windows 端等同在沒有可追溯紀錄的情況下縮小了必要集合。
   - 修法：
     - 在 Windows 原生跑一次 `cargo test -p sw-core --features test-fakes -- --list` 取得 collect 輸出，再用 `regen_lock.py --l1-add-only --set s21_core --platform windows --collect <out>` 補登兩個新 nodeid。
     - 在原生 runner 補登前，於 `docs/rust-refactor/reviews/p7-fix-responses.md`（或 evidence index）新增明確的 LOCAL-WINDOWS-TODO 條目，列出兩個 nodeid 與負責 Task，並在 P7 PR 的 owner 待辦一併列出。

### c11aa89 feat(rust-quiet): 完成 Ready Reconfirm 有界化與 Quiet gate 回應

與 Python 1:1 驗證（通過的部分）：
- `boot_quiet_blocks_probe` 對應 `_boot_quiet_blocks_probe`（`session_manager.py:1731-1733`）。空字串 env 視為 falsy，有測試覆蓋。
- `agent_gate_active` 對應 `session_manager.py:717-729`。
- `execute_gate_reject` 對應 `_agent_gate_reject_locked`（`:1654-1697`）：鍵序 `ok,error_code,selector,hint,recommended_action`／`ok,error_code,selector,retry_after_s`，`round(max(until-now,0),1)`。
- `submit_gate_reject` 對應 `service.py:784-826`。
- 32 列 `gate_responses` 與 13 列 `quiet_rx` golden 已用 python3.12 重新擷取，與 repo 逐位元組相同。
- Fence 端：Python `uart_io.py:635-649` 中，`system_probe` 的 login／ready 走 `_gate_probe_quiet_until`（console unlock 為 0），`system:recover` 與 `self_test_probe` 走 `_gate_self_test_quiet_until`（不豁免）。Rust 新拆的 `LoginProbe|ReadyReconfirm`（看 `probe_quiet_exempt`）與 `SystemRecoverKey|SelfTestProbe`（只看 `quiet_active`）與此一致。
- #49：`arm_boot_quiet` 對 console unlock 不遞增 `login_gen`（`:597-598`）。Rust 的 `exempt_bumps` 加上 `login_gen_current` 讓在途 run 的結果被套用，結果一致。反證 2 確認修前紅、修後綠。
- reboot recovery 的延長 `min(max(deadline, until+timeout), cap)` 與 `session_manager.py:5903` 一致。

發現：

1. [Medium] `crates/sw-core/src/session_core.rs:2592-2596`（既有 bridge 再探）與 `:4538-4543`（reboot recovery 起 run）：fence 靜默狀態被錯誤清除
   - 問題：這兩處原本只在 quiet 不在進行中時才會執行，所以照舊寫 `f.quiet_active = false; f.probe_quiet_exempt = false;`。本 commit 改用 `boot_quiet_blocks_probe` 之後，console unlock profile 在 quiet 進行中也會走到這裡，結果 quiet 仍在進行中時 fence 的 `quiet_active` 被清成 false，最長維持到 180 s quiet 到期或解除為止。這打破了 commit 自己宣稱的不變式「SystemRecoverKey／SelfTestProbe 仍以 quiet_active 擋（R5-F2）」。
   - Python 對照：console unlock 只把 `_gate_probe_quiet_until` 設 0，`_gate_self_test_quiet_until=boot_quiet_until` 不變，所以 `system:recover`（CTRL-C／CTRL-D 救援鍵）與 self-test probe 在 quiet 期間仍回 `quiet`（`uart_io.py:635-649`）。
   - 實測（反證 4）：console unlock、ATTACHED，RX 送 `U-Boot 2024.04`，再送 `SessionAttach`。結果 `boot_quiet_remaining_s` 非 null，但 `fence.quiet_active=false`，`Fence::admit(SystemRecoverKey)` 回 `Ok(())`，Python 會回 `quiet`。
   - 可觀察性：目前 Command 類仍被 `reconfirm_pending` 擋，self-test 有 core 層的 `quiet_active(now)` 預檢（`:4923`），所以暫時沒有外部可觀察差異。但 fence 是 TX 線性化的最後防線，P7 Task 7／force recover 在 ATTACHED 送救援鍵時就會在 U-Boot autoboot 倒數中送出 bytes。
   - 修法：兩處改成「保留現行 quiet」：
     ```rust
     let qa = self.quiet.quiet_active(now);
     f.quiet_active = qa;
     f.probe_quiet_exempt = qa && !boot_quiet_blocks_probe(&self.quiet, &self.profile, now);
     ```
     再補回歸測試 `console_unlock_reprobe_during_quiet_keeps_recover_key_blocked`：用上述序列斷言 `Fence::admit(SystemRecoverKey|SelfTestProbe) == Err(Quiet)`，而在途 run 的 `LoginProbe` 為 `Ok`。reboot recovery 路徑也各寫一案，並以 `regen_lock --l1-add-only` 登記。
2. [Medium] `crates/sw-core/tests/session_core_quiet_p7.rs`（整檔）：計畫要求的測試缺列，commit 卻宣稱「完成」
   - 08 Task 4 Step 1 表格與 parity 追加表列出的測試中，以下在全 repo 都找不到（`grep -rn "fn <name>"` 無結果），而 commit 只聲明了 3 列沿用 06 與 1 列 BLOCKED-UPSTREAM：
     - parity 追加表 **#17** `passthrough_reattach_clears_previous_profile_quiet_and_reconfirm`：標為「必要」的完成條件（parity-amendments 第 53 列，ADD-TO-PLAN P7-T4），完全沒有，也沒聲明。另外，Python 的 detach（`session_manager.py:2513-2530`）不清 `boot_quiet_until`／`ready_reconfirm_pending`，只在 passthrough 重掛時才由 `reset_passthrough_boot_gate`（`:644-654`，`:4858`、`:5540`）清除；Rust 則在每次 stop 時 `self.quiet = QuietState::default()`（`session_core.rs:2934`，0a187c1 引入，不屬本範圍）。也就是說，DETACHED 期間 `session.list` 的 `boot_quiet_remaining_s`、`ready_reconfirm_pending`，以及 180 s 內重插時是否擋 probe，兩邊可能不同。#17 測試若以 Python oracle 撰寫，會直接暴露這個差異。這是我依程式碼推論，Python 端沒有實跑。
     - `confirm_ready_clears_all`：沒有聲明。
     - `bootloader_tail_blocks_clear`：brcm `CFE> ` 尾行不解除 quiet，沒有聲明。
     - `quiet_rx_runs_during_foreground_busy`：沒有聲明。
     - `execute_time_quiet_has_retry_after_s`：37.26 s 應得 37.3。golden 的 execute 列只有整數值 120.0／0.0／5.0，沒有覆蓋 `py_round` 的進位。
     - `rpc_human_source_is_treated_as_agent`：只在 submit 層由 golden 010／013 間接覆蓋，SessionCore 層沒有。
     - `passthrough_banner_restarts_quiet_window`、`passthrough_ready_never_gated`：commit 稱「沿用 06 既有」，但沒有指名是哪些 06 測試，追溯不到；06 的 `passthrough_rx_restarts_quiet_window`、`passthrough_ready_not_demoted_by_rx_or_banner` 沒有斷言 banner 後立即 Submit 進 Arbiter。
     - `banner_arm_bumps_fsm_gen_once_and_invalidates_human_input`：以縮減名 `banner_arm_bumps_fsm_gen_once` 入鎖（已聲明 BLOCKED-UPSTREAM，可接受，但完整名稱需日後另行 add-only 補登）。
   - 修法：
     - 依計畫名稱補齊上列測試，斷言值以 Python 實測為準：#17 用 `test_p1oc_sc35_state.py` 的 oracle；retry 用 `round(37.26,1)==37.3`。
     - 用 `regen_lock --l1-add-only` 登記。
     - 在 commit／`p7-fix-responses.md` 逐列標明「沿用的 06 nodeid」或「BLOCKED-UPSTREAM 依賴項」。
     - 補齊之前，commit 不得以「完成」描述 Task 4。
3. [Low] `crates/sw-core/tests/session_core_quiet_p7.rs:190-206`、`:208-220`：斷言比計畫弱
   - `passthrough_banner_never_arms_quiet`：計畫要求 uboot-template 與 others-template 各一，並在 ATTACHED 與 READY 下各收一次 banner。實作只測單一 passthrough profile 的 ATTACHED。
   - `stale_login_finished_is_ignored`：計畫要求「不清 pending」，但測試沒先 arm pending，也沒斷言 `ready_reconfirm_pending`。
   - 修法：前者改成 2 profile × 2 狀態的迴圈；後者先 `to_ready()`，送 banner、清 quiet，在 pending 狀態下注入舊世代 `LoginFinished{Ready}`，再斷言 `ready_reconfirm_pending==true`、`state` 不變。
4. [Low] `crates/sw-core/src/quiet_gate.rs:256-303`、`:305-334`：新的純函式沒有被 production 使用
   - 問題：`submit_gate_reject`／`execute_gate_reject` 在 production 沒有任何呼叫點（`grep` 只在 tests 命中）。實際 RPC 路徑仍走 `session_core.rs:1687-1745`、`:3535-3551` 的內嵌重複實作，所以 golden 位元組相等只證明了未使用的函式，兩份實作日後可能漂移。
   - 另外，`truthy()` 不是 Python truthiness，`0`／`""`／`[]` 會被視為真；目前輸入都來自 `public()` 的 bool，所以不可達。
   - 修法：讓 `session_core` 的 submit gate 呼叫 `submit_gate_reject(&public, source)`、execute gate 呼叫 `execute_gate_reject`（passthrough 與 human 判定留在呼叫端），刪掉內嵌重複；或至少新增測試，以同一組 `gate_responses` 輸入驅動 SessionCore 的 `Submit`，比對位元組。`truthy` 改用 `sw_proto` 既有的 `py_truthy`。
5. [Low] `crates/sw-core/src/session_core.rs:4504-4520`、`:2579-2596`：兩處判定變更沒有測試
   - 問題：reboot recovery 與既有 bridge 再探改用 `boot_quiet_blocks_probe`，但沒有 console unlock 的測試。只有 attach 路徑由 `console_unlock_attach_during_quiet_still_starts_login` 覆蓋，把這兩處還原也不會有任何測試變紅。
   - 對應的 Python 行為：console unlock 在 quiet 中，reboot recovery 會立即送 probe，不延長 deadline（`:5889-5917`）；ATTACHED 時 `session.attach` 會再探（`:4449`）。
   - 修法：各補一個 SessionCore 測試，斷言 quiet 中仍產生 `StartLogin`、`rr.deadline` 未延長，並與 C-1 的回歸測試合併撰寫。

## High／Medium 摘要

| 嚴重度 | Commit | 位置 | 摘要 |
|---|---|---|---|
| Medium | 94526b4 | `tests/acceptance/l1_required.lock.json` `s21_core.windows` | Windows 必要集合逐名移除舊 nodeid 卻沒補登替代（連同 fa847c7 共淨減 2 項），LOCAL-WINDOWS-TODO 在 repo 中無追蹤條目 |
| Medium | c11aa89 | `crates/sw-core/src/session_core.rs:2592-2596`、`:4538-4543` | console unlock 在 quiet 中起再探／reboot recovery run 時把 fence `quiet_active` 清成 false，SystemRecoverKey／SelfTestProbe 在 quiet 期間被 fence 放行（Python `self_test_quiet_until` 仍擋）；已用 scratch 測試實證 |
| Medium | c11aa89 | `crates/sw-core/tests/session_core_quiet_p7.rs` | 計畫必要測試缺列且未聲明：parity #17 `passthrough_reattach_clears_previous_profile_quiet_and_reconfirm`、`confirm_ready_clears_all`、`bootloader_tail_blocks_clear`、`quiet_rx_runs_during_foreground_busy`、`execute_time_quiet_has_retry_after_s` 等；另 Rust detach 清 quiet 與 Python 保留的差異（範圍外，0a187c1）會由 #17 暴露 |

High：0。Medium：3。Low：7（be89081 有 2、c11aa89 有 5）。
