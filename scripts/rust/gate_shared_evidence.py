#!/usr/bin/env python3
"""產生、驗證與匯入綁定候選 HEAD 的 S2 共用 gate 證據。"""
from __future__ import annotations

import argparse
import hashlib
import importlib.util
import json
import os
import re
import shutil
import subprocess
import sys
from pathlib import Path, PurePosixPath
from typing import Any, Mapping, Sequence

ROOT = Path(__file__).resolve().parents[2]
DEFAULT_LOCK = ROOT / "tests/acceptance/l1_required.lock.json"
SCHEMA = "serialwrap-gate-shared-evidence.v1"
REQUIRED_ARTIFACTS = ("debug_cli", "debug_daemon", "release_cli", "release_daemon")
ARTIFACT_PATHS = {
    "debug_cli": "debug/serialwrap",
    "debug_daemon": "debug/serialwrapd",
    "release_cli": "release/serialwrap",
    "release_daemon": "release/serialwrapd",
}


def sha256_file(path: Path) -> str | None:
    try:
        return hashlib.sha256(path.read_bytes()).hexdigest()
    except OSError:
        return None


def get_current_head(root: Path = ROOT) -> str | None:
    try:
        return subprocess.run(["git", "rev-parse", "HEAD"], cwd=root, capture_output=True, text=True,
                              check=True).stdout.strip()
    except (OSError, subprocess.CalledProcessError):
        return None


def is_worktree_clean(root: Path = ROOT) -> bool:
    try:
        result = subprocess.run(["git", "status", "--porcelain", "--untracked-files=normal"], cwd=root,
                                capture_output=True, text=True, check=True)
    except (OSError, subprocess.CalledProcessError):
        return False
    return not result.stdout.strip()


def _safe_path(base: Path, raw: Any) -> Path | None:
    if not isinstance(raw, str) or not raw:
        return None
    candidate = Path(raw)
    if candidate.is_absolute():
        return None
    try:
        resolved = (base / candidate).resolve(strict=True)
        resolved.relative_to(base.resolve(strict=True))
    except (OSError, ValueError):
        return None
    return resolved


def _safe_role(raw: str) -> Path | None:
    rel = PurePosixPath(raw)
    if rel.is_absolute() or not raw or ".." in rel.parts:
        return None
    return Path(*rel.parts)


def _load_manifest(shared_dir: Path) -> tuple[dict[str, Any] | None, list[str]]:
    path = shared_dir / "manifest.json"
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        return None, [f"manifest 不可讀：{exc}"]
    if not isinstance(data, dict) or data.get("schema") != SCHEMA:
        return None, [f"manifest schema 須為 {SCHEMA}"]
    return data, []


def _locked_argv(set_id: str, platform: str, lock_path: Path) -> tuple[list[str] | None, str | None]:
    try:
        lock = json.loads(lock_path.read_text(encoding="utf-8"))
        selector = lock["sets"][set_id][platform]["selector"]
        argv = selector["argv"]
    except (OSError, ValueError, KeyError, TypeError) as exc:
        return None, f"鎖檔 selector 不可讀：{set_id}/{platform}：{exc}"
    if not isinstance(argv, list) or not all(isinstance(part, str) for part in argv):
        return None, f"鎖檔 selector argv 格式錯誤：{set_id}/{platform}"
    return argv, None


def _locked_nodeids(set_id: str, platform: str, lock_path: Path) -> tuple[list[str] | None, str | None]:
    try:
        lock = json.loads(lock_path.read_text(encoding="utf-8"))
        nodeids = lock["sets"][set_id][platform]["nodeids"]
    except (OSError, ValueError, KeyError, TypeError) as exc:
        return None, f"鎖檔 nodeids 不可讀：{set_id}/{platform}：{exc}"
    if not isinstance(nodeids, list) or not nodeids or not all(isinstance(item, str) and item for item in nodeids):
        return None, f"鎖檔 nodeids 格式錯誤：{set_id}/{platform}"
    if len(nodeids) != len(set(nodeids)):
        return None, f"鎖檔 nodeids 有重複項：{set_id}/{platform}"
    return nodeids, None


