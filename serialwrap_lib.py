from __future__ import annotations

import argparse
import dataclasses
import datetime as _dt
import fcntl
import json
import os
import re
import shlex
import subprocess
import sys
import time
import uuid
from typing import Any, Iterable


STATE_DIR = "/tmp/serialwrap"
LOCKS_DIR = "/tmp/serialwrap/locks"
REGISTRY_PATH = "/tmp/serialwrap/registry.json"

B_LOG_DIR = "/home/paul_chen/b-log"

TMUX_BIN = "/usr/bin/tmux"
PS_BIN = "/usr/bin/ps"

SERIALWRAP_VERSION = "0.1.0-dev"

HELP_EPILOG = """\
範例（開發階段）:
  /home/paul_chen/arc_prj/ser-dep/serialwrap attach --list
  /home/paul_chen/arc_prj/ser-dep/serialwrap attach --auto
  /home/paul_chen/arc_prj/ser-dep/serialwrap attach COM0 --tmux-target %2
  /home/paul_chen/arc_prj/ser-dep/serialwrap run COM0 --cmd 'echo true' --timeout 5
  /home/paul_chen/arc_prj/ser-dep/serialwrap read COM0 --max-bytes 65536
  /home/paul_chen/arc_prj/ser-dep/serialwrap detach COM0

狀態檔:
  /tmp/serialwrap/registry.json
  /tmp/serialwrap/locks/COMx.lock

輸出:
  預設輸出為 JSON（stable keys, 便於 AI/程式解析）

常見 error_code:
  MULTI_MINICOM_UNREGISTERED  COM mapping 不唯一，需手動 attach
  LOGIN_REQUIRED              console 需要登入（工具不處理密碼）
  LOCKED                      同一 COM 正在執行 run/read
  COM_NOT_FOUND               找不到對應 COM 的 minicom instance
  LOG_NOT_FOUND               log 檔案不存在
  MAX_OUTPUT_EXCEEDED         輸出超過上限（partial=true）
  TIMEOUT                     超時（partial=true）
"""


@dataclasses.dataclass(frozen=True)
class PaneInfo:
    pane_id: str
    tmux_target: str
    pane_tty: str
    current_command: str


@dataclasses.dataclass(frozen=True)
class MinicomInstance:
    pid: int
    tty: str
    argv: str
    pane: PaneInfo
    log_path: str | None
    device: str | None
    baud: int | None
    com: str | None


def _ensure_state_dirs() -> None:
    os.makedirs(STATE_DIR, exist_ok=True)
    os.makedirs(LOCKS_DIR, exist_ok=True)


def _json_dumps_stable(obj: Any) -> str:
    return json.dumps(obj, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def _print_json(obj: Any) -> None:
    sys.stdout.write(_json_dumps_stable(obj))
    sys.stdout.write("\n")


def _run(cmd: list[str], *, timeout_s: float | None = None) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        cmd,
        check=False,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        timeout=timeout_s,
    )


def _now_ts() -> str:
    return _dt.datetime.now(tz=_dt.timezone.utc).strftime("%Y%m%d-%H%M%S")


def _parse_minicom_args(argv_str: str) -> tuple[str | None, str | None, int | None]:
    try:
        argv = shlex.split(argv_str)
    except ValueError:
        argv = argv_str.split()

    log_path: str | None = None
    device: str | None = None
    baud: int | None = None

    idx = 0
    while idx < len(argv):
        token = argv[idx]
        if token == "-C" and idx + 1 < len(argv):
            log_path = argv[idx + 1]
            idx += 2
            continue
        if token.startswith("-C") and token != "-C":
            log_path = token[2:]
            idx += 1
            continue
        if token == "-D" and idx + 1 < len(argv):
            device = argv[idx + 1]
            idx += 2
            continue
        if token.startswith("-D") and token != "-D":
            device = token[2:]
            idx += 1
            continue
        if token == "-b" and idx + 1 < len(argv):
            try:
                baud = int(argv[idx + 1])
            except ValueError:
                baud = None
            idx += 2
            continue
        idx += 1

    return log_path, device, baud


