#!/usr/bin/env python3
"""收集 Cargo/libtest 的逐案結果並輸出 l1-results.v1（P3-T17、R5-F3）。"""

from __future__ import annotations

import argparse
import json
import os
import re
import shutil
import subprocess
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Sequence


TEST_LINE = re.compile(r"^test (.*?)(?: - should panic)? \.\.\. (ok|FAILED|ignored)(?:,.*)?$")
LIST_LINE = re.compile(r"^(.+?):\s+test\s*$")
CAPABILITY_TEST = re.compile(r"^cap_([a-z0-9]+(?:_[a-z0-9]+)*)__")


@dataclass
class TargetRun:
    crate: str
    target: str
    executable: str
    collected_names: list[str] = field(default_factory=list)
    ignored_names: set[str] = field(default_factory=set)
    results: list[dict[str, Any]] = field(default_factory=list)
    exit_codes: list[int] = field(default_factory=list)


def _parse_args(argv: Sequence[str]) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--platform", choices=("linux", "windows"), default="linux")
    parser.add_argument("--runner", help="執行測試 exe 的 runner 路徑")
    parser.add_argument("--list-only", action="store_true", help="只輸出已收集的 nodeid")
    parser.add_argument("--caps", action="append", default=[], help="另執行 ignored 的指定資源測試，可重複")
    parser.add_argument("--timeout", type=float, default=900.0, help="每次測試執行的逾時秒數")
    parser.add_argument("--summary-out", help=argparse.SUPPRESS)
    parser.add_argument("--out", required=True, help="JSON 報告路徑")
    args, cargo_args = parser.parse_known_args(list(argv))
    if cargo_args and cargo_args[0] == "--":
        cargo_args = cargo_args[1:]
    args.cargo_args = cargo_args
    return args


def _json_lines(output: str) -> list[dict[str, Any]]:
    records: list[dict[str, Any]] = []
    for line in output.splitlines():
        try:
            value = json.loads(line)
        except json.JSONDecodeError:
            continue
        if isinstance(value, dict):
            records.append(value)
    return records


def _test_targets(records: Sequence[dict[str, Any]]) -> list[TargetRun]:
    targets: list[TargetRun] = []
    for record in records:
        if record.get("reason") != "compiler-artifact":
            continue
        target = record.get("target")
        profile = record.get("profile")
        if not isinstance(target, dict) or not isinstance(profile, dict) or profile.get("test") is not True:
            continue
        executable = record.get("executable")
        name = target.get("name")
        crate = _package_name(record.get("package_id"))
        if not isinstance(executable, str) or not isinstance(name, str):
            continue
        if not crate:
            continue
        targets.append(TargetRun(crate=crate, target=name, executable=executable))
    return targets


def _package_name(package_id: object) -> str:
    """從 Cargo 新舊格式 package id 取套件名，不把版本號當 crate 名。"""
    if not isinstance(package_id, str) or not package_id:
        return ""
    if " " in package_id:
        return package_id.split(" ", 1)[0]
    source, _, fragment = package_id.partition("#")
    if "@" in fragment:
        name, _, _version = fragment.partition("@")
        if name:
            return name
    path = source.partition("+")[2] or source
    return path.rstrip("/").rsplit("/", 1)[-1]


def _listed_names(text: str) -> list[str]:
    names: list[str] = []
    for line in text.splitlines():
        matched = LIST_LINE.match(line.strip())
        if matched:
            names.append(matched.group(1))
    return names


def _nodeid(target: TargetRun, name: str) -> str:
    return f"cargo:{target.crate}::{target.target}::{name}"


def _capture_text(value: str | bytes | None) -> str:
    if isinstance(value, bytes):
        return value.decode("utf-8", errors="replace")
    return value or ""


def _run(argv: list[str], timeout_s: float) -> tuple[int, str, str, bool]:
    try:
        proc = subprocess.run(
            argv,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=timeout_s,
            check=False,
        )
        return_code = proc.returncode if proc.returncode >= 0 else 128 - proc.returncode
        complete = proc.returncode >= 0 and proc.returncode != 124
        return return_code, proc.stdout or "", proc.stderr or "", complete
    except subprocess.TimeoutExpired as exc:
        return 124, _capture_text(exc.stdout), _capture_text(exc.stderr), False
    except OSError as exc:
        return 127, "", str(exc), False


