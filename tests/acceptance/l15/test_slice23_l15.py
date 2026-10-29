"""Slice 2.3 L1.5 gate 案例（P7；OD-34，2026-10-28 擁有者裁決）：依賴 P7 readiness reprobe／#162 再確認的三案收進
Slice 2.3 gate，須在 Rust daemon 下 PASS。

- `l15-delayed-reply`（P2 凍結情境）、`s21-reboot-minimal-guard`（P5 s21 情境）：`func-test/runner.py --mode bridge
  --scenarios --case <id>`。
- `cowork-01-agent-bad-cmd`（Slice 2.2 func-test 案例）：`--mode bridge --case <id>`；另依賴 P6 human console
  （`session.console_attach`），P6 合入前 Rust daemon 回 METHOD_NOT_FOUND（BLOCKED-UPSTREAM，見
  `docs/rust-refactor/reviews/p7-local-windows-todo.md` P7-BU-7）。
- `l15-dut-reboot-port-alive`、`l15-dut-silent`（P2 凍結情境）與 `brcm-01-login-flow`（func-test 案例）：cases.yaml
  `daemon_slice: 2.3` 的必要 L1.5 列（第 11 輪補列）。
- `ev-01-spawn-handler`（daemon_slice 2.3）：Linux 列於本檔驗證；Windows 列（OD-31，SC-26 `cmd.exe /d /s /c` shell
  handler）須原生 PASS，列 LOCAL-WINDOWS-TODO P7-LW-7。

- `s23-*`（08 Task 18 的 bridge 模式 wire 證據六案，`func-test/scenarios/s23-*.yaml`）：Python 基線與 Rust daemon
  各跑一次（`test_s23_scenario`）。`s23-interleave-login-agent-human`、`s23-reboot-recovery-no-replay` 依賴 P6 human
  console，Rust 列在 P6 合入前 FAIL（BLOCKED-UPSTREAM，不得轉 SKIP）。
  偏離計畫原文：計畫把情境檔放在 `tests/acceptance/l15/slice23/`；P2 runner 的 `--scenarios` 只探索
  `func-test/scenarios/`（runner 屬測試工具，改動須獨立 PR），故比照 06 T21 的 `s21-*` 放在 `func-test/scenarios/`。

缺 daemon artifact 或 bridge 自測報告時 skip（EVIDENCE_MISSING）；`SERIALWRAP_GATE=1` 下缺證據即失敗，不得 skip。
`comparison.json` 缺漏或 verdict 非 PASS 一律 FAIL（不得轉 SKIP／PASS）。
"""
import json, os, pathlib, subprocess, sys
import pytest

ROOT = pathlib.Path(__file__).resolve().parents[3]
SCENARIOS = ["l15-delayed-reply", "s21-reboot-minimal-guard", "l15-dut-reboot-port-alive", "l15-dut-silent"]
CASES = ["cowork-01-agent-bad-cmd", "ev-01-spawn-handler", "brcm-01-login-flow"]
S23 = sorted(p.stem for p in (ROOT / "func-test/scenarios").glob("s23-*.yaml"))
GATE = os.environ.get("SERIALWRAP_GATE") == "1"


def _run(case: str, scenarios: bool, tmp_path: pathlib.Path, label: str = "rust",
         daemon_env: str = "SERIALWRAP_L15_RUST_DAEMON") -> None:
    exe, report = os.environ.get(daemon_env), os.environ.get("SERIALWRAP_L15_SELFTEST_REPORT")
    if not exe or not report:
        if GATE:
            pytest.fail(f"{daemon_env}/SERIALWRAP_L15_SELFTEST_REPORT not set under SERIALWRAP_GATE=1")
        pytest.skip("EVIDENCE_MISSING: artifact or bridge selftest report not provided")
    env = dict(os.environ, SERIALWRAP_DAEMON_EXE=exe, SERIALWRAP_AB_LABEL=f"{label}-s23")
    out = tmp_path / label / case
    argv = [sys.executable, "func-test/runner.py", "--mode", "bridge", "--case", case,
            "--selftest-report", report, "--report-dir", str(out)]
    if scenarios:
        argv.insert(4, "--scenarios")
    r = subprocess.run(argv, cwd=ROOT, env=env, capture_output=True, text=True, timeout=900)
    found = next(out.rglob("comparison.json"), None)
    assert found is not None, f"INSUFFICIENT_EVIDENCE: {case}/{label} 無 comparison.json（rc={r.returncode}）\n{r.stdout[-4000:]}\n{r.stderr[-4000:]}"
    verdict = json.loads(found.read_text(encoding="utf-8"))
    assert r.returncode == 0 and verdict["verdict"] == "PASS", verdict


