"""Stage 3 發佈前的 Python daemon 退役守門與 bundle 封裝。"""

from __future__ import annotations

import argparse
import hashlib
import io
import json
import os
import re
import shutil
import stat
import sys
import tarfile
import tempfile
import zipfile
from pathlib import Path, PurePosixPath, PureWindowsPath
from typing import Any, Mapping

from tools.stage3.jsonio import atomic_write_text, sha256_file
from tools.stage3.reason_codes import HarnessError

REPO_ROOT = Path(__file__).resolve().parents[2]
RETIREMENT_PATH = REPO_ROOT / "packaging" / "python_daemon_retirement.json"
MANIFEST_MARKER: Mapping[str, str] = {"python_daemon": "removed"}
KEEP_PYTHON_TEST_TOOLS: tuple[str, ...] = (
    "realhw",
    "regression",
    "reliability",
    "func-test",
    "tools/uart_stdio_bridge",
    "tests/contract_blackbox",
    "tests/acceptance",
    "tools/stage3",
    "sw_core",
)
ARCHIVE_SUFFIX: Mapping[str, str] = {"linux": ".tar.gz", "windows": ".zip"}
_PLATFORMS = ("linux", "windows")
_OLD_STAGES = ("1A", "1B", "2.1", "2.2", "2.3", "2.4")
_SHA256 = re.compile(r"^[0-9a-f]{64}$")


def load_retirement(path: Path = RETIREMENT_PATH) -> dict:
    """讀取退役裁決檔；不在呼叫端隱藏損毀或缺檔。"""
    doc = json.loads(Path(path).read_text(encoding="utf-8"))
    if not isinstance(doc, dict):
        raise ValueError("retirement document must be a JSON object")
    return doc


def _retirement_path(repo_root: Path) -> Path:
    return Path(repo_root) / "packaging" / "python_daemon_retirement.json"


def _index_path(retirement: Mapping[str, Any], repo_root: Path) -> Path | None:
    rel = retirement.get("evidence_index")
    if not isinstance(rel, str) or not rel.strip():
        return None
    candidate = Path(rel)
    if candidate.is_absolute() or ".." in candidate.parts:
        return None
    root = Path(repo_root).resolve()
    path = (root / candidate).resolve()
    try:
        path.relative_to(root)
    except ValueError:
        return None
    return path


def _retired_false_violations(retirement: Mapping[str, Any]) -> list[str]:
    violations: list[str] = []
    for key in ("evidence_index_sha256", "approved_by", "approved_at", "decision_ref", "effective_bundle_version"):
        if retirement.get(key) is not None:
            violations.append(f"retirement: {key} must be null while retired is false")
    targets = retirement.get("stage3_target_artifact_ids")
    if not isinstance(targets, dict) or any(targets.get(p) is not None for p in _PLATFORMS):
        violations.append("retirement: stage3 target ids must be null while retired is false")
    return violations


def _retired_flag(retirement: Mapping[str, Any]) -> bool:
    """讀取嚴格 JSON boolean，拒絕整數、字串及其他 truthy 值。"""
    value = retirement.get("retired")
    if type(value) is not bool:
        raise HarnessError("HARNESS_RETIREMENT_INCONSISTENT", "retired must be a JSON boolean")
    return value


def _load_cases(repo_root: Path) -> dict[str, dict]:
    """載入 XFAIL 核准資料；缺檔時傳空對照，讓 evidence gate 保守判定。"""
    path = Path(repo_root) / "tests" / "acceptance" / "cases.yaml"
    if not path.is_file():
        return {}
    try:
        import yaml

        doc = yaml.safe_load(path.read_text(encoding="utf-8"))
    except Exception as exc:
        raise ValueError(f"cannot read acceptance cases: {exc}") from exc
    if not isinstance(doc, dict) or not isinstance(doc.get("cases"), list):
        raise ValueError("acceptance cases must contain a cases list")
    cases: dict[str, dict] = {}
    for item in doc["cases"]:
        if isinstance(item, dict) and isinstance(item.get("case_id"), str):
            cases[item["case_id"]] = item
    return cases


