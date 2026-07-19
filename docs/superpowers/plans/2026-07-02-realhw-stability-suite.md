# #122 實機穩定性測試套件 Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 建立手動觸發、無人在場的實機穩定性套件 `python3 -m realhw`：P0 煙霧＋P1 核心穩定性全自動、長跑（預設 32h）無人看護＋事後分析，報告落 `~/b-log/realhw-reports/`。

**Architecture:** 獨立 stdlib-only Python harness（`realhw/`，不入 wheel、不被 pytest 收集）：Case registry＋preflight＋continue-on-failure 執行引擎＋JSON/MD 報告；drivers 以 subprocess 包已安裝 `serialwrap` CLI／tmux／usbipd／systemd。**測部署後系統**——不 import sw_core。權威設計＝`docs/superpowers/specs/2026-07-02-realhw-stability-suite-design.md`（含完整 case 目錄與坑）；OpenSpec＝`openspec/changes/archive/2026-07-19-realhw-stability-suite-122/`（實作 PR 內已歸檔）。

**Tech Stack:** Python 3.10+ stdlib（dataclasses/subprocess/json/argparse）、PyYAML（config，runtime 既有依賴）、tmux、usbipd-win、systemd（NOPASSWD sudo）。

**執行環境注意：**
- 工作區：repo 根下 worktree `.worktrees/122-realhw-stability-suite`（分支 `feature/122-realhw-stability-suite`）。開工前 `git branch --show-current` 確認。
- 單元測試（Tasks 1-5 的純邏輯）在 `tests/` 用 pytest 正常跑——#120 conftest 防線已上線，直接 `python3 -m pytest -q tests/test_realhw_*.py`。
- **實機驗收步驟（Tasks 6-10 的 `--tier` 實跑）操作 live daemon 與真板**：跑之前確認沒有其他 pytest 在跑；兩板（COM0=dut-prpl/AC01QZT0、COM1=sta-prpl/AQ00OAQ7）READY。破壞性 case 會 reboot 真板、restart daemon、usbipd 插拔——這是套件目的，但失敗時務必把板子恢復 READY 再繼續（`serialwrap device attach` / `sudo systemctl restart serialwrap`）。
- **R-21 陷阱**：任何進 repo 的檔案不得含 `/home/<user>/` 絕對路徑字面值（policy secret-scan 會 FAIL）——一律 `~` 或 `Path.home()`。
- Commit 一律 Conventional Commits 繁中＋雙 trailer：
  ```
  Co-authored-by: Copilot <223556219+Copilot@users.noreply.github.com>
  Co-Authored-By: Claude Fable 5 <noreply@anthropic.com>
  ```

## File Structure

| 檔案 | 職責 |
|---|---|
| `realhw/__init__.py` | 空（package 標記） |
| `realhw/__main__.py` | argparse CLI、載 config、preflight、跑選中 cases、寫報告、exit code |
| `realhw/harness.py` | `Case`/`CaseResult`/`Ctx`、registry＋過濾、`parse_duration`、報告產生（json/md） |
| `realhw/drivers.py` | `SwCli`/`TmuxCtl`/`Usbipd`/`Systemd` 薄包裝＋`strip_ansi`/`parse_usbipd_list` 純函式 |
| `realhw/preflight.py` | 六項檢查（純判定函式＋收集器） |
| `realhw/cases/__init__.py` | import 各 case 模組觸發註冊 |
| `realhw/cases/p0.py` | P0×8 |
| `realhw/cases/p1_console.py` | P1 console×7 |
| `realhw/cases/p1_cmd.py` | P1 cmd×3 |
| `realhw/cases/p1_wal.py` | P1 WAL×2 |
| `realhw/cases/p1_restart.py` | P1 重啟×4（destructive 排尾） |
| `realhw/cases/p1_handoff.py` | P1 交接×2 |
| `realhw/cases/p1_hotplug.py` | P1 插拔×2 |
| `realhw/cases/longrun.py` | 長跑編排＋分析器 |
| `realhw/config.yaml` | 本機組態（boards serial/com/alias、usbipd_exe、tmux_prefix、timeouts） |
| `tests/test_realhw_harness.py` | registry/過濾/duration/報告 單測 |
| `tests/test_realhw_drivers.py` | usbipd 解析/ANSI 剝除/marker 斷言 單測 |
| `tests/test_realhw_preflight.py` | preflight 判定邏輯 單測 |
| `tests/test_realhw_longrun.py` | 長跑分析器 單測（合成 log） |
| `docs/func-test/realhw-stability-checklist.md` | 人可讀清單（P0/P1 對照＋P2 手動） |
| `changelog.d/122-realhw-stability-suite.md` | R-09 fragment |

pytest 不收集 `realhw/`：測試檔全在 `tests/`，`realhw/` 無 `test_*.py`，天然不被收集（不需額外設定；勿在 pyproject 加 pytest 段）。

---

### Task 1: harness 核心（Case/registry/過濾/duration，TDD）

**Files:** Create `realhw/__init__.py`、`realhw/harness.py`、`tests/test_realhw_harness.py`

- [ ] **Step 1: RED 測試**

```python
# tests/test_realhw_harness.py
"""#122 realhw harness 純邏輯單測（不碰 live）。"""
from __future__ import annotations

import pytest

from realhw import harness


def _mk(id, tier="p0", destructive=False):
    return harness.Case(id=id, tier=tier, title=id, run=lambda ctx: harness.CaseResult("PASS"),
                        destructive=destructive)


def test_select_by_tier_excludes_longrun():
    reg = [_mk("a", "p0"), _mk("b", "p1"), _mk("c", "longrun")]
    got = harness.select_cases(reg, tiers=["p0", "p1"], only=None, skip=[])
    assert [c.id for c in got] == ["a", "b"]  # longrun 絕不被 p0/p1 隱含


def test_select_only_overrides_tier():
    reg = [_mk("a", "p0"), _mk("b", "p1")]
    got = harness.select_cases(reg, tiers=["p0"], only="b", skip=[])
    assert [c.id for c in got] == ["b"]


def test_select_skip():
    reg = [_mk("a", "p1"), _mk("b", "p1")]
    got = harness.select_cases(reg, tiers=["p1"], only=None, skip=["a"])
    assert [c.id for c in got] == ["b"]


def test_select_unknown_only_raises():
    with pytest.raises(harness.UnknownCaseError):
        harness.select_cases([_mk("a")], tiers=["p0"], only="nope", skip=[])


@pytest.mark.parametrize("s,secs", [("32h", 115200), ("45m", 2700), ("3600s", 3600), ("2h", 7200)])
def test_parse_duration(s, secs):
    assert harness.parse_duration(s) == secs


def test_parse_duration_rejects_garbage():
    with pytest.raises(ValueError):
        harness.parse_duration("soon")
```

- [ ] **Step 2: 確認 RED**：`python3 -m pytest -q tests/test_realhw_harness.py`，Expected: `ModuleNotFoundError: No module named 'realhw'`。
- [ ] **Step 3: 實作**

```python
# realhw/__init__.py
"""#122 實機穩定性套件——測部署後系統；禁 import sw_core。"""
```

