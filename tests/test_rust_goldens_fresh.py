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
    # P7 Task 3 上游補件（P3 缺口）：sw_proto::pyre::escape
    ("gen_pyre_escape.py", "pyre_escape.tsv"),
    # 審查 F84（P5-T19）：SC-19 壞 config.yaml 例外類別（permission 列於 root 時以 setpriv 降權）。
    ("gen_allowlist_unreadable_config.py", "allowlist_unreadable_config.tsv"),
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
