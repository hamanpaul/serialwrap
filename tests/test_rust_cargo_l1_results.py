"""P3-T17：`scripts/rust/cargo_l1_results.py` 的逐案結果轉換（R5-F3，`l1-results.v1`）。

以假 cargo、假 libtest 可執行檔與假 runner（`tests/fixtures/libtest_sample/`）驅動，不需真 cargo。
判定一律以 P2 Task 13 的唯一實作 `tests/acceptance/l1_judge.py` 為準（依路徑載入，不另寫判定）。
每個測試先斷言腳本存在（紅燈原因為「尚未實作」），再斷言報告內容，最後才呼叫 P2 判定。
"""

from __future__ import annotations

import hashlib
import importlib.util
import json
import os
import shutil
import subprocess
import sys
from pathlib import Path
from types import ModuleType
from typing import Any

import pytest

pytestmark = [pytest.mark.core_portable, pytest.mark.process_rpc]

ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "scripts" / "rust" / "cargo_l1_results.py"
FIXTURE = ROOT / "tests" / "fixtures" / "libtest_sample"
L1_JUDGE = ROOT / "tests" / "acceptance" / "l1_judge.py"
ACCEPTANCE = ROOT / "tests" / "acceptance"
CRATE = "sw-proto"
TARGET = "pyjson_golden"
A = f"cargo:{CRATE}::{TARGET}::a"
B = f"cargo:{CRATE}::{TARGET}::b"
C = f"cargo:{CRATE}::{TARGET}::cap_posix_pty__c"


def _assert_script() -> None:
    assert SCRIPT.is_file(), "scripts/rust/cargo_l1_results.py 尚未實作（P3-T17 Step 4）"


def _load_judge() -> ModuleType:
    assert L1_JUDGE.is_file(), (
        "tests/acceptance/l1_judge.py 不存在：P2 Task 13 尚未合併到本分支（前置缺口，見 plan §0.2；不得自寫判定代替）"
    )
    spec = importlib.util.spec_from_file_location("l1_judge", L1_JUDGE)
    assert spec is not None and spec.loader is not None
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def _install(src_name: str, dst: Path) -> Path:
    """把 fixtures 內的假工具複製到 dst 並加上執行權限（不依賴 git 的 exec bit）。"""
    dst.parent.mkdir(parents=True, exist_ok=True)
    shutil.copyfile(FIXTURE / src_name, dst)
    dst.chmod(0o755)
    return dst


def _write_exe(exe_dir: Path, name: str, *, run_out: str, exit_code: int = 0) -> Path:
    exe_dir.mkdir(parents=True, exist_ok=True)
    if os.name == "nt":
        exe = exe_dir / f"{name}.cmd"
        # 與 fake_libtest.sh 同語意：參數附加到 argv.log；`--list`／`--ignored --list` 輸出清單；能力 run（`--ignored
        # <filter>…`）有 run_cap_out.txt 時輸出該檔；其餘輸出 run_out.txt。
        exe.write_text(
            "@echo off\r\n"
            'set "base=%~dp0"\r\n'
            '>>"%base%argv.log" echo(%*\r\n'
            'if /i "%~1"=="--list" (type "%base%list_all.txt" & exit /b 0)\r\n'
            'if /i "%~1"=="--ignored" if /i "%~2"=="--list" (type "%base%list_ignored.txt" & exit /b 0)\r\n'
            'if /i "%~1"=="--ignored" if exist "%base%run_cap_out.txt" (type "%base%run_cap_out.txt" & exit /b 0)\r\n'
            'type "%base%run_out.txt"\r\n'
            'for /f "usebackq delims=" %%I in ("%base%exit_code") do exit /b %%I\r\n'
            "exit /b 0\r\n",
            encoding="utf-8",
        )
    else:
        exe = _install("fake_libtest.sh", exe_dir / name)
    shutil.copyfile(FIXTURE / "list_all.txt", exe_dir / "list_all.txt")
    shutil.copyfile(FIXTURE / "list_ignored.txt", exe_dir / "list_ignored.txt")
    shutil.copyfile(FIXTURE / run_out, exe_dir / "run_out.txt")
    (exe_dir / "exit_code").write_text(str(exit_code), encoding="utf-8")
    return exe


def _artifact(exe: Path, target: str, *, package: str = CRATE) -> dict[str, Any]:
    """與 `cargo test --no-run --message-format=json` 的 compiler-artifact 形狀一致（test target）。"""
    return {
        "reason": "compiler-artifact",
        "package_id": f"path+file:///work/crates/{package}#{package}@0.1.0",
        "target": {
            "kind": ["test"],
            "crate_types": ["bin"],
            "name": target,
            "src_path": f"/work/crates/{package}/tests/{target}.rs",
            "edition": "2021",
            "doc": False,
            "doctest": False,
            "test": True,
        },
        "profile": {"opt_level": "0", "debuginfo": 2, "debug_assertions": True, "overflow_checks": True, "test": True},
        "features": [],
        "filenames": [str(exe)],
        "executable": str(exe),
        "fresh": False,
    }


