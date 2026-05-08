# Design: ATTACHED / U-Boot fallback recovery + paulsha-conventions 導入（Issue #44）

> 本檔聚焦合約面（schema / RPC / lease / profile）。完整 narrative 與權衡見
> `docs/superpowers/specs/2026-05-07-issue-44-bootloader-recovery-design.md`。

## 1. 解法總覽

```
profile.bootloader_prompts: list[str]                  # 新欄位（opt-in，預設 []）
        │
        ▼
session.self_test
   └── ATTACHED 路徑多一次 RX tail 比對
       命中 → classification "BOOTLOADER"、recommended_action "recover_interactive"
              result 帶 matched_prompt / rx_tail
        │
        ▼
session.interactive_open(selector, allow_attached=True, ...)
   └── 放寬 gate：state ∈ {ATTACHED}、bridge alive、當下重新匹配 bootloader_prompts
       有 human lease → bridge.suspend_interactive()，lease.suspended_human=True
       開出 lease，recovery_mode=True
        │
        ▼
session.interactive_send / interactive_status          # 既有 RPC，重用
   plain / base64 / key 三種 encoding
        │
        ▼
session.interactive_close
   └── lease.suspended_human=True → bridge.resume_interactive()
       human deferred buffer 一次性 flush
```

## 2. Profile schema 變更

### 2.1 新欄位 `bootloader_prompts`

```yaml
# profiles/<vendor>.yaml
prompt_regex: "^# $"
bootloader_prompts:                  # 新增；opt-in；預設 []
  - "^=> $"
  - "^Marvell>> $"
  - "^BCM\\d+>> $"
```

- 型別：`list[str]`，每元素 regex（與 `prompt_regex` 同 flavor）。
- 與 `prompt_regex` 並存；同時匹配 OS 與 bootloader 時取 BOOTLOADER（更安全）。
- `bootloader_prompts == []` → 此 profile 不識別 bootloader、行為同今日。

### 2.2 文件位置

- profile schema 文件：`docs/serialwrap-spec.md`（profile 章節）。
- 第一波 vendor profile 至少補：BGW720（Broadcom CFE）+ 一個 Marvell 平台（若 repo 已有）。

## 3. `SessionManager.self_test` 行為

### 3.1 新分類分支

`session_manager.py:1738` 附近的 ATTACHED 區塊：

```python
if session.state == "ATTACHED":
    if profile.platform == "passthrough":
        classification = "PASSTHROUGH"
        recommended_action = "console_attach"
    elif session.last_error == "LOGIN_REQUIRED":
        classification = "LOGIN_REQUIRED"
        recommended_action = "console_attach"
    elif session.last_error == "REBOOTING":
        classification = "REBOOTING"
        recommended_action = "wait_or_console_attach"
    elif _matches_any_bootloader_prompt(rx_tail, profile.bootloader_prompts):
        classification = "BOOTLOADER"
        recommended_action = "recover_interactive"
    else:
        classification = "ATTACHED_NOT_READY"
        recommended_action = "console_attach"
```

`_matches_any_bootloader_prompt(rx_tail, patterns)`：對 `rx_tail` 最後一行做
逐個 regex 匹配；命中即返回該 pattern。`rx_tail` 取自
`bridge.rx_tail(N)`（N=512，內部常數）。

### 3.2 結果 schema

`BOOTLOADER` classification 結果：

```jsonc
{
  "ok": true,
  "classification": "BOOTLOADER",
  "matched_prompt": "^=> $",
  "rx_tail": "...\n=> ",
  "recommended_action": "recover_interactive",
  "session": { ... },
  "interactive_owner": "human:xxxx" | "agent" | null,
  "human_attached": true | false,
  "recovery_mode": true | false        // 若已有 recovery lease 則 true
}
```

其他 classification 仍含既有欄位 + `recovery_mode`（多數情況為 false）。

## 4. `interactive_open` 放寬

### 4.1 Signature

```python
def interactive_open(
    self,
    selector: str,
    *,
    owner: str = "agent",
    timeout_s: float = 60.0,
    command: str = "",
    allow_attached: bool = False,    # 新增
) -> dict[str, Any]:
```

### 4.2 Gate 邏輯

```
session 不存在 / bridge=None              → SESSION_NOT_READY
session.state == "READY"                  → 走原路徑（不變）
session.state == "ATTACHED" 且 allow_attached:
    bridge.snapshot() 不健康               → SESSION_NOT_READY
    重跑 _matches_any_bootloader_prompt:
        未命中                             → SESSION_NOT_READY (error_detail: NOT_BOOTLOADER)
        命中                               → 開 lease（recovery_mode=True）
其他 state                                → SESSION_NOT_READY
```

匹配在 `_lock` 內進行，與 `_open_interactive_locked` 同一原子區，避免
self_test → interactive_open 之間 target 已走出 U-Boot 的 race。

