"""§15.7.1 情境的 Python 基線 smoke 與 PTY 替身交接測試（P2-T11，T11-1）。

依據：`docs/rust-refactor/plans/03-stage0-test-infra.md` Task 11（§15.7.1、§11.6 Slice 2.1、R0-F3）。
範圍：
1. `func-test/runner.py --mode bridge --scenarios` 對 Python 基線執行 `l15-reject-zero-tx` 與 `l15-release-inflight`；
2. `hold_endpoint`／`release_endpoint` 以真實 PTY 驗證 TIOCEXCL 於 release 後被清除（R0-F3）。

本檔需要 PTY 與子行程，故依 T14 規則以模組層 `posix_only`＋`posix_pty`＋`process_rpc` 標註（不得在子層再加平台 marker）。
PTY 模組於函式內延遲 import，避免 Windows 收集時失敗。
紅燈預期：runner 尚無 `--mode`／`--scenarios` 與情境檔、無 `hold_endpoint`，故失敗原因為缺少行為。
"""
from __future__ import annotations

import pathlib
import subprocess
import sys

import pytest

pytestmark = [pytest.mark.posix_only, pytest.mark.posix_pty, pytest.mark.process_rpc]

REPO_ROOT = pathlib.Path(__file__).resolve().parents[1]
FUNC_TEST = REPO_ROOT / "func-test"
RUNNER = FUNC_TEST / "runner.py"
SMOKE_TIMEOUT_S = 300


def _run_bridge_case(case_id: str) -> subprocess.CompletedProcess:
    """以子行程執行 bridge 模式的單一情境（隔離目錄由 runner 自行建立於暫存區）。"""
    return subprocess.run(
        [sys.executable, str(RUNNER), "--mode", "bridge", "--scenarios", "--case", case_id],
        cwd=str(REPO_ROOT),
        capture_output=True,
        text=True,
        timeout=SMOKE_TIMEOUT_S,
        check=False,
    )


def test_reject_zero_tx_runs_on_python():
    """l15-reject-zero-tx 對修正後 Python 基線執行，exit 0（含換行與超長命令被拒時 TX 皆為零）。"""
    proc = _run_bridge_case("l15-reject-zero-tx")
    assert proc.returncode == 0, f"exit={proc.returncode}\n{proc.stdout[-2000:]}\n{proc.stderr[-2000:]}"


def test_release_inflight_runs_on_python():
    """l15-release-inflight 結果為 PASS；P1 T08 未合併時為 dependency_blocked:SC-04（不得 PASS 以外的假綠或 SKIP）。"""
    proc = _run_bridge_case("l15-release-inflight")
    text = proc.stdout + proc.stderr
    verdict_lines = [ln for ln in text.splitlines() if "l15-release-inflight" in ln]
    assert verdict_lines, f"輸出中沒有 l15-release-inflight 的判定行\n{text[-2000:]}"
    verdict = "\n".join(verdict_lines)
    assert proc.returncode == 0, f"exit={proc.returncode}\n{text[-2000:]}"
    assert ("PASS" in verdict) or ("dependency_blocked:SC-04" in verdict), f"判定不是 PASS 或 dependency_blocked:SC-04：{verdict}"
    assert "SKIP" not in verdict, f"不得以 SKIP 結案：{verdict}"


# ---------------------------------------------------------------------------
# PTY 替身：hold_endpoint／release_endpoint 的 TIOCEXCL 語意（R0-F3）
# ---------------------------------------------------------------------------


def _load_runner():
    import importlib.util

    spec = importlib.util.spec_from_file_location("l15_smoke_runner_under_test", RUNNER)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


class _FakeBridge:
    """BridgeSession 的最小替身：hold_endpoint 只需 slave_path（經 describe() 取得）。"""

    def __init__(self, evidence_dir, wal_dir, *, slave_path):
        self.evidence_dir = pathlib.Path(evidence_dir)
        self.wal_dir = pathlib.Path(wal_dir)
        self.slave_path = slave_path

    def describe(self) -> dict:
        return {"slave_path": self.slave_path}


class _FakeHarness:
    def __init__(self, root, *, bridge):
        self.root = pathlib.Path(root)
        self.bridge = bridge
        self.evidence_dir = bridge.evidence_dir
        self.socket_path = str(self.root / "run" / "serialwrapd.sock")
        self.env: dict[str, str] = {}

    def wal_dir(self) -> pathlib.Path:
        return self.bridge.wal_dir


def test_release_endpoint_clears_tiocexcl(tmp_path):
    """測試持有 PTY master；hold_endpoint{excl} 成功後一般 open 得 EBUSY；release_endpoint 後一般 open 必須成功。

    root 下 TIOCEXCL 不生效，無法判定 R0-F3，依規格記為證據不足（skip），不計 PASS。
    """
    import errno
    import os
    import pty

    if os.geteuid() == 0:
        pytest.skip("證據不足：root 下 TIOCEXCL 不生效，須以非 root 實測 R0-F3")

    runner_mod = _load_runner()
    master, slave_fd = pty.openpty()
    slave_path = os.ttyname(slave_fd)
    os.close(slave_fd)
    tr = None
    try:
        fake = _FakeBridge(tmp_path / "evidence", tmp_path / "wal", slave_path=slave_path)
        harness = _FakeHarness(tmp_path, bridge=fake)
        tc = runner_mod.TestCase(
            name="l15-tiocexcl-unit",
            category="l15-scenario",
            severity="critical",
            description="PTY 替身單元測試",
            steps=[],
        )
        tc.bridge = {"applicable": True, "only": True, "compare": "full"}
        tr = runner_mod.TestRunner(tc)
        tr.harness = harness

        tr._exec_step({"action": "hold_endpoint", "mode": "excl", "seconds": 60, "save_as": "h"}, 0)
        assert tr.variables["h"]["opened"] is True, f"hold_endpoint 未取得端點：{tr.variables.get('h')}"

        with pytest.raises(OSError) as blocked:
            os.close(os.open(slave_path, os.O_RDWR | os.O_NOCTTY))
        assert blocked.value.errno == errno.EBUSY, "hold_endpoint{excl} 應使一般 open 得 EBUSY"

        tr._exec_step({"action": "release_endpoint", "from": "h"}, 0)
        fd = os.open(slave_path, os.O_RDWR | os.O_NOCTTY)
        os.close(fd)
    finally:
        if tr is not None:
            try:
                tr._exec_step({"action": "release_endpoint", "from": "h"}, 0)
            except Exception:  # noqa: BLE001 - 已釋放或未成功持有時忽略
                pass
        os.close(master)
