"""Python daemon 退役切換（P9-T16）：退役守門、發佈邊界位元組核對與 release workflow 結構（紅燈）。

標記說明：本檔混合純記憶體測試與呼叫 CLI 子程序的測試，依 P0 規則「同檔混合時資源標記放函式層」，
模組層只放平台標記 `core_portable`；每個測試函式各自帶 `memory_only` 或 `process_rpc`
（`test_resource_markers.py` 的 `test_module_resource_marker_not_mixed` 不允許模組層與函式層各放資源標記）。

前置模組（`tools/stage3/*`、`tests/stage3_fakes.py`、`tests/fakes_evidence.py`）以 `_Lazy` 代理延遲匯入，
使本檔在前置 Task 尚未合入時仍可收集；存取時才匯入，缺件即以 ModuleNotFoundError 失敗。
"""

from __future__ import annotations

import ast
import importlib
import json
import re
import shutil
import subprocess
import sys
import tarfile
import zipfile
from pathlib import Path

import pytest

import fakes_retirement

pytestmark = [pytest.mark.core_portable]

ROOT = Path(__file__).resolve().parents[1]
PLATFORMS = ("linux", "windows")


class _Lazy:
    """延遲匯入代理：第一次存取屬性時才 import 目標模組（前置 Task 未合入時收集不受影響）。"""

    def __init__(self, module: str) -> None:
        self._module = module

    def __getattr__(self, name: str):
        return getattr(importlib.import_module(self._module), name)


retirement = _Lazy("tools.stage3.retirement")
evidence_index = _Lazy("tools.stage3.evidence_index")
ab_manifest = _Lazy("tools.stage3.ab_manifest")
jsonio = _Lazy("tools.stage3.jsonio")
fakes = _Lazy("stage3_fakes")

OLD_STAGES = ("1A", "1B", "2.1", "2.2", "2.3", "2.4")
STAGE3_CANDIDATE = {
    "stage": "3", "retirement_applies": True, "publishable": False, "candidate": True,
    "include_python_runtime": False, "include_python_cli_delegate": False, "daemon_implementation": "rust",
    "manifest_retirement": {"python_daemon": "removed"},
}
STAGE3_RETIRED_PUBLISHABLE = dict(STAGE3_CANDIDATE, publishable=True, candidate=False)
STAGE3_RETIRED_DEV = dict(STAGE3_CANDIDATE, publishable=False, candidate=False)


def _old_plan(stage: str, *, publishable: bool, candidate: bool = False) -> dict:
    return {"stage": stage, "retirement_applies": False, "publishable": publishable,
            "candidate": candidate, "manifest_retirement": None}


BUNDLE_PLAN_ROWS = (
    [(s, False, False, _old_plan(s, publishable=True)) for s in OLD_STAGES]
    + [(s, False, True, _old_plan(s, publishable=False)) for s in OLD_STAGES]
    + [("3", False, False, STAGE3_CANDIDATE), ("3", False, True, STAGE3_CANDIDATE)]
    + [("3", True, False, STAGE3_RETIRED_PUBLISHABLE), ("3", True, True, STAGE3_RETIRED_DEV)]
    + [(s, True, False, "HARNESS_RETIREMENT_INCONSISTENT") for s in OLD_STAGES]
    + [(s, True, True, _old_plan(s, publishable=False)) for s in OLD_STAGES]
)


def _release(repo: Path, r: dict) -> list[str]:
    return retirement.verify(r, repo_root=repo, mode="release", evidence_root=repo / "evidence",
                             bundle_manifests=fakes_retirement.bundle_args(repo))


def _cli(*args: str) -> subprocess.CompletedProcess:
    return subprocess.run([sys.executable, "-m", "tools.stage3", "retirement", *args],
                          cwd=ROOT, capture_output=True, text=True, timeout=300)


def _dotted(node: ast.AST) -> str:
    if isinstance(node, ast.Name):
        return node.id
    if isinstance(node, ast.Attribute):
        base = _dotted(node.value)
        return f"{base}.{node.attr}" if base else node.attr
    return ""


