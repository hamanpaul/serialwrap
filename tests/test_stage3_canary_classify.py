# tests/test_stage3_canary_classify.py
"""P9-T10 干擾／交接分類與活躍日（tools/stage3/canary_classify.py）的紅燈測試（TDD 第一輪）。

依 10-stage3-canary.md Task 10：本檔一律於測試函式內以 importlib 延遲載入 `tools.stage3.canary_classify`、
`tools.stage3.canary_policy` 與 `stage3_fakes`，使 `python3 -m pytest -q --collect-only tests/` 在實作前後皆能完成收集。
本檔不實作任何 production 程式；替身與政策模組（Task 1、Task 2）尚未存在時，紅燈原因即為模組不存在（ModuleNotFoundError）。

計畫內部的不一致（已於對應測試以註解標示）：
- Step 1 表格把「ended_steady=False 的 episode（序列末端、跨缺口、跨 daemon pid 變更）→ 無 cycle」寫為期望；
  但 Interfaces 規則明定這類 episode 仍輸出 `result="ended_non_ready"` 的 cycle（只作紀錄、不計入配額），
  且 `test_reconfirm_failed_is_not_steady` 亦要求此類 cycle 存在。因此上述三個測試改以
  「不得出現 `recovered_ready` cycle（即不計入配額）」斷言，兩種實作皆相容。
"""

from __future__ import annotations

import importlib
import itertools
from datetime import datetime, timedelta, timezone
from typing import Any

import pytest


pytestmark = [pytest.mark.core_portable, pytest.mark.memory_only]

COM = "COM0"
_IDS = itertools.count(1)


def _fakes():
    """延遲載入 tests/stage3_fakes.py（尚未實作時拋出 ModuleNotFoundError）。"""
    return importlib.import_module("stage3_fakes")


def _cc():
    """延遲載入 tools/stage3/canary_classify.py（尚未實作時拋出 ModuleNotFoundError）。"""
    return importlib.import_module("tools.stage3.canary_classify")


def _policy_mod():
    return importlib.import_module("tools.stage3.canary_policy")


def _pol(tmp_path, **over: Any):
    """以 stage3_fakes.write_policy 寫出 frozen 政策並由 canary_policy.load_policy 載入。"""
    fakes = _fakes()
    return _policy_mod().load_policy(fakes.write_policy(tmp_path / "p.json", **over))


def _iso(offset_s: int, *, hour: int = 1, day: int = 3) -> str:
    """2026-11-<day> <hour>:00:00 UTC 起算的 ISO 時間（與 stage3_fakes.ts 同格式）。"""
    base = datetime(2026, 11, day, hour, tzinfo=timezone.utc)
    return (base + timedelta(seconds=offset_s)).isoformat()


def _r(state: str, *, gen: int = 1, pid: int | None = 100, ok: bool = True, **extra: Any) -> dict[str, Any]:
    """一筆快照的 COM0 列描述：state、bridge_generation、daemon pid、daemon_ok 與 session 額外欄位。"""
    return {"state": state, "gen": gen, "pid": pid, "ok": ok, "extra": extra}


def _timeline(fakes, rows, *, start_s: int = 0, step_s: int = 30, hour: int = 1, com: str = COM) -> list[dict]:
    """每列一筆快照（每 step_s 秒一筆），session 只含 com 一筆。rows 元素可為狀態字串或 _r(...) 結果。"""
    out = []
    for i, raw in enumerate(rows):
        r = _r(raw) if isinstance(raw, str) else raw
        extra = dict(r["extra"])
        sess = (com, r["state"], r["gen"], extra) if extra else (com, r["state"], r["gen"])
        out.append(
            fakes.snapshot(
                _iso(start_s + i * step_s, hour=hour),
                pid=r["pid"],
                ok=r["ok"],
                sessions=(sess,),
            )
        )
    return out


def _nid() -> int:
    return next(_IDS)


