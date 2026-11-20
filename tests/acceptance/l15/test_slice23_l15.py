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
# SPEC-GAP #7（rulings/p7.md 2026-10-29「以 WAL 先後判定」）：P2 runner 的字面 payload 比對無法表達隨機 nonce 之後的
# 界線，不改 runner；對下列情境另以 runner 保存的 WAL（`<report>/…/wal/raw.wal.ndjson*`）判定 nonce probe 與 READY
# 之間無 daemon 自身（`system`／`system:*`）以外來源的 TX，且至少有這麼多個已收的 probe 窗口（初次登入＋recover 重新登入）。
WAL_PROBE_WINDOWS = {"s23-interleave-login-agent-human": 2}


def s23_probe_ready_windows(rows: list[dict], prompt_regex: str, com: str = "COM0") -> dict:
    """以 WAL 紀錄的 seq 先後切出每個 ready probe 窗口並檢查來源（純函式）。

    窗口起點＝`system` 來源、payload 含 `echo __READY__<nonce>` 的 TX；終點＝READY 證據，即其後累積的 RX 先出現
    `__READY__<nonce>`、且在該 nonce 之後再比對到 `prompt_regex`（Python `login_fsm` 先等 nonce、再等 prompt 才回
    READY）。窗口內同 COM 任何非 daemon 自身來源的 TX（`system` 與 `system:*` 如救援鍵 `system:recover` 以外，即
    agent／human 等）記為問題；窗口到 WAL 結尾仍未收也記為問題。
    回傳 `{"windows": 已收窗口數, "problems": [...]}`。"""
    import base64
    import re

    recs = sorted((r for r in rows if r.get("com") == com), key=lambda r: int(r.get("seq") or 0))
    prompt = re.compile(prompt_regex)
    windows, problems = 0, []
    i = 0
    while i < len(recs):
        r = recs[i]
        payload = base64.b64decode(r.get("payload_b64") or "")
        m = re.search(rb"echo __READY__([0-9A-Za-z]+)", payload) if r.get("dir") == "TX" else None
        if m is None or r.get("source") != "system":
            i += 1
            continue
        token = b"__READY__" + m.group(1)
        acc = b""
        closed_at = None
        for j in range(i + 1, len(recs)):
            x = recs[j]
            src = str(x.get("source") or "")
            if x.get("dir") == "TX" and not (src == "system" or src.startswith("system:")):
                problems.append(f"probe seq={r.get('seq')} 與 READY 之間出現 {x.get('source')} 的 TX seq={x.get('seq')}")
            elif x.get("dir") == "RX":
                acc += base64.b64decode(x.get("payload_b64") or "")
                at = acc.find(token)
                if at >= 0 and prompt.search(acc[at + len(token):].decode("latin-1")):
                    closed_at = j
                    break
        if closed_at is None:
            problems.append(f"probe seq={r.get('seq')}（{token.decode('latin-1')}）之後未見 READY 證據（nonce＋prompt）")
            break
        windows += 1
        i = closed_at + 1
    return {"windows": windows, "problems": problems}


def _assert_wal_probe_windows(case: str, out: pathlib.Path) -> None:
    import yaml

    wal_dirs = sorted({p.parent for p in out.rglob("raw.wal.ndjson*")})
    assert len(wal_dirs) == 1, f"INSUFFICIENT_EVIDENCE: {case} 的 WAL 目錄應恰一個：{wal_dirs}"
    rows: list[dict] = []
    for path in sorted(wal_dirs[0].glob("raw.wal.ndjson*")):
        for line in path.read_text(encoding="utf-8").splitlines():
            if line.strip():
                rows.append(json.loads(line.rstrip("\r\n")))
    prompt = yaml.safe_load((ROOT / "func-test/scenarios" / f"{case}.yaml").read_text(encoding="utf-8"))["profile"]["prompt_regex"]
    got = s23_probe_ready_windows(rows, prompt)
    assert not got["problems"] and got["windows"] >= WAL_PROBE_WINDOWS[case], got


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
    if scenarios and case in WAL_PROBE_WINDOWS:
        _assert_wal_probe_windows(case, out)


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


def _wal_row(seq: int, direction: str, payload: bytes, source: str = "system", com: str = "COM0") -> dict:
    import base64
    return {"seq": seq, "mono_ts_ns": seq * 1000, "com": com, "dir": direction, "source": source,
            "payload_b64": base64.b64encode(payload).decode("ascii")}


_PROMPT = "(?m)^root@op3:.*# "


@pytest.mark.core_portable
@pytest.mark.memory_only
def test_wal_probe_ready_window_oracle():
    """SPEC-GAP #7（rulings/p7.md：以 WAL 先後判定）：nonce probe 的 TX 起到 READY 證據（RX 見 nonce 且其後見
    prompt）止，WAL 中同 COM 不得有 daemon 自身（system／system:*）以外來源的 TX；窗口未收（無 READY 證據）即不成立。"""
    probe = _wal_row(10, "TX", b"echo __READY__ab12cd34\n")
    echo = _wal_row(11, "RX", b"EXEC:echo __READY__ab12cd34\r\nRESULT:echo __READY__ab12cd34:OK\r\n")
    prompt = _wal_row(13, "RX", b"root@op3:~# ")
    agent = _wal_row(12, "TX", b"echo AGENT_P7\n", source="agent:l15")
    human = _wal_row(12, "TX", b"HUMAN_P7\n", source="human:c1")
    after = _wal_row(14, "TX", b"echo AGENT_P7\n", source="agent:l15")
    other_com = _wal_row(12, "TX", b"x\n", source="agent:l15", com="COM1")
    ok = s23_probe_ready_windows([probe, echo, prompt, after, other_com], _PROMPT)
    assert ok == {"windows": 1, "problems": []}, ok
    for bad in (agent, human, _wal_row(12, "TX", b"x\n", source="rpc:other")):
        got = s23_probe_ready_windows([probe, echo, bad, prompt], _PROMPT)
        assert got["windows"] == 1 and len(got["problems"]) == 1 and "seq=12" in got["problems"][0], got
    # daemon 自身的 system:* 來源（如救援鍵 system:recover）不算 agent／human
    keys = _wal_row(12, "TX", b"\x03", source="system:recover")
    assert s23_probe_ready_windows([probe, echo, keys, prompt], _PROMPT) == {"windows": 1, "problems": []}
    # prompt 只出現在 nonce 之前、或根本沒有 prompt → 窗口未收
    early = _wal_row(9, "RX", b"root@op3:~# ")
    got = s23_probe_ready_windows([early, probe, echo], _PROMPT)
    assert got["windows"] == 0 and got["problems"] and "未見 READY" in got["problems"][0], got
    # 亂序輸入以 seq 排序；nonce 跨 RX 紀錄切開仍可辨識
    split = [_wal_row(20, "TX", b"echo __READY__00ff00ff\n"), _wal_row(21, "RX", b"EXEC:echo __READY__00f"),
             _wal_row(22, "RX", b"f00ff\r\nRESULT:ok\r\nroot@op3:~# ")]
    assert s23_probe_ready_windows(list(reversed(split)), _PROMPT) == {"windows": 1, "problems": []}
