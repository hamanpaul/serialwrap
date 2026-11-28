"""以暫存 Git 歷史驗證出口索引的逐提交只增規則。"""

from __future__ import annotations

import json
import inspect
import os
import subprocess
import sys
from pathlib import Path

import pytest

import fakes_evidence
import stage3_fakes as fakes
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


def _check_pr_range(repo: Path, base_ref: str, head_ref: str) -> None:
    """以 production guard 與 workflow 使用的正式 CLI 驗證真實 DAG。"""
    try:
        evidence_index.check_pr_history_append_only(repo, base_ref, head_ref)
    except (OSError, ValueError, subprocess.SubprocessError) as exc:
        raise HistoryGuardError(str(exc)) from exc
    checkout = Path(__file__).resolve().parents[1]
    environment = dict(os.environ)
    current_pythonpath = environment.get("PYTHONPATH")
    environment["PYTHONPATH"] = (
        str(checkout) if not current_pythonpath else str(checkout) + os.pathsep + current_pythonpath
    )
    result = subprocess.run(
        [
            sys.executable,
            "-m",
            "tools.stage3.evidence_index",
            "--repo",
            str(repo),
            "--check-pr-history",
            base_ref,
            head_ref,
        ],
        cwd=checkout,
        env=environment,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        check=False,
    )
    if result.returncode:
        detail = (result.stderr + result.stdout).decode("utf-8", errors="replace").strip()
        raise HistoryGuardError(f"production CLI rejected PR history ({result.returncode}): {detail}")


def test_pr_history_helper_uses_production_guard_and_cli(tmp_path, monkeypatch):
    """DAG 驗收必須經 production guard 與正式 CLI。"""
    repo = _init_repo(tmp_path / "production-guard")
    base = _commit_tree(repo, {INDEX_PATH: _index_bytes([])}, message="base")
    _set_ref(repo, "refs/heads/main", base)
    head = _commit(repo, base, [_row("f" * 20)], "append")
    calls = []
    original = evidence_index.check_pr_history_append_only

    def observed(repo_arg, base_arg, head_arg):
        calls.append((Path(repo_arg), base_arg, head_arg))
        return original(repo_arg, base_arg, head_arg)

    monkeypatch.setattr(evidence_index, "check_pr_history_append_only", observed)
    cli_calls = []
    original_run = subprocess.run

    def observed_run(argv, *args, **kwargs):
        if isinstance(argv, (list, tuple)) and "tools.stage3.evidence_index" in argv and "--check-pr-history" in argv:
            cli_calls.append(list(argv))
        return original_run(argv, *args, **kwargs)

    monkeypatch.setattr(subprocess, "run", observed_run)
    _check_pr_range(repo, "refs/heads/main", head)
    assert calls == [(repo, "refs/heads/main", head)]
    assert len(cli_calls) == 1
    assert cli_calls[0][cli_calls[0].index("--repo") + 1] == str(repo)
    assert cli_calls[0][-3:] == ["--check-pr-history", "refs/heads/main", head]


def test_pr_workflow_always_checks_history_with_full_fetch(tmp_path):
    """workflow 對每個 PR 執行正確 base/head 的歷史檢查。"""
    import yaml

    root = Path(__file__).resolve().parents[1]
    workflow = yaml.safe_load((root / ".github/workflows/tests.yml").read_bytes())
    trigger = workflow.get("on", workflow.get(True))
    assert isinstance(trigger, dict) and "pull_request" in trigger
    steps = workflow["jobs"]["pytest"]["steps"]
    checkout = next(step for step in steps if step.get("uses", "").startswith("actions/checkout@"))
    assert checkout.get("with", {}).get("fetch-depth") == 0
    guard = next(step for step in steps if "Stage 3 證據索引逐提交只增檢查" in step.get("name", ""))
    assert guard.get("if") == "${{ github.event_name == 'pull_request' }}"
    env = guard.get("env", {})
    assert env.get("BASE_REF") == "origin/${{ github.base_ref }}"
    assert env.get("HEAD_REF") == "${{ github.event.pull_request.head.sha }}"
    run = guard.get("run", "")
    assert "--repo ." in run and "--check-pr-history" in run
    assert '"$BASE_REF"' in run and '"$HEAD_REF"' in run
    assert "paths:" not in (workflow.get("on", workflow.get(True)).get("pull_request", {}) or {})