@pytest.mark.posix_only
@pytest.mark.posix_pty
@pytest.mark.process_rpc
@pytest.mark.cap_posix_pty
@pytest.mark.parametrize("case", SCENARIOS)
def test_scenario_rust(case, tmp_path):
    _run(case, True, tmp_path)


@pytest.mark.posix_only
@pytest.mark.posix_pty
@pytest.mark.process_rpc
@pytest.mark.cap_posix_pty
@pytest.mark.parametrize("case", CASES)
def test_case_rust(case, tmp_path):
    _run(case, False, tmp_path)


@pytest.mark.core_portable
@pytest.mark.memory_only
def test_s23_case_list():
    """08 Task 18 的六個 bridge 情境皆存在（不得以刪檔縮小範圍）。"""
    assert S23 == sorted([
        "s23-interleave-login-agent-human", "s23-login-no-resend", "s23-login-wire-equals-wal",
        "s23-quiet-mid-login-zero-tx", "s23-reboot-recovery-no-replay", "s23-recover-force-handoff",
    ])


@pytest.mark.posix_only
@pytest.mark.posix_pty
@pytest.mark.process_rpc
@pytest.mark.cap_posix_pty
@pytest.mark.parametrize("case", S23)
@pytest.mark.parametrize("label,daemon_env", [("python", "SERIALWRAP_L15_PY_DAEMON"), ("rust", "SERIALWRAP_L15_RUST_DAEMON")])
def test_s23_scenario(case, label, daemon_env, tmp_path):
    _run(case, True, tmp_path, label=label, daemon_env=daemon_env)


def _s23(name: str) -> dict:
    return __import__("yaml").safe_load((ROOT / "func-test/scenarios" / f"{name}.yaml").read_text(encoding="utf-8"))


def _actions(doc: dict) -> list[dict]:
    return list(doc.get("steps") or [])


@pytest.mark.core_portable
@pytest.mark.memory_only
def test_s23_oracle_windows_cover_the_violation_points():
    """審查 P7-f9e90a9-3／-4／-5：s23 情境的判定窗口與 oracle 必須涵蓋各自的違規點（結構回歸）。"""
    # -3：quiet 零 TX 窗口須在測試送出 banner 之前開始，DUT 不得在登入步驟自行吐 banner
    q = _s23("s23-quiet-mid-login-zero-tx")
    steps = _actions(q)
    begin = next(i for i, s in enumerate(steps) if s.get("action") == "op_begin" and s.get("op_id") == "quiet")
    import base64
    banner = next(i for i, s in enumerate(steps) if s.get("action") == "dut_control"
                  and b"Hit any key" in base64.b64decode(str((s.get("params") or {}).get("payload_b64") or "")))
    assert begin < banner, "零 TX 窗口須早於 banner"
    assert all("Hit any key" not in str(st.get("respond")) for st in q["target"]["login_steps"])
    # -4：interleave 須核對帳號、secret 到 READY nonce probe 的完整登入區間，恰兩次
    il = _s23("s23-interleave-login-agent-human")
    payloads = [p for s in _actions(il) if s.get("action") == "assert_wire_contiguous" for p in s["payloads"]]
    assert payloads.count("root\ns3cretP7\necho __READY__") == 2, payloads
    # -5：force handoff 須證明實際重開一次（bridge_generation 1 → 2 跨過 force 窗口）
    fh = _actions(_s23("s23-recover-force-handoff"))
    gens = [(i, s.get("equals")) for i, s in enumerate(fh) if s.get("action") == "wait_cli_until"
            and str(s.get("json_path", "")).endswith(".bridge_generation")]
    fb = next(i for i, s in enumerate(fh) if s.get("action") == "op_begin" and s.get("op_id") == "force")
    fe = next(i for i, s in enumerate(fh) if s.get("action") == "op_end" and s.get("op_id") == "force")
    assert any(i < fb and v == 1 for i, v in gens) and any(i > fe and v == 2 for i, v in gens), gens
