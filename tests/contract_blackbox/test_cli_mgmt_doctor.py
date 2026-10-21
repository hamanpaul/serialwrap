"""P4-T13 `doctor` 全部檢查原生與 version_pairing（SC-25）黑箱契約測試。"""
from __future__ import annotations

import hashlib
import json
import os
import shutil
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

# SC-25 附註（P4-T13）：無 manifest 時 python／pyyaml 的檢查對象是 release manifest 的 daemon runtime，
# Rust CLI 本身不是 Python，故 shared A/B 不比 detail；Rust 端須為 ok=false 並給重新安裝提示（見 contracts/behavior_changes.yaml）。
_RUNTIME_CHECKS = ("python", "pyyaml")
_RUNTIME_CHECK_NAMES = ("python", "pyyaml", "pyserial")
_REINSTALL_FIX = "重新安裝發佈組合（serialwrap setup）"


def _sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _full_bundle_version_dir(tmp_path: Path) -> Path:
    """`mkbundle.sh --dev`（非 sentinel）的自含 bundle：`versions/<V>/` 內含 CLI、daemon launcher、py runtime 與 manifest。

    sentinel bundle 的 `py/` 不含 `sw_core`，無法執行 bundle 內 Python doctor（計畫 Task 13 要求「自含 bundle」）。
    """
    out = tmp_path / "fullbundle"
    res = subprocess.run(
        ["bash", str(repo_root() / "scripts" / "rust" / "mkbundle.sh"), "--dev", "--out", str(out), "--print-dir"],
        capture_output=True,
        text=True,
        timeout=900,
        check=False,
    )
    assert res.returncode == 0, res.stderr
    bundle_out = Path(res.stdout.strip().splitlines()[-1])
    version = (repo_root() / "VERSION").read_text(encoding="utf-8").strip()
    return bundle_out / "versions" / version


def _specified_rust_bundle() -> Path | None:
    """`SERIALWRAP_CLI_EXE`（即 `SW_BUNDLE/bin/serialwrap`）指定的受測 Rust artifact 所在 bundle 根；未指定或非 Rust 回 None。"""
    if not is_rust():
        return None
    cli = os.environ.get("SERIALWRAP_CLI_EXE", "")
    if not cli or cli == "/bin/serialwrap":
        return None
    cli_path = Path(cli)
    assert cli_path.name == "serialwrap" and cli_path.parent.name == "bin", f"SERIALWRAP_CLI_EXE 應為 <bundle>/bin/serialwrap：{cli}"
    return cli_path.resolve().parent.parent


def _require_artifact(bundle_root: Path) -> dict[str, str]:
    """受測 artifact 的必要零件（CLI 與 release-manifest.json）；缺件即明確失敗，並回傳路徑與 sha256 作證據。"""
    cli = bundle_root / "bin" / "serialwrap"
    manifest = bundle_root / "release-manifest.json"
    missing = [str(p) for p in (cli, manifest) if not p.is_file()]
    assert not missing, f"受測 artifact 缺少必要零件：{missing}"
    return {
        "bundle": str(bundle_root),
        "cli_sha256": _sha256(cli),
        "manifest_sha256": _sha256(manifest),
    }


def _bundle_has_python_oracle(bundle_root: Path) -> bool:
    """bundle 內是否帶完整 Python runtime（可 import sw_core）；sentinel bundle 的 py/ 不含 sw_core。"""
    py = bundle_root / "py" / "bin" / "python3"
    if not py.exists():
        return False
    res = subprocess.run([str(py), "-I", "-c", "import sw_core"], capture_output=True, text=True, timeout=30)
    return res.returncode == 0


