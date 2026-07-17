# bcm/BDK agent 操作健壯性 Implementation Plan（#140 帳密觀測性 ＋ #114 autoboot lease）

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 讓 agent 操作 bcm/BDK 板時：(1) 帳密解析為空且 profile 宣告了帳密來源時不再靜默狂 probe、回明確 `CREDENTIALS_UNRESOLVED` 含解析路徑；(2) 能在 U-Boot autoboot 倒數窗開 recovery lease 中斷 autoboot。

**Architecture:** 兩個獨立小改動。A：`sw_core/auth.py` 讓 `resolve_session_auth` 回報解析狀態；`sw_core/session_manager.py` 在「宣告帳密但解析空」時把 session 標為終態 `CREDENTIALS_UNRESOLVED` 並停止自動 reprobe，一次性清楚示警。B：`sw_core/session_manager.py` 的 `interactive_open` allow_attached gate 擴充，複用 #130 `detect_boot_banner`，banner 命中亦授予 recovery lease 並回 `boot_interrupt`。

**Tech Stack:** Python 3.10+、pytest、既有 UARTBridge/PTY 測試框架。

## Global Constraints

- 純 POSIX/共用邏輯，**不動** `sw_core/rpc_win.py`/`lock_win.py`/`device_source.py` win 後端與 console TCP 路徑；Windows daemon 走同一路徑同步受益、行為無分歧。
- 所有註解/docstring/commit message/文件 **一律繁體中文**。
- RPC 回應維持 `dict[str, Any] + ok: bool`、失敗附 `error_code`、例外不穿越 RPC 邊界。
- 新欄位/錯誤碼皆 **additive**，不改既有欄位；`CREDENTIALS_UNRESOLVED`、`interactive-open` 回應的 `boot_interrupt` 為新增。
- 設定物件 frozen dataclass，執行期狀態用 `dataclasses.replace`。
- commit trailer 三行：`Co-authored-by: Copilot <223556219+Copilot@users.noreply.github.com>` / `Co-Authored-By: Claude Fable 5 <noreply@anthropic.com>` / `Claude-Session: https://claude.ai/code/session_01NK1zUT41zHT5NAojzRhNR9`。
- 測試以 `python3 -m pytest -q tests/ --ignore=tests/test_agent_defer_tx.py --ignore=tests/test_flash_pump.py --ignore=tests/test_flash_service_wiring.py --ignore=tests/test_human_agent_coexist.py --ignore=tests/test_multiagent_e2e.py --ignore=tests/test_multiagent_stress.py` 為 deterministic gate。
- policy check：`python3 -m policy_check --repo .` 通過（R-22 85 筆 pre-existing warn 屬正常）。

---

### Task 1: auth 回傳解析狀態 `AuthResolution`

**Files:**
- Modify: `sw_core/auth.py`（`resolve_session_auth` 約 :56-89、`SessionAuth` 約 :17）
- Test: `tests/test_auth.py`

**Interfaces:**
- Produces: `AuthResolution`（frozen dataclass：`reason: str`（`"ok"`/`"env_file_missing"`/`"env_file_unreadable"`/`"key_absent"`/`"not_configured"`）、`env_file_path: str | None`）；`resolve_session_auth(sp) -> tuple[SessionAuth, AuthResolution]`。
- Consumes（既有）：`SessionProfile`（`env_file`/`user_env`/`pass_env`/`username`）、`parse_env_file`。

- [ ] **Step 1: 寫 failing test**（`tests/test_auth.py` 新增；用 `tmp_path` 造 env 檔與 SessionProfile）