```python
# realhw/harness.py
"""Case 模型、registry 與過濾、duration 解析、報告產生。純邏輯（可單測）。"""
from __future__ import annotations

import dataclasses
import json
import re
import time
from pathlib import Path
from typing import Any, Callable


class UnknownCaseError(Exception):
    pass


@dataclasses.dataclass
class CaseResult:
    verdict: str  # PASS | FAIL | SKIP
    reason: str = ""
    evidence: dict[str, str] = dataclasses.field(default_factory=dict)
    duration_s: float = 0.0


@dataclasses.dataclass(frozen=True)
class Case:
    id: str
    tier: str  # p0 | p1 | longrun
    title: str
    run: Callable[[Any], CaseResult]
    destructive: bool = False
    requires: tuple[str, ...] = ()
    hints: tuple[str, ...] = ()


REGISTRY: list[Case] = []


def register(case: Case) -> Case:
    if any(c.id == case.id for c in REGISTRY):
        raise ValueError(f"duplicate case id: {case.id}")
    REGISTRY.append(case)
    return case


def select_cases(registry: list[Case], *, tiers: list[str], only: str | None, skip: list[str]) -> list[Case]:
    if only is not None:
        hit = [c for c in registry if c.id == only]
        if not hit:
            raise UnknownCaseError(only)
        return hit
    unknown = [s for s in skip if not any(c.id == s for c in registry)]
    if unknown:
        raise UnknownCaseError(",".join(unknown))
    return [c for c in registry if c.tier in tiers and c.id not in skip]


_DUR = re.compile(r"^(\d+)([hms])$")


def parse_duration(text: str) -> int:
    m = _DUR.match(text.strip())
    if not m:
        raise ValueError(f"duration 格式須為 <N>h/<N>m/<N>s：{text!r}")
    n, unit = int(m.group(1)), m.group(2)
    return n * {"h": 3600, "m": 60, "s": 1}[unit]
```

- [ ] **Step 4: 確認 GREEN**：同命令，Expected: 全 PASS。
- [ ] **Step 5: Commit** `feat(realhw): harness 核心——Case/registry/tier 過濾/duration 解析（#122）`

---

### Task 2: 報告產生器（TDD）

**Files:** Modify `realhw/harness.py`、`tests/test_realhw_harness.py`（追加）

- [ ] **Step 1: RED 追加**

```python
def test_report_md_lists_all_and_details_failures(tmp_path):
    results = [
        ("p0-doctor", harness.CaseResult("PASS", duration_s=1.2)),
        ("p1-con-fanout", harness.CaseResult("FAIL", reason="marker 未出現",
                                             evidence={"pane": "p1-con-fanout/pane.txt"})),
        ("p1-hp-cycle", harness.CaseResult("SKIP", reason="前置不滿足：COM1 非 READY")),
    ]
    hints = {"p1-con-fanout": ("先確認 console 沒掉回 line-buffer",)}
    meta = {"version": "0.2.2", "git": "abc123", "tiers": "p0,p1", "started_at": "2026-07-02T10:00:00"}
    md = harness.render_report_md(meta, results, hints)
    assert "PASS: 1" in md and "FAIL: 1" in md and "SKIP: 1" in md
    assert "p1-con-fanout" in md and "marker 未出現" in md
    assert "先確認 console 沒掉回 line-buffer" in md          # 診斷提示進報告
    assert "p1-con-fanout/pane.txt" in md                      # evidence 連結

    harness.write_reports(tmp_path, meta, results, hints)
    data = json.loads((tmp_path / "report.json").read_text())
    assert data["meta"]["git"] == "abc123"
    assert data["results"][1]["verdict"] == "FAIL"
    assert (tmp_path / "report.md").exists()
```

（測試檔頂補 `import json`。）

- [ ] **Step 2: RED**：`AttributeError: ... has no attribute 'render_report_md'`。
- [ ] **Step 3: 實作（harness.py 追加）**

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
        "| case | verdict | 時間(s) | 說明 |",
        "|---|---|---|---|",
    ]
    for cid, r in results:
        lines.append(f"| {cid} | {r.verdict} | {r.duration_s:.1f} | {r.reason} |")
    fails = [(cid, r) for cid, r in results if r.verdict == "FAIL"]
    if fails:
        lines += ["", "## 失敗案例"]
        for cid, r in fails:
            lines += ["", f"### {cid}", f"- 原因：{r.reason}"]
            for h in hints.get(cid, ()):
                lines.append(f"- 提示：{h}")
            for k, v in r.evidence.items():
                lines.append(f"- evidence：[{k}]({v})")
    return "\n".join(lines) + "\n"


def write_reports(report_dir: Path, meta: dict[str, Any], results: list[tuple[str, CaseResult]],
                  hints: dict[str, tuple[str, ...]]) -> None:
    report_dir.mkdir(parents=True, exist_ok=True)
    payload = {"meta": meta, "results": [
        {"id": cid, "verdict": r.verdict, "reason": r.reason,
         "duration_s": r.duration_s, "evidence": r.evidence} for cid, r in results]}
    (report_dir / "report.json").write_text(
        json.dumps(payload, ensure_ascii=False, indent=1), encoding="utf-8")
    (report_dir / "report.md").write_text(render_report_md(meta, results, hints), encoding="utf-8")
```

- [ ] **Step 4: GREEN**＋**Step 5: Commit** `feat(realhw): 報告產生器 report.json/report.md（#122）`

---

### Task 3: drivers（解析純函式 TDD＋subprocess 薄包裝）

**Files:** Create `realhw/drivers.py`、`tests/test_realhw_drivers.py`

- [ ] **Step 1: RED 測試**

```python
# tests/test_realhw_drivers.py
"""#122 drivers 純函式單測（不碰 live）。"""
from __future__ import annotations

from realhw import drivers

USBIPD_LIST = """Connected:
BUSID  VID:PID    DEVICE                        STATE
8-1    0403:6001  USB Serial Converter          Attached
8-2    0403:6001  USB Serial Converter          Attached

Persisted:
GUID  DEVICE
"""


def test_parse_usbipd_list_maps_busids():
    got = drivers.parse_usbipd_list(USBIPD_LIST)
    assert got == ["8-1", "8-2"]


def test_strip_ansi():
    assert drivers.strip_ansi("a\x1b[31mred\x1b[0mb\x1b(B") == "aredb"


def test_find_marker_ignores_ansi_and_wraps():
    pane = "prompt$ echo MARK_42\r\n\x1b[1mMARK_42\x1b[0m\r\nprompt$"
    assert drivers.find_marker(pane, "MARK_42")
    assert not drivers.find_marker(pane, "MARK_99")
```

- [ ] **Step 2: RED**（ModuleNotFoundError）→ **Step 3: 實作**

