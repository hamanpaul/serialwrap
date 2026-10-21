# tests/test_stage3_fakes.py
"""P9-T2 共用基礎測試替身（tests/stage3_fakes.py）的紅燈測試（TDD 第一輪）。

依 10-stage3-canary.md Task 2：替身模組與 tools.stage3（Task 1）尚未實作前，本檔一律於測試函式內以
`_load()`／`_fakes()` 延遲 import，使 `python3 -m pytest -q --collect-only tests/` 在任何階段都能完成收集。
本檔不實作任何 production 程式；紅燈原因應為模組或符號不存在（ImportError），而非語法或 fixture 錯誤。

marker：依計畫 Task 2 為模組層 `pytestmark = [core_portable, memory_only]`。
"""

from __future__ import annotations

import importlib
import json
import subprocess
from datetime import datetime
from pathlib import Path
from typing import Any

import pytest


# 資源 marker 逐項標註：test_stub_cli_logs_calls 會以 subprocess 執行 stub CLI（process_rpc），其餘為 memory_only。
pytestmark = [pytest.mark.core_portable]

REPO_ROOT = Path(__file__).resolve().parents[1]
ACCEPTANCE_DIR = REPO_ROOT / "tests" / "acceptance"


def _load(module_name: str, *names: str) -> list[Any]:
    """延遲 import 模組並取出名稱；相依模組或名稱尚未實作時於測試函式內拋出 ImportError。"""
    mod = importlib.import_module(module_name)
    out: list[Any] = []
    for name in names:
        if not hasattr(mod, name):
            raise ImportError(f"cannot import name {name!r} from {module_name!r}")
        out.append(getattr(mod, name))
    return out


def _fakes():
    """延遲載入 tests/stage3_fakes.py（尚未實作時拋出 ModuleNotFoundError）。"""
    return importlib.import_module("stage3_fakes")


@pytest.mark.memory_only
def test_wal_record_fields():
    fakes = _fakes()
    assert set(fakes.wal_record(1)) == {
        "cmd_id",
        "com",
        "crc32",
        "dir",
        "len",
        "loss_flag",
        "meta",
        "mono_ts_ns",
        "payload_b64",
        "seq",
        "source",
        "wall_ts",
    }
    assert fakes.wal_record(1, payload=b"abc")["len"] == 3


@pytest.mark.memory_only
def test_write_wal_no_trailing_newline(tmp_path):
    fakes = _fakes()
    path = tmp_path / "raw.wal.ndjson"
    fakes.write_wal(path, [fakes.wal_record(1), fakes.wal_record(2)], trailing_newline=False)
    data = path.read_bytes()
    assert data, "寫出的 WAL 檔不應為空"
    assert not data.endswith(b"\n")


@pytest.mark.memory_only
def test_make_bundle_hashes_real(tmp_path):
    fakes = _fakes()

    rust = fakes.make_bundle(tmp_path / "rust")
    rust_manifest = json.loads((rust / "release-manifest.json").read_text(encoding="utf-8"))
    rust_daemon = rust_manifest["components"]["daemon"]
    assert rust_daemon["launcher"]["sha256"] == fakes.sha256_file(rust / "bin" / "serialwrapd")
    assert "path" not in rust_daemon

    py = fakes.make_bundle(tmp_path / "py", daemon_impl="python")
    py_manifest = json.loads((py / "release-manifest.json").read_text(encoding="utf-8"))
    assert py_manifest["components"]["daemon"]["package"]["tree_sha256"] == fakes.tree_sha256(py / "py")


@pytest.mark.memory_only
def test_make_bundle_stage3_marker(tmp_path):
    fakes = _fakes()

    s3 = fakes.make_bundle(tmp_path / "s3", stage="3")
    m3 = json.loads((s3 / "release-manifest.json").read_text(encoding="utf-8"))
    assert m3["retirement"] == {"python_daemon": "removed"}
    assert "python_cli_delegate" not in m3["components"]

    s24 = fakes.make_bundle(tmp_path / "s24", stage="2.4")
    m24 = json.loads((s24 / "release-manifest.json").read_text(encoding="utf-8"))
    assert "retirement" not in m24


