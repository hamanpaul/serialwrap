"""P4-T13 `doctor` 全部檢查原生與 version_pairing（SC-25）黑箱契約測試。"""
from __future__ import annotations

import hashlib
import json
import os
import re
import shutil
import subprocess
import sys
import time
from pathlib import Path

import pytest

from bb_harness import (
    ab_env,
    cli_prefix,
    is_rust,
    isolated_env,
    python_baseline_prefix,
    repo_root,
    rust_daemon_code_fingerprint,
)
from bb_l15 import in_isolated_pidns

if os.environ.get("SERIALWRAP_CLI_EXE") == "/bin/serialwrap":
    sw_bundle = os.environ.get("SW_BUNDLE")
    if sw_bundle and (Path(sw_bundle) / "bin" / "serialwrap").exists():
        os.environ["SERIALWRAP_CLI_EXE"] = str(Path(sw_bundle) / "bin" / "serialwrap")
    elif (repo_root() / "target" / "mkbundle-pytest" / "bin" / "serialwrap").exists():
        os.environ["SERIALWRAP_CLI_EXE"] = str(repo_root() / "target" / "mkbundle-pytest" / "bin" / "serialwrap")

pytestmark = [pytest.mark.posix_only, pytest.mark.process_rpc]

# SC-25 附註（P4-T13）：無 manifest 時 python／pyyaml 的檢查對象是 release manifest 的 daemon runtime，
# Rust CLI 本身不是 Python，故 shared A/B 不比 detail；Rust 端須為 ok=false 並給重新安裝提示（見 contracts/behavior_changes.yaml）。
_RUNTIME_CHECKS = ("python", "pyyaml")
_RUNTIME_CHECK_NAMES = ("python", "pyyaml", "pyserial")
_REINSTALL_FIX = "重新安裝發佈組合（serialwrap setup）"
_DAEMON_CODE_CURRENT_FIX = (
    "serialwrap service restart（或 serialwrap setup，指紋不符時會自動重啟 systemd 服務；"
    "on-demand 模式：serialwrap daemon stop 後 serialwrap daemon start）；"
    "serialwrap daemon status 的 code_path 可看 daemon 載入的是哪份安裝"
)


def _assert_doctor_check_payload(actual: dict, expected: dict, context: str) -> None:
    """逐欄比對 doctor check 的完整外部 payload。"""
    assert actual == expected, f"{context}: doctor check payload mismatch"


def _python_code_fingerprint(bundle_root: Path, full_oracle: bool) -> str:
    """以 Python oracle 自己的 interpreter 計算基線指紋，不在測試行程匯入產品模組。"""
    if full_oracle:
        interpreter = bundle_root / "py" / "bin" / "python3"
        command = [str(interpreter), "-I", "-B"]
    else:
        command = [sys.executable]
    command.extend(
        ["-c", "from sw_core.version import code_fingerprint; print(code_fingerprint() or '')"]
    )
    result = subprocess.run(
        command,
        cwd=repo_root(),
        capture_output=True,
        text=True,
        timeout=30,
    )
    assert result.returncode == 0, f"Python doctor 指紋計算失敗：{result.stderr}"
    fingerprint = result.stdout.strip()
    assert re.fullmatch(r"[0-9a-f]{12}", fingerprint), f"Python doctor 指紋格式錯誤：{fingerprint!r}"
    return fingerprint


def _assert_doctor_payload_mutations_rejected() -> None:
    expected = {
        "advisory": True,
        "check": "daemon_code_current",
        "detail": "daemon 與目前安裝的程式碼相同（code 0123456789ab）",
        "fix": "",
        "ok": True,
    }
    counterexamples = (
        ("A", "detail", "錯誤診斷"),
        ("A", "fix", "錯誤修復指示"),
        ("B", "fix", "錯誤修復指示"),
    )
    for context, field, value in counterexamples:
        malformed = dict(expected)
        malformed[field] = value
        with pytest.raises(AssertionError):
            _assert_doctor_check_payload(malformed, expected, context)


def _daemon_code_current_payload(*, ok: bool, detail: str, fix: str) -> dict:
    return {
        "advisory": True,
        "check": "daemon_code_current",
        "detail": detail,
        "fix": fix,
        "ok": ok,
    }


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
    cli = ab_env().get("SERIALWRAP_CLI_EXE", "")
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
    res = subprocess.run([str(py), "-I", "-B", "-c", "import sw_core"], capture_output=True, text=True, timeout=30)
    return res.returncode == 0


