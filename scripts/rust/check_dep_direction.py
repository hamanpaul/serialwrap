#!/usr/bin/env python3
"""Crate 依賴方向檢查（arch §1.3、§2.2；計畫 Task 1）。純 stdlib。

對兩個 target（linux／windows）各執行一次 ``cargo metadata --locked --filter-platform <triple>``，
以**允許表**檢查 workspace 成員之間的依賴（任何依賴種類，含 dev／build，皆計入）：

- ``sw-proto``：不依賴任何 workspace 成員；
- ``sw-pal``：不依賴任何 workspace 成員（尤其不得依賴 ``sw-proto``）；
- ``sw-core``：只允許 ``sw-proto``、``sw-pal``；
- ``sw-daemon``：只允許 ``sw-core``、``sw-proto``、``sw-pal``；
- ``sw-cli``：只允許 ``sw-proto``、``sw-pal``（不得連結 ``sw-core``／``sw-daemon``）。
- ``sw-com0com-helper``：只允許 ``sw-pal``；提權 helper 不得繞過 PAL 直接呼叫平台 API。

另：``sw-pal`` 以外的 crate 不得直接依賴 ``libc``／``windows-sys``。
六個必要成員缺一（名稱不符或未納入 workspace）為工具錯誤；未登錄於允許表的 workspace 成員視為違規。
違規列出 ``<crate> -> <dep>: <rule>``；乾淨 exit 0，違規 exit 1，工具錯誤 exit 2。
"""
from __future__ import annotations

import argparse
import json
import subprocess
import sys
from pathlib import Path

TRIPLES = ("x86_64-unknown-linux-gnu", "x86_64-pc-windows-gnu")
PAL = "sw-pal"
PROTO = "sw-proto"
CLI = "sw-cli"
CORE = "sw-core"
DAEMON = "sw-daemon"
COM0COM_HELPER = "sw-com0com-helper"
PLATFORM_LIBS = frozenset({"libc", "windows-sys"})

# 依賴允許表（arch §1.3）：成員 → 允許直接依賴的 workspace 成員。
ALLOWED_WORKSPACE_DEPS: dict[str, frozenset[str]] = {
    PROTO: frozenset(),
    PAL: frozenset(),
    CORE: frozenset({PROTO, PAL}),
    DAEMON: frozenset({CORE, PROTO, PAL}),
    CLI: frozenset({PROTO, PAL}),
    COM0COM_HELPER: frozenset({PAL}),
}


def cargo_metadata(root: Path, triple: str) -> dict:
    proc = subprocess.run(
        [
            "cargo",
            "metadata",
            "--locked",
            "--format-version",
            "1",
            "--filter-platform",
            triple,
            "--manifest-path",
            str(root / "Cargo.toml"),
        ],
        capture_output=True,
        text=True,
        cwd=root,
        check=False,
    )
    if proc.returncode != 0:
        raise RuntimeError(f"cargo metadata 失敗（{triple}）：{proc.stderr.strip()}")
    return json.loads(proc.stdout)


def violations_for(meta: dict) -> set[str]:
    id_to_name = {pkg["id"]: pkg["name"] for pkg in meta["packages"]}
    members = {id_to_name[mid] for mid in meta["workspace_members"]}
    missing = sorted(set(ALLOWED_WORKSPACE_DEPS) - members)
    if missing:
        raise RuntimeError(f"workspace 缺少必要成員（名稱不符或未納入 members）：{', '.join(missing)}")
    out: set[str] = set()
    for node in meta["resolve"]["nodes"]:
        src = id_to_name.get(node["id"])
        if src not in members:
            continue
        if src not in ALLOWED_WORKSPACE_DEPS:
            out.add(f"{src}: dep-direction（未登錄於依賴允許表的 workspace 成員）")
        allowed = ALLOWED_WORKSPACE_DEPS.get(src, frozenset())
        for dep in node.get("deps", []):
            dst = id_to_name.get(dep["pkg"])
            if dst is None:
                continue
            if dst in members and dst not in allowed:
                out.add(f"{src} -> {dst}: dep-direction（{src} 不得依賴 {dst}；依 arch §1.3 允許表）")
            if src != PAL and dst in PLATFORM_LIBS:
                out.add(f"{src} -> {dst}: dep-direction（平台 FFI 依賴只允許 sw-pal 直接持有）")
    return out


def run(root: Path) -> set[str]:
    found: set[str] = set()
    for triple in TRIPLES:
        found |= violations_for(cargo_metadata(root, triple))
    return found


def _force_utf8_stdio() -> None:
    """固定 stdout／stderr 為 UTF-8、LF：Windows 預設 code page（如 cp1252）無法編碼繁中訊息。"""
    for stream in (sys.stdout, sys.stderr):
        reconfigure = getattr(stream, "reconfigure", None)
        if reconfigure is not None:
            reconfigure(encoding="utf-8", newline="\n")


def main(argv: list[str] | None = None) -> int:
    _force_utf8_stdio()
    ap = argparse.ArgumentParser(description="crate 依賴方向檢查（arch §1.3）")
    default_root = Path(__file__).resolve().parents[2]
    ap.add_argument("--root", type=Path, default=default_root, help="repo 根目錄（預設為本腳本上兩層）")
    args = ap.parse_args(argv)
    try:
        found = run(args.root.resolve())
    except (RuntimeError, OSError, KeyError, json.JSONDecodeError) as exc:
        print(f"check_dep_direction: {exc}", file=sys.stderr)
        return 2
    for line in sorted(found):
        print(line)
    return 1 if found else 0


if __name__ == "__main__":
    sys.exit(main())
