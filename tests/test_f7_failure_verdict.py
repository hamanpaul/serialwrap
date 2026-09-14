"""F7 checksum oracle 的純邏輯回歸測試；不啟動真板或 live ctx.sw。"""
from __future__ import annotations

import sys
from pathlib import Path
from types import SimpleNamespace

REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT))
sys.path.insert(0, str(REPO_ROOT / "regression"))

from realhw.harness import CaseResult  # noqa: E402
from serialwrap_regression.cases import f07_file_transfer as f07


def test_checksum_mismatch_is_test_failure_independent_of_tool_probe() -> None:
    for tools_present in (True, False, None):
        result = f07._transfer_failure_verdict(
            {"ok": False, "error_code": "CHECKSUM_MISMATCH"},
            verb="pull",
            tools_present=tools_present,
        )

        assert result.verdict == "FAIL"
        assert result.category == "test"
        assert result.reason_code == "binary_roundtrip_mismatch"


def test_non_checksum_transfer_failures_keep_existing_environment_classification() -> None:
    for code in ("PULL_PARSE_FAILED", "MOVE_FAILED", "TRANSFER_TIMEOUT"):
        result = f07._transfer_failure_verdict(
            {"ok": False, "error_code": code},
            verb="pull",
            tools_present=True,
        )

        assert result.verdict == "SKIP"
        assert result.category == "environment"
        assert result.reason_code == (
            "transfer_timeout" if code == "TRANSFER_TIMEOUT" else "transfer_environment_failure"
        )


def test_run_on_boards_stops_after_checksum_failure_instead_of_hiding_it_with_pass() -> None:
    ctx = SimpleNamespace(
        cfg={"boards": [{"com": "COM0"}, {"com": "COM1"}]},
        notes=[],
    )
    ctx.note = lambda name, value: ctx.notes.append((name, value))
    called: list[str] = []

    def runner(_ctx, com: str) -> CaseResult:
        called.append(com)
        if com == "COM0":
            return f07._transfer_failure_verdict(
                {"ok": False, "error_code": "CHECKSUM_MISMATCH"},
                verb="pull",
                tools_present=None,
            )
        return CaseResult("PASS")

    result = f07._run_on_boards(ctx, runner, case_tag="rt")

    assert called == ["COM0"]
    assert result.verdict == "FAIL"
    assert result.category == "test"
    assert result.reason_code == "binary_roundtrip_mismatch"
    assert result.reason.startswith("[COM0] ")
    assert ctx.notes == []
