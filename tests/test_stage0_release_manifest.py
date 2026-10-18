# tests/test_stage0_release_manifest.py
"""P1 Stage 0 Task T18 紅燈測試：Python 側 release manifest、tree_sha256、version_pairing、DF-05 daemon 定位。

依 02-stage0-python-bc.md Global Constraints：
本模組不在頂層 import 本 Phase 才新增的模組（`sw_core.release_manifest`），
一律於測試函式或 fixture 內以 `_load_rm()` 延遲 import，使
`python3 -m pytest -q --collect-only tests/` 在任何階段都能完成收集。

marker 規則（P0 T14）：
本檔所有測試皆為純記憶體／暫存檔單元測試，無平台相依 syscall，標註：
pytestmark = [pytest.mark.core_portable, pytest.mark.memory_only]
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import sys
from pathlib import Path
from typing import Any
from unittest import mock

import pytest

pytestmark = [pytest.mark.core_portable, pytest.mark.memory_only]

BASELINE_LINUX_CHECKS = [
    "python",
    "pyyaml",
    "serialwrap_on_path",
    "serialwrapd_on_path",
    "other_serialwrap_installs",
    "dialout",
    "serialwrap_minicom_on_path",
    "jq_on_path",
    "minicom_on_path",
    "systemd",
    "supervision_mode",
    "single_daemon",
    "endpoint_reachable",
    "wal_dir",
    "wal_writable",
    "profile_bootloader_prompts",
    "devices",
    "wsl_systemd",
]

BASELINE_WINDOWS_CHECKS = [
    "python",
    "pyyaml",
    "pyserial",
    "serialwrap_on_path",
    "serialwrapd_on_path",
    "other_serialwrap_installs",
    "supervision_mode",
    "daemon_endpoint",
    "wal_dir",
    "wal_writable",
    "profile_bootloader_prompts",
    "devices",
]


def _load_rm() -> Any:
    """延遲 import sw_core.release_manifest；實作前拋出 ImportError。"""
    from sw_core import release_manifest as rm

    return rm


def _manifest(tmp_path: Path, **over: Any) -> Path:
    """建立測試用 release manifest 檔案及假執行檔。"""
    rm = _load_rm()
    (tmp_path / "bin").mkdir(exist_ok=True)
    for rel, data in (("bin/serialwrap", b"cli"), ("bin/serialwrapd", b"launcher"), ("py/python", b"py")):
        (tmp_path / rel).parent.mkdir(parents=True, exist_ok=True)
        (tmp_path / rel).write_bytes(data)

    def h(b: bytes) -> str:
        return hashlib.sha256(b).hexdigest()

    m: dict[str, Any] = {
        "schema_version": 1,
        "bundle_version": "0.4.0",
        "stage": "1A",
        "target": "x86_64-unknown-linux-gnu",
        "git_revision": "0" * 40,
        "contract_revision": "1" * 40,
        "created_at": "2026-12-01T03:00:00+00:00",
        "components": {
            "cli": {
                "implementation": "rust",
                "version": "0.4.0",
                "git_revision": "0" * 40,
                "path": "bin/serialwrap",
                "sha256": h(b"cli"),
            },
            "daemon": {
                "implementation": "python",
                "version": "0.4.0",
                "git_revision": "0" * 40,
                "launcher": {"path": "bin/serialwrapd", "sha256": h(b"launcher")},
                "entry": ["py/python", "-I", "-B", "py/serialwrapd.py"],
                "runtime": {"path": "py/python", "python_version": "3.12.7", "sha256": h(b"py")},
                "package": {
                    "name": "serialwrap",
                    "version": "0.4.0",
                    "wheel_sha256": "2" * 64,
                    "tree_sha256": "TREE",
                },
            },
            "python_cli_delegate": {
                "entry": ["py/python", "-I", "-B", "-m", "sw_core.cli"],
                "tree_sha256": "TREE",
            },
            "assets": {
                "skill_sha256": "S1",
                "skill_windows_sha256": "S2",
                "profiles_sha256": "3" * 64,
            },
            "supervisor": {
                "default_mode": "systemd-user",
                "definition_sha256": "4" * 64,
            },
        },
        "pairing": {"rule": "exact", "cli_version": "0.4.0", "daemon_version": "0.4.0"},
    }
    m.update(over)
    p = tmp_path / rm.MANIFEST_NAME
    p.write_text(json.dumps(m), encoding="utf-8")
    return p


def test_tree_sha256_matches_golden_vectors() -> None:
    """驗證 tree_sha256 演算法與 golden 向量相符。"""
    _load_rm()
    root = Path(__file__).resolve().parents[1] / "contracts" / "golden"
    for line in (root / "tree_sha256.ndjson").read_text(encoding="utf-8").splitlines():
        case = json.loads(line)
        entries = "".join(
            f"{rel}\0{hashlib.sha256(data.encode()).hexdigest()}\n"
            for rel, data in sorted(case["files"].items(), key=lambda kv: kv[0].encode())
        )
        assert hashlib.sha256(entries.encode()).hexdigest() == case["tree_sha256"]


def test_tree_sha256_excludes_pycache(tmp_path: Path) -> None:
    """驗證 tree_sha256 排除 __pycache__ 目錄與 .pyc 檔案。"""
    rm = _load_rm()
    (tmp_path / "a.py").write_text("x", encoding="utf-8")
    (tmp_path / "__pycache__").mkdir()
    (tmp_path / "__pycache__" / "a.cpython-312.pyc").write_bytes(b"\0")
    expected = hashlib.sha256(f"a.py\0{hashlib.sha256(b'x').hexdigest()}\n".encode()).hexdigest()
    assert rm.tree_sha256(str(tmp_path)) == expected


def test_pairing_ok(tmp_path: Path) -> None:
    """驗證版本與雜湊全符合時 check_version_pairing 回傳 ok。"""
    rm = _load_rm()
    p = _manifest(tmp_path)
    m = rm.load_release_manifest(str(p))
    item = rm.check_version_pairing(
        m,
        manifest_dir=str(tmp_path),
        cli_version="0.4.0",
        daemon_version="0.4.0",
        package_tree=lambda: "TREE",
        asset_sha256={"skill": "S1", "skill_windows": "S2"}.get,
    )
    assert item == {"check": "version_pairing", "ok": True, "advisory": False, "detail": "ok", "mismatches": []}


def test_pairing_reports_mismatches_in_order(tmp_path: Path) -> None:
    """驗證 check_version_pairing 依規格凍結順序列出 mismatches。"""
    rm = _load_rm()
    p = _manifest(tmp_path)
    m = rm.load_release_manifest(str(p))
    (tmp_path / "bin" / "serialwrap").write_bytes(b"tampered")
    item = rm.check_version_pairing(
        m,
        manifest_dir=str(tmp_path),
        cli_version="0.4.1",
        daemon_version=None,
        package_tree=lambda: "OTHER",
        asset_sha256={"skill": "S1", "skill_windows": "X"}.get,
    )
    assert item["ok"] is False
    assert item["mismatches"] == [
        "cli_version",
        "daemon_unreachable",
        "cli_sha256",
        "package_tree_sha256",
        "python_cli_delegate_tree_sha256",
        "skill_windows_sha256",
    ]


def test_find_manifest_env_then_bundle_dir(tmp_path: Path) -> None:
    """驗證 find_release_manifest 優先序：env > bundle dir > None。"""
    rm = _load_rm()
    p = _manifest(tmp_path)
    assert rm.find_release_manifest(env={"SERIALWRAP_RELEASE_MANIFEST": str(p)}, argv0="/usr/bin/x") == str(p)
    assert rm.find_release_manifest(env={}, argv0=str(tmp_path / "bin" / "serialwrap")) == str(p)
    assert rm.find_release_manifest(env={}, argv0="/nonexistent/bin/serialwrap") is None


def test_daemon_launch_argv_order(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """驗證 DF-05 daemon 定位順序：manifest > 同層 serialwrapd > checkout script > python -m。"""
    rm = _load_rm()
    p = _manifest(tmp_path)
    none_py = str(tmp_path / "no-serialwrapd.py")  # 測試在 checkout 中執行，需明確停用 checkout 分支
    assert rm.daemon_launch_argv(
        env={"SERIALWRAP_RELEASE_MANIFEST": str(p)}, argv0="/x/serialwrap", frozen=False, script_path=none_py
    ) == [str(tmp_path / "bin" / "serialwrapd")]

    # T18 計畫偏差（見 changelog／回報）：manifest 查找為 argv0 的 bin/../release-manifest.json，
    # 原 side=tmp/side 的父層即 tmp_path（_manifest 寫入處），會先命中 manifest（DF-05 步驟 1）。
    # 改為 tmp/side/bin，使 bin/../ 不含 manifest，仍驗證步驟 3（同目錄 serialwrapd）。
    side = tmp_path / "side" / "bin"
    side.mkdir(parents=True)
    # Windows 上 DF-05 步驟 3 的同目錄執行檔為 serialwrapd.exe（release_manifest 以 os.name 判定）。
    d = side / ("serialwrapd.exe" if os.name == "nt" else "serialwrapd")
    d.write_text("#!/bin/sh\n", encoding="utf-8")
    d.chmod(0o755)
    assert rm.daemon_launch_argv(env={}, argv0=str(side / "serialwrap"), frozen=False, script_path=none_py) == [str(d)]

    script = tmp_path / "serialwrapd.py"
    script.write_text("", encoding="utf-8")
    assert rm.daemon_launch_argv(
        env={}, argv0="/nonexistent/serialwrap", python="/py", frozen=False, script_path=str(script)
    ) == ["/py", str(script)]

    assert rm.daemon_launch_argv(
        env={}, argv0="/nonexistent/serialwrap", python="/py", frozen=False, script_path=none_py
    ) == ["/py", "-m", "sw_core.daemon"]


def test_tree_sha256_impl_matches_golden_vectors(tmp_path: Path) -> None:
    """實作 rm.tree_sha256 對 golden 每筆向量相符（整樹掃描與 files= 兩種路徑皆需通過）。"""
    rm = _load_rm()
    root = Path(__file__).resolve().parents[1] / "contracts" / "golden"
    lines = (root / "tree_sha256.ndjson").read_text(encoding="utf-8").splitlines()
    assert len(lines) >= 4
    for idx, line in enumerate(lines):
        case = json.loads(line)
        base = tmp_path / f"case{idx}"
        for rel, data in case["files"].items():
            target = base / rel
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_bytes(data.encode("utf-8"))
        assert rm.tree_sha256(str(base)) == case["tree_sha256"], f"case {idx} full-walk"
        assert rm.tree_sha256(str(base), files=list(case["files"])) == case["tree_sha256"], f"case {idx} files="


def test_tree_sha256_excludes_nested_pycache_and_stray_pyc(tmp_path: Path) -> None:
    """巢狀 __pycache__ 與目錄外的 .pyc 皆排除；其餘 .py 照常計入。"""
    rm = _load_rm()
    (tmp_path / "pkg" / "__pycache__").mkdir(parents=True)
    (tmp_path / "pkg" / "__init__.py").write_bytes(b"")
    (tmp_path / "pkg" / "__pycache__" / "__init__.cpython-312.pyc").write_bytes(b"\0")
    (tmp_path / "pkg" / "stray.pyc").write_bytes(b"\1")
    entries = f"pkg/__init__.py\0{hashlib.sha256(b'').hexdigest()}\n"
    assert rm.tree_sha256(str(tmp_path)) == hashlib.sha256(entries.encode("utf-8")).hexdigest()


def test_tree_sha256_missing_root_raises(tmp_path: Path) -> None:
    """根目錄不存在時拋出 OSError，不得回傳空樹雜湊（os.walk onerror）。"""
    rm = _load_rm()
    with pytest.raises(OSError):
        rm.tree_sha256(str(tmp_path / "missing"))


def test_package_tree_sha256_matches_independent_recompute() -> None:
    """package_tree_sha256 的 relpath 以 sw_core/ 起頭，且與獨立重算相等。"""
    rm = _load_rm()
    import sw_core

    pkg = Path(sw_core.__file__).resolve().parent
    pairs: list[tuple[str, str]] = []
    for path in sorted(pkg.rglob("*")):
        if not path.is_file():
            continue
        parts = path.relative_to(pkg.parent).parts
        if "__pycache__" in parts or path.suffix == ".pyc":
            continue
        pairs.append(("/".join(parts), hashlib.sha256(path.read_bytes()).hexdigest()))
    assert pairs and all(rel.startswith("sw_core/") for rel, _ in pairs)
    entries = "".join(f"{rel}\0{h}\n" for rel, h in sorted(pairs, key=lambda kv: kv[0].encode("utf-8")))
    assert rm.package_tree_sha256() == hashlib.sha256(entries.encode("utf-8")).hexdigest()


def _asset_stub(name: str) -> str:
    return {"skill": "S1", "skill_windows": "S2"}[name]


def test_pairing_rejects_mixed_versions(tmp_path: Path) -> None:
    """規格 §9.5／arch §10.3：CLI 與 daemon 皆須等於 bundle_version；pairing 自洽的舊版仍不得通過。"""
    rm = _load_rm()
    p = _manifest(tmp_path)
    base = rm.load_release_manifest(str(p))
    common = dict(
        manifest_dir=str(tmp_path),
        package_tree=lambda: "TREE",
        asset_sha256=_asset_stub,
    )

    old_daemon = json.loads(json.dumps(base))
    old_daemon["pairing"]["daemon_version"] = "0.3.2"
    old_daemon["components"]["daemon"]["version"] = "0.3.2"
    item = rm.check_version_pairing(old_daemon, cli_version="0.4.0", daemon_version="0.3.2", **common)
    assert item["ok"] is False
    assert item["mismatches"] == ["daemon_version"]

    # 不自洽：pairing 宣告 0.4.0，但 daemon 實際回報 0.3.2
    item = rm.check_version_pairing(base, cli_version="0.4.0", daemon_version="0.3.2", **common)
    assert item["mismatches"] == ["daemon_version"]

    old_cli = json.loads(json.dumps(base))
    old_cli["pairing"]["cli_version"] = "0.3.2"
    old_cli["components"]["cli"]["version"] = "0.3.2"
    item = rm.check_version_pairing(old_cli, cli_version="0.3.2", daemon_version="0.4.0", **common)
    assert item["ok"] is False
    assert item["mismatches"] == ["cli_version"]


def test_pairing_rejects_non_exact_rule_when_called_directly(tmp_path: Path) -> None:
    """check_version_pairing 不依賴呼叫端先行驗證：rule 非 exact 直接拋 invalid release manifest。"""
    rm = _load_rm()
    m = rm.load_release_manifest(str(_manifest(tmp_path)))
    m["pairing"]["rule"] = "loose"
    with pytest.raises(ValueError, match="invalid release manifest: pairing.rule"):
        rm.check_version_pairing(
            m, manifest_dir=str(tmp_path), cli_version="0.4.0", daemon_version="0.4.0",
            package_tree=lambda: "TREE", asset_sha256=_asset_stub,
        )


def test_hash_format_fields_must_be_strings(tmp_path: Path) -> None:
    """64 位數字的整數不得因 str() 通過 hex 格式檢查；載入層亦拒絕非字串。"""
    rm = _load_rm()
    p = _manifest(tmp_path)
    m = rm.load_release_manifest(str(p))
    m["components"]["assets"]["profiles_sha256"] = int("1" * 64)
    m["components"]["supervisor"]["definition_sha256"] = int("2" * 64)
    item = rm.check_version_pairing(
        m, manifest_dir=str(tmp_path), cli_version="0.4.0", daemon_version="0.4.0",
        package_tree=lambda: "TREE", asset_sha256=_asset_stub,
    )
    assert "profiles_sha256_format" in item["mismatches"]
    assert "supervisor_definition_sha256_format" in item["mismatches"]

    raw = json.loads(p.read_text(encoding="utf-8"))
    raw["components"]["assets"]["profiles_sha256"] = 1234
    p.write_text(json.dumps(raw), encoding="utf-8")
    with pytest.raises(ValueError, match="invalid release manifest: components.assets.profiles_sha256"):
        rm.load_release_manifest(str(p))


def test_launcher_invalid_type_rejected_and_startup_falls_back(tmp_path: Path) -> None:
    """launcher.path 為 null／數字 → 載入拒絕（欄位名稱明確）；daemon start 退回後續定位順序而非 AttributeError。"""
    rm = _load_rm()
    p = _manifest(tmp_path)
    none_py = str(tmp_path / "no-serialwrapd.py")
    for bad in (None, 5):
        raw = json.loads(p.read_text(encoding="utf-8"))
        raw["components"]["daemon"]["launcher"]["path"] = bad
        p.write_text(json.dumps(raw), encoding="utf-8")
        with pytest.raises(ValueError, match="invalid release manifest: components.daemon.launcher.path"):
            rm.load_release_manifest(str(p))
        argv = rm.daemon_launch_argv(
            env={"SERIALWRAP_RELEASE_MANIFEST": str(p)}, argv0="/nonexistent/serialwrap",
            python="/py", frozen=False, script_path=none_py,
        )
        assert argv == ["/py", "-m", "sw_core.daemon"]


def test_absolute_launcher_path_is_not_rerooted(tmp_path: Path) -> None:
    """dev bundle 的 launcher 為 repo 內絕對路徑：不得被 manifest 目錄重新掛載（否則假 launcher_sha256 不符）。"""
    rm = _load_rm()
    repo = tmp_path / "repo"
    repo.mkdir()
    launcher = repo / "serialwrapd.py"
    launcher.write_text("#!/usr/bin/env python3\n", encoding="utf-8")
    bundle = tmp_path / "bundle"
    bundle.mkdir()
    p = _manifest(bundle)
    raw = json.loads(p.read_text(encoding="utf-8"))
    raw["components"]["daemon"]["launcher"] = {"path": str(launcher), "sha256": rm.file_sha256(str(launcher))}
    p.write_text(json.dumps(raw), encoding="utf-8")

    loaded = rm.load_release_manifest(str(p))
    item = rm.check_version_pairing(
        loaded, manifest_dir=str(bundle), cli_version="0.4.0", daemon_version="0.4.0",
        package_tree=lambda: "TREE", asset_sha256=_asset_stub,
    )
    assert "launcher_sha256" not in item["mismatches"]

    argv = rm.daemon_launch_argv(
        env={"SERIALWRAP_RELEASE_MANIFEST": str(p)}, argv0="/x/serialwrap",
        frozen=False, script_path=str(tmp_path / "no-serialwrapd.py"),
    )
    assert argv == [os.path.abspath(str(launcher))]


def test_deeply_nested_manifest_is_invalid_not_crash(tmp_path: Path) -> None:
    """深層巢狀 JSON 不得以 RecursionError 穿越；回報 invalid release manifest。"""
    rm = _load_rm()
    p = tmp_path / rm.MANIFEST_NAME
    p.write_text("[" * 100000 + "]" * 100000, encoding="utf-8")
    with pytest.raises(ValueError, match="invalid release manifest: unreadable"):
        rm.load_release_manifest(str(p))


def test_doctor_pairing_probes_resolved_endpoint(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """config.yaml 殘留失效路徑時，doctor 須探測 CLI 實際解析到的 canonical endpoint（#108 fallback）。"""
    _load_rm()
    from sw_core import doctor_cmd

    p = _manifest(tmp_path)
    monkeypatch.setenv("SERIALWRAP_RELEASE_MANIFEST", str(p))
    seen: list[str] = []

    def fake_rpc(endpoint: str, method: str, params: dict, timeout_s: float | None = None, **_kw: Any) -> dict:
        seen.append(endpoint)
        return {"ok": True, "version": "0.4.0"}

    with (
        mock.patch(
            "sw_core.cli._resolve_default_endpoint_with_source",
            return_value=("/canonical/live.sock", "config.yaml（記錄 /stale/config.sock 不可連）"),
        ),
        mock.patch("sw_core.client.rpc_call", side_effect=fake_rpc),
    ):
        item = doctor_cmd._check_version_pairing(str(p))
    assert seen == ["/canonical/live.sock"]
    assert "daemon_unreachable" not in item["mismatches"]
    assert "daemon_version" not in item["mismatches"]


