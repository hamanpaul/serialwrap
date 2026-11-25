#!/usr/bin/env python3
"""Slice 2.3（P7）出口 gate 報告產生器與驗證器（08-slice23-login-events.md Task 19）。

`p7_gate.sh` 只負責「執行並記錄」：每個子程序的 argv、rc、時間、log（落檔＋sha256）、測試計數與 evidence 寫入
`<run>/results.json`；列狀態一律由本檔計算（gate 腳本本身不判定）。

狀態規則（R5-F3）：
- 測試列（`TEST_ROWS`）PASS ⇔ rc==0、`required_set` 非 null、`l1_results`（P2 `l1-results.v1`）經 P2
  `tests/acceptance/l1_judge.judge` 判定 PASS（每個凍結 nodeid 恰一列 passed、無重複、無 skip／未執行、exit 0、
  finished）、`test_counts` 與 l1 彙總相符、所有 evidence 檔存在且 sha256 相符；l1_judge FAIL 或任一列 failed → FAIL；
  INSUFFICIENT／INVALID／缺 l1_results／缺 required_set → INCOMPLETE；`required_set` 必須等於列名對應的
  `p7_gate:<slug>`（不得以較小集合替代），lock 的 selector 一併交給 judge 核對。
- 非測試列 PASS ⇔ rc==0 且至少一項 evidence、每項皆有有效 sha256 且相符；rc!=0 → FAIL；evidence 不符 → INCOMPLETE。
- 能力缺失（`caps_missing`）→ 該列 BLOCKED-OWNER＋B 編號。
- 擁有者項目 B-01～B-16：只有 `owner_evidence[B-xx]` 的檔案存在且 sha256 相符時 PASS，否則 BLOCKED-OWNER。
- `REQUIRED_ROWS` 缺列 → INCOMPLETE（未執行）。

用法：
  python3 scripts/rust/p7_gate_report.py init   --results-dir <run> --run-id <id> [--caps-missing c …] [--artifact p …]
  python3 scripts/rust/p7_gate_report.py record --results-dir <run> --name <列名> --rc <n> --log <p> --argv-json <j>
                                                --started-at <t> --ended-at <t> [--l1-results <p>] [--platform linux]
                                                [--evidence <p> …]       # 只記錄（log／evidence 落 sha256、測試計數取自 l1）
  python3 scripts/rust/p7_gate_report.py build  --results-dir <run> [--l1-lock <lock>] [--out <md>]
  python3 scripts/rust/p7_gate_report.py verify --results-dir <run> --md <md>     # 不一致清單非空 → exit 1
只用 stdlib；不 import sw_core。
"""
from __future__ import annotations

import argparse
import datetime as dt
import hashlib
import importlib.util
import json
import re
import shutil
import subprocess
import sys
from pathlib import Path
from typing import Any, Mapping, Sequence

ROOT = Path(__file__).resolve().parents[2]
DEFAULT_L1_LOCK = ROOT / "tests/acceptance/l1_required.lock.json"
OD59_ROWS: tuple[str, ...] = ("OD-59 control latency 500x3 A", "OD-59 control latency 500x3 B")
WINDOWS_EV01_ROW = "win-func-test ev-01-spawn-handler (Windows)"

REQUIRED_ROWS: tuple[str, ...] = (
    "check_cfg_boundary",
    "cargo test --workspace",
    "win-test.sh (non-COM)",
    "win-test.sh --caps windows_com",
    "contract_blackbox p7 A",
    "contract_blackbox p7 B",
    "Rust-only console line limit",
    *OD59_ROWS,
    "contract_blackbox slice21 B",
    "contract_blackbox slice22 B",
    "l15 slice21 B",
    "l15 slice23 B",
    "func-test brcm-01-login-flow",
    "func-test ev-01-spawn-handler",
    WINDOWS_EV01_ROW,
    "regen_lock --check",
    "cases.lock slice_2.3 coverage",
    "python3 -m pytest -q tests/ --deselect tests/test_rust_bench_tools.py::test_cargo_workspace_tests_windows_native --deselect tests/test_rust_stage1b_registration.py::test_p4_required_cases_have_passing_evidence",
    "policy_check",
)
TEST_ROWS: frozenset[str] = frozenset({
    "cargo test --workspace",
    "win-test.sh (non-COM)",
    "win-test.sh --caps windows_com",
    "contract_blackbox p7 A",
    "contract_blackbox p7 B",
    "Rust-only console line limit",
    *OD59_ROWS,
    "contract_blackbox slice21 B",
    "contract_blackbox slice22 B",
    "l15 slice21 B",
    "l15 slice23 B",
    "python3 -m pytest -q tests/ --deselect tests/test_rust_bench_tools.py::test_cargo_workspace_tests_windows_native --deselect tests/test_rust_stage1b_registration.py::test_p4_required_cases_have_passing_evidence",
})
FULL_PYTEST_N3_ROW = (
    "python3 -m pytest -q tests/ --deselect tests/test_rust_bench_tools.py::test_cargo_workspace_tests_windows_native "
    "--deselect tests/test_rust_stage1b_registration.py::test_p4_required_cases_have_passing_evidence"
)
# 能力缺失 → BLOCKED-OWNER 的對應（列名 → (cap, B 編號)）。
CAP_ROWS: Mapping[str, tuple[str, str]] = {
    "win-test.sh (non-COM)": ("windows_native", "B-06"),
    "win-test.sh --caps windows_com": ("windows_com", "B-06"),
}
OWNER_ITEMS: Mapping[str, str] = {
    "B-01": "四平台實機登入（bcm、prpl、op3 shell、smart4R console unlock）",
    "B-02": "L2 F9 六案（destructive）",
    "B-03": "真實 USB／usbip read-endpoint stall",
    "B-04": "L2 F10 兩案（destructive）",
    "B-05": "L2 F11 兩案",
    "B-06": "原生 Windows COM handle 證據（OD-08、OD-15）",
    "B-07": "L3 realhw P0＋登入／Recovery／事件 P1 與 remote",
    "B-08": "測試團隊 Windows 工作站 event handler",
    # 先前切片 L2 regression family（08 BLOCKED-OWNER 總表；rulings/p7.md SPEC-GAP #4 (i)）
    "B-09": "L2 F1 命令契約五案（先前切片）",
    "B-10": "L2 F2 背壓四案（先前切片）",
    "B-11": "L2 F3 可觀測性四案（先前切片）",
    "B-12": "L2 F4 session 語意三案（先前切片）",
    "B-13": "L2 F5 console 共存五案（先前切片，含 destructive）",
    "B-14": "L2 F6 RPC 存活兩案（先前切片，two_boards）",
    "B-15": "L2 F8 daemon 單例兩案（先前切片）",
    "B-16": "L2 F12 診斷兩案（先前切片）",
}
# parity-amendments-2026-10-26 #188：P7 承接列的必要 L1 集合（Task 0 起登記；py_parity_* 為後續 Task 增列）。
PARITY_SETS: tuple[str, ...] = ("p7_gate:py_parity", "p7_gate:py_parity_recover", "p7_gate:py_parity_attach")


