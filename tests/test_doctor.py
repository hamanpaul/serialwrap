"""Task 12 — serialwrap doctor 環境診斷的單元測試。

驗證重點：
- dialout 群組缺漏時 ok=False 並給出 usermod 修復提示。
- dialout 為成員時 ok=True 且 fix 為空字串。
- python 檢查在目前直譯器（≥3.10）下必通過。
"""


def test_doctor_reports_dialout_missing_with_fix():
    from sw_core.sysenv import FakeEffects
    from sw_core.doctor_cmd import run_doctor
    fx = FakeEffects(systemd=True, in_groups=set())  # 不在 dialout
    report = run_doctor(fx=fx)
    item = next(i for i in report if i["check"] == "dialout")
    assert item["ok"] is False
    assert "usermod -aG dialout" in item["fix"]


def test_doctor_dialout_ok_when_member():
    from sw_core.sysenv import FakeEffects
    from sw_core.doctor_cmd import run_doctor
    report = run_doctor(fx=FakeEffects(systemd=True, in_groups={"dialout"}))
    item = next(i for i in report if i["check"] == "dialout")
    assert item["ok"] is True and item["fix"] == ""


def test_doctor_python_check_passes_on_current_interpreter():
    from sw_core.doctor_cmd import run_doctor
    report = run_doctor()
    assert next(i for i in report if i["check"] == "python")["ok"] is True
