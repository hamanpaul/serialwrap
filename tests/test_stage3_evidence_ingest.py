"""P9-T14B 證據 ingest（`tools/stage3/evidence_ingest.py`）測試（TDD 紅燈階段）。

依據 `docs/rust-refactor/plans/10-stage3-canary.md` Task 14B 的測試表撰寫，涵蓋：
- 各 `kind` 的推導表（ab／canary／drill／junit／realhw／functest／regression／gate／manual／od09）
- 受測內容指紋（R3-F2／R4-F1）與 deployed_content 量測（R5-F4）
- L1 完整性判定（R5-F3）：委派 P2 `tests/acceptance/l1_judge.py`，不在 ingest 內重寫判定
- Canary 防回放（R3-F1）、OD-15 證據層級拒收

本檔只寫測試。所有 `tools.stage3.*` 與 `fakes_evidence`／`stage3_fakes`／`fakes_canary` 皆以 lazy import
放在測試函式內，因此在實作與替身尚未存在時仍可 collection，各測試以「模組不存在」的 ImportError 紅燈。
"""

from __future__ import annotations

import ast
import copy
import dataclasses
import hashlib
import json
import shutil
from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest
import yaml

import fakes_l1

pytestmark = [pytest.mark.core_portable, pytest.mark.memory_only]

REPO_ROOT = Path(__file__).resolve().parents[1]
INGEST_SRC = REPO_ROOT / "tools" / "stage3" / "evidence_ingest.py"
CASES_YAML = REPO_ROOT / "tests" / "acceptance" / "cases.yaml"
CASES_LOCK = REPO_ROOT / "tests" / "acceptance" / "cases.lock.json"
ACCEPTANCE_YAML = REPO_ROOT / "realhw" / "acceptance" / "longrun_acceptance.yaml"

NOW = "2026-11-02T10:00:00+00:00"
NOW2 = "2026-11-02T11:00:00+00:00"
NOW3 = "2026-11-02T12:00:00+00:00"
NOW4 = "2026-11-02T13:00:00+00:00"
NOW5 = "2026-11-02T14:00:00+00:00"
LATER = "2026-11-17T00:00:00+00:00"
SET_AT = "2026-10-31T00:00:00+00:00"  # fakes_evidence.set_target 使用的固定時間戳

L1_SET = "s3_release_contract"
LINUX_L1 = "S3-L1-RELEASE-CONTRACT-LINUX"
WINDOWS_L1 = "S3-L1-RELEASE-CONTRACT-WINDOWS"
NODES = [
    "tests/contract_blackbox/test_a.py::t1",
    "tests/contract_blackbox/test_a.py::t2",
    "tests/contract_blackbox/test_a.py::t3",
]
ENV_KEYS = ("os_build", "cpu", "ram_mb", "filesystem", "av_product", "drivers", "board_firmware", "toolchains")
FULL_CYCLES = {
    "handoff_release_attach": 10,
    "hotplug_reattach": 5,
    "dut_reboot": 10,
    "recovery": 10,
    "human_agent_coexist": 15,
}


# ---------------------------------------------------------------------------
# lazy 匯入（實作與替身尚未存在時不得在 collection 階段失敗）
# ---------------------------------------------------------------------------
def _ei():
    from tools.stage3 import evidence_ingest

    return evidence_ingest


def _ix():
    from tools.stage3 import evidence_index

    return evidence_index


def _ccl():
    from tools.stage3 import canary_ledger

    return canary_ledger


def _cpol():
    from tools.stage3 import canary_policy

    return canary_policy


def _ci():
    from tools.stage3 import canary_incidents

    return canary_incidents


def _ab():
    from tools.stage3 import ab_manifest

    return ab_manifest


def _rt():
    from tools.stage3 import retirement

    return retirement


def _harness_error():
    from tools.stage3.reason_codes import HarnessError

    return HarnessError


def _fe():
    import fakes_evidence

    return fakes_evidence


def _fk():
    import stage3_fakes

    return stage3_fakes


def _fc():
    import fakes_canary

    return fakes_canary


def _deployed_content():
    from realhw import deployed_content

    return deployed_content


# ---------------------------------------------------------------------------
# 一般輔助
# ---------------------------------------------------------------------------
def _sha(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _write_json(path: Path, doc: Any) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(doc, ensure_ascii=False, sort_keys=True, indent=1), encoding="utf-8")
    return path


def _cases() -> dict[str, dict]:
    doc = yaml.safe_load(CASES_YAML.read_text(encoding="utf-8"))
    return {c["case_id"]: c for c in doc["cases"]}


def _cases_lock() -> dict:
    return json.loads(CASES_LOCK.read_text(encoding="utf-8"))


def _layer_ids(item, platform: str) -> list[str]:
    """`required_case_set` ∩ {item.layer、platform 適用} 的案例 ID（與 Task 7／14B 的案例歸屬規則一致）。"""
    cases = _cases()
    if item.required_case_set:
        pool = _cases_lock()["sets"][item.required_case_set]
    else:
        pool = list(cases)
    return [c for c in pool if cases[c]["layer"] == item.layer and platform in cases[c]["platforms"]]


def _env_file(root: Path) -> Path:
    return _write_json(root / "environment.json", {k: f"fake-{k}" for k in ENV_KEYS})


def _world(root: Path, *, stage: str = "3", platforms: tuple[str, ...] = ("linux", "windows")) -> SimpleNamespace:
    """建立證據索引與 (stage, platform) target；stage 3 的 target 另記受測 bundle 指紋。"""
    idx = _fe().evidence_index(root, stage=stage, contract="c" * 40, platforms=platforms)
    return SimpleNamespace(
        root=root,
        idx=idx,
        evidence_root=root / "evidence",
        cat=_ix().load_exit_catalog(),
        env=_env_file(root),
        cases=_cases(),
        lock=_cases_lock(),
        stage=stage,
        bundles={p: root / "bundles" / p for p in platforms},
    )


def _treed_world(root: Path, stage: str) -> SimpleNamespace:
    """建立帶 bundle 指紋的 target。stage 3 直接使用 fakes 的 bundle；其餘 stage 另建 bundle 並以 bundle_dir 設定。"""
    w = _world(root, stage=stage)
    if stage == "3":
        return w
    fk = _fe()
    w.bundles = {}
    for platform in ("linux", "windows"):
        bdir = root / f"bundles-{stage}" / platform
        fk.make_bundle(bdir, stage=stage)
        w.bundles[platform] = bdir
        _ix().set_target(
            w.idx,
            stage=stage,
            platform=platform,
            release_manifest=bdir / "release-manifest.json",
            contract_revision="c" * 40,
            tool_revision="t" * 40,
            case_list_sha256=_sha(CASES_LOCK),
            set_by="test",
            now=SET_AT,
            bundle_dir=bdir,
        )
    return w


def _target(w, platform: str, stage: str | None = None) -> dict:
    return _ix().load_index(w.idx)["stage_targets"][stage or w.stage][platform]


def _bundle(w, platform: str) -> Path:
    return w.bundles[platform]


def _state(w) -> tuple[bytes, tuple[str, ...]]:
    """index.json 位元組與 evidence_root 內檔案清單；拒收後必須完全不變。"""
    files: tuple[str, ...] = ()
    if w.evidence_root.exists():
        files = tuple(sorted(str(p.relative_to(w.evidence_root)) for p in w.evidence_root.rglob("*") if p.is_file()))
    return w.idx.read_bytes(), files


def _ingest(w, *, kind: str, item_id: str, platform: str, report_path: Path, release_manifest: Path | None = None,
            reuse_of: str | None = None, l1_lock: dict | None = None, now: str = NOW):
    return _ei().ingest(
        w.idx,
        kind=kind,
        item_id=item_id,
        platform=platform,
        report=report_path,
        evidence_root=w.evidence_root,
        catalog=w.cat,
        cases=w.cases,
        lock=w.lock,
        environment=w.env,
        release_manifest=release_manifest,
        reuse_of=reuse_of,
        now=now,
        l1_lock=l1_lock,
    )


def _assert_ingest_invalid(exc_info, detail: str | None = None) -> None:
    assert exc_info.value.code == "HARNESS_EVIDENCE_INGEST_INVALID", exc_info.value
    if detail is not None:
        assert detail in exc_info.value.detail, exc_info.value.detail


def _status(w, item_id: str, *, l1_lock: dict | None = None) -> tuple[str, list[str]]:
    return _ix().item_status(
        _ix().load_index(w.idx), w.cat[item_id], evidence_root=w.evidence_root, cases=w.cases, l1_lock=l1_lock
    )


def _stage(w, stage: str = "3", *, l1_lock: dict | None = None) -> dict:
    return _ix().stage_status(
        _ix().load_index(w.idx), stage, catalog=w.cat, evidence_root=w.evidence_root, cases=w.cases, l1_lock=l1_lock
    )


def _first_item(cat: dict, *, stage: str, kind: str, platform: str):
    for item in cat.values():
        if item.stage == stage and item.evidence_kind == kind and item.platform in (platform, "both"):
            return item
    raise AssertionError(f"no {stage} {kind} item for {platform}")


def _sideload(src: Path, dst: Path) -> Path:
    """複製一份安裝，並在 bin/ 加一個旁載檔（manifest 與 bin/serialwrap 位元組不變）。"""
    shutil.copytree(src, dst)
    (dst / "bin" / "version.dll").write_bytes(b"sideloaded-dll")
    return dst


def _bracket(cli: Path) -> dict:
    """受測部署內容（P2 Task 10 `measure`）：CLI 量測＋安裝內 daemon 映像（daemon_argv）＋既有 daemon 的 daemon_live。

    注意：`tests/fakes_evidence.deployed_bracket` 只以 `measure(cli)` 量 CLI，未帶 daemon_argv、也無 daemon_live；
    依 R5 規則 junit／functest（daemon_resolved）與 realhw／regression／gate／manual（daemon_live）會因此被判「未量測」。
    為使乾淨安裝的接受路徑反映規格，本檔統一以此函式量測（同一個 P2 measure，只多帶 daemon 參數）。
    """
    daemon = cli.parent / "serialwrapd"
    measured = _deployed_content().measure(cli, daemon_argv=[str(daemon)])
    live = {"pid": 4321, "image_path": str(daemon), "image_sha256": _sha(daemon), "in_install": True, "error": None}
    return {"start": measured, "end": copy.deepcopy(measured), "daemon_live": [live]}


