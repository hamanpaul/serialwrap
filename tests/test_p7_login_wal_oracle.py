from __future__ import annotations

import pytest

pytestmark = [pytest.mark.core_portable, pytest.mark.memory_only]

from contract_blackbox.p7_login_oracle import assert_login_window, login_window_problems


def _row(seq: int, direction: str, source: str, payload: bytes) -> dict:
    import base64

    return {
        "seq": seq,
        "mono_ts_ns": seq * 1000,
        "dir": direction,
        "source": source,
        "payload_b64": base64.b64encode(payload).decode("ascii"),
    }


def _login_rows() -> list[dict]:
    return [
        _row(10, "TX", "system", b"root\n"),
        _row(11, "TX", "human:console-1", b"HUMAN_DURING_LOGIN_P7\n"),
        _row(12, "TX", "system", b"pw\n"),
        _row(13, "TX", "system", b"echo __READY__abc123\n"),
        _row(14, "RX", "system", b"__READY__abc123\r\nroot@fake:~# "),
    ]


def test_login_window_oracle_rejects_synthetic_agent_tx():
    rows = _login_rows()
    assert login_window_problems(rows, login_start_seq=10, ready_seq=14, human_payload=b"HUMAN_DURING_LOGIN_P7\n") == []
    mutated = rows[:2] + [_row(12, "TX", "agent:mutation", b"echo forbidden\n")] + rows[2:]
    with pytest.raises(AssertionError, match="agent TX"):
        assert_login_window(mutated, login_start_seq=10, ready_seq=14, human_payload=b"HUMAN_DURING_LOGIN_P7\n")


def test_login_window_oracle_rejects_swallowed_human_bytes():
    rows = [row for row in _login_rows() if row["source"] != "human:console-1"]
    with pytest.raises(AssertionError, match="human TX"):
        assert_login_window(rows, login_start_seq=10, ready_seq=14, human_payload=b"HUMAN_DURING_LOGIN_P7\n")

    missing_mono = [dict(row) for row in _login_rows()]
    missing_mono[2].pop("mono_ts_ns")
    with pytest.raises(AssertionError, match="mono_ts_ns"):
        assert_login_window(
            missing_mono, login_start_seq=10, ready_seq=14,
            human_payload=b"HUMAN_DURING_LOGIN_P7\n",
        )
