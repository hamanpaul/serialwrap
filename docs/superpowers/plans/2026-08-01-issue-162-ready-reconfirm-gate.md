---
status: accepted
work_item: issue-162-ready-reconfirm
target_branch: feature/162-ready-reconfirm-gate
issue: 162
---

# #162 quiet 清空改綁 READY 再確認實作計畫

**Goal:** 消除「quiet window 過期即開 gate、stale READY 讓第一個 agent 命令吃到 askconsole 啟用 banner」的污染——agent gate 的解除改綁 READY 經 nonce probe 再確認，並由 reprobe 引擎在 TX 靜默結束後自動補一輪確認 probe（該 probe 的 `\n`+nonce 順帶消耗掉 activation banner）。

**Architecture:** `SessionRuntime` 新增 transient `ready_reconfirm_pending`；arm quiet 時置 True，所有 READY 確認點（attach/probe/reboot-recovery/self-test）呼叫新 `confirm_ready()` 清除；agent gate 三處（execute_command／file_push／file_pull）與 service `_resolve_session_id` 改判統一入口 `agent_gate_active()`（`boot_quiet_active() or ready_reconfirm_pending`）；reprobe 引擎放行 READY＋pending 的確認 probe（沿用既有 RX-idle／backoff／human-active 守衛）。TX 靜默維度各點維持 `boot_quiet_active()` 不動。

**根因（設計實證，取代 issue 原假說）:** 污染不是 RX buffer 殘留（quiet-cleared 時 last_rx_age_s=104.7、污染命令 created→done 僅 103ms、stdout 是對命令 bytes 的新鮮回應）。真機制＝prpl/OpenWrt askconsole 停在「Please press Enter to activate this console」，既不匹配 login_regex 也不匹配 prompt_regex → quiet 只能靠 180s 過期清空；期間 session 名義 READY 且 reprobe 跳過 READY，無人重新確認；第一個命令的 `\n` 觸發 askconsole 啟用，命令文字被 askfirst 吞掉、ash 印 banner＋fresh prompt，prompt_regex 在 banner 後匹配 → status=done、stdout=banner、marker 遺失。

## Decisions（本計畫已裁決，實作不得再開放）

1. `retry_after_s` 在 pending-only 時固定 **5.0**（≈RX idle 3s＋一輪 backoff）；不做動態值——消費端只當提示，動態值增加契約面而無實益。
2. `_self_test_impl` 的 READY nonce 成功分支**納入**確認點（一致性，1 行）。
3. **不**把 askconsole 字串納入偵測 pattern（YAGNI；probe 的 `\n` 已消耗它，加 pattern 等於維護第三方 image 文案）。
4. f9-spontaneous 的 evidence 已存整個 session dict，天然涵蓋新欄位，**不另加**記錄邏輯。

## Task 1: SessionRuntime 狀態與統一 gate 入口

**Files:** `sw_core/session_manager.py`

- [ ] 1.1 `SessionRuntime` 新增 `ready_reconfirm_pending: bool = False`（transient，不進 `_save_state`）
- [ ] 1.2 `arm_boot_quiet()` 末尾置 `ready_reconfirm_pending = True`；新增 `confirm_ready()`（`clear_boot_quiet()` + 清 pending）；新增 `agent_gate_active(now=None)` = `boot_quiet_active(now) or ready_reconfirm_pending`
- [ ] 1.3 `to_public_dict()` 新增 `ready_reconfirm_pending`（additive、sort_keys 穩定）
- [ ] 1.4 對應單測：`tests/test_boot_quiet.py::TestReadyReconfirmGate` 的 `test_public_dict_exposes_ready_reconfirm_pending`；先紅後綠

## Task 2: READY 確認點接線與 agent gate 切換

**Files:** `sw_core/session_manager.py`、`sw_core/service.py`

- [ ] 2.1 確認點改呼 `confirm_ready()`：`_probe_existing_bridge` ok 分支、`_attach_by_id` 兩處 READY 落定、`_spawn_reboot_recovery` ok 分支、`_handle_reboot_command` 2s prompt-return 分支、`_self_test_impl` READY nonce 成功分支
- [ ] 2.2 agent gate 三處改 `session.agent_gate_active()`：`execute_command`、`file_push`、`file_pull`
- [ ] 2.3 `service._resolve_session_id` gate 條件擴為「quiet 剩餘 or `ready_reconfirm_pending`」，pending-only 時 `retry_after_s=5.0`、hint 改「等 daemon 重新確認 READY 後重送」；回應形狀不變
- [ ] 2.4 單測：`test_quiet_expiry_keeps_agent_gate`（quiet 過期但 pending → AUTOBOOT_QUIET 且零 TX）、`test_prompt_regex_clear_keeps_pending`、`test_submit_time_gate_pending_only`、`test_file_push_pull_gated_when_pending`、`test_human_source_not_gated_when_pending`、`test_ready_confirm_sites_clear_pending`

## Task 3: reprobe 引擎的 READY 再確認分支

**Files:** `sw_core/session_manager.py`

- [ ] 3.1 `_prepare_reprobe_locked` 加 READY 分支：僅 `ready_reconfirm_pending and not boot_quiet_active(now)` 且通過既有 `next_reprobe_at` backoff／`_rx_idle_enough`／`_human_active_locked`／bridge 非 None／非 inflight 才回 probe
- [ ] 3.2 `_reprobe_target_still_valid_locked` 放寬 `state not in ("ATTACHED","READY")`；READY 需 pending 才 valid；`_is_reprobe_prompt_error` 檢查僅施於 ATTACHED
- [ ] 3.3 `_probe_existing_bridge` 的 quiet 早退分支對 `state==READY` 不覆寫 `last_error=PROMPT_UNAVAILABLE`
- [ ] 3.4 單測：`test_reconfirm_probe_clears_pending_and_allows_execute`、`test_prepare_reprobe_ready_branch`（三態表）

## Task 4: 哨兵 case、文件與 changelog

**Files:** `regression/serialwrap_regression/cases/f09_boot_uboot.py`、`README.md`、`docs/serialwrap-spec.md`、`docs/regression-plugin.md`、`changelog.d/162-ready-reconfirm-gate.md`

- [ ] 4.1 `_wait_quiet_cleared` 條件加 `and not last.get("ready_reconfirm_pending")`（舊 daemon 無欄位→falsy，向下相容）；四段設計其餘不動
- [ ] 4.2 README（boot quiet window 段與 error code 表）＋`docs/serialwrap-spec.md` 同步「清空判定＝READY 再確認（nonce probe），probe 同時消耗 askconsole 啟用 banner」與新欄位
- [ ] 4.3 `docs/regression-plugin.md` 已知基準：`f9-spontaneous` 由 #162 紅燈哨兵改記為常駐防線
- [ ] 4.4 `changelog.d/162-ready-reconfirm-gate.md`（`type: fix`、`scope: session_manager`）

## Task 5: 驗證閘

- [ ] 5.1 `python3 -m pytest -q tests/` 無新失敗
- [ ] 5.2 `python3 -m policy_check --repo .` 通過
- [ ] 5.3 實機（人工閘，PR 後）：`testpilot run serialwrap_regression --case f9-spontaneous-reboot-agent-gated` 轉綠，且 `f9-quiet-window-agent-passthrough` 維持綠

## Open Questions

- 無
