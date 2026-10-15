#!/usr/bin/env python3
"""擷取 TRANSPORT_STALL 人類可讀提示文字（transport_stall_hint）的位元組級 golden（GAP-G-09）。

本腳本以代表性輸入（含 null real_path、null busid、有效 busid 與四捨五入邊界）
呼叫 ``sw_core.transport_stall.transport_stall_hint``，並產生
``contracts/golden/last_error/transport_stall_hint.json``。

用法：``python3 scripts/stage0/capture_transport_stall_hint.py [--out PATH]``
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))

from sw_core import transport_stall

CAPTURED_BY = "scripts/stage0/capture_transport_stall_hint.py"
SOURCE = "sw_core/transport_stall.py:transport_stall_hint"
DEFAULT_OUT = ROOT / "contracts" / "golden" / "last_error" / "transport_stall_hint.json"

INPUT_CASES: list[dict] = [
    {"real_path": None, "rx_age_s": 45.0, "busid": None},
    {"real_path": "/dev/ttyUSB0", "rx_age_s": 45.04, "busid": "1-1"},
    {"real_path": "/dev/ttyUSB0", "rx_age_s": 44.96, "busid": None},
    {"real_path": "/dev/ttyUSB0", "rx_age_s": 3.05, "busid": "1-1.4"},
]


def capture() -> dict:
    """執行特徵化案例擷取，產生 golden 字典。"""
    cases = []
    for c in INPUT_CASES:
        with mock.patch("sw_core.transport_stall.resolve_usb_busid", return_value=c["busid"]):
            expected = transport_stall.transport_stall_hint(c["real_path"], c["rx_age_s"])
        cases.append({
            "busid": c["busid"],
            "expected": expected,
            "real_path": c["real_path"],
            "rx_age_s": c["rx_age_s"],
        })
    return {
        "captured_by": CAPTURED_BY,
        "cases": cases,
        "source": SOURCE,
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--out", type=Path, default=DEFAULT_OUT, help="輸出路徑（預設為契約 golden）")
    args = parser.parse_args(argv)

    doc = capture()
    text = json.dumps(doc, ensure_ascii=False, sort_keys=True, separators=(",", ":")) + "\n"
    args.out.parent.mkdir(parents=True, exist_ok=True)
    args.out.write_bytes(text.encode("utf-8"))
    print(f"wrote {args.out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
