"""以暫存 Git 歷史驗證出口索引的逐提交只增規則。"""

from __future__ import annotations

import json
import subprocess
from pathlib import Path

import pytest

from tools.stage3 import evidence_index

pytestmark = [pytest.mark.core_portable, pytest.mark.process_rpc]


INDEX_PATH = "docs/rust-refactor/evidence/index.json"


class HistoryGuardError(AssertionError):
    """暫存 PR 歷史無法安全驗證或只增檢查失敗。"""


def _git(
    repo: Path,
    *args: str,
    input_bytes: bytes | None = None,
    check: bool = True,
) -> str:
    result = _git_result(repo, *args, input_bytes=input_bytes)
    if check and result.returncode:
        raise HistoryGuardError(
            f"git {' '.join(args)} 失敗（{result.returncode}）："
            f"{result.stderr.decode('utf-8', errors='replace').strip()}"
        )
    return result.stdout.decode("utf-8", errors="replace").strip()


def _git_result(repo: Path, *args: str, input_bytes: bytes | None = None) -> subprocess.CompletedProcess[bytes]:
    return subprocess.run(
        ["git", *args],
        cwd=repo,
        input=input_bytes,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        check=False,
    )


def _init_repo(path: Path) -> Path:
    path.mkdir(parents=True, exist_ok=True)
    _git(path, "init", "--quiet")
    _git(path, "config", "user.name", "Stage3 Test")
    _git(path, "config", "user.email", "stage3-test@example.invalid")
    _git(path, "config", "core.autocrlf", "true")
    _git(path, "config", "commit.gpgsign", "false")
    _git(path, "symbolic-ref", "HEAD", "refs/heads/main")
    return path


def _row(entry_id: str, *, summary: str = "去識別測試摘要") -> dict[str, str]:
    """建立可公開驗證的受限 FAIL 列，避免測試夾帶原始內容。"""
    return {
        "entry_id": entry_id,
        "result": "FAIL",
        "source_visibility": "restricted",
        "artifact_id": "a" * 64,
        "platform": "linux",
        "item_id": "S3-ROLLBACK-LINUX",
        "candidate_round_id": "b" * 32,
        "context_id": "c" * 64,
        "private_source_sha256": "d" * 64,
        "failure_summary": summary,
        "verified_by": "fixture-reviewer",
        "verified_at": "2026-11-01T00:00:00+00:00",
    }


def _ordinary_row(entry_id: str, *, result: str = "FAIL") -> dict:
    """建立一般 Stage 3 entry 的完整索引欄位。"""
    return {
        "exit_catalog_sha256": "e" * 64,
        "candidate_round_id": "b" * 32,
        "archive_sha256": "a" * 64,
        "entry_id": entry_id,
        "item_id": "S3-ROLLBACK-LINUX",
        "platform": "linux",
        "result": result,
        "artifact_id": "a" * 64,
        "runtime_digest": "f" * 64,
        "bundle_tree_sha256": "1" * 64,
        "runtime_tree_sha256": "2" * 64,
        "contract_revision": "c" * 40,
        "tool_revision": "t" * 40,
        "case_list_sha256": "0" * 64,
        "required_cases_sha256": "3" * 64,
        "l1_set_sha256": None,
        "workload_sha256": None,
        "environment_id": "4" * 64,
        "produced_at": "2026-11-01T00:00:00+00:00",
        "started_at": "2026-11-01T00:00:00+00:00",
        "producer": "ingest:drill-report",
        "files": [{"path": "fixture/report.json", "sha256": "5" * 64}],
        "xfail_cases": [],
        "reuse_of": None,
        "note": "fixture",
        "ledger_through": None,
        "ledger_records": None,
    }


def _index_bytes(entries: list[dict]) -> bytes:
    value = {
        "candidate_rounds": [],
        "current_candidate_round_id": None,
        "entries": entries,
        "legacy_obligations": {"resolutions": None, "scope": None},
        "schema_version": 2,
        "stage_targets": {},
        "target_history": [],
    }
    return (json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":")) + "\n").encode("utf-8")


def _commit_tree(
    repo: Path,
    files: dict[str, bytes],
    *,
    parents: tuple[str, ...] = (),
    message: str,
) -> str:
    """用 Git plumbing 建立真實 commit，避免依賴平台專屬 shell 或 checkout。"""
    _git(repo, "read-tree", "--empty")
    for name, content in sorted(files.items()):
        blob = _git(repo, "hash-object", "-w", "--stdin", input_bytes=content)
        _git(repo, "update-index", "--add", "--cacheinfo", "100644", blob, name)
    tree = _git(repo, "write-tree")
    argv = ["commit-tree", tree]
    for parent in parents:
        argv.extend(("-p", parent))
    argv.extend(("-m", message))
    return _git(repo, *argv)


