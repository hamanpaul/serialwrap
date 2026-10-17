"""P4 管理面黑箱測試 fixtures 與輔助工具。

依據 P4-T1 規範提供：
- stub_path：`env["PATH"]` 前置 stubs/bin，並保留 `/usr/bin:/bin`（直接改 `env` 夾具的 dict，與被測行程同源）
- stub_log：讀取 `env["SW_STUB_LOG"]` 的 ndjson 記錄
- no_delegation：測後斷言 `env["SW_DELEGATE_MARKER"]` 不存在
- mgmt_bundle：`mkbundle.sh --dev --delegate-sentinel --print-dir` 產物；測後斷言 `<bundle>/DELEGATED` 不存在
- run_wrapper：以 bash 執行物化 wrapper（逾時 30 s）
- live_daemon：以 FakeTarget＋PyDaemon 在隔離 env 啟動真 Python daemon（passthrough 目標，已附掛 COM0）

所有夾具都依賴 `env`（與 `bb_harness.isolated_env` 同源），因此參數順序不影響隔離。
"""
from __future__ import annotations

import hashlib
import json
import os
import subprocess
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, Iterator

import pytest

from bb_harness import repo_root

STUBS_BIN = Path(__file__).resolve().parent / "stubs" / "bin"
LIVE_PROFILE_YAML = """profiles:
  live-passthrough:
    platform: passthrough
targets:
  - com: COM0
    profile: live-passthrough
    device_by_id: usb-FAKE_TARGET_0-if00
"""


@dataclass
class MgmtBundle:
    cli: Path
    root: Path
    materialized_bin: Path


@pytest.fixture
def stub_path(env: dict[str, str]) -> Path:
    """`env["PATH"]` 前置 stubs/bin，之後接 `/usr/bin` 與 `/bin`（不繼承宿主 PATH，避免碰到真實 systemctl／sudo）。"""
    env["PATH"] = os.pathsep.join([str(STUBS_BIN), "/usr/bin", "/bin"])
    return STUBS_BIN


@pytest.fixture
def stub_log(env: dict[str, str], tmp_path: Path) -> Callable[[], list[dict[str, Any]]]:
    """設定 `env["SW_STUB_LOG"]`，回傳讀取器。"""
    log_file = tmp_path / "stub.ndjson"
    env["SW_STUB_LOG"] = str(log_file)

    def _reader() -> list[dict[str, Any]]:
        if not log_file.exists():
            return []
        return [json.loads(line) for line in log_file.read_text(encoding="utf-8").splitlines() if line.strip()]

    return _reader


@pytest.fixture
def no_delegation(env: dict[str, str], tmp_path: Path) -> Iterator[Path]:
    """測後斷言未發生非預期的 Python 委派（`env["SW_DELEGATE_MARKER"]` 不存在）。"""
    marker = tmp_path / "DELEGATED"
    env["SW_DELEGATE_MARKER"] = str(marker)
    yield marker
    assert not marker.exists(), f"偵測到非預期的委派呼叫：marker 存在於 {marker}"


@pytest.fixture
def mgmt_bundle(env: dict[str, str], tmp_path: Path) -> Iterator[MgmtBundle]:
    """以 `mkbundle.sh --dev --delegate-sentinel --print-dir` 物化測試 bundle（P4-T1）。

    `env["SERIALWRAP_RELEASE_MANIFEST"]` 指向該 bundle 的 `release-manifest.json`，sentinel marker 位於 bundle 根。
    """
    script = repo_root() / "scripts" / "rust" / "mkbundle.sh"
    out = tmp_path / "bundle"
    res = subprocess.run(
        ["bash", str(script), "--dev", "--delegate-sentinel", "--out", str(out), "--print-dir"],
        capture_output=True,
        text=True,
        timeout=900,
        check=False,
    )
    assert res.returncode == 0, res.stderr
    bundle_dir = Path(res.stdout.strip().splitlines()[-1])
    manifest = bundle_dir / "release-manifest.json"
    env["SERIALWRAP_RELEASE_MANIFEST"] = str(manifest)
    env["SW_DELEGATE_MARKER"] = str(bundle_dir / "DELEGATED")
    bundle = MgmtBundle(
        cli=bundle_dir / "bin" / ("serialwrap.exe" if os.name == "nt" else "serialwrap"),
        root=bundle_dir,
        materialized_bin=bundle_dir / "bin",
    )
    yield bundle
    marker = bundle_dir / "DELEGATED"
    assert not marker.exists(), f"偵測到漏網委派：{marker} 存在"


def run_wrapper(path: Path | str, args: list[str], env: dict[str, str] | None = None) -> subprocess.CompletedProcess[str]:
    """以 bash 執行物化 wrapper，逾時 30 s。"""
    cmd = ["bash", str(path), *args]
    return subprocess.run(cmd, capture_output=True, text=True, timeout=30, env=env)


class LiveDaemon:
    """`live_daemon` 夾具的句柄：`endpoint` 為 unix socket 路徑，`console_list` 只回傳受測 daemon 的成功回應。"""

    def __init__(self, endpoint: str) -> None:
        self.endpoint = endpoint

    def rpc(self, method: str, params: dict[str, Any]) -> dict[str, Any]:
        from bb_daemon import _send_rpc

        resp = _send_rpc(self.endpoint, method, params, timeout_s=5.0)
        assert resp is not None, f"{method} 無回應（endpoint={self.endpoint}）"
        return resp

    def console_list(self, com: str) -> list[dict[str, Any]]:
        # RPC 回應為平面物件（{"ok":true,"consoles":[...],"session":{...}}），非 {"result":...} 包裝；錯誤回應直接拒絕。
        resp = self.rpc("session.console_list", {"selector": com})
        assert resp.get("ok") is True, f"session.console_list 失敗：{resp}"
        consoles = resp.get("consoles")
        assert isinstance(consoles, list), f"consoles 欄位缺失：{resp}"
        return consoles


@pytest.fixture
def live_daemon(tmp_path: Path, env: dict[str, str]) -> Iterator[LiveDaemon]:
    """以 FakeTarget（先）＋PyDaemon（後）在隔離 env 啟動真 Python daemon，COM0 附掛於 passthrough 目標。

    teardown 以 try/finally 先停 daemon 再停目標。
    """
    import time

    from bb_daemon import PyDaemon
    from bb_faketarget import FakeTarget

    target = FakeTarget(tmp_path / "fake-target")
    daemon: PyDaemon | None = None
    try:
        target.start(Path(env["SERIALWRAP_BY_ID_DIR"]))
        profile_dir = tmp_path / "live-profiles"
        profile_dir.mkdir(parents=True, exist_ok=True)
        (profile_dir / "live.yaml").write_text(LIVE_PROFILE_YAML, encoding="utf-8")
        sock = str(tmp_path / "live.sock")
        daemon = PyDaemon(env, profile_dir=profile_dir, socket=sock, lock=tmp_path / "live.lock")
        daemon.start()
        handle = LiveDaemon(sock)
        # 等待 COM0 附掛完成（passthrough 停在 ATTACHED，console_list 即可用）。
        deadline = time.monotonic() + 15.0
        while True:
            resp = handle.rpc("session.console_list", {"selector": "COM0"})
            if resp.get("ok") is True:
                break
            assert time.monotonic() < deadline, f"COM0 未附掛：{resp}"
            time.sleep(0.2)
        yield handle
    finally:
        if daemon is not None:
            daemon.stop()
        target.stop()


def sha256_file(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()