```python
def test_resolve_auth_env_file_missing(tmp_path):
    sp = _mk_profile(env_file=str(tmp_path / "nope.env"), user_env="BRCM_USER", pass_env="BRCM_PASS")
    auth, res = resolve_session_auth(sp)
    assert res.reason == "env_file_missing"
    assert res.env_file_path.endswith("nope.env")
    assert auth.username is None and auth.password is None

def test_resolve_auth_key_absent(tmp_path):
    p = tmp_path / "brcm.env"; p.write_text("OTHER=x\n")
    sp = _mk_profile(env_file=str(p), user_env="BRCM_USER", pass_env="BRCM_PASS")
    _, res = resolve_session_auth(sp); assert res.reason == "key_absent"

def test_resolve_auth_ok(tmp_path):
    p = tmp_path / "brcm.env"; p.write_text("BRCM_USER=admin\nBRCM_PASS=admin\n")
    sp = _mk_profile(env_file=str(p), user_env="BRCM_USER", pass_env="BRCM_PASS")
    auth, res = resolve_session_auth(sp)
    assert res.reason == "ok" and auth.username == "admin" and auth.password == "admin"

def test_resolve_auth_not_configured():
    sp = _mk_profile(env_file=None, user_env=None, pass_env=None)
    _, res = resolve_session_auth(sp); assert res.reason == "not_configured"
```
（`_mk_profile` helper：以現有 `SessionProfile` 建構最小欄位；參考 `tests/test_login_fsm.py` 既有 profile 構造方式。）

- [ ] **Step 2: 跑測試確認 fail**
Run: `python3 -m pytest tests/test_auth.py -q -k resolve_auth`
Expected: FAIL（`resolve_session_auth` 回單一值、無 `AuthResolution`）

- [ ] **Step 3: 實作**（`sw_core/auth.py`）
- 新增 `@dataclasses.dataclass(frozen=True) class AuthResolution: reason: str = "ok"; env_file_path: str | None = None`。
- `resolve_session_auth` 改回 `tuple[SessionAuth, AuthResolution]`。判定：
  - 無 `user_env`/`pass_env`/`env_file` → `not_configured`。
  - 有 `env_file`：`expanded` 不存在 → `env_file_missing`；讀取例外 → `env_file_unreadable`；讀成功但缺 `user_env`/`pass_env` key 或值空 → `key_absent`；否則 `ok`。
  - env 全從 os.environ 補齊而齊全 → `ok`。
  - `env_file_path` 帶 `expanded`（有 env_file 時）。
- **不改** 既有 log.warning 行為（保留），另供上層決定示警。

- [ ] **Step 4: 跑測試確認 pass**
Run: `python3 -m pytest tests/test_auth.py -q -k resolve_auth`
Expected: PASS

- [ ] **Step 5: 修 4 個呼叫端相容**（`sw_core/session_manager.py:1843,1963,2183,2505`）
每處 `auth = resolve_session_auth(...)` → `auth, _auth_res = resolve_session_auth(...)`（Task 2 會用到 `_auth_res`，此步先解包不改行為）。跑既有 `tests/test_login_fsm.py`、`tests/test_session_bind.py` 確認無破壞。

- [ ] **Step 6: Commit**
```bash
git add sw_core/auth.py tests/test_auth.py sw_core/session_manager.py
git commit -m "feat(auth): resolve_session_auth 回傳 AuthResolution 解析狀態（#140）" # + trailer
```

---

### Task 2: 宣告帳密但解析空 → CREDENTIALS_UNRESOLVED 終態、不再空 probe

**Files:**
- Modify: `sw_core/session_manager.py`（attach 路徑約 :1843/:1963/:2183/:2505 的 auth 解析後；reprobe gate）
- Modify: `sw_core/constants.py`（若需錯誤碼常數）
- Test: `tests/test_login_fsm.py` 或新 `tests/test_credentials_unresolved.py`

**Interfaces:**
- Consumes: `AuthResolution`（Task 1）。
- Produces: 常數 `ERROR_CREDENTIALS_UNRESOLVED = "CREDENTIALS_UNRESOLVED"`；session 進此態的判定 helper `_credentials_declared_but_unresolved(profile, auth_res) -> bool`。

- [ ] **Step 1: 寫 failing test**（session 層：宣告 env_file 帳密但檔缺 → attach 後 last_error=CREDENTIALS_UNRESOLVED、bridge 無空帳密 TX）
測試用既有 daemon/PTY fake-target 手法（參考 `tests/test_login_fsm.py` / `tests/test_session_bind.py`）：fake target 對送入的空行回 `Login:`／`Password:`／`Login incorrect`；profile 宣告 `env_file`（指向不存在檔）+ user_env/pass_env；斷言 attach 後 `session.last_error == "CREDENTIALS_UNRESOLVED"`，且 fake target 未收到超過 1 次 probe（不進 login 迴圈）。

