# Co-work Session 可用性 Implementation Plan（#51 + #53）

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 讓「看起來 attached 但實際不能用」的 co-work session 變可用：閒置/孤兒 human lease 可被 agent soft preempt（#53），passthrough/bootloader 等 profile 以 `ready_probe` 判定可下命令並能進 READY（#51）。

**Architecture:** 在 `UARTBridge` 追蹤真實 human 鍵入時間；`SessionManager` 以時間窗（60s）區分 `human_active` 並驅動 soft preempt；以 `command_capable = bool(ready_probe)` 取代 passthrough 寫死，cmd submit 回 `PROFILE_NOT_COMMAND_CAPABLE`；新增 `uboot-template` 並真機驗證。

**Tech Stack:** Python 3.12、unittest、fake PTY、既有 `SessionManager`/`UARTBridge`/`login_fsm`。

設計依據：`docs/superpowers/specs/2026-06-17-cowork-session-usability-design.md`；契約：`openspec/changes/cowork-session-usability/`。

---

## 慣例（每個 task 通用）

- 測試置於 `tests/test_cowork_session_usability.py`（新檔，逐 task 追加 class/method）。
- SessionManager 建構：`SessionManager(profiles, WalWriter(wal_dir=tmp), on_ready=lambda _:None, on_detached=lambda _:None)`，並在 setUp 覆寫 `sm_mod.STATE_PATH`（見 `tests/test_session_bind.py`）。
- 跑單一測試：`python3 -m pytest tests/test_cowork_session_usability.py::<Class>::<test> -v`。
- 每個 GREEN 後跑 `python3 -m pytest -q tests/test_cowork_session_usability.py`。

---

## Task 1：command_capable 判準 + PROFILE_NOT_COMMAND_CAPABLE（#51）

**Files:**
- Modify: `sw_core/session_manager.py`（`_attach_by_id` ~1025、`_probe_existing_bridge` ~920、cmd submit gate）
- Test: `tests/test_cowork_session_usability.py`

- [ ] **Step 1：RED — 空 ready_probe passthrough 維持 ATTACHED 且 cmd submit 回 PROFILE_NOT_COMMAND_CAPABLE**

新增 helper：建一個 `platform="passthrough"`、`ready_probe=""` 的 profile，attach 到 fake PTY（沿用既有測試的 fake by-id 機制；若無則用 `tests/` 內既有 PTY helper），斷言：
```python
def test_empty_ready_probe_stays_attached_and_not_command_capable(self):
    mgr, sel = self._attach_passthrough(ready_probe="")   # helper attaches a fake PTY
    state = mgr.self_test(sel)["session"]["state"]
    self.assertEqual(state, "ATTACHED")
    resp = mgr.submit_command(selector=sel, command="echo x", source="agent:test", mode="line", timeout_s=5)
    self.assertFalse(resp["ok"])
    self.assertEqual(resp["error_code"], "PROFILE_NOT_COMMAND_CAPABLE")
```

- [ ] **Step 2：跑測試確認 RED**

Run: `python3 -m pytest tests/test_cowork_session_usability.py -k empty_ready_probe -v`
Expected: FAIL（目前回 `SESSION_NOT_READY`，非 `PROFILE_NOT_COMMAND_CAPABLE`）。

- [ ] **Step 3：GREEN — 改 attach/probe gate 與 cmd submit gate**

在 `_attach_by_id`（line 1025-1026）與 `_probe_existing_bridge`（line 920-924）把 `passthrough_only` / `platform=="passthrough"` 改為：
```python
command_capable = bool((profile.ready_probe or "").strip())
if not command_capable:
    ok, err = False, None        # 維持 ATTACHED（非 command-capable）
else:
    ... 走既有 probe_ready / ensure_ready ...
```
在 cmd submit gate（session_manager 對 `state != "READY"` 的 submit 入口，~1858-1921，以及 `service.py:275`）：當 session 為 `ATTACHED` 且 `not command_capable` 時回
```python
return {"ok": False, "error_code": "PROFILE_NOT_COMMAND_CAPABLE",
        "hint": "此 profile 僅支援 console；要下命令請設定 ready_probe 或改用具 prompt 的 profile。",
        "session": session_public}
```
其餘未 READY 情形維持 `SESSION_NOT_READY`。

