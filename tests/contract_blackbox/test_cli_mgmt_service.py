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


@pytest.mark.parametrize(
    ("config_text", "stderr_exact"),
    [
        # bool／None key：json.dumps 轉為 "true"／"false"／"null"（sort_keys 依原始 key）。
        ("supervision_mode: {true: 1, false: 2}\n", True),
        ("supervision_mode: {null: 1}\n", True),
        # 不可序列化（date）與 sort_keys 無法比較的 key：CLI 邊界輸出 INTERNAL_ERROR JSON，stderr 保留錯誤末行。
        ("supervision_mode: 2020-01-01\n", False),
        ("supervision_mode: {1: a, b: c}\n", False),
    ],
)
def test_service_ab_non_string_mode(
    tmp_path: Path, no_delegation: Path, config_text: str, stderr_exact: bool
) -> None:
    results: dict[str, tuple[int, str, str]] = {}
    for label, prefix in (("python", python_baseline_prefix()), ("cli", cli_prefix())):
        work = tmp_path / label
        work.mkdir()
        env = isolated_env(work)
        env["SW_DELEGATE_MARKER"] = str(no_delegation)
        cfg = Path(env["SERIALWRAP_CONFIG_DIR"]) / "config.yaml"
        cfg.write_text(config_text, encoding="utf-8")
        proc = subprocess.run(
            prefix + ["service", "start"],
            env=env,
            capture_output=True,
            text=True,
            timeout=30,
        )
        results[label] = (proc.returncode, proc.stdout, proc.stderr)

    assert results["cli"][0] == results["python"][0]
    assert results["cli"][1] == results["python"][1]
    if stderr_exact:
        assert results["cli"][2] == results["python"][2]
    else:
        py_last = results["python"][2].strip().splitlines()[-1]
        cli_last = results["cli"][2].strip().splitlines()[-1]
        assert cli_last == py_last
