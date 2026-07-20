# serialwrap-reliability testpilot plugin 殼（Phase 2）Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 建立 `reliability/` dev-only editable dist（`serialwrap-reliability`），把 #122 realhw 引擎接上 testpilot-core：entry point 註冊、`PluginBase` 生命週期薄轉接（prepare_run＝realhw preflight gate、execute_step＝black-box `case.run(ctx)`、evaluate＝分類抄寫 `_last_failure`）、testbed.yaml 與 config.json 雙來源等價 loader、md/json reporter 重用 realhw 報告。對應 OpenSpec `serialwrap-reliability-plugin` tasks **群組 5-6**。

**Architecture:** 兩個入口、一個引擎——realhw（standalone `python3 -m realhw`）維持不動；plugin 是第二個前端。`plugin.py` 是唯一 import testpilot 的檔案；核心邏輯（case dict 映射、`_last_failure` 抄寫、cfg 合成、longrun 步進、恢復/清殘）全在**不 import testpilot** 的 `core.py`／`testbed_loader.py`／`reporter.py`——serialwrap CI 不裝 testpilot 也能單測。editable install 下 `__file__` 在 repo 內，`parents[2]`＝repo root，插 `sys.path` 後 `import realhw` 零打包技巧。權威設計＝`docs/superpowers/specs/2026-07-20-serialwrap-reliability-testpilot-plugin-design.md`；OpenSpec delta＝`openspec/changes/serialwrap-reliability-plugin/specs/reliability-testpilot-plugin/spec.md`。

**Tech Stack:** Python 3.10+（typing／dataclasses／threading／subprocess）、PyYAML（testbed loader；serialwrap 執行期既有依賴）、hatchling（dist build backend）、testpilot-core 0.3.4（僅 bench venv；CI 不裝）。

**執行環境注意：**
- 工作區：worktree `.worktrees/reliability-plugin`（分支 `feature/serialwrap-reliability-plugin`）。開工前 `git branch --show-current` 確認。
- **本 plan 以「Phase 1 已完成」為前提**（同 change 的 tasks 群組 1-4：`CaseResult` 增 `category`/`reason_code`、preflight 增 benchlock/capabilities/windows_daemon、remote 族 7 case）。Task 0 有硬檢核；缺項就 **STOP**，先 rebase Phase 1 分支，不要繞過。
- Tasks 1-8 為純邏輯＋單測，直接在本 worktree 跑 `python3 -m pytest -q tests/test_reliability_*.py`。Tasks 9-13 標【真機-人工閘】：操作 live daemon、真板與 `~/prj_arc/testpilot` venv，需人在場確認每步輸出。
- **R-21 陷阱**：任何進 repo 的檔案不得含 `/home/<user>/` 絕對路徑字面值——一律 `~` 或 `Path.home()`（`/mnt/c/...` 不受限）。
- Commit 一律 Conventional Commits 繁中＋雙 trailer：
  ```
  Co-authored-by: Copilot <223556219+Copilot@users.noreply.github.com>
  Co-Authored-By: Claude Fable 5 <noreply@anthropic.com>
  ```

## Global Constraints

1. **語言**：所有檔案內容（註解、docstring、報表字串）、commit message、PR 內容一律繁體中文。
2. **pytest 必綠**：每個 Task 結束 `python3 -m pytest -q tests/test_reliability_*.py` 全過；Task 8 跑全量 `python3 -m pytest -q tests/`，不得引入**新的**失敗（既有 flaky：`test_multiagent_e2e.py::test_five_agents_three_rounds_no_conflict`、`t8_full_run_simulation`、`test_t1_wal_reset_preserves_console`）。
3. **policy_check**：pinned SHA `ee87a6d5ed91209d944934a2559f4f2622fd1ac2`；本地跑 `python3 -m policy_check --repo .`，開 PR 前另帶 `--pr-title/--pr-body/--pr-base-ref/--pr-head-ref` 複現 CI。
4. **禁止直接 commit 到 `main`**；全程停留在 `feature/serialwrap-reliability-plugin`。
5. **release wheel 零改動**：主 `pyproject.toml` 一個字不動；Task 1 與 Task 8 各驗一次 wheel 內容（僅 `sw_core/**`＋dist-info，無 `realhw`/`reliability`/`serialwrap_reliability`）。
6. **reliability dist 永不 release**：不上傳任何 index、不進 CI matrix、唯一支援 editable install；`reliability/pyproject.toml` version 固定 `0.1.0`。
7. **testpilot import 邊界**：`core.py`/`testbed_loader.py`/`reporter.py`/所有 `tests/test_reliability_*.py` **禁止 import testpilot**（單測釘死）；只有 `plugin.py` 可以。
8. **不用 testpilot transport 表達 case 動作**：tmux/usbipd/systemd/CLI 全由 realhw drivers 自理（Thin Adapter，spec MUST NOT）。
9. 單測不得碰 live daemon/真板：realhw I/O 面（SwCli/preflight collect）一律 monkeypatch 或 fake。

## 契約事實（testpilot-core@0.3.4 實地考證——本 plan 的程式碼依此為準）

以下每條都直接讀過原始碼；worker 實作時**不要**憑印象改動這些接法。

| # | 事實 | 出處 |
|---|---|---|
| C1 | `API_VERSION = "1.1"`；loader `_check_api_compat`：plugin major 必須相等、core minor ≥ plugin minor；`api_version` 未宣告直接 `IncompatiblePluginError`。plugin class 以**無參數**建構。 | `testpilot/api/__init__.py` L58；`core/plugin_loader.py` |
| C2 | 必要 abstract：`name`（property）、`discover_cases() -> list[dict]`、`execute_step(case, step, topology) -> dict`（keys：`success`/`output`/`captured`/`timing`）、`evaluate(case, results) -> bool`。`setup_env`/`verify_env`/`teardown`/`execution_policy`/`verify_install`/`create_reporter`/`report_formats`/`prepare_run` 皆可覆寫。 | `core/plugin_base.py` |
| C3 | 引擎每 attempt：`setup_env` → `verify_env` → 逐 step `execute_step` → **step `success=False` 即 break、`evaluate` 不會執行**（comment=`step failed: <id>`）→ 全 step 成功才 `evaluate` → finally `teardown`。**因此本 plugin 的 execute_step 一律回 `success=True`，判決權集中在 `evaluate`**（含 FAIL/SKIP）。 | `core/execution_engine.py` L174-241 |
| C4 | `evaluate` 收到的 `case` 是 `runtime_case = dict(case)`（淺拷貝）；在其上設 `case["_last_failure"]` 會被 on_failure hook 讀到（payload 帶同一個 runtime_case），但**不會**寫回 `prepare_run` 回傳的原 case dict——reporter 不能靠 case dict 拿結果，要靠 plugin 實例累積。 | `core/execution_engine.py` L147、L227-241；`core/remediation.py` L361-370 |
| C5 | 分類分桶在 core：`_classify_diagnostic_status` 讀 `failure_snapshot.category`——`environment/session`→FailEnv、`configuration/config`→FailConfig、`test/semantic`→FailTest、其餘（含空）→Inconclusive；verdict True→Pass（有 remediation 史→PassAfterRemediation）。 | `core/execution_engine.py` L82-99 |
| C6 | **failure_snapshot 只由 `RuntimeRemediationCoordinator.handle_on_failure` 產生，且該 handler 在 `remediation.enabled` 為 false 時直接 return、什麼都不寫**（L358-359）。snapshot 來源＝`case["_last_failure"]` 經 `_coerce_failure_snapshot`：`category`/`reason_code`/`comment` 直抄、`evidence` 必須是 **list[str]**、category 空值 coerce 成 `"inconclusive"`。 | `core/remediation.py` L155-171、L243-268、L357-373 |
| C7 | ⚠️ **spec 矛盾（已裁決）**：openspec spec 要求 remediation `enabled: false`，但依 C6 這會讓所有 FAIL 都變 Inconclusive，「分類抄寫落桶」Scenario 永不可能過。**裁決：`agent-config.yaml` 設 `remediation.enabled: true`＋防線鎖死動作（執行點移除＋decision 恆 None——後者為唯一真正生效的阻擋點，見 C10）**（詳見 Task 6 說明與 agent-config 註解）；openspec 各檔同款措辭已由主 session 同步修訂。 | 見 Task 6 |
| C8 | hooks 有總閘：`HookDispatcher.dispatch` 先看 `hooks.enabled_hooks`，不在清單裡的 hook 一律 no-op。**`on_failure`/`pre_case`/`post_case` 必須列入**（on_failure＝snapshot 擷取；pre_case＝coordinator 狀態重置；post_case＝snapshot 帶回 RetryResult）。`on_retry` 刻意**不**列（防線之一）。 | `core/hook_policy.py` L82、L119 |
| C9 | retry 預設值陷阱：agent-config 缺 `execution.retry.max_attempts` 時 `build_execution_policy` 預設 **2**。必須顯式寫 `max_attempts: 1`。`on_retry` hook 只在 attempt_index>1 才 dispatch → max_attempts=1 保證 remediation 動作永無執行點。 | `core/runner_selector.py` L83；`core/execution_engine.py` L302-316 |
| C10 | remediation 動作的**唯一真正生效阻擋點＝decision 恆 None**：plugin 不覆寫 `request_remediation_decision`/`build_remediation_decision`（PluginBase 預設回 None）→ `_validate_decision` 首行 `decision is None` 直接 return。⚠️ 注意：`_validate_decision` 的白名單檢查是 `if self.allowed_actions and ...`（L483）——**空集合 falsy 被短路、不攔截**；`allowed_actions: []` 不具攔截效果、不可依賴（僅宣示性組態）。 | `core/plugin_base.py` L97-125；`core/remediation.py` L424-493 |
| C11 | `agent-config.yaml` 位置＝`plugin.plugin_root / "agent-config.yaml"`（`plugin_root`＝plugin.py 所在目錄）。 | `core/runner_selector.py` L49-51 |
| C12 | testbed staging：CLI 每次解析 plugin 都把 `<plugin_root>/testbed.yaml.example` **原樣覆蓋**到 `<root>/configs/testbed.yaml`（缺 example 直接 FileNotFoundError）。⇒ **bench 事實的可編輯正本＝example 檔本身**（editable 佈局在 repo 內）；改 staged 副本會在下次 run 被蓋掉。 | `core/testbed_bootstrap.py` |
| C13 | `topology` 參數＝`Orchestrator.config`＝`TestbedConfig`（`.raw` 全 YAML dict、`.devices`、`.get_device(role)`、`.variables`）。 | `core/orchestrator.py` L83-86；`core/testbed_config.py` |
| C14 | run 順序：`plugin.prepare_run(case_ids)`（**在此 raise 即 fail-fast，任何 case 之前**）→ `_start_run_capture`（預設 SerialwrapBackend：對 live daemon 做 **`wal reset`**；daemon 已在跑則不另起、`teardown_run` 為 no-op 不停 daemon）→ 逐 case `execute_with_retry` → 每 case 寫 `agent_trace/<sanitized_id>.json`（含 `diagnostic_status`、`failure_snapshot`）→ `reporter = plugin.create_reporter()`；**reporter 必須有 `build_reports(run_result)`，回 None 會 RuntimeError**。 | `core/run_loop.py` L208-410；`runtime/serialwrap_backend.py` L62-71、L158-160 |
| C15 | `PreparedRun(cases: list[dict], artifacts: dict)`；`prepared.artifacts` 原樣進 `RunResult.artifacts` → reporter 可讀。`RunResult` 欄位：`cases`（`CaseRunRecord`：`.case`/`.retry: RetryResult`/`.case_id`/…）、`run_id`/`run_date`/`plugin_name`/`fw_ver`/`fw_ver_source`/`artifact_dir`/`agent_trace_dir`/`execution_policy`/`artifacts`/`version_manifest`。`RetryResult.diagnostic_status`/`failure_snapshot`。 | `core/prepared_run.py`；`core/run_loop.py` L42-64、L216-218、L392 |
| C16 | 報告身分 hook：run_loop 呼叫（若存在）`plugin.capture_dut_firmware_version(config, cases)`；回 `{"git": <str>}` 時、CLI 未給 `--dut-fw-ver` 就以它當 `fw_ver`（source=`dut_git_revision`）。 | `core/run_loop.py` L67-104 |
| C17 | case dict 最低形狀（`schema/case_schema.py` 的 validate_case 詞彙，我們自產 dict 也照此形）：`id`/`name`/`topology{devices:非空 mapping}`/`steps: 非空 list（每步 id/action/target）`/`pass_criteria: 非空 list`。`--case` 選擇（PluginBase 預設 prepare_run）以 id/aliases 比對；本 plugin 覆寫 prepare_run、用精確 id 比對（case 無 aliases，語意等價）。 | `schema/case_schema.py` L15-21；`core/case_utils.py` L82-101 |
| C18 | timeout 是軟數字：`attempt_timeout_seconds` 只進 step payload 與 trace，core 不 kill。longrun 不需要為 timeout 拆步，拆步是進度/trace 結構。 | `core/execution_engine.py` L45-63、L184-186 |
| C19 | 報表落點：`<root>/plugins/<plugin_name>/reports/<run_id>/`（root 預設＝testpilot-core checkout；`agent_trace/` 在其下）。entry-point plugin 不必真的住在 `plugins/` 目錄。 | `core/run_loop.py` L220、L240-252 |
| C20 | `execution_policy()` 只有 `mode`/`max_concurrency` 會被 run-level 採用（與 agent-config 不一致時強制覆寫並 warning）。 | `core/run_loop.py` L107-138 |

## Interfaces（每任務 Consumes/Produces 的權威簽章表）

**Phase 1 交付（本 plan 消費，Task 0 檢核）：**

