"""§15.7.1 情境腳本與協調器新動作的 TDD 紅燈測試（P2-T11，T11-1）。

依據：`docs/rust-refactor/plans/03-stage0-test-infra.md` Task 11（§15.7.1、§11.3、§15.3）。
範圍：情境 YAML 結構、不變量執行者對照、切片覆蓋、runner 新動作的語意（以替身 BridgeSession 與手寫 wire／correlation 證據驅動）。
紅燈預期：情境檔不存在、`func-test/runner.py` 尚無新動作與 `INVARIANT_ENFORCERS`，故失敗原因皆為缺少行為，而非語法或收集錯誤。

約定（測試作者決定，實作者依此對齊）：
- 非 PASS 結果以 `StepError` 表示（FAIL），或例外訊息含 `INSUFFICIENT_EVIDENCE`（INSUFFICIENT）、`cap_missing:<cap>`（SKIP）。
- 情境 `meta.scenario.daemon_slice` 為切片字串（與 `cases.yaml` 一致）。
- `hold_endpoint` 的 POSIX 端點路徑取自 bridge `describe()["slave_path"]`。
- op 快照由 `BridgeSession.snapshot()` 提供，其回傳帶 `peer_open`（即時，Windows 為 None）。

本檔所有測試皆為 memory_only；凡需 PTY 的測試（`test_release_endpoint_clears_tiocexcl`）已依 T14 規則移到 `tests/test_l15_smoke.py`。
所有 import 於函式內延遲執行，確保 `python3 -m pytest -q --collect-only tests/` 可完整收集。
"""
from __future__ import annotations

import hashlib
import json
import pathlib
import re
import sys

import pytest
import yaml

# T14 規則：同檔無資源 marker 的模組層平台 marker；資源 marker 放函式層。
pytestmark = [pytest.mark.core_portable]

REPO_ROOT = pathlib.Path(__file__).resolve().parents[1]
FUNC_TEST = REPO_ROOT / "func-test"
SCEN_DIR = FUNC_TEST / "scenarios"
BRIDGE_SRC = REPO_ROOT / "tools" / "uart_stdio_bridge" / "src"

RUN_ID = "l15-unit-run"
CASE_ID = "l15-unit"
CLOCK_ID = "test-monotonic:l15-unit"
WALL_TS = "2026-10-15T03:06:17+00:00"
BRIDGE_START_SEQ = 1

EXPECTED_L15 = (
    "l15-delayed-reply",
    "l15-half-prompt",
    "l15-dut-silent",
    "l15-slow-reader-backpressure",
    "l15-dut-reboot-port-alive",
    "l15-transport-disconnect",
    "l15-multi-source-contention-cli",
    "l15-multi-source-contention-console",
    "l15-stale-event-delay",
    "l15-wal-interference",
    "l15-reject-zero-tx",
    "l15-release-inflight",
    "l15-exclusive-open",
    "l15-reboot-min-guard",
    "l15-context-switch-deferred-input",
)
EXPECTED_WIN = (
    "win-com-open-exclusive",
    "win-com-txrx-bridge",
    "win-com-cancel-close",
)
SIX_INVARIANTS = frozenset({
    "zero_tx_on_reject",
    "no_forbidden_interleave",
    "no_stale_context_replay",
    "no_reopen_before_handoff",
    "no_auto_resend",
    "insufficient_evidence_not_pass",
})
NEW_ACTIONS = (
    "op_begin",
    "op_end",
    "bridge_control",
    "dut_control",
    "wait_wire_marker",
    "assert_zero_tx",
    "assert_wire_contiguous",
    "assert_compare",
    "wal_control",
    "assert_wal_meta",
    "assert_wal_marker",
    "assert_command_resolved",
    "assert_wire_absent",
    "assert_peer_closed",
    "hold_endpoint",
    "release_endpoint",
)
FORBIDDEN_UART_WORDS = (
    "transport_disconnect",
    "transport_reconnect",
    "dut_restart",
    "pause_pump",
    "dut_control",
    "bridge_control",
)
SYNC_ACTIONS = frozenset({"wait_wire_marker", "wait_state", "wait_command_done", "wait_ready"})
FAULT_ACTIONS = frozenset({"bridge_control", "dut_control", "wal_control"})
REVISION_RE = re.compile(r"^\d{4}-\d{2}-\d{2}\.\d+$")
TERMINAL_STATES = ("done", "error", "canceled", "interactive")


# ---------------------------------------------------------------------------
# 共用helper（皆為延遲 import；不開 socket、子行程、PTY 或裝置）
# ---------------------------------------------------------------------------


def _scenario_files() -> list[pathlib.Path]:
    return sorted(SCEN_DIR.glob("l15-*.yaml")) + sorted(SCEN_DIR.glob("win-com-*.yaml"))


def _yaml(path: pathlib.Path) -> dict:
    return yaml.safe_load(path.read_text(encoding="utf-8"))


