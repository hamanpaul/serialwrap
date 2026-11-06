"""DUT 情境腳本觸發計畫（sans-IO 模型資料型別）。

定義 ScenarioPlan 與 Trigger，用於在接收特定字串時注入故障、延遲或狀態轉移。
"""
from __future__ import annotations

import base64
from dataclasses import dataclass
from typing import Any

VALID_TRIGGER_KINDS = {
    "delay_reply",
    "half_prompt",
    "silent",
    "stop_stdin",
    "logical_reboot",
    "flood",
}


@dataclass(frozen=True)
class Trigger:
    """單一命令比對觸發條件。"""

    id: str
    line_contains: str
    action: dict[str, Any]
    once: bool = True


@dataclass(frozen=True)
class ScenarioPlan:
    """DUT 行為情境計畫。

    Attributes:
        seed: 隨機種子，預留供 §15.7.1 隨機故障注入情境使用。
        triggers: 比對觸發條件列表。
    """

    seed: int
    triggers: tuple[Trigger, ...]

    @classmethod
    def from_json(cls, data: dict[str, Any]) -> ScenarioPlan:
        """自 JSON 字典載入計畫，強制驗證 schema_version 為 dut-plan.v1 及各 action 欄位。"""
        if data.get("schema_version") != "dut-plan.v1":
            raise ValueError(f"Unsupported schema_version: {data.get('schema_version')}")

        seen_ids: set[str] = set()
        triggers_list: list[Trigger] = []

        for t in data.get("triggers", []):
            tid = str(t.get("id", ""))
            if not tid:
                raise ValueError("Trigger id 不得為空")
            if tid in seen_ids:
                raise ValueError(f"重複的 trigger id: {tid}")
            seen_ids.add(tid)

            line_contains = str(t.get("line_contains", ""))
            action = dict(t.get("action", {}))
            kind = action.get("kind")
            if kind not in VALID_TRIGGER_KINDS:
                raise ValueError(f"不支援的 trigger action kind: {kind}")

            if kind == "delay_reply":
                delay_ms = int(action.get("delay_ms", 0))
                if delay_ms < 0:
                    raise ValueError("delay_ms 必須 >= 0")
            elif kind == "half_prompt":
                split_at = int(action.get("split_at", 0))
                gap_ms = int(action.get("gap_ms", 0))
                if split_at < 0 or gap_ms < 0:
                    raise ValueError("split_at 與 gap_ms 必須 >= 0")
                inject_b64 = str(action.get("inject_b64", ""))
                try:
                    base64.b64decode(inject_b64, validate=True)
                except Exception as exc:
                    raise ValueError(f"half_prompt inject_b64 必須為合法 Base64: {exc}") from exc
            elif kind == "stop_stdin":
                resume_ms = action.get("resume_after_ms")
                if resume_ms is not None and int(resume_ms) < 0:
                    raise ValueError("resume_after_ms 必須 >= 0")
            elif kind == "logical_reboot":
                banner_delay = int(action.get("banner_delay_ms", 0))
                if banner_delay < 0:
                    raise ValueError("banner_delay_ms 必須 >= 0")
            elif kind == "flood":
                b_count = int(action.get("bytes", 0))
                if b_count <= 0:
                    raise ValueError("flood bytes 必須 > 0")
                line = str(action.get("line", ""))
                if not line:
                    raise ValueError("flood line 不得為空")

            triggers_list.append(
                Trigger(
                    id=tid,
                    line_contains=line_contains,
                    action=action,
                    once=bool(t.get("once", True)),
                )
            )

        return cls(seed=int(data.get("seed", 0)), triggers=tuple(triggers_list))

    def to_json(self) -> dict[str, Any]:
        """序列化為 JSON 字典。"""
        return {
            "schema_version": "dut-plan.v1",
            "seed": self.seed,
            "triggers": [
                {
                    "id": t.id,
                    "line_contains": t.line_contains,
                    "action": t.action,
                    "once": t.once,
                }
                for t in self.triggers
            ],
        }