def _stage3_result(retirement: Mapping[str, Any], *, repo_root: Path, evidence_root: Path,
                   index_path: Path) -> tuple[str, str | None]:
    """以 Task 14 的唯讀 stage_status 判斷；缺少介面一律阻擋。"""
    try:
        from tools.stage3 import evidence_index

        index = evidence_index.load_index(index_path)
        catalog = evidence_index.load_exit_catalog()
        cases = _load_cases(repo_root)
        l1_lock_path = Path(repo_root) / "tests" / "acceptance" / "l1_required.lock.json"
        try:
            l1_lock = json.loads(l1_lock_path.read_text(encoding="utf-8"))
        except (OSError, UnicodeError, json.JSONDecodeError):
            l1_lock = None
        result = evidence_index.stage_status(
            index,
            "3",
            catalog=catalog,
            evidence_root=Path(evidence_root),
            cases=cases,
            l1_lock=l1_lock,
        )
        value = result.get("result") if isinstance(result, dict) else None
        return (value, None) if value in {"PASS", "FAIL", "BLOCKED"} else ("BLOCKED", None)
    except Exception as exc:
        return "BLOCKED", f"{type(exc).__name__}: {exc}"


def _manifest_doc(path: Path) -> dict | None:
    try:
        doc = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError):
        return None
    return doc if isinstance(doc, dict) else None


def _bundle_check(bundle_dir: Path, manifest_doc: Mapping[str, Any], *, label: str,
                  prefix: str) -> list[str]:
    """重算 component 與完整檔案樹；兩項都執行並保留全部違規。"""
    violations: list[str] = []
    try:
        from tools.stage3.ab_manifest import bundle_tree_sha256, verify_bundle
    except (ImportError, AttributeError):
        return [f"retirement: {prefix} verification unavailable: {label}"]

    try:
        for item in verify_bundle(bundle_dir, manifest_doc, label=label):
            violations.append(f"retirement: {prefix} content mismatch: {label}: {item}")
    except Exception as exc:
        violations.append(f"retirement: {prefix} content mismatch: {label}: {exc}")

    try:
        bundle_tree_sha256(bundle_dir)
    except HarnessError as exc:
        if exc.code == "HARNESS_ARTIFACT_MISMATCH":
            violations.append(f"retirement: {prefix} non-regular file: {label}")
        else:
            violations.append(f"retirement: {prefix} tree unavailable: {label}")
    except Exception:
        violations.append(f"retirement: {prefix} tree unavailable: {label}")
    return violations


def _bundle_tree(bundle_dir: Path) -> str:
    from tools.stage3.ab_manifest import bundle_tree_sha256

    return bundle_tree_sha256(bundle_dir)


def _target(index: Mapping[str, Any], platform: str) -> Mapping[str, Any] | None:
    stages = index.get("stage_targets")
    if not isinstance(stages, dict):
        return None
    stage = stages.get("3")
    if not isinstance(stage, dict):
        return None
    target = stage.get(platform)
    return target if isinstance(target, dict) else None


def _load_verified_index(
    retirement: Mapping[str, Any], repo_root: Path
) -> tuple[Path | None, dict[str, Any], list[str]]:
    """載入 index，並共同檢查路徑與核准時記錄的位元組雜湊。"""
    index_path = _index_path(retirement, repo_root)
    if index_path is None or not index_path.is_file():
        return index_path, {}, ["retirement: evidence index missing or outside repo"]
    violations: list[str] = []
    try:
        actual_sha = sha256_file(index_path)
    except OSError:
        actual_sha = None
    if not isinstance(retirement.get("evidence_index_sha256"), str) or actual_sha != retirement.get("evidence_index_sha256"):
        violations.append("retirement: evidence index sha256 mismatch")
    try:
        from tools.stage3 import evidence_index

        index = evidence_index.load_index(index_path)
    except ImportError:
        try:
            raw = json.loads(index_path.read_text(encoding="utf-8"))
            index = raw if isinstance(raw, dict) else {}
        except (OSError, UnicodeError, json.JSONDecodeError) as exc:
            index = {}
            violations.append(f"retirement: evidence index unreadable: {exc}")
    except Exception as exc:
        index = {}
        violations.append(f"retirement: evidence index unreadable: {type(exc).__name__}: {exc}")
    return index_path, index, violations


