"""OD-19：模擬端點防毒（scripts/stage0/av_sim.py）對 WAL 的干擾驗收。

驗收項（OD-19 (2)）：記錄不遺失（seq 1..N 連續）、寫入延遲 p99 有上限、rotate 與 unlink 最終完成、
F12 marker 持續進展。干擾必須實際發生（denied／rotate_retry 皆需大於 0），否則視為空轉、不得通過。

- 注入式版本（``core_portable``＋``memory_only``）：``AvSimWalFs`` 以邏輯時鐘套用同一持有模型，
  任何 OS 皆可執行，不開子行程。
- 原生版本（``windows_native``＋``process_rpc``）：Windows helper 行程以不含 FILE_SHARE_DELETE 的共用模式
  實際佔用 WAL 檔，只能於擁有者 Windows host 以 ``scripts/stage0/win-pytest.sh`` 執行；非 Windows 由
  conftest 標為 SKIP（SKIP 不計為 PASS，屬證據不足）。

本檔不在模組層放資源 marker（每個測試函式各自標記），以符合 tests/test_resource_markers.py 的規則。
"""

from __future__ import annotations

import glob
import importlib
import json
import math
import os
import subprocess
import sys
import time
from pathlib import Path
from typing import Any

import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]
AV_SIM_PATH = REPO_ROOT / "scripts" / "stage0" / "av_sim.py"
SEED = 20261023
RECORD_COUNT = 600
ROTATE_BYTES = 4096
LATENCY_P99_BOUND_S = 0.05  # 注入式版本不睡眠，單筆 append 的 p99 上限（秒）


def _av() -> Any:
    """延遲匯入 scripts/stage0/av_sim.py（測試收集階段不依賴該模組）。"""
    sys.path.insert(0, str(AV_SIM_PATH.parent))
    try:
        return importlib.import_module("av_sim")
    finally:
        sys.path.pop(0)


class _Clock:
    """注入式邏輯時鐘：WalWriter 退避與 AvSim 持有皆以此計時。"""

    def __init__(self) -> None:
        self.t = 1000.0

    def __call__(self) -> float:
        return self.t

    def advance(self, s: float) -> None:
        self.t += s


def _read_all_seqs(wal_dir: Path) -> list[int]:
    seqs: list[int] = []
    for path in glob.glob(str(wal_dir / "raw.wal.ndjson*")):
        with open(path, encoding="utf-8") as fp:
            for line in fp:
                line = line.strip()
                if line.startswith("{"):
                    seqs.append(int(json.loads(line)["seq"]))
    return sorted(seqs)


def _read_all_rows(wal_dir: Path) -> list[dict]:
    """依 seq 排序的全部 WAL 列（含歸檔段與現行檔）。"""
    rows: list[dict] = []
    for path in glob.glob(str(wal_dir / "raw.wal.ndjson*")):
        with open(path, encoding="utf-8") as fp:
            for line in fp:
                line = line.strip()
                if line.startswith("{"):
                    rows.append(json.loads(line))
    return sorted(rows, key=lambda r: int(r["seq"]))


def _wait_ready_record(path: Path, timeout_s: float) -> dict[str, Any]:
    """ready 檔可見但內容尚未完成時重試至期限，避免把短暫解析競態當成證據不足。"""
    deadline = time.monotonic() + timeout_s
    last_error: Exception | None = None
    while time.monotonic() < deadline:
        try:
            record = json.loads(path.read_text(encoding="utf-8"))
            if isinstance(record, dict) and record.get("ready") is True:
                return record
            last_error = ValueError("ready 記錄格式錯誤")
        except (OSError, ValueError, TypeError) as exc:
            last_error = exc
        time.sleep(0.01)
    raise AssertionError(f"ready 記錄未於期限內完成：{last_error}")


# OD-19（tick 補寫連續性 META）：延後輪替於無新寫入的 tick 內完成時，會寫入一筆 WAL_ROTATED_AFTER_DEFERRAL META，
# 其佔用一個 seq。資料記錄數＝總 seq 數扣除此類 META；seq 仍必須無缺號（不遺失、不重複）。
_CONTINUITY_META = "WAL_ROTATED_AFTER_DEFERRAL"


def _is_continuity_meta(row: dict) -> bool:
    return row.get("dir") == "META" and (row.get("meta") or {}).get("event") == _CONTINUITY_META


def _injected_writer(tmp_path: Path, clock: _Clock, seed: int = SEED):
    from sw_core.wal import WalWriter

    av = _av()
    fs = av.AvSimWalFs(clock=clock, seed=seed)
    w = WalWriter(wal_dir=str(tmp_path), rotate_bytes=ROTATE_BYTES, fs=fs, clock=clock)
    return w, fs


def _drain_pending(w, clock: _Clock, rounds: int = 12) -> None:
    """推進邏輯時間超過最長持有（500 ms）與延遲間隔（1 s），以 tick 讓延後的輪替完成。"""
    for _ in range(rounds):
        clock.advance(0.5)
        w.tick()


def _timed_append(writer, samples_ms: list[float], **kwargs):
    started = time.perf_counter_ns()
    try:
        return writer.append(**kwargs)
    finally:
        samples_ms.append((time.perf_counter_ns() - started) / 1_000_000)


@pytest.mark.core_portable
@pytest.mark.memory_only
def test_av_sim_hold_schedule_is_seeded_and_bounded():
    av = _av()
    a = av.HoldSchedule(SEED)
    b = av.HoldSchedule(SEED)
    c = av.HoldSchedule(SEED + 1)
    seq_a = [a.next_hold_s() for _ in range(64)]
    assert seq_a == [b.next_hold_s() for _ in range(64)], "同 seed 必須可重現"
    assert seq_a != [c.next_hold_s() for _ in range(64)], "不同 seed 應產生不同序列"
    assert all(av.HOLD_MS_MIN / 1000 <= h <= av.HOLD_MS_MAX / 1000 for h in seq_a)
    params = av.describe(SEED)
    assert params["hold_ms_range"] == [10, 500] and params["seed"] == SEED
    assert params["oplock"] == "not_requested"
    oplock_params = av.describe(SEED, oplock=True)
    assert oplock_params["mode"] == "oplock" and oplock_params["oplock"] == "pending"


@pytest.mark.core_portable
@pytest.mark.memory_only
def test_av_sim_no_record_loss_seq_continuous_under_interference(tmp_path):
    clock = _Clock()
    w, fs = _injected_writer(tmp_path, clock)
    for _ in range(RECORD_COUNT):
        clock.advance(0.001)
        w.append(com="COM0", direction="TX", source="av_sim", payload=b"x" * 64)
    _drain_pending(w, clock)
    w.close()
    rows = _read_all_rows(tmp_path)
    seqs = [int(r["seq"]) for r in rows]
    assert seqs == list(range(1, len(rows) + 1)), "seq 無缺號、無重複（1..M）"
    data = [r for r in rows if not _is_continuity_meta(r)]
    assert len(data) == RECORD_COUNT, "資料記錄不遺失（扣除 tick 補寫的連續性 META）"
    assert [int(r["seq"]) for r in data] == sorted(int(r["seq"]) for r in data)
    assert fs.denied >= 1, "干擾必須實際發生，否則驗收為空轉"


@pytest.mark.core_portable
@pytest.mark.memory_only
def test_av_sim_write_latency_p99_is_bounded(tmp_path):
    clock = _Clock()
    w, _fs = _injected_writer(tmp_path, clock)
    samples: list[float] = []
    for _ in range(RECORD_COUNT):
        clock.advance(0.001)
        started = time.perf_counter()
        w.append(com="COM0", direction="TX", source="av_sim", payload=b"x" * 64)
        samples.append(time.perf_counter() - started)
    w.close()
    ordered = sorted(samples)
    p99 = ordered[max(0, int(len(ordered) * 0.99) - 1)]
    assert p99 <= LATENCY_P99_BOUND_S, f"寫入延遲 p99={p99:.6f}s 超過上限 {LATENCY_P99_BOUND_S}s"


@pytest.mark.core_portable
@pytest.mark.memory_only
def test_av_sim_rotate_completes_finally_and_deferred_queue_drains(tmp_path):
    clock = _Clock()
    w, fs = _injected_writer(tmp_path, clock)
    for _ in range(RECORD_COUNT):
        clock.advance(0.001)
        w.append(com="COM0", direction="TX", source="av_sim", payload=b"x" * 64)
    health_during = w.health()
    assert health_during["rotate_retry"] >= 1, "干擾應使輪替至少重試一次"
    _drain_pending(w, clock)
    health_after = w.health()
    assert health_after["deferred_delete_pending"] == 0, "延後佇列必須在干擾結束後清空"
    archives = [n for n in os.listdir(tmp_path) if n.startswith("raw.wal.ndjson.")]
    assert archives, "輪替必須最終完成（至少一個歸檔段）"
    # 干擾結束後寫入仍可進行（AV 持有不會永久阻塞寫入）；seq 續接上一筆已寫入的紀錄（含連續性 META）。
    last_before = w.health()["current_seq"]
    assert last_before >= RECORD_COUNT
    clock.advance(0.5)
    rec = w.append(com="COM0", direction="TX", source="av_sim", payload=b"after")
    assert rec["seq"] == last_before + 1
    w.close()


