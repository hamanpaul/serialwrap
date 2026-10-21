"""載入、驗證並核對 Sequential A/B 執行所需的凍結 manifest。"""

from __future__ import annotations

import hashlib
import json
import os
import re
import stat
from dataclasses import dataclass
from pathlib import Path, PurePosixPath, PureWindowsPath
from string import Formatter
from typing import Any, Mapping, Sequence

import yaml

from tools.stage3.jsonio import dumps_compact, sha256_file
from tools.stage3.proc import Runner, run_cmd
from tools.stage3.reason_codes import HarnessError

LAYER_ADAPTER: Mapping[str, str] = {"l1.5": "l15_functest", "l2": "l2_regression", "l3": "l3_realhw"}
TEMPLATE_KEYS = frozenset({"cli_exe", "daemon_exe", "bundle_dir", "report_dir", "instance_dir", "seed",
                           "side", "round", "out_dir", "repo_root", "endpoint"})
_INVARIANTS = ("zero_tx_on_reject", "no_auto_resend", "no_forbidden_interleave",
               "no_stale_context_replay", "no_reopen_before_handoff", "insufficient_evidence_not_pass")
_STAGES = {"1A", "1B", "2.1", "2.2", "2.3", "2.4", "3"}
_PLATFORMS = {"linux", "windows"}


@dataclass(frozen=True)
class FrozenFile:
    path: str
    sha256: str


@dataclass(frozen=True)
class FrozenTree:
    path: str
    tree_sha256: str


@dataclass(frozen=True)
class DutSpec:
    kind: str
    transport: str
    com_pair: Mapping[str, str] | None
    image: str | None
    image_digest: str | None
    boards: tuple[str, ...]
    reset_argv: tuple[str, ...]
    fingerprint_argv: tuple[str, ...]
    expected_fingerprint: Mapping[str, str]


@dataclass(frozen=True)
class Authorization:
    allow_destructive: bool
    decision_ref: str | None
    boards: tuple[str, ...]


@dataclass(frozen=True)
class SuiteSpec:
    adapter: str
    timeout_s: float
    selftest_report: FrozenFile | None
    testbed_template: FrozenFile | None
    realhw_config_template: FrozenFile | None
    tier: str | None
    extra_args: tuple[str, ...]


@dataclass(frozen=True)
class ReleaseSpec:
    timeout_s: float
    poll_s: float
    endpoints: tuple[str, ...]
    device_paths: tuple[str, ...]
    lock_paths: tuple[str, ...]
    device_open_probe: bool
    accept_unreadable_holders: bool


@dataclass(frozen=True)
class ReverifySpec:
    enabled: bool
    order: tuple[str, ...]


@dataclass(frozen=True)
class AbSide:
    label: str
    role: str
    bundle_dir: Path
    release_manifest: FrozenFile
    cli_exe: Path
    daemon_exe: Path
    activate_argv: tuple[str, ...]
    deactivate_argv: tuple[str, ...]
    manifest_doc: Mapping[str, Any]


@dataclass(frozen=True)
class AbManifest:
    schema_version: int
    run_id: str
    layer: str
    platform: str
    stage: str
    contract_revision: str
    case_list: FrozenFile
    case_lock: FrozenFile
    required_case_set: str
    case_ids: tuple[str, ...]
    tools: tuple[FrozenFile | FrozenTree, ...]
    allowed_differences: FrozenFile
    acceptance: FrozenFile | None
    profile_files: tuple[FrozenFile, ...]
    seed: int
    platform_settings: Mapping[str, Any]
    dut: DutSpec
    authorization: Authorization | None
    test_device_allowlist: tuple[str, ...]
    sides: tuple[AbSide, AbSide]
    suite: SuiteSpec
    endpoint: str | None
    release: ReleaseSpec
    reverify: ReverifySpec
    failure_retention: str | None
    source_path: Path
    source_sha256: str


def _invalid(detail: str) -> HarnessError:
    return HarnessError("HARNESS_MANIFEST_INVALID", detail)


def _mapping(value: Any, where: str) -> Mapping[str, Any]:
    if not isinstance(value, dict):
        raise _invalid(f"{where} must be an object")
    return value


