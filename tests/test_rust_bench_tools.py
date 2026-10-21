"""Stage 1B 效能與打包自檢工具測試（P4-T16）。

涵蓋：
- scripts/rust/bench_cli_roundtrip.py：CLI 暖快取往返延遲量測、分位數、gate 判定與標準環境判定（WSL／容器／平台不符）。
- scripts/rust/check_no_onefile_extract.py：單檔暫存解壓行為檢查（正向對照與負向案例）。
- Windows 原生 L1 cargo 測試判定（唯一判定實作為 P2 l1_judge）。
- scripts/rust/check_p4_cases.py --l1-results 完整度閘門檢查（逐情境經 P2 l1_judge）。

阻塞說明：l1_judge.py／evidence_labels.py 位於 feature/rust-refactor，本分支尚未併入；相關測試以
`BLOCKED-PREREQ` 明確失敗，不以 xfail 或 skip 隱藏。
"""
from __future__ import annotations

import importlib.util
import json
import os
import subprocess
import sys
import tempfile
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




# --- P4-T16 修正（審查 #3）：標準環境判定、保留鍵與身分欄位 ---------------------------------

def _load_bench():
    spec = importlib.util.spec_from_file_location("bench_cli_roundtrip", BENCH_SCRIPT)
    assert spec is not None and spec.loader is not None
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def _full_manifest(path: Path, *, drop: tuple[str, ...] = ()) -> Path:
    """人工量測欄位（非工具自動量測者）；drop 用於製造缺欄。"""
    data = {
        "memory": "16 GiB",
        "filesystem": "ext4",
        "storage": "NVMe",
        "power_plan": "balanced",
        "virtualization": "none",
        "endpoint_av": "none",
        "contract_revision": "rev-test",
        "tool_revision": "tool-test",
        "serial_driver": "n/a",
    }
    for key in drop:
        data.pop(key, None)
    path.write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")
    return path


def _native_linux_host() -> dict:
    return {"host_platform": "linux", "wsl": False, "container": False}


def _wsl_host() -> dict:
    return {"host_platform": "linux", "wsl": True, "container": False}


def _windows_host() -> dict:
    return {"host_platform": "windows", "wsl": False, "container": False}


def _fake_measure_factory(samples: list[float]):
    def _measure(argv, endpoint, n, warmup):  # noqa: ARG001
        return {"samples_ms": list(samples), "failed_samples": 0, "attempts": len(samples), "warmup_failed": 0}

    return _measure


def _quantile_samples(p50: float, p95: float, p99: float) -> list[float]:
    """n=100 的樣本，使 nearest-rank 的 p50／p95／p99 恰為指定值。"""
    return [p50] * 50 + [p95] * 45 + [p99] * 5


def _run_bench(mod, tmp_path: Path, *, platform: str, manifest: Path, host: dict, monkeypatch, samples: list[float]) -> dict:
    stub = tmp_path / "cli_stub"
    stub.write_bytes(b"#!/bin/sh\necho '{\"ok\":true}'\n")
    daemon = tmp_path / "daemon_stub"
    daemon.write_bytes(b"daemon-artifact\n")
    monkeypatch.setattr(mod, "measure", _fake_measure_factory(samples))
    args = mod._parse_args(
        [
            "--cli", str(stub),
            "--endpoint", "tcp://127.0.0.1:48700",
            "--n", "100",
            "--warmup", "0",
            "--platform", platform,
            "--env-manifest", str(manifest),
            "--daemon-exe", str(daemon),
        ],
        host,
    )
    return mod.build_report(args, host=host)


def test_bench_wsl_host_is_never_standard_even_with_full_manifest(tmp_path: Path, monkeypatch) -> None:
    """WSL2 主機即使 manifest 欄位齊備、數值達標，也必須為 NOT_STANDARD_ENV（§1.1.1）。"""
    mod = _load_bench()
    manifest = _full_manifest(tmp_path / "m.json")
    report = _run_bench(
        mod, tmp_path, platform="linux", manifest=manifest, host=_wsl_host(), monkeypatch=monkeypatch,
        samples=_quantile_samples(1.0, 1.0, 1.0),
    )
    assert report["gate"] == "NOT_STANDARD_ENV", report["env"]["non_standard_reasons"]
    assert any("wsl2_host" in r for r in report["env"]["non_standard_reasons"])


