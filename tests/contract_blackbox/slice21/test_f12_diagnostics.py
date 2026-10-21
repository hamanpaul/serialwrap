"""F12 診斷（Slice 2.1 黑箱，承接 L1 案例；OD-13 修訂 oracle）。

版本一致性與 WAL 活體 marker 的 CRC 驗證：讀回可關聯 marker 的新紀錄並驗證 CRC，不以 mtime 判定。
"""
import base64
import re
import secrets
import subprocess
import zlib

import pytest

pytestmark = pytest.mark.cap_posix_pty


def test_f12_version_reported_consistent(shell_daemon):
    cli_v = subprocess.run([shell_daemon.cli, "--version"], capture_output=True, text=True, env=shell_daemon.env).stdout
    status = shell_daemon.sw("daemon", "status")
    assert re.search(r"\d+\.\d+\.\d+", cli_v).group(0) == re.search(r"\d+\.\d+\.\d+", status["version"]).group(0)


def test_f12_wal_path_live_marker_crc(shell_daemon):
    st = shell_daemon.sw("daemon", "status")
    start = shell_daemon.sw("wal", "current-seq")["seq"]
    marker = f"MARKER_{secrets.token_hex(3)}"
    sub = shell_daemon.sw("cmd", "submit", "--selector", "COM0", "--cmd", f"echo {marker}", "--source", "agent:f12")
    shell_daemon.wait_command_done(sub["cmd_id"], timeout=15)               # 決定性同步：等命令終態，TX 必已寫入 WAL
    page = shell_daemon.sw("log", "tail-raw", "--selector", "COM0", "--from-seq", str(start), "--limit", "200")
    hits = [r for r in page["records"] if r["dir"] == "TX" and marker.encode() in base64.b64decode(r["payload_b64"])]
    assert hits, "TX record carrying the marker must appear after start seq"
    r = hits[0]
    raw = base64.b64decode(r["payload_b64"])
    assert r["cmd_id"] == sub["cmd_id"] and r["len"] == len(raw) and r["crc32"] == f"{zlib.crc32(raw) & 0xffffffff:08x}"
    assert r["seq"] > start and st["wal_path"].endswith("raw.wal.ndjson") and st["wal"]["healthy"] is True
