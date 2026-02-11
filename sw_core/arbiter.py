from __future__ import annotations

import dataclasses
import queue
import threading
import time
import uuid
from typing import Any, Callable

from .util import now_iso


@dataclasses.dataclass(order=True)
class _QueuedCommand:
    sort_key: tuple[int, int]
    cmd_id: str = dataclasses.field(compare=False)
    session_id: str = dataclasses.field(compare=False)
    command: str = dataclasses.field(compare=False)
    source: str = dataclasses.field(compare=False)
    mode: str = dataclasses.field(compare=False)
    timeout_s: float = dataclasses.field(compare=False)


class CommandArbiter:
    def __init__(self, send_cb: Callable[[str, str, str, str], None]) -> None:
        self._send_cb = send_cb
        self._lock = threading.Lock()
        self._queues: dict[str, queue.PriorityQueue[_QueuedCommand]] = {}
        self._threads: dict[str, threading.Thread] = {}
        self._stops: dict[str, threading.Event] = {}
        self._commands: dict[str, dict[str, Any]] = {}
        self._counter = 0

    def register_session(self, session_id: str) -> None:
        with self._lock:
            if session_id in self._queues:
                return
            pq: queue.PriorityQueue[_QueuedCommand] = queue.PriorityQueue()
            stop_event = threading.Event()
            self._queues[session_id] = pq
            self._stops[session_id] = stop_event
            th = threading.Thread(target=self._worker, args=(session_id, pq, stop_event), daemon=True)
            self._threads[session_id] = th
            th.start()

    def unregister_session(self, session_id: str) -> None:
        with self._lock:
            stop = self._stops.pop(session_id, None)
            pq = self._queues.pop(session_id, None)
            th = self._threads.pop(session_id, None)
        if stop:
            stop.set()
        if pq:
            try:
                pq.put_nowait(_QueuedCommand(sort_key=(0, 0), cmd_id="", session_id="", command="", source="", mode="", timeout_s=0.0))
            except Exception:
                pass
        if th and th.is_alive():
            th.join(timeout=1.0)

    def submit(
        self,
        *,
        session_id: str,
        command: str,
        source: str,
        mode: str,
        timeout_s: float,
        priority: int = 10,
    ) -> dict[str, Any]:
        with self._lock:
            pq = self._queues.get(session_id)
            if pq is None:
                return {"ok": False, "error_code": "SESSION_NOT_READY", "session_id": session_id}
            self._counter += 1
            counter = self._counter

        cmd_id = uuid.uuid4().hex
        now = now_iso()
        rec = {
            "cmd_id": cmd_id,
            "session_id": session_id,
            "command": command,
            "source": source,
            "mode": mode,
            "timeout_s": timeout_s,
            "priority": priority,
            "status": "accepted",
            "created_at": now,
            "accepted_at": now,
            "started_at": None,
            "done_at": None,
            "error_code": None,
        }
        with self._lock:
            self._commands[cmd_id] = rec
        pq.put(_QueuedCommand(sort_key=(priority, counter), cmd_id=cmd_id, session_id=session_id, command=command, source=source, mode=mode, timeout_s=timeout_s))
        return {"ok": True, "cmd_id": cmd_id, "status": "accepted", "session_id": session_id}

    def get(self, cmd_id: str) -> dict[str, Any]:
        with self._lock:
            rec = self._commands.get(cmd_id)
            if rec is None:
                return {"ok": False, "error_code": "CMD_NOT_FOUND", "cmd_id": cmd_id}
            return {"ok": True, "command": dict(rec)}

    def cancel(self, cmd_id: str) -> dict[str, Any]:
        with self._lock:
            rec = self._commands.get(cmd_id)
            if rec is None:
                return {"ok": False, "error_code": "CMD_NOT_FOUND", "cmd_id": cmd_id}
            if rec["status"] in {"done", "error"}:
                return {"ok": False, "error_code": "CMD_NOT_CANCELABLE", "cmd_id": cmd_id}
            rec["status"] = "canceled"
            rec["done_at"] = now_iso()
            return {"ok": True, "cmd_id": cmd_id, "status": "canceled"}

    def _worker(self, session_id: str, pq: queue.PriorityQueue[_QueuedCommand], stop_event: threading.Event) -> None:
        while not stop_event.is_set():
            try:
                item = pq.get(timeout=0.2)
            except queue.Empty:
                continue
            if stop_event.is_set():
                break
            if not item.cmd_id:
                continue

            with self._lock:
                rec = self._commands.get(item.cmd_id)
                if rec is None:
                    continue
                if rec.get("status") == "canceled":
                    continue
                rec["status"] = "running"
                rec["started_at"] = now_iso()

            try:
                self._send_cb(session_id, item.command, item.source, item.cmd_id)
            except Exception:
                with self._lock:
                    rec = self._commands.get(item.cmd_id)
                    if rec:
                        rec["status"] = "error"
                        rec["error_code"] = "SEND_FAILED"
                        rec["done_at"] = now_iso()
                continue

            with self._lock:
                rec = self._commands.get(item.cmd_id)
                if rec and rec.get("status") != "canceled":
                    rec["status"] = "done"
                    rec["done_at"] = now_iso()

    def snapshot(self) -> list[dict[str, Any]]:
        with self._lock:
            return [dict(v) for _, v in sorted(self._commands.items(), key=lambda kv: kv[1].get("created_at", ""))]
