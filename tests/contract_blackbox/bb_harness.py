"""黑箱契約 harness 公開介面（P3-T2 紅燈階段 stub）。

只提供 plan 規定的公開名稱與簽章；函式本體一律 raise NotImplementedError("P3-T2")，
實作於 Step 4 完成。stdlib-only，不得 import sw_core／serialwrap／serialwrapd。
"""
from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path
from typing import Mapping, Sequence

NL: bytes = b"\r\n" if os.name == "nt" else b"\n"


@dataclass(frozen=True)
class CliResult:
    rc: int
    stdout: bytes
    stderr: bytes


def repo_root() -> Path:
    raise NotImplementedError("P3-T2")


def ab_env() -> dict[str, str]:
    raise NotImplementedError("P3-T2")


def ab_label() -> str:
    raise NotImplementedError("P3-T2")


def is_rust() -> bool:
    raise NotImplementedError("P3-T2")


def cli_prefix() -> list[str]:
    raise NotImplementedError("P3-T2")


def python_baseline_prefix() -> list[str]:
    raise NotImplementedError("P3-T2")


def python_baseline_interpreter() -> list[str]:
    raise NotImplementedError("P3-T2")


def daemon_prefix() -> list[str]:
    raise NotImplementedError("P3-T2")


def isolated_env(tmp: Path, **overrides: str | None) -> dict[str, str]:
    raise NotImplementedError("P3-T2")


def run_cli(args: Sequence[str], *, env: Mapping[str, str], stdin: bytes | None = None,
            timeout_s: float = 60.0, merge_stderr: bool = False, cwd: Path | None = None) -> CliResult:
    raise NotImplementedError("P3-T2")


def stdout_lines(r: CliResult) -> list[str]:
    raise NotImplementedError("P3-T2")
