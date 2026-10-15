# P0 Stage 0 契約治理實作計畫（01-stage0-contracts）

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 凍結 serialwrap Python 基線的對外契約（`contracts/` 下 7 個規格檔、`code_extraction.yaml`／`diagnostic_aux.yaml`／`daemon_logging.yaml`／`wal_meta_events.schema.json` 4 個輔助檔，以及 `contracts/golden/` 下的位元組級 golden），建立「由程式碼抽出會輸出的碼 → 對照目錄 → 對照 SKILL 說明」的資料驅動驗證器（OD-06，不寫死任何錯誤碼清單），在 paulsha-conventions 本地分支新增對應的通用規則，並替既有 pytest 註冊與標註平台／資源 marker。

**Architecture:** 契約檔是權威（ADR-07），Python 基線與之後的 Rust 實作都對契約測試。驗證器放在 `tools/contracts/`，只用 stdlib＋PyYAML＋AST，**不 import `sw_core`**；它讀 `contracts/code_extraction.yaml` 宣告的 sink／relay／passthrough 規則，以 AST 做常數傳遞與跨函式（參數、回傳值）傳遞，得出「程式實際可能輸出的碼」，再與 `contracts/error_codes.yaml`、`contracts/session_last_errors.yaml` 及 SKILL 受管區段比對。計數一律由資料產生。SKILL 受管區段由目錄生成，以既有 R-26（`generated_facts`）防漂移；OD-06 通用規則在 paulsha-conventions 新增為 R-27，引擎 pin 升級前由本 repo pytest 以同語意本地強制。

**Tech Stack:** Python 3.10+（tests CI 3.12；policy CI 的 reusable workflow 為 3.11 且只裝引擎與 PyYAML）、pytest、PyYAML、`jsonschema==4.10.3`（**只在 `tests/` 使用**；`tools/contracts/**` 只得 import stdlib 與 PyYAML，因 R-26 會在 policy CI 執行 `python3 -m tools.contracts skill-section`；CI tests job 新增安裝）、JSON Schema Draft 2020-12、Python `ast`。本 Phase 不寫 Rust；產出的 YAML 欄位與列舉值依 `rust-architecture.md` §3.2 對應 P3 的 `sw-proto` 型別。

**Spec:** `docs/rust-refactor/serialwrap-rust-spec-final.md` §3.2、§4.2、§8.1、§9.1～§9.5、§10.1、§10.4、§11.2～§11.4、ADR-07、ADR-08；`docs/rust-refactor/owner-decisions-2026-10-15.md` OD-04、OD-06、OD-11、OD-13；`docs/rust-refactor/plans/00-master-plan.md`；`docs/rust-refactor/plans/rust-architecture.md` §1.3、§3.1、§3.2、§3.3、§3.5、§3.8、§7.4、§9；`docs/rust-refactor/plans/spec-reconciliation.md`（全表；本 Phase 承接標 P0 的列）；盤點 `docs/rust-refactor/inventory/01-cli.md`、`02-rpc.md`、`03-errors.md`、`04-wal-state.md`、`08-tests-assets.md`、`09-events-transfer.md`、`10-gaps.md`。

**工作目錄：** serialwrap 一律在 `feature/rust-refactor` 分支的 worktree 根目錄（下文記為 `$SW_ROOT`，執行者以 `SW_ROOT=$(git rev-parse --show-toplevel)` 在該 worktree 內求得；子任務可另開 `wt/rust-refactor/<subtask>` worktree）。下文路徑皆相對此根目錄，T13 除外（paulsha-conventions 另一 repo：其本機 checkout 記為 `$CONV_REPO`、T13 隔離 worktree 記為 `$CONV_WT`，兩者由執行者依本機佈局設定，計畫不寫死任何本機絕對路徑）。

---

## Global Constraints

（自 `00-master-plan.md` 摘錄本 Phase 相關者，另補本 Phase 專屬條款）

- RPC／CLI `error_code` 與 session `last_error` 為兩個獨立命名空間（ADR-07、§9.3）；`CREDENTIALS_UNRESOLVED` 只屬後者，不得成為 `error_codes.yaml` 的 entry。
- 錯誤碼計數由 `contracts/error_codes.yaml` 資料產生，不得硬編碼 78（§9.2、OD-06）。驗證器、R-27 規則與任何測試都不得以字面數字斷言目錄筆數。
- OD-06 通用規則不得內建任何錯誤碼清單；repo 以設定宣告「如何抽出會輸出的 code（含常數傳遞路徑）」與「說明文件位置」。
- `--retries` 只對 11 個唯讀方法重試：`health.ping`、`health.status`、`session.list`、`session.get_state`、`session.activity`、`session.console_list`、`session.log_status`、`device.list`、`alias.list`、`mcu.patterns`、`mcu.status`（§4.2）；`session.interactive_status` 絕不重試。契約檔必須如實反映。
- CLI JSON：`json.dumps(..., ensure_ascii=False, sort_keys=True, separators=(",", ":"))`＋`\n`；`state.json`／WAL 以 `sort_keys=True`；RPC wire 不排序（`rust-architecture.md` §3.3 四種 profile）。
- WAL rotate 門檻 `R=67108864`，條件 `size >= R`，寫入前檢查（`WAL-D7`）；`crc32` 為 8 位小寫 hex 字串（`WAL-D1`）。
- 已核准但未實作的碼（BC-03 `COMMAND_ABORTED_IN_FLIGHT`、SC-19 `TEST_DEVICE_ALLOWLIST_REJECTED`）只能以 `status: APPROVED` 登錄，不得列為生效；程式輸出未登錄的碼必須判 FAIL，不得從掃描結果隱藏（ADR-08）。`exclude_values` 只能排除「不是碼」或「中繼轉錄產物」，且每筆以 WARN 回報；不得排除任何目錄中已有的值。
- **狀態推進協定（P1 之後沿用）**：本 Phase 的測試不得以字面 `status == "APPROVED"` 凍結「核准未實作」狀態，一律以資料推導（T6 C16／C17）：碼所引用的變更全為 `APPROVED`／`PROPOSED` 時碼必須 `APPROVED`＋`reachable: false`；任一引用變更為 `IMPLEMENTED`／`VERIFIED` 時碼必須 `IMPLEMENTED`／`VERIFIED`＋`reachable: true`＋非空 `contract_tests`。`rpc_schema.json`／`cli_schema.json` 的 `x-pending` 每筆帶 `change`：變更仍 `APPROVED` 時執行期必須**不存在**該欄位／子命令；變更 `IMPLEMENTED` 以後該筆必須已併入正式欄位並自 `x-pending` 刪除，且與 status 翻轉在同一 commit。
- **revision 命名**：本 Phase 的整數版本一律稱 `catalog_revision`（各 YAML 頂層 `revision`、變更項 `approved_at_revision`、schema 擴充鍵 `x-catalog-revision`）。`contract_revision` 保留給 40 hex 的 `git rev-parse HEAD:contracts` tree SHA，只在 build／capture 時計算並記錄於 release manifest 或證據 JSON，**不得**寫入 `contracts/` 內任何檔（tree hash 無法存進它所雜湊的 tree）。
- marker 註冊、逐檔標註與稽核的唯一 owner 為本 Phase T14；P1～P9 新增的每個測試檔都必須帶符合 T14 規則的 `pytestmark`／class marker，否則 `tests/test_resource_markers.py` FAIL。
- `tools/contracts/**` 只 import stdlib＋PyYAML（T2 AST 測試強制），須在 Python 3.10～3.12 產生逐位元組相同的 `skill-section` 輸出；不得使用版本相依的 `ast.unparse` 文字作為比對鍵。
- 測試工具 `HARNESS_*` 原因碼不得混入產品目錄（ADR-08、§11.4）。
- 不得把未分類錯誤改成 `INTERNAL_ERROR` 來提高覆蓋率（§9.2）；本 Phase **不改任何產品行為**，Python 現況即契約（調和表 KEEP）。
- `tools/contracts/**` 不得 import `sw_core`（黑箱獨立性，§3.1、§11.3）；`tests/test_contracts_*.py` 中只有明確標為 L1 白箱的 schema 對照測試可 import `sw_core`。
- 修改契約、比較器或哨兵的 PR 需獨立審閱（§10.4）；不得以刪案例、改分母、FAIL 改 SKIP 取得全綠。
- repo 政策：不得 commit 到 `main`；每個動到 `code_paths` 的 PR 附 `changelog.d/<slug>.md`（frontmatter `type` 取自 `feat|fix|refactor|perf|change|docs|remove|deprecate|security`）；commit 用 Conventional Commits 繁中 subject，含 `Co-authored-by: Copilot <223556219+Copilot@users.noreply.github.com>` trailer；完成前跑 `python3 -m pytest -q tests/` 與 `python3 -m policy_check --repo .`。
- 文件、註解、YAML 說明文字一律繁體中文；識別字、碼值、欄位名逐字保留。

## Review Focus

1. 抽取器的常數傳遞是否完整：`ERROR_*` 匯入常數、參數預設值、呼叫點參數、回傳 tuple、`TunnelError(code)`、`last_error` 提升路徑，任何一條漏抓都會讓「程式會輸出、卻沒登錄」的碼逃過 OD-06。
2. 命名空間是否分離：`CREDENTIALS_UNRESOLVED` 不得進 `error_codes.yaml`；last_error 外流以單一 passthrough meta 項表示、不計入 RPC 碼數；`CMD_LENGTH_WARNING`／`NOT_BOOTLOADER` 只在輔助目錄。
3. 「已核准未生效」與「生效」的邊界：APPROVED 碼若被抽到即 FAIL；EXISTING 碼沒有契約測試即 FAIL；計數不得出現字面常數。
4. YAML 1.1 陷阱：未加引號的 `yes`／`no`／日期會被 PyYAML 轉成 bool／`datetime.date`，loader 必須明確拒絕，避免 P3 Rust 生成器讀到錯型別。
5. 資源 marker 不得高報：`core_portable` 項目不得有平台條件 SKIP，`windows_com` 只能標在真正以原生 serial backend 開 COM 的測試；本 Phase 在本環境不得宣稱任何 `windows_com`／`physical_uart` 證據；Windows 失敗不得以改標 `posix_only` 縮小分母。
6. 狀態推進不得被測試卡死、也不得被繞過：測試以資料推導 APPROVED／IMPLEMENTED 關係（C16／C17），`contract_revision`（tree SHA）不得寫入 `contracts/`；調和表每個 P0 列都有凍結位置（`p0_row_coverage.yaml`）。

---

## 執行順序（逐 Task red→green）

1. **逐 Task 依依賴順序 red→green**：順序為 T1→T2→T3→T4→T5→T6→T7→T8→T9→T10→T16→T11→T12→T13→T14→T15。每個 Task 先由測試作者完成 Step 1～3（寫失敗測試、確認紅燈、commit 測試），再由實作者完成 Step 4～7，之後才開始下一個 Task。這樣每個 Task 的紅燈都建立在前一個 Task 已實作的基礎上，可觀察到的是**預期的語意失敗**（例如 `CatalogError`、C09 finding），不是任意的 `ModuleNotFoundError`。
   - 每個 Task 的 Step 2 只接受該 Task 寫明的失敗原因；出現其他失敗（collection error、與本 Task 無關的 import error）時先修測試，不得當作紅燈已驗證。
   - 所有新測試模組**不得在模組頂層 import `tools.contracts.*`**，一律在函式內以 `_load()` 輔助函式延遲 import，讓 `python3 -m pytest -q tests/` 能完成 collection。
   - 可延後轉綠的 node（只有這些，其他 node 在所屬 Task 的 Step 5 必須 PASS）：`tests/test_contracts_catalog.py` 中依賴尚未建立之目錄檔的 `test_*_real_*`（分別在 T3／T4／T5 轉綠）；Phase 出口測試 `tests/test_contracts_validate.py::test_real_repo_contract_check_passes` **在 T12 Step 1 才新增**，T12 Step 5 起必須 PASS，因此 T6～T11 的 Step 6 全套結果不會被它遮蔽。
   - 本 Phase 的 commit 只留在 feature 分支，PR 不得在 Phase 出口條件全部成立前開出。
2. T14 必須在 T1～T13、T16 新增的測試檔都存在之後才實作（它要標註全部測試檔）；T15 的新測試檔建立時即帶 T14 規則的 marker。
3. 每個 Task 實作完成後，由審查 A（Claude Sonnet，effort max）與審查 B（Codex `gpt-6.1-sol`，reasoning medium）雙審；審查者不得為該 Task 的實作者（master plan §2）。T2 的審查 B 必須實際執行 T2 的 mutation 測試並在審查紀錄附輸出。

## 檔案總覽

| 路徑 | 動作 | Task |
|---|---|---|
| `tools/contracts/__init__.py`、`__main__.py`、`cli.py` | 新增 | T1、T6 |
| `tools/contracts/catalog.py` | 新增 | T1 |
| `tools/contracts/extract.py` | 新增 | T2 |
| `tools/contracts/validate.py` | 新增 | T6 |
| `tools/contracts/skill_section.py` | 新增 | T11 |
| `contracts/code_extraction.yaml` | 新增 | T2 |
| `contracts/error_codes.yaml` | 新增 | T3（T7 補 `contract_tests`） |
| `contracts/session_last_errors.yaml`、`contracts/diagnostic_aux.yaml` | 新增 | T4 |
| `contracts/behavior_changes.yaml` | 新增 | T5 |
| `contracts/rpc_schema.json` | 新增 | T8 |
| `contracts/golden/rpc/line_limit.json`、`scripts/stage0/capture_rpc_line_limit.py` | 新增 | T8 |
| `contracts/cli_schema.json` | 新增 | T9 |
| `contracts/wal_record.schema.json`、`contracts/wal_meta_events.schema.json`、`contracts/state_file.schema.json` | 新增 | T10 |
| `contracts/daemon_logging.yaml`、`contracts/golden/last_error/transport_stall_hint.json`、`scripts/stage0/capture_transport_stall_hint.py` | 新增 | T16 |
| `contracts/p0_row_coverage.yaml` | 新增（調和表 P0 列 → 凍結位置） | T15 |
| `tests/fixtures/r27_parity/*.yaml` | 新增（C13／R-27 共用對等 fixture） | T13 |
| `tests/test_contracts_keep_baseline.py` | 新增 | T16 |
| `docs/contracts-governance.md` | 新增（契約治理與 `python3 -m tools.contracts` 用法，R-18） | T12 |
| `contracts/release_manifest.schema.json` | **不屬本 Phase**：由 P1 T18（SC-25 Python 側）建立，見「下游計畫須同步的修訂」 | — |
| `sw_core/assets/skill/SKILL.md`、`sw_core/assets/skill/SKILL_WINDOWS.md` | 新增受管區段 | T11；T15 勘誤 |
| `.project-policy.yml` | 宣告 `diagnostic_codes`、`generated_facts`、`contracts/**`、preflight | T12 |
| `CLAUDE.md` | 契約治理段、marker 段、勘誤 | T12、T14、T15 |
| `.github/workflows/tests.yml` | pip 加 `jsonschema` | T8 |
| `tests/conftest.py` | `pytest_configure` 註冊 10 個 marker（marker 註冊的唯一定義處）；docstring 勘誤 | T14、T15 |
| `tests/test_*.py`（全部既有檔） | 加 `pytestmark`／class 級 marker | T14 |
| `tests/test_contracts_catalog.py` | 新增 | T1、T3、T4、T5 |
| `tests/test_contracts_extract.py` | 新增 | T2 |
| `tests/test_contracts_validate.py` | 新增 | T6 |
| `tests/test_contracts_gap_codes.py` | 新增 | T7 |
| `tests/test_contracts_rpc_schema.py` | 新增 | T8 |
| `tests/test_contracts_cli_schema.py` | 新增 | T9 |
| `tests/test_contracts_wal_state_schema.py` | 新增 | T10 |
| `tests/test_contracts_skill_section.py` | 新增 | T11 |
| `tests/test_contracts_policy_decl.py` | 新增 | T12 |
| `tests/test_resource_markers.py` | 新增 | T14 |
| `tests/test_contracts_doc_errata.py` | 新增 | T15 |
| `docs/rust-refactor/spec-errata.md` | 新增（規格勘誤清單，不改定稿本文）；**唯一建立者 T7**（建骨架與「盤點勘誤」節），T15 只追加 | T7、T15 |
| `README.md`、`docs/design-event-trigger.md`、`docs/serialwrap-spec.md`、`sw_core/file_transfer.py`（註解） | 勘誤 | T15 |
| `changelog.d/rust-p0-*.md` | 每 Task 一個 | 各 Task |
| `$CONV_WT/`（paulsha-conventions 另一 repo 的 `feature/od06-diagnostic-code-coverage` worktree） | R-27 | T13 |

## 共用介面（本 Phase 產出、後續 Phase 消費）

### Python：`tools/contracts`

```python
# tools/contracts/catalog.py
INTERFACES: Final[tuple[str, ...]] = ("rpc", "rpc_frame", "cli_client", "cli_local", "rec", "proc")
RETRYABLE: Final[tuple[str, ...]] = ("yes", "no", "conditional", "fix_args")
CODE_STATUS: Final[tuple[str, ...]] = ("EXISTING", "APPROVED", "IMPLEMENTED", "VERIFIED")
EFFECTIVE_STATUS: Final[frozenset[str]] = frozenset({"EXISTING", "IMPLEMENTED", "VERIFIED"})
CHANGE_STATUS: Final[tuple[str, ...]] = ("PROPOSED", "APPROVED", "IMPLEMENTED", "VERIFIED")
SESSION_STATES: Final[tuple[str, ...]] = ("DETACHED", "ATTACHING", "ATTACHED", "READY", "RECOVERING", "RELEASED", "FLASHING")
PLATFORMS: Final[tuple[str, ...]] = ("posix", "windows")
UART_TX: Final[tuple[str, ...]] = ("none", "possible", "confirmed")      # 回此碼時是否可能已有外部 UART TX（§11.3 不變量 #1／#5）
CHANGE_SCOPES: Final[tuple[str, ...]] = ("both", "rust_only", "python_when_manifest_present")
AB_PARTICIPATION: Final[tuple[str, ...]] = ("diff", "absolute_only")
APPROVAL_OWNER_BY_BASIS: Final[Mapping[str, str]] = {
    "owner_decision": "hamanpaul", "spec_final": "spec-final", "reconciliation": "spec-reconciliation"}
CLI_CONTEXTS: Final[tuple[str, ...]] = (   # exit_codes／x-exit-codes 的封閉 context 集合
    "_run_rpc", "daemon start", "daemon stop", "event", "connect", "benches", "remote", "setup",
    "service <action>", "cmd submit", "profile test", "doctor", "argparse-boundary", "fallthrough")
SAFETY_INVARIANTS: Final[tuple[str, ...]] = (   # §11.3 六項不變量；ID 與 P2 cases.yaml 的 invariants 欄一致
    "zero_tx_on_reject", "no_forbidden_interleave", "no_stale_context_replay",
    "no_reopen_before_handoff", "no_auto_resend", "insufficient_evidence_not_pass")

@dataclass(frozen=True)
class Scope:
    interfaces: tuple[str, ...]
    methods: tuple[str, ...]
    platforms: tuple[str, ...]

@dataclass(frozen=True)
class ErrorCodeEntry:
    code: str
    status: str
    reachable: bool
    unreachable_reason: str | None
    constant: str | None
    scope: Scope
    meaning: str
    side_effect: str
    retryable: str
    uart_tx: str                   # ∈ UART_TX
    ok_response: bool              # 此碼出現在 ok:true 回應（如 PROMPT_TIMEOUT_RECOVERED，ERR-7.1-e）；預設 false
    agent_action: str
    extra_fields: tuple[str, ...]
    exit_codes: Mapping[str, int]  # 鍵 ∈ CLI_CONTEXTS
    contract_tests: tuple[str, ...]
    behavior_change: tuple[str, ...]
    sources: tuple[str, ...]

@dataclass(frozen=True)
class PassthroughEntry:
    id: str
    namespace_ref: str
    scope: Scope
    rule: str
    owner_confirmation: str
    reconciliation_refs: tuple[str, ...]

@dataclass(frozen=True)
class LastErrorEntry:
    value: str
    kind: str                      # "named" | "dynamic_prefix"
    reason_forms: tuple[str, ...]
    reachable: bool
    unreachable_reason: str | None
    meaning: str
    agent_action: str
    terminal: bool                 # 只有操作者明確動作才會解除（cleared_by 無自動來源）且不自動重探
    auto_reprobe_attached: bool
    auto_reprobe_detached: bool
    manual_recovery: str
    logging: str
    detail: str
    states: tuple[str, ...]
    set_by: tuple[str, ...]
    cleared_by: tuple[str, ...]    # 盤點 03 §5.3 清除點，形如 "auto:<描述>@session_manager.py:<行>" 或 "operator:<RPC method>"
    promoted_via: tuple[str, ...]
    error_code_namespace: str      # "forbidden" | "allowed_shared"
    contract_tests: tuple[str, ...]

@dataclass(frozen=True)
class Approval:
    owner: str
    date: str
    decision_ref: str
    basis: str                     # "owner_decision" | "spec_final" | "reconciliation"

@dataclass(frozen=True)
class BehaviorChange:
    id: str
    kind: str                      # "BC" | "SC" | "DF"
    title: str
    status: str
    approval: Approval
    owner_confirmation: str        # "not_required" | "pending_default" | "confirmed"
    owner_item: int | None
    default_if_pending: str | None
    applies_python_min: str | None # 目標 release 版號（尚未發佈；bump 由 release PR 執行）
    applies_rust_stage: str | None
    approved_at_revision: int      # 核准時的 catalog_revision；不是 contract_revision（tree SHA）
    spec_refs: tuple[str, ...]
    reconciliation_refs: tuple[str, ...]
    scope: str                     # ∈ CHANGE_SCOPES
    ab_participation: str          # ∈ AB_PARTICIPATION；scope both→diff，其他→absolute_only
    phases: tuple[str, ...]
    case_ids: tuple[str, ...]
    compatibility_impact: str
    approval_conditions: str
    related_decisions: tuple[str, ...]

@dataclass(frozen=True)
class OwnerConfirmation:
    item: int
    topic: str
    refs: tuple[str, ...]
    default: str
    affects: tuple[str, ...]

@dataclass(frozen=True)
class SafetyInvariant:
    id: str                        # ∈ SAFETY_INVARIANTS
    title: str
    spec_ref: str                  # "§11.3"
    error_codes: tuple[str, ...]   # 與此不變量相關的碼（例 zero_tx_on_reject → uart_tx == none 的碼，由資料推導並核對）
    case_ids: tuple[str, ...]      # 本檔 changes 內已有、可作此不變量證據的 case_id（可空；P2 cases.yaml 以 invariants 欄補齊）

@dataclass(frozen=True)
class AuxCatalog:
    warning_codes: tuple[tuple[str, str], ...]               # (code, meaning)
    error_details: tuple[tuple[str, str, str], ...]          # (value, parent_code, meaning)
    classifications: tuple[tuple[str, tuple[str, ...], str], ...]   # (value, recommended_actions, meaning)；多值拆成 tuple
    recommended_actions: tuple[tuple[str, str], ...]         # (value, meaning)：全部 emission 點的 recommended_action 值
    recovery_actions: tuple[tuple[str, str], ...]
    recover_actions: tuple[tuple[str, str], ...]

@dataclass(frozen=True)
class Catalogs:
    error_codes: tuple[ErrorCodeEntry, ...]
    passthrough: tuple[PassthroughEntry, ...]
    last_errors: tuple[LastErrorEntry, ...]
    behavior_changes: tuple[BehaviorChange, ...]
    owner_confirmations: tuple[OwnerConfirmation, ...]
    safety_invariants: tuple[SafetyInvariant, ...]
    aux: AuxCatalog
    revisions: Mapping[str, int]   # 檔名 → catalog_revision

class CatalogError(Exception):
    problems: list[str]
    def __init__(self, problems: list[str]) -> None: ...

def validate_error_codes_doc(doc: object) -> list[str]: ...
def validate_session_last_errors_doc(doc: object) -> list[str]: ...
def validate_behavior_changes_doc(doc: object) -> list[str]: ...
def validate_aux_doc(doc: object) -> list[str]: ...
def load_yaml_strict(path: Path) -> object: ...       # 重複鍵、yaml.YAMLError、非 UTF-8 → CatalogError（PyYAML 預設會靜默保留最後一個重複鍵）
def load_error_codes(repo_root: Path) -> tuple[tuple[ErrorCodeEntry, ...], tuple[PassthroughEntry, ...]]: ...
def load_session_last_errors(repo_root: Path) -> tuple[LastErrorEntry, ...]: ...
def load_behavior_changes(repo_root: Path) -> tuple[tuple[BehaviorChange, ...], tuple[OwnerConfirmation, ...], tuple[SafetyInvariant, ...]]: ...
def load_aux(repo_root: Path) -> AuxCatalog: ...
def load_catalogs(repo_root: Path) -> Catalogs: ...   # 組合上列四個；任一檔有問題即 raise CatalogError（彙整全部問題）
```

```python
# tools/contracts/extract.py
@dataclass(frozen=True)
class SinkSpec:
    kind: str     # dict_key | subscript_store | call_keyword | param_default | call_arg | attribute_store
    key: str | None = None
    keyword: str | None = None
    param: str | None = None
    callee: str | None = None
    index: int | None = None
    attr: str | None = None
    value_pattern: str | None = None
    implies_interface: str | None = None

@dataclass(frozen=True)
class RelaySpec:
    kind: str     # call_method_arg | subscript_load | attribute_load
    method: str | None = None
    arg: str | None = None
    key: str | None = None
    attr: str | None = None

@dataclass(frozen=True)
class PassthroughSpec:
    kind: str     # call_method_arg | subscript_load | attribute_load
    emits: str
    method: str | None = None
    arg: str | None = None
    key: str | None = None
    attr: str | None = None

@dataclass(frozen=True)
class NamespaceSpec:
    name: str
    sinks: tuple[SinkSpec, ...]
    relays: tuple[RelaySpec, ...]
    passthrough: tuple[PassthroughSpec, ...]

@dataclass(frozen=True)
class AllowUnresolved:
    namespace: str
    function: str     # "<relpath>::<Class>.<func>" 或 "<relpath>::<func>"
    expr: str         # canon_expr(node)：版本無關的正規化字串（見 T2 語意 8），不得用 ast.unparse
    reason: str

@dataclass(frozen=True)
class ExcludeValue:
    namespace: str
    value: str
    category: str     # "not_a_code" | "relay_artifact"
    reason: str

def canon_expr(node: ast.AST) -> str: ...   # 遞迴輸出 "<NodeType>(<field>=<value>,...)"，欄位依 _fields 順序、略過 ctx／type_comment／kind 與空 list／None；常數以 repr

@dataclass(frozen=True)
class ExtractionConfig:
    roots: tuple[str, ...]
    exclude: tuple[str, ...]
    code_pattern: str
    dynamic_prefix_pattern: str
    max_depth: int
    namespaces: tuple[NamespaceSpec, ...]
    exclude_functions: tuple[str, ...]
    allow_unresolved: tuple[AllowUnresolved, ...]
    exclude_values: tuple[ExcludeValue, ...]

@dataclass(frozen=True)
class Emission:
    namespace: str
    value: str                     # "FOO"、"REPROBE_FAILED:*" 或 passthrough id
    kind: str                      # "code" | "dynamic_prefix" | "passthrough"
    file: str
    line: int
    function: str
    sink: str                      # SinkSpec.kind
    implies_interface: str | None

@dataclass(frozen=True)
class Unresolved:
    namespace: str
    file: str
    line: int
    function: str
    expr: str
    reason: str                    # unknown_name | unknown_callee | no_return_value | unsupported_expr | max_depth | cycle | malformed_code

@dataclass(frozen=True)
class ExtractionResult:
    emissions: tuple[Emission, ...]
    unresolved: tuple[Unresolved, ...]                 # 已扣除 allow_unresolved
    stale_allow_unresolved: tuple[AllowUnresolved, ...]
    stale_exclude_values: tuple[ExcludeValue, ...]
    applied_exclusions: tuple[tuple[ExcludeValue, int], ...]   # (排除項, 被扣除的 emission 數)；全部以 C19 WARN 回報
    def values(self, namespace: str, *, include_passthrough: bool = False) -> frozenset[str]: ...

class ExtractionConfigError(Exception): ...
def load_extraction_config(path: Path) -> ExtractionConfig: ...
def extract(repo_root: Path, config: ExtractionConfig) -> ExtractionResult: ...
```

```python
# tools/contracts/validate.py
@dataclass(frozen=True)
class Finding:
    check: str        # "C01".."C19"
    severity: str     # "FAIL" | "WARN"
    subject: str      # 碼值、檔案或 method
    message: str

def check_catalog_against_emissions(cat: Catalogs, res: ExtractionResult) -> list[Finding]: ...   # C02 C03 C04 C06
def check_extraction_hygiene(cat: Catalogs, res: ExtractionResult) -> list[Finding]: ...         # C07 C08 C19
def check_contract_tests(cat: Catalogs, repo_root: Path) -> list[Finding]: ...                   # C09
def check_cross_refs(cat: Catalogs) -> list[Finding]: ...                                        # C10 C11 C12 C16
def check_doc_coverage(values: Iterable[str], docs: Sequence[Path], entry_pattern: str,
                       require_in: str = "all") -> list[Finding]: ...                            # C13（R-27 等價）
def check_generated_facts(repo_root: Path, policy: Mapping[str, Any]) -> list[Finding]: ...      # C14（R-26 等價）
def check_schema_refs(cat: Catalogs, repo_root: Path) -> list[Finding]: ...                      # C15 C17 C18
def summarize(cat: Catalogs, res: ExtractionResult) -> dict[str, Any]: ...
def run_all(repo_root: Path) -> tuple[list[Finding], dict[str, Any]]: ...

# tools/contracts/skill_section.py
RETRYABLE_LABEL: Final[Mapping[str, str]] = {"yes": "是", "no": "否", "conditional": "視情況", "fix_args": "修正參數後"}
def render(cat: Catalogs, namespace: str) -> str: ...      # namespace ∈ {"error_code","session_last_error"}

# tools/contracts/cli.py
def main(argv: list[str] | None = None) -> int: ...
```

CLI（`python3 -m tools.contracts <sub>`，cwd＝repo 根目錄，`--repo` 預設 `.`）：

| 子命令 | 輸出 | exit |
|---|---|---|
| `extract --namespace {error_code,session_last_error} [--format lines\|json]` | `lines`：排序後每行一個值（不含 passthrough）；`json`：emission 物件陣列（`DISK_STABLE` 序列化） | 0；設定錯誤 1（stderr 一行 `contracts: <msg>`） |
| `check [--only C02,C09,...]` | `{"findings":[...],"ok":bool,"summary":{...}}`（`ensure_ascii=False, sort_keys=True, separators=(",",":")`＋`\n`） | 無 FAIL 為 0，否則 1 |
| `summary` | `summarize()` 結果，同上序列化 | 0 |
| `skill-section --namespace {error_code,session_last_error}` | `render()` 結果＋`\n` | 0 |
| `effective --namespace error_code` | 生效集合＝抽出值 ∩ `status ∈ EFFECTIVE_STATUS`，排序每行一個 | 0 |

### Rust：P3 依本 Phase 資料產生的型別（本 Phase 不寫 Rust，只保證 YAML 可無損映射）

```rust
// crates/sw-proto/src/error_codes.rs（P3 由 scripts/rust/gen_error_codes.py 依 contracts/error_codes.yaml 產生）
pub enum Interface { Rpc, RpcFrame, CliClient, CliLocal, Rec, Proc }
pub enum Retryable { Yes, No, Conditional, FixArgs }
pub enum UartTx { None, Possible, Confirmed }
pub enum Status { Existing, Approved, Implemented, Verified }
pub struct ErrorMeta {
    pub code: &'static str,
    pub interfaces: &'static [Interface],
    pub retryable: Retryable,
    pub uart_tx: UartTx,
    pub reachable: bool,
    pub status: Status,
}
pub enum ErrorCode { /* 每個 YAML entry 一個 variant；UPPER_SNAKE → UpperCamel，例：CMD_TOO_LONG → CmdTooLong */ }
impl ErrorCode {
    pub const META: &'static [ErrorMeta];
    pub fn as_str(&self) -> &'static str;
}
pub enum RpcErrorCode { Code(ErrorCode), LastErrorPassthrough(SessionLastError) }

// crates/sw-proto/src/session_last_errors.rs（P3 依 contracts/session_last_errors.yaml 產生）
pub enum SessionLastError {
    /* kind: named → 無欄位 variant，例：PROMPT_UNAVAILABLE → PromptUnavailable */
    ReprobeFailed(String),   // value "REPROBE_FAILED:*"，String＝冒號後字尾
    AttachFailed(String),    // "ATTACH_FAILED:*"
    BridgeDown(String),      // "BRIDGE_DOWN:*"
}
impl SessionLastError {
    pub fn as_wire(&self) -> String;
    pub fn parse(s: &str) -> Option<Self>;
}
```

YAML→Rust 映射：`rpc→Interface::Rpc`、`rpc_frame→RpcFrame`、`cli_client→CliClient`、`cli_local→CliLocal`、`rec→Rec`、`proc→Proc`；`"yes"→Retryable::Yes`、`"no"→No`、`"conditional"→Conditional`、`"fix_args"→FixArgs`；`EXISTING→Status::Existing`、`APPROVED→Approved`、`IMPLEMENTED→Implemented`、`VERIFIED→Verified`。`wal_record.schema.json` 對應 `sw_proto::wal_types::WalRecord`（arch §3.8）；`state_file.schema.json` 對應 `sw_core::state_store`；`rpc_schema.json` 的 `x-methods` 鍵集合對應 `sw_proto::rpc_types::Method`（arch §3.5）。

---

## Task T1：契約目錄 loader 與結構規則

**Test author model:** agy-gemini-3.8-flash-high　**Implementer model:** haiku-xhigh

**Files:**
- Create: `tools/contracts/__init__.py`、`tools/contracts/catalog.py`
- Test: `tests/test_contracts_catalog.py`（本 Task 建立；T3～T5 追加）
- Fragment: `changelog.d/rust-p0-contract-catalog-loader.md`

**Interfaces:**
- Consumes：PyYAML `yaml.safe_load`。
- Produces：「共用介面」所列 `catalog.py` 全部名稱。

**結構規則（loader 必須逐條檢查，問題字串格式 `<檔名>:<定位>: <訊息>`，定位如 `codes[3](AUTOBOOT_QUIET).retryable`）：**

`error_codes.yaml`：
1. 頂層：`schema_version == 1`、`revision` 為 ≥1 的 int、`namespace == "error_code"`、`codes` 為 list、`passthrough` 為 list；不得有其他頂層鍵。
2. `code` 唯一，符合 `^[A-Z][A-Z0-9_]*[A-Z0-9]$`；`codes` 依 `code` 升冪排序。
3. `status ∈ CODE_STATUS`；`reachable` 為 bool；`reachable: false` 時 `unreachable_reason` 為非空 str。
4. `scope.interfaces` 非空且 ⊆ `INTERFACES`；`scope.methods` 為 list[str]；`scope.platforms` 非空且 ⊆ `PLATFORMS`。
5. `meaning`、`side_effect`、`agent_action` 為非空 str，且不得含 `|` 或換行（SKILL 表格需要）。
6. `retryable` 必須是 str 且 ∈ `RETRYABLE`；若讀到 bool，訊息須含「引號」二字（提示 YAML 1.1 把 `yes`／`no` 轉成布林）。`uart_tx` 必須是 str 且 ∈ `UART_TX`；`ok_response` 選填 bool（預設 false）。
7. `extra_fields` list[str]；`contract_tests` list[str]，每筆符合 `^[^\s:]+\.py(::[A-Za-z_][\w\[\]\-.]*)*$`；`sources` list[str]（選填）；`constant` 選填，符合 `^[A-Z_][A-Z0-9_]*$`。
8. `behavior_change` list[str]，每筆符合 `^(BC|SC|DF)-\d{2}$`。
9. `scope.interfaces` 含 `cli_local` 時 `exit_codes` 必須為非空 mapping[str,int]；其他情況選填；`exit_codes` 的鍵必須 ∈ `CLI_CONTEXTS`。
10. `status: APPROVED` 時 `reachable` 必須為 false 且 `behavior_change` 非空；`status ∈ {IMPLEMENTED, VERIFIED}` 時 `reachable` 必須為 true 且 `contract_tests` 非空（狀態與所引用變更的一致性屬 T6 C16）。
11. 不得有 `code` 以 `HARNESS_` 開頭。
12. `passthrough[*]`：`id` 符合 `^[a-z][a-z0-9_]*$` 且唯一、`namespace_ref == "session_last_errors"`、`scope.methods` 非空、`rule` 非空、`owner_confirmation ∈ {not_required, pending_default, confirmed}`、`reconciliation_refs` list[str]。

