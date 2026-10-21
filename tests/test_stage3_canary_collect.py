"""P9-T9 Canary collector 與 zipapp 的紅燈測試（對應 docs/rust-refactor/plans/10-stage3-canary.md Task 9）。

本檔只描述 tools/stage3/canary_collect.py、collector_main.py、pack_collector.py 的行為契約，不含實作。
tools.stage3.* 與 tests/stage3_fakes.py 一律於測試函式內 lazy import，使整個套件仍可收集（red 階段）。
黑箱邊界：不 import sw_core；CLI 以測試端 Runner 或 stub 腳本替身，不呼叫 interactive-status（Lease）。
"""
from __future__ import annotations

import ast
import json
import os
import shutil
import subprocess
import sys
import zipfile
from collections import OrderedDict
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace
from typing import Any, Callable

import pytest

# 本檔混合子行程（collector CLI、zipapp）與記憶體替身；依 T14 規則模組層只放平台與主要資源 marker
pytestmark = [pytest.mark.core_portable, pytest.mark.process_rpc]

REPO = Path(__file__).resolve().parents[1]


# --------------------------------------------------------------------------- #
# 測試替身與共用輔助（全部 lazy import；不 import sw_core）
# --------------------------------------------------------------------------- #
def _fakes():
    import stage3_fakes  # Task 2 基礎替身（tests/stage3_fakes.py）

    return stage3_fakes


def _res(obj: Any, rc: int = 0, stderr: str = ""):
    """回傳 tools.stage3.proc.CmdResult（stdout 為 JSON 字串）。"""
    from tools.stage3.proc import CmdResult

    return CmdResult(rc=rc, stdout=json.dumps(obj), stderr=stderr, elapsed_s=0.0, timed_out=False)


def _always_done(cid: str) -> dict[str, Any]:
    return {"ok": True, "command": {"status": "done", "cmd_id": cid, "error_code": None}}


def _daemon(wal: Path, *, pid: int = 11, version: str = "0.9.0") -> dict[str, Any]:
    return {
        "running": True,
        "pid": pid,
        "version": version,
        "wal_path": str(wal),
        "wal": {"healthy": True},
        "multi_open": False,
    }


class _CannedRun:
    """測試端 Runner：`daemon status`／`session list` 回固定 JSON，`cmd status --cmd-id` 交給 cmd_status。

    所有呼叫（argv）皆記入 calls，供斷言「只呼叫允許的子命令」與 `cmd status` 次數。
    """

    def __init__(
        self,
        daemon: dict[str, Any] | None = None,
        *,
        daemon_rc: int = 0,
        sessions: list[dict[str, Any]] | None = None,
        cmd_status: Callable[[str], dict[str, Any]] | None = None,
    ) -> None:
        self.daemon = daemon
        self.daemon_rc = daemon_rc
        self.sessions = list(sessions or [])
        self.cmd_status = cmd_status or _always_done
        self.calls: list[list[str]] = []

    def __call__(self, argv, *, env=None, timeout=None, cwd=None):
        argv = list(argv)
        self.calls.append(argv)
        sub = tuple(argv[1:3])
        if sub == ("daemon", "status"):
            if self.daemon_rc != 0:
                from tools.stage3.proc import CmdResult

                return CmdResult(rc=self.daemon_rc, stdout="", stderr="daemon unreachable", elapsed_s=0.0, timed_out=False)
            return _res({"ok": True, **(self.daemon or {})})
        if sub == ("session", "list"):
            return _res({"ok": True, "sessions": self.sessions})
        if sub == ("cmd", "status"):
            cid = argv[argv.index("--cmd-id") + 1]
            obj = self.cmd_status(cid)
            return _res(obj, rc=0 if obj.get("ok") else 1)
        return _res({"ok": False, "error_code": "TEST_UNEXPECTED_ARGV"}, rc=2, stderr=" ".join(argv))


def _cmd_status_calls(run: _CannedRun) -> list[list[str]]:
    return [c for c in run.calls if tuple(c[1:3]) == ("cmd", "status")]


def _env(root: Path, records: list[dict[str, Any]] | None = None, *, policy_over: dict[str, Any] | None = None):
    """建立 WAL（root/wal/raw.wal.ndjson）、frozen policy（經 load_policy）、dummy CLI 與 ledger 目錄路徑。"""
    fakes = _fakes()
    from tools.stage3 import canary_policy

    wal = root / "wal" / "raw.wal.ndjson"
    wal.parent.mkdir(parents=True, exist_ok=True)
    fakes.write_wal(wal, records or [])
    pol = canary_policy.load_policy(fakes.write_policy(root / "policy.json", **(policy_over or {})))
    cli = root / "bin" / "serialwrap"
    cli.parent.mkdir(parents=True, exist_ok=True)
    cli.write_bytes(b"not-a-real-cli")
    return SimpleNamespace(wal=wal, pol=pol, ledger=root / "ledger", cli=cli)


def _wal_path(root: Path) -> Path:
    p = root / "wal" / "raw.wal.ndjson"
    p.parent.mkdir(parents=True, exist_ok=True)
    return p


def _submits(start: int, n: int) -> list[dict[str, Any]]:
    fakes = _fakes()
    return [fakes.wal_record(i, cmd_id=fakes.uuid_cmd_id(i)) for i in range(start, start + n)]


def _ledger(ledger_dir: Path) -> list[dict[str, Any]]:
    p = ledger_dir / "ledger.ndjson"
    if not p.exists():
        return []
    return [json.loads(line) for line in p.read_text(encoding="utf-8").splitlines() if line.strip()]


def _pending(ledger_dir: Path) -> dict[str, Any]:
    p = ledger_dir / "pending_results.json"
    return json.loads(p.read_text(encoding="utf-8")) if p.exists() else {}


def _pin_daemon_sha(monkeypatch, sha: str) -> None:
    """take_snapshot 會對 daemon pid 取執行檔 hash；合成 bundle 無真實 daemon 行程，故釘住為 launcher sha。"""
    from tools.stage3 import canary_collect

    monkeypatch.setattr(canary_collect, "daemon_exe_sha256", lambda pid: sha)


