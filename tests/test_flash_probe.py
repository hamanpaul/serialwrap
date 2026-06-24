"""tests/test_flash_probe.py — sync-probe 偵測器單元測試。"""
from sw_core.mcu_patterns import McuPatternRegistry
from sw_core.flash_endpoint import detect_mcu_line


class FakeTransport:
    """以 by_id -> 回應 bytes 模擬各候選對 probe 的反應。"""
    def __init__(self, replies):
        self._replies = replies  # {by_id: bytes|None}
        self.written = {}

    def probe(self, by_id, probe_bytes, expect, timeout_ms):
        self.written.setdefault(by_id, []).append(probe_bytes)
        reply = self._replies.get(by_id)
        return reply is not None and reply.startswith(expect)


def _cand(com, by_id, command_capable):
    return {"com": com, "by_id": by_id, "real_path": "/dev/x",
            "command_capable": command_capable}


def test_excludes_command_capable_console():
    reg = McuPatternRegistry.default()
    cands = [_cand("COM1", "console-id", True), _cand("COM0", "mcu-id", False)]
    t = FakeTransport({"mcu-id": b"\x00\xcc"})
    res = detect_mcu_line(cands, reg, t)
    assert res.status == "matched"
    assert res.by_id == "mcu-id"
    assert "console-id" not in t.written  # console 從未被 probe


def test_ambiguous_when_multiple_ack():
    reg = McuPatternRegistry.default()
    cands = [_cand("COM0", "a", False), _cand("COM2", "b", False)]
    t = FakeTransport({"a": b"\x00\xcc", "b": b"\x00\xcc"})
    res = detect_mcu_line(cands, reg, t)
    assert res.status == "ambiguous"
    assert set(res.hits) == {"a", "b"}


def test_no_match_returns_none_status():
    reg = McuPatternRegistry.default()
    cands = [_cand("COM0", "a", False)]
    t = FakeTransport({"a": None})
    res = detect_mcu_line(cands, reg, t)
    assert res.status == "none"


# ── RACE-2（#83）：_BridgeProbe.probe 期間持有 bridge 寫入仲裁（flash_mode gate）─────────────
import threading  # noqa: E402

from sw_core.service import _BridgeProbe  # noqa: E402


class _GateBridge:
    """記錄 flash_tx 當下的 flash_mode 狀態與 set_flash_mode 事件。"""
    def __init__(self):
        self.flash_mode = False
        self.events = []
        self.raise_on_tx = False

    def set_flash_mode(self, enabled):
        self.flash_mode = enabled
        self.events.append(("gate", enabled))

    def flash_tx(self, payload):
        self.events.append(("flash_tx", self.flash_mode))
        if self.raise_on_tx:
            raise OSError("tx boom")


def _probe_with(bridge):
    sess = type("S", (), {"bridge": bridge})()
    sessions = type("Ss", (), {"get_session": lambda self, com: sess if com == "COM0" else None})()
    svc = type("Svc", (), {"_flash_lock": threading.Lock(),
                           "_flash_rx_buffers": {}, "_sessions": sessions})()
    return _BridgeProbe(svc, {"mcu-id": "COM0"}, sync_bytes=b"UU")


def test_bridge_probe_holds_flash_gate_during_write():
    """probe 寫 sync bytes 當下 flash_mode 須為 True（獨佔該 bridge，drop human/注入），結束還原 False。"""
    b = _GateBridge()
    probe = _probe_with(b)
    ok = probe.probe("mcu-id", b"PP", expect=b"ACK", timeout_ms=20)  # ACK 永不到 → timeout False
    assert ok is False
    assert ("flash_tx", True) in b.events       # 寫入當下 gate 為 True
    assert b.flash_mode is False                 # probe 結束已還原（候選回非 flash）
    assert b.events[-1] == ("gate", False)


def test_bridge_probe_restores_gate_on_tx_error():
    """flash_tx 例外時 gate 仍須被還原（finally），不致把候選 bridge 永久卡在 flash_mode。"""
    b = _GateBridge()
    b.raise_on_tx = True
    probe = _probe_with(b)
    assert probe.probe("mcu-id", b"PP", expect=b"ACK", timeout_ms=20) is False
    assert b.flash_mode is False
    assert b.events[-1] == ("gate", False)