def _target_identity_violations(
    retirement: Mapping[str, Any], index: Mapping[str, Any]
) -> list[str]:
    target_ids = retirement.get("stage3_target_artifact_ids")
    if not isinstance(target_ids, dict) or set(target_ids) != set(_PLATFORMS):
        return ["retirement: stage3 target ids must include linux and windows"]
    violations: list[str] = []
    for platform in _PLATFORMS:
        target = _target(index, platform)
        actual = target.get("artifact_id") if target else None
        supplied = target_ids.get(platform)
        if not isinstance(supplied, str) or not _SHA256.fullmatch(supplied) or supplied != actual:
            violations.append(f"retirement: stage3 target artifact mismatch: {platform}")
    return violations


def _python_runtime_paths(bundle_dir: Path) -> list[str]:
    """列出退役 bundle 中殘留的 Python runtime 或 daemon package 路徑。"""
    root = Path(bundle_dir)
    found: set[str] = set()
    for current, dirs, names in os.walk(root, topdown=True, followlinks=False):
        current_path = Path(current)
        for name in dirs:
            path = current_path / name
            rel = path.relative_to(root).as_posix()
            parts = PurePosixPath(rel).parts
            if rel == "py" or "sw_core" in parts:
                found.add(rel)
        for name in names:
            path = current_path / name
            rel = path.relative_to(root).as_posix()
            parts = PurePosixPath(rel).parts
            if "sw_core" in parts or name == "serialwrapd.py":
                found.add(rel)
    return sorted(found, key=lambda rel: rel.encode("utf-8"))


def _python_runtime_violations(bundle_dir: Path, *, label: str, prefix: str = "bundle") -> list[str]:
    return [
        f"retirement: {prefix} contains python runtime: {label}: {rel}"
        for rel in _python_runtime_paths(bundle_dir)
    ]


def verify(
    retirement: dict,
    *,
    repo_root: Path,
    mode: str,
    evidence_root: Path | None = None,
    bundle_manifests: Mapping[str, Path] | None = None,
) -> list[str]:
    """核對裁決、Stage 3 證據與實際發佈 bundle 位元組。"""
    if mode not in {"repo", "release"}:
        return [f"retirement: invalid verify mode: {mode}"]
    if not isinstance(retirement, dict):
        return ["retirement: retirement document must be an object"]
    try:
        retired = _retired_flag(retirement)
    except HarnessError as exc:
        return [f"retirement: {exc.code}: {exc.detail}"]
    if not retired:
        return _retired_false_violations(retirement)

    violations: list[str] = []
    index_path, index, index_violations = _load_verified_index(retirement, repo_root)
    violations.extend(index_violations)

    for key in ("approved_by", "approved_at", "decision_ref", "effective_bundle_version"):
        value = retirement.get(key)
        if not isinstance(value, str) or not value.strip():
            violations.append(f"retirement: {key} required")

    violations.extend(_target_identity_violations(retirement, index))
    target_ids = retirement.get("stage3_target_artifact_ids")
    if not isinstance(target_ids, dict):
        target_ids = {}

    root = Path(repo_root)
    for rel in KEEP_PYTHON_TEST_TOOLS:
        if not (root / rel).exists():
            violations.append(f"retirement: keep path missing: {rel}")

    if mode == "repo":
        return violations

    if evidence_root is None:
        violations.append("retirement: evidence root required")
    elif index_path is not None and index:
        result, evaluation_error = _stage3_result(
            retirement, repo_root=root, evidence_root=Path(evidence_root), index_path=index_path
        )
        if evaluation_error is not None:
            violations.append(f"retirement: stage 3 evaluation failed: {evaluation_error}")
        if result != "PASS":
            violations.append(f"retirement: stage 3 not PASS: {result}")
    else:
        violations.append("retirement: stage 3 not PASS: BLOCKED")

    if bundle_manifests is None or set(bundle_manifests) != set(_PLATFORMS):
        violations.append("retirement: bundle manifests must include linux and windows")
        return violations

    try:
        from tools.stage3.ab_manifest import bundle_tree_sha256, verify_bundle
    except (ImportError, AttributeError):
        violations.append("retirement: bundle verification unavailable")
        return violations

    for platform in _PLATFORMS:
        manifest_path = Path(bundle_manifests[platform])
        if manifest_path.name != "release-manifest.json":
            violations.append(f"retirement: bundle manifest not at bundle root: {platform}")
        try:
            manifest_sha = sha256_file(manifest_path)
        except OSError:
            manifest_sha = None
        if manifest_sha != target_ids.get(platform):
            violations.append(f"retirement: bundle manifest mismatch: {platform}")

        doc = _manifest_doc(manifest_path)
        if doc is None:
            violations.append(f"retirement: bundle manifest unreadable: {platform}")
            continue
        if doc.get("retirement") != dict(MANIFEST_MARKER):
            violations.append(f"retirement: bundle retirement marker mismatch: {platform}")
        if doc.get("bundle_version") != retirement.get("effective_bundle_version"):
            violations.append(f"retirement: bundle version mismatch: {platform}")

        bundle_dir = manifest_path.parent
        violations.extend(_python_runtime_violations(bundle_dir, label=platform))
        try:
            component_violations = verify_bundle(bundle_dir, doc, label=platform)
        except Exception as exc:
            component_violations = [str(exc)]
        for item in component_violations:
            violations.append(f"retirement: bundle content mismatch: {platform}: {item}")

        target = _target(index, platform)
        expected_tree = target.get("bundle_tree_sha256") if target else None
        if not isinstance(expected_tree, str):
            violations.append(f"retirement: target missing bundle tree: {platform}")
        try:
            actual_tree = bundle_tree_sha256(bundle_dir)
        except HarnessError as exc:
            if exc.code == "HARNESS_ARTIFACT_MISMATCH":
                violations.append(f"retirement: bundle non-regular file: {platform}")
            else:
                violations.append(f"retirement: bundle tree unavailable: {platform}")
        except Exception:
            violations.append(f"retirement: bundle tree unavailable: {platform}")
        else:
            if isinstance(expected_tree, str) and actual_tree != expected_tree:
                violations.append(f"retirement: bundle tree mismatch: {platform}")

    return violations


