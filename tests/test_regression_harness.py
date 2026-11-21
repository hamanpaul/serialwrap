"""#155 regression plugin harness／core 純邏輯單測——不 import testpilot、不碰 live。"""
from __future__ import annotations

import subprocess
import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT / "regression"))

from serialwrap_regression import core, guards, harness  # noqa: E402
from serialwrap_regression.cases import f07_file_transfer as f07  # noqa: E402

pytestmark = [pytest.mark.core_portable, pytest.mark.memory_only]

CFG = {
    "boards": [
        {"com": "COM0", "alias": "dut-prpl", "serial": "S0", "platform": "prpl"},
        {"com": "COM1", "alias": "com1-brcm", "serial": "S1", "platform": "bcm"},
    ],
    "serialwrap_exe": "/opt/bin/serialwrap",
    "allow_destructive": False,
    "tmux_prefix": "swreg",
    "timeouts": {"ready_wait_s": 180, "boot_wait_s": 240, "cmd_timeout_s": 12},
}


def _mk(case_id: str, family: str = "F3", **kw) -> harness.Case:
    return harness.Case(
        id=case_id,
        family=family,
        title=case_id,
        run=lambda ctx: None,
        issues=kw.pop("issues", ("#94",)),
        **kw,
    )


@pytest.fixture(autouse=True)
def _clean_registry(monkeypatch):
    monkeypatch.setattr(harness, "REGISTRY", [])


def test_register_rejects_duplicate_id():
    harness.register(_mk("a"))
    with pytest.raises(ValueError, match="duplicate"):
        harness.register(_mk("a"))


def test_register_rejects_empty_issues():
    with pytest.raises(ValueError, match="issue"):
        harness.register(_mk("b", issues=()))


def test_register_rejects_unknown_family():
    with pytest.raises(ValueError, match="family"):
        harness.register(_mk("c", family="F99"))


def test_ordered_follows_family_order():
    harness.register(_mk("x-f9", family="F9"))
    harness.register(_mk("x-f3b", family="F3"))
    harness.register(_mk("x-f3a", family="F3"))
    harness.register(_mk("x-f1", family="F1"))
    got = [c.id for c in harness.ordered(list(harness.REGISTRY))]
    assert got == ["x-f3a", "x-f3b", "x-f1", "x-f9"]


def test_registry_disjoint_from_realhw():
    """兩 plugin 的 registry 不得互染（#155 spec：discover 互不撈到對方）。"""
    core.ensure_realhw_importable()
    import realhw.cases  # noqa: F401
    from realhw import harness as rh

    reg_ids = {c.id for c in core.load_registry()}
    realhw_ids = {c.id for c in rh.REGISTRY}
    assert reg_ids.isdisjoint(realhw_ids)


def test_case_dicts_carry_family_and_issues():
    harness.register(_mk("f3-x", family="F3", issues=("#94", "#16")))
    dicts = core.build_case_dicts(list(harness.REGISTRY), CFG)
    meta = dicts[0]["metadata"]
    assert meta["family"] == "F3"
    assert meta["issues"] == ["#94", "#16"]
    assert dicts[0]["topology"]["devices"]["COM0"]["platform"] == "prpl"


def test_runtime_skip_destructive_gated():
    got = core.runtime_skip({"destructive": True}, {}, None, allow_destructive=False)
    assert got is not None and got[0] == "destructive_gated"
    assert core.runtime_skip({"destructive": True}, {}, None, allow_destructive=True) is None


def test_runtime_skip_broken_by_and_requires():
    got = core.runtime_skip({"destructive": True}, {}, "f9-x", allow_destructive=True)
    assert got is not None and got[0] == "broken_by:f9-x"
    got = core.runtime_skip({"requires": ["two_boards"]}, {"two_boards": "board_down"}, None,
                            allow_destructive=False)
    assert got is not None and got[0] == "board_down"
    assert core.runtime_skip({}, {}, None, allow_destructive=False) is None