_SHA256_RE = re.compile(r"^[0-9a-f]{64}$")
# verify 必須核對的受測 artifact（Rust CLI 與 daemon；路徑為 repo 相對，檔名可帶 `.exe`）：缺任一 → 驗證失敗
#（審查 P7-c5f0fc9-3：`artifacts:{}` 不得通過）。
REQUIRED_ARTIFACTS: tuple[str, ...] = ("serialwrap", "serialwrapd")


def slug(name: str) -> str:
    """列名 → 必要集合 slug（`cargo test --workspace` → `cargo-test-workspace`）。"""
    return re.sub(r"[^a-z0-9]+", "-", name.lower()).strip("-")


def expected_set(name: str) -> str:
    """列名對應的必要集合固定為 `p7_gate:<slug>`，不得以其他集合替代（審查 P7-c5f0fc9-1）。"""
    if name == FULL_PYTEST_N3_ROW:
        return "p7_gate:python3-m-pytest-q-tests"
    return f"p7_gate:{slug(name)}"


def current_head() -> str | None:
    try:
        return subprocess.run(["git", "rev-parse", "HEAD"], cwd=ROOT, capture_output=True, text=True,
                              check=True).stdout.strip()
    except (OSError, subprocess.CalledProcessError):
        return None


def _load_judge():
    spec = importlib.util.spec_from_file_location("p7_l1_judge", ROOT / "tests/acceptance/l1_judge.py")
    assert spec is not None and spec.loader is not None
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def _sha256(path: Path) -> str | None:
    try:
        return hashlib.sha256(path.read_bytes()).hexdigest()
    except OSError:
        return None


