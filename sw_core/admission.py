from __future__ import annotations

import dataclasses
import threading
import uuid
from typing import Any, Literal


@dataclasses.dataclass(frozen=True)
class Ticket:
    """一項尚未完成的有副作用操作。"""

    kind: str
    com: str | None
    seq: int


class AdmissionGate:
    """讓新操作 admission 與 daemon 交接凍結共用同一把葉鎖。"""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._tickets: dict[int, Ticket] = {}
        self._flags: dict[str, set[str]] = {}
        self._next_seq = 0
        self._frozen = False
        self._token: str | None = None

    @property
    def frozen(self) -> bool:
        with self._lock:
            return self._frozen

    def admit(self, kind: str, com: str | None) -> Ticket | None:
        with self._lock:
            if self._frozen:
                return None
            self._next_seq += 1
            ticket = Ticket(kind=kind, com=com, seq=self._next_seq)
            self._tickets[ticket.seq] = ticket
            return ticket

    def done(self, ticket: Ticket) -> None:
        with self._lock:
            self._tickets.pop(ticket.seq, None)

    def set_flag(self, com: str, flag: Literal["flashing", "recovering", "lease"], on: bool) -> None:
        with self._lock:
            flags = self._flags.setdefault(com, set())
            if on:
                flags.add(flag)
            else:
                flags.discard(flag)
            if not flags:
                self._flags.pop(com, None)

    def freeze(self) -> None:
        """停止接受新操作；daemon 一般關機不需等待 busy 判定。"""
        with self._lock:
            self._frozen = True
            if self._token is None:
                self._token = uuid.uuid4().hex

    def freeze_if_idle(self, reason: str) -> tuple[str | None, dict[str, Any]]:
        del reason  # 保留呼叫契約；凍結狀態不因原因改變。
        with self._lock:
            if self._frozen:
                return self._token, {}
            by_com: dict[str, dict[str, set[str]]] = {}
            for ticket in self._tickets.values():
                com = ticket.com or "*"
                by_com.setdefault(com, {"ops": set(), "flags": set()})["ops"].add(ticket.kind)
            for com, flags in self._flags.items():
                row = by_com.setdefault(com, {"ops": set(), "flags": set()})
                row["flags"].update(flags)
            flashing = any("flashing" in row["flags"] for row in by_com.values()) or any(
                ticket.kind.startswith("flash.") for ticket in self._tickets.values()
            )
            if by_com:
                return None, {
                    "by_com": {
                        com: {
                            "ops": sorted(row["ops"]),
                            "flags": sorted(row["flags"]),
                        }
                        for com, row in sorted(by_com.items())
                    },
                    "flashing": flashing,
                }
            self._frozen = True
            self._token = uuid.uuid4().hex
            return self._token, {}
