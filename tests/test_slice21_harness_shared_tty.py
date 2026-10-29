"""rulings/p5.md（P6 第 5 輪 SPEC-GAP #1，編排者裁決照修）：`tests/contract_blackbox/slice21/harness.py` 的
`start_daemon` 對共用同一 tty 的第二個 daemon 不得另建 FakeTarget，也不得改寫第一個 daemon 的 by-id symlink。

舊行為：第二個 daemon 以 `extra_env={"SERIALWRAP_BY_ID_DIR": <第一個 daemon 的 by-id 目錄>}` 啟動時，harness 仍新建
FakeTarget 並 `target.start(by_id_dir, BY_ID_NAME)`，把 `fake-uart0` 改指向新的 PTY——`test_two_daemons_same_tty`
因此不是兩個 daemon 搶同一 tty。"""
from __future__ import annotations

import os
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "tests" / "contract_blackbox" / "slice21"))
sys.path.insert(0, str(ROOT / "tests" / "contract_blackbox"))

pytestmark = [pytest.mark.posix_only, pytest.mark.posix_pty, pytest.mark.process_rpc]


@pytest.mark.skipif(os.name != "posix", reason="POSIX PTY FakeTarget")
def test_second_daemon_sharing_tty_keeps_first_by_id_link(tmp_path):
    import harness

    first = harness.start_daemon(tmp_path / "first")
    second = None
    try:
        link = Path(first.env["SERIALWRAP_BY_ID_DIR"]) / harness.BY_ID_NAME
        before = os.readlink(link)
        assert before == first.targets["COM0"].slave_path
        second = harness.start_daemon(tmp_path / "second", share_tty_with=first)
        assert os.readlink(link) == before, "第二個 daemon 改寫了第一個 daemon 的 by-id symlink"
        assert second.targets["COM0"] is first.targets["COM0"], "共用 tty 時不得另建 FakeTarget"
        assert second.env["SERIALWRAP_BY_ID_DIR"] == first.env["SERIALWRAP_BY_ID_DIR"]
        second.stop()
        assert first.targets["COM0"].link_path is not None and os.path.islink(link), "停第二個 daemon 不得關閉共用 FakeTarget"
        assert first.rpc("health.ping", {}).get("ok")
    finally:
        if second is not None:
            second.stop()
        first.stop()