def _test_hook_inspector() -> Any:
    """載入 bundle 建置器共用的逐名測試鉤子政策，避免 gate 副本漂移。"""
    path = ROOT / "scripts/rust/check_test_hook_markers.py"
    spec = importlib.util.spec_from_file_location("serialwrap_test_hook_markers", path)
    if spec is None or spec.loader is None:
        raise ValueError(f"無法載入共用測試鉤子檢查器：{path}")
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def windows_bundle_identity(bundle_dir: Path) -> dict[str, Any]:
    """核對 Windows bundle 的 Stage 2 manifest／執行檔，並計算完整檔案樹 SHA-256。"""
    root = Path(bundle_dir).resolve(strict=True)
    if not root.is_dir():
        raise ValueError(f"Windows bundle 不是目錄：{root}")
    files: list[Path] = []
    for path in root.rglob("*"):
        if path.is_symlink():
            raise ValueError(f"Windows bundle 不得含 symlink：{path.relative_to(root)}")
        if path.is_file() and path.relative_to(root).as_posix() != ".mkbundle-owner":
            files.append(path)
    tree = hashlib.sha256()
    for path in sorted(files, key=lambda item: item.relative_to(root).as_posix().encode("utf-8")):
        relative = path.relative_to(root).as_posix().encode("utf-8")
        tree.update(len(relative).to_bytes(8, "big"))
        tree.update(relative)
        digest = _sha256(path)
        if digest is None:
            raise ValueError(f"無法讀取 Windows bundle 檔案：{path.relative_to(root)}")
        tree.update(bytes.fromhex(digest))

    try:
        pointer = json.loads((root / "bin/current-release.json").read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        raise ValueError(f"bundle current-release.json 不可讀：{exc}") from exc
    version = pointer.get("bundle_version") if isinstance(pointer, Mapping) else None
    expected_dir = f"versions/{version}" if isinstance(version, str) else None
    if not isinstance(version, str) or re.fullmatch(r"\d+\.\d+\.\d+", version) is None or pointer.get("dir") != expected_dir:
        raise ValueError("bundle current-release.json 的 bundle_version／dir 不符")
    version_root = (root / expected_dir).resolve()
    if not version_root.is_relative_to(root):
        raise ValueError("bundle current-release.json 指向 bundle 外路徑")
    manifest_path = version_root / "release-manifest.json"
    try:
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        raise ValueError(f"bundle release-manifest.json 不可讀：{exc}") from exc
    if not isinstance(manifest, Mapping) or manifest.get("schema_version") != 1 \
            or manifest.get("bundle_version") != version:
        raise ValueError("bundle release-manifest.json 的 schema_version／bundle_version 不符")
    sums_path = version_root / "SHA256SUMS"
    try:
        sums_lines = sums_path.read_text(encoding="utf-8").splitlines()
    except OSError as exc:
        raise ValueError(f"bundle SHA256SUMS 不可讀：{exc}") from exc
    sums: dict[str, str] = {}
    for line in sums_lines:
        match = re.fullmatch(r"([0-9a-f]{64})  (.+)", line)
        if match is None:
            raise ValueError(f"bundle SHA256SUMS 格式錯誤：{line!r}")
        digest, relative = match.groups()
        if (relative.startswith("/") or "\\" in relative or ".." in relative.split("/")
                or relative in {"release-manifest.json", "SHA256SUMS"} or relative in sums):
            raise ValueError(f"bundle SHA256SUMS 路徑重複／不安全：{relative!r}")
        sums[relative] = digest
    version_files = {
        path.relative_to(version_root).as_posix(): path
        for path in version_root.rglob("*")
        if path.is_file() and path.relative_to(version_root).as_posix() not in {"release-manifest.json", "SHA256SUMS"}
    }
    if set(sums) != set(version_files):
        raise ValueError("bundle SHA256SUMS 清單與 version 目錄檔案集合不符")
    for relative, path in version_files.items():
        if _sha256(path) != sums[relative]:
            raise ValueError(f"bundle SHA256SUMS SHA-256 不符：{relative}")
    stage = manifest.get("stage")
    git_revision = manifest.get("git_revision")
    if not isinstance(git_revision, str) or re.fullmatch(r"[0-9a-f]{40}", git_revision) is None:
        raise ValueError("bundle release-manifest.json 缺有效 git_revision")
    components = manifest.get("components")
    cli = components.get("cli") if isinstance(components, Mapping) else None
    daemon = components.get("daemon") if isinstance(components, Mapping) else None
    if not isinstance(cli, Mapping) or cli.get("implementation") != "rust" or cli.get("version") != version:
        raise ValueError("bundle CLI 不是同版本 Rust 實作")
    cli_rel = cli.get("path")
    if cli_rel != "bin/serialwrap.exe":
        raise ValueError(f"bundle manifest CLI path 不符：{cli_rel!r}")
    cli_expected = cli.get("sha256")
    cli_versioned = version_root / cli_rel
    cli_root = root / "bin/serialwrap.exe"
    cli_sha = _sha256(cli_versioned)
    if not isinstance(cli_expected, str) or not _SHA256_RE.fullmatch(cli_expected) or cli_sha != cli_expected \
            or _sha256(cli_root) != cli_expected:
        raise ValueError("Windows bundle CLI SHA-256 與 manifest／穩定入口不符")

    daemon_sha: str | None = None
    binaries = [("bin/serialwrap.exe", cli_versioned)]
    if stage == "rust-2":
        if manifest.get("target") != "x86_64-pc-windows-gnu" or not isinstance(daemon, Mapping) \
                or daemon.get("implementation") != "rust" or daemon.get("version") != version:
            raise ValueError("bundle 不是 x86_64 Windows Rust Stage 2")
        launcher = daemon.get("launcher")
        daemon_rel = launcher.get("path") if isinstance(launcher, Mapping) else None
        daemon_expected = launcher.get("sha256") if isinstance(launcher, Mapping) else None
        if daemon_rel != "bin/serialwrapd.exe" or not isinstance(daemon_expected, str) \
                or not _SHA256_RE.fullmatch(daemon_expected):
            raise ValueError("Stage 2 bundle manifest daemon launcher 不符")
        daemon_sha = _sha256(version_root / daemon_rel)
        if daemon_sha != daemon_expected or _sha256(root / "bin/serialwrapd.exe") != daemon_expected:
            raise ValueError("Windows bundle daemon SHA-256 與 manifest／穩定入口不符")
        if daemon.get("git_revision") != git_revision or cli.get("git_revision") != git_revision:
            raise ValueError("Stage 2 bundle 元件 git_revision 與 manifest 不符")
        binaries.append((daemon_rel, version_root / daemon_rel))
    hook_inspector = _test_hook_inspector()
    for relative, executable in binaries:
        for marker in hook_inspector.inspect_binary(executable):
            raise ValueError(f"Windows bundle 執行檔含禁用測試鉤子 {marker}：{relative}")
    return {
        "bundle_sha256": tree.hexdigest(),
        "stage": stage,
        "version": version,
        "git_revision": git_revision,
        "manifest_sha256": _sha256(manifest_path),
        "cli_sha256": cli_sha,
        "daemon_sha256": daemon_sha,
    }


def windows_ev01_status(report_path: Path | None, bundle_dir: Path | None,
                        candidate_head: Any) -> tuple[str, str]:
    """以完整報告、HEAD 與 bundle 檔案樹身分核對 ev-01 Windows 列。"""
    if report_path is None or bundle_dir is None:
        return "INCOMPLETE", "缺 Windows ev-01 report 或 Stage 2 bundle 路徑"
    report_path = Path(report_path)
    bundle_dir = Path(bundle_dir)
    if report_path.name != "func-test-report.json" or report_path.parent.name != "basic":
        return "INCOMPLETE", "Windows ev-01 report 必須是 basic/func-test-report.json"
    metadata_path = report_path.parent.parent / "win-func-test-meta.json"
    try:
        report = json.loads(report_path.read_text(encoding="utf-8"))
        metadata = json.loads(metadata_path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        return "INCOMPLETE", f"Windows ev-01 report/meta 不可讀：{exc}"
    if not isinstance(report, Mapping) or not isinstance(metadata, Mapping):
        return "INCOMPLETE", "Windows ev-01 report/meta 根節點必須是 object"
    if not isinstance(candidate_head, str) or re.fullmatch(r"[0-9a-f]{40}", candidate_head) is None:
        return "INCOMPLETE", "P7 results.json.git_head 無法核對 Windows ev-01 證據"
    if metadata.get("schema_version") != "win-func-test-meta.v2" or metadata.get("git_clean") is not True:
        return "INCOMPLETE", "Windows func-test metadata schema/git_clean 不符"
    for label, value in (("report metadata", metadata.get("git_head")),
                         ("Stage 2 bundle", metadata.get("bundle_git_revision"))):
        if value != candidate_head:
            return "INCOMPLETE", f"{label} HEAD {value!r} 與候選版 {candidate_head!r} 不符"
    if not isinstance(metadata.get("git_tree"), str) or re.fullmatch(r"[0-9a-f]{40}", metadata["git_tree"]) is None:
        return "INCOMPLETE", "Windows func-test metadata 缺有效 git_tree"
    if metadata.get("com_pair") != {"daemon": "COM20", "bridge": "COM21"} \
            or metadata.get("transport_mode") != "virtual_com0com":
        return "INCOMPLETE", "Windows ev-01 必須使用核准的 COM20:COM21 virtual_com0com"
    if metadata.get("daemon_exe_used") is not True:
        return "INCOMPLETE", "Windows ev-01 未證明以 Stage 2 Rust daemon 執行"

    try:
        identity = windows_bundle_identity(bundle_dir)
    except (OSError, ValueError, TypeError) as exc:
        return "INCOMPLETE", f"Windows Stage 2 bundle 無法核對：{exc}"
    if identity.get("stage") != "rust-2":
        return "INCOMPLETE", f"Windows bundle stage={identity.get('stage')!r}，須為 rust-2"
    identity_fields = {
        "bundle_sha256": "bundle_sha256",
        "bundle_stage": "stage",
        "bundle_version": "version",
        "bundle_git_revision": "git_revision",
        "bundle_manifest_sha256": "manifest_sha256",
        "cli_exe_sha256": "cli_sha256",
        "daemon_exe_sha256": "daemon_sha256",
    }
    for field, identity_key in identity_fields.items():
        if metadata.get(field) != identity.get(identity_key):
            return "INCOMPLETE", f"Windows bundle {field} SHA-256／身分與報告不符"
    report_sha = _sha256(report_path)
    if not isinstance(metadata.get("basic_report_sha256"), str) or metadata.get("basic_report_sha256") != report_sha:
        return "INCOMPLETE", "Windows ev-01 basic report SHA-256 與 metadata 不符"

    runs = metadata.get("runs") if isinstance(metadata.get("runs"), Mapping) else {}
    scenario_run = runs.get("scenarios")
    if metadata.get("case_filter") != "ev-01-spawn-handler" or metadata.get("scenario_skipped") is not True \
            or not isinstance(scenario_run, Mapping) or scenario_run.get("skipped") is not True \
            or scenario_run.get("runner_exit") != 0 or scenario_run.get("runner_argv") != []:
        return "INCOMPLETE", "Windows func-test metadata 未證明只執行一次聚焦 ev-01 basic runner"
    basic_run = runs.get("basic")
    argv = basic_run.get("runner_argv") if isinstance(basic_run, Mapping) else None
    if not isinstance(basic_run, Mapping) or basic_run.get("report") != "basic/func-test-report.json":
        return "INCOMPLETE", "Windows func-test metadata 缺 basic report run"
    if not isinstance(argv, list) or not all(isinstance(item, str) for item in argv):
        return "INCOMPLETE", "Windows basic runner argv 格式錯誤"
    if argv[:4] != ["-3.12", "func-test/runner.py", "--mode", "bridge"] or "--scenarios" in argv:
        return "INCOMPLETE", "Windows basic runner 不是指定的 bridge 模式"
    for flag, expected in (("--case", "ev-01-spawn-handler"), ("--daemon-com", "COM20"),
                           ("--bridge-com", "COM21"), ("--transport-mode", "virtual_com0com")):
        if argv.count(flag) != 1:
            return "INCOMPLETE", f"Windows basic runner {flag} 必須恰出現一次"
        position = argv.index(flag)
        if argv[position + 1:position + 2] != [expected]:
            return "INCOMPLETE", f"Windows basic runner {flag} 不符"
    try:
        basic_exit = basic_run.get("runner_exit")
        if type(basic_exit) is not int:
            return "INCOMPLETE", "Windows basic runner exit 格式錯誤"
        if report.get("schema_version") != "func-test-report.v1" or report.get("complete") is not True \
                or report.get("independent_cases") != 1:
            return "INCOMPLETE", "Windows basic func-test 報告未完整完成單一案例"
        results = report.get("results")
        if not isinstance(results, list) or len(results) != 1 or not isinstance(results[0], Mapping):
            return "INCOMPLETE", "Windows basic func-test 報告不是恰一筆必要案例"
        result = results[0]
        if result.get("case_id") != "ev-01-spawn-handler" or result.get("mode") != "bridge" \
                or type(result.get("run_index")) is not int or result.get("run_index") != 0:
            return "INCOMPLETE", "Windows basic func-test 報告案例／mode／run_index 不符"
        verdict = result.get("verdict")
        expected_summary = {"runs": 1, "pass": int(verdict == "PASS"), "fail": int(verdict == "FAIL"),
                            "insufficient": int(verdict == "INSUFFICIENT_EVIDENCE"), "skip": int(verdict == "SKIP")}
        bridge_summary = (report.get("modes") or {}).get("bridge") if isinstance(report.get("modes"), Mapping) else None
        if bridge_summary != expected_summary:
            return "INCOMPLETE", "Windows basic func-test 統計與 ev-01 verdict 不符"
        if verdict == "FAIL":
            return "FAIL", "ev-01-spawn-handler Windows verdict=FAIL"
        if verdict != "PASS":
            return "INCOMPLETE", f"ev-01-spawn-handler Windows verdict={verdict!r}"
        if result.get("gate_eligible") is not True:
            return "INCOMPLETE", "ev-01-spawn-handler Windows gate_eligible 不是 true"
        if basic_exit != 0:
            return "INCOMPLETE", f"Windows basic runner_exit={basic_exit}，PASS 證據不完整"
    except (AttributeError, TypeError, ValueError) as exc:
        return "INCOMPLETE", f"Windows ev-01 report 格式錯誤：{exc}"
    return "PASS", f"ev-01-spawn-handler PASS＋gate_eligible；bundle SHA-256 {identity['bundle_sha256']}"


def _resolve(base: Path, p: str) -> Path:
    q = Path(p)
    return q if q.is_absolute() else base / q


def _evidence_errors(base: Path, evidence: Any, *, required: bool) -> list[str]:
    """每項證據須有路徑與有效 sha256 且相符；`required` 時不得為空（審查 P7-c5f0fc9-2）。"""
    items = list(evidence or [])
    if required and not items:
        return ["缺必要證據"]
    errs: list[str] = []
    for item in items:
        if not isinstance(item, Mapping) or not isinstance(item.get("path"), str):
            errs.append(f"evidence 格式錯誤：{item!r}")
            continue
        want = item.get("sha256")
        if not (isinstance(want, str) and _SHA256_RE.match(want)):
            errs.append(f"evidence 缺有效 sha256：{item['path']}")
            continue
        got = _sha256(_resolve(base, item["path"]))
        if got is None:
            errs.append(f"evidence 缺檔：{item['path']}")
        elif got != want:
            errs.append(f"evidence sha256 不符：{item['path']}")
    return errs


def _windows_provenance_problems(report: Mapping[str, Any], candidate_head: Any) -> list[str]:
    problems: list[str] = []
    if report.get("platform") != "windows" or report.get("finished") is not True:
        problems.append("platform 須為 windows 且 finished 須為 true")
    if not isinstance(candidate_head, str) or re.fullmatch(r"[0-9a-f]{40}", candidate_head) is None:
        problems.append("results.json.git_head 無法核對 Windows 證據")
    provenance = report.get("provenance")
    git = provenance.get("git") if isinstance(provenance, Mapping) else None
    git = git if isinstance(git, Mapping) else {}
    end = git.get("end")
    end = end if isinstance(end, Mapping) else {}
    for label, state in (("provenance.git", git), ("provenance.git.end", end)):
        if state.get("head") != candidate_head or not state.get("head"):
            problems.append(f"{label}.head={state.get('head')!r} 與候選版 {candidate_head!r} 不符")
        if state.get("clean") is not True:
            problems.append(f"{label}.clean={state.get('clean')!r}（須為 true）")
    return problems


def _test_row_status(base: Path, row: Mapping[str, Any], lock: Mapping[str, Any] | None, judge,
                     candidate_head: Any = None) -> tuple[str, str]:
    counts = row.get("test_counts") or {}
    rc = row.get("rc")
    sid = row.get("required_set")
    l1_path = row.get("l1_results")
    if counts.get("failed"):
        # 任一列 failed 即 FAIL（不論是否附 l1_results；OD-15 COM 列依實跑判定）。
        return "FAIL", f"failed={counts.get('failed')} rc={rc}"
    if not sid:
        return "INCOMPLETE", "缺 required_set"
    if sid != expected_set(row.get("name", "")):
        return "INCOMPLETE", f"required_set {sid} 與列名對應的 {expected_set(row.get('name', ''))} 不符"
    if not l1_path:
        return "INCOMPLETE", "缺 l1_results"
    try:
        report = json.loads(_resolve(base, l1_path).read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        return "INCOMPLETE", f"l1_results 不可讀：{exc}"
    platform = row.get("platform") or report.get("platform") or "linux"
    if platform == "windows":
        problems = _windows_provenance_problems(report, candidate_head)
        if problems:
            return "INCOMPLETE", "; ".join(problems)[:300]
    try:
        required = judge.required_for(lock or {}, sid, platform)
        selector = lock["sets"][sid][platform].get("selector")
    except (KeyError, TypeError):
        return "INCOMPLETE", f"鎖中無 {sid}.{platform}"
    verdict = judge.judge(report, required, platform=platform, selector=selector)
    if verdict["verdict"] == "FAIL":
        return "FAIL", "; ".join(verdict.get("reasons") or [f"failed={counts.get('failed')}"])[:300]
    if verdict["verdict"] != "PASS":
        detail = verdict.get("reasons") or []
        missing = verdict.get("missing") or []
        text = "; ".join(str(x) for x in detail)
        if missing:
            text = f"missing {len(missing)}；" + text
        return "INCOMPLETE", text[:300] or verdict["verdict"]
    agg = verdict.get("counts") or {}
    if counts and (counts.get("passed") != agg.get("passed") or counts.get("failed", 0) != agg.get("failed", 0)):
        return "INCOMPLETE", f"test_counts 與 l1 彙總不符：{counts} vs {agg}"
    if rc != 0:
        return "INCOMPLETE", f"rc={rc}"
    ev = _evidence_errors(base, row.get("evidence"), required=False)
    if ev:
        return "INCOMPLETE", "; ".join(ev)
    return "PASS", f"{sid} {len(required)} 筆"


def _windows_ev01_row_status(base: Path, row: Mapping[str, Any], candidate_head: Any) -> tuple[str, str]:
    if row.get("rc") != 0:
        return "FAIL", f"Windows ev-01 匯入 rc={row.get('rc')}"
    evidence_errors = _evidence_errors(base, row.get("evidence"), required=True)
    if evidence_errors:
        return "INCOMPLETE", "; ".join(evidence_errors)
    evidence = row.get("windows_ev01")
    if not isinstance(evidence, Mapping):
        return "INCOMPLETE", "缺 Windows ev-01 evidence import"
    report_rel, bundle_rel = evidence.get("report_path"), evidence.get("bundle_path")
    if not isinstance(report_rel, str) or not isinstance(bundle_rel, str):
        return "INCOMPLETE", str(evidence.get("snapshot_error") or "缺 Windows ev-01 report/bundle 路徑")
    return windows_ev01_status(_resolve(base, report_rel), _resolve(base, bundle_rel), candidate_head)


def _row_status(base: Path, row: Mapping[str, Any], caps_missing: set[str], lock, judge,
                candidate_head: Any) -> tuple[str, str]:
    name = row.get("name", "")
    if name == WINDOWS_EV01_ROW:
        return _windows_ev01_row_status(base, row, candidate_head)
    cap = CAP_ROWS.get(name)
    if cap is not None and cap[0] in caps_missing:
        return "BLOCKED-OWNER", cap[1]
    if name in TEST_ROWS:
        return _test_row_status(base, row, lock, judge, candidate_head)
    if row.get("rc") != 0:
        return "FAIL", f"rc={row.get('rc')}"
    ev = _evidence_errors(base, row.get("evidence"), required=True)
    if ev:
        return "INCOMPLETE", "; ".join(ev)
    return "PASS", ""


def _owner_status(base: Path, owner_evidence: Mapping[str, Any], bid: str) -> tuple[str, str]:
    ev = owner_evidence.get(bid)
    if not ev:
        return "BLOCKED-OWNER", "證據未提供"
    errs = _evidence_errors(base, ev, required=True)
    if errs:
        return "BLOCKED-OWNER", "; ".join(errs)
    return "PASS", "擁有者證據已核對"


def _policy_check_pr_label(row: Mapping[str, Any] | None) -> str:
    """回報 policy_check 實際 argv 帶入的 --pr-labels，不從環境或 VERSION 推算。"""
    if not isinstance(row, Mapping):
        return "未執行"
    argv = row.get("argv")
    if not isinstance(argv, list) or not all(isinstance(arg, str) for arg in argv):
        return "argv 格式錯誤"
    labels: list[str] = []
    index = 0
    while index < len(argv):
        arg = argv[index]
        if arg == "--pr-labels":
            labels.append(argv[index + 1] if index + 1 < len(argv) else "<缺值>")
            index += 2
        elif arg.startswith("--pr-labels="):
            labels.append(arg.partition("=")[2])
            index += 1
        else:
            index += 1
    return "、".join(labels) if labels else "未帶入"


def build_report(results_dir: Path, *, l1_lock: Path | None = None) -> str:
    results_dir = Path(results_dir)
    data = json.loads((results_dir / "results.json").read_text(encoding="utf-8"))
    lock_path = Path(l1_lock) if l1_lock is not None else DEFAULT_L1_LOCK
    try:
        lock = json.loads(lock_path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        lock = None
    judge = _load_judge()
    caps_missing = set(data.get("caps_missing") or [])
    rows = {r.get("name"): r for r in data.get("rows") or [] if isinstance(r, Mapping)}
    policy_label = _policy_check_pr_label(rows.get("policy_check"))
    lines = [
        "# Slice 2.3（P7）出口 gate 報告",
        "",
        f"- run_id: `{data.get('run_id')}`",
        f"- git_head: `{data.get('git_head')}`",
        f"- worktree_clean: `{data.get('worktree_clean')}`",
        f"- caps_missing: `{sorted(caps_missing)}`",
        f"- policy_check_pr_label: `{policy_label}`",
        f"- tool_versions: `{json.dumps(data.get('tool_versions'), sort_keys=True)}`",
        f"- l1_lock_sha256: `{_sha256(lock_path)}`",
        "",
        "## artifacts",
        "",
        "| artifact | sha256 |",
        "|---|---|",
    ]
    for name, digest in sorted((data.get("artifacts") or {}).items()):
        lines.append(f"| {name} | {digest} |")
    breaker = data.get("breaker")
    if isinstance(breaker, Mapping):
        assessment = breaker.get("assessment") if isinstance(breaker.get("assessment"), Mapping) else {}
        exemption = breaker.get("exemption") if isinstance(breaker.get("exemption"), Mapping) else None
        lines += ["", "## 承接率斷路器", "",
                  f"- slice: `{breaker.get('slice')}`",
                  f"- 快照數／凍結分母: `{breaker.get('snapshot_count')}`／`{breaker.get('denominator')}`",
                  f"- 實際狀態：{breaker.get('actual_verdict')}",
                  f"- tripped／insufficient_evidence: `{assessment.get('tripped')}`／`{assessment.get('insufficient_evidence')}`",
                  f"- 窗口累計變化（pp）: `{assessment.get('window_delta_pp')}`"]
        if assessment.get("reason"):
            lines.append(f"- 實際判定原因: {str(assessment['reason']).replace('|', '/')}")
        if exemption:
            lines.append(f"- gate 效果：OD-87 豁免（{exemption.get('date')}；{exemption.get('reason')}）")
        else:
            lines.append(f"- gate 判定：{breaker.get('gate_verdict')}")
        evidence = data.get("breaker_evidence")
        if isinstance(evidence, Mapping):
            lines.append(f"- 評估證據 sha256: `{evidence.get('sha256')}`；exit `{data.get('breaker_exit_code')}`")
    prior_artifacts = data.get("prior_slice_artifacts") or {}
    if prior_artifacts:
        lines += ["", "## 先前切片 artifact", "", "| artifact | sha256 |", "|---|---|"]
        for name, digest in sorted(prior_artifacts.items()):
            lines.append(f"| {name} | {digest} |")
    lines += ["", "OD-59 的一般 p7 A／B 列只作小樣本快速檢查，不代表達成 500×3 契約；契約判定只看下方專屬量測列。"]
    statuses: dict[str, str] = {}
    names = list(REQUIRED_ROWS) + [n for n in rows if n not in REQUIRED_ROWS]
    for heading, section_rows in (("## 列狀態", [n for n in names if n not in OD59_ROWS]),
                                  ("## OD-59 契約量測", [n for n in OD59_ROWS if n in names])):
        lines += ["", heading, "", "| 列 | 狀態 | 說明 | log_sha256 |", "|---|---|---|---|"]
        for name in section_rows:
            row = rows.get(name)
            if row is None:
                status, detail = "INCOMPLETE", "未執行"
                log_sha = ""
            else:
                status, detail = _row_status(results_dir, row, caps_missing, lock, judge, data.get("git_head"))
                log_sha = row.get("log_sha256") or ""
            statuses[name] = status
            lines.append(f"| {name} | {status} | {detail.replace('|', '/')} | {log_sha} |")
    lines += ["", "## 擁有者項目", "", "| 項目 | 狀態 | 說明 |", "|---|---|---|"]
    owner_evidence = data.get("owner_evidence") or {}
    for bid, title in OWNER_ITEMS.items():
        status, detail = _owner_status(results_dir, owner_evidence, bid)
        lines.append(f"| {bid} | {status} | {title}；{detail} |")
    lines += ["", "## parity_amendments", "", "| 必要集合 | 平台 | nodeid 數 |", "|---|---|---|"]
    for sid in PARITY_SETS:
        for plat in ("linux", "windows"):
            n = len(((lock or {}).get("sets", {}).get(sid, {}).get(plat) or {}).get("nodeids") or [])
            if n:
                lines.append(f"| {sid} | {plat} | {n} |")
    breaker_ok = not isinstance(breaker, Mapping) or breaker.get("gate_verdict") in ("PASS", "OD-87_EXEMPT")
    ok = breaker_ok and all(s in ("PASS", "BLOCKED-OWNER") for s in statuses.values())
    lines += ["", f"結論：{'PASS（含 BLOCKED-OWNER）' if ok else 'NOT-PASS'}", ""]
    return "\n".join(lines)


def verify_report(md_path: Path, results_dir: Path, *, l1_lock: Path | None = None) -> list[str]:
    """重算報告並逐行比對；另核對 artifact、log sha256 與 results.json 的 git_head／run_id。"""
    results_dir = Path(results_dir)
    problems: list[str] = []
    try:
        data = json.loads((results_dir / "results.json").read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        return [f"results.json 不可讀：{exc}"]
    text = Path(md_path).read_text(encoding="utf-8")
    if f"run_id: `{data.get('run_id')}`" not in text:
        problems.append("run_id 不符")
    if f"git_head: `{data.get('git_head')}`" not in text:
        problems.append("git_head 不符")
    head = current_head()
    if not data.get("git_head") or data.get("git_head") != head:
        problems.append(f"results.json git_head {data.get('git_head')} 與目前 HEAD {head} 不符")
    names = {Path(p).name.removesuffix(".exe") for p in (data.get("artifacts") or {})}
    for need in REQUIRED_ARTIFACTS:
        if need not in names:
            problems.append(f"缺必要 artifact：{need}")
    for path, want in (data.get("artifacts") or {}).items():
        got = _sha256(_resolve(ROOT, path))
        if got is None or got != want:
            problems.append(f"artifact sha256 不符或缺檔：{path}")
    breaker_evidence = data.get("breaker_evidence")
    if isinstance(breaker_evidence, Mapping):
        path, want = breaker_evidence.get("path"), breaker_evidence.get("sha256")
        got = _sha256(_resolve(results_dir, path)) if isinstance(path, str) else None
        if got is None or got != want:
            problems.append("斷路器評估證據 sha256 不符或缺檔")
    prior_paths = data.get("prior_slice_artifact_paths") or {}
    for name, want in (data.get("prior_slice_artifacts") or {}).items():
        path = prior_paths.get(name)
        got = _sha256(_resolve(results_dir, path)) if isinstance(path, str) else None
        if got is None or got != want:
            problems.append(f"先前切片 artifact sha256 不符或缺檔：{name}")
    for row in data.get("rows") or []:
        lp, want = row.get("log_path"), row.get("log_sha256")
        if lp and want and _sha256(_resolve(results_dir, lp)) != want:
            problems.append(f"log sha256 不符：{row.get('name')}")
    expect = build_report(results_dir, l1_lock=l1_lock).splitlines()
    got = text.splitlines()
    if expect != got:
        diff = [f"- {e!r} / + {g!r}" for e, g in zip(expect, got) if e != g][:10]
        problems.append("列狀態重算不符：" + "; ".join(diff) if diff else "行數不符")
    return problems


def _rel_to(base: Path, p: Path) -> str:
    try:
        return Path(p).resolve().relative_to(Path(base).resolve()).as_posix()
    except ValueError:
        return Path(p).as_posix()


def _tool_versions() -> dict[str, str | None]:
    out: dict[str, str | None] = {"python": sys.version.split()[0]}
    try:
        import pytest  # noqa: PLC0415
        out["pytest"] = pytest.__version__
    except ImportError:
        out["pytest"] = None
    return out


def init_results(results_dir: Path, run_id: str, *, caps_missing: list[str], artifacts: list[Path],
                 prior_slice_artifacts: Sequence[tuple[str, Path]] = ()) -> dict[str, Any]:
    """`p7_gate.sh` 起點：記錄 HEAD、受測 artifact 與獨立的先前切片 artifact 身分。"""
    status = subprocess.run(["git", "status", "--porcelain"], cwd=ROOT, capture_output=True, text=True)
    prior_sha = {name: _sha256(path) for name, path in prior_slice_artifacts}
    prior_paths = {name: _rel_to(results_dir, path) for name, path in prior_slice_artifacts}
    data = {
        "run_id": run_id,
        "git_head": current_head(),
        "worktree_clean": status.returncode == 0 and not status.stdout.strip(),
        "caps_missing": sorted(set(caps_missing)),
        "artifacts": {_rel_to(ROOT, a): _sha256(a) for a in artifacts},
        "prior_slice_artifacts": prior_sha,
        "prior_slice_artifact_paths": prior_paths,
        # 受測工具版本（審查 P7-b85eec5-1：pytest 9.2.0 對過長字串參數改用 `<argname><index>` id，3 個凍結 nodeid
        # 會因此缺測；報告須註明版本以判讀 INCOMPLETE 原因）。
        "tool_versions": _tool_versions(),
        "rows": [],
        "owner_evidence": {},
    }
    Path(results_dir).mkdir(parents=True, exist_ok=True)
    (Path(results_dir) / "results.json").write_text(json.dumps(data, ensure_ascii=False, indent=1), encoding="utf-8")
    return data


def record_row(results_dir: Path, *, name: str, rc: int | None, log: Path, argv: list[str], started_at: str,
               ended_at: str, l1_results: Path | None = None, platform: str = "linux",
               evidence: list[Path] = ()) -> dict[str, Any]:
    """只記錄、不判定：同名列覆寫；測試列附 `required_set`（列名對應集合）與 l1 的測試計數。"""
    results_dir = Path(results_dir)
    path = results_dir / "results.json"
    data = json.loads(path.read_text(encoding="utf-8"))
    row: dict[str, Any] = {
        "name": name, "argv": argv, "rc": rc, "started_at": started_at, "ended_at": ended_at,
        "log_path": _rel_to(results_dir, log), "log_sha256": _sha256(Path(log)), "platform": platform,
        "evidence": [{"path": _rel_to(results_dir, e), "sha256": _sha256(Path(e))} for e in evidence],
    }
    if name in TEST_ROWS:
        row["required_set"] = expected_set(name)
        counts = {"passed": 0, "failed": 0, "ignored": 0}
        if l1_results is not None and Path(l1_results).is_file():
            row["l1_results"] = _rel_to(results_dir, l1_results)
            try:
                rep = json.loads(Path(l1_results).read_text(encoding="utf-8"))
            except ValueError:
                rep = {}
            for r in rep.get("results") or []:
                o = r.get("outcome")
                key = "passed" if o == "passed" else "failed" if o in ("failed", "error") else "ignored"
                counts[key] += 1
        row["test_counts"] = counts
    data["rows"] = [r for r in data.get("rows") or [] if r.get("name") != name] + [row]
    path.write_text(json.dumps(data, ensure_ascii=False, indent=1), encoding="utf-8")
    return row


def record_windows_ev01(results_dir: Path, *, report_path: Path | None, bundle_dir: Path | None,
                        log_path: Path) -> dict[str, Any]:
    """把外部 Windows ev-01 報告與 Stage 2 bundle 複製進 P7 run，供 build／verify 重算。"""
    results_dir = Path(results_dir)
    log_path = Path(log_path)
    log_path.parent.mkdir(parents=True, exist_ok=True)
    evidence: list[Path] = []
    snapshot: dict[str, Any] = {}
    messages: list[str] = []
    destination = results_dir / "windows-ev01"
    if report_path is None:
        messages.append("未指定 Windows ev-01 report")
    else:
        source_report = Path(report_path)
        if source_report.name != "func-test-report.json" or source_report.parent.name != "basic":
            messages.append("report 路徑必須是 basic/func-test-report.json")
        else:
            source_meta = source_report.parent.parent / "win-func-test-meta.json"
            if not source_report.is_file() or not source_meta.is_file():
                messages.append("缺 basic/func-test-report.json 或 win-func-test-meta.json")
            else:
                report_copy = destination / "reports/basic/func-test-report.json"
                meta_copy = destination / "reports/win-func-test-meta.json"
                try:
                    report_copy.parent.mkdir(parents=True, exist_ok=True)
                    shutil.copy2(source_report, report_copy)
                    shutil.copy2(source_meta, meta_copy)
                except OSError as exc:
                    snapshot["snapshot_error"] = f"Windows report/meta 快照失敗：{exc}"
                    messages.append(snapshot["snapshot_error"])
                else:
                    evidence.extend((report_copy, meta_copy))
                    snapshot["report_path"] = _rel_to(results_dir, report_copy)
                    messages.append("已快照 basic report 與 metadata")
    if bundle_dir is None:
        messages.append("未指定 Windows Stage 2 bundle")
    else:
        source_bundle = Path(bundle_dir)
        try:
            windows_bundle_identity(source_bundle)
            bundle_copy = destination / "bundle"
            shutil.copytree(source_bundle, bundle_copy, ignore=shutil.ignore_patterns(".mkbundle-owner"))
            snapshot["bundle_path"] = _rel_to(results_dir, bundle_copy)
            messages.append("已快照 Windows bundle")
        except (OSError, ValueError, shutil.Error) as exc:
            snapshot["snapshot_error"] = f"Windows bundle 快照失敗：{exc}"
            messages.append(snapshot["snapshot_error"])
    log_path.write_text("\n".join(messages) + "\n", encoding="utf-8")
    evidence.append(log_path)
    imported_at = dt.datetime.now(dt.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    row = {
        "name": WINDOWS_EV01_ROW,
        "argv": ["import-win-func-test", str(report_path or ""), str(bundle_dir or "")],
        "rc": 0,
        "started_at": imported_at,
        "ended_at": imported_at,
        "log_path": _rel_to(results_dir, log_path),
        "log_sha256": _sha256(log_path),
        "platform": "windows",
        "evidence": [{"path": _rel_to(results_dir, item), "sha256": _sha256(item)} for item in evidence],
        "windows_ev01": snapshot,
    }
    result_file = results_dir / "results.json"
    data = json.loads(result_file.read_text(encoding="utf-8"))
    data["rows"] = [item for item in data.get("rows") or [] if item.get("name") != WINDOWS_EV01_ROW] + [row]
    result_file.write_text(json.dumps(data, ensure_ascii=False, indent=1), encoding="utf-8")
    return row


def record_breaker(results_dir: Path, breaker_json: Path, *, exit_code: int) -> dict[str, Any]:
    """將 slice_breaker 評估輸出及其 SHA-256 納入本 gate 的結果與可重算報告。"""
    results_dir = Path(results_dir)
    path = results_dir / "results.json"
    data = json.loads(path.read_text(encoding="utf-8"))
    source = Path(breaker_json)
    breaker = json.loads(source.read_text(encoding="utf-8"))
    if not isinstance(breaker, Mapping):
        raise ValueError("breaker.json 根節點必須是 object")
    if not isinstance(breaker.get("actual_verdict"), str) or not isinstance(breaker.get("gate_verdict"), str):
        raise ValueError("breaker.json 缺 actual_verdict／gate_verdict")
    data["breaker"] = dict(breaker)
    data["breaker_evidence"] = {"path": _rel_to(results_dir, source), "sha256": _sha256(source)}
    data["breaker_exit_code"] = exit_code
    path.write_text(json.dumps(data, ensure_ascii=False, indent=1), encoding="utf-8")
    return data["breaker"]


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    sub = ap.add_subparsers(dest="cmd", required=True)
    i = sub.add_parser("init")
    i.add_argument("--results-dir", required=True, type=Path)
    i.add_argument("--run-id", required=True)
    i.add_argument("--caps-missing", nargs="*", default=[])
    i.add_argument("--artifact", nargs="*", default=[], type=Path)
    i.add_argument("--prior-slice-artifact", nargs=2, action="append", default=[], metavar=("NAME", "PATH"))
    r = sub.add_parser("record")
    r.add_argument("--results-dir", required=True, type=Path)
    r.add_argument("--name", required=True)
    r.add_argument("--rc", type=int)
    r.add_argument("--log", required=True, type=Path)
    r.add_argument("--argv-json", required=True)
    r.add_argument("--started-at", required=True)
    r.add_argument("--ended-at", required=True)
    r.add_argument("--l1-results", type=Path)
    r.add_argument("--platform", default="linux")
    r.add_argument("--evidence", nargs="*", default=[], type=Path)
    b = sub.add_parser("build")
    b.add_argument("--results-dir", required=True, type=Path)
    b.add_argument("--l1-lock", type=Path)
    b.add_argument("--out", type=Path)
    v = sub.add_parser("verify")
    v.add_argument("--results-dir", required=True, type=Path)
    v.add_argument("--md", required=True, type=Path)
    v.add_argument("--l1-lock", type=Path)
    br = sub.add_parser("record-breaker")
    br.add_argument("--results-dir", required=True, type=Path)
    br.add_argument("--breaker-json", required=True, type=Path)
    br.add_argument("--exit-code", required=True, type=int)
    we = sub.add_parser("record-windows-ev01")
    we.add_argument("--results-dir", required=True, type=Path)
    we.add_argument("--report", type=Path)
    we.add_argument("--bundle", type=Path)
    we.add_argument("--log", required=True, type=Path)
    wi = sub.add_parser("windows-bundle-identity")
    wi.add_argument("--bundle", required=True, type=Path)
    args = ap.parse_args(argv)
    if args.cmd == "init":
        init_results(args.results_dir, args.run_id, caps_missing=args.caps_missing, artifacts=args.artifact,
                     prior_slice_artifacts=[(name, Path(path)) for name, path in args.prior_slice_artifact])
        return 0
    if args.cmd == "record":
        record_row(args.results_dir, name=args.name, rc=args.rc, log=args.log, argv=json.loads(args.argv_json),
                   started_at=args.started_at, ended_at=args.ended_at, l1_results=args.l1_results,
                   platform=args.platform, evidence=args.evidence)
        return 0
    if args.cmd == "build":
        md = build_report(args.results_dir, l1_lock=args.l1_lock)
        if args.out:
            args.out.write_text(md, encoding="utf-8", newline="\n")
        else:
            sys.stdout.write(md)
        return 0
    if args.cmd == "record-breaker":
        record_breaker(args.results_dir, args.breaker_json, exit_code=args.exit_code)
        return 0
    if args.cmd == "record-windows-ev01":
        record_windows_ev01(args.results_dir, report_path=args.report, bundle_dir=args.bundle, log_path=args.log)
        return 0
    if args.cmd == "windows-bundle-identity":
        sys.stdout.write(json.dumps(windows_bundle_identity(args.bundle), sort_keys=True) + "\n")
        return 0
    problems = verify_report(args.md, args.results_dir, l1_lock=args.l1_lock)
    for p in problems:
        print(p, file=sys.stderr)
    return 1 if problems else 0


if __name__ == "__main__":
    sys.exit(main())
