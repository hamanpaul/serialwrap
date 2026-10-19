"""python3 -m realhw——實機穩定性套件入口。"""
from __future__ import annotations

import argparse
import datetime
import dataclasses
import hashlib
import os
import subprocess
import sys
from pathlib import Path

from . import cases  # noqa: F401  # import 觸發 case 註冊
from . import deployed_content, drivers, frontends, harness, preflight


def _sha256_or_none(path: Path) -> str | None:
    try:
        return hashlib.sha256(path.read_bytes()).hexdigest()
    except OSError:
        return None


def _contract_revision(repo_root: Path) -> str | None:
    try:
        result = subprocess.run(["git", "-C", str(repo_root), "rev-parse", "HEAD:contracts"],
                                capture_output=True, text=True)
    except (OSError, subprocess.SubprocessError):
        return None
    value = (result.stdout or "").strip()
    return value if len(value) == 40 and all(c in "0123456789abcdef" for c in value) else None


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="realhw", description="serialwrap 實機穩定性套件（#122）")
    ap.add_argument("--tier", default="p0",
                    help="p0|p1|remote|longrun，逗號多選；remote/longrun 必須顯式指定")
    ap.add_argument("--only")
    ap.add_argument("--skip", default="")
    ap.add_argument("--duration", default="32h", help="longrun 時長（<N>h/<N>m/<N>s）")
    ap.add_argument("--report-dir")
    ap.add_argument("--list", action="store_true")
    args = ap.parse_args(argv)

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
    resolved, exe_identity = preflight.resolve_serialwrap_exe(cfg)
    sw = drivers.SwCli(exe=resolved)
    win = drivers.WinSwCli(cfg.get("win_serialwrap_exe", ""))
    tmux = drivers.TmuxCtl(cfg["tmux_prefix"])
    human_factory = frontends.make_frontend_factory(cfg, tmux=tmux, sw=sw)
    ctx = harness.Ctx(cfg=cfg, report_dir=report_dir, case_dir=report_dir,
                      sw=sw, tmux=tmux,
                      usbipd=drivers.Usbipd(cfg["usbipd_exe"]), systemd=drivers.Systemd(),
                      win=win, human=human_factory)

    bench_fd = preflight.acquire_benchlock(preflight.bench_lock_path(cfg))
    try:
        cli_version = str(sw.run("--version").get("_raw", ""))
        daemon_status = sw.run("daemon", "status")
        daemon_version = str(daemon_status.get("version") or "")
        version_problem = preflight.version_gate(cli_version, daemon_version)
        boards = [b["com"] for b in cfg["boards"]]
        cross_os_guard, cross_os_detail = preflight.cross_os_holder_check(cfg, boards)
        checks = preflight.collect(cfg, sw, Path(__file__).resolve().parent.parent,
                                   benchlock_ok=bench_fd is not None, win=win)
        checks = dataclasses.replace(
            checks,
            version_problem=version_problem,
            exe_identity=exe_identity,
            cross_os_guard=cross_os_guard,
            cross_os_detail=cross_os_detail,
        )
        ok, problems = preflight.evaluate(checks)
        for p in problems:
            print(f"[preflight] {p}")
        if not ok:
            print("[preflight] 拒跑：缺項如上")
            refused_meta = {
                "version": cli_version,
                "git": "",
                "tiers": args.tier,
                "started_at": ts,
                "started_at_utc": datetime.datetime.now(datetime.timezone.utc).isoformat(),
                "preflight_notes": problems,
                "cli_identity": exe_identity,
                "daemon_identity": {"pid": daemon_status.get("pid"),
                                    "version": daemon_version,
                                    "image_path": None, "image_sha256": None},
                "contract_revision": None,
                "cases_yaml_sha256": None,
                "cross_os_guard": cross_os_guard,
            }
            harness.write_reports(report_dir, refused_meta, [], {})
            return 2
        caps = preflight.collect_capabilities(sw)
        missing_caps = preflight.missing_capabilities(caps)
        frontend_ok, _frontend_detail = frontends.frontend_capability(cfg)
        missing_caps.update(preflight.platform_missing_caps(frontend_ok=frontend_ok, cfg=cfg))
        destructive = [c.id for c in selected if c.destructive]
        if destructive:
            print(f"[preflight] 本輪破壞性動作：{', '.join(destructive)}")
        print(f"[realhw] 報告目錄：{report_dir}")

        version = cli_version
        git = subprocess.run(["git", "rev-parse", "--short", "HEAD"],
                             capture_output=True, text=True).stdout.strip()
        cfg["_version"] = version
        cfg["_git"] = git
        started_at_utc = datetime.datetime.now(datetime.timezone.utc).isoformat()
        deployed_start = deployed_content.measure(resolved)
        install = Path(deployed_start["install_dir"]) if deployed_start.get("install_dir") else None
        pid = daemon_status.get("pid")
        live = [deployed_content.live_daemon(int(pid), install)] if pid else []
        daemon_live = live[0] if live else None
        meta = {
            "version": version,
            "git": git,
            "tiers": args.tier, "started_at": ts, "preflight_notes": problems,
            "started_at_utc": started_at_utc,
            "cli_identity": exe_identity,
            "daemon_identity": {"pid": pid, "version": daemon_version,
                                "image_path": daemon_live.get("image_path") if daemon_live else None,
                                "image_sha256": daemon_live.get("image_sha256") if daemon_live else None},
            "contract_revision": _contract_revision(Path(__file__).resolve().parent.parent),
            "cases_yaml_sha256": _sha256_or_none(
                Path(__file__).resolve().parent.parent / "tests/acceptance/cases.yaml"),
            "cross_os_guard": cross_os_guard,
            "cross_os_detail": cross_os_detail,
            "capabilities_missing": missing_caps,
            "win_daemon_present": checks.win_daemon_present,
            "win_daemon_holds": list(checks.win_daemon_holds),
        }
        results = harness.run_cases(selected, ctx, boards=boards, missing_caps=missing_caps)
        deployed_end = deployed_content.measure(resolved)
        meta["deployed_content"] = deployed_content.bracket(
            deployed_start, deployed_end, live=live)
        hints = {c.id: c.hints for c in selected}
        harness.write_reports(report_dir, meta, results, hints)
        print(f"[realhw] 完成：{report_dir}/report.md")
        return 1 if any(r.verdict == "FAIL" for _, r in results) else 0
    finally:
        if bench_fd is not None:
            os.close(bench_fd)


if __name__ == "__main__":
    sys.exit(main())
