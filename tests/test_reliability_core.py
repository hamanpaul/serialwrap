"""Phase 2 plugin core（serialwrap_reliability.core）純邏輯單測——不 import testpilot、不碰 live。"""
from __future__ import annotations

import re
import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT / "reliability"))

from serialwrap_reliability import core  # noqa: E402


def test_repo_root_locates_worktree():
    assert core.REPO_ROOT == REPO_ROOT
    assert (core.REPO_ROOT / "realhw" / "harness.py").is_file()


def test_resolve_repo_root_rejects_non_editable_layout(tmp_path):
    fake = tmp_path / "venv" / "lib" / "python3.12" / "site-packages" / "serialwrap_reliability" / "core.py"
    fake.parent.mkdir(parents=True)
    fake.write_text("", encoding="utf-8")
    with pytest.raises(RuntimeError, match="editable 安裝"):
        core.resolve_repo_root(fake)


def test_ensure_realhw_importable_idempotent():
    got = core.ensure_realhw_importable()
    assert got == REPO_ROOT
    import realhw  # noqa: F401

    before = list(sys.path)
    core.ensure_realhw_importable()
    assert sys.path.count(str(REPO_ROOT)) == before.count(str(REPO_ROOT))


def test_load_registry_populated_unique():
    registry = core.load_registry()
    ids = [case.id for case in registry]
    assert len(ids) == len(set(ids))
    assert len(ids) >= 29
    assert any(case.id == "p0-doctor" for case in registry)


def test_core_modules_do_not_import_testpilot():
    pkg = REPO_ROOT / "reliability" / "serialwrap_reliability"
    for name in ("__init__.py", "core.py"):
        text = (pkg / name).read_text(encoding="utf-8")
        bad = [line for line in text.splitlines() if re.match(r"\s*(import|from)\s+testpilot", line)]
        assert not bad, f"{name} 不得 import testpilot：{bad}"
