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
    env = guards.throwaway_env(tmp_path / "wd", tmp_path / "byid", tmp_path / "rt")
    for key in ("SERIALWRAP_STATE_DIR", "SERIALWRAP_WAL_DIR",
                "SERIALWRAP_CONFIG_DIR", "SERIALWRAP_PROFILE_DIR",
                "SERIALWRAP_BY_PATH_DIR"):
        assert env[key].startswith(str(tmp_path / "wd")), key
    # RUN_DIR 必須是獨立短路徑（AF_UNIX sun_path 107 字元上限，首輪實測 108/111 必現超限）
    assert env["SERIALWRAP_RUN_DIR"] == str(tmp_path / "rt")
    assert env["SERIALWRAP_BY_ID_DIR"] == str(tmp_path / "byid")
    # by-path 沙盒化（round 4 實測）：漏覆寫會讓 throwaway 掃到主機真實 by-path、
    # 把 PROD 在用的裝置撈進偵測池（two-reader 風險）
    assert env["SERIALWRAP_BY_PATH_DIR"] == str(tmp_path / "wd" / "bypath")
    assert "SERIALWRAP_ENDPOINT" not in env
