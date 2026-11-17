#!/usr/bin/env python3
"""驗證並匯入 Slice 2.1 的容器 sys_admin L1 證據。"""
from __future__ import annotations

import argparse
import hashlib
import json
import pathlib
import re
import shutil
import subprocess
import sys
from collections.abc import Mapping
from typing import Any

ROOT = pathlib.Path(__file__).resolve().parents[2]
JUDGE = ROOT / "scripts/rust/slice21_gate_judge.py"
SECTION = "linux_cap_sys_admin"
REQUIRED_SET = "s21_gate:cap_sys_admin"
EVIDENCE_FILE = "sys-admin-evidence.json"
L1_FILE = "l1-results.json"
LOG_FILE = "container.log"
_CAP_SYS_ADMIN = 1 << 21
_HEX_RE = re.compile(r"^[0-9a-fA-F]+$")
_SHA256_RE = re.compile(r"^sha256:[0-9a-f]{64}$")
_COMMIT_RE = re.compile(r"^[0-9a-f]{40}$")


def _load_json(path: pathlib.Path) -> Any:
    return json.loads(path.read_text(encoding="utf-8"))


def _locked_plan() -> dict[str, Any]:
    proc = subprocess.run(
        [sys.executable, str(JUDGE), "caps-plan", "--section", SECTION],
        cwd=ROOT, capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=30,
        check=False,
    )
    if proc.returncode != 0:
        raise RuntimeError(f"無法從凍結 selector 推導 sys_admin 計畫：{proc.stderr.strip()}")
    plan = json.loads(proc.stdout)
    if (set(plan) != {"caps", "argv", "caps_only"} or plan.get("caps") != ["sys_admin"]
            or plan.get("caps_only") is not True or not isinstance(plan.get("argv"), list)
            or not plan["argv"] or not all(isinstance(arg, str) for arg in plan["argv"])):
        raise RuntimeError(f"鎖中的 sys_admin 計畫不符預期：{plan!r}")
    return plan


def _regular_file(directory: pathlib.Path, name: str) -> pathlib.Path:
    path = directory / name
    if path.is_symlink() or not path.is_file():
        raise ValueError(f"缺少一般檔案 {name}")
    return path


