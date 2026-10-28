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


# ---------------------------------------------------------------------------
# OD-39（2026-10-28）：runner 擴充承接 slice21-l15-gaps.md #1／#3／#4／#5／#8／#9（不改 bridge 工具樹）
# ---------------------------------------------------------------------------


def _write_wal(path: pathlib.Path, rows: list[dict]) -> None:
    import json

    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("".join(json.dumps(r) + "\n" for r in rows), encoding="utf-8")


def test_pre_daemon_steps_loaded_and_run_before_daemon(tmp_path, monkeypatch):
    """gaps #1／#3／#8：情境 `pre_daemon_steps` 於 DUT／bridge 就緒後、daemon 啟動前執行；只接受不需 daemon 的動作。"""
    _ensure_func_test_path()
    from lib import daemon_harness, yaml_loader

    scen = tmp_path / "x.yaml"
    scen.write_text("meta: {name: x}\npre_daemon_steps:\n  - {action: sleep, seconds: 0}\nsteps: []\n", encoding="utf-8")
    tc = yaml_loader.load_test_case(scen)
    assert tc.pre_daemon_steps == [{"action": "sleep", "seconds": 0}]

    order: list[str] = []
    cfg = daemon_harness.HarnessConfig(target_config=yaml_loader_target(), mode="pty", evidence_dir=tmp_path / "ev")
    h = daemon_harness.DaemonHarness(cfg)
    monkeypatch.setattr(h, "_start_fake_target", lambda: order.append("dut"))
    monkeypatch.setattr(h, "wait_endpoint", lambda timeout_s=5.0: order.append("endpoint"))

    class _P:
        def __init__(self, *a, **k):
            order.append("daemon")

        def poll(self):
            return 0

    monkeypatch.setattr(daemon_harness.subprocess, "Popen", _P)
    monkeypatch.setattr(h, "stop", lambda: None)
    h.start(before_daemon=lambda: order.append("pre"))
    assert order[:3] == ["dut", "pre", "daemon"], order

    runner_mod = _load_runner()
    fh = _FakeHarness(tmp_path)
    fh.bridge = None    # op_begin 不取 bridge 快照
    tr = _make_runner(runner_mod, fh, tmp_path / "reports")
    tr._run_pre_daemon_steps([{"action": "sleep", "seconds": 0}, {"action": "op_begin", "op_id": "w", "settle": False}])
    with pytest.raises(runner_mod.StepError) as exc:
        tr._run_pre_daemon_steps([{"action": "cli", "argv": ["session", "list"]}])
    assert "pre_daemon_steps" in str(exc.value)


def yaml_loader_target():
    _ensure_func_test_path()
    from lib.target_config import TargetConfig

    return TargetConfig.from_yaml({})


def test_assert_when_runs_matching_branch(tmp_path):
    """gaps #4：條件式斷言——依已存變數選分支執行；分支內斷言失敗即失敗。"""
    runner_mod = _load_runner()
    tr = _make_runner(runner_mod, _FakeHarness(tmp_path), tmp_path / "reports")
    tr.variables["big"] = {"error_code": "COMMAND_ABORTED_IN_FLIGHT"}
    ran: list[str] = []
    tr._step_sleep = lambda step, idx: ran.append(str(step.get("tag")))  # type: ignore[method-assign]
    step = {"action": "assert_when", "value": "{big.error_code}", "in": ["COMMAND_ABORTED_IN_FLIGHT"],
            "then": [{"action": "sleep", "tag": "then"}], "else": [{"action": "sleep", "tag": "else"}]}
    tr._exec_step(step, 0)
    tr.variables["big"] = {"error_code": None}
    tr._exec_step(step, 1)
    assert ran == ["then", "else"]
    with pytest.raises(runner_mod.StepError):
        tr._exec_step({"action": "assert_when", "value": "{big.error_code}", "equals": None,
                       "then": [{"action": "assert_any", "of": []}]}, 2)