def _assert_verified(res, bundle: Path, manifest_sha: str) -> None:
    from tools.stage3 import ab_manifest

    artifact_id, m, check, trees = res
    assert check == "verified"
    assert artifact_id == manifest_sha
    assert m == json.loads((bundle / "release-manifest.json").read_text(encoding="utf-8"))
    assert trees == (ab_manifest.bundle_tree_sha256(bundle), ab_manifest.runtime_tree_sha256(bundle))


class _Clock:
    """假時鐘：iso() 回傳目前假時間，sleep() 推進之。"""

    def __init__(self) -> None:
        self._t = datetime(2026, 11, 2, 0, 0, 0, tzinfo=timezone.utc)

    def iso(self) -> str:
        return self._t.isoformat()

    def sleep(self, s: float) -> None:
        self._t += timedelta(seconds=s)


class _EvictingCmdStore:
    """P2 Task 14 同名替身的行為副本（依 sw_core/arbiter.py:233-251 的語意）。

    完成紀錄超過 history_max 時淘汰最舊者（done_at 最小）；被淘汰的 cmd_id 查詢回 CMD_NOT_FOUND。
    本檔不 import P2 測試模組，避免測試檔之間相依。
    """

    def __init__(self, history_max: int = 512) -> None:
        self.history_max = history_max
        self._done: OrderedDict[str, None] = OrderedDict()
        self.evicted: set[str] = set()

    @property
    def evicted_count(self) -> int:
        return len(self.evicted)

    def complete(self, cid: str) -> None:
        self._done[cid] = None  # 完成即寫入，done_at 單調遞增
        while len(self._done) > self.history_max:
            old, _ = self._done.popitem(last=False)
            self.evicted.add(old)

    def handle(self, cid: str) -> dict[str, Any]:
        if cid not in self._done:
            return {"ok": False, "error_code": "CMD_NOT_FOUND", "cmd_id": cid}
        return {"ok": True, "command": {"status": "done", "cmd_id": cid, "error_code": None}}


def _run_canary_weeks(root: Path, *, drain: bool) -> SimpleNamespace:
    """模擬 4 個收集週期：每週 300 個新 agent cmd_id 進 WAL 且立即 done；週間 store 另完成 100 筆非 canary 命令。

    drain=True：每次 collect_once 後依 collect_loop 規則連續呼叫 drain_results_once（每次間隔 1 s）直到 pending 清空。
    drain=False：只呼叫 collect_once（對照組）。
    """
    fakes = _fakes()
    from tools.stage3 import canary_collect

    env = _env(root, [], policy_over={"result_poll_max_per_cycle": 50, "collector_interval_s": 30})
    clock = _Clock()
    store = _EvictingCmdStore(history_max=512)
    run = _CannedRun(_daemon(env.wal), cmd_status=store.handle)
    canary_ids: list[str] = []
    pending_by_week: list[dict[str, Any]] = []
    seq = 0
    for week in range(4):
        batch = []
        for _ in range(300):
            seq += 1
            cid = fakes.uuid_cmd_id(seq)
            canary_ids.append(cid)
            store.complete(cid)
            batch.append(fakes.wal_record(seq, cmd_id=cid))
        fakes.write_wal(env.wal, batch, mode="a")
        canary_collect.collect_once(env.cli, env.ledger, env.pol, platform="linux", run=run, now=clock.iso)
        if drain:
            while canary_collect.drain_results_once(env.cli, env.ledger, env.pol, run=run, now=clock.iso) > 0:
                clock.sleep(1.0)
        pending_by_week.append(_pending(env.ledger))
        for i in range(100):
            store.complete(f"nc-{week}-{i:03d}")
        clock.sleep(30.0)
    return SimpleNamespace(ledger=env.ledger, canary_ids=canary_ids, store=store, pending_by_week=pending_by_week)


# --------------------------------------------------------------------------- #
# Collector 行為
# --------------------------------------------------------------------------- #
def test_collector_never_calls_lease_touching_commands(tmp_path):
    fakes = _fakes()
    from tools.stage3 import canary_collect, canary_policy

    stub_dir = tmp_path / "bin"
    c1 = fakes.uuid_cmd_id(1)
    cli = fakes.make_stub_cli(stub_dir, {
        "daemon status": [{"rc": 0, "stdout": {"ok": True, "running": True, "pid": 11, "version": "0.9.0",
                                                "wal_path": str(tmp_path / "wal" / "raw.wal.ndjson"), "wal": {"healthy": True},
                                                "multi_open": False}}],
        "session list": [{"rc": 0, "stdout": {"ok": True, "sessions": [{"com": "COM0", "state": "READY", "bridge_generation": 3}]}}],
        f"cmd status --cmd-id {c1}": [{"rc": 0, "stdout": {"ok": True, "command": {"status": "done"}}}],
    })
    (tmp_path / "wal").mkdir()
    fakes.write_wal(tmp_path / "wal" / "raw.wal.ndjson", [fakes.wal_record(1, cmd_id=c1)])
    pol = canary_policy.load_policy(fakes.write_policy(tmp_path / "policy.json"))
    out = canary_collect.collect_once(cli, tmp_path / "ledger", pol, platform="linux")
    assert out["snapshot_ok"] is True and out["new_submits"] == 1
    canary_collect.collect_once(cli, tmp_path / "ledger", pol, platform="linux")
    argvs = [c["argv"] for c in fakes.read_calls(stub_dir)]
    assert all("interactive" not in " ".join(a) for a in argvs)
    assert {tuple(a[:2]) for a in argvs} <= set(canary_collect.READONLY_COMMANDS)


def test_draft_policy_refused(tmp_path):
    fakes = _fakes()
    from tools.stage3 import canary_collect, canary_policy
    from tools.stage3.reason_codes import HarnessError

    env = _env(tmp_path, _submits(1, 1))
    draft = canary_policy.load_policy(
        fakes.write_policy(tmp_path / "draft.json", status="draft", frozen_by=None, frozen_at=None, decision_ref=None)
    )
    run = _CannedRun(_daemon(env.wal))
    with pytest.raises(HarnessError) as ei:
        canary_collect.collect_once(env.cli, env.ledger, draft, platform="linux", run=run)
    assert ei.value.code == "HARNESS_CANARY_POLICY_NOT_FROZEN"
    assert _ledger(env.ledger) == []