def test_failure_payload_fills_category():
    assert core.failure_payload({"verdict": "PASS"}) is None
    fail = core.failure_payload({"verdict": "FAIL", "reason": "x", "category": "",
                                 "reason_code": "r", "evidence": {}})
    assert fail is not None and fail["category"] == "test"
    skip = core.failure_payload({"verdict": "SKIP", "reason": "x", "category": "",
                                 "reason_code": "r", "evidence": {}})
    assert skip is not None and skip["category"] == "environment"


def test_filter_for_run_keeps_destructive_for_gate():
    """未點名時不預先剔除 destructive——由 runtime_skip 記 SKIP（gate 可見）。"""
    harness.register(_mk("f9-y", family="F9", destructive=True))
    dicts = core.build_case_dicts(list(harness.REGISTRY), CFG)
    assert [c["id"] for c in core.filter_for_run(dicts, set())] == ["f9-y"]
    assert core.filter_for_run(dicts, {"nope"}) == []


def test_load_testbed_defaults(tmp_path):
    example = tmp_path / "testbed.yaml.example"
    example.write_text("boards:\n  - com: COM0\n    serial: S0\n", encoding="utf-8")
    cfg = core.load_testbed(example)
    assert cfg["allow_destructive"] is False
    assert Path(cfg["serialwrap_exe"]).parts[-3:] == (".local", "bin", "serialwrap")
    assert not cfg["serialwrap_exe"].startswith("~")
    assert cfg["timeouts"]["ready_wait_s"] == 180
    real = tmp_path / "testbed.yaml"
    real.write_text("allow_destructive: true\nboards: []\n", encoding="utf-8")
    assert core.load_testbed(example)["allow_destructive"] is True


def test_pending_issue_accepts_empty_issues():
    """Stage 0 待補 issue：pending_issue=True 且 issues 為空可註冊（不得以假號補位）。"""
    case = harness.register(_mk("p-pending", issues=(), pending_issue=True))
    assert case.pending_issue is True
    assert case.issues == ()


def test_pending_issue_and_issues_are_mutually_exclusive():
    with pytest.raises(ValueError, match="互斥"):
        harness.register(_mk("p-both", issues=("#94",), pending_issue=True))


def test_empty_issues_still_rejected_without_pending_flag():
    """正式 case 維持 #155 規則：空 issues 且未標 pending_issue 一律拒絕。"""
    with pytest.raises(ValueError, match="issue"):
        harness.register(_mk("p-none", issues=(), pending_issue=False))


def test_case_dict_emits_pending_issue_key():
    harness.register(_mk("p-dict", issues=(), pending_issue=True))
    dicts = core.build_case_dicts(list(harness.REGISTRY), CFG)
    meta = dicts[0]["metadata"]
    assert meta["pending_issue"] is True
    assert meta["issues"] == []


def test_case_dict_pending_issue_false_for_issue_bound_case():
    harness.register(_mk("p-bound", issues=("#94",)))
    dicts = core.build_case_dicts(list(harness.REGISTRY), CFG)
    meta = dicts[0]["metadata"]
    assert meta["pending_issue"] is False
    assert meta["issues"] == ["#94"]


def _write_proc_stat(proc_root: Path, pid: int, state: str, pgid: int) -> None:
    proc_dir = proc_root / str(pid)
    proc_dir.mkdir()
    (proc_dir / "stat").write_text(
        f"{pid} (throwaway worker with spaces) {state} 1 {pgid} 0 0 0\n",
        encoding="ascii",
    )


def test_throwaway_wait_reaps_daemon_and_ignores_zombie_group_members(tmp_path):
    class _FinishedProcess:
        pid = 410

        def __init__(self):
            self.wait_timeout = None

        def wait(self, *, timeout):
            self.wait_timeout = timeout
            return 0

    proc_root = tmp_path / "proc"
    proc_root.mkdir()
    _write_proc_stat(proc_root, 420, "Z", 410)
    daemon = guards.ThrowawayDaemon("/bin/serialwrap", tmp_path / "ta", tmp_path / "byid", "")
    daemon._proc = _FinishedProcess()
    daemon._proc_pgid = 410

    exit_code, live_members = daemon.wait_for_exit(timeout_s=0.75, proc_root=proc_root)

    assert daemon._proc.wait_timeout == 0.75
    assert exit_code == 0
    assert live_members == []

    _write_proc_stat(proc_root, 421, "S", 410)
    _, live_members = daemon.wait_for_exit(timeout_s=0.75, proc_root=proc_root)
    assert live_members == [421]