# ---------------------------------------------------------------------------
# 報告替身（依 Task 6／12／13／15／P2 的報告形狀）
# ---------------------------------------------------------------------------
def _ab_report(w, *, platform: str = "linux", stage: str = "3", top: dict | None = None,
               b_side: dict | None = None, evidence_layer: str | None = None, verdict: str = "PASS") -> dict:
    t = _target(w, platform, stage)
    a_side = {"label": "A", "role": "baseline", "release_manifest_sha256": "a" * 64, "runtime_digest": "0" * 64,
              "bundle_tree_sha256": "1" * 64, "runtime_tree_sha256": "2" * 64}
    b = {"label": "B", "role": "candidate", "release_manifest_sha256": t["artifact_id"],
         "runtime_digest": t["runtime_digest"], "bundle_tree_sha256": t["bundle_tree_sha256"],
         "runtime_tree_sha256": t["runtime_tree_sha256"]}
    if evidence_layer is not None:
        b["evidence_layer"] = evidence_layer
    b.update(b_side or {})
    doc = {"ab_manifest_sha256": "e" * 64, "run_id": "run-14b", "layer": "l3", "platform": platform,
           "stage": stage, "contract_revision": t["contract_revision"], "case_list_sha256": t["case_list_sha256"],
           "tool_revision": t["tool_revision"], "verdict": verdict, "reason_code": None,
           "sides": [a_side, b], "rounds": [], "started_at": "2026-11-02T09:00:00+00:00", "finished_at": NOW}
    doc.update(top or {})
    return doc


def _canary_doc(w, *, platform: str = "linux", result: str = "PASS", reasons=(), open_incidents=(), failing=(),
                first: str = "2026-11-02T06:00:00+00:00", ledger_through: str = "2026-11-16T18:00:00+00:00",
                ledger_records: int = 5040, bundle: str | None = None, runtime: str | None = None,
                top: dict | None = None) -> dict:
    t = _target(w, platform)
    doc = {"platform": platform, "artifact_id": t["artifact_id"],
           "bundle_tree_sha256": bundle or t["bundle_tree_sha256"],
           "runtime_tree_sha256": runtime or t["runtime_tree_sha256"],
           "result": result, "reasons": list(reasons), "policy_sha256": "p" * 64,
           "ledger_through": ledger_through, "ledger_records": ledger_records,
           "tally": {"open_incidents": list(open_incidents), "failing_incidents": list(failing),
                     "first_counted_wall": first, "submits": 5040}}
    doc.update(top or {})
    return doc


def _gate_json(g) -> dict:
    """`canary report --out` 的 JSON 即 CanaryGate 的 dataclass 序列化。"""
    return json.loads(json.dumps(dataclasses.asdict(g), ensure_ascii=False, sort_keys=True))


def _policy(root: Path):
    return _cpol().load_policy(_fk().write_policy(root / "policy.json"))


def _drill_doc(w, platform: str, *, verdict: str = "PASS", bundle: str | None = None, runtime: str | None = None,
               started: str = "2026-11-02T08:00:00+00:00", top: dict | None = None) -> dict:
    t = _target(w, platform)
    doc = {"verdict": verdict, "reason_code": None, "started_at": started,
           "current_release_manifest_sha256": t["artifact_id"], "previous_release_manifest_sha256": "p" * 64,
           "current_bundle_tree_sha256": bundle or t["bundle_tree_sha256"],
           "current_runtime_tree_sha256": runtime or t["runtime_tree_sha256"],
           "steps": [], "limitations": []}
    doc.update(top or {})
    return doc


def _gate_doc(*, platform: str = "linux", mode: str = "candidate", release_gate_result: str = "PASS",
              engine_verdict: str = "PASS", cli_sha: str, deployed: Any, top: dict | None = None) -> dict:
    doc = {"schema": "release-gate.v1", "platform": platform, "mode": mode,
           "release_gate_result": release_gate_result,
           "baseline_result": None if mode == "candidate" else "PASS",
           "engine_result": {"verdict": engine_verdict, "mode": "lr-mixed"},
           "acceptance": {"revision": 2, "workload_contract_sha256": "w" * 64},
           "run": {"cli_identity": {"sha256": cli_sha, "resolved": "/x/serialwrap", "pinned": True},
                   "deployed_content": deployed},
           "checks": []}
    doc.update(top or {})
    return doc


def _suite_doc(w, item, platform: str, *, cli_sha: str, deployed: Any, drop: tuple[str, ...] = (),
               top: dict | None = None) -> dict:
    """TestPilot／realhw `report.json`：`meta.cli_identity`、`meta.deployed_content`、`results[{id,verdict}]`。"""
    results = [{"id": cid, "verdict": "PASS", "reason": "", "category": "fake", "reason_code": None,
                "duration_s": 1.0, "evidence": {}} for cid in _layer_ids(item, platform) if cid not in drop]
    doc = {"meta": {"cli_identity": {"sha256": cli_sha, "resolved": "/x/serialwrap", "pinned": True},
                    "deployed_content": deployed, "started_at_utc": "2026-11-02T08:00:00+00:00",
                    "platform": platform},
           "results": results}
    doc.update(top or {})
    return doc


def _manual_doc(root: Path, *, procedure_sha: str | None = None, deployed: Any = None, top: dict | None = None) -> dict:
    root.mkdir(parents=True, exist_ok=True)
    proc = root / "procedure.md"
    if not proc.exists():
        proc.write_text("# 人工程序（測試用）\n", encoding="utf-8")
    doc = {"procedure_path": str(proc), "procedure_sha256": procedure_sha or _sha(proc), "result": "PASS",
           "executed_by": "owner", "executed_at": NOW, "artifacts": [], "deployed_content": deployed}
    doc.update(top or {})
    return doc


def _functest_tree(w, item, platform: str, root_dir: Path, *, cli_sha: str, rel_sha: str, deployed: Any,
                   layer: str | None = None) -> Path:
    """func-test `--report-dir` 樹：每個適用案例一個 `<case>/<mode>/<n>/`，含 manifest／comparison／bridge-manifest。"""
    for cid in _layer_ids(item, platform):
        run = root_dir / cid / "local-process" / "0"
        manifest = {"schema": "l15-manifest.v1", "case_id": cid,
                    "artifacts": {"cli": {"sha256": cli_sha},
                                  "daemon": {"implementation": "rust", "sha256": "d" * 64, "tree_sha256": "d" * 64},
                                  "release_manifest_sha256": rel_sha, "deployed_content": deployed}}
        if layer is not None:
            manifest["evidence_layer"] = layer
        _write_json(run / "manifest.json", manifest)
        _write_json(run / "comparison.json", {"schema": "comparison.v1", "verdict": "PASS", "streams": [],
                                              "invariants": []})
        _write_json(run / "bridge-manifest.json", {"closed_cleanly": True, "failure": None})
    return root_dir


def _od09_doc() -> dict:
    return {"thresholds": {
                "resources": {"status": "frozen"},
                "min_effective_load": {"status": "frozen"},
                "comparison_uncertainty": {"status": "frozen"},
            },
            "revision": 2,
            "acceptance_sha256": _sha(ACCEPTANCE_YAML),
            "frozen_at": "2026-10-20T00:00:00+00:00",
            "baseline_evidence": [],
            "requires_baseline_gate_rerun": False}


def _retirement_doc(w) -> dict:
    t_linux, t_win = _target(w, "linux"), _target(w, "windows")
    return {"retired": True, "evidence_index": str(w.idx), "evidence_index_sha256": _sha(w.idx),
            "approved_by": "owner", "approved_at": NOW, "decision_ref": "BO-P9-13",
            "effective_bundle_version": "0.9.0",
            "stage3_target_artifact_ids": {"linux": t_linux["artifact_id"], "windows": t_win["artifact_id"]}}


def _l1_lock_for_stage3(w) -> dict:
    sets: dict[str, dict[str, list[str]]] = {}
    for item in w.cat.values():
        if item.stage == "3" and item.layer == "L1" and item.required_l1_set:
            per = sets.setdefault(item.required_l1_set, {})
            for platform in ("linux", "windows"):
                if item.platform in (platform, "both"):
                    per[platform] = list(NODES)
    return fakes_l1.lock(sets)


def _canary_scenario(root: Path):
    """Stage 3 canary：滿額 PASS 帳本、再附加一組未結案 pid 變更的快照（R3-F1 的前置情境）。"""
    w = _treed_world(root, "3")
    t = _target(w, "linux")
    pol = _policy(root)
    recs = _fc().synthetic_canary(days=14, submits_per_day=360, cycles=FULL_CYCLES, platform="linux",
                                  artifact=t["artifact_id"], policy_sha256=pol.sha256,
                                  bundle_tree=t["bundle_tree_sha256"], runtime_tree=t["runtime_tree_sha256"])
    trees = _fe().target_trees(w.idx, "linux")
    g = _ccl().gate(recs, pol, platform="linux", artifact_id=t["artifact_id"], **trees)
    pid_change = [
        _fk().snapshot("2026-11-16T06:00:00+00:00", pid=200, artifact=t["artifact_id"],
                       bundle_tree=t["bundle_tree_sha256"], runtime_tree=t["runtime_tree_sha256"]),
        _fk().snapshot("2026-11-16T06:00:30+00:00", pid=200, artifact=t["artifact_id"],
                       bundle_tree=t["bundle_tree_sha256"], runtime_tree=t["runtime_tree_sha256"]),
    ]
    recs2 = recs + pid_change
    g2 = _ccl().gate(recs2, pol, platform="linux", artifact_id=t["artifact_id"], **trees)
    return SimpleNamespace(w=w, pol=pol, recs=recs, recs2=recs2, g=g, g2=g2, trees=trees, root=root)


# ===========================================================================
# ab-report
# ===========================================================================
def test_ab_report_pass_ingested_and_copied(tmp_path):
    w = _world(tmp_path)
    rp = _write_json(tmp_path / "reports" / "ab.json", _ab_report(w))
    entry = _ingest(w, kind="ab-report", item_id="S3-L3-AB-48H-LINUX", platform="linux", report_path=rp)
    assert entry.result == "PASS"
    assert entry.producer == "ingest:ab-report"
    assert entry.files, "PASS 證據必須複製報告檔"
    for f in entry.files:
        copied = w.evidence_root / f.path
        assert copied.is_file()
        assert _sha(copied) == f.sha256
    status, _ = _status(w, "S3-L3-AB-48H-LINUX")
    assert status == "SATISFIED"


def test_ab_report_artifact_mismatch_rejected(tmp_path):
    w = _world(tmp_path)
    before = _state(w)
    rp = _write_json(tmp_path / "ab.json", _ab_report(w, b_side={"release_manifest_sha256": "9" * 64}))
    with pytest.raises(_harness_error()) as exc:
        _ingest(w, kind="ab-report", item_id="S3-L3-AB-48H-LINUX", platform="linux", report_path=rp)
    _assert_ingest_invalid(exc)
    assert _state(w) == before