def _submit(n: int, wall_ts: str, *, com: str = COM, source: str = "agent:a", platform: str = "linux") -> dict:
    """帳本 submit 紀錄（欄位依 Task 1 帳本紀錄型別表）。cmd_id 為 arbiter 形式 32 hex。"""
    return {
        "type": "submit",
        "record_id": f"submit-{_nid():06d}",
        "host_id": "h1",
        "platform": platform,
        "cmd_id": f"{n:032x}",
        "com": com,
        "source": source,
        "wall_ts": wall_ts,
        "seq": n,
        "wal_file": "raw.wal.ndjson",
    }


def _result(n: int, status: str, t_wall: str, *, error_code: str | None = None, platform: str = "linux") -> dict:
    """帳本 submit_result 紀錄（status 為 done／error／timeout／unknown）。"""
    return {
        "type": "submit_result",
        "record_id": f"result-{_nid():06d}",
        "host_id": "h1",
        "platform": platform,
        "cmd_id": f"{n:032x}",
        "status": status,
        "error_code": error_code,
        "t_wall": t_wall,
    }


def _signal(kind: str, wall_ts: str, *, com: str = COM, source: str = "system:recover", platform: str = "linux") -> dict:
    """帳本 wal_signal 紀錄（kind 為 Task 8 WalSignal.kind，例如 human_tx／recover_tx）。"""
    return {
        "type": "wal_signal",
        "record_id": f"signal-{_nid():06d}",
        "host_id": "h1",
        "platform": platform,
        "kind": kind,
        "com": com,
        "wall_ts": wall_ts,
        "seq": _nid(),
        "source": source,
        "detail": {},
    }


def _note(kind: str, frm: str, to: str, *, platform: str = "linux") -> dict:
    """帳本 note 紀錄（kind 為 planned_restart／planned_downtime；[from, to] 為計畫維護區間）。"""
    return {
        "type": "note",
        "record_id": f"note-{_nid():06d}",
        "host_id": "h1",
        "platform": platform,
        "kind": kind,
        "from": frm,
        "to": to,
        "reason": "owner-approved maintenance",
        "author": "owner",
        "t_wall": frm,
    }


def _content_snapshots(fakes, *, artifact: str = "art-1", bundle: str | None = None, runtime: str | None = None,
                       hour: int = 1) -> list[dict]:
    """涵蓋 [hour:00:00, hour:00:00+3600 s] 的 READY 快照，每 30 s 一筆（共 121 筆，間隔合計 3600 s）。"""
    bundle = fakes.BTREE if bundle is None else bundle
    runtime = fakes.RTREE if runtime is None else runtime
    return [
        fakes.snapshot(
            _iso(i * 30, hour=hour),
            artifact=artifact,
            bundle_tree=bundle,
            runtime_tree=runtime,
            sessions=((COM, "READY", 1),),
        )
        for i in range(121)
    ]


def _batch(n: int, *, start_s: int = 0, step_s: int = 60, hour: int = 1) -> tuple[list[dict], list[dict]]:
    """n 筆 submit 與各自的 done 結果（時間依 step_s 排列）。"""
    subs = [_submit(i + 1, _iso(start_s + i * step_s, hour=hour)) for i in range(n)]
    res = [_result(i + 1, "done", _iso(start_s + i * step_s, hour=hour)) for i in range(n)]
    return subs, res


# ---------------------------------------------------------------------------
# 計畫 Step 1 程式碼區塊內的測試
# ---------------------------------------------------------------------------


def test_release_attach_counts_once(tmp_path):
    fakes = _fakes()
    cc = _cc()
    pol = _pol(tmp_path)
    snaps = _timeline(fakes, ["READY", "RELEASED", "RELEASED", "ATTACHING", "READY"])
    cycles = cc.classify(snaps, [], [], [], pol, platform="linux")
    assert [c.type for c in cycles] == ["handoff_release_attach"]
    assert cycles[0].artifact_id == "art-1"
    assert (cycles[0].bundle_tree_sha256, cycles[0].runtime_tree_sha256) == (fakes.BTREE, fakes.RTREE)


