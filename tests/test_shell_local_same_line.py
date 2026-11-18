"""檢查 shell 宣告不得在同一敘述引用較早宣告的變數。"""
from __future__ import annotations

import re
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
DECLARATIONS = {"local", "declare", "typeset", "readonly", "export"}
ASSIGNMENT = re.compile(r"^([A-Za-z_][A-Za-z0-9_]*)=(.*)$", re.DOTALL)
NAME = re.compile(r"[A-Za-z_][A-Za-z0-9_]*")

pytestmark = [pytest.mark.core_portable, pytest.mark.process_rpc]


def _shell_commands(source: str) -> list[list[tuple[str, int]]]:
    """以 shell 引號與命令分隔符切出簡單命令，保留每個字詞的行號。"""
    commands: list[list[tuple[str, int]]] = []
    command: list[tuple[str, int]] = []
    word: list[str] = []
    line = 1
    word_line = 1
    quote: str | None = None
    command_substitution = 0
    index = 0

    def finish_word() -> None:
        nonlocal word
        if word:
            command.append(("".join(word), word_line))
            word = []

    def finish_command() -> None:
        finish_word()
        if command:
            commands.append(command.copy())
            command.clear()

    def add(char: str) -> None:
        nonlocal word_line
        if not word:
            word_line = line
        word.append(char)

    while index < len(source):
        char = source[index]

        if quote == "'":
            add(char)
            if char == "'":
                quote = None
            if char == "\n":
                line += 1
            index += 1
            continue

        if char == "\\" and index + 1 < len(source):
            following = source[index + 1]
            if following == "\n":
                line += 1
                index += 2
                continue
            add(char)
            add(following)
            index += 2
            if following == "\n":
                line += 1
            continue

        if char == '"':
            add(char)
            quote = None if quote == '"' else '"'
            index += 1
            continue

        if quote is None and char == "'":
            add(char)
            quote = "'"
            index += 1
            continue

        if quote != "'" and source.startswith("$(", index):
            add("$")
            add("(")
            command_substitution += 1
            index += 2
            continue

        if command_substitution:
            if char == "(":
                command_substitution += 1
            elif char == ")":
                command_substitution -= 1
            add(char)
            if char == "\n":
                line += 1
            index += 1
            continue

        if quote is None and char == "#" and not word:
            while index < len(source) and source[index] != "\n":
                index += 1
            continue

        if quote is None and char in " \t\r":
            finish_word()
            index += 1
            continue

        if quote is None and char in "\n;|&":
            finish_command()
            if char in "|&" and index + 1 < len(source) and source[index + 1] == char:
                index += 1
            if char == "\n":
                line += 1
            index += 1
            continue

        if quote is None and char in "{}" and not word:
            finish_command()
            index += 1
            continue

        add(char)
        index += 1

    finish_command()
    return commands


def _variable_references(value: str) -> set[str]:
    references: set[str] = set()
    quote: str | None = None
    index = 0
    while index < len(value):
        char = value[index]
        if char == "\\" and quote != "'" and index + 1 < len(value):
            index += 2
            continue
        if char == "'" and quote in (None, "'"):
            quote = None if quote == "'" else "'"
            index += 1
            continue
        if char == '"' and quote in (None, '"'):
            quote = None if quote == '"' else '"'
            index += 1
            continue
        if char == "$" and quote != "'" and index + 1 < len(value):
            start = index + 1
            if value[start] == "{":
                start += 1
            match = NAME.match(value, start)
            if match:
                references.add(match.group())
                index = match.end()
                continue
        index += 1
    return references


def _declaration_index(command: list[tuple[str, int]]) -> int | None:
    tokens = [token for token, _ in command]
    index = 0
    while index < len(tokens) and tokens[index] in {
        "if",
        "then",
        "do",
        "else",
        "elif",
        "while",
        "until",
        "!",
    }:
        index += 1
    while index < len(tokens) and ASSIGNMENT.fullmatch(tokens[index]):
        index += 1
    if index < len(tokens) and tokens[index] == "builtin":
        index += 1
    if index < len(tokens) and tokens[index] in DECLARATIONS:
        return index
    return None


def _same_statement_references(source: str, path: str) -> list[str]:
    offenders: list[str] = []
    for command in _shell_commands(source):
        declaration_index = _declaration_index(command)
        if declaration_index is None:
            continue
        declared: set[str] = set()
        for token, lineno in command[declaration_index + 1 :]:
            assignment = ASSIGNMENT.fullmatch(token)
            if assignment is None:
                continue
            name, value = assignment.groups()
            earlier_references = _variable_references(value) & declared
            earlier_references.discard(name)
            for reference in sorted(earlier_references):
                offenders.append(
                    f"{path}:{lineno}: 同一宣告中的 {name} 引用先前宣告的 {reference}"
                )
            declared.add(name)
    return offenders


def _tracked_shell_scripts() -> list[Path]:
    output = subprocess.run(
        ["git", "ls-files", "*.sh"],
        cwd=ROOT,
        check=True,
        capture_output=True,
        text=True,
    ).stdout
    return [ROOT / path for path in output.splitlines() if path]


def test_pattern_detects_prior_reference_and_ignores_self_or_other_statements() -> None:
    sample = '''\
f() {
  local a=1 b="$a"
  local base=1 braced="${base}" defaulted="${base:-default}" literal='$base'
  local X="${X:-default}" PATH="/bin:$PATH" inherited="$outside"
  local earlier=1; local later="$earlier"
  declare declared=1 next="$declared"
  typeset typed=1 next="$typed"
  readonly fixed=1 next="$fixed"
  export exported=1 next="$exported"
  local split=1 \\
    wrapped="$split"
}
'''

    assert _same_statement_references(sample, "sample.sh") == [
        "sample.sh:2: 同一宣告中的 b 引用先前宣告的 a",
        "sample.sh:3: 同一宣告中的 braced 引用先前宣告的 base",
        "sample.sh:3: 同一宣告中的 defaulted 引用先前宣告的 base",
        "sample.sh:6: 同一宣告中的 next 引用先前宣告的 declared",
        "sample.sh:7: 同一宣告中的 next 引用先前宣告的 typed",
        "sample.sh:8: 同一宣告中的 next 引用先前宣告的 fixed",
        "sample.sh:9: 同一宣告中的 next 引用先前宣告的 exported",
        "sample.sh:11: 同一宣告中的 wrapped 引用先前宣告的 split",
    ]


def test_bash_nounset_same_statement_reference_exits_with_error() -> None:
    result = subprocess.run(
        ["bash", "-c", 'set -u; f(){ local a=1 b="$a"; echo "$b"; }; f'],
        capture_output=True,
        text=True,
        check=False,
    )

    assert result.returncode != 0
    assert "unbound variable" in result.stderr


def test_tracked_shell_scripts_have_no_prior_reference_in_declaration() -> None:
    offenders = []
    for path in _tracked_shell_scripts():
        relative = path.relative_to(ROOT).as_posix()
        source = path.read_text(encoding="utf-8", errors="replace")
        offenders.extend(_same_statement_references(source, relative))

    assert offenders == [], "同一宣告中引用先前變數：\n" + "\n".join(offenders)
