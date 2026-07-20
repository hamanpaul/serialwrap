"""python3 -m realhw——實機穩定性套件入口。"""
from __future__ import annotations

import argparse
import datetime
import os
import subprocess
import sys
from pathlib import Path

from . import cases  # noqa: F401  # import 觸發 case 註冊
from . import drivers, harness, preflight


def main() -> int:
    ap = argparse.ArgumentParser(prog="realhw", description="serialwrap 實機穩定性套件（#122）")
    ap.add_argument("--tier", default="p0",
                    help="p0|p1|remote|longrun，逗號多選；remote/longrun 必須顯式指定")
    ap.add_argument("--only")
    ap.add_argument("--skip", default="")
    ap.add_argument("--duration", default="32h", help="longrun 時長（<N>h/<N>m/<N>s）")
    ap.add_argument("--report-dir")
    ap.add_argument("--list", action="store_true")
    args = ap.parse_args()

    cfg = harness.load_cfg()
    cfg["duration_s"] = harness.parse_duration(args.duration)
    tiers = [t.strip() for t in args.tier.split(",") if t.strip()]
    selected = harness.select_cases(harness.REGISTRY, tiers=tiers, only=args.only,
                                    skip=[s for s in args.skip.split(",") if s])
    if args.list:
        for c in harness.REGISTRY:
            mark = "⚡" if c.destructive else "  "
            print(f"{mark} [{c.tier}] {c.id}  {c.title}")
        return 0

    ts = datetime.datetime.now().strftime("%y%m%d-%H%M%S")
    report_dir = Path(args.report_dir or Path.home() / "b-log" / "realhw-reports" / ts)
    sw = drivers.SwCli()
    win = drivers.WinSwCli(cfg.get("win_serialwrap_exe", ""))
    ctx = harness.Ctx(cfg=cfg, report_dir=report_dir, case_dir=report_dir,
                      sw=sw, tmux=drivers.TmuxCtl(cfg["tmux_prefix"]),
                      usbipd=drivers.Usbipd(cfg["usbipd_exe"]), systemd=drivers.Systemd(),
                      win=win)

    bench_fd = preflight.acquire_benchlock(preflight.bench_lock_path())
    try:
        checks = preflight.collect(cfg, sw, Path(__file__).resolve().parent.parent,
                                   benchlock_ok=bench_fd is not None, win=win)
        ok, problems = preflight.evaluate(checks)
        for p in problems:
            print(f"[preflight] {p}")
        if not ok:
            print("[preflight] 拒跑：缺項如上")
            return 2
        caps = preflight.collect_capabilities(sw)
        missing_caps = preflight.missing_capabilities(caps)
        destructive = [c.id for c in selected if c.destructive]
        if destructive:
            print(f"[preflight] 本輪破壞性動作：{', '.join(destructive)}")
        print(f"[realhw] 報告目錄：{report_dir}")

        boards = [b["com"] for b in cfg["boards"]]
        version = sw.run("--version").get("_raw", "")
        git = subprocess.run(["git", "rev-parse", "--short", "HEAD"],
                             capture_output=True, text=True).stdout.strip()
        cfg["_version"] = version
        cfg["_git"] = git
        meta = {
            "version": version,
            "git": git,
            "tiers": args.tier, "started_at": ts, "preflight_notes": problems,
            "capabilities_missing": missing_caps,
            "win_daemon_present": checks.win_daemon_present,
            "win_daemon_holds": list(checks.win_daemon_holds),
        }
        results = harness.run_cases(selected, ctx, boards=boards, missing_caps=missing_caps)
        hints = {c.id: c.hints for c in selected}
        harness.write_reports(report_dir, meta, results, hints)
        print(f"[realhw] 完成：{report_dir}/report.md")
        return 1 if any(r.verdict == "FAIL" for _, r in results) else 0
    finally:
        if bench_fd is not None:
            os.close(bench_fd)


if __name__ == "__main__":
    sys.exit(main())