def _serialwrapd_pids() -> list[int]:
    """/proc 內 cmdline 基名為 serialwrapd（或 serialwrapd.py）的行程，與 ab_blackbox.sh preflight 同一判準。"""
    found: list[int] = []
    for entry in Path("/proc").iterdir():
        if not entry.name.isdigit():
            continue
        try:
            parts = (entry / "cmdline").read_bytes().split(b"\0")
        except OSError:
            continue
        names = {Path(p.decode("utf-8", errors="replace")).name for p in parts if p}
        if names & {"serialwrapd", "serialwrapd.py"}:
            found.append(int(entry.name))
    return found


def _wait_no_serialwrapd(timeout_s: float = 15.0) -> None:
    """隔離 pid namespace 內，doctor 比對前輪詢至 /proc 無 serialwrapd。

    preflight 已保證隔離 namespace 內無非本 run 的 serialwrapd，故殘留即為前序測試的 daemon 延遲退出
    （例如 handoff 排空窗口，PENDING-OD-S1X-HANDOFF-WINDOW）；py 與 Rust 兩輪若一輪見到、一輪未見，
    single_daemon 的 detail 即不一致。逾時 fail 並列出殘留 pid，不得以「flaky」略過。
    非隔離執行（一般 pytest，機器上可能有生產 daemon）不輪詢，single_daemon 維持即時比對。
    """
    if not in_isolated_pidns():
        return
    deadline = time.monotonic() + timeout_s
    while True:
        pids = _serialwrapd_pids()
        if not pids:
            return
        if time.monotonic() >= deadline:
            pytest.fail(f"隔離 pid namespace 內 doctor 比對前仍有殘留 serialwrapd：pid={pids}")
        time.sleep(0.2)


def _profile_tree_snapshot(config_dir: Path) -> tuple[tuple[str, bool, bytes | None], ...]:
    """擷取隔離設定目錄的相對路徑、目錄型態與檔案 bytes。"""
    if not config_dir.exists():
        return ()
    return tuple(
        sorted(
            (
                item.relative_to(config_dir).as_posix(),
                item.is_dir(),
                None if item.is_dir() else item.read_bytes(),
            )
            for item in config_dir.rglob("*")
        )
    )


def _run_rust_doctor_with_profile(work: Path, profile_bytes: bytes | None) -> tuple[dict, dict, subprocess.CompletedProcess[str]]:
    """執行指定 Rust artifact；doctor 只讀隔離設定目錄，不啟動 daemon。"""
    assert is_rust(), "此 blackbox 必須以 SERIALWRAP_AB_LABEL=rust-p289 執行"
    env = isolated_env(work)
    config_dir = Path(env["SERIALWRAP_CONFIG_DIR"])
    if profile_bytes is not None:
        profile_dir = config_dir / "profiles"
        profile_dir.mkdir(parents=True, exist_ok=True)
        (profile_dir / "default.yaml").write_bytes(profile_bytes)
    before = _profile_tree_snapshot(config_dir)
    proc = subprocess.run(
        cli_prefix() + ["doctor"],
        env=env,
        capture_output=True,
        text=True,
        timeout=30,
    )
    assert proc.returncode == 0, f"Rust doctor exit={proc.returncode}: {proc.stderr}"
    result = json.loads(proc.stdout)
    checks = [item for item in result["checks"] if item.get("check") == "profile_template_fields"]
    assert len(checks) == 1, checks
    assert _profile_tree_snapshot(config_dir) == before, "doctor 不得改寫 profile 或其目錄內容"
    return result, checks[0], proc


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

    _wait_no_serialwrapd()
    py_proc = subprocess.run(
        python_baseline_prefix() + ["doctor"],
        env=env_py,
        capture_output=True,
        text=True,
        timeout=30,
    )
    _wait_no_serialwrapd()
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

    # Python 基線固定 19 項；Rust 依 BC-08 只額外插入一個 profile_template_fields。
    assert len(py_checks) == 19
    if is_rust():
        profile_checks = [c for c in cli_checks if c.get("check") == "profile_template_fields"]
        assert len(profile_checks) == 1, profile_checks
        profile_index = next(i for i, item in enumerate(cli_checks) if item["check"] == "profile_template_fields")
        assert cli_checks[profile_index - 1]["check"] == "profile_bootloader_prompts"
        assert profile_checks[0]["advisory"] is True
        cli_shared_checks = [c for c in cli_checks if c["check"] != "profile_template_fields"]
        assert len(cli_checks) == 20
    else:
        assert len(cli_checks) == 19
        cli_shared_checks = cli_checks
    assert [c["check"] for c in cli_shared_checks] == [c["check"] for c in py_checks]

    for py_item, cli_item in zip(py_checks, cli_shared_checks):
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


