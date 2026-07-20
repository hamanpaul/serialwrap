# serialwrap reliability Phase 1（realhw 擴充）Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 完成 openspec change `serialwrap-reliability-plugin` 的 **Phase 1（tasks 群組 1-4＋收尾 7）**——realhw 套件 standalone 可交付擴充：`CaseResult` 分類欄（category/reason_code）＋既有 29 case 逐案標註、preflight 兩級判決（suite-refuse：benchlock；family-gate：capabilities）、`WinSwCli`＋hp-cycle Windows 端自動救援鏈、新 tier `remote` 7 case（rm-topo×4 包裝 docker harness＋rm-live×3 部署 daemon＋真板）。Phase 2（plugin 殼）不在本 plan，但本 plan 產出的介面（`load_cfg`／`REGISTRY`／`CaseResult`／capabilities 形狀）即 Phase 2 的消費面。

**Architecture:** 沿用 #122 架構——`realhw/` 獨立 stdlib-only harness（不入 wheel、不被 pytest 收集、禁 import sw_core、測部署後系統）。純邏輯（分類映射、版本比較、救援決策、verdict 分桶、state dir 解析）全部下沉為可單測純函式（`harness.py`／`drivers.py`／`preflight.py`），subprocess 執行層維持薄包裝。rm-topo 以 `$1` 逐拓樸分派參數 shell out 到 `tools/docker/remote_tunnel_test.sh`（包裝而非移植）；rm-live 以 docker 容器當 ssh 對端、對部署 daemon＋真板驗 `serialwrap remote`（PR #143）。權威設計＝`docs/superpowers/specs/2026-07-20-serialwrap-reliability-testpilot-plugin-design.md`；OpenSpec＝`openspec/changes/serialwrap-reliability-plugin/`。

**Tech Stack:** Python 3.10+ stdlib（dataclasses/subprocess/json/argparse/fcntl）、bash（remote_tunnel_test.sh 微改）、docker（remote 族）、tmux、usbipd-win、systemd（NOPASSWD sudo）、Windows 端 serialwrap.exe（經 `/mnt/c`）。

## Global Constraints

- **語言**：文件、註解、docstring、commit message 一律**繁體中文**（repo 語言政策）。
- **測試**：每個 code task 走 TDD（寫失敗測試→跑確認失敗→最小實作→跑過→commit）。收尾必跑 `python3 -m pytest -q tests/`——不得引入**新的**失敗；既知 pre-existing：`test_multiagent_e2e.py::test_five_agents_three_rounds_no_conflict`（TX count mismatch）、`t8_full_run_simulation`（~50% flaky）、`test_t1_wal_reset_preserves_console`（~1/5 flaky）、與其他 suite 並行時 PTY-heavy 6 檔競態。
- **policy**：收尾必跑 `python3 -m policy_check --repo .`（pinned SHA `ee87a6d5ed91209d944934a2559f4f2622fd1ac2`）；開 PR 前以 `--pr-title/--pr-body/--pr-base-ref/--pr-head-ref` 帶參複現 CI（本地不帶參是假綠）。R-09：production code 變更需 `changelog.d/*.md` fragment（Task 16）。
- **分支**：禁 commit `main`。本 plan 在 worktree `.worktrees/reliability-plugin`、分支 `feature/serialwrap-reliability-plugin`（開工前 `git branch --show-current` 確認）。
- **Commit**：Conventional Commits 繁中 subject＋trailer：
  ```
  Co-authored-by: Copilot <223556219+Copilot@users.noreply.github.com>
  ```
  （執行 agent 依其環境規範另附自身 Co-Authored-By trailer。）
- **R-21**：任何進 repo 的檔案不得含 `/home/<user>/` 絕對路徑字面值——一律 `~`／`Path.home()`（`/mnt/c/...` 非 home、可寫）。
- **realhw 契約**：不進 wheel（pyproject packages 不含 realhw）、不被 pytest 收集（測試全在 `tests/test_realhw_*.py`）、`realhw/` 內禁 `import sw_core`、stdlib-only。
- **【真機-人工閘】**：標此記號的步驟操作 live daemon／真板／Windows 端／docker，**實作 agent 只做到單測綠與程式就緒**；真機步驟由操作者執行並把結果記回 plan checkbox。跑真機前確認無其他 pytest／testpilot 在跑、兩板 READY。

## 介面總表（Phase 2 plugin 消費面——本 plan 的 Produces 契約）

| 介面 | 簽章／形狀 | 出處 |
|---|---|---|
| `realhw.load_cfg` | `load_cfg(config_path: Path \| None = None, *, injected: dict[str, Any] \| None = None) -> dict[str, Any]`——`injected` 非 None 即採用（Phase 2 testbed.yaml 合成 dict 注入）、否則讀 config.json；兩來源套相同預設正規化 | Task 3 |
| `harness.REGISTRY` | `list[Case]`；`Case(id, tier, title, run, destructive, requires, hints)`，tier ∈ p0/p1/remote/longrun | 既有＋Task 14/15 |
| `harness.CaseResult` | `CaseResult(verdict, reason="", evidence={}, duration_s=0.0, category="", reason_code="")`；category ∈ environment/session/configuration/test/空 | Task 1 |
| `harness.run_cases` | `run_cases(cases, ctx, *, boards: list[str], missing_caps: dict[str, str] \| None = None) -> list[tuple[str, CaseResult]]` | Task 1/5 |
| `preflight.Checks`／`evaluate` | `evaluate(c: Checks) -> tuple[bool, list[str]]`（suite-refuse 判定，純函式） | 既有＋Task 6/8 |
| `preflight.Capabilities` | frozen dataclass：`remote_capability: bool`、`deployed_version: str`、`docker: bool` | Task 4 |
| `preflight.missing_capabilities` | `missing_capabilities(caps, *, minimum=(0,2,3)) -> dict[str, str]`——缺項名（＝`Case.requires` 詞彙）→ reason_code；空 dict＝全滿足 | Task 4 |
| `preflight.collect_capabilities` | `collect_capabilities(sw) -> Capabilities`（I/O 收集層） | Task 4 |
| `preflight.acquire_benchlock` | `acquire_benchlock(lock_path: Path) -> int \| None`（fd 持有至行程結束） | Task 6 |
| `harness.write_reports` | report.json 每筆結果含 `category`/`reason_code` 鍵 | Task 1 |

## File Structure（本 plan 觸及）

| 檔案 | 動作 | 職責 |
|---|---|---|
| `realhw/harness.py` | Modify | `CaseResult` 增欄、報告分類欄、`run_cases` 兜底/family-gate、`load_cfg`、`Ctx.win` |
| `realhw/__init__.py` | Modify | re-export `load_cfg` |
| `realhw/config.json` | Modify | 增 `win_serialwrap_exe` 欄位 |
| `realhw/preflight.py` | Modify | capabilities（純判定＋收集）、benchlock、`Checks` 擴欄、windows_daemon 歸因 |
| `realhw/drivers.py` | Modify | `WinSwCli`、`parse_win_held`/`match_held_for_serial`、`plan_hp_rescue`、`classify_topology_run`、`remote_state_dir`、`Usbipd.attach` 回 rc |
| `realhw/cases/*.py`（既有 8 檔） | Modify | 逐案標註 category/reason_code（Task 2 對照表機械執行） |
| `realhw/cases/p1_hotplug.py` | Modify | p1-hp-cycle 接救援鏈 |
| `realhw/cases/remote.py` | Create | rm-topo×4＋rm-live×3 |
| `realhw/cases/__init__.py` | Modify | `from . import remote` |
| `realhw/__main__.py` | Modify | load_cfg／benchlock／capabilities／WinSwCli 接線、tier help |
| `tools/docker/remote_tunnel_test.sh` | Modify | `$1` 逐拓樸分派（微改） |
| `tests/test_realhw_harness.py` | Modify | 分類欄／report／family-gate／load_cfg 單測 |
| `tests/test_realhw_preflight.py` | Modify | capabilities／benchlock／windows_daemon 單測 |
| `tests/test_realhw_drivers.py` | Modify | WinSwCli 解析／救援決策／topo verdict／state dir 單測 |
| `docs/func-test/realhw-stability-checklist.md` | Modify | remote 族＋新 preflight＋config 欄位 |
| `README.md` | Modify | realhw 段補 `--tier remote`（中英兩段同步） |
| `changelog.d/reliability-phase1-realhw.md` | Create | R-09 fragment |

## 與現況／spec 對齊注記（實作前必讀）

1. **harness 容器前綴**：`remote_tunnel_test.sh` 實際容器名為 `sw-rt-*-${SUFFIX}`、network 為 `net_*_${SUFFIX}`（**非** swremote-）。rm-live 用 `rhwlive-` 前綴區隔。
2. **script `set -u`**：檔尾 `main` 呼叫必須改 `main "${1:-all}"`，否則無參數時 `$1` unbound 直接炸。
3. **lr-mixed 現況**：無 RSS 閾值 FAIL、無快照斷流偵測（spec §5 提及 `rss_leak`/`snapshot_gap` 為代表性 code）。本 plan 只對**既有 4 個失敗理由**標分類（`daemon_died`/`both_boards_stuck`/`board_stuck`/`cmd_error_rate_high`）；`rss_leak`/`snapshot_gap` 留給未來補該偵測時使用（不屬群組 1-4 範圍，勿順手加行為）。
4. **`win_serialwrap_exe` 實際路徑 bench 未知**：config 預設空字串（＝Windows 端不可用、救援鏈降級 no-op）；【真機-人工閘】前由操作者填實際 `/mnt/c/...` 路徑。
5. **舊部署無 `--version`**（#131 新增）：`serialwrap --version` 在 0.2.2 部署回 argparse error（rc=2、stdout 空）→ `parse_version(None)` → 判 `deployed_daemon_stale`，行為正確、免特判。
6. **`Checks` 為 frozen dataclass 且既有單測以全欄位建構**：新欄位一律帶預設值附加於**尾端**，不得插中間。
7. **rm-topo verdict 純函式放 `drivers.py`**（openspec tasks 4.2 指定測試檔為 `tests/test_realhw_drivers.py`，函式歸屬對齊）。

---

### Task 1: CaseResult 分類欄＋報告分類欄＋run_cases 兜底（TDD）

**Files:** Modify `realhw/harness.py`、`tests/test_realhw_harness.py`
**Interfaces:** Consumes—現有 `CaseResult`/`render_report_md`/`write_reports`/`run_cases`。Produces—`CaseResult(..., category="", reason_code="")`；report.md `| case | verdict | 分類 | 時間(s) | 說明 |`；report.json 每筆含 `category`/`reason_code`；`run_cases` 未捕捉例外＝FAIL＋空 category＋`uncaught_exception`、broken_by SKIP＝`environment`＋`broken_by:<id>`。

- [ ] **Step 1: RED——追加單測**（`tests/test_realhw_harness.py` 檔尾追加）：

```python
def test_case_result_classification_fields_default_empty():
    # 向後相容：不填 category/reason_code 合法且為空字串
    r = harness.CaseResult("PASS")
    assert r.category == "" and r.reason_code == ""
    r2 = harness.CaseResult("FAIL", reason="x", category="test", reason_code="cross_talk")
    assert (r2.category, r2.reason_code) == ("test", "cross_talk")


def test_report_shows_category_column_and_json_fields(tmp_path):
    results = [
        ("a", harness.CaseResult("PASS", duration_s=0.1)),
        ("b", harness.CaseResult("FAIL", reason="斷言不過", category="test",
                                 reason_code="cross_talk")),
        ("c", harness.CaseResult("SKIP", reason="缺 base64", category="environment",
                                 reason_code="base64_missing")),
    ]
    meta = {"version": "0.2.3", "git": "abc", "tiers": "p0", "started_at": "t"}
    md = harness.render_report_md(meta, results, {})
    assert "| 分類 |" in md                       # 表頭多分類欄
    assert "test/cross_talk" in md                 # FAIL 帶 category/reason_code
    assert "environment/base64_missing" in md      # SKIP 亦呈現分類
    harness.write_reports(tmp_path, meta, results, {})
    data = json.loads((tmp_path / "report.json").read_text())
    assert data["results"][1]["category"] == "test"
    assert data["results"][1]["reason_code"] == "cross_talk"
    assert data["results"][0]["category"] == ""    # PASS 空分類


def test_run_cases_uncaught_exception_is_inconclusive(tmp_path):
    def boom(ctx):
        raise RuntimeError("kaboom")
    cases = [harness.Case(id="x", tier="p0", title="x", run=boom)]
    ctx = harness.Ctx(cfg={}, report_dir=tmp_path, case_dir=tmp_path,
                      sw=None, tmux=None, usbipd=None, systemd=None)
    results = harness.run_cases(cases, ctx, boards=[])  # boards=[] 不觸恢復檢查
    (cid, r), = results
    assert cid == "x" and r.verdict == "FAIL"
    assert r.category == "" and r.reason_code == "uncaught_exception"  # 誠實 Inconclusive
```

- [ ] **Step 2: 跑測試確認失敗**：

```bash
# （於本 worktree 根目錄執行）
python3 -m pytest -q tests/test_realhw_harness.py
```

預期：3 failed（`TypeError: unexpected keyword argument 'category'` 之類）, 11 passed。

- [ ] **Step 3: GREEN——`realhw/harness.py` 改 4 處**。

(3a) `CaseResult` 全段替換：

```python
@dataclasses.dataclass
class CaseResult:
    verdict: str  # PASS | FAIL | SKIP
    reason: str = ""
    evidence: dict[str, str] = dataclasses.field(default_factory=dict)
    duration_s: float = 0.0
    # 分類契約（reliability plugin spec §5，向後相容：預設空）：
    # category ∈ environment | session | configuration | test | 空（空＝Inconclusive）
    # reason_code＝自由字串，進 trace 供診斷分桶，不影響 verdict
    category: str = ""
    reason_code: str = ""
```

(3b) `render_report_md` 全函式替換：

```python
def render_report_md(meta: dict[str, Any], results: list[tuple[str, CaseResult]],
                     hints: dict[str, tuple[str, ...]]) -> str:
    counts = {"PASS": 0, "FAIL": 0, "SKIP": 0}
    for _, r in results:
        counts[r.verdict] = counts.get(r.verdict, 0) + 1
    lines = [
        "# realhw 實機穩定性報告",
        "",
        f"- 版本：{meta.get('version')}（git {meta.get('git')}）",
        f"- tiers：{meta.get('tiers')}；開始：{meta.get('started_at')}",
        f"- 結果：PASS: {counts['PASS']}／FAIL: {counts['FAIL']}／SKIP: {counts['SKIP']}",
        "",
        "| case | verdict | 分類 | 時間(s) | 說明 |",
        "|---|---|---|---|---|",
    ]
    for cid, r in results:
        cat = r.category or "-"
        if r.reason_code:
            cat = f"{cat}/{r.reason_code}"
        lines.append(f"| {cid} | {r.verdict} | {cat} | {r.duration_s:.1f} | {r.reason} |")
    fails = [(cid, r) for cid, r in results if r.verdict == "FAIL"]
    if fails:
        lines += ["", "## 失敗案例"]
        for cid, r in fails:
            lines += ["", f"### {cid}", f"- 原因：{r.reason}"]
            if r.category or r.reason_code:
                lines.append(f"- 分類：{r.category or '(空＝Inconclusive)'}／{r.reason_code or '-'}")
            for h in hints.get(cid, ()):
                lines.append(f"- 提示：{h}")
            for k, v in r.evidence.items():
                lines.append(f"- evidence：[{k}]({v})")
    return "\n".join(lines) + "\n"
```

(3c) `write_reports` 的 payload 替換：

```python
def write_reports(report_dir: Path, meta: dict[str, Any], results: list[tuple[str, CaseResult]],
                  hints: dict[str, tuple[str, ...]]) -> None:
    report_dir.mkdir(parents=True, exist_ok=True)
    payload = {"meta": meta, "results": [
        {"id": cid, "verdict": r.verdict, "reason": r.reason,
         "category": r.category, "reason_code": r.reason_code,
         "duration_s": r.duration_s, "evidence": r.evidence} for cid, r in results]}
    (report_dir / "report.json").write_text(
        json.dumps(payload, ensure_ascii=False, indent=1), encoding="utf-8")
    (report_dir / "report.md").write_text(render_report_md(meta, results, hints), encoding="utf-8")
```