```python
# realhw/harness.py（Phase 1 後）
@dataclasses.dataclass
class CaseResult:
    verdict: str                       # PASS | FAIL | SKIP
    reason: str = ""
    category: str = ""                 # "" | environment | session | configuration | test
    reason_code: str = ""              # 自由字串（進 trace）
    evidence: dict[str, str] = field(default_factory=dict)
    duration_s: float = 0.0

@dataclasses.dataclass(frozen=True)
class Case:
    id: str; tier: str; title: str
    run: Callable[[Any], CaseResult]
    destructive: bool = False
    requires: tuple[str, ...] = ()
    hints: tuple[str, ...] = ()

REGISTRY: list[Case]                                   # import realhw.cases 後填滿（29+7 條）
def recovery_command(state: str | None) -> tuple[str, ...]
def write_reports(report_dir: Path, meta: dict, results: list[tuple[str, CaseResult]],
                  hints: dict[str, tuple[str, ...]]) -> None
def parse_duration(text: str) -> int                   # "15m" -> 900

# realhw/preflight.py（Phase 1 後；collect/evaluate 簽章不變，Checks 增欄含 benchlock/windows_daemon）
def collect(cfg: dict, sw, repo_root) -> Checks        # I/O 收集（含 benchlock flock 嘗試，句柄由模組持有）
def evaluate(c: Checks) -> tuple[bool, list[str]]      # suite-refuse 判定（純函式）
def capabilities(cfg: dict, sw) -> dict[str, bool]     # family-gate：鍵對齊 Case.requires 詞彙
                                                       # （"docker"/"remote_capability"/"two_boards"/"tmux"…）

# realhw/drivers.py（既有，不變）
class SwCli:  def run(*args, timeout=30.0) -> dict;  def sessions() -> list[dict]
              def session(com) -> dict;  def submit_and_wait(...) -> dict
              def wait_state(com, want, *, timeout_s, poll_s=2.0) -> bool
class TmuxCtl(prefix); class Usbipd(exe); class Systemd
```

> 若 Phase 1 實際落地的 `capabilities` 名稱/簽章不同，唯一調整點是 `core.run_preflight()`（已用 `getattr` 防禦式消費）；其餘任務零波及。

**testpilot-core 消費面（bench venv 才存在）：** `testpilot.api.PluginBase`、`testpilot.api.PreparedRun`——僅 `plugin.py` 引用。

## File Structure

| 檔案 | 職責 |
|---|---|
| `reliability/pyproject.toml` | dev-only dist 宣告（name/version/deps/entry point；hatchling） |
| `reliability/serialwrap_reliability/__init__.py` | package 標記＋`__version__` |
| `reliability/serialwrap_reliability/core.py` | 不 import testpilot 的核心：repo-root 定位、registry→case dicts、longrun steps 合成、`_last_failure` 抄寫、runtime skip、black-box 執行、恢復/清殘、`LongrunRunner`、`run_preflight` |
| `reliability/serialwrap_reliability/testbed_loader.py` | testbed.yaml（dict）與 config.json → 同形 realhw cfg |
| `reliability/serialwrap_reliability/plugin.py` | PluginBase glue（唯一 testpilot 接觸面） |
| `reliability/serialwrap_reliability/reporter.py` | `build_reports(run_result)`——重用 realhw `write_reports`＋身分烙印 |
| `reliability/serialwrap_reliability/agent-config.yaml` | sequential、max_attempts=1、hooks 最小集、remediation 鎖死（C7） |
| `reliability/serialwrap_reliability/testbed.yaml.example` | bench 事實正本（staging 每 run 覆蓋 configs/testbed.yaml） |
| `tests/test_reliability_core.py` | core.py 單測（不 import testpilot） |
| `tests/test_reliability_testbed.py` | 雙來源等價單測 |
| `tests/test_reliability_pluginfiles.py` | agent-config/testbed example/pyproject/plugin.py 的契約檔單測（純文字/YAML 檢核） |
| `tests/test_reliability_reporter.py` | reporter 單測（stub RunResult） |
| `changelog.d/reliability-testpilot-plugin.md` | R-09 fragment |
| `docs/func-test/realhw-stability-checklist.md` | 增「plugin 入口」節（R-18） |

---

### Task 0: 前置檢核（Phase 1 就位；不改碼）

**Interfaces** — Consumes：Phase 1 全部產物；Produces：無（gate）。

- [ ] **Step 1: 分支與 Phase 1 檢核**

```bash
cd ~/prj_pri/serialwrap/.worktrees/reliability-plugin
git branch --show-current    # 期望：feature/serialwrap-reliability-plugin
python3 - <<'EOF'
import dataclasses, sys
sys.path.insert(0, ".")
from realhw import harness, preflight
fields = {f.name for f in dataclasses.fields(harness.CaseResult)}
assert {"category", "reason_code"} <= fields, f"Phase 1 未就位：CaseResult 缺分類欄 {fields}"
if not callable(getattr(preflight, "capabilities", None)):
    print("[warn] preflight.capabilities 缺席——family-gate 將 degrade（core.run_preflight 回空 capabilities，僅影響 requires→SKIP，suite-refuse 不受影響）")
import realhw.cases  # noqa
print(f"REGISTRY={len(harness.REGISTRY)} cases；CaseResult fields OK")
EOF
# 期望：REGISTRY=36 cases（Phase 1 remote 族已入；仍為 29 表示 Phase 1 remote 未 merge——STOP、先 rebase）
```

- [ ] **Step 2: pytest 基線**

```bash
python3 -m pytest -q tests/ -x --ignore=tests/test_multiagent_e2e.py --ignore=tests/test_multiagent_stress.py -q | tail -3
# 期望：全過（既有 flaky 檔已排除）；記下通過數當基線
```

---

### Task 1: dist 骨架＋release wheel 零改動驗證（openspec 5.1）

**Interfaces** — Consumes：主 `pyproject.toml`（唯讀）；Produces：`reliability/pyproject.toml`、`reliability/serialwrap_reliability/__init__.py`、dist 名 `serialwrap-reliability` 0.1.0、entry point `testpilot.plugins:serialwrap_reliability`。

- [ ] **Step 1: 建 wheel 基線（改碼前）**

```bash
cd ~/prj_pri/serialwrap/.worktrees/reliability-plugin
WHEELDIR=$(mktemp -d)
python3 -m pip wheel . --no-deps -w "$WHEELDIR" -q
unzip -Z1 "$WHEELDIR"/serialwrap-*.whl | sort > "$WHEELDIR/listing-before.txt"
wc -l "$WHEELDIR/listing-before.txt"   # 記下行數；echo $WHEELDIR 記下路徑供 Step 4 與 Task 8 用
```

- [ ] **Step 2: 建立 `reliability/pyproject.toml`**

```toml
# reliability/pyproject.toml — dev-only dist（永不 release、永不上傳 index；唯一支援 editable install）。
# build backend 選 hatchling：對齊 lab plugin 生態（wifi_llapi 與 testpilot-core 同為 hatchling），
# editable 走 _editable_impl .pth——package 的 __file__ 留在 repo 內，core.py 的 repo-root 定位依賴此性質。
[build-system]
requires = ["hatchling"]
build-backend = "hatchling.build"

[project]
name = "serialwrap-reliability"
version = "0.1.0"
description = "serialwrap 實機穩定性套件（realhw）的 testpilot plugin 殼（dev-only editable）"
requires-python = ">=3.10"
dependencies = [
    "testpilot-core>=0.3.4,<1.0",
]

[project.entry-points."testpilot.plugins"]
serialwrap_reliability = "serialwrap_reliability.plugin:Plugin"

[tool.hatch.build.targets.wheel]
packages = ["serialwrap_reliability"]
```

- [ ] **Step 3: 建立 `reliability/serialwrap_reliability/__init__.py`**

```python
"""serialwrap-reliability——realhw 引擎的 testpilot plugin 殼（dev-only editable dist）。

注意：本 __init__ 不得 import plugin/testpilot——serialwrap CI（未裝 testpilot）
要能 import 本 package 的 core/testbed_loader/reporter。
"""
from __future__ import annotations

__version__ = "0.1.0"
```

- [ ] **Step 4: 驗 release wheel 零改動**

```bash
python3 -m pip wheel . --no-deps -w "$WHEELDIR" -q --exists-action w
unzip -Z1 "$WHEELDIR"/serialwrap-*.whl | sort > "$WHEELDIR/listing-after.txt"
diff "$WHEELDIR/listing-before.txt" "$WHEELDIR/listing-after.txt" && echo "WHEEL-UNCHANGED"
# 期望：無 diff、印 WHEEL-UNCHANGED
unzip -Z1 "$WHEELDIR"/serialwrap-*.whl | grep -E 'realhw|reliability' ; echo "grep rc=$?"
# 期望：無輸出、grep rc=1（wheel 內不含 realhw / serialwrap_reliability / reliability）
```

- [ ] **Step 5: commit**

```bash
git add reliability/
git commit -m "feat(reliability): 新增 serialwrap-reliability dev-only dist 骨架（entry point＋hatchling）"
```

---

### Task 2: core.py——repo-root 定位與 realhw bootstrap（openspec 5.2 前半，TDD）

**Interfaces** — Consumes：realhw package 佈局（repo root 直下）；Produces：`core.REPO_ROOT: Path`、`core.ensure_realhw_importable() -> Path`、`core.load_registry() -> list[Case]`。

- [ ] **Step 1: RED 測試——建立 `tests/test_reliability_core.py`**

```python
"""Phase 2 plugin core（serialwrap_reliability.core）純邏輯單測——不 import testpilot、不碰 live。"""
from __future__ import annotations

import re
import sys
import time
from pathlib import Path
from types import SimpleNamespace

import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT / "reliability"))

from serialwrap_reliability import core  # noqa: E402


def test_repo_root_locates_worktree():
    # editable 佈局：reliability/serialwrap_reliability/core.py 的 parents[2]＝repo root
    assert core.REPO_ROOT == REPO_ROOT
    assert (core.REPO_ROOT / "realhw" / "harness.py").is_file()


def test_ensure_realhw_importable_idempotent():
    got = core.ensure_realhw_importable()
    assert got == REPO_ROOT
    import realhw  # noqa: F401  # bootstrap 後可 import
    before = list(sys.path)
    core.ensure_realhw_importable()  # 第二次不得重複插入
    assert sys.path.count(str(REPO_ROOT)) == before.count(str(REPO_ROOT))


def test_load_registry_populated_unique():
    reg = core.load_registry()
    ids = [c.id for c in reg]
    assert len(ids) == len(set(ids))
    assert len(ids) >= 29  # #122 既有 29；Phase 1 remote 族後為 36
    assert any(c.id == "p0-doctor" for c in reg)


def test_core_modules_do_not_import_testpilot():
    pkg = REPO_ROOT / "reliability" / "serialwrap_reliability"
    for name in ("__init__.py", "core.py"):
        text = (pkg / name).read_text(encoding="utf-8")
        bad = [ln for ln in text.splitlines() if re.match(r"\s*(import|from)\s+testpilot", ln)]
        assert not bad, f"{name} 不得 import testpilot：{bad}"
```

- [ ] **Step 2: 跑 RED**

```bash
python3 -m pytest -q tests/test_reliability_core.py
# 期望：ImportError/ModuleNotFoundError（serialwrap_reliability.core 尚不存在）→ 失敗
```

- [ ] **Step 3: GREEN——建立 `reliability/serialwrap_reliability/core.py`**

```python
"""serialwrap_reliability 核心邏輯——不 import testpilot（serialwrap CI 直接單測）。

分層原則（openspec Requirement「生命週期映射（Thin Adapter）」）：
- plugin.py 只做 PluginBase glue；一切可純測邏輯集中本檔與 testbed_loader.py。
- 對 realhw 的依賴走「repo root 插 sys.path → import realhw」——editable 佈局下
  parents[2] 即 repo root，realhw 不需被打包（spec MUST NOT 要求 realhw 打包）。
"""
from __future__ import annotations

import re
import subprocess
import sys
import threading
import time
from pathlib import Path
from typing import Any, Callable

#: editable 佈局：reliability/serialwrap_reliability/core.py → parents[2]＝repo root
REPO_ROOT: Path = Path(__file__).resolve().parents[2]


def ensure_realhw_importable() -> Path:
    """把 repo root 插入 sys.path（冪等），使 ``import realhw`` 可用；回傳 repo root。"""
    root = str(REPO_ROOT)
    if root not in sys.path:
        sys.path.insert(0, root)
    return REPO_ROOT


def load_registry() -> list[Any]:
    """import realhw.cases 觸發 case 註冊後，回傳 REGISTRY 淺拷貝。"""
    ensure_realhw_importable()
    import realhw.cases  # noqa: F401  # import 副作用：填 REGISTRY

    from realhw import harness

    return list(harness.REGISTRY)
```

- [ ] **Step 4: 跑 GREEN＋commit**

```bash
python3 -m pytest -q tests/test_reliability_core.py
# 期望：4 passed
git add reliability/serialwrap_reliability/core.py tests/test_reliability_core.py
git commit -m "feat(reliability): core.py repo-root 定位與 realhw bootstrap（TDD）"
```

---

### Task 3: core.py——case dict 映射／longrun steps 合成／選擇期過濾（openspec 5.2 中段，TDD）

**Interfaces** — Consumes：`harness.Case`（id/tier/title/destructive/requires/hints）、cfg dict（`boards`/`longrun.snapshot_interval_s`/`duration_s`）；Produces：`synth_longrun_steps(duration_s, interval_s) -> list[dict]`、`case_to_dict(case, cfg) -> dict`（形狀對齊 C17）、`build_case_dicts(registry, cfg) -> list[dict]`、`filter_for_run(cases, requested_ids) -> list[dict]`。

