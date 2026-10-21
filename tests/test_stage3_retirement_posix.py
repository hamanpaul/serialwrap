"""Python daemon 退役切換（P9-T16）：POSIX 限定的 mkbundle 計畫與符號連結守門（紅燈）。

標記說明：模組層只放平台標記 `posix_only`；資源標記依函式放置（`process_rpc`＝呼叫 bash 子程序，
`memory_only`＝純記憶體），理由同 `test_stage3_retirement.py`（P0 規則：混合時放函式層）。
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import tarfile
from pathlib import Path

import pytest

import fakes_retirement

pytestmark = [pytest.mark.posix_only]

ROOT = Path(__file__).resolve().parents[1]
MKBUNDLE = ROOT / "scripts" / "rust" / "mkbundle.sh"
LINUX_TARGET = "x86_64-unknown-linux-gnu"


def _retirement():
    from tools.stage3 import retirement

    return retirement


def _run_mkbundle(cwd: Path, *args: str) -> subprocess.CompletedProcess:
    return subprocess.run(["bash", str(cwd / "scripts" / "rust" / "mkbundle.sh"), *args],
                          cwd=cwd, capture_output=True, text=True, timeout=300)


def _mini_repo(root: Path, retirement_doc: dict) -> Path:
    """建立只含 mkbundle.sh、tools/stage3 與指定 retirement 檔的暫存 repo（不動真實 packaging 檔）。"""
    mini = root / "mini"
    (mini / "scripts" / "rust").mkdir(parents=True)
    shutil.copy2(MKBUNDLE, mini / "scripts" / "rust" / "mkbundle.sh")
    ignore = shutil.ignore_patterns("__pycache__", "*.pyc")
    (mini / "tools").mkdir(parents=True)
    if (ROOT / "tools" / "__init__.py").exists():
        shutil.copy2(ROOT / "tools" / "__init__.py", mini / "tools" / "__init__.py")
    if (ROOT / "tools" / "stage3").is_dir():
        shutil.copytree(ROOT / "tools" / "stage3", mini / "tools" / "stage3", ignore=ignore)
    (mini / "packaging").mkdir(parents=True)
    (mini / "packaging" / "python_daemon_retirement.json").write_text(
        json.dumps(retirement_doc, ensure_ascii=False, indent=1, sort_keys=True) + "\n", encoding="utf-8")
    return mini


@pytest.mark.process_rpc
def test_mkbundle_plan_only_stage3_candidate():
    r = subprocess.run(["bash", str(MKBUNDLE), "--target", LINUX_TARGET, "--stage", "3", "--plan-only"],
                       cwd=ROOT, capture_output=True, text=True, timeout=300)
    assert r.returncode == 0, r.stderr
    plan = json.loads(r.stdout.strip().splitlines()[-1])
    assert plan["candidate"] is True and plan["publishable"] is False


@pytest.mark.process_rpc
def test_mkbundle_plan_only_stage2_after_retire_refused(tmp_path):
    retired = {"retired": True, "evidence_index": "docs/rust-refactor/evidence/index.json",
               "evidence_index_sha256": "0" * 64, "approved_by": "owner", "approved_at": "2026-11-30T01:00:00+00:00",
               "decision_ref": "OD-14", "effective_bundle_version": "1.0.0",
               "stage3_target_artifact_ids": {"linux": "0" * 64, "windows": "0" * 64}}
    mini = _mini_repo(tmp_path, retired)
    r = _run_mkbundle(mini, "--target", LINUX_TARGET, "--stage", "2.4", "--plan-only")
    assert r.returncode == 2
    assert "HARNESS_RETIREMENT_INCONSISTENT" in r.stdout + r.stderr


@pytest.mark.process_rpc
def test_mkbundle_accepts_existing_flags(tmp_path):
    out = tmp_path / "out"
    r = subprocess.run(["bash", str(MKBUNDLE), "--target", LINUX_TARGET, "--dev", "--out", str(out), "--plan-only"],
                       cwd=ROOT, capture_output=True, text=True, timeout=300)
    assert r.returncode == 0, r.stdout + r.stderr
    plan = json.loads(r.stdout.strip().splitlines()[-1])
    assert plan["stage"] == "1A"
    assert not out.exists()                                                     # --plan-only 不 build


@pytest.mark.process_rpc
def test_mkbundle_does_not_print_unbuilt_bundle_dir():
    r = _run_mkbundle(ROOT, "--print-dir")
    assert r.returncode == 2
    assert "HARNESS_BUILD_UNAVAILABLE" in r.stdout + r.stderr
    assert r.stdout == ""


@pytest.mark.memory_only
def test_pack_bundle_preserves_hardlinked_file_bytes(tmp_path):
    source = tmp_path / "bundle"
    source.mkdir()
    first = source / "first.bin"
    second = source / "second.bin"
    payload = b"hardlinked release payload"
    first.write_bytes(payload)
    os.link(first, second)
    archive_path = tmp_path / "bundle.tar.gz"

    _retirement().pack_bundle(source, archive_path)

    with tarfile.open(archive_path, "r:gz") as archive:
        members = {member.name: member for member in archive.getmembers()}
        assert members["second.bin"].isfile()
        assert members["second.bin"].size == len(payload)
        assert archive.extractfile("second.bin").read() == payload


@pytest.mark.memory_only
def test_verify_archive_handles_null_target_id_map(tmp_path):
    repo = tmp_path / "repo"
    index_path = repo / "docs" / "rust-refactor" / "evidence" / "index.json"
    index_path.parent.mkdir(parents=True)
    index_path.write_text(json.dumps({"stage_targets": {"3": {"linux": {}}}}), encoding="utf-8")
    archive_path = tmp_path / "empty-linux.tar.gz"
    with tarfile.open(archive_path, "w:gz"):
        pass

    violations, _ = _retirement().verify_archive(
        {"retired": True, "evidence_index": "docs/rust-refactor/evidence/index.json",
         "stage3_target_artifact_ids": None},
        repo_root=repo,
        platform="linux",
        archive=archive_path,
    )

    assert "retirement: archive manifest unreadable: linux" in violations


@pytest.mark.memory_only
def test_release_refuses_symlink_in_bundle(tmp_path):                         # R2-F7：符號連結指向同位元組的複本仍不是一般檔
    repo = fakes_retirement.retirement_repo(tmp_path, stage3_result="PASS")
    r = fakes_retirement.approved(repo)
    bundle = repo / "bundles" / "linux"
    served = bundle / "bin" / "serialwrapd"
    copy = tmp_path / "serialwrapd-copy"
    copy.write_bytes(served.read_bytes())
    served.unlink()
    served.symlink_to(copy)
    v = _retirement().verify(r, repo_root=repo, mode="release", evidence_root=repo / "evidence",
                             bundle_manifests=fakes_retirement.bundle_args(repo))
    assert "retirement: bundle non-regular file: linux" in v


@pytest.mark.memory_only
def test_verify_archive_refuses_symlink_member(tmp_path):                     # R2-F7：archive 內的符號連結成員
    repo = fakes_retirement.retirement_repo(tmp_path, stage3_result="PASS")
    r = fakes_retirement.approved(repo)
    bundle = repo / "bundles" / "linux"
    archive = tmp_path / "sym.tar.gz"
    link = tmp_path / "serialwrapd-link"
    link.symlink_to(bundle / "bin" / "serialwrapd")
    with tarfile.open(archive, "w:gz") as tf:
        tf.add(bundle / "release-manifest.json", arcname="release-manifest.json")
        tf.add(link, arcname="bin/serialwrapd")                                 # 預設 dereference=False → 以 symlink 成員寫入
    v, _ = _retirement().verify_archive(r, repo_root=repo, platform="linux", archive=archive)
    assert "retirement: archive unsafe member: bin/serialwrapd" in v