### 4.3 Lease lifecycle（stash-and-restore）

`attach_console` 會替 human 開出 session-layer lease（`session_manager.py:1536-1541`），
所以 recovery 流程不能只動 bridge-layer ownership——必須把 human 的 session-layer
lease 從 `self._interactive` 暫時移除（stash），recovery close 時還原。否則後續
`_refresh_interactive_locked` 會發現 bridge `interactive_owner` 不再是 human、
把 lease 視為失效並 close。

```python
# allow_attached=True 開 lease 流程（_lock 內）
existing = self._refresh_interactive_locked(session)
if existing is not None:
    if existing.owner.startswith("human:"):
        # stash human lease
        self._interactive.pop(existing.interactive_id, None)
        session.interactive_session_id = None
        session._stashed_human_lease = existing
        session.bridge.suspend_interactive()
        suspended_human = True
    else:
        return SESSION_INTERACTIVE_BUSY     # agent lease 已存在，不允許 recovery
else:
    suspended_human = False

timeout_s = min(timeout_s, MAX_RECOVERY_LEASE_S)
lease = self._open_interactive_locked(
    session,
    owner="agent",
    timeout_s=timeout_s,
    recovery_mode=True,
    suspended_human=suspended_human,
)
return lease

# _close_interactive_locked（recovery lease）
lease = self._interactive.pop(interactive_id, None)
session.interactive_session_id = None
session.bridge.set_interactive_owner(None)

if lease.suspended_human:
    session.bridge.resume_interactive()     # 既有實作 flush deferred buffer
    stashed = session._stashed_human_lease
    if stashed is not None and not stashed.expired():
        client_id = stashed.owner.split(":", 1)[1]
        if session.bridge.console_has_external_peer(client_id):
            self._interactive[stashed.interactive_id] = stashed
            session.interactive_session_id = stashed.interactive_id
            session.bridge.set_interactive_owner(stashed.owner)   # 與 resume 結果冪等
    session._stashed_human_lease = None
```

`SessionRuntime` dataclass 新增 `_stashed_human_lease: InteractiveLease | None = None`。

### 4.4 RPC / CLI

- `service.py` `session.interactive_open` 從 `params.get("allow_attached", False)` 讀，傳給 `SessionManager.interactive_open`。
- `cli.py` `session interactive-open` 子命令加 `--allow-attached` flag（`store_true`，default False），寫入 RPC params。
- `--help` 文字明寫：「在 ATTACHED + bootloader 子狀態下開啟 recovery interactive lease；human lease 會被自動 suspend 並在 close 時 resume」。

## 5. `InteractiveLease` 變更

`session_manager.py` 的 `InteractiveLease` dataclass 新增：

```python
@dataclass
class InteractiveLease:
    ...
    recovery_mode: bool = False
    suspended_human: bool = False
```

snapshot / `interactive_status` / `_lease_context()` 加上 `recovery_mode`：

```python
def _lease_context(self, lease):
    return {
        "interactive_owner": lease.owner if lease else None,
        "human_attached": bool(lease and lease.owner.startswith("human:")),
        "recovery_mode": bool(lease and lease.recovery_mode),
    }
```

## 6. 常數

`sw_core/constants.py` 新增：

```python
MAX_RECOVERY_LEASE_S: float = 120.0
BOOTLOADER_RX_TAIL_BYTES: int = 512
```

## 7. paulsha-conventions Bootstrap

### 7.1 必新增檔

| 路徑 | 必要欄位 |
|---|---|
| `.paul-project.yml` | `policy_profile: flat`、`policy_version: 1.0.0`、`code_paths`（見 §7.2）、`cli`（見 §7.3） |
| `VERSION` | `0.0.1`（對齊既有 git tag `v0.0.1` / R-07） |
| `CHANGELOG.md` | Keep-a-Changelog 1.1.0；`[Unreleased]` 段含本 PR 三條 entry |
| `CLAUDE.md` | managed-by marker + policy_version: 1.0.0 + 本地化 checklist |
| `AGENTS.md` | 與 `CLAUDE.md` 同內容（首行 marker 不同） |
| `GEMINI.md` | 與 `CLAUDE.md` 同內容 |
| `.github/pull_request_template.md` | conventions 標準 template，含 R-11 checklist |
| `.github/workflows/policy-check.yml` | 雙 pin 到 `hamanpaul/paulsha-conventions@ff1a031172ec24fc155699f9f3ce5bdea24d9e24` |

### 7.2 `.paul-project.yml.code_paths`

```yaml
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
```

### 7.3 `.paul-project.yml.cli`（R-16）

第一版至少宣告 `serialwrap --help` 一條，標 marker `serialwrap-help`：

```yaml
cli:
  - command: "./serialwrap"
    help_args: ["--help"]
    reflected_in: "README.md"
    marker: "serialwrap-help"
```