def test_ab_report_tool_revision_mismatch_rejected(tmp_path):
    w = _world(tmp_path)
    before = _state(w)
    rp = _write_json(tmp_path / "ab.json", _ab_report(w, top={"tool_revision": "9" * 40}))
    with pytest.raises(_harness_error()) as exc:
        _ingest(w, kind="ab-report", item_id="S3-L3-AB-48H-LINUX", platform="linux", report_path=rp)
    _assert_ingest_invalid(exc)
    assert _state(w) == before


def test_ab_report_bundle_tree_mismatch_rejected(tmp_path):
    """R3-F2：manifest 未變、bundle 新增未宣告的旁載檔，B 側指紋與 target 不同 → 拒收。"""
    w = _world(tmp_path)
    before = _state(w)
    ab = _ab()
    b = _bundle(w, "linux")
    (b / "bin" / "version.dll").write_bytes(b"sideloaded-dll")
    tree = ab.bundle_tree_sha256(b)
    rtree = ab.runtime_tree_sha256(b)
    rp = _write_json(tmp_path / "ab.json", _ab_report(w, b_side={"bundle_tree_sha256": tree,
                                                                  "runtime_tree_sha256": rtree}))
    with pytest.raises(_harness_error()) as exc:
        _ingest(w, kind="ab-report", item_id="S3-L3-AB-48H-LINUX", platform="linux", report_path=rp)
    _assert_ingest_invalid(exc)
    assert "bundle_tree_sha256" in exc.value.detail and "runtime_tree_sha256" in exc.value.detail
    assert _state(w) == before

    # B 側缺 bundle_tree_sha256 鍵 → 同碼
    doc = _ab_report(w)
    del doc["sides"][1]["bundle_tree_sha256"]
    rp2 = _write_json(tmp_path / "ab-missing.json", doc)
    with pytest.raises(_harness_error()) as exc2:
        _ingest(w, kind="ab-report", item_id="S3-L3-AB-48H-LINUX", platform="linux", report_path=rp2)
    _assert_ingest_invalid(exc2)
    assert _state(w) == before


def test_ingest_entry_records_target_tree(tmp_path):
    """R3-F2／R4-F1：entry 的內容指紋取自產生端報告的量測值（恰等於 target），不由 target 回填。"""
    w = _world(tmp_path)
    t = _target(w, "linux")
    ab = _ab()

    # ab-report：B 側量測值即 target
    rp = _write_json(tmp_path / "ab.json", _ab_report(w))
    e_ab = _ingest(w, kind="ab-report", item_id="S3-L3-AB-48H-LINUX", platform="linux", report_path=rp)
    assert e_ab.bundle_tree_sha256 == t["bundle_tree_sha256"]
    assert e_ab.runtime_tree_sha256 == t["runtime_tree_sha256"]

    # drill-report：D1 對乾淨 bundle 量測的值
    b = _bundle(w, "linux")
    rd = _write_json(tmp_path / "drill.json", _drill_doc(
        w, "linux", bundle=ab.bundle_tree_sha256(b), runtime=ab.runtime_tree_sha256(b)))
    e_drill = _ingest(w, kind="drill-report", item_id="S3-ROLLBACK-LINUX", platform="linux", report_path=rd,
                      now=NOW2)
    assert e_drill.bundle_tree_sha256 == ab.bundle_tree_sha256(b) == t["bundle_tree_sha256"]
    assert e_drill.runtime_tree_sha256 == ab.runtime_tree_sha256(b) == t["runtime_tree_sha256"]

    # junit：resource-report.json 的 deployed_content 為乾淨 bundle 的量測；值來自報告、不是回填
    lock = fakes_l1.lock({L1_SET: {"linux": NODES}})
    rep = fakes_l1.full_pass(lock, L1_SET, "linux", deployed_content=_bracket(b / "bin" / "serialwrap"))
    jp = fakes_l1.write_report_dir(tmp_path / "junit-ok", rep)
    e_junit = _ingest(w, kind="junit", item_id=LINUX_L1, platform="linux", report_path=jp, l1_lock=lock, now=NOW3)
    assert e_junit.bundle_tree_sha256 == t["bundle_tree_sha256"]
    assert e_junit.runtime_tree_sha256 == t["runtime_tree_sha256"]

    # od09-freeze：TREE_EXEMPT_KINDS，取 target 同名值作索引綁定
    ro = _write_json(tmp_path / "od09.json", _od09_doc())
    e_od = _ingest(w, kind="od09-freeze", item_id="S3-OD09-ALL-FROZEN", platform="linux", report_path=ro, now=NOW4)
    assert e_od.bundle_tree_sha256 == t["bundle_tree_sha256"]
    assert e_od.runtime_tree_sha256 == t["runtime_tree_sha256"]


# ===========================================================================
# 旁載安裝與未量測（R4-F1／R5-F4）
# ===========================================================================
def test_canary_and_drill_sideloaded_install_rejected(tmp_path):
    """R4-F1：同 manifest、同 binary 但多旁載檔的工作站安裝，Canary 不計入、drill 報告拒收。"""
    w = _world(tmp_path)
    t = _target(w, "linux")
    ab = _ab()
    b = _bundle(w, "linux")
    ws = _sideload(b, tmp_path / "workstation")
    assert _sha(ws / "release-manifest.json") == t["artifact_id"]
    assert (ws / "bin" / "serialwrap").read_bytes() == (b / "bin" / "serialwrap").read_bytes()
    ws_b, ws_r = ab.bundle_tree_sha256(ws), ab.runtime_tree_sha256(ws)
    assert ws_b != t["bundle_tree_sha256"] and ws_r != t["runtime_tree_sha256"]

    # (1) Canary：以 target 指紋計數 → INCOMPLETE、不計配額；以工作站指紋計數 → PASS，但 ingest 拒收
    pol = _policy(tmp_path)
    recs = _fc().synthetic_canary(days=14, submits_per_day=360, cycles=FULL_CYCLES, platform="linux",
                                  artifact=t["artifact_id"], policy_sha256=pol.sha256,
                                  bundle_tree=ws_b, runtime_tree=ws_r)
    g_target = _ccl().gate(recs, pol, platform="linux", artifact_id=t["artifact_id"],
                           bundle_tree_sha256=t["bundle_tree_sha256"], runtime_tree_sha256=t["runtime_tree_sha256"])
    assert g_target.result == "INCOMPLETE"
    assert g_target.tally.submits == 0
    assert g_target.tally.tree_mismatch == 5040
    g_ws = _ccl().gate(recs, pol, platform="linux", artifact_id=t["artifact_id"],
                       bundle_tree_sha256=ws_b, runtime_tree_sha256=ws_r)
    assert g_ws.result == "PASS"
    before = _state(w)
    doc = _gate_json(g_ws)
    rp = _write_json(tmp_path / "canary-ws.json", doc)
    for item_id in ("S3-CANARY-LINUX", "S3-INCIDENTS-CLOSED"):
        with pytest.raises(_harness_error()) as exc:
            _ingest(w, kind="canary-report", item_id=item_id, platform="linux", report_path=rp)
        _assert_ingest_invalid(exc)
        assert "bundle_tree_sha256" in exc.value.detail and "runtime_tree_sha256" in exc.value.detail
    assert _state(w) == before

    # (2) Drill：報告的 artifact 與 target 相同、started_at 晚於 target（舊時刻規則會放行），但指紋為工作站值
    drill = _write_json(tmp_path / "drill-ws.json", _drill_doc(w, "linux", bundle=ws_b, runtime=ws_r))
    with pytest.raises(_harness_error()) as exc2:
        _ingest(w, kind="drill-report", item_id="S3-ROLLBACK-LINUX", platform="linux", report_path=drill)
    _assert_ingest_invalid(exc2)
    assert _state(w) == before

    # (3) 拒收後 S3-CANARY-LINUX／S3-ROLLBACK-LINUX 維持 MISSING，stage 3 BLOCKED
    st = _stage(w, "3")
    assert st["items"]["S3-CANARY-LINUX"]["status"] == "MISSING"
    assert st["items"]["S3-ROLLBACK-LINUX"]["status"] == "MISSING"
    assert st["result"] == "BLOCKED"

    # 乾淨發佈物不能借用旁載安裝的證據：retirement release 驗證仍因 stage 3 BLOCKED 被拒
    viol = _rt().verify(_retirement_doc(w), repo_root=REPO_ROOT, mode="release", evidence_root=w.evidence_root,
                        bundle_manifests={"linux": b / "release-manifest.json",
                                          "windows": _bundle(w, "windows") / "release-manifest.json"})
    assert any("stage 3 not PASS: BLOCKED" in v for v in viol), viol


def test_canary_and_drill_missing_tree_rejected(tmp_path):
    """R4-F1：缺指紋鍵、值為 null 或非 64hex 一律拒收，不得改走時刻規則回填 target。"""
    w = _world(tmp_path)
    before = _state(w)
    base = _canary_doc(w, first="2026-11-02T06:00:00+00:00")  # 晚於 target set_at
    for key in ("bundle_tree_sha256", "runtime_tree_sha256"):
        doc = copy.deepcopy(base)
        del doc[key]
        rp = _write_json(tmp_path / f"canary-no-{key}.json", doc)
        with pytest.raises(_harness_error()) as exc:
            _ingest(w, kind="canary-report", item_id="S3-CANARY-LINUX", platform="linux", report_path=rp)
        _assert_ingest_invalid(exc)
    for bad in (None, "not-a-digest"):
        doc = copy.deepcopy(base)
        doc["bundle_tree_sha256"] = bad
        rp = _write_json(tmp_path / f"canary-bad-{bad}.json", doc)
        with pytest.raises(_harness_error()) as exc:
            _ingest(w, kind="canary-report", item_id="S3-CANARY-LINUX", platform="linux", report_path=rp)
        _assert_ingest_invalid(exc)
    assert _state(w) == before

    for key in ("current_bundle_tree_sha256", "current_runtime_tree_sha256"):
        drill = _drill_doc(w, "linux")
        del drill[key]
        rp = _write_json(tmp_path / f"drill-no-{key}.json", drill)
        with pytest.raises(_harness_error()) as exc:
            _ingest(w, kind="drill-report", item_id="S3-ROLLBACK-LINUX", platform="linux", report_path=rp)
        _assert_ingest_invalid(exc)
    assert _state(w) == before


