#!/usr/bin/env python3
"""以 Python 基線產生串接 reboot tokenizer 與 segment golden。"""

from __future__ import annotations

import argparse
import json
import shlex
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[3]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from sw_core.session_manager import find_chained_reboot_segment


COMMANDS = [
    "firstboot -y && reboot",
    "bcm_bootstate 3 && reboot",
    "sync; systemctl reboot",
    "false || shutdown -r now",
    "sleep 1 & reboot -f",
    "sync && echo b > /proc/sysrq-trigger",
    "A && reboot -f",
    "reboot",
    "systemctl reboot",
    "shutdown -r now",
    "echo 'a && reboot'",
    'echo "a || shutdown -r now"',
    "echo reboot && ls",
    "A && 'unterminated",
    'A && "unterminated',
    "A && reboot ;",
    "A ; reboot && reboot -f",
    "A ; echo reboot && sleep",
    "A & shutdown --reboot now",
    "A || /sbin/reboot",
    "A && /usr/sbin/shutdown -r now",
    "A&&reboot",
    "A||shutdown -r",
    "A;systemctl reboot",
    "A&reboot",
    "A && reboot; next",
    "A && echo b>/proc/sysrq-trigger",
    "A && echo b > /proc/sysrq-trigger",
    "A && echo 'b' > /proc/sysrq-trigger",
    'A && echo "b" > /proc/sysrq-trigger',
    "A && reboot --help",
    "A && systemctl rebootfoo",
    "A && systemctl reboot",
    "A && shutdown -h now",
    "A && reboot # keep",
    "A && shutdown -rnow",
    "A && ./reboot",
    "A && /usr/bin/reboot",
    "A && echo '#' && reboot",
    "A && echo 'semi; reboot' ; shutdown -r now",
]


def hex_utf8(value: str) -> str:
    return value.encode("utf-8").hex()


def tokens(command: str) -> list[str] | dict[str, str]:
    lexer = shlex.shlex(command, posix=True, punctuation_chars=True)
    lexer.commenters = ""
    try:
        return list(lexer)
    except ValueError as error:
        return {"error": str(error)}


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--out", type=Path, default=ROOT / "contracts/golden/expect_reboot_chained.tsv")
    args = parser.parse_args()

    rows = []
    for index, command in enumerate(COMMANDS, start=1):
        expected_tokens = json.dumps(tokens(command), ensure_ascii=False, separators=(",", ":"))
        expected_segment = json.dumps(
            find_chained_reboot_segment(command), ensure_ascii=False, separators=(",", ":")
        )
        rows.append(
            "\t".join(
                (
                    f"case_{index:02}",
                    hex_utf8(command),
                    hex_utf8(expected_tokens),
                    hex_utf8(expected_segment),
                )
            )
        )

    args.out.parent.mkdir(parents=True, exist_ok=True)
    args.out.write_text("\n".join(rows) + "\n", encoding="utf-8", newline="\n")
    print(f"產生 {len(rows)} 個 Python golden：{args.out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
