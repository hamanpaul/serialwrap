#!/usr/bin/env python3
"""擷取 FakeTarget（commit 93553e6）行為 golden。一次性產生，產出後不得為遷就實作而重產。

本腳本扮演 daemon 端：FakeTarget 在 PTY master 端讀命令、寫回應，因此本腳本只經
PTY slave 寫入命令並讀回應（與 FakeTarget 的 master 端形成一對）。舊版 FakeTarget 以
``git show 93553e6:func-test/lib/fake_target.py`` 取得，於記憶體中 exec 載入，不使用 HEAD，
也不寫入任何暫存檔。

用法（於 repo 根目錄執行）::

    python3 tools/uart_stdio_bridge/tests/fixtures/capture_faketarget_golden.py

golden 檔格式（每檔一個 JSON 物件）::

    {
      "source_blob": "93553e6:func-test/lib/fake_target.py",
      "config": <TargetConfig.from_yaml 可接受的 YAML 形式 dict>,
      "steps": [
        {
          "pre_pause_s": float,     # 選填：送入輸入前呼叫 pause_responding(pre_pause_s)
          "pre_wait_ms": int,       # 選填：送入輸入前等待（期間持續擷取，noise 照常記錄）
          "input_b64": str,         # 寫入 PTY slave 的位元組
          "output_b64": str,        # 送出後 wait_ms 內收到、已剔除 noise 行的位元組
          "wait_ms": int
        }
      ],
      "noise": [{"t_ms": int, "tick": int}],   # 僅 noise.json：相對 start() 的到達時間
      "total_ms": int                          # 僅 noise.json：擷取結束時間
    }
"""
from __future__ import annotations

import argparse
import base64
import json
import os
import pathlib
import re
import select
import subprocess
import sys
import time
import types
from typing import Any

import yaml

SOURCE_REV = "93553e6"
SOURCE_PATH = "func-test/lib/fake_target.py"
NOISE_RE = re.compile(rb"KDBG:tick:(\d+)\r\n")

REPO_ROOT = pathlib.Path(__file__).resolve().parents[4]
GOLDEN_DIR = pathlib.Path(__file__).resolve().parent / "faketarget_golden"
BRCM_CASE = REPO_ROOT / "func-test" / "cases" / "brcm-01-login-flow.yaml"

PROMPT_DEFAULT = "root@prplOS:/# "
DEFAULT_RESPONSE = "EXEC:{cmd}\r\nRESULT:{cmd}:OK\r\nroot@prplOS:/# "

DEFAULT_CFG: dict[str, Any] = {
    "platform": "prpl",
    "boot_banner": "boot done\r\nroot@prplOS:/# ",
    "noise": {"enabled": False},
    "commands": {"default": DEFAULT_RESPONSE},
}

OVERRIDES_CFG: dict[str, Any] = {
    "platform": "prpl",
    "boot_banner": "boot done\r\nroot@prplOS:/# ",
    "noise": {"enabled": False},
    "commands": {
        "default": DEFAULT_RESPONSE,
        "overrides": {
            "slow": {"response": "S\r\n# ", "delay_ms": 700},
            "hang": {"interactive": True},
        },
    },
}

NOISE_CFG: dict[str, Any] = {
    "platform": "prpl",
    "boot_banner": "boot done\r\nroot@prplOS:/# ",
    "noise": {"enabled": True, "interval_ms": 100},
    "commands": {"default": DEFAULT_RESPONSE},
}


def _brcm_cfg() -> dict[str, Any]:
    """登入流程 golden 直接取自 brcm-01 案例的 target 區塊（不手抄）。"""
    data = yaml.safe_load(BRCM_CASE.read_text(encoding="utf-8"))
    return data["target"]


def _scenarios() -> dict[str, dict[str, Any]]:
    """每個情境：config（YAML 形式）、steps（輸入與等待）、noise（是否擷取 noise）。

    step 欄位：input（bytes）、wait_ms、選填 pre_pause_s／pre_wait_ms／expect_empty。
    expect_empty 只用於 interactive 命令（不回應，輸出為空是預期行為）。
    """
    return {
        "default": {
            "config": DEFAULT_CFG,
            "steps": [
                {"input": b"ls\n", "wait_ms": 500},
                {"input": b"\n", "wait_ms": 500},
                {"input": b"echo a b\r\n", "wait_ms": 500},
                {"input": b"\xff\xfe\n", "wait_ms": 500},
                {"input": b"pwd\n", "wait_ms": 500},
            ],
        },
        "login_brcm": {
            "config": _brcm_cfg(),
            "steps": [
                {"input": b"bogus\n", "wait_ms": 500},
                {"input": b"admin\n", "wait_ms": 500},
                {"input": b"wrong\n", "wait_ms": 500},
                {"input": b"admin\n", "wait_ms": 500},
                {"input": b"sh\n", "wait_ms": 500},
                {"input": b"\n", "wait_ms": 500},
            ],
        },
        "overrides": {
            "config": OVERRIDES_CFG,
            "steps": [
                {"input": b"slow\n", "wait_ms": 1200},
                {"input": b"ls\n", "wait_ms": 500},
                {"input": b"hang\n", "wait_ms": 500, "expect_empty": True},
                {"input": b"\n", "wait_ms": 500},
            ],
        },
        "pause": {
            "config": DEFAULT_CFG,
            "steps": [
                {"pre_pause_s": 1.0, "input": b"ls\n", "wait_ms": 1500},
                {"input": b"\n", "wait_ms": 500},
            ],
        },
        "noise": {
            "config": NOISE_CFG,
            "noise": True,
            "steps": [
                {"pre_pause_s": 0.35, "pre_wait_ms": 1000, "input": b"ls\n", "wait_ms": 500},
            ],
        },
    }


