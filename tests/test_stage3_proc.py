from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

from tools.stage3.proc import run_cmd

pytestmark = [pytest.mark.core_portable, pytest.mark.process_rpc]

ROOT = Path(__file__).resolve().parents[1]


def test_run_cmd_timeout():
    result = run_cmd([sys.executable, "-c", "import time;time.sleep(5)"], timeout=0.2)
    assert result.rc == -1 and result.timed_out


def test_run_cmd_forwards_env_and_cwd(tmp_path):
    code = "import os,sys;sys.stdout.write(os.environ['S3_T1_PROBE'] + '|' + os.getcwd())"
    env = {**os.environ, "S3_T1_PROBE": "probe-42"}
    result = run_cmd([sys.executable, "-c", code], env=env, cwd=tmp_path, timeout=30)
    assert result.rc == 0 and not result.timed_out
    value, cwd = result.stdout.split("|", 1)
    assert value == "probe-42"
    assert Path(cwd).resolve() == tmp_path.resolve()


def test_run_cmd_bytes_output_preserves_nul_and_control_bytes():
    payload = b"reports/name\r\npart.json\0reports/next\nname.json\0"
    code = "import sys;sys.stdout.buffer.write(" + repr(payload) + ")"
    result = run_cmd([sys.executable, "-c", code], bytes_output=True, timeout=30)

    assert result.rc == 0 and not result.timed_out
    assert result.stdout == payload


def test_spawn_writes_output_to_pid_named_log(tmp_path):
    from tools.stage3 import proc

    handle = proc.spawn([sys.executable, "-c", "import sys;sys.stdout.write('hello')"], cwd=tmp_path)
    assert handle.wait(timeout=20) == 0
    assert (tmp_path / f"spawn-{handle.pid}.log").read_bytes() == b"hello"
    assert handle.log_error is None


def test_spawn_keeps_draining_after_log_write_failure(tmp_path):
    from tools.stage3 import proc

    code = "import sys,time;time.sleep(0.5);sys.stdout.buffer.write(b'x' * (1 << 20));sys.stdout.flush()"
    handle = proc.spawn([sys.executable, "-c", code], cwd=tmp_path)
    handle._log.close()
    assert handle.wait(timeout=20) == 0
    assert handle.log_error


def test_public_seed_git_show_matches_manifest_and_attachments():
    from tools.stage3 import required_cases

    evidence_root = ROOT / "docs" / "rust-refactor" / "evidence"
    manifest = json.loads((evidence_root / "denominators" / "source-manifest.json").read_text(encoding="utf-8"))

    def git_show(revision: str, repo_path: str) -> bytes:
        result = subprocess.run(
            ["git", "show", f"{revision}:{repo_path}"], cwd=ROOT, capture_output=True, check=False
        )
        assert result.returncode == 0, result.stderr.decode("utf-8", errors="replace")
        return result.stdout

    assert required_cases.verify_seed_sources(manifest, evidence_root=evidence_root, source_reader=git_show) == []


def _init_repo(tmp_path: Path) -> Path:
    repo = tmp_path / "repo"
    repo.mkdir()
    git = ["git", "-c", "user.name=stage3-test", "-c", "user.email=stage3-test@example.invalid",
           "-c", "commit.gpgsign=false", "-c", "core.autocrlf=false"]
    subprocess.run(git + ["init", "-q"], cwd=repo, check=True)
    (repo / "present.txt").write_bytes(b"a\nb\n")
    subprocess.run(git + ["add", "present.txt"], cwd=repo, check=True)
    subprocess.run(git + ["commit", "-q", "-m", "init"], cwd=repo, check=True)
    return repo


def test_git_show_missing_path_is_locale_independent(tmp_path, monkeypatch):
    # 「路徑不在 revision」的判定不得依賴 git 英文訊息；非英文 locale（例如 zh_TW）下首個 PR 的 base 無鎖仍須回 None
    from tools.stage3.required_cases import _git_show

    repo = _init_repo(tmp_path)
    for key, value in (("LANGUAGE", "zh_TW"), ("LC_ALL", "zh_TW.UTF-8"), ("LANG", "zh_TW.UTF-8")):
        monkeypatch.setenv(key, value)
    assert _git_show(repo, "HEAD", "present.txt") == b"a\nb\n"
    assert _git_show(repo, "HEAD", "tests/acceptance/missing.json") is None
    (repo / "untracked.json").write_text("{}", encoding="utf-8")       # 磁碟上有、revision 沒有
    assert _git_show(repo, "HEAD", "untracked.json") is None


def test_git_show_unknown_revision_raises(tmp_path):
    from tools.stage3.required_cases import _git_show

    repo = _init_repo(tmp_path)
    with pytest.raises(ValueError):
        _git_show(repo, "0" * 40, "present.txt")
    with pytest.raises(ValueError):
        _git_show(repo, "no-such-ref", "present.txt")