- [ ] **Step 4：跑測試確認 GREEN**

Run: `python3 -m pytest tests/test_cowork_session_usability.py -k empty_ready_probe -v` → PASS

- [ ] **Step 5：RED+GREEN — 有 ready_probe 的 passthrough 可進 READY**

加測試：`ready_probe="echo __READY__${nonce}"` + 可匹配 prompt 的 fake target → attach 後 `state == "READY"`、`submit_command(... mode="line")` 回 `ok==True`。實作已由 Step 3 的 `command_capable` 分支涵蓋；若 fake target 無法回 nonce，於測試用可控的 echo PTY。確認 PASS。

- [ ] **Step 6：Commit**

```bash
git add sw_core/session_manager.py tests/test_cowork_session_usability.py
git commit -m 'feat(session): command_capable 以 ready_probe 判定，cmd submit 回 PROFILE_NOT_COMMAND_CAPABLE（#51）

Co-authored-by: Copilot <223556219+Copilot@users.noreply.github.com>
Co-Authored-By: Claude Opus 4.8 <noreply@anthropic.com>'
```

---

## Task 2：self_test 新增 command_capable 欄位（#51）

**Files:** Modify `sw_core/session_manager.py`（`self_test` ~2117）；Test 同上檔。

- [ ] **Step 1：RED**
```python
def test_self_test_exposes_command_capable(self):
    mgr, sel = self._attach_passthrough(ready_probe="")
    self.assertIn("command_capable", mgr.self_test(sel))
    self.assertFalse(mgr.self_test(sel)["command_capable"])
```
- [ ] **Step 2：RED 確認** — `pytest -k command_capable` → FAIL（KeyError/AssertionError）。
- [ ] **Step 3：GREEN** — 在 `self_test` 每個 return 的最外層加 `"command_capable": bool((session.profile.ready_probe or "").strip())`（SESSION_NOT_FOUND 分支無 session → `False`）。建議集中由一個 helper 算，避免漏分支。
- [ ] **Step 4：GREEN 確認** — PASS。
- [ ] **Step 5：Commit**（`feat(session): self_test 輸出 command_capable（#51）` + trailers）。

---

## Task 3：U-Boot command profile + 真機驗證（#51）

**Files:** Modify `profiles/default.yaml`、`sw_core/config.py`（若需新 platform 列舉）；Test 同上檔。

- [ ] **Step 1：新增 uboot-template**（`profiles/default.yaml`）：
```yaml
  uboot-template:
    platform: passthrough        # 不需 login；readiness 只看 ready_probe/prompt_regex
    prompt_regex: "(?m)^(=>|u-boot>|CFE>) $"   # 真機驗證時收斂為實機字串
    login_regex: "$^"
    password_regex: "$^"
    ready_probe: "echo __READY__${nonce}"
    uart: { baud: 115200, data_bits: 8, parity: N, stop_bits: 1, flow_control: none, xonxoff: false }
```
- [ ] **Step 2：RED+GREEN（單元）** — 測試綁 `uboot-template` 的 session：`command_capable==True`；給可回 nonce 的 fake U-Boot PTY → `state=="READY"`、`submit_command('printenv', mode='line')` ok。
- [ ] **Step 3：確認與 #44 並存** — 加（或確認既有）測試：`interactive_open(..., allow_attached=True)` 的 bootloader recovery 路徑不受 command profile 影響。
- [ ] **Step 4：真機驗證（COM1，verification，不入 CI）** — 先 `serialwrap session list` 確認 COM1 實機；將 COM1 綁 `uboot-template`（或臨時 profile）、使 target 進 U-Boot prompt（必要時 reset/中斷開機），校正 `prompt_regex`；執行
  `serialwrap cmd submit --selector COM1 --cmd 'printenv' --mode line --source agent:verify` → 取回框出的 env 輸出；記錄為驗證證據。
- [ ] **Step 5：Commit**（`feat(profile): 新增 uboot-template，READY 泛化至 bootloader（#51）` + trailers）。

---

## Task 4：UARTBridge 追蹤 last_human_input_at（#53）

**Files:** Modify `sw_core/uart_io.py`（state init ~98、`_handle_console_rx` 401-403、`snapshot` 646-655）；Test 同上檔。