(3d) `run_cases` 兩處（broken_by SKIP＋except 兜底）改為：

```python
        if broken_by and ("two_boards" in case.requires or case.destructive):
            results.append((case.id, CaseResult(
                "SKIP", reason=f"前置不滿足（{broken_by} 後板卡未恢復）",
                category="environment", reason_code=f"broken_by:{broken_by}")))
            continue
```

```python
        except Exception as exc:  # case 內未捕捉例外＝FAIL（Inconclusive：空 category），不中止套件
            r = CaseResult("FAIL", reason=f"未捕捉例外：{exc!r}",
                           category="", reason_code="uncaught_exception")
```

- [ ] **Step 4: 跑過**：

```bash
python3 -m pytest -q tests/test_realhw_harness.py
```

預期：`14 passed`（11 原有＋3 新增）。

- [ ] **Step 5: commit**：

```bash
git add realhw/harness.py tests/test_realhw_harness.py
git commit -m "feat(realhw): CaseResult 增 category/reason_code 分類欄與報告呈現

reliability plugin Phase 1a：分類契約（environment|session|configuration|test|空=Inconclusive）；
report.md 增分類欄、report.json 增欄位；run_cases 未捕捉例外兜底=空 category+uncaught_exception、
broken_by SKIP=environment+broken_by:<id>。向後相容（不填合法）。

Co-authored-by: Copilot <223556219+Copilot@users.noreply.github.com>"
```

---
### Task 2: 既有 29 case 逐案標註（照對照表機械執行）

**Files:** Modify `realhw/cases/p0.py`、`p1_console.py`、`p1_cmd.py`、`p1_wal.py`、`p1_restart.py`、`p1_handoff.py`、`p1_hotplug.py`、`longrun.py`
**Interfaces:** Consumes—Task 1 的 `CaseResult` 新欄。Produces—29 case 每個 FAIL/SKIP return 點帶 `category=`/`reason_code=`。

**裁決線（spec §5，唯一準則，逐案不猶豫）**：case 內斷言失敗**預設 `category="test"`**（板卡健康由 preflight＋case 間恢復保證）；case 明確偵測之**外因**（工具/系統/板卡噪音/外部持有）＝`environment`；**bench 組態錯**＝`configuration`；執行期 SKIP＝`environment`。PASS return 一律不動。

- [ ] **Step 1: 照下表機械執行**——每列對應一個 `return CaseResult("FAIL"/"SKIP", ...)`，在該呼叫**追加** `category="..."`、`reason_code="..."` 兩個 keyword 引數（表列順序＝各檔案內出現順序，可由 reason 字串唯一定位）：

**`realhw/cases/p0.py`**

| case id | return 點（reason 片段） | category | reason_code |
|---|---|---|---|
| p0-doctor | `doctor 未過：` | test | doctor_not_green |
| p0-doctor | `非 READY（` | test | board_not_ready |
| p0-doctor | `by-id 不含預期 serial` | configuration | testbed_board_mismatch |
| p0-cmd-async | `stdout 未含 marker` | test | cmd_marker_missing |
| p0-console-raw | `未拿到 raw interactive ownership` | test | raw_ownership_not_granted |
| p0-console-raw | `Tab 補完未出現` | test | raw_fallback_linebuffer |
| p0-clear-reattach | `clear 後未在時限內回 READY` | test | reattach_timeout |
| p0-selftest | `classification=` | test | selftest_not_ok |
| p0-blog-clean | `未產生新的 mini_COM0_*.log` | test | blog_capture_missing |
| p0-blog-clean | `capture 含 transcript 標頭或 ANSI` | test | blog_ansi_regression |
| p0-wal-live | `mirror mtime 未跳動` | test | wal_mirror_stale |
| p0-wal-live | `未見命令 marker` | test | wal_marker_missing |
| p0-multiopen | `multi_open=` | environment | foreign_tty_holder |

（p0-multiopen 判 environment：外來 tty 持有者／第二 daemon 是本機環境干擾，非受測 daemon 行為缺陷。）

**`realhw/cases/p1_console.py`**

| case id | return 點（reason 片段） | category | reason_code |
|---|---|---|---|
| p1-con-fanout | `console 畫面缺 marker` | test | console_fanout_lost |
| p1-con-defer | `submit 未回 cmd_id` | test | submit_no_cmd_id |
| p1-con-defer | `agent 命令未乾淨完成` | test | defer_interleaved |
| p1-con-defer | `疑似被 human console 阻擋` | test | defer_agent_blocked |
| p1-con-defer | `deferred human 輸入未 flush` | test | defer_flush_lost |
| p1-con-busy | `human_active 窗內 agent 竟奪權` | test | busy_gate_bypassed |
| p1-con-softpreempt | `idle human lease 未被軟奪` | test | soft_preempt_denied |
| p1-con-softpreempt | `close 後原 human console owner 未恢復` | test | owner_not_restored |
| p1-con-liveness | `未偵測到本套件新起的 minicom PID` | environment | minicom_spawn_failed |
| p1-con-liveness | `human_attached 未轉 false` | test | orphan_not_recycled |
| p1-con-orphan | `孤兒未回收` | test | orphan_not_recycled |
| p1-con-orphan | `未拿回 raw interactive ownership` | test | raw_ownership_not_granted |
| p1-con-orphan | `Tab 補完未出現` | test | raw_fallback_linebuffer |
| p1-con-second | `interactive_owner 數應為 1` | test | owner_count_mismatch |
| p1-con-second | `第二 console 未建立` | test | second_console_missing |

**`realhw/cases/p1_cmd.py`**

| case id | return 點（reason 片段） | category | reason_code |
|---|---|---|---|
| p1-cmd-modes | `line 模式 stdout 未含 marker` | test | cmd_marker_missing |
| p1-cmd-modes | `background submit 未回 cmd_id` | test | submit_no_cmd_id |
| p1-cmd-modes | `background 輸出缺 marker` | test | background_output_missing |
| p1-cmd-modes | `interactive-open 未回 interactive_id` | test | interactive_open_failed |
| p1-cmd-modes | `interactive 畫面未見 IA_OK` | test | interactive_echo_missing |
| p1-cmd-modes | `未回 SESSION_NOT_FOUND` | test | error_code_contract |
| p1-cmd-serial | `未完成（status=` | test | serial_cmd_incomplete |
| p1-cmd-serial | `cross-talk 混入` | test | cross_talk |
| p1-cmd-serial | `WAL TX 計數` | test | wal_tx_count_mismatch |
| p1-cmd-file | SKIP `target 缺 base64` | environment | base64_missing |
| p1-cmd-file | `file push 失敗` | test | file_push_failed |
| p1-cmd-file | `pull 檔案讀取失敗` | test | file_pull_read_failed |
| p1-cmd-file | `round-trip md5 不符` | test | file_md5_mismatch |
| p1-cmd-file | `疑 event loop 凍結` | test | rpc_freeze |

**`realhw/cases/p1_wal.py`**

| case id | return 點（reason 片段） | category | reason_code |
|---|---|---|---|
| p1-wal-reset | `console-attach 未回 vtty/client_id` | test | console_attach_failed |
| p1-wal-reset | `reset 後 current-seq 非 0` | test | wal_reset_seq_nonzero |
| p1-wal-reset | `原 console client 掉線` | test | console_dropped |
| p1-wal-reset | `console 未見 reset 後命令 marker` | test | console_fanout_lost |
| p1-wal-reset | `未重新累積 seq` | test | wal_seq_not_accumulating |
| p1-wal-reset | `與 WAL 檔尾 seq` | test | wal_integrity |
| p1-wal-fullrun | `console-attach 未回 vtty/client_id` | test | console_attach_failed |
| p1-wal-fullrun | `seq 未嚴格遞增` | test | wal_seq_not_increasing |
| p1-wal-fullrun | `wal export 無記錄` | test | wal_export_empty |
| p1-wal-fullrun | `console client 掉線` | test | console_dropped |
| p1-wal-fullrun | `console 缺 marker` | test | console_fanout_lost |

**`realhw/cases/p1_restart.py`**

| case id | return 點（reason 片段） | category | reason_code |
|---|---|---|---|
| p1-rst-daemon | SKIP `板不安靜` | environment | board_noisy |
| p1-rst-daemon | `systemctl restart 回 rc=` | environment | systemd_restart_failed |
| p1-rst-daemon | `restart 後未回 READY` | test | restart_ready_timeout |
| p1-rst-daemon | `MainPID 未變更` | environment | systemd_restart_ineffective |
| p1-rst-daemon | `by-id 對調` | test | com_rank_flipped |
| p1-rst-daemon | `profile 漂移` | test | profile_drift |
| p1-rst-reboot | `reboot 後未在時限內自動回 READY` | test | reboot_ready_timeout |
| p1-rst-reboot | `console client 未存活` | test | console_dropped |
| p1-rst-reboot | `WAL 未跨 reboot 連續記錄` | test | wal_not_continuous |
| p1-rst-bootwindow | `開機窗 attach 後未在時限內自動回 READY` | test | bootwindow_ready_timeout |
| p1-rst-recover | `recover 後 self-test 未 OK` | test | recover_selftest_failed |

（`systemctl restart` 失敗／MainPID 未變＝sudo NOPASSWD／systemd 環境問題，非受測 daemon 行為 → environment。）

**`realhw/cases/p1_handoff.py`**

| case id | return 點（reason 片段） | category | reason_code |
|---|---|---|---|
| p1-ho-cycle | `COM1 無 attached_real_path` | test | attached_path_missing |
| p1-ho-cycle | `release 後 COM1 非 RELEASED` | test | release_state_wrong |
| p1-ho-cycle | `未回 DEVICE_STILL_HELD` | test | still_held_not_detected |
| p1-ho-cycle | `kill 外部 minicom 後 attach 未回 READY` | test | reclaim_ready_timeout |
| p1-ho-cycle | `COM0（prpl）被擾動` | test | bystander_disturbed |
| p1-ho-persist | `release 後 COM1 非 RELEASED` | test | release_state_wrong |
| p1-ho-persist | `systemctl restart 回 rc=` | environment | systemd_restart_failed |
| p1-ho-persist | `restart 後 COM0 未回 READY` | test | restart_ready_timeout |
| p1-ho-persist | `未保持 RELEASED` | test | released_not_persisted |
| p1-ho-persist | `attach 後 COM1 未回 READY` | test | reclaim_ready_timeout |

**`realhw/cases/p1_hotplug.py`**（Task 11 會改寫 p1-hp-cycle，本 task 仍先標註——Task 11 的完整檔案已含這些標註，屆時直接覆蓋不衝突）

| case id | return 點（reason 片段） | category | reason_code |
|---|---|---|---|
| p1-hp-cycle | SKIP `不在 usbipd list（換線？）` | environment | busid_missing |
| p1-hp-cycle | `detach 後 COM1 未在 30s 內離開 READY` | test | hotunplug_not_detected |
| p1-hp-cycle | `擾動了 COM0` | test | bystander_disturbed |
| p1-hp-cycle | `回插後 COM1 未自動回 READY` | test | replug_ready_timeout |
| p1-hp-cycle | `未回原 COM` | test | com_rank_flipped |
| p1-hp-reorder | SKIP `busid 不在 usbipd list` | environment | busid_missing |
| p1-hp-reorder | `反序回插後` | test | replug_ready_timeout |
| p1-hp-reorder | `COM↔板對調` | test | com_rank_flipped |
| p1-hp-reorder | `systemctl restart 回 rc=` | environment | systemd_restart_failed |
| p1-hp-reorder | `restart 後` + `未回 READY` | test | restart_ready_timeout |
| p1-hp-reorder | `#100 rank 退化` | test | com_rank_flipped |

- [ ] **Step 2: `realhw/cases/longrun.py` lr-mixed 收尾段**（非機械、唯一需要邏輯的檔）——把檔尾 `verdict = "FAIL" if reasons else "PASS"` 起的 return 段替換為：

```python
    # 分類（spec §5 lr-mixed）：依嚴重度優先序取單一 reason_code；乾淨跑完＝空分類 PASS。
    # 注：rss_leak/snapshot_gap 需 RSS 閾值/斷流偵測，現況未實作、不在此標（見 plan 對齊注記 3）。
    reason_code = ""
    if analysis["daemon_death_at"] is not None or major.get("kind") == "daemon_death":
        reason_code = "daemon_died"
    elif major.get("kind") == "both_boards_stuck":
        reason_code = "both_boards_stuck"
    elif long_stuck:
        reason_code = "board_stuck"
    elif reasons:
        reason_code = "cmd_error_rate_high"

    verdict = "FAIL" if reasons else "PASS"
    evidence = {"analysis": _rel(analysis_path), "events": _rel(events_path),
                "snapshots": _rel(snaps_path)}
    return CaseResult(verdict, reason="；".join(reasons) or "長跑完成、無重大事件",
                      category="test" if reasons else "", reason_code=reason_code,
                      evidence=evidence)
```

- [ ] **Step 3: 覆蓋驗證**（每個 FAIL/SKIP return 都已標註——PASS 與 `_error` dict return 不算）：

```bash
# 未標註的 FAIL/SKIP CaseResult 應為 0 行（多行 return 用 grep -A2 人工複查）
grep -rn 'CaseResult("FAIL"' realhw/cases/ | grep -v "category=" | grep -v "harness.py"
grep -rn 'CaseResult("SKIP"' realhw/cases/ | grep -v "category="
python3 -m pytest -q tests/test_realhw_harness.py tests/test_realhw_drivers.py tests/test_realhw_preflight.py
python3 -c "import realhw.cases; from realhw import harness; print(len(harness.REGISTRY))"
```

預期：兩個 grep 空輸出。注意：`category=` 多在**下一行**（多行呼叫），grep 會**誤報未標註**——命中行以 `grep -A2` 展開複查，並就上表逐列人工勾稽一次為準；pytest `22 passed`；REGISTRY `29`。

- [ ] **Step 4: commit**：

```bash
git add realhw/cases/
git commit -m "feat(realhw): 既有 29 case 逐案標註 category/reason_code（分類裁決線）

斷言失敗預設 test；明確外因（工具/系統/噪音/外部持有）=environment；bench 組態錯=configuration；
執行期 SKIP=environment；lr-mixed 依嚴重度取單一 reason_code。

Co-authored-by: Copilot <223556219+Copilot@users.noreply.github.com>"
```

---

### Task 3: `load_cfg` 雙來源 loader＋config 增 `win_serialwrap_exe`（TDD）

**Files:** Modify `realhw/harness.py`、`realhw/__init__.py`、`realhw/config.json`、`tests/test_realhw_harness.py`
**Interfaces:** Produces—`realhw.load_cfg(config_path=None, *, injected=None) -> dict`（Phase 2 plugin 以 testbed.yaml 合成 dict 走 `injected`；雙來源等價由相同正規化保證）；config 新欄 `win_serialwrap_exe`（空字串＝Windows 端不可用）。

- [ ] **Step 1: RED**（`tests/test_realhw_harness.py` 追加）：

```python
def test_load_cfg_reads_config_json_with_defaults(tmp_path):
    p = tmp_path / "config.json"
    p.write_text('{"boards": [], "usbipd_exe": "/x", "tmux_prefix": "t"}', encoding="utf-8")
    cfg = harness.load_cfg(p)
    assert cfg["usbipd_exe"] == "/x"
    assert cfg["win_serialwrap_exe"] == ""   # 預設正規化：缺欄補空字串


def test_load_cfg_injected_dict_equivalent(tmp_path):
    # 雙來源等價：同一 bench 事實，檔案來源與注入來源產出相同 cfg（Phase 2 testbed 契約）
    facts = {"boards": [{"com": "COM0", "serial": "S1", "busid": "1-1"}],
             "usbipd_exe": "/x", "tmux_prefix": "t"}
    p = tmp_path / "config.json"
    p.write_text(json.dumps(facts), encoding="utf-8")
    assert harness.load_cfg(p) == harness.load_cfg(injected=facts)
    assert harness.load_cfg(injected=facts)["win_serialwrap_exe"] == ""
    # injected 不被就地污染（淺複製）
    assert "win_serialwrap_exe" not in facts
```

