# Issue #44 Bootloader Recovery + paulsha-conventions Adoption Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Add an agent-driven bootloader recovery interactive lease that preserves human console ownership via stash-and-restore, and adopt `hamanpaul/paulsha-conventions` policy v1.0.0 (R-01 ~ R-16) in the same PR.

**Architecture:** Two phases bundled in one PR. Phase A bootstraps conventions (CHANGELOG / VERSION / `.paul-project.yml` / agent files / policy-check workflow) so phase B's code commits satisfy R-09 (CHANGELOG matches code). Phase B extends profile schema with `bootloader_prompts`, adds `BOOTLOADER` self_test classification, and relaxes `interactive_open` with `allow_attached=True` using stash-and-restore semantics: human session-layer lease is moved to `session._stashed_human_lease`, bridge enters suspend mode, agent recovery lease takes over; on close, deferred buffer is flushed and the stashed human lease is restored.

**Tech Stack:** Python 3.11, dataclasses, threading.RLock for `_lock`, paulsha-conventions policy engine v1.0.0 (Python pkg), GitHub Actions reusable workflow, OpenSpec v1.3.0 for spec gating, existing pytest test bench.

**Spec references:**
- Brainstorming narrative: `docs/superpowers/specs/2026-05-07-issue-44-bootloader-recovery-design.md`
- OpenSpec change: `openspec/changes/2026-05-07-bootloader-recovery-44/` (proposal / design / tasks / specs/session-selftest / specs/session-interactive)
- paulsha-conventions main HEAD pinned: `ff1a031172ec24fc155699f9f3ce5bdea24d9e24`

**Branch:** `feature/bootloader-recovery-44` (already created; commits `7e12ae0` + `203269c` hold the spec).

---

## Phase A — paulsha-conventions bootstrap

R-09 ("code change must update CHANGELOG `[Unreleased]`") is enforced by policy-check. We must scaffold conventions BEFORE any phase B code commit, otherwise CI will fail on the first feature commit. Phase A produces a single squash-merge-able state with `policy_check` green; phase B then layers feature work on top.

### Task A1: Capture pytest baseline

**Files:**
- (read-only) `tests/`

- [ ] **Step 1: Run the full test suite to capture the green-baseline output**

Run: `python3 -m pytest -q tests/ 2>&1 | tail -20`
Expected: a small number of pre-existing failures (notably `test_multiagent_e2e.test_five_agents_three_rounds_no_conflict` per the selftest-collab-handoff archive note); record the exact failing tests so we can detect regressions later.

- [ ] **Step 2: Save baseline list to a scratch file outside of git**

Run:
```bash
python3 -m pytest -q tests/ 2>&1 | tee /tmp/serialwrap-pretest-baseline.txt
```
Expected: file written. We do NOT commit this file; it's a workbench reference.

### Task A2: Add `.paul-project.yml`

**Files:**
- Create: `/home/paul_chen/prj_pri/serialwrap/.paul-project.yml`

- [ ] **Step 1: Create `.paul-project.yml` at repo root**

Content (exact):
```yaml
policy_profile: flat
policy_version: "1.0.0"
code_paths:
  - "sw_core/**"
  - "sw_mcp/**"
  - "tools/**"
  - "tests/**"
  - "profiles/**"
  - "serialwrap"
  - "serialwrapd.py"
  - "serialwrap-mcp"
  - "install.sh"
cli:
  - command: "./serialwrap"
    help_args: ["--help"]
    reflected_in: "README.md"
    marker: "serialwrap-help"
```

- [ ] **Step 2: Verify YAML parses**

Run: `python3 -c "import yaml; yaml.safe_load(open('.paul-project.yml')); print('ok')"`
Expected: `ok`

### Task A3: Add `VERSION`

**Files:**
- Create: `/home/paul_chen/prj_pri/serialwrap/VERSION`

- [ ] **Step 1: Create VERSION**

Content (exactly one line, no trailing newline issues):
```
0.0.0
```

- [ ] **Step 2: Verify**

Run: `cat VERSION`
Expected: `0.0.0`

### Task A4: Add `CHANGELOG.md` skeleton

**Files:**
- Create: `/home/paul_chen/prj_pri/serialwrap/CHANGELOG.md`

We're populating the `[Unreleased]` section now with the conventions-adoption entry; the issue-#44 entries are added incrementally during phase B (so each commit satisfies R-09 by editing CHANGELOG together).

- [ ] **Step 1: Create CHANGELOG.md**

Content (exact):
````markdown
# Changelog

本專案所有重大變更都會記錄在此檔案。