```python
# realhw/drivers.py
"""subprocess 薄包裝＋純解析函式。禁 import sw_core（測部署後系統）。"""
from __future__ import annotations

import json
import os
import re
import shlex
import subprocess
import time
from pathlib import Path

_ANSI = re.compile(r"\x1b(?:\[[0-9;?]*[A-Za-z]|\([A-Za-z]|[=>])")


def strip_ansi(text: str) -> str:
    return _ANSI.sub("", text)


def find_marker(pane_text: str, marker: str) -> bool:
    return marker in strip_ansi(pane_text)


def parse_usbipd_list(output: str) -> list[str]:
    """回傳 Connected 段的 BUSID 清單（序列裝置歸屬由 config 的 serial↔busid 對照另行判定）。"""
    busids: list[str] = []
    in_connected = False
    for line in output.splitlines():
        if line.startswith("Connected:"):
            in_connected = True
            continue
        if line.startswith("Persisted:"):
            break
        if in_connected:
            m = re.match(r"^(\d+-\d+)\s", line)
            if m:
                busids.append(m.group(1))
    return busids


def _run(argv: list[str], timeout: float = 30.0) -> subprocess.CompletedProcess:
    return subprocess.run(argv, capture_output=True, text=True, timeout=timeout)


class SwCli:
    """已安裝 serialwrap CLI 的薄包裝；stdout 嘗試 JSON 解析。"""

    def run(self, *args: str, timeout: float = 30.0) -> dict:
        cp = _run(["serialwrap", *args], timeout=timeout)
        out = cp.stdout.strip()
        try:
            data = json.loads(out) if out else {}
        except json.JSONDecodeError:
            data = {"_raw": out}
        data["_rc"] = cp.returncode
        data["_stderr"] = cp.stderr.strip()
        return data

    def sessions(self) -> list[dict]:
        return self.run("session", "list").get("sessions") or []

    def session(self, com: str) -> dict:
        for s in self.sessions():
            if s.get("com") == com:
                return s
        return {}

    def submit_and_wait(self, com: str, cmd: str, *, cmd_timeout: float = 12.0,
                        settle_s: float = 1.5, poll_s: float = 0.5) -> dict:
        """cmd submit → 輪詢 cmd status 到 done（坑：submit 後立刻讀有 line race）。"""
        sub = self.run("cmd", "submit", "--selector", com, "--cmd", cmd,
                       "--cmd-timeout", str(cmd_timeout))
        cmd_id = sub.get("cmd_id")
        if not cmd_id:
            return {"_error": "submit 未回 cmd_id", **sub}
        deadline = time.monotonic() + cmd_timeout + 10
        time.sleep(settle_s)
        while time.monotonic() < deadline:
            st = self.run("cmd", "status", "--cmd-id", str(cmd_id))
            command = st.get("command") or {}
            if command.get("status") in ("done", "error", "timeout"):
                return command
            time.sleep(poll_s)
        return {"_error": "cmd status 輪詢逾時", "cmd_id": cmd_id}

    def wait_state(self, com: str, want: str, *, timeout_s: float, poll_s: float = 2.0) -> bool:
        deadline = time.monotonic() + timeout_s
        while time.monotonic() < deadline:
            if self.session(com).get("state") == want:
                return True
            time.sleep(poll_s)
        return False


class TmuxCtl:
    def __init__(self, prefix: str) -> None:
        self._prefix = prefix

    def name(self, tag: str) -> str:
        return f"{self._prefix}-{tag}-{os.getpid()}"

    def new(self, session: str, command: str) -> None:
        _run(["tmux", "new-session", "-d", "-s", session, command])

    def send(self, session: str, text: str, *, enter: bool = True) -> None:
        _run(["tmux", "send-keys", "-t", session, "-l", "--", text])
        if enter:
            _run(["tmux", "send-keys", "-t", session, "Enter"])

    def send_key(self, session: str, key: str) -> None:  # 例："Tab"、"Enter"
        _run(["tmux", "send-keys", "-t", session, key])

    def capture(self, session: str) -> str:
        return _run(["tmux", "capture-pane", "-p", "-t", session]).stdout

    def kill(self, session: str) -> None:
        _run(["tmux", "kill-session", "-t", session])


class Usbipd:
    def __init__(self, exe: str) -> None:
        self._exe = exe

    def list_busids(self) -> list[str]:
        return parse_usbipd_list(_run([self._exe, "list"]).stdout)

    def detach(self, busid: str) -> None:
        _run([self._exe, "detach", "-b", busid])

    def attach(self, busid: str) -> None:
        _run([self._exe, "attach", "-w", "-b", busid], timeout=60)


class Systemd:
    UNIT = "serialwrap"

    def restart(self) -> int:
        return _run(["sudo", "-n", "systemctl", "restart", self.UNIT]).returncode

    def main_pid(self) -> int:
        out = _run(["systemctl", "show", "-p", "MainPID", self.UNIT]).stdout
        try:
            return int(out.strip().split("=", 1)[1])
        except (IndexError, ValueError):
            return 0
```

- [ ] **Step 4: GREEN**：`python3 -m pytest -q tests/test_realhw_drivers.py tests/test_realhw_harness.py` 全 PASS。
- [ ] **Step 5: Commit** `feat(realhw): drivers——swcli/tmux/usbipd/systemd 薄包裝＋解析純函式（#122）`

> 注意：usbipd `list` 輸出的欄位在不同版本會變——實作時先在本機跑一次 `usbipd.exe list` 比對真實輸出，必要時調 `parse_usbipd_list` 與測試樣本（樣本以真實輸出為準）。busid↔板卡 serial 的對照存 config（`8-1: AC01QZT0` 型），每輪跑前以 `list_busids` 驗 busid 存在。

---

### Task 4: `Ctx`＋執行引擎＋CLI＋config＋preflight

**Files:** Create `realhw/__main__.py`、`realhw/preflight.py`、`realhw/config.yaml`；Modify `realhw/harness.py`（Ctx＋run_cases）；Create `tests/test_realhw_preflight.py`

- [ ] **Step 1: preflight RED 測試**

```python
# tests/test_realhw_preflight.py
"""#122 preflight 判定邏輯單測——吃注入的檢查結果，不碰 live。"""
from __future__ import annotations

from realhw import preflight


def _checks(**over):
    base = dict(git_behind=0, doctor_ok=True, boards_ready=["COM0", "COM1"],
                boards_expected=["COM0", "COM1"], tools_missing=[],
                leaked_daemons=[], other_pytest=False, state_polluted=False)
    base.update(over)
    return preflight.Checks(**base)


def test_all_green_passes():
    ok, problems = preflight.evaluate(_checks())
    assert ok and problems == []


def test_missing_tool_fails():
    ok, problems = preflight.evaluate(_checks(tools_missing=["tmux"]))
    assert not ok and any("tmux" in p for p in problems)


def test_other_pytest_fails_with_mutex_reason():
    ok, problems = preflight.evaluate(_checks(other_pytest=True))
    assert not ok and any("live guard" in p for p in problems)


def test_boards_not_ready_fails():
    ok, problems = preflight.evaluate(_checks(boards_ready=["COM0"]))
    assert not ok and any("COM1" in p for p in problems)


def test_git_behind_warns_but_passes():
    ok, problems = preflight.evaluate(_checks(git_behind=3))
    assert ok and any("落後" in p for p in problems)  # 警告仍列出但不擋
```

- [ ] **Step 2: RED** → **Step 3: 實作**

