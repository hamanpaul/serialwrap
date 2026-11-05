"""黑箱 harness 自測。

模組層只放資源 marker；平台 marker 由各函式自帶（同檔混合 POSIX-only 與可攜測試，P0 T14 規則 2）。
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

from bb_harness import NL, run_cli

pytestmark = [pytest.mark.process_rpc]


@pytest.mark.core_portable
def test_stubd_records_wire_and_cli_prints_sorted(env, stubd):
    s = stubd({"methods": {"session.list": [{"reply": {"version": "0.0.0-test", "sessions": [], "ok": True, "b": {"z": 1, "a": 2.0}}}]}})
    r = run_cli(["--socket", s.endpoint, "session", "list"], env=env)
    assert r.rc == 0
    assert r.stdout == b'{"b":{"a":2.0,"z":1},"ok":true,"sessions":[],"version":"0.0.0-test"}' + NL
    assert s.requests() == [b'{"id":1,"method":"session.list","params":{}}\n']


@pytest.mark.core_portable
def test_stubd_close_gives_empty_response(env, stubd):
    s = stubd({"default": [{"close": True}]})
    r = run_cli(["--socket", s.endpoint, "wal", "current-seq"], env=env)
    assert r.rc == 2
    assert json.loads(r.stdout) == {"ok": False, "error_code": "EMPTY_RESPONSE"}


@pytest.mark.posix_only
@pytest.mark.posix_pty
def test_faketarget_login_flow(tmp_path, faketarget):
    import os, time
    link = faketarget.start(tmp_path / "by-id")
    assert link.is_symlink()
    fd = os.open(os.path.realpath(link), os.O_RDWR | os.O_NOCTTY)
    def read_until(token: bytes, timeout: float = 3.0) -> bytes:
        buf, end = b"", time.monotonic() + timeout
        while token not in buf and time.monotonic() < end:
            try:
                buf += os.read(fd, 4096)
            except BlockingIOError:
                time.sleep(0.01)
        assert token in buf, buf
        return buf
    os.set_blocking(fd, False)
    read_until(b"login: ")
    os.write(fd, b"root\r"); read_until(b"Password: ")
    os.write(fd, b"pw\r"); read_until(b"root@fake:~# ")
    os.write(fd, b"echo hi\r"); out = read_until(b"hi\r\nroot@fake:~# ")
    assert b"echo hi\r\n" in out
    os.write(fd, b"\x03"); read_until(b"^C\r\nroot@fake:~# ")
    os.close(fd)
    assert faketarget.rx_log() == b"root\rpw\recho hi\r\x03"


@pytest.mark.posix_only
@pytest.mark.posix_pty
def test_faketarget_login_rejection(tmp_path, faketarget):
    import os, time
    link = faketarget.start(tmp_path / "by-id-reject")
    fd = os.open(os.path.realpath(link), os.O_RDWR | os.O_NOCTTY)

    def read_until(token: bytes, timeout: float = 3.0) -> bytes:
        buf, end = b"", time.monotonic() + timeout
        while token not in buf and time.monotonic() < end:
            try:
                buf += os.read(fd, 4096)
            except BlockingIOError:
                time.sleep(0.01)
        assert token in buf, buf
        return buf

    os.set_blocking(fd, False)
    read_until(b"login: ")
    # 空行 probe：維持在 login:
    os.write(fd, b"\r\n")
    read_until(b"login: ")
    # 錯誤帳號：維持在 login:
    os.write(fd, b"wronguser\r")
    read_until(b"login: ")
    # 正確帳號：進入 Password:
    os.write(fd, b"root\r")
    read_until(b"Password: ")
    # 錯誤密碼：Login incorrect 並退回 login:
    os.write(fd, b"wrongpw\r")
    read_until(b"Login incorrect\r\nlogin: ")
    # 再次正確帳號與密碼：進入 prompt
    os.write(fd, b"root\r")
    read_until(b"Password: ")
    os.write(fd, b"pw\r")
    read_until(b"root@fake:~# ")
    os.close(fd)


@pytest.mark.core_portable
def test_isolated_env_strips_all_serialwrap_vars(env, monkeypatch, tmp_path):
    from bb_harness import isolated_env
    monkeypatch.setenv("SERIALWRAP_RELEASE_MANIFEST", "/x")
    monkeypatch.setenv("SERIALWRAP_SOMETHING_NEW", "1")
    e = isolated_env(tmp_path)
    assert not [k for k in e if k.startswith("SERIALWRAP_") and not k.endswith("_DIR")]
    for k in ("SERIALWRAP_CLI_EXE", "SERIALWRAP_DAEMON_EXE", "SERIALWRAP_AB_LABEL", "SERIALWRAP_DELEGATE_DEPTH"):
        assert k not in env
    assert env["COLUMNS"] == "80"


@pytest.mark.core_portable
def test_daemon_prefix_resolution(monkeypatch):
    from bb_harness import daemon_prefix
    import ab_env
    # 測試指定 .py 入口時前置直譯器
    monkeypatch.setattr(ab_env, "AB_ENV", {"SERIALWRAP_DAEMON_EXE": "/opt/daemon.py"})
    prefix = daemon_prefix()
    assert prefix == [sys.executable, "/opt/daemon.py"]

    # 測試原生執行檔時不前置直譯器（Windows .exe 亦同）
    monkeypatch.setattr(ab_env, "AB_ENV", {"SERIALWRAP_DAEMON_EXE": "/opt/serialwrapd.exe"})
    prefix = daemon_prefix()
    assert prefix == ["/opt/serialwrapd.exe"]


def _run_s1mx6_guard_session(tmp_path: Path, action: str):
    import os
    import subprocess
    import sys

    here = Path(__file__).resolve().parent
    home = tmp_path / "home-at-session-start"
    later_home = tmp_path / "home-after-change"
    unit = home / ".config" / "systemd" / "user" / "serialwrap.service"
    unit.parent.mkdir(parents=True)
    later_home.mkdir()
    if action not in {"add", "add_after_home_change"}:
        unit.write_bytes(b"unit before session\n")
    initial_mtime_ns = unit.stat().st_mtime_ns if unit.exists() else 0

    plugin_dir = tmp_path / "plugin"
    plugin_dir.mkdir()
    plugin = plugin_dir / "s1mx6_guard_plugin.py"
    plugin.write_text(
        r"""import os
