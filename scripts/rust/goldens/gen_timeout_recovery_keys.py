#!/usr/bin/env python3
"""產生 Rust-only BC-09 的核准 timeout recovery 情境矩陣 golden。

golden 直接來自 OD-111／issue 308 的設定語意表，不啟動產品程式、不擷取產品輸出，
也不讀取凍結的 Python profile golden。

用法：
    python3 scripts/rust/goldens/gen_timeout_recovery_keys.py
    python3 scripts/rust/goldens/gen_timeout_recovery_keys.py --check
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[3]
OUT = ROOT / "contracts/golden/rust_only/timeout_recovery_keys.ndjson"

# 這是規格 oracle：CTRL_C=0x03、CTRL_D=0x04，清單順序就是實際 TX 順序。
SCENARIOS: tuple[dict[str, object], ...] = (
    {"case": "language_default_omitted", "template": "OMITTED", "target": "OMITTED", "effective": ["CTRL_C"], "tx_hex": "03"},
    {"case": "template_null_uses_default", "template": None, "target": "OMITTED", "effective": ["CTRL_C"], "tx_hex": "03"},
    {"case": "target_omitted_inherits", "template": ["CTRL_D", "CTRL_C"], "target": "OMITTED", "effective": ["CTRL_D", "CTRL_C"], "tx_hex": "0403"},
    {"case": "target_null_inherits", "template": ["CTRL_D", "CTRL_C"], "target": None, "effective": ["CTRL_D", "CTRL_C"], "tx_hex": "0403"},
    {"case": "target_list_replaces", "template": ["CTRL_D"], "target": ["CTRL_C"], "effective": ["CTRL_C"], "tx_hex": "03"},
    {"case": "empty_list_sends_nothing", "template": ["CTRL_C", "CTRL_D"], "target": [], "effective": [], "tx_hex": ""},
    {"case": "explicit_two_key_order", "template": ["CTRL_C", "CTRL_D"], "target": "OMITTED", "effective": ["CTRL_C", "CTRL_D"], "tx_hex": "0304"},
)


def render() -> str:
    return "".join(
        json.dumps(row, ensure_ascii=False, sort_keys=True, separators=(",", ":")) + "\n"
        for row in SCENARIOS
    )


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--check", action="store_true", help="驗證 golden 與核准情境表一致")
    args = parser.parse_args()
    expected = render()
    if args.check:
        try:
            actual = OUT.read_text(encoding="utf-8")
        except OSError as exc:
            print(f"timeout recovery golden 不存在：{OUT}: {exc}", file=sys.stderr)
            return 1
        if actual != expected:
            print(f"timeout recovery golden 與情境表不一致：{OUT}", file=sys.stderr)
            return 1
        print(f"timeout recovery golden 一致：{OUT.relative_to(ROOT)}")
        return 0
    OUT.parent.mkdir(parents=True, exist_ok=True)
    OUT.write_text(expected, encoding="utf-8", newline="\n")
    print(f"已產生 Rust-only timeout recovery golden：{OUT.relative_to(ROOT)}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
