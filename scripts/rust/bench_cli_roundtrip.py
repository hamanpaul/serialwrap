#!/usr/bin/env python3
"""CLI 暖快取往返延遲量測與 §1.2 效能 gate 判定（P4-T16，stdlib-only）。

固定程序（規格 §1.2「CLI 暖快取往返」，daemon 已運行、200 次 `daemon status`）：
- 先執行 `--warmup` 次（不計入樣本）。
- 再收集 `--n` 個「有效樣本」：每次以 subprocess 啟動 `<cli> --endpoint <ep> daemon status`，
  計時邊界為 Popen 呼叫前 → wait() 返回後（time.perf_counter_ns）。
- 有效樣本＝子行程 exit 0 且 stdout 為 `ok:true` 的 JSON。其餘記為 failed_samples，不計入分位數。
- failed_samples > 0 → ok:false，gate 為 MEASUREMENT_FAILED（量測失敗，不輸出 PASS）。
- 分位數採 nearest-rank：rank = ceil(p/100 × n)（1-based），以整數運算實作避免浮點誤差。
- gate（§1.2）：linux ≤ 2／3.5／5 ms，windows ≤ 5／10／15 ms（p50／p95／p99）；任一超標 → FAIL。

標準量測環境（§1.1.1）由工具自行判定，不採信 manifest 的宣稱：
- 主機平台必須與 `--platform` 相符（預設即取主機平台）；不符 → NOT_STANDARD_ENV。
- Linux 主機若為 WSL（/proc/version 含 microsoft、或 WSL_DISTRO_NAME／WSL_INTEROP）→ NOT_STANDARD_ENV（僅作開發參考）。
- 容器內（/.dockerenv 或 cgroup 含 docker／containerd／kubepods／lxc）→ NOT_STANDARD_ENV。
- 必填 manifest 欄位（含 daemon_sha256）缺漏 → NOT_STANDARD_ENV。
- `--env-manifest` 不得覆寫工具自動量測的保留鍵（遇到即 exit 2）。

`--cli-only-stub`：對立即回覆的替身端點（P3 bb_stubd）量測，只作 CLI 歸因參考，gate 為 REFERENCE_ONLY。

退出碼：報告成功寫出即 0（判定以 JSON 的 `ok` 與 `gate` 為準）；用法或工具錯誤為 2。
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import platform
import shlex
import subprocess
import sys
import time
from pathlib import Path
from typing import Any, Mapping, Sequence

# (p50, p95, p99) 上限，單位 ms（計畫 Task 16 Produces；規格 §1.2）。
THRESHOLDS_MS: dict[str, tuple[float, float, float]] = {
    "linux": (2.0, 3.5, 5.0),
    "windows": (5.0, 10.0, 15.0),
}
# §1.1.1 量測 manifest 必填欄位；缺任一即 NOT_STANDARD_ENV。daemon_sha256 為 CLI／daemon artifact 身分。
REQUIRED_ENV_KEYS: tuple[str, ...] = (
    "os_build",
    "cpu",
    "memory",
    "filesystem",
    "storage",
    "power_plan",
    "virtualization",
    "endpoint_av",
    "cli_sha256",
    "daemon_sha256",
    "contract_revision",
    "tool_revision",
    "timing_method",
    "serial_driver",
)
# 由工具自行量測的鍵：`--env-manifest` 不得覆寫（遇到即 exit 2）。daemon_sha256 僅於提供 --daemon-exe 時屬於此類。
AUTO_ENV_KEYS: frozenset[str] = frozenset(
    {
        "platform",
        "host_platform",
        "wsl",
        "container",
        "os_build",
        "cpu",
        "cli_sha256",
        "timing_method",
        "endpoint",
        "python",
        "host",
        "non_standard_reasons",
        "missing_fields",
    }
)
TIMING_METHOD = "time.perf_counter_ns；Popen 呼叫前 → wait() 返回後"
PLATFORMS = ("linux", "windows")
CONTAINER_CGROUP_TOKENS = ("docker", "containerd", "kubepods", "lxc")


def calculate_quantiles(samples: Sequence[float]) -> dict[str, float]:
    """以 nearest-rank 法計算 p50／p95／p99（rank = ceil(p·n/100)，1-based）。"""
    if not samples:
        raise ValueError("樣本為空，無法計算分位數")
    ordered = sorted(samples)
    n = len(ordered)
    out: dict[str, float] = {}
    for p in (50, 95, 99):
        rank = (p * n + 99) // 100  # 整數 ceil(p·n/100)
        rank = max(1, min(n, rank))
        out[f"p{p}_ms"] = ordered[rank - 1]
    return out


def evaluate_gate(
    platform_name: str,
    *,
    p50: float | None,
    p95: float | None,
    p99: float | None,
    env_valid: bool,
    failed_samples: int = 0,
) -> dict[str, Any]:
    """回傳 `{"gate": ..., "thresholds_ms": {...}}`；gate 為 PASS／FAIL／NOT_STANDARD_ENV／MEASUREMENT_FAILED。"""
    if platform_name not in THRESHOLDS_MS:
        raise ValueError(f"未知平台：{platform_name!r}")
    lim50, lim95, lim99 = THRESHOLDS_MS[platform_name]
    thresholds = {"p50_ms": lim50, "p95_ms": lim95, "p99_ms": lim99}
    if failed_samples > 0 or p50 is None or p95 is None or p99 is None:
        gate = "MEASUREMENT_FAILED"
    elif not env_valid:
        gate = "NOT_STANDARD_ENV"
    elif p50 <= lim50 and p95 <= lim95 and p99 <= lim99:
        gate = "PASS"
    else:
        gate = "FAIL"
    return {"gate": gate, "thresholds_ms": thresholds}


def _read_text(path: Path) -> str:
    try:
        return path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return ""


def detect_host(
    *,
    sys_platform: str | None = None,
    environ: Mapping[str, str] | None = None,
    proc_version: Path = Path("/proc/version"),
    dockerenv: Path = Path("/.dockerenv"),
    cgroup: Path = Path("/proc/1/cgroup"),
) -> dict[str, Any]:
    """自動偵測主機平台、WSL 與容器（不採信 manifest）。參數可注入，供測試模擬不同主機。"""
    sp = sys.platform if sys_platform is None else sys_platform
    env = os.environ if environ is None else environ
    if sp.startswith("win"):
        host_platform = "windows"
    elif sp.startswith("linux"):
        host_platform = "linux"
    else:
        host_platform = sp
    wsl = False
    container = False
    if host_platform == "linux":
        version = _read_text(proc_version).lower()
        wsl = "microsoft" in version or bool(env.get("WSL_DISTRO_NAME")) or bool(env.get("WSL_INTEROP"))
        cgroup_text = _read_text(cgroup).lower()
        container = dockerenv.exists() or any(tok in cgroup_text for tok in CONTAINER_CGROUP_TOKENS)
    return {"host_platform": host_platform, "wsl": bool(wsl), "container": bool(container)}


def environment_problems(host: Mapping[str, Any], platform_name: str) -> list[str]:
    """§1.1.1 標準量測環境的硬性條件；任一不成立即 NOT_STANDARD_ENV。"""
    problems: list[str] = []
    if platform_name != host.get("host_platform"):
        problems.append(f"platform_mismatch：--platform={platform_name} 與主機 {host.get('host_platform')} 不符")
    if host.get("wsl"):
        problems.append("wsl2_host：WSL2 不是 §1.1.1 標準量測環境（僅作開發參考）")
    if host.get("container"):
        problems.append("container_host：容器內量測不是 §1.1.1 標準量測環境")
    return problems


def _split_cli(cli: str) -> list[str]:
    """`--cli` 可為單一執行檔路徑，或含參數的命令字串（例如 `python3 stub.py`）。"""
    if Path(cli).is_file():
        return [cli]
    if os.name != "nt":
        return shlex.split(cli)
    words = []
    for tok in shlex.split(cli, posix=False):
        if len(tok) >= 2 and tok[0] == tok[-1] and tok[0] in "\"'":
            tok = tok[1:-1]
        words.append(tok)
    return words


def _sha256_file(path: str) -> str | None:
    p = Path(path)
    if not p.is_file():
        return None
    return hashlib.sha256(p.read_bytes()).hexdigest()


def _is_ok_json(stdout: bytes) -> bool:
    try:
        data = json.loads(stdout.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError):
        return False
    return isinstance(data, dict) and data.get("ok") is True


def _one_call(argv: list[str], endpoint: str) -> tuple[bool, float]:
    """執行一次 `<cli> --endpoint <ep> daemon status`，回傳（是否有效, 耗時 ms）。"""
    cmd = argv + ["--endpoint", endpoint, "daemon", "status"]
    t0 = time.perf_counter_ns()
    proc = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
    out, _err = proc.communicate()
    rc = proc.wait()
    t1 = time.perf_counter_ns()
    elapsed_ms = (t1 - t0) / 1_000_000
    return (rc == 0 and _is_ok_json(out)), elapsed_ms


def collect_env(
    argv: list[str],
    platform_name: str,
    manifest_path: str | None,
    endpoint: str,
    *,
    host: Mapping[str, Any] | None = None,
    daemon_exe: str | None = None,
) -> dict[str, Any]:
    """組出 §1.1.1 量測 manifest：自動量測欄位＋`--env-manifest` 提供的人工欄位。"""
    host = detect_host() if host is None else host
    env: dict[str, Any] = {
        "platform": platform_name,
        "host_platform": host.get("host_platform"),
        "wsl": bool(host.get("wsl")),
        "container": bool(host.get("container")),
        "os_build": platform.platform(),
        "cpu": f"{platform.machine()} x{os.cpu_count() or 0}",
        "cli_sha256": _sha256_file(argv[0]),
        "timing_method": TIMING_METHOD,
        "endpoint": endpoint,
        "python": sys.version.split()[0],
    }
    if daemon_exe:
        env["daemon_sha256"] = _sha256_file(daemon_exe)
    if manifest_path:
        extra = json.loads(Path(manifest_path).read_text(encoding="utf-8"))
        if not isinstance(extra, dict):
            raise ValueError("--env-manifest 必須是 JSON 物件")
        for key, value in extra.items():
            if key in AUTO_ENV_KEYS or (key == "daemon_sha256" and "daemon_sha256" in env):
                raise ValueError(f"--env-manifest 不得覆寫自動量測的保留鍵：{key}")
            if value not in (None, ""):
                env[key] = value
    env["non_standard_reasons"] = environment_problems(host, platform_name)
    env["missing_fields"] = [k for k in REQUIRED_ENV_KEYS if not env.get(k)]
    return env


def measure(
    argv: list[str],
    endpoint: str,
    n: int,
    warmup: int,
) -> dict[str, Any]:
    """執行 warmup 與量測，回傳樣本與失敗計數（不含 gate 判定）。"""
    warmup_failed = 0
    for _ in range(warmup):
        ok, _ms = _one_call(argv, endpoint)
        if not ok:
            warmup_failed += 1

    samples: list[float] = []
    failed = 0
    max_attempts = max(3 * n, n + 10)
    attempts = 0
    while len(samples) < n and attempts < max_attempts:
        attempts += 1
        ok, ms = _one_call(argv, endpoint)
        if ok:
            samples.append(ms)
        else:
            failed += 1
    return {
        "samples_ms": samples,
        "failed_samples": failed,
        "attempts": attempts,
        "warmup_failed": warmup_failed,
    }


def build_report(args: argparse.Namespace, *, host: Mapping[str, Any] | None = None) -> dict[str, Any]:
    argv = _split_cli(args.cli)
    raw = measure(argv, args.endpoint, args.n, args.warmup)
    samples = raw["samples_ms"]
    failed = raw["failed_samples"]
    env = collect_env(
        argv,
        args.platform,
        args.env_manifest,
        args.endpoint,
        host=host,
        daemon_exe=getattr(args, "daemon_exe", None),
    )
    env_valid = not env["missing_fields"] and not env["non_standard_reasons"]

    if samples:
        quant = calculate_quantiles(samples)
        p50, p95, p99 = quant["p50_ms"], quant["p95_ms"], quant["p99_ms"]
        p50, p95, p99 = (round(p50, 6), round(p95, 6), round(p99, 6))
    else:
        p50 = p95 = p99 = None

    if args.cli_only_stub:
        gate = "REFERENCE_ONLY"
    else:
        gate = evaluate_gate(
            args.platform,
            p50=p50,
            p95=p95,
            p99=p99,
            env_valid=env_valid,
            failed_samples=failed,
        )["gate"]

    # 嘗試次數耗盡仍不足 n 個有效樣本時，必有失敗樣本（attempts ≥ n），故 failed>0 涵蓋所有量測失敗。
    ok = failed == 0 and len(samples) == args.n
    report: dict[str, Any] = {
        "cli": args.cli,
        "n": args.n,
        "warmup": args.warmup,
        "platform": args.platform,
        "p50_ms": p50,
        "p95_ms": p95,
        "p99_ms": p99,
        "samples_ms": [round(s, 6) for s in samples],
        "failed_samples": failed,
        "attempts": raw["attempts"],
        "warmup_failed": raw["warmup_failed"],
        "gate": gate,
        "ok": ok,
        "cli_only_stub": bool(args.cli_only_stub),
        "env": env,
    }
    if not ok:
        report["error"] = "failed_samples" if failed else "insufficient_valid_samples"
    return report


def _parse_args(argv: Sequence[str] | None, host: Mapping[str, Any] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="CLI 暖快取往返延遲量測（P4-T16）")
    parser.add_argument("--cli", required=True, help="CLI 執行檔路徑，或含參數的命令字串")
    parser.add_argument("--endpoint", required=True, help="daemon endpoint（例如 tcp://127.0.0.1:48700）")
    parser.add_argument("--n", type=int, default=200, help="有效樣本數（預設 200）")
    parser.add_argument("--warmup", type=int, default=20, help="暖機次數，不計入樣本（預設 20）")
    parser.add_argument(
        "--platform",
        choices=PLATFORMS,
        default=None,
        help="gate 門檻所屬平台（預設取主機平台；與主機不符即 NOT_STANDARD_ENV）",
    )
    parser.add_argument("--json-out", help="報告 JSON 輸出路徑")
    parser.add_argument("--env-manifest", help="§1.1.1 人工量測欄位（JSON 物件）；不得含工具自動量測的保留鍵")
    parser.add_argument("--daemon-exe", help="daemon 執行檔路徑；由工具計算 daemon_sha256（CLI／daemon artifact 身分）")
    parser.add_argument(
        "--cli-only-stub",
        action="store_true",
        help="對 bb_stubd 等立即回覆的替身端點量測，只作歸因參考（gate 為 REFERENCE_ONLY）",
    )
    args = parser.parse_args(list(argv) if argv is not None else None)
    if args.n < 1 or args.warmup < 0:
        parser.error("--n 須 ≥1、--warmup 須 ≥0")
    if args.platform is None:
        host_platform = (host or detect_host())["host_platform"]
        if host_platform not in PLATFORMS:
            parser.error(f"無法判定主機平台（{host_platform!r}），請明示 --platform")
        args.platform = host_platform
    return args


def main(argv: Sequence[str] | None = None) -> int:
    try:
        host = detect_host()
        args = _parse_args(argv, host)
        report = build_report(args, host=host)
    except SystemExit:
        raise
    except (OSError, ValueError, json.JSONDecodeError) as exc:
        print(f"bench_cli_roundtrip: {exc}", file=sys.stderr)
        return 2
    text = json.dumps(report, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    if args.json_out:
        Path(args.json_out).parent.mkdir(parents=True, exist_ok=True)
        Path(args.json_out).write_text(text + "\n", encoding="utf-8")
    # 強制 UTF-8 輸出：Windows 主控台預設 cp950 無法表示部分字元，且報告含繁中說明。
    sys.stdout.buffer.write((text + "\n").encode("utf-8"))
    sys.stdout.flush()
    return 0


if __name__ == "__main__":
    sys.exit(main())
