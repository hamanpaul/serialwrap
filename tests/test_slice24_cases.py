"""Slice 2.4（P8 Task 1）案例登記、契約增補登記與斷路器凍結的一致性測試。

依 `docs/rust-refactor/plans/09-slice24-transfer-flash.md` Task 1 Step 1，並依編排者 brief（2026-11-07）與擁有者裁決修正：
- SC-33 依擁有者 OD-79（`reviews/rulings/s2.md`）撤回、不登記，亦不新增 `pending_owner_confirmations` item 8；
  原列 SC-33 的 `s24-xfer-session-gates` 改列 SC-03，並依 OD-74 列 DF-02。
- XFER-ABORT-01 依擁有者 OD-62 比照 BC-03：`s24-xfer-abort-on-release` 的契約為 `FLUSHED_BY_RECOVERY`／
  `COMMAND_ABORTED_IN_FLIGHT`＋WAL META `command_aborted_in_flight`，不寫計畫原稿的 framing `EXCEPTION`。
- 凍結檔一律由 `scripts/rust/freeze_slice_cases.py` 產生（不手寫），`stage2_started_at` 沿用 Slice 2.1；
  凍結後 lock 只會變大，分母不得縮小（有效分母恆為 lock `sets["slice_2.4"]` 的子集）。
- `cases.yaml` revision 每次前進，已凍結切片都要以 OD-76 的 `revision_advance` 留修訂紀錄。
"""
from __future__ import annotations

import hashlib
import importlib.util
import json
import subprocess
import sys
from pathlib import Path

import pytest
import yaml

# 本檔混合 memory_only 與 process_rpc（子程序呼叫 regen_lock／slice_breaker），模組層只放平台 marker（T14 規則）
pytestmark = [pytest.mark.core_portable]

ROOT = Path(__file__).resolve().parents[1]
ACC = ROOT / "tests" / "acceptance"
BREAKER = ACC / "breaker"
FREEZE24 = BREAKER / "slice_2.4.json"
SNAPSHOTS24 = BREAKER / "slice_2.4.snapshots.ndjson"
STAGE_A = "P8-T1 階段 A：s24 案例、SC-32 與 Slice 2.4 凍結尚未登記"