def _derive_com(log_path: str | None, device: str | None) -> str | None:
    if log_path:
        match = re.search(r"mini_COM(\d+)_", os.path.basename(log_path))
        if match:
            return f"COM{match.group(1)}"
    if device:
        match = re.search(r"ttyUSB(\d+)$", device)
        if match:
            return f"COM{match.group(1)}"
    return None


def _list_tmux_panes() -> dict[str, PaneInfo]:
    fmt = "#{pane_id}\t#{session_name}:#{window_index}.#{pane_index}\t#{pane_current_command}\t#{pane_tty}"
    proc = _run([TMUX_BIN, "list-panes", "-a", "-F", fmt])
    if proc.returncode != 0:
        raise RuntimeError(proc.stderr.strip() or "tmux list-panes failed")

    panes: dict[str, PaneInfo] = {}
    for line in proc.stdout.splitlines():
        parts = line.split("\t")
        if len(parts) != 4:
            continue
        pane_id, tmux_target, current_cmd, pane_tty = parts
        panes[pane_tty.strip()] = PaneInfo(
            pane_id=pane_id.strip(),
            tmux_target=tmux_target.strip(),
            current_command=current_cmd.strip(),
            pane_tty=pane_tty.strip(),
        )
    return panes


def _list_minicom_processes() -> list[tuple[int, str, str]]:
    proc = _run([PS_BIN, "-eo", "pid=,tty=,args="])
    if proc.returncode != 0:
        raise RuntimeError(proc.stderr.strip() or "ps failed")

    out: list[tuple[int, str, str]] = []
    for line in proc.stdout.splitlines():
        stripped = line.strip()
        if not stripped:
            continue
        parts = stripped.split(maxsplit=2)
        if len(parts) != 3:
            continue
        pid_str, tty, argv = parts
        if tty == "?":
            continue
        if not re.search(r"\bminicom\b", argv):
            continue
        try:
            pid = int(pid_str)
        except ValueError:
            continue
        out.append((pid, tty, argv))
    return out


def discover_minicom_instances() -> list[MinicomInstance]:
    panes_by_tty = _list_tmux_panes()
    instances: list[MinicomInstance] = []
    for pid, tty, argv in _list_minicom_processes():
        pane_tty = f"/dev/{tty}"
        pane = panes_by_tty.get(pane_tty)
        if pane is None:
            continue
        log_path, device, baud = _parse_minicom_args(argv)
        com = _derive_com(log_path, device)
        instances.append(
            MinicomInstance(
                pid=pid,
                tty=tty,
                argv=argv,
                pane=pane,
                log_path=log_path,
                device=device,
                baud=baud,
                com=com,
            )
        )
    instances.sort(key=lambda x: (x.com or "COM?", x.pane.pane_id))
    return instances


def _load_registry() -> dict[str, Any]:
    _ensure_state_dirs()
    if not os.path.exists(REGISTRY_PATH):
        return {}
    with open(REGISTRY_PATH, "r", encoding="utf-8") as fp:
        try:
            obj = json.load(fp)
        except json.JSONDecodeError:
            return {}
    if not isinstance(obj, dict):
        return {}
    return obj


def _write_registry(registry: dict[str, Any]) -> None:
    _ensure_state_dirs()
    tmp_path = f"{REGISTRY_PATH}.tmp.{os.getpid()}"
    with open(tmp_path, "w", encoding="utf-8") as fp:
        fp.write(_json_dumps_stable(registry))
        fp.write("\n")
    os.replace(tmp_path, REGISTRY_PATH)


