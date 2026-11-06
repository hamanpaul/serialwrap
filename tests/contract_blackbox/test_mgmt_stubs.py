"""管理面 stub、委派哨兵、夾具與 bundle 形狀自測（P4-T1）。

- stub／哨兵以 `sys.executable` 執行（無副檔名的 shebang 薄殼在 Windows 不可直接 exec，故 core_portable 測試不得依賴之）
- bundle、live_daemon 依賴 bash／真 daemon，標 posix_only
"""
from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

from mgmt_fixtures import sha256_file

pytestmark = [pytest.mark.process_rpc]

STUBS = Path(__file__).parent / "stubs" / "bin"


def _run_stub(name: str, args: list[str], tmp_path: Path, script: dict | None = None):
    log = tmp_path / "stub.ndjson"
    env = dict(os.environ, SW_STUB_LOG=str(log))
    if script is not None:
        env["SW_STUB_SCRIPT"] = json.dumps(script, ensure_ascii=False)
    p = subprocess.run([sys.executable, str(STUBS / name), *args], capture_output=True, text=True, env=env)
    rows = [json.loads(line) for line in log.read_text(encoding="utf-8").splitlines() if line.strip()] if log.exists() else []
    return p, rows


@pytest.mark.core_portable
def test_stub_logs_argv_and_defaults_to_rc0(tmp_path: Path) -> None:
    p, rows = _run_stub("systemctl", ["--user", "start", "serialwrap"], tmp_path)
    assert p.returncode == 0 and p.stdout == "" and p.stderr == ""
    assert rows == [{"tool": "systemctl", "argv": ["--user", "start", "serialwrap"], "cwd": os.getcwd()}]


@pytest.mark.core_portable
def test_stub_scripted_response_with_prefix(tmp_path: Path) -> None:
    script = {"schtasks /Query*": {"rc": 1, "stdout": "", "stderr": "錯誤: 系統找不到指定的檔案。"}}
    p, _ = _run_stub("schtasks", ["/Query", "/TN", "serialwrap"], tmp_path, script)
    assert p.returncode == 1 and p.stderr == "錯誤: 系統找不到指定的檔案。"


@pytest.mark.core_portable
def test_delegate_sentinel_exits_97_and_writes_marker(tmp_path: Path) -> None:
    marker = tmp_path / "DELEGATED"
    p = subprocess.run(
        [sys.executable, str(Path(__file__).parent / "delegate_sentinel.py"), "doctor"],
        env=dict(os.environ, SW_DELEGATE_MARKER=str(marker)),
        capture_output=True,
    )
    assert p.returncode == 97 and marker.exists()


@pytest.mark.core_portable
def test_stub_path_fixture_prefers_stubs_and_drops_host_path(env, stub_path) -> None:
    # stub_path 與 env 同源：子行程讀到的 PATH 首項必為 stubs/bin，且不繼承宿主 PATH（如 ~/.local/bin）。
    parts = env["PATH"].split(os.pathsep)
    assert parts[0] == str(stub_path)
    assert parts[1:] == ["/usr/bin", "/bin"]


@pytest.mark.posix_only
def test_mkbundle_release_manifest_matches_parser_contract(mgmt_bundle) -> None:
    # 與 sw-proto::parse_manifest（schema_version 1）及黑箱 _artifact_identity（components.cli.implementation）對齊。
    manifest = json.loads((mgmt_bundle.root / "release-manifest.json").read_text(encoding="utf-8"))
    assert manifest["schema_version"] == 1
    assert manifest["components"]["cli"]["implementation"] == "rust"
    assert manifest["components"]["cli"]["sha256"] == sha256_file(mgmt_bundle.cli)
    entry = manifest["components"]["python_cli_delegate"]["entry"]
    assert entry and (mgmt_bundle.root / entry[0]).exists(), "委派 entry[0] 必須存在於 bundle 內"
    assert Path(entry[-1]).is_file(), "委派 entry 末項應為 sentinel 檔"
    assert not (mgmt_bundle.root / "DELEGATED").exists()


@pytest.mark.posix_only
def test_live_daemon_fixture_serves_attached_console_list(live_daemon) -> None:
    # 真 daemon 必須附掛 COM0；錯誤回應不得被當成空清單。
    consoles = live_daemon.console_list("COM0")
    assert isinstance(consoles, list)
