# 設計：ATTACHED / U-Boot fallback recovery 與 paulsha-conventions 導入（Issue #44）

> Brainstorming 產出的 narrative spec。實作面正式合約見
> `openspec/changes/2026-05-07-bootloader-recovery-44/`，本文件以可讀說明為主。

## 0. 背景

`session.self_test` 在 `ATTACHED_NOT_READY` 狀態下建議 `console_attach` 作為 fallback，但
實機驗證顯示：當 COM0 / COM1 已被 `human:*` 占用為 interactive owner 時，新 attach 進
來的 console（不論是 agent 或第二個 human）並不會自動取得 `interactive_owner: true`，
因此其 PTY 寫入不會抵達 UART——`reset\n` 等 raw 指令對 U-Boot 失效。唯一可行的目前
路徑是強制把人類觀察者踢掉，與 `selftest-collab-handoff`（issue #42）同期建立的
「人類觀察 + agent 控制」雙軌模型互相違背。

本設計提供一條保留 human ownership 的 recovery 通道，並順手把 repo 收進
`paulsha-conventions` policy（R-01 ~ R-16）治理。

## 1. 目標與非目標

**目標**

- 在 `ATTACHED` + bootloader 子狀態下，agent 可送任意 raw bytes 到 UART，而不需
  奪走 human 的 console ownership。
- daemon 主動辨識「target 已掉到 bootloader」，回給 agent 明確的 classification
  與證據（matched prompt、最近 RX tail），避免 agent 在 `ATTACHED_NOT_READY`
  下傻等。
- 最大化重用既有 `bridge.suspend_interactive()` / `resume_interactive()` 機制
  （已在 self_test、command path、file transfer 共用），不引入第二條
  ownership 模型。
- 把 repo 一次接進 paulsha-conventions（policy_profile=`flat`、policy_version=
  `1.0.0`）；本 PR merge 後 `python3 -m policy_check --repo .` 必須全綠。

**非目標**

- 不重新設計 lease / ownership 模型（option C，formal co-lease 在本次範圍外）。
- 不對 `interactive_open` 增加 OS 之外的 ready-state 概念；READY 仍代表 OS prompt
  通過 probe。
- 不做 U-Boot 互動的 high-level helper（例如 `recover_via_uboot()`）；agent 拿到
  raw 通道後自己處理 prompt。

## 2. 解法總覽

```
profile.bootloader_prompts: list[str]   # 新欄位，profile 宣告 bootloader prompt regex
        │
        ▼
session.self_test
   └── ATTACHED 路徑多一次 RX tail 比對
       命中 → classification = "BOOTLOADER"
              recommended_action = "recover_interactive"
              matched_prompt / rx_tail 帶在 result 上
        │
        ▼
session.interactive_open(selector, allow_attached=True, ...)
   └── 放寬 gate：ATTACHED + bridge alive + 當下重新匹配 bootloader_prompts
       若有 human lease：bridge.suspend_interactive()
       開出 agent lease（recovery_mode=true、suspended_human=true）
        │
        ▼
session.interactive_send / interactive_status   ← 完全重用既有 RPC
   plain / base64 / key 三種 encoding 都可
        │
        ▼
session.interactive_close
   └── lease.suspended_human=true → bridge.resume_interactive()
       human deferred buffer 一次性 flush 到 UART
```

關鍵不變式（與既有架構對齊）：

1. `interactive_owner` 物理意義不變：仍是 `_handle_console_rx` 的 raw passthrough
   對象。recovery 期間 owner 透過 `_suspended_owner` 暫存，與 self_test 共用。
2. `READY` 契約不變：READY 仍代表「probe 通過、prompt 在 OS shell」。recovery
   lease 只在 `state == "ATTACHED"` 下成立，且 lease snapshot 帶 `recovery_mode:
   true` 區分。
3. 不新增 RPC verb：擴充 `interactive_open` 的入參、`self_test` 的 classification
   即可。CLI 既有 `serialwrap session interactive-open` 加 `--allow-attached` flag。

## 3. Profile schema 與 bootloader 偵測

### 3.1 Profile 新欄位