def _set_ref(repo: Path, name: str, revision: str, *, old: str | None = None) -> None:
    args = ["update-ref", name, revision]
    if old is not None:
        args.append(old)
    _git(repo, *args)


def _resolve_commit(repo: Path, ref: str) -> str:
    resolved = _git(repo, "rev-parse", "--verify", f"{ref}^{{commit}}", check=False)
    if not resolved:
        raise HistoryGuardError(f"無法解析 commit：{ref}")
    return resolved


def _tree_index(repo: Path, commit: str, *, allow_missing: bool) -> dict:
    # 先確認 commit/tree 可讀；只有確認 tree 完整且路徑不存在時才視為首次新增。
    result = _git_result(repo, "ls-tree", "-r", "--name-only", commit)
    if result.returncode:
        raise HistoryGuardError(f"commit 或 tree 物件無法讀取：{commit}")
    listing = result.stdout.decode("utf-8", errors="replace").strip()
    if INDEX_PATH not in listing.splitlines():
        if allow_missing:
            return {"entries": []}
        raise HistoryGuardError(f"head 缺少證據索引：{commit}")
    raw = _git(repo, "cat-file", "blob", f"{commit}:{INDEX_PATH}", check=False)
    if not raw and _git(repo, "cat-file", "-e", f"{commit}:{INDEX_PATH}", check=False):
        raise HistoryGuardError(f"索引 blob 無法讀取：{commit}")
    try:
        value = json.loads(raw)
    except (json.JSONDecodeError, TypeError) as exc:
        raise HistoryGuardError(f"索引 JSON 無效：{commit}") from exc
    if not isinstance(value, dict) or not isinstance(value.get("entries"), list):
        raise HistoryGuardError(f"索引結構無效：{commit}")
    return value


def _index_blob(repo: Path, commit: str) -> bytes | None:
    listing_result = _git_result(repo, "ls-tree", "-r", "--name-only", commit)
    if listing_result.returncode:
        raise HistoryGuardError(f"commit 或 tree 物件無法讀取：{commit}")
    listing = listing_result.stdout.decode("utf-8", errors="replace").strip()
    if INDEX_PATH not in listing.splitlines():
        return None
    result = subprocess.run(
        ["git", "cat-file", "blob", f"{commit}:{INDEX_PATH}"],
        cwd=repo,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        check=False,
    )
    if result.returncode:
        raise HistoryGuardError(f"索引 blob 無法讀取：{commit}")
    return result.stdout


def _compare(repo: Path, base: str, head: str, *, allow_missing_base: bool) -> None:
    base_index = _tree_index(repo, base, allow_missing=allow_missing_base)
    head_index = _tree_index(repo, head, allow_missing=False)
    violations = evidence_index.check_index_append_only(base_index, head_index)
    if violations:
        raise HistoryGuardError("索引只增檢查失敗：" + "; ".join(violations))


def _check_pr_range(repo: Path, base_ref: str, head_ref: str) -> None:
    """依 plan 逐提交、逐父節點及最終 merge-base/head 比對純函式。"""
    if _git(repo, "rev-parse", "--is-shallow-repository") == "true":
        raise HistoryGuardError("shallow repository 不可驗證")
    base = _resolve_commit(repo, base_ref)
    head = _resolve_commit(repo, head_ref)
    merge_base = _git(repo, "merge-base", base, head, check=False)
    if not merge_base:
        raise HistoryGuardError("base 與 head 沒有可解析的 merge-base")

    commits = _git(repo, "rev-list", "--reverse", "--topo-order", f"{merge_base}..{head}")
    for commit in commits.splitlines():
        parents_line = _git(repo, "rev-list", "--parents", "-n", "1", commit)
        parts = parents_line.split()
        parents = parts[1:]
        for parent in parents:
            if _index_blob(repo, parent) != _index_blob(repo, commit):
                _compare(repo, parent, commit, allow_missing_base=True)

    # 不以最終 diff 代替逐提交檢查；額外保留整體 merge-base 對 head 的比對。
    _compare(repo, merge_base, head, allow_missing_base=True)


def _commit(repo: Path, parent: str, entries: list[dict], message: str) -> str:
    commit = _commit_tree(
        repo,
        {INDEX_PATH: _index_bytes(entries), "README.md": b"fixture\n"},
        parents=(parent,),
        message=message,
    )
    _set_ref(repo, "refs/heads/main", commit, old=parent)
    return commit


