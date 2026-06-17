from sw_core.flash_endpoint import resolve_flash_target


def _sess(com, command_capable):
    return {"com": com, "by_id": com + "-id", "command_capable": command_capable}


def test_explicit_console_blocked_without_force():
    sessions = [_sess("COM1", True)]
    res = resolve_flash_target("COM1", sessions, force=False)
    assert res["ok"] is False and res["error_code"] == "FLASH_TARGET_IS_CONSOLE"


def test_explicit_console_allowed_with_force():
    sessions = [_sess("COM1", True)]
    res = resolve_flash_target("COM1", sessions, force=True)
    assert res["ok"] is True and res["by_id"] == "COM1-id"


def test_non_console_target_ok():
    sessions = [_sess("COM0", False)]
    res = resolve_flash_target("COM0", sessions, force=False)
    assert res["ok"] is True and res["by_id"] == "COM0-id"


def test_unknown_selector():
    res = resolve_flash_target("COMX", [_sess("COM0", False)], force=False)
    assert res["ok"] is False and res["error_code"] == "SESSION_NOT_FOUND"