- [ ] **Step 1: RED——append 到 `tests/test_reliability_core.py`**

```python
# ---------------------------------------------------------------- Task 3：case dict 映射
CFG = {
    "boards": [
        {"com": "COM0", "alias": "dut-prpl", "serial": "S0", "busid": "8-1", "platform": "prpl"},
        {"com": "COM1", "alias": "sta-prpl", "serial": "S1", "busid": "8-2", "platform": "brcm"},
    ],
    "tmux_prefix": "realhw",
    "usbipd_exe": "/mnt/c/x/usbipd.exe",
    "timeouts": {"ready_wait_s": 180, "reboot_wait_s": 300, "human_active_window_s": 60},
    "longrun": {"snapshot_interval_s": 300, "agent_workers": 4},
    "duration_s": 900,
}


def _mk_case(id: str, tier: str = "p0", destructive: bool = False,
             requires: tuple = (), hints: tuple = ()):
    core.ensure_realhw_importable()
    from realhw import harness
    return harness.Case(id=id, tier=tier, title=f"title-{id}",
                        run=lambda ctx: harness.CaseResult("PASS"),
                        destructive=destructive, requires=tuple(requires), hints=tuple(hints))


@pytest.mark.parametrize("duration_s,interval_s,n", [(900, 300, 3), (60, 300, 1), (0, 300, 1)])
def test_synth_longrun_steps_count(duration_s, interval_s, n):
    steps = core.synth_longrun_steps(duration_s, interval_s)
    assert len(steps) == n  # 最少 1（openspec：duration/interval → N checkpoints，最少 1）
    assert steps[0]["id"] == "checkpoint-001"
    for s in steps:
        assert {"id", "action", "target"} <= set(s)  # C17：step 必要鍵
        assert s["action"] == "longrun_checkpoint"


def test_case_to_dict_single_step_schema():
    d = core.case_to_dict(_mk_case("p0-x", requires=("two_boards",), hints=("h1",)), CFG)
    assert {"id", "name", "topology", "steps", "pass_criteria"} <= set(d)  # C17：頂層必要鍵
    assert set(d["topology"]["devices"]) == {"COM0", "COM1"}
    assert d["steps"] == [{"id": "exec", "action": "run_case", "target": "bench"}]
    assert d["pass_criteria"] == ["realhw_case_verdict"]  # 佔位：真判決在 evaluate()
    assert d["metadata"] == {"tier": "p0", "destructive": False,
                             "requires": ["two_boards"], "hints": ["h1"]}


def test_case_to_dict_longrun_synthesizes_checkpoints():
    d = core.case_to_dict(_mk_case("lr-mixed", tier="longrun"), CFG)
    assert len(d["steps"]) == 3  # 900s // 300s
    assert [s["id"] for s in d["steps"]] == ["checkpoint-001", "checkpoint-002", "checkpoint-003"]


def test_filter_for_run_default_excludes_destructive():
    cases = core.build_case_dicts(
        [_mk_case("a"), _mk_case("b", destructive=True), _mk_case("c")], CFG)
    got = core.filter_for_run(cases, set())
    assert [c["id"] for c in got] == ["a", "c"]  # 選擇期排除：不進 run、不進報表


def test_filter_for_run_explicit_id_includes_destructive():
    cases = core.build_case_dicts([_mk_case("a"), _mk_case("b", destructive=True)], CFG)
    got = core.filter_for_run(cases, {"b"})
    assert [c["id"] for c in got] == ["b"]  # 顯式點名才納入
```

- [ ] **Step 2: 跑 RED**

```bash
python3 -m pytest -q tests/test_reliability_core.py
# 期望：新測試以 AttributeError（core 無 synth_longrun_steps 等）失敗
```

- [ ] **Step 3: GREEN——append 到 `core.py`**

```python
# ---------------------------------------------------------------- case dict 映射（openspec 5.2）
def synth_longrun_steps(duration_s: int, interval_s: int) -> list[dict[str, Any]]:
    """duration/interval → N 個 checkpoint step（最少 1）；形狀對齊 testpilot case schema。"""
    n = max(1, int(duration_s) // max(1, int(interval_s)))
    return [
        {"id": f"checkpoint-{i:03d}", "action": "longrun_checkpoint", "target": "bench"}
        for i in range(1, n + 1)
    ]


def case_to_dict(case: Any, cfg: dict[str, Any]) -> dict[str, Any]:
    """realhw Case → testpilot case dict。

    - topology.devices 自 cfg["boards"] 帶入（testbed 事實）。
    - longrun tier：steps 依 duration_s/snapshot_interval_s 合成 N checkpoints。
    - pass_criteria 為佔位（always-pass；真正判決集中在 plugin.evaluate）。
    - tier/destructive/requires/hints 進 metadata（選擇期過濾與 family-gate 用）。
    """
    devices = {
        str(b["com"]): {
            "role": str(b.get("alias", "")),
            "serial": str(b.get("serial", "")),
            "busid": str(b.get("busid", "")),
            "platform": str(b.get("platform", "")),
        }
        for b in cfg.get("boards", [])
    }
    if case.tier == "longrun":
        interval = int((cfg.get("longrun") or {}).get("snapshot_interval_s") or 300)
        steps = synth_longrun_steps(int(cfg.get("duration_s") or 0), interval)
    else:
        steps = [{"id": "exec", "action": "run_case", "target": "bench"}]
    return {
        "id": case.id,
        "name": case.title,
        "topology": {"devices": devices},
        "steps": steps,
        "pass_criteria": ["realhw_case_verdict"],
        "metadata": {
            "tier": case.tier,
            "destructive": bool(case.destructive),
            "requires": list(case.requires),
            "hints": list(case.hints),
        },
    }


def build_case_dicts(registry: list[Any], cfg: dict[str, Any]) -> list[dict[str, Any]]:
    """整個 registry → case dicts（維持註冊順序＝執行順序）。"""
    return [case_to_dict(c, cfg) for c in registry]


def filter_for_run(cases: list[dict[str, Any]], requested_ids: set[str]) -> list[dict[str, Any]]:
    """選擇期過濾（openspec Scenario「破壞性 case 選擇期排除」）：

    - 有顯式點名：精確 id 比對（含 destructive——點名即 opt-in）。
    - 無點名：排除 destructive（不進 run、不出現在報表）。
    """
    if requested_ids:
        return [c for c in cases if c["id"] in requested_ids]
    return [c for c in cases if not (c.get("metadata") or {}).get("destructive")]
```

- [ ] **Step 4: 跑 GREEN＋commit**

```bash
python3 -m pytest -q tests/test_reliability_core.py
# 期望：全 passed
git add -u && git add tests/test_reliability_core.py
git commit -m "feat(reliability): case dict 映射、longrun checkpoints 合成與選擇期過濾（TDD）"
```

---

### Task 4: core.py——判決抄寫／runtime skip／black-box 執行／恢復清殘／LongrunRunner（openspec 5.2 後半，TDD）

**Interfaces** — Consumes：`CaseResult`（含 Phase 1 分類欄）、`harness.REGISTRY`、`harness.recovery_command`、`SwCli`/`preflight`；Produces：
`result_to_dict(result) -> dict`、`failure_payload(result_dict) -> dict | None`（＝`case["_last_failure"]` 形狀，對齊 C6）、`runtime_skip(case_meta, capabilities, broken_by) -> tuple[str, str] | None`、`make_skip_result(reason_code, comment) -> CaseResult`、`run_case_blackbox(case_id, ctx) -> CaseResult`、`build_ctx(cfg, report_dir) -> Ctx`、`recover_boards(ctx, boards, *, ready_timeout_s=60.0) -> list[str]`、`sweep_tmux(prefix) -> list[str]`、`checkpoint_index(step_id, *, fallback) -> int`、`class LongrunRunner`、`run_preflight(cfg) -> dict`。

- [ ] **Step 1: RED——append 到 `tests/test_reliability_core.py`**

```python
# ---------------------------------------------------------------- Task 4：判決抄寫與執行編排
def test_failure_payload_pass_is_none():
    assert core.failure_payload({"verdict": "PASS"}) is None


def test_failure_payload_fail_copies_classification():
    payload = core.failure_payload({
        "verdict": "FAIL", "reason": "fan-out 斷線", "category": "test",
        "reason_code": "console_fanout_lost",
        "evidence": {"pane": "p1-con/pane.txt"}, "duration_s": 1.0,
    })
    assert payload == {
        "category": "test",
        "reason_code": "console_fanout_lost",
        "comment": "fan-out 斷線",
        "evidence": ["p1-con/pane.txt"],  # C6：coordinator 端 evidence 契約＝list[str]
        "metadata": {"realhw_verdict": "FAIL"},
    }


def test_failure_payload_runtime_skip_defaults_environment():
    payload = core.failure_payload({"verdict": "SKIP", "reason": "base64 缺", "category": "",
                                    "reason_code": "base64_missing", "evidence": {}})
    assert payload["category"] == "environment"  # 執行期 SKIP＝FailEnv（分類映射總表）


def test_failure_payload_fail_without_category_stays_empty():
    payload = core.failure_payload({"verdict": "FAIL", "reason": "boom", "category": "",
                                    "reason_code": "uncaught_exception", "evidence": {}})
    assert payload["category"] == ""  # 空 category → core coerce inconclusive → Inconclusive（誠實）


def test_runtime_skip_broken_by_and_capabilities():
    meta_dep = {"requires": ["two_boards"], "destructive": False}
    assert core.runtime_skip(meta_dep, {}, "p1-hp-cycle") == (
        "broken_by:p1-hp-cycle", "前置不滿足（p1-hp-cycle 後板卡未恢復）")
    meta_rm = {"requires": ["docker"], "destructive": False}
    assert core.runtime_skip(meta_rm, {"docker": False}, None) == (
        "docker_unavailable", "能力缺項：docker")
    assert core.runtime_skip(meta_rm, {"docker": True}, None) is None
    assert core.runtime_skip(meta_rm, {}, None) is None  # capabilities 未回報該鍵＝不擋（preflight 已把關）
    assert core.runtime_skip({"requires": [], "destructive": False}, {}, "x") is None


def test_make_skip_result_shape():
    r = core.make_skip_result("docker_unavailable", "能力缺項：docker")
    assert (r.verdict, r.category, r.reason_code) == ("SKIP", "environment", "docker_unavailable")


def test_run_case_blackbox_pass_and_uncaught(monkeypatch, tmp_path):
    core.ensure_realhw_importable()
    from realhw import harness

    ok = harness.Case(id="fake-ok", tier="p0", title="ok",
                      run=lambda ctx: harness.CaseResult("PASS"))

    def _boom(ctx):
        raise RuntimeError("爆")

    bad = harness.Case(id="fake-bad", tier="p0", title="bad", run=_boom)
    monkeypatch.setattr(harness, "REGISTRY", [ok, bad])
    ctx = SimpleNamespace(report_dir=tmp_path, case_dir=tmp_path)

    r1 = core.run_case_blackbox("fake-ok", ctx)
    assert r1.verdict == "PASS" and r1.duration_s >= 0.0
    assert ctx.case_dir == tmp_path / "fake-ok"  # 對齊 run_cases：case_dir per-case

    r2 = core.run_case_blackbox("fake-bad", ctx)
    assert (r2.verdict, r2.category, r2.reason_code) == ("FAIL", "", "uncaught_exception")

    r3 = core.run_case_blackbox("no-such", ctx)
    assert (r3.verdict, r3.category, r3.reason_code) == (
        "FAIL", "configuration", "invalid_case_config")


class _FakeSw:
    """恢復流程 fake：第一輪回報非 READY、恢復後 READY。"""

    def __init__(self, initial_state: str) -> None:
        self.state = initial_state
        self.calls: list[tuple[str, ...]] = []

    def session(self, com: str) -> dict:
        return {"com": com, "state": self.state}

    def run(self, *args: str, **kw) -> dict:
        self.calls.append(args)
        self.state = "READY"  # 恢復動詞生效
        return {"ok": True}

    def wait_state(self, com: str, want: str, *, timeout_s: float, poll_s: float = 2.0) -> bool:
        return self.state == want


def test_recover_boards_dispatches_state_aware_verb(monkeypatch):
    monkeypatch.setattr(core.time, "sleep", lambda s: None)
    sw = _FakeSw("ATTACHED")
    ctx = SimpleNamespace(sw=sw)
    left = core.recover_boards(ctx, ["COM0"])
    assert left == []
    assert sw.calls == [("session", "recover", "--selector", "COM0")]  # 非 READY→recover（recovery_command）


def test_recover_boards_ready_is_noop():
    sw = _FakeSw("READY")
    assert core.recover_boards(SimpleNamespace(sw=sw), ["COM0", "COM1"]) == []
    assert sw.calls == []


def test_sweep_tmux_kills_only_prefix(monkeypatch):
    ran: list[list[str]] = []

    def fake_run(argv, capture_output=True, text=True):
        ran.append(list(argv))
        if argv[:2] == ["tmux", "ls"]:
            return SimpleNamespace(stdout="realhw-p0con-1\nother-sess\nrealhw-lrhuman-2\n",
                                   returncode=0)
        return SimpleNamespace(stdout="", returncode=0)

    monkeypatch.setattr(core.subprocess, "run", fake_run)
    killed = core.sweep_tmux("realhw")
    assert killed == ["realhw-p0con-1", "realhw-lrhuman-2"]
    assert ["tmux", "kill-session", "-t", "other-sess"] not in ran


def test_checkpoint_index():
    assert core.checkpoint_index("checkpoint-007", fallback=9) == 7
    assert core.checkpoint_index("weird", fallback=9) == 9


def test_longrun_runner_join_and_snapshots(tmp_path):
    snaps = tmp_path / "snapshots.ndjson"
    snaps.write_text('{"t":0}\n{"t":300}\n', encoding="utf-8")
    sentinel = object()
    runner = core.LongrunRunner(run_fn=lambda: sentinel, snapshots_path=snaps, duration_s=0)
    runner.start()
    progress = runner.wait_checkpoint(1, 1)  # 最後一點：join 收尾
    assert progress == {"checkpoint": 1, "total": 1, "snapshots_seen": 2, "finished": True}
    assert runner.result() is sentinel


def test_longrun_runner_skipped_mode():
    r = core.make_skip_result("docker_unavailable", "能力缺項：docker")
    runner = core.LongrunRunner.skipped(r)
    progress = runner.wait_checkpoint(1, 3)
    assert progress["finished"] is True and progress["snapshots_seen"] == 0
    assert runner.result() is r


def test_run_preflight_refuse_and_ok(monkeypatch):
    core.ensure_realhw_importable()
    from realhw import drivers, preflight

    monkeypatch.setattr(preflight, "collect", lambda cfg, sw, root: "CHECKS")
    monkeypatch.setattr(preflight, "evaluate", lambda c: (False, ["板卡未 READY：COM1"]))
    out = core.run_preflight({"boards": []})
    assert out == {"ok": False, "problems": ["板卡未 READY：COM1"],
                   "capabilities": {}, "deployed_version": ""}

    monkeypatch.setattr(preflight, "evaluate", lambda c: (True, []))
    monkeypatch.setattr(preflight, "capabilities",
                        lambda cfg, sw: {"docker": True}, raising=False)
    monkeypatch.setattr(drivers.SwCli, "run",
                        lambda self, *a, **k: {"_raw": "serialwrap 0.2.3", "_rc": 0})
    out = core.run_preflight({"boards": []})
    assert out["ok"] is True
    assert out["capabilities"] == {"docker": True}
    assert out["deployed_version"] == "serialwrap 0.2.3"
```

