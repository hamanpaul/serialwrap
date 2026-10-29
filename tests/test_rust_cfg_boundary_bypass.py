"""P3-T1 審查修正：cfg 邊界檢查的繞過與誤判案例。

本檔為新增檔；既有 `tests/test_rust_cfg_boundary.py` 為凍結 oracle，不得修改。
涵蓋：`cfg_attr` 的後續屬性、`cfg!` 的各種定界符與空白、`#` 與 `[` 之間的空白、
群組 `use`（`std::{…, os::unix::…}`、`std::os::{…, unix::…}`）、`std::env::consts::OS`／`FAMILY`、
`all(test,)` 尾端逗號、`c"…"` 字串、Cargo.toml 的引號 key 與 `[build-dependencies.x]` 表形式、
工具錯誤（exit 2）與 cp1252 stdout 下的 UTF-8 輸出。
"""
from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "scripts" / "rust" / "check_cfg_boundary.py"
pytestmark = [pytest.mark.core_portable, pytest.mark.process_rpc]


def _run(root: Path, env: dict[str, str] | None = None) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [sys.executable, str(SCRIPT), "--root", str(root)],
        capture_output=True,
        text=True,
        encoding="utf-8",
        env=env,
        check=False,
    )


def _write(tmp: Path, rel: str, body: str) -> None:
    f = tmp / rel
    f.parent.mkdir(parents=True, exist_ok=True)
    f.write_text(body, encoding="utf-8")


def _crate_rs(tmp: Path, body: str) -> None:
    _write(tmp, "crates/sw-core/src/bypass.rs", body)
    _write(tmp, "crates/sw-core/Cargo.toml", '[package]\nname = "sw-core"\nversion = "0.0.0"\n')


DETECT = [
    # cfg_attr 的後續屬性也要檢查（原版只看第一個參數）
    ("#[cfg_attr(not(test), cfg(unix))]\nfn a() {}\n", "cfg-predicate"),
    ("#[cfg_attr(test, cfg_attr(unix, allow(dead_code)))]\nfn a() {}\n", "cfg-predicate"),
    ("#[cfg_attr(test, allow(dead_code), cfg(windows))]\nfn a() {}\n", "cfg-predicate"),
    # cfg! 的 `{}` 與 `[]` 定界符、空白
    ("fn a() -> bool { cfg! { windows } }\n", "cfg-predicate"),
    ("fn a() -> bool { cfg![unix] }\n", "cfg-predicate"),
    ("fn a() -> bool { cfg ! ( windows ) }\n", "cfg-predicate"),
    # `#` 與 `[` 之間、`[` 與 cfg 之間的合法空白
    ("# [cfg(unix)]\nfn a() {}\n", "cfg-predicate"),
    ("#[ cfg ( unix ) ]\nfn a() {}\n", "cfg-predicate"),
    # 群組 use 寫法
    ("use std::{fs, os::unix::fs::PermissionsExt};\n", "os-module"),
    ("use std::os::{raw::c_int, unix::io::RawFd};\n", "os-module"),
    ("use std::os;\nfn f() { let _ = os::windows::fs::symlink_file; }\n", "os-module"),
    # 執行期平台名稱比較
    ('fn f() -> bool { std::env::consts::OS == "windows" }\n', "os-name"),
    ("use std::env::consts::{FAMILY};\n", "os-name"),
]


@pytest.mark.parametrize("body, rule", DETECT)
def test_detects_bypass(tmp_path, body, rule):
    _crate_rs(tmp_path, body)
    r = _run(tmp_path)
    assert r.returncode == 1, r.stdout + r.stderr
    assert "crates/sw-core/src/bypass.rs:" in r.stdout
    assert f": {rule}: " in r.stdout