@pytest.mark.core_portable
@pytest.mark.memory_only
def test_av_sim_f12_marker_progress_across_rotation(tmp_path):
    from realhw import wal_oracle

    clock = _Clock()
    w, fs = _injected_writer(tmp_path, clock)
    wal_path = str(tmp_path / "raw.wal.ndjson")
    w.append(com="COM0", direction="TX", source="av_sim", payload=b"prefill")
    cursor = wal_oracle.open_cursor(wal_path)
    progressed = 0
    for k in range(4):
        marker = f"AV-SIM-F12-MARKER-{k}".encode()
        w.append(com="COM0", direction="TX", source="av_sim", payload=marker, cmd_id=f"av-f12-{k}")
        res = wal_oracle.wait_for_marker(
            cursor,
            marker=marker,
            cmd_id=f"av-f12-{k}",
            timeout_s=5.0,
            sleep=clock.advance,
            monotonic=clock,
        )
        assert res.ok, f"marker {k} 未被 oracle 判定為進展：{res.reason_code}"
        progressed += 1
        for _ in range(RECORD_COUNT // 4):  # 每段寫入量足以跨越 rotate 門檻
            clock.advance(0.001)
            w.append(com="COM0", direction="TX", source="av_sim", payload=b"x" * 64)
    _drain_pending(w, clock)
    w.close()
    assert progressed == 4
    seqs = _read_all_seqs(tmp_path)
    assert seqs == list(range(1, len(seqs) + 1)), "marker 持續進展期間 seq 必須連續"


@pytest.mark.core_portable
@pytest.mark.memory_only
def test_av_sim_oplock_variant_is_wired(monkeypatch):
    """保留原測試涵蓋面：--oplock 須進入 helper，不得再以未實作退出。"""
    av = _av()
    seen = {}

    def fake_helper(**kwargs):
        seen.update(kwargs)
        return 0

    monkeypatch.setattr(av, "run_helper", fake_helper)
    rc = av.main(
        ["--watch-dir", ".", "--seed", "1", "--duration-s", "0", "--ready-file", "r",
         "--stop-file", "s", "--log-file", "l", "--go-file", "g", "--ack-file", "a", "--oplock"]
    )
    assert rc == 0
    assert seen["oplock"] is True


@pytest.mark.core_portable
@pytest.mark.memory_only
def test_spike3_records_no_delete_primary_and_oplock_variant_separately(monkeypatch, tmp_path):
    """Spike 3 主 helper 不帶 --oplock；oplock 只記入獨立變體。"""
    import importlib.util
    from sw_core import wal_fs

    script = REPO_ROOT / "scripts" / "stage0" / "spike3_win_wal.py"
    spec = importlib.util.spec_from_file_location("spike3_helper_modes_test", script)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    recorder = module.Recorder()
    primary = module.AvHelper(tmp_path, SEED, recorder)
    variant = module.AvHelper(tmp_path, SEED, recorder, oplock=True, variant="oplock")

    assert "--oplock" not in primary._argv()
    assert "--oplock" in variant._argv()
    assert primary._evidence()["mode"] == "share_no_delete"
    assert variant._evidence()["mode"] == "oplock"
    # 替身 helper 與延遲 rename 共用的 lease／break 時點（perf_counter_ns）。
    lease_state: dict[str, int] = {}

    class StubHelper:
        def __init__(self, wal_dir, seed, rec, *, oplock=False, variant=None):
            self.rec = rec
            self.variant = variant
            self.lease_acquired_ns = None
            self.lease_until_ns = None
            self.rec.observations.setdefault("av_sim", {}).setdefault("variants", {})[variant] = {
                **module._av_sim_module().describe(seed, oplock=oplock),
                "launch_error": "not_started", "grabs": None, "open_errors": None, "helper_exit": None,
                "lease_request_id": None, "oplock_breaks": [],
            }

        def start(self):
            self.rec.observations["av_sim"]["variants"][self.variant]["launch_error"] = "none"
            return True

        def ready_holds(self, name):
            return name == "raw.wal.ndjson"

        def arm_hold(self, _name):
            self.lease_acquired_ns = time.perf_counter_ns()
            # 常駐模擬高負載：lease 取得到 rotation 請求之間有 30 ms 排程延遲（舊替身 break 寫死 +25 ms 會誤判 INSUFFICIENT）。
            real_sleep(0.03)
            # lease 須涵蓋 rotation 請求前的排程延遲；break 由 rename 請求觸發（見 DelayedRenameFs）。
            self.lease_until_ns = self.lease_acquired_ns + 200_000_000
            lease_state["until_ns"] = self.lease_until_ns
            self.lease_request_id = "stub-oplock-lease"
            self.rec.observations["av_sim"]["variants"][self.variant]["lease_request_id"] = self.lease_request_id
            return True

        def stop(self):
            close_completed_ns = time.perf_counter_ns()
            self.rec.observations["av_sim"]["variants"][self.variant].update(
                {"grabs": 1, "helper_exit": 0, "oplock": "granted", "oplock_breaks": [{
                    "event": "oplock_break", "request_id": self.lease_request_id, "name": "raw.wal.ndjson",
                    "lease_acquired_ns": self.lease_acquired_ns, "lease_until_ns": self.lease_until_ns,
                    "break_notified_ns": lease_state["break_ns"],
                    "break_completed": True, "ack_required": True,
                    "ack_method": "close_handle", "close_started_ns": self.lease_until_ns,
                    "close_completed_ns": close_completed_ns, "close_succeeded": True,
                    "acknowledged_ns": close_completed_ns,
                }]}
            )

    monkeypatch.setattr(module, "AvHelper", StubHelper)
    real_sleep = time.sleep
    real_default_wal_fs = wal_fs.default_wal_fs

    class DelayedRenameFs:
        def __init__(self, delegate):
            self.delegate = delegate

        def rename_noreplace_or_suffix(self, src, base_dst):
            # 模擬 oplock：第一次 rename 請求即觸發 break 通知，helper 持有到 lease 到期才 close（ACK），
            # rename 隨後完成；oplock 只 break 一次，之後的 rename（後續輪替）不再等待。
            if "break_ns" not in lease_state:
                lease_state["break_ns"] = time.perf_counter_ns()
                remaining_s = (lease_state["until_ns"] - time.perf_counter_ns()) / 1e9
                real_sleep(max(0.0, remaining_s) + 0.002)
            return self.delegate.rename_noreplace_or_suffix(src, base_dst)

        def __getattr__(self, name):
            return getattr(self.delegate, name)

    monkeypatch.setattr(wal_fs, "default_wal_fs", lambda: DelayedRenameFs(real_default_wal_fs()))
    monkeypatch.setattr(module.time, "sleep", lambda _seconds: None)
    (tmp_path / "raw.wal.ndjson").write_bytes(b"prefill\n")
    status = module._collect_oplock_variant(tmp_path, recorder)
    workload = recorder.observations.get("oplock_variant", {})
    assert status == "PASS", {
        "status": status,
        "error": recorder.observations.get("oplock_variant_error"),
        "rotation_requested_ns": workload.get("rotation_requested_ns"),
        "rotation_completed_ns": workload.get("rotation_completed_ns"),
        "lease_until_ns": workload.get("lease_until_ns"),
        "oplock_breaks": workload.get("oplock_breaks"),
    }
    assert recorder.observations["av_sim"]["variants"]["oplock"]["share_mode"] == (
        "FILE_SHARE_READ|FILE_SHARE_WRITE|FILE_SHARE_DELETE"
    )


@pytest.mark.core_portable
@pytest.mark.memory_only
def test_spike3_oplock_variant_runs_append_rotation_and_f12(monkeypatch, tmp_path):
    _run_spike3_oplock_variant_workload(monkeypatch, tmp_path, failure_point=None)


@pytest.mark.core_portable
@pytest.mark.memory_only
@pytest.mark.parametrize("failure_point", ["drain", "close", "break-after-expiry"])
def test_spike3_oplock_variant_failures_write_evidence(monkeypatch, tmp_path, failure_point):
    _run_spike3_oplock_variant_workload(monkeypatch, tmp_path, failure_point)


@pytest.mark.core_portable
@pytest.mark.memory_only
def test_spike3_oplock_workload_failure_is_fail_not_missing_evidence(monkeypatch, tmp_path):
    _run_spike3_oplock_variant_workload(monkeypatch, tmp_path, failure_point="records")


def _run_spike3_oplock_variant_workload(monkeypatch, tmp_path, failure_point):
    """oplock 持有期間須實際 append、跨 rotate，並由 F12 oracle 讀到前後 marker。"""
    import importlib.util
    from sw_core import wal
    from sw_core import wal_fs
    from realhw import wal_oracle

    script = REPO_ROOT / "scripts" / "stage0" / "spike3_win_wal.py"
    spec = importlib.util.spec_from_file_location("spike3_oplock_workload_test", script)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    events: list[tuple[str, int]] = []
    # 替身 helper 與延遲 rename 共用的 lease／break 時點（perf_counter_ns）。
    lease_state: dict[str, int] = {}

    class FakeWriter:
        def __init__(self, *, wal_dir, rotate_bytes, fs=None):
            self.wal_dir = Path(wal_dir)
            self.wal_dir.mkdir(parents=True, exist_ok=True)
            self.index = 0
            self.fs = fs

        def append(self, *, payload, **_kwargs):
            self.index += 1
            if self.index == 1:
                (self.wal_dir / "raw.wal.ndjson").write_bytes(b"prefill\n")
            elif self.index == 2:
                # 模擬高負載：lease 取得到 rotation 請求之間有排程延遲（全套 pytest 曾因此誤判 INSUFFICIENT）。
                real_sleep(0.015)
                wal_path = self.wal_dir / "raw.wal.ndjson"
                wal_path.write_bytes(b"rotation request\n")
                assert self.fs is not None
                self.fs.rename_noreplace_or_suffix(str(wal_path), f"{wal_path}.1")
            events.append(("append", self.index))
            return {"seq": self.index}

        def health(self):
            return {"rotate_retry": 1, "rotate_deferred": 0, "deferred_delete_pending": 0,
                    "write_failures": 0}

        def close(self):
            if failure_point == "close":
                raise OSError("injected close failure")

        def tick(self):
            pass

    class FakeHelper:
        def __init__(self, wal_dir, seed, rec, *, oplock=False, variant=None):
            rec.observations.setdefault("av_sim", {}).setdefault("variants", {})[variant] = {
                **module._av_sim_module().describe(seed, oplock=oplock),
                "launch_error": "not_started", "grabs": None, "open_errors": None,
                "helper_exit": None, "lease_request_id": None, "oplock_breaks": [],
            }
            self.rec = rec
            self.variant = variant

        def start(self):
            events.append(("helper_start", 0))
            self.rec.observations["av_sim"]["variants"][self.variant]["launch_error"] = "none"
            return True

        def ready_holds(self, _name):
            return True

        def arm_hold(self, _name):
            events.append(("helper_rearm", 0))
            self.lease_acquired_ns = time.perf_counter_ns()
            # lease 須涵蓋 rotation 請求前的排程延遲；break 由 rename 請求觸發（見 DelayedRenameFs）。
            hold_ns = 500_000_000 if failure_point == "break-after-expiry" else 200_000_000
            self.lease_until_ns = self.lease_acquired_ns + hold_ns
            lease_state["until_ns"] = self.lease_until_ns
            self.lease_request_id = "fake-oplock-lease"
            self.rec.observations["av_sim"]["variants"][self.variant]["lease_request_id"] = self.lease_request_id
            return True

        def stop(self):
            close_started_ns = self.lease_until_ns + 2 if failure_point == "break-after-expiry" else self.lease_until_ns
            close_completed_ns = max(time.perf_counter_ns(), close_started_ns + 1)
            self.rec.observations["av_sim"]["variants"][self.variant].update(
                {"grabs": 1, "open_errors": 0, "helper_exit": 0, "oplock": "granted", "oplock_breaks": [{
                    "event": "oplock_break", "request_id": self.lease_request_id, "name": "raw.wal.ndjson",
                    "lease_acquired_ns": self.lease_acquired_ns, "lease_until_ns": self.lease_until_ns,
                    "break_notified_ns": self.lease_until_ns + 1 if failure_point == "break-after-expiry"
                    else lease_state["break_ns"],
                    "break_completed": True, "ack_required": True,
                    "ack_method": "close_handle", "close_started_ns": close_started_ns,
                    "close_completed_ns": close_completed_ns, "close_succeeded": True,
                    "acknowledged_ns": close_completed_ns,
                }]}
            )

    class OracleResult:
        ok = True
        reason_code = "none"
        diagnostics = {}

    monkeypatch.setattr(wal, "WalWriter", FakeWriter)
    monkeypatch.setattr(module, "AvHelper", FakeHelper)
    monkeypatch.setattr(module, "_work_dir", lambda _name: tmp_path / "oplock-work")
    real_glob = module.glob.glob
    def drain_writer(*_args):
        if failure_point == "drain":
            raise OSError("injected drain failure")

    monkeypatch.setattr(module, "_drain_writer", drain_writer)
    monkeypatch.setattr(module.glob, "glob", real_glob)
    monkeypatch.setattr(
        module, "_read_records",
        lambda _path: [] if failure_point == "records" else [{"seq": i} for i in range(1, 404)],
    )
    monkeypatch.setattr(module, "_continuity_meta_count", lambda _records: 0)
    monkeypatch.setattr(module, "_seq_continuous", lambda records: bool(records))
    monkeypatch.setattr(wal_oracle, "open_cursor", lambda _path: object())
    monkeypatch.setattr(wal_oracle, "wait_for_marker", lambda *_args, **_kwargs: OracleResult())
    monkeypatch.setattr(wal_oracle, "verify_record", lambda _row: None)
    real_sleep = time.sleep

    real_default_wal_fs = wal_fs.default_wal_fs

    class DelayedRenameFs:
        def __init__(self, delegate):
            self.delegate = delegate

        def rename_noreplace_or_suffix(self, src, base_dst):
            # 模擬 oplock：rename 請求即觸發 break 通知，helper 持有到 lease 到期才 close（ACK），rename 隨後完成。
            if failure_point == "break-after-expiry":
                real_sleep(0.52)
            else:
                lease_state["break_ns"] = time.perf_counter_ns()
                remaining_s = (lease_state["until_ns"] - time.perf_counter_ns()) / 1e9
                real_sleep(max(0.0, remaining_s) + 0.002)
            return self.delegate.rename_noreplace_or_suffix(src, base_dst)

        def __getattr__(self, name):
            return getattr(self.delegate, name)

    monkeypatch.setattr(wal_fs, "default_wal_fs", lambda: DelayedRenameFs(real_default_wal_fs()))
    monkeypatch.setattr(module.time, "sleep", lambda _seconds: None)
    recorder = module.Recorder()
    recorder.observations["av_sim"] = {
        **module._av_sim_module().describe(SEED),
        "launch_error": "none", "grabs": 1, "open_errors": 0, "helper_exit": 0,
        "variants": {},
    }

    expected_status = "PASS" if failure_point is None else (
        "FAIL" if failure_point == "records" else "INSUFFICIENT_EVIDENCE"
    )
    assert module._collect_oplock_variant(tmp_path, recorder) == expected_status

    workload = recorder.observations["oplock_variant"]
    if failure_point == "records":
        assert workload["seq_continuous"] is False
        return
    if failure_point is not None:
        if failure_point == "break-after-expiry":
            assert workload["oplock_breaks"][0]["break_notified_ns"] >= workload["lease_until_ns"]
            return
        error_key = "oplock_variant_error" if failure_point == "drain" else "oplock_variant_close_error"
        assert error_key in recorder.observations
        return
    assert events.index(("append", 1)) < events.index(("helper_start", 0))
    assert events.index(("helper_start", 0)) < events.index(("helper_rearm", 0))
    assert events.index(("helper_rearm", 0)) < events.index(("append", 2))
    assert workload["append_count"] == 403
    assert workload["archives"] == 1
    assert workload["pre_rotate"]["ok"] is True and workload["post_rotate"]["ok"] is True
    assert workload["seq_continuous"] is True and workload["crc_errors"] == []
    assert workload["rotation_requested_ns"] < workload["lease_until_ns"]
    assert workload["rotation_completed_ns"] > workload["lease_until_ns"]
    assert workload["interference_observed"] is True
    assert workload["rotation_append_index"] <= workload["rotation_completion_append_index"]
    assert workload["rotation_append_started_ns"] <= workload["rotation_requested_ns"]
    assert workload["rotation_append_completed_ns"] >= workload["rotation_requested_ns"]
    assert workload["rotation_completion_append_started_ns"] <= workload["rotation_completed_ns"]
    assert workload["rotation_completion_append_completed_ns"] >= workload["rotation_completed_ns"]


@pytest.mark.core_portable
@pytest.mark.memory_only
def test_oplock_request_uses_overlapped_fsctl_and_records_grant(monkeypatch):
    """FSCTL_REQUEST_OPLOCK 需記錄 break 通知，並以關閉 handle 確認 ACK 時點。"""
    av = _av()
    calls = {}
    closed = []
    clock = iter((111, 333, 444))

    class FakeFunction:
        def __init__(self, callback):
            self.callback = callback

        def __call__(self, *args):
            return self.callback(*args)

    def create_file(*args):
        calls["create_file"] = args
        return 123

    def create_event(*args):
        calls["create_event"] = args
        return 456

    def device_io_control(*args):
        calls["device_io_control"] = args
        args[4]._obj.Flags = av.REQUEST_OPLOCK_OUTPUT_FLAG_ACK_REQUIRED
        return 0

    def get_overlapped_result(*args):
        calls["get_overlapped_result"] = args
        args[2]._obj.value = 0
        return 1

    fake_kernel32 = type("FakeKernel32", (), {})()
    fake_kernel32.CreateFileW = FakeFunction(create_file)
    fake_kernel32.CreateEventW = FakeFunction(create_event)
    fake_kernel32.DeviceIoControl = FakeFunction(device_io_control)
    fake_kernel32.WaitForSingleObject = FakeFunction(lambda *_args: av.WAIT_OBJECT_0)
    fake_kernel32.GetOverlappedResult = FakeFunction(get_overlapped_result)
    fake_kernel32.CloseHandle = FakeFunction(lambda handle: closed.append(handle) or 1)

    lease, status = av._request_oplock(
        r"C:\wal\raw.wal.ndjson", kernel32=fake_kernel32, last_error=lambda: av.ERROR_IO_PENDING
    )
    assert status == "granted"
    assert lease is not None and lease.handle == 123 and lease.event == 456
    assert calls["device_io_control"][1] == av.FSCTL_REQUEST_OPLOCK
    request = calls["device_io_control"][2]._obj
    assert request.StructureVersion == av.REQUEST_OPLOCK_CURRENT_VERSION
    assert request.StructureLength == av.ctypes.sizeof(av.REQUEST_OPLOCK_INPUT_BUFFER)
    assert request.RequestedOplockLevel == av.OPLOCK_LEVEL_CACHE_READ | av.OPLOCK_LEVEL_CACHE_HANDLE
    assert request.Flags == av.REQUEST_OPLOCK_INPUT_FLAG_REQUEST
    assert calls["create_file"][2] & av._FILE_SHARE_DELETE
    assert calls["create_file"][5] & av._FILE_FLAG_OVERLAPPED
    assert closed == []
    assert av._observe_oplock_break(lease, kernel32=fake_kernel32, perf_counter_ns=lambda: next(clock))
    assert lease.break_notified_ns == 111
    assert lease.break_completed is True and lease.break_ack_required is True
    av._close_lease(lease, kernel32=fake_kernel32, perf_counter_ns=lambda: next(clock))
    assert lease.break_ack_method == "close_handle"
    assert lease.break_ack_close_started_ns == 333
    assert lease.break_ack_close_completed_ns == 444
    assert lease.break_ack_close_succeeded is True
    assert lease.break_acknowledged_ns == 444
    assert len(closed) == 2, "確認 oplock break 後須關閉檔案 handle，再關 notification event"


@pytest.mark.core_portable
@pytest.mark.memory_only
def test_oplock_close_failure_does_not_claim_ack(monkeypatch):
    """CloseHandle 失敗時不得留下成功 ACK 區間。"""
    av = _av()
    lease = av.OplockLease(
        handle=123, event=456, overlapped=av._OVERLAPPED(), output=av.REQUEST_OPLOCK_OUTPUT_BUFFER(),
        break_notified_ns=150, break_completed=True, break_ack_required=True,
    )
    clock = iter((220, 230))

    class FakeClose:
        argtypes = None
        restype = None

        @staticmethod
        def __call__(_handle):
            return 0

    api = type("FakeKernel32", (), {"CloseHandle": FakeClose()})()
    av._close_lease(lease, kernel32=api, perf_counter_ns=lambda: next(clock))

    assert lease.break_ack_close_started_ns == 220
    assert lease.break_ack_close_completed_ns == 230
    assert lease.break_ack_close_succeeded is False
    assert lease.break_ack_method is None
    assert lease.break_acknowledged_ns is None


@pytest.mark.posix_only
@pytest.mark.memory_only
def test_av_sim_helper_rejects_non_windows_host(tmp_path):
    av = _av()
    rc = av.main(
        ["--watch-dir", str(tmp_path), "--seed", "1", "--duration-s", "0",
         "--ready-file", str(tmp_path / "r"), "--stop-file", str(tmp_path / "s"),
         "--log-file", str(tmp_path / "l"), "--go-file", str(tmp_path / "g"),
         "--ack-file", str(tmp_path / "a")]
    )
    assert rc == av.EXIT_NOT_WINDOWS
    assert not (tmp_path / "r").exists(), "非 Windows 不得產生 ready 旗標（不得偽裝為已佔用）"


@pytest.mark.windows_native
@pytest.mark.process_rpc
def test_native_av_sim_helper_holds_wal_and_writer_completes(tmp_path):
    from sw_core.wal import WalWriter
    from sw_core.wal_fs import WindowsWalFs

    wal_dir = tmp_path / "wal"
    wal_dir.mkdir()
    (wal_dir / "raw.wal.ndjson").write_bytes(b"")
    ready = tmp_path / "ready.flag"
    stop = tmp_path / "stop.flag"
    log = tmp_path / "helper.jsonl"
    proc = subprocess.Popen(
        [sys.executable, str(AV_SIM_PATH), "--watch-dir", str(wal_dir), "--seed", str(SEED),
         "--duration-s", "120", "--ready-file", str(ready), "--stop-file", str(stop),
         "--log-file", str(log), "--go-file", str(tmp_path / "helper.go"),
         "--ack-file", str(tmp_path / "helper.ack")]
    )
    try:
        ready_record = _wait_ready_record(ready, 10.0)
        assert "raw.wal.ndjson" in ready_record.get("held_files", []), (
            "ready 必須表示首次目錄掃描已實際持有啟動前存在的 WAL"
        )
        w = WalWriter(wal_dir=str(wal_dir), rotate_bytes=8192, fs=WindowsWalFs())
        started = time.monotonic()
        count = 0
        samples_ms: list[float] = []
        while time.monotonic() - started < 3.0:  # 持續寫入，使多次輪替與持有窗口重疊
            _timed_append(w, samples_ms, com="COM0", direction="TX", source="av_sim", payload=b"x" * 64)
            count += 1
            w.tick()
        deadline = time.monotonic() + 10.0
        while time.monotonic() < deadline:
            w.tick()
            if w.health()["deferred_delete_pending"] == 0:
                break
            time.sleep(0.05)
        health = w.health()
        w.close()
        assert health["rotate_retry"] >= 1, "原生 helper 應實際造成共用違規（rotate_retry ≥ 1）"
        assert health["deferred_delete_pending"] == 0, "干擾結束後延後佇列必須清空"
        assert len(samples_ms) == count and samples_ms and all(math.isfinite(value) and value >= 0 for value in samples_ms)
        rows = _read_all_rows(wal_dir)
        seqs = [int(row["seq"]) for row in rows]
        data_rows = [row for row in rows if not _is_continuity_meta(row)]
        assert seqs == list(range(1, len(rows) + 1)), "資料與延後輪替 META 的 seq 必須連續"
        assert len(data_rows) == count, "所有 append 資料皆須保留（連續性 META 另計）"
    finally:
        stop.write_text("stop\n", encoding="utf-8")
        rc = proc.wait(timeout=60)
    assert rc == 0
    summary = [json.loads(line) for line in log.read_text(encoding="utf-8").splitlines()][-1]
    assert summary["event"] == "summary" and summary["grabs"] >= 1


@pytest.mark.windows_native
@pytest.mark.process_rpc
def test_native_av_sim_oplock_variant_keeps_wal_writes_and_rotation_live(tmp_path):
    """原生 Windows 檔案系統記錄 RH break 通知與 close ACK，並驗證 WAL 寫入／輪替。"""
    from sw_core.wal import WalWriter
    from sw_core.wal_fs import WindowsWalFs

    wal_dir = tmp_path / "wal-oplock"
    wal_dir.mkdir()
    ready = tmp_path / "oplock-ready.flag"
    stop = tmp_path / "oplock-stop.flag"
    log = tmp_path / "oplock-helper.jsonl"
    proc = subprocess.Popen(
        [sys.executable, str(AV_SIM_PATH), "--watch-dir", str(wal_dir), "--seed", str(SEED),
         "--duration-s", "30", "--ready-file", str(ready), "--stop-file", str(stop),
         "--log-file", str(log), "--go-file", str(tmp_path / "oplock.go"),
         "--ack-file", str(tmp_path / "oplock.ack"), "--oplock"]
    )
    writer = None
    try:
        _wait_ready_record(ready, 10.0)
        writer = WalWriter(wal_dir=str(wal_dir), rotate_bytes=8192, fs=WindowsWalFs())
        samples_ms: list[float] = []
        _timed_append(writer, samples_ms, com="COM0", direction="TX", source="av_sim_oplock", payload=b"first")
        time.sleep(0.1)
        for index in range(96):
            _timed_append(writer, samples_ms, com="COM0", direction="TX", source="av_sim_oplock",
                          payload=(b"x" * 128) + str(index).encode())
            writer.tick()
        stop.write_text("stop\n", encoding="utf-8")
        rc = proc.wait(timeout=60)
        assert rc == 0
        deadline = time.monotonic() + 10.0
        while writer.health()["deferred_delete_pending"] and time.monotonic() < deadline:
            writer.tick()
            time.sleep(0.05)
        health = writer.health()
        writer.close()
        writer = None
        assert health["deferred_delete_pending"] == 0
        assert len(samples_ms) == 97 and all(math.isfinite(value) and value >= 0 for value in samples_ms)
        assert [path for path in wal_dir.iterdir() if path.name.startswith("raw.wal.ndjson.")]
        seqs = _read_all_seqs(wal_dir)
        assert seqs == list(range(1, len(seqs) + 1)), "oplock 變體期間 WAL seq 必須連續"
        log_rows = [json.loads(line) for line in log.read_text(encoding="utf-8").splitlines()]
        summary = log_rows[-1]
        assert summary["event"] == "summary" and summary["grabs"] >= 1
        assert summary["oplock"] == "granted"
        break_rows = [row for row in log_rows if row.get("event") == "oplock_break"]
        assert break_rows, "rotation 應觸發並記錄實際 RH oplock break 通知"
        assert any(
            row["break_completed"] is True and row["ack_required"] is True
            and row["ack_method"] == "close_handle"
            and isinstance(row["break_notified_ns"], int)
            and row["close_succeeded"] is True
            and isinstance(row["close_started_ns"], int)
            and isinstance(row["close_completed_ns"], int)
            and row["close_started_ns"] <= row["close_completed_ns"]
            and isinstance(row["acknowledged_ns"], int)
            and row["acknowledged_ns"] == row["close_completed_ns"]
            for row in break_rows
        ), "oplock break 必須完成，並記錄關閉 handle 的隱含 ACK"
    finally:
        if writer is not None:
            writer.close()
        if proc.poll() is None:
            stop.write_text("stop\n", encoding="utf-8")
            proc.wait(timeout=60)


@pytest.mark.core_portable
@pytest.mark.memory_only
def test_spike3_measured_writer_records_append_latency(monkeypatch):
    """append 延遲量測使用高解析計時器，避免 15.625 ms clock 將短寫入量化為零。"""
    import importlib.util

    script = REPO_ROOT / "scripts" / "stage0" / "spike3_win_wal.py"
    spec = importlib.util.spec_from_file_location("spike3_win_wal_test", script)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    perf_ticks = iter([1_000_000, 3_500_000, 10_000_000, 13_250_000])
    coarse_ticks = iter([1_000_000_000, 1_015_625_000, 1_031_250_000, 1_046_875_000])
    monkeypatch.setattr(module.time, "perf_counter_ns", lambda: next(perf_ticks))
    monkeypatch.setattr(module.time, "monotonic_ns", lambda: next(coarse_ticks))
    rec = module.Recorder()

    class FakeWriter:
        def append(self, **kwargs):
            return {"seq": 1}

    writer = module._MeasuredWalWriter(FakeWriter(), rec)
    assert writer.append(com="COM0", direction="TX", payload=b"a") == {"seq": 1}
    assert writer.append(com="COM0", direction="TX", payload=b"b") == {"seq": 1}
    assert rec.observations["append_latency_ms"] == [2.5, 3.25]
    assert rec.observations["append_count"] == len(rec.observations["append_latency_ms"])


@pytest.mark.core_portable
@pytest.mark.memory_only
def test_spike3_main_helper_start_failure_records_not_requested_oplock(tmp_path):
    """主 helper 啟動失敗時仍須符合 schema 的 not_requested oplock 欄位。"""
    import importlib.util

    script = REPO_ROOT / "scripts" / "stage0" / "spike3_win_wal.py"
    spec = importlib.util.spec_from_file_location("spike3_helper_failure_test", script)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    rec = module.Recorder()
    helper = module.AvHelper(tmp_path, module.SPIKE3_SEED, rec)
    helper._fail("PermissionError: helper launch refused")

    helper.stop()

    assert rec.observations["av_sim"]["oplock"] == "not_requested"


@pytest.mark.core_portable
@pytest.mark.memory_only
def test_spike3_writes_insufficient_evidence_when_schema_validation_fails(monkeypatch, tmp_path):
    """證據 schema 驗證異常須留下 INSUFFICIENT_EVIDENCE 檔供診斷。"""
    import importlib.util
    import jsonschema

    script = REPO_ROOT / "scripts" / "stage0" / "spike3_win_wal.py"
    spec = importlib.util.spec_from_file_location("spike3_validation_failure_test", script)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    real_os = module.os
    case_called = False
    edr_called = False

    class WindowsOSProxy:
        name = "nt"

        def __getattr__(self, name):
            return getattr(real_os, name)

    def fake_case(rec, _args):
        nonlocal case_called
        case_called = True
        rec.observations["av_sim"] = {
            **module._av_sim_module().describe(module.SPIKE3_SEED),
            "launch_error": "none", "grabs": 1, "open_errors": 0, "helper_exit": 0,
        }
        return "PASS", "none"

    def fake_query_edr():
        nonlocal edr_called
        edr_called = True
        return {"product": "test", "status": "unknown", "interceptions": "unknown"}

    def fail_validation(_evidence):
        raise jsonschema.ValidationError("injected schema failure")

    monkeypatch.setattr(module, "os", WindowsOSProxy())
    monkeypatch.setattr(module, "query_edr", fake_query_edr)
    monkeypatch.setattr(module, "gather_env", lambda *_args: {"os": "Windows 11"})
    monkeypatch.setitem(module.CASE_FUNCS, module.CASE_IDS[0], fake_case)
    monkeypatch.setattr(module, "_validate", fail_validation)
    evidence_dir = tmp_path / "evidence"
    rc = module.run_case(module.argparse.Namespace(
        case=module.CASE_IDS[0], evidence_dir=str(evidence_dir), unlink_mode="classic",
    ))

    evidence = json.loads((evidence_dir / f"{module.CASE_IDS[0]}.json").read_text(encoding="utf-8"))
    assert rc != 0
    assert case_called and edr_called
    assert evidence["verdict"] == "INSUFFICIENT_EVIDENCE"
    assert "injected schema failure" in evidence["observations"]["evidence_validation_error"]
    timing = evidence["env"]
    clock_info = time.get_clock_info("perf_counter")
    assert timing["timing_method"] == "time.perf_counter_ns"
    assert timing["timing_clock"] == clock_info.implementation
    assert timing["timing_resolution_ns"] == max(1, math.ceil(clock_info.resolution * 1_000_000_000))
    assert timing["timing_monotonic"] is True and timing["timing_adjustable"] is False


@pytest.mark.core_portable
@pytest.mark.memory_only
def test_spike3_oplock_variant_exception_still_writes_evidence(monkeypatch, tmp_path):
    """oplock 子變體的 prefill 失敗時，run_case 仍須落下不足證據 JSON。"""
    import importlib.util
    import json

    script = REPO_ROOT / "scripts" / "stage0" / "spike3_win_wal.py"
    spec = importlib.util.spec_from_file_location("spike3_variant_exception_test", script)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    real_os = module.os

    class WindowsOSProxy:
        name = "nt"

        def __getattr__(self, name):
            return getattr(real_os, name)

    def fake_case(rec, _args):
        (tmp_path / "raw.wal.ndjson").write_bytes(b"prefill\n")
        rec.observations["wal_dir"] = str(tmp_path)
        rec.observations["av_sim"] = {
            **module._av_sim_module().describe(module.SPIKE3_SEED),
            "launch_error": "none", "grabs": 1, "open_errors": 0, "helper_exit": 0,
        }
        return "PASS", "none"

    monkeypatch.setattr(module, "os", WindowsOSProxy())
    monkeypatch.setattr(
        module, "query_edr",
        lambda: {"product": "test", "status": "unknown", "interceptions": "unknown"},
    )
    monkeypatch.setattr(module, "gather_env", lambda *_args: {"os": "Windows 11"})
    monkeypatch.setitem(module.CASE_FUNCS, module.CASE_IDS[0], fake_case)
    monkeypatch.setattr(
        module, "_collect_oplock_variant",
        lambda *_args: (_ for _ in ()).throw(OSError("injected oplock prefill failure")),
    )
    snapshots = {"test-binary": {"exists": True, "sha256": "a" * 64}}
    monkeypatch.setattr(module, "binary_subject_paths", lambda: ["test-binary"])
    monkeypatch.setattr(module, "snapshot_binaries", lambda _paths: snapshots)
    evidence_dir = tmp_path / "evidence"

    raised = None
    try:
        rc = module.run_case(module.argparse.Namespace(
            case=module.CASE_IDS[0], evidence_dir=str(evidence_dir), unlink_mode="classic",
        ))
    except Exception as exc:  # 保持 assertion failure，而非讓目前外拋變成 pytest error。
        raised = exc
        rc = None

    evidence_path = evidence_dir / f"{module.CASE_IDS[0]}.json"
    assert raised is None, f"run_case 不得讓 oplock 子變體例外穿出：{raised!r}"
    assert rc == 1
    assert evidence_path.is_file()
    evidence = json.loads(evidence_path.read_text(encoding="utf-8"))
    assert evidence["verdict"] == "INSUFFICIENT_EVIDENCE"
    assert "injected oplock prefill failure" in evidence["observations"]["oplock_variant_error"]


@pytest.mark.core_portable
@pytest.mark.memory_only
def test_spike3_helper_retries_until_partial_ready_record_is_complete(monkeypatch, tmp_path):
    """ready 檔短暫可見但尚未完整時，consumer 應重試而非立即拒絕。"""
    import importlib.util

    script = REPO_ROOT / "scripts" / "stage0" / "spike3_win_wal.py"
    spec = importlib.util.spec_from_file_location("spike3_partial_ready_test", script)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    rec = module.Recorder()
    helper = module.AvHelper(tmp_path, module.SPIKE3_SEED, rec)
    helper._ready.write_text("{", encoding="utf-8")

    class FakeProcess:
        returncode = 0

        def poll(self):
            return None

        def wait(self, timeout=None):
            return 0

    monkeypatch.setattr(module.subprocess, "Popen", lambda _argv: FakeProcess())
    original_sleep = module.time.sleep
    published = False

    def publish_complete_record(delay):
        nonlocal published
        if not published:
            published = True
            helper._ready.write_text(json.dumps({
                "ready": True, "held_files": ["raw.wal.ndjson"],
            }), encoding="utf-8")
        original_sleep(min(delay, 0.001))

    monkeypatch.setattr(module.time, "sleep", publish_complete_record)
    assert helper.start()
    assert helper.ready_holds("raw.wal.ndjson")
    helper.stop()


@pytest.mark.core_portable
@pytest.mark.memory_only
def test_av_sim_ready_record_uses_atomic_replace(monkeypatch, tmp_path):
    """ready JSON 先完整寫入同目錄暫存檔，再以 replace 發布。"""
    av = _av()
    ready = tmp_path / "ready.flag"
    replace = av.os.replace
    published: list[tuple[Path, Path]] = []

    def checked_replace(source, destination):
        source_path, destination_path = Path(source), Path(destination)
        assert source_path.parent == destination_path.parent == tmp_path
        assert json.loads(source_path.read_text(encoding="utf-8")) == {
            "ready": True, "held_files": ["raw.wal.ndjson"],
        }
        assert not destination_path.exists()
        published.append((source_path, destination_path))
        replace(source, destination)

    monkeypatch.setattr(av.os, "replace", checked_replace)
    av._publish_ready_record(ready, ["raw.wal.ndjson"])

    assert len(published) == 1
    assert json.loads(ready.read_text(encoding="utf-8"))["ready"] is True
    assert not published[0][0].exists()


@pytest.mark.core_portable
@pytest.mark.memory_only
def test_av_sim_ready_publication_retries_permission_errors(monkeypatch, tmp_path):
    """Windows scanner 短暫佔用暫存檔時，有界重試後仍須完整發布 ready。"""
    av = _av()
    ready = tmp_path / "ready.flag"
    real_replace = av.os.replace
    attempts = 0

    def flaky_replace(source, destination):
        nonlocal attempts
        attempts += 1
        if attempts <= 2:
            raise PermissionError("injected sharing violation")
        real_replace(source, destination)

    monkeypatch.setattr(av.os, "replace", flaky_replace)
    av._publish_ready_record(ready, ["raw.wal.ndjson"])

    assert attempts == 3
    assert json.loads(ready.read_text(encoding="utf-8")) == {
        "ready": True, "held_files": ["raw.wal.ndjson"],
    }
    assert not list(tmp_path.glob(".ready.flag.*.tmp"))


@pytest.mark.core_portable
@pytest.mark.memory_only
def test_av_sim_rearm_request_reopens_seen_wal_and_acks_live_lease(monkeypatch, tmp_path):
    """已在首次掃描 seen 集合中的 WAL 仍可由 go 握手重新開啟並回傳 lease。"""
    av = _av()
    wal_path = tmp_path / "raw.wal.ndjson"
    wal_path.write_bytes(b"prefill\n")
    go_file = tmp_path / "helper.go"
    ack_file = tmp_path / "helper.ack"
    go_file.write_text(json.dumps({"request_id": "test-1", "name": wal_path.name}), encoding="utf-8")
    opened: list[str] = []
    monkeypatch.setattr(av, "_open_share_hold", lambda path: opened.append(path) or 123)
    holds = []
    seen_request_ids: set[str] = set()
    counts = {"grabs": 0, "open_errors": 0}
    events: list[dict[str, Any]] = []

    av._process_rearm_request(
        watch_dir=str(tmp_path), go_file=str(go_file), ack_file=str(ack_file),
        seen_request_ids=seen_request_ids, holds=holds, schedule=av.HoldSchedule(SEED),
        counts=counts, events=events, oplock=False,
    )
    av._process_rearm_request(
        watch_dir=str(tmp_path), go_file=str(go_file), ack_file=str(ack_file),
        seen_request_ids=seen_request_ids, holds=holds, schedule=av.HoldSchedule(SEED),
        counts=counts, events=events, oplock=False,
    )

    ack = json.loads(ack_file.read_text(encoding="utf-8"))
    assert opened == [str(wal_path)]
    assert ack["request_id"] == "test-1" and ack["held_file"] == wal_path.name
    assert 10 <= ack["hold_ms"] <= 500
    assert len(holds) == 1 and holds[0].lease.handle == 123 and len(events) == 1
    assert (holds[0].until_ns - holds[0].acquired_ns > 10_000_000
            and counts["grabs"] == 1)
    assert events[0]["event"] == "rearm"


@pytest.mark.core_portable
@pytest.mark.memory_only
def test_spike3_helper_requires_existing_wal_and_confirmed_handle(monkeypatch, tmp_path):
    """Spike 3 helper 僅能在 WAL 已存在且 ready 確認持有後回傳。"""
    import importlib.util

    script = REPO_ROOT / "scripts" / "stage0" / "spike3_win_wal.py"
    spec = importlib.util.spec_from_file_location("spike3_helper_barrier_test", script)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    events: list[tuple[str, bool]] = []

    class FakeHelper:
        def __init__(self, wal_dir, _seed, _rec, *, oplock=False, variant=None):
            self.wal_path = Path(wal_dir) / "raw.wal.ndjson"
            events.append(("constructed", self.wal_path.is_file()))

        def start(self):
            events.append(("start", self.wal_path.is_file()))
            return True

        def ready_holds(self, name):
            events.append(("ready_holds", name == "raw.wal.ndjson" and self.wal_path.is_file()))
            return self.wal_path.is_file() and name == "raw.wal.ndjson"

        def arm_hold(self, name):
            events.append(("arm_hold", name == "raw.wal.ndjson" and self.wal_path.is_file()))
            return self.wal_path.is_file() and name == "raw.wal.ndjson"

        def stop(self):
            events.append(("stop", self.wal_path.is_file()))

    monkeypatch.setattr(module, "AvHelper", FakeHelper)
    rec = module.Recorder()
    with pytest.raises(RuntimeError, match="WAL 必須先存在"):
        module._start_av_helper_with_wal(tmp_path, rec)
    assert events == []

    (tmp_path / "raw.wal.ndjson").write_bytes(b"prefill\n")
    helper = module._start_av_helper_with_wal(tmp_path, rec)
    assert isinstance(helper, FakeHelper)
    assert events == [
        ("constructed", True), ("start", True), ("ready_holds", True), ("arm_hold", True),
    ]


@pytest.mark.core_portable
@pytest.mark.memory_only
def test_spike3_helper_stops_when_ready_does_not_hold_wal(monkeypatch, tmp_path):
    """沒有取得既存 WAL 的 helper 必須先停止，再拒絕讓案例開始。"""
    import importlib.util

    script = REPO_ROOT / "scripts" / "stage0" / "spike3_win_wal.py"
    spec = importlib.util.spec_from_file_location("spike3_helper_failed_barrier_test", script)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    (tmp_path / "raw.wal.ndjson").write_bytes(b"prefill\n")
    events: list[str] = []

    class FakeHelper:
        def __init__(self, *_args, **_kwargs):
            pass

        def start(self):
            events.append("start")
            return True

        def ready_holds(self, _name):
            events.append("ready_holds")
            return False

        def stop(self):
            events.append("stop")

    monkeypatch.setattr(module, "AvHelper", FakeHelper)
    with pytest.raises(RuntimeError, match="首次掃描未持有"):
        module._start_av_helper_with_wal(tmp_path, module.Recorder())
    assert events == ["start", "ready_holds", "stop"]


@pytest.mark.core_portable
@pytest.mark.memory_only
def test_spike3_helper_rearms_after_delayed_ready(monkeypatch, tmp_path):
    """ready 讀取延遲超過最長 lease 後，刺激前必須重新取得有效持有。"""
    import importlib.util

    script = REPO_ROOT / "scripts" / "stage0" / "spike3_win_wal.py"
    spec = importlib.util.spec_from_file_location("spike3_delayed_ready_rearm_test", script)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    (tmp_path / "raw.wal.ndjson").write_bytes(b"prefill\n")
    events: list[str] = []
    real_sleep = time.sleep

    class FakeHelper:
        def __init__(self, *_args, **_kwargs):
            pass

        def start(self):
            real_sleep(0.55)
            return True

        def ready_holds(self, name):
            events.append(("ready_holds", name))
            return name == "raw.wal.ndjson"

        def arm_hold(self, name):
            events.append(("arm_hold", name))
            return True

        def stop(self):
            events.append(("stop", ""))

    monkeypatch.setattr(module, "AvHelper", FakeHelper)
    module._start_av_helper_with_wal(tmp_path, module.Recorder())
    assert events == [
        ("ready_holds", "raw.wal.ndjson"),
        ("arm_hold", "raw.wal.ndjson"),
    ]


@pytest.mark.core_portable
@pytest.mark.memory_only
def test_spike3_arm_hold_rearms_expired_ack_before_accepting(monkeypatch, tmp_path):
    """ACK 送達 consumer 前 lease 已過期時，必須重送 go 並取得仍有效的 lease。"""
    import importlib.util
    import json
    import threading

    script = REPO_ROOT / "scripts" / "stage0" / "spike3_win_wal.py"
    spec = importlib.util.spec_from_file_location("spike3_expired_ack_test", script)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    recorder = module.Recorder()
    helper = module.AvHelper(tmp_path, SEED, recorder)
    (tmp_path / "raw.wal.ndjson").write_bytes(b"prefill\n")

    class RunningProcess:
        @staticmethod
        def poll():
            return None

    helper._proc = RunningProcess()
    stop = threading.Event()
    request_ids: set[str] = set()

    def serve_go_requests():
        while not stop.is_set():
            try:
                request = json.loads(helper._go.read_text(encoding="utf-8"))
            except (OSError, ValueError):
                time.sleep(0.001)
                continue
            request_id = request.get("request_id")
            if not isinstance(request_id, str) or request_id in request_ids:
                time.sleep(0.001)
                continue
            request_ids.add(request_id)
            now_ns = time.perf_counter_ns()
            if len(request_ids) == 1:
                acquired_ns, until_ns = now_ns - 200_000_000, now_ns - 1
            else:
                acquired_ns, until_ns = now_ns, now_ns + 200_000_000
            helper._ack.write_text(
                json.dumps({
                    "request_id": request_id, "held_file": "raw.wal.ndjson", "hold_ms": 200,
                    "acquired_ns": acquired_ns, "until_ns": until_ns,
                }),
                encoding="utf-8",
            )

    server = threading.Thread(target=serve_go_requests, daemon=True)
    server.start()
    try:
        assert helper.arm_hold("raw.wal.ndjson") is True
        assert len(request_ids) >= 2
        assert helper.lease_acquired_ns <= time.perf_counter_ns() < helper.lease_until_ns
    finally:
        stop.set()
        server.join(1)


@pytest.mark.core_portable
@pytest.mark.memory_only
def test_spike3_arm_hold_never_accepts_only_expired_acknowledgements(monkeypatch, tmp_path):
    """helper 每次 ACK 的 lease 都已到期（或剩餘不足安全邊界）時，真實 arm_hold 必須逾時失敗、不得記錄 lease。"""
    import importlib.util
    import json
    import threading

    script = REPO_ROOT / "scripts" / "stage0" / "spike3_win_wal.py"
    spec = importlib.util.spec_from_file_location("spike3_always_expired_ack_test", script)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    module.AV_HELPER_READY_TIMEOUT_S = 0.3
    recorder = module.Recorder()
    helper = module.AvHelper(tmp_path, SEED, recorder)
    (tmp_path / "raw.wal.ndjson").write_bytes(b"prefill\n")

    class RunningProcess:
        returncode = None

        @staticmethod
        def poll():
            return None

    helper._proc = RunningProcess()
    stop = threading.Event()
    request_ids: set[str] = set()

    def serve_expired_acks():
        while not stop.is_set():
            try:
                request = json.loads(helper._go.read_text(encoding="utf-8"))
            except (OSError, ValueError):
                time.sleep(0.001)
                continue
            request_id = request.get("request_id")
            if not isinstance(request_id, str) or request_id in request_ids:
                time.sleep(0.001)
                continue
            request_ids.add(request_id)
            now_ns = time.perf_counter_ns()
            # 交替送出「已到期」與「剩餘時間落在安全邊界內」的 lease；兩者都不得被接受。
            remaining_ns = -1 if len(request_ids) % 2 else module.AV_HELPER_LEASE_SAFETY_NS // 2
            until_ns = now_ns + remaining_ns
            helper._ack.write_text(
                json.dumps({
                    "request_id": request_id, "held_file": "raw.wal.ndjson", "hold_ms": 200,
                    "acquired_ns": until_ns - 200_000_000, "until_ns": until_ns,
                }),
                encoding="utf-8",
            )

    server = threading.Thread(target=serve_expired_acks, daemon=True)
    server.start()
    try:
        assert helper.arm_hold("raw.wal.ndjson") is False
        assert len(request_ids) >= 2, "過期 ACK 必須觸發重新 go，而不是只讀一次"
        assert helper.lease_acquired_ns is None and helper.lease_until_ns is None
        assert "逾時" in (helper.launch_error or "")
    finally:
        stop.set()
        server.join(1)


@pytest.mark.core_portable
@pytest.mark.memory_only
@pytest.mark.parametrize(
    "response_mode",
    ["wrong_request_id", "wrong_held_file", "hold_too_short", "hold_too_long", "helper_error", "invalid_times", "helper_exited", "timeout"],
)
def test_spike3_arm_hold_rejects_invalid_acknowledgements(monkeypatch, tmp_path, response_mode):
    """go／ack 不符、helper 錯誤、無效 lease、提前退出或逾時都不得取得成功。"""
    import importlib.util
    import json
    import threading

    script = REPO_ROOT / "scripts" / "stage0" / "spike3_win_wal.py"
    spec = importlib.util.spec_from_file_location("spike3_invalid_ack_test", script)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    module.AV_HELPER_READY_TIMEOUT_S = 0.05
    real_sleep = time.sleep
    monkeypatch.setattr(module.time, "sleep", lambda _seconds: real_sleep(0.001))
    helper = module.AvHelper(tmp_path, SEED, module.Recorder())

    class RunningProcess:
        returncode = 1 if response_mode == "helper_exited" else None

        @staticmethod
        def poll():
            return 1 if response_mode == "helper_exited" else None

    helper._proc = RunningProcess()
    stop = threading.Event()

    def write_response():
        while not stop.is_set() and not helper._go.exists():
            time.sleep(0.001)
        if response_mode in ("timeout", "helper_exited") or stop.is_set():
            return
        request = json.loads(helper._go.read_text(encoding="utf-8"))
        now_ns = time.perf_counter_ns()
        response = {
            "request_id": request["request_id"], "held_file": "raw.wal.ndjson", "hold_ms": 200,
            "acquired_ns": now_ns, "until_ns": now_ns + 200_000_000,
        }
        if response_mode == "wrong_request_id":
            response["request_id"] += "-wrong"
        elif response_mode == "wrong_held_file":
            response["held_file"] = "other.wal.ndjson"
        elif response_mode == "hold_too_short":
            response["hold_ms"] = 9
        elif response_mode == "hold_too_long":
            response["hold_ms"] = 501
            response["until_ns"] = response["acquired_ns"] + 501_000_000
        elif response_mode == "helper_error":
            response["error"] = "PermissionError: injected"
        elif response_mode == "invalid_times":
            response["until_ns"] = response["acquired_ns"]
        helper._ack.write_text(json.dumps(response), encoding="utf-8")

    server = threading.Thread(target=write_response, daemon=True)
    server.start()
    try:
        assert helper.arm_hold("raw.wal.ndjson") is False
        assert helper.launch_error
    finally:
        stop.set()
        server.join(1)


@pytest.mark.core_portable
@pytest.mark.memory_only
def test_av_sim_rearm_ack_records_monotonic_lease_bounds(monkeypatch, tmp_path):
    """av_sim ACK 必須記錄實際取得 handle 後的單調時鐘 lease 起訖。"""
    av = _av()
    wal_path = tmp_path / "raw.wal.ndjson"
    wal_path.write_bytes(b"prefill\n")
    go_file = tmp_path / "go.json"
    ack_file = tmp_path / "ack.json"
    go_file.write_text(json.dumps({"request_id": "request-1", "name": wal_path.name}), encoding="utf-8")

    class Schedule:
        @staticmethod
        def next_hold_s():
            return 0.2

    class Handle:
        @staticmethod
        def close():
            pass

    monkeypatch.setattr(av, "_open_share_hold", lambda _path: Handle())
    holds = []
    av._process_rearm_request(
        watch_dir=str(tmp_path), go_file=str(go_file), ack_file=str(ack_file),
        seen_request_ids=set(), holds=holds, schedule=Schedule(),
        counts={"grabs": 0, "open_errors": 0}, events=[], oplock=False,
    )

    ack = json.loads(ack_file.read_text(encoding="utf-8"))
    assert ack["held_file"] == wal_path.name
    assert isinstance(ack.get("acquired_ns"), int)
    assert isinstance(ack.get("until_ns"), int)
    assert 199_000_000 <= ack["until_ns"] - ack["acquired_ns"] <= 201_000_000
    assert holds and holds[0].until_ns == ack["until_ns"]


@pytest.mark.core_portable
@pytest.mark.memory_only
def test_av_sim_hold_expiration_uses_qpc_deadline(monkeypatch):
    """粗時鐘已超過 hold 秒數時，helper 仍須持有到 QPC ACK 截止時間。"""
    av = _av()
    qpc_now_ns = [125_600_000]
    monotonic_now = [0.1256]
    monkeypatch.setattr(av.time, "perf_counter_ns", lambda: qpc_now_ns[0])
    monkeypatch.setattr(av.time, "monotonic", lambda: monotonic_now[0])
    held = av.HeldFile(
        until_ns=128_600_000, lease=av.OplockLease(handle=123), name="raw.wal.ndjson",
        acquired_ns=15_600_000,
    )
    holds = [held]
    released = []
    monkeypatch.setattr(av, "_release_held_file", lambda entry, _events: released.append(entry))

    av._release_expired_holds(holds, [], oplock=False)
    assert holds == [held] and released == []

    qpc_now_ns[0] = 128_600_000
    av._release_expired_holds(holds, [], oplock=False)
    assert holds == [] and released == [held]


@pytest.mark.core_portable
@pytest.mark.memory_only
def test_case_rotate_runs_only_after_live_helper_rearm(monkeypatch, tmp_path):
    """case_rotate 先建 WAL、啟動並重新武裝 helper，再執行剩餘 399 次 append。"""
    import importlib.util
    from sw_core import wal

    script = REPO_ROOT / "scripts" / "stage0" / "spike3_win_wal.py"
    spec = importlib.util.spec_from_file_location("spike3_rotate_case_order_test", script)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    events: list[tuple[str, int]] = []
    clock_ns = [1_000_000_000]

    def perf_counter_ns() -> int:
        value = clock_ns[0]
        clock_ns[0] += 40_000_000
        return value

    monkeypatch.setattr(module.time, "perf_counter_ns", perf_counter_ns)

    class FakeWriter:
        archive_created = False

        def __init__(self, *, wal_dir, rotate_bytes):
            self.wal_dir = Path(wal_dir)
            self.wal_dir.mkdir(parents=True, exist_ok=True)
            self.index = 0

        def append(self, **_kwargs):
            self.index += 1
            if self.index == 1:
                (self.wal_dir / "raw.wal.ndjson").write_bytes(b"prefill\n")
            if self.index == 3:
                # 首次 lease 內 rotation request 受阻；下一次 append 才成功建立 archive。
                type(self).archive_created = True
            events.append(("append", self.index))
            return {"rotation_failed": self.index == 2}

        def health(self):
            return {"rotate_retry": 1, "rotate_deferred": 0, "deferred_delete_pending": 0}

        def close(self):
            pass

        def tick(self):
            pass

    class FakeHelper:
        def __init__(self, _wal_dir, _seed, rec, **_kwargs):
            self.rec = rec
            self.lease_acquired_ns = 0
            self.lease_until_ns = 0

        def start(self):
            events.append(("helper_start", 0))
            self.rec.observations["av_sim"] = {"grabs": 1, "launch_error": "none"}
            self.lease_acquired_ns = clock_ns[0]
            self.lease_until_ns = self.lease_acquired_ns + 20_000_000
            return True

        def ready_holds(self, _name):
            events.append(("ready", 0))
            return True

        def arm_hold(self, _name):
            events.append(("rearm", 0))
            return True

        def stop(self):
            events.append(("helper_stop", 0))

    monkeypatch.setattr(wal, "WalWriter", FakeWriter)
    monkeypatch.setattr(module, "AvHelper", FakeHelper)
    monkeypatch.setattr(module, "_work_dir", lambda _name: tmp_path)
    monkeypatch.setattr(module, "_drain_writer", lambda *_args: None)
    monkeypatch.setattr(module.glob, "glob", lambda _pattern: [
        str(tmp_path / "raw.wal.ndjson"),
        *([str(tmp_path / "raw.wal.ndjson.1")] if FakeWriter.archive_created else []),
    ])
    monkeypatch.setattr(module, "_read_records", lambda _path: [{"seq": i} for i in range(1, 401)])
    monkeypatch.setattr(module, "_continuity_meta_count", lambda _records: 0)
    monkeypatch.setattr(module, "_seq_continuous", lambda records: bool(records))
    rec = module.Recorder()

    status, _reason = module.case_rotate(rec, module.argparse.Namespace())

    assert status == "PASS"
    assert events[0] == ("append", 1)
    assert events.index(("append", 1)) < events.index(("helper_start", 0))
    assert events.index(("helper_start", 0)) < events.index(("rearm", 0))
    assert events.index(("rearm", 0)) < events.index(("append", 2))
    assert len([event for event in events if event[0] == "append"]) == 400
    assert rec.observations["lease_acquired_ns"] <= rec.observations["stimulus_started_ns"]
    assert rec.observations["stimulus_started_ns"] < rec.observations["lease_until_ns"]


@pytest.mark.core_portable
@pytest.mark.memory_only
def test_spike3_case_barriers_and_atomic_ready_call_sites_are_wired():
    """四案共用預填 WAL／helper barrier，helper ready 使用原子發布函式。"""
    import ast

    source = (REPO_ROOT / "scripts" / "stage0" / "spike3_win_wal.py").read_text(encoding="utf-8")
    tree = ast.parse(source)
    functions = {
        node.name: node for node in ast.walk(tree)
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
    }
    case_names = ("case_rotate", "case_unlink", "case_reader_handle", "case_f12_progress")
    for name in case_names:
        calls = [node for node in ast.walk(functions[name]) if isinstance(node, ast.Call)]
        assert any(isinstance(call.func, ast.Name) and call.func.id == "_start_av_helper_with_wal" for call in calls)
        assert not any(isinstance(call.func, ast.Name) and call.func.id == "AvHelper" for call in calls)
        append_lines = [call.lineno for call in calls if isinstance(call.func, ast.Attribute)
                        and call.func.attr == "append" and isinstance(call.func.value, ast.Name)
                        and call.func.value.id == "writer"]
        helper_lines = [call.lineno for call in calls if isinstance(call.func, ast.Name)
                        and call.func.id == "_start_av_helper_with_wal"]
        assert append_lines and helper_lines and min(append_lines) < min(helper_lines)

    av_source = (REPO_ROOT / "scripts" / "stage0" / "av_sim.py").read_text(encoding="utf-8")
    av_tree = ast.parse(av_source)
    av_run_helper = next(node for node in ast.walk(av_tree)
                         if isinstance(node, ast.FunctionDef) and node.name == "run_helper")
    helper_calls = [node for node in ast.walk(av_run_helper) if isinstance(node, ast.Call)]
    assert any(isinstance(call.func, ast.Name) and call.func.id == "_publish_ready_record" for call in helper_calls)
    assert any(isinstance(call.func, ast.Name) and call.func.id == "_process_rearm_request" for call in helper_calls)


@pytest.mark.core_portable
@pytest.mark.memory_only
def test_av_sim_run_helper_processes_go_request_and_publishes_live_ack(monkeypatch, tmp_path):
    """run_helper 執行 go／ack 重取路徑並發布有效單調時鐘 lease。"""
    av = _av()
    wal_path = tmp_path / "raw.wal.ndjson"
    wal_path.write_bytes(b"prefill\n")
    go_file = tmp_path / "helper.go"
    ack_file = tmp_path / "helper.ack"
    ready_file = tmp_path / "helper.ready"
    stop_file = tmp_path / "helper.stop"
    log_file = tmp_path / "helper.jsonl"
    go_file.write_text(json.dumps({"request_id": "run-helper-1", "name": wal_path.name}), encoding="utf-8")

    class WindowsOSProxy:
        name = "nt"
        path = os.path

        def __getattr__(self, name):
            return getattr(os, name)

    class Handle:
        @staticmethod
        def close():
            pass

    real_exists = av.os.path.exists
    monkeypatch.setattr(av, "os", WindowsOSProxy())
    monkeypatch.setattr(av.os.path, "exists", lambda path: ack_file.is_file() if path == str(stop_file) else real_exists(path))
    monkeypatch.setattr(av, "_open_share_hold", lambda _path: Handle())
    monkeypatch.setattr(av, "_close_lease", lambda _lease: None)
    monkeypatch.setattr(av.time, "sleep", lambda _seconds: None)

    assert av.run_helper(
        watch_dir=str(tmp_path), seed=SEED, duration_s=10.0,
        ready_file=str(ready_file), stop_file=str(stop_file), log_file=str(log_file),
        go_file=str(go_file), ack_file=str(ack_file),
    ) == 0

    ack = json.loads(ack_file.read_text(encoding="utf-8"))
    assert ack["request_id"] == "run-helper-1"
    assert ack["held_file"] == wal_path.name
    assert ack["until_ns"] - ack["acquired_ns"] == ack["hold_ms"] * 1_000_000
    rows = [json.loads(line) for line in log_file.read_text(encoding="utf-8").splitlines()]
    assert any(row.get("event") == "rearm" and row.get("request_id") == "run-helper-1" for row in rows)


@pytest.mark.core_portable
@pytest.mark.memory_only
def test_av_sim_rearm_oplock_result_updates_summary(monkeypatch, tmp_path):
    """go/ack 重新取得的 RH oplock 結果須納入 helper 摘要。"""
    av = _av()
    wal_path = tmp_path / "raw.wal.ndjson"
    wal_path.write_bytes(b"prefill\n")
    go_file = tmp_path / "helper.go"
    ack_file = tmp_path / "helper.ack"
    ready_file = tmp_path / "helper.ready"
    stop_file = tmp_path / "helper.stop"
    log_file = tmp_path / "helper.jsonl"
    go_file.write_text(json.dumps({"request_id": "oplock-rearm-1", "name": wal_path.name}), encoding="utf-8")
    results = iter(["denied", "granted"])

    class WindowsOSProxy:
        name = "nt"
        path = os.path

        def __getattr__(self, name):
            return getattr(os, name)

    class Handle:
        @staticmethod
        def close():
            pass

    real_exists = av.os.path.exists
    monkeypatch.setattr(av, "os", WindowsOSProxy())
    monkeypatch.setattr(av.os.path, "exists", lambda path: ack_file.is_file() if path == str(stop_file) else real_exists(path))
    monkeypatch.setattr(av, "_request_oplock", lambda _path: (Handle(), next(results)))
    monkeypatch.setattr(av, "_close_lease", lambda _lease: None)
    monkeypatch.setattr(av.time, "sleep", lambda _seconds: None)

    assert av.run_helper(
        watch_dir=str(tmp_path), seed=SEED, duration_s=10.0,
        ready_file=str(ready_file), stop_file=str(stop_file), log_file=str(log_file),
        go_file=str(go_file), ack_file=str(ack_file), oplock=True,
    ) == 0

    rows = [json.loads(line) for line in log_file.read_text(encoding="utf-8").splitlines()]
    assert rows[-1]["event"] == "summary"
    assert rows[-1]["oplock"] == "granted"


@pytest.mark.core_portable
@pytest.mark.memory_only
@pytest.mark.parametrize("sharing_violation_first", [False, True])
def test_spike3_unlink_verdict_requires_observed_sharing_violation(
    monkeypatch, tmp_path, sharing_violation_first: bool
):
    """EDR helper grabs 不能代替外部 unlink 實際遭遇 FILE_SHARE_DELETE 衝突。"""
    from sw_core import win32_files
    import importlib.util

    script = REPO_ROOT / "scripts" / "stage0" / "spike3_win_wal.py"
    spec = importlib.util.spec_from_file_location("spike3_unlink_case_test", script)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    attempts = 0
    events: list[tuple[str, int]] = []

    class FakeHelper:
        def __init__(self, _wal_dir, _seed, rec, *, oplock=False, variant=None):
            self.rec = rec

        def start(self):
            events.append(("helper_start", len(events)))
            self.rec.observations["av_sim"] = {"grabs": 1, "launch_error": "none"}
            return True

        def ready_holds(self, name: str) -> bool:
            return name == "raw.wal.ndjson"

        def arm_hold(self, name: str) -> bool:
            self.lease_acquired_ns = time.perf_counter_ns()
            self.lease_until_ns = self.lease_acquired_ns + 500_000_000
            return name == "raw.wal.ndjson"

        def stop(self):
            self.rec.observations["av_sim"].update({"helper_exit": 0, "oplock": "granted"})

    class FakeWriter:
        def __init__(self, **kwargs):
            self.calls = 0
            self.wal_path = Path(kwargs["wal_dir"]) / "raw.wal.ndjson"

        def append(self, **_kwargs):
            self.calls += 1
            self.wal_path.touch(exist_ok=True)
            events.append(("append", self.calls))
            return {"seq": self.calls}

        def close(self):
            pass

    def delete(_path):
        nonlocal attempts
        attempts += 1
        events.append(("unlink", attempts))
        if sharing_violation_first and attempts == 1:
            error = PermissionError("sharing violation")
            error.winerror = 32
            raise error

    meta = [{"dir": "META", "loss_flag": True,
             "meta": {"event": "WAL_RECREATED_AFTER_EXTERNAL_UNLINK", "range_known": True,
                      "potentially_lost_seq_range": [1, 2]}}]
    monkeypatch.setattr(module, "AvHelper", FakeHelper)
    monkeypatch.setattr(module, "_work_dir", lambda _case: tmp_path)
    monkeypatch.setattr(module, "_safe_read_records", lambda _wal_dir, _rec: meta)
    monkeypatch.setattr(module.time, "sleep", lambda _seconds: None)
    monkeypatch.setattr(win32_files, "delete_classic", delete)
    monkeypatch.setattr("sw_core.wal.WalWriter", FakeWriter)

    rec = module.Recorder()
    verdict, _reason = module.case_unlink(rec, module.argparse.Namespace(unlink_mode="classic"))

    assert attempts == (2 if sharing_violation_first else 1)
    assert rec.observations.get("sharing_violation_attempts") == int(sharing_violation_first)
    assert rec.observations.get("interference_observed") is sharing_violation_first
    assert verdict == ("PASS" if sharing_violation_first else "FAIL")
    helper_start = next(i for i, item in enumerate(events) if item[0] == "helper_start")
    prefill_done = next(i for i, item in enumerate(events) if item == ("append", 10))
    first_unlink = next(i for i, item in enumerate(events) if item[0] == "unlink")
    assert prefill_done < helper_start < first_unlink, events
