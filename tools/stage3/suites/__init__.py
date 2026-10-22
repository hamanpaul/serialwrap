"""Stage 3 A/B suite adapter 的共用介面。"""

from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, Mapping


@dataclass(frozen=True)
class SuiteRun:
    """保存單側引擎執行結果與原始身分證據。"""

    status: str
    rc: int | None
    report_dir: Path
    identity: Mapping[str, Any]
    notes: tuple[str, ...]


REPORT_ENDPOINTS = "endpoints.json"


def _repo_root(m: Any, repo_root: Path | None = None) -> Path:
    if repo_root is not None:
        return Path(repo_root).resolve()
    return Path(__file__).resolve().parents[3]


def _frozen_path(m: Any, item: Any, repo_root: Path | None = None) -> Path:
    path = Path(item.path)
    return path if path.is_absolute() else _repo_root(m, repo_root) / path


def _write_json(path: Path, value: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value, ensure_ascii=False, sort_keys=True, indent=2) + "\n",
                    encoding="utf-8", newline="\n")


def _read_json_object(path: Path) -> dict[str, Any] | None:
    try:
        value = json.loads(Path(path).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    return value if isinstance(value, dict) else None


def _infra(inst: Any, note: str, rc: int | None = None,
           identity: Mapping[str, Any] | None = None) -> SuiteRun:
    return SuiteRun("INFRA_ERROR", rc, Path(inst.report_dir), identity or {}, (note,))


def run_side(m: Any, side: Any, inst: Any, *, run: Callable[..., Any], spawn: Callable[..., Any],
             now: Callable[[], str], probe: Any = None,
             repo_root: Path | None = None) -> SuiteRun:
    """依凍結 manifest 的 adapter 名稱執行單側 suite。"""
    try:
        adapter = m.suite.adapter
        if adapter == "l15_functest":
            from .l15_functest import run as run_l15

            return run_l15(m, side, inst, run=run, spawn=spawn, now=now, probe=probe,
                           repo_root=repo_root)
        if adapter == "l2_regression":
            from .l2_regression import run as run_l2

            return run_l2(m, side, inst, run=run, spawn=spawn, now=now, repo_root=repo_root)
        if adapter == "l3_realhw":
            from .l3_realhw import run as run_l3

            return run_l3(m, side, inst, run=run, spawn=spawn, now=now, repo_root=repo_root)
        return _infra(inst, f"HARNESS_SUITE_INFRA_ERROR: unknown suite adapter {adapter!r}")
    except Exception as exc:  # noqa: BLE001 - adapter 失敗須留在 suite 結果內
        return _infra(inst, f"HARNESS_SUITE_INFRA_ERROR: {type(exc).__name__}: {exc}")


def _mapping(value: Any) -> Mapping[str, Any]:
    return value if isinstance(value, Mapping) else {}


def _nested(value: Any, *keys: str) -> Any:
    node: Any = value
    for key in keys:
        if not isinstance(node, Mapping):
            return None
        node = node.get(key)
    return node


def _deployed_brackets(value: Any) -> list[Any]:
    if isinstance(value, Mapping):
        return [value]
    if isinstance(value, (list, tuple)):
        return list(value)
    return []


def verify_run_identity(m: Any, side: Any, run: SuiteRun, *,
                        expected_trees: Mapping[str, str]) -> list[str]:
    """比對 suite 報告的實際身分與該側 manifest 及凍結部署樹。"""
    identity = _mapping(run.identity)
    components = _mapping(side.manifest_doc.get("components"))
    cli_component = _mapping(components.get("cli"))
    daemon_component = _mapping(components.get("daemon"))
    actual_cli = _mapping(identity.get("cli"))
    actual_daemon = _mapping(identity.get("daemon"))
    violations: list[str] = []
    brackets = _deployed_brackets(identity.get("deployed_content"))

    def add(value: str) -> None:
        if value not in violations:
            violations.append(value)

    if actual_cli.get("sha256") != cli_component.get("sha256"):
        add("cli sha256 mismatch")
    report_cli = _mapping(identity.get("report_cli_identity"))
    gate_cli = _mapping(identity.get("gate_cli_identity"))
    if (report_cli and gate_cli
            and report_cli.get("sha256") != gate_cli.get("sha256")):
        add("cli sha256 mismatch")

    expected_impl = daemon_component.get("implementation")
    actual_impl = actual_daemon.get("implementation")
    if m.layer == "l1.5":
        # func-test 依 argv 分類；Python launcher 可能被回報為 rust，故以位元組身分判定。
        expected_daemon_sha = _nested(daemon_component, "launcher", "sha256")
        actual_daemon_sha = actual_daemon.get("sha256")
        if (not isinstance(actual_daemon_sha, str) or not actual_daemon_sha
                or actual_daemon_sha != expected_daemon_sha):
            add("daemon sha256 mismatch")
        if expected_impl == "rust" and actual_impl != "rust":
            add("daemon implementation mismatch")
    elif expected_impl == "rust":
        # L2／L3 報告不都輸出 implementation；launcher/image SHA 與 live-daemon
        # 證據才是可核對的實際身分來源。
        if actual_impl is not None and actual_impl != "rust":
            add("daemon implementation mismatch")
        expected_daemon_sha = _nested(daemon_component, "launcher", "sha256")
        actual_daemon_sha = actual_daemon.get("sha256")
        if m.layer == "l3":
            actual_daemon_sha = actual_daemon_sha or actual_daemon.get("image_sha256")
            if (not isinstance(actual_daemon_sha, str) or not actual_daemon_sha
                    or actual_daemon_sha != expected_daemon_sha):
                add("daemon sha256 mismatch")
        if m.layer == "l2":
            for bracket in brackets:
                live = _mapping(bracket).get("daemon_live")
                if not isinstance(live, (list, tuple)) or len(live) != 1:
                    add("daemon sha256 mismatch")
                    continue
                daemon = _mapping(live[0])
                if ("error" not in daemon or daemon.get("error") is not None
                        or daemon.get("in_install") is not True
                        or not isinstance(daemon.get("image_sha256"), str)
                        or daemon.get("image_sha256") != expected_daemon_sha):
                    add("daemon sha256 mismatch")
    elif expected_impl == "python":
        if actual_impl is not None and actual_impl != "python":
            add("daemon implementation mismatch")
        expected_tree = _nested(daemon_component, "package", "tree_sha256")
        actual_tree = actual_daemon.get("tree_sha256") or actual_daemon.get("package_tree_sha256")
        if actual_tree is not None and actual_tree != expected_tree:
            add("daemon sha256 mismatch")

    if m.layer in {"l2", "l3"}:
        if actual_cli.get("pinned") is not True:
            add("cli not pinned")
        resolved = actual_cli.get("resolved")
        configured = actual_cli.get("configured")
        if not isinstance(configured, str) or not configured.strip():
            add("cli not pinned")
        else:
            try:
                if Path(configured).resolve() != Path(side.cli_exe).resolve():
                    add("cli not pinned")
            except (OSError, RuntimeError):
                add("cli not pinned")
        # resolved 只要求有記錄：activate_argv 可能把 bundle 安裝到另一個 versions/<v>/ 再執行，
        # 實際執行路徑不必等於 side.cli_exe；該安裝的內容改由 deployed_content 與 expected_trees 核對。
        if not isinstance(resolved, str) or not resolved.strip():
            add("cli not pinned")
        if report_cli and report_cli.get("pinned") is not True:
            add("cli not pinned")
        if gate_cli and gate_cli.get("pinned") is not True:
            add("cli not pinned")

    recorded_manifest = identity.get("release_manifest_sha256")
    if recorded_manifest is not None and recorded_manifest != side.release_manifest.sha256:
        add("release manifest mismatch")

    if m.layer in {"l2", "l3"}:
        expected_version = daemon_component.get("version")
        actual_version = identity.get("daemon_version") or actual_daemon.get("version")
        if (isinstance(expected_version, str) and expected_version
                and (not isinstance(actual_version, str) or not actual_version
                     or actual_version != expected_version)):
            add("daemon version mismatch")

    measured: list[tuple[str, str]] = []
    if not brackets:
        add("deployed content unmeasured")
    else:
        for bracket in brackets:
            bracket = _mapping(bracket)
            start = _mapping(bracket.get("start"))
            end_value = bracket.get("end")
            end = _mapping(end_value)
            if (not start or not end or start.get("error") is not None or end.get("error") is not None
                    or any(not isinstance(start.get(key), str) or not start.get(key)
                           or not isinstance(end.get(key), str) or not end.get(key)
                           for key in ("bundle_tree_sha256", "runtime_tree_sha256"))):
                add("deployed content unmeasured")
                continue
            start_pair = (start["bundle_tree_sha256"], start["runtime_tree_sha256"])
            end_pair = (end["bundle_tree_sha256"], end["runtime_tree_sha256"])
            if start_pair != end_pair:
                add("deployed content changed during suite")
            measured.append(start_pair)
        if measured and any(pair != measured[0] for pair in measured[1:]):
            add("deployed content changed during suite")

    if measured and "deployed content changed during suite" not in violations:
        expected_pair = (expected_trees.get("bundle_tree_sha256"), expected_trees.get("runtime_tree_sha256"))
        if measured[0] != expected_pair:
            add("deployed tree mismatch")
    elif not isinstance(expected_trees, Mapping):
        add("deployed content unmeasured")

    return violations


__all__ = ["REPORT_ENDPOINTS", "SuiteRun", "run_side", "verify_run_identity"]