def test_pr_ci_rejects_index_history_rewrite(tmp_path):
    """刪除或重寫 merge-base 已存在的正式 FAIL 必須被拒。"""
    for mode in ("delete", "rewrite"):
        repo = _init_repo(tmp_path / mode)
        fail = _row("1" * 20)
        passed = _row("2" * 20, summary="保留列")
        base = _commit_tree(
            repo,
            {INDEX_PATH: _index_bytes([fail, passed]), "README.md": b"base\n"},
            message="base with fail",
        )
        _set_ref(repo, "refs/heads/main", base)
        if mode == "delete":
            entries = [passed]
        else:
            changed = {**fail, "entry_id": "3" * 20, "failure_summary": "改寫摘要"}
            entries = [changed, passed]
        head = _commit(repo, base, entries, f"{mode} fail row")
        _set_ref(repo, "refs/heads/pr", head)
        with pytest.raises(HistoryGuardError, match="只增檢查"):
            _check_pr_range(repo, base, "refs/heads/pr")

    # 合法分支保留全部 base entries 並追加新列仍通過。
    repo = _init_repo(tmp_path / "append-only")
    fail = _row("4" * 20)
    base = _commit_tree(repo, {INDEX_PATH: _index_bytes([fail])}, message="base")
    _set_ref(repo, "refs/heads/main", base)
    head = _commit(repo, base, [fail, _row("5" * 20)], "append")
    _set_ref(repo, "refs/heads/pr", head)
    _check_pr_range(repo, base, "refs/heads/pr")


@pytest.mark.parametrize("removed_kind", ["ordinary", "restricted"])
def test_pr_ci_rejects_fail_added_then_removed(tmp_path, removed_kind):
    """中途加入再刪除 FAIL，即使最終索引與 base 相同也拒絕。"""
    repo = _init_repo(tmp_path / removed_kind)
    base_index = _index_bytes([])
    base = _commit_tree(repo, {INDEX_PATH: base_index}, message="base without fail")
    _set_ref(repo, "refs/heads/main", base)
    fail = (
        _ordinary_row("6" * 20)
        if removed_kind == "ordinary"
        else _row("6" * 20, summary="restricted failure")
    )
    middle = _commit(repo, base, [fail], "record fail")
    head = _commit(repo, middle, [], "remove fail")
    _set_ref(repo, "refs/heads/pr", head)
    assert _index_blob(repo, base) == _index_blob(repo, head)
    with pytest.raises(HistoryGuardError, match="只增檢查"):
        _check_pr_range(repo, base, "refs/heads/pr")

    # 若保留中途 FAIL，對照分支可只增通過。
    _check_pr_range(repo, base, middle)


def test_pr_ci_checks_merge_parents_and_force_push(tmp_path):
    """merge 要比較每個父節點；force-push 換 head 後需重新驗新範圍。"""
    repo = _init_repo(tmp_path / "merge")
    base = _commit_tree(repo, {INDEX_PATH: _index_bytes([])}, message="base")
    _set_ref(repo, "refs/heads/main", base)
    first_row = _ordinary_row("7" * 20, result="PASS")
    second_row = _row("8" * 20, summary="第二父節點新增")
    first = _commit_tree(
        repo,
        {INDEX_PATH: _index_bytes([first_row])},
        parents=(base,),
        message="first parent append",
    )
    second = _commit_tree(
        repo,
        {INDEX_PATH: _index_bytes([second_row])},
        parents=(base,),
        message="second parent fail",
    )
    good_merge = _commit_tree(
        repo,
        {INDEX_PATH: _index_bytes([first_row, second_row])},
        parents=(first, second),
        message="merge both histories",
    )
    _set_ref(repo, "refs/heads/good-merge", good_merge)
    _check_pr_range(repo, "refs/heads/main", "refs/heads/good-merge")

    bad_merge = _commit_tree(
        repo,
        {INDEX_PATH: _index_bytes([first_row])},
        parents=(first, second),
        message="drop second parent fail",
    )
    _set_ref(repo, "refs/heads/bad-merge", bad_merge)
    with pytest.raises(HistoryGuardError, match="只增檢查"):
        _check_pr_range(repo, "refs/heads/main", "refs/heads/bad-merge")

    old_head = _commit_tree(
        repo,
        {INDEX_PATH: _index_bytes([_ordinary_row("9" * 20, result="PASS")])},
        parents=(base,),
        message="old force-push range",
    )
    _set_ref(repo, "refs/heads/pr", old_head)
    _check_pr_range(repo, "refs/heads/main", "refs/heads/pr")

    fail = _row("a" * 20, summary="新範圍中途 FAIL")
    added = _commit_tree(
        repo,
        {INDEX_PATH: _index_bytes([fail])},
        parents=(base,),
        message="new range records fail",
    )
    new_head = _commit_tree(
        repo,
        {INDEX_PATH: _index_bytes([])},
        parents=(added,),
        message="new range removes fail",
    )
    _set_ref(repo, "refs/heads/pr", new_head, old=old_head)
    assert _index_blob(repo, base) == _index_blob(repo, new_head)
    with pytest.raises(HistoryGuardError, match="只增檢查"):
        _check_pr_range(repo, "refs/heads/main", "refs/heads/pr")