def test_junit_and_realhw_sideloaded_install_rejected(tmp_path):
    """R5-F4：junit／realhw 的執行安裝與 target 不同（旁載檔）即拒收；乾淨安裝則接受。"""
    w = _world(tmp_path)
    t = _target(w, "linux")
    b = _bundle(w, "linux")
    inst = _sideload(b, tmp_path / "inst")
    assert _sha(inst / "release-manifest.json") == t["artifact_id"]
    before = _state(w)
    rel = b / "release-manifest.json"

    # (1) junit
    lock = fakes_l1.lock({L1_SET: {"linux": NODES}})
    rep = fakes_l1.full_pass(lock, L1_SET, "linux", deployed_content=_bracket(inst / "bin" / "serialwrap"))
    jp = fakes_l1.write_report_dir(tmp_path / "junit-inst", rep)
    with pytest.raises(_harness_error()) as exc:
        _ingest(w, kind="junit", item_id=LINUX_L1, platform="linux", report_path=jp, l1_lock=lock)
    _assert_ingest_invalid(exc)
    assert "bundle_tree_sha256" in exc.value.detail and "runtime_tree_sha256" in exc.value.detail

    # (2) realhw-report
    item = w.cat["S3-L3-P0P1-REMOTE-LINUX"]
    doc = _suite_doc(w, item, "linux", cli_sha=_sha(inst / "bin" / "serialwrap"),
                     deployed=_bracket(inst / "bin" / "serialwrap"))
    rp = _write_json(tmp_path / "realhw-inst.json", doc)
    with pytest.raises(_harness_error()) as exc2:
        _ingest(w, kind="realhw-report", item_id=item.item_id, platform="linux", report_path=rp, release_manifest=rel)
    _assert_ingest_invalid(exc2)

    # (3) 拒收後狀態不變，兩項維持 MISSING
    assert _state(w) == before
    st = _stage(w, "3", l1_lock=lock)
    assert st["items"][LINUX_L1]["status"] == "MISSING"
    assert st["items"]["S3-L3-P0P1-REMOTE-LINUX"]["status"] == "MISSING"

    # (4) 對照組：乾淨安裝 → 接受，entry 兩個 tree 等於 target
    rep_ok = fakes_l1.full_pass(lock, L1_SET, "linux", deployed_content=_bracket(b / "bin" / "serialwrap"))
    jp_ok = fakes_l1.write_report_dir(tmp_path / "junit-clean", rep_ok)
    e1 = _ingest(w, kind="junit", item_id=LINUX_L1, platform="linux", report_path=jp_ok, l1_lock=lock, now=NOW2)
    assert e1.bundle_tree_sha256 == t["bundle_tree_sha256"]
    assert e1.runtime_tree_sha256 == t["runtime_tree_sha256"]
    doc_ok = _suite_doc(w, item, "linux", cli_sha=_sha(b / "bin" / "serialwrap"),
                        deployed=_bracket(b / "bin" / "serialwrap"))
    rp_ok = _write_json(tmp_path / "realhw-clean.json", doc_ok)
    e2 = _ingest(w, kind="realhw-report", item_id=item.item_id, platform="linux", report_path=rp_ok,
                 release_manifest=rel, now=NOW3)
    assert e2.bundle_tree_sha256 == t["bundle_tree_sha256"]
    assert e2.runtime_tree_sha256 == t["runtime_tree_sha256"]


def test_junit_release_cli_with_repo_daemon_rejected(tmp_path):
    """R5 審查後續：只量 CLI 安裝樹時，「release CLI＋repo Python daemon」也會得到等於 target 的指紋；須拒收。"""
    w = _world(tmp_path)
    b = _bundle(w, "linux")
    cli = b / "bin" / "serialwrap"
    lock = fakes_l1.lock({L1_SET: {"linux": NODES}})
    repo_daemon = REPO_ROOT / "tests" / "contract_blackbox" / "bin" / "py-serialwrapd"

    def _junit_with(daemon_resolved, daemon_sha, error) -> Path:
        dep = copy.deepcopy(_bracket(cli))
        for side in ("start", "end"):
            dep[side]["daemon_resolved"] = daemon_resolved
            dep[side]["daemon_sha256"] = daemon_sha
            dep[side]["error"] = error
        rep = fakes_l1.full_pass(lock, L1_SET, "linux", deployed_content=dep)
        return fakes_l1.write_report_dir(tmp_path / f"junit-{error}", rep)

    before = _state(w)
    jp = _junit_with(str(repo_daemon), "e" * 64, "daemon_outside_install")
    with pytest.raises(_harness_error()) as exc:
        _ingest(w, kind="junit", item_id=LINUX_L1, platform="linux", report_path=jp, l1_lock=lock)
    _assert_ingest_invalid(exc, "deployed daemon differs")
    assert _state(w) == before

    jp2 = _junit_with(None, None, "daemon_env_missing")
    with pytest.raises(_harness_error()) as exc2:
        _ingest(w, kind="junit", item_id=LINUX_L1, platform="linux", report_path=jp2, l1_lock=lock)
    _assert_ingest_invalid(exc2)
    assert _state(w) == before

    # 對照組：daemon 位於安裝內且 sha256 等於 launcher → 接受
    installed_daemon = b / "bin" / "serialwrapd"
    jp3 = _junit_with(str(installed_daemon), _sha(installed_daemon), None)
    entry = _ingest(w, kind="junit", item_id=LINUX_L1, platform="linux", report_path=jp3, l1_lock=lock, now=NOW2)
    assert entry.bundle_tree_sha256 == _target(w, "linux")["bundle_tree_sha256"]


def test_realhw_live_daemon_outside_install_rejected(tmp_path):
    """R5 審查後續：realhw 的 daemon_live 必須全部在安裝內且映像等於 launcher。"""
    w = _world(tmp_path)
    item = w.cat["S3-L3-P0P1-REMOTE-LINUX"]
    b = _bundle(w, "linux")
    cli = b / "bin" / "serialwrap"
    rel = b / "release-manifest.json"
    before = _state(w)

    def _doc(live) -> Path:
        dep = copy.deepcopy(_bracket(cli))
        dep["daemon_live"] = live
        return _write_json(tmp_path / f"realhw-live-{len(str(live))}.json",
                           _suite_doc(w, item, "linux", cli_sha=_sha(cli), deployed=dep))

    outside = [{"pid": 4321, "image_path": str(REPO_ROOT / "serialwrapd.py"), "image_sha256": "f" * 64,
                "in_install": False, "error": None}]
    with pytest.raises(_harness_error()) as exc:
        _ingest(w, kind="realhw-report", item_id=item.item_id, platform="linux", report_path=_doc(outside),
                release_manifest=rel)
    _assert_ingest_invalid(exc, "deployed daemon differs")
    assert _state(w) == before

    with pytest.raises(_harness_error()) as exc2:
        _ingest(w, kind="realhw-report", item_id=item.item_id, platform="linux", report_path=_doc([]),
                release_manifest=rel)
    _assert_ingest_invalid(exc2, "report lacks deployed tree: realhw-report")
    assert _state(w) == before

    inside = [{"pid": 4321, "image_path": str(b / "bin" / "serialwrapd"), "image_sha256": _sha(b / "bin" / "serialwrapd"),
               "in_install": True, "error": None}]
    entry = _ingest(w, kind="realhw-report", item_id=item.item_id, platform="linux", report_path=_doc(inside),
                    release_manifest=rel, now=NOW2)
    assert entry.result in ("PASS", "FAIL", "INSUFFICIENT")


def test_windows_stable_bin_sidecar_rejected(tmp_path):
    """B5（R5 審查後續）：Windows 穩定 bin 的 entry_dir 不一致即拒收。"""
    w = _world(tmp_path)
    b = _bundle(w, "windows")
    cli = b / "bin" / "serialwrap"
    lock = fakes_l1.lock({L1_SET: {"windows": NODES}})
    before = _state(w)

    dep = copy.deepcopy(_bracket(cli))
    dep["start"]["error"] = "entry_dir_extra: version.dll"
    rep = fakes_l1.full_pass(lock, L1_SET, "windows", deployed_content=dep)
    jp = fakes_l1.write_report_dir(tmp_path / "junit-entry-extra", rep)
    with pytest.raises(_harness_error()) as exc:
        _ingest(w, kind="junit", item_id=WINDOWS_L1, platform="windows", report_path=jp, l1_lock=lock)
    _assert_ingest_invalid(exc, "deployed entry dir differs")
    assert _state(w) == before

    dep2 = copy.deepcopy(_bracket(cli))
    dep2["start"]["entry_dir_sha256"] = "a" * 64
    dep2["end"]["entry_dir_sha256"] = "b" * 64
    rep2 = fakes_l1.full_pass(lock, L1_SET, "windows", deployed_content=dep2)
    jp2 = fakes_l1.write_report_dir(tmp_path / "junit-entry-changed", rep2)
    with pytest.raises(_harness_error()) as exc2:
        _ingest(w, kind="junit", item_id=WINDOWS_L1, platform="windows", report_path=jp2, l1_lock=lock)
    _assert_ingest_invalid(exc2, "deployed content changed during run")
    assert _state(w) == before


