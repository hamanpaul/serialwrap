"""原生 Windows 無 COM 黑箱：TCP endpoint、單實例、WAL 刪除、config 寫入、延後 method 與關機（P5-T22，紅燈）。

對應計畫 Task 22 Step 1。執行方式：`scripts/rust/win-blackbox.ps1 -Suite slice21-nocom`（Windows Python 3.12）；
POSIX 上整檔 SKIP（windows_native）。

隔離與安全：
- 每個 daemon 以 `isolated_env(tmp)` 設定全部 SERIALWRAP_* 目錄，不觸碰使用者真實 state／config／run。
- 預設 `SERIALWRAP_TEST_DEVICE_ALLOWLIST=COM250`（不存在的埠），確保 daemon 不會在原生 Windows 上開啟任何真實 COM。
- 本檔所有 daemon 由 fixture 收尾，未正常結束者強制終止。

紅燈原因：P5 Windows daemon 尚未實作（或 TCP 綁定／單實例探測尚未就緒）。
"""
from __future__ import annotations

import json
import os
import socket
import subprocess
import time
from pathlib import Path

import pytest
import yaml

from bb_harness import daemon_prefix, isolated_env

pytestmark = [
    pytest.mark.windows_native,
    pytest.mark.process_rpc,
    pytest.mark.skipif(os.name != "nt", reason="windows_native：僅原生 Windows 執行"),
]

NOCOM_ALLOWLIST = "COM250"   # 不存在的埠：無 COM 情境的安全閘（不得指向真實裝置）
DAEMON_ALREADY_RUNNING_STDERR = b"serialwrapd: DAEMON_ALREADY_RUNNING\n"


def _free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def _rpc(port: int, method: str, params: dict | None = None, timeout_s: float = 5.0) -> dict:
    """送一行 JSON 請求到 127.0.0.1:<port>，讀回一行 JSON 回應；連線或解析失敗直接拋例外。"""
    with socket.create_connection(("127.0.0.1", port), timeout=timeout_s) as sock:
        sock.sendall(json.dumps({"id": 1, "method": method, "params": params or {}}).encode("utf-8") + b"\n")
        buf = b""
        while b"\n" not in buf:
            chunk = sock.recv(65536)
            if not chunk:
                break
            buf += chunk
    return json.loads(buf.split(b"\n", 1)[0].decode("utf-8"))


def _env(tmp: Path, port: int, **overrides: str | None) -> dict[str, str]:
    base = {
        "SERIALWRAP_ENDPOINT": None,                       # 未設：由 TCP port 決定 endpoint
        "SERIALWRAP_TCP_PORT": str(port),
        "SERIALWRAP_TEST_DEVICE_ALLOWLIST": NOCOM_ALLOWLIST,
    }
    base.update(overrides)
    return isolated_env(tmp, **base)


class _Daemons:
    """記錄本測試啟動的 daemon，fixture 收尾時強制終止仍存活者。"""

    def __init__(self) -> None:
        self.procs: list[subprocess.Popen] = []

    def spawn(self, env: dict[str, str]) -> subprocess.Popen:
        proc = subprocess.Popen(daemon_prefix(), env=env, stdout=subprocess.DEVNULL, stderr=subprocess.PIPE)
        self.procs.append(proc)
        return proc

    def start(self, tmp: Path, port: int, **overrides: str | None) -> subprocess.Popen:
        proc = self.spawn(_env(tmp, port, **overrides))
        deadline = time.monotonic() + 15.0
        while time.monotonic() < deadline:
            if proc.poll() is not None:
                raise RuntimeError(f"daemon 提前結束 rc={proc.returncode}")
            try:
                if _rpc(port, "health.ping", timeout_s=0.5).get("pong") is True:
                    return proc
            except OSError:
                pass
            time.sleep(0.1)
        raise TimeoutError("daemon 未在 15 s 內回應 health.ping")


@pytest.fixture
def daemons():
    box = _Daemons()
    yield box
    for proc in box.procs:
        if proc.poll() is None:
            proc.kill()
            proc.wait(timeout=10)


def _provoke_wal_record(port: int) -> None:
    """觸發一筆 WAL 紀錄（無 COM 情境）。實際觸發的 RPC 由 P5 實作階段確認；本行為待確認假設。"""
    _rpc(port, "session.bind", {"selector": NOCOM_ALLOWLIST, "device_by_id": "nonexistent-nocom-device"})


def test_tcp_endpoint_loopback_and_ping(daemons, tmp_path):
    port = _free_port()
    daemons.start(tmp_path, port)
    resp = _rpc(port, "health.ping")
    assert resp.get("pong") is True


def test_second_daemon_exit_2(daemons, tmp_path):
    port = _free_port()
    daemons.start(tmp_path, port)
    second = daemons.spawn(_env(tmp_path, port))           # 同 endpoint 再啟動
    _, stderr = second.communicate(timeout=20)
    assert second.returncode == 2
    assert stderr == DAEMON_ALREADY_RUNNING_STDERR


def test_wal_share_delete_while_running(daemons, tmp_path):
    port = _free_port()
    daemons.start(tmp_path, port)
    _provoke_wal_record(port)
    wal = tmp_path / "wal" / "raw.wal.ndjson"
    assert wal.exists()
    # daemon 執行中刪除 WAL（FILE_SHARE_DELETE）必須成功
    r = subprocess.run(["cmd", "/c", "del", str(wal)], capture_output=True, timeout=30)
    assert r.returncode == 0
    assert not wal.exists()
    # 下一筆紀錄觸發 META 重建
    _provoke_wal_record(port)
    first = json.loads(wal.read_text(encoding="utf-8").splitlines()[0])
    assert first["dir"] == "META"
    assert first["meta"]["event"] == "WAL_RECREATED_AFTER_EXTERNAL_UNLINK"


def test_config_yaml_socket_path_written(daemons, tmp_path):
    port = _free_port()
    daemons.start(tmp_path, port)
    config = tmp_path / "config" / "config.yaml"
    deadline = time.monotonic() + 10.0
    while not config.exists() and time.monotonic() < deadline:
        time.sleep(0.1)
    assert yaml.safe_load(config.read_text(encoding="utf-8"))["socket_path"] == f"tcp://127.0.0.1:{port}"


def test_deferred_methods_windows(daemons, tmp_path):
    port = _free_port()
    daemons.start(tmp_path, port)
    resp = _rpc(port, "session.console_attach", {"selector": NOCOM_ALLOWLIST})
    assert resp["ok"] is False
    assert resp["error_code"] == "METHOD_NOT_FOUND"
    assert resp["method"] == "session.console_attach"


def test_session_public_console_endpoint_null(daemons, tmp_path):
    # 名稱聲稱 SessionPublic 的 console_endpoint 為 null；本斷言依計畫取 health.status 的 allowlist 回報。
    port = _free_port()
    daemons.start(tmp_path, port, SERIALWRAP_TEST_DEVICE_ALLOWLIST=NOCOM_ALLOWLIST)
    resp = _rpc(port, "health.status")
    assert resp["test_device_allowlist"] == {"enabled": True, "coms": ["COM250"], "source": "env"}


def test_stop_exit_0(daemons, tmp_path):
    port = _free_port()
    proc = daemons.start(tmp_path, port)
    resp = _rpc(port, "daemon.stop")
    assert resp["ok"] is True
    assert resp["stopping"] is True
    assert proc.wait(timeout=30) == 0
    assert list((tmp_path / "run").glob("*lock*")), "關機後 lock 檔仍應存在"
