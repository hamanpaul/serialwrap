# tests/test_stage0_captures.py
"""Stage 0 P1 Task T19：凍結值擷取與 S1-W WAL 微基準（TDD 紅燈）。

依據：`docs/rust-refactor/plans/02-stage0-python-bc.md` Task T19 的 A1 失敗測試
（規格 §1.1.2、§1.1.3、§8.1；OD-10）。本檔只含失敗測試，不含產品碼。

- 被測腳本（`scripts/stage0/capture_bc_baseline.py`、`wal_microbench.py`、`wal_shorttest.py`）
  與 `contracts/captures/` 產物皆由 B1 產生；Stage A 下應因腳本不存在（子行程 returncode 2）
  或產物檔不存在（`FileNotFoundError`）紅燈。
- 本檔只做頂層 stdlib import；`jsonschema` 於測試函式內延遲 import。
- 依 P0 T14：整檔 `core_portable`（Linux 與 Windows 皆必跑）＋ `process_rpc`（以子行程執行腳本）。
"""
from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

import pytest

# 模組層統一：平台 marker 為 core_portable；資源 marker 為 process_rpc（_run 以子行程執行腳本）
pytestmark = [pytest.mark.core_portable, pytest.mark.process_rpc]

REPO = Path(__file__).resolve().parents[1]


def _run(*args):
    return subprocess.run([sys.executable, *args], cwd=REPO, capture_output=True, text=True, timeout=300)


def test_capture_is_reproducible_and_matches_checked_in(tmp_path):
    out = tmp_path / "cap.json"
    r = _run("scripts/stage0/capture_bc_baseline.py", "--out", str(out))
    assert r.returncode == 0, r.stderr
    got = json.loads(out.read_text()); want = json.loads((REPO / "contracts/captures/stage0-python-bc.json").read_text())
    got.pop("git_revision"); want.pop("git_revision")
    assert got == want


def test_capture_contains_required_frozen_values(tmp_path):
    want = json.loads((REPO / "contracts/captures/stage0-python-bc.json").read_text())
    import jsonschema
    jsonschema.validate(want, json.loads((REPO / "contracts/captures/stage0-python-bc.schema.json").read_text()))
    assert want["wal"]["rotate_bytes"] == 67108864 and want["wal"]["rotate_compare"] == ">="
    assert want["wal"]["rotate_check"] == "before_write"
    assert want["wal"]["crc_empty"] == "00000000"
    assert want["wal"]["seq_after_reset"] == 1 and want["wal"]["seq_after_rotate"] == 1
    assert {m["expected"] for m in want["bc03_mapping"]} == {
        "COMMAND_ABORTED_IN_FLIGHT", "SEND_FAILED", "FLUSHED_BY_RECOVERY"}
    assert want["release_drain_timeout_shape"]["error_code"] == "SESSION_RECOVERING"


def test_microbench_smoke_crosses_rotate(tmp_path):
    out = tmp_path / "bench.json"
    r = _run("scripts/stage0/wal_microbench.py", "--streams", "2", "--records", "300", "--payload", "1024",
             "--rotate-bytes", "262144", "--prefill-ratio", "0.9375", "--out", str(out), "--wal-root", str(tmp_path / "w"))
    assert r.returncode == 0, r.stderr
    d = json.loads(out.read_text())
    assert d["total_records"] == 600
    assert d["ordinary_samples"] + d["rotate_samples"] == 600       # rotate 當筆不計入 append_ns（§1.2）
    assert d["append_ns"]["n"] == d["ordinary_samples"] and d["rotate_samples"] == len(d["rotate_pauses_ns"])
    assert all(n >= 1 for n in d["rotations_per_stream"])
    for k in ("os", "python", "virtualization", "memory_bytes", "storage_device", "power", "endpoint_av",
              "artifacts", "contract_tree", "tool_revision", "timing_method", "serial"):
        assert k in d["env"] and d["env"][k] not in (0, "", None)  # 查不到寫 "unknown"，不得填零
    assert d["rotate_max_ns"] == max(d["rotate_pauses_ns"])
    assert d["append_ns"]["p50"] <= d["append_ns"]["p95"] <= d["append_ns"]["p99"]


def test_microbench_prefill_is_parseable_wal(tmp_path):
    out = tmp_path / "bench.json"
    r = _run("scripts/stage0/wal_microbench.py", "--streams", "1", "--records", "10", "--payload", "64",
             "--rotate-bytes", "65536", "--prefill-ratio", "0.9375", "--out", str(out), "--wal-root", str(tmp_path / "w"))
    assert r.returncode == 0, r.stderr
    files = list((tmp_path / "w").rglob("raw.wal.ndjson*"))
    assert files, "預填後必須存在 WAL 檔"
    for p in files:
        for line in p.read_text(encoding="utf-8").splitlines():
            assert isinstance(json.loads(line)["seq"], int)   # 預填不得用無效 padding（§1.1.3）


def test_shorttest_smoke(tmp_path):
    out = tmp_path / "short.json"
    r = _run("scripts/stage0/wal_shorttest.py", "--duration-s", "2", "--rate", "10240", "--streams", "2",
             "--rotate-bytes", "16384", "--out", str(out), "--wal-root", str(tmp_path / "w"))
    assert r.returncode == 0, r.stderr
    d = json.loads(out.read_text())
    assert d["write_failures"] == 0 and d["loss_records"] == 0 and d["seq_gaps"] == 0