def cmd_attach_list(args: argparse.Namespace) -> int:
    instances = discover_minicom_instances()
    payload = {
        "ok": True,
        "instances": [
            {
                "pid": inst.pid,
                "pane_id": inst.pane.pane_id,
                "tmux_target": inst.pane.tmux_target,
                "pane_tty": inst.pane.pane_tty,
                "minicom_argv": inst.argv,
                "log_path": inst.log_path,
                "device": inst.device,
                "baud": inst.baud,
                "derived_com": inst.com,
            }
            for inst in instances
        ],
    }
    _print_json(payload)
    return 0


def _registry_entry_for_instance(
    com: str,
    inst: MinicomInstance,
    *,
    mode: str,
    prompt_regex: str,
) -> dict[str, Any]:
    entry: dict[str, Any] = {
        "com": com,
        "tmux_target": inst.pane.tmux_target,
        "pane_id": inst.pane.pane_id,
        "pane_tty": inst.pane.pane_tty,
        "pid": inst.pid,
        "log_path": inst.log_path,
        "rx_source": "minicom-C" if inst.log_path else None,
        "mode": mode,
        "prompt_regex": prompt_regex,
        "cursor": None,
    }
    return entry


def _make_multi_unregistered_error(instances: list[MinicomInstance]) -> dict[str, Any]:
    return {
        "ok": False,
        "error_code": "MULTI_MINICOM_UNREGISTERED",
        "instances": [
            {
                "pid": inst.pid,
                "pane_id": inst.pane.pane_id,
                "tmux_target": inst.pane.tmux_target,
                "pane_tty": inst.pane.pane_tty,
                "minicom_argv": inst.argv,
                "log_path": inst.log_path,
                "derived_com": inst.com,
            }
            for inst in instances
        ],
        "next_actions": [
            "serialwrap attach COM0 --tmux-target %<pane_id> --mode shell --prompt-regex '<re>'",
        ],
    }


def cmd_attach_auto(args: argparse.Namespace) -> int:
    registry = _load_registry()
    instances = discover_minicom_instances()

    candidates: dict[str, MinicomInstance] = {}
    ambiguous: list[MinicomInstance] = []
    for inst in instances:
        if inst.com is None:
            ambiguous.append(inst)
            continue
        if inst.log_path is None:
            ambiguous.append(inst)
            continue
        if inst.com in registry:
            continue
        if inst.com in candidates:
            ambiguous.append(inst)
            ambiguous.append(candidates[inst.com])
            del candidates[inst.com]
            continue
        candidates[inst.com] = inst

    registered: list[str] = []
    for com, inst in sorted(candidates.items(), key=lambda x: x[0]):
        registry[com] = _registry_entry_for_instance(
            com,
            inst,
            mode=args.mode,
            prompt_regex=args.prompt_regex,
        )
        registered.append(com)
    _write_registry(registry)
    if ambiguous:
        payload = _make_multi_unregistered_error(instances)
        payload["registered"] = registered
        _print_json(payload)
        return 2
    _print_json({"ok": True, "registered": registered})
    return 0


def _resolve_tmux_pane_target(tmux_target: str) -> PaneInfo:
    fmt = "#{pane_id}\t#{session_name}:#{window_index}.#{pane_index}\t#{pane_current_command}\t#{pane_tty}"
    proc = _run([TMUX_BIN, "display-message", "-p", "-t", tmux_target, fmt])
    if proc.returncode != 0:
        raise RuntimeError(proc.stderr.strip() or "tmux display-message failed")

    parts = proc.stdout.strip().split("\t")
    if len(parts) != 4:
        raise RuntimeError("unexpected tmux pane format")
    pane_id, resolved_target, current_cmd, pane_tty = parts
    return PaneInfo(
        pane_id=pane_id.strip(),
        tmux_target=resolved_target.strip(),
        pane_tty=pane_tty.strip(),
        current_command=current_cmd.strip(),
    )


