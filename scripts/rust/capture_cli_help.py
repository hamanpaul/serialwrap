#!/usr/bin/env python3
"""擷取 Python 3.12.3 CLI help／usage／錯誤輸出作為 golden（P3-T10）。

依 plan §2 Task 10 規格：
- 檢查 Python 版本必須恰為 3.12.3，不符即 exit 2。
- 以 subprocess 執行 Python CLI（<repo>/serialwrap）。
- 固定隔離環境變數，其餘 SERIALWRAP_* 全清空。
- 產出各情境 JSON 檔與 _meta.json。
"""
from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
from pathlib import Path

# 嚴格鎖定 Python 3.12.3 方言
if sys.version_info[:3] != (3, 12, 3):
    sys.stderr.write(f"錯誤：Python 直譯器版本 {sys.version.split()[0]} ≠ 3.12.3，無法對齊 argparse 方言\n")
    sys.exit(2)

REPO_ROOT = Path(__file__).resolve().parents[2]
CLI_EXE = REPO_ROOT / "serialwrap"

DELEGATED_GROUPS = {
    "daemon",
    "remote",
    "connect",
    "benches",
    "supervision-mode",
    "service",
    "setup",
    "doctor",
}

ALL_GROUPS = [
    "daemon",
    "device",
    "session",
    "alias",
    "cmd",
    "stream",
    "log",
    "file",
    "wal",
    "mcu",
    "remote",
    "connect",
    "benches",
    "event",
    "supervision-mode",
    "service",
    "setup",
    "doctor",
    "profile",
    "skill",
]

NATIVE_SUBCOMMANDS: dict[str, list[str]] = {
    "device": ["list", "release", "attach"],
    "session": [
        "list",
        "clear",
        "bind",
        "pin",
        "unpin",
        "attach",
        "self-test",
        "activity",
        "recover",
        "console-attach",
        "console-detach",
        "console-list",
        "interactive-open",
        "interactive-send",
        "interactive-status",
        "interactive-close",
        "log-start",
        "log-stop",
        "log-status",
    ],
    "alias": ["list", "set", "assign", "unassign"],
    "cmd": ["submit", "status", "result-tail", "cancel"],
    "stream": ["tail"],
    "log": ["tail-raw", "tail-text"],
    "file": ["push", "pull"],
    "wal": ["export", "reset", "current-seq"],
    "mcu": ["patterns", "status"],
    "event": ["add", "rm", "list", "show", "enable", "disable", "status", "reset", "reload", "tail"],
    "profile": ["test"],
    "skill": [],
}


def is_native(argv: list[str]) -> bool:
    for token in argv:
        if token in DELEGATED_GROUPS:
            return False
        if token.startswith("--endpoint=") and token.split("=", 1)[1] in DELEGATED_GROUPS:
            return False
    return True


def make_env(columns: int) -> dict[str, str]:
    env = {
        "HOME": "/golden/home",
        "LANG": "C.UTF-8",
        "LC_ALL": "C.UTF-8",
        "COLUMNS": str(columns),
        "SERIALWRAP_CONFIG_DIR": "/golden/config",
        "SERIALWRAP_STATE_DIR": "/golden/state",
        "SERIALWRAP_RUN_DIR": "/golden/run",
        "PATH": os.environ.get("PATH", "/usr/bin:/bin"),
    }
    return env


def run_one(argv: list[str], columns: int) -> tuple[int, str, str]:
    cmd = [str(CLI_EXE), *argv]
    res = subprocess.run(cmd, env=make_env(columns), capture_output=True, text=True, check=False)
    return res.returncode, res.stdout, res.stderr


def git_rev() -> str:
    try:
        r = subprocess.run(["git", "rev-parse", "HEAD"], cwd=REPO_ROOT, capture_output=True, text=True, check=True)
        return r.stdout.strip()
    except Exception:
        return "unknown"


