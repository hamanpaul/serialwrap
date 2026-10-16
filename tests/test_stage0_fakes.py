# tests/test_stage0_fakes.py
"""P1 Stage 0 測試設施（T01）的 A1 紅燈測試：替身、平台 marker、TX 結果純型別、junit 摘要。

依 02-stage0-python-bc.md Global Constraints：本模組不在頂層 import 本 Phase 才新增的
模組或名稱（`stage0_fakes`、`sw_core.serial_port.TxResult`、`sw_core.uart_io.TxIncompleteError` 等），
一律於測試函式內以 `_load()` 延遲 import，使 `python3 -m pytest -q --collect-only tests/` 在任何階段都能完成收集。

marker 規則（P0 T14）：資源 marker `memory_only` 放模組層；平台 marker 放函式層
（同檔混合平台，不得在模組層放平台 marker，否則每個 item 的平台 marker 數不為 1）。
"""
from __future__ import annotations

import importlib
import importlib.util
import os
import threading
from pathlib import Path
from typing import Any

import pytest

pytestmark = [pytest.mark.memory_only]


def _load(module_name: str, *names: str) -> list[Any]:
    """延遲 import 模組並取出名稱；相依模組或名稱尚未實作時於測試函式內拋出 ImportError／ModuleNotFoundError。"""
    mod = importlib.import_module(module_name)
    out: list[Any] = []
    for name in names:
        if not hasattr(mod, name):
            raise ImportError(f"cannot import name {name!r} from {module_name!r}")
        out.append(getattr(mod, name))
    return out


@pytest.mark.core_portable
def test_scripted_write_consumes_actions_in_order():
    (ScriptedWrite,) = _load("stage0_fakes", "ScriptedWrite")
    w = ScriptedWrite([2, OSError(5, "EIO")])
    assert w(3, b"abcd") == 2
    with pytest.raises(OSError):
        w(3, b"cd")
    assert w.calls == [b"abcd", b"cd"]


@pytest.mark.core_portable
def test_fake_port_blocks_until_event_set():
    (FakePort,) = _load("stage0_fakes", "FakePort")
    gate = threading.Event()
    port = FakePort(block=gate)
    done = threading.Event()
    t = threading.Thread(target=lambda: (port.write(b"x"), done.set()), daemon=True)
    t.start()
    assert not done.wait(0.1)
    gate.set()
    assert done.wait(1.0)
    assert port.writes == [b"x"]
    t.join(1.0)


@pytest.mark.core_portable
def test_make_bridge_uses_fake_port_and_isolated_wal(tmp_path):
    FakePort, make_bridge, wal_records = _load("stage0_fakes", "FakePort", "make_bridge", "wal_records")
    _bridge, wal = make_bridge(tmp_path, port=FakePort())
    assert wal.wal_path.startswith(str(tmp_path))
    assert wal_records(wal) == []


@pytest.mark.windows_native
def test_windows_native_marker_only_runs_on_nt():
    assert os.name == "nt"


@pytest.mark.core_portable
def test_tx_result_complete_property():
    (TxResult,) = _load("sw_core.serial_port", "TxResult")
    assert TxResult("completed", 3, 3).complete is True
    assert TxResult("completed", 2, 3, "short_write").complete is False
    assert TxResult("unknown", 3, 3).complete is False


@pytest.mark.core_portable
def test_tx_incomplete_error_family_is_oserror_and_runtimeerror_compatible():
    import errno

    (TxResult,) = _load("sw_core.serial_port", "TxResult")
    SerialNotReady, TxIncompleteError, TxUnknownError = _load(
        "sw_core.uart_io", "SerialNotReady", "TxIncompleteError", "TxUnknownError"
    )
    r = TxResult("not_submitted", 0, 1, "serial not ready")
    assert isinstance(SerialNotReady(r), RuntimeError) and isinstance(SerialNotReady(r), OSError)
    e = TxUnknownError(TxResult("unknown", 2, 5, "SerialTimeoutException: x"), errno.ETIMEDOUT)
    assert isinstance(e, TxIncompleteError) and e.errno == errno.ETIMEDOUT and e.result.confirmed == 2


@pytest.mark.core_portable
def test_junit_summary_records_exit_and_duplicates(tmp_path):   # R5-F3：summary 必帶 rc／finished／duplicates
    p = Path(__file__).resolve().parents[1] / "scripts" / "stage0" / "junit_summary.py"
    assert p.is_file(), "scripts/stage0/junit_summary.py 尚未建立（T01 B1）"
    spec = importlib.util.spec_from_file_location("junit_summary", p)
    js = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(js)
    x = tmp_path / "j.xml"
    x.write_text('<testsuites><testsuite tests="3" failures="0" errors="0" skipped="1">'
                 '<testcase classname="tests.test_a" name="t1"/><testcase classname="tests.test_a" name="t1"/>'
                 '<testcase classname="tests.test_a" name="t2"><skipped message="x"/></testcase></testsuite></testsuites>',
                 encoding="utf-8")
    s = js.summarize(x, pytest_exit=0, require_native=False)
    assert s["rc"] == 0 and s["pytest_exit"] == 0 and s["finished"] is True
    assert s["duplicates"] == ["tests/test_a.py::t1"] and [k["nodeid"] for k in s["skipped"]] == ["tests/test_a.py::t2"]
    assert js.summarize(x, pytest_exit=2, require_native=False)["rc"] == 2
    assert js.summarize(tmp_path / "missing.xml", pytest_exit=0, require_native=False)["finished"] is False
