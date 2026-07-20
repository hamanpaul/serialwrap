#!/usr/bin/env python3
"""Docker 內的「uart 端」daemon 啟動器，供 remote_tunnel_test.sh 使用。

與 tools/docker/remote_lab.py 的差異：本程式**不**做 socat unix->tcp 橋接——
`remote_tunnel_test.sh` 驗收的是 `serialwrap remote`（ssh -R/-L）本身如何把
本機 daemon 的 AF_UNIX socket 推到對端，橋接工作交給 `serialwrap remote`，
不能再有第二條（不安全的）0.0.0.0 socat 通道混進來污染斷言。

流程：
1. 啟動 fake target（PTY）+ serialwrapd（AF_UNIX socket），沿用
   func-test/lib/daemon_harness.py 的 DaemonHarness。
2. 等待 session READY。
3. 把 DaemonHarness 解出的 SERIALWRAP_* 目錄寫成 shell-sourceable env 檔
   （預設 /home/tester/sw-uart.env），供同容器之後的 `docker exec ... serialwrap
   remote ...` / `serialwrap daemon status` 呼叫 source 後拿到一致的
   RUN_DIR／STATE_DIR／BY_ID_DIR（DaemonHarness 用 tempfile.TemporaryDirectory()
   產生，事先不可預測，故用檔案傳遞而非固定路徑）。
4. 印一行 JSON（event=UART_HARNESS_READY）到 stdout 供外部（docker logs）確認，
   然後阻塞至收到 SIGTERM/SIGINT，離開前呼叫 harness.stop() 清理。
"""
from __future__ import annotations

import argparse
import json
import pathlib
import shlex
import signal
import subprocess
import sys
import time

ROOT_DIR = pathlib.Path(__file__).resolve().parents[2]
FUNC_TEST_DIR = ROOT_DIR / "func-test"
if str(FUNC_TEST_DIR) not in sys.path:
    sys.path.insert(0, str(FUNC_TEST_DIR))

from lib.daemon_harness import DaemonHarness, HarnessConfig  # noqa: E402
from lib.fake_target import TargetConfig  # noqa: E402

_ENV_KEYS = (
    "SERIALWRAP_STATE_DIR",
    "SERIALWRAP_RUN_DIR",
    "SERIALWRAP_BY_ID_DIR",
    "SERIALWRAP_BY_PATH_DIR",
)


def _serialwrap_bin() -> str:
    return str(ROOT_DIR / "serialwrap")


def _json_cmd(argv: list[str], env: dict[str, str], timeout: float = 5.0) -> dict[str, object]:
    proc = subprocess.run(
        argv, env=env, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
        text=True, timeout=timeout, check=False,
    )
    stdout = (proc.stdout or "").strip()
    if not stdout:
        return {}
    try:
        obj = json.loads(stdout)
    except json.JSONDecodeError:
        return {}
    return obj if isinstance(obj, dict) else {}


def _wait_ready(harness: DaemonHarness, selector: str, timeout_s: float) -> None:
    deadline = time.monotonic() + timeout_s
    last: dict[str, object] = {}
    while time.monotonic() < deadline:
        last = _json_cmd(
            [_serialwrap_bin(), "--socket", harness.socket_path, "session", "list"],
            env=harness.env, timeout=5.0,
        )
        sessions = last.get("sessions") if isinstance(last, dict) else None
        if isinstance(sessions, list):
            for session in sessions:
                if not isinstance(session, dict):
                    continue
                if session.get("com") == selector and session.get("state") == "READY":
                    return
        time.sleep(0.3)
    raise RuntimeError(f"session {selector} 未於 {timeout_s}s 內 READY：{last}")


def _write_env_file(path: str, env: dict[str, str]) -> None:
    lines = [f"export {key}={shlex.quote(env[key])}\n" for key in _ENV_KEYS if key in env]
    with open(path, "w", encoding="utf-8") as fh:
        fh.writelines(lines)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="docker uart 端 fake target + serialwrapd 啟動器")
    parser.add_argument("--selector", default="COM0")
    parser.add_argument("--ready-timeout", type=float, default=20.0)
    parser.add_argument("--env-out", default="/home/tester/sw-uart.env", help="寫出 shell-sourceable env 檔路徑")
    args = parser.parse_args(argv)

    target_cfg = TargetConfig(
        platform="prpl",
        boot_banner="boot done\r\nroot@prplOS:/# ",
        noise_enabled=False,
        default_response="EXEC:{cmd}\r\nRESULT:{cmd}:OK\r\nroot@prplOS:/# ",
    )
    harness = DaemonHarness(HarnessConfig(target_config=target_cfg, com=args.selector, alias="docker-remote-tunnel"))
    stop = False

    def _handle_stop(_signum: int, _frame: object) -> None:
        nonlocal stop
        stop = True

    signal.signal(signal.SIGTERM, _handle_stop)
    signal.signal(signal.SIGINT, _handle_stop)

    harness.start()
    try:
        _wait_ready(harness, args.selector, args.ready_timeout)
        _write_env_file(args.env_out, harness.env)
        print(
            json.dumps(
                {
                    "ok": True,
                    "event": "UART_HARNESS_READY",
                    "selector": args.selector,
                    "socket": harness.socket_path,
                    "env_out": args.env_out,
                },
                ensure_ascii=False, separators=(",", ":"),
            ),
            flush=True,
        )
        while not stop:
            time.sleep(0.5)
    finally:
        harness.stop()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