def _lib_artifact(package: str = CRATE) -> dict[str, Any]:
    """lib target 沒有 test executable，腳本必須略過（不得算進 collected）。"""
    return {
        "reason": "compiler-artifact",
        "package_id": f"path+file:///work/crates/{package}#{package}@0.1.0",
        "target": {
            "kind": ["lib"],
            "crate_types": ["lib"],
            "name": package.replace("-", "_"),
            "src_path": f"/work/crates/{package}/src/lib.rs",
            "edition": "2021",
            "doc": True,
            "doctest": True,
            "test": True,
        },
        "profile": {"opt_level": "0", "debuginfo": 2, "debug_assertions": True, "overflow_checks": True, "test": False},
        "features": [],
        "filenames": [f"/work/target/debug/lib{package.replace('-', '_')}.rlib"],
        "executable": None,
        "fresh": False,
    }


def _write_cargo(bin_dir: Path, artifacts: list[dict[str, Any]], *, exit_code: int = 0) -> None:
    bin_dir.mkdir(parents=True, exist_ok=True)
    if os.name == "nt":
        (bin_dir / "cargo.cmd").write_text(
            "@echo off\r\n"
            'type "%~dp0cargo_no_run.jsonl"\r\n'
            'for /f "usebackq delims=" %%I in ("%~dp0cargo_exit") do exit /b %%I\r\n'
            "exit /b 0\r\n",
            encoding="utf-8",
        )
    else:
        _install("fake_cargo.sh", bin_dir / "cargo")
    lines = [json.dumps(a) for a in artifacts]
    lines.append(json.dumps({"reason": "build-finished", "success": exit_code == 0}))
    (bin_dir / "cargo_no_run.jsonl").write_text("\n".join(lines) + "\n", encoding="utf-8")
    (bin_dir / "cargo_exit").write_text(str(exit_code), encoding="utf-8")


def _run(cwd: Path, bin_dir: Path, args: list[str]) -> subprocess.CompletedProcess[str]:
    env = {**os.environ, "PATH": f"{bin_dir}{os.pathsep}{os.environ.get('PATH', '')}"}
    return subprocess.run(
        [sys.executable, str(SCRIPT), *args, "--", "--workspace"],
        cwd=cwd,
        env=env,
        capture_output=True,
        text=True,
        check=False,
    )


def _report(out: Path, r: subprocess.CompletedProcess[str]) -> dict[str, Any]:
    assert out.is_file(), f"cargo_l1_results.py 未寫出報告；stderr={r.stderr[-2000:]}"
    return json.loads(out.read_text(encoding="utf-8"))


def _rows(rep: dict[str, Any]) -> dict[str, dict[str, Any]]:
    return {row["nodeid"]: row for row in rep["results"]}


@pytest.mark.process_rpc
def test_collected_from_list_not_from_run(tmp_path: Path) -> None:
    _assert_script()
    exe = _write_exe(tmp_path / "exe_a", TARGET, run_out="run_a_ok.txt")
    bin_dir = tmp_path / "bin"
    _write_cargo(bin_dir, [_artifact(exe, TARGET)])
    out = tmp_path / "l1.json"
    r = _run(tmp_path, bin_dir, ["--platform", "linux", "--out", str(out)])
    rep = _report(out, r)
    assert rep["format"] == "l1-results.v1" and rep["platform"] == "linux" and rep["finished"] is True
    assert sorted(rep["collected"]) == sorted([A, B, C])          # 來自 --list（含 ignored 的 cap_*）
    assert [row["nodeid"] for row in rep["results"]] == [A]        # 來自執行輸出，只有 `test a ... ok`
    res = _load_judge().judge(rep, [A, B], platform="linux")
    assert res["verdict"] == "INSUFFICIENT"
    assert res["not_run"] == [B]


@pytest.mark.process_rpc
def test_ignored_cap_is_skipped_not_pass(tmp_path: Path) -> None:
    _assert_script()
    exe = _write_exe(tmp_path / "exe_a", TARGET, run_out="run_ignored_cap.txt")
    bin_dir = tmp_path / "bin"
    _write_cargo(bin_dir, [_artifact(exe, TARGET)])
    out = tmp_path / "l1.json"
    r = _run(tmp_path, bin_dir, ["--platform", "linux", "--out", str(out)])
    rep = _report(out, r)
    row = _rows(rep)[C]
    assert row["outcome"] == "skipped"
    assert row["skip_kind"] == "capability"                        # cap_posix_pty__ 前綴
    res = _load_judge().judge(rep, [A, C], platform="linux")
    assert res["verdict"] == "INSUFFICIENT"
    assert C in [x["nodeid"] for x in res["skipped_required"]]


@pytest.mark.process_rpc
def test_failed_and_exit_code_recorded(tmp_path: Path) -> None:
    _assert_script()
    exe = _write_exe(tmp_path / "exe_a", TARGET, run_out="run_b_failed.txt", exit_code=101)
    bin_dir = tmp_path / "bin"
    _write_cargo(bin_dir, [_artifact(exe, TARGET)])
    out = tmp_path / "l1.json"
    r = _run(tmp_path, bin_dir, ["--platform", "linux", "--out", str(out)])
    rep = _report(out, r)
    assert _rows(rep)[B]["outcome"] == "failed"
    assert rep["exit_code"] == 101
    assert rep["finished"] is True
    assert _load_judge().judge(rep, [A, B], platform="linux")["verdict"] == "FAIL"


