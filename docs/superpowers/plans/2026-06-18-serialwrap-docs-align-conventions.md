# #67 文件對齊 + conventions 升 1.0.5 實作計畫

> **給 agentic worker：** 必用子技能——`superpowers:subagent-driven-development`（建議）或 `superpowers:executing-plans` 逐 task 實作。步驟用 checkbox（`- [ ]`）追蹤。

**目標：** 把 serialwrap repo 的 policy pin 升到 conventions v1.0.5（讓 R-22 doc_reference 在本地與 CI 生效），並一次對齊落後現行架構的文件。

**架構：** 兩支 PR。PR#1 純機械式版本 pin（config + workflow + 四份 agent 檔同步），以 `policy_check` 當測試 harness 做 RED（版本漂移偵測）→ GREEN。PR#2 文件對齊（刪 stub / spec 降級指向 openspec / 標歷史 / README 狀態機補 RELEASED+FLASHING / skills.md 最小 cross-ref），以 `policy_check`（含 R-22）+ grep 斷言當 harness。**不動任何程式碼、不改 capability 行為。**

**技術棧：** `paulsha-conventions` policy engine（`python3 -m policy_check`）、pytest、git、markdown/mermaid。

**設計來源:** `docs/superpowers/specs/2026-06-18-serialwrap-docs-align-and-skill-consolidation-design.md`

---

## Branch & PR 策略

- **PR#1** branch `feature/67-conventions-v1.0.5`（off `main`）：conventions 升版（+ 本計畫與設計文件兩個 planning 檔可一併帶入）。
- **PR#2** branch `feature/67-docs-align`（off PR#1 branch，使其 CI 走已升級的 reusable workflow → R-22 生效）：文件對齊。
- 兩支皆非 `main`。先 merge PR#1，PR#2 rebase 到更新後 `main`。
- 常數：舊 SHA `77a3e8381eeced9dbba623e450ed6a5c1fcc7b18`（v1.0.4）；新 SHA `484f963adddf384d30fa0dd85aef35dddf822ee7`（v1.0.5，lightweight tag）。

---

# PR#1 — conventions 升 1.0.5

### Task 1：複核 v1.0.5 SHA 並重裝本地引擎

**Files:** 無（環境準備）

- [ ] **Step 1：deref tag 確認 commit SHA**

Run:
```bash
git ls-remote https://github.com/hamanpaul/paulsha-conventions.git refs/tags/v1.0.5 'refs/tags/v1.0.5^{}'
```
Expected：只出現一行 `484f963adddf384d30fa0dd85aef35dddf822ee7   refs/tags/v1.0.5`（lightweight tag，無 `^{}` deref 行 → 該 SHA 即 commit）。若出現 `^{}` 行，改用該行的 SHA。

- [ ] **Step 2：重裝本地引擎到 v1.0.5**

Run:
```bash
python3 -m pip install --user --disable-pip-version-check --force-reinstall \
  "git+https://github.com/hamanpaul/paulsha-conventions.git@484f963adddf384d30fa0dd85aef35dddf822ee7"
```
Expected：安裝成功。

- [ ] **Step 3：確認引擎含 R-22（先前 stale 只到 R-16）**

Run:
```bash
python3 -m policy_check --repo . 2>&1 | grep -oE 'R-[0-9]+' | sort -u | tr '\n' ' '
```
Expected：出現 `R-22`（且 R-17~R-21 也在）。

### Task 2：RED — 只 bump config，證明 policy_check 抓到版本漂移

**Files:**
- Modify: `.paul-project.yml`（`policy_version` 行）

- [ ] **Step 1：開 PR#1 分支**

Run:
```bash
git switch main && git switch -c feature/67-conventions-v1.0.5
git branch --show-current
```
Expected：`feature/67-conventions-v1.0.5`。
（若 planning 檔在別的分支，先 `git cherry-pick` 設計文件 commit `d6f4197` 與本計畫 commit 過來，或在本分支重新 commit。）

- [ ] **Step 2：只把 `.paul-project.yml` 的 policy_version 改 1.0.5**

`.paul-project.yml` 第 2 行：
```yaml
policy_version: "1.0.5"
```
（其餘四份 agent 檔與 workflow 暫不動。）

- [ ] **Step 3：跑 policy_check，確認 RED**

Run:
```bash
python3 -m policy_check --repo . 2>&1 | grep -E 'R-14|R-20'
```
Expected：**FAIL** —
- `R-14` FAIL：agent 檔 `policy_version`（1.0.4）與 `.paul-project.yml`（1.0.5）不一致。
- `R-20`：workflow 內 `policy_version: "1.0.4"` 與 config（1.0.5）不一致（R-20 在有 workflow literal 時 FAIL）。

這證明版本一致性 gate 確實會抓漂移。

