"""P4-T8 `remote`（-R／-L／status／close／stop 別名）黑箱契約（SC-21、BC-05）。

A/B 案例（`status`／`close` 空集合、stub ssh 開通）同時執行 Python 基線與受測 CLI，比對輸出。
`test_remote_topologies_docker` 使用 P4-T1 的 `mgmt_bundle`（`mkbundle.sh --dev --delegate-sentinel`）
驗證 Rust CLI 在四種容器拓樸的遠端行為。fixture 延遲載入，避免本檔在夾具未就緒時影響整體收集。
"""
from __future__ import annotations

import os
import re
import json
import signal
import shutil
import subprocess
import time
from pathlib import Path

import pytest

from bb_harness import (
    cli_prefix,
    isolated_env,
    python_baseline_prefix,
    repo_root,
)

# ssh stub：-O check 成功；ss 探測回 loopback 監聽列；spawn（含 -N）長駐，與 Python 基線共用行為。
_SSH_STUB = """#!/bin/bash
for a in "$@"; do
  case "$a" in
    -O) exit 0 ;;
    ss) echo "LISTEN 0 128 127.0.0.1:48800 0.0.0.0:*"; exit 0 ;;
  esac
done
for a in "$@"; do
  if [ "$a" = "-N" ]; then exec sleep 300; fi
done
exit 0
"""


def _stub_path(work: Path) -> str:
    bin_dir = work / "stub-bin"
    bin_dir.mkdir(parents=True, exist_ok=True)
    ssh = bin_dir / "ssh"
    ssh.write_text(_SSH_STUB, encoding="utf-8")
    ssh.chmod(0o755)
    return f"{bin_dir}{os.pathsep}{os.environ.get('PATH', '')}"


def _norm_pid(text: str) -> str:
    return re.sub(r'"pid":\d+', '"pid":0', text)


def _proc_status(pid: int) -> tuple[int, str] | None:
    """讀取 Linux 的行程建立序號，避免清理時誤碰重用的 PID。"""
    try:
        stat = Path(f"/proc/{pid}/stat").read_text(encoding="ascii")
    except OSError:
        return None
    right_paren = stat.rfind(")")
    fields = stat[right_paren + 2 :].split()
    try:
        return int(fields[19]), fields[0]
    except (IndexError, ValueError):
        return None


def test_remote_ab_status_close_empty(tmp_path: Path) -> None:
    results: dict[str, tuple[int, str, int, str]] = {}
    for label, prefix in (("python", python_baseline_prefix()), ("cli", cli_prefix())):
        work = tmp_path / label
        work.mkdir()
        env = isolated_env(work)
        status = subprocess.run(
            prefix + ["remote"], env=env, capture_output=True, text=True, timeout=60
        )
        close = subprocess.run(
            prefix + ["remote", "close"], env=env, capture_output=True, text=True, timeout=60
        )
        results[label] = (status.returncode, status.stdout, close.returncode, close.stdout)

    assert results["cli"] == results["python"]
    assert results["cli"] == (
        0,
        '{"ok":true,"tunnels":[]}\n',
        0,
        '{"closed":[],"ok":true}\n',
    )


def test_remote_ab_open_with_stub_ssh(tmp_path: Path) -> None:
    outcomes: dict[str, tuple[int, str]] = {}
    for label, prefix in (("python", python_baseline_prefix()), ("cli", cli_prefix())):
        work = tmp_path / label
        work.mkdir()
        env = isolated_env(work)
        env["PATH"] = _stub_path(work)
        # 全域 --endpoint 必須在子命令之前（inventory 07 §8.5）；tcp endpoint 讓 forward_src
        # （127.0.0.1:48700）在兩個實作間可直接比對，不含隔離目錄路徑。
        argv = prefix + ["--endpoint", "tcp://127.0.0.1:48700", "remote", "-R", "u@relay:48800"]
        try:
            proc = subprocess.run(argv, env=env, capture_output=True, text=True, timeout=60)
            outcomes[label] = (proc.returncode, _norm_pid(proc.stdout))
        finally:
            subprocess.run(
                prefix + ["remote", "close", "all"],
                env=env,
                capture_output=True,
                text=True,
                timeout=60,
            )

    assert outcomes["cli"] == outcomes["python"]
    assert outcomes["cli"][0] == 0
    # 避免兩邊同為失敗而空泛相等：開通後的狀態必須是 active。
    assert '"status":"active"' in outcomes["cli"][1], outcomes["cli"][1]


