"""Slice 2.1 L1.5 bridge 回歸（P5 Task 21）：以 P2 runner 重跑 P2 凍結的 2.1 情境與 s21 情境，Python 基線與 Rust daemon 各跑一次。

- `test_case_list_matches_freeze`：P2 凍結（`cases.lock.json` 的 `slice_2.1`）的 l15 情境恰為 `P2_21`，且 s21 情境恰 10 個（純讀檔）。
- `test_case`：每個情境 × {python, rust} 以 `func-test/runner.py --mode bridge --scenarios --case <id>` 執行，
  `comparison.json` 的 verdict 須為 PASS。缺 daemon artifact 或 bridge 自測報告時 skip（EVIDENCE_MISSING）；
  `SERIALWRAP_GATE=1` 下缺證據即失敗，不得 skip。
"""
import json, os, pathlib, subprocess, sys
import pytest

ROOT = pathlib.Path(__file__).resolve().parents[3]
P2_21 = ["l15-half-prompt", "l15-slow-reader-backpressure", "l15-transport-disconnect",
         "l15-multi-source-contention-cli", "l15-stale-event-delay", "l15-wal-interference", "l15-reject-zero-tx",
         "l15-exclusive-open", "l15-reboot-min-guard", "l15-release-inflight"]
S21 = sorted(p.stem for p in (ROOT / "func-test/scenarios").glob("s21-*.yaml"))
# OD-34（2026-10-28）：依賴 readiness reprobe（P7-T5）的情境逐名延至 Slice 2.3 出口（情境檔保留，2.3 須 PASS）。
#   l15-delayed-reply 已自 cases.lock.json 的 slice_2.1 延後（凍結修訂 #1），故不在 P2_21。
DEFERRED_TO_S23 = {"l15-delayed-reply": "OD-34", "s21-reboot-minimal-guard": "OD-34"}
# OD-40（2026-10-28）：l15-wal-interference 自 l15_ab 判定逐名移出（情境檔與 2.1 必要案例保留），改由
# tests/acceptance/slice21_gate_expect.yaml 的 `l15_ab_excluded` 所列 pytest WAL 測試承擔。
# OD-46（2026-10-29）：s21-wal-unlink-under-traffic 比照 OD-40 逐名移出，改由持續流量 WAL unlink 黑箱 A/B 承擔。
# OD-58（2026-10-29）：s21-event-latency 逐名移出（OD-45 下兩個 daemon 皆無計時欄位而恆 INSUFFICIENT），改由
# event_latency section（L1 結構 gate＋scripts/rust/event_latency_measure.py 外部量測）承擔；情境檔保留。
L15_AB_EXCLUDED = {"l15-wal-interference": "OD-40", "s21-wal-unlink-under-traffic": "OD-46", "s21-event-latency": "OD-58"}
L15_AB_CASES = [c for c in P2_21 + S21 if c not in DEFERRED_TO_S23 and c not in L15_AB_EXCLUDED]
GATE = os.environ.get("SERIALWRAP_GATE") == "1"
RECORD_ONLY: set[str] = set()   # s21-exclusive-conflict-plain-open 已依規格 §6.2 定案為拒絕（§0.2），改為正式判定


# 本檔混合純讀檔與 PTY daemon 案例，依 T14 規則平台／資源 marker 皆放函式層（不放模組層）。
@pytest.mark.core_portable
@pytest.mark.memory_only
def test_case_list_matches_freeze():
    lock = json.loads((ROOT / "tests/acceptance/cases.lock.json").read_text(encoding="utf-8"))
    cases = __import__("yaml").safe_load((ROOT / "tests/acceptance/cases.yaml").read_text(encoding="utf-8"))
    l15 = {c["case_id"] for c in cases["cases"] if c.get("suite") == "l15-scenario" and c["case_id"] in lock["sets"]["slice_2.1"]}
    assert l15 == set(P2_21)
    assert len(S21) == 10
    # 延後與移出皆逐名且情境檔仍在（不得以刪檔縮小範圍）。
    for case in list(DEFERRED_TO_S23) + list(L15_AB_EXCLUDED):
        assert (ROOT / "func-test/scenarios" / f"{case}.yaml").is_file(), case
    assert "l15-delayed-reply" not in lock["sets"]["slice_2.1"] and "l15-delayed-reply" in lock["sets"]["slice_2.3"]
    assert "l15-wal-interference" in lock["sets"]["slice_2.1"], "OD-40 只移出 l15_ab 判定，不移出 2.1 分母"
    expect = __import__("yaml").safe_load((ROOT / "tests/acceptance/slice21_gate_expect.yaml").read_text(encoding="utf-8"))
    excluded = {row["case_id"]: row for row in expect.get("l15_ab_excluded") or []}
    assert set(excluded) == set(L15_AB_EXCLUDED)
    for case, od in L15_AB_EXCLUDED.items():
        assert excluded[case]["decision"] == od and excluded[case]["covered_by"], case
    # OD-58：event_latency 移出者須以 event_latency section 承擔（covered_by_sections），不得只列 L1 節點
    assert excluded["s21-event-latency"].get("covered_by_sections") == ["event_latency"]


@pytest.mark.posix_only
@pytest.mark.posix_pty
@pytest.mark.process_rpc
@pytest.mark.cap_posix_pty
@pytest.mark.parametrize("case", L15_AB_CASES)
@pytest.mark.parametrize("label,daemon_env", [("python", "SERIALWRAP_L15_PY_DAEMON"), ("rust", "SERIALWRAP_L15_RUST_DAEMON")])
def test_case(case, label, daemon_env, tmp_path):
    exe, report = os.environ.get(daemon_env), os.environ.get("SERIALWRAP_L15_SELFTEST_REPORT")
    if not exe or not report:
        if GATE:
            pytest.fail(f"{daemon_env}/SERIALWRAP_L15_SELFTEST_REPORT not set under SERIALWRAP_GATE=1")   # gate 下缺證據即失敗，不得 skip
        pytest.skip("EVIDENCE_MISSING: artifact or bridge selftest report not provided")
    env = dict(os.environ, SERIALWRAP_DAEMON_EXE=exe, SERIALWRAP_AB_LABEL=f"{label}-s21")
    out = tmp_path / label / case
    r = subprocess.run([sys.executable, "func-test/runner.py", "--mode", "bridge", "--scenarios", "--case", case,
                        "--selftest-report", report, "--report-dir", str(out)], cwd=ROOT, env=env, capture_output=True, text=True, timeout=900)
    found = next(out.rglob("comparison.json"), None)
    # runner 未產生 comparison.json（例如情境 compare: none，或 runner 提早失敗）即證據不足，不得視為 PASS
    assert found is not None, f"INSUFFICIENT_EVIDENCE: {case}/{label} 無 comparison.json（rc={r.returncode}）\n{r.stdout[-4000:]}\n{r.stderr[-4000:]}"
    verdict = json.loads(found.read_text(encoding="utf-8"))
    if case in RECORD_ONLY:
        assert verdict["verdict"] in {"PASS", "FAIL"}, verdict        # 只要求證據完整；結果寫入報告供裁決
        return
    assert r.returncode == 0 and verdict["verdict"] == "PASS", verdict