def test_tree_change_between_snapshots_not_attributed():  # R4-F1，failing-first
    fakes = _fakes()
    cc = _cc()
    a = fakes.snapshot(fakes.ts(3, 1, 0), artifact="art-1", pid=10)
    b = fakes.snapshot(fakes.ts(3, 1, 1), artifact="art-1", pid=10, bundle_tree="e" * 64, runtime_tree="f" * 64)  # 同 manifest、同 pid，旁載後
    assert cc.attribute_artifact(fakes.ts(3, 1, 0, 30), [a, b]) is None
    c = fakes.snapshot(fakes.ts(3, 1, 2), artifact="art-1", pid=10, bundle_tree="e" * 64, runtime_tree="f" * 64)
    assert cc.attribute_artifact(fakes.ts(3, 1, 1, 30), [a, b, c]) == cc.Attribution("art-1", "e" * 64, "f" * 64)
    assert cc.attribute_artifact(fakes.ts(3, 1, 1, 30), [a, b, c]) != cc.Attribution("art-1", fakes.BTREE, fakes.RTREE)


def test_episode_across_gap_not_counted(tmp_path):
    fakes = _fakes()
    cc = _cc()
    pol = _pol(tmp_path)
    # 第一段 READY→RELEASED 之後快照間隔 29.5 分鐘（> 30 s × 3），episode 被切斷。
    # 計畫原文期望 classify == []；見檔頭說明，此處只斷言「不得計入配額」。
    snaps = _timeline(fakes, ["READY", "RELEASED"]) + _timeline(fakes, ["RELEASED", "READY"], start_s=1800)
    counted = [c for c in cc.classify(snaps, [], [], [], pol, platform="linux") if c.result == "recovered_ready"]
    assert counted == []


def test_hotplug_requires_generation_bump(tmp_path):
    fakes = _fakes()
    cc = _cc()
    pol = _pol(tmp_path)
    same = _timeline(fakes, [_r("READY", gen=4), _r("DETACHED", gen=4), _r("READY", gen=4)])
    bump = _timeline(fakes, [_r("READY", gen=4), _r("DETACHED", gen=4), _r("READY", gen=5)])
    assert cc.classify(same, [], [], [], pol, platform="linux") == []
    assert [c.type for c in cc.classify(bump, [], [], [], pol, platform="linux")] == ["hotplug_reattach"]


def test_flash_not_counted_on_windows(tmp_path):
    fakes = _fakes()
    cc = _cc()
    pol = _pol(tmp_path)
    snaps = [dict(s, platform="windows") for s in _timeline(fakes, ["READY", "FLASHING", "ATTACHED", "READY"])]
    assert cc.classify(snaps, [], [], [], pol, platform="windows") == []


def test_ambiguous_artifact_is_none():
    fakes = _fakes()
    cc = _cc()
    a = fakes.snapshot(fakes.ts(3, 1, 0), artifact="art-1", pid=10)
    b = fakes.snapshot(fakes.ts(3, 1, 1), artifact="art-2", pid=11)
    assert cc.attribute_artifact(fakes.ts(3, 1, 0, 30), [a, b]) is None


# ---------------------------------------------------------------------------
# 計畫 Step 1 表格內的測試
# ---------------------------------------------------------------------------


def test_dut_reboot_from_boot_quiet(tmp_path):
    fakes = _fakes()
    cc = _cc()
    pol = _pol(tmp_path)
    snaps = _timeline(
        fakes,
        [
            "READY",
            _r("READY", boot_quiet_remaining_s=12, ready_reconfirm_pending=True),
            _r("READY", ready_reconfirm_pending=True),
            "READY",
        ],
    )
    cycles = cc.classify(snaps, [], [], [], pol, platform="linux")
    assert [c.type for c in cycles] == ["dut_reboot"]
    assert cycles[0].result == "recovered_ready"


def test_reconfirm_failed_is_not_steady(tmp_path):
    fakes = _fakes()
    cc = _cc()
    pol = _pol(tmp_path)
    snaps = _timeline(fakes, ["READY", _r("READY", ready_reconfirm_failed=True)])
    cycles = cc.classify(snaps, [], [], [], pol, platform="linux")
    assert len(cycles) == 1
    assert cycles[0].result == "ended_non_ready"
    assert cycles[0].type == "dut_reboot"  # boot_signal 由 ready_reconfirm_failed 觸發（classify_episode 優先序）