def test_collect_twice_no_duplicate_submit(tmp_path):
    from tools.stage3 import canary_collect

    env = _env(tmp_path, _submits(1, 1))
    run = _CannedRun(_daemon(env.wal))
    out1 = canary_collect.collect_once(env.cli, env.ledger, env.pol, platform="linux", run=run)
    out2 = canary_collect.collect_once(env.cli, env.ledger, env.pol, platform="linux", run=run)
    assert out1["new_submits"] == 1
    assert out2["new_submits"] == 0
    assert len([r for r in _ledger(env.ledger) if r["type"] == "submit"]) == 1


def test_submit_result_recorded(tmp_path):
    fakes = _fakes()
    from tools.stage3 import canary_collect

    env = _env(tmp_path, _submits(1, 1))
    canary_collect.collect_once(env.cli, env.ledger, env.pol, platform="linux", run=_CannedRun(_daemon(env.wal)))
    done = [r for r in _ledger(env.ledger) if r["type"] == "submit_result" and r["cmd_id"] == fakes.uuid_cmd_id(1)]
    assert len(done) == 1 and done[0]["status"] == "done"

    # not-found 分支：新 WAL 記錄，cmd status 回 CMD_NOT_FOUND
    fakes.write_wal(env.wal, _submits(2, 1), mode="a")
    not_found = _CannedRun(_daemon(env.wal), cmd_status=lambda cid: {"ok": False, "error_code": "CMD_NOT_FOUND", "cmd_id": cid})
    canary_collect.collect_once(env.cli, env.ledger, env.pol, platform="linux", run=not_found)
    unk = [r for r in _ledger(env.ledger) if r["type"] == "submit_result" and r["cmd_id"] == fakes.uuid_cmd_id(2)]
    assert len(unk) == 1 and unk[0]["status"] == "unknown"


def test_result_poll_rate_limited(tmp_path):
    from tools.stage3 import canary_collect

    env = _env(tmp_path, _submits(1, 120), policy_over={"collector.result_poll_max_per_cycle": 7})
    run = _CannedRun(_daemon(env.wal))
    out = canary_collect.collect_once(env.cli, env.ledger, env.pol, platform="linux", run=run)
    assert out["new_submits"] == 120
    assert len(_cmd_status_calls(run)) == 7


def test_pending_poll_rotates_past_old_nonterminal_commands(tmp_path):
    fakes = _fakes()
    from tools.stage3 import canary_collect

    env = _env(tmp_path, [], policy_over={"collector.result_poll_max_per_cycle": 50})
    cmd_ids = [fakes.uuid_cmd_id(value) for value in range(1, 71)]
    first_seen = {
        cmd_id: "2026-11-02T01:00:00+00:00" if index < 60 else "2026-11-02T01:01:00+00:00"
        for index, cmd_id in enumerate(cmd_ids)
    }
    env.ledger.mkdir(parents=True)
    (env.ledger / "pending_results.json").write_text(json.dumps(first_seen), encoding="utf-8")
    statuses = {
        cmd_id: {"ok": True, "command": {"status": "running" if index < 60 else "done", "error_code": None}}
        for index, cmd_id in enumerate(cmd_ids)
    }
    run = _CannedRun(_daemon(env.wal), cmd_status=statuses.__getitem__)

    for _ in range(3):
        canary_collect.drain_results_once(env.cli, env.ledger, env.pol, run=run)

    results = [row for row in _ledger(env.ledger) if row.get("type") == "submit_result"]
    assert {row["cmd_id"] for row in results if row["status"] == "done"} == set(cmd_ids[60:])


def test_pending_poll_cursor_survives_collect_state_reset(tmp_path):
    fakes = _fakes()
    from tools.stage3 import canary_collect

    env = _env(tmp_path, [], policy_over={"collector.result_poll_max_per_cycle": 50})
    cmd_ids = [fakes.uuid_cmd_id(value) for value in range(1, 121)]
    pending = {cmd_id: f"2026-11-02T01:{index // 60:02d}:{index % 60:02d}+00:00" for index, cmd_id in enumerate(cmd_ids)}
    env.ledger.mkdir(parents=True)
    (env.ledger / "pending_results.json").write_text(json.dumps(pending), encoding="utf-8")
    run = _CannedRun(
        _daemon(env.wal),
        cmd_status=lambda _cmd_id: {"ok": True, "command": {"status": "running", "error_code": None}},
    )

    canary_collect.drain_results_once(env.cli, env.ledger, env.pol, run=run)
    canary_collect.drain_results_once(env.cli, env.ledger, env.pol, run=run)
    first_batches = [
        [call[call.index("--cmd-id") + 1] for call in _cmd_status_calls(run)[:50]],
        [call[call.index("--cmd-id") + 1] for call in _cmd_status_calls(run)[50:100]],
    ]
    canary_collect._save_poll_state(env.ledger, selected=[], last_polled=None)
    canary_collect.drain_results_once(env.cli, env.ledger, env.pol, run=run)
    third_batch = [call[call.index("--cmd-id") + 1] for call in _cmd_status_calls(run)[100:]]

    assert first_batches == [cmd_ids[:50], cmd_ids[50:100]]
    assert third_batch == cmd_ids[100:] + cmd_ids[:30]


def test_canceled_and_interactive_results_do_not_stay_pending(tmp_path):
    fakes = _fakes()
    from tools.stage3 import canary_collect

    env = _env(tmp_path, [])
    cmd_ids = [fakes.uuid_cmd_id(value) for value in (1, 2)]
    env.ledger.mkdir(parents=True)
    (env.ledger / "pending_results.json").write_text(
        json.dumps({cmd_ids[0]: "2026-11-02T01:00:00+00:00", cmd_ids[1]: "2026-11-02T01:00:01+00:00"}),
        encoding="utf-8",
    )
    statuses = {
        cmd_ids[0]: {"ok": True, "command": {"status": "canceled", "error_code": None}},
        cmd_ids[1]: {"ok": True, "command": {"status": "interactive", "error_code": None}},
    }

    assert canary_collect.drain_results_once(
        env.cli, env.ledger, env.pol, run=_CannedRun(_daemon(env.wal), cmd_status=statuses.__getitem__)
    ) == 0
    results = [row for row in _ledger(env.ledger) if row.get("type") == "submit_result"]
    assert {(row["cmd_id"], row["status"], row["error_code"]) for row in results} == {
        (cmd_ids[0], "unknown", "CANCELED"),
        (cmd_ids[1], "unknown", "INTERACTIVE"),
    }


