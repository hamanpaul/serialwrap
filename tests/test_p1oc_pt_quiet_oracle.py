"""Task P1-OC-T7：``pt-quiet-ready`` 真板 case 的純 oracle 與替身 ctx 測試（不跑真板）。

依據：`docs/rust-refactor/plans/02b-stage0-owner-confirmations.md` Task T7（#155 回歸 case）、
規格 §11.3（證據不足不得 PASS）、SC-35（passthrough attach 零 TX、靜默 30 秒才升 READY）。
驗證：
1. WAL 紀錄解析：空白、尾行寫入中略過、不可解析行與 seq 缺口一律判證據缺損；
2. attach 至 READY 全程零 TX（含 25 秒之後的 TX）、READY 不早於 30 秒靜默下限、RX 重置靜默窗；
3. 仍有 RX 的板子判 SKIP ``rx_not_quiet``、從未升 READY 的靜默板判 FAIL；
4. 命令完成判定只讀 ``cmd status`` 的 ``command`` 巢狀紀錄，``partial``／缺 marker 判 FAIL，WAL 無對應 TX 判 SKIP；
5. throwaway daemon 啟動失敗統一為 SKIP ``throwaway_start_failed``（對齊 f08／f10）。
"""
from __future__ import annotations

import base64
import importlib
import json
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

pytestmark = [pytest.mark.core_portable, pytest.mark.memory_only]

REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT / "regression"))


def _load():
    return importlib.import_module("serialwrap_regression.cases.f09_boot_uboot")


def _s(t: float, *, state: str = "ATTACHED", age=None, rx10: int = 0, tx: int = 0,
       problem: str = "", t1: float | None = None) -> dict:
    return {
        "t0": t, "t1": t + 0.01 if t1 is None else t1, "state": state,
        "last_rx_age_s": age, "rx_bytes_last_10s": rx10, "tx": tx, "wal_problem": problem,
    }


def _silent_then_ready(ready_at: float = 30.5) -> list[dict]:
    """attach_sent_s=0 時的靜默板：每秒 ATTACHED，於 ready_at 首次觀測到 READY。"""
    samples = [_s(float(t)) for t in range(0, int(ready_at))]
    samples.append(_s(ready_at, state="READY"))
    return samples


def _wal_line(seq: int, *, direction: str = "RX", payload: bytes = b"") -> str:
    return json.dumps({"seq": seq, "dir": direction, "payload_b64": base64.b64encode(payload).decode()})


# ---------------------------------------------------------------------------
# WAL 解析
# ---------------------------------------------------------------------------

def test_parse_wal_empty_text_is_not_a_problem_by_itself():
    f09 = _load()
    assert f09.parse_wal_ndjson("") == ([], "")


def test_parse_wal_ignores_unterminated_tail_being_written():
    f09 = _load()
    text = _wal_line(1) + "\n" + '{"seq": 2, "dir": "T'
    records, problem = f09.parse_wal_ndjson(text)
    assert problem == ""
    assert [r["seq"] for r in records] == [1]


def test_parse_wal_rejects_seq_gap():
    f09 = _load()
    records, problem = f09.parse_wal_ndjson(_wal_line(1) + "\n" + _wal_line(3) + "\n")
    assert "seq" in problem


def test_parse_wal_rejects_unparseable_complete_line():
    f09 = _load()
    _, problem = f09.parse_wal_ndjson(_wal_line(1) + "\nnot-json\n")
    assert "JSON" in problem


# ---------------------------------------------------------------------------
# SC-35 quiet oracle
# ---------------------------------------------------------------------------

def test_silent_promotion_at_or_after_floor_passes():
    f09 = _load()
    verdict, code, _ = f09.evaluate_pt_quiet_ready(_silent_then_ready(), attach_sent_s=0.0)
    assert (verdict, code) == ("PASS", "")


def test_tx_during_attach_window_fails():
    f09 = _load()
    samples = [_s(float(t), tx=(1 if t >= 5 else 0)) for t in range(0, 31)]
    samples.append(_s(30.5, state="READY", tx=1))
    verdict, code, _ = f09.evaluate_pt_quiet_ready(samples, attach_sent_s=0.0)
    assert (verdict, code) == ("FAIL", "passthrough_attach_sent_tx")