@pytest.mark.rust_only
@pytest.mark.case("bc08-doctor-profile-template-fields")
def test_doctor_profile_template_fields_exact_payload(tmp_path: Path) -> None:
    profile = (
        b"profiles:\n"
        b"  prpl-template:\n"
        b"    platform: prpl\n"
        b"    prompt_regex: x\n"
        b"    login_regex: ''\n"
        b"    password_regex: ''\n"
        b"    ready_probe: ''\n"
        b"    uart: {}\n"
    )
    result, check, _ = _run_rust_doctor_with_profile(tmp_path, profile)
    assert check["ok"] is False
    assert check["advisory"] is True
    assert {
        key: check.get(key)
        for key in ("comparison_status", "missing_fields", "compared_templates", "reason")
    } == {
        "comparison_status": "missing_fields",
        "missing_fields": {"prpl-template": ["boot_done_regex", "bootloader_prompts", "timeout_recovery_keys"]},
        "compared_templates": ["prpl-template"],
        "reason": None,
    }
    assert isinstance(result.get("ok"), bool)


@pytest.mark.rust_only
@pytest.mark.case("bc08-doctor-profile-unavailable")
def test_doctor_profile_template_fields_invalid_yaml(tmp_path: Path) -> None:
    profile = b"profiles: [SW_SECRET_PROFILE_SENTINEL\n"
    _, check, proc = _run_rust_doctor_with_profile(tmp_path, profile)
    assert check["ok"] is False
    assert check["advisory"] is True
    assert {
        key: check.get(key)
        for key in ("comparison_status", "missing_fields", "compared_templates", "reason")
    } == {
        "comparison_status": "unavailable",
        "missing_fields": None,
        "compared_templates": [],
        "reason": "live_yaml",
    }
    assert "SW_SECRET_PROFILE_SENTINEL" not in proc.stdout + proc.stderr


@pytest.mark.rust_only
@pytest.mark.case("bc08-doctor-profile-unavailable")
def test_doctor_profile_template_fields_not_materialized(tmp_path: Path) -> None:
    _, check, _ = _run_rust_doctor_with_profile(tmp_path, None)
    assert check["ok"] is False
    assert check["advisory"] is True
    assert {
        key: check.get(key)
        for key in ("comparison_status", "missing_fields", "compared_templates", "reason")
    } == {
        "comparison_status": "not_materialized",
        "missing_fields": None,
        "compared_templates": [],
        "reason": "live_not_found",
    }


