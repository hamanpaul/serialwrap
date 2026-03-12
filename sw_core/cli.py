from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
import time
from typing import Any

from .client import rpc_call
from .constants import LOCK_PATH, PROFILE_DIR, SOCKET_PATH

DEFAULT_DAEMON_ENV_FILE = os.environ.get("SERIALWRAP_DAEMON_ENV_FILE", "~/OPI.env")


def _print(obj: dict[str, Any]) -> None:
    sys.stdout.write(json.dumps(obj, ensure_ascii=False, sort_keys=True, separators=(",", ":")))
    sys.stdout.write("\n")


def _daemon_script_path() -> str:
    here = os.path.dirname(os.path.abspath(__file__))
    return os.path.normpath(os.path.join(here, "..", "serialwrapd.py"))


def _decode_env_text(raw: bytes) -> str:
    return raw.decode("utf-8", errors="surrogateescape")


def _load_daemon_start_env(env_file: str | None = DEFAULT_DAEMON_ENV_FILE) -> tuple[dict[str, str], str | None]:
    env = dict(os.environ)
    if env_file is None:
        return env, None

    path = os.path.expanduser(env_file).strip()
    if not path or not os.path.isfile(path):
        return env, None

    proc = subprocess.run(
        ["bash", "-lc", 'set -a && source "$1" >/dev/null && env -0', "serialwrap", path],
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        check=False,
    )
    if proc.returncode != 0:
        stderr = _decode_env_text(proc.stderr).strip()
        raise RuntimeError(stderr or f"failed to source {path}")

    loaded_env: dict[str, str] = {}
    for row in proc.stdout.split(b"\0"):
        if not row:
            continue
        key, sep, value = row.partition(b"=")
        if not sep:
            continue
        loaded_env[_decode_env_text(key)] = _decode_env_text(value)
    return loaded_env, path


def _run_daemon_start(args: argparse.Namespace) -> int:
    cmd = [
        sys.executable,
        _daemon_script_path(),
        "--profile-dir",
        args.profile_dir,
        "--socket",
        args.socket,
        "--lock",
        args.lock,
    ]
    try:
        daemon_env, _ = _load_daemon_start_env()
    except RuntimeError as exc:
        _print(
            {
                "ok": False,
                "error_code": "ENV_FILE_SOURCE_FAILED",
                "env_file": os.path.expanduser(DEFAULT_DAEMON_ENV_FILE),
                "message": str(exc),
            }
        )
        return 2

    if args.foreground:
        return subprocess.call(cmd, env=daemon_env)

    proc = subprocess.Popen(cmd, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, start_new_session=True, env=daemon_env)

    # 等待 daemon 就緒（最多 3 秒）
    for attempt in range(15):
        time.sleep(0.2)
        if proc.poll() is not None:
            _print({"ok": False, "error_code": "DAEMON_EXITED", "pid": proc.pid, "returncode": proc.returncode})
            return 2
        resp = rpc_call(args.socket, "health.ping", {}, timeout_s=0.5)
        if resp.get("ok"):
            result: dict[str, Any] = {"ok": True, "pid": proc.pid, "socket": args.socket}
            health = rpc_call(args.socket, "health.status", {}, timeout_s=1.0)
            warnings = health.get("warnings")
            if warnings:
                result["warnings"] = warnings
            _print(result)
            return 0

    _print({"ok": False, "error_code": "DAEMON_NOT_READY", "pid": proc.pid})
    return 2


def _run_daemon_stop(args: argparse.Namespace) -> int:
    resp = rpc_call(args.socket, "daemon.stop", {}, timeout_s=2.0)
    if not resp.get("ok"):
        _print(resp)
        return 2
    _print(resp)
    return 0