```yaml
bootloader_prompts:
  - "^=> $"           # 標準 U-Boot
  - "^Marvell>> $"    # Marvell vendor U-Boot
  - "^BCM\\d+>> $"    # Broadcom CFE / vendor bootloader
```

- 型別：`list[str]`，每元素為一條 regex（與既有 `prompt_regex` 同 flavor）。
- 預設：`[]`（此 profile 不識別 bootloader，行為同今日）。
- 與 `prompt_regex`（OS）並存，互不覆蓋。

### 3.2 偵測時機

`SessionManager.self_test` 在 `session.state == "ATTACHED"` 的判斷區塊
（`sw_core/session_manager.py:1738` 附近）多一次比對：

```python
if session.state == "ATTACHED":
    if profile.platform == "passthrough":
        classification = "PASSTHROUGH"; recommended_action = "console_attach"
    elif last_error == "LOGIN_REQUIRED":
        classification = "LOGIN_REQUIRED"; recommended_action = "console_attach"
    elif last_error == "REBOOTING":
        classification = "REBOOTING"; recommended_action = "wait_or_console_attach"
    elif _matches_any_bootloader_prompt(rx_tail, profile.bootloader_prompts):
        classification = "BOOTLOADER"; recommended_action = "recover_interactive"
    else:
        classification = "ATTACHED_NOT_READY"; recommended_action = "console_attach"
```

`_matches_any_bootloader_prompt` 從 `bridge.rx_tail(N)` 取最近 N 字元（N=512）做
regex 比對。N 為內部常數，不暴露為入參以避免 API 表面膨脹。

### 3.3 結果 schema

`BOOTLOADER` classification 的 result 在既有 `lease_context` 欄位之外，新增：

```jsonc
{
  "ok": true,
  "classification": "BOOTLOADER",
  "matched_prompt": "^=> $",          // 命中的 regex
  "rx_tail": "...\n=> ",              // clean_text(bridge.rx_tail(512))
  "recommended_action": "recover_interactive",
  "session": { ... },
  "interactive_owner": "human:xxxx",
  "human_attached": true
}
```

### 3.4 邊界情況

- `bootloader_prompts == []` → 跳過此檢查、行為完全不變（向後相容）。
- 同時匹配 `prompt_regex`（OS）與 `bootloader_prompts` → 取 BOOTLOADER（更安全：
  寧可開 recovery 路徑，不要誤判為 OS shell）。
- RX buffer 為空 → 不匹配，落回 `ATTACHED_NOT_READY`。

## 4. `interactive_open` 放寬與 lease lifecycle

### 4.1 入參擴充

```python
def interactive_open(
    self,
    selector: str,
    *,
    owner: str = "agent",
    timeout_s: float = 60.0,
    command: str = "",
    allow_attached: bool = False,   # 新增
) -> dict[str, Any]:
```

RPC 層 `service.py` 從 `params.get("allow_attached", False)` 讀取；CLI 加
`--allow-attached` flag。

### 4.2 新 gate 邏輯

取代 `session_manager.py:1572` 的單一 READY 檢查：

```
session 不存在 / bridge=None        → SESSION_NOT_READY
session.state == "READY"            → 走原路徑（不變）
session.state == "ATTACHED" 且 allow_attached:
    bridge.snapshot() 不健康         → SESSION_NOT_READY
    重跑一次 bootloader 偵測:
        未命中 bootloader_prompts    → SESSION_NOT_READY (error_detail: NOT_BOOTLOADER)
        命中                         → 繼續開 lease
其他 state                          → SESSION_NOT_READY
```

關鍵：**`allow_attached=True` 不等於「可以隨意開」**。仍要當下重新匹配 bootloader
prompt，避免 race（self_test 跟 interactive_open 之間 target 已走出 U-Boot）。
匹配在 `_lock` 內做，與 lease 建立同一原子區。

### 4.3 Lease 建立（stash-and-restore 機制）

`attach_console` 會替 human 開出 session-layer lease（`session_manager.py:1536-1541`，
owner=`"human:<client_id>"`）。recovery 不能用 self_test 的「純 bridge-layer
suspend」模式，因為 session-layer 仍有 active lease，後續 `_refresh_interactive_locked`
會發現 bridge `interactive_owner` 不再是 human、把 lease 視為失效並 close 掉。