def test_expired_pending_is_removed_after_cli_exception(tmp_path):
    fakes = _fakes()
    from tools.stage3 import canary_collect

    env = _env(tmp_path, [])
    cmd_id = fakes.uuid_cmd_id(1)
    env.ledger.mkdir(parents=True)
    (env.ledger / "pending_results.json").write_text(
        json.dumps({cmd_id: "2026-10-30T00:00:00+00:00"}), encoding="utf-8"
    )

    def cli_error(*_args, **_kwargs):
        raise FileNotFoundError("CLI disappeared")

    assert canary_collect.drain_results_once(
        env.cli, env.ledger, env.pol, run=cli_error, now=lambda: "2026-11-02T00:00:00+00:00"
    ) == 0
    records = _ledger(env.ledger)
    result = next(row for row in records if row.get("type") == "submit_result")
    assert result["cmd_id"] == cmd_id
    assert result["status"] == "unknown" and result["reason"] == "STALE_NON_TERMINAL"
    assert any(row.get("detail") == "cmd_status_error:FileNotFoundError" for row in records)


def test_drain_reads_pending_state_without_reparsing_ledger(tmp_path, monkeypatch):
    fakes = _fakes()
    from tools.stage3 import canary_collect

    env = _env(tmp_path, [])
    cmd_id = fakes.uuid_cmd_id(1)
    env.ledger.mkdir(parents=True)
    (env.ledger / "pending_results.json").write_text(
        json.dumps({cmd_id: "2026-11-02T01:00:00+00:00"}), encoding="utf-8"
    )

    def fail_ledger_read(_ledger_dir):
        raise AssertionError("drain must not parse the full ledger")

    monkeypatch.setattr(canary_collect, "_read_ledger", fail_ledger_read)
    assert canary_collect.drain_results_once(
        env.cli, env.ledger, env.pol, run=_CannedRun(_daemon(env.wal))
    ) == 0
    result = next(row for row in _ledger(env.ledger) if row.get("type") == "submit_result")
    assert result["cmd_id"] == cmd_id and result["status"] == "done"


def test_only_exact_cmd_not_found_is_treated_as_eviction(tmp_path):
    fakes = _fakes()
    from tools.stage3 import canary_collect

    env = _env(tmp_path, [])
    aliases = ("COMMAND_NOT_FOUND", "NOT_FOUND", "X_CMD_NOT_FOUND", "cmd_not_found")
    cmd_ids = [fakes.uuid_cmd_id(value) for value in range(1, len(aliases) + 1)]
    env.ledger.mkdir(parents=True)
    (env.ledger / "pending_results.json").write_text(
        json.dumps({cmd_id: "2026-11-02T01:00:00+00:00" for cmd_id in cmd_ids}), encoding="utf-8"
    )
    statuses = {
        cmd_id: {"ok": False, "error_code": alias, "cmd_id": cmd_id}
        for cmd_id, alias in zip(cmd_ids, aliases)
    }

    assert canary_collect.drain_results_once(
        env.cli, env.ledger, env.pol, run=_CannedRun(_daemon(env.wal), cmd_status=statuses.__getitem__)
    ) == len(aliases)
    records = _ledger(env.ledger)
    assert not [row for row in records if row.get("type") == "submit_result"]
    assert set(json.loads((env.ledger / "pending_results.json").read_text(encoding="utf-8"))) == set(cmd_ids)


def test_canary_results_survive_history_eviction(tmp_path):
    drained = _run_canary_weeks(tmp_path / "drain", drain=True)
    recs = _ledger(drained.ledger)
    assert len(drained.canary_ids) == 1200
    done = [r for r in recs if r["type"] == "submit_result" and r["status"] == "done"]
    assert {r["cmd_id"] for r in done} == set(drained.canary_ids)
    assert len(done) == 1200
    assert drained.store.evicted_count >= 1600 - 512  # 確實發生淘汰
    assert [r for r in recs if r["type"] == "submit_result" and r.get("reason") == "CMD_NOT_FOUND"] == []
    assert all(p == {} for p in drained.pending_by_week)  # 每週期結束 pending_results.json 為空

    # 對照組：同負載、只呼叫 collect_once、不 drain → 被淘汰的 canary 結果必須以 unknown＋anomaly 留痕
    control = _run_canary_weeks(tmp_path / "no_drain", drain=False)
    crecs = _ledger(control.ledger)
    evicted = [
        r for r in crecs
        if r["type"] == "submit_result" and r["status"] == "unknown" and r.get("reason") == "CMD_NOT_FOUND"
    ]
    assert evicted, "對照組應產生 CMD_NOT_FOUND 的 unknown 結果"
    anomalies = {r["cmd_id"] for r in crecs if r["type"] == "harvest_anomaly" and r.get("kind") == "result_evicted"}
    assert {r["cmd_id"] for r in evicted} <= anomalies
    # Task 12 tally 將這些筆數計入 submits_result_evicted 而非 submits，屬 Task 12 測試範圍，此處不重複斷言


def test_daemon_down_snapshot_recorded(tmp_path):
    from tools.stage3 import canary_collect

    env = _env(tmp_path, _submits(1, 1))
    canary_collect.collect_once(env.cli, env.ledger, env.pol, platform="linux", run=_CannedRun(daemon_rc=2))
    recs = _ledger(env.ledger)
    snaps = [r for r in recs if r["type"] == "snapshot"]
    assert snaps and snaps[-1]["daemon_ok"] is False
    assert snaps[-1]["error"]
    assert [r for r in recs if r["type"] == "submit"] == []  # daemon 失聯不呼叫 harvest


@pytest.mark.parametrize(
    ("message", "expected"),
    [
        ("[Errno 111] Connection refused", "connect_refused"),
        ("[Errno 2] No such file or directory", "not_running"),
        ("[WinError 10061] No connection could be made", "connect_refused"),
        ("[WinError 2] The system cannot find the file specified", "not_running"),
        ("[Errno 22] Invalid argument", "daemon_status_failed:rc=2"),
    ],
)
def test_socket_failure_marks_confirmed_dead_daemon(message, expected):
    from tools.stage3 import canary_collect
    from tools.stage3.proc import CmdResult

    def run(argv, **_kwargs):
        assert argv[1:3] == ["daemon", "status"]
        return CmdResult(
            rc=2,
            stdout=json.dumps({"ok": False, "error_code": "SOCKET_ERROR", "message": message}),
            stderr=f"serialwrap daemon.status: SOCKET_ERROR: {message}",
            elapsed_s=0.0,
            timed_out=False,
        )

    snapshot = canary_collect.take_snapshot(Path("serialwrap"), platform="linux", run=run)
    assert snapshot.daemon_ok is False
    assert snapshot.error == expected