def test_late_tx_after_25s_but_before_ready_fails():
    f09 = _load()
    samples = [_s(float(t), tx=(1 if t >= 27 else 0)) for t in range(0, 30)]
    samples.append(_s(30.5, state="READY", tx=1))
    verdict, code, _ = f09.evaluate_pt_quiet_ready(samples, attach_sent_s=0.0)
    assert (verdict, code) == ("FAIL", "passthrough_attach_sent_tx")


def test_ready_observed_before_quiet_floor_fails():
    f09 = _load()
    samples = [_s(float(t)) for t in range(0, 10)] + [_s(10.0, state="READY")]
    verdict, code, _ = f09.evaluate_pt_quiet_ready(samples, attach_sent_s=0.0)
    assert (verdict, code) == ("FAIL", "quiet_promoted_early")


def test_rx_resets_quiet_window_so_early_ready_fails():
    """t=20 仍有 RX；promotion 不得早於 t=50，於 t=35 即 READY 應 FAIL。"""
    f09 = _load()
    samples = [_s(float(t)) for t in range(0, 20)]
    samples.append(_s(20.0, age=0.0, rx10=5))
    samples += [_s(float(t), age=float(t - 20)) for t in range(21, 35)]
    samples.append(_s(35.0, state="READY", age=15.0))
    verdict, code, _ = f09.evaluate_pt_quiet_ready(samples, attach_sent_s=0.0)
    assert (verdict, code) == ("FAIL", "quiet_promoted_early")


def test_early_ready_after_observed_rx_fails_even_though_ready_sample_has_rx():
    """ATTACHED 時已觀測到 t=20 的 RX，t=25 即 READY：屬確定違規，須 FAIL 而非因 READY 觀測點有 RX 而 SKIP。"""
    f09 = _load()
    samples = [_s(float(t)) for t in range(0, 20)]
    samples += [_s(20.0, age=0.0, rx10=5), _s(21.0, age=1.0, rx10=5), _s(24.0, age=4.0, rx10=5)]
    samples.append(_s(25.0, state="READY", age=5.0, rx10=5))
    verdict, code, _ = f09.evaluate_pt_quiet_ready(samples, attach_sent_s=0.0)
    assert (verdict, code) == ("FAIL", "quiet_promoted_early")


def test_ready_sample_with_recent_rx_is_skipped_not_passed():
    f09 = _load()
    samples = _silent_then_ready()
    samples[-1] = _s(30.5, state="READY", age=1.0, rx10=3)
    verdict, code, _ = f09.evaluate_pt_quiet_ready(samples, attach_sent_s=0.0)
    assert (verdict, code) == ("SKIP", "rx_not_quiet")


def test_noisy_board_that_never_goes_quiet_is_skipped():
    f09 = _load()
    samples = [_s(float(t), age=float(t % 20)) for t in range(0, 76)]
    verdict, code, _ = f09.evaluate_pt_quiet_ready(samples, attach_sent_s=0.0)
    assert (verdict, code) == ("SKIP", "rx_not_quiet")


def test_silent_board_never_promoted_fails():
    f09 = _load()
    samples = [_s(float(t)) for t in range(0, 76)]
    verdict, code, _ = f09.evaluate_pt_quiet_ready(samples, attach_sent_s=0.0)
    assert (verdict, code) == ("FAIL", "quiet_not_promoted")


def test_wal_problem_skips_with_insufficient_evidence():
    f09 = _load()
    samples = _silent_then_ready()
    samples[3]["wal_problem"] = "WAL seq 缺口或亂序：預期 4、實得 5"
    verdict, code, _ = f09.evaluate_pt_quiet_ready(samples, attach_sent_s=0.0)
    assert (verdict, code) == ("SKIP", "insufficient_evidence")


def test_observed_tx_takes_precedence_over_wal_problem():
    f09 = _load()
    samples = _silent_then_ready()
    samples[3]["wal_problem"] = "WAL 第 2 行無法解析為 JSON"
    samples[5]["tx"] = 1
    verdict, code, _ = f09.evaluate_pt_quiet_ready(samples, attach_sent_s=0.0)
    assert (verdict, code) == ("FAIL", "passthrough_attach_sent_tx")


def test_no_samples_is_insufficient_evidence():
    f09 = _load()
    verdict, code, _ = f09.evaluate_pt_quiet_ready([], attach_sent_s=0.0)
    assert (verdict, code) == ("SKIP", "insufficient_evidence")