正確機制：**stash human lease**。

```
allow_attached=True 路徑：
    若 session 既有 lease：
        owner.startswith("human:"):
            stash 該 lease：從 self._interactive pop、塞進 session._stashed_human_lease
            session.interactive_session_id = None
            bridge.suspend_interactive()       # 同 self_test 機制
            繼續開 recovery lease
        其他（owner=agent 等）:
            回 SESSION_INTERACTIVE_BUSY（既有保護不變）
    無既有 lease：
        繼續開 recovery lease（recovery_mode=True、suspended_human=False）

_open_interactive_locked(..., recovery_mode=True):
    建出 InteractiveLease（owner="agent"、recovery_mode=True、suspended_human=<是否 stashed>）
    bridge.set_interactive_owner("agent")
    若有 stashed human lease：lease.suspended_human = True
```

`SessionRuntime` 新增欄位 `_stashed_human_lease: InteractiveLease | None`，預設 None。
此欄位只在 recovery lease 存在時非 None；recovery close 時恢復。

### 4.4 `InteractiveLease` 新欄位

- `recovery_mode: bool`（預設 False；`allow_attached=True` 開出來的 lease 為 True）。
- `suspended_human: bool`（建立時是否呼叫 `suspend_interactive`；決定 close 時要
  不要 resume）。

### 4.5 Close 路徑

`interactive_close` / lease expire 路徑：

```
_close_interactive_locked(session, interactive_id=interactive_id):
    從 self._interactive pop recovery lease
    若 lease.suspended_human:
        bridge.resume_interactive()                   # 自動 replay deferred buffer
        從 session._stashed_human_lease 取出 stashed
        若 stashed 仍未 expire 且 bridge.console_has_external_peer(human_client_id):
            self._interactive[stashed.interactive_id] = stashed
            session.interactive_session_id = stashed.interactive_id
            bridge.set_interactive_owner(stashed.owner)   # bridge 已透過 resume 還原；此處冪等
        否則：丟棄 stashed（human 已離開）
        session._stashed_human_lease = None
回傳 ok
```

`bridge.resume_interactive` 既有實作會把 human deferred bytes flush 回 UART
（uart_io.py:609-625）；此處只是把 session-layer lease 也跟著還原。

### 4.6 逾時 cap

`allow_attached=True` 開出的 lease 強制 `timeout_s ≤ MAX_RECOVERY_LEASE_S`
（120s，定義在 `sw_core/constants.py`）。超過直接 clamp 並在 result 內 echo
實際 timeout。理由：recovery 期間 human 被 suspend，agent 不能無限期持有；
逾時後 lease 自動 expire（既有 `lease.expired()` 邏輯），expire 時走相同
close + resume 路徑。

### 4.7 Snapshot 補強

`interactive_status` 與 `self_test` 的 `lease_context` 多帶 `recovery_mode`：

```jsonc
{
  "interactive_owner": "agent",
  "human_attached": false,
  "recovery_mode": true
}
```

讓 agent 與 human-side observer 都能看到「目前在 recovery」。

### 4.8 邊界情況

- **agent 已有 lease + 又呼叫一次 `allow_attached=True`**：仍回
  `SESSION_INTERACTIVE_BUSY`（recovery 不可遞迴開、不可在 agent 已持有正常
  lease 時搶；只允許「無 lease」或「human lease」起點）。
- **human 在 recovery 期間 detach console**：`bridge.detach_console` 會把
  console client 移除；`_refresh_interactive_locked` 不會看到 stashed lease
  （因為已從 `_interactive` pop），所以 stash 不被誤清。recovery close 時
  檢查 `bridge.console_has_external_peer` → False → 丟棄 stashed lease，agent
  recovery 結束後 session 回到「無 lease」狀態。
- **stashed human lease 在 recovery 期間 expire**：`InteractiveLease.expired()`
  以 last touch + timeout_s 判斷；recovery close 時若 stashed 已 expire，丟棄
  即可（行為上等同 human 自然 timeout）。
