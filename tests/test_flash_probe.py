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