# Task 1 案例表（brief 修正後）：case_id → (suite, layer, platforms, capabilities, prerequisites, invariants, behavior_changes)
TABLE: dict[str, tuple[str, str, set[str], set[str], set[str], set[str], set[str]]] = {
    "s24-xfer-templates-golden": ("rust-l1", "L1", {"linux", "windows"}, {"memory_only"}, {"file_transfer"}, set(), set()),
    "s24-xfer-push-roundtrip": ("contract-blackbox", "L1.5", {"linux"}, {"posix_pty", "process_rpc"},
                                {"file_transfer"}, set(), set()),
    "s24-xfer-pull-roundtrip": ("contract-blackbox", "L1.5", {"linux"}, {"posix_pty", "process_rpc"},
                                {"file_transfer"}, set(), set()),
    "s24-xfer-error-shapes": ("rust-l1", "L1", {"linux", "windows"}, {"memory_only"}, {"file_transfer"},
                              {"zero_tx_on_reject"}, set()),
    "s24-xfer-echo-stall": ("contract-blackbox", "L1.5", {"linux"}, {"posix_pty", "process_rpc"}, {"file_transfer"},
                            {"no_auto_resend"}, set()),
    # SC-33 依 OD-79 撤回 → 改列 SC-03；DF-02 依 OD-74（明寫 human:<x> 比照 agent）
    "s24-xfer-session-gates": ("contract-blackbox", "L1.5", {"linux"}, {"posix_pty", "process_rpc"},
                               {"file_transfer", "quiet_reconfirm", "lease"},
                               {"zero_tx_on_reject", "no_forbidden_interleave", "no_stale_context_replay"},
                               {"SC-02", "SC-03", "SC-14", "DF-02"}),
    # OD-62：file.* 傳輸中止比照 BC-03（BC-03 compatibility_impact 已含 file.*）
    "s24-xfer-abort-on-release": ("contract-blackbox", "L1.5", {"linux"}, {"posix_pty", "process_rpc"},
                                  {"file_transfer", "drain_release"}, {"no_auto_resend", "no_stale_context_replay"},
                                  {"SC-04", "BC-03"}),
    "s24-xfer-no-retry-side-effect": ("contract-blackbox", "L1.5", {"linux", "windows"}, {"process_rpc"},
                                      {"file_transfer", "device_handoff_full"}, {"no_auto_resend"}, set()),
    "s24-flash-silent-unbridged": ("contract-blackbox", "L1.5", {"linux"}, {"posix_pty", "process_rpc"}, {"mcu_flash"},
                                   {"zero_tx_on_reject"}, set()),
    "s24-flash-sync-replay": ("contract-blackbox", "L1.5", {"linux"}, {"posix_pty", "process_rpc"}, {"mcu_flash"},
                              {"no_auto_resend"}, set()),
    "s24-flash-holder-gone": ("contract-blackbox", "L1.5", {"linux"}, {"posix_pty", "process_rpc"}, {"mcu_flash"},
                              set(), set()),
    "s24-flash-baud-mirror": ("contract-blackbox", "L1.5", {"linux"}, {"posix_pty", "process_rpc"}, {"mcu_flash"},
                              set(), set()),
    "s24-flash-arbitration": ("rust-l1", "L1", {"linux", "windows"}, {"memory_only"}, {"mcu_flash", "deferred_buffer"},
                              {"zero_tx_on_reject", "no_stale_context_replay"}, {"SC-02", "SC-14", "BC-02"}),
    "s24-flash-ioerror-recovering": ("rust-l1", "L1", {"linux", "windows"}, {"memory_only"},
                                     {"mcu_flash", "recovery_min"}, {"no_reopen_before_handoff"}, {"SC-32"}),
    "s24-mcu-rpc-shapes": ("contract-blackbox", "L1.5", {"linux", "windows"}, {"process_rpc"}, {"mcu_flash"},
                           set(), set()),
    "s24-ho-ext-tool": ("contract-blackbox", "L1.5", {"linux"}, {"posix_pty", "process_rpc"}, {"device_handoff_full"},
                        {"no_reopen_before_handoff"}, {"SC-05", "BC-04"}),
    "s24-ho-second-daemon": ("contract-blackbox", "L1.5", {"linux"}, {"posix_pty", "process_rpc"},
                             {"device_handoff_full"}, {"no_reopen_before_handoff"}, {"SC-05"}),
    "s24-ho-persist-restart": ("contract-blackbox", "L1.5", {"linux"}, {"posix_pty", "process_rpc"},
                               {"device_handoff_full", "state_persistence"}, set(), {"DF-04"}),
    "s24-ho-windows-com": ("contract-blackbox", "L1.5", {"windows"}, {"windows_com", "process_rpc"},
                           {"device_handoff_full"}, {"no_reopen_before_handoff"}, {"BC-04"}),
    "s24-sup-systemd-runtime": ("contract-blackbox", "L1.5", {"linux"}, {"supervisor_runtime", "posix_pty"},
                                {"supervision", "file_transfer", "mcu_flash"}, {"no_auto_resend"}, {"SC-11"}),
    "s24-sup-wintask-runtime": ("rust-l1", "L1.5", {"windows"}, {"supervisor_runtime"}, {"supervision"},
                                set(), {"SC-24"}),
    "s24-upd-guards": ("contract-blackbox", "L1.5", {"linux"}, {"posix_pty", "process_rpc"},
                       {"supervision", "file_transfer", "mcu_flash", "lease"}, {"no_reopen_before_handoff"}, {"SC-23"}),
    "s24-upd-rollback-cross-stage": ("contract-blackbox", "L1.5", {"linux"}, {"posix_pty", "process_rpc"},
                                     {"supervision", "device_handoff_full"}, {"no_reopen_before_handoff"}, {"SC-23"}),
    "s24-contract-coverage": ("rust-l1", "L1", {"linux", "windows"}, {"memory_only"}, set(),
                              {"insufficient_evidence_not_pass"}, set()),
}
P8_IDS = set(TABLE)
REUSED_P2 = {"f7-binary-roundtrip-md5", "f7-larger-file-not-truncated", "p1-cmd-file", "p1-ho-cycle",
             "p1-ho-persist", "p1-rst-daemon", "lr-mixed", "win-task-restart-on-failure", "win-task-72h",
             "p2-1-mcu-flash-manual"}
COMMON = {"family": "S24", "daemon_slice": "2.4", "necessity": "required", "stage0_required": False,
          "destructive": False, "expected": "PASS", "sentinel": None, "issues": [],
          "implemented_by": "09-slice24-transfer-flash.md", "cross_slice_reason": None}