def bundle_plan(stage: str, retirement: dict, *, dev: bool) -> dict:
    """依 Stage 與擁有者裁決產生唯讀打包計畫。"""
    if stage not in (*_OLD_STAGES, "3"):
        raise HarnessError("HARNESS_MANIFEST_INVALID", f"unsupported release stage: {stage}")
    retired = _retired_flag(retirement)
    if stage == "3":
        plan = {
            "stage": "3",
            "retirement_applies": True,
            "publishable": retired and not dev,
            "candidate": not retired,
            "include_python_runtime": False,
            "include_python_cli_delegate": False,
            "daemon_implementation": "rust",
            "manifest_retirement": dict(MANIFEST_MARKER),
        }
        if retired and dev:
            plan["candidate"] = False
        return plan
    if retired and not dev:
        raise HarnessError("HARNESS_RETIREMENT_INCONSISTENT", "retired releases require stage 3")
    return {
        "stage": stage,
        "retirement_applies": False,
        "publishable": not dev,
        "candidate": False,
        "manifest_retirement": None,
    }


def github_output(retirement: dict) -> str:
    """輸出供 GitHub Actions 使用的 retirement boolean。"""
    retired = _retired_flag(retirement)
    return f"retired={'true' if retired else 'false'}\n"


def _bundle_files(bundle_dir: Path) -> list[tuple[str, Path]]:
    root = Path(bundle_dir)
    if not root.is_dir() or root.is_symlink():
        raise HarnessError("HARNESS_ARTIFACT_MISMATCH", f"bundle root is not a directory: {root}")
    files: list[tuple[str, Path]] = []
    for current, dirs, names in os.walk(root, topdown=True, followlinks=False):
        current_path = Path(current)
        kept_dirs: list[str] = []
        for name in sorted(dirs):
            path = current_path / name
            try:
                mode = path.lstat().st_mode
            except OSError as exc:
                raise HarnessError("HARNESS_ARTIFACT_MISMATCH", f"cannot stat {path}: {exc}") from exc
            if not stat.S_ISDIR(mode):
                rel = path.relative_to(root).as_posix()
                raise HarnessError("HARNESS_ARTIFACT_MISMATCH", f"non-regular file: {rel}")
            kept_dirs.append(name)
        dirs[:] = kept_dirs
        for name in sorted(names):
            path = current_path / name
            try:
                mode = path.lstat().st_mode
            except OSError as exc:
                raise HarnessError("HARNESS_ARTIFACT_MISMATCH", f"cannot stat {path}: {exc}") from exc
            if not stat.S_ISREG(mode):
                rel = path.relative_to(root).as_posix()
                raise HarnessError("HARNESS_ARTIFACT_MISMATCH", f"non-regular file: {rel}")
            files.append((path.relative_to(root).as_posix(), path))
    return sorted(files, key=lambda pair: pair[0].encode("utf-8"))


