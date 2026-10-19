"""P4-T10 `setup` 與 `supervision-mode set` 黑箱契約（SC-12、SC-22、SC-24、DF-10～DF-13、DF-16）。

A/B 案例（on-demand 隔離、stub systemctl user setup、reconcile timeout）同時執行 Python 基線與受測 CLI，
比對輸出與物化樹。
預設執行（Python 基線）時 A/B 案例應通過（Python 對 Python）；以 SERIALWRAP_AB_LABEL=rust-1b
執行時，setup 與 supervision-mode 仍委派 sentinel（exit 97），因此案例應失敗（紅燈，exit 97）。
"""
from __future__ import annotations

import json
import os
import re
import socket
import subprocess
import threading
from pathlib import Path

import pytest

from bb_harness import (
    cli_prefix,
    isolated_env,
    python_baseline_prefix,
    repo_root,
)
from mgmt_fixtures import STUBS_BIN

pytestmark = [pytest.mark.posix_only, pytest.mark.process_rpc]


def _get_cli_prefix() -> list[str]:
    pref = cli_prefix()
    if pref == ["/bin/serialwrap"] and not Path("/bin/serialwrap").exists():
        sw_bundle = os.environ.get("SW_BUNDLE")
        if sw_bundle:
            return [str(Path(sw_bundle) / "bin" / "serialwrap")]
    return pref


def _stub_path(work: Path) -> str:
    bin_dir = work / "exec_bin"
    bin_dir.mkdir(parents=True, exist_ok=True)
    sw_bin = bin_dir / "serialwrap"
    if not sw_bin.exists():
        try:
            os.symlink(repo_root() / "serialwrap", sw_bin)
        except OSError:
            pass
    return f"{bin_dir}{os.pathsep}{STUBS_BIN}{os.pathsep}/usr/bin{os.pathsep}/bin"


def _normalize_output(text: str, work: Path) -> str:
    normalized = text.replace(str(work), "<work>")
    normalized = re.sub(r"/tmp/[^\"\'\s\\]+", "<tmp>", normalized)
    return normalized


def test_setup_ab_on_demand_isolated(tmp_path: Path) -> None:
    results: dict[str, tuple[int, str, list[str]]] = {}
    for label, prefix in (("python", python_baseline_prefix()), ("cli", _get_cli_prefix())):
        work = tmp_path / label
        work.mkdir()
        env = isolated_env(work)
        env["PATH"] = _stub_path(work)
        proc = subprocess.run(
            prefix + ["setup", "--on-demand"],
            env=env,
            capture_output=True,
            text=True,
            timeout=60,
        )
        norm_stdout = _normalize_output(proc.stdout, work)

        tree: list[str] = []
        for root_dir in [work / "config", work / "home", work / "state"]:
            if root_dir.exists():
                for p in root_dir.rglob("*"):
                    tree.append(str(p.relative_to(work)))
        tree.sort()
        results[label] = (proc.returncode, norm_stdout, tree)

    assert results["cli"][0] == results["python"][0]
    assert results["cli"][0] == 0
    assert results["cli"][1] == results["python"][1]
    assert results["cli"][2] == results["python"][2]


def test_setup_ab_user_with_stub_systemctl(tmp_path: Path) -> None:
    results: dict[str, tuple[int, str, str]] = {}
    for label, prefix in (("python", python_baseline_prefix()), ("cli", _get_cli_prefix())):
        work = tmp_path / label
        work.mkdir()
        env = isolated_env(work)
        env["PATH"] = _stub_path(work)

        # 啟動簡單 mock daemon，回應 ping/status/flashing 並在收到 stop 後結束
        sock_path = Path(env["SERIALWRAP_RUN_DIR"]) / "serialwrapd.sock"
        sock_path.parent.mkdir(parents=True, exist_ok=True)
        srv = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        srv.bind(str(sock_path))
        srv.listen(5)
        srv.settimeout(1.0)

        def serve() -> None:
            while True:
                try:
                    conn, _ = srv.accept()
                except socket.timeout:
                    continue
                except OSError:
                    break
                try:
                    data = conn.recv(1024)
                    if b"health.ping" in data:
                        conn.sendall(b"{\"ok\":true}\n")
                    elif b"health.status" in data:
                        conn.sendall(b"{\"ok\":true,\"pid\":4321}\n")
                    elif b"mcu.status" in data:
                        conn.sendall(b"{\"ok\":true,\"flashing\":false}\n")
                    elif b"daemon.stop" in data:
                        conn.sendall(b"{\"ok\":true,\"stopping\":true}\n")
                        conn.close()
                        break
                    conn.close()
                except OSError:
                    break
            try:
                srv.close()
            except OSError:
                pass

        th = threading.Thread(target=serve, daemon=True)
        th.start()

        proc = subprocess.run(
            prefix + ["setup", "--user"],
            env=env,
            capture_output=True,
            text=True,
            timeout=60,
        )
        try:
            srv.close()
        except OSError:
            pass
        th.join(timeout=2.0)

        norm_stdout = _normalize_output(proc.stdout, work)
        unit_file = work / "home" / ".config" / "systemd" / "user" / "serialwrap.service"
        unit_content = unit_file.read_text(encoding="utf-8") if unit_file.exists() else ""
        norm_unit = _normalize_output(unit_content, work)
        results[label] = (proc.returncode, norm_stdout, norm_unit)

    assert results["cli"][0] == results["python"][0]
    assert results["cli"][0] == 0
    assert results["cli"][1] == results["python"][1]
    assert results["cli"][2] == results["python"][2]