def test_boot_quiet_ready_forms_episode(tmp_path):
    fakes = _fakes()
    cc = _cc()
    pol = _pol(tmp_path)
    # 守住產品事實：DUT reboot 時 session 維持 READY；READY＋boot quiet 不得視為穩態
    snaps = _timeline(fakes, ["READY", _r("READY", boot_quiet_remaining_s=12), "READY"])
    assert cc.episodes(snaps, pol) != []


def test_recovery_from_state(tmp_path):
    fakes = _fakes()
    cc = _cc()
    pol = _pol(tmp_path)
    snaps = _timeline(fakes, ["READY", "RECOVERING", "READY"])
    assert [c.type for c in cc.classify(snaps, [], [], [], pol, platform="linux")] == ["recovery"]


def test_recovery_from_unsnapshotted_recover_tx(tmp_path):
    fakes = _fakes()
    cc = _cc()
    pol = _pol(tmp_path)
    snaps = _timeline(fakes, ["READY"] * 5)  # 全程 READY，無 episode
    close = [_signal("recover_tx", _iso(10)), _signal("recover_tx", _iso(40))]  # 相隔 30 s
    far = [_signal("recover_tx", _iso(10)), _signal("recover_tx", _iso(310))]  # 相隔 300 s
    assert [c.type for c in cc.classify(snaps, [], close, [], pol, platform="linux")] == ["recovery"]
    assert [c.type for c in cc.classify(snaps, [], far, [], pol, platform="linux")] == ["recovery", "recovery"]


def test_recover_tx_inside_episode_not_double_counted(tmp_path):
    fakes = _fakes()
    cc = _cc()
    pol = _pol(tmp_path)
    snaps = _timeline(fakes, ["READY", "ATTACHED", "READY"])
    signals = [_signal("recover_tx", _iso(30))]  # 落在 episode（ATTACHED 快照）期間
    assert [c.type for c in cc.classify(snaps, [], signals, [], pol, platform="linux")] == ["recovery"]


def test_unfinished_episode_dropped(tmp_path):
    fakes = _fakes()
    cc = _cc()
    pol = _pol(tmp_path)
    snaps = _timeline(fakes, ["READY", "RELEASED"])  # 序列末端仍非穩態
    # 計畫原文期望 無 cycle；見檔頭說明，此處只斷言「不得計入配額」。
    counted = [c for c in cc.classify(snaps, [], [], [], pol, platform="linux") if c.result == "recovered_ready"]
    assert counted == []


def test_pid_change_splits_episode(tmp_path):
    fakes = _fakes()
    cc = _cc()
    pol = _pol(tmp_path)
    snaps = _timeline(
        fakes,
        [_r("READY", pid=100), _r("RELEASED", pid=100), _r("ATTACHING", pid=101), _r("READY", pid=101)],
    )
    # episode 中途 daemon pid 改變 → 切斷；計畫原文期望 無 cycle，見檔頭說明。
    counted = [c for c in cc.classify(snaps, [], [], [], pol, platform="linux") if c.result == "recovered_ready"]
    assert counted == []


def test_passthrough_attached_is_steady(tmp_path):
    fakes = _fakes()
    cc = _cc()
    pol = _pol(tmp_path)
    variants = [
        [_r("ATTACHED", command_capable=False), _r("ATTACHED", command_capable=False)],  # command_capable=False
        [_r("ATTACHED", platform="passthrough"), _r("ATTACHED", platform="passthrough")],  # platform=passthrough
        [  # 使用者自訂 profile 名稱的 passthrough：判定不依 profile 名稱
            _r("ATTACHED", platform="passthrough", profile="my-custom-probe"),
            _r("ATTACHED", platform="passthrough", profile="my-custom-probe"),
        ],
    ]
    for rows in variants:
        snaps = _timeline(fakes, rows)
        assert cc.episodes(snaps, pol) == []
        assert cc.classify(snaps, [], [], [], pol, platform="linux") == []


