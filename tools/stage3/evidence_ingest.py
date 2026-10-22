"""Stage 3 出口證據 ingest：依產生工具報告推導結果、核對受測身分並保存逐檔摘要。"""

from __future__ import annotations

import importlib.util
import importlib
import json
import os
import re
import shutil
import stat
import sys
import tempfile
import xml.etree.ElementTree as ET
from collections import Counter
from dataclasses import asdict, replace
from datetime import date, datetime, time, timezone
from pathlib import Path
from typing import Any, Mapping, NamedTuple, Sequence

from tools.stage3 import jsonio
from tools.stage3.derived import Derived
from tools.stage3.reason_codes import HarnessError


KINDS: tuple[str, ...] = (
    "junit",
    "functest-report",
    "bridge-selftest",
    "regression-report",
    "realhw-report",
    "gate-report",
    "ab-report",
    "canary-report",
    "drill-report",
    "od09-freeze",
    "manual-procedure",
)
TREE_EXEMPT_KINDS: frozenset[str] = frozenset({"od09-freeze", "bridge-selftest"})
TREE_REQUIRED_KINDS: frozenset[str] = frozenset(KINDS) - TREE_EXEMPT_KINDS

_ALLOWED_EVIDENCE_LAYERS: frozenset[str] = frozenset(
    {"L2-bench", "L3-bench", "hw-bench", "L1.5-docker", "host-throwaway", "L1.5-local", "native-windows"}
)
_NON_BOARD_EVIDENCE_LAYERS: frozenset[str] = frozenset({"L1.5-docker", "host-throwaway"})
_ALLOWED_NON_BOARD_ITEM_LAYERS: frozenset[str] = frozenset({"l1.5", "spike", "supervisor", "perf", "manual"})
_SHA256_RE = re.compile(r"^[0-9a-f]{64}$")
_REPO_ROOT = Path(__file__).resolve().parents[2]
_L1_JUDGE_PATH = _REPO_ROOT / "tests" / "acceptance" / "l1_judge.py"
_L1_LOCK_PATH = _REPO_ROOT / "tests" / "acceptance" / "l1_required.lock.json"
_CASES_YAML_PATH = _REPO_ROOT / "tests" / "acceptance" / "cases.yaml"
_BRIDGE_SRC = _REPO_ROOT / "tools" / "uart_stdio_bridge" / "src"
_PROCEDURES_ROOT = _REPO_ROOT / "docs" / "rust-refactor" / "procedures"
_ACCEPTANCE_PATH = _REPO_ROOT / "realhw" / "acceptance" / "longrun_acceptance.yaml"
# 與 P2 `realhw.acceptance.od09_freeze.LEDGER_PATH` 同一路徑；以模組常數傳入，使測試可隔離候選開跑 ledger（P9-T15）。
_LEDGER_PATH = _REPO_ROOT / "realhw" / "acceptance" / "candidate_longrun_starts.ndjson"


class DeployedTree(NamedTuple):
    """報告內量測的 bundle 與 runtime 樹指紋。"""

    bundle_tree_sha256: str | None
    runtime_tree_sha256: str | None


def _invalid(detail: str) -> HarnessError:
    return HarnessError("HARNESS_EVIDENCE_INGEST_INVALID", detail)


def _is_digest(value: Any) -> bool:
    return isinstance(value, str) and _SHA256_RE.fullmatch(value) is not None


def _is_revision(value: Any) -> bool:
    return isinstance(value, str) and len(value) == 40


def _read_json(path: Path, *, where: str = "報告") -> dict[str, Any]:
    path = Path(path)
    try:
        raw = path.read_bytes()
        doc = json.loads(raw.decode("utf-8"))
    except (OSError, UnicodeDecodeError, ValueError) as exc:
        raise _invalid(f"{where} 無法讀取或解析：{path.name}：{exc}") from exc
    if not isinstance(doc, dict):
        raise _invalid(f"{where} 頂層必須是 JSON 物件：{path.name}")
    return doc


def _sha256_file(path: Path) -> str:
    try:
        return jsonio.sha256_file(Path(path))
    except OSError as exc:
        raise _invalid(f"檔案無法計算 sha256：{Path(path).name}：{exc}") from exc


def _timestamp(value: Any, *, where: str) -> str:
    if not isinstance(value, str):
        raise _invalid(f"{where} 缺少時間")
    try:
        parsed = jsonio.parse_iso(value)
        return parsed.astimezone(timezone.utc).isoformat()
    except (AttributeError, TypeError, ValueError) as exc:
        raise _invalid(f"{where} 時間格式無效：{value!r}") from exc


def _first_timestamp(values: Sequence[Any], *, where: str) -> str:
    for value in values:
        if value is not None:
            return _timestamp(value, where=where)
    raise _invalid(f"{where} 缺少開始時間")


def _check_cases_yaml_sha(value: Any, *, where: str) -> str | None:
    if value is None:
        return None
    if not _is_digest(value):
        raise _invalid(f"{where} cases_yaml_sha256 格式無效")
    if value != _sha256_file(_CASES_YAML_PATH):
        raise _invalid(f"{where} cases_yaml_sha256 與目前 cases.yaml 不符")
    return value


def _load_manifest(path: Path) -> tuple[dict[str, Any], str]:
    path = Path(path)
    doc = _read_json(path, where="release manifest")
    return doc, _sha256_file(path)


def _manifest_cli_sha(doc: Mapping[str, Any]) -> str | None:
    components = doc.get("components")
    cli = components.get("cli") if isinstance(components, Mapping) else None
    digest = cli.get("sha256") if isinstance(cli, Mapping) else None
    return digest if _is_digest(digest) else None


def _manifest_daemon_shas(doc: Mapping[str, Any] | None) -> frozenset[str]:
    if not isinstance(doc, Mapping):
        return frozenset()
    components = doc.get("components")
    daemon = components.get("daemon") if isinstance(components, Mapping) else None
    if not isinstance(daemon, Mapping):
        return frozenset()
    hashes: set[str] = set()
    for value in daemon.values():
        digest = value.get("sha256") if isinstance(value, Mapping) else None
        if _is_digest(digest):
            hashes.add(digest)
    for key in ("sha256", "runtime_sha256"):
        if _is_digest(daemon.get(key)):
            hashes.add(daemon[key])
    runtime = daemon.get("runtime")
    if isinstance(runtime, Mapping) and _is_digest(runtime.get("sha256")):
        hashes.add(runtime["sha256"])
    return frozenset(hashes)


def _manifest_from_report(
    brackets: Sequence[Mapping[str, Any] | None],
    *,
    artifact_id: str | None,
) -> dict[str, Any] | None:
    """只在報告提供的安裝路徑可讀且 manifest digest 相符時取其元件清單。"""
    if not _is_digest(artifact_id):
        return None
    for bracket in brackets:
        if not isinstance(bracket, Mapping):
            continue
        start = bracket.get("start")
        install_dir = start.get("install_dir") if isinstance(start, Mapping) else None
        if not isinstance(install_dir, str) or not install_dir:
            continue
        try:
            doc, digest = _load_manifest(Path(install_dir) / "release-manifest.json")
        except HarnessError:
            continue
        if digest == artifact_id:
            return doc
    return None


def _daemon_error(error: Any) -> bool:
    return isinstance(error, str) and error in {"daemon_outside_install", "daemon_mismatch"}


def _entry_dir_error(error: Any) -> bool:
    return isinstance(error, str) and error.startswith(("entry_dir_extra:", "entry_copy_mismatch:"))


def _validate_deployed_side(side: Mapping[str, Any], *, where: str) -> None:
    required = {
        "schema_version",
        "error",
        "bundle_tree_sha256",
        "runtime_tree_sha256",
        "release_manifest_sha256",
        "cli_sha256",
        "install_dir",
    }
    if not required <= set(side):
        raise _invalid(f"deployed-content.v1 欄位缺漏：{where}")
    if side.get("schema_version") != "deployed-content.v1":
        raise _invalid(f"deployed-content schema 不符：{where}")
    if not isinstance(side.get("error"), (str, type(None))):
        raise _invalid(f"deployed-content error 格式錯誤：{where}")
    if not isinstance(side.get("install_dir"), (str, type(None))):
        raise _invalid(f"deployed-content install_dir 格式錯誤：{where}")
    for key in (
        "bundle_tree_sha256",
        "runtime_tree_sha256",
        "release_manifest_sha256",
        "cli_sha256",
        "entry_dir_sha256",
        "daemon_sha256",
    ):
        value = side.get(key)
        if value is not None and not _is_digest(value):
            raise _invalid(f"deployed-content {key} 格式錯誤：{where}")
    for key in ("daemon_resolved", "cli_resolved", "cli_configured"):
        value = side.get(key)
        if value is not None and not isinstance(value, str):
            raise _invalid(f"deployed-content {key} 格式錯誤：{where}")


def _validate_daemon_resolved(
    side: Mapping[str, Any],
    *,
    manifest_doc: Mapping[str, Any] | None,
    where: str,
) -> bool:
    error = side.get("error")
    if error == "daemon_env_missing" and side.get("daemon_resolved") is None:
        return False
    if _daemon_error(error):
        raise _invalid(f"deployed daemon differs: {where}")
    resolved = side.get("daemon_resolved")
    daemon_sha = side.get("daemon_sha256")
    if resolved is None:
        return False
    if not isinstance(resolved, str) or not _is_digest(daemon_sha):
        return False
    expected = _manifest_daemon_shas(manifest_doc)
    if not expected:
        return False
    if daemon_sha not in expected:
        raise _invalid(f"deployed daemon differs: {where}")
    return True