def test_prepfreeze_wal_submits_are_not_counted(tmp_path):
    fakes = _fakes()
    from tools.stage3 import canary_collect

    old_id, new_id = fakes.uuid_cmd_id(1), fakes.uuid_cmd_id(2)
    rows = [
        fakes.wal_record(1, cmd_id=old_id, wall_ts="2026-10-31T23:59:59+00:00"),
        fakes.wal_record(2, cmd_id=new_id, wall_ts="2026-11-01T00:00:01+00:00"),
    ]
    env = _env(tmp_path, rows)
    run = _CannedRun(_daemon(env.wal))

    out = canary_collect.collect_once(env.cli, env.ledger, env.pol, platform="linux", run=run)

    submits = [row for row in _ledger(env.ledger) if row.get("type") == "submit"]
    assert out["new_submits"] == 1
    assert [row["cmd_id"] for row in submits] == [new_id]


def test_torn_ledger_line_does_not_stop_collector(tmp_path):
    fakes = _fakes()
    from tools.stage3 import canary_collect

    cmd_id = fakes.uuid_cmd_id(1)
    env = _env(tmp_path, [fakes.wal_record(1, cmd_id=cmd_id)])
    env.ledger.mkdir(parents=True)
    (env.ledger / "ledger.ndjson").write_bytes(b'{"type":"snapshot","broken":')

    out = canary_collect.collect_once(env.cli, env.ledger, env.pol, platform="linux", run=_CannedRun(_daemon(env.wal)))

    from tools.stage3 import jsonio

    records, malformed = jsonio.read_ndjson(env.ledger / "ledger.ndjson")
    assert out["snapshot_ok"] is True and out["new_submits"] == 1
    assert malformed == [1]
    assert any(row.get("detail") == "malformed_ledger_line:1" for row in records)


def test_collect_loop_uses_injected_monotonic_clock_and_stops(tmp_path, monkeypatch):
    from threading import Event

    from tools.stage3 import canary_collect

    env = _env(tmp_path, [], policy_over={"collector.interval_s": 3})
    clock = [0.0]
    collected_at: list[float] = []
    stop = Event()

    def collect(*_args, **_kwargs):
        collected_at.append(clock[0])
        if len(collected_at) == 2:
            stop.set()

    def sleep(delay):
        clock[0] += delay

    monkeypatch.setattr(canary_collect, "collect_once", collect)
    canary_collect.collect_loop(
        env.cli,
        env.ledger,
        env.pol,
        platform="linux",
        stop=stop,
        monotonic=lambda: clock[0],
        sleep=sleep,
    )

    assert collected_at == [0.0, 3.0]


def test_collect_loop_drains_pending_between_collections(tmp_path, monkeypatch):
    from threading import Event

    from tools.stage3 import canary_collect

    env = _env(tmp_path, [], policy_over={"collector.interval_s": 4})
    env.ledger.mkdir(parents=True)
    pending_path = env.ledger / "pending_results.json"
    poll_state_path = env.ledger / "pending_poll_state.json"
    first_id = "00000000000000000000000000000001"
    second_id = "00000000000000000000000000000002"
    clock = [0.0]
    events: list[str] = []
    stop = Event()

    def collect(*_args, **_kwargs):
        events.append("collect")
        if events.count("collect") == 1:
            pending_path.write_text(
                json.dumps(
                    {
                        first_id: "2026-11-02T01:00:00+00:00",
                        second_id: "2026-11-02T01:00:01+00:00",
                    }
                ),
                encoding="utf-8",
            )
            poll_state_path.write_text(
                json.dumps(
                    {
                        "last_cmd_id": first_id,
                        "last_first_seen": "2026-11-02T01:00:00+00:00",
                        "last_polled_ids": [first_id],
                    }
                ),
                encoding="utf-8",
            )
        else:
            stop.set()

    def drain(*_args, **_kwargs):
        events.append("drain")
        poll_state_path.write_text(
            json.dumps(
                {
                    "last_cmd_id": second_id,
                    "last_first_seen": "2026-11-02T01:00:01+00:00",
                    "last_polled_ids": [second_id],
                }
            ),
            encoding="utf-8",
        )
        return 2

    def sleep(delay):
        clock[0] += delay

    monkeypatch.setattr(canary_collect, "collect_once", collect)
    monkeypatch.setattr(canary_collect, "drain_results_once", drain)
    canary_collect.collect_loop(
        env.cli,
        env.ledger,
        env.pol,
        platform="linux",
        stop=stop,
        monotonic=lambda: clock[0],
        sleep=sleep,
    )

    assert events == ["collect", "drain", "collect"]


def test_policy_ref_written_with_revision(tmp_path):
    from tools.stage3 import canary_collect

    env = _env(tmp_path, _submits(1, 1))
    run = _CannedRun(_daemon(env.wal))
    canary_collect.collect_once(env.cli, env.ledger, env.pol, platform="linux", run=run)
    recs = _ledger(env.ledger)
    assert recs[0]["type"] == "policy_ref"
    assert recs[0]["policy_sha256"] == env.pol.sha256
    assert isinstance(recs[0]["collector_revision"], str) and recs[0]["collector_revision"]
    canary_collect.collect_once(env.cli, env.ledger, env.pol, platform="linux", run=run)
    assert len([r for r in _ledger(env.ledger) if r["type"] == "policy_ref"]) == 1  # policy 不變不重寫