```python
# realhw/preflight.py
"""preflight：收集（I/O）與判定（純函式）分離，判定可單測。"""
from __future__ import annotations

import dataclasses
import shutil
import subprocess


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


def evaluate(c: Checks) -> tuple[bool, list[str]]:
    problems: list[str] = []
    ok = True
    if c.git_behind > 0:
        problems.append(f"警告：本機落後 origin/main {c.git_behind} commits（不擋，但報告會記錄）")
    if not c.doctor_ok:
        ok = False
        problems.append("serialwrap doctor 未通過")
    missing = [b for b in c.boards_expected if b not in c.boards_ready]
    if missing:
        ok = False
        problems.append(f"板卡未 READY：{','.join(missing)}")
    for t in c.tools_missing:
        ok = False
        problems.append(f"工具不可用：{t}")
    if c.leaked_daemons:
        ok = False
        problems.append(f"殘留 daemon：{','.join(c.leaked_daemons)}（先清理，見 checklist 前置B）")
    if c.other_pytest:
        ok = False
        problems.append("偵測到 pytest 執行中——本套件與單元測試互斥（#120 live guard 會誤判本套件操作為 FAIL）")
    if c.state_polluted:
        ok = False
        problems.append("live state.json 含 /tmp/sw-* 污染哨兵（先清理）")
    return ok, problems


def collect(cfg: dict, sw, repo_root) -> Checks:
    """I/O 收集層——實作時逐項：
    git_behind：git -C <repo_root> fetch -q origin && git rev-list --count HEAD..origin/main
    doctor_ok：sw.run("doctor") 解析各 check 全綠
    boards_ready：sw.sessions() 中 state==READY 的 com；boards_expected 來自 cfg["boards"]
    tools_missing：shutil.which("tmux")、Path(cfg["usbipd_exe"]).exists()、sudo -n true
    leaked_daemons：pgrep -af 'sw-coexis[t]|sw-pytest-iso'（character class 防 self-match）
    other_pytest：pgrep -af 'pytest'（排除自身行程樹）
    state_polluted：讀 ~/.local/state/serialwrap/state.json，bindings 值含 "/tmp/" 即 True
    """
    ...
```

（`collect` 的 docstring 即實作規格——照條列逐項落地，回傳 `Checks`；`evaluate` 已由單測釘住。）

- [ ] **Step 4: harness 追加 `Ctx` 與 `run_cases`（執行引擎）**

```python
# realhw/harness.py 追加
@dataclasses.dataclass
class Ctx:
    cfg: dict
    report_dir: Path
    case_dir: Path
    sw: Any
    tmux: Any
    usbipd: Any
    systemd: Any

    def note(self, name: str, content: str) -> str:
        """寫 evidence 檔，回傳相對路徑（進 CaseResult.evidence）。"""
        self.case_dir.mkdir(parents=True, exist_ok=True)
        p = self.case_dir / name
        p.write_text(content, encoding="utf-8")
        return str(p.relative_to(self.report_dir))


def run_cases(cases: list[Case], ctx: Ctx, *, boards: list[str]) -> list[tuple[str, CaseResult]]:
    results: list[tuple[str, CaseResult]] = []
    broken_by: str | None = None
    for case in cases:
        ctx.case_dir = ctx.report_dir / case.id
        if broken_by and ("two_boards" in case.requires or case.destructive):
            results.append((case.id, CaseResult("SKIP", reason=f"前置不滿足（{broken_by} 後板卡未恢復）")))
            continue
        t0 = time.monotonic()
        try:
            r = case.run(ctx)
        except Exception as exc:  # case 內未捕捉例外＝FAIL，不中止套件
            r = CaseResult("FAIL", reason=f"未捕捉例外：{exc!r}")
        r.duration_s = time.monotonic() - t0
        results.append((case.id, r))
        # case 間恢復檢查：兩板 READY 才續跑依賴板卡的 case
        not_ready = [b for b in boards if ctx.sw.session(b).get("state") != "READY"]
        if not_ready:
            for b in not_ready:
                ctx.sw.run("device", "attach", "--selector", b)
            time.sleep(5)
            not_ready = [b for b in boards if not ctx.sw.wait_state(b, "READY", timeout_s=60)]
        if not_ready and broken_by is None:
            broken_by = case.id
    return results
```

- [ ] **Step 5: `realhw/__main__.py`**

```python
"""python3 -m realhw——實機穩定性套件入口。"""
from __future__ import annotations

import argparse
import datetime
import subprocess
import sys
from pathlib import Path

import yaml

from . import cases  # noqa: F401  # import 觸發 case 註冊
from . import drivers, harness, preflight


def main() -> int:
    ap = argparse.ArgumentParser(prog="realhw", description="serialwrap 實機穩定性套件（#122）")
    ap.add_argument("--tier", default="p0", help="p0|p1|longrun，逗號多選；longrun 必須顯式指定")
    ap.add_argument("--only")
    ap.add_argument("--skip", default="")
    ap.add_argument("--duration", default="32h", help="longrun 時長（<N>h/<N>m/<N>s）")
    ap.add_argument("--report-dir")
    ap.add_argument("--list", action="store_true")
    args = ap.parse_args()

    cfg = yaml.safe_load((Path(__file__).parent / "config.yaml").read_text())
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
    ctx = harness.Ctx(cfg=cfg, report_dir=report_dir, case_dir=report_dir,
                      sw=sw, tmux=drivers.TmuxCtl(cfg["tmux_prefix"]),
                      usbipd=drivers.Usbipd(cfg["usbipd_exe"]), systemd=drivers.Systemd())

    checks = preflight.collect(cfg, sw, Path(__file__).resolve().parent.parent)
    ok, problems = preflight.evaluate(checks)
    for p in problems:
        print(f"[preflight] {p}")
    if not ok:
        print("[preflight] 拒跑：缺項如上")
        return 2
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
    }
    results = harness.run_cases(selected, ctx, boards=boards)
    hints = {c.id: c.hints for c in selected}
    harness.write_reports(report_dir, meta, results, hints)
    print(f"[realhw] 完成：{report_dir}/report.md")
    return 1 if any(r.verdict == "FAIL" for _, r in results) else 0


if __name__ == "__main__":
    sys.exit(main())
```

- [ ] **Step 6: `realhw/config.yaml`**

```yaml
# 本機組態（#122）——機器特定值；R-21：勿寫絕對 home 路徑
boards:
  - com: COM0
    alias: dut-prpl
    serial: AC01QZT0
    busid: "8-1"     # 換線會變：每輪跑前 usbipd list 驗證存在
  - com: COM1
    alias: sta-prpl
    serial: AQ00OAQ7
    busid: "8-2"
usbipd_exe: /mnt/c/Program Files/usbipd-win/usbipd.exe
tmux_prefix: realhw
timeouts:
  ready_wait_s: 180
  reboot_wait_s: 300
  human_active_window_s: 60
longrun:
  snapshot_interval_s: 300
  agent_workers: 4
```