- [ ] **Step 2: 跑測試確認 fail**
Run: `python3 -m pytest tests/test_credentials_unresolved.py -q`
Expected: FAIL

- [ ] **Step 3: 實作**
- `_credentials_declared_but_unresolved`: `bool(profile.user_env or profile.pass_env or profile.env_file) and auth_res.reason != "ok"`。
- attach 路徑：解析 auth 後若上式為真 → 不進 `ensure_ready`/login/probe，直接 `_transition_to_attached(session, "CREDENTIALS_UNRESOLVED")`（沿用既有 last_error set 機制），並 `return`。
- reprobe gate（找 `_prepare_reprobe_locked` 或等價）：session `last_error == CREDENTIALS_UNRESOLVED` 時不排 reprobe（終態，需手動 attach/recover）。手動 `attach`/`recover` 會重新解析 auth，若使用者已補帳密則 `reason == ok`、正常往下。
- 一次性 `log.warning` + WAL 事件（若有 WAL 事件 API，比照既有 system 事件）：含 `auth_res.env_file_path` 與 `auth_res.reason`，**不印帳密值**。

- [ ] **Step 4: 跑測試確認 pass**
Run: `python3 -m pytest tests/test_credentials_unresolved.py -q`
Expected: PASS

- [ ] **Step 5: 回歸——not_configured 不受影響**
加測試：profile 未宣告帳密（passthrough/prpl 無 env_file）→ 行為與變更前一致（不進 CREDENTIALS_UNRESOLVED）。跑 `tests/test_login_fsm.py` 全綠。

- [ ] **Step 6: Commit**
```bash
git commit -m "fix(session): 宣告帳密但解析空時回 CREDENTIALS_UNRESOLVED 終態、不再空 probe login（#140）" # + trailer
```

---

### Task 3: interactive_open allow_attached gate 擴充 detect_boot_banner + boot_interrupt

**Files:**
- Modify: `sw_core/session_manager.py`（`interactive_open` allow_attached ATTACHED 分支，約 :3065-3075 的 `_matches_any_bootloader_prompt` gate）
- Test: `tests/test_interactive_lease.py`（或既有 interactive 測試檔）

**Interfaces:**
- Consumes: `detect_boot_banner`（`sw_core/login_fsm.py:14`，既有）、`_matches_any_bootloader_prompt`（既有）。
- Produces: allow_attached ATTACHED 分支回應可含 `boot_interrupt: True`（banner 命中時）。

- [ ] **Step 1: 寫 failing test**
```python
def test_allow_attached_opens_lease_on_autoboot_countdown():
    # session ATTACHED、bridge healthy、rx_tail 末段含 "Hit any key to stop autoboot:  2"
    # 但不匹配 bootloader_prompts（如 prpl 空 bootloader_prompts 或無 => 行）
    res = mgr.interactive_open(selector, allow_attached=True)
    assert res["ok"] is True and res["boot_interrupt"] is True
    assert mgr._interactive[res["interactive_id"]].recovery_mode is True

def test_allow_attached_bootloader_prompt_no_boot_interrupt():
    # rx_tail 末行 "=> " 匹配 bootloader_prompts
    res = mgr.interactive_open(selector, allow_attached=True)
    assert res["ok"] is True and res.get("boot_interrupt") is not True

def test_allow_attached_rejects_when_neither_prompt_nor_banner():
    # rx_tail 一般 shell log、無 => 無 banner
    res = mgr.interactive_open(selector, allow_attached=True)
    assert res["ok"] is False and res["error_detail"] == "NOT_BOOTLOADER"
```
（構造手法參考既有 allow_attached 測試——設 session ATTACHED + 注入 bridge.rx_tail 內容。）

- [ ] **Step 2: 跑測試確認 fail**
Run: `python3 -m pytest tests/test_interactive_lease.py -q -k allow_attached`
Expected: FAIL（banner 情境現回 NOT_BOOTLOADER）

