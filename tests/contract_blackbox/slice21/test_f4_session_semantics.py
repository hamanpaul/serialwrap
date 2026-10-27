"""F4 session 語意（Slice 2.1 黑箱，承接 f4-* 案例）。

- test_interactive_line_cmd_defined 屬 Slice 2.2 lock，於 2.1 作提前證據，不計入 2.1 分母。
"""
import re

import pytest

pytestmark = pytest.mark.cap_posix_pty

ACTIVITY_CLASSES = {"active", "idle-healthy", "quiet-suspicious", "newly-attached", "offline"}


def test_activity_classification(shell_daemon):
    # `session.activity` 回應形狀同 `session.get_state`：`{"ok": true, "session": {...}}`（Python
    # service.py `get_session_state`）；分類欄位位於 `session` 物件內（P5-T20 補強：原本讀頂層鍵，
    # Python 基線與 Rust 皆 KeyError；改讀正確路徑並加斷言 ok，語意不放寬）。
    before = shell_daemon.sw("session", "activity", "--selector", "COM0")
    assert before["ok"] is True and before["_rc"] == 0
    assert before["session"]["activity_classification"] in ACTIVITY_CLASSES
    sub = shell_daemon.sw("cmd", "submit", "--selector", "COM0", "--cmd", "echo act")
    shell_daemon.wait_command_done(sub["cmd_id"], timeout=15)
    after = shell_daemon.sw("session", "activity", "--selector", "COM0")
    assert after["ok"] is True and after["_rc"] == 0
    assert after["session"]["activity_classification"] in ACTIVITY_CLASSES


def test_background_result_tail_consistent(shell_daemon):
    sub = shell_daemon.sw("cmd", "submit", "--selector", "COM0", "--cmd", "printf M1..M50", "--mode", "background")
    cid = sub["cmd_id"]
    shell_daemon.wait_command_done(cid, timeout=30)
    text_parts: list[str] = []
    cursor = 0
    for _ in range(100):
        page = shell_daemon.sw("cmd", "result-tail", "--cmd-id", cid, "--from-chunk", str(cursor), "--limit", "200")
        assert page["ok"] is True
        if not page["chunks"]:
            break
        text_parts.extend(page["chunks"])
        cursor = page["next_chunk"]
    full = "".join(text_parts)
    # 只計「獨立成行的 M<n>」，避免把命令回顯 `printf M1..M50` 誤計入
    found = re.findall(r"(?m)^M(\d+)\r?$", full)
    for i in range(1, 51):
        assert found.count(str(i)) == 1, f"M{i} 應恰出現 1 次，實際 {found.count(str(i))} 次"


def test_interactive_line_cmd_defined(shell_daemon):
    lease = shell_daemon.sw("session", "interactive-open", "--selector", "COM0", "--owner", "agent")
    assert "interactive_id" in lease, lease
    iid = lease["interactive_id"]
    try:
        r = shell_daemon.sw("cmd", "submit", "--selector", "COM0", "--cmd", "echo ilc")
        if "cmd_id" in r and "error_code" not in r:
            done = shell_daemon.wait_command_done(r["cmd_id"], timeout=30)
            assert done["status"] in {"done", "error"}
            assert done.get("error_code") != "PROMPT_TIMEOUT"
        else:
            assert r.get("error_code"), f"lease 期間 submit 既非命令也非錯誤碼：{r}"
    finally:
        shell_daemon.sw("session", "interactive-close", "--interactive-id", iid)
    shell_daemon.wait_state("COM0", "READY", timeout=15)
