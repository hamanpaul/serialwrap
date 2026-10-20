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
import io
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
ROOT_VALUE_OPTIONS = ("--socket", "--endpoint", "--bench", "--timeout", "--retries")

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
    index = 0
    while index < len(argv):
        token = argv[index]
        if token == "--":
            index += 1
            return index >= len(argv) or argv[index] not in DELEGATED_GROUPS
        option, separator, _value = token.partition("=")
        matches = [name for name in ROOT_VALUE_OPTIONS if name.startswith(option)]
        if matches and (option in ROOT_VALUE_OPTIONS or len(matches) == 1):
            index += 1 if separator else 2
            continue
        if token.startswith("-"):
            index += 1
            continue
        return token not in DELEGATED_GROUPS
    return True


def make_env(columns: int, platform: str) -> dict[str, str]:
    if platform == "windows":
        home = r"C:\golden\home"
        config_dir = r"C:\golden\config"
        state_dir = r"C:\golden\state"
        run_dir = r"C:\golden\run"
    else:
        home = "/golden/home"
        config_dir = "/golden/config"
        state_dir = "/golden/state"
        run_dir = "/golden/run"
    env = {
        "HOME": home,
        "COLUMNS": str(columns),
        "SERIALWRAP_CONFIG_DIR": config_dir,
        "SERIALWRAP_STATE_DIR": state_dir,
        "SERIALWRAP_RUN_DIR": run_dir,
        "PATH": os.environ.get("PATH", "/usr/bin:/bin"),
    }
    if platform == "linux":
        env.update(LANG="C.UTF-8", LC_ALL="C.UTF-8")
    return env


def run_one(argv: list[str], columns: int, platform: str) -> tuple[int, str, str]:
    cmd = [str(CLI_EXE), *argv]
    res = subprocess.run(
        cmd,
        cwd=REPO_ROOT,
        env=make_env(columns, platform),
        capture_output=True,
        text=True,
        check=False,
    )
    return res.returncode, res.stdout, res.stderr


def run_parser(argv: list[str], columns: int, platform: str) -> tuple[int, str, str]:
    """直接擷取 argparse 結果，與 CLI 後續錯誤包裝分開保存。"""
    old_env = os.environ.copy()
    stdout_io, stderr_io = io.StringIO(), io.StringIO()
    old_stdout, old_stderr = sys.stdout, sys.stderr
    try:
        for key in list(os.environ):
            if key.startswith("SERIALWRAP_"):
                os.environ.pop(key, None)
        os.environ.update(make_env(columns, platform))

        if str(REPO_ROOT) not in sys.path:
            sys.path.insert(0, str(REPO_ROOT))
        from sw_core.cli import build_parser

        sys.stdout, sys.stderr = stdout_io, stderr_io
        try:
            build_parser().parse_args(argv)
            rc = 0
        except SystemExit as exc:
            rc = exc.code if isinstance(exc.code, int) else (0 if exc.code is None else 1)
    finally:
        sys.stdout, sys.stderr = old_stdout, old_stderr
        os.environ.clear()
        os.environ.update(old_env)
    return rc, stdout_io.getvalue(), stderr_io.getvalue()


# ── P4-T1：Rust-only 擴充（`--rust-only`）──────────────────────────────────────
# 與 `crates/sw-cli/src/cli_spec.rs` 的 Rust-only 宣告逐項對應：Stage 1B 新增的旗標不屬於 Python 基線，
# 因此 golden 以 Python 3.12.3 argparse 在相同 `add_argument` 擴充後擷取（計畫 Task 1 步驟 5）。
RUST_ONLY_GROUPS = ("supervision-mode", "setup")
RUST_ONLY_COLUMNS = (80, 120)


def _subparser_choices(parser: argparse.ArgumentParser) -> dict:
    for action in parser._actions:
        if isinstance(action, argparse._SubParsersAction):
            return action.choices
    raise SystemExit("找不到根層子命令群組")


