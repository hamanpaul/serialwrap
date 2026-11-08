"""P4-T9 `connect`／`benches` 原生（SC-27）黑箱契約。

A/B 案例同時以 Python 基線與受測 CLI 執行並比對輸出：
stub ssh：connect → benches → connect --close → benches 四步輸出 Python／Rust 相同（pid 正規化）。

已知 A/B 差異（本檔未覆蓋此情境，不納入比對）：`benches.yaml` 語法錯誤時兩實作皆回 INTERNAL_ERROR，
但 message 文字不同（Python 為 libyaml 文字，Rust 為 sw-proto yaml11 解析器文字）。來源為 sw-proto 解析器，非 P4-T9 範圍。
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


# PID 1 只等待自己啟動的子行程、從不回收其他孤兒（容器無 init、`unshare --pid` 的情境）。
_NON_REAPING_PID1 = """import json, subprocess, sys
prefix = json.loads(sys.argv[1])
out = []
for args in (["connect", "B1"], ["connect", "B1", "--close"], ["benches"]):
    r = subprocess.run(prefix + args, capture_output=True, text=True, timeout=60)
    out.append([r.returncode, r.stdout])
subprocess.run(prefix + ["connect", "B1", "--close"], capture_output=True, timeout=30)
print(json.dumps(out))
"""


def _unshare_pidns_argv() -> list[str] | None:
    """可用時回傳以非 root 使用者建立獨立 PID／mount namespace 的 argv 前綴；不可用回 None。"""
    argv = ["unshare", "--user", "--map-current-user", "--pid", "--fork", "--mount", "--mount-proc"]
    try:
        probe = subprocess.run(argv + ["true"], capture_output=True, timeout=10)
    except (OSError, subprocess.TimeoutExpired):
        return None
    return argv if probe.returncode == 0 else None


@pytest.mark.case("sc27-windows-connect-benches")
def test_connect_close_with_non_reaping_pid1(tmp_path: Path) -> None:
    """PID 1 不回收孤兒時，`connect --close` 仍須回報已關閉且清除記憶（07 §8.4 `_terminate_pgid` 語意）。

    回歸：Rust PAL 曾在 SIGKILL 後把殘留的 zombie 群組當成「未收斂」而回錯誤，使隧道被標為 error、
    `closed` 為空；Python 基線於 SIGKILL＋reap 後即視為完成。兩實作輸出須相同。
    """
    unshare = _unshare_pidns_argv()
    if unshare is None:
        pytest.skip("此宿主無法以非特權 unshare 建立 PID namespace（環境能力不足，非 PASS）")
    results: dict[str, Any] = {}
    for label, prefix in (("python", python_baseline_prefix()), ("cli", cli_prefix())):
        work = tmp_path / label
        work.mkdir(parents=True, exist_ok=True)
        env = isolated_env(work)
        env["PATH"] = _stub_path(work)
        cfg_dir = work / "config"
        cfg_dir.mkdir(parents=True, exist_ok=True)
        (cfg_dir / "benches.yaml").write_text(_BENCHES_YAML, encoding="utf-8")
        env["SERIALWRAP_CONFIG_DIR"] = str(cfg_dir)
        driver = work / "pid1.py"
        driver.write_text(_NON_REAPING_PID1, encoding="utf-8")
        proc = subprocess.run(
            unshare + ["python3", "-I", str(driver), json.dumps(prefix)],
            env=env,
            capture_output=True,
            text=True,
            timeout=180,
        )
        assert proc.returncode == 0, proc.stderr
        steps = [(rc, _norm_pid(out)) for rc, out in json.loads(proc.stdout)]
        results[label] = steps

    assert results["cli"] == results["python"]
    connect, close, benches = results["python"]
    assert connect[0] == 0 and '"status":"active"' in connect[1]
    assert close == (0, '{"closed":[49001],"ok":true}\n')
    assert benches[0] == 0 and '"alive":false' in benches[1]