def _evaluate_windows_l1(report: Mapping[str, Any], *, required_set: str, expected_head: str,
                         lock_path: Path) -> dict[str, Any]:
    """用全計畫唯一的 l1_judge 核對原生集合、selector、結果與 provenance。"""
    reasons: list[str] = []
    selector, selector_error = _locked_argv(required_set, "windows", lock_path)
    nodeids, nodeids_error = _locked_nodeids(required_set, "windows", lock_path)
    if selector_error:
        reasons.append(selector_error)
    if nodeids_error:
        reasons.append(nodeids_error)
    if report.get("format") != "l1-results.v1" or report.get("runner") != "cargo" \
            or report.get("platform") != "windows":
        reasons.append("Windows L1 必須是 Cargo l1-results.v1")
    if selector is not None and report.get("argv") != selector:
        reasons.append(f"Windows L1 argv 不符：report={report.get('argv')!r} lock={selector!r}")
    native = report.get("provenance")
    native = native if isinstance(native, Mapping) else {}
    git = native.get("git")
    git = git if isinstance(git, Mapping) else {}
    end = git.get("end")
    end = end if isinstance(end, Mapping) else {}
    if native.get("schema") != "cargo-l1-provenance.v1" \
            or native.get("platform") != "windows" \
            or native.get("build_target") != "x86_64-pc-windows-gnu":
        reasons.append("Windows L1 缺少原生 Windows Cargo provenance")
    for label, row in (("provenance.git", git), ("provenance.git.end", end)):
        if row.get("head") != expected_head:
            reasons.append(f"{label}.head={row.get('head')!r} 與候選 HEAD {expected_head!r} 不符")
        if row.get("clean") is not True:
            reasons.append(f"{label}.clean={row.get('clean')!r}（須為 true）")
    exit_code = report.get("exit_code")
    if type(exit_code) is not int:
        reasons.append("Windows L1 exit_code 缺失或格式錯誤")
    if reasons:
        return {"status": "EVIDENCE_MISSING", "reasons": reasons, "exit_code": exit_code}

    judge_path = ROOT / "tests/acceptance/l1_judge.py"
    spec = importlib.util.spec_from_file_location("serialwrap_gate_shared_l1_judge", judge_path)
    if spec is None or spec.loader is None:
        return {"status": "EVIDENCE_MISSING", "reasons": ["無法載入 tests/acceptance/l1_judge.py"]}
    module = importlib.util.module_from_spec(spec)
    try:
        spec.loader.exec_module(module)
        judged = module.judge(report, nodeids or [], platform="windows", selector={"argv": selector or []})
    except (OSError, ValueError, TypeError, AttributeError) as exc:
        return {"status": "EVIDENCE_MISSING", "reasons": [f"l1_judge 執行失敗：{exc}"]}
    verdict = judged.get("verdict") if isinstance(judged, Mapping) else None
    if verdict not in ("PASS", "FAIL"):
        return {"status": "EVIDENCE_MISSING", "reasons": list(judged.get("reasons") or [])
                if isinstance(judged, Mapping) else ["l1_judge 結果格式錯誤"], "exit_code": exit_code}
    return {"status": verdict, "reasons": list(judged.get("reasons") or []), "exit_code": exit_code}


