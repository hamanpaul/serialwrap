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


@pytest.mark.core_portable
@pytest.mark.parametrize(
    ("host", "marker"), [("posix", "windows_native"), ("nt", "posix_only")],
)
def test_standalone_rejects_foreign_platform_before_fixtures(monkeypatch, host, marker):
    """獨立黑箱須在 fixture 啟動前拒絕外平台案例，避免在 Linux 開啟 WinDLL。"""
    import importlib.util
    from types import SimpleNamespace

    path = Path(__file__).with_name("conftest.py")
    spec = importlib.util.spec_from_file_location("standalone_conftest_probe", path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    monkeypatch.setattr(module, "os", SimpleNamespace(name=host))
    item = SimpleNamespace(get_closest_marker=lambda name: object() if name == marker else None)
    with pytest.raises(pytest.skip.Exception, match=marker):
        module.pytest_runtest_setup(item)


@pytest.fixture
def bubblewrap():
    """只探測沙箱能力；不接觸 Docker 或宿主執行期。"""
    import shutil
    import subprocess

    if shutil.which("bwrap") is None:
        pytest.skip("cap bubblewrap：宿主未安裝；rust-linux CI 必須先安裝並成功進入沙箱")
    capability = subprocess.run(
        ["bwrap", "--ro-bind", "/", "/", "--unshare-pid", "--proc", "/proc", "true"],
        capture_output=True, text=True, timeout=20,
    )
    if capability.returncode != 0:
        pytest.skip(f"cap bubblewrap namespace：{capability.stderr.strip()}")
    return shutil.which("bwrap")


@pytest.fixture
def clean_sandbox_checkout():
    """在本 worktree 內建立尚無 target 的最小 checkout，避免依賴先前 Cargo 建置。"""
    import shutil
    import tempfile

    source_root = Path(__file__).resolve().parents[2]
    with tempfile.TemporaryDirectory(prefix=".sandbox-probe-", dir=source_root) as directory:
        root = Path(directory)
        script = root / "scripts" / "rust" / "blackbox_sandbox.sh"
        script.parent.mkdir(parents=True)
        shutil.copyfile(source_root / "scripts" / "rust" / "blackbox_sandbox.sh", script)
        assert not (root / "target").exists()
        yield root


@pytest.mark.posix_only
def test_ci_sandbox_hides_host_processes_and_runtime(tmp_path, bubblewrap, clean_sandbox_checkout):
    """乾淨 checkout 亦須可執行探針，並維持 PID、runtime 與唯讀根目錄防線。"""
    import os
    import subprocess

    root = clean_sandbox_checkout
    script = root / "scripts" / "rust" / "blackbox_sandbox.sh"
    probe = tmp_path / "sandbox_probe.py"
    probe.write_text(
        "import errno, os\n"
        "from pathlib import Path\n"
        "assert os.readlink('/proc/self/ns/pid') != os.environ['SW_PIDNS_OUTER']\n"
        "assert not list(Path('/run').iterdir())\n"
        "assert not list(Path('/dev').glob('ttyUSB*'))\n"
        "current = os.readlink('/proc/self/ns/pid')\n"
        "for proc in Path('/proc').iterdir():\n"
        "    if proc.name.isdigit():\n"
        "        assert os.readlink(proc / 'ns/pid') == current\n"
        "try:\n"
        "    Path('/home/.serialwrap-sandbox-write-probe').write_text('forbidden')\n"
        "except OSError as exc:\n"
        "    assert exc.errno == errno.EROFS, repr(exc)\n"
        "else:\n"
        "    raise AssertionError('worktree 外可寫')\n"
        "Path(os.environ['SW_SANDBOX_WRITABLE']).write_text('ok')\n",
        encoding="utf-8",
    )
    # 外層 /tmp 不會出現在新沙箱；探針以 stdin 傳入，成果只寫 worktree 內。
    writable = root / "target" / f"sandbox-probe-{os.getpid()}.txt"
    writable.parent.mkdir(parents=True, exist_ok=True)
    child_env = dict(os.environ, SW_SANDBOX_WRITABLE=str(writable))
    try:
        result = subprocess.run(
            ["bash", str(script), sys.executable, "-"], input=probe.read_text(encoding="utf-8"),
            env=child_env, cwd=root, capture_output=True, text=True, timeout=30,
        )
        assert result.returncode == 0, result.stdout + result.stderr
        assert writable.read_text(encoding="utf-8") == "ok"
    finally:
        writable.unlink(missing_ok=True)


@pytest.mark.posix_only
def test_ci_sandbox_reports_missing_docker_socket(bubblewrap):
    """缺少 socket 時須回證據不足 exit 3，且不能執行要求的命令。"""
    import subprocess

    script = Path(__file__).resolve().parents[2] / "scripts" / "rust" / "blackbox_sandbox.sh"
    result = subprocess.run(
        [bubblewrap, "--ro-bind", "/", "/", "--tmpfs", "/run",
         "--unshare-pid", "--proc", "/proc", "--die-with-parent",
         "bash", str(script), "--with-docker", "printf", "forbidden-command"],
        capture_output=True, text=True, timeout=30,
    )
    assert result.returncode == 3, result.stdout + result.stderr
    assert result.stderr == "缺少 Docker Unix socket，證據不足\n"
    assert result.stdout == ""


@pytest.mark.core_portable
def test_systemd_failure_keeps_complete_reason_and_stderr(tmp_path, monkeypatch):
    """僅驗證診斷工具：合成 INSUFFICIENT 報告仍須失敗，完整原因與 stderr 必須可見。"""
    import subprocess
    import test_cli_mgmt_update as update

    reason = "runner-error-detail-" + "x" * 800
    report = {
        "evidence_layer": "L1.5-docker", "producer": "run-systemd.sh",
        "runner_error": reason,
        "results": [{"case_id": "update-exit1", "verdict": "INSUFFICIENT_EVIDENCE", "reason": reason}],
    }
    monkeypatch.setattr(update, "repo_root", lambda: tmp_path)
    monkeypatch.setattr(update, "update_bundles", lambda: (tmp_path / "old", tmp_path / "new"))
    for bundle, version in ((tmp_path / "old", "0.4.0"), (tmp_path / "new", "0.4.1")):
        cfg = bundle / "versions" / version / "py" / "pyvenv.cfg"
        cfg.parent.mkdir(parents=True)
        cfg.write_text("home = /usr/bin\ninclude-system-site-packages = false\n", encoding="utf-8")

    def failed_runner(argv, **kwargs):
        out = Path(argv[argv.index("--out") + 1])
        out.mkdir(parents=True)
        (out / "l15-docker-report.json").write_text(json.dumps(report), encoding="utf-8")
        return subprocess.CompletedProcess(argv, 1, "runner-stdout-detail", "runner-stderr-detail")

    monkeypatch.setattr(update.subprocess, "run", failed_runner)
    with pytest.raises(AssertionError) as failure:
        update.test_update_systemd_exit1_not_relaunched_l15(tmp_path, {})
    diagnostic = failure.value.args[0]
    assert isinstance(diagnostic, str), "直接附 dict 會被 pytest 摘略，無法取得 CI 根因"
    assert reason in diagnostic and "runner-stderr-detail" in diagnostic and "runner-stdout-detail" in diagnostic
    saved = list((tmp_path / "target" / "systemd-evidence-kept").glob("*l15-docker-report.json"))
    assert len(saved) == 1 and json.loads(saved[0].read_text(encoding="utf-8")) == report


@pytest.mark.core_portable
def test_systemd_rejects_toolcache_runtime_before_docker(tmp_path, monkeypatch):
    """僅驗證啟動前防線：hosted toolcache venv 不能當成 Ubuntu 容器可執行的 runtime。"""
    import test_cli_mgmt_update as update

    old, new = tmp_path / "old", tmp_path / "new"
    for bundle, version in ((old, "0.4.0"), (new, "0.4.1")):
        cfg = bundle / "versions" / version / "py" / "pyvenv.cfg"
        cfg.parent.mkdir(parents=True)
        cfg.write_text("home = /opt/hostedtoolcache/Python/3.12.3/x64/bin\ninclude-system-site-packages = false\n", encoding="utf-8")
    monkeypatch.setattr(update, "update_bundles", lambda: (old, new))
    monkeypatch.setattr(update, "repo_root", lambda: tmp_path)

    def forbidden_docker(*args, **kwargs):
        raise AssertionError("Docker 不得早於 runtime 檢查啟動")

    monkeypatch.setattr(update.subprocess, "run", forbidden_docker)
    with pytest.raises(AssertionError, match="home = /usr/bin"):
        update.test_update_systemd_exit1_not_relaunched_l15(tmp_path, {})
