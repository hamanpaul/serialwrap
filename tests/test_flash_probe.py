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


# ── RACE-2（#83）：_BridgeProbe.probe 寫入仲裁 + 命中後 gate 持有到交接（Codex 必修）──────────
import threading  # noqa: E402

from sw_core.service import _BridgeProbe  # noqa: E402


class _GateBridge:
    """記錄 flash_tx 當下 flash_mode 與 set_flash_mode 事件；ack 設定時 flash_tx 模擬 MCU 命中回應。"""
    def __init__(self, svc, com):
        self._svc = svc
        self._com = com
        self.flash_mode = False
        self.events = []
        self.raise_on_tx = False
        self.ack = None  # bytes → flash_tx 時填入 svc rx buffer，模擬命中

    def set_flash_mode(self, enabled):
        self.flash_mode = enabled
        self.events.append(("gate", enabled))

    def flash_tx(self, payload):
        self.events.append(("flash_tx", self.flash_mode))
        if self.raise_on_tx:
            raise OSError("tx boom")
        if self.ack is not None:
            with self._svc._flash_lock:
                self._svc._flash_rx_buffers[self._com] = bytearray(self.ack)


def _make_env(coms):
    """coms: {com: by_id}。回傳 (svc, bridges{com->bridge}, probe)。"""
    svc = type("Svc", (), {})()
    svc._flash_lock = threading.Lock()
    svc._flash_rx_buffers = {}
    bridges = {com: _GateBridge(svc, com) for com in coms}

    class _Sess:
        def __init__(self, b):
            self.bridge = b

    class _Sessions:
        def get_session(self, com):
            return _Sess(bridges[com]) if com in bridges else None

    svc._sessions = _Sessions()
    by_id_to_com = {by_id: com for com, by_id in coms.items()}
    return svc, bridges, _BridgeProbe(svc, by_id_to_com, sync_bytes=b"UU")


def test_bridge_probe_holds_flash_gate_during_write():
    """probe 寫 sync 當下 flash_mode 須為 True（獨佔，drop human/注入）；未命中則結束還原 False。"""
    _svc, bridges, probe = _make_env({"COM0": "mcu-id"})
    b = bridges["COM0"]
    ok = probe.probe("mcu-id", b"PP", expect=b"ACK", timeout_ms=20)  # 無 ack → timeout False
    assert ok is False
    assert ("flash_tx", True) in b.events        # 寫入當下 gate 為 True
    assert b.flash_mode is False and b.events[-1] == ("gate", False)  # 未命中 → 解除


def test_bridge_probe_restores_gate_on_tx_error():
    """flash_tx 例外時 gate 仍須被還原（finally），不致把候選 bridge 永久卡在 flash_mode。"""
    _svc, bridges, probe = _make_env({"COM0": "mcu-id"})
    b = bridges["COM0"]
    b.raise_on_tx = True
    assert probe.probe("mcu-id", b"PP", expect=b"ACK", timeout_ms=20) is False
    assert b.flash_mode is False and b.events[-1] == ("gate", False)


def test_bridge_probe_holds_gate_on_match():
    """命中（收到 ACK）時 gate 須保持 True（held），不在 probe 結束時解除；release_held 才釋放。"""
    _svc, bridges, probe = _make_env({"COM0": "mcu-id"})
    b = bridges["COM0"]
    b.ack = b"ACK"
    assert probe.probe("mcu-id", b"PP", expect=b"ACK", timeout_ms=200) is True
    assert b.flash_mode is True and probe.held == {"COM0": b}   # 命中 → gate 保持（held）
    probe.release_held()
    assert b.flash_mode is False and probe.held == {}


def test_release_held_adopt_keeps_winner_gated():
    """release_held(adopt=com)：命中線交接 flash 生命週期 → gate 維持 True、移出 held。"""
    _svc, bridges, probe = _make_env({"COM0": "mcu-id"})
    b = bridges["COM0"]
    b.ack = b"ACK"
    assert probe.probe("mcu-id", b"PP", expect=b"ACK", timeout_ms=200) is True
    probe.release_held(adopt="COM0")
    assert b.flash_mode is True and probe.held == {}


def test_matched_bridge_stays_gated_while_later_candidate_probed():
    """Finding 2：第一候選命中（ACK）後仍須持 gate；當較晚候選 probe（timeout）時不被解 gate。"""
    _svc, bridges, probe = _make_env({"COMA": "id-a", "COMB": "id-b"})
    a, bb = bridges["COMA"], bridges["COMB"]
    a.ack = b"ACK"                                            # A 命中
    assert probe.probe("id-a", b"PP", expect=b"ACK", timeout_ms=200) is True
    assert a.flash_mode is True                               # A 命中後 held
    assert probe.probe("id-b", b"PP", expect=b"ACK", timeout_ms=20) is False  # detect 續探 B（timeout）
    assert bb.flash_mode is False                             # B 未命中 → 解除
    assert a.flash_mode is True and probe.held == {"COMA": a}  # 關鍵：A 仍 gated（不被續探解 gate）
