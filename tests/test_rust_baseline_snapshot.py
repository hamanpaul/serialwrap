"""OD-75：保存本次出口快照，拒絕沿用既有目錄或符號連結。"""
from __future__ import annotations

import subprocess

import pytest
from scripts.rust import l15_exit_driver as driver

pytestmark = [pytest.mark.posix_only, pytest.mark.process_rpc]


@pytest.mark.parametrize("existing", ["absent", "directory", "symlink"])
def test_snapshot_refuses_old_evidence_without_overwriting(tmp_path, existing):
    source = (driver._REPO_ROOT / "scripts/rust/stage1_exit_driver.sh").read_text()
    start = source.index("snapshot_p4_evidence() {")
    end = source.index("\n}", start) + 2
    origin = tmp_path / "origin"
    origin.mkdir()
    (origin / "report.json").write_text("new")
    dest = tmp_path / "snapshot"
    old = tmp_path / "old"
    if existing != "absent":
        old.mkdir()
        (old / "report.json").write_text("old")
        if existing == "symlink":
            dest.symlink_to(old, target_is_directory=True)
        else:
            old.rename(dest)
    proc = subprocess.run(["bash", "-c", source[start:end] + '\nsnapshot_p4_evidence "$1" "$2"',
                           "snapshot", str(origin), str(dest)], capture_output=True)
    assert proc.returncode == (0 if existing == "absent" else 1)
    assert (dest / "report.json").read_text() == ("new" if existing == "absent" else "old")

