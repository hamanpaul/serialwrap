"""bridge 自測與資格判定的失敗測試（P2-T8，§15.4.3／Spike 4）。"""

from __future__ import annotations

import copy
import os
import platform
import subprocess
import sys
from datetime import date
from pathlib import Path

import pytest

TOOL_ROOT = Path(__file__).resolve().parents[1]
SRC = TOOL_ROOT / "src"


def _report(check_ids, *, tree_sha256="a" * 64, endpoint="posix-pty", transport_disconnect=True):
    return {
        "schema_version": "bridge-selftest.v1",
        "created_at": "2026-10-16T00:00:00Z",
        "tool": {
            "name": "uart-stdio-bridge",
            "version": "0.1.0",
            "tree_sha256": tree_sha256,
        },
        "platform": {"os": platform.system()},
        "endpoint": endpoint,
        "transport_mode": "virtual_pty" if endpoint == "posix-pty" else "virtual_com0com",
        "capabilities": {"transport_disconnect": transport_disconnect},
        "checks": [
            {"id": check_id, "verdict": "PASS", "reason": "", "evidence_dir": ""}
            for check_id in check_ids
        ],
        "verdict": "PASS",
        "test_overrides": {},
    }


def _approved(tree_sha256):
    return {
        "schema_version": "bridge-approved.v1",
        "tree_sha256": tree_sha256,
        "reviewer": "independent-reviewer",
        "review_ref": "review:task-8",
        "date": "2026-10-16",
    }


def _qualification_args(tree_sha256, approved):
    return {
        "endpoint": "posix-pty",
        "tool_tree_sha256": tree_sha256,
        "platform_os": platform.system(),
        "approved": approved,
    }


def test_check_ids_cover_spec_list():
    from uart_stdio_bridge.selftest import checks
    from uart_stdio_bridge.selftest.checks import CHECK_IDS

    assert len(CHECK_IDS) == 14
    required_terms = (
        "完整 byte 值域",
        "多區塊",
        "短讀",
        "stderr",
        "EOF",
        "程序退出",
        "故障控制",
        "CRC",
        "破壞證據",
        "DUT restart",
        "transport fault",
        "空間不足",
    )
    assert checks.__doc__ is not None
    assert all(term in checks.__doc__ for term in required_terms)


@pytest.mark.posix_pty
def test_posix_selftest_all_pass(tmp_path):
    from uart_stdio_bridge.selftest.runner import SelftestContext, run_selftest

    ctx = SelftestContext(
        endpoint="posix-pty",
        com_pair=None,
        transport_mode="virtual_pty",
        work_dir=tmp_path,
    )
    report = run_selftest(ctx)

    assert report["verdict"] == "PASS"
    assert len(report["checks"]) == 14
    assert all(check["verdict"] == "PASS" for check in report["checks"])


def test_report_schema_and_identity(tmp_path):
    from uart_stdio_bridge.identity import tool_tree_sha256
    from uart_stdio_bridge.selftest.runner import SelftestContext, run_selftest

    ctx = SelftestContext(
        endpoint="posix-pty",
        com_pair=None,
        transport_mode="virtual_pty",
        work_dir=tmp_path,
    )
    report = run_selftest(ctx)

    assert report["tool"]["tree_sha256"] == tool_tree_sha256()
    assert report["platform"]["os"] == platform.system()


def test_is_qualified_rejects_other_revision():
    from uart_stdio_bridge.selftest.checks import CHECK_IDS
    from uart_stdio_bridge.selftest.runner import is_qualified

    tree_sha256 = "a" * 64
    report = _report(CHECK_IDS, tree_sha256=tree_sha256)
    approved = _approved(tree_sha256)
    args = _qualification_args(tree_sha256, approved)

    changed_report = copy.deepcopy(report)
    replacement = "b" if tree_sha256[0] != "b" else "c"
    changed_report["tool"]["tree_sha256"] = replacement + tree_sha256[1:]
    assert is_qualified(changed_report, **args) is False

    mismatched_approved = _approved("c" * 64)
    assert is_qualified(report, **_qualification_args(tree_sha256, mismatched_approved)) is False
    assert is_qualified(report, **args) is True


