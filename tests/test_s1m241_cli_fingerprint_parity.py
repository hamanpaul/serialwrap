"""S1M-241 ITEM 2：setup 以既有 ping 和已安裝 manifest 輸出 daemon 程式碼指紋。"""
from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import time
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[1]
MKBUNDLE = REPO / "scripts" / "rust" / "mkbundle.sh"
VERSION = (REPO / "VERSION").read_text(encoding="utf-8").strip()

pytestmark = [pytest.mark.posix_only, pytest.mark.process_rpc]


def _require_cargo() -> None:
    if shutil.which("cargo") is None:
        if os.environ.get("SW_REQUIRE_CARGO") == "1":
            pytest.fail("SW_REQUIRE_CARGO=1 但找不到 cargo（證據不足，非 PASS）")
        pytest.skip("cargo 不存在（證據不足，非 PASS）")


class _LazyBundle:
    """第一次使用時建立一份共用的完整 dev bundle。"""

    def __init__(self, root: Path) -> None:
        self._root = root
        self._out: Path | None = None

    def out(self) -> Path:
        _require_cargo()
        if self._out is None:
            out = self._root / "dev bundle"
            args = [str(MKBUNDLE), "--dev", "--out", str(out)]
            result = subprocess.run(
                args,
                capture_output=True,
                text=True,
                timeout=900,
                cwd=str(REPO),
                check=False,
            )
            assert result.returncode == 0, (
                f"mkbundle.sh 建置失敗（rc={result.returncode}）：{result.stderr.strip()}"
            )
            self._out = out
        return self._out


@pytest.fixture(scope="module")
def bundle(tmp_path_factory) -> _LazyBundle:
    return _LazyBundle(tmp_path_factory.mktemp("s1m241-bundle", numbered=False))


def _installed_layout(bundle_root: Path, install_root: Path) -> tuple[Path, Path, str]:
    """在隔離根建立安裝版本、stable entry 與 current-release.json。"""
    version_source = bundle_root / "versions" / VERSION
    version_dir = install_root / "versions" / VERSION
    shutil.copytree(version_source, version_dir, symlinks=True)

    manifest_path = version_dir / "release-manifest.json"
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    fingerprint = manifest["components"]["daemon"].get("code_fingerprint")
    assert isinstance(fingerprint, str) and fingerprint, "bundle manifest 缺少 daemon.code_fingerprint"

    bin_dir = install_root / "bin"
    bin_dir.mkdir(parents=True)
    for name in ("serialwrap", "serialwrapd"):
        os.symlink(f"../versions/{VERSION}/bin/{name}", bin_dir / name)
    (install_root / "current-release.json").write_text(
        json.dumps({"bundle_version": VERSION, "dir": f"versions/{VERSION}"}),
        encoding="utf-8",
    )
    return bin_dir / "serialwrap", manifest_path, fingerprint


def _isolated_env(
    root: Path,
    install_root: Path,
    manifest_path: Path,
    endpoint: Path,
) -> dict[str, str]:
    """每個子程序都明確收到隔離目錄、endpoint 與 manifest。"""
    env = dict(os.environ)
    for key in tuple(env):
        if key.startswith("SERIALWRAP_"):
            env.pop(key, None)

    paths = {
        "home": root / "home",
        "config": root / "config",
        "state": root / "state",
        "run": root / "run",
        "wal": root / "wal",
        "by-id": root / "by-id",
        "by-path": root / "by-path",
        "blog": root / "blog",
        "events": root / "events.d",
        "events-runtime": root / "events-rt",
        "data": root / "data",
        "system-config": root / "system-config",
    }
    for path in paths.values():
        path.mkdir(parents=True, exist_ok=True)

    env.update(
        {
            "HOME": str(paths["home"]),
            "XDG_CONFIG_HOME": str(root / "xdg-config"),
            "XDG_STATE_HOME": str(root / "xdg-state"),
            "XDG_DATA_HOME": str(root / "xdg-data"),
            "XDG_RUNTIME_DIR": str(root / "xdg-runtime"),
            "SERIALWRAP_CONFIG_DIR": str(paths["config"]),
            "SERIALWRAP_CONFIG": str(paths["config"] / "config.yaml"),
            "SERIALWRAP_STATE_DIR": str(paths["state"]),
            "SERIALWRAP_RUN_DIR": str(paths["run"]),
            "SERIALWRAP_WAL_DIR": str(paths["wal"]),
            "SERIALWRAP_BY_ID_DIR": str(paths["by-id"]),
            "SERIALWRAP_BY_PATH_DIR": str(paths["by-path"]),
            "SERIALWRAP_LOG_DIR": str(paths["blog"]),
            "SERIALWRAP_EVENTS_DIR": str(paths["events"]),
            "SERIALWRAP_EVENTS_RUNTIME_DIR": str(paths["events-runtime"]),
            "SERIALWRAP_DATA_DIR": str(paths["data"]),
            "SERIALWRAP_PROFILE_DIR": str(paths["config"] / "profiles"),
            "SERIALWRAP_TEST_SYSTEM_CONFIG_PATH": str(paths["system-config"] / "config.yaml"),
            "SERIALWRAP_INSTALL_ROOT": str(install_root),
            "SERIALWRAP_RELEASE_MANIFEST": str(manifest_path),
            "SERIALWRAP_ENDPOINT": str(endpoint),
            "SERIALWRAP_TTYMCU_PATH": str(paths["run"] / "dev" / "ttyMCU"),
            "PYTHONDONTWRITEBYTECODE": "1",
        }
    )
    config = Path(env["SERIALWRAP_CONFIG"])
    config.write_text("supervision_mode: on-demand\n", encoding="utf-8")
    return env


