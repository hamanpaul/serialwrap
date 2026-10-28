"""P5 Task 21a：P2 runner 補動作的 TDD 紅燈測試（測試資產變更，§10.4）。

依據：`docs/rust-refactor/plans/06-slice21-daemon-core.md` Task 21a（§10.4、§15.3、§15.7.1、R0-F3）。
範圍：`func-test/runner.py` 動作表與五個新動作的語意：
- `external_open`／`external_close`：外部程式以 flock＋TIOCEXCL 持有 PTY slave，釋放後一般 open 必須成功；
- `measure_rpc_latency`：對 Python 基線 daemon 量測 RPC 往返樣本；
- `measure_event_latency`：本檔只驗證動作表登記（語意由 Task 21 情境消費）；
- `wait_cli_until`：輪詢 CLI 條件，逾時拋出 StepError。
紅燈預期：`func-test/runner.py` 尚無上述五個動作，`TestRunner._exec_step` 以「未知 action」StepError 拒絕，
失敗原因為缺少行為，而非語法或收集錯誤。

約定（測試作者決定，實作者依此對齊）：
- 五個動作皆於 `mode="bridge"` 的 TestRunner 上執行（與 P2 bridge-only 動作一致）；測試以 `report_dir` 指向暫存區，不寫入使用者的 `~/b-log`。
- `external_open`／`external_close` 的結果寫入 `variables[save_as]`，形如 `{"opened": bool, "errno": int|None, "pid": int|None}`；`external_close` 以 `from` 指定 save_as。
- `measure_rpc_latency` 樣本寫入 `comparison["latency_samples_ms"][save_as]`（毫秒數值列表）。
- `wait_cli_until` 的 `json_path` 採點分隔路徑、列表以數字索引（如 `sessions.0.state`）；逾時拋出 StepError，訊息含 `wait_cli_until` 與「逾時」。
- PTY／flock 相關測試於 Windows skip；root 下 TIOCEXCL 不生效，對應測試依 Task 21a 規則 skip 並在報告列為「證據不足」。
- 所有平台相依 import 皆於函式內延遲執行，確保收集階段不因平台而失敗。
- 本檔不觸及正式 daemon、`~/.config/serialwrap` 或 `~/.local/state/serialwrap`：外層 `SERIALWRAP_*` 一律移除並指向暫存目錄。
"""
from __future__ import annotations

import errno
import importlib.util
import os
import pathlib
import sys
import time

import pytest

REPO_ROOT = pathlib.Path(__file__).resolve().parents[2]
FUNC_TEST = REPO_ROOT / "func-test"
RUNNER = FUNC_TEST / "runner.py"
S21_ACTIONS = (
    "external_open",
    "external_close",
    "measure_rpc_latency",
    "measure_event_latency",
    "wait_cli_until",
)
SAMPLE_COUNT = 10
ISO_SUBDIRS = {
    "SERIALWRAP_STATE_DIR": "state",
    "SERIALWRAP_RUN_DIR": "run",
    "SERIALWRAP_WAL_DIR": "wal",
    "SERIALWRAP_CONFIG_DIR": "config",
    "SERIALWRAP_BY_ID_DIR": "by-id",
    "SERIALWRAP_BY_PATH_DIR": "by-path",
}


@pytest.fixture(autouse=True)
def _isolated_serialwrap_env(tmp_path, monkeypatch):
    """移除外層 SERIALWRAP_*（含 CLI／daemon 覆寫），並把各目錄指向本測試的暫存區。"""
    for key in [k for k in os.environ if k.startswith("SERIALWRAP_")]:
        monkeypatch.delenv(key, raising=False)
    for key, sub in ISO_SUBDIRS.items():
        monkeypatch.setenv(key, str(tmp_path / "sw-env" / sub))


# ---------------------------------------------------------------------------
# 共用 helper（延遲 import；不開真實裝置）
# ---------------------------------------------------------------------------


def _load_runner():
    spec = importlib.util.spec_from_file_location("s21_actions_runner_under_test", RUNNER)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _ensure_func_test_path() -> None:
    if str(FUNC_TEST) not in sys.path:
        sys.path.insert(0, str(FUNC_TEST))