def test_bench_platform_mismatch_is_not_standard(tmp_path: Path, monkeypatch) -> None:
    """Linux 主機以 --platform windows 量測，即使數值達 Windows 門檻也不得 PASS。"""
    mod = _load_bench()
    manifest = _full_manifest(tmp_path / "m.json")
    report = _run_bench(
        mod, tmp_path, platform="windows", manifest=manifest, host=_native_linux_host(), monkeypatch=monkeypatch,
        samples=_quantile_samples(4.9, 9.9, 14.9),
    )
    assert report["gate"] == "NOT_STANDARD_ENV"
    assert any("platform_mismatch" in r for r in report["env"]["non_standard_reasons"])


def test_bench_native_host_with_full_manifest_passes(tmp_path: Path, monkeypatch) -> None:
    """正向對照：原生 Linux、欄位齊備、數值達標 → PASS；原生 Windows 同理。"""
    mod = _load_bench()
    manifest = _full_manifest(tmp_path / "m.json")
    linux = _run_bench(
        mod, tmp_path, platform="linux", manifest=manifest, host=_native_linux_host(), monkeypatch=monkeypatch,
        samples=_quantile_samples(1.5, 3.4, 4.9),
    )
    assert linux["gate"] == "PASS", linux["env"]
    windows = _run_bench(
        mod, tmp_path, platform="windows", manifest=manifest, host=_windows_host(), monkeypatch=monkeypatch,
        samples=_quantile_samples(4.9, 9.9, 14.9),
    )
    assert windows["gate"] == "PASS", windows["env"]


def test_bench_missing_daemon_artifact_is_not_standard(tmp_path: Path, monkeypatch) -> None:
    """未提供 --daemon-exe 且 manifest 亦無 daemon_sha256 → NOT_STANDARD_ENV（缺 daemon artifact 身分）。"""
    mod = _load_bench()
    stub = tmp_path / "cli_stub"
    stub.write_bytes(b"x")
    monkeypatch.setattr(mod, "measure", _fake_measure_factory(_quantile_samples(1.0, 1.0, 1.0)))
    manifest = _full_manifest(tmp_path / "m.json")
    args = mod._parse_args(
        ["--cli", str(stub), "--endpoint", "tcp://127.0.0.1:48700", "--n", "100", "--warmup", "0",
         "--platform", "linux", "--env-manifest", str(manifest)],
        _native_linux_host(),
    )
    report = mod.build_report(args, host=_native_linux_host())
    assert report["gate"] == "NOT_STANDARD_ENV"
    assert "daemon_sha256" in report["env"]["missing_fields"]


def test_bench_manifest_cannot_override_measured_identity(tmp_path: Path) -> None:
    """--env-manifest 覆寫工具自動量測的保留鍵（例如 wsl、cli_sha256）→ exit 2，不產生報告。"""
    manifest = _full_manifest(tmp_path / "m.json")
    data = json.loads(manifest.read_text(encoding="utf-8"))
    data["wsl"] = False
    data["cli_sha256"] = "0" * 64
    manifest.write_text(json.dumps(data), encoding="utf-8")
    stub = tmp_path / "stub.py"
    stub.write_text("import sys\nprint('{\"ok\":true}')\n", encoding="utf-8")
    res = subprocess.run(
        [
            sys.executable, str(BENCH_SCRIPT),
            "--cli", f"{sys.executable} {stub}",
            "--endpoint", "tcp://127.0.0.1:48700",
            "--n", "1", "--warmup", "0",
            "--platform", "linux",
            "--env-manifest", str(manifest),
        ],
        capture_output=True,
        text=True,
    )
    assert res.returncode == 2, res.stdout + res.stderr
    assert "保留鍵" in res.stderr


# --- P4-T16 修正（審查 #5、#7）：單檔解壓檢查的正向對照與負向案例 ------------------------------

