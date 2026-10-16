"""DUT 行為模型（dut/model.py、dut/plan.py）失敗測試（P2-T4，TDD 紅燈）。

對應 plan 03-stage0-test-infra.md Task 4 的 T4-1。產品模組尚未實作，因此所有
``uart_stdio_bridge.dut`` 的 import 都放在測試函式內（lazy import），確保 pytest 收集不受影響。
"""
from __future__ import annotations

import ast
import base64
import dataclasses
import json
import re
import subprocess
import sys
import types
from pathlib import Path

import pytest
import yaml

pytestmark = pytest.mark.memory_only

ROOT = Path(__file__).resolve().parents[3]
SRC = ROOT / "tools" / "uart_stdio_bridge" / "src"
DUT_DIR = SRC / "uart_stdio_bridge" / "dut"
GOLDEN = Path(__file__).resolve().parent / "fixtures" / "faketarget_golden"
CASES_DIR = ROOT / "func-test" / "cases"
LEGACY_REV = "93553e6"
LEGACY_PATH = "func-test/lib/fake_target.py"

DEFAULT_BANNER = b"boot done\r\nroot@prplOS:/# "
PROMPT = b"root@prplOS:/# "
FORBIDDEN_IO_MODULES = {"os", "pty", "termios", "select", "socket", "subprocess"}


def _src_on_path() -> None:
    if str(SRC) not in sys.path:
        sys.path.insert(0, str(SRC))


def _load_golden(name: str) -> dict:
    return json.loads((GOLDEN / f"{name}.json").read_text(encoding="utf-8"))


def _data(emits) -> bytes:
    return b"".join(e.data for e in emits)


def _load_legacy_module() -> types.ModuleType:
    """以 git blob 93553e6 於記憶體載入舊版 FakeTarget（不使用 HEAD，不寫暫存檔）。"""
    blob = subprocess.run(
        ["git", "-C", str(ROOT), "show", f"{LEGACY_REV}:{LEGACY_PATH}"],
        check=True,
        capture_output=True,
    ).stdout
    module = types.ModuleType("fake_target_legacy_93553e6")
    module.__file__ = f"<git:{LEGACY_REV}:{LEGACY_PATH}>"
    sys.modules[module.__name__] = module
    exec(compile(blob, module.__file__, "exec"), module.__dict__)  # noqa: S102 - 受控的 git blob
    return module


def _normalize(cfg) -> dict:
    out = {}
    for field in dataclasses.fields(cfg):
        if field.name in ("overrides", "login_steps"):
            continue
        out[field.name] = getattr(cfg, field.name)
    out["overrides"] = {k: dataclasses.asdict(v) for k, v in cfg.overrides.items()}
    out["login_steps"] = [dataclasses.asdict(s) for s in cfg.login_steps]
    return out


def _quiet_cfg(**extra):
    from uart_stdio_bridge.dut.model import TargetConfig

    data = {"noise": {"enabled": False}}
    data.update(extra)
    return TargetConfig.from_yaml(data)


def _noise_cfg(interval_ms: int):
    from uart_stdio_bridge.dut.model import TargetConfig

    return TargetConfig.from_yaml({"noise": {"enabled": True, "interval_ms": interval_ms}})


def _plan(*triggers: dict) -> dict:
    return {"schema_version": "dut-plan.v1", "seed": 1, "triggers": list(triggers)}


def _trigger(tid: str, line: str, action: dict, *, once: bool = True) -> dict:
    return {"id": tid, "line_contains": line, "action": action, "once": once}


def _replay(model, golden: dict) -> bytes:
    """依 golden 的 wait_ms 以虛擬時間重放；回傳所有輸出位元組（含 banner）。"""
    now = 0
    out = _data(model.boot(now))
    for step in golden["steps"]:
        out += _data(model.feed(base64.b64decode(step["input_b64"]), now))
        end = now + step["wait_ms"]
        while (d := model.next_deadline_ms()) is not None and d <= end:
            out += _data(model.poll(d))
        now = end
    return out