def _string(value: Any, where: str, *, allow_empty: bool = False) -> str:
    if not isinstance(value, str) or (not allow_empty and not value):
        raise _invalid(f"{where} must be a string")
    return value


def _number(value: Any, where: str) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)) or value <= 0:
        raise _invalid(f"{where} must be a positive number")
    return float(value)


def _strings(value: Any, where: str) -> tuple[str, ...]:
    if not isinstance(value, list) or any(not isinstance(item, str) for item in value):
        raise _invalid(f"{where} must be an array of strings")
    return tuple(value)


def _frozen(value: Any, where: str, *, tree_ok: bool = False) -> FrozenFile | FrozenTree:
    obj = _mapping(value, where)
    path = _string(obj.get("path"), f"{where}.path")
    if tree_ok and isinstance(obj.get("tree_sha256"), str):
        digest = obj["tree_sha256"]
        if not re.fullmatch(r"[0-9a-f]{64}", digest):
            raise _invalid(f"{where}.tree_sha256 must be lowercase SHA-256")
        return FrozenTree(path, digest)
    digest = _string(obj.get("sha256"), f"{where}.sha256")
    if not re.fullmatch(r"[0-9a-f]{64}", digest):
        raise _invalid(f"{where}.sha256 must be lowercase SHA-256")
    return FrozenFile(path, digest)


def _path(root: Path, value: str) -> Path:
    path = Path(value)
    return path if path.is_absolute() else root / path


def _inside(root: Path, candidate: Path) -> bool:
    try:
        candidate.resolve().relative_to(root.resolve())
    except (OSError, ValueError):
        return False
    return True


def _release_component_path(bundle: Path, doc: Mapping[str, Any], *, daemon: bool) -> Path | None:
    components = doc.get("components")
    if not isinstance(components, dict):
        return None
    component = components.get("daemon" if daemon else "cli")
    if not isinstance(component, dict):
        return None
    path: Any = None
    if daemon:
        launcher = component.get("launcher")
        if isinstance(launcher, dict):
            path = launcher.get("path")
        if path is None:
            runtime = component.get("runtime")
            if isinstance(runtime, dict):
                path = runtime.get("path")
        if path is None:
            entry = component.get("entry")
            if isinstance(entry, list) and entry and isinstance(entry[0], str):
                path = entry[0]
    else:
        path = component.get("path")
    if not isinstance(path, str) or not path:
        return None
    return _path(bundle, path)