# ---------------------------------------------------------------------------
# 命令完成判定
# ---------------------------------------------------------------------------

def test_command_nested_shape_passes():
    f09 = _load()
    resp = {"ok": True, "command": {"status": "done", "partial": False, "stdout": "echo\nPTQ_12345\n"}}
    verdict, code, _ = f09.evaluate_pt_command(
        f09.command_from_get(resp), marker="PTQ_12345", wal_problem="", wal_marker_seen=True)
    assert (verdict, code) == ("PASS", "")


def test_top_level_status_is_not_read_as_command():
    f09 = _load()
    resp = {"ok": True, "status": "done", "partial": False, "stdout": "PTQ_12345"}
    verdict, code, _ = f09.evaluate_pt_command(
        f09.command_from_get(resp), marker="PTQ_12345", wal_problem="", wal_marker_seen=True)
    assert (verdict, code) == ("FAIL", "completion_regex_not_matched")


def test_partial_completion_fails():
    f09 = _load()
    command = {"status": "done", "partial": True, "stdout": "PTQ_12345"}
    verdict, code, _ = f09.evaluate_pt_command(
        command, marker="PTQ_12345", wal_problem="", wal_marker_seen=True)
    assert (verdict, code) == ("FAIL", "completion_regex_not_matched")


def test_missing_marker_in_stdout_fails():
    f09 = _load()
    command = {"status": "done", "partial": False, "stdout": "other"}
    verdict, code, _ = f09.evaluate_pt_command(
        command, marker="PTQ_12345", wal_problem="", wal_marker_seen=True)
    assert (verdict, code) == ("FAIL", "completion_regex_not_matched")


def test_completed_command_without_wal_tx_is_insufficient_evidence():
    f09 = _load()
    command = {"status": "done", "partial": False, "stdout": "PTQ_12345"}
    verdict, code, _ = f09.evaluate_pt_command(
        command, marker="PTQ_12345", wal_problem="", wal_marker_seen=False)
    assert (verdict, code) == ("SKIP", "insufficient_evidence")


def test_has_tx_containing_matches_only_tx_payloads():
    f09 = _load()
    tx = {"seq": 1, "dir": "TX", "payload_b64": base64.b64encode(b"echo PTQ_1\n").decode()}
    rx = {"seq": 2, "dir": "RX", "payload_b64": base64.b64encode(b"PTQ_2\n").decode()}
    assert f09.has_tx_containing([tx, rx], "PTQ_1") is True
    assert f09.has_tx_containing([tx, rx], "PTQ_2") is False


# ---------------------------------------------------------------------------
# case 流程：throwaway 啟動失敗統一為 SKIP
# ---------------------------------------------------------------------------

def test_throwaway_start_failure_is_skip_not_fail(monkeypatch, tmp_path):
    f09 = _load()
    from serialwrap_regression import guards as guards_mod

    class _FailingDaemon:
        def __init__(self, **kwargs):
            pass

        def __enter__(self):
            raise RuntimeError("替身：daemon 未在時限內就緒")

        def __exit__(self, *exc):
            return None

    monkeypatch.setattr(guards_mod, "ThrowawayDaemon", _FailingDaemon)
    monkeypatch.setattr(f09, "_sandbox_symlink_for_board", lambda board, by_id_dir: "usb-test-if00")
    monkeypatch.setattr(f09, "_reclaim_and_ensure_ready", lambda ctx, com: None)
    ctx = SimpleNamespace(
        cfg={"boards": [{"com": "COM0", "serial": "TESTSERIAL"}], "serialwrap_exe": "/nonexistent/serialwrap"},
        case_dir=tmp_path,
        sw=SimpleNamespace(run=lambda *args, **kwargs: {"ok": True}),
        note=lambda name, content: name,
    )
    result = f09.pt_quiet_ready(ctx)
    assert result.verdict == "SKIP"
    assert result.reason_code == "throwaway_start_failed"


def test_pt_quiet_ready_case_is_registered_pending_issue():
    f09 = _load()
    from serialwrap_regression import harness

    cases = [c for c in harness.REGISTRY if c.id == "pt-quiet-ready"]
    assert len(cases) == 1
    assert cases[0].destructive is True
    assert f09.pt_quiet_ready is cases[0].run