- [ ] **Step 7: 驗證**：`python3 -m pytest -q tests/test_realhw_preflight.py tests/test_realhw_harness.py` 全 PASS；`python3 -m realhw --list`（此時 cases/ 空、印空清單即可——先建 `realhw/cases/__init__.py` 空檔）。
- [ ] **Step 8: Commit** `feat(realhw): 執行引擎/preflight/CLI/config（#122）`

---

### Task 5: P0 cases（8 條，完整實作）

**Files:** Create `realhw/cases/p0.py`；Modify `realhw/cases/__init__.py`（`from . import p0`）

Case 模式（所有 case 檔共用；`register` 於 import 時執行）：

```python
# realhw/cases/p0.py
"""P0 煙霧（~15 分鐘）。全部非破壞性。"""
from __future__ import annotations

import random
import time
from pathlib import Path

from ..harness import Case, CaseResult, register
from ..drivers import find_marker, strip_ansi


def _case(id, title, hints=(), requires=(), destructive=False):
    def deco(fn):
        register(Case(id=id, tier="p0", title=title, run=fn,
                      destructive=destructive, requires=requires, hints=tuple(hints)))
        return fn
    return deco


@_case("p0-doctor", "doctor 全綠＋兩板 READY", requires=("two_boards",))
def p0_doctor(ctx):
    doc = ctx.sw.run("doctor")
    ctx.note("doctor.json", str(doc))
    bad = [c for c in (doc.get("checks") or []) if not c.get("ok")]
    if bad:
        return CaseResult("FAIL", reason=f"doctor 未過：{[c.get('name') for c in bad]}")
    for b in ctx.cfg["boards"]:
        s = ctx.sw.session(b["com"])
        if s.get("state") != "READY":
            return CaseResult("FAIL", reason=f"{b['com']} 非 READY（{s.get('state')}）")
        if b["serial"] not in (s.get("device_by_id") or ""):
            return CaseResult("FAIL", reason=f"{b['com']} by-id 不含預期 serial {b['serial']}")
    return CaseResult("PASS")


@_case("p0-cmd-async", "cmd submit→status async 全流程",
       hints=("submit 後立刻讀 status 有 line race——submit_and_wait 已隔拍輪詢",
              "雙板要序列化送，back-to-back 會撞 foreground busy"),
       requires=("two_boards",))
def p0_cmd_async(ctx):
    for b in ctx.cfg["boards"]:  # 逐板序列化
        marker = f"P0_{random.randint(10000, 99999)}"
        cmd = ctx.sw.submit_and_wait(b["com"], f"echo {marker}")
        ctx.note(f"{b['com']}-cmd.json", str(cmd))
        if marker not in (cmd.get("stdout") or ""):
            return CaseResult("FAIL", reason=f"{b['com']} stdout 未含 marker（status={cmd.get('status')}）")
    return CaseResult("PASS")


@_case("p0-console-raw", "minicom 連線＋raw ownership（Tab 補完）",
       hints=("方向鍵/Tab 失效＝掉回 line-buffer，多半 orphan lease 佔住授予閘（#76/#99）",
              "minicom 顯示 Offline（DCD 未拉）不影響輸入"),
       requires=("tmux", "two_boards"))
def p0_console_raw(ctx):
    ses = ctx.tmux.name("p0con")
    ctx.tmux.new(ses, "serialwrap-minicom COM0")
    try:
        time.sleep(6)  # 等 console-attach＋minicom 起來
        cl = ctx.sw.run("session", "console-list", "--selector", "COM0")
        ctx.note("console-list.json", str(cl))
        consoles = cl.get("consoles") or []
        if not any(c.get("interactive_owner") for c in consoles):
            return CaseResult("FAIL", reason="第一個 console 未拿到 raw interactive ownership")
        ctx.tmux.send(ses, "ec", enter=False)
        ctx.tmux.send_key(ses, "Tab")
        time.sleep(2)
        pane = ctx.tmux.capture(ses)
        ctx.note("pane.txt", pane)
        if "echo" not in strip_ansi(pane):
            return CaseResult("FAIL", reason="Tab 補完未出現（raw 路徑疑掉回 line-buffer）")
        ctx.tmux.send_key(ses, "C-u")  # 清掉半行，還原乾淨 prompt
        return CaseResult("PASS")
    finally:
        ctx.tmux.kill(ses)
        time.sleep(3)  # 等 router 清理 detach


@_case("p0-clear-reattach", "session clear 自動 re-attach", requires=("two_boards",))
def p0_clear_reattach(ctx):
    ctx.sw.run("session", "clear", "--selector", "COM0")
    if not ctx.sw.wait_state("COM0", "READY", timeout_s=ctx.cfg["timeouts"]["ready_wait_s"]):
        return CaseResult("FAIL", reason="clear 後未在時限內回 READY")
    return CaseResult("PASS")


@_case("p0-selftest", "self-test 基本判讀")
def p0_selftest(ctx):
    st = ctx.sw.run("session", "self-test", "--selector", "COM0")
    ctx.note("selftest.json", str(st))
    if not (st.get("probe_ok") and st.get("classification") == "OK"):
        return CaseResult("FAIL", reason=f"classification={st.get('classification')} probe_ok={st.get('probe_ok')}")
    return CaseResult("PASS")


@_case("p0-blog-clean", "b-log 純淨度（無 ANSI transcript 回歸）",
       hints=("回歸根因＝script transcript 模式（6df17a5）；預設應為 minicom 原生 -C（PR#98）",),
       requires=("tmux",))
def p0_blog_clean(ctx):
    before = set((Path.home() / "b-log").glob("mini_COM0_*.log"))
    ses = ctx.tmux.name("p0blog")
    ctx.tmux.new(ses, "serialwrap-minicom COM0")
    time.sleep(6)
    ctx.tmux.kill(ses)
    time.sleep(3)
    new = sorted(set((Path.home() / "b-log").glob("mini_COM0_*.log")) - before)
    if not new:
        return CaseResult("FAIL", reason="未產生新的 mini_COM0_*.log capture")
    text = new[-1].read_bytes()
    esc = text.count(b"\x1b")
    ctx.note("capture-head.txt", text[:2000].decode("utf-8", errors="replace"))
    if b"Script started" in text or esc > 0:
        return CaseResult("FAIL", reason=f"capture 含 transcript 標頭或 ANSI（ESC×{esc}）")
    return CaseResult("PASS")


@_case("p0-wal-live", "WAL 活性與位置",
       hints=("live WAL 一律在 ~/.local/state/serialwrap/wal（systemd 不繼承 shell env）；勿讀 stale ~/b-log/raw.*",))
def p0_wal_live(ctx):
    mirror = Path.home() / ".local/state/serialwrap/wal/raw.mirror.log"
    before = mirror.stat().st_mtime if mirror.exists() else 0
    marker = f"P0WAL_{random.randint(10000, 99999)}"
    ctx.sw.submit_and_wait("COM0", f"echo {marker}")
    time.sleep(2)
    if not mirror.exists() or mirror.stat().st_mtime <= before:
        return CaseResult("FAIL", reason="live WAL mirror mtime 未跳動")
    tail = mirror.read_text(errors="replace")[-8000:]
    if marker not in tail:
        return CaseResult("FAIL", reason="WAL mirror 未見命令 marker")
    return CaseResult("PASS")


@_case("p0-multiopen", "無多開（multi_open/foreign_holders 空）")
def p0_multiopen(ctx):
    st = ctx.sw.run("daemon", "status")
    ctx.note("daemon-status.json", str(st))
    if st.get("multi_open") or st.get("foreign_holders"):
        return CaseResult("FAIL", reason=f"multi_open={st.get('multi_open')} holders={st.get('foreign_holders')}")
    return CaseResult("PASS")
```