def _command_matches(name: str, argv: Any, shared_dir: Path) -> bool:
    if not isinstance(argv, list) or not all(isinstance(part, str) for part in argv):
        return False
    if name == "cargo_workspace_default":
        return argv == ["--workspace"]
    if name == "p7_ab":
        expected = [
            "bash", "scripts/rust/run_p7_ab.sh", "--run-id", shared_dir.name,
            "--run-dir", str(shared_dir / "work/p7-ab"),
            "--out", str(shared_dir / "work/p7-ab.md"),
            "--rust-cli", str(shared_dir / "artifacts" / ARTIFACT_PATHS["debug_cli"]),
            "--rust-daemon", str(shared_dir / "artifacts" / ARTIFACT_PATHS["debug_daemon"]),
            "--od59-contract",
        ]
        return bool(re.fullmatch(r"[A-Za-z0-9._-]+", shared_dir.name)) and argv == expected
    if name == "s24_ab":
        return argv == [
            "-q", "tests/contract_blackbox", "tests/acceptance/l15/test_slice21_l15.py",
            "tests/acceptance/l15/test_slice23_l15.py", "tests/test_multiagent_e2e.py",
            "tests/test_human_agent_coexist.py", "tests/test_rust_contract_coverage.py",
            "tests/test_transfer_golden_fresh.py",
        ]
    if name == "cargo_contract_results":
        return argv == ["--include-cap", "posix_pty", "--include-cap", "process_rpc",
                        "--workspace", "--features", "test-fakes"]
    if name == "windows_l1":
        return argv == ["import", "--windows-l1"]
    return False


def validate_section(shared_dir: Path, *, gate: str, section: str, required_set: str | None, platform: str,
                     expected_artifacts: Mapping[str, Path] | None = None,
                     current_head: str | None = None, worktree_clean: bool | None = None,
                     lock_path: Path = DEFAULT_LOCK, expected_command: str | None = None) -> dict[str, Any]:
    """依目前 HEAD、工作樹、artifact 與 lock selector 驗證一段共用證據。"""
    shared_dir = Path(shared_dir).resolve()
    manifest, reasons = _load_manifest(shared_dir)
    if manifest is None:
        return {"status": "EVIDENCE_MISSING", "reasons": reasons}
    provenance = manifest.get("provenance")
    if not isinstance(provenance, Mapping):
        return {"status": "EVIDENCE_MISSING", "reasons": ["provenance 缺失"]}

    want_head = current_head if current_head is not None else get_current_head()
    if not want_head or provenance.get("git_head") != want_head:
        reasons.append(f"HEAD 不符：shared={provenance.get('git_head')!r} current={want_head!r}")
    clean_now = worktree_clean if worktree_clean is not None else is_worktree_clean()
    if provenance.get("worktree_clean") is not True or clean_now is not True:
        reasons.append("shared/current worktree 必須乾淨")

    artifact_rows = provenance.get("artifacts")
    if not isinstance(artifact_rows, Mapping):
        artifact_rows = {}
    for name in REQUIRED_ARTIFACTS:
        row = artifact_rows.get(name)
        raw_path = row.get("path") if isinstance(row, Mapping) else None
        if raw_path != f"artifacts/{ARTIFACT_PATHS[name]}":
            reasons.append(f"artifact 路徑不符：{name} 須為 artifacts/{ARTIFACT_PATHS[name]}")
        path = _safe_path(shared_dir, raw_path)
        expected_hash = row.get("sha256") if isinstance(row, Mapping) else None
        actual_hash = sha256_file(path) if path is not None else None
        if not isinstance(expected_hash, str) or actual_hash != expected_hash:
            reasons.append(f"artifact SHA256 不符或缺失：{name}")
        local = (expected_artifacts or {}).get(name)
        if local is not None and sha256_file(Path(local)) != expected_hash:
            reasons.append(f"候選 artifact SHA256 不符：{name}")

    sections = manifest.get("sections")
    row = sections.get(section) if isinstance(sections, Mapping) else None
    if not isinstance(row, Mapping):
        reasons.append(f"共用 section 缺失：{section}")
        return {"status": "EVIDENCE_MISSING", "reasons": reasons}

    recorded_argv = row.get("argv")
    if required_set:
        expected_argv, lock_error = _locked_argv(required_set, platform, lock_path)
        if lock_error:
            reasons.append(lock_error)
        else:
            if recorded_argv != expected_argv:
                reasons.append(f"argv 不符：shared={recorded_argv!r} lock={expected_argv!r}")
            selector_key = f"{required_set}/{platform}"
            selectors = row.get("selectors")
            selector_argv = selectors.get(selector_key) if isinstance(selectors, Mapping) else None
            if selector_argv != expected_argv:
                reasons.append(f"selector provenance 不符：{selector_key}")
    elif expected_command is None or not _command_matches(expected_command, recorded_argv, shared_dir):
        reasons.append(f"argv 不符預期命令：{expected_command!r}")

    files = row.get("files")
    if not isinstance(files, Mapping) or not files:
        reasons.append("section 未附任何 evidence file")
    else:
        for role, file_row in files.items():
            file_path = _safe_path(shared_dir, file_row.get("path") if isinstance(file_row, Mapping) else None)
            expected_hash = file_row.get("sha256") if isinstance(file_row, Mapping) else None
            if file_path is None or not isinstance(expected_hash, str) or sha256_file(file_path) != expected_hash:
                reasons.append(f"evidence SHA256 不符或缺失：{role}")

    if reasons:
        return {"status": "EVIDENCE_MISSING", "reasons": reasons}
    exit_code = row.get("exit_code")
    if type(exit_code) is not int:
        return {"status": "EVIDENCE_MISSING", "reasons": ["section exit_code 缺失或格式錯誤"]}
    verdict = "FAIL" if exit_code != 0 else "PASS"
    if section.startswith("windows_l1:"):
        if required_set is None or platform != "windows":
            return {"status": "EVIDENCE_MISSING", "reasons": ["Windows L1 匯入必須指定 Windows lock set"]}
        report_row = files.get("l1-results.json") if isinstance(files, Mapping) else None
        report_path = _safe_path(shared_dir, report_row.get("path") if isinstance(report_row, Mapping) else None)
        try:
            report = json.loads(report_path.read_text(encoding="utf-8")) if report_path is not None else None
        except (OSError, ValueError):
            report = None
        if not isinstance(report, Mapping):
            return {"status": "EVIDENCE_MISSING", "reasons": ["Windows L1 JSON 不可讀"]}
        evaluation = _evaluate_windows_l1(report, required_set=required_set,
                                          expected_head=want_head or "", lock_path=lock_path)
        if evaluation["status"] == "EVIDENCE_MISSING":
            return evaluation
        if evaluation["exit_code"] != exit_code or row.get("status") != evaluation["status"]:
            return {"status": "EVIDENCE_MISSING", "reasons": ["Windows L1 section status/exit_code 與凍結案例判定不一致"]}
        verdict = evaluation["status"]
    return {"status": verdict, "reasons": [], "exit_code": exit_code,
            "gate": gate, "section": section, "required_set": required_set, "platform": platform,
            "argv": list(row["argv"]), "files": dict(files)}


