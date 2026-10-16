"""FakeTarget 改寫後的等價性失敗測試（P2-T4，TDD 紅燈，posix_pty）。

``func-test/lib/fake_target.FakeTarget`` 改為「PTY I/O thread＋DutModel」後，公開 API 與位元組行為
必須不變。``test_faketarget_replays_golden`` 對舊版（改寫前）即應通過，用以證明 golden 正確；
``test_faketarget_uses_model`` 與 ``test_target_config_import_safe`` 在改寫前應失敗。
"""
from __future__ import annotations

import base64
import json
import os
import select
import subprocess
import sys
import time
from pathlib import Path

import pytest

pytestmark = pytest.mark.posix_pty

ROOT = Path(__file__).resolve().parents[3]
FUNC_TEST = ROOT / "func-test"
GOLDEN = Path(__file__).resolve().parent / "fixtures" / "faketarget_golden"


def _func_test_on_path() -> None:
    if str(FUNC_TEST) not in sys.path:
        sys.path.insert(0, str(FUNC_TEST))


def _load_golden(name: str) -> dict:
    return json.loads((GOLDEN / f"{name}.json").read_text(encoding="utf-8"))


def _collect_until(fd: int, deadline: float) -> bytes:
    """自 PTY slave 讀取直到 deadline（monotonic 秒）為止。"""
    buf = b""
    while (remaining := deadline - time.monotonic()) > 0:
        ready, _, _ = select.select([fd], [], [], remaining)
        if ready:
            buf += os.read(fd, 65536)
    return buf


def test_faketarget_uses_model():
    _func_test_on_path()
    from lib.fake_target import FakeTarget, TargetConfig

    target = FakeTarget(TargetConfig.from_yaml(None))
    try:
        model = target.model  # 改寫前此屬性不存在
        from uart_stdio_bridge.dut.model import DutModel

        assert isinstance(model, DutModel)
    finally:
        target.stop()


@pytest.mark.parametrize("name", ["default", "login_brcm", "overrides", "pause"])
def test_faketarget_replays_golden(name):
    _func_test_on_path()
    from lib.fake_target import FakeTarget, TargetConfig

    golden = _load_golden(name)
    cfg = TargetConfig.from_yaml(golden["config"])
    target = FakeTarget(cfg)
    fd = os.open(target.slave_path, os.O_RDWR | os.O_NOCTTY)
    try:
        target.start()
        banner = _collect_until(fd, time.monotonic() + 0.5)
        assert banner == cfg.boot_banner.encode("utf-8")
        for index, step in enumerate(golden["steps"]):
            if "pre_pause_s" in step:
                target.pause_responding(step["pre_pause_s"])
            os.write(fd, base64.b64decode(step["input_b64"]))
            got = _collect_until(fd, time.monotonic() + step["wait_ms"] / 1000.0)
            want = base64.b64decode(step["output_b64"])
            assert got == want, f"{name} step {index}: got={got!r} want={want!r}"
    finally:
        target.stop()
        os.close(fd)


def test_target_config_import_safe():
    code = (
        "import sys; sys.modules['pty'] = None; sys.modules['termios'] = None; "
        "import target_config"
    )
    proc = subprocess.run(
        [sys.executable, "-c", code],
        cwd=str(FUNC_TEST / "lib"),
        capture_output=True,
        text=True,
        timeout=60,
    )
    assert proc.returncode == 0, proc.stderr