- **recovery 期間 target 自己跳出 U-Boot 進到 OS**：agent 仍持 lease，可繼續送
  bytes。lease close 時 resume 把 human deferred buffer flush，順序正確；
  下一次 self_test 應走 READY 路徑。
- **bridge 在 recovery 期間掛掉**：既有 `_on_bridge_down` 流程觸發、recovery
  lease 與 stashed lease 都應隨 session 失效流程清乾淨；deferred buffer 隨
  bridge 銷毀（既有行為）。

## 5. 測試策略

### 5.1 單元測試（`tests/`）

對齊 `openspec/specs/session-selftest/spec.md` 風格，新加 spec scenarios：

1. `self_test classifies BOOTLOADER when bootloader_prompts matches RX tail`
   - profile 帶 `bootloader_prompts=["=> $"]`、bridge.rx_tail 餵 `"...\n=> "`、
     state=ATTACHED → `classification == "BOOTLOADER"`、
     `recommended_action == "recover_interactive"`、
     `matched_prompt == "=> $"`、`rx_tail` 含 `"=> "`。

2. `self_test falls back to ATTACHED_NOT_READY when bootloader_prompts is empty`
   → `ATTACHED_NOT_READY`（向後相容驗證）。

3. `self_test prefers BOOTLOADER over ATTACHED_NOT_READY when both could apply`
   - rx_tail 同時含 OS 樣字串與 bootloader prompt → BOOTLOADER。

4. `interactive_open with allow_attached=False rejects ATTACHED state`
   - state=ATTACHED → `SESSION_NOT_READY`（向後相容）。

5. `interactive_open with allow_attached=True rejects ATTACHED if no bootloader match`
   - rx_tail 無匹配 → `SESSION_NOT_READY`（`error_detail: NOT_BOOTLOADER`）。

6. `interactive_open with allow_attached=True opens recovery lease in BOOTLOADER`
   - 無 human lease → ok=True、`recovery_mode=true`、
     `suspend_interactive` **未被呼叫**。

7. `interactive_open recovery stashes human lease and restores on close`
   - 有 human lease（透過 `console-attach` 開）+ ATTACHED + bootloader 命中 →
     開 recovery lease 時：human lease 被 pop 出 `_interactive` 並 stash 到
     `session._stashed_human_lease`、`bridge.suspend_interactive()` 呼叫一次。
     `interactive_close` 時：`bridge.resume_interactive()` 呼叫一次（flush
     deferred buffer，斷言 `bridge.send_bytes(source="human:...",
     payload=<deferred>)` 被呼叫）、stashed lease 還原回 `_interactive`、
     `session.interactive_session_id` 指向 human lease。
   - 7a：既有 lease owner 為 `agent`（非 human） → recovery 開啟回
     `SESSION_INTERACTIVE_BUSY`、stash 不被建立。
   - 7b：stash 在 recovery 期間 expire → close 時丟棄、session 回到無 lease。
   - 7c：human 在 recovery 期間 detach console → close 時
     `console_has_external_peer=False`、stash 丟棄。

8. `interactive_send during recovery writes raw bytes`
   - `data="reset\n"` (encoding=plain) → `bridge.send_bytes(b"reset\n",
     source="agent")` 被呼叫；`data="ctrl-c"` (encoding=key) → `b"\x03"`。

9. `recovery lease enforces MAX_RECOVERY_LEASE_S cap`
   - `interactive_open(allow_attached=True, timeout_s=600)` →
     回傳 lease.timeout_s == 120。

10. `recovery lease auto-expires resumes human`
    - 把 lease.timeout_s 設小、模擬時間流逝、呼叫 `interactive_send` →
      觸發 expired 路徑、`resume_interactive` 被呼叫、回傳 `INTERACTIVE_EXPIRED`。

11. `recovery lease snapshot exposes recovery_mode in interactive_status and self_test lease_context`.

### 5.2 整合 / fixture 測試（`func-test/`）

用既有 fake-target / pty fixture 模擬 U-Boot prompt：

- 開 attach → human attach console → fake target 卡在 `=> ` →
  跑 self_test 驗 BOOTLOADER → agent 開 recovery lease → 送 `reset\n` →
  fake target 切換到 OS prompt → close lease → 驗 human deferred buffer 已 flush。