def pack_bundle(bundle_dir: Path, out: Path) -> None:
    """以 POSIX 相對路徑封裝 bundle 中的所有一般檔。"""
    root = Path(bundle_dir).resolve()
    output = Path(out)
    if output.resolve().is_relative_to(root):
        raise HarnessError("HARNESS_ARTIFACT_MISMATCH", "archive output must be outside bundle")
    files = _bundle_files(Path(bundle_dir))
    output.parent.mkdir(parents=True, exist_ok=True)
    if output.name.endswith(".tar.gz"):
        with tarfile.open(output, mode="w:gz", format=tarfile.PAX_FORMAT) as archive:
            for rel, path in files:
                info = archive.gettarinfo(str(path), arcname=rel)
                info.type = tarfile.REGTYPE
                info.size = path.stat().st_size
                info.linkname = ""
                info.uid = info.gid = 0
                info.uname = info.gname = ""
                info.mtime = 0
                with path.open("rb") as source:
                    archive.addfile(info, source)
        return
    if output.suffix == ".zip":
        with zipfile.ZipFile(output, mode="w", compression=zipfile.ZIP_DEFLATED) as archive:
            for rel, path in files:
                info = zipfile.ZipInfo(rel, date_time=(1980, 1, 1, 0, 0, 0))
                info.compress_type = zipfile.ZIP_DEFLATED
                info.external_attr = (stat.S_IFREG | 0o644) << 16
                with path.open("rb") as source:
                    archive.writestr(info, source.read())
        return
    raise HarnessError("HARNESS_ARTIFACT_MISMATCH", "archive output must end in .tar.gz or .zip")


def _safe_member(name: str) -> tuple[bool, str]:
    if not name or "\\" in name or "\x00" in name or PurePosixPath(name).is_absolute() or PureWindowsPath(name).is_absolute():
        return False, name
    parts = name.rstrip("/").split("/")
    if not parts or any(part in {"", ".", ".."} for part in parts):
        return False, name
    if re.match(r"^[A-Za-z]:", parts[0]):
        return False, name
    return True, "/".join(parts)


def _unpack_archive(data: bytes, destination: Path, *, kind: str) -> str | None:
    """先完整核對成員，再手動解開一般檔，拒絕連結與特殊檔。"""
    seen: set[str] = set()
    try:
        if kind == "zip":
            with zipfile.ZipFile(io.BytesIO(data), "r") as archive:
                infos = archive.infolist()
                checked: list[tuple[zipfile.ZipInfo, str, bool]] = []
                for info in infos:
                    ok, rel = _safe_member(info.filename)
                    if not ok or rel in seen:
                        return f"retirement: archive unsafe member: {info.filename}"
                    seen.add(rel)
                    is_dir = info.is_dir()
                    mode = (info.external_attr >> 16) & 0xFFFF
                    file_type = stat.S_IFMT(mode)
                    if (is_dir and file_type not in {0, stat.S_IFDIR}) or (not is_dir and file_type not in {0, stat.S_IFREG}):
                        return f"retirement: archive unsafe member: {info.filename}"
                    checked.append((info, rel, is_dir))
                for info, rel, is_dir in checked:
                    target = destination.joinpath(*rel.split("/"))
                    if is_dir:
                        target.mkdir(parents=True, exist_ok=True)
                    else:
                        target.parent.mkdir(parents=True, exist_ok=True)
                        with archive.open(info, "r") as source, target.open("wb") as sink:
                            shutil.copyfileobj(source, sink)
            return None

        with tarfile.open(fileobj=io.BytesIO(data), mode="r:gz") as archive:
            members = archive.getmembers()
            checked_tar: list[tuple[tarfile.TarInfo, str, bool]] = []
            for member in members:
                ok, rel = _safe_member(member.name)
                if not ok or rel in seen or not (member.isfile() or member.isdir()):
                    return f"retirement: archive unsafe member: {member.name}"
                seen.add(rel)
                checked_tar.append((member, rel, member.isdir()))
            for member, rel, is_dir in checked_tar:
                target = destination.joinpath(*rel.split("/"))
                if is_dir:
                    target.mkdir(parents=True, exist_ok=True)
                    continue
                target.parent.mkdir(parents=True, exist_ok=True)
                source = archive.extractfile(member)
                if source is None:
                    return f"retirement: archive unsafe member: {member.name}"
                with source, target.open("wb") as sink:
                    shutil.copyfileobj(source, sink)
        return None
    except (OSError, tarfile.TarError, zipfile.BadZipFile, EOFError, ValueError,
            NotImplementedError, RuntimeError) as exc:
        return f"retirement: archive unreadable: {exc}"