@pytest.mark.memory_only
def test_stage3_bundle_plan_marks_candidate_before_approval():
    plan = retirement.bundle_plan("3", {"retired": False}, dev=False)
    assert plan["candidate"] is True and plan["publishable"] is False
    assert plan["manifest_retirement"] == {"python_daemon": "removed"}
    assert plan["include_python_runtime"] is False and plan["daemon_implementation"] == "rust"


@pytest.mark.memory_only
def test_stage3_default_retirement_file_is_candidate_before_approval():
    plan = retirement.bundle_plan("3", retirement.load_retirement(), dev=False)
    assert plan["candidate"] is True and plan["publishable"] is False


@pytest.mark.memory_only
@pytest.mark.parametrize("value", [1, "true", None])
def test_bundle_plan_rejects_non_boolean_retirement(value):
    with pytest.raises(retirement.HarnessError) as exc:
        retirement.bundle_plan("2.4", {"retired": value}, dev=False)
    assert exc.value.code == "HARNESS_RETIREMENT_INCONSISTENT"


@pytest.mark.memory_only
def test_retired_true_requires_stage3_evidence(tmp_path):
    repo = fakes_retirement.retirement_repo(tmp_path, stage3_result="BLOCKED")
    idx = repo / "docs/rust-refactor/evidence/index.json"
    targets = json.loads(idx.read_text(encoding="utf-8"))["stage_targets"]["3"]
    effective_version = json.loads(
        (repo / "bundles" / "linux" / "release-manifest.json").read_text(encoding="utf-8")
    )["bundle_version"]
    r = dict(retirement.load_retirement(repo / "packaging" / "python_daemon_retirement.json"), retired=True,
             approved_by="owner", approved_at=fakes.ts(30, 1), decision_ref="OD-14", effective_bundle_version=effective_version,
             stage3_target_artifact_ids={p: targets[p]["artifact_id"] for p in ("linux", "windows")},
             evidence_index_sha256=jsonio.sha256_file(idx))
    v = retirement.verify(r, repo_root=repo, mode="release", evidence_root=repo / "evidence",
                          bundle_manifests={p: repo / "bundles" / p / "release-manifest.json" for p in ("linux", "windows")})
    assert any(x.startswith("retirement: stage 3 not PASS") for x in v)


@pytest.mark.memory_only
def test_release_requires_tested_bundle_bytes(tmp_path):
    repo = fakes_retirement.retirement_repo(tmp_path, stage3_result="PASS")
    r = fakes_retirement.approved(repo)
    rebuilt = repo / "bundles" / "linux" / "release-manifest.json"
    doc = json.loads(rebuilt.read_text(encoding="utf-8"))
    doc["created_at"] = "2027-01-01T00:00:00+00:00"            # 模擬「核准後重建」
    rebuilt.write_text(json.dumps(doc), encoding="utf-8")
    v = retirement.verify(r, repo_root=repo, mode="release", evidence_root=repo / "evidence",
                          bundle_manifests={p: repo / "bundles" / p / "release-manifest.json" for p in ("linux", "windows")})
    assert "retirement: bundle manifest mismatch: linux" in v


@pytest.mark.memory_only
def test_release_clean_tested_bundle_passes(tmp_path):                        # 正向對照：未動的受測 bundle 必須通過
    repo = fakes_retirement.retirement_repo(tmp_path, stage3_result="PASS")
    assert _release(repo, fakes_retirement.approved(repo)) == []


@pytest.mark.memory_only
@pytest.mark.parametrize("platform", ["linux", "windows"])
@pytest.mark.parametrize("component", ["daemon.launcher", "cli"])
def test_release_refuses_swapped_binary_with_approved_manifest(tmp_path, platform, component):   # R2-F7
    repo = fakes_retirement.retirement_repo(tmp_path, stage3_result="PASS")
    r = fakes_retirement.approved(repo)
    bundle = repo / "bundles" / platform
    manifest_path = bundle / "release-manifest.json"
    manifest_before = manifest_path.read_bytes()
    doc = json.loads(manifest_before)
    rel = (doc["components"]["daemon"]["launcher"]["path"]
           if component == "daemon.launcher" else doc["components"]["cli"]["path"])
    (bundle / rel).write_bytes(b"#!/bin/sh\necho swapped\n")                  # 受測後替換，manifest 原封不動
    assert manifest_path.read_bytes() == manifest_before
    assert jsonio.sha256_file(manifest_path) == r["stage3_target_artifact_ids"][platform]
    v = _release(repo, r)
    assert f"retirement: bundle manifest mismatch: {platform}" not in v          # manifest 檢查本身仍會通過……
    assert f"retirement: bundle content mismatch: {platform}: release component sha256 mismatch: {platform} {component}" in v
    assert f"retirement: bundle tree mismatch: {platform}" in v                 # ……但內容與檔案樹都必須拒絕