def import_section(shared_dir: Path, *, gate: str, section: str, required_set: str | None, platform: str,
                   expected_artifacts: Mapping[str, Path] | None = None,
                   current_head: str | None = None, worktree_clean: bool | None = None,
                   destination: Path, lock_path: Path = DEFAULT_LOCK,
                   expected_command: str | None = None) -> dict[str, Any]:
    """驗證後複製 evidence；無效時不複製，也不提供重跑指示。"""
    result = validate_section(shared_dir, gate=gate, section=section, required_set=required_set,
                              platform=platform, expected_artifacts=expected_artifacts,
                              current_head=current_head, worktree_clean=worktree_clean,
                              lock_path=lock_path, expected_command=expected_command)
    if result["status"] == "EVIDENCE_MISSING":
        return result
    root = Path(shared_dir).resolve()
    destination = Path(destination).resolve()
    copied: dict[str, str] = {}
    for role, row in result["files"].items():
        source = _safe_path(root, row["path"])
        rel_role = _safe_role(str(role))
        if source is None or rel_role is None:
            return {"status": "EVIDENCE_MISSING", "reasons": [f"evidence 路徑或 role 不安全：{role}"]}
        target = destination / rel_role
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(source, target)
        copied[role] = str(target)
    result["files"] = copied
    return result


