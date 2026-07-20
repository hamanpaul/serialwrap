"""serialwrap_reliability 核心邏輯——不 import testpilot 的薄 adapter。"""
from __future__ import annotations

import re
import subprocess
import sys
import threading
import time
from pathlib import Path
from typing import Any, Callable


def resolve_repo_root(core_file: Path) -> Path:
    """由模組檔位置推回 repo root；僅支援 editable 安裝。"""
    root = Path(core_file).resolve().parents[2]
    if not (root / "realhw" / "harness.py").is_file():
        raise RuntimeError(
            f"serialwrap_reliability.core 僅支援 editable 安裝；"
            f"REPO_ROOT={root} 下找不到 realhw/harness.py。"
            "請從 repo root 執行 pip install -e reliability/"
        )
    return root


REPO_ROOT: Path = resolve_repo_root(Path(__file__))


def ensure_realhw_importable() -> Path:
    """把 repo root 冪等插入 ``sys.path``，讓 ``import realhw`` 可用。"""
    root = str(REPO_ROOT)
    if root not in sys.path:
        sys.path.insert(0, root)
    return REPO_ROOT


def load_registry() -> list[Any]:
    """載入 realhw registry，回傳淺拷貝。

    首次呼叫會 import ``realhw.cases`` 觸發所有 case 的 ``register()``，
    將結果寫入 ``harness.REGISTRY``；此副作用是 process-wide 且不可逆。
    後續呼叫只讀取既有 registry，不重複註冊。
    """
    ensure_realhw_importable()
    import realhw.cases  # noqa: F401
    from realhw import harness

    return list(harness.REGISTRY)