- [ ] **Step 1：RED**
```python
def test_human_keystroke_updates_last_input_but_probe_does_not(self):
    bridge = self._make_bridge_with_human_owner()   # helper：開 bridge、attach console、set_interactive_owner("human:c1")
    before = bridge.snapshot().get("last_human_input_at")
    bridge._handle_console_rx(bridge._clients["c1"], b"a")   # 真人鍵入
    after = bridge.snapshot().get("last_human_input_at")
    self.assertIsNotNone(after)
    self.assertNotEqual(before, after)
```
- [ ] **Step 2：RED 確認** — FAIL（snapshot 無 `last_human_input_at`）。
- [ ] **Step 3：GREEN**
  - init（~line 101 後）：`self._last_human_input_at: float | None = None`
  - `_handle_console_rx` human-owner 直送分支（line 401-403）內加：`with self._state_lock: self._last_human_input_at = time.monotonic()`（`time` 已 import）。**只在此分支更新**（不在 deferred、不在 serial RX loop）。
  - `snapshot()` return dict 加 `"last_human_input_at": self._last_human_input_at`（在 `_state_lock` 內讀取）。
- [ ] **Step 4：GREEN 確認** — PASS。
- [ ] **Step 5：Commit**（`feat(uart): 追蹤 human 真實鍵入時間 last_human_input_at（#53）` + trailers）。

---

## Task 5：human_active 語意 + self_test 欄位（#53）

**Files:** Modify `sw_core/constants.py`（常數）、`sw_core/session_manager.py`（`_lease_context` 1395、callers、self_test）；Test 同上檔。

- [ ] **Step 1：RED**
```python
def test_human_active_false_when_idle(self):
    mgr, sel = self._ready_session_with_human_lease(last_input_age_s=120)
    r = mgr.self_test(sel)
    self.assertTrue(r["human_attached"])      # 語意不變
    self.assertFalse(r["human_active"])        # 閒置
def test_human_active_true_when_recent(self):
    mgr, sel = self._ready_session_with_human_lease(last_input_age_s=5)
    self.assertTrue(mgr.self_test(sel)["human_active"])
```
- [ ] **Step 2：RED 確認** — FAIL（無 `human_active`）。
- [ ] **Step 3：GREEN**
  - `constants.py` 加：`HUMAN_ACTIVE_WINDOW_S: float = 60.0`。
  - `_lease_context` 改簽名 `_lease_context(self, lease, *, bridge=None)`：
    ```python
    human_attached = bool(interactive_owner and interactive_owner.startswith("human:"))
    human_active = False
    if human_attached and bridge is not None:
        last = bridge.snapshot().get("last_human_input_at")
        human_active = last is not None and (time.monotonic() - last) <= HUMAN_ACTIVE_WINDOW_S
    return {..., "human_attached": human_attached, "human_active": human_active, ...}
    ```
  - 更新 4 個 caller（line 2131/2141/2151/2154）：有 session.bridge 的傳 `bridge=session.bridge`；`None` lease 的維持（human_active=False）。
  - self_test 既有以 `human_attached` 阻擋/建議 idle 的分支（若有），改看 `human_active`。
- [ ] **Step 4：GREEN 確認** — PASS；並跑既有 `tests/`（確認 `human_attached` 契約測試不退化）：`python3 -m pytest -q tests/ -k "self_test or selftest or interactive"`。
- [ ] **Step 5：Commit**（`feat(session): 新增 human_active 時間窗語意（#53）` + trailers）。

---

## Task 6：soft preempt + liveness（#53）

**Files:** Modify `sw_core/session_manager.py`（agent 取得控制權路徑：`interactive_open` ~1906、及命令注入前 suspend/resume 處）；Test 同上檔。