def import_artifacts(shared_dir: Path, destination: Path, *, current_head: str | None = None,
                     worktree_clean: bool | None = None) -> dict[str, Any]:
    """驗證候選 provenance 後複製所有 Linux CLI／daemon artifacts。"""
    shared_dir = Path(shared_dir).resolve()
    manifest, reasons = _load_manifest(shared_dir)
    if manifest is None:
        return {"status": "EVIDENCE_MISSING", "reasons": reasons}
    provenance = manifest.get("provenance")
    if not isinstance(provenance, Mapping):
        return {"status": "EVIDENCE_MISSING", "reasons": ["provenance 缺失"]}
    expected_head = current_head if current_head is not None else get_current_head()
    if provenance.get("git_head") != expected_head:
        reasons.append(f"HEAD 不符：shared={provenance.get('git_head')!r} current={expected_head!r}")
    clean_now = worktree_clean if worktree_clean is not None else is_worktree_clean()
    if provenance.get("worktree_clean") is not True or clean_now is not True:
        reasons.append("shared/current worktree 必須乾淨")
    artifact_rows = provenance.get("artifacts")
    if not isinstance(artifact_rows, Mapping):
        artifact_rows = {}
    sources: dict[str, Path] = {}
    for name in REQUIRED_ARTIFACTS:
        row = artifact_rows.get(name)
        raw_path = row.get("path") if isinstance(row, Mapping) else None
        if raw_path != f"artifacts/{ARTIFACT_PATHS[name]}":
            reasons.append(f"artifact 路徑不符：{name} 須為 artifacts/{ARTIFACT_PATHS[name]}")
        source = _safe_path(shared_dir, raw_path)
        expected_sha = row.get("sha256") if isinstance(row, Mapping) else None
        if source is None or not isinstance(expected_sha, str) or sha256_file(source) != expected_sha:
            reasons.append(f"artifact SHA256 不符或缺失：{name}")
        else:
            sources[name] = source
    if reasons:
        return {"status": "EVIDENCE_MISSING", "reasons": reasons}
    destination = Path(destination).resolve()
    destination.mkdir(parents=True, exist_ok=True)
    copied: dict[str, str] = {}
    for name, source in sources.items():
        target = destination / ARTIFACT_PATHS[name]
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(source, target)
        copied[name] = str(target)
    return {"status": "PASS", "artifacts": copied}


def init_manifest(shared_dir: Path, artifact_paths: Mapping[str, Path], *, head: str | None = None,
                  clean: bool | None = None) -> dict[str, Any]:
    """把四個 Linux 候選 artifact 複製到證據目錄並記錄 SHA256 provenance。"""
    shared_dir = Path(shared_dir).resolve()
    if shared_dir.exists() and any(shared_dir.iterdir()):
        raise ValueError(f"輸出目錄非空：{shared_dir}")
    shared_dir.mkdir(parents=True, exist_ok=True)
    artifact_rows: dict[str, dict[str, str]] = {}
    for name in REQUIRED_ARTIFACTS:
        source = Path(artifact_paths[name]).resolve()
        if not source.is_file():
            raise ValueError(f"候選 artifact 缺失：{name}：{source}")
        relative = Path("artifacts") / ARTIFACT_PATHS[name]
        destination = shared_dir / relative
        destination.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(source, destination)
        digest = sha256_file(destination)
        assert digest is not None
        artifact_rows[name] = {"path": relative.as_posix(), "sha256": digest}
    manifest = {"schema": SCHEMA,
                "provenance": {"git_head": head if head is not None else get_current_head(),
                               "worktree_clean": clean if clean is not None else is_worktree_clean(),
                               "artifacts": artifact_rows},
                "sections": {}}
    _write_manifest(shared_dir, manifest)
    return manifest