- [ ] **Step 3: 實作**（ATTACHED 分支）
- 取 `rx_tail_clean` 後：`matched = _matches_any_bootloader_prompt(rx_tail_clean, session.profile.bootloader_prompts)`。
- 若 `matched is None` 且 `detect_boot_banner(rx_tail_clean)` 為真 → 視為可授予，設 `boot_interrupt = True`。
- 若 `matched is None` 且非 banner → 維持 `NOT_BOOTLOADER`。
- 授予 lease 的既有路徑不變（recovery_mode/owner/human stash-restore）；成功回應在 banner 情境加 `result["boot_interrupt"] = True`（prompt 情境不加或 False）。

- [ ] **Step 4: 跑測試確認 pass**
Run: `python3 -m pytest tests/test_interactive_lease.py -q -k allow_attached`
Expected: PASS

- [ ] **Step 5: Commit**
```bash
git commit -m "feat(session): interactive-open --allow-attached 於 autoboot 倒數窗授予 recovery lease（#114）" # + trailer
```

---

### Task 4: PTY fake-target E2E（sandbox，非 tests/）

**Files:**
- Create: sandbox 腳本（scratchpad，非 repo）——複用本 session `scratchpad/sw-130/`、`sw-123/` 手法（throwaway daemon + PTY fake target，獨立 SERIALWRAP_* env + 短 socket symlink）

- [ ] **Step 1: #140 E2E**——fake target 吐 `Login:`；profile 宣告 env_file（不存在）→ throwaway daemon attach → 斷言 session `last_error=CREDENTIALS_UNRESOLVED`、WAL 零空帳密 TX、daemon.log 有含路徑的警告。清理 daemon/target、pgrep 驗無殘留。
- [ ] **Step 2: #114 E2E**——fake U-Boot target 吐 autoboot 倒數行並保持 → `interactive-open --allow-attached` 回 `boot_interrupt:true` → `interactive-send` 送鍵 → fake target 轉 `=>`、lease 仍持有。清理。
- [ ] **Step 3: 記錄兩 E2E 翻轉證據於 PR 描述**（不 commit sandbox 腳本）。

---

### Task 5: 文件 + changelog + 收斂

**Files:**
- Modify: `README.md`（中英）、`docs/serialwrap-spec.md`、`sw_core/assets/skill/SKILL.md`
- Create: `changelog.d/140-credentials-unresolved.md`、`changelog.d/114-autoboot-interrupt-lease.md`

- [ ] **Step 1:** README（中英對照）：`CREDENTIALS_UNRESOLVED` 排查段（明載 env_file **相對 daemon profile-dir** 解析、非 XDG config；恢復步驟＝補帳密後手動 attach/recover）；`interactive-open --allow-attached` autoboot 倒數窗中斷用法與 `boot_interrupt`。R-16：若動到被 marker 涵蓋的 help 段需重生（本變更預期不動 top-level/daemon/session/device group help；確認後跳過）。
- [ ] **Step 2:** `docs/serialwrap-spec.md`、`SKILL.md` 同步。
- [ ] **Step 3:** 兩個 changelog fragment（`140-*` type: fix、`114-*` type: feat；frontmatter 對齊既有）。
- [ ] **Step 4:** `grep -rnE '</content>|</invoke>|</parameter>'` 產出 .md 無殘留。
- [ ] **Step 5:** 跑 deterministic pytest 子集全綠、`policy_check --repo .` 通過。
- [ ] **Step 6: Commit** 文件與 fragment。

---

### Task 6: 對抗式 review + openspec archive + PR

- [ ] **Step 1:** 獨立對抗式 review agent（唯讀）審 `git diff main...HEAD`：A 的終態判定/reprobe gate/not_configured 不誤擋/示警不洩帳密；B 的 banner 誤授風險/與 #130 相容/回應 additive；測試紅燈有效性；政策面。收斂 findings。
- [ ] **Step 2:** `openspec archive bcm-agent-operability`（依 openspec-archive-change）。
- [ ] **Step 3:** 帶 PR 上下文複現 `policy_check`（`--pr-title`/`--pr-body`/`--pr-base-ref main`/`--pr-head-ref feature/bcm-agent-operability-114-140`）確認 R-09/R-10/R-11/R-16/R-17 綠。
- [ ] **Step 4:** push；`gh pr create` body `Closes #114`、`Closes #140` + Policy Checklist 全勾。
- [ ] **Step 5:** CI 綠後 merge。
