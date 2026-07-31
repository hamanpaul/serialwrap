"""#155 U-Boot 唯讀護欄／throwaway env 純邏輯單測。"""
from __future__ import annotations

import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT / "regression"))

from serialwrap_regression import guards  # noqa: E402


@pytest.mark.parametrize("cmd", [
    "saveenv",
    "env save",
    "env  save",
    "env default -a",
    "setenv bootdelay 0",
    "setenv bootcmd boot",
    "sf write 0 0 100",
    "nand write 0 0 100",
    "mmc write 0 0 100",
    "tftpboot 0x80000 fw.bin",
    "printenv; saveenv",
    "printenv\nsaveenv",
    "printenv && saveenv",
    "printenv | grep boot",
    "run bootcmd",
    "",
])
def test_uboot_forbidden(cmd):
    with pytest.raises(guards.UBootGuardError):
        guards.validate_uboot_cmd(cmd)


@pytest.mark.parametrize("cmd", [
    "printenv",
    "printenv bootcmd",
    "bdinfo",
    "version",
    "help",
    "echo hi",
])
def test_uboot_allowed(cmd):
    guards.validate_uboot_cmd(cmd)  # 不 raise


def test_leave_rejects_non_whitelisted_via():
    con = guards.UBootConsole(ctx=None, com="COM0", tmux_session="s")
    with pytest.raises(guards.UBootGuardError):
        con.leave(via="poweroff")


def test_throwaway_env_isolates_all_dirs(tmp_path):
    env = guards.throwaway_env(tmp_path / "wd", tmp_path / "byid")
    for key in ("SERIALWRAP_RUN_DIR", "SERIALWRAP_STATE_DIR", "SERIALWRAP_WAL_DIR",
                "SERIALWRAP_CONFIG_DIR", "SERIALWRAP_PROFILE_DIR"):
        assert env[key].startswith(str(tmp_path / "wd")), key
    assert env["SERIALWRAP_BY_ID_DIR"] == str(tmp_path / "byid")
    assert "SERIALWRAP_ENDPOINT" not in env
