#!/usr/bin/env python3
"""確認正式 tag 沿用同版 RC，且 RC 後沒有改動執行檔來源。"""
from __future__ import annotations

import argparse
import re
import subprocess
import sys
from pathlib import Path
from typing import Sequence

_SHA_RE = re.compile(r"^[0-9a-f]{40}$")
_STATUS_LINE_RE = re.compile(r'^\s+status:\s+"(?:APPROVED|IMPLEMENTED|VERIFIED)"$')
_BEHAVIOR_PATH = "contracts/behavior_changes.yaml"
_BREAKER_SNAPSHOT_DIR = "tests/acceptance/breaker"


class PromoteGuardError(ValueError):
    """RC 與正式 tag 之間的歷史不符合 promote 條件。"""


def _git(repo: Path, *args: str, check: bool = True) -> subprocess.CompletedProcess[str]:
    result = subprocess.run(
        ["git", "-C", str(repo), *args], capture_output=True, text=True, check=False,
    )
    if check and result.returncode != 0:
        raise PromoteGuardError(f"git {' '.join(args)} 失敗：{result.stderr.strip()}")
    return result


def _status_only_change(repo: Path, rc_commit: str, release_commit: str) -> None:
    diff = _git(
        repo, "diff", "--no-ext-diff", "--unified=0", rc_commit, release_commit, "--", _BEHAVIOR_PATH,
    ).stdout
    removed: list[str] = []
    added: list[str] = []
    for line in diff.splitlines():
        if line.startswith(("diff ", "index ", "--- ", "+++ ", "@@")):
            continue
        if line.startswith("-"):
            removed.append(line[1:])
        elif line.startswith("+"):
            added.append(line[1:])
    if not removed and not added:
        raise PromoteGuardError(f"{_BEHAVIOR_PATH} 被列為變更路徑，但無法確認純 status 欄位差異")
    if len(removed) != len(added):
        raise PromoteGuardError(f"{_BEHAVIOR_PATH} 不是成對的 status 欄位變更")
    if any(not _STATUS_LINE_RE.fullmatch(line) for line in (*removed, *added)):
        raise PromoteGuardError(f"{_BEHAVIOR_PATH} 在 RC 後只允許變更 status 欄位")


def _allowed_path(path: str) -> bool:
    if path.startswith("docs/") or path.startswith("changelog.d/") or path == "CHANGELOG.md":
        return True
    path_obj = Path(path)
    if path_obj.parent.as_posix() == _BREAKER_SNAPSHOT_DIR and path_obj.name.endswith(".snapshots.ndjson"):
        return True
    return path == _BEHAVIOR_PATH


def validate_promote_range(repo: Path, rc_commit: str, release_commit: str) -> list[str]:
    """要求 RC 為正式 tag 祖先，且提交差異只觸及明確核准的非執行檔路徑。"""
    repo = repo.resolve(strict=True)
    for label, revision in (("RC", rc_commit), ("正式 tag", release_commit)):
        if _SHA_RE.fullmatch(revision) is None:
            raise PromoteGuardError(f"{label} commit 必須是完整 40 位小寫 Git SHA：{revision!r}")
        _git(repo, "cat-file", "-e", f"{revision}^{{commit}}")

    ancestor = _git(repo, "merge-base", "--is-ancestor", rc_commit, release_commit, check=False)
    if ancestor.returncode != 0:
        raise PromoteGuardError(f"RC commit {rc_commit} 不是正式 tag commit {release_commit} 的祖先")

    changed_raw = _git(repo, "diff", "--name-only", "-z", "--no-renames", rc_commit, release_commit).stdout
    changed_paths = sorted(path for path in changed_raw.split("\0") if path)
    rejected = [path for path in changed_paths if not _allowed_path(path)]
    if rejected:
        raise PromoteGuardError("RC 後差異含不允許路徑：" + ", ".join(rejected))
    if _BEHAVIOR_PATH in changed_paths:
        _status_only_change(repo, rc_commit, release_commit)
    return changed_paths


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--repo", type=Path, required=True, help="含 RC 與正式 tag commit 的 checkout")
    parser.add_argument("--rc-commit", required=True, help="已通過 gate 的 RC 完整 commit SHA")
    parser.add_argument("--release-commit", required=True, help="正式 tag 指向的完整 commit SHA")
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        changed = validate_promote_range(args.repo, args.rc_commit, args.release_commit)
    except (OSError, PromoteGuardError) as exc:
        print(f"release_promote_guard: 拒絕 promote：{exc}", file=sys.stderr)
        return 1
    print(
        f"release_promote_guard: PASS rc={args.rc_commit} release={args.release_commit} "
        f"allowed_changes={changed}"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