格式基於 [Keep a Changelog 1.1.0](https://keepachangelog.com/zh-TW/1.1.0/)，
本專案遵循 hamanpaul project policy v1.0.0。

## [Unreleased]

### Added
- **paulsha-conventions v1.0.0 baseline**：repo 首次接入 policy engine（`policy_profile: flat`、`policy_version: 1.0.0`）。新增 `.paul-project.yml`、`VERSION`、`CHANGELOG.md`、`CLAUDE.md`、`AGENTS.md`、`GEMINI.md`、`.github/pull_request_template.md`、`.github/workflows/policy-check.yml`；既有 `.github/copilot-instructions.md` 補 managed-by marker 與 policy_version 段。

### Changed
- **README.md**：補齊 `## Install` / `## Usage` / `## Version` 必備段落（R-02），既有內容保留。

### Notes
- 後續 PR 自動受 R-01 ~ R-16 規範；merge 前 `python3 -m policy_check --repo .` 必須全綠。
````

- [ ] **Step 2: Verify Keep-a-Changelog format**

Run: `grep -E '^# Changelog$|^## \[Unreleased\]$|^### (Added|Changed|Notes)' CHANGELOG.md`
Expected: All four lines printed (no missing headings).

### Task A5: Add agent convention files (`CLAUDE.md` / `AGENTS.md` / `GEMINI.md`)

**Files:**
- Create: `/home/paul_chen/prj_pri/serialwrap/CLAUDE.md`
- Create: `/home/paul_chen/prj_pri/serialwrap/AGENTS.md`
- Create: `/home/paul_chen/prj_pri/serialwrap/GEMINI.md`

R-13 / R-14 require all four agent files (these three plus the existing `.github/copilot-instructions.md`) to share the same `policy_version` and a `managed-by` marker. We use one shared body and only vary the marker per file.

- [ ] **Step 1: Define the shared body once (paste into each file with file-specific marker line)**

Shared body (tagged `<BODY>` here for clarity — only the body, no marker line):
````markdown
<!-- BODY -->
policy_version: 1.0.0

# Agent Policy Checklist

本 repo 受 hamanpaul project policy v1.0.0 管轄。
所有 agent 進入 session 時，必須依下列 checklist 行動。

## 本 repo 的 profile
- policy_profile: `flat` （見 `.paul-project.yml`）
- policy_version: `1.0.0`

## 動工前
- [ ] 確認當前分支不是 `main`
  - 若在 `main`，先開 `feature/<slug>` 分支
- [ ] 若本任務跨多個子項，先建議用 `git worktree` 拆開

## 改 code 時
- [ ] 同一 PR 必須同步更新 `CHANGELOG.md [Unreleased]`
- [ ] 除非可明確標示為 docs-only / test-only / chore，否則不得省略 CHANGELOG
- [ ] code_paths 涵蓋的檔案變動皆視為 code change

## 完成任務（claim done）前
- [ ] `CHANGELOG.md [Unreleased]` 有對應 entry（或 PR 標 `skip-changelog` + 理由）
- [ ] `VERSION` 內容與意圖一致
- [ ] `.github/pull_request_template.md` checklist 全勾
- [ ] 測試全綠（本 repo: `python3 -m pytest -q tests/`）
- [ ] `python3 -m policy_check --repo .` 無任何 failure
- [ ] 若跳過任何檢查，PR 必須帶對應豁免 label + 理由

## 禁止
- 直接 commit 到 `main`
- 建立不符合命名規則的分支（必須 `feature/<slug>`）
- 發明新 `policy-exempt:*` label（**只能用 policy 列舉的白名單**）
- 修改本檔而不同步其他三份 agent convention 檔

## Exemption Labels 白名單
- `policy-exempt:readme-sections` — R-02
- `policy-exempt:changelog-format` — R-04
- `policy-exempt:pr-title` — R-10
- `policy-exempt:branch-name` — R-12
- `policy-exempt:agent-files` — R-13
- `policy-exempt:cli-help` — R-16
- `skip-changelog` — R-09（特殊用途，需附理由）
- `wip` — R-11（自動通過 PR body checkbox 未全勾）
````

- [ ] **Step 2: Create `CLAUDE.md`**

Content = first line marker + shared body:
```markdown
<!-- managed-by: hamanpaul/paulsha-conventions@v1.0.0 -->
<!-- 若修改此檔，同步更新 CLAUDE.md / AGENTS.md / GEMINI.md / .github/copilot-instructions.md 四份 -->
policy_version: 1.0.0

# Agent Policy Checklist

[... rest of body verbatim from Step 1 ...]
```

(Replace `[... rest of body verbatim from Step 1 ...]` with the actual body content from Step 1, starting after `policy_version: 1.0.0` since that line is already in the marker block.)

- [ ] **Step 3: Create `AGENTS.md` with identical content to `CLAUDE.md`**

Run: `cp CLAUDE.md AGENTS.md`
Expected: file copied.

- [ ] **Step 4: Create `GEMINI.md` with identical content**

Run: `cp CLAUDE.md GEMINI.md`
Expected: file copied.

- [ ] **Step 5: Verify all four agent files share the policy_version line**

Run:
```bash
grep -H "policy_version: 1.0.0" CLAUDE.md AGENTS.md GEMINI.md .github/copilot-instructions.md
```
Expected: 4 lines, one per file. Note: `.github/copilot-instructions.md` won't yet have it; that's fixed in Task A6.

### Task A6: Update `.github/copilot-instructions.md` with marker + policy_version

**Files:**
- Modify: `/home/paul_chen/prj_pri/serialwrap/.github/copilot-instructions.md` (prepend 3 lines, keep existing 11KB intact)

- [ ] **Step 1: Prepend the marker block**

Use `Edit` to add three lines at the very top of the file (before whatever is currently line 1):
```markdown
<!-- managed-by: hamanpaul/paulsha-conventions@v1.0.0 -->
<!-- 若修改此檔，同步更新 CLAUDE.md / AGENTS.md / GEMINI.md / .github/copilot-instructions.md 四份 -->
policy_version: 1.0.0

```
(Note the trailing blank line to separate from existing content.)

- [ ] **Step 2: Verify all four agent files now share policy_version**

Run:
```bash
grep -H "policy_version: 1.0.0" CLAUDE.md AGENTS.md GEMINI.md .github/copilot-instructions.md
```
Expected: 4 matching lines.

### Task A7: Add `.github/pull_request_template.md`

**Files:**
- Create: `/home/paul_chen/prj_pri/serialwrap/.github/pull_request_template.md`

- [ ] **Step 1: Create the PR template**

Content (exact):
```markdown
## Summary

<!-- One paragraph explaining what this PR does and why. -->

## Test Plan

<!-- Bulleted checklist of how to verify this PR. -->

## Policy Checklist (R-11)

- [ ] 我已確認當前分支不是 `main`，且分支名符合 `feature/<slug>` 或 `wt/<feature>/<subtask>`
- [ ] `CHANGELOG.md [Unreleased]` 有對應 entry（或 PR 已標 `skip-changelog` + 理由）
- [ ] `VERSION` 內容與本 PR 意圖一致（release PR 才可偏離最新 tag）
- [ ] 測試全綠：`python3 -m pytest -q tests/`
- [ ] policy 檢查通過：`python3 -m policy_check --repo .`
- [ ] 若跳過任何檢查，本 PR 已帶對應豁免 label + 理由

## Issue Reference

<!-- Closes #N or References #N -->
```

### Task A8: Add policy-check workflow

**Files:**
- Create: `/home/paul_chen/prj_pri/serialwrap/.github/workflows/policy-check.yml`

- [ ] **Step 1: Create the workflow**

Content (exact, with the pinned SHA from spec):
```yaml
name: Policy Check
on: [pull_request]

permissions:
  contents: read

jobs:
  policy:
    uses: hamanpaul/paulsha-conventions/.github/workflows/reusable-policy-check.yml@ff1a031172ec24fc155699f9f3ce5bdea24d9e24
    with:
      policy_profile: flat
      policy_version: "1.0.0"
      policy_engine_ref: ff1a031172ec24fc155699f9f3ce5bdea24d9e24
```

- [ ] **Step 2: Verify YAML syntax**

Run: `python3 -c "import yaml; yaml.safe_load(open('.github/workflows/policy-check.yml')); print('ok')"`
Expected: `ok`

### Task A9: Audit README.md required sections (R-02)

**Files:**
- Read first: `/home/paul_chen/prj_pri/serialwrap/README.md`
- Modify if needed: `/home/paul_chen/prj_pri/serialwrap/README.md`

- [ ] **Step 1: Check for required sections**

Run: `grep -nE '^## (Install|Usage|Version)' README.md`
Expected: 3 matching lines. If fewer than 3, proceed to Step 2.

- [ ] **Step 2: For each missing heading, append a stub section at the end of README.md**

If `## Install` is missing, append:
```markdown

## Install

See `install.sh` and the [Quick start](#quick-start) section above. In short:

```bash
./install.sh
```
```

If `## Usage` is missing, append:
```markdown

## Usage

See sections above for full CLI usage. Quick reference:

<!-- BEGIN: cli-help marker="serialwrap-help" -->
<!-- END: cli-help marker="serialwrap-help" -->

The `cli-help` marker block is auto-populated by `paulsha-conventions/scripts/update-cli-help.sh`.
```

If `## Version` is missing, append:
```markdown

## Version

`VERSION` 檔（repo root）為專案版號 single source of truth。

當前版本：`0.0.0`（baseline；首次接入 paulsha-conventions v1.0.0）。
```

- [ ] **Step 3: Verify all three sections now exist**

Run: `grep -nE '^## (Install|Usage|Version)' README.md`
Expected: 3 matching lines.

### Task A10: Install policy engine locally and run first check

**Files:**
- (no file changes)

- [ ] **Step 1: Install policy_check pinned to the same SHA as the workflow**

Run:
```bash
python3 -m pip install --user --disable-pip-version-check \
  "git+https://github.com/hamanpaul/paulsha-conventions.git@ff1a031172ec24fc155699f9f3ce5bdea24d9e24"
```
Expected: install succeeds (pulls PyYAML if missing).

- [ ] **Step 2: Run the policy check**

Run: `python3 -m policy_check --repo .`
Expected: All R-01 ~ R-16 pass. If R-09 fails because the bootstrap files themselves count as code-paths-adjacent changes without CHANGELOG, see Step 3.

- [ ] **Step 3: If any rule fails, stop and inspect**

Each failure prints rule ID + reason. Likely culprits and fixes:
- R-04 (CHANGELOG format): missing `## [Unreleased]` heading or wrong order — re-check Task A4.
- R-08 (`.paul-project.yml` schema): missing `policy_profile`/`policy_version` — re-check Task A2.
- R-13/R-14 (agent files): missing `policy_version` line in any of the four files — re-check Task A5/A6.
- R-15 (workflow pinning): SHA in workflow doesn't match expected format — re-check Task A8.

Iterate until `policy_check` reports zero failures.

### Task A11: Run CLI help marker injection

**Files:**
- Modify: `README.md` (only the marker block, via the helper script)

- [ ] **Step 1: Locate the conventions repo's helper script**

The skill's `paulsha-conventions` working copy at `/tmp/paulsha-conventions` exists from the brainstorming session. If it's gone, re-clone:
```bash
git -C /tmp clone --depth 1 https://github.com/hamanpaul/paulsha-conventions.git
```

- [ ] **Step 2: Run the helper script from the serialwrap repo**

Run:
```bash
cd /home/paul_chen/prj_pri/serialwrap
bash /tmp/paulsha-conventions/scripts/update-cli-help.sh
```
Expected: `README.md` `cli-help` marker block populated with `./serialwrap --help` output.

- [ ] **Step 3: Re-run policy_check**

Run: `python3 -m policy_check --repo .`
Expected: still green; R-16 now passes with help text matching declared CLI.

### Task A12: Commit phase A

**Files:**
- (commit all phase A additions)

- [ ] **Step 1: Stage all phase A files**

Run:
```bash
git add .paul-project.yml VERSION CHANGELOG.md \
        CLAUDE.md AGENTS.md GEMINI.md \
        .github/copilot-instructions.md \
        .github/pull_request_template.md \
        .github/workflows/policy-check.yml \
        README.md
```

- [ ] **Step 2: Commit**

Run:
```bash
git commit -m "$(cat <<'EOF'
chore(policy): adopt paulsha-conventions v1.0.0 (R-01 ~ R-16 baseline)

Bootstrap policy engine for serialwrap: add .paul-project.yml (profile=flat,
version=1.0.0), VERSION, CHANGELOG.md, CLAUDE.md / AGENTS.md / GEMINI.md
(synchronized with existing .github/copilot-instructions.md), PR template
with R-11 checklist, and policy-check workflow dual-pinned to
hamanpaul/paulsha-conventions@ff1a031172ec24fc155699f9f3ce5bdea24d9e24.

README.md gains the required ## Install / ## Usage / ## Version sections
and a cli-help marker block populated from `./serialwrap --help` via the
conventions helper script.

`python3 -m policy_check --repo .` is green at this commit.

Co-Authored-By: Claude Opus 4.7 <noreply@anthropic.com>
EOF
)"
```

Expected: clean commit on `feature/bootloader-recovery-44`.

- [ ] **Step 3: Confirm policy_check still green**

Run: `python3 -m policy_check --repo .`
Expected: zero failures.

---

## Phase B — Issue #44 implementation

TDD strictly: each behavior gets a failing test first, then minimal implementation, then a commit. CHANGELOG `[Unreleased]` is updated alongside each commit so R-09 stays green.

### Task B1: Add constants

**Files:**
- Modify: `/home/paul_chen/prj_pri/serialwrap/sw_core/constants.py` (append)

- [ ] **Step 1: Read current constants.py**

Run: `cat sw_core/constants.py`
Expected: see existing constants; identify where to append (end of file).

- [ ] **Step 2: Append new constants**

Append to `sw_core/constants.py`:
```python


# Bootloader recovery (issue #44)
MAX_RECOVERY_LEASE_S: float = 120.0
"""Maximum timeout (seconds) for an interactive lease opened with
``allow_attached=True``. Caller-supplied timeout_s is clamped to this value
so a stuck agent cannot indefinitely suspend a human observer."""

BOOTLOADER_RX_TAIL_BYTES: int = 512
"""Byte window of bridge RX tail to scan for bootloader_prompts during
self_test classification of ATTACHED state."""
```

- [ ] **Step 3: Verify import**

Run:
```bash
python3 -c "from sw_core.constants import MAX_RECOVERY_LEASE_S, BOOTLOADER_RX_TAIL_BYTES; print(MAX_RECOVERY_LEASE_S, BOOTLOADER_RX_TAIL_BYTES)"
```
Expected: `120.0 512`

### Task B2: Profile schema — `bootloader_prompts` field

**Files:**
- Modify: `/home/paul_chen/prj_pri/serialwrap/sw_core/config.py` (`ProfileTemplate`, `SessionProfile`, `_template_from_dict`)
- Test: `/home/paul_chen/prj_pri/serialwrap/tests/test_config_profiles.py`

- [ ] **Step 1: Write the failing tests**

Append to `tests/test_config_profiles.py` (or create new test functions inside the existing test class — match the file's existing pattern):
```python
def test_profile_template_default_bootloader_prompts_is_empty():
    """ProfileTemplate.bootloader_prompts defaults to []."""
    from sw_core.config import ProfileTemplate
    tpl = ProfileTemplate(profile_name="x")
    assert tpl.bootloader_prompts == []


def test_profile_loader_accepts_bootloader_prompts_list(tmp_path):
    """Profile YAML with bootloader_prompts list is parsed into ProfileTemplate.bootloader_prompts."""
    import yaml
    from sw_core.config import load_profiles

    profile_dir = tmp_path / "profiles"
    profile_dir.mkdir()
    (profile_dir / "test.yaml").write_text(yaml.safe_dump({
        "profiles": {
            "uboot-template": {
                "platform": "bcm",
                "prompt_regex": "^# $",
                "bootloader_prompts": ["^=> $", "^Marvell>> $"],
            },
        },
    }))
    result = load_profiles(str(profile_dir))
    tpl = result.templates_by_file["test.yaml"]["uboot-template"]
    assert tpl.bootloader_prompts == ["^=> $", "^Marvell>> $"]


def test_profile_loader_missing_bootloader_prompts_yields_empty(tmp_path):
    """Profile YAML without bootloader_prompts gets [] (backward compat)."""
    import yaml
    from sw_core.config import load_profiles

    profile_dir = tmp_path / "profiles"
    profile_dir.mkdir()
    (profile_dir / "test.yaml").write_text(yaml.safe_dump({
        "profiles": {
            "no-uboot": {"platform": "prpl", "prompt_regex": "^# $"},
        },
    }))
    result = load_profiles(str(profile_dir))
    tpl = result.templates_by_file["test.yaml"]["no-uboot"]
    assert tpl.bootloader_prompts == []
```

If the existing tests don't reference `templates_by_file`, inspect `LoadResult` (`sw_core/config.py:236`) for the actual attribute name and adjust.

- [ ] **Step 2: Run the new tests to confirm they fail**

Run: `python3 -m pytest tests/test_config_profiles.py -v -k bootloader`
Expected: 3 tests FAIL (`bootloader_prompts` attribute does not exist).

- [ ] **Step 3: Add the `bootloader_prompts` field to `ProfileTemplate`**

In `sw_core/config.py`, modify the `ProfileTemplate` dataclass (around line 21):
```python
@dataclasses.dataclass
class ProfileTemplate:
    profile_name: str
    platform: str = "prpl"
    prompt_regex: str = r"(?m)^root@prplOS:.*# "
    login_regex: str = r"(?mi)^login:\\s*$"
    password_regex: str = r"(?mi)^password:\\s*$"
    post_login_cmd: str = ""
    ready_probe: str = "echo __READY__${nonce}"
    username: str | None = None
    user_env: str | None = None
    pass_env: str | None = None
    env_file: str | None = None
    timeout_s: float = 10.0
    quiet_window_s: float = 2.0
    hard_timeout_s: float = 60.0
    log_dir: str | None = None
    uart: UartProfile = dataclasses.field(default_factory=UartProfile)
    bootloader_prompts: list[str] = dataclasses.field(default_factory=list)
```

- [ ] **Step 4: Add the `bootloader_prompts` field to `SessionProfile`** (frozen, so default_factory needs `dataclasses.field(default_factory=tuple)` or a frozen-friendly default)

In `sw_core/config.py`, modify `SessionProfile` (around line 41). Because `SessionProfile` is `frozen=True`, mutable defaults aren't allowed. Use a tuple:
```python
@dataclasses.dataclass(frozen=True)
class SessionProfile:
    profile_name: str
    com: str
    act_no: int
    alias: str
    device_by_id: str
    platform: str
    prompt_regex: str = r"(?m)^root@prplOS:.*# "
    login_regex: str = r"(?mi)^login:\\s*$"
    password_regex: str = r"(?mi)^password:\\s*$"
    post_login_cmd: str = ""
    ready_probe: str = "echo __READY__${nonce}"
    username: str | None = None
    user_env: str | None = None
    pass_env: str | None = None
    env_file: str | None = None
    timeout_s: float = 10.0
    quiet_window_s: float = 2.0
    hard_timeout_s: float = 60.0
    log_dir: str | None = None
    uart: UartProfile = dataclasses.field(default_factory=UartProfile)
    bootloader_prompts: tuple[str, ...] = ()
```

- [ ] **Step 5: Wire the loader (`_template_from_dict`)**

In `sw_core/config.py`, modify `_template_from_dict` (around line 114):
```python
def _template_from_dict(name: str, raw: dict[str, Any], *, base_dir: str) -> ProfileTemplate:
    bootloader_raw = raw.get("bootloader_prompts") or []
    if isinstance(bootloader_raw, list):
        bootloader_prompts = [str(p) for p in bootloader_raw if isinstance(p, str) and p.strip()]
    else:
        bootloader_prompts = []
    return ProfileTemplate(
        profile_name=name,
        platform=str(raw.get("platform") or "prpl").strip().lower(),
        # ... (existing fields unchanged)
        uart=_load_uart(raw.get("uart")),
        bootloader_prompts=bootloader_prompts,
    )
```

(Keep the existing fields verbatim; only add the new `bootloader_prompts=` line and the parsing block above.)

- [ ] **Step 6: Wire `_merge_session` to propagate to `SessionProfile`**

In `sw_core/config.py`, find `_merge_session` (around line 185). It composes a `SessionProfile` from a `ProfileTemplate` + per-target overrides. Find the return / construction site and add:
```python
        bootloader_prompts=tuple(template.bootloader_prompts),
```
(near the other field assignments — check the function body to find the exact site).

- [ ] **Step 7: Run the new tests to confirm they pass**

Run: `python3 -m pytest tests/test_config_profiles.py -v -k bootloader`
Expected: 3 PASS.

- [ ] **Step 8: Run the full config-profiles test file to confirm no regression**

Run: `python3 -m pytest tests/test_config_profiles.py -q`
Expected: all green.

- [ ] **Step 9: Update CHANGELOG `[Unreleased]`**

Edit `CHANGELOG.md`. Add to `### Added` (preserving existing entries):
```markdown
- **`bootloader_prompts` profile schema field**：opt-in `list[str]`（預設 `[]`），宣告 bootloader prompt regex（U-Boot / Marvell / Broadcom CFE 等）；`SessionProfile` 暴露為 `tuple[str, ...]`。
```

- [ ] **Step 10: Commit**

Run:
```bash
git add sw_core/config.py tests/test_config_profiles.py CHANGELOG.md
git commit -m "$(cat <<'EOF'
feat(profile): add bootloader_prompts schema field

Opt-in list of regexes the daemon scans against bridge RX tail to detect
when target dropped into a bootloader (U-Boot / Marvell / Broadcom CFE).
Default is empty list, preserving existing behavior. Plumbed through
ProfileTemplate (mutable list) and SessionProfile (frozen tuple) via
_template_from_dict and _merge_session.

Closes part of #44 (detection layer).

Co-Authored-By: Claude Opus 4.7 <noreply@anthropic.com>
EOF
)"
```

- [ ] **Step 11: Verify policy_check still green**

Run: `python3 -m policy_check --repo .`
Expected: zero failures.

### Task B3: `_matches_any_bootloader_prompt` helper

**Files:**
- Modify: `/home/paul_chen/prj_pri/serialwrap/sw_core/session_manager.py` (add free helper near top of module or as private staticmethod)
- Test: extend `tests/test_session_bind.py` or new `tests/test_bootloader_recovery.py`

- [ ] **Step 1: Create `tests/test_bootloader_recovery.py` and write the failing helper test**

Create `tests/test_bootloader_recovery.py`:
```python
"""Tests for bootloader recovery (issue #44)."""

import re
import unittest


class TestBootloaderPromptMatcher(unittest.TestCase):
    def test_matches_returns_pattern_when_last_line_matches(self):
        from sw_core.session_manager import _matches_any_bootloader_prompt
        rx_tail = "Loading...\nUbooting\n=> "
        result = _matches_any_bootloader_prompt(rx_tail, ["^=> $", "^Marvell>> $"])
        self.assertEqual(result, "^=> $")

    def test_matches_returns_none_when_no_match(self):
        from sw_core.session_manager import _matches_any_bootloader_prompt
        rx_tail = "root@prplOS:~# "
        self.assertIsNone(_matches_any_bootloader_prompt(rx_tail, ["^=> $"]))

    def test_matches_handles_empty_pattern_list(self):
        from sw_core.session_manager import _matches_any_bootloader_prompt
        self.assertIsNone(_matches_any_bootloader_prompt("=> ", []))

    def test_matches_handles_empty_rx_tail(self):
        from sw_core.session_manager import _matches_any_bootloader_prompt
        self.assertIsNone(_matches_any_bootloader_prompt("", ["^=> $"]))

    def test_matches_inspects_only_last_line(self):
        from sw_core.session_manager import _matches_any_bootloader_prompt
        # Earlier line happens to look like a prompt, but the LAST line is OS prompt
        rx_tail = "=> previous line\nroot@prplOS:~# "
        self.assertIsNone(_matches_any_bootloader_prompt(rx_tail, ["^=> $"]))


if __name__ == "__main__":
    unittest.main()
```

- [ ] **Step 2: Run the test to confirm it fails**

Run: `python3 -m pytest tests/test_bootloader_recovery.py::TestBootloaderPromptMatcher -v`
Expected: 5 FAIL with `ImportError: cannot import name '_matches_any_bootloader_prompt'`.

- [ ] **Step 3: Add the helper to `sw_core/session_manager.py`**

Add this free function near the top of the module (after imports, before the first dataclass — around line 40):
```python
def _matches_any_bootloader_prompt(rx_tail: str, patterns: list[str] | tuple[str, ...]) -> str | None:
    """Return the first pattern whose regex matches the LAST non-empty line of
    ``rx_tail``, or None if no pattern matches.

    Used by self_test to classify ATTACHED state as BOOTLOADER when target is
    sitting at a U-Boot / vendor bootloader prompt.
    """
    if not rx_tail or not patterns:
        return None
    lines = rx_tail.splitlines()
    if not lines:
        return None
    last_line = lines[-1]
    if not last_line.strip():
        # If the very last line is whitespace, look one line up
        if len(lines) >= 2:
            last_line = lines[-2]
        else:
            return None
    for pattern in patterns:
        try:
            if re.search(pattern, last_line):
                return pattern
        except re.error:
            continue
    return None
```

(Confirm `re` is already imported at top of the file; if not, add `import re`.)

- [ ] **Step 4: Run the tests**

Run: `python3 -m pytest tests/test_bootloader_recovery.py::TestBootloaderPromptMatcher -v`
Expected: 5 PASS.

- [ ] **Step 5: Commit**

Run:
```bash
git add sw_core/session_manager.py tests/test_bootloader_recovery.py
git commit -m "$(cat <<'EOF'
feat(self_test): add _matches_any_bootloader_prompt helper

Pure-string matcher that tests the last non-empty line of an RX tail
against an ordered list of regex patterns and returns the first match.
Foundation for the BOOTLOADER classification.

Co-Authored-By: Claude Opus 4.7 <noreply@anthropic.com>
EOF
)"
```

### Task B4: `self_test` BOOTLOADER classification

**Files:**
- Modify: `sw_core/session_manager.py` (`self_test` ATTACHED branch at ~line 1738)
- Test: `tests/test_bootloader_recovery.py`

- [ ] **Step 1: Write the failing tests**

Append to `tests/test_bootloader_recovery.py`:
```python
import dataclasses
import os
import tempfile
import unittest.mock as mock

from sw_core.config import SessionProfile, UartProfile
from sw_core.device_watcher import DeviceInfo
from sw_core.session_manager import InteractiveLease, SessionManager
from sw_core.wal import WalWriter


def _make_profile_with_bootloader(
    name="p",
    com="COM0",
    alias="lab+1",
    by_id="/dev/serial/by-id/orig",
    bootloader_prompts=("^=> $",),
):
    return SessionProfile(
        profile_name=name,
        com=com,
        act_no=1,
        alias=alias,
        device_by_id=by_id,
        platform="bcm",
        prompt_regex=r"^# $",
        login_regex=r"login:",
        password_regex=r"password:",
        ready_probe=r"echo __READY__${nonce}",
        bootloader_prompts=tuple(bootloader_prompts),
    )


class TestSelfTestBootloaderClassification(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()

    def tearDown(self):
        self._tmp.cleanup()

    def _make_mgr_with_attached_bridge(self, *, bootloader_prompts, rx_tail):
        profiles = [_make_profile_with_bootloader(bootloader_prompts=bootloader_prompts)]
        mgr = SessionManager(
            profiles,
            WalWriter(wal_dir=self._tmp.name),
            on_ready=lambda _sid: None,
            on_detached=lambda _sid: None,
        )
        session = mgr.get_session("COM0")
        assert session is not None
        bridge = mock.MagicMock()
        bridge.snapshot.return_value = {
            "running": True,
            "serial_alive": True,
            "vtty_alive": True,
            "vtty": "/dev/pts/9",
            "interactive_owner": None,
        }
        bridge.rx_tail.return_value = rx_tail
        session.bridge = bridge
        session.state = "ATTACHED"
        session.attached_real_path = "/dev/ttyUSB0"
        with mgr._lock:
            mgr._devices = {"/dev/serial/by-id/orig": DeviceInfo(by_id="/dev/serial/by-id/orig", real_path="/dev/ttyUSB0")}
        return mgr, session, bridge

    def test_bootloader_classification_when_rx_tail_matches(self):
        mgr, _, _ = self._make_mgr_with_attached_bridge(
            bootloader_prompts=("^=> $",),
            rx_tail="Loading...\n=> ",
        )
        result = mgr.self_test("COM0")
        self.assertTrue(result["ok"])
        self.assertEqual(result["classification"], "BOOTLOADER")
        self.assertEqual(result["recommended_action"], "recover_interactive")
        self.assertEqual(result["matched_prompt"], "^=> $")
        self.assertIn("=> ", result["rx_tail"])

    def test_attached_not_ready_when_bootloader_prompts_empty(self):
        mgr, _, _ = self._make_mgr_with_attached_bridge(
            bootloader_prompts=(),
            rx_tail="=> ",
        )
        result = mgr.self_test("COM0")
        self.assertEqual(result["classification"], "ATTACHED_NOT_READY")
        self.assertEqual(result["recommended_action"], "console_attach")

    def test_bootloader_preferred_when_both_prompts_match(self):
        # OS prompt regex (per profile) is ^# $, which would match "# " in rx_tail.
        # Bootloader regex is ^=> $. Last line is "=> " — only bootloader matches.
        # This case is unambiguous (last-line-only). The "could match both" case
        # arises if someone supplies overlapping patterns; verify ordering.
        mgr, _, _ = self._make_mgr_with_attached_bridge(
            bootloader_prompts=("^=> $",),
            rx_tail="boot...\n# echo something\n=> ",
        )
        result = mgr.self_test("COM0")
        self.assertEqual(result["classification"], "BOOTLOADER")


if __name__ == "__main__":
    unittest.main()
```

- [ ] **Step 2: Run the new tests to confirm they fail**

Run: `python3 -m pytest tests/test_bootloader_recovery.py::TestSelfTestBootloaderClassification -v`
Expected: 3 FAIL with `KeyError: 'classification' == 'BOOTLOADER'` or similar (ATTACHED_NOT_READY returned instead).

- [ ] **Step 3: Add the BOOTLOADER branch to `self_test`**

In `sw_core/session_manager.py`, locate the ATTACHED block (around line 1738) — it currently reads:
```python
if session.state == "ATTACHED":
    if session.profile.platform == "passthrough":
        classification = "PASSTHROUGH"
        recommended_action = "console_attach"
    elif session.last_error == "LOGIN_REQUIRED":
        classification = "LOGIN_REQUIRED"
        recommended_action = "console_attach"
    elif session.last_error == "REBOOTING":
        classification = "REBOOTING"
        recommended_action = "wait_or_console_attach"
    else:
        classification = "ATTACHED_NOT_READY"
        recommended_action = "console_attach"
    return {
        "ok": True,
        "classification": classification,
        # ... existing fields
    }
```

Modify to insert the bootloader branch between REBOOTING and the else:
```python
if session.state == "ATTACHED":
    matched_bootloader: str | None = None
    rx_tail_evidence: str | None = None
    if session.profile.platform == "passthrough":
        classification = "PASSTHROUGH"
        recommended_action = "console_attach"
    elif session.last_error == "LOGIN_REQUIRED":
        classification = "LOGIN_REQUIRED"
        recommended_action = "console_attach"
    elif session.last_error == "REBOOTING":
        classification = "REBOOTING"
        recommended_action = "wait_or_console_attach"
    else:
        rx_tail_evidence = clean_text(bridge.rx_tail(BOOTLOADER_RX_TAIL_BYTES))
        matched_bootloader = _matches_any_bootloader_prompt(
            rx_tail_evidence,
            session.profile.bootloader_prompts,
        )
        if matched_bootloader is not None:
            classification = "BOOTLOADER"
            recommended_action = "recover_interactive"
        else:
            classification = "ATTACHED_NOT_READY"
            recommended_action = "console_attach"
    payload = {
        "ok": True,
        "classification": classification,
        "session": session.to_public_dict(),
        "attached_real_path": attached_real_path,
        "current_real_path": device.real_path,
        "attached_vtty": snapshot.get("vtty"),
        "bridge_generation": session.bridge_generation,
        "recommended_action": recommended_action,
        **lease_context,
    }
    if matched_bootloader is not None:
        payload["matched_prompt"] = matched_bootloader
        payload["rx_tail"] = rx_tail_evidence
    return payload
```

(Adjust to whatever the actual existing `return` dict structure is; add `matched_prompt` and `rx_tail` only when classification is BOOTLOADER. Verify the lookup chain: `from sw_core.constants import BOOTLOADER_RX_TAIL_BYTES` must be added to imports if not already present.)

- [ ] **Step 4: Run the tests**

Run: `python3 -m pytest tests/test_bootloader_recovery.py::TestSelfTestBootloaderClassification -v`
Expected: 3 PASS.

- [ ] **Step 5: Run the full self_test test file to confirm no regression**

Run: `python3 -m pytest tests/test_session_bind.py -q -k self_test`
Expected: all green (existing self_test tests unaffected because they don't set bootloader_prompts).

- [ ] **Step 6: Update CHANGELOG**

Add to `### Added`:
```markdown
- **`session.self_test` `BOOTLOADER` classification**：在 `state == "ATTACHED"` 路徑下，若 RX tail 末行匹配 `bootloader_prompts` 任一條，回傳 `classification: "BOOTLOADER"`、`recommended_action: "recover_interactive"`、附 `matched_prompt` 與 `rx_tail`。其他 ATTACHED 子分類（PASSTHROUGH / LOGIN_REQUIRED / REBOOTING）優先序保持不變。
```

- [ ] **Step 7: Commit**

Run:
```bash
git add sw_core/session_manager.py tests/test_bootloader_recovery.py CHANGELOG.md
git commit -m "$(cat <<'EOF'
feat(self_test): classify BOOTLOADER when RX tail matches bootloader_prompts

self_test in ATTACHED state now scans the last non-empty line of
bridge.rx_tail(BOOTLOADER_RX_TAIL_BYTES) against profile.bootloader_prompts
and returns classification=BOOTLOADER + recommended_action=recover_interactive
+ matched_prompt + rx_tail evidence. Other ATTACHED substates
(PASSTHROUGH / LOGIN_REQUIRED / REBOOTING) keep priority; BOOTLOADER is
checked only after they all miss and before falling into ATTACHED_NOT_READY.

Closes part of #44 (detection layer).

Co-Authored-By: Claude Opus 4.7 <noreply@anthropic.com>
EOF
)"
```

### Task B5: `InteractiveLease` and `SessionRuntime` schema

**Files:**
- Modify: `sw_core/session_manager.py` (`InteractiveLease`, `SessionRuntime`)
- Test: `tests/test_bootloader_recovery.py`

- [ ] **Step 1: Write failing schema tests**

Append to `tests/test_bootloader_recovery.py`:
```python
class TestInteractiveLeaseSchema(unittest.TestCase):
    def test_lease_has_recovery_mode_default_false(self):
        from sw_core.session_manager import InteractiveLease
        lease = InteractiveLease(
            interactive_id="i1",
            session_id="s1",
            owner="agent",
            created_at="now",
            timeout_s=10.0,
        )
        self.assertFalse(lease.recovery_mode)

    def test_lease_has_suspended_human_default_false(self):
        from sw_core.session_manager import InteractiveLease
        lease = InteractiveLease(
            interactive_id="i1",
            session_id="s1",
            owner="agent",
            created_at="now",
            timeout_s=10.0,
        )
        self.assertFalse(lease.suspended_human)

    def test_session_runtime_stashed_human_lease_default_none(self):
        from sw_core.session_manager import SessionRuntime
        from sw_core.config import SessionProfile
        profile = SessionProfile(
            profile_name="p", com="COM0", act_no=1, alias="lab+1",
            device_by_id="/dev/serial/by-id/orig", platform="bcm",
        )
        sr = SessionRuntime(session_id="s1", profile=profile)
        self.assertIsNone(sr._stashed_human_lease)
```

- [ ] **Step 2: Run to confirm failure**

Run: `python3 -m pytest tests/test_bootloader_recovery.py::TestInteractiveLeaseSchema -v`
Expected: 3 FAIL on missing attributes.

- [ ] **Step 3: Add fields to `InteractiveLease`**

In `sw_core/session_manager.py` (around line 64-72), modify:
```python
@dataclasses.dataclass
class InteractiveLease:
    interactive_id: str
    session_id: str
    owner: str
    created_at: str
    timeout_s: float
    last_activity_at: float = dataclasses.field(default_factory=time.monotonic)
    status: str = "active"
    recovery_mode: bool = False
    suspended_human: bool = False

    def touch(self) -> None:
        self.last_activity_at = time.monotonic()

    def expired(self) -> bool:
        return time.monotonic() - self.last_activity_at > self.timeout_s
```

- [ ] **Step 4: Add `_stashed_human_lease` to `SessionRuntime`**

In `sw_core/session_manager.py` (around line 110-115), append a field:
```python
    retained_human_owner: str | None = None
    retained_human_timeout_s: float | None = None
    fg_cmd_started_mono: float | None = None
    fg_cmd_expected_duration_s: float | None = None
    _stashed_human_lease: "InteractiveLease | None" = None  # bootloader recovery (issue #44)
    # ... rest of existing fields
```

(Forward reference because `InteractiveLease` is defined above; no quotes needed if the order is fine — `InteractiveLease` is defined before `SessionRuntime` in this file.)

- [ ] **Step 5: Run schema tests**

Run: `python3 -m pytest tests/test_bootloader_recovery.py::TestInteractiveLeaseSchema -v`
Expected: 3 PASS.

- [ ] **Step 6: Run full test suite to detect any regression**

Run: `python3 -m pytest -q tests/`
Expected: same baseline as Task A1; new tests in `test_bootloader_recovery.py` all pass.

- [ ] **Step 7: Commit**

Run:
```bash
git add sw_core/session_manager.py tests/test_bootloader_recovery.py
git commit -m "$(cat <<'EOF'
feat(session): add recovery_mode/suspended_human/stashed_human_lease fields

Schema-only change adding three opt-in fields:
  - InteractiveLease.recovery_mode (bool, default False)
  - InteractiveLease.suspended_human (bool, default False)
  - SessionRuntime._stashed_human_lease (InteractiveLease | None, default None)

These wire up the stash-and-restore mechanism for bootloader recovery; no
behavior change in this commit.

Co-Authored-By: Claude Opus 4.7 <noreply@anthropic.com>
EOF
)"
```

### Task B6: `_lease_context` exposes `recovery_mode`

**Files:**
- Modify: `sw_core/session_manager.py` (`_lease_context` at line 1089)
- Test: extend `tests/test_bootloader_recovery.py`

- [ ] **Step 1: Write the failing test**

Append:
```python
class TestLeaseContext(unittest.TestCase):
    def test_lease_context_includes_recovery_mode_true(self):
        import tempfile
        from sw_core.wal import WalWriter
        from sw_core.session_manager import InteractiveLease, SessionManager
        with tempfile.TemporaryDirectory() as tmp:
            mgr = SessionManager(
                [_make_profile_with_bootloader()],
                WalWriter(wal_dir=tmp),
                on_ready=lambda _: None, on_detached=lambda _: None,
            )
            lease = InteractiveLease(
                interactive_id="i1", session_id="s1", owner="agent",
                created_at="now", timeout_s=10.0, recovery_mode=True,
            )
            ctx = mgr._lease_context(lease)
            self.assertEqual(ctx["recovery_mode"], True)
            self.assertEqual(ctx["interactive_owner"], "agent")
            self.assertEqual(ctx["human_attached"], False)

    def test_lease_context_recovery_mode_false_for_normal_lease(self):
        import tempfile
        from sw_core.wal import WalWriter
        from sw_core.session_manager import InteractiveLease, SessionManager
        with tempfile.TemporaryDirectory() as tmp:
            mgr = SessionManager(
                [_make_profile_with_bootloader()],
                WalWriter(wal_dir=tmp),
                on_ready=lambda _: None, on_detached=lambda _: None,
            )
            lease = InteractiveLease(
                interactive_id="i1", session_id="s1", owner="human:abc",
                created_at="now", timeout_s=10.0,
            )
            ctx = mgr._lease_context(lease)
            self.assertEqual(ctx["recovery_mode"], False)
            self.assertEqual(ctx["human_attached"], True)

    def test_lease_context_no_lease(self):
        import tempfile
        from sw_core.wal import WalWriter
        from sw_core.session_manager import SessionManager
        with tempfile.TemporaryDirectory() as tmp:
            mgr = SessionManager(
                [_make_profile_with_bootloader()],
                WalWriter(wal_dir=tmp),
                on_ready=lambda _: None, on_detached=lambda _: None,
            )
            ctx = mgr._lease_context(None)
            self.assertIsNone(ctx["interactive_owner"])
            self.assertEqual(ctx["human_attached"], False)
            self.assertEqual(ctx["recovery_mode"], False)
```

- [ ] **Step 2: Confirm failure**

Run: `python3 -m pytest tests/test_bootloader_recovery.py::TestLeaseContext -v`
Expected: 3 FAIL (missing `recovery_mode` key).

- [ ] **Step 3: Modify `_lease_context`**

In `sw_core/session_manager.py` around line 1089:
```python
    def _lease_context(self, lease: InteractiveLease | None) -> dict[str, Any]:
        interactive_owner = lease.owner if lease is not None else None
        return {
            "interactive_owner": interactive_owner,
            "human_attached": bool(interactive_owner and interactive_owner.startswith("human:")),
            "recovery_mode": bool(lease is not None and lease.recovery_mode),
        }
```

- [ ] **Step 4: Run the lease_context tests**

Run: `python3 -m pytest tests/test_bootloader_recovery.py::TestLeaseContext -v`
Expected: 3 PASS.

- [ ] **Step 5: Run all self_test tests to confirm no regression**

Run: `python3 -m pytest tests/test_session_bind.py -q -k self_test`
Expected: green (existing tests don't reference recovery_mode but it'll be present in their dicts harmlessly).

- [ ] **Step 6: Commit**

Run:
```bash
git add sw_core/session_manager.py tests/test_bootloader_recovery.py
git commit -m "$(cat <<'EOF'
feat(session): expose recovery_mode in _lease_context

self_test result, interactive_status, and any caller using _lease_context
now sees a recovery_mode boolean alongside interactive_owner / human_attached.
True iff the active lease has recovery_mode set; False otherwise (including
no-lease).

Co-Authored-By: Claude Opus 4.7 <noreply@anthropic.com>
EOF
)"
```

### Task B7: `interactive_open` rejects ATTACHED with `allow_attached=False`

**Files:**
- Modify: `sw_core/session_manager.py` (`interactive_open` signature at line 1569)
- Test: `tests/test_bootloader_recovery.py`

- [ ] **Step 1: Write failing test**

Append:
```python
class TestInteractiveOpenAllowAttachedFalse(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()

    def tearDown(self):
        self._tmp.cleanup()

    def test_attached_state_rejected_when_allow_attached_false(self):
        from sw_core.wal import WalWriter
        mgr = SessionManager(
            [_make_profile_with_bootloader()],
            WalWriter(wal_dir=self._tmp.name),
            on_ready=lambda _: None, on_detached=lambda _: None,
        )
        session = mgr.get_session("COM0")
        assert session is not None
        bridge = mock.MagicMock()
        bridge.snapshot.return_value = {"running": True, "serial_alive": True, "vtty_alive": True, "vtty": "/dev/pts/9", "interactive_owner": None}
        bridge.rx_tail.return_value = "=> "
        session.bridge = bridge
        session.state = "ATTACHED"

        # Default: allow_attached unset
        resp = mgr.interactive_open("COM0", owner="agent", timeout_s=30.0)
        self.assertFalse(resp["ok"])
        self.assertEqual(resp["error_code"], "SESSION_NOT_READY")

        # Explicit False
        resp = mgr.interactive_open("COM0", owner="agent", timeout_s=30.0, allow_attached=False)
        self.assertFalse(resp["ok"])
        self.assertEqual(resp["error_code"], "SESSION_NOT_READY")
```

- [ ] **Step 2: Confirm failure**

Run: `python3 -m pytest tests/test_bootloader_recovery.py::TestInteractiveOpenAllowAttachedFalse -v`
Expected: FAIL with `TypeError: interactive_open() got an unexpected keyword argument 'allow_attached'`.

- [ ] **Step 3: Add the `allow_attached` parameter (no behavior yet)**

In `sw_core/session_manager.py` line 1569:
```python
def interactive_open(
    self,
    selector: str,
    *,
    owner: str = "agent",
    timeout_s: float = 60.0,
    command: str = "",
    allow_attached: bool = False,
) -> dict[str, Any]:
    with self._lock:
        session = self.get_session(selector)
        if session is None or session.bridge is None:
            return {"ok": False, "error_code": "SESSION_NOT_READY", "selector": selector}
        if session.state != "READY" and not allow_attached:
            return {"ok": False, "error_code": "SESSION_NOT_READY", "selector": selector}
        # ... rest unchanged for now (READY-only path); ATTACHED path added in B8
        if session.state != "READY":
            # allow_attached=True with ATTACHED — bootloader gate added next task
            return {"ok": False, "error_code": "SESSION_NOT_READY", "selector": selector, "error_detail": "NOT_BOOTLOADER"}
        # existing READY-path body follows (lease busy check, _open_interactive_locked, etc.)
        # ...
```

(Carefully preserve existing READY-path behavior — only refactor the gate, don't change anything else.)

- [ ] **Step 4: Run tests**

Run: `python3 -m pytest tests/test_bootloader_recovery.py::TestInteractiveOpenAllowAttachedFalse -v`
Expected: PASS.

- [ ] **Step 5: Run interactive_open existing tests**

Run: `python3 -m pytest tests/test_session_bind.py -q -k interactive_open`
Expected: PASS (READY path unchanged).

- [ ] **Step 6: Commit**

```bash
git add sw_core/session_manager.py tests/test_bootloader_recovery.py
git commit -m "feat(session): add allow_attached param to interactive_open (gate only)

Co-Authored-By: Claude Opus 4.7 <noreply@anthropic.com>"
```

### Task B8: `interactive_open` ATTACHED gate — reject when no bootloader match

**Files:**
- Modify: `sw_core/session_manager.py` (`interactive_open`)
- Test: `tests/test_bootloader_recovery.py`

- [ ] **Step 1: Write failing test**

Append:
```python
class TestInteractiveOpenAttachedNoMatch(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
    def tearDown(self):
        self._tmp.cleanup()

    def test_allow_attached_rejects_when_no_bootloader_match(self):
        from sw_core.wal import WalWriter
        mgr = SessionManager(
            [_make_profile_with_bootloader(bootloader_prompts=("^=> $",))],
            WalWriter(wal_dir=self._tmp.name),
            on_ready=lambda _: None, on_detached=lambda _: None,
        )
        session = mgr.get_session("COM0")
        bridge = mock.MagicMock()
        bridge.snapshot.return_value = {"running": True, "serial_alive": True, "vtty_alive": True, "vtty": "/dev/pts/9", "interactive_owner": None}
        bridge.rx_tail.return_value = "root@prplOS:~# "  # OS prompt, no bootloader
        session.bridge = bridge
        session.state = "ATTACHED"

        resp = mgr.interactive_open("COM0", owner="agent", timeout_s=30.0, allow_attached=True)
        self.assertFalse(resp["ok"])
        self.assertEqual(resp["error_code"], "SESSION_NOT_READY")
        self.assertEqual(resp["error_detail"], "NOT_BOOTLOADER")

    def test_allow_attached_rejects_when_bridge_unhealthy(self):
        from sw_core.wal import WalWriter
        mgr = SessionManager(
            [_make_profile_with_bootloader(bootloader_prompts=("^=> $",))],
            WalWriter(wal_dir=self._tmp.name),
            on_ready=lambda _: None, on_detached=lambda _: None,
        )
        session = mgr.get_session("COM0")
        bridge = mock.MagicMock()
        bridge.snapshot.return_value = {"running": False, "serial_alive": True, "vtty_alive": True, "vtty": "/dev/pts/9", "interactive_owner": None}
        bridge.rx_tail.return_value = "=> "
        session.bridge = bridge
        session.state = "ATTACHED"

        resp = mgr.interactive_open("COM0", owner="agent", timeout_s=30.0, allow_attached=True)
        self.assertFalse(resp["ok"])
        self.assertEqual(resp["error_code"], "SESSION_NOT_READY")
```

- [ ] **Step 2: Confirm failure**

Run: `python3 -m pytest tests/test_bootloader_recovery.py::TestInteractiveOpenAttachedNoMatch -v`
Expected: depends on B7 stub — if B7 stub returns NOT_BOOTLOADER unconditionally, the no-match test passes but the unhealthy-bridge test may fail. Adjust expectations.

- [ ] **Step 3: Implement the proper ATTACHED gate**

In `sw_core/session_manager.py` `interactive_open`, replace the B7 stub:
```python
def interactive_open(
    self,
    selector: str,
    *,
    owner: str = "agent",
    timeout_s: float = 60.0,
    command: str = "",
    allow_attached: bool = False,
) -> dict[str, Any]:
    with self._lock:
        session = self.get_session(selector)
        if session is None or session.bridge is None:
            return {"ok": False, "error_code": "SESSION_NOT_READY", "selector": selector}

        bridge = session.bridge
        is_recovery_path = False

        if session.state == "READY":
            pass  # existing READY logic below
        elif session.state == "ATTACHED" and allow_attached:
            snapshot = bridge.snapshot()
            if not (snapshot.get("running") and snapshot.get("serial_alive") and snapshot.get("vtty_alive")):
                return {"ok": False, "error_code": "SESSION_NOT_READY", "selector": selector}
            rx_tail_evidence = clean_text(bridge.rx_tail(BOOTLOADER_RX_TAIL_BYTES))
            matched = _matches_any_bootloader_prompt(rx_tail_evidence, session.profile.bootloader_prompts)
            if matched is None:
                return {
                    "ok": False, "error_code": "SESSION_NOT_READY",
                    "selector": selector, "error_detail": "NOT_BOOTLOADER",
                }
            is_recovery_path = True
        else:
            return {"ok": False, "error_code": "SESSION_NOT_READY", "selector": selector}

        # Lease busy / open / set_interactive_owner — extended in B9 for stash
        if not is_recovery_path:
            if self._refresh_interactive_locked(session) is not None:
                return {"ok": False, "error_code": "SESSION_INTERACTIVE_BUSY", "interactive_session_id": session.interactive_session_id}
            lease = self._open_interactive_locked(session, owner=owner, timeout_s=timeout_s)
        else:
            # placeholder until B9: reject for now if any existing lease
            if self._refresh_interactive_locked(session) is not None:
                return {"ok": False, "error_code": "SESSION_INTERACTIVE_BUSY", "interactive_session_id": session.interactive_session_id}
            clamped_timeout = min(timeout_s, MAX_RECOVERY_LEASE_S)
            lease = self._open_interactive_locked(session, owner=owner, timeout_s=clamped_timeout)
            lease.recovery_mode = True

    if command:
        self._mark_session_tx(session)
        bridge.send_command(command, source=owner, cmd_id=None)
    return {
        "ok": True,
        "interactive_id": lease.interactive_id,
        "session": session.to_public_dict(),
        "recovery_mode": lease.recovery_mode,
    }
```

(Add imports if needed: `from sw_core.constants import MAX_RECOVERY_LEASE_S, BOOTLOADER_RX_TAIL_BYTES`.)

- [ ] **Step 4: Run B8 tests**

Run: `python3 -m pytest tests/test_bootloader_recovery.py::TestInteractiveOpenAttachedNoMatch -v`
Expected: 2 PASS.

- [ ] **Step 5: Run all interactive_open tests**

Run: `python3 -m pytest tests/test_bootloader_recovery.py tests/test_session_bind.py -q -k interactive_open`
Expected: green.

- [ ] **Step 6: Commit**

```bash
git add sw_core/session_manager.py tests/test_bootloader_recovery.py
git commit -m "feat(session): allow_attached gate validates bridge + bootloader prompt

Co-Authored-By: Claude Opus 4.7 <noreply@anthropic.com>"
```

### Task B9: `interactive_open` recovery — open without human lease

**Files:**
- Modify: `sw_core/session_manager.py` (`interactive_open` recovery branch)
- Test: `tests/test_bootloader_recovery.py`

- [ ] **Step 1: Write failing test**

Append:
```python
class TestInteractiveOpenRecoveryNoHuman(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
    def tearDown(self):
        self._tmp.cleanup()

    def test_recovery_lease_opens_when_no_existing_lease(self):
        from sw_core.wal import WalWriter
        mgr = SessionManager(
            [_make_profile_with_bootloader(bootloader_prompts=("^=> $",))],
            WalWriter(wal_dir=self._tmp.name),
            on_ready=lambda _: None, on_detached=lambda _: None,
        )
        session = mgr.get_session("COM0")
        bridge = mock.MagicMock()
        bridge.snapshot.return_value = {"running": True, "serial_alive": True, "vtty_alive": True, "vtty": "/dev/pts/9", "interactive_owner": None}
        bridge.rx_tail.return_value = "=> "
        session.bridge = bridge
        session.state = "ATTACHED"

        resp = mgr.interactive_open("COM0", owner="agent", timeout_s=30.0, allow_attached=True)
        self.assertTrue(resp["ok"])
        self.assertTrue(resp["recovery_mode"])
        # Verify suspend_interactive NOT called (no human lease)
        bridge.suspend_interactive.assert_not_called()
        # Verify lease state
        lease = mgr._interactive[resp["interactive_id"]]
        self.assertTrue(lease.recovery_mode)
        self.assertFalse(lease.suspended_human)
        self.assertIsNone(session._stashed_human_lease)
```

- [ ] **Step 2: Run; confirm passing already**

Run: `python3 -m pytest tests/test_bootloader_recovery.py::TestInteractiveOpenRecoveryNoHuman -v`
Expected: probably PASS already (B8 covers this case). If not, the assertion details may need adjustment.

If this test passes without changes, that's correct behavior — recovery without human is identical to a normal agent lease + recovery_mode flag. Move to Step 3 directly.

- [ ] **Step 3: Commit (test-only addition, sealing the contract)**

```bash
git add tests/test_bootloader_recovery.py
git commit -m "test(session): cover recovery lease open without existing human lease

Co-Authored-By: Claude Opus 4.7 <noreply@anthropic.com>"
```

### Task B10: `interactive_open` recovery — stash existing human lease

**Files:**
- Modify: `sw_core/session_manager.py` (`interactive_open` recovery branch — replace placeholder with stash logic)
- Test: `tests/test_bootloader_recovery.py`

- [ ] **Step 1: Write failing test**

Append:
```python
class TestInteractiveOpenRecoveryStashHuman(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
    def tearDown(self):
        self._tmp.cleanup()

    def test_recovery_open_stashes_existing_human_lease(self):
        from sw_core.wal import WalWriter
        mgr = SessionManager(
            [_make_profile_with_bootloader(bootloader_prompts=("^=> $",))],
            WalWriter(wal_dir=self._tmp.name),
            on_ready=lambda _: None, on_detached=lambda _: None,
        )
        session = mgr.get_session("COM0")
        bridge = mock.MagicMock()
        bridge.snapshot.return_value = {"running": True, "serial_alive": True, "vtty_alive": True, "vtty": "/dev/pts/9", "interactive_owner": "human:abc"}
        bridge.rx_tail.return_value = "=> "
        bridge.console_has_external_peer.return_value = True
        session.bridge = bridge
        session.state = "ATTACHED"

        # Pre-existing human lease (simulating console-attach having opened it)
        human_lease = InteractiveLease(
            interactive_id="lease-human-1", session_id=session.session_id,
            owner="human:abc", created_at="now", timeout_s=300.0,
        )
        with mgr._lock:
            mgr._interactive[human_lease.interactive_id] = human_lease
            session.interactive_session_id = human_lease.interactive_id

        resp = mgr.interactive_open("COM0", owner="agent", timeout_s=30.0, allow_attached=True)

        self.assertTrue(resp["ok"])
        self.assertTrue(resp["recovery_mode"])
        bridge.suspend_interactive.assert_called_once()

        # Human lease moved to stash
        self.assertNotIn("lease-human-1", mgr._interactive)
        self.assertIs(session._stashed_human_lease, human_lease)
        # Recovery lease is now active
        recovery_id = resp["interactive_id"]
        self.assertIn(recovery_id, mgr._interactive)
        recovery = mgr._interactive[recovery_id]
        self.assertTrue(recovery.recovery_mode)
        self.assertTrue(recovery.suspended_human)
        self.assertEqual(session.interactive_session_id, recovery_id)

    def test_recovery_open_rejects_when_existing_lease_is_agent(self):
        from sw_core.wal import WalWriter
        mgr = SessionManager(
            [_make_profile_with_bootloader(bootloader_prompts=("^=> $",))],
            WalWriter(wal_dir=self._tmp.name),
            on_ready=lambda _: None, on_detached=lambda _: None,
        )
        session = mgr.get_session("COM0")
        bridge = mock.MagicMock()
        bridge.snapshot.return_value = {"running": True, "serial_alive": True, "vtty_alive": True, "vtty": "/dev/pts/9", "interactive_owner": "agent"}
        bridge.rx_tail.return_value = "=> "
        bridge.console_has_external_peer.return_value = True
        session.bridge = bridge
        session.state = "ATTACHED"

        agent_lease = InteractiveLease(
            interactive_id="lease-agent-1", session_id=session.session_id,
            owner="agent", created_at="now", timeout_s=60.0,
        )
        with mgr._lock:
            mgr._interactive[agent_lease.interactive_id] = agent_lease
            session.interactive_session_id = agent_lease.interactive_id

        resp = mgr.interactive_open("COM0", owner="agent", timeout_s=30.0, allow_attached=True)

        self.assertFalse(resp["ok"])
        self.assertEqual(resp["error_code"], "SESSION_INTERACTIVE_BUSY")
        # Stash NOT taken
        self.assertIsNone(session._stashed_human_lease)
        # Original agent lease still in place
        self.assertIn("lease-agent-1", mgr._interactive)
        bridge.suspend_interactive.assert_not_called()
```

- [ ] **Step 2: Confirm failure**

Run: `python3 -m pytest tests/test_bootloader_recovery.py::TestInteractiveOpenRecoveryStashHuman -v`
Expected: 2 FAIL — `test_recovery_open_stashes_existing_human_lease` because B8's placeholder rejects with SESSION_INTERACTIVE_BUSY when human lease exists.

- [ ] **Step 3: Replace recovery placeholder with stash logic**

In `sw_core/session_manager.py` `interactive_open` recovery branch (the `is_recovery_path` block from B8), replace the placeholder body:
```python
        # Lease busy / open / set_interactive_owner
        if not is_recovery_path:
            if self._refresh_interactive_locked(session) is not None:
                return {"ok": False, "error_code": "SESSION_INTERACTIVE_BUSY", "interactive_session_id": session.interactive_session_id}
            lease = self._open_interactive_locked(session, owner=owner, timeout_s=timeout_s)
        else:
            existing = self._refresh_interactive_locked(session)
            if existing is not None:
                if not existing.owner.startswith("human:"):
                    return {"ok": False, "error_code": "SESSION_INTERACTIVE_BUSY", "interactive_session_id": existing.interactive_id}
                # Stash the human lease; bridge-layer suspend
                self._interactive.pop(existing.interactive_id, None)
                session.interactive_session_id = None
                session._stashed_human_lease = existing
                bridge.suspend_interactive()
                suspended_human = True
            else:
                suspended_human = False

            clamped_timeout = min(timeout_s, MAX_RECOVERY_LEASE_S)
            lease = self._open_interactive_locked(session, owner=owner, timeout_s=clamped_timeout)
            lease.recovery_mode = True
            lease.suspended_human = suspended_human
```

- [ ] **Step 4: Run the stash tests**

Run: `python3 -m pytest tests/test_bootloader_recovery.py::TestInteractiveOpenRecoveryStashHuman -v`
Expected: 2 PASS.

- [ ] **Step 5: Run all interactive tests**

Run: `python3 -m pytest tests/test_bootloader_recovery.py tests/test_session_bind.py -q -k interactive`
Expected: green.

- [ ] **Step 6: Commit**

```bash
git add sw_core/session_manager.py tests/test_bootloader_recovery.py
git commit -m "feat(session): stash human lease and suspend bridge on recovery open

Co-Authored-By: Claude Opus 4.7 <noreply@anthropic.com>"
```

### Task B11: `interactive_close` — restore stashed human lease

**Files:**
- Modify: `sw_core/session_manager.py` (`_close_interactive_locked` at line 1044)
- Test: `tests/test_bootloader_recovery.py`

- [ ] **Step 1: Write failing tests covering the four close-path scenarios**

Append:
```python
class TestInteractiveCloseRecoveryRestore(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
    def tearDown(self):
        self._tmp.cleanup()

    def _setup_recovery_with_human_stashed(self, *, peer_alive=True):
        from sw_core.wal import WalWriter
        mgr = SessionManager(
            [_make_profile_with_bootloader(bootloader_prompts=("^=> $",))],
            WalWriter(wal_dir=self._tmp.name),
            on_ready=lambda _: None, on_detached=lambda _: None,
        )
        session = mgr.get_session("COM0")
        bridge = mock.MagicMock()
        bridge.snapshot.return_value = {"running": True, "serial_alive": True, "vtty_alive": True, "vtty": "/dev/pts/9", "interactive_owner": "human:abc"}
        bridge.rx_tail.return_value = "=> "
        bridge.console_has_external_peer.return_value = peer_alive
        session.bridge = bridge
        session.state = "ATTACHED"

        human_lease = InteractiveLease(
            interactive_id="lease-human-1", session_id=session.session_id,
            owner="human:abc", created_at="now", timeout_s=300.0,
        )
        with mgr._lock:
            mgr._interactive[human_lease.interactive_id] = human_lease
            session.interactive_session_id = human_lease.interactive_id

        open_resp = mgr.interactive_open("COM0", owner="agent", timeout_s=30.0, allow_attached=True)
        return mgr, session, bridge, human_lease, open_resp["interactive_id"]

    def test_close_restores_stashed_human_when_alive(self):
        mgr, session, bridge, human_lease, recovery_id = self._setup_recovery_with_human_stashed()
        close_resp = mgr.interactive_close(recovery_id)
        self.assertTrue(close_resp["ok"])
        bridge.resume_interactive.assert_called_once()
        # Human lease restored
        self.assertIn("lease-human-1", mgr._interactive)
        self.assertEqual(session.interactive_session_id, "lease-human-1")
        self.assertIsNone(session._stashed_human_lease)
        # Recovery lease gone
        self.assertNotIn(recovery_id, mgr._interactive)

    def test_close_discards_stash_when_human_detached(self):
        mgr, session, bridge, human_lease, recovery_id = self._setup_recovery_with_human_stashed(peer_alive=False)
        close_resp = mgr.interactive_close(recovery_id)
        self.assertTrue(close_resp["ok"])
        bridge.resume_interactive.assert_called_once()
        # Stash dropped
        self.assertNotIn("lease-human-1", mgr._interactive)
        self.assertIsNone(session._stashed_human_lease)
        self.assertIsNone(session.interactive_session_id)

    def test_close_discards_expired_stash(self):
        mgr, session, bridge, human_lease, recovery_id = self._setup_recovery_with_human_stashed()
        # Force expire by rolling last_activity_at backwards
        human_lease.last_activity_at = time.monotonic() - 99999
        close_resp = mgr.interactive_close(recovery_id)
        self.assertTrue(close_resp["ok"])
        bridge.resume_interactive.assert_called_once()
        self.assertNotIn("lease-human-1", mgr._interactive)
        self.assertIsNone(session._stashed_human_lease)
```

(Add `import time` at top of `tests/test_bootloader_recovery.py` if not present.)

- [ ] **Step 2: Confirm failure**

Run: `python3 -m pytest tests/test_bootloader_recovery.py::TestInteractiveCloseRecoveryRestore -v`
Expected: 3 FAIL — `_close_interactive_locked` doesn't restore stash yet.

- [ ] **Step 3: Modify `_close_interactive_locked`**

In `sw_core/session_manager.py` around line 1044:
```python
    def _close_interactive_locked(
        self,
        session: SessionRuntime,
        *,
        interactive_id: str | None = None,
        expected_owner: str | None = None,
    ) -> InteractiveLease | None:
        lease_id = interactive_id or session.interactive_session_id
        if lease_id is None:
            return None
        lease = self._interactive.get(lease_id)
        if lease is not None and expected_owner is not None and lease.owner != expected_owner:
            return None
        if lease is not None:
            lease.status = "closed"
            self._interactive.pop(lease_id, None)
        session.interactive_session_id = None
        if session.bridge is not None:
            session.bridge.set_interactive_owner(None)

        # Recovery lease close: resume bridge + restore stashed human (issue #44)
        if lease is not None and lease.suspended_human and session.bridge is not None:
            session.bridge.resume_interactive()
            stashed = session._stashed_human_lease
            if stashed is not None:
                if not stashed.expired():
                    client_id = stashed.owner.split(":", 1)[1] if ":" in stashed.owner else ""
                    if client_id and session.bridge.console_has_external_peer(client_id):
                        self._interactive[stashed.interactive_id] = stashed
                        session.interactive_session_id = stashed.interactive_id
                        session.bridge.set_interactive_owner(stashed.owner)
                session._stashed_human_lease = None

        return lease
```

- [ ] **Step 4: Run tests**

Run: `python3 -m pytest tests/test_bootloader_recovery.py::TestInteractiveCloseRecoveryRestore -v`
Expected: 3 PASS.

- [ ] **Step 5: Full regression**

Run: `python3 -m pytest -q tests/`
Expected: same green/known-failure baseline as Task A1.

- [ ] **Step 6: Commit**

```bash
git add sw_core/session_manager.py tests/test_bootloader_recovery.py
git commit -m "feat(session): restore stashed human lease on recovery close

Co-Authored-By: Claude Opus 4.7 <noreply@anthropic.com>"
```

### Task B12: Recovery lease auto-expire path

**Files:**
- Modify: `sw_core/session_manager.py` (`interactive_send`, `interactive_status` expired branches at lines 1613, ~1635)
- Test: `tests/test_bootloader_recovery.py`

- [ ] **Step 1: Write failing test**

Append:
```python
class TestRecoveryLeaseExpire(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
    def tearDown(self):
        self._tmp.cleanup()

    def test_interactive_send_on_expired_recovery_lease_resumes_human(self):
        from sw_core.wal import WalWriter
        mgr = SessionManager(
            [_make_profile_with_bootloader(bootloader_prompts=("^=> $",))],
            WalWriter(wal_dir=self._tmp.name),
            on_ready=lambda _: None, on_detached=lambda _: None,
        )
        session = mgr.get_session("COM0")
        bridge = mock.MagicMock()
        bridge.snapshot.return_value = {"running": True, "serial_alive": True, "vtty_alive": True, "vtty": "/dev/pts/9", "interactive_owner": "human:abc"}
        bridge.rx_tail.return_value = "=> "
        bridge.console_has_external_peer.return_value = True
        session.bridge = bridge
        session.state = "ATTACHED"

        human_lease = InteractiveLease(
            interactive_id="lease-human-1", session_id=session.session_id,
            owner="human:abc", created_at="now", timeout_s=300.0,
        )
        with mgr._lock:
            mgr._interactive[human_lease.interactive_id] = human_lease
            session.interactive_session_id = human_lease.interactive_id

        open_resp = mgr.interactive_open("COM0", owner="agent", timeout_s=10.0, allow_attached=True)
        recovery_id = open_resp["interactive_id"]

        # Force recovery lease to expire
        recovery = mgr._interactive[recovery_id]
        recovery.last_activity_at = time.monotonic() - 99999

        send_resp = mgr.interactive_send(recovery_id, data="reset\n")
        self.assertFalse(send_resp["ok"])
        self.assertEqual(send_resp["error_code"], "INTERACTIVE_EXPIRED")
        bridge.resume_interactive.assert_called_once()
        # Human lease should be restored
        self.assertIn("lease-human-1", mgr._interactive)
        self.assertIsNone(session._stashed_human_lease)
```

- [ ] **Step 2: Confirm failure**

Run: `python3 -m pytest tests/test_bootloader_recovery.py::TestRecoveryLeaseExpire -v`
Expected: FAIL — current `interactive_send` expired path closes the lease but doesn't trigger our recovery restore (it does call `_close_interactive_locked` but suspended_human path is on `_close_interactive_locked` itself, so this might already pass — verify).

- [ ] **Step 3: If failing, ensure expired path goes through `_close_interactive_locked`**

Inspect `interactive_send` (line 1608) and `interactive_status` (line 1628) expired branches. They should call `self._close_interactive_locked(session, interactive_id=interactive_id)`. If they already do (they do — line 1617), no change needed; just confirm behavior.

If a code change is needed, modify the expired branches to call `_close_interactive_locked` consistently.

- [ ] **Step 4: Run test**

Run: `python3 -m pytest tests/test_bootloader_recovery.py::TestRecoveryLeaseExpire -v`
Expected: PASS.

- [ ] **Step 5: Commit (test-only if no code changed; otherwise both)**

```bash
git add tests/test_bootloader_recovery.py
git commit -m "test(session): cover recovery lease auto-expire restoring human

Co-Authored-By: Claude Opus 4.7 <noreply@anthropic.com>"
```

### Task B13: MAX_RECOVERY_LEASE_S clamp test

**Files:**
- Test: `tests/test_bootloader_recovery.py`

- [ ] **Step 1: Write the test**

Append:
```python
class TestRecoveryLeaseClamp(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
    def tearDown(self):
        self._tmp.cleanup()

    def test_recovery_clamps_caller_timeout_to_max(self):
        from sw_core.wal import WalWriter
        from sw_core.constants import MAX_RECOVERY_LEASE_S
        mgr = SessionManager(
            [_make_profile_with_bootloader(bootloader_prompts=("^=> $",))],
            WalWriter(wal_dir=self._tmp.name),
            on_ready=lambda _: None, on_detached=lambda _: None,
        )
        session = mgr.get_session("COM0")
        bridge = mock.MagicMock()
        bridge.snapshot.return_value = {"running": True, "serial_alive": True, "vtty_alive": True, "vtty": "/dev/pts/9", "interactive_owner": None}
        bridge.rx_tail.return_value = "=> "
        session.bridge = bridge
        session.state = "ATTACHED"

        resp = mgr.interactive_open("COM0", owner="agent", timeout_s=600.0, allow_attached=True)
        self.assertTrue(resp["ok"])
        lease = mgr._interactive[resp["interactive_id"]]
        self.assertEqual(lease.timeout_s, MAX_RECOVERY_LEASE_S)

    def test_recovery_does_not_clamp_below_caller_timeout(self):
        from sw_core.wal import WalWriter
        mgr = SessionManager(
            [_make_profile_with_bootloader(bootloader_prompts=("^=> $",))],
            WalWriter(wal_dir=self._tmp.name),
            on_ready=lambda _: None, on_detached=lambda _: None,
        )
        session = mgr.get_session("COM0")
        bridge = mock.MagicMock()
        bridge.snapshot.return_value = {"running": True, "serial_alive": True, "vtty_alive": True, "vtty": "/dev/pts/9", "interactive_owner": None}
        bridge.rx_tail.return_value = "=> "
        session.bridge = bridge
        session.state = "ATTACHED"

        resp = mgr.interactive_open("COM0", owner="agent", timeout_s=30.0, allow_attached=True)
        self.assertTrue(resp["ok"])
        lease = mgr._interactive[resp["interactive_id"]]
        self.assertEqual(lease.timeout_s, 30.0)

    def test_ready_path_unaffected_by_clamp(self):
        from sw_core.wal import WalWriter
        mgr = SessionManager(
            [_make_profile_with_bootloader()],
            WalWriter(wal_dir=self._tmp.name),
            on_ready=lambda _: None, on_detached=lambda _: None,
        )
        session = mgr.get_session("COM0")
        bridge = mock.MagicMock()
        bridge.snapshot.return_value = {"running": True, "serial_alive": True, "vtty_alive": True, "vtty": "/dev/pts/9", "interactive_owner": None}
        session.bridge = bridge
        session.state = "READY"

        resp = mgr.interactive_open("COM0", owner="agent", timeout_s=600.0, allow_attached=True)
        self.assertTrue(resp["ok"])
        lease = mgr._interactive[resp["interactive_id"]]
        self.assertEqual(lease.timeout_s, 600.0)  # NOT clamped — READY path
```

- [ ] **Step 2: Run; if any fails, B10 clamp logic was wrong**

Run: `python3 -m pytest tests/test_bootloader_recovery.py::TestRecoveryLeaseClamp -v`
Expected: 3 PASS (B10 already implemented `min(timeout_s, MAX_RECOVERY_LEASE_S)` only on recovery path).

- [ ] **Step 3: Commit**

```bash
git add tests/test_bootloader_recovery.py
git commit -m "test(session): verify MAX_RECOVERY_LEASE_S clamps recovery only

Co-Authored-By: Claude Opus 4.7 <noreply@anthropic.com>"
```

### Task B14: `interactive_send` during recovery (key + plain encoding)

**Files:**
- Test: `tests/test_bootloader_recovery.py`

(`interactive_send` already supports plain/key/base64 — no code change. We just verify behavior under recovery_mode.)

- [ ] **Step 1: Write the test**

Append:
```python
class TestRecoveryInteractiveSend(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
    def tearDown(self):
        self._tmp.cleanup()

    def _open_recovery(self):
        from sw_core.wal import WalWriter
        mgr = SessionManager(
            [_make_profile_with_bootloader(bootloader_prompts=("^=> $",))],
            WalWriter(wal_dir=self._tmp.name),
            on_ready=lambda _: None, on_detached=lambda _: None,
        )
        session = mgr.get_session("COM0")
        bridge = mock.MagicMock()
        bridge.snapshot.return_value = {"running": True, "serial_alive": True, "vtty_alive": True, "vtty": "/dev/pts/9", "interactive_owner": None}
        bridge.rx_tail.return_value = "=> "
        session.bridge = bridge
        session.state = "ATTACHED"
        resp = mgr.interactive_open("COM0", owner="agent", timeout_s=30.0, allow_attached=True)
        return mgr, session, bridge, resp["interactive_id"]

    def test_send_plain_writes_raw_bytes(self):
        mgr, _, bridge, recovery_id = self._open_recovery()
        send_resp = mgr.interactive_send(recovery_id, data="reset\n")
        self.assertTrue(send_resp["ok"])
        bridge.send_bytes.assert_called_with(b"reset\n", source="agent", cmd_id=None)

    def test_send_key_ctrl_c(self):
        mgr, _, bridge, recovery_id = self._open_recovery()
        send_resp = mgr.interactive_send(recovery_id, data="ctrl-c", encoding="key")
        self.assertTrue(send_resp["ok"])
        bridge.send_bytes.assert_called_with(b"\x03", source="agent", cmd_id=None)
```

- [ ] **Step 2: Run**

Run: `python3 -m pytest tests/test_bootloader_recovery.py::TestRecoveryInteractiveSend -v`
Expected: 2 PASS.

- [ ] **Step 3: Commit**

```bash
git add tests/test_bootloader_recovery.py
git commit -m "test(session): verify interactive_send works under recovery_mode

Co-Authored-By: Claude Opus 4.7 <noreply@anthropic.com>"
```

### Task B15: RPC and CLI `--allow-attached`

**Files:**
- Modify: `sw_core/service.py` (`session.interactive_open` handler at line 371)
- Modify: `sw_core/cli.py` (`session interactive-open` subparser)
- Test: extend or add to `tests/test_bootloader_recovery.py`

- [ ] **Step 1: Write failing tests**

Append:
```python
class TestRpcAllowAttached(unittest.TestCase):
    def test_service_passes_allow_attached(self):
        # Smoke test: service handler accepts allow_attached param
        # Implementation: import the service handler and call it with mock SessionManager
        from sw_core.service import RpcService
        sm = mock.MagicMock()
        sm.interactive_open.return_value = {"ok": True, "interactive_id": "x", "session": {}, "recovery_mode": True}
        svc = RpcService.__new__(RpcService)
        svc._sessions = sm
        # Exercise the handler — adapt to actual RpcService API
        result = svc.dispatch("session.interactive_open", {"selector": "COM0", "allow_attached": True, "owner": "agent"})
        self.assertTrue(result["ok"])
        sm.interactive_open.assert_called_with("COM0", owner="agent", timeout_s=60.0, command="", allow_attached=True)
```

(Adjust test to actual `service.py` entry point — inspect `service.py:300+` to find the dispatch method name.)

- [ ] **Step 2: Confirm failure**

Run: `python3 -m pytest tests/test_bootloader_recovery.py::TestRpcAllowAttached -v`
Expected: FAIL — handler doesn't pass `allow_attached`.

- [ ] **Step 3: Modify `service.py` handler**

In `sw_core/service.py` line 371-378:
```python
        if method == "session.interactive_open":
            selector = str(params.get("selector") or params.get("session_id") or params.get("com") or params.get("alias") or "")
            owner = str(params.get("owner") or "agent")
            timeout_s = float(params.get("timeout_s") or 60.0)
            command = str(params.get("command") or "")
            allow_attached = bool(params.get("allow_attached", False))
            if not selector:
                return {"ok": False, "error_code": "INVALID_ARGS"}
            return self._sessions.interactive_open(
                selector,
                owner=owner,
                timeout_s=timeout_s,
                command=command,
                allow_attached=allow_attached,
            )
```

- [ ] **Step 4: Modify CLI**

In `sw_core/cli.py`, find the `session interactive-open` subparser (search for `interactive-open`):
```python
    p_io = sess_sub.add_parser("interactive-open")
    p_io.add_argument("selector", ...)
    p_io.add_argument("--owner", default="agent")
    p_io.add_argument("--timeout", type=float, default=60.0)
    p_io.add_argument("--command", default="")
    p_io.add_argument(
        "--allow-attached",
        action="store_true",
        help="Open a recovery interactive lease in ATTACHED + bootloader state. "
             "Suspends any human lease for the duration; restores on close.",
    )
```

Then in the dispatch:
```python
    elif args.session_cmd == "interactive-open":
        params = {
            "selector": args.selector,
            "owner": args.owner,
            "timeout_s": args.timeout,
            "command": args.command,
            "allow_attached": args.allow_attached,
        }
        return _run_rpc(args, "session.interactive_open", params)
```

(Match exact existing patterns; the actual CLI layout differs — look at how other flags are wired in `cli.py`.)

- [ ] **Step 5: Run RPC test + manual CLI smoke**

Run: `python3 -m pytest tests/test_bootloader_recovery.py::TestRpcAllowAttached -v`
Expected: PASS.

Manual CLI smoke (no daemon needed for help):
```bash
./serialwrap session interactive-open --help | grep -i allow-attached
```
Expected: line describing the flag.

- [ ] **Step 6: Update CHANGELOG**

Add to `### Added`:
```markdown
- **`session.interactive_open` `allow_attached` 入參**：opt-in 放寬 READY-only gate；ATTACHED + bootloader 命中時開出 recovery lease。RPC 從 `params.get("allow_attached", False)` 讀；CLI 加 `--allow-attached` flag。
- **`session.interactive_open` recovery_mode 透出**：result 最外層含 `recovery_mode: bool`。
- **MAX_RECOVERY_LEASE_S=120s clamp**：recovery lease 強制逾時上限，避免 agent 無限期 suspend 人類觀察者。
- **stash-and-restore lease 機制**：recovery 期間 human session-layer lease 暫存於 `session._stashed_human_lease`、bridge 進入 suspend 模式；recovery close 時 resume + 還原（若 stash 仍未 expire 且 human 仍 console-attached）。
```

- [ ] **Step 7: Commit**

```bash
git add sw_core/service.py sw_core/cli.py tests/test_bootloader_recovery.py CHANGELOG.md
git commit -m "$(cat <<'EOF'
feat(session): wire allow_attached through RPC and CLI

session.interactive_open now reads allow_attached from RPC params; the
session interactive-open CLI subcommand accepts --allow-attached. Help text
explains the recovery semantics (suspends human lease, restored on close).

Closes part of #44.

Co-Authored-By: Claude Opus 4.7 <noreply@anthropic.com>
EOF
)"
```

### Task B16: Vendor profile — add `bootloader_prompts` to a real profile

**Files:**
- Modify: `profiles/default.yaml` (add bootloader_prompts to brcm-template)

- [ ] **Step 1: Edit `profiles/default.yaml`**

Use `Edit` to add `bootloader_prompts` to `brcm-template`:
```yaml
  brcm-template:
    platform: bcm
    prompt_regex: "(?m)[>#]\\s*$"
    login_regex: "(?mi)login:\\s*$"
    password_regex: "(?mi)password:\\s*$"
    post_login_cmd: "sh"
    ready_probe: "echo __READY__${nonce}"
    user_env: "BRCM_USER"
    pass_env: "BRCM_PASS"
    env_file: "brcm.env"
    timeout_s: 15
    bootloader_prompts:
      - "^CFE> $"
      - "^=> $"
      - "^BCM\\d+>> $"
    uart:
      baud: 115200
      ...
```

(Keep all existing fields; only add `bootloader_prompts` block.)

- [ ] **Step 2: Verify YAML**

Run: `python3 -c "import yaml; yaml.safe_load(open('profiles/default.yaml')); print('ok')"`
Expected: `ok`.

- [ ] **Step 3: Verify the loader picks it up**

Run:
```bash
python3 -c "
from sw_core.config import load_profiles
result = load_profiles('profiles')
tpl = result.templates_by_file['default.yaml']['brcm-template']
print('bootloader_prompts:', tpl.bootloader_prompts)
"
```
Expected: prints `bootloader_prompts: ['^CFE> $', '^=> $', '^BCM\\d+>> $']`.

- [ ] **Step 4: Commit**

```bash
git add profiles/default.yaml
git commit -m "feat(profile): add bootloader_prompts to brcm-template

Covers CFE, U-Boot, and Broadcom vendor bootloader prompts. Other templates
(prpl-template, op3-template, others-template) keep bootloader_prompts at
its default [] until a concrete need arises.

Co-Authored-By: Claude Opus 4.7 <noreply@anthropic.com>"
```

### Task B17: Documentation — `docs/serialwrap-spec.md`

**Files:**
- Modify: `/home/paul_chen/prj_pri/serialwrap/docs/serialwrap-spec.md`

- [ ] **Step 1: Find self_test section**

Run: `grep -n "self_test\|9\\.1" docs/serialwrap-spec.md | head -20`

- [ ] **Step 2: Add BOOTLOADER classification description**

In the self_test section, after the existing classification list, add:
```markdown
- `BOOTLOADER`：session 處於 `ATTACHED` 狀態、`bridge.rx_tail` 末行匹配 `profile.bootloader_prompts` 任一條 regex。`recommended_action` 為 `recover_interactive`；result 額外帶 `matched_prompt`（命中的 regex 字面值）與 `rx_tail`（最近 512 bytes 的 RX 證據）。Agent 應改用 `interactive_open(--allow-attached)` 進入 recovery 流程而非 `console_attach`。
```

- [ ] **Step 3: Add interactive section update**

Find the `interactive_open` description and append:
```markdown
**`allow_attached` 入參**（issue #44）：opt-in、預設 `False`。當 `True` 時放寬 READY-only gate 至 `state ∈ {ATTACHED}` + bridge alive + `rx_tail` 當下匹配 `bootloader_prompts`。匹配失敗回 `SESSION_NOT_READY` (`error_detail: NOT_BOOTLOADER`)。匹配成功且既有 lease 為 `human:*` 時，daemon 採 stash-and-restore：把 human session-layer lease 從 `_interactive` 暫存到 `session._stashed_human_lease`、`bridge.suspend_interactive()`；recovery close 時 `bridge.resume_interactive()` flush deferred buffer，並還原 stashed lease（若仍未 expire 且 human 仍 console-attached）。Recovery lease `timeout_s` 強制 ≤ `MAX_RECOVERY_LEASE_S`（120s，避免 agent 無限期 suspend 人類）。Result 最外層含 `recovery_mode: bool`。
```

- [ ] **Step 4: Add profile schema description**

Find profile schema section and add:
```markdown
- `bootloader_prompts: list[str]` (opt-in, default `[]`)：宣告 bootloader prompt 的 regex 列表（U-Boot、Marvell、Broadcom CFE、vendor bootloader 等）。每元素為 regex（與 `prompt_regex` 同 flavor）；`session.self_test` 在 `ATTACHED` 狀態下會用此列表去匹配 `bridge.rx_tail` 末行，命中即分類為 `BOOTLOADER`。
```

- [ ] **Step 5: Commit**

```bash
git add docs/serialwrap-spec.md
git commit -m "docs(session): document BOOTLOADER classification and recovery interactive_open

Co-Authored-By: Claude Opus 4.7 <noreply@anthropic.com>"
```

### Task B18: README usage example

**Files:**
- Modify: `README.md`

- [ ] **Step 1: Append a recovery example to `## Usage`**

In `README.md` `## Usage` section (or near a troubleshooting subsection if one exists), add:
```markdown
### Bootloader recovery (issue #44)

When a target board drops into U-Boot or a vendor bootloader, the daemon detects
this via `profile.bootloader_prompts` and reports `classification: BOOTLOADER`.
An agent can then drive the bootloader without evicting a human observer:

```bash
# 1. Confirm the daemon sees the bootloader
./serialwrap session self-test --selector COM0
# → classification: BOOTLOADER, matched_prompt: ^=> $

# 2. Open a recovery interactive lease (suspends human input, capped at 120s)
./serialwrap session interactive-open --selector COM0 --allow-attached
# → returns interactive_id, recovery_mode: true

# 3. Drive U-Boot via raw bytes
./serialwrap session interactive-send --interactive-id <id> --data "printenv\n"
./serialwrap session interactive-send --interactive-id <id> --data "reset\n"

# 4. Close the lease — human input that arrived during recovery is replayed
./serialwrap session interactive-close --interactive-id <id>
```

The human observer's console stays attached throughout; their keystrokes are
buffered during the recovery window and flushed to UART on close.
```

- [ ] **Step 2: Commit**

```bash
git add README.md
git commit -m "docs(readme): add bootloader recovery usage example

Co-Authored-By: Claude Opus 4.7 <noreply@anthropic.com>"
```

### Task B19: Func-test fixture (end-to-end)

**Files:**
- Create or modify: `func-test/test_bootloader_recovery_fixture.py` (or extend existing fake-target fixture)

Note: This is the most environment-dependent task. If `func-test/` lacks a fake-target fixture you can drive (check `func-test/` contents), skip this task and rely on Task B20 manual verification + the unit tests above.

- [ ] **Step 1: Inspect `func-test/`**

Run: `ls func-test/ && cat func-test/conftest.py 2>/dev/null | head -60`

- [ ] **Step 2: If a usable fake-target fixture exists, create the test**

Create `func-test/test_bootloader_recovery.py` (adapt to actual fixture API):
```python
"""End-to-end: fake target sitting at U-Boot prompt, agent recovers without
evicting a human observer."""

def test_recovery_with_human_observer(fake_target_uboot_fixture):
    target, daemon = fake_target_uboot_fixture
    # 1. Daemon attaches; target sits at U-Boot
    # 2. Human attaches console
    # 3. self_test reports BOOTLOADER
    # 4. Human types into console — bytes delivered to UART (interactive_owner=human)
    # 5. Agent opens recovery via allow_attached=True
    # 6. Human types more — bytes accumulate in deferred buffer
    # 7. Agent sends "reset\n" — UART receives reset
    # 8. Agent closes recovery
    # 9. Verify human's deferred bytes flushed to UART AFTER reset
    # 10. Verify human lease back in _interactive
    ...
```

- [ ] **Step 3: If no fixture exists, skip and document in tasks.md**

Skip-edit `openspec/changes/2026-05-07-bootloader-recovery-44/tasks.md` Task 7.12 (`func-test`) to point at "covered by manual verification in Task B20".

- [ ] **Step 4: Commit (only if you wrote code)**

```bash
git add func-test/test_bootloader_recovery.py
git commit -m "test(func-test): end-to-end bootloader recovery with human observer

Co-Authored-By: Claude Opus 4.7 <noreply@anthropic.com>"
```

### Task B20: Manual verification on real hardware (or skip if no hardware)

**Files:**
- (no file changes; verification log goes into PR description)

- [ ] **Step 1: Boot a target board into U-Boot**

Hold a key during target reset to interrupt autoboot, or trigger from a known-broken state.

- [ ] **Step 2: Daemon: attach + human console + self_test**

```bash
./serialwrap daemon stop && ./serialwrap daemon start
./serialwrap session attach --selector COM0
./serialwrap session console-attach --selector COM0   # human observer
./serialwrap session self-test --selector COM0
```
Expected: `classification: BOOTLOADER`, `matched_prompt` filled, `rx_tail` shows `=> ` or vendor prompt, `interactive_owner: human:...`, `human_attached: true`.

- [ ] **Step 3: From a separate terminal: open recovery, drive U-Boot, close**

```bash
ID=$(./serialwrap session interactive-open --selector COM0 --allow-attached --owner agent --timeout 60 | jq -r .interactive_id)
./serialwrap session interactive-send --interactive-id $ID --data "printenv\n"
./serialwrap session interactive-status --interactive-id $ID --screen-chars 4096
./serialwrap session interactive-send --interactive-id $ID --data "reset\n"
sleep 3
./serialwrap session interactive-close --interactive-id $ID
```

While `printenv` and `reset` are running, type into the human console-attach window — those bytes should NOT appear on UART until the close call.

- [ ] **Step 4: Verify human lease restored**

Run: `./serialwrap session self-test --selector COM0`
Expected: human lease reflected (after reset, target may be in OS or back at U-Boot depending on timing — either way, no stuck recovery).

- [ ] **Step 5: Negative: bootloader_prompts not configured**

Run on a profile without `bootloader_prompts`:
Expected: `self_test` returns `ATTACHED_NOT_READY` (backward-compat).

- [ ] **Step 6: Negative: open recovery when board is in OS**

Run: `./serialwrap session interactive-open --selector COM0 --allow-attached`
Expected: when board is in OS and rx_tail has OS prompt: `error_code: SESSION_NOT_READY`, `error_detail: NOT_BOOTLOADER`.

- [ ] **Step 7: Document outcomes**

Capture command transcripts and append to PR body under "## Test Plan / Manual Verification". If hardware unavailable, write "Hardware verification deferred; covered by unit + func-test fixtures in Tasks B1-B19" in the PR.

### Task B21: Migrate OpenSpec change → archive (post-merge — DO NOT run before merge)

This task is a reminder for the post-merge step (Task 13.1 in the OpenSpec change tasks.md). Do NOT execute during PR; it's the merge-aftermath step.

After PR is merged to main:
```bash
mv openspec/changes/2026-05-07-bootloader-recovery-44 openspec/changes/archive/
# Then merge the change's specs/ into openspec/specs/:
mkdir -p openspec/specs/session-interactive
cp openspec/changes/archive/2026-05-07-bootloader-recovery-44/specs/session-interactive/spec.md openspec/specs/session-interactive/spec.md
# For session-selftest: append the new ADDED Requirements to the existing spec.md
# (manual merge to avoid clobbering selftest-collab-handoff content)
```

### Task B22: Final verification + PR

**Files:**
- (no file changes)

- [ ] **Step 1: Full test suite**

Run: `python3 -m pytest -q tests/`
Expected: same baseline as Task A1 (existing known-failure unchanged); all new bootloader recovery tests pass.

- [ ] **Step 2: Policy check**

Run: `python3 -m policy_check --repo .`
Expected: zero failures.

- [ ] **Step 3: OpenSpec validate**

Run: `openspec validate 2026-05-07-bootloader-recovery-44 --strict`
Expected: `is valid`.

- [ ] **Step 4: Push branch**

Run: `git push -u origin feature/bootloader-recovery-44`

- [ ] **Step 5: Open PR**

Run:
```bash
gh pr create --title "feat(session): add bootloader recovery interactive lease (#44)" --body "$(cat <<'EOF'
## Summary

Closes #44. Adds an agent-driven bootloader recovery interactive lease that preserves human console ownership via stash-and-restore, plus paulsha-conventions v1.0.0 baseline.

## What Changes

- Profile schema: opt-in `bootloader_prompts` regex list (default `[]`)
- `session.self_test`: `BOOTLOADER` classification with `matched_prompt` + `rx_tail` evidence
- `session.interactive_open`: `allow_attached=True` opens recovery lease in ATTACHED + bootloader; stash-and-restore of human lease; bridge suspend/resume
- `MAX_RECOVERY_LEASE_S` clamp (120s) prevents agent from indefinitely suspending humans
- paulsha-conventions v1.0.0 bootstrap (R-01 ~ R-16): `.paul-project.yml`, `VERSION`, `CHANGELOG.md`, agent files, PR template, policy-check workflow

## Specs

- Brainstorming: `docs/superpowers/specs/2026-05-07-issue-44-bootloader-recovery-design.md`
- OpenSpec: `openspec/changes/2026-05-07-bootloader-recovery-44/`

## Test Plan

- [x] `python3 -m pytest -q tests/` green (new file: `tests/test_bootloader_recovery.py` with 20+ scenarios)
- [x] `python3 -m policy_check --repo .` green (R-01 ~ R-16)
- [x] `openspec validate 2026-05-07-bootloader-recovery-44 --strict` green
- [ ] Manual hardware verification on BGW720 in U-Boot (see PR body / commit messages)

## Policy Checklist (R-11)

- [x] Branch `feature/bootloader-recovery-44`
- [x] CHANGELOG.md `[Unreleased]` updated per commit
- [x] VERSION at 0.0.0 baseline (no release)
- [x] All R-01 ~ R-16 checks pass locally
- [x] No exemption labels needed

🤖 Generated with [Claude Code](https://claude.com/claude-code)
EOF
)"
```

- [ ] **Step 6: Wait for `Policy Check` workflow to go green**

If R-15 fails because of SHA pinning issues, double-check `.github/workflows/policy-check.yml` matches the spec.

---

## Self-Review

I checked the plan against the spec:

**Spec coverage:**
- Profile schema `bootloader_prompts` → Task B2 ✓
- self_test BOOTLOADER classification → Task B4 ✓
- self_test fallback when empty / preferred when both match → Task B4 ✓
- interactive_open `allow_attached` rejecting ATTACHED w/o flag → Task B7 ✓
- interactive_open rejecting bridge unhealthy / no bootloader match → Task B8 ✓
- interactive_open opening recovery without human lease → Task B9 ✓
- interactive_open stashing human lease → Task B10 ✓
- interactive_open rejecting agent lease → Task B10 ✓
- interactive_close restoring stash (alive / detached / expired) → Task B11 ✓
- Recovery lease auto-expire → Task B12 ✓
- MAX_RECOVERY_LEASE_S clamp + READY unaffected → Task B13 ✓
- interactive_send during recovery (plain + key) → Task B14 ✓
- recovery_mode in lease_context / interactive_status / interactive_open result → Task B6 + B10 ✓
- RPC + CLI passthrough → Task B15 ✓
- Vendor profile updates → Task B16 ✓
- Docs (serialwrap-spec + README usage) → Task B17 + B18 ✓
- paulsha-conventions bootstrap (all 8 R-* requirements via Phase A) → Tasks A1-A12 ✓
- CHANGELOG entries per commit → woven through B2/B4/B15 ✓
- Func-test (best-effort) → Task B19 ✓
- Manual hardware verification → Task B20 ✓
- PR creation → Task B22 ✓

**Placeholder scan:** No "TBD" / "TODO" / "implement later" / vague handwaving. Each step has actual code or commands.

**Type consistency:**
- `_matches_any_bootloader_prompt(rx_tail: str, patterns: list[str] | tuple[str, ...]) -> str | None` — used with both `list` (from `ProfileTemplate.bootloader_prompts`) and `tuple` (from frozen `SessionProfile.bootloader_prompts`); accepted by signature ✓
- `InteractiveLease.recovery_mode: bool` / `suspended_human: bool` — same names used in B5, B6, B10, B11 ✓
- `SessionRuntime._stashed_human_lease: InteractiveLease | None` — same name used everywhere it's referenced ✓
- `MAX_RECOVERY_LEASE_S` / `BOOTLOADER_RX_TAIL_BYTES` — defined once in B1, imported where used ✓

Plan ready.

---

## Execution Handoff

Plan complete and saved to `docs/superpowers/plans/2026-05-07-issue-44-bootloader-recovery-and-conventions.md`. Two execution options:

**1. Subagent-Driven (recommended)** — I dispatch a fresh subagent per task, review between tasks, fast iteration

**2. Inline Execution** — Execute tasks in this session using executing-plans, batch execution with checkpoints

**Which approach?**