def main() -> None:
    parser = argparse.ArgumentParser(description="擷取 Python 3.12.3 CLI help/usage/error golden")
    parser.add_argument("--out", default=str(REPO_ROOT / "contracts" / "cli_help" / "linux"), help="輸出目錄")
    args = parser.parse_args()

    out_dir = Path(args.out)
    out_dir.mkdir(parents=True, exist_ok=True)

    cases: list[dict] = []

    # 1. 根層級 --help（80 與 120 欄寬）
    for col in (80, 120):
        cases.append({
            "id": f"help_root_col{col}",
            "argv": ["--help"],
            "columns": col,
            "post_parse_effects": False,
        })

    # 2. 20 個群組各自 --help（80 與 120 欄寬）
    for grp in ALL_GROUPS:
        safe_grp = grp.replace("-", "_")
        for col in (80, 120):
            cases.append({
                "id": f"help_grp_{safe_grp}_col{col}",
                "argv": [grp, "--help"],
                "columns": col,
                "post_parse_effects": False,
            })

    # 3. 12 個原生群組所有子命令 --help（80 與 120 欄寬）
    for grp, subs in NATIVE_SUBCOMMANDS.items():
        safe_grp = grp.replace("-", "_")
        for sub in subs:
            safe_sub = sub.replace("-", "_")
            for col in (80, 120):
                cases.append({
                    "id": f"help_sub_{safe_grp}_{safe_sub}_col{col}",
                    "argv": [grp, sub, "--help"],
                    "columns": col,
                    "post_parse_effects": False,
                })

    # 4. 指定錯誤與特殊情境
    error_cases: list[tuple[str, list[str], bool]] = [
        ("err_empty", [], False),
        ("err_bogus", ["bogus"], False),
        ("err_session_missing_sub", ["session"], False),
        ("err_session_bogus_sub", ["session", "bogus"], False),
        ("err_retries_invalid_int", ["--retries", "x", "session", "list"], False),
        ("err_timeout_invalid_float", ["--timeout", "abc", "session", "list"], False),
        ("err_timeout_missing_val", ["--timeout"], False),
        ("err_verbose_ambiguous", ["--v", "session", "list"], False),
        ("quirk_sock_abbrev", ["--sock", "/x", "session", "list"], True),
        ("err_session_list_extra", ["session", "list", "extra"], False),
        ("err_file_push_bad_ack_mode", ["file", "push", "--selector", "C", "--local", "a", "--remote", "b", "--ack-mode", "bad"], False),
        ("err_skill_bad_platform", ["skill", "--platform", "bsd"], False),
        ("err_event_reset_missing_mut", ["event", "reset"], False),
        ("err_event_reset_mutex_conflict", ["event", "reset", "--rule-id", "a", "--selector", "b"], False),
        ("err_event_tail_bad_int", ["event", "tail", "-n", "x"], False),
        ("help_event_tail_with_options", ["event", "tail", "-n5", "--since=7", "--help"], False),
        ("err_cmd_submit_missing_selector", ["cmd", "submit"], False),
        ("err_cmd_submit_selector_missing_val", ["cmd", "submit", "--selector"], False),
        ("err_session_recover_bad_timeout", ["session", "recover", "--selector", "A", "--timeout", "x"], False),
        ("boundary_socket_connect", ["--socket", "connect"], False),
        ("boundary_endpoint_connect", ["--endpoint=connect"], False),
        ("err_dashdash_before_group", ["--", "session", "list"], False),
        ("err_dashdash_after_sub", ["session", "list", "--", "x"], False),
        ("err_vv_missing_group", ["-vv"], False),
        ("action_version", ["--version"], False),
        ("action_version_session", ["--version", "session"], False),
        ("action_help_session", ["-h", "session"], False),
        ("action_session_help_list", ["session", "-h", "list"], False),
        ("err_profile_test_missing_mut", ["profile", "test", "--profile", "p"], False),
        ("err_alias_assign_missing_alias", ["alias", "assign", "--by-id", "b"], False),
        ("quirk_stream_tail_neg_seq", ["stream", "tail", "--from-seq", "-1"], True),
        ("err_log_tail_raw_invalid_int", ["log", "tail-raw", "--from-seq", "1e3"], False),
        ("quirk_retries_neg_one", ["--retries", "-1", "session", "list"], True),
    ]

    for cid, argv, post_parse in error_cases:
        cases.append({
            "id": cid,
            "argv": argv,
            "columns": 80,
            "post_parse_effects": post_parse,
        })

    print(f"總共要擷取 {len(cases)} 筆 CLI help/usage/error 情境...")

    for c in cases:
        argv = c["argv"]
        col = c["columns"]
        rc, out, err = run_one(argv, col)
        # 對於 `--sock /x session list` 縮寫成功情境，計畫註記：此情境只錄 rc 與 stdout
        if c["id"] == "quirk_sock_abbrev":
            err = ""
        record = {
            "argv": argv,
            "columns": col,
            "rc": rc,
            "stdout": out,
            "stderr": err,
            "native": is_native(argv),
            "post_parse_effects": c["post_parse_effects"],
        }
        dest_file = out_dir / f"{c['id']}.json"
        dest_file.write_text(json.dumps(record, ensure_ascii=False, indent=2), encoding="utf-8")

    meta = {
        "python_version": "3.12.3",
        "git_revision": git_rev(),
    }
    # 寫入 _meta.json 到 contracts/cli_help/ 與 out_dir
    meta_json = json.dumps(meta, ensure_ascii=False, indent=2) + "\n"
    (REPO_ROOT / "contracts" / "cli_help" / "_meta.json").write_text(meta_json, encoding="utf-8")
    (out_dir / "_meta.json").write_text(meta_json, encoding="utf-8")
    print(f"完成！已輸出至 {out_dir}")


if __name__ == "__main__":
    main()
