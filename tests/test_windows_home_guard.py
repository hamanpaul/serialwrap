"""Windows 使用者家目錄 guard 僅追蹤 setup 資產。"""
from __future__ import annotations

import importlib.util
from pathlib import Path

import pytest

pytestmark = [pytest.mark.core_portable, pytest.mark.memory_only]


def test_live_state_and_wal_do_not_change_static_home_snapshot(tmp_path):
    helper_path = Path(__file__).with_name("windows_home_snapshot.py")
    spec = importlib.util.spec_from_file_location("windows_home_snapshot_test_helper", helper_path)
    assert spec is not None and spec.loader is not None
    helper = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(helper)
    snapshot_user_home = helper.snapshot_windows_user_home

    home = tmp_path / "profile"
    home.mkdir()
    live_state = home / ".local" / "state" / "serialwrap" / "state.json"
    live_wal = home / ".local" / "state" / "serialwrap" / "wal" / "raw.wal.ndjson"
    live_state.parent.mkdir(parents=True)
    live_wal.parent.mkdir(parents=True)
    live_state.write_bytes(b"state before\n")
    live_wal.write_bytes(b"wal before\n")

    before = snapshot_user_home(str(home))
    live_state.write_bytes(b"state changed\n")
    with live_wal.open("ab") as stream:
        stream.write(b"normal WAL append\n")
    after_live_writes = snapshot_user_home(str(home))
    assert before == after_live_writes

    cli_wrapper = home / ".local" / "bin" / "serialwrap"
    cli_wrapper.parent.mkdir(parents=True)
    cli_wrapper.write_text("wrapper\n", encoding="utf-8")
    after_setup_asset_write = snapshot_user_home(str(home))
    assert after_setup_asset_write != after_live_writes