def cmd_attach_com(args: argparse.Namespace) -> int:
    com = args.com
    if not re.fullmatch(r"COM\d+", com):
        _print_json({"ok": False, "error_code": "INVALID_COM", "com": com})
        return 2

    pane = _resolve_tmux_pane_target(args.tmux_target)
    instances = discover_minicom_instances()
    inst: MinicomInstance | None = None
    for candidate in instances:
        if candidate.pane.pane_tty == pane.pane_tty:
            inst = candidate
            break
    if inst is None:
        _print_json(
            {
                "ok": False,
                "error_code": "MINICOM_NOT_FOUND_IN_PANE",
                "tmux_target": pane.tmux_target,
                "pane_tty": pane.pane_tty,
            }
        )
        return 2

    if inst.log_path is None:
        _print_json(
            {
                "ok": False,
                "error_code": "LOG_PATH_NOT_FOUND",
                "tmux_target": inst.pane.tmux_target,
                "pane_tty": inst.pane.pane_tty,
                "pid": inst.pid,
                "minicom_argv": inst.argv,
            }
        )
        return 2

    registry = _load_registry()
    registry[com] = _registry_entry_for_instance(
        com,
        inst,
        mode=args.mode,
        prompt_regex=args.prompt_regex,
    )
    _write_registry(registry)
    _print_json({"ok": True, "registered": [com], "log_path": inst.log_path, "tmux_target": inst.pane.tmux_target})
    return 0


def cmd_list(args: argparse.Namespace) -> int:
    registry = _load_registry()
    payload = {"ok": True, "registry": registry}
    _print_json(payload)
    return 0


def _strip_ansi(text: str) -> str:
    text = re.sub(r"\x1B\[[0-9;?]*[A-Za-z]", "", text)
    text = re.sub(r"\x1B\][^\x07]*(\x07|\x1B\\)", "", text)
    return text


def _apply_backspaces(text: str) -> str:
    out: list[str] = []
    for ch in text:
        if ch == "\b" or ch == "\x7f":
            if out:
                out.pop()
            continue
        if ch == "\a":
            continue
        out.append(ch)
    return "".join(out)


def _clean_output(text: str) -> str:
    text = text.replace("\r\n", "\n").replace("\r", "")
    text = _strip_ansi(text)
    text = _apply_backspaces(text)
    return text


def _send_line(tmux_target: str, line: str, *, clear_line: bool) -> None:
    if clear_line:
        proc = _run([TMUX_BIN, "send-keys", "-t", tmux_target, "C-u"])
        if proc.returncode != 0:
            raise RuntimeError(proc.stderr.strip() or "tmux send-keys clear-line failed")
    proc = _run([TMUX_BIN, "send-keys", "-t", tmux_target, "-l", line])
    if proc.returncode != 0:
        raise RuntimeError(proc.stderr.strip() or "tmux send-keys failed")
    proc = _run([TMUX_BIN, "send-keys", "-t", tmux_target, "C-m"])
    if proc.returncode != 0:
        raise RuntimeError(proc.stderr.strip() or "tmux send-keys enter failed")


class _LockTimeout(Exception):
    pass


def _com_lock_path(com: str) -> str:
    return os.path.join(LOCKS_DIR, f"{com}.lock")


def _acquire_com_lock(com: str, *, timeout_s: float) -> int:
    _ensure_state_dirs()
    fd = os.open(_com_lock_path(com), os.O_RDWR | os.O_CREAT, 0o600)
    t0 = time.monotonic()
    while True:
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
            return fd
        except BlockingIOError:
            if time.monotonic() - t0 >= timeout_s:
                os.close(fd)
                raise _LockTimeout()
            time.sleep(0.05)


def _release_com_lock(fd: int) -> None:
    try:
        fcntl.flock(fd, fcntl.LOCK_UN)
    finally:
        os.close(fd)


def _cursor_from_file(path: str) -> dict[str, Any]:
    st = os.stat(path)
    return {"inode": st.st_ino, "offset": st.st_size}


def _read_since(path: str, offset: int, *, max_bytes: int) -> tuple[str, int]:
    with open(path, "rb") as fp:
        fp.seek(offset)
        data = fp.read(max_bytes)
        new_offset = fp.tell()
    return data.decode("utf-8", errors="replace"), new_offset