# cases.schema.json 列舉於 P8 Task 1 之前的既有值（只增不改：既有值必須全數保留）
SUITES_BEFORE = {"regression", "realhw", "func-test", "l15-scenario", "contract-blackbox", "windows-native",
                 "manual-procedure"}
FAMILIES_BEFORE = {"F1", "F2", "F3", "F4", "F5", "F6", "F7", "F8", "F9", "F10", "F11", "F12", "P0", "P1", "remote",
                   "longrun", "L15", "WIN", "MANUAL"}


def _cases() -> dict:
    return {c["case_id"]: c for c in yaml.safe_load((ACC / "cases.yaml").read_text(encoding="utf-8"))["cases"]}


def _p8_cases() -> dict:
    """回傳全部案例；P8 案例缺漏時以斷言失敗（紅燈來自斷言，不來自 KeyError）。"""
    by_id = _cases()
    missing = sorted(P8_IDS - by_id.keys())
    assert not missing, f"cases.yaml 缺 P8 案例：{missing}"
    return by_id


def _load(name: str, rel: str):
    spec = importlib.util.spec_from_file_location(name, ROOT / rel)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def _catalog() -> dict:
    return yaml.safe_load((ROOT / "contracts" / "behavior_changes.yaml").read_text(encoding="utf-8"))


def _revision_key(rev: str) -> tuple[int, ...]:
    head, _, n = str(rev).partition(".")
    return tuple(int(p) for p in head.split("-")) + (int(n),)


@pytest.mark.memory_only
@pytest.mark.xfail(strict=True, reason=STAGE_A)
def test_schema_enums_add_only():
    case = json.loads((ACC / "cases.schema.json").read_text(encoding="utf-8"))["$defs"]["case"]["properties"]
    suites, families = set(case["suite"]["enum"]), set(case["family"]["enum"])
    assert SUITES_BEFORE <= suites and FAMILIES_BEFORE <= families, "列舉只增不改：既有值不得移除"
    assert "rust-l1" in suites and "S24" in families


@pytest.mark.memory_only
@pytest.mark.xfail(strict=True, reason=STAGE_A)
def test_p8_cases_follow_p2_schema_and_slice():
    by_id = _cases()
    assert not (P8_IDS - by_id.keys()), sorted(P8_IDS - by_id.keys())
    assert len(P8_IDS) == len(TABLE)
    for cid in sorted(P8_IDS):
        c = by_id[cid]
        assert {k: c[k] for k in COMMON} == COMMON, cid
        assert isinstance(c["platforms"], list) and isinstance(c["capabilities"], list), cid
        assert c["contract"].strip() and c["title"].strip() and c["source_ref"].strip(), cid
        assert isinstance(c["evidence"], list) and c["evidence"] and all(isinstance(e, str) and e for e in c["evidence"]), cid
    extra = sorted(cid for cid in by_id if cid.startswith("s24-") and cid not in P8_IDS)
    assert extra == [], f"Task 1 只登記計畫表列的 s24 案例：{extra}"


@pytest.mark.memory_only
@pytest.mark.xfail(strict=True, reason=STAGE_A)
def test_p8_case_rows_match_task1_table():
    by_id = _p8_cases()
    for cid, (suite, layer, platforms, caps, prereqs, invariants, bcs) in sorted(TABLE.items()):
        c = by_id[cid]
        got = (c["suite"], c["layer"], set(c["platforms"]), set(c["capabilities"]), set(c["prerequisites"]),
               set(c["invariants"]), set(c["behavior_changes"]))
        assert got == (suite, layer, platforms, caps, prereqs, invariants, bcs), cid


@pytest.mark.memory_only
@pytest.mark.xfail(strict=True, reason=STAGE_A)
def test_p8_case_windows_status_and_cli_stage_rules():
    """windows_status：platforms 含 windows → native，否則 gap 並附理由；cli_stage：supervision 前置或
    supervisor_runtime 能力 → 1B，其餘 1A（Task 1 共同欄位規則）。"""
    by_id = _p8_cases()
    for cid in sorted(P8_IDS):
        c = by_id[cid]
        if "windows" in c["platforms"]:
            assert (c["windows_status"], c["windows_gap_reason"]) == ("native", None), cid
        else:
            assert c["windows_status"] == "gap" and str(c["windows_gap_reason"] or "").strip(), cid
        needs_1b = "supervision" in c["prerequisites"] or "supervisor_runtime" in c["capabilities"]
        assert c["cli_stage"] == ("1B" if needs_1b else "1A"), cid
        assert "platform_capabilities" not in c, cid


