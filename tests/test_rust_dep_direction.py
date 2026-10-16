"""P3-T1 審查修正：依賴方向檢查的允許表、缺成員與 `--locked`。

本檔為新增檔；既有 `tests/test_rust_workspace_layout.py` 為凍結 oracle，不得修改。
全部使用 path-only 的臨時 workspace（外部 `libc` 為本機 path 套件），全程離線。
"""
from __future__ import annotations

import os
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "scripts" / "rust" / "check_dep_direction.py"
pytestmark = [pytest.mark.core_portable, pytest.mark.process_rpc]

# 依賴種類：normal（[dependencies]）、dev（[dev-dependencies]）；`libc` 指向臨時目錄內的外部 path 套件。
OK_LAYOUT: dict[str, list[tuple[str, str]]] = {
    "sw-proto": [],
    "sw-pal": [],
    "sw-core": [("sw-proto", "normal"), ("sw-pal", "normal")],
    "sw-daemon": [("sw-core", "normal"), ("sw-proto", "normal"), ("sw-pal", "normal")],
    "sw-cli": [("sw-proto", "normal"), ("sw-pal", "normal"), ("sw-pal", "dev")],
}


@pytest.fixture(autouse=True)
def _cargo_available():
    if shutil.which("cargo") is None:
        if os.environ.get("SW_REQUIRE_CARGO") == "1":
            pytest.fail("SW_REQUIRE_CARGO=1 但找不到 cargo（證據不足，非 PASS）")
        pytest.skip("cargo 不存在（證據不足，非 PASS）")


def _make_ws(base: Path, layout: dict[str, list[tuple[str, str]]]) -> Path:
    ws = base / "ws"
    ext_libc = base / "ext" / "libc"
    ws.mkdir(parents=True)
    (ws / "Cargo.toml").write_text('[workspace]\nmembers = ["crates/*"]\nresolver = "2"\n', encoding="utf-8")
    if any(dep == "libc" for deps in layout.values() for dep, _ in deps):
        (ext_libc / "src").mkdir(parents=True)
        (ext_libc / "Cargo.toml").write_text('[package]\nname = "libc"\nversion = "0.0.0"\nedition = "2021"\n', encoding="utf-8")
        (ext_libc / "src" / "lib.rs").write_text("", encoding="utf-8")
    for name, deps in layout.items():
        d = ws / "crates" / name
        (d / "src").mkdir(parents=True)
        (d / "src" / "lib.rs").write_text("", encoding="utf-8")
        normal = [f'{dep} = {{ path = "{_dep_path(dep, ext_libc)}" }}' for dep, kind in deps if kind == "normal"]
        dev = [f'{dep} = {{ path = "{_dep_path(dep, ext_libc)}" }}' for dep, kind in deps if kind == "dev"]
        text = f'[package]\nname = "{name}"\nversion = "0.0.0"\nedition = "2021"\n\n[dependencies]\n'
        text += "".join(line + "\n" for line in normal)
        if dev:
            text += "\n[dev-dependencies]\n" + "".join(line + "\n" for line in dev)
        (d / "Cargo.toml").write_text(text, encoding="utf-8")
    subprocess.run(
        ["cargo", "generate-lockfile", "--manifest-path", str(ws / "Cargo.toml")],
        capture_output=True, text=True, check=True,
    )
    return ws


def _dep_path(dep: str, ext_libc: Path) -> str:
    return str(ext_libc) if dep == "libc" else f"../{dep}"


def _run(ws: Path) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [sys.executable, str(SCRIPT), "--root", str(ws)],
        capture_output=True, text=True, encoding="utf-8", check=False,
    )


def _plus(base: dict[str, list[tuple[str, str]]], name: str, *extra: tuple[str, str]) -> dict[str, list[tuple[str, str]]]:
    out = {k: list(v) for k, v in base.items()}
    out[name] = out[name] + list(extra)
    return out


def test_allowed_layout_passes(tmp_path):
    ws = _make_ws(tmp_path, OK_LAYOUT)
    r = _run(ws)
    assert r.returncode == 0, r.stdout + r.stderr


VIOLATIONS = [
    ("cli_to_core", _plus(OK_LAYOUT, "sw-cli", ("sw-core", "normal")), "sw-cli -> sw-core"),
    ("cli_to_daemon", _plus(OK_LAYOUT, "sw-cli", ("sw-daemon", "normal")), "sw-cli -> sw-daemon"),
    ("cli_to_core_dev_only", _plus(OK_LAYOUT, "sw-cli", ("sw-core", "dev")), "sw-cli -> sw-core"),
    ("pal_to_proto", _plus(OK_LAYOUT, "sw-pal", ("sw-proto", "normal")), "sw-pal -> sw-proto"),
    ("proto_to_pal", _plus(OK_LAYOUT, "sw-proto", ("sw-pal", "normal")), "sw-proto -> sw-pal"),
    ("core_to_cli", _plus(OK_LAYOUT, "sw-core", ("sw-cli", "normal")), "sw-core -> sw-cli"),
    ("daemon_to_cli", _plus(OK_LAYOUT, "sw-daemon", ("sw-cli", "normal")), "sw-daemon -> sw-cli"),
    ("core_to_libc", _plus(OK_LAYOUT, "sw-core", ("libc", "normal")), "sw-core -> libc"),
]


@pytest.mark.parametrize("layout, expect", [(v[1], v[2]) for v in VIOLATIONS], ids=[v[0] for v in VIOLATIONS])
def test_violation_is_reported(tmp_path, layout, expect):
    ws = _make_ws(tmp_path, layout)
    r = _run(ws)
    assert r.returncode == 1, r.stdout + r.stderr
    assert f"{expect}: dep-direction" in r.stdout


def test_missing_member_is_tool_error(tmp_path):
    # 把 sw-cli 改名為 sw-client 並仍依賴 sw-core：舊版以名稱比對會直接放行。
    layout = {
        "sw-proto": [],
        "sw-pal": [],
        "sw-core": [("sw-proto", "normal"), ("sw-pal", "normal")],
        "sw-daemon": [("sw-core", "normal")],
        "sw-client": [("sw-core", "normal")],
    }
    ws = _make_ws(tmp_path, layout)
    r = _run(ws)
    assert r.returncode == 2, r.stdout + r.stderr
    assert "sw-cli" in r.stderr


def test_stale_lock_is_not_rewritten(tmp_path):
    ws = _make_ws(tmp_path, OK_LAYOUT)
    lock = ws / "Cargo.lock"
    lock.write_text("version = 4\n", encoding="utf-8")
    before = lock.read_bytes()
    r = _run(ws)
    assert r.returncode == 2, r.stdout + r.stderr
    assert lock.read_bytes() == before