def test_snapshot_parses_sessions(tmp_path):
    from tools.stage3 import canary_collect

    env = _env(tmp_path, [])
    sessions = [
        {
            "com": "COM0", "state": "READY", "bridge_generation": 3,
            "boot_quiet_remaining_s": 2.5, "ready_reconfirm_pending": False, "ready_reconfirm_failed": True,
            "command_capable": True, "platform": "shell", "profile": "p1", "last_error": None,
            "last_ready_at": "2026-11-02T01:00:00+00:00",
        },
        {"com": "COM1", "state": "ATTACHED", "bridge_generation": 1},
    ]
    snap = canary_collect.take_snapshot(env.cli, platform="linux", run=_CannedRun(_daemon(env.wal), sessions=sessions))
    s0, s1 = snap.sessions
    assert s0.boot_quiet_remaining_s == 2.5
    assert s0.ready_reconfirm_pending is False
    assert s0.ready_reconfirm_failed is True
    assert s0.command_capable is True
    assert s0.platform == "shell"
    assert s0.last_ready_at == "2026-11-02T01:00:00+00:00"
    assert s1.boot_quiet_remaining_s is None
    assert s1.ready_reconfirm_pending is None
    assert s1.ready_reconfirm_failed is None
    assert s1.command_capable is None
    assert s1.platform is None
    assert s1.last_ready_at is None


def test_host_id_stable():
    from tools.stage3 import canary_collect

    first = canary_collect.host_id()
    second = canary_collect.host_id()
    assert first == second
    assert len(first) == 16


# --------------------------------------------------------------------------- #
# Artifact 歸屬（CLI＋daemon 雙重核實＋部署目錄指紋）
# --------------------------------------------------------------------------- #
def test_resolve_artifact_linux_symlink_verified(tmp_path):
    fakes = _fakes()
    from tools.stage3 import canary_collect

    bundle = fakes.make_bundle(tmp_path / "b")
    link_dir = tmp_path / "home" / ".local" / "bin"
    link_dir.mkdir(parents=True)
    link = link_dir / "serialwrap"
    os.symlink(bundle / "bin" / "serialwrap", link)
    daemon_sha = fakes.sha256_file(bundle / "bin" / "serialwrapd")  # launcher sha
    res = canary_collect.resolve_artifact(link, daemon_pid=11, daemon_version="0.9.0", daemon_sha=lambda pid: daemon_sha)
    _assert_verified(res, bundle, fakes.sha256_file(bundle / "release-manifest.json"))


def test_resolve_artifact_windows_current_release(tmp_path):
    fakes = _fakes()
    from tools.stage3 import canary_collect

    install = tmp_path / "install"
    (install / "versions").mkdir(parents=True)
    bundle = fakes.make_bundle(install / "versions" / "0.9.0")
    bin_dir = install / "bin"
    bin_dir.mkdir(parents=True)
    cli = bin_dir / "serialwrap.exe"
    shutil.copyfile(bundle / "bin" / "serialwrap", cli)
    shutil.copyfile(bundle / "bin" / "serialwrapd", bin_dir / "serialwrapd.exe")
    (bin_dir / "current-release.json").write_text(
        json.dumps({"bundle_version": "0.9.0", "dir": str(bundle)}), encoding="utf-8"
    )
    daemon_sha = fakes.sha256_file(bundle / "bin" / "serialwrapd")
    res = canary_collect.resolve_artifact(
        cli, daemon_pid=11, daemon_version="0.9.0", daemon_sha=lambda pid: daemon_sha, layout="windows"
    )
    _assert_verified(res, bundle, fakes.sha256_file(bundle / "release-manifest.json"))


def test_windows_entry_directory_rejects_sideloaded_files(tmp_path):
    fakes = _fakes()
    from tools.stage3 import canary_collect

    install = tmp_path / "install"
    bundle = fakes.make_bundle(install / "versions" / "0.9.0")
    entry_dir = install / "bin"
    entry_dir.mkdir(parents=True)
    cli = entry_dir / "serialwrap.exe"
    shutil.copyfile(bundle / "bin" / "serialwrap", cli)
    shutil.copyfile(bundle / "bin" / "serialwrapd", entry_dir / "serialwrapd.exe")
    (entry_dir / "current-release.json").write_text(
        json.dumps({"bundle_version": "0.9.0", "dir": str(bundle)}), encoding="utf-8"
    )
    daemon_sha = fakes.sha256_file(bundle / "bin" / "serialwrapd")
    assert canary_collect.resolve_artifact(
        cli, daemon_pid=11, daemon_version="0.9.0", daemon_sha=lambda _pid: daemon_sha, layout="windows"
    )[2] == "verified"

    (entry_dir / "version.dll").write_bytes(b"sideloaded")
    artifact_id, _manifest, check, trees = canary_collect.resolve_artifact(
        cli, daemon_pid=11, daemon_version="0.9.0", daemon_sha=lambda _pid: daemon_sha, layout="windows"
    )
    assert artifact_id is None and check == "tree_invalid" and trees is None


def test_deployed_tree_matches_task3_algorithm(tmp_path):
    fakes = _fakes()
    from tools.stage3 import ab_manifest, canary_collect

    b = fakes.make_bundle(tmp_path / "b")
    (b / "assets").mkdir(exist_ok=True)
    (b / "assets" / "x.bin").write_bytes(b"\x00\x01\x02")
    (b / "py" / "__pycache__").mkdir(parents=True, exist_ok=True)
    (b / "py" / "__pycache__" / "m.cpython-312.pyc").write_bytes(b"pyc-bytes")
    before = canary_collect.deployed_tree_digests(b)
    assert before == (ab_manifest.bundle_tree_sha256(b), ab_manifest.runtime_tree_sha256(b))

    sums = b / "SHA256SUMS"
    data = bytearray(sums.read_bytes())
    data[0] ^= 0x01  # 只改根目錄 SHA256SUMS 一個位元組（長度不變）
    sums.write_bytes(bytes(data))
    after = canary_collect.deployed_tree_digests(b)
    assert after[0] != before[0]  # bundle 全樹含 SHA256SUMS
    assert after[1] == before[1]  # runtime 樹排除根目錄 SHA256SUMS