def verify_archive(retirement: dict, *, repo_root: Path, platform: str, archive: Path) -> tuple[list[str], str]:
    """從同一份 archive bytes 驗證 manifest、components、bundle tree 並回傳其 SHA-256。"""
    path = Path(archive)
    try:
        data = path.read_bytes()
    except OSError as exc:
        return [f"retirement: archive unreadable: {exc}"], ""
    archive_sha = hashlib.sha256(data).hexdigest()
    if platform not in ARCHIVE_SUFFIX:
        return [f"retirement: archive format mismatch: {platform}"], archive_sha
    if not path.name.endswith(ARCHIVE_SUFFIX[platform]):
        return [f"retirement: archive format mismatch: {platform}"], archive_sha
    try:
        retired = _retired_flag(retirement)
    except HarnessError as exc:
        return [f"retirement: {exc.code}: {exc.detail}"], archive_sha
    if not retired:
        return ["retirement: HARNESS_RETIREMENT_NOT_APPROVED"], archive_sha

    index_path, index, index_violations = _load_verified_index(retirement, repo_root)
    if index_path is None:
        return index_violations or ["retirement: evidence index unreadable"], archive_sha
    violations: list[str] = list(index_violations)
    violations.extend(_target_identity_violations(retirement, index))
    target = _target(index, platform)
    if target is None:
        violations.append(f"retirement: archive manifest mismatch: {platform}")
    target_ids = retirement.get("stage3_target_artifact_ids")
    expected_artifact = target_ids.get(platform) if isinstance(target_ids, dict) else None
    expected_tree = target.get("bundle_tree_sha256") if target else None

    with tempfile.TemporaryDirectory(prefix="serialwrap-retirement-") as temp:
        extracted = Path(temp)
        kind = "zip" if path.name.endswith(".zip") else "tar"
        unsafe = _unpack_archive(data, extracted, kind=kind)
        if unsafe is not None:
            return [unsafe], archive_sha
        manifest_path = extracted / "release-manifest.json"
        try:
            manifest_sha = sha256_file(manifest_path)
        except OSError:
            manifest_sha = None
        if manifest_sha != expected_artifact:
            violations.append(f"retirement: archive manifest mismatch: {platform}")
        doc = _manifest_doc(manifest_path)
        if doc is None:
            violations.append(f"retirement: archive manifest unreadable: {platform}")
            return violations, archive_sha
        if doc.get("retirement") != dict(MANIFEST_MARKER):
            violations.append(f"retirement: archive retirement marker mismatch: {platform}")
        if doc.get("bundle_version") != retirement.get("effective_bundle_version"):
            violations.append(f"retirement: archive bundle version mismatch: {platform}")
        violations.extend(_python_runtime_violations(extracted, label=platform, prefix="archive"))
        violations.extend(_bundle_check(extracted, doc, label=platform, prefix="archive"))
        try:
            actual_tree = _bundle_tree(extracted)
        except HarnessError as exc:
            if exc.code == "HARNESS_ARTIFACT_MISMATCH":
                violations.append(f"retirement: archive non-regular file: {platform}")
            else:
                violations.append(f"retirement: archive tree unavailable: {platform}")
        except Exception:
            violations.append(f"retirement: archive tree unavailable: {platform}")
        else:
            if not isinstance(expected_tree, str) or actual_tree != expected_tree:
                violations.append(f"retirement: archive tree mismatch: {platform}")
    return violations, archive_sha


def _parse_bundle_manifests(values: list[str]) -> dict[str, Path] | None:
    parsed: dict[str, Path] = {}
    for value in values:
        platform, sep, path = value.partition("=")
        if not sep or platform not in _PLATFORMS or not path or platform in parsed:
            return None
        parsed[platform] = Path(path)
    return parsed


def _emit_violations(violations: list[str]) -> None:
    if violations:
        print(json.dumps(
            {"ok": False, "violations": violations},
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        ))