def _run_onefile(tmp_path: Path, cli_text: str, *, runs: int, private: bool) -> dict:
    fake_cli = tmp_path / f"fake_{abs(hash(cli_text)) % 10**8}.py"
    fake_cli.write_text(cli_text, encoding="utf-8")
    work = tmp_path / "work"
    work.mkdir(exist_ok=True)
    cmd = [
        sys.executable, str(ONEFILE_SCRIPT),
        "--cli", f"{sys.executable} {fake_cli}",
        "--cli-args", "",
        "--runs", str(runs),
        "--work-base", str(work),
    ]
    env = dict(os.environ)
    if private:
        sys_root = tmp_path / "sys"
        sys_root.mkdir(exist_ok=True)
        cmd += ["--system-tmp", str(sys_root)]
    else:
        # 故意寫入 runner 未宣告獨占的共享系統暫存根，驗證事件只能判為證據不足。
        sys_root = Path(tempfile.gettempdir())
    env["SYS_ROOT"] = str(sys_root)
    res = subprocess.run(cmd, capture_output=True, text=True, env=env)
    assert res.returncode == 0, res.stdout + res.stderr
    return json.loads(res.stdout)


def test_no_onefile_clean_cli_passes_on_private_system_tmp(tmp_path: Path) -> None:
    """正向對照：乾淨 CLI 在獨占系統暫存根下 → verdict PASS、ok true（避免永遠 ok:false 的假紅）。"""
    data = _run_onefile(tmp_path, "print('ok')\n", runs=2, private=True)
    assert data["verdict"] == "PASS", data
    assert data["ok"] is True
    assert data["created"] == [] and data["leftover"] == [] and data["system_tmp_new"] == []


def test_no_onefile_nested_write_in_private_system_tmp_fails(tmp_path: Path) -> None:
    """負向：CLI 在既有系統暫存子目錄內建立並刪除檔案 → FAIL，且事件路徑含該子目錄（審查 #7）。"""
    cli_text = (
        "import os\n"
        "d = os.path.join(os.environ['SYS_ROOT'], 'existing')\n"
        "os.makedirs(d, exist_ok=True)\n"
        "p = os.path.join(d, 'payload.bin')\n"
        "open(p, 'w').write('x')\n"
        "os.remove(p)\n"
    )
    (tmp_path / "sys" / "existing").mkdir(parents=True, exist_ok=True)  # 既有子目錄（非本次執行新建）
    data = _run_onefile(tmp_path, cli_text, runs=1, private=True)
    assert data["verdict"] == "FAIL", data
    assert any("payload.bin" in row["path"] for row in data["created"]), data["created"]


def test_no_onefile_shared_system_tmp_is_insufficient_not_pass(tmp_path: Path) -> None:
    """負向：共用系統暫存根的他人寫入無法歸屬 → INSUFFICIENT（ok false），不得判 PASS（審查 #5）。"""
    probe = f"sw-p4t16-probe-{os.getpid()}.bin"
    cli_text = (
        "import os, tempfile\n"
        f"p = os.path.join('/tmp' if os.name == 'posix' else tempfile.gettempdir(), {probe!r})\n"
        "open(p, 'w').write('x')\n"
        "os.remove(p)\n"
    )
    sys_root = Path(tempfile.gettempdir())
    try:
        data = _run_onefile(tmp_path, cli_text, runs=1, private=False)
    finally:
        for candidate in (sys_root / probe, Path("/tmp") / probe, Path(os.environ.get("TEMP", "")) / probe):
            if candidate.name and candidate.exists():
                candidate.unlink()
    assert data["verdict"] == "INSUFFICIENT", data
    assert data["ok"] is False
    assert data["system_tmp_unattributed"], data


# --- P4-T16 修正（審查 #4b、#6）：Windows 原生 L1 cargo 與完整度，唯一判定實作為 P2 l1_judge --------------

L1_JUDGE_PATH = ROOT / "tests" / "acceptance" / "l1_judge.py"
L1_LOCK_PATH = ROOT / "tests" / "acceptance" / "l1_required.lock.json"
EVIDENCE_L1_DIR = ROOT / "target" / "p4-evidence" / "l1"