`session_last_errors.yaml`：
1. 頂層 `schema_version == 1`、`revision`、`namespace == "session_last_error"`、`values` list；不得有其他鍵。
2. `kind == "named"` 時 `value` 符合 `^[A-Z][A-Z0-9_]*[A-Z0-9]$`；`kind == "dynamic_prefix"` 時符合 `^[A-Z][A-Z0-9_]*:\*$` 且 `reason_forms` 非空。
3. `value` 唯一並升冪排序；`reachable`／`unreachable_reason` 同上。
4. `meaning`、`agent_action`、`manual_recovery`、`logging`、`detail` 非空 str、無 `|`／換行。
5. `terminal` bool；`auto_reprobe` 為 `{attached: bool, detached: bool}`；`states` 非空 ⊆ `SESSION_STATES`；`set_by` 非空 list[str]；`cleared_by` 非空 list[str]，每筆符合 `^(auto|operator):.+`；`promoted_via` ⊆ `{session.attach, session.recover}`；`error_code_namespace ∈ {forbidden, allowed_shared}`；`contract_tests` 同上格式。
6. 一致性：`terminal: true` ⇒ `auto_reprobe.attached` 與 `auto_reprobe.detached` 皆 false，且 `cleared_by` 沒有 `auto:` 開頭的項；`terminal: false` ⇒ `cleared_by` 至少一筆 `auto:` 或 `auto_reprobe` 至少一邊為 true；`promoted_via` 非空 ⇒ `"ATTACHED" in states`（提升路徑只在 bridge 存在的 ATTACHED 態可達，盤點 03 §5.4）。

`behavior_changes.yaml`：
1. 頂層 `schema_version == 1`、`revision`、`changes` list、`pending_owner_confirmations` list、`safety_invariants` list。
2. `id` 符合 `^(BC|SC|DF)-\d{2}$` 且唯一，`kind` 等於 id 前綴；`status ∈ CHANGE_STATUS`。
3. `approval.owner` 非空 str；`approval.date` 必須是 str 且符合 `^\d{4}-\d{2}-\d{2}$`（讀到 `datetime.date` 時訊息須含「引號」）；`approval.basis ∈ {owner_decision, spec_final, reconciliation}`；`approval.owner == APPROVAL_OWNER_BY_BASIS[basis]`（由 agent 推導的 SC／DF 不得冒用擁有者名義）；`kind == "BC"` 時 `basis == "owner_decision"` 且 `decision_ref == "OD-04"`；`kind == "DF"` 時 `basis == "reconciliation"`。
4. `applies_to.python` 為 `null` 或 `{min_version: <\d+\.\d+\.\d+>}`；`applies_to.rust` 為 `null` 或 `{stage: one of stage-1a|stage-1b|slice-2.1|slice-2.2|slice-2.3|slice-2.4|stage-3}`；`scope ∈ CHANGE_SCOPES`；`scope == "rust_only"` 時 `applies_to.python` 必須為 null；`scope == "python_when_manifest_present"` 時 `applies_to.python` 必須非 null。
5. `approved_at_revision` 為 int，1 ≤ 值 ≤ 檔案 `revision`；檔內不得出現 `contract_revision` 鍵（訊息須提示「tree SHA 只記於 release manifest」）。
6. `spec_refs` 非空；`phases` 非空 ⊆ `{P0..P9}`；`case_ids` 非空、全檔唯一、符合 `^[a-z0-9][a-z0-9-]*$`；`compatibility_impact`、`approval_conditions`、`title` 非空；`ab_participation ∈ AB_PARTICIPATION`，且 `scope == "both"` ⇔ `ab_participation == "diff"`。
7. `owner_confirmation ∈ {not_required, pending_default, confirmed}`；`pending_default` 時 `owner_item ∈ 1..7` 且 `default_if_pending` 非空。
8. `pending_owner_confirmations[*]`：`item` 1..7 唯一、`topic`／`default` 非空、`refs` 非空、`affects` 每筆為已存在的 change id 或 `error_codes:passthrough:<id>`／`session_last_errors:<value>`。
9. `safety_invariants[*]`：`id` 集合恰為 `SAFETY_INVARIANTS`、`title` 非空、`spec_ref == "§11.3"`、`error_codes` list[str]、`case_ids` 每筆必須是本檔某 change 的 case_id。

`diagnostic_aux.yaml`：`schema_version == 1`、`revision`、六個 list 鍵 `warning_codes`、`error_details`、`classifications`、`recommended_actions`、`recovery_actions`、`recover_actions`，各元素欄位依「共用介面」`AuxCatalog`；值唯一；`classifications[*].recommended_actions` 為非空 list，每個值必須在 `recommended_actions` 中（不得出現 `attach／recover` 這類合成字串）。

`load_yaml_strict(path)`：以自訂 `SafeLoader` 子類別在 `construct_mapping` 偵測重複鍵（問題字串含 `重複鍵` 與行號）；`yaml.YAMLError`、`UnicodeDecodeError` 轉成 `CatalogError`；不得讓例外原樣穿出 loader。

`load_catalogs(repo_root)`：以四個 per-file loader 讀 `contracts/{error_codes,session_last_errors,behavior_changes,diagnostic_aux}.yaml`，任一缺檔或有問題即 `raise CatalogError(problems)`（所有檔案的問題一次列全）；成功時回傳 `Catalogs`。per-file loader 只讀自己的檔，讓 T3／T4／T5 在其他目錄檔尚未建立時即可轉綠。

- [ ] **Step 1：寫失敗測試**（`tests/test_contracts_catalog.py`）

```python
from __future__ import annotations

import importlib
import sys
import textwrap
from pathlib import Path

import pytest
import yaml

ROOT = Path(__file__).resolve().parents[1]


def _load():
    if str(ROOT) not in sys.path:
        sys.path.insert(0, str(ROOT))
    return importlib.import_module("tools.contracts.catalog")


GOOD_EC = textwrap.dedent("""\
    schema_version: 1
    revision: 1
    namespace: error_code
    codes:
      - code: FOO_BAR
        status: EXISTING
        reachable: true
        scope: {interfaces: [rpc], methods: [x.y], platforms: [posix, windows]}
        meaning: 意義
        side_effect: 無
        retryable: "no"
        uart_tx: none
        agent_action: 處置
        extra_fields: []
        contract_tests: [tests/test_x.py::test_y]
        behavior_change: []
    passthrough: []
    """)


def test_good_error_codes_doc_has_no_problems():
    assert _load().validate_error_codes_doc(yaml.safe_load(GOOD_EC)) == []


def test_unquoted_yes_retryable_is_rejected_with_quote_hint():
    doc = yaml.safe_load(GOOD_EC.replace('retryable: "no"', "retryable: yes"))
    problems = _load().validate_error_codes_doc(doc)
    assert any("codes[0](FOO_BAR).retryable" in p and "引號" in p for p in problems)


def test_load_catalogs_reports_all_missing_files(tmp_path):
    cat = _load()
    with pytest.raises(cat.CatalogError) as ei:
        cat.load_catalogs(tmp_path)
    joined = "\n".join(ei.value.problems)
    for name in ("error_codes.yaml", "session_last_errors.yaml", "behavior_changes.yaml", "diagnostic_aux.yaml"):
        assert name in joined
```

其餘結構規則以參數化表格展開（每列一個 `pytest.param`，對 `GOOD_EC`／對應的 GOOD 文件做單一修改，斷言 `problems` 中有一筆同時含「定位」與「關鍵字」）：

| test id | 修改 | 斷言 problems 含 |
|---|---|---|
| `ec_bad_code_pattern` | `code: FOO_BAR`→`code: foo_bar` | `codes[0](foo_bar).code` |
| `ec_duplicate_code` | `codes` 複製第一筆 | `重複` |
| `ec_unsorted` | 新增 `code: AAA` 於 FOO_BAR 之後 | `排序` |
| `ec_bad_status` | `status: EXISTING`→`status: LIVE` | `.status` |
| `ec_unreachable_needs_reason` | `reachable: true`→`reachable: false` | `.unreachable_reason` |
| `ec_bad_interface` | `[rpc]`→`[daemon]` | `.scope.interfaces` |
| `ec_empty_platforms` | `[posix, windows]`→`[]` | `.scope.platforms` |
| `ec_pipe_in_meaning` | `meaning: 意義`→`meaning: "a|b"` | `.meaning` |
| `ec_newline_in_action` | `agent_action: 處置`→`agent_action: "a\nb"` | `.agent_action` |
| `ec_bool_no_retryable` | `retryable: "no"`→`retryable: no` | `引號` |
| `ec_bad_contract_test_ref` | `[tests/test_x.py::test_y]`→`[not a path]` | `.contract_tests` |
| `ec_bad_bc_ref` | `behavior_change: []`→`behavior_change: [BC-1]` | `.behavior_change` |
| `ec_cli_local_needs_exit_codes` | `[rpc]`→`[cli_local]` | `.exit_codes` |
| `ec_approved_must_be_unreachable` | `status: EXISTING`→`status: APPROVED`（reachable 仍 true） | `APPROVED` |
| `ec_implemented_needs_tests` | `status: IMPLEMENTED`、`contract_tests: []` | `.contract_tests` |
| `ec_bad_uart_tx` | `uart_tx: none`→`uart_tx: maybe` | `.uart_tx` |
| `ec_unknown_exit_context` | `[rpc]`→`[cli_local]` 並加 `exit_codes: {"daemon begin": 2}` | `.exit_codes` |
| `yaml_duplicate_key` | 對 `load_yaml_strict` 餵含兩個 `retryable:` 的 entry | `重複鍵` |
| `yaml_syntax_error` | `load_yaml_strict` 讀 `codes: [` | `CatalogError`（不是 `yaml.YAMLError`） |
| `ec_harness_prefix_forbidden` | `FOO_BAR`→`HARNESS_TIMEOUT` | `HARNESS_` |
| `ec_unknown_top_key` | 加頂層 `extra: 1` | `extra` |
| `ec_passthrough_bad_ref` | `passthrough: []`→一筆 `namespace_ref: other` | `.namespace_ref` |
| `le_dynamic_needs_reason_forms` | session_last_errors GOOD 文件中 dynamic 項刪 `reason_forms` | `.reason_forms` |
| `le_bad_state` | `states: [ATTACHED]`→`[ONLINE]` | `.states` |
| `le_bad_promoted_via` | `promoted_via: [session.attach]`→`[command.submit]` | `.promoted_via` |
| `le_bad_namespace_flag` | `error_code_namespace: forbidden`→`maybe` | `.error_code_namespace` |
| `le_terminal_cannot_reprobe` | `terminal: true` 且 `auto_reprobe: {attached: true, detached: false}` | `.terminal` |
| `le_terminal_no_auto_clear` | `terminal: true` 且 `cleared_by: ["auto:裝置在線@session_manager.py:1846"]` | `.cleared_by` |
| `le_promoted_needs_attached` | `states: [DETACHED]` 且 `promoted_via: [session.recover]` | `.promoted_via` |
| `le_bad_cleared_by` | `cleared_by: [whenever]` | `.cleared_by` |
| `bc_unquoted_date` | `date: "2026-10-15"`→`date: 2026-10-15` | `引號` |
| `bc_kind_mismatch` | `id: BC-01, kind: SC` | `.kind` |
| `bc_bc_requires_od04` | BC 項 `decision_ref: OD-06` | `OD-04` |
| `bc_owner_matches_basis` | DF 項 `approval: {basis: reconciliation, owner: hamanpaul}` | `.approval.owner` |
| `bc_rust_only_no_python` | `scope: rust_only` 且 `applies_to.python: {min_version: "0.4.0"}` | `.applies_to.python` |
| `bc_manifest_scope_needs_python` | `scope: python_when_manifest_present` 且 `applies_to.python: null` | `.applies_to.python` |
| `bc_ab_participation_mismatch` | `scope: both` 且 `ab_participation: absolute_only` | `.ab_participation` |
| `bc_revision_exceeds_file` | `approved_at_revision: 2`（檔案 `revision: 1`） | `.approved_at_revision` |
| `bc_contract_revision_key_forbidden` | change 項加 `contract_revision: 1` | `tree SHA` |
| `bc_invariant_set_incomplete` | `safety_invariants` 少一筆 `no_auto_resend` | `safety_invariants` |
| `bc_invariant_unknown_case` | `safety_invariants[0].case_ids: [nope-case]` | `case_ids` |
| `bc_duplicate_case_id` | 兩項共用同一 case id | `case_ids` |
| `bc_pending_needs_item` | `owner_confirmation: pending_default` 無 `owner_item` | `.owner_item` |
| `bc_affects_unknown` | `pending_owner_confirmations[0].affects: [SC-99]` | `affects` |
| `aux_duplicate_classification` | classifications 兩筆同 value | `重複` |
| `aux_classification_action_unknown` | classification 的 `recommended_actions: ["attach／recover"]`（不在 `recommended_actions` 清單） | `.recommended_actions` |

（`session_last_errors`、`behavior_changes`、`diagnostic_aux` 的 GOOD 文件常數寫在同檔，各自最小合法內容一筆；測試作者依上述結構規則撰寫。）

- [ ] **Step 2：執行並確認 FAIL**

Run: `python3 -m pytest -q tests/test_contracts_catalog.py`
Expected: 全部 FAIL，錯誤為 `ModuleNotFoundError: No module named 'tools.contracts'`（collection 成功，個別 test 失敗）。

- [ ] **Step 3：commit 測試**

```bash
git add tests/test_contracts_catalog.py
git commit -m "test(contracts): 新增契約目錄結構規則的失敗測試" -m "Co-authored-by: Copilot <223556219+Copilot@users.noreply.github.com>"
```

- [ ] **Step 4：實作 `tools/contracts/__init__.py`、`tools/contracts/catalog.py`**

  - `__init__.py`：只放模組 docstring 與 `CONTRACTS_DIR = "contracts"`；不得 import `sw_core`。
  - `catalog.py`：每個 `validate_*_doc` 以「逐鍵檢查、累積 problems、不 raise」實作；型別判斷 bool 必須先於 int（`isinstance(True, int)` 為真）。
  - `load_catalogs`：先跑四個 validate，全部無問題才建 dataclass；tuple 化所有 list；`exit_codes` 以 `types.MappingProxyType(dict(...))` 包裝。
  - 只 import stdlib 與 `yaml`（`tools/contracts/**` 的 import 白名單由 T2 `test_tools_contracts_imports_stdlib_and_yaml_only` 強制）。
  - 交叉檢查（`behavior_change` id 是否存在等）**不放在** loader，屬 T6 `check_cross_refs`。

- [ ] **Step 5：執行並確認 PASS**

Run: `python3 -m pytest -q tests/test_contracts_catalog.py`
Expected: 全部 PASS（T3～T5 的內容測試在各自 Task 才新增）。

- [ ] **Step 6：全套與政策**

Run: `python3 -m pytest -q tests/` 與 `python3 -m policy_check --repo .`
Expected: 無新失敗（逐 Task red→green，前面 Task 不留紅燈）；policy_check 無 FAIL。後續 Task 的 Step 6 均同此標準。

- [ ] **Step 7：commit 實作**

`changelog.d/rust-p0-contract-catalog-loader.md`：

```markdown
---
type: feat
scope: contracts
---
新增 `tools/contracts` 契約目錄 loader，逐條驗證 `error_codes`／`session_last_errors`／`behavior_changes`／`diagnostic_aux` 的結構與 YAML 1.1 型別陷阱（Rust 重構 P0）。
```

```bash
git add tools/contracts/__init__.py tools/contracts/catalog.py changelog.d/rust-p0-contract-catalog-loader.md
git commit -m "feat(contracts): 新增契約目錄 loader 與結構驗證" -m "Co-authored-by: Copilot <223556219+Copilot@users.noreply.github.com>"
```

**BLOCKED-OWNER：** 無（本 Task 證據全部可在本環境產出）。

---

## Task T2：AST 碼抽取器（常數傳遞、參數／回傳傳遞、動態前綴）

**Test author model:** haiku-xhigh　**Implementer model:** codex-gpt-6-luna-max（高風險：靜態分析的完整性直接決定 OD-06 是否有漏網碼）

**Files:**
- Create: `tools/contracts/extract.py`、`contracts/code_extraction.yaml`
- Test: `tests/test_contracts_extract.py`
- Fragment: `changelog.d/rust-p0-code-extractor.md`

**Interfaces:**
- Consumes：Python `ast`（3.10 語法）、PyYAML。
- Produces：「共用介面」所列 `extract.py` 全部名稱；`contracts/code_extraction.yaml`（T6、T12 與 R-27 宣告的抽取命令都讀它）。

**`contracts/code_extraction.yaml` 內容（逐字建立）：**

```yaml
# 本檔宣告「如何從 sw_core 抽出會輸出的碼」（OD-06：repo 以設定宣告抽取路徑，規則本身不內建碼表）。
schema_version: 1
roots: ["sw_core/**/*.py"]
exclude: ["sw_core/assets/**"]
code_pattern: '^[A-Z][A-Z0-9_]*[A-Z0-9]$'
dynamic_prefix_pattern: '^[A-Z][A-Z0-9_]*:'
max_depth: 12
exclude_functions:
  # property setter：`self._last_error = value` 的 value 來自所有 `.last_error = X` 指派，已由 attribute_store sink 直接擷取
  - "sw_core/session_manager.py::SessionRuntime.last_error"
namespaces:
  error_code:
    sinks:
      - {kind: dict_key, key: error_code}
      - {kind: subscript_store, key: error_code}
      - {kind: call_keyword, keyword: error_code}
      - {kind: param_default, param: error_code}
      - {kind: call_arg, callee: TunnelError, index: 0}
      - {kind: call_arg, callee: _EndpointResolutionError, index: 0}
      - {kind: call_arg, callee: RuntimeError, index: 0, value_pattern: '^[A-Z][A-Z0-9]*(_[A-Z0-9]+)+$', implies_interface: proc}
    relays:
      - {kind: call_method_arg, method: get, arg: error_code}
      - {kind: subscript_load, key: error_code}
      - {kind: attribute_load, attr: code}
    passthrough:
      - {kind: attribute_load, attr: last_error, emits: last_error_passthrough}
      - {kind: call_method_arg, method: get, arg: last_error, emits: last_error_passthrough}
      - {kind: subscript_load, key: last_error, emits: last_error_passthrough}
  session_last_error:
    sinks:
      - {kind: attribute_store, attr: last_error}
    relays:
      - {kind: attribute_load, attr: last_error}
      - {kind: call_method_arg, method: get, arg: last_error}
    passthrough: []
allow_unresolved: []
exclude_values: []
```

`allow_unresolved` 與 `exclude_values` 起始為空；實作者在 Step 5 對真實 `sw_core` 執行後，**只能**為「經人工確認不輸出新碼」的位置加項，且每項 `reason` 必須寫出該運算式為何不會產生新碼（例如「`cli_trace` 只轉錄 response 既有 error_code」）。不得用來掩蓋真的會輸出、但目錄沒有的碼。`exclude_values` 每項另須 `category ∈ {not_a_code, relay_artifact}`；任一被排除的值若同時是任何目錄（`error_codes`／`session_last_errors`／`diagnostic_aux`）中的值，T6 判 C19 FAIL；其他每筆排除都以 C19 WARN 回報並計入 `summary()`。

**抽取語意（實作者照此實作；測試以此為準）：**

1. 檔案集合：`roots` 以 `pathlib.Path(repo_root).glob(pattern)` 的遞迴語意展開（`**` 匹配零或多層目錄，因此 `sw_core/**/*.py` 同時含 `sw_core/cli.py` 與 `sw_core/event_engine/*.py`），只收一般檔案；扣除 `exclude`（同樣以 `Path.glob` 展開後做集合差）；依 repo 相對 POSIX 路徑排序。**不得用 `fnmatch`**（`fnmatch("sw_core/cli.py", "sw_core/**/*.py")` 為 False，會漏掉 `sw_core/` 直層的全部模組）。
2. 對每個 namespace 的每個 sink，找出 AST 中對應位置的「值運算式」：
   - `dict_key`：`Dict` 字面中 key 為 `Constant(key)` 的 value；以及 `dict(<key>=X)` 呼叫的 keyword。
   - `subscript_store`：`Assign`／`AugAssign` target 為 `Subscript(slice=Constant(key))`。
   - `call_keyword`：任何 `Call` 的 `keyword(arg=keyword)`。
   - `param_default`：`FunctionDef` 參數名等於 `param` 的預設值（含 kw-only）。
   - `call_arg`：`Call.func` 為 `Name(callee)` 或 `Attribute(attr=callee)` 時第 `index` 個位置參數；`value_pattern` 存在時只收符合的字串常數，不符者**靜默忽略**（例：`RuntimeError("serial not ready")`）。
   - `attribute_store`：`Assign` target 為 `Attribute(attr=attr)`（不論 receiver）。
3. `resolve(expr, ctx, depth, index=None) -> set[Value]`，`Value` 為 code／dynamic_prefix／passthrough／relay／unresolved 之一：
   1. `Constant(str)`：符合 `code_pattern` → code；`dynamic_prefix_pattern` 不適用於純常數；空字串 → 忽略。其他非空字串：若此值是**明確碼 sink**（`dict_key`、`subscript_store`、`call_keyword`、`param_default`，以及未設 `value_pattern` 的 `call_arg`）經任何傳遞路徑解析出的值 → unresolved(`malformed_code`)（例：`{"error_code": "new-code"}`、`TunnelError("bad code")` 必須 FAIL，不得靜默）；只有設了 `value_pattern` 的 `call_arg`（例 `RuntimeError`）不符 pattern 時才忽略。
   2. `Constant(None)`、`Constant(bool/int)` → 忽略。
   3. `JoinedStr`：第一段為 `Constant(str)` 且符合 `dynamic_prefix_pattern` → dynamic_prefix `<到第一個 {} 之前的字面>*`，例 `f"REPROBE_FAILED:{x}"` → `REPROBE_FAILED:*`；否則 unresolved(`unsupported_expr`)。
   4. `BinOp(Add)` 左運算元為 `Constant(str)` 且符合 `dynamic_prefix_pattern` → 同上；否則 unresolved。
   5. `BoolOp`（`or`／`and`）→ 各運算元聯集；`IfExp` → `body` 與 `orelse` 聯集。
   6. relay 與 passthrough 樣式先於一般規則比對：`call_method_arg` 為 `<任意>.<method>(Constant(arg), ...)`；`subscript_load` 為 `<任意>[Constant(key)]`（Load context）；`attribute_load` 為 `<任意>.<attr>`（Load）。命中 relay → relay（不產生值）**∪ 對其餘位置參數與 `default=` keyword 遞迴 resolve 的結果**（例：`resp.get("error_code", "NEW_CODE")` 產生 `NEW_CODE`；`resp.get("error_code", fallback)` 依 `fallback` 往下解析）；命中 passthrough → passthrough(`emits`) ∪ 同樣的 default 解析結果。
   7. `Name`：依序找 (a) 同函式內所有對該名的賦值（`Assign`、`AnnAssign`、`AugAssign`、tuple 解包——記錄解包位置作 index、`NamedExpr`、`for` 目標、`with ... as`），取聯集；(b) 若為該函式參數 → 預設值 ∪ 全部呼叫點實參（依「名稱」比對 `Name(func)` 或 `Attribute(attr=func)` 呼叫，位置或 keyword 對應，跳過 `self`／`cls`）；(c) 同模組頂層賦值；(d) `from X import NAME [as A]`（含相對 import，依 package 路徑解析）→ 目標模組頂層賦值。都找不到 → unresolved(`unknown_name`)。
   8. `Attribute(value=Name(m), attr=A)`，`m` 為 `import X as m`／`from . import m` 解析到的 repo 模組 → 該模組頂層 `A`；`self.A`／`cls.A` → 所屬 class 的 class-level 賦值；都不符 → unresolved(`unknown_name`)。
   9. `Call`：(a) `<Dict 字面>.get(k[, d])` 或 `Name.get(...)`（Name 解析為 Dict 字面）→ 所有 dict value ∪ `d`；(b) callee 名稱（`Name.id` 或 `Attribute.attr`）在 repo 中有同名 `def`（不論 class）→ 這些函式所有 `return` 運算式的聯集；有 `index` 時取 `Tuple` 第 index 個元素，return 本身是 Name／Call 時把 index 往下傳；函式無任何帶值 return → unresolved(`no_return_value`)；(c) repo 內無同名函式 → unresolved(`unknown_callee`)。
   10. `Subscript(value=Dict 字面, slice=Constant)` → 對應 value；`Tuple`／`List` 在有 index 時取該元素。
   11. 其他節點 → unresolved(`unsupported_expr`)。
   12. 防護：以 `(file, node 起始位置, index)` 為 key 的 visited 集合；重入時回傳 unresolved(`cycle`)，但若同一次 resolve 的其他分支已得到值，`cycle` 不計入結果（只在聯集完全沒有值時保留）；`depth > max_depth` → unresolved(`max_depth`)。
4. `exclude_functions` 內的函式本體不產生 sink（呼叫點傳遞仍照常解析）。
5. emission 去重鍵為 `(namespace, value, file, line, sink)`；`ExtractionResult.emissions` 依 `(namespace, value, file, line)` 排序。
6. `allow_unresolved` 以 `(namespace, function, expr)` 比對並扣除，`expr` 一律為 `canon_expr(node)`（`Unresolved.expr` 同樣以 `canon_expr` 產生，3.10～3.12 輸出相同）；沒有比對到任何 unresolved 的 allow 項列入 `stale_allow_unresolved`。`exclude_values` 自 `values()` 結果扣除，並把 `(項, 扣除數)` 記入 `applied_exclusions`；沒對應任何 emission 的列入 `stale_exclude_values`。
7. `load_extraction_config` 對未知鍵、未知 `kind`、缺必要欄位（`dict_key`／`subscript_store` 需 `key`、`call_keyword` 需 `keyword`、`param_default` 需 `param`、`call_arg` 需 `callee`＋`index`、`attribute_store`／`attribute_load` 需 `attr`、`call_method_arg` 需 `method`＋`arg`、passthrough 需 `emits`、`exclude_values` 需 `category`＋非空 `reason`）、重複 YAML 鍵與 `yaml.YAMLError` raise `ExtractionConfigError`。
8. `canon_expr(node)`：遞迴輸出 `<型別名>(<欄位>=<值>,...)`，欄位依 `node._fields` 順序，略過 `ctx`、`type_comment`、`kind` 與值為 `None`／空 list 的欄位；`Constant` 輸出 `Constant(value=<repr>)`；list 以 `[a,b]`。不得呼叫 `ast.unparse` 或 `ast.dump`（兩者輸出隨 Python 版本變動）。

- [ ] **Step 1：寫失敗測試**（`tests/test_contracts_extract.py`）

```python
from __future__ import annotations

import importlib
import sys
import textwrap
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]

BASE_CONFIG = textwrap.dedent("""\
    schema_version: 1
    roots: ["pkg/**/*.py"]
    exclude: []
    code_pattern: '^[A-Z][A-Z0-9_]*[A-Z0-9]$'
    dynamic_prefix_pattern: '^[A-Z][A-Z0-9_]*:'
    max_depth: 12
    exclude_functions: []
    namespaces:
      error_code:
        sinks:
          - {kind: dict_key, key: error_code}
          - {kind: subscript_store, key: error_code}
          - {kind: call_keyword, keyword: error_code}
          - {kind: param_default, param: error_code}
          - {kind: call_arg, callee: TunnelError, index: 0}
          - {kind: call_arg, callee: RuntimeError, index: 0, value_pattern: '^[A-Z][A-Z0-9]*(_[A-Z0-9]+)+$', implies_interface: proc}
        relays:
          - {kind: call_method_arg, method: get, arg: error_code}
          - {kind: subscript_load, key: error_code}
          - {kind: attribute_load, attr: code}
        passthrough:
          - {kind: attribute_load, attr: last_error, emits: last_error_passthrough}
          - {kind: call_method_arg, method: get, arg: last_error, emits: last_error_passthrough}
      session_last_error:
        sinks:
          - {kind: attribute_store, attr: last_error}
        relays:
          - {kind: attribute_load, attr: last_error}
        passthrough: []
    allow_unresolved: []
    exclude_values: []
    """)


def _load():
    if str(ROOT) not in sys.path:
        sys.path.insert(0, str(ROOT))
    return importlib.import_module("tools.contracts.extract")


def _run(tmp_path, files, config=BASE_CONFIG):
    ex = _load()
    for rel, src in files.items():
        p = tmp_path / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(textwrap.dedent(src), encoding="utf-8")
    (tmp_path / "pkg" / "__init__.py").touch()
    cfg = tmp_path / "contracts" / "code_extraction.yaml"
    cfg.parent.mkdir(exist_ok=True)
    cfg.write_text(config, encoding="utf-8")
    return ex.extract(tmp_path, ex.load_extraction_config(cfg))


def test_imported_constant_is_propagated(tmp_path):
    res = _run(tmp_path, {
        "pkg/constants.py": 'ERROR_X = "X_CODE"\n',
        "pkg/svc.py": """\
            from .constants import ERROR_X as E
            def f():
                return {"ok": False, "error_code": E}
            """,
    })
    assert res.values("error_code") == {"X_CODE"}
    assert res.unresolved == ()


def test_return_tuple_index_propagation(tmp_path):
    res = _run(tmp_path, {"pkg/ft.py": """\
        def _probe():
            if a():
                return None, "TRANSFER_TIMEOUT"
            return None, "TARGET_DECODER_MISSING"
        def a():
            return True
        def push():
            dec, probe_error = _probe()
            if probe_error is not None:
                return {"ok": False, "error_code": probe_error}
        """})
    assert res.values("error_code") == {"TRANSFER_TIMEOUT", "TARGET_DECODER_MISSING"}


def test_param_chain_into_last_error(tmp_path):
    res = _run(tmp_path, {"pkg/sm.py": """\
        class M:
            def _detach(self, s, *, reason):
                s.last_error = reason
            def _by_id(self, b, *, reason):
                self._detach(b, reason=reason)
            def clear(self, s):
                self._detach(s, reason="CLEARED")
            def update(self, b, x):
                self._by_id(b, reason="DEVICE_REMOVED" if x else "DEVICE_REBOUND_REQUIRED")
        """})
    assert res.values("session_last_error") == {"CLEARED", "DEVICE_REMOVED", "DEVICE_REBOUND_REQUIRED"}
```

其餘案例以表格展開（每列一個 test function，`_run` 同上；「預期」欄為完整相等斷言，`U` 表示 `res.unresolved` 的 `reason` 集合）：

| test 名稱 | 原始碼重點 | 預期 |
|---|---|---|
| `test_literal_dict_key` | `return {"ok": False, "error_code": "FOO_BAR"}` | `values("error_code") == {"FOO_BAR"}` |
| `test_dict_call_keyword_form` | `return dict(ok=False, error_code="FOO")` | `{"FOO"}` |
| `test_module_attribute_constant` | `from . import constants as c`；`{"error_code": c.ERROR_X}` | `{"X_CODE"}` |
| `test_class_attribute_constant` | `class S: CODE = "S_CODE"`；方法內 `{"error_code": self.CODE}` | `{"S_CODE"}` |
| `test_boolop_dict_get_with_passthrough` | `code = {"FLASHING": "FLASHING_BUSY"}.get(state) or snap.get("last_error") or "NOT_READY"`；`return {"error_code": code}` | `values == {"FLASHING_BUSY","NOT_READY"}`；`values(..., include_passthrough=True) ⊇ {"last_error_passthrough"}` |
| `test_subscript_store` | `rec["error_code"] = "SEND_FAILED"` | `{"SEND_FAILED"}` |
| `test_relay_get_not_emitted` | `rec["error_code"] = result.get("error_code") or "COMMAND_FAILED"` | `{"COMMAND_FAILED"}`、`U == set()` |
| `test_param_default` | `def unregister(sid, error_code: str = "FLUSHED_BY_RECOVERY"): rec["error_code"] = error_code` | `{"FLUSHED_BY_RECOVERY"}` |
| `test_call_keyword_sink` | `arb.unregister(sid, error_code="FLUSHED_BY_SHUTDOWN")` | `{"FLUSHED_BY_SHUTDOWN"}` |
| `test_positional_callsite_propagation` | `def _fail(code): return {"error_code": code}`；`_fail("A_CODE")`；`self._fail(code="B_CODE")` | `{"A_CODE","B_CODE"}` |
| `test_exception_call_arg` | `class TunnelError(Exception): ...`；`raise TunnelError("INVALID_TARGET", "x")`；`raise rt.TunnelError("SSH_NOT_FOUND")` | `{"INVALID_TARGET","SSH_NOT_FOUND"}` |
| `test_exception_code_attr_is_relay` | `except TunnelError as exc: return {"error_code": exc.code}` | `values == set()`、`U == set()` |
| `test_runtime_error_value_pattern` | `raise RuntimeError("DAEMON_ALREADY_RUNNING")`；`raise RuntimeError("serial not ready")` | `{"DAEMON_ALREADY_RUNNING"}`；該 emission `implies_interface == "proc"` |
| `test_non_sink_uppercase_ignored` | `return {"warning": {"code": "CMD_LENGTH_WARNING"}}`；`X = "EMPTY_COMMAND"` | `values == set()` |
| `test_none_and_empty_ignored` | `{"error_code": None}`；`{"error_code": ""}` | `values == set()`、`U == set()` |
| `test_malformed_code_in_explicit_sink_unresolved` | `{"error_code": "new-code"}`；`raise TunnelError("bad code")` | `values == set()`、`U == {"malformed_code"}`，兩筆 |
| `test_malformed_via_propagation_unresolved` | `def _fail(code): return {"error_code": code}`；`_fail("oops")` | `U == {"malformed_code"}` |
| `test_relay_get_default_resolved` | `return {"error_code": resp.get("error_code", "NEW_CODE")}`；另一函式 `resp.get("error_code", fb)`，`fb = "FB_CODE"` | `values == {"NEW_CODE","FB_CODE"}`、`U == set()` |
| `test_roots_glob_includes_flat_and_nested` | `roots: ["pkg/**/*.py"]`；`pkg/flat.py` 與 `pkg/sub/deep.py` 各輸出一碼 | 兩碼都在 `values`；另以 `fnmatch` 的對照斷言證明本測試會抓到 fnmatch 實作 |
| `test_canon_expr_version_stable` | `canon_expr(ast.parse("a.get('x') or f(b, k=1)", mode="eval").body)` | 逐字等於測試內寫死的期望字串（不含 `ctx=`） |
| `test_fstring_dynamic_prefix` | `s.last_error = f"REPROBE_FAILED:{type(e).__name__}"` | `values("session_last_error") == {"REPROBE_FAILED:*"}`，kind `dynamic_prefix` |
| `test_binop_dynamic_prefix` | `s.last_error = "BRIDGE_DOWN:" + reason` | `{"BRIDGE_DOWN:*"}` |
| `test_last_error_self_relay` | `s.last_error = s.last_error or "RECOVERY_TIMEOUT"` | `{"RECOVERY_TIMEOUT"}`、`U == set()` |
| `test_unknown_callee_unresolved` | `return {"error_code": compute()}`（repo 無 `compute`） | `U == {"unknown_callee"}`，`Unresolved.function` 形如 `pkg/x.py::f` |
| `test_cycle_guard` | `def a(): return b()`；`def b(): return a()`；`{"error_code": a()}` | 有限時間內結束；`U == {"cycle"}` |
| `test_max_depth` | 20 層 `def fN(): return fN+1()`，最底層 `return "DEEP_CODE"` | `U == {"max_depth"}` |
| `test_exclude_function` | config `exclude_functions: ["pkg/x.py::R.last_error"]`；`R.last_error` setter 內 `self._last_error = value` 且 sink 為 `attribute_store attr: _last_error` | 該函式不產生 unresolved |
| `test_allow_unresolved_and_stale` | 一筆比對得到、一筆比對不到的 allow 項 | `unresolved == ()`；`stale_allow_unresolved` 恰為比對不到的那筆 |
| `test_exclude_values_and_stale` | `exclude_values` 含 `FOO`（有 emission，`category: not_a_code`）與 `GHOST`（無） | `FOO ∉ values`；`applied_exclusions` 含 `FOO` 且扣除數 ≥1；`stale_exclude_values` 恰含 `GHOST` |
| `test_config_unknown_kind_rejected` | sink `kind: magic` | `ExtractionConfigError` |
| `test_config_missing_field_rejected` | `call_arg` 缺 `index`；`exclude_values` 缺 `category` | 兩者皆 `ExtractionConfigError` |
| `test_config_duplicate_key_rejected` | YAML 中 `max_depth` 出現兩次 | `ExtractionConfigError` |
| `test_no_sw_core_import` | AST 掃 `tools/contracts/*.py` | 無任何 `import sw_core`／`from sw_core` |
| `test_tools_contracts_imports_stdlib_and_yaml_only` | AST 掃 `tools/contracts/*.py` 全部 import 的頂層模組名 | ⊆ `sys.stdlib_module_names ∪ {"yaml", "tools"}`（3.10 起有此屬性） |

**Mutation 測試**（`test_mutation_detects[<id>]` 參數化；每列在合成 repo 植入一個「目錄沒有的碼」，斷言 `values()` 含該碼——任何一列抓不到即代表抽取路徑有洞；審查 B 必須執行並附輸出）：

| id | 植入方式 |
|---|---|
| `dict_key` | `return {"error_code": "MUT_A"}` |
| `dict_call` | `return dict(error_code="MUT_B")` |
| `subscript_store` | `rec["error_code"] = "MUT_C"` |
| `call_keyword` | `arb.unregister(sid, error_code="MUT_D")` |
| `param_default` | `def f(error_code="MUT_E"): ...` |
| `call_arg_exc` | `raise TunnelError("MUT_F")` |
| `call_arg_proc` | `raise RuntimeError("MUT_G_X")` |
| `attribute_store_le` | `s.last_error = "MUT_H"`（namespace `session_last_error`） |
| `imported_const` | `from .c import E`；`{"error_code": E}`，`c.py`：`E = "MUT_I"` |
| `module_attr_const` | `from . import c as k`；`{"error_code": k.E2}` |
| `return_tuple` | `def p(): return None, "MUT_J"`；`_, e = p()`；`{"error_code": e}` |
| `callsite_arg` | `def g(code): return {"error_code": code}`；`g("MUT_K")` |
| `kw_callsite_arg` | `self.g(code="MUT_L")` |
| `relay_default` | `resp.get("error_code", "MUT_M")` |
| `passthrough_or` | `snap.get("last_error") or "MUT_N"` |
| `ifexp` | `"MUT_O" if x else None` |
| `dict_get_lookup` | `{"S": "MUT_P"}.get(state)` |
| `fstring_prefix` | `s.last_error = f"MUT_Q:{x}"` → `MUT_Q:*` |
| `binop_prefix` | `s.last_error = "MUT_R:" + x` → `MUT_R:*` |

