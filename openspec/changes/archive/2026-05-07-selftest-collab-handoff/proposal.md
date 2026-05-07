# Proposal: self_test 與 human-monitor 協作（Issue #42）

## Why

`session.self_test` 在偵測到 human attach console（lease owner 以 `human:` 開頭）時會 short-circuit 回 `HUMAN_INTERACTIVE_ACTIVE` + `recommended_action: wait_or_detach_console`，但 `command.submit` / `file.push` / `file.pull` 早已支援 agent 自動 `suspend_interactive` / `resume_interactive` 的協作模式。導致 controller 看到 self_test 結果就放棄接管，與系統實際能力不符，破壞 reboot-test 等「人類觀察 + agent 控制」雙軌情境。

## What Changes

- `SessionManager.self_test`：移除 human-lease short-circuit，改為走完 device → bridge → vtty → state → probe 完整流程。
- 新增 `strict_human_lock: bool = False` 參數（`session.self_test` RPC、`session self-test` CLI 同步暴露）。`True` 時保留舊 short-circuit 行為。
- 所有 self_test result（不分 classification）增補 `interactive_owner: str | None` 與 `human_attached: bool` 欄位，讓呼叫者仍能感知 human 在場。
- self_test 在實際送 `ready_probe` 階段，若 human lease 存在，自動以 `bridge.suspend_interactive()` / `resume_interactive()` 包覆，與 command path 對齊，避免 probe 字元與 human typing 在 UART 上交錯。
- **BREAKING**（行為層）：預設模式下不再回傳 `HUMAN_INTERACTIVE_ACTIVE` classification；既有 caller 若強依賴此 classification，需顯式傳 `strict_human_lock=True`。

## Capabilities

### New Capabilities
- `session-selftest`: 首次以 openspec 形式為 `session.self_test` 建立 capability spec（baseline 取自既有 docs/serialwrap-spec.md §9.1），並引入 collaborative 行為。

### Modified Capabilities
（無——`openspec/specs/` 目前無 self_test baseline，本變更同時建立 baseline 與引入新行為。）

## Impact

- 影響程式：
  - `sw_core/session_manager.py`（`self_test` 函式體與 signature）
  - `sw_core/service.py`（`session.self_test` RPC handler 讀新 param）
  - `sw_core/cli.py`（`session self-test` 加 `--strict-human-lock` flag）
  - `sw_mcp/`（若 MCP `session_self_test` tool 描述提到舊分類，需同步）
- 影響測試：`tests/test_session_bind.py` 既有 `test_self_test_reports_human_interactive_active` 必須改寫，並新增 default-walkthrough / suspend-orchestration / non-OK paths 等案例。
- 影響文件：`docs/serialwrap-spec.md` §9.1 self_test 章節需更新分類列表、輸入/輸出 schema、新增 collaborative monitoring 段落。
- 不影響：UART RX path、event engine、WAL、login FSM、其他 RPC。
- 相容性：新增 opt-in flag 模式，呼叫方若不傳 `strict_human_lock` 即取得新行為；舊 strict 行為仍可取得。