- [ ] **Step 2: 跑 RED**

```bash
python3 -m pytest -q tests/test_reliability_core.py
# 期望：Task 4 新測試以 AttributeError 失敗、既有測試仍過
```

- [ ] **Step 3: GREEN——append 到 `core.py`**

```python
# ---------------------------------------------------------------- 判決抄寫（openspec Scenario「分類抄寫落桶」）
#: Case.requires 詞彙 → FailEnv reason_code（分類映射總表；未列者退 <req>_missing）
REQUIRES_REASON: dict[str, str] = {
    "docker": "docker_unavailable",
    "remote_capability": "remote_capability_missing",
    "two_boards": "two_boards_missing",
    "tmux": "tmux_missing",
}


def requires_reason(req: str) -> str:
    """requires 項 → reason_code（family-gate 執行期 SKIP 用）。"""
    return REQUIRES_REASON.get(req, f"{req}_missing")


def result_to_dict(result: Any) -> dict[str, Any]:
    """CaseResult → plain dict（進 execute_step 的 captured；容忍未來增欄）。"""
    return {
        "verdict": str(getattr(result, "verdict", "")),
        "reason": str(getattr(result, "reason", "") or ""),
        "category": str(getattr(result, "category", "") or ""),
        "reason_code": str(getattr(result, "reason_code", "") or ""),
        "evidence": dict(getattr(result, "evidence", {}) or {}),
        "duration_s": float(getattr(result, "duration_s", 0.0) or 0.0),
    }


def failure_payload(result_dict: dict[str, Any]) -> dict[str, Any] | None:
    """CaseResult dict → ``case["_last_failure"]``（PASS 回 None）。

    契約對齊 testpilot-core `remediation._coerce_failure_snapshot`（契約事實 C6）：
    - category/reason_code/comment 直抄；evidence 必須是 **list[str]**（抄 evidence dict 的路徑值）。
    - SKIP 且 category 空 → 補 "environment"（執行期 SKIP＝FailEnv，分類映射總表）。
    - FAIL 且 category 空 → 保持空（core 端 coerce 成 "inconclusive" → Inconclusive，誠實承認分不清）。
    """
    verdict = str(result_dict.get("verdict", ""))
    if verdict == "PASS":
        return None
    category = str(result_dict.get("category", "") or "")
    if verdict == "SKIP" and not category:
        category = "environment"
    evidence = result_dict.get("evidence") or {}
    return {
        "category": category,
        "reason_code": str(result_dict.get("reason_code", "") or ""),
        "comment": str(result_dict.get("reason", "") or f"realhw verdict={verdict}"),
        "evidence": [str(v) for v in evidence.values()],
        "metadata": {"realhw_verdict": verdict},
    }


def runtime_skip(case_meta: dict[str, Any], capabilities: dict[str, bool],
                 broken_by: str | None) -> tuple[str, str] | None:
    """執行期 SKIP 判定：回傳 (reason_code, comment)；None＝可跑。

    - broken_by：前一 case 弄壞板卡未恢復——沿用 run_cases 規則（requires two_boards
      或 destructive 的後續 case SKIP＝FailEnv/broken_by:<id>）。
    - capabilities family-gate：requires 中被 preflight 標為 False 的能力 → SKIP；
      capabilities 未回報該鍵＝不擋（suite-refuse 已把關基本盤）。
    """
    requires = [str(r) for r in (case_meta.get("requires") or [])]
    if broken_by and ("two_boards" in requires or case_meta.get("destructive")):
        return (f"broken_by:{broken_by}", f"前置不滿足（{broken_by} 後板卡未恢復）")
    for req in requires:
        if req in capabilities and not capabilities[req]:
            return (requires_reason(req), f"能力缺項：{req}")
    return None


def make_skip_result(reason_code: str, comment: str) -> Any:
    """合成執行期 SKIP 的 CaseResult（environment→FailEnv）。"""
    ensure_realhw_importable()
    from realhw import harness

    return harness.CaseResult("SKIP", reason=comment,
                              category="environment", reason_code=reason_code)


# ---------------------------------------------------------------- black-box 執行與 bench 編排
def build_ctx(cfg: dict[str, Any], report_dir: Path) -> Any:
    """建 realhw Ctx（drivers 實體化；setup_env 建一次、整場重用）。"""
    ensure_realhw_importable()
    from realhw import drivers, harness

    return harness.Ctx(
        cfg=cfg, report_dir=report_dir, case_dir=report_dir,
        sw=drivers.SwCli(),
        tmux=drivers.TmuxCtl(str(cfg.get("tmux_prefix") or "realhw")),
        usbipd=drivers.Usbipd(str(cfg.get("usbipd_exe") or "")),
        systemd=drivers.Systemd(),
    )


def run_case_blackbox(case_id: str, ctx: Any) -> Any:
    """black-box 呼叫 realhw ``case.run(ctx)``（Thin Adapter 核心）。

    - 未捕捉例外 → FAIL＋reason_code=uncaught_exception、category 空（→Inconclusive），
      對齊 run_cases 的兜底語意。
    - registry 查無 id → FAIL＝configuration/invalid_case_config。
    """
    ensure_realhw_importable()
    from realhw import harness

    target = next((c for c in harness.REGISTRY if c.id == case_id), None)
    if target is None:
        return harness.CaseResult("FAIL", reason=f"registry 查無 case：{case_id}",
                                  category="configuration",
                                  reason_code="invalid_case_config")
    ctx.case_dir = ctx.report_dir / case_id  # 對齊 run_cases：per-case evidence 目錄
    t0 = time.monotonic()
    try:
        result = target.run(ctx)
    except Exception as exc:  # 兜底：case 內未捕捉例外不得炸穿 plugin
        result = harness.CaseResult("FAIL", reason=f"未捕捉例外：{exc!r}",
                                    reason_code="uncaught_exception")
    result.duration_s = time.monotonic() - t0
    return result


def recover_boards(ctx: Any, boards: list[str], *, ready_timeout_s: float = 60.0) -> list[str]:
    """case 間恢復（對齊 run_cases）：非 READY 板依 recovery_command 選語意正確動詞恢復；
    回傳仍未恢復的 COM 清單（供 broken_by 標記）。"""
    ensure_realhw_importable()
    from realhw import harness

    not_ready = [b for b in boards if ctx.sw.session(b).get("state") != "READY"]
    if not not_ready:
        return []
    for b in not_ready:
        verb = harness.recovery_command(ctx.sw.session(b).get("state"))
        ctx.sw.run(*verb, "--selector", b)
    time.sleep(5)
    return [b for b in boards if not ctx.sw.wait_state(b, "READY", timeout_s=ready_timeout_s)]


def sweep_tmux(prefix: str) -> list[str]:
    """掃掉 ``<prefix>-`` 開頭的殘留 tmux session（teardown 清殘；benchlock 保證同 bench 無並行套件）。"""
    cp = subprocess.run(["tmux", "ls", "-F", "#{session_name}"],
                        capture_output=True, text=True)
    killed: list[str] = []
    for name in (cp.stdout or "").splitlines():
        name = name.strip()
        if name.startswith(f"{prefix}-"):
            subprocess.run(["tmux", "kill-session", "-t", name],
                           capture_output=True, text=True)
            killed.append(name)
    return killed


def checkpoint_index(step_id: str, *, fallback: int) -> int:
    """``checkpoint-007`` → 7；解析不出退 fallback。"""
    m = re.search(r"(\d+)$", step_id.strip())
    return int(m.group(1)) if m else fallback


class LongrunRunner:
    """背景 thread 跑 realhw 長跑 case；checkpoint step 依 duration 均分步進。

    - 進度監控來源＝realhw 增量寫的 snapshots.ndjson（testpilot agent_trace 於 case
      結束才落盤，不能當長跑監控來源——openspec Requirement）。
    - 也支援「已判 SKIP」退化模式（:meth:`skipped`）：不開 thread、result 即席回傳。
    """

    def __init__(self, run_fn: Callable[[], Any], snapshots_path: Path, duration_s: int) -> None:
        self._run_fn = run_fn
        self._snapshots_path = Path(snapshots_path)
        self._duration_s = max(0, int(duration_s))
        self._result: Any = None
        self._thread: threading.Thread | None = None
        self._started_at = 0.0

    @classmethod
    def skipped(cls, result: Any) -> "LongrunRunner":
        runner = cls(run_fn=lambda: result,
                     snapshots_path=Path("/nonexistent-snapshots"), duration_s=0)
        runner._result = result
        return runner

    def start(self) -> None:
        if self._thread is not None or self._result is not None:
            return
        self._started_at = time.monotonic()

        def _run() -> None:
            self._result = self._run_fn()

        self._thread = threading.Thread(target=_run, name="reliability-longrun", daemon=True)
        self._thread.start()

    def wait_checkpoint(self, index: int, total: int) -> dict[str, Any]:
        """等到第 index/total 檢查點時刻（長跑先結束則提前返回）；最後一點 join 收尾。"""
        total = max(1, total)
        if self._thread is not None:
            deadline = self._started_at + self._duration_s * (min(index, total) / total)
            while time.monotonic() < deadline and self._thread.is_alive():
                time.sleep(min(5.0, max(0.1, deadline - time.monotonic())))
            if index >= total:
                self._thread.join()
        seen = 0
        if self._snapshots_path.exists():
            text = self._snapshots_path.read_text(encoding="utf-8", errors="replace")
            seen = sum(1 for line in text.splitlines() if line.strip())
        finished = self._thread is None or not self._thread.is_alive()
        return {"checkpoint": index, "total": total,
                "snapshots_seen": seen, "finished": finished}

    def result(self) -> Any:
        """長跑 CaseResult；未完成時 join（evaluate/teardown 的收割保底）。"""
        if self._thread is not None and self._thread.is_alive():
            self._thread.join()
        return self._result


# ---------------------------------------------------------------- preflight gate（openspec：prepare_run＝gate）
def run_preflight(cfg: dict[str, Any]) -> dict[str, Any]:
    """realhw preflight（suite-refuse gate；含 Phase 1 benchlock）＋capabilities＋deployed 版本。

    Phase 1 契約：``preflight.collect(cfg, sw, repo_root)``／``evaluate(checks)`` 簽章不變；
    ``capabilities(cfg, sw)`` 為 Phase 1 新增——此處以 getattr 防禦式消費（缺席時 degrade
    成空 dict：family-gate 不啟動、suite-refuse 不受影響）。

    回傳 ``{"ok": bool, "problems": list[str], "capabilities": dict[str, bool],
    "deployed_version": str}``。
    """
    ensure_realhw_importable()
    from realhw import drivers, preflight

    sw = drivers.SwCli()
    checks = preflight.collect(cfg, sw, REPO_ROOT)
    ok, problems = preflight.evaluate(checks)
    caps_fn = getattr(preflight, "capabilities", None)
    caps = dict(caps_fn(cfg, sw)) if (ok and callable(caps_fn)) else {}
    deployed = str(sw.run("--version").get("_raw", "")).strip() if ok else ""
    return {"ok": bool(ok), "problems": list(problems),
            "capabilities": caps, "deployed_version": deployed}
```

- [ ] **Step 4: 跑 GREEN＋commit**

```bash
python3 -m pytest -q tests/test_reliability_core.py
# 期望：全 passed（約 20+ 項）
git add -u
git commit -m "feat(reliability): 判決抄寫、runtime skip、black-box 執行與 longrun 步進（TDD）"
```

---

### Task 5: testbed_loader——雙來源等價（openspec 5.3，TDD）