真實 repo 測試（讀 `contracts/code_extraction.yaml` 對 `sw_core` 執行）。以下集合是**測試資料**（驗證抽取器沒漏掉已知的困難傳遞路徑），不是政策清單：

```python
TRICKY_ERROR_CODES = {
    "AUTOBOOT_QUIET", "READY_UNCONFIRMED", "CMD_TOO_LONG", "CMD_CONTAINS_NEWLINE",
    "INVALID_COMPLETION_REGEX", "FLUSHED_BY_RECOVERY", "FLUSHED_BY_SHUTDOWN", "SEND_FAILED",
    "COMMAND_FAILED", "TARGET_DECODER_MISSING", "TARGET_ENCODER_MISSING", "TRANSFER_PROBE_FAILED",
    "NOT_READY", "INVALID_TARGET", "BENCH_ENDPOINT_NOT_REMEMBERED", "TUNNEL_STATUS_ERROR",
    "DAEMON_ALREADY_RUNNING", "SOCKET_UNAVAILABLE", "NEEDS_SUDO", "NO_SYSTEMD",
}
TRICKY_LAST_ERRORS = {
    "CREDENTIALS_UNRESOLVED", "PROMPT_UNAVAILABLE", "BOOTLOADER_STUCK", "CLEARED", "RELEASED",
    "REBOUND", "DEVICE_REMOVED", "DEVICE_REBOUND_REQUIRED", "RECOVERY_TIMEOUT", "RECOVERY_ERROR",
    "REBOOTING", "TRANSPORT_STALL", "RX_FLOOD", "PROMPT_TIMEOUT", "LOGIN_REQUIRED",
    "REPROBE_FAILED:*", "ATTACH_FAILED:*", "BRIDGE_DOWN:*",
}
NOT_ERROR_CODES = {"CMD_LENGTH_WARNING", "EMPTY_COMMAND", "NOT_BOOTLOADER", "INVALID_INTERACTIVE_ENCODING"}


def _real():
    ex = _load()
    return ex.extract(ROOT, ex.load_extraction_config(ROOT / "contracts" / "code_extraction.yaml"))


def test_real_repo_tricky_error_codes_found():
    assert TRICKY_ERROR_CODES <= _real().values("error_code")


def test_real_repo_tricky_last_errors_found():
    assert TRICKY_LAST_ERRORS <= _real().values("session_last_error")


def test_real_repo_non_codes_excluded():
    assert not (NOT_ERROR_CODES & _real().values("error_code"))


def test_real_repo_passthrough_detected():
    assert "last_error_passthrough" in _real().values("error_code", include_passthrough=True)


def test_real_repo_proc_codes_marked():
    proc = {e.value for e in _real().emissions if e.implies_interface == "proc"}
    assert {"DAEMON_ALREADY_RUNNING", "SOCKET_UNAVAILABLE"} <= proc


def test_real_repo_no_unresolved_or_stale():
    res = _real()
    assert res.unresolved == ()
    assert res.stale_allow_unresolved == ()
    assert res.stale_exclude_values == ()


def test_real_repo_scans_flat_modules():
    # 防 fnmatch 類回歸：sw_core 直層模組必須被掃到
    files = {e.file for e in _real().emissions}
    assert {"sw_core/service.py", "sw_core/session_manager.py", "sw_core/cli.py", "sw_core/arbiter.py"} <= files
```

- [ ] **Step 2：執行並確認 FAIL**

Run: `python3 -m pytest -q tests/test_contracts_extract.py`
Expected: 全部 FAIL（`ModuleNotFoundError: tools.contracts.extract`；真實 repo 測試另因 `contracts/code_extraction.yaml` 不存在而 FAIL）。

- [ ] **Step 3：commit 測試**

```bash
git add tests/test_contracts_extract.py
git commit -m "test(contracts): 新增錯誤碼 AST 抽取器的失敗測試" -m "Co-authored-by: Copilot <223556219+Copilot@users.noreply.github.com>"
```

- [ ] **Step 4：實作 `tools/contracts/extract.py` 與 `contracts/code_extraction.yaml`**

結構（pseudocode，名稱照用）：

```python
class _Module:            # 每個檔案一份
    rel: str; tree: ast.Module; pkg: str               # pkg 例 "sw_core"
    top_assigns: dict[str, list[ast.expr]]
    imports: dict[str, tuple[str, str | None]]         # 本地名 → (模組 rel 路徑, 原名或 None=整個模組)
    functions: list[_Func]

class _Func:
    qualname: str        # "<rel>::<Class>.<name>" 或 "<rel>::<name>"
    node: ast.FunctionDef | ast.AsyncFunctionDef
    module: _Module
    cls: ast.ClassDef | None
    params: list[str]    # 依序；含 kw-only
    local_assigns: dict[str, list[tuple[ast.expr, int | None]]]   # 值與解包 index

class _Index:            # 全 repo
    modules: dict[str, _Module]
    funcs_by_name: dict[str, list[_Func]]
    calls_by_name: dict[str, list[tuple[_Func | None, ast.Call]]]  # 呼叫所在函式與 Call 節點

def extract(repo_root, config):
    index = _build_index(repo_root, config)
    emissions, unresolved = [], []
    for ns in config.namespaces:
        for func_or_module, node, sink in _iter_sinks(index, ns, config.exclude_functions):
            vals = _resolve(node.value_expr, ctx=_Ctx(index, ns, func_or_module), depth=0, index=None, visited=set())
            ...  # code / dynamic_prefix / passthrough → Emission；relay 丟棄；unresolved → Unresolved
    return _apply_allow_and_exclude(emissions, unresolved, config)
```

真實 repo 執行後若出現 unresolved，逐一判斷：能以新增抽取語意（屬第 3 點的一般規則）解決者先改抽取器並補 synthetic 測試；確屬「只轉錄既有碼」者才加 `allow_unresolved`（附 reason）。不得以 `exclude_values` 隱藏真的會輸出的碼——那種碼要在 T3 登錄。

- [ ] **Step 5：執行並確認 PASS**

Run: `python3 -m pytest -q tests/test_contracts_extract.py`
Expected: 全部 PASS。另執行 `python3 -m tools.contracts extract --namespace error_code --format lines`（T6 才有 CLI；本 Step 可先以 `python3 -c "from tools.contracts.extract import *; ..."` 列出），把結果與盤點 03 §3、§5.2 比對，差異寫入 Step 7 commit 訊息的 body（例如盤點漏列或抽取多抓）。

- [ ] **Step 6：全套與政策**

Run: `python3 -m pytest -q tests/`、`python3 -m policy_check --repo .`
Expected: 無新失敗。

- [ ] **Step 7：commit 實作**

`changelog.d/rust-p0-code-extractor.md`：

```markdown
---
type: feat
scope: contracts
---
新增以 AST 抽出 `sw_core` 會輸出的 error_code 與 session last_error 的抽取器，支援匯入常數、參數預設值、呼叫點參數、回傳 tuple、例外建構參數與動態前綴；抽取規則由 `contracts/code_extraction.yaml` 宣告（OD-06）。
```

```bash
git add tools/contracts/extract.py contracts/code_extraction.yaml changelog.d/rust-p0-code-extractor.md
git commit -m "feat(contracts): 新增錯誤碼 AST 抽取器與抽取設定" -m "Co-authored-by: Copilot <223556219+Copilot@users.noreply.github.com>"
```

**BLOCKED-OWNER：** 無。

---

## Task T3：`contracts/error_codes.yaml`（RPC／CLI error_code 權威目錄）

**Test author model:** agy-gemini-3.8-flash-high　**Implementer model:** haiku-xhigh

**Files:**
- Create: `contracts/error_codes.yaml`
- Test: `tests/test_contracts_catalog.py`（追加 `test_ec_real_*`）
- Fragment: `changelog.d/rust-p0-error-codes-catalog.md`

**Interfaces:**
- Consumes：T1 `load_catalogs`。
- Produces：`contracts/error_codes.yaml`；P3 `scripts/rust/gen_error_codes.py` 依此產生 `ErrorCode`／`ErrorMeta`／`RpcErrorCode`（見「共用介面」Rust 段）。

**內容來源與轉寫規則：**

- entry 集合＝盤點 03 §3.1、§3.2、§3.3、§3.4、§3.5（REC 碼 `FLUSHED_BY_RECOVERY`、`FLUSHED_BY_SHUTDOWN`、`SEND_FAILED`、`COMMAND_FAILED`）、§3.6（PROC 碼 `DAEMON_ALREADY_RUNNING`、`SOCKET_UNAVAILABLE`）的全部碼，加 BC-03 `COMMAND_ABORTED_IN_FLIGHT`、SC-19 `TEST_DEVICE_ALLOWLIST_REJECTED`。同一碼出現在多表時合併成一筆，`scope.interfaces` 取聯集（例：`INVALID_ARGS`＝`[rpc, cli_local]`、`FLASHING_BUSY`＝`[rpc, rec, cli_local]`、`INVALID_ENDPOINT`＝`[cli_client, cli_local]`、`INVALID_COMPLETION_REGEX`＝`[rpc, rec, cli_local]`、`UNKNOWN_PROFILE`＝`[rpc, cli_local]`、`DAEMON_ALREADY_RUNNING`＝`[proc]`）。依盤點應得 95 筆，此數字只供實作者自我核對，**不得**寫進任何程式、測試或文件。
- **不得**收錄：`CREDENTIALS_UNRESOLVED` 與其他只經 last_error 提升的值（改由 `passthrough` 表示，RPC-D-02）、`CMD_LENGTH_WARNING`、`NOT_BOOTLOADER`、`INVALID_INTERACTIVE_ENCODING`、`util.shell_command_incomplete_reason` 的 7 個值、self-test classification。
- 介面對照：盤點欄「RPC」→`rpc`、「RPC-frame」→`rpc_frame`、「CLI-client」→`cli_client`、「CLI-local」→`cli_local`、「REC」→`rec`、「PROC」→`proc`。
- `retryable` 對照盤點「可重試」欄：先正規化（去掉全形／半形括號內的附註，例 `是（等 READY 再送）`→`是`；`白名單方法是；其他否` 這類分支敘述視為 `條件`），再映射 `是`→`"yes"`、`否`→`"no"`、`條件`→`"conditional"`、`—`→`"fix_args"`；同碼在不同介面不一致時取 `"conditional"`（例：`TIMEOUT` 白名單方法可重試、其他不可）。`READY_UNCONFIRMED`＝`"no"`、`AUTOBOOT_QUIET`＝`"yes"`、`SEND_FAILED`＝`"no"`（結果不明，不可假設安全重試）、`COMMAND_ABORTED_IN_FLIGHT`＝`"no"`、`FLUSHED_BY_RECOVERY`／`FLUSHED_BY_SHUTDOWN`＝`"yes"`（未執行，可在 READY 後重送）。所有值必須加引號。
- 盤點沒有可重試欄的碼（RPC framing、CLI-local、PROC、REC fallback）依下表，不得留給實作者自由判斷；下表與盤點欄都涵蓋不到的碼，實作者須停下回報，不得自行補值：

  | retryable | 碼 |
  |---|---|
  | `"fix_args"` | `INVALID_JSON`、`INVALID_REQUEST`、`INVALID_METHOD`、`INVALID_ARGS`、`INVALID_ENDPOINT`、`INVALID_TARGET`、`INVALID_BENCH_CONFIG`、`INVALID_BENCH_STATE`、`INVALID_COMPLETION_REGEX`、`UNKNOWN_BENCH_CODE`、`UNKNOWN_EVENT_CMD`、`UNKNOWN_PROFILE`、`BENCH_ENDPOINT_NOT_REMEMBERED`、`REMOTE_NOT_SUPPORTED`、`NEEDS_SUDO`、`NO_SYSTEMD`、`ENV_FILE_SOURCE_FAILED`、`SAMPLE_READ_FAILED`、`DAEMON_BINARY_NOT_FOUND`、`SSH_NOT_FOUND`、`TUNNEL_CONFLICT` |
  | `"no"` | `EXCEPTION`、`INTERNAL_ERROR`、`DAEMON_ALREADY_RUNNING`、`DAEMON_ALREADY_RUNNING_ELSEWHERE`、`SOCKET_UNAVAILABLE`、`COMMAND_FAILED` |
  | `"conditional"` | `DAEMON_NOT_READY`、`DAEMON_EXITED`、`TUNNEL_SPAWN_FAILED`、`REMOTE_BIND_UNVERIFIED`、`TUNNEL_STILL_ACTIVE`、`TUNNEL_STATUS_ERROR`、`BENCH_STATE_IO_ERROR` |

  盤點值為 `n/a` 者（`PROMPT_TIMEOUT_RECOVERED`、`FORCE_RECOVERED` 等出現在 `ok:true` 回應的碼，ERR-7.1-e）：`retryable: "no"`（命令已執行，不得重送）、`ok_response: true`。
- `uart_tx`（§11.3 不變量 #1、#5 的機器可讀錨點）：gate／admission 拒絕與未執行即清掉的碼為 `none`——`SESSION_NOT_READY`、`AUTOBOOT_QUIET`、`READY_UNCONFIRMED`、`CMD_TOO_LONG`、`CMD_CONTAINS_NEWLINE`、`SESSION_QUEUE_FULL`、`SESSION_RECOVERING`、`FLASHING_BUSY`、`SESSION_BUSY`、`SESSION_INTERACTIVE_BUSY`、`PROFILE_NOT_COMMAND_CAPABLE`、全部 `INVALID_*`／`UNKNOWN_*`（`INVALID_RESPONSE` 除外）、全部 `FLUSHED_*`、`INVALID_JSON`／`INVALID_REQUEST`／`INVALID_METHOD`、`INVALID_ENDPOINT`、只含 `cli_local`／`proc` 介面的碼；已送出命令後才判定的碼為 `confirmed`（例 `PROMPT_TIMEOUT`：可能已送 `\x03`／`\x04`）；結果不明者為 `possible`（`SEND_FAILED`、`COMMAND_ABORTED_IN_FLIGHT`、`EXCEPTION`，以及請求可能已抵達 daemon 的 client 碼 `TIMEOUT`、`SOCKET_ERROR`、`EMPTY_RESPONSE`、`INVALID_RESPONSE`）。其餘 RPC 碼依 `side_effect` 欄判定，不確定時取 `possible`（不得以 `none` 高報安全性）。
- `reachable: false` 且附 `unreachable_reason`：`FLASH_TARGET_IS_CONSOLE`（production 無呼叫端）、`UNKNOWN_EVENT_CMD`（argparse `required=True` 攔截）、`COMMAND_FAILED`（execute_command 所有 ok:false 都帶碼，僅為 fallback）、`COMMAND_ABORTED_IN_FLIGHT`、`TEST_DEVICE_ALLOWLIST_REJECTED`（核准未實作）。其餘 `reachable: true`。
- `status`：既有碼 `EXISTING`；`COMMAND_ABORTED_IN_FLIGHT`、`TEST_DEVICE_ALLOWLIST_REJECTED` 為 `APPROVED`。P1 實作這兩碼時，輸出路徑必須能被 `contracts/code_extraction.yaml` 的 sink 擷取，並在**同一 commit** 把碼 `status` 改 `IMPLEMENTED`、`reachable` 改 true、補 `contract_tests`、把所引用變更改 `IMPLEMENTED`；否則 C03／C04／C16 會 FAIL：
  - `COMMAND_ABORTED_IN_FLIGHT`：例如 `rec["error_code"] = "COMMAND_ABORTED_IN_FLIGHT"`（既有 `subscript_store` sink）。
  - `TEST_DEVICE_ALLOWLIST_REJECTED`：P1 直接 `print` 到 stderr 的寫法任何 sink 都看不到。P1 必須以模組常數 `ERROR_TEST_DEVICE_ALLOWLIST_REJECTED = "TEST_DEVICE_ALLOWLIST_REJECTED"` 經 `raise RuntimeError(ERROR_TEST_DEVICE_ALLOWLIST_REJECTED)`（`reason` 另以例外屬性或第二個參數傳遞）送到 daemon 的 stderr 格式化點，讓既有 `call_arg RuntimeError` sink（`implies_interface: proc`）擷取；若 P1 改用其他輸出方式，必須在同一 commit 於 `code_extraction.yaml` 新增對應 sink 並補 T2 synthetic 測試。
- `scope.methods`：RPC 碼列盤點 03 §4 矩陣中會回出該碼的 method；CLI-local 碼列 `CLI_CONTEXTS` 中的 context 字串（依盤點 01 §2.2、§2.4 與 03 §3.4 位置欄：`INTERNAL_ERROR` 需含 `event`、`daemon stop`、`connect`、`benches`、`remote`；`INVALID_ARGS` 需含 `daemon start`、`argparse-boundary`、`fallthrough`、`remote`）；PROC 碼列 `serialwrapd`。
- `scope.platforms`：預設 `[posix, windows]`；`NEEDS_SUDO`、`DAEMON_ALREADY_RUNNING_ELSEWHERE`、`SOCKET_UNAVAILABLE` 為 `[posix]`。
- `extra_fields`：照盤點「附帶欄位」逐字列鍵名（例：`AUTOBOOT_QUIET`＝`[retry_after_s, hint, session, selector]`、`INVALID_RULE_SCHEMA`＝`[error]`、`LOG_OPEN_FAILED`＝`[detail]`、`WAL_MISSING`＝`[wal_path, wal_dir, wal_dir_exists, wal_file_exists, current_seq, hint]`、`DEVICE_STILL_HELD`＝`[pids, selector]`）。
- `exit_codes`：`cli_local` 碼依盤點 03 §3.4 exit 欄與 01 §2.4，鍵為 `CLI_CONTEXTS` 中的 context，每個出現該碼的 context 都要有一筆（例：`REMOTE_NOT_SUPPORTED`＝`{"daemon start": 2, "connect": 1, "benches": 1, "remote": 1}`、`INVALID_COMPLETION_REGEX`＝`{"cmd submit": 1}`、`SAMPLE_READ_FAILED`＝`{"profile test": 0}`、`UNKNOWN_PROFILE`＝`{"profile test": 0}`）。
- `side_effect`：照盤點「副作用」欄寫實；無副作用寫「無」；`PROMPT_TIMEOUT` 必須寫出可能已送 `\x03`、`\x04` 並轉 ATTACHED、flush 佇列；`ALIAS_NOT_FOUND` 寫「失敗前仍寫入 state.json」。
- `behavior_change`：`COMMAND_ABORTED_IN_FLIGHT`＝`[BC-03]`；`TEST_DEVICE_ALLOWLIST_REJECTED`＝`[SC-19]`；`DEVICE_STILL_HELD`＝`[BC-04, SC-05]`（將加 `holder_unknown`）；`SESSION_RECOVERING`＝`[SC-01, SC-04]`；`FLASHING_BUSY`＝`[SC-02]`；`AUTOBOOT_QUIET`＝`[SC-03]`；`INVALID_RESPONSE`＝`[DF-06]`；`INTERNAL_ERROR`＝`[DF-07]`；`DAEMON_EXITED`＝`[DF-05]`；`REMOTE_NOT_SUPPORTED`＝`[SC-27, BC-05]`；`SEND_FAILED`＝`[BC-03]`（沿用為「結果不明」映射，ERR-7.3-9）；其他空 list。
- `contract_tests`：填入現有 `tests/` 中確實斷言該碼的測試（以 `grep -lw <CODE> tests/*.py` 找檔，再挑出斷言該碼的 test node）；盤點時查無測試的碼（T7 清單）本 Task 先留空，由 T7 補。T6 的 C09 會在 T7 完成前對這些碼 FAIL，屬預期。
- `meaning`／`agent_action`：繁中、單行、不得含 `|`；`agent_action` 要寫出 Agent 可執行的下一步（例：`READY_UNCONFIRMED`→「勿重送；執行 `serialwrap session self-test` 取分類，再依分類處理」）。
- `passthrough` 一筆：

```yaml
passthrough:
  - id: last_error_passthrough
    namespace_ref: session_last_errors
    scope: {interfaces: [rpc], methods: [session.attach, session.recover], platforms: [posix, windows]}
    rule: "session.attach 在 command-capable 且 state 不是 READY／ATTACHING、也不是 FLASHING／RECOVERING 時回 error_code=last_error；session.recover 的 REPROBE 分支未恢復時以 ok:true 回 error_code=last_error 或 SESSION_NOT_READY"
    owner_confirmation: pending_default
    reconciliation_refs: [RPC-D-02, ERR-7.2-b, ERR-7.3-1, SES-16.5-02]
```

entry 範例（格式基準）：

```yaml
  - code: AUTOBOOT_QUIET
    status: EXISTING
    reachable: true
    constant: ERROR_AUTOBOOT_QUIET
    scope: {interfaces: [rpc, rec], methods: [command.submit, file.push, file.pull], platforms: [posix, windows]}
    meaning: session 名義 READY，但 boot quiet window 進行中或 READY 再確認尚未完成
    side_effect: 無（零 UART 副作用）
    retryable: "yes"
    uart_tx: none
    agent_action: 依 retry_after_s 等待後重送；manager 層回應無 retry_after_s 時以 5 秒為間隔重試
    extra_fields: [retry_after_s, hint, session, selector]
    contract_tests: [tests/test_boot_quiet.py]
    behavior_change: [SC-03]
    sources: ["sw_core/service.py:671", "sw_core/service.py:683", "sw_core/session_manager.py:1155"]
```

- [ ] **Step 1：寫失敗測試**（追加至 `tests/test_contracts_catalog.py`）

```python
def _ec():
    return _load().load_error_codes(ROOT)      # (codes, passthrough)；只讀 error_codes.yaml


def _ec_by_code():
    return {e.code: e for e in _ec()[0]}


def test_ec_real_catalog_loads():
    _ec()   # 不 raise 即通過


def _assert_status_tracks_change(entry, change_status: str | None):
    # 不凍結字面 APPROVED：依變更狀態推導（P1 翻轉後本測試仍成立）
    if change_status in (None, "PROPOSED", "APPROVED"):
        assert entry.status == "APPROVED" and entry.reachable is False
    else:
        assert entry.status in ("IMPLEMENTED", "VERIFIED") and entry.reachable is True and entry.contract_tests
```

（`change_status` 由測試以 `load_behavior_changes(ROOT)` 讀取；T3 時 `behavior_changes.yaml` 尚未建立，讀檔失敗時以 `None` 代入。T5 完成後同一測試改走真實狀態。）

| test 名稱 | 斷言 |
|---|---|
| `test_ec_credentials_unresolved_not_an_error_code` | `"CREDENTIALS_UNRESOLVED" not in _ec_by_code()` |
| `test_ec_passthrough_single_meta_entry` | `[p.id for p in passthrough] == ["last_error_passthrough"]` 且 `namespace_ref == "session_last_errors"`、`owner_confirmation ∈ {"pending_default","confirmed"}` |
| `test_ec_bc03_status_tracks_change` | `COMMAND_ABORTED_IN_FLIGHT`：`_assert_status_tracks_change(e, BC-03 狀態)`、`retryable == "no"`、`uart_tx == "possible"`、`"BC-03" in behavior_change`、`scope.interfaces == ("rec",)` |
| `test_ec_sc19_status_tracks_change` | `TEST_DEVICE_ALLOWLIST_REJECTED`：`_assert_status_tracks_change(e, SC-19 狀態)`、`scope.interfaces == ("proc",)`、`"SC-19" in behavior_change` |
| `test_ec_proc_only_codes` | `DAEMON_ALREADY_RUNNING`、`SOCKET_UNAVAILABLE` 的 `scope.interfaces == ("proc",)` |
| `test_ec_rec_codes` | `FLUSHED_BY_RECOVERY`、`FLUSHED_BY_SHUTDOWN`、`SEND_FAILED`、`COMMAND_FAILED` 的 interfaces 含 `rec` |
| `test_ec_frame_codes` | `INVALID_JSON`、`INVALID_REQUEST`、`INVALID_METHOD`、`EXCEPTION` 的 interfaces 含 `rpc_frame` |
| `test_ec_client_codes` | `TIMEOUT`、`SOCKET_ERROR`、`EMPTY_RESPONSE`、`INVALID_RESPONSE` 的 interfaces 含 `cli_client` |
| `test_ec_retryable_frozen_values` | `AUTOBOOT_QUIET=="yes"`、`READY_UNCONFIRMED=="no"`、`SEND_FAILED=="no"`、`TIMEOUT=="conditional"`、`FLUSHED_BY_RECOVERY=="yes"`、`INVALID_JSON=="fix_args"`、`INTERNAL_ERROR=="no"`、`DAEMON_NOT_READY=="conditional"` |
| `test_ec_uart_tx_gate_rejects_are_none` | `SESSION_NOT_READY`、`AUTOBOOT_QUIET`、`READY_UNCONFIRMED`、`CMD_TOO_LONG`、`CMD_CONTAINS_NEWLINE`、`SESSION_QUEUE_FULL`、`FLUSHED_BY_RECOVERY` 的 `uart_tx == "none"`；`PROMPT_TIMEOUT == "confirmed"`；`SEND_FAILED == "possible"` |
| `test_ec_unreachable_set` | `{c for c,e in by.items() if not e.reachable and e.status == "EXISTING"} == {"FLASH_TARGET_IS_CONSOLE","UNKNOWN_EVENT_CMD","COMMAND_FAILED"}`；且 `{c for c,e in by.items() if e.status == "APPROVED"} ⊆ {c for c,e in by.items() if not e.reachable}`（APPROVED 集合由資料推導，不寫死） |
| `test_ec_ok_response_codes` | `ok_response is True` 的碼其 `retryable == "no"` |
| `test_ec_shape_quirks_frozen` | `"error" in by["INVALID_RULE_SCHEMA"].extra_fields`、`"detail" in by["LOG_OPEN_FAILED"].extra_fields`、`"retry_after_s" in by["AUTOBOOT_QUIET"].extra_fields`、`"retry_after_s" not in by["READY_UNCONFIRMED"].extra_fields` |
| `test_ec_exit_codes_frozen` | `by["REMOTE_NOT_SUPPORTED"].exit_codes == {"daemon start": 2, "connect": 1, "benches": 1, "remote": 1}`、`by["INVALID_COMPLETION_REGEX"].exit_codes["cmd submit"] == 1`、`by["SAMPLE_READ_FAILED"].exit_codes == {"profile test": 0}`、`{"event","daemon stop","connect","benches","remote"} <= set(by["INTERNAL_ERROR"].exit_codes)`、`by["INVALID_ARGS"].exit_codes["argparse-boundary"] == 1` |
| `test_ec_non_codes_absent` | `{"CMD_LENGTH_WARNING","NOT_BOOTLOADER","INVALID_INTERACTIVE_ENCODING","EMPTY_COMMAND"}` 與目錄鍵交集為空 |
| `test_ec_constants_recorded` | `by["CMD_TOO_LONG"].constant == "ERROR_CMD_TOO_LONG"`、`by["INVALID_COMPLETION_REGEX"].constant == "ERROR_INVALID_COMPLETION_REGEX"` |

- [ ] **Step 2：執行並確認 FAIL**

Run: `python3 -m pytest -q tests/test_contracts_catalog.py -k "test_ec_"`
Expected: FAIL，原因為 `CatalogError`（`error_codes.yaml` 不存在；T1 已實作 loader，不得是 `ModuleNotFoundError`）。

- [ ] **Step 3：commit 測試**

```bash
git add tests/test_contracts_catalog.py
git commit -m "test(contracts): 新增 error_codes 目錄內容的失敗測試" -m "Co-authored-by: Copilot <223556219+Copilot@users.noreply.github.com>"
```

- [ ] **Step 4：實作** 依上述規則建立 `contracts/error_codes.yaml`（`codes` 依 code 升冪排序；檔頭兩行註解說明「計數由資料產生，不得硬編碼」與「新增碼須同步 behavior_changes.yaml 與 SKILL 受管區段」）。

- [ ] **Step 5：執行並確認 PASS**

Run: `python3 -m pytest -q tests/test_contracts_catalog.py`
Expected: T1、T3 範圍全部 PASS。另執行 T2 抽取器，確認抽出值都在目錄中（T6 C02 的前置自查）。

- [ ] **Step 6：全套與政策** 同 T1 Step 6。

- [ ] **Step 7：commit 實作**

`changelog.d/rust-p0-error-codes-catalog.md`：

```markdown
---
type: feat
scope: contracts
---
新增 `contracts/error_codes.yaml`：依 Python 基線凍結全部 RPC／CLI error_code 的意義、副作用、可重試性、Agent 處置、介面與附帶欄位，last_error 外流以單一 passthrough 項表示，BC-03 與 SC-19 新碼以 APPROVED（未生效）登錄。
```

```bash
git add contracts/error_codes.yaml changelog.d/rust-p0-error-codes-catalog.md
git commit -m "feat(contracts): 新增 error_code 權威目錄" -m "Co-authored-by: Copilot <223556219+Copilot@users.noreply.github.com>"
```

**BLOCKED-OWNER：** 擁有者確認清單 #1（RPC-D-02，last_error 以 error_code 外流）——本 Task 依預設「保留現況」凍結為 `owner_confirmation: pending_default`；擁有者確認後改 `confirmed` 並遞增 `revision`。

---

## Task T4：`contracts/session_last_errors.yaml` 與 `contracts/diagnostic_aux.yaml`

**Test author model:** agy-gemini-3.8-flash-high　**Implementer model:** haiku-xhigh

**Files:**
- Create: `contracts/session_last_errors.yaml`、`contracts/diagnostic_aux.yaml`
- Test: `tests/test_contracts_catalog.py`（追加 `test_le_real_*`、`test_aux_real_*`）
- Fragment: `changelog.d/rust-p0-last-error-catalog.md`

**Interfaces:**
- Consumes：T1 loader。
- Produces：`SessionLastError`（P3 生成，見「共用介面」）；輔助目錄供 P3 生成 `WarningCode`、`ErrorDetail`、`Classification`、`RecommendedAction`、`RecoveryAction`（arch §3.2 末條）。

**內容規則：**

- `values`＝盤點 03 §5.2 的 31 個具名值＋3 個動態前綴 `REPROBE_FAILED:*`、`ATTACH_FAILED:*`、`BRIDGE_DOWN:*`（`BRIDGE_DOWN:*` 的 `reason_forms: ["SERIAL_READ:<errno|ExcName>", "SELECT:<errno|ExcName>"]`；`REPROBE_FAILED:*`、`ATTACH_FAILED:*` 的 `reason_forms: ["<ExcClassName>"]`）。
- `reachable: false`：`USER_ENV_MISSING`（`unreachable_reason` 引用 03 §7.5 與調和表 ERR-7.5-a／GAP-X3）。
- `cleared_by`：照 03 §5.3 的 13 個清除點逐值填寫；自動清除寫 `auto:<描述>@session_manager.py:<行>`（例：`auto:自動重探 DETACHED→ATTACHING@session_manager.py:1463`、`auto:裝置在線立即清除@session_manager.py:1846`），只有操作者動作才清除者寫 `operator:<RPC method>`（例 `operator:session.attach`、`operator:session.recover`、`operator:device.attach`、`operator:daemon restart`）。
- `terminal` 定義＝「只有操作者明確動作才會解除，且不自動重探」，由 `cleared_by`／`auto_reprobe` 推導並受 T1 一致性規則約束：`cleared_by` 沒有任何 `auto:` 項、且 `auto_reprobe` 兩邊皆 false 者為 `true`，其餘 `false`。預期為 `true` 的集合（實作者以 `cleared_by` 推導後核對，不一致時以程式為準並在 commit body 說明）：`CREDENTIALS_UNRESOLVED`、`BOOTLOADER_STUCK`、`LOGIN_REQUIRED`、`REBOOTING`、`READY_NONCE_TIMEOUT`、`POST_LOGIN_CMD_TIMEOUT`、`PASS_ENV_REQUIRED`、`PASS_ENV_MISSING`、`LOGIN_USER_REQUIRED`、`USER_ENV_MISSING`、`CONSOLE_UNLOCK_IDLE_TIMEOUT`、`CONSOLE_UNLOCK_BOOTLOADER`、`RELEASED`、`DEVICE_REBOUND_REQUIRED`、`RECOVERY_ERROR`、`RECOVERY_TIMEOUT`。`CONSOLE_UNLOCK_PROMPT_TIMEOUT` **不是**終態（以 `_PROMPT_TIMEOUT` 結尾，屬自動重探資格）。`CLEARED`、`REBOUND`、`DEVICE_REMOVED`、`DEVICE_NOT_FOUND`、`ATTACH_FAILED:*`、`BRIDGE_DOWN:*` 等雖不自動重探，但有裝置事件的自動清除點，故為 `false`。
- `auto_reprobe`（03 §5.3 `_is_reprobe_prompt_error`，`session_manager.py:1332-1356`）：`PROMPT_UNAVAILABLE`＝`{attached: true, detached: false}`；`PROMPT_TIMEOUT`、全部以 `_PROMPT_TIMEOUT` 結尾的值（`LOGIN_PROMPT_TIMEOUT`、`BCM_PROMPT_TIMEOUT`、`SHELL_PROMPT_TIMEOUT`、`PRPL_PROMPT_TIMEOUT`、`READY_PROMPT_TIMEOUT`、`CONSOLE_UNLOCK_PROMPT_TIMEOUT`）、`RX_FLOOD`、`TRANSPORT_STALL`＝`{attached: true, detached: true}`；其餘皆 false。
- `promoted_via`：只有在 bridge 存在的 ATTACHED 態可達的值才會被提升（`session.attach` 經 `_attach_state_result`、`session.recover` 經 REPROBE 分支，兩者都需 bridge；盤點 03 §5.4）。`"ATTACHED" in states` 的值為 `[session.attach, session.recover]`；`states` 不含 `ATTACHED` 者（例：`CLEARED`、`RELEASED`、`REBOUND`、`DEVICE_REMOVED`、`DEVICE_REBOUND_REQUIRED`、`DEVICE_NOT_FOUND`、`DEVICE_REMOVED_DURING_ATTACH`、`ATTACH_FAILED:*`、`BRIDGE_DOWN:*`）為 `[]`——`recover_session` 對 RELEASED 早退、無 bridge 時回 `REATTACH` 或 `SESSION_NOT_READY`（`session_manager.py:4590-4616`），不會把它們放進 `error_code`；`USER_ENV_MISSING` 為 `[]`。
- `error_code_namespace`：`CREDENTIALS_UNRESOLVED` 為 `forbidden`（ADR-07）；其餘 `allowed_shared`（`PROMPT_TIMEOUT`、`DEVICE_NOT_FOUND` 同時是 error_code，屬既有共名）。
- `logging`（§9.3「一次性 log＋WAL 警告」兩半都要寫）：`CREDENTIALS_UNRESOLVED`＝「進入終態時一次性 WAL META `credentials_unresolved`（reason ∈ env_file_missing／env_file_unreadable／key_absent），並以 daemon logger 輸出一次 WARNING（模板逐字取自 `session_manager.py:1211` 的 format 字串），兩者皆以 credentials_warned 去重」；`TRANSPORT_STALL`＝「一次性 WAL META `transport_stall_suspected`（real_path、rx_age_s 取 1 位小數）與一次 WARNING（模板逐字取自 `session_manager.py:1311`），RX 恢復後重臂」；其餘「無」。WARNING 的去向、格式與完整訊息表屬 T16 `contracts/daemon_logging.yaml`，本欄的模板字串必須與該檔對應項逐字相同（T16 測試核對）。
- `detail`：依 03 §5.5——`TRANSPORT_STALL`＝「`transport_stall_hint(real_path, rx_age_s)` 的繁中文字；位元組級凍結於 `contracts/golden/last_error/transport_stall_hint.json`（T16，GAP-G-09）」；`LOGIN_FSM_DETAIL_ERRORS` 12 值＝「`clean_text(rx_tail())[-300:]`，空則 null」；`BOOTLOADER_STUCK`＝「`matched_prompt=<pattern>`」；其餘「null」。
- `states`、`set_by`：照 03 §5.2 兩張表「伴隨 state」與「產生點」。
- `manual_recovery`：`CREDENTIALS_UNRESOLVED`＝「補齊帳密後由使用者明確 `serialwrap session attach` 或 `session recover`，或重啟 daemon；不得改寫 state.json 假裝恢復」（§9.3）。
- `contract_tests`：填現有斷言該值的測試（例：`tests/test_credentials_unresolved.py`、`tests/test_transport_stall.py`、`tests/test_rx_flood.py`、`tests/test_login_fsm.py`）；查無者留空並列入 T7 第二表。
- `diagnostic_aux.yaml`：`warning_codes: [{code: CMD_LENGTH_WARNING, meaning: ...}]`；`error_details: [{value: NOT_BOOTLOADER, parent_code: SESSION_NOT_READY, meaning: ...}]`；`classifications`＝03 §6 的 16 個值（`SESSION_RECOVERING`、`HUMAN_INTERACTIVE_ACTIVE`、`DEVICE_MISSING`、`DEVICE_REBOUND_REQUIRED`、`BRIDGE_DOWN`、`VTTY_STALE`、`PASSTHROUGH`、`LOGIN_REQUIRED`、`REBOOTING`、`BOOTLOADER`、`RX_FLOOD`、`ATTACHED_NOT_READY`、`AUTOBOOT_QUIET`、`RELEASED`、`TARGET_UNRESPONSIVE`、`OK`），`recommended_actions` 為 list，照該節括號內值拆開（例 `attach／recover` → `[attach, recover]`）；`recommended_actions`（頂層清單，供 P3 生成 `RecommendedAction`）＝以 `grep -n '"recommended_action"' sw_core/*.py` 找出的**全部**輸出點的值聯集（含 classification 以外的 `self_test`、`device_attach`、`wait_external_flash`、`pin_profile`、`recover_interactive` 等），每個值附 meaning，並在 commit body 列出每個值的來源行；`recovery_actions`＝`PROMOTE_HUMAN_INTERACTIVE`、`EXPECT_REBOOT`、`CTRL_C`、`CTRL_D`、`FORCE_CLEAR_REATTACH`、`NONE`；`recover_actions`＝`REATTACH`、`REPROBE`、`FORCE_CLEAR_REATTACH`。