def load_legacy_fake_target() -> types.ModuleType:
    """以 git blob 93553e6 於記憶體中載入舊版 FakeTarget（不寫暫存檔、不用 HEAD）。"""
    blob = subprocess.run(
        ["git", "-C", str(REPO_ROOT), "show", f"{SOURCE_REV}:{SOURCE_PATH}"],
        check=True,
        capture_output=True,
    ).stdout
    module = types.ModuleType("fake_target_legacy")
    module.__file__ = f"<git:{SOURCE_REV}:{SOURCE_PATH}>"
    sys.modules[module.__name__] = module
    exec(compile(blob, module.__file__, "exec"), module.__dict__)  # noqa: S102 - 受控的 git blob
    return module


def _now_ms(t0: float) -> float:
    return (time.monotonic() - t0) * 1000.0


def _pump(fd: int, events: list[tuple[float, bytes]], t0: float, until_ms: float) -> None:
    """持續讀取 PTY slave 直到 until_ms，每個 chunk 記錄到達時間（相對 t0，毫秒）。"""
    while True:
        remaining = until_ms - _now_ms(t0)
        if remaining <= 0:
            return
        ready, _, _ = select.select([fd], [], [], min(remaining, 50.0) / 1000.0)
        if ready:
            chunk = os.read(fd, 65536)
            if not chunk:
                raise RuntimeError("PTY slave EOF：FakeTarget 提前結束")
            events.append((_now_ms(t0), chunk))


def _wait_banner(fd: int, events: list[tuple[float, bytes]], t0: float, expected: bytes) -> None:
    """等待開機 banner 到達（以 10 ms 為單位），不加入固定延遲，確保 t0 之後的時序與模型一致。"""
    while True:
        _pump(fd, events, t0, _now_ms(t0) + 10.0)
        if b"".join(chunk for _, chunk in events).startswith(expected):
            return
        if _now_ms(t0) > 2000.0:
            raise RuntimeError("2 s 內未收到開機 banner")


def capture(legacy: types.ModuleType, spec: dict[str, Any]) -> dict[str, Any]:
    cfg_yaml = spec["config"]
    expected_banner = legacy.TargetConfig.from_yaml(cfg_yaml).boot_banner.encode("utf-8")
    target = legacy.FakeTarget(legacy.TargetConfig.from_yaml(cfg_yaml))
    fd = os.open(target.slave_path, os.O_RDWR | os.O_NOCTTY)
    events: list[tuple[float, bytes]] = []
    steps_out: list[dict[str, Any]] = []
    try:
        t0 = time.monotonic()
        target.start()
        _wait_banner(fd, events, t0, expected_banner)

        for step in spec["steps"]:
            if "pre_pause_s" in step:
                target.pause_responding(step["pre_pause_s"])
            if step.get("pre_wait_ms"):
                _pump(fd, events, t0, _now_ms(t0) + step["pre_wait_ms"])
            start_idx = len(events)
            t_write = _now_ms(t0)
            os.write(fd, step["input"])
            _pump(fd, events, t0, t_write + step["wait_ms"])
            raw = b"".join(chunk for _, chunk in events[start_idx:])
            out = NOISE_RE.sub(b"", raw)
            if b"KDBG" in out:
                raise RuntimeError(f"noise 行未完整剔除（可能被切斷）：{out!r}")
            if not out and not step.get("expect_empty"):
                raise RuntimeError(f"非空命令步驟沒有輸出：{step['input']!r}")
            item: dict[str, Any] = {
                "input_b64": base64.b64encode(step["input"]).decode("ascii"),
                "output_b64": base64.b64encode(out).decode("ascii"),
                "wait_ms": step["wait_ms"],
            }
            if "pre_pause_s" in step:
                item["pre_pause_s"] = step["pre_pause_s"]
            if step.get("pre_wait_ms"):
                item["pre_wait_ms"] = step["pre_wait_ms"]
            steps_out.append(item)
        total_ms = _now_ms(t0)

        noise: list[dict[str, int]] = []
        for t, chunk in events:
            for match in NOISE_RE.finditer(chunk):
                noise.append({"t_ms": round(t), "tick": int(match.group(1))})
    finally:
        target.stop()
        os.close(fd)

    doc: dict[str, Any] = {
        "source_blob": f"{SOURCE_REV}:{SOURCE_PATH}",
        "config": cfg_yaml,
        "steps": steps_out,
    }
    if spec.get("noise"):
        if [n["tick"] for n in noise] != list(range(1, len(noise) + 1)):
            raise RuntimeError(f"noise tick 不連續：{noise}")
        doc["noise"] = noise
        doc["total_ms"] = round(total_ms)
    elif noise:
        raise RuntimeError("非 noise 情境卻擷取到 noise 行")
    return doc


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--out", type=pathlib.Path, default=GOLDEN_DIR)
    parser.add_argument("--only", action="append", default=None, help="只擷取指定情境（可重複）")
    args = parser.parse_args()

    legacy = load_legacy_fake_target()
    args.out.mkdir(parents=True, exist_ok=True)
    scenarios = _scenarios()
    names = args.only or list(scenarios)
    for name in names:
        doc = capture(legacy, scenarios[name])
        path = args.out / f"{name}.json"
        path.write_text(json.dumps(doc, ensure_ascii=False, indent=2, sort_keys=True) + "\n", encoding="utf-8")
        print(f"wrote {path} steps={len(doc['steps'])}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