def test_doctor_pairing_fix_text_for_unreachable_only(tmp_path: Path) -> None:
    """只缺 daemon 時 fix 指向 daemon start，而非重裝發佈組合。"""
    _load_rm()
    from sw_core import doctor_cmd

    p = _manifest(tmp_path)
    only_unreachable = {
        "check": "version_pairing", "ok": False, "advisory": False,
        "detail": "daemon_unreachable", "mismatches": ["daemon_unreachable"],
    }
    with (
        mock.patch("sw_core.cli._resolve_default_endpoint_with_source", return_value=("/x.sock", "預設")),
        mock.patch("sw_core.client.rpc_call", return_value={"ok": False}),
        mock.patch("sw_core.release_manifest.check_version_pairing", return_value=dict(only_unreachable)),
    ):
        item = doctor_cmd._check_version_pairing(str(p))
    assert item["fix"] == "serialwrap daemon start（或 service start）後重跑 doctor"


def test_doctor_without_manifest_unchanged(monkeypatch: pytest.MonkeyPatch) -> None:
    """無 manifest 時 run_doctor() 的 check 名稱清單與實作前相同，不含 version_pairing。"""
    _load_rm()
    from sw_core.doctor_cmd import run_doctor
    from sw_core.sysenv import FakeEffects

    monkeypatch.delenv("SERIALWRAP_RELEASE_MANIFEST", raising=False)
    report = run_doctor(fx=FakeEffects(systemd=True, in_groups={"dialout"}), platform="linux")
    checks = [i["check"] for i in report]
    assert checks == BASELINE_LINUX_CHECKS
    assert "version_pairing" not in checks

    win_report = run_doctor(fx=FakeEffects(), platform="win32")
    win_checks = [i["check"] for i in win_report]
    assert win_checks == BASELINE_WINDOWS_CHECKS
    assert "version_pairing" not in win_checks