def test_unmeasured_report_rejected_under_tree_target(tmp_path):
    """R5-F4：target 記有 tree 時，未量測、中途改變、CLI／manifest 不符的報告一律拒收。"""
    w = _world(tmp_path)
    before = _state(w)
    b = _bundle(w, "linux")
    cli = b / "bin" / "serialwrap"
    rel = b / "release-manifest.json"
    lock = fakes_l1.lock({L1_SET: {"linux": NODES}})

    # (1) junit 缺 deployed_content 鍵
    rep = fakes_l1.full_pass(lock, L1_SET, "linux", deployed_content=_bracket(cli))
    del rep["deployed_content"]
    jp = fakes_l1.write_report_dir(tmp_path / "junit-no-key", rep)
    with pytest.raises(_harness_error()) as exc:
        _ingest(w, kind="junit", item_id=LINUX_L1, platform="linux", report_path=jp, l1_lock=lock)
    _assert_ingest_invalid(exc, "report lacks deployed_content: junit")

    # (2) junit 的 deployed_content 為 null（未設 SERIALWRAP_CLI_EXE）
    rep2 = fakes_l1.full_pass(lock, L1_SET, "linux", deployed_content=None)
    jp2 = fakes_l1.write_report_dir(tmp_path / "junit-null", rep2)
    with pytest.raises(_harness_error()) as exc2:
        _ingest(w, kind="junit", item_id=LINUX_L1, platform="linux", report_path=jp2, l1_lock=lock)
    _assert_ingest_invalid(exc2, "report lacks deployed tree: junit")
    assert _state(w) == before

    item = w.cat["S3-L3-P0P1-REMOTE-LINUX"]

    # (3) realhw start 未解析（digest 皆 None）
    dep = copy.deepcopy(_bracket(cli))
    dep["start"].update({"error": "unresolved", "bundle_tree_sha256": None, "release_manifest_sha256": None})
    rp = _write_json(tmp_path / "realhw-unresolved.json",
                     _suite_doc(w, item, "linux", cli_sha=_sha(cli), deployed=dep))
    with pytest.raises(_harness_error()) as exc3:
        _ingest(w, kind="realhw-report", item_id=item.item_id, platform="linux", report_path=rp, release_manifest=rel)
    _assert_ingest_invalid(exc3, "report lacks deployed tree: realhw-report")

    # (4) start 為乾淨 bundle、end 為執行中途加入旁載檔後的量測
    inst = _sideload(b, tmp_path / "midrun")
    dep4 = copy.deepcopy(_bracket(b / "bin" / "serialwrap"))
    dep4["end"] = copy.deepcopy(_bracket(inst / "bin" / "serialwrap")["end"])
    rp4 = _write_json(tmp_path / "realhw-midrun.json",
                      _suite_doc(w, item, "linux", cli_sha=_sha(cli), deployed=dep4))
    with pytest.raises(_harness_error()) as exc4:
        _ingest(w, kind="realhw-report", item_id=item.item_id, platform="linux", report_path=rp4, release_manifest=rel)
    _assert_ingest_invalid(exc4, "deployed content changed during run")

    # (5) 量測的 cli_sha256 ≠ meta.cli_identity.sha256
    rp5 = _write_json(tmp_path / "realhw-cli-mismatch.json",
                      _suite_doc(w, item, "linux", cli_sha="0" * 64, deployed=_bracket(cli)))
    with pytest.raises(_harness_error()) as exc5:
        _ingest(w, kind="realhw-report", item_id=item.item_id, platform="linux", report_path=rp5, release_manifest=rel)
    _assert_ingest_invalid(exc5, "deployed cli differs from report cli")

    # (6) 量測的 release_manifest_sha256 ≠ --release-manifest（報告的 artifact）
    dep6 = copy.deepcopy(_bracket(cli))
    for side in ("start", "end"):
        dep6[side]["release_manifest_sha256"] = "9" * 64
    rp6 = _write_json(tmp_path / "realhw-manifest-mismatch.json",
                      _suite_doc(w, item, "linux", cli_sha=_sha(cli), deployed=dep6))
    with pytest.raises(_harness_error()) as exc6:
        _ingest(w, kind="realhw-report", item_id=item.item_id, platform="linux", report_path=rp6, release_manifest=rel)
    _assert_ingest_invalid(exc6, "deployed manifest differs from report artifact")
    assert _state(w) == before


def test_all_artifact_kinds_require_measured_tree(tmp_path):
    """R5-F4：六種執行受測 artifact 的報告，旁載安裝一律拒收，乾淨 bundle 接受且 entry 指紋＝量測值。"""
    ei = _ei()
    assert ei.TREE_REQUIRED_KINDS == frozenset(ei.KINDS) - {"od09-freeze", "bridge-selftest"}
    src = INGEST_SRC.read_text(encoding="utf-8")
    assert "report predates target bundle tree" not in src

    params = [
        ("junit", LINUX_L1),
        ("realhw-report", "S3-L3-P0P1-REMOTE-LINUX"),
        ("functest-report", "S24-L15-ALL-CONTRACTS-LINUX"),
        ("regression-report", "S24-L2-ALL-38"),
        ("gate-report", "S24-L3-LRMIXED-48H-LINUX"),
        ("manual-procedure", "S24-P2-1-MCU-FLASH"),
    ]
    for kind, item_id in params:
        root = tmp_path / kind
        w = _treed_world(root, _catalog_stage(item_id))
        item = w.cat[item_id]
        platform = "linux"
        assert item.evidence_kind == kind
        b = _bundle(w, platform)
        clean_cli = b / "bin" / "serialwrap"
        inst = _sideload(b, root / "inst")

        # 旁載安裝 → 拒收，且狀態不變
        before = _state(w)
        rp, rm, l1 = _evidence_doc(kind, w, item, platform, inst / "bin" / "serialwrap", root / "side", b)
        with pytest.raises(_harness_error()) as exc:
            _ingest(w, kind=kind, item_id=item_id, platform=platform, report_path=rp, release_manifest=rm,
                    l1_lock=l1)
        _assert_ingest_invalid(exc)
        assert _state(w) == before, kind

        # 乾淨 bundle → 接受，指紋等於量測值（亦等於 target）
        rp2, rm2, l12 = _evidence_doc(kind, w, item, platform, clean_cli, root / "clean", b)
        entry = _ingest(w, kind=kind, item_id=item_id, platform=platform, report_path=rp2, release_manifest=rm2,
                        l1_lock=l12, now=NOW2)
        t = _target(w, platform, item.stage)
        assert entry.bundle_tree_sha256 == t["bundle_tree_sha256"], kind
        assert entry.runtime_tree_sha256 == t["runtime_tree_sha256"], kind


def _catalog_stage(item_id: str) -> str:
    return "3" if item_id.startswith("S3-") else "2.4"


def _evidence_doc(kind: str, w, item, platform: str, cli: Path, root: Path, bundle: Path):
    """依 kind 產生報告（量測 cli 所在安裝），回傳 (報告路徑, release-manifest 路徑或 None, l1_lock 或 None)。"""
    rel = bundle / "release-manifest.json"
    deployed = _bracket(cli)
    cli_sha = _sha(cli)
    if kind == "junit":
        lock = fakes_l1.lock({item.required_l1_set: {platform: NODES}})
        rep = fakes_l1.full_pass(lock, item.required_l1_set, platform, deployed_content=deployed)
        return fakes_l1.write_report_dir(root / "junit", rep), None, lock
    if kind == "realhw-report":
        doc = _suite_doc(w, item, platform, cli_sha=cli_sha, deployed=deployed)
        return _write_json(root / "realhw" / "report.json", doc), rel, None
    if kind == "regression-report":
        doc = _suite_doc(w, item, platform, cli_sha=cli_sha, deployed=deployed)
        return _write_json(root / "regression" / "report.json", doc), None, None
    if kind == "gate-report":
        doc = _gate_doc(platform=platform, cli_sha=cli_sha, deployed=deployed)
        return _write_json(root / "gate" / "release-gate.json", doc), rel, None
    if kind == "functest-report":
        d = _functest_tree(w, item, platform, root / "func", cli_sha=cli_sha, rel_sha=_sha(rel), deployed=deployed)
        return d, None, None
    if kind == "manual-procedure":
        return _write_json(root / "manual" / "record.json", _manual_doc(root / "manual", deployed=deployed)), None, None
    raise AssertionError(kind)


def test_untreed_target_records_measured_or_none(tmp_path):
    """R5-F4：未記 tree 的 target（Stage 1A 未傳 bundle_dir）：entry 記報告量測值，deployed_content 為 null 時為 None。"""
    w = _world(tmp_path, stage="1A")
    t = _target(w, "linux", "1A")
    assert "bundle_tree_sha256" not in t or t.get("bundle_tree_sha256") is None
    item = _first_item(w.cat, stage="1A", kind="junit", platform="linux")
    lock = fakes_l1.lock({item.required_l1_set: {"linux": NODES}})
    inst_root = tmp_path / "inst1a"
    fk = _fe()
    inst = fk.make_bundle(inst_root, stage="1A")
    dep = _bracket(inst / "bin" / "serialwrap")
    rep = fakes_l1.full_pass(lock, item.required_l1_set, "linux", deployed_content=dep)
    jp = fakes_l1.write_report_dir(tmp_path / "junit-1a", rep)
    entry = _ingest(w, kind="junit", item_id=item.item_id, platform="linux", report_path=jp, l1_lock=lock)
    assert entry.bundle_tree_sha256 is not None
    assert entry.bundle_tree_sha256 == dep["start"]["bundle_tree_sha256"]
    assert entry.runtime_tree_sha256 == dep["start"]["runtime_tree_sha256"]

    rep2 = fakes_l1.full_pass(lock, item.required_l1_set, "linux", deployed_content=None)
    jp2 = fakes_l1.write_report_dir(tmp_path / "junit-1a-null", rep2)
    entry2 = _ingest(w, kind="junit", item_id=item.item_id, platform="linux", report_path=jp2, l1_lock=lock, now=NOW2)
    assert entry2.bundle_tree_sha256 is None
    assert entry2.runtime_tree_sha256 is None