def _validate_daemon_live(
    bracket: Mapping[str, Any],
    *,
    manifest_doc: Mapping[str, Any] | None,
    where: str,
) -> bool:
    live = bracket.get("daemon_live")
    if not isinstance(live, list) or not live:
        return False
    for position, row in enumerate(live, start=1):
        if not isinstance(row, Mapping):
            raise _invalid(f"deployed daemon differs: {where} daemon_live 第 {position} 筆格式錯")
        if row.get("error") is not None:
            return False
        if row.get("in_install") is not True or not _is_digest(row.get("image_sha256")):
            raise _invalid(f"deployed daemon differs: {where}")
        install_dir = bracket.get("start", {}).get("install_dir") if isinstance(bracket.get("start"), Mapping) else None
        if isinstance(bracket.get("end"), Mapping):
            end_install = bracket["end"].get("install_dir")
            if install_dir is None:
                install_dir = end_install
            elif end_install != install_dir:
                raise _invalid("deployed content changed during run")
        expected = _manifest_daemon_shas(manifest_doc)
        if not expected:
            return False
        if row.get("image_sha256") not in expected:
            raise _invalid(f"deployed daemon differs: {where}")
    return True


def _bracket_artifact_id(brackets: Sequence[Mapping[str, Any] | None]) -> str | None:
    for bracket in brackets:
        if bracket is None or not isinstance(bracket, Mapping):
            continue
        start = bracket.get("start")
        if isinstance(start, Mapping):
            digest = start.get("release_manifest_sha256")
            if _is_digest(digest):
                return digest
    return None


def deployed_tree(
    brackets: Sequence[Mapping[str, Any] | None],
    *,
    kind: str,
    artifact_id: str | None,
    cli_sha256: str | None,
    manifest_doc: Mapping[str, Any] | None = None,
) -> DeployedTree:
    """檢查報告中的部署量測 bracket，回傳一致且可信的 bundle／runtime 指紋。"""
    if not brackets:
        return DeployedTree(None, None)
    if manifest_doc is None:
        manifest_doc = _manifest_from_report(brackets, artifact_id=artifact_id)
    values: list[tuple[str, str, str, str | None]] = []
    for index, bracket in enumerate(brackets, start=1):
        if bracket is None:
            return DeployedTree(None, None)
        if not isinstance(bracket, Mapping) or not {"start", "end"} <= set(bracket):
            raise _invalid(f"report lacks deployed_content: {kind}")
        start = bracket["start"]
        end = bracket["end"]
        if not isinstance(start, Mapping) or not isinstance(end, Mapping):
            raise _invalid(f"report lacks deployed_content: {kind}")
        where = f"{kind} bracket {index}"
        _validate_deployed_side(start, where=f"{where} start")
        _validate_deployed_side(end, where=f"{where} end")

        for side_name, side in (("start", start), ("end", end)):
            error = side.get("error")
            if _entry_dir_error(error):
                raise _invalid(f"deployed entry dir differs: {where} {side_name}")
            if _daemon_error(error):
                raise _invalid(f"deployed daemon differs: {where} {side_name}")
            if error is not None and error != "daemon_env_missing":
                return DeployedTree(None, None)

        entry_start = start.get("entry_dir_sha256")
        entry_end = end.get("entry_dir_sha256")
        if entry_start is not None and entry_end is not None and entry_start != entry_end:
            raise _invalid("deployed content changed during run")

        if kind in {"junit", "functest-report"}:
            checked = [
                _validate_daemon_resolved(start, manifest_doc=manifest_doc, where=f"{where} start"),
                _validate_daemon_resolved(end, manifest_doc=manifest_doc, where=f"{where} end"),
            ]
            if not all(checked):
                return DeployedTree(None, None)
        else:
            if not _validate_daemon_live(bracket, manifest_doc=manifest_doc, where=where):
                return DeployedTree(None, None)

        bundle = start.get("bundle_tree_sha256")
        runtime = start.get("runtime_tree_sha256")
        manifest = start.get("release_manifest_sha256")
        cli = start.get("cli_sha256")
        if not all(_is_digest(value) for value in (bundle, runtime, manifest, cli)):
            return DeployedTree(None, None)
        if artifact_id is not None and manifest != artifact_id:
            raise _invalid("deployed manifest differs from report artifact")
        if cli_sha256 is not None and cli != cli_sha256:
            raise _invalid("deployed cli differs from report cli")

        end_values = (
            end.get("bundle_tree_sha256"),
            end.get("runtime_tree_sha256"),
            end.get("release_manifest_sha256"),
        )
        if not all(_is_digest(value) for value in end_values):
            return DeployedTree(None, None)
        if (bundle, runtime, manifest) != end_values:
            raise _invalid("deployed content changed during run")
        values.append((bundle, runtime, manifest, cli))

    first = values[0]
    if any(row[:3] != first[:3] for row in values[1:]):
        raise _invalid("deployed content changed during run")
    if cli_sha256 is not None and any(row[3] != cli_sha256 for row in values):
        raise _invalid("deployed cli differs from report cli")
    if artifact_id is not None and first[2] != artifact_id:
        raise _invalid("deployed manifest differs from report artifact")
    return DeployedTree(first[0], first[1])


def _load_l1_judge():
    spec = importlib.util.spec_from_file_location("stage3_p2_l1_judge", _L1_JUDGE_PATH)
    if spec is None or spec.loader is None:
        raise _invalid(f"P2 l1_judge.py 無法載入：{_L1_JUDGE_PATH}")
    module = importlib.util.module_from_spec(spec)
    try:
        spec.loader.exec_module(module)
    except Exception as exc:
        raise _invalid(f"P2 l1_judge.py 載入失敗：{exc}") from exc
    return module


def _read_l1_lock(l1_lock: Mapping[str, Any] | None) -> Mapping[str, Any]:
    if l1_lock is not None:
        if not isinstance(l1_lock, Mapping):
            raise _invalid("L1 凍結集合必須是 JSON 物件")
        return l1_lock
    try:
        doc = json.loads(_L1_LOCK_PATH.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, ValueError) as exc:
        raise _invalid(f"L1 凍結集合無法讀取：{_L1_LOCK_PATH.name}：{exc}") from exc
    if not isinstance(doc, Mapping):
        raise _invalid("L1 凍結集合頂層必須是 JSON 物件")
    return doc


def _earliest_aware_timestamp(values: Sequence[Any]) -> str | None:
    parsed: list[datetime] = []
    for value in values:
        if not isinstance(value, str):
            continue
        try:
            parsed.append(jsonio.parse_iso(value).astimezone(timezone.utc))
        except (TypeError, ValueError):
            continue
    return min(parsed).isoformat() if parsed else None


def _junit_counts(path: Path) -> tuple[dict[str, int], str | None]:
    try:
        root = ET.parse(path).getroot()
    except (OSError, ET.ParseError) as exc:
        raise _invalid(f"junit XML 無法解析：{Path(path).name}：{exc}") from exc
    keys = ("tests", "failures", "errors", "skipped")
    suites = [node for node in root.iter() if node.tag.rsplit("}", 1)[-1] == "testsuite"]
    suite_timestamp = _earliest_aware_timestamp(
        [root.attrib.get("timestamp"), *(node.attrib.get("timestamp") for node in suites)]
    )
    if all(key in root.attrib for key in keys):
        values = root.attrib
    else:
        totals: dict[str, int] = {}
        try:
            for key in keys:
                totals[key] = sum(int(node.attrib.get(key, "0")) for node in suites)
        except ValueError as exc:
            raise _invalid(f"junit XML 計數不是整數：{Path(path).name}") from exc
        if not suites:
            raise _invalid(f"junit XML 缺少計數：{Path(path).name}")
        return totals, suite_timestamp
    try:
        counts = {key: int(values[key]) for key in keys}
    except (TypeError, ValueError) as exc:
        raise _invalid(f"junit XML 計數不是整數：{Path(path).name}") from exc
    if any(value < 0 for value in counts.values()):
        raise _invalid(f"junit XML 計數不可為負：{Path(path).name}")
    return counts, suite_timestamp


def _source_files(path: Path) -> tuple[Path, ...]:
    path = Path(path)
    if not path.exists():
        raise _invalid(f"證據檔案不存在：{path}")
    if path.is_dir():
        files: list[Path] = []
        for candidate in sorted(path.rglob("*"), key=lambda p: p.relative_to(path).as_posix().encode("utf-8")):
            try:
                mode = candidate.lstat().st_mode
            except OSError as exc:
                raise _invalid(f"證據檔案無法檢查：{candidate.name}：{exc}") from exc
            if stat.S_ISLNK(mode):
                raise _invalid(f"證據目錄含 symlink：{candidate.name}")
            if stat.S_ISREG(mode):
                files.append(candidate)
            elif not stat.S_ISDIR(mode):
                raise _invalid(f"證據目錄含非一般檔案：{candidate.name}")
        if not files:
            raise _invalid(f"證據目錄沒有一般檔案：{path.name}")
        return tuple(files)
    try:
        mode = path.lstat().st_mode
    except OSError as exc:
        raise _invalid(f"證據檔案無法檢查：{path.name}：{exc}") from exc
    if not stat.S_ISREG(mode):
        raise _invalid(f"證據來源不是一般檔案：{path.name}")
    return (path,)


def _load_deployed(bracket: Any, *, kind: str) -> Mapping[str, Any] | None:
    if bracket is None:
        return None
    if not isinstance(bracket, Mapping) or not {"start", "end"} <= set(bracket):
        raise _invalid(f"report lacks deployed_content: {kind}")
    return bracket