@pytest.mark.process_rpc
def test_build_failure_is_not_success(tmp_path: Path) -> None:
    _assert_script()
    bin_dir = tmp_path / "bin"
    _write_cargo(bin_dir, [_lib_artifact()], exit_code=101)        # 編譯失敗：沒有任何 test executable
    out = tmp_path / "l1.json"
    r = _run(tmp_path, bin_dir, ["--platform", "linux", "--out", str(out)])
    rep = _report(out, r)
    assert rep["finished"] is False
    assert rep["exit_code"] == 101
    assert rep["results"] == []
    assert _load_judge().judge(rep, [A], platform="linux")["verdict"] == "INSUFFICIENT"


@pytest.mark.process_rpc
def test_cargo_build_runs_from_this_script_workspace_even_when_called_elsewhere(tmp_path: Path) -> None:
    """Cargo 必須在收集器所在 repo 執行，不能把其他 cwd 的產物配上本 repo 的 HEAD。"""
    _assert_script()
    other_checkout = tmp_path / "other-checkout"
    other_checkout.mkdir()
    exe = _write_exe(tmp_path / "exe", TARGET, run_out="run_a_ok.txt")
    bin_dir = tmp_path / "bin"
    _write_cargo(bin_dir, [_artifact(exe, TARGET)])
    cwd_log = tmp_path / "cargo-cwd.txt"
    if os.name == "nt":
        (bin_dir / "cargo.cmd").write_text(
            "@echo off\r\n"
            'echo %CD%>>"%CARGO_CWD_LOG%"\r\n'
            'type "%~dp0cargo_no_run.jsonl"\r\n'
            'for /f "usebackq delims=" %%I in ("%~dp0cargo_exit") do exit /b %%I\r\n'
            "exit /b 0\r\n",
            encoding="utf-8",
        )
    else:
        (bin_dir / "cargo").write_text(
            "#!/bin/sh\n"
            'printf "%s\\n" "$PWD" >> "$CARGO_CWD_LOG"\n'
            'dir=$(dirname "$0")\n'
            'cat "$dir/cargo_no_run.jsonl"\n'
            'exit "$(cat "$dir/cargo_exit")"\n',
            encoding="utf-8",
        )
        (bin_dir / "cargo").chmod(0o755)
    env = {**os.environ, "PATH": f"{bin_dir}{os.pathsep}{os.environ.get('PATH', '')}",
           "CARGO_CWD_LOG": str(cwd_log)}
    external_manifest = other_checkout / "Cargo.toml"
    external_manifest.write_text("[workspace]\nmembers = []\n", encoding="utf-8")
    rejected_out = tmp_path / "external-workspace.json"
    rejected = subprocess.run(
        [sys.executable, str(SCRIPT), "--platform", "linux", "--out", str(rejected_out), "--",
         "--manifest-path", str(external_manifest)],
        cwd=other_checkout,
        env=env,
        capture_output=True,
        text=True,
        check=False,
    )
    assert rejected.returncode == 2, rejected.stderr
    assert "manifest" in rejected.stderr.lower(), rejected.stderr
    assert not cwd_log.exists(), "外部 manifest 在 Cargo 啟動前即應被拒絕"

    out = tmp_path / "l1.json"
    r = subprocess.run(
        [sys.executable, str(SCRIPT), "--platform", "linux", "--out", str(out), "--", "--workspace"],
        cwd=other_checkout,
        env=env,
        capture_output=True,
        text=True,
        check=False,
    )
    rep = _report(out, r)
    assert rep["finished"] is True
    assert cwd_log.read_text(encoding="utf-8").splitlines() == [str(ROOT)]


@pytest.mark.process_rpc
def test_timeout_marks_unfinished(tmp_path: Path) -> None:
    _assert_script()
    exe_a = _write_exe(tmp_path / "exe_a", TARGET, run_out="run_a_ok.txt")
    exe_t = _write_exe(tmp_path / "exe_timeout", "pycompat_golden", run_out="run_a_ok.txt")
    bin_dir = tmp_path / "bin"
    _write_cargo(bin_dir, [_artifact(exe_a, TARGET), _artifact(exe_t, "pycompat_golden")])
    if os.name == "nt":
        runner = bin_dir / "fake_runner.cmd"
        runner.write_text(
            "@echo off\r\n"
            'set "target=%~1"\r\n'
            'if /i "%target:\\exe_timeout\\=%" neq "%target%" exit /b 124\r\n'
            'call "%~1" %2 %3 %4 %5\r\n'
            "exit /b %errorlevel%\r\n",
            encoding="utf-8",
        )
    else:
        runner = _install("fake_runner.sh", bin_dir / "fake_runner.sh")  # 第二支 exe 回 124
    out = tmp_path / "l1.json"
    r = _run(tmp_path, bin_dir, ["--platform", "windows", "--runner", str(runner), "--out", str(out)])
    rep = _report(out, r)
    assert rep["exit_code"] == 124
    assert rep["finished"] is False
    assert _load_judge().judge(rep, [A], platform="windows")["verdict"] == "INSUFFICIENT"