def _run_rpc(args: argparse.Namespace, method: str, params: dict[str, Any]) -> int:
    resp = rpc_call(args.socket, method, params, timeout_s=args.timeout_s)
    _print(resp)
    return 0 if resp.get("ok") else 2


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="serialwrap", description="serialwrap client")
    p.add_argument("--socket", default=SOCKET_PATH)
    p.add_argument("--timeout", dest="timeout_s", type=float, default=5.0)

    sub = p.add_subparsers(dest="cmd", required=True)

    p_daemon = sub.add_parser("daemon")
    daemon_sub = p_daemon.add_subparsers(dest="daemon_cmd", required=True)

    p_ds = daemon_sub.add_parser("start")
    p_ds.add_argument("--profile-dir", default=PROFILE_DIR)
    p_ds.add_argument("--lock", default=LOCK_PATH)
    p_ds.add_argument("--foreground", action="store_true")

    daemon_sub.add_parser("stop")
    daemon_sub.add_parser("status")

    sub.add_parser("device").add_subparsers(dest="device_cmd", required=True).add_parser("list")

    p_session = sub.add_parser("session")
    sess_sub = p_session.add_subparsers(dest="session_cmd", required=True)
    sess_sub.add_parser("list")
    p_sc = sess_sub.add_parser("clear")
    p_sc.add_argument("--selector", required=True, help="session_id | COMx | alias")
    p_sb = sess_sub.add_parser("bind")
    p_sb.add_argument("--selector", required=True, help="session_id | COMx | alias")
    p_sb.add_argument("--device-by-id", required=True)
    p_sa = sess_sub.add_parser("attach")
    p_sa.add_argument("--selector", required=True, help="session_id | COMx | alias")
    p_sst = sess_sub.add_parser("self-test")
    p_sst.add_argument("--selector", required=True, help="session_id | COMx | alias")
    p_sst.add_argument("--probe-timeout", dest="probe_timeout_s", type=float, default=2.0)
    p_sr = sess_sub.add_parser("recover")
    p_sr.add_argument("--selector", required=True, help="session_id | COMx | alias")
    p_sr.add_argument("--timeout", dest="recover_timeout_s", type=float, default=2.0)
    p_sca = sess_sub.add_parser("console-attach")
    p_sca.add_argument("--selector", required=True, help="session_id | COMx | alias")
    p_sca.add_argument("--label")
    p_scd = sess_sub.add_parser("console-detach")
    p_scd.add_argument("--selector", required=True, help="session_id | COMx | alias")
    p_scd.add_argument("--client-id", required=True)
    p_scl = sess_sub.add_parser("console-list")
    p_scl.add_argument("--selector", required=True, help="session_id | COMx | alias")
    p_sio = sess_sub.add_parser("interactive-open")
    p_sio.add_argument("--selector", required=True, help="session_id | COMx | alias")
    p_sio.add_argument("--owner", default="agent")
    p_sio.add_argument("--timeout", dest="interactive_timeout_s", type=float, default=60.0)
    p_sio.add_argument("--command", default="")
    p_sis = sess_sub.add_parser("interactive-send")
    p_sis.add_argument("--interactive-id", required=True)
    p_sis.add_argument("--data", required=True)
    p_sis.add_argument("--encoding", default="plain")
    p_sist = sess_sub.add_parser("interactive-status")
    p_sist.add_argument("--interactive-id", required=True)
    p_sist.add_argument("--screen-chars", type=int, default=2048)
    p_sic = sess_sub.add_parser("interactive-close")
    p_sic.add_argument("--interactive-id", required=True)

    p_alias = sub.add_parser("alias")
    alias_sub = p_alias.add_subparsers(dest="alias_cmd", required=True)
    alias_sub.add_parser("list")
    p_as = alias_sub.add_parser("set")
    p_as.add_argument("--session-id", required=True)
    p_as.add_argument("--alias", required=True)
    p_aa = alias_sub.add_parser("assign")
    p_aa.add_argument("--by-id", required=True)
    p_aa.add_argument("--alias", required=True)
    p_aa.add_argument("--profile")
    p_au = alias_sub.add_parser("unassign")
    p_au.add_argument("--alias", required=True)

    p_cmd = sub.add_parser("cmd")
    cmd_sub = p_cmd.add_subparsers(dest="cmd_cmd", required=True)
    p_cs = cmd_sub.add_parser("submit")
    p_cs.add_argument("--selector", required=True)
    p_cs.add_argument("--cmd", dest="command_text", default="")
    p_cs.add_argument("--source", default="agent")
    p_cs.add_argument("--mode", default="line")
    p_cs.add_argument("--priority", type=int, default=10)
    p_cs.add_argument("--cmd-timeout", dest="cmd_timeout_s", type=float, default=10.0)
    p_cg = cmd_sub.add_parser("status")
    p_cg.add_argument("--cmd-id", required=True)
    p_cr = cmd_sub.add_parser("result-tail")
    p_cr.add_argument("--cmd-id", required=True)
    p_cr.add_argument("--from-chunk", type=int, default=0)
    p_cr.add_argument("--limit", type=int, default=200)
    p_cc = cmd_sub.add_parser("cancel")
    p_cc.add_argument("--cmd-id", required=True)

    p_stream = sub.add_parser("stream")
    stream_sub = p_stream.add_subparsers(dest="stream_cmd", required=True)
    p_st = stream_sub.add_parser("tail")
    p_st.add_argument("--selector")
    p_st.add_argument("--com")
    p_st.add_argument("--from-seq", type=int, default=0)
    p_st.add_argument("--limit", type=int, default=200)

    p_log = sub.add_parser("log")
    log_sub = p_log.add_subparsers(dest="log_cmd", required=True)
    p_lr = log_sub.add_parser("tail-raw")
    p_lr.add_argument("--selector")
    p_lr.add_argument("--com")
    p_lr.add_argument("--from-seq", type=int, default=0)
    p_lr.add_argument("--limit", type=int, default=200)
    p_lt = log_sub.add_parser("tail-text")
    p_lt.add_argument("--selector")
    p_lt.add_argument("--com")
    p_lt.add_argument("--from-seq", type=int, default=0)
    p_lt.add_argument("--limit", type=int, default=200)

    p_wal = sub.add_parser("wal")
    wal_sub = p_wal.add_subparsers(dest="wal_cmd", required=True)
    p_we = wal_sub.add_parser("export")
    p_we.add_argument("--from-seq", type=int, default=0)
    p_we.add_argument("--to-seq", type=int, default=0)
    p_we.add_argument("--limit", type=int, default=1000)

    return p