def _case_ids(
    item: Any,
    *,
    cases: Mapping[str, dict],
    lock: Mapping[str, Any],
    platform: str,
    tiers: frozenset[str] | None = None,
) -> list[str]:
    set_id = getattr(item, "required_case_set", None)
    if set_id is not None:
        try:
            pool = lock["sets"][set_id]
        except (KeyError, TypeError):
            return []
        if isinstance(pool, Mapping):
            pool = pool.get(platform, [])
        if not isinstance(pool, Sequence) or isinstance(pool, (str, bytes)):
            raise _invalid(f"frozen case set {set_id} 的平台清單格式無效")
        if not all(isinstance(case_id, str) and case_id for case_id in pool):
            raise _invalid(f"frozen case set {set_id} 含無效案例 ID")
        candidates = list(pool)
        duplicate_ids = sorted(case_id for case_id, count in Counter(candidates).items() if count > 1)
        if duplicate_ids:
            raise _invalid(f"frozen case set {set_id} 含重複案例：{duplicate_ids}")
    else:
        candidates = list(cases)
    layer = str(getattr(item, "layer", "")).casefold()
    required: list[str] = []
    for case_id in candidates:
        case = cases.get(case_id)
        if not isinstance(case, Mapping):
            if set_id is not None:
                raise _invalid(f"frozen case {case_id} has no definition")
            continue
        case_layer = case.get("layer")
        platforms = case.get("platforms", ())
        if (not isinstance(case_layer, str) or not case_layer
                or not isinstance(platforms, Sequence) or isinstance(platforms, (str, bytes))
                or not all(isinstance(value, str) for value in platforms)):
            if set_id is not None:
                raise _invalid(f"frozen case {case_id} has malformed definition")
            continue
        if (
            case_layer.casefold() == layer
            and platform in platforms
        ):
            if tiers is not None:
                family = case.get("family")
                if not isinstance(family, str) or not family:
                    raise _invalid(f"realhw frozen case {case_id} lacks family tier")
                if family.casefold() not in tiers:
                    continue
            required.append(case_id)
    return required


def _layer_values(kind: str, report: Mapping[str, Any], *, runs: Sequence[Mapping[str, Any]] = ()) -> tuple[str, ...]:
    values: list[str] = []
    if kind == "ab-report":
        sides = report.get("sides")
        if isinstance(sides, list):
            values.extend(side.get("evidence_layer") for side in sides if isinstance(side, Mapping) and "evidence_layer" in side)
        if "evidence_layer" in report:
            values.append(report["evidence_layer"])
    elif kind == "functest-report":
        values.extend(run.get("evidence_layer") for run in runs if "evidence_layer" in run)
    elif "evidence_layer" in report:
        values.append(report["evidence_layer"])
    out: list[str] = []
    for value in values:
        if value is None:
            continue
        if not isinstance(value, str) or value not in _ALLOWED_EVIDENCE_LAYERS:
            raise _invalid(f"unknown evidence_layer: {value!r}")
        if value not in out:
            out.append(value)
    return tuple(out)


def _check_evidence_layers(kind: str, report: Mapping[str, Any], item: Any,
                           *, runs: Sequence[Mapping[str, Any]] = ()) -> tuple[str, ...]:
    layers = _layer_values(kind, report, runs=runs)
    item_layer = str(getattr(item, "layer", "")).casefold()
    if getattr(item, "candidate_longrun", False):
        real_board = True
    else:
        real_board = item_layer not in _ALLOWED_NON_BOARD_ITEM_LAYERS
    if real_board:
        for value in layers:
            if value in _NON_BOARD_EVIDENCE_LAYERS:
                raise _invalid(f"evidence layer not real-board: {value}")
    return layers


def _with_layer_note(note: str, layers: Sequence[str]) -> str:
    if not layers:
        return note
    return f"{note}；evidence_layer：{', '.join(layers)}"


def _derive_junit(
    report_path: Path,
    *,
    item: Any,
    platform: str,
    l1_lock: Mapping[str, Any] | None,
    release_manifest: Path | None,
) -> Derived:
    report_path = Path(report_path)
    direct_json = report_path.suffix.casefold() == ".json"
    resource_path = report_path if direct_json else report_path.parent / "resource-report.json"
    report = _read_json(resource_path, where="SW_RESOURCE_REPORT")
    if "deployed_content" not in report:
        raise _invalid("report lacks deployed_content: junit")
    layers = _check_evidence_layers("junit", report, item)
    deployed = report["deployed_content"]
    bracket = _load_deployed(deployed, kind="junit")
    brackets: list[Mapping[str, Any] | None] = [bracket]
    artifact_id = _bracket_artifact_id(brackets)
    runner = report.get("runner")
    if runner == "cargo":
        counts, junit_started = None, None
        has_junit = False
    elif direct_json:
        raise _invalid("pytest junit XML is required")
    else:
        counts, junit_started = _junit_counts(report_path)
        has_junit = True

    manifest_doc: dict[str, Any] | None = None
    if release_manifest is not None:
        manifest_doc, provided_artifact = _load_manifest(Path(release_manifest))
        if artifact_id is not None and artifact_id != provided_artifact:
            raise _invalid("deployed manifest differs from report artifact")
        artifact_id = provided_artifact

    set_id = getattr(item, "required_l1_set", None)
    if not isinstance(set_id, str) or not set_id:
        raise _invalid("junit item lacks required_l1_set")
    frozen = _read_l1_lock(l1_lock)
    judge = _load_l1_judge()
    selector = None
    try:
        required = judge.required_for(frozen, set_id, platform)
        selector = frozen["sets"][set_id][platform].get("selector")
    except (KeyError, TypeError):
        required = []
        missing_set = True
    else:
        missing_set = False
    verdict = judge.judge(report, required, platform=platform, junit=counts, selector=selector)
    if missing_set:
        verdict = dict(verdict)
        verdict["reasons"] = ["no frozen l1 set", *verdict.get("reasons", [])]
    if verdict.get("verdict") == "INVALID":
        raise _invalid("; ".join(str(reason) for reason in verdict.get("reasons", ["invalid l1-results report"])))
    if verdict.get("verdict") not in {"PASS", "FAIL", "INSUFFICIENT"}:
        raise _invalid(f"P2 l1_judge 回傳未知結果：{verdict.get('verdict')!r}")

    tree = deployed_tree(
        brackets,
        kind="junit",
        artifact_id=artifact_id,
        cli_sha256=None,
        manifest_doc=manifest_doc,
    )
    started = _started_at("junit", report, junit_started=junit_started, report_path=resource_path)
    return Derived(
        result=verdict["verdict"],
        artifact_id=artifact_id,
        contract_revision=None,
        tool_revision=None,
        case_list_sha256=None,
        workload_sha256=None,
        bundle_tree_sha256=tree.bundle_tree_sha256,
        runtime_tree_sha256=tree.runtime_tree_sha256,
        xfail_cases=(),
        started_at=started,
        files=tuple(dict.fromkeys((report_path, resource_path))) if has_junit else (resource_path,),
        ledger_through=None,
        ledger_records=None,
        l1_set_sha256=verdict.get("required_sha256"),
        l1_verdict=verdict,
        note=_with_layer_note(f"L1 凍結案例判定：{verdict['verdict']}", layers),
        detail=tuple(str(reason) for reason in verdict.get("reasons", [])),
    )


def _report_bracket(doc: Mapping[str, Any], *, kind: str) -> Mapping[str, Any] | None:
    if kind == "realhw-report" or kind == "regression-report":
        meta = doc.get("meta")
        if not isinstance(meta, Mapping) or "deployed_content" not in meta:
            raise _invalid(f"report lacks deployed_content: {kind}")
        value = meta["deployed_content"]
    elif kind == "gate-report":
        run = doc.get("run")
        if not isinstance(run, Mapping) or "deployed_content" not in run:
            raise _invalid(f"report lacks deployed_content: {kind}")
        value = run["deployed_content"]
    elif kind == "manual-procedure":
        if "deployed_content" not in doc:
            raise _invalid(f"report lacks deployed_content: {kind}")
        value = doc["deployed_content"]
    else:
        raise _invalid(f"不支援的 deployed_content 類型：{kind}")
    if value is None:
        return None
    if not isinstance(value, Mapping) or not {"start", "end"} <= set(value):
        raise _invalid(f"report lacks deployed_content: {kind}")
    return value


def _reported_cli_sha(doc: Mapping[str, Any], *, kind: str) -> str | None:
    if kind in {"realhw-report", "regression-report"}:
        meta = doc.get("meta")
        identity = meta.get("cli_identity") if isinstance(meta, Mapping) else None
    elif kind == "gate-report":
        run = doc.get("run")
        identity = run.get("cli_identity") if isinstance(run, Mapping) else None
    else:
        return None
    if identity is None:
        return None
    if not isinstance(identity, Mapping) or not _is_digest(identity.get("sha256")):
        raise _invalid(f"{kind} 報告的 CLI 身分缺少 sha256")
    return identity["sha256"]


def _started_at(
    kind: str,
    doc: Mapping[str, Any],
    *,
    junit_started: str | None = None,
    report_path: Path | None = None,
    manifest: Mapping[str, Any] | None = None,
) -> str:
    values: list[Any] = []
    if kind == "junit":
        deployed = doc.get("deployed_content")
        start = deployed.get("start") if isinstance(deployed, Mapping) else None
        values.extend((doc.get("started_at"), doc.get("started_at_utc")))
        values.append(start.get("measured_at") if isinstance(start, Mapping) else None)
        values.append(junit_started)
        if report_path is not None:
            try:
                values.append(datetime.fromtimestamp(Path(report_path).stat().st_mtime, timezone.utc).isoformat())
            except OSError as exc:
                raise _invalid(f"junit resource-report mtime 無法讀取：{exc}") from exc
    elif kind == "functest-report":
        values.extend((doc.get("started_at"), manifest.get("created_at") if isinstance(manifest, Mapping) else None))
    elif kind in {"realhw-report", "regression-report"}:
        meta = doc.get("meta")
        if isinstance(meta, Mapping):
            deployed = meta.get("deployed_content")
            if not isinstance(deployed, Mapping):
                deployed = meta.get("deployed_content_start")
            start = deployed.get("start") if isinstance(deployed, Mapping) else deployed
            if isinstance(deployed, Mapping) and not isinstance(start, Mapping):
                start = deployed
            values.append(meta.get("started_at_utc"))
            values.append(start.get("measured_at") if isinstance(start, Mapping) else None)
    elif kind == "gate-report":
        run = doc.get("run")
        if isinstance(run, Mapping):
            values.append(run.get("started_at_utc"))
    elif kind == "bridge-selftest":
        values.append(doc.get("created_at"))
    elif kind == "manual-procedure":
        values.append(doc.get("executed_at"))
    else:
        values.extend((doc.get("started_at"), doc.get("started_at_utc")))
    return _first_timestamp(values, where=kind)