@pytest.mark.memory_only
@pytest.mark.parametrize("rel", ["bin/serialwrapd", "bin/serialwrap"])
def test_release_refuses_deleted_binary_with_approved_manifest(tmp_path, rel):  # R2-F7
    repo = fakes_retirement.retirement_repo(tmp_path, stage3_result="PASS")
    r = fakes_retirement.approved(repo)
    bundle = repo / "bundles" / "linux"
    (bundle / rel).unlink()
    v = _release(repo, r)
    assert f"retirement: bundle content mismatch: linux: file missing: {bundle / rel}" in v
    assert "retirement: bundle tree mismatch: linux" in v


@pytest.mark.memory_only
@pytest.mark.parametrize("op", ["modify", "delete"])
def test_release_refuses_tampered_asset_with_approved_manifest(tmp_path, op):  # R2-F7：assets 沒有 path，只有檔案樹守得住
    repo = fakes_retirement.retirement_repo(tmp_path, stage3_result="PASS")
    r = fakes_retirement.approved(repo)
    asset = repo / "bundles" / "windows" / "assets" / "skill" / "SKILL.md"
    if op == "modify":
        asset.write_text("tampered\n", encoding="utf-8")
    else:
        asset.unlink()
    v = _release(repo, r)
    assert "retirement: bundle tree mismatch: windows" in v
    assert not any(x.startswith("retirement: bundle manifest mismatch") for x in v)


@pytest.mark.memory_only
def test_release_refuses_added_sideload_file(tmp_path):                       # R2-F7：manifest 未宣告的新增檔
    repo = fakes_retirement.retirement_repo(tmp_path, stage3_result="PASS")
    r = fakes_retirement.approved(repo)
    (repo / "bundles" / "windows" / "bin" / "version.dll").write_bytes(b"MZ")
    assert "retirement: bundle tree mismatch: windows" in _release(repo, r)


@pytest.mark.memory_only
def test_release_refuses_sideload_after_retarget(tmp_path):                   # R3-F2：加旁載檔後重設 target，舊 PASS 不得帶著新 tree 發佈
    from tools.stage3.reason_codes import HarnessError

    repo = fakes_retirement.retirement_repo(tmp_path, stage3_result="PASS")
    idx = repo / "docs/rust-refactor/evidence/index.json"
    bundle = repo / "bundles" / "windows"
    (bundle / "bin" / "version.dll").write_bytes(b"MZ")
    before = idx.read_bytes()
    with pytest.raises(HarnessError) as ei:                                     # 正規路徑：同 manifest 換 tree → set_target 拒絕
        tgt = evidence_index.load_index(idx)["stage_targets"]["3"]["windows"]
        evidence_index.set_target(idx, stage="3", platform="windows", release_manifest=bundle / "release-manifest.json",
                                  contract_revision=tgt["contract_revision"], tool_revision=tgt["tool_revision"],
                                  case_list_sha256=tgt["case_list_sha256"], set_by="t", now=fakes.ts(30, 0), bundle_dir=bundle)
    assert ei.value.code == "HARNESS_ARTIFACT_MISMATCH"
    assert idx.read_bytes() == before
    doc = json.loads(idx.read_text(encoding="utf-8"))                            # 繞過：直接改寫索引把 target tree 換成新值
    doc["stage_targets"]["3"]["windows"]["bundle_tree_sha256"] = ab_manifest.bundle_tree_sha256(bundle)
    doc["stage_targets"]["3"]["windows"]["runtime_tree_sha256"] = ab_manifest.runtime_tree_sha256(bundle)
    idx.write_text(json.dumps(doc), encoding="utf-8")
    v = _release(repo, fakes_retirement.approved(repo))                         # 依修改後索引重算 evidence_index_sha256
    assert "retirement: bundle tree mismatch: windows" not in v                 # tree 比對本身會被新凍結值騙過……
    assert "retirement: stage 3 not PASS: BLOCKED" in v                         # ……但所有 windows 舊 PASS 已因內容指紋過期