- [ ] **Step 1**：照上實作＋`cases/__init__.py` 加 `from . import p0  # noqa: F401`。
- [ ] **Step 2**：欄位名核對——實作前先在本機跑 `serialwrap doctor`、`session console-list --selector COM0`、`session self-test --selector COM0`、`daemon status` 各一次，比對 JSON 欄位名（`checks[].ok`/`consoles[].interactive_owner`/`probe_ok`/`classification`/`multi_open`/`foreign_holders`），與程式碼不符處修正程式碼（以實際 payload 為準，並把修正記進 commit message）。
- [ ] **Step 3: 實機驗收**：`python3 -m realhw --tier p0` → 8/8 PASS、報告產出於 `~/b-log/realhw-reports/<ts>/`。
- [ ] **Step 4: Commit** `feat(realhw): P0 煙霧 8 case＋實機驗收（#122）`

---

### Task 6: P1 console cases（7 條）

**Files:** Create `realhw/cases/p1_console.py`；Modify `cases/__init__.py`

`_case` helper 同 Task 5 型（tier="p1"）。兩條完整範例＋五條精確規格：

- [ ] **Step 1: `p1-con-fanout`（T6）與 `p1-con-defer`（T7）完整實作**

```python
@_case("p1-con-fanout", "human 即時看到 agent 命令與回應（T6）",
       hints=("畫面缺 marker 先確認 console 沒掉回 line-buffer、無洩漏 daemon 掉字",),
       requires=("tmux", "two_boards"))
def p1_con_fanout(ctx):
    ses = ctx.tmux.name("fanout")
    ctx.tmux.new(ses, "serialwrap-minicom COM0")
    try:
        time.sleep(6)
        markers = [f"T6_{i}_{random.randint(1000, 9999)}" for i in range(3)]
        for m in markers:
            ctx.sw.submit_and_wait("COM0", f"echo {m}")
            time.sleep(0.5)
        time.sleep(2)
        pane = ctx.tmux.capture(ses)
        ctx.note("pane.txt", pane)
        missing = [m for m in markers if not find_marker(pane, m)]
        if missing:
            return CaseResult("FAIL", reason=f"console 畫面缺 marker：{missing}")
        return CaseResult("PASS")
    finally:
        ctx.tmux.kill(ses)
        time.sleep(3)


@_case("p1-con-defer", "human 打字不擋 agent（T7 suspend/deferred/resume）",
       hints=("deferred flush 後 human 輸入應自成一行、不與 agent 命令 byte 交錯",),
       requires=("tmux", "two_boards"))
def p1_con_defer(ctx):
    ses = ctx.tmux.name("defer")
    ctx.tmux.new(ses, "serialwrap-minicom COM0")
    try:
        time.sleep(6)
        ctx.tmux.send(ses, "echo HUMAN_HALF", enter=False)  # 半行不送出
        t0 = time.monotonic()
        cmd = ctx.sw.submit_and_wait("COM0", "echo T7_AGENT")
        took = time.monotonic() - t0
        if cmd.get("status") != "done" or "T7_AGENT" not in (cmd.get("stdout") or ""):
            return CaseResult("FAIL", reason=f"human 打字期間 agent 命令未完成（status={cmd.get('status')}）")
        if took > 15:
            return CaseResult("FAIL", reason=f"agent 命令耗時 {took:.1f}s（疑似被 human console 阻擋）")
        ctx.tmux.send_key(ses, "Enter")  # flush deferred
        time.sleep(2)
        pane = ctx.tmux.capture(ses)
        ctx.note("pane.txt", pane)
        if not find_marker(pane, "HUMAN_HALF"):
            return CaseResult("FAIL", reason="deferred human 輸入未 flush 回 UART")
        return CaseResult("PASS")
    finally:
        ctx.tmux.kill(ses)
        time.sleep(3)
```

- [ ] **Step 2: 其餘五條依規格實作**（模式同上：tmux 起 minicom → 動作 → CLI/畫面雙斷言 → finally 清理）：

| id | 動作序列 | 斷言 | hints |
|---|---|---|---|
| `p1-con-busy` | minicom 起→send-keys 一行（造 human_active）→ 立刻 `serialwrap session interactive-open --selector COM0 --owner agent:realhw --timeout 10` | 回應 `error_code == "SESSION_INTERACTIVE_BUSY"`（active 窗內不奪權） | 「human_active 窗＝60s（HUMAN_ACTIVE_WINDOW_S）」 |
| `p1-con-softpreempt` | minicom 起→**不**輸入、等 `human_active_window_s+5` 秒→`interactive-open` 同上→取 `interactive_id` 後 `interactive-close` | open 成功且回 `soft_preempted: true`；close 後 `console-list` 的原 console 仍在且恢復 owner | 「閒置降級不中斷 human console」 |
| `p1-con-liveness` | minicom 起→`pgrep -x minicom` 取 PID→`kill -9 <pid>`→輪詢 `session self-test` | ≤60s 內 `human_attached` 轉 false／`console-list` count 回落 | 「勿 pkill -f（self-match exit 144）；孤兒只來自 SIGKILL/crash」 |
| `p1-con-orphan` | 承 liveness 後直接重開 minicom→Tab 補完檢查（同 p0-console-raw 手法） | 不需 daemon restart 即拿回 raw ownership | 「#76 孤兒回收＋自癒；grace 3s 內 flap 不掉 line-buffer」 |
| `p1-con-second` | 第一個 minicom 起→第二個 tmux 再起 `serialwrap-minicom COM0`→`console-list` | 恰一個 `interactive_owner:true`；第二 console 存在且非 owner（line-buffer 路徑） | 「第二 console 走 line-buffer 是契約」 |

- [ ] **Step 3: 實機驗收**：`python3 -m realhw --tier p1 --skip p1-rst-daemon,p1-rst-reboot,p1-rst-bootwindow,p1-rst-recover,p1-ho-cycle,p1-ho-persist,p1-hp-cycle,p1-hp-reorder,p1-cmd-modes,p1-cmd-serial,p1-cmd-file,p1-wal-reset,p1-wal-fullrun`（此時只有 console 7 條存在，skip 清單按尚未實作者調整——或直接逐條 `--only`）。等待節奏不穩就調 sleep/timeout 進 config。
- [ ] **Step 4: Commit** `feat(realhw): P1 console 對抗 7 case（#122）`

---

### Task 7: P1 cmd＋WAL cases（5 條）

**Files:** Create `realhw/cases/p1_cmd.py`、`realhw/cases/p1_wal.py`

- [ ] **Step 1: 依規格實作**（全 CLI 類，無 tmux；`p1-wal-*` 需掛一個 console 驗存活——用 `session console-attach --selector COM0 --label realhw` 取 vtty＋tmux `cat <vtty>`，比照 coexist 輕量法）：

