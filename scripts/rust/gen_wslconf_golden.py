#!/usr/bin/env python3
"""以 Python configparser 產生 WSL /etc/wsl.conf 合併基準（DF-11、P4-T10）。"""

from __future__ import annotations

import json
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from sw_core.setup_cmd import _merge_wsl_conf_systemd

OUTPUT = (
    REPO_ROOT
    / "crates"
    / "sw-cli"
    / "tests"
    / "golden"
    / "wslconf.ndjson"
)

CASES: list[tuple[str, str]] = [
    ("empty", ""),
    ("existing_boot_empty", "[boot]\n"),
    ("existing_boot_systemd_false", "[boot]\nsystemd=false\n"),
    ("existing_boot_systemd_true", "[boot]\nsystemd=true\n"),
    ("bad_format_missing_section", "systemd=false\n"),
    ("bad_format_duplicate_section", "[boot]\nsystemd=false\n[boot]\n"),
    ("bad_format_duplicate_option", "[boot]\nsystemd=false\nsystemd=true\n"),
    (
        "with_comments",
        "# comment\n; semi comment\n[network]\ngenerateResolvConf=false\n",
    ),
    (
        "case_sensitive_keys",
        "[interop]\nenabled=true\nappendWindowsPath=false\n",
    ),
    ("default_section", "[DEFAULT]\nmetadata=true\n[boot]\nsystemd=false\n"),
    (
        "repeated_default_sections",
        "[DEFAULT]\nfirst=true\n[network]\nhostname=demo\n[DEFAULT]\nsecond=true\n",
    ),
    ("key_colon_value", "[user]\ndefault: alice\n"),
    ("continuation_line", "[automount]\noptions = metadata,\n\tumask=22\n"),
    (
        "multiple_sections_with_boot",
        "[network]\nhostname = demo\n\n[boot]\ncommand = /usr/bin/run\n",
    ),
    (
        "section_header_trailing_text",
        "[automount]\nenabled=true\n[boot] # keep\nsystemd=false\n[network]trailing\nhostname=x\n",
    ),
    ("section_header_spacing_preserved", "[ boot ]\nvalue = kept\n[ ]\nempty = named\n"),
    (
        "blank_line_in_continuation",
        "[user]\ncommand = first\n\n\tthird\n[boot]\nsystemd=false\n",
    ),
]


def main() -> int:
    OUTPUT.parent.mkdir(parents=True, exist_ok=True)
    lines: list[str] = []
    for case, content in CASES:
        expected = _merge_wsl_conf_systemd(content)
        row = {
            "case": case,
            "input": content,
            "expected": expected,
        }
        lines.append(json.dumps(row, ensure_ascii=False))

    OUTPUT.write_text("\n".join(lines) + "\n", encoding="utf-8")
    print(f"已產生 {len(lines)} 筆 golden 案例至 {OUTPUT}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
