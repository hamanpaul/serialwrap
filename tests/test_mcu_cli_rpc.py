import pytest

from sw_core.service import SerialwrapService

try:
    import state_iso  # pytest／unittest discover：tests/ 在 sys.path
except ImportError:  # python3 -m unittest tests.test_x（repo root 跑法，#120）
    from tests import state_iso


@pytest.fixture(autouse=True)
def _iso_state():
    """#120 per-file 隔離：SerialwrapService([]) 建構會落 state.json（單檔直跑防線）。"""
    with state_iso.isolated_state():
        yield


def test_mcu_patterns_lists_families():
    svc = SerialwrapService([])
    res = svc.rpc("mcu.patterns", {})
    assert res["ok"] is True
    assert any(p["family"] == "ti-cc26xx" for p in res["patterns"])


def test_mcu_status_reports_candidates_and_flashing():
    svc = SerialwrapService([])
    res = svc.rpc("mcu.status", {})
    assert res["ok"] is True
    assert "candidates" in res
    assert res["flashing"] is False