- [ ] **Step 2: 跑確認失敗**：`python3 -m pytest -q tests/test_realhw_harness.py` → 預期 2 failed（`AttributeError: ... 'load_cfg'`）, 14 passed。

- [ ] **Step 3: GREEN——`realhw/harness.py` 檔尾（`recovery_command` 之前任意處）加**：

```python
# 組態預設正規化（雙來源共用；load_cfg 唯一入口）
_CFG_DEFAULTS: dict[str, Any] = {
    "win_serialwrap_exe": "",   # Windows 端 serialwrap.exe（/mnt/c 路徑）；空＝不可用
    "tmux_prefix": "realhw",
}


def load_cfg(config_path: Path | None = None, *,
             injected: dict[str, Any] | None = None) -> dict[str, Any]:
    """組態單一 loader、雙來源（reliability plugin 介面，spec 決策 1）。

    - ``injected`` 非 None → 淺複製後採用、完全不讀檔——Phase 2 plugin 以
      testbed.yaml 合成 dict 注入；雙來源等價性由「同走本函式正規化」保證。
    - 否則讀 ``config_path``（預設 ``realhw/config.json``；stdlib json，維持
      realhw stdlib-only 契約）。
    """
    if injected is not None:
        cfg: dict[str, Any] = dict(injected)
    else:
        path = config_path or Path(__file__).parent / "config.json"
        cfg = json.loads(path.read_text(encoding="utf-8"))
    for key, val in _CFG_DEFAULTS.items():
        cfg.setdefault(key, val)
    return cfg
```

`realhw/__init__.py` 全檔替換：

```python
"""#122 實機穩定性套件——測部署後系統；禁 import sw_core。"""
from __future__ import annotations

from .harness import load_cfg  # noqa: F401  # Phase 2 plugin 消費面（realhw.load_cfg）
```

`realhw/config.json` 全檔替換（`_readme` 增一行＋新欄；boards/timeouts/longrun 原值不動）：

```json
{
  "_readme": [
    "本機組態（#122）——機器特定值；R-21：勿寫絕對 home 路徑（一律 ~ 或 Path.home()）。",
    "harness 主要用 com/serial/busid；platform/profile 僅供 case 判讀。",
    "busid 換線會變：每輪跑前 usbipd list 驗證存在（config 只存 serial→期望 COM 映射）。",
    "COM1 現為 brcm/BDK（曾為 prpl，alias 沿用歷史命名）。",
    "本檔為 stdlib json（非 YAML）——維持 realhw stdlib-only 契約，勿引入第三方解析器。",
    "win_serialwrap_exe：Windows 端 serialwrap.exe 的 /mnt/c 路徑（0718 雙 daemon 救援與 windows_daemon 診斷用）；空字串＝Windows 端不可用（探測/救援降級 no-op），真機跑 hp 救援前由操作者填實際路徑。"
  ],
  "boards": [
    {"com": "COM0", "alias": "dut-prpl", "serial": "AC01QZT0", "busid": "8-1", "platform": "prpl"},
    {"com": "COM1", "alias": "sta-prpl", "serial": "AQ00OAQ7", "busid": "8-2", "platform": "brcm", "profile": "brcm-template"}
  ],
  "usbipd_exe": "/mnt/c/Program Files/usbipd-win/usbipd.exe",
  "win_serialwrap_exe": "",
  "tmux_prefix": "realhw",
  "timeouts": {"ready_wait_s": 180, "reboot_wait_s": 300, "human_active_window_s": 60},
  "longrun": {"snapshot_interval_s": 300, "agent_workers": 4}
}
```

- [ ] **Step 4: 跑過**：`python3 -m pytest -q tests/test_realhw_harness.py` → 預期 `16 passed`。
- [ ] **Step 5: commit**（`feat(realhw): load_cfg 雙來源 loader＋config 增 win_serialwrap_exe 欄位`＋trailer）。

---
### Task 4: capabilities 純判定＋收集（family-gate，TDD）

**Files:** Modify `realhw/preflight.py`、`tests/test_realhw_preflight.py`
**Interfaces:** Produces—`Capabilities(remote_capability, deployed_version, docker)`（frozen）、`parse_version(text) -> tuple[int,int,int] | None`、`missing_capabilities(caps, *, minimum=(0,2,3)) -> dict[str,str]`（鍵＝`Case.requires` 詞彙：`remote_capability`/`deployed_recent`/`docker`；值＝reason_code）、`collect_capabilities(sw) -> Capabilities`。

- [ ] **Step 1: RED**（`tests/test_realhw_preflight.py` 追加）：

```python
def test_parse_version():
    assert preflight.parse_version("serialwrap 0.2.3") == (0, 2, 3)
    assert preflight.parse_version("0.10.1") == (0, 10, 1)
    assert preflight.parse_version("") is None
    assert preflight.parse_version("usage: serialwrap ...") is None  # 舊部署無 --version


def test_missing_capabilities_all_present():
    caps = preflight.Capabilities(remote_capability=True,
                                  deployed_version="serialwrap 0.2.3", docker=True)
    assert preflight.missing_capabilities(caps) == {}


def test_missing_capabilities_maps_reason_codes():
    caps = preflight.Capabilities(remote_capability=False,
                                  deployed_version="serialwrap 0.2.2", docker=False)
    got = preflight.missing_capabilities(caps)
    assert got == {
        "remote_capability": "remote_capability_missing",
        "deployed_recent": "deployed_daemon_stale",
        "docker": "docker_unavailable",
    }


def test_missing_capabilities_unparseable_version_is_stale():
    caps = preflight.Capabilities(remote_capability=True, deployed_version="", docker=True)
    assert preflight.missing_capabilities(caps) == {"deployed_recent": "deployed_daemon_stale"}
```

- [ ] **Step 2: 跑確認失敗**：`python3 -m pytest -q tests/test_realhw_preflight.py` → 預期 4 failed, 5 passed。

- [ ] **Step 3: GREEN——`realhw/preflight.py`**。頂部 import 增 `import re`、`import shutil`（shutil 已有）。`Checks` 定義之後加：

```python
# ── family-gate 能力（capabilities）：缺項不擋整場，只讓宣告 requires 的 case 執行期 SKIP ──

_VERSION_RE = re.compile(r"(\d+)\.(\d+)\.(\d+)")
MIN_DEPLOYED: tuple[int, int, int] = (0, 2, 3)  # #130 autoboot guard／remote 所在版本


def parse_version(text: str) -> tuple[int, int, int] | None:
    """從 `serialwrap 0.2.3` 之類字串解析 (0,2,3)；解析不到回 None（含舊部署無 --version）。"""
    m = _VERSION_RE.search(text or "")
    if not m:
        return None
    return (int(m.group(1)), int(m.group(2)), int(m.group(3)))


@dataclasses.dataclass(frozen=True)
class Capabilities:
    remote_capability: bool   # 部署 CLI 有 `remote` 子命令
    deployed_version: str     # `serialwrap --version` 原始字串
    docker: bool              # docker CLI＋daemon 可達


def missing_capabilities(caps: Capabilities, *,
                         minimum: tuple[int, int, int] = MIN_DEPLOYED) -> dict[str, str]:
    """缺項 → reason_code（純判定）。鍵對齊 Case.requires 詞彙；空 dict＝全滿足。"""
    missing: dict[str, str] = {}
    if not caps.remote_capability:
        missing["remote_capability"] = "remote_capability_missing"
    ver = parse_version(caps.deployed_version)
    if ver is None or ver < minimum:
        missing["deployed_recent"] = "deployed_daemon_stale"
    if not caps.docker:
        missing["docker"] = "docker_unavailable"
    return missing


def collect_capabilities(sw) -> Capabilities:
    """I/O 收集層（判定在 missing_capabilities，單測釘住）。

    - remote_capability：`serialwrap remote status` 回 ok:true（無此子命令時 argparse
      rc=2、stdout 空 → ok 缺 → False）。status 為唯讀且會順手 prune 死 state，無副作用風險。
    - deployed_version：`serialwrap --version`（#131 新增；舊部署回 argparse error → 空字串）。
    - docker：CLI 存在且 `docker info` rc==0（daemon 可達）。image 建置延遲到第一個
      rm-topo case（spec §6），此處不 build。
    """
    remote_ok = bool(sw.run("remote", "status").get("ok"))
    version = sw.run("--version").get("_raw", "")
    docker_ok = False
    if shutil.which("docker"):
        try:
            docker_ok = _run(["docker", "info"], timeout=20).returncode == 0
        except (subprocess.SubprocessError, OSError):
            docker_ok = False
    return Capabilities(remote_capability=remote_ok, deployed_version=version, docker=docker_ok)
```

- [ ] **Step 4: 跑過**：`python3 -m pytest -q tests/test_realhw_preflight.py` → 預期 `9 passed`。
- [ ] **Step 5: commit**（`feat(realhw): preflight capabilities family-gate（版本比較/remote 探測/docker 可達）`＋trailer）。

---

### Task 5: `run_cases` family-gate 執行期 SKIP 接線（TDD）

**Files:** Modify `realhw/harness.py`、`tests/test_realhw_harness.py`
**Interfaces:** Produces—`run_cases(cases, ctx, *, boards, missing_caps: dict[str, str] | None = None)`；case.requires 命中 missing_caps 鍵 → SKIP＝`environment`＋該 reason_code（spec：執行期 SKIP 誠實入帳，選擇期排除是 Phase 2 plugin 的事）。

- [ ] **Step 1: RED**（`tests/test_realhw_harness.py` 追加）：

```python
def test_run_cases_family_gate_runtime_skip(tmp_path):
    ran = []
    def ok_run(ctx):
        ran.append(1)
        return harness.CaseResult("PASS")
    cases = [
        harness.Case(id="rm-x", tier="remote", title="x", run=ok_run,
                     requires=("docker", "remote_capability")),
        harness.Case(id="p0-y", tier="p0", title="y", run=ok_run, requires=("two_boards",)),
    ]
    ctx = harness.Ctx(cfg={}, report_dir=tmp_path, case_dir=tmp_path,
                      sw=None, tmux=None, usbipd=None, systemd=None)
    results = harness.run_cases(cases, ctx, boards=[],
                                missing_caps={"docker": "docker_unavailable"})
    assert results[0][1].verdict == "SKIP"
    assert results[0][1].category == "environment"
    assert results[0][1].reason_code == "docker_unavailable"
    # 未宣告缺項能力的 case 照常執行（two_boards 屬 suite-refuse 詞彙、不受 family-gate 管）
    assert results[1][1].verdict == "PASS" and ran == [1]
```

- [ ] **Step 2: 跑確認失敗** → 預期 1 failed（`unexpected keyword argument 'missing_caps'`）, 16 passed。

- [ ] **Step 3: GREEN——`run_cases` 簽章與迴圈頭替換**（broken_by 判斷之後、`t0 =` 之前插入 family-gate 段）：

```python
def run_cases(cases: list[Case], ctx: Ctx, *, boards: list[str],
              missing_caps: dict[str, str] | None = None) -> list[tuple[str, CaseResult]]:
    results: list[tuple[str, CaseResult]] = []
    broken_by: str | None = None
    caps_missing = missing_caps or {}
    for case in cases:
        ctx.case_dir = ctx.report_dir / case.id
        if broken_by and ("two_boards" in case.requires or case.destructive):
            results.append((case.id, CaseResult(
                "SKIP", reason=f"前置不滿足（{broken_by} 後板卡未恢復）",
                category="environment", reason_code=f"broken_by:{broken_by}")))
            continue
        lacking = [req for req in case.requires if req in caps_missing]
        if lacking:
            results.append((case.id, CaseResult(
                "SKIP",
                reason=f"能力缺項（family-gate）：{','.join(caps_missing[r] for r in lacking)}",
                category="environment", reason_code=caps_missing[lacking[0]])))
            continue
        t0 = time.monotonic()
```

（`t0 = time.monotonic()` 起之後的內容不動。）

- [ ] **Step 4: rst-reboot／bootwindow 掛上 `deployed_recent`**（spec 案例修訂表：部署 <0.2.3 時 SKIP＝FailEnv/deployed_daemon_stale，case 本體零改動、只改 requires）。`realhw/cases/p1_restart.py` 兩處 decorator 的 `requires` 替換：

```python
@_case("p1-rst-reboot", "target reboot 後自動恢復 READY＋console 存活＋WAL 連續",
       hints=("reboot status 可能 timeout（prplOS 回 prompt 後才斷）——容忍",
              "console client 應跨 reboot 存活（daemon 保 bridge，走 RECOVERING）"),
       requires=("two_boards", "deployed_recent"))
```

```python
@_case("p1-rst-bootwindow", "開機窗 clear+attach 不卡死、最終自動 READY（#69/#94）",
       hints=("attach 回應非致命 error_code 或 ok 皆可；降級斷言＝最終自動 READY 即 PASS",
              "reprobe_attempts 實況記進 evidence"),
       requires=("two_boards", "deployed_recent"))
```

（`p1-rst-daemon`／`p1-rst-recover` 不掛——與板卡 autoboot guard（#130）無關。`two_boards`/`tmux` 屬 suite-refuse 詞彙、不在 missing_caps 鍵域，family-gate 不會誤殺。）

- [ ] **Step 5: 跑過**：`python3 -m pytest -q tests/test_realhw_harness.py` → 預期 `17 passed`；`python3 -c "import realhw.cases; from realhw import harness; print([c.requires for c in harness.REGISTRY if c.id in ('p1-rst-reboot','p1-rst-bootwindow')])"` → 兩組皆含 `deployed_recent`。
- [ ] **Step 6: commit**（`feat(realhw): run_cases 接 family-gate——requires 缺項執行期 SKIP=FailEnv；rst-reboot/bootwindow 掛 deployed_recent`＋trailer）。

---

### Task 6: benchlock（flock＋pgrep 外部 testpilot，suite-refuse，TDD）

**Files:** Modify `realhw/preflight.py`、`tests/test_realhw_preflight.py`
**Interfaces:** Produces—`bench_lock_path() -> Path`（`~/.local/state/serialwrap/bench.lock`）、`acquire_benchlock(lock_path) -> int | None`（fd 持有至行程結束、flock 天性自動釋放）、`Checks` 增 `benchlock_ok: bool = True`／`external_testpilot: tuple[str, ...] = ()`、`evaluate` 兩個新拒跑理由。

- [ ] **Step 1: RED**（`tests/test_realhw_preflight.py` 追加；檔頂 import 增 `import os`）：

```python
def test_benchlock_mutual_exclusion(tmp_path):
    lock = tmp_path / "bench.lock"
    fd1 = preflight.acquire_benchlock(lock)
    assert fd1 is not None
    # flock 對同行程的第二個獨立 fd 亦互斥（open file description 級鎖）
    assert preflight.acquire_benchlock(lock) is None
    os.close(fd1)  # 釋放後可再取
    fd2 = preflight.acquire_benchlock(lock)
    assert fd2 is not None
    os.close(fd2)


def test_benchlock_failure_refuses_suite():
    ok, problems = preflight.evaluate(_checks(benchlock_ok=False))
    assert not ok and any("bench.lock" in p for p in problems)


def test_external_testpilot_refuses_suite():
    ok, problems = preflight.evaluate(
        _checks(external_testpilot=("1234 testpilot run wifi_llapi",)))
    assert not ok and any("testpilot" in p for p in problems)
```

- [ ] **Step 2: 跑確認失敗** → 預期 3 failed, 9 passed。

- [ ] **Step 3: GREEN——`realhw/preflight.py`**。頂部 import 增 `import fcntl`。`Checks` 尾端加預設欄位：

```python
@dataclasses.dataclass(frozen=True)
class Checks:
    git_behind: int
    doctor_ok: bool
    boards_ready: list[str]
    boards_expected: list[str]
    tools_missing: list[str]
    leaked_daemons: list[str]
    other_pytest: bool
    state_polluted: bool
    # v1.0.10+ reliability plugin（Task 6/8）——新欄一律帶預設值附加尾端（向後相容）
    benchlock_ok: bool = True
    external_testpilot: tuple[str, ...] = ()
```