class _FakeBridge:
    """BridgeSession 的最小替身：外部持有動作以 describe() 取得 PTY slave 路徑。"""

    def __init__(self, slave_path):
        self.slave_path = slave_path

    def describe(self) -> dict:
        return {"slave_path": self.slave_path}


class _FakeHarness:
    """DaemonHarness 的最小替身：提供 root、endpoint、env、bridge 與 wal_dir()。"""

    def __init__(self, root, *, slave_path=None):
        self.root = pathlib.Path(root)
        self.bridge = _FakeBridge(slave_path)
        self.endpoint = str(self.root / "run" / "serialwrapd.sock")
        self.socket_path = self.endpoint
        self.env: dict[str, str] = {}

    def wal_dir(self) -> pathlib.Path:
        return self.root / "wal"


def _make_runner(runner_mod, harness, report_root):
    tc = runner_mod.TestCase(
        name="s21-actions-unit",
        category="s21-unit",
        severity="critical",
        description="P5 Task 21a 單元測試替身",
        steps=[],
    )
    tc.bridge = {"applicable": True, "only": True, "compare": "none"}
    tr = runner_mod.TestRunner(tc, mode="bridge", report_dir=report_root)
    tr.harness = harness
    return tr


def _open_pty_pair():
    """建立 PTY：測試持有 master，回傳 (master_fd, slave_path)；slave 路徑交給 runner 當作受測端點。"""
    import pty

    master, slave_fd = pty.openpty()
    slave_path = os.ttyname(slave_fd)
    os.close(slave_fd)
    return master, slave_path


def _assert_probe_blocked(slave_path) -> None:
    """以一般 open＋flock(LOCK_EX|LOCK_NB) 探測：非 root 下 TIOCEXCL 會先讓 open 得 EBUSY；root 下由 flock 擋住。"""
    import fcntl

    try:
        fd = os.open(slave_path, os.O_RDWR | os.O_NOCTTY | os.O_NONBLOCK)
    except OSError as exc:
        assert exc.errno == errno.EBUSY, f"open 被擋但 errno 非 EBUSY：{exc}"
        return
    try:
        with pytest.raises(OSError) as blocked:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        assert blocked.value.errno in (errno.EWOULDBLOCK, errno.EAGAIN), (
            f"flock 被擋但 errno 非 EWOULDBLOCK：{blocked.value}"
        )
    finally:
        os.close(fd)


def _probe_free(slave_path) -> bool:
    """一般 open＋flock(LOCK_EX|LOCK_NB) 皆成功才算端點可用。"""
    import fcntl

    try:
        fd = os.open(slave_path, os.O_RDWR | os.O_NOCTTY | os.O_NONBLOCK)
    except OSError:
        return False
    try:
        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        fcntl.flock(fd, fcntl.LOCK_UN)
        return True
    except OSError:
        return False
    finally:
        os.close(fd)


def _release_quietly(tr, key: str) -> None:
    """測試收尾：先正式以 external_close 釋放；失敗時對記錄的子行程 pid 送 SIGTERM，避免替身遺留獨占。"""
    import signal

    if tr is None:
        return
    try:
        tr._exec_step({"action": "external_close", "from": key}, 0)
    except Exception:  # noqa: BLE001 - 收尾階段只做盡力釋放
        pid = (tr.variables.get(key) or {}).get("pid")
        if isinstance(pid, int):
            try:
                os.kill(pid, signal.SIGTERM)
            except OSError:
                pass


# ---------------------------------------------------------------------------
# 動作表
# ---------------------------------------------------------------------------


def test_action_table_contains_s21_actions(tmp_path):
    """runner 動作表含本 Task 的五個新動作（以未知動作訊息判定缺漏；不帶 harness 以免副作用）。"""
    runner_mod = _load_runner()
    missing: list[str] = []
    for name in S21_ACTIONS:
        tr = _make_runner(runner_mod, None, tmp_path / "reports")
        try:
            tr._exec_step({"action": name}, 0)
        except Exception as exc:  # noqa: BLE001 - 只關心是否被識別為動作
            if "未知 action" in str(exc):
                missing.append(name)
    assert missing == [], f"runner 動作表缺少：{missing}"