def record_section(shared_dir: Path, *, name: str, argv: Sequence[str], exit_code: int,
                   files: Mapping[str, Path], required_sets: Sequence[str] = (), platform: str = "linux",
                   lock_path: Path = DEFAULT_LOCK) -> dict[str, Any]:
    """複製 section 產物並記錄實際 argv 與相符的鎖定 selector。"""
    shared_dir = Path(shared_dir).resolve()
    manifest, errors = _load_manifest(shared_dir)
    if manifest is None:
        raise ValueError("; ".join(errors))
    destination = shared_dir / "sections" / name
    destination.mkdir(parents=True, exist_ok=True)
    file_rows: dict[str, dict[str, str]] = {}
    for role, raw_path in files.items():
        rel_role = _safe_role(str(role))
        if rel_role is None:
            raise ValueError(f"evidence role 路徑不安全：{role}")
        source = Path(raw_path).resolve()
        if not source.is_file():
            continue
        target = destination / rel_role
        target.parent.mkdir(parents=True, exist_ok=True)
        if source != target.resolve():
            shutil.copy2(source, target)
        digest = sha256_file(target)
        assert digest is not None
        file_rows[str(role)] = {"path": target.relative_to(shared_dir).as_posix(), "sha256": digest}
    actual_argv = [str(item) for item in argv]
    selectors: dict[str, list[str]] = {}
    for set_id in required_sets:
        expected, error = _locked_argv(set_id, platform, lock_path)
        if error is None and expected == actual_argv:
            selectors[f"{set_id}/{platform}"] = expected
    row = {"argv": actual_argv, "selectors": selectors, "exit_code": int(exit_code), "files": file_rows}
    manifest["sections"][name] = row
    provenance = manifest.get("provenance")
    if isinstance(provenance, dict):
        provenance["worktree_clean"] = (provenance.get("worktree_clean") is True and is_worktree_clean()
                                         and get_current_head() == provenance.get("git_head"))
    _write_manifest(shared_dir, manifest)
    return row


def record_windows_l1(shared_dir: Path, *, source_root: Path, required_set: str,
                      current_head: str | None = None, worktree_clean: bool | None = None,
                      lock_path: Path = DEFAULT_LOCK) -> dict[str, Any]:
    """匯入一份編排者提供的 Windows 原生 L1，並以鎖定 selector 記錄 provenance。"""
    shared_dir = Path(shared_dir).resolve()
    source_root = Path(source_root).resolve()
    manifest, errors = _load_manifest(shared_dir)
    if manifest is None:
        return {"status": "EVIDENCE_MISSING", "reasons": errors}
    provenance = manifest.get("provenance")
    if not isinstance(provenance, Mapping):
        return {"status": "EVIDENCE_MISSING", "reasons": ["共用 provenance 缺失"]}

    reasons: list[str] = []
    want_head = current_head if current_head is not None else get_current_head()
    clean_now = worktree_clean if worktree_clean is not None else is_worktree_clean()
    if not want_head or provenance.get("git_head") != want_head:
        reasons.append(f"共用 HEAD 不符：shared={provenance.get('git_head')!r} current={want_head!r}")
    if provenance.get("worktree_clean") is not True or clean_now is not True:
        reasons.append("共用證據/current worktree 必須乾淨")

    source = _safe_path(source_root, str(Path(required_set) / "l1-results.json"))
    if source is None:
        reasons.append(f"Windows L1 不可讀：{source_root / required_set / 'l1-results.json'}")
        report: dict[str, Any] = {}
    else:
        try:
            report = json.loads(source.read_text(encoding="utf-8"))
        except (OSError, ValueError) as exc:
            reasons.append(f"Windows L1 JSON 不可讀：{exc}")
            report = {}
    if not isinstance(report, Mapping):
        reasons.append("Windows L1 JSON 根節點必須是 object")
        report = {}
    if reasons:
        return {"status": "EVIDENCE_MISSING", "reasons": reasons}
    evaluation = _evaluate_windows_l1(report, required_set=required_set,
                                      expected_head=want_head or "", lock_path=lock_path)
    if evaluation["status"] == "EVIDENCE_MISSING":
        return evaluation
    selector, selector_error = _locked_argv(required_set, "windows", lock_path)
    if selector_error:
        return {"status": "EVIDENCE_MISSING", "reasons": [selector_error]}
    exit_code = evaluation["exit_code"]

    section_name = f"windows_l1:{required_set}"
    sections = manifest.get("sections")
    if not isinstance(sections, dict):
        return {"status": "EVIDENCE_MISSING", "reasons": ["manifest.sections 格式錯誤"]}
    if section_name in sections:
        return {"status": "EVIDENCE_MISSING", "reasons": [f"Windows L1 section 已存在：{section_name}"]}

    section_dir = shared_dir / "sections" / f"windows-l1-{hashlib.sha256(required_set.encode()).hexdigest()[:12]}"
    section_dir.mkdir(parents=True, exist_ok=True)
    destination = section_dir / "l1-results.json"
    shutil.copy2(source, destination)
    digest = sha256_file(destination)
    assert digest is not None
    sections[section_name] = {
        "argv": list(selector or []),
        "selectors": {f"{required_set}/windows": list(selector or [])},
        "exit_code": exit_code,
        "status": evaluation["status"],
        "files": {"l1-results.json": {"path": destination.relative_to(shared_dir).as_posix(),
                                      "sha256": digest}},
    }
    _write_manifest(shared_dir, manifest)
    return {"status": evaluation["status"], "exit_code": exit_code,
            "section": section_name, "required_set": required_set, "platform": "windows",
            "argv": list(selector or []), "file_sha256": digest}