@pytest.mark.memory_only
def test_release_refuses_target_without_bundle_tree(tmp_path):
    repo = fakes_retirement.retirement_repo(tmp_path, stage3_result="PASS")
    idx = repo / "docs/rust-refactor/evidence/index.json"
    doc = json.loads(idx.read_text(encoding="utf-8"))
    del doc["stage_targets"]["3"]["linux"]["bundle_tree_sha256"]
    idx.write_text(json.dumps(doc), encoding="utf-8")
    r = fakes_retirement.approved(repo)                                         # 依修改後索引重算 evidence_index_sha256
    assert "retirement: target missing bundle tree: linux" in _release(repo, r)


@pytest.mark.memory_only
def test_release_refused_after_new_canary_incident(tmp_path):                 # R3-F1：已登錄的 Canary PASS 被新事故報告撤銷
    repo = fakes_retirement.retirement_repo(tmp_path, stage3_result="PASS")
    assert _release(repo, fakes_retirement.approved(repo)) == []             # 對照：撤銷前可退役
    idx = repo / "docs/rust-refactor/evidence/index.json"
    cat = evidence_index.load_exit_catalog()
    for item in ("S3-CANARY-WINDOWS", "S3-INCIDENTS-CLOSED"):                # 新報告：open incident → INSUFFICIENT，帳本版本較新
        evidence_index.add_entry(idx, fakes_retirement.entry(repo, item, "INSUFFICIENT", platform="windows", n=7), catalog=cat)
    r = fakes_retirement.approved(repo)                                         # 依修改後索引重算 evidence_index_sha256
    assert "retirement: stage 3 not PASS: BLOCKED" in _release(repo, r)


@pytest.mark.memory_only
def test_verify_archive_accepts_packed_tested_bundle(tmp_path):
    repo = fakes_retirement.retirement_repo(tmp_path, stage3_result="PASS")
    r = fakes_retirement.approved(repo)
    out = tmp_path / "out" / "serialwrap-linux.tar.gz"
    out.parent.mkdir()
    retirement.pack_bundle(repo / "bundles" / "linux", out)
    v, digest = retirement.verify_archive(r, repo_root=repo, platform="linux", archive=out)
    assert v == [] and digest == jsonio.sha256_file(out)


@pytest.mark.memory_only
@pytest.mark.parametrize("op", ["swap_daemon", "delete_cli", "modify_asset"])
def test_verify_archive_refuses_archive_built_from_tampered_copy(tmp_path, op):   # R2-F7：最終 archive 與受測物不一致
    repo = fakes_retirement.retirement_repo(tmp_path, stage3_result="PASS")
    r = fakes_retirement.approved(repo)
    copy = tmp_path / "copy"
    shutil.copytree(repo / "bundles" / "linux", copy)                            # release-manifest.json 位元組相同
    if op == "swap_daemon":
        (copy / "bin" / "serialwrapd").write_bytes(b"swapped")
    elif op == "delete_cli":
        (copy / "bin" / "serialwrap").unlink()
    else:
        (copy / "assets" / "skill" / "SKILL.md").write_text("x", encoding="utf-8")
    out = tmp_path / "serialwrap-linux.tar.gz"
    retirement.pack_bundle(copy, out)
    v, _ = retirement.verify_archive(r, repo_root=repo, platform="linux", archive=out)
    assert "retirement: archive manifest mismatch: linux" not in v
    assert "retirement: archive tree mismatch: linux" in v
    if op == "swap_daemon":
        assert "retirement: archive content mismatch: linux: release component sha256 mismatch: linux daemon.launcher" in v
    if op == "delete_cli":
        assert any(x.startswith("retirement: archive content mismatch: linux: file missing:") and x.endswith("serialwrap")
                   for x in v)


