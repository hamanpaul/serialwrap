from __future__ import annotations

import dataclasses
import queue
import threading
import time
import uuid
from typing import Any, Callable

from .constants import CMD_HISTORY_MAX, CMD_PENDING_MAX
from .command_completion import ERROR_INVALID_COMPLETION_REGEX, valid_completion_regex
from .util import now_iso

CMD_WARN_BYTES = 4096
CMD_REJECT_BYTES = 16384

# 錯誤碼／警告碼字串的單一事實來源（#129 review）：submit() 的拒絕／警告點與
# service.py health.status 的 limits 欄位（reject_error_code 等）皆引用此處，
# 避免同一字串在多處硬編碼而各自漂移。
ERROR_CMD_TOO_LONG = "CMD_TOO_LONG"
ERROR_CMD_CONTAINS_NEWLINE = "CMD_CONTAINS_NEWLINE"
WARNING_CMD_LENGTH = "CMD_LENGTH_WARNING"


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
    completion_regex: str | None = dataclasses.field(default=None, compare=False)
    trusted_human: bool = dataclasses.field(default=False, compare=False)


class CommandArbiter:
    _worker_context = threading.local()

    def __init__(
        self,
        send_cb: Callable[..., dict[str, Any]],
        *,
        admission: Any | None = None,
        on_cancel: Callable[[str], None] | None = None,
    ) -> None:
        self._send_cb = send_cb
        self._admission = admission
        self._on_cancel = on_cancel
        self._lock = threading.Lock()
        self._queues: dict[str, queue.PriorityQueue[_QueuedCommand]] = {}
        self._threads: dict[str, threading.Thread] = {}
        self._stops: dict[str, threading.Event] = {}
        self._commands: dict[str, dict[str, Any]] = {}
        self._admission_tickets: dict[str, Any] = {}
        self._running: dict[str, str] = {}
        self._cancel_callbacks: dict[str, Callable[[str], None]] = {}
        self._counter = 0

    @classmethod
    def register_current_cancel_callback(
        cls, cmd_id: str, callback: Callable[[str], None]
    ) -> bool:
        """讓 send callback 內的 manager 為目前 worker 命令登記取消撤銷器。"""
        arbiter = getattr(cls._worker_context, "arbiter", None)
        active_cmd_id = getattr(cls._worker_context, "cmd_id", None)
        if arbiter is None or active_cmd_id != cmd_id:
            return False
        cancel_now = False
        with arbiter._lock:
            rec = arbiter._commands.get(cmd_id)
            if rec is None:
                return False
            if rec.get("status") == "canceled" or rec.get("done_at"):
                cancel_now = rec.get("status") == "canceled"
            else:
                arbiter._cancel_callbacks[cmd_id] = callback
        if cancel_now:
            callback(cmd_id)
        return True

    @classmethod
    def unregister_current_cancel_callback(cls, cmd_id: str) -> None:
        arbiter = getattr(cls._worker_context, "arbiter", None)
        active_cmd_id = getattr(cls._worker_context, "cmd_id", None)
        if arbiter is None or active_cmd_id != cmd_id:
            return
        with arbiter._lock:
            arbiter._cancel_callbacks.pop(cmd_id, None)

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

        #156：``SerialwrapService._on_command_flush`` 已呼叫本方法（掛在
        ``SessionManager._recover_after_failure`` 的 CTRL_C/CTRL_D 攔截成功路徑，
        session 全程停留 READY、不經過 ``unregister_session``）——已有呼叫方，非死碼。
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
            if rec.get("done_at") is not None:
                self._finish_admission_locked(str(rec.get("cmd_id") or ""))
                continue
            if rec.get("status") != "accepted":
                continue
            rec["status"] = "error"
            rec["error_code"] = error_code
            rec["done_at"] = now
            ticket = self._admission_tickets.pop(str(rec.get("cmd_id") or ""), None)
            if ticket is not None and self._admission is not None:
                self._admission.done(ticket)
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
        completion_regex: str | None = None,
        admission_com: str | None = None,
        admission_exempt: bool = False,
        trusted_human: bool = False,
    ) -> dict[str, Any]:
        if not valid_completion_regex(completion_regex):
            return {"ok": False, "error_code": ERROR_INVALID_COMPLETION_REGEX}
        cmd_len = len(command.encode("utf-8", errors="replace"))
        if cmd_len > CMD_REJECT_BYTES:
            return {
                "ok": False,
                "error_code": ERROR_CMD_TOO_LONG,
                "cmd_length": cmd_len,
                "limit": CMD_REJECT_BYTES,
                "hint": "Command exceeds 16 KB hard limit. Use file-based injection or split into smaller commands.",
            }
        cmd_warning = None
        if cmd_len > CMD_WARN_BYTES:
            cmd_warning = {
                "code": WARNING_CMD_LENGTH,
                "cmd_length": cmd_len,
                "soft_limit": CMD_WARN_BYTES,
                "hint": "Command exceeds 4 KB soft limit. Long commands may cause UART buffer overflow or prompt timeout.",
            }
        if "\n" in command:
            return {
                "ok": False,
                "error_code": ERROR_CMD_CONTAINS_NEWLINE,
                "hint": "Command must not contain embedded newline characters. "
                        "Split into multiple independent submissions.",
            }

        ticket = None
        if self._admission is not None and not admission_exempt:
            ticket = self._admission.admit("command.submit", admission_com or session_id.rsplit(":", 1)[-1])
            if ticket is None:
                return {"ok": False, "error_code": "FLUSHED_BY_SHUTDOWN", "reason": "handoff_frozen"}

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
            "completion_regex": completion_regex,
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
                if ticket is not None and self._admission is not None:
                    self._admission.done(ticket)
                return {"ok": False, "error_code": "SESSION_NOT_READY", "session_id": session_id}
            pending = self._count_pending_locked(session_id)
            if pending >= CMD_PENDING_MAX:
                # 進行中（accepted/running）命令已達 per-session 硬上限：拒絕而非排隊。eviction 只能淘汰
                # 已完成命令，無法回收尚未執行者；少了 admission control，client 比 UART worker 快時
                # _commands 與 PriorityQueue 會持續累積 accepted/running records 而 OOM。
                if ticket is not None and self._admission is not None:
                    self._admission.done(ticket)
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
            if ticket is not None:
                self._admission_tickets[cmd_id] = ticket
            self._evict_commands_locked()
        pq.put(
            _QueuedCommand(
                sort_key=(priority, counter),
                cmd_id=cmd_id,
                session_id=session_id,
                command=command,
                source=source,
                mode=mode,
                timeout_s=timeout_s,
                expected_duration_s=expected_duration_s,
                completion_regex=completion_regex,
                trusted_human=trusted_human,
            )
        )
        result: dict[str, Any] = {"ok": True, "cmd_id": cmd_id, "status": "accepted", "session_id": session_id}
        if cmd_warning is not None:
            result["warning"] = cmd_warning
        return result

    def _count_pending_locked(self, session_id: str) -> int:
        """計算該 session 尚占用容量的命令數，包含仍在 worker 執行的 canceled 命令。

        未終結命令以 ``done_at`` 為 None 判定；canceled-running 雖已有 ``done_at``，
        仍由 ``_running`` 登記，在 worker 真正返回前持續計入。須持有 ``self._lock`` 呼叫。

        以掃描 _commands 計數而非維護獨立計數器：admission control 把 pending 上限封頂後 _commands
        大小有界（≤ CMD_HISTORY_MAX + pending 上限），掃描成本可控；且避免計數器在多處終結轉移
        （worker 成功/例外、cancel、unregister 殘留）漏增漏減而 drift。
        """
        pending = sum(
            1 for rec in self._commands.values()
            if rec.get("session_id") == session_id and not rec.get("done_at")
        )
        running_cmd_id = self._running.get(session_id)
        running_rec = self._commands.get(running_cmd_id or "")
        if (
            running_rec is not None
            and running_rec.get("session_id") == session_id
            and running_rec.get("status") == "canceled"
        ):
            pending += 1
        return pending

    def _evict_commands_locked(self) -> None:
        """淘汰最舊的「已完成（有 done_at）」命令記錄，使數量回到上限（#81）。

        worker 尚未返回的命令永不淘汰，即使 cancel 已設定 ``done_at``；全部保留中的命令
        使歷史暫時超量，worker 收尾後再收斂。
        須在持有 ``self._lock`` 下呼叫。
        """
        if len(self._commands) <= CMD_HISTORY_MAX:
            return
        running_cmd_ids = set(self._running.values())
        done = [
            (cid, rec)
            for cid, rec in self._commands.items()
            if rec.get("done_at") and cid not in running_cmd_ids
        ]
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
            was_accepted = rec["status"] == "accepted"
            rec["status"] = "canceled"
            rec["done_at"] = now_iso()
            if was_accepted:
                self._finish_admission_locked(cmd_id)
            self._evict_commands_locked()  # 命令終結即收斂 history，不必等下一次 submit（Copilot 審查）
            callback = self._on_cancel or self._cancel_callbacks.get(cmd_id)
        if callback is not None:
            callback(cmd_id)
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
                    self._finish_admission_locked(item.cmd_id)
                    continue
                if rec.get("status") == "canceled" or rec.get("done_at"):
                    # 已被 cancel 或已被 flush 終結（#128）的記錄：跳過，不得重設 running
                    # 或執行 send_cb（防 flush 與取件之間的 race 把終端態覆寫回進行中）。
                    self._finish_admission_locked(item.cmd_id)
                    continue
                rec["status"] = "running"
                rec["started_at"] = now_iso()
                self._running[session_id] = item.cmd_id

            try:
                args = (session_id, item.command, item.source, item.cmd_id, item.timeout_s, item.mode, item.expected_duration_s)
                # 未指定時保留既有 callback 七參數契約；選用欄位僅在需要時傳遞。
                options: dict[str, Any] = {}
                if item.completion_regex is not None:
                    options["completion_regex"] = item.completion_regex
                if item.trusted_human:
                    options["trusted_human"] = True
                previous_arbiter = getattr(self._worker_context, "arbiter", None)
                previous_cmd_id = getattr(self._worker_context, "cmd_id", None)
                self._worker_context.arbiter = self
                self._worker_context.cmd_id = item.cmd_id
                try:
                    result = self._send_cb(*args, **options)
                finally:
                    if previous_arbiter is None:
                        try:
                            del self._worker_context.arbiter
                        except AttributeError:
                            pass
                    else:
                        self._worker_context.arbiter = previous_arbiter
                    if previous_cmd_id is None:
                        try:
                            del self._worker_context.cmd_id
                        except AttributeError:
                            pass
                    else:
                        self._worker_context.cmd_id = previous_cmd_id
            except Exception:
                with self._lock:
                    rec = self._commands.get(item.cmd_id)
                    if rec:
                        rec["status"] = "error"
                        rec["error_code"] = "SEND_FAILED"
                        rec["done_at"] = now_iso()
                    if self._running.get(session_id) == item.cmd_id:
                        self._running.pop(session_id, None)
                    self._evict_commands_locked()  # worker 收尾後才能淘汰 canceled-running 記錄
                    self._finish_admission_locked(item.cmd_id)
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
                if self._running.get(session_id) == item.cmd_id:
                    self._running.pop(session_id, None)
                self._evict_commands_locked()  # worker 收尾後才收斂 history
                self._finish_admission_locked(item.cmd_id)

    def _finish_admission_locked(self, cmd_id: str) -> None:
        ticket = self._admission_tickets.pop(cmd_id, None)
        if ticket is not None and self._admission is not None:
            self._admission.done(ticket)

    def snapshot(self) -> list[dict[str, Any]]:
        with self._lock:
            return [dict(v) for _, v in sorted(self._commands.items(), key=lambda kv: kv[1].get("created_at", ""))]