def test_assert_wal_tx_total_sums_cmd_tx_bytes(tmp_path):
    """gaps #4：聚合斷言——同一 cmd_id 的 TX payload 長度總和（含歸檔 WAL）與門檻比較。"""
    import base64

    runner_mod = _load_runner()
    h = _FakeHarness(tmp_path)
    b64 = lambda b: base64.b64encode(b).decode()  # noqa: E731
    _write_wal(h.wal_dir() / "raw.wal.ndjson.20261028-000000", [{"dir": "TX", "cmd_id": "c1", "payload_b64": b64(b"x" * 100)}])
    _write_wal(h.wal_dir() / "raw.wal.ndjson", [{"dir": "TX", "cmd_id": "c1", "payload_b64": b64(b"y" * 50)},
                                                  {"dir": "TX", "cmd_id": "c2", "payload_b64": b64(b"z" * 999)},
                                                  {"dir": "RX", "cmd_id": "c1", "payload_b64": b64(b"r" * 999)}])
    tr = _make_runner(runner_mod, h, tmp_path / "reports")
    tr.variables["big"] = {"cmd_id": "c1"}
    out = tr._exec_step({"action": "assert_wal_tx_total", "cmd_id": "{big.cmd_id}", "lt": 151}, 0)
    with pytest.raises(runner_mod.StepError):
        tr._exec_step({"action": "assert_wal_tx_total", "cmd_id": "{big.cmd_id}", "lt": 150}, 1)
    with pytest.raises(runner_mod.StepError):
        tr._exec_step({"action": "assert_wal_tx_total", "cmd_id": "{big.cmd_id}"}, 2)   # 未給比較子


def test_assert_any_passes_if_one_alternative_passes(tmp_path):
    """gaps #5：OR 斷言——任一替代分支全部通過即通過；全部失敗則列出各分支錯誤。只接受無副作用的動作。"""
    runner_mod = _load_runner()
    tr = _make_runner(runner_mod, _FakeHarness(tmp_path), tmp_path / "reports")
    tr.variables["rb"] = {"recovery_action": "NONE", "status": "done"}
    ok = {"action": "assert_any", "of": [
        [{"action": "assert_when", "value": "{rb.recovery_action}", "equals": "EXPECT_REBOOT", "then": [], "else": [{"action": "assert_any", "of": []}]}],
        [{"action": "assert_when", "value": "{rb.status}", "equals": "done", "then": [], "else": [{"action": "assert_any", "of": []}]}],
    ]}
    tr._exec_step(ok, 0)
    bad = {"action": "assert_any", "of": [
        [{"action": "assert_when", "value": "{rb.status}", "equals": "error", "then": [], "else": [{"action": "assert_any", "of": []}]}]]}
    with pytest.raises(runner_mod.StepError):
        tr._exec_step(bad, 1)
    with pytest.raises(runner_mod.StepError) as exc:
        tr._exec_step({"action": "assert_any", "of": [[{"action": "cli", "argv": ["x"]}]]}, 2)
    assert "assert_any" in str(exc.value)


def test_query_dut_generation_reads_wire_records(tmp_path):
    """gaps #5：DUT generation 查詢——取 wire.ndjson 的最大 dut_generation，可接 expect。"""
    import json

    runner_mod = _load_runner()
    tr = _make_runner(runner_mod, _FakeHarness(tmp_path), tmp_path / "reports")
    wire = tr._evidence_dir_path() / "wire.ndjson"
    wire.parent.mkdir(parents=True, exist_ok=True)
    wire.write_text("".join(json.dumps({"seq": i, "dut_generation": g}) + "\n" for i, g in enumerate([1, 1, 2])), encoding="utf-8")
    out = tr._exec_step({"action": "query_dut_generation", "save_as": "gen", "expect": {"dut_generation": 2}}, 0)
    assert tr.variables["gen"]["dut_generation"] == 2
    with pytest.raises(runner_mod.StepError):
        tr._exec_step({"action": "query_dut_generation", "expect": {"dut_generation": 3}}, 1)