@pytest.mark.memory_only
def test_verify_archive_refuses_unsafe_member(tmp_path):
    repo = fakes_retirement.retirement_repo(tmp_path, stage3_result="PASS")
    r = fakes_retirement.approved(repo)
    out = tmp_path / "evil.tar.gz"
    with tarfile.open(out, "w:gz") as tf:
        tf.add(repo / "bundles" / "linux" / "release-manifest.json", arcname="release-manifest.json")
        tf.add(repo / "bundles" / "linux" / "bin" / "serialwrapd", arcname="../serialwrapd")
    v, _ = retirement.verify_archive(r, repo_root=repo, platform="linux", archive=out)
    assert "retirement: archive unsafe member: ../serialwrapd" in v
    assert not (tmp_path / "serialwrapd").exists()                             # 不得解到暫存目錄之外


@pytest.mark.process_rpc
def test_cli_verify_archive_writes_sha256sums_only_when_clean(tmp_path):
    repo = fakes_retirement.retirement_repo(tmp_path, stage3_result="PASS")
    fakes_retirement.write_approved(repo)                                       # 把 approved(repo) 寫回 packaging/python_daemon_retirement.json
    good, bad = tmp_path / "good.tar.gz", tmp_path / "bad.tar.gz"
    retirement.pack_bundle(repo / "bundles" / "linux", good)
    (repo / "bundles" / "linux" / "bin" / "serialwrapd").write_bytes(b"swapped")
    retirement.pack_bundle(repo / "bundles" / "linux", bad)

    def cli(archive, sums):
        return _cli("verify-archive", "--repo-root", str(repo), "--platform", "linux",
                    "--archive", str(archive), "--sha256sums", str(sums))

    ok = cli(good, tmp_path / "SUMS-good")
    assert ok.returncode == 0
    assert (tmp_path / "SUMS-good").read_text(encoding="utf-8") == f"{jsonio.sha256_file(good)}  good.tar.gz\n"
    ng = cli(bad, tmp_path / "SUMS-bad")
    assert ng.returncode == 1 and "retirement: archive tree mismatch: linux" in ng.stdout + ng.stderr
    assert not (tmp_path / "SUMS-bad").exists()


@pytest.mark.memory_only
@pytest.mark.parametrize(
    "stage, retired, dev, expected",
    BUNDLE_PLAN_ROWS,
)
def test_bundle_plan_table(stage, retired, dev, expected):
    from tools.stage3.reason_codes import HarnessError

    ret = {"retired": retired}                                                  # bundle_plan 只讀退役旗標
    if isinstance(expected, str):
        with pytest.raises(HarnessError) as ei:
            retirement.bundle_plan(stage, ret, dev=dev)
        assert ei.value.code == expected
    else:
        assert retirement.bundle_plan(stage, ret, dev=dev) == expected


@pytest.mark.memory_only
def test_repo_default_verify_clean():
    assert retirement.verify(retirement.load_retirement(), repo_root=ROOT, mode="repo") == []


@pytest.mark.memory_only
def test_release_mode_requires_evidence_root(tmp_path):
    repo = fakes_retirement.retirement_repo(tmp_path, stage3_result="PASS")
    v = retirement.verify(fakes_retirement.approved(repo), repo_root=repo, mode="release", evidence_root=None,
                          bundle_manifests=fakes_retirement.bundle_args(repo))
    assert "retirement: evidence root required" in v


@pytest.mark.memory_only
def test_release_mode_requires_both_platforms(tmp_path):
    repo = fakes_retirement.retirement_repo(tmp_path, stage3_result="PASS")
    only_linux = {"linux": repo / "bundles" / "linux" / "release-manifest.json"}
    v = retirement.verify(fakes_retirement.approved(repo), repo_root=repo, mode="release",
                          evidence_root=repo / "evidence", bundle_manifests=only_linux)
    assert v != []


@pytest.mark.memory_only
def test_target_ids_must_match_index(tmp_path):
    repo = fakes_retirement.retirement_repo(tmp_path, stage3_result="PASS")
    r = fakes_retirement.approved(repo)
    r["stage3_target_artifact_ids"]["windows"] = "0" * 64                        # 與索引 target 不同
    v = retirement.verify(r, repo_root=repo, mode="repo")
    assert v != [] and all(x.startswith("retirement:") for x in v)


