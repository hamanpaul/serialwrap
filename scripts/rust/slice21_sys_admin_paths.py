#!/usr/bin/env python3
"""Slice 2.1 sys_admin 證據輸出路徑驗證。"""
from __future__ import annotations

import argparse
from pathlib import Path
import sys


def resolve_output_dir(out_dir: str | Path, repo_root: str | Path) -> Path:
    """解析輸出路徑；repo 內僅允許 target/，repo 外不受限。"""
    try:
        output = Path(out_dir).expanduser().resolve()
        root = Path(repo_root).expanduser().resolve()
    except (OSError, RuntimeError, ValueError) as exc:
        raise ValueError(f"無法解析 --out／repo 路徑：{exc}") from exc
    try:
        relative = output.relative_to(root)
    except ValueError:
        return output

    if not relative.parts or relative.parts[0] != "target":
        raise ValueError(f"--out 必須位於工作樹 target/ 內或 repo 外：{output}")
    return output


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--out", required=True)
    parser.add_argument("--repo", required=True)
    args = parser.parse_args(argv)
    try:
        output = resolve_output_dir(args.out, args.repo)
    except ValueError as exc:
        print(f"slice21_sys_admin_paths.py: {exc}", file=sys.stderr)
        return 2
    print(output)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
