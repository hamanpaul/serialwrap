"""`run --endpoint win-serial` 的 peer gate 規格（Linux 純邏輯）。

DSR 映射僅於 virtual_com0com（com0com）實測；physical_usb_uart 的 DSR 可能未接，CLI 不得為其啟用 peer gate。
測試以 monkeypatch 攔截 `open_endpoint`，於取得端點規格後即中止，不啟動 bridge 或 DUT。
"""

from __future__ import annotations

import pytest


class _SpecCaptured(Exception):
    """攔截 open_endpoint 時丟出，用以中止 `_run_command` 而不進入 bridge 執行。"""


def _win_serial_argv(evidence_dir: str, *extra: str) -> list[str]:
    return [
        "--run-id",
        "r-spec",
        "--case-id",
        "c-spec",
        "--port-id",
        "COM0",
        "--evidence-dir",
        evidence_dir,
        "--endpoint",
        "win-serial",
        "--com",
        "COMb",
        "--dut-mode",
        "local-process",
        "--dut-argv-json",
        '["true"]',
        *extra,
    ]


@pytest.mark.parametrize(
    ("extra", "expected_peer_gate"),
    [
        ([], True),
        (["--transport-mode", "virtual_com0com"], True),
        (["--transport-mode", "virtual_com0com", "--no-peer-gate"], False),
        (["--transport-mode", "physical_usb_uart"], False),
        (["--transport-mode", "physical_usb_uart", "--no-peer-gate"], False),
    ],
)
def test_win_serial_run_peer_gate_spec(tmp_path, monkeypatch, extra, expected_peer_gate):
    import uart_stdio_bridge.endpoints as endpoints
    from uart_stdio_bridge.cli import _run_command

    captured: dict = {}

    def fake_open_endpoint(spec):
        captured.update(spec)
        raise _SpecCaptured

    monkeypatch.setattr(endpoints, "open_endpoint", fake_open_endpoint)
    with pytest.raises(_SpecCaptured):
        _run_command(_win_serial_argv(str(tmp_path / "evidence"), *extra))

    assert captured["kind"] == "win-serial"
    assert captured["peer_gate"] is expected_peer_gate
