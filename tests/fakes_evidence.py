"""Stage 3 證據索引測試使用的隔離 fixture builder。"""

from __future__ import annotations

import hashlib
import json
from dataclasses import asdict, replace
from pathlib import Path
from typing import Any, Iterable


ENV_DOC: dict[str, Any] = {
    "os_build": "fixture-os",
    "cpu": "fixture-cpu",
    "ram_mb": 4096,
    "filesystem": "fixture-fs",
    "av_product": "fixture-av",
    "drivers": "fixture-drivers",
    "board_firmware": "fixture-firmware",
    "toolchains": "fixture-toolchains",
}


def _evidence_api():
    """延遲載入 T14 介面，讓替身可在實作前單獨匯入。"""
    from tools.stage3 import evidence_index

    return evidence_index


def _ab_manifest_api():
    """延遲載入既有 T3 的 FrozenFile 與 bundle 驗證介面。"""
    from tools.stage3 import ab_manifest

    return ab_manifest


def _jsonio_api():
    from tools.stage3 import jsonio

    return jsonio


def _stage3_fakes():
    """延遲載入 T2 的 bundle 與 archive 替身。"""
    import stage3_fakes

    return stage3_fakes


def _write_json(path: Path, value: Any) -> None:
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    data = json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":")) + "\n"
    path.write_bytes(data.encode("utf-8"))


def _file_sha256(path: Path) -> str:
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def read_seed_revision(revision: str, repo_path: str) -> bytes | None:
    """從指定 Git revision 取回測試所需的原始 seed blob。"""
    import subprocess

    result = subprocess.run(
        ["git", "show", f"{revision}:{repo_path}"],
        cwd=Path(__file__).resolve().parents[1],
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        check=False,
    )
    return result.stdout if result.returncode == 0 else None


def _payload_sha256(value: Any) -> str:
    data = json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(data.encode("utf-8")).hexdigest()


def _evidence_root(root: Path) -> Path:
    return Path(root) / "docs" / "rust-refactor" / "evidence"