**Interfaces** — Consumes：`TestbedConfig.raw` 形狀（C13；整份 YAML dict，頂層 `testbed:`）、`realhw/config.json` 形狀、`harness.parse_duration`；Produces：`testbed_to_cfg(raw: dict) -> dict`（純函式）、`config_json_to_cfg(path, *, duration=None) -> dict`、`load_testbed_cfg(path) -> dict`。等價律：**同一組 bench 事實，兩條路合成的 cfg dict 相等**（openspec Scenario「雙來源等價」）。

- [ ] **Step 1: RED——建立 `tests/test_reliability_testbed.py`**

```python
"""testbed.yaml 與 config.json 雙來源等價單測（openspec 5.3）——不 import testpilot。"""
from __future__ import annotations

import json
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT / "reliability"))

from serialwrap_reliability import testbed_loader  # noqa: E402

# 同一組 bench 事實的兩種寫法 ------------------------------------------------
CONFIG_JSON = {
    "_readme": ["底線鍵＝註解，loader 必須丟棄"],
    "boards": [
        {"com": "COM0", "alias": "dut-prpl", "serial": "AC01QZT0", "busid": "8-1",
         "platform": "prpl"},
        {"com": "COM1", "alias": "sta-prpl", "serial": "AQ00OAQ7", "busid": "8-2",
         "platform": "brcm", "profile": "brcm-template"},
    ],
    "usbipd_exe": "/mnt/c/Program Files/usbipd-win/usbipd.exe",
    "win_serialwrap_exe": "/mnt/c/serialwrap/serialwrap.exe",
    "tmux_prefix": "realhw",
    "timeouts": {"ready_wait_s": 180, "reboot_wait_s": 300, "human_active_window_s": 60},
    "longrun": {"snapshot_interval_s": 300, "agent_workers": 4},
}

TESTBED_RAW = {
    "testbed": {
        "name": "serialwrap-reliability-bench",
        "run_backend": "serialwrap",
        "devices": {
            # 故意反序放 STA 在前：loader 須依 selector 排序回 COM0 在前
            "STA": {"role": "sta", "transport": "serialwrap", "selector": "COM1",
                    "alias": "sta-prpl", "serial": "AQ00OAQ7", "busid": "8-2",
                    "platform": "brcm", "profile": "brcm-template"},
            "DUT": {"role": "dut", "transport": "serialwrap", "selector": "COM0",
                    "alias": "dut-prpl", "serial": "AC01QZT0", "busid": "8-1",
                    "platform": "prpl"},
        },
        "variables": {},
        "serialwrap_reliability": {
            "usbipd_exe": "/mnt/c/Program Files/usbipd-win/usbipd.exe",
            "win_serialwrap_exe": "/mnt/c/serialwrap/serialwrap.exe",
            "tmux_prefix": "realhw",
            "timeouts": {"ready_wait_s": 180, "reboot_wait_s": 300,
                         "human_active_window_s": 60},
            "longrun": {"duration": "15m", "snapshot_interval_s": 300, "agent_workers": 4},
        },
    }
}


def test_two_sources_equivalent(tmp_path):
    p = tmp_path / "config.json"
    p.write_text(json.dumps(CONFIG_JSON, ensure_ascii=False), encoding="utf-8")
    cfg_json = testbed_loader.config_json_to_cfg(p, duration="15m")
    cfg_yaml = testbed_loader.testbed_to_cfg(TESTBED_RAW)
    assert cfg_json == cfg_yaml  # 等價律：同 bench 事實 → 相同 cfg dict


def test_testbed_boards_sorted_by_selector():
    cfg = testbed_loader.testbed_to_cfg(TESTBED_RAW)
    assert [b["com"] for b in cfg["boards"]] == ["COM0", "COM1"]
    assert cfg["boards"][1]["profile"] == "brcm-template"


def test_testbed_duration_converted_and_stripped():
    cfg = testbed_loader.testbed_to_cfg(TESTBED_RAW)
    assert cfg["duration_s"] == 900  # "15m"
    assert "duration" not in cfg["longrun"]  # longrun 子 dict 不留原字串（等價律）


def test_win_serialwrap_exe_passthrough():
    cfg = testbed_loader.testbed_to_cfg(TESTBED_RAW)
    assert cfg["win_serialwrap_exe"] == "/mnt/c/serialwrap/serialwrap.exe"


def test_config_json_drops_comment_keys(tmp_path):
    p = tmp_path / "config.json"
    p.write_text(json.dumps(CONFIG_JSON, ensure_ascii=False), encoding="utf-8")
    cfg = testbed_loader.config_json_to_cfg(p)
    assert not any(k.startswith("_") for k in cfg)
    assert "duration_s" not in cfg  # 未給 duration 就不合成
```

- [ ] **Step 2: 跑 RED**

```bash
python3 -m pytest -q tests/test_reliability_testbed.py
# 期望：ModuleNotFoundError: serialwrap_reliability.testbed_loader → 失敗
```

- [ ] **Step 3: GREEN——建立 `reliability/serialwrap_reliability/testbed_loader.py`**

```python
"""testbed.yaml（plugin）與 config.json（standalone）雙來源 → 同形 realhw cfg dict。

等價律（openspec Requirement「組態來源與雙來源等價」）：同一組 bench 事實，
兩條路合成的 cfg dict **相等**——由 tests/test_reliability_testbed.py 釘死。
不 import testpilot；PyYAML 僅在 load_testbed_cfg 內延遲 import（serialwrap 執行期既有依賴）。
"""
from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from serialwrap_reliability.core import ensure_realhw_importable

#: 裝置欄位 → board dict 欄位（缺欄不寫，維持與 config.json 等價的最小形狀）
_BOARD_OPTIONAL_KEYS: tuple[str, ...] = ("alias", "serial", "busid", "platform", "profile")
#: serialwrap_reliability 區塊直帶欄位（longrun 另行正規化）
_SECTION_KEYS: tuple[str, ...] = ("usbipd_exe", "win_serialwrap_exe", "tmux_prefix", "timeouts")


def testbed_to_cfg(raw: dict[str, Any]) -> dict[str, Any]:
    """TestbedConfig.raw（整份 YAML dict）→ realhw cfg dict。

    - ``testbed.devices``：selector 當 COM；boards 依 selector 排序（確定性）。
    - ``testbed.serialwrap_reliability``：usbipd_exe/win_serialwrap_exe/tmux_prefix/
      timeouts 直帶；``longrun.duration``（如 "15m"）經 realhw parse_duration 換算成
      頂層 ``duration_s``，longrun 子 dict 內不留 duration 字串（等價律）。
    """
    ensure_realhw_importable()
    from realhw import harness

    testbed = raw.get("testbed", raw) or {}
    section = dict(testbed.get("serialwrap_reliability") or {})

    boards: list[dict[str, Any]] = []
    devices = testbed.get("devices") or {}
    for _, dev in sorted(devices.items(), key=lambda kv: str((kv[1] or {}).get("selector", ""))):
        dev = dev or {}
        board: dict[str, Any] = {"com": str(dev.get("selector", ""))}
        for key in _BOARD_OPTIONAL_KEYS:
            if key in dev:
                board[key] = dev[key]
        boards.append(board)

    cfg: dict[str, Any] = {"boards": boards}
    for key in _SECTION_KEYS:
        if key in section:
            cfg[key] = section[key]

    longrun = dict(section.get("longrun") or {})
    duration = longrun.pop("duration", None)
    if longrun or "longrun" in section:
        cfg["longrun"] = longrun
    if duration:
        cfg["duration_s"] = harness.parse_duration(str(duration))
    return cfg


def config_json_to_cfg(path: Path | str, *, duration: str | None = None) -> dict[str, Any]:
    """standalone config.json → cfg（丟棄 ``_`` 開頭註解鍵；duration 換算 duration_s）。"""
    ensure_realhw_importable()
    from realhw import harness

    data = json.loads(Path(path).read_text(encoding="utf-8"))
    cfg = {k: v for k, v in data.items() if not str(k).startswith("_")}
    if duration:
        cfg["duration_s"] = harness.parse_duration(duration)
    return cfg


def load_testbed_cfg(path: Path | str) -> dict[str, Any]:
    """讀 testbed.yaml 檔 → cfg（PyYAML 延遲 import）。"""
    import yaml

    raw = yaml.safe_load(Path(path).read_text(encoding="utf-8")) or {}
    return testbed_to_cfg(raw)
```

- [ ] **Step 4: 跑 GREEN＋commit**

```bash
python3 -m pytest -q tests/test_reliability_testbed.py tests/test_reliability_core.py
# 期望：全 passed
git add reliability/serialwrap_reliability/testbed_loader.py tests/test_reliability_testbed.py
git commit -m "feat(reliability): testbed loader 與 config.json 雙來源等價（TDD）"
```

---

### Task 6: plugin.py＋agent-config.yaml＋testbed.yaml.example（openspec 5.4，TDD＝契約檔單測）

**Interfaces** — Consumes：`testpilot.api.PluginBase`/`PreparedRun`（C1/C2）、`core.*`（Task 2-4 全部）、`testbed_loader.testbed_to_cfg`；Produces：`Plugin`（`api_version="1.1"`、`name="serialwrap_reliability"`、生命週期映射全套）、`agent-config.yaml`（C7-C10 鎖死組合）、`testbed.yaml.example`（bench 事實正本，C12）。

> **C7 裁決說明（worker 必讀，勿「修正」回 false）**：`remediation.enabled: true` 在這裡是「**FailureSnapshot 擷取開關**」而不是「自動修復開關」。動作被以下防線鎖死：
> ① `retry.max_attempts: 1` → `on_retry` 永不 dispatch（remediation 唯一執行點在 on_retry，`execution_engine.py` L302-316）且 `enabled_hooks` 也不含 `on_retry`——從時序上移除執行點；
> ② 本 plugin 不覆寫 `request_remediation_decision`/`build_remediation_decision`（PluginBase 預設回 None → decision 永遠 None，`_validate_decision` 首行直接 return）——**這是唯一真正生效的 decision 阻擋點**；
> ③ `allowed_actions: []`——**注意：不具攔截效果**（`remediation.py` L483 是 `if self.allowed_actions and ...`，空集合 falsy、白名單檢查被短路＝不攔截）。空清單僅保留為宣示性組態，不可依賴。
> 若設 false：`handle_on_failure` 直接 return（`remediation.py` L358-359）→ failure_snapshot 永遠 None → **所有 FAIL 都變 Inconclusive**，openspec「分類抄寫落桶」Scenario 必死。PR 需同步修訂 openspec spec 的「remediation `enabled: false`」字句。

- [ ] **Step 1: RED——建立 `tests/test_reliability_pluginfiles.py`**

```python
"""plugin 契約檔單測（agent-config／testbed example／pyproject／plugin.py 原始碼掃描）。

plugin.py 本體邏輯 bench 才驗（需 testpilot venv）；本檔只釘「檔案契約」——
不 import testpilot、不 import plugin。
"""
from __future__ import annotations

import re
import sys
from pathlib import Path

import yaml

REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT / "reliability"))

from serialwrap_reliability import testbed_loader  # noqa: E402

PKG = REPO_ROOT / "reliability" / "serialwrap_reliability"


def test_agent_config_locks_remediation_and_retry():
    cfg = yaml.safe_load((PKG / "agent-config.yaml").read_text(encoding="utf-8"))
    execution = cfg["execution"]
    assert execution["mode"] == "sequential"
    assert execution["max_concurrency"] == 1
    assert execution["retry"]["max_attempts"] == 1  # C9：缺省預設是 2，必須顯式 1
    hooks = set(cfg["hooks"]["enabled_hooks"])
    assert {"pre_case", "post_case", "on_failure"} <= hooks  # C8：snapshot 擷取必需
    assert "on_retry" not in hooks  # 防線：remediation 唯一執行點永不 dispatch
    assert cfg["remediation"]["enabled"] is True  # C7 裁決：snapshot 擷取開關
    # 宣示性組態：空白名單在 core 被 falsy 短路、不具攔截效果（真正阻擋＝decision 恆 None，見 C10）
    assert cfg["remediation"]["allowed_actions"] == []


def test_testbed_example_equivalent_to_config_json():
    # 5.3 等價律的「真檔」版本：example 與 realhw/config.json 描述同一個 bench
    raw = yaml.safe_load((PKG / "testbed.yaml.example").read_text(encoding="utf-8"))
    cfg_yaml = testbed_loader.testbed_to_cfg(raw)
    cfg_json = testbed_loader.config_json_to_cfg(REPO_ROOT / "realhw" / "config.json")
    assert cfg_yaml["boards"] == cfg_json["boards"]
    for key in ("usbipd_exe", "tmux_prefix", "timeouts", "longrun"):
        assert cfg_yaml[key] == cfg_json[key], f"{key} 兩來源不一致"
    assert cfg_yaml["duration_s"] == 900  # example 預設短跑 15m
    assert cfg_yaml.get("win_serialwrap_exe")  # Windows 端 serialwrap.exe 路徑必填


def test_pyproject_entry_point_and_pin():
    text = (REPO_ROOT / "reliability" / "pyproject.toml").read_text(encoding="utf-8")
    assert 'name = "serialwrap-reliability"' in text
    assert 'version = "0.1.0"' in text
    assert '"testpilot.plugins"' in text
    assert 'serialwrap_reliability = "serialwrap_reliability.plugin:Plugin"' in text
    assert '"testpilot-core>=0.3.4,<1.0"' in text


def test_plugin_source_contract():
    text = (PKG / "plugin.py").read_text(encoding="utf-8")
    assert re.search(r'^\s*api_version\s*=\s*"1\.1"', text, re.M)  # C1
    # Thin Adapter：plugin.py 是唯一 testpilot 接觸面；其餘模組已由
    # test_core_modules_do_not_import_testpilot 反向釘住
    assert re.search(r"from testpilot\.api import .*PluginBase", text)
```

