"""P3 golden 新鮮度：產生器重跑的輸出必須與 contracts/golden 逐位元組相同。

之後每個 golden Task 在 ``GENERATORS`` 追加一列。
"""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
pytestmark = [pytest.mark.core_portable, pytest.mark.process_rpc]

GENERATORS = [
    ("gen_pyjson.py", "pyjson.tsv"),
    ("gen_pycompat.py", "pycompat.tsv"),
    ("gen_pycompat.py", "pypath.posix.tsv"),
    ("gen_pycompat.py", "pypath.windows.tsv"),
    ("gen_unicode_tables.py", "unicode_tables.txt"),
    ("gen_pyre.py", "pyre.tsv"),
    ("gen_constants.py", "constants.json"),
    ("gen_runtime_paths.py", "runtime_paths.posix.tsv"),
    ("gen_runtime_paths.py", "runtime_paths.windows.tsv"),
    ("gen_endpoint.py", "endpoint.tsv"),
    ("gen_wal_line.py", "wal_line.tsv"),
    ("gen_cli_trace.py", "cli_trace.tsv"),
    # P6 Slice 2.2（07 Task 2／Task 3）
    ("gen_telnet_filter.py", "telnet_filter.tsv"),
    ("gen_line_editor.py", "line_editor.tsv"),
    ("gen_human_line_mode.py", "human_console_mode.tsv"),
    ("gen_shlex.py", "shlex_split.tsv"),
]


@pytest.mark.skipif(
    sys.version_info[:3] != (3, 12, 3),
    reason="golden 以 CPython 3.12.3 為基準（CI 釘 3.12.3；其他版本為證據不足，非 PASS）",
)
@pytest.mark.parametrize("script, out", GENERATORS)
def test_golden_is_fresh(tmp_path, script, out):
    target = tmp_path / out
    subprocess.run(
        [sys.executable, str(ROOT / "scripts/rust/goldens" / script), "--out", str(target)],
        check=True,
    )
    assert target.read_bytes() == (ROOT / "contracts/golden" / out).read_bytes()


@pytest.mark.skipif(
    sys.version_info[:3] != (3, 12, 3),
    reason="golden 以 CPython 3.12.3 為基準（CI 釘 3.12.3；其他版本為證據不足，非 PASS）",
)
def test_pyre_limits_is_fresh(tmp_path):
    """``pyre.limits.json`` 記錄 PARSE_DEPTH_LIMIT（Python 首次 RecursionError 的深度 T），須與重跑結果逐位元組相同。"""
    subprocess.run(
        [
            sys.executable,
            str(ROOT / "scripts/rust/goldens/gen_pyre.py"),
            "--out",
            str(tmp_path / "pyre.tsv"),
            "--limits-out",
            str(tmp_path / "pyre.limits.json"),
        ],
        check=True,
    )
    assert (tmp_path / "pyre.limits.json").read_bytes() == (ROOT / "contracts/golden/pyre.limits.json").read_bytes()


def test_wal_record_goldens_are_fresh(tmp_path):
    target = tmp_path / "wal"
    subprocess.run(
        [
            sys.executable,
            str(ROOT / "scripts/stage0/capture_wal_golden.py"),
            "--out-dir",
            str(target),
        ],
        check=True,
        cwd=ROOT,
    )
    expected = sorted((ROOT / "contracts/golden/wal").glob("*.json"))
    generated = sorted(target.glob("*.json"))
    assert expected, "contracts/golden/wal/*.json 不得為空"
    assert [p.name for p in generated] == [p.name for p in expected]
    for generated_file, expected_file in zip(generated, expected):
        assert generated_file.read_bytes() == expected_file.read_bytes(), generated_file.name


# parity-amendments-2026-10-26 #181（P6 審查 F8）：golden 以位元組比對（本檔與 Rust include_str!），
# Windows 檢出若被 core.autocrlf 改成 CRLF 會整批失效，故 contracts/golden/** 必須宣告 eol=lf；
# 刻意保存 CRLF 的 profiles fixture（37_bom_crlf）必須宣告 -text，避免被正規化。
_CRLF_FIXTURES = ("contracts/golden/profiles_fixtures/37_bom_crlf/",)


def _git(*args: str) -> str:
    return subprocess.run(["git", "-C", str(ROOT), *args], check=True, capture_output=True, text=True).stdout


def test_golden_gitattributes_force_lf():
    try:
        files = [f for f in _git("ls-files", "-z", "contracts/golden").split("\0") if f]
    except (OSError, subprocess.CalledProcessError) as exc:  # 非 git 檢出（例如 sdist）
        pytest.skip(f"無法列出 git 追蹤檔：{exc}")
    assert files, "contracts/golden 應有追蹤檔"
    out = subprocess.run(["git", "-C", str(ROOT), "check-attr", "-z", "--stdin", "text", "eol"],
                         input="\0".join(files) + "\0", check=True, capture_output=True, text=True).stdout
    parts = out.split("\0")
    attrs: dict[str, dict[str, str]] = {}
    for i in range(0, len(parts) - 2, 3):
        attrs.setdefault(parts[i], {})[parts[i + 1]] = parts[i + 2]
    for f in files:
        if f.startswith(_CRLF_FIXTURES):
            assert attrs[f]["text"] == "unset", f"{f} 需 -text 以保存 CRLF 位元組"
        else:
            assert attrs[f] == {"text": "set", "eol": "lf"}, f"{f} 的 git 屬性 {attrs[f]}，應為 text eol=lf"
