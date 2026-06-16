<!-- managed-by: hamanpaul/paulsha-conventions@v1.0.4 -->
<!-- 若修改此檔，同步更新 CLAUDE.md / AGENTS.md / GEMINI.md / .github/copilot-instructions.md 四份 -->
policy_version: 1.0.4
<!-- policy_version 為 policy_check R-14 machine-readable marker；需保持裸行格式，請勿移入 frontmatter 或 code block。 -->

# serialwrap — AI Agent Policy Checklist

本文件為所有 AI agent（Claude、GitHub Copilot、Gemini 等）在本 repo 工作時必須遵守的政策清單。

## 分支政策

- **禁止直接 commit 到 `main` 分支**，所有變更必須透過 PR。
- 跨多個子項目或長期功能開發，建議使用 `git worktree` 避免分支污染。
- 分支命名慣例：`feature/<issue-id>-<short-desc>`、`fix/<issue-id>-<short-desc>`。

## 變更紀錄政策

- 所有 production code 與文件變更，**必須同步更新 `CHANGELOG.md`**（`[Unreleased]` 段落）。
- 版本號更動時，同步更新 `VERSION` 檔案。

## 測試政策

- **完成任何 code change 前，必須執行**：
  ```bash
  python3 -m pytest -q tests/
  ```
- 亦可執行：
  ```bash
  python3 -m unittest discover -s tests -v
  ```
- 既有失敗：`tests/test_multiagent_e2e.py::TestMultiAgentE2E::test_five_agents_three_rounds_no_conflict`（agent TX count mismatch，pre-existing）。
- 不得引入**新的**測試失敗。

## Policy Check 政策

- 完成任何 phase 前，必須執行：
  ```bash
  python3 -m policy_check --repo .
  ```
- policy engine pinned SHA：`77a3e8381eeced9dbba623e450ed6a5c1fcc7b18`。
- 安裝命令：
  ```bash
  python3 -m pip install --user --disable-pip-version-check \
    "git+https://github.com/hamanpaul/paulsha-conventions.git@77a3e8381eeced9dbba623e450ed6a5c1fcc7b18"
  ```

## Agent 檔案同步政策

- **禁止單獨修改以下任一檔案**；必須同時更新四份：
  - `CLAUDE.md`
  - `AGENTS.md`
  - `GEMINI.md`
  - `.github/copilot-instructions.md`（marker 區段）
- 檔案首行必須保留：
  ```
  <!-- managed-by: hamanpaul/paulsha-conventions@v1.0.4 -->
  ```

## PR 政策

- 所有 PR 必須填寫 `.github/pull_request_template.md` 的 Policy Checklist（R-11）。
- PR checklist 項目：
  - [ ] 分支不是 `main`
  - [ ] `CHANGELOG.md` 已更新
  - [ ] `VERSION` 已更新（若有版本號變動）
  - [ ] `python3 -m pytest -q tests/` 通過（無新失敗）
  - [ ] `python3 -m policy_check --repo .` 通過
  - [ ] 四份 agent 檔案已同步（若有修改）
  - [ ] 已標記 exemption label（若適用）

## Exemption Label 白名單

以下 label 可豁免特定 policy 規則（需在 PR 標記）：

| Label | 豁免項目 |
|-------|---------|
| `policy-exempt-changelog` | 免更新 CHANGELOG（如純文件拼字修正）|
| `policy-exempt-tests` | 免跑測試（如純 CI/文件變更）|
| `policy-exempt-version` | 免更新 VERSION（如非 release 的 chore）|

## 語言政策

- 本 repo 文件、註解、docstring、README、規格、commit message 與 AI 回覆**一律使用繁體中文**。

## Commit 政策

- Commit message 使用 Conventional Commits 格式（繁中 subject）。
- 所有 AI-assisted commit 必須包含 trailer：
  ```
  Co-authored-by: Copilot <223556219+Copilot@users.noreply.github.com>
  ```

## v1.0.1 新增規則（issue 連結 / docs 對齊 / 語言）
> 本段於 policy 1.0.1 隨 R-17 / R-18 與語言規範新增。

- **R-17（PR↔issue，FAIL gate）**：PR body 引用 issue（`#N`）時必須為 closing-keyword 形式（`Closes` / `Fixes` / `Resolves #N`），merge 由 GitHub 原生自動關閉 issue 並留下 cross-reference；只引用不關閉時上 `policy-exempt:issue-link`。
- **R-18（docs 對齊，WARN，不擋 merge）**：`code_paths` 有變動但 `README.md` / `docs/**` 未同步時提醒；純內部變動可上 `policy-exempt:docs-sync`。
- **語言規範（checklist）**：依 repo 來源決定語言——`github.com/hamanpaul/*`、`github.com/paulc-arc/*` → zh-tw；arcadyan GitLab → en_US。涵蓋 PR 標題／內文與所有 comment。本 repo 屬 `hamanpaul` → zh-tw。
- **動工前（軟性，不打斷流程）**：若任務對應某 issue，`gh issue view <N>` 核對相關性後分支可命名 `feature/<N>-<slug>`，開 PR 於 body 寫 `Closes #N`；查無對應 issue 照常進行，不另開、不停。
- **Exemption 白名單新增**：`policy-exempt:issue-link`（R-17）、`policy-exempt:docs-sync`（R-18）。