`evaluate` 在 `state_polluted` 段之後、`return` 之前加：

```python
    if not c.benchlock_ok:
        ok = False
        problems.append("benchlock：~/.local/state/serialwrap/bench.lock 被他者持有"
                        "（另一場 reliability／wifi_llapi run？）——bench 互斥、整場拒跑")
    if c.external_testpilot:
        ok = False
        problems.append("偵測到進行中的外部 testpilot run（bench 互斥、整場拒跑）："
                        + "；".join(c.external_testpilot))
```

模組層加（`_state_polluted` 之後）：

```python
def bench_lock_path() -> Path:
    """bench 互斥鎖檔（R-21：以 Path.home() 組路徑）。"""
    return Path.home() / ".local/state/serialwrap/bench.lock"


def acquire_benchlock(lock_path: Path) -> int | None:
    """非阻塞 flock；成功回傳 fd（呼叫端持有到行程結束、勿 close——flock 隨行程終止
    自動釋放，spec §6 run 級保證），已被持有回 None。"""
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    fd = os.open(lock_path, os.O_RDWR | os.O_CREAT, 0o600)
    try:
        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError:
        os.close(fd)
        return None
    return fd


def _external_testpilot() -> list[str]:
    """pgrep 偵測進行中的外部 `testpilot run`（character-class 防 self-match）。"""
    cp = _run(["pgrep", "-af", r"testpilot ru[n]"])
    return [ln for ln in cp.stdout.splitlines() if ln.strip()]
```

`collect()` 簽章與 return 替換（此時先接 benchlock/external，win 欄 Task 8 再加）：

```python
def collect(cfg: dict, sw, repo_root, *, benchlock_ok: bool = True) -> Checks:
```

return 的 `Checks(...)` 增兩引數：

```python
        benchlock_ok=benchlock_ok,
        external_testpilot=tuple(_external_testpilot()),
```

（collect 的 docstring 同步補兩行說明：benchlock 由呼叫端 `acquire_benchlock` 後注入；external_testpilot＝`pgrep -af 'testpilot ru[n]'`。）

- [ ] **Step 4: 跑過**：`python3 -m pytest -q tests/test_realhw_preflight.py` → 預期 `12 passed`。
- [ ] **Step 5: commit**（`feat(realhw): benchlock suite-refuse——flock+pgrep 外部 testpilot 互斥`＋trailer）。

---

### Task 7: `WinSwCli` driver＋持有清單純解析（TDD）

**Files:** Modify `realhw/drivers.py`、`tests/test_realhw_drivers.py`
**Interfaces:** Produces—`WinSwCli(exe)`（`.available()`/`.run(*args)`/`.held_devices()`/`.release(com)`，JSON 解析比照 SwCli）、純函式 `parse_win_held(payload) -> list[dict]`、`match_held_for_serial(held, serial) -> dict | None`。

- [ ] **Step 1: RED**（`tests/test_realhw_drivers.py` 追加）：

```python
WIN_SESSIONS = {
    "ok": True,
    "sessions": [
        {"com": "COM3", "state": "READY", "device_by_id": "USB\\VID_0403+PID_6001+AQ00OAQ7A"},
        {"com": "COM4", "state": "DETACHED", "device_by_id": ""},
        {"com": "COM5", "state": "ATTACHED", "device_by_id": ""},
        {"com": "COM6", "state": "RELEASED", "device_by_id": ""},
    ],
}


def test_parse_win_held_excludes_detached_and_released():
    held = drivers.parse_win_held(WIN_SESSIONS)
    assert [h["com"] for h in held] == ["COM3", "COM5"]
    assert held[0]["state"] == "READY"


def test_match_held_for_serial_exact_hit():
    held = drivers.parse_win_held(WIN_SESSIONS)
    hit = drivers.match_held_for_serial(held, "AQ00OAQ7")
    assert hit is not None and hit["com"] == "COM3"


def test_match_held_for_serial_fallback_and_miss():
    # Windows 端常不暴露 by-id：全清單皆無 by-id 資訊 → 保守回第一筆（寧可多觸發一次
    # 無害的 release 探測，也不漏掉 0718 型持有）
    held = [{"com": "COM7", "state": "ATTACHED", "device_by_id": ""}]
    assert drivers.match_held_for_serial(held, "AC01QZT0") == held[0]
    # 有 by-id 資訊但比不到 → None（不亂歸因）
    held2 = [{"com": "COM8", "state": "READY", "device_by_id": "OTHER_SERIAL"}]
    assert drivers.match_held_for_serial(held2, "AC01QZT0") is None
    assert drivers.match_held_for_serial([], "AC01QZT0") is None
```

- [ ] **Step 2: 跑確認失敗** → 預期 3 failed, 3 passed。

- [ ] **Step 3: GREEN——`realhw/drivers.py`**。頂部 import 增 `from pathlib import Path`。`parse_usbipd_list` 之後加純函式，`Systemd` class 之後加 driver：

```python
def parse_win_held(payload: dict) -> list[dict]:
    """Windows 端 `session list` JSON → 持有中 session 清單（純解析，可單測）。

    state 非 DETACHED/RELEASED（含空）即視為「Windows 端 serialwrapd 持有該 COM 的
    exclusive handle」——0718 報告：此持有會讓 usbipd 拒絕再匯出給 WSL。
    """
    held: list[dict] = []
    for s in payload.get("sessions") or []:
        state = (s.get("state") or "").upper()
        if state in ("DETACHED", "RELEASED", ""):
            continue
        held.append({"com": s.get("com") or "", "state": state,
                     "device_by_id": s.get("device_by_id") or ""})
    return held


def match_held_for_serial(held: list[dict], serial: str) -> dict | None:
    """以板卡 serial 對 Windows 端持有清單歸屬（純函式）。

    - 任一筆 device_by_id 含 serial → 精確命中該筆。
    - 全清單皆無 by-id 資訊且非空 → 保守回第一筆（Windows 端常不暴露 by-id；
      寧可多觸發一次無害的 release 探測，也不漏掉 0718 型持有）。
    - 其餘（含空清單、有 by-id 但比不到）→ None。
    """
    for h in held:
        if serial and serial in (h.get("device_by_id") or ""):
            return h
    if held and not any(h.get("device_by_id") for h in held):
        return held[0]
    return None
```

```python
class WinSwCli:
    """Windows 端 serialwrap.exe 薄包裝（經 /mnt/c 呼叫；0718 雙 daemon 救援）。

    exe 為空或不存在＝Windows 端不可用：available() False、run 回 _rc=-1 停用值，
    所有上層（preflight 診斷、hp 救援鏈）據此降級 no-op。
    """

    def __init__(self, exe: str) -> None:
        self._exe = exe or ""

    def available(self) -> bool:
        return bool(self._exe) and Path(self._exe).exists()

    def run(self, *args: str, timeout: float = 30.0) -> dict:
        if not self.available():
            return {"_rc": -1, "_stderr": "win serialwrap.exe 未設定或不存在", "_raw": ""}
        cp = _run([self._exe, *args], timeout=timeout)
        out = cp.stdout.strip()
        try:
            data = json.loads(out) if out else {}
        except json.JSONDecodeError:
            data = {"_raw": out}
        data["_rc"] = cp.returncode
        data["_stderr"] = cp.stderr.strip()
        return data

    def held_devices(self) -> list[dict]:
        """Windows 端 session list → 持有清單（解析走 parse_win_held 純函式）。"""
        return parse_win_held(self.run("session", "list"))

    def release(self, com: str) -> dict:
        """Windows 端 device release（救援鏈動作；#54 handoff 語意）。"""
        return self.run("device", "release", "--selector", com,
                        "--source", "agent:realhw", "--reason", "realhw hp-rescue")
```

- [ ] **Step 4: 跑過**：`python3 -m pytest -q tests/test_realhw_drivers.py` → 預期 `6 passed`。
- [ ] **Step 5: commit**（`feat(realhw): WinSwCli driver＋Windows 端持有清單純解析（0718 雙 daemon）`＋trailer）。

---

### Task 8: preflight `windows_daemon` 診斷增強（TDD）

**Files:** Modify `realhw/preflight.py`、`tests/test_realhw_preflight.py`
**Interfaces:** Consumes—Task 7 `WinSwCli`/`match_held_for_serial`。Produces—`Checks` 增 `win_daemon_present: bool = False`／`win_daemon_holds: tuple[str, ...] = ()`（值＝本 bench 期望板的 WSL COM 名）；`evaluate` 對「兩板 READY 缺項」中被 Windows 端持有者，訊息歸因 `windows_daemon_holds_device`；`collect(cfg, sw, repo_root, *, benchlock_ok=True, win=None)`。

- [ ] **Step 1: RED**（`tests/test_realhw_preflight.py` 追加）：

```python
def test_boards_missing_attributed_to_windows_daemon():
    ok, problems = preflight.evaluate(_checks(
        boards_ready=["COM0"], win_daemon_present=True, win_daemon_holds=("COM1",)))
    assert not ok
    joined = "\n".join(problems)
    assert "windows_daemon_holds_device" in joined     # 歸因烙進缺項訊息
    assert "COM1" in joined


def test_boards_missing_without_windows_attribution_unchanged():
    ok, problems = preflight.evaluate(_checks(boards_ready=["COM0"]))
    assert not ok and any("COM1" in p and "windows_daemon" not in p for p in problems)
```

- [ ] **Step 2: 跑確認失敗** → 預期 2 failed（`unexpected keyword argument 'win_daemon_present'`）, 12 passed。

- [ ] **Step 3: GREEN——`realhw/preflight.py`**。頂部加 `from . import drivers`。`Checks` 尾端再加兩欄：

```python
    win_daemon_present: bool = False
    win_daemon_holds: tuple[str, ...] = ()  # 被 Windows 端持有的「期望板」WSL COM 名
```

`evaluate` 的 boards 缺項段替換：

```python
    missing = [b for b in c.boards_expected if b not in c.boards_ready]
    if missing:
        ok = False
        attributed = [b for b in missing if b in c.win_daemon_holds]
        plain = [b for b in missing if b not in c.win_daemon_holds]
        if plain:
            problems.append(f"板卡未 READY：{','.join(plain)}")
        if attributed:
            problems.append(
                f"板卡未 READY：{','.join(attributed)}（歸因 windows_daemon_holds_device："
                f"Windows 端 serialwrapd 持有該裝置的 exclusive handle，usbipd 拒絕匯出；"
                f"先於 Windows 端 `serialwrap.exe device release` 再重跑，見 0718 報告）")
```

`collect()` 簽章與實作替換（benchlock 段 Task 6 已加，這裡再加 win）：

```python
def collect(cfg: dict, sw, repo_root, *, benchlock_ok: bool = True, win=None) -> Checks:
```

boards 收集之後、`return Checks(...)` 之前加：

```python
    win_present = bool(win is not None and win.available())
    win_holds: list[str] = []
    if win_present:
        held = win.held_devices()
        for b in cfg["boards"]:
            if drivers.match_held_for_serial(held, b.get("serial", "")) is not None:
                win_holds.append(b["com"])
```

`Checks(...)` 增兩引數：

```python
        win_daemon_present=win_present,
        win_daemon_holds=tuple(win_holds),
```

- [ ] **Step 4: 跑過**：`python3 -m pytest -q tests/test_realhw_preflight.py` → 預期 `14 passed`。
- [ ] **Step 5: commit**（`feat(realhw): preflight windows_daemon 診斷——READY 缺項歸因 windows_daemon_holds_device`＋trailer）。

---

### Task 9: `__main__` 統一接線（load_cfg／benchlock／capabilities／WinSwCli／Ctx.win／tier help）

**Files:** Modify `realhw/__main__.py`、`realhw/harness.py`（Ctx 加一欄）
**Interfaces:** Consumes—Tasks 3-8 全部產物。Produces—可跑的 `python3 -m realhw`：suite-refuse（含 benchlock）→ family-gate 印缺項 → `run_cases(missing_caps=...)`；meta 烙 capabilities 與 windows_daemon。

- [ ] **Step 1: `realhw/harness.py` 的 `Ctx` 加尾欄**：

```python
@dataclasses.dataclass
class Ctx:
    cfg: dict
    report_dir: Path
    case_dir: Path
    sw: Any
    tmux: Any
    usbipd: Any
    systemd: Any
    win: Any = None  # WinSwCli（Windows 端 serialwrap.exe）；None/不可用＝救援鏈降級

    def note(self, name: str, content: str) -> str:
        """寫 evidence 檔，回傳相對路徑（進 CaseResult.evidence）。"""
        self.case_dir.mkdir(parents=True, exist_ok=True)
        p = self.case_dir / name
        p.write_text(content, encoding="utf-8")
        return str(p.relative_to(self.report_dir))
```

- [ ] **Step 2: `realhw/__main__.py` 全檔替換**：

```python
"""python3 -m realhw——實機穩定性套件入口。"""
from __future__ import annotations

import argparse
import datetime
import subprocess
import sys
from pathlib import Path

from . import cases  # noqa: F401  # import 觸發 case 註冊
from . import drivers, harness, preflight


def main() -> int:
    ap = argparse.ArgumentParser(prog="realhw", description="serialwrap 實機穩定性套件（#122）")
    ap.add_argument("--tier", default="p0",
                    help="p0|p1|remote|longrun，逗號多選；remote 與 longrun 必須顯式指定")
    ap.add_argument("--only")
    ap.add_argument("--skip", default="")
    ap.add_argument("--duration", default="32h", help="longrun 時長（<N>h/<N>m/<N>s）")
    ap.add_argument("--report-dir")
    ap.add_argument("--list", action="store_true")
    args = ap.parse_args()

    cfg = harness.load_cfg()
    cfg["duration_s"] = harness.parse_duration(args.duration)
    tiers = [t.strip() for t in args.tier.split(",") if t.strip()]
    selected = harness.select_cases(harness.REGISTRY, tiers=tiers, only=args.only,
                                    skip=[s for s in args.skip.split(",") if s])
    if args.list:
        for c in harness.REGISTRY:
            mark = "⚡" if c.destructive else "  "
            print(f"{mark} [{c.tier}] {c.id}  {c.title}")
        return 0

    ts = datetime.datetime.now().strftime("%y%m%d-%H%M%S")
    report_dir = Path(args.report_dir or Path.home() / "b-log" / "realhw-reports" / ts)
    sw = drivers.SwCli()
    win = drivers.WinSwCli(cfg.get("win_serialwrap_exe") or "")
    ctx = harness.Ctx(cfg=cfg, report_dir=report_dir, case_dir=report_dir,
                      sw=sw, tmux=drivers.TmuxCtl(cfg["tmux_prefix"]),
                      usbipd=drivers.Usbipd(cfg["usbipd_exe"]), systemd=drivers.Systemd(),
                      win=win)

    # suite-refuse 級：benchlock fd 持有到行程結束（flock 天性自動釋放，勿 close）。
    lock_fd = preflight.acquire_benchlock(preflight.bench_lock_path())  # noqa: F841
    checks = preflight.collect(cfg, sw, Path(__file__).resolve().parent.parent,
                               benchlock_ok=lock_fd is not None, win=win)
    ok, problems = preflight.evaluate(checks)
    for p in problems:
        print(f"[preflight] {p}")
    if not ok:
        print("[preflight] 拒跑：缺項如上")
        return 2

    # family-gate 級：缺項不擋整場，宣告 requires 的 case 執行期 SKIP＝FailEnv。
    caps = preflight.collect_capabilities(sw)
    missing_caps = preflight.missing_capabilities(caps)
    for name, code in missing_caps.items():
        print(f"[preflight] 能力缺項（family-gate）：{name}——對應 case 執行期 SKIP（{code}）")

    destructive = [c.id for c in selected if c.destructive]
    if destructive:
        print(f"[preflight] 本輪破壞性動作：{', '.join(destructive)}")
    print(f"[realhw] 報告目錄：{report_dir}")

    boards = [b["com"] for b in cfg["boards"]]
    meta = {
        "version": sw.run("--version").get("_raw", ""),
        "git": subprocess.run(["git", "rev-parse", "--short", "HEAD"],
                              capture_output=True, text=True).stdout.strip(),
        "tiers": args.tier, "started_at": ts, "preflight_notes": problems,
        "capabilities": {"deployed_version": caps.deployed_version,
                         "remote_capability": caps.remote_capability,
                         "docker": caps.docker, "missing": missing_caps},
        "windows_daemon": {"present": checks.win_daemon_present,
                           "holds": list(checks.win_daemon_holds)},
    }
    results = harness.run_cases(selected, ctx, boards=boards, missing_caps=missing_caps)
    hints = {c.id: c.hints for c in selected}
    harness.write_reports(report_dir, meta, results, hints)
    print(f"[realhw] 完成：{report_dir}/report.md")
    return 1 if any(r.verdict == "FAIL" for _, r in results) else 0


if __name__ == "__main__":
    sys.exit(main())
```