@pytest.mark.process_rpc
def test_duplicate_nodeid_preserved(tmp_path: Path) -> None:
    _assert_script()
    exe1 = _write_exe(tmp_path / "exe_a", TARGET, run_out="run_a_ok.txt")
    exe2 = _write_exe(tmp_path / "exe_b", TARGET, run_out="run_a_ok.txt")   # 同 crate／target／名稱
    bin_dir = tmp_path / "bin"
    _write_cargo(bin_dir, [_artifact(exe1, TARGET), _artifact(exe2, TARGET)])
    out = tmp_path / "l1.json"
    r = _run(tmp_path, bin_dir, ["--platform", "linux", "--out", str(out)])
    rep = _report(out, r)
    assert rep["collected"].count(A) == 2                          # 原樣保留，不去重
    assert [row["nodeid"] for row in rep["results"]].count(A) == 2
    res = _load_judge().judge(rep, [A, B], platform="linux")
    assert res["verdict"] == "INSUFFICIENT"
    assert A in res["duplicates"]


@pytest.mark.process_rpc
def test_list_only_feeds_regen_lock(tmp_path: Path) -> None:
    _assert_script()
    exe = _write_exe(tmp_path / "exe_a", TARGET, run_out="run_a_ok.txt")
    bin_dir = tmp_path / "bin"
    _write_cargo(bin_dir, [_artifact(exe, TARGET)])
    collect = tmp_path / "collect.json"
    r = subprocess.run(
        [sys.executable, str(SCRIPT), "--list-only", "--out", str(collect), "--", "--workspace"],
        cwd=tmp_path,
        env={**os.environ, "PATH": f"{bin_dir}{os.pathsep}{os.environ.get('PATH', '')}"},
        capture_output=True,
        text=True,
        check=False,
    )
    assert collect.is_file(), f"--list-only 未寫出輸出；stderr={r.stderr[-2000:]}"
    data = json.loads(collect.read_text(encoding="utf-8"))
    assert set(data) == {"collected", "ignored"}                   # 只含收集清單與 ignored 名單，不含執行結果（OD-24）
    assert sorted(data["collected"]) == sorted([A, B, C])
    assert data["ignored"] == [C]                                  # ignored ⊆ collected

    # 以 P2 Task 13 的 regen_lock.py 登記到暫存鎖檔（副本），必須成功登記三筆
    regen = ACCEPTANCE / "regen_lock.py"
    assert regen.is_file(), "tests/acceptance/regen_lock.py 不存在：P2 Task 13 尚未合併（前置缺口，見 plan §0.2）"
    acc = tmp_path / "tests" / "acceptance"
    shutil.copytree(ACCEPTANCE, acc)
    lock = acc / "l1_required.lock.json"
    lock.write_text(
        json.dumps(
            {
                "schema_version": 1,
                "sets": {"t": {"linux": {"selector": {"argv": ["--workspace"], "paths": [], "markexpr": ""}, "nodeids": []}}},
            }
        ),
        encoding="utf-8",
    )
    rr = subprocess.run(
        [sys.executable, str(acc / "regen_lock.py"), "--l1-add-only", "--set", "t", "--platform", "linux", "--collect", str(collect)],
        cwd=tmp_path,
        capture_output=True,
        text=True,
        check=False,
    )
    assert rr.returncode == 0, rr.stderr[-2000:]
    got = json.loads(lock.read_text(encoding="utf-8"))["sets"]["t"]["linux"]["nodeids"]
    assert got == sorted([A, B])                                   # OD-24：ignored 的 C 不得新增進凍結集合