def load_manifest(path: Path) -> AbManifest:
    """讀取 A/B manifest 並檢查結構、階段與兩側 artifact 對應。"""
    source = Path(path).resolve()
    try:
        doc = json.loads(source.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise _invalid(f"cannot read manifest: {exc}") from exc
    root = _mapping(doc, "manifest")
    if root.get("schema_version") != 1:
        raise _invalid("schema_version must be 1")
    run_id = _string(root.get("run_id"), "run_id")
    layer = _string(root.get("layer"), "layer")
    platform = _string(root.get("platform"), "platform")
    stage = _string(root.get("stage"), "stage")
    contract_revision = _string(root.get("contract_revision"), "contract_revision")
    if not re.fullmatch(r"[0-9a-f]{40}", contract_revision):
        raise _invalid("contract_revision must be 40 lowercase hex characters")
    if layer not in LAYER_ADAPTER or platform not in _PLATFORMS or stage not in _STAGES:
        raise _invalid("unsupported layer, platform, or stage")

    case_list = _frozen(root.get("case_list"), "case_list")
    case_lock = _frozen(root.get("case_lock"), "case_lock")
    allowed_differences = _frozen(root.get("allowed_differences"), "allowed_differences")
    required_case_set = _string(root.get("required_case_set"), "required_case_set")
    case_ids = _strings(root.get("case_ids"), "case_ids")
    if not case_ids or len(set(case_ids)) != len(case_ids):
        raise _invalid("case_ids must be non-empty and unique")
    raw_tools = root.get("tools")
    if not isinstance(raw_tools, list) or not raw_tools:
        raise _invalid("tools must be a non-empty array")
    tools = tuple(_frozen(item, f"tools[{i}]", tree_ok=True) for i, item in enumerate(raw_tools))
    acceptance_value = root.get("acceptance")
    acceptance = None if acceptance_value is None else _frozen(acceptance_value, "acceptance")
    if isinstance(acceptance, FrozenTree):
        raise _invalid("acceptance must be a frozen file")
    profile_files = tuple(_frozen(item, f"profile_files[{i}]") for i, item in enumerate(root.get("profile_files", [])))

    dut_doc = _mapping(root.get("dut"), "dut")
    pair = dut_doc.get("com_pair")
    if pair is not None:
        pair = _mapping(pair, "dut.com_pair")
        if any(not isinstance(k, str) or not isinstance(v, str) for k, v in pair.items()):
            raise _invalid("dut.com_pair must map strings to strings")
    image = dut_doc.get("image")
    image_digest = dut_doc.get("image_digest")
    if image is not None and not isinstance(image, str):
        raise _invalid("dut.image must be a string or null")
    if image_digest is not None and not isinstance(image_digest, str):
        raise _invalid("dut.image_digest must be a string or null")
    expected_fp = _mapping(dut_doc.get("expected_fingerprint"), "dut.expected_fingerprint")
    if any(not isinstance(k, str) or not isinstance(v, str) for k, v in expected_fp.items()):
        raise _invalid("dut.expected_fingerprint must map strings to strings")
    dut = DutSpec(
        kind=_string(dut_doc.get("kind"), "dut.kind"),
        transport=_string(dut_doc.get("transport"), "dut.transport"),
        com_pair=pair,
        image=image,
        image_digest=image_digest,
        boards=_strings(dut_doc.get("boards"), "dut.boards"),
        reset_argv=_strings(dut_doc.get("reset_argv"), "dut.reset_argv"),
        fingerprint_argv=_strings(dut_doc.get("fingerprint_argv"), "dut.fingerprint_argv"),
        expected_fingerprint=expected_fp,
    )

    auth_doc = root.get("authorization")
    authorization = None
    if auth_doc is not None:
        auth_doc = _mapping(auth_doc, "authorization")
        decision = auth_doc.get("decision_ref")
        if decision is not None and not isinstance(decision, str):
            raise _invalid("authorization.decision_ref must be a string or null")
        if not isinstance(auth_doc.get("allow_destructive"), bool):
            raise _invalid("authorization.allow_destructive must be boolean")
        authorization = Authorization(auth_doc["allow_destructive"], decision,
                                      _strings(auth_doc.get("boards"), "authorization.boards"))
    allowlist = _strings(root.get("test_device_allowlist"), "test_device_allowlist")

    sides_raw = root.get("sides")
    if not isinstance(sides_raw, list) or len(sides_raw) != 2:
        raise _invalid("sides must contain exactly A and B")
    sides: list[AbSide] = []
    for i, side_value in enumerate(sides_raw):
        side = _mapping(side_value, f"sides[{i}]")
        label = "A" if i == 0 else "B"
        role = "baseline" if i == 0 else "candidate"
        if side.get("label") != label or side.get("role") != role:
            raise _invalid(f"side {label} must have role {role}")
        bundle = _path(source.parent, _string(side.get("bundle_dir"), f"sides[{i}].bundle_dir")).resolve()
        manifest = _frozen(side.get("release_manifest"), f"sides[{i}].release_manifest")
        if isinstance(manifest, FrozenTree):
            raise _invalid("release_manifest must be a frozen file")
        manifest_path = _path(source.parent, manifest.path)
        try:
            manifest_doc = json.loads(manifest_path.read_text(encoding="utf-8"))
        except (OSError, UnicodeError, json.JSONDecodeError) as exc:
            raise _invalid(f"side {label} release manifest unreadable: {exc}") from exc
        manifest_doc = _mapping(manifest_doc, f"side {label} release manifest")
        cli_exe = _path(source.parent, _string(side.get("cli_exe"), f"sides[{i}].cli_exe")).resolve()
        daemon_exe = _path(source.parent, _string(side.get("daemon_exe"), f"sides[{i}].daemon_exe")).resolve()
        expected_cli = _release_component_path(bundle, manifest_doc, daemon=False)
        expected_daemon = _release_component_path(bundle, manifest_doc, daemon=True)
        if expected_cli is None or cli_exe != expected_cli.resolve():
            raise _invalid(f"side {label} cli_exe is not the declared CLI component")
        if expected_daemon is None or daemon_exe != expected_daemon.resolve():
            raise _invalid(f"side {label} daemon_exe is not the declared daemon component")
        sides.append(AbSide(label, role, bundle, manifest, cli_exe, daemon_exe,
                            _strings(side.get("activate_argv"), f"sides[{i}].activate_argv"),
                            _strings(side.get("deactivate_argv"), f"sides[{i}].deactivate_argv"), manifest_doc))

    suite_doc = _mapping(root.get("suite"), "suite")
    def suite_file(key: str) -> FrozenFile | None:
        value = suite_doc.get(key)
        if value is None:
            return None
        item = _frozen(value, f"suite.{key}")
        if isinstance(item, FrozenTree):
            raise _invalid(f"suite.{key} must be a frozen file")
        return item
    tier = suite_doc.get("tier")
    if tier is not None and not isinstance(tier, str):
        raise _invalid("suite.tier must be a string or null")
    suite = SuiteSpec(_string(suite_doc.get("adapter"), "suite.adapter"), _number(suite_doc.get("timeout_s"), "suite.timeout_s"),
                      suite_file("selftest_report"), suite_file("testbed_template"), suite_file("realhw_config_template"),
                      tier, _strings(suite_doc.get("extra_args"), "suite.extra_args"))

    release_doc = _mapping(root.get("release"), "release")
    release = ReleaseSpec(_number(release_doc.get("timeout_s"), "release.timeout_s"),
                          _number(release_doc.get("poll_s"), "release.poll_s"),
                          _strings(release_doc.get("endpoints"), "release.endpoints"),
                          _strings(release_doc.get("device_paths"), "release.device_paths"),
                          _strings(release_doc.get("lock_paths"), "release.lock_paths"),
                          bool(release_doc.get("device_open_probe")),
                          bool(release_doc.get("accept_unreadable_holders")))
    reverify_doc = _mapping(root.get("reverify"), "reverify")
    if not isinstance(reverify_doc.get("enabled"), bool):
        raise _invalid("reverify.enabled must be boolean")
    reverify = ReverifySpec(reverify_doc["enabled"], _strings(reverify_doc.get("order"), "reverify.order"))

    endpoint = root.get("endpoint")
    failure_retention = root.get("failure_retention")
    if endpoint is not None and not isinstance(endpoint, str):
        raise _invalid("endpoint must be a string or null")
    if failure_retention is not None and not isinstance(failure_retention, str):
        raise _invalid("failure_retention must be a string or null")
    if suite.adapter != LAYER_ADAPTER[layer]:
        raise _invalid("suite adapter does not match layer")
    if stage == "3" and required_case_set != "slice_2.4":
        raise _invalid("Stage 3 requires slice_2.4 case set")
    if layer == "l1.5":
        if dut.kind not in {"local-process", "docker", "physical-substitute"} or suite.selftest_report is None:
            raise _invalid("L1.5 requires a virtual DUT and selftest report")
    else:
        if dut.kind != "hardware" or not dut.boards or not all(side.activate_argv and side.deactivate_argv for side in sides):
            raise _invalid("L2/L3 requires hardware, boards, activate and deactivate argv")
    if dut.kind == "docker" and not dut.image_digest:
        raise _invalid("docker DUT requires image_digest")
    if dut.transport == "com0com-pair" and dut.com_pair is None:
        raise _invalid("com0com-pair requires com_pair")
    if layer == "l2" and suite.testbed_template is None:
        raise _invalid("L2 requires testbed_template")
    if layer == "l3" and (acceptance is None or endpoint is None or suite.realhw_config_template is None
                           or suite.tier is None or failure_retention != "on-demand"):
        raise _invalid("L3 requires acceptance, endpoint, config template, tier, and on-demand failure retention")
    if reverify.enabled and reverify.order != ("B", "A"):
        raise _invalid("reverify order must be B then A")
    if stage in {"1A", "1B"}:
        if _impl(sides[0].manifest_doc, "daemon") != "python" or _impl(sides[1].manifest_doc, "daemon") != "python":
            raise _invalid("Stage 1 requires Python daemons on both sides")
        if _impl(sides[0].manifest_doc, "cli") != "python" or _impl(sides[1].manifest_doc, "cli") != "rust":
            raise _invalid("Stage 1 requires Python CLI on A and Rust CLI on B")
    else:
        if _impl(sides[0].manifest_doc, "daemon") != "python" or _impl(sides[1].manifest_doc, "daemon") != "rust":
            raise _invalid("A/B requires Python daemon on A and Rust daemon on B")
        if _impl(sides[0].manifest_doc, "cli") != _impl(sides[1].manifest_doc, "cli"):
            raise _invalid("Stage 2+ CLI implementation must match")
        if _component(sides[0].manifest_doc, "cli").get("sha256") != _component(sides[1].manifest_doc, "cli").get("sha256"):
            raise _invalid("Stage 2+ CLI sha256 must match")
    if stage == "3":
        candidate = sides[1].manifest_doc
        if candidate.get("stage") != "3" or candidate.get("retirement") != {"python_daemon": "removed"}:
            raise _invalid("Stage 3 candidate requires retirement marker")
    if not _valid_templates(sides, suite):
        raise _invalid("unknown template key")

    return AbManifest(
        1, run_id, layer, platform, stage, contract_revision, case_list, case_lock, required_case_set,
        case_ids, tools, allowed_differences, acceptance, tuple(profile_files),
        int(root.get("seed", 0)), _mapping(root.get("platform_settings"), "platform_settings"), dut,
        authorization, allowlist, (sides[0], sides[1]), suite, endpoint, release, reverify,
        failure_retention, source, sha256_file(source),
    )


def _components(manifest: Mapping[str, Any]) -> Mapping[str, Any]:
    value = manifest.get("components")
    return value if isinstance(value, dict) else {}


def _component(manifest: Mapping[str, Any], name: str) -> Mapping[str, Any]:
    value = _components(manifest).get(name)
    return value if isinstance(value, dict) else {}


def _impl(manifest: Mapping[str, Any], name: str) -> Any:
    return _component(manifest, name).get("implementation")


def _valid_templates(sides: Sequence[AbSide], suite: SuiteSpec) -> bool:
    values = [*suite.extra_args]
    for side in sides:
        values.extend(side.activate_argv)
        values.extend(side.deactivate_argv)
    for value in values:
        for _, field, _, _ in Formatter().parse(value):
            if field is not None and field.split(".", 1)[0].split("[", 1)[0] not in TEMPLATE_KEYS:
                return False
    return True


def _tree_sha256(root: Path, *, runtime: bool = False) -> str:
    root = Path(root)
    rows: list[str] = []
    if not root.is_dir():
        raise OSError(f"tree directory missing: {root}")
    for current, dirs, names in os.walk(root, topdown=True, followlinks=False):
        current_path = Path(current)
        for name in list(dirs):
            path = current_path / name
            mode = path.lstat().st_mode
            if not stat.S_ISDIR(mode):
                raise HarnessError("HARNESS_ARTIFACT_MISMATCH", f"non-regular file: {path.relative_to(root).as_posix()}")
        for name in names:
            path = current_path / name
            rel = path.relative_to(root).as_posix()
            mode = path.lstat().st_mode
            if not stat.S_ISREG(mode):
                raise HarnessError("HARNESS_ARTIFACT_MISMATCH", f"non-regular file: {rel}")
            if runtime and rel in {"release-manifest.json", "SHA256SUMS"}:
                continue
            if not runtime and ("__pycache__" in Path(rel).parts or path.suffix == ".pyc"):
                continue
            rows.append(f"{rel}\0{sha256_file(path)}\n")
    return hashlib.sha256("".join(sorted(rows)).encode("utf-8")).hexdigest()


def _bundle_tree(root: Path, *, runtime: bool = False) -> str:
    """release bundle digest: every regular file is included; metadata omission is runtime-only."""
    root = Path(root)
    rows: list[str] = []
    if not root.is_dir():
        raise OSError(f"bundle directory missing: {root}")
    for current, dirs, names in os.walk(root, topdown=True, followlinks=False):
        current_path = Path(current)
        for name in list(dirs):
            path = current_path / name
            rel = path.relative_to(root).as_posix()
            if not stat.S_ISDIR(path.lstat().st_mode):
                raise HarnessError("HARNESS_ARTIFACT_MISMATCH", f"non-regular file: {rel}")
        for name in names:
            path = current_path / name
            rel = path.relative_to(root).as_posix()
            if not stat.S_ISREG(path.lstat().st_mode):
                raise HarnessError("HARNESS_ARTIFACT_MISMATCH", f"non-regular file: {rel}")
            if runtime and rel in {"release-manifest.json", "SHA256SUMS"}:
                continue
            rows.append(f"{rel}\0{sha256_file(path)}\n")
    rows.sort(key=lambda row: row.split("\0", 1)[0].encode("utf-8"))
    return hashlib.sha256("".join(rows).encode("utf-8")).hexdigest()


def bundle_tree_sha256(bundle_dir: Path) -> str:
    return _bundle_tree(Path(bundle_dir))


def runtime_tree_sha256(bundle_dir: Path) -> str:
    return _bundle_tree(Path(bundle_dir), runtime=True)


def _package_root(bundle: Path, components: Mapping[str, Any], component: Mapping[str, Any]) -> Path | None:
    runtime = components.get("daemon")
    if isinstance(runtime, dict):
        runtime_doc = runtime.get("runtime")
        if isinstance(runtime_doc, dict) and isinstance(runtime_doc.get("path"), str):
            return _path(bundle, runtime_doc["path"]).parent
    entry = component.get("entry")
    if isinstance(entry, list) and entry and isinstance(entry[0], str):
        if "python_cli_delegate" in components and component is components.get("python_cli_delegate"):
            return bundle / "py"
        return _path(bundle, entry[0]).parent
    return None


def runtime_digest(manifest_doc: Mapping[str, Any]) -> str:
    components = _components(manifest_doc)
    cli = components.get("cli") if isinstance(components.get("cli"), dict) else {}
    daemon = components.get("daemon") if isinstance(components.get("daemon"), dict) else {}
    launcher = daemon.get("launcher") if isinstance(daemon.get("launcher"), dict) else {}
    runtime = daemon.get("runtime") if isinstance(daemon.get("runtime"), dict) else {}
    package = daemon.get("package") if isinstance(daemon.get("package"), dict) else {}
    assets = components.get("assets")
    supervisor = components.get("supervisor") if isinstance(components.get("supervisor"), dict) else {}
    identity = {
        "cli": cli.get("sha256"),
        "daemon_impl": daemon.get("implementation"),
        "daemon": launcher.get("sha256"),
        "runtime": runtime.get("sha256"),
        "package_tree": package.get("tree_sha256"),
        "assets": assets,
        "supervisor": supervisor.get("definition_sha256"),
    }
    return hashlib.sha256(dumps_compact(identity).encode("utf-8")).hexdigest()


def _declared_files(value: Any, path: tuple[str, ...] = ()) -> list[tuple[str, Mapping[str, Any]]]:
    found: list[tuple[str, Mapping[str, Any]]] = []
    if isinstance(value, dict):
        if isinstance(value.get("path"), str) and isinstance(value.get("sha256"), str):
            found.append((".".join(path), value))
        else:
            for key, child in value.items():
                found.extend(_declared_files(child, path + (key,)))
    elif isinstance(value, list):
        for i, child in enumerate(value):
            found.extend(_declared_files(child, path + (str(i),)))
    return found


def verify_bundle(bundle_dir: Path, manifest_doc: Mapping[str, Any], *, label: str) -> list[str]:
    bundle = Path(bundle_dir).resolve()
    components = _components(manifest_doc)
    violations: list[str] = []
    for component, obj in _declared_files(components):
        raw_path = obj["path"]
        pure = PurePosixPath(raw_path)
        win = PureWindowsPath(raw_path)
        if pure.is_absolute() or win.is_absolute() or ".." in pure.parts or "\\" in raw_path:
            violations.append(f"release component path escapes bundle: {label} {component}")
            continue
        path = bundle.joinpath(*pure.parts)
        if not _inside(bundle, path):
            violations.append(f"release component path escapes bundle: {label} {component}")
            continue
        try:
            mode = path.lstat().st_mode
        except OSError:
            violations.append(f"file missing: {path}")
            continue
        if not stat.S_ISREG(mode):
            violations.append(f"release component non-regular file: {label} {component}")
            continue
        try:
            actual = sha256_file(path)
        except OSError:
            violations.append(f"file missing: {path}")
            continue
        if actual != obj["sha256"]:
            violations.append(f"release component sha256 mismatch: {label} {component}")

    daemon = components.get("daemon") if isinstance(components.get("daemon"), dict) else {}
    package = daemon.get("package") if isinstance(daemon.get("package"), dict) else {}
    delegate = components.get("python_cli_delegate") if isinstance(components.get("python_cli_delegate"), dict) else {}
    for component, definition in (("daemon.package", package), ("python_cli_delegate", delegate)):
        expected = definition.get("tree_sha256")
        if not isinstance(expected, str):
            continue
        root = _package_root(bundle, components, definition)
        if root is None:
            violations.append(f"release tree mismatch: {label} {component}")
            continue
        try:
            actual = _tree_sha256(root)
        except (OSError, HarnessError):
            violations.append(f"release tree mismatch: {label} {component}")
            continue
        if actual != expected:
            violations.append(f"release tree mismatch: {label} {component}")
    return violations


def _read_cases(repo_root: Path, m: AbManifest) -> tuple[list[dict], Mapping[str, Any]]:
    try:
        cases_doc = yaml.safe_load(_path(repo_root, m.case_list.path).read_text(encoding="utf-8"))
        lock = json.loads(_path(repo_root, m.case_lock.path).read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError, yaml.YAMLError) as exc:
        raise _invalid(f"cannot read frozen cases: {exc}") from exc
    if not isinstance(cases_doc, dict) or not isinstance(cases_doc.get("cases"), list):
        raise _invalid("cases.yaml must contain cases list")
    if not isinstance(lock, dict) or not isinstance(lock.get("sets"), dict):
        raise _invalid("cases.lock.json must contain sets")
    return cases_doc["cases"], lock["sets"]


def _case_layer_matches(row: Mapping[str, Any], layer: str) -> bool:
    value = row.get("layer")
    return isinstance(value, str) and value.casefold() == layer.casefold()


def required_cases(m: AbManifest, *, repo_root: Path) -> tuple[set[str], dict[str, dict]]:
    rows, sets = _read_cases(repo_root, m)
    if m.required_case_set not in sets or not isinstance(sets[m.required_case_set], list):
        raise _invalid(f"unknown required case set: {m.required_case_set}")
    by_id = {row.get("case_id"): row for row in rows if isinstance(row, dict) and isinstance(row.get("case_id"), str)}
    required: set[str] = set()
    for case_id in sets[m.required_case_set]:
        row = by_id.get(case_id)
        if not isinstance(row, dict):
            continue
        platforms = row.get("platforms")
        if _case_layer_matches(row, m.layer) and isinstance(platforms, list) and m.platform in platforms:
            required.add(case_id)
    return required, by_id


def verify_case_coverage(m: AbManifest, *, repo_root: Path) -> list[str]:
    rows, _ = _read_cases(repo_root, m)
    by_id = {row.get("case_id"): row for row in rows if isinstance(row, dict) and isinstance(row.get("case_id"), str)}
    required, _ = required_cases(m, repo_root=repo_root)
    selected = set(m.case_ids)
    violations = [f"required case missing: {case_id}" for case_id in sorted(required - selected)]
    violations.extend(f"unknown case: {case_id}" for case_id in sorted(selected - by_id.keys()))

    case_doc = yaml.safe_load(_path(repo_root, m.case_list.path).read_text(encoding="utf-8"))
    declared = case_doc.get("invariant_ids", []) if isinstance(case_doc, dict) else []
    for invariant in _INVARIANTS:
        if invariant not in declared:
            continue
        covered = any(
            case_id in by_id
            and _case_layer_matches(by_id[case_id], m.layer)
            and isinstance(by_id[case_id].get("platforms"), list)
            and m.platform in by_id[case_id]["platforms"]
            and invariant in (by_id[case_id].get("invariants") or [])
            for case_id in selected
        )
        exists_for_layer = any(
            isinstance(row, dict) and _case_layer_matches(row, m.layer)
            and invariant in (row.get("invariants") or [])
            for row in rows
        )
        if exists_for_layer and not covered:
            violations.append(f"invariant not covered: {invariant}")

    auth = m.authorization
    for case_id in sorted(selected):
        row = by_id.get(case_id)
        if (not isinstance(row, dict) or not _case_layer_matches(row, m.layer)
                or not isinstance(row.get("platforms"), list) or m.platform not in row["platforms"]
                or row.get("destructive") is not True):
            continue
        if auth is None or not auth.allow_destructive or not auth.decision_ref:
            violations.append(f"destructive case without authorization: {case_id}")
    if auth is not None:
        violations.extend(f"authorization board not in dut: {board}" for board in auth.boards if board not in m.dut.boards)
    if m.platform == "windows" and m.dut.transport == "com0com-pair" and not m.test_device_allowlist:
        violations.append("windows com0com instance without test_device_allowlist")
    return violations


def verify_frozen(m: AbManifest, *, repo_root: Path, run: Runner = run_cmd) -> list[str]:
    """重算所有凍結輸入、bundle 元件及 case coverage。"""
    root = Path(repo_root)
    violations: list[str] = []

    def check_file(item: FrozenFile, prefix: str) -> None:
        path = _path(root, item.path)
        try:
            digest = sha256_file(path)
        except OSError:
            violations.append(f"file missing: {path}")
            return
        if digest != item.sha256:
            violations.append(f"{prefix} sha256 mismatch: {item.path}")

    check_file(m.case_list, "case_list")
    check_file(m.case_lock, "case_lock")
    check_file(m.allowed_differences, "allowed_differences")
    if m.acceptance is not None:
        check_file(m.acceptance, "acceptance")
    for item in m.profile_files:
        check_file(item, "profile")
    for item in (m.suite.selftest_report, m.suite.testbed_template, m.suite.realhw_config_template):
        if item is not None:
            check_file(item, "suite input")
    for item in m.tools:
        path = _path(root, item.path)
        if isinstance(item, FrozenFile):
            check_file(item, "tool")
        else:
            try:
                actual = _tree_sha256(path)
            except (OSError, HarnessError):
                actual = None
            if actual != item.tree_sha256:
                violations.append(f"tool tree mismatch: {item.path}")

    for side in m.sides:
        manifest_path = _path(root, side.release_manifest.path)
        try:
            if sha256_file(manifest_path) != side.release_manifest.sha256:
                violations.append(f"release manifest sha256 mismatch: side {side.label}")
        except OSError:
            violations.append(f"file missing: {manifest_path}")
        violations.extend(verify_bundle(side.bundle_dir, side.manifest_doc, label=f"side {side.label}"))

    result = run(["git", "-C", str(root), "rev-parse", "HEAD:contracts"])
    if result.rc != 0 or result.timed_out:
        violations.append(f"contract_revision unavailable: repo {m.contract_revision}")
    else:
        actual = result.stdout.strip()
        if actual != m.contract_revision:
            violations.append(f"contract_revision mismatch: manifest {m.contract_revision} repo {actual}")

    if m.dut.kind == "docker" and m.dut.image and m.dut.image_digest:
        result = run(["docker", "image", "inspect", "--format", "{{.Id}}", m.dut.image])
        if result.rc != 0 or result.timed_out or result.stdout.strip() != m.dut.image_digest:
            violations.append("dut image digest mismatch")
    violations.extend(verify_case_coverage(m, repo_root=root))
    return violations


def render_argv(template: Sequence[str], values: Mapping[str, str]) -> list[str]:
    rendered: list[str] = []
    for value in template:
        try:
            fields = [field for _, field, _, _ in Formatter().parse(value) if field is not None]
            if any(field.split(".", 1)[0].split("[", 1)[0] not in TEMPLATE_KEYS for field in fields):
                raise _invalid(f"unknown template key in {value!r}")
            rendered.append(value.format_map(values))
        except (KeyError, ValueError, IndexError) as exc:
            raise _invalid(f"cannot render argv template {value!r}: {exc}") from exc
    return rendered
