"""P4-T9 `connect`／`benches` 原生（SC-27）黑箱契約。

A/B 案例同時以 Python 基線與受測 CLI 執行並比對輸出：
stub ssh：connect → benches → connect --close → benches 四步輸出 Python／Rust 相同（pid 正規化）。
"""
from __future__ import annotations

import json
import os
import re
import subprocess
from pathlib import Path
from typing import Any

import pytest

from bb_harness import (
    cli_prefix,
    isolated_env,
    python_baseline_prefix,
)

pytestmark = [
    pytest.mark.posix_only,
    pytest.mark.process_rpc,
]

_SSH_STUB = """#!/usr/bin/env python3
import sys, socket, select, os, signal
args = sys.argv[1:]
if "-O" in args:
    sys.exit(0)
port = None
for i, a in enumerate(args):
    if a == "-L" and i + 1 < len(args):
        for p in args[i + 1].split(":"):
            if p.isdigit():
                port = int(p)
                break
if port is not None:
    srv = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    srv.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    srv.bind(("127.0.0.1", port))
    srv.listen(5)
    while True:
        r, _, _ = select.select([srv], [], [], 1.0)
        if srv in r:
            conn, _ = srv.accept()
            try:
                conn.recv(1024)
                conn.sendall(b'{"ok":true,"pid":1234}\\n')
            finally:
                conn.close()
else:
    signal.pause()
"""

_BENCHES_YAML = """benches:
  B1:
    target: u@relay
    remote_socket: /run/sw.sock
    local_port: 49001
    ssh_opts: []
    autossh: false
"""


def _stub_path(work: Path) -> str:
    bin_dir = work / "stub-bin"
    bin_dir.mkdir(parents=True, exist_ok=True)
    ssh = bin_dir / "ssh"
    ssh.write_text(_SSH_STUB, encoding="utf-8")
    ssh.chmod(0o755)
    return f"{bin_dir}{os.pathsep}{os.environ.get('PATH', '')}"


def _norm_pid(text: str) -> str:
    """正規化 pid 欄位，避免行程 PID 差異導致比對失敗。"""
    return re.sub(r'"pid":\d+', '"pid":0', text)


@pytest.mark.case("sc27-windows-connect-benches")
def test_connect_benches_ab(tmp_path: Path) -> None:
    """stub ssh：connect → benches → connect --close → benches 四步輸出 Python／Rust 相同（pid 正規化）。"""
    steps: dict[str, list[tuple[int, str]]] = {}

    for label, prefix in (("python", python_baseline_prefix()), ("cli", cli_prefix())):
        work = tmp_path / label
        work.mkdir(parents=True, exist_ok=True)
        env = isolated_env(work)
        env["PATH"] = _stub_path(work)

        cfg_dir = work / "config"
        cfg_dir.mkdir(parents=True, exist_ok=True)
        (cfg_dir / "benches.yaml").write_text(_BENCHES_YAML, encoding="utf-8")
        env["SERIALWRAP_CONFIG_DIR"] = str(cfg_dir)

        label_steps: list[tuple[int, str]] = []

        try:
            # 1. connect B1
            r1 = subprocess.run(
                prefix + ["connect", "B1"],
                env=env,
                capture_output=True,
                text=True,
                timeout=30,
            )
            label_steps.append((r1.returncode, _norm_pid(r1.stdout)))

            # 2. benches
            r2 = subprocess.run(
                prefix + ["benches"],
                env=env,
                capture_output=True,
                text=True,
                timeout=30,
            )
            label_steps.append((r2.returncode, _norm_pid(r2.stdout)))

            # 3. connect B1 --close
            r3 = subprocess.run(
                prefix + ["connect", "B1", "--close"],
                env=env,
                capture_output=True,
                text=True,
                timeout=30,
            )
            label_steps.append((r3.returncode, _norm_pid(r3.stdout)))

            # 4. benches (after close)
            r4 = subprocess.run(
                prefix + ["benches"],
                env=env,
                capture_output=True,
                text=True,
                timeout=30,
            )
            label_steps.append((r4.returncode, _norm_pid(r4.stdout)))

            steps[label] = label_steps
        finally:
            # 確保清理可能殘留的 tunnel
            subprocess.run(
                prefix + ["connect", "B1", "--close"],
                env=env,
                capture_output=True,
                text=True,
                timeout=10,
            )

    # 比對 Python 與受測 CLI 輸出
    assert steps["cli"] == steps["python"]

    # 驗證 Python 基線輸出符合規格預期
    py_steps = steps["python"]
    assert py_steps[0][0] == 0  # connect rc 0
    assert '"status":"active"' in py_steps[0][1]
    assert py_steps[1][0] == 0  # benches rc 0
    assert '"alive":true' in py_steps[1][1]
    assert py_steps[2][0] == 0  # close rc 0
    assert py_steps[3][0] == 0  # benches after close rc 0
    assert '"alive":false' in py_steps[3][1]
