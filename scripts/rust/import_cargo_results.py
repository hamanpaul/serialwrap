#!/usr/bin/env python3
"""逐一執行 Cargo/libtest 測試，匯入與 pytest 相同格式的結果。"""
from __future__ import annotations

import argparse
import json
import os
import re
import subprocess
import sys
import time
from pathlib import Path
from typing import Any, Sequence

from contract_coverage import ROOT, _contract_revision, _sha256


_CAPABILITY = re.compile(r"^cap_([a-z0-9]+(?:_[a-z0-9]+)*)__")
_LISTED_TEST = re.compile(r"^(.+?):\s+test\s*$")


def _utc_now() -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())


def _package_name(package_id: object) -> str:
    if not isinstance(package_id, str) or not package_id:
        return ""
    if " " in package_id:
        return package_id.split(" ", 1)[0]
    source, _, fragment = package_id.partition("#")
    if "@" in fragment:
        name, _, _version = fragment.partition("@")
        if name:
            return name
    return (source.partition("+")[2] or source).rstrip("/").rsplit("/", 1)[-1]


def _test_targets(records: Sequence[dict[str, Any]]) -> list[dict[str, str]]:
    targets = []
    for record in records:
        if record.get("reason") != "compiler-artifact":
            continue
        target = record.get("target")
        profile = record.get("profile")
        if not isinstance(target, dict) or not isinstance(profile, dict) or profile.get("test") is not True:
            continue
        executable = record.get("executable")
        crate = _package_name(record.get("package_id"))
        name = target.get("name")
        if isinstance(executable, str) and isinstance(name, str) and crate:
            targets.append({"crate": crate, "test_file": name, "executable": executable})
    return targets


def _json_lines(text: str) -> list[dict[str, Any]]:
    output = []
    for line in text.splitlines():
        try:
            value = json.loads(line)
        except json.JSONDecodeError:
            continue
        if isinstance(value, dict):
            output.append(value)
    return output


def _listed_names(text: str) -> list[str]:
    return [matched.group(1) for line in text.splitlines() if (matched := _LISTED_TEST.match(line.strip()))]


def _nodeid(crate: str, test_file: str, name: str) -> str:
    return f"cargo:{crate}::{test_file}::{name}"


def _capability(name: str) -> str | None:
    matched = _CAPABILITY.match(name.rsplit("::", 1)[-1])
    return matched.group(1) if matched else None


def _should_execute(name: str, allowed_caps: set[str]) -> bool:
    capability = _capability(name)
    return capability is None or capability in allowed_caps


def _run(argv: Sequence[str], timeout: float) -> tuple[int, str, str, bool]:
    try:
        proc = subprocess.run(
            list(argv), cwd=ROOT, capture_output=True, text=True, encoding="utf-8", errors="replace",
            timeout=timeout, check=False,
        )
    except subprocess.TimeoutExpired as exc:
        stdout = exc.stdout.decode("utf-8", errors="replace") if isinstance(exc.stdout, bytes) else (exc.stdout or "")
        stderr = exc.stderr.decode("utf-8", errors="replace") if isinstance(exc.stderr, bytes) else (exc.stderr or "")
        return 124, stdout, stderr, False
    except OSError as exc:
        return 127, "", f"{type(exc).__name__}: {exc}", False
    code = proc.returncode if proc.returncode >= 0 else 128 - proc.returncode
    return code, proc.stdout or "", proc.stderr or "", proc.returncode >= 0


def _phase_outcome(return_code: int, stdout: str, stderr: str) -> str:
    output = stdout + "\n" + stderr
    if re.search(r"\.\.\. ignored(?:\s|$)", output):
        return "skipped"
    return "passed" if return_code == 0 else "failed"


def _base_document(label: str, platform: str, started_at: str) -> dict[str, Any]:
    cli = os.environ.get("SERIALWRAP_CLI_EXE")
    daemon = os.environ.get("SERIALWRAP_DAEMON_EXE")
    cases = ROOT / "tests/acceptance/cases.yaml"
    lock = ROOT / "tests/acceptance/cases.lock.json"
    native_windows = platform == "windows" and os.name == "nt" and sys.platform == "win32"
    return {
        "label": label,
        "platform": platform,
        "native_windows": native_windows,
        "native_build_link": native_windows,
        "cli_exe": cli,
        "cli_sha256": _sha256(Path(cli)) if cli else None,
        "daemon_exe": daemon,
        "daemon_sha256": _sha256(Path(daemon)) if daemon else None,
        "contract_revision": _contract_revision(ROOT / "contracts"),
        "cases_yaml_sha256": _sha256(cases),
        "cases_lock_sha256": _sha256(lock),
        "tool": {"name": "import_cargo_results", "revision": _sha256(Path(__file__))},
        "started_at": started_at,
    }