def _report_started(doc: Mapping[str, Any], *, kind: str) -> str:
    return _started_at(kind, doc)


def _approved_xfail(
    case_id: str,
    row: Mapping[str, Any],
    *,
    cases: Mapping[str, dict],
    check_time: datetime,
) -> bool:
    case = cases.get(case_id)
    if not isinstance(case, Mapping) or case.get("expected") != "XFAIL":
        return False
    invariants = case.get("invariants") or []
    if isinstance(invariants, str):
        invariants = [invariants]
    if not isinstance(invariants, list) or not all(isinstance(value, str) for value in invariants):
        raise _invalid(f"案例 {case_id} 的 invariants 結構不符")
    if invariants:
        return False
    sentinel = case.get("sentinel")
    if not isinstance(sentinel, Mapping):
        return False
    try:
        raw_expiry = sentinel.get("expires")
        if isinstance(raw_expiry, datetime):
            expiry = raw_expiry if raw_expiry.tzinfo is not None else None
        elif isinstance(raw_expiry, date):
            expiry = datetime.combine(raw_expiry, time.min, tzinfo=timezone.utc)
        elif isinstance(raw_expiry, str):
            try:
                expiry = jsonio.parse_iso(raw_expiry)
            except ValueError:
                expiry = datetime.combine(date.fromisoformat(raw_expiry), time.min, tzinfo=timezone.utc)
        else:
            expiry = None
    except (AttributeError, TypeError, ValueError):
        return False
    if expiry is None:
        return False
    signature = sentinel.get("signature")
    reason = row.get("reason", "")
    return (
        expiry.astimezone(timezone.utc) > check_time
        and isinstance(signature, str)
        and bool(signature)
        and signature in str(reason or "")
    )


def _derive_suite_report(
    kind: str,
    report_path: Path,
    *,
    item: Any,
    cases: Mapping[str, dict],
    lock: Mapping[str, Any],
    release_manifest: Path | None,
    platform: str,
    now: str | None,
) -> Derived:
    report_path = Path(report_path)
    doc = _read_json(report_path, where=kind)
    manifest_doc: dict[str, Any] | None = None
    artifact_id: str | None = None
    if release_manifest is not None:
        manifest_doc, artifact_id = _load_manifest(Path(release_manifest))
    bracket = _report_bracket(doc, kind=kind)
    cli_sha = _reported_cli_sha(doc, kind=kind)
    if kind in {"realhw-report", "regression-report"} and cli_sha is None:
        raise _invalid(f"{kind} 報告缺少 meta.cli_identity.sha256")
    if kind == "gate-report":
        if manifest_doc is None:
            raise _invalid("gate-report 必須提供 --release-manifest")
        expected_cli = _manifest_cli_sha(manifest_doc)
        run = doc.get("run")
        identity = run.get("cli_identity") if isinstance(run, Mapping) else None
        run_cli = identity.get("sha256") if isinstance(identity, Mapping) else None
        if expected_cli is None or run_cli != expected_cli:
            raise _invalid("release manifest CLI sha256 與 gate 報告不符")
        cli_sha = run_cli

    if bracket is None:
        brackets: list[Mapping[str, Any] | None] = [None]
    else:
        brackets = [bracket]
    if artifact_id is None:
        artifact_id = _bracket_artifact_id(brackets)
    tree = deployed_tree(
        brackets,
        kind=kind,
        artifact_id=artifact_id,
        cli_sha256=cli_sha,
        manifest_doc=manifest_doc,
    )

    meta_for_tiers = doc.get("meta")
    tiers: frozenset[str] | None = None
    if kind == "realhw-report":
        raw_tiers = meta_for_tiers.get("tiers") if isinstance(meta_for_tiers, Mapping) else None
        if not isinstance(raw_tiers, str) or not raw_tiers.strip():
            raise _invalid("realhw 報告缺少 meta.tiers")
        tier_values = [value.strip().casefold() for value in raw_tiers.split(",")]
        if not all(tier_values):
            raise _invalid("realhw 報告 meta.tiers 格式無效")
        tiers = frozenset(tier_values)
    required = _case_ids(item, cases=cases, lock=lock, platform=platform, tiers=tiers)
    result = "PASS"
    details: list[str] = []
    xfail_cases: list[str] = []
    workload_sha: str | None = None
    contract: str | None = None
    tool: str | None = None
    case_sha: str | None = None
    if kind == "gate-report":
        if doc.get("platform") != platform:
            raise _invalid("gate-report platform 與指定平台不符")
        mode = doc.get("mode")
        if not isinstance(mode, str) or mode not in {"baseline", "candidate"}:
            raise _invalid(f"gate-report mode 無效：{mode!r}")
        expected_mode = "candidate" if getattr(item, "candidate_longrun", False) else "baseline"
        if mode != expected_mode:
            raise _invalid(f"gate-report mode 與出口項目不符：{mode} != {expected_mode}")
        raw_result = doc.get("baseline_result") if mode == "baseline" else doc.get("release_gate_result")
        if doc.get("engine_result", {}).get("verdict") == "FAIL" if isinstance(doc.get("engine_result"), Mapping) else False:
            result = "FAIL"
            details.append("engine_result.verdict == FAIL")
        elif raw_result == "PASS":
            result = "PASS"
        elif raw_result == "FAIL":
            result = "FAIL"
        elif isinstance(raw_result, str) and raw_result in {
            "INSUFFICIENT_EVIDENCE", "BLOCKED", "NOT_APPLICABLE_BASELINE"
        }:
            result = "INSUFFICIENT"
        else:
            raise _invalid(f"gate-report 結果無效：{raw_result!r}")
        run = doc.get("run")
        acceptance = doc.get("acceptance")
        effective = acceptance.get("effective_content") if isinstance(acceptance, Mapping) else None
        workload = effective.get("workload") if isinstance(effective, Mapping) else None
        engine_files = workload.get("engine_files") if isinstance(workload, Mapping) else None
        engine_files_sha = run.get("engine_files_sha256") if isinstance(run, Mapping) else None
        if (not isinstance(engine_files, list) or not engine_files
                or not all(isinstance(path, str) and path for path in engine_files)
                or len(set(engine_files)) != len(engine_files)
                or not isinstance(engine_files_sha, Mapping)
                or set(engine_files_sha) != set(engine_files)
                or not all(_is_digest(engine_files_sha[path]) for path in engine_files)):
            raise _invalid("gate-report engine_files_sha256 缺少有效完整 workload 指紋")
        pairs = [[path, engine_files_sha[path]] for path in engine_files]
        workload_sha = jsonio.sha256_bytes(jsonio.dumps_compact(pairs).encode("utf-8"))
        contract_value = run.get("contract_revision") if isinstance(run, Mapping) else None
        if contract_value is not None and not _is_revision(contract_value):
            raise _invalid("gate-report contract_revision 格式無效")
        contract = contract_value
        _check_cases_yaml_sha(run.get("cases_yaml_sha256") if isinstance(run, Mapping) else None,
                              where="gate-report")
        case_lock_sha = run.get("case_lock_sha256") if isinstance(run, Mapping) else None
        if case_lock_sha is not None and not _is_digest(case_lock_sha):
            raise _invalid("gate-report case_lock_sha256 格式無效")
        case_sha = case_lock_sha
        started = _report_started(doc, kind=kind)
        files = (report_path,)
        note = f"release gate 判定：{result}"
    else:
        results = doc.get("results")
        if not isinstance(results, list):
            raise _invalid(f"{kind} 缺少 results 陣列")
        ids: list[str] = []
        rows: list[tuple[str, str, Mapping[str, Any]]] = []
        rows_by_id: dict[str, Mapping[str, Any]] = {}
        for row in results:
            if not isinstance(row, Mapping):
                raise _invalid(f"{kind} results 含非物件列")
            case_id = row.get("id", row.get("case_id"))
            verdict = row.get("verdict")
            if not isinstance(case_id, str) or not case_id:
                raise _invalid(f"{kind} results 列缺少案例 id")
            ids.append(case_id)
            verdict_text = verdict if isinstance(verdict, str) else None
            rows.append((case_id, verdict_text, row))
            rows_by_id[case_id] = row
            if verdict_text not in {"PASS", "FAIL", "XFAIL", "XPASS", "SKIP", "ERROR", "INSUFFICIENT_EVIDENCE"}:
                details.append(f"unknown verdict: {case_id}={verdict!r}")
        duplicate = sorted(case_id for case_id, count in Counter(ids).items() if count > 1)
        missing = [case_id for case_id in required if case_id not in rows_by_id]
        if duplicate or missing or details:
            result = "INSUFFICIENT"
            if duplicate:
                details.append(f"duplicate cases: {duplicate}")
            if missing:
                details.append(f"missing cases: {missing}")
        if not required and result != "FAIL":
            result = "INSUFFICIENT"
            details.append("no frozen required case set")
        failed = False
        insufficient = False
        check_time = jsonio.parse_iso(now).astimezone(timezone.utc) if now is not None else datetime.now(timezone.utc)

        # 任一報告列的 FAIL 都不能被 required set 以外的身分隱藏。
        for case_id, verdict, row in rows:
            if verdict in {"FAIL", "XFAIL"}:
                if _approved_xfail(case_id, row, cases=cases, check_time=check_time):
                    xfail_cases.append(case_id)
                else:
                    failed = True
                    details.append(f"unapproved {verdict.casefold()}: {case_id}")
            elif verdict not in {"PASS", "XPASS", "SKIP", "ERROR", "INSUFFICIENT_EVIDENCE"}:
                insufficient = True

        for case_id in required:
            row = rows_by_id.get(case_id)
            if row is None:
                continue
            raw_verdict = row.get("verdict")
            verdict = raw_verdict if isinstance(raw_verdict, str) else None
            if verdict == "PASS" or (
                verdict in {"FAIL", "XFAIL"}
                and _approved_xfail(case_id, row, cases=cases, check_time=check_time)
            ):
                continue
            if verdict not in {"FAIL", "XFAIL"}:
                insufficient = True
                details.append(f"insufficient verdict: {case_id}={raw_verdict!r}")
        if failed:
            result = "FAIL"
        elif insufficient:
            result = "INSUFFICIENT"
        started = _report_started(doc, kind=kind)
        meta = doc.get("meta")
        contract = meta.get("contract_revision") if isinstance(meta, Mapping) else doc.get("contract_revision")
        tool = meta.get("tool_revision") if isinstance(meta, Mapping) else doc.get("tool_revision")
        _check_cases_yaml_sha(meta.get("cases_yaml_sha256") if isinstance(meta, Mapping) else None,
                              where=kind)
        case_sha = meta.get("case_lock_sha256") if isinstance(meta, Mapping) else doc.get("case_lock_sha256")
        if case_sha is not None and not _is_digest(case_sha):
            raise _invalid(f"{kind} case_lock_sha256 格式無效")
        workload_sha = meta.get("workload_sha256") if isinstance(meta, Mapping) else doc.get("workload_sha256")
        if contract is not None and not _is_revision(contract):
            raise _invalid(f"{kind} contract_revision 格式無效")
        if tool is not None and not _is_revision(tool):
            raise _invalid(f"{kind} tool_revision 格式無效")
        workload_sha = workload_sha if _is_digest(workload_sha) else None
        files = (report_path,)
        note = f"{kind} 單側案例判定：{result}"
        if kind == "realhw-report":
            meta = doc.get("meta")
            contract = meta.get("contract_revision", contract) if isinstance(meta, Mapping) else contract
            tool = meta.get("tool_revision", tool) if isinstance(meta, Mapping) else tool

    layers = _check_evidence_layers(kind, doc, item)
    note = _with_layer_note(note, layers)
    return Derived(
        result=result,
        artifact_id=artifact_id,
        contract_revision=contract,
        tool_revision=tool,
        case_list_sha256=case_sha,
        workload_sha256=workload_sha,
        bundle_tree_sha256=tree.bundle_tree_sha256,
        runtime_tree_sha256=tree.runtime_tree_sha256,
        xfail_cases=tuple(sorted(set(xfail_cases))),
        started_at=started,
        files=files,
        ledger_through=None,
        ledger_records=None,
        l1_set_sha256=None,
        l1_verdict=None,
        note=note,
        detail=tuple(details),
    )


