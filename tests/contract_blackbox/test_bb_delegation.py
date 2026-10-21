"""黑箱測試：Stage 1A 透明委派（P3-T16）。

A 組：Rust artifact 委派的群組，其 rc／stdout／stderr 必須與 Python 基線位元組相同（Python 輪亦須 PASS）。
B 組：Rust-only 委派機制（manifest 定位、固定入口、遞迴防護、exec 失敗不回退、不搜 PATH、訊號與 stdin 原樣）。

B 組的 bundle 為 `scripts/rust/mkbundle.sh --dev --out` 產物的複本，於測試本體內惰性建立；
建立失敗以 AssertionError 呈現（紅燈時可辨識為「dev bundle 尚未實作」，不是收集錯誤）。
"""
from __future__ import annotations

import json
import os
import shutil
import signal
import subprocess
import time
from pathlib import Path
from typing import Callable

import pytest

import bb_harness
from bb_harness import python_baseline_prefix, repo_root, run_cli

REPO = repo_root()
MKBUNDLE = REPO / "scripts" / "rust" / "mkbundle.sh"
PROBE = REPO / "tests" / "contract_blackbox" / "data" / "delegate_probe.py"
VERSION = (REPO / "VERSION").read_text(encoding="utf-8").strip()

pytestmark = [
    pytest.mark.process_rpc,
    pytest.mark.needs_delegation,
]

def _case(argv: list[str], *, red: bool = True):
    return pytest.param(argv, id=" ".join(argv))


# A：兩種 artifact 都必須產生與 Python 完全相同的 rc／stdout／stderr
SAME_AS_PYTHON = [
    _case(["supervision-mode"]),
    _case(["connect"]),
    _case(["benches"]),
    # 本分支 remote 原生承接（P3-T10 classify 測試與 BC-05）：此案例不是 P3-T16 紅燈，保留為保真守門，不標 xfail。
    _case(["remote", "status"], red=False),
    _case(["setup", "--help"]),
    _case(["daemon", "--help"]),
    _case(["--timeout", "abc", "service", "status"]),
    _case(["-h", "daemon"]),
]


def _parse(stdout: bytes, stderr: bytes, rc: int) -> dict:
    """stdout 必須是單一 JSON object；否則以 AssertionError 呈現（紅燈的可辨識失敗）。"""
    try:
        return json.loads(stdout.decode("utf-8"))
    except (ValueError, UnicodeDecodeError) as exc:
        raise AssertionError(f"stdout 非 JSON（rc={rc}）：{stdout!r}；stderr={stderr!r}") from exc


class _DevBundle:
    """以 `mkbundle.sh --dev --out` 惰性建立的基底 bundle。"""

    def __init__(self, root: Path) -> None:
        self._root = root
        self._out: Path | None = None

    def out(self) -> Path:
        if self._out is None:
            supplied = os.environ.get("SW_WIN_DEV_BUNDLE") if os.name == "nt" else None
            if supplied:
                # Windows 主機直接使用互通腳本建立的本機 dev bundle，避免在 Windows 啟動 POSIX .sh。
                out = Path(supplied)
                assert (out / "bin" / "serialwrap.exe").is_file(), f"Windows dev bundle 缺少 CLI：{out}"
                self._out = out
                return self._out
            out = self._root / "dev-bundle"
            res = subprocess.run(
                [str(MKBUNDLE), "--dev", "--out", str(out)],
                capture_output=True,
                text=True,
                timeout=900,
                cwd=str(REPO),
                check=False,
            )
            assert res.returncode == 0, (
                f"mkbundle.sh --dev --out 失敗（rc={res.returncode}）：{res.stderr.strip()}"
            )
            self._out = out
        return self._out


def _link_or_copy(src: str, dst: str) -> None:
    """變體以硬連結共用基底 bundle 的二進位（同檔案系統，零複製成本）；連結失敗才複製。"""
    try:
        os.link(src, dst)
    except OSError:
        shutil.copy2(src, dst)