def _merge_existing(path: Path, incoming: dict[str, Any]) -> dict[str, Any]:
    if not path.exists():
        return incoming
    try:
        existing = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        incoming.setdefault("identity_errors", []).append("existing 結果檔無法讀取，拒絕以新輪覆寫")
        incoming["finished"] = False
        return incoming
    if not isinstance(existing, dict) or existing.get("label") != incoming.get("label"):
        incoming.setdefault("identity_errors", []).append("同 label 的既有結果檔格式或 label 不符")
        incoming["finished"] = False
        return incoming
    identity_fields = ("platform", "contract_revision", "cases_yaml_sha256", "cases_lock_sha256", "cli_sha256", "daemon_sha256")
    conflicts = [field for field in identity_fields if existing.get(field) != incoming.get(field)]
    merged = dict(incoming)
    merged["started_at"] = existing.get("started_at", incoming.get("started_at"))
    merged["collected"] = [*existing.get("collected", []), *incoming.get("collected", [])]
    merged["results"] = [*existing.get("results", []), *incoming.get("results", [])]
    merged["exit_code"] = max(int(existing.get("exit_code", 0)), int(incoming.get("exit_code", 0)))
    merged["finished"] = existing.get("finished") is True and incoming.get("finished") is True and not conflicts
    merged["errors"] = [*existing.get("errors", []), *incoming.get("errors", [])]
    merged["identity_errors"] = [*existing.get("identity_errors", [])]
    if conflicts:
        merged["identity_errors"].append("同 label 結果來源身分不同：" + ", ".join(conflicts))
    return merged


def _convert_fixture(document: dict[str, Any]) -> tuple[list[str], list[dict[str, Any]], int, bool]:
    collected: list[str] = []
    results: list[dict[str, Any]] = []
    for target in document.get("targets", []):
        if not isinstance(target, dict):
            continue
        crate = str(target.get("crate", ""))
        test_file = str(target.get("test_file", ""))
        for test in target.get("tests", []):
            if not isinstance(test, dict):
                continue
            name = str(test.get("name", ""))
            nodeid = _nodeid(crate, test_file, name)
            if nodeid not in collected:
                collected.append(nodeid)
            outcome = str(test.get("outcome", "error"))
            if outcome not in {"passed", "failed", "skipped", "xfailed", "error"}:
                outcome = "error"
            results.append({
                "nodeid": nodeid,
                "outcome": outcome,
                "phase": "call",
                "skip_reason": test.get("skip_reason"),
                "evidence": test.get("evidence", []),
            })
    code = document.get("exit_code", 0)
    code = code if isinstance(code, int) else 1
    finished = document.get("finished") is True and code == 0
    return collected, results, code, finished