def apply_rust_only(parser: argparse.ArgumentParser) -> None:
    """把 Stage 1B 的 Rust-only 旗標加到 `supervision-mode`／`setup` 子 parser（計畫 Task 1 步驟 4 逐字）。"""
    choices = _subparser_choices(parser)
    sup = choices["supervision-mode"]
    sup.add_argument(
        "words",
        nargs="*",
        metavar="set MODE",
        help="不帶參數：印出目前監管模式；set MODE：切換並執行 reconcile",
    )
    sup.add_argument(
        "--force",
        action="store_true",
        help="相容旗標：不覆寫交接護欄（flash、傳輸、活躍操作或探測不明時一律拒絕）",
    )
    sup.add_argument(
        "--with-sudo",
        dest="with_sudo",
        action="store_true",
        default=False,
        help="允許代跑需要 root／系統管理員權限的指令",
    )
    setup = choices["setup"]
    mutex = setup._mutually_exclusive_groups[0]
    added = [
        mutex.add_argument(
            "--windows-task",
            dest="windows_task",
            action="store_true",
            help="Windows：以工作排程器登入自啟監管（Windows 預設）",
        ),
        mutex.add_argument(
            "--windows-service",
            dest="windows_service",
            action="store_true",
            help="Windows：以 Windows 服務監管（OD-12：只列出待執行指令）",
        ),
        mutex.add_argument(
            "--rollback",
            action="store_true",
            help="回滾到上一個完整發佈組合（Previous）",
        ),
    ]
    com0com_mutex = setup.add_mutually_exclusive_group()
    com0com_added = [
        com0com_mutex.add_argument(
            "--with-com0com",
            dest="with_com0com",
            action="store_true",
            help="Windows：選擇安裝實驗性的 com0com 虛擬序列埠（僅此選用元件會要求 UAC）",
        ),
        com0com_mutex.add_argument(
            "--remove-com0com",
            dest="remove_com0com",
            action="store_true",
            help="Windows：移除 com0com 預設虛擬序列埠 pair",
        ),
    ]
    # argparse 的互斥群組只在成員於 `_actions` 中連續時才以 `a | b` 顯示用法；把新成員移到既有群組成員之後，
    # 讓 golden 反映「同一互斥群組」的語意（Rust emulator 亦以群組整體渲染）。
    anchor = next(a for a in setup._actions if a.dest == "on_demand")
    rest = [a for a in setup._actions if a not in added]
    position = rest.index(anchor) + 1
    setup._actions[:] = rest[:position] + added + rest[position:]
    # 保留新選項在 CLI 規格中的順序，且只讓兩個 com0com 動作互斥。
    setup._actions.extend(action for action in com0com_added if action not in setup._actions)


def write_rust_only(out_dir: Path) -> list[Path]:
    """擷取 `contracts/cli_help/rust_only/{supervision-mode,setup}.{80,120}.txt`（COLUMNS 決定 argparse 寬度）。"""
    if str(REPO_ROOT) not in sys.path:
        sys.path.insert(0, str(REPO_ROOT))
    written: list[Path] = []
    out_dir.mkdir(parents=True, exist_ok=True)
    old_columns = os.environ.get("COLUMNS")
    try:
        for columns in RUST_ONLY_COLUMNS:
            os.environ["COLUMNS"] = str(columns)
            from sw_core.cli import build_parser

            parser = build_parser()
            apply_rust_only(parser)
            choices = _subparser_choices(parser)
            for group in RUST_ONLY_GROUPS:
                target = out_dir / f"{group}.{columns}.txt"
                target.write_text(choices[group].format_help(), encoding="utf-8", newline="\n")
                written.append(target)
    finally:
        if old_columns is None:
            os.environ.pop("COLUMNS", None)
        else:
            os.environ["COLUMNS"] = old_columns
    return written


def git_rev() -> str:
    try:
        r = subprocess.run(["git", "rev-parse", "HEAD"], cwd=REPO_ROOT, capture_output=True, text=True, check=True)
        return r.stdout.strip()
    except Exception:
        return "unknown"