def _write_legacy_scope(root: Path, index_path: Path, catalog: dict[str, Any]) -> None:
    """建立完整的合成 S3-E 範圍與核定附件；resolution 保持未完成。"""
    evidence_root = _evidence_root(root)
    evidence_root.mkdir(parents=True, exist_ok=True)

    case_lock_path = Path(__file__).resolve().parent / "acceptance" / "cases.lock.json"
    case_lock = json.loads(case_lock_path.read_bytes())
    case_sets = case_lock.get("sets", {})
    obligations: list[dict[str, Any]] = []
    for item in catalog.values():
        if item.stage == "3" or item.release_requirement != "required":
            continue
        platforms = ("linux", "windows") if item.platform == "both" else (item.platform,)
        case_ids = list(case_sets.get(item.required_case_set, [])) if item.required_case_set else []
        if not case_ids:
            case_ids = [f"fixture:{item.item_id}"]
        for platform in platforms:
            scope = {"case_ids": case_ids}
            obligations.append(
                {
                    "key": f"{item.stage}:{item.item_id}:{platform}",
                    "stage": item.stage,
                    "platform": platform,
                    "item_id": item.item_id,
                    "source_refs": [item.spec_ref],
                    "required_scope": scope,
                    "required_scope_sha256": _payload_sha256(scope),
                    "safety_invariant": True,
                    "allowed_evidence": {
                        "kinds": [item.evidence_kind],
                        "case_ids": case_ids,
                    },
                }
            )

    # 來源清冊中的非目錄義務保留獨立 key，並以 fixture 識別值對應現行 schema。
    non_catalog_scope = {"case_ids": ["fixture:legacy-source-obligation"]}
    obligations.append(
        {
            "key": "legacy:fixture-source-obligation",
            "stage": "legacy",
            "platform": "linux",
            "item_id": "LEGACY-FIXTURE-SOURCE-OBLIGATION",
            "source_refs": ["fixture:legacy-source-inventory"],
            "required_scope": non_catalog_scope,
            "required_scope_sha256": _payload_sha256(non_catalog_scope),
            "safety_invariant": False,
            "allowed_evidence": {
                "kinds": ["manual-procedure"],
                "case_ids": non_catalog_scope["case_ids"],
            },
        }
    )

    source_ids = sorted(
        {source_ref for obligation in obligations for source_ref in obligation["source_refs"]}
    )
    sources: list[dict[str, Any]] = []
    source_hashes: list[dict[str, str]] = []
    for number, source_id in enumerate(source_ids):
        source_path = evidence_root / "legacy-sources" / f"source-{number:03d}.json"
        _write_json(source_path, {"fixture_source_id": source_id})
        source_sha256 = _file_sha256(source_path)
        sources.append(
            {
                "source_id": source_id,
                "version": "fixture-v1",
                "snapshot": {
                    "path": source_path.relative_to(evidence_root).as_posix(),
                    "sha256": source_sha256,
                },
            }
        )
        source_hashes.append({"source_id": source_id, "sha256": source_sha256})

    catalog_path = Path(__file__).resolve().parent / "acceptance" / "stage_exits.json"
    scope_body = {
        "schema_version": 1,
        "exit_catalog_sha256": _file_sha256(catalog_path),
        "sources": sources,
        "obligations": obligations,
    }
    review_payload = {
        "fixture": "owner-review-record",
        "source_hashes": source_hashes,
        "scope_sha256": _payload_sha256(scope_body),
    }
    review_path = evidence_root / "legacy-scope-review.json"
    _write_json(review_path, review_payload)
    scope = {
        **scope_body,
        "review": {
            "reviewer_id": "fixture-owner",
            "approved_at": "2026-10-31T00:00:00+00:00",
            "decision_ref": "OD-FIXTURE",
            "record": {
                "path": review_path.relative_to(evidence_root).as_posix(),
                "sha256": _file_sha256(review_path),
            },
        },
    }
    scope_path = evidence_root / "legacy-obligations-scope.json"
    _write_json(scope_path, scope)

    index = json.loads(Path(index_path).read_bytes())
    index["legacy_obligations"] = {
        "scope": {
            "path": scope_path.relative_to(evidence_root).as_posix(),
            "sha256": _file_sha256(scope_path),
        },
        "resolutions": None,
    }
    _write_json(index_path, index)


def _copy_denominator_inputs(root: Path) -> None:
    """保存指定 Git revision 的公開 seed 與目前分母鎖快照。"""
    import subprocess

    from tools.stage3 import required_cases

    repo_root = Path(__file__).resolve().parents[1]
    evidence_root = _evidence_root(root)
    sources = [
        (repo_root / "tests" / "acceptance" / "stage3_required_cases.lock.json", "stage3-required-cases.lock.json"),
        (repo_root / "docs" / "rust-refactor" / "evidence" / "denominators" / "source-manifest.json", "denominators/source-manifest.json"),
    ]
    for repo_path, relative in (
        ("tests/acceptance/cases.yaml", "denominators/cases.yaml"),
        ("tests/acceptance/cases.lock.json", "denominators/cases.lock.json"),
    ):
        result = subprocess.run(
            ["git", "show", f"{required_cases.SEED_REVISION}:{repo_path}"],
            cwd=repo_root,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            check=False,
        )
        if result.returncode:
            raise RuntimeError(f"無法讀取指定 seed {repo_path}: {result.stderr.decode('utf-8', errors='replace')}")
        target = evidence_root / relative
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(result.stdout)
    for source, relative in sources:
        target = evidence_root / relative
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(source.read_bytes())


def _entry_id(entry: Any) -> str:
    fields = asdict(entry)
    fields.pop("entry_id", None)
    fields["record_id"] = ""
    return _jsonio_api().record_id(fields)