def _derive_functest(
    report_dir: Path,
    *,
    item: Any,
    cases: Mapping[str, dict],
    lock: Mapping[str, Any],
    platform: str,
    release_manifest: Path | None,
) -> Derived:
    report_dir = Path(report_dir)
    if not report_dir.is_dir():
        raise _invalid(f"func-test 報告目錄不存在：{report_dir}")
    required = _case_ids(item, cases=cases, lock=lock, platform=platform)
    manifest_paths = sorted(report_dir.rglob("manifest.json"), key=lambda p: p.as_posix().encode("utf-8"))
    runs: list[dict[str, Any]] = []
    brackets: list[Mapping[str, Any] | None] = []
    artifact_ids: list[str] = []
    cli_ids: list[str] = []
    starts: list[str] = []
    contract_revisions: list[str | None] = []
    cases_yaml_shas: list[str | None] = []
    result = "PASS"
    details: list[str] = []
    run_case_ids: set[str] = set()
    for manifest_path in manifest_paths:
        manifest = _read_json(manifest_path, where="func-test manifest")
        case_id = manifest.get("case_id")
        if not isinstance(case_id, str):
            raise _invalid(f"func-test manifest 缺少 case_id：{manifest_path.name}")
        starts.append(_started_at("functest-report", {}, manifest=manifest))
        contract_revision = manifest.get("contract_revision")
        if contract_revision is not None and not _is_revision(contract_revision):
            raise _invalid("func-test manifest contract_revision 格式無效")
        contract_revisions.append(contract_revision)
        cases_yaml_shas.append(
            _check_cases_yaml_sha(manifest.get("cases_yaml_sha256"), where="func-test manifest")
        )
        run_case_ids.add(case_id)
        runs.append(manifest)
        artifacts = manifest.get("artifacts")
        if not isinstance(artifacts, Mapping):
            raise _invalid(f"func-test manifest 缺少 artifacts：{manifest_path.name}")
        deployed = _load_deployed(artifacts.get("deployed_content"), kind="functest-report")
        if "deployed_content" not in artifacts:
            raise _invalid("report lacks deployed_content: functest-report")
        brackets.append(deployed)
        release_sha = artifacts.get("release_manifest_sha256")
        if release_sha is not None:
            if not _is_digest(release_sha):
                raise _invalid("func-test manifest release_manifest_sha256 格式無效")
            artifact_ids.append(release_sha)
        cli = artifacts.get("cli")
        cli_sha = cli.get("sha256") if isinstance(cli, Mapping) else None
        if cli_sha is not None:
            if not _is_digest(cli_sha):
                raise _invalid("func-test manifest CLI sha256 格式無效")
            cli_ids.append(cli_sha)

        run_dir = manifest_path.parent
        comparison_path = run_dir / "comparison.json"
        bridge_path = run_dir / "bridge-manifest.json"
        comparison = _read_json(comparison_path, where="func-test comparison")
        bridge = _read_json(bridge_path, where="func-test bridge manifest")
        verdict = comparison.get("verdict")
        invariant_rows = comparison.get("invariants", [])
        invariant_fail = isinstance(invariant_rows, list) and any(
            isinstance(row, Mapping) and row.get("verdict") == "FAIL" for row in invariant_rows
        )
        if verdict == "FAIL" or invariant_fail:
            result = "FAIL"
            details.append(f"case failed: {case_id}")
        elif verdict != "PASS" or bridge.get("closed_cleanly") is not True:
            if result != "FAIL":
                result = "INSUFFICIENT"
            details.append(f"case insufficient: {case_id}")

    missing = [case_id for case_id in required if case_id not in run_case_ids]
    if missing and result != "FAIL":
        result = "INSUFFICIENT"
        details.append(f"missing cases: {missing}")
    if not manifest_paths:
        result = "INSUFFICIENT"
        details.append("func-test report has no run manifests")
    if not required and result != "FAIL":
        result = "INSUFFICIENT"
        details.append("no required L1.5 cases")
    if len(set(artifact_ids)) > 1 or len(set(cli_ids)) > 1:
        raise _invalid("func-test artifact identity changed between runs")
    if len(set(contract_revisions)) > 1 or len(set(cases_yaml_shas)) > 1:
        raise _invalid("func-test contract identity changed between runs")
    artifact_id = artifact_ids[0] if artifact_ids else _bracket_artifact_id(brackets)
    cli_sha = cli_ids[0] if cli_ids else None
    manifest_doc: dict[str, Any] | None = None
    if release_manifest is not None:
        manifest_doc, provided_artifact = _load_manifest(Path(release_manifest))
        if artifact_id is not None and artifact_id != provided_artifact:
            raise _invalid("report artifact differs from --release-manifest")
        artifact_id = provided_artifact
    tree = deployed_tree(
        brackets,
        kind="functest-report",
        artifact_id=artifact_id,
        cli_sha256=cli_sha,
        manifest_doc=manifest_doc,
    )
    layers = _check_evidence_layers("functest-report", {}, item, runs=runs)
    note = _with_layer_note(f"func-test 單側案例判定：{result}", layers)
    summary_path = report_dir / "func-test-report.json"
    if summary_path.is_file():
        summary = _read_json(summary_path, where="func-test summary")
        started = _started_at("functest-report", summary, manifest=(runs[0] if runs else None))
    elif starts:
        started = min(starts, key=jsonio.parse_iso)
    else:
        raise _invalid("func-test 報告沒有 run 開始時間")
    return Derived(
        result=result,
        artifact_id=artifact_id,
        contract_revision=contract_revisions[0] if contract_revisions else None,
        tool_revision=None,
        case_list_sha256=None,
        workload_sha256=None,
        bundle_tree_sha256=tree.bundle_tree_sha256,
        runtime_tree_sha256=tree.runtime_tree_sha256,
        xfail_cases=(),
        started_at=started,
        files=(report_dir,),
        ledger_through=None,
        ledger_records=None,
        l1_set_sha256=None,
        l1_verdict=None,
        note=note,
        detail=tuple(details),
    )


def _derive_ab(report_path: Path, *, item: Any, platform: str) -> Derived:
    path = Path(report_path)
    doc = _read_json(path, where="A/B 報告")
    sides = doc.get("sides")
    if not isinstance(sides, list) or len(sides) != 2 or not all(isinstance(side, Mapping) for side in sides):
        raise _invalid("A/B 報告 sides 必須恰含 A、B 兩側")
    a_side, b_side = sides
    if doc.get("platform") != platform:
        raise _invalid("A/B 報告 platform 與指定平台不符")
    if str(doc.get("layer", "")).casefold() != str(getattr(item, "layer", "")).casefold():
        raise _invalid("A/B 報告 layer 與出口項目不符")
    if not isinstance(doc.get("stage"), str) or doc.get("stage") != str(getattr(item, "stage", "")):
        raise _invalid("A/B 報告 stage 與出口項目不符")
    artifact_id = b_side.get("release_manifest_sha256")
    if not _is_digest(artifact_id):
        raise _invalid("A/B 報告 B 側缺少有效 release_manifest_sha256")
    bundle = b_side.get("bundle_tree_sha256")
    runtime = b_side.get("runtime_tree_sha256")
    if not _is_digest(bundle) or not _is_digest(runtime):
        raise _invalid("A/B 報告 B 側缺少有效 bundle_tree_sha256／runtime_tree_sha256")
    contract = doc.get("contract_revision")
    tool = doc.get("tool_revision")
    case_yaml_sha = _check_cases_yaml_sha(doc.get("case_list_sha256"), where="A/B 報告")
    case_sha = doc.get("case_lock_sha256")
    if not _is_revision(contract) or not _is_revision(tool) or not _is_digest(case_sha):
        raise _invalid("A/B 報告缺少有效 contract_revision／tool_revision／case_lock_sha256")
    if case_yaml_sha is None:
        raise _invalid("A/B 報告缺少 case_list_sha256")
    verdict = doc.get("verdict")
    result = {"PASS": "PASS", "FAIL": "FAIL"}.get(verdict, "INFRA_ERROR") if isinstance(verdict, str) else "INFRA_ERROR"
    started = _timestamp(doc.get("started_at"), where="A/B 報告")
    layers = _check_evidence_layers("ab-report", doc, item)
    return Derived(
        result=result,
        artifact_id=artifact_id,
        contract_revision=contract,
        tool_revision=tool,
        case_list_sha256=case_sha,
        workload_sha256=doc.get("workload_sha256") if _is_digest(doc.get("workload_sha256")) else None,
        bundle_tree_sha256=bundle,
        runtime_tree_sha256=runtime,
        xfail_cases=(),
        started_at=started,
        files=(path,),
        ledger_through=None,
        ledger_records=None,
        l1_set_sha256=None,
        l1_verdict=None,
        note=_with_layer_note(f"A/B 報告判定：{result}", layers),
    )


