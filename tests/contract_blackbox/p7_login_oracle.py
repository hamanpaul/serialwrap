"""P7 登入窗口的 WAL 判定器（OD-86）。"""
from __future__ import annotations

import base64
import re


def _seq(row: dict) -> int:
    try:
        return int(row.get("seq"))
    except (TypeError, ValueError):
        return -1


def _payload(row: dict) -> bytes:
    try:
        return base64.b64decode(row.get("payload_b64") or "", validate=True)
    except (ValueError, TypeError):
        return b""


def login_window_bounds(
    rows: list[dict],
    *,
    after_seq: int,
    username: bytes,
    ready_prompt: bytes,
) -> tuple[int, int]:
    """依序定位 username TX 到 nonce probe 後 READY prompt RX 的窗口。"""
    ordered = sorted((row for row in rows if _seq(row) > after_seq), key=_seq)
    username_row = next(
        (row for row in ordered if row.get("dir") == "TX" and row.get("source") == "system" and _payload(row) == username),
        None,
    )
    if username_row is None:
        raise AssertionError(f"WAL 缺少登入 username TX：{username!r}")
    start = _seq(username_row)
    probe_row = next(
        (row for row in ordered if _seq(row) > start and row.get("dir") == "TX"
         and row.get("source") == "system" and b"__READY__" in _payload(row)),
        None,
    )
    if probe_row is None:
        raise AssertionError("WAL 缺少登入後的 nonce probe TX")
    nonce_match = re.search(rb"__READY__([0-9a-fA-F]+)", _payload(probe_row))
    if nonce_match is None:
        raise AssertionError("WAL nonce probe TX 缺少可解析的 __READY__ nonce")
    nonce = nonce_match.group(1)
    probe_seq = _seq(probe_row)
    rx = bytearray()
    rx_seq_by_byte: list[int] = []
    nonce_seen = False
    for row in ordered:
        if _seq(row) <= probe_seq or row.get("dir") != "RX":
            continue
        payload = _payload(row)
        if not payload:
            continue
        rx.extend(payload)
        rx_seq_by_byte.extend([_seq(row)] * len(payload))
        nonce_at = rx.find(nonce)
        if nonce_at < 0:
            continue
        nonce_seen = True
        prompt_at = rx.find(ready_prompt, nonce_at + len(nonce))
        if prompt_at >= 0:
            prompt_end = prompt_at + len(ready_prompt) - 1
            return start, rx_seq_by_byte[prompt_end]
    if not nonce_seen:
        raise AssertionError(f"WAL 缺少 nonce probe 的實際回應：{nonce!r}")
    raise AssertionError(f"WAL nonce 回應後缺少 READY prompt：{ready_prompt!r}")


def login_window_problems(
    rows: list[dict],
    *,
    login_start_seq: int,
    ready_seq: int,
    human_payload: bytes,
) -> list[str]:
    """檢查登入到 READY 的窗口沒有 agent TX，且 human 輸入確實記入 WAL。"""
    window = [row for row in rows if login_start_seq <= _seq(row) <= ready_seq]
    problems: list[str] = []
    for row in rows:
        if _seq(row) < 0:
            problems.append("WAL 記錄缺少有效 seq")
    timed: list[tuple[int, int]] = []
    for row in window:
        try:
            timed.append((_seq(row), int(row["mono_ts_ns"])))
        except (KeyError, TypeError, ValueError):
            problems.append(f"登入窗口 WAL 記錄缺少有效 mono_ts_ns：seq={_seq(row)}")
    agents = [row for row in window if row.get("dir") == "TX" and str(row.get("source", "")).startswith("agent:")]
    if agents:
        problems.append(f"登入窗口含 agent TX：{[_seq(row) for row in agents]}")
    humans = [
        row for row in window
        if row.get("dir") == "TX" and str(row.get("source", "")).startswith("human:")
        and human_payload in _payload(row)
    ]
    if not humans:
        problems.append(f"登入窗口沒有記錄 human TX：{human_payload!r}")
    if any(later[1] < earlier[1] for earlier, later in zip(timed, timed[1:])):
        problems.append(f"登入窗口 WAL mono_ts_ns 倒退：{timed}")
    return problems


def assert_login_window(
    rows: list[dict],
    *,
    login_start_seq: int,
    ready_seq: int,
    human_payload: bytes,
) -> None:
    """違反 OD-86 的任一條件即讓案例失敗。"""
    problems = login_window_problems(
        rows,
        login_start_seq=login_start_seq,
        ready_seq=ready_seq,
        human_payload=human_payload,
    )
    assert not problems, "; ".join(problems)