def test_gate_report_reuse_carries_measured_tree(tmp_path):
    """R5-F4／R3-F2：2.4 長跑的 gate 報告量測值供 Stage 3 以 reuse_of 沿用；指紋不同則 STALE。"""
    w = _treed_world(tmp_path / "w", "2.4")
    b = _bundle(w, "linux")
    cli = b / "bin" / "serialwrap"
    rel = b / "release-manifest.json"
    before = _state(w)
    inst = _sideload(b, tmp_path / "w" / "side")
    item24 = w.cat["S24-L3-LRMIXED-48H-LINUX"]
    assert item24.evidence_kind == "gate-report"

    # 旁載安裝量測 → 拒收（不會產生可供 reuse_of 的 entry）
    rp_side = _write_json(tmp_path / "gate-side.json", _gate_doc(
        cli_sha=_sha(inst / "bin" / "serialwrap"), deployed=_bracket(inst / "bin" / "serialwrap")))
    with pytest.raises(_harness_error()) as exc:
        _ingest(w, kind="gate-report", item_id=item24.item_id, platform="linux", report_path=rp_side,
                release_manifest=rel)
    _assert_ingest_invalid(exc)
    assert _state(w) == before

    rp = _write_json(tmp_path / "gate-clean.json", _gate_doc(
        cli_sha=_sha(cli), deployed=_bracket(cli)))
    gate_entry = _ingest(w, kind="gate-report", item_id=item24.item_id, platform="linux", report_path=rp,
                         release_manifest=rel)
    assert gate_entry.runtime_tree_sha256 == _deployed_content().tree_digests(b)[1]

    # Stage 3 target：runtime 檔與 b 相同 → 沿用成立
    fk = _fe()
    b3 = fk.make_bundle(tmp_path / "b3", stage="3")
    # make_bundle 只在 stage≠3 時建 py/ 委派樹；複製 b 的 py/，使 b3 的 runtime 檔確實與 b 相同（本案前提）
    shutil.copytree(b / "py", b3 / "py")
    _ix().set_target(w.idx, stage="3", platform="linux", release_manifest=b3 / "release-manifest.json",
                     contract_revision="c" * 40, tool_revision="t" * 40, case_list_sha256=_sha(CASES_LOCK),
                     set_by="test", now=SET_AT, bundle_dir=b3)
    # 沿用條件要求 environment_id 等其餘欄位全同；替身 entry 預設用 fakes 的 ENV_DOC，改為與被引用 entry 相同
    reuse = _fe().entry(w.root, "S3-L3-AB-48H-LINUX", "PASS", platform="linux", n=7,
                        reuse_of=gate_entry.entry_id, environment_id=gate_entry.environment_id)
    _ix().add_entry(w.idx, reuse, catalog=w.cat)
    status, _ = _status(w, "S3-L3-AB-48H-LINUX")
    assert status == "SATISFIED"

    # Stage 3 bundle 另加 bin/version.dll（新 artifact、新 target）→ 同一 reuse_of 必須 STALE
    b3x = fk.make_bundle(tmp_path / "b3x", stage="3")
    shutil.copytree(b / "py", b3x / "py")
    (b3x / "bin" / "version.dll").write_bytes(b"sideloaded-dll")
    # make_bundle 對同 stage 產出位元組相同的 manifest；改 created_at 使其成為新 artifact（同 manifest 換內容會被 set_target 拒絕）
    m3x = json.loads((b3x / "release-manifest.json").read_text(encoding="utf-8"))
    m3x["created_at"] = "2026-10-31T00:00:00+00:00"
    _write_json(b3x / "release-manifest.json", m3x)
    _ix().set_target(w.idx, stage="3", platform="linux", release_manifest=b3x / "release-manifest.json",
                     contract_revision="c" * 40, tool_revision="t" * 40, case_list_sha256=_sha(CASES_LOCK),
                     set_by="test", now=NOW, bundle_dir=b3x)
    reuse2 = _fe().entry(w.root, "S3-L3-AB-48H-LINUX", "PASS", platform="linux", n=8,
                         reuse_of=gate_entry.entry_id, environment_id=gate_entry.environment_id)
    _ix().add_entry(w.idx, reuse2, catalog=w.cat)
    status2, reasons2 = _status(w, "S3-L3-AB-48H-LINUX")
    assert status2 == "STALE"
    assert any("reuse identity differs: runtime_tree_sha256" in r for r in reasons2), reasons2


def test_producer_tree_algorithm_matches_task3(tmp_path):
    """R5-F4：三個獨立實作（realhw、Task 3、Task 9 collector）對同一目錄逐值相等。"""
    from tools.stage3 import canary_collect

    fk = _fe()
    base = fk.make_bundle(tmp_path / "d", stage="3")
    with_dll = _sideload(base, tmp_path / "d-dll")
    with_pyc = tmp_path / "d-pyc"
    shutil.copytree(base, with_pyc)
    (with_pyc / "py" / "__pycache__").mkdir(parents=True, exist_ok=True)
    (with_pyc / "py" / "__pycache__" / "m.cpython-312.pyc").write_bytes(b"\x00pyc")
    for d in (base, with_dll, with_pyc):
        ref = (_ab().bundle_tree_sha256(d), _ab().runtime_tree_sha256(d))
        assert _deployed_content().tree_digests(d) == ref
        assert tuple(canary_collect.deployed_tree_digests(d)) == ref


def test_kind_must_match_item(tmp_path):
    w = _world(tmp_path)
    before = _state(w)
    rp = _write_json(tmp_path / "ab.json", _ab_report(w))
    with pytest.raises(_harness_error()) as exc:
        _ingest(w, kind="ab-report", item_id="S3-ROLLBACK-LINUX", platform="linux", report_path=rp)
    _assert_ingest_invalid(exc)
    assert _state(w) == before


# ===========================================================================
# OD-15：證據層級
# ===========================================================================
@pytest.mark.parametrize("case", ["regression-s24-l2", "gate-s24-l3", "ab-s3-l3", "drill-s3-windows", "canary-s3-linux"])
def test_docker_and_throwaway_reports_rejected_for_real_board_items(tmp_path, case):
    if case in ("regression-s24-l2", "gate-s24-l3"):
        w = _treed_world(tmp_path, "2.4")
    else:
        w = _world(tmp_path)
    before = _state(w)
    if case == "regression-s24-l2":
        item = w.cat["S24-L2-ALL-38"]
        cli = _bundle(w, "linux") / "bin" / "serialwrap"
        doc = _suite_doc(w, item, "linux", cli_sha=_sha(cli), deployed=_bracket(cli),
                         top={"evidence_layer": "L1.5-docker"})
        kind, item_id, platform, rp_doc, rm = "regression-report", item.item_id, "linux", doc, None
    elif case == "gate-s24-l3":
        cli = _bundle(w, "linux") / "bin" / "serialwrap"
        doc = _gate_doc(cli_sha=_sha(cli), deployed=_bracket(cli),
                        top={"evidence_layer": "L1.5-docker"})
        kind, item_id, platform, rp_doc = "gate-report", "S24-L3-LRMIXED-48H-LINUX", "linux", doc
        rm = _bundle(w, "linux") / "release-manifest.json"
    elif case == "ab-s3-l3":
        doc = _ab_report(w, b_side={"evidence_layer": "L1.5-docker"})
        kind, item_id, platform, rp_doc, rm = "ab-report", "S3-L3-AB-48H-LINUX", "linux", doc, None
    elif case == "drill-s3-windows":
        doc = _drill_doc(w, "windows", top={"evidence_layer": "host-throwaway"})
        kind, item_id, platform, rp_doc, rm = "drill-report", "S3-ROLLBACK-WINDOWS", "windows", doc, None
    else:
        doc = _canary_doc(w, top={"evidence_layer": "L1.5-docker"})
        kind, item_id, platform, rp_doc, rm = "canary-report", "S3-CANARY-LINUX", "linux", doc, None
    status_before = _status(w, item_id)[0] if w.cat[item_id].platform in (platform, "both") else None
    value = rp_doc["evidence_layer"] if "evidence_layer" in rp_doc else rp_doc["sides"][1]["evidence_layer"]
    rp = _write_json(tmp_path / f"{case}.json", rp_doc)
    with pytest.raises(_harness_error()) as exc:
        _ingest(w, kind=kind, item_id=item_id, platform=platform, report_path=rp, release_manifest=rm)
    _assert_ingest_invalid(exc)
    assert exc.value.detail.startswith("evidence layer not real-board: ")
    assert value in exc.value.detail
    assert _state(w) == before
    if status_before is not None:
        assert _status(w, item_id)[0] == status_before


def test_docker_and_throwaway_reports_accepted_for_non_real_items(tmp_path):
    """OD-15：L1.5 項與 Stage 0 Spike 項可接受 docker／throwaway 證據，entry 保留 evidence_layer 值於 note。"""
    w = _treed_world(tmp_path / "a", "2.4")
    item = w.cat["S24-L15-ALL-CONTRACTS-LINUX"]
    b = _bundle(w, "linux")
    cli = b / "bin" / "serialwrap"
    tree = _functest_tree(w, item, "linux", tmp_path / "a" / "func", cli_sha=_sha(cli),
                          rel_sha=_sha(b / "release-manifest.json"), deployed=_bracket(cli),
                          layer="L1.5-docker")
    entry = _ingest(w, kind="functest-report", item_id=item.item_id, platform="linux", report_path=tree)
    assert entry.result == "PASS"

    w0 = _world(tmp_path / "b", stage="0")
    rp = _write_json(tmp_path / "b" / "manual.json", _manual_doc(tmp_path / "b",
                                                                  top={"evidence_layer": "host-throwaway"}))
    e0 = _ingest(w0, kind="manual-procedure", item_id="S0-SPIKE2-WINTASK-72H", platform="windows", report_path=rp)
    assert e0.result == "PASS"
    assert "host-throwaway" in e0.note


def test_unknown_evidence_layer_rejected(tmp_path):
    w = _treed_world(tmp_path, "3")
    item = w.cat["S3-L3-P0P1-REMOTE-LINUX"]
    b = _bundle(w, "linux")
    cli = b / "bin" / "serialwrap"
    rel = b / "release-manifest.json"
    before = _state(w)
    bad = _suite_doc(w, item, "linux", cli_sha=_sha(cli), deployed=_bracket(cli),
                     top={"evidence_layer": "L3-docker"})
    rp = _write_json(tmp_path / "realhw-bad-layer.json", bad)
    with pytest.raises(_harness_error()) as exc:
        _ingest(w, kind="realhw-report", item_id=item.item_id, platform="linux", report_path=rp, release_manifest=rel)
    _assert_ingest_invalid(exc)
    assert _state(w) == before

    ok = _suite_doc(w, item, "linux", cli_sha=_sha(cli), deployed=_bracket(cli))
    rp_ok = _write_json(tmp_path / "realhw-no-layer.json", ok)
    entry = _ingest(w, kind="realhw-report", item_id=item.item_id, platform="linux", report_path=rp_ok,
                    release_manifest=rel, now=NOW2)
    assert entry.result in ("PASS", "FAIL", "INSUFFICIENT")


# ===========================================================================
# canary／drill／gate 結果映射
# ===========================================================================
def test_canary_incomplete_is_insufficient(tmp_path):
    w = _world(tmp_path)
    doc = _canary_doc(w, result="INCOMPLETE", reasons=["open incidents: X"], open_incidents=["X"])
    rp = _write_json(tmp_path / "canary.json", doc)
    entry = _ingest(w, kind="canary-report", item_id="S3-CANARY-LINUX", platform="linux", report_path=rp)
    assert entry.result == "INSUFFICIENT"


def test_incidents_closed_from_canary_report(tmp_path):
    w = _world(tmp_path)
    rp = _write_json(tmp_path / "canary.json", _canary_doc(w, result="PASS"))
    entry = _ingest(w, kind="canary-report", item_id="S3-INCIDENTS-CLOSED", platform="linux", report_path=rp)
    assert entry.result == "PASS"