def synth_longrun_steps(duration_s: int, interval_s: int) -> list[dict[str, Any]]:
    """duration/interval → N 個 checkpoint step（最少 1）。"""
    n = max(1, int(duration_s) // max(1, int(interval_s)))
    return [
        {"id": f"checkpoint-{i:03d}", "action": "longrun_checkpoint", "target": "bench"}
        for i in range(1, n + 1)
    ]


def case_to_dict(case: Any, cfg: dict[str, Any]) -> dict[str, Any]:
    """realhw Case → testpilot case dict 最小形狀。"""
    devices: dict[str, dict[str, str]] = {}
    for index, board in enumerate(cfg.get("boards", [])):
        com = board.get("com")
        if not com:
            raise ValueError(f"boards[{index}] 缺 'com' 欄位（board={board!r}）")
        if str(com) in devices:
            raise ValueError(f"boards[{index}] 的 com={com!r} 與前面的 board 重複，topology key 碰撞")
        devices[str(com)] = {
            "role": str(board.get("alias", "")),
            "serial": str(board.get("serial", "")),
            "busid": str(board.get("busid", "")),
            "platform": str(board.get("platform", "")),
        }
    if case.tier == "longrun":
        interval_s = int((cfg.get("longrun") or {}).get("snapshot_interval_s") or 300)
        steps = synth_longrun_steps(int(cfg.get("duration_s") or 0), interval_s)
    else:
        steps = [{"id": "exec", "action": "run_case", "target": "bench"}]
    return {
        "id": case.id,
        "name": case.title,
        "topology": {"devices": devices},
        "steps": steps,
        "pass_criteria": ["realhw_case_verdict"],
        "metadata": {
            "tier": case.tier,
            "destructive": bool(case.destructive),
            "requires": list(case.requires),
            "hints": list(case.hints),
        },
    }


def build_case_dicts(registry: list[Any], cfg: dict[str, Any]) -> list[dict[str, Any]]:
    """整個 registry → case dicts（維持註冊順序）。"""
    return [case_to_dict(case, cfg) for case in registry]


def filter_for_run(cases: list[dict[str, Any]], requested_ids: set[str]) -> list[dict[str, Any]]:
    """預設排除 destructive；顯式點名時允許。"""
    if requested_ids:
        return [case for case in cases if case["id"] in requested_ids]
    return [case for case in cases if not (case.get("metadata") or {}).get("destructive")]


def result_to_dict(result: Any) -> dict[str, Any]:
    """CaseResult → plain dict。"""
    return {
        "verdict": str(getattr(result, "verdict", "")),
        "reason": str(getattr(result, "reason", "") or ""),
        "category": str(getattr(result, "category", "") or ""),
        "reason_code": str(getattr(result, "reason_code", "") or ""),
        "evidence": dict(getattr(result, "evidence", {}) or {}),
        "duration_s": float(getattr(result, "duration_s", 0.0) or 0.0),
    }


def failure_payload(result_dict: dict[str, Any]) -> dict[str, Any] | None:
    """CaseResult dict → ``_last_failure`` payload。"""
    verdict = str(result_dict.get("verdict", ""))
    if verdict == "PASS":
        return None
    category = str(result_dict.get("category", "") or "")
    if verdict == "SKIP" and not category:
        category = "environment"
    evidence = result_dict.get("evidence") or {}
    return {
        "category": category,
        "reason_code": str(result_dict.get("reason_code", "") or ""),
        "comment": str(result_dict.get("reason", "") or f"realhw verdict={verdict}"),
        "evidence": [str(v) for v in evidence.values()],
        "metadata": {"realhw_verdict": verdict},
    }


def runtime_skip(
    case_meta: dict[str, Any], missing_caps: dict[str, str], broken_by: str | None
) -> tuple[str, str] | None:
    """執行期 SKIP 判定。"""
    requires = [str(req) for req in (case_meta.get("requires") or [])]
    if broken_by and ("two_boards" in requires or case_meta.get("destructive")):
        return (f"broken_by:{broken_by}", f"前置不滿足（{broken_by} 後板卡未恢復）")
    for req in requires:
        if req in missing_caps:
            return (missing_caps[req], f"能力缺項：{req}")
    return None


def make_skip_result(reason_code: str, comment: str) -> Any:
    """合成執行期 SKIP 的 CaseResult。"""
    ensure_realhw_importable()
    from realhw import harness

    return harness.CaseResult(
        "SKIP", reason=comment, category="environment", reason_code=reason_code
    )


def build_ctx(cfg: dict[str, Any], report_dir: Path) -> Any:
    """建 realhw Ctx。"""
    ensure_realhw_importable()
    from realhw import drivers, harness

    return harness.Ctx(
        cfg=cfg,
        report_dir=report_dir,
        case_dir=report_dir,
        sw=drivers.SwCli(),
        tmux=drivers.TmuxCtl(str(cfg.get("tmux_prefix") or "realhw")),
        usbipd=drivers.Usbipd(str(cfg.get("usbipd_exe") or "")),
        systemd=drivers.Systemd(),
        win=drivers.WinSwCli(cfg["win_serialwrap_exe"]),
    )


def run_case_blackbox(case_id: str, ctx: Any) -> Any:
    """black-box 呼叫 realhw ``case.run(ctx)``。"""
    ensure_realhw_importable()
    from realhw import harness

    target = next((case for case in harness.REGISTRY if case.id == case_id), None)
    if target is None:
        return harness.CaseResult(
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
        result = harness.CaseResult(
            "FAIL", reason=f"未捕捉例外：{exc!r}", reason_code="uncaught_exception"
        )
    result.duration_s = time.monotonic() - t0
    return result


def recover_boards(ctx: Any, boards: list[str], *, ready_timeout_s: float = 60.0) -> list[str]:
    """case 間恢復。"""
    ensure_realhw_importable()
    from realhw import harness

    not_ready = [board for board in boards if ctx.sw.session(board).get("state") != "READY"]
    if not not_ready:
        return []
    for board in not_ready:
        verb = harness.recovery_command(ctx.sw.session(board).get("state"))
        ctx.sw.run(*verb, "--selector", board)
    time.sleep(5)
    return [
        board
        for board in boards
        if not ctx.sw.wait_state(board, "READY", timeout_s=ready_timeout_s)
    ]


def sweep_tmux(prefix: str) -> list[str]:
    """掃掉 ``<prefix>-`` 開頭的殘留 tmux session。"""
    cp = subprocess.run(["tmux", "ls", "-F", "#{session_name}"], capture_output=True, text=True)
    killed: list[str] = []
    for name in (cp.stdout or "").splitlines():
        name = name.strip()
        if name.startswith(f"{prefix}-"):
            subprocess.run(["tmux", "kill-session", "-t", name], capture_output=True, text=True)
            killed.append(name)
    return killed


def checkpoint_index(step_id: str, *, fallback: int) -> int:
    """``checkpoint-007`` → 7；解析不出退 fallback。"""
    match = re.search(r"(\d+)$", step_id.strip())
    return int(match.group(1)) if match else fallback


class LongrunRunner:
    """背景 thread 跑 realhw 長跑 case。"""

    def __init__(self, run_fn: Callable[[], Any], snapshots_path: Path, duration_s: int) -> None:
        self._run_fn = run_fn
        self._snapshots_path = Path(snapshots_path)
        self._duration_s = max(0, int(duration_s))
        self._result: Any = None
        self._thread: threading.Thread | None = None
        self._started_at = 0.0

    @classmethod
    def skipped(cls, result: Any) -> "LongrunRunner":
        runner = cls(lambda: result, Path("nonexistent-snapshots"), 0)
        runner._result = result
        return runner

    def start(self) -> None:
        if self._thread is not None or self._result is not None:
            return
        self._started_at = time.monotonic()

        def _run() -> None:
            self._result = self._run_fn()

        self._thread = threading.Thread(target=_run, name="reliability-longrun", daemon=True)
        self._thread.start()

    def wait_checkpoint(self, index: int, total: int) -> dict[str, Any]:
        """等到第 index/total 檢查點。"""
        total = max(1, total)
        if self._thread is not None:
            deadline = self._started_at + self._duration_s * (min(index, total) / total)
            while time.monotonic() < deadline and self._thread.is_alive():
                time.sleep(min(5.0, max(0.1, deadline - time.monotonic())))
            if index >= total:
                self._thread.join()
        seen = 0
        if self._snapshots_path.exists():
            text = self._snapshots_path.read_text(encoding="utf-8", errors="replace")
            seen = sum(1 for line in text.splitlines() if line.strip())
        finished = self._thread is None or not self._thread.is_alive()
        return {
            "checkpoint": index,
            "total": total,
            "snapshots_seen": seen,
            "finished": finished,
        }

    def result(self) -> Any:
        """取回結果；未完成時 join。"""
        if self._thread is not None and self._thread.is_alive():
            self._thread.join()
        return self._result


def run_preflight(cfg: dict[str, Any]) -> dict[str, Any]:
    """realhw preflight gate。"""
    ensure_realhw_importable()
    from realhw import drivers, preflight

    sw = drivers.SwCli()
    win = drivers.WinSwCli(cfg["win_serialwrap_exe"])
    lock_fd = preflight.acquire_benchlock(preflight.bench_lock_path())
    checks = preflight.collect(cfg, sw, REPO_ROOT, benchlock_ok=lock_fd is not None, win=win)
    ok, problems = preflight.evaluate(checks)
    missing_caps: dict[str, str] = {}
    deployed_version = ""
    if ok:
        caps = preflight.collect_capabilities(sw)
        missing_caps = dict(preflight.missing_capabilities(caps))
        deployed_version = str(caps.deployed_version).strip()
    return {
        "ok": bool(ok),
        "problems": list(problems),
        "missing_caps": missing_caps,
        "deployed_version": deployed_version,
        "benchlock_fd": lock_fd,
    }