def test_sideloaded_install_same_manifest_changes_snapshot_tree(tmp_path, monkeypatch):
    fakes = _fakes()
    from tools.stage3 import ab_manifest, canary_collect

    bundle = fakes.make_bundle(tmp_path / "b")
    manifest_sha = fakes.sha256_file(bundle / "release-manifest.json")
    tree0 = canary_collect.deployed_tree_digests(bundle)
    (bundle / "bin" / "version.dll").write_bytes(b"sideloaded-dll")  # manifest／serialwrap／serialwrapd 皆不動
    daemon_sha = fakes.sha256_file(bundle / "bin" / "serialwrapd")
    cli = bundle / "bin" / "serialwrap"

    artifact_id, _m, check, trees = canary_collect.resolve_artifact(
        cli, daemon_pid=11, daemon_version="0.9.0", daemon_sha=lambda pid: daemon_sha
    )
    assert check == "verified"
    assert artifact_id == manifest_sha  # 歸屬仍以 manifest 位元組為鍵
    assert trees != tree0
    assert trees == (ab_manifest.bundle_tree_sha256(bundle), ab_manifest.runtime_tree_sha256(bundle))

    # collector 寫入帳本的 snapshot 必須記錄實際部署內容的新指紋
    _pin_daemon_sha(monkeypatch, daemon_sha)
    env = _env(tmp_path / "env", [])
    canary_collect.collect_once(cli, env.ledger, env.pol, platform="linux", run=_CannedRun(_daemon(env.wal)))
    snaps = [r for r in _ledger(env.ledger) if r["type"] == "snapshot"]
    assert snaps[-1]["artifact_check"] == "verified"
    assert snaps[-1]["bundle_tree_sha256"] == ab_manifest.bundle_tree_sha256(bundle)
    assert snaps[-1]["runtime_tree_sha256"] == ab_manifest.runtime_tree_sha256(bundle)


def test_deployed_tree_rehashed_each_snapshot(tmp_path, monkeypatch):
    fakes = _fakes()
    from tools.stage3 import canary_collect

    bundle = fakes.make_bundle(tmp_path / "b")
    dll = bundle / "bin" / "version.dll"
    dll.write_bytes(b"v1")
    daemon_sha = fakes.sha256_file(bundle / "bin" / "serialwrapd")
    _pin_daemon_sha(monkeypatch, daemon_sha)
    run = _CannedRun(_daemon(_wal_path(tmp_path / "env")))
    cli = bundle / "bin" / "serialwrap"

    s1 = canary_collect.take_snapshot(cli, platform="linux", run=run)
    st = os.stat(dll)
    dll.write_bytes(b"v2")  # 同大小、改寫內容
    os.utime(dll, ns=(st.st_atime_ns, st.st_mtime_ns))  # 保留原 mtime，不得以 (size, mtime) 快取略過
    s2 = canary_collect.take_snapshot(cli, platform="linux", run=run)
    assert s1.artifact_check == "verified" and s2.artifact_check == "verified"
    assert s1.bundle_tree_sha256 != s2.bundle_tree_sha256


def test_unhashable_deployed_tree_not_attributed(tmp_path, monkeypatch):
    fakes = _fakes()
    from tools.stage3 import canary_collect
    from tools.stage3.reason_codes import HarnessError

    bundle = fakes.make_bundle(tmp_path / "b")
    daemon_sha = fakes.sha256_file(bundle / "bin" / "serialwrapd")
    cli = bundle / "bin" / "serialwrap"

    def _boom(_bundle_dir):
        raise HarnessError("HARNESS_ARTIFACT_MISMATCH", "non-regular file: bin/extra")

    monkeypatch.setattr(canary_collect, "deployed_tree_digests", _boom)
    artifact_id, _m, check, trees = canary_collect.resolve_artifact(
        cli, daemon_pid=11, daemon_version="0.9.0", daemon_sha=lambda pid: daemon_sha
    )
    assert (artifact_id, check, trees) == (None, "tree_invalid", None)

    _pin_daemon_sha(monkeypatch, daemon_sha)
    snap = canary_collect.take_snapshot(cli, platform="linux", run=_CannedRun(_daemon(_wal_path(tmp_path / "env"))))
    assert snap.artifact_id is None
    assert snap.bundle_tree_sha256 is None
    assert snap.runtime_tree_sha256 is None


def test_deployed_tree_reports_relative_path_when_file_changes_during_hash(tmp_path, monkeypatch):
    fakes = _fakes()
    from tools.stage3 import canary_collect
    from tools.stage3.reason_codes import HarnessError

    bundle = fakes.make_bundle(tmp_path / "bundle")
    real_fstat = os.fstat
    calls = 0

    def changed_fstat(fd):
        nonlocal calls
        result = real_fstat(fd)
        calls += 1
        if calls != 2:
            return result
        return SimpleNamespace(
            st_dev=result.st_dev,
            st_ino=result.st_ino,
            st_size=result.st_size,
            st_mtime_ns=result.st_mtime_ns + 1,
            st_mode=result.st_mode,
        )

    monkeypatch.setattr(canary_collect.os, "fstat", changed_fstat)
    with pytest.raises(HarnessError) as ei:
        canary_collect.deployed_tree_digests(bundle)
    assert ei.value.detail.startswith("file changed during hashing: ")
    assert str(bundle) not in ei.value.detail


def test_new_cli_old_daemon_not_attributed(tmp_path):
    fakes = _fakes()
    from tools.stage3 import canary_collect

    bundle = fakes.make_bundle(tmp_path / "b")
    cli = bundle / "bin" / "serialwrap"
    daemon_sha = fakes.sha256_file(bundle / "bin" / "serialwrapd")

    aid, _m, check, _t = canary_collect.resolve_artifact(
        cli, daemon_pid=11, daemon_version="0.8.0", daemon_sha=lambda pid: daemon_sha
    )
    assert aid is None and check == "version_mismatch"

    old_launcher_sha = "e" * 64  # 版本相同但 daemon 執行檔為舊 launcher
    aid2, _m2, check2, _t2 = canary_collect.resolve_artifact(
        cli, daemon_pid=11, daemon_version="0.9.0", daemon_sha=lambda pid: old_launcher_sha
    )
    assert aid2 is None and check2 == "daemon_mismatch"


def test_cli_binary_swapped_not_attributed(tmp_path):
    fakes = _fakes()
    from tools.stage3 import canary_collect

    bundle = fakes.make_bundle(tmp_path / "b")
    cli = bundle / "bin" / "serialwrap"
    cli.write_bytes(b"#!/bin/sh\necho swapped\n")  # 改寫 CLI binary，manifest 未動
    daemon_sha = fakes.sha256_file(bundle / "bin" / "serialwrapd")
    aid, _m, check, _t = canary_collect.resolve_artifact(
        cli, daemon_pid=11, daemon_version="0.9.0", daemon_sha=lambda pid: daemon_sha
    )
    assert aid is None and check == "cli_mismatch"