@pytest.mark.memory_only
def test_write_ab_manifest_validates_schema(tmp_path):
    import jsonschema

    fakes = _fakes()
    schema = json.loads((ACCEPTANCE_DIR / "ab_manifest.schema.json").read_text(encoding="utf-8"))
    validator = jsonschema.Draft202012Validator(schema)
    for layer in ("l1.5", "l2", "l3"):
        path = fakes.write_ab_manifest(tmp_path / layer.replace(".", "_"), layer=layer, platform="linux")
        doc = json.loads(path.read_text(encoding="utf-8"))
        errors = [e.message for e in validator.iter_errors(doc)]
        assert errors == [], f"{layer}: {errors}"


@pytest.mark.memory_only
def test_fake_probe_sequences():
    (FakeProbe,) = _load("stage3_fakes", "FakeProbe")
    probe = FakeProbe(pid_alive={7: [True, False]})
    assert [probe.pid_alive(7) for _ in range(3)] == [True, False, False]


@pytest.mark.memory_only
def test_recording_runner_kind_and_side():
    (RecordingRunner,) = _load("stage3_fakes", "RecordingRunner")
    runner = RecordingRunner()

    runner(["/x/serialwrap", "daemon", "status"], env={"SW_STAGE3_SIDE": "A"})
    call = runner.calls[-1]
    assert call["kind"] == "cli"
    assert call["sub"] == "daemon status"
    assert call["side"] == "A"

    runner(["python3", "-m", "realhw", "--tier", "longrun", "--report-dir", "/r"])
    assert runner.calls[-1]["kind"] == "realhw"
    assert runner.calls[-1]["sub"] == ""

    runner(["python3", "func-test/runner.py", "--mode", "bridge"])
    assert runner.calls[-1]["kind"] == "functest"


@pytest.mark.memory_only
def test_recording_runner_on_effect():
    (RecordingRunner,) = _load("stage3_fakes", "RecordingRunner")
    runner = RecordingRunner(script={"functest:": [{"rc": 3, "stdout": "ok", "stderr": ""}]})
    seen: list[tuple[list[str], dict]] = []
    runner.on("functest:", lambda argv, env: seen.append((list(argv), dict(env))))

    argv = ["python3", "func-test/runner.py", "--mode", "bridge"]
    result = runner(argv, env={"K": "V"})

    assert seen == [(argv, {"K": "V"})]
    assert result.rc == 3
    assert result.stdout == "ok"


@pytest.mark.process_rpc
def test_stub_cli_logs_calls(tmp_path):
    fakes = _fakes()
    stub_dir = tmp_path / "stub"
    stub = fakes.make_stub_cli(
        stub_dir,
        {
            "session list": [
                {"rc": 0, "stdout": "first\n", "stderr": ""},
                {"rc": 0, "stdout": "second\n", "stderr": ""},
            ]
        },
    )

    first = subprocess.run([str(stub), "session", "list"], capture_output=True, text=True, check=False, timeout=30)
    second = subprocess.run([str(stub), "session", "list"], capture_output=True, text=True, check=False, timeout=30)

    assert len(fakes.read_calls(stub_dir)) == 2
    assert first.stdout.strip() == "first"
    assert second.stdout.strip() == "second"


@pytest.mark.memory_only
def test_fake_clock_sleep_advances_both():
    (FakeClock,) = _load("stage3_fakes", "FakeClock")
    clock = FakeClock(start_mono=1000.0, start_wall="2026-11-02T00:00:00+00:00")
    mono0 = clock.now()
    wall0 = datetime.fromisoformat(clock.iso())

    clock.sleep(30)

    assert clock.now() - mono0 == 30
    assert (datetime.fromisoformat(clock.iso()) - wall0).total_seconds() == 30


@pytest.mark.memory_only
def test_snapshot_record_shape():
    import jsonschema

    fakes = _fakes()
    rec = fakes.snapshot(fakes.ts(2, 1))
    assert rec["type"] == "snapshot"

    schema = json.loads((ACCEPTANCE_DIR / "canary_ledger_record.schema.json").read_text(encoding="utf-8"))
    errors = [e.message for e in jsonschema.Draft202012Validator(schema).iter_errors(rec)]
    assert errors == []


@pytest.mark.memory_only
def test_policy_dict_loads(tmp_path):
    fakes = _fakes()
    (load_policy,) = _load("tools.stage3.canary_policy", "load_policy")

    policy = load_policy(fakes.write_policy(tmp_path / "p.json"))

    assert policy.status == "frozen"
    assert policy.platforms["linux"].utc_offset_minutes == 0
    assert policy.platforms["windows"].utc_offset_minutes == 0