def test_passthrough_ready_is_steady(tmp_path):
    fakes = _fakes()
    cc = _cc()
    pol = _pol(tmp_path)
    # SC-35（OD-14 #2）：passthrough 持續 RX 停在 ATTACHED，安靜 30 s 後轉 READY；兩者皆為穩態，不構成 episode。
    # stuck_attached 屬 Task 11 事故範圍，本檔只斷言分類層的「無 episode、無 cycle」。
    for cap in (False, True):
        rows = [
            _r("ATTACHED", platform="passthrough", command_capable=cap),
            _r("ATTACHED", platform="passthrough", command_capable=cap),
            _r("ATTACHED", platform="passthrough", command_capable=cap),
            _r(
                "READY",
                platform="passthrough",
                command_capable=cap,
                boot_quiet_remaining_s=None,
                ready_reconfirm_pending=False,
            ),
            _r(
                "READY",
                platform="passthrough",
                command_capable=cap,
                boot_quiet_remaining_s=None,
                ready_reconfirm_pending=False,
            ),
        ]
        snaps = _timeline(fakes, rows)
        assert cc.episodes(snaps, pol) == [], f"command_capable={cap}"
        assert cc.classify(snaps, [], [], [], pol, platform="linux") == [], f"command_capable={cap}"


def test_coexist_bucket_and_cap(tmp_path):
    fakes = _fakes()
    cc = _cc()
    pol = _pol(tmp_path)
    submits: list[dict] = []
    signals: list[dict] = []
    for i in range(7):  # 同 com、同日 7 個不同 600 s 桶，每桶同時有 human_tx 與 agent submit
        t = _iso(i * 600, hour=9)
        signals.append(_signal("human_tx", t, source="human:alice"))
        submits.append(_submit(i + 1, t))
    cycles = cc.classify([], submits, signals, [], pol, platform="linux")
    assert len(cycles) == 5  # 每 (host, com, 本地日期) 至多 coexist_cap_per_day（5）
    assert {c.type for c in cycles} == {"human_agent_coexist"}


def test_planned_restart_cycle(tmp_path):
    fakes = _fakes()
    cc = _cc()
    pol = _pol(tmp_path)
    note = _note("planned_restart", _iso(-120, hour=10), _iso(300, hour=10))

    # (a) pid 100→101 且 note 覆蓋
    snaps = [fakes.snapshot(_iso(0, hour=10), pid=100), fakes.snapshot(_iso(120, hour=10), pid=101)]
    assert [c.type for c in cc.classify(snaps, [], [], [note], pol, platform="linux")] == ["planned_restart"]

    # (b) 中間夾一個 daemon_ok=False 的失聯快照，仍成立
    snaps_gap = [
        fakes.snapshot(_iso(0, hour=10), pid=100),
        fakes.snapshot(_iso(60, hour=10), ok=False, pid=None),
        fakes.snapshot(_iso(120, hour=10), pid=101),
    ]
    assert [c.type for c in cc.classify(snaps_gap, [], [], [note], pol, platform="linux")] == ["planned_restart"]

    # (c) 無 note → 無 cycle（由 Task 11 開事故）
    assert cc.classify(snaps, [], [], [], pol, platform="linux") == []


def test_unverified_artifact_not_attributed():
    fakes = _fakes()
    cc = _cc()
    # 兩側 artifact_check 皆為 version_mismatch
    a = fakes.snapshot(_iso(0, hour=1), artifact_check="version_mismatch")
    b = fakes.snapshot(_iso(60, hour=1), artifact_check="version_mismatch")
    assert cc.attribute_artifact(_iso(30, hour=1), [a, b]) is None
    # 兩側 verified 但 bundle_tree 缺值（R4-F1：無內容指紋不歸屬）
    a2 = fakes.snapshot(_iso(0, hour=1), bundle_tree=None)
    b2 = fakes.snapshot(_iso(60, hour=1), bundle_tree=None)
    assert cc.attribute_artifact(_iso(30, hour=1), [a2, b2]) is None
    # 補充：runtime_tree 缺值同樣不歸屬（計畫規則「分別相同且非 None」）
    a3 = fakes.snapshot(_iso(0, hour=1), runtime_tree=None)
    b3 = fakes.snapshot(_iso(60, hour=1), runtime_tree=None)
    assert cc.attribute_artifact(_iso(30, hour=1), [a3, b3]) is None