def test_resolve_artifact_missing(tmp_path):
    from tools.stage3 import canary_collect

    cli = tmp_path / "bin" / "serialwrap"
    cli.parent.mkdir(parents=True)
    cli.write_bytes(b"x")
    res = canary_collect.resolve_artifact(cli, daemon_pid=11, daemon_version="0.9.0", daemon_sha=lambda pid: None)
    assert res == (None, None, "unresolved", None)


# --------------------------------------------------------------------------- #
# 打包與 stdlib 限制
# --------------------------------------------------------------------------- #
def test_pack_collector_runs_outside_repo(tmp_path):
    fakes = _fakes()
    from tools.stage3.pack_collector import pack

    pyz = pack(tmp_path / "sw-canary.pyz")
    elsewhere = tmp_path / "elsewhere"
    elsewhere.mkdir()
    help_run = subprocess.run(
        [sys.executable, str(pyz), "collect", "--help"],
        cwd=elsewhere, capture_output=True, text=True, timeout=120,
    )
    assert help_run.returncode == 0, help_run.stderr

    draft = fakes.write_policy(tmp_path / "draft.json", status="draft", frozen_by=None, frozen_at=None, decision_ref=None)
    once = subprocess.run(
        [
            sys.executable, str(pyz), "collect", "--once",
            "--cli", str(tmp_path / "no-such-cli"),
            "--ledger-dir", str(tmp_path / "ledger"),
            "--policy", str(draft),
            "--platform", "linux",
        ],
        cwd=elsewhere, capture_output=True, text=True, timeout=120,
    )
    assert once.returncode == 2
    body = json.loads(once.stdout)  # 原因碼登錄表必須可自 zip 內讀取
    assert body["reason_code"] == "HARNESS_CANARY_POLICY_NOT_FROZEN"

    with zipfile.ZipFile(pyz) as z:
        names = z.namelist()
    assert any(n.endswith("stage3_reason_codes.json") for n in names)
    assert not any(n.endswith("ab_runner.py") for n in names)
    assert not any("sw_core" in n for n in names)


def test_pack_collector_uses_reproducible_zip_metadata(tmp_path):
    from tools.stage3.pack_collector import pack

    first = pack(tmp_path / "first.pyz")
    with zipfile.ZipFile(first) as archive:
        infos = archive.infolist()
    assert infos
    assert all(info.date_time == (1980, 1, 1, 0, 0, 0) for info in infos)
    assert all((info.external_attr >> 16) & 0o777 == 0o644 for info in infos)

    second = pack(tmp_path / "second.pyz")
    assert first.read_bytes() == second.read_bytes()


def test_collector_revision_hash_is_computed_once_per_process(tmp_path, monkeypatch):
    from tools.stage3 import canary_collect

    pyz = tmp_path / "sw-canary.pyz"
    pyz.write_bytes(b"running collector")
    calls: list[Path] = []

    def digest(path):
        calls.append(Path(path))
        return "a" * 64

    clear_cache = getattr(canary_collect.collector_revision, "cache_clear", lambda: None)
    clear_cache()
    monkeypatch.setattr(sys, "argv", [str(pyz)])
    monkeypatch.setattr(canary_collect.jsonio, "sha256_file", digest)
    try:
        assert canary_collect.collector_revision() == "a" * 64
        assert canary_collect.collector_revision() == "a" * 64
        assert calls == [pyz]
    finally:
        getattr(canary_collect.collector_revision, "cache_clear", lambda: None)()


@pytest.mark.parametrize(
    "argv",
    [
        lambda tmp_path: [
            "note", "--ledger-dir", str(tmp_path / "ledger"), "--kind", "planned_restart",
            "--from", "2026-11-02T01:00:00", "--to", "2026-11-02T02:00:00+00:00",
            "--reason", "未使用時區", "--author", "owner", "--host-id", "h1", "--platform", "linux",
        ],
        lambda tmp_path: [
            "collect", "--once", "--cli", str(tmp_path / "serialwrap"), "--ledger-dir", str(tmp_path / "ledger"),
            "--policy", str(tmp_path / "missing-policy.json"), "--platform", "linux",
        ],
    ],
)
def test_cli_value_and_file_errors_are_json(argv, tmp_path, capsys):
    from tools.stage3.collector_main import main

    assert main(argv(tmp_path)) == 2
    result = json.loads(capsys.readouterr().out)
    assert result["ok"] is False and result["reason_code"] == "HARNESS_CLI_ERROR"


def test_cli_output_uses_utf8_with_cp1252_console(tmp_path):
    args = [
        "note", "--ledger-dir", str(tmp_path / "ledger"), "--kind", "planned_restart",
        "--from", "2026-11-02T01:00:00+00:00", "--to", "2026-11-02T02:00:00+00:00",
        "--reason", "重啟驗證", "--author", "owner", "--host-id", "h1", "--platform", "linux",
    ]
    code = "from tools.stage3.collector_main import main; raise SystemExit(main(" + repr(args) + "))"
    env = {**os.environ, "PYTHONIOENCODING": "cp1252"}
    proc = subprocess.run(
        [sys.executable, "-c", code], cwd=REPO, env=env, capture_output=True, text=True, encoding="utf-8", timeout=30
    )
    assert proc.returncode == 0, proc.stderr
    assert "重啟驗證" in proc.stdout
    assert json.loads(proc.stdout)["record"]["reason"] == "重啟驗證"


def test_collector_modules_stdlib_only():
    from tools.stage3.pack_collector import COLLECTOR_MODULES

    bad: list[tuple[str, str]] = []
    for name in COLLECTOR_MODULES:
        path = REPO / "tools" / "stage3" / ("__init__.py" if name == "__init__" else f"{name}.py")
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                tops = [alias.name.split(".")[0] for alias in node.names]
            elif isinstance(node, ast.ImportFrom) and node.level == 0 and node.module:
                tops = [node.module.split(".")[0]]
            else:
                continue
            for top in tops:
                if top not in sys.stdlib_module_names and top != "tools":
                    bad.append((name, top))
    assert bad == []