@pytest.mark.process_rpc
def test_not_retired_release_mode_not_approved(tmp_path):
    repo = fakes_retirement.retirement_repo(tmp_path, stage3_result="PASS")  # 預設未核准
    r = _cli("verify", "--mode", "release", "--repo-root", str(repo), "--evidence-root", str(repo / "evidence"),
             "--bundle-manifest", f"linux={repo / 'bundles' / 'linux' / 'release-manifest.json'}",
             "--bundle-manifest", f"windows={repo / 'bundles' / 'windows' / 'release-manifest.json'}")
    assert r.returncode == 2 and "HARNESS_RETIREMENT_NOT_APPROVED" in r.stdout + r.stderr


@pytest.mark.memory_only
def test_keep_list_missing_detected(tmp_path):
    repo = fakes_retirement.retirement_repo(tmp_path, stage3_result="PASS")
    shutil.rmtree(repo / "realhw")
    v = _release(repo, fakes_retirement.approved(repo))
    assert "retirement: keep path missing: realhw" in v


@pytest.mark.memory_only
def test_index_sha_mismatch(tmp_path):
    repo = fakes_retirement.retirement_repo(tmp_path, stage3_result="PASS")
    r = fakes_retirement.approved(repo)
    r["evidence_index_sha256"] = "0" * 64
    v = retirement.verify(r, repo_root=repo, mode="repo")
    assert "retirement: evidence index sha256 mismatch" in v


@pytest.mark.memory_only
def test_github_output():
    assert retirement.github_output({"retired": False}) == "retired=false\n"
    assert retirement.github_output({"retired": True}) == "retired=true\n"


@pytest.mark.memory_only
def test_pack_bundle_posix_members(tmp_path):
    repo = fakes_retirement.retirement_repo(tmp_path, stage3_result="PASS")
    bundle = repo / "bundles" / "linux"
    expected = sorted(p.relative_to(bundle).as_posix() for p in bundle.rglob("*") if p.is_file())
    for out in (tmp_path / "pack.zip", tmp_path / "pack.tar.gz"):
        retirement.pack_bundle(bundle, out)
        if out.suffix == ".zip":
            with zipfile.ZipFile(out) as zf:
                names = zf.namelist()
            assert not any(n.endswith("/") for n in names)                      # 只放一般檔，無目錄項
        else:
            with tarfile.open(out) as tf:
                members = tf.getmembers()
            assert all(m.isfile() for m in members)
            names = [m.name for m in members]
        assert all("\\" not in n and not n.startswith("/") and ".." not in n.split("/") for n in names)
        assert names == sorted(names)                                           # 依 relpath 排序
        assert set(names) == set(expected)                                      # 無外層目錄、集合等於全部一般檔


@pytest.mark.memory_only
def test_verify_archive_format_must_match_platform(tmp_path):
    repo = fakes_retirement.retirement_repo(tmp_path, stage3_result="PASS")
    r = fakes_retirement.approved(repo)
    zip_path = tmp_path / "linux.zip"
    retirement.pack_bundle(repo / "bundles" / "linux", zip_path)
    v, _ = retirement.verify_archive(r, repo_root=repo, platform="linux", archive=zip_path)
    assert "retirement: archive format mismatch: linux" in v


@pytest.mark.process_rpc
def test_verify_archive_not_retired(tmp_path):
    repo = fakes_retirement.retirement_repo(tmp_path, stage3_result="PASS")  # 未核准
    archive, sums = tmp_path / "serialwrap-linux.tar.gz", tmp_path / "SHA256SUMS-linux"
    retirement.pack_bundle(repo / "bundles" / "linux", archive)
    r = _cli("verify-archive", "--repo-root", str(repo), "--platform", "linux",
             "--archive", str(archive), "--sha256sums", str(sums))
    assert r.returncode == 2 and "HARNESS_RETIREMENT_NOT_APPROVED" in r.stdout + r.stderr
    assert not sums.exists()


