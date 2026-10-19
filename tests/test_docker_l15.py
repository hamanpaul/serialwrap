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


def _checker_run(tmp_path: Path, report: dict, required_layer: str | None) -> subprocess.CompletedProcess:
    """建立含合成證據索引的暫存 repo（tests/acceptance 整棵複製後覆寫 stage0 索引），以 --stage 0 執行 checker。"""
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
        "case_ids": ["sm-01-attach-to-ready"],
        "artifact_path": rel.as_posix(),
        "sha256": hashlib.sha256(payload).hexdigest(),
        "status": "present",
        "owner": "test",
    }
    if required_layer is not None:
        item["required_layer"] = required_layer
    index = {"schema_version": 1, "stage": "0", "items": [item]}
    (repo / "tests" / "acceptance" / "stage0_evidence_index.yaml").write_text(
        yaml.safe_dump(index, allow_unicode=True, sort_keys=False), encoding="utf-8")
    return subprocess.run(
        [sys.executable, str(repo / "tests" / "acceptance" / "check_evidence_index.py"),
         "--stage", "0", "--root", str(evidence)],
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


@pytest.mark.process_rpc
def test_run_systemd_dry_run_asserts_dropin():
    r = _run_script(RUN_SYSTEMD, "--mode", "systemd-user", "--dry-run")
    assert r.returncode == 0, _tail(r)
    assert "systemctl --user show serialwrap -p Restart -p RefuseManualStart" in r.stdout
    assert "Restart=no" in r.stdout
    assert "RefuseManualStart=yes" in r.stdout
    assert "journalctl --user -u serialwrap -o cat" in r.stdout


@pytest.mark.process_rpc
def test_longrun_and_supervision_isolated():
    lr = _run_script(RUN_LONGRUN, "--hours", "48", "--dry-run")
    sv = _run_script(RUN_SUPERVISION, "--hours", "72", "--mode", "systemd-user", "--dry-run")
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
@pytest.mark.parametrize(
    ("required", "layer", "producer"),
    [(req, lay, prod) for req in REAL_LAYERS
     for lay, prod in (("L1.5-docker", "run-l15.sh"), ("host-throwaway", "win-task-throwaway.ps1"))],
)
def test_evidence_index_rejects_docker_for_l2(tmp_path, required, layer, producer):
    r = _checker_run(tmp_path, {"producer": producer, "evidence_layer": layer}, required)
    assert r.returncode == 5, _tail(r)
    assert "evidence_layer_mismatch" in r.stdout, _tail(r)


@pytest.mark.process_rpc
def test_evidence_index_fail_closed_missing_layer(tmp_path):
    r = _checker_run(tmp_path, {"producer": "run-longrun.sh"}, "L1.5-docker")
    assert r.returncode == 5, _tail(r)
    assert "evidence_layer_missing" in r.stdout, _tail(r)


@pytest.mark.process_rpc
def test_evidence_index_requires_required_layer(tmp_path):
    r = _checker_run(tmp_path, {"producer": "run-l15.sh", "evidence_layer": "L1.5-docker"}, None)
    assert r.returncode == 2, _tail(r)


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