@pytest.mark.rust_only
@pytest.mark.posix_only
@pytest.mark.process_rpc
def test_remote_sigint_cleans_spawned_tunnel(tmp_path: Path) -> None:
    work = tmp_path / "sigint"
    work.mkdir()
    env = isolated_env(work)
    bin_dir = work / "stub-bin"
    bin_dir.mkdir()
    ssh = bin_dir / "ssh"
    ssh.write_text(
        "#!/bin/sh\n"
        "for arg in \"$@\"; do\n"
        "  if [ \"$arg\" = \"-O\" ]; then exit 255; fi\n"
        "  if [ \"$arg\" = \"-N\" ]; then exec sleep 300; fi\n"
        "done\n"
        "exit 0\n",
        encoding="utf-8",
    )
    ssh.chmod(0o755)
    env["PATH"] = f"{bin_dir}{os.pathsep}{os.environ.get('PATH', '')}"
    command = cli_prefix() + [
        "--endpoint",
        "tcp://127.0.0.1:48700",
        "remote",
        "-R",
        "u@relay:48800",
        "--ready-timeout",
        "20",
    ]
    process = subprocess.Popen(
        command,
        cwd=repo_root(),
        env=env,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )
    tunnel_pid: int | None = None
    tunnel_start_ticks: int | None = None
    state_path = Path(env["SERIALWRAP_RUN_DIR"]) / "remote" / "48800.json"
    try:
        deadline = time.monotonic() + 10
        while time.monotonic() < deadline:
            if state_path.exists():
                state = json.loads(state_path.read_text(encoding="utf-8"))
                tunnel_pid = int(state["pid"])
                tunnel_start_ticks = int(state["pid_start_ticks"])
                if state.get("status") == "spawning":
                    break
            if process.poll() is not None:
                break
            time.sleep(0.02)
        assert tunnel_pid is not None and state_path.exists(), "CLI 未建立 isolated spawning state"

        process.send_signal(signal.SIGINT)
        stdout, stderr = process.communicate(timeout=10)
        assert process.returncode == 130, (process.returncode, stdout, stderr)
        assert not state_path.exists(), "Ctrl+C 後 registry state 必須移除"

        deadline = time.monotonic() + 5
        while time.monotonic() < deadline:
            current = _proc_status(tunnel_pid)
            if (
                current is None
                or current[0] != tunnel_start_ticks
                or current[1] in {"Z", "X", "x"}
            ):
                break
            time.sleep(0.05)
        else:
            raise AssertionError(f"Ctrl+C 後 ssh tunnel 仍存活：pid={tunnel_pid}")
    finally:
        if process.poll() is None:
            process.kill()
            process.wait(timeout=5)
        if tunnel_pid is not None and tunnel_start_ticks is not None:
            current = _proc_status(tunnel_pid)
            if current is not None and current[0] == tunnel_start_ticks:
                try:
                    os.killpg(tunnel_pid, signal.SIGKILL)
                except ProcessLookupError:
                    pass


@pytest.mark.case("bc05-three-topologies")
@pytest.mark.rust_only
@pytest.mark.posix_only
@pytest.mark.process_rpc
def test_remote_topologies_docker(tmp_path: Path, mgmt_bundle) -> None:
    if shutil.which("docker") is None:
        pytest.skip("cap docker：本機沒有 docker 指令")

    env = isolated_env(tmp_path)
    env["SERIALWRAP_CLI_BUNDLE_DIR"] = str(mgmt_bundle.root)
    script = repo_root() / "tools" / "docker" / "remote_tunnel_test.sh"
    # bash -x 把每個 docker exec 的展開命令寫到 stderr，用以核對 serialwrap 的呼叫路徑。
    proc = subprocess.run(
        ["bash", "-x", str(script), "all"],
        env=env,
        capture_output=True,
        text=True,
        timeout=1800,
    )
    assert proc.returncode == 0, (proc.stdout + proc.stderr)[-4000:]

    trace = proc.stderr.splitlines()
    bare_calls = [
        line for line in trace if "docker exec" in line and re.search(r"\sserialwrap(\s|$)", line)
    ]
    assert not bare_calls, bare_calls[:3]
    assert any("/opt/sw-bundle/bin/serialwrap" in line for line in trace)