更多 subcommand（`session self-test --help`、`session interactive-open --help` 等）
若 help 文字仍會在本 PR 內變動（新加 `--allow-attached`），可在 PR 最後階段一次補。

### 7.4 既有 `.github/copilot-instructions.md`

首行加：

```markdown
<!-- managed-by: hamanpaul/paulsha-conventions@v1.0.0 -->
<!-- 若修改此檔，同步更新 CLAUDE.md / AGENTS.md / GEMINI.md / .github/copilot-instructions.md 四份 -->
policy_version: 1.0.0
```

其餘 11KB 既有內容保留；conventions 不要求覆蓋現有 instructions、只要求
marker / policy_version 一致（R-13 / R-14）。

### 7.5 README.md 段落（R-02）

確認既有 README 含：

- `## Install`
- `## Usage`
- `## Version`

缺則補 heading + 一段引用既有內容的指向（不重寫既有 ~34KB 內容）。
若 R-16 啟用，`## Usage` 內加 `<!-- BEGIN: cli-help marker="serialwrap-help" -->`
… `<!-- END: cli-help marker="serialwrap-help" -->` 區塊。

### 7.6 `.github/workflows/policy-check.yml`

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

## 8. CHANGELOG `[Unreleased]` 條目（本 PR）

```markdown
## [Unreleased]

### Added
- **bootloader_prompts profile schema**：新增 `list[str]` 欄位，宣告 bootloader prompt regex（U-Boot / Marvell / Broadcom CFE 等）。預設 `[]` 維持向後相容。
- **session.self_test BOOTLOADER classification**：ATTACHED 狀態下若 RX tail 命中 `bootloader_prompts`，回傳 `classification: "BOOTLOADER"` + `recommended_action: "recover_interactive"` + `matched_prompt` + `rx_tail`。
- **interactive_open `allow_attached` 入參**：在 BOOTLOADER 子狀態下放寬 READY-only gate；自動 suspend human interactive、close 時 resume。CLI 同步加 `--allow-attached` flag。
- **InteractiveLease `recovery_mode` / `suspended_human` 欄位**：snapshot / interactive_status / self_test lease_context 透出 `recovery_mode`。
- **MAX_RECOVERY_LEASE_S=120s clamp**：recovery lease 強制逾時上限，避免 agent 無限期持有並 suspend 人類觀察者。
- **OpenSpec change package**：`openspec/changes/2026-05-07-bootloader-recovery-44/`（proposal / design / tasks / specs）。
- **Brainstorming narrative spec**：`docs/superpowers/specs/2026-05-07-issue-44-bootloader-recovery-design.md`。

### Changed
- **adopt paulsha-conventions v1.0.0**：本 repo 首次接入 policy engine（policy_profile=`flat`、policy_version=`1.0.0`）。新增 `.paul-project.yml`、`VERSION`、`CHANGELOG.md`、`CLAUDE.md`、`AGENTS.md`、`GEMINI.md`、`.github/pull_request_template.md`、`.github/workflows/policy-check.yml`；既有 `.github/copilot-instructions.md` 加 managed-by marker 與 policy_version 段。
- **README.md**：補齊 `## Install` / `## Usage` / `## Version` 必備段落（R-02）。

### Notes
- 行為層 BREAKING：實機 ATTACHED 狀態若 profile 宣告 `bootloader_prompts` 且 RX tail 命中，self_test 會回 `BOOTLOADER` 而非 `ATTACHED_NOT_READY`；強依賴 `ATTACHED_NOT_READY` 字串的 caller 需擴展處理。
```

## 9. 不變式 / 風險（合約面）

- `interactive_owner` 物理意義：仍是 `_handle_console_rx` 的 raw passthrough 對象；recovery 期間 bridge-layer owner 透過 `_suspended_owner` 暫存。
- `READY` 契約：READY 仍代表「probe 通過、prompt 在 OS shell」；recovery lease 只在 ATTACHED 下成立。
- recovery lease 起點限制：呼叫 `interactive_open(allow_attached=True)` 時，session 既有 lease 必須是「無」或「`human:*`」其一；既有 lease 為 agent 時仍回 `SESSION_INTERACTIVE_BUSY`（不允許 agent 自我搶 lease）。
- session-layer lease 唯一性：`self._interactive` 在任一時刻最多含 1 個 lease。recovery 期間 human 的 session-layer lease 暫存於 `session._stashed_human_lease`（不在 `_interactive` 中），所以 `_refresh_interactive_locked` 不會誤判為失效。
- `MAX_RECOVERY_LEASE_S` 為內部常數而非 RPC param；改值需 PR 改 constant，避免 caller 自行 raise cap。
- stashed human lease 在 recovery 期間 expire：等同 human 自然 timeout；recovery close 時若 stash expire 直接丟棄，session 回到「無 lease」狀態。