def _advance(model, to_ms: int) -> list:
    """處理 to_ms 之前（含）所有到期輸出。"""
    out = []
    while (d := model.next_deadline_ms()) is not None and d <= to_ms:
        out += model.poll(d)
    out += model.poll(to_ms)
    return out


@pytest.mark.parametrize("name", ["default", "login_brcm", "overrides"])
def test_model_matches_faketarget_golden(name):
    _src_on_path()
    from uart_stdio_bridge.dut.model import DutModel, TargetConfig

    g = _load_golden(name)
    cfg = TargetConfig.from_yaml(g["config"])
    expected = b"".join(base64.b64decode(s["output_b64"]) for s in g["steps"])
    banner = cfg.boot_banner.encode("utf-8")
    assert _replay(DutModel(cfg), g) == banner + expected


def test_from_yaml_same_semantics_as_legacy():
    _src_on_path()
    from uart_stdio_bridge.dut.model import TargetConfig as NewConfig

    legacy = _load_legacy_module()
    cases = sorted(CASES_DIR.glob("*.yaml"))
    assert len(cases) == 13
    for case in cases:
        target = (yaml.safe_load(case.read_text(encoding="utf-8")) or {}).get("target")
        old = _normalize(legacy.TargetConfig.from_yaml(target))
        new = _normalize(NewConfig.from_yaml(target))
        assert new == old, case.name


def test_json_roundtrip():
    _src_on_path()
    from uart_stdio_bridge.dut.model import TargetConfig

    for name in ("default", "login_brcm", "overrides"):
        cfg = TargetConfig.from_yaml(_load_golden(name)["config"])
        assert TargetConfig.from_json(cfg.to_json()) == cfg, name


def test_delay_serializes_following_outputs():
    _src_on_path()
    from uart_stdio_bridge.dut.model import DutModel

    cfg = _quiet_cfg(commands={"overrides": {"slow": {"response": "S\r\n# ", "delay_ms": 700}}})
    m = DutModel(cfg)
    m.boot(0)
    assert m.feed(b"slow\n\n", 0) == []
    assert m.poll(699) == []
    out = m.poll(700)
    assert [e.data for e in out] == [b"S\r\n# ", b"# "]
    assert [e.at_ms for e in out] == [700, 700]


def test_pause_delays_commands_not_login():
    _src_on_path()
    from uart_stdio_bridge.dut.model import DutModel

    cfg = _quiet_cfg(
        login_steps=[
            {"expect": "admin", "respond": "\r\nPassword: "},
            {"expect": "pw", "respond": "\r\nroot@x:/# "},
        ]
    )
    m = DutModel(cfg)
    m.boot(0)
    m.pause_responding(0, 1000)
    login = m.feed(b"admin\n", 0) + m.feed(b"pw\n", 0)
    assert [(e.at_ms, e.data) for e in login] == [(0, b"\r\nPassword: "), (0, b"\r\nroot@x:/# ")]
    cmd = m.feed(b"ls\n", 0) + m.poll(1000)
    resp = [e for e in cmd if e.data.startswith(b"EXEC:ls")]
    assert len(resp) == 1
    assert resp[0].at_ms == 1000


def test_pause_capped_at_30s():
    _src_on_path()
    from uart_stdio_bridge.dut.model import DutModel

    m = DutModel(_quiet_cfg())
    m.boot(0)
    m.pause_responding(0, 60000)
    assert m.feed(b"ls\n", 0) == []
    assert m.poll(29_999) == []
    resp = [e for e in m.poll(30000) if e.kind == "response"]
    assert [e.at_ms for e in resp] == [30000]


def test_noise_ticks_and_pause():
    _src_on_path()
    from uart_stdio_bridge.dut.model import DutModel

    m = DutModel(_noise_cfg(50))
    boot = m.boot(0)
    assert _data([e for e in boot if e.kind == "banner"]) == DEFAULT_BANNER
    noise_at_boot = [(e.at_ms, e.data) for e in boot if e.kind == "noise"]
    assert noise_at_boot == [(0, b"KDBG:tick:1\r\n")]
    assert [(e.at_ms, e.data) for e in m.poll(50) if e.kind == "noise"] == [(50, b"KDBG:tick:2\r\n")]
    m.pause_responding(60, 100)
    for t in range(100, 160):
        assert [e for e in m.poll(t) if e.kind == "noise"] == [], t
    resumed = [(e.at_ms, e.data) for e in m.poll(160) if e.kind == "noise"]
    assert resumed == [(160, b"KDBG:tick:3\r\n")]
    after = [(e.at_ms, e.data) for e in m.poll(210) if e.kind == "noise"]
    assert after == [(210, b"KDBG:tick:4\r\n")]