（原檔的 `import json` 已移除：config 讀取改走 `harness.load_cfg`，全檔不再有 `json.` 呼叫。）

- [ ] **Step 3: 冒煙驗證**（不碰 live——`--list` 不做 preflight）：

```bash
python3 -m realhw --list | head -5
python3 -m realhw --list | wc -l
python3 -m pytest -q tests/test_realhw_harness.py tests/test_realhw_drivers.py tests/test_realhw_preflight.py
```

預期：--list 印出 29 行 case 清單（`[p0] p0-doctor ...` 開頭）；pytest `37 passed`（17+6+14）。

- [ ] **Step 4: commit**（`feat(realhw): 入口接線 benchlock/capabilities/WinSwCli——兩級判決上線`＋trailer）。

---
### Task 10: hp 救援鏈純決策函式＋`Usbipd.attach` 回 rc（TDD）

**Files:** Modify `realhw/drivers.py`、`tests/test_realhw_drivers.py`
**Interfaces:** Produces—`plan_hp_rescue(win_available: bool, held_com: str | None, retries_done: int, *, max_retries: int = 2) -> tuple[str, ...]`（動作 token：`fail_attended`／`attach_retry`／`win_release:<COM>`）；`Usbipd.attach(busid) -> int`（回 returncode，呼叫端可忽略——既有呼叫點不需改）。

- [ ] **Step 1: RED**（`tests/test_realhw_drivers.py` 追加）：

```python
def test_plan_hp_rescue_release_then_retry_when_held():
    # Windows 端持有 → 先 release 再重試 attach
    assert drivers.plan_hp_rescue(True, "COM3", 0) == ("win_release:COM3", "attach_retry")
    assert drivers.plan_hp_rescue(True, "COM3", 1) == ("win_release:COM3", "attach_retry")


def test_plan_hp_rescue_bare_retry_when_not_held_or_no_win():
    # 無 Windows 端可探測／未持有 → 裸重試（環境抖動也給機會）
    assert drivers.plan_hp_rescue(False, None, 0) == ("attach_retry",)
    assert drivers.plan_hp_rescue(True, None, 0) == ("attach_retry",)


def test_plan_hp_rescue_exhausted_is_fail_attended():
    # 重試額度（≤2 次 attach_retry）用盡 → 放棄自動救援
    assert drivers.plan_hp_rescue(True, "COM3", 2) == ("fail_attended",)
    assert drivers.plan_hp_rescue(False, None, 2) == ("fail_attended",)
    assert drivers.plan_hp_rescue(True, None, 5) == ("fail_attended",)
```

- [ ] **Step 2: 跑確認失敗** → 預期 3 failed, 6 passed。

- [ ] **Step 3: GREEN——`realhw/drivers.py`**。`match_held_for_serial` 之後加：

```python
def plan_hp_rescue(win_available: bool, held_com: str | None, retries_done: int,
                   *, max_retries: int = 2) -> tuple[str, ...]:
    """hp-cycle usbipd attach 回插失敗的救援動作序列（純決策；spec：probe→release→retry≤2→fail attended）。

    注入探測結果、回傳動作 token 序列，subprocess 執行薄層（p1_hotplug）逐一執行：
      - ("fail_attended",)                       重試額度用盡，放棄自動救援（FAIL＋attended）
      - ("attach_retry",)                        直接重試 usbipd attach（無 Windows 端可探測／未偵測到持有）
      - (f"win_release:{held_com}", "attach_retry")  Windows 端 release 該 COM 後重試

    retries_done＝已執行過的 attach_retry 次數（首次失敗的原始 attach 不計）。
    """
    if retries_done >= max_retries:
        return ("fail_attended",)
    if win_available and held_com:
        return (f"win_release:{held_com}", "attach_retry")
    return ("attach_retry",)
```

`Usbipd.attach` 替換：

```python
    def attach(self, busid: str) -> int:
        """回傳 usbipd attach 的 returncode（0＝成功；非 0＝回插失敗，交由救援鏈歸因）。"""
        return _run([self._exe, "attach", "-w", "-b", busid], timeout=60).returncode
```

- [ ] **Step 4: 跑過**：`python3 -m pytest -q tests/test_realhw_drivers.py` → 預期 `9 passed`。
- [ ] **Step 5: commit**（`feat(realhw): hp 救援鏈純決策函式 plan_hp_rescue＋Usbipd.attach 回傳 rc`＋trailer）。

---

### Task 11: `p1-hp-cycle` 接上救援鏈（subprocess 薄層＋evidence＋FailEnv 標註）

**Files:** Modify `realhw/cases/p1_hotplug.py`
**Interfaces:** Consumes—Task 10 `plan_hp_rescue`、Task 7 `WinSwCli`（經 `ctx.win`）、Task 9 `Ctx.win`。Produces—救援失敗 FAIL＝`environment/windows_daemon_holds_device`（曾偵測到持有）或 `environment/usbipd_device_lost`（未持有仍失敗），reason 含「attended」；救援成功照常收尾、過程記 `rescue.log` evidence。

- [ ] **Step 1: `realhw/cases/p1_hotplug.py` 全檔替換**（含 Task 2 的分類標註；p1-hp-reorder 僅帶標註、不接救援）：

```python
"""P1 USB 熱插拔（destructive）：usbipd detach/attach 下的 DETACHED-rebind 與 COM↔by-id 確定性。

busid 換線會變——每條先以 usbipd list 驗 config busid 存在，缺則 SKIP（非 FAIL）。
COM1＝brcm/BDK 板。

p1-hp-cycle 內建 Windows 端自動救援鏈（0718 雙 daemon 根因）：usbipd attach 回插失敗
→ WinSwCli 探測 Windows 端 serialwrapd 是否持有 → device release → 重試 attach（≤2 次）；
決策在 drivers.plan_hp_rescue（純函式），本檔僅為 subprocess 執行薄層。
"""
from __future__ import annotations

import time

from .. import drivers
from ..harness import Case, CaseResult, register


def _case(id, title, hints=(), requires=(), destructive=True):
    def deco(fn):
        register(Case(id=id, tier="p1", title=title, run=fn,
                      destructive=destructive, requires=requires, hints=tuple(hints)))
        return fn
    return deco


def _board(ctx, com: str) -> dict:
    for b in ctx.cfg["boards"]:
        if b["com"] == com:
            return b
    return {}


def _ensure_attached(ctx, timeout_s: float) -> None:
    """finally 還原：缺席的 busid 補 attach，兩板等回 READY。"""
    present = set(ctx.usbipd.list_busids())
    for b in ctx.cfg["boards"]:
        if b["busid"] not in present:
            ctx.usbipd.attach(b["busid"])
    for b in ctx.cfg["boards"]:
        ctx.sw.wait_state(b["com"], "READY", timeout_s=timeout_s)


def _wait_left_ready(ctx, com: str, timeout_s: float) -> str | None:
    """等 com 離開 READY（熱移除被偵測），回最後觀察到的 state。"""
    deadline = time.monotonic() + timeout_s
    last = ctx.sw.session(com).get("state")
    while time.monotonic() < deadline:
        last = ctx.sw.session(com).get("state")
        if last != "READY":
            return last
        time.sleep(2)
    return last


def _attach_with_rescue(ctx, board: dict, log: list[str]) -> tuple[bool, str]:
    """usbipd attach＋自動救援鏈（執行薄層；決策在 drivers.plan_hp_rescue）。

    回傳 (usbipd attach 最終是否成功, 失敗歸因 reason_code)。
    只在 usbipd 層失敗（rc!=0，0718 訊號）觸發救援；attach 成功但 session 未回 READY
    屬 daemon 行為、由呼叫端以既有斷言處理（test/replug_ready_timeout）。
    """
    rc = ctx.usbipd.attach(board["busid"])
    log.append(f"usbipd attach rc={rc}")
    if rc == 0:
        return True, ""
    win = getattr(ctx, "win", None)
    retries = 0
    held_seen = False
    while True:
        win_ok = bool(win is not None and win.available())
        held_com: str | None = None
        if win_ok:
            held = win.held_devices()
            log.append(f"win held_devices={held}")
            hit = drivers.match_held_for_serial(held, board.get("serial", ""))
            if hit is not None:
                held_com = hit.get("com") or None
                held_seen = True
        plan = drivers.plan_hp_rescue(win_ok, held_com, retries)
        log.append(f"plan_hp_rescue(win={win_ok}, held={held_com}, retries={retries}) -> {plan}")
        if plan == ("fail_attended",):
            return False, ("windows_daemon_holds_device" if held_seen else "usbipd_device_lost")
        for action in plan:
            if action.startswith("win_release:"):
                com = action.split(":", 1)[1]
                rel = win.release(com)
                log.append(f"win release {com} -> ok={rel.get('ok')} rc={rel.get('_rc')}")
                time.sleep(2)  # 給 Windows 端釋放 handle 的空檔
            elif action == "attach_retry":
                retries += 1
                rc = ctx.usbipd.attach(board["busid"])
                log.append(f"usbipd attach retry#{retries} rc={rc}")
                if rc == 0:
                    return True, ""


@_case("p1-hp-cycle", "COM1 熱移除轉 DETACHED、COM0 不受擾、回插自動回原 COM READY（含 Windows 端自動救援）",
       hints=("熱插沿用 DETACHED-rebind：同 by-id 板回原 COM 空槽（#100）",
              "busid 不在 usbipd list＝換線，SKIP 非 FAIL",
              "回插後回 READY 靠 attach+login FSM：brcm/BDK 板需 credential（#140）——"
              "deployed daemon 缺 #140 修正時可能卡 ATTACHED 不回 READY",
              "usbipd attach 失敗→自動救援鏈（Windows 端 device release+重試≤2）；"
              "救不回才 FAIL=windows_daemon_holds_device＋attended（0718 根因）",
              "config 的 win_serialwrap_exe 為空＝Windows 端不可探測，救援降級裸重試"),
       requires=("two_boards",))
def p1_hp_cycle(ctx):
    ready_wait = ctx.cfg["timeouts"]["ready_wait_s"]
    b1 = _board(ctx, "COM1")
    busids = ctx.usbipd.list_busids()
    ctx.note("usbipd-list.txt", str(busids))
    if b1.get("busid") not in busids:
        return CaseResult("SKIP", reason=f"COM1 busid {b1.get('busid')} 不在 usbipd list（換線？）",
                          category="environment", reason_code="busid_missing")
    rescue_log: list[str] = []
    try:
        ctx.usbipd.detach(b1["busid"])
        state = _wait_left_ready(ctx, "COM1", 30)
        ctx.note("com1-state.txt", f"after detach state={state}")
        if state == "READY":
            return CaseResult("FAIL", reason="detach 後 COM1 未在 30s 內離開 READY",
                              category="test", reason_code="hotunplug_not_detected")
        if ctx.sw.session("COM0").get("state") != "READY":
            return CaseResult("FAIL", reason="COM1 熱移除擾動了 COM0（非 READY）",
                              category="test", reason_code="bystander_disturbed")
        attached, fail_code = _attach_with_rescue(ctx, b1, rescue_log)
        if not attached:
            ev = {"rescue": ctx.note("rescue.log", "\n".join(rescue_log))}
            return CaseResult("FAIL",
                              reason="usbipd attach 回插失敗且自動救援未果（attended：需人工處置"
                                     "Windows 端／重插線；救援過程見 evidence）",
                              category="environment", reason_code=fail_code, evidence=ev)
        if not ctx.sw.wait_state("COM1", "READY", timeout_s=ready_wait):
            return CaseResult("FAIL", reason="回插後 COM1 未自動回 READY",
                              category="test", reason_code="replug_ready_timeout")
        now = ctx.sw.session("COM1")
        if b1["serial"] not in (now.get("device_by_id") or ""):
            return CaseResult("FAIL", reason=f"回插後 COM1 by-id 不含 {b1['serial']}（未回原 COM）",
                              category="test", reason_code="com_rank_flipped")
        r = CaseResult("PASS")
        if rescue_log:  # 救援成功也留紀錄（spec：救援過程記 evidence）
            r.evidence["rescue"] = ctx.note("rescue.log", "\n".join(rescue_log))
            r.reason = "PASS（經自動救援：Windows 端 release 後回插成功）"
        return r
    finally:
        _ensure_attached(ctx, ready_wait)


@_case("p1-hp-reorder", "兩板反序回插仍各回原 COM，restart 後 rank 不翻轉（#100）",
       hints=("反序 attach 檢驗 DETACHED-rebind 依 by-id 認板、非列舉序",
              "restart 後 startup rank 仍 COM0=AC01QZT0/COM1=AQ00OAQ7 為 #100 核心保證"),
       requires=("two_boards",))
def p1_hp_reorder(ctx):
    ready_wait = ctx.cfg["timeouts"]["ready_wait_s"]
    reboot_wait = ctx.cfg["timeouts"]["reboot_wait_s"]
    b0, b1 = _board(ctx, "COM0"), _board(ctx, "COM1")
    busids = ctx.usbipd.list_busids()
    ctx.note("usbipd-list.txt", str(busids))
    missing = [b["busid"] for b in (b0, b1) if b["busid"] not in busids]
    if missing:
        return CaseResult("SKIP", reason=f"busid 不在 usbipd list：{missing}（換線？）",
                          category="environment", reason_code="busid_missing")
    before = {b["com"]: ctx.sw.session(b["com"]).get("attached_real_path") for b in (b0, b1)}
    try:
        ctx.usbipd.detach(b0["busid"])
        ctx.usbipd.detach(b1["busid"])
        time.sleep(3)
        # 反序回插：COM1 的 busid 先
        ctx.usbipd.attach(b1["busid"])
        time.sleep(2)
        ctx.usbipd.attach(b0["busid"])
        for b in (b0, b1):
            if not ctx.sw.wait_state(b["com"], "READY", timeout_s=ready_wait):
                return CaseResult("FAIL", reason=f"反序回插後 {b['com']} 未回 READY",
                                  category="test", reason_code="replug_ready_timeout")
        for b in (b0, b1):
            now = ctx.sw.session(b["com"])
            if b["serial"] not in (now.get("device_by_id") or ""):
                return CaseResult("FAIL", reason=f"{b['com']} by-id 不含 {b['serial']}（COM↔板對調）",
                                  category="test", reason_code="com_rank_flipped")
        after = {b["com"]: ctx.sw.session(b["com"]).get("attached_real_path") for b in (b0, b1)}
        ctx.note("realpath.txt", f"before={before} after={after}")
        # restart：startup rank 下仍 COM0=AC01QZT0 / COM1=AQ00OAQ7
        rc = ctx.systemd.restart()
        if rc != 0:
            return CaseResult("FAIL", reason=f"systemctl restart 回 rc={rc}",
                              category="environment", reason_code="systemd_restart_failed")
        for b in (b0, b1):
            if not ctx.sw.wait_state(b["com"], "READY", timeout_s=reboot_wait):
                return CaseResult("FAIL", reason=f"restart 後 {b['com']} 未回 READY",
                                  category="test", reason_code="restart_ready_timeout")
        for b in (b0, b1):
            now = ctx.sw.session(b["com"])
            if b["serial"] not in (now.get("device_by_id") or ""):
                return CaseResult("FAIL", reason=f"restart 後 {b['com']} 不對應 {b['serial']}（#100 rank 退化）",
                                  category="test", reason_code="com_rank_flipped")
        return CaseResult("PASS")
    finally:
        _ensure_attached(ctx, reboot_wait)
```

