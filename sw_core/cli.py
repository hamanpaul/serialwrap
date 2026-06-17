from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
import time
from collections.abc import Sequence
from typing import Any

from .client import rpc_call
from .constants import LOCK_PATH, PROFILE_DIR, SOCKET_PATH

_USE_DEFAULT_ENV = object()
LEGACY_DAEMON_ENV_FILE = "~/OPI.env"
PROFILE_DAEMON_ENV_FILE = "OPI.env"


class EnvFileSourceError(RuntimeError):
    def __init__(self, path: str, message: str) -> None:
        super().__init__(message)
        self.path = path


def _print(obj: dict[str, Any]) -> None:
    sys.stdout.write(json.dumps(obj, ensure_ascii=False, sort_keys=True, separators=(",", ":")))
    sys.stdout.write("\n")


def _daemon_script_path() -> str:
    here = os.path.dirname(os.path.abspath(__file__))
    return os.path.normpath(os.path.join(here, "..", "serialwrapd.py"))


def _decode_env_text(raw: bytes) -> str:
    return raw.decode("utf-8", errors="surrogateescape")


def _configured_daemon_env_file() -> str | None:
    raw = os.environ.get("SERIALWRAP_DAEMON_ENV_FILE")
    if raw is None:
        return LEGACY_DAEMON_ENV_FILE
    value = raw.strip()
    return value or None


def _load_daemon_start_env_files(env_files: Sequence[str]) -> tuple[dict[str, str], list[str]]:
    env = dict(os.environ)
    loaded_paths: list[str] = []
    seen_paths: set[str] = set()
    for env_file in env_files:
        path = os.path.expanduser(str(env_file).strip())
        if not path or path in seen_paths:
            continue
        seen_paths.add(path)
        if not os.path.isfile(path):
            continue

        proc = subprocess.run(
            ["bash", "-lc", 'set -a && source "$1" >/dev/null && env -0', "serialwrap", path],
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            check=False,
            env=env,
        )
        if proc.returncode != 0:
            stderr = _decode_env_text(proc.stderr).strip()
            raise EnvFileSourceError(path, stderr or f"failed to source {path}")

        loaded_env: dict[str, str] = {}
        for row in proc.stdout.split(b"\0"):
            if not row:
                continue
            key, sep, value = row.partition(b"=")
            if not sep:
                continue
            loaded_env[_decode_env_text(key)] = _decode_env_text(value)
        env = loaded_env
        loaded_paths.append(path)
    return env, loaded_paths


def _load_daemon_start_env(env_file: str | None | object = _USE_DEFAULT_ENV) -> tuple[dict[str, str], str | None]:
    if env_file is _USE_DEFAULT_ENV:
        env_file = _configured_daemon_env_file()
    if env_file is None:
        return dict(os.environ), None
    env, loaded = _load_daemon_start_env_files([str(env_file)])
    return env, loaded[0] if loaded else None


def _resolve_daemon_start_env_files(profile_dir: str) -> list[str]:
    """解析 daemon 啟動時要載入的 runtime env 檔。

    帳密不再在此階段載入（改為 per-session 解析），
    這裡只處理 runtime 設定（如 SERIALWRAP_WAL_DIR）。
    """
    explicit_env_file = os.environ.get("SERIALWRAP_DAEMON_ENV_FILE")
    if explicit_env_file is not None:
        value = explicit_env_file.strip()
        return [value] if value else []

    env_files: list[str] = []
    fallback = _configured_daemon_env_file()
    if fallback is not None:
        env_files.append(fallback)
    profile_env = os.path.join(profile_dir, PROFILE_DAEMON_ENV_FILE)
    if profile_env not in env_files:
        env_files.append(profile_env)
    return env_files


def _run_daemon_start(args: argparse.Namespace) -> int:
    if getattr(args, "endpoint", None):
        _print({"ok": False, "error_code": "REMOTE_NOT_SUPPORTED", "message": "--endpoint 不支援 daemon start（daemon 只能在本機啟動）"})
        return 2
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
    env_files = _resolve_daemon_start_env_files(args.profile_dir)
    try:
        daemon_env, loaded_env_files = _load_daemon_start_env_files(env_files)
    except EnvFileSourceError as exc:
        payload: dict[str, Any] = {
            "ok": False,
            "error_code": "ENV_FILE_SOURCE_FAILED",
            "env_file": exc.path,
            "message": str(exc),
        }
        if env_files:
            payload["env_files"] = [os.path.expanduser(path) for path in env_files]
        _print(payload)
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
            if loaded_env_files:
                result["env_files"] = loaded_env_files
            health = rpc_call(args.socket, "health.status", {}, timeout_s=1.0)
            warnings = health.get("warnings")
            if warnings:
                result["warnings"] = warnings
            _print(result)
            return 0

    _print({"ok": False, "error_code": "DAEMON_NOT_READY", "pid": proc.pid})
    return 2


