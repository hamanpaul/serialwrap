#!/usr/bin/env python3
"""F139：取受測 daemon 的版本供 Slice 2.1 gate identity（`daemon_version`）。

serialwrapd 依 Python 1:1 不提供 `--version`；版本只在 RPC 回應的 `version` 欄（#154，每個回應皆 setdefault）。
本工具以 tests/contract_blackbox/slice21 的 harness 在 throwaway 目錄起一個 daemon（FakeTarget，不碰正式環境）、
送 `daemon.status`、讀 `version` 後停止。取不到即印空字串並以 exit 3 結束（gate 將 identity 記為 null → INCOMPLETE）。

用法：python3 scripts/rust/probe_daemon_version.py --daemon <serialwrapd> [--cli <serialwrap>] [--work-dir DIR]
"""
from __future__ import annotations

import argparse
import pathlib
import shutil
import sys
import tempfile
from typing import Any

ROOT = pathlib.Path(__file__).resolve().parents[2]


def version_from_result(result: Any) -> str | None:
    """RPC 回應 → 版本字串；缺欄位、非字串或空字串回 None（純函式）。"""
    if not isinstance(result, dict):
        return None
    ver = result.get("version")
    return ver if isinstance(ver, str) and ver.strip() else None


def probe(daemon: str, *, cli: str | None = None, work: pathlib.Path | None = None) -> str | None:
    sys.path.insert(0, str(ROOT / "scripts" / "rust"))
    import event_latency_measure as elm  # noqa: PLC0415 — 共用 harness 匯入與 NDJSON client

    harness = elm._import_harness()
    root = pathlib.Path(work or tempfile.mkdtemp(prefix="swver-"))
    daemon_cmd = [daemon] if not daemon.endswith(".py") else [sys.executable, daemon]
    d = rpc = None
    try:
        d = harness.start_daemon(root, daemon=daemon_cmd, cli=[cli] if cli else None, login=False)
        rpc = elm._Rpc(d.endpoint)
        return version_from_result(rpc.call("daemon.status", {}))
    finally:
        if rpc is not None:
            rpc.close()
        if d is not None:
            d.stop()
        shutil.rmtree(root, ignore_errors=True)


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--daemon", required=True)
    ap.add_argument("--cli")
    ap.add_argument("--work-dir")
    args = ap.parse_args(argv)
    try:
        ver = probe(args.daemon, cli=args.cli, work=pathlib.Path(args.work_dir) if args.work_dir else None)
    except Exception as exc:  # noqa: BLE001 — 取不到即缺漏，由 judge 判 INCOMPLETE
        print(f"probe_daemon_version: {type(exc).__name__}: {exc}", file=sys.stderr)
        ver = None
    print(ver or "")
    return 0 if ver else 3


if __name__ == "__main__":
    sys.exit(main())