def test_active_day_threshold(tmp_path):
    fakes = _fakes()
    cc = _cc()
    pol = _pol(tmp_path)
    snaps = _content_snapshots(fakes)  # 覆蓋 3,600 s
    content = cc.Attribution("art-1", fakes.BTREE, fakes.RTREE)

    subs, res = _batch(19)
    assert cc.active_days(subs, snaps, pol, platform="linux", content=content, results=res) == []  # 19 個 → 非活躍

    subs, res = _batch(20)
    assert cc.active_days(subs, snaps, pol, platform="linux", content=content, results=res) == ["2026-11-03"]

    res[-1] = _result(20, "unknown", _iso(19 * 60), error_code="CMD_NOT_FOUND")  # 1 個結果 unknown → 不計
    assert cc.active_days(subs, snaps, pol, platform="linux", content=content, results=res) == []


def test_active_day_uses_utc_offset(tmp_path):
    fakes = _fakes()
    cc = _cc()
    pol = _pol(tmp_path, **{"platforms.linux.utc_offset_minutes": 480})  # UTC+8
    # UTC 2026-11-03 17:00–18:00 → 本地 2026-11-04 01:00–02:00
    snaps = _content_snapshots(fakes, hour=17)
    subs, res = _batch(20, hour=17)
    content = cc.Attribution("art-1", fakes.BTREE, fakes.RTREE)
    assert cc.active_days(subs, snaps, pol, platform="linux", content=content, results=res) == ["2026-11-04"]


def test_active_day_other_artifact_excluded(tmp_path):
    fakes = _fakes()
    cc = _cc()
    pol = _pol(tmp_path)
    snaps = _content_snapshots(fakes, artifact="art-2")  # 全部歸屬 art-2
    subs, res = _batch(20)
    assert cc.active_days(
        subs, snaps, pol, platform="linux", content=cc.Attribution("art-1", fakes.BTREE, fakes.RTREE), results=res
    ) == []
    # 補充正向對照：同一資料查 art-2 應為活躍，避免測試因恆回 [] 而假綠
    assert cc.active_days(
        subs, snaps, pol, platform="linux", content=cc.Attribution("art-2", fakes.BTREE, fakes.RTREE), results=res
    ) == ["2026-11-03"]


def test_active_day_other_tree_excluded(tmp_path):
    fakes = _fakes()
    cc = _cc()
    pol = _pol(tmp_path)
    # 同 artifact_id、同 manifest，但實際部署內容含旁載檔（R4-F1）
    snaps = _content_snapshots(fakes, artifact="art-1", bundle="e" * 64, runtime="f" * 64)
    subs, res = _batch(20)
    assert cc.active_days(
        subs, snaps, pol, platform="linux", content=cc.Attribution("art-1", fakes.BTREE, fakes.RTREE), results=res
    ) == []
    assert cc.active_days(
        subs, snaps, pol, platform="linux", content=cc.Attribution("art-1", "e" * 64, "f" * 64), results=res
    ) == ["2026-11-03"]


def test_coverage_ignores_gaps(tmp_path):
    fakes = _fakes()
    cc = _cc()
    pol = _pol(tmp_path)
    # 兩筆內容相同的快照相隔 10 分鐘（> 3 × 30 s），覆蓋秒數應為 0
    snaps = [fakes.snapshot(_iso(0, hour=1)), fakes.snapshot(_iso(600, hour=1))]
    subs, res = _batch(20, step_s=30)  # 20 個 submit 落在 [01:00, 01:10) 內，歸屬皆成立
    assert cc.active_days(
        subs, snaps, pol, platform="linux", content=cc.Attribution("art-1", fakes.BTREE, fakes.RTREE), results=res
    ) == []
