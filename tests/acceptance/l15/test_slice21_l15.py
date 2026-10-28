"""Slice 2.1 L1.5 bridge 回歸（P5 Task 21）：以 P2 runner 重跑 P2 凍結的 2.1 情境與 s21 情境，Python 基線與 Rust daemon 各跑一次。

- `test_case_list_matches_freeze`：P2 凍結（`cases.lock.json` 的 `slice_2.1`）的 l15 情境恰為 `P2_21`，且 s21 情境恰 10 個（純讀檔）。
- `test_case`：每個情境 × {python, rust} 以 `func-test/runner.py --mode bridge --scenarios --case <id>` 執行，
  `comparison.json` 的 verdict 須為 PASS。缺 daemon artifact 或 bridge 自測報告時 skip（EVIDENCE_MISSING）；
  `SERIALWRAP_GATE=1` 下缺證據即失敗，不得 skip。
"""
import json, os, pathlib, subprocess, sys
import pytest

ROOT = pathlib.Path(__file__).resolve().parents[3]
P2_21 = ["l15-delayed-reply", "l15-half-prompt", "l15-slow-reader-backpressure", "l15-transport-disconnect",
         "l15-multi-source-contention-cli", "l15-stale-event-delay", "l15-wal-interference", "l15-reject-zero-tx",
         "l15-exclusive-open", "l15-reboot-min-guard", "l15-release-inflight"]
S21 = sorted(p.stem for p in (ROOT / "func-test/scenarios").glob("s21-*.yaml"))
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


@pytest.mark.posix_only
@pytest.mark.posix_pty
@pytest.mark.process_rpc
@pytest.mark.cap_posix_pty
@pytest.mark.parametrize("case", P2_21 + S21)
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