def _write_manifest(shared_dir: Path, manifest: Mapping[str, Any]) -> None:
    path = shared_dir / "manifest.json"
    tmp = path.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(manifest, ensure_ascii=False, sort_keys=True, indent=2) + "\n", encoding="utf-8")
    os.replace(tmp, path)


def _parse_artifacts(values: Sequence[str]) -> dict[str, Path]:
    out: dict[str, Path] = {}
    for value in values:
        if "=" not in value:
            raise ValueError(f"artifact 須為 name=path：{value}")
        name, raw_path = value.split("=", 1)
        if not name or not raw_path:
            raise ValueError(f"artifact 須為 name=path：{value}")
        out[name] = Path(raw_path)
    return out


def _parse_files(values: Sequence[str]) -> dict[str, Path]:
    out: dict[str, Path] = {}
    for value in values:
        if "=" not in value:
            raise ValueError(f"evidence file 須為 role=path：{value}")
        role, raw_path = value.split("=", 1)
        if not role or not raw_path:
            raise ValueError(f"evidence file 須為 role=path：{value}")
        out[role] = Path(raw_path)
    return out


def _cli_import(args: argparse.Namespace) -> int:
    try:
        result = import_section(args.shared_evidence, gate=args.gate, section=args.section,
                                required_set=args.required_set, platform=args.platform,
                                expected_artifacts=_parse_artifacts(args.artifact),
                                destination=args.out_dir, lock_path=args.lock,
                                expected_command=args.expected_command)
    except (OSError, ValueError) as exc:
        result = {"status": "EVIDENCE_MISSING", "reasons": [str(exc)]}
    print(json.dumps(result, ensure_ascii=False, sort_keys=True))
    return 3 if result["status"] == "EVIDENCE_MISSING" else 0


def _cli_init(args: argparse.Namespace) -> int:
    manifest = init_manifest(args.shared_evidence, _parse_artifacts(args.artifact))
    print(json.dumps({"schema": manifest["schema"], "provenance": manifest["provenance"]},
                     ensure_ascii=False, sort_keys=True))
    return 0


def _cli_import_artifacts(args: argparse.Namespace) -> int:
    result = import_artifacts(args.shared_evidence, args.out_dir)
    print(json.dumps(result, ensure_ascii=False, sort_keys=True))
    return 3 if result["status"] == "EVIDENCE_MISSING" else 0