def _detect_login_prompt(text: str) -> bool:
    if re.search(r"(?mi)^login:\s*$", text):
        return True
    if re.search(r"(?mi)^password:\s*$", text):
        return True
    return False


def _normalize_newlines(text: str) -> str:
    return text.replace("\r\n", "\n").replace("\r", "\n")


def _find_marker_after_line(text: str, marker: str) -> int | None:
    idx = text.find(marker)
    if idx < 0:
        return None
    next_nl = text.find("\n", idx)
    if next_nl < 0:
        return len(text)
    return next_nl + 1


def _find_marker_line_start(text: str, marker: str) -> int | None:
    idx = text.find(marker)
    if idx < 0:
        return None
    line_start = text.rfind("\n", 0, idx)
    return 0 if line_start < 0 else line_start + 1


def _marker_line_pattern(marker: str) -> re.Pattern[str]:
    return re.compile(rf"(?:^|\n){re.escape(marker)}\s*(?:\n|$)")


def _find_marker_output_line_end(text: str, marker: str) -> int | None:
    match = _marker_line_pattern(marker).search(text)
    if match is None:
        return None
    return match.end()


def _find_marker_output_line_start(text: str, marker: str) -> int | None:
    match = _marker_line_pattern(marker).search(text)
    if match is None:
        return None
    if match.start() == 0:
        return 0
    if text[match.start()] == "\n":
        return match.start() + 1
    return match.start()


def _strip_shell_noise(text: str, *, cmd: str, begin_marker: str, end_marker: str) -> str:
    lines = text.splitlines()
    out: list[str] = []
    removed_first_cmd_echo = False
    for line in lines:
        if begin_marker in line or end_marker in line:
            continue
        if not removed_first_cmd_echo and cmd and cmd in line:
            removed_first_cmd_echo = True
            continue
        out.append(line)
    return "\n".join(out) + ("\n" if text.endswith("\n") else "")


def _resolve_com_entry(registry: dict[str, Any], com: str, *, mode: str, prompt_regex: str) -> tuple[dict[str, Any] | None, dict[str, Any] | None]:
    entry = registry.get(com)
    if isinstance(entry, dict):
        tmux_target = entry.get("tmux_target")
        log_path = entry.get("log_path")
        if isinstance(tmux_target, str) and isinstance(log_path, str) and os.path.exists(log_path):
            return entry, None

    instances = discover_minicom_instances()
    matches = [inst for inst in instances if inst.com == com]
    if len(matches) == 1 and matches[0].log_path:
        new_entry = _registry_entry_for_instance(com, matches[0], mode=mode, prompt_regex=prompt_regex)
        registry[com] = new_entry
        _write_registry(registry)
        return new_entry, None
    if len(matches) == 0:
        return None, {"ok": False, "error_code": "COM_NOT_FOUND", "com": com}

    err = _make_multi_unregistered_error(matches)
    err["com"] = com
    return None, err