class _UnreapableProcess:
    """wait 一律逾時、poll 一律回報仍在執行的替身（模擬 SIGKILL 後仍收不回來的 daemon）。"""

    pid = 510

    def __init__(self):
        self.wait_calls = 0

    def poll(self):
        return None

    def wait(self, *, timeout):
        self.wait_calls += 1
        raise subprocess.TimeoutExpired(cmd="serialwrapd", timeout=timeout)


def _unreapable_daemon(tmp_path, monkeypatch):
    daemon = guards.ThrowawayDaemon("/bin/serialwrap", tmp_path / "ta", tmp_path / "byid", "")
    daemon._proc = _UnreapableProcess()
    daemon._proc_pgid = 510
    daemon._run_dir = tmp_path / "run"
    daemon._run_dir.mkdir()
    signals: list[tuple[int, int]] = []
    monkeypatch.setattr(guards.os, "killpg", lambda pgid, sig: signals.append((pgid, sig)))
    monkeypatch.setattr(daemon, "run", lambda *args, **kwargs: None)
    return daemon, signals


def test_throwaway_exit_keeps_original_exception_when_daemon_is_unreapable(tmp_path, monkeypatch):
    daemon, signals = _unreapable_daemon(tmp_path, monkeypatch)
    proc = daemon._proc
    run_dir = daemon._run_dir

    with pytest.warns(RuntimeWarning, match="仍未被回收"):
        daemon.__exit__(ValueError, ValueError("原始錯誤"), None)

    assert proc.wait_calls == 2
    assert signals == [(510, 15), (510, 9)]
    assert daemon._proc is None
    assert not run_dir.exists()


def test_throwaway_exit_reports_unreapable_daemon_after_cleanup(tmp_path, monkeypatch):
    daemon, signals = _unreapable_daemon(tmp_path, monkeypatch)
    run_dir = daemon._run_dir

    with pytest.raises(RuntimeError, match="pid=510"):
        daemon.__exit__(None, None, None)

    assert signals == [(510, 15), (510, 9)]
    assert daemon._proc is None
    assert not run_dir.exists()


def _wal_record(seq: int, direction: str, payload: bytes, cmd_id: str | None = None) -> str:
    import base64
    import json

    return json.dumps({
        "seq": seq,
        "dir": direction,
        "payload_b64": base64.b64encode(payload).decode("ascii"),
        "cmd_id": cmd_id,
    })


def test_i282_midline_oracle_requires_busy_echo_stall_and_two_prior_chunks():
    import base64

    from sw_core.file_transfer import _chunk_command

    marker = "I282_ECHO_OFF_test"
    path = "/tmp/.sw_upload_0123456789ab"
    encoded = base64.b64encode(bytes(range(256)) * 2).decode("ascii")
    first = _chunk_command(encoded, "base64", ">", path)
    second = _chunk_command(encoded, "base64", ">>", path)
    rows = [
        _wal_record(1, "TX", (first + "\n").encode("ascii"), "ft-12345678-0"),
        _wal_record(2, "TX", (second + "\n").encode("ascii"), "ft-12345678-1"),
    ]
    wal_text = "\n".join(rows)
    partial_line = second.encode("ascii")[:200]
    bridge_rx = (
        first.encode("ascii") + b"\r\nSW_XFER_CHUNK_42\r\n"
        + second.encode("ascii") + b"\r\nSW_XFER_CHUNK_42\r\n"
        + partial_line + b"\r\n" + marker.encode("ascii") + b"\r\n"
    )

    assert f07._midline_push_observed(
        wal_text, bridge_rx_bytes=bridge_rx, echo_off_marker=marker, foreground_busy=True,
    ) is True
    assert f07._midline_push_observed(
        wal_text, bridge_rx_bytes=bridge_rx, echo_off_marker=marker, foreground_busy=False,
    ) is False
    assert f07._midline_push_observed(
        rows[1], bridge_rx_bytes=bridge_rx,
        echo_off_marker=marker, foreground_busy=True,
    ) is False
    one_complete_chunk = rows[1]
    assert f07._midline_push_observed(
        one_complete_chunk, bridge_rx_bytes=bridge_rx,
        echo_off_marker=marker, foreground_busy=True,
    ) is False
    complete_line = bridge_rx.replace(
        partial_line + b"\r\n", second.encode("ascii") + b"\r\n\r\n",
    )
    assert f07._midline_push_observed(
        wal_text, bridge_rx_bytes=complete_line, echo_off_marker=marker, foreground_busy=True,
    ) is False