- [ ] **Step 2: 驗證（單測綠＋import 健康）**：

```bash
python3 -c "import realhw.cases; from realhw import harness; print(len(harness.REGISTRY))"
python3 -m pytest -q tests/test_realhw_harness.py tests/test_realhw_drivers.py tests/test_realhw_preflight.py
```

預期：`29`；`40 passed`（17+9+14）。

- [ ] **Step 3: commit**（`feat(realhw): p1-hp-cycle 內建 Windows 端自動救援鏈（0718 根因，attended 降級為 fallback）`＋trailer）。

- [ ] **Step 4:【真機-人工閘】`--only p1-hp-cycle` 實跑**（操作者執行；實作 agent 到此為止）：
  1. `realhw/config.json` 填入實際 `win_serialwrap_exe`（`/mnt/c/...` 路徑；bench 上以 `ls '/mnt/c/Program Files'` 等確認）。此值為機器特定、**不 commit 佔位假路徑**——若 bench 值穩定可 commit 實值。
  2. `python3 -m realhw --only p1-hp-cycle` → 期望 PASS；報告 `report.md` 分類欄空（PASS）。
  3. Windows 端持有情境實測：手動於 Windows 端讓 serialwrapd.exe attach COM1 對應裝置後 detach `8-2` 再跑一次 → 期望自動救援（report evidence 含 `rescue.log`、reason 標「經自動救援」）或 FAIL=`environment/windows_daemon_holds_device`＋attended（若 release 仍救不回）。
  4. 結果記回本 checkbox（PASS/FAIL＋報告路徑）。

---

### Task 12: `remote_tunnel_test.sh` 加 `$1` 逐拓樸分派（微改、向後相容）

**Files:** Modify `tools/docker/remote_tunnel_test.sh`
**Interfaces:** Produces—`bash remote_tunnel_test.sh [direct|nat_host|dual_nat|gwports|all]`；無參數＝all（向後相容既有 CI／手動用法）；未知參數＝FAIL exit 1。

- [ ] **Step 1: 檔尾 main 段替換**。原：

```bash
# ══════════════════════════ main ══════════════════════════
main() {
  log "build image: ${IMAGE_TAG}"
  DOCKER_BUILDKIT=1 docker build --progress=plain -t "${IMAGE_TAG}" "${ROOT_DIR}" || fail "docker build 失敗"

  topology_direct
  topology_nat_host
  topology_dual_nat
  topology_gatewayports_failclosed

  log "remote-tunnel acceptance: PASS"
}

main
```

改為：

```bash
# ══════════════════════════ main ══════════════════════════
# 用法：remote_tunnel_test.sh [direct|nat_host|dual_nat|gwports|all]
# 無參數＝all（向後相容）；realhw rm-topo 族逐拓樸呼叫（reliability plugin Phase 1d）。
main() {
  local sel="${1:-all}"
  case "$sel" in
    direct|nat_host|dual_nat|gwports|all) : ;;
    *) fail "未知拓樸參數：${sel}（可用：direct|nat_host|dual_nat|gwports|all）" ;;
  esac

  log "build image: ${IMAGE_TAG}"
  DOCKER_BUILDKIT=1 docker build --progress=plain -t "${IMAGE_TAG}" "${ROOT_DIR}" || fail "docker build 失敗"

  case "$sel" in
    direct)   topology_direct ;;
    nat_host) topology_nat_host ;;
    dual_nat) topology_dual_nat ;;
    gwports)  topology_gatewayports_failclosed ;;
    all)
      topology_direct
      topology_nat_host
      topology_dual_nat
      topology_gatewayports_failclosed
      ;;
  esac

  log "remote-tunnel acceptance: PASS"
}

main "${1:-all}"
```

（**坑**：檔頭 `set -uo pipefail`——`main` 呼叫端也必須用 `"${1:-all}"`，直接 `main "$1"` 在無參數時 unbound variable 即炸。）

- [ ] **Step 2: 驗證**：

```bash
bash -n tools/docker/remote_tunnel_test.sh && echo SYNTAX-OK
grep -n 'main "${1:-all}"' tools/docker/remote_tunnel_test.sh
```

預期：`SYNTAX-OK`；grep 命中 1 行。（實跑任一拓樸屬【真機-人工閘】，於 Task 17 一併驗。）

- [ ] **Step 3: commit**（`feat(tools): remote_tunnel_test.sh 逐拓樸分派參數（realhw rm-topo 前置，預設 all 向後相容）`＋trailer）。

---

### Task 13: rm-topo verdict 映射＋`remote_state_dir` 純函式（TDD）

**Files:** Modify `realhw/drivers.py`、`tests/test_realhw_drivers.py`
**Interfaces:** Produces—`classify_topology_run(rc: int, log_tail: str) -> tuple[str, str, str, str]`（(verdict, category, reason_code, reason)）；`remote_state_dir(env: dict[str, str] | None = None) -> Path`（複刻部署 CLI 的 remote registry 目錄解析、供 rm-live 淨空斷言；禁 import sw_core 故自行複刻，解析序＝`sw_core/constants.py` 的 `_run_dir_default`）。

**verdict 分桶判別規則（明確、可單測；spec「script 自身 FAIL 訊息→test 或 environment」的裁決）**：
- `rc==0` 且尾段含 `SKIP：` → SKIP＝`environment/docker_unavailable`（script 的 docker 不可用路徑 exit 0；realhw 端 requires=docker 正常已先攔，此為防禦性保留）。
- `rc==0` → PASS。
- `rc!=0`：reason 取尾段最後一行 `FAIL:`；尾段含「`docker build 失敗`」→ `environment/docker_build_failed`；含「`逾時未就緒`」（uart harness 未起）→ `environment/harness_not_ready`；**其餘一律** `test/tunnel_assertion_failed`（拓樸斷言①-⑧驗的是 serialwrap remote 行為＝受測物）。

- [ ] **Step 1: RED**（`tests/test_realhw_drivers.py` 追加；`tmp_path` fixture 即 Path、不需額外 import）：

```python
def test_classify_topology_run_pass_and_skip():
    assert drivers.classify_topology_run(0, "[serialwrap] === 拓樸 1／direct：PASS ===") == \
        ("PASS", "", "", "")
    v, cat, code, _ = drivers.classify_topology_run(
        0, "[serialwrap] SKIP：docker daemon 不可連（docker info 失敗）…")
    assert (v, cat, code) == ("SKIP", "environment", "docker_unavailable")


def test_classify_topology_run_environment_signals():
    v, cat, code, reason = drivers.classify_topology_run(
        1, "[serialwrap] FAIL: docker build 失敗")
    assert (v, cat, code) == ("FAIL", "environment", "docker_build_failed")
    assert "docker build" in reason
    v, cat, code, _ = drivers.classify_topology_run(
        1, "[serialwrap] FAIL: sw-rt-uart1-9：uart harness（fake target + serialwrapd）逾時未就緒")
    assert (v, cat, code) == ("FAIL", "environment", "harness_not_ready")


def test_classify_topology_run_assertion_failure_is_test():
    tail = "[serialwrap] FAIL: assertion⑤：sw-rt-agent1 port 7777 bind 位址非 loopback：0.0.0.0:7777"
    v, cat, code, reason = drivers.classify_topology_run(1, tail)
    assert (v, cat, code) == ("FAIL", "test", "tunnel_assertion_failed")
    assert "assertion⑤" in reason
    # rc!=0 但 log 尾段無 FAIL 行（如 timeout 砍掉）→ 仍 test 分桶、reason 註明 rc
    v, cat, code, reason = drivers.classify_topology_run(124, "…被截斷的輸出…")
    assert (v, cat, code) == ("FAIL", "test", "tunnel_assertion_failed")
    assert "rc=124" in reason


def test_remote_state_dir_resolution_order(tmp_path):
    # 複刻 CLI 解析序：SERIALWRAP_RUN_DIR > SERIALWRAP_STATE_DIR > XDG_RUNTIME_DIR > XDG state 預設
    assert drivers.remote_state_dir({"SERIALWRAP_RUN_DIR": str(tmp_path)}) == tmp_path / "remote"
    assert drivers.remote_state_dir({"SERIALWRAP_STATE_DIR": str(tmp_path)}) == tmp_path / "remote"
    assert drivers.remote_state_dir({"XDG_RUNTIME_DIR": str(tmp_path)}) == \
        tmp_path / "serialwrap" / "remote"
    got = drivers.remote_state_dir({})
    assert str(got).endswith(".local/state/serialwrap/run/remote")
```

- [ ] **Step 2: 跑確認失敗** → 預期 4 failed, 9 passed。

- [ ] **Step 3: GREEN——`realhw/drivers.py` 加**（`plan_hp_rescue` 之後）：

```python
def classify_topology_run(rc: int, log_tail: str) -> tuple[str, str, str, str]:
    """remote_tunnel_test.sh 單拓樸執行結果 → (verdict, category, reason_code, reason)。

    判別規則（rm-topo 分桶裁決線）：
    - rc==0 且尾段含 `SKIP：`（script 的 docker 不可用路徑 exit 0）→ SKIP/environment/
      docker_unavailable——防禦性保留，realhw 端 requires=docker 正常已先攔。
    - rc==0 → PASS。
    - rc!=0：reason 取尾段最後一行 FAIL:；環境訊號（docker build 失敗／uart harness
      逾時未就緒）→ environment＋對應碼；其餘＝拓樸斷言①-⑧失敗（受測物行為）→
      test/tunnel_assertion_failed。
    """
    tail = log_tail or ""
    if rc == 0:
        if "SKIP：" in tail:
            return ("SKIP", "environment", "docker_unavailable",
                    "script 回報 docker 不可用（SKIP）")
        return ("PASS", "", "", "")
    fail_lines = [ln.strip() for ln in tail.splitlines() if "FAIL:" in ln]
    reason = fail_lines[-1] if fail_lines else f"script 異常結束 rc={rc}（log 尾段無 FAIL 行）"
    if "docker build 失敗" in tail:
        return ("FAIL", "environment", "docker_build_failed", reason)
    if "逾時未就緒" in tail:
        return ("FAIL", "environment", "harness_not_ready", reason)
    return ("FAIL", "test", "tunnel_assertion_failed", reason)


def remote_state_dir(env: dict[str, str] | None = None) -> Path:
    """複刻部署 CLI 的 remote registry 目錄（`<RUN_DIR>/remote/`）解析。

    realhw 禁 import sw_core，故照 `sw_core/constants.py::_run_dir_default` 同序複刻：
    SERIALWRAP_RUN_DIR → SERIALWRAP_STATE_DIR → XDG_RUNTIME_DIR/serialwrap →
    (XDG_STATE_HOME|~/.local/state)/serialwrap/run；一律加尾段 remote/。
    env 可注入（單測）；預設讀 os.environ。
    """
    e = os.environ if env is None else env
    run = (e.get("SERIALWRAP_RUN_DIR") or "").strip()
    if run:
        return Path(os.path.expanduser(run)) / "remote"
    state = (e.get("SERIALWRAP_STATE_DIR") or "").strip()
    if state:
        return Path(os.path.expanduser(state)) / "remote"
    xrt = (e.get("XDG_RUNTIME_DIR") or "").strip()
    if xrt:
        return Path(xrt) / "serialwrap" / "remote"
    state_home = (e.get("XDG_STATE_HOME") or "").strip()
    base = Path(os.path.expanduser(state_home)) if state_home else Path.home() / ".local/state"
    return base / "serialwrap" / "run" / "remote"
```

- [ ] **Step 4: 跑過**：`python3 -m pytest -q tests/test_realhw_drivers.py` → 預期 `13 passed`。
- [ ] **Step 5: commit**（`feat(realhw): rm-topo verdict 分桶純函式＋remote state dir 解析（rm 族前置）`＋trailer）。

---
### Task 14: `realhw/cases/remote.py`——rm-topo ×4＋tier `remote` 接線

**Files:** Create `realhw/cases/remote.py`；Modify `realhw/cases/__init__.py`
**Interfaces:** Consumes—Task 12 的 script `$1` 分派、Task 13 `classify_topology_run`。Produces—4 個 tier=`remote` case（`rm-topo-direct`/`rm-topo-nat-host`/`rm-topo-dual-nat`/`rm-topo-gwports`，requires=`("docker",)`、全非破壞性）；image 建置延遲到第一個 rm-topo case（script 自帶 build，docker cache 使後續快）；finally 掃殘留容器/network（wrapper 第二道防線，script trap 為第一道）。

- [ ] **Step 1: Create `realhw/cases/remote.py`**：

```python
"""tier `remote`：serialwrap remote（PR #143）實機驗證。

第一層 rm-topo ×4——逐拓樸 shell out 包裝 tools/docker/remote_tunnel_test.sh
（容器封閉世界＋假 UART，驗這台機的隧道工具鏈）；exit code＋log 尾段 →
drivers.classify_topology_run 分桶。image 建置延遲到第一個 rm-topo case
（script 自帶 docker build，cache 使後續拓樸秒過）。

第二層 rm-live ×3（Task 15 追加於本檔尾）——docker 容器只當 ssh 對端，
對「部署 daemon＋真板」驗 -R expose 穿隧道端到端／orphan 自癒／open-close 循環。

容器名前綴：harness＝`sw-rt-*-${SUFFIX}`（本檔以 SUFFIX=rhw<pid> 注入）；
rm-live＝`rhwlive-*`——兩層各自 teardown、互不掃到（spec §8 風險緩解）。
"""
from __future__ import annotations

import os
import subprocess
from pathlib import Path

from .. import drivers
from ..harness import Case, CaseResult, register

_SCRIPT = Path(__file__).resolve().parents[2] / "tools" / "docker" / "remote_tunnel_test.sh"
_TOPO_TIMEOUT_S = 1800  # 首次含 docker build；cache 後單拓樸約 1-3 分鐘


def _case(id, title, hints=(), requires=(), destructive=False):
    def deco(fn):
        register(Case(id=id, tier="remote", title=title, run=fn,
                      destructive=destructive, requires=requires, hints=tuple(hints)))
        return fn
    return deco


def _sweep_docker(needle: str) -> None:
    """掃殘留容器/network（名稱含 needle）；script trap 為第一道防線，此為第二道。"""
    ps = subprocess.run(["docker", "ps", "-aq", "--filter", f"name={needle}"],
                        capture_output=True, text=True)
    ids = [x for x in ps.stdout.split() if x]
    if ids:
        subprocess.run(["docker", "rm", "-f", *ids], capture_output=True, text=True)
    nets = subprocess.run(["docker", "network", "ls", "-q", "--filter", f"name={needle}"],
                          capture_output=True, text=True)
    nids = [x for x in nets.stdout.split() if x]
    if nids:
        subprocess.run(["docker", "network", "rm", *nids], capture_output=True, text=True)


def _run_topology(ctx, topo: str) -> CaseResult:
    """單拓樸 shell out：SUFFIX 注入（掃殘留可對齊）、全量 log 進 evidence。"""
    suffix = f"rhw{os.getpid()}"
    env = dict(os.environ, SUFFIX=suffix)
    try:
        try:
            cp = subprocess.run(["bash", str(_SCRIPT), topo], capture_output=True,
                                text=True, timeout=_TOPO_TIMEOUT_S, env=env)
            rc = cp.returncode
            out = (cp.stdout or "") + "\n--- stderr ---\n" + (cp.stderr or "")
        except subprocess.TimeoutExpired as exc:
            rc = -1
            out = f"{exc.stdout or ''}\n--- stderr ---\n{exc.stderr or ''}\n（逾時 {_TOPO_TIMEOUT_S}s 遭終止）"
    finally:
        _sweep_docker(suffix)
    log_rel = ctx.note(f"{topo}.log", out)
    verdict, category, code, reason = drivers.classify_topology_run(rc, out[-8000:])
    return CaseResult(verdict, reason=reason or f"{topo} 拓樸驗收通過",
                      category=category, reason_code=code, evidence={"log": log_rel})


_TOPO_HINTS = (
    "包裝而非移植：斷言細節看 tools/docker/remote_tunnel_test.sh 檔頭①-⑧",
    "FAIL 行含 docker build/harness 逾時＝environment；其餘＝拓樸斷言（test）",
    "殘留容器 sw-rt-*／network net_*：script trap＋wrapper finally 雙防線",
)


@_case("rm-topo-direct", "direct：-R expose＋-L connect＋close/prune 全流程（容器封閉世界）",
       hints=_TOPO_HINTS, requires=("docker",))
def rm_topo_direct(ctx):
    return _run_topology(ctx, "direct")


@_case("rm-topo-nat-host", "NAT→host relay＋攻擊者容器隔離斷言",
       hints=_TOPO_HINTS, requires=("docker",))
def rm_topo_nat_host(ctx):
    return _run_topology(ctx, "nat_host")


@_case("rm-topo-dual-nat", "雙 NAT relay＋兩側繞行隔離斷言",
       hints=_TOPO_HINTS, requires=("docker",))
def rm_topo_dual_nat(ctx):
    return _run_topology(ctx, "dual_nat")


@_case("rm-topo-gwports", "GatewayPorts/--remote-socket fail-closed＋teardown 複查",
       hints=_TOPO_HINTS, requires=("docker",))
def rm_topo_gwports(ctx):
    return _run_topology(ctx, "gwports")
```