@pytest.mark.case("sc25-release-manifest-version-pairing")
def test_doctor_ab_shared_checks(tmp_path: Path) -> None:
    work_py = tmp_path / "py"
    work_cli = tmp_path / "cli"
    work_py.mkdir()
    work_cli.mkdir()

    env_py = isolated_env(work_py)
    env_cli = isolated_env(work_cli)

    cli_argv = cli_prefix()
    if is_rust():
        # 無 manifest 的情境：release manifest 由 current_exe 所在 bundle 定位，故把受測 CLI 複製到不含
        # release-manifest.json 的隔離目錄再執行（sentinel bundle 本身帶 manifest）。
        exe = Path(cli_argv[0])
        standalone = work_cli / "bin" / exe.name
        standalone.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(exe, standalone)
        cli_argv = [str(standalone)]

    py_proc = subprocess.run(
        python_baseline_prefix() + ["doctor"],
        env=env_py,
        capture_output=True,
        text=True,
        timeout=30,
    )
    cli_proc = subprocess.run(
        cli_argv + ["doctor"],
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

    # Linux 前 18 項逐欄比對（python／pyyaml 的 detail 除外，見上方 SC-25 附註）
    assert len(py_checks) == 18
    assert len(cli_checks) == 18
    assert [c["check"] for c in cli_checks] == [c["check"] for c in py_checks]

    for py_item, cli_item in zip(py_checks, cli_checks):
        chk = py_item["check"]
        assert cli_item["check"] == chk
        assert cli_item["advisory"] == py_item["advisory"], f"check={chk} advisory mismatch"
        if chk in _RUNTIME_CHECKS:
            if is_rust():
                # 無 manifest：Rust 無法定位 daemon runtime，ok=false 並給統一重新安裝提示。
                assert cli_item["ok"] is False, f"check={chk} 應為 ok=false（無 manifest）"
                assert cli_item["fix"] == _REINSTALL_FIX, f"check={chk} fix mismatch"
            continue
        assert cli_item["ok"] == py_item["ok"], f"check={chk} ok mismatch"
        assert cli_item["fix"] == py_item["fix"], f"check={chk} fix mismatch"
        # 兩輪各用獨立隔離目錄，detail 內的路徑先把各自工作目錄正規化為同一占位字串再比對。
        py_detail = str(py_item["detail"]).replace(str(work_py), "<WORK>")
        cli_detail = str(cli_item["detail"]).replace(str(work_cli), "<WORK>")
        assert cli_detail == py_detail, f"check={chk} detail mismatch"


@pytest.mark.case("sc25-release-manifest-version-pairing")
def test_doctor_ab_version_pairing_in_bundle(tmp_path: Path) -> None:
    # 受測 artifact：有指定 SERIALWRAP_CLI_EXE（Rust）時即為該 bundle（不另行重建）；否則建置自含 bundle。
    artifact = _specified_rust_bundle()
    bundle_root = artifact if artifact is not None else _full_bundle_version_dir(tmp_path)
    evidence = _require_artifact(bundle_root)
    rust_cli = str(bundle_root / "bin" / "serialwrap")
    manifest = str(bundle_root / "release-manifest.json")

    # Python oracle：自含 bundle 內的 py（P1 T18，`-I -m sw_core.cli`）；sentinel artifact 的 py 不含 sw_core，
    # 則以凍結的 Python 基線對同一份 manifest 與檔案執行（version_pairing 只比對 manifest 目錄內的檔案）。
    full_oracle = _bundle_has_python_oracle(bundle_root)
    if full_oracle:
        py_doctor = [str(bundle_root / "py" / "bin" / "python3"), "-I", "-m", "sw_core.cli", "doctor"]
    else:
        py_doctor = python_baseline_prefix() + ["doctor"]

    def _pair(env: dict[str, str]) -> tuple[list[dict], list[dict]]:
        py_res = subprocess.run(py_doctor, env=env, capture_output=True, text=True, timeout=30)
        rust_res = subprocess.run([rust_cli, "doctor"], env=env, capture_output=True, text=True, timeout=30)
        assert py_res.returncode == 0, f"Python doctor 失敗：{py_res.stderr}（證據 {evidence}）"
        assert rust_res.returncode == 0, f"Rust doctor 失敗：{rust_res.stderr}（證據 {evidence}）"
        return json.loads(py_res.stdout)["checks"], json.loads(rust_res.stdout)["checks"]

    def _assert_same_checks(py_checks: list[dict], rust_checks: list[dict], state: str) -> None:
        assert [c["check"] for c in rust_checks] == [c["check"] for c in py_checks], f"{state}: 項目順序不一致（證據 {evidence}）"
        for py_item, rust_item in zip(py_checks, rust_checks):
            name = py_item["check"]
            if not full_oracle and name in _RUNTIME_CHECK_NAMES:
                # 基線 interpreter 與 artifact 內的 runtime 可能不同，只比名稱與 advisory。
                assert rust_item["advisory"] == py_item["advisory"], f"{state}: check={name} advisory mismatch"
                continue
            assert rust_item == py_item, f"{state}: check={name} 不一致（證據 {evidence}）\nrust={rust_item}\npy={py_item}"

    # 狀態 1：daemon 未跑
    work1 = tmp_path / "daemon_down"
    work1.mkdir()
    env1 = isolated_env(work1)
    env1["SERIALWRAP_RELEASE_MANIFEST"] = manifest

    py_checks1, rust_checks1 = _pair(env1)
    _assert_same_checks(py_checks1, rust_checks1, "daemon_down")
    rust_vp1 = next(c for c in rust_checks1 if c["check"] == "version_pairing")
    assert rust_vp1["ok"] is False, rust_vp1
    assert "daemon_unreachable" in rust_vp1["mismatches"], rust_vp1

    # 狀態 2：daemon 執行中（以隔離 RUN_DIR 的預設 socket 啟動真 Python daemon，health.status 回報其版本）。
    # RUN_DIR 取短路徑，避免 AF_UNIX socket 路徑超過上限。
    work2 = tmp_path / "daemon_up"
    work2.mkdir()
    env2 = isolated_env(work2)
    env2["SERIALWRAP_RELEASE_MANIFEST"] = manifest
    run2 = work2 / "r"
    run2.mkdir()
    env2["SERIALWRAP_RUN_DIR"] = str(run2)
    profile2 = work2 / "profiles"
    profile2.mkdir()

    from bb_daemon import PyDaemon

    daemon = PyDaemon(env2, profile_dir=profile2, socket=str(run2 / "serialwrapd.sock"), lock=work2 / "daemon.lock")
    daemon.start()
    try:
        py_checks2, rust_checks2 = _pair(env2)
        _assert_same_checks(py_checks2, rust_checks2, "daemon_up")
        rust_vp2 = next(c for c in rust_checks2 if c["check"] == "version_pairing")
        # 必須真的配對成功（不得只是兩邊同為 daemon_unreachable 而相等）。
        assert rust_vp2["ok"] is True, f"version_pairing 應 ok（證據 {evidence}）：{rust_vp2}"
        assert rust_vp2["detail"] == "ok", rust_vp2
        assert rust_vp2["mismatches"] == [], rust_vp2
    finally:
        daemon.stop()