def test_setup_ab_reconcile_timeout(tmp_path: Path) -> None:
    results: dict[str, tuple[int, str]] = {}
    for label, prefix in (("python", python_baseline_prefix()), ("cli", _get_cli_prefix())):
        work = tmp_path / label
        work.mkdir()
        env = isolated_env(work)
        env["PATH"] = _stub_path(work)

        # 啟動永遠忽略 daemon.stop 並持續回 ping 的 mock daemon
        sock_path = Path(env["SERIALWRAP_RUN_DIR"]) / "serialwrapd.sock"
        sock_path.parent.mkdir(parents=True, exist_ok=True)
        srv = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        srv.bind(str(sock_path))
        srv.listen(5)
        srv.settimeout(0.5)

        stopped = threading.Event()

        def serve() -> None:
            while not stopped.is_set():
                try:
                    conn, _ = srv.accept()
                except socket.timeout:
                    continue
                except OSError:
                    break
                try:
                    data = conn.recv(1024)
                    if b"health.ping" in data:
                        conn.sendall(b"{\"ok\":true}\n")
                    elif b"health.status" in data:
                        conn.sendall(b"{\"ok\":true,\"pid\":9999}\n")
                    elif b"mcu.status" in data:
                        conn.sendall(b"{\"ok\":true,\"flashing\":false}\n")
                    elif b"daemon.stop" in data:
                        # 回 ok 但不停止服務
                        conn.sendall(b"{\"ok\":true,\"stopping\":true}\n")
                    conn.close()
                except OSError:
                    break
            try:
                srv.close()
            except OSError:
                pass

        th = threading.Thread(target=serve, daemon=True)
        th.start()

        try:
            proc = subprocess.run(
                prefix + ["setup", "--user"],
                env=env,
                capture_output=True,
                text=True,
                timeout=60,
            )
            norm_stdout = _normalize_output(proc.stdout, work)
            results[label] = (proc.returncode, norm_stdout)
        finally:
            stopped.set()
            try:
                srv.close()
            except OSError:
                pass
            th.join(timeout=2.0)

    assert results["cli"][0] == results["python"][0]
    assert results["cli"][0] == 1
    assert "TIMEOUT" in results["cli"][1]
    assert "reconcile_wait_old_exit" in results["cli"][1]
    assert results["cli"][1] == results["python"][1]


@pytest.mark.rust_only
def test_supervision_set_rust_only(tmp_path: Path) -> None:
    work = tmp_path / "supervision_set"
    work.mkdir()
    env = isolated_env(work)
    prefix = _get_cli_prefix()

    # 1. words 不是 [] 或 ["set", MODE] -> INVALID_ARGS exit 2
    bad_words = subprocess.run(
        prefix + ["supervision-mode", "set"],
        env=env,
        capture_output=True,
        text=True,
        timeout=30,
    )
    assert bad_words.returncode == 2, (bad_words.returncode, bad_words.stdout, bad_words.stderr)
    bad_words_json = json.loads(bad_words.stdout)
    assert bad_words_json.get("error_code") == "INVALID_ARGS"
    assert "用法：serialwrap supervision-mode [set MODE]" in bad_words_json.get("message", "")

    # 2. MODE 不在合法值 -> INVALID_ARGS exit 2
    bad_mode = subprocess.run(
        prefix + ["supervision-mode", "set", "bogus", "--force"],
        env=env,
        capture_output=True,
        text=True,
        timeout=30,
    )
    assert bad_mode.returncode == 2, (bad_mode.returncode, bad_mode.stdout, bad_mode.stderr)
    bad_mode_json = json.loads(bad_mode.stdout)
    assert bad_mode_json.get("error_code") == "INVALID_ARGS"