def validate(results: pathlib.Path, candidate_commit: str) -> tuple[dict[str, Any] | None, list[str]]:
    """核對容器版本、凍結 selector、候選 HEAD、L1 provenance 與獨立能力探測。"""
    errors: list[str] = []
    if not _COMMIT_RE.fullmatch(candidate_commit):
        return None, [f"候選 HEAD 格式錯誤：{candidate_commit!r}"]
    try:
        directory = results.resolve(strict=True)
        if not directory.is_dir():
            return None, [f"sys_admin 證據路徑不是目錄：{results}"]
        evidence_path = _regular_file(directory, EVIDENCE_FILE)
        l1_path = _regular_file(directory, L1_FILE)
        log_path = _regular_file(directory, LOG_FILE)
        metadata = _load_json(evidence_path)
        l1 = _load_json(l1_path)
    except (OSError, ValueError, json.JSONDecodeError) as exc:
        return None, [f"sys_admin 證據不可讀：{exc}"]

    try:
        plan = _locked_plan()
    except (OSError, RuntimeError, ValueError, json.JSONDecodeError) as exc:
        return None, [str(exc)]

    if not isinstance(metadata, Mapping):
        errors.append("sys-admin-evidence.json 須為 JSON 物件")
        metadata = {}
    if metadata.get("schema") != "slice21-sys-admin-evidence.v1":
        errors.append("sys_admin evidence schema 不符")
    if metadata.get("candidate_commit") != candidate_commit:
        errors.append(f"sys_admin 證據 HEAD 不符：{metadata.get('candidate_commit')!r} != {candidate_commit}")
    if metadata.get("required_set") != REQUIRED_SET:
        errors.append(f"required_set 不符：{metadata.get('required_set')!r}")
    if metadata.get("platform") != "linux":
        errors.append(f"platform 不符：{metadata.get('platform')!r}")
    if metadata.get("selector") != plan["argv"]:
        errors.append("證據 selector 與 l1_required.lock.json 不符")
    if metadata.get("caps") != plan["caps"] or metadata.get("caps_only") is not plan["caps_only"]:
        errors.append("證據 --caps／--caps-only 與凍結 selector 計畫不符")
    if metadata.get("image") != "ubuntu:24.04":
        errors.append(f"容器 image 不符：{metadata.get('image')!r}")
    image_os = metadata.get("container_os")
    if not isinstance(image_os, Mapping) or image_os.get("id") != "ubuntu" or image_os.get("version_id") != "24.04":
        errors.append("容器 /etc/os-release 不是 Ubuntu 24.04")
    if not isinstance(metadata.get("image_id"), str) or not _SHA256_RE.fullmatch(metadata["image_id"]):
        errors.append("缺少可核對的 Ubuntu image digest")
    target_dir = metadata.get("cargo_target_dir")
    if not isinstance(target_dir, str) or not target_dir.startswith("/tmp/") or target_dir.startswith("/workspace/"):
        errors.append(f"CARGO_TARGET_DIR 未隔離於 repo 外：{target_dir!r}")

    if not isinstance(metadata.get("probe"), Mapping):
        errors.append("缺少 euid／CapEff 探測證據")
        probe: Mapping[str, Any] = {}
    else:
        probe = metadata["probe"]
    euid = probe.get("euid")
    cap_eff = probe.get("cap_eff")
    if not isinstance(euid, int) or isinstance(euid, bool) or euid != 0:
        errors.append(f"容器探測 euid 必須為 0：{euid!r}")
    if not isinstance(cap_eff, str) or not _HEX_RE.fullmatch(cap_eff):
        errors.append(f"容器探測 CapEff 格式錯誤：{cap_eff!r}")
    elif not (int(cap_eff, 16) & _CAP_SYS_ADMIN):
        errors.append(f"容器 CapEff 未包含 CAP_SYS_ADMIN：{cap_eff}")

    if not isinstance(l1, Mapping):
        errors.append("l1-results.json 須為 JSON 物件")
        l1 = {}
    if l1.get("format") != "l1-results.v1" or l1.get("schema_version") != 2:
        errors.append("L1 格式不是 l1-results.v1／schema_version 2")
    if l1.get("runner") != "cargo" or l1.get("platform") != "linux":
        errors.append("L1 runner／platform 必須為 cargo／linux")
    if l1.get("argv") != plan["argv"]:
        errors.append("L1 argv 與鎖中的 selector 不符")
    l1_exit = l1.get("exit_code")
    if not isinstance(l1_exit, int) or isinstance(l1_exit, bool):
        errors.append(f"L1 exit_code 必須為整數：{l1_exit!r}")
    cargo_exit = metadata.get("cargo_exit_code")
    if not isinstance(cargo_exit, int) or isinstance(cargo_exit, bool) or cargo_exit != l1_exit:
        errors.append("容器記錄的 cargo exit code 與 l1-results 不符")
    provenance = l1.get("provenance")
    if not isinstance(provenance, Mapping) or provenance.get("schema") != "cargo-l1-provenance.v1":
        errors.append("L1 缺少 cargo-l1-provenance.v1")
    else:
        if provenance.get("platform") != "linux":
            errors.append("L1 provenance platform 必須為 linux")
        git = provenance.get("git")
        end = git.get("end") if isinstance(git, Mapping) else None
        if not isinstance(git, Mapping) or git.get("head") != candidate_commit or git.get("clean") is not True:
            errors.append("L1 起始 git provenance HEAD／clean 不符")
        if not isinstance(end, Mapping) or end.get("head") != candidate_commit or end.get("clean") is not True:
            errors.append("L1 結束 git provenance HEAD／clean 不符")
    if l1.get("finished") is not True:
        errors.append("L1 收集／執行尚未完整結束")

    if errors:
        return None, errors
    return {
        "metadata": metadata,
        "l1": l1,
        "evidence_path": evidence_path,
        "l1_path": l1_path,
        "log_path": log_path,
        "euid": euid,
        "cap_eff": cap_eff,
    }, []


def _record(run_dir: pathlib.Path, *, command: str, exit_code: int | None = None,
            l1_rel: str | None = None, probe: str, run_euid: int | None = None,
            reasons: list[str] | None = None) -> None:
    argv = [
        sys.executable, str(JUDGE), "record-section", "--run-dir", str(run_dir),
        "--name", SECTION, "--required-set", REQUIRED_SET, "--command", command,
        "--log", "logs/linux_cap_sys_admin.log", "--capability-probe", probe,
    ]
    if exit_code is not None:
        argv.extend(("--exit", str(exit_code)))
    if l1_rel is not None:
        argv.extend(("--l1-results", l1_rel))
    if run_euid is not None:
        argv.extend(("--run-euid", str(run_euid)))
    if reasons:
        argv.extend(("--section-verdict", "EVIDENCE_MISSING"))
        for reason in reasons:
            argv.extend(("--reason", reason))
    proc = subprocess.run(argv, cwd=ROOT, capture_output=True, text=True, encoding="utf-8", errors="replace",
                          timeout=30, check=False)
    if proc.returncode != 0:
        raise RuntimeError(f"record-section 失敗：{proc.stdout}{proc.stderr}")


