## Summary

<!-- 簡述此 PR 的目的與變更範圍 -->

## Test Plan

<!-- 說明如何驗證此 PR 的正確性 -->

- [ ] 執行 `python3 -m pytest -q tests/` — 無新增失敗
- [ ] 執行 `python3 -m policy_check --repo .` — 通過（release PR 在 tag 建立前改用 `python3 -m policy_check --repo . --pr-labels release:<version>`）

## Policy Checklist (R-11)

- [ ] 分支不是 `main`（不可直接 commit 到 main）
- [ ] 變更已記錄：新增 `changelog.d/<issue>-<slug>.md` fragment（code 變更必備，R-09；純文件／release PR 可改動 `CHANGELOG.md` 或免記）
- [ ] `VERSION` 已更新（若有版本號變動）
- [ ] `python3 -m pytest -q tests/` 通過（無新失敗）
- [ ] `python3 -m policy_check --repo .` 通過（release PR 在 tag 建立前改用 `--pr-labels release:<version>`）
- [ ] 四份 agent 檔案已同步（若有修改 `CLAUDE.md` / `AGENTS.md` / `GEMINI.md` / `.github/copilot-instructions.md`）
- [ ] 已標記適用 label（release PR 使用 `release:<version>`；豁免白名單見 `CLAUDE.md`，如 `skip-changelog`(R-09)／`policy-exempt:ci-tests`(R-19)／`policy-exempt:issue-link`(R-17)）
- [ ] 回歸 case 評估已記錄（修 bug issue 時：新增 `regression/` case／pytest 已覆蓋／免加理由，見 CLAUDE.md「回歸 case 政策」）

## Rust 重構跟進

<!-- Rust 重構（docs/rust-refactor/）完成前，凡改動 `sw_core/`、`serialwrap.py`、`serialwrapd.py`、`sw_core/assets/` 或外部可觀察行為的 PR，須寫明 Rust 端要跟進的 Phase 與位置；不影響者寫「不需要」並附理由。 -->

- 需跟進的 Phase：<!-- Stage 1（Rust CLI）／P5（daemon 核心）／P6（console）／P7（登入與事件）／P8（傳輸與燒錄）／不需要 -->
- Rust 對應位置與要做的事：

## Issue Reference

<!-- 關聯的 Issue，例如：Closes #44 -->
