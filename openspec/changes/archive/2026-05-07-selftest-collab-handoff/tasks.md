## 1. Branch / scaffold

- [x] 1.1 從 `main` 開新 branch `fix/selftest-collab-42`
- [x] 1.2 確認 `pytest tests/test_session_bind.py` 在改動前全綠（baseline）

## 2. SessionManager.self_test 行為改動

- [x] 2.1 在 `sw_core/session_manager.py` 為 `self_test` 新增 `strict_human_lock: bool = False` 參數
- [x] 2.2 在函式入口取得 lease snapshot 後，計算 `interactive_owner` / `human_attached`，包成 helper（如 `_lease_context(lease)`）統一注入到每個 return dict
- [x] 2.3 移除 `lease.owner.startswith("human:")` 的 short-circuit；保留為 `if strict_human_lock and lease ...:` 的 opt-in 分支
- [x] 2.4 `strict_human_lock=True` 分支的 return 維持 `classification="HUMAN_INTERACTIVE_ACTIVE"` / `recommended_action="wait_or_detach_console"` / `interactive_id`，並補上 `interactive_owner` / `human_attached`
- [x] 2.5 在 probe 階段（`bridge.send_command(probe, ...)` 前後）加 `if lease and lease.owner.startswith("human:"): bridge.suspend_interactive()` ... `try/finally` ... `bridge.resume_interactive()`，確保 suspend/resume 在 `self._lock` 之外
- [x] 2.6 確認所有 return（含 `OK` / `TARGET_UNRESPONSIVE` / 各 `ATTACHED_*` / `BRIDGE_DOWN` / `VTTY_STALE` / `DEVICE_*` / `SESSION_RECOVERING` / `PASSTHROUGH`）都帶 `interactive_owner` 與 `human_attached`

## 3. RPC / CLI passthrough

- [x] 3.1 `sw_core/service.py` 的 `session.self_test` handler 從 `params.get("strict_human_lock", False)` 讀取並傳入
- [x] 3.2 `sw_core/cli.py` 的 `session self-test` 子命令加 `--strict-human-lock` flag（store_true、default False）
- [x] 3.3 CLI 將 flag 寫入 RPC params 中，並在 `--help` 文字描述用途
- [x] 3.4 grep `sw_mcp/` 是否有 `session_self_test` tool 描述提到舊行為；若有，同步加 `strict_human_lock` 參數說明與更新分類列表

## 4. Tests

- [x] 4.1 將 `tests/test_session_bind.py:test_self_test_reports_human_interactive_active` 改名為 `test_self_test_strict_mode_reports_human_interactive_active`，並改成傳 `strict_human_lock=True` 才斷言 `HUMAN_INTERACTIVE_ACTIVE`
- [x] 4.2 新增 `test_self_test_default_walks_through_with_human_attached`：human lease 存在、預設模式應走完整流程、回 `OK` 並含 `human_attached=True` / `interactive_owner.startswith("human:")`
- [x] 4.3 新增 `test_self_test_default_suspends_human_during_probe`：mock `bridge.suspend_interactive` / `resume_interactive`，驗證 probe 前後各被呼叫一次、順序正確、即使 probe 拋例外 `resume` 仍被呼叫（finally 行為）
- [x] 4.4 新增 `test_self_test_no_suspend_when_lease_is_agent`：lease owner 為 `"agent"`，suspend/resume 皆不被呼叫
- [x] 4.5 新增 `test_self_test_human_attached_field_in_non_ok_paths`：human lease + `device_watcher` 報 missing，確認 `DEVICE_MISSING` result 仍含 `human_attached=True` / `interactive_owner`
- [x] 4.6 跑 `python -m pytest tests/test_session_bind.py -v` 全綠
- [x] 4.7 跑 `python -m pytest tests/ -q` 全 suite 確認無回歸

  - 驗證註記：`python3 -m unittest discover -s tests -v` 僅 `tests.test_multiagent_e2e.TestMultiAgentE2E.test_five_agents_three_rounds_no_conflict` 失敗，已在乾淨 `HEAD` snapshot 重現，判定為既有基線問題，非本次 `self_test` regression。

## 5. Docs

- [x] 5.1 `docs/serialwrap-spec.md` §9.1：分類列表移除（或註記為 strict-only）`HUMAN_INTERACTIVE_ACTIVE`；輸入加 `strict_human_lock`、輸出加 `interactive_owner` / `human_attached`
- [x] 5.2 `docs/serialwrap-spec.md` §9.1 新增「Collaborative monitoring」段落，說明 human attach 期間 self_test 走完整流程、probe 階段自動 suspend/resume、與 command path 行為一致
- [x] 5.3 若 §10、§11 等其他章節有 cross-reference 到 self_test 分類，同步更新
- [x] 5.4 README 不動

## 6. Functional verification

- [x] 6.1 重啟一次 daemon、`session console-attach COM0`（模擬 human attach），執行 `serialwrap session self-test --selector COM0`，確認回 `OK` 而非 `HUMAN_INTERACTIVE_ACTIVE`
- [x] 6.2 再加 `--strict-human-lock` 重跑，確認回 `HUMAN_INTERACTIVE_ACTIVE`
- [x] 6.3 在 attach 期間從另一 terminal 觸發 `serialwrap cmd submit COM0 "echo hi"`，觀察 console 內 human 輸入累積與 resume flush 與 self_test 的 suspend 不衝突

## 7. Commit / PR

- [ ] 7.1 commit 1：`feat(self_test): add strict_human_lock + interactive_owner/human_attached fields`
- [ ] 7.2 commit 2：`feat(self_test): suspend human interactive during probe`
- [ ] 7.3 commit 3：`test(self_test): cover default walk-through + strict mode + suspend orchestration`
- [ ] 7.4 commit 4：`docs(self_test): document collaborative monitoring in spec §9.1`
- [ ] 7.5 push branch、開 PR `fix(self_test): allow agent handoff while human monitors console (#42)`、PR body 連結 issue #42

## 8. Archive openspec change

- [x] 8.1 PR merge 後，使用 openspec-archive-change 把 `openspec/changes/selftest-collab-handoff/` 移到 `openspec/changes/archive/<date>-selftest-collab-42/`
- [x] 8.2 把新 capability spec 移到 `openspec/specs/session-selftest/spec.md` 作為 baseline
