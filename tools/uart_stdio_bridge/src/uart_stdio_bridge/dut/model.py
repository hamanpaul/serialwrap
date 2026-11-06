"""DUT 行為模型（sans-IO 純邏輯狀態機）。

重現 FakeTarget 的命令回應、登入流程、背景雜訊、動態提示符、串行延遲與 pause 語意，
所有時間由呼叫端注入，不執行任何底層 I/O 或執行緒等待。
"""
from __future__ import annotations

import base64
import heapq
from collections.abc import Mapping
from dataclasses import dataclass, field
from typing import Any

from uart_stdio_bridge.dut.plan import ScenarioPlan, Trigger


@dataclass(frozen=True)
class CommandOverride:
    """單一指令的自訂回應設定。"""

    response: str = ""
    delay_ms: int = 0
    interactive: bool = False


@dataclass(frozen=True)
class LoginStep:
    """登入序列中的一個步驟。"""

    expect: str
    respond: str


@dataclass(frozen=True)
class TargetConfig:
    """DUT 行為組態。"""

    platform: str = "prpl"
    boot_banner: str = "boot done\r\nroot@prplOS:/# "
    noise_enabled: bool = True
    noise_interval_ms: int = 50
    noise_pattern: str = "KDBG:tick:{tick}\r\n"
    default_response: str = "EXEC:{cmd}\r\nRESULT:{cmd}:OK\r\nroot@prplOS:/# "
    overrides: Mapping[str, CommandOverride] = field(default_factory=dict)
    login_steps: tuple[LoginStep, ...] = ()

    @classmethod
    def from_yaml(cls, data: dict[str, Any] | None) -> TargetConfig:
        """自 YAML 字典載入組態（與 legacy FakeTarget.from_yaml 同語意）。"""
        if data is None:
            return cls()
        noise = data.get("noise") or {}
        raw_overrides = (data.get("commands") or {}).get("overrides") or {}
        overrides: dict[str, CommandOverride] = {}
        for cmd_str, ov in raw_overrides.items():
            if isinstance(ov, dict):
                overrides[cmd_str] = CommandOverride(
                    response=ov.get("response", ""),
                    delay_ms=ov.get("delay_ms", 0),
                    interactive=ov.get("interactive", False),
                )
        raw_login = data.get("login_steps") or []
        login_steps = [
            LoginStep(expect=s["expect"], respond=s["respond"])
            for s in raw_login
            if isinstance(s, dict)
        ]
        return cls(
            platform=data.get("platform", "prpl"),
            boot_banner=data.get("boot_banner", "boot done\r\nroot@prplOS:/# "),
            noise_enabled=noise.get("enabled", True),
            noise_interval_ms=noise.get("interval_ms", 50),
            noise_pattern=noise.get("pattern", "KDBG:tick:{tick}\r\n"),
            default_response=(data.get("commands") or {}).get(
                "default", "EXEC:{cmd}\r\nRESULT:{cmd}:OK\r\nroot@prplOS:/# "
            ),
            overrides=overrides,
            login_steps=tuple(login_steps),
        )

    def to_json(self) -> dict[str, Any]:
        """序列化為 JSON 字典。"""
        return {
            "platform": self.platform,
            "boot_banner": self.boot_banner,
            "noise_enabled": self.noise_enabled,
            "noise_interval_ms": self.noise_interval_ms,
            "noise_pattern": self.noise_pattern,
            "default_response": self.default_response,
            "overrides": {
                k: {
                    "response": v.response,
                    "delay_ms": v.delay_ms,
                    "interactive": v.interactive,
                }
                for k, v in self.overrides.items()
            },
            "login_steps": [
                {"expect": s.expect, "respond": s.respond}
                for s in self.login_steps
            ],
        }

    @classmethod
    def from_json(cls, data: dict[str, Any]) -> TargetConfig:
        """自 JSON 字典反序列化。"""
        overrides = {}
        for k, v in data.get("overrides", {}).items():
            delay_ms = int(v.get("delay_ms", 0))
            if delay_ms < 0:
                raise ValueError(f"override {k} delay_ms 必須 >= 0")
            overrides[k] = CommandOverride(
                response=str(v.get("response", "")),
                delay_ms=delay_ms,
                interactive=bool(v.get("interactive", False)),
            )
        login_steps = tuple(
            LoginStep(expect=str(s["expect"]), respond=str(s["respond"]))
            for s in data.get("login_steps", [])
        )
        noise_interval_ms = int(data.get("noise_interval_ms", 50))
        if noise_interval_ms < 1:
            raise ValueError("noise_interval_ms 必須 >= 1")
        return cls(
            platform=str(data.get("platform", "prpl")),
            boot_banner=str(data.get("boot_banner", "boot done\r\nroot@prplOS:/# ")),
            noise_enabled=bool(data.get("noise_enabled", True)),
            noise_interval_ms=noise_interval_ms,
            noise_pattern=str(data.get("noise_pattern", "KDBG:tick:{tick}\r\n")),
            default_response=str(
                data.get(
                    "default_response", "EXEC:{cmd}\r\nRESULT:{cmd}:OK\r\nroot@prplOS:/# "
                )
            ),
            overrides=overrides,
            login_steps=login_steps,
        )


