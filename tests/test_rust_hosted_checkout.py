"""Windows autocrlf 檢出不得改寫 Rust 內嵌資料的位元組。"""
from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
pytestmark = [pytest.mark.core_portable, pytest.mark.process_rpc]


@pytest.mark.parametrize(
    "rel",
    [
        "crates/sw-proto/src/pyre/unicode_names.tsv",
        "sw_core/assets/skill/SKILL.md",
        "sw_core/assets/skill/SKILL_WINDOWS.md",
        "sw_core/assets/profiles/default.yaml",
        "sw_core/assets/profiles/brcm.env.example",
        "sw_core/assets/tools/minicom_router.sh",
        "sw_core/assets/tools/minicom-broker.sh",
        "sw_core/assets/tools/minicom-raw.sh",
    ],
)
def test_windows_autocrlf_preserves_embedded_asset_bytes(tmp_path, rel):
    repo = tmp_path / "repo"
    repo.mkdir()

    def git(*args):
        return subprocess.run(
            ["git", "-C", str(repo), *args],
            check=True,
            capture_output=True,
        )

    git("init", "-q")
    git("config", "core.autocrlf", "true")
    (repo / ".gitattributes").write_bytes((ROOT / ".gitattributes").read_bytes())
    source = (ROOT / rel).read_bytes()
    target = repo / rel
    target.parent.mkdir(parents=True)
    target.write_bytes(source)
    git("add", ".gitattributes", rel)
    target.unlink()
    git("checkout-index", "--", rel)
    checked_out = target.read_bytes()
    assert b"\r\n" not in checked_out, rel
    assert checked_out == source, rel
