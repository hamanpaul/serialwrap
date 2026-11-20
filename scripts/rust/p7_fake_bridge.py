"""P7 Task 1（08-slice23-login-events.md）：Python 版腳本化 bridge，與 Rust 測試支援 `LoginRig` 共用同一 DSL。

DSL（JSON 物件）：
  {"initial_rx": str, "reactions": [{"on": "eq"|"prefix"|"secret"|"any", "match": str, "rx": str, "after_s"?: float}],
   "nonces": [str], "rx_bytes_10s": int, "idle_s": float, "env": {str: str}}

語意（Rust `LoginRig` 必須逐條相同）：
- 虛擬時鐘從 0.0 起；`initial_rx` 於 t=0 出現（非空時視為一次 RX）；`idle_s` 為開始前已靜默的秒數
  （`rx_idle_s = 虛擬現在 − 最後一次 RX 出現時刻`；開始時最後 RX 時刻＝`-idle_s`，有 `initial_rx` 時為 0）。
- `wait_for_regex(pattern, timeout)`：先 `re.compile`（無效 regex 照 Python 立即拋 `re.error`）；對目前整個 RX 文字
  `re.search`；未命中時，若有排程中的 RX 會在 `now+timeout` 之內出現，時鐘推進到該時刻、附加後再比對；
  都沒有則時鐘 `+= timeout` 並回 False。
- `send_command(cmd, source)`：記錄 `(source, (cmd+"\\n").encode(), log=True)`（已以 `\\n` 結尾者不再補），並依**第一個**
  符合的 reaction（`eq`：完全相等；`prefix`：開頭相符；`any`：任何命令）附加 `rx`（`{nonce}` 代換為最近一次 nonce）；
  reaction 不消耗，可重複觸發。`after_s` 存在時於 `now+after_s` 才出現。
- `send_secret(s)`：記錄 `("system:secret", payload, log=False)`，觸發 `on="secret"` 的第一個 reaction。
- `clear_rx_buffer()` 清空文字（不影響最後 RX 時刻）；`rx_tail()` 回最後 4096 字元；`rx_text_from(0)` 回目前全文。
- `rx_stats()` 回 `{"rx_bytes_last_10s": rx_bytes_10s, "rx_rate_bps": rx_bytes_10s // 10}`。
- `time.monotonic()`／`time.sleep(d)` 由 `VirtualClock` 取代（sleep 會遞送期間到期的 RX）；`uuid.uuid4().hex[:8]` 依序取 `nonces`。
"""
from __future__ import annotations

import re
from typing import Any


class VirtualClock:
    def __init__(self) -> None:
        self.now = 0.0
        self.bridge: ScriptedBridge | None = None

    def monotonic(self) -> float:
        return self.now

    def advance_to(self, t: float) -> None:
        if t > self.now:
            self.now = t
        if self.bridge is not None:
            self.bridge._deliver_due()

    def sleep(self, d: float) -> None:
        self.advance_to(self.now + max(0.0, d))


class _FakeUuid:
    def __init__(self, nonce: str) -> None:
        self.hex = (nonce + "0" * 32)[:32]


class ScriptedBridge:
    def __init__(self, dsl: dict[str, Any], clock: VirtualClock) -> None:
        self._clock = clock
        clock.bridge = self
        self._reactions = list(dsl.get("reactions") or [])
        self._nonces = list(dsl.get("nonces") or [])
        self._nonce_i = 0
        self._last_nonce = ""
        self._rx_bytes_10s = int(dsl.get("rx_bytes_10s") or 0)
        self._text = ""
        self._last_rx = -float(dsl.get("idle_s") or 0.0)
        self._pending: list[tuple[float, int, str]] = []
        self._seq = 0
        self.sent: list[tuple[str, bytes, bool]] = []
        initial = dsl.get("initial_rx") or ""
        if initial:
            self._append(initial)

    # ── 時間與 RX ─────────────────────────────────────────────────────────
    def _append(self, text: str) -> None:
        self._text += text
        self._last_rx = self._clock.now

    def _schedule(self, at: float, text: str) -> None:
        self._seq += 1
        self._pending.append((at, self._seq, text))
        self._pending.sort()

    def _deliver_due(self) -> None:
        while self._pending and self._pending[0][0] <= self._clock.now:
            _at, _s, text = self._pending.pop(0)
            self._append(text)

    def next_nonce_uuid(self) -> _FakeUuid:
        nonce = self._nonces[self._nonce_i] if self._nonce_i < len(self._nonces) else f"{self._nonce_i:08x}"
        self._nonce_i += 1
        self._last_nonce = nonce[:8]
        return _FakeUuid(nonce)

    def _react(self, kind: str, cmd: str) -> None:
        for r in self._reactions:
            on = r.get("on")
            match = r.get("match", "")
            if kind == "secret":
                hit = on == "secret"
            else:
                hit = (on == "eq" and cmd == match) or (on == "prefix" and cmd.startswith(match)) or on == "any"
            if not hit:
                continue
            rx = str(r.get("rx", "")).replace("{nonce}", self._last_nonce)
            after = r.get("after_s")
            if after:
                self._schedule(self._clock.now + float(after), rx)
            else:
                self._append(rx)
            return

    # ── UARTBridge duck type（login_fsm 使用的面）────────────────────────
    def send_command(self, cmd: str, *, source: str, cmd_id: str | None = None, permit: Any = None) -> None:
        payload = cmd.encode("utf-8", errors="replace")
        if not payload.endswith(b"\n"):
            payload += b"\n"
        self.sent.append((source, payload, True))
        self._react("cmd", cmd)

    def send_secret(self, secret: str, *, permit: Any = None) -> None:
        payload = secret.encode("utf-8", errors="replace")
        if not payload.endswith(b"\n"):
            payload += b"\n"
        self.sent.append(("system:secret", payload, False))
        self._react("secret", secret)

    def clear_rx_buffer(self) -> None:
        self._text = ""

    def wait_for_regex(self, pattern: str, timeout_s: float) -> bool:
        regex = re.compile(pattern)
        deadline = self._clock.now + timeout_s
        while True:
            if regex.search(self._text):
                return True
            if self._pending and self._pending[0][0] <= deadline:
                self._clock.advance_to(self._pending[0][0])
                continue
            self._clock.advance_to(deadline)
            return False

    def rx_tail(self, max_chars: int = 4096) -> str:
        return self._text[-max_chars:]

    def rx_text_from(self, from_offset: int) -> str:
        return self._text

    def rx_idle_s(self) -> float:
        return max(0.0, self._clock.now - self._last_rx)

    def rx_stats(self) -> dict[str, int]:
        return {"rx_bytes_last_10s": self._rx_bytes_10s, "rx_rate_bps": self._rx_bytes_10s // 10}