- [ ] **Step 2: 跑 RED**

```bash
python3 -m pytest -q tests/test_reliability_pluginfiles.py
# 期望：FileNotFoundError（agent-config.yaml / testbed.yaml.example / plugin.py 尚不存在）→ 失敗
```

- [ ] **Step 3: GREEN——建立 `reliability/serialwrap_reliability/agent-config.yaml`**

```yaml
# serialwrap_reliability 執行策略（testpilot RunnerSelector 消費；位置＝plugin_root，契約 C11）。
version: 1
execution:
  scope: per_case
  mode: sequential            # 真機單 bench：嚴格序列化
  max_concurrency: 1
  failure_policy: retry_then_fail_and_continue
  retry:
    max_attempts: 1           # 真機 case 重跑無意義且危險（restart/插拔）；缺省預設是 2，必須顯式 1
  timeout:
    base_seconds: 120         # 軟性數字：core 不 kill、僅進 trace（契約 C18）
    per_step_seconds: 60
    max_seconds: 900
hooks:
  enabled_hooks:              # 最小集；on_failure＝FailureSnapshot 擷取所必需（契約 C8）
    - pre_case
    - post_case
    - on_failure
  fail_open: true
remediation:
  # 注意：enabled 在 core 的實作裡是「FailureSnapshot 擷取開關」——設 false 會讓
  # handle_on_failure 直接 return、所有 FAIL 判 Inconclusive（remediation.py L358）。
  # 自動修復動作的阻擋：max_attempts=1（on_retry 永不 dispatch、且未列入 enabled_hooks，
  # 移除唯一執行點）＋plugin 不覆寫 decision hooks（預設 None → decision 恆 None——
  # 這是唯一真正生效的阻擋點）。注意：allowed_actions 空清單在 core 是 falsy、白名單
  # 檢查被短路（remediation.py L483）——不具攔截效果、不可依賴，留空僅為宣示。
  enabled: true
  allowed_actions: []
```

- [ ] **Step 4: GREEN——建立 `reliability/serialwrap_reliability/testbed.yaml.example`**

```yaml
# serialwrap-reliability bench 事實源。
# 注意（契約 C12）：testpilot 每次解析本 plugin 都會把「本檔」原樣覆蓋到
# <root>/configs/testbed.yaml——editable 佈局下本檔就是可編輯正本；改 staged 副本無效。
# 與 realhw/config.json 描述同一個 bench（雙來源等價由單測釘死）。
# R-21：勿寫絕對 home 路徑（/mnt/c/... 不受限）。
testbed:
  name: serialwrap-reliability-bench
  run_backend: serialwrap
  devices:
    DUT:
      role: dut
      transport: serialwrap
      selector: COM0
      alias: dut-prpl
      serial: AC01QZT0
      busid: "8-1"
      platform: prpl
    STA:
      role: sta
      transport: serialwrap
      selector: COM1
      alias: sta-prpl
      serial: AQ00OAQ7
      busid: "8-2"
      platform: brcm
      profile: brcm-template
  variables: {}
  serialwrap_reliability:
    usbipd_exe: "/mnt/c/Program Files/usbipd-win/usbipd.exe"
    win_serialwrap_exe: "/mnt/c/serialwrap/serialwrap.exe"   # Windows 端原生 serialwrap.exe（WinSwCli／hp 救援鏈）
    tmux_prefix: realhw
    timeouts:
      ready_wait_s: 180
      reboot_wait_s: 300
      human_active_window_s: 60
    longrun:
      duration: "15m"          # 短跑預設（bench 驗收/日常）；48h 僅重大進版前手動改
      snapshot_interval_s: 300
      agent_workers: 4
```

> 若 Phase 1 的 2.4 已把 `win_serialwrap_exe` 寫進 `realhw/config.json`，`test_testbed_example_equivalent_to_config_json` 可再加一行 `assert cfg_yaml["win_serialwrap_exe"] == cfg_json["win_serialwrap_exe"]`；本檔的值以 bench 實際路徑為準（裝機時對照 `ls /mnt/c/...` 修正——example 值是佔位慣例值，不是猜測的真路徑）。

- [ ] **Step 5: GREEN——建立 `reliability/serialwrap_reliability/plugin.py`**

```python
"""testpilot PluginBase glue——薄殼（Thin Adapter；邏輯在 core.py／testbed_loader.py／reporter.py）。

僅本檔 import testpilot；serialwrap CI 不裝 testpilot 也能測其餘模組。
本檔邏輯的驗證面＝bench 整合（plan Task 9-13）；CI 只掃原始碼契約（test_reliability_pluginfiles）。

生命週期映射（openspec Requirement「生命週期映射（Thin Adapter）」）：
- prepare_run  → realhw preflight 當 suite-refuse gate（缺項 raise，run_loop 不捕捉→fail-fast）
                 ＋選擇期過濾（destructive 未點名不進 run）
- setup_env    → 建一次 realhw Ctx（per-case 重用）
- execute_step → black-box case.run(ctx)；**一律回 success=True**（契約 C3：step 失敗會
                 跳過 evaluate；判決權集中在 evaluate）
- evaluate     → CaseResult verdict → bool；FAIL/SKIP 把分類抄進 case["_last_failure"]（契約 C4-C6）
- teardown     → 板卡恢復（recovery_command 語意分派）＋tmux 殘留清掃＋broken_by 標記
"""
from __future__ import annotations

import datetime
import shutil
import subprocess
import time
from pathlib import Path
from typing import Any, Sequence

import yaml

from testpilot.api import PluginBase, PreparedRun

from serialwrap_reliability import core
from serialwrap_reliability.testbed_loader import testbed_to_cfg


class PreflightRefused(RuntimeError):
    """suite-refuse：preflight 缺項整場拒跑（在任何 case verdict 之前 fail-fast）。"""


class Plugin(PluginBase):
    """serialwrap-reliability：realhw 引擎的第二個前端。"""

    api_version = "1.1"

    def __init__(self) -> None:
        # reporter 消費的公開狀態（run_loop 在所有 case 之後才 create_reporter，契約 C14）
        self.run_results: list[tuple[str, Any]] = []   # (case_id, CaseResult)
        self.run_meta: dict[str, Any] = {}             # version/git/started_at/preflight_notes
        self.ctx: Any = None                           # realhw Ctx（setup_env 建一次）
        # 內部狀態
        self._cfg: dict[str, Any] | None = None
        self._capabilities: dict[str, bool] = {}
        self._broken_by: str | None = None
        self._longruns: dict[str, core.LongrunRunner] = {}

    # ------------------------------------------------------------------ 識別
    @property
    def name(self) -> str:
        return "serialwrap_reliability"

    @property
    def version(self) -> str:
        return "0.1.0"

    def execution_policy(self, case: dict[str, Any]) -> dict[str, Any]:
        """真機單 bench：嚴格序列化（run-level 只採 mode/max_concurrency，契約 C20）。"""
        return {"mode": "sequential", "max_concurrency": 1}

    # ------------------------------------------------------------------ 組態
    def _load_cfg(self) -> dict[str, Any]:
        """bench 事實源＝plugin_root/testbed.yaml.example。

        staging（契約 C12）每次 run 把本檔原樣覆蓋到 configs/testbed.yaml——讀正本
        與讀 staged 副本恆等，且 prepare_run/discover_cases 沒有 topology 可用，
        直接讀正本可保兩處零漂移。
        """
        if self._cfg is None:
            raw = yaml.safe_load(
                (self.plugin_root / "testbed.yaml.example").read_text(encoding="utf-8")) or {}
            self._cfg = testbed_to_cfg(raw)
        return self._cfg

    # ------------------------------------------------------------------ 發現與選擇
    def discover_cases(self) -> list[dict[str, Any]]:
        return core.build_case_dicts(core.load_registry(), self._load_cfg())

    def prepare_run(self, case_ids: Sequence[str] | None) -> PreparedRun:
        cfg = self._load_cfg()
        pf = core.run_preflight(cfg)
        for note in pf["problems"]:
            print(f"[preflight] {note}")
        if not pf["ok"]:
            raise PreflightRefused("preflight 拒跑：" + "；".join(pf["problems"]))
        self._capabilities = dict(pf["capabilities"])
        head = subprocess.run(
            ["git", "-C", str(core.REPO_ROOT), "rev-parse", "--short", "HEAD"],
            capture_output=True, text=True).stdout.strip()
        self.run_meta = {
            "version": pf["deployed_version"],   # 報告身分＝deployed serialwrap 版本
            "git": head,                          # repo HEAD sha
            "tiers": "plugin",
            "started_at": datetime.datetime.now().strftime("%y%m%d-%H%M%S"),
            "preflight_notes": list(pf["problems"]),
            # 板卡降為環境 metadata（openspec Scenario「報告身分烙印」）；板卡 fw 的
            # 主動探測屬 Phase 1 preflight 範圍，preflight 若回報即隨 problems/notes 入 meta
            "boards": [
                {"com": str(b.get("com", "")), "alias": str(b.get("alias", "")),
                 "serial": str(b.get("serial", "")), "platform": str(b.get("platform", ""))}
                for b in cfg.get("boards", [])
            ],
        }
        requested = {str(c).strip() for c in (case_ids or []) if str(c).strip()}
        cases = core.filter_for_run(self.discover_cases(), requested)
        return PreparedRun(cases=cases, artifacts={
            "realhw_meta": dict(self.run_meta),
            "capabilities": dict(self._capabilities),
        })

    # ------------------------------------------------------------------ 生命週期
    def setup_env(self, case: dict[str, Any], topology: Any) -> bool:
        if self.ctx is None:
            ts = self.run_meta.get("started_at") or datetime.datetime.now().strftime(
                "%y%m%d-%H%M%S")
            report_dir = Path.home() / "b-log" / "realhw-reports" / f"tp-{ts}"
            self.ctx = core.build_ctx(self._load_cfg(), report_dir)
        return True

    def execute_step(self, case: dict[str, Any], step: dict[str, Any],
                     topology: Any) -> dict[str, Any]:
        case_id = str(case.get("id", "?"))
        meta = dict(case.get("metadata") or {})
        t0 = time.monotonic()
        if str(step.get("action") or "run_case") == "longrun_checkpoint":
            return self._execute_checkpoint(case, step, case_id, meta, t0)

        skip = core.runtime_skip(meta, self._capabilities, self._broken_by)
        if skip is not None:
            reason_code, comment = skip
            result = core.make_skip_result(reason_code, comment)
        else:
            result = core.run_case_blackbox(case_id, self.ctx)
        self.run_results.append((case_id, result))
        # 契約 C3：一律 success=True——判決權在 evaluate（step 失敗會讓 evaluate 被跳過）
        return {
            "success": True,
            "output": str(getattr(result, "reason", "") or result.verdict),
            "captured": {"realhw": core.result_to_dict(result)},
            "timing": time.monotonic() - t0,
        }

    def _execute_checkpoint(self, case: dict[str, Any], step: dict[str, Any],
                            case_id: str, meta: dict[str, Any], t0: float) -> dict[str, Any]:
        runner = self._longruns.get(case_id)
        if runner is None:
            skip = core.runtime_skip(meta, self._capabilities, self._broken_by)
            if skip is not None:
                reason_code, comment = skip
                runner = core.LongrunRunner.skipped(core.make_skip_result(reason_code, comment))
            else:
                runner = core.LongrunRunner(
                    run_fn=lambda: core.run_case_blackbox(case_id, self.ctx),
                    snapshots_path=self.ctx.report_dir / case_id / "snapshots.ndjson",
                    duration_s=int(self.ctx.cfg.get("duration_s") or 0),
                )
                runner.start()
            self._longruns[case_id] = runner
        total = len(case.get("steps") or []) or 1
        index = core.checkpoint_index(str(step.get("id", "")), fallback=total)
        progress = runner.wait_checkpoint(index, total)
        return {
            "success": True,  # always-pass：不讓 core 中斷長跑（openspec Requirement）
            "output": (f"checkpoint {index}/{total}"
                       f"（snapshots={progress['snapshots_seen']}，finished={progress['finished']}）"),
            "captured": {"progress": progress},
            "timing": time.monotonic() - t0,
        }

    def evaluate(self, case: dict[str, Any], results: dict[str, Any]) -> bool:
        case_id = str(case.get("id", "?"))
        runner = self._longruns.pop(case_id, None)
        if runner is not None:
            result = runner.result()  # 判決集中收尾：join 後讀 CaseResult
            self.run_results.append((case_id, result))
            result_dict = core.result_to_dict(result)
        else:
            result_dict = None
            for step_result in (results.get("steps") or {}).values():
                captured = (step_result or {}).get("captured") or {}
                if "realhw" in captured:
                    result_dict = captured["realhw"]
            if result_dict is None:
                # adapter 保險絲：沒有 realhw 結果＝Inconclusive（category 空）
                case["_last_failure"] = {
                    "category": "", "reason_code": "adapter_no_result",
                    "comment": "execute_step 未產出 realhw 結果（adapter 缺陷）",
                    "evidence": [],
                }
                return False
        payload = core.failure_payload(result_dict)
        if payload is None:
            return True
        case["_last_failure"] = payload  # C4-C6：on_failure hook 由此抄成 FailureSnapshot
        return False

    def teardown(self, case: dict[str, Any], topology: Any) -> None:
        case_id = str(case.get("id", "?"))
        runner = self._longruns.pop(case_id, None)
        if runner is not None:
            # 例外路徑保險：evaluate 沒收割到（execute_step 中途炸）就在這裡收
            self.run_results.append((case_id, runner.result()))
        if self.ctx is None:
            return
        cfg = self.ctx.cfg
        boards = [str(b["com"]) for b in cfg.get("boards", [])]
        not_ready = core.recover_boards(self.ctx, boards)
        if not_ready and self._broken_by is None:
            self._broken_by = case_id  # 後續依賴板卡的 case → FailEnv/broken_by:<id>
        core.sweep_tmux(str(cfg.get("tmux_prefix") or "realhw"))

    # ------------------------------------------------------------------ 安裝健檢與報告
    def verify_install(self) -> list[tuple[bool, str]]:
        """映射 realhw preflight 的工具檢查（testpilot --verify-install）。"""
        checks: list[tuple[bool, str]] = [
            (shutil.which("serialwrap") is not None, "serialwrap CLI 在 PATH"),
            (shutil.which("tmux") is not None, "tmux 可用"),
            (shutil.which("minicom") is not None, "minicom 可用"),
        ]
        try:
            cfg = self._load_cfg()
            usbipd = str(cfg.get("usbipd_exe", ""))
            checks.append((bool(usbipd) and Path(usbipd).exists(),
                           f"usbipd 存在（{usbipd or '未設定'}）"))
            checks.append((bool(cfg.get("boards")), "testbed 至少一塊板"))
        except Exception as exc:
            checks.append((False, f"testbed.yaml.example 載入失敗：{exc!r}"))
        try:
            n = len(core.load_registry())
            checks.append((n >= 29, f"realhw registry 可載入（{n} cases）"))
        except Exception as exc:
            checks.append((False, f"realhw 載入失敗：{exc!r}"))
        return checks

    def capture_dut_firmware_version(self, config: Any,
                                     cases: list[dict[str, Any]]) -> dict[str, Any]:
        """報告身分（契約 C16）：--dut-fw-ver 未給時，以 deployed serialwrap 版本當 fw_ver。"""
        deployed = str(self.run_meta.get("version", "")).strip()
        return {"git": deployed} if deployed else {}

    def create_reporter(self) -> Any:
        from serialwrap_reliability.reporter import ReliabilityReporter

        return ReliabilityReporter(plugin=self)

    def report_formats(self) -> list[str]:
        return ["md", "json"]
```