class _Variant:
    """基底 bundle 的複本，改寫 `python_cli_delegate.entry`；第一次存取 `exe`／`manifest` 時才建立。"""

    def __init__(self, base: _DevBundle, dest: Path, entry: Callable[[Path], list[str]]) -> None:
        self._base = base
        self._dest = dest
        self._entry = entry
        self._made: Path | None = None

    def _ensure(self) -> Path:
        if self._made is None:
            shutil.copytree(self._base.out(), self._dest, symlinks=True, copy_function=_link_or_copy)
            vdir = self._dest / "versions" / VERSION
            mpath = vdir / "release-manifest.json"
            assert mpath.exists(), f"manifest 不存在：{mpath}"
            data = json.loads(mpath.read_text(encoding="utf-8"))
            data["components"]["python_cli_delegate"]["entry"] = self._entry(vdir)
            mpath.unlink()  # 先斷開可能與基底共用的硬連結，避免改寫到基底 bundle 的 manifest
            mpath.write_text(
                json.dumps(data, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
                encoding="utf-8",
            )
            self._made = self._dest
        return self._made

    @property
    def exe(self) -> Path:
        return self._ensure() / "bin" / ("serialwrap.exe" if os.name == "nt" else "serialwrap")

    @property
    def manifest(self) -> Path:
        return Path(os.path.realpath(self._ensure() / "versions" / VERSION / "release-manifest.json"))


def _probe_entry(vdir: Path) -> list[str]:
    if os.name == "nt":
        # Windows dev bundle（win-blackbox.ps1）的直譯器依 runtime 為 py\python.exe 或
        # py\Scripts\python.exe；沿用複本 manifest 原 entry[0]（改寫前讀取）。
        data = json.loads((vdir / "release-manifest.json").read_text(encoding="utf-8"))
        return [data["components"]["python_cli_delegate"]["entry"][0], "-I", str(PROBE)]
    return ["py/bin/python3", "-I", str(PROBE)]


def _self_loop_entry(vdir: Path) -> list[str]:
    # 相對於 manifest 目錄解析為 bundle 內的 Rust 執行檔自身；Windows dev bundle 的執行檔只在
    # 根層 bin\serialwrap.exe（versions\<V>\ 下沒有 bin）
    return ["../../bin/serialwrap.exe"] if os.name == "nt" else ["bin/serialwrap"]


def _broken_entry(vdir: Path) -> list[str]:
    broken = vdir / "broken-python3"
    broken.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
    broken.chmod(0o000)
    return ["broken-python3", "-I", str(PROBE)]


def _install(tmp_path: Path, base: _DevBundle, monkeypatch: pytest.MonkeyPatch, entry) -> _Variant:
    var = _Variant(base, tmp_path / "bundle", entry)
    # run_cli 經 bb_harness.cli_prefix() 取得受測入口；此處切到本測試專屬的 bundle 複本。
    monkeypatch.setattr(bb_harness, "cli_prefix", lambda: [str(var.exe)])
    return var


@pytest.fixture(scope="module")
def dev_base(tmp_path_factory) -> _DevBundle:
    return _DevBundle(tmp_path_factory.mktemp("dev-base", numbered=False))


@pytest.fixture
def probe_bundle(tmp_path: Path, dev_base: _DevBundle, monkeypatch: pytest.MonkeyPatch) -> _Variant:
    return _install(tmp_path, dev_base, monkeypatch, _probe_entry)


@pytest.fixture
def self_loop_bundle(tmp_path: Path, dev_base: _DevBundle, monkeypatch: pytest.MonkeyPatch) -> _Variant:
    return _install(tmp_path, dev_base, monkeypatch, _self_loop_entry)


@pytest.fixture
def broken_entry_bundle(tmp_path: Path, dev_base: _DevBundle, monkeypatch: pytest.MonkeyPatch) -> _Variant:
    return _install(tmp_path, dev_base, monkeypatch, _broken_entry)


@pytest.mark.parametrize("argv", SAME_AS_PYTHON)
@pytest.mark.core_portable
def test_delegated_output_identical_to_python(env, argv):
    r = run_cli(argv, env=env)
    if bb_harness.is_rust() and argv == ["setup", "--help"]:
        # Stage 1B 原生承接 setup 並加入 Rust-only 旗標（SC-23／SC-24／SC-36，absolute_only），help 與 Python 必然不同；
        # oracle 改為 `capture_cli_help.py --rust-only` 擷取的 golden（同 test_bb_cli_help 的 Rust 輪）。
        golden = REPO / "contracts" / "cli_help" / "rust_only" / f"setup.{env.get('COLUMNS', '80')}.txt"
        assert golden.is_file(), f"證據不足：缺少 Rust-only help golden {golden}"
        nl = bb_harness.NL.decode()
        assert (r.rc, r.stdout.decode(), r.stderr) == (0, golden.read_text(encoding="utf-8").replace("\n", nl), b"")
        return
    py = subprocess.run([*python_baseline_prefix(), *argv], env=env, capture_output=True, check=False)
    assert (r.rc, r.stdout, r.stderr) == (py.returncode, py.stdout, py.stderr)


@pytest.mark.core_portable
def test_daemon_status_via_stub(env, stubd):
    s = stubd({"methods": {"health.status": [{"reply": {"ok": True, "pid": 1, "version": "x"}}]}})
    py = subprocess.run([*python_baseline_prefix(), "--socket", s.endpoint, "daemon", "status"],
                        env=env, capture_output=True, check=False)
    r = run_cli(["--socket", s.endpoint, "daemon", "status"], env=env)
    assert (r.rc, r.stdout) == (py.returncode, py.stdout)


# B：Rust-only 委派機制
@pytest.mark.rust_only
@pytest.mark.core_portable
def test_missing_manifest(tmp_path, env):
    src = Path(bb_harness.cli_prefix()[-1])
    assert src.exists(), f"受測 Rust CLI 不存在（{src}）"
    exe = tmp_path / "serialwrap"
    exe.write_bytes(src.read_bytes())
    exe.chmod(0o755)
    delegate_env = dict(env, SERIALWRAP_TEST_DELEGATE_GROUP="doctor")
    r = subprocess.run([str(exe), "doctor"], env=delegate_env, capture_output=True, check=False)
    assert r.returncode == 1
    out = _parse(r.stdout, r.stderr, r.returncode)
    assert out["error_code"] == "INTERNAL_ERROR"
    assert out["message"].startswith("delegation target unavailable: release manifest not found: ")
    assert r.stderr.decode("utf-8").startswith(
        "serialwrap: doctor failed: INTERNAL_ERROR: delegation target unavailable"
    )


@pytest.mark.rust_only
@pytest.mark.core_portable
@pytest.mark.parametrize("exit_code", [0, 37, 1])
def test_probe_receives_argv_env_stdin_and_exit_code(probe_bundle, env, tmp_path, exit_code):
    counter = tmp_path / "count"
    e = dict(
        env,
        SERIALWRAP_TEST_DELEGATE_GROUP="setup",
        PROBE_READ_STDIN="1",
        PROBE_EXIT=str(exit_code),
        PROBE_COUNTER=str(counter),
    )
    r = run_cli(["--socket", "/x y", "setup", "--user", "é", "--", "-z"], env=e, stdin=b"hello")
    assert r.rc == exit_code
    got = _parse(r.stdout, r.stderr, r.rc)
    assert got["argv"] == ["--socket", "/x y", "setup", "--user", "é", "--", "-z"]
    assert got["depth"] == "1" and len(got["sha"]) == 64 and got["stdin"] == "hello"
    assert got["manifest"] == str(probe_bundle.manifest)
    assert len(counter.read_text(encoding="utf-8").splitlines()) == 1  # 委派恰執行一次（非零 exit 也不重試）


@pytest.mark.rust_only
@pytest.mark.core_portable
def test_exec_failure_runs_nothing_and_does_not_fall_back(broken_entry_bundle, env, tmp_path):
    # entry[0] 存在但不可執行（chmod 000）→ exec failed；不得改走 PATH 或重試
    marker = tmp_path / "marker"
    fake = tmp_path / "fakebin"
    fake.mkdir()
    (fake / "python3").write_text(f"#!/bin/sh\ntouch {marker}\n", encoding="utf-8")
    (fake / "python3").chmod(0o755)
    r = run_cli(
        ["doctor"],
        env=dict(env, SERIALWRAP_TEST_DELEGATE_GROUP="doctor", PATH=f"{fake}:{env['PATH']}"),
    )
    assert r.rc == 1
    assert _parse(r.stdout, r.stderr, r.rc)["message"].startswith(
        "delegation target unavailable: exec failed: "
    )
    assert not marker.exists()


@pytest.mark.rust_only
@pytest.mark.core_portable
def test_recursion_detected_when_entry_points_to_self(self_loop_bundle, env):
    r = run_cli(["doctor"], env=dict(env, SERIALWRAP_TEST_DELEGATE_GROUP="doctor"), timeout_s=20)
    assert r.rc == 1
    assert _parse(r.stdout, r.stderr, r.rc)["message"] == "delegation recursion detected"


@pytest.mark.rust_only
@pytest.mark.core_portable
def test_never_searches_path(probe_bundle, tmp_path, env):
    fake = tmp_path / "fakebin"
    fake.mkdir()
    marker = tmp_path / "marker"
    for name in ("python3", "python", "serialwrap"):
        p = fake / name
        p.write_text(f"#!/bin/sh\ntouch {marker}\nexit 99\n", encoding="utf-8")
        p.chmod(0o755)
    r = run_cli(
        ["doctor"],
        env=dict(env, SERIALWRAP_TEST_DELEGATE_GROUP="doctor", PATH=f"{fake}:{env['PATH']}"),
    )
    assert r.rc == 0 and not marker.exists()


@pytest.mark.rust_only
@pytest.mark.posix_only
def test_exec_replaces_process_and_sigterm_reaches_child(probe_bundle, env):
    e = dict(env, SERIALWRAP_TEST_DELEGATE_GROUP="doctor", PROBE_READ_STDIN="1")
    p = subprocess.Popen(
        [*bb_harness.cli_prefix(), "doctor"], env=e, stdin=subprocess.PIPE, stdout=subprocess.PIPE
    )
    try:
        time.sleep(0.5)
        assert p.poll() is None, "行程已結束：委派未以 exec 取代本行程"
        cmdline = Path(f"/proc/{p.pid}/cmdline").read_bytes().split(b"\0")
        assert any(b"delegate_probe.py" in a for a in cmdline)
        p.send_signal(signal.SIGTERM)
        assert p.wait(timeout=5) == -signal.SIGTERM
    finally:
        if p.poll() is None:
            p.kill()
            p.wait()


@pytest.mark.rust_only
@pytest.mark.windows_native
def test_windows_exec_or_wait_exit_code_and_terminate_propagation(probe_bundle, env, tmp_path):
    exit_env = dict(env, SERIALWRAP_TEST_DELEGATE_GROUP="setup", PROBE_EXIT="37")
    exited = run_cli(["setup"], env=exit_env, timeout_s=30)
    assert exited.rc == 37, f"exec_or_wait 未保留委派 exit code：{exited.rc}"

    counter = tmp_path / "delegate-child-pid.txt"
    wait_env = dict(
        env,
        SERIALWRAP_TEST_DELEGATE_GROUP="setup",
        PROBE_READ_STDIN="1",
        PROBE_COUNTER=str(counter),
    )
    child = subprocess.Popen(
        [*bb_harness.cli_prefix(), "setup"],
        env=wait_env,
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    )
    try:
        deadline = time.monotonic() + 10
        while not counter.is_file() and child.poll() is None and time.monotonic() < deadline:
            time.sleep(0.05)
        assert counter.is_file(), "委派子行程未啟動並進入 stdin 等待"
        child.terminate()
        stdout, stderr = child.communicate(input=b"", timeout=10)
        assert child.returncode == 1, f"Windows terminate exit code 不符：{child.returncode}"
        assert stdout == b"", f"委派子行程在父行程終止後仍存活並輸出：{stdout!r}"
        assert stderr == b"", f"terminate 傳遞期間出現 stderr：{stderr!r}"
    finally:
        if child.poll() is None:
            child.kill()
            child.communicate(timeout=5)


@pytest.mark.rust_only
@pytest.mark.core_portable
def test_no_output_before_delegation(probe_bundle, env):
    r = run_cli(["-v", "--socket", "/nope", "daemon", "status"],
                env=dict(env, SERIALWRAP_TEST_DELEGATE_GROUP="daemon", SERIALWRAP_LOG_LEVEL="DEBUG"))
    assert r.stderr == b""  # probe 不寫 stderr；Rust 端在委派前不得輸出 trace 或任何字元