def _iter_steps(steps):
    """展開 steps（含 parallel／repeat 等巢狀 `steps` 清單），依出現順序產生每個 step dict。"""
    for step in steps or []:
        if not isinstance(step, dict):
            continue
        yield step
        for value in step.values():
            if isinstance(value, list):
                yield from _iter_steps(value)


def _load_runner():
    import importlib.util

    spec = importlib.util.spec_from_file_location("l15_runner_under_test", FUNC_TEST / "runner.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _bridge_writer_cls():
    src = str(BRIDGE_SRC)
    if src not in sys.path:
        sys.path.insert(0, src)
    from uart_stdio_bridge.evidence.writer import EvidenceWriter

    return EvidenceWriter


def _zero_offsets() -> dict[str, int]:
    return {"serial_read": 0, "dut_stdin_write": 0, "dut_stdout_read": 0, "serial_write": 0}


def _tx_offsets(chunks) -> dict[str, int]:
    total = sum(len(c) for c in chunks)
    offsets = _zero_offsets()
    offsets["serial_read"] = total
    offsets["dut_stdin_write"] = total
    return offsets


def _write_wire(evidence_dir: pathlib.Path, events) -> list[int]:
    """以 Task 2 `EvidenceWriter` 寫 bridge.v1 wire。

    events：("tx", bytes) 同時寫 serial_read 與 dut_stdin_write；("meta", dict) 寫 META。
    回傳與 events 一一對應的「該事件最後一筆 seq」；bridge_start 固定為 BRIDGE_START_SEQ。
    """
    writer_cls = _bridge_writer_cls()
    evidence_dir.mkdir(parents=True, exist_ok=True)
    writer = writer_cls(evidence_dir / "wire.ndjson", run_id=RUN_ID, case_id=CASE_ID, clock_id=CLOCK_ID)
    writer.start()
    seqs: list[int] = []
    try:
        writer.emit_meta(port_id="COM0", endpoint_generation=1, dut_generation=1,
                         meta={"event": "bridge_start"})
        for kind, value in events:
            if kind == "tx":
                writer.emit_data(port_id="COM0", endpoint_generation=1, dut_generation=1,
                                 observation="serial_read", payload=value)
                seq = writer.emit_data(port_id="COM0", endpoint_generation=1, dut_generation=1,
                                       observation="dut_stdin_write", payload=value)
            elif kind == "meta":
                seq = writer.emit_meta(port_id="COM0", endpoint_generation=1, dut_generation=1, meta=value)
            else:
                raise ValueError(f"未知事件種類：{kind}")
            seqs.append(seq)
    finally:
        writer.close()
    (evidence_dir / "control.ndjson").write_bytes(b"")
    return seqs


def _wire_position(path: pathlib.Path) -> tuple[int, dict[str, int]]:
    """由現有 wire.ndjson 計算最後 seq 與各 observation 的 stream 結束位置（供預設快照）。"""
    offsets = _zero_offsets()
    last = 0
    if path.exists():
        for line in path.read_text(encoding="utf-8").splitlines():
            rec = json.loads(line)
            last = max(last, int(rec["seq"]))
            if rec["dir"] in ("TX", "RX"):
                end = int(rec["stream_offset"]) + int(rec["len"])
                offsets[rec["observation"]] = max(offsets[rec["observation"]], end)
    return last, offsets


def _corr(seq: int, *, op_id: str, kind: str, wire_seq: int, offsets: dict[str, int],
          settled: bool = True, wal_seq: int = 0) -> dict:
    """手寫一筆 correlation.v1 紀錄（欄位依 tests/acceptance/correlation.schema.json）。"""
    return {
        "schema_version": "correlation.v1",
        "run_id": RUN_ID,
        "case_id": CASE_ID,
        "seq": seq,
        "mono_ts_ns": seq * 1_000_000,
        "wall_ts": WALL_TS,
        "clock_id": CLOCK_ID,
        "op_id": op_id,
        "step_index": seq,
        "kind": kind,
        "action": "op",
        "argv": None,
        "rc": None,
        "response_sha256": None,
        "cmd_id": None,
        "wal_seq": wal_seq,
        "settled": settled,
        "wire": {
            "endpoint_generation": 1,
            "dut_generation": 1,
            "wire_seq": wire_seq,
            "offsets": dict(offsets),
        },
        "marker": None,
        "marker_dir": None,
    }


def _write_correlation(evidence_dir: pathlib.Path, records: list[dict]) -> None:
    evidence_dir.mkdir(parents=True, exist_ok=True)
    lines = [json.dumps(r, ensure_ascii=False, sort_keys=True, separators=(",", ":")) for r in records]
    (evidence_dir / "correlation.ndjson").write_text("\n".join(lines) + "\n", encoding="utf-8")


class _FakeBridge:
    """BridgeSession 的最小替身，只提供 runner 動作會呼叫的屬性與方法（Task 10 介面）。

    snap：若設定，`snapshot()` 直接回傳此快照（settled／wire_seq／offsets／peer_open）；未設定則由 wire.ndjson 計算。
    events：與比較器 spy 共用的事件序列（`"checkpoint"` 或 `(kind, ...)` tuple）。
    """

    def __init__(self, evidence_dir, wal_dir, *, events=None, slave_path=None, peer_open=False):
        self.evidence_dir = pathlib.Path(evidence_dir)
        self.wal_dir = pathlib.Path(wal_dir)
        self.slave_path = slave_path
        self.peer_open = peer_open
        self.snap: dict | None = None
        self.events = events if events is not None else []
        self.checkpoint_paths: list[pathlib.Path] = []
        self.calls: list[tuple[str, object]] = []

    def describe(self) -> dict:
        return {"slave_path": self.slave_path, "peer_open": self.peer_open}

    def snapshot(self, *, settle: bool = True) -> dict:
        if self.snap is not None:
            snap = dict(self.snap)
        else:
            wire_seq, offsets = _wire_position(self.evidence_dir / "wire.ndjson")
            snap = {"settled": True, "wire_seq": wire_seq, "offsets": offsets, "peer_open": self.peer_open}
        return {
            "settled": bool(snap.get("settled", True)),
            "wal_seq": 0,
            "peer_open": snap.get("peer_open"),
            "wire": {
                "endpoint_generation": 1,
                "dut_generation": 1,
                "wire_seq": int(snap["wire_seq"]),
                "offsets": dict(snap["offsets"]),
            },
        }

    def checkpoint(self) -> pathlib.Path:
        """寫出 checkpoint.v1（wire 前綴長度與 sha256），回傳路徑；與 Task 5／7 的驗證欄位對齊。"""
        self.events.append("checkpoint")
        wire = self.evidence_dir / "wire.ndjson"
        data = wire.read_bytes() if wire.exists() else b""
        last_seq, _ = _wire_position(wire)
        cp_dir = self.evidence_dir / "checkpoints"
        cp_dir.mkdir(parents=True, exist_ok=True)
        cp_id = f"cp-{len(self.checkpoint_paths) + 1}"
        path = cp_dir / f"{cp_id}.json"
        body = {
            "schema_version": "checkpoint.v1",
            "checkpoint_id": cp_id,
            "path": str(path),
            "wire_len": len(data),
            "wire_sha256": hashlib.sha256(data).hexdigest(),
            "wire_seq": last_seq,
            "control_len": 0,
            "control_sha256": hashlib.sha256(b"").hexdigest(),
        }
        path.write_text(json.dumps(body, sort_keys=True, separators=(",", ":")) + "\n", encoding="utf-8")
        self.checkpoint_paths.append(path)
        return path

    def call(self, action: str, params=None) -> dict:
        self.calls.append((action, params))
        return {"ok": True}


class _FakeHarness:
    """DaemonHarness 的最小替身：提供 runner 所需的 socket_path、env、bridge 與 wal_dir()。"""

    def __init__(self, root, *, bridge: _FakeBridge | None = None):
        self.root = pathlib.Path(root)
        self.bridge = bridge
        self.evidence_dir = bridge.evidence_dir if bridge is not None else None
        self.socket_path = str(self.root / "run" / "serialwrapd.sock")
        self.env: dict[str, str] = {}

    def wal_dir(self) -> pathlib.Path:
        assert self.bridge is not None
        return self.bridge.wal_dir


def _make_runner(runner_mod, steps, harness, *, bridge: bool = True):
    tc = runner_mod.TestCase(
        name=CASE_ID,
        category="l15-scenario",
        severity="critical",
        description="單元測試替身",
        steps=list(steps),
    )
    tc.bridge = {"applicable": True, "only": True, "compare": "full"} if bridge else None
    tr = runner_mod.TestRunner(tc)
    tr.harness = harness
    return tr


def _expect(runner_mod, tr, step, outcome: str, label: str) -> None:
    """執行單一 step 並依 outcome 斷言：pass／fail／insufficient／skip（見模組 docstring 約定）。"""
    try:
        tr._exec_step(step, 0)
    except Exception as exc:  # noqa: BLE001 - 於下方依 outcome 分類
        text = str(exc)
        assert "未知 action" not in text, f"{label}: runner 尚未實作動作（紅燈）：{text}"
        if outcome == "pass":
            raise AssertionError(f"{label}: 預期 PASS，實際例外：{text}") from exc
        if outcome == "fail":
            assert isinstance(exc, runner_mod.StepError), (
                f"{label}: 預期 FAIL（StepError），實際 {type(exc).__name__}: {text}"
            )
            assert "INSUFFICIENT_EVIDENCE" not in text, f"{label}: FAIL 不得以 INSUFFICIENT_EVIDENCE 表示：{text}"
        elif outcome == "insufficient":
            assert "INSUFFICIENT_EVIDENCE" in text, f"{label}: 預期 INSUFFICIENT_EVIDENCE，實際 {text}"
        elif outcome == "skip":
            assert "cap_missing:peer_state" in text, f"{label}: 預期 SKIP cap_missing:peer_state，實際 {text}"
        else:
            raise AssertionError(f"未知 outcome {outcome}")
        return
    assert outcome == "pass", f"{label}: 預期 {outcome}，實際 PASS（未拒絕）"


def _patch_cli(monkeypatch, runner_mod, responses: list[dict]) -> list[list[str]]:
    """以序列回應取代 runner 的 cli_run；回應用完後重複最後一筆。回傳每次呼叫的 argv 記錄。"""
    calls: list[list[str]] = []

    def fake_cli(argv, **kwargs):
        calls.append(list(argv))
        idx = min(len(calls), len(responses)) - 1
        return dict(responses[idx])

    monkeypatch.setattr(runner_mod, "cli_run", fake_cli)
    return calls


def _install_comparator_spies(monkeypatch, events: list) -> None:
    """替換比較器的 compare／check_zero_tx，記錄收到的 checkpoint 參數（須於 runner 載入前呼叫）。"""
    src = str(BRIDGE_SRC)
    if src not in sys.path:
        sys.path.insert(0, src)
    from uart_stdio_bridge.compare import comparator

    def spy_compare(inputs):
        events.append(("compare", inputs.checkpoint))
        return {"schema_version": "comparison.v1", "verdict": "PASS"}

    def spy_zero_tx(inputs, op_id):
        events.append(("check_zero_tx", inputs.checkpoint, op_id))
        return {"id": "zero_tx_on_reject", "op_id": op_id, "verdict": "PASS", "detail": ""}

    monkeypatch.setattr(comparator, "compare", spy_compare)
    monkeypatch.setattr(comparator, "check_zero_tx", spy_zero_tx)


def _cli_argv_has(step: dict, *words: str) -> bool:
    if step.get("action") != "cli":
        return False
    argv = [str(a) for a in (step.get("argv") or [])]
    return all(w in argv for w in words)


# ---------------------------------------------------------------------------
# 結構與 metadata
# ---------------------------------------------------------------------------


@pytest.mark.memory_only
def test_scenarios_present():
    """情境檔恰為 §15.7.1 表列的 15 個 L1.5 與 3 個 Windows-native stem。"""
    l15 = sorted(p.stem for p in SCEN_DIR.glob("l15-*.yaml"))
    win = sorted(p.stem for p in SCEN_DIR.glob("win-com-*.yaml"))
    assert l15 == sorted(EXPECTED_L15)
    assert win == sorted(EXPECTED_WIN)


@pytest.mark.memory_only
def test_scenario_meta_complete():
    """每個情境的 meta.scenario 含 revision、seed、capabilities、timeout_s、invariants、expected 且合法。"""
    files = _scenario_files()
    assert len(files) == 18, f"情境檔數量應為 18，實際 {len(files)}"
    for path in files:
        doc = _yaml(path)
        meta = doc["meta"]
        sc = meta["scenario"]
        assert meta["name"] == path.stem, f"{path.stem}: meta.name 必須等於檔名 stem"
        assert REVISION_RE.match(str(sc["revision"])), f"{path.stem}: revision 格式錯誤 {sc['revision']!r}"
        assert isinstance(sc["seed"], int) and not isinstance(sc["seed"], bool), f"{path.stem}: seed 必須為 int"
        assert isinstance(sc["capabilities"], list) and sc["capabilities"], f"{path.stem}: capabilities 必須為非空 list"
        assert sc["timeout_s"] > 0, f"{path.stem}: timeout_s 必須 > 0"
        assert isinstance(sc["invariants"], list), f"{path.stem}: invariants 必須為 list"
        assert set(sc["invariants"]) <= SIX_INVARIANTS, f"{path.stem}: invariants 含未知 ID {set(sc['invariants']) - SIX_INVARIANTS}"
        assert sc["expected"] == "PASS", f"{path.stem}: expected 必須為 PASS"


@pytest.mark.memory_only
def test_declared_invariants_have_enforcers():
    """每個宣告的不變量在 steps 中至少出現一個 INVARIANT_ENFORCERS 所列動作（防止只貼標籤）。"""
    runner_mod = _load_runner()
    enforcers = runner_mod.INVARIANT_ENFORCERS
    assert set(enforcers) == SIX_INVARIANTS, "INVARIANT_ENFORCERS 鍵須恰為 §11.3 六個 ID"
    files = _scenario_files()
    assert files, "沒有任何情境檔可檢查"
    for path in files:
        doc = _yaml(path)
        sc = doc["meta"]["scenario"]
        used = {step.get("action") for step in _iter_steps(doc.get("steps"))}
        for inv in sc["invariants"]:
            allowed = set(enforcers[inv])
            assert used & allowed, f"{path.stem} 宣告 {inv} 但 steps 無對應執行者動作 {sorted(allowed)}"
            if path.stem.startswith("win-com-") and inv == "no_reopen_before_handoff":
                assert "hold_endpoint" in used, f"{path.stem}: Windows 的 no_reopen_before_handoff 須以 hold_endpoint 滿足"


@pytest.mark.memory_only
def test_slice21_handoff_classes_covered():
    """2.1 切片涵蓋 Release、Recovery、Reboot、獨占；2.2 切片涵蓋 Lease 切換（interactive-open）。"""
    by_slice: dict[str, list[dict]] = {}
    for path in _scenario_files():
        doc = _yaml(path)
        by_slice.setdefault(str(doc["meta"]["scenario"].get("daemon_slice")), []).append(doc)
    s21 = [s for doc in by_slice.get("2.1", []) for s in _iter_steps(doc.get("steps"))]
    s22 = [s for doc in by_slice.get("2.2", []) for s in _iter_steps(doc.get("steps"))]
    assert s21, "沒有 daemon_slice=2.1 的情境"
    assert any(_cli_argv_has(s, "device", "release") for s in s21), "2.1 缺 Release（device release）"
    assert any(_cli_argv_has(s, "session", "recover") for s in s21), "2.1 缺 Recovery（session recover）"
    assert any(
        s.get("action") in ("dut_control", "bridge_control") and "logical_reboot" in json.dumps(s, ensure_ascii=False)
        for s in s21
    ), "2.1 缺 Reboot（logical_reboot）"
    assert any(s.get("action") == "hold_endpoint" for s in s21), "2.1 缺獨占（hold_endpoint）"
    assert any(_cli_argv_has(s, "interactive-open") for s in s22), "2.2 缺 Lease 切換（interactive-open）"


@pytest.mark.memory_only
def test_scenarios_bridge_only():
    """所有情境宣告為 bridge 專用（bridge.applicable 與 bridge.only 皆為 true）。"""
    files = _scenario_files()
    assert files, "沒有任何情境檔可檢查"
    for path in files:
        bridge = _yaml(path).get("bridge") or {}
        assert bridge.get("applicable") is True, f"{path.stem}: bridge.applicable 必須為 true"
        assert bridge.get("only") is True, f"{path.stem}: bridge.only 必須為 true"


@pytest.mark.memory_only
def test_fault_actions_not_in_uart_stream():
    """控制字樣只能走 bridge_control／dut_control，不得出現在 UART 輸入（cli argv、console／raw 輸入）。"""
    files = _scenario_files()
    assert files, "沒有任何情境檔可檢查"
    for path in files:
        for step in _iter_steps(_yaml(path).get("steps")):
            action = step.get("action")
            if action == "cli":
                text = " ".join(str(a) for a in (step.get("argv") or []))
            elif action in ("console_write", "input_raw"):
                text = json.dumps(step, ensure_ascii=False)
            else:
                continue
            for word in FORBIDDEN_UART_WORDS:
                assert word not in text, f"{path.stem}: UART 路徑出現控制字樣 {word!r}（§15.3）"


@pytest.mark.memory_only
def test_no_fixed_sleep_as_sync_point():
    """sleep 合計 ≤10 s；每個故障動作之後都有同步點（wait_wire_marker／wait_state／wait_command_done／wait_ready）。"""
    files = _scenario_files()
    assert files, "沒有任何情境檔可檢查"
    for path in files:
        steps = list(_iter_steps(_yaml(path).get("steps")))
        total = sum(float(s.get("seconds", 0)) for s in steps if s.get("action") == "sleep")
        assert total <= 10, f"{path.stem}: sleep 合計 {total} s 超過 10 s（§15.7.1 不得以固定 sleep 當同步點）"
        for idx, step in enumerate(steps):
            if step.get("action") in FAULT_ACTIONS:
                later = [s.get("action") for s in steps[idx + 1:]]
                assert any(a in SYNC_ACTIONS for a in later), (
                    f"{path.stem}: 故障動作 {step.get('action')} 之後沒有同步點"
                )


@pytest.mark.memory_only
def test_runner_actions_exist(tmp_path):
    """runner 動作表含 §15.7.1 的 16 個新動作（以未知動作訊息判定缺漏；不帶 harness 以免副作用）。"""
    runner_mod = _load_runner()
    missing: list[str] = []
    for name in NEW_ACTIONS:
        tr = _make_runner(runner_mod, [], None, bridge=True)
        try:
            tr._exec_step({"action": name}, 0)
        except Exception as exc:  # noqa: BLE001 - 只關心是否被識別為動作
            if "未知 action" in str(exc):
                missing.append(name)
    assert missing == [], f"runner 動作表缺少：{missing}"


# ---------------------------------------------------------------------------
# runner 新動作的語意（替身 BridgeSession＋手寫證據）
# ---------------------------------------------------------------------------


@pytest.mark.memory_only
def test_assert_wire_absent(tmp_path):
    """marker 只在 after_op 之前 → PASS；之後出現 → FAIL；快照未 settled → INSUFFICIENT_EVIDENCE。"""
    runner_mod = _load_runner()
    marker = "L15_STALE_HUMAN"
    step = {"action": "assert_wire_absent", "marker": marker, "after_op": "reboot"}
    subcases = [
        ("marker_only_before", [b"L15_STALE_HUMAN\n", b"echo ok\n"], 1, True, "pass"),
        ("marker_after", [b"echo ok\n", b"L15_STALE_HUMAN\n"], 1, True, "fail"),
        ("unsettled", [b"echo ok\n"], 1, False, "insufficient"),
    ]
    for name, chunks, n_before, settled, outcome in subcases:
        root = tmp_path / name
        evd = root / "evidence"
        seqs = _write_wire(evd, [("tx", c) for c in chunks])
        _write_correlation(evd, [
            _corr(1, op_id="reboot", kind="op_start", wire_seq=BRIDGE_START_SEQ, offsets=_zero_offsets()),
            _corr(2, op_id="reboot", kind="op_end", wire_seq=seqs[n_before - 1],
                  offsets=_tx_offsets(chunks[:n_before]), settled=settled),
        ])
        fake = _FakeBridge(evd, root / "wal")
        tr = _make_runner(runner_mod, [step], _FakeHarness(root, bridge=fake))
        _expect(runner_mod, tr, step, outcome, f"assert_wire_absent[{name}]")


@pytest.mark.memory_only
def test_assert_peer_closed(tmp_path):
    """release 回覆時 peer 已關閉且交接區間無 peer_state open:true → PASS；其餘 FAIL；Windows（null）→ SKIP。"""
    runner_mod = _load_runner()
    step = {"action": "assert_peer_closed", "at_op": "release", "until_op": "reattach"}
    subcases = [
        # 名稱, release 快照的 peer_open, 區間內的額外 wire 事件, 預期
        ("closed_no_reopen", False, [], "pass"),
        ("peer_open_at_op", True, [], "fail"),
        ("reopen_in_window", False, [("meta", {"event": "peer_state", "open": True})], "fail"),
        ("windows_null", None, [], "skip"),
    ]
    zero = _zero_offsets()
    for name, release_peer, events, outcome in subcases:
        root = tmp_path / name
        seqs = _write_wire(root / "evidence", events)
        release_seq = BRIDGE_START_SEQ
        reattach_seq = seqs[-1] if seqs else BRIDGE_START_SEQ
        fake = _FakeBridge(root / "evidence", root / "wal")
        tr = _make_runner(runner_mod, [], _FakeHarness(root, bridge=fake))

        fake.snap = {"settled": True, "wire_seq": release_seq, "offsets": zero, "peer_open": release_peer}
        tr._exec_step({"action": "op_begin", "op_id": "release"}, 0)
        tr._exec_step({"action": "op_end", "op_id": "release"}, 0)

        fake.snap = {"settled": True, "wire_seq": reattach_seq, "offsets": zero, "peer_open": False}
        tr._exec_step({"action": "op_begin", "op_id": "reattach"}, 0)
        tr._exec_step({"action": "op_end", "op_id": "reattach"}, 0)

        _expect(runner_mod, tr, step, outcome, f"assert_peer_closed[{name}]")


@pytest.mark.memory_only
def test_command_resolved_terminal_set(tmp_path, monkeypatch):
    """終態白名單 {done,error,canceled,interactive}：依序回 accepted→running→done 通過；永久 accepted 逾時失敗；缺 status 或未知值失敗。"""
    runner_mod = _load_runner()
    payload = "echo L15_RESOLVED\n"
    cases: list[tuple[str, list[dict], str]] = [
        ("accepted_running_done", [
            {"ok": True, "command": {"status": "accepted"}},
            {"ok": True, "command": {"status": "running"}},
            {"ok": True, "command": {"status": "done"}},
        ], "pass"),
    ]
    cases += [(f"terminal_{s}", [{"ok": True, "command": {"status": s}}], "pass") for s in TERMINAL_STATES if s != "done"]
    cases += [
        ("accepted_forever_timeout", [{"ok": True, "command": {"status": "accepted"}}], "fail"),
        ("status_missing", [{"ok": True, "command": {}}], "fail"),
        ("unknown_queued", [{"ok": True, "command": {"status": "queued"}}], "fail"),
    ]
    for name, responses, outcome in cases:
        root = tmp_path / name
        evd = root / "evidence"
        _write_wire(evd, [("tx", payload.encode("latin-1"))])
        _patch_cli(monkeypatch, runner_mod, responses)
        fake = _FakeBridge(evd, root / "wal")
        tr = _make_runner(runner_mod, [], _FakeHarness(root, bridge=fake))
        tr.variables["d"] = {"ok": True, "cmd_id": "c-term"}
        step = {"action": "assert_command_resolved", "from": "d", "timeout_s": 1, "payload": payload}
        _expect(runner_mod, tr, step, outcome, f"assert_command_resolved[{name}]")


@pytest.mark.memory_only
def test_assert_command_resolved_both_branches(tmp_path, monkeypatch):
    """被接受（有 cmd_id）payload 恰一次通過、兩次失敗；被拒（無 cmd_id、有 error_code）payload 零次通過、一次失敗。"""
    runner_mod = _load_runner()
    payload = "echo L15_X\n"
    accepted = {"ok": True, "cmd_id": "c-acc"}
    rejected = {"ok": False, "error_code": "CMD_CONTAINS_NEWLINE"}
    error_terminal = [{"ok": True, "command": {"status": "error"}}]
    subcases = [
        ("accepted_once", accepted, [("tx", payload.encode())], error_terminal, "pass"),
        ("accepted_twice", accepted, [("tx", payload.encode()), ("tx", payload.encode())], error_terminal, "fail"),
        ("rejected_zero", rejected, [], [], "pass"),
        ("rejected_once", rejected, [("tx", payload.encode())], [], "fail"),
    ]
    for name, saved, events, responses, outcome in subcases:
        root = tmp_path / name
        evd = root / "evidence"
        _write_wire(evd, events)
        _patch_cli(monkeypatch, runner_mod, responses or [{"ok": True, "command": {"status": "error"}}])
        fake = _FakeBridge(evd, root / "wal")
        tr = _make_runner(runner_mod, [], _FakeHarness(root, bridge=fake))
        tr.variables["d"] = dict(saved)
        step = {"action": "assert_command_resolved", "from": "d", "timeout_s": 2, "payload": payload}
        _expect(runner_mod, tr, step, outcome, f"assert_command_resolved[{name}]")


@pytest.mark.memory_only
def test_wal_meta_assert(tmp_path):
    """WAL 目錄內存在 META 且含指定值、loss_flag 相符、含指定 key 才 PASS。"""
    runner_mod = _load_runner()
    meta_rec = {
        "cmd_id": None,
        "com": "COM0",
        "crc32": "00000000",
        "dir": "META",
        "len": 0,
        "loss_flag": True,
        "meta": {"event": "WAL_RECREATED_AFTER_EXTERNAL_UNLINK", "potentially_lost_seq_range": "unknown"},
        "mono_ts_ns": 1,
        "payload_b64": "",
        "seq": 1,
        "source": "serialwrap:l15-unit",
        "wall_ts": WALL_TS,
    }
    root = tmp_path
    wal_dir = root / "wal"
    wal_dir.mkdir(parents=True)
    (wal_dir / "raw.wal.ndjson").write_text(
        json.dumps(meta_rec, ensure_ascii=False, sort_keys=True, separators=(",", ":")) + "\n", encoding="utf-8"
    )
    fake = _FakeBridge(root / "evidence", wal_dir)
    harness = _FakeHarness(root, bridge=fake)
    ok_step = {
        "action": "assert_wal_meta",
        "contains_value": "WAL_RECREATED_AFTER_EXTERNAL_UNLINK",
        "loss_flag": True,
        "has_keys": ["potentially_lost_seq_range"],
    }
    bad_step = dict(ok_step, loss_flag=False)
    _expect(runner_mod, _make_runner(runner_mod, [ok_step], harness), ok_step, "pass", "assert_wal_meta[match]")
    _expect(runner_mod, _make_runner(runner_mod, [bad_step], harness), bad_step, "fail", "assert_wal_meta[loss_flag_mismatch]")


@pytest.mark.memory_only
def test_mid_run_asserts_use_checkpoint(tmp_path, monkeypatch):
    """assert_compare／assert_zero_tx／assert_wire_absent 執行前各呼叫一次 checkpoint()，且比較器收到該 checkpoint 路徑。"""
    events: list = []
    _install_comparator_spies(monkeypatch, events)
    runner_mod = _load_runner()
    root = tmp_path
    evd = root / "evidence"
    chunks = [b"echo ok\n"]
    _write_wire(evd, [("tx", c) for c in chunks])
    _write_correlation(evd, [
        _corr(1, op_id="mid", kind="op_start", wire_seq=BRIDGE_START_SEQ, offsets=_zero_offsets()),
        _corr(2, op_id="mid", kind="op_end", wire_seq=BRIDGE_START_SEQ + 2, offsets=_tx_offsets(chunks)),
    ])
    fake = _FakeBridge(evd, root / "wal", events=events)
    tr = _make_runner(runner_mod, [], _FakeHarness(root, bridge=fake))
    steps = [
        {"action": "assert_zero_tx", "op_id": "mid"},
        {"action": "assert_compare"},
        {"action": "assert_wire_absent", "marker": "L15_NEVER", "after_op": "mid"},
    ]
    for step in steps:
        tr._exec_step(step, 0)

    kinds = [e if isinstance(e, str) else e[0] for e in events]
    assert kinds == ["checkpoint", "check_zero_tx", "checkpoint", "compare", "checkpoint"], kinds
    assert len(fake.checkpoint_paths) == 3
    assert pathlib.Path(events[1][1]) == fake.checkpoint_paths[0]
    assert pathlib.Path(events[3][1]) == fake.checkpoint_paths[1]


@pytest.mark.memory_only
def test_bridge_only_action_rejected_in_pty(tmp_path):
    """pty 模式（無 bridge）執行 bridge 專用動作 op_begin → StepError('bridge-only action')。"""
    runner_mod = _load_runner()
    tr = _make_runner(runner_mod, [], None, bridge=False)
    with pytest.raises(runner_mod.StepError, match="bridge-only action"):
        tr._exec_step({"action": "op_begin", "op_id": "x"}, 0)


@pytest.mark.memory_only
def test_plus_one_x_derived_var(tmp_path, monkeypatch):
    """save_as 存入 limits.max_submit_cmd_bytes=5 後，{st.limits.max_submit_cmd_bytes_plus_one_x} 展開為 6 個 x。"""
    runner_mod = _load_runner()
    _patch_cli(monkeypatch, runner_mod, [{"ok": True, "limits": {"max_submit_cmd_bytes": 5}}])
    tr = _make_runner(runner_mod, [], _FakeHarness(tmp_path), bridge=False)
    tr._exec_step({"action": "cli", "argv": ["daemon", "status"], "save_as": "st"}, 0)
    assert tr._expand("{st.limits.max_submit_cmd_bytes_plus_one_x}") == "xxxxxx"


# ---------------------------------------------------------------------------
# win-com-txrx-bridge 情境形狀（S1X Y4，CHG-S1X-WIN-TXRX-PAYLOADS-01）
# ---------------------------------------------------------------------------


@pytest.mark.memory_only
def test_repeat_exposes_one_based_index(tmp_path, monkeypatch):
    """`repeat` 迴圈內可用 `{repeat.index}`（1 起算）；迴圈結束後還原外層同名變數。"""
    runner_mod = _load_runner()
    calls = _patch_cli(monkeypatch, runner_mod, [{"ok": True, "cmd_id": "c"}])
    tr = _make_runner(runner_mod, [], _FakeHarness(tmp_path), bridge=False)
    tr.variables["repeat"] = {"index": "outer"}
    tr._exec_step({"action": "repeat", "count": 3, "steps": [
        {"action": "cli", "argv": ["cmd", "submit", "--cmd", "echo W_{repeat.index}"]}]}, 0)
    assert [c[-1] for c in calls] == ["echo W_1", "echo W_2", "echo W_3"]
    assert tr.variables["repeat"] == {"index": "outer"}


@pytest.mark.memory_only
def test_win_com_txrx_bridge_expects_each_payload_exactly_once():
    """plan 03 §win-com-txrx-bridge：`echo W_<n>` 50 次、每個 payload 在 wire 恰一次且依序；console bytes 原樣出現。

    舊版以相同 `echo W_TEST` 送 50 次卻只預期 1 次（比較器要求次數完全相符，正確行為必 FAIL），且未檢 console bytes。
    """
    doc = yaml.safe_load((SCEN_DIR / "win-com-txrx-bridge.yaml").read_text(encoding="utf-8"))
    steps = doc["steps"]
    rep = next(s for s in steps if s.get("action") == "repeat")
    assert rep["count"] == 50
    submit = rep["steps"][0]
    assert _cli_argv_has(submit, "cmd", "submit") and "echo W_{repeat.index}" in submit["argv"]
    asserts = [s for s in steps if s.get("action") == "assert_wire_contiguous"]
    assert [f"echo W_{n}\n" for n in range(1, 51)] in [a["payloads"] for a in asserts]
    console = next(s for s in steps if s.get("action") == "console_write")["input_raw"]
    assert console == "".join(chr(c) for c in range(0x20, 0x7F)) + "\r"
    assert [console] in [a["payloads"] for a in asserts]
    # 斷言前須等 console bytes 落盤到 wire（證據 writer 非同步）
    names = [s.get("action") for s in steps]
    wait_idx = next(i for i, s in enumerate(steps)
                    if s.get("action") == "wait_wire_marker" and console.endswith(s.get("marker", "\0")))
    assert names.index("console_write") < wait_idx < names.index("assert_wire_contiguous")
    # 最後一個命令須 done（佇列 FIFO，最後一個完成代表 50 個都已執行）
    assert any(_cli_argv_has(s, "cmd", "status") and (s.get("expect") or {}).get("command.status") == "done"
               for s in steps)
