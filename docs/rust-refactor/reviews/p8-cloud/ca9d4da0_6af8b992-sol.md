> 審查者：claude-fable-5-1（重型任務，擁有者 2026-11-07 指定）

## A 前輪 finding 核對

無（本輪無前輪報告）。

## B 新發現

審查範圍為 3ec29f1c（public 欄位）與 6af8b992（L1 登記）；ca9d4da0 為本包第 1 項、是本範圍的基底，只在追查影響時參考。

核對結果摘要（皆對照 origin/main `sw_core/session_manager.py`，與工作樹 Python 無差異）：

- **鍵位置**：Python 在 `ready_reconfirm_remaining_s` 之後、`idle_for_ms` 之前插入；Rust `session_core.rs:1023` 同位置。`contracts/rpc_schema.json` 的 `x-order`（:101）與 `required`（:491）皆已含此鍵，Rust 投影由此補齊先前缺的 required 鍵。
- **值語意**：Python `boot_evidence_wait_active`＝`until > 0 and now < until`，有效時 `round(until - now, 1)`；Rust `quiet_gate.rs` 的 `boot_evidence_wait_active`／`boot_evidence_wait_remaining_s` 逐條相同，active 守衛保證減法不下溢。`py_round` 走 `format!("{:.1}")` 的 half-even，測試中 0.25→0.2、0.35→0.3、0.05→0.1 與 CPython 一致。
- **正式呼叫路徑**：欄位加在既有 `SessionCore::public()`，該函式為 `session.*` RPC 回應的唯一投影（`session_core.rs:1686-1731,1981,1998`），非新增孤立介面。
- **transient**：Rust 無任何地方把 `QuietState` 寫入 state.json（grep 僅 `quiet_for_test`／fence 同步），與 Python「不進 `_save_state`」一致。
- **測試**：`public_dict_key_order_with_sc04_fields` 與 `session_public_fields_p7` 僅插入新鍵、仍做完整序列比對，未削弱。`wp56_public_wait_remaining_matches_python` 覆蓋未開始、`until == now`、整段寬限、捨入、到期前 1 ns、恰到期與讀取無副作用。
- **L1 鎖**：s21_core／linux 清單 1774→1775，僅新增 1 筆、仍排序，`regen_lock.py --check` 通過。
- **定向實測**（CARGO_TARGET_DIR=/tmp/solrev-p8-rust-refactor-p8-target）：

| 測試檔 | 結果 |
|---|---|
| sw-core `session_core`／`quiet_gate_p7`／`session_core_quiet_p7`／`reprobe` | 8＋6＋67＋21 passed，0 failed |
| sw-daemon `session_rpc_p7::session_public_fields_p7` | 1 passed |

**P8-6af8b99-1** [Low] docs/rust-refactor/inventory/05-session.md:172 — §public 的「依序 key」清單仍停在 `ready_reconfirm_remaining_s`→`idle_for_ms`，未加入 `boot_evidence_wait_remaining_s`；本項標題為「對齊 session public 欄位、鍵序與規格」，commit 只同步了 02-rpc §6.1，兩份 inventory 鍵序自此不一致。無執行期影響。修法：在該行 `ready_reconfirm_remaining_s` 後補 `boot_evidence_wait_remaining_s`（註 #243），並於 :167 附近補 `boot_evidence_wait_active(now=None)`／`begin_boot_evidence_wait`／`note_boot_evidence` 三個 accessor 的一行說明。

**P8-6af8b99-2** [Low] crates/sw-core/tests/session_core.rs:684（commit 3ec29f1c 訊息）— 訊息引用「rulings/s2.md 2026-11-08『對照 Python』例外」作為修改凍結鍵序測試的依據，但該檔不在 repo（`git ls-files` 無 rulings/ 目錄），審查者無法追溯。修改本身為對齊 Python 基線必要且未削弱斷言，故不影響結論；建議編排者在 rulings/p8.md 轉錄或引用該裁決，讓後續輪次可核對。

## 既有碼待辦（不計入結論）

- `crates/sw-core/src/session_core.rs:3648` 的 detach 路徑未清 `boot_evidence_wait_until`（Python `session_manager.py:2704` 會清）；屬本包第 5 子項「detach 清除生命週期」範圍。
- `begin_boot_evidence_wait` 尚無產品端呼叫（Python `:6411,6466` 的 reboot 路徑）、reprobe 規劃與 `_still_valid` 守衛（Python `:2222,2339,4746`）、self-test／submit 的 `boot_evidence_wait_active` gate（Python `:7816`）均未接；屬本包第 3～5 子項。

## 對裁決的異議（不計入結論）

無。

結論：可放行
