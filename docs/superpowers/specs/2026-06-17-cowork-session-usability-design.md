# Co-work Session 可用性設計（#51 + #53）

- 日期：2026-06-17
- 對應 issue：
  - [#53](https://github.com/hamanpaul/serialwrap/issues/53)（孤兒 minicom 假性佔用 `human_attached`/`interactive_owner`，無 idle/evict）
  - [#51](https://github.com/hamanpaul/serialwrap/issues/51)（passthrough/others-template 卡在 `ATTACHED`，`cmd submit` 回 `SESSION_NOT_READY`）
- 相關：[#52](https://github.com/hamanpaul/serialwrap/issues/52)（傳檔拖慢共用 console，fairness）—— 同屬 co-work 可用性，但**本設計不含 #52**。
- 狀態：設計（brainstorming 產出），待 user 審閱 → writing-plans。

本設計把兩個 issue 合併處理，因為兩者本質相同：**「session 看起來 attached，但實際不能用」**——
#53 是 human lease 退化成無限期硬鎖、#51 是 passthrough 永遠不到可下命令的狀態。

## 1. 背景與問題

### 1.1 #53 — co-work lease 失去釋放路徑

serialwrap 的 console 共用設計本意是**禮讓**，不是硬鎖：

- `sw_core/uart_io.py` 有 `_interactive_owner` / `_agent_active` / `_suspended_owner` / `_deferred_buffers`。
- `_handle_console_rx`（`uart_io.py:395-423`）：human owner 鍵入直接進 UART（line 401-403）；
  agent 命令注入期間（`_agent_active` 且該 client 為 `_suspended_owner`）human 鍵入改進
  `_deferred_buffers` 暫存（line 405-412），agent 結束後回放。`suspend_interactive()`
  （`uart_io.py:598`）/ `resume_interactive()`（`uart_io.py:609`）即此機制。

問題：human lease 一旦被持有，就**沒有 idle/staleness 判定、沒有自動降級**。

- `_lease_context`（`session_manager.py:1395`）的 `human_attached`（line 1399）只是
  「**有沒有 human lease**」的二元值，與「human 是否真的在輸入」無關。
- `_refresh_interactive_locked`（`session_manager.py:1362-1393`）**已有 liveness**：human lease
  的 console peer 不見了（`console_has_external_peer` 為 False）會 detach + 關 lease（line 1376-1382）。
  但 **alive 但閒置的孤兒 minicom**（peer 還開著）回 True → lease 永久留著。
- `idle_for_ms` 會被 broker 週期 probe 的 RX 一直洗小，是假訊號，不能拿來判 idle。

結果：一個閒置（甚至跨 9 小時）的孤兒 minicom 永久持有 `interactive_owner=human:...`，
agent 取不到 interactive 控制權，體感「卡住」。現況唯一解是手動 `console-detach` + `recover --force`。

### 1.2 #51 — passthrough 永遠不到可下命令的狀態

`_attach_by_id`（`session_manager.py:1025-1026`）對 passthrough 寫死 `ok=False`：

```python
if passthrough_only:        # profile.platform == "passthrough"
    ok = False
    err = None
```

於是 passthrough session 永遠落在 `ATTACHED`（line 1091-1093），`_probe_existing_bridge`
（line 920-924）對 passthrough 也只回現狀、不升 READY。`cmd submit` gate 需要 READY，
故回 `SESSION_NOT_READY`（`service.py:275`、`session_manager.py:1858-1921`）。

根因在 profile：`others-template` 的 `ready_probe: ""`（`profiles/default.yaml:62`）。
語意問題：`ATTACHED` 看起來像能用，但 `cmd submit` 完全不可用，且錯誤碼語意不清。

## 2. 目標 / 非目標

**目標**
- 閒置/孤兒的 human lease 不再無限期擋人；保留「禮讓、不洗掉 human 畫面」的原意。
- passthrough 與其他「只有 console、無 prompt」的 profile 明確表達「不可下命令」，錯誤碼清楚。
- 有可用 `ready_probe`（+ 非退化 `prompt_regex`）的 target 能進可下命令的 READY——**泛化到 U-Boot**。
- 在真機 COM1 上驗證 U-Boot command profile 可下 line 命令。

**非目標（本次不做）**
- #52 的 bulk transfer fairness / QoS。
- long-idle 自動清理 console（閒置已不擋人；真正清理交 agent 主動 `recover`/`console-detach`）。
- 改變 `human_attached` 既有語意（採 additive 新欄位，見 §3）。
- bootloader 互動/燒錄（#44 的 recovery lease 路徑維持原樣，本設計只新增 line-command 路徑並確認兩者並存）。

## 3. 關鍵決策（brainstorming 結論）

1. **#51 + #53 合併**為單一「co-work session 可用性」設計。
2. **#53 判準採「真實鍵入時間窗 + liveness」**：
   - active 視窗 `HUMAN_ACTIVE_WINDOW_S = 60s`（最後一次真人鍵入 ≤ 60s 才算持有）。
   - liveness：console peer 真的關了（沿用既有 `console_has_external_peer`）→ detach。
3. **idle 但活著的真人 → soft 降級**（沿用 `suspend_interactive`/`_deferred_buffers`，畫面與輸入不丟）；
   **liveness 判定已死的孤兒 → 直接 detach**。
4. **`human_attached` 不改語意（additive）**：新增 `human_active`（在視窗內），擋人/搶佔與
   `self-test recommended_action` 改看 `human_active`，降低破壞既有 consumer/測試風險。
5. **#51 開關 = `ready_probe` 非空 = command-capable**（取代 `platform == "passthrough"` 寫死）。
6. **error code = `PROFILE_NOT_COMMAND_CAPABLE`**（取代不清楚的 `SESSION_NOT_READY`）。
7. **READY 泛化**：READY 定義改為「對得上預期 prompt + `ready_probe` round-trip」，與底層是 OS 或
   bootloader 無關。U-Boot 只要綁對 profile 即可進 READY。
8. **U-Boot 本次正式做 + 真機驗證**（COM1）。

## 4. 詳細設計

### 4.1 #53 — co-work lease 釋放

**(a) 真實鍵入時間追蹤（`uart_io.py`）**
- 在 `_handle_console_rx` 的 human-owner 直送路徑（line 401-403）記錄 `last_human_input_at`
  （per console 或 per interactive owner；只算真人鍵入，排除 broker probe / agent 注入）。
- 經 `snapshot()` 對外暴露，供 session_manager 判 active。

**(b) active 判準與語意拆分（`session_manager.py`）**
- `_lease_context`（line 1395-1399）新增 `human_active`：
  `human_attached and (now - last_human_input_at) <= HUMAN_ACTIVE_WINDOW_S`。
- `human_attached` 維持 = 「有 human lease」。
- `self-test` 多回 `human_active`；`recommended_action` 依 `human_active`（idle 時不再以 human 阻擋）。

**(c) soft preempt（agent 取得控制權）**
- agent `interactive-open` / 需要 owner 的動作，遇到 human lease 但 `human_active=False` 時：
  以既有 suspend/deferral 將 human **降級**（非搶斷），agent 取得 owner；結束後 resume/回放。
- `human_active=True`（真人正在用）時維持現狀（agent 命令仍走 deferral，不奪互動 owner）。

**(d) liveness（死孤兒）**
- 沿用/確保 `_refresh_interactive_locked` 的 `console_has_external_peer` 路徑在 self-test 與
  acquire 前被呼叫：peer 已關 → detach console + 關 lease。
- 不新增 long-idle 自動清理。

### 4.2 #51 — command-capable 泛化

- **判準**：`command_capable = bool(profile.ready_probe.strip())`。
  - 註：`command_capable` 只 gate「能否嘗試進 READY」；**可靠的輸出框取另需非退化的 `prompt_regex`**
    （`others-template` 預設 `prompt_regex: ".*"` 是退化值，若只設 `ready_probe` 而不改 prompt，
    READY 可達但 line 輸出框取不可靠——這是使用者 profile 設定責任，非本機制保證）。
- `_attach_by_id`（line 1025-1026）與 `_probe_existing_bridge`（line 920-924）：
  把 `passthrough_only` 寫死 `ok=False` 改為——`command_capable` 為真才走 `probe_ready`/`ensure_ready`；
  否則維持 `ATTACHED`（非 command-capable）。passthrough 但有設定 `ready_probe` 的 target 也能進 READY。
- **cmd submit gate**：session 為 `ATTACHED` 且 `command_capable=False` →
  回 `{"ok": False, "error_code": "PROFILE_NOT_COMMAND_CAPABLE", "hint": "此 profile 僅支援
  console；要下命令請設定 ready_probe 或改用具 prompt 的 profile。"}`，取代 `SESSION_NOT_READY`。
  （保留 `SESSION_NOT_READY` 給「command-capable 但尚未 READY」的情形。）
- **self-test**：多回 `command_capable`。
- **README**：補 `ATTACHED` vs `READY` 與 passthrough/command-capable 說明（R-18 docs 對齊）。

### 4.3 U-Boot command profile + 真機驗證

- 新增 `uboot-template`（`profiles/default.yaml`）：
  - `platform`：值不影響 readiness 判定（readiness 只看 `ready_probe`/`prompt_regex`）；
    platform 主要影響 `_prompt_timeout_error` 的錯誤字串與 detect 排序。是否新增 `uboot` 列舉值留 plan 階段定。
  - `prompt_regex`：對 U-Boot 提示符（依 COM1 實機，如 `(?m)^=> $` / `^u-boot> $`，驗證時定）。
  - `ready_probe: "echo __READY__${nonce}"`（多數 U-Boot 有 `echo`；實機若無則換會回顯之指令）。
  - `login_regex` / `password_regex` 退化（無 login）。
- **確認 line-command 路徑不被 bootloader 狀態硬擋**，且與 #44 recovery lease（`bootloader_prompts`、
  `interactive-open --allow-attached`）並存不衝突。
- **真機驗證（COM1）**：將 COM1 綁 `uboot-template`、進 U-Boot prompt，
  `cmd submit --selector COM1 --cmd 'printenv' --mode line` 能正確框出輸出、session 為 READY。

## 5. 測試策略

- 單元/整合（fake PTY，入 CI）：
  - `human_active` 計算：60s 視窗內/外；broker probe RX 不影響 `last_human_input_at`。
  - soft preempt：human idle 時 agent 可取得 owner；human 鍵入進 deferral、回放正確、位元組不交錯。
  - liveness：peer 關閉 → detach + 關 lease。
  - #51：空 `ready_probe` → `ATTACHED` + `PROFILE_NOT_COMMAND_CAPABLE`；設 `ready_probe` 的
    passthrough → 進 READY → `cmd submit` 成功；`self-test` 回 `command_capable`。
- 真機（verification，不入 CI）：COM1 U-Boot 端到端。
- **不得新增既有測試失敗**；注意兩支 pre-existing flaky（`test_five_agents_three_rounds_no_conflict`、
  `t8_full_run_simulation`）。

## 6. 風險與相容性

- `human_attached` 語意不變（additive `human_active`）→ 降低破壞既有 consumer/測試風險。
- soft preempt 動到 co-work 核心（suspend/resume + deferral）→ 重點測位元組不交錯、回放正確、
  競態（agent 結束與 human 重新活躍同時）。
- U-Boot 驗證需板子能進 bootloader（可能要 reset/中斷開機）；驗證前先確認 COM1 實機狀態與 prompt 字串。
- READY 泛化後，OS profile 掉進 U-Boot 仍**不該** READY（prompt 對不上即非 READY），語意自洽。

## 7. 對外行為變更摘要

| 介面 | 變更 |
|------|------|
| `cmd submit` | 非 command-capable session 改回 `PROFILE_NOT_COMMAND_CAPABLE`（原 `SESSION_NOT_READY`）|
| `session self-test` / get-state | 新增 `human_active`、`command_capable` 欄位（additive）|
| profile schema | `ready_probe` 非空即視為 command-capable；新增 `uboot-template` 範例 |
| 行為 | human lease 閒置（>60s）可被 agent soft preempt；死孤兒 console 自動 detach |