def test_pr_ci_index_base_fail_closed(tmp_path):
    """缺 ref、shallow、缺物件、壞 JSON 及 head 刪檔均 fail-closed。"""
    first_add = _init_repo(tmp_path / "first-add")
    base = _commit_tree(first_add, {"README.md": b"no index\n"}, message="no index yet")
    _set_ref(first_add, "refs/heads/main", base)
    head = _commit(
        first_add,
        base,
        [_row("b" * 20)],
        "create first index",
    )
    _check_pr_range(first_add, "refs/heads/main", head)
    with pytest.raises(HistoryGuardError, match="無法解析 commit"):
        _check_pr_range(first_add, "refs/heads/missing", head)

    bad_base_repo = _init_repo(tmp_path / "bad-base")
    bad_base = _commit_tree(
        bad_base_repo,
        {INDEX_PATH: b"{broken\n"},
        message="bad base JSON",
    )
    _set_ref(bad_base_repo, "refs/heads/main", bad_base)
    good_head = _commit_tree(
        bad_base_repo,
        {INDEX_PATH: _index_bytes([])},
        parents=(bad_base,),
        message="repair JSON",
    )
    _set_ref(bad_base_repo, "refs/heads/pr", good_head)
    with pytest.raises(HistoryGuardError, match="JSON 無效"):
        _check_pr_range(bad_base_repo, "refs/heads/main", "refs/heads/pr")

    deleted_repo = _init_repo(tmp_path / "deleted-head")
    base = _commit_tree(deleted_repo, {INDEX_PATH: _index_bytes([])}, message="valid base")
    _set_ref(deleted_repo, "refs/heads/main", base)
    deleted = _commit_tree(
        deleted_repo,
        {"README.md": b"deleted index\n"},
        parents=(base,),
        message="delete index",
    )
    _set_ref(deleted_repo, "refs/heads/pr", deleted)
    with pytest.raises(HistoryGuardError, match="head 缺少"):
        _check_pr_range(deleted_repo, "refs/heads/main", "refs/heads/pr")

    shallow_repo = _init_repo(tmp_path / "shallow")
    base = _commit_tree(shallow_repo, {INDEX_PATH: _index_bytes([])}, message="shallow base")
    _set_ref(shallow_repo, "refs/heads/main", base)
    head = _commit(shallow_repo, base, [_row("c" * 20)], "shallow head")
    shallow_path = Path(_git(shallow_repo, "rev-parse", "--git-path", "shallow"))
    if not shallow_path.is_absolute():
        shallow_path = shallow_repo / shallow_path
    shallow_path.write_bytes((base + "\n").encode("ascii"))
    with pytest.raises(HistoryGuardError, match="shallow"):
        _check_pr_range(shallow_repo, base, head)

    missing_repo = _init_repo(tmp_path / "missing-object")
    base = _commit_tree(missing_repo, {INDEX_PATH: _index_bytes([])}, message="object base")
    _set_ref(missing_repo, "refs/heads/main", base)
    head = _commit(missing_repo, base, [_row("d" * 20)], "object head")
    object_dir = Path(_git(missing_repo, "rev-parse", "--git-path", "objects"))
    if not object_dir.is_absolute():
        object_dir = missing_repo / object_dir
    commit_object = object_dir / base[:2] / base[2:]
    assert commit_object.is_file()
    commit_object.unlink()
    with pytest.raises(HistoryGuardError):
        _check_pr_range(missing_repo, base, head)

    unrelated_repo = _init_repo(tmp_path / "unrelated")
    unrelated_base = _commit_tree(
        unrelated_repo,
        {INDEX_PATH: _index_bytes([])},
        message="unrelated base",
    )
    other_root = _commit_tree(
        unrelated_repo,
        {INDEX_PATH: _index_bytes([])},
        message="unrelated head",
    )
    _set_ref(unrelated_repo, "refs/heads/main", unrelated_base)
    _set_ref(unrelated_repo, "refs/heads/pr", other_root)
    with pytest.raises(HistoryGuardError, match="merge-base"):
        _check_pr_range(unrelated_repo, "refs/heads/main", "refs/heads/pr")