@pytest.mark.case("sc25-release-manifest-version-pairing")
@pytest.mark.pidns_rig
def test_doctor_ab_version_pairing_in_bundle(tmp_path: Path) -> None:
    _assert_doctor_payload_mutations_rejected()

    # 受測 artifact：有指定 SERIALWRAP_CLI_EXE（Rust）時即為該 bundle（不另行重建）；否則建置自含 bundle。
    artifact = _specified_rust_bundle()
    bundle_root = artifact if artifact is not None else _full_bundle_version_dir(tmp_path)
    evidence = _require_artifact(bundle_root)
    rust_cli = str(bundle_root / "bin" / "serialwrap")
    manifest = str(bundle_root / "release-manifest.json")

    # Python oracle：自含 bundle 內的 py（P1 T18，`-I -m sw_core.cli`）；sentinel artifact 的 py 不含 sw_core，
    # 則以凍結的 Python 基線對同一份 manifest 與檔案執行（version_pairing 只比對 manifest 目錄內的檔案）。
    full_oracle = _bundle_has_python_oracle(bundle_root)
    python_fp = _python_code_fingerprint(bundle_root, full_oracle)
    if full_oracle:
        py_doctor = [str(bundle_root / "py" / "bin" / "python3"), "-I", "-B", "-m", "sw_core.cli", "doctor"]
    else:
        py_doctor = python_baseline_prefix() + ["doctor"]

    def _pair(env: dict[str, str]) -> tuple[list[dict], list[dict]]:
        py_res = subprocess.run(py_doctor, env=env, capture_output=True, text=True, timeout=30)
        rust_res = subprocess.run([rust_cli, "doctor"], env=env, capture_output=True, text=True, timeout=30)
        assert py_res.returncode == 0, f"Python doctor 失敗：{py_res.stderr}（證據 {evidence}）"
        assert rust_res.returncode == 0, f"Rust doctor 失敗：{rust_res.stderr}（證據 {evidence}）"
        return json.loads(py_res.stdout)["checks"], json.loads(rust_res.stdout)["checks"]

    def _assert_same_checks(py_checks: list[dict], rust_checks: list[dict], state: str) -> None:
        py_version_pairing = [c for c in py_checks if c.get("check") == "version_pairing"]
        assert len(py_version_pairing) == 1, f"{state}: Python bundle 應保留唯一 version_pairing（證據 {evidence}）"
        assert len(py_checks) == 20, f"{state}: Python 應為 19 項基線加 version_pairing（證據 {evidence}）"
        assert len(rust_checks) == 21, f"{state}: Rust 應為 19 項基線、version_pairing 加唯一 BC-08（證據 {evidence}）"
        profile_checks = [c for c in rust_checks if c.get("check") == "profile_template_fields"]
        assert len(profile_checks) == 1, f"{state}: Rust profile_template_fields 必須恰好一項（證據 {evidence}）"
        profile_index = next(i for i, item in enumerate(rust_checks) if item["check"] == "profile_template_fields")
        assert rust_checks[profile_index - 1]["check"] == "profile_bootloader_prompts", f"{state}: BC-08 插入位置錯誤（證據 {evidence}）"
        assert profile_checks[0]["advisory"] is True, f"{state}: BC-08 必須為 advisory（證據 {evidence}）"
        shared_rust_checks = [item for item in rust_checks if item["check"] != "profile_template_fields"]
        assert [c["check"] for c in shared_rust_checks] == [c["check"] for c in py_checks], f"{state}: Python 19 項順序或集合不一致（證據 {evidence}）"
        for py_item, rust_item in zip(py_checks, shared_rust_checks):
            name = py_item["check"]
            if name == "daemon_code_current":
                # 此 advisory 比對各 CLI 與目前 daemon 的實作指紋；Stage 2 Rust daemon 的指紋不等於 Python 基線。
                assert py_item["advisory"] is True and rust_item["advisory"] is True, state
                if state == "daemon_down":
                    expected = _daemon_code_current_payload(
                        ok=True,
                        detail="daemon 未在跑或無法連線，略過",
                        fix="",
                    )
                    _assert_doctor_check_payload(py_item, expected, f"{state}: Python")
                    _assert_doctor_check_payload(rust_item, expected, f"{state}: Rust")
                elif is_rust():
                    daemon_fp = rust_daemon_code_fingerprint()
                    expected_rust = _daemon_code_current_payload(
                        ok=True,
                        detail=f"daemon 與目前安裝的程式碼相同（code {daemon_fp}）",
                        fix="",
                    )
                    expected_py = _daemon_code_current_payload(
                        ok=False,
                        detail=(
                            f"daemon 載入的程式碼（code {daemon_fp}）與目前安裝的（code {python_fp}）不同"
                        ),
                        fix=_DAEMON_CODE_CURRENT_FIX,
                    )
                    _assert_doctor_check_payload(py_item, expected_py, f"{state}: Python")
                    _assert_doctor_check_payload(rust_item, expected_rust, f"{state}: Rust")
                else:
                    expected = _daemon_code_current_payload(
                        ok=True,
                        detail=f"daemon 與目前安裝的程式碼相同（code {python_fp}）",
                        fix="",
                    )
                    _assert_doctor_check_payload(py_item, expected, f"{state}: Python")
                    _assert_doctor_check_payload(rust_item, expected, f"{state}: Rust")
                continue
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

    # A 輪以 Python daemon、B 輪以候選 daemon；daemon_code_current 是依實作指紋判定的 advisory。
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