def _parse_results(text: str, target: TargetRun) -> list[dict[str, Any]]:
    parsed: list[dict[str, Any]] = []
    for line in text.splitlines():
        matched = TEST_LINE.match(line.strip())
        if not matched:
            continue
        name, status = matched.groups()
        if status == "ok":
            outcome = "passed"
            skip_kind = None
        elif status == "FAILED":
            outcome = "failed"
            skip_kind = None
        else:
            outcome = "skipped"
            capability = CAPABILITY_TEST.match(name)
            skip_kind = "capability" if capability else "other"
        parsed.append(
            {
                "nodeid": _nodeid(target, name),
                "outcome": outcome,
                "skip_kind": skip_kind,
                "phases": {
                    "setup": "passed",
                    "call": outcome,
                    "teardown": "passed",
                },
            }
        )
    return parsed


def _read_report(path: str, value: dict[str, Any]) -> None:
    output = Path(path)
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":")) + "\n", encoding="utf-8")


def main(argv: Sequence[str] | None = None) -> int:
    args = _parse_args(sys.argv[1:] if argv is None else argv)
    out_path = Path(args.out)
    cargo = shutil.which("cargo")
    if cargo is None:
        print("cargo_l1_results.py: cargo 不存在，證據不足", file=sys.stderr)
        return 2

    build_argv = [cargo, "test", "--no-run", "--message-format=json"]
    if args.platform == "windows":
        build_argv.extend(["--target", "x86_64-pc-windows-gnu"])
    build_argv.extend(args.cargo_args)
    build_rc, build_stdout, build_stderr, build_complete = _run(build_argv, args.timeout)
    targets = _test_targets(_json_lines(build_stdout))
    collected: list[str] = []
    finished = build_rc == 0 and build_complete and bool(targets)
    exit_code = build_rc

    if build_rc != 0 or not build_complete or not targets:
        if not targets and build_rc == 0:
            exit_code = 1
            build_stderr += "\nCargo 沒有產生測試 executable，證據不足。"
        if args.list_only:
            _read_report(args.out, {"collected": []})
        else:
            _read_report(
                args.out,
                {
                    "schema_version": 2,
                    "format": "l1-results.v1",
                    "runner": "cargo",
                    "platform": args.platform,
                    "argv": args.cargo_args,
                    "markexpr": "",
                    "counts": {"all": {"passed": 0, "failed": 0, "skipped": 0}},
                    "resources": {},
                    "uart_kind": "native",
                    "not_collected_platform": [],
                    "collected": [],
                    "deselected": [],
                    "results": [],
                    "exit_code": exit_code,
                    "finished": False,
                    "core_portable_platform_skips": [],
                    "deployed_content": None,
                },
            )
        if build_stderr:
            print(build_stderr, file=sys.stderr, end="" if build_stderr.endswith("\n") else "\n")
        return exit_code or 1

    for target in targets:
        runner_argv = [args.runner, target.executable] if args.runner else [target.executable]
        list_rc, list_stdout, list_stderr, list_complete = _run(
            runner_argv + ["--list", "--format", "terse"], args.timeout
        )
        ignored_rc, ignored_stdout, ignored_stderr, ignored_complete = _run(
            runner_argv + ["--ignored", "--list", "--format", "terse"], args.timeout
        )
        if list_rc != 0 or ignored_rc != 0 or not list_complete or not ignored_complete:
            finished = False
            failure = list_rc or ignored_rc or 124
            exit_code = max(exit_code, failure)
            if list_stderr or ignored_stderr:
                print(list_stderr + ignored_stderr, file=sys.stderr, end="")
            # 一個 test executable 無法收集時，整份 L1 已不完整；停止反覆呼叫同一個失效 runner。
            break

        all_names = _listed_names(list_stdout)
        ignored_names = _listed_names(ignored_stdout)
        target.ignored_names = set(ignored_names)
        target.collected_names = list(all_names)
        for name in ignored_names:
            if name not in target.collected_names:
                target.collected_names.append(name)
        collected.extend(_nodeid(target, name) for name in target.collected_names)

    if args.list_only:
        # plan 04 Task 17：--list-only 輸出每個收集到的測試，ignored 與 cap_* 皆保留，
        # 使 regen_lock 不會把既有 ignored／cap_* 列誤判為移除。任何排除須在 freeze 輸入之外明示處理，不得經由此處縮小分母。
        _read_report(args.out, {"collected": collected})
        return 0 if finished else 1

    summaries: list[dict[str, Any]] = []
    for target in targets:
        if not target.collected_names:
            continue
        runner_argv = [args.runner, target.executable] if args.runner else [target.executable]
        run_rc, run_stdout, run_stderr, run_complete = _run(runner_argv, args.timeout)
        target.exit_codes.append(run_rc)
        if not run_complete:
            finished = False
        if run_stderr:
            print(run_stderr, file=sys.stderr, end="" if run_stderr.endswith("\n") else "\n")
        target.results = _parse_results(run_stdout, target)

        available_caps = {
            matched.group(1)
            for name in target.ignored_names
            if (matched := CAPABILITY_TEST.match(name)) is not None
        }
        selected_caps = [cap for cap in args.caps if cap in available_caps]
        for cap in selected_caps:
            cap_rc, cap_stdout, cap_stderr, cap_complete = _run(
                runner_argv + ["--ignored", f"cap_{cap}__"], args.timeout
            )
            target.exit_codes.append(cap_rc)
            if not cap_complete:
                finished = False
            if cap_stderr:
                print(cap_stderr, file=sys.stderr, end="" if cap_stderr.endswith("\n") else "\n")
            cap_results = _parse_results(cap_stdout, target)
            replaced = {row["nodeid"]: row for row in cap_results}
            target.results = [replaced.get(row["nodeid"], row) for row in target.results]
            known = {row["nodeid"] for row in target.results}
            target.results.extend(row for row in cap_results if row["nodeid"] not in known)

        exit_code = max([exit_code, *target.exit_codes])
        counts = {"passed": 0, "failed": 0, "ignored": 0}
        for result in target.results:
            if result["outcome"] == "passed":
                counts["passed"] += 1
            elif result["outcome"] == "failed":
                counts["failed"] += 1
            else:
                counts["ignored"] += 1
        summaries.append(
            {
                "exe": target.executable,
                "caps": selected_caps,
                **counts,
                "exit": max(target.exit_codes, default=1),
            }
        )

    results = [row for target in targets for row in target.results]
    tally = {"passed": 0, "failed": 0, "skipped": 0}
    for result in results:
        outcome = result["outcome"]
        if outcome in tally:
            tally[outcome] += 1
        elif outcome in ("xfailed", "xpassed"):
            tally["failed" if outcome == "xpassed" else "skipped"] += 1
    report = {
        "schema_version": 2,
        "format": "l1-results.v1",
        "runner": "cargo",
        "platform": args.platform,
        "argv": args.cargo_args,
        "markexpr": "",
        "counts": {"all": tally},
        "resources": {},
        "uart_kind": "native",
        "not_collected_platform": [],
        "collected": collected,
        "deselected": [],
        "results": results,
        "exit_code": exit_code,
        "finished": finished,
        "core_portable_platform_skips": [],
        "deployed_content": None,
    }
    _read_report(args.out, report)
    if args.summary_out:
        available_caps = {
            matched.group(1)
            for target in targets
            for name in target.ignored_names
            if (matched := CAPABILITY_TEST.match(name)) is not None
        }
        evidence_insufficient = {
            matched.group(1)
            for result in results
            if result["outcome"] == "skipped" and result["skip_kind"] == "capability"
            for matched in [CAPABILITY_TEST.match(result["nodeid"].rsplit("::", 1)[-1])]
            if matched is not None
        }
        evidence_insufficient.update(set(args.caps) - available_caps)
        _read_report(
            args.summary_out,
            {
                "exes": summaries,
                "evidence_insufficient": sorted(evidence_insufficient),
            },
        )
    return 0 if finished and exit_code == 0 and summaries else (exit_code or 1)


if __name__ == "__main__":
    raise SystemExit(main())
