# #69 attach 撞開機窗自動重探回 READY 實作計畫

> **給 agentic worker：** 必用子技能——`superpowers:subagent-driven-development`（建議）或 `superpowers:executing-plans` 逐 task 實作。步驟用 checkbox（`- [ ]`）追蹤。

**目標：** 讓 daemon 在 session 因 attach 撞 DUT 開機窗而卡非 READY 時，於 prompt 可用後有界自動重探回 READY，無需人工 `recover`。

**架構：** 方案 A——`sw_core/session_manager.py` 新增 `reconcile_readiness()`（RX-idle 觸發、有界 backoff、複用既有 probe），由 daemon periodic tick 週期呼叫；session 以欄位表達重試進度（不新增 FSM 狀態）。配套 minicom_router 訊息 + docs。**真機先複製（已完成）→ 修復 → 真機驗證。**

**技術棧：** Python（unittest）、`paulsha-conventions` policy engine、真機（COM1 / FTDI AC01QZT0，user 同意的測試板）。

**來源：** 設計 `docs/superpowers/specs/2026-06-22-attach-boot-auto-reprobe-design.md`（含真機複製 §2、根因 §3、真機驗證 §6）；openspec `openspec/changes/attach-boot-auto-reprobe/`。

**常數：** 已在分支 `feature/69-attach-boot-auto-reprobe`（off main）。真機複製已完成、COM1 已還原為 READY。

---

## Task 1：常數 + session 欄位

**Files:** `sw_core/constants.py`、`sw_core/session_manager.py`（`SessionRuntime` / `to_public_dict`）

- [ ] **Step 1：constants**
新增（值可依真機調，先用保守預設）：
```python
REPROBE_RX_IDLE_S = 3.0          # RX 轉閒多久才視為「boot log 已停、prompt 可能可用」
REPROBE_BACKOFF_S = 2.0          # 首次重探間隔
REPROBE_MAX_INTERVAL_S = 15.0    # backoff 上限
REPROBE_MAX_ATTEMPTS = 10        # 重探次數上限（達上限停手）
```
- [ ] **Step 2：SessionRuntime 欄位 + 公開**
`SessionRuntime` 加 `reprobe_attempts: int = 0`、`next_reprobe_at: float | None = None`、`reprobe_exhausted: bool = False`；`to_public_dict()` 暴露這三欄；在「成功進 READY / 人工 recover / device 變動 / RELEASED」時歸零（找既有 state→READY 與 recover 收尾處重置）。
- [ ] **Step 3**：`python3 -c "import sw_core.session_manager"` 匯入無誤。

## Task 2：RED — reconcile 觸發/邊界的失敗測試

**Files:** `tests/test_readiness_reprobe.py`（新增）

- [ ] **Step 1：寫失敗測試**（用既有測試風格的 fake bridge / monkeypatch probe；參考 `tests/test_login_fsm.py`、`tests/test_session_bind.py` 的建構方式）
涵蓋：
  1. `ATTACHED`+`last_error=PROMPT_UNAVAILABLE`、RX idle ≥ `REPROBE_RX_IDLE_S` → `reconcile_readiness()` 觸發重探，probe 成功 → state `READY`、欄位歸零。
  2. RX idle < 門檻（boot log 仍噴）→ 不重探（`reprobe_attempts` 不增）。
  3. human-active interactive lease / `FLASHING` / `RELEASED` → 跳過、不送 probe。
  4. probe 持續失敗 → backoff 遞增、達 `REPROBE_MAX_ATTEMPTS` → `reprobe_exhausted=True` 且停止重探。
  5. `DETACHED`+`*_PROMPT_TIMEOUT`、device 在位 → 重探（走 `_spawn_attach`/`ensure_ready`）。
- [ ] **Step 2：跑測試確認 RED**
Run: `python3 -m pytest -q tests/test_readiness_reprobe.py`
Expected：FAIL（`reconcile_readiness` 不存在 / 行為未實作）。

## Task 3：GREEN — 實作 reconcile_readiness

**Files:** `sw_core/session_manager.py`

- [ ] **Step 1：實作 `reconcile_readiness(self) -> None`**
掃 `self._sessions`，對每個 session 在 `self._lock` 下套用觸發條件（見設計 §4）：
  - 跳過：READY / RELEASED（或 by-id 在 `_released_by_ids`）/ FLASHING / human-active interactive lease / device 不在位 / `reprobe_exhausted` / 未到 `next_reprobe_at`。
  - 候選：`ATTACHED` 且 `last_error` 屬 prompt-unavailable 類，或 `DETACHED` 且 `last_error` 屬 prompt-timeout 類。
  - RX-idle gate：`now - last_rx_at >= REPROBE_RX_IDLE_S`（用既有 bridge/snapshot 的 last_rx）。
  - 動作：`ATTACHED` → 複用 `_probe_existing_bridge`；`DETACHED` → 複用 `_spawn_attach`/attach 路徑。成功 READY → 欄位歸零；失敗 → `reprobe_attempts += 1`、`next_reprobe_at = now + min(REPROBE_BACKOFF_S * 2**(n-1), REPROBE_MAX_INTERVAL_S)`；達上限 → `reprobe_exhausted = True`。
  - 以 lock + `next_reprobe_at` 去重，避免與 recover/attach 重入。