def test_approved_revision_matches_tool_tree():
    from uart_stdio_bridge.identity import tool_tree_sha256
    from uart_stdio_bridge.selftest.runner import load_approved_revision

    assert load_approved_revision()["tree_sha256"] == tool_tree_sha256()


def test_approved_revision_reviewer_present():
    from uart_stdio_bridge.selftest.runner import load_approved_revision

    approved = load_approved_revision()

    assert approved["reviewer"]
    assert approved["review_ref"]
    assert approved["date"]
    assert date.fromisoformat(approved["date"]).isoformat() == approved["date"]


def test_is_qualified_rejects_any_fail_or_notrun():
    from uart_stdio_bridge.selftest.checks import CHECK_IDS
    from uart_stdio_bridge.selftest.runner import is_qualified

    tree_sha256 = "a" * 64
    report = _report(CHECK_IDS, tree_sha256=tree_sha256)
    args = _qualification_args(tree_sha256, _approved(tree_sha256))

    for index in range(len(report["checks"])):
        failed = copy.deepcopy(report)
        failed["checks"][index]["verdict"] = "FAIL"
        assert is_qualified(failed, **args) is False

    for index in range(len(report["checks"])):
        not_run = copy.deepcopy(report)
        not_run["checks"][index]["verdict"] = "NOT_RUN"
        assert is_qualified(not_run, **args) is False


def test_win_serial_transport_not_run_allowed_only_with_flag():
    from uart_stdio_bridge.selftest.checks import CHECK_IDS
    from uart_stdio_bridge.selftest.runner import is_qualified

    tree_sha256 = "a" * 64
    report = _report(
        CHECK_IDS,
        tree_sha256=tree_sha256,
        endpoint="win-serial",
        transport_disconnect=False,
    )
    st12 = next(check for check in report["checks"] if check["id"] == "ST-12")
    st12["verdict"] = "NOT_RUN"
    approved = _approved(tree_sha256)
    args = {
        "endpoint": "win-serial",
        "tool_tree_sha256": tree_sha256,
        "platform_os": platform.system(),
        "approved": approved,
    }

    assert is_qualified(report, **args) is True

    report["capabilities"]["transport_disconnect"] = True
    assert is_qualified(report, **args) is False


@pytest.mark.posix_pty
def test_selftest_detects_broken_bridge(tmp_path, monkeypatch):
    from uart_stdio_bridge.selftest.runner import SelftestContext, run_selftest

    monkeypatch.setenv("UART_STDIO_BRIDGE_TEST_FAULT", "drop_every_1000th")
    ctx = SelftestContext(
        endpoint="posix-pty",
        com_pair=None,
        transport_mode="virtual_pty",
        work_dir=tmp_path,
    )
    report = run_selftest(ctx, only=["ST-02"])

    st02 = next(check for check in report["checks"] if check["id"] == "ST-02")
    assert st02["verdict"] == "FAIL"
    assert report["verdict"] == "FAIL"


@pytest.mark.posix_pty
def test_cli_selftest_exit_code(tmp_path):
    env = os.environ.copy()
    existing_pythonpath = env.get("PYTHONPATH")
    env["PYTHONPATH"] = os.pathsep.join(
        path for path in (str(SRC), existing_pythonpath) if path
    )

    only_st01 = subprocess.run(
        [
            sys.executable,
            "-m",
            "uart_stdio_bridge",
            "selftest",
            "--endpoint",
            "posix-pty",
            "--transport-mode",
            "virtual_pty",
            "--out",
            str(tmp_path / "st-01"),
            "--only",
            "ST-01",
        ],
        capture_output=True,
        text=True,
        env=env,
        check=False,
    )
    assert only_st01.returncode == 0, only_st01.stdout + only_st01.stderr

    only_st06 = subprocess.run(
        [
            sys.executable,
            "-m",
            "uart_stdio_bridge",
            "selftest",
            "--endpoint",
            "posix-pty",
            "--transport-mode",
            "virtual_pty",
            "--out",
            str(tmp_path / "st-06"),
            "--only",
            "ST-06",
        ],
        capture_output=True,
        text=True,
        env=env,
        check=False,
    )
    assert only_st06.returncode == 0, only_st06.stdout + only_st06.stderr


