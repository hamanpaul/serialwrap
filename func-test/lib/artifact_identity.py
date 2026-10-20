"""L1.5 報告使用的 CLI、daemon 與程式碼樹身分。"""
from __future__ import annotations

import pathlib
import subprocess
import sys
from typing import Any

ROOT_DIR = pathlib.Path(__file__).resolve().parents[2]
BRIDGE_SRC = ROOT_DIR / "tools" / "uart_stdio_bridge" / "src"
if str(BRIDGE_SRC) not in sys.path:
    sys.path.insert(0, str(BRIDGE_SRC))

from uart_stdio_bridge.identity import (  # noqa: E402
    file_sha256,
    package_tree_sha256,
    repo_tree_sha256,
)


def _release_manifest_for_cli(cli_path: pathlib.Path) -> pathlib.Path | None:
    candidate = cli_path.parent.parent / "release-manifest.json"
    return candidate if candidate.is_file() else None


def collect_artifacts(
    cli_argv: list[str], daemon_argv: list[str], repo_root: pathlib.Path
) -> dict[str, Any]:
    """收集實際解析到的 CLI／daemon 身分與 Python 程式碼摘要。"""
    if not cli_argv or not daemon_argv:
        raise ValueError("CLI 與 daemon argv 均不得為空")
    cli_path = pathlib.Path(cli_argv[0]).expanduser().resolve()
    try:
        version_proc = subprocess.run(
            [str(cli_path), "--version"],
            capture_output=True,
            text=True,
            timeout=5.0,
            check=False,
        )
        version_stdout = version_proc.stdout
    except (OSError, subprocess.TimeoutExpired):
        version_stdout = ""

    manifest_path = _release_manifest_for_cli(cli_path)
    release_manifest_sha256 = file_sha256(manifest_path) if manifest_path else None
    daemon_path = pathlib.Path(daemon_argv[0]).expanduser().resolve()
    is_python = any(
        arg.endswith("serialwrapd.py")
        or (arg == "-m" and i + 1 < len(daemon_argv) and daemon_argv[i + 1] == "sw_core.daemon")
        for i, arg in enumerate(daemon_argv)
    )
    if is_python:
        daemon_info: dict[str, Any] = {
            "argv": list(daemon_argv),
            "sha256": None,
            "implementation": "python",
            "package_tree_sha256": package_tree_sha256(repo_root),
            "repo_tree_sha256": repo_tree_sha256(repo_root),
        }
    else:
        daemon_info = {
            "argv": list(daemon_argv),
            "sha256": file_sha256(daemon_path) if daemon_path.is_file() else None,
            "implementation": "rust",
            "package_tree_sha256": None,
            "repo_tree_sha256": None,
        }
    return {
        "cli": {
            "path": str(cli_path),
            "sha256": file_sha256(cli_path) if cli_path.is_file() else None,
            "version_stdout": version_stdout,
        },
        "daemon": daemon_info,
        "release_manifest_sha256": release_manifest_sha256,
    }


__all__ = ["collect_artifacts"]
