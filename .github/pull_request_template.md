## Summary

<!-- 簡述此 PR 的目的與變更範圍 -->

## Test Plan

<!-- 說明如何驗證此 PR 的正確性 -->

- [ ] 執行 `python3 -m pytest -q tests/` — 無新增失敗
- [ ] 執行 `python3 -m policy_check --repo .` — 通過

## Policy Checklist (R-11)

- [ ] 分支不是 `main`（不可直接 commit 到 main）
- [ ] `CHANGELOG.md` 已更新（`[Unreleased]` 段落）
- [ ] `VERSION` 已更新（若有版本號變動）
- [ ] `python3 -m pytest -q tests/` 通過（無新失敗）
- [ ] `python3 -m policy_check --repo .` 通過
- [ ] 四份 agent 檔案已同步（若有修改 `CLAUDE.md` / `AGENTS.md` / `GEMINI.md` / `.github/copilot-instructions.md`）
- [ ] 已標記適用 label（release PR 使用 `release:<version>`；豁免白名單：`policy-exempt-changelog`、`policy-exempt-tests`）

## Issue Reference

<!-- 關聯的 Issue，例如：Closes #44 -->
