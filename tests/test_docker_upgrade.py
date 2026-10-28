"""OD-33 R2(b)(c)：0.3.2 正式版 → 0.4.0 候選的升級驗證（Linux systemd docker 容器）。

判定邏輯（`scripts/docker/upgrade_checks.py`）以純函式測試：daemon 行程辨識（不得把含 `serialwrapd` 字樣的
shell 指令或 launcher→python 父子算成兩個 daemon）、雙 daemon（two-reader）重疊、設定／profiles 逐位元組保留、
alias／binding 語意相容與整體 verdict（缺項 INSUFFICIENT、任一 FAIL 即 FAIL）。
host runner `scripts/docker/run-upgrade.sh` 以 `--dry-run` 檢查不碰 docker 的命令組成；實跑測試在 docker 可用時
於 OD-16 (5) 的 ubuntu:24.04 systemd 容器中，對 systemd-user 與 on-demand 兩種監管模式各跑一次完整流程。

同檔混合平台與資源（純函式為 core_portable／memory_only；bash runner 與 docker 實跑為 posix_only），
依 Task 14 規則 2 模組層不放 marker，逐一標在測試函式上。
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[1]
RUN_UPGRADE = REPO / "scripts" / "docker" / "run-upgrade.sh"
DOCKERFILE = REPO / "tools" / "docker" / "upgrade.Dockerfile"
SYSTEMD_DOCKERFILE = REPO / "tools" / "docker" / "systemd.Dockerfile"

sys.path.insert(0, str(REPO))

from scripts.docker import upgrade_checks as uc  # noqa: E402


def _proc(root: Path, pid: int, argv: list[str], ppid: int = 1) -> None:
    d = root / str(pid)
    d.mkdir(parents=True)
    (d / "cmdline").write_bytes(b"\0".join(a.encode() for a in argv) + b"\0")
    (d / "stat").write_text(f"{pid} (x) S {ppid} {pid} {pid} 0 -1\n", encoding="utf-8")


# ---------- daemon 行程辨識 ----------

@pytest.mark.core_portable
@pytest.mark.memory_only
@pytest.mark.parametrize("argv", [
    ["/home/swtest/.local/share/pipx/venvs/serialwrap/bin/python", "/home/swtest/.local/bin/serialwrapd"],
    ["/home/swtest/.local/share/serialwrap/versions/0.4.0/py/bin/python3", "-I", "-B",
     "/home/swtest/.local/share/serialwrap/versions/0.4.0/py/serialwrapd.py"],
    ["/home/swtest/.local/share/serialwrap/versions/0.4.0/bin/serialwrapd"],
    ["python3", "-m", "sw_core.daemon", "--socket", "/run/x.sock"],
])
def test_daemon_argv_recognized(argv):
    assert uc.is_daemon_argv(argv), argv


@pytest.mark.core_portable
@pytest.mark.memory_only
@pytest.mark.parametrize("argv", [
    ["bash", "-lc", "serialwrap daemon stop; ls ~/.local/bin/serialwrapd"],
    ["runuser", "-u", "swtest", "--", "/home/swtest/.local/bin/serialwrapd"],
    ["/home/swtest/.local/bin/serialwrap", "daemon", "start"],
    ["python3", "/opt/sw-driver/run_upgrade_container.py", "--mode", "serialwrapd"],
    ["grep", "serialwrapd"],
    [],
])
def test_non_daemon_argv_not_recognized(argv):
    assert not uc.is_daemon_argv(argv), argv


@pytest.mark.core_portable
@pytest.mark.memory_only
def test_scan_counts_launcher_child_as_one_instance(tmp_path):
    """launcher 以子行程啟動 python daemon（父子皆可辨識為 daemon）只算一個實例；兩個無親子關係者算兩個。"""
    _proc(tmp_path, 10, ["/v/0.4.0/bin/serialwrapd"])
    _proc(tmp_path, 11, ["/v/0.4.0/py/bin/python3", "-I", "-B", "/v/0.4.0/py/serialwrapd.py"], ppid=10)
    _proc(tmp_path, 12, ["bash", "-lc", "echo serialwrapd"])
    assert uc.scan_daemon_instances(tmp_path) == [10]
    _proc(tmp_path, 20, ["/pipx/bin/python", "/home/u/.local/bin/serialwrapd"])
    assert uc.scan_daemon_instances(tmp_path) == [10, 20]


@pytest.mark.core_portable
@pytest.mark.memory_only
def test_scan_tolerates_vanished_process(tmp_path):
    _proc(tmp_path, 30, ["/v/bin/serialwrapd"])
    (tmp_path / "31").mkdir()  # 行程在掃描中途結束：cmdline／stat 已不存在
    (tmp_path / "self").mkdir()
    assert uc.scan_daemon_instances(tmp_path) == [30]


# ---------- two-reader 重疊 ----------

@pytest.mark.core_portable
@pytest.mark.memory_only
def test_two_reader_overlap_fails():
    """舊 daemon 未停即起新 daemon：任何一個取樣同時有兩個實例即 FAIL。"""
    samples = [(0.0, [101]), (0.05, [101, 202]), (0.10, [202])]
    row = uc.judge_two_reader(samples, min_samples=3)
    assert row["verdict"] == "FAIL", row
    assert row["detail"]["overlaps"][0]["pids"] == [101, 202]


@pytest.mark.core_portable
@pytest.mark.memory_only
def test_two_reader_sequential_handoff_passes():
    samples = [(0.0, [101]), (0.05, []), (0.10, [202]), (0.15, [202])]
    row = uc.judge_two_reader(samples, min_samples=3)
    assert row["verdict"] == "PASS", row
    assert row["detail"]["instances_seen"] == [101, 202]


@pytest.mark.core_portable
@pytest.mark.memory_only
def test_two_reader_too_few_samples_insufficient():
    row = uc.judge_two_reader([(0.0, [101])], min_samples=3)
    assert row["verdict"] == "INSUFFICIENT", row


# ---------- 設定與狀態保留 ----------

@pytest.mark.core_portable
@pytest.mark.memory_only
def test_config_lost_after_upgrade_fails(tmp_path):
    """升級後 config 遺失或被改寫：逐位元組比對 FAIL，並指出差異檔。"""
    cfg = tmp_path / "serialwrap"
    (cfg / "profiles").mkdir(parents=True)
    (cfg / "config.yaml").write_text("supervision_mode: systemd-user\n", encoding="utf-8")
    (cfg / "profiles" / "bench.yaml").write_text("targets: []\n", encoding="utf-8")
    before = uc.snapshot_files(cfg)
    (cfg / "config.yaml").unlink()
    row = uc.judge_files_preserved("config_preserved", before, uc.snapshot_files(cfg))
    assert row["verdict"] == "FAIL", row
    assert row["detail"]["missing"] == ["config.yaml"]
    (cfg / "config.yaml").write_text("supervision_mode: on-demand\n", encoding="utf-8")
    row = uc.judge_files_preserved("config_preserved", before, uc.snapshot_files(cfg))
    assert row["verdict"] == "FAIL" and row["detail"]["changed"] == ["config.yaml"], row
    (cfg / "config.yaml").write_text("supervision_mode: systemd-user\n", encoding="utf-8")
    assert uc.judge_files_preserved("config_preserved", before, uc.snapshot_files(cfg))["verdict"] == "PASS"


@pytest.mark.core_portable
@pytest.mark.memory_only
def test_alias_binding_lost_fails():
    base = {"aliases": [{"alias": "upgbench", "session_id": "shellboard:COM7", "updated_at": "t1"},
                        {"alias": "pinned", "device_by_id": "/dev/serial/by-id/x", "profile": "shellboard",
                         "updated_at": "t2"}],
            "sessions": [{"session_id": "shellboard:COM7", "device_by_id": "/dev/serial/by-id/rebound"}]}
    same = json.loads(json.dumps(base))
    same["aliases"][0]["updated_at"] = "t9"  # 時間戳不屬語意
    assert uc.judge_state_compatible(base, same)["verdict"] == "PASS"
    lost = json.loads(json.dumps(base))
    del lost["aliases"][1]
    row = uc.judge_state_compatible(base, lost)
    assert row["verdict"] == "FAIL" and row["detail"]["aliases_missing"], row
    rebound = json.loads(json.dumps(base))
    rebound["sessions"][0]["device_by_id"] = "/dev/serial/by-id/x"
    row = uc.judge_state_compatible(base, rebound)
    assert row["verdict"] == "FAIL" and row["detail"]["bindings_changed"], row


# ---------- doctor ----------

@pytest.mark.core_portable
@pytest.mark.memory_only
def test_doctor_green_requires_non_advisory_and_pairing():
    ok = {"ok": True, "checks": [{"check": "dialout", "ok": True, "advisory": False},
                                 {"check": "version_pairing", "ok": True, "advisory": False},
                                 {"check": "devices", "ok": False, "advisory": True}]}
    assert uc.judge_doctor("doctor_green_new", ok, require_pairing=True)["verdict"] == "PASS"
    bad = json.loads(json.dumps(ok))
    bad["checks"][0]["ok"] = False
    assert uc.judge_doctor("doctor_green_new", bad, require_pairing=True)["verdict"] == "FAIL"
    nopair = {"ok": True, "checks": [c for c in ok["checks"] if c["check"] != "version_pairing"]}
    assert uc.judge_doctor("doctor_green_new", nopair, require_pairing=True)["verdict"] == "FAIL"
    assert uc.judge_doctor("doctor_green_old", nopair, require_pairing=False)["verdict"] == "PASS"
    other_adv = json.loads(json.dumps(ok))
    other_adv["checks"].append({"check": "jq_on_path", "ok": False, "advisory": True})
    assert uc.judge_doctor("doctor_green_new", other_adv, require_pairing=True)["verdict"] == "FAIL"


# ---------- 整體 verdict ----------

@pytest.mark.core_portable
@pytest.mark.memory_only
def test_overall_verdict_rules():
    rows = [{"id": cid, "verdict": "PASS"} for cid in uc.REQUIRED_CHECKS]
    assert uc.overall_verdict(rows) == "PASS"
    assert uc.overall_verdict(rows[:-1]) == "INSUFFICIENT"
    rows[0] = {"id": rows[0]["id"], "verdict": "FAIL"}
    assert uc.overall_verdict(rows[:-1]) == "FAIL"
    dup = [{"id": cid, "verdict": "PASS"} for cid in uc.REQUIRED_CHECKS] + [{"id": uc.REQUIRED_CHECKS[0],
                                                                               "verdict": "PASS"}]
    assert uc.overall_verdict(dup) == "FAIL"


@pytest.mark.core_portable
@pytest.mark.memory_only
def test_refused_upgrade_must_leave_install_untouched():
    """舊 daemon 執行中時 0.4.0 setup 若拒絕：舊 daemon 須同 PID 存活、入口與設定不變，否則 FAIL。"""
    before = {"daemon_pids": [101], "entries": {"serialwrap": "/pipx/bin/serialwrap"}, "files": {"config.yaml": "a"}}
    after_ok = json.loads(json.dumps(before))
    assert uc.judge_refusal_untouched("upgrade_with_old_daemon_running", before, after_ok)["verdict"] == "PASS"
    killed = dict(after_ok, daemon_pids=[])
    assert uc.judge_refusal_untouched("upgrade_with_old_daemon_running", before, killed)["verdict"] == "FAIL"
    switched = dict(after_ok, entries={"serialwrap": "/v/0.4.0/bin/serialwrap"})
    assert uc.judge_refusal_untouched("upgrade_with_old_daemon_running", before, switched)["verdict"] == "FAIL"


# ---------- runner 與映像 ----------

@pytest.mark.core_portable
@pytest.mark.memory_only
def test_upgrade_image_pins_same_base_as_systemd_image():
    """升級容器與 OD-16 (5) systemd 容器使用同一個 ubuntu:24.04 digest，且不放 repo 內容、不連網安裝依賴。"""
    def base(path: Path) -> str:
        return next(line for line in path.read_text(encoding="utf-8").splitlines() if line.startswith("FROM "))
    assert base(DOCKERFILE) == base(SYSTEMD_DOCKERFILE)
    text = DOCKERFILE.read_text(encoding="utf-8")
    assert "COPY" not in text
    for pkg in ("pipx", "python3-yaml", "systemd", "dbus-user-session"):
        assert pkg in text, pkg


def _docker_available() -> bool:
    if shutil.which("docker") is None:
        return False
    try:
        return subprocess.run(["docker", "info"], capture_output=True, timeout=60).returncode == 0
    except (OSError, subprocess.TimeoutExpired):
        return False


# POSIX-only：bash runner 需 POSIX bash、git 與 python3。
@pytest.mark.posix_only
@pytest.mark.process_rpc
def test_run_upgrade_dry_run_lists_both_modes_and_isolation(tmp_path):
    r = subprocess.run(["bash", str(RUN_UPGRADE), "--dry-run", "--out", str(tmp_path / "ev")],
                       cwd=REPO, capture_output=True, text=True, timeout=120)
    assert r.returncode == 0, r.stdout + r.stderr
    out = r.stdout
    assert "git archive v0.3.2" in out
    assert "--version-override 0.4.0" in out
    assert "--network none" in out
    for mode in ("systemd-user", "on-demand"):
        assert f"--mode {mode}" in out, out
    assert "/dev/ttyUSB" not in out and "--device" not in out


@pytest.mark.posix_only
@pytest.mark.process_rpc
def test_run_upgrade_refuses_existing_evidence(tmp_path):
    out = tmp_path / "ev"
    (out / "systemd-user").mkdir(parents=True)
    (out / "systemd-user" / "upgrade-report.json").write_text("{}", encoding="utf-8")
    r = subprocess.run(["bash", str(RUN_UPGRADE), "--out", str(out), "--mode", "systemd-user"],
                       cwd=REPO, capture_output=True, text=True, timeout=120)
    assert r.returncode == 2, r.stdout + r.stderr


@pytest.mark.posix_only
@pytest.mark.process_rpc
@pytest.mark.supervisor_runtime
def test_docker_upgrade_0_3_2_to_0_4_0(tmp_path):
    """實跑：兩種監管模式各自完成 0.3.2 安裝→0.4.0 升級→降級回 0.3.2→重新升級，全部檢查 PASS。"""
    if not _docker_available():
        pytest.skip("cap_missing:docker：本機無可用 docker daemon（能力式 SKIP，證據不足，非 PASS）")
    if subprocess.run(["git", "rev-parse", "-q", "--verify", "refs/tags/v0.3.2"], cwd=REPO,
                      capture_output=True).returncode != 0:
        pytest.skip("cap_missing:tag_v0.3.2：版本庫沒有 v0.3.2 tag（證據不足，非 PASS）")
    out = tmp_path / "ev"
    r = subprocess.run(["bash", str(RUN_UPGRADE), "--out", str(out)], cwd=REPO, capture_output=True,
                       text=True, timeout=1800, env=dict(os.environ))
    reports = {}
    for mode in ("systemd-user", "on-demand"):
        path = out / mode / "upgrade-report.json"
        reports[mode] = json.loads(path.read_text(encoding="utf-8")) if path.is_file() else None
    diag = json.dumps({m: None if rep is None else [c for c in rep.get("checks", []) if c.get("verdict") != "PASS"]
                       for m, rep in reports.items()}, ensure_ascii=False)[:4000]
    assert r.returncode == 0, r.stdout[-2000:] + r.stderr[-2000:] + diag
    for mode, rep in reports.items():
        assert rep is not None, f"{mode} 未產生 upgrade-report.json"
        assert rep["verdict"] == "PASS", diag
        assert rep["evidence_layer"] == "L1.5-docker"
        assert rep["old_version"] == "0.3.2" and rep["new_version"] == "0.4.0"
