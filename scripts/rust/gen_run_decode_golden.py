#!/usr/bin/env python3
"""以 Python 3.12 subprocess text mode 擷取 SysEnv 解碼基準。"""

from __future__ import annotations

import json
import locale
import os
import subprocess
import sys
from pathlib import Path
from typing import Any


OUTPUT = Path(__file__).resolve().parents[2] / "crates/sw-pal/tests/golden/run_decode.ndjson"
CASE_ORDER = (
    "universal_newlines",
    "invalid_utf8",
    "invalid_utf8_truncated_lead",
    "invalid_utf8_bad_continuation",
    "invalid_utf8_overlong",
    "invalid_utf8_surrogate",
    "invalid_utf8_out_of_range",
    "windows_cp950_invalid_bytes",
    "windows_acp_echo",
)
INVALID_UTF8_CASES = (
    ("invalid_utf8_truncated_lead", r"\xc2"),
    ("invalid_utf8_bad_continuation", r"\xe2\x82A"),
    ("invalid_utf8_overlong", r"\xe0\x80\x80"),
    ("invalid_utf8_surrogate", r"\xed\xa0\x80"),
    ("invalid_utf8_out_of_range", r"\xf4\x90\x80\x80"),
)


def capture(
    case: str, argv: list[str], *, encoding: str | None = None
) -> dict[str, Any]:
    """執行基準命令，保留 Python text=True 的解碼結果或例外字串。"""
    options: dict[str, Any] = {}
    if encoding is not None:
        options["encoding"] = encoding
    try:
        result = subprocess.run(
            argv,
            capture_output=True,
            text=True,
            check=False,
            **options,
        )
    except UnicodeDecodeError as error:
        row = {
            "argv": argv,
            "case": case,
            "decode_error": str(error),
            "rc": None,
            "stderr": None,
            "stdout": None,
        }
        if encoding is not None:
            row["encoding"] = encoding
        return row
    row = {
        "argv": argv,
        "case": case,
        "decode_error": None,
        "rc": result.returncode,
        "stderr": result.stderr,
        "stdout": result.stdout,
    }
    if encoding is not None:
        row["encoding"] = encoding
    return row


def read_existing_cases() -> dict[str, dict[str, Any]]:
    """保留另一作業系統已擷取的案例列，避免單平台重生時刪掉對照。"""
    if not OUTPUT.exists():
        return {}
    rows: dict[str, dict[str, Any]] = {}
    for line in OUTPUT.read_text(encoding="utf-8").splitlines()[1:]:
        if line:
            row = json.loads(line)
            if not row.get("meta"):
                rows[row["case"]] = row
    return rows


def main() -> int:
    if sys.version_info[:2] != (3, 12):
        raise SystemExit("此 golden 必須由 Python 3.12 產生")

    rows = read_existing_cases()
    rows["windows_cp950_invalid_bytes"] = capture(
        "windows_cp950_invalid_bytes",
        [
            sys.executable,
            "-c",
            "import sys; sys.stdout.buffer.write(bytes.fromhex('a4a4ff'))",
        ],
        encoding="cp950",
    )
    if os.name == "nt":
        rows["windows_acp_echo"] = capture(
            "windows_acp_echo", ["cmd", "/c", "echo 中文"]
        )
    else:
        rows["universal_newlines"] = capture(
            "universal_newlines", ["printf", r"a\r\nb\rc\n"]
        )
        rows["invalid_utf8"] = capture("invalid_utf8", ["printf", r"\xff"])
        for case, value in INVALID_UTF8_CASES:
            rows[case] = capture(case, ["printf", value])

    metadata = {
        "encoding": locale.getpreferredencoding(False),
        "generator": "Python 3.12 subprocess.run(capture_output=True, text=True)",
        "meta": True,
        "python": ".".join(map(str, sys.version_info[:3])),
    }
    lines = [json.dumps(metadata, ensure_ascii=True, sort_keys=True, separators=(",", ":"))]
    for case in CASE_ORDER:
        if case in rows:
            lines.append(json.dumps(rows[case], ensure_ascii=True, sort_keys=True, separators=(",", ":")))
    OUTPUT.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
