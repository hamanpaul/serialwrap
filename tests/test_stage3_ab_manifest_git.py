"""Stage 3 T3：以隔離 Git repo 驗證原始 blob 位元組。"""

from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

import stage3_fakes as fakes
from test_stage3_ab_manifest import _windows_l15_transport_fixture
from tools.stage3 import ab_manifest
from tools.stage3.proc import run_cmd

pytestmark = [pytest.mark.posix_only, pytest.mark.process_rpc]


def _git(root: Path, *args: str) -> str:
    result = subprocess.run(
        ["git", "-C", str(root), *args],
        check=True,
        capture_output=True,
        text=True,
        encoding="utf-8",
    )
    return result.stdout.strip()


def test_tool_revision_mapping_check_compares_raw_bytes_under_autocrlf(tmp_path):
    manifest_path = _windows_l15_transport_fixture(tmp_path)
    manifest = ab_manifest.load_manifest(manifest_path, repo_root=tmp_path)
    map_ref = manifest.platform_settings["case_transport_map"]
    map_path = tmp_path / map_ref["path"]

    _git(tmp_path, "init", "-q")
    _git(tmp_path, "config", "user.email", "t3-test@example.invalid")
    _git(tmp_path, "config", "user.name", "T3 test")
    _git(tmp_path, "config", "core.autocrlf", "true")
    _git(tmp_path, "add", "--", map_ref["path"])
    _git(tmp_path, "commit", "-q", "-m", "synthetic tool revision")
    revision = _git(tmp_path, "rev-parse", "HEAD")

    assert ab_manifest._tool_revision_map_errors(
        manifest, repo_root=tmp_path, run=run_cmd, tool_revision=revision,
    ) == []

    raw_lf = map_path.read_bytes()
    assert b"\r\n" not in raw_lf
    map_path.write_bytes(raw_lf.replace(b"\n", b"\r\n"))
    assert ab_manifest._tool_revision_map_errors(
        manifest, repo_root=tmp_path, run=run_cmd, tool_revision=revision,
    ) == [f"case transport map tool revision mismatch: {map_ref['path']}"]