def _send_rpc(endpoint: str, method: str) -> dict | None:
    harness_dir = str(REPO / "tests" / "contract_blackbox")
    if harness_dir not in sys.path:
        sys.path.insert(0, harness_dir)
    from bb_daemon import _send_rpc as send

    return send(endpoint, method, {}, timeout_s=1.0)


def _wait_ready(endpoint: str, daemon: subprocess.Popen, timeout_s: float = 30.0) -> dict:
    deadline = time.monotonic() + timeout_s
    while time.monotonic() < deadline:
        if daemon.poll() is not None:
            raise AssertionError(f"隔離 Python daemon 提早結束 rc={daemon.returncode}")
        response = _send_rpc(endpoint, "health.ping")
        if response and response.get("ok") is True:
            return response
        time.sleep(0.2)
    raise AssertionError(f"隔離 Python daemon 未就緒：{endpoint}")


def _isolated_pidns_prefix() -> list[str]:
    """避免 setup 的行程掃描看見宿主 daemon；能力不足時明確記為 SKIP。"""
    harness_dir = str(REPO / "tests" / "contract_blackbox")
    if harness_dir not in sys.path:
        sys.path.insert(0, harness_dir)
    from bb_l15 import unshare_pidns_argv

    prefix = unshare_pidns_argv()
    if prefix is None:
        pytest.skip("無法以非特權 unshare 建立 pid namespace（環境能力不足，非 PASS）")
    return prefix


def test_s1m241_setup_current_manifest_is_reported(bundle, tmp_path: Path) -> None:
    bundle_root = bundle.out()
    install_root = tmp_path / "installed"
    cli, manifest_path, manifest_fingerprint = _installed_layout(bundle_root, install_root)
    work = tmp_path / "isolated"
    run_dir = work / "run"
    run_dir.mkdir(parents=True)
    endpoint = run_dir / "serialwrapd.sock"
    env = _isolated_env(work, install_root, manifest_path, endpoint)
    profile_dir = Path(env["SERIALWRAP_PROFILE_DIR"])
    profile_dir.mkdir(parents=True, exist_ok=True)
    daemon_log = work / "python-daemon.log"

    daemon: subprocess.Popen | None = None
    try:
        with daemon_log.open("w", encoding="utf-8") as log:
            daemon = subprocess.Popen(
                [
                    sys.executable,
                    str(REPO / "serialwrapd.py"),
                    "--socket",
                    str(endpoint),
                    "--lock",
                    str(run_dir / "serialwrapd.lock"),
                    "--profile-dir",
                    str(profile_dir),
                ],
                env=env,
                cwd=str(work),
                stdin=subprocess.DEVNULL,
                stdout=log,
                stderr=subprocess.STDOUT,
            )
        ping = _wait_ready(str(endpoint), daemon)
        assert ping.get("code_fingerprint") == manifest_fingerprint

        # daemon 啟動時會記錄 endpoint；移除該欄位，讓 setup 由明確的隔離 endpoint 預設值解析，
        # 保持測試只觀察 setup 自己的既有 ping 探測。
        Path(env["SERIALWRAP_CONFIG"]).write_text(
            "supervision_mode: on-demand\n", encoding="utf-8"
        )
        result = subprocess.run(
            _isolated_pidns_prefix() + [str(cli), "setup", "--on-demand"],
            env=env,
            cwd=str(work),
            capture_output=True,
            text=True,
            timeout=180,
            check=False,
        )
        assert result.returncode == 0, (
            f"installed Rust CLI setup 失敗（rc={result.returncode}）："
            f"stdout={result.stdout!r}, stderr={result.stderr!r}, daemon={daemon_log.read_text(encoding='utf-8')}"
        )
        lines = [line for line in result.stdout.splitlines() if line.strip()]
        assert len(lines) == 1, f"setup stdout 必須只有一個 JSON：{result.stdout!r}"
        payload = json.loads(lines[0])
        daemon_code = payload["daemon_code"]
        assert daemon_code["installed"] == manifest_fingerprint
        assert daemon_code["daemon"] == manifest_fingerprint
        assert daemon_code["action"] == "none"
        assert "checked" not in daemon_code
        assert result.stderr == ""
    finally:
        if daemon is not None and daemon.poll() is None:
            try:
                _send_rpc(str(endpoint), "daemon.stop")
                daemon.wait(timeout=5.0)
            except (OSError, subprocess.TimeoutExpired):
                daemon.terminate()
                try:
                    daemon.wait(timeout=5.0)
                except subprocess.TimeoutExpired:
                    daemon.kill()
                    daemon.wait(timeout=5.0)