@pytest.mark.memory_only
@pytest.mark.xfail(strict=True, reason=STAGE_A)
def test_p8_case_sources_are_nodeids():
    """source_ref 為 `<路徑>::<測試名>`；contract-blackbox 案例另須符合下游切片分群規則（tests/contract_blackbox/ 或
    crates/ 開頭、全切片唯一，見 tests/test_acceptance_cases.py::_split_contract_blackbox）。evidence 為 pytest 或
    `cargo:<crate>::<test_file>::<name>` nodeid（Task 15 宣告式對應的來源）。"""
    by_id = _p8_cases()
    for cid in sorted(P8_IDS):
        c = by_id[cid]
        ref = c["source_ref"]
        assert "::" in ref and ref.split("::", 1)[0].endswith((".py", ".rs")), cid
        if c["suite"] == "contract-blackbox":
            assert ref.startswith(("tests/contract_blackbox/", "crates/")), cid
        for ev in c["evidence"]:
            assert ev.startswith("cargo:") or (".py::" in ev and ev.startswith("tests/")), (cid, ev)
            if ev.startswith("cargo:"):
                assert len(ev.removeprefix("cargo:").split("::")) == 3, (cid, ev)
    refs = [c["source_ref"] for c in by_id.values() if c["suite"] == "contract-blackbox" and c["case_id"] in P8_IDS]
    assert len(refs) == len(set(refs)), "contract-blackbox source_ref 不得重複"


@pytest.mark.memory_only
@pytest.mark.xfail(strict=True, reason=STAGE_A)
def test_owner_rulings_reflected_in_cases():
    by_id = _p8_cases()
    gates = by_id["s24-xfer-session-gates"]
    assert "SC-33" not in gates["behavior_changes"], "SC-33 依 OD-79 撤回"
    assert {"SC-03", "DF-02"} <= set(gates["behavior_changes"])
    assert "AUTOBOOT_QUIET" in gates["contract"] and "retry_after_s" in gates["contract"]
    abort = by_id["s24-xfer-abort-on-release"]
    for token in ("FLUSHED_BY_RECOVERY", "COMMAND_ABORTED_IN_FLIGHT", "command_aborted_in_flight", "OD-62"):
        assert token in abort["contract"], token
    assert "EXCEPTION" not in abort["contract"], "OD-62 已裁決比照 BC-03，不得以 framing EXCEPTION 為契約"
    assert "RECOVERING" in by_id["s24-flash-ioerror-recovering"]["contract"]


@pytest.mark.memory_only
def test_no_aggregate_or_duplicate_of_p2_cases():
    by_id = _cases()
    assert REUSED_P2 <= by_id.keys()
    assert not [cid for cid in by_id if cid.startswith("s24-") and by_id[cid]["layer"] in ("L2", "L3", "manual")], \
        "L2／L3／手動一律引用 P2 個別案例，不得新增聚合案例"


def _regen_check() -> subprocess.CompletedProcess:
    return subprocess.run([sys.executable, str(ACC / "regen_lock.py"), "--check"], capture_output=True, text=True,
                          encoding="utf-8", cwd=ROOT)


@pytest.mark.process_rpc
@pytest.mark.xfail(strict=True, reason=STAGE_A)
def test_lock_contains_p8_and_never_shrinks():
    r = _regen_check()
    assert r.returncode == 0, r.stderr
    lock = json.loads((ACC / "cases.lock.json").read_text(encoding="utf-8"))
    # f7-binary-roundtrip-md5 自 P2（4dc3cc6e）即為 necessity: conditional，依 regen_lock.compute_sets 不入切片分母；
    # 計畫／brief「slice_2.4 含 f7-binary-roundtrip-md5」不成立（改為 required 屬分母屬性變更，需擁有者裁決）。
    assert P8_IDS | {"p1-cmd-file"} <= set(lock["sets"]["slice_2.4"])
    by_id = _cases()
    assert all(by_id[cid]["necessity"] == "required" for cid in lock["sets"]["slice_2.4"])
    assert {cid for cid in lock["sets"]["slice_2.4"] if cid.startswith("s24-")} == P8_IDS