@pytest.mark.process_rpc
def test_list_only_keeps_ignored_non_cap_in_denominator(tmp_path: Path) -> None:
    """plan 04 Task 17：--list-only 必須收入 ignored 的非 cap_* 測試（與 cap_* 同樣保留），
    否則既有鎖列會被 regen_lock 誤判為移除、分母被無聲縮小。

    OD-24（2026-10-27）：輸出另附 ignored 名單；regen_lock --l1-add-only 新增時排除 ignored，
    但鎖中既有的 ignored 列仍在 collected 內，不因此被視為移除。"""
    _assert_script()
    exe = _write_exe(tmp_path / "exe_a", TARGET, run_out="run_a_ok.txt")
    ignored = exe.parent / "list_ignored.txt"
    ignored.write_text(
        ignored.read_text(encoding="utf-8") + "windows_switch_running_exe_rename: test\n",
        encoding="utf-8",
    )
    w = f"cargo:{CRATE}::{TARGET}::windows_switch_running_exe_rename"
    bin_dir = tmp_path / "bin"
    _write_cargo(bin_dir, [_artifact(exe, TARGET)])
    collect = tmp_path / "collect.json"
    r = subprocess.run(
        [sys.executable, str(SCRIPT), "--list-only", "--out", str(collect), "--", "--workspace"],
        cwd=tmp_path,
        env={**os.environ, "PATH": f"{bin_dir}{os.pathsep}{os.environ.get('PATH', '')}"},
        capture_output=True,
        text=True,
        check=False,
    )
    assert collect.is_file(), f"--list-only 未寫出輸出；stderr={r.stderr[-2000:]}"
    data = json.loads(collect.read_text(encoding="utf-8"))
    assert set(data) == {"collected", "ignored"}
    assert sorted(data["collected"]) == sorted([A, B, C, w])       # ignored 非 cap_* 的 W 必須在分母內
    assert sorted(data["ignored"]) == sorted([C, w])

    regen = ACCEPTANCE / "regen_lock.py"
    assert regen.is_file(), "tests/acceptance/regen_lock.py 不存在：P2 Task 13 尚未合併（前置缺口，見 plan §0.2）"
    acc = tmp_path / "tests" / "acceptance"
    shutil.copytree(ACCEPTANCE, acc)
    lock = acc / "l1_required.lock.json"
    lock.write_text(
        json.dumps(
            {
                "schema_version": 1,
                "sets": {"t": {"linux": {"selector": {"argv": ["--workspace"], "paths": [], "markexpr": ""}, "nodeids": []}}},
            }
        ),
        encoding="utf-8",
    )
    rr = subprocess.run(
        [sys.executable, str(acc / "regen_lock.py"), "--l1-add-only", "--set", "t", "--platform", "linux", "--collect", str(collect)],
        cwd=tmp_path,
        capture_output=True,
        text=True,
        check=False,
    )
    assert rr.returncode == 0, rr.stderr[-2000:]
    got = json.loads(lock.read_text(encoding="utf-8"))["sets"]["t"]["linux"]["nodeids"]
    assert got == sorted([A, B])                                   # OD-24：ignored 的 C、W 不新增

    # 鎖中既有的 ignored 列 W 仍在 collected 內：不得被視為移除，也不需 --allow-removal
    lock.write_text(
        json.dumps(
            {
                "schema_version": 1,
                "sets": {"t": {"linux": {"selector": {"argv": ["--workspace"], "paths": [], "markexpr": ""}, "nodeids": [A, w]}}},
            }
        ),
        encoding="utf-8",
    )
    kept = subprocess.run(
        [sys.executable, str(acc / "regen_lock.py"), "--l1-add-only", "--set", "t", "--platform", "linux", "--collect", str(collect)],
        cwd=tmp_path,
        capture_output=True,
        text=True,
        check=False,
    )
    assert kept.returncode == 0, kept.stderr[-2000:]
    got = json.loads(lock.read_text(encoding="utf-8"))["sets"]["t"]["linux"]["nodeids"]
    assert got == sorted([A, B, w])


def _run_env(cwd: Path, bin_dir: Path, args: list[str], *, extra_env: dict[str, str] | None = None,
             drop: tuple[str, ...] = ()) -> subprocess.CompletedProcess[str]:
    """同 `_run`，但可指定額外環境變數並移除指定變數（避免外層 RUSTFLAGS 影響 provenance 斷言）。"""
    env = {**os.environ, "PATH": f"{bin_dir}{os.pathsep}{os.environ.get('PATH', '')}"}
    for name in drop:
        env.pop(name, None)
    env.update(extra_env or {})
    return subprocess.run(
        [sys.executable, str(SCRIPT), *args, "--", "--workspace"],
        cwd=cwd,
        env=env,
        capture_output=True,
        text=True,
        check=False,
    )


def _git_head() -> str:
    return subprocess.check_output(["git", "rev-parse", "HEAD"], cwd=ROOT, text=True).strip()


@pytest.mark.process_rpc
def test_provenance_binds_executable_sha_head_and_toolchain(tmp_path: Path) -> None:
    """報告的 provenance 記錄每個執行檔的路徑與執行前 sha256、受測 HEAD（開始與結束）、rustc -vV 與 RUSTFLAGS 摘要。"""
    _assert_script()
    exe = _write_exe(tmp_path / "exe_a", TARGET, run_out="run_a_ok.txt")
    bin_dir = tmp_path / "bin"
    _write_cargo(bin_dir, [_artifact(exe, TARGET)])
    out = tmp_path / "l1.json"
    r = _run_env(tmp_path, bin_dir, ["--platform", "linux", "--out", str(out)],
                 drop=("RUSTFLAGS", "CARGO_ENCODED_RUSTFLAGS"))
    rep = _report(out, r)
    prov = rep["provenance"]
    assert prov["schema"] == "cargo-l1-provenance.v1"
    assert prov["platform"] == "linux" and prov["build_target"] is None
    assert prov["git"]["head"] == _git_head()
    assert isinstance(prov["git"]["clean"], bool)
    assert prov["git"]["end"]["head"] == _git_head()
    assert isinstance(prov["git"]["end"]["clean"], bool)
    [row] = prov["executables"]
    assert row["crate"] == CRATE and row["target"] == TARGET
    assert row["path"] == str(exe.resolve())
    assert row["sha256"] == hashlib.sha256(exe.read_bytes()).hexdigest()
    assert sorted(row["nodeids"]) == sorted([A, B, C])
    toolchain = prov["toolchain"]
    if shutil.which("rustc") is not None:
        assert toolchain["rustc_vv"] and toolchain["rustc_vv"].startswith("rustc "), toolchain
        assert toolchain["rustc_vv_sha256"] == hashlib.sha256(toolchain["rustc_vv"].encode("utf-8")).hexdigest()
    else:
        assert toolchain["rustc_vv"] is None and toolchain["error"], toolchain
    assert prov["rustflags"]["RUSTFLAGS"] == {"set": False, "sha256": None}
    assert prov["rustflags"]["CARGO_ENCODED_RUSTFLAGS"] == {"set": False, "sha256": None}


