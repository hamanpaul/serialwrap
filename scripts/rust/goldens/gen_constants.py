#!/usr/bin/env python3
"""產生 ``contracts/golden/constants.json``（P3-T7 的常數同步基準）。

以 ``ast`` 從 ``sw_core/constants.py``、``client.py``、``cli.py``、``arbiter.py``、
``command_completion.py``、``file_transfer.py``、``cli_trace.py``、``setup_cmd.py``
抽出常數的實際值，並附上架構規範常數，寫入 JSON。

用法::

    python3 scripts/rust/goldens/gen_constants.py [--out contracts/golden/constants.json]
"""

from __future__ import annotations

import argparse
import ast
import json
from pathlib import Path

ROOT = Path(__file__).resolve().parents[3]


def extract_constants(root: Path = ROOT) -> dict[str, object]:
    res: dict[str, object] = {}

    # 1. sw_core/constants.py
    tree = ast.parse((root / "sw_core/constants.py").read_text(encoding="utf-8"))
    for node in tree.body:
        if isinstance(node, ast.AnnAssign) and isinstance(node.target, ast.Name):
            if node.target.id == "DEFAULT_TCP_PORT":
                # int(os.environ.get('SERIALWRAP_TCP_PORT', '48700'))
                val = node.value.args[0].args[1].value
                res["constants.DEFAULT_TCP_PORT_RAW_DEFAULT"] = val
            elif node.target.id == "LOOPBACK_TCP_HOSTS":
                # frozenset({'127.0.0.1', 'localhost', '::1'})
                hosts = sorted([elt.value for elt in node.value.args[0].elts])
                res["constants.LOOPBACK_TCP_HOSTS"] = hosts

    # 2. sw_core/client.py
    tree = ast.parse((root / "sw_core/client.py").read_text(encoding="utf-8"))
    for node in tree.body:
        if isinstance(node, ast.Assign):
            for t in node.targets:
                if isinstance(t, ast.Name):
                    if t.id == "RETRYABLE_READONLY_METHODS":
                        res["client.RETRYABLE_READONLY_METHODS"] = sorted(
                            [elt.value for elt in node.value.args[0].elts]
                        )
                    elif t.id == "_RETRY_BACKOFF_BASE_S":
                        res["client.RETRY_BACKOFF_BASE_S"] = node.value.value
                    elif t.id == "_RETRY_BACKOFF_MAX_S":
                        res["client.RETRY_BACKOFF_MAX_S"] = node.value.value
                    elif t.id == "_RETRYABLE_TRANSIENT_ERROR_CODES":
                        res["client.TRANSIENT_ERROR_CODES"] = sorted(
                            [elt.value for elt in node.value.args[0].elts]
                        )
                    elif t.id == "_PROBE_TIMEOUT_S":
                        res["client.PROBE_TIMEOUT_S"] = node.value.value
                    elif t.id == "_PROBE_DEADLINE_S":
                        res["client.PROBE_DEADLINE_S"] = node.value.value
                    elif t.id == "_PROBE_METHODS":
                        res["client.PROBE_METHODS"] = [elt.value for elt in node.value.elts]

    for n in ast.walk(tree):
        if isinstance(n, ast.Call) and isinstance(n.func, ast.Attribute) and n.func.attr == "recv":
            if n.args and isinstance(n.args[0], ast.Constant):
                res["client.RECV_CHUNK"] = n.args[0].value
                break

    # 3. sw_core/cli.py
    tree = ast.parse((root / "sw_core/cli.py").read_text(encoding="utf-8"))
    for node in tree.body:
        if isinstance(node, ast.Assign):
            for t in node.targets:
                if isinstance(t, ast.Name):
                    if t.id == "DEFAULT_RPC_TIMEOUT_S":
                        res["cli.DEFAULT_RPC_TIMEOUT_S"] = node.value.value
                    elif t.id == "LONG_RPC_TIMEOUT_FLOOR_S":
                        res["cli.LONG_RPC_TIMEOUT_FLOOR_S"] = node.value.value
                    elif t.id == "_LONG_RPC_METHODS":
                        res["cli.LONG_RPC_METHODS"] = sorted(
                            [elt.value for elt in node.value.args[0].elts]
                        )

    for n in ast.walk(tree):
        if isinstance(n, ast.FunctionDef) and n.name == "_endpoint_alive":
            for sub in ast.walk(n):
                if (
                    isinstance(sub, ast.Call)
                    and isinstance(sub.func, ast.Attribute)
                    and sub.func.attr == "settimeout"
                ):
                    if sub.args and isinstance(sub.args[0], ast.Constant):
                        res["cli.ENDPOINT_PROBE_TIMEOUT_S"] = sub.args[0].value
                        break

    # 4. sw_core/setup_cmd.py
    tree = ast.parse((root / "sw_core/setup_cmd.py").read_text(encoding="utf-8"))
    for node in tree.body:
        if isinstance(node, ast.Assign):
            for t in node.targets:
                if isinstance(t, ast.Name) and t.id == "SYSTEM_SOCKET":
                    res["constants.SYSTEM_SOCKET"] = node.value.value

    # 5. sw_core/command_completion.py
    tree = ast.parse((root / "sw_core/command_completion.py").read_text(encoding="utf-8"))
    for node in tree.body:
        if isinstance(node, ast.Assign):
            for t in node.targets:
                if isinstance(t, ast.Name) and t.id == "COMPLETION_REGEX_MAX_BYTES":
                    res["command_completion.COMPLETION_REGEX_MAX_BYTES"] = node.value.value

    # 6. sw_core/arbiter.py
    tree = ast.parse((root / "sw_core/arbiter.py").read_text(encoding="utf-8"))
    for node in tree.body:
        if isinstance(node, ast.Assign):
            for t in node.targets:
                if isinstance(t, ast.Name):
                    if t.id == "CMD_WARN_BYTES":
                        res["arbiter.CMD_WARN_BYTES"] = node.value.value
                    elif t.id == "CMD_REJECT_BYTES":
                        res["arbiter.CMD_REJECT_BYTES"] = node.value.value

    # 7. sw_core/file_transfer.py
    tree = ast.parse((root / "sw_core/file_transfer.py").read_text(encoding="utf-8"))
    for node in tree.body:
        if isinstance(node, ast.Assign):
            for t in node.targets:
                if isinstance(t, ast.Name):
                    if t.id == "DEFAULT_CHUNK_SIZE":
                        res["file_transfer.FILE_DEFAULT_CHUNK_SIZE"] = node.value.value
                    elif t.id == "_ACK_MODES":
                        res["file_transfer.FILE_ACK_MODES"] = [
                            elt.value for elt in node.value.elts
                        ]

    # 8. sw_core/cli_trace.py
    tree = ast.parse((root / "sw_core/cli_trace.py").read_text(encoding="utf-8"))
    for node in tree.body:
        if isinstance(node, ast.Assign):
            for t in node.targets:
                if isinstance(t, ast.Name) and t.id == "_MAX_NUMERIC_LOG_LEVEL_DIGITS":
                    res["cli_trace.MAX_NUMERIC_LOG_LEVEL_DIGITS"] = node.value.value

    # 9. Rust / arch constants（arch §3.1 / §5.10 / §7.4 / §10.1）
    res["arch.CLIENT_RESPONSE_MAX_BYTES"] = 256 * 1024 * 1024
    res["arch.DELEGATE_DEPTH_ENV"] = "SERIALWRAP_DELEGATE_DEPTH"
    res["arch.DELEGATE_ARGV_SHA_ENV"] = "SERIALWRAP_DELEGATE_ARGV_SHA"

    return res


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--out",
        type=Path,
        default=ROOT / "contracts/golden/constants.json",
        help="輸出路徑（預設 contracts/golden/constants.json）",
    )
    args = parser.parse_args()

    data = extract_constants()
    args.out.parent.mkdir(parents=True, exist_ok=True)
    with open(args.out, "w", encoding="utf-8", newline="\n") as f:
        json.dump(data, f, indent=2, sort_keys=True, ensure_ascii=False)
        f.write("\n")
    print(f"Wrote {len(data)} constants to {args.out}")


if __name__ == "__main__":
    main()