def make_bundle(root: Path, *, stage: str = "3", target: str = "x86_64-unknown-linux-gnu") -> Path:
    """呼叫 T2 builder 建立隔離 bundle。"""
    return _stage3_fakes().make_bundle(Path(root), stage=stage, target=target)


def evidence_index(
    root: Path,
    *,
    stage: str,
    artifact_manifest: Path | None = None,
    contract: str,
    tool: str = "t" * 40,
    case_list: str = "0" * 64,
    platforms: Iterable[str] = ("linux", "windows"),
) -> Path:
    """建立 index、公開 evidence root、清冊 fixture 與逐平台 target。"""
    api = _evidence_api()
    root = Path(root)
    root.mkdir(parents=True, exist_ok=True)
    evidence_root = _evidence_root(root)
    evidence_root.mkdir(parents=True, exist_ok=True)
    index_path = root / "index.json"
    _write_json(
        index_path,
        {
            "candidate_rounds": [],
            "current_candidate_round_id": None,
            "entries": [],
            "legacy_obligations": {"resolutions": None, "scope": None},
            "schema_version": 2,
            "stage_targets": {},
            "target_history": [],
        },
    )

    catalog = api.load_exit_catalog()
    if stage == "3":
        _write_legacy_scope(root, index_path, catalog)
        _copy_denominator_inputs(root)
        round_id = api.begin_candidate_round(
            index_path,
            rc_tag="v1.0.0-rc.1",
            set_by="tests/fakes_evidence.py",
            now="2026-10-30T00:00:00+00:00",
        )
    else:
        round_id = None

    for platform in platforms:
        target = "x86_64-pc-windows-msvc" if platform == "windows" else "x86_64-unknown-linux-gnu"
        bundle = root / "bundles" / platform
        make_bundle(bundle, stage=stage, target=target)
        manifest_path = bundle / "release-manifest.json"
        if artifact_manifest is not None:
            source = Path(artifact_manifest)
            if source.resolve() != manifest_path.resolve():
                manifest_path.write_bytes(source.read_bytes())

        kwargs: dict[str, Any] = {
            "stage": stage,
            "platform": platform,
            "release_manifest": manifest_path,
            "contract_revision": contract,
            "tool_revision": tool,
            "case_list_sha256": case_list,
            "set_by": "tests/fakes_evidence.py",
            "now": "2026-10-31T00:00:00+00:00",
        }
        if stage == "3":
            kwargs.update(
                {
                    "bundle_dir": bundle,
                    "archive": _stage3_fakes().rc_archive(bundle),
                    "candidate_round_id": round_id,
                }
            )
        api.set_target(index_path, **kwargs)
    return index_path