def _derive_canary(report_path: Path, *, item: Any, platform: str) -> Derived:
    path = Path(report_path)
    doc = _read_json(path, where="Canary 報告")
    if doc.get("platform") != platform:
        raise _invalid("Canary 報告 platform 與指定平台不符")
    artifact_id = doc.get("artifact_id")
    if not _is_digest(artifact_id):
        raise _invalid("Canary 報告缺少有效 artifact_id")
    raw_result = doc.get("result")
    if not isinstance(raw_result, str) or raw_result not in {
        "PASS", "FAIL", "INCOMPLETE", "POLICY_NOT_FROZEN", "POLICY_CHANGED"
    }:
        raise _invalid(f"Canary 報告 result 無效：{raw_result!r}")
    bundle = doc.get("bundle_tree_sha256")
    runtime = doc.get("runtime_tree_sha256")
    if not _is_digest(bundle) or not _is_digest(runtime):
        raise _invalid("Canary 報告缺少有效 bundle_tree_sha256／runtime_tree_sha256")
    ledger_through = doc.get("ledger_through")
    ledger_records = doc.get("ledger_records")
    if not isinstance(ledger_through, str) or not isinstance(ledger_records, int) or isinstance(ledger_records, bool):
        raise _invalid("Canary 報告缺少有效 ledger_through／ledger_records")
    if ledger_records < 0:
        raise _invalid("Canary 報告 ledger_records 不可為負")
    ledger_through = _timestamp(ledger_through, where="Canary 報告 ledger_through")
    tally = doc.get("tally")
    if not isinstance(tally, Mapping):
        raise _invalid("Canary 報告缺少 tally")
    if getattr(item, "item_id", "") == "S3-INCIDENTS-CLOSED":
        open_incidents = tally.get("open_incidents")
        failing_incidents = tally.get("failing_incidents")
        if not isinstance(open_incidents, list) or not isinstance(failing_incidents, list):
            raise _invalid("Canary tally 缺少 open_incidents／failing_incidents")
        if failing_incidents:
            result = "FAIL"
        elif not open_incidents and not (
            raw_result in {"POLICY_NOT_FROZEN", "POLICY_CHANGED"}
        ):
            result = "PASS"
        else:
            result = "INSUFFICIENT"
    else:
        if raw_result == "PASS":
            result = "PASS"
        elif raw_result == "FAIL":
            result = "FAIL"
        else:
            result = "INSUFFICIENT"
    first_counted = tally.get("first_counted_wall")
    if first_counted is None:
        started = ledger_through
    else:
        started = _timestamp(first_counted, where="Canary tally first_counted_wall")
    layers = _check_evidence_layers("canary-report", doc, item)
    return Derived(
        result=result,
        artifact_id=artifact_id,
        contract_revision=None,
        tool_revision=None,
        case_list_sha256=None,
        workload_sha256=None,
        bundle_tree_sha256=bundle,
        runtime_tree_sha256=runtime,
        xfail_cases=(),
        started_at=started,
        files=(path,),
        ledger_through=ledger_through,
        ledger_records=ledger_records,
        l1_set_sha256=None,
        l1_verdict=None,
        note=_with_layer_note(f"Canary 帳本判定：{result}", layers),
        detail=tuple(str(r) for r in doc.get("reasons", []) if isinstance(r, str)),
    )


def _derive_drill(report_path: Path, *, item: Any) -> Derived:
    path = Path(report_path)
    doc = _read_json(path, where="回滾演練報告")
    artifact_id = doc.get("current_release_manifest_sha256")
    bundle = doc.get("current_bundle_tree_sha256")
    runtime = doc.get("current_runtime_tree_sha256")
    if not _is_digest(artifact_id):
        raise _invalid("回滾演練報告缺少有效 current_release_manifest_sha256")
    if not _is_digest(bundle) or not _is_digest(runtime):
        raise _invalid("回滾演練報告缺少有效 current_bundle_tree_sha256／current_runtime_tree_sha256")
    mapping = {"PASS": "PASS", "FAIL": "FAIL", "PRECONDITION_FAILED": "INSUFFICIENT", "INCOMPLETE": "INSUFFICIENT"}
    verdict = doc.get("verdict")
    if not isinstance(verdict, str) or verdict not in mapping:
        raise _invalid(f"回滾演練報告 verdict 無效：{verdict!r}")
    started = _timestamp(doc.get("started_at"), where="回滾演練報告")
    layers = _check_evidence_layers("drill-report", doc, item)
    return Derived(
        result=mapping[verdict],
        artifact_id=artifact_id,
        contract_revision=None,
        tool_revision=None,
        case_list_sha256=None,
        workload_sha256=None,
        bundle_tree_sha256=bundle,
        runtime_tree_sha256=runtime,
        xfail_cases=(),
        started_at=started,
        files=(path,),
        ledger_through=None,
        ledger_records=None,
        l1_set_sha256=None,
        l1_verdict=None,
        note=_with_layer_note(f"回滾演練判定：{mapping[verdict]}", layers),
    )


def _derive_bridge(report_path: Path, *, platform: str) -> Derived:
    path = Path(report_path)
    doc = _read_json(path, where="bridge 自測報告")
    if "qualified" in doc:
        raise _invalid("bridge 自測不得包含人工 qualified 欄位")
    bridge_path = str(_BRIDGE_SRC)
    inserted_path = False
    try:
        if bridge_path not in sys.path:
            sys.path.insert(0, bridge_path)
            inserted_path = True
        runner = importlib.import_module("uart_stdio_bridge.selftest.runner")
        identity = importlib.import_module("uart_stdio_bridge.identity")
        if (not Path(runner.__file__).resolve().is_relative_to(_BRIDGE_SRC.resolve())
                or not Path(identity.__file__).resolve().is_relative_to(_BRIDGE_SRC.resolve())):
            raise _invalid("bridge qualification module is not from this repository")
        approved = runner.load_approved_revision()
        endpoint = "posix-pty" if platform == "linux" else "win-serial"
        platform_os = "Linux" if platform == "linux" else "Windows"
        qualified = runner.is_qualified(
            doc,
            endpoint=endpoint,
            tool_tree_sha256=identity.tool_tree_sha256(),
            platform_os=platform_os,
            approved=approved,
        )
    except HarnessError:
        raise
    except Exception as exc:
        raise _invalid(f"P2 bridge qualification 無法執行：{exc}") from exc
    finally:
        if inserted_path and bridge_path in sys.path:
            sys.path.remove(bridge_path)
    result = "PASS" if qualified else "FAIL"
    started = _started_at("bridge-selftest", doc)
    return Derived(
        result=result,
        artifact_id=None,
        contract_revision=None,
        tool_revision=None,
        case_list_sha256=None,
        workload_sha256=None,
        bundle_tree_sha256=None,
        runtime_tree_sha256=None,
        xfail_cases=(),
        started_at=started,
        files=(path,),
        ledger_through=None,
        ledger_records=None,
        l1_set_sha256=None,
        l1_verdict=None,
        note=f"bridge 自測 qualified={qualified}",
    )


def _derive_od09(
    report_path: Path,
    *,
    index_doc: Mapping[str, Any] | None,
) -> Derived:
    from tools.stage3 import od09_consume

    # fail closed：缺證據索引不得退化為「無候選長跑」而 PASS（與 od09_consume 的 entries 守門一致）。
    if not isinstance(index_doc, Mapping):
        raise _invalid("od09-freeze 推導需要證據索引（index_doc）")
    return od09_consume.derive_freeze_report(
        Path(report_path),
        acceptance_path=_ACCEPTANCE_PATH,
        index=dict(index_doc),
        ledger_path=_LEDGER_PATH,
    )