def test_model_matches_noise_golden():
    _src_on_path()
    from uart_stdio_bridge.dut.model import DutModel, TargetConfig

    g = _load_golden("noise")
    model = DutModel(TargetConfig.from_yaml(g["config"]))
    emits = list(model.boot(0))
    now = 0
    for step in g["steps"]:
        if "pre_pause_s" in step:
            model.pause_responding(now, round(step["pre_pause_s"] * 1000))
        now += step.get("pre_wait_ms", 0)
        emits += _advance(model, now)
        emits += model.feed(base64.b64decode(step["input_b64"]), now)
        now += step["wait_ms"]
        emits += _advance(model, now)

    expected_cmd = b"".join(base64.b64decode(s["output_b64"]) for s in g["steps"])
    cmd = b"".join(e.data for e in emits if e.kind not in ("banner", "noise"))
    assert cmd == expected_cmd

    horizon = g["total_ms"] - 100
    model_noise = [
        (e.at_ms, int(re.search(rb"tick:(\d+)", e.data).group(1)))
        for e in emits
        if e.kind == "noise"
    ]
    golden_noise = [(n["t_ms"], n["tick"]) for n in g["noise"]]
    model_noise = [x for x in model_noise if x[0] < horizon]
    golden_noise = [x for x in golden_noise if x[0] < horizon]
    assert len(golden_noise) >= 5
    assert [tick for _, tick in model_noise] == [tick for _, tick in golden_noise]
    for (model_at, _), (golden_at, _) in zip(model_noise, golden_noise):
        assert abs(model_at - golden_at) <= 60


def test_half_prompt_trigger():
    _src_on_path()
    from uart_stdio_bridge.dut.model import DutModel, ScenarioPlan

    inject = b"\x1b[32m"
    plan = _plan(
        _trigger(
            "L15_HALF",
            "L15_HALF",
            {
                "kind": "half_prompt",
                "split_at": 5,
                "gap_ms": 800,
                "inject_b64": base64.b64encode(inject).decode("ascii"),
            },
        )
    )
    m = DutModel(_quiet_cfg(), ScenarioPlan.from_json(plan))
    m.boot(0)
    body = b"EXEC:L15_HALF\r\nRESULT:L15_HALF:OK\r\n"
    immediate = _data(m.feed(b"L15_HALF\n", 0))
    assert PROMPT[5:] not in immediate
    assert _data(m.poll(799)) == b""
    late = _data(m.poll(800))
    assert late == PROMPT[5:]
    assert immediate + late == body + inject + PROMPT


def test_silent_trigger():
    _src_on_path()
    from uart_stdio_bridge.dut.model import DutModel, ScenarioPlan

    plan = _plan(_trigger("L15_SILENT", "L15_SILENT", {"kind": "silent"}))
    m = DutModel(_quiet_cfg(), ScenarioPlan.from_json(plan))
    m.boot(0)
    assert m.feed(b"L15_SILENT\n", 0) == []
    for t in (1000, 5000, 10_000):
        assert m.poll(t) == []
    out = m.feed(b"ls\n", 10_000)
    assert _data(out) == b"EXEC:ls\r\nRESULT:ls:OK\r\nroot@prplOS:/# "


