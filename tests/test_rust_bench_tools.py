"""Stage 1B 效能與打包自檢工具測試（P4-T16）。

涵蓋：
- scripts/rust/bench_cli_roundtrip.py：CLI 暖快取往返延遲量測、分位數計算（nearest-rank）與 gate 判定。
- scripts/rust/check_no_onefile_extract.py：Windows/Linux 單檔暫存解壓行為檢查。
- Windows 原生 L1 cargo 測試判定。
- scripts/rust/check_p4_cases.py --l1-results 完整度閘門檢查。
"""
from __future__ import annotations

import importlib.util
import json
import subprocess
import sys
from pathlib import Path

import pytest

pytestmark = [
    pytest.mark.core_portable,
    pytest.mark.process_rpc,
]

ROOT = Path(__file__).resolve().parents[1]
BENCH_SCRIPT = ROOT / "scripts" / "rust" / "bench_cli_roundtrip.py"
ONEFILE_SCRIPT = ROOT / "scripts" / "rust" / "check_no_onefile_extract.py"
CHECK_SCRIPT = ROOT / "scripts" / "rust" / "check_p4_cases.py"


@pytest.mark.xfail(strict=True, reason="P4-T16 紅燈：bench_cli_roundtrip.py 尚未實作")
def test_bench_script_outputs_percentiles(tmp_path: Path) -> None:
    """對 stub CLI（python -c 印 {"ok":true} 並 exit 0）n=10 → JSON 含 p50_ms≤p95_ms≤p99_ms、samples_ms 長度 10、failed_samples==0。"""
    assert BENCH_SCRIPT.is_file(), f"量測工具不存在：{BENCH_SCRIPT}"
    stub_cli = tmp_path / "stub_cli.py"
    stub_cli.write_text("import sys\nprint('{\"ok\":true}')\nsys.exit(0)\n", encoding="utf-8")
    out_json = tmp_path / "bench_out.json"

    res = subprocess.run(
        [
            sys.executable,
            str(BENCH_SCRIPT),
            "--cli",
            f"{sys.executable} {stub_cli}",
            "--endpoint",
            "tcp://127.0.0.1:48700",
            "--n",
            "10",
            "--warmup",
            "1",
            "--platform",
            "linux",
            "--json-out",
            str(out_json),
        ],
        capture_output=True,
        text=True,
    )
    assert res.returncode == 0, res.stdout + res.stderr
    data = json.loads(out_json.read_text(encoding="utf-8"))
    assert data["p50_ms"] <= data["p95_ms"] <= data["p99_ms"]
    assert len(data["samples_ms"]) == 10
    assert data["failed_samples"] == 0


@pytest.mark.xfail(strict=True, reason="P4-T16 紅燈：bench_cli_roundtrip.py 尚未實作")
def test_bench_nearest_rank_quantiles() -> None:
    """注入固定樣本 1..100 ms → p50=50、p95=95、p99=99。"""
    assert BENCH_SCRIPT.is_file(), f"量測工具不存在：{BENCH_SCRIPT}"
    spec = importlib.util.spec_from_file_location("bench_cli_roundtrip", BENCH_SCRIPT)
    assert spec is not None and spec.loader is not None
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)

    samples = list(range(1, 101))
    q = mod.calculate_quantiles(samples)
    assert q["p50_ms"] == 50
    assert q["p95_ms"] == 95
    assert q["p99_ms"] == 99