def import_section(results: pathlib.Path, run_dir: pathlib.Path, candidate_commit: str) -> dict[str, Any]:
    run_dir.mkdir(parents=True, exist_ok=True)
    logs_dir = run_dir / "logs"
    logs_dir.mkdir(parents=True, exist_ok=True)
    log_dest = logs_dir / "linux_cap_sys_admin.log"
    accepted, reasons = validate(results, candidate_commit)
    command = f"SW_GATE_SYS_ADMIN_RESULTS 匯入：{results}"
    if accepted is None:
        log_dest.write_text("sys_admin 證據拒收：\n" + "\n".join(reasons) + "\n", encoding="utf-8")
        safe_reasons = reasons or ["sys_admin 證據不可用"]
        _record(run_dir, command=command, probe="sys_admin=unavailable:" + "; ".join(safe_reasons),
                reasons=safe_reasons)
        return {"accepted": False, "reasons": safe_reasons}

    destination = run_dir / SECTION
    copied_l1 = destination / L1_FILE
    copied_evidence = destination / EVIDENCE_FILE
    if destination.exists() or log_dest.exists():
        reasons = ["拒絕覆寫本次 gate run 已存在的 sys_admin 證據"]
        log_dest.write_text("sys_admin 證據拒收：\n" + reasons[0] + "\n", encoding="utf-8")
        _record(run_dir, command=command, probe="sys_admin=unavailable:" + reasons[0], reasons=reasons)
        return {"accepted": False, "reasons": reasons}
    destination.mkdir(parents=True)
    shutil.copyfile(accepted["l1_path"], copied_l1)
    shutil.copyfile(accepted["evidence_path"], copied_evidence)
    shutil.copyfile(accepted["log_path"], log_dest)
    probe = f"sys_admin=available:euid={accepted['euid']} CapEff={accepted['cap_eff']}"
    exit_code = accepted["l1"].get("exit_code")
    _record(run_dir, command=command, exit_code=exit_code, l1_rel=f"{SECTION}/{L1_FILE}",
            probe=probe, run_euid=accepted["euid"])
    return {
        "accepted": True, "candidate_commit": candidate_commit, "required_set": REQUIRED_SET,
        "selector": accepted["metadata"]["selector"], "capability_probe": probe,
        "l1_exit_code": exit_code,
        "l1_sha256": hashlib.sha256(copied_l1.read_bytes()).hexdigest(),
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest="command", required=True)
    check = sub.add_parser("validate", help="只驗證容器證據並輸出 JSON")
    check.add_argument("--results", required=True)
    check.add_argument("--candidate-commit", required=True)
    imp = sub.add_parser("import-section", help="驗證證據並寫入 gate section 片段")
    imp.add_argument("--results", required=True)
    imp.add_argument("--run-dir", required=True)
    imp.add_argument("--candidate-commit", required=True)
    args = parser.parse_args(argv)
    try:
        if args.command == "validate":
            accepted, reasons = validate(pathlib.Path(args.results), args.candidate_commit)
            if accepted is None:
                print(json.dumps({"accepted": False, "reasons": reasons}, ensure_ascii=False, sort_keys=True))
                return 1
            metadata = accepted["metadata"]
            print(json.dumps({
                "accepted": True, "candidate_commit": metadata["candidate_commit"],
                "required_set": metadata["required_set"], "selector": metadata["selector"],
                "probe": metadata["probe"], "cargo_exit_code": accepted["l1"]["exit_code"],
            }, ensure_ascii=False, sort_keys=True))
            return 0
        result = import_section(pathlib.Path(args.results), pathlib.Path(args.run_dir), args.candidate_commit)
        print(json.dumps(result, ensure_ascii=False, sort_keys=True))
        return 0
    except (OSError, RuntimeError, ValueError, json.JSONDecodeError, subprocess.SubprocessError) as exc:
        print(f"slice21_sys_admin_evidence.py: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