def test_logical_reboot_generation():
    _src_on_path()
    from uart_stdio_bridge.dut.model import DutModel, ScenarioPlan

    plan = _plan(
        _trigger(
            "reboot",
            "reboot",
            {"kind": "logical_reboot", "banner_delay_ms": 500, "pre_text": "BOOTING\r\n"},
        )
    )
    cfg = _quiet_cfg(commands={"overrides": {"ls": {"response": "EXEC\r\ncustom# "}}})
    m = DutModel(cfg, ScenarioPlan.from_json(plan))
    m.boot(0)
    assert m.generation == 1
    m.feed(b"ls\n", 0)  # 動態 prompt 變為 custom#
    immediate = _data(m.feed(b"reboot\n", 0))
    assert b"BOOTING\r\n" in immediate
    assert m.generation == 2
    assert _data(m.poll(499)) == b""
    assert _data(m.poll(500)) == DEFAULT_BANNER
    assert _data(m.feed(b"\n", 500)) == PROMPT


def test_flood_exact_length_bounded_chunks():
    _src_on_path()
    from uart_stdio_bridge.dut.model import DutModel, ScenarioPlan

    plan = _plan(_trigger("flood", "flood", {"kind": "flood", "bytes": 200_000, "line": "FLOOD{n}\r\n"}))
    m = DutModel(_quiet_cfg(), ScenarioPlan.from_json(plan))
    m.boot(0)
    emits = list(m.feed(b"flood\n", 0))
    for _ in range(100_000):
        d = m.next_deadline_ms()
        if d is None:
            break
        emits += m.poll(d)
    flood = [e.data for e in emits if e.kind == "flood"]
    assert sum(len(c) for c in flood) == 200_000
    assert max(len(c) for c in flood) <= 65536
    assert b"".join(flood).startswith(b"FLOOD")


def test_stop_stdin_and_resume():
    _src_on_path()
    from uart_stdio_bridge.dut.model import DutModel, ScenarioPlan

    plan = _plan(_trigger("stopin", "stopin", {"kind": "stop_stdin", "resume_after_ms": 1000}))
    m = DutModel(_quiet_cfg(), ScenarioPlan.from_json(plan))
    m.boot(0)
    m.feed(b"stopin\n", 0)
    assert m.stdin_paused is True
    m.poll(999)
    assert m.stdin_paused is True
    m.poll(1000)
    assert m.stdin_paused is False


def test_control_ack_shapes():
    _src_on_path()
    from uart_stdio_bridge.dut.model import DutModel

    m = DutModel(_quiet_cfg())
    m.boot(0)
    emits, ack = m.apply_control({"id": "a", "action": "emit", "payload_b64": "AP8="}, 0)
    assert [e.data for e in emits] == [b"\x00\xff"]
    assert set(ack) == {"id", "ok", "generation", "t_ms"}
    assert ack["id"] == "a"
    assert ack["ok"] is True
    assert ack["generation"] == 1
    assert isinstance(ack["t_ms"], int)

    emits, ack = m.apply_control({"id": "b", "action": "no_such_action"}, 0)
    assert emits == []
    assert ack["id"] == "b"
    assert ack["ok"] is False
    assert ack["error"] == "unknown_action"


def test_model_has_no_io_imports():
    for name in ("model.py", "plan.py"):
        tree = ast.parse((DUT_DIR / name).read_text(encoding="utf-8"))
        modules: set[str] = set()
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                modules |= {alias.name.split(".")[0] for alias in node.names}
            elif isinstance(node, ast.ImportFrom) and node.level == 0 and node.module:
                modules.add(node.module.split(".")[0])
        assert not (modules & FORBIDDEN_IO_MODULES), (name, modules & FORBIDDEN_IO_MODULES)


def test_dut_package_self_contained():
    package_prefix = "uart_stdio_bridge.dut"
    files = sorted(DUT_DIR.glob("*.py"))
    assert files, "dut/ 套件尚未存在"
    for path in files:
        tree = ast.parse(path.read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                for alias in node.names:
                    top = alias.name.split(".")[0]
                    ok = top in sys.stdlib_module_names or alias.name.startswith(package_prefix)
                    assert ok, (path.name, alias.name)
            elif isinstance(node, ast.ImportFrom):
                if node.level > 0:
                    assert node.level == 1, (path.name, node.level)
                    continue
                mod = node.module or ""
                ok = mod.split(".")[0] in sys.stdlib_module_names or mod.startswith(package_prefix)
                assert ok, (path.name, mod)