def entry(
    root: Path,
    item_id: str,
    result: str,
    *,
    platform: str,
    n: int = 0,
    files: tuple[Any, ...] | None = None,
    **over: Any,
):
    """建立一般 EvidenceEntry；預設報告位於 docs/rust-refactor/evidence。"""
    api = _evidence_api()
    ab_manifest = _ab_manifest_api()
    fakes = _stage3_fakes()
    root = Path(root)
    index_path = root / "index.json"
    index = api.load_index(index_path)
    catalog = api.load_exit_catalog()
    item = catalog.get(item_id)
    stage = item.stage if item is not None else next(iter(index.get("stage_targets", {})), "3")
    target = index.get("stage_targets", {}).get(stage, {}).get(platform)
    if target is None:
        target = next(
            (targets[platform] for targets in index.get("stage_targets", {}).values() if platform in targets),
            None,
        )
    if target is None:
        raise AssertionError(f"fixture 缺少 {platform} target")

    evidence_root = _evidence_root(root)
    if files is None:
        report = evidence_root / f"{item_id}-{platform}-{n}.json"
        _write_json(report, {"item_id": item_id, "result": result, "n": n})
        files = (
            ab_manifest.FrozenFile(
                path=report.relative_to(evidence_root).as_posix(),
                sha256=_file_sha256(report),
            ),
        )

    evidence_kind = item.evidence_kind if item is not None else "manual-procedure"
    is_l3 = item is not None and item.layer == "L3"
    timestamp = fakes.ts(10 + n, 0)
    ledger_through = timestamp if evidence_kind == "canary-report" else None
    ledger_records = 100 + n if evidence_kind == "canary-report" else None
    values: dict[str, Any] = {
        "exit_catalog_sha256": target.get("exit_catalog_sha256", "0" * 64),
        "candidate_round_id": target.get("candidate_round_id") if stage == "3" else None,
        "archive_sha256": target.get("archive_sha256") if stage == "3" else None,
        "entry_id": "",
        "item_id": item_id,
        "platform": platform,
        "result": result,
        "artifact_id": target["artifact_id"],
        "runtime_digest": target["runtime_digest"],
        "bundle_tree_sha256": target.get("bundle_tree_sha256"),
        "runtime_tree_sha256": target.get("runtime_tree_sha256"),
        "contract_revision": target["contract_revision"],
        "tool_revision": target["tool_revision"],
        "case_list_sha256": target["case_list_sha256"],
        "required_cases_sha256": target.get("required_cases_sha256") if stage == "3" else None,
        "l1_set_sha256": None,
        "workload_sha256": "f" * 64 if is_l3 else None,
        "environment_id": api.environment_id(ENV_DOC),
        "produced_at": timestamp,
        "started_at": timestamp,
        "producer": "ingest:test",
        "files": tuple(files),
        "xfail_cases": (),
        "reuse_of": None,
        "note": "測試替身證據",
        "ledger_through": ledger_through,
        "ledger_records": ledger_records,
    }
    values.update(over)
    entry_obj = api.EvidenceEntry(**values)
    return replace(entry_obj, entry_id=_entry_id(entry_obj))


def restricted_entry(
    root: Path,
    item_id: str,
    *,
    platform: str,
    failure_summary: str = "去識別的測試失敗摘要",
    **over: Any,
) -> Any:
    """建立只含公開最小欄位的 R8-01 restricted FAIL。"""
    api = _evidence_api()
    target = api.load_index(Path(root) / "index.json")["stage_targets"]["3"][platform]
    fields: dict[str, Any] = {
        "result": "FAIL",
        "source_visibility": "restricted",
        "artifact_id": target["artifact_id"],
        "platform": platform,
        "item_id": item_id,
        "candidate_round_id": target["candidate_round_id"],
        "context_id": "a" * 64,
        "private_source_sha256": "b" * 64,
        "failure_summary": failure_summary,
        "verified_by": "fixture-verifier",
        "verified_at": "2026-11-02T00:00:00+00:00",
    }
    fields.update(over)
    fields["entry_id"] = _jsonio_api().record_id({**fields, "record_id": ""})
    return api.RestrictedFailEntry(**fields)


def target_trees(index_path: Path, platform: str, *, stage: str = "3") -> dict[str, str | None]:
    """回傳 target 凍結的 bundle／runtime tree 指紋。"""
    target = _evidence_api().load_index(index_path)["stage_targets"][stage][platform]
    return {
        "bundle_tree_sha256": target.get("bundle_tree_sha256"),
        "runtime_tree_sha256": target.get("runtime_tree_sha256"),
    }


def deployed_bracket(cli_exe: Path) -> dict[str, Any]:
    """以既有 P2 helper 實際量測安裝內容；不從 target 人工回填。"""
    from realhw.deployed_content import measure

    cli = Path(cli_exe)
    daemon_name = "serialwrapd.exe" if cli.suffix.casefold() == ".exe" else "serialwrapd"
    measured = measure(cli, daemon_argv=[str(cli.with_name(daemon_name))])
    return {"start": measured, "end": dict(measured)}