@pytest.mark.parametrize("emit_eof", [True, False])
def test_st05_waits_for_stdout_eof_before_continuing_tx(tmp_path, monkeypatch, emit_eof):
    import json
    import threading
    from types import SimpleNamespace

    from uart_stdio_bridge.selftest import checks

    class _Clock:
        now = 0.0
        emitted = False

        def monotonic(self):
            return self.now

        def sleep(self, seconds):
            self.now += seconds
            if emit_eof and seconds < 0.1 and not self.emitted:
                _bridges[-1].writer.append(meta={"event": "dut_stdout_eof"})
                self.emitted = True

    class _Writer:
        def __init__(self, path, **_kwargs):
            self.path = Path(path)
            self.path.parent.mkdir(parents=True, exist_ok=True)
            self.path.write_text("", encoding="utf-8")
            self.seq = 0

        def append(self, **record):
            self.seq += 1
            record = {"seq": self.seq, **record}
            with self.path.open("a", encoding="utf-8") as stream:
                stream.write(json.dumps(record) + "\n")

    _bridges = []

    class _Bridge:
        def __init__(self, _cfg, _endpoint, _dut, writer, **_kwargs):
            self.writer = writer
            self._done_event = threading.Event()
            self.failure = None
            _bridges.append(self)

        def start(self):
            return None

        def handle_action(self, action, _params, _action_id):
            if action == "shutdown":
                self.writer.append(meta={"event": "bridge_stop"})
                self._done_event.set()
            return {}

        def wait(self):
            return 0

    class _Serial:
        def __enter__(self):
            return self

        def __exit__(self, *_args):
            return None

        def write(self, data):
            _bridges[-1].writer.append(observation="dut_stdin_write", len=len(data))
            return len(data)

        def read(self, size):
            return b"A" * size

    monkeypatch.setattr(checks, "time", _Clock())
    monkeypatch.setattr(checks, "open_endpoint", lambda _spec: object())
    monkeypatch.setattr(checks, "DutProcess", lambda *_args, **_kwargs: object())
    monkeypatch.setattr(checks, "EvidenceWriter", _Writer)
    monkeypatch.setattr(checks, "Bridge", _Bridge)
    monkeypatch.setitem(sys.modules, "serial", SimpleNamespace(Serial=lambda *_args, **_kwargs: _Serial()))

    ev_dir = tmp_path / "st05"
    ev_dir.mkdir()
    ctx = checks.SelftestContext(
        endpoint="win-serial",
        com_pair=("COM20", "COM21"),
        transport_mode="virtual_com0com",
        work_dir=tmp_path,
    )

    result = checks._run_st05(ctx, ev_dir)

    if not emit_eof:
        assert result.verdict == "FAIL"
        assert "dut_stdout_eof" in result.reason
        return

    assert result.verdict == "PASS", result.reason
    wire = [json.loads(line) for line in (ev_dir / "wire.ndjson").read_text().splitlines()]
    eof_seq = next(record["seq"] for record in wire if record.get("meta", {}).get("event") == "dut_stdout_eof")
    post_eof_tx = next(
        record["seq"]
        for record in wire
        if record.get("observation") == "dut_stdin_write" and record.get("len") == 512
    )
    assert eof_seq < post_eof_tx
