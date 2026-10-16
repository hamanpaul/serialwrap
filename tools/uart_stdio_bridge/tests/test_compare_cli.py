from __future__ import annotations

import json
import os
import subprocess
import sys

import pytest

from evidence_builder import EvidenceSet, SOURCE_ROOT


pytestmark = pytest.mark.core_portable


def _run_compare(ev: EvidenceSet, output_path, *, extra_args=(), extra_env=None):
    env = os.environ.copy()
    existing = env.get("PYTHONPATH")
    env["PYTHONPATH"] = os.pathsep.join(
        item for item in (str(SOURCE_ROOT), existing) if item
    )
    if extra_env:
        env.update(extra_env)
    command = [
        sys.executable,
        "-m",
        "uart_stdio_bridge",
        "compare",
        "--evidence-dir",
        str(ev.evidence_dir),
        "--wal-dir",
        str(ev.wal_dir),
        "--port-map",
        "COM0=COM0",
        *extra_args,
        "--out",
        str(output_path),
    ]
    return subprocess.run(command, capture_output=True, text=True, env=env, timeout=15)


@pytest.mark.process_rpc
def test_cli_exit_codes(tmp_path):
    passing = EvidenceSet.clean(tmp_path / "pass")
    pass_path = tmp_path / "pass.json"
    pass_result = _run_compare(passing, pass_path)
    assert pass_result.returncode == 0
    pass_obj = json.loads(pass_path.read_text(encoding="utf-8"))
    assert pass_obj["verdict"] == "PASS"
    assert pass_path.read_text(encoding="utf-8") == json.dumps(
        pass_obj, ensure_ascii=False, indent=2, sort_keys=True
    ) + "\n"

    failing = EvidenceSet.build(
        tmp_path / "fail",
        tx_chunks_wire=[b"left\n"],
        tx_chunks_wal=[b"right\n"],
    )
    fail_result = _run_compare(failing, tmp_path / "fail.json")
    assert fail_result.returncode == 1

    insufficient = EvidenceSet.clean(tmp_path / "insufficient")
    insufficient.apply_tamper("flip_payload_keep_crc")
    insufficient_result = _run_compare(insufficient, tmp_path / "insufficient.json")
    assert insufficient_result.returncode == 4

    secret = EvidenceSet.build(
        tmp_path / "secret",
        tx_chunks_wire=[b"command\n", b"s3cr3t\n"],
        tx_chunks_wal=[b"command\n"],
    )
    secret_path = tmp_path / "secret.json"
    secret_result = _run_compare(
        secret,
        secret_path,
        extra_args=("--rule", "secret_tx_unlogged", "--secret-env", "SW_COMPARE_TEST_SECRET"),
        extra_env={"SW_COMPARE_TEST_SECRET": "s3cr3t"},
    )
    assert secret_result.returncode == 0
    secret_obj = json.loads(secret_path.read_text(encoding="utf-8"))
    tx_stream = next(stream for stream in secret_obj["streams"] if stream["dir"] == "TX")
    assert len(tx_stream["rules_applied"]) == 1