- [ ] **Step 1：寫失敗測試**（追加）

| test 名稱 | 斷言 |
|---|---|
| `test_le_real_dynamic_prefixes` | `{v.value for v in cat.last_errors if v.kind == "dynamic_prefix"} == {"REPROBE_FAILED:*","ATTACH_FAILED:*","BRIDGE_DOWN:*"}` |
| `test_le_real_credentials_unresolved_frozen` | 該項 `terminal is True`、`auto_reprobe_attached is False`、`auto_reprobe_detached is False`、`error_code_namespace == "forbidden"`、`"credentials_unresolved" in logging`、`"session attach" in manual_recovery` |
| `test_le_real_user_env_missing_unreachable` | `reachable is False` 且 `unreachable_reason` 非空 |
| `test_le_real_prompt_unavailable_reprobe_attached_only` | `auto_reprobe_attached is True`、`auto_reprobe_detached is False` |
| `test_le_real_transport_stall_detail_frozen` | `"transport_stall_hint" in detail`、`"transport_stall_suspected" in logging`、`"WARNING" in logging`（位元組級 golden 由 T16 核對） |
| `test_le_real_bootloader_stuck_detail` | `"matched_prompt=" in detail` |
| `test_le_real_promoted_attach_subset` | `PROMPT_UNAVAILABLE`、`CREDENTIALS_UNRESOLVED`、`RX_FLOOD` 的 `promoted_via == ("session.attach","session.recover")`；`CLEARED`、`RELEASED`、`REBOUND` 的 `promoted_via == ()` |
| `test_le_real_reprobe_matches_runtime_rule` | 參數化全部 named 值與 `{"ATTACHED","DETACHED"}`：以 `SessionManager([], WalWriter(wal_dir=tmp), on_ready=..., on_detached=...)._is_reprobe_prompt_error(state, value)`（L1 白箱，函式內 import `sw_core`）等於 `auto_reprobe_attached`／`auto_reprobe_detached` |
| `test_le_real_console_unlock_prompt_timeout_not_terminal` | `CONSOLE_UNLOCK_PROMPT_TIMEOUT`：`terminal is False`、兩邊 `auto_reprobe` 皆 True |
| `test_le_real_cleared_by_present` | 每個值 `cleared_by` 非空；`CREDENTIALS_UNRESOLVED` 的 `cleared_by` 只含 `operator:` 項，且含 `operator:session.attach` 與 `operator:session.recover` |
| `test_le_real_not_in_error_code_namespace` | 所有 `error_code_namespace == "forbidden"` 的值都不在 `load_error_codes(ROOT)` 的碼中 |
| `test_aux_real_classifications` | `len(set(...)) == len(...)`（唯一）、含 `"BOOTLOADER"` 且其 `recommended_actions == ("recover_interactive",)`、含 `"OK"` 且 `recommended_actions == ("none",)`；每個 classification 的 action 都在 `aux.recommended_actions` 中 |
| `test_aux_real_recommended_actions_cover_emitters` | AST 掃 `sw_core/*.py` 所有 `{"recommended_action": <Constant>}` 字面與 `x["recommended_action"] = <Constant>` 的值 ⊆ `aux.recommended_actions` 值集合 |
| `test_aux_real_warning_not_error_code` | `CMD_LENGTH_WARNING` 在 `aux.warning_codes`，不在 `error_codes` |

- [ ] **Step 2：執行並確認 FAIL** — `python3 -m pytest -q tests/test_contracts_catalog.py -k "le_real or aux_real"`；Expected：FAIL（檔案不存在）。
- [ ] **Step 3：commit 測試** — `git commit -m "test(contracts): 新增 session last_error 與輔助目錄的失敗測試"`（含 Copilot trailer，格式同 T1）。
- [ ] **Step 4：實作** 兩個 YAML 檔（值升冪排序；檔頭註解說明與 `error_codes.yaml` 不共用計數）。
- [ ] **Step 5：執行並確認 PASS** — 同 Step 2 指令；Expected：PASS。
- [ ] **Step 6：全套與政策** — 同 T1。
- [ ] **Step 7：commit 實作**

`changelog.d/rust-p0-last-error-catalog.md`：

```markdown
---
type: feat
scope: contracts
---
新增 `contracts/session_last_errors.yaml`（31 個具名值＋3 個動態前綴的終態性、自動重探、手動恢復、記錄與 detail 規則）與 `contracts/diagnostic_aux.yaml`（warning、detail、self-test classification 與 recovery action 目錄），與 RPC error_code 分屬不同命名空間。
```

`git add contracts/session_last_errors.yaml contracts/diagnostic_aux.yaml changelog.d/rust-p0-last-error-catalog.md`；`git commit -m "feat(contracts): 新增 session last_error 與診斷輔助目錄" -m "Co-authored-by: Copilot <223556219+Copilot@users.noreply.github.com>"`

**BLOCKED-OWNER：** 無。

---

## Task T5：`contracts/behavior_changes.yaml`（BC／SC／DF 登記）

**Test author model:** agy-gemini-3.8-flash-high　**Implementer model:** haiku-xhigh

**Files:**
- Create: `contracts/behavior_changes.yaml`
- Test: `tests/test_contracts_catalog.py`（追加 `test_bc_real_*`）
- Fragment: `changelog.d/rust-p0-behavior-changes.md`

**Interfaces:**
- Consumes：T1 loader；調和表 §1、§3。
- Produces：P1 依 `case_ids` 撰寫 Stage 0 證據；P2 的 `tests/acceptance/cases.yaml` 必須收錄本檔全部 `case_ids`（P2 以測試核對，見「下游計畫須同步的修訂」）；`safety_invariants` 的 ID 供 P2 `cases.yaml` 的 `invariants` 欄引用；`ab_participation` 供 P9 A/B 判定；`approved_at_revision` 為 catalog revision（release manifest 的 `contract_revision` 是 build 時計算的 tree SHA，不在本檔）。

**共通欄位值：**

- `approval.date: "2026-10-15"`；`approval.owner` 依 basis 填實際核准者（`APPROVAL_OWNER_BY_BASIS`），不得一律寫 repo 擁有者：
  - BC：`approval: {basis: owner_decision, owner: hamanpaul, decision_ref: OD-04}`、`owner_confirmation: not_required`。
  - SC-19：`approval: {basis: owner_decision, owner: hamanpaul, decision_ref: OD-11}`（擁有者裁決）。
  - 其他 SC：`approval: {basis: spec_final, owner: spec-final, decision_ref: "serialwrap-rust-spec-final.md <§>"}`（§ 取調和表「規格依據」欄）。
  - DF：`approval: {basis: reconciliation, owner: spec-reconciliation, decision_ref: "spec-reconciliation.md §1.2"}`。調和表檔頭明定「除文末擁有者確認清單外，不需再向擁有者確認」，因此 SC／DF 不另列擁有者 ack；擁有者確認清單 #3～#7 所涉項目以 `owner_confirmation: pending_default` 表示。
- `status`：BC、SC、DF 全部為 `APPROVED`（BC 由 OD-04、SC-19 由 OD-11、其他 SC 由定稿規格、DF 由調和表核准；沒有 `PROPOSED` 項）。之後由 P1／P3+ 依「狀態推進協定」改為 `IMPLEMENTED`，`VERIFIED` 只由擁有者標記。
- `scope`：調和表「雙邊」→`both`、「Rust-only」→`rust_only`；P1 T18 若把 SC-25 改為「Python 僅在 bundle 內偵測到 manifest 時」，改值為 `python_when_manifest_present`（本 Phase 先依調和表填 `rust_only`）。
- `ab_participation`：`scope == "both"` → `diff`；其他 → `absolute_only`（P9 `10-stage3-canary.md` 以此判定 A/B 差異是否為已登記差異）。
- `applies_to.python`：`scope: both` 為 `{min_version: "0.4.0"}`——這是**目標 release 版號**，不代表 `VERSION` 已是 0.4.0（目前 `VERSION`＝`0.3.2`）。版號 bump 不在 P0／P1 feature PR 內做（會觸發 R-07），由 Stage 0 release PR 帶 `release:0.4.0` label 一次 bump；若選定不同版號須回改本檔並遞增 `revision`。`rust_only` 為 `null`。
- `applies_to.rust.stage`：phases 中最早的 Rust phase：P3→`stage-1a`、P4→`stage-1b`、P5→`slice-2.1`、P6→`slice-2.2`、P7→`slice-2.3`、P8→`slice-2.4`；phases 只有 P1／P2 的項目為 `stage-1a`（Rust CLI 起即承接）。
- `approved_at_revision: 1`；檔案 `revision: 1`。檔內不寫 `contract_revision`。
- `compatibility_impact`：寫出對既有 agent／L2／L3 的可觀測差異（例 SC-01：「`RECOVERING` 下 submit 由 `SESSION_NOT_READY` 改為 `SESSION_RECOVERING`；依碼分支的 agent 需新增處理」）。
- `approval_conditions`：BC 照規格 §9.1「核准範圍與必要驗證」欄逐字；SC／DF 寫調和表該列「理由」。SC-22 另寫調和表 SC-22 列的交接護欄條文（R1-F3：fail-closed 安全檢查、`phase:"supervision_set_safety_check"`、停止只經 `daemon.handoff_stop{reason:"supervision_set"}`、`--force` 不覆寫），`spec_refs` 取該列「規格依據」`["§4.2 #16","§6.1","§12.1"]`。

**BC 項（逐字）：**

| id | title | spec_refs | reconciliation_refs | phases | case_ids | related_decisions |
|---|---|---|---|---|---|---|
| BC-01 | WAL 常駐 handle、每筆 flush、Windows share-delete、自癒與缺口記錄 | `["§9.1","§8.2","ADR-05"]` | `[WAL-D5, WAL-D6, WAL-1.4b, UIO-12.4-a]` | `[P1, P5]` | `bc01-wal-resident-handle`、`bc01-wal-flush-per-record`、`bc01-wal-rotate-normal`、`bc01-wal-external-unlink-meta`、`bc01-wal-external-reader-handle`、`bc01-wal-recreate-failure-stops-tx`、`bc01-f12-oracle-progress` | `[OD-04, OD-05, OD-13]` |
| BC-02 | DeferredBuffer 溢位或跨上下文時整段清空 | `["§9.1","§5.2.6"]` | `[SES-16.1, UIO-12.5, SES-16.4-05, UIO-12.9-c]` | `[P1, P6]` | `bc02-deferred-overflow-clear`、`bc02-deferred-context-switch-clear`、`bc02-mailbox-stale-input-not-replayed`、`bc02-normal-replay-53` | `[OD-04]` |
| BC-03 | 新增 `COMMAND_ABORTED_IN_FLIGHT` | `["§9.1","§5.1.4"]` | `[SES-16.2, UIO-12.2-d, ERR-7.3-9]` | `[P1, P5]` | `bc03-aborted-in-flight-after-1byte`、`bc03-unknown-outcome-send-failed`、`bc03-no-auto-resend` | `[OD-04]` |
| BC-04 | Per-UART 獨占、衝突阻斷與 holder 診斷 | `["§9.1","§6.2"]` | `[RPC-D-08, ERR-7.1-h, UIO-12.1-a, UIO-12.1-b, UIO-12.1-c, UIO-12.1-d, PLT-D-13]` | `[P1, P5]` | `bc04-posix-exclusive-tiocexcl-flock`、`bc04-posix-close-clears-tiocexcl`、`bc04-device-still-held-pids`、`bc04-holder-unknown-proc-unreadable`、`bc04-windows-access-denied-holder-unknown`、`bc04-device-attach-sync-reacquire` | `[OD-04, OD-05, OD-08]` |
| BC-05 | Windows `remote -R/-L` | `["§9.1"]` | `[PLT-D-08]` | `[P1, P4]` | `bc05-windows-remote-reverse`、`bc05-windows-remote-local`、`bc05-process-lifecycle-stop-restart`、`bc05-three-topologies` | `[OD-04]` |

`bc04-posix-close-clears-tiocexcl`（R0-F3）：PTY master 持續開啟下，daemon 關閉 tty（PROBE→正式 bridge、release、TIOCEXCL 後的失敗路徑）後同一 slave 的一般 open 不得 EBUSY。證據：P1 T10 `test_close_clears_tiocexcl_reopen_with_master_open`、`test_post_lock_reject_clears_excl`、`test_release_then_external_plain_open_succeeds`、`test_dynamic_probe_then_bridge_reopen_native`；P5 Task 2 `cap_posix_pty__probe_close_reopen_with_master_open`、`cap_posix_pty__open_failure_after_tiocexcl_clears_excl`、`cap_posix_pty__release_then_external_plain_open_succeeds`。

**SC／DF 項：** 內容與 phases、scope 照調和表 §1.1、§1.2 與 §2 各列「階段」欄（「雙邊」→`both`，「Rust-only」→`rust_only`）。case_ids 逐字如下（每項一個）：

| id | case_id | id | case_id |
|---|---|---|---|
| SC-01 | `sc01-recovering-submit-open` | SC-15 | `sc15-console-ring-drop-oldest` |
| SC-02 | `sc02-flashing-submit-open` | SC-16 | `sc16-console-line-reject-log` |
| SC-03 | `sc03-quiet-retry-after-execute-file` | SC-17 | `sc17-windows-multi-open-scan` |
| SC-04 | `sc04-drain-recovering-release-timeout` | SC-18 | `sc18-windows-ondemand-autostart` |
| SC-05 | `sc05-device-attach-sync-exclusive` | SC-19 | `sc19-test-device-allowlist` |
| SC-06 | `sc06-pending-counts-canceled-running` | SC-20 | `sc20-daemon-restart` |
| SC-07 | `sc07-tx-wal-os-confirmed-bytes` | SC-21 | `sc21-remote-stop-alias` |
| SC-08 | `sc08-uds-perm-0600` | SC-22 | `sc22-supervision-mode-set` |
| SC-09 | `sc09-config-discovery-order` | SC-23 | `sc23-setup-rollback` |
| SC-10 | `sc10-lock-path-normalize-lockfileex` | SC-24 | `sc24-windows-task-service` |
| SC-11 | `sc11-ordered-shutdown` | SC-25 | `sc25-release-manifest-version-pairing` |
| SC-12 | `sc12-reconcile-wait-old-daemon` | SC-26 | `sc26-windows-event-handler` |
| SC-13 | `sc13-lease-expiry-tick` | SC-27 | `sc27-windows-connect-benches` |
| SC-14 | `sc14-flash-probe-eligibility` | | |
| DF-01 | `df01-rule-id-path-traversal` | DF-10 | `df10-system-scope-state-path` |
| DF-02 | `df02-human-source-spoof` | DF-11 | `df11-wsl-systemd-sudo-gate` |
| DF-03 | `df03-archive-name-collision` | DF-12 | `df12-setup-ran-field` |
| DF-04 | `df04-released-persist-restarts` | DF-13 | `df13-minicom-broker-rename` |
| DF-05 | `df05-daemon-locate-order` | DF-14 | `df14-startup-scan-rank-watch-order` |
| DF-06 | `df06-client-non-json-response` | DF-15 | `df15-rx-exact-wal-seq` |
| DF-07 | `df07-bad-config-yaml` | DF-16 | `df16-windows-skill-copy` |
| DF-08 | `df08-contains-flags-validate` | DF-17 | `df17-wal-mirror-retry-no-dup-seq` |
| DF-09 | `df09-registry-load-valueerror` | DF-19 | `df19-self-test-probe-quiet-fence` |

**SC-23／SC-18 的交接拉起抑制（R5-F1；外部可觀測、Rust-only）**：SC-23 的 `compatibility_impact` 必須寫明「update／rollback／回退／中斷復原（與 SC-22 `supervision-mode set`）送 `daemon.handoff_stop` 前先抑制監管拉起：systemd 模式寫入持久 drop-in `serialwrap.service.d/90-serialwrap-update-hold.conf`（`Restart=no`、`RefuseManualStart=yes`）並 `disable`，期間外部 `systemctl start serialwrap`／`serialwrap service start`／systemd 路由的 `daemon start`／`serialwrap-minicom` 的 `AUTO_START_DAEMON` 會被拒絕，完成、拒絕還原或 `setup` 復原後才撤銷並依原值 enable；updater 中斷時 hold 保留到下一次 `serialwrap setup`；windows-task 在更新期間維持 disabled（重新註冊的 XML `Enabled=false`）；偵測到交接後被拉起或手動啟動的實例會對它重新交接，忙碌時拒絕更新且不中止其操作；新增 phase `update_autostart_hold`／`update_autostart_release`（不新增碼）」，`approval_conditions` 附加「§6.1 不得讓新舊監管項同時拉起、§12.1 步驟 3 先停止該實例的監管拉起能力」。SC-18 的 `compatibility_impact` 必須寫明「`install-journal.json` 存在時不自動啟動、不重送，stderr 印 `serialwrap: on-demand 自動啟動暫停：更新／回滾進行中（install-journal.json 存在），請待 serialwrap setup 完成`；與交接停止共用 `<RUN_DIR>/autostart.lock`」。兩項 `case_ids` 不變（`sc23-setup-rollback`、`sc18-windows-ondemand-autostart` 由 P4 `p4_case_map.yaml` 綁到 05 Task 3／7／12 的 R5-F1 測試）。

`owner_confirmation: pending_default` 的項目與 `owner_item`：SC-08→3、SC-04→4、DF-02→5、SC-26→6、SC-27→7；`default_if_pending` 照調和表 §3「預設」欄逐字。其餘 `not_required`。

`pending_owner_confirmations`（7 筆，照調和表 §3）：

```yaml
pending_owner_confirmations:
  - {item: 1, topic: "session.attach／recover 把 last_error（含 CREDENTIALS_UNRESOLVED）放進 error_code", refs: [RPC-D-02, ERR-7.2-b, ERR-7.3-1, SES-16.5-02], default: "保留現況；error_codes.yaml 只放一筆 passthrough 項、不計入 RPC 碼", affects: ["error_codes:passthrough:last_error_passthrough", "session_last_errors:CREDENTIALS_UNRESOLVED"]}
  - {item: 2, topic: "規格中「passthrough ATTACHED 可進 Arbiter」的例外", refs: [SES-16.4-03], default: "不啟用，維持 PROFILE_NOT_COMMAND_CAPABLE", affects: []}
  - {item: 3, topic: "UDS 權限", refs: [RPC-D-05, SC-08], default: "預設 0600、新建目錄 0700；system 模式明確指定 SERIALWRAP_SOCKET_GROUP 時 0660 加 chgrp", affects: [SC-08]}
  - {item: 4, topic: "release 遇 drain 逾時的回應", refs: [SC-04], default: "{ok:false, error_code:SESSION_RECOVERING, drain_stalled:true, drain_timeout_epoch:N, session:…}；回條遲到時仍完成 release，下次呼叫回 already_released", affects: [SC-04]}
  - {item: 5, topic: "RPC 帶入的 human: source", refs: [RPC-H-05, DF-02], default: "一律以 agent 對待；daemon 內部 human console 路徑不受影響", affects: [DF-02]}
  - {item: 6, topic: "Windows event handler", refs: [EVT-D-S6, SC-26], default: "shell 走 cmd.exe /d /s /c；Job Object 處理 timeout；env 白名單加 SYSTEMROOT、COMSPEC、PATHEXT、TEMP、TMP、USERPROFILE", affects: [SC-26]}
  - {item: 7, topic: "BC-05 的 Windows remote 是否涵蓋 connect／benches", refs: [SC-27], default: "涵蓋，移除兩者在 Windows 的 REMOTE_NOT_SUPPORTED", affects: [SC-27]}
```

（`affects` 內 `session_last_errors:<value>` 形式需 T1 loader 支援解析；item 2 無變更項可影響，故為空 list。）

`safety_invariants`（§11.3 六項；ID 與 P2 `03-stage0-test-infra.md` 的 `invariants` 欄逐字相同）：

| id | title | error_codes | case_ids（本檔已有、可作證據者） |
|---|---|---|---|
| `zero_tx_on_reject` | 被拒絕且依契約應為零副作用的請求，外部 TX 必須為零 | `error_codes.yaml` 中 `uart_tx == "none"` 且介面含 `rpc` 或 `rec` 的全部碼（實作時由資料產生後貼入，T5 測試核對兩者相等） | `sc01-recovering-submit-open`、`sc02-flashing-submit-open`、`sc03-quiet-retry-after-execute-file`、`df19-self-test-probe-quiet-fence`（R5-F2） |
| `no_forbidden_interleave` | 不得將不同來源的 bytes 以契約禁止的方式交錯 | `[]` | `[]`（本檔無對應 case；由 P2 `cases.yaml` 以 invariants 欄承接） |
| `no_stale_context_replay` | Recovery／Release／Reboot／Lease 切換不得回放失效上下文的輸入 | `[FLUSHED_BY_RECOVERY, FLUSHED_BY_SHUTDOWN]` | `bc02-deferred-context-switch-clear`、`bc02-mailbox-stale-input-not-replayed` |
| `no_reopen_before_handoff` | 舊 I/O worker 未完成交接前，不得另開同一受管端點，亦不得回覆釋放成功 | `[DEVICE_STILL_HELD, SESSION_RECOVERING]` | `sc04-drain-recovering-release-timeout`、`bc04-device-attach-sync-reacquire`、`sc05-device-attach-sync-exclusive` |
| `no_auto_resend` | 已有或可能有裝置副作用的命令不得自動重送 | `uart_tx ∈ {possible, confirmed}` 的全部碼（同上由資料產生） | `bc03-no-auto-resend`、`bc03-unknown-outcome-send-failed` |
| `insufficient_evidence_not_pass` | 證據不足、擷取失敗或測試未完成不得轉為 PASS | `[]` | `bc01-f12-oracle-progress` |

- [ ] **Step 1：寫失敗測試**（追加）

| test 名稱 | 斷言 |
|---|---|
| `test_bc_real_five_bcs_registered` | `{c.id for c in changes if c.kind == "BC"} == {"BC-01","BC-02","BC-03","BC-04","BC-05"}`，每筆 `status != "PROPOSED"`（不凍結字面 APPROVED；P1 翻成 IMPLEMENTED 後仍成立）、`approval.decision_ref == "OD-04"`、`approval.date == "2026-10-15"`、`approval.owner == "hamanpaul"` |
| `test_bc_real_approval_owner_by_basis` | 每筆 `approval.owner == APPROVAL_OWNER_BY_BASIS[approval.basis]`；SC-19 `basis == "owner_decision"`、`decision_ref == "OD-11"`；所有 DF `basis == "reconciliation"` |
| `test_bc_real_bc_case_ids_frozen` | BC-03 的 `case_ids == ("bc03-aborted-in-flight-after-1byte","bc03-unknown-outcome-send-failed","bc03-no-auto-resend")`；BC-01 含 `"bc01-f12-oracle-progress"`；BC-01 `related_decisions` 含 `"OD-13"` |
| `test_bc_real_sc_df_ranges` | SC id 集合等於 `{f"SC-{i:02d}" for i in range(1, 28)}`；DF 集合等於 `{f"DF-{i:02d}" for i in range(1, 18)} | {"DF-19"}`（範圍取自調和表 §1 的編號區間，屬資料而非計數政策；DF-18 保留給 P7 `08` §0.5，P7 Task 0 登記後改為 `range(1, 20)`，R5-F2）；DF-19 另斷言 `scope == "both"`、`phases == ["P1", "P5", "P7"]`、`case_ids == ("df19-self-test-probe-quiet-fence",)`、`reconciliation_refs` 含 `"SES-16.6-08"`、`applies_to.rust.stage == "slice-2.1"` |
| `test_bc_real_rust_only_python_null` | `scope == "rust_only"` 者 `applies_python_min is None`；SC-15、SC-17、SC-18、SC-22、SC-23、SC-24、SC-26 皆為 `rust_only`；SC-25 的 `scope ∈ {"rust_only","python_when_manifest_present"}` |
| `test_bc_real_sc22_switch_guard_not_force_overridable`（R1-F3） | SC-22：`scope == "rust_only"`、`"§12.1"` 在任一 `spec_refs` 元素內；`approval_conditions` 同時含 `daemon.handoff_stop`、`supervision_set_safety_check` 與 `--force`；`compatibility_impact` 含 `force_ignored`（內容：「`supervision-mode set` 在 Flash、FileTransfer、命令、lease、recovering、身分不明或探測失敗時拒絕切換（`phase:supervision_set_safety_check`，既有碼），`--force` 不覆寫、只回 `force_ignored:true`；停止舊 daemon 只經 `daemon.handoff_stop`，不支援交接的舊 daemon 無法被切換」）；`case_ids == ("sc22-supervision-mode-set",)` 不變 |
| `test_bc_real_ab_participation` | 每筆 `ab_participation == ("diff" if scope == "both" else "absolute_only")`（P9 消費此欄） |
| `test_bc_real_no_contract_revision_key` | 原始 YAML 文字不含 `contract_revision:` |
| `test_bc_real_safety_invariants` | `[i.id for i in safety_invariants] == list(SAFETY_INVARIANTS)`；`zero_tx_on_reject.error_codes` 等於 `{c for c in error_codes if c.uart_tx == "none" and {"rpc","rec"} & set(c.scope.interfaces)}`；`no_auto_resend.error_codes` 等於 `uart_tx ∈ {possible, confirmed}` 的碼集合 |
| `test_bc_real_pending_items` | `{c.id: c.owner_item for c in changes if c.owner_confirmation == "pending_default"} == {"SC-08": 3, "SC-04": 4, "DF-02": 5, "SC-26": 6, "SC-27": 7}`；`[o.item for o in owner_confirmations] == [1,2,3,4,5,6,7]` |
| `test_bc_real_case_ids_globally_unique` | 全部 case_ids 無重複 |
| `test_bc_real_error_code_refs_resolve` | `error_codes.yaml` 每個 `behavior_change` id 都存在於本檔（T6 C10 的前置） |
| `test_bc_real_sc23_sc18_autostart_hold_registered`（R5-F1） | SC-23 `compatibility_impact` 同時含子字串 `RefuseManualStart=yes`、`Restart=no`、`90-serialwrap-update-hold.conf`、`重新交接`、`update_autostart_hold`；SC-23 `approval_conditions` 含 `§12.1 步驟 3`；SC-18 `compatibility_impact` 含 `install-journal.json` 與 `autostart.lock`；兩者 `scope == "rust_only"`、`case_ids` 分別仍為 `("sc23-setup-rollback",)`、`("sc18-windows-ondemand-autostart",)` |

- [ ] **Step 2：執行並確認 FAIL** — `python3 -m pytest -q tests/test_contracts_catalog.py -k bc_real`；Expected：FAIL（檔案不存在）。
- [ ] **Step 3：commit 測試** — `git commit -m "test(contracts): 新增行為變更登記的失敗測試"`（含 trailer）。
- [ ] **Step 4：實作** `contracts/behavior_changes.yaml`（`changes` 依 BC→SC→DF、編號升冪排列；所有日期、版本字串加引號）。
- [ ] **Step 5：執行並確認 PASS** — 同 Step 2；並跑整個 `tests/test_contracts_catalog.py` 全 PASS。
- [ ] **Step 6：全套與政策** — 同 T1。
- [ ] **Step 7：commit 實作**

`changelog.d/rust-p0-behavior-changes.md`：

```markdown
---
type: feat
scope: contracts
---
新增 `contracts/behavior_changes.yaml`：登記 OD-04 核准的 BC-01～BC-05（核准者、日期、目標版本、核准時 catalog revision、案例 ID、A/B 參與方式、相容性影響、核准條件），以及依定稿規格推導的 SC-01～SC-27 與缺陷修正 DF-01～DF-17、DF-19（self-test nonce probe 提交點 quiet 重驗；DF-18 保留給 P7）、§11.3 六項安全不變量對照，另列 7 項待擁有者確認事項與預設值。
```

`git add contracts/behavior_changes.yaml changelog.d/rust-p0-behavior-changes.md`；`git commit -m "feat(contracts): 新增 BC/SC/DF 行為變更登記" -m "Co-authored-by: Copilot <223556219+Copilot@users.noreply.github.com>"`

**BLOCKED-OWNER：** 擁有者確認清單 #1～#7（調和表 §3）。本 Task 以 `pending_default` 凍結預設值；擁有者逐項確認後改 `confirmed`、遞增檔案 `revision`，並把受影響項的 `approved_at_revision` 更新為新 revision。

---

## Task T6：契約驗證器 CLI（目錄↔抽取↔契約測試↔交叉參照↔文件）

**Test author model:** haiku-xhigh　**Implementer model:** agy-gemini-3.8-flash-high

**Files:**
- Create: `tools/contracts/validate.py`、`tools/contracts/cli.py`、`tools/contracts/__main__.py`
- Test: `tests/test_contracts_validate.py`
- Fragment: `changelog.d/rust-p0-contract-validator.md`

**Interfaces:**
- Consumes：T1 `load_catalogs`、T2 `extract`／`load_extraction_config`；T11 `skill_section.render`（C14 使用，T11 前 C14 以 import 失敗回報 FAIL）；`.project-policy.yml`（C13、C14）。
- Produces：「共用介面」`validate.py`、`cli.py` 全部名稱；`python3 -m tools.contracts` CLI（T12 的 preflight、R-26／R-27 宣告與 P2／P3 CI 使用）。

**檢查定義（finding 的 `check` 欄固定使用以下代號）：**

| 代號 | 規則 | severity |
|---|---|---|
| C01 | `load_catalogs` 失敗：每個 problem 一筆 | FAIL |
| C02 | 抽出值（含動態前綴；error_code 另含 passthrough id）不在對應目錄 → 「實作輸出未登錄的碼」 | FAIL |
| C03 | 抽出值對應的 entry `status == "APPROVED"` → 「已核准未實作的碼不得生效；實作後改 IMPLEMENTED」 | FAIL |
| C04 | entry `reachable: true` 且 `status ∈ EFFECTIVE_STATUS` 但未被抽出 → 「目錄過期」 | FAIL |
| C06 | emission `implies_interface == "proc"` 而 entry `scope.interfaces` 不含 `proc` | FAIL |
| C07 | `res.unresolved` 每筆；`res.stale_allow_unresolved` 每筆 | FAIL |
| C08 | `res.stale_exclude_values` 每筆 | FAIL |
| C09 | entry `reachable: true` 且 `status ∈ EFFECTIVE_STATUS` 但 `contract_tests` 為空；`contract_tests` 指向的檔案不存在；`::` 後的 node 路徑（`Class::func` 或 `func`，參數化 id `[...]` 先去掉）在該檔 AST 中找不到對應的 `def`／`class`；該 node 本體內（只看 AST 字串常數與識別字，**不看註解與 docstring**）找不到該碼（`\b<CODE>\b`；動態前綴取 `*` 前字面）。只寫檔名、不帶 node 時，要求檔內至少一個 `test*` 函式本體含該碼 | FAIL |
| C10 | `behavior_change` id 不存在於 `behavior_changes.yaml`；`status == "APPROVED"` 的碼所引用的變更沒有任何一筆 `status ∈ {APPROVED, IMPLEMENTED, VERIFIED}` | FAIL |
| C11 | `error_code_namespace == "forbidden"` 的 last_error 值出現在 `error_codes.yaml` | FAIL |
| C12 | 任一目錄值以 `HARNESS_` 開頭 | FAIL |
| C13 | `.project-policy.yml` 的 `diagnostic_codes` 每個 namespace：執行 `extract` 命令取得值，依 `entry_pattern` 在每份 `docs` 中找有非空 meaning 與 action 的條目；缺漏者每碼一筆（語意與 T13 R-27 相同） | FAIL |
| C14 | `.project-policy.yml` 的 `generated_facts` 中 `marker` 為 `error-codes`／`session-last-errors` 的項目：命令輸出與標記區塊（`<!-- BEGIN: generated-fact marker="..." -->`）`strip()` 後不一致 | FAIL |
| C15 | `contracts/rpc_schema.json` 的 `x-methods.*.errors`／`error_order` 有值不在目錄（passthrough 以 `@last_error_passthrough` 表示）；`contracts/cli_schema.json` 的 `x-exit-codes[*].codes` 與目錄 `exit_codes` **雙向**不一致（目錄有、schema 無，或 schema 有、目錄無；context 不在 `CLI_CONTEXTS`）；兩檔缺一 | FAIL |
| C16 | 狀態一致性：碼的 `behavior_change` 所引用變更全為 `PROPOSED`／`APPROVED` 時碼必須 `APPROVED`；任一為 `IMPLEMENTED`／`VERIFIED` 時碼必須 `IMPLEMENTED`／`VERIFIED`、`reachable: true`、`contract_tests` 非空（只對 `status != "EXISTING"` 的碼套用） | FAIL |
| C17 | `x-pending` 併入協定：`rpc_schema.json`／`cli_schema.json` 每筆 `x-pending` 必須有 `change` 且存在於 `behavior_changes.yaml`；該變更 `IMPLEMENTED`／`VERIFIED` 時該筆仍留在 `x-pending` → FAIL（訊息：「須併入正式欄位並刪除 x-pending 項」） | FAIL |
| C18 | `rpc_schema.json` 任一 `x-methods.*.result` 或 `$defs.*` 為空物件 `{}`、缺 `type`、`type: object` 卻無 `properties`；或 method 缺 `side_effects`／`error_order` | FAIL |
| C19 | 每筆 `applied_exclusions`（WARN，subject 為值，message 含 category 與 reason）；被排除的值同時存在於任一目錄 → FAIL | WARN／FAIL |

`summarize()` 回傳（鍵名逐字，所有數字由資料計算）：

```json
{"behavior_changes":{"BC":0,"DF":0,"SC":0,"by_status":{},"pending_owner_confirmations":0},
 "error_code":{"by_interface":{},"by_status":{},"by_uart_tx":{},"catalog_total":0,"effective":0,"emitted":0,"passthrough":0},
 "exclusions":[],
 "safety_invariants":{"<id>":{"case_ids":0,"error_codes":[]}},
 "session_last_error":{"catalog_total":0,"dynamic_prefix":0,"emitted":0,"named":0,"terminal":0}}
```

（`exclusions` 為 `[{"namespace","value","category","reason","count"}]`；`safety_invariants.<id>.error_codes` 為排序後碼清單，供 P2 `cases.yaml` 的 invariant 引用核對。）

- [ ] **Step 1：寫失敗測試**（`tests/test_contracts_validate.py`）

```python
from __future__ import annotations

import importlib
import json
import re
import subprocess
import sys
import textwrap
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def _load(name: str):
    if str(ROOT) not in sys.path:
        sys.path.insert(0, str(ROOT))
    return importlib.import_module(f"tools.contracts.{name}")


# Phase 出口測試 test_real_repo_contract_check_passes 由 T12 Step 1 追加到本檔（見 T12），
# 避免 T6～T11 期間長時間紅燈遮蔽各 Task Step 6 的全套結果。


def test_summary_counts_are_data_derived():
    cat_mod, ex_mod, val = _load("catalog"), _load("extract"), _load("validate")
    cat = cat_mod.load_catalogs(ROOT)
    res = ex_mod.extract(ROOT, ex_mod.load_extraction_config(ROOT / "contracts" / "code_extraction.yaml"))
    s = val.summarize(cat, res)
    assert s["error_code"]["catalog_total"] == len(cat.error_codes)
    assert s["session_last_error"]["catalog_total"] == len(cat.last_errors)
    assert s["error_code"]["passthrough"] == len(cat.passthrough)


def test_no_hardcoded_78_anywhere_in_validator():
    for p in (ROOT / "tools" / "contracts").glob("*.py"):
        assert not re.search(r"\b78\b", p.read_text(encoding="utf-8")), p
```

每個檢查一個合成案例（在 `tmp_path` 建最小 repo：`contracts/` 四個 YAML、`contracts/code_extraction.yaml`、`pkg/*.py`、`tests/test_dummy.py`；直接呼叫對應 `check_*` 函式），斷言 `findings` 恰含一筆該代號且 `subject` 正確：

