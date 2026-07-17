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

    def unregister_session(self, session_id: str, error_code: str = "FLUSHED_BY_RECOVERY") -> None:
        with self._lock:
            stop = self._stops.pop(session_id, None)
            pq = self._queues.pop(session_id, None)
            th = self._threads.pop(session_id, None)
            # 佇列已丟棄：與 pop 在**同一把鎖內**原子終結所有尚未啟動的 queued 命令（#128）。
            # 否則 stale accepted 記錄永無 done_at、永久計入 _count_pending_locked 佔用
            # CMD_PENDING_MAX 額度，數次 recovery episode 後該 session 直到 daemon 重啟前
            # 一律 SESSION_QUEUE_FULL。
            # 不得延後到鎖外（例如 join 之後）再 flush：舊 worker 卡在 send_cb 超過 join
            # timeout 時，期間若發生 re-register + 新 submit，遲到的 flush 會把新佇列上的
            # 健康 accepted 命令誤標終結、永不執行（epoch race，#128 review F1）。
            # 與 pop 原子完成則不漏不多殺：pop 前入列者必被掃到、pop 後的 submit 回
            # SESSION_NOT_READY、已 dequeue 的 in-flight 非 accepted 不受影響，
            # flush 與取件間的殘餘 race 由 worker 的 done_at 防線接住。
            self._flush_session_locked(session_id, error_code)
        if stop:
            stop.set()
        if pq:
            try:
                pq.put_nowait(_QueuedCommand(sort_key=(0, 0), cmd_id="", session_id="", command="", source="", mode="", timeout_s=0.0, expected_duration_s=None))
            except Exception:
                pass
        if th and th.is_alive() and th is not threading.current_thread():
            th.join(timeout=1.0)

    def flush_session(self, session_id: str, error_code: str = "FLUSHED_BY_RECOVERY") -> int:
        """終結該 session「尚未啟動」（status=accepted、done_at=None）的佇列命令（#128）。

        鎖外公開入口（保留給測試與未來的顯式 flush 需求）；``unregister_session`` 內
        改以 ``_flush_session_locked`` 於首段鎖內與 pop queue 原子執行，勿再從該處
        呼叫本方法（避免 epoch race，#128 review F1）。回傳被 flush 的筆數。
        """
        with self._lock:
            return self._flush_session_locked(session_id, error_code)

    def _flush_session_locked(self, session_id: str, error_code: str) -> int:
        """flush 實作：須在持有 ``self._lock`` 下呼叫（#128）。

        以 ``status=error`` + ``error_code`` + ``done_at`` 標記終端態：client 對這些 cmd_id 的
        ``command.get`` 會看到明確的「未執行、可於 session 回 READY 後重送」語意，且記錄
        轉為可淘汰、pending 額度即刻釋放。in-flight（running/interactive）命令不重複標記，
        留給 worker 以真實結果終結。

        刻意**不**呼叫 ``_evict_commands_locked()``：``_commands`` 超量（> CMD_HISTORY_MAX）
        時 evict 可能把剛 flush 的記錄當場淘汰，client 隨後 ``command.get`` 只拿得到
        CMD_NOT_FOUND 而非 FLUSHED_BY_RECOVERY，喪失「未執行、可重送」語意（#128 review
        F3）。history 收斂交給後續 submit／worker 終結／cancel 路徑的 evict，增長有界
        （pending 受 CMD_PENDING_MAX 封頂、已終結者下次 evict 即回收）。
        """
        now = now_iso()
        flushed = 0
        for rec in self._commands.values():
            if rec.get("session_id") != session_id:
                continue
            if rec.get("done_at") is not None or rec.get("status") != "accepted":
                continue
            rec["status"] = "error"
            rec["error_code"] = error_code
            rec["done_at"] = now
            flushed += 1
        return flushed

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
            self._evict_commands_locked()  # 命令終結即收斂 history，不必等下一次 submit（Copilot 審查）
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
                if rec.get("status") == "canceled" or rec.get("done_at"):
                    # 已被 cancel 或已被 flush 終結（#128）的記錄：跳過，不得重設 running
                    # 或執行 send_cb（防 flush 與取件之間的 race 把終端態覆寫回進行中）。
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
                        self._evict_commands_locked()  # 終結即收斂 history（Copilot 審查）
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
                    self._evict_commands_locked()  # 終結即收斂 history，尖峰後不再長期超量（Copilot 審查）

    def snapshot(self) -> list[dict[str, Any]]:
        with self._lock:
            return [dict(v) for _, v in sorted(self._commands.items(), key=lambda kv: kv[1].get("created_at", ""))]