@pytest.mark.memory_only
@pytest.mark.xfail(strict=True, reason=STAGE_A)
def test_new_behavior_changes_registered_and_keep_items_not():
    doc = _catalog()
    bc = {c["id"]: c for c in doc["changes"]}
    assert "SC-32" in bc, "SC-32 尚未登記"
    sc32 = bc["SC-32"]
    assert sc32["kind"] == "SC" and sc32["scope"] == "rust_only" and sc32["ab_participation"] == "absolute_only"
    assert sc32["applies_to"] == {"python": None, "rust": {"stage": "slice-2.4"}}
    assert sc32["phases"] == ["P8"] and sc32["case_ids"] == ["s24-flash-ioerror-recovering"]
    assert sc32["approval"]["basis"] == "spec_final" and sc32["approval"]["owner"] == "spec-final"
    assert sc32["owner_confirmation"] == "not_required" and sc32["owner_item"] is None
    assert sc32["status"] in {"APPROVED", "IMPLEMENTED", "VERIFIED"}
    assert 1 <= sc32["approved_at_revision"] <= doc["revision"]
    for token in ("UartIoError", "DeviceDetached", "RECOVERING", "flash_mode"):
        assert token in sc32["compatibility_impact"], token
    assert "SC-33" not in bc, "SC-33 依擁有者 OD-79 撤回，不得登記"
    pending = doc.get("pending_owner_confirmations") or []
    assert all(o.get("item") != 8 for o in pending), "OD-79 一併撤回 pending owner item 8"
    assert "SC-33" not in json.dumps(pending, ensure_ascii=False)
    assert "SC-08" not in bc                                                  # OD-14 #3 撤回
    assert not any(k.startswith(("XFER-", "MCU-STATUS", "EVT-D-", "SC-23-G", "SC-24-S")) for k in bc), \
        "KEEP 項只寫調和表，不登記 behavior_changes.yaml"


@pytest.mark.memory_only
@pytest.mark.xfail(strict=True, reason=STAGE_A)
def test_reconciliation_table_updated():
    text = (ROOT / "docs/rust-refactor/plans/spec-reconciliation.md").read_text(encoding="utf-8")
    rows = {line.split("|")[1].strip(): line for line in text.splitlines()
            if line.startswith("| ") and line.count("|") >= 3}
    missing = sorted({"SC-32", "SC-33", "XFER-ABORT-01", "MCU-STATUS-01", "EVT-D-F4"} - rows.keys())
    assert not missing, f"調和表缺列：{missing}"
    assert "Rust-only" in rows["SC-32"] and "UartIoError" in rows["SC-32"]
    assert "OD-79" in rows["SC-33"] and "撤回" in rows["SC-33"]
    xfer = rows["XFER-ABORT-01"]
    assert "KEEP" in xfer and "OD-62" in xfer
    assert "FLUSHED_BY_RECOVERY" in xfer and "COMMAND_ABORTED_IN_FLIGHT" in xfer
    assert "KEEP" in rows["MCU-STATUS-01"] and "mcu.status" in rows["MCU-STATUS-01"]
    f4 = rows["EVT-D-F4"]
    assert "OD-80" in f4 and f4.rstrip(" |").rsplit("|", 1)[-1].strip().startswith("yes"), "EVT-D-F4 需擁有者欄"


def _effective(path: Path) -> dict:
    return _load("freeze_slice_cases_s24", "scripts/rust/freeze_slice_cases.py").load_effective_freeze(path)


@pytest.mark.memory_only
@pytest.mark.xfail(strict=True, reason=STAGE_A)
def test_breaker_freeze_matches_lock():
    assert FREEZE24.exists(), "Slice 2.4 凍結檔須由 freeze_slice_cases.py 產生"
    assert SNAPSHOTS24.exists(), "每週快照檔須與凍結檔一併 check-in"
    base = json.loads(FREEZE24.read_text(encoding="utf-8"))
    for key in ("cases_lock_sha256", "cases_yaml_sha256", "denominator_sha256", "required_case_ids",
                "case_fingerprints", "stage2_started_at"):
        assert key in base, f"凍結檔缺 {key}（不得手寫，須由工具產生）"
    fr = _effective(FREEZE24)
    lock_bytes = (ACC / "cases.lock.json").read_bytes()
    lock = json.loads(lock_bytes)
    cases_doc = yaml.safe_load((ACC / "cases.yaml").read_text(encoding="utf-8"))
    assert (fr["schema_version"], fr["slice"], fr["denominator_set"]) == (1, "2.4", "slice_2.4")
    assert fr["window"] == {"start": fr["frozen_at"], "weeks": 3}
    origin = json.loads((BREAKER / "slice_2.1.json").read_text(encoding="utf-8"))["stage2_started_at"]
    assert fr["stage2_started_at"] == origin, "Stage 2 起算日沿用 Slice 2.1（OD-02 單一起點）"
    assert fr["denominator"] == len(fr["required_case_ids"]) == len(set(fr["required_case_ids"]))
    # 凍結後新增案例只會讓 lock 變大；分母不得縮小、凍結的必要案例不得自 lock 消失
    assert set(fr["required_case_ids"]) <= set(lock["sets"]["slice_2.4"])
    assert P8_IDS | {"p1-cmd-file"} <= set(fr["required_case_ids"])      # f7-binary-roundtrip-md5 為 conditional，見上
    assert set(_effective(BREAKER / "slice_2.3.json")["required_case_ids"]) <= set(fr["required_case_ids"]), \
        "2.1～2.3 回歸須含於 2.4 分母"
    canon = json.dumps([[cid, lock["case_attrs"][cid]] for cid in fr["required_case_ids"]],
                       ensure_ascii=False, separators=(",", ":"), sort_keys=True)
    assert fr["denominator_sha256"] == hashlib.sha256(canon.encode("utf-8")).hexdigest()
    assert _revision_key(fr["cases_revision"]) <= _revision_key(cases_doc["revision"])
    if fr["cases_revision"] == lock["cases_revision"]:
        assert fr["required_case_ids"] == lock["sets"]["slice_2.4"]
        assert fr["cases_lock_sha256"] == hashlib.sha256(lock_bytes).hexdigest()
        assert fr["cases_yaml_sha256"] == hashlib.sha256((ACC / "cases.yaml").read_bytes()).hexdigest()