| test 名稱 | 合成情境 | 斷言 |
|---|---|---|
| `test_c02_emitted_not_in_catalog` | `pkg` 輸出 `GHOST_CODE`，目錄無 | 一筆 `C02`，subject `GHOST_CODE` |
| `test_c03_approved_but_emitted` | 目錄 `NEW_CODE` 為 APPROVED，`pkg` 輸出它 | 一筆 `C03` |
| `test_c04_effective_but_not_emitted` | 目錄 `OLD_CODE` EXISTING reachable，`pkg` 未輸出 | 一筆 `C04` |
| `test_c04_unreachable_not_flagged` | 同上但 `reachable: false` | 無 `C04` |
| `test_c06_proc_interface_missing` | `raise RuntimeError("DAEMON_X")`，目錄 interfaces `[rpc]` | 一筆 `C06` |
| `test_c07_unresolved_reported` | `{"error_code": unknown()}` | 一筆 `C07` |
| `test_c08_stale_exclude` | `exclude_values` 有未輸出的值 | 一筆 `C08` |
| `test_c09_missing_contract_test` | EXISTING reachable 碼 `contract_tests: []` | 一筆 `C09` |
| `test_c09_test_file_lacks_code` | `contract_tests: [tests/test_dummy.py]`，檔內無該碼 | 一筆 `C09` |
| `test_c09_node_does_not_exist` | `contract_tests: [tests/test_dummy.py::test_does_not_exist]`，檔內其他 test 含該碼 | 一筆 `C09`（node 不存在） |
| `test_c09_code_only_in_comment` | node 存在，但碼只出現在該函式的 `#` 註解與 docstring | 一筆 `C09` |
| `test_c09_code_in_other_test_only` | node `test_a` 不含碼，同檔 `test_b` 含碼 | 一筆 `C09` |
| `test_c09_class_node_and_param_id` | `tests/test_dummy.py::TestX::test_y[case1]`，`TestX.test_y` 本體含碼 | 無 `C09` |
| `test_c10_unknown_behavior_change` | `behavior_change: [SC-99]` | 一筆 `C10` |
| `test_c16_approved_code_with_implemented_change` | 碼 APPROVED、引用變更 IMPLEMENTED | 一筆 `C16` |
| `test_c16_implemented_code_with_approved_change` | 碼 IMPLEMENTED（reachable、有 tests）、引用變更 APPROVED | 一筆 `C16` |
| `test_c17_pending_not_folded` | `rpc_schema.json` 的 `x-pending` 有 `change: SC-04`，SC-04 為 IMPLEMENTED | 一筆 `C17` |
| `test_c17_pending_missing_change` | `x-pending` 項缺 `change` | 一筆 `C17` |
| `test_c18_empty_result_placeholder` | 某 method `"result": {}` | 一筆 `C18` |
| `test_c19_exclusion_warned` | `exclude_values` 有 `FOO`（非目錄值，有 emission） | 一筆 `C19` WARN，`run_all` 的 `ok` 仍為 true |
| `test_c19_exclusion_hides_catalog_value` | `exclude_values` 排除目錄中已有的 `OLD_CODE` | 一筆 `C19` FAIL |
| `test_run_all_without_policy_file` | 合成 repo 沒有 `.project-policy.yml` | 不 raise；policy 視為 `{}`，C13／C14 無 finding |
| `test_c11_forbidden_shared_name` | last_error `X_ONLY` forbidden，error_codes 也有 `X_ONLY` | 一筆 `C11` |
| `test_c12_harness_prefix` | 直接以 `catalog.Catalogs` 建構含 `HARNESS_X` 的物件 | 一筆 `C12` |
| `test_c13_doc_coverage_missing` | `check_doc_coverage(["A_CODE","B_CODE"], [doc], pattern)`，doc 只有 `A_CODE` 列 | 一筆 `C13`，subject `B_CODE` |
| `test_c13_empty_action_not_counted` | doc 中 `B_CODE` 列的 action 欄為空白 | `B_CODE` 仍有 `C13` |
| `test_c13_require_in_any` | 兩份 doc，`B_CODE` 只在其中一份，`require_in="any"` | 無 finding |
| `test_c14_generated_fact_drift` | `generated_facts` 命令輸出與標記區塊不同 | 一筆 `C14` |
| `test_cli_extract_lines_sorted` | `python3 -m tools.contracts extract --namespace error_code --format lines --repo <tmp>` | stdout 行已排序、無 passthrough id、exit 0 |
| `test_cli_check_exit_1_on_fail` | 合成 repo 含 C02 | exit 1、stdout JSON `ok is False` |
| `test_cli_bad_config_exit_1` | `code_extraction.yaml` 含未知 kind | exit 1、stderr 以 `contracts: ` 開頭 |

- [ ] **Step 2：執行並確認 FAIL** — `python3 -m pytest -q tests/test_contracts_validate.py`；Expected：FAIL，原因為 `ModuleNotFoundError: tools.contracts.validate`（T1、T2 模組已存在，若錯誤指向 `catalog`／`extract` 代表前置 Task 未完成）。
- [ ] **Step 3：commit 測試** — `git commit -m "test(contracts): 新增契約驗證器的失敗測試"`（含 trailer）。
- [ ] **Step 4：實作**

```python
def run_all(repo_root):
    findings = []
    try:
        cat = load_catalogs(repo_root)
    except CatalogError as exc:
        return [Finding("C01", "FAIL", "contracts", p) for p in exc.problems], {}
    res = extract(repo_root, load_extraction_config(repo_root / "contracts" / "code_extraction.yaml"))
    findings += check_catalog_against_emissions(cat, res)
    findings += check_extraction_hygiene(cat, res)
    findings += check_contract_tests(cat, repo_root)
    findings += check_cross_refs(cat)
    policy_path = repo_root / ".project-policy.yml"
    policy = (yaml.safe_load(policy_path.read_text(encoding="utf-8")) or {}) if policy_path.is_file() else {}
    for ns in policy.get("diagnostic_codes") or []:
        values = _run_extract_command(ns["extract"], repo_root)        # shlex.split、cwd=repo_root、LC_ALL=C、timeout 300
        findings += check_doc_coverage(values, [repo_root / d for d in ns["docs"]],
                                       ns["entry_pattern"], ns.get("require_in", "all"))
    findings += check_generated_facts(repo_root, policy)
    findings += check_schema_refs(cat, repo_root)
    return sorted(findings, key=lambda f: (f.check, f.subject, f.message)), summarize(cat, res)
```

`check_doc_coverage`：對每份 doc 以 `re.finditer(entry_pattern, text, re.MULTILINE)` 建 `code → (meaning.strip(), action.strip())`，只收 meaning 與 action 皆非空者；`require_in == "all"` 要每份 doc 都有，`"any"` 只要一份。`check_contract_tests` 以 `ast.parse` 解析測試檔，依 `::` 路徑找 `ClassDef`／`FunctionDef`，以 `ast.walk` 收集該 node 內 `Constant(str)` 與 `Name`／`Attribute` 識別字後比對（docstring 排除）。`ok` 只看 FAIL，WARN 不影響 exit code。`cli.py` 以 argparse 實作上表子命令；JSON 輸出用 `json.dumps(obj, ensure_ascii=False, sort_keys=True, separators=(",", ":"))`。

- [ ] **Step 5：執行並確認 PASS** — `python3 -m pytest -q tests/test_contracts_validate.py`。Expected：全部 PASS。另手動執行 `python3 -m tools.contracts check`，預期 exit 1，finding 只會是 C09（T7 未完成）、C13／C14（T11、T12 未完成）、C15／C17／C18（T8、T9 未完成）；出現其他代號代表 T1～T5 有缺漏，先回頭修正。
- [ ] **Step 6：全套與政策** — 同 T1。
- [ ] **Step 7：commit 實作**

`changelog.d/rust-p0-contract-validator.md`：

```markdown
---
type: feat
scope: contracts
---
新增 `python3 -m tools.contracts`（extract／check／summary／skill-section／effective）：比對程式抽出的碼與契約目錄、檢查契約測試（解析到 test node）、行為變更交叉參照與狀態一致性、`x-pending` 併入協定、命名空間分離、排除項稽核與 SKILL 文件覆蓋；所有計數由資料產生。
```

`git add tools/contracts/validate.py tools/contracts/cli.py tools/contracts/__main__.py changelog.d/rust-p0-contract-validator.md`；`git commit -m "feat(contracts): 新增契約驗證器 CLI" -m "Co-authored-by: Copilot <223556219+Copilot@users.noreply.github.com>"`

**BLOCKED-OWNER：** 無。

---

## Task T7：補齊缺少契約測試的錯誤碼（Python 基線特徵化測試）

**Test author model:** haiku-xhigh　**Implementer model:** agy-gemini-3.8-flash-high（填 `contract_tests` 參照，並處理特徵化測試揭露的盤點差異）

**Files:**
- Create: `tests/test_contracts_gap_codes.py`、`docs/rust-refactor/spec-errata.md`（本 Task 為唯一建立者：建立檔頭說明、「盤點勘誤」節與「規格勘誤」空節；T15 只追加）
- Modify: `contracts/error_codes.yaml`、`contracts/session_last_errors.yaml`（`contract_tests` 欄）
- Fragment: `changelog.d/rust-p0-gap-code-tests.md`

**Interfaces:**
- Consumes：`sw_core`（L1 白箱；本檔是允許 import `sw_core` 的特徵化測試）、T6 C09。
- Produces：每個生效碼至少一個契約測試，滿足 §9.2「每個生效 entry 必須具有對應契約測試」。

**說明：** 本 Task 是**特徵化**（characterisation）Task：這些測試鎖定 Python 現況，寫成後應**直接 PASS**；本 Task 的紅燈閘門是 T6 的 `python3 -m tools.contracts check --only C09` exit 1（`contract_tests` 為空），而非 pytest 紅燈。若某個特徵化測試對現況 FAIL，代表盤點描述與程式不符：以實際程式行為為準修正測試斷言，並在 `docs/rust-refactor/spec-errata.md`「盤點勘誤」節記一筆，不得改產品程式。

**error_code 缺測表**（盤點時 `grep -lw <CODE> tests/*.py func-test` 無結果者；每列一個 test）：

| test 名稱 | 準備 | 斷言 |
|---|---|---|
| `test_alias_not_found_saves_state_first` | `SessionManager([profile], WalWriter(wal_dir=tmp), on_ready=..., on_detached=...)`（照 `tests/test_attach_error_surface.py::_mgr`） | `mgr.unassign_alias("ghost") == {"ok": False, "error_code": "ALIAS_NOT_FOUND", "alias": "ghost"}`，且呼叫後 state 檔存在 |
| `test_base64_decode_failed_pull` | 照 `tests/test_file_transfer.py` 的 `_FakeBridge` 模式複製最小 fake，讓 pull 的 sentinel 區段內容為 `QQ`（padding 錯誤） | `resp["error_code"] == "BASE64_DECODE_FAILED"` 且本地檔不存在 |
| `test_console_not_found_detach_unknown_client` | READY session（照 `tests/test_service_human_console.py` 建 bridge fake） | `mgr.detach_console("COM0", "nope")["error_code"] == "CONSOLE_NOT_FOUND"`，回應含 `client_id == "nope"` |
| `test_device_not_bound_attach` | profile `device_by_id=None` | `r = mgr.attach_session("COM0")`；`r["ok"] is False`、`r["error_code"] == "DEVICE_NOT_BOUND"`、`"session" in r` |
| `test_framing_exception_bytes` | 下方 `_roundtrip`；handler `raise RuntimeError("kaboom")` | 回應行 `== b'{"id":1,"ok":false,"error_code":"EXCEPTION","message":"kaboom"}\n'` |
| `test_framing_invalid_json_bytes` | 送 `b"{bad\n"` | `== b'{"ok": false, "error_code": "INVALID_JSON"}\n'` |
| `test_framing_invalid_request_bytes` | 送 `b"[1]\n"` | `== b'{"ok": false, "error_code": "INVALID_REQUEST"}\n'` |
| `test_framing_invalid_method_bytes` | 送 `b'{"id":7}\n'` | `== b'{"id":7,"ok":false,"error_code":"INVALID_METHOD"}\n'` |
| `test_force_recover_timeout` | READY-capable session；`mock.patch` `clear_session` 為 no-op、`time.sleep` 為 no-op；session 永不 READY | `r = mgr.recover_session("COM0", force=True)`；`r["error_code"] == "FORCE_RECOVER_TIMEOUT"`、`r["action"] == "FORCE_CLEAR_REATTACH"`、`r["recovered"] is False` |
| `test_invalid_rule_schema_uses_error_key` | 以 service 層送 `event.rule_set`，`rule` 缺必要欄位（建構方式沿用 `tests/test_event_rpc.py` 與 `tests/test_daemon_service_selector.py` 的 service 測試替身） | `r["ok"] is False`、`r["error_code"] == "INVALID_RULE_SCHEMA"`、`"error" in r`、`"message" not in r` |
| `test_rule_not_found` | 同上，`event.rule_get` 不存在的 rule_id | `r["ok"] is False`、`r["error_code"] == "RULE_NOT_FOUND"` |
| `test_move_timeout_push` | `_FakeBridge`：chunk 與 md5 正常，`mv` 之後 prompt 不出現 | `r["error_code"] == "MOVE_TIMEOUT"`，且 fake 錄到的 TX 中 `mv` 之後沒有 `rm` 清理命令 |
| `test_not_ready_attach_state_result` | command-capable session，`s.state = "ATTACHED"`、`s.last_error = None` | `mgr._attach_state_result(s, s.to_public_dict())["error_code"] == "NOT_READY"` |
| `test_invalid_response_non_object` | 127.0.0.1 TCP 伺服器回 `b"[1]\n"` | `sw_core.client._rpc_call_once(f"tcp://127.0.0.1:{port}", "health.ping", {}) == {"ok": False, "error_code": "INVALID_RESPONSE"}` |
| `test_daemon_exited_on_spawn` | 照 `tests/test_cli_daemon_start.py` patch `Popen`，`poll()` 回 3 | stdout JSON `error_code == "DAEMON_EXITED"`、含 `pid`、`returncode == 3`；exit 2 |
| `test_invalid_bench_config_connect` | `SERIALWRAP_BENCHES_FILE` 指向缺欄位的 benches.yaml | `serialwrap connect X` stdout `error_code == "INVALID_BENCH_CONFIG"`、exit 1 |
| `test_unknown_bench_code_connect` | 合法 benches.yaml、未知 code `ZZ` | `error_code == "UNKNOWN_BENCH_CODE"`、`message == "unknown bench code: ZZ"`、exit 1 |
| `test_no_systemd_service_action` | `sw_core.service_ctl.service_action("start", mode="on-demand")` | `r["ok"] is False`、`r["error_code"] == "NO_SYSTEMD"`、`r["mode"] == "on-demand"`、`r["action"] == "start"`、`r["hint"]` 非空 |
| `test_tunnel_status_error_malformed` | patch `remote_tunnel.status` 回 `{"ok": True}`（缺 tunnels） | `serialwrap remote status` stdout `error_code == "TUNNEL_STATUS_ERROR"`、exit 1 |
| `test_command_failed_fallback` | `CommandArbiter(send_cb=lambda *a, **kw: {"ok": False})`，register、submit 後等 worker 完成 | `arb.get(cmd_id)["command"]["error_code"] == "COMMAND_FAILED"`、`status == "error"`（鍵路徑照 `arbiter.get` 實際回傳形狀） |
| `test_socket_unavailable_lock`（`posix_only`） | `SingletonLock(lock, sock)`，`sock` 路徑存在，patch `socket.socket.connect` 拋 `PermissionError(errno.EACCES, "denied")` | `pytest.raises(RuntimeError, match="^SOCKET_UNAVAILABLE$")` |
| `test_unknown_event_cmd_unreachable` | `sw_core.cli.build_parser().parse_args(["event", "bogus"])` | `SystemExit` 且 `code == 2`（證明 `UNKNOWN_EVENT_CMD` 不可達） |

framing 輔助（逐字放在檔內）：

```python
import asyncio


def _roundtrip(payload: bytes, handler) -> list[bytes]:
    from sw_core.rpc_posix import serve_connection

    async def main():
        server = await asyncio.start_server(
            lambda r, w: serve_connection(r, w, handler, set()), "127.0.0.1", 0)
        port = server.sockets[0].getsockname()[1]
        reader, writer = await asyncio.open_connection("127.0.0.1", port)
        writer.write(payload)
        await writer.drain()
        writer.write_eof()
        lines = []
        while True:
            line = await reader.readline()
            if not line:
                break
            lines.append(line)
        writer.close()
        server.close()
        await server.wait_closed()
        return lines

    return asyncio.run(main())
```

**last_error 缺測表**（`grep -lw <VALUE> tests/test_*.py` 無任何斷言者；只以字面參數出現、沒有斷言 last_error 值的也算缺測；每列一個 test，觸發方式依盤點 03 §5.2 產生點；setter 序列一律以 `mock.patch.object(SessionRuntime, "last_error", new_callable=PropertyMock)` 或包裝 setter 記錄寫入值）：

| test 名稱 | 觸發（盤點 03 §5.2 產生點） | 斷言 |
|---|---|---|
| `test_last_error_pass_env_required` | `login_fsm.py:112`：profile 設 `user_env` 但缺 `pass_env`，餵 password prompt | login 結果 `last_error == "PASS_ENV_REQUIRED"`、detail 為 rx tail 或 None |
| `test_last_error_login_user_required` | `login_fsm.py:219`：login prompt 出現但未設帳號 | `"LOGIN_USER_REQUIRED"` |
| `test_last_error_console_unlock_prompt_timeout` | `login_fsm.py:323`：console unlock 期間 prompt 永不出現（`time` 以 fake clock 推進） | `"CONSOLE_UNLOCK_PROMPT_TIMEOUT"`；並斷言 `_is_reprobe_prompt_error("ATTACHED", v)` 與 `("DETACHED", v)` 皆 True |
| `test_last_error_cleared` | `clear_session`（`session_manager.py:1841`），裝置不在線 | setter 序列含 `"CLEARED"`，最終值 `"CLEARED"`；裝置在線版本最終值為 `None` |
| `test_last_error_rebound` | `bind_session`（`:2063`），裝置不在線 | setter 序列含 `"REBOUND"`，最終值 `"DEVICE_NOT_FOUND"`；裝置在線版本最終值 `None` |
| `test_last_error_device_removed_during_attach` | `_attach_by_id` 進行中移除裝置（`:2700`） | `"DEVICE_REMOVED_DURING_ATTACH"`、state `DETACHED` |
| `test_last_error_recovery_error` | reboot recovery 中 `ensure_ready` 拋例外（`:3441`→`:3466`） | setter 序列含 `"RECOVERY_ERROR"` |
| `test_last_error_recovery_timeout` | reboot recovery 逾時、bridge 為 None（`:3490`）；另一案 bridge 存在且既有 `last_error="PROMPT_TIMEOUT"`（`:3493`） | 前者 `"RECOVERY_TIMEOUT"`＋`DETACHED`；後者保留 `"PROMPT_TIMEOUT"`＋`ATTACHED` |
| `test_last_error_reprobe_failed_prefix` | reprobe worker `_run` 中 probe 拋 `OSError`（`:1584`） | `last_error == "REPROBE_FAILED:OSError"`、state `ATTACHED` |
| `test_last_error_bridge_down_prefix` | `_handle_bridge_down` 收 `"SERIAL_READ:EIO"`（`:2453`），裝置不在線 | `last_error == "BRIDGE_DOWN:SERIAL_READ:EIO"`；裝置在線版本最終值 `None` |

T4 實作時若再發現 `contract_tests` 為空的值，於本表追加一列（同樣格式），不得只在實作時臨時補。`USER_ENV_MISSING`（`reachable: false`）不需測試。

- [ ] **Step 1：寫測試**（上兩表全部；本檔混合平台與資源，依 T14 規則**模組層不放 `pytestmark`**，每個 class 自帶一個平台 marker 與資源 marker：`TestGapMemory`（`core_portable`、`memory_only`）、`TestGapFramingTcp`（framing／TCP／subprocess 類，`core_portable`、`process_rpc`）、`TestGapPosixLock`（`test_socket_unavailable_lock`，`posix_only`、`memory_only`）、`TestGapLastError`（`core_portable`、`memory_only`））。同時建立 `docs/rust-refactor/spec-errata.md` 骨架。
- [ ] **Step 2：確認紅燈** — `python3 -m pytest -q tests/test_contracts_gap_codes.py` 預期 **PASS**（特徵化）；`python3 -m tools.contracts check --only C09` 預期 exit 1，finding 恰為上兩表的碼與值（尚未填 `contract_tests`）——多出或少於上表都要先查明。
- [ ] **Step 3：commit 測試** — `git commit -m "test(contracts): 新增缺測錯誤碼的基線特徵化測試"`（含 trailer）。
- [ ] **Step 4：實作** 在兩份目錄的對應 entry 填入 `contract_tests: [tests/test_contracts_gap_codes.py::<test 名稱>]`。
- [ ] **Step 5：確認 PASS** — `python3 -m tools.contracts check --only C09` exit 0。
- [ ] **Step 6：全套與政策** — 同 T1。
- [ ] **Step 7：commit**

`changelog.d/rust-p0-gap-code-tests.md`：

```markdown
---
type: change
scope: tests
---
為盤點時沒有任何測試斷言的 error_code 與 session last_error 補上 Python 基線特徵化測試（framing 位元組、alias、console、file transfer、bench、service、lock 等），使每個生效碼都有契約測試。
```

`git add tests/test_contracts_gap_codes.py docs/rust-refactor/spec-errata.md contracts/error_codes.yaml contracts/session_last_errors.yaml changelog.d/rust-p0-gap-code-tests.md`；`git commit -m "test(contracts): 為缺測錯誤碼補齊契約測試參照" -m "Co-authored-by: Copilot <223556219+Copilot@users.noreply.github.com>"`

**BLOCKED-OWNER：** 無（皆為 L1；不涉實機）。

---

## Task T8：`contracts/rpc_schema.json`

**Test author model:** agy-gemini-3.8-flash-high　**Implementer model:** haiku-xhigh

**Files:**
- Create: `contracts/rpc_schema.json`、`scripts/stage0/capture_rpc_line_limit.py`、`contracts/golden/rpc/line_limit.json`
- Modify: `.github/workflows/tests.yml`（兩個 pip install 步驟中第一個加 `jsonschema==4.10.3`）
- Test: `tests/test_contracts_rpc_schema.py`
- Fragment: `changelog.d/rust-p0-rpc-schema.md`

**Interfaces:**
- Consumes：盤點 02 §1、§5、§6、§7；03 §4；T3 目錄（C15）；`tests/test_daemon_service_selector.py` 的 service 測試替身。
- Produces：P3 `sw_proto::rpc_types::Method`（54 個基線 variant＋SC-11 `x-pending` 的 `daemon.handoff_stop`＝55 個，＋`Unknown(String)`）、`Method::is_retryable_readonly()`、`Method::uses_long_timeout()`；P5 framing 與 `SessionPublic::to_public` 欄位順序；`contracts/golden/rpc/line_limit.json`（P5 `06-slice21-daemon-core.md` 讀 `{"max_ok_bytes","first_close_bytes"}`，RPC-H-02）；`tests/contract_blackbox/` 的回應驗證。

**`contracts/golden/` 目錄慣例（本 Phase 建立，後續 Phase 沿用）：** `contracts/golden/<領域>/<名稱>.{json,ndjson}`；每個 golden 由 `scripts/stage0/capture_*.py`（或各 Phase 指定的 capture 腳本）對真實 Python 基線產生，檔內含 `"captured_by"`（腳本相對路徑）與 `"source"`（被量測的程式位置）；以 `DISK_STABLE`（`ensure_ascii=False, sort_keys=True, separators=(",",":")`＋`\n`）序列化；golden 不得手改，重新擷取須在 commit body 寫明原因與差異。golden 檔屬 `contracts/**`，改動需獨立審閱（§10.4）。

**檔案結構（JSON Schema Draft 2020-12；`x-` 開頭為本 repo 擴充鍵）：**

```json
{
  "$schema": "https://json-schema.org/draft/2020-12/schema",
  "$id": "https://github.com/hamanpaul/serialwrap/contracts/rpc_schema.json",
  "title": "serialwrap RPC contract",
  "x-catalog-revision": 1,
  "x-wire": {
    "framing": "ndjson",
    "line_strip": true,
    "empty_line": "ignored_no_response",
    "max_request_line_bytes": 65536,
    "max_request_line_unit": "bytes before LF（不含 LF；CRLF 時 CR 計入）",
    "line_limit_golden": "golden/rpc/line_limit.json",
    "over_limit": "close_without_response",
    "params_non_object": "coerced_to_empty_object",
    "key_order": ["id", "<handler insertion order>", "version"],
    "allow_nan": true,
    "compact_separators": [",", ":"],
    "early_responses": {
      "INVALID_JSON": "{\"ok\": false, \"error_code\": \"INVALID_JSON\"}\n",
      "INVALID_REQUEST": "{\"ok\": false, \"error_code\": \"INVALID_REQUEST\"}\n"
    },
    "version_absent_for": ["INVALID_JSON", "INVALID_REQUEST", "INVALID_METHOD", "EXCEPTION", "daemon.stop"]
  },
  "x-generic-errors": ["METHOD_NOT_FOUND", "EXCEPTION", "INVALID_JSON", "INVALID_REQUEST", "INVALID_METHOD"],
  "$defs": {
    "SessionPublic": {"type": "object", "x-order": ["session_id", "profile", "..."], "properties": {}, "required": []},
    "LeaseContext": {}, "CommandRecord": {}, "WalRecord": {"$ref": "wal_record.schema.json"},
    "DeviceRow": {}, "ConsoleRow": {}, "AliasRow": {}, "ErrorResponse": {}
  },
  "x-methods": {
    "health.ping": {"params": {}, "result": {"type": "object", "x-order": ["ok", "..."], "properties": {"ok": {"type": "boolean", "const": true}}, "required": ["ok"]},
                    "errors": [], "error_order": [], "side_effects": [],
                    "retryable_readonly": true, "blocking": false,
                    "long_timeout": false, "selector_chain": null, "status": "EXISTING", "source": "sw_core/service.py:706"}
  },
  "x-pending": [
    {"id": "SC-04", "change": "SC-04", "adds": {"SessionPublic": ["drain_stalled", "drain_timeout_epoch"]}, "position": "after capture, before console_endpoint"},
    {"id": "BC-04", "change": "BC-04", "adds": {"DEVICE_STILL_HELD": ["holder_unknown"]}},
    {"id": "SC-19", "change": "SC-19", "adds": {"health.status": ["test_device_allowlist"]},
     "shape": {"test_device_allowlist": {"type": "object", "properties": {"enabled": {"const": true}, "coms": {"type": "array", "items": {"type": "string"}}}, "required": ["enabled", "coms"], "additionalProperties": false}},
     "position": "after multi_open_detail；未啟用時不輸出此鍵",
     "env": "SERIALWRAP_TEST_DEVICE_ALLOWLIST（逗號分隔）", "config": "windows.test_device_allowlist", "precedence": "env > config",
     "isolation": "SERIALWRAP_STATE_DIR、SERIALWRAP_RUN_DIR、SERIALWRAP_CONFIG_DIR 三者皆明確設定且解析後都不等於預設路徑",
     "refusal": {"stderr": "serialwrapd: TEST_DEVICE_ALLOWLIST_REJECTED: <reason>\n", "exit": 2}},
    {"id": "SC-11-handoff", "change": "SC-11", "adds_method": "daemon.handoff_stop",
     "params": {"reason": {"type": "string", "x-coercion": "str(params.get(k) or D)", "default": "update"}},
     "result": {"ok": {"const": true}, "stopping": {"const": true}, "handoff_token": {"type": "string", "pattern": "^[0-9a-f]{32}$"}},
     "errors": ["FLASHING_BUSY", "SESSION_BUSY"], "error_order": ["FLASHING_BUSY", "SESSION_BUSY"],
     "busy_row": ["com", "state", "foreground_busy", "outstanding_commands", "interactive_session_id", "recovering", "ops"],
     "atomicity": "busy 判定、凍結所有新的有副作用操作與發 token 在 daemon 共同 admission 邊界的同一臨界區完成；busy 時不凍結、不停止；ok 時回應寫出後走 daemon.stop 同一 SC-11 停止路徑",
     "frozen_rejection": {"error_code": "FLUSHED_BY_SHUTDOWN", "reason": "handoff_frozen", "applies_to": "side_effects 非空且 retryable_readonly == false 的方法＋ttyMCU sync-probe（端點沉默）"},
     "version_absent": true, "retryable_readonly": false, "blocking": false, "long_timeout": false, "selector_chain": null}
  ]
}
```

（`SC-11-handoff` 由 R1-F1 加入：§12.1 步驟 2～3 要求「不符合交接條件時拒絕更新」後才「要求 daemon 安全停止」，CLI 端預檢與一般 `daemon.stop` 之間的窗口只能由 daemon 原子關閉。P1 T08 併入後 `x-methods` 由 54 增為 55、`x-wire.version_absent_for` 加入 `daemon.handoff_stop`；併入前 P3 `Method::known()` 的權威集合為 `x-methods` 鍵 ∪ `x-pending[*].adds_method`。）

（上方 `"..."` 只示意層級；實作時 `SessionPublic.x-order` 必須逐字列出盤點 02 §6.1 的全部 key，`properties` 逐欄給 type／nullable，`console_endpoint` 標 `"x-conditional": "ConsoleKind::TcpTelnet 或 bridge 有 endpoint"` 且不列入 `required`。SC-19 形狀依調和表 GAP-G-07 定案凍結為 `{"enabled":true,"coms":[...]}`，**不含** `source`；P1 若要加 `source` 須先改調和表與本檔並遞增 revision。）

規則：
- `x-methods` 恰為 02 §7 的 54 個 method（`daemon.handoff_stop` 於 P0 只在 `x-pending`，P1 T08 併入後為 55，`test_methods_equal_dispatch_plus_daemon_stop` 同 commit 改為 `| {"daemon.stop", "daemon.handoff_stop"}`）；`params` 每個參數給 `type`、`x-coercion`（取 02 §5.2 表的寫法字串，例 `"int(params.get(k) or D)"`）、`default`；`selector_chain` 取 02 §5.3 的五種之一（`"selector"`、`"selector>session_id>com>alias"`、`"selector>com>alias"`、`"com>selector"`、`"raw_selector"`）或 null；`errors` 取 03 §4 矩陣（`session.attach`、`session.recover` 另含 `"@last_error_passthrough"`）；`retryable_readonly`、`blocking`、`long_timeout` 依 client／daemon／cli 的三個集合；`session.interactive_status` 的 `retryable_readonly` 必須為 false。
- **§9.4 凍結維度**（每個 method 都要有，空 `{}` 由 C18 判 FAIL）：
  - `result`：成功回應的完整 JSON Schema（`type`、`properties`、`required`、`x-order`；可 `$ref` 共用 `$defs`），每個欄位寫 `type`（含 `null`）；§9.4 表列欄位（`multi_open`、`foreign_holders`、`rx_bytes_last_10s`、`last_rx_age_s`、`ready_reconfirm`、`human_active` 等）另加 `x-unit`（如 `"s"`、`"bytes"`、`"pid"`）、`enum`（有列舉時）、`x-updated-by`（更新時機，例 `"ready_reconfirm": "1 s tick；session.list 不主動評估，最遲約 1 s 翻轉（GAP-G-13）"`、`"foreign_holders": "on-demand 掃 /proc；包含 daemon 自身 pid（RPC-D-10／PLT-O-04）"`）。
  - `error_order`：同一請求同時違反多個條件時的判定先後（依 `service.py`／`session_manager.py` 實際檢查順序，例 `command.submit`：`["SESSION_NOT_FOUND","FLASHING_BUSY","SESSION_RECOVERING","SESSION_NOT_READY","AUTOBOOT_QUIET","READY_UNCONFIRMED","CMD_CONTAINS_NEWLINE","CMD_TOO_LONG","INVALID_COMPLETION_REGEX","SESSION_QUEUE_FULL"]`——實作者以程式實讀為準，並為每個 method 的前兩個衝突條件各寫一個 precedence 測試）；`errors` 為集合、`error_order` 為其有序排列，兩者元素集合必須相等。
  - `side_effects`：list，每筆 `{"what": <描述>, "when": "always|on_success|on_failure", "idempotent": bool}`；RETRYABLE 白名單內的冪等副作用必須照實列出（RPC-D-12：例 `session.list`／`session.get_state` 的 lease expire、`device.list` 的 prune）；無副作用寫 `[]`。
- `x-pending` 每筆必須有 `change`（behavior_changes id）；狀態推進協定見 Global Constraints（C17）。

**RPC 行長上限擷取（RPC-H-02，本 Task 子步驟）：** `scripts/stage0/capture_rpc_line_limit.py` 分別經真實 `JsonRpcUnixServer`（POSIX，暫存目錄 socket）與 `TcpRpcServer`（`127.0.0.1:0`）啟動、handler 回 `{"ok":true}`，對每個 server 送出內容長度 `n ∈ {65535, 65536, 65537}`（`{"method":"health.ping","pad":"xxx..."}` 填到 n bytes）各三種結尾：`\n`、`\r\n`、分段送入（先送前半、`drain` 後 50 ms 再送後半＋`\n`），記錄每種是否收到回應行或連線被關閉且無任何回應 bytes；寫出 `contracts/golden/rpc/line_limit.json`：`{"captured_by":"scripts/stage0/capture_rpc_line_limit.py","source":"sw_core/rpc_posix.py serve_connection（asyncio StreamReader 預設 limit）","unit":"bytes before LF","max_ok_bytes":<N>,"first_close_bytes":<N+1>,"crlf_max_ok_bytes":<M>,"segmented_same_as_single":true,"servers":{"posix_unix":{...},"tcp":{...}}}`。兩個 server 結果不一致時腳本 exit 1（契約不得含平台分歧而不自知），實作者停下回報。`x-wire.max_request_line_bytes` 必須等於 golden 的 `max_ok_bytes`。

- [ ] **Step 1：寫失敗測試**（`tests/test_contracts_rpc_schema.py`；L1 白箱，可 import `sw_core` 於函式內）

```python
from __future__ import annotations

import ast
import json
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def _schema():
    return json.loads((ROOT / "contracts" / "rpc_schema.json").read_text(encoding="utf-8"))


def _dispatch_methods() -> set[str]:
    tree = ast.parse((ROOT / "sw_core" / "service.py").read_text(encoding="utf-8"))
    fn = next(n for n in ast.walk(tree) if isinstance(n, ast.FunctionDef) and n.name == "_dispatch")
    found: set[str] = set()
    for node in ast.walk(fn):
        if isinstance(node, ast.Compare) and isinstance(node.left, ast.Name) and node.left.id == "method":
            for op, comp in zip(node.ops, node.comparators):
                if isinstance(op, ast.Eq) and isinstance(comp, ast.Constant):
                    found.add(comp.value)
                if isinstance(op, ast.In) and isinstance(comp, (ast.Tuple, ast.Set, ast.List)):
                    found |= {e.value for e in comp.elts if isinstance(e, ast.Constant)}
    return found


def _literal_set(path: str, name: str) -> set[str]:
    tree = ast.parse((ROOT / path).read_text(encoding="utf-8"))
    for node in tree.body:
        if isinstance(node, ast.Assign) and any(isinstance(t, ast.Name) and t.id == name for t in node.targets):
            value = node.value.args[0] if isinstance(node.value, ast.Call) else node.value
            return {e.value for e in value.elts}
    raise AssertionError(name)


def test_methods_equal_dispatch_plus_daemon_stop():
    assert set(_schema()["x-methods"]) == _dispatch_methods() | {"daemon.stop"}


def test_retryable_flags_match_client_whitelist():
    flagged = {m for m, v in _schema()["x-methods"].items() if v["retryable_readonly"]}
    assert flagged == _literal_set("sw_core/client.py", "RETRYABLE_READONLY_METHODS")
    assert _schema()["x-methods"]["session.interactive_status"]["retryable_readonly"] is False
```

其餘：