def cmd_run(args: argparse.Namespace) -> int:
    _ensure_state_dirs()
    registry = _load_registry()
    com = args.com
    entry, err = _resolve_com_entry(registry, com, mode="shell", prompt_regex=r".*# $")
    if err is not None:
        _print_json(err)
        return 2
    if entry is None:
        _print_json({"ok": False, "error_code": "REGISTRY_INVALID", "com": com})
        return 2

    tmux_target = entry.get("tmux_target")
    log_path = entry.get("log_path")
    mode = entry.get("mode", "shell")
    prompt_regex = entry.get("prompt_regex", r".*# $")
    if not isinstance(tmux_target, str) or not isinstance(log_path, str):
        _print_json({"ok": False, "error_code": "REGISTRY_INVALID", "com": com})
        return 2

    if not os.path.exists(log_path):
        _print_json({"ok": False, "error_code": "LOG_NOT_FOUND", "com": com, "log_path": log_path})
        return 2

    run_id = uuid.uuid4().hex
    begin_marker = f"__SERIALWRAP_BEGIN__{run_id}"
    end_marker = f"__SERIALWRAP_END__{run_id}"

    start_cursor = _cursor_from_file(log_path)
    start_offset = int(start_cursor["offset"])

    lock_fd: int | None = None
    try:
        lock_fd = _acquire_com_lock(com, timeout_s=args.lock_timeout_s)
    except _LockTimeout:
        _print_json({"ok": False, "error_code": "LOCKED", "com": com})
        return 2

    t0 = time.monotonic()
    try:
        if mode == "shell":
            _send_line(tmux_target, f"echo {begin_marker}; {args.cmd}; echo {end_marker}", clear_line=True)
        else:
            _send_line(tmux_target, args.cmd, clear_line=False)

        deadline = time.monotonic() + args.timeout_s
        scan_buf = ""
        capture = ""
        offset = start_offset
        found_begin = False
        last_data_at = time.monotonic()
        prompt_seen_at: float | None = None

        while time.monotonic() < deadline:
            chunk, new_offset = _read_since(log_path, offset, max_bytes=args.poll_max_bytes)
            if new_offset != offset:
                offset = new_offset
                last_data_at = time.monotonic()
                chunk = _normalize_newlines(chunk)

                if mode == "shell":
                    if not found_begin:
                        scan_buf += chunk
                        if len(scan_buf) > args.max_scan_chars:
                            scan_buf = scan_buf[-args.max_scan_chars :]
                        if _detect_login_prompt(scan_buf):
                            _print_json(
                                {
                                    "ok": False,
                                    "error_code": "LOGIN_REQUIRED",
                                    "com": com,
                                    "tmux_target": tmux_target,
                                    "log_path": log_path,
                                }
                            )
                            return 2
                        begin_pos = _find_marker_output_line_end(scan_buf, begin_marker)
                        if begin_pos is not None:
                            found_begin = True
                            capture = scan_buf[begin_pos:]
                            scan_buf = ""
                    else:
                        capture += chunk

                    if found_begin:
                        if _detect_login_prompt(capture):
                            _print_json(
                                {
                                    "ok": False,
                                    "error_code": "LOGIN_REQUIRED",
                                    "com": com,
                                    "tmux_target": tmux_target,
                                    "log_path": log_path,
                                }
                            )
                            return 2
                        end_pos = _find_marker_output_line_start(capture, end_marker)
                        if end_pos is not None:
                            content = capture[:end_pos]
                            content = _strip_shell_noise(content, cmd=args.cmd, begin_marker=begin_marker, end_marker=end_marker)
                            stdout = _clean_output(content).strip("\n")
                            duration_ms = int((time.monotonic() - t0) * 1000)
                            entry["cursor"] = {"inode": start_cursor["inode"], "offset": offset}
                            registry[com] = entry
                            _write_registry(registry)
                            _print_json(
                                {
                                    "ok": True,
                                    "com": com,
                                    "stdout": stdout,
                                    "log": log_path,
                                    "start_offset": start_offset,
                                    "end_offset": offset,
                                    "duration_ms": duration_ms,
                                    "partial": False,
                                }
                            )
                            return 0
                        if len(capture) > args.max_output_chars:
                            stdout = _clean_output(capture[-args.max_output_chars :]).strip("\n")
                            duration_ms = int((time.monotonic() - t0) * 1000)
                            _print_json(
                                {
                                    "ok": False,
                                    "error_code": "MAX_OUTPUT_EXCEEDED",
                                    "com": com,
                                    "stdout": stdout,
                                    "log": log_path,
                                    "start_offset": start_offset,
                                    "end_offset": offset,
                                    "duration_ms": duration_ms,
                                    "partial": True,
                                }
                            )
                            return 2
                else:
                    capture += chunk
                    if _detect_login_prompt(capture):
                        _print_json(
                            {
                                "ok": False,
                                "error_code": "LOGIN_REQUIRED",
                                "com": com,
                                "tmux_target": tmux_target,
                                "log_path": log_path,
                            }
                        )
                        return 2
                    if len(capture) > args.max_output_chars:
                        stdout = _clean_output(capture[-args.max_output_chars :]).strip("\n")
                        duration_ms = int((time.monotonic() - t0) * 1000)
                        _print_json(
                            {
                                "ok": False,
                                "error_code": "MAX_OUTPUT_EXCEEDED",
                                "com": com,
                                "stdout": stdout,
                                "log": log_path,
                                "start_offset": start_offset,
                                "end_offset": offset,
                                "duration_ms": duration_ms,
                                "partial": True,
                            }
                        )
                        return 2
                    if re.search(prompt_regex, capture):
                        prompt_seen_at = time.monotonic()

            if mode != "shell" and prompt_seen_at is not None:
                if time.monotonic() - last_data_at >= args.idle_timeout_s:
                    stdout = _clean_output(capture).strip("\n")
                    duration_ms = int((time.monotonic() - t0) * 1000)
                    entry["cursor"] = {"inode": start_cursor["inode"], "offset": offset}
                    registry[com] = entry
                    _write_registry(registry)
                    _print_json(
                        {
                            "ok": True,
                            "com": com,
                            "stdout": stdout,
                            "log": log_path,
                            "start_offset": start_offset,
                            "end_offset": offset,
                            "duration_ms": duration_ms,
                            "partial": False,
                        }
                    )
                    return 0

            time.sleep(args.poll_interval_s)

        duration_ms = int((time.monotonic() - t0) * 1000)
        stdout = _clean_output(capture or scan_buf).strip("\n")
        _print_json(
            {
                "ok": False,
                "error_code": "TIMEOUT",
                "com": com,
                "stdout": stdout,
                "log": log_path,
                "start_offset": start_offset,
                "end_offset": offset,
                "duration_ms": duration_ms,
                "partial": True,
            }
        )
        return 2
    finally:
        if lock_fd is not None:
            _release_com_lock(lock_fd)