from pathlib import Path

import pytest


@pytest.hookimpl(tryfirst=True)
def pytest_sessionstart(session):
    Path.home = classmethod(lambda cls: Path(os.environ["HOME"]))


@pytest.hookimpl(hookwrapper=True, tryfirst=True)
def pytest_runtest_call(item):
    yield
    if item.name != "test_daemon_prefix_resolution":
        return
    unit = Path(os.environ["S1MX6_GUARD_UNIT"])
    action = os.environ["S1MX6_GUARD_ACTION"]
    if action == "add":
        unit.write_bytes(b"new unit\n")
    elif action == "delete":
        unit.unlink()
    elif action == "mtime":
        st = unit.stat()
        os.utime(unit, ns=(st.st_atime_ns, st.st_mtime_ns + 1_000_000_000))
    elif action == "content_same_mtime":
        unit.write_bytes(b"unit after session\n")
        st = unit.stat()
        os.utime(unit, ns=(st.st_atime_ns, int(os.environ["S1MX6_GUARD_MTIME_NS"])))
    elif action == "add_after_home_change":
        os.environ["HOME"] = os.environ["S1MX6_GUARD_LATER_HOME"]
        unit.write_bytes(b"new unit after HOME change\n")
    elif action == "read_error":
        unit.unlink()
        unit.mkdir()
""",
        encoding="utf-8",
    )

    report = tmp_path / "resource-report.json"
    child_env = dict(os.environ)
    child_env.update(
        {
            "HOME": str(home),
            "XDG_CONFIG_HOME": str(tmp_path / "xdg-config"),
            "SERIALWRAP_AB_LABEL": "rust-cli-python-daemon",
            "SERIALWRAP_CLI_EXE": "/bin/true" if os.name != "nt" else sys.executable,
            "SW_RESOURCE_REPORT": str(report),
            "S1MX6_GUARD_UNIT": str(unit),
            "S1MX6_GUARD_ACTION": action,
            "S1MX6_GUARD_LATER_HOME": str(later_home),
            "S1MX6_GUARD_MTIME_NS": str(initial_mtime_ns),
            "PYTHONPATH": os.pathsep.join(
                part for part in (str(plugin_dir), os.environ.get("PYTHONPATH", "")) if part
            ),
        }
    )
    proc = subprocess.run(
        [
            sys.executable,
            "-m",
            "pytest",
            "-q",
            f"--confcutdir={here}",
            "-p",
            "no:cacheprovider",
            "-p",
            "s1mx6_guard_plugin",
            str(Path(__file__).resolve()),
            "-k",
            "test_daemon_prefix_resolution",
        ],
        env=child_env,
        cwd=str(here.parents[1]),
        capture_output=True,
        text=True,
        timeout=120,
    )
    return proc, report


@pytest.mark.core_portable
@pytest.mark.parametrize(
    "action",
    ["add", "delete", "mtime", "content_same_mtime", "add_after_home_change", "read_error"],
)
def test_s1mx6_guard_fails_when_unit_changes(tmp_path: Path, action: str):
    proc, report = _run_s1mx6_guard_session(tmp_path, action)
    output = proc.stdout + proc.stderr
    assert report.exists(), output
    data = json.loads(report.read_text(encoding="utf-8"))
    assert proc.returncode != 0 and data["exit_code"] != 0, {
        "pytest_exit": proc.returncode,
        "report_exit": data["exit_code"],
        "output": output,
    }


@pytest.mark.core_portable
def test_s1mx6_guard_passes_when_unit_is_unchanged(tmp_path: Path):
    proc, report = _run_s1mx6_guard_session(tmp_path, "unchanged")
    output = proc.stdout + proc.stderr
    assert report.exists(), output
    data = json.loads(report.read_text(encoding="utf-8"))
    assert proc.returncode == 0 and data["exit_code"] == 0, {
        "pytest_exit": proc.returncode,
        "report_exit": data["exit_code"],
        "output": output,
    }


# P2 Task 13 的 L1 報告 plugin 與獨立執行時的 sessionfinish 斷言；見 plan Task 2 conftest 段落。
@pytest.mark.core_portable
@pytest.mark.process_rpc
def test_bb_standalone_writes_l1_report(tmp_path):
    import os
    import subprocess
    import sys

    here = Path(__file__).resolve().parent  # tests/contract_blackbox
    rpt = tmp_path / "r.json"
    env = dict(os.environ, SW_RESOURCE_REPORT=str(rpt))
    proc = subprocess.run(
        [sys.executable, "-m", "pytest", "-q", str(here), f"--confcutdir={here}",
         "-p", "no:cacheprovider", "-k", "bb_no_product_import"],
        env=env, cwd=str(here.parents[1]), capture_output=True, text=True, timeout=300,
    )
    assert proc.returncode == 0, proc.stdout + proc.stderr
    assert rpt.exists(), "L1 報告未寫出（sw_resource_report plugin 未載入）"
    data = json.loads(rpt.read_text(encoding="utf-8"))
    assert data["format"] == "l1-results.v1"
    assert data["finished"] is True