def _cli_record(args: argparse.Namespace) -> int:
    try:
        command_argv = json.loads(args.argv_json)
    except ValueError as exc:
        raise ValueError(f"argv-json 非有效 JSON：{exc}") from exc
    if not isinstance(command_argv, list) or not all(isinstance(item, str) for item in command_argv):
        raise ValueError("argv-json 須為字串陣列")
    row = record_section(args.shared_evidence, name=args.section, argv=command_argv,
                         exit_code=args.exit_code, files=_parse_files(args.file),
                         required_sets=args.required_set, platform=args.platform, lock_path=args.lock)
    print(json.dumps(row, ensure_ascii=False, sort_keys=True))
    return 0


def _cli_record_windows(args: argparse.Namespace) -> int:
    result = record_windows_l1(args.shared_evidence, source_root=args.windows_l1,
                               required_set=args.required_set, lock_path=args.lock)
    print(json.dumps(result, ensure_ascii=False, sort_keys=True))
    return 3 if result["status"] == "EVIDENCE_MISSING" else 0


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest="command", required=True)
    imp = sub.add_parser("import-section", help="驗證 provenance 後匯入一段證據")
    imp.add_argument("--shared-evidence", type=Path, required=True)
    imp.add_argument("--gate", required=True, choices=("slice21", "s22", "p7", "slice24"))
    imp.add_argument("--section", required=True)
    imp.add_argument("--required-set")
    imp.add_argument("--expected-command", choices=("cargo_workspace_default", "p7_ab", "s24_ab",
                                                      "cargo_contract_results", "windows_l1"))
    imp.add_argument("--platform", required=True, choices=("linux", "windows"))
    imp.add_argument("--artifact", action="append", default=[], help="要與 provenance 比對的 name=path")
    imp.add_argument("--out-dir", type=Path, required=True)
    imp.add_argument("--lock", type=Path, default=DEFAULT_LOCK)
    imp.set_defaults(func=_cli_import)
    init = sub.add_parser("init", help="記錄候選 artifacts 與 git provenance")
    init.add_argument("--shared-evidence", type=Path, required=True)
    init.add_argument("--artifact", action="append", default=[], help="候選 artifact name=path")
    init.set_defaults(func=_cli_init)
    artifact_import = sub.add_parser("import-artifacts", help="驗證後匯入 release/debug CLI 與 daemon")
    artifact_import.add_argument("--shared-evidence", type=Path, required=True)
    artifact_import.add_argument("--out-dir", type=Path, required=True)
    artifact_import.set_defaults(func=_cli_import_artifacts)
    rec = sub.add_parser("record-section", help="記錄一段命令的 argv、結果與 SHA256 evidence")
    rec.add_argument("--shared-evidence", type=Path, required=True)
    rec.add_argument("--section", required=True)
    rec.add_argument("--argv-json", required=True)
    rec.add_argument("--exit-code", required=True, type=int)
    rec.add_argument("--file", action="append", default=[], help="evidence file role=path；role 可包含子目錄")
    rec.add_argument("--required-set", action="append", default=[])
    rec.add_argument("--platform", default="linux", choices=("linux", "windows"))
    rec.add_argument("--lock", type=Path, default=DEFAULT_LOCK)
    rec.set_defaults(func=_cli_record)
    rec_win = sub.add_parser("record-windows-l1", help="匯入並驗證編排者提供的 Windows 原生 L1")
    rec_win.add_argument("--shared-evidence", type=Path, required=True)
    rec_win.add_argument("--windows-l1", type=Path, required=True,
                         help="內含 <required-set>/l1-results.json 的目錄")
    rec_win.add_argument("--required-set", required=True)
    rec_win.add_argument("--lock", type=Path, default=DEFAULT_LOCK)
    rec_win.set_defaults(func=_cli_record_windows)
    args = parser.parse_args(argv)
    try:
        return int(args.func(args))
    except (OSError, ValueError) as exc:
        print(json.dumps({"status": "EVIDENCE_MISSING", "reasons": [str(exc)]}, ensure_ascii=False), file=sys.stderr)
        return 3


if __name__ == "__main__":
    raise SystemExit(main())