def test_i282_midline_oracle_rejects_shutdown_without_line_clear():
    import base64

    from sw_core.file_transfer import _chunk_command

    marker = "I282_ECHO_OFF_no_clear"
    path = "/tmp/.sw_upload_0123456789ab"
    encoded = base64.b64encode(bytes(range(256)) * 2).decode("ascii")
    first = _chunk_command(encoded, "base64", ">", path)
    second = _chunk_command(encoded, "base64", ">>", path)
    rows = [
        _wal_record(1, "TX", (first + "\n").encode("ascii"), "ft-12345678-0"),
        _wal_record(2, "TX", (second + "\n").encode("ascii"), "ft-12345678-1"),
    ]
    bridge_rx = (
        second.encode("ascii")[:200]
        + marker.encode("ascii") + b"\r\n"
    )

    assert f07._midline_push_observed(
        "\n".join(rows), bridge_rx_bytes=bridge_rx,
        echo_off_marker=marker, foreground_busy=True,
    ) is True
    verdict = f07._midline_shutdown_verdict(clear_count=0)
    assert verdict is not None
    assert verdict.verdict == "FAIL"
    assert verdict.reason_code == "midline_clear_count_mismatch"


def test_i282_uart_rx_observer_reads_only_throwaway_console_pty(tmp_path, monkeypatch):
    class _ThrowawayCli:
        def __init__(self):
            self.calls = []

        def run(self, *args, **kwargs):
            self.calls.append((args, kwargs))
            if args[1] == "console-attach":
                return {"ok": True, "vtty": "/dev/pts/47", "client_id": "client-i282"}
            return {"ok": True}

    class _ReaderProcess:
        def poll(self):
            return 0

    ta = _ThrowawayCli()

    def _fake_popen(argv, **kwargs):
        assert argv == ["cat", "/dev/pts/47"]
        kwargs["stdout"].write(b"UART bridge RX bytes")
        return _ReaderProcess()

    monkeypatch.setattr(f07.subprocess, "Popen", _fake_popen)
    path = tmp_path / "bridge-rx.bin"
    observer = f07._UartRxObserver(ta, "COM0", path)
    try:
        assert observer.bytes() == b"UART bridge RX bytes"
    finally:
        observer.close()

    assert [call[0][1] for call in ta.calls] == ["console-attach", "console-detach"]


def test_i282_uart_rx_observer_detaches_if_capture_file_cannot_open(tmp_path):
    class _ThrowawayCli:
        def __init__(self):
            self.calls = []

        def run(self, *args, **kwargs):
            self.calls.append((args, kwargs))
            if args[1] == "console-attach":
                return {"ok": True, "vtty": "/dev/pts/47", "client_id": "client-i282"}
            return {"ok": True}

    ta = _ThrowawayCli()
    missing_parent_path = tmp_path / "missing" / "bridge-rx.bin"

    with pytest.raises(FileNotFoundError):
        f07._UartRxObserver(ta, "COM0", missing_parent_path)

    assert [call[0][1] for call in ta.calls] == ["console-attach", "console-detach"]


