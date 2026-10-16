"""socat 接線 spike 的失敗測試（§15.4.1 第一步；Task 9 T9-1）。

spike 只是拓樸可行性檢查，不是正式差分證據，也不是 gate：
socat 缺席時腳本輸出 `NOT_RUN` 並以 exit 0 結束，而不是失敗。
本檔只依 Task 9 Interfaces 撰寫，不窺探實作；腳本或子程序尚未存在時，
測試以明確的「檔案不存在」訊息失敗（紅燈的原因是缺少行為，不是 collection 錯誤）。

marker 說明：socat PTY 測試標 `posix_pty`（Task 3 於工具 conftest 註冊），
Windows L1 job 以 `-m "not posix_pty ..."` 排除；echo 子程序測試純 stdlib，可跨平台執行。
"""

from __future__ import annotations

import json
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

TOOL_ROOT = Path(__file__).resolve().parents[1]
SPIKE_DIR = TOOL_ROOT / "spikes"
SCRIPT = SPIKE_DIR / "socat_wiring.sh"
ECHO_CHILD = SPIKE_DIR / "echo_child.py"

SCHEMA_VERSION = "socat-spike.v1"
NOTE_MARKER = "非差分證據"
SPIKE_TIMEOUT_S = 120
ECHO_TIMEOUT_S = 30


def _socat_missing() -> bool:
    # 每次測試時才查詢 PATH，避免模組匯入時就固定住結果
    return shutil.which("socat") is None


def _run_spike(out_dir: Path) -> subprocess.CompletedProcess:
    # 以顯式 bash 呼叫，不依賴腳本的可執行位元
    assert SCRIPT.is_file(), f"spike 腳本不存在：{SCRIPT}（Task 9 I9-1 尚未實作）"
    return subprocess.run(
        ["bash", str(SCRIPT), "--out", str(out_dir)],
        capture_output=True,
        timeout=SPIKE_TIMEOUT_S,
        check=False,
    )


def _assert_ok(proc: subprocess.CompletedProcess) -> None:
    stderr = proc.stderr.decode("utf-8", errors="replace")
    assert proc.returncode == 0, f"spike 腳本應 exit 0，實得 {proc.returncode}；stderr={stderr!r}"


def _last_stdout_json(proc: subprocess.CompletedProcess) -> dict:
    text = proc.stdout.decode("utf-8", errors="replace")
    lines = [ln for ln in text.splitlines() if ln.strip()]
    assert lines, "腳本沒有任何 stdout 輸出（NOT_RUN 路徑應印出 JSON）"
    return json.loads(lines[-1])


def _read_report(out_dir: Path) -> dict:
    report_path = out_dir / "spike-report.json"
    assert report_path.is_file(), f"缺少 spike-report.json：{report_path}"
    return json.loads(report_path.read_text(encoding="utf-8"))


@pytest.mark.posix_only
@pytest.mark.posix_pty
@pytest.mark.process_rpc
def test_spike_report_shape(tmp_path):
    """socat 缺席時輸出 NOT_RUN；存在時報告 schema_version 與 note 符合契約。"""
    out_dir = tmp_path / "spike"
    out_dir.mkdir()
    proc = _run_spike(out_dir)
    _assert_ok(proc)

    if _socat_missing():
        result = _last_stdout_json(proc)
        assert result["result"] == "NOT_RUN"
        assert result["reason"] == "socat_missing"
        return

    report = _read_report(out_dir)
    assert report["schema_version"] == SCHEMA_VERSION
    assert NOTE_MARKER in report["note"]


@pytest.mark.posix_only
@pytest.mark.posix_pty
@pytest.mark.process_rpc
def test_spike_checks_pass_with_socat(tmp_path):
    """socat 存在時三項拓樸檢查皆為 True。"""
    if _socat_missing():
        pytest.skip("socat 不存在：本項只在 socat 存在時適用（缺席時由 test_spike_report_shape 覆蓋 NOT_RUN）")

    out_dir = tmp_path / "spike"
    out_dir.mkdir()
    proc = _run_spike(out_dir)
    _assert_ok(proc)

    checks = _read_report(out_dir)["checks"]
    assert checks["raw_roundtrip"] is True
    assert checks["no_echo"] is True
    assert checks["eof_on_child_exit"] is True


@pytest.mark.posix_only
@pytest.mark.posix_pty
@pytest.mark.process_rpc
def test_spike_report_has_no_verdict(tmp_path):
    """spike 報告不得含 verdict 欄位（spike 不是 gate 證據）。"""
    out_dir = tmp_path / "spike"
    out_dir.mkdir()
    proc = _run_spike(out_dir)
    _assert_ok(proc)

    if _socat_missing():
        # NOT_RUN 路徑沒有報告檔，檢查其 stdout JSON 同樣不含 verdict
        assert "verdict" not in _last_stdout_json(proc)
        return

    report = _read_report(out_dir)
    assert "verdict" not in report


@pytest.mark.process_rpc
def test_echo_child_binary():
    """echo_child.py 以 binary 原樣回寫，讀到 __EXIT__ 行即 exit 0。"""
    assert ECHO_CHILD.is_file(), f"echo 子程序不存在：{ECHO_CHILD}（Task 9 I9-1 尚未實作）"
    payload = bytes(range(256)) + b"\n__EXIT__\n"
    expected = bytes(range(256)) + b"\n"
    proc = subprocess.run(
        [sys.executable, str(ECHO_CHILD)],
        input=payload,
        capture_output=True,
        timeout=ECHO_TIMEOUT_S,
        check=False,
    )
    assert proc.returncode == 0, f"echo_child 應 exit 0，實得 {proc.returncode}；stderr={proc.stderr!r}"
    assert proc.stdout == expected
