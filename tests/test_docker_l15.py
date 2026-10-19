"""OD-15 docker 驗證環境與兩容器拓樸的失敗測試（P2 Task 17，T17-1）。

涵蓋：compose 拓樸與裝置隔離、runner 的 --dry-run 輸出（沙箱 by-id、證據標記、systemd drop-in 斷言）、
長跑與監管 runner 的 P1 前置條件與隔離、證據索引的 evidence_layer 規則（經 check_evidence_index.py）、
報告 uart_kind（resource_report_plugin）、以及能力式 SKIP 的兩容器真跑 smoke。

模組層只放平台 marker；資源 marker 逐一標在測試函式上（tests/test_resource_markers.py 的 mixed-scope 規則）。
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

pytestmark = pytest.mark.core_portable

REPO = Path(__file__).resolve().parents[1]
COMPOSE = REPO / "tools" / "docker" / "compose.l15.yaml"
SCRIPTS = REPO / "scripts" / "docker"
RUN_L15 = SCRIPTS / "run-l15.sh"
RUN_SYSTEMD = SCRIPTS / "run-systemd.sh"
RUN_LONGRUN = SCRIPTS / "run-longrun.sh"
RUN_SUPERVISION = SCRIPTS / "run-supervision.sh"
ACCEPTANCE = REPO / "tests" / "acceptance"
CHECKER = ACCEPTANCE / "check_evidence_index.py"
BC_PATH = REPO / "contracts" / "behavior_changes.yaml"
RESOURCE_PLUGIN = REPO / "tests" / "resource_report_plugin.py"
REAL_LAYERS = ("L2-bench", "L3-bench", "hw-bench")
FORBIDDEN_DEVICES = ("/dev/ttyUSB", "/dev/ttyACM", "/dev/serial")

# 報告外掛在子行程內以 spec_from_file_location 載入（寫法同 Task 13／P3 黑箱 conftest，不依賴 root conftest）
_CONFTEST_TEMPLATE = '''\
import importlib.util

_spec = importlib.util.spec_from_file_location("sw_resource_report", {plugin})
_plugin = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(_plugin)


def pytest_configure(config):
    if not config.pluginmanager.has_plugin("sw_resource_report"):
        config.pluginmanager.register(_plugin, "sw_resource_report")
'''


def _require(path: Path, owner: str) -> None:
    """依賴的交付物尚不存在時以明確訊息紅燈，避免被 exit code 2 之類的巧合誤判為通過。"""
    assert path.is_file(), f"{path.relative_to(REPO)} 尚不存在（{owner}）"


def _tail(proc: subprocess.CompletedProcess) -> str:
    return (f"rc={proc.returncode}\n--- stdout ---\n{proc.stdout[-1500:]}\n"
            f"--- stderr ---\n{proc.stderr[-1500:]}")


def _env(extra: dict[str, str] | None = None) -> dict[str, str]:
    env = dict(os.environ)
    env.pop("SW_BC_PATH", None)
    env.update(extra or {})
    return env


def _run_script(script: Path, *args: str, env: dict[str, str] | None = None, timeout: int = 120):
    _require(script, "Task 17 runner")
    return subprocess.run(["bash", str(script), *args], cwd=REPO, env=_env(env),
                          capture_output=True, text=True, timeout=timeout)


def _compose_services() -> dict:
    import yaml  # noqa: PLC0415

    _require(COMPOSE, "Task 17 compose 拓樸")
    return yaml.safe_load(COMPOSE.read_text(encoding="utf-8"))["services"]


def _compose_project(text: str) -> str | None:
    for line in text.splitlines():
        if "docker compose" in line:
            m = re.search(r"\s-p\s+(\S+)", line)
            if m:
                return m.group(1)
    return None


def _volume_sources(text: str) -> set[str]:
    return {m.group(1) for m in re.finditer(r"(?:^|\s)-v\s+([^\s:]+):", text, re.MULTILINE)}


def _set_bc_status(node, bc_id: str, status: str) -> bool:
    if isinstance(node, dict):
        if node.get("id") == bc_id:
            node["status"] = status
            return True
        return any(_set_bc_status(v, bc_id, status) for v in node.values())
    if isinstance(node, list):
        return any(_set_bc_status(v, bc_id, status) for v in node)
    return False


def _valid_p1_gate_env(tmp_path: Path) -> dict[str, str]:
    import yaml  # noqa: PLC0415

    changes_path = tmp_path / "behavior_changes.yaml"
    codes_path = tmp_path / "error_codes.yaml"
    changes_path.write_text(yaml.safe_dump({"changes": [
        {"id": item, "status": "IMPLEMENTED"}
        for item in ("BC-01", "BC-02", "BC-03", "BC-04", "BC-05", "SC-34", "SC-35")
    ]}), encoding="utf-8")
    codes_path.write_text(yaml.safe_dump({"codes": [{"code": "DRAIN_STALLED"}]}), encoding="utf-8")
    return {"SW_BC_PATH": str(changes_path), "SW_ERROR_CODES_PATH": str(codes_path)}


def _bundle_fixture(path: Path) -> Path:
    path.mkdir(parents=True, exist_ok=True)
    binaries = path / "bin"
    binaries.mkdir()
    files = {}
    for name in ("serialwrap", "serialwrapd"):
        target = binaries / name
        target.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
        target.chmod(0o755)
        files[name] = hashlib.sha256(target.read_bytes()).hexdigest()
    zeros40 = "0" * 40
    zeros64 = "0" * 64
    manifest = {
        "schema_version": 1, "bundle_version": "0.1.0", "stage": "test", "created_at": "fixture",
        "target": "linux", "git_revision": zeros40, "contract_revision": zeros40,
        "components": {
            "cli": {"implementation": "rust", "version": "0.1.0", "git_revision": zeros40,
                    "path": "bin/serialwrap", "sha256": files["serialwrap"]},
            "daemon": {"implementation": "rust", "version": "0.1.0", "git_revision": zeros40,
                       "launcher": {"path": "bin/serialwrapd", "sha256": files["serialwrapd"]},
                       "entry": ["bin/serialwrapd"],
                       "runtime": {"path": "runtime", "python_version": "3.12", "sha256": zeros64},
                       "package": {"name": "serialwrap", "version": "0.1.0", "wheel_sha256": zeros64,
                                   "tree_sha256": zeros64}},
            "python_cli_delegate": {"entry": ["python3"], "tree_sha256": zeros64},
            "assets": {"skill_sha256": zeros64, "skill_windows_sha256": zeros64, "profiles_sha256": zeros64},
            "supervisor": {"default_mode": "on-demand", "definition_sha256": zeros64},
        },
        "pairing": {"rule": "exact", "cli_version": "0.1.0", "daemon_version": "0.1.0"},
    }
    (path / "release-manifest.json").write_text(json.dumps(manifest), encoding="utf-8")
    return path


def _checker_run(tmp_path: Path, report: dict, required_layer: str | None) -> subprocess.CompletedProcess:
    """保留 Stage 0 正式索引，只追加不承接 case 覆蓋的合成標記項目。"""
    import yaml  # noqa: PLC0415

    _require(CHECKER, "Task 12 check_evidence_index.py")
    repo = tmp_path / "repo"
    shutil.copytree(ACCEPTANCE, repo / "tests" / "acceptance")
    evidence = tmp_path / "evidence"
    rel = Path("report") / "layer.json"
    (evidence / rel).parent.mkdir(parents=True, exist_ok=True)
    payload = json.dumps(report, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")
    (evidence / rel).write_bytes(payload)
    item = {
        "id": "S0-SYNTH-LAYER",
        "requirement": "OD-15 證據層級合成項",
        "spec_ref": "OD-15",
        "case_ids": [],
        "artifact_path": rel.as_posix(),
        "sha256": hashlib.sha256(payload).hexdigest(),
        "status": "present",
        "owner": "test",
    }
    if required_layer is not None:
        item["required_layer"] = required_layer
    # 保留正式 Stage 0 分母與 case 覆蓋，只追加單一合成項目供層級規則測試。
    index = yaml.safe_load((repo / "tests" / "acceptance" / "stage0_evidence_index.yaml").read_text(encoding="utf-8"))
    index["items"].append(item)
    (repo / "tests" / "acceptance" / "stage0_evidence_index.yaml").write_text(
        yaml.safe_dump(index, allow_unicode=True, sort_keys=False), encoding="utf-8")
    return subprocess.run(
        [sys.executable, str(repo / "tests" / "acceptance" / "check_evidence_index.py"),
         "--stage", "0", "--root", str(evidence),
         "--index", str(repo / "tests" / "acceptance" / "stage0_evidence_index.yaml"),
         "--cases", str(repo / "tests" / "acceptance" / "cases.yaml")],
        cwd=repo, capture_output=True, text=True, timeout=120,
    )


def _docker_available() -> bool:
    if shutil.which("docker") is None:
        return False
    try:
        return subprocess.run(["docker", "info"], capture_output=True, timeout=60).returncode == 0
    except (OSError, subprocess.TimeoutExpired):
        return False


@pytest.mark.memory_only
def test_compose_two_app_containers():
    services = _compose_services()
    default = {name for name, svc in services.items() if not svc.get("profiles")}
    assert default == {"serialwrap", "dut-sim"}
    dut = services["dut-sim"]
    assert dut.get("stdin_open") is True
    assert dut.get("tty", False) is False
    assert dut.get("network_mode") == "none"


@pytest.mark.memory_only
def test_compose_no_real_serial_devices():
    services = _compose_services()
    for name, svc in services.items():
        for key in ("devices", "volumes"):
            for entry in svc.get(key) or []:
                text = entry if isinstance(entry, str) else json.dumps(entry)
                for bad in FORBIDDEN_DEVICES:
                    assert bad not in text, f"{name}.{key} 含真實裝置路徑 {bad}"
        assert svc.get("privileged", False) is False, f"{name} 不得 privileged"
        assert "cap_add" not in svc, f"{name} 不得新增超出 systemd 容器政策的 capabilities"
        # systemd profile 的例外只允許 cgroup: host 與 /sys/fs/cgroup 掛載；其他服務一律不得使用
        systemd = any(str(p).startswith("systemd") for p in (svc.get("profiles") or []))
        if systemd:
            if "cgroup" in svc:
                assert svc["cgroup"] == "host", f"{name} 的 cgroup 只允許 host"
        else:
            assert "cgroup" not in svc, f"{name} 非 systemd profile 不得設定 cgroup"
            assert "/sys/fs/cgroup" not in json.dumps(svc.get("volumes") or []), \
                f"{name} 非 systemd profile 不得掛載 /sys/fs/cgroup"


@pytest.mark.process_rpc
def test_run_l15_dry_run_uses_sandbox_by_id():
    r = _run_script(RUN_L15, "pytest-physical-uart", "--dry-run")
    assert r.returncode == 0, _tail(r)
    assert "SERIALWRAP_BY_ID_DIR=/run/sw-fake/by-id" in r.stdout
    assert "SW_UART_KIND=pty-fake" in r.stdout
    assert "/dev/serial/by-id" not in r.stdout


@pytest.mark.process_rpc
def test_run_l15_dry_run_labels_report():
    r = _run_script(RUN_L15, "pytest-physical-uart", "--dry-run")
    assert r.returncode == 0, _tail(r)
    assert "--evidence-layer L1.5-docker" in r.stdout
    assert "-m posix_pty" in r.stdout
    assert "-m physical_uart" not in r.stdout


@pytest.mark.process_rpc
def test_run_l15_dry_run_uses_full_suite_by_default(tmp_path):
    bundle = _bundle_fixture(tmp_path / "bundle")
    r = _run_script(RUN_L15, "func-test-bridge", "--dry-run", "--bundle", str(bundle))
    assert r.returncode == 0, _tail(r)
    assert "--scenarios" in r.stdout
    assert "--case" not in r.stdout
    assert f"{bundle}:/opt/sw-bundle:ro" in r.stdout


@pytest.mark.process_rpc
def test_run_l15_refuses_invalid_bundle_before_dry_run(tmp_path):
    r = _run_script(RUN_L15, "func-test-bridge", "--dry-run", "--bundle", str(tmp_path))
    assert r.returncode == 2, _tail(r)
    assert "bundle manifest 無效" in r.stderr


@pytest.mark.process_rpc
@pytest.mark.parametrize(
    ("suite", "expected"),
    [("realhw-p0p1", "--tier p0\\,p1"), ("regression-nondestructive", "testpilot --root")],
)
def test_run_l15_dry_run_uses_suite_entrypoint(suite, expected):
    r = _run_script(RUN_L15, suite, "--dry-run")
    assert r.returncode == 0, _tail(r)
    assert expected in r.stdout


@pytest.mark.process_rpc
def test_run_systemd_dry_run_asserts_dropin(tmp_path):
    r = _run_script(RUN_SYSTEMD, "--mode", "systemd-user", "--dry-run", env=_valid_p1_gate_env(tmp_path))
    assert r.returncode == 0, _tail(r)
    assert "systemctl --user show serialwrap -p Restart -p RefuseManualStart" in r.stdout
    assert "Restart=no" in r.stdout
    assert "RefuseManualStart=yes" in r.stdout
    assert "journalctl --user -u serialwrap -o cat" in r.stdout
    assert "build_dut_image.sh" not in r.stdout


@pytest.mark.process_rpc
def test_run_systemd_system_dry_run_uses_system_scope(tmp_path):
    r = _run_script(RUN_SYSTEMD, "--mode", "systemd-system", "--dry-run",
                    env=_valid_p1_gate_env(tmp_path))
    assert r.returncode == 0, _tail(r)
    assert "systemctl show serialwrap -p Restart -p RefuseManualStart" in r.stdout
    assert "journalctl -u serialwrap -o cat" in r.stdout
    assert "systemctl --user" not in r.stdout


@pytest.mark.process_rpc
def test_longrun_and_supervision_isolated(tmp_path):
    gate_env = _valid_p1_gate_env(tmp_path)
    lr = _run_script(RUN_LONGRUN, "--hours", "48", "--dry-run", env=gate_env)
    sv = _run_script(RUN_SUPERVISION, "--hours", "72", "--mode", "systemd-user", "--dry-run", env=gate_env)
    assert lr.returncode == 0, _tail(lr)
    assert sv.returncode == 0, _tail(sv)
    lr_project = _compose_project(lr.stdout)
    sv_project = _compose_project(sv.stdout)
    assert lr_project and sv_project and lr_project != sv_project
    assert _volume_sources(lr.stdout).isdisjoint(_volume_sources(sv.stdout))


@pytest.mark.process_rpc
def test_longrun_refuses_before_p1(tmp_path):
    import yaml  # noqa: PLC0415

    doc = yaml.safe_load(BC_PATH.read_text(encoding="utf-8"))
    assert _set_bc_status(doc, "BC-01", "APPROVED"), "contracts/behavior_changes.yaml 找不到 BC-01"
    bc_copy = tmp_path / "behavior_changes.yaml"
    bc_copy.write_text(yaml.safe_dump(doc, allow_unicode=True, sort_keys=False), encoding="utf-8")
    r = _run_script(RUN_LONGRUN, "--hours", "48", "--dry-run", env={"SW_BC_PATH": str(bc_copy)})
    assert r.returncode == 3, _tail(r)
    assert "P1 未合併：BC-01 非 IMPLEMENTED" in r.stderr


@pytest.mark.process_rpc
def test_supervision_refuses_before_p1(tmp_path):
    import yaml  # noqa: PLC0415

    doc = yaml.safe_load(BC_PATH.read_text(encoding="utf-8"))
    for item in ("BC-01", "BC-02", "BC-03", "BC-04", "BC-05"):
        assert _set_bc_status(doc, item, "IMPLEMENTED"), f"找不到 {item}"
    bc_copy = tmp_path / "behavior_changes.yaml"
    ec_copy = tmp_path / "error_codes.yaml"
    bc_copy.write_text(yaml.safe_dump(doc, allow_unicode=True, sort_keys=False), encoding="utf-8")
    ec_copy.write_text("codes: []\n", encoding="utf-8")
    env = {"SW_BC_PATH": str(bc_copy), "SW_ERROR_CODES_PATH": str(ec_copy)}
    r = _run_script(RUN_SUPERVISION, "--hours", "72", "--mode", "systemd-user", "--dry-run", env=env)
    assert r.returncode == 3, _tail(r)
    assert "P1-OC 未合併" in r.stderr


@pytest.mark.process_rpc
@pytest.mark.parametrize(
    ("required", "layer", "producer"),
    [(req, lay, prod) for req in REAL_LAYERS
     for lay, prod in (("L1.5-docker", "run-l15.sh"), ("host-throwaway", "win-task-throwaway.ps1"))],
)
def test_evidence_index_rejects_docker_for_l2(tmp_path, required, layer, producer):
    r = _checker_run(tmp_path, {"producer": producer, "evidence_layer": layer, "uart_kind": "native",
                                "results": [{"case_id": "probe", "verdict": "PASS"}]}, required)
    assert r.returncode == 5, _tail(r)
    assert "evidence_layer_mismatch" in r.stdout, _tail(r)


@pytest.mark.process_rpc
def test_evidence_index_fail_closed_missing_layer(tmp_path):
    r = _checker_run(tmp_path, {"producer": "run-longrun.sh"}, "L1.5-docker")
    assert r.returncode == 5, _tail(r)
    assert "evidence_layer_missing" in r.stdout, _tail(r)


@pytest.mark.process_rpc
@pytest.mark.parametrize("report", [
    {"producer": "run-l15.sh", "evidence_layer": ["L1.5-docker"]},
    {"producer": "run-l15.sh", "evidence_layer": "L1.5-docker", "uart_kind": "pty-fake",
     "results": [{"case_id": "probe", "verdict": "FAIL"}]},
    {"producer": "run-l15.sh", "evidence_layer": "L1.5-docker", "uart_kind": "pty-fake",
     "results": [{"case_id": "probe", "verdict": "INSUFFICIENT_EVIDENCE"}]},
])
def test_evidence_index_rejects_invalid_docker_report(tmp_path, report):
    r = _checker_run(tmp_path, report, "L1.5-docker")
    assert r.returncode == 5, _tail(r)
    assert any(token in r.stdout for token in (
        "evidence_layer_unknown", "evidence_report_invalid", "evidence_not_pass",
        "evidence_topology_invalid", "evidence_image_digests_invalid",
    )), _tail(r)


@pytest.mark.process_rpc
def test_evidence_index_requires_required_layer(tmp_path):
    r = _checker_run(tmp_path, {"producer": "run-l15.sh", "evidence_layer": "L1.5-docker"}, None)
    assert r.returncode == 2, _tail(r)
    assert "schema_invalid" in r.stderr, _tail(r)


@pytest.mark.process_rpc
def test_resource_report_uart_kind(tmp_path):
    _require(RESOURCE_PLUGIN, "Task 13 resource_report_plugin")
    sub = tmp_path / "sub"
    sub.mkdir()
    (sub / "conftest.py").write_text(_CONFTEST_TEMPLATE.format(plugin=json.dumps(str(RESOURCE_PLUGIN))),
                                     encoding="utf-8")
    (sub / "test_ok.py").write_text("def test_ok():\n    assert True\n", encoding="utf-8")
    for uart, expect in (("pty-fake", "pty-fake"), (None, "native")):
        report = tmp_path / f"r-{expect}.json"
        env = dict(os.environ)
        env.pop("SW_UART_KIND", None)
        env.pop("SW_RESOURCE_REPORT", None)
        env["SW_RESOURCE_REPORT"] = str(report)
        if uart is not None:
            env["SW_UART_KIND"] = uart
        r = subprocess.run(
            [sys.executable, "-m", "pytest", "-q", str(sub), f"--confcutdir={sub}", "-p", "no:cacheprovider"],
            cwd=tmp_path, env=env, capture_output=True, text=True, timeout=180,
        )
        assert r.returncode == 0, _tail(r)
        assert report.is_file(), "resource_report_plugin 未寫出 SW_RESOURCE_REPORT 報告"
        data = json.loads(report.read_text(encoding="utf-8"))
        assert data["uart_kind"] == expect


@pytest.mark.process_rpc
def test_docker_smoke_two_container(tmp_path):
    # 本檔未註冊 docker marker（P0 T14 註冊表）；能力缺失以執行期能力式 SKIP 表達
    if not _docker_available():
        pytest.skip("cap_missing:docker：本機無可用 docker daemon（能力式 SKIP，證據不足，非 PASS）")
    r = _run_script(RUN_L15, "func-test-bridge", "--case", "sm-01-attach-to-ready", "--out", str(tmp_path),
                    timeout=900)
    assert r.returncode == 0, _tail(r)
    report_path = tmp_path / "l15-docker-report.json"
    assert report_path.is_file(), "run-l15.sh 未寫出 l15-docker-report.json"
    report = json.loads(report_path.read_text(encoding="utf-8"))
    assert report["evidence_layer"] == "L1.5-docker"
    assert report["topology"] == "docker-2c"
    digests = report["image_digests"]
    assert len(digests) == 3
    assert all(str(v).startswith("sha256:") for v in digests.values())
    verdicts = {row.get("case_id"): row.get("verdict") for row in report.get("results", [])}
    assert verdicts.get("sm-01-attach-to-ready") == "PASS", _tail(r)


@pytest.mark.memory_only
def test_l15_suite_runtime_import_and_cli_paths_match_serialwrap_image():
    """回歸：容器內 runner 使用的 runtime 模組必須有 import，預設 CLI／daemon 路徑須與 serialwrap image 安裝位置一致。

    serialwrap.Dockerfile 以容器系統 Python 安裝 repo（不建 /opt/serialwrap/.venv），console script 位於
    /usr/local/bin；runner 若仍指向 .venv，func-test 會以 FileNotFoundError 失敗、realhw／regression suite
    會因未 import runtime 而 NameError。
    """
    import ast  # noqa: PLC0415

    suite = SCRIPTS / "run_l15_suite.py"
    tree = ast.parse(suite.read_text(encoding="utf-8"))
    used = {node.value.id for node in ast.walk(tree)
            if isinstance(node, ast.Attribute) and isinstance(node.value, ast.Name)}
    imported = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            imported |= {(alias.asname or alias.name).split(".")[0] for alias in node.names}
        elif isinstance(node, ast.ImportFrom):
            imported |= {alias.asname or alias.name for alias in node.names}
    assert "runtime" in used and "runtime" in imported, "run_l15_suite.py 使用 runtime.* 但未 import"

    dockerfile = (REPO / "tools" / "docker" / "serialwrap.Dockerfile").read_text(encoding="utf-8")
    image_has_venv = "/opt/serialwrap/.venv" in dockerfile
    defaults = [node.args[1].value for node in ast.walk(tree)
                if isinstance(node, ast.Call) and getattr(node.func, "attr", "") == "get"
                and len(node.args) == 2 and isinstance(node.args[0], ast.Constant)
                and node.args[0].value in {"SERIALWRAP_CLI_EXE", "SERIALWRAP_DAEMON_EXE"}]
    assert len(defaults) == 2, defaults
    regression_cli = re.search(r"^\s*CONTAINER_CLI=(\S+)$", RUN_L15.read_text(encoding="utf-8"), re.M)
    assert regression_cli is not None
    for path in [*defaults, regression_cli.group(1)]:
        assert image_has_venv or "/.venv/" not in path, f"{path} 指向 serialwrap image 不存在的 venv"