def test_rustc_version_probe_uses_repo_toolchain_cwd(monkeypatch) -> None:
    """rustc -vV 必須在 repo cwd 執行，套用該 repo 的 rust-toolchain.toml。"""
    _assert_script()
    spec = importlib.util.spec_from_file_location("cargo_l1_toolchain_cwd_test", SCRIPT)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    monkeypatch.setitem(sys.modules, spec.name, module)
    spec.loader.exec_module(module)
    calls: list[tuple[list[str], dict[str, Any]]] = []

    def fake_run(argv, **kwargs):
        calls.append((argv, kwargs))
        return subprocess.CompletedProcess(argv, 0, "rustc 1.96.1\n", "")

    monkeypatch.setenv("RUSTC", "rustc-shim")
    monkeypatch.setattr(module.subprocess, "run", fake_run)

    result = module._toolchain()

    assert result["rustc_vv"].startswith("rustc 1.96.1")
    assert calls and calls[0][1].get("cwd") == ROOT


@pytest.mark.process_rpc
def test_rustflags_recorded_as_digest_without_flag_text(tmp_path: Path) -> None:
    """RUSTFLAGS 只記錄是否設定與 sha256；旗標文字本身不寫入報告。"""
    _assert_script()
    flags = "-C debuginfo=0 --cfg rustflags_marker_for_digest_test"
    exe = _write_exe(tmp_path / "exe_a", TARGET, run_out="run_a_ok.txt")
    bin_dir = tmp_path / "bin"
    _write_cargo(bin_dir, [_artifact(exe, TARGET)])
    out = tmp_path / "l1.json"
    r = _run_env(tmp_path, bin_dir, ["--platform", "linux", "--out", str(out)],
                 extra_env={"RUSTFLAGS": flags}, drop=("CARGO_ENCODED_RUSTFLAGS",))
    rep = _report(out, r)
    assert rep["provenance"]["rustflags"]["RUSTFLAGS"] == {
        "set": True,
        "sha256": hashlib.sha256(flags.encode("utf-8")).hexdigest(),
    }
    assert "rustflags_marker_for_digest_test" not in out.read_text(encoding="utf-8")


MUTATING_LIBTEST = """import pathlib
import sys

me = pathlib.Path(__file__)
if "--list" in sys.argv:
    if "--ignored" not in sys.argv:
        print("a: test")
        print("b: test")
    sys.exit(0)
print("test a ... ok")
print("test b ... ok")
me.write_text(me.read_text(encoding="utf-8") + "# rebuilt during run\\n", encoding="utf-8")
"""


@pytest.mark.process_rpc
def test_executable_rewritten_during_run_marks_report_unfinished(tmp_path: Path) -> None:
    """測試執行期間執行檔被改寫：報告 finished=false，且 provenance 記錄的是執行前的 sha256（不是事後的內容）。

    以 Python 腳本當測試執行檔（runner 為 sys.executable），故與平台無關、不依賴 shebang。
    """
    _assert_script()
    exe = tmp_path / "exe_m" / "mutating_libtest.py"
    exe.parent.mkdir(parents=True)
    exe.write_text(MUTATING_LIBTEST, encoding="utf-8")
    before = hashlib.sha256(exe.read_bytes()).hexdigest()
    bin_dir = tmp_path / "bin"
    _write_cargo(bin_dir, [_artifact(exe, TARGET)])
    out = tmp_path / "l1.json"
    r = _run(tmp_path, bin_dir, ["--platform", "linux", "--runner", sys.executable, "--out", str(out)])
    rep = _report(out, r)
    assert rep["finished"] is False
    assert r.returncode != 0
    assert "測試執行檔於執行期間被改寫" in r.stderr, r.stderr
    [row] = rep["provenance"]["executables"]
    assert row["sha256"] == before
    assert row["sha256"] != hashlib.sha256(exe.read_bytes()).hexdigest()


@pytest.mark.process_rpc
def test_build_failure_report_still_carries_provenance(tmp_path: Path) -> None:
    """建置失敗的報告也附 provenance（finished=false、executables 為空），不得缺少紀錄欄位。"""
    _assert_script()
    bin_dir = tmp_path / "bin"
    _write_cargo(bin_dir, [_lib_artifact()], exit_code=101)
    out = tmp_path / "l1.json"
    r = _run(tmp_path, bin_dir, ["--platform", "linux", "--out", str(out)])
    rep = _report(out, r)
    assert rep["finished"] is False
    prov = rep["provenance"]
    assert prov["schema"] == "cargo-l1-provenance.v1"
    assert prov["executables"] == []
    assert prov["git"]["head"] == _git_head()