def test_canary_new_incident_after_pass_blocks_stage3(tmp_path):
    """R3-F1 整合：PASS 報告後出現未結案事故 → 新報告 INSUFFICIENT，舊 PASS 失效；結案後回到 PASS。"""
    sc = _canary_scenario(tmp_path)
    w, g, g2 = sc.w, sc.g, sc.g2
    fe = _fe()
    cl = _ccl()
    t = _target(w, "linux")
    assert g.result == "PASS", g.reasons
    judge = fakes_l1.l1_judge_module()
    l1 = _l1_lock_for_stage3(w)

    # 其餘 Stage 3 項目以 PASS 登錄（canary 的 linux 兩項除外）
    n = 0
    for item in w.cat.values():
        if item.stage != "3":
            continue
        for platform in ("linux", "windows"):
            if item.platform not in (platform, "both"):
                continue
            if platform == "linux" and item.item_id in ("S3-CANARY-LINUX", "S3-INCIDENTS-CLOSED"):
                continue
            n += 1
            over: dict[str, Any] = {}
            if item.evidence_kind == "junit":
                over["l1_set_sha256"] = judge.set_sha256(l1["sets"][item.required_l1_set][platform]["nodeids"])
            _ix().add_entry(w.idx, fe.entry(w.root, item.item_id, "PASS", platform=platform, n=n, **over), catalog=w.cat)

    # (1) 報告 A：PASS
    rpa = _write_json(sc.root / "canary-a.json", _gate_json(g))
    e_canary = _ingest(w, kind="canary-report", item_id="S3-CANARY-LINUX", platform="linux", report_path=rpa,
                       l1_lock=l1)
    e_inc = _ingest(w, kind="canary-report", item_id="S3-INCIDENTS-CLOSED", platform="linux", report_path=rpa,
                    l1_lock=l1, now=NOW2)
    assert e_canary.result == "PASS" and e_inc.result == "PASS"
    assert e_canary.ledger_records == g.ledger_records
    assert _stage(w, "3", l1_lock=l1)["result"] == "PASS"

    # (2) 報告 B：未結案 pid 變更 → INCOMPLETE，帳本版本較新
    assert g2.result == "INCOMPLETE" and g2.tally.open_incidents
    assert g2.ledger_through > g.ledger_through
    rpb = _write_json(sc.root / "canary-b.json", _gate_json(g2))
    e_b1 = _ingest(w, kind="canary-report", item_id="S3-CANARY-LINUX", platform="linux", report_path=rpb,
                   l1_lock=l1, now=NOW3)
    e_b2 = _ingest(w, kind="canary-report", item_id="S3-INCIDENTS-CLOSED", platform="linux", report_path=rpb,
                   l1_lock=l1, now=NOW4)
    assert e_b1.result == "INSUFFICIENT" and e_b2.result == "INSUFFICIENT"

    # (3) 目前報告為 B：兩項 MISSING，stage 3 BLOCKED，舊 PASS 不再計入
    status, reasons = _status(w, "S3-CANARY-LINUX", l1_lock=l1)
    assert status == "MISSING"
    assert any(f"superseded: latest canary report {e_b1.entry_id} is INSUFFICIENT" in r for r in reasons), reasons
    st = _stage(w, "3", l1_lock=l1)
    assert st["result"] == "BLOCKED"
    assert st["items"]["S3-CANARY-LINUX"]["satisfying_entries"] == []

    # (4) 以 environment 結案後重算 gate（PASS、帳本版本再增大）→ 報告 C 讓 stage 3 回到 PASS
    known = _ci().current_incidents(sc.recs2, [])
    open_id = sorted(g2.tally.open_incidents)[0]
    closed_rec = _ci().close(sc.root / "ledger", open_id, resolution="environment", issue_ref=None,
                             note="計畫內維護重開機", closed_by="owner", now=LATER, known=known)
    recs3 = sc.recs2 + [closed_rec]
    g3 = cl.gate(recs3, sc.pol, platform="linux", artifact_id=t["artifact_id"], **sc.trees)
    assert g3.result == "PASS", g3.reasons
    assert g3.ledger_through > g2.ledger_through
    rpc = _write_json(sc.root / "canary-c.json", _gate_json(g3))
    e_c = _ingest(w, kind="canary-report", item_id="S3-CANARY-LINUX", platform="linux", report_path=rpc,
                  l1_lock=l1, now=NOW5)
    _ingest(w, kind="canary-report", item_id="S3-INCIDENTS-CLOSED", platform="linux", report_path=rpc,
            l1_lock=l1, now="2026-11-02T15:00:00+00:00")
    assert e_c.result == "PASS"
    st3 = _stage(w, "3", l1_lock=l1)
    assert st3["result"] == "PASS", st3
    assert st3["items"]["S3-CANARY-LINUX"]["satisfying_entries"] == [e_c.entry_id]


def test_canary_older_report_reingest_rejected(tmp_path):
    """R3-F1：覆蓋後重新匯入舊報告（帳本版本較舊）→ 拒收，狀態不變。"""
    sc = _canary_scenario(tmp_path)
    w, g, g2 = sc.w, sc.g, sc.g2
    rpa = _write_json(sc.root / "canary-a.json", _gate_json(g))
    rpb = _write_json(sc.root / "canary-b.json", _gate_json(g2))
    e_b = _ingest(w, kind="canary-report", item_id="S3-CANARY-LINUX", platform="linux", report_path=rpb, now=NOW3)
    assert e_b.result == "INSUFFICIENT"
    before = _state(w)
    with pytest.raises(_harness_error()) as exc:
        _ingest(w, kind="canary-report", item_id="S3-CANARY-LINUX", platform="linux", report_path=rpa, now=NOW4)
    _assert_ingest_invalid(exc, f"canary report older than ingested {e_b.entry_id}")
    assert _state(w) == before
    assert _status(w, "S3-CANARY-LINUX")[0] == "MISSING"


def test_canary_report_without_ledger_version_rejected(tmp_path):
    w = _world(tmp_path)
    before = _state(w)
    doc = _canary_doc(w, result="PASS")
    del doc["ledger_records"]
    rp = _write_json(tmp_path / "canary-no-version.json", doc)
    with pytest.raises(_harness_error()) as exc:
        _ingest(w, kind="canary-report", item_id="S3-CANARY-LINUX", platform="linux", report_path=rp)
    _assert_ingest_invalid(exc)
    assert _state(w) == before


def test_drill_incomplete_is_insufficient(tmp_path):
    w = _world(tmp_path)
    rp = _write_json(tmp_path / "drill.json", _drill_doc(w, "linux", verdict="INCOMPLETE"))
    entry = _ingest(w, kind="drill-report", item_id="S3-ROLLBACK-LINUX", platform="linux", report_path=rp)
    assert entry.result == "INSUFFICIENT"


def test_gate_report_engine_fail_wins(tmp_path):
    w = _treed_world(tmp_path, "2.4")
    b = _bundle(w, "linux")
    cli = b / "bin" / "serialwrap"
    doc = _gate_doc(release_gate_result="PASS", engine_verdict="FAIL", cli_sha=_sha(cli),
                    deployed=_bracket(cli))
    rp = _write_json(tmp_path / "gate.json", doc)
    entry = _ingest(w, kind="gate-report", item_id="S24-L3-LRMIXED-48H-LINUX", platform="linux", report_path=rp,
                    release_manifest=b / "release-manifest.json")
    assert entry.result == "FAIL"


def test_gate_report_cli_identity_checked(tmp_path):
    w = _treed_world(tmp_path, "2.4")
    b = _bundle(w, "linux")
    cli = b / "bin" / "serialwrap"
    before = _state(w)
    doc = _gate_doc(cli_sha="0" * 64, deployed=_bracket(cli))
    rp = _write_json(tmp_path / "gate-cli.json", doc)
    with pytest.raises(_harness_error()) as exc:
        _ingest(w, kind="gate-report", item_id="S24-L3-LRMIXED-48H-LINUX", platform="linux", report_path=rp,
                release_manifest=b / "release-manifest.json")
    _assert_ingest_invalid(exc)
    assert _state(w) == before


def test_regression_report_missing_case_insufficient(tmp_path):
    w = _treed_world(tmp_path, "2.4")
    item = w.cat["S24-L2-ALL-38"]
    ids = _layer_ids(item, "linux")
    assert len(ids) >= 2
    b = _bundle(w, "linux")
    cli = b / "bin" / "serialwrap"
    doc = _suite_doc(w, item, "linux", cli_sha=_sha(cli), deployed=_bracket(cli), drop=(ids[0],))
    rp = _write_json(tmp_path / "regression-missing.json", doc)
    entry = _ingest(w, kind="regression-report", item_id=item.item_id, platform="linux", report_path=rp)
    assert entry.result == "INSUFFICIENT"


def test_junit_platform_skip_insufficient(tmp_path):
    w = _world(tmp_path)
    lock = fakes_l1.lock({L1_SET: {"linux": NODES}})
    rows = [fakes_l1.row(n) for n in NODES]
    rep = fakes_l1.report(rows, platform="linux", core_portable_platform_skips=["tests/x.py::skip_me"],
                          deployed_content=None)
    jp = fakes_l1.write_report_dir(tmp_path / "junit-cp-skip", rep)
    entry = _ingest(w, kind="junit", item_id=LINUX_L1, platform="linux", report_path=jp, l1_lock=lock)
    assert entry.result == "INSUFFICIENT"


# ===========================================================================
# junit／L1 完整性（R5-F3）
# ===========================================================================
def _l1_world(tmp_path: Path):
    w = _world(tmp_path)
    lock = fakes_l1.lock({L1_SET: {"linux": NODES}})
    return w, lock


def test_junit_single_pass_not_full_l1_exit(tmp_path):
    """R5-F3：只跑通一個案例（tests=1、無 failure）不得判 PASS；舊判準 tests>0 會給 PASS。"""
    w, lock = _l1_world(tmp_path)
    rep = fakes_l1.report([fakes_l1.row(NODES[0])], platform="linux", deployed_content=None)
    jp = fakes_l1.write_report_dir(tmp_path / "junit-single", rep)
    entry = _ingest(w, kind="junit", item_id=LINUX_L1, platform="linux", report_path=jp, l1_lock=lock)
    assert entry.result == "INSUFFICIENT"
    verdict_files = [f for f in entry.files if f.path.endswith("l1-verdict.json")]
    assert verdict_files, "l1-verdict.json 必須併入 evidence 檔"
    verdict = json.loads((w.evidence_root / verdict_files[0].path).read_text(encoding="utf-8"))
    assert verdict["missing"] == NODES[1:]
    assert _status(w, LINUX_L1, l1_lock=lock)[0] == "MISSING"
    assert _stage(w, "3", l1_lock=lock)["result"] == "BLOCKED"


