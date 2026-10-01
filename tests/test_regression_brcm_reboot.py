"""#224 實機 case 的純邏輯替身測試；不接觸 live daemon／UART。"""
from __future__ import annotations

import base64
import sys
from pathlib import Path
from types import SimpleNamespace

REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT / "regression"))

from serialwrap_regression.cases import f09_brcm_reboot_login as case  # noqa: E402


def _record(seq: int, direction: str, payload: bytes) -> dict:
    return {"seq": seq, "dir": direction,
            "payload_b64": base64.b64encode(payload).decode("ascii")}


class _Sw:
    def __init__(self, *, spaced: bool = True) -> None:
        self.spaced = spaced
        self.rebooted = False
        self.boot_reads = 0

    def session(self, com: str) -> dict:
        assert com == "COM1"
        return {"state": "READY", "platform": "bcm", "ready_reconfirm_pending": False,
                "ready_reconfirm_failed": False, "boot_quiet_remaining_s": None}

    def run(self, *args: str) -> dict:
        if args[:2] == ("cmd", "submit"):
            if "sync; reboot" in args:
                self.rebooted = True
                return {"cmd_id": "reboot-1"}
            return {"cmd_id": "boot-id-1"}
        if args[:2] == ("cmd", "status"):
            self.boot_reads += 1
            boot_id = ("34193e57-d2f4-4a74-839a-00666b7d27be" if self.rebooted
                       else "9e046aec-575d-46c5-b80a-be109a83ce81")
            return {"command": {"status": "done", "stdout": boot_id}}
        if args[:2] == ("wal", "current-seq"):
            return {"ok": True, "seq": 10}
        if args[:2] == ("log", "tail-raw"):
            records = [
                _record(11, "TX", b"sync; reboot\n"),
                _record(12, "TX", b"example-user\n"),
                _record(13, "RX", b"\r\n > " if self.spaced else b"\r\n> "),
                _record(14, "TX", b"sh\n"),
                _record(15, "RX", b"\r\n# "),
                _record(16, "TX", b"echo __READY__abc123\n"),
                _record(17, "RX", b"__READY__abc123\r\n# "),
            ]
            return {"ok": True, "truncated": False, "records": records, "current_seq": 17}
        raise AssertionError(args)


def _ctx(sw: _Sw):
    notes: dict[str, str] = {}

    def note(name: str, content: str) -> str:
        notes[name] = content
        return name

    return SimpleNamespace(
        sw=sw, note=note, notes=notes,
        cfg={"boards": [{"com": "COM1", "platform": "bcm"}],
             "timeouts": {"boot_wait_s": 240}},
    )


def test_brcm_reboot_case_passes_with_spaced_prompt(monkeypatch):
    sw = _Sw()
    ctx = _ctx(sw)
    cleaned: list[str] = []
    monkeypatch.setattr(case.time, "sleep", lambda _: None)
    monkeypatch.setattr(case.guards, "ensure_ready",
                        lambda _ctx, com, **_kw: cleaned.append(com))
    result = case.f9_brcm_spaced_prompt_reboot_login(ctx)
    assert result.verdict == "PASS"
    assert cleaned == ["COM1"]
    assert sw.boot_reads >= 2
    assert "spaced_bdk_prompt" in ctx.notes["sta-wal-markers.txt"]
    assert "example-user" not in str(ctx.notes)


def test_brcm_reboot_case_skips_when_spaced_prompt_not_exercised(monkeypatch):
    ctx = _ctx(_Sw(spaced=False))
    monkeypatch.setattr(case.time, "sleep", lambda _: None)
    monkeypatch.setattr(case.guards, "ensure_ready", lambda *_a, **_kw: True)
    result = case.f9_brcm_spaced_prompt_reboot_login(ctx)
    assert result.verdict == "SKIP"
    assert result.reason_code == "spaced_prompt_not_observed"


def test_wal_markers_pages_through_truncated_range():
    pages = iter([
        {"ok": True, "truncated": True, "records": [
            _record(11, "RX", b"\r\n > "), _record(12, "TX", b"sh\n")]},
        {"ok": True, "truncated": False, "records": [
            _record(13, "TX", b"echo __READY__abc123\n"),
            _record(14, "RX", b"__READY__abc123\r\n# ")]},
    ])
    ctx = SimpleNamespace(sw=SimpleNamespace(run=lambda *_args: next(pages)))
    marks, timeline, error = case._wal_markers(ctx, "COM1", 10)
    assert error is None
    assert len(timeline) == 4
    assert marks == {"spaced_bdk_prompt", "post_login_sh", "ready_probe_tx", "ready_probe_rx"}


def test_wal_markers_rejects_missing_wal():
    ctx = SimpleNamespace(sw=SimpleNamespace(run=lambda *_args: {
        "ok": False, "error_code": "WAL_MISSING", "records": []}))
    _marks, _timeline, error = case._wal_markers(ctx, "COM1", 10)
    assert error == "wal_unavailable"
