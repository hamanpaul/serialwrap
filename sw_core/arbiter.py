from __future__ import annotations

import dataclasses
import queue
import threading
import time
import uuid
from typing import Any, Callable

from .constants import CMD_HISTORY_MAX, CMD_PENDING_MAX
from .util import now_iso

CMD_WARN_BYTES = 4096
CMD_REJECT_BYTES = 16384


@dataclasses.dataclass(order=True)
class _QueuedCommand:
    sort_key: tuple[int, int]
    cmd_id: str = dataclasses.field(compare=False)
    session_id: str = dataclasses.field(compare=False)
    command: str = dataclasses.field(compare=False)
    source: str = dataclasses.field(compare=False)
    mode: str = dataclasses.field(compare=False)
    timeout_s: float = dataclasses.field(compare=False)
    expected_duration_s: float | None = dataclasses.field(default=None, compare=False)


class CommandArbiter:
    def __init__(self, send_cb: Callable[[str, str, str, str, float, str, float | None], dict[str, Any]]) -> None:
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
                pq.put_nowait(_QueuedCommand(sort_key=(0, 0), cmd_id="", session_id="", command="", source="", mode="", timeout_s=0.0, expected_duration_s=None))
            except Exception:
                pass
        if th and th.is_alive() and th is not threading.current_thread():
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
        expected_duration_s: float | None = None,
    ) -> dict[str, Any]:
        cmd_len = len(command.encode("utf-8", errors="replace"))
        if cmd_len > CMD_REJECT_BYTES:
            return {
                "ok": False,
                "error_code": "CMD_TOO_LONG",
                "cmd_length": cmd_len,
                "limit": CMD_REJECT_BYTES,
                "hint": "Command exceeds 16 KB hard limit. Use file-based injection or split into smaller commands.",
            }
        cmd_warning = None
        if cmd_len > CMD_WARN_BYTES:
            cmd_warning = {
                "code": "CMD_LENGTH_WARNING",
                "cmd_length": cmd_len,
                "soft_limit": CMD_WARN_BYTES,
                "hint": "Command exceeds 4 KB soft limit. Long commands may cause UART buffer overflow or prompt timeout.",
            }
        if "\n" in command:
            return {
                "ok": False,
                "error_code": "CMD_CONTAINS_NEWLINE",
                "hint": "Command must not contain embedded newline characters. "
                        "Split into multiple independent submissions.",
            }

        cmd_id = uuid.uuid4().hex
        now = now_iso()
        rec = {
            "cmd_id": cmd_id,
            "session_id": session_id,
            "command": command,
            "source": source,
            "mode": mode,
            "execution_mode": {"fg": "line", "bg": "background"}.get(mode, mode),
            "timeout_s": timeout_s,
            "expected_duration_s": expected_duration_s,
            "priority": priority,
            "status": "accepted",
            "created_at": now,
            "accepted_at": now,
            "started_at": None,
            "done_at": None,
            "error_code": None,
            "stdout": "",
            "partial": False,
            "background_capture_id": None,
            "interactive_session_id": None,
            "recovery_action": None,
        }
        # admission control + 入列在同一把鎖內原子完成：先擋下超量，再 counter/insert/evict，避免
        # 兩個並發 submit 各自通過檢查後雙雙插入而短暫超出上限（#81 Codex 必修）。
        with self._lock:
            pq = self._queues.get(session_id)
            if pq is None:
                return {"ok": False, "error_code": "SESSION_NOT_READY", "session_id": session_id}
            pending = self._count_pending_locked(session_id)
            if pending >= CMD_PENDING_MAX:
                # 進行中（accepted/running）命令已達 per-session 硬上限：拒絕而非排隊。eviction 只能淘汰
                # 已完成命令，無法回收尚未執行者；少了 admission control，client 比 UART worker 快時
                # _commands 與 PriorityQueue 會持續累積 accepted/running records 而 OOM。
                return {
                    "ok": False,
                    "error_code": "SESSION_QUEUE_FULL",
                    "session_id": session_id,
                    "pending": pending,
                    "limit": CMD_PENDING_MAX,
                    "hint": "Per-session pending command queue is full (backpressure). "
                            "Wait for in-flight commands to drain before submitting more.",
                }
            self._counter += 1
            counter = self._counter
            self._commands[cmd_id] = rec
            self._evict_commands_locked()
        pq.put(_QueuedCommand(sort_key=(priority, counter), cmd_id=cmd_id, session_id=session_id, command=command, source=source, mode=mode, timeout_s=timeout_s, expected_duration_s=expected_duration_s))
        result: dict[str, Any] = {"ok": True, "cmd_id": cmd_id, "status": "accepted", "session_id": session_id}
        if cmd_warning is not None:
            result["warning"] = cmd_warning
        return result

    def _count_pending_locked(self, session_id: str) -> int:
        """該 session 進行中（accepted/running，done_at 為 None）的命令數。須在持有 ``self._lock`` 下呼叫。

        以掃描 _commands 計數而非維護獨立計數器：admission control 把 pending 上限封頂後 _commands
        大小有界（≤ CMD_HISTORY_MAX + pending 上限），掃描成本可控；且避免計數器在多處終結轉移
        （worker 成功/例外、cancel、unregister 殘留）漏增漏減而 drift。
        """
        return sum(
            1 for rec in self._commands.values()
            if rec.get("session_id") == session_id and not rec.get("done_at")
        )

    def _evict_commands_locked(self) -> None:
        """淘汰最舊的「已完成（有 done_at）」命令記錄，使數量回到上限（#81）。

        進行中（done_at 為 None）的命令永不淘汰；全部進行中時暫時超量。
        須在持有 ``self._lock`` 下呼叫。
        """
        if len(self._commands) <= CMD_HISTORY_MAX:
            return
        done = [(cid, rec) for cid, rec in self._commands.items() if rec.get("done_at")]
        done.sort(key=lambda kv: kv[1].get("done_at") or "")
        excess = len(self._commands) - CMD_HISTORY_MAX
        for cid, _rec in done[:excess]:
            self._commands.pop(cid, None)

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
                result = self._send_cb(session_id, item.command, item.source, item.cmd_id, item.timeout_s, item.mode, item.expected_duration_s)
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
                    if isinstance(result, dict):
                        for key in (
                            "stdout",
                            "partial",
                            "background_capture_id",
                            "interactive_session_id",
                            "recovery_action",
                            "execution_mode",
                        ):
                            if key in result:
                                rec[key] = result[key]
                        if not result.get("ok", True):
                            rec["status"] = "error"
                            rec["error_code"] = result.get("error_code") or "COMMAND_FAILED"
                        elif result.get("status") == "interactive":
                            rec["status"] = "interactive"
                        else:
                            rec["status"] = "done"
                    else:
                        rec["status"] = "done"
                    rec["done_at"] = now_iso()

    def snapshot(self) -> list[dict[str, Any]]:
        with self._lock:
            return [dict(v) for _, v in sorted(self._commands.items(), key=lambda kv: kv[1].get("created_at", ""))]