def _derive_manual(
    report_path: Path,
    *,
    item: Any,
    platform: str,
    release_manifest: Path | None = None,
) -> Derived:
    path = Path(report_path)
    doc = _read_json(path, where="人工程序紀錄")
    if getattr(item, "evidence_kind", None) != "manual-procedure":
        raise _invalid("manual-procedure 只能用於 manual-procedure 出口項目")
    procedure_path_value = doc.get("procedure_path")
    procedure_sha = doc.get("procedure_sha256")
    if not isinstance(procedure_path_value, str) or not procedure_path_value:
        raise _invalid("人工程序紀錄缺少 procedure_path")
    procedure_path = Path(procedure_path_value)
    if not procedure_path.is_absolute():
        procedure_path = path.parent / procedure_path
    if not procedure_path.is_file() or not _is_digest(procedure_sha):
        raise _invalid("人工程序檔案不存在或 procedure_sha256 格式無效")
    try:
        procedure_path = procedure_path.resolve(strict=True)
    except OSError as exc:
        raise _invalid(f"人工程序檔案無法解析：{exc}") from exc
    try:
        procedure_path.relative_to(_PROCEDURES_ROOT.resolve())
    except ValueError as exc:
        raise _invalid("人工程序路徑位於核准 procedures 目錄外") from exc
    if _sha256_file(procedure_path) != procedure_sha:
        raise _invalid("人工程序 procedure_sha256 與現行程序檔不符")
    executed_by = doc.get("executed_by")
    if not isinstance(executed_by, str) or not executed_by.strip():
        raise _invalid("人工程序紀錄缺少 executed_by")
    artifacts = doc.get("artifacts")
    if not isinstance(artifacts, list):
        raise _invalid("人工程序紀錄 artifacts 必須是陣列")
    files: list[Path] = [path, procedure_path]
    for artifact in artifacts:
        if isinstance(artifact, str):
            value = artifact
        elif isinstance(artifact, Mapping):
            value = artifact.get("path")
        else:
            raise _invalid("人工程序 artifacts 含無效項目")
        if not isinstance(value, str) or not value:
            raise _invalid("人工程序 artifact 缺少 path")
        artifact_path = Path(value)
        if not artifact_path.is_absolute():
            artifact_path = path.parent / artifact_path
        if not artifact_path.is_file():
            raise _invalid(f"人工程序 artifact 不存在：{artifact_path.name}")
        files.append(artifact_path)
    bracket = _report_bracket(doc, kind="manual-procedure")
    brackets: list[Mapping[str, Any] | None] = [bracket]
    artifact_id = _bracket_artifact_id(brackets)
    manifest_doc: dict[str, Any] | None = None
    if release_manifest is not None:
        manifest_doc, provided_artifact = _load_manifest(Path(release_manifest))
        if artifact_id is not None and artifact_id != provided_artifact:
            raise _invalid("deployed manifest differs from report artifact")
        artifact_id = provided_artifact
    tree = deployed_tree(
        brackets,
        kind="manual-procedure",
        artifact_id=artifact_id,
        cli_sha256=None,
        manifest_doc=manifest_doc,
    )
    result = doc.get("result")
    if not isinstance(result, str) or result not in {
        "PASS", "FAIL", "SKIP", "XFAIL", "XPASS", "INFRA_ERROR", "INSUFFICIENT"
    }:
        raise _invalid(f"人工程序紀錄 result 無效：{result!r}")
    started = _timestamp(doc.get("executed_at"), where="人工程序紀錄 executed_at")
    layers = _check_evidence_layers("manual-procedure", doc, item)
    return Derived(
        result=result,
        artifact_id=artifact_id,
        contract_revision=None,
        tool_revision=None,
        case_list_sha256=None,
        workload_sha256=None,
        bundle_tree_sha256=tree.bundle_tree_sha256,
        runtime_tree_sha256=tree.runtime_tree_sha256,
        xfail_cases=(),
        started_at=started,
        files=tuple(dict.fromkeys(files)),
        ledger_through=None,
        ledger_records=None,
        l1_set_sha256=None,
        l1_verdict=None,
        note=_with_layer_note(f"人工程序判定：{result}", layers),
    )


def derive(
    kind: str,
    report: Path,
    *,
    item: Any,
    cases: Mapping[str, dict],
    lock: Mapping[str, Any],
    release_manifest: Path | None,
    platform: str,
    l1_lock: Mapping[str, Any] | None = None,
    index_doc: Mapping[str, Any] | None = None,
    now: str | None = None,
) -> Derived:
    """依報告來源推導單一出口證據，不接受操作者自行提供的 result 欄位替代判定。"""
    if kind not in KINDS:
        raise _invalid(f"未知 evidence kind：{kind!r}")
    if kind == "junit":
        return _derive_junit(
            Path(report),
            item=item,
            platform=platform,
            l1_lock=l1_lock,
            release_manifest=release_manifest,
        )
    if kind == "functest-report":
        return _derive_functest(
            Path(report),
            item=item,
            cases=cases,
            lock=lock,
            platform=platform,
            release_manifest=release_manifest,
        )
    if kind == "bridge-selftest":
        return _derive_bridge(Path(report), platform=platform)
    if kind in {"regression-report", "realhw-report", "gate-report"}:
        return _derive_suite_report(
            kind,
            Path(report),
            item=item,
            cases=cases,
            lock=lock,
            release_manifest=release_manifest,
            platform=platform,
            now=now,
        )
    if kind == "ab-report":
        return _derive_ab(Path(report), item=item, platform=platform)
    if kind == "canary-report":
        return _derive_canary(Path(report), item=item, platform=platform)
    if kind == "drill-report":
        return _derive_drill(Path(report), item=item)
    if kind == "od09-freeze":
        return _derive_od09(Path(report), index_doc=index_doc)
    return _derive_manual(
        Path(report), item=item, platform=platform, release_manifest=release_manifest
    )


def _entry_value(entry: Any, key: str) -> Any:
    if isinstance(entry, Mapping):
        return entry.get(key)
    return getattr(entry, key, None)


def _entry_id(index: Mapping[str, Any], entry_id: str) -> Any | None:
    entries = index.get("entries")
    if not isinstance(entries, list):
        raise _invalid("證據索引缺少 entries 陣列")
    for entry in entries:
        if _entry_value(entry, "entry_id") == entry_id:
            return entry
    return None


def _entry_matches_target(entry: Any, target: Mapping[str, Any], *, item: Any) -> bool:
    """判斷既有 entry 是否仍屬於目前 target 的有效集合。"""
    for key in ("artifact_id", "contract_revision", "tool_revision"):
        if _entry_value(entry, key) != target.get(key):
            return False
    if getattr(item, "required_case_set", None) is not None:
        if _entry_value(entry, "case_list_sha256") != target.get("case_list_sha256"):
            return False
    if target.get("bundle_tree_sha256") is not None:
        for key in ("bundle_tree_sha256", "runtime_tree_sha256"):
            value = _entry_value(entry, key)
            if value is None or value != target.get(key):
                return False
    return True


def _ledger_version(entry: Any) -> tuple[datetime, int, str]:
    raw_through = _entry_value(entry, "ledger_through")
    if raw_through is None:
        through = datetime.min.replace(tzinfo=timezone.utc)
    else:
        through = jsonio.parse_iso(raw_through).astimezone(timezone.utc)
    raw_records = _entry_value(entry, "ledger_records")
    records = raw_records if isinstance(raw_records, int) and not isinstance(raw_records, bool) else -1
    entry_id = _entry_value(entry, "entry_id")
    return through, records, entry_id if isinstance(entry_id, str) else ""


def _validate_target(
    derived: Derived,
    *,
    kind: str,
    target: Mapping[str, Any],
    reuse_entry: Any | None,
) -> tuple[str | None, str | None]:
    comparisons = ("contract_revision", "tool_revision", "case_list_sha256")
    if reuse_entry is not None:
        if derived.artifact_id is not None and derived.artifact_id != _entry_value(reuse_entry, "artifact_id"):
            raise _invalid("reuse identity differs: artifact_id")
        mismatches = [
            key
            for key in comparisons
            if getattr(derived, key) is not None and getattr(derived, key) != target.get(key)
        ]
        for key in ("bundle_tree_sha256", "runtime_tree_sha256"):
            value = getattr(derived, key)
            expected = _entry_value(reuse_entry, key)
            if value is not None and value != expected:
                mismatches.append(key)
        if (
            _entry_value(reuse_entry, "bundle_tree_sha256") is not None
            or _entry_value(reuse_entry, "runtime_tree_sha256") is not None
        ):
            if derived.bundle_tree_sha256 is None or derived.runtime_tree_sha256 is None:
                raise _invalid(f"report lacks deployed tree: {kind}")
        if mismatches:
            raise _invalid("reuse identity differs: " + ", ".join(dict.fromkeys(mismatches)))
    else:
        mismatches = [
            key
            for key in comparisons
            if getattr(derived, key) is not None and getattr(derived, key) != target.get(key)
        ]
        if derived.artifact_id is not None and derived.artifact_id != target.get("artifact_id"):
            mismatches.append("artifact_id")
        for key in ("bundle_tree_sha256", "runtime_tree_sha256"):
            value = getattr(derived, key)
            expected = target.get(key)
            if value is not None and expected is not None and value != expected:
                mismatches.append(key)
        if target.get("bundle_tree_sha256") is not None and kind in TREE_REQUIRED_KINDS:
            if derived.bundle_tree_sha256 is None:
                raise _invalid(f"report lacks deployed tree: {kind}")
            if derived.runtime_tree_sha256 is None:
                raise _invalid(f"report lacks deployed tree: {kind}")
        if mismatches:
            raise _invalid("target mismatch: " + ", ".join(dict.fromkeys(mismatches)))
    if kind in TREE_EXEMPT_KINDS:
        return target.get("bundle_tree_sha256"), target.get("runtime_tree_sha256")
    if reuse_entry is not None:
        return target.get("bundle_tree_sha256"), target.get("runtime_tree_sha256")
    return derived.bundle_tree_sha256, derived.runtime_tree_sha256