- [ ] **Step 1：RED — idle 時 agent 可 soft preempt、active 時不可**
```python
def test_agent_soft_preempts_idle_human(self):
    mgr, sel = self._ready_session_with_human_lease(last_input_age_s=120)
    resp = mgr.interactive_open(sel, owner="agent")       # agent 取得控制權
    self.assertTrue(resp["ok"])
    # human 仍 attached（被降級，非 detach）
    self.assertTrue(any(c for c in mgr.console_list(sel)["consoles"] if "human" in c.get("label","")))
def test_agent_cannot_preempt_active_human(self):
    mgr, sel = self._ready_session_with_human_lease(last_input_age_s=5)
    resp = mgr.interactive_open(sel, owner="agent")
    self.assertFalse(resp["ok"])
    self.assertIn(resp["error_code"], ("SESSION_INTERACTIVE_BUSY", "SESSION_NOT_READY"))
```
- [ ] **Step 2：RED — 死孤兒 detach**
```python
def test_dead_orphan_console_detached(self):
    mgr, sel = self._ready_session_with_human_lease(last_input_age_s=120, peer_alive=False)
    mgr.self_test(sel)   # 觸發 liveness
    self.assertIsNone(mgr.self_test(sel)["interactive_owner"])
```
- [ ] **Step 3：RED 確認** — FAIL（目前 idle human 仍硬擋 / 孤兒不被收）。
- [ ] **Step 4：GREEN — soft preempt**
  在 `interactive_open`（owner 非 human、session READY、已有 human lease 時）：
  - 先 `_refresh_interactive_locked`（觸發既有 peer-gone liveness）。
  - 若 human lease 仍在：算 `human_active`（同 Task 5 邏輯）。
    - `human_active == True` → 維持既有 busy 行為（回 `SESSION_INTERACTIVE_BUSY`）。
    - `human_active == False` → soft preempt：`bridge.suspend_interactive()` 把 human 降級，開 agent lease（owner="agent"）；agent lease close 時 `bridge.resume_interactive()` 還原 + 回放 deferral（沿用既有 resume 路徑）。
  - **不 detach** human console（仍 attached）。
- [ ] **Step 5：GREEN — liveness** 確保 self_test / interactive_open 進入前呼叫 `_refresh_interactive_locked`（已對 peer-gone 做 detach+close，line 1376-1382）；補上 self_test 路徑若未呼叫。
- [ ] **Step 6：GREEN 確認 + 競態檢查** — PASS；額外手測/測試「agent 結束 resume 與 human 重新鍵入同時」不丟字、不交錯。跑 `python3 -m pytest -q tests/ -k "interactive or e2e or multiagent"`（注意兩支 pre-existing flaky）。
- [ ] **Step 7：Commit**（`feat(session): 閒置 human lease 可被 agent soft preempt、死孤兒 detach（#53）` + trailers）。

---

## Task 7：文件 + CHANGELOG

**Files:** Modify `README.md`、`CHANGELOG.md`。

- [ ] **Step 1：README** 補一節「Session 狀態與可下命令」：`ATTACHED` vs `READY`、`command_capable`/`PROFILE_NOT_COMMAND_CAPABLE`、`uboot-template` 用法、human_active/soft-preempt co-work 行為。
- [ ] **Step 2：CHANGELOG** `[Unreleased]`：Added（uboot-template、human_active/command_capable 欄位、soft preempt）、Changed（cmd submit 對非 command-capable 回 PROFILE_NOT_COMMAND_CAPABLE）。
- [ ] **Step 3：Commit**（`docs: 補 co-work session 可用性說明與 CHANGELOG（#51 #53）` + trailers）。

---

## Task 8：驗證、review、archive、PR

- [ ] **Step 1：全套測試** — `python3 -m pytest -q tests/`：確認僅兩支 pre-existing flaky 可能失敗，無新失敗。
- [ ] **Step 2：policy** — `python3 -m policy_check --repo .` 通過（注意 R-16 CLI help、R-18 docs；本 change 未改 CLI help，README 已補）。
- [ ] **Step 3：requesting-code-review** — 對每個 finding 走 receiving-code-review，修後 **re-review**。
- [ ] **Step 4：openspec archive** — `openspec archive cowork-session-usability`（或 opsx:archive）。
- [ ] **Step 5：commit/push/PR** — conventional-commit；push；`gh pr create` body 填 Policy Checklist + `Closes #51` + `Closes #53`；CI 綠後依使用者指示 merge。

---

## Self-Review（已執行）

- **Spec coverage**：session-command-readiness（Task 1-3）、session-interactive ADDED（Task 4,6）、session-selftest ADDED（Task 2,5）皆有對應 task。
- **Placeholder scan**：U-Boot `prompt_regex` 實機字串為「驗證時收斂」屬刻意的經驗值，非 TODO。
- **Type consistency**：`command_capable`/`human_active`/`last_human_input_at`/`HUMAN_ACTIVE_WINDOW_S` 命名跨 task 一致。