@pytest.mark.memory_only
@pytest.mark.parametrize("slice_id", ["2.1", "2.2", "2.3"])
def test_frozen_slices_follow_cases_revision(slice_id):
    """OD-76：cases.yaml revision 每次前進，已凍結切片的有效凍結內容都要以 revision_advance 等修訂跟上，
    且分母與凍結時相同（扣除逐名核准的延後）。"""
    base_path = BREAKER / f"slice_{slice_id}.json"
    fr = _effective(base_path)
    cases_doc = yaml.safe_load((ACC / "cases.yaml").read_text(encoding="utf-8"))
    lock = json.loads((ACC / "cases.lock.json").read_text(encoding="utf-8"))
    assert fr["cases_revision"] == cases_doc["revision"] == lock["cases_revision"]
    assert fr["required_case_ids"] == lock["sets"][f"slice_{slice_id}"]


def _run_breaker(*args: str) -> subprocess.CompletedProcess:
    return subprocess.run([sys.executable, str(ROOT / "scripts/rust/slice_breaker.py"), *args],
                          capture_output=True, text=True, encoding="utf-8", cwd=ROOT)


@pytest.mark.process_rpc
@pytest.mark.xfail(strict=True, reason=STAGE_A)
def test_breaker_cli_does_not_trip_on_frozen_slice24():
    """新凍結檔＋空快照於窗口起點判定：未觸發、未證據不足、未超過 OD-02 上限（OD-60：stage2_estimate_weeks=6）。"""
    assert FREEZE24.exists()
    frozen_at = json.loads(FREEZE24.read_text(encoding="utf-8"))["frozen_at"]
    r = _run_breaker("--freeze", str(FREEZE24), "--snapshots", str(SNAPSHOTS24), "--today", frozen_at,
                     "--stage2-estimate-weeks", "6")
    assert r.returncode == 0, r.stderr
    out = json.loads(r.stdout)
    assert (out["tripped"], out["insufficient_evidence"], out["od02_resubmit"]) == (False, False, False)


@pytest.mark.memory_only
def test_breaker_trips_on_lt_10pp_over_three_weeks():
    sb = _load("sb_s24", "scripts/rust/slice_breaker.py")
    fr = {"window": {"start": "2026-11-02", "weeks": 3}, "stage2_started_at": "2026-08-01", "denominator": 100}
    snaps = [{"date": "2026-11-02", "rate": 0.40}, {"date": "2026-11-09", "rate": 0.43},
             {"date": "2026-11-16", "rate": 0.46}, {"date": "2026-11-23", "rate": 0.49}]
    out = sb.evaluate(fr, snaps, today="2026-11-23", stage2_estimate_weeks=4)
    assert out["tripped"] is True and round(out["window_delta_pp"], 1) == 9.0
    snaps[-1]["rate"] = 0.51
    assert sb.evaluate(fr, snaps, today="2026-11-23", stage2_estimate_weeks=4)["tripped"] is False
    assert sb.evaluate(fr, snaps, today="2027-03-01", stage2_estimate_weeks=4)["od02_resubmit"] is True