ALLOW = [
    # 尾端逗號是合法 Rust
    "#[cfg(all(test,))]\nfn a() {}\n",
    '#[cfg(any(test, feature = "test-fakes",))]\nfn a() {}\n',
    # 不含平台謂詞的 cfg_attr
    "#[cfg_attr(test, derive(Debug))]\nstruct A;\n",
    "#[cfg_attr(not(test), allow(dead_code))]\nfn a() {}\n",
    '#[cfg_attr(test, cfg(feature = "test-fakes"))]\nfn a() {}\n',
    # C 字串常值內容不是程式碼
    "fn f() -> &'static std::ffi::CStr { c\"#[cfg(unix)] text\" }\n",
    "fn f() -> &'static str { \"std::os::unix\" }\n",
    # std::os::raw、std::os::fd 不是平台模組
    "use std::os::raw::c_int;\nuse std::os::fd::AsRawFd;\n",
    # 非 OS／FAMILY 的 consts 成員
    "use std::env::consts::ARCH;\n",
    "fn f() { let cfg_os = 1; let _ = cfg_os; }\n",
]


@pytest.mark.parametrize("body", ALLOW)
def test_allows_safe_forms(tmp_path, body):
    _crate_rs(tmp_path, body)
    r = _run(tmp_path)
    assert r.returncode == 0, r.stdout + r.stderr


CARGO = [
    # 引號 key 的 package.build
    ('[package]\nname = "sw-core"\nversion = "0.0.0"\n"build" = "tools/build.rs"\n', "build-script"),
    # 表形式的 build-dependencies
    ('[package]\nname = "sw-core"\nversion = "0.0.0"\n[build-dependencies.cc]\nversion = "1"\n', "build-script"),
    # 任何 [target.*] 區段（不只 cfg(...)）
    (
        '[package]\nname = "sw-core"\nversion = "0.0.0"\n'
        '[target.x86_64-pc-windows-gnu.dependencies]\nfoo = { path = "../foo" }\n',
        "target-section",
    ),
    (
        '[package]\nname = "sw-core"\nversion = "0.0.0"\n'
        "[target.'cfg(unix)'.build-dependencies]\ncc = \"1\"\n",
        "build-script",
    ),
]


# P5 第 6 輪 SPEC-GAP #3（編排者裁決：明確 ids，不改斷言）：pytest 9.2 起過長的字串參數改以 `toml2` 類 id 呈現，
# nodeid 隨 pytest 版本變動、無法穩定入鎖。明確 ids 沿用 pytest 9.0.3 的自動 id 形式（`<toml>-<rule>`），
# 使已凍結的三個 nodeid 不變（無須移除）、第四個（target-section）在各版本皆穩定。
CARGO_IDS = [f"{toml}-{rule}" for toml, rule in CARGO]


@pytest.mark.parametrize("toml, rule", CARGO, ids=CARGO_IDS)
def test_cargo_toml_bypass(tmp_path, toml, rule):
    _write(tmp_path, "crates/sw-core/src/lib.rs", "")
    _write(tmp_path, "crates/sw-core/Cargo.toml", toml)
    r = _run(tmp_path)
    assert r.returncode == 1, r.stdout + r.stderr
    assert "crates/sw-core/Cargo.toml:" in r.stdout
    assert f": {rule}: " in r.stdout


def test_missing_crates_dir_is_tool_error(tmp_path):
    r = _run(tmp_path / "does-not-exist")
    assert r.returncode == 2, r.stdout + r.stderr
    assert r.stderr.strip() != ""


def test_zero_scanned_files_is_tool_error(tmp_path):
    (tmp_path / "crates" / "sw-core").mkdir(parents=True)
    r = _run(tmp_path)
    assert r.returncode == 2, r.stdout + r.stderr
    assert r.stderr.strip() != ""


def test_non_ascii_output_survives_cp1252(tmp_path):
    _write(tmp_path, "crates/sw-core/src/a.rs", "#[cfg(unix)] // 僅 POSIX\nfn a() {}\n")
    env = {**os.environ, "PYTHONIOENCODING": "cp1252"}
    r = _run(tmp_path, env=env)
    assert r.returncode == 1, r.stdout + r.stderr
    assert "僅 POSIX" in r.stdout