@pytest.mark.process_rpc   # 本檔資源 marker 一律 process_rpc（test_module_resource_marker_not_mixed）
def test_capability_matched_on_last_path_segment() -> None:
    """P5 第 6 輪 gate 實跑揭露：lib 單元測試的 libtest 名稱帶模組路徑（`console_pty::posix::tests::cap_posix_pty__x`），
    只比對名稱開頭會把它當成非能力測試——`--caps posix_pty` 不執行、skip_kind 記成 other，OD-55 移入
    `s21_gate:cap_posix_pty` 的 4 個 lib 能力測試因此恆 skipped。能力判定須取最後一段。"""
    spec = importlib.util.spec_from_file_location("cargo_l1_results", SCRIPT)
    mod = importlib.util.module_from_spec(spec)
    sys.modules.setdefault("cargo_l1_results", mod)        # dataclass 需模組已登錄
    spec.loader.exec_module(mod)
    assert mod.capability_of("console_pty::posix::tests::cap_posix_pty__new_slave_speed_not_b0") == "posix_pty"
    assert mod.capability_of("cap_windows_com__open") == "windows_com"
    assert mod.capability_of("serial::posix::tests::plain_cap_posix_pty__x") is None


@pytest.mark.process_rpc
def test_failure_output_is_echoed_and_saved(tmp_path: Path) -> None:
    """P5 第 7 輪：libtest 把失敗段（panic 訊息）印在 stdout；收集器只解析 stdout，失敗時 gate log 只剩兩行、無從追根因
    （r6 gate 的 lock_conformance_suite）。失敗時須把 stdout 的 failures 段轉印到 stderr，並把完整 stdout／stderr
    存到報告旁的 `<報告>.failures/`。"""
    _assert_script()
    exe = _write_exe(tmp_path / "exe_a", TARGET, run_out="run_b_failed.txt", exit_code=101)
    bin_dir = tmp_path / "bin"
    _write_cargo(bin_dir, [_artifact(exe, TARGET)])
    out = tmp_path / "l1.json"
    r = _run(tmp_path, bin_dir, ["--platform", "linux", "--out", str(out)])
    rep = _report(out, r)
    assert _rows(rep)[B]["outcome"] == "failed"
    assert "---- b stdout ----" in r.stderr and "panicked at" in r.stderr, r.stderr
    saved = sorted((tmp_path / "l1.json.failures").glob("*.stdout.txt"))
    assert saved, list(tmp_path.iterdir())
    text = saved[0].read_text(encoding="utf-8")
    assert "---- b stdout ----" in text and "test b ... FAILED" in text


@pytest.mark.process_rpc
def test_passing_run_saves_no_failure_output(tmp_path: Path) -> None:
    _assert_script()
    exe = _write_exe(tmp_path / "exe_a", TARGET, run_out="run_a_ok.txt")
    bin_dir = tmp_path / "bin"
    _write_cargo(bin_dir, [_artifact(exe, TARGET)])
    out = tmp_path / "l1.json"
    r = _run(tmp_path, bin_dir, ["--platform", "linux", "--out", str(out)])
    _report(out, r)
    assert not (tmp_path / "l1.json.failures").exists()


@pytest.mark.process_rpc
def test_capability_runs_are_single_threaded(tmp_path: Path) -> None:
    """P5 第 7 輪：能力測試（`--caps`）改以 `--test-threads=1` 執行。同執行檔平行測試 fork 子行程時，子行程在 exec 前
    暫時持有父行程全部 fd 的複本（含 CLOEXEC 者），被其他測試的 /proc holder 掃描看成外部 holder（cap_posix_pty__*
    並行負載下 5～11/180 間歇 `Held(pids:[同執行檔子行程])`；單執行緒 0/180）。預設（非能力）執行不變。
    （sol P5-54dd588-2 時改由假 libtest 自身記錄參數，移除原本的平台條件 SKIP——本檔為 core_portable。）"""
    _assert_script()
    exe = _write_exe(tmp_path / "exe_a", TARGET, run_out="run_ignored_cap.txt")
    bin_dir = tmp_path / "bin"
    _write_cargo(bin_dir, [_artifact(exe, TARGET)])
    out = tmp_path / "l1.json"
    r = _run(tmp_path, bin_dir, ["--platform", "linux", "--caps", "posix_pty", "--out", str(out)])
    _report(out, r)
    runs = _test_runs(exe)
    cap_runs = [a for a in runs if "--ignored" in a]
    default_runs = [a for a in runs if "--ignored" not in a]
    assert cap_runs and all("--test-threads=1" in a for a in cap_runs), runs
    assert default_runs and not any("--test-threads=1" in a for a in default_runs), runs