@pytest.mark.memory_only
def test_schema_stage3_requires_marker(tmp_path):
    import jsonschema

    schema = json.loads((ROOT / "contracts" / "release_manifest.schema.json").read_text(encoding="utf-8"))

    def doc_of(bundle: Path) -> dict:
        return json.loads((bundle / "release-manifest.json").read_text(encoding="utf-8"))

    unmarked = doc_of(fakes.make_bundle(tmp_path / "s3-unmarked", stage="3", retirement_marker=False))
    with pytest.raises(jsonschema.ValidationError):
        jsonschema.validate(unmarked, schema)

    marked = doc_of(fakes.make_bundle(tmp_path / "s3-marked", stage="3", retirement_marker=True))
    jsonschema.validate(marked, schema)                                         # 應通過

    stage24 = doc_of(fakes.make_bundle(tmp_path / "s24-marked", stage="2.4", retirement_marker=True))
    with pytest.raises(jsonschema.ValidationError):
        jsonschema.validate(stage24, schema)


@pytest.mark.memory_only
def test_schema_marker_forbids_python_delegate(tmp_path):
    import jsonschema

    schema = json.loads((ROOT / "contracts" / "release_manifest.schema.json").read_text(encoding="utf-8"))
    base = json.loads((fakes.make_bundle(tmp_path / "s3", stage="3", retirement_marker=True)
                       / "release-manifest.json").read_text(encoding="utf-8"))
    jsonschema.validate(base, schema)                                           # 不帶委派的退役 manifest 必須通過
    base["components"]["python_cli_delegate"] = {"entry": ["py/python", "-m", "sw_core.cli"], "tree_sha256": "0" * 64}
    with pytest.raises(jsonschema.ValidationError):
        jsonschema.validate(base, schema)


@pytest.mark.memory_only
def test_manifest_has_no_approval_fields():
    schema = json.loads((ROOT / "contracts" / "release_manifest.schema.json").read_text(encoding="utf-8"))
    ret = schema["properties"]["retirement"]
    assert ret.get("additionalProperties") is False
    assert set(ret["properties"]) == {"python_daemon"}
    assert ret["properties"]["python_daemon"].get("const") == "removed"


@pytest.mark.memory_only
def test_release_workflow_gates_python_artifact_chains():
    import yaml

    wf = yaml.safe_load((ROOT / ".github" / "workflows" / "release.yml").read_text(encoding="utf-8"))
    jobs = wf["jobs"]
    assert "retirement-gate" in jobs
    off = "needs.retirement-gate.outputs.retired != 'true'"
    on = "needs.retirement-gate.outputs.retired == 'true'"
    assert off in jobs["publish-windows-exe"].get("if", "")
    for step in jobs["publish"]["steps"]:
        run = step.get("run", "")
        if re.search(r"build --wheel|\.whl", run):
            assert off in step.get("if", ""), step.get("name")
        if re.search(r"pytest|policy_check|gh release create", run):
            assert "retirement-gate" not in step.get("if", ""), step.get("name")
    for name in ("publish-stage3-bundle-linux", "publish-stage3-bundle-windows"):
        job = jobs[name]
        assert on in job["if"]
        runs = " ".join(st.get("run", "") for st in job["steps"])
        assert "retirement verify --mode release" in runs and "--bundle-manifest" in runs
        assert "mkbundle.sh" not in runs and "cargo build" not in runs
        assert "Compress-Archive" not in runs and not re.search(r"(^|[\s;|&])tar\s", runs)   # R2-F7：只用 retirement pack
        idx = {key: next(i for i, st in enumerate(job["steps"]) if key in st.get("run", ""))
               for key in ("retirement verify --mode release", "retirement pack", "retirement verify-archive", "gh release upload")}
        assert idx["retirement verify --mode release"] < idx["retirement pack"] < idx["retirement verify-archive"] \
            < idx["gh release upload"], idx
        upload = job["steps"][idx["gh release upload"]]["run"]
        assert "SHA256SUMS-" in upload and "SW_STAGE3_BUNDLE_DIR" not in upload   # 只上傳已驗證 archive 與其 SHA256SUMS
        for step in job["steps"]:
            for env_name, value in step.get("env", {}).items():
                assert not (env_name.startswith("SW_") and re.fullmatch(
                    rf"\$\{{\{{\s*env\.{re.escape(env_name)}\s*\}}\}}", value
                )), (name, step.get("name"), env_name, value)