- [ ] **Step 2: `realhw/cases/__init__.py` 全檔替換**：

```python
"""realhw case 模組——import 各子模組觸發 register()。

P0×8＋P1×20＋longrun×1（#122）＋remote×7（reliability plugin Phase 1d）。
"""
from __future__ import annotations

from . import p0  # noqa: F401
from . import p1_console  # noqa: F401
from . import p1_cmd  # noqa: F401
from . import p1_wal  # noqa: F401
from . import p1_restart  # noqa: F401
from . import p1_handoff  # noqa: F401
from . import p1_hotplug  # noqa: F401
from . import longrun  # noqa: F401
from . import remote  # noqa: F401
```

- [ ] **Step 3: 冒煙驗證**：

```bash
python3 -m realhw --list | grep -c "\[remote\]"
python3 -m realhw --list | wc -l
python3 -m pytest -q tests/test_realhw_harness.py tests/test_realhw_drivers.py tests/test_realhw_preflight.py
```

預期：`4`；`33`；`44 passed`（17+13+14）。

- [ ] **Step 4: commit**（`feat(realhw): tier remote——rm-topo×4 包裝 docker 三拓樸 harness`＋trailer）。

---

### Task 15: rm-live ×3（e2e／orphan／cycle：部署 daemon＋真板）

**Files:** Modify `realhw/cases/remote.py`（檔尾追加）
**Interfaces:** Consumes—部署 CLI `serialwrap remote`（`_run_remote` JSON 契約：open 回 `{ok,status,pid,listen_port,...}`、`remote status` 回 `{ok,tunnels:[...]}`、`remote close all` 冪等）、Task 13 `remote_state_dir`。Produces—`rm-live-e2e`/`rm-live-orphan`/`rm-live-cycle`（requires=`("docker","two_boards","remote_capability")`、非破壞性、容器前綴 `rhwlive-`）。

- [ ] **Step 1: `realhw/cases/remote.py` import 區補三行**（`import os` 之後）：

```python
import json
import random
import time
```

- [ ] **Step 2: 檔尾追加 rm-live 段**：

```python
# ═══════════════════ rm-live：部署 daemon＋真板（容器只當 ssh 對端）═══════════════════

_LIVE_PREFIX = "rhwlive"  # 與 harness 的 sw-rt-* 前綴區隔
_IMAGE_TAG = os.environ.get("IMAGE_TAG", "serialwrap:remote-tunnel-test")  # 與 script 同 image
_LIVE_PORT = 7777

_LIVE_HINTS = (
    "受測物＝部署 daemon＋remote CLI＋真板；容器只是 sshd 對端（image 與 rm-topo 共用）",
    "host→容器 ssh 用 image 預燒 tester 金鑰（docker cp 匯出）＋--ssh-opt 關 host-key 檢查",
    "close 後淨空＝remote status 空＋state dir 無 *.json/cm-*/*.log＋無殘留 ssh 行程",
    "daemon pid 全程不變＝remote 純 CLI 便利層、daemon 零觸碰（PR #143 契約）",
)


def _ensure_image(ctx) -> str | None:
    """確保測試 image 存在（延遲建置；build log 進 evidence）。失敗回錯誤訊息。"""
    chk = subprocess.run(["docker", "image", "inspect", _IMAGE_TAG],
                         capture_output=True, text=True)
    if chk.returncode == 0:
        return None
    root = Path(__file__).resolve().parents[2]
    bld = subprocess.run(["docker", "build", "-t", _IMAGE_TAG, str(root)],
                         capture_output=True, text=True, timeout=1800)
    ctx.note("image-build.log", (bld.stdout or "") + "\n" + (bld.stderr or ""))
    if bld.returncode != 0:
        return f"docker build 失敗 rc={bld.returncode}"
    return None


def _start_ssh_peer(ctx, tag: str) -> tuple[str, str, Path | None]:
    """起 sshd 對端容器；回傳 (容器名, 容器 IP, ssh 私鑰路徑)。

    失敗徵候以空 ip／None key 表達（呼叫端 FAIL=environment/sshd_unavailable）。
    金鑰＝image 預燒的 tester ed25519（docker cp 匯出＋chmod 600）。
    """
    name = f"{_LIVE_PREFIX}-{tag}-{os.getpid()}"
    subprocess.run(["docker", "rm", "-f", name], capture_output=True, text=True)
    run = subprocess.run(["docker", "run", "-d", "--init", "--name", name, _IMAGE_TAG,
                          "sleep", "infinity"], capture_output=True, text=True)
    if run.returncode != 0:
        return name, "", None
    subprocess.run(["docker", "exec", name, "bash", "-c",
                    "mkdir -p /run/sshd && /usr/sbin/sshd"], capture_output=True, text=True)
    if subprocess.run(["docker", "exec", name, "pgrep", "-x", "sshd"],
                      capture_output=True, text=True).returncode != 0:
        return name, "", None
    ip = subprocess.run(["docker", "inspect", "-f",
                         "{{range .NetworkSettings.Networks}}{{.IPAddress}}{{end}}", name],
                        capture_output=True, text=True).stdout.strip()
    ctx.case_dir.mkdir(parents=True, exist_ok=True)
    key = ctx.case_dir / "id_ed25519"
    cp = subprocess.run(["docker", "cp", f"{name}:<tester-home>/.ssh/id_ed25519", str(key)],
                        capture_output=True, text=True)
    if cp.returncode != 0 or not key.exists():
        return name, "", None
    key.chmod(0o600)
    return name, ip, key


def _remote_open(ctx, ip: str, key: Path) -> dict:
    """host 端 `serialwrap remote tester@<ip>:7777`（-R expose 預設）。"""
    return ctx.sw.run("remote", f"tester@{ip}:{_LIVE_PORT}",
                      f"--ssh-opt=-i{key}",
                      "--ssh-opt=-oStrictHostKeyChecking=no",
                      "--ssh-opt=-oUserKnownHostsFile=/dev/null",
                      timeout=60)


def _agent_exec(name: str, *sub: str, timeout: float = 60.0) -> dict:
    """容器內以 --endpoint 穿隧道呼叫 serialwrap（stdout JSON 解析比照 SwCli）。"""
    cp = subprocess.run(["docker", "exec", "-u", "tester", name, "serialwrap",
                         "--endpoint", f"tcp://127.0.0.1:{_LIVE_PORT}", *sub],
                        capture_output=True, text=True, timeout=timeout)
    out = cp.stdout.strip()
    try:
        data = json.loads(out) if out else {}
    except json.JSONDecodeError:
        data = {"_raw": out}
    data["_rc"] = cp.returncode
    data["_stderr"] = cp.stderr.strip()
    return data


def _registry_leftovers() -> list[str]:
    """部署端 remote registry 殘留（*.json／cm-*／*.log；.registry.lock 常駐屬正常）。"""
    d = drivers.remote_state_dir()
    if not d.is_dir():
        return []
    return sorted(p.name for p in d.iterdir()
                  if p.suffix in (".json", ".log") or p.name.startswith("cm-"))


def _live_teardown(ctx, name: str) -> None:
    """best-effort 收尾：close all＋殺容器（每案 finally 必經）。"""
    ctx.sw.run("remote", "close", "all")
    if name:
        subprocess.run(["docker", "rm", "-f", name], capture_output=True, text=True)


@_case("rm-live-e2e", "-R expose 至容器→穿隧道 session list＋真板 marker＋WAL 歸因→close 淨空、daemon pid 不變",
       hints=_LIVE_HINTS, requires=("docker", "two_boards", "remote_capability"))
def rm_live_e2e(ctx):
    err = _ensure_image(ctx)
    if err:
        return CaseResult("FAIL", reason=err, category="environment",
                          reason_code="docker_build_failed")
    ctx.sw.run("remote", "close", "all")  # 防前輪殘留撞 identity（benchlock 保證無他人隧道）
    pid_before = ctx.sw.run("daemon", "status").get("pid")
    start_seq = ctx.sw.run("wal", "current-seq").get("seq") or 0
    name = ""
    try:
        name, ip, key = _start_ssh_peer(ctx, "e2e")
        if not ip or key is None:
            return CaseResult("FAIL", reason="sshd 對端容器未就緒（docker run/sshd/金鑰）",
                              category="environment", reason_code="sshd_unavailable")
        opened = _remote_open(ctx, ip, key)
        ctx.note("open.json", str(opened))
        if not opened.get("ok") or opened.get("status") != "active":
            return CaseResult("FAIL",
                              reason=f"remote -R expose 未 active（{opened.get('error_code') or opened.get('status')}）",
                              category="test", reason_code="tunnel_open_failed")
        sl = _agent_exec(name, "session", "list")
        ctx.note("session-list.json", str(sl))
        if not sl.get("ok") or not any(s.get("com") == "COM0" and s.get("state") == "READY"
                                       for s in sl.get("sessions") or []):
            return CaseResult("FAIL", reason="容器內穿隧道 session list 未見 COM0 READY",
                              category="test", reason_code="tunnel_session_list_failed")
        marker = f"RMLIVE_{random.randint(10000, 99999)}"
        sub = _agent_exec(name, "cmd", "submit", "--selector", "COM0",
                          "--cmd", f"echo {marker}", "--source", "agent:rhwremote",
                          "--cmd-timeout", "12")
        cmd_id = sub.get("cmd_id")
        if not cmd_id:
            return CaseResult("FAIL", reason=f"穿隧道 cmd submit 未回 cmd_id（{sub.get('error_code')}）",
                              category="test", reason_code="tunnel_submit_failed")
        command: dict = {}
        deadline = time.monotonic() + 30
        time.sleep(1.5)  # submit 後立即讀有 line race（同 SwCli.submit_and_wait 慣例）
        while time.monotonic() < deadline:
            command = _agent_exec(name, "cmd", "status", "--cmd-id", str(cmd_id)).get("command") or {}
            if command.get("status") in ("done", "error", "timeout"):
                break
            time.sleep(0.5)
        ctx.note("cmd.json", str(command))
        if command.get("status") != "done" or marker not in (command.get("stdout") or ""):
            return CaseResult("FAIL", reason=f"真板未回 marker（status={command.get('status')}）",
                              category="test", reason_code="tunnel_cmd_failed")
        exp = ctx.sw.run("wal", "export", "--from-seq", str(start_seq))
        tx = [r for r in exp.get("records") or []
              if r.get("dir") == "TX" and r.get("source") == "agent:rhwremote"]
        ctx.note("wal-tx.json", str(tx))
        if not tx:
            return CaseResult("FAIL", reason="WAL 無 source=agent:rhwremote 的 TX 記錄（穿隧道歸因遺失）",
                              category="test", reason_code="wal_source_attribution_lost")
        closed = ctx.sw.run("remote", "close", "all")
        ctx.note("close.json", str(closed))
        time.sleep(1)
        st = ctx.sw.run("remote", "status")
        if st.get("tunnels"):
            return CaseResult("FAIL", reason=f"close all 後 remote status 非空：{st.get('tunnels')}",
                              category="test", reason_code="tunnel_state_leak")
        leftovers = _registry_leftovers()
        if leftovers:
            return CaseResult("FAIL", reason=f"close all 後 state dir 殘留：{leftovers}",
                              category="test", reason_code="tunnel_state_leak")
        orphan = subprocess.run(["pgrep", "-af", f"ssh.*{ip}"],
                                capture_output=True, text=True).stdout.strip()
        if orphan:
            return CaseResult("FAIL", reason=f"close all 後殘留 ssh 行程：{orphan}",
                              category="test", reason_code="tunnel_orphan_ssh")
        pid_after = ctx.sw.run("daemon", "status").get("pid")
        if pid_after != pid_before:
            return CaseResult("FAIL", reason=f"daemon pid 變動（{pid_before}->{pid_after}）",
                              category="test", reason_code="daemon_touched_by_remote")
        return CaseResult("PASS")
    finally:
        _live_teardown(ctx, name)


@_case("rm-live-orphan", "kill -9 隧道 ssh→remote status prune 自癒→重開成功",
       hints=_LIVE_HINTS, requires=("docker", "two_boards", "remote_capability"))
def rm_live_orphan(ctx):
    err = _ensure_image(ctx)
    if err:
        return CaseResult("FAIL", reason=err, category="environment",
                          reason_code="docker_build_failed")
    ctx.sw.run("remote", "close", "all")
    pid_before = ctx.sw.run("daemon", "status").get("pid")
    name = ""
    try:
        name, ip, key = _start_ssh_peer(ctx, "orphan")
        if not ip or key is None:
            return CaseResult("FAIL", reason="sshd 對端容器未就緒（docker run/sshd/金鑰）",
                              category="environment", reason_code="sshd_unavailable")
        opened = _remote_open(ctx, ip, key)
        ctx.note("open.json", str(opened))
        ssh_pid = opened.get("pid")
        if not opened.get("ok") or opened.get("status") != "active" or not ssh_pid:
            return CaseResult("FAIL",
                              reason=f"remote -R expose 未 active（{opened.get('error_code') or opened.get('status')}）",
                              category="test", reason_code="tunnel_open_failed")
        os.kill(int(ssh_pid), 9)  # 模擬 ssh 崩潰（SIGKILL 不走正常拆除）
        time.sleep(1)
        st = ctx.sw.run("remote", "status")  # status 應就地 prune 死 state＋cm/log
        ctx.note("status-after-kill.json", str(st))
        if st.get("tunnels"):
            return CaseResult("FAIL", reason=f"kill -9 後 remote status 未 prune：{st.get('tunnels')}",
                              category="test", reason_code="tunnel_prune_failed")
        leftovers = _registry_leftovers()
        if leftovers:
            return CaseResult("FAIL", reason=f"prune 後 state dir 殘留：{leftovers}",
                              category="test", reason_code="tunnel_prune_failed")
        reopened = _remote_open(ctx, ip, key)  # 自癒：孤兒清掉後同 identity 重開成功
        ctx.note("reopen.json", str(reopened))
        if not reopened.get("ok") or reopened.get("status") != "active":
            return CaseResult("FAIL",
                              reason=f"prune 後重開失敗（{reopened.get('error_code') or reopened.get('status')}）",
                              category="test", reason_code="tunnel_reopen_failed")
        ctx.sw.run("remote", "close", "all")
        time.sleep(1)
        if ctx.sw.run("remote", "status").get("tunnels") or _registry_leftovers():
            return CaseResult("FAIL", reason="收尾 close all 後仍有殘留",
                              category="test", reason_code="tunnel_state_leak")
        pid_after = ctx.sw.run("daemon", "status").get("pid")
        if pid_after != pid_before:
            return CaseResult("FAIL", reason=f"daemon pid 變動（{pid_before}->{pid_after}）",
                              category="test", reason_code="daemon_touched_by_remote")
        return CaseResult("PASS")
    finally:
        _live_teardown(ctx, name)


@_case("rm-live-cycle", "open/close ×5 registry 不累積、daemon 零觸碰",
       hints=_LIVE_HINTS, requires=("docker", "two_boards", "remote_capability"))
def rm_live_cycle(ctx):
    err = _ensure_image(ctx)
    if err:
        return CaseResult("FAIL", reason=err, category="environment",
                          reason_code="docker_build_failed")
    ctx.sw.run("remote", "close", "all")
    pid_before = ctx.sw.run("daemon", "status").get("pid")
    name = ""
    try:
        name, ip, key = _start_ssh_peer(ctx, "cycle")
        if not ip or key is None:
            return CaseResult("FAIL", reason="sshd 對端容器未就緒（docker run/sshd/金鑰）",
                              category="environment", reason_code="sshd_unavailable")
        for i in range(5):
            opened = _remote_open(ctx, ip, key)
            if not opened.get("ok") or opened.get("status") != "active":
                ctx.note(f"round{i}-open.json", str(opened))
                return CaseResult("FAIL",
                                  reason=f"第 {i + 1} 輪 open 未 active（{opened.get('error_code') or opened.get('status')}）",
                                  category="test", reason_code="tunnel_open_failed")
            st = ctx.sw.run("remote", "status")
            if len(st.get("tunnels") or []) != 1:
                return CaseResult("FAIL", reason=f"第 {i + 1} 輪 status 隧道數≠1：{st.get('tunnels')}",
                                  category="test", reason_code="tunnel_state_leak")
            ctx.sw.run("remote", "close", "all")
            time.sleep(1)
            st = ctx.sw.run("remote", "status")
            if st.get("tunnels"):
                return CaseResult("FAIL", reason=f"第 {i + 1} 輪 close 後 status 非空：{st.get('tunnels')}",
                                  category="test", reason_code="tunnel_state_leak")
        leftovers = _registry_leftovers()
        if leftovers:
            return CaseResult("FAIL", reason=f"5 輪後 state dir 殘留：{leftovers}",
                              category="test", reason_code="tunnel_state_leak")
        pid_after = ctx.sw.run("daemon", "status").get("pid")
        if pid_after != pid_before:
            return CaseResult("FAIL", reason=f"daemon pid 變動（{pid_before}->{pid_after}）",
                              category="test", reason_code="daemon_touched_by_remote")
        return CaseResult("PASS")
    finally:
        _live_teardown(ctx, name)
```

