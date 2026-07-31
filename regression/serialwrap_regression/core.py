"""核心邏輯——registry 載入、testbed、case dict、blackbox 執行、分診 payload。

不 import testpilot（CI 可測）；鏡射 serialwrap_reliability.core 介面、去 longrun、
加 allow_destructive gate 與 family/issues metadata。
"""
from __future__ import annotations

import time
from pathlib import Path
from typing import Any

from serialwrap_regression import harness
from serialwrap_regression.preflight import REPO_ROOT, ensure_realhw_importable

_TESTBED_DEFAULTS: dict[str, Any] = {
    "serialwrap_exe": "~/.local/bin/serialwrap",
    "allow_destructive": False,
    "tmux_prefix": "swreg",
    "timeouts": {"ready_wait_s": 180, "boot_wait_s": 240, "cmd_timeout_s": 12},
}


def load_testbed(path: Path) -> dict[str, Any]:
    """讀 testbed.yaml（缺檔退 example）；補預設、展開 serialwrap_exe 的 ~。"""
    import yaml

    real = path.with_name("testbed.yaml")
    src = real if real.is_file() else path
    cfg: dict[str, Any] = yaml.safe_load(src.read_text(encoding="utf-8")) or {}
    for key, val in _TESTBED_DEFAULTS.items():
        cfg.setdefault(key, val)
    timeouts = dict(_TESTBED_DEFAULTS["timeouts"])
    timeouts.update(cfg.get("timeouts") or {})
    cfg["timeouts"] = timeouts
    cfg["serialwrap_exe"] = str(Path(str(cfg["serialwrap_exe"])).expanduser())
    cfg["_testbed_source"] = str(src)
    return cfg


def load_registry() -> list[harness.Case]:
    return harness.load_registry()


def case_to_dict(case: harness.Case, cfg: dict[str, Any]) -> dict[str, Any]:
    """Case → testpilot case dict 最小形狀（含 family/issues 供報告追溯）。"""
    devices: dict[str, dict[str, str]] = {}
    for index, board in enumerate(cfg.get("boards", [])):
        com = board.get("com")
        if not com:
            raise ValueError(f"boards[{index}] 缺 'com' 欄位（board={board!r}）")
        if str(com) in devices:
            raise ValueError(f"boards[{index}] 的 com={com!r} 重複，topology key 碰撞")
        devices[str(com)] = {
            "role": str(board.get("alias", "")),
            "serial": str(board.get("serial", "")),
            "platform": str(board.get("platform", "")),
        }
    return {
        "id": case.id,
        "name": case.title,
        "topology": {"devices": devices},
        "steps": [{"id": "exec", "action": "run_case", "target": "bench"}],
        "pass_criteria": ["regression_case_verdict"],
        "metadata": {
            "family": case.family,
            "issues": list(case.issues),
            "destructive": bool(case.destructive),
            "requires": list(case.requires),
            "hints": list(case.hints),
        },
    }


def build_case_dicts(registry: list[harness.Case], cfg: dict[str, Any]) -> list[dict[str, Any]]:
    """整個 registry → case dicts（依 FAMILY_ORDER 排序＝執行順序）。"""
    return [case_to_dict(case, cfg) for case in harness.ordered(registry)]


def filter_for_run(cases: list[dict[str, Any]], requested_ids: set[str]) -> list[dict[str, Any]]:
    """點名時取子集；未點名跑全套（destructive 由 runtime_skip 依 gate 記 SKIP）。"""
    if requested_ids:
        return [case for case in cases if case["id"] in requested_ids]
    return list(cases)


def runtime_skip(
    case_meta: dict[str, Any],
    missing_caps: dict[str, str],
    broken_by: str | None,
    *,
    allow_destructive: bool,
) -> tuple[str, str] | None:
    """執行期 SKIP 判定：destructive gate → bench 破損 → 能力缺項。"""
    if case_meta.get("destructive") and not allow_destructive:
        return ("destructive_gated", "破壞性 case 未解鎖（testbed allow_destructive: false）")
    requires = [str(req) for req in (case_meta.get("requires") or [])]
    if broken_by and (case_meta.get("destructive") or requires):
        return (f"broken_by:{broken_by}", f"前置不滿足（{broken_by} 後板卡未恢復）")
    for req in requires:
        if req in missing_caps:
            return (missing_caps[req], f"能力缺項：{req}")
    return None