# ---------------------------------------------------------------------------
# external_open／external_close：外部持有與 TIOCEXCL 清除（R0-F3）
# ---------------------------------------------------------------------------


@pytest.mark.skipif(os.name == "nt", reason="PTY 與 flock 僅 POSIX")
def test_external_open_flock_tiocexcl_blocks_probe(tmp_path):
    """external_open hold=flock_tiocexcl 成功後，同一路徑的 open_exclusive 等價探測（flock LOCK_EX|LOCK_NB）失敗。"""
    runner_mod = _load_runner()
    master, slave_path = _open_pty_pair()
    tr = None
    try:
        tr = _make_runner(runner_mod, _FakeHarness(tmp_path, slave_path=slave_path), tmp_path / "reports")
        tr._exec_step(
            {"action": "external_open", "target": "session_slave", "hold": "flock_tiocexcl", "save_as": "ext"},
            0,
        )
        held = tr.variables.get("ext") or {}
        assert held.get("opened") is True, f"external_open 未取得端點：{held}"
        assert held.get("errno") is None, f"成功開啟時 errno 應為 None：{held}"
        _assert_probe_blocked(slave_path)
    finally:
        _release_quietly(tr, "ext")
        os.close(master)


@pytest.mark.skipif(os.name == "nt", reason="PTY 與 flock 僅 POSIX")
def test_external_close_restores_probe(tmp_path):
    """external_close 後，同一路徑的一般 open 與 flock(LOCK_EX|LOCK_NB) 皆成功。"""
    runner_mod = _load_runner()
    master, slave_path = _open_pty_pair()
    tr = None
    try:
        tr = _make_runner(runner_mod, _FakeHarness(tmp_path, slave_path=slave_path), tmp_path / "reports")
        tr._exec_step(
            {"action": "external_open", "target": "session_slave", "hold": "flock_tiocexcl", "save_as": "ext"},
            0,
        )
        assert (tr.variables.get("ext") or {}).get("opened") is True, "前置條件失敗：external_open 未持有端點"
        tr._exec_step({"action": "external_close", "from": "ext"}, 0)
        assert _probe_free(slave_path), "external_close 後一般 open＋flock 仍被擋"
    finally:
        _release_quietly(tr, "ext")
        os.close(master)


@pytest.mark.skipif(os.name == "nt", reason="PTY 與 TIOCEXCL 僅 POSIX")
def test_external_close_clears_tiocexcl_with_master_open(tmp_path):
    """測試持有 PTY master；external_open hold=flock_tiocexcl 後一般 os.open(slave) 得 EBUSY；external_close 後一般 os.open(slave) 必須成功。"""
    if hasattr(os, "geteuid") and os.geteuid() == 0:
        pytest.skip("證據不足：root 下 TIOCEXCL 不生效，須以非 root 實測 R0-F3")

    runner_mod = _load_runner()
    master, slave_path = _open_pty_pair()
    tr = None
    try:
        tr = _make_runner(runner_mod, _FakeHarness(tmp_path, slave_path=slave_path), tmp_path / "reports")
        tr._exec_step(
            {"action": "external_open", "target": "session_slave", "hold": "flock_tiocexcl", "save_as": "ext"},
            0,
        )
        assert (tr.variables.get("ext") or {}).get("opened") is True, "前置條件失敗：external_open 未持有端點"

        with pytest.raises(OSError) as blocked:
            fd = os.open(slave_path, os.O_RDWR | os.O_NOCTTY)
            os.close(fd)
        assert blocked.value.errno == errno.EBUSY, "external_open{flock_tiocexcl} 應使一般 open 得 EBUSY"

        tr._exec_step({"action": "external_close", "from": "ext"}, 0)
        fd = os.open(slave_path, os.O_RDWR | os.O_NOCTTY)  # 不得 EBUSY
        os.close(fd)
    finally:
        _release_quietly(tr, "ext")
        os.close(master)