def main(argv: list[str] | None = None) -> int:
    p = build_parser()
    args = p.parse_args(argv)

    if args.cmd == "daemon":
        if args.daemon_cmd == "start":
            return _run_daemon_start(args)
        if args.daemon_cmd == "stop":
            return _run_daemon_stop(args)
        if args.daemon_cmd == "status":
            return _run_rpc(args, "health.status", {})

    if args.cmd == "device" and args.device_cmd == "list":
        return _run_rpc(args, "device.list", {})

    if args.cmd == "session":
        if args.session_cmd == "list":
            return _run_rpc(args, "session.list", {})
        if args.session_cmd == "clear":
            return _run_rpc(args, "session.clear", {"selector": args.selector})
        if args.session_cmd == "bind":
            return _run_rpc(args, "session.bind", {"selector": args.selector, "device_by_id": args.device_by_id})
        if args.session_cmd == "attach":
            return _run_rpc(args, "session.attach", {"selector": args.selector})
        if args.session_cmd == "self-test":
            return _run_rpc(args, "session.self_test", {"selector": args.selector, "timeout_s": args.probe_timeout_s})
        if args.session_cmd == "recover":
            return _run_rpc(args, "session.recover", {"selector": args.selector, "timeout_s": args.recover_timeout_s})
        if args.session_cmd == "console-attach":
            params: dict[str, Any] = {"selector": args.selector}
            if args.label:
                params["label"] = args.label
            return _run_rpc(args, "session.console_attach", params)
        if args.session_cmd == "console-detach":
            return _run_rpc(args, "session.console_detach", {"selector": args.selector, "client_id": args.client_id})
        if args.session_cmd == "console-list":
            return _run_rpc(args, "session.console_list", {"selector": args.selector})
        if args.session_cmd == "interactive-open":
            return _run_rpc(
                args,
                "session.interactive_open",
                {
                    "selector": args.selector,
                    "owner": args.owner,
                    "timeout_s": args.interactive_timeout_s,
                    "command": args.command,
                },
            )
        if args.session_cmd == "interactive-send":
            return _run_rpc(
                args,
                "session.interactive_send",
                {"interactive_id": args.interactive_id, "data": args.data, "encoding": args.encoding},
            )
        if args.session_cmd == "interactive-status":
            return _run_rpc(
                args,
                "session.interactive_status",
                {"interactive_id": args.interactive_id, "screen_chars": args.screen_chars},
            )
        if args.session_cmd == "interactive-close":
            return _run_rpc(args, "session.interactive_close", {"interactive_id": args.interactive_id})

    if args.cmd == "alias":
        if args.alias_cmd == "list":
            return _run_rpc(args, "alias.list", {})
        if args.alias_cmd == "set":
            return _run_rpc(args, "alias.set", {"session_id": args.session_id, "alias": args.alias})
        if args.alias_cmd == "assign":
            params: dict[str, Any] = {"by_id": args.by_id, "alias": args.alias}
            if args.profile:
                params["profile"] = args.profile
            return _run_rpc(args, "alias.assign", params)
        if args.alias_cmd == "unassign":
            return _run_rpc(args, "alias.unassign", {"alias": args.alias})

    if args.cmd == "cmd":
        if args.cmd_cmd == "submit":
            return _run_rpc(
                args,
                "command.submit",
                {
                    "selector": args.selector,
                    "cmd": args.command_text,
                    "source": args.source,
                    "mode": args.mode,
                    "priority": args.priority,
                    "timeout_s": args.cmd_timeout_s,
                },
            )
        if args.cmd_cmd == "status":
            return _run_rpc(args, "command.get", {"cmd_id": args.cmd_id})
        if args.cmd_cmd == "result-tail":
            return _run_rpc(
                args,
                "command.result_tail",
                {"cmd_id": args.cmd_id, "from_chunk": args.from_chunk, "limit": args.limit},
            )
        if args.cmd_cmd == "cancel":
            return _run_rpc(args, "command.cancel", {"cmd_id": args.cmd_id})

    if args.cmd == "stream" and args.stream_cmd == "tail":
        selector = args.selector or args.com
        params: dict[str, Any] = {"from_seq": args.from_seq, "limit": args.limit}
        if selector:
            params["selector"] = selector
        return _run_rpc(args, "result.tail", params)

    if args.cmd == "log":
        selector = args.selector or args.com
        params = {"from_seq": args.from_seq, "limit": args.limit}
        if selector:
            params["selector"] = selector
        if args.log_cmd == "tail-raw":
            return _run_rpc(args, "log.tail_raw", params)
        if args.log_cmd == "tail-text":
            return _run_rpc(args, "log.tail_text", params)

    if args.cmd == "wal" and args.wal_cmd == "export":
        return _run_rpc(args, "wal.range", {"from_seq": args.from_seq, "to_seq": args.to_seq, "limit": args.limit})

    _print({"ok": False, "error_code": "INVALID_ARGS"})
    return 2