def _test_runs(exe: Path) -> list[list[str]]:
    """假 libtest 的 argv.log 中非 `--list` 的呼叫（即實際的測試 run）。"""
    lines = (exe.parent / "argv.log").read_text(encoding="utf-8").splitlines()
    return [line.split() for line in lines if "--list" not in line.split()]


# --------------------------------------------------------------------------- sol P5-54dd588-2（Medium）：--caps-only

def _exe_with_failing_default_and_ok_cap(exe_dir: Path) -> Path:
    """預設 run 有一列失敗（b FAILED、exit 101，模擬 root 下 sw-pal 必紅的預設測試），能力 run 只回 cap 列 passed。"""
    exe = _write_exe(exe_dir, TARGET, run_out="run_b_failed.txt", exit_code=101)
    shutil.copyfile(FIXTURE / "run_cap_c_ok.txt", exe_dir / "run_cap_out.txt")
    return exe


@pytest.mark.process_rpc
def test_caps_without_caps_only_runs_default_tests_and_fails(tmp_path: Path) -> None:
    """失敗情境（sol P5-54dd588-2）：能力 section 原命令 `--caps <cap>` 先跑預設 run；預設列失敗 → l1_judge 規則
    「任何列失敗 → FAIL」，即使必要集合只有能力列且能力列通過。"""
    _assert_script()
    exe = _exe_with_failing_default_and_ok_cap(tmp_path / "exe_a")
    bin_dir = tmp_path / "bin"
    _write_cargo(bin_dir, [_artifact(exe, TARGET)])
    out = tmp_path / "l1.json"
    r = _run(tmp_path, bin_dir, ["--platform", "linux", "--caps", "posix_pty", "--out", str(out)])
    rep = _report(out, r)
    assert [a for a in _test_runs(exe) if "--ignored" not in a], "未帶 --caps-only 時仍執行預設 run"
    assert _rows(rep)[C]["outcome"] == "passed"
    assert _load_judge().judge(rep, [C], platform="linux")["verdict"] == "FAIL"


@pytest.mark.process_rpc
def test_caps_only_skips_default_run_and_deselects_other_rows(tmp_path: Path) -> None:
    """sol P5-54dd588-2（Medium）：`--caps-only` 略過預設 run，只跑 `--ignored cap_<cap>__ --test-threads=1`；其餘收集列
    記為 deselected（結果與收集一致），報告 argv 為呼叫端交付的 selector argv；能力列通過 → l1_judge PASS。"""
    _assert_script()
    exe = _exe_with_failing_default_and_ok_cap(tmp_path / "exe_a")
    bin_dir = tmp_path / "bin"
    _write_cargo(bin_dir, [_artifact(exe, TARGET)])
    out = tmp_path / "l1.json"
    r = _run(tmp_path, bin_dir, ["--platform", "linux", "--caps-only", "--caps", "posix_pty", "--out", str(out)])
    rep = _report(out, r)
    assert r.returncode == 0, r.stderr
    assert _test_runs(exe) == [["--ignored", "cap_posix_pty__", "--test-threads=1"]]
    assert set(_rows(rep)) == {C} and _rows(rep)[C]["outcome"] == "passed"
    assert sorted(rep["deselected"]) == sorted([A, B]) and sorted(rep["collected"]) == sorted([A, B, C])
    assert rep["argv"] == ["--workspace"] and rep["exit_code"] == 0 and rep["finished"] is True
    res = _load_judge().judge(rep, [C], platform="linux")
    assert res["verdict"] == "PASS", res["reasons"]
    # 必要集合含非能力列時，--caps-only 的結果不得被當成已執行（deselected required → 非 PASS）
    assert _load_judge().judge(rep, [A, C], platform="linux")["verdict"] != "PASS"


@pytest.mark.process_rpc
def test_caps_only_target_without_selected_cap_is_not_run(tmp_path: Path) -> None:
    """`--caps-only` 時不含所選能力測試的執行檔完全不執行，其收集列全數 deselected；沒有任何執行檔含該能力 → 非 0 結束。"""
    _assert_script()
    exe = _exe_with_failing_default_and_ok_cap(tmp_path / "exe_a")
    bin_dir = tmp_path / "bin"
    _write_cargo(bin_dir, [_artifact(exe, TARGET)])
    out = tmp_path / "l1.json"
    r = _run(tmp_path, bin_dir, ["--platform", "linux", "--caps-only", "--caps", "sys_admin", "--out", str(out)])
    rep = _report(out, r)
    assert r.returncode != 0
    assert _test_runs(exe) == []
    assert rep["results"] == [] and sorted(rep["deselected"]) == sorted([A, B, C])


@pytest.mark.process_rpc
def test_caps_only_requires_caps(tmp_path: Path) -> None:
    """`--caps-only` 未指定任何 `--caps` 無意義（會什麼都不跑），拒絕並以 2 結束、不寫報告。"""
    _assert_script()
    out = tmp_path / "l1.json"
    r = subprocess.run([sys.executable, str(SCRIPT), "--caps-only", "--out", str(out), "--", "--workspace"],
                       cwd=tmp_path, capture_output=True, text=True, check=False)
    assert r.returncode == 2 and not out.exists(), (r.returncode, r.stderr)