# ---------------------------------------------------------------------------
# measure_rpc_latency：對 Python 基線 daemon 量測樣本
# ---------------------------------------------------------------------------


@pytest.mark.skipif(os.name == "nt", reason="DaemonHarness pty 模式僅 POSIX（FakeTarget 需 PTY）")
def test_measure_rpc_latency_python_baseline_samples(tmp_path):
    """measure_rpc_latency samples=10 對 Python 基線 daemon 產生 10 個正數樣本，寫入 comparison.latency_samples_ms["lat"]。"""
    runner_mod = _load_runner()
    _ensure_func_test_path()
    from lib.daemon_harness import DaemonHarness, HarnessConfig

    harness = DaemonHarness(HarnessConfig(mode="pty"))
    harness.start()
    try:
        tr = _make_runner(runner_mod, harness, tmp_path / "reports")
        tr._exec_step(
            {"action": "measure_rpc_latency", "method": "session.list", "samples": SAMPLE_COUNT, "save_as": "lat"},
            0,
        )
        samples = ((tr.comparison or {}).get("latency_samples_ms") or {}).get("lat")
        assert isinstance(samples, list), f"未寫入 comparison.latency_samples_ms['lat']：{tr.comparison}"
        assert len(samples) == SAMPLE_COUNT, f"樣本數應為 {SAMPLE_COUNT}，實際 {len(samples)}"
        assert all(isinstance(x, (int, float)) and not isinstance(x, bool) and x > 0 for x in samples), (
            f"樣本需為正數毫秒：{samples}"
        )
    finally:
        harness.stop()


# ---------------------------------------------------------------------------
# wait_cli_until：條件輪詢與逾時
# ---------------------------------------------------------------------------


def test_wait_cli_until_timeout_raises_step_error(tmp_path, monkeypatch):
    """wait_cli_until 條件永不成立時於 timeout_s 後拋出 StepError（訊息含 wait_cli_until 與「逾時」）。"""
    runner_mod = _load_runner()

    def fake_cli(argv, **kwargs):
        return {"ok": True, "sessions": [{"com": "COM0", "state": "ATTACHED"}]}

    monkeypatch.setattr(runner_mod, "cli_run", fake_cli)
    tr = _make_runner(runner_mod, _FakeHarness(tmp_path), tmp_path / "reports")
    step = {
        "action": "wait_cli_until",
        "argv": ["session", "list"],
        "json_path": "sessions.0.state",
        "equals": "READY",
        "timeout_s": 0.5,
    }
    started = time.monotonic()
    with pytest.raises(runner_mod.StepError) as exc:
        tr._exec_step(step, 0)
    elapsed = time.monotonic() - started
    text = str(exc.value)
    assert "未知 action" not in text, f"動作尚未實作：{text}"
    assert "wait_cli_until" in text and "逾時" in text, f"逾時訊息不符：{text}"
    assert elapsed >= 0.4, f"未等待至 timeout_s 即回傳（{elapsed:.2f}s）"


def test_expect_list_contains_matches_expanded_scalar():
    """第三輪（gaps #2）：`{list_contains: v}` 在 list 中找與 v 字串相等的元素（`{ext.pid}` 展開為字串、pids 為 int）。"""
    sys.path.insert(0, str(FUNC_TEST / "lib"))
    from expect_engine import check_expect

    resp = {"error_code": "DEVICE_STILL_HELD", "pids": [101, 4242]}
    assert check_expect(resp, {"pids": {"list_contains": "4242"}}) == []
    assert check_expect(resp, {"pids": {"list_contains": 4242}}) == []
    assert len(check_expect(resp, {"pids": {"list_contains": "999"}})) == 1
    assert len(check_expect({"pids": "4242"}, {"pids": {"list_contains": "4242"}})) == 1   # 非 list 不得視為命中
    assert len(check_expect({}, {"pids": {"list_contains": "1"}})) == 1
