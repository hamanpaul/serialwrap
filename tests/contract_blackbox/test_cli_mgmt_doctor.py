"""P4-T13 `doctor` 全部檢查原生與 version_pairing（SC-25）黑箱契約測試。"""
from __future__ import annotations

import json
import os
import subprocess
from pathlib import Path

import pytest

from bb_harness import (
    cli_prefix,
    is_rust,
    isolated_env,
    python_baseline_prefix,
    repo_root,
)

if os.environ.get("SERIALWRAP_CLI_EXE") == "/bin/serialwrap":
    sw_bundle = os.environ.get("SW_BUNDLE")
    if sw_bundle and (Path(sw_bundle) / "bin" / "serialwrap").exists():
        os.environ["SERIALWRAP_CLI_EXE"] = str(Path(sw_bundle) / "bin" / "serialwrap")
    elif (repo_root() / "target" / "mkbundle-dev" / "bin" / "serialwrap").exists():
        os.environ["SERIALWRAP_CLI_EXE"] = str(repo_root() / "target" / "mkbundle-dev" / "bin" / "serialwrap")

pytestmark = [pytest.mark.posix_only, pytest.mark.process_rpc]


@pytest.mark.case("sc25-release-manifest-version-pairing")
# 僅 Rust 輪為紅燈：Python 基線輪兩側皆為 Python CLI，A/A 比對本即通過（strict xfail 不得套在基線輪，否則 XPASS→FAIL）。
@pytest.mark.xfail(is_rust(), strict=True, reason="P4-T13 紅燈")
def test_doctor_ab_shared_checks(tmp_path: Path) -> None:
    work_py = tmp_path / "py"
    work_cli = tmp_path / "cli"
    work_py.mkdir()
    work_cli.mkdir()

    env_py = isolated_env(work_py)
    env_cli = isolated_env(work_cli)

    py_proc = subprocess.run(
        python_baseline_prefix() + ["doctor"],
        env=env_py,
        capture_output=True,
        text=True,
        timeout=30,
    )
    cli_proc = subprocess.run(
        cli_prefix() + ["doctor"],
        env=env_cli,
        capture_output=True,
        text=True,
        timeout=30,
    )

    assert py_proc.returncode == 0, f"Python doctor 失敗：{py_proc.stderr}"
    assert cli_proc.returncode == 0, f"CLI doctor 失敗：{cli_proc.stderr}"

    py_res = json.loads(py_proc.stdout)
    cli_res = json.loads(cli_proc.stdout)

    assert "checks" in py_res and "checks" in cli_res
    py_checks = py_res["checks"]
    cli_checks = cli_res["checks"]

    # 無 manifest 時兩邊皆無 version_pairing
    assert not any(c.get("check") == "version_pairing" for c in py_checks)
    assert not any(c.get("check") == "version_pairing" for c in cli_checks)

    # Linux 前 18 項逐欄比對（除 python/pyyaml detail 外逐欄相同）
    assert len(py_checks) == 18
    assert len(cli_checks) == 18
    assert [c["check"] for c in cli_checks] == [c["check"] for c in py_checks]

    for py_item, cli_item in zip(py_checks, cli_checks):
        chk = py_item["check"]
        assert cli_item["check"] == chk
        assert cli_item["ok"] == py_item["ok"], f"check={chk} ok mismatch"
        assert cli_item["advisory"] == py_item["advisory"], f"check={chk} advisory mismatch"
        assert cli_item["fix"] == py_item["fix"], f"check={chk} fix mismatch"
        if chk not in ("python", "pyyaml"):
            # 兩輪各用獨立隔離目錄，detail 內的路徑先把各自工作目錄正規化為同一占位字串再比對。
            py_detail = str(py_item["detail"]).replace(str(work_py), "<WORK>")
            cli_detail = str(cli_item["detail"]).replace(str(work_cli), "<WORK>")
            assert cli_detail == py_detail, f"check={chk} detail mismatch"


@pytest.mark.case("sc25-release-manifest-version-pairing")
@pytest.mark.xfail(strict=True, reason="P4-T13 紅燈")
def test_doctor_ab_version_pairing_in_bundle(tmp_path: Path, mgmt_bundle) -> None:
    bundle_root = mgmt_bundle.root
    rust_cli = str(mgmt_bundle.cli)
    py_doctor = [str(bundle_root / "py" / "bin" / "python3"), "-I", "-m", "sw_core.cli", "doctor"]

    # 狀態 1：daemon 未跑
    work1 = tmp_path / "daemon_down"
    work1.mkdir()
    env1 = isolated_env(work1)
    env1["SERIALWRAP_RELEASE_MANIFEST"] = str(bundle_root / "release-manifest.json")

    py_res1 = subprocess.run(py_doctor, env=env1, capture_output=True, text=True, timeout=30)
    rust_res1 = subprocess.run([rust_cli, "doctor"], env=env1, capture_output=True, text=True, timeout=30)

    assert py_res1.returncode == 0
    assert rust_res1.returncode == 0

    py_checks1 = json.loads(py_res1.stdout)["checks"]
    rust_checks1 = json.loads(rust_res1.stdout)["checks"]

    py_vp1 = next(c for c in py_checks1 if c["check"] == "version_pairing")
    rust_vp1 = next(c for c in rust_checks1 if c["check"] == "version_pairing")

    assert rust_vp1 == py_vp1
    assert rust_vp1["ok"] is False
    assert rust_vp1["detail"] == "daemon_unreachable"
    assert rust_vp1["mismatches"] == ["daemon_unreachable"]

    # 狀態 2：daemon 執行中
    work2 = tmp_path / "daemon_up"
    work2.mkdir()
    env2 = isolated_env(work2)
    env2["SERIALWRAP_RELEASE_MANIFEST"] = str(bundle_root / "release-manifest.json")

    from bb_daemon import PyDaemon
    from bb_faketarget import FakeTarget

    target = FakeTarget(work2 / "fake_target", count=1)
    target.start()
    try:
        daemon = PyDaemon(work2 / "daemon", env=env2, targets=[("COM0", target.ports[0])])
        daemon.start()
        try:
            py_res2 = subprocess.run(py_doctor, env=env2, capture_output=True, text=True, timeout=30)
            rust_res2 = subprocess.run([rust_cli, "doctor"], env=env2, capture_output=True, text=True, timeout=30)

            assert py_res2.returncode == 0
            assert rust_res2.returncode == 0

            py_checks2 = json.loads(py_res2.stdout)["checks"]
            rust_checks2 = json.loads(rust_res2.stdout)["checks"]

            py_vp2 = next(c for c in py_checks2 if c["check"] == "version_pairing")
            rust_vp2 = next(c for c in rust_checks2 if c["check"] == "version_pairing")

            assert rust_vp2 == py_vp2
        finally:
            daemon.stop()
    finally:
        target.stop()
