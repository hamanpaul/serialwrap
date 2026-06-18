from sw_core.service import SerialwrapService


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
