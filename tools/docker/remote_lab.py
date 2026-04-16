#!/usr/bin/env python3
"""Docker 內的 remote-support 實驗室。

Container A 會執行：
1. 啟動 fake target
2. 啟動 serialwrapd（Unix socket）
3. 等待 session READY
4. 用 socat 將 Unix socket 暴露成 TCP port

設計目標是讓另一個 container 直接以
``serialwrap --endpoint tcp://<container-name>:7777 ...`` 存取。
"""
from __future__ import annotations

import argparse
import json
import os
import pathlib
import signal
import subprocess
import sys
import time

ROOT_DIR = pathlib.Path(__file__).resolve().parents[2]
FUNC_TEST_DIR = ROOT_DIR / "func-test"
if str(FUNC_TEST_DIR) not in sys.path:
    sys.path.insert(0, str(FUNC_TEST_DIR))

from lib.daemon_harness import DaemonHarness, HarnessConfig
from lib.fake_target import TargetConfig


def _serialwrap_bin() -> str:
    return str(ROOT_DIR / "serialwrap")


def _json_cmd(argv: list[str], env: dict[str, str], timeout: float = 5.0) -> dict[str, object]:
    proc = subprocess.run(
        argv,
        env=env,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        timeout=timeout,
        check=False,
    )
    stdout = (proc.stdout or "").strip()
    if not stdout:
        return {
            "ok": False,
            "error_code": "EMPTY_STDOUT",
            "returncode": proc.returncode,
            "stderr": (proc.stderr or "").strip(),
        }
    try:
        obj = json.loads(stdout)
    except json.JSONDecodeError:
        return {
            "ok": False,
            "error_code": "INVALID_JSON_STDOUT",
            "stdout": stdout,
            "stderr": (proc.stderr or "").strip(),
            "returncode": proc.returncode,
        }
    if isinstance(obj, dict):
        return obj
    return {"ok": False, "error_code": "INVALID_RESPONSE", "response": obj}


def _wait_ready(harness: DaemonHarness, selector: str, timeout_s: float) -> None:
    deadline = time.monotonic() + timeout_s
    last: dict[str, object] = {}
    while time.monotonic() < deadline:
        last = _json_cmd(
            [_serialwrap_bin(), "--socket", harness.socket_path, "session", "list"],
            env=harness.env,
            timeout=5.0,
        )
        sessions = last.get("sessions") if isinstance(last, dict) else None
        if isinstance(sessions, list):
            for session in sessions:
                if not isinstance(session, dict):
                    continue
                if session.get("com") == selector and session.get("state") == "READY":
                    return
        time.sleep(0.3)
    raise RuntimeError(f"session {selector} did not become READY within {timeout_s}s: {last}")


def _start_socat(socket_path: str, listen_host: str, tcp_port: int) -> subprocess.Popen[str]:
    return subprocess.Popen(
        [
            "socat",
            f"TCP-LISTEN:{tcp_port},bind={listen_host},reuseaddr,fork",
            f"UNIX-CONNECT:{socket_path}",
        ],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        text=True,
    )


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="啟動 Docker remote-support lab")
    parser.add_argument("--listen-host", default=os.environ.get("SERIALWRAP_REMOTE_BIND", "0.0.0.0"))
    parser.add_argument("--tcp-port", type=int, default=int(os.environ.get("SERIALWRAP_REMOTE_PORT", "7777")))
    parser.add_argument("--selector", default="COM0")
    parser.add_argument("--ready-timeout", type=float, default=15.0)
    args = parser.parse_args(argv)

    target_cfg = TargetConfig(
        platform="prpl",
        boot_banner="boot done\r\nroot@prplOS:/# ",
        noise_enabled=False,
        default_response="EXEC:{cmd}\r\nRESULT:{cmd}:OK\r\nroot@prplOS:/# ",
    )
    harness = DaemonHarness(HarnessConfig(target_config=target_cfg, com=args.selector, alias="docker-remote"))
    socat_proc: subprocess.Popen[str] | None = None
    stop = False

    def _handle_stop(_signum: int, _frame: object) -> None:
        nonlocal stop
        stop = True

    signal.signal(signal.SIGTERM, _handle_stop)
    signal.signal(signal.SIGINT, _handle_stop)

    try:
        harness.start()
        _wait_ready(harness, args.selector, args.ready_timeout)
        socat_proc = _start_socat(harness.socket_path, args.listen_host, args.tcp_port)
        endpoint = f"tcp://{args.listen_host}:{args.tcp_port}"
        print(
            json.dumps(
                {
                    "ok": True,
                    "event": "REMOTE_LAB_READY",
                    "selector": args.selector,
                    "socket": harness.socket_path,
                    "endpoint": endpoint,
                    "listen_host": args.listen_host,
                    "tcp_port": args.tcp_port,
                },
                ensure_ascii=False,
                separators=(",", ":"),
            ),
            flush=True,
        )
        while not stop:
            if socat_proc.poll() is not None:
                raise RuntimeError("socat exited unexpectedly")
            time.sleep(0.5)
    finally:
        if socat_proc is not None and socat_proc.poll() is None:
            socat_proc.terminate()
            try:
                socat_proc.wait(timeout=3.0)
            except subprocess.TimeoutExpired:
                socat_proc.kill()
        harness.stop()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