def test_assert_wal_window_forbids_records_inside_op_window(tmp_path):
    """gaps #9：以 WAL 紀錄判定 op 窗口內的狀態序列（兩個 daemon 皆無狀態轉移 META；以「窗口內該 COM 無 RX／TX」
    證明 daemon 未開埠進入 ATTACHING／READY）。窗口以 op_begin／op_end 的 CLOCK_MONOTONIC 對 WAL mono_ts_ns。"""
    runner_mod = _load_runner()
    h = _FakeHarness(tmp_path)
    h.bridge = None
    tr = _make_runner(runner_mod, h, tmp_path / "reports")
    tr._exec_step({"action": "op_begin", "op_id": "win", "settle": False}, 0)
    start = tr._ops["win"]["start_mono_ns"]
    tr._exec_step({"action": "op_end", "op_id": "win", "settle": False}, 1)
    end = tr._ops["win"]["end_mono_ns"]
    _write_wal(h.wal_dir() / "raw.wal.ndjson", [
        {"com": "COM0", "dir": "TX", "mono_ts_ns": start - 1},
        {"com": "COM1", "dir": "TX", "mono_ts_ns": start + 1},
        {"com": "COM0", "dir": "META", "mono_ts_ns": start + 1},
    ])
    tr._exec_step({"action": "assert_wal_window", "op_id": "win", "com": "COM0", "forbid_dirs": ["RX", "TX"]}, 2)
    _write_wal(h.wal_dir() / "raw.wal.ndjson", [{"com": "COM0", "dir": "RX", "mono_ts_ns": (start + end) // 2}])
    with pytest.raises(runner_mod.StepError):
        tr._exec_step({"action": "assert_wal_window", "op_id": "win", "com": "COM0", "forbid_dirs": ["RX", "TX"]}, 3)
    with pytest.raises(runner_mod.StepError):
        tr._exec_step({"action": "assert_wal_window", "op_id": "nosuch", "com": "COM0", "forbid_dirs": ["TX"]}, 4)


def test_assert_value_and_tx_total_save_as(tmp_path):
    """gaps #4：`assert_value` 比對已存變數（equals／in／matches）；`assert_wal_tx_total` 可 save_as 供條件式使用。"""
    runner_mod = _load_runner()
    h = _FakeHarness(tmp_path)
    _write_wal(h.wal_dir() / "raw.wal.ndjson", [])
    tr = _make_runner(runner_mod, h, tmp_path / "reports")
    tr.variables["r"] = {"code": "X", "n": 0}
    tr._exec_step({"action": "assert_value", "value": "{r.code}", "in": ["X", "Y"]}, 0)
    with pytest.raises(runner_mod.StepError):
        tr._exec_step({"action": "assert_value", "value": "{r.code}", "equals": "Y"}, 1)
    tr._exec_step({"action": "assert_wal_tx_total", "cmd_id": "c9", "eq": 0, "save_as": "t"}, 2)
    assert tr.variables["t"]["tx_bytes"] == 0


def test_assert_wire_offsets_requires_settled_snapshot(tmp_path):
    """gaps #1／#3：窗口自 daemon 啟動前起算時，以 settled 快照的絕對 wire offset 判定零 TX（daemon 不存在時
    serial_read 必為 0）；快照未 settled 即 INSUFFICIENT_EVIDENCE，不得視為 PASS。"""
    runner_mod = _load_runner()
    h = _FakeHarness(tmp_path)
    snaps = [{"settled": True, "wire": {"offsets": {"serial_read": 0, "serial_write": 9}}},
             {"settled": True, "wire": {"offsets": {"serial_read": 4, "serial_write": 9}}},
             {"settled": False, "wire": {"offsets": {"serial_read": 0}}}]
    h.bridge.snapshot = lambda settle: snaps.pop(0)  # type: ignore[attr-defined]
    tr = _make_runner(runner_mod, h, tmp_path / "reports")
    step = {"action": "assert_wire_offsets", "observation": "serial_read", "equals": 0}
    tr._exec_step(step, 0)
    with pytest.raises(runner_mod.StepError):
        tr._exec_step(step, 1)
    with pytest.raises(runner_mod.StepError) as exc:
        tr._exec_step(step, 2)
    assert "INSUFFICIENT_EVIDENCE" in str(exc.value)