- [ ] **Step 6: 跑 GREEN＋commit**

```bash
python3 -m pytest -q tests/test_reliability_pluginfiles.py tests/test_reliability_core.py tests/test_reliability_testbed.py
# 期望：全 passed
git add reliability/serialwrap_reliability/ tests/test_reliability_pluginfiles.py
git commit -m "feat(reliability): PluginBase glue、agent-config 鎖死組合與 testbed 事實正本"
```

---

### Task 7: reporter.py——md/json 重用 realhw 報告＋身分烙印（openspec 5.5，TDD）

**Interfaces** — Consumes：`RunResult` duck-type（C15：`artifact_dir`/`artifacts`/`fw_ver`/`run_id`/`cases[].retry.diagnostic_status`）、`plugin.run_results`/`run_meta`/`ctx`、`harness.write_reports`、`core.load_registry`；Produces：`ReliabilityReporter.build_reports(run_result) -> dict`（run_loop 契約 C14）。

- [ ] **Step 1: RED——建立 `tests/test_reliability_reporter.py`**

```python
"""reporter 單測（stub RunResult／stub plugin）——不 import testpilot。"""
from __future__ import annotations

import json
import sys
from pathlib import Path
from types import SimpleNamespace

REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT / "reliability"))

from serialwrap_reliability import core  # noqa: E402
from serialwrap_reliability.reporter import ReliabilityReporter  # noqa: E402


def _mk_run(tmp_path: Path):
    core.ensure_realhw_importable()
    from realhw import harness

    report_dir = tmp_path / "b-log-run"
    artifact_dir = tmp_path / "tp-artifacts"
    plugin = SimpleNamespace(
        run_results=[
            ("p0-doctor", harness.CaseResult("PASS")),
            ("p0-cmd-async", harness.CaseResult(
                "FAIL", reason="marker 未見", category="test",
                reason_code="marker_missing", evidence={"cmd": "p0-cmd-async/cmd.json"})),
        ],
        run_meta={"version": "serialwrap 0.2.3", "git": "abc1234",
                  "tiers": "plugin", "started_at": "260720-120000",
                  "preflight_notes": []},
        ctx=SimpleNamespace(report_dir=report_dir),
    )
    run_result = SimpleNamespace(
        run_id="20260720T120000000000",
        fw_ver="serialwrap 0.2.3",
        artifact_dir=artifact_dir,
        artifacts={"realhw_meta": dict(plugin.run_meta)},
        cases=[
            SimpleNamespace(retry=SimpleNamespace(diagnostic_status="Pass")),
            SimpleNamespace(retry=SimpleNamespace(diagnostic_status="FailTest")),
        ],
    )
    return plugin, run_result, report_dir, artifact_dir


def test_build_reports_writes_md_json_and_copies(tmp_path):
    plugin, run_result, report_dir, artifact_dir = _mk_run(tmp_path)
    payload = ReliabilityReporter(plugin=plugin).build_reports(run_result)

    assert (report_dir / "report.md").is_file()      # realhw 慣有落點（b-log）
    assert (report_dir / "report.json").is_file()
    assert (artifact_dir / "report.md").is_file()    # testpilot artifact_dir 拿 copy
    assert (artifact_dir / "report.json").is_file()

    data = json.loads((report_dir / "report.json").read_text(encoding="utf-8"))
    assert data["meta"]["version"] == "serialwrap 0.2.3"  # 報告身分＝deployed 版本
    assert data["meta"]["fw_ver"] == "serialwrap 0.2.3"
    assert data["meta"]["run_id"] == "20260720T120000000000"
    ids = [r["id"] for r in data["results"]]
    assert ids == ["p0-doctor", "p0-cmd-async"]

    assert payload["plugin"] == "serialwrap_reliability"
    assert payload["diagnostic_counts"] == {"Pass": 1, "FailTest": 1}
    assert payload["cases"] == 2
    assert payload["reports"]["report.md"].endswith("report.md")
```

- [ ] **Step 2: 跑 RED**

```bash
python3 -m pytest -q tests/test_reliability_reporter.py
# 期望：ModuleNotFoundError: serialwrap_reliability.reporter → 失敗
```

- [ ] **Step 3: GREEN——建立 `reliability/serialwrap_reliability/reporter.py`**

```python
"""md/json 報表——重用 realhw write_reports 產物＋run meta 烙 deployed serialwrap 版本。

run_loop 的 reporter 契約（契約 C14）＝物件有 ``build_reports(run_result) -> dict``；
本檔以 duck-typing 消費 RunResult 屬性，不 import testpilot（CI 可測）。
"""
from __future__ import annotations

import shutil
from pathlib import Path
from typing import Any

from serialwrap_reliability import core


class ReliabilityReporter:
    """把 plugin 累積的 (case_id, CaseResult) 寫成 realhw 慣有 report.md/report.json，
    並複製到 testpilot 的 artifact_dir；回傳 run 摘要 payload（run_loop 原樣輸出）。"""

    def __init__(self, plugin: Any) -> None:
        self._plugin = plugin

    def build_reports(self, run_result: Any) -> dict[str, Any]:
        core.ensure_realhw_importable()
        from realhw import harness

        registry = {c.id: c for c in core.load_registry()}
        results = list(self._plugin.run_results)
        hints = {cid: (registry[cid].hints if cid in registry else ())
                 for cid, _ in results}

        artifacts = dict(getattr(run_result, "artifacts", {}) or {})
        meta = dict(artifacts.get("realhw_meta") or self._plugin.run_meta or {})
        meta["fw_ver"] = str(getattr(run_result, "fw_ver", "") or "")     # 報告身分
        meta["run_id"] = str(getattr(run_result, "run_id", "") or "")

        ctx = getattr(self._plugin, "ctx", None)
        report_dir = Path(ctx.report_dir) if ctx is not None else Path(
            getattr(run_result, "artifact_dir"))
        harness.write_reports(report_dir, meta, results, hints)

        artifact_dir = Path(getattr(run_result, "artifact_dir"))
        artifact_dir.mkdir(parents=True, exist_ok=True)
        copies: dict[str, str] = {}
        for name in ("report.md", "report.json"):
            src = report_dir / name
            if src.is_file() and src.resolve() != (artifact_dir / name).resolve():
                shutil.copy2(src, artifact_dir / name)
            copies[name] = str(artifact_dir / name)

        diag: dict[str, int] = {}
        for record in getattr(run_result, "cases", []) or []:
            status = str(getattr(getattr(record, "retry", None),
                                 "diagnostic_status", "") or "?")
            diag[status] = diag.get(status, 0) + 1

        return {
            "plugin": "serialwrap_reliability",
            "run_id": meta.get("run_id", ""),
            "deployed_version": meta.get("version", ""),
            "report_dir": str(report_dir),
            "reports": copies,
            "diagnostic_counts": diag,
            "cases": len(results),
        }
```

- [ ] **Step 4: 跑 GREEN＋commit**

```bash
python3 -m pytest -q tests/test_reliability_reporter.py
# 期望：1 passed
git add reliability/serialwrap_reliability/reporter.py tests/test_reliability_reporter.py
git commit -m "feat(reliability): reporter 重用 realhw 報告並烙 deployed 版本（TDD）"
```

---

### Task 8: 收尾——changelog fragment／docs 對齊／全量驗證（openspec 7.x 之 Phase 2 份額）

**Interfaces** — Consumes：全部前置任務；Produces：R-09 fragment、checklist 增節、全量綠。

- [ ] **Step 1: `changelog.d/reliability-testpilot-plugin.md`**

```markdown
---
type: feat
scope: reliability
---
`reliability/`：serialwrap-reliability testpilot plugin 殼（dev-only editable dist，永不 release）——entry point 註冊、PluginBase 生命週期映射（prepare_run＝realhw preflight gate、execute_step＝black-box `case.run(ctx)`、evaluate＝分類抄寫 `_last_failure`）、testbed.yaml 與 config.json 雙來源等價 loader、md/json reporter 重用 realhw 報告與 deployed 版本烙印；release wheel 零改動。
```

- [ ] **Step 2: docs 對齊（R-18）——`docs/func-test/realhw-stability-checklist.md` 檔尾新增一節**

```markdown
## testpilot plugin 入口（Phase 2，dev-only）

realhw 的第二個前端；引擎與 case 完全同一套，選擇/分診/trace/報表交給 testpilot。

- 安裝（僅 editable；永不 release）：`cd ~/prj_arc/testpilot && uv pip install -e ~/prj_pri/serialwrap/reliability`
- bench 事實正本：`reliability/serialwrap_reliability/testbed.yaml.example`（testpilot 每次 run 會把它覆蓋到 `configs/testbed.yaml`——改正本，勿改 staged 副本）；與 `realhw/config.json` 的等價性由 `tests/test_reliability_pluginfiles.py` 釘死。
- 常用命令：`testpilot list-plugins`／`testpilot list-cases serialwrap_reliability`／`testpilot run serialwrap_reliability --case p0-doctor`。
- 分診契約：PASS→Pass；FAIL 依 `CaseResult.category` 落 FailTest/FailEnv/FailConfig；執行期 SKIP→FailEnv；未捕捉例外→Inconclusive。破壞性 case 預設不進 run，`--case` 顯式點名才執行。
- 長跑：`lr-mixed` 於 discover 依 testbed `longrun.duration`/`snapshot_interval_s` 合成 N 個 checkpoint step；進度看 realhw 的 `snapshots.ndjson`，判決集中收尾。
```

- [ ] **Step 3: 全量驗證（含 wheel 複驗）**

```bash
python3 -m pytest -q tests/
# 期望：新測試全過；除既有 flaky（multiagent_e2e t5、t8-coexist、t1-wal-reset）外無失敗
python3 -m policy_check --repo .
# 期望：R-01..R-22 無 FAIL（R-09 由 fragment 滿足；R-18 由 checklist 增節滿足）
WHEELDIR=$(mktemp -d)
python3 -m pip wheel . --no-deps -w "$WHEELDIR" -q
unzip -Z1 "$WHEELDIR"/serialwrap-*.whl | grep -E 'realhw|reliability' ; echo "grep rc=$?"
# 期望：無輸出、rc=1（release wheel 零改動）
```

- [ ] **Step 4: commit**

```bash
git add changelog.d/reliability-testpilot-plugin.md docs/func-test/realhw-stability-checklist.md
git commit -m "docs(reliability): changelog fragment 與 checklist plugin 入口節（R-09/R-18）"
```

---

### Task 9:【真機-人工閘】editable install＋冒煙（openspec 6.1）

> 前置：Phase 1 的營運前置已完成（bench 已 redeploy 0.2.3+remote，`serialwrap --version` 對得上）；兩板 READY；無其他 pytest／wifi_llapi run。

- [ ] **Step 1: editable install**

```bash
cd ~/prj_arc/testpilot
uv pip install -e ~/prj_pri/serialwrap/.worktrees/reliability-plugin/reliability
# 期望：Successfully installed serialwrap-reliability-0.1.0
# 注意：merge 後改指主 checkout（uv pip install -e ~/prj_pri/serialwrap/reliability），
#       worktree 刪除前必須重裝，否則 entry point 指向失效路徑。
```

- [ ] **Step 2: list-plugins／list-cases**

```bash
uv run testpilot list-plugins
# 期望：表格含一列  serialwrap_reliability | v0.1.0 (36 cases)
uv run testpilot list-cases serialwrap_reliability | tail -5
uv run testpilot list-cases serialwrap_reliability | grep -c "p0-\|p1-\|rm-\|lr-"
# 期望：36 條（29 既有＋7 remote）；含 tier/destructive 資訊在 name/steps 欄可辨識
```