def test_doctor_with_manifest_appends_pairing_last(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """設 SERIALWRAP_RELEASE_MANIFEST → 最後一個 item 的 check == 'version_pairing'、advisory is False。"""
    _load_rm()
    from sw_core.doctor_cmd import run_doctor
    from sw_core.sysenv import FakeEffects

    p = _manifest(tmp_path)
    monkeypatch.setenv("SERIALWRAP_RELEASE_MANIFEST", str(p))
    report = run_doctor(fx=FakeEffects(systemd=True, in_groups={"dialout"}), platform="linux")
    assert len(report) > 0
    last_item = report[-1]
    assert last_item["check"] == "version_pairing"
    assert last_item["advisory"] is False


def test_invalid_manifest_reported(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """schema_version: 2 → item ok is False、detail == 'invalid release manifest: schema_version'。"""
    rm = _load_rm()
    from sw_core.doctor_cmd import run_doctor
    from sw_core.sysenv import FakeEffects

    p = _manifest(tmp_path, schema_version=2)
    with pytest.raises(ValueError, match="invalid release manifest: schema_version"):
        rm.load_release_manifest(str(p))

    monkeypatch.setenv("SERIALWRAP_RELEASE_MANIFEST", str(p))
    report = run_doctor(fx=FakeEffects(systemd=True, in_groups={"dialout"}), platform="linux")
    item = report[-1]
    assert item["check"] == "version_pairing"
    assert item["ok"] is False
    assert item["detail"] == "invalid release manifest: schema_version"


def test_cli_daemon_start_uses_launch_argv(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """cli._run_daemon_start 的 spawn argv 前綴等於 daemon_launch_argv() 結果（替身 ProcessCtl）。"""
    rm = _load_rm()
    from sw_core import cli

    p = _manifest(tmp_path)
    monkeypatch.setenv("SERIALWRAP_RELEASE_MANIFEST", str(p))

    args = argparse.Namespace(
        profile_dir=str(tmp_path / "profiles"),
        socket=str(tmp_path / "sw.sock"),
        lock=str(tmp_path / "sw.lock"),
        foreground=False,
        with_sudo=False,
        endpoint=None,
        force_spawn=True,
    )
    fake_proc = mock.Mock()
    # T18 計畫偏差（見回報）：替身行程必須仍在執行（poll() 回 None），否則
    # _run_daemon_start 會依 DAEMON_EXITED 路徑回 2，與「spawn 成功回 0」的斷言矛盾。
    fake_proc.poll.return_value = None
    with (
        mock.patch("sw_core.cli._safe_runtime_config", return_value=None),
        mock.patch("sw_core.cli._probe_healthy_daemon", return_value=False),
        mock.patch("sw_core.cli._find_conflicting_daemon", return_value=None),
        mock.patch("sw_core.cli.subprocess.Popen", return_value=fake_proc) as popen,
        mock.patch("sw_core.cli.rpc_call", return_value={"ok": True}),
        mock.patch("sw_core.cli.time.sleep"),
        mock.patch("sw_core.cli._print"),
    ):
        rc = cli._run_daemon_start(args)
        assert rc == 0
        spawned_cmd = popen.call_args.args[0]
        expected_prefix = rm.daemon_launch_argv(argv0=sys.argv[0])
        assert expected_prefix == [str(tmp_path / "bin" / "serialwrapd")]
        assert spawned_cmd[: len(expected_prefix)] == expected_prefix


def test_pipx_layout_no_longer_spawns_missing_serialwrapd_py(tmp_path: Path) -> None:
    """模擬 pipx 佈局（無 repo 根 serialwrapd.py、同目錄無 serialwrapd）→ argv 為 python -m sw_core.daemon（DF-05，CLI-7.4）。"""
    rm = _load_rm()
    argv = rm.daemon_launch_argv(
        env={},
        argv0=str(tmp_path / "bin" / "serialwrap"),
        python="/usr/bin/python3",
        frozen=False,
        script_path=str(tmp_path / "nonexistent" / "serialwrapd.py"),
    )
    assert argv == ["/usr/bin/python3", "-m", "sw_core.daemon"]

    argv_default = rm.daemon_launch_argv(
        env={},
        argv0=str(tmp_path / "bin" / "serialwrap"),
        frozen=False,
        script_path=str(tmp_path / "nonexistent" / "serialwrapd.py"),
    )
    assert argv_default == [sys.executable, "-m", "sw_core.daemon"]


def test_manifest_schema_file_matches_loader_required_keys(tmp_path: Path) -> None:
    """contracts/release_manifest.schema.json 的 required 與 load_release_manifest 驗證鍵一致（含 python_cli_delegate、assets、supervisor）。"""
    rm = _load_rm()
    schema_path = Path(__file__).resolve().parents[1] / "contracts" / "release_manifest.schema.json"
    assert schema_path.is_file(), f"schema file missing: {schema_path}"
    schema = json.loads(schema_path.read_text(encoding="utf-8"))

    top_required = set(schema.get("required", []))
    assert {
        "schema_version",
        "bundle_version",
        "stage",
        "created_at",
        "target",
        "git_revision",
        "contract_revision",
        "components",
        "pairing",
    }.issubset(top_required)
    comp_required = set(schema.get("properties", {}).get("components", {}).get("required", []))
    assert {"cli", "daemon", "python_cli_delegate", "assets", "supervisor"}.issubset(comp_required)

    for key in ("components.python_cli_delegate", "components.assets", "components.supervisor"):
        parts = key.split(".")
        p = _manifest(tmp_path)
        m = json.loads(p.read_text(encoding="utf-8"))
        sub = m
        for part in parts[:-1]:
            sub = sub[part]
        del sub[parts[-1]]
        p.write_text(json.dumps(m), encoding="utf-8")
        with pytest.raises(ValueError, match=f"invalid release manifest: {key}"):
            rm.load_release_manifest(str(p))


def test_frozen_branch_preserved(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """frozen=True、sys.executable 同層有 serialwrapd.exe → [<該路徑>]；同層沒有且 PATH 也沒有 → []。"""
    rm = _load_rm()
    fake_exe = tmp_path / "bin" / "serialwrap.exe"
    fake_exe.parent.mkdir(parents=True, exist_ok=True)
    fake_exe.write_text("", encoding="utf-8")
    monkeypatch.setattr(sys, "executable", str(fake_exe))

    sibling = fake_exe.parent / "serialwrapd.exe"
    sibling.write_text("", encoding="utf-8")
    with mock.patch("shutil.which", return_value=None):
        argv = rm.daemon_launch_argv(env={}, argv0=str(fake_exe), frozen=True)
        assert argv == [str(sibling)]

    sibling.unlink()
    with mock.patch("shutil.which", return_value=None):
        argv = rm.daemon_launch_argv(env={}, argv0=str(fake_exe), frozen=True)
        assert argv == []


def test_missing_delegate_key_invalid(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """刪除 components.python_cli_delegate → detail == 'invalid release manifest: components.python_cli_delegate'。"""
    rm = _load_rm()
    from sw_core.doctor_cmd import run_doctor
    from sw_core.sysenv import FakeEffects

    p = _manifest(tmp_path)
    m = json.loads(p.read_text(encoding="utf-8"))
    del m["components"]["python_cli_delegate"]
    p.write_text(json.dumps(m), encoding="utf-8")

    with pytest.raises(ValueError, match="invalid release manifest: components.python_cli_delegate"):
        rm.load_release_manifest(str(p))

    monkeypatch.setenv("SERIALWRAP_RELEASE_MANIFEST", str(p))
    report = run_doctor(fx=FakeEffects(systemd=True, in_groups={"dialout"}), platform="linux")
    item = report[-1]
    assert item["check"] == "version_pairing"
    assert item["ok"] is False
    assert item["detail"] == "invalid release manifest: components.python_cli_delegate"


def test_profiles_supervisor_hash_format_only(tmp_path: Path) -> None:
    """profiles_sha256: 'zz' → mismatches 含 'profiles_sha256_format'；合法 64 hex 但任意值 → 不列 mismatch。"""
    rm = _load_rm()
    p = _manifest(tmp_path)
    m = rm.load_release_manifest(str(p))

    # 合法 64 位小寫 hex 但非特定值 → 不列 mismatch
    m["components"]["assets"]["profiles_sha256"] = "f" * 64
    m["components"]["supervisor"]["definition_sha256"] = "e" * 64
    item = rm.check_version_pairing(
        m,
        manifest_dir=str(tmp_path),
        cli_version="0.4.0",
        daemon_version="0.4.0",
        package_tree=lambda: "TREE",
        asset_sha256={"skill": "S1", "skill_windows": "S2"}.get,
    )
    assert "profiles_sha256_format" not in item["mismatches"]
    assert "supervisor_definition_sha256_format" not in item["mismatches"]
    assert item["ok"] is True

    # 非法格式（如 'zz'）→ mismatches 含對應 format 項目
    m["components"]["assets"]["profiles_sha256"] = "zz"
    m["components"]["supervisor"]["definition_sha256"] = "zz"
    bad_item = rm.check_version_pairing(
        m,
        manifest_dir=str(tmp_path),
        cli_version="0.4.0",
        daemon_version="0.4.0",
        package_tree=lambda: "TREE",
        asset_sha256={"skill": "S1", "skill_windows": "S2"}.get,
    )
    assert "profiles_sha256_format" in bad_item["mismatches"]
    assert "supervisor_definition_sha256_format" in bad_item["mismatches"]
    assert bad_item["ok"] is False