def _collect(args: argparse.Namespace, cargo_args: Sequence[str]) -> tuple[list[str], list[dict[str, Any]], int, bool, list[str]]:
    timeout = args.timeout
    command = ["cargo", "test", "--no-run", "--message-format", "json", *cargo_args]
    build_code, build_stdout, build_stderr, build_finished = _run(command, timeout)
    errors: list[str] = []
    if build_code != 0 or not build_finished:
        errors.append(f"cargo build exit={build_code}: {(build_stderr or build_stdout).strip()[-2000:]}")
        return [], [], build_code or 1, False, errors

    targets = _test_targets(_json_lines(build_stdout))
    if not targets:
        return [], [], 1, False, ["cargo --no-run 沒有回報 test binary"]
    collected: list[str] = []
    results: list[dict[str, Any]] = []
    exit_codes: list[int] = []
    finished = True
    allowed_caps = set(args.include_cap)
    for target in targets:
        executable = target["executable"]
        listed_code, listed_stdout, listed_stderr, listed_finished = _run([executable, "--list"], timeout)
        if listed_code != 0 or not listed_finished:
            finished = False
            exit_codes.append(listed_code or 1)
            errors.append(f"{target['crate']}::{target['test_file']} --list exit={listed_code}: {listed_stderr.strip()}")
            continue
        names = _listed_names(listed_stdout)
        for name in names:
            nodeid = _nodeid(target["crate"], target["test_file"], name)
            collected.append(nodeid)
        for name in names:
            nodeid = _nodeid(target["crate"], target["test_file"], name)
            capability = _capability(name)
            if capability and not _should_execute(name, allowed_caps):
                results.append({
                    "nodeid": nodeid,
                    "outcome": "skipped",
                    "phase": "call",
                    "skip_reason": f"cap:{capability} 未由 --include-cap 明確授權",
                    "evidence": [],
                })
                continue
            test_argv = [executable, "--exact", name, "--nocapture"]
            if capability:
                test_argv.append("--include-ignored")
            code, stdout, stderr, test_finished = _run(test_argv, timeout)
            if not test_finished:
                finished = False
            exit_codes.append(code)
            outcome = _phase_outcome(code, stdout, stderr)
            if code != 0 and outcome != "skipped":
                errors.append(f"{nodeid} exit={code}: {(stderr or stdout).strip()[-1500:]}")
            results.append({
                "nodeid": nodeid,
                "outcome": outcome,
                "phase": "call",
                "skip_reason": "libtest marked ignored" if outcome == "skipped" else None,
                "evidence": [],
            })
    exit_code = max((code for code in exit_codes if code), default=0)
    if not finished and exit_code == 0:
        exit_code = 1
    return collected, results, exit_code, finished and exit_code == 0, errors


def _parse_args(argv: Sequence[str]) -> tuple[argparse.Namespace, list[str]]:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--label", required=True)
    parser.add_argument("--platform", choices=("linux", "windows"), default="linux")
    parser.add_argument("--out-dir", type=Path, required=True)
    parser.add_argument("--fixture", type=Path, help="用小型 JSON 樣本驗證格式，不執行 Cargo")
    parser.add_argument("--timeout", type=float, default=float(os.environ.get("SW_CARGO_RESULT_TIMEOUT", "900")))
    parser.add_argument("--include-cap", action="append", default=[], help="執行指定 cap:<resource> 測試；可重複，預設不執行任何能力測試")
    args, cargo_args = parser.parse_known_args(list(argv))
    if cargo_args and cargo_args[0] == "--":
        cargo_args = cargo_args[1:]
    return args, cargo_args


def main(argv: Sequence[str] | None = None) -> int:
    args, cargo_args = _parse_args(argv or sys.argv[1:])
    started_at = _utc_now()
    document = _base_document(args.label, args.platform, started_at)
    errors: list[str] = []
    if args.fixture:
        try:
            fixture = json.loads(args.fixture.read_text(encoding="utf-8"))
            if not isinstance(fixture, dict):
                raise ValueError("fixture 根節點不是 object")
            collected, results, exit_code, finished = _convert_fixture(fixture)
            errors = [str(value) for value in fixture.get("errors", [])]
        except (OSError, json.JSONDecodeError, ValueError) as exc:
            collected, results, exit_code, finished = [], [], 2, False
            errors.append(f"fixture 讀取失敗：{type(exc).__name__}: {exc}")
    else:
        if not cargo_args:
            cargo_args = ["--workspace", "--features", "test-fakes"]
        collected, results, exit_code, finished, errors = _collect(args, cargo_args)
    document.update({
        "finished_at": _utc_now(),
        "collected": collected,
        "exit_code": exit_code,
        "finished": finished,
        "results": results,
    })
    if errors:
        document["errors"] = errors
    args.out_dir.mkdir(parents=True, exist_ok=True)
    out_path = args.out_dir / f"results-{args.label}.json"
    document = _merge_existing(out_path, document)
    out_path.write_text(json.dumps(document, ensure_ascii=False, sort_keys=True, separators=(",", ":")) + "\n", encoding="utf-8")
    print(f"results: {out_path}")
    print(f"collected={len(collected)} results={len(results)} exit_code={exit_code} finished={finished}")
    return 0 if finished else (exit_code or 1)


if __name__ == "__main__":
    raise SystemExit(main())
