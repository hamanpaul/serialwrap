"""win-pytest.sh 的 staging 複製必須讓 staging repo 的 `git status` 乾淨（含 symlink）。

候選版 36ef2d7b 的 Windows 原生 py_parity（F-S19）證據被拒：`cp --parents` 解參考了 `AGENTS.md`、
`GEMINI.md`、`.github/copilot-instructions.md` 三個 symlink，Windows git 判為已修改，provenance clean=false。
本測試以 Linux git 的 core.symlinks=false 模擬 Windows git 的行為。
"""
from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
LIB = ROOT / "scripts/stage0/lib-stage-copy.sh"

pytestmark = [pytest.mark.posix_only, pytest.mark.process_rpc]


def _git(cwd: Path, *args: str) -> str:
    return subprocess.run(["git", "-C", str(cwd), *args], check=True, capture_output=True, text=True).stdout


def _make_repo(tmp_path: Path) -> Path:
    repo = tmp_path / "repo"
    (repo / ".github").mkdir(parents=True)
    _git(tmp_path, "init", "-q", str(repo))
    _git(repo, "config", "user.email", "t@example.invalid")
    _git(repo, "config", "user.name", "t")
    (repo / "CLAUDE.md").write_text("policy\n", encoding="utf-8")
    (repo / "AGENTS.md").symlink_to("CLAUDE.md")
    (repo / ".github" / "copilot-instructions.md").symlink_to("../CLAUDE.md")
    (repo / "plain.txt").write_text("x\n", encoding="utf-8")
    _git(repo, "add", "-A")
    _git(repo, "commit", "-q", "-m", "init")
    return repo


def _stage(repo: Path, stage: Path, copy_cmd: str) -> str:
    manifest = repo.parent / "files.list"
    manifest.write_bytes(subprocess.run(
        ["git", "-C", str(repo), "ls-files", "-z", "--cached", "--others", "--exclude-standard", "--deduplicate"],
        check=True, capture_output=True).stdout)
    stage.mkdir()
    subprocess.run(["bash", "-c", copy_cmd, "_", str(repo), str(stage), str(manifest)], check=True)
    _git(stage, "init", "-q")
    _git(stage, "config", "--local", "core.symlinks", "false")
    _git(stage, "config", "--local", "core.autocrlf", "false")
    _git(stage, "fetch", "-q", "--no-tags", str(repo), "HEAD")
    _git(stage, "update-ref", "HEAD", "FETCH_HEAD")
    _git(stage, "read-tree", "HEAD")
    return _git(stage, "status", "--porcelain")


def test_stage_copy_keeps_symlinks_clean(tmp_path: Path) -> None:
    repo = _make_repo(tmp_path)
    status = _stage(repo, tmp_path / "stage", f'source "{LIB}"; stage_copy_repo "$1" "$2" "$3"')
    assert status == "", status
    assert (tmp_path / "stage" / "AGENTS.md").read_text(encoding="utf-8") == "CLAUDE.md"


def test_dereferencing_copy_would_be_dirty(tmp_path: Path) -> None:
    """反例：舊作法（cp --parents 解參考 symlink）會讓 staging 不乾淨，證明上面的測試能抓到這個缺陷。"""
    repo = _make_repo(tmp_path)
    old = ('cd "$1"; while IFS= read -r -d "" p; do [[ -e "$p" || -L "$p" ]] || continue; '
           'cp --parents --target-directory="$2" -- "$p"; done < "$3"')
    status = _stage(repo, tmp_path / "stage", old)
    assert "AGENTS.md" in status


def test_win_pytest_uses_stage_copy_library() -> None:
    text = (ROOT / "scripts/stage0/win-pytest.sh").read_text(encoding="utf-8")
    assert 'stage_copy_repo "$repo" "$stage" "$manifest"' in text
    assert "core.symlinks false" in text
