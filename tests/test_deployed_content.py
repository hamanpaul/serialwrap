# tests/test_deployed_content.py
"""實際部署內容量測（deployed-content.v1）與 tree digest、sidecar、daemon image 檢測測試。"""
from __future__ import annotations

import hashlib
import json
import os
import pathlib
import shutil
import subprocess
import sys
from typing import Any

import pytest

pytestmark = [pytest.mark.core_portable]

REPO_ROOT = pathlib.Path(__file__).resolve().parent.parent


def _sha256_bytes(b: bytes) -> str:
    return hashlib.sha256(b).hexdigest()


def _sha256_file(p: pathlib.Path) -> str:
    return hashlib.sha256(p.read_bytes()).hexdigest()


def _pin_posix_layout(monkeypatch: pytest.MonkeyPatch) -> None:
    """釘 POSIX bundle 佈局：install_dir 依 os.name 分支（nt 走 current-release.json）。

    本檔這些測試的假安裝樹皆為 POSIX 佈局（install = cli.parent.parent）；Windows 佈局另由
    test_deployed_content_windows_stable_bin_sidecar 覆蓋。僅影響單一測試，不 skip、不改斷言。
    """
    monkeypatch.setattr(os, "name", "posix")


@pytest.mark.memory_only
def test_deployed_content_sideload_changes_tree(tmp_path: pathlib.Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """驗證在安裝目錄旁載檔案會改變 bundle/runtime tree digest，但 release-manifest 與 CLI sha256 保持不變。"""
    import realhw.deployed_content as dc
    _pin_posix_layout(monkeypatch)

    v1 = tmp_path / "v1"
    bin_dir = v1 / "bin"
    bin_dir.mkdir(parents=True, exist_ok=True)
    cli_file = bin_dir / "serialwrap"
    cli_file.write_bytes(b"cli")
    manifest_file = v1 / "release-manifest.json"
    manifest_data = {
        "schema_version": "release-manifest.v1",
        "components": {
            "cli": {"path": "bin/serialwrap", "sha256": _sha256_bytes(b"cli")},
        },
    }
    manifest_file.write_text(json.dumps(manifest_data), encoding="utf-8")

    a = dc.measure(cli_file)
    assert a["error"] is None
    assert a["install_dir"] == str(v1)
    assert a["release_manifest_sha256"] == _sha256_file(manifest_file)
    assert len(a["bundle_tree_sha256"]) == 64
    assert len(a["runtime_tree_sha256"]) == 64
    assert a["bundle_tree_sha256"] != a["runtime_tree_sha256"]

    v2 = tmp_path / "v2"
    shutil.copytree(v1, v2)
    sidecar = v2 / "bin" / "version.dll"
    sidecar.write_bytes(b"sidecar-dll")

    b = dc.measure(v2 / "bin" / "serialwrap")
    assert b["release_manifest_sha256"] == a["release_manifest_sha256"]
    assert b["cli_sha256"] == a["cli_sha256"]
    assert b["bundle_tree_sha256"] != a["bundle_tree_sha256"]
    assert b["runtime_tree_sha256"] != a["runtime_tree_sha256"]


@pytest.mark.memory_only
def test_deployed_content_algorithm_vector(tmp_path: pathlib.Path) -> None:
    """驗證 tree_digests 演算法向量符合 sorted <posix relpath>\\0<sha256>\\n 之規範。"""
    import realhw.deployed_content as dc

    manifest_file = tmp_path / "release-manifest.json"
    manifest_file.write_bytes(b"m")
    sums_file = tmp_path / "SHA256SUMS"
    sums_file.write_bytes(b"s")
    x_file = tmp_path / "bin" / "x"
    x_file.parent.mkdir(parents=True, exist_ok=True)
    x_file.write_bytes(b"x")

    m_sha = _sha256_bytes(b"m")
    s_sha = _sha256_bytes(b"s")
    x_sha = _sha256_bytes(b"x")

    expected_bundle_seq = f"SHA256SUMS\0{s_sha}\nbin/x\0{x_sha}\nrelease-manifest.json\0{m_sha}\n"
    expected_runtime_seq = f"bin/x\0{x_sha}\n"

    expected_bundle_tree = _sha256_bytes(expected_bundle_seq.encode("utf-8"))
    expected_runtime_tree = _sha256_bytes(expected_runtime_seq.encode("utf-8"))

    bundle_tree, runtime_tree = dc.tree_digests(tmp_path)
    assert bundle_tree == expected_bundle_tree
    assert runtime_tree == expected_runtime_tree


@pytest.mark.memory_only
def test_deployed_content_rehashes_preserved_mtime(tmp_path: pathlib.Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """驗證 measure 每次皆重新計算內容雜湊，即使檔案 mtime/size 被竄改保留仍能正確識別差異。"""
    import realhw.deployed_content as dc
    _pin_posix_layout(monkeypatch)

    v1 = tmp_path / "v1"
    bin_dir = v1 / "bin"
    bin_dir.mkdir(parents=True, exist_ok=True)
    cli_file = bin_dir / "serialwrap"
    cli_file.write_bytes(b"cli")
    dll_file = bin_dir / "version.dll"
    dll_file.write_bytes(b"original")
    manifest_file = v1 / "release-manifest.json"
    manifest_file.write_text(json.dumps({
        "schema_version": "release-manifest.v1",
        "components": {"cli": {"path": "bin/serialwrap", "sha256": _sha256_bytes(b"cli")}},
    }), encoding="utf-8")

    m1 = dc.measure(cli_file)
    stat = dll_file.stat()
    orig_atime, orig_mtime = stat.st_atime, stat.st_mtime

    dll_file.write_bytes(b"modified")
    os.utime(dll_file, (orig_atime, orig_mtime))

    m2 = dc.measure(cli_file)
    assert m1["bundle_tree_sha256"] != m2["bundle_tree_sha256"]


@pytest.mark.memory_only
def test_deployed_content_errors_null_digests(tmp_path: pathlib.Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """驗證當出現 error 時，除 install_dir 外所有 digest 欄位一律為 null。"""
    import realhw.deployed_content as dc
    _pin_posix_layout(monkeypatch)

    # 1. unresolved: CLI 不在含 release-manifest.json 的安裝內
    unresolved_cli = tmp_path / "loose" / "serialwrap"
    unresolved_cli.parent.mkdir(parents=True, exist_ok=True)
    unresolved_cli.write_bytes(b"cli")
    m_unres = dc.measure(unresolved_cli)
    assert m_unres["error"] == "unresolved"
    assert m_unres["bundle_tree_sha256"] is None
    assert m_unres["runtime_tree_sha256"] is None
    assert m_unres["entry_dir_sha256"] is None

    # 2. cli_mismatch: 改寫 bin/serialwrap 使 sha 不符 manifest
    v1 = tmp_path / "v1"
    (v1 / "bin").mkdir(parents=True, exist_ok=True)
    cli = v1 / "bin" / "serialwrap"
    cli.write_bytes(b"cli-corrupt")
    (v1 / "release-manifest.json").write_text(json.dumps({
        "schema_version": "release-manifest.v1",
        "components": {"cli": {"path": "bin/serialwrap", "sha256": _sha256_bytes(b"expected-cli")}},
    }), encoding="utf-8")

    m_mismatch = dc.measure(cli)
    assert m_mismatch["error"] == "cli_mismatch"
    assert m_mismatch["bundle_tree_sha256"] is None
    assert m_mismatch["runtime_tree_sha256"] is None

    # 3. tree_invalid: 安裝內含有 symlink 或非一般檔
    cli.write_bytes(b"expected-cli")
    symlink_file = v1 / "bin" / "link"
    try:
        symlink_file.symlink_to(cli)
    except OSError:
        pass

    if not symlink_file.is_symlink():
        def fake_tree_digests(_p: Any) -> tuple[str, str]:
            raise dc.DeployedContentError("non-regular file: bin/link")
        monkeypatch.setattr(dc, "tree_digests", fake_tree_digests)

    m_sym = dc.measure(cli)
    assert str(m_sym["error"]).startswith("tree_invalid: ")
    assert m_sym["bundle_tree_sha256"] is None
    assert m_sym["runtime_tree_sha256"] is None


@pytest.mark.memory_only
def test_deployed_content_daemon_outside_install_flagged(tmp_path: pathlib.Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """驗證 daemon_argv 指向安裝目錄外或 sha256 不符時正確標記 error。"""
    import realhw.deployed_content as dc
    _pin_posix_layout(monkeypatch)

    v1 = tmp_path / "v1"
    (v1 / "bin").mkdir(parents=True, exist_ok=True)
    cli = v1 / "bin" / "serialwrap"
    cli.write_bytes(b"cli")
    daemon = v1 / "bin" / "serialwrapd"
    daemon.write_bytes(b"daemon")

    manifest = v1 / "release-manifest.json"
    manifest.write_text(json.dumps({
        "schema_version": "release-manifest.v1",
        "components": {
            "cli": {"path": "bin/serialwrap", "sha256": _sha256_bytes(b"cli")},
            "daemon": {"launcher": {"path": "bin/serialwrapd", "sha256": _sha256_bytes(b"daemon")}},
        },
    }), encoding="utf-8")

    # 外掛 daemon（指向 repo 目錄或安裝外）
    outside_daemon = tmp_path / "repo" / "py-serialwrapd"
    outside_daemon.parent.mkdir(parents=True, exist_ok=True)
    outside_daemon.write_bytes(b"outside")
    m_out = dc.measure(cli, daemon_argv=[str(outside_daemon)])
    assert m_out["error"] == "daemon_outside_install"
    assert m_out["bundle_tree_sha256"] is None

    # daemon 內容改寫 (daemon_mismatch)
    v2 = tmp_path / "v2"
    shutil.copytree(v1, v2)
    (v2 / "bin" / "serialwrapd").write_bytes(b"tampered-daemon")
    m_tamper = dc.measure(v2 / "bin" / "serialwrap", daemon_argv=[str(v2 / "bin" / "serialwrapd")])
    assert m_tamper["error"] == "daemon_mismatch"

    # 相符
    m_ok = dc.measure(cli, daemon_argv=[str(daemon)])
    assert m_ok["error"] is None
    assert m_ok["daemon_sha256"] == _sha256_bytes(b"daemon")

    # daemon_argv 為 None
    m_none = dc.measure(cli, daemon_argv=None)
    assert m_none["error"] is None
    assert m_none["daemon_resolved"] is None


@pytest.mark.memory_only
def test_missing_daemon_file_without_manifest_sha_is_mismatch(tmp_path: pathlib.Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """驗證 manifest 沒有有效 daemon hash 時，不會讓不存在的 daemon 誤判相符。"""
    import realhw.deployed_content as dc
    _pin_posix_layout(monkeypatch)

    install = tmp_path / "install"
    (install / "bin").mkdir(parents=True)
    cli = install / "bin" / "serialwrap"
    cli.write_bytes(b"cli")
    missing_daemon = install / "bin" / "serialwrapd"
    (install / "release-manifest.json").write_text(json.dumps({
        "schema_version": "release-manifest.v1",
        "components": {
            "cli": {"path": "bin/serialwrap", "sha256": _sha256_bytes(b"cli")},
            "daemon": {"launcher": {"path": "bin/serialwrapd"}},
        },
    }), encoding="utf-8")

    result = dc.measure(cli, daemon_argv=[str(missing_daemon)])

    assert result["error"] == "daemon_mismatch"
    assert result["daemon_sha256"] is None


@pytest.mark.memory_only
def test_deployed_content_cli_keeps_live_daemon_outside_schema_object(
    tmp_path: pathlib.Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """驗證 CLI 把 live daemon 寫到獨立 sidecar，deployed-content.v1 不增加未登記欄位。"""
    import realhw.deployed_content as dc

    cli = tmp_path / "serialwrap"
    cli.write_bytes(b"cli")
    measured = {
        "schema_version": "deployed-content.v1",
        "measured_at": "2026-10-19T00:00:00+00:00",
        "cli_configured": str(cli),
        "cli_resolved": str(cli),
        "cli_sha256": None,
        "install_dir": None,
        "release_manifest_sha256": None,
        "bundle_tree_sha256": None,
        "runtime_tree_sha256": None,
        "entry_dir_sha256": None,
        "daemon_configured": None,
        "daemon_resolved": None,
        "daemon_sha256": None,
        "error": "unresolved",
    }
    live = {"pid": 123, "image_path": "/fake/daemon", "image_sha256": None, "in_install": False, "error": None}
    monkeypatch.setattr(dc, "measure", lambda *args, **kwargs: dict(measured))
    monkeypatch.setattr(dc, "live_daemon", lambda *args, **kwargs: dict(live))
    output = tmp_path / "measure.json"

    assert dc.main(["--cli", str(cli), "--daemon-pid", "123", "--out", str(output)]) == 0

    assert "live_daemon" not in json.loads(output.read_text(encoding="utf-8"))
    sidecar = output.with_name(output.name + ".live-daemon.json")
    assert json.loads(sidecar.read_text(encoding="utf-8")) == live


@pytest.mark.process_rpc
def test_deployed_content_live_daemon_image(tmp_path: pathlib.Path) -> None:
    """驗證 live_daemon 取得執行中 daemon 行程之影像路徑與 sha256，並檢查是否在安裝目錄下。"""
    import realhw.deployed_content as dc

    proc = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(30)"])
    try:
        live = dc.live_daemon(proc.pid, install=tmp_path)
        assert live["image_path"] == str(pathlib.Path(sys.executable).resolve())
        assert live["in_install"] is False
        assert live["error"] is None

        # 不存在的 pid
        live_dead = dc.live_daemon(99999999, install=tmp_path)
        assert live_dead["error"] == "image_unavailable"
    finally:
        proc.terminate()
        proc.wait(timeout=5.0)


@pytest.mark.memory_only
def test_deployed_content_windows_relative_dir_resolves_from_install_root(
    tmp_path: pathlib.Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Windows dev bundle 的 current-release.json `dir` 為相對安裝根的 `versions/<V>`（OD-18 (3)，正斜線為新寫入形式）。

    讀取端須同時接受舊版反斜線形式 `versions\\<V>`（既有 sidecar 相容），兩種形式皆須解析到同一安裝根。
    回歸：若以 bin\\（cli.parent）為基準解析，會得到 bin\\versions\\<V>（不存在）而 install_dir 為 None（unresolved）。
    POSIX 行為不變：本測試只在 os.name 偽裝為 nt 時走 sidecar 分支。
    """
    import realhw.deployed_content as dc
    monkeypatch.setattr(os, "name", "nt")

    for form in ("versions\\0.4.0", "versions/0.4.0"):
        root = tmp_path / ("root-" + ("bs" if "\\" in form else "fs"))
        bin_dir = root / "bin"
        bin_dir.mkdir(parents=True, exist_ok=True)
        ver_dir = root / "versions" / "0.4.0"
        (ver_dir / "bin").mkdir(parents=True, exist_ok=True)
        cli_bytes = b"sw-cli-relative"

        (bin_dir / "serialwrap.exe").write_bytes(cli_bytes)
        (bin_dir / "current-release.json").write_text(
            json.dumps({"bundle_version": "0.4.0", "dir": form}), encoding="utf-8"
        )
        (ver_dir / "bin" / "serialwrap.exe").write_bytes(cli_bytes)
        (ver_dir / "release-manifest.json").write_text(json.dumps({
            "schema_version": "release-manifest.v1",
            "components": {"cli": {"path": "bin/serialwrap.exe", "sha256": _sha256_bytes(cli_bytes)}},
        }), encoding="utf-8")

        m = dc.measure(bin_dir / "serialwrap.exe")
        assert m["install_dir"] == str(ver_dir.resolve()), form
        assert m["error"] is None, form
        assert len(m["bundle_tree_sha256"]) == 64, form


@pytest.mark.memory_only
def test_deployed_content_windows_stable_bin_sidecar(tmp_path: pathlib.Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """驗證 Windows 佈局下穩定 bin 目錄中旁載檔與複本不一致會被正確檢測。"""
    import realhw.deployed_content as dc
    monkeypatch.setattr(os, "name", "nt")

    root = tmp_path / "root"
    bin_dir = root / "bin"
    bin_dir.mkdir(parents=True, exist_ok=True)
    ver_dir = root / "versions" / "0.4.0"
    (ver_dir / "bin").mkdir(parents=True, exist_ok=True)

    cli_bytes = b"sw-cli"
    daemon_bytes = b"sw-daemon"

    (bin_dir / "serialwrap.exe").write_bytes(cli_bytes)
    (bin_dir / "serialwrapd.exe").write_bytes(daemon_bytes)
    (bin_dir / "current-release.json").write_text(json.dumps({"dir": str(ver_dir)}), encoding="utf-8")

    (ver_dir / "bin" / "serialwrap.exe").write_bytes(cli_bytes)
    (ver_dir / "bin" / "serialwrapd.exe").write_bytes(daemon_bytes)
    (ver_dir / "release-manifest.json").write_text(json.dumps({
        "schema_version": "release-manifest.v1",
        "components": {
            "cli": {"path": "bin/serialwrap.exe", "sha256": _sha256_bytes(cli_bytes)},
            "daemon": {"launcher": {"path": "bin/serialwrapd.exe", "sha256": _sha256_bytes(daemon_bytes)}},
        },
    }), encoding="utf-8")

    m = dc.measure(bin_dir / "serialwrap.exe")
    assert m["error"] is None
    assert len(m["entry_dir_sha256"]) == 64

    # 加旁載檔 version.dll -> error == "entry_dir_extra: version.dll"
    (bin_dir / "version.dll").write_bytes(b"sidecar")
    m_extra = dc.measure(bin_dir / "serialwrap.exe")
    assert m_extra["error"] == "entry_dir_extra: version.dll"

    (bin_dir / "version.dll").unlink()

    # 改寫 serialwrapd.exe 一個 byte -> "entry_copy_mismatch: serialwrapd.exe"
    (bin_dir / "serialwrapd.exe").write_bytes(b"tampered")
    m_mismatch = dc.measure(bin_dir / "serialwrap.exe")
    assert m_mismatch["error"] == "entry_copy_mismatch: serialwrapd.exe"