| id | 動作序列 | 斷言 |
|---|---|---|
| `p1-cmd-modes` | (1) line：`submit_and_wait 'echo MODE_LINE'`；(2) background：`cmd submit --mode background --cmd 'for i in 1 2 3; do echo BG_$i; sleep 1; done'` → 輪詢 `cmd result-tail --cmd-id <id>` 收齊 BG_1..3；(3) interactive：`session interactive-open --owner agent:realhw` → `interactive-send --data 'echo IA_OK' --encoding text`＋`--data enter --encoding key` → `interactive-status` 含 IA_OK → `interactive-close`；(4) 錯誤面：對 RELEASED/DETACHED 前先跳過——改用不存在 selector `COM9` submit 應回 `SESSION_NOT_FOUND` | 三模式各自輸出正確；錯誤碼正確 |
| `p1-cmd-serial` | 5 條執行緒（`concurrent.futures.ThreadPoolExecutor`）各 `--source agent:rhw{n}` 三輪 `submit_and_wait 'echo A{n}_R{r}_MARK'`（單板 COM0；executor 內建序列化重試 FOREGROUND_BUSY：回 busy 就 sleep 0.5 重試 ≤20 次） | 全部 done；每筆 stdout 只含自己 marker（無 cross-talk）；`wal export --from-seq 0` 各 source TX 計數==提交數 |
| `p1-cmd-file` | 產 256KB 隨機檔→`file push --selector COM0 --local <f> --remote /tmp/rhw.bin`；push 進行中背景執行緒每 0.5s `health ping`（`serialwrap health ping` 或 `daemon status` 輕量 RPC）記延遲→`file pull` 回來比 md5→板上 `rm /tmp/rhw.bin` | md5 一致；ping 最大延遲 <3s（#52 歷史病灶 19.8s） |
| `p1-wal-reset`（T1/T2/T3） | console 掛著→記 `wal current-seq`（>0 先送一筆）→`wal reset`→`current-seq==0`→`submit 'echo T1_ALIVE'`→console-list 原 client 仍在→pane 見 T1_ALIVE→`current-seq>0` 且與 live WAL 檔尾 seq 相等 | reset 契約全項；console 不斷線 |
| `p1-wal-fullrun`（T8） | console 掛著→`wal reset`→3 輪：記 seq→`submit 'echo CASE_{i}_RESULT'`→等 1s→seq 嚴格遞增→`wal export --from-seq 0` 有記錄→console 存活＋pane 見全部 marker | 歷史 flaky（t8 假 PTY ~50%）在實機版應穩；失敗訊息附「async line race——等待要足」 |

- [ ] **Step 2: 實機逐條 `--only` 驗收**；`health ping` 命令名以 `serialwrap --help` 實際為準（無獨立子命令就用 `daemon status` 當探針並記進 hints）。
- [ ] **Step 3: Commit** `feat(realhw): P1 cmd/WAL 5 case（#122）`

---

### Task 8: P1 restart／handoff／hotplug cases（8 條，全 destructive）

**Files:** Create `realhw/cases/p1_restart.py`、`p1_handoff.py`、`p1_hotplug.py`

- [ ] **Step 1: 依規格實作**（每條 `finally` 負責還原；等待上限用 config `reboot_wait_s`）：

| id | 動作序列 | 斷言／還原 |
|---|---|---|
| `p1-rst-daemon` ⚡ | 記兩板 `com↔device_by_id↔profile` 映射與 `systemd.main_pid()`→`log-start`/等 3s/`log-stop` 驗 0 byte（安靜才重啟；吵就 SKIP reason=板不安靜）→`sudo systemctl restart serialwrap`→等兩板 READY | MainPID 變更；映射與 profile 逐板不變（#100/#95）；還原＝無（restart 即狀態） |
| `p1-rst-reboot` ⚡ | 掛 console→`submit_and_wait 'reboot' `（status 可能 timeout——prplOS 立刻回 prompt 後才斷線，容忍）→輪詢狀態序列：≤60s 內見 RECOVERING（或 DETACHED→ATTACHING 路徑，記錄實況）→`reboot_wait_s` 內回 READY | 全程無人工；console client 存活；WAL 連續含 boot log |
| `p1-rst-bootwindow` ⚡（#69/#94） | `submit 'reboot'`→等 8s（開機窗）→`session clear`＋`session attach --selector COM0`→attach 回應記錄（非致命 error_code 或 ok）→不做人工 recover，輪詢 `reprobe_attempts`／狀態 | `reboot_wait_s` 內自動 READY；`reprobe_attempts` 實況記進 evidence；卡不住開機窗（timeout_s=10s vs 板 12s boot）時降級斷言＝最終自動 READY 即 PASS |
| `p1-rst-recover` | `session recover --selector COM0`（正常板上跑）→回應無論 TIMEOUT/ok 都接受→立刻 `self-test` | `probe_ok=true` 且 classification=OK（TIMEOUT≠失敗是契約）；`bridge_generation` 記進 evidence |
| `p1-ho-cycle` ⚡ | `device release --selector COM1 --source agent:realhw --reason 'realhw p1-ho-cycle'`→讀 live state.json 驗 `released` 有該筆→tmux 起 `minicom -D <attached_real_path 記錄值> -b 115200`（外部持有者）→等 3s→`daemon status` `foreign_holders` 含該 tty→`device attach --selector COM1` 應回 `DEVICE_STILL_HELD`→kill 外部 minicom→`device attach` 成功→等 READY | finally：確保外部 minicom 殺掉＋`device attach`＋等 READY |
| `p1-ho-persist` ⚡ | `device release --selector COM1`→`sudo systemctl restart serialwrap`→等 daemon 起、COM0 READY→驗 COM1 仍 RELEASED（不被搶回）→`device attach --selector COM1`→READY | finally 同上 |
| `p1-hp-cycle` ⚡ | `usbipd list` 驗 config busid 存在→`detach -b <COM1 busid>`→≤30s 內 COM1 轉 DETACHED、COM0 不受擾（state 仍 READY）→`attach -w -b`→COM1 自動回原 COM、READY | finally：確保 busid attach 回來＋等 READY |
| `p1-hp-reorder` ⚡ | 兩板 detach→反序 attach（COM1 的 busid 先）→兩板各自 DETACHED-rebind 回原 COM（by-id 對應不變、real_path 可能翻轉——記 evidence）→`sudo systemctl restart`→startup rank 下仍 COM0=AC01QZT0/COM1=AQ00OAQ7 | finally：兩 busid 皆 attach＋restart 後等兩板 READY |

- [ ] **Step 2: 逐條 `--only` 實機驗收**（destructive——一條一條跑、確認還原後再跑下一條）。
- [ ] **Step 3: Commit** `feat(realhw): P1 restart/handoff/hotplug 8 case（#122）`

---

### Task 9: 長跑（分析器 TDD＋編排）

**Files:** Create `realhw/cases/longrun.py`、`tests/test_realhw_longrun.py`

- [ ] **Step 1: 分析器 RED 測試**