def _copy_evidence(
    derived: Derived,
    *,
    evidence_root: Path,
    item_id: str,
    platform: str,
    now: str,
    report_root: Path,
) -> tuple[tuple[Any, ...], Path, bool]:
    from tools.stage3 import evidence_index

    sources: list[tuple[Path | None, str]] = []
    seen: set[Path] = set()
    external_number = 0
    report_root = Path(report_root).resolve()
    for source_value in derived.files:
        source = Path(source_value)
        try:
            resolved_source = source.resolve()
        except OSError as exc:
            raise _invalid(f"證據檔案無法解析：{source.name}：{exc}") from exc
        if resolved_source in seen:
            continue
        seen.add(resolved_source)
        expanded = _source_files(source)
        if source.is_dir():
            prefix = resolved_source.name
            for file_path in expanded:
                relative = f"{prefix}/{file_path.relative_to(source).as_posix()}"
                sources.append((file_path, relative))
        else:
            try:
                relative = resolved_source.relative_to(report_root).as_posix()
            except ValueError:
                external_number += 1
                relative = f"external/{external_number}/{resolved_source.name}"
            sources.append((source, relative))
    if derived.l1_verdict is not None:
        sources.append((None, "l1-verdict.json"))  # type: ignore[arg-type]

    normalized_sources: list[tuple[Path | None, str]] = []
    destinations: set[str] = set()
    for source, relative in sources:
        relative_path = Path(relative)
        if relative_path.is_absolute() or not relative_path.parts or ".." in relative_path.parts:
            raise _invalid(f"證據相對路徑無效：{relative!r}")
        normalized = relative_path.as_posix()
        if normalized in destinations:
            raise _invalid(f"證據目的路徑重複：{normalized}")
        destinations.add(normalized)
        normalized_sources.append((source, normalized))

    stamp = now.replace(":", "")
    root = Path(evidence_root)
    parent = root / item_id / platform
    root_existed = root.exists()
    try:
        parent.mkdir(parents=True, exist_ok=True)
    except OSError as exc:
        raise _invalid(f"證據目錄無法建立：{parent}：{exc}") from exc
    destination = parent / stamp
    if destination.exists():
        _remove_empty_parents(parent, root if root_existed else root.parent)
        raise _invalid(f"evidence destination already exists: {item_id}/{platform}/{stamp}")
    try:
        temporary = Path(tempfile.mkdtemp(prefix=".ingest-", dir=str(parent)))
    except OSError as exc:
        _remove_empty_parents(parent, root if root_existed else root.parent)
        raise _invalid(f"證據暫存目錄無法建立：{parent}：{exc}") from exc
    committed = False
    frozen_files: list[Any] = []
    try:
        for source, relative in normalized_sources:
            out = temporary / relative
            out.parent.mkdir(parents=True, exist_ok=True)
            if source is None:
                out.write_bytes(jsonio.dumps_report(dict(derived.l1_verdict or {})).encode("utf-8"))
            else:
                shutil.copyfile(source, out)
            frozen_files.append(
                evidence_index.FrozenFile(
                    path=(Path(item_id) / platform / stamp / relative).as_posix(),
                    sha256=_sha256_file(out),
                )
            )
        os.replace(temporary, destination)
        committed = True
        return tuple(frozen_files), destination, committed
    except OSError as exc:
        shutil.rmtree(temporary, ignore_errors=True)
        if committed:
            shutil.rmtree(destination, ignore_errors=True)
        _remove_empty_parents(parent, root if root_existed else root.parent)
        raise _invalid(f"證據檔案無法保存：{exc}") from exc
    except BaseException:
        shutil.rmtree(temporary, ignore_errors=True)
        if committed:
            shutil.rmtree(destination, ignore_errors=True)
        _remove_empty_parents(parent, root if root_existed else root.parent)
        raise


def _remove_empty_parents(path: Path, stop: Path) -> None:
    current = Path(path)
    stop = Path(stop)
    while current != stop:
        try:
            current.rmdir()
        except OSError:
            break
        current = current.parent


def _entry_record_id(entry: Any) -> str:
    fields = asdict(entry)
    fields.pop("entry_id", None)
    fields["record_id"] = ""
    return jsonio.record_id(fields)


def _load_environment(path: Path) -> dict[str, Any]:
    from tools.stage3 import evidence_index

    try:
        doc = json.loads(Path(path).read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, ValueError) as exc:
        raise _invalid(f"environment 檔案無法讀取：{Path(path).name}：{exc}") from exc
    if not isinstance(doc, dict):
        raise _invalid("environment 頂層必須是 JSON 物件")
    missing = [key for key in evidence_index.ENV_KEYS if key not in doc]
    if missing:
        raise _invalid(f"environment 缺少欄位：{missing}")
    return doc


def ingest(
    index_path: Path,
    *,
    kind: str,
    item_id: str,
    platform: str,
    report: Path,
    evidence_root: Path,
    catalog: Mapping[str, Any],
    cases: Mapping[str, dict],
    lock: Mapping[str, Any],
    environment: Path,
    release_manifest: Path | None = None,
    reuse_of: str | None = None,
    now: str,
    l1_lock: Mapping[str, Any] | None = None,
):
    """推導報告結果、核對 target 身分，再複製檔案並原子登錄 evidence entry。"""
    from tools.stage3 import evidence_index

    if kind not in KINDS:
        raise _invalid(f"未知 evidence kind：{kind!r}")
    item = catalog.get(item_id)
    if item is None:
        raise _invalid(f"未知出口項目：{item_id}")
    if kind != getattr(item, "evidence_kind", None):
        raise _invalid(f"kind 與出口項目不符：{kind} != {getattr(item, 'evidence_kind', None)}")
    item_platform = getattr(item, "platform", None)
    if item_platform not in {platform, "both"}:
        raise _invalid(f"platform 與出口項目不符：{platform}")
    if platform not in {"linux", "windows"}:
        raise _invalid(f"未知 platform：{platform!r}")
    now = _timestamp(now, where="ingest now")

    try:
        index_doc = evidence_index.load_index(Path(index_path))
    except (OSError, ValueError, TypeError, KeyError) as exc:
        raise _invalid(f"證據索引無法讀取：{exc}") from exc
    if not isinstance(index_doc, Mapping):
        raise _invalid("證據索引頂層必須是物件")
    stage = str(getattr(item, "stage", ""))
    stage_targets = index_doc.get("stage_targets")
    if not isinstance(stage_targets, Mapping):
        raise _invalid("證據索引缺少 stage_targets 物件")
    stage_target = stage_targets.get(stage)
    if not isinstance(stage_target, Mapping):
        raise _invalid(f"no target: {stage}/{platform}")
    target = stage_target.get(platform)
    if not isinstance(target, Mapping):
        raise _invalid(f"no target: {stage}/{platform}")

    derived = derive(
        kind,
        Path(report),
        item=item,
        cases=cases,
        lock=lock,
        release_manifest=release_manifest,
        platform=platform,
        l1_lock=l1_lock,
        index_doc=index_doc,
        now=now,
    )
    if derived.artifact_id is None and release_manifest is not None:
        _, provided_artifact = _load_manifest(Path(release_manifest))
        derived = replace(derived, artifact_id=provided_artifact)
    if release_manifest is not None and derived.artifact_id is not None:
        _, provided_artifact = _load_manifest(Path(release_manifest))
        if derived.artifact_id != provided_artifact:
            raise _invalid("report artifact differs from --release-manifest")

    reuse_entry = _entry_id(index_doc, reuse_of) if reuse_of is not None else None
    if reuse_of is not None and reuse_entry is None:
        raise _invalid(f"reuse entry not found: {reuse_of}")
    bundle_tree, runtime_tree = _validate_target(
        derived,
        kind=kind,
        target=target,
        reuse_entry=reuse_entry,
    )

    if kind == "canary-report":
        entries = index_doc.get("entries")
        if not isinstance(entries, list):
            raise _invalid("證據索引缺少 entries 陣列")
        if getattr(item, "evidence_kind", None) != "canary-report":
            raise _invalid("canary-report 與出口項目 evidence_kind 不符")
        existing = [
            entry
            for entry in entries
            if _entry_value(entry, "item_id") == item_id
            and _entry_value(entry, "platform") == platform
            and _entry_matches_target(entry, target, item=item)
        ]
        if existing:
            try:
                latest = max(existing, key=_ledger_version)
                old_version = _ledger_version(latest)[:2]
            except (AttributeError, TypeError, ValueError) as exc:
                raise _invalid(f"既有 canary 帳本版本無效：{exc}") from exc
            new_version = (
                jsonio.parse_iso(derived.ledger_through).astimezone(timezone.utc)
                if derived.ledger_through is not None
                else datetime.min.replace(tzinfo=timezone.utc),
                derived.ledger_records if derived.ledger_records is not None else -1,
            )
            if new_version < old_version:
                raise _invalid(f"canary report older than ingested {_entry_value(latest, 'entry_id')}")

    env_doc = _load_environment(Path(environment))
    try:
        environment_id = evidence_index.environment_id(env_doc)
    except (KeyError, TypeError, ValueError) as exc:
        raise _invalid(f"environment 無法計算識別碼：{exc}") from exc

    evidence_root_path = Path(evidence_root)
    evidence_root_existed = evidence_root_path.exists()
    copied_files: tuple[Any, ...] = ()
    destination: Path | None = None
    try:
        copied_files, destination, _ = _copy_evidence(
            derived,
            evidence_root=evidence_root_path,
            item_id=item_id,
            platform=platform,
            now=now,
            report_root=(Path(report).resolve() if Path(report).is_dir() else Path(report).resolve().parent),
        )
        fields = {
            "entry_id": "",
            "item_id": item_id,
            "platform": platform,
            "result": derived.result,
            "artifact_id": target.get("artifact_id"),
            "runtime_digest": target.get("runtime_digest"),
            "bundle_tree_sha256": bundle_tree,
            "runtime_tree_sha256": runtime_tree,
            "contract_revision": target.get("contract_revision"),
            "tool_revision": target.get("tool_revision"),
            "case_list_sha256": target.get("case_list_sha256"),
            "l1_set_sha256": derived.l1_set_sha256,
            "workload_sha256": derived.workload_sha256,
            "environment_id": environment_id,
            "produced_at": now,
            "started_at": derived.started_at,
            "producer": f"ingest:{kind}",
            "files": copied_files,
            "xfail_cases": tuple(derived.xfail_cases),
            "reuse_of": reuse_of,
            "note": derived.note + ("；" + "；".join(derived.detail) if derived.detail else ""),
            "ledger_through": derived.ledger_through,
            "ledger_records": derived.ledger_records,
        }
        entry = evidence_index.EvidenceEntry(**fields)
        entry = replace(entry, entry_id=_entry_record_id(entry))
        evidence_index.add_entry(Path(index_path), entry, catalog=catalog)
        return entry
    except BaseException:
        if destination is not None:
            shutil.rmtree(destination, ignore_errors=True)
            _remove_empty_parents(
                destination.parent,
                evidence_root_path if evidence_root_existed else evidence_root_path.parent,
            )
        raise

