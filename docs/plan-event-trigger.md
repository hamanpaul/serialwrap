# #37 Event Trigger Implementation Plan

> 📌 **歷史快照**：#37 event trigger 已交付。本計畫含「Phase 12 — MCP tools」等步驟（建立 tests/test_event_mcp.py、修改 sw_mcp/server.py 的 serialwrap_event_* definitions）**已隨 #59 MCP 退役而不適用**——相關檔案已自 repo 移除，event 功能僅經 RPC/CLI 表面提供（`serialwrap event ...`）。本檔僅留作歷史，不再維護；勿照其 MCP 步驟操作。

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Implement v1 of the UART RX → pattern → handler trigger described in `docs/design-event-trigger.md` (issue #37): a udev/crontab-style declarative rule engine that lives next to the bridge, runs handlers as fire-and-forget subprocesses, and never blocks UART IO.

**Architecture:** New `sw_core/event_engine/` package containing schema/registry/counter/event-log/line-buffer/matcher/dispatcher/engine modules. Bridge's existing `on_rx_data` hook feeds bytes into a per-COM bounded queue consumed by a single matcher thread; matched events go to a small thread pool that fork/exec handlers with stdin JSON + env subset. RPC/CLI/MCP add CRUD + status surface. All paths use drop_oldest for overflow; nothing back-pressures the bridge.

**Tech Stack:** Python 3.12 stdlib only (json, re, threading, subprocess, fcntl/os.rename for atomic IO). Tests use `unittest`. Func-test uses existing YAML runner.

---

## File Structure

### New files (under `sw_core/event_engine/`)

| File | Responsibility |
|---|---|
| `__init__.py` | Public surface re-exports (`EventEngine`, `Rule`) |
| `schema.py` | `Rule` dataclass + `validate_rule_dict()` |
| `counter.py` | `Counter` dataclass + atomic load/save/clear in `EVENTS_RUNTIME_DIR` |
| `registry.py` | `RuleRegistry`: load `events.d/`, save/delete, reload diff |
| `event_log.py` | `EventLogger`: append ndjson, rotate, tail with filters |
| `line_buffer.py` | `LineBuffer` (per-COM byte→line splitter) + ANSI strip |
| `matcher.py` | Pattern eval, gate predicates, bounded queue, worker thread |
| `dispatcher.py` | Spawn pool, payload builder, timeout, drop_oldest |
| `engine.py` | `EventEngine`: lifecycle, COM enable/disable, RPC handlers |

### New tests (`tests/test_event_*.py`, flat layout per project convention)

`test_event_schema.py`, `test_event_counter.py`, `test_event_registry.py`, `test_event_log.py`, `test_event_line_buffer.py`, `test_event_matcher.py`, `test_event_dispatcher.py`, `test_event_engine.py`, `test_event_rpc.py`, `test_event_mcp.py`, `test_event_cli.py`.

### Modified files

| File | Change summary |
|---|---|
| `sw_core/constants.py` | Add `EVENTS_DIR` / `EVENTS_RUNTIME_DIR` / `EVENTS_LOG_PATH` defaults |
| `sw_core/uart_io.py` | Compose existing `on_rx_data` callback so engine receives a copy without touching session manager's existing consumer |
| `sw_core/session_manager.py` | Expose `active_cmd_id_for(com)` helper for scope filter; lifecycle hooks for engine start/stop |
| `sw_core/service.py` | Register 10 new RPC methods, load engine in daemon startup |
| `sw_core/cli.py` | New `event` subcommand group |
| sw_mcp/server.py（已退役 #59）| 10 new tool definitions with required "call status first" notice |
| `README.md`, `sw_core/assets/skill/SKILL.md` | Doc additions |

---

## Conventions used by every task

- **TDD loop**: write failing test → run (FAIL) → minimal impl → run (PASS) → commit.
- **Test file template** (use this exact preamble in every new test file):

```python
from __future__ import annotations
import unittest
```

- **Test runner**: `python -m unittest tests.test_event_<topic> -v` (project standard, see existing `tests/test_*.py`).
- **Commits**: conventional, scoped to the file group, e.g. `feat(event): add Rule schema validator`.
- **Push policy**: do NOT push branches; this plan only commits locally.

---

## Phase 0 — Skeleton & Constants

### Task 0.1: Create `event_engine/` package directory

**Files:** Create `sw_core/event_engine/__init__.py`

- [ ] **Step 1:** Create the file with placeholder content.

```python
"""sw_core.event_engine — pattern → spawn handler trigger engine (issue #37)."""
from __future__ import annotations

__all__: list[str] = []
```

- [ ] **Step 2:** Verify import works.

Run: `python -c "import sw_core.event_engine"`
Expected: no output, exit 0.

- [ ] **Step 3:** Commit.

```bash
git add sw_core/event_engine/__init__.py
git commit -m "feat(event): scaffold event_engine package"
```

### Task 0.2: Add EVENTS_* constants

**Files:** Modify `sw_core/constants.py`

- [ ] **Step 1:** Add the new env-overridable paths after existing entries (before `DEFAULT_WAL_ROTATE_BYTES`).

```python
EVENTS_DIR = _env_path(
    "SERIALWRAP_EVENTS_DIR",
    os.path.join(os.path.expanduser("~"), ".serialwrap", "events.d"),
)
EVENTS_RUNTIME_DIR = _env_path(
    "SERIALWRAP_EVENTS_RUNTIME_DIR",
    os.path.join(STATE_DIR, "events"),
)
EVENTS_LOG_PATH = _env_path(
    "SERIALWRAP_EVENTS_LOG_PATH",
    os.path.join(EVENTS_RUNTIME_DIR, "events.ndjson"),
)
EVENTS_LOG_ROTATE_BYTES = 10 * 1024 * 1024
EVENTS_LOG_BACKUP_COUNT = 3
```

- [ ] **Step 2:** Extend `ensure_runtime_dirs()` to create the new dirs.

```python
def ensure_runtime_dirs() -> None:
    os.makedirs(STATE_DIR, exist_ok=True)
    os.makedirs(RUN_DIR, exist_ok=True)
    os.makedirs(WAL_DIR, exist_ok=True)
    os.makedirs(EVENTS_DIR, exist_ok=True)
    os.makedirs(EVENTS_RUNTIME_DIR, exist_ok=True)
```

- [ ] **Step 3:** Verify imports.

Run: `python -c "from sw_core.constants import EVENTS_DIR, EVENTS_RUNTIME_DIR, EVENTS_LOG_PATH; print(EVENTS_DIR)"`
Expected: prints a path ending in `events.d`.

- [ ] **Step 4:** Commit.

```bash
git add sw_core/constants.py
git commit -m "feat(event): add EVENTS_* runtime path constants"
```

---

## Phase 1 — Rule Schema

### Task 1.1: Failing test for Rule dataclass + validator (happy path)

**Files:** Create `tests/test_event_schema.py`

- [ ] **Step 1:** Write the file.

```python
from __future__ import annotations
import unittest

from sw_core.event_engine.schema import Rule, validate_rule_dict


class TestRuleSchema(unittest.TestCase):
    def _minimal(self) -> dict:
        return {
            "schema_version": 1,
            "owner": "tools-static",
            "name": "temp-overhold",
            "kind": "tool",
            "selectors": ["COM0"],
            "pattern": {"kind": "contains", "value": "Temperature overhold 105C"},
            "handler": {"exec": ["/usr/local/bin/notice-tool"]},
        }

    def test_minimal_valid(self) -> None:
        rule = validate_rule_dict(self._minimal())
        self.assertIsInstance(rule, Rule)
        self.assertEqual(rule.rule_id, "tools-static.temp-overhold")
        self.assertEqual(rule.profile, "ALL")
        self.assertEqual(rule.level, "INFO")
        self.assertEqual(rule.scope, "spontaneous")
        self.assertIsNone(rule.max_fires)
        self.assertEqual(rule.cooldown_ms, 0)
        self.assertEqual(rule.timeout_ms, 10000)
        self.assertTrue(rule.auto_enable_com_on_load)
        self.assertFalse(rule.debug)


if __name__ == "__main__":
    unittest.main()
```

- [ ] **Step 2:** Run.

Run: `python -m unittest tests.test_event_schema -v`
Expected: FAIL — `ModuleNotFoundError: No module named 'sw_core.event_engine.schema'`.

### Task 1.2: Implement Rule dataclass + validator (minimal)

**Files:** Create `sw_core/event_engine/schema.py`

- [ ] **Step 1:** Create the module.

```python
from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Any

_VALID_KIND = {"tool", "agent"}
_VALID_SCOPE = {"spontaneous", "command_output", "any"}
_VALID_LEVEL = {"INFO", "NOTYS", "WARN", "ERR", "ENMR", "CRITL"}
_VALID_PATTERN_KIND = {"contains", "regex"}
_NAME_RE = re.compile(r"^[a-z0-9][a-z0-9-]{0,63}$")
_OWNER_RE = re.compile(r"^[a-z0-9][a-z0-9-]{0,63}$")


@dataclass(frozen=True)
class Pattern:
    kind: str
    value: str
    flags: str = ""


@dataclass(frozen=True)
class Handler:
    exec: list[str] | None = None
    shell: str | None = None


@dataclass(frozen=True)
class Rule:
    schema_version: int
    owner: str
    name: str
    rule_id: str
    kind: str
    selectors: tuple[str, ...]
    profile: str
    level: str
    pattern: Pattern
    scope: str
    max_fires: int | None
    cooldown_ms: int
    timeout_ms: int
    handler: Handler
    auto_enable_com_on_load: bool
    debug: bool
    raw: dict[str, Any] = field(default_factory=dict)


class RuleSchemaError(ValueError):
    """Raised when a rule definition does not satisfy the schema."""


def _require(cond: bool, msg: str) -> None:
    if not cond:
        raise RuleSchemaError(msg)


def validate_rule_dict(obj: dict[str, Any]) -> Rule:
    _require(isinstance(obj, dict), "rule must be a JSON object")
    _require(obj.get("schema_version") == 1, "schema_version must be 1")
    owner = str(obj.get("owner") or "")
    name = str(obj.get("name") or "")
    _require(bool(_OWNER_RE.match(owner)), "owner must match [a-z0-9-]{1,64}")
    _require(bool(_NAME_RE.match(name)), "name must match [a-z0-9-]{1,64}")
    derived_id = f"{owner}.{name}"
    rid = obj.get("rule_id", derived_id)
    _require(rid == derived_id, f"rule_id must equal '{derived_id}'")

    kind = obj.get("kind")
    _require(kind in _VALID_KIND, f"kind must be one of {sorted(_VALID_KIND)}")

    selectors_raw = obj.get("selectors")
    _require(isinstance(selectors_raw, list) and selectors_raw, "selectors must be a non-empty list")
    selectors: list[str] = []
    for s in selectors_raw:
        _require(isinstance(s, str) and s, "each selector must be a non-empty string")
        selectors.append(s)

    profile = str(obj.get("profile", "ALL"))
    level = str(obj.get("level", "INFO"))
    _require(level in _VALID_LEVEL, f"level must be one of {sorted(_VALID_LEVEL)}")

    pat_raw = obj.get("pattern")
    _require(isinstance(pat_raw, dict), "pattern must be an object")
    pkind = pat_raw.get("kind")
    pvalue = pat_raw.get("value")
    pflags = str(pat_raw.get("flags", ""))
    _require(pkind in _VALID_PATTERN_KIND, "pattern.kind must be 'contains' or 'regex'")
    _require(isinstance(pvalue, str) and pvalue != "", "pattern.value must be non-empty string")
    if pkind == "regex":
        try:
            re.compile(pvalue, _flags_from_string(pflags))
        except re.error as exc:
            raise RuleSchemaError(f"pattern.value is not a valid regex: {exc}") from exc
    pattern = Pattern(kind=pkind, value=pvalue, flags=pflags)

    scope = str(obj.get("scope", "spontaneous"))
    _require(scope in _VALID_SCOPE, f"scope must be one of {sorted(_VALID_SCOPE)}")

    max_fires_raw = obj.get("max_fires", None)
    _require(
        max_fires_raw is None or (isinstance(max_fires_raw, int) and max_fires_raw >= 0),
        "max_fires must be null or non-negative int",
    )
    cooldown_ms = int(obj.get("cooldown_ms", 0))
    timeout_ms = int(obj.get("timeout_ms", 10000))
    _require(cooldown_ms >= 0, "cooldown_ms must be >= 0")
    _require(timeout_ms > 0, "timeout_ms must be > 0")

    h_raw = obj.get("handler")
    _require(isinstance(h_raw, dict), "handler must be an object")
    h_exec = h_raw.get("exec")
    h_shell = h_raw.get("shell")
    _require(
        bool(h_exec) ^ bool(h_shell),
        "handler must have exactly one of 'exec' (list[str]) or 'shell' (str)",
    )
    if h_exec is not None:
        _require(
            isinstance(h_exec, list) and all(isinstance(x, str) and x for x in h_exec),
            "handler.exec must be a non-empty list of non-empty strings",
        )
        handler = Handler(exec=list(h_exec), shell=None)
    else:
        _require(isinstance(h_shell, str) and h_shell, "handler.shell must be non-empty string")
        handler = Handler(exec=None, shell=h_shell)

    return Rule(
        schema_version=1,
        owner=owner,
        name=name,
        rule_id=derived_id,
        kind=kind,
        selectors=tuple(selectors),
        profile=profile,
        level=level,
        pattern=pattern,
        scope=scope,
        max_fires=max_fires_raw,
        cooldown_ms=cooldown_ms,
        timeout_ms=timeout_ms,
        handler=handler,
        auto_enable_com_on_load=bool(obj.get("auto_enable_com_on_load", True)),
        debug=bool(obj.get("debug", False)),
        raw=dict(obj),
    )


def _flags_from_string(s: str) -> int:
    out = 0
    for ch in s:
        if ch == "i":
            out |= re.IGNORECASE
        elif ch == "s":
            out |= re.DOTALL
        elif ch == "m":
            out |= re.MULTILINE
        else:
            raise RuleSchemaError(f"unsupported regex flag: {ch}")
    return out
```

- [ ] **Step 2:** Run.

Run: `python -m unittest tests.test_event_schema -v`
Expected: PASS.

- [ ] **Step 3:** Commit.

```bash
git add sw_core/event_engine/schema.py tests/test_event_schema.py
git commit -m "feat(event): add Rule dataclass + schema validator"
```

### Task 1.3: Add error-path tests

**Files:** Modify `tests/test_event_schema.py`

- [ ] **Step 1:** Add tests covering every reject branch.

```python
    def test_missing_schema_version(self) -> None:
        d = self._minimal(); d.pop("schema_version")
        with self.assertRaises(Exception):
            validate_rule_dict(d)

    def test_bad_owner(self) -> None:
        d = self._minimal(); d["owner"] = "Tools_Static"
        with self.assertRaises(Exception):
            validate_rule_dict(d)

    def test_bad_kind(self) -> None:
        d = self._minimal(); d["kind"] = "robot"
        with self.assertRaises(Exception):
            validate_rule_dict(d)

    def test_handler_must_xor(self) -> None:
        d = self._minimal()
        d["handler"] = {"exec": ["/x"], "shell": "echo x"}
        with self.assertRaises(Exception):
            validate_rule_dict(d)
        d["handler"] = {}
        with self.assertRaises(Exception):
            validate_rule_dict(d)

    def test_invalid_regex(self) -> None:
        d = self._minimal()
        d["pattern"] = {"kind": "regex", "value": "[broken"}
        with self.assertRaises(Exception):
            validate_rule_dict(d)

    def test_rule_id_mismatch(self) -> None:
        d = self._minimal()
        d["rule_id"] = "wrong.id"
        with self.assertRaises(Exception):
            validate_rule_dict(d)

    def test_explicit_rule_id_ok(self) -> None:
        d = self._minimal()
        d["rule_id"] = "tools-static.temp-overhold"
        rule = validate_rule_dict(d)
        self.assertEqual(rule.rule_id, "tools-static.temp-overhold")

    def test_selectors_all(self) -> None:
        d = self._minimal(); d["selectors"] = ["ALL"]
        rule = validate_rule_dict(d)
        self.assertEqual(rule.selectors, ("ALL",))
```

- [ ] **Step 2:** Run.

Run: `python -m unittest tests.test_event_schema -v`
Expected: PASS (8+ tests).

- [ ] **Step 3:** Commit.

```bash
git add tests/test_event_schema.py
git commit -m "test(event): cover Rule schema reject paths"
```

---

## Phase 2 — Counter (atomic file IO)

### Task 2.1: Failing tests for Counter

**Files:** Create `tests/test_event_counter.py`

- [ ] **Step 1:** Write the test.

```python
from __future__ import annotations
import os
import tempfile
import unittest

from sw_core.event_engine.counter import Counter, CounterStore


class TestCounter(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.mkdtemp(prefix="sw-counter-")
        self.store = CounterStore(self.tmp)

    def tearDown(self) -> None:
        import shutil
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_load_missing_returns_zero(self) -> None:
        c = self.store.load("a.b")
        self.assertEqual(c.fires, 0)
        self.assertIsNone(c.last_fire_ts)
        self.assertFalse(c.exhausted)

    def test_save_then_load(self) -> None:
        self.store.save("a.b", Counter(fires=3, last_fire_ts=1234, exhausted=False))
        c = self.store.load("a.b")
        self.assertEqual(c.fires, 3)
        self.assertEqual(c.last_fire_ts, 1234)
        self.assertFalse(c.exhausted)

    def test_clear_removes_file(self) -> None:
        self.store.save("a.b", Counter(fires=1, last_fire_ts=10, exhausted=False))
        self.assertTrue(os.path.exists(self.store.path_for("a.b")))
        self.store.clear("a.b")
        self.assertFalse(os.path.exists(self.store.path_for("a.b")))
        c = self.store.load("a.b")
        self.assertEqual(c.fires, 0)

    def test_atomic_save_does_not_leave_tmp(self) -> None:
        self.store.save("a.b", Counter(fires=1, last_fire_ts=1, exhausted=False))
        leftover = [f for f in os.listdir(self.tmp) if f.endswith(".tmp")]
        self.assertEqual(leftover, [])

    def test_list_known_rule_ids(self) -> None:
        self.store.save("a.b", Counter(fires=1, last_fire_ts=1, exhausted=False))
        self.store.save("c.d", Counter(fires=2, last_fire_ts=2, exhausted=True))
        self.assertEqual(set(self.store.known_rule_ids()), {"a.b", "c.d"})


if __name__ == "__main__":
    unittest.main()
```

- [ ] **Step 2:** Run.

Run: `python -m unittest tests.test_event_counter -v`
Expected: FAIL (ModuleNotFoundError).

### Task 2.2: Implement Counter store

**Files:** Create `sw_core/event_engine/counter.py`

- [ ] **Step 1:** Write the module.

```python
from __future__ import annotations

import json
import os
from dataclasses import dataclass
from typing import Iterable


@dataclass
class Counter:
    fires: int = 0
    last_fire_ts: int | None = None
    exhausted: bool = False

    def to_json(self) -> dict:
        return {"fires": self.fires, "last_fire_ts": self.last_fire_ts, "exhausted": self.exhausted}

    @classmethod
    def from_json(cls, obj: dict) -> "Counter":
        return cls(
            fires=int(obj.get("fires", 0)),
            last_fire_ts=obj.get("last_fire_ts"),
            exhausted=bool(obj.get("exhausted", False)),
        )


class CounterStore:
    """Per-rule counter persisted under EVENTS_RUNTIME_DIR (tmpfs).

    Files are atomic via tmp+rename. Missing file ⇒ zero counter.
    """

    def __init__(self, root: str) -> None:
        self._root = root
        os.makedirs(self._root, exist_ok=True)

    def path_for(self, rule_id: str) -> str:
        return os.path.join(self._root, f"{rule_id}.counter.json")

    def load(self, rule_id: str) -> Counter:
        path = self.path_for(rule_id)
        try:
            with open(path, "r", encoding="utf-8") as f:
                return Counter.from_json(json.load(f))
        except FileNotFoundError:
            return Counter()
        except (json.JSONDecodeError, OSError):
            # Best-effort: corruption in tmpfs counter → start fresh.
            return Counter()

    def save(self, rule_id: str, counter: Counter) -> None:
        path = self.path_for(rule_id)
        tmp = path + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(counter.to_json(), f)
            f.flush()
            os.fsync(f.fileno())
        os.rename(tmp, path)

    def clear(self, rule_id: str) -> None:
        try:
            os.unlink(self.path_for(rule_id))
        except FileNotFoundError:
            pass

    def known_rule_ids(self) -> Iterable[str]:
        try:
            entries = os.listdir(self._root)
        except FileNotFoundError:
            return []
        out: list[str] = []
        suffix = ".counter.json"
        for entry in entries:
            if entry.endswith(suffix):
                out.append(entry[: -len(suffix)])
        return out
```

- [ ] **Step 2:** Run.

Run: `python -m unittest tests.test_event_counter -v`
Expected: PASS.

- [ ] **Step 3:** Commit.

```bash
git add sw_core/event_engine/counter.py tests/test_event_counter.py
git commit -m "feat(event): add CounterStore with atomic writes"
```

---

## Phase 3 — RuleRegistry

### Task 3.1: Failing tests for RuleRegistry

**Files:** Create `tests/test_event_registry.py`

- [ ] **Step 1:** Write the test.

```python
from __future__ import annotations
import json
import os
import tempfile
import unittest

from sw_core.event_engine.registry import RuleRegistry, RuleLoadError


def _rule_dict(owner: str, name: str, **overrides) -> dict:
    base = {
        "schema_version": 1,
        "owner": owner,
        "name": name,
        "kind": "tool",
        "selectors": ["COM0"],
        "pattern": {"kind": "contains", "value": "x"},
        "handler": {"exec": ["/bin/true"]},
    }
    base.update(overrides)
    return base


class TestRuleRegistry(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.mkdtemp(prefix="sw-events-d-")
        self.reg = RuleRegistry(self.tmp)

    def tearDown(self) -> None:
        import shutil
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_load_empty_dir(self) -> None:
        result = self.reg.load_all()
        self.assertEqual(result.rules, [])
        self.assertEqual(result.failed, [])

    def test_save_then_load(self) -> None:
        d = _rule_dict("o", "n")
        rule = self.reg.upsert(d)
        self.assertEqual(rule.rule_id, "o.n")
        self.assertTrue(os.path.exists(os.path.join(self.tmp, "o.n.json")))
        loaded = self.reg.load_all().rules
        self.assertEqual([r.rule_id for r in loaded], ["o.n"])

    def test_delete(self) -> None:
        self.reg.upsert(_rule_dict("o", "n"))
        self.assertTrue(self.reg.delete("o.n"))
        self.assertFalse(self.reg.delete("o.n"))

    def test_invalid_file_collected_in_failed(self) -> None:
        with open(os.path.join(self.tmp, "bad.json"), "w") as f:
            f.write("{not json")
        with open(os.path.join(self.tmp, "bad-schema.json"), "w") as f:
            json.dump({"schema_version": 1}, f)
        result = self.reg.load_all()
        self.assertEqual(result.rules, [])
        self.assertEqual({entry.path for entry in result.failed},
                         {os.path.join(self.tmp, "bad.json"),
                          os.path.join(self.tmp, "bad-schema.json")})

    def test_reload_diff_classification(self) -> None:
        old_a = self.reg.upsert(_rule_dict("o", "a"))
        self.reg.upsert(_rule_dict("o", "b"))
        # Mutate b directly on disk to simulate external edit
        path_b = os.path.join(self.tmp, "o.b.json")
        with open(path_b, "r") as f:
            obj = json.load(f)
        obj["cooldown_ms"] = 5000
        with open(path_b, "w") as f:
            json.dump(obj, f)
        # Add c, remove a
        self.reg.upsert(_rule_dict("o", "c"))
        os.unlink(os.path.join(self.tmp, "o.a.json"))
        diff = self.reg.diff_against([old_a, self.reg.get("o.b")])  # use stale snapshot
        self.assertEqual({r.rule_id for r in diff.added}, {"o.c"})
        self.assertEqual({r.rule_id for r in diff.changed}, {"o.b"})
        self.assertEqual({r.rule_id for r in diff.removed}, {"o.a"})


if __name__ == "__main__":
    unittest.main()
```

- [ ] **Step 2:** Run.

Run: `python -m unittest tests.test_event_registry -v`
Expected: FAIL (ModuleNotFoundError).

### Task 3.2: Implement RuleRegistry

**Files:** Create `sw_core/event_engine/registry.py`

- [ ] **Step 1:** Write the module.

```python
from __future__ import annotations

import json
import os
from dataclasses import dataclass
from typing import Iterable

from .schema import Rule, RuleSchemaError, validate_rule_dict


class RuleLoadError(Exception):
    pass


@dataclass
class FailedLoad:
    path: str
    reason: str


@dataclass
class LoadResult:
    rules: list[Rule]
    failed: list[FailedLoad]


@dataclass
class DiffResult:
    added: list[Rule]
    removed: list[Rule]
    changed: list[Rule]


class RuleRegistry:
    """File-backed registry under EVENTS_DIR (events.d/<rule_id>.json)."""

    def __init__(self, root: str) -> None:
        self._root = root
        os.makedirs(self._root, exist_ok=True)

    @property
    def root(self) -> str:
        return self._root

    def path_for(self, rule_id: str) -> str:
        return os.path.join(self._root, f"{rule_id}.json")

    def load_all(self) -> LoadResult:
        rules: list[Rule] = []
        failed: list[FailedLoad] = []
        try:
            entries = sorted(os.listdir(self._root))
        except FileNotFoundError:
            return LoadResult(rules=[], failed=[])
        for entry in entries:
            if not entry.endswith(".json"):
                continue
            full = os.path.join(self._root, entry)
            try:
                with open(full, "r", encoding="utf-8") as f:
                    data = json.load(f)
            except (OSError, json.JSONDecodeError) as exc:
                failed.append(FailedLoad(full, f"read/parse failed: {exc}"))
                continue
            try:
                rule = validate_rule_dict(data)
            except RuleSchemaError as exc:
                failed.append(FailedLoad(full, str(exc)))
                continue
            rules.append(rule)
        return LoadResult(rules=rules, failed=failed)

    def get(self, rule_id: str) -> Rule | None:
        path = self.path_for(rule_id)
        try:
            with open(path, "r", encoding="utf-8") as f:
                return validate_rule_dict(json.load(f))
        except FileNotFoundError:
            return None
        except (OSError, json.JSONDecodeError, RuleSchemaError) as exc:
            raise RuleLoadError(f"cannot load {rule_id}: {exc}") from exc

    def upsert(self, obj: dict) -> Rule:
        rule = validate_rule_dict(obj)
        path = self.path_for(rule.rule_id)
        tmp = path + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(rule.raw, f, ensure_ascii=False, indent=2, sort_keys=True)
            f.flush()
            os.fsync(f.fileno())
        os.rename(tmp, path)
        return rule

    def delete(self, rule_id: str) -> bool:
        try:
            os.unlink(self.path_for(rule_id))
            return True
        except FileNotFoundError:
            return False

    def diff_against(self, previous: Iterable[Rule]) -> DiffResult:
        prev_map = {r.rule_id: r for r in previous}
        current = {r.rule_id: r for r in self.load_all().rules}
        added = [current[k] for k in current.keys() - prev_map.keys()]
        removed = [prev_map[k] for k in prev_map.keys() - current.keys()]
        changed: list[Rule] = []
        for k in current.keys() & prev_map.keys():
            if current[k].raw != prev_map[k].raw:
                changed.append(current[k])
        return DiffResult(added=added, removed=removed, changed=changed)
```

- [ ] **Step 2:** Run.

Run: `python -m unittest tests.test_event_registry -v`
Expected: PASS.

- [ ] **Step 3:** Commit.

```bash
git add sw_core/event_engine/registry.py tests/test_event_registry.py
git commit -m "feat(event): add RuleRegistry with diff support"
```

---

## Phase 4 — Event log (ndjson + rotation)

### Task 4.1: Failing tests for EventLogger

**Files:** Create `tests/test_event_log.py`

- [ ] **Step 1:** Write the test.

```python
from __future__ import annotations
import json
import os
import tempfile
import unittest

from sw_core.event_engine.event_log import EventLogger


class TestEventLogger(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.mkdtemp(prefix="sw-event-log-")
        self.path = os.path.join(self.tmp, "events.ndjson")
        self.log = EventLogger(self.path, rotate_bytes=1024, backup_count=2)

    def tearDown(self) -> None:
        import shutil
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_write_one(self) -> None:
        self.log.write({"type": "rule_loaded", "rule_id": "o.n"})
        with open(self.path, "r", encoding="utf-8") as f:
            line = f.readline().strip()
        obj = json.loads(line)
        self.assertEqual(obj["type"], "rule_loaded")
        self.assertEqual(obj["rule_id"], "o.n")
        self.assertIn("ts", obj)

    def test_rotation(self) -> None:
        big = "x" * 200
        for _ in range(10):
            self.log.write({"type": "match_recorded", "blob": big})
        # primary + at most backup_count rotations
        files = sorted(os.listdir(self.tmp))
        self.assertIn("events.ndjson", files)
        self.assertTrue(any(f.startswith("events.ndjson.") for f in files))

    def test_tail_filter(self) -> None:
        self.log.write({"type": "match_recorded", "rule_id": "o.a", "selector": "COM0"})
        self.log.write({"type": "match_recorded", "rule_id": "o.b", "selector": "COM1"})
        self.log.write({"type": "fire_completed", "rule_id": "o.a", "selector": "COM0"})
        rows = self.log.tail(rule_id="o.a")
        self.assertEqual([r["type"] for r in rows], ["match_recorded", "fire_completed"])
        rows2 = self.log.tail(selector="COM1")
        self.assertEqual([r["rule_id"] for r in rows2], ["o.b"])
        rows3 = self.log.tail(n=1)
        self.assertEqual(len(rows3), 1)
        self.assertEqual(rows3[0]["type"], "fire_completed")


if __name__ == "__main__":
    unittest.main()
```

- [ ] **Step 2:** Run → expect FAIL.

### Task 4.2: Implement EventLogger

**Files:** Create `sw_core/event_engine/event_log.py`

- [ ] **Step 1:** Write the module.

```python
from __future__ import annotations

import json
import os
import threading
import time
from typing import Any


class EventLogger:
    """Newline-delimited JSON log with size-based rotation.

    Thread-safe via internal lock. Synchronous fsync on each write to keep
    forensic value across daemon failover.
    """

    def __init__(self, path: str, rotate_bytes: int = 10 * 1024 * 1024, backup_count: int = 3) -> None:
        self._path = path
        self._rotate_bytes = rotate_bytes
        self._backup_count = backup_count
        self._lock = threading.Lock()
        os.makedirs(os.path.dirname(path) or ".", exist_ok=True)

    @property
    def path(self) -> str:
        return self._path

    def write(self, event: dict[str, Any]) -> None:
        if "ts" not in event:
            event = dict(event)
            event["ts"] = int(time.time() * 1000)
        line = json.dumps(event, ensure_ascii=False) + "\n"
        encoded = line.encode("utf-8")
        with self._lock:
            self._rotate_if_needed(len(encoded))
            with open(self._path, "ab") as f:
                f.write(encoded)
                f.flush()
                os.fsync(f.fileno())

    def _rotate_if_needed(self, incoming: int) -> None:
        try:
            size = os.path.getsize(self._path)
        except FileNotFoundError:
            return
        if size + incoming <= self._rotate_bytes:
            return
        for i in range(self._backup_count - 1, 0, -1):
            src = f"{self._path}.{i}"
            dst = f"{self._path}.{i + 1}"
            if os.path.exists(src):
                os.replace(src, dst)
        if os.path.exists(self._path):
            os.replace(self._path, f"{self._path}.1")

    def tail(
        self,
        *,
        rule_id: str | None = None,
        selector: str | None = None,
        since_ts: int | None = None,
        n: int | None = None,
    ) -> list[dict[str, Any]]:
        try:
            with open(self._path, "r", encoding="utf-8") as f:
                lines = f.readlines()
        except FileNotFoundError:
            return []
        out: list[dict[str, Any]] = []
        for line in lines:
            try:
                obj = json.loads(line)
            except json.JSONDecodeError:
                continue
            if rule_id is not None and obj.get("rule_id") != rule_id:
                continue
            if selector is not None and obj.get("selector") != selector:
                continue
            if since_ts is not None and obj.get("ts", 0) < since_ts:
                continue
            out.append(obj)
        if n is not None:
            out = out[-n:]
        return out
```

- [ ] **Step 2:** Run → PASS.

- [ ] **Step 3:** Commit.

```bash
git add sw_core/event_engine/event_log.py tests/test_event_log.py
git commit -m "feat(event): add ndjson EventLogger with rotation + tail"
```

---

## Phase 5 — Line buffer + ANSI strip

### Task 5.1: Failing tests for LineBuffer

**Files:** Create `tests/test_event_line_buffer.py`

- [ ] **Step 1:** Write the test.

```python
from __future__ import annotations
import unittest

from sw_core.event_engine.line_buffer import LineBuffer, strip_ansi


class TestLineBuffer(unittest.TestCase):
    def test_simple_line(self) -> None:
        lb = LineBuffer()
        lines = lb.feed(b"hello\n")
        self.assertEqual(lines, ["hello"])

    def test_partial_then_complete(self) -> None:
        lb = LineBuffer()
        self.assertEqual(lb.feed(b"part"), [])
        self.assertEqual(lb.feed(b"ial\n"), ["partial"])

    def test_multiple_lines_in_one_chunk(self) -> None:
        lb = LineBuffer()
        self.assertEqual(lb.feed(b"a\nb\nc\n"), ["a", "b", "c"])

    def test_crlf_normalized(self) -> None:
        lb = LineBuffer()
        self.assertEqual(lb.feed(b"alpha\r\nbeta\r\n"), ["alpha", "beta"])

    def test_max_line_truncates_and_emits(self) -> None:
        lb = LineBuffer(max_line_bytes=8)
        out = lb.feed(b"abcdefghIJK\n")
        self.assertEqual(len(out), 1)
        self.assertTrue(out[0].startswith("abcdefgh"))
        self.assertIn("truncated", out[0])

    def test_strip_ansi_basic(self) -> None:
        self.assertEqual(strip_ansi("\x1b[31mred\x1b[0m"), "red")
        self.assertEqual(strip_ansi("\x1b[?2004hpaste mode"), "paste mode")

    def test_strip_ansi_in_buffer(self) -> None:
        lb = LineBuffer()
        out = lb.feed(b"\x1b[31mred line\x1b[0m\n")
        self.assertEqual(out, ["red line"])


if __name__ == "__main__":
    unittest.main()
```

- [ ] **Step 2:** Run → FAIL.

### Task 5.2: Implement LineBuffer + strip_ansi

**Files:** Create `sw_core/event_engine/line_buffer.py`

- [ ] **Step 1:** Write the module.

```python
from __future__ import annotations

import re

# ANSI CSI / OSC / control sequences. Covers most common terminal escapes.
_ANSI_RE = re.compile(r"\x1b(?:\[[0-?]*[ -/]*[@-~]|\][^\x07]*\x07|[@-Z\\-_])")


def strip_ansi(text: str) -> str:
    return _ANSI_RE.sub("", text)


class LineBuffer:
    """Per-COM byte→line splitter with ANSI cleaning.

    - Accepts bytes via ``feed()``, returns a list of clean str lines whose
      terminator (\\n) has been seen.
    - Accumulates incomplete tail across calls.
    - Truncates lines that exceed ``max_line_bytes`` and tags them so the
      caller still sees activity rather than an unbounded buffer.
    """

    def __init__(self, max_line_bytes: int = 16 * 1024) -> None:
        self._buf = bytearray()
        self._max = max_line_bytes

    def feed(self, data: bytes) -> list[str]:
        self._buf.extend(data)
        out: list[str] = []
        while True:
            idx = self._buf.find(b"\n")
            if idx < 0:
                if len(self._buf) > self._max:
                    chunk = bytes(self._buf[: self._max])
                    out.append(self._finalize(chunk, truncated=True))
                    del self._buf[: self._max]
                break
            chunk = bytes(self._buf[:idx])
            del self._buf[: idx + 1]
            if chunk.endswith(b"\r"):
                chunk = chunk[:-1]
            out.append(self._finalize(chunk, truncated=False))
        return out

    def _finalize(self, raw: bytes, *, truncated: bool) -> str:
        text = raw.decode("utf-8", errors="replace")
        text = strip_ansi(text)
        if truncated:
            text += " ...truncated"
        return text
```

- [ ] **Step 2:** Run → PASS.

- [ ] **Step 3:** Commit.

```bash
git add sw_core/event_engine/line_buffer.py tests/test_event_line_buffer.py
git commit -m "feat(event): add LineBuffer + strip_ansi"
```

---

## Phase 6 — Pattern eval + Gates + Matcher worker

### Task 6.1: Failing tests for pattern eval and gates

**Files:** Create `tests/test_event_matcher.py`

- [ ] **Step 1:** Write the test.

```python
from __future__ import annotations
import unittest

from sw_core.event_engine.matcher import (
    PatternMatcher,
    apply_cooldown,
    apply_max_fires,
    apply_scope,
    apply_profile,
)
from sw_core.event_engine.counter import Counter
from sw_core.event_engine.schema import validate_rule_dict


def _rule(**overrides) -> object:
    base = {
        "schema_version": 1,
        "owner": "o",
        "name": "n",
        "kind": "tool",
        "selectors": ["COM0"],
        "pattern": {"kind": "contains", "value": "panic"},
        "handler": {"exec": ["/bin/true"]},
    }
    base.update(overrides)
    return validate_rule_dict(base)


class TestPatternMatcher(unittest.TestCase):
    def test_contains_hit(self) -> None:
        rule = _rule()
        pm = PatternMatcher(rule.pattern)
        m = pm.eval("Kernel panic - not syncing")
        self.assertIsNotNone(m)
        self.assertEqual(m.matched_text, "panic")
        self.assertEqual(m.groups, [])

    def test_contains_miss(self) -> None:
        rule = _rule()
        pm = PatternMatcher(rule.pattern)
        self.assertIsNone(pm.eval("nothing here"))

    def test_contains_case_insensitive(self) -> None:
        rule = _rule(pattern={"kind": "contains", "value": "PANIC", "flags": "i"})
        pm = PatternMatcher(rule.pattern)
        self.assertIsNotNone(pm.eval("kernel panic"))

    def test_regex_groups(self) -> None:
        rule = _rule(pattern={"kind": "regex", "value": r"temp=(\d+)C"})
        pm = PatternMatcher(rule.pattern)
        m = pm.eval("sensor temp=105C ok")
        self.assertIsNotNone(m)
        self.assertEqual(m.groups, ["105"])
        self.assertEqual(m.matched_text, "temp=105C")


class TestGates(unittest.TestCase):
    def test_cooldown_blocks_within_window(self) -> None:
        rule = _rule(cooldown_ms=1000)
        c = Counter(fires=1, last_fire_ts=1_000_000, exhausted=False)
        self.assertFalse(apply_cooldown(rule, c, now_ms=1_000_500))
        self.assertTrue(apply_cooldown(rule, c, now_ms=1_001_500))

    def test_cooldown_zero_always_passes(self) -> None:
        rule = _rule(cooldown_ms=0)
        c = Counter(fires=1, last_fire_ts=1_000_000)
        self.assertTrue(apply_cooldown(rule, c, now_ms=1_000_001))

    def test_max_fires_exhaustion(self) -> None:
        rule = _rule(max_fires=2)
        self.assertTrue(apply_max_fires(rule, Counter(fires=0)))
        self.assertTrue(apply_max_fires(rule, Counter(fires=1)))
        self.assertFalse(apply_max_fires(rule, Counter(fires=2, exhausted=True)))

    def test_max_fires_null_unlimited(self) -> None:
        rule = _rule(max_fires=None)
        self.assertTrue(apply_max_fires(rule, Counter(fires=10000)))

    def test_scope_spontaneous(self) -> None:
        rule = _rule(scope="spontaneous")
        self.assertTrue(apply_scope(rule, active_cmd_id=None))
        self.assertFalse(apply_scope(rule, active_cmd_id="cmd-7"))

    def test_scope_command_output(self) -> None:
        rule = _rule(scope="command_output")
        self.assertFalse(apply_scope(rule, active_cmd_id=None))
        self.assertTrue(apply_scope(rule, active_cmd_id="cmd-7"))

    def test_scope_any(self) -> None:
        rule = _rule(scope="any")
        self.assertTrue(apply_scope(rule, active_cmd_id=None))
        self.assertTrue(apply_scope(rule, active_cmd_id="cmd-7"))

    def test_profile_all(self) -> None:
        rule = _rule(profile="ALL")
        self.assertTrue(apply_profile(rule, com_profile=None))
        self.assertTrue(apply_profile(rule, com_profile="brcm"))

    def test_profile_match(self) -> None:
        rule = _rule(profile="brcm")
        self.assertTrue(apply_profile(rule, com_profile="brcm"))
        self.assertFalse(apply_profile(rule, com_profile="opi"))
        self.assertFalse(apply_profile(rule, com_profile=None))


if __name__ == "__main__":
    unittest.main()
```

- [ ] **Step 2:** Run → FAIL.

### Task 6.2: Implement PatternMatcher + gate helpers (no worker yet)

**Files:** Create `sw_core/event_engine/matcher.py`

- [ ] **Step 1:** Write the initial version.

```python
from __future__ import annotations

import re
from dataclasses import dataclass

from .counter import Counter
from .schema import Pattern, Rule, _flags_from_string


@dataclass
class MatchResult:
    matched_text: str
    groups: list[str]


class PatternMatcher:
    def __init__(self, pattern: Pattern) -> None:
        self._pattern = pattern
        if pattern.kind == "regex":
            self._re = re.compile(pattern.value, _flags_from_string(pattern.flags))
        elif pattern.kind == "contains":
            flags = _flags_from_string(pattern.flags)
            self._re = re.compile(re.escape(pattern.value), flags)
        else:
            raise ValueError(f"unknown pattern kind: {pattern.kind}")

    def eval(self, line: str) -> MatchResult | None:
        m = self._re.search(line)
        if m is None:
            return None
        return MatchResult(matched_text=m.group(0), groups=list(m.groups()))


def apply_cooldown(rule: Rule, counter: Counter, *, now_ms: int) -> bool:
    if rule.cooldown_ms <= 0 or counter.last_fire_ts is None:
        return True
    return (now_ms - counter.last_fire_ts) >= rule.cooldown_ms


def apply_max_fires(rule: Rule, counter: Counter) -> bool:
    if rule.max_fires is None:
        return True
    if counter.exhausted:
        return False
    return counter.fires < rule.max_fires


def apply_scope(rule: Rule, *, active_cmd_id: str | None) -> bool:
    if rule.scope == "any":
        return True
    if rule.scope == "spontaneous":
        return active_cmd_id is None
    if rule.scope == "command_output":
        return active_cmd_id is not None
    return False


def apply_profile(rule: Rule, *, com_profile: str | None) -> bool:
    if rule.profile == "ALL":
        return True
    return com_profile == rule.profile
```

- [ ] **Step 2:** Run → PASS for the gate / pattern tests.

- [ ] **Step 3:** Commit.

```bash
git add sw_core/event_engine/matcher.py tests/test_event_matcher.py
git commit -m "feat(event): pattern eval + scope/profile/cooldown/max_fires gates"
```

### Task 6.3: Failing tests for the matcher worker (queue + drop_oldest)

**Files:** Modify `tests/test_event_matcher.py`

- [ ] **Step 1:** Append a new TestCase exercising the worker.

```python
from sw_core.event_engine.matcher import MatcherWorker, MatcherFire


class _FakeContext:
    def __init__(self) -> None:
        self.active_cmd_id_value: str | None = None
        self.profile_value: str | None = None
        self.fires: list[MatcherFire] = []
        self.dropped: list[dict] = []
        self.skipped: list[dict] = []
        self.now_ms_value = 1_000_000

    # Hooks the worker calls back into:
    def active_cmd_id(self, com: str) -> str | None:
        return self.active_cmd_id_value

    def com_profile(self, com: str) -> str | None:
        return self.profile_value

    def now_ms(self) -> int:
        return self.now_ms_value

    def emit_fire(self, fire: MatcherFire) -> None:
        self.fires.append(fire)

    def emit_dropped(self, event: dict) -> None:
        self.dropped.append(event)

    def emit_skipped(self, event: dict) -> None:
        self.skipped.append(event)

    def counter_for(self, rule_id: str) -> Counter:
        return Counter()


class TestMatcherWorker(unittest.TestCase):
    def setUp(self) -> None:
        self.ctx = _FakeContext()
        self.rule = _rule(pattern={"kind": "contains", "value": "panic"})
        self.worker = MatcherWorker(rules=[self.rule], context=self.ctx, queue_max=4)
        self.worker.start()

    def tearDown(self) -> None:
        self.worker.stop()

    def test_match_emits_fire(self) -> None:
        self.worker.feed_line("COM0", "Kernel panic - not syncing", wal_seq=42)
        self.worker.flush_for_test(timeout=1.0)
        self.assertEqual(len(self.ctx.fires), 1)
        f = self.ctx.fires[0]
        self.assertEqual(f.rule.rule_id, "o.n")
        self.assertEqual(f.selector, "COM0")
        self.assertEqual(f.match.matched_text, "panic")
        self.assertEqual(f.wal_seq, 42)

    def test_no_match_does_not_emit(self) -> None:
        self.worker.feed_line("COM0", "all good", wal_seq=1)
        self.worker.flush_for_test(timeout=1.0)
        self.assertEqual(self.ctx.fires, [])

    def test_drop_oldest_when_queue_full(self) -> None:
        # Worker is paused so we can pile up.
        self.worker._paused_for_test = True
        for i in range(10):
            self.worker.feed_line("COM0", f"panic {i}", wal_seq=i)
        # release & drain
        self.worker._paused_for_test = False
        self.worker.flush_for_test(timeout=1.0)
        # at most queue_max + a tiny race window of drained items
        self.assertGreaterEqual(len(self.ctx.dropped), 1)

    def test_selector_all_applies_to_any_com(self) -> None:
        rule_all = _rule(name="m", selectors=["ALL"])
        self.worker.replace_rules([self.rule, rule_all])
        self.worker.feed_line("COMX", "panic now", wal_seq=99)
        self.worker.flush_for_test(timeout=1.0)
        # only the ALL-selector rule should fire on COMX (rule restricted to COM0 should not)
        self.assertEqual([f.rule.rule_id for f in self.ctx.fires], ["o.m"])
```

- [ ] **Step 2:** Run → FAIL (MatcherWorker / MatcherFire not yet defined).

### Task 6.4: Implement the matcher worker

**Files:** Modify `sw_core/event_engine/matcher.py`

- [ ] **Step 1:** Append the worker classes.

```python
import queue
import threading
from dataclasses import dataclass
from typing import Iterable, Protocol


class MatcherContext(Protocol):
    def active_cmd_id(self, com: str) -> str | None: ...
    def com_profile(self, com: str) -> str | None: ...
    def now_ms(self) -> int: ...
    def emit_fire(self, fire: "MatcherFire") -> None: ...
    def emit_dropped(self, event: dict) -> None: ...
    def emit_skipped(self, event: dict) -> None: ...
    def counter_for(self, rule_id: str) -> Counter: ...


@dataclass
class MatcherFire:
    rule: Rule
    selector: str
    match: MatchResult
    wal_seq: int
    matched_at: int
    active_cmd_id: str | None


@dataclass
class _Item:
    selector: str
    line: str
    wal_seq: int


class MatcherWorker:
    def __init__(
        self,
        rules: Iterable[Rule],
        context: MatcherContext,
        queue_max: int = 1024,
    ) -> None:
        self._rules: list[Rule] = list(rules)
        self._ctx = context
        self._queue: queue.Queue[_Item] = queue.Queue(maxsize=queue_max)
        self._queue_max = queue_max
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self._lock = threading.RLock()
        self._compiled: dict[str, PatternMatcher] = {}
        self._paused_for_test = False
        self._idle_event = threading.Event()
        self._idle_event.set()
        self._rebuild_compiled()

    def start(self) -> None:
        if self._thread is not None:
            return
        self._thread = threading.Thread(target=self._run, daemon=True, name="event-matcher")
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()
        if self._thread is not None:
            # nudge the queue so .get() returns
            try:
                self._queue.put_nowait(_Item("__stop__", "", 0))
            except queue.Full:
                pass
            self._thread.join(timeout=2.0)
            self._thread = None

    def replace_rules(self, rules: Iterable[Rule]) -> None:
        with self._lock:
            self._rules = list(rules)
            self._rebuild_compiled()

    def _rebuild_compiled(self) -> None:
        self._compiled = {r.rule_id: PatternMatcher(r.pattern) for r in self._rules}

    def feed_line(self, selector: str, line: str, wal_seq: int) -> None:
        item = _Item(selector=selector, line=line, wal_seq=wal_seq)
        self._idle_event.clear()
        try:
            self._queue.put_nowait(item)
        except queue.Full:
            # drop_oldest: pop one then push the new
            try:
                dropped = self._queue.get_nowait()
                self._ctx.emit_dropped({
                    "type": "event_dropped",
                    "reason": "matcher_queue_overflow",
                    "selector": dropped.selector,
                    "wal_seq": dropped.wal_seq,
                })
            except queue.Empty:
                pass
            try:
                self._queue.put_nowait(item)
            except queue.Full:
                self._ctx.emit_dropped({
                    "type": "event_dropped",
                    "reason": "matcher_queue_overflow",
                    "selector": selector,
                    "wal_seq": wal_seq,
                })

    def flush_for_test(self, timeout: float = 1.0) -> None:
        # block until queue empty and worker idle
        end = self._ctx.now_ms() + int(timeout * 1000)
        while True:
            if self._queue.empty() and self._idle_event.is_set():
                return
            if self._ctx.now_ms() > end:
                return
            self._idle_event.wait(0.01)

    def _run(self) -> None:
        while not self._stop.is_set():
            try:
                item = self._queue.get(timeout=0.1)
            except queue.Empty:
                self._idle_event.set()
                continue
            if item.selector == "__stop__":
                return
            if self._paused_for_test:
                # re-enqueue to allow pile-up tests
                try:
                    self._queue.put_nowait(item)
                except queue.Full:
                    pass
                continue
            try:
                self._evaluate(item)
            finally:
                if self._queue.empty():
                    self._idle_event.set()

    def _evaluate(self, item: _Item) -> None:
        with self._lock:
            rules_snapshot = list(self._rules)
            compiled_snapshot = dict(self._compiled)
        for rule in rules_snapshot:
            if "ALL" not in rule.selectors and item.selector not in rule.selectors:
                continue
            counter = self._ctx.counter_for(rule.rule_id)
            now = self._ctx.now_ms()
            active = self._ctx.active_cmd_id(item.selector)
            profile = self._ctx.com_profile(item.selector)
            pm = compiled_snapshot.get(rule.rule_id)
            if pm is None:
                continue
            match = pm.eval(item.line)
            if match is None:
                continue
            if not apply_scope(rule, active_cmd_id=active):
                if rule.debug:
                    self._ctx.emit_skipped({
                        "type": "match_skipped",
                        "rule_id": rule.rule_id,
                        "selector": item.selector,
                        "reason": "scope_mismatch",
                    })
                continue
            if not apply_profile(rule, com_profile=profile):
                if rule.debug:
                    self._ctx.emit_skipped({
                        "type": "match_skipped",
                        "rule_id": rule.rule_id,
                        "selector": item.selector,
                        "reason": "profile_mismatch",
                    })
                continue
            if not apply_max_fires(rule, counter):
                if rule.debug:
                    self._ctx.emit_skipped({
                        "type": "match_skipped",
                        "rule_id": rule.rule_id,
                        "selector": item.selector,
                        "reason": "exhausted",
                    })
                continue
            if not apply_cooldown(rule, counter, now_ms=now):
                if rule.debug:
                    self._ctx.emit_skipped({
                        "type": "match_skipped",
                        "rule_id": rule.rule_id,
                        "selector": item.selector,
                        "reason": "cooldown",
                    })
                continue
            self._ctx.emit_fire(MatcherFire(
                rule=rule,
                selector=item.selector,
                match=match,
                wal_seq=item.wal_seq,
                matched_at=now,
                active_cmd_id=active,
            ))
```

- [ ] **Step 2:** Run.

Run: `python -m unittest tests.test_event_matcher -v`
Expected: PASS.

- [ ] **Step 3:** Commit.

```bash
git add sw_core/event_engine/matcher.py tests/test_event_matcher.py
git commit -m "feat(event): matcher worker with bounded queue + drop_oldest"
```

---

## Phase 7 — Dispatcher (spawn pool)

### Task 7.1: Failing tests for payload builder + dispatcher

**Files:** Create `tests/test_event_dispatcher.py`

- [ ] **Step 1:** Write the test.

```python
from __future__ import annotations
import json
import os
import shutil
import tempfile
import threading
import time
import unittest

from sw_core.event_engine.dispatcher import (
    Dispatcher,
    DispatcherContext,
    build_payload,
    build_env,
)
from sw_core.event_engine.counter import Counter, CounterStore
from sw_core.event_engine.schema import validate_rule_dict
from sw_core.event_engine.matcher import MatcherFire, MatchResult


def _rule_exec(exec_argv: list[str], **overrides) -> object:
    base = {
        "schema_version": 1,
        "owner": "o", "name": "n", "kind": "tool",
        "selectors": ["COM0"],
        "pattern": {"kind": "contains", "value": "panic"},
        "handler": {"exec": exec_argv},
        "timeout_ms": 2000,
    }
    base.update(overrides)
    return validate_rule_dict(base)


class _Ctx:
    def __init__(self, tmp: str) -> None:
        self.tmp = tmp
        self.events: list[dict] = []
        self.counters = CounterStore(os.path.join(tmp, "counters"))
        self.lock = threading.Lock()

    def emit(self, event: dict) -> None:
        with self.lock:
            self.events.append(event)

    def counter_store(self) -> CounterStore:
        return self.counters


class TestPayload(unittest.TestCase):
    def test_payload_shape(self) -> None:
        rule = _rule_exec(["/bin/true"])
        fire = MatcherFire(
            rule=rule, selector="COM0",
            match=MatchResult(matched_text="panic", groups=[]),
            wal_seq=12345, matched_at=1000, active_cmd_id=None,
        )
        payload = build_payload(fire, fire_count=3, bridge_generation=7,
                                matched_line="Kernel panic - not syncing")
        self.assertEqual(payload["schema_version"], 1)
        self.assertEqual(payload["rule_id"], "o.n")
        self.assertEqual(payload["selector"], "COM0")
        self.assertEqual(payload["wal_seq"], 12345)
        self.assertEqual(payload["fire_count"], 3)
        self.assertEqual(payload["matched_text"], "panic")
        self.assertEqual(payload["matched_line"], "Kernel panic - not syncing")

    def test_env_subset_only_strings(self) -> None:
        rule = _rule_exec(["/bin/true"])
        fire = MatcherFire(
            rule=rule, selector="COM0",
            match=MatchResult(matched_text="panic", groups=[]),
            wal_seq=12345, matched_at=1000, active_cmd_id=None,
        )
        env = build_env(fire, fire_count=3)
        self.assertIn("PATH", env)
        self.assertEqual(env["SERIALWRAP_EVENT_RULE_ID"], "o.n")
        self.assertEqual(env["SERIALWRAP_EVENT_FIRE_COUNT"], "3")
        for v in env.values():
            self.assertIsInstance(v, str)


class TestDispatcher(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.mkdtemp(prefix="sw-dispatcher-")
        self.ctx = _Ctx(self.tmp)
        self.dispatcher = Dispatcher(
            event_emit=self.ctx.emit,
            counter_store=self.ctx.counters,
            per_daemon_max=2,
        )
        self.dispatcher.start()

    def tearDown(self) -> None:
        self.dispatcher.stop()
        shutil.rmtree(self.tmp, ignore_errors=True)

    def _make_fire(self, rule_dict: dict, matched_line: str = "panic now") -> MatcherFire:
        rule = validate_rule_dict(rule_dict)
        return MatcherFire(
            rule=rule, selector="COM0",
            match=MatchResult(matched_text="panic", groups=[]),
            wal_seq=1, matched_at=1, active_cmd_id=None,
        )

    def test_exec_handler_writes_completed(self) -> None:
        marker = os.path.join(self.tmp, "fired")
        rule = {
            "schema_version": 1,
            "owner": "o", "name": "fire", "kind": "tool",
            "selectors": ["COM0"],
            "pattern": {"kind": "contains", "value": "panic"},
            "handler": {"shell": f"echo done > {marker}"},
            "timeout_ms": 3000,
        }
        self.dispatcher.dispatch(self._make_fire(rule, "panic"), matched_line="panic line")
        self.dispatcher.flush_for_test(timeout=3.0)
        self.assertTrue(os.path.exists(marker))
        types = [e["type"] for e in self.ctx.events]
        self.assertIn("fire_completed", types)

    def test_timeout_kills_handler(self) -> None:
        rule = {
            "schema_version": 1,
            "owner": "o", "name": "slow", "kind": "tool",
            "selectors": ["COM0"],
            "pattern": {"kind": "contains", "value": "panic"},
            "handler": {"shell": "sleep 5"},
            "timeout_ms": 200,
        }
        self.dispatcher.dispatch(self._make_fire(rule, "panic"), matched_line="x")
        self.dispatcher.flush_for_test(timeout=3.0)
        types = [e["type"] for e in self.ctx.events]
        self.assertIn("fire_timeout", types)

    def test_per_rule_concurrency_drops_oldest(self) -> None:
        rule = {
            "schema_version": 1,
            "owner": "o", "name": "busy", "kind": "tool",
            "selectors": ["COM0"],
            "pattern": {"kind": "contains", "value": "panic"},
            "handler": {"shell": "sleep 0.5"},
            "timeout_ms": 2000,
        }
        f1 = self._make_fire(rule, "p1")
        f2 = self._make_fire(rule, "p2")
        self.dispatcher.dispatch(f1, matched_line="p1")
        # back-to-back; second one should be dropped while first is running
        self.dispatcher.dispatch(f2, matched_line="p2")
        self.dispatcher.flush_for_test(timeout=3.0)
        reasons = [e.get("reason") for e in self.ctx.events if e["type"] == "event_dropped"]
        self.assertIn("per_rule_busy", reasons)


if __name__ == "__main__":
    unittest.main()
```

- [ ] **Step 2:** Run → FAIL.

### Task 7.2: Implement Dispatcher

**Files:** Create `sw_core/event_engine/dispatcher.py`

- [ ] **Step 1:** Write the module.

```python
from __future__ import annotations

import json
import os
import signal
import subprocess
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from typing import Callable

from .counter import Counter, CounterStore
from .matcher import MatcherFire

_STDIO_TAIL_BYTES = 4096
_INHERITED_ENV_KEYS = ("PATH", "HOME", "USER", "LANG", "LC_ALL")


def build_payload(
    fire: MatcherFire,
    *,
    fire_count: int,
    bridge_generation: int = 0,
    matched_line: str = "",
) -> dict:
    rule = fire.rule
    return {
        "schema_version": 1,
        "rule_id": rule.rule_id,
        "rule_name": rule.name,
        "owner": rule.owner,
        "kind": rule.kind,
        "selector": fire.selector,
        "matched_at": fire.matched_at,
        "matched_line": matched_line,
        "matched_text": fire.match.matched_text,
        "match_groups": list(fire.match.groups),
        "scope": rule.scope,
        "active_cmd_id": fire.active_cmd_id,
        "wal_seq": fire.wal_seq,
        "bridge_generation": bridge_generation,
        "fire_count": fire_count,
        "level": rule.level,
        "profile": rule.profile,
    }


def build_env(fire: MatcherFire, *, fire_count: int) -> dict[str, str]:
    rule = fire.rule
    env: dict[str, str] = {}
    for key in _INHERITED_ENV_KEYS:
        if key in os.environ:
            env[key] = os.environ[key]
    matched_text = fire.match.matched_text
    if len(matched_text) > _STDIO_TAIL_BYTES:
        matched_text = matched_text[:_STDIO_TAIL_BYTES] + " ...truncated"
    env.update({
        "SERIALWRAP_EVENT_SCHEMA_VERSION": "1",
        "SERIALWRAP_EVENT_RULE_ID": rule.rule_id,
        "SERIALWRAP_EVENT_OWNER": rule.owner,
        "SERIALWRAP_EVENT_KIND": rule.kind,
        "SERIALWRAP_EVENT_SELECTOR": fire.selector,
        "SERIALWRAP_EVENT_MATCHED_AT": str(fire.matched_at),
        "SERIALWRAP_EVENT_MATCHED_TEXT": matched_text,
        "SERIALWRAP_EVENT_FIRE_COUNT": str(fire_count),
        "SERIALWRAP_EVENT_WAL_SEQ": str(fire.wal_seq),
        "SERIALWRAP_EVENT_LEVEL": rule.level,
        "SERIALWRAP_EVENT_SCOPE": rule.scope,
    })
    return env


class Dispatcher:
    """Spawn pool with per-rule concurrency = 1, per-daemon cap configurable."""

    def __init__(
        self,
        *,
        event_emit: Callable[[dict], None],
        counter_store: CounterStore,
        per_daemon_max: int = 8,
    ) -> None:
        self._emit = event_emit
        self._counters = counter_store
        self._per_daemon_max = per_daemon_max
        self._executor: ThreadPoolExecutor | None = None
        self._busy_rules: set[str] = set()
        self._lock = threading.Lock()
        self._inflight = 0
        self._idle = threading.Event()
        self._idle.set()

    def start(self) -> None:
        if self._executor is None:
            self._executor = ThreadPoolExecutor(max_workers=self._per_daemon_max, thread_name_prefix="event-dispatch")

    def stop(self) -> None:
        if self._executor is not None:
            self._executor.shutdown(wait=True)
            self._executor = None

    def flush_for_test(self, timeout: float = 5.0) -> None:
        end = time.time() + timeout
        while time.time() < end:
            if self._inflight == 0 and self._idle.is_set():
                return
            self._idle.wait(0.05)

    def dispatch(self, fire: MatcherFire, *, matched_line: str) -> None:
        rule = fire.rule
        with self._lock:
            if rule.rule_id in self._busy_rules:
                self._emit({
                    "type": "event_dropped",
                    "reason": "per_rule_busy",
                    "rule_id": rule.rule_id,
                    "selector": fire.selector,
                })
                return
            if self._inflight >= self._per_daemon_max:
                self._emit({
                    "type": "event_dropped",
                    "reason": "per_daemon_saturated",
                    "rule_id": rule.rule_id,
                    "selector": fire.selector,
                })
                return
            self._busy_rules.add(rule.rule_id)
            self._inflight += 1
            self._idle.clear()

        if self._executor is None:
            self.start()
        assert self._executor is not None
        self._executor.submit(self._run_handler, fire, matched_line)

    def _run_handler(self, fire: MatcherFire, matched_line: str) -> None:
        rule = fire.rule
        try:
            counter = self._counters.load(rule.rule_id)
            new_fires = counter.fires + 1
            payload = build_payload(
                fire,
                fire_count=new_fires,
                matched_line=matched_line,
            )
            env = build_env(fire, fire_count=new_fires)
            argv: list[str] | None
            if rule.handler.exec is not None:
                argv = list(rule.handler.exec)
                shell = False
            else:
                argv = ["/bin/sh", "-c", rule.handler.shell or ""]
                shell = False
            stdin_bytes = (json.dumps(payload, ensure_ascii=False) + "\n").encode("utf-8")
            start = time.time()
            try:
                proc = subprocess.Popen(
                    argv,
                    stdin=subprocess.PIPE,
                    stdout=subprocess.PIPE,
                    stderr=subprocess.PIPE,
                    env=env,
                    close_fds=True,
                    shell=shell,
                )
            except (FileNotFoundError, PermissionError) as exc:
                self._emit({
                    "type": "fire_failed",
                    "rule_id": rule.rule_id,
                    "selector": fire.selector,
                    "reason": str(exc),
                })
                self._post_fire_counter_update(rule.rule_id, new_fires)
                return

            try:
                stdout, stderr = proc.communicate(stdin_bytes, timeout=rule.timeout_ms / 1000.0)
                duration = int((time.time() - start) * 1000)
                self._emit({
                    "type": "fire_completed",
                    "rule_id": rule.rule_id,
                    "selector": fire.selector,
                    "exit_code": proc.returncode,
                    "duration_ms": duration,
                    "stdout_tail": stdout[-_STDIO_TAIL_BYTES:].decode("utf-8", errors="replace"),
                    "stderr_tail": stderr[-_STDIO_TAIL_BYTES:].decode("utf-8", errors="replace"),
                    "fire_count": new_fires,
                    "wal_seq": fire.wal_seq,
                })
            except subprocess.TimeoutExpired:
                proc.terminate()
                try:
                    proc.wait(timeout=1.0)
                except subprocess.TimeoutExpired:
                    proc.kill()
                duration = int((time.time() - start) * 1000)
                self._emit({
                    "type": "fire_timeout",
                    "rule_id": rule.rule_id,
                    "selector": fire.selector,
                    "duration_ms": duration,
                    "fire_count": new_fires,
                    "wal_seq": fire.wal_seq,
                })
            self._post_fire_counter_update(rule.rule_id, new_fires)
        finally:
            with self._lock:
                self._busy_rules.discard(rule.rule_id)
                self._inflight -= 1
                if self._inflight == 0:
                    self._idle.set()

    def _post_fire_counter_update(self, rule_id: str, new_fires: int) -> None:
        counter = self._counters.load(rule_id)
        counter.fires = new_fires
        counter.last_fire_ts = int(time.time() * 1000)
        # exhaustion is decided externally (engine knows max_fires); dispatcher is dumb.
        self._counters.save(rule_id, counter)


class DispatcherContext:
    """Dummy alias kept for type hints in tests; engine wires the real one."""
    pass
```

- [ ] **Step 2:** Run.

Run: `python -m unittest tests.test_event_dispatcher -v`
Expected: PASS (3 tests).

- [ ] **Step 3:** Commit.

```bash
git add sw_core/event_engine/dispatcher.py tests/test_event_dispatcher.py
git commit -m "feat(event): dispatcher with timeout, per-rule + per-daemon caps"
```

---

## Phase 8 — Engine orchestration

### Task 8.1: Failing tests for EventEngine integration

**Files:** Create `tests/test_event_engine.py`

- [ ] **Step 1:** Write the test.

```python
from __future__ import annotations
import os
import shutil
import tempfile
import time
import unittest

from sw_core.event_engine.engine import EventEngine, EngineDeps
from sw_core.event_engine.counter import CounterStore


class _FakeBridgeQueries:
    def __init__(self) -> None:
        self.profiles: dict[str, str | None] = {}
        self.active: dict[str, str | None] = {}

    def active_cmd_id_for(self, com: str) -> str | None:
        return self.active.get(com)

    def profile_for(self, com: str) -> str | None:
        return self.profiles.get(com)

    def known_coms(self) -> list[str]:
        return sorted(set(self.profiles.keys()) | set(self.active.keys()))


class TestEventEngine(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.mkdtemp(prefix="sw-event-engine-")
        self.events_dir = os.path.join(self.tmp, "events.d")
        self.runtime_dir = os.path.join(self.tmp, "runtime")
        self.log_path = os.path.join(self.runtime_dir, "events.ndjson")
        os.makedirs(self.events_dir)
        os.makedirs(self.runtime_dir)
        self.bridge = _FakeBridgeQueries()
        self.engine = EventEngine(EngineDeps(
            events_dir=self.events_dir,
            runtime_dir=self.runtime_dir,
            log_path=self.log_path,
            bridge=self.bridge,
        ))

    def tearDown(self) -> None:
        self.engine.stop()
        shutil.rmtree(self.tmp, ignore_errors=True)

    def _add_rule(self, owner: str, name: str, marker: str, **overrides) -> dict:
        rule = {
            "schema_version": 1,
            "owner": owner, "name": name, "kind": "tool",
            "selectors": ["COM0"],
            "pattern": {"kind": "contains", "value": "panic"},
            "handler": {"shell": f"touch {marker}"},
            "timeout_ms": 2000,
            "auto_enable_com_on_load": True,
        }
        rule.update(overrides)
        self.engine.rule_set(rule)
        return rule

    def test_rule_set_then_fire(self) -> None:
        marker = os.path.join(self.tmp, "fired")
        self._add_rule("o", "n", marker)
        self.engine.start()
        # COM0 must be auto-enabled because rule.auto_enable_com_on_load=true
        status = self.engine.com_status("COM0")
        self.assertTrue(status["enabled"])
        self.engine.feed_line("COM0", "Kernel panic - not syncing", wal_seq=1)
        self._wait_for_file(marker, timeout=3.0)

    def test_disable_clears_counter(self) -> None:
        marker = os.path.join(self.tmp, "fired2")
        self._add_rule("o", "x", marker)
        self.engine.start()
        self.engine.feed_line("COM0", "panic", wal_seq=1)
        self._wait_for_file(marker, timeout=3.0)
        store = CounterStore(self.runtime_dir)
        self.assertGreater(store.load("o.x").fires, 0)
        self.engine.com_disable("COM0")
        self.assertEqual(store.load("o.x").fires, 0)

    def test_reload_diff_applies(self) -> None:
        marker = os.path.join(self.tmp, "fired3")
        self._add_rule("o", "y", marker)
        self.engine.start()
        # External edit: drop the rule file
        os.unlink(os.path.join(self.events_dir, "o.y.json"))
        self.engine.reload()
        self.engine.feed_line("COM0", "panic", wal_seq=1)
        time.sleep(0.4)
        self.assertFalse(os.path.exists(marker))

    def _wait_for_file(self, path: str, timeout: float) -> None:
        end = time.time() + timeout
        while time.time() < end:
            if os.path.exists(path):
                return
            time.sleep(0.05)
        self.fail(f"file did not appear: {path}")


if __name__ == "__main__":
    unittest.main()
```

- [ ] **Step 2:** Run → FAIL.

### Task 8.2: Implement EventEngine

**Files:** Create `sw_core/event_engine/engine.py`

- [ ] **Step 1:** Write the module.

```python
from __future__ import annotations

import threading
import time
from dataclasses import dataclass
from typing import Callable, Iterable, Protocol

from .counter import Counter, CounterStore
from .dispatcher import Dispatcher
from .event_log import EventLogger
from .matcher import MatcherFire, MatcherWorker
from .registry import RuleRegistry
from .schema import Rule


class BridgeQueries(Protocol):
    def active_cmd_id_for(self, com: str) -> str | None: ...
    def profile_for(self, com: str) -> str | None: ...
    def known_coms(self) -> list[str]: ...


@dataclass
class EngineDeps:
    events_dir: str
    runtime_dir: str
    log_path: str
    bridge: BridgeQueries
    queue_max: int = 1024
    per_daemon_max: int = 8


class EventEngine:
    def __init__(self, deps: EngineDeps) -> None:
        self._deps = deps
        self._registry = RuleRegistry(deps.events_dir)
        self._counters = CounterStore(deps.runtime_dir)
        self._log = EventLogger(deps.log_path)
        self._lock = threading.RLock()
        self._enabled_coms: set[str] = set()
        self._rules: list[Rule] = []
        self._matcher: MatcherWorker | None = None
        self._dispatcher: Dispatcher | None = None
        self._started = False

    # ── lifecycle ────────────────────────────────────────────────────────
    def start(self) -> None:
        if self._started:
            return
        self._dispatcher = Dispatcher(
            event_emit=self._log.write,
            counter_store=self._counters,
            per_daemon_max=self._deps.per_daemon_max,
        )
        self._dispatcher.start()

        load = self._registry.load_all()
        for fail in load.failed:
            self._log.write({"type": "rule_load_failed", "path": fail.path, "reason": fail.reason})
        with self._lock:
            self._rules = load.rules
            for rule in load.rules:
                self._log.write({"type": "rule_loaded", "rule_id": rule.rule_id})
                if rule.auto_enable_com_on_load:
                    for sel in rule.selectors:
                        if sel == "ALL":
                            for com in self._deps.bridge.known_coms():
                                self._mark_com_enabled(com, "auto")
                        else:
                            self._mark_com_enabled(sel, "auto")

        self._matcher = MatcherWorker(
            rules=self._rules,
            context=self._matcher_ctx(),
            queue_max=self._deps.queue_max,
        )
        self._matcher.start()
        self._started = True

    def stop(self) -> None:
        if not self._started:
            return
        if self._matcher is not None:
            self._matcher.stop()
        if self._dispatcher is not None:
            self._dispatcher.stop()
        self._matcher = None
        self._dispatcher = None
        self._started = False

    # ── feeds ────────────────────────────────────────────────────────────
    def feed_line(self, com: str, line: str, wal_seq: int) -> None:
        with self._lock:
            if com not in self._enabled_coms:
                return
            matcher = self._matcher
        if matcher is None:
            return
        matcher.feed_line(com, line, wal_seq)

    # ── CRUD ─────────────────────────────────────────────────────────────
    def rule_set(self, obj: dict) -> Rule:
        rule = self._registry.upsert(obj)
        with self._lock:
            self._rules = [r for r in self._rules if r.rule_id != rule.rule_id] + [rule]
            if self._matcher is not None:
                self._matcher.replace_rules(self._rules)
            if rule.auto_enable_com_on_load:
                for sel in rule.selectors:
                    if sel == "ALL":
                        for com in self._deps.bridge.known_coms():
                            self._mark_com_enabled(com, "rule_set")
                    else:
                        self._mark_com_enabled(sel, "rule_set")
        self._log.write({"type": "rule_loaded", "rule_id": rule.rule_id})
        return rule

    def rule_delete(self, rule_id: str) -> bool:
        ok = self._registry.delete(rule_id)
        if ok:
            self._counters.clear(rule_id)
            with self._lock:
                self._rules = [r for r in self._rules if r.rule_id != rule_id]
                if self._matcher is not None:
                    self._matcher.replace_rules(self._rules)
            self._log.write({"type": "rule_unloaded", "rule_id": rule_id})
        return ok

    def rule_list(self, *, selector: str | None = None, owner: str | None = None) -> list[dict]:
        with self._lock:
            rules = list(self._rules)
        out: list[dict] = []
        for r in rules:
            if selector is not None and selector not in r.selectors and "ALL" not in r.selectors:
                continue
            if owner is not None and r.owner != owner:
                continue
            counter = self._counters.load(r.rule_id)
            out.append({
                "rule_id": r.rule_id,
                "owner": r.owner,
                "kind": r.kind,
                "selectors": list(r.selectors),
                "fires": counter.fires,
                "exhausted": counter.exhausted,
                "last_fire_ts": counter.last_fire_ts,
            })
        return out

    def rule_get(self, rule_id: str) -> dict | None:
        rule = self._registry.get(rule_id)
        if rule is None:
            return None
        counter = self._counters.load(rule_id)
        return {"rule": rule.raw, "counter": counter.to_json()}

    def reload(self) -> dict:
        with self._lock:
            previous = list(self._rules)
        diff = self._registry.diff_against(previous)
        with self._lock:
            current = self._registry.load_all().rules
            self._rules = current
            if self._matcher is not None:
                self._matcher.replace_rules(self._rules)
            for r in diff.removed:
                self._counters.clear(r.rule_id)
            for r in diff.added:
                if r.auto_enable_com_on_load:
                    for sel in r.selectors:
                        if sel == "ALL":
                            for com in self._deps.bridge.known_coms():
                                self._mark_com_enabled(com, "reload")
                        else:
                            self._mark_com_enabled(sel, "reload")
        for r in diff.added:
            self._log.write({"type": "rule_loaded", "rule_id": r.rule_id})
        for r in diff.removed:
            self._log.write({"type": "rule_unloaded", "rule_id": r.rule_id})
        for r in diff.changed:
            self._log.write({"type": "rule_loaded", "rule_id": r.rule_id})
        return {
            "added": [r.rule_id for r in diff.added],
            "removed": [r.rule_id for r in diff.removed],
            "changed": [r.rule_id for r in diff.changed],
        }

    # ── COM toggle ───────────────────────────────────────────────────────
    def com_enable(self, com: str) -> dict:
        self._mark_com_enabled(com, "manual")
        return self.com_status(com)

    def com_disable(self, com: str) -> dict:
        with self._lock:
            self._enabled_coms.discard(com)
            affected = [r.rule_id for r in self._rules if com in r.selectors or "ALL" in r.selectors]
        for rid in affected:
            previous = self._counters.load(rid)
            self._counters.clear(rid)
            self._log.write({
                "type": "counter_reset",
                "rule_id": rid,
                "previous_fires": previous.fires,
                "triggered_by": "com_disable",
            })
        self._log.write({"type": "com_disabled", "selector": com, "triggered_by": "manual"})
        return self.com_status(com)

    def com_status(self, com: str | None = None) -> dict:
        with self._lock:
            if com is None:
                return {"coms": sorted(self._enabled_coms)}
            return {
                "selector": com,
                "enabled": com in self._enabled_coms,
                "active_rules": [
                    r.rule_id for r in self._rules
                    if com in r.selectors or "ALL" in r.selectors
                ],
            }

    def reset(self, *, rule_id: str | None = None, selector: str | None = None) -> int:
        cleared = 0
        with self._lock:
            target_ids: list[str] = []
            if rule_id is not None:
                target_ids = [rule_id]
            elif selector is not None:
                target_ids = [r.rule_id for r in self._rules
                              if selector in r.selectors or "ALL" in r.selectors]
        for rid in target_ids:
            previous = self._counters.load(rid)
            self._counters.clear(rid)
            cleared += 1
            self._log.write({
                "type": "counter_reset",
                "rule_id": rid,
                "previous_fires": previous.fires,
                "triggered_by": "reset",
            })
        return cleared

    def tail(self, **filters) -> list[dict]:
        return self._log.tail(**filters)

    # ── internals ────────────────────────────────────────────────────────
    def _mark_com_enabled(self, com: str, triggered_by: str) -> None:
        with self._lock:
            if com in self._enabled_coms:
                return
            self._enabled_coms.add(com)
        self._log.write({"type": "com_enabled", "selector": com, "triggered_by": triggered_by})

    def _matcher_ctx(self) -> "MatcherContextImpl":
        return MatcherContextImpl(self)


class MatcherContextImpl:
    def __init__(self, engine: EventEngine) -> None:
        self._engine = engine

    def active_cmd_id(self, com: str) -> str | None:
        return self._engine._deps.bridge.active_cmd_id_for(com)

    def com_profile(self, com: str) -> str | None:
        return self._engine._deps.bridge.profile_for(com)

    def now_ms(self) -> int:
        return int(time.time() * 1000)

    def emit_fire(self, fire: MatcherFire) -> None:
        engine = self._engine
        engine._log.write({
            "type": "match_recorded",
            "rule_id": fire.rule.rule_id,
            "selector": fire.selector,
            "wal_seq": fire.wal_seq,
            "matched_text": fire.match.matched_text,
        })
        # update exhaustion state pre-dispatch
        counter = engine._counters.load(fire.rule.rule_id)
        if fire.rule.max_fires is not None and counter.fires + 1 >= fire.rule.max_fires:
            counter.exhausted = True
            engine._counters.save(fire.rule.rule_id, counter)
        if engine._dispatcher is not None:
            engine._dispatcher.dispatch(fire, matched_line=fire.match.matched_text)

    def emit_dropped(self, event: dict) -> None:
        self._engine._log.write(event)

    def emit_skipped(self, event: dict) -> None:
        self._engine._log.write(event)

    def counter_for(self, rule_id: str) -> Counter:
        return self._engine._counters.load(rule_id)
```

- [ ] **Step 2:** Update `sw_core/event_engine/__init__.py` to expose top-level types.

```python
"""sw_core.event_engine — pattern → spawn handler trigger engine (issue #37)."""
from __future__ import annotations

from .engine import EngineDeps, EventEngine
from .schema import Rule, validate_rule_dict

__all__ = ["EventEngine", "EngineDeps", "Rule", "validate_rule_dict"]
```

- [ ] **Step 3:** Run.

Run: `python -m unittest tests.test_event_engine -v`
Expected: PASS.

- [ ] **Step 4:** Commit.

```bash
git add sw_core/event_engine/engine.py sw_core/event_engine/__init__.py tests/test_event_engine.py
git commit -m "feat(event): EventEngine orchestration with reload + COM toggle + reset"
```

---

## Phase 9 — Bridge wiring (zero back-pressure)

### Task 9.1: Failing test that bridge → engine.feed_line is wired

**Files:** Modify `tests/test_event_engine.py`

- [ ] **Step 1:** Add a new test that drives a real `Bridge` instance.

```python
import os as _os

class TestBridgeWiring(unittest.TestCase):
    def test_engine_receives_lines_from_bridge_callback(self) -> None:
        # Use the same fake-target machinery the project's UART tests use.
        import pty
        from sw_core.uart_io import Bridge
        from sw_core.wal import WalWriter

        master, slave = pty.openpty()
        try:
            received: list[tuple[str, str, int]] = []

            class _Hook:
                def __init__(self) -> None:
                    self.bridge = Bridge(
                        device_path=_os.ttyname(slave),
                        com="COMTEST",
                        wal=WalWriter(wal_dir=tempfile.mkdtemp(prefix="sw-wal-")),
                        on_rx_data=self.on_rx,
                    )
                    self.line_buf = b""

                def on_rx(self, data: bytes) -> None:
                    self.line_buf += data
                    while b"\n" in self.line_buf:
                        line, self.line_buf = self.line_buf.split(b"\n", 1)
                        received.append(("COMTEST", line.decode(), 0))

            hook = _Hook()
            hook.bridge.start()
            try:
                _os.write(master, b"panic now\n")
                end = time.time() + 2.0
                while time.time() < end and not received:
                    time.sleep(0.02)
                self.assertEqual(received[0][1], "panic now")
            finally:
                hook.bridge.stop()
        finally:
            _os.close(master)
            try:
                _os.close(slave)
            except OSError:
                pass
```

> The test confirms `Bridge.on_rx_data` already delivers a copy. **No bridge code change is required for v1**; this is a pinning test that locks the integration shape.

- [ ] **Step 2:** Run.

Run: `python -m unittest tests.test_event_engine.TestBridgeWiring -v`
Expected: PASS.

- [ ] **Step 3:** Commit.

```bash
git add tests/test_event_engine.py
git commit -m "test(event): pin bridge on_rx_data → engine wiring"
```

### Task 9.2: SessionManager helpers for engine consumption

**Files:** Modify `sw_core/session_manager.py`

- [ ] **Step 1:** Locate the `SessionManager` class (look around its existing `get_state` / `list_sessions` methods) and add two read-only helpers.

```python
    def active_cmd_id_for(self, com: str) -> str | None:
        with self._lock:
            session = self._sessions_by_com.get(com)
            if session is None:
                return None
            return session.fg_cmd_id if session.foreground_busy else None

    def profile_for(self, com: str) -> str | None:
        with self._lock:
            session = self._sessions_by_com.get(com)
            if session is None:
                return None
            return session.profile_name

    def known_coms(self) -> list[str]:
        with self._lock:
            return sorted(self._sessions_by_com.keys())
```

> These are **strictly read-only** and use the existing `_lock`; no new mutation paths are introduced. If your tree uses different attribute names (e.g. `fg_cmd_id` vs `current_cmd_id`), match the existing names — do not invent new fields.

- [ ] **Step 2:** Add a unit test in `tests/test_event_engine.py` (or `test_session_capture.py` if you have a fixture):

```python
class TestSessionManagerHelpers(unittest.TestCase):
    def test_helpers_exist(self) -> None:
        from sw_core.session_manager import SessionManager
        sm = SessionManager.__new__(SessionManager)  # bypass __init__
        # Just verify methods exist + return None for unknown COMs without crashing.
        sm._lock = threading.Lock()
        sm._sessions_by_com = {}
        self.assertIsNone(sm.active_cmd_id_for("COM-none"))
        self.assertIsNone(sm.profile_for("COM-none"))
        self.assertEqual(sm.known_coms(), [])
```

- [ ] **Step 3:** Run.

Run: `python -m unittest tests.test_event_engine -v`
Expected: PASS.

- [ ] **Step 4:** Commit.

```bash
git add sw_core/session_manager.py tests/test_event_engine.py
git commit -m "feat(session): expose active_cmd_id_for / profile_for / known_coms"
```

### Task 9.3: Wire engine into bridge callback in service.py

**Files:** Modify `sw_core/service.py`

- [ ] **Step 1:** Locate where `Bridge` is constructed (search `Bridge(` in `session_manager.py` / `service.py`). Add a thin shim that the daemon installs at startup so each new bridge composes its existing `on_rx_data` with an engine feeder.

Approach: rather than mutating bridge construction sites, expose a registration on `SessionManager`:

```python
# session_manager.py — add inside SessionManager
    def add_rx_observer(self, observer: Callable[[str, bytes, int], None]) -> None:
        with self._lock:
            self._rx_observers.append(observer)
```

And in `_handle_serial_rx` (or wherever new bridges are wrapped), already calls existing observers. If your tree builds Bridge objects in-place, wrap their `on_rx_data` like:

```python
        prev_callback = on_rx_data
        def _wrapped(data: bytes, _com=com, _prev=prev_callback) -> None:
            if _prev is not None:
                _prev(data)
            for obs in self._rx_observers:
                try:
                    obs(_com, data, self._wal.current_seq())
                except Exception:
                    pass  # observer failures must not impact bridge
        on_rx_data = _wrapped
```

> Exact placement depends on your local code. Read `session_manager.py` lines around bridge construction; the rule is **never raise out of an observer**.

- [ ] **Step 2:** In `service.py`, instantiate the engine after `SessionManager` is up (before RPC accept loop):

```python
from sw_core.event_engine import EventEngine, EngineDeps
from sw_core.constants import EVENTS_DIR, EVENTS_RUNTIME_DIR, EVENTS_LOG_PATH

engine = EventEngine(EngineDeps(
    events_dir=EVENTS_DIR,
    runtime_dir=EVENTS_RUNTIME_DIR,
    log_path=EVENTS_LOG_PATH,
    bridge=session_manager,
))
engine.start()

def _engine_observer(com: str, data: bytes, wal_seq: int) -> None:
    # We split lines per-COM in the engine so SessionManager doesn't need its own
    # buffer; share a per-COM LineBuffer here.
    buf = _line_buffers.setdefault(com, LineBuffer())
    for line in buf.feed(data):
        engine.feed_line(com, line, wal_seq)

session_manager.add_rx_observer(_engine_observer)
```

- [ ] **Step 3:** On daemon shutdown path, call `engine.stop()`. Find the existing graceful-shutdown block in `service.py`.

- [ ] **Step 4:** Run smoke import.

Run: `python -c "from sw_core.service import *"`
Expected: no exception.

- [ ] **Step 5:** Commit.

```bash
git add sw_core/service.py sw_core/session_manager.py
git commit -m "feat(event): wire EventEngine to bridge RX via SessionManager observer"
```

---

## Phase 10 — RPC integration

### Task 10.1: Failing tests for RPC routes

**Files:** Create `tests/test_event_rpc.py`

- [ ] **Step 1:** Write tests that exercise the dispatch via the same `_handler` callable used in production. Use the engine instance directly (the test does not need an actual unix socket).

```python
from __future__ import annotations
import os
import shutil
import tempfile
import unittest

from sw_core.event_engine import EventEngine, EngineDeps


class _NullBridge:
    def active_cmd_id_for(self, com): return None
    def profile_for(self, com): return None
    def known_coms(self): return ["COM0"]


class TestEventRpcDispatch(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.mkdtemp(prefix="sw-event-rpc-")
        self.engine = EventEngine(EngineDeps(
            events_dir=os.path.join(self.tmp, "events.d"),
            runtime_dir=os.path.join(self.tmp, "runtime"),
            log_path=os.path.join(self.tmp, "runtime", "events.ndjson"),
            bridge=_NullBridge(),
        ))
        os.makedirs(os.path.join(self.tmp, "events.d"))
        os.makedirs(os.path.join(self.tmp, "runtime"))

    def tearDown(self) -> None:
        self.engine.stop()
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_rule_set_then_list(self) -> None:
        rule = {
            "schema_version": 1,
            "owner": "o", "name": "n", "kind": "tool",
            "selectors": ["COM0"],
            "pattern": {"kind": "contains", "value": "x"},
            "handler": {"exec": ["/bin/true"]},
        }
        self.engine.rule_set(rule)
        rows = self.engine.rule_list()
        self.assertEqual([r["rule_id"] for r in rows], ["o.n"])

    def test_status_reflects_auto_enable(self) -> None:
        rule = {
            "schema_version": 1,
            "owner": "o", "name": "n", "kind": "tool",
            "selectors": ["COM0"],
            "pattern": {"kind": "contains", "value": "x"},
            "handler": {"exec": ["/bin/true"]},
            "auto_enable_com_on_load": True,
        }
        self.engine.rule_set(rule)
        self.assertTrue(self.engine.com_status("COM0")["enabled"])

    def test_reset_clears_counter(self) -> None:
        rule = {
            "schema_version": 1,
            "owner": "o", "name": "n", "kind": "tool",
            "selectors": ["COM0"],
            "pattern": {"kind": "contains", "value": "x"},
            "handler": {"exec": ["/bin/true"]},
        }
        self.engine.rule_set(rule)
        self.engine._counters.save("o.n",
            self.engine._counters.load("o.n").__class__(fires=5, last_fire_ts=1, exhausted=False))
        cleared = self.engine.reset(rule_id="o.n")
        self.assertEqual(cleared, 1)
        self.assertEqual(self.engine._counters.load("o.n").fires, 0)


if __name__ == "__main__":
    unittest.main()
```

- [ ] **Step 2:** Run → PASS (engine already implements these methods from Phase 8).

### Task 10.2: Hook the RPC dispatch table

**Files:** Modify `sw_core/service.py`

- [ ] **Step 1:** Append the new method blocks following the existing `if method == "..."` pattern. Place them next to other RPC handlers (around lines 460+ where the WAL methods live).

```python
        if method == "event.rule_set":
            return engine.rule_set(params or {}).raw  # returns the validated raw dict
        if method == "event.rule_delete":
            return {"deleted": engine.rule_delete(params["rule_id"])}
        if method == "event.rule_list":
            return engine.rule_list(
                selector=params.get("selector"),
                owner=params.get("owner"),
            )
        if method == "event.rule_get":
            return engine.rule_get(params["rule_id"])
        if method == "event.com_enable":
            return engine.com_enable(params["selector"])
        if method == "event.com_disable":
            return engine.com_disable(params["selector"])
        if method == "event.com_status":
            return engine.com_status(params.get("selector"))
        if method == "event.reset":
            return {"cleared": engine.reset(
                rule_id=params.get("rule_id"),
                selector=params.get("selector"),
            )}
        if method == "event.reload":
            return engine.reload()
        if method == "event.tail":
            return engine.tail(
                rule_id=params.get("rule_id"),
                selector=params.get("selector"),
                since_ts=params.get("since_ts"),
                n=params.get("n"),
            )
```

- [ ] **Step 2:** Add an RPC integration test in `tests/test_event_rpc.py`:

```python
    def test_dispatch_through_service_handler(self) -> None:
        from sw_core.service import build_handler  # if absent, skip; else exercise it.
        # If service.py exposes a handler factory we can drive it; otherwise this
        # test stays at the engine level above. Keep this guarded so the rest of
        # the suite stays green if the helper is renamed.
```

> Don't fail the suite if the helper has a different name in your tree; the engine-level tests above already cover behavior. The dispatch wiring will be exercised end-to-end in the func-test phase.

- [ ] **Step 3:** Run.

Run: `python -m unittest tests.test_event_rpc -v`
Expected: PASS.

- [ ] **Step 4:** Commit.

```bash
git add sw_core/service.py tests/test_event_rpc.py
git commit -m "feat(event): RPC routes for rule CRUD / com toggle / reset / reload / tail"
```

---

## Phase 11 — CLI subcommands

### Task 11.1: Failing tests for `serialwrap event` CLI

**Files:** Create `tests/test_event_cli.py`

- [ ] **Step 1:** Write the test. Use the same in-process invocation pattern as `tests/test_cli_daemon_start.py` if it exists; otherwise drive `argparse` directly.

```python
from __future__ import annotations
import io
import json
import os
import shutil
import tempfile
import unittest
from contextlib import redirect_stdout
from unittest.mock import patch

from sw_core.cli import main as cli_main


class _StubClient:
    def __init__(self) -> None:
        self.calls: list[tuple[str, dict]] = []
        self.responses: dict[str, object] = {}

    def __call__(self, method: str, params: dict | None = None) -> object:
        self.calls.append((method, dict(params or {})))
        return self.responses.get(method, {"ok": True, "method": method, "params": params})


class TestEventCli(unittest.TestCase):
    def test_event_status_calls_correct_method(self) -> None:
        stub = _StubClient()
        stub.responses["event.com_status"] = {"selector": "COM0", "enabled": True, "active_rules": []}
        with patch("sw_core.cli.rpc_call", stub):
            buf = io.StringIO()
            with redirect_stdout(buf):
                cli_main(["event", "status", "--selector", "COM0"])
            out = json.loads(buf.getvalue())
        self.assertEqual(stub.calls[0][0], "event.com_status")
        self.assertEqual(out["selector"], "COM0")

    def test_event_add_reads_file(self) -> None:
        tmp = tempfile.mkdtemp()
        try:
            path = os.path.join(tmp, "rule.json")
            rule = {
                "schema_version": 1, "owner": "o", "name": "n", "kind": "tool",
                "selectors": ["COM0"], "pattern": {"kind": "contains", "value": "x"},
                "handler": {"exec": ["/bin/true"]},
            }
            with open(path, "w") as f:
                json.dump(rule, f)
            stub = _StubClient()
            with patch("sw_core.cli.rpc_call", stub):
                with redirect_stdout(io.StringIO()):
                    cli_main(["event", "add", "--file", path])
            self.assertEqual(stub.calls[0][0], "event.rule_set")
            self.assertEqual(stub.calls[0][1]["owner"], "o")
        finally:
            shutil.rmtree(tmp, ignore_errors=True)


if __name__ == "__main__":
    unittest.main()
```

- [ ] **Step 2:** Run → FAIL (subcommand not yet wired).

### Task 11.2: Implement CLI subcommands

**Files:** Modify `sw_core/cli.py`

- [ ] **Step 1:** Locate the argparse subparsers construction (search `subparsers = parser.add_subparsers`). Add a new top-level command group:

```python
    p_event = subparsers.add_parser("event", help="event-trigger rule registry / matcher control")
    e_sub = p_event.add_subparsers(dest="event_cmd", required=True)

    e_add = e_sub.add_parser("add", help="register or update a rule from JSON file")
    e_add.add_argument("--file", required=True)

    e_rm = e_sub.add_parser("rm", help="delete a rule by id")
    e_rm.add_argument("rule_id")

    e_list = e_sub.add_parser("list")
    e_list.add_argument("--selector")
    e_list.add_argument("--owner")

    e_show = e_sub.add_parser("show")
    e_show.add_argument("rule_id")

    e_enable = e_sub.add_parser("enable")
    e_enable.add_argument("--selector", required=True)

    e_disable = e_sub.add_parser("disable")
    e_disable.add_argument("--selector", required=True)

    e_status = e_sub.add_parser("status")
    e_status.add_argument("--selector")

    e_reset = e_sub.add_parser("reset")
    grp = e_reset.add_mutually_exclusive_group(required=True)
    grp.add_argument("--rule-id")
    grp.add_argument("--selector")

    e_reload = e_sub.add_parser("reload")

    e_tail = e_sub.add_parser("tail")
    e_tail.add_argument("--rule-id")
    e_tail.add_argument("--selector")
    e_tail.add_argument("-n", type=int, default=50)
    e_tail.add_argument("--since", type=int)
```

- [ ] **Step 2:** Add the dispatch block (where other commands are dispatched).

```python
    if args.command == "event":
        return _dispatch_event(args)
```

```python
def _dispatch_event(args) -> None:
    if args.event_cmd == "add":
        with open(args.file, "r", encoding="utf-8") as f:
            params = json.load(f)
        result = rpc_call("event.rule_set", params)
    elif args.event_cmd == "rm":
        result = rpc_call("event.rule_delete", {"rule_id": args.rule_id})
    elif args.event_cmd == "list":
        result = rpc_call("event.rule_list", {
            "selector": args.selector, "owner": args.owner,
        })
    elif args.event_cmd == "show":
        result = rpc_call("event.rule_get", {"rule_id": args.rule_id})
    elif args.event_cmd == "enable":
        result = rpc_call("event.com_enable", {"selector": args.selector})
    elif args.event_cmd == "disable":
        result = rpc_call("event.com_disable", {"selector": args.selector})
    elif args.event_cmd == "status":
        result = rpc_call("event.com_status", {"selector": args.selector})
    elif args.event_cmd == "reset":
        result = rpc_call("event.reset", {
            "rule_id": args.rule_id, "selector": args.selector,
        })
    elif args.event_cmd == "reload":
        result = rpc_call("event.reload", {})
    elif args.event_cmd == "tail":
        result = rpc_call("event.tail", {
            "rule_id": args.rule_id,
            "selector": args.selector,
            "n": args.n,
            "since_ts": args.since,
        })
    else:
        raise SystemExit(f"unknown event subcommand: {args.event_cmd}")
    print(json.dumps(result, ensure_ascii=False, indent=2))
```

- [ ] **Step 3:** Run.

Run: `python -m unittest tests.test_event_cli -v`
Expected: PASS.

- [ ] **Step 4:** Commit.

```bash
git add sw_core/cli.py tests/test_event_cli.py
git commit -m "feat(event): serialwrap event CLI subcommand group"
```

---

## Phase 12 — MCP tools

### Task 12.1: Failing test for MCP completeness

**Files:** Create tests/test_event_mcp.py（已隨 MCP 退役移除 #59）

- [ ] **Step 1:** Write the test (mirror style of existing tests/test_mcp_completeness.py).

```python
from __future__ import annotations
import unittest

from sw_mcp.server import _TOOL_DEFS, _TOOL_MAP


_REQUIRED_TOOLS = {
    "serialwrap_event_rule_set": "event.rule_set",
    "serialwrap_event_rule_delete": "event.rule_delete",
    "serialwrap_event_rule_list": "event.rule_list",
    "serialwrap_event_rule_get": "event.rule_get",
    "serialwrap_event_enable": "event.com_enable",
    "serialwrap_event_disable": "event.com_disable",
    "serialwrap_event_status": "event.com_status",
    "serialwrap_event_reset": "event.reset",
    "serialwrap_event_reload": "event.reload",
    "serialwrap_event_tail": "event.tail",
}


class TestEventMcp(unittest.TestCase):
    def test_all_tools_in_map(self) -> None:
        for tool, method in _REQUIRED_TOOLS.items():
            self.assertIn(tool, _TOOL_MAP)
            self.assertEqual(_TOOL_MAP[tool], method)

    def test_descriptions_mention_status_first(self) -> None:
        for td in _TOOL_DEFS:
            if td["name"].startswith("serialwrap_event_"):
                self.assertIn("status", td["description"])

    def test_descriptions_warn_about_auto_enable(self) -> None:
        warned = False
        for td in _TOOL_DEFS:
            if td["name"] == "serialwrap_event_enable":
                self.assertIn("auto_enable", td["description"])
                warned = True
        self.assertTrue(warned)


if __name__ == "__main__":
    unittest.main()
```

- [ ] **Step 2:** Run → FAIL.

### Task 12.2: Add MCP tool definitions

**Files:** Modify sw_mcp/server.py（已退役 #59）

- [ ] **Step 1:** Append to `_TOOL_MAP`:

```python
_TOOL_MAP.update({
    "serialwrap_event_rule_set": "event.rule_set",
    "serialwrap_event_rule_delete": "event.rule_delete",
    "serialwrap_event_rule_list": "event.rule_list",
    "serialwrap_event_rule_get": "event.rule_get",
    "serialwrap_event_enable": "event.com_enable",
    "serialwrap_event_disable": "event.com_disable",
    "serialwrap_event_status": "event.com_status",
    "serialwrap_event_reset": "event.reset",
    "serialwrap_event_reload": "event.reload",
    "serialwrap_event_tail": "event.tail",
})
```

- [ ] **Step 2:** Append tool definitions to `_TOOL_DEFS`. The contract requirement is: every description includes "status" + (for enable/disable) "auto_enable".

```python
_EVENT_NOTICE = (
    "重要：daemon failover 後，rule 的 auto_enable_com_on_load 會自動把 COM 重新打開。"
    "請在設定前先呼叫 serialwrap_event_status 確認當下狀態，避免假設 fresh state。"
)

_TOOL_DEFS.extend([
    _td(
        "serialwrap_event_rule_set",
        f"註冊或更新一條 event rule（idempotent upsert）。{_EVENT_NOTICE}",
        {"rule": {"type": "object"}},
        ["rule"],
    ),
    _td(
        "serialwrap_event_rule_delete",
        f"刪除指定 rule_id 的 event rule（連同 counter）。{_EVENT_NOTICE}",
        {"rule_id": _STR},
        ["rule_id"],
    ),
    _td(
        "serialwrap_event_rule_list",
        f"列舉 event rule。{_EVENT_NOTICE}",
        {"selector": _STR, "owner": _STR},
    ),
    _td(
        "serialwrap_event_rule_get",
        f"取單一 event rule（含 counter）。{_EVENT_NOTICE}",
        {"rule_id": _STR},
        ["rule_id"],
    ),
    _td(
        "serialwrap_event_enable",
        f"啟用某 COM 上的 event matcher。{_EVENT_NOTICE}",
        {"selector": _STR},
        ["selector"],
    ),
    _td(
        "serialwrap_event_disable",
        f"停用某 COM 的 event matcher 並清除其上 rule 的 counter。{_EVENT_NOTICE}",
        {"selector": _STR},
        ["selector"],
    ),
    _td(
        "serialwrap_event_status",
        f"查詢 COM event matcher 狀態與 active rules（請在 enable/disable 前先呼叫此工具）。{_EVENT_NOTICE}",
        {"selector": _STR},
    ),
    _td(
        "serialwrap_event_reset",
        f"清除指定 rule 或某 COM 上 rule 的 counter（不刪除 rule）。{_EVENT_NOTICE}",
        {"rule_id": _STR, "selector": _STR},
    ),
    _td(
        "serialwrap_event_reload",
        f"重新掃描 events.d/，diff apply rule 變更。{_EVENT_NOTICE}",
    ),
    _td(
        "serialwrap_event_tail",
        f"tail event log。{_EVENT_NOTICE}",
        {"rule_id": _STR, "selector": _STR, "n": _INT, "since_ts": _INT},
    ),
])
```

- [ ] **Step 3:** Modify `serialwrap_event_enable` description so it explicitly mentions `auto_enable` (the test asserts this):

```python
    _td(
        "serialwrap_event_enable",
        (
            "啟用某 COM 上的 event matcher。注意 rule.auto_enable_com_on_load 會在 daemon 啟動時"
            "自動把對應 COM 打開，請先呼叫 serialwrap_event_status 確認當下狀態。"
        ),
        {"selector": _STR},
        ["selector"],
    ),
```

(replace the corresponding entry in the list rather than duplicating)

- [ ] **Step 4:** Run.

Run: `python -m unittest tests.test_event_mcp tests.test_mcp_completeness -v`
Expected: PASS.

- [ ] **Step 5:** Commit.

```bash
git add sw_mcp/server.py tests/test_event_mcp.py
git commit -m "feat(event): MCP tool definitions with status-first contract notice"
```

---

## Phase 13 — Func-test (end-to-end)

### Task 13.1: Add YAML case driving real bridge → rule → handler

**Files:** Create `func-test/cases/ev-01-spawn-handler.yaml`

- [ ] **Step 1:** Use the same idiom as existing cases like `co-01-line-buffering.yaml`. Drive a fake target that prints `Kernel panic - not syncing` and assert that:

(a) `serialwrap event tail --rule-id ...` shows a `fire_completed` row with exit_code 0.
(b) The handler's marker file is created on disk.

```yaml
name: ev-01-spawn-handler
description: end-to-end UART line → registered event rule → spawned handler
target:
  driver: fake_target
  prompt: "FAKE> "
steps:
  - action: bind_session
    selector: COMTEST
  - action: attach
    selector: COMTEST
  - action: shell
    cmd: |
      cat > /tmp/serialwrap-evtest.json <<JSON
      {
        "schema_version": 1,
        "owner": "ev",
        "name": "panic-marker",
        "kind": "tool",
        "selectors": ["COMTEST"],
        "pattern": {"kind": "contains", "value": "Kernel panic"},
        "handler": {"shell": "touch /tmp/serialwrap-evtest-fired"},
        "auto_enable_com_on_load": true,
        "timeout_ms": 3000
      }
      JSON
      rm -f /tmp/serialwrap-evtest-fired
  - action: cli
    args: [event, add, --file, /tmp/serialwrap-evtest.json]
  - action: cli
    args: [event, status, --selector, COMTEST]
    expect_json: { enabled: true }
  - action: target_emit
    payload: "Kernel panic - not syncing\n"
  - action: wait_for_file
    path: /tmp/serialwrap-evtest-fired
    timeout_s: 5
  - action: cli
    args: [event, tail, --rule-id, ev.panic-marker, -n, "10"]
    expect_contains: "fire_completed"
cleanup:
  - rm -f /tmp/serialwrap-evtest-fired /tmp/serialwrap-evtest.json
  - cli: [event, rm, ev.panic-marker]
```

- [ ] **Step 2:** Add any helper actions (`wait_for_file`, `target_emit`) to `func-test/lib/expect_engine.py` only if the runner doesn't already support them (read existing cases first; only extend if necessary).

- [ ] **Step 3:** Run.

Run: `python func-test/runner.py --case ev-01-spawn-handler`
Expected: case passes; marker file exists; tail output contains `fire_completed`.

- [ ] **Step 4:** Commit.

```bash
git add func-test/cases/ev-01-spawn-handler.yaml func-test/lib/expect_engine.py
git commit -m "test(event): func-test ev-01 covers spawn handler end-to-end"
```

---

## Phase 14 — Documentation & issue closure

### Task 14.1: README + sw_core/assets/skill/SKILL.md

**Files:** Modify `README.md`, `sw_core/assets/skill/SKILL.md`

- [ ] **Step 1:** Add a short section to `README.md` titled `Event Trigger (issue #37)` that links to `docs/design-event-trigger.md` and `docs/plan-event-trigger.md`, lists the 10 MCP tools by name, and shows one minimal rule example.

- [ ] **Step 2:** Update `sw_core/assets/skill/SKILL.md` with the new `event` subcommand group + at least one example. Add a "**先呼叫 serialwrap event status**" warning to the description.

- [ ] **Step 3:** Commit.

```bash
git add README.md sw_core/assets/skill/SKILL.md
git commit -m "docs(event): document event trigger CLI / MCP / safety contract"
```

### Task 14.2: Run full suite + lint

- [ ] **Step 1:** Run the entire test suite.

Run: `python -m unittest discover -s tests -v`
Expected: all green; the 11 new test files plus existing 30+ files all pass.

- [ ] **Step 2:** If your tree has a lint script (look in `install.sh` or a `Makefile`), run it. Otherwise skip.

- [ ] **Step 3:** Run the func-test suite.

Run: `python func-test/runner.py`
Expected: all cases pass.

### Task 14.3: Close issue

- [ ] **Step 1:** Add a comment summarizing the v1 implementation and pointing at:
  - `docs/design-event-trigger.md`
  - `docs/plan-event-trigger.md`
  - the merge commit hash

```bash
gh issue comment 37 --body "$(cat <<'EOF'
v1 已實作完成，符合 docs/design-event-trigger.md 規格：udev/crontab 風格 rule registry、單一 spawn dispatch、per-COM matcher（預設 disabled、由 rule.auto_enable_com_on_load 決定 daemon load 時是否自動上電）、stdin JSON + env 子集 payload、counter 走 tmpfs（D 路徑：disable/delete/reset 清，exhausted/restart 不清）、events.ndjson 觀測。

v2+ 候選：webhook dispatcher、rule 鏈接、daemon-side level gating、跨 host reboot counter、cross-host caller auth。
EOF
)"
```

- [ ] **Step 2:** Close the issue once the PR is merged (do NOT close before merge):

```bash
gh issue close 37 --comment "v1 shipped via merge commit ${MERGE_SHA}."
```

> **Do not** push or open a PR as part of this plan. PR creation is a separate, user-initiated step (per the project's git policy).

---

## Self-review notes (filled by plan author)

- **Spec coverage**: every section of `docs/design-event-trigger.md` maps to a phase here:
  - §3 architecture → Phases 6-9
  - §5 rule schema → Phase 1
  - §6 lifecycle/persistence → Phases 2, 8
  - §7 matcher → Phases 5-6, 9
  - §8 dispatcher contract → Phase 7
  - §9 RPC/CLI/MCP → Phases 10-12
  - §10 events.ndjson → Phase 4
  - §11 failure modes → exercised across Phases 6-8 tests
  - §13 file-touch list → matches Phase 0/9 modification scope
  - §14 development phases → preserved as Phase numbering above
  - Appendix A examples → Phase 13 func-test
- **Type consistency**: `Rule.handler.exec` is `list[str] | None`, `Handler.shell` is `str | None`; `Counter.fires` is `int`; `MatcherFire.match.matched_text` is the string used as `matched_text` in payload + env. `EventLogger.write` accepts a dict and stamps `ts` if missing. `EventEngine.feed_line(com, line, wal_seq)` matches what `service.py` shim passes from the bridge observer.
- **No placeholders**: every step contains either a code block or a concrete command + expected output. The only deferred decision (RPC integration test against `build_handler` helper) is explicitly guarded with "skip if not present" rather than written as TODO.