def main() -> None:
    parser = argparse.ArgumentParser(description="擷取 Python 3.12.3 CLI help/usage/error golden")
    parser.add_argument("--platform", choices=("linux", "windows"), help="指定 golden 平台，預設依執行環境判定")
    parser.add_argument("--out", help="輸出目錄；預設 contracts/cli_help/<platform>")
    parser.add_argument(
        "--rust-only",
        action="store_true",
        help="擷取 Stage 1B Rust-only golden（contracts/cli_help/rust_only/，P4-T1）",
    )
    args = parser.parse_args()
    if args.rust_only:
        out_dir = Path(args.out) if args.out else REPO_ROOT / "contracts" / "cli_help" / "rust_only"
        for written in write_rust_only(out_dir):
            print(written)
        return
    platform = args.platform or ("windows" if os.name == "nt" else "linux")
    if platform == "windows" and os.name != "nt":
        raise SystemExit("Windows CLI golden 必須由 Windows CPython 3.12.3 擷取；不可在其他平台代產")
    if platform == "linux" and os.name == "nt":
        raise SystemExit("Linux CLI golden 必須由 POSIX CPython 3.12.3 擷取")
    global CLI_EXE
    CLI_EXE = REPO_ROOT / ("serialwrap.py" if platform == "windows" else "serialwrap")

    default_out = REPO_ROOT / "contracts" / "cli_help" / platform
    out_dir = Path(args.out) if args.out else default_out
    out_dir.mkdir(parents=True, exist_ok=True)

    cases: list[dict] = []

    # 1. 根層級 --help（多個欄寬涵蓋文字斷行邊界）
    for col in (20, 21, 25, 30, 40, 60, 80, 90, 120):
        cases.append({
            "id": f"help_root_col{col}",
            "argv": ["--help"],
            "columns": col,
            "post_parse_effects": False,
        })

    # 2. 20 個群組各自 --help（多個欄寬涵蓋文字斷行邊界）
    for grp in ALL_GROUPS:
        safe_grp = grp.replace("-", "_")
        for col in (20, 21, 25, 30, 40, 60, 80, 90, 120):
            cases.append({
                "id": f"help_grp_{safe_grp}_col{col}",
                "argv": [grp, "--help"],
                "columns": col,
                "post_parse_effects": False,
            })

    # 3. 12 個原生群組所有子命令 --help（多個欄寬涵蓋文字斷行邊界）
    for grp, subs in NATIVE_SUBCOMMANDS.items():
        safe_grp = grp.replace("-", "_")
        for sub in subs:
            safe_sub = sub.replace("-", "_")
            for col in (60, 80, 90, 120):
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
        ("err_dashdash_lone", ["--"], False),
        ("err_dashdash_after_group", ["session", "--"], False),
        ("err_dashdash_after_sub", ["session", "list", "--", "x"], False),
        ("err_event_rm_extras_after_dashdash", ["event", "rm", "a", "b", "--"], False),
        ("err_event_tail_short_bundle_missing_value", ["event", "tail", "-hn"], False),
        ("err_profile_test_ambiguous_equal", ["profile", "test", "--pro=x"], False),
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

    if platform == "windows":
        windows_errors = {
            "err_empty",
            "err_bogus",
            "err_session_missing_sub",
            "err_session_bogus_sub",
            "err_retries_invalid_int",
            "err_timeout_invalid_float",
            "err_timeout_missing_val",
            "err_verbose_ambiguous",
            "err_session_list_extra",
            "err_event_reset_missing_mut",
        }
        cases = [
            {"id": "help_root_col80", "argv": ["--help"], "columns": 80, "post_parse_effects": False},
            {"id": "help_profile_test_col80", "argv": ["profile", "test", "--help"], "columns": 80, "post_parse_effects": False},
        ]
        cases.extend(
            {
                "id": case_id,
                "argv": argv,
                "columns": 80,
                "post_parse_effects": post_parse,
            }
            for case_id, argv, post_parse in error_cases
            if case_id in windows_errors
        )
    else:
        for cid, argv, post_parse in error_cases:
            cases.append({
                "id": cid,
                "argv": argv,
                "columns": 80,
                "post_parse_effects": post_parse,
            })

    if platform == "windows":
        cases = [case for case in cases if case["id"] in {"help_root_col80", "help_profile_test_col80"} or case["id"] in windows_errors]

    print(f"總共要擷取 {len(cases)} 筆 CLI help/usage/error 情境...")

    for c in cases:
        argv = c["argv"]
        col = c["columns"]
        rc, out, err = run_one(argv, col, platform)
        parse_rc, parse_out, parse_err = run_parser(argv, col, platform)
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
        if not c["post_parse_effects"] and (parse_rc, parse_out, parse_err) != (rc, out, err):
            record.update(parse_rc=parse_rc, parse_stdout=parse_out, parse_stderr=parse_err)
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