### 5.3 回歸

跑既有 selftest-collab-handoff 全部 scenarios（特別是 `strict_human_lock`、
suspend/resume 的 finally 路徑），確認新的 `BOOTLOADER` 分支不破壞。

### 5.4 OpenSpec 規格更新

把 1, 3, 4, 5, 6, 7, 9 寫進 `openspec/specs/session-selftest/spec.md` 的 ADDED
Requirements（與既有 selftest scenario 同格式）；lease / interactive_open 新行為
寫進新建 capability `openspec/specs/session-interactive/spec.md`（先在 change
package 內，archive 時搬到 specs/）。

### 5.5 手動驗證（實機）

- 板子敲進 U-Boot；掛 `serialwrap session console-attach` 當 human 觀察者。
- `serialwrap session self-test` → 預期 `classification: BOOTLOADER`。
- 用 client 開 `interactive-open --allow-attached` → 拿到 interactive_id。
- `interactive-send` 連送 `printenv\n`、`reset\n`，觀察 human console：agent
  動作期間 human 鍵盤輸入沉默，close 後一次補上去。
- 確認 agent 與 human 都沒看到 ownership 被搶走的錯誤訊息。

## 6. paulsha-conventions 導入（bootstrap）

把 #44 設計與 conventions bootstrap 包成同一個 PR；conventions 的 R-01 ~ R-16
規則一次到位，#44 spec 直接落在 conventions 目錄結構裡。

### 6.1 Bootstrap 檔清單

| 檔 | 內容 / 來源 | 對應 rule |
|---|---|---|
| `.paul-project.yml` | `policy_profile: flat`、`policy_version: 1.0.0`、`code_paths: ["sw_core/**", "sw_mcp/**", "tools/**", "tests/**"]`、`cli:` 列出 `serialwrap --help` 與 `serialwrap session self-test --help` 等對外 entry 與 marker | R-08 |
| `VERSION` | `0.0.0`（baseline；merge 後第一個 release 才升） | R-05 / R-06 |
| `CHANGELOG.md` | Keep-a-Changelog 1.1.0；`[Unreleased]` 段含本 PR 條目 | R-03 / R-04 / R-09 |
| `CLAUDE.md` / `AGENTS.md` / `GEMINI.md` | 直接複製 paulsha-conventions 對應檔，調整本地測試指令（`python3 -m pytest -q tests/`）；首行 `<!-- managed-by: hamanpaul/paulsha-conventions@v1.0.0 -->`；`policy_version: 1.0.0` | R-13 / R-14 |
| `.github/copilot-instructions.md` | 既有檔已存在；補首行 `managed-by` marker、`policy_version: 1.0.0` 段，與其他三份 agent file 同步 | R-13 / R-14 |
| `.github/pull_request_template.md` | conventions PR template；含 R-11 必勾項 | R-11 |
| `.github/workflows/policy-check.yml` | reusable caller workflow，雙 pin 到 `hamanpaul/paulsha-conventions@ff1a031172ec24fc155699f9f3ce5bdea24d9e24`；`policy_profile: flat`、`policy_version: 1.0.0`、`policy_engine_ref` 同 SHA | R-15 |

四份 agent convention file 必須同步（R-13 / R-14）；用同一份內容、各檔僅在開頭多一行
`<!-- managed-by ... -->` marker。

### 6.2 README.md 必備段落（R-02）

既有 README ~34KB；確認有 `## Install` / `## Usage` / `## Version` 三段。
缺則補 heading + 一段引用既有內容的指向，不重寫。

### 6.3 Change package layout

放棄「`docs/superpowers/specs/<date>-design.md` 單檔」路徑，改用 conventions /
既有 selftest-collab-handoff 的 OpenSpec 格式：

```
openspec/changes/2026-05-07-bootloader-recovery-44/
├── proposal.md
├── design.md          # 對應本文件 §2-§4 的合約面
├── tasks.md           # 含 bootstrap、design 落地、測試補齊、CHANGELOG entry
└── specs/
    ├── session-selftest/
    │   └── spec.md    # ADDED: BOOTLOADER classification、bootloader_prompts
    └── session-interactive/
        └── spec.md    # ADDED: allow_attached / recovery_mode / suspend-resume
                       #（新建 capability；archive 時搬到 openspec/specs/）
```

