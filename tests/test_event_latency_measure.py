"""OD-45：event_latency 外部量測判定（`scripts/rust/event_latency_measure.py` 的純函式；不啟動 daemon）。"""
from __future__ import annotations

import importlib.util
import pathlib

import pytest

pytestmark = [pytest.mark.core_portable, pytest.mark.memory_only]

ROOT = pathlib.Path(__file__).resolve().parents[1]
_spec = importlib.util.spec_from_file_location("event_latency_measure", ROOT / "scripts" / "rust" / "event_latency_measure.py")
elm = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(elm)

IDLE = {"loadavg_1min": 0.5, "cpu_count": 4}
BUSY = {"loadavg_1min": 9.0, "cpu_count": 4}


def _run(rep: int, load: str, event: str, verdict: str = "PASS") -> dict:
    return {"repeat": rep, "load": load, "event": event, "verdict": verdict, "max_ms": 1.0, "limit_ms": 50}


def _report(**kw) -> dict:
    runs = [_run(r, ld, ev) for r in (1, 2, 3) for ld in ("idle", "flood") for ev in elm.EXTERNAL_EVENTS]
    contract = elm.load_contract()
    l1 = {n: "passed" for ev in contract["events"] for n in ev["l1_gate"]}
    base = {"runs": runs, "repeats_required": 3, "diagnostic": False, "l1_gate": l1, "freeze_status": "frozen",
            "method_revision": "x", "identity": {"daemon_sha256": "a" * 64, "source_commit": "b" * 40, "source_tree_dirty": False}}
    base.update(kw)
    return base


def test_judge_run_limits_and_void():
    assert elm.judge_run([1.0, 49.9], want=2, limit=50, load_before=IDLE, load_after=IDLE, errors=[])["verdict"] == "PASS"
    assert elm.judge_run([1.0, 50.1], want=2, limit=50, load_before=IDLE, load_after=IDLE, errors=[])["verdict"] == "FAIL"
    # 樣本數不足、有錯誤、host 過載（loadavg > CPU 數）皆不得 PASS／FAIL 放行
    assert elm.judge_run([1.0], want=2, limit=50, load_before=IDLE, load_after=IDLE, errors=[])["verdict"] == "INSUFFICIENT_EVIDENCE"
    assert elm.judge_run([1.0, 2.0], want=2, limit=50, load_before=IDLE, load_after=IDLE,
                         errors=["x"])["verdict"] == "INSUFFICIENT_EVIDENCE"
    r = elm.judge_run([1.0, 2.0], want=2, limit=50, load_before=IDLE, load_after=BUSY, errors=[])
    assert r["void"] is True and r["verdict"] == "INSUFFICIENT_EVIDENCE"
    assert elm.judge_run([1.0], want=1, limit=None, load_before=IDLE, load_after=IDLE, errors=[])["verdict"] == "INSUFFICIENT_EVIDENCE"


def test_overall_verdict_requires_full_coverage_l1_gate_and_freeze():
    assert elm.overall_verdict(_report()) == ("PASS", [])
    rep = _report()
    rep["runs"][4]["verdict"] = "FAIL"
    assert elm.overall_verdict(rep)[0] == "FAIL"
    rep = _report()
    first = next(iter(rep["l1_gate"]))
    rep["l1_gate"][first] = "failed"
    assert elm.overall_verdict(rep)[0] == "FAIL"
    for kw in ({"freeze_status": "pending_review_record"}, {"diagnostic": True}, {"l1_gate": {}},
               {"runs": _report()["runs"][:-1]}, {"identity": {}},
               {"identity": {"daemon_sha256": "a" * 64, "source_commit": "b" * 40, "source_tree_dirty": True}}):
        v, reasons = elm.overall_verdict(_report(**kw))
        assert v == "INSUFFICIENT_EVIDENCE" and reasons, kw
    rep = _report()
    rep["l1_gate"][first] = "missing"
    assert elm.overall_verdict(rep)[0] == "INSUFFICIENT_EVIDENCE"


def test_l1_gate_status_reads_outcomes_from_l1_results():
    contract = elm.load_contract()
    nids = {n for ev in contract["events"] for n in ev["l1_gate"]}
    assert len(nids) >= 2
    one = sorted(nids)[0]
    st = elm.l1_gate_status(contract, {"results": [{"nodeid": one, "outcome": "passed"}]})
    assert st[one] == "passed" and all(v == "missing" for k, v in st.items() if k != one)
    assert set(elm.l1_gate_status(contract, None).values()) == {"missing"}


def test_only_external_events_are_measured():
    contract = elm.load_contract()
    ext = {e["event"] for e in contract["events"] if e.get("measure") == "external"}
    assert ext == set(elm.EXTERNAL_EVENTS)


class _FakeRpc:
    """記憶體替身：`command.get` 依序回 running、done；`command.cancel` 回 `cancel_reply`。"""

    def __init__(self, cancel_reply: dict, status_after_cancel: str = "canceled") -> None:
        self.cancel_reply = cancel_reply
        self.status_after_cancel = status_after_cancel
        self.cancelled = False

    def call(self, method: str, params: dict) -> dict:
        if method == "command.submit":
            return {"ok": True, "cmd_id": "c1"}
        if method == "command.get":
            status = self.status_after_cancel if self.cancelled else "running"
            return {"ok": True, "command": {"status": status, "done_at": "t" if self.cancelled else None}}
        if method == "session.get_state":
            return {"ok": True, "session": {"state": "READY"}}
        raise AssertionError(method)

    def timed(self, method: str, params: dict) -> tuple[float, dict]:
        assert method == "command.cancel"
        self.cancelled = True
        return 0.5, dict(self.cancel_reply)


def test_cancel_sample_rejects_refused_cancel():
    """sol P5-3084388-1：running → 自然完成 → 取消被拒（CMD_NOT_CANCELABLE）不得算有效取消樣本。"""
    with pytest.raises(RuntimeError):
        elm._sample_cancel(_FakeRpc({"ok": False, "error_code": "CMD_NOT_CANCELABLE", "cmd_id": "c1"}, "done"))
    # 回 ok 但命令最終不是 canceled（例如自然完成）也不算
    with pytest.raises(RuntimeError):
        elm._sample_cancel(_FakeRpc({"ok": True, "cmd_id": "c1", "status": "canceled"}, "done"))
    assert elm._sample_cancel(_FakeRpc({"ok": True, "cmd_id": "c1", "status": "canceled"})) == 0.5


def test_report_identity_binds_artifact_commit_tool_and_run(tmp_path):
    """sol P5-3084388-2：報告須可追溯受測 artifact（sha256）、來源 commit、工具／契約 revision 與 run ID。"""
    exe = tmp_path / "serialwrapd"
    exe.write_bytes(b"\x7fELF-fake")
    ident = elm.report_identity(str(exe), run_id="r-test")
    import hashlib

    assert ident["daemon_sha256"] == hashlib.sha256(b"\x7fELF-fake").hexdigest()
    assert ident["run_id"] == "r-test"
    assert len(ident["source_commit"]) == 40 and isinstance(ident["source_tree_dirty"], bool)
    assert len(ident["tool_sha256"]) == 64 and len(ident["contract_sha256"]) == 64
    assert ident["method_revision"] == elm.load_contract()["method_revision"]
    assert "/" not in ident["daemon"] or not ident["daemon"].startswith("/")   # 不落本機絕對路徑（R-21）