- [ ] **Step 3: 冒煙驗證**：

```bash
python3 -m realhw --list | grep "\[remote\]"
python3 -m realhw --list | wc -l
python3 -m pytest -q tests/test_realhw_harness.py tests/test_realhw_drivers.py tests/test_realhw_preflight.py
```

預期：7 行 remote case（rm-topo×4＋rm-live×3）；總數 `36`；`44 passed`。

- [ ] **Step 4: commit**（`feat(realhw): rm-live×3——部署 daemon＋真板穿隧道 e2e/orphan 自癒/open-close 循環`＋trailer）。

---
### Task 16: 文件對齊（checklist／README）＋changelog fragment（R-09／R-18）

**Files:** Modify `docs/func-test/realhw-stability-checklist.md`、`README.md`；Create `changelog.d/reliability-phase1-realhw.md`

- [ ] **Step 1: `docs/func-test/realhw-stability-checklist.md` 四處更新**：

(1a) 檔頭第 4 行 case 數句改為：

```markdown
> case id 與 `python3 -m realhw --list` 完全一致（P0×8＋P1×20＋remote×7＋longrun×1，共 36 條）。
```

(1b) 「本機環境基準」表尾加一列：

```markdown
| Windows 端 serialwrap.exe | `realhw/config.json` 的 `win_serialwrap_exe`（`/mnt/c/...`；空＝不可用，hp 救援鏈與 windows_daemon 診斷降級） |
```

(1c) 「前置作業」章開頭段落後補兩級判決說明（原六項不動，追加）：

```markdown
### E. 兩級判決新增項（reliability plugin Phase 1）

**suite-refuse 追加**：
- `benchlock`：取得 `~/.local/state/serialwrap/bench.lock` flock 且 `pgrep -af 'testpilot ru[n]'` 無進行中的外部 testpilot run——reliability 與 wifi_llapi 不可同跑（本套件會 restart daemon／拔 USB），拿不到即整場拒跑。
- `windows_daemon` 診斷：`WinSwCli` 探測 Windows 端 serialwrapd（存在＋持有清單烙進 run meta）；Windows 端持有目標裝置時，「兩板 READY」缺項訊息歸因 `windows_daemon_holds_device`（0718 根因：Windows 端 exclusive handle → usbipd 拒絕匯出；救法＝Windows 端 `serialwrap.exe device release`）。

**family-gate（capabilities，缺項不擋整場、對應 case 執行期 SKIP＝FailEnv）**：
- `remote_capability`：部署 CLI 有 `remote` 子命令（`serialwrap remote status` 回 ok）→ 缺＝rm-live 族 SKIP（`remote_capability_missing`）。
- `deployed_recent`：部署版本 ≥0.2.3（`serialwrap --version`）→ 缺＝宣告該 requires 的 case SKIP（`deployed_daemon_stale`）。
- `docker`：docker CLI＋daemon 可達 → 缺＝remote 族 SKIP（`docker_unavailable`）。image 建置延遲到第一個 rm-topo case。
```

(1d) 「長跑」章之前插入 remote 族章：

```markdown
## remote 族（tier=remote，7 條，全非破壞性；`--tier remote` 顯式指定）

驗 `serialwrap remote`（PR #143）的部署後實機面。第一層 rm-topo 包裝 `tools/docker/remote_tunnel_test.sh`（容器封閉世界＋假 UART，驗工具鏈）；第二層 rm-live 對部署 daemon＋真板（容器只當 ssh 對端）。

| case | 內容 | 手動等效 |
|---|---|---|
| rm-topo-direct | direct：-R expose＋-L connect＋close/prune 全流程 | `bash tools/docker/remote_tunnel_test.sh direct` |
| rm-topo-nat-host | NAT→host relay＋攻擊者容器隔離 | `bash tools/docker/remote_tunnel_test.sh nat_host` |
| rm-topo-dual-nat | 雙 NAT relay＋兩側繞行隔離 | `bash tools/docker/remote_tunnel_test.sh dual_nat` |
| rm-topo-gwports | GatewayPorts/--remote-socket fail-closed | `bash tools/docker/remote_tunnel_test.sh gwports` |
| rm-live-e2e | host `-R` expose 至容器→容器內 `--endpoint tcp://127.0.0.1:7777` session list＋`cmd submit COM0 echo <marker>`→真板回 marker、WAL source=`agent:rhwremote`→close 後 registry/log 淨空、無孤兒 ssh、daemon pid 全程不變 | `serialwrap remote tester@<容器IP>:7777` 後於容器內操作 |
| rm-live-orphan | `kill -9` 隧道 ssh→`remote status` prune 自癒→重開成功 | `kill -9 <ssh pid>; serialwrap remote status` |
| rm-live-cycle | open/close ×5 registry 不累積、daemon 零觸碰 | 迴圈 `serialwrap remote ...` / `remote close all` |

requires：rm-topo→docker；rm-live→docker＋two_boards＋remote_capability（缺項執行期 SKIP＝FailEnv）。容器前綴：rm-topo＝`sw-rt-*`（script 自有）、rm-live＝`rhwlive-*`，各自 teardown。
```

(1e) 報表描述處（若有 verdict 欄敘述）補一句：report.md/report.json 增 `分類`（category/reason_code）欄——分類裁決線見 openspec `serialwrap-reliability-plugin`。

- [ ] **Step 2: `README.md` 兩處同步**（英文段 ~L674 與繁中段 ~L1677，內容保持一致）。英文段替換為：

```markdown
python3 -m realhw --tier p0,p1                    # P0 smoke (×8) + P1 core stability (×20)
python3 -m realhw --tier remote                   # remote tunnel real-hw family (×7, needs docker)
python3 -m realhw --tier longrun --duration 48h   # unattended long run (default 32h when omitted)
```

繁中段替換為：

```markdown
python3 -m realhw --tier p0,p1                    # P0 煙霧（×8）＋P1 核心穩定性（×20）
python3 -m realhw --tier remote                   # remote 隧道實機族（×7，需 docker）
python3 -m realhw --tier longrun --duration 48h   # 長跑無人看護（省略 --duration 時預設 32h）
```

- [ ] **Step 3: Create `changelog.d/reliability-phase1-realhw.md`**（無對應 issue 號——沿 repo 既例用描述性 slug，如 `remote-tunnel-cli.md`；PR body 引用 openspec change 名，若要引 #122/#143 必須 closing-keyword 或上 `policy-exempt:issue-link`）：

```markdown
---
type: feat
scope: realhw
---
realhw 實機穩定性套件 Phase 1 擴充（reliability testpilot plugin 前置，openspec `serialwrap-reliability-plugin`）：`CaseResult` 增 `category`/`reason_code` 分類欄（environment|session|configuration|test|空=Inconclusive，向後相容）且既有 29 case 逐案標註、報表增分類欄；preflight 兩級判決——suite-refuse 新增 `benchlock`（flock `~/.local/state/serialwrap/bench.lock`＋pgrep 外部 `testpilot run` 互斥）與 `windows_daemon` 診斷（`WinSwCli` 經 `/mnt/c` 探測 Windows 端 serialwrapd 持有、READY 缺項歸因 `windows_daemon_holds_device`），family-gate 新增 capabilities（remote 子命令／部署版本 ≥0.2.3／docker 可達→對應 case 執行期 SKIP=FailEnv）；`p1-hp-cycle` 內建 Windows 端自動救援鏈（純決策 `plan_hp_rescue`：probe→device release→attach 重試≤2→fail attended，0718 雙 daemon 根因）；新 tier `remote` 7 case——`rm-topo-*`×4 逐拓樸包裝 `tools/docker/remote_tunnel_test.sh`（該 script 增 `$1` 分派參數、預設 all 向後相容）＋`rm-live-*`×3（部署 daemon＋真板穿隧道 e2e／orphan 自癒／open-close 循環）；`realhw.load_cfg()` 雙來源 loader（config.json／dict 注入）與 config 新欄 `win_serialwrap_exe` 供 Phase 2 plugin 消費。
```

- [ ] **Step 4: 驗證＋commit**：

```bash
python3 -m pytest -q tests/test_realhw_harness.py tests/test_realhw_drivers.py tests/test_realhw_preflight.py
git add docs/func-test/realhw-stability-checklist.md README.md changelog.d/reliability-phase1-realhw.md
git commit -m "docs(realhw): remote 族/兩級判決/分類欄對齊 checklist 與 README＋changelog fragment

Co-authored-by: Copilot <223556219+Copilot@users.noreply.github.com>"
```

預期：`44 passed`。

---

### Task 17: 全量驗證＋真機驗收（收尾，openspec tasks 7）

**Files:** 無新增（驗證與真機執行）

- [ ] **Step 1: 全套 pytest**（實作 agent 執行）：

```bash
python3 -m pytest -q tests/
```

預期：與 base 相比**無新增失敗**。既知例外（非本 PR 責任）：`test_multiagent_e2e.py::TestMultiAgentE2E::test_five_agents_three_rounds_no_conflict`（pre-existing）；`t8_full_run_simulation`／`test_t1_wal_reset_preserves_console` 偶發 flaky（重跑收斂）；與其他 suite 並跑時 PTY-heavy 6 檔競態（單獨重跑判別）。

- [ ] **Step 2: policy check**（實作 agent 執行；本地不帶 PR 參數是假綠，開 PR 前帶參複現 CI）：

```bash
python3 -m policy_check --repo .
# 開 PR 前（標題/內文按實際 PR 填）：
python3 -m policy_check --repo . \
  --pr-title "feat(realhw): reliability plugin Phase 1——分類欄/兩級判決/hp 救援鏈/remote 族" \
  --pr-body "$(cat /tmp/pr-body.md)" \
  --pr-base-ref main --pr-head-ref feature/serialwrap-reliability-plugin
```

預期：PASS（R-09 由 Task 16 fragment 滿足；R-18 由 checklist/README 滿足；R-21 無 bench home 絕對路徑——若 grep `/home/`，只允許容器 namespace 的既有腳本內容；本 plan 不再直寫任何實際 home 字面）。

- [ ] **Step 3:【真機-人工閘】營運前置 redeploy 0.2.3+remote**（操作者執行；spec §6）：

```bash
git fetch origin && git rev-list --count HEAD..origin/main   # 確認基準新鮮
./install.sh --system --with-sudo
# 坑：setup report transitioned:false 不自動重啟——必須手動：
sudo systemctl restart serialwrap
serialwrap --version && serialwrap doctor && serialwrap remote status
```

預期：version ≥0.2.3、doctor 全綠、`remote status` 回 `{"ok":true,"tunnels":[]}`。

- [ ] **Step 4:【真機-人工閘】standalone 驗收**（操作者執行，依序）：
  1. `python3 -m realhw --only p0-doctor`＋任一 PASS case → report.md 分類欄呈現正確（PASS 為 `-`）；`--only p1-cmd-file` 若 SKIP → 分類欄 `environment/base64_missing`（openspec tasks 1.3）。
  2. `python3 -m realhw --only p1-hp-cycle` → 含 Windows 端持有情境（見 Task 11 Step 4）。
  3. `python3 -m realhw --tier remote` → 7 case 全綠（docker 需可達；rm-live 需兩板 READY）。
  4. 補驗 capabilities 解鎖案：`python3 -m realhw --only p1-rst-reboot`、`--only p1-rst-bootwindow`（redeploy ≥0.2.3 後不再 SKIP）。
  5. benchlock 實測：另開 shell `python3 -c "import time,os,fcntl,pathlib;fd=os.open(pathlib.Path.home()/'.local/state/serialwrap/bench.lock',os.O_RDWR|os.O_CREAT);fcntl.flock(fd,fcntl.LOCK_EX);time.sleep(120)"` 持鎖，再跑 `python3 -m realhw --tier p0` → 期望整場拒跑並印 benchlock 缺項。
  6. 結果（PASS/FAIL＋報告目錄）記回本 checkbox。

- [ ] **Step 5: 收尾整理**：worktree 內 `git log --oneline main..HEAD` 確認 commit 序列完整；**不由實作 agent 開 PR**——push／PR（含 R-11 checklist、必要 label）由主 session 依 preflight-ci 流程統一處理。

---

## Spec 覆蓋自查表（openspec `specs/realhw-stability-suite/spec.md` delta → tasks）

| Delta Requirement | 對應 Task |
|---|---|
| tier 化 case 執行與選擇（MODIFIED：`--tier remote`、7 case、不被 p0/p1 隱含、不進 wheel/pytest） | Task 9（help）、14、15；wheel/pytest 契約沿用 #122 現況 |
| preflight 守門（MODIFIED：兩級判決——六項照舊＋benchlock suite-refuse；capabilities family-gate；WinSwCli 探測＋READY 缺項歸因；破壞性預告照舊） | Task 4、5、6、7、8、9 |
| 結果分類（ADDED：category/reason_code、裁決線、report 呈現、SKIP 分類化 scenario） | Task 1、2、3（config）、17 Step 4-1（真機 scenario） |
| remote 隧道實機驗證（ADDED：rm-topo×4 逐拓樸包裝＋`$1` 參數；rm-live×3 e2e/orphan/cycle；requires；image 延遲建置；前綴/teardown 分離） | Task 12、13、14、15、17 Step 4-3 |
| hp-cycle Windows 端自動救援（ADDED：純決策函式＋執行分離、release→retry≤2、FailEnv+attended、broken_by 沿用） | Task 10、11、17 Step 4-2 |

openspec `tasks.md` 群組對照：1a→Tasks 1-2（＋17 Step 4-1 即 tasks 1.3）；1b→Tasks 3-9（tasks 2.1-2.4）；1c→Tasks 10-11（tasks 3.1-3.3）；1d→Tasks 12-15（tasks 4.1-4.5）＋17 Step 3/4-3（tasks 4.6）；收尾 7→Tasks 16-17。群組 5/6（Phase 2 plugin）不在本 plan。