@pytest.mark.memory_only
def test_pytest_suite_does_not_depend_on_wheel():
    files = (sorted(ROOT.glob("tests/test_stage3_*.py"))
             + sorted((ROOT / "realhw").rglob("*.py")) + sorted((ROOT / "regression").rglob("*.py")))
    bad: list[tuple[str, int, str]] = []
    for path in files:
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                for alias in node.names:
                    if alias.name.split(".")[0] == "pkg_resources":
                        bad.append((str(path), node.lineno, "pkg_resources"))
            elif isinstance(node, ast.ImportFrom):
                if (node.module or "").split(".")[0] == "pkg_resources":
                    bad.append((str(path), node.lineno, "pkg_resources"))
            elif isinstance(node, ast.Call):
                name = _dotted(node.func)
                if (name.endswith("distribution") and "metadata" in name and node.args
                        and isinstance(node.args[0], ast.Constant) and node.args[0].value == "serialwrap"):
                    bad.append((str(path), node.lineno, "importlib.metadata.distribution('serialwrap')"))
    assert bad == []


@pytest.mark.memory_only
@pytest.mark.parametrize("rel", ["py/python.exe", "lib/sw_core/cli.py", "serialwrapd.py"])
def test_release_and_archive_refuse_python_runtime_payload(tmp_path, rel):
    repo = fakes_retirement.retirement_repo(tmp_path, stage3_result="PASS")
    approved = fakes_retirement.approved(repo)
    bundle = repo / "bundles" / "linux"
    residual = bundle / rel
    residual.parent.mkdir(parents=True, exist_ok=True)
    residual.write_bytes(b"python runtime")

    release_violations = _release(repo, approved)
    detected_path = "py" if rel.startswith("py/") else rel
    assert f"retirement: bundle contains python runtime: linux: {detected_path}" in release_violations

    archive = tmp_path / "serialwrap-linux.tar.gz"
    retirement.pack_bundle(bundle, archive)
    archive_violations, _ = retirement.verify_archive(
        approved, repo_root=repo, platform="linux", archive=archive
    )
    assert f"retirement: archive contains python runtime: linux: {detected_path}" in archive_violations


@pytest.mark.memory_only
def test_verify_archive_rechecks_index_hash_and_target_identity(tmp_path):
    repo = fakes_retirement.retirement_repo(tmp_path, stage3_result="PASS")
    approved = fakes_retirement.approved(repo)
    bundle = repo / "bundles" / "linux"
    archive = tmp_path / "serialwrap-linux.tar.gz"
    retirement.pack_bundle(bundle, archive)

    index = repo / "docs" / "rust-refactor" / "evidence" / "index.json"
    index.write_bytes(index.read_bytes() + b" ")
    violations, _ = retirement.verify_archive(
        approved, repo_root=repo, platform="linux", archive=archive
    )
    assert "retirement: evidence index sha256 mismatch" in violations

    approved = fakes_retirement.approved(repo)
    approved["stage3_target_artifact_ids"]["linux"] = "0" * 64
    violations, _ = retirement.verify_archive(
        approved, repo_root=repo, platform="linux", archive=archive
    )
    assert "retirement: stage3 target artifact mismatch: linux" in violations


@pytest.mark.memory_only
def test_unpack_archive_reports_unsupported_compression(tmp_path, monkeypatch):
    bundle = tmp_path / "bundle"
    bundle.mkdir()
    (bundle / "one.txt").write_text("one", encoding="utf-8")
    archive = tmp_path / "one.zip"
    retirement.pack_bundle(bundle, archive)

    def unsupported(*_args, **_kwargs):
        raise NotImplementedError("unsupported compression")

    monkeypatch.setattr(retirement.zipfile.ZipFile, "open", unsupported)
    result = retirement._unpack_archive(archive.read_bytes(), tmp_path / "extract", kind="zip")
    assert result is not None and result.startswith("retirement: archive unreadable:")