- [ ] **Step 3: run --case p0-doctor 冒煙（agent_trace/diagnostic_status/報表全鏈）**

```bash
uv run testpilot run serialwrap_reliability --case p0-doctor
# 期望 stdout：
#   [preflight] …（僅 git_behind 警告類，無 FAIL 項）
#   結尾印 build_reports payload：{'plugin': 'serialwrap_reliability', …
#    'diagnostic_counts': {'Pass': 1}, 'cases': 1, …}
RUN_DIR=$(ls -td ~/prj_arc/testpilot-core/plugins/serialwrap_reliability/reports/*/ | head -1)
jq '.final.diagnostic_status, .attempts | length' "$RUN_DIR/agent_trace/p0-doctor.json"
# 期望："Pass" 與 1（max_attempts=1，無 retry）
ls "$RUN_DIR"/report.md "$RUN_DIR"/report.json
ls -td ~/b-log/realhw-reports/tp-*/ | head -1   # realhw 慣有落點也有一份
# 期望：四個檔案都在；report.md 開頭「# realhw 實機穩定性報告」且版本行＝deployed 版本
```

> 已知副作用（契約 C14）：`testpilot run` 起跑時 SerialwrapBackend 會對 live daemon `wal reset` 一次（daemon 本身不會被停/重啟）。屬 lab 既有慣例；realhw case 的 WAL 斷言都是 case 內自取 seq 窗口，不依賴 run 前 WAL 內容。

---

### Task 10:【真機-人工閘】雙前端一致性（openspec 6.2）

- [ ] **Step 1: 產生非破壞性 case 清單**

```bash
cd ~/prj_pri/serialwrap/.worktrees/reliability-plugin
python3 -m realhw --list | awk '$1!="⚡" {print $2}' | tr -d '[]' > /tmp/nd-tiers.txt
python3 -m realhw --list | grep -v '⚡' | awk '{print $3}' | grep -E '^(p0|p1)-' > /tmp/nd-cases.txt
wc -l /tmp/nd-cases.txt   # 期望：20（P0×8＋P1 console×7＋cmd×3＋wal×2）
```

- [ ] **Step 2: standalone 跑 P0＋P1 非破壞性**

```bash
DSTR=$(python3 -m realhw --list | grep '⚡' | awk '{print $3}' | paste -sd,)
python3 -m realhw --tier p0,p1 --skip "$DSTR"
# 期望：exit 0；記下報告目錄 A（stdout 的 [realhw] 報告目錄：…）
```

- [ ] **Step 3: plugin 跑同一批**

```bash
cd ~/prj_arc/testpilot
uv run testpilot run serialwrap_reliability $(sed 's/^/--case /' /tmp/nd-cases.txt | tr '\n' ' ')
RUN_DIR=$(ls -td ~/prj_arc/testpilot-core/plugins/serialwrap_reliability/reports/*/ | head -1)
```

- [ ] **Step 4: 逐案 verdict 比對（兩邊 report.json 同 schema）**

```bash
jq -r '.results[] | "\(.id) \(.verdict)"' <報告目錄A>/report.json | sort > /tmp/side-a.txt
jq -r '.results[] | "\(.id) \(.verdict)"' "$RUN_DIR/report.json" | sort > /tmp/side-b.txt
diff /tmp/side-a.txt /tmp/side-b.txt && echo "CONSISTENT"
# 期望：無 diff、印 CONSISTENT。
# 不一致處置（openspec Requirement「雙前端一致性」）：先歸因（adapter bug vs 真機偶發：
# 看兩邊 evidence 與 WAL 時間窗），只對不一致 case 各重跑一次確認；歸因不出＝視為
# adapter 缺陷，回 Task 4/6 修——不得以「偶發」帶過。歸因紀錄附在 PR body。
```

---

### Task 11:【真機-人工閘】分類落桶三情境（openspec 6.3）

- [ ] **Step 1: FailEnv——停 docker 跑 rm-topo**

```bash
sudo systemctl stop docker
cd ~/prj_arc/testpilot
uv run testpilot run serialwrap_reliability --case rm-topo-direct
RUN_DIR=$(ls -td ~/prj_arc/testpilot-core/plugins/serialwrap_reliability/reports/*/ | head -1)
jq '.final.diagnostic_status, .failure_snapshot.reason_code' "$RUN_DIR/agent_trace/rm-topo-direct.json"
# 期望："FailEnv" 與 "docker_unavailable"（capabilities family-gate → 執行期 SKIP → FailEnv）
sudo systemctl start docker
```

- [ ] **Step 2: FailConfig——testbed 寫錯 serial**

```bash
# 暫改 reliability/serialwrap_reliability/testbed.yaml.example：DUT serial 改成 WRONGSER1
# （刻意用 serial 而非 busid：busid 隨插拔/換 hub 口會變動，不宜作 mismatch 測試欄位；
#   serial 為板卡固有識別——openspec tasks.md 6.3 已同步改為 serial）
uv run testpilot run serialwrap_reliability --case p0-doctor
RUN_DIR=$(ls -td ~/prj_arc/testpilot-core/plugins/serialwrap_reliability/reports/*/ | head -1)
jq '.final.diagnostic_status, .failure_snapshot.category' "$RUN_DIR/agent_trace/p0-doctor.json"
# 期望："FailConfig" 與 "configuration"（Phase 1 對 by-id/serial 不符標 testbed_board_mismatch）
# 驗完立刻 git checkout -- reliability/serialwrap_reliability/testbed.yaml.example 還原
```

- [ ] **Step 3: FailTest——真實產品面 FAIL**

```bash
# 先開一個外部 console 佔走 raw ownership（第一個 console 才拿 raw；case 內開的成第二個）
tmux new-session -d -s manual-hold "serialwrap-minicom COM0"; sleep 6
uv run testpilot run serialwrap_reliability --case p0-console-raw
RUN_DIR=$(ls -td ~/prj_arc/testpilot-core/plugins/serialwrap_reliability/reports/*/ | head -1)
jq '.final.diagnostic_status, .failure_snapshot.category' "$RUN_DIR/agent_trace/p0-console-raw.json"
# 期望："FailTest" 與 "test"（受測物反轉：case 內斷言失敗預設 test）
tmux kill-session -t manual-hold
# 收尾：確認兩板 READY（serialwrap session list）；trace 的 failure_snapshot.evidence 非空
```

---

### Task 12:【真機-人工閘】longrun 15m 短跑（openspec 6.4）

- [ ] **Step 1: 確認 testbed `longrun.duration: "15m"`（example 預設即是）後執行**

```bash
cd ~/prj_arc/testpilot
uv run testpilot run serialwrap_reliability --case lr-mixed
# 期間（另開 shell）驗步進模型：長跑進度監控來源＝realhw snapshots.ndjson（非 agent_trace）
LR_DIR=$(ls -td ~/b-log/realhw-reports/tp-*/ | head -1)/lr-mixed
watch -n 60 "wc -l $LR_DIR/snapshots.ndjson"
# 期望：每 300s 增 1 行、共約 3 行
```

- [ ] **Step 2: 收尾斷言**

```bash
RUN_DIR=$(ls -td ~/prj_arc/testpilot-core/plugins/serialwrap_reliability/reports/*/ | head -1)
jq '.final.diagnostic_status, (.attempts|length), (.attempts[0].commands|length)' \
   "$RUN_DIR/agent_trace/lr-mixed.json"
# 期望："Pass"、attempts==1（openspec Scenario「長跑不被 retry 重跑」）
ls "$LR_DIR/longrun-analysis.md" "$LR_DIR/events.ndjson" "$LR_DIR/snapshots.ndjson"
grep -m1 "daemon_death_at" "$LR_DIR/longrun-analysis.md"
# 期望：三檔皆在；daemon_death_at：None
```

---

### Task 13:【真機-人工閘】benchlock 拒跑實測（openspec 6.5）

- [ ] **Step 1: 佔住 benchlock 後啟動 plugin run**

```bash
flock ~/.local/state/serialwrap/bench.lock sleep 300 &
HOLD=$!
cd ~/prj_arc/testpilot
uv run testpilot run serialwrap_reliability --case p0-doctor; echo "rc=$?"
# 期望：stdout 有 [preflight] benchlock 缺項訊息、raise PreflightRefused（traceback 屬預期，
#       run_loop 不捕捉＝fail-fast）、rc 非 0；無新 reports 目錄、無任何 case 執行
kill $HOLD
```

- [ ] **Step 2: 模擬 wifi_llapi run 進行中（pgrep 面，依 Phase 1 偵測樣式）**

```bash
bash -c 'exec -a "testpilot run wifi_llapi" sleep 300' &
FAKE=$!
uv run testpilot run serialwrap_reliability --case p0-doctor; echo "rc=$?"
# 期望：同上被 suite-refuse 拒跑（訊息點名偵測到外部 testpilot run）
kill $FAKE
# 收尾複跑一次確認自癒：uv run testpilot run serialwrap_reliability --case p0-doctor → Pass
```

---

## Self-Review（plan 對 spec 覆蓋與矛盾清單）

**openspec `reliability-testpilot-plugin` Requirement → Task 對照：**

| Requirement / Scenario | 覆蓋 |
|---|---|
| plugin 註冊與 dev-only 安裝（entry point／deps／api_version 1.1／editable-only／`sys.path` import realhw） | Task 1（pyproject＋契約單測 Task 6 Step 1）、Task 2（REPO_ROOT/parents[2]）、Task 9 Step 1-2 |
| Scenario: testpilot 發現 plugin | Task 9 Step 2（list-plugins/list-cases 36 條） |
| Scenario: release wheel 不受影響 | Task 1 Step 1/4（before/after diff）＋Task 8 Step 3 複驗 |
| 生命週期映射（Thin Adapter；core.py 不 import testpilot；MUST NOT 用 testpilot transport） | Task 3/4（核心純函式）＋Task 6（glue）＋`test_core_modules_do_not_import_testpilot` |
| Scenario: 分類抄寫落桶（FailEnv＋trace 含 reason_code） | Task 4（`failure_payload` 單測）＋Task 11（三情境 bench） |
| Scenario: 破壞性 case 選擇期排除 | Task 3（`filter_for_run` 單測）＋Task 9（預設 run 不含 ⚡） |
| 組態來源與雙來源等價 | Task 5（合成 dict 等價單測）＋Task 6 Step 1（example↔config.json 真檔等價） |
| 執行策略與 longrun checkpoint 模型 | Task 6（agent-config 契約單測、execution_policy）＋Task 3/4（steps 合成、LongrunRunner）＋Task 12 |
| Scenario: 長跑不被 retry 重跑 | agent-config `max_attempts: 1`（單測釘住）＋Task 12 Step 2（attempts==1） |
| 報表與報告身分 | Task 7（reporter 單測：version/fw_ver/run_id 烙印）＋C16 hook＋Task 9 Step 3；run meta 含 deployed 版本＋repo HEAD sha＋板卡環境 metadata（prepare_run `boards`；板卡 fw 主動探測屬 Phase 1 preflight） |
| 雙前端一致性 | Task 10（含歸因程序） |

**Placeholder 掃描**：本 plan 所有程式碼區塊皆為完整可貼檔案／完整 append 片段；無 `...`、無 `TODO`、無「留待實作」。bench 命令的 `$RUN_DIR`/`$WHEELDIR` 等為 shell 變數（前一行有定義），非 placeholder。

**testpilot 契約 vs spec 的矛盾（實地考證發現，PR 時需同步修訂 openspec）：**

1. **remediation `enabled: false` 不可行（C6/C7）**——`RuntimeRemediationCoordinator.handle_on_failure` 在 disabled 時直接 return，failure_snapshot 永遠 None → `_classify_diagnostic_status` 對所有 FAIL 回 Inconclusive，「分類抄寫落桶」Scenario 必死。裁決：`enabled: true`＋防線鎖死動作——max_attempts=1 移除唯一執行點（on_retry 永不 dispatch）；不覆寫 decision hooks → **decision 恆 None＝唯一真正生效的阻擋點**；`allowed_actions: []` 僅宣示性（core 端空集合被 falsy 短路、不攔截，見 C10）。（openspec spec.md／design.md／權威設計 spec 及 tasks.md 5.4 的同款「remediation enabled: false」過時措辭，**已由主 session 同步修訂**為「decision 恆 None 為唯一生效阻擋點」語意；本 plan 對齊修訂後版本。）
2. **「hooks 最小集」必須含 `on_failure`/`pre_case`/`post_case`（C8）**——hooks 總閘不開，snapshot 同樣不落地；設計文件的「hooks 最小集」據此定義。
3. **「configs/testbed.yaml ← bench 事實源」的機械事實（C12）**——staging 每次 run 以 `<plugin_root>/testbed.yaml.example` 覆蓋 configs/testbed.yaml；可編輯正本是 example 檔（editable 佈局在 repo 內），改 staged 副本會被蓋掉。架構圖語意不變（testbed.yaml 仍是 runtime 讀的檔），但操作說明以本 plan 為準。
4. **execute_step 一律回 success=True（C3）**——引擎在 step 失敗時跳過 evaluate，`_last_failure` 將無人寫（→Inconclusive）；spec 的「evaluate 依 verdict 回布林並抄分類」隱含判決權在 evaluate，本 plan 把它明文化為 adapter 規則。
5. **`testpilot run` 起跑會對 live daemon `wal reset`（C14）**——spec 未提；屬 core 預設 run capture 行為（daemon 不會被停）。對 realhw case 無影響（case 內自取 seq 窗口），已記入 Task 9 注意事項。
6. **agent-config 缺 `retry.max_attempts` 時預設 2（C9）**——「重跑對真機危險」的保證必須靠顯式 `max_attempts: 1`，已由契約單測釘死。