@pytest.mark.xfail(strict=True, reason="P4-T16 紅燈：bench_cli_roundtrip.py 尚未實作")
def test_bench_failed_runs_not_counted(tmp_path: Path) -> None:
    """stub CLI 交替 exit 1 → failed_samples>0、ok:false、gate 不為 PASS。"""
    assert BENCH_SCRIPT.is_file(), f"量測工具不存在：{BENCH_SCRIPT}"
    cnt_file = tmp_path / "cnt.txt"
    stub_cli = tmp_path / "fail_stub.py"
    stub_cli.write_text(
        f"import sys, os\n"
        f"p = {repr(str(cnt_file))}\n"
        f"c = int(open(p).read()) if os.path.exists(p) else 0\n"
        f"open(p, 'w').write(str(c + 1))\n"
        f"if c % 2 == 1: sys.exit(1)\n"
        f"print('{{\"ok\":true}}')\n",
        encoding="utf-8",
    )
    out_json = tmp_path / "bench_fail_out.json"

    res = subprocess.run(
        [
            sys.executable,
            str(BENCH_SCRIPT),
            "--cli",
            f"{sys.executable} {stub_cli}",
            "--endpoint",
            "tcp://127.0.0.1:48700",
            "--n",
            "10",
            "--warmup",
            "1",
            "--platform",
            "linux",
            "--json-out",
            str(out_json),
        ],
        capture_output=True,
        text=True,
    )
    data = json.loads(out_json.read_text(encoding="utf-8"))
    assert data["failed_samples"] > 0
    assert data["ok"] is False
    assert data["gate"] != "PASS"


@pytest.mark.xfail(strict=True, reason="P4-T16 紅燈：bench_cli_roundtrip.py 尚未實作")
def test_bench_gate_thresholds() -> None:
    """注入 linux 樣本 p95=3.6 ms → gate:"FAIL"；windows p50=4.9／p95=9.9／p99=14.9 → gate:"PASS"（標準環境欄位齊備時）；欄位不齊 → NOT_STANDARD_ENV。"""
    assert BENCH_SCRIPT.is_file(), f"量測工具不存在：{BENCH_SCRIPT}"
    spec = importlib.util.spec_from_file_location("bench_cli_roundtrip", BENCH_SCRIPT)
    assert spec is not None and spec.loader is not None
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)

    res_linux = mod.evaluate_gate("linux", p50=1.5, p95=3.6, p99=4.8, env_valid=True)
    assert res_linux["gate"] == "FAIL"

    res_win_pass = mod.evaluate_gate("windows", p50=4.9, p95=9.9, p99=14.9, env_valid=True)
    assert res_win_pass["gate"] == "PASS"

    res_win_not_std = mod.evaluate_gate("windows", p50=4.9, p95=9.9, p99=14.9, env_valid=False)
    assert res_win_not_std["gate"] == "NOT_STANDARD_ENV"


@pytest.mark.xfail(strict=True, reason="P4-T16 紅燈：check_no_onefile_extract.py 尚未實作")
def test_no_onefile_checker_flags_new_dir(tmp_path: Path) -> None:
    """假 CLI 每次建立 _MEItest<i> → ok:false、leftover 長度 = runs。"""
    assert ONEFILE_SCRIPT.is_file(), f"單檔檢查工具不存在：{ONEFILE_SCRIPT}"
    fake_cli = tmp_path / "fake_onefile.py"
    fake_cli.write_text(
        "import os, sys, tempfile\n"
        "d = os.environ.get('TMPDIR') or tempfile.gettempdir()\n"
        "os.makedirs(os.path.join(d, '_MEItest' + str(os.getpid())), exist_ok=True)\n"
        "print('ok')\n",
        encoding="utf-8",
    )

    res = subprocess.run(
        [
            sys.executable,
            str(ONEFILE_SCRIPT),
            "--cli",
            f"{sys.executable} {fake_cli}",
            "--runs",
            "5",
        ],
        capture_output=True,
        text=True,
    )
    assert res.returncode == 0
    data = json.loads(res.stdout)
    assert data["ok"] is False
    assert len(data["leftover"]) == 5