`docs/superpowers/specs/2026-05-07-issue-44-bootloader-recovery-design.md`
（本文件）保留為 brainstorming narrative spec，連結到 OpenSpec change 目錄；
與既有 `docs/design-event-trigger.md` 慣例一致，不違反 conventions。

### 6.4 Branch / commit / PR 規範

- branch：`feature/bootloader-recovery-44`（R-12 from main 要求 `feature/*`）。
- commit：conventional-commit 格式。
- PR title：`feat(session): add bootloader recovery interactive lease (#44)`（R-10）。
- PR body：用 `.github/pull_request_template.md`，所有 checkbox 勾滿（R-11）。
- CHANGELOG `[Unreleased]` 至少含三條：
  1. `feat(session)` — bootloader recovery via `interactive_open(allow_attached=True)` + profile `bootloader_prompts`。
  2. `chore(policy)` — adopt paulsha-conventions v1.0.0 (R-01 ~ R-16 baseline)。
  3. `docs` — OpenSpec change package for issue #44。

### 6.5 Merge 前 gate

1. `python3 -m pip install --disable-pip-version-check
   git+https://github.com/hamanpaul/paulsha-conventions.git@ff1a031172ec24fc155699f9f3ce5bdea24d9e24`
   安裝 policy engine。
2. `python3 -m policy_check --repo .` 全綠（特別注意 R-09：CHANGELOG 必須對應
   code 變動；R-13 / R-14：四份 agent file 同步且 policy_version 一致）。
3. `python3 -m pytest -q tests/` 全綠（既有 + §5.1 新增 11 條 + §5.2 fixture 案例）。
4. PR 上 GitHub Action `Policy Check` 綠燈（R-15 dual-pin 驗證）。

## 7. 風險與權衡

- **READY 契約被「擴張」**：`interactive_open` 多了 ATTACHED + bootloader 路徑。
  風險：caller 誤把 recovery lease 當 OS shell。緩解：必須顯式傳
  `allow_attached=True`、result 內 `recovery_mode=true`、self_test
  classification 明確為 `BOOTLOADER`、CLI flag 名 `--allow-attached`
  字面提醒。
- **profile 須維護 bootloader_prompts**：profile 不更新 → 仍走
  `ATTACHED_NOT_READY`、agent 仍卡。緩解：第一波 PR 同步把 BGW720 / Marvell /
  常見 vendor profile 補上 prompt（後續 PR 可擴）。
- **suspend_interactive 期間 human 觀察者體驗變化**：人類打字會被吞、close 後
  一次性 flush。已是 self_test / command path 的既有行為，agent 開
  recovery lease 沿用。文件需把「recovery_mode 中 human 鍵盤暫停」明寫到
  serialwrap-spec 與 README troubleshooting。
- **MAX_RECOVERY_LEASE_S=120s 過短時 agent 多 step U-Boot 流程會被打斷**：
  agent 可在 expire 前主動 `interactive_close` + 重新 `interactive_open`，每段
  ≤120s 可任意延長；如果普遍嫌短再以 PR 調 constant，不破壞 schema。
- **policy_check 第一次跑可能擋住既有 PR**：本 PR merge 後的下一個 PR 必須符合
  R-09 / R-15 等規則；CHANGELOG / agent file 維護成本由整個 repo 承擔。緩解：
  在 README / CONTRIBUTING（若有）說明流程，並把 conventions exemption label
  白名單列清楚。

## 8. 後續工作（不在本 PR 範圍）

- formal co-lease 模型（option C）：把 human / agent ownership 統一進
  `(human_lease, agent_lease)`，淘汰 `_suspended_owner`。
- recovery_mode 的高階 helper：`session.recover_via_uboot(reset=True)` 之類
  one-shot 行為（純 thin wrapper，不影響 schema）。
- `bootloader_prompts` 自動偵測（從 RX banner pattern 自動判斷未宣告
  prompt 的 profile，給 hint）。
