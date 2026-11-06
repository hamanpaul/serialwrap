"""`bb_l15.run_nodeid_in_pidns` 的 exit status 判定回歸（以 stub 子行程取代 unshare 與 pytest，不需真實 namespace）。

舊版以 ``del returncode`` 丟棄子 pytest 的 exit status，只憑單筆 JUnit testcase 判定。子行程以 exit 1／3 結束
（例如 conftest live guard 以 ``session.exitstatus=1`` 回報的失敗）而 JUnit 僅有一筆 passed 時，會被誤判為 PASS。
本檔驗證：非零 exit 一律為 failed，即使 JUnit 為 passed／skipped；診斷訊息保留 exit code 與 stderr 尾段。
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

CONTRACT_DIR = Path(__file__).resolve().parent / "contract_blackbox"

pytestmark = [pytest.mark.posix_only, pytest.mark.process_rpc]

# stub 子行程：依 STUB_OUTCOME 寫出單筆 testcase 的 JUnit，輸出標記後以 STUB_RC 結束。
_STUB_CHILD = """\
import os
import sys
from pathlib import Path

args = sys.argv[1:]
junit = next(arg.split("=", 1)[1] for arg in args if arg.startswith("--junitxml="))
case = {
    "passed": '<testcase classname="stub" name="case"/>',
    "skipped": '<testcase classname="stub" name="case"><skipped message="stub-skip"/></testcase>',
    "failure": '<testcase classname="stub" name="case"><failure message="stub-failure"/></testcase>',
}[os.environ["STUB_OUTCOME"]]
Path(junit).write_text(
    '<?xml version="1.0"?><testsuites><testsuite name="stub" tests="1">' + case + "</testsuite></testsuites>",
    encoding="utf-8",
)
sys.stdout.write("STUB_STDOUT_MARKER\\n")
sys.stdout.flush()
sys.stderr.write("STUB_STDERR_MARKER\\n")
sys.stderr.flush()
raise SystemExit(int(os.environ["STUB_RC"]))
"""


def _bb_l15():
    if str(CONTRACT_DIR) not in sys.path:
        sys.path.insert(0, str(CONTRACT_DIR))
    import bb_l15

    return bb_l15


@pytest.fixture
def stub_child(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    bb = _bb_l15()
    stub = tmp_path / "stub_child.py"
    stub.write_text(_STUB_CHILD, encoding="utf-8")
    # 以 stub 取代 unshare 前綴；子行程仍為真實 subprocess，exit status 由 stub 決定。
    monkeypatch.setattr(bb, "unshare_pidns_argv", lambda: [sys.executable, str(stub)])
    monkeypatch.delenv("SW_AB_PIDLOG", raising=False)
    monkeypatch.delenv("SW_RESOURCE_REPORT", raising=False)
    return stub


@pytest.mark.parametrize(
    ("junit_outcome", "rc", "expected"),
    [
        ("passed", 0, "passed"),
        ("passed", 1, "failed"),
        ("passed", 3, "failed"),
        ("skipped", 0, "skipped"),
        ("skipped", 3, "failed"),
        ("failure", 1, "failed"),
    ],
)
def test_child_exit_status_is_authoritative(
    stub_child: Path,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    junit_outcome: str,
    rc: int,
    expected: str,
) -> None:
    bb = _bb_l15()
    monkeypatch.setenv("STUB_OUTCOME", junit_outcome)
    monkeypatch.setenv("STUB_RC", str(rc))
    outcome, message = bb.run_nodeid_in_pidns("tests/stub_child.py::test_case", tmp_path)
    assert outcome == expected, message
    if rc != 0:
        assert f"rc={rc}" in message, message
        assert "STUB_STDERR_MARKER" in message, message


def test_zero_exit_with_passed_junit_is_passed(stub_child: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """正向對照：exit 0 且 JUnit 單筆 passed 仍為 passed，確認修正未把所有結果都改成 failed。"""
    bb = _bb_l15()
    monkeypatch.setenv("STUB_OUTCOME", "passed")
    monkeypatch.setenv("STUB_RC", "0")
    assert bb.run_nodeid_in_pidns("tests/stub_child.py::test_case", tmp_path) == ("passed", "")


def test_missing_namespace_capability_stays_unavailable(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """能力不足仍回 unavailable（呼叫端 skip，絕不視為 PASS），不受 exit status 修正影響。"""
    bb = _bb_l15()
    monkeypatch.setattr(bb, "unshare_pidns_argv", lambda: None)
    outcome, message = bb.run_nodeid_in_pidns("tests/stub_child.py::test_case", tmp_path)
    assert outcome == "unavailable", message