@pytest.mark.xfail(strict=True, reason="P4-T16 紅燈：check_no_onefile_extract.py 尚未實作")
def test_no_onefile_checker_flags_create_then_delete(tmp_path: Path) -> None:
    """假 CLI 建立 x.exe 後立即刪除再退出 → ok:false、created 含該路徑。"""
    assert ONEFILE_SCRIPT.is_file(), f"單檔檢查工具不存在：{ONEFILE_SCRIPT}"
    fake_cli = tmp_path / "fake_delete.py"
    fake_cli.write_text(
        "import os, sys, tempfile\n"
        "d = os.environ.get('TMPDIR') or tempfile.gettempdir()\n"
        "p = os.path.join(d, 'x.exe')\n"
        "open(p, 'w').write('x')\n"
        "os.remove(p)\n"
        "print('ok')\n",
        encoding="utf-8",
    )

    res = subprocess.run(
        [
            sys.executable,
            str(ONEFILE_SCRIPT),
            "--cli",
            f"{sys.executable} {fake_cli}",
            "--runs",
            "1",
        ],
        capture_output=True,
        text=True,
    )
    assert res.returncode == 0
    data = json.loads(res.stdout)
    assert data["ok"] is False
    assert any("x.exe" in item["path"] for item in data.get("created", []))


@pytest.mark.xfail(strict=True, reason="P4-T16 紅燈：check_no_onefile_extract.py 尚未實作")
def test_no_onefile_checker_redirects_tmp(tmp_path: Path) -> None:
    """假 CLI 寫到 $TMPDIR/anything → 被偵測（名稱不限 _MEI*）。"""
    assert ONEFILE_SCRIPT.is_file(), f"單檔檢查工具不存在：{ONEFILE_SCRIPT}"
    fake_cli = tmp_path / "fake_anything.py"
    fake_cli.write_text(
        "import os, sys, tempfile\n"
        "d = os.environ.get('TMPDIR') or tempfile.gettempdir()\n"
        "p = os.path.join(d, 'custom_payload.bin')\n"
        "open(p, 'w').write('x')\n"
        "print('ok')\n",
        encoding="utf-8",
    )

    res = subprocess.run(
        [
            sys.executable,
            str(ONEFILE_SCRIPT),
            "--cli",
            f"{sys.executable} {fake_cli}",
            "--runs",
            "1",
        ],
        capture_output=True,
        text=True,
    )
    assert res.returncode == 0
    data = json.loads(res.stdout)
    assert data["ok"] is False
    assert any("custom_payload.bin" in item["path"] for item in data.get("created", []))


@pytest.mark.xfail(strict=True, reason="P4-T16 紅燈：l1_judge 與 Windows 原生 cargo 測試報告尚未就緒")
def test_cargo_workspace_tests_windows_native() -> None:
    """scripts/rust/win-test.sh --package sw-pal --package sw-cli 報告的 l1_results 以 l1_judge 判定為 PASS。"""
    judge_path = ROOT / "tests" / "acceptance" / "l1_judge.py"
    assert judge_path.is_file(), "tests/acceptance/l1_judge.py 尚未交付"

    spec = importlib.util.spec_from_file_location("l1_judge", judge_path)
    assert spec is not None and spec.loader is not None
    l1_judge = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(l1_judge)

    lock_path = ROOT / "tests" / "acceptance" / "l1_required.lock.json"
    assert lock_path.is_file(), "l1_required.lock.json 尚未就緒"


cargo_workspace_tests_windows_native = test_cargo_workspace_tests_windows_native


@pytest.mark.xfail(strict=True, reason="P4-T16 紅燈：check_p4_cases.py --l1-results 完整度閘門尚未實作")
def test_check_p4_cases_l1_completeness(tmp_path: Path) -> None:
    """暫存 l1_required.lock.json 的 s1b_20groups.linux 凍結 3 個 nodeid；check_p4_cases.py --check --l1-results <dir> 檢查完整度。"""
    l1_dir = tmp_path / "l1"
    l1_dir.mkdir(parents=True, exist_ok=True)

    res = subprocess.run(
        [
            sys.executable,
            str(CHECK_SCRIPT),
            "--check",
            "--l1-results",
            str(l1_dir),
        ],
        capture_output=True,
        text=True,
    )
    assert res.returncode == 1
    out = res.stdout + res.stderr
    assert "s1b_20groups" in out or "missing" in out