| test 名稱 | 斷言 |
|---|---|
| `test_rpc_schema_is_valid_draft2020` | `jsonschema.Draft202012Validator.check_schema(_schema())` 不 raise |
| `test_blocking_flags_match_daemon` | `{m for m,v in ... if v["blocking"]} == _literal_set("sw_core/daemon.py", "BLOCKING_RPC_METHODS")` |
| `test_long_timeout_flags_match_cli` | 同上對 `sw_core/cli.py` 的 `_LONG_RPC_METHODS` |
| `test_method_errors_in_catalog` | 每個 `errors` 值 ∈ 目錄碼 ∪ `{"@" + p.id for p in passthrough}` |
| `test_every_rpc_code_referenced` | 目錄中 `"rpc" in scope.interfaces` 的碼，都出現在某 method 的 `errors` 或 `x-generic-errors` |
| `test_session_public_order_matches_runtime` | 以 `SessionRuntime(session_id="p:COM0", profile=<SessionProfile>)` 建物件，`list(to_public_dict())` 等於 `x-order` 去掉 `console_endpoint` 與 `x-pending` 新增欄 |
| `test_session_public_instance_validates` | `jsonschema.validate(rt.to_public_dict(), {"$defs": s["$defs"], "$ref": "#/$defs/SessionPublic"})` |
| `test_command_record_order_matches_arbiter` | `CommandArbiter(send_cb=...)` submit 後取紀錄，key 順序等於 `$defs.CommandRecord.x-order` |
| `test_early_response_bytes_match_framing` | 以 T7 的 `_roundtrip` 取得 `INVALID_JSON`、`INVALID_REQUEST` 實際位元組，等於 `x-wire.early_responses` |
| `test_version_absent_for_matches_runtime` | 以 `_roundtrip` 送 `{bad`、`[1]`、`{"id":1}`、handler 拋例外的請求，以及經 service 替身呼叫 `daemon.stop`：回應皆不含 `version`；另送 `health.ping` 回應含 `version` 且為最後一個鍵 |
| `test_line_limit_matches_golden` | `x-wire.max_request_line_bytes == golden["max_ok_bytes"]`、`golden["first_close_bytes"] == golden["max_ok_bytes"] + 1`、`golden["unit"] == "bytes before LF"` |
| `test_line_limit_capture_reproducible` | （`process_rpc`、`posix_only` class）以 subprocess 重跑 `scripts/stage0/capture_rpc_line_limit.py --out <tmp>`，輸出除 `captured_by` 外與入庫 golden 相同 |
| `test_params_coercion_match_runtime` | 參數化 54 method × 每個 param：以 `tests/test_daemon_service_selector.py` 的 recording fake 替換 `SessionManager`／arbiter 的被呼叫方法，對 `_dispatch` 送 (a) 省略該 param、(b) 送 `None`、(c) 送字串型數字；斷言 fake 收到的值分別等於 schema 的 `default`、依 `x-coercion` 計算的值（期望值由 schema 字串查表得出，不 import 產品邏輯計算） |
| `test_selector_chain_match_runtime` | 對 `selector_chain` 非 null 的 method，依鏈序只給其中一個鍵（`selector`／`session_id`／`com`／`alias`），fake 收到的 selector 等於該值；同時給兩個鍵時取鏈中較前者 |
| `test_error_order_precedence` | 對每個 `len(error_order) >= 2` 的 method，以替身同時造成 `error_order[0]` 與 `error_order[1]` 的條件，回應 `error_code == error_order[0]` |
| `test_result_schemas_not_placeholders` | 每個 `x-methods.*.result` 與 `$defs.*` 都不是 `{}`，且有 `type`（C18 的本地鏡像） |
| `test_side_effects_declared_for_readonly_whitelist` | `retryable_readonly` 為 true 的 method 都有 `side_effects` 鍵；`session.list`、`device.list` 的 `side_effects` 非空且每筆 `idempotent is True`（RPC-D-12） |
| `test_foreign_holders_includes_self_pid` | `health.status`（或 `daemon status` 對應 method）的 `result` 中 `foreign_holders` 的 `x-updated-by` 含「daemon 自身 pid」；實跑 `sw_core.multi_open` 的掃描函式於本行程開啟的暫存 tty（無 PTY 平台改為 mock `/proc`）時結果含 `os.getpid()` |
| `test_session_list_ready_reconfirm_not_evaluated` | `session.list` 結果 schema 的 `ready_reconfirm.x-updated-by` 含 `1 s tick`；以 fake clock 讓 reconfirm 期限過期但不推進 tick，`session.list` 仍回舊值，推進一次 tick 後翻轉（GAP-G-13） |
| `test_pending_ids_exist` | `x-pending[*].change` 都存在於 `behavior_changes.yaml` |
| `test_pending_fields_absent_while_approved` | 對 `change` 狀態為 `APPROVED` 的 `x-pending` 項：`SessionRuntime.to_public_dict()` 不含 `drain_stalled`／`drain_timeout_epoch`、`health.status` 不含 `test_device_allowlist`（狀態為 `IMPLEMENTED` 以後此測試改由 C17 要求該項已併入並刪除） |

- [ ] **Step 2：確認 FAIL** — `python3 -m pytest -q tests/test_contracts_rpc_schema.py`；Expected：FAIL，原因為 `FileNotFoundError: contracts/rpc_schema.json`（CI 未裝 jsonschema 時另有 `ModuleNotFoundError: jsonschema`，須先在本機安裝再確認一次只剩 FileNotFoundError）。
- [ ] **Step 3：commit 測試** — `git commit -m "test(contracts): 新增 RPC 契約 schema 的失敗測試"`（含 trailer）。
- [ ] **Step 4：實作** 先寫並執行 `scripts/stage0/capture_rpc_line_limit.py` 產生 `contracts/golden/rpc/line_limit.json`；再寫 `contracts/rpc_schema.json`（UTF-8、2 空白縮排、`x-methods` 依 method 名升冪；`result`／`error_order`／`side_effects` 逐 method 依 `service.py` 實讀填寫，不得留 `{}`）；`.github/workflows/tests.yml` 第一個 pip 步驟改為 `python3 -m pip install --disable-pip-version-check pytest pyyaml pyserial jsonschema==4.10.3`。
- [ ] **Step 5：確認 PASS** — 同 Step 2 指令；Expected：PASS。
- [ ] **Step 6：全套與政策** — 同 T1；另確認 R-15／R-19／R-20 無新 FAIL。
- [ ] **Step 7：commit**

`changelog.d/rust-p0-rpc-schema.md`：

```markdown
---
type: feat
scope: contracts
---
新增 `contracts/rpc_schema.json`：凍結 NDJSON framing、早退回應位元組、實測的請求行長上限（`contracts/golden/rpc/line_limit.json`）、54 個 RPC method 的參數轉型、selector 別名鏈、成功回應 schema、錯誤碼與判定先後、副作用與重試／阻塞／長逾時旗標，以及 SessionPublic 等共用子結構的欄位順序；CI 安裝 jsonschema 供契約測試使用。
```

`git add contracts/rpc_schema.json contracts/golden/rpc/line_limit.json scripts/stage0/capture_rpc_line_limit.py .github/workflows/tests.yml tests/test_contracts_rpc_schema.py changelog.d/rust-p0-rpc-schema.md`；`git commit -m "feat(contracts): 新增 RPC 契約 schema" -m "Co-authored-by: Copilot <223556219+Copilot@users.noreply.github.com>"`

**BLOCKED-OWNER：** §9.4 L2／L3 依賴欄位（`multi_open`、`foreign_holders`、`rx_bytes_last_10s`、`last_rx_age_s` 等）在真實 bench daemon 上的實值確認（本 Task 只能以 in-process 物件與原始碼凍結，需擁有者 bench 以 `serialwrap daemon status`／`session list` 對照）。

---

## Task T9：`contracts/cli_schema.json`

**Test author model:** agy-gemini-3.8-flash-high　**Implementer model:** haiku-xhigh

**Files:**
- Create: `contracts/cli_schema.json`
- Test: `tests/test_contracts_cli_schema.py`
- Fragment: `changelog.d/rust-p0-cli-schema.md`

**Interfaces:**
- Consumes：盤點 01 §1、§2、§6；arch §7.1、§7.4；T3 目錄（C15）。
- Produces：P3 `sw-cli/src/cli_spec.rs` 宣告式表格與 `argparse/` 相容層的對照基準；P3 `delegate.rs` 的委派集合；P3 `scripts/rust/capture_cli_help.py` 的 parser 路徑清單。

**檔案結構：**

```json
{
  "$schema": "https://json-schema.org/draft/2020-12/schema",
  "$id": "https://github.com/hamanpaul/serialwrap/contracts/cli_schema.json",
  "title": "serialwrap CLI contract",
  "x-catalog-revision": 1,
  "x-prog": "serialwrap",
  "x-global-options": [
    {"flags": ["--version"], "action": "version", "dest": null},
    {"flags": ["-v", "--verbose"], "action": "count", "dest": "verbose", "default": 0},
    {"flags": ["--socket"], "action": "store", "dest": "socket", "type": "str", "default": null},
    {"flags": ["--endpoint"], "action": "store", "dest": "endpoint", "type": "str", "default": null, "metavar": "ENDPOINT"},
    {"flags": ["--bench"], "action": "store", "dest": "bench", "type": "str", "default": null, "metavar": "CODE"},
    {"flags": ["--timeout"], "action": "store", "dest": "timeout_s", "type": "float", "default": null},
    {"flags": ["--retries"], "action": "store", "dest": "retries", "type": "int", "default": 0}
  ],
  "x-groups": [
    {"name": "daemon", "stage": "delegate-1a", "local": true, "options": [], "mutex": [], "subcommands": [
      {"name": "start", "options": [{"flags": ["--foreground"], "action": "store_true", "dest": "foreground", "default": false}], "mutex": [], "rpc": null}
    ]},
    {"name": "setup", "stage": "delegate-1a", "local": true,
     "options": [{"flags": ["--user"], "action": "store_true", "dest": "user", "default": false}],
     "mutex": [{"dests": ["user", "system", "on_demand"], "required": false}], "subcommands": null},
    {"name": "session", "stage": "rust-1a", "local": false, "options": [], "mutex": [], "subcommands": [
      {"name": "list", "options": [], "mutex": [], "rpc": {"method": "session.list", "params": {}, "omit_when_none": [], "send_null": []}}
    ]}
  ],
  "x-output": {
    "stdout_json": {"ensure_ascii": false, "sort_keys": true, "separators": [",", ":"], "trailing_newline": true},
    "stderr_error_line": "serialwrap: {context }failed: {code}[: {message}][（hint: {hint}）]",
    "unknown_error_literal": "UNKNOWN_ERROR",
    "plain_text_commands": ["supervision-mode", "skill", "--version", "--help", "daemon start --foreground"]
  },
  "x-exit-codes": [
    {"context": "_run_rpc", "success": 0, "failure": 2, "codes": {"BENCH_ENDPOINT_NOT_REMEMBERED": 1, "INVALID_ENDPOINT": 1}},
    {"context": "daemon start", "success": 0, "failure": 2, "codes": {"REMOTE_NOT_SUPPORTED": 2, "INVALID_ARGS": 2}}
  ],
  "x-retry": {"methods": [], "transient_codes": ["TIMEOUT", "SOCKET_ERROR", "EMPTY_RESPONSE"],
              "backoff_base_s": 0.5, "backoff_factor": 2, "backoff_max_s": 5.0, "event_forwards_retries": false},
  "x-timeouts": {"default_rpc_timeout_s": 5.0, "long_rpc_timeout_floor_s": 45.0, "long_methods": []},
  "x-fixed-timeouts": [
    {"name": "daemon_stop_wait_s", "value": 2.0, "source": "sw_core/cli.py:<行>"},
    {"name": "probe_connect_timeout_s", "value": 0.5, "source": "..."},
    {"name": "daemon_ready_poll", "posix": {"tries": 15, "interval_s": 0.2}, "windows": {"tries": 50, "interval_s": 0.2}, "source": "sw_core/cli.py:618-621"}
  ],
  "x-env": [
    {"name": "SERIALWRAP_LOG_LEVEL", "read_by": "sw_core/cli.py:<行>", "default": null, "effect": "..."}
  ],
  "x-version": {"flag_output": "serialwrap <VERSION>\n", "stream": "stdout",
                "mismatch_warning": {"stream": "stderr", "template": "<逐字自 cli.py>", "blocking": false}},
  "x-delegation-1a": ["daemon", "service", "supervision-mode", "remote", "connect", "benches", "setup", "doctor"],
  "x-pending": [
    {"id": "SC-20", "change": "SC-20", "adds_subcommand": ["daemon", "restart"]},
    {"id": "SC-21", "change": "SC-21", "adds_subcommand": ["remote", "stop"]},
    {"id": "SC-22", "change": "SC-22", "adds_subcommand": ["supervision-mode", "set"]},
    {"id": "SC-23", "change": "SC-23", "adds_option": ["setup", "--rollback"]}
  ]
}
```

（`x-groups` 實作時須依 parser 註冊順序列滿 20 群組與全部子命令／選項；上方只示範形狀。沒有子命令的群組（`remote`、`connect`、`benches`、`supervision-mode`、`service`、`setup`、`doctor`、`skill`）以 `"subcommands": null` 表示，選項放群組層 `options`、互斥組放群組層 `mutex`。`x-exit-codes` 每列 `{context ∈ CLI_CONTEXTS, success, failure, codes: {CODE: exit}}`，須逐列轉寫 01 §2.4 全表，且 `codes` 與 `error_codes.yaml` 各碼的 `exit_codes` 雙向一致（C15）。`x-retry.methods`、`x-timeouts.long_methods`、`x-fixed-timeouts`（CLI-11.18：daemon stop 等待 2.0 s、probe 0.5／1.0 s、daemon ready 輪詢 POSIX 15×0.2 s／Windows 50×0.2 s，以及實讀發現的其他硬編逾時）、`x-env`（CLI-11.01：`SERIALWRAP_LOG_LEVEL` 與 `sw_core/cli.py`、`sw_core/client.py` 讀取的全部 `SERIALWRAP_*` env，`grep -n 'os.environ' sw_core/cli.py sw_core/client.py` 逐一列出）、`x-version`（§9.4 版本列：`--version` 輸出逐字、版本不符警告的 stream 與模板逐字、非阻斷）都必須是實值，`<行>`／`...` 不得入庫。）

選項物件欄位：`flags`（list）、`action`（`store`／`store_true`／`count`／`append`／`version`／`help`）、`dest`、`type`（`str`／`int`／`float`／null）、`default`、`choices`（list 或省略）、`required`（bool，僅 true 時寫）、`nargs`（僅非預設時寫）、`metavar`（僅有設定時寫）；positional 以 `"flags": []`＋`"positional": "<dest>"` 表示；mutually exclusive group 以 `"mutex": [{"dests": [<dest>...], "required": bool}]` 寫在其所屬的群組或子命令物件（例：`event reset` 的必選互斥組 `required: true`、`setup` 的 `user／system／on_demand`）。

子命令物件的 `rpc`：本地命令為 null；送 RPC 者為 `{"method": <name>, "params": {<param>: <來源 dest 或常數>}, "omit_when_none": [<param>...], "send_null": [<param>...]}`，照 01 §6 與 CLI-12.09 的 null／省略差異逐字轉寫（值為 None 時省略該鍵者列入 `omit_when_none`，照送 `null` 者列入 `send_null`）。

- [ ] **Step 1：寫失敗測試**（`tests/test_contracts_cli_schema.py`；L1 白箱）

```python
from __future__ import annotations

import argparse
import json
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def _schema():
    return json.loads((ROOT / "contracts" / "cli_schema.json").read_text(encoding="utf-8"))


_KIND = {
    argparse._StoreAction: "store", argparse._StoreTrueAction: "store_true",
    argparse._StoreFalseAction: "store_false", argparse._StoreConstAction: "store_const",
    argparse._CountAction: "count", argparse._AppendAction: "append",
    argparse._VersionAction: "version",
}


def _canon_action(a: argparse.Action) -> dict:
    kind = _KIND.get(type(a), type(a).__name__)      # 未列的 action 類別以類別名呈現，schema 須照實寫
    out = {"flags": list(a.option_strings), "action": kind,
           "dest": None if kind == "version" else a.dest}
    if kind in ("store", "append"):
        out["type"] = "str" if a.type in (None, str) else getattr(a.type, "__name__", repr(a.type))
    if kind not in ("version",):
        out["default"] = a.default
    if a.choices is not None and not isinstance(a.choices, dict):
        out["choices"] = list(a.choices)
    if a.required and a.option_strings:
        out["required"] = True
    if a.metavar is not None and a.option_strings:
        out["metavar"] = a.metavar
    if not a.option_strings:
        out["flags"], out["positional"] = [], a.dest
    if a.nargs not in (None, 0):
        out["nargs"] = a.nargs
    return out


def _non_help(parser):
    return [a for a in parser._actions
            if not isinstance(a, (argparse._HelpAction, argparse._SubParsersAction))]


def test_global_options_match_build_parser():
    from sw_core.cli import build_parser
    got = [_canon_action(a) for a in _non_help(build_parser())]
    assert got == _schema()["x-global-options"]


def test_group_order_matches_registration():
    from sw_core.cli import build_parser
    sub = next(a for a in build_parser()._actions if isinstance(a, argparse._SubParsersAction))
    assert [g["name"] for g in _schema()["x-groups"]] == list(sub.choices)
```

| test 名稱 | 斷言 |
|---|---|
| `test_cli_schema_valid_draft2020` | `check_schema` 不 raise |
| `test_every_subcommand_options_match` | **雙向**：(a) 以 parser 為準走訪——每個群組的 `_non_help` 選項等於 schema 群組層 `options`；群組有 subparsers 時其子命令名稱集合等於 schema `subcommands` 名稱集合（多出的 parser 子命令若對應 `change` 狀態為 `APPROVED` 的 `x-pending` 項 → FAIL「核准未實作的子命令已出現」；`IMPLEMENTED` 以後則要求已併入 `x-groups` 並自 `x-pending` 刪除）；(b) 以 schema 為準走訪——schema 每個子命令都存在於 parser；兩邊每個子命令的 `[_canon_action(a) for a in _non_help(subparser)]` 等於 schema `options`；`mutex` 與 parser `_mutually_exclusive_groups` 的 `(dests, required)` 相等 |
| `test_rpc_mapping_matches_cli` | 參數化每個 `rpc` 非 null 的子命令：以最小合法 argv 呼叫 `sw_core.cli.main([...])`，`mock.patch` 掉 `rpc_call`（及 `_rpc_call_once`）記錄 `(method, params)`，斷言 method 相等、params 鍵集合符合 `rpc.params`／`omit_when_none`／`send_null`（選項省略時 `omit_when_none` 鍵不在 params、`send_null` 鍵值為 None） |
| `test_retry_methods_match_client` | `set(x-retry.methods) == RETRYABLE_READONLY_METHODS`（AST，同 T8） |
| `test_long_methods_match_cli` | `set(x-timeouts.long_methods) == _LONG_RPC_METHODS` |
| `test_env_vars_listed` | AST 掃 `sw_core/cli.py`、`sw_core/client.py` 所有 `os.environ.get("<NAME>")`／`os.environ["<NAME>"]`／`os.getenv("<NAME>")` 的常數名 ⊆ `{e["name"] for e in x-env}`；`SERIALWRAP_LOG_LEVEL` 在其中 |
| `test_fixed_timeouts_have_sources` | `x-fixed-timeouts` 每筆的 `source` 指向的行（AST 讀取）含該數值常數 |
| `test_version_output_matches` | `cli.main(["--version"])` 的 stdout 等於 `x-version.flag_output` 以 `sw_core.version` 的版本替換後的字串 |
| `test_delegation_set_matches_arch` | `x-delegation-1a` 恰為 arch §7.4 的 8 個群組，且每個都在 `x-groups` 名稱中 |
| `test_exit_codes_consistent_with_catalog` | 目錄每個 `exit_codes` 的 `(context, code, exit)` 都在 `x-exit-codes[context].codes[code] == exit`，且 `x-exit-codes` 每個 `codes` 項都在目錄對應碼的 `exit_codes` 中（雙向；C15 本地鏡像）；所有 context ∈ `CLI_CONTEXTS` |
| `test_pending_ids_exist` | `x-pending[*].change` 存在於 `behavior_changes.yaml` |
| `test_doctor_and_profile_test_always_exit_0` | `x-exit-codes` 中 context `doctor` 與 `profile test` 的 `failure` 欄為 0 |

- [ ] **Step 2：確認 FAIL** — `python3 -m pytest -q tests/test_contracts_cli_schema.py`；Expected：FAIL（檔案不存在）。
- [ ] **Step 3：commit 測試** — `git commit -m "test(contracts): 新增 CLI 契約 schema 的失敗測試"`（含 trailer）。
- [ ] **Step 4：實作** — 以一次性腳本（放 scratchpad，不入庫）走訪 `build_parser()` 產生 `x-global-options`／`x-groups` 骨架（含群組層 options 與 mutex），再人工補 `stage`、`local`、`rpc`（每個子命令送出的 method 與 params 組裝，照 01 §6 與 CLI-12.09；由 `test_rpc_mapping_matches_cli` 驗證）、`x-exit-codes`、`x-fixed-timeouts`、`x-env`、`x-version`；成品手動審閱後入庫。
- [ ] **Step 5：確認 PASS** — 同 Step 2；Expected：PASS。
- [ ] **Step 6：全套與政策** — 同 T1。
- [ ] **Step 7：commit**

`changelog.d/rust-p0-cli-schema.md`：

```markdown
---
type: feat
scope: contracts
---
新增 `contracts/cli_schema.json`：凍結全域選項、20 個頂層群組（含群組層選項與互斥組）的子命令與選項、每個子命令送出的 RPC 與參數組裝、stdout／stderr 格式、exit code 總表、重試白名單、預設與硬編逾時、env 變數、`--version` 與版本不符警告，以及 Stage 1A 委派集合，並以測試對照 `build_parser()` 與實際 RPC 呼叫防漂移。
```

`git add contracts/cli_schema.json tests/test_contracts_cli_schema.py changelog.d/rust-p0-cli-schema.md`；`git commit -m "feat(contracts): 新增 CLI 契約 schema" -m "Co-authored-by: Copilot <223556219+Copilot@users.noreply.github.com>"`

**BLOCKED-OWNER：** 無（help／usage 位元組 golden 屬 P3 `contracts/cli_help/`）。

---

## Task T10：`contracts/wal_record.schema.json`、`contracts/wal_meta_events.schema.json` 與 `contracts/state_file.schema.json`

**Test author model:** haiku-xhigh　**Implementer model:** agy-gemini-3.8-flash-high

**Files:**
- Create: `contracts/wal_record.schema.json`、`contracts/wal_meta_events.schema.json`（META 事件的**唯一** schema 檔；P1 只修改、不另建）、`contracts/state_file.schema.json`
- Test: `tests/test_contracts_wal_state_schema.py`
- Fragment: `changelog.d/rust-p0-wal-state-schemas.md`

**Interfaces:**
- Consumes：盤點 04 §1.3、§1.6、§2.2；03 §5.6（META 事件）；arch §3.8。
- Produces：`sw_proto::wal_types::WalRecord` 的欄位與型別基準；P5 `state_store.rs` 只寫相同 5 個 key（WAL-D14）；F12 oracle（P2）讀回驗證所用 schema。

**`wal_record.schema.json` 規則：** `type: object`、`additionalProperties: false`；`required` 為 `cmd_id, com, crc32, dir, len, loss_flag, meta, mono_ts_ns, payload_b64, seq, source, wall_ts`；`rotation_failed` 選填且 `const: true`；`crc32` `pattern: "^[0-9a-f]{8}$"`；`seq`、`len`、`mono_ts_ns` 為 `integer` 且 `minimum: 0`（`seq` 最小 1）；`wall_ts` `pattern: "^\\d{4}-\\d{2}-\\d{2}T\\d{2}:\\d{2}:\\d{2}(\\.\\d{6})?\\+00:00$"`；`dir` `type: string`（`x-known-values: ["RX","TX","META"]`，不設 enum，因程式無驗證）；`payload_b64` 標準 alphabet 含 padding `^[A-Za-z0-9+/]*={0,2}$`；`cmd_id` `["string","null"]`；`meta` `type: object`。另加：
- `x-key-order: "sorted"`、`x-serialization: "DISK_STABLE（ensure_ascii=False, sort_keys=True, separators=(',',':')）＋\\n"`、`x-rotate: {"threshold_bytes": 67108864, "condition": "size >= R", "checked": "before_write", "applies_to": ["raw.wal.ndjson", "raw.mirror.log"]}`。
- `meta` 以 `if dir == "META" then meta: {"$ref": "wal_meta_events.schema.json"}` 連到 META schema；本檔不重複定義事件。

**`wal_meta_events.schema.json` 規則：** `oneOf` 依 `meta.event` 分派：`credentials_unresolved`（`event`、`reason ∈ {not_configured, ok, env_file_missing, env_file_unreadable, key_absent}`、`env_file` string）、`transport_stall_suspected`（`event`、`real_path`、`rx_age_s` number）、`WAL_RECREATED_AFTER_EXTERNAL_UNLINK`（arch §3.8 逐字欄位，`x-status: "APPROVED"`、`x-behavior-change: "BC-01"`，外層 `dir: META`、`source: system`、`loss_flag: true`）。`WAL_RECREATED_AFTER_EXTERNAL_UNLINK` 的缺口範圍必須以 if／then 明確表示（§8.2「未知範圍必須明示，不得猜測」）：`range_known: false` ⇒ `potentially_lost_seq_range` 為 `null`；`range_known: true` ⇒ `potentially_lost_seq_range` 為 `[first, last]` 兩個 `integer`（`minimum: 1`），並以 `x-constraint: "first <= last"` 標示、由測試驗證（JSON Schema 無法直接比較兩元素）。P1 依 arch §3.8 修訂補上 `com: "WAL"`、`previous_identity` `^[0-9]+:[0-9]+$` 時**修改本檔**並遞增 `x-catalog-revision`。

**`state_file.schema.json` 規則：** 頂層 `required` 與 `properties` 恰為 `aliases`、`bindings`、`profile_detected`、`profile_pins`、`released`，`additionalProperties: false`；`aliases` 值為 `oneOf`：session 型 `{session_id, updated_at}`（`additionalProperties: false`）或 by-id 型 `{device_by_id, updated_at, profile?}`；`bindings`、`profile_detected`、`profile_pins` 為 `additionalProperties: {"type":"string"}`；`released` 值為 `{by_id: ["string","null"], released_by: ["string","null"], released_at: ["string","null"], reason: {}}`——`_save_state` 的型別為 `dict[str, dict[str, str | None]]`、`release_device` 允許 `device_by_id=None`（`session_manager.py:812-819`、`:1873-1881`），`reason` 是 RPC 原樣傳入的 JSON 值（盤點 02 §6.1），故不限型別並以 `x-note` 說明；`x-serialization: "DISK_STABLE＋\\n"`、`x-no-version-field: true`（WAL-D14）、`x-write: "mkstemp(prefix='state.json.tmp.', 0600)→fsync→os.replace→dir fsync（非 Windows）"`。

- [ ] **Step 1：寫失敗測試**

| test 名稱 | 準備 | 斷言 |
|---|---|---|
| `test_wal_schema_valid_draft2020` | — | `check_schema` 不 raise |
| `test_wal_records_from_writer_validate` | `WalWriter(wal_dir=tmp)` 依序 `append(com="COM0", direction="RX", source="device", payload=b"hello\r\n")`、TX（`cmd_id="c1"`、`meta={"a":"中","z":1}`）、META（`payload=b""`） | 讀 `raw.wal.ndjson` 每行 `json.loads` 後 `jsonschema.validate` 通過；每行 key 順序等於 `sorted(keys)` |
| `test_wal_inventory_examples_validate` | 04 §1.3 三行實跑範例逐字貼入測試 | 三行皆通過 |
| `test_wal_crc32_uppercase_rejected` | 範例第一行把 `crc32` 改大寫 | `ValidationError` |
| `test_wal_unknown_key_rejected` | 加 `"extra": 1` | `ValidationError` |
| `test_wal_rotate_threshold_matches_constant` | 函式內 `from sw_core.constants import DEFAULT_WAL_ROTATE_BYTES`（L1 白箱；原始碼為 `64 * 1024 * 1024`，`ast.literal_eval` 無法求值） | 等於 `x-rotate.threshold_bytes` |
| `test_meta_schema_single_source` | `contracts/` 下只有 `wal_meta_events.schema.json` 定義 `WAL_RECREATED_AFTER_EXTERNAL_UNLINK`（掃全部 `*.json`）；`wal_record.schema.json` 以 `$ref` 指向它 | 成立 |
| `test_wal_recreated_meta_schema_present` | — | `WAL_RECREATED_AFTER_EXTERNAL_UNLINK` 分支存在且 `x-behavior-change == "BC-01"` |
| `test_wal_recreated_range_positive` | `range_known: true` ＋ `[3, 9]`；`range_known: false` ＋ `null` | 皆通過（以 `jsonschema.RefResolver` 預載兩檔；4.10.3 尚無 `referencing` 套件） |
| `test_wal_recreated_range_negative` | `range_known: false` ＋ `[3, 9]`；`range_known: true` ＋ `null`；`range_known: true` ＋ `[9, 3]`（以 `x-constraint` 測試輔助檢查）；`range_known: true` ＋ `["3","9"]` | 皆 `ValidationError` 或輔助檢查 FAIL |
| `test_state_schema_valid_draft2020` | — | `check_schema` 不 raise |
| `test_state_from_manager_validates` | `SessionManager([profile], ...)`（`STATE_PATH` 已由 conftest 隔離）；依序 `assign_alias`、`pin_session`、`release_device`（另一 session `device_by_id=None`、`reason={"why":"flash"}`）後 `_save_state()` | 讀回 JSON 通過 schema（含 `by_id: null` 與 object 型 `reason`）；檔案內容以 `\n` 結尾；key 為字母序 |
| `test_state_inventory_example_validates` | 04 §2.2 實跑範例（`updated_at` 的 `...` 換成合法 ISO 字串） | 通過 |
| `test_state_extra_top_key_rejected` | 加 `"version": 1` | `ValidationError` |

- [ ] **Step 2：確認 FAIL** — `python3 -m pytest -q tests/test_contracts_wal_state_schema.py`；Expected：FAIL（檔案不存在）。
- [ ] **Step 3：commit 測試** — `git commit -m "test(contracts): 新增 WAL 與 state.json schema 的失敗測試"`（含 trailer）。
- [ ] **Step 4：實作** 兩個 schema 檔。
- [ ] **Step 5：確認 PASS** — 同 Step 2。
- [ ] **Step 6：全套與政策** — 同 T1。
- [ ] **Step 7：commit**

`changelog.d/rust-p0-wal-state-schemas.md`：

```markdown
---
type: feat
scope: contracts
---
新增 `contracts/wal_record.schema.json`（WAL 行欄位、型別、crc32 格式、排序序列化、rotate 門檻）、`contracts/wal_meta_events.schema.json`（META 事件唯一 schema，含 BC-01 核准的重建事件與明示的缺口範圍規則）與 `contracts/state_file.schema.json`（state.json 五個頂層 key 與列結構），並以實際 WalWriter／SessionManager 輸出驗證。
```

`git add contracts/wal_record.schema.json contracts/wal_meta_events.schema.json contracts/state_file.schema.json tests/test_contracts_wal_state_schema.py changelog.d/rust-p0-wal-state-schemas.md`；`git commit -m "feat(contracts): 新增 WAL 紀錄與 state.json schema" -m "Co-authored-by: Copilot <223556219+Copilot@users.noreply.github.com>"`

**BLOCKED-OWNER：** 無（BC-01 的 Windows share-delete 與外部 unlink 證據屬 P1／P5）。

---

## Task T11：SKILL.md 受管區段生成

**Test author model:** agy-gemini-3.8-flash-high　**Implementer model:** haiku-xhigh

**Files:**
- Create: `tools/contracts/skill_section.py`
- Modify: `sw_core/assets/skill/SKILL.md`、`sw_core/assets/skill/SKILL_WINDOWS.md`
- Test: `tests/test_contracts_skill_section.py`
- Fragment: `changelog.d/rust-p0-skill-error-sections.md`

**Interfaces:**
- Consumes：T1 `Catalogs`。
- Produces：`render(cat, namespace) -> str`；`python3 -m tools.contracts skill-section --namespace ...`；SKILL 兩檔的 `generated-fact` 標記區塊（R-26 與 C14 比對對象；T12 `entry_pattern` 以此格式為準）。

**輸出格式（逐字）：**

error_code 表：

```text
| code | 介面／平台 | 可重試 | 意義 | Agent 處置 |
|---|---|---|---|---|
| `AUTOBOOT_QUIET` | rpc、rec／posix、windows | 是 | <meaning> | <agent_action> |
```

- 列依 code 升冪；介面以 `、` 連接，`status == "APPROVED"` 時介面後加 `（未生效）`；平台以 `、` 連接；可重試用 `RETRYABLE_LABEL`。
- 表格後一行：`passthrough：<id> — <rule>（值見下方 last_error 表）`，每個 passthrough 一行。

last_error 表：

```text
| last_error | 終態 | 自動重探 | 意義 | Agent 處置 |
|---|---|---|---|---|
| `CREDENTIALS_UNRESOLVED` | 終態 | 不自動重探 | <meaning> | <agent_action> |
```

- 自動重探欄：`attached and detached`→`ATTACHED／DETACHED 自動重探`；只 `attached`→`僅 ATTACHED 自動重探`；皆否→`不自動重探`。`reachable: false` 的值在意義欄尾加 `（目前不可達）`。

插入位置（兩檔結構不同）：`SKILL.md` 插在 `## 參考檔案` 之前，標題為 `## 錯誤碼與 last_error 處置目錄（自動產生；勿手改）`；`SKILL_WINDOWS.md` 沒有 `## 參考檔案`（章節為編號式 `## 1. 安裝`～`## 12. 疑難排解`），附加在檔尾，標題為 `## 13. 錯誤碼與 last_error 處置目錄（自動產生；勿手改）`。兩檔的 marker 區塊內容完全相同。以 `SKILL.md` 為例：

```markdown
## 錯誤碼與 last_error 處置目錄（自動產生；勿手改）

本節由 `python3 -m tools.contracts skill-section` 依 `contracts/error_codes.yaml` 與 `contracts/session_last_errors.yaml` 產生；`error_code` 與 session `last_error` 是兩個不同命名空間。修改請改 `contracts/` 後重新產生。

### RPC／CLI error_code

<!-- BEGIN: generated-fact marker="error-codes" -->
（render(cat, "error_code") 輸出）
<!-- END: generated-fact marker="error-codes" -->

### Session last_error

<!-- BEGIN: generated-fact marker="session-last-errors" -->
（render(cat, "session_last_error") 輸出）
<!-- END: generated-fact marker="session-last-errors" -->
```

- [ ] **Step 1：寫失敗測試**

| test 名稱 | 斷言 |
|---|---|
| `test_render_row_format` | 以合成 `Catalogs`（一筆 EXISTING、一筆 APPROVED）呼叫 `render`，輸出第三行逐字等於預期字串；APPROVED 列含 `（未生效）` |
| `test_render_sorted_and_header` | 前兩行為固定表頭；資料列 code 已排序 |
| `test_render_last_error_reprobe_labels` | 三種 auto_reprobe 組合得到三種標籤；`reachable: false` 列含 `（目前不可達）` |
| `test_skill_md_blocks_in_sync` | 對 `SKILL.md`、`SKILL_WINDOWS.md`，兩個 marker 區塊 `strip()` 後等於 `render(load_catalogs(ROOT), ns).strip()` |
| `test_skill_section_placement` | `SKILL.md` 中該節標題位於 `## 參考檔案` 之前；`SKILL_WINDOWS.md` 中 `## 13. 錯誤碼與 last_error 處置目錄` 位於 `## 12. 疑難排解` 之後且為最後一個 `## ` 標題 |
| `test_render_deterministic_bytes` | 以 subprocess 執行 `python3 -m tools.contracts skill-section --namespace error_code` 兩次輸出逐位元組相同，且等於行程內 `render()`＋`\n`（不依賴 dict 迭代以外的版本相依行為） |
| `test_skill_rows_match_entry_pattern` | T12 的 `entry_pattern`（測試內以同一字串常數宣告）能匹配每個資料列、不匹配表頭 |
| `test_skill_cmd_still_prints_skill` | `serialwrap skill` 輸出包含 `錯誤碼與 last_error 處置目錄`（沿用 `tests/test_skill_cmd.py` 的呼叫方式） |

- [ ] **Step 2：確認 FAIL** — `python3 -m pytest -q tests/test_contracts_skill_section.py`；Expected：FAIL。
- [ ] **Step 3：commit 測試** — `git commit -m "test(contracts): 新增 SKILL 受管區段的失敗測試"`（含 trailer）。
- [ ] **Step 4：實作** `skill_section.py`；執行 `python3 -m tools.contracts skill-section --namespace error_code` 與 `--namespace session_last_error`，把輸出貼進兩份 SKILL 的標記區塊。
- [ ] **Step 5：確認 PASS** — 同 Step 2；並跑 `tests/test_skill_cmd.py`。
- [ ] **Step 6：全套與政策** — 同 T1；`python3 -m policy_check --repo .` 的 R-16 必須仍 PASS（SKILL 變更不影響 README help 區塊）。
- [ ] **Step 7：commit**

`changelog.d/rust-p0-skill-error-sections.md`：

```markdown
---
type: feat
scope: skill
---
`SKILL.md` 與 `SKILL_WINDOWS.md` 新增自動產生的「錯誤碼與 last_error 處置目錄」，列出每個 error_code 與 session last_error 的意義、介面、可重試性與 Agent 處置（OD-06）。
```

`git add tools/contracts/skill_section.py sw_core/assets/skill/SKILL.md sw_core/assets/skill/SKILL_WINDOWS.md tests/test_contracts_skill_section.py changelog.d/rust-p0-skill-error-sections.md`；`git commit -m "feat(skill): 新增錯誤碼與 last_error 受管處置目錄" -m "Co-authored-by: Copilot <223556219+Copilot@users.noreply.github.com>"`

**BLOCKED-OWNER：** 無。

---

## Task T12：`.project-policy.yml` 宣告 OD-06 規則、R-26 同步與 preflight

**Test author model:** haiku-xhigh　**Implementer model:** agy-gemini-3.8-flash-high

**Files:**
- Create: `docs/contracts-governance.md`（繁中：`contracts/` 各檔用途、`python3 -m tools.contracts` 子命令與 exit code、C01～C19 代號表、狀態推進協定、golden 慣例；讓 R-18 對 `contracts/**`／`tools/contracts/**` 的變更有對應文件）
- Modify: `.project-policy.yml`、`CLAUDE.md`（新增「契約治理政策」段）、`tests/test_contracts_validate.py`（追加 Phase 出口測試）
- Test: `tests/test_contracts_policy_decl.py`、`tests/test_contracts_validate.py::test_real_repo_contract_check_passes`
- Fragment: `changelog.d/rust-p0-od06-policy-declaration.md`

