"""P6 新增 Rust 測試必須進 L1 凍結分母（sol 補審 P6 #1；07 Task 13、P2 Task 13 R5-F3）。

修訂版 P6 Task 13 要求「各 Task 在自己的紅燈 commit 登記新增 nodeid」：未登記者被刪除或改名時，
lock 不會識別必要節點消失。本檔以原始碼靜態掃描 P6 新增的 Rust 測試檔（不呼叫 cargo），
逐一斷言其 nodeid（`cargo:<crate>::<test 檔名>::<fn>`）已在對應的 `s22_gate:*` 集合：

- 一般測試（非 `cap_`、非 `#[cfg(windows)]`）→ `s22_gate:cargo_workspace` linux；
- `cap_posix_pty__*` → `s22_gate:console_conformance_pty` linux；
- `cap_windows_*`／`#[cfg(windows)]` 須以原生 Windows 收集登記（LOCAL-WINDOWS-TODO），本檔不判定。
"""
from __future__ import annotations

import json
import pathlib
import re

import pytest

pytestmark = [pytest.mark.core_portable, pytest.mark.memory_only]

ROOT = pathlib.Path(__file__).resolve().parents[1]
LOCK = ROOT / "tests/acceptance/l1_required.lock.json"

# P6（Slice 2.2）新增的 Rust 測試檔（相對 crates/<crate>/tests/）
P6_RUST_TEST_FILES = [
    ("sw-core", "deferred_bc02.rs"),
    ("sw-core", "human_line_mode.rs"),
    ("sw-core", "line_editor.rs"),
    ("sw-core", "out_ring.rs"),
    ("sw-core", "telnet_filter.rs"),
    ("sw-core", "ownership.rs"),
    ("sw-core", "session_core_console.rs"),
    ("sw-core", "human_tx_lane.rs"),
    ("sw-core", "fence_human_raw.rs"),
    ("sw-core", "actor_liveness.rs"),
    ("sw-core", "lease_p6.rs"),
    ("sw-core", "rescue.rs"),
    ("sw-core", "session_core_lease.rs"),
    ("sw-daemon", "deferred_methods.rs"),
    ("sw-pal", "console_conformance.rs"),
    ("sw-proto", "constants_slice22.rs"),
    ("sw-proto", "shlex_golden.rs"),
]

_ATTR = re.compile(r"#\[([^\]]*)\]")
_FN = re.compile(r"^\s*(?:pub\s+)?fn\s+([A-Za-z_][A-Za-z0-9_]*)\s*\(")


def _rust_tests(src: str) -> list[tuple[str, set[str]]]:
    """回傳 [(fn 名, 屬性集合)]：只收頂層（不縮排）`#[test]` fn；屬性可與 fn 同列（`#[test] fn x() {…}`）。"""
    out: list[tuple[str, set[str]]] = []
    attrs: set[str] = set()
    for line in src.splitlines():
        if line.startswith("//"):
            continue
        if line.startswith("#["):
            attrs.update(a.strip() for a in _ATTR.findall(line))
            line = _ATTR.sub("", line).strip()
            if not line:
                continue
        m = _FN.match(line) if not line[:1].isspace() else None
        if m and "test" in attrs:
            out.append((m.group(1), set(attrs)))
        if line.strip():
            attrs = set()
    return out


def _expected() -> dict[str, list[str]]:
    want: dict[str, list[str]] = {"s22_gate:cargo_workspace": [], "s22_gate:console_conformance_pty": []}
    for crate, name in P6_RUST_TEST_FILES:
        src = (ROOT / "crates" / crate / "tests" / name).read_text(encoding="utf-8")
        stem = name[: -len(".rs")]
        for fn, attrs in _rust_tests(src):
            nodeid = f"cargo:{crate}::{stem}::{fn}"
            if "cfg(windows)" in attrs or fn.startswith("cap_windows_"):
                continue
            if fn.startswith("cap_posix_pty__"):
                want["s22_gate:console_conformance_pty"].append(nodeid)
            elif not fn.startswith("cap_"):
                want["s22_gate:cargo_workspace"].append(nodeid)
    return want


def test_scanner_sees_one_line_and_attributed_tests():
    src = "#[test] fn a() {}\n#[test]\n#[ignore = \"cap:x\"]\nfn cap_x__b() {}\nfn helper() {}\n#[cfg(windows)]\n#[test]\nfn w() {}\n"
    got = _rust_tests(src)
    assert [n for n, _ in got] == ["a", "cap_x__b", "w"]
    assert "cfg(windows)" in got[2][1]


def test_scanner_finds_known_golden_test():
    want = _expected()
    assert "cargo:sw-core::telnet_filter::golden_matches_python_byte_for_byte" in want["s22_gate:cargo_workspace"]
    assert len(want["s22_gate:cargo_workspace"]) >= 100


@pytest.mark.parametrize("set_id", ["s22_gate:cargo_workspace", "s22_gate:console_conformance_pty"])
def test_p6_rust_tests_are_frozen_in_l1_lock(set_id):
    lock = json.loads(LOCK.read_text(encoding="utf-8"))
    frozen = set(lock["sets"].get(set_id, {}).get("linux", {}).get("nodeids", []))
    missing = [n for n in _expected()[set_id] if n not in frozen]
    assert not missing, f"{set_id}.linux 未登記 {len(missing)} 個 P6 Rust nodeid（以 regen_lock.py --l1-add-only 補登）：{missing[:10]}"