@dataclass(frozen=True)
class Emit:
    """DUT 輸出事件。"""

    at_ms: int
    data: bytes
    kind: str  # "banner" | "response" | "prompt" | "login" | "noise" | "emit" | "reboot" | "flood"


class DutModel:
    """DUT 行為模型 sans-IO 狀態機。"""

    def __init__(self, config: TargetConfig, plan: ScenarioPlan | None = None) -> None:
        self.config = config
        self.plan = plan
        self.generation: int = 1
        self.stdin_paused: bool = False

        self._triggers_enabled: dict[str, bool] = {}
        if plan is not None:
            for tr in plan.triggers:
                self._triggers_enabled[tr.id] = True

        self._heap: list[tuple[int, int, Emit]] = []
        self._seq: int = 0
        self._busy_until_ms: int = 0
        self._pause_until_ms: int = 0

        self._tick: int = 0
        self._next_noise_ms: int | None = None

        self._current_prompt: str | None = None
        self._login_idx: int = 0
        self._in_login: bool = len(config.login_steps) > 0

        self._buf: bytes = b""
        self._stdin_resume_ms: int | None = None

        # flood 狀態
        self._flood_remaining: int = 0
        self._flood_pattern: str = ""
        self._flood_n: int = 0
        self._flood_buf: bytearray = bytearray()
        self._next_flood_ms: int | None = None

    def _push_emit(self, emit: Emit) -> None:
        self._seq += 1
        heapq.heappush(self._heap, (emit.at_ms, self._seq, emit))

    def _pop_due(self, now_ms: int) -> list[Emit]:
        emits: list[Emit] = []
        while self._heap and self._heap[0][0] <= now_ms:
            _at_ms, _seq, emit = heapq.heappop(self._heap)
            emits.append(emit)
        return emits

    def _flush_buf(self, now_ms: int) -> None:
        """排空緩衝區中尚未執行的完整行命令（於 stdin 恢復時調用）。"""
        while not self.stdin_paused and b"\n" in self._buf:
            line_bytes, remaining = self._buf.split(b"\n", 1)
            cmd = line_bytes.replace(b"\r", b"").decode("utf-8", errors="replace").strip()
            line_emits = self._handle_line(cmd, now_ms)
            for e in line_emits:
                self._push_emit(e)
            self._buf = remaining

    def boot(self, now_ms: int) -> list[Emit]:
        """啟動開機流程，輸出 boot banner 與初始雜訊。"""
        emits: list[Emit] = []
        banner_data = self.config.boot_banner.encode("utf-8")
        emits.append(Emit(at_ms=now_ms, data=banner_data, kind="banner"))
        self._busy_until_ms = max(self._busy_until_ms, now_ms)

        if self.config.noise_enabled:
            self._tick = 1
            noise_msg = self.config.noise_pattern.replace("{tick}", str(self._tick))
            emits.append(Emit(at_ms=now_ms, data=noise_msg.encode("utf-8"), kind="noise"))
            interval = max(1, self.config.noise_interval_ms)
            self._next_noise_ms = now_ms + interval
        else:
            self._next_noise_ms = None
        return emits

    def feed(self, data: bytes, now_ms: int) -> list[Emit]:
        """餵入輸入位元組；回傳此刻（<= now_ms）即可輸出者，延後者存入 heap。"""
        self._buf += data
        if self.stdin_paused:
            return []
        while b"\n" in self._buf:
            line_bytes, remaining = self._buf.split(b"\n", 1)
            cmd = line_bytes.replace(b"\r", b"").decode("utf-8", errors="replace").strip()
            line_emits = self._handle_line(cmd, now_ms)
            for e in line_emits:
                self._push_emit(e)
            self._buf = remaining
            if self.stdin_paused:
                break
        return self._pop_due(now_ms)

    def _prompt_bytes(self) -> bytes:
        if self._current_prompt is not None:
            return self._current_prompt.encode("utf-8")
        if self.config.login_steps:
            last = self.config.login_steps[-1].respond
            lines = last.split("\r\n")
            prompt = lines[-1] if lines else "# "
            return prompt.encode("utf-8")
        lines = self.config.boot_banner.split("\r\n")
        prompt = lines[-1] if lines else "# "
        return prompt.encode("utf-8")

    def _handle_line(self, cmd: str, now_ms: int) -> list[Emit]:
        # 登入階段比對
        if self._in_login and self._login_idx < len(self.config.login_steps):
            step = self.config.login_steps[self._login_idx]
            t = max(now_ms, self._busy_until_ms)
            self._busy_until_ms = t
            if step.expect in cmd:
                self._login_idx += 1
                if self._login_idx >= len(self.config.login_steps):
                    self._in_login = False
                return [Emit(at_ms=t, data=step.respond.encode("utf-8"), kind="login")]
            else:
                if self._login_idx == 0:
                    banner = self.config.boot_banner
                    last = banner.split("\r\n")[-1] if banner else "Login: "
                    retry_data = f"\r\n{last}".encode("utf-8")
                else:
                    prev = self.config.login_steps[self._login_idx - 1].respond
                    last = prev.split("\r\n")[-1] if prev else ""
                    retry_data = f"\r\n{last}".encode("utf-8") if last else b""
                if retry_data:
                    return [Emit(at_ms=t, data=retry_data, kind="login")]
                return []

        # 檢查情境觸發條件
        trigger_to_apply: Trigger | None = None
        if self.plan is not None:
            for tr in self.plan.triggers:
                if self._triggers_enabled.get(tr.id, True) and tr.line_contains in cmd:
                    trigger_to_apply = tr
                    if tr.once:
                        self._triggers_enabled[tr.id] = False
                    break

        if trigger_to_apply is not None:
            action = trigger_to_apply.action
            action_kind = action.get("kind")
            if action_kind == "silent":
                return []
            if action_kind == "stop_stdin":
                self.stdin_paused = True
                resume_after_ms = action.get("resume_after_ms")
                if resume_after_ms is not None:
                    self._stdin_resume_ms = now_ms + int(resume_after_ms)
            elif action_kind == "logical_reboot":
                b_delay = int(action.get("banner_delay_ms", 0))
                p_text = str(action.get("pre_text", ""))
                return self.logical_reboot(now_ms, banner_delay_ms=b_delay, pre_text=p_text)
            elif action_kind == "flood":
                total_bytes = int(action.get("bytes", 0))
                pattern = str(action.get("line", ""))
                self._setup_flood(total_bytes, pattern, now_ms)
                return []

        # 空行送 prompt
        if not cmd:
            t = max(now_ms, self._busy_until_ms)
            self._busy_until_ms = t
            return [Emit(at_ms=t, data=self._prompt_bytes(), kind="prompt")]

        # 非空命令處理
        start = max(now_ms, self._busy_until_ms)
        if self._pause_until_ms > start:
            pause_boundary = min(self._pause_until_ms, start + 30000)
            cmd_start = max(start, pause_boundary)
        else:
            cmd_start = start

        cmd_delay = 0
        override = self.config.overrides.get(cmd)
        if override is not None:
            cmd_delay += override.delay_ms
            if override.interactive:
                self._busy_until_ms = cmd_start + cmd_delay
                return []

        if trigger_to_apply is not None and trigger_to_apply.action.get("kind") == "delay_reply":
            cmd_delay += int(trigger_to_apply.action.get("delay_ms", 0))

        at_ms = cmd_start + cmd_delay
        self._busy_until_ms = at_ms

        if override is not None:
            resp = override.response
        else:
            resp = self.config.default_response.replace("{cmd}", cmd)

        lines = resp.split("\r\n")
        last_line = lines[-1] if lines else ""
        if last_line.strip():
            self._current_prompt = last_line

        if trigger_to_apply is not None and trigger_to_apply.action.get("kind") == "half_prompt":
            split_at = int(trigger_to_apply.action.get("split_at", 0))
            gap_ms = int(trigger_to_apply.action.get("gap_ms", 0))
            inject_b64 = trigger_to_apply.action.get("inject_b64", "")
            inject = base64.b64decode(inject_b64)

            prompt_str = last_line
            body_str = resp[:-len(prompt_str)] if prompt_str else resp
            prompt_bytes = prompt_str.encode("utf-8")
            body_bytes = body_str.encode("utf-8")

            part1 = body_bytes + inject + prompt_bytes[:split_at]
            part2 = prompt_bytes[split_at:]

            emit1 = Emit(at_ms=at_ms, data=part1, kind="response")
            late_ms = at_ms + gap_ms
            emit2 = Emit(at_ms=late_ms, data=part2, kind="response")
            self._busy_until_ms = late_ms
            return [emit1, emit2]

        return [Emit(at_ms=at_ms, data=resp.encode("utf-8"), kind="response")]

    def _setup_flood(self, total_bytes: int, line_pattern: str, now_ms: int) -> None:
        self._flood_remaining = total_bytes
        self._flood_pattern = line_pattern
        self._flood_n = 0
        self._flood_buf = bytearray()
        self._next_flood_ms = now_ms

    def _get_flood_chunk(self, max_bytes: int = 65536) -> bytes:
        needed = min(max_bytes, self._flood_remaining)
        if needed <= 0:
            return b""
        while len(self._flood_buf) < needed:
            line = self._flood_pattern.replace("{n}", str(self._flood_n)).encode("utf-8")
            if not line:
                break
            self._flood_n += 1
            self._flood_buf.extend(line)
        chunk = bytes(self._flood_buf[:needed])
        del self._flood_buf[:needed]
        self._flood_remaining -= len(chunk)
        return chunk

    def poll(self, now_ms: int) -> list[Emit]:
        """依虛擬時間 poll，交出到期延後輸出、雜訊及 flood 分段。"""
        if self._stdin_resume_ms is not None and now_ms >= self._stdin_resume_ms:
            self.stdin_paused = False
            self._stdin_resume_ms = None
            self._flush_buf(now_ms)

        emits: list[Emit] = self._pop_due(now_ms)

        if self.config.noise_enabled and self._next_noise_ms is not None:
            interval = max(1, self.config.noise_interval_ms)
            while self._next_noise_ms is not None and self._next_noise_ms <= now_ms:
                if self._pause_until_ms > self._next_noise_ms:
                    self._next_noise_ms = self._pause_until_ms
                    if self._next_noise_ms > now_ms:
                        break
                self._tick += 1
                msg = self.config.noise_pattern.replace("{tick}", str(self._tick))
                emits.append(Emit(at_ms=self._next_noise_ms, data=msg.encode("utf-8"), kind="noise"))
                self._next_noise_ms += interval

        if (
            self._next_flood_ms is not None
            and now_ms >= self._next_flood_ms
            and self._flood_remaining > 0
        ):
            chunk = self._get_flood_chunk(65536)
            if chunk:
                emits.append(Emit(at_ms=now_ms, data=chunk, kind="flood"))
                if self._flood_remaining > 0:
                    self._next_flood_ms = now_ms
                else:
                    self._next_flood_ms = None

        return emits

    def next_deadline_ms(self) -> int | None:
        """計算下一個到期事件的虛擬時間戳（ms）；若無待辦事件則回傳 None。"""
        candidates: list[int] = []
        if self._heap:
            candidates.append(self._heap[0][0])
        if self.config.noise_enabled and self._next_noise_ms is not None:
            if self._pause_until_ms > self._next_noise_ms:
                candidates.append(self._pause_until_ms)
            else:
                candidates.append(self._next_noise_ms)
        if self._next_flood_ms is not None and self._flood_remaining > 0:
            candidates.append(self._next_flood_ms)
        if self._stdin_resume_ms is not None:
            candidates.append(self._stdin_resume_ms)
        return min(candidates) if candidates else None

    def pause_responding(self, now_ms: int, duration_ms: int) -> None:
        """暫停回應（模擬 target hang）。"""
        self._pause_until_ms = now_ms + duration_ms
        if self.config.noise_enabled and self._next_noise_ms is not None:
            if self._next_noise_ms < self._pause_until_ms:
                self._next_noise_ms = self._pause_until_ms

    def logical_reboot(
        self, now_ms: int, *, banner_delay_ms: int, pre_text: str = ""
    ) -> list[Emit]:
        """觸發邏輯重開機，遞增 generation 並在延遲後送出 boot banner。"""
        self._heap.clear()
        self._flood_remaining = 0
        self._flood_buf = bytearray()
        self._next_flood_ms = None
        self._pause_until_ms = 0
        self._buf = b""

        if pre_text:
            self._push_emit(Emit(at_ms=now_ms, data=pre_text.encode("utf-8"), kind="reboot"))
        self.generation += 1
        self._in_login = len(self.config.login_steps) > 0
        self._login_idx = 0
        self._current_prompt = None

        banner_at = now_ms + banner_delay_ms
        self._push_emit(
            Emit(
                at_ms=banner_at,
                data=self.config.boot_banner.encode("utf-8"),
                kind="banner",
            )
        )
        self._busy_until_ms = banner_at
        if self.config.noise_enabled:
            self._tick = 1
            interval = max(1, self.config.noise_interval_ms)
            self._next_noise_ms = banner_at + interval
        else:
            self._next_noise_ms = None

        return self._pop_due(now_ms)

    def emit_raw(self, data: bytes, now_ms: int) -> list[Emit]:
        """直接產生 raw 輸出事件。"""
        self._push_emit(Emit(at_ms=now_ms, data=data, kind="emit"))
        return self._pop_due(now_ms)

    def apply_control(
        self, msg: dict[str, Any], now_ms: int
    ) -> tuple[list[Emit], dict[str, Any]]:
        """處理控制通道指令並回傳 (emits, ack)。"""
        if not isinstance(msg, dict):
            return [], {"id": None, "ok": False, "error": "bad_request"}
        req_id = msg.get("id")
        action = msg.get("action")

        try:
            if action == "pause_responding":
                duration_ms = int(msg.get("duration_ms", 0))
                if duration_ms < 0:
                    raise ValueError("duration_ms 必須 >= 0")
                self.pause_responding(now_ms, duration_ms)
                ack = {"id": req_id, "ok": True, "generation": self.generation, "t_ms": now_ms}
                return [], ack

            if action == "emit":
                payload_b64 = msg.get("payload_b64")
                if payload_b64 is None:
                    return [], {"id": req_id, "ok": False, "error": "bad_payload"}
                try:
                    payload = base64.b64decode(str(payload_b64), validate=True)
                except Exception:
                    return [], {"id": req_id, "ok": False, "error": "bad_payload"}
                emits = self.emit_raw(payload, now_ms)
                ack = {"id": req_id, "ok": True, "generation": self.generation, "t_ms": now_ms}
                return emits, ack

            if action == "stdin_pause":
                self.stdin_paused = True
                ack = {"id": req_id, "ok": True, "generation": self.generation, "t_ms": now_ms}
                return [], ack

            if action == "stdin_resume":
                self.stdin_paused = False
                self._stdin_resume_ms = None
                self._flush_buf(now_ms)
                ack = {"id": req_id, "ok": True, "generation": self.generation, "t_ms": now_ms}
                return self._pop_due(now_ms), ack

            if action == "logical_reboot":
                banner_delay_ms = int(msg.get("banner_delay_ms", 0))
                if banner_delay_ms < 0:
                    raise ValueError("banner_delay_ms 必須 >= 0")
                pre_text = str(msg.get("pre_text", ""))
                emits = self.logical_reboot(
                    now_ms, banner_delay_ms=banner_delay_ms, pre_text=pre_text
                )
                ack = {"id": req_id, "ok": True, "generation": self.generation, "t_ms": now_ms}
                return emits, ack

            if action == "set_trigger":
                trigger_id = str(msg.get("trigger_id", ""))
                if trigger_id not in self._triggers_enabled:
                    return [], {"id": req_id, "ok": False, "error": "unknown_trigger"}
                enabled = bool(msg.get("enabled", True))
                self._triggers_enabled[trigger_id] = enabled
                ack = {"id": req_id, "ok": True, "generation": self.generation, "t_ms": now_ms}
                return [], ack

            ack = {"id": req_id, "ok": False, "error": "unknown_action"}
            return [], ack
        except (ValueError, TypeError, KeyError) as exc:
            return [], {"id": req_id, "ok": False, "error": "bad_request"}