def cmd_read(args: argparse.Namespace) -> int:
    _ensure_state_dirs()
    registry = _load_registry()
    com = args.com
    entry, err = _resolve_com_entry(registry, com, mode="shell", prompt_regex=r".*# $")
    if err is not None:
        _print_json(err)
        return 2
    if entry is None:
        _print_json({"ok": False, "error_code": "REGISTRY_INVALID", "com": com})
        return 2

    lock_fd: int | None = None
    try:
        lock_fd = _acquire_com_lock(com, timeout_s=args.lock_timeout_s)
    except _LockTimeout:
        _print_json({"ok": False, "error_code": "LOCKED", "com": com})
        return 2

    try:
        log_path = entry.get("log_path")
        if not isinstance(log_path, str):
            _print_json({"ok": False, "error_code": "REGISTRY_INVALID", "com": com})
            return 2

        cursor = entry.get("cursor")
        offset = 0
        if isinstance(cursor, dict) and isinstance(cursor.get("offset"), int):
            offset = int(cursor["offset"])
        if not os.path.exists(log_path):
            _print_json({"ok": False, "error_code": "LOG_NOT_FOUND", "com": com, "log_path": log_path})
            return 2

        text, new_offset = _read_since(log_path, offset, max_bytes=args.max_bytes)
        stdout = _clean_output(text)
        entry["cursor"] = {"inode": os.stat(log_path).st_ino, "offset": new_offset}
        registry[com] = entry
        _write_registry(registry)
        _print_json({"ok": True, "com": com, "stdout": stdout, "log": log_path, "start_offset": offset, "end_offset": new_offset})
        return 0
    finally:
        if lock_fd is not None:
            _release_com_lock(lock_fd)


