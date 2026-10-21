"""Stage 3 evidence index 測試使用的隔離證據與 bundle 替身。"""

from __future__ import annotations

import hashlib
import json
import shutil
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
    """延遲載入尚待實作的 evidence_index，讓整套測試仍可 collection。"""
    from tools.stage3 import evidence_index

    return evidence_index


def _ab_manifest_api():
    """延遲載入 Task 3 bundle 驗證介面。"""
    from tools.stage3 import ab_manifest

    return ab_manifest


def _jsonio_api():
    from tools.stage3 import jsonio

    return jsonio


def _write_json(path: Path, value: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":")) + "\n",
        encoding="utf-8",
    )


def _file_sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _tree_sha256(root: Path) -> str:
    """依 Stage 3 bundle tree 契約獨立計算測試替身目錄。"""
    rows = []
    for path in root.rglob("*"):
        if path.is_file():
            rel = path.relative_to(root).as_posix()
            rows.append((rel, _file_sha256(path)))
    payload = "".join(f"{rel}\0{digest}\n" for rel, digest in sorted(rows, key=lambda row: row[0].encode()))
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def make_bundle(root: Path, *, stage: str = "3", target: str = "x86_64-unknown-linux-gnu") -> Path:
    """建立內容完全在暫存目錄中的小型 bundle，供 target 與 drift 案例使用。"""
    root = Path(root)
    root.mkdir(parents=True, exist_ok=True)
    platform_slug = "windows" if "windows" in target else "linux"
    cli_bytes = f"serialwrap-cli-{platform_slug}".encode("ascii")
    daemon_bytes = f"serialwrap-daemon-{platform_slug}".encode("ascii")
    cli_path = root / "bin" / "serialwrap"
    daemon_path = root / "bin" / "serialwrapd"
    cli_path.parent.mkdir(parents=True, exist_ok=True)
    cli_path.write_bytes(cli_bytes)
    daemon_path.write_bytes(daemon_bytes)

    version = "0.9.0"
    git_revision = "1" * 40
    components: dict[str, Any] = {
        "cli": {
            "implementation": "rust",
            "version": version,
            "git_revision": git_revision,
            "path": "bin/serialwrap",
            "sha256": _file_sha256(cli_path),
        },
        "daemon": {
            "implementation": "rust",
            "version": version,
            "git_revision": git_revision,
            "launcher": {"path": "bin/serialwrapd", "sha256": _file_sha256(daemon_path)},
        },
        "assets": {
            "skill_sha256": "2" * 64,
            "skill_windows_sha256": "3" * 64,
            "profiles_sha256": "4" * 64,
        },
        "supervisor": {"default_mode": "on-demand", "definition_sha256": "5" * 64},
    }
    manifest: dict[str, Any] = {
        "schema_version": 1,
        "bundle_version": version,
        "stage": stage,
        "created_at": "2026-10-30T00:00:00+00:00",
        "target": target,
        "git_revision": git_revision,
        "contract_revision": "6" * 40,
        "components": components,
        "pairing": {"rule": "exact", "cli_version": version, "daemon_version": version},
    }
    if stage == "3":
        manifest["retirement"] = {"python_daemon": "removed"}
    else:
        py_file = root / "py" / "sw_core" / "cli.py"
        py_file.parent.mkdir(parents=True, exist_ok=True)
        py_file.write_text("# fixture\n", encoding="utf-8")
        components["python_cli_delegate"] = {
            "entry": ["python3", "-I", "-B", "-m", "sw_core.cli"],
            "tree_sha256": _tree_sha256(root / "py"),
        }

    manifest_path = root / "release-manifest.json"
    _write_json(manifest_path, manifest)
    sums = "".join(
        f"{_file_sha256(path)}  {path.relative_to(root).as_posix()}\n"
        for path in sorted((cli_path, daemon_path), key=lambda item: item.relative_to(root).as_posix())
    )
    (root / "SHA256SUMS").write_text(sums, encoding="utf-8")
    return root


def _write_empty_index(index_path: Path) -> None:
    _write_json(
        index_path,
        {"entries": [], "schema_version": 2, "stage_targets": {}, "target_history": []},
    )


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
    """建立私有 index、evidence root 與逐平台 target。"""
    api = _evidence_api()
    root = Path(root)
    root.mkdir(parents=True, exist_ok=True)
    (root / "evidence").mkdir(exist_ok=True)
    index_path = root / "index.json"
    _write_empty_index(index_path)

    for platform in platforms:
        target = "x86_64-pc-windows-msvc" if platform == "windows" else "x86_64-unknown-linux-gnu"
        bundle = root / "bundles" / platform
        make_bundle(bundle, stage=stage, target=target)
        manifest_path = bundle / "release-manifest.json"
        if artifact_manifest is not None:
            source = Path(artifact_manifest)
            if source.resolve() != manifest_path.resolve():
                shutil.copyfile(source, manifest_path)
        api.set_target(
            index_path,
            stage=stage,
            platform=platform,
            release_manifest=manifest_path,
            contract_revision=contract,
            tool_revision=tool,
            case_list_sha256=case_list,
            set_by="tests/fakes_evidence.py",
            now="2026-10-31T00:00:00+00:00",
            bundle_dir=bundle if stage == "3" else None,
        )
    return index_path


def _entry_id(entry: Any) -> str:
    fields = asdict(entry)
    fields.pop("entry_id", None)
    fields["record_id"] = ""
    return _jsonio_api().record_id(fields)


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
    """建立符合 EvidenceEntry 形狀的報告紀錄，預設檔案會寫在隔離 evidence root。"""
    api = _evidence_api()
    ab_manifest = _ab_manifest_api()
    from fakes_od09 import ts

    root = Path(root)
    root.mkdir(parents=True, exist_ok=True)
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

    if files is None:
        report = root / "evidence" / f"{item_id}-{platform}-{n}.json"
        report.parent.mkdir(parents=True, exist_ok=True)
        report.write_text(json.dumps({"item_id": item_id, "result": result, "n": n}, sort_keys=True) + "\n",
                          encoding="utf-8")
        files = (ab_manifest.FrozenFile(path=report.relative_to(root).as_posix(), sha256=_file_sha256(report)),)

    evidence_kind = item.evidence_kind if item is not None else "manual-procedure"
    is_l3 = item is not None and item.layer == "L3"
    ledger_through = ts(10 + n, 0) if evidence_kind == "canary-report" else None
    ledger_records = 100 + n if evidence_kind == "canary-report" else None
    values: dict[str, Any] = {
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
        "l1_set_sha256": None,
        "workload_sha256": "w" * 64 if is_l3 else None,
        "environment_id": api.environment_id(ENV_DOC),
        "produced_at": ts(10 + n, 0),
        "started_at": ts(10 + n, 0),
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


def target_trees(index_path: Path, platform: str, *, stage: str = "3") -> dict[str, str | None]:
    """回傳 target 已凍結的 bundle 與 runtime tree 指紋。"""
    target = _evidence_api().load_index(index_path)["stage_targets"][stage][platform]
    return {
        "bundle_tree_sha256": target.get("bundle_tree_sha256"),
        "runtime_tree_sha256": target.get("runtime_tree_sha256"),
    }


def deployed_bracket(cli_exe: Path) -> dict[str, Any]:
    """以 P2 deployed_content.measure 實際量測安裝內容的前後值。"""
    from realhw.deployed_content import measure

    measured = measure(Path(cli_exe))
    return {"start": measured, "end": measured}