def test_required_seed_is_bound_to_designated_source(tmp_path):
    """透過真實 Git 讀取指定 revision 的 seed，並拒絕改寫來源 revision。"""
    from tools.stage3 import required_cases

    idx = fakes_evidence.evidence_index(
        tmp_path,
        stage="3",
        contract="c" * 40,
        source_reader=evidence_index._git_seed_reader,
    )
    evidence_root = tmp_path / "docs" / "rust-refactor" / "evidence"
    source_reader = evidence_index._git_seed_reader
    manifest = json.loads((evidence_root / "denominators" / "source-manifest.json").read_bytes())
    assert required_cases.verify_seed_sources(
        manifest, evidence_root=evidence_root, source_reader=source_reader
    ) == []
    target = evidence_index.load_index(idx)["stage_targets"]["3"]["linux"]
    assert target["required_cases_lock"]["sha256"]
    assert target["required_cases_sha256"]

    undisclosed = json.loads(json.dumps(manifest))
    undisclosed["sources"][0]["revision"] = "0" * 40
    assert required_cases.verify_seed_sources(
        undisclosed, evidence_root=evidence_root, source_reader=source_reader
    )


def test_set_target_stage3_records_bundle_tree(tmp_path):
    """Stage 3 target 整合以指定 Git seed source reader 凍結 bundle 身分。"""
    api = evidence_index
    from tools.stage3 import ab_manifest

    idx = fakes_evidence.evidence_index(
        tmp_path,
        stage="3",
        contract="c" * 40,
        source_reader=api._git_seed_reader,
    )
    evidence_root = tmp_path / "docs" / "rust-refactor" / "evidence"
    catalog = api.load_exit_catalog()
    bundle = tmp_path / "bundles" / "linux"
    target = api.load_index(idx)["stage_targets"]["3"]["linux"]
    assert target["bundle_tree_sha256"] == ab_manifest.bundle_tree_sha256(bundle)
    assert target["runtime_tree_sha256"] == ab_manifest.runtime_tree_sha256(bundle)
    old_pass = fakes_evidence.entry(tmp_path, "S3-ROLLBACK-LINUX", "PASS", platform="linux")
    api.add_entry(idx, old_pass, catalog=catalog)
    current_round = api.begin_candidate_round(
        idx, rc_tag="v1.0.0-rc.2", set_by="test", now=fakes.ts(30, 0)
    )
    for platform in ("linux", "windows"):
        bundle = tmp_path / "bundles" / platform
        target_args = {
            "stage": "3",
            "platform": platform,
            "release_manifest": bundle / "release-manifest.json",
            "contract_revision": "c" * 40,
            "tool_revision": "t" * 40,
            "case_list_sha256": "0" * 64,
            "set_by": "test",
            "now": fakes.ts(30, 1),
            "bundle_dir": bundle,
            "archive": fakes.rc_archive(bundle),
            "candidate_round_id": current_round,
        }
        if "source_reader" in inspect.signature(api.set_target).parameters:
            target_args["source_reader"] = api._git_seed_reader
        api.set_target(idx, **target_args)
    after = api.load_index(idx)
    assert all(row.get("candidate_round_id") == current_round for row in after["target_history"][-2:])
    status_args = {"evidence_root": evidence_root, "cases": {}}
    if "source_reader" in inspect.signature(api.item_status).parameters:
        status_args["source_reader"] = api._git_seed_reader
    status, _ = api.item_status(after, catalog["S3-ROLLBACK-LINUX"], **status_args)
    assert status == "STALE"


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