```python
# tests/test_realhw_longrun.py
"""#122 長跑分析器單測——吃合成快照/事件，不碰 live。"""
from __future__ import annotations

from realhw.cases import longrun


def test_analyze_counts_and_stuck_attached():
    snapshots = [
        {"t": 0, "sessions": {"COM0": "READY", "COM1": "READY"}, "rss_kb": 50000, "pid": 1},
        {"t": 300, "sessions": {"COM0": "ATTACHED", "COM1": "READY"}, "rss_kb": 51000, "pid": 1},
        {"t": 600, "sessions": {"COM0": "ATTACHED", "COM1": "READY"}, "rss_kb": 52000, "pid": 1},
        {"t": 900, "sessions": {"COM0": "READY", "COM1": "READY"}, "rss_kb": 52000, "pid": 1},
    ]
    events = [
        {"t": 10, "source": "agent:rhw1", "kind": "submit"},
        {"t": 11, "source": "agent:rhw1", "kind": "done"},
        {"t": 20, "source": "agent:rhw2", "kind": "submit"},
        {"t": 30, "source": "agent:rhw2", "kind": "error", "detail": "SESSION_NOT_READY"},
    ]
    a = longrun.analyze(snapshots, events)
    assert a["per_source"]["agent:rhw1"] == {"submit": 1, "done": 1, "error": 0}
    assert a["per_source"]["agent:rhw2"]["error"] == 1
    assert a["stuck_attached"] == [{"com": "COM0", "from_t": 300, "to_t": 900, "duration_s": 600}]
    assert a["pid_changes"] == 0


def test_analyze_flags_daemon_death():
    snapshots = [
        {"t": 0, "sessions": {"COM0": "READY"}, "rss_kb": 1, "pid": 1},
        {"t": 300, "sessions": {}, "rss_kb": 0, "pid": 0},
    ]
    a = longrun.analyze(snapshots, [])
    assert a["daemon_death_at"] == 300
```

- [ ] **Step 2: RED → 實作 `analyze(snapshots, events) -> dict`**：per_source 計數（submit/done/error）、`stuck_attached`（連續非 READY 區段，起訖 t 與時長）、`pid_changes`、`daemon_death_at`（pid 轉 0 的首個 t，無則 None）、`rss_trend`（首尾值）。純函式、無 I/O。
- [ ] **Step 3: 編排實作（`lr-mixed` case，tier="longrun"）**：
  - 4 個 agent worker thread：輪流對兩板 `submit_and_wait`（mix：每第 3 輪改跑一次 background `echo` 短命令、每第 10 輪 interactive open/close），每動作 append event（`{"t","source","kind","detail?"}`）到 `events.ndjson`（開檔 append、即時 flush——無人環境 log 完整優先）。
  - 1 個 human 模擬 thread：tmux `serialwrap-minicom COM0`，每 2-5 分鐘 send-keys 一行 `echo HUMAN_TICK_<n>`；minicom 死了記 event 後重開。
  - snapshot thread：每 `snapshot_interval_s` 記 `{"t","sessions":{com:state},"rss_kb","pid"}` 到 `snapshots.ndjson`（RSS 讀 `/proc/<MainPID>/status` 的 VmRSS）。
  - 主迴圈：到 `cfg["duration_s"]` 或 SIGINT（`signal.signal` 設 flag）停止 workers；**重大事件**（pid==0 或兩板同時非 READY 持續 >15 分鐘）→ 停止負載、記 event、不重啟 daemon。
  - 收尾：`analyze()` 吃兩個 ndjson → `longrun-analysis.md`（統計＋stuck_attached 表＋事件時間線＋與歷史基線對照句），並回 CaseResult（重大事件或 FAIL 級統計→FAIL，否則 PASS）。
- [ ] **Step 4: 驗證**：單測 GREEN；實機短跑 `python3 -m realhw --tier longrun --duration 15m` → 負載/快照/報告全鏈路、兩板事後 READY。
- [ ] **Step 5: Commit** `feat(realhw): 長跑編排＋事後分析器（#122）`

---

### Task 10: checklist 文件＋README＋fragment＋收尾

**Files:** Create `docs/func-test/realhw-stability-checklist.md`、`changelog.d/122-realhw-stability-suite.md`；Modify `README.md`

- [ ] **Step 1: checklist 文件**——結構：前置作業（部署新鮮度／環境清潔／throwaway 隔離通則，含命令）→ P0/P1 逐 case 表（case id＋驗什麼＋手動等效命令＋判定＋坑）→ 長跑使用法 → P2 手動程序全文（MCU flash /dev/ttyMCU 完整程序含 GPIO BSL 與三坑、U-Boot template＋recovery lease、self-test 全譜情境表、安裝/監管模式轉換、Windows loopback）→ 坑速查表。內容以設計 spec 的 case 目錄＋本 plan 的規格表展開；case id 必須與 `python3 -m realhw --list` 一致（實作後跑一次 `--list` 對照）。**R-21**：全文不得含 `/home/<user>/` 字面。
- [ ] **Step 2: README**——「測試」章後加小節「實機穩定性測試（#122）」：三行說明＋`python3 -m realhw --tier p0,p1`／`--tier longrun --duration 32h`＋指向 checklist。
- [ ] **Step 3: fragment**

```markdown
---
type: feat
issue: 122
scope: realhw
---
新增實機穩定性測試套件 `python3 -m realhw`（#122）：P0 煙霧×8＋P1 核心穩定性×20（console 對抗/重啟恢復/裝置交接/usbipd 插拔/命令執行/WAL）全自動、長跑（預設 32h）無人看護＋事後分析報告（`~/b-log/realhw-reports/`）；preflight 守門（部署新鮮度/doctor/兩板/工具/環境乾淨/破壞性預告）；`docs/func-test/realhw-stability-checklist.md` 人可讀清單含 P2 手動程序。測部署後系統（不 import sw_core、不進 CI、不入 wheel）。
```

- [ ] **Step 4: 收尾驗證**：`python3 -m pytest -q tests/` 無新失敗（新單測含在內）；`python3 -m policy_check --repo . --pr-title "feat(realhw): #122 實機穩定性測試套件" --pr-body "Closes #122" --pr-base-ref main --pr-head-ref feature/122-realhw-stability-suite` 通過（R-21 特別注意 checklist 文件）。
- [ ] **Step 5: Commit** `docs(realhw): checklist＋README＋changelog fragment（#122）`

---

### Task 11: 全套實機驗收

- [ ] **Step 1**：`python3 -m realhw --list` 與 checklist 對照一致。
- [ ] **Step 2**：`python3 -m realhw --tier p0,p1` 完整跑（預留 ~1.5h：restart/reboot/hotplug 類每條數分鐘）→ 全 PASS（或逐項判讀 FAIL 修穩）。
- [ ] **Step 3**：跑後環境健康：兩板 READY、`daemon status` 乾淨、無 tmux 殘留 session（`tmux ls | grep realhw` 空）、報告目錄完整。
- [ ] **Step 4**：openspec tasks.md 全勾＋commit `docs(openspec): #122 tasks 完成勾稽`。