def cmd_detach(args: argparse.Namespace) -> int:
    registry = _load_registry()
    com = args.com
    if com in registry:
        del registry[com]
        _write_registry(registry)
    _print_json({"ok": True, "detached": com})
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="serialwrap",
        description="Operate minicom consoles via tmux and read back outputs from b-log.",
        epilog=HELP_EPILOG,
        formatter_class=argparse.RawTextHelpFormatter,
    )
    parser.add_argument("--version", action="version", version=f"%(prog)s {SERIALWRAP_VERSION}")

    sub = parser.add_subparsers(dest="cmd", required=True)

    p_attach = sub.add_parser("attach", help="Register an existing minicom console")
    p_attach.add_argument("--list", dest="attach_list", action="store_true", help="List running minicom instances (no registry change)")
    p_attach.add_argument("--auto", dest="attach_auto", action="store_true", help="Auto-attach all unambiguous minicom instances")
    p_attach.add_argument("com", nargs="?", help="COMx to attach (e.g. COM0)")
    p_attach.add_argument("--tmux-target", help="tmux target (%pane_id or session:win.pane)")
    p_attach.add_argument("--mode", default="shell", choices=["shell", "raw"])
    p_attach.add_argument("--prompt-regex", dest="prompt_regex", default=r".*# $")
    p_attach.set_defaults(func=cmd_attach)

    p_list = sub.add_parser("list", help="Show registry")
    p_list.set_defaults(func=cmd_list)

    p_run = sub.add_parser("run", help="Run a command on COMx and read its output")
    p_run.add_argument("com")
    p_run.add_argument("--cmd", required=True)
    p_run.add_argument("--timeout", dest="timeout_s", type=float, default=5.0)
    p_run.add_argument("--lock-timeout", dest="lock_timeout_s", type=float, default=2.0)
    p_run.add_argument("--idle-timeout", dest="idle_timeout_s", type=float, default=0.3)
    p_run.add_argument("--poll-interval", dest="poll_interval_s", type=float, default=0.05)
    p_run.add_argument("--poll-max-bytes", dest="poll_max_bytes", type=int, default=65536)
    p_run.add_argument("--max-scan-chars", dest="max_scan_chars", type=int, default=8192)
    p_run.add_argument("--max-output-chars", dest="max_output_chars", type=int, default=262144)
    p_run.set_defaults(func=cmd_run)

    p_read = sub.add_parser("read", help="Read new log output since last cursor")
    p_read.add_argument("com")
    p_read.add_argument("--max-bytes", type=int, default=65536)
    p_read.add_argument("--lock-timeout", dest="lock_timeout_s", type=float, default=2.0)
    p_read.set_defaults(func=cmd_read)

    p_detach = sub.add_parser("detach", help="Detach a COMx from registry")
    p_detach.add_argument("com")
    p_detach.set_defaults(func=cmd_detach)

    return parser


def cmd_attach(args: argparse.Namespace) -> int:
    if args.attach_list:
        if args.attach_auto or args.com is not None or args.tmux_target is not None:
            _print_json({"ok": False, "error_code": "INVALID_ARGS"})
            return 2
        return cmd_attach_list(args)

    if args.attach_auto:
        if args.com is not None or args.tmux_target is not None:
            _print_json({"ok": False, "error_code": "INVALID_ARGS"})
            return 2
        return cmd_attach_auto(args)

    if args.com is None:
        _print_json({"ok": False, "error_code": "INVALID_ARGS"})
        return 2
    if args.tmux_target is None:
        _print_json({"ok": False, "error_code": "TMUX_TARGET_REQUIRED"})
        return 2
    return cmd_attach_com(args)


def main(argv: Iterable[str] | None = None) -> int:
    _ensure_state_dirs()
    parser = build_parser()
    ns = parser.parse_args(list(argv) if argv is not None else None)

    func = getattr(ns, "func", None)
    if func is None:
        parser.print_help()
        return 2
    try:
        return int(func(ns))
    except KeyboardInterrupt:
        _print_json({"ok": False, "error_code": "INTERRUPTED"})
        return 130
    except Exception as exc:
        _print_json({"ok": False, "error_code": "EXCEPTION", "message": str(exc)})
        return 2
