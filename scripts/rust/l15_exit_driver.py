#!/usr/bin/env python3
"""只豁免 gate 已核准的 Windows L1.5 擁有者延後列之 producer exit 1。"""

from __future__ import annotations

import json
import re
import sys
from pathlib import Path
from typing import Any


_DEFERRED_CASES = {
    "l15-transport-disconnect": (
        {"FAIL", "INSUFFICIENT_EVIDENCE"},
        re.compile(r".*'transport_disconnect' 失敗: HARNESS_CAPABILITY_UNAVAILABLE.*", re.DOTALL),
    ),
    "ev-01-spawn-handler": (
        {"FAIL"},
        re.compile(r"步驟 \d+: 等待檔案逾時 [0-9.]+s: [^\r\n]*[/\\]ev-fired"),
    ),
    "sm-04-device-unplug": (
        {"SKIP"},
        re.compile(r"cap_missing:by_id_link"),
    ),
}


def _has_only_approved_l15_blocker(gate: dict[str, Any]) -> bool:
    if gate.get("overall") not in {"IMPLEMENTED", "VERIFIED"} or gate.get("errors") != []:
        return False
    cases = gate.get("cases")
    if not isinstance(cases, list):
        return False
    for case in cases:
        if not isinstance(case, dict) or case.get("id") not in _DEFERRED_CASES:
            continue
        if case.get("layer") != "L1.5" or case.get("status") != "BLOCKED-OWNER":
            continue
        evidence = case.get("evidence")
        if not isinstance(evidence, list) or not evidence:
            continue
        windows = [row for row in evidence if isinstance(row, dict) and row.get("platform") == "windows"]
        linux = [row for row in evidence if isinstance(row, dict) and row.get("platform") == "linux"]
        if not windows or not linux:
            continue
        if not all(row.get("verdict") == "PASS" and row.get("gate_eligible") is True for row in linux):
            continue
        allowed_verdicts, message_pattern = _DEFERRED_CASES[case["id"]]
        if not all(
            row.get("verdict") in allowed_verdicts
            and message_pattern.fullmatch(str(row.get("message") or "")) is not None
            and not any(
                isinstance(item, dict) and item.get("kind") == "harness_stop_failed"
                for item in (row.get("harness_diagnostics") or [])
            )
            for row in windows
        ):
            continue
        return True
    return False


def unresolved_steps(rc_text: str, gate: dict[str, Any]) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    """回傳未豁免的非零步驟與明確豁免項；格式錯誤一律 fail-closed。"""
    rows: list[dict[str, Any]] = []
    malformed: list[dict[str, Any]] = []
    for line_number, raw in enumerate(rc_text.splitlines(), start=1):
        if not raw.strip():
            continue
        parts = raw.split()
        if len(parts) != 2:
            malformed.append({"line": line_number, "raw": raw})
            continue
        name, value = parts
        try:
            code = int(value, 10)
        except ValueError:
            malformed.append({"line": line_number, "raw": raw})
            continue
        if code != 0:
            rows.append({"step": name, "rc": code})

    suppressed: list[dict[str, Any]] = []
    if (
        not malformed
        and len(rows) == 1
        and rows[0] == {"step": "win_l15_func_test", "rc": 1}
        and rc_text.splitlines().count("stage1a_gate 0") == 1
        and _has_only_approved_l15_blocker(gate)
    ):
        suppressed = rows
        rows = []
    return malformed + rows, suppressed


def main(argv: list[str] | None = None) -> int:
    args = list(sys.argv[1:] if argv is None else argv)
    try:
        if len(args) != 2:
            raise ValueError("用法：l15_exit_driver.py <rc.txt> <stage1a-gate.json>")
        rc_text = Path(args[0]).read_text(encoding="utf-8")
        gate = json.loads(Path(args[1]).read_text(encoding="utf-8"))
        if not isinstance(gate, dict):
            raise ValueError("stage1a gate 報告不是 object")
        unresolved, suppressed = unresolved_steps(rc_text, gate)
        summary = {"failed_steps": len(unresolved), "unresolved": unresolved, "suppressed": suppressed}
    except (OSError, ValueError, json.JSONDecodeError) as exc:
        summary = {"failed_steps": 1, "unresolved": [{"step": "exit_driver_summary", "error": str(exc)}],
                   "suppressed": []}
    print(json.dumps(summary, ensure_ascii=False, sort_keys=True, separators=(",", ":")))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