### Task 3：GREEN — 同步四份 agent 檔 + workflow，全綠

**Files:**
- Modify: `CLAUDE.md`、`AGENTS.md`、`GEMINI.md`、`.github/copilot-instructions.md`
- Modify: `.github/workflows/policy-check.yml`

- [ ] **Step 1：四份 agent 檔 marker + policy_version 行**

每份檔案首行 marker 與第 3 行：
```
<!-- managed-by: hamanpaul/paulsha-conventions@v1.0.5 -->
```
```
policy_version: 1.0.5
```
Run（批次替換 marker 與 policy_version 行）:
```bash
for f in CLAUDE.md AGENTS.md GEMINI.md .github/copilot-instructions.md; do
  sed -i 's#paulsha-conventions@v1\.0\.4#paulsha-conventions@v1.0.5#; s/^policy_version: 1\.0\.4/policy_version: 1.0.5/' "$f"
done
```

- [ ] **Step 2：四份 agent 檔 body 內的 install 指令 SHA 與 pinned SHA**

CLAUDE.md（及其餘三份若含相同段）body 內兩處 SHA：`policy engine pinned SHA` 與 install 指令的 `@<SHA>`。
Run:
```bash
for f in CLAUDE.md AGENTS.md GEMINI.md .github/copilot-instructions.md; do
  sed -i 's/77a3e8381eeced9dbba623e450ed6a5c1fcc7b18/484f963adddf384d30fa0dd85aef35dddf822ee7/g' "$f"
done
grep -rn '77a3e838' CLAUDE.md AGENTS.md GEMINI.md .github/copilot-instructions.md
```
Expected：grep 無殘留舊 SHA。

- [ ] **Step 3：workflow 三處（uses / policy_engine_ref / policy_version）**

`.github/workflows/policy-check.yml`：
```yaml
    uses: hamanpaul/paulsha-conventions/.github/workflows/reusable-policy-check.yml@484f963adddf384d30fa0dd85aef35dddf822ee7
    with:
      policy_profile: flat
      policy_version: "1.0.5"
      policy_engine_ref: 484f963adddf384d30fa0dd85aef35dddf822ee7
```
Run:
```bash
sed -i 's/77a3e8381eeced9dbba623e450ed6a5c1fcc7b18/484f963adddf384d30fa0dd85aef35dddf822ee7/g; s/policy_version: "1\.0\.4"/policy_version: "1.0.5"/' .github/workflows/policy-check.yml
grep -nE '484f963a|policy_version' .github/workflows/policy-check.yml
```
Expected：兩處 SHA 為新值、`policy_version: "1.0.5"`。

- [ ] **Step 4：跑 policy_check，確認 GREEN**

Run:
```bash
python3 -m policy_check --repo .
echo "EXIT=$?"
```
Expected：`EXIT=0`，R-01~R-22 無 FAIL（R-22 對既有 README/docs 的陳年懸空僅 WARN、不擋）。

### Task 4：CHANGELOG + commit（PR#1）

**Files:**
- Modify: `CHANGELOG.md`（`[Unreleased]`）

- [ ] **Step 1：CHANGELOG `[Unreleased]` 記一筆**

於 `## [Unreleased]` 下 `### Changed`（無則新增）加：
```markdown
- 升級 policy conventions v1.0.4 → v1.0.5（pin 重釘到 `484f963a…`）：`.paul-project.yml`、四份 agent 檔（marker/policy_version/install·pinned SHA）、`.github/workflows/policy-check.yml`（uses/policy_engine_ref/policy_version）一併同步。新增 R-22 doc_reference 於本地與 CI 生效。
```

- [ ] **Step 2：確認 VERSION 未動**

Run:
```bash
git diff --name-only | grep -x VERSION && echo "ERROR: VERSION 不該動" || echo "OK: VERSION 未動"
```
Expected：`OK: VERSION 未動`。

- [ ] **Step 3：commit**

Run:
```bash
git add .paul-project.yml CLAUDE.md AGENTS.md GEMINI.md .github/copilot-instructions.md .github/workflows/policy-check.yml CHANGELOG.md
git commit -m "chore(policy): 升級 conventions v1.0.4 → v1.0.5（重釘 484f963a，啟用 R-22）

Co-authored-by: Copilot <223556219+Copilot@users.noreply.github.com>
Co-Authored-By: Claude Opus 4.8 <noreply@anthropic.com>"
```

---

# PR#2 — 文件對齊

> 前置：PR#1 已 merge；本分支 off PR#1（或 rebase 到含升版的 `main`），本地引擎已是 v1.0.5。

### Task 5：刪 `sills.md`

**Files:**
- Delete: `sills.md`

- [ ] **Step 1：RED-style pre-flight — 確認無 in-scope doc 引用**