def _emit_error(error: HarnessError) -> None:
    detail = f"{error.code}: 退役檢查未通過；{error.detail or '未提供錯誤細節'}"
    print(json.dumps(
        {"ok": False, "reason_code": error.code, "detail": detail},
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ))


def main(argv: list[str] | None = None) -> int:
    """`python -m tools.stage3 retirement ...` 的本 Task CLI 入口。"""
    parser = argparse.ArgumentParser(prog="python -m tools.stage3")
    parent = parser.add_subparsers(dest="command", required=True)
    retirement_parser = parent.add_parser("retirement")
    actions = retirement_parser.add_subparsers(dest="action", required=True)

    plan_parser = actions.add_parser("bundle-plan")
    plan_parser.add_argument("--stage", required=True)
    plan_parser.add_argument("--dev", action="store_true")
    plan_parser.add_argument("--repo-root", type=Path, default=REPO_ROOT)

    output_parser = actions.add_parser("github-output")
    output_parser.add_argument("--repo-root", type=Path, default=REPO_ROOT)

    verify_parser = actions.add_parser("verify")
    verify_parser.add_argument("--mode", choices=("repo", "release"), required=True)
    verify_parser.add_argument("--repo-root", type=Path, default=REPO_ROOT)
    verify_parser.add_argument("--evidence-root", type=Path)
    verify_parser.add_argument("--bundle-manifest", action="append", default=[])

    archive_parser = actions.add_parser("verify-archive")
    archive_parser.add_argument("--repo-root", type=Path, default=REPO_ROOT)
    archive_parser.add_argument("--platform", required=True)
    archive_parser.add_argument("--archive", type=Path, required=True)
    archive_parser.add_argument("--sha256sums", type=Path, required=True)

    pack_parser = actions.add_parser("pack")
    pack_parser.add_argument("--bundle-dir", type=Path, required=True)
    pack_parser.add_argument("--out", type=Path, required=True)

    args = parser.parse_args(argv)
    if args.action == "pack":
        try:
            pack_bundle(args.bundle_dir, args.out)
        except HarnessError as exc:
            print(exc)
            return 1
        return 0

    try:
        retirement = load_retirement(_retirement_path(args.repo_root))
    except (OSError, UnicodeError, json.JSONDecodeError, ValueError) as exc:
        _emit_error(HarnessError("HARNESS_RETIREMENT_INCONSISTENT", f"無法讀取退役裁決檔：{exc}"))
        return 2
    if args.action == "bundle-plan":
        try:
            plan = bundle_plan(args.stage, retirement, dev=args.dev)
        except HarnessError as exc:
            _emit_error(exc)
            return 2
        print(json.dumps(plan, ensure_ascii=False, sort_keys=True, separators=(",", ":")))
        return 0
    if args.action == "github-output":
        try:
            output = github_output(retirement)
        except HarnessError as exc:
            print(str(exc), file=sys.stderr)
            return 2
        print(output, end="")
        return 0
    if args.action == "verify":
        if args.mode == "release":
            try:
                retired = _retired_flag(retirement)
            except HarnessError as exc:
                _emit_error(exc)
                return 2
            if not retired:
                _emit_error(HarnessError("HARNESS_RETIREMENT_NOT_APPROVED", "退役尚未核准，拒絕 release 發佈"))
                return 2
        manifests = _parse_bundle_manifests(args.bundle_manifest)
        violations = verify(
            retirement,
            repo_root=args.repo_root,
            mode=args.mode,
            evidence_root=args.evidence_root,
            bundle_manifests=manifests,
        )
        _emit_violations(violations)
        return 1 if violations else 0
    if args.action == "verify-archive":
        try:
            retired = _retired_flag(retirement)
        except HarnessError as exc:
            _emit_error(exc)
            return 2
        if not retired:
            _emit_error(HarnessError("HARNESS_RETIREMENT_NOT_APPROVED", "退役尚未核准，拒絕 archive 發佈"))
            return 2
        violations, archive_sha = verify_archive(
            retirement, repo_root=args.repo_root, platform=args.platform, archive=args.archive
        )
        _emit_violations(violations)
        if violations:
            return 1
        atomic_write_text(args.sha256sums, f"{archive_sha}  {args.archive.name}\n")
        return 0
    parser.error("unsupported retirement action")
    return 2


if __name__ == "__main__":
    raise SystemExit(main())
