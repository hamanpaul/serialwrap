"""Stage 1B pidns／release-check 之 POSIX 專屬測試（自 test_rust_bench_tools.py 移出）。

以模組層 `posix_only` 取代函式層 `skipif(os.name != "posix")`：函式層 skipif 不會被
`static_platform_skip` 偵測，平台限制因此不進入 `core_portable_platform_skips`（sol 3f05797#2）。
模組層 `posix_only` 於 Windows collection 前即排除（tests/conftest.py），不產生 skip 紀錄。
"""
from __future__ import annotations

import importlib.util
import json
import os
import shutil
import signal
import subprocess
import sys
import time
from pathlib import Path

import pytest

pytestmark = [
    pytest.mark.posix_only,
    pytest.mark.process_rpc,
]

ROOT = Path(__file__).resolve().parents[1]
AB_BLACKBOX_1B = ROOT / "scripts" / "rust" / "ab_blackbox_1b.py"


def _load_ab_blackbox_1b():
    spec = importlib.util.spec_from_file_location("ab_blackbox_1b_release_under_test", AB_BLACKBOX_1B)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _host_pid_ns() -> str:
    return os.readlink("/proc/self/ns/pid")


def _write_pidlog(path: Path, rows: list[dict]) -> Path:
    path.write_text("".join(json.dumps(row) + "\n" for row in rows), encoding="utf-8")
    return path


def test_release_check_never_probes_host_pid_of_foreign_namespace_row(tmp_path: Path) -> None:
    """pidns_rig 子行程寫入的 pid 為 namespace 內部編號（回歸：B 輪因 pid 120 被誤查 host /proc/120 而未啟動）。

    不同 namespace 的列不得以該數字查 host 行程；此處 host 上恰有一個同號行程存活，釋放檢查仍須判定為已釋放。
    """
    ab = _load_ab_blackbox_1b()
    sleeper = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(60)"])
    try:
        pidlog = _write_pidlog(tmp_path / "a.pids", [
            {"pid": sleeper.pid, "children": [], "socket": None, "lock": None, "run_dir": None,
             "pid_ns": "pid:[4026539999]"},
        ])
        result = ab._release_check(pidlog, tmp_path / "release.json", timeout_s=1.0)
    finally:
        sleeper.kill()
        sleeper.wait()
    assert sleeper.pid not in result["unreaped_pids"], result
    assert result["ok"] is True, result


def test_release_check_still_flags_live_pid_in_host_namespace(tmp_path: Path) -> None:
    """嚴格度不降低：與 host 同 namespace 的存活行程仍為 unreaped。"""
    ab = _load_ab_blackbox_1b()
    sleeper = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(60)"])
    try:
        pidlog = _write_pidlog(tmp_path / "a.pids", [
            {"pid": sleeper.pid, "children": [], "socket": None, "lock": None, "run_dir": None,
             "pid_ns": _host_pid_ns()},
        ])
        result = ab._release_check(pidlog, tmp_path / "release.json", timeout_s=0.5)
    finally:
        sleeper.kill()
        sleeper.wait()
    assert sleeper.pid in result["unreaped_pids"], result
    assert result["ok"] is False, result


def test_release_check_flags_populated_foreign_namespace_until_it_empties(tmp_path: Path) -> None:
    """namespace 仍有行程即 unreaped（列出其 host pid）；全數結束後才釋放。宿主無非特權 unshare 時 SKIP（非 PASS）。"""
    if shutil.which("unshare") is None:
        pytest.skip("缺 unshare，無法建立 PID namespace（環境能力不足，非 PASS）")
    probe = subprocess.run(
        ["unshare", "--user", "--map-current-user", "--pid", "--fork", "--mount", "--mount-proc", "true"],
        capture_output=True, timeout=30, check=False,
    )
    if probe.returncode != 0:
        pytest.skip("此宿主無法以非特權 unshare 建立 PID namespace（環境能力不足，非 PASS）")

    ab = _load_ab_blackbox_1b()
    host_ns = _host_pid_ns()
    wrapper = subprocess.Popen(
        ["unshare", "--user", "--map-current-user", "--pid", "--fork", "--mount", "--mount-proc",
         sys.executable, "-c", "import time; time.sleep(60)"],
        start_new_session=True, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
    )
    try:
        inner = None
        deadline = time.monotonic() + 15
        while inner is None and time.monotonic() < deadline:
            for proc_dir in Path("/proc").iterdir():
                if not proc_dir.name.isdigit():
                    continue
                try:
                    stat = (proc_dir / "stat").read_text(encoding="ascii")
                except OSError:
                    continue
                fields = stat[stat.rfind(")") + 2:].split()
                if fields and fields[1] == str(wrapper.pid) and os.readlink(proc_dir / "ns" / "pid") != host_ns:
                    inner = int(proc_dir.name)
                    break
            time.sleep(0.05)
        assert inner is not None, "未找到 unshare 子行程"
        namespace = os.readlink(f"/proc/{inner}/ns/pid")
        rows = [
            {"pid": 1, "children": [], "socket": None, "lock": None, "run_dir": None, "pid_ns": namespace},
            {"pid": wrapper.pid, "children": [], "socket": None, "lock": None, "run_dir": None, "pid_ns": host_ns},
        ]
        pidlog = _write_pidlog(tmp_path / "a.pids", rows)
        held = ab._release_check(pidlog, tmp_path / "held.json", timeout_s=1.0)
        assert held["ok"] is False, held
        assert inner in held["unreaped_pids"], held
    finally:
        try:
            os.killpg(wrapper.pid, signal.SIGKILL)
        except ProcessLookupError:
            pass
        wrapper.wait(timeout=30)

    released = ab._release_check(pidlog, tmp_path / "released.json", timeout_s=5.0)
    assert released["ok"] is True, released