def make_skip_result(reason_code: str, comment: str) -> Any:
    ensure_realhw_importable()
    from realhw.harness import CaseResult

    return CaseResult("SKIP", reason=comment, category="environment", reason_code=reason_code)


def result_to_dict(result: Any) -> dict[str, Any]:
    return {
        "verdict": str(getattr(result, "verdict", "")),
        "reason": str(getattr(result, "reason", "") or ""),
        "category": str(getattr(result, "category", "") or ""),
        "reason_code": str(getattr(result, "reason_code", "") or ""),
        "evidence": dict(getattr(result, "evidence", {}) or {}),
        "duration_s": float(getattr(result, "duration_s", 0.0) or 0.0),
    }


def failure_payload(result_dict: dict[str, Any]) -> dict[str, Any] | None:
    """PASS → None；其餘 → testpilot `_last_failure` payload（category 不留空）。"""
    verdict = str(result_dict.get("verdict", ""))
    if verdict == "PASS":
        return None
    category = str(result_dict.get("category", "") or "")
    if not category:
        category = "environment" if verdict == "SKIP" else "test"
    evidence = result_dict.get("evidence") or {}
    return {
        "category": category,
        "reason_code": str(result_dict.get("reason_code", "") or ""),
        "comment": str(result_dict.get("reason", "") or f"regression verdict={verdict}"),
        "evidence": [str(v) for v in evidence.values()],
        "metadata": {"regression_verdict": verdict},
    }


def build_ctx(cfg: dict[str, Any], report_dir: Path) -> Any:
    """建 realhw Ctx（sw 綁 pinned exe；usbipd／win 本 plugin 不用）。"""
    ensure_realhw_importable()
    from realhw import drivers
    from realhw.harness import Ctx

    return Ctx(
        cfg=cfg,
        report_dir=report_dir,
        case_dir=report_dir,
        sw=drivers.SwCli(exe=str(cfg["serialwrap_exe"])),
        tmux=drivers.TmuxCtl(str(cfg.get("tmux_prefix") or "swreg")),
        usbipd=None,
        systemd=drivers.Systemd(),
        win=None,
    )


def run_case_blackbox(case_id: str, ctx: Any) -> Any:
    """black-box 呼叫 case.run(ctx)；未捕捉例外＝FAIL（category=test）。"""
    ensure_realhw_importable()
    from realhw.harness import CaseResult

    target = next((c for c in load_registry() if c.id == case_id), None)
    if target is None:
        return CaseResult(
            "FAIL",
            reason=f"registry 查無 case：{case_id}",
            category="configuration",
            reason_code="invalid_case_config",
        )
    ctx.case_dir = ctx.report_dir / case_id
    t0 = time.monotonic()
    try:
        result = target.run(ctx)
    except Exception as exc:
        result = CaseResult(
            "FAIL",
            reason=f"未捕捉例外：{exc!r}",
            category="test",
            reason_code="uncaught_exception",
        )
    result.duration_s = time.monotonic() - t0
    return result


def recover_boards(ctx: Any, boards: list[str], *, ready_timeout_s: float = 60.0) -> list[str]:
    """case 間恢復：依板卡狀態選語意正確動詞；回未恢復清單。"""
    ensure_realhw_importable()
    from realhw.harness import recovery_command

    not_ready = [b for b in boards if ctx.sw.session(b).get("state") != "READY"]
    if not not_ready:
        return []
    for board in not_ready:
        verb = recovery_command(ctx.sw.session(board).get("state"))
        ctx.sw.run(*verb, "--selector", board)
    time.sleep(5)
    return [b for b in boards if not ctx.sw.wait_state(b, "READY", timeout_s=ready_timeout_s)]


def sweep_tmux(prefix: str) -> list[str]:
    """掃掉殘留 tmux session（收尾）。"""
    import subprocess

    cp = subprocess.run(["tmux", "ls", "-F", "#{session_name}"], capture_output=True, text=True)
    killed: list[str] = []
    for name in (cp.stdout or "").splitlines():
        name = name.strip()
        if name.startswith(f"{prefix}-"):
            subprocess.run(["tmux", "kill-session", "-t", name], capture_output=True, text=True)
            killed.append(name)
    return killed


__all__ = [
    "REPO_ROOT",
    "build_case_dicts",
    "build_ctx",
    "case_to_dict",
    "failure_payload",
    "filter_for_run",
    "load_registry",
    "load_testbed",
    "make_skip_result",
    "recover_boards",
    "result_to_dict",
    "run_case_blackbox",
    "runtime_skip",
    "sweep_tmux",
]