def test_i282_observer_creation_failure_stops_echo_watcher(tmp_path, monkeypatch):
    commands = []

    class _ThrowawayCli:
        _workdir = tmp_path

        def run(self, *args, **kwargs):
            if "file" in args and "push" in args:
                return {"ok": True}
            raise AssertionError(f"未預期的 throwaway CLI 呼叫：{args!r}")

        def wal_text(self):
            return ""

    class _Context:
        case_dir = tmp_path

        def note(self, *_args):
            pass

    def _line_command(_ta, _com, command, *, timeout_s=15.0):
        commands.append(command)
        if "for f in /tmp/*" in command:
            return {
                "status": "done",
                "stdout": "I282_42_BEGIN\nI282_42_DEST_ABSENT\nI282_42_END\n",
            }
        return {"status": "done", "stdout": ""}

    def _observer_unavailable(*_args, **_kwargs):
        raise RuntimeError("observer unavailable")

    monkeypatch.setattr(f07, "_throwaway_line_command", _line_command)
    monkeypatch.setattr(f07, "_UartRxObserver", _observer_unavailable)

    result = f07._body_push_stop_midline_clean_restart(
        _Context(), _ThrowawayCli(), "COM0", {"serial": "board-i282"},
    )

    assert result.reason_code == "bridge_observer_unavailable"
    assert any(
        command.startswith("touch /tmp/.i282-echo-watcher-")
        and ".done; sleep 2; stty echo; rm -f " in command
        for command in commands
    ), "observer 建立失敗離場前必須通知 DUT watcher 停止並復原 echo"


def test_i282_post_restart_temp_growth_is_detected():
    before = {"/tmp/.sw_upload_0123456789ab": 512}

    assert f07._changed_upload_temps(before, dict(before)) == []
    assert f07._changed_upload_temps(
        before, {"/tmp/.sw_upload_0123456789ab": 768},
    ) == ["/tmp/.sw_upload_0123456789ab"]
    assert f07._changed_upload_temps(
        before,
        {
            "/tmp/.sw_upload_0123456789ab": 512,
            "/tmp/.sw_upload_fedcba987654": 256,
        },
    ) == ["/tmp/.sw_upload_fedcba987654"]


def test_i282_upload_size_marker_is_parsed_from_dut_rx():
    marker = "I282_PRE_test_TEMP"
    wal_text = "\n".join([
        _wal_record(1, "RX", f"{marker} /tmp/.sw_upload_0123456789ab 1024\n".encode()),
        _wal_record(2, "RX", b"I282_PRE_test_TEMP /tmp/not-an-upload 55\n"),
        _wal_record(3, "TX", f"{marker} /tmp/.sw_upload_fedcba987654 9999\n".encode()),
    ])

    assert f07._upload_size_markers(wal_text, marker=marker) == {
        "/tmp/.sw_upload_0123456789ab": 1024,
    }


def test_i282_echo_watcher_waits_for_restore_signal_and_has_fallback_cleanup():
    script = f07._echo_stall_watcher_script(nonce="test")

    assert "done_path=/tmp/.i282-echo-watcher-test.done" in script
    assert "trap cleanup EXIT" in script
    assert "target_size=1024" in script
    assert '[ ! -e "$done_path" ]' in script
    assert '[ "$elapsed" -lt 180 ]' in script
    assert 'stty echo </dev/tty >/dev/null 2>&1 || :' in script
    assert 'stty -echo </dev/tty || exit 1' in script
    assert "I282_ECHO_RESTORED_%s" in script
    assert "'test'" in script


def test_i282_case_uses_midline_and_post_restart_temp_oracles():
    import inspect

    source = inspect.getsource(f07._body_push_stop_midline_clean_restart)
    assert "_midline_push_observed(" in source
    assert "bridge_rx_bytes=uart_observer.bytes()" in source
    assert "console-attach" in inspect.getsource(f07._UartRxObserver)
    assert "_upload_size_markers(" in source
    assert "_changed_upload_temps(" in source
    assert "daemon_exit_code != 0" in source
    assert "_midline_shutdown_verdict(clear_count)" in source
    assert "extra_chunk_commands != 0" in source
    assert "ta.wait_for_exit(timeout_s=15.0)" in source
    assert "touch {shlex.quote(watcher_done_path)}; sleep 2; stty echo" in source