Run:
```bash
grep -rniE 'sills' README.md docs/ skills.md | grep -v 'docs/superpowers'
echo "EXIT=$?"
```
Expected：無輸出（`EXIT=1`）。若有任一 in-scope（README.md / `docs/**` 非 superpowers / skills.md）引用 → 先改該引用，否則刪後 R-22 判 removed-this-change FAIL。

- [ ] **Step 2：刪檔**

Run:
```bash
git rm sills.md
```

- [ ] **Step 3：policy_check 確認 R-22 不 FAIL**

Run:
```bash
python3 -m policy_check --repo . 2>&1 | grep -E 'R-22'
echo "EXIT=${PIPESTATUS[0]}"
```
Expected：R-22 非 FAIL（pass 或對既有檔 WARN）。

### Task 6：`docs/serialwrap-spec.md` 降級為薄概覽 + 指向 openspec/specs

**Files:**
- Modify: `docs/serialwrap-spec.md`

- [ ] **Step 1：移除「完整規格」自稱（行 5）**

把 `docs/serialwrap-spec.md:5`：
```
本文件定義目前主線 `serialwrap` 的決策完整規格。…
```
改為：
```
> ⚠️ 本文件為**概覽**，非完整規格。canonical 規格已轉移到 `openspec/specs/*`（見下方各 capability 連結）；本文件僅保留高層脈絡，不再追蹤逐項行為。
```

- [ ] **Step 2：在開頭加 capability 索引（連結 target 皆已驗證存在）**

於概覽段後加一節：
```markdown
## Canonical 規格（openspec/specs）

- 裝置 handoff（release/attach、#54）：[`openspec/specs/device-handoff/spec.md`](../openspec/specs/device-handoff/spec.md)
- MCU flash 端點（`/dev/ttyMCU`、FLASHING，#55）：[`openspec/specs/mcu-flash-broker/spec.md`](../openspec/specs/mcu-flash-broker/spec.md)
- command_capable readiness（#51）：[`openspec/specs/session-command-readiness/spec.md`](../openspec/specs/session-command-readiness/spec.md)
- 互動 session / soft-preempt（#53）：[`openspec/specs/session-interactive/spec.md`](../openspec/specs/session-interactive/spec.md)
- self-test：[`openspec/specs/session-selftest/spec.md`](../openspec/specs/session-selftest/spec.md)
```

- [ ] **Step 3：刪除已被 openspec 取代的過時 detach 狀態機細節段**

把文中過時的 detach 狀態機/協定細節整段刪除或大幅精簡為一句「詳見上方 openspec 連結」。保留真正高層、未漂移的脈絡（目的、共享模型）。

- [ ] **Step 4：grep 斷言 + R-22**

Run:
```bash
grep -nE '完整規格' docs/serialwrap-spec.md; echo "claim EXIT=$?"   # 期望 EXIT=1（已無）
python3 -m policy_check --repo . 2>&1 | grep -E 'R-22'              # 期望非 FAIL；連結 target 存在
```
Expected：無「完整規格」殘留；R-22 不因本檔新增連結 FAIL。

### Task 7：`docs/plan.md` / `docs/todos.md` 標歷史

**Files:**
- Modify: `docs/plan.md`、`docs/todos.md`

- [ ] **Step 1：兩檔頂部加歷史標頭**

各檔第一行前插入：
```markdown
> 📌 **歷史快照**（截至 Phase 3）。本檔不反映 #51 / #53 / #54 / #55 之後的現況，僅留作歷史，不再維護。最新狀態見 `README.md` 與 `openspec/specs/*`。
```

- [ ] **Step 2：確認無新懸空引用**

Run:
```bash
python3 -m policy_check --repo . 2>&1 | grep -E 'R-22'
```
Expected：非 FAIL。

### Task 8：`README.md` 狀態機補 RELEASED + FLASHING

**Files:**
- Modify: `README.md`（mermaid 區塊，約行 96–113）

- [ ] **Step 1：在 mermaid stateDiagram 加 RELEASED + FLASHING 轉移**

於 `README.md` 的 `stateDiagram-v2` 區塊（`RECOVERING --> DETACHED: device lost` 之後、` ``` ` 之前）插入：
```
    ATTACHED --> RELEASED: device release
    READY --> RELEASED: device release
    RELEASED --> ATTACHING: device attach
    ATTACHED --> FLASHING: mcu flash（/dev/ttyMCU 認線）
    READY --> FLASHING: mcu flash（/dev/ttyMCU 認線）
    FLASHING --> ATTACHED: flash 結束（恢復先前）
    FLASHING --> READY: flash 結束（恢復先前）
```
（語意依據：`openspec/specs/device-handoff/spec.md` RELEASED 不自動 re-attach、attach 才收回；`openspec/specs/mcu-flash-broker/spec.md` FLASHING 認線後進入、結束恢復先前狀態。）

- [ ] **Step 2：在狀態機段補兩句說明（連結 canonical）**

於 `### ATTACHED vs READY…` 區塊後加：
```markdown
### `RELEASED` / `FLASHING`

- `RELEASED`（#54）：`device release` 把 raw 裝置交給外部工具獨佔（如燒錄），broker 關閉 FD、**不自動搶回**、跨 daemon 重啟保留；`device attach` 收回。詳見 `openspec/specs/device-handoff/spec.md`。
- `FLASHING`（#55）：外部 flasher 經 `/dev/ttyMCU` 認線後 session 進入，期間 `cmd submit` 回 `FLASHING_BUSY`、其他 COM 不受影響、daemon 不死；flash 結束自動恢復先前狀態。詳見 `openspec/specs/mcu-flash-broker/spec.md`。
```

- [ ] **Step 3：grep 斷言 + R-22**

Run:
```bash
grep -cE 'RELEASED|FLASHING' README.md                    # 期望 > 0
python3 -m policy_check --repo . 2>&1 | grep -E 'R-22'     # 期望非 FAIL
```
Expected：README 含 RELEASED/FLASHING；R-22 不 FAIL。

### Task 9：`skills.md` 最小 cross-ref

**Files:**
- Modify: `skills.md`

- [ ] **Step 1：頂部加 cross-ref 與過時標記**

於 `skills.md` 第 1 行 `# serialwrap-mcp Agent Skill` 之後插入：
```markdown

> ⚠️ **本檔狀態**：與 `hamanpaul/custom-skills` 的同名 skill 為兩份平行 agent 指南，且**只更新到 event 時代**——尚缺 device release/attach（#54）、command_capable + `PROFILE_NOT_COMMAND_CAPABLE`（#51）、`/dev/ttyMCU` + `mcu patterns/status`（#55）、human_active/soft-preempt（#53）。source-of-truth 收斂、改名（`serialwrap-mcp` → `serialwrap`）與 plugin 打包追蹤於 **#59**；在 #59 完成前，agent 操作以 README + `openspec/specs/*` 為準。
```

- [ ] **Step 2：R-22 確認**

Run:
```bash
python3 -m policy_check --repo . 2>&1 | grep -E 'R-22'
```
Expected：非 FAIL（注意：新加文字若含像路徑的反引號 token，須確保存在；上述只含 `mcu patterns/status` 等非路徑 token 與既有符號）。

### Task 10：全量驗證 + CHANGELOG + commit（PR#2）

**Files:**
- Modify: `CHANGELOG.md`

- [ ] **Step 1：CHANGELOG `[Unreleased]` 記一筆**

```markdown
- 文件與現行架構對齊：刪除 `sills.md` 轉址 stub；`docs/serialwrap-spec.md` 降級為概覽並指向 `openspec/specs/*`；`docs/plan.md`/`docs/todos.md` 標為歷史快照；`README.md` 狀態機補 `RELEASED`(#54)/`FLASHING`(#55)；`skills.md` 加 #59 cross-ref 與過時標記。
```

- [ ] **Step 2：完整 policy_check**

Run:
```bash
python3 -m policy_check --repo .
echo "EXIT=$?"
```
Expected：`EXIT=0`，無 FAIL。

- [ ] **Step 3：跑測試（確認沒被文件變更波及，記錄既有 flaky）**

Run:
```bash
python3 -m pytest -q tests/
```
Expected：除既有 flaky（`test_five_agents_three_rounds_no_conflict`、`t8_full_run_simulation`、`test_t1_wal_reset_preserves_console`）外無新失敗。文件變更理論上不影響測試。

- [ ] **Step 4：commit**

Run:
```bash
git add -A
git commit -m "docs: 文件與現行架構對齊（清 sills.md / spec 降級指向 openspec / README 補 RELEASED·FLASHING / skills.md cross-ref #59）

Co-authored-by: Copilot <223556219+Copilot@users.noreply.github.com>
Co-Authored-By: Claude Opus 4.8 <noreply@anthropic.com>"
```

---

## 驗收對照（self-review）

- conventions：`.paul-project.yml` / 四份 agent 檔 / workflow 皆 1.0.5 且 SHA=`484f963a…`；`policy_check` R-01~R-22 全綠（R-13/R-14/R-16/R-20 pass）。
- 文件：`sills.md` 已刪；`docs/serialwrap-spec.md` 無「完整規格」字樣且指向 openspec；`plan.md`/`todos.md` 標歷史；`README.md` 狀態機含 RELEASED/FLASHING；`skills.md` 有 #59 cross-ref。
- R-22：本次變更未產生新懸空引用（removed-this-change=0 FAIL）。
- 無程式碼變更、`VERSION` 未動、無新測試失敗。
- 範疇：#59（退役 MCP/改名/skill 整併）不在本計畫。
