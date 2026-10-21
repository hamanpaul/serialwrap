"""P9-T16：退役 gate 在純標準函式庫環境下仍可執行。"""

from __future__ import annotations

import json
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

pytestmark = [pytest.mark.core_portable, pytest.mark.process_rpc]

ROOT = Path(__file__).resolve().parents[1]


def _run_github_output(tmp_path: Path, retired: object) -> subprocess.CompletedProcess[str]:
    repo = tmp_path / "mini-repo"
    ignore = shutil.ignore_patterns("__pycache__", "*.pyc")
    shutil.copytree(ROOT / "tools", repo / "tools", ignore=ignore)
    packaging = repo / "packaging"
    packaging.mkdir(parents=True)
    (packaging / "python_daemon_retirement.json").write_text(
        json.dumps({"retired": retired}), encoding="utf-8"
    )
    script = (
        "import runpy, sys; "
        "sys.modules['yaml'] = None; sys.modules['realhw'] = None; "
        f"sys.argv = ['tools.stage3', 'retirement', 'github-output', '--repo-root', {str(repo)!r}]; "
        "runpy.run_module('tools.stage3', run_name='__main__')"
    )
    try:
        return subprocess.run(
            [sys.executable, "-S", "-c", f"import sys; sys.path.insert(0, {str(repo)!r}); {script}"],
            cwd=repo,
            capture_output=True,
            text=True,
            check=False,
        )
    finally:
        shutil.rmtree(repo)


def test_github_output_runs_without_yaml_or_realhw(tmp_path: Path) -> None:
    result = _run_github_output(tmp_path, False)

    assert result.returncode == 0, result.stderr
    assert result.stdout == "retired=false\n"


@pytest.mark.parametrize("retired", ["true", 1, None])
def test_github_output_rejects_non_boolean_retirement(tmp_path: Path, retired: object) -> None:
    result = _run_github_output(tmp_path, retired)

    assert result.returncode != 0
    assert "HARNESS_RETIREMENT_INCONSISTENT" in result.stdout + result.stderr
    assert "retired=false" not in result.stdout