def _require_l1_judge():
    if not L1_JUDGE_PATH.is_file():
        pytest.fail("BLOCKED-PREREQ：tests/acceptance/l1_judge.py 不存在（需併入 feature/rust-refactor；P2 為唯一 L1 判定實作）")
    spec = importlib.util.spec_from_file_location("l1_judge", L1_JUDGE_PATH)
    assert spec is not None and spec.loader is not None
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def test_cargo_workspace_tests_windows_native() -> None:
    """Windows 原生 cargo L1 報告必須以 P2 judge 判為 PASS（照計畫 L3538，不以「檔案存在」代替判定）。"""
    judge = _require_l1_judge()
    assert L1_LOCK_PATH.is_file(), "l1_required.lock.json 尚未就緒"
    lock = json.loads(L1_LOCK_PATH.read_text(encoding="utf-8"))
    report_path = EVIDENCE_L1_DIR / "windows" / "cargo" / "resource-report.json"
    assert report_path.is_file(), f"缺少 Windows 原生 cargo L1 報告（證據不足）：{report_path}"
    report = json.loads(report_path.read_text(encoding="utf-8"))
    result = judge.judge(
        report,
        judge.required_for(lock, "s1b_cargo", "windows"),
        platform="windows",
        junit=None,
        selector=lock["sets"]["s1b_cargo"]["windows"].get("selector"),
    )
    assert result["verdict"] == "PASS", result["reasons"]


cargo_workspace_tests_windows_native = test_cargo_workspace_tests_windows_native


def _load_check_module():
    spec = importlib.util.spec_from_file_location("check_p4_cases", CHECK_SCRIPT)
    assert spec is not None and spec.loader is not None
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def _valid_l1_report(platform: str, argv: list[str], nodeids: list[str]) -> dict:
    """符合 P2 l1-results.v1 結構的合成報告（argv[0] 為 pytest 入口，與 selector 之其餘參數相接）。"""
    rows = [
        {"nodeid": nid, "outcome": "passed", "skip_kind": None,
         "phases": {"setup": "passed", "call": "passed", "teardown": "passed"}}
        for nid in nodeids
    ]
    n = len(nodeids)
    tally = {"passed": n, "failed": 0, "skipped": 0}
    return {
        "schema_version": 2,
        "format": "l1-results.v1",
        "runner": "pytest",
        "platform": platform,
        "argv": ["pytest-entry", *argv],
        "markexpr": "",
        "counts": {"all": dict(tally)},
        "resources": {},
        "not_collected_platform": [],
        "core_portable_platform_skips": [],
        "collected": list(nodeids),
        "deselected": [],
        "results": rows,
        "exit_code": 0,
        "finished": True,
        "deployed_content": None,
    }


def _write_l1_fixture(root: Path) -> Path:
    """合成四個 P4 L1 集合（兩集合×兩平台）的鎖與報告；20groups 另寫 junit.xml（與報告交叉核對）。"""
    sets: dict[str, dict[str, dict]] = {}
    for set_name in ("s1b_20groups", "s1b_cargo"):
        sets[set_name] = {}
        for plat in ("linux", "windows"):
            sets[set_name][plat] = {
                "selector": {"argv": [set_name, plat], "paths": [], "markexpr": ""},
                "nodeids": [f"tests/x.py::{set_name}_{plat}_{i}" for i in range(3)],
            }
    lock_path = root / "lock.json"
    lock_path.write_text(json.dumps({"schema_version": 1, "sets": sets}), encoding="utf-8")
    subdir = {"s1b_20groups": "20groups", "s1b_cargo": "cargo"}
    for set_name, sub in subdir.items():
        for plat in ("linux", "windows"):
            entry = sets[set_name][plat]
            report = _valid_l1_report(plat, entry["selector"]["argv"], entry["nodeids"])
            base = root / "l1" / plat / sub
            base.mkdir(parents=True, exist_ok=True)
            (base / "resource-report.json").write_text(json.dumps(report), encoding="utf-8")
            if set_name == "s1b_20groups":
                n = len(entry["nodeids"])
                (base / "junit.xml").write_text(
                    f'<?xml version="1.0"?><testsuites><testsuite name="pytest" tests="{n}" failures="0" '
                    f'errors="0" skipped="0"/></testsuites>',
                    encoding="utf-8",
                )
    return lock_path


