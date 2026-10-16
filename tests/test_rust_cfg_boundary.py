from __future__ import annotations

import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "scripts" / "rust" / "check_cfg_boundary.py"
pytestmark = [pytest.mark.core_portable, pytest.mark.process_rpc]


def _run(root: Path) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [sys.executable, str(SCRIPT), "--root", str(root)],
        capture_output=True,
        text=True,
        check=False,
    )


def _crate(tmp: Path, name: str, rel: str, body: str) -> None:
    f = tmp / "crates" / name / rel
    f.parent.mkdir(parents=True, exist_ok=True)
    f.write_text(body, encoding="utf-8")
    toml = tmp / "crates" / name / "Cargo.toml"
    if not toml.exists():
        toml.write_text(
            f'[package]\nname = "{name}"\nversion = "0.0.0"\n', encoding="utf-8"
        )


@pytest.mark.skipif(not (ROOT / "crates").is_dir(), reason="crates/ 尚未建立")
@pytest.mark.xfail(strict=True, reason="P3-T1 紅燈")
def test_repo_clean():
    r = _run(ROOT)
    assert r.returncode == 0, r.stdout + r.stderr


@pytest.mark.parametrize(
    "snippet, rule",
    [
        ("#[cfg(unix)]\nfn a() {}\n", "cfg-predicate"),
        ("#[cfg(windows)]\nfn a() {}\n", "cfg-predicate"),
        ('#[cfg(target_os = "linux")]\nfn a() {}\n', "cfg-predicate"),
        ('#![cfg(target_family = "unix")]\n', "cfg-predicate"),
        ("#[cfg_attr(unix, allow(dead_code))]\nfn a() {}\n", "cfg-predicate"),
        ("fn a() { if cfg!(windows) {} }\n", "cfg-predicate"),
        ("#[cfg(all(test, unix))]\nfn a() {}\n", "cfg-predicate"),
        ("#[cfg(debug_assertions)]\nfn a() {}\n", "cfg-predicate"),
        ('#[cfg(feature = "other")]\nfn a() {}\n', "cfg-predicate"),
        ("use std::os::unix::ffi::OsStrExt;\n", "os-module"),
        ("fn a() { unsafe { libc::getpid(); } }\n", "ffi-crate"),
        ("use windows_sys::Win32::Foundation::HANDLE;\n", "ffi-crate"),
    ],
)
@pytest.mark.parametrize("subdir", ["src", "tests"])
@pytest.mark.xfail(strict=True, reason="P3-T1 紅燈")
def test_detects_forbidden(tmp_path, snippet, rule, subdir):
    _crate(tmp_path, "sw-cli", f"{subdir}/bad.rs", snippet)
    r = _run(tmp_path)
    assert r.returncode == 1
    assert f"crates/sw-cli/{subdir}/bad.rs:" in r.stdout
    assert f": {rule}: " in r.stdout


@pytest.mark.parametrize(
    "snippet",
    [
        "#[cfg(test)]\nmod t {}\n",
        '#[cfg(feature = "test-fakes")]\npub mod fakes {}\n',
        '#[cfg(all(test, feature = "test-fakes"))]\nmod t {}\n',
        "#[cfg(not(test))]\nfn a() {}\n",
    ],
)
@pytest.mark.xfail(strict=True, reason="P3-T1 紅燈")
def test_allows_test_and_fakes(tmp_path, snippet):
    _crate(tmp_path, "sw-cli", "src/ok.rs", snippet)
    assert _run(tmp_path).returncode == 0


@pytest.mark.xfail(strict=True, reason="P3-T1 紅燈")
def test_pal_exempt(tmp_path):
    _crate(
        tmp_path,
        "sw-pal",
        "src/sysenv/posix.rs",
        "#[cfg(unix)]\nuse libc::getpid;\n",
    )
    assert _run(tmp_path).returncode == 0


@pytest.mark.xfail(strict=True, reason="P3-T1 紅燈")
def test_build_script_and_target_section(tmp_path):
    _crate(tmp_path, "sw-proto", "build.rs", "fn main() {}\n")
    (tmp_path / "crates" / "sw-proto" / "Cargo.toml").write_text(
        '[package]\nname = "sw-proto"\nversion = "0.0.0"\n[target.\'cfg(unix)\'.dependencies]\nlibc = "0.2"\n',
        encoding="utf-8",
    )
    r = _run(tmp_path)
    assert r.returncode == 1
    assert ": build-script: " in r.stdout and ": target-section: " in r.stdout