def _run_daemon_stop(args: argparse.Namespace) -> int:
    resp = rpc_call(_resolve_endpoint(args), "daemon.stop", {}, timeout_s=2.0)
    if not resp.get("ok"):
        _print(resp)
        return 2
    _print(resp)
    return 0


def _resolve_endpoint(args: argparse.Namespace) -> str:
    """回傳實際連接 endpoint。

    若有 ``--endpoint`` 則優先，否則回 ``--socket`` 值（向後相容）。
    """
    ep = getattr(args, "endpoint", None)
    return ep if ep else args.socket


def _run_rpc(args: argparse.Namespace, method: str, params: dict[str, Any]) -> int:
    resp = rpc_call(_resolve_endpoint(args), method, params, timeout_s=args.timeout_s)
    _print(resp)
    return 0 if resp.get("ok") else 2


def _dispatch_event(args: argparse.Namespace) -> int:
    if args.event_cmd == "add":
        with open(args.file, "r", encoding="utf-8") as f:
            params = json.load(f)
        result = rpc_call(_resolve_endpoint(args), "event.rule_set", params, timeout_s=args.timeout_s)
    elif args.event_cmd == "rm":
        result = rpc_call(_resolve_endpoint(args), "event.rule_delete", {"rule_id": args.rule_id}, timeout_s=args.timeout_s)
    elif args.event_cmd == "list":
        result = rpc_call(
            _resolve_endpoint(args),
            "event.rule_list",
            {"selector": getattr(args, "selector", None), "owner": getattr(args, "owner", None)},
            timeout_s=args.timeout_s,
        )
    elif args.event_cmd == "show":
        result = rpc_call(_resolve_endpoint(args), "event.rule_get", {"rule_id": args.rule_id}, timeout_s=args.timeout_s)
    elif args.event_cmd == "enable":
        result = rpc_call(_resolve_endpoint(args), "event.com_enable", {"selector": args.selector}, timeout_s=args.timeout_s)
    elif args.event_cmd == "disable":
        result = rpc_call(_resolve_endpoint(args), "event.com_disable", {"selector": args.selector}, timeout_s=args.timeout_s)
    elif args.event_cmd == "status":
        result = rpc_call(_resolve_endpoint(args), "event.com_status", {"selector": getattr(args, "selector", None)}, timeout_s=args.timeout_s)
    elif args.event_cmd == "reset":
        result = rpc_call(
            _resolve_endpoint(args),
            "event.reset",
            {"rule_id": getattr(args, "rule_id", None), "selector": getattr(args, "selector", None)},
            timeout_s=args.timeout_s,
        )
    elif args.event_cmd == "reload":
        result = rpc_call(_resolve_endpoint(args), "event.reload", {}, timeout_s=args.timeout_s)
    elif args.event_cmd == "tail":
        result = rpc_call(
            _resolve_endpoint(args),
            "event.tail",
            {
                "rule_id": getattr(args, "rule_id", None),
                "selector": getattr(args, "selector", None),
                "n": args.n,
                "since_ts": getattr(args, "since", None),
            },
            timeout_s=args.timeout_s,
        )
    else:
        _print({"ok": False, "error_code": "UNKNOWN_EVENT_CMD", "cmd": args.event_cmd})
        return 2
    _print(result)
    return 0 if result.get("ok") else 2


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="serialwrap",
        description="serialwrap client（支援本機 Unix socket 與遠端 endpoint）",
        epilog=(
            "examples:\n"
            "  serialwrap session list\n"
            "  serialwrap --endpoint tcp://127.0.0.1:7777 session list\n"
            "  serialwrap --endpoint tcp://127.0.0.1:7777 cmd submit --selector COM0 --cmd 'uname -a'"
        ),
        formatter_class=argparse.RawTextHelpFormatter,
    )
    p.add_argument("--socket", default=SOCKET_PATH, help="本機 daemon 的 Unix socket 路徑（預設: %(default)s）")
    p.add_argument("--endpoint", default=None, metavar="ENDPOINT", help="遠端 daemon endpoint，例如 tcp://127.0.0.1:7777（優先於 --socket）")
    p.add_argument("--timeout", dest="timeout_s", type=float, default=5.0, help="RPC timeout 秒數（預設: %(default)s）")

    sub = p.add_subparsers(
        dest="cmd",
        required=True,
        title="command groups",
        metavar="<group>",
    )

    p_daemon = sub.add_parser(
        "daemon",
        help="管理 serialwrap daemon（啟動／停止／狀態）",
        description="管理 serialwrap daemon 行程：啟動、停止與查詢執行狀態。",
    )
    daemon_sub = p_daemon.add_subparsers(dest="daemon_cmd", required=True, metavar="<command>")

    p_ds = daemon_sub.add_parser("start", help="啟動 daemon（--foreground 可前景執行）")
    p_ds.add_argument("--profile-dir", default=PROFILE_DIR)
    p_ds.add_argument("--lock", default=LOCK_PATH)
    p_ds.add_argument("--foreground", action="store_true")

    daemon_sub.add_parser("stop", help="停止執行中的 daemon")
    daemon_sub.add_parser("status", help="顯示 daemon 狀態（pid／sessions／devices／log 路徑）")

    p_device = sub.add_parser(
        "device",
        help="實體 UART 裝置列舉與 handoff（release／attach）",
        description="管理實體 UART 裝置：列舉裝置，以及把 raw device 暫時交給外部工具獨佔再收回。",
    )
    device_sub = p_device.add_subparsers(dest="device_cmd", required=True, metavar="<command>")
    device_sub.add_parser("list", help="列出實體 UART 裝置（real_path 與 by-id）")
    p_drel = device_sub.add_parser(
        "release",
        help="釋放 raw 裝置給外部工具獨佔（如 MCU 燒錄），進入 RELEASED 不自動搶回",
    )
    p_drel.add_argument("--selector", required=True)
    p_drel.add_argument("--source", default="cli")
    p_drel.add_argument("--reason", default=None)
    p_datt = device_sub.add_parser(
        "attach",
        help="收回先前 release 的裝置並重建 console（外部仍持有時回 DEVICE_STILL_HELD，--force 略過）",
    )
    p_datt.add_argument("--selector", required=True)
    p_datt.add_argument("--force", action="store_true")

    p_session = sub.add_parser(
        "session",
        help="session 生命週期、探測、recover、console 與 interactive 操作",
        description="管理 session：列舉與綁定、健康探測（self-test）、recover、console 與 interactive lease、capture log。",
    )
    sess_sub = p_session.add_subparsers(dest="session_cmd", required=True, metavar="<command>")
    sess_sub.add_parser("list", help="列出所有 session 及其狀態")
    p_sc = sess_sub.add_parser("clear", help="清除 session（detach 後會自動 re-attach；交接外部請改用 device release）")
    p_sc.add_argument("--selector", required=True, help="session_id | COMx | alias")
    p_sb = sess_sub.add_parser("bind", help="把 session 綁定到指定裝置 by-id")
    p_sb.add_argument("--selector", required=True, help="session_id | COMx | alias")
    p_sb.add_argument("--device-by-id", required=True)
    p_sa = sess_sub.add_parser("attach", help="將 session attach 到裝置並建立 bridge")
    p_sa.add_argument("--selector", required=True, help="session_id | COMx | alias")
    p_sst = sess_sub.add_parser("self-test", help="探測 session 健康度，回報 classification 與 recommended_action")
    p_sst.add_argument("--selector", required=True, help="session_id | COMx | alias")
    p_sst.add_argument("--probe-timeout", dest="probe_timeout_s", type=float, default=2.0)
    p_sst.add_argument(
        "--strict-human-lock",
        action="store_true",
        default=False,
        help="嚴格模式：若 human interactive lease 仍在使用中則直接回報 busy；預設模式會先暫停 human interactive lease 再做 probe",
    )
    p_sact = sess_sub.add_parser("activity", help="顯示 session 的 RX／TX／state 活動")
    p_sact.add_argument("--selector", required=True, help="session_id | COMx | alias")
    p_sr = sess_sub.add_parser("recover", help="重建 bridge 修復不健康的 session（TARGET_UNRESPONSIVE 時用這個，非 device attach）")
    p_sr.add_argument("--selector", required=True, help="session_id | COMx | alias")
    p_sr.add_argument("--timeout", dest="recover_timeout_s", type=float, default=2.0)
    p_sr.add_argument("--force", action="store_true", help="force clear+reattach if normal recovery fails")
    p_sca = sess_sub.add_parser("console-attach", help="附加一個 console reader 到 session")
    p_sca.add_argument("--selector", required=True, help="session_id | COMx | alias")
    p_sca.add_argument("--label")
    p_scd = sess_sub.add_parser("console-detach", help="卸除指定的 console reader")
    p_scd.add_argument("--selector", required=True, help="session_id | COMx | alias")
    p_scd.add_argument("--client-id", required=True)
    p_scl = sess_sub.add_parser("console-list", help="列出 session 上的 console readers")
    p_scl.add_argument("--selector", required=True, help="session_id | COMx | alias")
    p_sio = sess_sub.add_parser("interactive-open", help="開啟 interactive lease（給全螢幕互動程式用）")
    p_sio.add_argument("--selector", required=True, help="session_id | COMx | alias")
    p_sio.add_argument("--owner", default="agent")
    p_sio.add_argument("--timeout", dest="interactive_timeout_s", type=float, default=60.0)
    p_sio.add_argument("--command", default="")
    p_sio.add_argument(
        "--allow-attached",
        action="store_true",
        default=False,
        help="允許在 ATTACHED 狀態下開啟 bootloader recovery lease（需通過 bootloader prompt 比對）。"
             " 若 session 已有 human interactive lease 則暫停並在 close 時恢復。",
    )
    p_sis = sess_sub.add_parser("interactive-send", help="送出按鍵／資料到 interactive lease")
    p_sis.add_argument("--interactive-id", required=True)
    p_sis.add_argument("--data", required=True)
    p_sis.add_argument("--encoding", default="plain")
    p_sist = sess_sub.add_parser("interactive-status", help="讀取 interactive lease 目前畫面與狀態")
    p_sist.add_argument("--interactive-id", required=True)
    p_sist.add_argument("--screen-chars", type=int, default=2048)
    p_sic = sess_sub.add_parser("interactive-close", help="關閉 interactive lease")
    p_sic.add_argument("--interactive-id", required=True)
    p_sls = sess_sub.add_parser("log-start", help="開始該 session 的 capture log")
    p_sls.add_argument("--selector", required=True, help="session_id | COMx | alias")
    p_slst = sess_sub.add_parser("log-stop", help="停止該 session 的 capture log")
    p_slst.add_argument("--selector", required=True, help="session_id | COMx | alias")
    p_slstat = sess_sub.add_parser("log-status", help="查詢該 session 的 capture log 狀態")
    p_slstat.add_argument("--selector", required=True, help="session_id | COMx | alias")

    p_alias = sub.add_parser(
        "alias",
        help="session 別名與 by-id 綁定管理",
        description="管理 session 別名：列舉、指定到 session_id、綁定裝置 by-id 與解除綁定。",
    )
    alias_sub = p_alias.add_subparsers(dest="alias_cmd", required=True, metavar="<command>")
    alias_sub.add_parser("list", help="列出所有 alias 綁定")
    p_as = alias_sub.add_parser("set", help="把 alias 指定到既有 session_id")
    p_as.add_argument("--session-id", required=True)
    p_as.add_argument("--alias", required=True)
    p_aa = alias_sub.add_parser("assign", help="把 alias 綁到裝置 by-id（可附 profile）")
    p_aa.add_argument("--by-id", required=True)
    p_aa.add_argument("--alias", required=True)
    p_aa.add_argument("--profile")
    p_au = alias_sub.add_parser("unassign", help="移除 alias 綁定")
    p_au.add_argument("--alias", required=True)

    p_cmd = sub.add_parser(
        "cmd",
        help="提交命令並讀取結果（line／background）",
        description="向 session 提交命令並取回結果：line 模式看 status，background 模式用 result-tail。",
    )
    cmd_sub = p_cmd.add_subparsers(dest="cmd_cmd", required=True, metavar="<command>")
    p_cs = cmd_sub.add_parser("submit", help="提交命令到 session（--mode line|background|interactive）")
    p_cs.add_argument("--selector", required=True)
    p_cs.add_argument("--cmd", dest="command_text", default="")
    p_cs.add_argument("--source", default="agent")
    p_cs.add_argument("--mode", default="line")
    p_cs.add_argument("--priority", type=int, default=10)
    p_cs.add_argument("--cmd-timeout", dest="cmd_timeout_s", type=float, default=10.0)
    p_cs.add_argument("--expected-duration", dest="expected_duration_s", type=float, default=None)
    p_cg = cmd_sub.add_parser("status", help="查詢命令狀態與 stdout（line 模式讀這裡）")
    p_cg.add_argument("--cmd-id", required=True)
    p_cr = cmd_sub.add_parser("result-tail", help="增量讀取 background 命令的結果 chunk")
    p_cr.add_argument("--cmd-id", required=True)
    p_cr.add_argument("--from-chunk", type=int, default=0)
    p_cr.add_argument("--limit", type=int, default=200)
    p_cc = cmd_sub.add_parser("cancel", help="取消執行中的命令")
    p_cc.add_argument("--cmd-id", required=True)

    p_stream = sub.add_parser(
        "stream",
        help="即時 tail 解析後的文字事件串流",
        description="即時 tail session 解析後的文字串流（line 事件）。",
    )
    stream_sub = p_stream.add_subparsers(dest="stream_cmd", required=True, metavar="<command>")
    p_st = stream_sub.add_parser("tail", help="即時 tail 解析後的文字串流")
    p_st.add_argument("--selector")
    p_st.add_argument("--com")
    p_st.add_argument("--from-seq", type=int, default=0)
    p_st.add_argument("--limit", type=int, default=200)

    p_log = sub.add_parser(
        "log",
        help="raw／text 日誌 tail（含 timestamp／seq／crc）",
        description="tail raw 或純文字日誌；raw 含 timestamp／source／seq／crc，可做回放與稽核。",
    )
    log_sub = p_log.add_subparsers(dest="log_cmd", required=True, metavar="<command>")
    p_lr = log_sub.add_parser("tail-raw", help="tail raw 日誌（含 timestamp／source／seq／crc）")
    p_lr.add_argument("--selector")
    p_lr.add_argument("--com")
    p_lr.add_argument("--from-seq", type=int, default=0)
    p_lr.add_argument("--limit", type=int, default=200)
    p_lt = log_sub.add_parser("tail-text", help="tail 純文字日誌")
    p_lt.add_argument("--selector")
    p_lt.add_argument("--com")
    p_lt.add_argument("--from-seq", type=int, default=0)
    p_lt.add_argument("--limit", type=int, default=200)

    p_file = sub.add_parser(
        "file",
        help="透過 UART 推送／拉取檔案",
        description="透過 UART 在本機與 target 之間推送（push）或拉取（pull）檔案。",
    )
    file_sub = p_file.add_subparsers(dest="file_cmd", required=True, metavar="<command>")
    p_fp = file_sub.add_parser("push", help="透過 UART 推送本機檔案到 target")
    p_fp.add_argument("--selector", required=True)
    p_fp.add_argument("--local", required=True)
    p_fp.add_argument("--remote", required=True)
    p_fp.add_argument("--chunk-size", dest="chunk_size", type=int, default=2048)
    p_fp.add_argument("--source", default="agent")
    p_fl = file_sub.add_parser("pull", help="透過 UART 從 target 拉取檔案到本機")
    p_fl.add_argument("--selector", required=True)
    p_fl.add_argument("--remote", required=True)
    p_fl.add_argument("--local", default=None)
    p_fl.add_argument("--source", default="agent")

    p_wal = sub.add_parser(
        "wal",
        help="write-ahead log 匯出／重設／seq 查詢",
        description="操作 write-ahead log（WAL）：匯出區段、重設與查詢目前 seq。",
    )
    wal_sub = p_wal.add_subparsers(dest="wal_cmd", required=True, metavar="<command>")
    p_we = wal_sub.add_parser("export", help="匯出 WAL 區段（--from-seq／--to-seq／--limit）")
    p_we.add_argument("--from-seq", type=int, default=0)
    p_we.add_argument("--to-seq", type=int, default=0)
    p_we.add_argument("--limit", type=int, default=1000)
    wal_sub.add_parser("reset", help="重設 WAL")
    wal_sub.add_parser("current-seq", help="顯示目前 WAL seq")

    p_event = sub.add_parser(
        "event",
        help="event-trigger 規則註冊與 matcher 控制",
        description="event-trigger 規則註冊表與 matcher 控制：新增／刪除／列舉／啟用停用規則與檢視觸發紀錄。",
    )
    e_sub = p_event.add_subparsers(dest="event_cmd", required=True, metavar="<command>")

    e_add = e_sub.add_parser("add", help="從 JSON 檔註冊或更新一條規則")
    e_add.add_argument("--file", required=True)

    e_rm = e_sub.add_parser("rm", help="依 id 刪除一條規則")
    e_rm.add_argument("rule_id")

    e_list = e_sub.add_parser("list", help="列出規則（可依 selector／owner 過濾）")
    e_list.add_argument("--selector")
    e_list.add_argument("--owner")

    e_show = e_sub.add_parser("show", help="依 id 顯示單一規則內容")
    e_show.add_argument("rule_id")

    e_enable = e_sub.add_parser("enable", help="啟用指定 selector 的規則 matcher")
    e_enable.add_argument("--selector", required=True)

    e_disable = e_sub.add_parser("disable", help="停用指定 selector 的規則 matcher")
    e_disable.add_argument("--selector", required=True)

    e_status = e_sub.add_parser("status", help="顯示 matcher 狀態（可依 selector 過濾）")
    e_status.add_argument("--selector")

    e_reset = e_sub.add_parser("reset", help="重設規則計數／狀態（--rule-id 或 --selector 擇一）")
    grp = e_reset.add_mutually_exclusive_group(required=True)
    grp.add_argument("--rule-id")
    grp.add_argument("--selector")

    e_reload = e_sub.add_parser("reload", help="重新載入規則註冊表")

    e_tail = e_sub.add_parser("tail", help="檢視規則觸發紀錄（可依 rule-id／selector 過濾）")
    e_tail.add_argument("--rule-id")
    e_tail.add_argument("--selector")
    e_tail.add_argument("-n", type=int, default=50)
    e_tail.add_argument("--since", type=int)

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

    if args.cmd == "device":
        if args.device_cmd == "list":
            return _run_rpc(args, "device.list", {})
        if args.device_cmd == "release":
            return _run_rpc(args, "device.release", {"selector": args.selector, "source": args.source, "reason": args.reason})
        if args.device_cmd == "attach":
            return _run_rpc(args, "device.attach", {"selector": args.selector, "force": args.force})

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
            return _run_rpc(
                args,
                "session.self_test",
                {
                    "selector": args.selector,
                    "timeout_s": args.probe_timeout_s,
                    "strict_human_lock": args.strict_human_lock,
                },
            )
        if args.session_cmd == "activity":
            return _run_rpc(args, "session.activity", {"selector": args.selector})
        if args.session_cmd == "recover":
            return _run_rpc(args, "session.recover", {"selector": args.selector, "timeout_s": args.recover_timeout_s, "force": getattr(args, "force", False)})
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
                    "allow_attached": args.allow_attached,
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
        if args.session_cmd == "log-start":
            return _run_rpc(args, "session.log_start", {"selector": args.selector})
        if args.session_cmd == "log-stop":
            return _run_rpc(args, "session.log_stop", {"selector": args.selector})
        if args.session_cmd == "log-status":
            return _run_rpc(args, "session.log_status", {"selector": args.selector})

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
            submit_params: dict[str, Any] = {
                "selector": args.selector,
                "cmd": args.command_text,
                "source": args.source,
                "mode": args.mode,
                "priority": args.priority,
                "timeout_s": args.cmd_timeout_s,
            }
            if args.expected_duration_s is not None:
                submit_params["expected_duration_s"] = args.expected_duration_s
            return _run_rpc(args, "command.submit", submit_params)
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

    if args.cmd == "file":
        if args.file_cmd == "push":
            return _run_rpc(
                args,
                "file.push",
                {
                    "selector": args.selector,
                    "local_path": args.local,
                    "remote_path": args.remote,
                    "chunk_size": args.chunk_size,
                    "source": args.source,
                },
            )
        if args.file_cmd == "pull":
            p: dict[str, Any] = {
                "selector": args.selector,
                "remote_path": args.remote,
                "source": args.source,
            }
            if args.local is not None:
                p["local_path"] = args.local
            return _run_rpc(args, "file.pull", p)

    if args.cmd == "wal" and args.wal_cmd == "export":
        return _run_rpc(args, "wal.range", {"from_seq": args.from_seq, "to_seq": args.to_seq, "limit": args.limit})

    if args.cmd == "wal" and args.wal_cmd == "reset":
        return _run_rpc(args, "wal.reset", {})

    if args.cmd == "wal" and args.wal_cmd == "current-seq":
        return _run_rpc(args, "wal.current_seq", {})

    if args.cmd == "event":
        return _dispatch_event(args)

    _print({"ok": False, "error_code": "INVALID_ARGS"})
    return 2