**Interfaces:**
- Consumes：T6 CLI、T11 標記區塊。
- Produces：`diagnostic_codes` 宣告（T13 R-27 讀取的設定形狀）；`generated_facts` 四項（現行 v1.0.18 引擎 R-26 立即生效）；preflight `contracts` 步驟。

**`.project-policy.yml` 新增內容（逐字；放在 `cli:` 之後）：**

```yaml
# OD-06：凡程式會輸出的 error_code 與 session last_error，都必須在 SKILL 有意義與處置。
# 規則本體為 paulsha-conventions R-27（通用、不內建碼表）；引擎 pin 升級前由
# tests/test_contracts_policy_decl.py 與 `python3 -m tools.contracts check` 的 C13 以同語意本地強制。
diagnostic_codes:
  - namespace: error_code
    extract: "python3 -m tools.contracts extract --namespace error_code --format lines"
    docs: ["sw_core/assets/skill/SKILL.md", "sw_core/assets/skill/SKILL_WINDOWS.md"]
    entry_pattern: '^\| `(?P<code>[^`]+)` \| [^|\n]* \| [^|\n]* \| (?P<meaning>[^|\n]*\S[^|\n]*) \| (?P<action>[^|\n]*\S[^|\n]*) \|$'
    require_in: all
  - namespace: session_last_error
    extract: "python3 -m tools.contracts extract --namespace session_last_error --format lines"
    docs: ["sw_core/assets/skill/SKILL.md", "sw_core/assets/skill/SKILL_WINDOWS.md"]
    entry_pattern: '^\| `(?P<code>[^`]+)` \| [^|\n]* \| [^|\n]* \| (?P<meaning>[^|\n]*\S[^|\n]*) \| (?P<action>[^|\n]*\S[^|\n]*) \|$'
    require_in: all
# R-26：SKILL 受管區段必須與契約目錄產生結果一致
generated_facts:
  - command: "python3 -m tools.contracts skill-section --namespace error_code"
    reflected_in: "sw_core/assets/skill/SKILL.md"
    marker: "error-codes"
  - command: "python3 -m tools.contracts skill-section --namespace session_last_error"
    reflected_in: "sw_core/assets/skill/SKILL.md"
    marker: "session-last-errors"
  - command: "python3 -m tools.contracts skill-section --namespace error_code"
    reflected_in: "sw_core/assets/skill/SKILL_WINDOWS.md"
    marker: "error-codes"
  - command: "python3 -m tools.contracts skill-section --namespace session_last_error"
    reflected_in: "sw_core/assets/skill/SKILL_WINDOWS.md"
    marker: "session-last-errors"
```

另外：`code_paths` 加 `"contracts/**"`；`preflight.steps` 在 `tests` 之前加 `{name: contracts, kind: validation, argv: ["python3", "-m", "tools.contracts", "check"], timeout_seconds: 300}`。

`CLAUDE.md` 新增「## 契約治理政策（Rust 重構 P0）」段（放在「回歸 case 政策」之後），內容：`contracts/` 為權威；改動 `sw_core` 的錯誤輸出必須同步 `contracts/error_codes.yaml`／`session_last_errors.yaml` 並重產 SKILL 區段；完成前執行 `python3 -m tools.contracts check`；修改 `contracts/`、比較器或哨兵的 PR 需獨立審閱（§10.4）；計數由資料產生，不得硬編碼；實作 BC／SC／DF 時依狀態推進協定在同一 commit 翻 status 並把 `x-pending` 併入；詳見 `docs/contracts-governance.md`。

- [ ] **Step 1：寫失敗測試**（`tests/test_contracts_policy_decl.py`；並在 `tests/test_contracts_validate.py` 追加 Phase 出口測試）

```python
# 追加至 tests/test_contracts_validate.py；本檔混合 memory_only 與 subprocess 測試，
# 依 T14 規則模組層只放平台 marker，資源 marker 放函式層
@pytest.mark.process_rpc
def test_real_repo_contract_check_passes():
    proc = subprocess.run([sys.executable, "-m", "tools.contracts", "check"],
                          cwd=ROOT, capture_output=True, text=True, timeout=300)
    out = json.loads(proc.stdout)
    assert proc.returncode == 0, json.dumps(out["findings"], ensure_ascii=False, indent=1)
    assert out["ok"] is True
```

```python
from __future__ import annotations

import importlib
import sys
from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parents[1]


def _policy():
    return yaml.safe_load((ROOT / ".project-policy.yml").read_text(encoding="utf-8"))


def test_diagnostic_codes_declared_for_both_namespaces():
    decl = {d["namespace"]: d for d in _policy()["diagnostic_codes"]}
    assert set(decl) == {"error_code", "session_last_error"}
    for d in decl.values():
        assert d["extract"].startswith("python3 -m tools.contracts extract")
        assert all((ROOT / p).is_file() for p in d["docs"])
        assert {"code", "meaning", "action"} <= set(__import__("re").compile(d["entry_pattern"]).groupindex)


def test_doc_coverage_local_mirror_of_r27_passes():
    if str(ROOT) not in sys.path:
        sys.path.insert(0, str(ROOT))
    val = importlib.import_module("tools.contracts.validate")
    findings, _ = val.run_all(ROOT)
    assert [f for f in findings if f.check in ("C13", "C14")] == []
```

| test 名稱 | 斷言 |
|---|---|
| `test_generated_facts_cover_both_skills` | `generated_facts` 中 `(reflected_in, marker)` 集合恰為兩檔 × 兩 marker |
| `test_contracts_in_code_paths` | `"contracts/**" in _policy()["code_paths"]` |
| `test_preflight_has_contracts_step_before_tests` | `preflight.steps` 名稱序列中 `contracts` 在 `tests` 之前，argv 為 `["python3","-m","tools.contracts","check"]` |
| `test_policy_rule_declaration_has_no_code_list` | `diagnostic_codes` 區塊序列化後不含任何目錄中的碼值（宣告只放抽取方式與文件位置，OD-06） |
| `test_claude_md_has_contract_governance_section` | `CLAUDE.md` 含 `## 契約治理政策` 與 `python3 -m tools.contracts check` |
| `test_governance_doc_lists_all_checks` | `docs/contracts-governance.md` 存在，且含 `C01`～`C19` 每個代號與 `python3 -m tools.contracts` 的五個子命令名 |

- [ ] **Step 2：確認 FAIL** — `python3 -m pytest -q tests/test_contracts_policy_decl.py tests/test_contracts_validate.py::test_real_repo_contract_check_passes`；Expected：前者 FAIL（`KeyError: 'diagnostic_codes'`）；出口測試 FAIL 且 findings 只剩 C13／C14（其他代號在 T1～T11、T16 已清零；出現其他代號先回頭修正）。
- [ ] **Step 3：commit 測試** — `git commit -m "test(policy): 新增 OD-06 政策宣告的失敗測試"`（含 trailer）。
- [ ] **Step 4：實作** 修改 `.project-policy.yml` 與 `CLAUDE.md`，新增 `docs/contracts-governance.md`。
- [ ] **Step 5：確認 PASS** — `python3 -m pytest -q tests/test_contracts_policy_decl.py tests/test_contracts_validate.py`；Expected：全部 PASS，包含 **`test_real_repo_contract_check_passes`**（Phase 出口測試）。
- [ ] **Step 6：全套與政策** — `python3 -m pytest -q tests/`；`python3 -m policy_check --repo .`：R-08 PASS（新鍵不被拒）、R-26 PASS（4 個 generated-fact 同步）、R-09 對 `contracts/**` 變更要求 fragment（本 commit 已附）、R-18 不再對 `contracts/**`／`tools/contracts/**` 警告（`docs/contracts-governance.md` 已同步）。
- [ ] **Step 7：commit**

`changelog.d/rust-p0-od06-policy-declaration.md`：

```markdown
---
type: change
scope: policy
---
`.project-policy.yml` 宣告 OD-06 診斷碼文件覆蓋設定（抽取命令、說明文件、條目格式）、以 R-26 同步 SKILL 受管區段、將 `contracts/**` 納入 code_paths，並在 preflight 加入 `python3 -m tools.contracts check`；CLAUDE.md 新增契約治理政策段。
```

`git add .project-policy.yml CLAUDE.md docs/contracts-governance.md tests/test_contracts_policy_decl.py tests/test_contracts_validate.py changelog.d/rust-p0-od06-policy-declaration.md`；`git commit -m "chore(policy): 宣告 OD-06 診斷碼覆蓋與契約 preflight" -m "Co-authored-by: Copilot <223556219+Copilot@users.noreply.github.com>"`

**BLOCKED-OWNER：** serialwrap PR 的 merge；`.github/workflows/` 引擎 pin 由 v1.0.18 升級到含 R-27 的正式版（依賴 T13 的上游 release）。

---

## Task T13：paulsha-conventions 通用規則 R-27（本地分支，不 push）

**Test author model:** haiku-xhigh　**Implementer model:** agy-gemini-3.8-flash-high

**工作目錄：** 以 worktree 隔離，不動 `$CONV_REPO`（paulsha-conventions 本機 checkout，執行者依本機佈局設定）目前 checkout 的 `main`：

```bash
: "${CONV_REPO:?請設定 paulsha-conventions 本機 checkout 路徑}"
CONV_WT="${CONV_WT:-$(dirname "$CONV_REPO")/paulsha-conventions-worktrees/od06-diagnostic-codes}"
git -C "$CONV_REPO" fetch --no-tags origin main   # 若網路不可用，直接使用本地已有的 origin/main（65d9e43）
git -C "$CONV_REPO" worktree add "$CONV_WT" \
  -b feature/od06-diagnostic-code-coverage origin/main
```

以下路徑相對 `$CONV_WT`。

**Files:**
- Create: `policy_check/rules/r27_diagnostic_code_coverage.py`、`tests/test_rule_r27_diagnostic_code_coverage.py`、`changelog.d/od06-diagnostic-code-coverage.md`
- Modify: `policy_check/rules/families.py`（`DOC-ALIGN` 加 `"R-27"`）、`README.md`（英文 `### Rules (R-01 ~ R-26)` 與中文 `### 規則總覽（R-01 ~ R-26）` **兩張表**都改 `R-27`、各加一列，並在兩段設定範例各加 `diagnostic_codes` 範例）

**Interfaces:**
- Consumes：`policy_check.rules.base`（`RuleContext`、`RuleResult`、`Status`）、`policy_check.rules.registry.register`、`policy_check.rules._marker_sync.run_command`。
- Produces：

```python
@register
class R27DiagnosticCodeCoverage:
    rule_id = "R-27"
    exempt_label = None    # opt-in；未宣告 diagnostic_codes 即不適用
    def check(self, ctx: RuleContext) -> RuleResult: ...
```

**規則語意（通用，不得內建任何碼）：**

1. `ctx.config.get("diagnostic_codes")` 為 None → `PASS`「No diagnostic_codes declared (not applicable).」。
2. 非 list，或任一項不是 mapping → `FAIL`。每項必填：`namespace`（非空 str，項目間唯一）、`extract`（非空 str 命令）、`docs`（非空 list[str]，repo 相對路徑，不得絕對、不得含 `..`）、`entry_pattern`（可編譯的 regex，且具名群組含 `code`、`meaning`、`action`）。選填：`require_in ∈ {all, any}`（預設 `all`）、`timeout_seconds`（int，1～300，預設 30）。
3. 以 `run_command(extract, ctx.repo_root, timeout=timeout_seconds)` 執行；`OSError`／`SubprocessError` 或非 0 exit → `FAIL`（detail 含 namespace 與 exit code）。
4. stdout 每行 `strip()`；忽略空行與 `#` 開頭行；其餘為碼（去重）。碼含空白 → `FAIL`。
5. 每份 doc 不存在 → `FAIL`。對每份 doc 以 `re.finditer(entry_pattern, text, re.MULTILINE)` 建表，只收 `meaning.strip()` 與 `action.strip()` 皆非空的條目。
6. 依 `require_in` 判定每碼是否已說明；缺漏碼排序後於 detail 列出 `<namespace>: <code> missing in <doc>`（最多 50 行，其餘以 `... and N more` 結尾）→ `FAIL`。
7. 全部通過 → `PASS`「<總碼數> code(s) across <namespace 數> namespace(s) documented.」。
8. 規則模組不得含任何看似錯誤碼的字串常數（docstring 以外，不得有符合 `^[A-Z][A-Z0-9]*_[A-Z0-9_]+$` 的字面）。

- [ ] **Step 1：寫失敗測試**（`tests/test_rule_r27_diagnostic_code_coverage.py`；沿用 `tests/test_rule_r25_doc_coverage.py` 的 `_init_repo`／`_write` 寫法）

```python
from __future__ import annotations

import ast
import re
import sys
from pathlib import Path

from policy_check import config as cfg
from policy_check.rules import registry
from policy_check.rules.base import RuleContext, Status

PATTERN = r'^\| `(?P<code>[^`]+)` \| (?P<meaning>[^|\n]*\S[^|\n]*) \| (?P<action>[^|\n]*\S[^|\n]*) \|$'


def get_rule():
    loaded = {r.rule_id: r for r in registry.load_all()}
    assert "R-27" in loaded, "R-27 is not registered"
    return loaded["R-27"]


def _repo(tmp_path: Path, codes: list[str], doc: str, extra: str = "") -> Path:
    (tmp_path / "tools").mkdir()
    (tmp_path / "tools" / "emit.py").write_text("print(%r)\n" % "\n".join(codes), encoding="utf-8")
    (tmp_path / "SKILL.md").write_text(doc, encoding="utf-8")
    (tmp_path / ".project-policy.yml").write_text(
        "policy_profile: flat\npolicy_version: 1.0.18\n"
        "diagnostic_codes:\n"
        "  - namespace: error_code\n"
        f"    extract: \"{sys.executable} tools/emit.py\"\n"
        "    docs: [SKILL.md]\n"
        f"    entry_pattern: '{PATTERN}'\n" + extra,
        encoding="utf-8")
    return tmp_path


def _ctx(repo: Path) -> RuleContext:
    return RuleContext(repo_root=repo, profile="flat", policy_version="1.0.18", config=cfg.load(repo))


def test_not_applicable_without_config(tmp_path):
    (tmp_path / ".project-policy.yml").write_text("policy_profile: flat\npolicy_version: 1.0.18\n", encoding="utf-8")
    assert get_rule().check(_ctx(tmp_path)).status == Status.PASS


def test_all_documented_passes(tmp_path):
    repo = _repo(tmp_path, ["A_ONE", "B_TWO"], "| `A_ONE` | 意義 | 處置 |\n| `B_TWO` | 意義 | 處置 |\n")
    assert get_rule().check(_ctx(repo)).status == Status.PASS


def test_missing_code_fails_and_names_it(tmp_path):
    repo = _repo(tmp_path, ["A_ONE", "B_TWO"], "| `A_ONE` | 意義 | 處置 |\n")
    result = get_rule().check(_ctx(repo))
    assert result.status == Status.FAIL
    assert "B_TWO" in result.detail


def test_rule_has_no_builtin_code_list():
    import policy_check.rules.r27_diagnostic_code_coverage as mod
    tree = ast.parse(Path(mod.__file__).read_text(encoding="utf-8"))
    docstrings = {id(n.body[0].value) for n in ast.walk(tree)
                  if isinstance(n, (ast.Module, ast.FunctionDef, ast.ClassDef)) and n.body
                  and isinstance(n.body[0], ast.Expr) and isinstance(n.body[0].value, ast.Constant)}
    offenders = [n.value for n in ast.walk(tree)
                 if isinstance(n, ast.Constant) and isinstance(n.value, str) and id(n) not in docstrings
                 and re.fullmatch(r"[A-Z][A-Z0-9]*_[A-Z0-9_]+", n.value)]
    assert offenders == []
```

| test 名稱 | 情境 | 斷言 |
|---|---|---|
| `test_empty_action_counts_as_missing` | doc 列 action 欄為空白 | FAIL，detail 含該碼 |
| `test_require_in_all_needs_every_doc` | 兩份 doc、碼只在一份 | FAIL |
| `test_require_in_any_accepts_one_doc` | 同上加 `require_in: any` | PASS |
| `test_extract_nonzero_exit_fails` | `extract` 為 `python -c "import sys; sys.exit(3)"` | FAIL，detail 含 `3` |
| `test_missing_doc_fails` | `docs: [NOPE.md]` | FAIL |
| `test_pattern_without_named_groups_fails` | `entry_pattern: '^x$'` | FAIL |
| `test_duplicate_namespace_fails` | 兩項同 namespace | FAIL |
| `test_absolute_doc_path_rejected` | `docs: [/etc/passwd]` | FAIL |
| `test_comment_and_blank_lines_ignored` | extract 輸出含 `# x` 與空行 | 不當作碼 |
| `test_detail_truncated_after_50` | 60 個未說明的碼 | detail 恰 50 行碼＋一行 `... and 10 more` |
| `test_family_registration` | `families.family_of("R-27") == "DOC-ALIGN"` | True |
| `test_readme_both_rule_tables_list_r27` | README 英文 `### Rules (R-01 ~ R-27)` 與中文 `### 規則總覽（R-01 ~ R-27）` 兩個標題都存在，且兩張表各有一列以 `| R-27 |` 開頭 | True |
| `test_parity_fixtures`（參數化 `tests/fixtures/r27_parity/*.yaml`） | 每個 fixture 宣告 `codes`、`docs`（檔名→內容）、`require_in`、`expected: {status, missing: [...]}`；R-27 結果的 status 與缺漏碼集合等於 `expected` | True |

- [ ] **Step 2：確認 FAIL** — 在 conventions worktree：`python3 -m pytest -q tests/test_rule_r27_diagnostic_code_coverage.py`；Expected：FAIL（`R-27 is not registered`）；另 `python3 -m pytest -q tests/test_families.py` 在實作後才應 PASS。
- [ ] **Step 3：commit 測試**（conventions repo 規則：分支 `feature/*`，fragment 隨實作 commit）

```bash
git add tests/test_rule_r27_diagnostic_code_coverage.py
git commit -m "test(r27): 新增診斷碼文件覆蓋規則的失敗測試" -m "Co-authored-by: Copilot <223556219+Copilot@users.noreply.github.com>"
```

- [ ] **Step 4：實作**

```python
"""R-27 — diagnostic code doc coverage（OD-06，opt-in）。

repo 以 `diagnostic_codes` 宣告抽取命令與說明文件；規則不內建任何碼。
"""
from __future__ import annotations

import re
import subprocess
from pathlib import PurePosixPath

from policy_check.rules.base import RuleContext, RuleResult, Status
from policy_check.rules.registry import register
from policy_check.rules._marker_sync import run_command

_MAX_LISTED = 50
_REQUIRED_GROUPS = {"code", "meaning", "action"}


@register
class R27DiagnosticCodeCoverage:
    rule_id = "R-27"
    exempt_label = None

    def check(self, ctx: RuleContext) -> RuleResult:
        entries = (ctx.config or {}).get("diagnostic_codes")
        if entries is None:
            return RuleResult(self.rule_id, Status.PASS, "No diagnostic_codes declared (not applicable).")
        problems, missing, total = self._validate(entries), [], 0
        if problems:
            return RuleResult(self.rule_id, Status.FAIL, "diagnostic_codes is misconfigured.", detail="\n".join(problems))
        for entry in entries:
            codes, err = self._extract(entry, ctx)
            if err:
                return RuleResult(self.rule_id, Status.FAIL, "diagnostic code extraction failed.", detail=err)
            total += len(codes)
            missing += self._missing(entry, codes, ctx)
        if missing:
            missing.sort()
            listed = missing[:_MAX_LISTED]
            if len(missing) > _MAX_LISTED:
                listed.append(f"... and {len(missing) - _MAX_LISTED} more")
            return RuleResult(self.rule_id, Status.FAIL, "emitted diagnostic codes lack documented meaning/action.",
                              detail="\n".join(listed))
        return RuleResult(self.rule_id, Status.PASS,
                          f"{total} code(s) across {len(entries)} namespace(s) documented.")
```

`_validate`、`_extract`（`run_command(..., timeout=entry.get("timeout_seconds", 30))`，捕捉 `OSError`／`subprocess.SubprocessError`）、`_missing`（依 `require_in` 判定，回傳 `"<namespace>: <code> missing in <doc>"` 字串）依「規則語意」實作。`families.py` 的 `("DOC-ALIGN", ("R-18", "R-22", "R-24", "R-25"))` 改為含 `"R-27"`。README 表加一列：`| R-27 | diagnostic code doc coverage (opt-in, OD-06) | when a repo declares diagnostic_codes: a code printed by the declared extract command has no doc entry with non-empty meaning and action in the declared docs, the command fails, or the config is invalid | — |`，並在設定範例段加 `diagnostic_codes` 範例（不得含真實碼表）。

`changelog.d/od06-diagnostic-code-coverage.md`：

```markdown
---
type: feat
scope: r27
---
新增 opt-in 規則 R-27：repo 以 `diagnostic_codes` 宣告抽取命令、說明文件與條目格式，凡抽取命令列出的診斷碼都必須在文件中有非空的意義與處置；規則本身不內建任何錯誤碼。
```

- [ ] **Step 5：確認 PASS**

Run（conventions worktree）：`python3 -m pytest -q`；Expected：全部 PASS（含 `tests/test_families.py` 完整性測試）。
Run（conventions worktree）：`python3 -m policy_check --repo .`；Expected：無 FAIL。
Run（對 serialwrap 驗證 R-27 實際判定）：

```bash
cd "$SW_ROOT"
PYTHONPATH="$CONV_WT" \
  python3 -c "from pathlib import Path; from policy_check import config as c; \
from policy_check.rules import registry; from policy_check.rules.base import RuleContext; \
r={x.rule_id:x for x in registry.load_all()}['R-27']; \
res=r.check(RuleContext(repo_root=Path('.'), profile='flat', policy_version='1.0.18', config=c.load(Path('.')))); \
print(res.status, res.message); print(res.detail)"
```

Expected：`Status.PASS` 並列出碼數（只判讀 R-27；不以此本地引擎的其他規則結果作為 serialwrap 政策判定）。

**C13 ↔ R-27 語意對等驗證：** 把 conventions 側 `tests/fixtures/r27_parity/*.yaml`（至少 6 個：全部有說明、缺一碼、action 空白、`require_in: all` 兩檔缺一、`require_in: any` 兩檔有一、含註解與空行的抽取輸出）逐字複製到 serialwrap `tests/fixtures/r27_parity/`，並在 serialwrap `tests/test_contracts_validate.py` 新增 `test_c13_parity_fixtures`（參數化同一組 fixture，以 `check_doc_coverage` 判定，斷言 FAIL／PASS 與缺漏碼集合等於 fixture 的 `expected`）。兩邊對同一組 fixture 的 `expected` 相同即證明語意對等；fixture 檔的 sha256 寫進 Step 7 的 PR 描述紀錄，任一邊修改 fixture 必須同步另一邊。

- [ ] **Step 6：commit（不 push）**

```bash
git add policy_check/rules/r27_diagnostic_code_coverage.py policy_check/rules/families.py README.md tests/fixtures/r27_parity changelog.d/od06-diagnostic-code-coverage.md
git commit -m "feat(r27): 新增通用診斷碼文件覆蓋規則（OD-06）" -m "Co-authored-by: Copilot <223556219+Copilot@users.noreply.github.com>"
git log --oneline -3   # 確認只在 feature/od06-diagnostic-code-coverage，未 push
```

- [ ] **Step 7：記錄** 在 serialwrap 的 PR 描述（T12 之後）註明 conventions 分支名、commit SHA 與「尚未 push／release」。

**Fragment（serialwrap 側）：** `changelog.d/rust-p0-r27-parity-fixtures.md`（`type: change`、`scope: tests`：新增 C13 與 R-27 共用的語意對等 fixture 與測試；`tests/**` 屬 `code_paths`，R-09 需要 fragment）。serialwrap 側 commit：`git add tests/fixtures/r27_parity tests/test_contracts_validate.py changelog.d/rust-p0-r27-parity-fixtures.md`；`git commit -m "test(contracts): 新增 C13 與 R-27 語意對等 fixture" -m "Co-authored-by: Copilot <223556219+Copilot@users.noreply.github.com>"`。

**BLOCKED-OWNER：** conventions 分支 push、PR、review、merge 與 v1.0.19（或擁有者選定版號）release；serialwrap 引擎 pin SHA 更新與 `CLAUDE.md` policy_version 同步（皆需擁有者授權推送）。

---

## Task T14：pytest 平台／資源 marker 註冊與逐檔標註

**Test author model:** codex-gpt-6-luna-max（平台判定規則屬高風險：誤標會讓記憶體替身被當成原生覆蓋）　**Implementer model:** haiku-xhigh（機械式逐檔標註）

**Files:**
- Modify: `tests/conftest.py`（新增 `pytest_configure`）、全部既有 `tests/test_*.py` 與本 Phase 新增測試檔（加 marker）、`CLAUDE.md`（測試政策段補 marker 用法）
- Create: `tests/test_resource_markers.py`
- Fragment: `changelog.d/rust-p0-pytest-resource-markers.md`

**Interfaces:**
- Consumes：§11.2 分類表；盤點 08 §1.5（BB-proc、BB-script、WB 分組與 F 節平台 skip 清單）。
- Produces：10 個 marker；P2（`03-stage0-test-infra.md`）與 P3+ 的 CI 以 `-m` 選取（例：`-m "core_portable and memory_only"`、`-m posix_pty`）。**本 Task 是 marker 註冊、檔內標註與稽核的唯一 owner**（調和表 TST-D-4 原列 P2，本 Phase 接手標註；P2 只保留報告）：
  - P1 `02-stage0-python-bc.md` T01 只保留 `pytest_collection_modifyitems` 的平台自動 skip（`windows_native` 非 Windows、`windows_com` 無 `SW_TEST_COM_PAIR`、`posix_pty` 無 `os.openpty`），**刪除**它自己的 `pytest_configure` marker 註冊；P1 新增的測試檔以本 Task 規則標 `pytestmark`（例：只標 `posix_pty` 而沒有平台 marker 的寫法會被稽核判 FAIL）。
  - P2 `03-stage0-test-infra.md` Task 13 不建立 `tests/platform_resources.yaml`、不在 `modifyitems` 套 marker，只保留 `SW_RESOURCE_REPORT` 報告與 `core_portable_platform_skips` 閘門，報告讀取本 Task 的檔內 marker。
  - P1～P9 新增的每個測試檔都必須通過本 Task 的稽核測試。

**marker 定義（`pytest_configure` 逐字註冊）：**

```python
_MARKERS = {
    "core_portable": "Linux 與 Windows 都必跑；禁止以平台原因 SKIP；不得宣稱測到原生 UART",
    "posix_only": "只在 POSIX 執行（PTY、POSIX 鎖、/proc、/dev/ttyMCU、AF_UNIX）",
    "windows_native": "只在原生 Windows 執行（檔案分享旗標、鎖、行程、排程器、原生裝置介面）",
    "memory_only": "只用行程內物件與暫存檔；不開 socket、子行程、PTY 或裝置",
    "process_rpc": "會啟動子行程或開真實 socket／RPC server",
    "posix_pty": "會開 POSIX PTY pair",
    "windows_com": "以原生 Windows serial backend 真正開啟 COM 裝置（socket 或記憶體 stream 不算）",
    "physical_uart": "需要實體 USB-UART 與 DUT",
    "supervisor_runtime": "需要真正運作中的 systemd／Task Scheduler／Windows service",
    "interactive_desktop": "需要登入中的桌面或終端多工器（tmux 等）",
}


def pytest_configure(config):
    for name, desc in _MARKERS.items():
        config.addinivalue_line("markers", f"{name}: {desc}")
```

**標註規則（稽核測試據此判定）：**

1. 每個 test item 的有效 marker＝模組 `pytestmark` ∪ class 的 `pytestmark`／decorator ∪ 函式 decorator。平台 marker（`core_portable`／`posix_only`／`windows_native`）恰一個；資源 marker 至少一個；`memory_only` 不得與其他資源 marker 並存。
2. 同檔混合平台時，**不得**在模組層放平台 marker，改放在 class／函式層。同檔混合資源（例：大多 `memory_only`、少數開 subprocess）時，**不得**在模組層放資源 marker，改放在 class／函式層——marker 是聯集語意，子層無法覆蓋模組層，模組層 `memory_only` 加上函式層 `process_rpc` 一定違反第 1 條的互斥。稽核以 `test_module_resource_marker_not_mixed` 強制：模組層 `pytestmark` 含資源 marker 時，同檔任何 class／函式不得再加不同的資源 marker；模組層含平台 marker 時，子層不得再加平台 marker。
3. 偵測（AST，作用範圍＝該 class 或頂層函式本體＋其以 Name 直接引用的同檔頂層 def／class，一層）：
   - `posix_pty`：引用 `pty` 模組、`os.openpty`、`pty.openpty`、`termios`。
   - `process_rpc`：`subprocess.Popen/run/check_output/call`、`socket.socket(`、`socketserver`、`asyncio.start_server/start_unix_server/open_connection/open_unix_connection`、`JsonRpcUnixServer(`、`TcpRpcServer(`。
   - `interactive_desktop`：字串常數 `"tmux"`。
   - 平台條件 SKIP：`skipif`／`skipIf`／`skipUnless` 的條件原始碼符合 `sys\.platform|os\.name|platform\.system|AF_UNIX|openpty|geteuid|fcntl|msvcrt|win32|_IS_WIN|IS_WINDOWS|posix`。
4. 偵測到的資源必須出現在 item 的資源 marker 中；偵測到 `posix_pty`、`AF_UNIX`、`fcntl` 的 item 平台必須是 `posix_only`；偵測到 `msvcrt` 者必須是 `windows_native`；帶平台條件 SKIP 的 item 不得是 `core_portable`。
5. 豁免：模組層 `RESOURCE_AUDIT_WAIVERS = {"<resource>": "<非空理由>"}` 字典字面可豁免第 4 條對該資源的偵測要求（例：只比對含 `tmux` 字樣的設定字串、從不啟動 tmux）。不得豁免平台條件 SKIP 規則。
6. 本 Phase **不得**對任何 item 標 `windows_com` 或 `physical_uart`（本環境無對應證據；稽核測試斷言這兩個 marker 的使用數為 0，P5／P2 引入真正測試時再改此斷言並經審閱）。

**起始標註表（依盤點 08 與 AST 掃描；其餘檔案預設模組層 `pytestmark = [pytest.mark.core_portable, pytest.mark.memory_only]`，再依稽核結果修正）：**

| 檔案 | 標註 |
|---|---|
| `test_multiagent_e2e.py` | 模組：`posix_only`、`posix_pty`、`process_rpc` |
| `test_human_agent_coexist.py` | 模組：`posix_only`、`posix_pty`、`process_rpc`、`interactive_desktop` |
| `test_interactive_raw.py`、`test_uart_io.py`、`test_uart_flash_bridge.py`、`test_flash_pump.py`、`test_flash_service_wiring.py`、`test_event_engine.py`、`test_command_completion.py`、`test_console_unlock.py`、`test_cowork_session_usability.py`、`test_concurrency_races.py` | 使用 PTY 的 class：`posix_only`、`posix_pty`；不使用者可留 `core_portable`、`memory_only`（依偵測範圍拆分） |
| `test_agent_defer_tx.py` | 模組：`posix_only`、`posix_pty`、`process_rpc` |
| `test_flash_endpoint.py` | 模組：`posix_only`、`posix_pty`（模組層 POSIX skipif） |
| `test_serial_port.py` | `skipUnless(posix and openpty)` 的 class：`posix_only`、`posix_pty`；pyserial 後端 class：`core_portable`、`memory_only` |
| `test_cli_timeout_robustness.py` | `_UnixRpcServerMixin` 系 class：`posix_only`、`process_rpc`；其餘 `core_portable`、`memory_only` |
| `test_cli_diagnostics.py` | `:359` AF_UNIX class：`posix_only`、`process_rpc`；`_TcpReplySocket` 系：`core_portable`、`process_rpc` |
| `test_remote_endpoint.py` | `:70`、`:82` AF_UNIX：`posix_only`、`process_rpc`；`TestRpcCallOverRealTcpSocket`：`core_portable`、`process_rpc` |
| `test_rpc_tcp.py`、`test_issue52_rpc_concurrency.py` | `core_portable`、`process_rpc` |
| `test_rpc_blocking_offload.py` | `posix_only`、`process_rpc` |
| `test_lock_win.py` | `windows_native`、`memory_only` |
| `test_win_daemon_start.py` | `:311` class：`windows_native`、`process_rpc`；其餘 `core_portable`、`memory_only` |
| `test_win_cli_endpoint.py` | `:126`：`windows_native`；`:121`：`posix_only`；其餘 `core_portable`（資源依偵測） |
| `test_daemon_service_selector.py` | `:63`、`:98` 的 skipif item：依條件為 `posix_only` 或 `windows_native`；其餘 `core_portable`、`memory_only` |
| `test_wal_missing_detection.py` | `:97` `skipIf(geteuid==0)` 所在 class：`posix_only`、`memory_only` |
| `test_minicom_router.py` | 模組：`posix_only`、`process_rpc`、`posix_pty`（`script` 指令配 PTY） |
| `test_bench_tools_cli.py`、`test_transfer_portability.py` | 模組：`posix_only`、`process_rpc`（bash、`/bin/sh`、`openssl`） |
| `test_bootloader_recovery.py` | CLI `--help` subprocess 的 class：`core_portable`、`process_rpc`；其餘 `core_portable`、`memory_only` |
| `test_realhw_preflight.py`、`test_reliability_core.py`、`test_realhw_cases.py`、`test_realhw_harness.py`、`test_realhw_main.py`、`test_regression_guards.py`、`test_regression_harness.py`、`test_reliability_pluginfiles.py`、`test_reliability_testbed.py` | `core_portable`、`memory_only`；若僅含 `"tmux"` 設定字串，加 `RESOURCE_AUDIT_WAIVERS = {"interactive_desktop": "只檢查命令字串組裝，不啟動 tmux"}` |
| `test_systemd_units.py`、`test_wsl_systemd.py`、`test_service_ctl.py`、`test_setup_reconcile.py` | `core_portable`、`memory_only`（FakeEffects；不涉真正 systemd，故不標 `supervisor_runtime`） |
| `test_contracts_*.py`（本 Phase） | 純 memory 的檔：模組層 `core_portable`、`memory_only`。混合資源檔（`test_contracts_validate.py` 的 CLI subprocess 與出口測試、`test_contracts_skill_section.py` 的 subprocess、`test_contracts_keep_baseline.py`、`test_contracts_doc_errata.py`）：模組層只放 `core_portable`，資源 marker 放函式／class 層；混合平台檔（`test_contracts_gap_codes.py` 的 `TestGapPosixLock`、`test_contracts_rpc_schema.py` 的 `test_line_limit_capture_reproducible` 所在 POSIX class）：模組層不放任何 marker，每個 class／函式各自帶平台與資源 marker（依 T7 的 class 分組方式） |
| `test_resource_markers.py`（本 Task） | 模組層只放 `core_portable`；AST 稽核函式各 `memory_only`；`test_markers_registered_in_conftest` 為 `process_rpc` |

- [ ] **Step 1：寫失敗測試**（`tests/test_resource_markers.py`）

```python
from __future__ import annotations

import ast
import re
import subprocess
import sys
from dataclasses import dataclass, field
from pathlib import Path

import pytest

# 本檔混合 memory_only（AST 稽核）與 process_rpc（subprocess），依規則 2 模組層只放平台 marker
pytestmark = [pytest.mark.core_portable]

TESTS = Path(__file__).resolve().parent
PLATFORM = {"core_portable", "posix_only", "windows_native"}
RESOURCE = {"memory_only", "process_rpc", "posix_pty", "windows_com", "physical_uart",
            "supervisor_runtime", "interactive_desktop"}
BUILTIN = {"parametrize", "skipif", "skip", "xfail", "usefixtures", "filterwarnings"}
PLATFORM_COND = re.compile(r"sys\.platform|os\.name|platform\.system|AF_UNIX|openpty|geteuid|fcntl|msvcrt"
                           r"|win32|_IS_WIN|IS_WINDOWS|posix")


def _mark_names(node: ast.AST) -> set[str]:
    out = set()
    for sub in ast.walk(node):
        if (isinstance(sub, ast.Attribute) and isinstance(sub.value, ast.Attribute)
                and sub.value.attr == "mark" and isinstance(sub.value.value, ast.Name)
                and sub.value.value.id == "pytest"):
            out.add(sub.attr)
    return out


def _pytestmark(body: list[ast.stmt]) -> set[str]:
    out = set()
    for stmt in body:
        if isinstance(stmt, ast.Assign) and any(isinstance(t, ast.Name) and t.id == "pytestmark" for t in stmt.targets):
            out |= _mark_names(stmt.value)
    return out


def _platform_skip(decorators: list[ast.expr], src: str) -> bool:
    for d in decorators:
        text = ast.get_source_segment(src, d) or ""
        if re.search(r"skip(if|If|Unless)\(", text) and PLATFORM_COND.search(text):
            return True
    return False


@dataclass
class Item:
    file: str
    name: str
    marks: set[str] = field(default_factory=set)
    platform_skip: bool = False
    scope: ast.AST | None = None


def _decor_marks(decorators: list[ast.expr]) -> set[str]:
    return set().union(*(_mark_names(d) for d in decorators))


def _module_skip(tree: ast.Module, src: str) -> bool:
    for stmt in tree.body:
        if isinstance(stmt, ast.Assign) and any(isinstance(t, ast.Name) and t.id == "pytestmark" for t in stmt.targets):
            seg = ast.get_source_segment(src, stmt) or ""
            if "skip" in seg and PLATFORM_COND.search(seg):
                return True
    return False


def _items(path: Path) -> list[Item]:
    src = path.read_text(encoding="utf-8")
    tree = ast.parse(src)
    module_marks = _pytestmark(tree.body)
    module_skip = _module_skip(tree, src)
    items: list[Item] = []
    for node in tree.body:
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and node.name.startswith("test"):
            items.append(Item(path.name, node.name,
                              module_marks | _decor_marks(node.decorator_list),
                              module_skip or _platform_skip(node.decorator_list, src), node))
        if isinstance(node, ast.ClassDef) and node.name.startswith("Test"):
            cls_marks = _pytestmark(node.body) | _decor_marks(node.decorator_list)
            cls_skip = _platform_skip(node.decorator_list, src)
            for fn in node.body:
                if isinstance(fn, (ast.FunctionDef, ast.AsyncFunctionDef)) and fn.name.startswith("test"):
                    items.append(Item(path.name, f"{node.name}.{fn.name}",
                                      module_marks | cls_marks | _decor_marks(fn.decorator_list),
                                      module_skip or cls_skip or _platform_skip(fn.decorator_list, src), node))
    return items


def _all_items() -> list[Item]:
    return [it for p in sorted(TESTS.glob("test_*.py")) for it in _items(p)]


@pytest.mark.memory_only
def test_every_item_has_exactly_one_platform_marker():
    bad = [(i.file, i.name, sorted(i.marks & PLATFORM)) for i in _all_items() if len(i.marks & PLATFORM) != 1]
    assert bad == []


@pytest.mark.memory_only
def test_every_item_has_resource_marker_and_memory_only_is_exclusive():
    bad = [(i.file, i.name) for i in _all_items()
           if not (i.marks & RESOURCE) or ("memory_only" in i.marks and len(i.marks & RESOURCE) > 1)]
    assert bad == []


@pytest.mark.memory_only
def test_only_registered_marker_names_used():
    bad = [(i.file, i.name, sorted(i.marks - PLATFORM - RESOURCE - BUILTIN)) for i in _all_items()
           if i.marks - PLATFORM - RESOURCE - BUILTIN]
    assert bad == []


@pytest.mark.memory_only
def test_core_portable_items_have_no_platform_skip():
    bad = [(i.file, i.name) for i in _all_items() if "core_portable" in i.marks and i.platform_skip]
    assert bad == []


@pytest.mark.memory_only
def test_no_windows_com_or_physical_uart_claims_in_p0():
    bad = [(i.file, i.name) for i in _all_items() if i.marks & {"windows_com", "physical_uart"}]
    assert bad == []


@pytest.mark.memory_only
def test_module_resource_marker_not_mixed():
    bad = []
    for p in sorted(TESTS.glob("test_*.py")):
        tree = ast.parse(p.read_text(encoding="utf-8"))
        mod = _pytestmark(tree.body)
        for it in _items(p):
            child = it.marks - mod
            if (mod & RESOURCE) and (child & RESOURCE):
                bad.append((it.file, it.name, "resource", sorted(child & RESOURCE)))
            if (mod & PLATFORM) and (child & PLATFORM):
                bad.append((it.file, it.name, "platform", sorted(child & PLATFORM)))
    assert bad == []


@pytest.mark.process_rpc
def test_markers_registered_in_conftest():
    # 帶 "tests" 參數，pytest 才會把 tests/conftest.py 當 initial conftest 載入
    out = subprocess.run([sys.executable, "-m", "pytest", "--markers", "-p", "no:cacheprovider", "tests"],
                         cwd=TESTS.parent, capture_output=True, text=True, timeout=300).stdout
    for name in PLATFORM | RESOURCE:
        assert f"@pytest.mark.{name}:" in out, name
```

另加 `test_detected_resources_are_marked`：對每個 item 的 `scope`（class 或函式）及其一層同檔 helper，依「標註規則」第 3 條偵測資源，斷言偵測集合（扣模組 `RESOURCE_AUDIT_WAIVERS` 鍵）⊆ item 資源 marker，且 PTY／AF_UNIX／fcntl → `posix_only`、msvcrt → `windows_native`；失敗訊息列出 `(file, item, missing)`。

- [ ] **Step 2：確認 FAIL** — `python3 -m pytest -q tests/test_resource_markers.py`；Expected：`test_every_item_has_exactly_one_platform_marker`、`test_every_item_has_resource_marker_and_memory_only_is_exclusive`、`test_detected_resources_are_marked`、`test_markers_registered_in_conftest` FAIL（既有檔無 marker；conftest 未註冊）。下列在標註前**空真通過**，屬預期、不可當作已驗證：`test_only_registered_marker_names_used`、`test_core_portable_items_have_no_platform_skip`、`test_no_windows_com_or_physical_uart_claims_in_p0`、`test_module_resource_marker_not_mixed`（尚無 marker 可比對）。為證明這四個測試確實有效，Step 1 另寫 `test_audit_rules_detect_synthetic_violations`：在 `tmp_path` 放四個合成測試檔（各違反一條：未註冊 marker、`core_portable`＋平台 skipif、`windows_com`、模組 `memory_only`＋函式 `process_rpc`），以同一組 `_items` 函式稽核，斷言四條規則各抓到一筆——此測試在 Step 2 必須 PASS。
- [ ] **Step 3：commit 測試** — `git commit -m "test(markers): 新增平台與資源 marker 稽核測試"`（含 trailer）。
- [ ] **Step 4：實作** — `tests/conftest.py` 加 `_MARKERS` 與 `pytest_configure`（放在檔尾，不得 import `sw_core`，不得移動 tripwire 之前的程式；這是 marker 註冊的唯一定義處，P1 T01 的 auto-skip hook 之後合併進同一檔但不得重複註冊）；依起始標註表與稽核結果替每個測試檔加 marker；`CLAUDE.md` 測試政策段補：marker 定義、`-m` 選取範例、`core_portable` 禁止平台 SKIP、`windows_com` 只能由原生 COM 測試使用。
- [ ] **Step 5：確認 PASS**
  - `python3 -m pytest -q tests/test_resource_markers.py` → PASS。
  - `python3 -m pytest -q tests/` → 與標註前相同的 passed／skipped 數（marker 不得改變結果）；把兩次 summary 行寫進 commit body。
  - `python3 -m pytest -q -m "core_portable" tests/ --co | tail -1` 與 `-m "posix_only"`、`-m "windows_native"` 三者 item 數相加等於全部 item 數；把三個數字寫進 commit body（作為後續任何重新分類的基準）。
  - **Windows 原生 core_portable 驗證（WSL interop）**：先凍結本次分類（上一步的三個 item 數），再匯出**目前工作樹**（含尚未 commit 的 marker 與 conftest 修改；`git archive HEAD` 會拿到 Step 3 的純測試 commit，`-m core_portable` 會選不到任何 item）：

    ```bash
    set -eu
    WIN_HOME=$(wslpath "$(cmd.exe /c 'echo %USERPROFILE%' 2>/dev/null | tr -d '\r')")
    [ -n "$WIN_HOME" ] && [ -d "$WIN_HOME" ] || { echo "WIN_HOME 無效：$WIN_HOME" >&2; exit 2; }
    DEST="$WIN_HOME/sw-p0-wincheck"
    case "$DEST" in "$WIN_HOME"/sw-p0-wincheck) ;; *) echo "DEST 異常" >&2; exit 2;; esac
    rm -rf -- "$DEST" && mkdir -p -- "$DEST/src" "$DEST/deps"
    git ls-files -z --cached --others --exclude-standard | tar --null -T - -c | tar -x -C "$DEST/src"
    (cd "$DEST/src" && find . -type f -print0 | sort -z | xargs -0 sha256sum) > "$DEST/manifest.sha256"
    cmd.exe /c "cd /d %USERPROFILE%\\sw-p0-wincheck && py -3.12 -m pip install -q --target deps pytest pyyaml pyserial jsonschema==4.10.3 && set \"PYTHONPATH=%USERPROFILE%\\sw-p0-wincheck\\deps\" && cd src && py -3.12 -m pytest -q -m core_portable tests --co > ..\\win-collect.txt && py -3.12 -m pytest -q -m core_portable tests > ..\\win-run.txt"
    ```

    （依賴裝進 `deps/` 並以 `PYTHONPATH` 使用，不改動擁有者 Windows 的 user／system site-packages；`manifest.sha256` 綁定本次受測的檔案集合。）
    Expected：Windows collected 的 `core_portable` item 數等於 Linux 的 `core_portable` 數；無 FAIL、無以平台原因產生的 SKIP。保存 `win-collect.txt`、`win-run.txt`、`manifest.sha256` 至 `~/b-log/rust-refactor/p0/` 並把兩平台的 collected／selected／passed 數寫進 commit body。
    **重新分類的限制（§10.4，不得以縮小分母取得全綠）：** Windows 失敗的 `core_portable` item 只有在「測試本身直接依賴 POSIX 設施」（PTY、AF_UNIX、`fcntl`、`/proc`、POSIX 路徑語意，且能指出測試中的具體行）時才可改標 `posix_only`，每筆須在測試旁加註解寫明根因與行號、經非實作者的獨立審閱者確認，並在 commit body 列出重新分類前後的 `core_portable` 數；失敗指向產品程式（`sw_core/**`）的 portable 路徑時**不得**重新分類，保留 FAIL 並開 P2 修復項。若 `tests/conftest.py`／`tests/liveguard.py` 在 Windows 無法載入，記錄錯誤全文並移交 P2（`03-stage0-test-infra.md`），本 Task 以 Linux 稽核結果交付，**不得**宣稱 Windows core_portable 已驗證。
- [ ] **Step 6：全套與政策** — 同 T1。
- [ ] **Step 7：commit**

`changelog.d/rust-p0-pytest-resource-markers.md`：

```markdown
---
type: change
scope: tests
---
pytest 註冊 core_portable／posix_only／windows_native 三種平台 marker 與 memory_only／process_rpc／posix_pty／windows_com／physical_uart／supervisor_runtime／interactive_desktop 七種資源 marker，逐檔標註既有測試，並以 AST 稽核測試防止漏標、誤標與 core_portable 平台 SKIP。
```

`git add tests/ CLAUDE.md changelog.d/rust-p0-pytest-resource-markers.md`；`git commit -m "test(markers): 註冊並標註 pytest 平台與資源 marker" -m "Co-authored-by: Copilot <223556219+Copilot@users.noreply.github.com>"`

**BLOCKED-OWNER：** `windows_com`（OD-08：原生 Windows COM handle，需自架 runner＋com0com 或實體 USB-UART 直連）、`physical_uart`（實體 DUT bench）、`supervisor_runtime`（真正 systemd／`windows-task` 監管，含 >72 h Spike 2）三類 marker 本 Phase 沒有任何可在本環境產出證據的測試；`interactive_desktop` 項目在無 tmux 的 runner 上的執行結果。

---

## Task T15：P0 文件勘誤（調和表指派 P0 的 DOC 列）與 P0 列覆蓋閘門

**Test author model:** agy-gemini-3.8-flash-high　**Implementer model:** haiku-xhigh

**Files:**
- Modify: `sw_core/assets/skill/SKILL.md`、`sw_core/assets/skill/SKILL_WINDOWS.md`（受管區段以外）、`README.md`（中英兩段同步）、`CLAUDE.md`、`docs/design-event-trigger.md`、`docs/serialwrap-spec.md`、`sw_core/file_transfer.py`（僅註解）、`tests/conftest.py`（僅 docstring）
- Modify: `docs/rust-refactor/spec-errata.md`（T7 已建立；本 Task 追加「規格勘誤」各節，每節標題含調和表 ID）
- Create: `contracts/p0_row_coverage.yaml`（調和表 P0 列 → 凍結位置的對照，見下）
- Create: `tests/test_contracts_doc_errata.py`（建立時即依 T14 規則標註：模組層 `core_portable`；檔案文字檢查函式 `memory_only`；`test_spec_final_untouched` 以 subprocess 跑 git，標 `process_rpc`）
- Fragment: `changelog.d/rust-p0-doc-errata.md`

**Interfaces:**
- Consumes：調和表 DOC 列與盤點 09 §「D-D1～D-D11」。
- Produces：與 Python 基線一致的操作文件；`spec-errata.md` 供定稿規格下一版 revision 參照（**不改** `serialwrap-rust-spec-final.md` 本文）。

**勘誤清單：**

| 調和表 ID | 檔案 | 修正 |
|---|---|---|
| EVT-D-D1 | `SKILL.md`「透過 UART 推送／拉取檔案」（`SKILL_WINDOWS.md` 無此節，不改） | `--chunk-size` 預設 2048 → 512 |
| EVT-D-D2 | 同上 | 「pull 省略 `--local` 時回傳檔案內容」→「寫到 daemon 工作目錄的 `basename(remote)`，回應附 `local_path`」 |
| EVT-D-D5、EVT-D-D11 | `SKILL.md`「Event Trigger Engine」（`SKILL_WINDOWS.md` 無此節）、`docs/design-event-trigger.md:33,159` | counter／event log 位置為 `STATE_DIR/events`（非 tmpfs，主機重開機保留）；`selector: ALL` 只在啟動時展開到當下已知 COM |
| EVT-D-D3、EVT-D-D4 | `README.md:2957,2999`（及英文段對應處） | 規則只讀 `*.json`；stdin payload 欄位為 `selector`、`matched_at`（無 `com`、`trigger_ts`） |
| EVT-D-D6、EVT-D-D7、EVT-D-D8 | `docs/design-event-trigger.md:79,249-255,374` | 飽和時丟棄新 fire；`match_skipped.reason` 只 4 種、`per_rule_busy`／`per_daemon_saturated` 只走 `event_dropped`；`matched_line` 等於 `matched_text`、`bridge_generation` 恆 0 |
| EVT-D-D9、WAL-D16 | `CLAUDE.md`「Agent 日誌 capture」 | `log_dir` 優先序只對 YAML explicit target 成立；動態偵測 session 只用 template `log_dir`，跳過 `defaults.log_dir` |
| EVT-D-D10 | `sw_core/file_transfer.py:38-42` 註解 | 512B chunk 整行約 783～800 bytes（取代 ≈741） |
| CLI-11.16 | `CLAUDE.md`「JSON 輸出必須維持緊湊且穩定」 | CLI `_print` 也用 `sort_keys=True` |
| SES-16.6-01 | `CLAUDE.md`「Session 狀態機」與「Platform 行為差異」 | passthrough 是否 probe 由 `ready_probe` 決定 |
| TST-D-18 | `tests/conftest.py` docstring | 「8 檔 per-file 隔離」→「13 檔」 |
| TST-D-5 | `CLAUDE.md` 測試政策「目前無已知恆定失敗」段 | 以 T14 Step 5 實測 summary 行更新 passed／skipped 數與日期 |
| RPC-D-14 | `docs/serialwrap-spec.md` | 「只 listen Unix socket」→ POSIX Unix socket、Windows `tcp://127.0.0.1:48700` |
| PLT-D-02 | `CLAUDE.md`「MCU flash 真機驗證手法」隔離跑法 | 隔離實例必須同時設 `SERIALWRAP_BY_ID_DIR` 與 `SERIALWRAP_BY_PATH_DIR` |
| CLI-11.01、CLI-11.03、CLI-11.11、CLI-11.13、RPC-D-03、RPC-D-04、RPC-D-09、RPC-D-12、ERR-7.3-3、WAL-D1、WAL-D4、UIO-12.7、TST-D-1、TST-D-2、TST-D-3、EVT-D-S2、EVT-D-S8、EVT-D-S9、GAP-X1、GAP-X2、GAP-X3、GAP-X4 | `docs/rust-refactor/spec-errata.md` | 每列一節：規格章節、規格原文摘要、Python 基線事實（引用盤點節）、契約檔對應位置（如 `cli_schema.json` 的 `x-retry`；CLI-11.01 對應 `x-global-options` 的 `--version`、`-v/--verbose` 與 `x-env` 的 `SERIALWRAP_LOG_LEVEL`——規格 §4.2 只列 5 個全域選項；GAP-X3 對應 `session_last_errors.yaml` 的 `USER_ENV_MISSING` `reachable: false`） |

`spec-errata.md` 的「盤點勘誤」節由 T7 建立並填寫（若有）；本 Task 只追加上表最後一列的「規格勘誤」各節，不得重建檔案。

- [ ] **Step 1：寫失敗測試**（`tests/test_contracts_doc_errata.py`）

| test 名稱 | 斷言 |
|---|---|
| `test_skill_chunk_size_512` | `SKILL.md` 不含 `--chunk-size\` 預設 2048`，且含 `預設 512`；`SKILL_WINDOWS.md` 不含 `預設 2048`（該檔無此節，只防誤植） |
| `test_skill_pull_writes_daemon_cwd` | `SKILL.md` 不含 `回傳檔案內容`、含 `local_path`；`SKILL_WINDOWS.md` 不含 `回傳檔案內容` |
| `test_skill_events_not_tmpfs` | `SKILL.md` 不含 `tmpfs 除外`、含 `STATE_DIR/events` |
| `test_readme_rules_json_only` | README 不含 `規則為 JSON/YAML 檔` |
| `test_readme_stdin_fields` | README 不含 `trigger_ts` |
| `test_claude_md_print_sort_keys` | CLAUDE.md「JSON 輸出」段含 `sort_keys=True` 並提到 CLI |
| `test_conftest_docstring_13_files` | `tests/conftest.py` 不含 `8 檔`，含 `13 檔` |
| `test_file_transfer_comment_updated` | `sw_core/file_transfer.py` 不含 `≈ 741` |
| `test_spec_errata_covers_assigned_rows` | `docs/rust-refactor/spec-errata.md` 含上表最後一列所列每個調和表 ID |
| `test_spec_final_untouched` | `git diff --quiet HEAD -- docs/rust-refactor/serialwrap-rust-spec-final.md` 於工作樹回傳 0（以 subprocess 執行；確保勘誤未改定稿本文） |
| `test_p0_rows_all_covered` | 解析 `docs/rust-refactor/plans/spec-reconciliation.md` 所有表格列：第一欄符合 `^[A-Z]{2,4}-`、「階段」欄（倒數第二欄）含 `P0` 者，其 ID 集合 ⊆ `contracts/p0_row_coverage.yaml` 的 `rows[*].id`；且每筆 `frozen_in` 非空、每個目標都存在（`<path>` 檔案存在；`<path>::<node>` 的 node 以 AST 找得到；`docs/rust-refactor/spec-errata.md#<ID>` 的檔內有含該 ID 的標題）。調和表新增 P0 列時本測試立即 FAIL，不得以刪列取得全綠 |

**`contracts/p0_row_coverage.yaml`（本 Task 建立）：** `schema_version: 1`、`revision: 1`、`rows: [{id: <調和表 ID>, frozen_in: [<目標>...], note: <選填>}]`，依 id 排序。每個標 P0 的調和表列（撰寫本計畫時共 89 列；數字只供自查，不得寫進測試）都要指到實際凍結位置，例：`RPC-H-02` → `contracts/golden/rpc/line_limit.json`、`tests/test_contracts_rpc_schema.py::test_line_limit_matches_golden`；`GAP-G-01` → `contracts/daemon_logging.yaml`；`GAP-G-13` → `tests/test_contracts_rpc_schema.py::test_session_list_ready_reconfirm_not_evaluated`；`RPC-D-10`／`PLT-O-04` → `tests/test_contracts_rpc_schema.py::test_foreign_holders_includes_self_pid`；`CLI-11.18` → `contracts/cli_schema.json`、`tests/test_contracts_cli_schema.py::test_fixed_timeouts_have_sources`；DOC 列 → `docs/rust-refactor/spec-errata.md#<ID>` 或本表上方勘誤清單修改的檔案。找不到任何現有 Task 產物可指的列，實作者必須補齊對應的凍結（測試或契約欄位）後再登錄，不得只寫說明文字。

- [ ] **Step 2：確認 FAIL** — `python3 -m pytest -q tests/test_contracts_doc_errata.py`；Expected：除 `test_spec_final_untouched` 外 FAIL；`python3 -m pytest -q tests/test_resource_markers.py` 仍 PASS（新檔的 marker 已合規）。
- [ ] **Step 3：commit 測試** — `git commit -m "test(docs): 新增 P0 文件勘誤的失敗測試"`（含 trailer）。
- [ ] **Step 4：實作** 依勘誤清單修改；README 中英兩段同步；SKILL 修改後重跑 `python3 -m tools.contracts check --only C14` 確認受管區段未被動到。
- [ ] **Step 5：確認 PASS** — 同 Step 2；Expected：全部 PASS。
- [ ] **Step 6：全套與政策** — `python3 -m pytest -q tests/`；`python3 -m policy_check --repo .`（R-16、R-18、R-22、R-26 PASS）；`python3 -m tools.contracts check` exit 0。
- [ ] **Step 7：commit**

`changelog.d/rust-p0-doc-errata.md`：

```markdown
---
type: docs
scope: docs
---
依 Rust 重構調和表修正 SKILL、README、CLAUDE.md、event trigger 設計文件與 file transfer 註解中與 Python 基線不符的描述（chunk 預設 512、pull 寫入位置、event 目錄非 tmpfs、CLI JSON sort_keys 等），並新增 `docs/rust-refactor/spec-errata.md` 記錄定稿規格勘誤。
```

`git add -A sw_core/assets/skill README.md CLAUDE.md docs/design-event-trigger.md docs/serialwrap-spec.md sw_core/file_transfer.py tests/conftest.py docs/rust-refactor/spec-errata.md contracts/p0_row_coverage.yaml tests/test_contracts_doc_errata.py changelog.d/rust-p0-doc-errata.md`；`git commit -m "docs: 修正 P0 調和表指派的文件勘誤" -m "Co-authored-by: Copilot <223556219+Copilot@users.noreply.github.com>"`

**BLOCKED-OWNER：** 定稿規格的 revision（是否把 `spec-errata.md` 併入規格下一版）由擁有者決定。

---

## Task T16：P0 KEEP 列基線凍結（daemon logging、診斷文字 golden、config quirks）

（執行順序在 T10 之後、T11 之前；編號置末只為不重排既有 Task。）

**Test author model:** haiku-xhigh　**Implementer model:** agy-gemini-3.8-flash-high（特徵化：鎖定現況、填契約檔）

**Files:**
- Create: `contracts/daemon_logging.yaml`、`scripts/stage0/capture_transport_stall_hint.py`、`contracts/golden/last_error/transport_stall_hint.json`、`tests/test_contracts_keep_baseline.py`
- Fragment: `changelog.d/rust-p0-keep-baseline.md`

**Interfaces:**
- Consumes：調和表 GAP-G-01、GAP-G-09、EVT-D-S14、EVT-D-C1／SES-16.6-04、EVT-D-C2、EVT-D-C3；盤點 10 §G-01（16 則訊息表）、09 §config、03 §5.5；T4 `session_last_errors.yaml` 的 `logging`／`detail` 欄。
- Produces：P5 `06-slice21-daemon-core.md` `Effect::Warn`／`DaemonLog`（GAP-G-01 模板）與 P7 `08-slice23-login-events.md` transport stall detail 的位元組基準；config 載入 quirks 的特徵化測試（P5 Rust config loader 的對照）。本 Task 不改產品行為。

（GAP-G-13、RPC-D-10／PLT-O-04、RPC-D-12、RPC-H-02 已在 T8；CLI-11.01、CLI-11.18 與 §9.4 版本列已在 T9。）

**`contracts/daemon_logging.yaml`：**

```yaml
schema_version: 1
revision: 1
sink:
  stream: stderr
  mechanism: "logging.lastResort（daemon 端未呼叫 basicConfig／addHandler／setLevel）"
  min_level: WARNING
  format: "%(message)s"
  info_dropped: true
  supervisor: "systemd 模式由 journald 承接 stderr；on-demand 模式寫到啟動者的 stderr"
messages:
  - id: credentials_unresolved
    logger: serialwrap
    level: WARNING
    template: "<逐字自 session_manager.py:1211 的 format 字串（含相鄰字面串接後的完整字串）>"
    args: [com, reason, env_file_or_dash]
    source: "sw_core/session_manager.py:1211"
    dedup: "credentials_warned"
  # 其餘依盤點 10 §G-01 的訊息表逐則列出（含 INFO 級：level: INFO、標 dropped: true）
```

- [ ] **Step 1：寫測試**（`tests/test_contracts_keep_baseline.py`；L1 白箱，函式內 import `sw_core`；模組層只放 `core_portable`，subprocess 類標 `process_rpc`，其餘 `memory_only`）

| test 名稱 | 準備 | 斷言 |
|---|---|---|
| `test_daemon_logging_all_calls_listed` | AST 掃 `sw_core/**/*.py`（`Path.glob` 遞迴）所有 `<logger>.debug/info/warning/error/exception/critical(<Constant 或相鄰字面串接>, ...)`，`<logger>` 為 `logging.getLogger(...)` 呼叫或由其賦值的名稱；`cli_trace.py` 的 CLI 自建 handler 除外並在 yaml 註明 | 每個 `(source 行, template)` 都在 `messages`；`messages` 每筆都仍對應一個呼叫（無過期項） |
| `test_daemon_has_no_logging_config` | AST 掃 `sw_core/daemon.py`、`sw_core/service.py`、`sw_core/__init__.py`、`serialwrapd.py` | 不含 `basicConfig`、`addHandler`、`setLevel`、`logging.config`；若出現代表基線已變，停下回報 |
| `test_lastresort_format_and_level` | subprocess：`python3 -c "import logging; l=logging.getLogger('serialwrap'); l.info('i'); l.warning('w %s', 1)"` | stderr 恰為 `w 1\n`（INFO 被丟棄、格式為 `%(message)s`） |
| `test_last_error_logging_templates_match` | 讀 T4 `session_last_errors.yaml` 的 `CREDENTIALS_UNRESOLVED`、`TRANSPORT_STALL` 的 `logging` 欄 | 欄內引用的 WARNING 模板與 `daemon_logging.yaml` 對應 `template` 逐字相同 |
| `test_transport_stall_hint_golden` | 讀 `contracts/golden/last_error/transport_stall_hint.json` 的 cases；每個 case 以 `mock.patch("sw_core.transport_stall.resolve_usb_busid", return_value=<case.busid>)` 呼叫 `transport_stall_hint(case.real_path, case.rx_age_s)` | 輸出逐字等於 `case.expected`（GAP-G-09 位元組級凍結） |
| `test_transport_stall_hint_capture_reproducible` | subprocess 重跑 `scripts/stage0/capture_transport_stall_hint.py --out <tmp>` | 與入庫 golden 相同 |
| `test_config_error_split`（EVT-D-S14） | 依盤點 09 config 節列出的「raise」與「靜默」兩類錯誤各取每一種，餵 `sw_core.config` 的載入函式 | raise 類拋出的例外型別與訊息、靜默類回傳值（被忽略的鍵／預設值）逐項等於測試內表格 |
| `test_login_regex_default_double_backslash`（EVT-D-C1、SES-16.6-04） | 讀 `sw_core.config` 中 `login_regex`／`password_regex` 預設值 | 等於原始碼中的字面值（含雙反斜線），且以 `re.search` 對 `"login: "` 的結果等於現況（記錄已知缺陷，不修） |
| `test_templates_last_file_wins`（EVT-D-C2） | `tmp_path` 放兩份 profile YAML，各定義不同 templates | 載入結果只含最後一份（依載入排序）的 templates |
| `test_string_false_is_truthy`（EVT-D-C3） | YAML 某布林欄位寫 `"false"`（字串） | 載入後該欄位被當成 True（現況），並在 assertion 訊息註明 EVT-D-C3 |

golden 檔格式：`{"captured_by":"scripts/stage0/capture_transport_stall_hint.py","source":"sw_core/transport_stall.py:transport_stall_hint","cases":[{"real_path":null,"rx_age_s":45.0,"busid":null,"expected":"..."},{"real_path":"/dev/ttyUSB0","rx_age_s":45.04,"busid":"1-1","expected":"..."},{"real_path":"/dev/ttyUSB0","rx_age_s":44.96,"busid":null,"expected":"..."},{"real_path":"/dev/ttyUSB0","rx_age_s":3.05,"busid":"1-1.4","expected":"..."}]}`（含四捨五入邊界）。

- [ ] **Step 2：確認紅燈** — `python3 -m pytest -q tests/test_contracts_keep_baseline.py`；Expected：讀 `daemon_logging.yaml`／golden 的測試 FAIL（`FileNotFoundError`）；config quirks 與 lastResort 測試為特徵化，應直接 PASS（FAIL 代表盤點描述與程式不符，依 T7 規則記盤點勘誤並以程式為準修正斷言）。
- [ ] **Step 3：commit 測試** — `git commit -m "test(contracts): 新增 P0 KEEP 列基線的特徵化測試"`（含 trailer）。
- [ ] **Step 4：實作** 寫並執行 `scripts/stage0/capture_transport_stall_hint.py` 產生 golden；依 AST 掃描結果與盤點 10 §G-01 建 `contracts/daemon_logging.yaml`（`template` 逐字複製原始碼，不得意譯）。
- [ ] **Step 5：確認 PASS** — 同 Step 2；Expected：全部 PASS。
- [ ] **Step 6：全套與政策** — 同 T1。
- [ ] **Step 7：commit**

`changelog.d/rust-p0-keep-baseline.md`：

```markdown
---
type: change
scope: contracts
---
新增 `contracts/daemon_logging.yaml`（daemon logging 去向、等級、格式與全部訊息模板）與 `contracts/golden/last_error/transport_stall_hint.json`（TRANSPORT_STALL detail 位元組級 golden），並以特徵化測試凍結 config 載入的已知 quirks（錯誤分流、login_regex 預設值、templates 只取最後一檔、字串 "false" 的 truthiness），供 Rust 重構對照。
```

`git add contracts/daemon_logging.yaml contracts/golden/last_error/transport_stall_hint.json scripts/stage0/capture_transport_stall_hint.py tests/test_contracts_keep_baseline.py changelog.d/rust-p0-keep-baseline.md`；`git commit -m "feat(contracts): 凍結 daemon logging 與 P0 KEEP 列基線" -m "Co-authored-by: Copilot <223556219+Copilot@users.noreply.github.com>"`

**BLOCKED-OWNER：** systemd 模式下 journald 實際承接 stderr 的觀察（需真正運作中的 systemd user service；`supervisor_runtime`）。

---

## 下游計畫須同步的修訂（本 Phase 定案；各計畫作者據此修訂，P0 不改他檔）

| 對象 | 需修訂內容 | 依據 |
|---|---|---|
| `02-stage0-python-bc.md` L28、L2613（DoD） | 「填 `contract_revision`（`git rev-parse HEAD:contracts`）」改為「依狀態推進協定翻 status；`contract_revision` 只記於 release manifest／證據 JSON，不寫入 `contracts/`」 | Global Constraints「revision 命名」 |
| `02-stage0-python-bc.md` 各 BC／SC／DF 實作 Task | 每個 Task 加一步：同一 commit 內翻 `behavior_changes.yaml` 與相關碼的 status、把 `rpc_schema.json`／`cli_schema.json` 對應 `x-pending` 併入正式欄位並刪除（SC-04 `drain_stalled`／`drain_timeout_epoch`、SC-11 `daemon.handoff_stop`（T08）、BC-04 `holder_unknown`、SC-19 `test_device_allowlist`、SC-20 `daemon restart`、SC-21、SC-22、SC-23） | C16、C17 |
| `02-stage0-python-bc.md` DoD | 加入 SC-19 與 `TEST_DEVICE_ALLOWLIST_REJECTED` 的翻轉；SC-19 輸出路徑須可被抽取（T3） | C03／C04／C16 |
| `02-stage0-python-bc.md` SC-19 Task（L1847、L1901） | `health.status.test_device_allowlist` 形狀依調和表 GAP-G-07 為 `{"enabled":true,"coms":[...]}`，移除 `source`；若要保留 `source`，先修調和表與 `rpc_schema.json` 並遞增 revision | T8 `x-pending` SC-19 |
| `02-stage0-python-bc.md` T18（L2398） | SC-25 scope 值改用 `python_when_manifest_present`（底線，`CHANGE_SCOPES` 之一）；`contracts/release_manifest.schema.json` 由 T18 建立（P0 不建立） | T1 `CHANGE_SCOPES`、檔案總覽 |
| `02-stage0-python-bc.md` T01（L89、L156-171） | 刪除 `pytest_configure` marker 註冊，只保留 `pytest_collection_modifyitems` auto-skip；新測試檔依 T14 規則標註 | T14 |
| `02-stage0-python-bc.md` L652、L809 | `contracts/wal_meta_events.schema.json` 改為「修改」（P0 T10 已建立），不得另建第二份 META schema | T10 |
| `03-stage0-test-infra.md` Task 13（L1989-2050） | 移除 `tests/platform_resources.yaml` 與 `modifyitems` 套 marker／未列檔 UsageError，只保留 `SW_RESOURCE_REPORT` 與 `core_portable_platform_skips` 閘門 | T14 |
| `03-stage0-test-infra.md` L21 | `release_manifest.schema.json` 來源改為 P1 T18 | 檔案總覽 |
| `03-stage0-test-infra.md` `cases.yaml` | 新增測試：`behavior_changes.yaml` 全部 `case_ids` ⊆ `cases.yaml` 的 `case_id`；`invariants` 欄的值 ⊆ `SAFETY_INVARIANTS` | T5 |
| `10-stage3-canary.md` L44 | `release_manifest.schema.json` 來源改為 P1 T18（P2 沿用） | 檔案總覽 |
| `spec-reconciliation.md` TST-D-4 | 階段欄改為「P0（註冊與標註）／P2（報告）」 | T14 |
| `spec-reconciliation.md` SC-25 | 由 P1 T18 依其計畫同步範圍欄 | T5 |
| Stage 0 release PR | 以 `release:0.4.0` label bump `VERSION` 至 0.4.0（feature PR 不 bump，避免 R-07） | T5 `applies_to.python` |

---

## Phase 出口條件

1. `python3 -m tools.contracts check` exit 0（`tests/test_contracts_validate.py::test_real_repo_contract_check_passes` PASS）。
2. `python3 -m pytest -q tests/` 全綠；passed／skipped 數與 T14 標註前一致（加上本 Phase 新增測試）。
3. `python3 -m policy_check --repo .` 無 FAIL；R-26 的 4 個 generated-fact 同步。
4. paulsha-conventions worktree：`python3 -m pytest -q` 全綠；本地 R-27 對 serialwrap 判定 PASS；分支未 push。
5. `contracts/` 下 7 個規格指定檔＋`code_extraction.yaml`、`diagnostic_aux.yaml`、`daemon_logging.yaml`、`wal_meta_events.schema.json`、`p0_row_coverage.yaml`，以及 `contracts/golden/rpc/line_limit.json`、`contracts/golden/last_error/transport_stall_hint.json` 均已入庫；summary 的所有計數由資料產生；`contracts/` 內沒有任何 `contract_revision` 鍵。
6. Windows 原生 `-m core_portable` 執行結果已記錄（兩平台 collected／selected／passed 數與 `manifest.sha256`；PASS，或明列移交 P2 的載入錯誤；不得標 PASS 未執行項；任何重新分類都有根因註解、獨立審閱與前後計數）。
7. 雙審（Sonnet max＋Codex gpt-6.1-sol medium）完成，且修改契約／比較器的 commit 有獨立審閱紀錄（§10.4）；T2 mutation 測試輸出已附於審查 B 紀錄。
8. `tests/test_contracts_doc_errata.py::test_p0_rows_all_covered` PASS（調和表每個 P0 列都指到實際凍結位置）。
9. 「下游計畫須同步的修訂」表已交給各計畫作者（本 Phase 不改他檔；交付訊息列出尚未修訂者）。

## BLOCKED-OWNER 彙總（本環境無法產出，交付時不得標 PASS）

| # | 項目 | 來源 Task | 需要的擁有者動作 |
|---|---|---|---|
| 1 | 擁有者確認清單 #1～#7（RPC-D-02 passthrough、SES-16.4-03、SC-08 UDS 權限、SC-04 drain 逾時回應、DF-02 human source、SC-26 Windows event handler、SC-27 Windows connect／benches） | T3、T5 | 逐項確認或改裁；確認後改 `confirmed` 並遞增 revision |
| 2 | paulsha-conventions R-27 分支 push、PR、merge、release | T13 | 授權推送與發版 |
| 3 | serialwrap 引擎 pin 升級至含 R-27 的版本、`CLAUDE.md` policy_version 同步、serialwrap PR merge | T12、T13 | 授權推送、merge |
| 4 | §9.4 L2／L3 依賴欄位在真實 bench daemon 的實值對照 | T8 | 在 bench 上跑 `serialwrap daemon status`、`session list` 並比對 `rpc_schema.json` |
| 5 | `windows_com` 原生 COM handle 證據（OD-08） | T14 | 自架 Windows runner＋com0com DUT-side pair 或實體 USB-UART 直連 |
| 6 | `physical_uart`、`supervisor_runtime`（含 `windows-task` >72 h）證據 | T14 | bench 排程（OD-10） |
| 7 | 定稿規格是否併入 `spec-errata.md` | T15 | 決定規格下一版 revision |
| 8 | systemd 模式 journald 承接 daemon stderr 的實機觀察 | T16 | 在運作中的 systemd user service 上確認 WARNING 出現在 journal |