@pytest.mark.parametrize("outcome", ["skipped", "xfailed"])
def test_junit_required_skip_not_pass(tmp_path, outcome):
    w, lock = _l1_world(tmp_path)
    rows = [fakes_l1.row(NODES[0]), fakes_l1.row(NODES[1]),
            fakes_l1.row(NODES[2], outcome, skip_kind="other")]
    rep = fakes_l1.report(rows, platform="linux", deployed_content=None)
    jp = fakes_l1.write_report_dir(tmp_path / f"junit-{outcome}", rep)
    entry = _ingest(w, kind="junit", item_id=LINUX_L1, platform="linux", report_path=jp, l1_lock=lock)
    assert entry.result == "INSUFFICIENT"
    verdict_files = [f for f in entry.files if f.path.endswith("l1-verdict.json")]
    verdict = json.loads((w.evidence_root / verdict_files[0].path).read_text(encoding="utf-8"))
    assert verdict["skipped_required"][0]["nodeid"] == NODES[2]


@pytest.mark.parametrize("case", ["duplicate", "exit-nonzero", "not-finished", "deselected", "junit-mismatch"])
def test_junit_duplicate_and_incomplete_runs_not_pass(tmp_path, case):
    w, lock = _l1_world(tmp_path)
    before = _state(w)
    if case == "duplicate":
        rows = [fakes_l1.row(NODES[0]), fakes_l1.row(NODES[1]), fakes_l1.row(NODES[1]), fakes_l1.row(NODES[2])]
        rep = fakes_l1.report(rows, platform="linux", deployed_content=None)
        jp = fakes_l1.write_report_dir(tmp_path / case, rep)
    elif case == "exit-nonzero":
        rep = fakes_l1.report([fakes_l1.row(n) for n in NODES], platform="linux", exit_code=2, deployed_content=None)
        jp = fakes_l1.write_report_dir(tmp_path / case, rep)
    elif case == "not-finished":
        rep = fakes_l1.report([fakes_l1.row(n) for n in NODES], platform="linux", finished=False, deployed_content=None)
        jp = fakes_l1.write_report_dir(tmp_path / case, rep)
    elif case == "deselected":
        rep = fakes_l1.report([fakes_l1.row(NODES[0]), fakes_l1.row(NODES[1])], platform="linux",
                              deselected=[NODES[2]], deployed_content=None)
        jp = fakes_l1.write_report_dir(tmp_path / case, rep)
    else:
        rep = fakes_l1.report([fakes_l1.row(n) for n in NODES], platform="linux", deployed_content=None)
        jp = fakes_l1.write_report_dir(tmp_path / case, rep, tests=4)
        with pytest.raises(_harness_error()) as exc:
            _ingest(w, kind="junit", item_id=LINUX_L1, platform="linux", report_path=jp, l1_lock=lock)
        _assert_ingest_invalid(exc, "junit/l1-results mismatch")
        assert _state(w) == before
        return

    entry = _ingest(w, kind="junit", item_id=LINUX_L1, platform="linux", report_path=jp, l1_lock=lock)
    assert entry.result == "INSUFFICIENT"
    verdict_files = [f for f in entry.files if f.path.endswith("l1-verdict.json")]
    verdict = json.loads((w.evidence_root / verdict_files[0].path).read_text(encoding="utf-8"))
    if case == "duplicate":
        assert verdict["duplicates"] == [NODES[1]]
    elif case == "exit-nonzero":
        assert any("runner exit 2" in r for r in verdict["reasons"])
    elif case == "not-finished":
        assert any("runner not finished" in r for r in verdict["reasons"])
    elif case == "deselected":
        assert verdict["deselected_required"] == [NODES[2]]


def test_junit_full_required_set_pass(tmp_path):
    """R5-F3：三個必要 nodeid 各一列 passed、另有非必要 skipped 列 → PASS；某必要列改 failed → FAIL。"""
    judge = fakes_l1.l1_judge_module()
    w, lock = _l1_world(tmp_path)
    rows = [fakes_l1.row(NODES[0]), fakes_l1.row(NODES[1]), fakes_l1.row(NODES[2]),
            fakes_l1.row("tests/contract_blackbox/test_a.py::t9", "skipped", skip_kind="other")]
    rep = fakes_l1.report(rows, platform="linux", deployed_content=None)
    jp = fakes_l1.write_report_dir(tmp_path / "junit-pass", rep)
    entry = _ingest(w, kind="junit", item_id=LINUX_L1, platform="linux", report_path=jp, l1_lock=lock)
    assert entry.result == "PASS"
    assert entry.l1_set_sha256 == judge.set_sha256(NODES)
    assert any(f.path.endswith("l1-verdict.json") for f in entry.files)

    rows_fail = [fakes_l1.row(NODES[0]), fakes_l1.row(NODES[1], "failed"), fakes_l1.row(NODES[2])]
    rep_fail = fakes_l1.report(rows_fail, platform="linux", deployed_content=None)
    jp_fail = fakes_l1.write_report_dir(tmp_path / "junit-fail", rep_fail)
    entry_fail = _ingest(w, kind="junit", item_id=LINUX_L1, platform="linux", report_path=jp_fail, l1_lock=lock,
                         now=NOW2)
    assert entry_fail.result == "FAIL"


def test_junit_l1_set_growth_makes_entry_stale(tmp_path):
    """R5-F3：必要集合 add-only 增加一筆後，舊 PASS 過期；集合不存在則 MISSING。"""
    w, lock = _l1_world(tmp_path)
    rep = fakes_l1.full_pass(lock, L1_SET, "linux", deployed_content=None)
    jp = fakes_l1.write_report_dir(tmp_path / "junit-growth", rep)
    entry = _ingest(w, kind="junit", item_id=LINUX_L1, platform="linux", report_path=jp, l1_lock=lock)
    assert entry.result == "PASS"
    assert _status(w, LINUX_L1, l1_lock=lock)[0] == "SATISFIED"

    grown = fakes_l1.lock({L1_SET: {"linux": NODES + ["tests/contract_blackbox/test_a.py::t4"]}})
    status, reasons = _status(w, LINUX_L1, l1_lock=grown)
    assert status == "STALE"
    assert any(f"l1 required set changed: {entry.entry_id}" in r for r in reasons), reasons

    status2, reasons2 = _status(w, LINUX_L1, l1_lock=fakes_l1.lock({}))
    assert status2 == "MISSING"
    assert any(f"no frozen l1 set: {L1_SET}" in r for r in reasons2), reasons2


def test_junit_item_without_l1_set_rejected(tmp_path):
    w, lock = _l1_world(tmp_path)
    item = w.cat[LINUX_L1]
    rep = fakes_l1.full_pass(lock, L1_SET, "linux", deployed_content=None)
    jp = fakes_l1.write_report_dir(tmp_path / "junit-no-set", rep)
    with pytest.raises(_harness_error()) as exc:
        _ei().derive("junit", jp, item=replace(item, required_l1_set=None), cases={}, lock={},
                     release_manifest=None, platform="linux", l1_lock=lock)
    _assert_ingest_invalid(exc)

    before = _state(w)
    bad_entry = _fe().entry(w.root, "S3-ROLLBACK-LINUX", "PASS", platform="linux", n=9, l1_set_sha256="a" * 64)
    with pytest.raises(_harness_error()) as exc2:
        _ix().add_entry(w.idx, bad_entry, catalog=w.cat)
    _assert_ingest_invalid(exc2)
    assert _state(w) == before


def test_junit_judge_matches_p2_vectors(tmp_path):
    """R5-F3：以 P2 `l1_judge_vectors.json` 為 golden 向量；唯一判定實作在 P2，ingest 只委派。"""
    vectors = json.loads((REPO_ROOT / "tests" / "acceptance" / "l1_judge_vectors.json").read_text(encoding="utf-8"))
    assert vectors, "P2 向量不得為空"
    w = _world(tmp_path)
    item = w.cat[LINUX_L1]
    for vec in vectors:
        rep = vec["report"]
        base = tmp_path / "vec" / vec["name"]
        junit = vec.get("junit")
        if junit is None:
            jp = fakes_l1.write_report_dir(base, rep)
        else:
            jp = fakes_l1.write_report_dir(base, rep, tests=junit.get("tests"), failures=junit.get("failures"),
                                           errors=junit.get("errors"), skipped=junit.get("skipped"))
        argv = list(rep.get("argv") or [])
        sel_argv = argv[1:] if rep.get("runner", "pytest") == "pytest" else argv
        selector = vec.get("selector") or {"argv": sel_argv, "markexpr": "", "paths": []}
        l1_lock = {"schema_version": 1,
                   "sets": {"vec": {vec["platform"]: {"selector": selector, "nodeids": list(vec["required"])}}}}
        if vec["verdict"] == "INVALID":
            with pytest.raises(_harness_error()) as exc:
                _ei().derive("junit", jp, item=item, cases={}, lock={}, release_manifest=None,
                             platform=vec["platform"], l1_lock=l1_lock)
            assert exc.value.code == "HARNESS_EVIDENCE_INGEST_INVALID", vec["name"]
        else:
            derived = _ei().derive("junit", jp, item=replace(item, required_l1_set="vec"), cases={}, lock={},
                                   release_manifest=None, platform=vec["platform"], l1_lock=l1_lock)
            assert derived.result == vec["verdict"], vec["name"]

    tree = ast.parse(INGEST_SRC.read_text(encoding="utf-8"))
    defs = {n.name for n in ast.walk(tree) if isinstance(n, ast.FunctionDef)}
    assert "judge" not in defs, "判定實作只能在 P2 l1_judge.py"
    for node in ast.walk(tree):
        if isinstance(node, ast.Compare):
            strs = {c.value for c in ast.walk(node) if isinstance(c, ast.Constant) and isinstance(c.value, str)}
            nums = {c.value for c in ast.walk(node) if isinstance(c, ast.Constant) and isinstance(c.value, int)}
            assert not ("tests" in strs and 0 in nums), ast.dump(node)


# ===========================================================================
# manual／target
# ===========================================================================
def test_manual_procedure_hash_checked(tmp_path):
    w = _treed_world(tmp_path, "2.4")
    before = _state(w)
    doc = _manual_doc(tmp_path / "manual", procedure_sha="0" * 64, deployed=None)
    rp = _write_json(tmp_path / "manual-bad.json", doc)
    with pytest.raises(_harness_error()) as exc:
        _ingest(w, kind="manual-procedure", item_id="S24-P2-1-MCU-FLASH", platform="linux", report_path=rp)
    _assert_ingest_invalid(exc)
    assert _state(w) == before


def test_no_target_rejected(tmp_path):
    w = _world(tmp_path, stage="3", platforms=("linux",))
    before = _state(w)
    rp = _write_json(tmp_path / "drill.json", _drill_doc(w, "linux"))
    with pytest.raises(_harness_error()) as exc:
        _ingest(w, kind="drill-report", item_id="S3-ROLLBACK-WINDOWS", platform="windows", report_path=rp)
    _assert_ingest_invalid(exc, "no target")
    assert _state(w) == before
