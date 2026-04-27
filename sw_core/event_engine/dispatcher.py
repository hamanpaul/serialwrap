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
    """Spawn pool with per-rule concurrency=1, per-daemon cap configurable."""

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
            self._executor = ThreadPoolExecutor(
                max_workers=self._per_daemon_max,
                thread_name_prefix="event-dispatch",
            )

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
        try:
            self._executor.submit(self._run_handler, fire, matched_line)
        except Exception as exc:
            with self._lock:
                self._busy_rules.discard(rule.rule_id)
                self._inflight -= 1
                if self._inflight == 0:
                    self._idle.set()
            self._emit({
                "type": "fire_failed",
                "rule_id": rule.rule_id,
                "selector": fire.selector,
                "reason": str(exc),
            })

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
            if rule.handler.exec is not None:
                argv = list(rule.handler.exec)
            else:
                argv = ["/bin/sh", "-c", rule.handler.shell or ""]
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
                    start_new_session=True,
                )
            except OSError as exc:
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
                try:
                    os.killpg(os.getpgid(proc.pid), signal.SIGTERM)
                except (ProcessLookupError, PermissionError):
                    pass
                try:
                    proc.wait(timeout=1.0)
                except subprocess.TimeoutExpired:
                    try:
                        os.killpg(os.getpgid(proc.pid), signal.SIGKILL)
                    except (ProcessLookupError, PermissionError):
                        pass
                try:
                    proc.communicate()
                except Exception:
                    pass
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
        self._counters.save(rule_id, counter)


class DispatcherContext:
    """Dummy alias kept for type hints in tests; engine wires the real one."""
    pass