- [ ] **Step 2：跑測試確認 GREEN**
Run: `python3 -m pytest -q tests/test_readiness_reprobe.py`
Expected：PASS。

## Task 4：daemon periodic tick 驅動

**Files:** `serialwrapd.py`（或 service/DeviceWatcher 的週期點——實作時 grep `DeviceWatcher`/週期迴圈確認驅動位置）

- [ ] **Step 1：在既有週期工作（如 device 掃描 tick）呼叫 `session_manager.reconcile_readiness()`**（與 DeviceWatcher 同節奏，或獨立 interval；避免新開執行緒若已有 tick）。
- [ ] **Step 2：`python3 -m pytest -q tests/`** 全跑，除既有 flaky 外無新失敗。

## Task 5：minicom_router 訊息 + 可選 wait

**Files:** `tools/minicom_router.sh`

- [ ] **Step 1：改 `:~340` 的 `broker not ready` 分支**——查 session state/`last_error`/`reprobe_attempts`，非 READY 且屬 prompt-timeout 類 / 正在重試時印明確提示：「DUT 可能仍在開機，serialwrap 正在自動重試；可稍候或手動 `serialwrap session recover --selector COMx`」。
- [ ] **Step 2（可選）**：`MINICOM_WAIT_READY=1` 時輪詢等待 READY（有上限）再開 minicom。
- [ ] **Step 3：`bash -n tools/minicom_router.sh`**。

## Task 6：docs

**Files:** `README.md`、`skills/serialwrap/SKILL.md`

- [ ] FAQ：「開機窗連不到」→ self-test 判讀（`ATTACHED_NOT_READY`/`BRIDGE_DOWN`）→ daemon 會自動重探 / 必要時手動 `session recover`。

## Task 7：本地驗證 + CHANGELOG + commit

**Files:** `CHANGELOG.md`

- [ ] `python3 -m pytest -q tests/`（除既有 flaky 無新失敗）。
- [ ] `python3 -m policy_check --repo .` EXIT=0（R-09 CHANGELOG）。
- [ ] `CHANGELOG.md [Unreleased]` 記一筆。
- [ ] commit（conventional，繁中 + 兩個 Co-author trailer）。

## Task 8：真機驗證（COM1，設計 §6）— 修復後執行

> ⚠️ 真機操作守則（見記憶 `mcu-flash-broker-realhw-validation` 等）：兩塊板都有 human minicom，**只動 user 同意的 COM1**；reboot 會中斷該板 minicom；完成後還原。

- [ ] 先確認部署的是本分支程式（必要時對 throwaway target dir `./install.sh /tmp/...` 或 prod 重啟前先談）。
- [ ] baseline：COM1 `READY`、self_test OK。
- [ ] `serialwrap cmd submit --selector COM1 --cmd reboot ...` → 開機窗內 `session clear`+`session attach`（重現卡住起點）。
- [ ] **不做任何人工 recover**，輪詢 `session list`/`self_test`：期望 boot 完成（RX 轉閒）後 session 在 backoff 窗內**自動**回 `READY`，`reprobe_attempts>0`、self_test `OK`。
- [ ] 開機窗內跑 `minicom_router.sh COM1` → 看到新明確提示（非 `broker not ready`）；READY 後正常開 minicom。
- [ ] 還原 COM1、確認 prod daemon 與 COM0 不受影響。
- [ ] 把真機驗證結果（含 `reprobe_attempts`、回 READY 耗時）記入 PR / CHANGELOG。

---

## 驗收對照（self-review）
- reconcile 觸發/RX-idle gate/backoff/上限/跳過 human-active·FLASHING·RELEASED 都有 unit 覆蓋且 RED→GREEN。
- daemon 週期呼叫 reconcile；不新增 FSM 狀態；不改 login_fsm 判定。
- minicom_router 非 READY 提示明確；docs FAQ 補上。
- pytest 無新失敗、policy EXIT=0、CHANGELOG 記錄。
- **真機驗證通過**：COM1 開機窗 attach 後**自動**回 READY（無人工）。
- 範疇：不處理 #52；不新增 plugin/MCP。