def test_check_l1_completeness_rules(tmp_path: Path) -> None:
    """R5-F3：正例全數 PASS；缺列／skipped／duplicate／exit_code／selector／finished 各變體 verdict 皆非 PASS（經 P2 judge）。"""
    _require_l1_judge()
    mod = _load_check_module()
    lock_path = _write_l1_fixture(tmp_path)
    l1_dir = tmp_path / "l1"
    target = l1_dir / "linux" / "20groups" / "resource-report.json"
    lock = json.loads(lock_path.read_text(encoding="utf-8"))
    frozen = lock["sets"]["s1b_20groups"]["linux"]["nodeids"]

    def verdicts() -> dict[tuple[str, str], str]:
        return {(r["set"], r["platform"]): r["verdict"] for r in mod.check_l1(l1_dir, lock_path)}

    assert all(v == "PASS" for v in verdicts().values()), verdicts()

    def mutate(fn) -> str:
        original = target.read_text(encoding="utf-8")
        data = json.loads(original)
        fn(data)
        target.write_text(json.dumps(data), encoding="utf-8")
        try:
            return verdicts()[("s1b_20groups", "linux")]
        finally:
            target.write_text(original, encoding="utf-8")

    def drop_one(d: dict) -> None:
        d["results"] = d["results"][:-1]
        d["collected"] = d["collected"][:-1]

    def skip_one(d: dict) -> None:
        d["results"][0]["outcome"] = "skipped"
        d["results"][0]["skip_kind"] = "other"

    def dup_one(d: dict) -> None:
        d["results"].append(dict(d["results"][0]))

    def bad_exit(d: dict) -> None:
        d["exit_code"] = 1

    def bad_argv(d: dict) -> None:
        d["argv"] = ["pytest-entry", "other"]

    def unfinished(d: dict) -> None:
        d["finished"] = False

    for label, fn in {
        "missing": drop_one,
        "skipped_required": skip_one,
        "duplicate": dup_one,
        "exit_code": bad_exit,
        "selector mismatch": bad_argv,
        "finished": unfinished,
    }.items():
        verdict = mutate(fn)
        assert verdict != "PASS", f"{label} 應判為非 PASS（證據不足／失敗），實得 {verdict}"
    assert frozen  # 凍結集合非空，確保變體確實作用於必要節點


def test_check_p4_cases_l1_completeness(tmp_path: Path) -> None:
    """CLI 逐情境：空 L1 目錄 → exit 1，stdout 同時含 set 名稱與 missing／INSUFFICIENT 字樣（審查 #4d）。"""
    _require_l1_judge()
    lock_path = _write_l1_fixture(tmp_path)
    l1_dir = tmp_path / "empty-l1"
    l1_dir.mkdir()
    res = subprocess.run(
        [
            sys.executable, str(CHECK_SCRIPT),
            "--check",
            "--evidence-dir", str(tmp_path / "ev"),
            "--l1-results", str(l1_dir),
            "--l1-lock", str(lock_path),
            "--summary-out", str(tmp_path / "summary.json"),
        ],
        capture_output=True,
        text=True,
    )
    assert res.returncode == 1, res.stdout + res.stderr
    assert "s1b_20groups" in res.stdout
    assert "INSUFFICIENT" in res.stdout or "missing" in res.stdout


@pytest.mark.parametrize("scenario", ["missing_one", "exit_code_nonzero"])
def test_check_p4_cases_l1_cli_scenarios(tmp_path: Path, scenario: str) -> None:
    """CLI 逐情境接線（審查 #4d）：單一必要節點缺列、runner exit_code≠0 → exit 1，stdout 指出 s1b_20groups。"""
    _require_l1_judge()
    lock_path = _write_l1_fixture(tmp_path)
    l1_dir = tmp_path / "l1"
    target = l1_dir / "linux" / "20groups" / "resource-report.json"
    data = json.loads(target.read_text(encoding="utf-8"))
    if scenario == "missing_one":
        data["results"] = data["results"][:-1]
        data["collected"] = data["collected"][:-1]
    else:
        data["exit_code"] = 1
    target.write_text(json.dumps(data), encoding="utf-8")
    res = subprocess.run(
        [
            sys.executable, str(CHECK_SCRIPT),
            "--check",
            "--evidence-dir", str(tmp_path / "ev"),
            "--l1-results", str(l1_dir),
            "--l1-lock", str(lock_path),
            "--summary-out", str(tmp_path / "summary.json"),
        ],
        capture_output=True,
        text=True,
    )
    assert res.returncode == 1, res.stdout + res.stderr
    assert "L1 s1b_20groups linux" in res.stdout, res.stdout
