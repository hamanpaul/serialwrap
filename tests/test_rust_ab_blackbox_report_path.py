"""`scripts/rust/ab_blackbox.sh` 的 A/B 比較器收尾：任何結果都要輸出報告路徑（S1X Y2）。

出口 run `s1exit-20261028T050554` 中比較器因 1 筆 baseline-fail 回非 0，`set -e` 使腳本在印出
「A/B 比較完成：<路徑>」前結束，exit driver 取不到報告路徑，gate 因「缺少 ab-report」連帶把
invariants／coverage／bb-* 判為證據不足。修正後：報告存在即印出路徑並保留比較器 exit code
（gate 依報告內容判 FAIL，而非缺檔）；比較器未產生報告時不印路徑。

本測試只截取腳本第 6 段（比較器收尾）以假比較器執行，不啟動 daemon、namespace 或 pytest。
"""
from __future__ import annotations

import re
import subprocess
from pathlib import Path

import pytest

pytestmark = [pytest.mark.posix_only, pytest.mark.process_rpc]

ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "scripts" / "rust" / "ab_blackbox.sh"
MARK = "# 6. 執行 ab_compare.py"
# 與 scripts/rust/stage1_exit_driver.sh 的 printed_path 樣式相同。
DRIVER_RE = re.compile(r"/[^\s]*target/ab-report/[^\s]+\.json")


def _run_tail(tmp_path: Path, *, compare_rc: int, write_report: bool) -> tuple[int, str, Path]:
    text = SCRIPT.read_text(encoding="utf-8")
    assert MARK in text, "ab_blackbox.sh 第 6 段標記不見了"
    tail = text[text.index(MARK):]
    repo = tmp_path / "repo"
    (repo / "scripts" / "rust").mkdir(parents=True)
    (repo / "target" / "ab-report").mkdir(parents=True)
    (repo / "scripts" / "rust" / "ab_compare.py").write_text(
        "import sys\n"
        "args = sys.argv[1:]\n"
        "out = args[args.index('--out') + 1]\n"
        f"if {write_report!r}:\n"
        "    open(out, 'w').write('{}')\n"
        f"sys.exit({compare_rc})\n",
        encoding="utf-8",
    )
    harness = tmp_path / "tail.sh"
    harness.write_text(
        "set -euo pipefail\n"
        f'REPO_ROOT="{repo}"\nRUN_ID="20261028_000000_1"\n'
        'A_XML=a.xml\nB_XML=b.xml\nID_A=ia.json\nID_B=ib.json\nREQUIRED_CASES=req.json\n'
        + tail,
        encoding="utf-8",
    )
    res = subprocess.run(["bash", str(harness)], capture_output=True, text=True, timeout=60)
    return res.returncode, res.stdout + res.stderr, repo / "target" / "ab-report" / "20261028_000000_1.json"


def test_report_path_printed_when_comparator_passes(tmp_path):
    rc, out, report = _run_tail(tmp_path, compare_rc=0, write_report=True)
    assert rc == 0, out
    assert DRIVER_RE.findall(out)[-1] == str(report), out


def test_report_path_printed_and_rc_kept_when_comparator_fails(tmp_path):
    """比較器回非 0 但已寫出報告：仍須印出路徑（driver 可取得），且 exit code 照傳（不得轉為 0）。"""
    rc, out, report = _run_tail(tmp_path, compare_rc=1, write_report=True)
    assert rc == 1, out
    found = DRIVER_RE.findall(out)
    assert found and found[-1] == str(report), out


def test_no_report_path_when_comparator_wrote_nothing(tmp_path):
    """比較器未產生報告：不得印出不存在的路徑（避免 gate 讀到缺檔路徑以外的猜測），exit code 照傳。"""
    rc, out, _report = _run_tail(tmp_path, compare_rc=2, write_report=False)
    assert rc == 2, out
    assert not DRIVER_RE.findall(out), out
