"""P4-T6 `config.yaml` 寫入、`service`、`supervision-mode` 查詢（DF-07）黑箱契約。

A/B 案例同時以 Python 基線與受測 CLI 執行並比對輸出。
預設執行（Python 基線）時 A/B 案例應通過（Python 對 Python）；
以 SERIALWRAP_CLI_EXE=$SW_BUNDLE/bin/serialwrap SERIALWRAP_AB_LABEL=rust-1b 執行時，
service 群組仍委派 sentinel（exit 97），因此案例應失敗（紅燈：exit 97）。
"""
from __future__ import annotations

import json
import subprocess
from pathlib import Path

import pytest

from bb_harness import (
    cli_prefix,
    isolated_env,
    python_baseline_prefix,
)

pytestmark = [
    pytest.mark.posix_only,
    pytest.mark.process_rpc,
]


def test_service_ab_on_demand(tmp_path: Path, no_delegation: Path) -> None:
    results: dict[str, tuple[int, str, str]] = {}
    for label, prefix in (("python", python_baseline_prefix()), ("cli", cli_prefix())):
        work = tmp_path / label
        work.mkdir()
        env = isolated_env(work)
        env["SW_DELEGATE_MARKER"] = str(no_delegation)
        proc = subprocess.run(
            prefix + ["service", "start"],
            env=env,
            capture_output=True,
            text=True,
            timeout=30,
        )
        results[label] = (proc.returncode, proc.stdout, proc.stderr)

    assert results["cli"][0] == results["python"][0]
    assert results["cli"][0] == 2
    assert results["cli"][1] == results["python"][1]
    assert results["cli"][2] == results["python"][2]

    data = json.loads(results["cli"][1])
    assert data["ok"] is False
    assert data["error_code"] == "NO_SYSTEMD"
    assert data["mode"] == "on-demand"
    assert data["action"] == "start"
    assert "serialwrap: service start failed: NO_SYSTEMD" in results["cli"][2]
